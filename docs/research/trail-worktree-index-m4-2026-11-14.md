# M4 준비: 내부 Linked Worktree의 Trail 인덱스 경계

상태: **M4-P0 내부 Target Resolver + M4-P1 Worktree 전용 status/refresh API와 임시 Git/Trail 통합 검증 구현. 운영 Worktree 재배치·터널 변경은 수행하지 않음.**

## 기존 결정과 범위

- LOCAL_DEV_MCP는 시작 시 한 번 확정된 `Policy.root`의 프로젝트만 소유한다. `[workspaces].paths` 목록이나 새 MCP 서버는 추가하지 않는다.
- M1-M3의 `get_project_index_status`·`refresh_project_index`는 **기본 Checkout**에만 적용한다. 변경 시 `trail build`/단발 `--no-reuse` 복구와 선택적 on-write coalescing을 유지한다.
- M4에서는 **같은 Git 저장소에 등록된 Linked Worktree 하나를 명시적으로 선택한 경우에만** 해당 Checkout 루트를 인덱싱한다.
- 별도 Git Worktree는 독립 HEAD·index·파일 상태를 가진다. 한 저장소 아래의 폴더라고 해서 모노레포의 하위 패키지는 아니며 **그래프도 분리**해야 한다. 소스 파일과 그래프의 브랜치 간 혼합은 결함이다.

## 기존 구현에서 재사용할 기반

- `list_project_worktrees`: Git 등록 정보와 허용 경로를 기준으로 식별하는 읽기 전용 인벤토리.
- `inspect_project_worktrunk`: branch, 실제 checkout 루트, Git 등록 및 dirty 상태 확인.
- `create_project_worktrunk(worktree_path=...)`: 프로젝트 내부 상대 POSIX 경로를 지정하는 실험적 기능. 생략하면 기존 `.lab/wt/<sha256-prefix>`를 사용한다.
- Worktrunk CLI의 `wt switch --path`는 실험 기능이며 업그레이드 회귀가 필요하다. 기존 Native Worktree와 Worktrunk Backend는 삭제·대체하지 않는다.
- 기존 `TaskWorktree` 및 검증된 Git 등록·공통 Git 디렉터리·branch/HEAD 확인 계약을 활용한다. 경로 문자열만 믿고 `trail build`를 호출하지 않는다.

`.wt/<branch>`는 후보 배치일 뿐 강제 마이그레이션이 아니다. 브랜치명에 슬래시가 있고 Git 등록 경로의 충돌 가능성도 있으므로 경로는 기존 도구가 검증·확정해야 한다. `.wt/`를 사용하려면 Git ignore 및 보호 경로·symlink·점유·Windows 호환 정책도 검증해야 한다. **기존 `.lab/wt` 배치를 유지해도 M4를 구현할 수 있다.**

## Trail Code와 내부 Worktree의 주의점

