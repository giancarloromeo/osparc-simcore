import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from pydantic import parse_obj_as
from pydantic.errors import PydanticErrorMixin
from sqlalchemy.ext.asyncio import AsyncConnection

from .models.projects import projects
from .utils_repos import transaction_context


class DBBaseProjectError(PydanticErrorMixin, Exception):
    msg_template: str = "Project utils unexpected error"


class DBProjectNotFoundError(DBBaseProjectError):
    msg_template: str = "Project project_uuid={project_uuid!r} not found"


class ProjectsRepo:
    def __init__(self, engine):
        self.engine = engine

    async def get_project_last_change_date(
        self,
        project_uuid: uuid.UUID,
        *,
        connection: AsyncConnection | None = None,
    ) -> datetime:
        async with transaction_context(self.engine, connection) as conn:
            get_stmt = sa.select(projects.c.last_change_date).where(
                projects.c.uuid == f"{project_uuid}"
            )

            result = await conn.execute(get_stmt)
            row = result.first()
            if row is None:
                raise DBProjectNotFoundError(project_uuid=project_uuid)
            date = parse_obj_as(datetime, row[0])
            return date.replace(tzinfo=timezone.utc)
