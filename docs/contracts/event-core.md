# Event Core — Experimental Evidence Contract (Stage 0)

이 계약은 B3-S(역할별 보장 분리)의 **순수 Python 내부 API**만 정의한다.
저장소의 [문서 권한 정책](../adr/0002-document-authority.md)에 따라 현재 실행 정책을 대체하지 않는다.

## 목적과 경계

- GitHub webhook과 로컬 작업이 공통으로 사용하는 이벤트 **정체성**과 **관측 증거**를 표현한다.
- Source Adapter가 모든 식별자와 증거 값을 명시적으로 제공한다. Core는 네트워크·원본 Payload·Git·DB·MCP·파일 시스템에 접근하지 않는다.
- 영속적 수락 ID(`event_id`), 중복 수락 처리, 작업 상태·재시도·복구 승인, Consumer Operation Contract는 Stage 0의 책임이 아니다.
- `Event Schema`, `Store Schema`, `Consumer Contract`, `Operation Contract`의 버전을 하나로 합치지 않는다.

## 값 계약

| 값 | 필드 | 의미 |
| --- | --- | --- |
| `EventIdentity` | `source_instance_id: str`, `event_key: str` | Source 인스턴스 내에서 명시적으로 주어진 이벤트 키 |
| `EventObservation` | `identity: EventIdentity`, `event_type: str`, `event_schema_version: str`, `observed_at: datetime`, `payload_digest: str` | 해당 이벤트의 관측 증거 |

두 값은 `@dataclass(frozen=True, slots=True)`이며, 값을 생성한 이후 변경할 수 없다.

- `source_instance_id`는 Source 이름만이 아니라 **Source 인스턴스를 구분하는 호출자 소유 식별자**다. `event_key`는 그 인스턴스 안의 이벤트 식별자다. 두 값을 합쳐 정체성을 구성한다.
- 두 ID와 `event_type`, `event_schema_version`은 비어 있거나 양끝 공백·제어 문자를 포함할 수 없다. Source별 이벤트 유형 및 Schema Version은 Core가 해석하지 않는 문자열이다.
- `observed_at`은 시간대 정보가 있는 Python `datetime`만 허용하며, 입력의 시간대 표현을 유지한다. 관측 시각을 이벤트 발생 시각으로 가정하지 않는다.
- `payload_digest`는 `sha256:` 뒤에 **소문자 16진수 64자리**를 붙인 명시적 SHA-256 값이다. Core가 원문을 가져오거나 해시를 계산·정규화하지 않는다. 원문 Payload의 바이트 선택과 정규화 여부는 Source Adapter 계약에서 결정한다.
- EventIdentity는 Payload 해시에서 추론하지 않는다. **같은 Digest라도 서로 다른 이벤트일 수 있고**, 같은 Identity에 다른 Digest가 관측될 수도 있다. 이러한 불일치에 대한 수락·경고·차단 정책은 Consumer가 결정한다.

## Stage 0 수락 기준

1. 서로 다른 `source_instance_id`의 동일한 `event_key`를 구별한다.
2. 서로 다른 Identity가 같은 `payload_digest`를 가질 수 있다. Digest 일치만으로 중복 이벤트를 단정하지 않는다.
3. 동일 Identity의 Digest 또는 Schema Version 차이를 관측 값으로 보존하며, 이벤트 정체성을 재할당하지 않는다.
4. 시간대 있는 시각을 그대로 보존하고, naive datetime·잘못된 레이블·잘못된 Digest·잘못된 타입은 거부한다.
5. EventIdentity와 EventObservation은 불변이며, 원본 Payload·영속 수락 ID가 필드에 없다.
6. 순수 단위 테스트에서 Git·MCP·외부 프로세스·DB 없이 사용 가능하다. 새 공개 MCP 도구·CLI 명령·외부 의존성은 없다.

[Stage 0 수락 테스트](../../tests/event_core/test_model.py)가 이 계약을 검증한다.

## 후속 단계 (이번 구현 범위 제외)

GitHub 전용 PostgreSQL·Procrastinate의 설치·Python 3.15 호환성·로컬 트랜잭션 검증은
[Stage 1 조사 기록](../research/b3s-delivery-compatibility-2026-11-02.md)에 있다.
이는 Event Core의 순수 모델 범위를 확장하거나 운영 DB 적용을 증명하지 않는다.
현재 연결과 미구현 실행 경계는 [Delivery](github-durable-delivery.md) 및
[Operation 계약](github-operation-plan.md)을 따른다.
Local Task는 현재 Shellous 실행 계약을 유지한다.
Q65~Q69의 보류·Recovery Probe·재실행 안전 규칙은 외부 부작용 경계에서 구현하며, 단순한 DB/Queue 재시도로 간주하지 않는다.
