import argparse
import sys
from enum import IntEnum

from .core import server


class ExitCode(IntEnum):
    """
    @see https://www.gnu.org/prep/standards/standards.html
    """

    SUCCESS = 0
    GENERAL = 1  # catch-all (GNU: "very broad information")
    USAGE = 2  # argparse 자동 반환
    CONFIG_ERROR = 3  # 설정 파일 부재/무효 → 호출자가 수정 후 재시도
    IO_ERROR = 4  # I/O 실패 → 재시도 가능
    DEPENDENCY = 5  # 외부 서비스/프로시 없음 → 재시도 or 스킵
    # ── 커스텀: 3~63, 79~125 ──
    INTERRUPTED = 130
    # 추가할 때: "호출자가 다르게 처리해야 하는가?"를 먼저 질문


def main() -> int:
    try:
        return _run()
    except KeyboardInterrupt, InterruptedError:
        print("Aborted", file=sys.stderr)
        return ExitCode.INTERRUPTED
    except OSError as exc:
        print(f"IO: {exc}", file=sys.stderr)
        return ExitCode.IO_ERROR


def _run() -> int:
    parser = argparse.ArgumentParser(prog="local-dev-mcp")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Run server").set_defaults(_callback=server.base)

    args = parser.parse_args()
    handler: server.Callback = args._callback
    return handler(args)
