# 선택적 Trail Code 인덱스 관리 계약 (M1–M4-P1)

## 범위

M1–M3는 이 MCP 서버 인스턴스의 고정 `Policy.root` 하나를 대상으로 한다. M4-P1은 같은 Git 저장소에 등록된 **Clean Linked Worktree** 하나를 정확한 브랜치·HEAD로 명시한 경우에만 독립 Trail 그래프를 조회·갱신한다. 새 Workspace 목록·프로젝트 ID·임의 경로·범용 명령 실행 기능은 추가하지 않는다.

기존 Trail MCP 터널의 조회 연결은 변경하지 않는다. LOCAL_DEV_MCP의 Index Manager는 `trail build` 실행·검증만 담당한다. 다른 프로젝트는 그 프로젝트를 루트로 설정한 LOCAL_DEV_MCP 인스턴스에서 동일한 도구를 실행할 수 있다.

## 활성화 (Opt-in)

이 프로젝트의 `config.toml`에서 `[system].allowed_commands`에 논리 권한 `"trail"`을 추가한다. 명령은 startup PATH의 `trail`에서 찾는다. PATH가 충분하지 않으면 시작 시 읽는 `.env`에 절대 실행 경로를 지정한다.

```dotenv
TRAIL_EXECUTABLE=/Users/mia/.local/share/vite-plus/bin/trail
# M3는 기본적으로 비활성화. 파일 변경 후 자동 갱신을 원하는 경우에만 활성화.
TRAIL_REFRESH_ON_WRITE=true
```

`TRAIL_EXECUTABLE`은 선택 사항이며 macOS 전용 경로를 코드에 하드코딩하지 않는다. Windows native에서는 OS에서 실행 가능한 `trail.exe` 또는 PATH를 이용한다. 실행 파일의 기본 이름은 `trail`/`trail.exe`만 허용한다.

설정 변경 후 LOCAL_DEV_MCP 인스턴스를 재시작해야 한다. 별도 Trail MCP 서버를 추가하지 않는다. 기존 `tunnel-client` 프로필은 그대로 둔다. `trail` 권한 또는 실행 파일이 없으면 상태는 `unavailable`이며 기존 파일·Git 도구는 계속 작동한다. 새 인덱스 도구는 기존 `config.toml`을 자동 수정하지 않는다.

## M1: get_project_index_status

입력은 없다. 고정 루트에서 `trail build --check <Policy.root>`를 실행한다. 구조 인덱스(`graph check`의 changed/added/removed)와 선택적 Deep/context 상태(`trail build --check` 헤더 및 stale summaries)를 따로 판정한다. `graph check: STALE`여도 **stale summaries만 존재하고 구조 변화가 없다면** 구조 인덱스는 `fresh`다. 고정 출력 형식을 인식하지 못하면 Fresh로 추정하지 않고 `unknown`이다. `ready_for_structural_search`는 구조 인덱스가 최신이고 미반영 변경 이벤트가 없을 때만 참이다.

- `structural_state`: `fresh / stale / missing / unknown / unavailable`
- `deep_state`: `fresh / stale / unknown / unavailable`
- `pending_changes`: 최근 MCP 파일 수정 이벤트 이후 재검증이 필요한지 여부
- `reason`: 표준화된 오류 종류만. 외부 프로세스의 원문 출력·비밀은 반환하지 않는다

`--check`에 포함된 오래된 Deep 요약만으로 구조 조회를 금지하지 않는다.

## M2: refresh_project_index

입력은 없다. 동일한 루트에서 `trail build --check`로 먼저 확인하고, 구조 상태가 `stale` 또는 `missing`일 때만 `trail build <Policy.root>`를 실행한다. `--deep`은 사용하지 않는다. 완료 후 다시 `--check`하여 구조 상태가 Fresh인 경우에만 성공으로 판정한다. `unknown`인 검사에 무작정 재빌드하지 않는다.

- 실행 인자는 서버에서 고정하며, 경로나 셸 문자열을 MCP 호출자가 전달하지 않는다.
- `trail` 권한·실행 파일 존재·실행 루트가 확인되어야 실행한다.
- 상태 확인은 30초, 빌드는 120초 제한. 출력은 최대 128 KiB이며 외부 출력 원문을 도구 결과로 노출하지 않는다.
- 증분 `trail build`가 종료 코드 0을 반환해도 후속 검사에서 구조 변화가 남는 경우, `trail build --no-reuse`를 **한 번만** 실행하고 재검증한다. 구조 상태가 여전히 불명확하면 Fresh로 간주하지 않는다.
- 같은 서버 인스턴스의 동시 Refresh는 Lock으로 직렬화하고, 이전 요청의 성공 결과를 재검증해 중복 빌드를 억제한다.
- 실패는 `failed`로 보고한다. 파일 수정이나 커밋을 취소하지 않는다.

## M3: 파일 수정 이후 선택적 자동 갱신

`TRAIL_REFRESH_ON_WRITE=true`가 설정된 경우에만 다음 LOCAL_DEV_MCP 파일 변경 경계에서 알림을 발생시킨다.

