# GitHub Durable Delivery — Experimental Stage 1 Contract

이 계약은 [Event Core](event-core.md)와
[GitHub webhook 검증](github-webhook-verification.md)이 생성하는 **VerifiedDelivery**를
사용하는 GitHub 전용 내부 Python Adapter의 영속 수락을 정의한다.
원본 Payload는 저장하지 않는다. HTTP 수락 연결은
[ASGI Receiver](github-webhook-verification.md)가 소유하며, 이 문서의 수락 Adapter는 Worker나 GitHub API 변경을 실행하지 않는다.
후속 읽기·Label Worker의 명시적 연결은 [Operation 계약](github-operation-plan.md)이 소유한다.

## 승인된 A안: 최소 정규화 Envelope

- `github_event_receipts`의 고유 키는 `(source_instance_id, event_key)`이며
  EventObservation의 `event_type`, `event_schema_version`, `payload_digest`,
  `observed_at`와 DB 생성 `event_id`를 기록한다.
- 정규화 Envelope는 `envelope_schema_version = github-target:1`,
  `repository`, `repository_id`, `target_kind` (issue/pull_request),
  `target_number`, `action`, `target_updated_at`이다.
  이벤트 Schema와 Envelope/Store/Operation Contract의 버전은 독립적으로 관리한다.
- [검증 모델](../../src/local_dev_mcp/tools/gh/webhook/verify.py)의
  `VerifiedTarget`이 이 최소 정보의 입력이다. 지원 대상이 없는 ping·미지원 Action은
  `target=None`이고 Job 수락에 사용할 수 없다.
- 원본 Webhook JSON/본문·비밀·인증정보·이벤트 전체 snapshot을 저장하지 않는다.
  Procrastinate Job 인자는 **`{"event_id": <receipt-id>}`만** 담고, Worker가
  필요 시 Receipt에서 정규화 Envelope를 읽어 현재 GitHub 상태를 재조회한다.
- 원본의 과거 필드는 보존되지 않으므로 나중에 복원할 수 없다.
  `target_updated_at`은 수신 당시 값이지 GitHub의 현재 상태 또는 실행 증명이 아니다.

## 스키마 설치·마이그레이션

- [Schema 소유 파일](../../src/local_dev_mcp/tools/gh/delivery/schema.py)의
  `RECEIPTS_DDL`은 **신규 테이블**용이며, 설치된 Procrastinate 3.10.0 schema와
  함께 호출자가 명시적 격리 schema에 적용한다.
- 구버전 `github_event_receipts`에는 Target 자체가 없었으므로 **기존 행에
  Envelope를 안전하게 재구성할 수 없다.** `MIGRATE_EMPTY_V1_TO_V2`는
  영(0)건인 이전 테이블에만 명시적으로 적용하도록 SQL guard를 포함한다.
  행이 하나라도 존재하면 실패하고 기존 데이터를 보존한다.
- Adapter를 import하거나 `accept_event`를 호출해도 DDL/migration은 자동
  실행하지 않는다. 운영 DB 적용·데이터 보존 및 마이그레이션 실행 권한은 별도 검토 대상이다.
- [읽기 전용 사전 검사](../../src/local_dev_mcp/tools/gh/delivery/readiness.py)의
  `inspect_delivery_schema(connection)`은 현재 연결의 `current_schema()`에서
  Receipt/Procrastinate Jobs 테이블, 필수 컬럼·타입·Null 제약과
  `(source_instance_id, event_key)` Unique Constraint를 조회한다.
  `require_delivery_schema`는 부적합 상태를 `SchemaNotReady`로 거부한다.
  **데이터·테이블 변경이나 자동 마이그레이션은 없다.**
  이 검사는 전체 Procrastinate 함수·권한·실행 안전성 인증이 아니며,
  실제 배포 전 계약/스키마 소유자 확인을 대체하지 않는다.

## 트랜잭션·중복·복구

`accept_event(*, connection, delivery, task) -> DeliveryReceipt`:

1. 호출자는 **서명이 검증된** `VerifiedDelivery`를 전달한다.
   EventIdentity와 Target의 Repository ID·Action·Event Type 간 불일치를 거부한다.
   연결은 `autocommit=True`이며 `IDLE`이어야 한다. 중첩 savepoint를 성공으로
   오인하지 않는다.
2. 단일 PostgreSQL 트랜잭션에서 Event + Envelope를 Receipt에 INSERT하고
   Unique Constraint로 동일 source-qualified delivery의 경합을 직렬화한다.
3. 동일 키가 있으면 이벤트 유형·버전·Digest **및 전체 최소 Envelope**를 대조한다.
   일치하면 기존 `event_id/job_id`로 `duplicate`를 반환하고 Job을 추가하지 않는다.
   다르면 `EventConflictError`다. 미지원 Envelope 버전 또는 비정상 `job_id`는
   `ReceiptInvariantError`로 보류한다.
