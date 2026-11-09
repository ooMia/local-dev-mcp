# 로컬 Git 작업 계약

현재 Git 도구는 HEAD 조회와 status → diff → stage/unstage → commit의 로컬 흐름, 명시적인 새 로컬 브랜치 생성·전환, 사전 검증된 local main 통합을 제공한다.

Private GitHub 원격의 상태 조회·main 추적 Ref Fetch·기능 브랜치 비강제 Push·Draft PR 생성·명시적 PR 병합은
별도 권한의 [원격 PR 발행 MCP 계약](remote-pr.md)을 따른다. 일반 Git 권한만으로 원격 쓰기를 실행할 수 없다.

도구 추가·공통 호출 기준은 [개발 규약](../../conventions.md#도구-정의와-호출-경계),
module의 연결 원리는 [실행 흐름](../../design.md)을 참조한다.

run 시작 시 Git 실행과 commit 준비를 따로 고정한다. Git 미설치·미허용은 Git 도구에,
identity·서명 준비 오류는 commit에 한정한다. 다른 기능이나 연결 전체를 막지 않는다.
준비 부족은 [`get_tool_readiness`](../../configuration.md#도구-준비-상태)에서 확인하고 수정 후 재시작한다.
권한 middleware와 직접 Git 준비·실행 경로의 검사도 유지한다.
`GIT_COMMIT_SIGN=true`인 경우에만 commit이 gpg를 추가 요구한다.
로컬 Git 도구에는 `git` 허용이 공통 적용된다. 원격 Git/PR 도구는 별도로 `gh`와 `git.remote.read`, `git.remote.write`, `gh.pr.create`, `gh.pr.ready`, `gh.pr.merge` 중 해당 논리 권한을 요구한다.
선택한 workspace 자체가 working tree root여야 한다. 상위 저장소를 자동 채택하지 않는다.

격리된 linked Worktree 준비·조회와 브랜치 기준 게시의 입력·실패 경계는 [Worktree 계약](worktrees.md)을 따른다.

로컬 후보 이력의 재구성은 [이력 재구성 계약](history-consolidation.md)을 따른다.

## 도구와 입력

| 도구 | 입력 | 역할 |
| --- | --- | --- |
| `get_project_head` | 없음 | 현재 로컬 HEAD commit의 전체 SHA |
| `get_main_head` | 없음 | 현재 브랜치를 변경하지 않고 로컬 `main`의 전체 SHA 조회 |
| `get_project_ref` | `ref` | 로컬 브랜치 또는 전체 Commit SHA를 읽기 전용으로 확인 |
| `compare_project_refs` | `base_ref`, `target_ref`, `expected_base_head`, `expected_target_head`, `paths=null` | 두 Ref의 이력 수·변경 경로·패치 비교 |
| `inspect_project_commit_history` | `base_ref`, `target_ref`, `expected_base_head`, `expected_target_head` | 기준 조상 이후 최대 64개 Commit의 SHA·부모 SHA·제목을 순서대로 조회 |
| `consolidate_project_history` | `new_branch`, `base_head`, `expected_branch`, `expected_head`, `units`, `allow_dirty_controller=false` | 원본 tree 보존 후보 생성; 명시적 opt-in은 dirty controller를 지문화해 보존하며 main·checkout 유지 |
| `inspect_project_policy` | `operation="integrate_main"`, `source_ref`, `expected_main_head`, `expected_source_head` | 읽기 전용 로컬 통합 사전 진단. 브랜치·조상·커밋된 평가와 미확인 원격 PR/CI 경계를 구분; 병합 승인 아님 |
| `get_project_status` | 없음 | branch/HEAD, 경로별 index·working-tree 상태 |
| `get_project_diff` | `staged=false`, `paths=null`, `include_untracked=false` | working tree 또는 index patch, 명시 범위의 제한된 untracked 미리보기 |
| `stage_project_paths` | `paths` | 선택 범위의 추가·수정·삭제를 index에 반영 |
| `unstage_project_paths` | `paths` | 작업 파일을 유지하면서 선택 범위 staging 해제 |
| `commit_project_changes` | `message`, 선택적 `expected_branch`, `expected_head`, `expected_index_fingerprint`, `expected_staged_paths` | 전체 index commit. 보호 입력 4개를 함께 주면 정확한 staged 경로와 인덱스 지문을 재검증 |
| `create_project_branch` | `new_branch`, `expected_branch`, `expected_head` | 현재 HEAD에서 새 로컬 브랜치를 만들고 전환; 미커밋 변경 유지 |
| `merge_project_branch` | `expected_source_branch`, `expected_source_head`, `expected_main_head`, `message=null` | 설정된 workflow는 평가 기록을 검증한 squash; 미설정은 `--ff-only` |
| `create_project_branch_from_ref` | `new_branch`, `base_ref`, `expected_base_head`, `expected_branch`, `expected_head` | 확인된 Ref에서 checkout 없이 새 브랜치 생성 |
| `switch_project_branch` | `target_branch`, `expected_target_head`, `expected_branch`, `expected_head` | 추적 변경이 없는 상태에서 기존 로컬 브랜치 전환 |
| `get_project_pick_state` | 없음 | HEAD·Index SHA-256 지문·변경 경로 관측 |
| `cherry_pick_project_commit` | `commit`, `expected_branch`, `expected_head`, `expected_index_fingerprint`, `allow_staged=false` | 비병합 커밋 하나를 commit 없이 적용 |
| `restore_project_pick_paths` | `paths`, `expected_branch`, `expected_head`, `expected_index_fingerprint` | 명시한 staged 파일만 HEAD 기준으로 복구 |


`staged`는 JSON boolean만, `paths`는 문자열 JSON 배열만 받으며 알 수 없는 필드는 거부한다.
예를 들어 `{"staged":"yes"}`나 `{"paths":"[\"a.py\"]"}`는 실행하지 않는다.
인자 오류는 안전한 필드·오류 종류·수정 안내를 반환하며 잘못된 값을 그대로 되풀이하지 않는다.
`get_project_diff()`의 인자 생략과 `paths=null`은 유효한 전체 조회지만, stage·unstage는 목록을 반드시 지정한다.

직접 Git argv를 받지 않는다. shell 문자열 없이 인자 배열을 사용한다.
경로는 비어 있지 않은 프로젝트 상대 경로 목록이며 파일·디렉터리·`.`를 허용한다.
`diff`에서 `paths=null`이면 전체 프로젝트, 명시적인 빈 목록은 오류다.
절대 경로·NUL·프로젝트 밖으로 나가는 경로·외부로 연결되는 부모 symlink 탐색을 거부한다.
경로를 정규화한 프로젝트 상대 이름으로 반환한다. 마지막 경로 자체가 symlink이면 Git entry로 다루고 대상을 읽지 않는다.
디렉터리는 하위 전체를 선택한다. `--literal-pathspecs`와 `--`로 옵션·패턴 주입을 막는다.

stage는 기존의 부분 staging을 현재 작업 파일 내용으로 덮어쓸 수 있다.
전체 반영은 반드시 `paths=["."]`로 요청한다. ignored 파일 강제 추가와 대화형 부분 staging은 없다.
일반 diff는 untracked 파일의 내용을 보여주지 않는다. `include_untracked=true`일 때만 명시 `paths`(루트 `.` 제외) 안의 비무시 untracked UTF-8 일반 파일을 제한적으로 미리본다.
이 옵션은 `staged=true`와 함께 사용할 수 없다. 파일당 32 KiB, 최대 32개, 미리보기 전체 128 KiB를 넘으면 일부만 반환한다.
symlink·바이너리·읽기 실패·허용되지 않은 경로는 미리보기에서 제외하고 `complete=false`로 표시한다.
무시된 파일은 Git status에서 제외되므로 미리보기 대상이 아니며, `complete=true`도 무시된 파일까지 검토했다는 의미는 아니다.
이때 `patch`에 포함된 신규 파일 내용은 Python이 생성한 unified text preview이며 `git diff --cached`와 동일한 Git 객체 검증 결과가 아니다.
실제 staging·index 변경·임시 `git add -N` 없이 읽는다. 원문에는 자동 credential 마스킹이 없으므로 필요한 파일만 요청한다.
기존처럼 새 파일을 stage한 뒤 staged diff로도 검토할 수 있다.
외부 diff·textconv 프로그램은 실행하지 않아 외부 프로그램에 의한 추가 실행과 내용 변환을 피한다.
patch의 credential 자동 탐지·마스킹은 제공하지 않는다.

## 커밋 사전 조건 — 선택적 보호 모드 (Issue #8 P1)

기존 `commit_project_changes(message)`는 하위 호환성을 위해 현재 인덱스 전체를 계속 커밋한다.
**기본적으로 의도 밖 staged 경로를 차단하는 기능이 아니다.** 에이전트의 변경 단위 커밋에는
아래 네 필드를 **모두** 함께 사용하는 보호 모드를 권장한다.

1. `stage_project_paths` 이후 `get_project_diff(staged=true)`로 변경 내용과 경로를 검토한다.
2. `get_project_pick_state`에서 현재 `branch`, `head`, `index_fingerprint`를 읽는다.
3. `expected_staged_paths`에는 검토한 **모든** staged 파일 이름을 정확하게 지정한다.
   Rename은 이전·새 경로를 모두 포함하며, 디렉터리나 `.`로 범위를 확장하지 않는다.
4. `commit_project_changes`에 관측 값과 메시지를 함께 전달한다.

보호 모드는 네 필드 중 일부만 있거나, 실제 staged 경로가 다르거나, 동일 경로의 staged
내용이 재스테이징되어 인덱스 지문이 달라지거나, HEAD·브랜치가 바뀌면 커밋 전에 거부한다.
초기 unborn 브랜치의 `head=null`은 `expected_head="unborn"`으로 표현할 수 있다.
거부 후 자동 unstage·reset·retry는 하지 않는다. 현재 staged diff와 pick state를 다시 확인한다.

이 사전 조건은 다른 OS 프로세스와 원자적이지 않으며 Git commit hook이 인덱스를 바꾸는
것까지 차단하지 않는다. 완료 후 실제 커밋의 변경 파일·HEAD와 기존 staged 검토 결과를
비교해야 한다. 명시적 보호 모드가 아닌 기존 API 사용자는 stage 범위만으로 안전을 가정하지 않는다.

## 선택적 Git 마이그레이션

`create_project_branch_from_ref`는 현재 브랜치·HEAD와 기준 Ref의 전체 SHA를 검사한 후
새 로컬 브랜치만 생성하고 checkout하지 않는다. `switch_project_branch`는 추적 파일과
index가 깨끗해야 실행되며, 충돌하지 않는 untracked 파일은 Git의 기본 보호 규칙에 따라 유지한다.
오류 후에는 자동 복구하지 않고 브랜치·HEAD·status를 재조회한다.

`get_project_pick_state`의 SHA-256 지문은 `git ls-files --stage -z`로 관측한 index의
상태를 나타내며, 사용자가 선택한 경로만의 지문이 아니다. Pick·복구 작업은 현재 브랜치·HEAD·
index 지문을 모두 검사한다. `cherry_pick_project_commit`은 단일 비병합 커밋의 전체 SHA만
받고, 보호 경로·기존 ignored/untracked 충돌을 사전에 검사한다.
`git cherry-pick --no-commit`을 사용해 적용 내용은 index에 남기고 commit하지 않는다.
다음 Pick의 `allow_staged=true`는 명시적으로 검토한 index 상태에서만 사용한다.
충돌·실패하면 일부 변경이 남을 수 있으므로 자동 abort·reset·replay하지 않는다.

`restore_project_pick_paths`는 명시한 개별 파일만 staged 상태에서 HEAD로 복구하며,
Index와 Working Tree 모두 해당 경로의 변경을 잃는다. 원본을 별도로 보관·검토하지 않았다면
실행하지 않는다. 전체 루트 `.` 복구, unstaged 추적 변경이 있는 복구,
관측되지 않은 staged 경로 복구는 거부한다.

## 공개 경로와 변경 범위

파일의 공개 범위는 [파일 접근 정책](../../configuration.md#파일-접근-정책)을 따른다.
보호 이름·접근 금지·허용 범위 밖의 경로는 status에서 제외하고, diff는 해당 변경의 원문을 patch에 포함하지 않는다.
rename은 이전·새 경로가 모두 허용되어야 공개하며, patch는 이름 추론으로 제한된 원문을 다시 포함하지 않도록 삭제·추가로 표시한다.
submodule은 짧은 변경 표시만 반환하고 하위 파일의 patch로 확장하지 않는다.

명시적으로 금지된 파일을 diff·stage 대상으로 요청하면 안전한 오류로 거부한다.
전체 또는 디렉터리 조회에 제한된 변경이 섞여 있으면 허용된 결과만 반환하고
`complete=false`, `reason="restricted_paths"`, `next_action`을 표시한다.
이때 빈 entries나 patch는 저장소가 깨끗하거나 변경이 없다는 증거가 아니다.

stage 범위에 읽을 수 없는 후보가 있으면 전체 요청을 거부하며 조용히 일부만 stage하지 않는다.
이미 index에 보호·접근 금지·허용 범위 밖의 변경이 있으면 commit 전에 거부한다.
해당 경로를 unstage하거나 소유자와 정책을 확인한 뒤 재요청한다. unstage는 작업 파일을 유지하며
index에서 제한된 항목을 제거하는 복구에도 사용할 수 있다.

`read_only_paths`와 `.gitignore`의 읽기 전용 규칙은 직접 파일 수정에 적용한다.
읽을 수 있는 경로라면 읽기 전용 지정만으로 Git stage·commit을 금지하지 않는다.
Git 내부 메타데이터 접근과 hooks·필터의 실행은 아래 계약을 따르며 경로 정책을 OS 수준의 격리로 확대하지 않는다.

## 읽기 전용 로컬 Ref 비교

`get_project_ref(ref)`는 로컬 브랜치 이름 또는 40/64자리 전체 commit SHA만 허용한다.
Git revision 표현식·원격 Ref·사용자 임의 Git argv는 허용하지 않는다. Ref가 없거나
commit으로 해석되지 않으면 안전한 오류를 반환한다.

`compare_project_refs(base_ref, target_ref, expected_base_head, expected_target_head, paths=null)`는
두 Ref를 재해석해 사전에 관측한 전체 SHA와 일치할 때만 비교한다.
`merge_base`는 공통 조상이고, `base_only_commits`·`target_only_commits`는 각 브랜치
단독 도달 commit 수다. `changes`는 최종 파일 상태 차이이고, `patch`는
base → target의 양방향이 아닌 **지정 방향** 패치다. 따라서 양쪽의 이력 차이와
최종 스냅샷 차이를 구분한다. 공통 조상이 없으면 `merge_base=null`일 수 있다.

변경 경로는 전체 Ref 사이에서 먼저 확인한 뒤 보호·거부 경로를 걸러낸다.
Rename의 양쪽 중 하나라도 숨김 경로이면 그 Rename 전체를 반환하지 않으며,
`complete=false` 및 `reason=restricted_paths`로 명시한다.
명시적 `paths`는 기존 project-relative 경로 검사를 적용하고, 결과 패치에는
외부 diff·textconv를 사용하지 않는다. 출력이 Git 응답 1MiB 상한을 넘으면
부분 결과 대신 오류로 중단하며, 이 경우 `paths`로 범위를 좁혀야 한다.
실행 종료 전에 양쪽 Ref를 다시 확인하지만 원자적 Git 저장소 snapshot은 보장하지 않는다.
Fetch·checkout·index 변경·commit·branch 삭제를 수행하지 않는다.

## 저장소 상태와 HEAD

고정 argv와 Git 출력 해석은 [명령 구현](../../../src/local_dev_mcp/tools/git/commands/),
공통 실행 설정은 [Git 실행 지원](../../../src/local_dev_mcp/tools/git/_execution.py)을 참조한다.

유효한 symbolic HEAD가 가리키는 branch ref가 아직 없으면 unborn으로 판별하고, 공개 HEAD 도구는 commit이 필요하다는 오류를 반환한다.
비저장소·깨진 HEAD를 같은 오류로 묶거나 unborn으로 간주하지 않는다.
`HEAD^{commit}`은 기존 객체를 commit으로 해석할 수 있는지도 Git에 검증시킨다.
전체 SHA-1은 20 bytes의 40자리 hex, SHA-256은 32 bytes의 64자리 hex이므로 두 길이를 허용한다.

쓰기 전에 working tree root를 확인한다. merge·rebase·cherry-pick·revert 진행 중이거나
unmerged index가 있으면 쓰기를 거부한다. linked worktree의 상태도 해당 worktree 기준으로 판단한다.
이 검사는 외부 프로세스와의 동시 변경을 막는 transaction이 아니다.

최초 commit 전 unstage는 실제 unborn branch인지 확인한 경우에만 선택한다.
깨진 HEAD·ref 조회 실패를 최초 저장소로 간주하지 않는다. `-f`를 사용하지 않으므로 index에만 있는
내용을 잃을 수 있으면 Git의 거부를 그대로 오류로 처리한다.

## 브랜치 분리

`create_project_branch`는 기존 로컬 브랜치에서 현재 HEAD를 기준으로 새 브랜치를 만들고 즉시 전환한다.
`expected_branch`와 전체 `expected_head`는 호출 직전에 조회한 정확한 값이어야 하며, 불일치하면 쓰기를 시작하지 않는다.
브랜치 이름은 영문·숫자·하이픈 및 슬래시로 구분된 이름과 `release/<canonical-X.Y.Z>`를 허용한다. 기존 브랜치 이름과 충돌하면 거부한다.
Index가 stage된 상태, Git integration 작업 중 또는 분리 HEAD에서는 거부한다. 작업 트리의 추적·미추적 변경을
stash/reset/commit하지 않고 새 브랜치로 함께 가져간다. 전환 후 반환하는 `status_unchanged`는 경로 상태가
같다는 관측일 뿐 파일 원문까지 독립적으로 보존했음을 증명하지 않는다.
Git checkout hook이나 외부 동시 변경이 있을 수 있다. 실패 뒤 자동 복구·재시도하지 말고 HEAD·branch·status를 다시 확인한다.
현재 브랜치에 커밋되지 않은 변경은 브랜치 전환 전후로 유지되므로 **분리 후 새 브랜치에서 커밋해야 main과 변경 이력이 분리된다**.

## commit·identity·서명·외부 프로그램

메시지는 NUL이 없고 공백뿐이지 않은 문자열 하나다. 여러 줄을 허용한다.
현재 index 전체를 기록하므로 사용자가 미리 stage한 변경도 포함된다.
자동 stage·경로별 commit·amend·빈 commit·author/date 덮어쓰기·승인 토큰은 없다.
commit할 내용이 없으면 오류다. 메시지 정리와 hook의 메시지 수정은 Git 규칙을 따른다.

identity·서명 의도·GPG 자원의 값과 우선순위는 [설정 계약](../../configuration.md)을 따른다.
사전 identity 검사·실제 commit·서명 검증은 같은 시작 snapshot을 사용한다.
설정 준비나 동적 prepare 실패 시 commit 본문을 실행하지 않는다. 별도 git config·자동 stage·재시도는 없다.
status와 staged diff를 확인한 뒤 commit을 요청한다.

hooks는 유지한다. 서명은 플래그의 의도를 명시적으로 적용하고, true의 실패 후 `--no-verify`·`--no-gpg-sign`으로 재시도하지 않는다.
필터도 유효한 Git 설정대로 실행한다. 필수로 지정하지 않은 필터의 부재·실패는 Git이 원문 통과로 처리할 수 있다.
필요한 필터·기존 GnuPG 키·agent의 비대화형 키 사용은 사용자가 준비한다. 도구는 입력을 질문하지 않는다.

`git` 허용은 hooks·필터·서명 프로그램 등 간접 실행과 네트워크를 제한하지 않는다.
쓰기 명령 뒤 부수적인 백그라운드 관리 프로세스가 생기는 일을 줄이기 위해 해당 실행의 자동 maintenance/GC를 끈다.
로컬 config는 변경하지 않는다.
외부 예약 maintenance와 hook이 따로 실행하는 프로세스는 통제하지 않는다.

## 결과·실패·크기 제한

모든 정상 결과에는 `project`와 UTC `observed_at`을 포함한다.

| 도구 | 추가 결과 |
| --- | --- |
| HEAD | `head` |
| status | `branch`, `head`, `entries`, `complete`, `reason`, `next_action` |
| diff | `staged`, `paths`, `patch`, `complete`, `reason`, `next_action` |
| stage/unstage | `success`, `paths` |
| commit | `success`, `head_before`, `head_after`, `head_observed`, `observation_error`, `signing_requested`, `signature_verified`, `signature_error` |

status의 unborn HEAD는 null, detached branch도 null이다. 경로 entry는 kind·path·original_path·
index_status·worktree_status·submodule 정보를 가진다. rename의 이전 경로와 공백·개행 파일명을 보존한다.
UTF-8로 안전하게 표현할 수 없는 출력은 문자 대체 없이 오류로 반환한다.

commit 전후 HEAD는 관측값이며 외부 동시 변경이 있으면 원인을 확정하지 못한다.
명령은 성공했지만 후속 HEAD 조회가 실패한 경우 `success=true`, `head_observed=false`와 안내를 반환한다.
이때 commit을 실패로 간주해 자동 재시도하지 않는다.

공통 Git 실행은 읽기 명령별 10초, 쓰기 명령별 60초 timeout을 사용한다.
HEAD는 추가로 root·HEAD·unborn 판별을 합친 전체 10초 실행 예산을 적용한다.
다른 도구는 저장소 확인·상태 확인 등 여러 명령의 합계에 별도 예산을 적용하지 않는다.
취소 시 shellous가 직접 프로세스를 정리하고 공통 실행이 취소를 다시 전달한다.
정리 시간까지 포함한 정확한 종료 시간이나 hooks의 전체 자식 트리 종료를 보장하지 않는다.
stdout은 스트림으로 읽고 최대 1 MiB만 보관하며 초과분도 끝까지 소비한다. 초과 조회는 불완전한 결과 대신 오류다.
큰 diff는 paths로 좁힐 수 있다. status에는 현재 경로 필터·pagination이 없다.
쓰기 명령의 stdout/stderr와 Git 오류 원문은 버리며 안전한 작업 맥락·종료 코드·조치만 오류로 반환한다.

timeout·취소·오류가 발생했다고 index·작업 파일·HEAD가 변하지 않았다고 해석하지 않는다.
hook이 변경한 뒤 실패하거나 commit 생성 후 중단될 수 있다. 상태와 HEAD를 재조회하고 판단해야 한다.
자동 재시도·rollback·전체 자식 트리 정리·동시 실행의 원자성은 제공하지 않는다.

## 선택적 OpenPGP 서명과 검증

서명 활성 여부·키 선택·홈/프로그램 탐색·권한은 [서명 설정](../../configuration.md#서명-기능과-내부-의존성)을 따른다.
서명 활성 시 Git에 적용하는 옵션은 저장소 설정보다 우선한다.
선택한 OpenPGP 형식·키·서명 프로그램은 해당 실행에만 적용하며 저장소 config에 기록하지 않는다.
연결 옵션은 [서명 실행 지원](../../../src/local_dev_mcp/tools/git/_signing.py)을 참조한다.

비대화형 wrapper와 공유 daemon의 수명주기는 [GPG 준비](../../configuration.md#gpg-준비와-피드백)를 따른다.
hooks가 .env를 바꾸어도 현재 서버는 시작 snapshot을 유지한다.

생성 성공 뒤 관측한 SHA의 서명 유효성과 서명 키 계열을 확인한다.
조회 인자와 출력 해석은 [서명 검증 구현](../../../src/local_dev_mcp/tools/git/commands/show.py)을 참조한다.
`G` 또는 `U`의 유효 서명이며 지정 fingerprint가 signer 또는 primary fingerprint와 일치해야 한다.
`!`가 있으면 실제 signer의 정확한 fingerprint 일치도 요구한다. owner trust와 암호학적 유효성은 구분한다.
검증 중 자동 키 다운로드·가져오기를 끈다. GitHub 계정 등록·Verified 표시까지 확인하지 않는다.
`success=true`는 commit 명령 성공이다. 비서명 결과는 `signing_requested=false`와 signature_verified/error=null이다.
서명 요청은 signing_requested=true이며, 미요청을 검증 실패로 표시하지 않는다. 후속 검증이 실패해도 commit을 되돌리거나 재시도하지 않고
`signature_verified=false`와 안전한 `signature_error`를 반환한다. HEAD를 관측하지 못한 경우도 검증 불가다.
Git/GPG 원문 출력·identity·fingerprint는 Chat 결과나 기본 진단에 포함하지 않는다.
동시 HEAD 변경·hooks의 임의 동작·전체 자식 트리 관리에 대한 기존 한계는 유지한다.

## 평가 기록에 근거한 main 통합

[릴리스 workflow](../release/README.md)를 opt-in한 base/source에서는 일반 병합에 새 호환성 평가 기록이 필요하다.
과거 기록·상위 설정을 지우거나 변경해 우회할 수 없다. 기록의 실제 의미는 사용자 또는 에이전트가 판단하고,
도구는 존재·형식·집계와 정확한 SHA를 검사한다. MCP에서 필요한 모든 입력 경로를 내용 조회 전에 승인하며,
변경 경로 또는 기존 평가 기록이 읽기 범위 밖이면 통합을 거부한다.

기존 세 expected 입력은 그대로 필요하다. 설정된 workspace는 최종 Conventional Commit `message`가 추가로
필수이며 type/topic 브랜치를 사용한다. 일반 통합에서 version은 그대로 유지한다. 검사가 통과하면 clean source에서
main으로 전환하고 squash를 stage한 뒤 index tree가 관측한 source와 같은지 확인한다. 기존 identity·서명 의도로
새 커밋을 만들고 단일 parent·tree·실제 제목·서명·최종 status를 확인한다. source의 서명을 새 squash의 서명으로
간주하지 않는다. 원래 source branch는 이동하거나 삭제하지 않는다.

workflow가 없는 workspace는 기존 fast-forward·source 서명 검사 계약을 유지한다. source는 두 경우 모두
현재 main을 포함해야 하며, divergence는 자동 해결하지 않는다. 반환 `mode`는 `squash` 또는 `fast-forward`,
`assessments`는 이번 통합의 평가 ID이며 `main_head_after`는 실제로 관측·검증한 커밋이다.
`merged=true`는 후속 검증까지 통과했다는 뜻이다.

hook 거부·제목 변경·서명 검증 실패·취소·관측 실패 뒤에는 main 전환·staging 또는 commit 생성이 이미
이루어졌을 수 있다. 오류를 무변경으로 해석하지 말고 상태와 HEAD를 조회한다. 자동 rollback·reset·재시도를 하지 않는다.

[프로젝트 hook](../workflow/README.md#로컬-main-검사)을 설치하면 main commit에는 Source-PR이 필요하며,
전체 품질 검사와 ref guard가 추가로 실행된다. Git 쓰기의 60초 제한과 실패 후 관측 계약은 유지한다.
