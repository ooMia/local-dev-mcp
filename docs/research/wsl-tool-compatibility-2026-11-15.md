# WSL2 공개 MCP 도구 호환성 감사 (2026-11-15)

## 검증 범위

목표는 WSL2 Ubuntu에서 **현재 공개 도구 71개**의 정상·거부·복구·외부 연동 경로를 실제 호출하여 검증하는 것이다. macOS startup 준비 상태와 GitHub Windows-native CI를 WSL 실행 증거로 사용하지 않는다.

새 진단 모듈(local_dev_mcp.quality.platform_audit)은 **읽기 전용**이다. 등록된 71개 도구를 11개 가족으로 고정하여 누락·중복을 판정하고, 바이너리 탐색 및 11개 읽기 경로만 실제 stdio MCP로 호출한다. 도구별 functional=NOT_RUN은 전체 기능 미검증을 뜻하며 catalog/readiness/PASS를 전수 기능 성공으로 확대하지 않는다. 전체 기능 검증은 기존 tests/mcp, tests/system, tests/tools 및 WSL 실행 증거가 담당한다.

## WSL 진단 및 테스트 실행

WSL 내부 셸에서 Linux 파일시스템에 독립적으로 clone한 checkout을 사용한다. 사용자의 현재 Git 브랜치, 작업 트리, GPG 홈, 실제 GitHub 원격에 테스트 쓰기를 수행하지 않는다.

    uname -a
    cat /etc/os-release
    for bin in uv git gh gpg gpgconf wt trail tunnel-client; do command -v "$bin" || printf 'MISSING %s\n' "$bin"; done
    uv python find 3.15
    uv sync --locked

    # WSL 서버용 config.toml과 .env가 준비된 프로젝트 루트에서 실행
    uv run --locked python -m local_dev_mcp.quality.platform_audit --workspace "$PWD" --require-wsl > wsl-preflight.json

    uv run --locked pytest -n 0 -m unit tests
    uv run --locked pytest -n 0 -m 'integration and not packaging' tests
    uv run --locked pytest -n 0 -m packaging tests

시스템 바이너리나 설정이 아직 준비되지 않았다면 --catalog-only로 서버 기동 없이 목록 정합성과 바이너리 위치만 확인할 수 있다. catalog-only 모드는 tools/list, readiness, 실제 도구 동작을 검증하지 않는다. 누락된 의존성은 BLOCKED로 기록하고, 자동 설치·비밀 설정 수정은 수행하지 않는다.

## 수용 기준과 정책

- tools/list 결과와 고정 카탈로그의 도구 이름·순서를 비교한다. 공개 도구 증감 시 audit family와 검증 소유자를 함께 갱신한다.
- get_tool_readiness는 시작 snapshot의 준비 여부만 판정한다. 바이너리 탐색 성공은 실행·호환성 증거가 아니다.
- 파일 쓰기·삭제, Git commit/cherry-pick, Worktree 생성/정리, Release 준비는 독립 임시 workspace와 bare remote에서 수행한다. 실행 전후 HEAD/Index/Working Tree 및 원문을 검증한다.
- 외부 GitHub 쓰기는 전용 테스트 원격과 승인된 권한을 요구한다. Elicitation Form/MRTR은 클라이언트가 지원을 선언할 때만 시험한다.
- WSL Linux 파일시스템을 주 기준으로 사용하고 /mnt/c 및 Windows interop 바이너리는 보조 검증으로 분리한다. POSIX flock, GPG Agent, Task Manager 취소, Trail 잠금, Worktrunk 등록 경로를 중점 확인한다.
- PASS에는 테스트 명령·결과·Git/파일 부작용 증거를 붙인다. FAIL, BLOCKED, SKIP, NOT_RUN은 서로 별개로 관리한다.

## 이번 작업의 실제 증거

- macOS self-local-dev-mcp: 등록 도구 71개 모두 startup ready. tests/system/test_stdio.py **11 passed, 5 warnings** (Python 3.15, 2026-11-15).
- WSL 내부 바이너리·stdio·전체 회귀는 아직 NOT_RUN. 현재 연결은 macOS 환경이며 WSL 실행 결과를 추정하지 않는다.
- WSL 실제 실행 환경, 로그, 차단 원인, 수정 SHA를 후속 증거로 기록한다.
