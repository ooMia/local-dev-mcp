# ADR 0005: 평가 기록이 필요한 squash와 누적 릴리스

- 상태: Partially superseded by [ADR 0006](0006-native-pr-workflow-mvp.md) — hook 경계만 대체; 명시적 평가·누적 릴리스는 유지
- 범위: 저장소의 개발·통합·호환성 평가·릴리스 준비
- 결정일: 2026-11-05

## 맥락과 결정

작업 종류와 릴리스 영향을 같은 이름으로 관리하면 작업 중 판단이 바뀌거나 여러 변경을 묶을 때 의미가 어긋난다.
작업 브랜치는 종류/주제로 구분하고, main에는 평가가 포함된 변경을 squash한다. 패키지 버전은 각 병합에서
바꾸지 않고 미소비 평가 기록을 모은 릴리스에서 한 번 갱신한다. 최대 영향과 원인을 도구로 조회할 수 있어야 한다.

Trio·Conductor의 분산 변경 기록·사전 검사·릴리스 취합 방식을 참고한다.
평가는 TOML로 기록하고 공통 내부 검사가 Git MCP와 개발 CLI에서 같은 snapshot을 검증한다.
Towncrier는 설명 렌더링, uv는 버전 원장 갱신을 맡는다. Commitizen의 메시지 기반 계산은 별도 기록 부담이
적지만 호환성 판단과 commit 문법을 결합하고, Changesets는 단일 Python 프로젝트에 다른 버전 원장을 추가하므로
이번에는 선택하지 않는다. 근거는 [조사](../research/git-workflow-enforcement-and-history-2026-11-04.md)에 둔다.

실제 공개 계약의 호환성·정정·migration·첫 Stable 선언은 사용자 또는 에이전트의 명시적 판단이다.
도구는 판단의 존재와 구조·정확한 SHA·정정 관계·누적 계산·준비 결과를 결정론적으로 검사한다.
판단 주체를 표시한 필드는 인증이나 승인 토큰이 아니다. 타입·파일명·변경량으로 의미상 호환성을 단정하지 않는다.

## 영향과 경계

현재는 초기 개발이며 0.x에서 major/minor 영향은 다음 minor로, patch는 다음 patch로 제안한다.
breaking 분류와 근거는 그대로 남기고 첫 1.0.0은 공개 계약 범위를 정한 별도 결정으로 취급한다.
기존 로컬 이력은 origin/main 위에서 의미 단위로 재구성하되 최종 tracked tree와 원본 출처를 검증·보존한다.
이후 workflow 구현은 별도 변경이다. WIP 전체를 되살려 현재 계약에 과거 기능을 재유입하지 않는다.

내부 경로는 평가가 없으면 병합을 거부한다. 로컬 hooks를 우회 불가능한 통제로 확대하지 않는다.
원격 브랜치 보호는 저장소 조건·요금제·권한이 지원할 때의 선택 사항이며 이번 결정의 동작 전제가 아니다.
원격 계정·규칙·push·publish·운영 Apply를 이 결정으로 자동 실행하지 않는다.

이 결정은 [ADR 0002](0002-document-authority.md)의 문서 권한이나
[ADR 0004](0004-document-core-identity-source-boundary.md)의 Experimental 범위를 대체하지 않는다.
개발 절차는 [conventions](../conventions.md), 실제 입력·실패 계약은
[Git](../tools/git/README.md)·[릴리스 도구](../tools/release/README.md)가 소유한다.
