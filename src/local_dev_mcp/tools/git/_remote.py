"""Bounded authenticated git/gh adapter for explicitly scoped remote workflows."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from fastmcp.exceptions import ToolError
from shellous import sh

from ...auth_environment import account_locations
from .. import executable
from ..workflow import developer_environment
from ._execution import GitExecution
from ._names import branch_name_allowed
from .commands import status

if TYPE_CHECKING:
    from ...app.runtime import ServerSettings

from .facades import _repository

_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_TYPES = ("feat", "fix", "refactor", "docs", "test", "build", "ci", "chore")
_TITLE = re.compile(r"(?:feat|fix|refactor|docs|test|build|ci|chore)(?:\([a-z0-9-]+\))?!?: [^\r\n]{1,90}\Z")


def valid_sha(value: object) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ToolError("A full 40/64-character commit SHA is required.")
    return value


def feature_name(branch: str) -> str:
    if not branch_name_allowed(branch) or not branch.startswith(tuple(t + "/" for t in (*_TYPES, "release"))):
        raise ToolError("Only allowed topic or canonical release branches can be published.")
    return branch


def title(value: str) -> str:
    if _TITLE.fullmatch(value) is None:
        raise ToolError("PR title must follow type(scope): concise change with no newline.")
    return value


def env() -> dict[str, str]:
    # Remote operations need the user's already configured gh/git credentials.
    # The normal local Git executor deliberately does not inherit these.
    inherited = developer_environment()
    # Remove relative auth configuration locations before applying approved absolute paths.
    # In particular, a relative XDG_STATE_HOME makes gh create files in the worktree.
    for key in (
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "GH_CONFIG_DIR",
    ):
        inherited.pop(key, None)
    return {
        **inherited,
        **account_locations(),
        **({"SSH_AUTH_SOCK": os.environ["SSH_AUTH_SOCK"]} if "SSH_AUTH_SOCK" in os.environ else {}),
        "GIT_TERMINAL_PROMPT": "0",
        "GH_PROMPT_DISABLED": "1",
        "GCM_INTERACTIVE": "never",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


_REMOTE_GIT_COMMANDS = frozenset({"ls-remote", "fetch", "push"})


def scoped_gh_credential_options(binary: Path) -> tuple[str, ...]:
    """Use authenticated gh only for this Git invocation, without mutating gitconfig.

    A scoped empty helper clears inherited helpers for github.com, so a stale
    global credential helper cannot override the authenticated gh account.
    The gh executable path is quoted for Git's credential-helper shell.
    """
    helper = "!" + shlex.quote(str(binary)) + " auth git-credential"
    return (
        "-c",
        "credential.https://github.com.helper=",
        "-c",
        "credential.https://github.com.helper=" + helper,
    )


@dataclass(frozen=True)
class Result:
    code: int
    output: str


@dataclass(frozen=True)
class Remote:
    settings: ServerSettings

    @property
    def root(self) -> Path:
        return self.settings.policy.root

    async def run(
        self, binary: str, *args: str, write: bool = False, input_text: str | None = None, allow_failure: bool = False
    ) -> Result:
        if binary not in ("git", "gh"):
            raise ToolError("Unregistered remote executable.")
        network_git = binary == "git" and bool(args) and args[0] in _REMOTE_GIT_COMMANDS
        try:
            self.settings.policy.require_project_access()
            self.settings.policy.require_command(binary)
            if network_git:
                self.settings.policy.require_command("gh")
        except PermissionError as error:
            raise ToolError(str(error)) from None
        executable_path = self.settings.require_git() if binary == "git" else executable("gh")
        if binary == "git":
            options = scoped_gh_credential_options(executable("gh")) if network_git else ()
            argv = ("--literal-pathspecs", "-C", str(self.root), *options, *args)
        else:
            argv = args
        command = (
            sh.result(executable_path, *argv)
            .set(cwd=self.root, inherit_env=False, env=env(), timeout=90 if write else 25)
            .stdin(input_text.encode("utf-8") if input_text is not None else sh.DEVNULL)
            .stdout(sh.CAPTURE)
            .stderr(sh.DEVNULL)
        )
        output = bytearray()
        exceeded = False
        try:
            async with command as process:
                if process.stdout is None:
                    raise ToolError("Remote process output was unavailable.")
                while chunk := await process.stdout.read(16384):
                    if len(output) + len(chunk) > 65536:
                        exceeded = True
                    if not exceeded:
                        output.extend(chunk)
            if process.cancelled:
                raise asyncio.CancelledError
            code = process.returncode
        except OSError, RuntimeError, TimeoutError:
            raise ToolError(
                f"{binary} remote operation did not finish. Reinspect remote refs before retrying."
            ) from None
        if exceeded or code is None:
            raise ToolError("Remote process output or termination was incomplete.")
        if code != 0 and not allow_failure:
            raise ToolError(
                f"{binary} remote operation exited {code}. Check host authentication, "
                "branch rules and ref state before retrying; sensitive command diagnostics are suppressed."
            )
        try:
            return Result(code, output.decode("utf-8"))
        except UnicodeDecodeError:
            raise ToolError("Remote command output was not UTF-8.") from None

    async def git(self, *args: str, write: bool = False, allow_failure: bool = False) -> Result:
        return await self.run("git", *args, write=write, allow_failure=allow_failure)

    async def gh(self, *args: str, write: bool = False, input_text: str | None = None) -> Result:
        return await self.run("gh", *args, write=write, input_text=input_text)

    async def repo(self) -> tuple[str, str]:
        url = (await self.git("remote", "get-url", "origin")).output.strip()
        repo = ""
        if url.startswith("git@github.com:"):
            repo = url.removeprefix("git@github.com:")
        elif url.startswith("ssh://git@github.com/"):
            repo = url.removeprefix("ssh://git@github.com/")
        else:
            parsed = urlparse(url)
            if parsed.scheme == "https" and parsed.hostname == "github.com":
                if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                    raise ToolError("Origin URL contains unsupported credentials or options.")
                repo = parsed.path.lstrip("/")
        repo = repo.removesuffix(".git").strip("/")
        if _REPO.fullmatch(repo) is None:
            raise ToolError("Origin must refer to exactly one github.com owner/repository.")
        selected: object = json.loads((await self.gh("repo", "view", "--json", "url")).output)
        observed: object = cast(dict[str, object], selected).get("url") if isinstance(selected, dict) else None
        if not isinstance(observed, str) or observed.lower() != ("https://github.com/" + repo).lower():
            raise ToolError("gh repository context differs from the Git origin; refusing remote operations.")
        return repo, "https://github.com/" + repo

    async def main_sha(self, name: str) -> str:
        response: object = json.loads((await self.gh("api", f"repos/{name}/git/ref/heads/main")).output)
        obj: object = cast(dict[str, object], response).get("object") if isinstance(response, dict) else None
        return valid_sha(cast(dict[str, object], obj).get("sha") if isinstance(obj, dict) else None)

    async def local_sha(self, ref: str) -> str | None:
        result = await self.git("rev-parse", "--verify", "--quiet", ref + "^{commit}", allow_failure=True)
        if result.code == 1:
            return None
        if result.code != 0:
            raise ToolError("Unable to resolve a local Git ref.")
        return valid_sha(result.output.strip())

    async def branch(self, name: str) -> str | None:
        result = await self.git("ls-remote", "--heads", "origin", "refs/heads/" + name)
        data = result.output.strip()
        if not data:
            return None
        fields = data.split("\t")
        if len(fields) != 2 or fields[1] != "refs/heads/" + name:
            raise ToolError("Unexpected remote ref response.")
        return valid_sha(fields[0])

    async def snapshot(self) -> tuple[str, str, str]:
        execution = GitExecution(self.settings)
        await _repository.verify_root(execution)
        branch, head, entries = await status.read_porcelain_v2(execution)
        if branch is None or head is None or entries:
            raise ToolError("A clean checked-out branch with no untracked files is required.")
        return branch, head, head

    async def ancestor(self, base: str, head: str) -> bool:
        result = await self.git("merge-base", "--is-ancestor", base, head, allow_failure=True)
        if result.code not in (0, 1):
            raise ToolError("Cannot verify remote ancestry.")
        return result.code == 0
