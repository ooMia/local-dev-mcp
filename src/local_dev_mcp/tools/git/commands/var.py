"""Check the prepared author and committer without guessing identities."""

from fastmcp.exceptions import ToolError

from .._execution import GitExecution


async def require_commit_identity(execution: GitExecution) -> None:
    for variable in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        result = await execution.run(
            "-c",
            "user.useConfigOnly=true",
            "var",
            variable,
            operation="Check prepared commit identity",
            allow_failure=True,
        )
        if result.code != 0:
            raise ToolError(
                "Git could not use the prepared commit identity. "
                "Check GIT_USER_NAME and GIT_USER_EMAIL in the project .env; no commit was attempted."
            )
