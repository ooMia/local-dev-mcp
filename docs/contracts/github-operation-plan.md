# GitHub Sealed Plan — Structure and Experimental Persistence

B3-S에서 Q70~Q76에 따라 실행 전 고정할 **Plan 구조**와 PostgreSQL
**append-only 영속화 API**를 정의한다. [영속 수락 계약](github-durable-delivery.md)의
Receipt·Job, Claim·Immutable Intent·실제 외부 변경은 별도 경계다.

## 불변 구조

[`OperationNode`](../../src/local_dev_mcp/tools/gh/delivery/operation_plan.py)는
`operation_id`, `consumer_id`, `operation_type`, `contract_version`,
`depends_on`을 담는 불변 값이다. `PlanStructure`는 Operation tuple의 순서와
`structure_version=b3s-plan:1`을 보존한다. 중복 ID·의존성, 누락된 참조,
순환 의존성, 빈 계획과 잘못된 식별자를 거부하며, 의존성 그래프를 자동 재정렬하지
않는다. Canonical UTF-8 bytes와 SHA-256 Digest는 구조 지문일 뿐 실행 허가가 아니다.

구조는 현재 GitHub 상태, Input/Effect, Immutable Intent, Checkpoint Output,
원본 Payload, 승인이나 Retry Safety를 포함하지 않는다.

## 실험적 PostgreSQL 영속화

- [`PLANS_DDL`](../../src/local_dev_mcp/tools/gh/delivery/schema.py)을
  **소유자가 명시적으로** 별도 PostgreSQL schema에 설치해야 한다.
  `github_sealed_plans`는 기존 Receipt의 `event_id`를 Primary Key이자 FK로
  참조하며 구조 Version, Digest, **Canonical bytes**, 봉인 시각을 저장한다.
  기존 Receipt·Procrastinate 테이블은 변경하지 않는다.
- DB Trigger가 기존 Plan 행의 `UPDATE`와 `DELETE`를 차단한다.
  이는 정상 애플리케이션 DML에서의 불변성이지 DB Owner/Superuser 또는
  백업·마이그레이션 작업으로부터의 절대적 변경 방지 보장은 아니다.
  운영 DB 적용, 파괴적 조치, 변경·정리 정책은 별도 검토 대상이다.
- [`seal_plan_structure`](../../src/local_dev_mcp/tools/gh/delivery/sealed_plan.py)은
  IDLE autocommit 연결과 **Job이 연결된 기존 Receipt**를 요구한다.
  최상위 트랜잭션 안에서 최초 INSERT를 시도하고 Primary Key로 동시
  수락을 직렬화한다. 최초 Commit 완료 시 `sealed`,
  동일 구조·동일 Canonical bytes 재확인 시 `existing`을 반환한다.
  기존 구조와 다르면 `PlanConflictError`로 거부하며, 기존 행을 갱신하지 않는다.
- `load_sealed_plan`은 **읽기 전용**이며 저장된 Version, 원본 bytes의
  SHA-256뿐 아니라 정규화된 JSON 구조도 다시 검증한다. 중복 JSON 키,
  예상 외 필드, 비정규 직렬화, 누락·순환 의존성, 미지원 Plan Version과
  1MiB를 넘는 구조는 **Digest가 유효하더라도** `PlanIntegrityError`로 거부한다.
  동일 계획의 재봉인 조회에서도 이 검사를 수행한다. 누락은 `None`이며
  본문 Payload·비밀은 기록하지 않는다.
- DB Schema는 API 호출·import 시 자동 설치/마이그레이션되지 않는다.
  Receipt/Job 수락이나 ASGI Receiver가 Plan을 자동 봉인하지 않는다.
  아래의 명시적 Worker가 Claim 이후 이 영속화 함수를 호출한다.

## Immutable Intent — 이벤트·Operation 단위의 실험적 영속화

사용자 결정 A안에 따라 Intent의 영속 식별자는 **`(event_id, operation_id)`**다.
Claim Generation은 이 Intent를 최초 작성한 실행 시도 증거로 보존하지만,
재시도마다 별도 Intent를 생성하거나 기존 Intent의 내용·버전을 교체하지 않는다.

- [`OperationIntent`](../../src/local_dev_mcp/tools/gh/delivery/operation_intent.py)는
  Plan의 Operation 명세, 명시적인 Input/Effect/Precondition/Postcondition SHA-256
  Digest, Retry Safety Rule·Probe Contract 식별자, 소비한 Output의 명시적 lineage를
  포함하는 불변 값이다. `b3s-intent:1`의 canonical bytes와 Digest를 제공한다.
  원본 Webhook Payload, GitHub API 인자, 인증정보 및 실제 상태 검증 증거는 저장하지
  않으며 Caller가 제공한 Digest를 실행 가능한 계약으로 신뢰하지 않는다.
- [`INTENTS_DDL`](../../src/local_dev_mcp/tools/gh/delivery/schema.py)은 별도
  `github_operation_intents` 테이블과 UPDATE/DELETE 차단 Trigger를 제공한다.
  `(event_id, operation_id)`를 Primary Key로, 최초 기록한
  `(event_id, created_generation)`을 Claim Generation의 Foreign Key로 둔다.
  기존 스키마 변경이나 자동 설치·마이그레이션·Worker 기동은 없다.
- [`record_operation_intent`](../../src/local_dev_mcp/tools/gh/delivery/immutable_intent.py)는
  IDLE autocommit 연결에서 Receipt→Procrastinate Job을 순서대로 잠그고,
  현재 Job 소유권·최신 started 이벤트·Claim Generation·Sealed Plan을 **같은
  트랜잭션** 안에서 다시 대조한다. 지원되는 Plan에 속한 Operation만 기록하며
  다른 Digest·Contract·Input/Effect로 같은 키를 재사용하면 충돌로 거부한다.
  동일한 canonical Intent는 다른 Claim Generation에서도 기존 기록을 반환한다.
- 후속 Operation은 아래의 Checkpoint 및 필드 단위 lineage를 대조한 경우에만
  Intent를 영속 기록한다. 필수 Checkpoint 또는 lineage가 없으면 Hold한다.
  반대로 **의존성이 없는 Root Operation은 lineage 인자를 허용하지 않는다.**
  빈 바인딩 목록이라도 전달되면 Intent 저장 전에 Hold하여, Root에
  부적절한 lineage 행이 남지 않도록 한다.
  `load_operation_intent`는 저장된 Version·Digest·canonical bytes뿐 아니라
  봉인된 Plan의 무결성·Digest 연결·Operation 명세 일치까지 읽기 전용으로
  검증한다. 종속 Intent는 원자적으로 기록된 lineage와 선행 Checkpoint를
  추가 검증하며, 필수 증거 테이블이 미설치되거나 lineage가 누락·손상됐으면
  무결성 오류로 거부한다. Root-only 조회에서는 선택적 증거 테이블 설치를
  강제하지 않지만, 테이블이 있는 경우 Root에 불필요한 lineage 행도 거부한다.
  불일치 또는 손상은 조회 성공으로 취급하지 않는다.

