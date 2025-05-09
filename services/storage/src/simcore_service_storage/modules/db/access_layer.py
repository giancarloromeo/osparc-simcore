"""Helper functions to determin access-rights on stored data

# DRAFT Rationale:

    osparc-simcore defines TWO authorization methods: i.e. a set of rules on what,
    how and when any resource can be accessed or operated by a user

    ## ROLE-BASED METHOD:
        In this method, a user is assigned a role (user/tester/admin) upon registration. Each role is
        system-wide and defines a set of operations that the user *can* perform
            - Every operation is named as a resource and an action (e.g. )
            - Resource is named hierarchically
            - Roles can inherit permitted operations from other role
        This method is static because is system-wide and it is defined directly in the
        code at services/web/server/src/simcore_service_webserver/security_roles.py
        It is defined on top of every API entrypoint and applied just after authentication of the user.

    ## GROUP-BASED METHOD:
        The second method is designed to dynamically share a resource among groups of users. A group
        defines a set of rules that apply to a resource and users can be added to the group dynamically.
        So far, there are two resources that define access rights (AR):
            - one applies to projects (read/write/delete) and
            - the other to services (execute/write)
        The project access rights are set in the column "access_rights" of the "projects" table .
        The service access rights has its own table: service_access_rights

        Access rights apply hierarchically, meaning that the access granted to a project applies
        to all nodes inside and stored data in nodes.

        How do these two AR coexist?: Access to read, write or delete a project are defined in the project AR but execution
        will depend on the service AR attached to nodes inside.

        What about stored data?
        - data generated in nodes inherits the AR from the associated project
        - data generated in API uses full AR provided by ownership (i.e. user_id in files_meta_data table)

"""

import logging

import sqlalchemy as sa
from models_library.groups import GroupID
from models_library.projects import ProjectID
from models_library.projects_nodes_io import StorageFileID
from models_library.users import UserID
from simcore_postgres_database.models.project_to_groups import project_to_groups
from simcore_postgres_database.models.projects import projects
from simcore_postgres_database.models.workspaces_access_rights import (
    workspaces_access_rights,
)
from simcore_postgres_database.storage_models import file_meta_data, user_to_groups
from simcore_postgres_database.utils_repos import pass_or_acquire_connection
from sqlalchemy.ext.asyncio import AsyncConnection

from ...constants import EXPORTS_S3_PREFIX
from ...exceptions.errors import InvalidFileIdentifierError
from ...models import AccessRights
from ._base import BaseRepository

_logger = logging.getLogger(__name__)


async def _get_user_groups_ids(
    connection: AsyncConnection, user_id: UserID
) -> list[GroupID]:
    stmt = sa.select(user_to_groups.c.gid).where(user_to_groups.c.uid == user_id)
    rows = (await connection.execute(stmt)).fetchall()
    assert rows is not None  # nosec
    return [g.gid for g in rows]


def _aggregate_access_rights(
    access_rights: dict[str, dict], group_ids: list[GroupID]
) -> AccessRights:
    try:
        prj_access = {"read": False, "write": False, "delete": False}
        for gid, grp_access in access_rights.items():
            if int(gid) in group_ids:
                for operation in grp_access:
                    prj_access[operation] |= grp_access[operation]

        return AccessRights(**prj_access)
    except KeyError:
        # NOTE: database does NOT include schema for json access_rights column!
        _logger.warning(
            "Invalid entry in projects.access_rights. Revoking all rights [%s]",
            access_rights,
        )
        return AccessRights.none()


def my_private_workspace_access_rights_subquery(user_group_ids: list[GroupID]):
    return (
        sa.select(
            project_to_groups.c.project_uuid,
            sa.func.jsonb_object_agg(
                project_to_groups.c.gid,
                sa.func.jsonb_build_object(
                    "read",
                    project_to_groups.c.read,
                    "write",
                    project_to_groups.c.write,
                    "delete",
                    project_to_groups.c.delete,
                ),
            ).label("access_rights"),
        )
        .where(
            (project_to_groups.c.read)  # Filters out entries where "read" is False
            & (
                project_to_groups.c.gid.in_(user_group_ids)
            )  # Filters gid to be in user_groups
        )
        .group_by(project_to_groups.c.project_uuid)
    ).subquery("my_access_rights_subquery")


