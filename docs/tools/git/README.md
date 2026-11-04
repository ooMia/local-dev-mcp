# 로컬 Git 작업 계약

현재 Git 도구는 HEAD 조회와 status → diff → stage/unstage → commit의 로컬 흐름을 제공한다.

도구 추가·공통 호출 기준은 [개발 규약](../../conventions.md#도구-정의와-호출-경계),
module의 연결 원리는 [실행 흐름](../../design.md)을 참조한다.

run 시작 시 Git 실행과 commit 준비를 따로 고정한다. Git 미설치·미허용은 Git 도구에,
identity·서명 준비 오류는 commit에 한정한다. 다른 기능이나 연결 전체를 막지 않는다.
준비 부족은 [`get_tool_readiness`](../../configuration.md#도구-준비-상태)에서 확인하고 수정 후 재시작한다.
권한 middleware와 직접 Git 준비·실행 경로의 검사도 유지한다.
`GIT_COMMIT_SIGN=true`인 경우에만 commit이 gpg를 추가 요구한다.
`git` 허용은 이 읽기·쓰기 도구 모두에 적용되며 하위 명령별 정책은 현재 없다.
실행 디렉터리 자체가 working tree root여야 한다. 상위 저장소를 자동 채택하지 않는다.

## 도구와 입력

| 도구 | 입력 | 역할 |
| --- | --- | --- |
| `get_project_head` | 없음 | 현재 로컬 HEAD commit의 전체 SHA |
| `get_project_status` | 없음 | branch/HEAD, 경로별 index·working-tree 상태 |
| `get_project_diff` | `staged=false`, `paths=null` | working tree 또는 index의 patch 검토 |
| `stage_project_paths` | `paths` | 선택 범위의 추가·수정·삭제를 index에 반영 |
| `unstage_project_paths` | `paths` | 작업 파일을 유지하면서 선택 범위 staging 해제 |
| `commit_project_changes` | `message` | 현재 index 전체를 commit |

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
일반 diff는 untracked 파일의 내용을 보여주지 않는다. 새 파일은 stage 후 staged diff로 검토할 수 있다.
외부 diff·textconv 프로그램은 실행하지 않아 외부 프로그램에 의한 추가 실행과 내용 변환을 피한다.
patch의 credential 자동 탐지·마스킹은 제공하지 않는다.

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
| status | `branch`, `head`, `entries` |
| diff | `staged`, `paths`, `patch` |
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