## Operation Checkpoint/Output과 명시적 lineage — 실험적 영속화

Q71~Q73에 따라 Operation의 부분 결과를 독립적으로 보존한다.

- [`OperationCheckpoint`](../../src/local_dev_mcp/tools/gh/delivery/operation_checkpoint.py)는
  `b3s-checkpoint:1`의 canonical bytes와 Digest를 사용한다. Operation ID,
  원본 Intent Digest, Output Schema Version, 필드별 Output Digest,
  Verification Contract와 Evidence Digest를 포함하며 실제 GitHub 응답 본문,
  인증정보·원본 Webhook Payload를 저장하지 않는다.
- [`CHECKPOINTS_DDL`](../../src/local_dev_mcp/tools/gh/delivery/schema.py)은
  **명시적 설치** 대상인 두 개의 별도 append-only 테이블을 추가한다.
  `github_operation_checkpoints`의 식별자는 `(event_id, operation_id)`이며
  최초 기록한 Claim Generation과 기존 Immutable Intent를 FK로 참조한다.
  `github_operation_lineages`도 `(event_id, operation_id)`를 PK로 사용하고
  종속 Intent를 FK로 참조한다. 양쪽 UPDATE/DELETE는 DB Trigger가 차단한다.
- [`record_operation_checkpoint`](../../src/local_dev_mcp/tools/gh/delivery/checkpoint_store.py)는
  Receipt와 Job을 잠근 트랜잭션 안에서 현재 Claim Generation,
  Sealed Plan, 기존 Intent 및 Operation 계약 일치를 재검증한다. 종속 Operation은
  원자적으로 기록된 필드 단위 lineage와 선행 Checkpoint까지 검증하고,
  누락·손상되면 새 Checkpoint 기록과 동일 증거의 재호출을 모두 Hold한다.
  Root Operation에 부적절한 lineage 행이 있어도 Hold하지만 선택적 테이블이
  없는 Root-only 계약에는 추가 설치를 요구하지 않는다. 동일 증거의
  중복은 `existing`, 상이한 증거는 Conflict다. 기존 Checkpoint의 기록
  Generation이 Intent의 최초 작성 Generation보다 앞서는 손상된 기록도
  `existing`으로 수락하지 않는다. 이전 Generation의 결과를
  덮어쓰거나 부재한 Intent를 성공으로 해석하지 않는다.
- [`OperationLineage`](../../src/local_dev_mcp/tools/gh/delivery/operation_lineage.py)는
  `b3s-lineage:1`에 따라 각 입력 필드의 **source Operation·Checkpoint Digest·
  source field·field Digest·Output Schema Version·target field·binding version**을
  불변으로 고정한다. [Intent 영속화](../../src/local_dev_mcp/tools/gh/delivery/immutable_intent.py)는
  종속 Operation의 모든 의존관계와 Checkpoint·Input/Output 필드를 교차 검증한 뒤
  **Intent와 lineage를 동일한 최상위 트랜잭션에서 커밋**한다. 검증은
  직접 참조한 Checkpoint에서 종료하지 않고 Sealed Plan DAG의 선행 Intent·
  Lineage·Checkpoint를 Root까지 확인한다. 중간 종속 Lineage가 누락·손상되거나
  Root에 잘못된 Lineage가 있으면 하위 Operation도 Hold하며, 공유된 상위
  의존성은 한 번만 순회한다. 재시도 시 기존 lineage를 비교하며
  누락·변경은 Hold/Conflict로 거부한다.
  독립 Root Intent의 기존 저장 형식 `b3s-intent:1`은 변경하지 않는다.
- `load_operation_checkpoint`와 `load_operation_lineage`는 읽기 전용이며
  canonical bytes·Digest와 참조한 Intent·Plan·source Checkpoint의 일치성을
  확인한다. 특히 lineage 조회는 종속 Intent의 저장된 구조 Digest가
  **Sealed Plan Digest와 일치하는지** 확인하며 Root Operation의
  잘못된 lineage 행은 무결성 오류로 거부한다. Checkpoint 조회도
  연결된 Intent의 필수 lineage·선행 Checkpoint를 다시 검증하며,
  사후 누락·손상된 종속 lineage나 Root의 부적절한 lineage가 있으면
  무결성 오류로 거부한다. 선택적 증거 테이블이 없는 Root-only 조회는
  계속 허용한다. 두 조회 경로 모두
  **Checkpoint의 기록 Generation이 원본 Intent의 최초 작성 Generation보다
  앞서는 경우** 무결성 오류로 거부한다. 종속 Intent 기록 시 선행 Checkpoint
  검증에도 동일한 시간순 조건을 적용한다.
  저장 기록은 **호출자가 제출한 검증 증거의 영속성**일 뿐
  해당 GitHub 변경이 실제 성공했음을 독자적으로 증명하지 않는다.

실제 검증 주체의 신뢰성, 외부 상태 재조회, Worker의 Intent→Effect→Checkpoint
순서·실패 복구·외부 동시성 제어는 여전히 별도 계약이며, 미확인 원격 결과를
Checkpoint 성공이나 자동 재실행 승인으로 바꾸지 않는다.

## Consumer-owned 버전 계약의 정적 호환성 — Q74~Q76

[`ConsumerOperationContract`](../../src/local_dev_mcp/tools/gh/delivery/operation_contracts.py)는
각 Consumer가 선언하는 명시적 `(consumer_id, operation_type, contract_version)`
계약이다. `execute / probe / decode / verify` 함수의 **존재**, 원격 작업의
retry/probe 계약, Consumer별 동시성 보호 요구·구현 수준 및 테스트 증거 Digest를
함께 명시한다. 모든 기능은 정확한 버전으로 등록되어야 한다.

- Q74=C: Consumer의 `ConcurrencyProtection`에서 요구한 보호 수준과
  구현 선언을 정확히 대조한다. 수준의 강약 관계는 공통 코드가 추측하지 않으며
  guard 누락·수준 불일치는 `concurrency_unverified` Hold다.
- Q75=A: 보호 수준의 소유자는 Consumer 코드다. 동일 Consumer·Operation에서
  요구 또는 구현 수준이 바뀌면 동일 Contract Version의 재사용을 거부한다.
  기존 Immutable Intent의 계약 버전을 자동 교체하지 않는다.
- Q76=B: `ContractCatalog`는 등록된 버전만 인식한다. 완전한
  `execute / probe / decode / verify` 중 하나라도 빠지거나 미등록 과거
  버전이면 `unsupported_contract` Hold다. 이전 버전을 최신 구현에
  묵시적으로 매핑하거나 재해석하지 않는다.
- `inspect_plan`은 Sealed Plan 전체와 제공된 Intent의 Retry/Probe 계약을
  정적 검토한다. **어떠한 Hook도 호출하지 않으며** 저장·실행·Retry를 하지
  않는다. `compatible`은 등록된 코드 형상의 일치이지 외부 실행 허가가 아니다.

### 첫 GitHub Issue 읽기 Consumer — 명시적 바인딩

