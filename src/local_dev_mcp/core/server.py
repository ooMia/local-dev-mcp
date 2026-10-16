import argparse
from collections.abc import Callable

type Callback = Callable[[argparse.Namespace], int]


class ServerArgs(argparse.Namespace):
    host: str = "localhost"
    port: int = 8080
    log_level: str = "info"
    config_path: str | None = None
    _callback: Callback | None = None


def base(_: argparse.Namespace) -> int:
    _echo()
    return 0


def _echo():
    print("Hello from local-dev-mcp!")
