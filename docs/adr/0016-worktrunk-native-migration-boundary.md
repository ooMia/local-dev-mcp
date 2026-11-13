# ADR 0016: Worktrunk의 기능 단위 Native 대체 경계

- 상태: Accepted
- 결정일: 2026-11-13
- 범위: LOCAL-DEV-MCP Worktree 기능의 단계적 Worktrunk 이행
- 근거: [ADR 0013](0013-worktrunk-state-based-removal.md), [ADR 0014](0014-worktrunk-hooks-opt-in.md), [ADR 0015](0015-worktrunk-state-based-preparation.md)
- 유지: Native 공개 API·운영 절차, 로컬/원격 `main` 병합 승인, PR 출처·권한·CI 판단

## 결정

Worktrunk가 동일한 **사용자 작업을 수행할 수 있는가**를 기준으로 대체한다.
Native 준비 Receipt, 해시 경로, `absent/partial/ready` 상태 머신을 새 Worktrunk 경로에 복제하지 않는다.
대신 허용된 Workspace 경로, Git 등록, 브랜치·HEAD, 개별 작업의 Clean 요건만 확인한다.

Worktrunk 기본 위치는 사용자 설정에 따라 Workspace 바깥이 될 수 있다.
따라서 Worktree를 새로 생성할 때는 검증된 상대 `worktree_path`를 절대 경로로 변환하여
`wt switch --path`에 전달한다. 옵션을 생략한 호출은 기존 해시 경로로 하위 호환을 유지한다.
기존에 등록된 Worktree는 생성 주체·경로 패턴과 무관하게 재사용할 수 있다.

| Native 기능 | 현재 Worktrunk 대체 가능성 | 판단 |
| --- | --- | --- |
| `prepare_project_worktree` | `create_project_worktrunk`의 기존 브랜치 체크아웃/재사용, 신규 생성까지 지원 | **기능 대체 가능**, 공개 입력·반환 호환은 별도 |
| `inspect_project_worktree` | Git 등록 조회와 Worktrunk 목록은 내부에 있으나 동등한 읽기 전용 공개 도구는 없음 | **조회 API 보완 필요** |
| `start_project_worktree_task` | 기존 도구의 `backend="worktrunk"`가 실제 등록 경로에서 실행 | **선택적 경로 이미 제공**; Native의 원본 Dirty Snapshot 제한은 후속 검토 |
| `finalize_project_worktree` | `remove_project_worktrunk`가 비강제 삭제·브랜치 처리를 위임 | **물리적 정리만 대체**; 병합 PR 출처 검증은 별도 책임 |
| `prepare_project_worktree_assessment`, `commit_project_worktree_assessment` | Native의 준비 기록과 경로에 직접 결합 | **미대체** |
| `inspect_project_branch_remote`, `push_project_branch`, `create_project_branch_draft_pr` | Native `require_ready`·출처와 게시 흐름을 사용 | **미대체**; 원격 안전장치·중복 PR 검사를 유지 |

다른 로컬 Git 브랜치·커밋·Cherry-pick 도구는 Worktree 생성기의 대체 범위가 아니다.
Worktrunk의 `wt merge`도 이 결정만으로 `main` 실행 경로를 얻지 않는다.

## 이행 순서와 검증

1. **현재 적용:** Worktrunk 생성/재사용/삭제와 선택적 검사 실행을 이용한다. Native API는 유지한다.
2. **다음 작은 단위:** Git 등록 정보 기반 Worktrunk 읽기 전용 상태 조회를 공개하고, 없음·정상·외부/권한 밖·등록 불일치 결과를 검증한다. 과거 Receipt를 요구하지 않는다.
3. **후속 단위:** 평가·원격 게시의 실행 대상 선택을 Native 고정 경로에서 분리하되, 정확한 SHA, Clean, PR 출처·CI·권한은 유지한다. 완료 전에는 Native 전용 API를 삭제하지 않는다.
4. **호환성 판단:** macOS 통합 테스트뿐 아니라 Windows native 실제 CLI·경로·프로세스 실행과 MCP 공개 입력/호출자 전환을 검증한 뒤, 중복 Native 엔드포인트의 퇴역을 별도 결정한다.

`wt switch --path`는 현재 실험적 인터페이스다. 사용자별 Worktrunk 경로 설정, Windows 지원,
실제 Hooks opt-in의 외부 실행까지 포함한 통합은 아직 완전히 검증되지 않았다.
이 제약은 사용자가 요청하지 않은 강제 삭제·승인 우회·원격 게시 자동화의 근거가 아니다.
