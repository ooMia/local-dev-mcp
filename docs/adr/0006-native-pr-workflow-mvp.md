# ADR 0006: 기본 PR 기능과 로컬 품질 게이트로 구성한 MVP

- 상태: Partially superseded by [ADR 0007](0007-project-owned-hook-configuration.md) — 로컬 hook 설치·설정만 대체
- 결정일: 2026-11-05
- 범위: 개발 과정 참조, 기본 브랜치 품질, 릴리스 배포물
- 대체: ADR 0005의 hook 강제성 확대 제외 부분. 명시적 평가와 누적 릴리스 결정은 유지한다.

## 결정과 이유

사용자는 추가 계획보다 직접 작성하는 코드를 최소화한 작동 가능한 MVP를 우선하도록 지시했다.
기본 브랜치의 기능별 결과와 개발 과정 참조는 GitHub PR + Squash로 구현한다.
원본 보존용 태그, 전용 GitHub App, 평가 기록의 trailer 이주는 도입하지 않는다.
기존 TOML 평가·Towncrier·uv의 누적 릴리스 흐름을 재사용한다.

프로젝트의 통합 CLI가 GitHub에서 관측한 PR URL과 head SHA를 최종 squash 메시지에 넣고,
head가 바뀌면 GitHub CLI의 조건부 병합이 거부하게 한다.
PR의 개발 기록을 외부 사용자가 결과 커밋에서 찾아갈 수 있게 한다.
임시 평가 파일을 모든 과거 Git tree에서 제거하는 조건은 추가하지 않는다.
릴리스 준비 시 평가를 소비하며, 최종 wheel과 sdist에는 내부 평가·영수증이 포함되지 않아야 한다.

로컬 품질 검사는 config.toml에 이미 등록된 다섯 비수정 검사 recipe를 재사용한다.
Git hook은 검사에 실패한 기본 브랜치 commit과 검증되지 않은 ref 이동을 거부한다.
검증 기록은 공통 Git 디렉터리 안에 두고 기준 SHA와 검사한 tree/후보 SHA를 결합한다.
이 기록은 인증 토큰이 아니다. 같은 OS 계정이 hook 또는 정책 코드를 제거하는 행위까지 방어하지 않는다.
ref 거부가 작업 파일·index를 복원한다는 보장도 추가하지 않는다.

원격 CI와 ruleset의 입력은 저장소에서 관리한다. 계정 권한·지원 기능과 실제 활성화는 별도로 검증한다.
로컬 검증 성공을 원격 설정 또는 병합 성공으로 표현하지 않는다.
통합은 main의 workflow_dispatch와 기존 GITHUB_TOKEN으로 실행한다.
품질 gates에는 bypass를 부여하지 않고 별도 writer 규칙으로 Actions App만 main을 갱신하도록 구성한다.
이에 따라 일반 사용자가 GitHub UI에서 출처를 삭제한 메시지로 병합하는 경로를 제한한다.
새 App 등록이나 외부 서비스 없이 native 기능을 조합하며, 실제 계정에서의 동작은 활성화 수용 검증으로 확인한다.

현재 명령과 운영 경계는 [workflow 계약](../tools/workflow/README.md)이 소유한다.
