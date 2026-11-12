# Worktrunk MCP 통합 — 선택적 실험 경로

참조: [Worktrunk 공식 저장소](https://github.com/max-sixty/worktrunk),
[Switch](https://worktrunk.dev/switch/), [Merge](https://worktrunk.dev/merge/),
[Remove](https://worktrunk.dev/remove/), [List](https://worktrunk.dev/list/).

[MCP Elicitation Capability 진단](elicitation-diagnostics.md)은 2026-07-28의 다중 왕복 지원 여부를 점검하며,
현재 `main` 자동 병합 승인으로 사용할 수 없다.

이 구현은 기존 Git Worktree/PR 도구를 **아직 대체하지 않는다**.
기존 API를 삭제하기 전에 실제 명령의 실패·안전 계약을 독립적으로 검증하기 위한 경로다.
실제 MCP 서버 사용에는 변경된 코드를 반영한 서버 재시작과 `wt` 실행 파일 설치가 필요하다.
Worktrunk 실행·JSON 처리·관리 경로는 `tools/git/worktrunk/`가 담당하며,
`tools/git/facades/worktrunk.py`는 MCP 입력과 실행기 연결만 담당한다.
기존 Git Native 유틸리티는 기능별 대체가 검증될 때까지 현재 경로를 보존한다.

## 공개 MCP 기능

| 도구 | 입력 | 결과 |
| --- | --- | --- |
| `create_project_worktrunk` | `branch`, `expected_branch`, `expected_head`, `expected_base_head`, `base_branch="main"` | 현재 checkout을 변경하지 않고 새 브랜치·Worktree 생성 |
| `prepare_project_worktrunk_main` | `expected_branch`, `expected_head`, `expected_main_head` | Main이 미등록 상태일 때 별도 `.lab/wt/main` 생성, 원본 Dirty 파일 보존 |
| `merge_project_worktrunk` | `branch`, `expected_source_head`, `expected_main_head`, `mode="squash"\|"ff"` | 품질 증거·main 승인 미구현 상태에서는 실행 거부 |
| `remove_project_worktrunk` | `branch`, `expected_branch_head`, `delete_branch=false` | Clean Worktree 정리, 명시적 선택 시 안전한 브랜치 삭제 |
| `get_project_worktrunk_merge_plan` | `evidence_id`, `branch`, `expected_source_head`, `expected_main_head` | 서버 로컬 검사 증거와 Clean main 검증, 승인 대기 상태의 read-only 계획 |

검토 계획의 `mode`는 `ff`로 명시하며 `plan_digest` 역시 동일한 모드를 포함한다.
이는 이후의 승인 계약에서 암묵적인 Squash로 작업 종류가 변경되는 것을 방지하기 위한 불변조건이다.

모두 `git`, `wt`, `git.worktree.write` 논리 권한이 필요하다.
`config.toml`의 허용 목록이 실행 파일 설치를 의미하지 않으며,
시작 시점의 도구 readiness가 실제 CLI 실행 성공을 보장하지 않는다.

## 안전 경계

- 브랜치 이름은 기존 프로젝트 기능 브랜치 검증을 재사용한다.
- 기능 Worktree 경로는 선택 Workspace 하위 `.lab/wt/<branch-sha256-prefix>`로 고정한다.
  Main 준비는 별도 `.lab/wt/main` 경로만 사용하며 기존 등록 Main/점유 경로를 거부한다.
  준비 전후 원본 Checkout 지문과 Main Ref·새 Worktree의 Git 포인터·Clean 상태를 검증한다.
  `wt switch --path` 플래그는 Worktrunk에서 **experimental**이므로 CLI 업그레이드 회귀가 필요하다.
- 현재 checkout의 원래 브랜치·HEAD·작업 트리 지문을 검증하며, 생성에 필요한 기본 Ref의 SHA를 확인한다.
- 기존 경로, 브랜치, 관리 외 Worktree는 채택하거나 덮어쓰지 않는다.
- 생성 전 Git ignore, 접근 정책, 보호 이름을 확인한다.
- 병합은 등록된 관리형 소스와 실제 Clean main Worktree, 정확한 SHA, source에 대한 main 조상 관계를 요구한다.
- 병합 시 `--no-rebase --no-remove --stage none --no-hooks`와 옵션별 `--no-squash`를 사용한다.
  결과 main Ref와 Git tree를 다시 확인한다.
- 제거는 `--foreground`, 비강제 옵션만 사용한다. Dirty/ignored 파일, 예상치 못한 HEAD, 관리 외 Worktree를 거부한다.
  MCP `remove_project_worktrunk`는 기존 Task Manager의 `exclusive_git_cleanup` 잠금을 재사용하여
  실행 중·정리 불확실한 Job과 Worktree 삭제를 직렬화한다. 독립 Git 프로세스까지 잠그지는 않는다.
- Worktrunk의 stdout JSON 형태는 명령마다 다르며, 프로세스 성공과 Git 상태 검증을 별도로 요구한다.
- 모든 외부 실행은 argv 기반이며 stdin을 닫고 stdout 크기·시간을 제한한다. 호출자 제공 쉘 문자열은 실행하지 않는다.
- 타임아웃 또는 결과 확인 실패 이후 동일한 쓰기 명령을 자동 재실행하지 않는다.

이 구현도 MCP 정책을 OS Sandbox로 바꾸지는 않는다. Worktrunk CLI·Git 프로세스는
실행 계정의 실제 파일 시스템 권한으로 동작한다. 다른 프로세스와의 원자적 잠금 및
명령 실행 중 Ref 이동까지 완전히 차단하지 않는다.

## 검증과 현재 제한

`tests/tools/git/test_worktrunk_cli.py`는 임시 Git 저장소에서 Worktrunk 실행 파일을 직접 호출한다.
`tests/tools/git/test_worktrunk_mcp.py`는 FastMCP Client를 통해 새 도구의 공개 계약을 검사한다.

- macOS에서 생성·Worktree 격리·이름 충돌·1개/3개 커밋 Squash·FF·정리·권한 거부를 검증한다.
- 테스트 호스트에 `wt`가 없으면 실제 CLI 테스트를 **skip**한다. skip을 PASS로 계산하지 않는다.
- Windows에서의 프로세스 실행, Worktrunk 설치, 경로 처리 및 OS별 테스트는 별도 검증이 필요하다.
- 기존 main 전용 `Source-PR` hooks 및 GitHub Rulesets가 활성화되어 있으면 실제 운영 저장소의
  로컬/원격 통합에 영향을 준다. 이 실험은 그 정책을 변경하지 않는다.
- 실제 사용자의 작업 저장소에는 검증 목적의 Worktrunk 통합·정리 명령을 실행하지 않는다.
- 실험용 `merge_project_worktrunk`는 아직 품질 Job 증거·명시적 main 승인 연결을 강제하지 않는다.
  이 경로를 공식 통합 게이트로 사용하거나 검증 없이 자동 호출해서는 안 된다.

기존 도구와 기능 중복을 제거하는 단계는 새 흐름이 기존 테스트를 만족하고 PR 강제 정책 변경을
안전하게 분리한 뒤 진행한다.