[`issue_read_consumer.py`](../../src/local_dev_mcp/tools/gh/delivery/issue_read_consumer.py)는
실제 GitHub REST `GET /repos/{owner}/{repo}/issues/{number}`의 **읽기 전용**
`github_issue/read_current/read:v1` 구현이다. 인증 토큰·원문 응답·HTTP
인자는 Intent에 저장하지 않는다. Receipt에서 복구한 `VerifiedTarget`과
호출자가 명시적으로 주입한 Transport를 **특정 Root Intent에 바인딩**하며,
최소 Envelope의 Input Digest가 불일치하면 네트워크 호출 전에 거부한다.
실행·Probe·Decode·Verify·읽기 전용 Guard는 계약 Hook을 제공하지만
`ContractCatalog`에 기본 등록하거나 Worker에서 자동 호출하지 않는다.

- 기본 Transport는 표준 라이브러리 HTTPS만 사용하고 `api.github.com`에
  한정된 GET, 10초 timeout, 1MiB 응답 상한, Redirect/비-200 거부,
  명시적 토큰 주입 및 GitHub API Version `2026-03-10`을 사용한다.
- Decode는 Issue ID·번호·Repository URL·Issue/PR 구분·상태·시각·Label
  형식을 검증하고 최소 필드 Digest와 응답 Digest만 Checkpoint에 담는다.
  공유 strict JSON decoder는 **응답에서 사용하지 않는 추가 필드까지** 중첩
  중복 키·NaN/Infinity·비정상 UTF-8을 거부하며, 실패는 `invalid_issue_response`
  Hold로 분류한다. 이 동작은 공통 decoder 추출 후에도 단위 회귀 테스트로 고정한다.
  Verify는 **새로 GET한 동일 응답 Digest/필드**와 일치할 때만 성공한다.
  비교 사이 상태 변화나 응답의 부가 필드 변경도 보수적으로 불일치 처리한다.
- `read_only_get:v1` Guard는 대상에 묶인 Intent를 확인한다. 원격 상태를
  고정하거나 외부 쓰기 동시성을 보호한다는 보장이 아니며, 반환된 Issue
  응답만으로 원래 Webhook의 `repository_id`까지 재검증하지는 않는다.
  제출한 테스트 Evidence Digest도 현재 정적 계약 검사의 선언이지
  실환경 동작 검증이나 실행 권한이 아니다.
- 단위 테스트는 Fake Transport/HTTP 연결을 검증한다. 격리 PostgreSQL
  통합 테스트는 Fake GET 응답→Decode/Verify→기존 Intent·Checkpoint
  영속화→읽기 전용 Recovery 관측의 결합을 검증한다. 실제 GitHub 접근 및
  운영 환경의 기본 등록·자동 복구는 별도 단계다.

### A안 — read_current:v1의 명시적 Procrastinate Worker 연결

[`register_issue_read_worker_task`](../../src/local_dev_mcp/tools/gh/delivery/issue_read_worker.py)는
호출자가 제공하는 `procrastinate.App`, PostgreSQL 연결 팩토리,
읽기 전용 Transport, 테스트 증거 Digest, Queue에 `github_issue_read_current_v1`
Task를 **명시적으로** 등록한다. Receiver에는 반환된 Task를
전달할 수 있으나, import·등록만으로 Worker를 시작하거나 외부 GET을
수행하지 않는다. `pass_context=True`, `retry=False`를 명시하고
실제 `JobContext`의 Job/Worker ID와 시도 횟수를 사용한다.

- [읽기 전용 Worker 준비 상태 검사](../../src/local_dev_mcp/tools/gh/delivery/issue_read_readiness.py)는
  기존 Receiver의 Receipt·Job Schema 검사를 재사용하고, 단일 Root
  읽기에 필요한 Sealed Plan·Claim Generation·Intent·Checkpoint,
  Procrastinate Worker/Event/Job 테이블의 필수 컬럼(일부 타입·Null 제약 포함)·
  기본키·주요 FK 및 append-only Trigger 활성 상태를 확인한다.
  `inspect_issue_read_worker_schema`는 idle autocommit 연결을 받고
  `inspect_issue_read_worker_database`는 호출자 제공 연결 팩토리를 사용한다.
  둘 다 `SchemaReadiness(ready, issues)`를 반환하며 DDL·Job·GET을 실행하지 않는다.
  누락/손상 상태를 진단할 뿐 실제 Worker 실행 권한을 부여하거나
  자동 기동을 수행하지 않는다. 일부 DDL·제약·Trigger 함수 본문·DB 권한,
  session_replication_role 및 검사 이후 Schema 변경까지 인증하지 않는다.
  [격리 PostgreSQL 테스트](../../tests/tools/gh/test_issue_read_readiness.py)는
  완전한 Schema와 컬럼·FK·불변 Trigger·시작 이벤트 유일성·이벤트 테이블
  결손을 각각 검증하며, 읽기 전용 연결에서도 실행 가능함을 확인한다.
- 실행 중인 Job·Worker·최신 started 이벤트를 확인하고 Claim Generation을
  영속 기록한 뒤 **단일 Root read:v1 Plan을 봉인**한다. 이어
  기존 Intent·Checkpoint가 없을 때만 새 Intent를 커밋하며,
  Claim/Plan 관측을 재확인한 뒤 Guard→GET→Decode→Claim/Plan 재관측→
  두 번째 GET Verify→Checkpoint Commit 순서로 진행한다.
  첫 GET 사이에 중단·Claim 소유권 변경·started 이벤트 변경을 감지하면
  검증용 두 번째 GET을 실행하지 않는다. 마지막 Checkpoint 기록은
  트랜잭션 내부에서 현재 Claim과 원본 Intent를 다시 검증한다.
  검증용 GET 중에도 소유권이 바뀔 수 있으므로 재관측은
  실행 독점권이나 외부 API fencing이 아니다.
- 기존 Intent가 있으면 Checkpoint 유무와 관계없이 **GET을 재실행하지 않고**
  Hold한다. HTTP 오류, GET 중단, 응답 불일치 및 Checkpoint 커밋
  실패는 Worker 실패로 남고 Procrastinate의 자동 재시도는 비활성화된다.
  재시작·수동 재처리의 안전성은 별도 복구 계약 없이는 승인하지 않는다.
- `pull_request` 대상, Label POST, 기본 Task 자동 등록, 서버 기동,
  원격 쓰기와 일반적인 DAG/종속 Operation 실행은 포함하지 않는다.
  제출된 테스트 Evidence Digest는 실제 배포의 검증 보증이 아니다.
- [실제 Worker 통합 테스트](../../tests/tools/gh/test_issue_read_worker.py)는
  격리 PostgreSQL과 Procrastinate Worker, Fake GET으로 정상 Checkpoint,
  GET 실패·2회 조회 불일치·PR 대상 Hold, 무자동 재시도 및
  기존 Intent에 대한 수동 재-Claim 이후 GET 차단을 검증한다.
  첫 GET 도중 abort 요청 시 두 번째 GET 차단, Verify GET 도중 최신
  started 이벤트가 교체된 경우 Checkpoint 거부도 검증한다.
