# Issue #8 P3 — 모노레포 검증 결과 집계의 증거 경계

- 구분: 조사·평가. 채택된 API 변경 또는 구현 완료 선언이 아니다
- 기준: 2026-11-09 로컬 `feat/issue-8-untracked-preview`까지의 코드
- 관련 이슈: https://github.com/ooMia/local-dev-mcp/issues/8
- 소유 계약: `docs/tools/tasks/README.md`, `docs/tools/git/worktrees.md`

## 질문

여러 하위 프로젝트의 lint·test 결과를 하나의 완료 판단으로 집계할 때 현재
`get_project_task` 응답만으로 서로 다른 실행과 입력 상태를 구별할 수 있는가?

**판정: 실행 작업의 식별과 완료 관측에는 충분하지만, 특정 소스 스냅샷에 대한
재현 가능한 성공 증거로 집계하기에는 부족하다.** 따라서 별도 scheduler·저장소·
자동 집계기를 먼저 만들지 않는다.

## 확인한 메타데이터

| 구분 | 현재 제공 | 집계에서의 의미 |
| --- | --- | --- |
| 작업 | `job_id`, `request_id`, `task`, `kind`, `level` | 한 서버의 제출·조회·중복 방지 |
| 실행 위치·선택 | `cwd`, `targets_relative_to`, `targets`, `all_tests` | 하위 프로젝트와 명시 테스트 범위 구분 |
| 실행 상태 | `state`, `exit_code`, `termination_confirmed` | 프로세스 종료와 성공·취소·실패 구분 |
| 시간 | `started_at`, `finished_at` | 실행 시점 관측. 입력 동일성 증거는 아님 |
| 진단 | `output_complete`, `output_reason`, 출력 포함 여부 | 잘림·마스킹·누락된 출력의 한계 |
| 관리 Worktree 전용 | 브랜치·HEAD·원본 브랜치·HEAD·검증 상태 | Git refs의 전후 일치. 실제 import된 코드와 동일하지 않을 수 있음 |

기존 결과는 프로세스 종료 코드 0을 테스트 수·수집 성공으로 환산하지 않는다.
서버 재시작·보관 기간 만료 뒤에는 결과를 복원하지 않는다.
시작 시 고정된 recipe와 공통 환경을 사용하지만 결과에는 그 설정의 식별자가 없다.

## 반례 및 결손

1. **동일 HEAD, 다른 working tree.** 실행 뒤 소스·테스트·의존성 파일이
   unstaged로 바뀌어도 HEAD는 같다. 서로 다른 성공 결과를 같은 입력의 검증으로
   재사용할 수 없다. index 지문만으로 unstaged·untracked 내용도 구분하지 못한다.
2. **실행 설정 변경.** 서버 재시작 후 동일한 recipe 이름·`cwd`·targets로
   다른 argv·환경·설치 패키지가 실행될 수 있다. 현재 반환 필드만으로 식별하지 못한다.
3. **검사 범위 누락.** 하위 프로젝트 하나의 성공은 다른 하위 프로젝트가
   실행됐거나 동일 선택을 통과했다는 근거가 아니다. 명시 targets와 all_tests는 구분해야 한다.
4. **출력의 의미.** `output_complete=false`이거나 runner가 시험 항목·경고를 숨기면
   성공 항목 수를 확정할 수 없다. `state=succeeded`는 작업 단위에만 적용한다.
5. **Worktree import.** cwd와 Git ref 검증이 성공해도 원본 editable 가상환경으로
   원본 소스를 import할 수 있다. `worktree.verified`만으로 대상 코드 실행을 주장하지 않는다.

## 현재 적용할 수 있는 집계 방식

별도 시스템을 만들지 않고 호출자가 각 하위 프로젝트의 job을 명시적으로 추적한다.
`list_project_tasks`로 등록 범위를 확인한 뒤 고유한 `request_id`로 요청하고,
각 `job_id`의 최종 상태·exit code·선택 범위·`cwd`·출력 완전성을 함께 기록한다.
모든 필수 작업이 실제로 `succeeded`이고 필요한 출력 근거가 확보됐을 때만
**그 실행 시점의 작업 결과**를 성공으로 보고한다.

반면 **현재 작업 트리의 코드가 검증됐다**고 주장하려면 별도 입력 상태 관측이
필요하다. `get_project_pick_state`의 HEAD·인덱스·상태, 대상 경로의 현재 내용과
최초 실행 시점의 동일성까지 검증할 수 없는 경우 해당 주장은 유보한다.
사후에 HEAD만 비교하거나 Git 작업을 자동 병합해 이 결손을 메우지 않는다.

## 추가 기능을 도입할 수용 기준

실제 모노레포 사용에서 반복적으로 증거 부족이 발견될 때 다음 기준을 먼저 확정한다.

- 동일 HEAD에 서로 다른 unstaged 또는 untracked 입력이 있으면 **서로 다른**
  실행 증거로 취급한다
- 같은 이름의 recipe라도 시작 설정·runner·의존성이 달라졌으면 자동 재사용하지 않는다
- 실패·미실행·취소·만료·출력 불완전 상태를 통과로 승격하지 않는다
- 하위 프로젝트별 필수 작업 목록을 명시하고 다른 프로젝트의 결과로 대신하지 않는다
- 원본 editable 설치가 대상 Worktree 코드를 실행하는지 별도 오라클로 확인한다
- 입력 지문 수집이 보호 파일·비밀 환경을 읽거나 임의 외부 명령을 실행하지 않는다

이 기준을 검증할 충분한 실제 사용 사례가 없으므로
`config_fingerprint`·`input_snapshot`·영속 집계·병렬 scheduler 등은
**현 단계에서 구현하지 않는다**. 우선 P0의 FDE 실환경 수용 검증을 통해
필요한 최소 증거를 특정하는 편이 비용과 위험이 작다.
