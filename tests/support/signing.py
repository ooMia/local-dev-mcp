"""Isolated signing configuration shared by real MCP integration tests."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SigningSetup:
    program: Path
    home: Path
    fingerprint: str

    def dotenv(self) -> str:
        return (
            f"GIT_COMMIT_SIGN=true\nGIT_SIGNING_KEY={self.fingerprint}\n"
            f"GIT_GPG_PROGRAM='{self.program}'\nGIT_GPG_HOME='{self.home}'\n"
        )
