# Fix 5 Failing Integration Tests

## Problem
The project owner (전권을 가진 개발자) encounters 5 failing integration tests in the local-dev-mcp MCP server project — 3 cases in `tests/app/test_cli.py::test_external_exit_is_normalized_and_raw_output_discarded` (exit code normalization/raw output discarding), 1 case in `tests/app/test_cli.py::test_connect_transports_missing_git_warning_once_and_continues` (warning count assertion), and 1 case in `tests/system/test_dag_workflow.py::test_stdio_workflow_obeys_dependencies_and_overlaps_independent_reads` (DAG dependency/ordering). Lint fixes were applied to improve code quality, but these tests now fail. Previously all tests passed on macOS. Leaving this unresolved blocks development freedom in the current WSL environment while preserving lint-driven code quality.

## Evidence
- `uv run --locked pytest -n 4 -m 'integration and not packaging'` reproduces the 5 failing cases.
- On macOS, prior versions of the same tests passed.
- Invalid/meaningless tests may be deleted; otherwise each test must pass against product requirements.

## Users
- **Primary**: Project owner / developer working on local-dev-mcp in a WSL environment, who needs lint quality and passing integration tests simultaneously.
- **Not for**: End users of the MCP server (no user-facing behavior change expected); podman infrastructure consumers.

## Hypothesis
We believe **통합 테스트** will **사용자의 잠재적 오류를 사전 차단** for **LOCAL-DEV-MCP 사용자**. We'll know we're right when **통합 테스트 전체 성공**.

## Success Metrics

| Metric | Target | How measured |
|---|---|---|
| Passing integration tests | 100% | `uv run --locked pytest -n 4 -m 'integration and not packaging'` reports zero failures |
| Lint fixes preserved | 100% | `uv run --locked ruff check src tests conftest.py` reports zero violations |

## Scope

**MVP** — All 5 failing integration tests pass, or invalid/meaningless tests are deleted, without reverting lint fixes and without modifying podman infrastructure.

**Out of scope**
- 신규 기능 추가 — 이 변경의 목적은 테스트 수정/삭제이지 기능 추가가 아님
- macOS 전용 동작 유지 — 현재 WSL 환경에서의 통과를 목표로 함
- podman 인프라 수정 — podman 트리거 케이스는 스킵되더라도 무시하기로 함

## Delivery Milestones

<!-- Business outcomes, not engineering tasks. /plan turns each into a plan. -->

| # | Milestone | Outcome | Status | Plan |
|---|---|---|---|---|
| 1 | Diagnose root cause of 5 failing tests | Identified cause per test case | pending | — |
| 2 | Fix or delete failing tests | All integration tests pass | pending | — |
| 3 | Verify lint + tests together | Ruff clean and pytest green | pending | — |

## Open Questions
- [ ] 문제 해결을 위해 의존성 목록이 수정되어야만 하는 경우

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| 테스트가 제품 요구사항과 무의미하게 연결됨 | Medium | 테스트 삭제 필요성 발생 | 무효 테스트 식별 후 삭제 |
| WSL 환경 특이성으로 macOS 통과와 다름 | Medium | 환경별 분기 필요 | WSL에서 재현 및 수정 |
| Lint 수정 reverted 위험 | Medium | 코드 품질 저하 | lint fixes 보존 조건 명시 |

---
*Status: DRAFT — requirements only. Implementation planning pending via /plan.*