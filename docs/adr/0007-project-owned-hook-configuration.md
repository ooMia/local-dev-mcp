# ADR 0007: 프로젝트가 소유하는 Git hook 설정

- 상태: Accepted
- 결정일: 2026-11-05
- 범위: 로컬 hook 설치·우선순위·활성화 진단
- 대체: ADR 0006의 로컬 hook 설치 위치와 사용자 설정 충돌 시 동작. 나머지는 유지한다.

## 결정

사용자는 기존 시스템 설정에 의존해 설치를 거부하기보다 프로젝트 내부 설정으로 우선 적용하도록 요청했다.
Git의 repository-local 설정으로 system/global core.hooksPath를 이 저장소 안에서 override한다.
이미 worktreeConfig를 사용하는 경우 현재 worktree 설정도 맞추며, 이 extension을 새로 활성화하지 않는다.
다른 worktree의 개별 설정과 system/global 설정·원래 hook 파일은 수정하지 않는다.

hook은 공통 Git 디렉터리의 프로젝트 전용 하위 경로에 생성한다.
기존 local/worktree 값은 최초 백업으로 보존하며 반복 설치 때 최초 값을 덮어쓰지 않는다.
설치 후 실제 유효 경로·wrapper·권한을 확인한 뒤에만 활성화를 성공으로 보고한다.
동일한 읽기 전용 doctor 명령으로 미설치·설정 변경·누락된 hook·Python 경로 변경을 식별한다.

pre-commit·Lefthook 같은 hook manager로 바꾸어도 Git 설정 우선순위와 ref guard의 프로젝트 계약은 남는다.
현재 문제는 native Git의 local/worktree override로 해결할 수 있어 외부 의존성을 추가하지 않는다.
검사 recipe, PR 통합, 평가·릴리스 계산, main ref 검증 기록의 의미는 바꾸지 않는다.
CLI 계약과 제한은 [workflow 안내](../tools/workflow/README.md)가 소유한다.
