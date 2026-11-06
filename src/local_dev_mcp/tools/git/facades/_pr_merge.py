"""Selected-origin adapter for the shared GitHub merge boundary."""

from typing import TYPE_CHECKING, Annotated

from fastmcp.exceptions import ToolError
from pydantic import Field

from ...gh import pr_merge
from .._execution import GitExecution
from .._remote import Remote
from . import _repository
from .remote_pr import require_permission

if TYPE_CHECKING:
    from ....app.runtime import ServerSettings

type PRNumber = Annotated[int, Field(ge=1, description="Observed PR number in the selected GitHub origin.")]
type MergeDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="Reviewed merge plan SHA-256.")]


async def context(settings: ServerSettings, capability: str) -> tuple[str, pr_merge.GitHubRunner]:
    require_permission(settings, capability)
    await _repository.verify_root(GitExecution(settings))
    remote = Remote(settings)
    repository, _ = await remote.repo()

    async def call(*args: str, write: bool = False, input_text: str | None = None) -> str:
        try:
            return (await remote.gh(*args, write=write, input_text=input_text)).output
        except ToolError:
            raise pr_merge.MergeCommandError from None

    return repository, call
