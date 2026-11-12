# ADR 0013: Worktrunk 삭제 책임의 상태 기반 위임

- 상태: Accepted
- 결정일: 2026-11-13
- 범위: `remove_project_worktrunk`의 Worktree 선택·삭제·브랜치 정리
- 부분 대체: [ADR 0012](0012-managed-worktree-finalization.md)의 관리 기록·병합 PR 출처·비관리 Worktree 거부 요건을 **Worktrunk 삭제 경로에 한해** 대체한다.
- 유지: Native `finalize_project_worktree`의 기존 정책, 별도의 `main` 통합 승인·PR·품질 검증은 변경하지 않는다.

## 결정

사용자는 Worktree의 생성 주체가 아니라 **현재의 저장소·대상·권한·상태**를 기준으로 작업하기를 원한다.
Worktrunk의 삭제 가능 여부와 브랜치 정리 기준을 다시 구현하는 것은 중복이다.

`remove_project_worktrunk`는 지정한 브랜치에 대한 **단 하나의 실제 Git 등록 Worktree**를 찾고,
현재 허용된 Workspace 내부의 대상 경로이며 원본 Checkout이 아닌지 검사한다.
기존 브랜치 HEAD의 예상값도 비교하여 사용자 요청의 대상을 고정한다.
이러한 대상·권한 검증은 Worktrunk의 삭제 판단과 별개로 LOCAL-DEV-MCP가 유지한다.

그 뒤에는 Worktrunk에 비강제 제거를 위임한다. 자체 Receipt·고정 관리 경로·Dirty/ignored 판정·
별도 브랜치 통합 판정은 요구하지 않는다. 외부 도구가 만든 Worktree도 같은 범위에서 대상이 될 수 있다.
기본 브랜치 정리는 Worktrunk의 판단을 따르며, 사용자는 명시적으로 삭제 요청이나 보존을 선택할 수 있다.
Worktrunk의 결과는 실제 `branch_outcome`을 기준으로 보고하고,
CLI 오류·불확실한 JSON을 성공으로 간주하거나 맹목적으로 재시도하지 않는다.

삭제는 기존 Task Manager의 같은 서버 내 정리 잠금을 재사용한다.
범위를 벗어난 경로·연결 상태가 불명확한 대상·원본 Checkout은 삭제하지 않는다.
`--force`, `--force-delete` 및 외부 실행 Hooks는 이 결정만으로 활성화하지 않는다.

## 영향과 검증

사용자가 생성한 허용 범위 내의 Git 등록 Worktree를 별도 채택 기록 없이 제거할 수 있다.
브랜치 제거 여부는 요청값과 별개로 Worktrunk 결과에 따라 달라질 수 있으므로 이를 명시한다.
임시 저장소의 정상 제거·미병합 보존·Dirty 거부·정책 범위 밖 거부로 회귀를 확인한다.
현재 사용자 입력·결과 계약은 [Worktrunk 계약](../tools/git/worktrunk.md)이 소유한다.
