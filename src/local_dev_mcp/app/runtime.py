"""Server-local immutable settings; feature preparation failures remain scoped."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path

from fastmcp.exceptions import ToolError

from ..errors import ConfigurationError, DependencyError, LocalIOError, PolicyError
from ..readiness import PreparationIssue, ToolReadiness
from ..redaction import Redactor
from ..settings import Policy, read_dotenv
from ..tools.git import PreparedGitCommit, command_environment, commit_configuration, git_configuration
from ..tools.indexing import IndexManager
from ..tools.indexing._worktree_service import WorktreeIndexService
from ..tools.tasks import TaskManager
from ..tools.workspace import WorkspaceFiles
from ._task_environment import public_environment_names, task_environment


@dataclass(frozen=True)
class ServerSettings:
    policy: Policy
    git: Path | PreparationIssue
    commit: PreparedGitCommit | PreparationIssue = field(repr=False)
    environment: tuple[tuple[str, str], ...] = field(repr=False)
    commit_required_commands: tuple[str, ...] = ("git",)
    snapshot_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    readiness: tuple[ToolReadiness, ...] = ()
    task_environment: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    redactor: Redactor = field(default_factory=lambda: Redactor.from_values({}), repr=False)
    trail_executable: str | None = field(default=None, repr=False)
    trail_refresh_on_write: bool = False

    @cached_property
    def index(self) -> IndexManager:
        return IndexManager(
            self.policy,
            dict(self.environment),
            executable=self.trail_executable,
            refresh_on_write=self.trail_refresh_on_write,
        )

    @cached_property
    def worktree_indexes(self) -> WorktreeIndexService:
        return WorktreeIndexService(self)

    @cached_property
    def files(self) -> WorkspaceFiles:
        return WorkspaceFiles(self.policy, change_callback=self.index.changed)

    @cached_property
    def tasks(self) -> TaskManager:
        return TaskManager(self.policy, self.policy.jobs, dict(self.task_environment), self.redactor)

    @classmethod
    def load(cls, policy: Policy) -> ServerSettings:
        try:
            policy.require_project_access()
        except PermissionError:
            raise PolicyError.path_denied("project directory") from None
        values = read_dotenv(policy.root)
        try:
            git = git_configuration(policy)
        except (ConfigurationError, DependencyError, LocalIOError, PolicyError) as error:
            git = PreparationIssue.from_error(error)
        if isinstance(git, PreparationIssue):
            commit = git
        else:
            try:
                commit = commit_configuration(values, policy)
            except (ConfigurationError, DependencyError, LocalIOError, PolicyError) as error:
                commit = PreparationIssue.from_error(error)
        # Authorization requirements must remain available when preparation fails.
        commands = ("git", "gpg") if values.get("GIT_COMMIT_SIGN") == "true" else ("git",)
        return cls(
            policy,
            git,
            commit,
            tuple(command_environment().items()),
            commands,
            task_environment=tuple(task_environment(values).items()),
            redactor=Redactor.from_values(
                values, public_names=public_environment_names(values, policy.public_environment)
            ),
            trail_executable=values.get("TRAIL_EXECUTABLE"),
            trail_refresh_on_write=values.get("TRAIL_REFRESH_ON_WRITE") == "true",
        )

    def require_git(self) -> Path:
        if isinstance(self.git, PreparationIssue):
            raise ToolError(self.git.diagnostic())
        return self.git

    def require_commit(self) -> PreparedGitCommit:
        if isinstance(self.commit, PreparationIssue):
            raise ToolError(self.commit.diagnostic())
        return self.commit
