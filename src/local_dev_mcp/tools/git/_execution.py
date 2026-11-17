"""Policy-checked Git process execution, output limits and cancellation."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from fastmcp.exceptions import ToolError
from shellous import sh

from ._prepare import PreparedGitCommit, require_git_access
from ._signing import signing_options

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings

READ_TIMEOUT: Final = 10
WRITE_TIMEOUT: Final = 60
OUTPUT_LIMIT: Final = 1024 * 1024


def text_output(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ToolError(
            "Git output is not valid UTF-8; no lossy filename or patch conversion was performed."
        ) from error


@dataclass(frozen=True)
class GitResult:
    code: int
    output: bytes


@dataclass(frozen=True)
class GitExecution:
    settings: ServerSettings
    preparation: PreparedGitCommit | None = None

    @property
    def root(self) -> Path:
        return self.settings.policy.root

    async def run(
        self,
        *args: str,
        operation: str,
        write: bool = False,
        capture: bool = True,
        allow_failure: bool = False,
    ) -> GitResult:
        require_git_access(self.settings, self.preparation)
        options = ("-c", "maintenance.auto=false", "-c", "gc.auto=0") if write else ()
        if self.preparation is not None and self.preparation.signing is not None:
            options += signing_options(self.preparation.signing)
        command = (
            sh.result(self.settings.require_git(), "--literal-pathspecs", "-C", str(self.root), *options, *args)
            .set(
                inherit_env=False,
                env={
                    **dict(self.settings.environment),
                    **(self.preparation.environment() if self.preparation is not None else {}),
                },
                timeout=WRITE_TIMEOUT if write else READ_TIMEOUT,
            )
            .stdin(sh.DEVNULL)
            .stdout(sh.CAPTURE if capture else sh.DEVNULL)
            .stderr(sh.DEVNULL)
        )
        output = bytearray()
        exceeded = False
        uncertainty = " State may have changed; inspect status and HEAD before retrying." if write else ""
        try:
            async with command as process:
                if capture:
                    if process.stdout is None:
                        raise ToolError(f"{operation}: Git output stream is unavailable.")
                    while chunk := await process.stdout.read(65536):
                        if len(output) + len(chunk) > OUTPUT_LIMIT:
                            exceeded = True
                        if not exceeded:
                            output.extend(chunk)
            # shellous may suppress cancellation after reaping the process.
            # Propagate it so an enclosing tool deadline or caller still owns it.
            if process.cancelled:
                raise asyncio.CancelledError
            code = process.returncode
        except FileNotFoundError as error:
            raise ToolError("The git executable or execution directory is not available.") from error
        except TimeoutError as error:
            timeout = WRITE_TIMEOUT if write else READ_TIMEOUT
            raise ToolError(f"{operation} timed out after {timeout} seconds.{uncertainty}") from error
        except OSError as error:
            raise ToolError(f"{operation}: local process I/O failed.{uncertainty}") from error
        except RuntimeError as error:
            raise ToolError(f"{operation}: Git process termination could not be confirmed.{uncertainty}") from error
        if code is None:
            raise ToolError(f"{operation}: Git process termination could not be confirmed.{uncertainty}")
        if exceeded:
            raise ToolError(f"{operation}: output exceeded 1 MiB; no incomplete result was returned.")
        if code != 0 and not allow_failure:
            signing_guide = (
                " For signing, check `gpg --homedir <HOME> "
                "--with-subkey-fingerprint --list-secret-keys` and prepare key use outside MCP. "
                "Key listing does not establish an unlocked key. Settings changes require restart."
                if write and self.preparation is not None and self.preparation.signing is not None
                else ""
            )
            raise ToolError(
                f"{operation} failed (Git exit code {code}). Check paths, index state, commit identity and "
                f"repository-local hooks, signing or filters as applicable. "
                f"Raw process logs are not returned.{signing_guide}{uncertainty}"
            )
        return GitResult(code, bytes(output))
