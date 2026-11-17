"""Safe file-tool failures; never include OS messages or file contents."""

from typing import Self

from fastmcp.exceptions import ToolError


class WorkspaceError(ToolError):
    # Access and input contracts.
    @classmethod
    def path(cls) -> Self:
        return cls("Use a nonempty workspace-relative path without NUL or workspace escapes.")

    @classmethod
    def protected(cls) -> Self:
        return cls("This path is protected. .git and .env files are unavailable except .env.example.")

    @classmethod
    def symlink(cls) -> Self:
        return cls("Symlink traversal and content access are not supported. Choose a regular workspace path.")

    @classmethod
    def access(cls) -> Self:
        return cls("Cannot access the requested workspace path. Check existence, type and local permissions.")

    @classmethod
    def arguments(cls, field: str, guide: str) -> Self:
        return cls(f"Invalid {field}. {guide}")

    # Reading, discovery and stored observations.
    @classmethod
    def text(cls) -> Self:
        return cls("Only UTF-8 regular text files without NUL are supported; no lossy conversion was performed.")

    @classmethod
    def size(cls) -> Self:
        return cls("The file exceeds the 8 MiB read limit. Choose a smaller text file.")

    @classmethod
    def line(cls) -> Self:
        return cls("A complete requested line exceeds the response limit. No partial line or read_ref was returned.")

    @classmethod
    def changed(cls) -> WorkspaceChanged:
        return WorkspaceChanged(
            "The file changed during reading. Discard this observation and read the required range again."
        )

    @classmethod
    def ignore(cls) -> Self:
        return cls("Cannot safely read a workspace .gitignore. Check its UTF-8 encoding, type and access permissions.")

    @classmethod
    def reference(cls) -> Self:
        return cls("The read_ref is unavailable for this server/workspace or has expired. Read the range again.")

    @classmethod
    def response(cls) -> Self:
        return cls("Workspace response metadata exceeds the output limit. Choose a shorter, narrower path.")

    @classmethod
    def budget(cls) -> Self:
        return cls("File reading exceeded its execution budget. Request again after checking local I/O.")


class WorkspaceChanged(WorkspaceError):
    """An access-safe, typed marker for changes detected while reading."""
