"""Private Git signing/verification entry point with fixed noninteractive flags."""

import os
import sys
from pathlib import Path

# Fixed flags override the corresponding options in the selected home's config.
# GnuPG may start shared daemons; prompting and automatic key retrieval stay disabled.
GPG_OPTIONS: tuple[str, ...] = (
    "--batch",
    "--no-tty",
    "--pinentry-mode",
    "error",
    "--no-auto-key-retrieve",
    "--auto-key-locate",
    "clear",
    "--no-auto-key-import",
)


def _resolve_gpg_program() -> Path | None:
    """Validate and return the managed GPG executable path."""
    raw_program = os.environ.get("LOCAL_DEV_MCP_GPG_PROGRAM")
    gnupg_home = os.environ.get("GNUPGHOME")

    if not raw_program or not gnupg_home:
        return None

    program_path = Path(raw_program)
    if not program_path.is_absolute():
        return None

    # Verify it is an existing regular file (or symlink to one) with execute permissions
    if not program_path.is_file() or not os.access(program_path, os.X_OK):
        return None

    return program_path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    program = _resolve_gpg_program()

    if program is None:
        print(
            "gpg-wrapper: missing or invalid LOCAL_DEV_MCP_GPG_PROGRAM / GNUPGHOME",
            file=sys.stderr,
        )
        return 1

    program_str = str(program)
    try:
        # Process replacement is intentional: Git owns this signer process, and
        # the executable is a validated absolute path from the managed environment.
        os.execv(program_str, [program_str, *GPG_OPTIONS, *args])  # noqa: S606
    except OSError as exc:
        print(f"gpg-wrapper: failed to execute {program_str}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
