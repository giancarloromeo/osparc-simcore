from datetime import datetime
from typing import Any, TypeAlias
from uuid import UUID

from pydantic import BaseModel, Field, PositiveFloat, model_validator
from typing_extensions import Self

TaskRpcId: TypeAlias = UUID


class TaskRpcStatus(BaseModel):
    task_id: TaskRpcId
    task_progress: PositiveFloat = Field(..., ge=0.0, le=1.0)
    done: bool
    started: datetime
    stopped: datetime | None

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        is_done = self.done
        is_stopped = self.stopped is not None

        if is_done != is_stopped:
            msg = f"Inconsistent data: {self.done=}, {self.stopped=}"
            raise ValueError(msg)
        return self


class TaskRpcResult(BaseModel):
    result: Any | None
    error: Any | None


class TaskRpcGet(BaseModel):
    task_id: TaskRpcId
    task_name: str
