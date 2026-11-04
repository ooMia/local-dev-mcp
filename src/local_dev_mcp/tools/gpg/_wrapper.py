"""Private Git signing/verification entry point with fixed noninteractive flags."""

import os
import sys

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


def main() -> int:
    program = os.environ.get("LOCAL_DEV_MCP_GPG_PROGRAM")
    if not program or not os.path.isabs(program) or not os.environ.get("GNUPGHOME"):
        return 1
    try:
        # Replace this process; Git still manages one direct signer process.
        os.execv(program, [program, *GPG_OPTIONS, *sys.argv[1:]])
    except OSError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