- [프로세스 종료 검증](../../tests/tools/gh/test_issue_read_worker_crash.py)은
  실제 Procrastinate Worker 자식 프로세스를 Intent 및 Checkpoint
  커밋 직전·직후에 강제 종료(`os._exit`)한다. Intent 직전에는
  Claim/Plan만 영속하여 Recovery가 `active_unresolved` Hold,
  Intent 직후·GET 전에는 Checkpoint 없이 Intent만 영속하여
  `outcome_unknown` Hold다. Checkpoint 직전에는 Fake GET 2회와
  Verify까지 완료해도 Checkpoint가 없고 `outcome_unknown` Hold다.
  Checkpoint 직후에는 별도 DB 세션에서도 커밋된 Checkpoint를 확인하고
  Recovery가 `checkpoint_observed`를 반환한다.
  모든 경우 새 Worker가 여전히 `doing`인 Job을 자동 재실행하지 않으며,
  기존 Intent가 영속한 세 경우는 테스트 전용 새 Worker/started
  이벤트를 부여해도 GET 재실행을 차단한다. 커밋 전 종료는 원격
  미실행의 독립 증명이나 자동 복구 허가가 아니다.
- [Receiver→Worker 연결 통합 테스트](../../tests/tools/gh/test_issue_read_pipeline.py)는
  격리 PostgreSQL에서 서명된 ASGI Webhook부터 Receipt·Job Commit,
  명시적으로 등록한 Procrastinate 읽기 전용 Task, Claim·Intent 및
  Checkpoint/Recovery 판정까지 연속 실행한다. 서명 오류는 DB 연결 이전에
  거부하며, 중복 Delivery는 새 Job을 만들지 않고 충돌하는 Delivery는
  `409`를 반환한다. Fake GET 성공은 `checkpoint_observed`, 실패는
  자동 재시도 없이 `outcome_unknown` Hold임을 검증한다. 이는 테스트의
  명시적 구성이지 운영 Worker 자동 등록·기동을 의미하지 않는다.
  실제 GitHub 네트워크와 운영 배포 기동은 검증하지 않는다.

### Q77=A — Issue Label 목표 상태 기반 쓰기 Consumer (실험적)

