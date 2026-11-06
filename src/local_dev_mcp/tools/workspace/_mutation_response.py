"""File creation and deletion outcomes with bounded submission receipts."""

from typing import Annotated, Literal

from pydantic import Field

from ._response import FileEdit

type RequestId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


class FileMutation(FileEdit):
    operation: Literal["create", "delete"]
    request_id: str
    replayed: bool = False
    created_directories: list[str] = Field(default_factory=list[str])
