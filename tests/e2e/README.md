# 실제 ChatGPT E2E

이 문서는 연결 환경이나 실제 사용자 흐름을 확인할 때 외부 클라이언트에서 수행하는 선택적 수용 절차다.
준비 담당자와 ChatGPT 세션이 호출·결과 활용을 확인하며 서버가 스스로 연결 여부를 판정하는 절차가 아니다.
서버 기능의 자동 검증은 [개발 흐름](../system/test_development_cycle.py)과 [Git 의존 시나리오](../system/test_dag_workflow.py)가 담당한다.
적용 범위는 [테스트 안내](../../docs/testing.md#서버-기능과-외부-클라이언트-검증)를 따른다.

## 준비

1. 실제 작업 저장소와 분리한 테스트 전용 Git workspace를 준비한다. 기존 사용자 파일·index·키를 사용하지 않는다.
2. [설정 계약](../../docs/configuration.md)에 따라 그 workspace의 `config.toml`과 `.env`를 준비한다.
   기본 시나리오는 `GIT_COMMIT_SIGN=false`와 테스트 identity를 사용한다. credential은 결과에 기록하지 않는다.
3. `data.txt`에 `original` 한 줄을 기록한 baseline commit을 준비하고 baseline SHA를 보관한다.
   `config.toml`과 `.env`는 테스트 저장소의 exclude에 넣는다. `data.txt`는 직접 수정이 허용된 경로로 둔다.
4. `local-dev-mcp --workspace /path/to/test-workspace connect`로 준비한 workspace를 지정하고 ChatGPT에 연결한다.
   Tunnel 시작 메시지는 연결 성공 판정에 사용하지 않는다.
5. 작은 개발 작업에는 대상 프로젝트의 unit·format·lint recipe를 등록하고 `list_project_tasks`에서 준비 여부를 확인한다.

## 작은 개발 작업

일반 ChatGPT 세션에서 새 소스와 테스트를 작성해 실제 변경을 완료한다. 각 파일 변경과 작업 제출에는 의도를 식별할
새 요청 ID를 사용하고, 응답을 잃었을 때만 동일한 ID·입력을 재전송한다.

| 단계 | 실제 Chat 호출과 관측할 계약 |
| --- | --- |
| 기준 확인 | readiness·등록 recipe·status로 대상 workspace와 기존 사용자 변경을 확인한다. |
| 새 테스트·소스 | `create_workspace_file`로 필요한 UTF-8 소스와 테스트를 만든다. 없는 부모가 필요할 때만 `create_parents=true`를 지정한다. 기존 경로 충돌은 읽고 판단하며 덮어쓰지 않는다. |
| 실패 재현 | `start_project_task`로 작성한 테스트만 선택한다. 기본 응답의 실패 상태·종료 코드를 확인하고 `get_project_task(include_output=true)`로 같은 실행의 마스킹 진단을 확인한다. |
| 구현 | 생성 결과 또는 새 읽기의 `read_ref`로 소스를 수정하고 실제 관측 결과를 확인한다. |
| 검사 | format·lint·선택 unit과 필요한 관련 검사를 순서대로 실행한다. Format 뒤 편집할 파일은 다시 읽는다. 기본 polling은 `output_included=false`이며 상세 조회가 재실행을 만들지 않아야 한다. |
| 선택적 파일 정리 | 불필요한 파일은 현재 내용을 읽은 뒤 `delete_workspace_file`로 단일 삭제한다. 자체 복구·휴지통은 없으므로 원본 보존이 필요한 자료를 쓰지 않는다. |
| 변경 검토 | status·읽기·diff로 의도한 변경만 있는지 확인한다. 새 파일의 staged diff가 필요하면 해당 경로를 명시적으로 stage한다. 검증된 변경을 기본 완료로 두고 commit은 별도 요청으로 수행한다. |

생성 또는 삭제 응답을 잃은 경우 같은 요청을 다시 보낸다. 그 사이 사용자가 같은 경로에 파일을 만들었더라도
이전 receipt 조회만 수행하고 새 파일을 덮어쓰거나 다시 삭제하지 않아야 한다.
실제 Chat 호출·실행 대상·결과·최종 파일 확인이 이 세션에서의 완료 증거다. 로컬 자동 테스트를 이 기록으로 대체하지 않는다.

## Git 기록까지 확인하는 선택 시나리오

시나리오는 하나씩 직렬 실행한다. 아래 같은 단계의 읽기 호출은 준비 조건을 공유하며 독립적으로 실행할 수 있다.

| 노드 | 선행 성공 조건 | 실제 Chat 호출과 관측할 계약 |
| --- | --- | --- |
| 시작 | 테스트 workspace·Tunnel 준비 | Chat에서 `get_tool_readiness`를 호출한다. 대상 workspace와 `startup_snapshot_only`, 필요한 도구의 ready를 확인한다. |
| 파일 읽기 | 시작 | `read_workspace_file(path="data.txt")`가 `original` 원문, version, read_ref를 반환한다. |
| 파일 수정 | 파일 읽기 | `replace_workspace_file(path="data.txt", read_ref=읽은 참조, replacement="updated\n")`를 호출한다. outcome=applied, changed=true와 새 관측 원문·참조를 확인한다. |
| 수정 후 읽기 | 파일 수정 | 다시 읽어 `updated` 원문과 수정 결과의 version_after가 일치함을 확인한다. |
| 변경 확인 | 파일 수정 | `get_project_status`의 HEAD가 baseline이고 `data.txt`만 working-tree 수정임을 확인한다. status·diff의 complete와 의도한 patch를 확인한다. |
| stage | 수정 후 읽기와 변경 확인 | `stage_project_paths(paths=["data.txt"])`를 호출한다. |
| staged diff | stage | `get_project_diff(staged=true, paths=["data.txt"])`로 승인할 patch를 확인한다. |
| staged status | stage | `get_project_status`에서 해당 경로가 staged이고 working-tree 변경이 없음을 확인한다. |
| commit | staged diff와 staged status | `commit_project_changes(message="ChatGPT E2E commit")`를 호출한다. success, baseline인 head_before, head_after, head_observed를 확인한다. unsigned 결과는 signing_requested=false와 signature 관련 null이다. |
| HEAD read-back | commit | `get_project_head`가 head_after와 같아야 한다. |
| status read-back | commit | `get_project_status`가 같은 HEAD와 빈 entries를 반환해야 한다. |
| 외부 read-back | commit | 준비 담당자가 테스트 workspace의 `git show HEAD:data.txt`와 commit 메시지·SHA를 확인한다. 이 외부 확인을 Chat 도구 호출로 기록하지 않는다. |
| 거부 확인 | 시작 | `read_workspace_file(path=".env")` 및 workspace 밖 경로를 요청한다. 거부 응답에 내용·credential이 노출되지 않아야 한다. |

각 노드의 호출 이름·안전한 입력·실제 응답·결과·선행 관계를 증거에 남긴다. 실패한 노드의 후속은 실행하지 않고
직접 차단과 선행 경로 미충족을 구분한다. 독립 가지의 결과는 별도로 기록하며, 하나의 성공을 전체 성공으로 확대하지 않는다.
쓰기 오류가 나면 재시도 전에 HEAD와 status로 실제 상태를 확인한다.

별도 충돌 확인에서는 읽기 후 준비 담당자가 같은 파일을 수정하고 기존 참조로 교체를 요청한다.
`stale_version`으로 거부되고 사용자 변경이 남아 있어야 한다. 이후 Chat에서 현재 내용을 다시 읽고 수정안을 재구성해
새 참조로 작업을 재개한다. `applied_unverified`나 응답 유실 때는 같은 교체를 반복하지 않고 실제 파일부터 조회한다.

## 재연결과 정리

연결을 종료한 뒤 같은 테스트 workspace로 재연결하고 Chat에서 readiness·HEAD·status·파일을 다시 조회한다.
Git commit과 파일은 유지돼야 한다. 이전 read_ref의 재사용을 기대하지 않으며 새 읽기 결과를 사용한다.
재연결도 실제 호출·read-back 증거가 있을 때만 통과로 기록한다. 완료 후 시나리오가 준비한 프로세스와
테스트 workspace만 정리한다. 사용자 credential·공유 GPG 서비스는 정리 대상으로 삼지 않는다.
