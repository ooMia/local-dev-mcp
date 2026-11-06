# 등록된 CLI 작업 실행 계약

프로젝트가 등록한 lint·format·test CLI를 선택 workspace에서 실행한다. 설치된 Ruff·pytest·다른 언어의 runner를
고정 argv로 사용할 수 있으며, 호출자가 임의 명령·shell 문자열·자유 CLI 옵션을 전달하는 기능은 제공하지 않는다.
recipe·권한·공통 환경·시작 모드는 [설정 계약](../../configuration.md#cli-작업-설정)을 따른다.

## 도구와 선택 실행

| 도구 | 입력 | 역할 |
| --- | --- | --- |
| `list_project_tasks` | 없음 | 등록 recipe와 각각의 준비 상태 조회 |
| `start_project_task` | `task`, `request_id`, `targets=null`, `all_tests=false` | 작업 제출, 짧은 완료 결과 또는 실행 ID 반환 |
| `get_project_task` | `job_id`, `include_output=false` | 상태 조회; 명시적으로 보관된 진단 포함 |
| `cancel_project_task` | `job_id` | 취소 요청 후 직접 프로세스 정리 결과 조회 |

먼저 목록의 `ready`, `reason`, `next_action`을 확인한다. `permission_denied`·`executable_unavailable`·`environment_invalid`는
해당 recipe만 사용 불가로 만들며, 설정·설치 수정 후 재시작한다. 목록에는 argv나 환경값을 반환하지 않는다.

test 작업은 대상과 전체 실행 중 하나를 명시한다.

```json
{"task":"unit","request_id":"edit-42-unit-1","targets":["tests/test_settings_values.py::test_empty_allowlists_remain_empty"]}
```

```json
{"task":"integration","request_id":"edit-42-integration-1","all_tests":true}
```

`targets`는 기본 전체 대상을 대체한다. 생략·빈 목록·대상과 `all_tests=true` 동시 지정은 거부하고,
선택 오류나 일치 없음에서 전체 실행으로 자동 확대하지 않는다. lint·format은 고정 recipe만 사용하므로 두 선택 필드를 받지 않는다.

공개 targets는 1..32개, 각 1..1024자다. workspace 상대 경로와 `파일::클래스::함수[매개변수]` 같은 node ID를
각각 한 토큰으로 전달한다. 절대 경로·외부 경로·상위 이동·제어 문자·`-` 또는 `@`로 시작하는 인자는 거부한다.
파일 경로 부분의 접근 권한을 확인하되 실제 테스트 존재와 node ID의 의미는 CLI가 판단한다.
허용 경로의 조상으로 이동하는 권한만으로 그 조상 전체를 검사할 수는 없다. 이름이 `-`·`@`로 시작하는 파일은
`./-file.py`·`./@file.py`처럼 명시하며, 정규화 후에도 그 경로 접두사를 유지한다.
매개변수 안의 공백·중간 `-`는 shell 문자열로 다시 해석하지 않는다. runner별 selector 의미를 확인해야 한다.

unit의 `-n 0`, integration의 `-n 4` 등 실행 옵션은 recipe가 소유한다. 대상 프로젝트의 `level="e2e"`는
그 프로젝트의 runner 분류이며 실제 ChatGPT에서 local-dev-mcp를 사용했다는 증거가 아니다.

## 제출·조회·취소와 복구

새 실행에는 새 `request_id`를 사용한다. 허용 문자는 ASCII 영숫자·점·밑줄·콜론·하이픈이며 최대 128자다.
응답을 잃었을 때 같은 서버에 같은 ID와 같은 작업·선택 인자를 다시 보내면 기존 job을 반환하고 재실행하지 않는다.
같은 ID로 다른 입력을 보내면 거부한다. 이후 `get_project_task(job_id)`로 관측한다.

서버는 최대 1초간 완료를 기다린다. 이 안에 끝나면 완료 결과를, 계속 실행 중이면 `state="running"`과 `job_id`를 반환한다.
시작·기본 조회·취소 응답은 진단 원문을 생략한다. 실패 원인이 필요하면 같은 job을 다음처럼 조회한다.

```json
{"job_id":"<job-id>","include_output":true}
```

이 조회는 작업을 다시 실행하지 않고 이미 수집·마스킹한 출력만 반환한다.
클라이언트 호출이 중단됐다는 이유로 제출된 job의 종료나 무변경을 추정하지 않는다. 조회하거나 명시적으로 취소한다.

workspace·서버당 한 작업만 실행하며 대기열은 없다. 다른 작업이 실행 중이면 새 제출을 거부한다.
작업 중에는 입력 파일 편집을 피하고 종료 후 현재 상태를 읽는다. IDE·외부 도구와 공유하는 OS 배타 잠금은 제공하지 않는다.
직접 종료를 확인하지 못한 `cleanup_unconfirmed` 작업도 새 실행을 차단하며 로컬 상태 확인과 재시작을 안내한다.

완료 결과는 최대 128개·완료 후 30분간 서버 메모리에 보관한다. `request_id` 장부는 결과가 퇴출돼도 최대 4096건을 유지해
같은 제출을 새 실행으로 바꾸지 않는다. 결과가 만료된 ID의 재제출은 오류이며 장부가 가득 차면 새 시작을 거부한다.
필요한 결과와 파일 상태를 확인한 후 재시작한다. 재시작하면 결과·장부가 사라지므로 자동 replay나 영구 중복 방지를 보장하지 않는다.
ID는 인증된 Chat 대화 구분이 아니며, 다른 서버·workspace로의 결과 복구도 제공하지 않는다.

## 결과와 다음 행동

| state | 의미 |
| --- | --- |
| `running` | 실행 중. 조회하거나 취소 요청 |
| `succeeded` | 직접 프로세스가 exit code 0으로 종료 |
| `failed` | 비정상 종료 또는 실행 실패. 진단과 현재 파일 확인 |
| `timed_out` | 시간 예산 소진, 직접 종료 확인 |
| `cancelled` | 취소 처리, 직접 종료 확인 |
| `start_failed` | 프로세스를 시작하지 못함. 준비·실행 경로 확인 |
| `cleanup_unconfirmed` | 직접 프로세스 종료를 확인하지 못함. 로컬 프로세스·파일 상태부터 확인 |

결과는 `job_id`, `request_id`, `task`, `kind`, `level`, `targets`, `all_tests`, `state`, `exit_code`,
`termination_confirmed`, UTC `started_at`·`finished_at`, `stdout`, `stderr`, `output_included`, `output_complete`, `output_reason`,
`may_have_changed_files`, `next_action`을 제공한다. 종료 코드는 CLI 결과이며 수집·통과 테스트 수를 추론하지 않는다.
진단이 보류되거나 선택한 범위가 제한된 경우를 포함해, exit code 0만으로 전체 회귀 성공을 주장하지 않는다.

`output_included=false`이면 이번 응답의 stdout·stderr는 빈 문자열이다. 출력 수집을 중단했다거나 실제 출력이
비어 있다는 뜻은 아니다. `include_output=true`로 조회한 `output_included=true` 결과의 빈 문자열은 현재 보관된
해당 채널 내용이 비어 있음을 뜻한다. `output_complete`·`output_reason`은 표시 선택과 무관한 수집·마스킹·잘림 상태다.
실행 중에는 아직 수집 중인 결과임을 함께 확인한다.

format뿐 아니라 lint·test도 cache·report·snapshot 등을 만들 수 있으므로 모든 작업은 부수 변경 가능성이 있다.
직접 파일 수정의 읽기 전용·ignore 정책으로 외부 프로그램의 모든 쓰기를 차단하지 않는다.
비정상 종료·timeout·취소는 rollback하지 않으며, 변경 여부를 확인한 뒤 의도적인 재실행에 새 request ID를 사용한다.
완료된 job을 취소하면 원래 결과를 유지한다. 정상 서버 종료 때 실행 중 작업을 취소하고 라이브러리 정리를 기다린다.
정리 확인은 직접 프로세스에 한정하고 runner가 만든 모든 후손·외부 서비스·호스트 강제 종료까지 확대하지 않는다.

## 진단과 출력 보호

stdout·stderr는 각각 UTF-8 최대 64 KiB의 제한된 진단을 제공한다. 알려진 비공개 값의 처리는 저장·공개 전에 수행하며,
바이트 청크 경계에서 나뉜 값도 이어서 검사한다. raw 출력 파일을 따로 저장하거나 임의 traceback을 응답에 복사하지 않는다.
상세 조회도 이 보관 상한 밖의 출력·만료된 결과·runner가 생산 단계에서 숨긴 출력을 복구하지 못한다.
기본 조회가 진단을 생략해도 수집·마스킹은 계속되며, 상세 조회 옵션은 실행 요청의 지문이나 재실행 여부를 바꾸지 않는다.

Catalog는 [프로젝트 환경의 분류](../../configuration.md#프로세스-환경)를 시작 시 한 번 적용한다.
비공개 값의 정확한 문자열과 지원하는 JSON escape·URL encoding·Base64 표현을 `[REDACTED]`로 치환한다.
임의 중첩 encoding·일부 문자열·알려지지 않은 외부 credential·새로 파생된 값까지 탐지한다는 보장은 아니다.
환경값의 사용 경로를 함수마다 표시하거나 전이적으로 추적하는 방식은 사용하지 않는다.

| 조건 | 출력 처리 |
| --- | --- |
| 비공개 값이 1..3자 | 광범위 오탐 대신 진단 전체 보류. 비밀이 아닌 운영 값이면 공통 `public_names`에서 분류 |
| Catalog가 값 256개·값당 8 KiB·전체 변형 1 MiB 또는 4096개 상한 초과 | 진단 보류, 실행 상태·안전한 원인 유지 |
| 비정상 UTF-8 | 해당 채널의 진단 보류 |
| 채널 출력 상한 초과 | 제한된 진단과 `output_complete=false`, `output_reason` 반환 |

진단 보류는 `[diagnostics withheld]`와 이유를 반환하고 프로세스 실행 결과와 구분한다.
`public_names`는 출력 분류이며 추가 실행 권한·환경 전달 허용 목록이 아니다.
직접 파일 읽기·Git patch의 일반적인 비밀 탐지·마스킹으로 이 기능의 범위를 확대하지 않는다.