4. 신규 이벤트에만
   `task.configure(connection=connection).defer_async(event_id=...)`로 Job을
   생성하고 Receipt에 `job_id`를 연결한다. Job 등록/연결 실패 시 전체 Rollback한다.
5. 트랜잭션의 **최상위 Commit 후** `accepted`를 반환한다. 이는 HTTP ACK가
   실제 GitHub에 전달됐거나 Worker가 실행·변경했다는 증거가 아니다.

`lookup_receipt(*, connection, delivery) -> DeliveryReceipt | None`은
읽기 전용 중복·ACK 유실 조회다. 대조 실패·미지원 계약은 명시적 오류로 처리한다.
`None`은 조회 당시 커밋된 행을 찾지 못했다는 의미일 뿐, 진행 중인 다른 연결의
Commit 실패를 증명하거나 자동 재수락을 허용하지 않는다.

`load_target(*, connection, event_id) -> VerifiedTarget | None`은
Job의 `event_id`로 저장된 최소 Envelope를 조회하는 **읽기 전용** 경로다.
Job 연결·Envelope 버전·필수 필드가 불완전하면 Hold를 위한
`ReceiptInvariantError`를 발생시킨다. 이는 현재 GitHub 상태도,
Q70의 Immutable Intent도 아니며 외부 쓰기를 허용하지 않는다.

[`inspect_queued_envelope`](../../src/local_dev_mcp/tools/gh/delivery/worker_probe.py)은
Worker Claim **이전의 읽기 전용 관찰**이다. 전달된 `event_id/job_id`와
Receipt의 Source·이벤트/Envelope 버전·대상 Action·Digest, 실제
Procrastinate Job의 ID·`task_name` 및 `args == {"event_id": ...}`를
**하나의 SELECT**로 대조한다. 호출자는 등록된 Task의 기대 이름을
`expected_task_name`으로 명시해야 하며, 이름 불일치도 `hold` 대상이다.
`observed`는 조회 시점에 연결 관계가 일치했다는 의미일 뿐,
Job 상태·실행 가능성·GitHub 쓰기 권한을 보증하지 않는다. Receipt 누락,
미지원 계약·Action, Job 연결 오류·불일치는 `hold` 및 안전한 사유 코드로
반환한다. DB 접근 자체의 오류는 성공 또는 Hold로 위장하지 않고 전파한다.
이 API는 Claim, Queue 상태 전환, Intent 생성, 자동 재시도, 외부 API 호출을
실행하지 않는다.

[`inspect_running_claim`](../../src/local_dev_mcp/tools/gh/delivery/claim_evidence.py)은
**실행 중인 Procrastinate Job의 읽기 전용 상태 관찰**이다. 호출자가
`JobContext`에서 가져온 Event/Job ID·Task 이름·Worker ID·시도 횟수를
전달하면 하나의 SELECT로 Receipt 연결, Job 인자, `doing` 상태,
등록된 Worker 존재, `abort_requested=false`, 가장 최근 Job 이벤트가
`procrastinate_events.started`인지 확인하고 해당 ID를 반환한다. 불일치·부재는 `hold`로
분류하며, DB 오류는 그대로 전파한다.

`observed`와 `started_event_id`는 **조회 시점의 증거**일 뿐,
Delivery의 Claim Generation, Worker lease, 실행 권한이 아니다.
Procrastinate의 시도 횟수는 실행이 종료될 때 증가할 수 있으므로
그 자체를 Claim Generation으로 사용하지 않는다. `started_event_id`는
동일 Job의 서로 다른 시작 전이를 구별할 수 있으나 별도의 Generation
영속화·fencing을 대체하지 않는다. 조회 직후 상태가 변경될 수 있으므로
이 함수의 반환만으로 외부 변경을 실행하거나 불확실한 결과를 재시도하지 않는다.
실제 Worker를 시작하거나 Job을 Claim하는 코드는 포함하지 않는다.

## Delivery Claim Generation — 실험적 PostgreSQL 기록

- [`CLAIM_GENERATIONS_DDL`](../../src/local_dev_mcp/tools/gh/delivery/schema.py)은
  **명시적으로 설치**하는 추가 테이블과 UPDATE/DELETE 거부 Trigger다.
  Receipt·Procrastinate·Sealed Plan의 기존 테이블을 변경하지 않는다.
  `github_delivery_claim_generations`은 `(event_id, generation)`을
  Primary Key로 사용하며 `started_event_id`, `job_id`, `worker_id`,
  `attempts`를 append-only로 보존한다.
