import functools
from uuid import UUID

import sqlalchemy as sa
from simcore_postgres_database.models.groups import user_to_groups
from simcore_postgres_database.models.projects_tags import projects_tags
from simcore_postgres_database.models.services_tags import services_tags
from simcore_postgres_database.models.tags import tags
from simcore_postgres_database.models.tags_access_rights import tags_access_rights
from simcore_postgres_database.models.users import users
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql.selectable import ScalarSelect
from typing_extensions import TypedDict

_TAG_COLUMNS = [
    tags.c.id,
    tags.c.name,
    tags.c.description,
    tags.c.color,
]

_ACCESS_RIGHTS_COLUMNS = [
    tags_access_rights.c.read,
    tags_access_rights.c.write,
    tags_access_rights.c.delete,
]


def _join_user_groups_tag(*, access_condition, tag_id: int, user_id: int):
    return user_to_groups.join(
        tags_access_rights,
        (user_to_groups.c.uid == user_id)
        & (user_to_groups.c.gid == tags_access_rights.c.group_id)
        & (access_condition)
        & (tags_access_rights.c.tag_id == tag_id),
    )


def _join_user_to_given_tag(*, access_condition, tag_id: int, user_id: int):
    return _join_user_groups_tag(
        access_condition=access_condition,
        tag_id=tag_id,
        user_id=user_id,
    ).join(tags)


def _join_user_to_tags(*, access_condition, user_id: int):
    return user_to_groups.join(
        tags_access_rights,
        (user_to_groups.c.uid == user_id)
        & (user_to_groups.c.gid == tags_access_rights.c.group_id)
        & (access_condition),
    ).join(tags)


def get_tag_stmt(
    user_id: int,
    tag_id: int,
):
    return (
        sa.select(
            *_TAG_COLUMNS,
            # aggregation ensures MOST PERMISSIVE policy of access-rights
            sa.func.bool_or(tags_access_rights.c.read).label("read"),
            sa.func.bool_or(tags_access_rights.c.write).label("write"),
            sa.func.bool_or(tags_access_rights.c.delete).label("delete"),
        )
        .select_from(
            _join_user_to_given_tag(
                access_condition=tags_access_rights.c.read.is_(True),
                tag_id=tag_id,
                user_id=user_id,
            )
        )
        .group_by(tags.c.id)
    )


def list_tags_stmt(*, user_id: int):
    return (
        sa.select(
            *_TAG_COLUMNS,
            # aggregation ensures MOST PERMISSIVE policy of access-rights
            sa.func.bool_or(tags_access_rights.c.read).label("read"),
            sa.func.bool_or(tags_access_rights.c.write).label("write"),
            sa.func.bool_or(tags_access_rights.c.delete).label("delete"),
        )
        .select_from(
            _join_user_to_tags(
                access_condition=tags_access_rights.c.read.is_(True),
                user_id=user_id,
            )
        )
        .group_by(tags.c.id)  # makes it tag.id uniqueness
        .order_by(tags.c.priority.nulls_last())
        .order_by(tags.c.id)
    )


def create_tag_stmt(**values):
    return tags.insert().values(**values).returning(*_TAG_COLUMNS)


def count_groups_with_given_access_rights_stmt(
    *,
    user_id: int,
    tag_id: int,
    read: bool | None,
    write: bool | None,
    delete: bool | None,
):
    """
    How many groups (from this user_id) are given EXACTLY these access permissions
    """
    access = []
    if read is not None:
        access.append(tags_access_rights.c.read == read)
    if write is not None:
        access.append(tags_access_rights.c.write == write)
    if delete is not None:
        access.append(tags_access_rights.c.delete == delete)

    if not access:
        msg = "Undefined access"
        raise ValueError(msg)

    j = _join_user_groups_tag(
        access_condition=functools.reduce(sa.and_, access),
        user_id=user_id,
        tag_id=tag_id,
    )
    return sa.select(sa.func.count(user_to_groups.c.uid)).select_from(j)


def update_tag_stmt(*, user_id: int, tag_id: int, **updates):
    return (
        tags.update()
        .where(tags.c.id == tag_id)
        .where(
            (tags.c.id == tags_access_rights.c.tag_id)
            & (tags_access_rights.c.write.is_(True))
        )
        .where(
            (tags_access_rights.c.group_id == user_to_groups.c.gid)
            & (user_to_groups.c.uid == user_id)
        )
        .values(**updates)
        .returning(*_TAG_COLUMNS, *_ACCESS_RIGHTS_COLUMNS)
    )


