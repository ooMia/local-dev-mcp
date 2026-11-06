"""Prepare a neutral workspace; tests opt in to commands and Git identity."""

from pathlib import Path
from typing import Protocol


class WorkspaceConfigFactory(Protocol):
    def __call__(self, commands: str = "[]", paths: str = '["."]') -> Path: ...


def configure(root: Path, commands: str = "[]", paths: str = '["."]') -> None:
    (root / "config.toml").write_text(f"[filesystem]\nallowed_paths={paths}\n[system]\nallowed_commands={commands}\n")
    (root / ".env").write_text("")
