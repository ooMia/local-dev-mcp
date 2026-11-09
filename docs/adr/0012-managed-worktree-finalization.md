# ADR 0012: 검증된 병합 이후 관리형 Worktree 정리

- 상태: Accepted
- 결정일: 2026-11-10
- 범위: LOCAL-DEV-MCP가 생성한 linked Worktree의 정리와 선택적 로컬 topic 브랜치 삭제
- 보완: [ADR 0010](0010-deterministic-pr-merge.md)의 **PR 병합 도구 자체는 브랜치를 삭제하지 않는다**는 경계를 유지하며, 별도의 명시적 종료 도구를 추가한다.
- 유지: [ADR 0011](0011-chat-pr-lifecycle.md)의 CI 성공 오케스트레이션 책임 및 merge 도구 내부 강제 보류

## 결정

관리형 Worktree는 단순 임시 폴더가 아니라 Git 등록·checkout·준비 기록·브랜치로 연결된 자원이다.
기능 PR의 Squash Merge가 성공해도 소스 브랜치는 main의 조상이 되지 않으며,
Worktree에 checkout된 브랜치는 Git의 `branch -D`로도 삭제할 수 없다.
따라서 `finalize_project_worktree`를 **별도의 명시적 파괴적 도구**로 제공한다.

종료에는 관리 경로와 준비 기록의 일치, 현재 원본 branch/HEAD, 대상 branch/HEAD와 clean 상태,
진행 중 작업 없음, 저장소에 등록된 Worktree의 양방향 Git 연결을 요구한다.
GitHub PR은 동일 원격 저장소의 `main`을 대상으로 이미 `MERGED` 상태여야 하며,
PR source branch 이름·정확한 HEAD, 실제 결과 Squash 커밋의 단일 부모 및
`Source-PR`/`Source-Head` 메시지를 읽기 전용으로 검증한다.
PR 번호만으로 병합 완료를 주장하지 않는다.

작업이 진행 중이거나 대상에 추적·미추적·Git ignored 파일이 있으면 제거를 거부한다.
`git worktree remove`는 **강제 옵션 없이** 호출한다.
대상 Worktree 제거 이후의 로컬 topic 브랜치 삭제는 `delete_branch=true`를 명시했을 때만 허용한다.
브랜치가 없을 때는 삭제한 것으로 보고하지 않는다. 원격 브랜치 삭제, 강제 Git Ref 이동,
stash/reset, GitHub PR 변경과 원본 Checkout 전환은 수행하지 않는다.

사용자가 도구 바깥에서 정상 `git worktree remove`와 브랜치 제거를 이미 수행한 경우,
**실제 경로 부재·Git 등록 부재·유효한 준비 기록**을 모두 확인한 후
남은 기록 하나만 정리할 수 있다. 경로가 남았거나 Git 등록이 불확실한 `partial` 상태는 거부한다.
정리 중 중단되면 일부 Git 리소스만 남을 수 있으므로 원본 상태를 재관측하고
검증 없이 쓰기 요청을 반복하지 않는다.

이는 같은 서버 프로세스의 등록 작업 제출과의 경쟁을 제한한다.
별도 CLI·프로세스·파일 시스템 경합을 OS 수준에서 차단한다고 주장하지 않는다.
원격 PR·CI 병합 승인은 별도 계약이며, 정리 도구가 PR을 Ready로 전환하거나 병합하지 않는다.

## 적용

입력·상태·실패 이후 조사 절차는 [Worktree 계약](../tools/git/worktrees.md)이 소유한다.
새 도구에 대해 사용자 문서·회귀 테스트·서명된 기능 PR 평가를 같은 기능 단위로 갱신한다.
