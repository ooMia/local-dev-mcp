# 문서 지도와 결정 우선순위

이 저장소의 문서는 **상위 결정 → 기능별 계약 → 구현 설명·검증 → 조사 근거** 순서로 해석한다.
경로 이동이나 파일 이름만으로 문서의 우선순위 또는 최신성을 판단하지 않는다.

## 권한과 소유권

| 층위 | 소유 문서 | 범위 |
| --- | --- | --- |
| 저장소 지침 | [AGENTS.md](../AGENTS.md) | 작업 범위·안전·변경 검증 |
| 채택 결정 | [ADR 0001](adr/0001-observed-file-edits-and-access.md), [ADR 0002](adr/0002-document-authority.md), [ADR 0004](adr/0004-document-core-identity-source-boundary.md) | 상위 아키텍처·권한 결정 |
| 개발 규약 | [conventions.md](conventions.md) | 배치·명명·테스트 |
| 운영 계약 | [configuration.md](configuration.md), [도구별 계약](tools/) | 실제 입력·결과·권한 |
| 실험적 내부 계약 | [Document Core](contracts/document-core.md), [Event Core](contracts/event-core.md), [GitHub Delivery](contracts/github-durable-delivery.md), [GitHub Webhook 검증](contracts/github-webhook-verification.md), [GitHub Plan 구조](contracts/github-operation-plan.md) | 순수 모델·GitHub 영속 수락 및 인증 경계 |
| 설계·검증 | [design.md](design.md), [testing.md](testing.md) | 연결 구조 및 시험 근거 |

[초기 Migration Guide](migration-guide.md)는 관리 문서 5개의 명시적 등록 정보와 관계 승인 대기 상태를 보유한다.
새 계약은 실제 구현 범위에 맞춰 소유 문서에 추가한다. 조사 자료는 구현된 동작의 계약을 대체하지 않는다.
연구 근거: [테스트 정책](research/testing-policy-research.md), [CLI 작업 정책](research/cli-task-policy-research.md), [B3-S 의존성·호환성 검증](research/b3s-delivery-compatibility-2026-11-02.md).

## 적용 순서

1. 루트 지침과 관련 Accepted ADR을 확인한다.
2. 변경 영역의 현재 계약·규약·코드·테스트를 대조한다.
3. 충돌 시 하위 설명만 임의로 수정하지 않고 결정 변경 여부를 확인한다.
4. 유지할 결정에 맞춰 코드·테스트·계약을 함께 갱신한다.
5. 참조·diff·검증 결과와 미검증 범위를 보고한다.