[`issue_label_consumer.py`](../../src/local_dev_mcp/tools/gh/delivery/issue_label_consumer.py)는
`github_issue/add_label/add_label:v2`의 **명시적 주입형 Consumer 계약**이다.
[GitHub 공식 Label API](https://docs.github.com/en/rest/issues/labels)의
기존 Label 추가 `POST /repos/{owner}/{repo}/issues/{number}/labels`를
목표로 하며 전체 목록 교체 `PUT`는 사용하지 않는다. 명시적 HTTPS
POST Transport와 참여 Worker 간 PostgreSQL 보호 구간을 제공하지만
**운영 Worker 자동 연결, 크래시 이후 안전한 재실행·복구는 미구현**이다.

- `read_current`의 검증된 Checkpoint와 원본 Intent를 요구하며,
  Label·최소 Envelope·선행 Checkpoint Digest를 새로운 종속 Intent에
  결합한다. `labels` 필드의 Digest·Schema를 포함한 Lineage를 원자적으로
  저장하는 기존 B3-S API와 조합한다. 원본 Label 이름은 영속 Intent나
  Output Checkpoint에 저장하지 않는다.
- Q77=A: 조회에서 목표 Label이 존재하면 `goal_satisfied`이며,
  **우리 요청이 추가했다는 실행 귀속 증명은 아니다.** 응답이 유실되어도
  `probe`는 조회만 수행한다. 목표 Label이 확인되지 않으면
  `inconclusive`로 유지하며 미실행 판정·자동 POST 재시도를 하지 않는다.
  `decode/verify`도 Label 존재를 기준으로 검증하고, 무관한 Label 변화는
  성공 판정에 필요한 동일성 조건으로 요구하지 않는다.
- `execute`는 명시적으로 제공된 `write_scope` 아래에서
  **GET→필요 시 Attempt 커밋→단일 POST→GET**을 수행한다. 쓰기 응답 유실,
  사후 목표 상태 불일치 및 관측 검증 실패는 Hold한다. 동일 바인딩
  인스턴스의 반복 실행도 거부하지만, 이 메모리 플래그는 영속화된
  재실행 방지 수단이 아니다.
- Q78=A: [`PostgresIssueLabelWriteScope`](../../src/local_dev_mcp/tools/gh/delivery/issue_label_scope.py)는
  기존 PostgreSQL 연결의 트랜잭션 단위 advisory lock을
  **Repository ID + Issue 번호**로 생성한다. 참여하는 Worker는 같은 키를
  사용하며, 락은 GET→선택적 POST→GET 전체 동안 유지된다. 락 경합은
  대기하거나 자동 재시도하지 않고 Hold한다. 잠금 상태에서 Receipt·
  실행 중인 Job·Claim Generation·Sealed Plan·저장된 Intent·Lineage·
  원본 Checkpoint 및 미기록 결과를 재검증한다.
- Q74와 Q78의 보장 범위는 **동일한 PostgreSQL 보호 구간을 사용하는
  우리 Worker 사이**에 한정된다. 다른 GitHub 사용자·앱을 차단하지 않고,
  PostgreSQL 세션 연결이 원격 요청 도중 끊기면 락이 풀릴 수 있다.
  원격 조건부 쓰기 CAS나 fencing/Exactly-once 보장은 없다.
- `GitHubIssueLabelHttpTransport`는 자격 증명을 호출자가 명시적으로
  주입하는 단일 POST 어댑터다. 공개 기본 Consumer 등록이나 Worker
  실행 경로에는 연결되지 않는다. HTTP POST 대기 중 취소되면 시작된
  동기 HTTP 요청이 종료될 때까지 보호 구간을 유지한 후 취소를 전파한다.
  HTTP 상태 오류·응답 유실·취소는 무변경 증거가 아니다.
- Q79=A: [`ATTEMPTS_DDL`](../../src/local_dev_mcp/tools/gh/delivery/schema.py)는
  선택 설치하는 append-only `github_operation_attempts` 테이블이다.
  PK는 `(event_id, operation_id)`이며 Claim Generation·기존 Intent에
  FK를 가진다. 기록에는 Origin Generation·Intent Digest·정규 최소 증거만
  저장하며 원본 Webhook·Label·토큰·HTTP 응답은 저장하지 않는다.
  **Attempt는 POST 전 커밋한 시도 가능성 증거이지, 실제 전송·성공 증거가 아니다.**
- Q79 실행 경계: 첫 DB 연결은 Q78의 advisory/Claim 잠금을 유지한다.
  서로 다른 두 번째 autocommit 연결이 별도 최상위 트랜잭션으로 Attempt를
  커밋해야만 `AttemptPermit.commit_attempt()`가 반환되어 POST가 가능하다.
  두 연결이 서로 다른 PostgreSQL Backend이고 같은 Database·현재 Schema 및
  Receipt·Intent·Checkpoint·Claim Generation·Attempt 테이블 OID를
  바라보는지 사전 대조한다. 불일치하면 외부 GET·POST 전에
  `write_scope_attempt_connection_mismatch` Hold를 반환한다. 격리
  PostgreSQL의 서로 다른 Schema 연결로 이 거부를 검증한다.
  이 확인은 다른 클러스터의 우연한 동명 객체·OID 충돌이나 이후
  Search Path/DDL 변경까지 막는 증명이 아니며, 연결 소유자는
  동일 PostgreSQL 저장소와 전용 세션을 유지해야 한다.
  사전 `guard`와 이미 목표를 충족한 GET은 Attempt를 기록하지 않는다.
  기존 Attempt가 있거나 저장 스키마·원본 Claim·Lineage가 불완전하면 Hold한다.
  Attempt 테이블의 조회 필수 컬럼·정확한 `(event_id, operation_id)` 기본키,
  원본 Intent·Claim Generation 외래키 또는 활성화된 UPDATE/DELETE 거부
  Trigger가 누락되면 쓰기 Scope는 외부 GET·POST 전에 거부하고 읽기 전용
  Recovery는 `evidence_store_unavailable` Hold를 반환한다. 격리
  PostgreSQL에서 각각의 FK 제거·Trigger 비활성화도 검증한다.
  이 최소 catalog 검사는 선언된 제약·Trigger 바인딩 및 활성 상태를
  확인할 뿐 함수 본문, 전체 DDL·권한, 세션 복제 모드 또는 동시 Schema 변경의
  안전성을 인증하지 않는다.
  Origin Generation이 최초 Intent Generation과 다르면 재실행하지 않는다.
  다른 Worker/Generation에서 최초 Attempt를 덮어쓰거나 자동 재기록하지 않는다.
- Q75/Q76: 이 보호 수준의 변경으로 Consumer Version을 v1에서 **v2로
  명시적으로 변경**했다. 기존 v1 Intent는 v2로 자동 변환하지 않는다.
  Recovery 관측은 v2의 Attempt 테이블·행·Digest를 읽기 전용으로 검증하여
  `attempt_digest`를 노출할 수 있지만, Checkpoint가 없으면 기존
  `outcome_unknown` Hold를 유지한다. 재시도나 원격 Probe 자동 실행은 없다.
- Fake GET/POST와 격리 PostgreSQL 테스트는 읽기 Checkpoint→종속
  Intent·Lineage→Label 목표 Checkpoint→Recovery 관측, 타 세션의 동일
  Issue 잠금 경합, POST 전 별도 Attempt Commit, 시도 후 재진입 거부와
  사후 Checkpoint 조회를 확인한다. 독립 Python 프로세스의 Attempt 커밋 직후
  강제 종료(`os._exit`)를 주입하고, 다른 세션에서 Checkpoint 없는
  `outcome_unknown` Hold 및 중복 실행 차단도 확인한다. Worker 중단 요청·
  소유권 변경·새 Claim Generation에서 이전 실행 주체가 차단되고,
  새 Generation도 최초 Intent Generation을 바꿔 재실행하지 못함을 확인한다.
  또한 테스트용 POST 도중 Q78 세션 연결이 끊겨 advisory lock이 해제되더라도
  별도 커밋한 Attempt가 보존되어 재진입을 거부하며 Checkpoint 없는
  Recovery는 `outcome_unknown` Hold임을 확인한다. Attempt 저장용 연결을
  실제 커밋 직후 닫고 호출 경계에 오류를 주입하거나, 커밋 후 대기 중인
  Consumer Task를 취소해도 POST가 시작되지 않고 동일 바인딩·새 DB 세션의
  재실행이 차단되는지 검증한다. 이 검증은 실제 PostgreSQL COMMIT 응답의
  네트워크 유실을 재현한 것은 아니며 원격 Exactly-once 보장도 아니다.
  테스트용 직접 Attempt 기록과 Fake POST를 사용하며 운영 Worker의
  크래시·재기동 E2E, 실제 GitHub POST·동시 외부 Actor의 경합은
  검증하지 않았다.
- T1·T2 (격리 PostgreSQL): T1은 미커밋 Attempt INSERT 직후 저장 연결을
  종료해 행이 롤백되고 POST가 시작되지 않으며, 같은 Consumer 바인딩의
  재실행이 거부됨을 확인한다. Attempt가 없는 상태의 Recovery도
  `outcome_unknown` Hold이지 미실행 확정·새 바인딩의 영속 차단은 아니다.
  T2는 독립 autocommit 연결 두 개의 동일 Attempt 기록 경쟁에서 정확히
  한 건만 커밋되고 다른 하나는 거부되며, Recovery의 Attempt Digest
  관측과 Consumer 재실행 차단을 확인한다. Q79의 추가 장애 검증은 이
  경계에서 종료하며 PostgreSQL COMMIT 응답의 실제 네트워크 유실과
  운영 Worker E2E는 별도 검증 대상으로 남긴다.

### add_label:v2 — 명시적 2단계 Procrastinate Worker (실험적)

[`register_issue_label_worker_task`](../../src/local_dev_mcp/tools/gh/delivery/issue_label_worker.py)는
호출자가 소유한 Procrastinate App·Queue·정규화 Receipt용 연결 팩토리,
GET/POST Transport, 고정 Label 목표 및 테스트 증거 Digest로
`github_issue_add_label_v2` Task를 **명시적으로** 등록한다.
`pass_context=True`, `retry=False`이며 Job args는 이전 계약대로
`{"event_id": id}`뿐이다. Import·등록으로 Worker나 외부 요청을
자동 시작하지 않는다. 등록 시 고정한 Label/Transport는 배포 간
동일하게 관리해야 하며 기존 저장 기록에서 원본 Label을 복구할 수 없다.

- Event별 최초 Plan 봉인 시 `read:v1 → add_label:v2` 두 Operation의
  구조를 함께 고정한다. 동일 Plan의 재봉인은 기존 영속 계약을 따르지만,
  기존 Intent·Checkpoint가 있으면 실행하지 않는다. 이미 `read:v1` 하나로
  봉인된 Event의 Plan은 확장·재작성하지 않으며, 기존 읽기 Worker도 변경하지 않는다.
- 실행 순서는 Claim Generation→Sealed Plan→Root Intent→두 번의 GET
  검증→Root Checkpoint 커밋→종속 Intent·lineage 동일 트랜잭션 커밋→
  Q78 보호 구간(목표 GET→필요한 경우 Q79 Attempt 독립 커밋→단일 POST→
  목표 GET)→재검증 GET→종속 Checkpoint 커밋이다.
  Root 읽기 Checkpoint가 없으면 쓰기 Intent도 기록하지 않는다.
- 시작 시 기존 Root·Label Intent 또는 Checkpoint가 있으면 **GET/POST 전에
  Hold**한다. 중단·오류 이후 새로운 Claim이 있어도 기존 기록을 재해석하거나
  GET/POST를 자동 재개하지 않는다. POST 전 Attempt가 커밋되면 응답 유실·
  사후 목표 상태 불일치·Checkpoint 실패를 원격 무변경으로 간주하지 않는다.
  이미 Label이 존재하는 경우 POST 및 Attempt 없이 목표 Checkpoint만 기록한다.
- 호출자는 Procrastinate Schema와 Receipt·Plan·Claim·Intent·Checkpoint
  (lineage 포함)·Attempt DDL을 명시적으로 준비해야 한다. Worker는
  최초 Claim/GET/Intent 기록 전에 기존 읽기 Worker Schema 검사와
  추가 lineage·Attempt 테이블의 최소 컬럼·키·FK·불변 Trigger 바인딩을
  **읽기 전용**으로 점검한다. 이는 함수 본문·권한·동시 DDL의 인증이 아니다.
  PostgreSQL 연결 두 개는 현재 Schema/Backend 일치 검사를 거치며
  `PostgresIssueLabelWriteScope`가 DB Claim·동시성 검증을 소유한다.
  테스트 증거 Digest는 실환경 승인이나 원격 fencing이 아니다.
- [격리 PostgreSQL Worker 테스트](../../tests/tools/gh/test_issue_label_worker.py)는
  실제 Procrastinate Claim과 Fake GET/POST로 정상 쓰기·목표 기충족,
  POST 응답 유실·사후 상태 불일치, 읽기 단계 중단, PR 대상 거부,
  Attempt 선커밋 및 advisory lock 유지, 실패 Job 무자동 재시도와
  수동 재-Claim 시 기존 Intent 재실행 차단을 검증한다.
- 같은 Worker 통합 테스트의 **외부 Actor 경합 5종**은 별도 비동기
  작업과 독립 PostgreSQL 세션을 사용한다. 외부 Actor는 Q78 advisory
  lock이 점유돼 협력 Worker가 동일 키를 획득하지 못함을 확인하면서도,
  잠금에 참여하지 않고 Fake GitHub 원격 상태를 변경한다.
  보호 구간의 사전 GET에서 Actor가 목표를 추가하면 Attempt·POST 없이
  성공하며, 우리 POST가 목표를 추가하지 않았어도 외부 Actor가 이후
  추가했다면 목표 상태 Checkpoint는 기록할 수 있다. 이는
  **목표 충족 증거이지 우리 요청의 효과 귀속이 아니다**(Q77).
  우리 POST 뒤 외부 Actor가 Label을 제거하면 사후 GET에서 Hold,
  최종 재검증 GET 직전에 제거하면 Q78 잠금 해제 후 실패하며 두 경우
  모두 Attempt는 남고 Label Checkpoint는 기록되지 않는다.
  실패한 Job의 테스트 전용 수동 재-Claim도 기존 Intent로 재실행을
  차단한다. 목표 외 Label 추가는 성공 조건을 무효화하지 않는다.
  이 경합은 **동일 테스트 프로세스의 Fake 원격 상태 및 별도 DB 세션**
  시뮬레이션이다. 실제 GitHub 사용자·외부 앱·네트워크 왕복,
  교차 프로세스 Actor 간 성능·경합 순서를 검증한 것은 아니다.
- [독립 프로세스 HTTPS 경합 테스트](../../tests/tools/gh/test_issue_label_worker_external_https.py)는
  기존 `GitHubIssueHttpTransport`·`GitHubIssueLabelHttpTransport`가
  인증서·호스트명을 검증한 채 로컬 HTTPS Fake GitHub에 실제 GET/POST
  요청을 전송하는 경계로 확장한다. 테스트 내부에서만
  `api.github.com` 연결 대상을 `127.0.0.1`로 바꾸며 외부로
  실제 GitHub 요청을 보내지 않는다. 별도 Python 프로세스의 외부
  Actor는 **독립 PostgreSQL 연결**에서 Q78 잠금 가능 여부와 Q79의
  Attempt 건수를 확인하고, 자체 TLS 인증서 검증을 거쳐 Fake 서버의
  전용 테스트 엔드포인트에 상태 변경 요청을 보낸다.
  요청·응답 경계로 결정론적 순서를 강제해 5가지 경합을 재현한다:
  보호 구간 중 목표 사전 충족(POST·Attempt 생략), POST와 관계없이
  외부 Actor가 목표 달성(효과 귀속 불가), POST 직후 목표 제거(Hold),
  잠금 해제 후 최종 재검증 직전 제거(Hold), 무관한 Label 추가(허용).
  실패한 경우에는 Test-only 강제 새 Claim도 기존 Intent 때문에
  GET·POST 전에 거부된다. 이는 **실제 TLS 네트워크·독립 프로세스 경합**
  검증이지만, GitHub 실제 API의 동시성·관측 지연·일관성, 외부 서비스의
  실행 귀속이나 Exactly-once를 증명하지 않는다.
- [실제 Worker 프로세스 크래시 테스트](../../tests/tools/gh/test_issue_label_worker_crash.py)는
  격리 PostgreSQL의 Procrastinate 자식 프로세스를 Attempt INSERT 성공 직후
  (아직 미커밋인 트랜잭션 내부)와 별도 트랜잭션 커밋 직후
  (Fake POST 효과 적용 전)에 각각 강제 종료한다. 미커밋 INSERT는
  같은 세션에서만 보이고 프로세스 종료 후 롤백됨을 확인한다.
  두 경우 모두 Root·Label Intent, Root Checkpoint, lineage는 영속하고
  Label Checkpoint는 없으며, Attempt는 커밋 후 종료에서만 남는다.
  독립 연결의 Recovery는 모두 `outcome_unknown` Hold로 판정하고,
  Attempt Digest는 커밋된 경우에만 관측한다. 새 Worker는 여전히 `doing`인
  Job을 자동 재실행하지 않으며 테스트용 새 Worker·started 이벤트로
  재-Claim해도 기존 Intent가 GET/POST를 차단한다. Attempt 미기록은
  원격 미실행 증명도, 새 실행 승인도 아니다.
- 같은 격리 Worker 테스트는 세 가지 **불확실한 결과**도 주입한다.
  첫째, Q79의 실제 최상위 트랜잭션 커밋과 독립 세션의 영속 확인 이후
  호출자에 `psycopg.OperationalError`를 발생시켜 **COMMIT 확인 응답 유실**을
  모사한다. Attempt는 남고 POST는 시작되지 않으며 Job은 실패한다.
  둘째·셋째, 별도 세션에 Fake 서버 수신 기록을 커밋한 POST가
  Label을 적용했거나 적용하지 않았더라도 응답은 유실되도록 한다.
  양쪽 모두 Checkpoint 없이 `outcome_unknown` Hold이며 재실행하지 않는다.
  명시적 읽기 전용 Probe는 각각 `goal_satisfied`와 `inconclusive`를
  관측하지만 실행 귀속을 주장하거나 Checkpoint를 소급 생성하지 않는다.
  실패 Job의 재기동 및 테스트 전용 새 Claim에서도 GET/POST가 차단된다.
  이 세 가지는 **Fake POST 및 커밋 완료 후 예외 주입**에 기반한다.
- 동일한 Procrastinate Worker 테스트에서 **실제 loopback TCP 장애**도
  검증한다. 첫째, 기존 `GitHubIssueLabelHttpTransport`의 HTTPS 연결 생성만
  자식 프로세스 내부에서 로컬 HTTP 소켓으로 교체한다. 고정된
  `api.github.com` 대상명·정확한 POST 경로·헤더·Label JSON 본문을
  서버가 수신한 뒤, Label 적용·미적용 각각에서 HTTP 응답을 보내지 않고
  TCP 연결을 닫는다. 독립 세션에 Fake 서버 수신 사실을 남기지만
  Worker는 실패하며 Attempt만 영속하고 Label Checkpoint는 남기지 않는다.
  둘째, **Attempt 전용 PostgreSQL 연결**에만 loopback TCP 프록시를 삽입한다.
  해당 테스트 연결은 SSL/GSS를 비활성화하며, 프록시가 실제 PostgreSQL
  `COMMIT`의 CommandComplete 응답을 수신한 뒤 클라이언트에는 전송하지
  않고 TCP 연결을 끊는다. 독립 PostgreSQL 세션에서 Attempt Commit을
  확인하고 POST가 시작되지 않으며, Recovery는 `outcome_unknown`을
  유지하고 재-Claim에서도 실행하지 않음을 검증한다.
  테스트 외 연결·외부 GitHub로는 장애 트래픽을 전달하지 않는다.
- [localhost 전용 테스트 TLS 인증서](../../tests/tools/gh/loopback_tls_test_only.pem)를
  사용해 같은 Worker의 **실제 TLS POST** 응답 유실도 검증한다.
  자식 프로세스에서만 HTTPSConnection의 초기 연결 대상을 127.0.0.1로
  지정하고 Python 기본 인증서 검증·호스트명 검사는 활성화한다.
  테스트 인증서를 명시적으로 신뢰하는 경우 TLS 1.2/1.3 핸드셰이크 이후
  서버가 Label POST 전체를 수신하고 적용하거나 적용하지 않은 채
  HTTP 응답 없이 소켓을 닫는다. 각각 원격 관측·Recovery·재-Claim을
  앞선 TCP 사례와 동일하게 검증한다. 테스트 인증서를 신뢰하지 않는 경우
  인증서 검증 오류로 POST가 서버에 도달하지 않아도, 이미 기록된
  Attempt 및 `outcome_unknown` Hold는 자동 재실행 권한이 아니다.
  인증서의 개인 키는 **공개된 테스트 전용 비밀이 아닌 픽스처**로,
  localhost 밖의 인증·서명·운영 환경에 재사용해서는 안 된다.
  추가 런타임·개발 의존성이나 GitHub 실제 요청은 도입하지 않는다.
- 같은 공개 테스트 인증서로 **PostgreSQL의 TLS Attempt 연결 장애**도
  두 경우 검증한다. Procrastinate Worker는 Attempt 전용 연결에서만
  `sslmode=verify-full`, `gssencmode=disable`을 사용한다. 프록시는
  실제 PostgreSQL `SSLRequest`를 수락한 뒤 TLS 1.2/1.3 연결을 종료하고,
  인증된 통신의 평문 PostgreSQL 프레임을 격리 loopback DB로 전달한다.
  **COMMIT 응답 유실**에서는 프록시가 백엔드의 `COMMIT` 완료 프레임을
  관측하고 클라이언트에 전달하지 않은 채 연결을 끊는다. 다른 DB 세션의
  읽기에서 영속 Attempt가 확인되고, POST·Label Checkpoint가 생성되지
  않으며 Recovery는 `outcome_unknown` Hold를 유지하고 재-Claim도
  실행을 차단한다.
  **호스트명 검증 실패**에서는 같은 테스트 인증서를 신뢰하더라도
  `host`와 실제 접속 주소 `hostaddr`가 가리키는 이름이 다르면 libpq가
  연결을 거부한다. 이 단계는 Worker가 Attempt 전용 연결을 미리 열기
  때문에 Claim Generation·Plan·Intent 기록보다 **앞서** 실패한다.
  따라서 원격 실행 결과 불확실성과 구별하고, Attempt 미기록·POST
  미실행·실패 Job 무자동 재시도만 확인한다. 이 경우 Intent가 없으므로
  기존 Intent에 의한 재-Claim Hold가 검증됐다고 주장하지 않는다.
- 이 검증은 loopback TLS 종료 **테스트 프록시**를 포함하지만 운영
  PostgreSQL의 종단 간 TLS, GSS/Kerberos, GitHub PKI·SNI·HTTP/2,
  운영 중간 장비·네트워크 분할, 실제 GitHub 외부 Actor 경합 및 안전한 수동 복구를
  검증하거나 활성화하지 않는다. 네트워크에서 `COMMIT` 결과를 받지
  못한 것이 원격 미실행이나 Exactly-once 보장을 뜻하지 않는다.

### 영속 Delivery의 읽기 전용 배포 사전 검사

[`inspect_persisted_delivery_contracts`](../../src/local_dev_mcp/tools/gh/delivery/deployment_compatibility.py)는
명시적 `event_id`의 Receipt·Job 연결·Sealed Plan·기록된 Immutable Intent를
읽고, 현재 Consumer Catalog와 정확한 버전을 대조한다. 이미 기록된 **종속 Intent**는
필드 단위 lineage와 선행 Checkpoint·Intent의 정규 bytes·Digest·Plan 연결도
같은 읽기 전용 트랜잭션에서 검증한다. 해당 증거 테이블이 설치되지 않았거나
lineage·source Checkpoint가 누락·손상됐으면 `lineage_invalid` Hold다.
lineage 테이블이 설치되어 있다면 **Root Intent에 존재해서는 안 되는 lineage 행**도
`lineage_invalid` Hold로 분류한다. 저장된 증거가 없다는 것과 부적절한 증거가
존재한다는 것을 모두 확인한다.
Checkpoint 저장 테이블이 설치되어 있으면 Root를 포함한 **모든 기존 Checkpoint**의
정규 bytes·Digest와 원본 Intent 연결도 검증하며 손상·불일치는
`checkpoint_invalid` Hold로 처리한다. Checkpoint의 기록 Generation이
Intent의 최초 작성 Generation보다 앞서면 시간순 계약 위반으로 Hold한다.
기록된 Intent·Checkpoint가 참조하는 Claim Generation은 **동일한 Receipt Job의
실제 `started` 이벤트**에 연결되는지도 검증하며, 참조가 누락됐거나 다른
Job을 가리키면 `claim_evidence_invalid` Hold다. 참조하지 않는 Claim 이력은
이 검사에서 임의로 복구하거나 재해석하지 않는다. 테이블 미설치 Root-only
단계에 별도 DDL이나 Checkpoint 생성을 강제하지 않는다.
`inspect_pending_deployment_contracts`는 Procrastinate의 종료되지 않은
Receipt Job 전체를 **단일 PostgreSQL REPEATABLE READ READ ONLY 트랜잭션**
내에서 제한된 수로 확인한다. 범위가 상한을 넘으면 일부 성공이 아니라
`scope_incomplete` Hold를 반환한다. 누락된 Plan, 비정규 Intent·버전 충돌은
Hold이며 알 수 없는 과거 버전을 최신 구현에 대입하지 않는다.

이 검사는 **스냅샷 관측**일 뿐 실제 배포 승인, Worker 시작, GitHub API 호출,
서버 전체의 미래 상태 보장 또는 외부 실행권이 아니다. 종료된 Job은 일괄 검사
대상에서 제외하며, 수동 재처리 후보는 `event_id`별로 명시적으로 다시 조사해야
한다. 저장된 Output의 실제 진위나 안전한 재실행 조건까지 검증하지 않는다.

### Operation별 Recovery Evidence 관찰 — Q65~Q69

[`inspect_operation_recovery_evidence`](../../src/local_dev_mcp/tools/gh/delivery/recovery_inspection.py)는
명시적 `(event_id, operation_id)`와 현재 Consumer Contract 목록을 전달받아
Receipt·Job·Sealed Plan·Immutable Intent·Checkpoint·선행 lineage를
**하나의 PostgreSQL REPEATABLE READ READ ONLY 스냅샷**에서 대조한다.
Consumer Hook, Recovery Probe, 외부 GitHub 조회·쓰기, Job 상태 변경,
DDL 및 자동 재시도는 실행하지 않는다. Root-only 저장 계약에서는
Checkpoint·lineage DDL이 선택 설치이지만, Recovery 관측은 **두 증거 테이블이
모두 있을 때만** 수행한다. 하나라도 미설치라면 조인 오류나 신규 Pending
추정 대신 `evidence_store_unavailable` Hold를 반환한다. 이는 Root-only
배포 **정적 호환성** 검사와는 독립된 판단이다.

- `pending_observed`: 해당 Operation의 Intent가 없고 Job 상태가 `todo`이며
  **이 Job의 started 이벤트 이력과 Receipt의 Claim Generation 기록도 모두
  없고 Job의 `attempts=0`, `worker_id=NULL`, `abort_requested=false`인
  경우**에 한정된다. 이는 DB 관측이며 외부 변경이 절대 없었다는 증명이나
  재실행 허가는 아니다.
- `active_unresolved` / `historical_start_unresolved`: Intent가 없더라도
  실행 중이거나 이전 started 이력이 있으면 Pending으로 축소하지 않는다.
- `claim_history_unresolved`: started 이력이 누락됐더라도 Receipt에
  **Claim Generation 증거가 남아 있으면** 미실행으로 판정하지 않고 Hold한다.
  Job 이벤트와 저장된 Claim 이력의 불일치를 자동 보정하지 않는다.
- `job_history_unresolved`: started/Claim 이력이 없더라도 Job의 재시도
  횟수·Worker 귀속·Abort 요청이 신규 Pending 상태와 충돌하면 Hold한다.
- `outcome_unknown`: Intent는 커밋됐으나 Checkpoint가 없을 때에는
  GitHub 변경의 결과를 알 수 없는 것으로 유지한다. **무변경으로 간주하거나
  자동 재시도하지 않는다.**
- `checkpoint_observed`: Intent와 Output Checkpoint의 정규 bytes·Digest,
  Plan 및 필드 단위 선행 lineage가 일치할 때에만 저장 기록의 존재를 보고한다.
  별도의 신뢰 가능한 외부 결과 검증이 없는 한 GitHub 작업 성공이나 Exactly-once
  증명은 아니다.
- `hold`: 미지원 Consumer 버전, 누락·손상된 Plan/Intent/Checkpoint/lineage,
  알 수 없는 Job 상태 등은 구체적인 사유와 함께 보류한다.

Q66의 실제 Consumer별 **읽기 전용 Recovery Probe**, Q68의 미실행 또는
업무 멱등성에 대한 검증 가능 증거, Q74의 실행 시점 동시성 보호를
아직 연결하지 않았다. 이 API는 실행 승인 토큰을 발급하거나 재시도를 결정하지 않는다.

읽기 전용 Issue Consumer의 **명시적 바인딩** 구현은 있으나, 운영용
`ContractCatalog`/Worker에 자동 등록되지는 않았다. 테스트의 가상 Consumer와
제출한 테스트 증거 Digest는 원격 동시성 보호의 실제 검증이 아니다. 향후
운영 배포에는 실행 시점 Claim·Guard 검사와 테스트 증거 검증,
프로덕션 배포 경로에 사전 검사 연결, 명시적 복구 절차가 별도로 필요하다.

## 미구현 실행 경계

- Q72: 단일 Root 읽기와 `read:v1 → add_label:v2` 2단계 Worker는
  Claim 기록→Plan 봉인→Intent 커밋을 강제한다. 그러나 기존
  `seal_plan_structure` API 자체는 실행 승인이 아니며 일반적인
  Worker/Operation의 순서 강제는 아직 구현하지 않았다.
- Q70~Q73: 두 개의 명시적 Operation은 Root Checkpoint→종속 Intent·lineage
  →목표 Checkpoint를 영속화하지만, 범용 DAG·다중 Root 및 복구 상태
  머신 실행 연결은 구현하지 않았다.
- Q74~Q76: 읽기 및 Label의 **명시적 Task 바인딩**은 있으나
  운영 기본 등록·GitHub 쓰기 활성화·실제 배포 동시성·복구 검증은 없다.
  정적 Registry 일치와 단일 DB 잠금만으로 원격 실행 안전성이나
  Exactly-once 결과를 인증하지 않는다.
- Q65~Q69: 결과 불명확한 GitHub 변경을 무변경으로 해석하거나
  자동 재실행하지 않는다. Recovery Probe·허용된 재실행 조건은
  별도 검증·결정이 필요하다.

[순수 구조 테스트](../../tests/tools/gh/test_operation_plan.py)는 불변
구조와 결정적 Digest를 검증한다.
[Plan·Intent 통합 테스트](../../tests/tools/gh/test_delivery_evidence.py)와
[Checkpoint·lineage 통합 테스트](../../tests/tools/gh/test_delivery_checkpoints.py)는
격리된 임시 schema에서 동시 기록·멱등성·충돌·손상·불변 DB Trigger를,
[Claim 통합 테스트](../../tests/tools/gh/test_delivery_claims.py)는 Generation 변경을 검증한다.
[읽기 전용 Worker 통합 테스트](../../tests/tools/gh/test_issue_read_worker.py)는
실제 Procrastinate Job Claim과 Fake GitHub GET을 결합한다.
[Label Worker 통합 테스트](../../tests/tools/gh/test_issue_label_worker.py)는
Fake GET/POST 및 Attempt 영속화까지 확인한다. 실제 GitHub 쓰기와
운영 배포 기동 E2E는 검증하지 않았다.