def my_shared_workspace_access_rights_subquery(user_group_ids: list[GroupID]):
    return (
        sa.select(
            workspaces_access_rights.c.workspace_id,
            sa.func.jsonb_object_agg(
                workspaces_access_rights.c.gid,
                sa.func.jsonb_build_object(
                    "read",
                    workspaces_access_rights.c.read,
                    "write",
                    workspaces_access_rights.c.write,
                    "delete",
                    workspaces_access_rights.c.delete,
                ),
            ).label("access_rights"),
        )
        .where(
            (
                workspaces_access_rights.c.read
            )  # Filters out entries where "read" is False
            & (
                workspaces_access_rights.c.gid.in_(user_group_ids)
            )  # Filters gid to be in user_groups
        )
        .group_by(workspaces_access_rights.c.workspace_id)
    ).subquery("my_workspace_access_rights_subquery")


async def _list_user_projects_access_rights_with_read_access(
    connection: AsyncConnection, user_id: UserID
) -> list[ProjectID]:
    """
    Returns access-rights of user (user_id) over all OWNED or SHARED projects
    """

    user_group_ids: list[GroupID] = await _get_user_groups_ids(connection, user_id)
    _my_access_rights_subquery = my_private_workspace_access_rights_subquery(
        user_group_ids
    )

    private_workspace_query = (
        sa.select(
            projects.c.uuid,
        )
        .select_from(projects.join(_my_access_rights_subquery))
        .where(projects.c.workspace_id.is_(None))
    )

    _my_workspace_access_rights_subquery = my_shared_workspace_access_rights_subquery(
        user_group_ids
    )

    shared_workspace_query = (
        sa.select(projects.c.uuid)
        .select_from(
            projects.join(
                _my_workspace_access_rights_subquery,
                projects.c.workspace_id
                == _my_workspace_access_rights_subquery.c.workspace_id,
            )
        )
        .where(projects.c.workspace_id.is_not(None))
    )

    combined_query = sa.union_all(private_workspace_query, shared_workspace_query)

    projects_access_rights = []

    async for row in await connection.stream(combined_query):
        assert isinstance(row.uuid, str)  # nosec

        projects_access_rights.append(ProjectID(row.uuid))

    return projects_access_rights