def delete_tag_stmt(*, user_id: int, tag_id: int):
    return (
        tags.delete()
        .where(tags.c.id == tag_id)
        .where(
            (tags_access_rights.c.tag_id == tag_id)
            & (tags_access_rights.c.delete.is_(True))
        )
        .where(
            (tags_access_rights.c.group_id == user_to_groups.c.gid)
            & (user_to_groups.c.uid == user_id)
        )
        .returning(tags_access_rights.c.delete)
    )


#
# ACCESS RIGHTS
#

_TAG_ACCESS_RIGHTS_COLS = [
    tags_access_rights.c.tag_id,
    tags_access_rights.c.group_id,
    *_ACCESS_RIGHTS_COLUMNS,
]


class TagAccessRightsDict(TypedDict):
    tag_id: int
    group_id: int
    # access rights
    read: bool
    write: bool
    delete: bool


def has_access_rights_stmt(
    *,
    tag_id: int,
    caller_user_id: int | None = None,
    caller_group_id: int | None = None,
    read: bool = False,
    write: bool = False,
    delete: bool = False,
):
    conditions = []

    # caller
    if caller_user_id is not None:
        group_condition = (
            tags_access_rights.c.group_id
            == sa.select(users.c.primary_gid)
            .where(users.c.id == caller_user_id)
            .scalar_subquery()
        )
    elif caller_group_id is not None:
        group_condition = tags_access_rights.c.group_id == caller_group_id
    else:
        msg = "Either caller_user_id or caller_group_id must be provided."
        raise ValueError(msg)

    conditions.append(group_condition)

    # access-right
    if read:
        conditions.append(tags_access_rights.c.read.is_(True))
    if write:
        conditions.append(tags_access_rights.c.write.is_(True))
    if delete:
        conditions.append(tags_access_rights.c.delete.is_(True))

    return sa.select(tags_access_rights.c.group_id).where(
        sa.and_(
            tags_access_rights.c.tag_id == tag_id,
            *conditions,
        )
    )


def list_tag_group_access_stmt(*, tag_id: int):
    return sa.select(*_TAG_ACCESS_RIGHTS_COLS).where(
        tags_access_rights.c.tag_id == tag_id
    )


def upsert_tags_access_rights_stmt(
    *,
    tag_id: int,
    group_id: int | None = None,
    user_id: int | None = None,
    read: bool,
    write: bool,
    delete: bool,
):
    assert not (user_id is None and group_id is None)  # nosec
    assert not (user_id is not None and group_id is not None)  # nosec

    target_group_id: int | ScalarSelect

    if user_id:
        assert not group_id  # nosec
        target_group_id = (
            sa.select(users.c.primary_gid)
            .where(users.c.id == user_id)
            .scalar_subquery()
        )
    else:
        assert group_id  # nosec
        target_group_id = group_id

    return (
        pg_insert(tags_access_rights)
        .values(
            tag_id=tag_id,
            group_id=target_group_id,
            read=read,
            write=write,
            delete=delete,
        )
        .on_conflict_do_update(
            index_elements=["tag_id", "group_id"],
            set_={"read": read, "write": write, "delete": delete},
        )
        .returning(*_TAG_ACCESS_RIGHTS_COLS)
    )


def delete_tag_access_rights_stmt(*, tag_id: int, group_id: int):
    return (
        sa.delete(tags_access_rights)
        .where(
            (tags_access_rights.c.tag_id == tag_id)
            & (tags_access_rights.c.group_id == group_id)
        )
        .returning(tags_access_rights.c.tag_id.is_not(None))
    )


#
# PROJECT TAGS
#


def get_tags_for_project_stmt(*, project_index: int):
    return sa.select(projects_tags.c.tag_id).where(
        projects_tags.c.project_id == project_index
    )


def add_tag_to_project_stmt(
    *, project_index: int, tag_id: int, project_uuid_for_rut: UUID
):
    return (
        pg_insert(projects_tags)
        .values(
            project_id=project_index,
            tag_id=tag_id,
            project_uuid_for_rut=f"{project_uuid_for_rut}",
        )
        .on_conflict_do_nothing()
    )


#
# SERVICE TAGS
#


def get_tags_for_services_stmt(*, key: str, version: str):
    return sa.select(services_tags.c.tag_id).where(
        (services_tags.c.service_key == key)
        & (services_tags.c.service_version == version)
    )


def add_tag_to_services_stmt(*, key: str, version: str, tag_id: int):
    return (
        pg_insert(services_tags)
        .values(
            service_key=key,
            service_version=version,
            tag_id=tag_id,
        )
        .on_conflict_do_nothing()
    )
