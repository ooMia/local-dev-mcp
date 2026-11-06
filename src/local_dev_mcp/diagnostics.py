"""Safe CLI diagnostics; formatters and handlers own output, not error callers."""

import logging
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager

from .errors import CliError, ExitCode
from .readiness import PreparationIssue

_LOGGER = logging.getLogger("local_dev_mcp.diagnostics")


class _UTCFormatter(logging.Formatter):
    converter = time.gmtime


@contextmanager
def stderr_diagnostics() -> Generator[None]:
    # Bind the current stderr for each CLI invocation (including embedded calls).
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _UTCFormatter(
            "%(asctime)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    previous_level, previous_propagation = _LOGGER.level, _LOGGER.propagate
    _LOGGER.setLevel(logging.INFO)
    _LOGGER.propagate = False
    _LOGGER.addHandler(handler)
    try:
        yield
    finally:
        _LOGGER.removeHandler(handler)
        handler.close()
        _LOGGER.setLevel(previous_level)
        _LOGGER.propagate = previous_propagation


def report(error: CliError) -> None:
    level = logging.INFO if error.code == ExitCode.INTERRUPTED else logging.ERROR
    # Never pass an exception object, exc_info, or an exception chain to logging.
    _LOGGER.log(level, "%s", error.diagnostic())


def status(message: str) -> None:
    """Report application-authored lifecycle messages, never external log text."""
    _LOGGER.info("%s", message)


def warn_preparation(issues: tuple[PreparationIssue, ...]) -> None:
    for issue in dict.fromkeys(issues):
        _LOGGER.warning("%s", issue.diagnostic())
