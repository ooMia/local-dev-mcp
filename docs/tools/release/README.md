# 개발용 릴리스 도구

이 내부 CLI는 선택한 Git workspace의 commit snapshot을 읽어 명시적으로 기록한 호환성 판단을 검증·집계한다.
서버 시작·dotenv·계정 credential을 로딩하지 않으며 임의 shell 명령을 받지 않는다.
개발 규약과 판단 기준은 [conventions](../../conventions.md), 실제 Git 통합은 [Git 계약](../git/README.md)이 소유한다.

최종 wheel·sdist의 내부 평가·영수증 제외는 [배포물 검사](../workflow/README.md#릴리스와-배포물)를 따른다.

## 설정과 평가 기록

Python 단일 패키지의 `pyproject.toml`에서 명시적으로 사용한다.

```toml
[tool.local-dev-mcp.release]
enabled = true
base_branch = "main"
initial_base = "<이 프로젝트의 관측된 전체 commit SHA>"
```

`initial_base`는 사용 가능한 초기 비교 지점이다. 사용자에게 배포한 마지막 artifact를 자동 발견하거나
그 SHA를 대신하는 값이 아니다. 설정 필드·고정 경로를 임의로 늘리지 않는다.

평가 기록은 `.changes/<소문자-숫자-하이픈>.toml`에 둔다.

```toml
impact = "minor"
summary = "새로운 선택적 MCP 기능 추가"
reason = "기존 도구의 호출·결과 계약을 유지하며 기능을 추가한다."
contract = "MCP"
assessed_by = "agent"
migration = ""
supersedes = []
```

`impact`는 `major | minor | patch | none`, `assessed_by`는 `user | agent`다.
필드 타입·알 수 없는 필드·빈 판단 근거를 거부한다. `major`에는 비어 있지 않은 migration이 필요하다.
`assessed_by`는 기록 작성자의 선언이며 인증·승인 토큰이 아니다.

최초 정리 기준선의 malformed 평가 보완은 [ADR 0008](../../adr/0008-initial-consolidated-baseline.md)의 한정 예외를 따른다.
일반 변경에서는 과거 기록을 고치거나 지우지 않는다. 잘못된 판단·완전한 revert를 반영할 때는 새 기록의
`supersedes`에 미소비 기록 ID를 명시하고 이유를 적는다. 정정된 원본도 감사 가능하도록 릴리스 전까지 보관한다.
없는 ID·자기 참조·순환·하나의 기록을 복수로 정정하는 상태는 거부한다.
정정이 실제로 맞는지는 사용자나 에이전트가 공개 계약을 비교해 판단한다.

Git tree의 일반 파일만 읽는다. 기록당 16 KiB·최대 256개이며 중첩 경로·symlink·비 UTF-8 입력을 거부한다.
`.changes/README.md`는 설명 파일로 제외한다. 실제 집계는 정정되지 않은 유효 기록을 대상으로 한다.

## 조회와 공통 병합 검사

```sh
uv run python -m local_dev_mcp.tools.release plan
uv run python -m local_dev_mcp.tools.release check --base <전체 SHA> --head <전체 SHA>
uv run python -m local_dev_mcp.tools.release --workspace /absolute/project plan
```

`--workspace`는 subcommand 앞에 지정한다. 생략하면 현재 폴더를 쓰며 선택한 폴더 자체가 Git root여야 한다.
`--head` 생략은 현재 HEAD를 관측한다. 명시한 SHA는 40/64자리 전체 값만 받고 branch·revision 표현식을 받지 않는다.
dirty 작업 파일은 commit snapshot에 포함하지 않는다. 두 commit은 정확한 불변 입력이며 fetch·checkout을 하지 않는다.

`plan`은 JSON으로 기준 `head`, `digest`, `current_version`, `next_version`, `maximum_impact`,
영향별 `counts`, 최대 영향의 기록 ID `causes`, 정정 이력을 포함한 `changes`를 반환한다.
안정판에서는 major/minor/patch를 그대로 한 번 적용한다. 초기 `0.x`에서는 major/minor가 다음 minor,
patch가 다음 patch를 제안한다. 원래 breaking 분류는 보존하며 첫 Stable을 자동 선언하지 않는다.
유효 기록이 없거나 모두 none이면 `next_version=null`이다.

`check`는 상위 설정이 없는 workspace를 legacy로 구분한다. opt-in한 workspace는 기존 설정을 없애거나
비활성화해 우회할 수 없다. 현재 base의 모든 기록을 보존하고 이번 source에 새 평가를 추가해야 한다.
`none`도 평가 기록이며 기록 생략의 예외가 아니다. 일반 변경은 version·changelog·release receipt를 바꾸지 않는다.
이 검사와 Git MCP 병합은 같은 구현을 사용한다. 내용의 의미상 호환성 판단은 검사 성공만으로 입증되지 않는다.

정상 종료는 0, 검증·로컬 I/O 실패는 2다. 실패를 무변경 또는 자동 재시도 가능한 상태로 해석하지 않는다.
Git 조회 출력은 명령당 1 MiB, 10초로 제한하고 외부 Git 오류 원문은 공개하지 않는다.

## Chat MCP 릴리스 흐름

연결된 Chat에서 `get_release_plan()`을 호출하면 **커밋된** HEAD 기준 `plan`(`digest`, `current_version`, `next_version`, `maximum_impact`, `counts`, `causes`, 평가별 근거), 현재 branch, Clean 여부를 조회한다. Dirty한 작업 트리의 평가 파일은 계산에 포함되지 않는다. 권장 버전은 명시적 평가를 바탕으로 한 제안이며, 그 의미상의 타당성은 별도로 판단한다. 사용자가 다른 버전을 선택하려면 임의 버전 문자열로 계산을 우회하지 않고 해당 평가·호환성 근거를 수정 후 커밋하여 다시 계획한다.

1. `get_release_plan`으로 전체 SHA·Digest·권장 버전과 Clean 여부를 확인한다. `next_version=null`이면 준비할 릴리스가 없다.
2. `main`에서 `create_project_branch(new_branch="release/<next_version>", expected_branch="main", expected_head=<plan.head>)`를 호출한다. 브랜치 변경은 사용자 요청의 실행 범위와 Git 정책을 따른다.
3. `prepare_project_release(expected_head=<plan.head>, expected_plan=<plan.digest>, expected_version=<plan.next_version>)`를 호출한다. 이 도구는 `git` 외에 **`uv.release`** 명시 권한 및 uv 실행 파일을 요구하며, Clean 릴리스 브랜치와 출력/평가 파일의 접근·쓰기 정책을 다시 검사한다. `uv.release` 권한을 설정했다면 서버를 재시작한다.
4. 생성된 파일(`pyproject.toml`, `uv.lock`, `CHANGELOG.md`, `.release.toml`)과 소비된 `.changes/*.toml`을 검토하고 stage·commit한다. 정확한 base/head의 릴리스 통합 검사와 전체 품질 검사를 수행한다.
5. `release/X.Y.Z` 브랜치를 원격 main에 대한 비강제 Push 후 `create_project_draft_pr`로 Draft PR을 연다. 게시 전 영수증과 소비 범위를 다시 검증하며 일반 기능 PR용 신규 평가를 요구하지 않는다. [PR 통합 절차](../git/remote-pr.md#채팅-내-ci-관측과-ready-전환)에 따라 정확한 HEAD의 CI·Ready·검토된 Squash Merge를 확인한다. 로컬의 `merge_project_branch` 경로는 별도 로컬 통합 정책을 따른다.
6. Tag·artifact Push·Publish는 병합된 최종 release commit에 대한 별도 작업이다.

`get_release_plan`은 읽기 전용이며 `prepare_project_release`는 파일 쓰기 도구다. Prepare는 **재실행용 영속 영수증을 제공하지 않으므로**, 실패·응답 유실 후 동일 요청을 즉시 재호출하면 안 된다. 상태를 재조회해 부분 변경을 검토한다. 둘 모두 원격 Release를 자동 수행하지 않고 임의 명령을 실행할 수 없다.

## 릴리스 준비

계획의 head·digest·next_version을 확인한 뒤 같은 HEAD에서 `release/<next_version>` 브랜치를 만든다.

```sh
uv run python -m local_dev_mcp.tools.release prepare --expected-head <전체 SHA> --expected-plan <digest>
```

clean index·작업 트리, 정확한 branch/head/digest, 일반 파일·부모 경로를 확인한다.
uv의 실제 workspace root가 선택한 Git root와 다르면 쓰기 전에 거부한다. 상위 uv workspace의 member는 지원하지 않는다. none만 남은 계획은 준비하지 않는다.
uv version이 계산한 정확 버전과 lock을 갱신하며 환경 sync는 하지 않는다.
lock 갱신에 필요한 의존성 metadata는 uv의 표준 조회를 사용하므로 index 접근이 필요할 수 있다. 실패해도 자동 재실행하지 않는다. 고정 Towncrier 렌더러로 notes를 만들고,
기존 changelog bytes를 보존해 새 항목 하나를 추가한다. `.release.toml`에는 원본 head·계획 digest·old/new version·
소비 기록 ID를 남기고 기록을 소비한다. commit·tag·push·publish는 수행하지 않는다.

외부 uv 실행이 실패하거나 이후 외부 변경을 관측하면 version/lock이 이미 바뀌었을 수 있다. 자동 rollback·재시도
하지 말고 상태를 조회한다. 새 source를 확인하지 않은 채 이전 prepare 명령을 반복하지 않는다. 기존 출력이 symlink이거나
관측한 원문과 달라졌으면 덮어쓰지 않는다. 설치 제품의 version 표시는 설치 metadata를 따르므로 개발환경의
`uv sync --locked` 및 서버 재시작 전까지 이전 version이 관측될 수 있다.

준비 결과를 검토·stage·commit한 뒤 `check --base <준비 원본 SHA> --head <준비 commit SHA>`를 실행한다.
릴리스 검사는 main의 현 계획과 영수증, pyproject/lock 일치, 기록 전체 소비, 결정론적 changelog, 허용된 경로만의
차이를 검사한다. 다른 source/의존성 수정이나 main 이동이 있으면 재준비가 필요하다. MCP로 통합할 때는 정확한
`release/<계산 버전>` 브랜치와 최종 메시지도 필요하다. 태그는 통합된 릴리스 commit에 명시적으로 붙이는 별도 단계다.
