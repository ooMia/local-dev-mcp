# 검사 실행과 테스트 환경

테스트 배치·명명·격리·순차 실행 판단은 [개발 규약](conventions.md#타입과-테스트)이 소유한다.
이 문서는 검사 명령과 테스트 환경의 준비·정리·증거 범위를 설명한다.

## 서버 기능과 외부 클라이언트 검증

서버 기능은 제어 가능한 MCP client로 요청·정상 응답·입력과 권한 거부·실제 파일 및 Git 상태를 검증한다.
서버가 도구 결과를 반환했다는 사실과 클라이언트가 그 결과로 사용자 작업을 완료했다는 사실은 구분한다.

[ChatGPT E2E](../GLOSSARY.md#language)는 외부 클라이언트에서 수행하는 선택적 수용 검증이다.
그 [사용 확인 절차](../tests/e2e/README.md)는 연결 환경이나 실제 사용 흐름을 확인할 때 적용하며,
서버 기능 변경의 필수 완료 조건이나 서버가 스스로 수행할 기능으로 두지 않는다.

공통 [workflow 검사](tools/workflow/README.md)는 아래 등록된 비수정 검사들을 재사용한다.
실제 hook·다중 커밋 출처·배포 archive 검사는 tests/tools/test_workflow*.py가 담당한다.
GitHub ruleset payload·조건부 병합의 경계 테스트와 실제 원격 활성화 검증은 구분한다.

## 실행 경로

```sh
uv sync --locked
uv run --locked pyright
uv run --locked ruff check src tests conftest.py
uv run --locked ruff format --check src tests conftest.py
uv run --locked pytest                         # 일반 회귀
uv run --locked pytest -n 0 -m unit            # 독립 데이터·callable 검사
uv run --locked pytest -n 4 -m 'integration and not packaging'  # 실제 자원·protocol 검사
uv run --locked pytest -n 0                    # pytest 항목의 순차 재현
uv run --locked pytest -n 0 --durations=15      # setup/call/teardown 병목 확인
uv run --locked pytest -n 0 -m packaging        # 설치 패키지 검증
```

pytest 기본 선택은 `pyproject.toml`을 따르며 packaging·e2e를 제외한다. 각 항목은 unit·integration·e2e 중
정확히 하나를 선언해야 하며, 수집 단계에서 누락·중복을 거부한다. 위치만으로 계층을 추정하지 않는다.
worker 수는 `-n 2`처럼 조절하며, `-n 0`과 `-m packaging`은 해당 기본 옵션을 덮어쓴다.
`-m integration`만 지정하면 packaging도 포함되므로 독립 실행할 때는 위 선택식을 사용한다.
일반 회귀는 개발 환경의 설치·진입점을 사용하므로 별도 가상환경을 수동 준비하지 않는다.
Pylance와 CLI의 버전별 진단은 다를 수 있으며 저장소의 타입 검사 기준은 lock된 CLI다.
위 unit 프로필은 실행 준비 비용을 줄이려고 xdist worker를 끈다. 테스트 간 독립성과 병렬 유효성은 계속 요구한다.
일반 회귀의 기본 worker 수는 `pyproject.toml`을 그대로 따르며 프로필별 선택과 구분한다.

[workspace 선택 검사](../tests/system/test_workspace_selection.py)는 호출 위치와 대상에 별도 저장소·설정을 준비하고
실제 CLI·stdio의 읽기·Git 작업이 선택한 대상에만 적용되는지 확인한다.
[connect 검사](../tests/app/test_workspace_connect.py)는 실제 사전 검사와 테스트 전용 Tunnel 실행 파일을 사용해
대상 경로 전달과 자식의 설정 로딩을 확인한다. 이 로컬 검사는 원격 인증이나 실제 Chat 연결의 증거가 아니다.

[수정 계약 검사](../tests/tools/workspace/test_editing.py)는 실제 파일·MCP를 사용해 관측 구간 교체, 빈 파일,
원문·mode 보존, 연속 참조, 동일 파일 동시 요청과 정책 거부를 검증한다.
사용자가 구간 안·밖을 바꾸거나 앞에 줄을 삽입하면 기존 참조가 거부되고, 현재 내용을 다시 읽어 재개할 수 있어야 한다.
[실제 stdio 수정 흐름](../tests/system/test_workspace_editing.py)은 CLI 자식 프로세스에서도 같은 계약을 확인한다.
[경로 정책 검사](../tests/test_path_policy.py)와 [Git 공개 범위 검사](../tests/tools/git/test_path_visibility.py)는
읽기 전용과 접근 금지를 구분하고, 제한된 경로가 결과에서 제외될 때 불완전함을 표시하는지 확인한다.

## MCP로 등록 검사 실행

검사 recipe 등록은 [설정 예시](configuration.md#cli-작업-설정), 선택 실행과 결과 해석은
[CLI 작업 계약](tools/tasks/README.md)을 따른다. 새 테스트를 작성한 뒤 구체적인 파일·node ID를 `targets`로 선택하고,
전체 프로필은 `all_tests=true`로 별도 요청한다. worker·marker 옵션을 MCP 호출에 자유 문자열로 덧붙이지 않는다.

unit은 `-n 0`, integration은 `-n 4`를 recipe의 고정 옵션으로 구성한다. target 프로젝트 E2E는 그 runner의
선택·병렬 설정을 사용하며, 이 저장소의 실제 ChatGPT 수용 검증을 로컬 CLI 실행 결과로 대체하지 않는다.
runner의 선택 대상·exit code·보호된 진단을 함께 확인하고, 진단이 보류됐거나 테스트가 수집되지 않은 결과를
전체 회귀 성공으로 해석하지 않는다. timeout·취소 후에는 cache·report·format 변경이 남을 수 있다.

반복 개발용 recipe는 `--verbosity=-1 --tb=short --no-header -rfE`로 성공 항목별 출력을 줄인다.
`--show-capture=no`·`--no-summary`로 실패 근거를 없애거나, 출력 축소를 이유로 경고·fail-fast 정책을 바꾸지 않는다.
MCP의 기본 시작·조회·취소 응답은 진단을 생략하므로 실패 원인은 `get_project_task(job_id, include_output=true)`로 확인한다.
`output_included`는 표시 선택이고 `output_complete`·`output_reason`은 실제 수집·보호·잘림 상태다.

설정 parser와 정책 편집 opt-out은 [설정 값 검사](../tests/test_settings_values.py),
[실제 경로 정책 검사](../tests/test_settings.py), [CLI·Tunnel 전달 검사](../tests/app/test_workspace_connect.py)에서 확인한다.
stdout·stderr의 알려진 값 치환과 바이트 경계·상한은 [출력 처리 검사](../tests/test_redaction.py),
실행 수명·중복 제출·취소는 [실제 작업 실행 검사](../tests/tools/tasks/test_task_execution.py)에서 확인한다.
[공개 MCP 계약](../tests/mcp/test_tasks.py)과 [실제 stdio 작업 흐름](../tests/system/test_task_stdio.py)은
도구 등록·출력과 CLI 시작 경계의 연결을 검증한다.

## 작은 개발 작업의 완료

[파일 생성·삭제 검사](../tests/tools/workspace/test_file_mutations.py)는 기존 경로 충돌, 명시적 부모 생성,
관측 뒤 사용자 변경, 직접 쓰기 정책, 요청 재전송과 부분 적용을 검증한다.
[개발 흐름 검사](../tests/system/test_development_cycle.py)는 별도 workspace의 실제 CLI·MCP에서 새 소스·테스트 생성,
선택 테스트 실패, 구현·재관측, 검사 성공과 변경 검토까지 연결한다. 원본이 바뀐 삭제나 응답 유실 뒤 재전송이
새 사용자 파일을 덮어쓰거나 다시 삭제하지 않아야 한다.

기본 완료는 검증된 working-tree 변경과 검토 가능한 diff다. 새 파일 원문은 읽기로 확인하고,
staged diff가 필요하면 대상 파일만 명시적으로 stage한다. 기존 사용자 staging을 자동 포함하거나 commit하지 않는다.
format은 원문을 바꿀 수 있으므로 후속 편집 전 다시 읽어 새 `read_ref`를 받는다.
이 로컬 검사는 실제 ChatGPT 세션에서 같은 도구를 사용했다는 증거와 구분한다.

## 의존 시나리오 실행과 보고

실제 stdio·Git의 [의존 시나리오](../tests/system/test_dag_workflow.py)는 discovery 뒤 독립 조회,
stage 뒤 index 관측, commit 뒤 HEAD·상태·원문 관측을 연결한다.
공유 Client의 준비·정리는 시나리오의 owner task가 소유한다. DAG 노드는 준비된 Client를 사용한다.
저장소 루트의 `conftest.py`가 [DAG pytest adapter](../tests/support/pytest_dag.py)를 먼저 로딩한다.
adapter가 옵션·fixture·노드 보고를 소유하므로 보고서 디렉터리가 이미 존재해도 테스트 경로를 생략한 아래 명령을 사용할 수 있다.

```sh
uv run --locked pytest -n 0 -m dag --dag-workers 4 --dag-report .lab/dag
uv run --locked pytest -n 0 -m dag --dag-node head_after --dag-workers 2 --dag-report .lab/dag-selected
uv run --locked pytest -n 0 -m dag --dag-fail-fast
```

`--dag-node`는 반복할 수 있으며 선택한 노드의 모든 선행을 자동 포함한다. 알 수 없는 ID·누락된 선행·중복 정의·
순환은 실행 전에 거부한다. `DAG plan`은 포함한 노드를 표시하고 JSON 보고에는 요청·포함 노드와 의존 관계를 남긴다.
출력을 즉시 보려면 `-s`를 사용한다. `--dag-node`는 선택한 각 DAG에 적용되므로 서로 다른 그래프의 ID를 사용할 때는
pytest 파일·항목도 함께 선택한다.

`-n 0`은 pytest 항목 사이의 worker를 끈다. 시나리오 내부는 `--dag-workers`의 양수 한도 아래에서 독립 ready 노드를
병렬 실행한다. 내부까지 순차로 재현하려면 `--dag-workers 1`을 사용한다. pytest worker와 DAG worker를 함께 늘리면
전체 자원 사용량도 늘어난다.

노드 보고는 다음 상태를 구분한다. 후속 미실행을 새로운 실패나 성공으로 집계하지 않는다.

| 상태 | 의미 |
| --- | --- |
| `pass` | 해당 노드 검증 성공 |
| `fail` / `error` | assertion 실패 / 실행·준비 오류 |
| `skip` | 명시적으로 사용할 수 없는 검증·선행 조건 |
| `blocked` | 직접 부모의 실패·오류·skip·취소로 실행 차단 |
| `not_reached` | 더 깊은 선행 경로 미충족으로 실행하지 않음 |
| `not_run` | 전역 fail-fast·외부 취소로 dispatch하지 않음 |
| `cancelled` | 실행 중 취소되어 검증을 완료하지 못함 |

정상 모드는 실패와 무관한 가지를 계속 실행한다. `--dag-fail-fast`와 pytest `-x`는 새 dispatch를 중단하고 이미
active인 노드의 결과·정리를 기다린다. `--maxfail=N`의 pytest 항목 실패 한도와 노드 의존성 차단은 별개다.
외부 취소는 정상 결과로 바꾸지 않으며 active action의 정리를 기다린 뒤 전파한다.

pytest는 시나리오 한 항목을 집계한다. [adapter](../tests/support/pytest_dag.py)는 노드별 상태·차단 부모·최초 원인·시작/종료의 단조 시각을
report section과 JUnit user properties에 담고, `--dag-report`를 지정하면 항목별 JSON 파일도 쓴다.
시각은 같은 실행 안의 중첩·소요 시간 관측용이며 UTC timestamp나 실행 간 절대 시각 비교값이 아니다.
각 실행에는 별도 report 디렉터리를 사용한다. 같은 항목을 같은 디렉터리에서 다시 실행하면 이전 JSON을 덮어쓴다.
전체 포함 노드가 모두 PASS이고 실행이 중단되지 않았을 때만 시나리오를 성공으로 판정한다.
취소 때도 먼저 완료한 노드의 결과를 보존하고, active 노드의 정리 후 `interrupted=true`인 보고에 취소·미실행 상태를 남긴다.
`unfinished`는 결과가 누락됐거나 `cancelled`·`not_run`인 ID를 담는다. 의존성으로 차단된 경로는 각 노드의 상태로 확인한다.
취소 자체는 호출자에게 다시 전파한다. 강제 프로세스 종료·전원 종료나 보고 경로의 I/O 실패까지 보장을 확대하지 않는다.
지원 구현의 실패·합류·부분 선택·취소 계약은 [DAG 자체 검사](../tests/support/test_dag.py)에서 확인한다.
pytest 보고·취소 결합은 [adapter 검사](../tests/support/test_pytest_dag.py), 초기 옵션 탐색은
[실제 CLI 검사](../tests/system/test_dag_cli.py)가 검증한다.

## 품질 관측

관측 자료는 테스트 수나 비율 quota를 채우는 용도가 아니다. 계약별 정상·거부·실패 검증의 공백을 먼저 살피고,
순서 독립성·결함 검출력·준비 비용을 같은 조건에서 비교한다. 해석 기준과 조사 근거는
[테스트 정책의 근거](research/testing-policy-research.md#개선-판단과-증거의-한계)를 따른다.

unit의 순서 독립성은 seed를 기록해 직렬·병렬로 확인한다. 기본 회귀에서는 randomly 플러그인을 끄므로
다음 명령은 addopts를 명시적으로 덮어써 활성화한다. 의존 시나리오의 노드 순서를 임의로 섞는 명령은 아니다.

```sh
uv run --locked pytest -o 'addopts=-v --tb=short --strict-markers' -p randomly -m unit -n 0 --randomly-seed=20261028
uv run --locked pytest -o 'addopts=-v --tb=short --strict-markers' -p randomly -m unit -n 4 --randomly-seed=42
uv run --locked coverage run -m pytest -n 0 -m unit
uv run --locked coverage report
uv run --locked coverage json -o .lab/unit-coverage.json
```

coverage는 line과 branch를 함께 측정한다. `-n 0`인 위 경로의 pytest 프로세스만 측정하며, 별도 stdio·CLI·Git/GPG
자식의 실행까지 포함하지 않는다. unit에서 호출하지 않는 I/O adapter의 미측정 줄은 integration 검증 공백과 동일하지 않다.
기본 합계 백분율은 line·branch 기회의 합계이므로 순수 branch 비율로 표시하지 않는다.
프로세스별 실행 범위를 분명히 하고, 측정을 위해 제품의 환경 격리 계약을 느슨하게 하지 않는다.
[coverage 프로세스 측정](https://coverage.readthedocs.io/en/7.17.0/config.html#run-patch)

mutation은 선택 개발 그룹으로 설치하며 현재 대상은 순수 바이트 처리 `_text.py`다.
소스 package 전체를 복사할 수 있도록 `source_paths`를 지정하고 `only_mutate`로 변이 파일을 제한한다.
대상 파일만 복사하면 나머지 package가 빠져 테스트 수집부터 실패할 수 있다.

```sh
uv sync --locked --group mutation
uv run --locked --group mutation mutmut run --max-children 2
uv run --locked --group mutation mutmut results
uv run --locked --group mutation mutmut show '<mutant name>'
```

mutmut의 `mutants/` 작업 사본에서 unit 프로필을 실행한다. pytest 항목 병렬화는 끄고 변이 간 동시 실행 수만 제한한다.
기준 테스트와 도구의 변이 없는 확인 실행이 성공했는지 먼저 확인한다. 수집·import·setup 오류를 killed로 해석하지 않는다.
survivor는 diff와 해당 계약을 읽어 assertion 공백·동등한 동작·미검증 입력을 구분하고, timeout·실행 오류·미실행도 별도로 기록한다.
원본에 변이를 적용하는 `mutmut apply`는 검증 절차에 포함하지 않는다. 결과 캐시가 의심되면 `mutants/`를 비운 뒤 재실행한다.
지원 OS·프로세스 격리 조건과 설정 의미는 [mutmut 공식 안내](https://mutmut.readthedocs.io/en/latest/)를 따른다.

## 테스트 자원과 정리

fixture가 Git workspace·stdio 설정을 테스트별로 준비해 index·HEAD·hooks·설정 변경을 격리한다.
일반 GPG 홈·생성 키·서비스는 worker별 session에서 사용하고, 잠긴 키·daemon 상태 변경은 전용 환경으로 분리한다.
자원 준비·정리의 구현은 [공유 fixture](../tests/conftest.py)를 참조한다.
일반 workspace의 [기준 설정](../tests/support/workspace.py)은 명령 미허용·identity 없는 상태로 시작한다.
[Git 준비](../tests/support/git.py)는 저장소·identity·서명을 필요한 검사에서 선택하고,
[MCP 결과 검사](../tests/support/mcp.py)는 현재 in-process FastMCP client의 결과를 확인한다.
stdio를 사용하는 다른 SDK client의 결과 처리를 같은 helper로 가정하지 않는다.

GPG 홈은 Unix socket 경로 길이 제한 때문에 짧은 임시 경로를 사용한다.
fixture는 자기 홈에 속한 서비스만 종료한 뒤 홈을 제거한다. 사용자 키 저장소·공유 서비스는 사용하거나 정리하지 않는다.
pytest의 workspace 보관 정책과 별개로 GPG 홈·설치 환경은 context manager가 정리한다.
강제 종료·전원 종료 시 정리까지 보장하지는 않는다.

기본 홈 탐색 회귀는 메타데이터 조회 결과를 테스트 홈으로 대체해 사용자 홈을 방문하지 않는다.
실제 서명 검증은 설치된 GPG와 wrapper를 사용한다. 자동 시작·연결 종료 후 daemon 존속과
잠긴 키의 비대화형 실패는 테스트가 소유한 전용 자원으로 검증한다.

## 설치 검증

개발 환경의 소스 import만으로는 Git이 실행하는 설치 진입점의 배포·연결을 확인할 수 없다.
`packaging` 검사는 설치된 CLI → stdio → Git → 설치된 GPG wrapper → 실제 서명·검증 경계를 확인한다.

`uv`·현재 Python·Git·GPG·gpgconf가 필요하다. opt-in 순차 session에서 wheel·설치 가상환경·캐시를
한 번 준비하고 종료 시 제거한다. 준비 순서와 실행 인자는 [설치 fixture](../tests/support/packaging.py)가 소유한다.

일반 회귀에서는 설치 fixture가 실행되지 않는다. 전체 회귀를 새 환경에서 반복하는 검사도 아니다.
자동 Python 다운로드는 끄며 별도 임시 uv 캐시를 사용한다. 빌드 backend와 런타임 의존성 다운로드에는
네트워크가 필요할 수 있다. 실패 메시지의 단계와 의존성·네트워크 상태를 확인한다.

이 검사가 제공하는 증거는 **현재 interpreter·바이너리와 lock된 의존성 조합에서 설치 제품이 동작한다**는 것이다.
export 목록이 누락 의존성을 공급할 수 있어 wheel metadata만의 완전성을 증명하지 않는다.
모든 지원 버전·OS의 호환성이나 build 의존성까지 lock된 재현성도 보장하지 않는다.
서명 성공은 로컬 OpenPGP 검증이며 실제 Chat 연결·GitHub Verified와 구분한다.

근거: [pytest 설치 패키지 검증](https://docs.pytest.org/en/stable/explanation/goodpractices.html),
[PyPA 의존성과 환경 목록](https://packaging.python.org/en/latest/discussions/install-requires-vs-requirements/),
[uv build constraints](https://docs.astral.sh/uv/concepts/projects/build/#build-constraints),
[xdist session fixture](https://pytest-xdist.readthedocs.io/en/stable/how-to.html#making-session-scoped-fixtures-execute-only-once).


## GitHub Delivery 검증

[Receipt 검사](../tests/tools/gh/test_delivery_receipts.py)를 비롯한 GitHub 통합 테스트는
테스트별 임시 PostgreSQL schema를 만들고 자기 자원만 정리한다. 연결·명시적 Task 준비는
[공용 지원](../tests/support/gh_delivery.py), Label Scope의 두 독립 세션과 공통 증거 준비는
[해당 지원 module](../tests/support/gh_label_scope.py)이 소유한다.
실패 주입·claim 변경·schema 손상은 각 시나리오에서 명시하며 공용 준비의 모드로 숨기지 않는다.
DB 환경 부재에 따른 skip는 실제 영속성 검증의 성공으로 세지 않는다.

[고정 canonical 벡터](../tests/tools/gh/test_canonical_evidence.py)는 저장된 증거의 bytes·digest와
strict JSON 오류 의미를 검증한다. README의 도구 표와 수동 TOOLS 목록의 동등성은
[문서 검사](../tests/system/test_documentation_links.py)가 확인한다.
이 검사는 문서 설명의 의미나 실제 원격 GitHub 실행 성공을 증명하지 않는다.

## Git 통합·누적 릴리스 검사

```sh
uv run --locked pytest tests/tools/release tests/tools/git/test_release_merge.py tests/mcp/test_package_version.py
uv run python -m local_dev_mcp.tools.release check --base <전체 SHA> --head <전체 SHA>
uv run python -m local_dev_mcp.tools.release plan
```

순수 모델 검사는 영향 집계·정정 관계·canonical version·digest를 검사한다. 실제 Git snapshot과 MCP 검사는
평가 누락/변조·설정 우회·경로 권한·관측 HEAD·squash tree/parent/title·새 commit 서명과 hook 이후 상태를 검사한다.
릴리스 검사는 의존성 없는 별도 임시 프로젝트에서 실제 uv/Towncrier로 prepare→commit→check와 MCP 통합을 수행하고
symlink·stale 계획·소스/의존성/과거 changelog 변조·외부 working state 변화의 거부를 확인한다.
이 리허설은 실제 선택 workspace의 릴리스·tag·게시를 실행한 증거가 아니다.

## Actions와 로컬 검증의 범위

[ADR 0009](adr/0009-actions-unit-tests.md)에 따라 `Project checks` workflow의 `Project quality` job은
main push와 main 대상 PR에서 형식·린트·타입 검사와 `pytest -n 0 -m unit tests`만 실행한다.
테스트 선택은 `unit` marker로 고정하며 바이너리 의존성을 자동 감지해 skip하지 않는다.

통합·설치·실제 Git snapshot 릴리스 검사와 wheel·sdist 빌드 검사는 로컬에서 수행한다.
Actions에서는 전체 검사를 포함하는 `workflow check`를 호출하지 않는다. 로컬 `workflow check`와
hooks의 검사 범위는 그대로 유지한다. PR의 정확한 base/head에 대한 릴리스 검사는 위 로컬 명령으로
별도 수행해야 하며, CI 성공은 그 결과나 패키지 검증을 보장하지 않는다.

workflow 파일의 존재를 원격 실행 성공이나 required check 설정의 증거로 취급하지 않는다.
merge queue를 사용하려면 `merge_group` 대상을 별도로 추가·검증해야 한다.

## 명시적 PR 병합 검사

[순수 병합 검사](../tests/tools/gh/test_pr_merge.py)는 고정 메시지·계획 변경·조건부 요청과
응답 유실·이미 병합·실제 메시지 불일치의 결과 판정을 검증한다. GitHub 경계만 대체하며 외부 바이너리는 실행하지 않는다.
[CLI 경계](../tests/tools/test_workflow_github.py)와
[MCP 공개 계약](../tests/tools/git/test_pr_merge_mcp.py)은 공통 구현 연결, origin 일치, strict 입력,
독립 권한 거부와 중복 쓰기 방지를 확인한다. MCP 검사는 로컬 실제 framework와 Git root를 사용한다.
실제 GitHub merge와 새 도구의 ChatGPT 호출은 별도 수용 증거이며, 이 테스트로 대체하지 않는다.

[채팅 PR 수명 검사](../tests/tools/gh/test_pr_lifecycle.py)는 정확한 head, 공급자 구분, 부분 조회,
중복 검사, Ready 응답 유실과 재관측을 순수 데이터로 검증한다.
[공개 MCP 검사](../tests/tools/git/test_pr_lifecycle_mcp.py)는 strict 입력과 독립 권한 및 실제 호출 흐름을 확인한다.
[새 원격 main 검사](../tests/tools/git/test_remote_main_fetch.py)는 임시 bare 원격의 실제 새 commit과
재작성된 main을 취득해 추적 ref·로컬 main·기능 HEAD 보존을 검증한다. 실제 GitHub/ChatGPT 수용과 구분한다.
