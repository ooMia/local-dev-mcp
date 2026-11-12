# 공통 품질 프로파일과 Worktrunk 실행 경계

## 단일 검사 원본

- `config.toml`의 `[jobs.<name>]`만이 검사 `argv`, `permission`, `cwd`, `timeout_seconds`, `default_targets`를 소유한다.
- `.config/quality.toml`은 이름이 등록된 **비수정 검사**만 선택한다. `pre_merge`는 `format_check → lint → typecheck → unit → integration` 순서로 실행한다.
- 명세는 schema 1, 중복 이름·누락 recipe·수정형 `format`·미등록 검사를 거부한다.
- `require_clean=true`면 직접 CLI 실행 전후 Git HEAD 및 Working Tree 상태를 확인한다.

## 직접 Worktrunk CLI

`.config/wt.toml`의 `[pre-merge]`는 다음 공통 진입점을 호출한다.

```sh
uv run --locked python -m local_dev_mcp.quality --profile pre_merge
```

직접 CLI는 검사 실패를 종료 코드로 반환하여 Worktrunk가 로컬 병합을 중단하도록 한다.
프로젝트 Hooks는 사용자별 Worktrunk 승인이 필요하다. `wt config approvals add`에서 명령을 직접 검토한다.
에이전트는 승인 대신 `--yes`를 자동으로 사용하지 않는다.
`--no-hooks`, `wt step push`, 직접 Git 조작은 이 경로를 우회할 수 있다.

## LOCAL-DEV-MCP

MCP는 Worktrunk Hook을 실행하지 않는다. `list_project_tasks`가 `profiles_status`, 검사 순서, Manifest Digest와
각 등록 작업의 준비 상태를 보여준다. 호출자는 그 순서대로 기존 `start_project_task`에 제출한다.
`profiles_status=invalid`는 거부 상태이며 `absent`와 혼동하지 않는다.
Task Manager의 작업 ID·허용 명령·출력 마스킹·취소·작업 슬롯을 재사용한다.
`start_project_worktree_task(backend="worktrunk")`는 `create_project_worktrunk`가 생성한
`.lab/wt/`의 Git-등록 Worktree를 선택한다. 원본 controller의 관측 HEAD·미커밋 파일 지문과
실행 대상의 브랜치·HEAD·Clean 상태·Git 포인터를 전후 확인한다.
`backend="native"`는 종전 `.lab/worktrees` + Receipt 방식 그대로 유지한다.
Worktrunk 실행 경로는 추가 `wt` 논리 권한이 필요하고, Task 응답의 `worktree.backend`로 구분된다.
작업의 프로세스 종료 코드가 0이어도 `worktree.verified=false`면 검증 실패로 취급한다.
`get_project_task`의 exit=0은 성공적인 프로세스 종료를 뜻할 뿐 **검사 대상 Git 상태의 증거가 아니다.**
`verify_project_worktrunk_quality`는 Worktrunk의 Clean Source와 동일한 품질 명세를 확인한 뒤,
프로파일의 필수 Job ID를 순서대로 검사한다. 각 Job은 같은 Source HEAD·Worktree·Task 정의와
동일한 원본 Checkout Branch·HEAD, 등록 작업의 Kind·Level·CWD에 결합돼야 하며,
테스트는 `all_tests=true`와 전체 기본 Target이 필요하다. Source Tree·Target HEAD·검사 명세/Recipe Digest를
하나의 서버 로컬 증거로 발급한다. 저장된 Job이 만료되거나 서버가 재시작되면 증거도 무효다.
단, 범용 등록 명령의 종료 코드 0은 실제 테스트 수집 개수의 보증은 아니다. pytest의 미수집 종료 코드처럼
명령 실행기에서 실패가 보고돼야 거부할 수 있으며, 추후 테스트 수집을 증명하는 별도 계약이 필요하다.
증거 발급과 로컬 `main` 통합 승인은 독립적이며, **증거 단독으로 Git Ref를 갱신할 수 없다.**
`get_project_worktrunk_merge_plan`은 증거 재조회, Source/Target HEAD·Tree, Manifest/Recipe 및
Clean `main` Worktree를 검사한 뒤 `approval_required`와 검토용 Digest를 반환한다.
Worktrunk MCP는 증거와 검토 계획에서 종료하며 `main` 병합 도구를 등록하지 않는다.
이후 GitHub PR·CI·사용자 승인 또는 별도의 사용자 통제 Git 작업은 독립적인 실행 경로에 속한다.

기존 로컬 `main` 승인 및 원격 PR/CI 계약은 이 프로파일로 대체하지 않는다.
기존 `historical_*` 검사는 명시적으로 선택하는 과거 이력용이며 `pre_merge`의 자동 fallback이 아니다.

## 권한과 환경

`config.toml`에는 임의 명령이 정의될 수 있으므로 직접 CLI는 사용자가 신뢰한 프로젝트 환경에서만 실행한다.
MCP는 각 Task의 논리 권한을 다시 확인해야 하며, 품질 명세 파일이 권한을 추가하지 않는다.
검사 파일 내용 변경 시 Worktrunk 승인 명령 문자열이 그대로일 수 있어 승인을 새로 요구하지 않을 수 있다.
보안 경계는 Worktrunk의 Hook 승인 단독이 아니라 MCP의 도구·경로 권한 및 원격 보호 정책이다.

## 검증

- `tests/quality/test_manifest.py`: 명세 파싱과 등록 작업 선택
- `tests/quality/test_cli.py`: 임시 Git 저장소에서 직접 실행과 차단
- `tests/quality/test_worktrunk_hook.py`: 실제 `wt hook show`가 Hook 설정을 인식하는지 점검 (설치된 플랫폼)

Worktrunk를 호출해도 실제 Git Tree·Refs·사용자 파일 상태를 검사해야 하며, 특정 Git 서브커맨드 호출을
공개 Facade의 성공 기준으로 삼지 않는다.