- [`record_claim_generation`](../../src/local_dev_mcp/tools/gh/delivery/claim_generation.py)은
  IDLE autocommit DB 연결을 사용하고, Receipt 행과 실제 Procrastinate Job 행을
  **순서대로 FOR UPDATE**로 잠근다. Caller가 관찰했던 정확한 `started_event_id`가
  아직 최신 `started` 이벤트이며 Job이 `doing`, 등록 Worker 일치,
  중단 요청 없음, Task·인자·시도 횟수 일치인 경우에만 기록한다.
- 동일한 현재 `started_event_id`는 `existing`을 반환하며, 새 시작 이벤트는
  **최신 Generation + 1**을 신규 행에 기록한다. 이전 이벤트 또는 서로 모순된
  Worker/시도 정보는 `ClaimGenerationHold`로 거부하고 기존 이력을 보존한다.
  Receipt 행 잠금은 여러 동시 기록이 같은 Generation을 생성하지 못하게 한다.
- `load_latest_claim_generation`은 최신 Generation을 **읽기 전용**으로 조회한다.
  운영 DB 설치·자동 마이그레이션·Worker Claim/기동은 수행하지 않는다.
  Trigger 불변성은 정상 SQL DML 기준이며 DB 관리자에 의한 스키마 변경까지
  막는다는 의미가 아니다.

이 Generation은 **DB에 저장된 Claim 관찰 이력**이며 원격 GitHub API 쓰기의
fencing token 또는 실행 승인 증명이 아니다. Procrastinate Job의 소유권은
DB 트랜잭션 종료 이후 바뀔 수 있다. Immutable Intent 기록은
같은 트랜잭션의 최신 Generation·Job 소유권을 다시 검증하지만, 이후
실행 권한이나 원격 GitHub 쓰기의 안전성을 보장하지 않는다.
원격 부작용에는 별도 Operation Contract·최신 상태 평가·재실행 정책이 필요하다.
불확실한 원격 결과를 이 기록만으로 자동 재실행하지 않는다.

[`inspect_claim_plan`](../../src/local_dev_mcp/tools/gh/delivery/claim_plan.py)은
**읽기 전용 Claim/Plan 일관성 관찰**이다. 하나의 SELECT에서 현재
Procrastinate Job·Worker·마지막 started 이벤트, 최신 Claim Generation 및
append-only Sealed Plan을 함께 확인한다. 호출자가 지정한 Plan의 canonical
bytes까지 대조하고, 없거나 지원되지 않는 증거는 `hold`로 반환한다.
확인 결과는 조회 시점의 일관성만 나타내며, 트랜잭션 종료 뒤 Claim의
소유권이 바뀌는 것을 막는다거나 외부 GitHub 쓰기 권한을 생성하지 않는다.

## 수락 이후의 연결과 미구현 경계

- HTTP 인증·스트리밍 및 Commit 후 ACK 조합은 [ASGI Receiver](github-webhook-verification.md)가
  소유한다. 서버/Ingress 배포·운영 Schema 적용은 이 수락 Adapter가 수행하지 않는다.
- [Operation 계약](github-operation-plan.md)은 append-only Plan·Intent·Checkpoint·lineage·Attempt와
  명시적 단일 Issue 읽기 Worker 및 `read:v1 → add_label:v2` 2단계 Worker를 소유한다.
  따라서 종속 Operation 전체를 미구현으로 해석하지 않는다. 자동 Task 등록·운영 Worker 기동,
  일반 그래프 실행과 결과 불명확 상태의 자동 재실행은 제공하지 않는다.
- Recovery의 로컬 영속 증거 관찰은 구현돼 있다. 이는 원격 성공의 독립적 진위 증명이나
  안전한 재실행 승인이 아니며, 구체적인 분류와 Hold는 [Recovery 계약](github-operation-plan.md#operation별-recovery-evidence-관찰--q65q69)을 따른다.
- 기존 Procrastinate·Psycopg 기본 의존성 배치는 변경하지 않는다.
  WIP SQLite Outbox 자동화 브랜치는 병합하지 않는다.

[Receipt 통합 테스트](../../tests/tools/gh/test_delivery_receipts.py)는 고유 임시 schema에서
Event+Envelope+Job Commit, 동시 중복·필드 충돌·전체 Rollback과 빈 테이블 전용 마이그레이션을,
[Receiver 통합 테스트](../../tests/tools/gh/test_delivery_receiver.py)는 Commit 이후 ACK·ACK 유실 조회를 검증한다.
Checkpoint/lineage 검증은 [Operation 계약](github-operation-plan.md)의 소유 테스트로 연결한다.
테스트 환경이 없으면 DB 테스트가 skip될 수 있다.
