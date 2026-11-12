# local-dev-mcp

ChatGPT chat 세션에서 지정한 로컬 개발 workspace를 다루기 위한 MCP 실행 프로젝트다.
현재 파일 목록·경로/내용 검색·텍스트 읽기·관측 구간 수정·파일 생성·단일 파일 삭제,
등록된 lint·format·test 실행과 로컬 Git 작업을 제공한다.
임의 명령 실행·OS 격리는 제공하지 않는다. GitHub 원격 조회·기능 브랜치 게시·Draft PR 생성·명시적 PR 병합은 별도 권한과 인증된 호스트 `gh`가 있을 때만 제공한다.

## 준비와 실행

Python 3.15 이상·uv가 필요하며 Git 도구에는 Git이 필요하다. Chat 연결에는 별도로 설치한 tunnel-client와
사용자가 준비한 OpenAI Secure MCP Tunnel이 필요하다.

1. 이 저장소에서 `uv sync --locked`로 개발 환경을 준비한다.
2. 대상 workspace에 [`.env.example`](.env.example)의 안내로 `.env`를 준비한다.
3. 같은 위치의 `config.toml`에 대상 workspace와 필요한 명령을 허용한다.
   [설정 예시와 권한](docs/configuration.md#configtoml)을 따른다.
4. 이 저장소의 개발 환경에서 대상 경로를 지정해 실행한다.

```sh
uv run --locked local-dev-mcp --workspace /path/to/workspace connect
```

| 명령 | 역할 |
| --- | --- |
| `local-dev-mcp [--workspace PATH] [--allow-policy-edits] run` | 선택한 workspace의 stdio MCP 서버 실행 |
| `local-dev-mcp [--workspace PATH] [--allow-policy-edits] connect` | 시작 설정 사전 검사 후 Tunnel client 실행, client가 같은 Python 환경의 run 실행 |

`--workspace`는 `run`·`connect` 앞에 둔다. 생략하면 현재 디렉터리를 사용한다.
대상 선택과 설정 파일의 기준은 [Workspace 선택](docs/configuration.md#workspace-선택)을 따른다.
루트 `config.toml`·모든 `.gitignore`도 MCP로 편집하려면 시작할 때 `--allow-policy-edits`를 명시한다.
다른 경로 제한과 오래된 관측의 거부는 유지하며, 기본값과 적용 시점은 [정책 파일 편집](docs/configuration.md#정책-파일-편집)을 따른다.

CLI는 credential을 질문하지 않는다. identity는 commit에 필요하고,
서명은 `GIT_COMMIT_SIGN`으로 선택한다. 설정 변경 후에는 재시작한다.
정확한 값·생략·탐색 규칙은 [설정 계약](docs/configuration.md)을 따른다.

Git·identity·서명 준비 부족은 해당 도구만 사용 불가로 만들고 WARNING으로 안내한다.
공통 정책·workspace·필수 파일 오류는 연결을 막는다.
연결된 Chat에서 먼저 준비 상태를 확인한다.

> local-dev-mcp의 get_tool_readiness를 호출해서 사용할 수 없는 도구와 필요한 준비를 알려줘.

연결 중에는 터미널을 유지하고 Ctrl+C로 종료한다. 프로세스 상태의 의미는
[연결 사전 검사와 상태 해석](docs/configuration.md#연결-사전-검사와-상태-해석)을 따른다.
사전 검사 실패는 안전한 원인·수정 안내를 표시하며, 외부 client의 원문 로그는 전달하지 않는다.
직접 stdio로 연결하는 MCP client에는 `local-dev-mcp --workspace /path/to/workspace run`을 사용한다.

## 도구 사용

| 도구 | 역할 |
| --- | --- |
| `list_workspace_entries` | 바로 아래 항목의 이름순 목록 |
| `find_workspace_paths` | glob으로 파일 경로 검색 |
| `search_workspace_text` | literal 내용 검색; 기본은 경로만 반환 |
| `read_workspace_file` | 필요한 줄 범위·전체 version·읽기 참조 |
| `replace_workspace_file` | 읽기 참조가 가리키는 구간 전체를 새 문자열로 교체 |
| `create_workspace_file` | 기존 파일을 덮어쓰지 않고 UTF-8 파일 생성; 부모 생성은 명시적으로 선택 |
| `delete_workspace_file` | 읽기 참조와 전체 버전을 확인한 단일 파일 삭제 |
| `get_release_plan` | 커밋된 변경 영향·권장 버전·SHA/Digest·현재 상태 조회 |
| `prepare_project_release` | 관측한 릴리스 버전·Digest와 일치할 때 버전·Lock·Changelog·Receipt 갱신 |
| `get_tool_readiness` | 시작 snapshot의 도구별 준비 여부·안전한 수정 안내 |
| `list_project_tasks` | 등록된 lint·format·test recipe와 각각의 준비 여부 |
| `start_project_task` | 등록한 작업을 시작하고 짧은 결과 또는 실행 ID 반환 |
| `get_project_task` | 작업 상태 조회; `include_output=true`로 보관된 마스킹 진단 확인 |
| `cancel_project_task` | 작업 취소 요청 후 직접 프로세스 종료 확인 |
| `get_project_head` | 최신 로컬 HEAD commit SHA |
| `get_main_head` | 로컬 main HEAD commit SHA |
| `get_project_ref` | 로컬 브랜치·전체 SHA를 해석하여 비교 기준 확인 |
| `compare_project_refs` | 로컬 Git ref 사이 커밋·경로·patch 비교 |
| `inspect_project_commit_history` | 확인된 두 Commit 사이의 SHA·부모·제목 이력 조회(최대 64개) |
| `consolidate_project_history` | 검토한 기능 경계의 원본 tree를 보존한 로컬 후보 이력 생성 |
| `inspect_project_policy` | 정확한 로컬 main/source SHA의 브랜치·평가 선행 조건 진단. 원격 PR·CI 확인 및 통합 승인은 별도 |
| `get_project_status` | branch/HEAD와 경로별 index·working-tree 상태 |
| `get_project_diff` | working tree 또는 staged patch |
| `stage_project_paths` | 명시한 상대 경로의 추가·수정·삭제 stage |
| `unstage_project_paths` | 작업 파일을 남기고 unstage |
| `commit_project_changes` | 현재 index 전체로 commit |
| `create_project_branch` | 현재 HEAD에서 로컬 브랜치 생성·전환 |
| `create_project_branch_from_ref` | 지정한 로컬 기준 ref에서 브랜치 생성·전환 |
| `switch_project_branch` | 기존 로컬 브랜치로 전환 |
| `get_project_pick_state` | cherry-pick 진행 상태 확인 |
| `cherry_pick_project_commit` | 선택한 로컬 커밋을 현재 브랜치에 적용 |
| `restore_project_pick_paths` | cherry-pick 관련 명시적 경로 복원 |
| `merge_project_branch` | 평가 기록 기반 squash 또는 미설정 workspace의 fast-forward 통합 |
| `diagnose_project_remote_auth` | 인증 설정·GitHub 로그인·private 저장소 접근을 비밀값 없이 진단 |
| `inspect_project_remote` | private GitHub origin 및 로컬·원격 `main`과 기능 Ref 상태 조회 |
| `fetch_project_remote_main` | 관측 SHA가 일치할 때 `origin/main`만 비강제 Fetch |
| `push_project_feature` | 미공개 main 혼입을 거부하고 기능 브랜치만 비강제 Push |
| `create_project_draft_pr` | 신규 `.changes`와 커밋을 기반으로 중복 없이 Draft PR 생성 |
| `get_project_pr_merge_plan` | 실제 PR head와 고정 squash 메시지·검토 Digest 조회 |
| `get_project_pr_status` | 정확한 PR head의 CI·Draft·병합 가능 상태 조회 |
| `mark_project_pr_ready` | 검토 head·Digest에 대해 명시적으로 Ready 전환하고 결과 확인 |
| `merge_project_pr` | 검토 head·Digest로 조건부 squash하고 실제 결과 커밋 검증 |
| `prepare_project_worktree` | 원본 checkout을 전환하지 않고 기존 topic 브랜치를 격리된 Worktree에 준비 |
| `inspect_project_worktree` | 관리 Worktree의 absent·partial·ready, SHA와 clean 상태 조회 |
| `finalize_project_worktree` | 검증된 병합 PR과 clean 상태를 확인하고 관리 Worktree·준비 기록을 제거; 로컬 브랜치 삭제는 명시적 선택 |
| `start_project_worktree_task` | 지정 Worktree에서 등록 검사 실행; 기존 job 조회·취소와 실행 슬롯 공유 |
| `prepare_project_worktree_assessment` | 명시적으로 검토한 평가 TOML을 관리 Worktree에 새로 작성하고 Digest 반환 |
| `commit_project_worktree_assessment` | Digest가 일치하는 새 평가 하나만 검증·Stage·서명 설정에 따라 Commit |
| `inspect_project_branch_remote` | 준비된 지정 브랜치를 기준으로 원격 refs·기준선 관계 조회 |
| `push_project_branch` | 원본을 보존하며 관리 Worktree의 관측 SHA를 동일 원격 기능 ref에 비강제 Push |
| `create_project_branch_draft_pr` | 현재 템플릿과 대상 커밋·평가로 지정 브랜치 Draft PR 게시 |
| `create_project_worktrunk` | 로컬 기준 SHA에서 보호된 경로의 새 브랜치·Worktree 생성 (실험적) |
| `prepare_project_worktrunk_main` | 기존 main Ref를 전용 `.lab/wt/main` Worktree에 안전하게 Checkout (Ref 변경 없음) |
| `merge_project_worktrunk` | 품질 증거·main 승인 게이트 구현 전까지 실행 거부 (실험적) |
| `verify_project_worktrunk_quality` | Worktrunk Source의 완전한 검사 Job·HEAD·Tree·Manifest 증거 발급 (승인과 별개) |
| `get_project_worktrunk_merge_plan` | 품질 증거·Main Worktree 상태·FF 선행조건 검증, 변경 없는 검토 계획 생성 |
| `remove_project_worktrunk` | Clean Worktree 정리 및 선택적 안전한 브랜치 삭제 (실험적) |
| `diagnose_documents` | 등록된 문서의 진단·관계 승인 대기 상태 확인 |

파일 탐색·읽기·변경은 Git·identity 없이 사용할 수 있다. 보호 파일과 symlink는 읽지 않는다.
검색 결과의 complete와 읽기의 다음 줄을 확인하고 필요한 범위만 요청한다.
입력·결과·제한·read_ref의 의미는 [Workspace 계약](docs/tools/workspace/README.md)을 따른다.

수정은 `read_workspace_file`로 필요한 구간을 읽은 뒤 같은 경로의 `read_ref`와 새 `replacement`를 전달한다.
성공하면 실제로 관측한 새 원문과 참조로 이어간다. 사용자가 중간에 파일을 수정해 `stale_version`이 반환되면
현재 내용을 다시 읽고 수정안을 재구성한다. 읽기 전용·접근 금지 범위는 [파일 접근 정책](docs/configuration.md#파일-접근-정책)으로 설정한다.

새 파일 생성·삭제는 새 `request_id`를 사용하고 응답 유실 때 같은 ID와 입력으로 결과를 다시 확인한다.
생성은 기존 경로를 덮어쓰지 않으며, 없는 부모는 `create_parents=true`일 때만 만든다.
삭제는 영구 삭제다. Git에 없는 최신 수정·새 파일의 내용을 별도로 백업하거나 복구하지 않는다.

검사는 프로젝트가 `config.toml`에 등록한 CLI와 고정 옵션으로 실행한다. 먼저 `list_project_tasks`를 확인하고,
테스트에는 구체적인 `targets` 또는 전체 실행을 뜻하는 `all_tests=true`를 명시한다.
기본 응답은 상태·종료 코드·선택 범위만 제공하며, 진단은 `get_project_task(job_id, include_output=true)`로 요청한다.
응답을 잃었을 때는 같은 `request_id`와 인자로 재조회하며, 실행·실패·취소·진단의 의미는 [작업 계약](docs/tools/tasks/README.md)을 따른다.

작은 개발 작업은 새 소스·테스트 생성 → 선택 테스트 실패 확인 → 구현 → format·lint·필요한 검사 → 변경 검토로 진행한다.
format 뒤에는 파일을 다시 읽어 새 `read_ref`를 사용한다. 새 파일은 읽기로 검토할 수 있고, staged diff는 명시적으로 stage한 뒤 조회한다.
기본 완료는 검증된 working-tree 변경과 검토 가능한 diff이며, commit은 별도 요청으로 수행한다.

전체 stage는 `paths=["."]`로 명시한다. commit은 자동 stage하지 않으며 기존 staged 변경도 포함한다.
입력·결과·timeout·서명·실패 후 확인 방법은 [Git 계약](docs/tools/git/README.md)에 있다.
허용한 프로그램의 모든 간접 실행을 가두는 sandbox는 아니다.

## 개발

변경 전 [개발 규약](docs/conventions.md)을 읽고 [검사 명령](docs/testing.md#실행-경로)을 따른다.
의존성과 진입점은 `pyproject.toml`, 정확한 설치 버전은 `uv.lock`이 소유한다.
설치 진입점의 경로를 바꾼 뒤에는 `uv sync --locked --reinstall-package local-dev-mcp`로
개발 환경의 실행 스크립트를 갱신하고 서버를 재시작한다.

## 문서

- [개발 규약](docs/conventions.md): 명명·경계·타입·테스트·문서 유지와 변경 완료 조건.
- [설정과 운영 계약](docs/configuration.md): 환경 값·권한·시작 snapshot·종료 코드·진단·정리 보장.
- [Workspace 계약](docs/tools/workspace/README.md): 파일 접근·검색·읽기·수정·충돌 후 재개·읽기 참조.
- [Git 계약](docs/tools/git/README.md): 입력·명령·결과·실패 의미.
- [CLI 작업 계약](docs/tools/tasks/README.md): 선택 실행·실행 ID·취소·응답 복구·제한된 진단.
- [연결 원리](docs/design.md): CLI·MCP·Tunnel의 흐름과 import·오류·취소 처리의 이유.
- [검사와 환경](docs/testing.md): 실행 절차·fixture 자원·설치 검증의 범위.
- [B3-S 개발 DB (Podman)](infra/postgres/README.md): GitHub 영속 전달 검증을 위한 로컬 PostgreSQL 구성.
- [테스트 정책 근거](docs/research/testing-policy-research.md): 검증 계층·소유권 배치·의존 실행·품질 판단의 조사 근거.
- [CLI 작업 정책 근거](docs/research/cli-task-policy-research.md): 명령 권한·공통 환경·출력 보호의 조사 근거와 한계.
- [ChatGPT 사용 확인](tests/e2e/README.md): 외부 클라이언트에서 수행하는 선택적 수용 검증.

## 개발 통합과 릴리스

Chat에서 `get_release_plan`으로 현재 커밋의 호환성 평가와 `next_version`을 확인한다. `main`이 Clean이면 `create_project_branch`로 정확한 HEAD에서 `release/<next_version>`을 만들고, `prepare_project_release(expected_head, expected_plan, expected_version)`으로 버전·Lock·릴리스 기록을 준비한다. 쓰기 준비에는 `config.toml`의 `uv.release` 명시 허용이 필요하다. 변경된 파일을 검토·검증한 다음 기존 Git 도구로 stage·commit·main squash 통합한다. Prepare는 commit·tag·push·publish를 하지 않으며 중간 실패 시 파일이 일부 변경됐을 수 있으므로 status를 먼저 확인한다. 자세한 절차는 [릴리스 MCP 흐름](docs/tools/release/README.md#chat-mcp-릴리스-흐름)을 따른다.

작업 종류 중심 브랜치·평가 기록·squash·누적 릴리스는 [개발 규약](docs/conventions.md#git-workflow와-호환성-평가)을 따른다.
누적 최대 영향과 원인은 `uv run python -m local_dev_mcp.tools.release plan`으로 조회한다.
설정·평가 형식·prepare·실패 후 조치는 [릴리스 도구 계약](docs/tools/release/README.md)을 참조한다.
원격 보호 규칙은 저장소 조건이 지원할 때만 추가할 수 있으며 로컬 개발의 필수 준비가 아니다.
