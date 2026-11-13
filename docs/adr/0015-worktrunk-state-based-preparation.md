# ADR 0015: Worktrunk Worktree의 현재 상태 기반 확보

- 상태: Accepted
- 결정일: 2026-11-13
- 범위: `create_project_worktrunk`의 생성·기존 브랜치 체크아웃·재사용
- 보완: [ADR 0013](0013-worktrunk-state-based-removal.md)의 생성 주체 비의존 원칙을 Worktree 확보에도 적용한다.
- 유지: [ADR 0014](0014-worktrunk-hooks-opt-in.md)의 호출별 Hooks Opt-in, Native Worktree·PR·Main 통합 정책은 변경하지 않는다.

## 결정

Worktree 확보의 결과는 사용자가 선택한 저장소·브랜치가 접근 가능한 실제 Git 등록 Worktree에 연결되는 것이다.
LOCAL-DEV-MCP가 직접 생성했는지, 기존에 다른 도구로 만들었는지 또는 별도 Receipt가 있는지는
재사용의 필수 조건이 아니다. 이미 등록된 브랜치가 접근 가능한 Worktree를 가지면 해당 경로를 재사용한다.
미커밋 변경은 보존하며, Clean이 필요한 후속 작업의 실행 가능성과 재사용 자체를 구분한다.

브랜치는 있지만 Worktree가 없다면 Worktrunk의 `wt switch <branch>`에
명시적 허용 경로·`--no-cd`를 제공해 체크아웃한다. 브랜치가 없다면 기존 `wt switch --create`
방식으로 생성하며, 이 경우에는 기본 Ref의 정확한 예상 SHA를 반드시 요구한다.
기존 브랜치의 예상 SHA는 선택적으로 지정할 수 있고 지정했다면 현재 값과 일치해야 한다.
이미 존재하는 브랜치를 새 브랜치로 덮어쓰지 않으며, 관리 경로가 점유된 경우에도 덮어쓰지 않는다.

기존 Worktree의 재사용과 새 체크아웃은 저장소의 Git 등록 정보·Worktree 연결·Workspace 접근 정책으로
확인하며, 기존 브랜치의 신규 체크아웃에는 대상 Git Tree의 보호 경로 검사도 적용한다.
현재 원본 Checkout·기존 데이터·다른 저장소/권한 밖 Worktree를 변경하거나 임의로 채택하지 않는다.
생성·체크아웃에는 [Hooks Opt-in 결정](0014-worktrunk-hooks-opt-in.md)을 유지한다.

## 영향과 검증

결과의 `action`은 `created`, `checked_out`, `reused`로 구분한다.
기존 체크아웃이 조건을 충족하면 외부 `wt` 호출 없이 결과를 반환한다.
Worktrunk 명령이 중단되거나 응답이 불확실하면 현재 Git 상태를 재관측하고, 쓰기를 맹목적으로 반복하지 않는다.
실제 테스트는 일회용 Git 저장소에서 생성, 재사용, 기존 브랜치 체크아웃, 권한 거부 및 충돌을 검증한다.
공개 입력·결과의 소유 계약은 [Worktrunk 문서](../tools/git/worktrunk.md)에 둔다.