class AccessLayerRepository(BaseRepository):
    async def get_project_access_rights(
        self,
        *,
        connection: AsyncConnection | None = None,
        user_id: UserID,
        project_id: ProjectID,
    ) -> AccessRights:
        """
        Returns access-rights of user (user_id) over a project resource (project_id)
        """

        async with pass_or_acquire_connection(self.db_engine, connection) as conn:
            user_group_ids = await _get_user_groups_ids(conn, user_id)
            _my_access_rights_subquery = my_private_workspace_access_rights_subquery(
                user_group_ids
            )

            private_workspace_query = (
                sa.select(
                    projects.c.prj_owner,
                    _my_access_rights_subquery.c.access_rights,
                )
                .select_from(projects.join(_my_access_rights_subquery))
                .where(
                    (projects.c.uuid == f"{project_id}")
                    & (projects.c.workspace_id.is_(None))
                )
            )

            _my_workspace_access_rights_subquery = (
                my_shared_workspace_access_rights_subquery(user_group_ids)
            )

            shared_workspace_query = (
                sa.select(
                    projects.c.prj_owner,
                    _my_workspace_access_rights_subquery.c.access_rights,
                )
                .select_from(
                    projects.join(
                        _my_workspace_access_rights_subquery,
                        projects.c.workspace_id
                        == _my_workspace_access_rights_subquery.c.workspace_id,
                    )
                )
                .where(
                    (projects.c.uuid == f"{project_id}")
                    & (projects.c.workspace_id.is_not(None))
                )
            )

            combined_query = sa.union_all(
                private_workspace_query, shared_workspace_query
            )
            result = await conn.execute(combined_query)
            row = result.one_or_none()

        if not row:
            # Either project does not exists OR user_id has NO access
            return AccessRights.none()

        assert row.prj_owner is None or isinstance(row.prj_owner, int)  # nosec
        assert isinstance(row.access_rights, dict)  # nosec

        if row.prj_owner == user_id:
            return AccessRights.all()

        # determine user's access rights by aggregating AR of all groups
        return _aggregate_access_rights(row.access_rights, user_group_ids)

    async def _get_access_from_metadata_entry(
        self,
        *,
        connection: AsyncConnection | None,
        user_id: UserID,
        file_id: StorageFileID,
    ) -> AccessRights | None:
        #
        # 1. file registered in file_meta_data table
        #
        stmt = sa.select(file_meta_data.c.project_id, file_meta_data.c.user_id).where(
            file_meta_data.c.file_id == f"{file_id}"
        )
        async with pass_or_acquire_connection(self.db_engine, connection) as conn:
            result = await conn.execute(stmt)
            row = result.one_or_none()

        if not row:
            return None

        if int(row.user_id) == user_id:
            # is owner
            return AccessRights.all()

        if not row.project_id:
            # not owner and not shared via project
            return AccessRights.none()

        # has associated project
        access_rights = await self.get_project_access_rights(
            user_id=user_id, project_id=row.project_id
        )
        if not access_rights:
            _logger.warning(
                "File %s references a project %s that does not exists in db. "
                " TIP: Audit sync between files_meta_data and projects tables",
                file_id,
                row.project_id,
            )
            return AccessRights.none()

        return access_rights

    async def _get_access_without_metardata_entry(
        self,
        *,
        user_id: UserID,
        file_id: StorageFileID,
    ) -> AccessRights:
        #
        # 2. file is NOT registered in meta-data table e.g. it is about to be uploaded or it was deleted
        #    We rely on the assumption that file_id is formatted either as
        #
        #       - project's data: {project_id}/{node_id}/{filename/with/possible/folders}
        #       - API data:       api/{file_id}/{filename/with/possible/folders}
        #       - Exporter data:  exporter/{user_id}/{filename/with/possible/folders}
        #

        try:
            parent, _, _ = file_id.split("/", maxsplit=2)

            if parent == "api":
                # ownership still not defined, so we assume it is user_id
                return AccessRights.all()

            if parent == EXPORTS_S3_PREFIX:
                # ownership still not defined, so we assume it is user_id
                # NOTE: all permissions are required for: downloading, uploading and aborting
                return AccessRights.all()

            # otherwise assert 'parent' string corresponds to a valid UUID
            access_rights = await self.get_project_access_rights(
                user_id=user_id, project_id=ProjectID(parent)
            )
            if not access_rights:
                _logger.warning(
                    "File %s references a project that does not exists in db",
                    file_id,
                )
                return AccessRights.none()

            return access_rights

        except (ValueError, AttributeError) as err:
            raise InvalidFileIdentifierError(
                identifier=file_id,
                details=str(err),
            ) from err

    async def get_file_access_rights(
        self,
        *,
        connection: AsyncConnection | None = None,
        user_id: UserID,
        file_id: StorageFileID,
    ) -> AccessRights:
        """
        Returns access-rights of user (user_id) over data file resource (file_id)

        Raises InvalidFileIdentifierError
        """
        access_rights = await self._get_access_from_metadata_entry(
            connection=connection, user_id=user_id, file_id=file_id
        )

        if access_rights is not None:
            return access_rights

        return await self._get_access_without_metardata_entry(
            user_id=user_id, file_id=file_id
        )

    async def get_readable_project_ids(
        self, *, connection: AsyncConnection | None = None, user_id: UserID
    ) -> list[ProjectID]:
        """Returns a list of projects where user has granted read-access"""
        async with pass_or_acquire_connection(self.db_engine, connection) as conn:
            return await _list_user_projects_access_rights_with_read_access(
                conn, user_id
            )