- `create_workspace_file` / `replace_workspace_file` / `delete_workspace_file`에서 실제로 변경된 결과(`changed=true`와 `applied` 또는 `applied_unverified`)를 받은 경우
- 동일 `request_id`의 재생 결과, 변경 없음, 거부된 변경은 이벤트를 발생시키지 않는다.
- 여러 변경은 짧게 모아 인스턴스별 단일 자동 갱신 작업으로 처리한다. 다른 변경이 빌드 중 추가되면 다음 검사를 수행한다.
- 파일 변경 응답 이후 실행하며, 후속 인덱스 갱신 실패는 이미 완료된 원본 파일 변경의 성공 여부를 뒤집지 않는다. 서버 종료 시 실행 중 자동 작업을 취소한다.

다른 도구가 직접 만든 Worktree 변경이나 IDE·터미널에서 수행된 수정까지 감지한다고 주장하지 않는다. 해당 경로는 Trail Code의 조회 전 자체 갱신 또는 명시적인 M1·M2 호출을 사용한다.

## 현재 범위의 미해결 검증

실행 정책·MCP 도구 스키마·동시 호출·출력 제한·파일 수정 후 동작은 로컬 테스트에서 검증한다. **실제 설치된 Trail CLI를 사용한 임시 프로젝트 통합 테스트**(`tests/mcp/test_trail_index_e2e.py`)로 초기 인덱스 생성·파일 변경 후 자동 갱신·중복 요청 재생을 검증했다. 이 E2E는 Trail이 설치되지 않은 환경에서는 skip 처리하므로, skip을 성공 근거로 삼지 않는다. 2026-11-15 KST 실제 MCP 재로드 이후 기본 루트의 `refresh_project_index`가 `structural_state=fresh`, `deep_state=stale`, `ready_for_structural_search=true`를 반환했다. **Windows native 검증**은 별도로 남아 있다.

Trail Code 구조 그래프 준비와 현재 터널의 조회 도구가 그 그래프를 참조하는지는 독립적으로 검증해야 한다. 인덱스 갱신 성공만으로 토큰 절감 성과가 입증되는 것은 아니다.

참고: [Trail Code 및 토큰 실험 연구 기록](../../research/trail-code-context-budget-2026-11-14.md)

M4 준비: [Git Linked Worktree별 Trail 그래프 격리·회귀 매트릭스](../../research/trail-worktree-index-m4-2026-11-14.md).

M4-P0 내부 구현: `resolve_index_target`은 기존 공개 M1–M3 입력을 변경하지 않고 등록된 Linked Worktree의 루트·브랜치·HEAD·Clean 상태를 확인한다. Worktree에만 `GRAFT_NO_IGNORE`·`GRAFT_NO_GITIGNORE`을 적용하며 서로 다른 소스의 CLI 조회를 임시 저장소에서 검증했다.

## M4-P1: 등록된 Worktree 전용 상태·갱신

- `get_project_worktree_index_status(branch, expected_head)`: **Git 등록된 Clean Worktrunk**의 정확한 브랜치·HEAD와 Trail 구조·Deep 상태를 확인한다. `git`·`trail` 논리 권한만 필요하며, Worktree 쓰기 권한은 요구하지 않는다.
- `refresh_project_worktree_index(branch, expected_head)`: 같은 대상의 구조 그래프만 갱신하며 `git`·`trail`·`wt`·`git.worktree.write` 권한이 필요하다. `--deep`·`git reset`·`stash`·임의 Git 명령은 실행하지 않는다.
- 두 도구 모두 등록된 상대 Worktree 경로·브랜치·HEAD를 결과에 포함한다. 기본 Checkout은 선택할 수 없다. `project`는 원본 `Policy.root`이며, `worktree_path`는 검증된 상대 경로다.
- 호출 전후 Worktree의 Git 등록, 공통 Git 저장소, HEAD·브랜치, Clean 상태를 확인하고 대상이 이동하거나 Dirty 상태가 되면 결과 성공 대신 오류를 반환한다.
- 같은 서버 인스턴스에서는 **대상 절대 루트별 공유 `asyncio.Lock`**을 사용해 동시 status/refresh를 직렬화하고, 두 번째 갱신은 첫 성공 뒤 재검사해 불필요한 빌드를 생략한다. 서로 다른 루트는 독립적으로 실행할 수 있다. 잠금은 외부 Trail 프로세스·다른 서버 인스턴스와 공유하지 않는다.
- Worktree 내부 Trail 실행에는 `GRAFT_NO_IGNORE=1`·`GRAFT_NO_GITIGNORE=1`을 주입하고 기존 M1–M3 인스턴스 설정과 자동 갱신 루프를 변경하지 않는다. 파일 변경 자동 감지는 Worktree에 확장하지 않는다.
- 임시 Git 저장소의 서로 다른 두 Worktree와 실제 Trail CLI 통합 검증에서 교차 조회·중복 동시 갱신·권한·HEAD·Dirty 상태 거부를 확인한다. Windows native, 외부 프로세스 동시 갱신, 타임아웃·정리 경계, tunnel-client 별도 Worktree 탐색 통합은 **후속 검증 대상**이다.

