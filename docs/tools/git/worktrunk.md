# Worktrunk MCP 통합 — 선택적 실험 경로

참조: [Worktrunk 공식 저장소](https://github.com/max-sixty/worktrunk),
[Switch](https://worktrunk.dev/switch/), [Merge](https://worktrunk.dev/merge/),
[Remove](https://worktrunk.dev/remove/), [List](https://worktrunk.dev/list/).

[MCP Elicitation Capability 진단](elicitation-diagnostics.md)은 2026-07-28의 다중 왕복 지원 여부를 점검하며,
현재 `main` 자동 병합 승인으로 사용할 수 없다.

이 구현은 기존 Git Worktree/PR 도구를 **아직 대체하지 않는다**.
기존 API를 삭제하기 전에 실제 명령의 실패·안전 계약을 독립적으로 검증하기 위한 경로다.
실제 MCP 서버 사용에는 변경된 코드를 반영한 서버 재시작이 필요하다. `wt` 실행 파일은 생성·삭제 등 Worktrunk CLI 호출에 필요하지만, 상태 조회에는 필요하지 않다.
Worktrunk 실행·JSON 처리·관리 경로는 `tools/git/worktrunk/`가 담당하며,
`tools/git/facades/worktrunk.py`는 MCP 입력과 실행기 연결만 담당한다.
기존 Git Native 유틸리티는 기능별 대체가 검증될 때까지 현재 경로를 보존한다.

## 공개 MCP 기능

| 도구 | 입력 | 결과 |
| --- | --- | --- |
| `inspect_project_worktrunk` | `branch` | Git 등록 상태 기반 읽기 전용 조회. `state=absent/not_checked_out/ready/dirty/blocked`, `branch_head`, `head`, `clean`, `registered`, 허용된 `path`와 차단 `reason` 반환 |
| `list_project_worktrees` | `include_branches=true`, `include_remote=false` | Git 등록 Worktree와 로컬 브랜치 목록. 허용 경로만 상대 경로로 표시하고 차단 Worktree는 개수로만 집계. Worktrunk Schema 2와 Git SHA·경로가 일치할 때만 Ahead/Behind·충돌 상태 추가 |
| `get_project_worktree_review` | `branch`, `expected_head`, `include_patch=false`, `base_branch="main"` | Git 등록 Checkout의 커밋된 변경·Staged·Unstaged·Untracked 요약. 패치는 명시 선택이며 보호 경로는 제외. SHA·기준 브랜치 이동 시 거부 |
| `get_project_worktree_pick_state` | `branch`, `expected_branch_head`, `expected_branch`, `expected_head` | 원본 Dirty 변경 지문을 보존한 상태에서 대상 Worktree의 HEAD·index 지문·staged/unstaged 경로 관측. `wt` 실행 파일 불필요 |
| `cherry_pick_project_worktree_commit` | 앞의 네 값, `commit`, `expected_index_fingerprint`, `allow_staged=false` | Git 등록 대상에서 기존 Cherry-pick 충돌·보호 경로 검사를 재사용하여 `--no-commit` 적용. 원본 Dirty 변경 보존, 자동 커밋·reset 없음 |
| `commit_project_worktree_pick` | 앞의 네 값, `message`, `expected_index_fingerprint`, `expected_staged_paths` | 리뷰한 대상 인덱스 전체 경로·지문을 일치시킨 뒤 기존 서명/Hook 정책으로 Commit, 정확한 부모·변경 경로·Clean 상태 확인 |
| `plan_project_worktree_cleanup` | `min_age_days=1` (0–365) | `wt step prune --dry-run` 후보에 대해 Clean·Git 등록·허용 경로·로컬 `main` 조상 관계를 재확인한 읽기 전용 제안. 미지원 CLI는 `complete=false`; 삭제 없음 |
| `create_project_worktrunk` | `branch`, `expected_branch`, `expected_head`, `expected_base_head=null`, `expected_branch_head=null`, `base_branch="main"`, `run_hooks=false`, `worktree_path=null` | 새 브랜치 생성·기존 브랜치 체크아웃·등록된 Worktree 재사용. `action=created/checked_out/reused` |
| `remove_project_worktrunk` | `branch`, `expected_branch_head`, `delete_branch=null`, `run_hooks=false` | 허용된 Workspace 내 Git 등록 Worktree를 비강제 제거. `null`은 Worktrunk의 브랜치 정리 기본값, `false`는 보존, `true`는 안전한 삭제 시도 |
| `get_project_worktrunk_merge_plan` | `evidence_id`, `branch`, `expected_source_head`, `expected_main_head` | 서버 로컬 검사 증거·Main Ref·Fast-forward 관계 검증, 별도 Main Checkout 없는 읽기 전용 제안 |


### 격리된 Worktree 커밋 이식

1. `get_project_worktree_pick_state`로 **Git 등록된 대상**의 현재 HEAD, 인덱스 SHA-256 지문 및 변경 목록을 확인한다. `expected_branch`·`expected_head`는 **원본 controller**, `branch`·`expected_branch_head`는 **대상 Worktree**다.
2. `cherry_pick_project_worktree_commit`으로 단일 비병합 커밋을 `--no-commit`으로 적용한다. 시작은 빈 index가 원칙이며 후속 `allow_staged=true`는 관측된 인덱스 지문에만 허용된다. 사용자 파일/ignore 충돌·보호 경로·중간 Git 작업 상태를 기존 Cherry-pick 안전 계약으로 거부한다.
3. `get_project_worktree_review(include_patch=true)`의 `patches.staged`와 새 Pick State로 실제 staged 경로를 검토한다. 이후 `commit_project_worktree_pick`에 정확한 `expected_staged_paths`와 지문을 전달하여 한 단위의 변경을 커밋한다. 대상에 unstaged 또는 untracked 파일이 있으면 커밋을 거부한다.
4. 커밋의 새로운 HEAD를 다시 관측한다. 다음 Source Commit은 새로운 대상 HEAD를 사용한다. PR 게시와 `main` 병합은 기존 원격 PR·CI 정책을 별도 적용한다.

세 도구 모두 원본 controller의 Dirty 경로 상태·파일 내용 지문을 작업 전후 검사하지만, 별도 OS Git 프로세스와 원자적 락을 공유하지는 않는다. 원본 Dirty 경로 256개·64 MiB 상한, 접근 정책, 등록된 Worktree와 공통 Git 저장소 검증을 유지한다. 쓰기에는 `git`·`wt`·`git.worktree.write` 논리 권한이 필요하며 `wt` CLI 실행을 요구하지 않는다. 커밋은 별도 Git identity/서명 설정도 필요하다. 충돌·훅 오류·통신 중단 이후에는 `HEAD`·index·status를 관측하고 재시도·abort·reset을 자동으로 수행하지 않는다. 수정 코드 반영을 위해 MCP 재시작이 필요하다.

평가 작성·커밋도 기존 `prepare_project_worktree_assessment` 및 `commit_project_worktree_assessment`에 동일한 `backend="worktrunk"`를 지정하여 함께 사용한다. Native Receipt 없이도 원본 Clean·평가 Digest·정확한 단일 파일 커밋·서명 검증을 유지한다.

원격 게시도 기존 `push_project_branch`와 `create_project_branch_draft_pr`의 `backend="worktrunk"`를 명시하여 수행한다. 실제 Git 등록 Worktree를 사용하며, 대상 Worktree Clean·비강제 Push·기준 Ref 및 중복 Draft PR 검사는 유지한다. 원본은 기본적으로 Clean을 요구한다. **Worktrunk에서만** `allow_dirty_controller=true`를 명시하면 원본의 Dirty 경로·index·파일 내용 SHA-256 지문을 작업 전후 재확인해 보존한다. 다른 backend에서 이 옵션은 거부하며 기존 Native 계약을 완화하지 않는다. 원본 Dirty 경로 256개·일반 파일 64 MiB 상한 및 외부 프로세스의 비원자적 변경 한계는 기존 Worktree snapshot 계약과 동일하다. 원격 쓰기가 성공한 뒤 원본 검증에 실패할 수도 있으므로 결과가 불확실하면 원격 Ref/PR을 읽기 전용으로 재조회하고 자동 재시도하지 않는다. `main` 승인·병합은 포함하지 않는다.

원격 읽기 전용 검사는 기존 `inspect_project_branch_remote(backend="worktrunk")`를 사용한다. 공개 입력·권한과 기본 `native` 경로는 [Worktree 계약](worktrees.md)을 따른다.

검토 계획의 `mode`는 `ff`로 명시하며 `plan_digest` 역시 동일한 모드를 포함한다.
이는 외부 실행 경로에서 암묵적인 Squash로 작업 종류가 변경되는 것을 방지하기 위한 검토 정보다.

`list_project_worktrees`도 **Git 읽기 권한만으로 호출**할 수 있다. `wt` 권한 또는 실행 파일이 없으면 Git 기준의 등록·HEAD·Dirty·로컬 브랜치만 반환하며 Ahead/Behind 등 Worktrunk 전용 정보는 `null`, `enrichment=not_available`로 남긴다. `wt list --branches --no-progressive --format=json`의 Schema 2를 사용할 때는 Git과 경로·SHA가 모두 일치해야 추가 정보를 확정한다. 불일치·CLI 실패는 `complete=false`와 원인으로 알리고, 일부 값만 신뢰하는 일은 없다. 조회는 최대 128개 행이며 `include_remote=true`는 원격 읽기 승인/노출 정책이 확립되지 않아 명시적으로 거부한다. 차단 Worktree의 경로·브랜치명은 표시하지 않고 `blocked_count`만 반환한다. 이 도구는 CI나 Agent 상태를 직접 확정하지 않는다.

`get_project_worktree_review`는 기존 Git Compare/Diff의 보호 경로 필터와 1 MiB Git 출력 상한을 재사용한다. 기본은 커밋된 변경 목록·Staged·Unstaged·Untracked 개수만 반환한다. `include_patch=true`인 경우 256 KiB 이하의 커밋·Staged·Unstaged 패치를 제공하지만 Untracked 파일 본문은 포함하지 않는다. 보호 경로가 누락되면 `complete=false`로 보고한다. 원본 및 대상 브랜치의 HEAD 이동은 거부하며, 진행 중 독립 프로세스의 파일 변경까지 원자적으로 차단하지는 못한다.

`plan_project_worktree_cleanup`은 `wt step prune --dry-run` 결과의 브랜치·절대 경로를 Git 등록 Checkout의 브랜치·경로와 정확히 대조한다. CLI 후보의 경로가 다르면 후보를 제외하고, 필수 JSON 필드가 누락되거나 브랜치·경로가 중복되면 `complete=false`로 실패 폐쇄한다. 조상 관계 및 Clean 상태는 추가 검증하며, 이 계획은 실제 삭제 권한이나 실행 승인으로 사용하지 않는다. 후보 상태를 재조회한 뒤 `main` HEAD를 다시 확인하므로 검증 중 변경은 거부한다. 단, 독립 Git 프로세스의 마지막 조회 이후 변경까지 원자적으로 차단하지는 못하며, 삭제 실행 시 별도 검증이 필요하다.

`inspect_project_worktrunk`는 **`git` 읽기 권한만** 요구한다. CLI 실행과 변경 작업에는 각 도구의 `wt`, `git.worktree.write` 등 선언된 권한이 별도로 필요하다.
`config.toml`의 허용 목록이 실행 파일 설치를 의미하지 않으며,
시작 시점의 도구 readiness가 실제 CLI 실행 성공을 보장하지 않는다.

## 안전 경계

- 브랜치 이름은 기존 프로젝트 기능 브랜치 검증을 재사용한다.
- **조회**는 Git Worktree 등록·현재 브랜치 Ref·실제 Checkout 루트 및 공통 Git 디렉터리·Git status를 대조한다. `absent`는 브랜치가 없고 등록도 없는 경우, `not_checked_out`는 브랜치만 존재하는 경우, `ready`/`dirty`는 허용된 Checkout의 현재 작업 상태다. `blocked`는 등록·경로·Ref가 일치하지 않거나 권한 밖인 상태로, 해당 경로를 공개하지 않는다. 조회 결과는 데이터 제거·원격 게시 승인이나 이전의 Native Receipt와 무관하다.
- `create_project_worktrunk.worktree_path`는 **선택 Workspace 기준 상대 POSIX 경로**다. 지정하면 Git ignore·보호 경로·권한 정책·symlink·점유 여부를 확인한 뒤 해당 위치에 생성/체크아웃한다. 이미 연결된 동일 브랜치는 실제 경로가 지정값과 일치할 때 재사용한다.
  생략하면 기존 `.lab/wt/<branch-sha256-prefix>` 위치를 사용해 기존 호출을 유지한다. 따라서 **고정 해시 경로는 더 이상 필수 조건이 아니다.**
  Worktrunk의 기본 `worktree-path` 템플릿은 저장소 밖의 형제 경로를 만들 수 있어 그대로 사용하지 않는다. 생성할 때마다 검증된 경로를 `wt switch --path`로 전달하며, 이 플래그는 현재 Worktrunk에서 **experimental**이므로 업그레이드 회귀가 필요하다.
  검토 계획은 원본과 Source Worktree, Main Git Ref만 확인하며 Main Checkout의 존재를 요구하지 않는다.
- 새 Worktree를 확보할 때 현재 Checkout의 **브랜치·HEAD**가 요청값과 일치하는지 전후 확인하고, 생성에 필요한 기본 Ref의 SHA를 확인한다. 원본의 Dirty 파일 내용이나 개수는 Worktree 확보의 선행 조건이 아니며 읽거나 해시하지 않는다. 따라서 다른 프로세스에 의한 원본 파일 내용 변화를 독립적으로 증명하지는 않는다.
- **생성·확보**는 [ADR 0015](../../adr/0015-worktrunk-state-based-preparation.md)를 따른다. 브랜치가 없으면 정확한 `expected_base_head`가 필요하다. 기존 브랜치에는 `expected_branch_head`를 선택적으로 지정할 수 있으며, 이미 체크아웃된 동일 저장소·브랜치는 생성 주체와 무관하게 재사용한다. 기존 경로나 다른 브랜치를 덮어쓰지 않는다.
- **삭제**는 [ADR 0013](../../adr/0013-worktrunk-state-based-removal.md)에 따라 생성 주체·Receipt·고정 관리 경로를 요구하지 않는다. Git에 등록된 단일 대상과 허용된 Workspace 경로, 현재 브랜치 HEAD를 확인한다.
- 새 Worktree를 생성하기 전에 Git ignore, 접근 정책, 보호 이름을 확인한다. 이미 등록된 Worktree는 허용된 경로·Git Worktree 등록·실제 Checkout 루트·공통 Git 디렉터리·정확한 브랜치를 확인해 변경사항을 보존한다. Native의 `.git` 포인터·backlink 해석이나 Receipt를 재사용의 조건으로 요구하지 않는다. **재사용 가능**과 후속 **Clean 상태 요구**는 서로 다른 조건이다.
- Worktrunk Backend의 Task 실행·품질 증거 검증은 고정 해시 경로가 아니라 현재 Git에 등록된 Worktree 경로를 사용한다. 이때는 브랜치/HEAD 일치와 Clean 상태를 별도 요구하며, Quality Job과 증거는 실제 경로에 결합한다.
- Lifecycle Hooks는 [ADR 0014](../../adr/0014-worktrunk-hooks-opt-in.md)에 따라 기본 비활성화다.
  `run_hooks=true`를 명시하면 Worktrunk 사용자·프로젝트 설정의 Hooks를 실행한다.
  이때 `--yes`로 Worktrunk의 대화형 승인 프롬프트를 생략하므로, 설정된 Hooks의 외부 명령 실행까지 호출자가 허용한 것으로 간주한다.
  Hooks는 MCP 경로·명령 허용 목록의 OS 수준 격리 대상이 아니며, 해당 호출의 외부 부작용은 MCP가 검증하지 않는다.
- 검토 계획은 등록된 관리형 소스, 정확한 Main·Source SHA 및 Main 조상 관계를 검증한다.
  실제 통합 시점의 승인·Clean 상태·Refs 경합 방어는 별도의 실행 경로가 책임진다.
- 제거는 `--foreground`, 비강제 옵션만 사용한다. MCP가 CLI 호출 직전 등록 경로·HEAD·Clean 상태를 재검증하고, Dirty/Untracked Checkout이나 이동된 Checkout은 삭제 요청 전에 거부한다. 미병합 브랜치 삭제 판단과 별도의 비강제 안전 확인은 Worktrunk에 위임한다.
  MCP `remove_project_worktrunk`는 기존 Task Manager의 `exclusive_git_cleanup` 잠금을 재사용하여
  실행 중·정리 불확실한 Job과 Worktree 삭제를 직렬화한다. 독립 Git 프로세스까지 잠그지는 않는다.
  CLI의 결과 JSON만으로 삭제 성공을 확정하지 않고 대상 경로의 부재, Git Worktree 등록 해제, `branch_outcome`과 실제 브랜치 Ref 존재 여부를 재확인한다. 불일치하거나 재조회에 실패하면 성공을 반환하지 않고 현재 상태를 다시 점검하도록 한다.
- Worktrunk의 stdout JSON 형태는 명령마다 다르며, 삭제는 결과의 대상·`branch_outcome`을 검증한다. 지원하지 않는 결과는 성공으로 보고하지 않는다.
- 모든 외부 실행은 argv 기반이며 stdin을 닫는다. stdout은 제한된 크기로 스트리밍 수집하여 1 MiB 초과 시 자식 프로세스를 종료·회수한다. 읽기/쓰기 실행 시간 제한 및 비대화형 Git 설정을 유지하며 호출자 제공 쉘 문자열은 실행하지 않는다.
- 타임아웃 또는 결과 확인 실패 이후 동일한 쓰기 명령을 자동 재실행하지 않는다.

이 구현도 MCP 정책을 OS Sandbox로 바꾸지는 않는다. Worktrunk CLI·Git 프로세스는
실행 계정의 실제 파일 시스템 권한으로 동작한다. 다른 프로세스와의 원자적 잠금 및
명령 실행 중 Ref 이동까지 완전히 차단하지 않는다.

## 검증과 현재 제한

`tests/tools/git/test_worktrunk_cli.py`는 임시 Git 저장소에서 Worktrunk 실행 파일을 직접 호출한다.
`tests/tools/git/test_worktrunk_mcp.py`는 FastMCP Client를 통해 새 도구의 공개 계약을 검사한다.
`tests/tools/git/test_worktrunk_quality.py`는 Main Checkout이 없는 경우에도 제안이 생성되는지 검증한다.

- macOS에서 생성·기존 브랜치 체크아웃·Worktree 재사용·Worktree 격리·경로 점유 충돌·1개/3개 커밋 Squash·FF·삭제·권한 거부를 검증한다. 읽기 전용 상태 조회는 `wt` 설치·권한 없이도 Git 등록 정보로 동작하며, 부재·Dirty·외부 경로·불일치 등을 분류하는 테스트가 있다. Workspace 내 외부 생성 Worktree 재사용과 브랜치 정리 결과도 검증한다.
- Hooks 선택은 실제 사용자 설정을 실행하지 않고, 기본 `--no-hooks`와 명시적 `--yes` CLI 인자 및 MCP 입력 스키마를 검증한다.
- 테스트 호스트에 `wt`가 없으면 실제 CLI 테스트를 **skip**한다. skip을 PASS로 계산하지 않는다.
- Windows에서의 프로세스 실행, Worktrunk 설치, 경로 처리 및 OS별 테스트는 별도 검증이 필요하다.
- 기존 main 전용 `Source-PR` hooks 및 GitHub Rulesets가 활성화되어 있으면 실제 운영 저장소의
  로컬/원격 통합에 영향을 준다. 이 실험은 그 정책을 변경하지 않는다.
- 실제 사용자의 작업 저장소에는 검증 목적의 Worktrunk 통합·정리 명령을 실행하지 않는다.
- Worktrunk MCP와 내부 Worktrunk Adapter에는 로컬 main 병합 진입점을 제공하지 않는다.
  검증된 변경 제안은 별도의 사용자 승인·GitHub PR/CI 또는 사용자가 직접 통제하는 Git 작업으로 통합한다.
- `get_project_worktrunk_merge_plan`은 읽기 전용이며 실행 권한을 생성하지 않는다.

[ADR 0016: Worktrunk의 기능 단위 Native 대체 경계](../../adr/0016-worktrunk-native-migration-boundary.md)에
준비·조회·검사·삭제·평가·원격 게시별 대체 가능성과 이행 순서를 기록한다.
기존 Native 도구는 계약과 호환성이 검증되기 전까지 유지한다.
