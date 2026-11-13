# ADR 0014: Worktrunk Lifecycle Hooks의 호출별 Opt-in

- 상태: Accepted
- 결정일: 2026-11-13
- 범위: `create_project_worktrunk`, `remove_project_worktrunk`에서의 Worktrunk Lifecycle Hooks
- 보완: [ADR 0013](0013-worktrunk-state-based-removal.md)의 삭제 판단 위임과 별개로, 외부 Hooks 실행의 결정 권한을 분리한다.

## 결정

Worktree 생명주기의 생성·제거는 Worktrunk에 위임하지만,
Worktrunk의 사용자·프로젝트 설정에 등록된 외부 Hooks를 **기본적으로 실행하지 않는다**.
각 공개 MCP 호출에서 `run_hooks=true`를 명시한 경우에 한해 Worktrunk Hooks를 활성화한다.
기본 호출은 `--no-hooks`이며, opt-in 호출은 비대화형 프로세스에서 별도 승인 프롬프트가
필요하지 않도록 `--yes`를 전달한다.

Hooks는 Worktrunk가 구성·실행하며, LOCAL-DEV-MCP가 명령 내용이나 부작용을 검증하지 않는다.
따라서 `run_hooks=true`는 설정된 Hooks의 외부 명령 실행도 허용하는 선택이다.
MCP의 `allowed_commands`와 `allowed_paths`를 Hooks 프로세스의 OS Sandbox로 간주하지 않는다.
기존 `main` 병합 승인이나 `--force` 삭제 허용으로 해석하지 않는다.

## 영향 및 검증

기본값은 기존 동작과 호환된다. opt-in은 생성·삭제 호출 각각에만 적용되며 영구 설정을 변경하지 않는다.
호출 경계에서는 true/false에 따라 전달되는 CLI 플래그와 MCP 입력 스키마를 회귀 검증한다.
실제 사용자 Hooks가 실행되는 테스트는 수행하지 않으며, 외부 설정의 동작 보장은 별도 통제된
환경의 검증 대상이다. 사용자 계약은 [Worktrunk 계약](../tools/git/worktrunk.md)에 둔다.