- Trail Code의 기본 인덱스는 Git에 무시된 nested clone을 상위 그래프에 포함하지 않는다. `--follow-nested-repos`는 이 문제의 해법이 아니며, 작업 브랜치별로 다른 소스가 동일 그래프에 섞이면 안 된다.
- Trail Code [이슈 #453](https://github.com/trailhq/Trail-Code/issues/453): **상위 Checkout 내부의 Linked Worktree에서 명시적 dir 없이 조회하면 상위 Checkout의 그래프를 잘못 선택할 수 있음**. 2026-11-14 기준 사용자 환경의 수정 여부 미검증.
- M4 인덱싱·조회는 매번 **Git에서 검증한 정확한 Worktree 루트**를 Trail CLI 인자로 지정한다. 최초 사용 전에 그 Worktree에 `trail build <worktree-root>`를 실행해 별도 그래프를 생성하고 `--check`로 검증한다.
- **실측된 CLI 부작용:** 실제 Trail 구조 빌드는 신규 임시 Git 저장소에 untracked `.ignore`(ripgrep re-admit 파일)를 생성했다. Trail 공식 설정의 `GRAFT_NO_IGNORE=1`을 전달하면 이 생성을 막았고 **Git status Clean + Worktree별 독립 그래프** 재시험이 PASS였다. 초기 M4 실행 환경에 이 옵션을 적용할지, `.ignore`를 명시 관리할지는 별도 결정한다. 또한 `GRAFT_NO_GITIGNORE=1`을 사용한다면 `graft/` 또는 버전별 Trail 캐시의 Git ignore 상태를 별도 보장해야 한다. 실제 원본 Checkout을 이용한 시험이나 강제 cleanup은 금지한다.
- 현재 터널에서 실행 중인 `trail mcp <Policy.root>`는 **기본 프로젝트 조회**에만 고정된다. 내부 Worktree 인덱스가 생성돼도 그 터널의 Worktree별 조회가 자동 보장되지는 않는다.

## M4 제안 내부 인터페이스 (미구현)

```text
resolve_index_target(kind="project") -> Policy.root
resolve_index_target(kind="worktree", branch, expected_head)
  -> Git registered checkout within Policy.root
  -> repo-common-dir matches parent repository
  -> exact branch + HEAD + realpath + access policy checked
  -> Worktree root, distinct graph identity
```

기존 M1-M3 공개 도구에는 임의 경로 인자를 추가하지 않는다. 추후 인터페이스는 두 안 중 검증 후 결정한다.

1. **기존 status/refresh에 선택적 `branch`·`expected_head` 추가**: 도구 수 적고 하위 호환 가능하나 공개 입력 스키마 변경.
2. **Worktree 전용 status/refresh 도구 추가**: 분리된 권한·준비 상태를 더 명확히 다룰 수 있으나 도구가 늘어남.

최소 권장안은 최초 PoC에서 **내부 Target Resolver + 별도 테스트 코드**로 안전 계약을 확인하고, API 선택은 그 결과에 따라 결정하는 것이다.

최소 권장안의 첫 단계는 `src/local_dev_mcp/tools/indexing/_targets.py`의 `resolve_index_target(settings, kind, branch, expected_head)`로 구현했다. `kind="project"`는 원래 설정을 그대로 반환하고 선택자를 거부한다. `kind="worktree"`는 **Git 등록된 Worktrunk Checkout**에만 접근하며, 기존 `WorktrunkAdapter.require_ready`로 정확한 HEAD·브랜치·Git 공통 디렉터리·Clean 상태·접근 권한을 확인한다. 경로 문자열 입력은 없다.

Worktree 범위에서만 `GRAFT_NO_IGNORE=1`, `GRAFT_NO_GITIGNORE=1`을 설정해 Trail 빌드의 `.ignore` 생성 및 추적 `.gitignore` 수정을 방지한다. 설정은 기존 런타임 환경의 **스코프 복사본**에만 적용된다. 기본 Checkout의 M1-M3 환경은 변경하지 않는다. 별도 Worktree별 자동 갱신·MCP 조회 라우팅은 아직 미구현이다.

`tests/mcp/test_trail_m4_index_targets.py`는 임시 Git 저장소의 동일 `marker()` 심볼에 서로 다른 `M4_ONLY_MAIN_71`·`M4_ONLY_FEATURE_89` 문자열을 넣고 각 루트에 대해 `trail grep ... <explicit-root>`를 실행한다. **실제 양쪽 조회 PASS, 교차 조회 오인 0건**, Git status Clean, HEAD 유지, 잘못된 HEAD·Dirty Worktree·권한 부족 거부를 확인했다. 초기 실행에서는 기본 Checkout의 `.ignore`가 생성돼 테스트가 FAIL이었으며, 테스트의 기본 Checkout 빌드에도 명시적으로 `GRAFT_NO_IGNORE=1`을 주입하자 PASS가 됐다. 운영 프로젝트의 기본 설정은 바꾸지 않았다.

**P0 미검증:** 상위 그래프 선택을 유발하는 무인자 CLI의 이슈 #453 실재 재현, 여러 동시 대상의 잠금 공유, 타임아웃/외부 프로세스 경합, Windows native, MCP 공개 API 설계·조회 전환. P0 PASS는 M4 전체 완료로 간주하지 않는다.

## M4-P1 상태·갱신 API 구현 및 검증

기존 M1-M3 공개 입력 스키마는 그대로 두고 `get_project_worktree_index_status(branch, expected_head)`, `refresh_project_worktree_index(branch, expected_head)` 두 도구를 추가한다. P0 Resolver는 읽기와 쓰기 권한을 분리하고, `WorktreeIndexService`는 등록된 실제 루트 경로별로 서버 로컬 `asyncio.Lock`을 공유한다. 대상 선택 직후와 잠금 내부, Trail 명령 완료 후 Git 등록·HEAD·Clean 상태를 다시 확인한다. 호출별 `IndexManager`를 종료하며 임의 경로·`--deep`·Worktree 자동 갱신은 허용하지 않는다.

임시 저장소의 두 Linked Worktree에서 실제 Trail 구조 그래프를 생성하고 동일 심볼의 두 값이 섞이지 않는지 검증했다. 같은 Worktree의 두 `refresh` 동시 호출은 한 번 빌드 후 다른 호출이 `already_fresh`를 확인하도록 직렬화된다. 상태 읽기는 쓰기 권한 없이 허용하며, 잘못된 SHA·Dirty 대상·실행 도중 변경된 Git 상태는 거부한다.

**P1 잔여 범위:** 독립 MCP 서버 간 공유 잠금은 없고, 외부 Trail 실행의 경합·타임아웃 복구·Windows native 호환성·Trail 터널에 Worktree별 탐색 라우팅은 검증되지 않았다. 따라서 M4 전체 완료나 모든 환경에서의 그래프 격리를 주장하지 않는다.

## M4-P2: 실패·타임아웃 복구 회귀

`tests/mcp/test_trail_m4_failure_recovery.py`는 임시 Git Linked Worktree와 공개 MCP API를 대상으로 다음 4개 테스트를 추가한다.

- Trail 미설치 `unavailable`, `--check` 타임아웃의 빌드 차단, 구조 빌드 타임아웃 후 명시 재시도 및 Fresh 판정
- 실행 중 갱신 취소 이후 서버 로컬 Worktree 잠금 재획득과 재실행
- 실제 하위 Python 프로세스의 시간 초과·출력 128 KiB 상한에 따른 정규화된 오류 보고와 정리 경로
- 비밀이 포함된 모의 CLI 출력의 비노출, 임시 Git HEAD·Clean 상태 유지

4개 테스트 PASS는 **M4-08 중 서버 로컬 실패 복구의 수용 기준**으로 해석한다. 다른 서버·외부 Trail 프로세스의 동시 접근과 후손 프로세스 정리는 별도 미해결 과제이며 Windows native 실증을 대체하지 않는다.

## M4-P3: 다중 MCP 인스턴스의 Trail 빌드 경합 재현·차단

`tests/mcp/test_trail_m4_interprocess_lock.py`에서 독립된 `ServerSettings` 두 개가 같은 Linked Worktree를 갱신하면 **최대 2개의 구조 빌드가 중첩되는 실패**를 구현 전에 재현했다. P1의 `asyncio.Lock`은 인스턴스별이기 때문이다.

P3에서는 공통 Git 제어 디렉터리에 대상 절대 루트 SHA-256에서 파생한 잠금 파일을 두고 POSIX `flock`, Windows `msvcrt.locking`으로 협력하는 LOCAL_DEV_MCP 프로세스를 직렬화한다. 파일은 삭제하지 않으며 실제 잠금 소유권은 OS 파일 디스크립터에 귀속된다. 비차단 재시도·360초 대기 상한·취소·프로세스 종료 시 OS 해제를 사용한다. 잠금은 대상 재검증부터 Trail의 체크/빌드/재검증까지 보유한다.

수정 뒤 독립 서버 객체 간 중첩 빌드 수는 1로 감소했고, 실제 두 Python 프로세스의 진입/종료 순서도 직렬화를 보였다. 강제 프로세스 종료 뒤 즉시 새 프로세스가 잠금을 획득했으며, 지정된 대기 제한에서 실패를 반환하고 서로 다른 루트에서는 동시 진행 가능함을 검증했다. Checkout Git status는 Clean이고 HEAD도 유지됐다.

**한계:** 합의한 OS advisory lock에 참여하지 않는 외부 `trail build`/터널, 원격 파일 시스템의 잠금 신뢰성, Windows native 실측은 검증 대상이다. Git common directory가 승인된 프로젝트 밖에 있으면 실패로 처리하므로 외부 `--separate-git-dir` 구성은 이 버전에서 지원하지 않는다. 모든 외부 Trail 접근의 안전성까지 증명한 것은 아니다.

## M4-P4: 명시적 Worktree 루트의 Skeleton·Callers 조회 검증

공식 Trail Code CLI 계약의 `trail skeleton <file> [dir]`·`trail callers <symbol> [dir]`를 실제 설치된 CLI로 시험했다. `tests/mcp/test_trail_m4_index_targets.py`는 기본 Checkout과 Git Linked Worktree에 이름이 같은 `marker()`를 두되 호출자 함수만 `call_main()`·`call_feature()`로 구분한다.

각 대상에 정확한 절대 루트를 전달한 `grep`·`skeleton`·`callers` 결과를 교차 비교해 서로 다른 브랜치의 소스·호출자가 섞이지 않음을 확인했다. 두 Git HEAD와 Clean 상태가 그대로 유지됐고 실제 Trail 통합 테스트가 PASS였다. 이는 M4-03, M4-12의 명시적 루트 조회 경로에 대한 회귀 증거다.

**남은 M4-04:** 루트 인자를 생략한 CLI가 상위 그래프를 고르는 Trail 이슈 #453의 재현 여부는 이번 테스트로 판정하지 않았다. 운영 경로는 항상 검증된 루트를 전달하므로 이 동작에 의존하지 않는다.

## 성공 기준 및 최소 회귀 매트릭스

| ID | 검증 상황 | 수용 기준 |
| --- | --- | --- |
| M4-01 | 기본 Checkout 조회·갱신 | 기존 M1-M3 입력/결과·Fresh 판정 동일 |
| M4-02 | 임시 저장소의 `.wt` 내부 Linked Worktree | Git 등록·정확한 branch/HEAD 확인 후만 해당 루트 갱신 |
| M4-03 | 기본 Checkout과 Worktree의 동일 심볼을 서로 다르게 수정 | 각 구조 그래프가 해당 코드만 포함하고 cross-branch 오인 0건 |
| M4-04 | Nested Worktree에서 명령 실행 | CLI root를 명시하지 않은 경우의 #453 회귀를 재현·명시 root 사용으로 우회 검증 |
| M4-05 | 다른 저장소 링크·허용 Workspace 밖·symlink·경로 조작 | 모두 거부. 임의 절대 경로 인자 없음 |
| M4-06 | 중복 브랜치·이동된 HEAD·삭제/Dirty Worktree·등록 불일치 | 현재 상태 불확실 시 갱신 차단, 기존 파일 보존 |
| M4-07 | 두 Worktree 동시 수정·빌드 | 대상 루트별 갱신 직렬화, 교차 인덱스 오염 없음 |
| M4-08 | Trail 미설치·빌드 타임아웃·실패 | 원본 파일 변경 성공 유지, Fallback 명시 |
| M4-09 | `.wt/` ignore와 `graft/` 자동 ignore 처리 | 기본 Checkout 및 Worktree의 Git status가 의도치 않게 변경되지 않음 |
| M4-10 | Windows native·macOS | 경로·실행 파일·대소문자·권한·동시성 계약 검증 |
| M4-11 | 여러 LOCAL_DEV_MCP 인스턴스 | 각 인스턴스의 Policy.root와 Worktree만 접근·조회 |
| M4-12 | 전체 품질 검사와 실제 탐색 | Ruff/Pyright/Unit/Integration, Trail `skeleton`·`callers` 소스 근거 일치 |

검증은 사용자 저장소가 아니라 **테스트 임시 Git 저장소 및 Linked Worktree**에서 수행한다. 변경 로깅은 Git SHA·Worktree identity·실제 Trail 결과로 근거를 남긴다.

## M4 실행 전 게이트

1. M1-M3 패치가 main에 반영되고, 실행 중인 LOCAL_DEV_MCP 서버가 재시작되어 실제 `get_project_index_status`가 구조 Fresh를 반환할 것.
2. 실제 Trail CLI + 임시 Workspace E2E에서 M1·M2·M3 on-write 변경을 재현할 것 (자동 검증 추가).
3. 사용자 미커밋 `config.toml`과 기존 Worktree를 보존하고, 원격 게시·main 병합 시 기존 PR/CI 정책 준수.
4. `.wt`로 모든 Worktree를 옮길 필요가 있는지, 현행 `.lab/wt`를 유지해도 되는지 검증 후 결정.

참고: [Git Worktree 공식 설명](https://git-scm.com/docs/git-worktree), [Trail Code 구조/중첩 저장소](https://github.com/trailhq/Trail-Code/blob/main/README.md), [Nested Worktree 문제 #453](https://github.com/trailhq/Trail-Code/issues/453), [기존 Worktrunk 계약](../tools/git/worktrunk.md).
