# 로컬 이력 재구성

`consolidate_project_history`는 검토한 기능 경계의 Git tree를 재사용해 새 `consolidated/<topic>`
브랜치에 의미 단위 커밋을 만든다. 기존 브랜치와 원본 커밋을 보존하며 checkout·index·main·원격을 변경하지 않는다.
이 도구는 후보 이력 생성만 맡는다. main 채택·게시·릴리스는 별도 작업이다.
이 저장소의 최초 정리 기준선은 [ADR 0008](../../adr/0008-initial-consolidated-baseline.md)의 한정 절차를 따른다.

## 입력과 결과

- `expected_branch`, `expected_head`: controller checkout의 브랜치와 관측한 전체 SHA. 기본적으로 clean checkout을 요구한다.
- `allow_dirty_controller=false`: 기본값. `true`로 명시한 경우에만 기존 staged·unstaged·untracked 변경을 유지한 채 후보 생성 가능. 이 옵션은 checkout을 변경하거나 사용자 변경을 commit하지 않는다.
- `base_head`: 원본 이력의 출발점인 전체 SHA. 현재 HEAD까지 최대 64개 연속 단일 부모 커밋이어야 한다.
- `new_branch`: 존재하지 않는 `consolidated/<topic>` 브랜치.
- `units`: 1~16개의 `source_head`·`message` 객체. 끝 지점은 중복 없이 원본 순서를 따르고 마지막은
  `expected_head`와 같아야 한다. 각 구간은 실제 tree 변경을 포함해야 한다.
  메시지는 Conventional Commit 형식이며 NUL과 예약된 출처 trailer를 허용하지 않는다.

각 새 커밋은 직전 후보 커밋을 부모로 두고 해당 원본 끝 지점의 tree를 그대로 사용한다.
따라서 파일 모드·바이너리·symlink·삭제·기존 평가 기록도 Git 객체 수준에서 동일하게 유지된다.
이력 재구성은 기존 평가의 호환성 판단을 새로 추정하거나 평가 파일을 정정하지 않는다.
메시지의 `Source-Base`, `Source-Head`, `Source-Tree`가 원본 구간과 snapshot을 가리킨다.
원본 브랜치를 로컬에 보존해 구간 내 개발 기록도 조회할 수 있게 한다.
원본 브랜치를 원격에 게시하지 않았다면 이 출처 SHA를 새 원격 clone에서 조회할 수 없다.
출처 trailer 자체는 원본 객체의 원격 보존이나 fetch 가능성을 보장하지 않는다.

결과는 새 브랜치·HEAD, 원본 구간별 새 커밋·tree, 서명 및 checkout 보존 검증 여부를 반환한다.
Git commit identity와 서명 준비는 [기본 Git 계약](README.md)을 재사용한다.
서명을 설정한 경우 모든 후보 서명이 설정한 키와 일치해야 최종 ref를 생성한다.
선택한 snapshot 사이에 보호·비공개 경로 변경이 있으면 전체 작업을 거부한다.

## 검증과 실패 경계

`commit-tree`는 일반 commit hooks를 실행하지 않는다. 이 도구의 성공은 품질 승인이나
`Source-PR`를 요구하는 [정상 main 통합](../workflow/README.md)의 대체 증거가 아니다.
후보 채택 전 별도 로컬 clone에서 각 후보 커밋을 checkout하고 해당 snapshot의 lock과
[검사 절차](../../testing.md)에 따라 검증한다. clone의 원격 연결을 제거한 뒤 검증에 사용한다.
`consolidated/*`는 현재 관리 Worktree의 topic 이름 규칙에 포함되지 않으므로 그 경로로 준비할 수 없다.
원본 최종 tree와의 일치 및 변경 범위의 의미도 검토한다.
릴리스 초기 기준처럼 조상 관계를 요구하는 설정도 재구성한 이력에서 확인한다.

원본 상태를 반복 확인한 뒤, 기존 ref가 없다는 compare-and-create 조건으로 최종 브랜치 하나만 생성한다.
`allow_dirty_controller=true`는 변경 경로의 status·index와 읽기 허용된 dirty 파일 내용의 SHA-256을 작업 전후 비교하며, 내용이나 브랜치·HEAD가 달라지면 후보 ref를 만들지 않는다.
경로 256개, 일반 파일 합계 64 MiB를 넘거나 금지·보호·특수 경로는 안전하게 거부한다. 무시된 파일은 Git status의 관측 범위가 아니고 동시 OS 변경을 원자적으로 격리하지 않는다.
다른 Worktree 작업이나 원격 게시에 이 옵션이 암묵적으로 전파되지는 않는다.
동시 생성된 브랜치와 dangling symbolic ref를 덮어쓰지 않으며 다른 ref로 간접 쓰기하지 않는다. 실패 시 ref가 없는 중간 commit 객체가 남을 수 있고,
ref 생성 후 응답 유실·관측 실패 시 브랜치가 이미 존재할 수 있다.
`get_project_ref`, `get_project_status`로 결과를 관측하며 같은 이름을 덮어쓰거나 자동 reset하지 않는다.
