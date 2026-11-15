# 문서 지도와 결정 우선순위

이 저장소의 문서는 **상위 결정 → 기능별 계약 → 구현 설명·검증 → 조사 근거** 순서로 해석한다.
경로 이동이나 파일 이름만으로 문서의 우선순위 또는 최신성을 판단하지 않는다.

## 권한과 소유권

| 층위 | 소유 문서 | 범위 |
| --- | --- | --- |
| 저장소 지침 | [AGENTS.md](../AGENTS.md) | 작업 범위·안전·변경 검증 |
| 채택 결정 | [현재 적용 정책](#현재-적용-정책) | 현재 유효한 범위와 부분 대체·일회성 예외를 작업별로 조회 |
| 개발 규약 | [conventions.md](conventions.md) | 배치·명명·테스트 |
| 운영 계약 | [configuration.md](configuration.md), [도구별 계약](tools/) | 실제 입력·결과·권한; [릴리스 도구](tools/release/README.md), [Trail 인덱스](tools/indexing/README.md), [PR workflow](tools/workflow/README.md) |
| 실험적 내부 계약 | [Document Core](contracts/document-core.md), [Event Core](contracts/event-core.md), [GitHub Delivery](contracts/github-durable-delivery.md), [GitHub Webhook 검증](contracts/github-webhook-verification.md), [GitHub Plan 구조](contracts/github-operation-plan.md) | 순수 모델·GitHub 영속 수락 및 인증 경계 |
| 설계·검증 | [design.md](design.md), [testing.md](testing.md) | 연결 구조 및 시험 근거 |

[초기 Migration Guide](migration-guide.md)는 관리 문서 5개의 명시적 등록 정보와 관계 승인 대기 상태를 보유한다.
새 계약은 실제 구현 범위에 맞춰 소유 문서에 추가한다. 조사 자료는 구현된 동작의 계약을 대체하지 않는다.
연구 근거: [Trail Code 탐색·토큰 실험](research/trail-code-context-budget-2026-11-14.md), [테스트 정책](research/testing-policy-research.md), [CLI 작업 정책](research/cli-task-policy-research.md), [B3-S 의존성·호환성 검증](research/b3s-delivery-compatibility-2026-11-02.md), [Issue #8 P3 검증 결과 증거 평가](research/issue-8-verification-evidence-2026-11-09.md), [WSL2 도구 호환성 감사](research/wsl-tool-compatibility-2026-11-15.md).

## 현재 적용 정책

ADR의 **Partially superseded**는 전체 폐기가 아니라 명시한 범위의 부분 대체를 뜻한다.
현재 실행 입력·오류·권한은 ADR 본문이 아닌 연결된 기능 계약이 소유한다.

| 작업 영역 | 현재 적용할 결정 | 실행 계약·예외 |
| --- | --- | --- |
| 파일 접근·문서 권한·실험 모델 | [ADR 0001](adr/0001-observed-file-edits-and-access.md), [ADR 0002](adr/0002-document-authority.md), [ADR 0004](adr/0004-document-core-identity-source-boundary.md) | [Workspace](tools/workspace/README.md), [Document Core](contracts/document-core.md) |
| 명시적 호환성 평가·누적 릴리스 | [ADR 0005](adr/0005-assessed-squash-and-batched-releases.md) — hook 경계·최초 평가 보완만 부분 대체 | [릴리스 계약](tools/release/README.md); 미소비 `.changes`는 병합할 때 임의 삭제하지 않음 |
| 기능 PR·로컬 hook | [ADR 0006](adr/0006-native-pr-workflow-mvp.md) — 후속 결정이 Actions 전용 병합과 hook 위치를 부분 대체; [ADR 0007](adr/0007-project-owned-hook-configuration.md) — 프로젝트 소유 hook | [Workflow](tools/workflow/README.md), [Git](tools/git/README.md) |
| 최초 정리 기준선 **일회성 예외** | [ADR 0008](adr/0008-initial-consolidated-baseline.md) — 최초 게시·손상된 평가 보완만 해당 | [역사적 기준선 절차](tools/workflow/initial-baseline.md); 이후 기능 통합에는 적용 금지 |
| GitHub Actions 검사 범위 | [ADR 0009](adr/0009-actions-unit-tests.md) — CI Unit 중심 | [검증 계약](testing.md#actions와-로컬-검증의-범위); 로컬 Integration과 별개 |
| PR Squash·출처·Ready | [ADR 0010](adr/0010-deterministic-pr-merge.md) — 직접 MCP 병합, [ADR 0011](adr/0011-chat-pr-lifecycle.md) — CI 관측·Ready 전환 추가 | [원격 PR 계약](tools/git/remote-pr.md); 정확한 HEAD의 CI는 에이전트가 별도 확인 |
| Worktree 확보·종료·Hooks | [ADR 0012](adr/0012-managed-worktree-finalization.md) — Native 정리, [ADR 0013](adr/0013-worktrunk-state-based-removal.md) — Worktrunk 삭제, [ADR 0014](adr/0014-worktrunk-hooks-opt-in.md) — Hooks 명시적 활성화, [ADR 0015](adr/0015-worktrunk-state-based-preparation.md) — 상태 기반 Worktree 확보, [ADR 0016](adr/0016-worktrunk-native-migration-boundary.md) — Native 기능 단위 이행 | [Native Worktree 계약](tools/git/worktrees.md), [Worktrunk 계약](tools/git/worktrunk.md); 원격 PR 병합과 별개 |

## 적용 순서

1. 루트 지침과 관련 Accepted ADR을 확인한다.
2. 변경 영역의 현재 계약·규약·코드·테스트를 대조한다.
3. 충돌 시 하위 설명만 임의로 수정하지 않고 결정 변경 여부를 확인한다.
4. 유지할 결정에 맞춰 코드·테스트·계약을 함께 갱신한다.
5. 참조·diff·검증 결과와 미검증 범위를 보고한다.
