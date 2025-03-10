import logging

import arrow
from aiohttp import web
from models_library.folders import FolderID
from models_library.products import ProductName
from models_library.projects import ProjectID
from models_library.users import UserID
from models_library.workspaces import WorkspaceID, WorkspaceUpdates
from simcore_postgres_database.utils_repos import transaction_context

from ..db.plugin import get_asyncpg_engine
from ..folders._trash_service import trash_folder, untrash_folder
from ..projects._trash_service import trash_project, untrash_project
from . import _workspaces_repository, _workspaces_service

_logger = logging.getLogger(__name__)


async def _check_exists_and_access(
    app: web.Application,
    *,
    product_name: ProductName,
    user_id: UserID,
    workspace_id: WorkspaceID,
):
    await _workspaces_service.check_user_workspace_access(
        app=app,
        user_id=user_id,
        workspace_id=workspace_id,
        product_name=product_name,
        permission="delete",
    )


async def trash_workspace(
    app: web.Application,
    *,
    product_name: ProductName,
    user_id: UserID,
    workspace_id: WorkspaceID,
    force_stop_first: bool,
):
    await _check_exists_and_access(
        app, product_name=product_name, user_id=user_id, workspace_id=workspace_id
    )

    trashed_at = arrow.utcnow().datetime

    async with transaction_context(get_asyncpg_engine(app)) as connection:
        # EXPLICIT trash
        await _workspaces_repository.update_workspace(
            app,
            connection,
            product_name=product_name,
            workspace_id=workspace_id,
            updates=WorkspaceUpdates(trashed=trashed_at, trashed_by=user_id),
        )

        # IMPLICIT trash
        child_folders: list[FolderID] = [
            # NOTE: follows up with https://github.com/ITISFoundation/osparc-simcore/issues/7034
        ]

        for folder_id in child_folders:
            await trash_folder(
                app,
                product_name=product_name,
                user_id=user_id,
                folder_id=folder_id,
                force_stop_first=force_stop_first,
            )

        child_projects: list[ProjectID] = [
            # NOTE: follows up with https://github.com/ITISFoundation/osparc-simcore/issues/7034
        ]

        for project_id in child_projects:
            await trash_project(
                app,
                product_name=product_name,
                user_id=user_id,
                project_id=project_id,
                force_stop_first=force_stop_first,
                explicit=False,
            )


async def untrash_workspace(
    app: web.Application,
    *,
    product_name: ProductName,
    user_id: UserID,
    workspace_id: WorkspaceID,
):
    await _check_exists_and_access(
        app, product_name=product_name, user_id=user_id, workspace_id=workspace_id
    )

    async with transaction_context(get_asyncpg_engine(app)) as connection:
        # EXPLICIT UNtrash
        await _workspaces_repository.update_workspace(
            app,
            connection,
            product_name=product_name,
            workspace_id=workspace_id,
            updates=WorkspaceUpdates(trashed=None, trashed_by=None),
        )

        child_folders: list[FolderID] = [
            # NOTE: follows up with https://github.com/ITISFoundation/osparc-simcore/issues/7034
        ]

        for folder_id in child_folders:
            await untrash_folder(
                app,
                product_name=product_name,
                user_id=user_id,
                folder_id=folder_id,
            )

        child_projects: list[ProjectID] = [
            # NOTE: follows up with https://github.com/ITISFoundation/osparc-simcore/issues/7034
        ]

        for project_id in child_projects:
            await untrash_project(
                app, product_name=product_name, user_id=user_id, project_id=project_id
            )
