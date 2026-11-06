# GitHub Webhook — Experimental Verification Contract

이 계약은 B3-S의 GitHub 전용 **수신 전 검증·정규화와 ASGI HTTP 수락 경계**를 정의한다.
[Event Core](event-core.md)와 [Durable Delivery](github-durable-delivery.md)를 연결한다.
현재 ASGI 앱 구성 함수는 제공하지만 독립 HTTP 서버 기동/배포, Worker, webhook 등록,
외부 GitHub 쓰기는 제공하지 않는다.

## 신뢰·입력

- 호출자가 **별도로 준비한 고엔트로피 webhook secret**(`bytes`, 32~8192바이트)과
  명시적 `repository full_name -> repository.id` 허용 목록을 전달한다.
  자동 Git remote 탐색, 환경 변수 fallback, GitHub credential 조회는 수행하지 않는다.
- `verify_delivery(secret, headers, body, repositories)`는 원본 **바이트**에 대한
  GitHub `X-Hub-Signature-256` HMAC-SHA256를 상수 시간 비교로 검증한다.
  공식 [GitHub 서명 검증 문서](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)를 따른다.
- 본문은 최대 1 MiB, `Content-Type: application/json`(UTF-8 선택 가능),
  `Content-Length`가 실제 길이와 일치해야 한다.
  필수 헤더 누락·동일 이름의 중복 헤더·인코딩·부정확한 Signature는 거부한다.
- JSON은 유효한 UTF-8 객체여야 한다. 중복 키·NaN/Infinity·잘못된 타입·재귀 한계
  오류를 거부하며 비밀·본문을 오류 메시지에 포함하지 않는다.
- **서명된 Payload**의 `repository.full_name`과 `repository.id`를 허용 목록의
  이름·숫자 ID 모두와 대조한다. 서명은 별도 HTTP 헤더의 진실성을 보장하지 않는다.

## 검증 결과

- `VerifiedDelivery`는 `EventObservation`과 선택적 `VerifiedTarget`를 가진
  불변 값이다. 원본 Payload와 secret은 모델에 보관하지 않는다.
- 정체성은 검증된 저장소 ID를 Source 인스턴스 이름으로 사용한
  `(github:repository:<id>, X-GitHub-Delivery)`다.
  헤더의 Delivery ID는 **서명된 본문과 별개의 식별자**다.
- Digest는 전송된 원본 바이트의 SHA-256이다. Event Schema는
  `github-webhook:1`이며 저장소·Store Schema 및 Consumer Operation Contract의
  버전과 결합하지 않는다.
- 지원 범위는 `issues`와 `pull_request`의 기존 정해진 Action이며,
  결과 `VerifiedTarget`에는 작업 종류·저장소 이름/ID·번호·Action·updated_at을 담는다.
  기타 유효한 이벤트·Action, `ping`의 Target은 `None`이다. **이는 수락 작업이 아니다.**
- `InvalidWebhook`은 민감 정보를 포함하지 않는 사유 코드만 제공한다.

## ASGI HTTP Receiver 수락 계약

- [`create_app`](../../src/local_dev_mcp/tools/gh/webhook/receiver.py)은
  `secret`, 명시적 Repository ID 허용 목록, `connection_factory`,
  Procrastinate Task를 주입받는 **ASGI Callable**을 반환한다.
  Import/생성 자체는 포트를 열거나 PostgreSQL에 연결하지 않는다.
  앱 생성 시 Repository 허용 목록의 이름·양의 정수 ID·대소문자 중복을
  **즉시 검증**하고 별도 스냅샷으로 보관한다.
  잘못된 설정은 첫 요청을 기다리지 않고 `ValueError`로 실패한다.
  프로세스·포트·Ingress·DB Schema 적용은 별도의 운영 소유자 책임이다.
- 유일한 HTTP 경로는 `POST /webhooks/github`이다. ASGI Lifespan은
  시작/종료 메시지에 응답하지만 Worker/연결 생성은 하지 않는다.
  운영자는 필요할 때 `inspect_receiver_database(connection_factory=...)`를
  **명시적으로 호출**하여 기존 읽기 전용 스키마 검사의 결과(`SchemaReadiness`)를
  확인할 수 있다. 이 점검은 ASGI 앱/Worker를 시작하거나 DDL을 실행하지 않으며
  연결 실패는 호출자에게 전파된다. Lifespan 자동 준비 판정으로 간주하지 않는다.
- 원본 ASGI 헤더의 중복을 보존해 전달하며, `Content-Length`를
  먼저 확인한다. 스트림에서 본문을 읽는 도중 1MiB 및 선언 길이를
  초과하면 즉시 거부하고, 수신 전체에는 5초 제한을 둔다.
  정상 완료된 본문만 서명·Repository 검사에 전달한다.
- **검증 완료 전에는 DB 연결을 열지 않는다.** `target=None`인
  인증된 ping·미지원 이벤트는 저장/큐잉 없이 `200 ignored`를 반환한다.
- 지원 대상 이벤트는 지정된 DB 연결을 연 뒤
  [읽기 전용 스키마 사전 검사](github-durable-delivery.md)를 통과해야
  [`accept_event`](github-durable-delivery.md)를 호출한다.
  필수 컬럼·중복 방지 제약·Queue 테이블이 부적합하면 **503으로 거부**하며
  Receipt/Job을 INSERT하지 않는다. 사전 검사는 요청마다 수행하므로
  DB 메타데이터 조회 비용이 추가된다.
  Receipt + 최소 Envelope + Job의 **최상위 Commit 완료 후** 신규 이벤트는
  `202 accepted`, 동일 중복은 `200 duplicate`을 반환한다.
- Signature 오류는 `401`, Scope 위반은 `403`, Payload/헤더/스트림 오류는
  `400`, 큰 본문은 `413`, 미지원 Content-Type은 `415`,
  동일 이벤트 증거 충돌은 `409`, DB/Job/Commit 오류 또는 결과 불명확 상태는
  **`503 storage_unavailable`**이다.
  반환 본문은 고정된 오류 코드만 담으며 Secret·Payload·DB 오류 원문을 포함하지 않는다.
- HTTP 성공 반환은 **응답이 GitHub에 실제 전달되었다는 보장도,
  Worker 실행·GitHub API 변경 성공의 증거도 아니다.**
  연결 중단/알 수 없는 Commit 상태에서 무조건 재수락하거나 외부 쓰기를
  재시도하지 않는다. 기존 Q65~Q69의 읽기 전용 Recovery Probe와
  Q70~Q76의 Immutable Intent·Checkpoint·Hold 계약을 그대로 따른다.
- Receiver는 PostgreSQL 스키마를 설치·마이그레이션하지 않는다.
  임의 Starlette/ASGI 서버 패키지를 추가하지 않고 표준 ASGI 3 인터페이스만
  구현한다. 네트워크 레벨의 HTTPS/서버 E2E 테스트는 아직 수행하지 않았다.

이 모듈을 import해도 HTTP listen·webhook 등록·설정 수정·Worker 시작·
GitHub API 변경은 발생하지 않는다.

[Webhook 검증 단위 테스트](../../tests/tools/gh/test_webhook_verify.py)와
[ASGI Receiver 단위 테스트](../../tests/tools/gh/test_webhook_receiver.py)는
인증·요청 크기·스트리밍·중복 헤더·오류 응답을 검증한다.
[PostgreSQL 통합 테스트](../../tests/tools/gh/test_delivery_receiver.py)는 ASGI 요청을 통한
Commit 이후 ACK, 중복·충돌·DB 실패 시 반환 및 Job/Receipt 보존을 검증한다.
