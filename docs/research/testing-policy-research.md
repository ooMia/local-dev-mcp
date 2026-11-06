# 테스트 정책의 근거

조사일: 2026-10-28. 공식 문서·기술 자료·공식 저장소의 release snapshot을 대조한 뒤 채택한 정책의 근거다.
규칙은 [개발 규약](../conventions.md#타입과-테스트), 실행 명령·보고 옵션은 [테스트 안내](../testing.md)가 소유한다.
외부 프로젝트의 관행을 보편 표준으로 삼거나, 문서 조사만으로 현재 환경의 도구 호환성을 보장하지 않는다.

## 검증 계층과 실행 방식

검증 범위와 사용하는 자원의 범위는 다른 축이다. Google은 scope와 size를 구분하고 unit이 항상 small인 것은
아니라고 설명한다. 이 프로젝트는 그 구분을 참고하되 **unit은 실제 파일·프로세스 등 OS 자원 없이 데이터·객체·callable을
검사한다**는 경계를 선택했다. 실제 자원·프로토콜·정책·실패의 결합은 integration으로 검증한다.
이는 프로젝트의 일관된 분류 기준이며 Python 전체가 반드시 따르는 정의라는 뜻은 아니다.
[Google: Unit Testing](https://abseil.io/resources/swe-book/html/ch12.html)

unit의 독립 실행과 integration의 명시적 상태 의존을 함께 유지한다. 병렬·직렬은 새로운 검증 계층이 아니다.
서로 독립인 검증은 병렬 실행하고, 같은 index를 소비·변경하는 단계처럼 실제 선후 관계가 있는 경우에만 DAG를 선언한다.
단순 파일 수집 순서나 이전 실행의 잔여 상태는 의존 관계의 구현으로 사용하지 않는다.
다른 테스트의 상태, 전역 환경, 고정된 시간 여유에 기대는 검증은 불안정해질 수 있다.
[pytest: Flaky tests](https://docs.pytest.org/en/stable/explanation/flaky.html)

## Fixture·실제 자원·대역

fixture는 준비·제공·수명·정리를 담당하고, test double은 실제 의존성을 대신한다. 실제 임시 저장소도 mock도
fixture로 제공할 수 있으므로 둘은 대립하는 선택이 아니다. 안전하고 재현 가능한 실제 구현을 사용하되,
특정 실패를 재현하거나 비결정적 의존성을 제어할 때 필요한 지점만 fake·mock으로 바꾼다.
대역의 성공 응답은 실제 의존성의 프로토콜·지속 상태·부작용이 맞는다는 증거를 대신하지 않는다.
[pytest: Fixtures](https://docs.pytest.org/en/stable/how-to/fixtures.html),
[Google: Test Doubles](https://abseil.io/resources/swe-book/html/ch13.html)

대부분의 assertion은 반환값·공개 오류·지속 상태를 확인한다. 다만 정책 거부 뒤 프로세스 미실행,
실패 후 자동 재시도 금지처럼 호출 여부·순서 자체가 계약이면 상호작용 관측이 필요하다.
내부 helper가 호출됐다는 사실만으로 사용자에게 필요한 결과를 검증했다고 주장하지 않는다.
[Google: Behavior, Not Implementation](https://testing.googleblog.com/2013/08/testing-on-toilet-test-behavior-not.html),
[Google: State vs. Interactions](https://testing.googleblog.com/2013/03/testing-on-toilet-testing-state-vs.html)

patch는 대상 코드가 이름을 조회하는 namespace에 적용한다. autospec 같은 interface 검사는 대역 오용을 줄이지만
실제 프로토콜·반환 의미·부작용까지 검증하지 않는다. 주입한 실패의 범위와 실제 자원에서 확인한 범위를 구분한다.
[Python: Where to patch](https://docs.python.org/3/library/unittest.mock.html#where-to-patch),
[Python: Autospeccing](https://docs.python.org/3/library/unittest.mock.html#autospeccing)

fixture 위치는 공유 범위, scope는 비용·격리·정리 요구로 결정한다. conftest의 하위 디렉터리 가시성과
function/session 수명은 별개다. 준비하다 실패한 fixture의 yield 뒤 정리는 실행되지 않을 수 있으므로,
완료된 자원 준비마다 정리 책임을 등록하고 사용자 자원을 테스트 수명에 포함하지 않는다.
[pytest: Fixture availability](https://docs.pytest.org/en/stable/reference/fixtures.html#conftest-py-sharing-fixtures-across-multiple-files),
[pytest: Safe fixture structure](https://docs.pytest.org/en/stable/how-to/fixtures.html#safe-fixture-structure)

## 소스 소유권과 테스트 위치

pytest의 배치 안내는 별도 tests 폴더와 패키지 내부 테스트를 모두 설명한다. PyPA의 src layout은 설치·import
경계를 다룬다. 이들 지침이 소스 파일별 완전한 mirror나 unit/integration/e2e 최상위 디렉터리를 요구하는 것은 아니다.
[pytest: Good Integration Practices](https://docs.pytest.org/en/stable/explanation/goodpractices.html),
[PyPA: src layout vs flat layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)

다음은 직접 읽은 release tag의 제한된 사례다. 태그 URL을 사용했으며 commit SHA까지 고정하지 않았다.
전체 테스트를 전수 검토하거나 현재 설치된 라이브러리의 동작을 이 snapshot으로 판정하지 않는다.

| 공식 snapshot | 관측한 소유권 대응과 실행 방식 |
| --- | --- |
| [pytest 8.3.5](https://github.com/pytest-dev/pytest/tree/8.3.5/testing) | `src/_pytest/capture.py`와 `testing/test_capture.py`처럼 의미가 대응한다. [같은 파일](https://github.com/pytest-dev/pytest/blob/8.3.5/testing/test_capture.py)에 객체 직접 검사와 `runpytest_subprocess` 검사가 있다. |
| [HTTPX 0.28.1](https://github.com/encode/httpx/tree/0.28.1/tests) | `_auth.py`와 `test_auth.py`, `_client.py`와 `tests/client/`가 대응한다. [실제 서버 fixture](https://github.com/encode/httpx/blob/0.28.1/tests/conftest.py)와 [MockTransport·monkeypatch](https://github.com/encode/httpx/blob/0.28.1/tests/test_auth.py)를 함께 사용한다. 이 snapshot은 src layout이 아니다. |
| [MCP Python SDK v1.12.4](https://github.com/modelcontextprotocol/python-sdk/tree/v1.12.4/tests) | `src/mcp`의 client/server/shared와 테스트 경로가 대응한다. 기능 소유권 아래 [test_integration.py](https://github.com/modelcontextprotocol/python-sdk/blob/v1.12.4/tests/server/fastmcp/test_integration.py)가 별도 서버 process와 SSE/StreamableHTTP 검사를 수행한다. |
| [GitPython 3.1.44](https://github.com/gitpython-developers/GitPython/tree/3.1.44/test) | `git/index/`와 [test_index.py](https://github.com/gitpython-developers/GitPython/blob/3.1.44/test/test_index.py)처럼 의미가 대응한다. [공유 자원 helper](https://github.com/gitpython-developers/GitPython/blob/3.1.44/test/lib/helper.py)가 writable repo를 준비한다. 내부 파일을 전부 일대일 복제하지 않는다. |

이 프로젝트는 기능 소유권에 맞춰 테스트를 배치하되 private 파일을 전부 일대일 복제하지 않는다.
전체 stdio·설치 흐름은 여러 기능을 연결하므로 system 검증으로 둔다.
순수 데이터 처리와 실제 관측을 분리해 unit과 integration의 책임을 명확히 한다.
현재 배치와 분류 기준은 [개발 규약](../conventions.md#타입과-테스트), 연결 구조는 [설계](../design.md)가 소유한다.

## 의존 실행과 결과 보존

pytest fixture 의존은 준비 순서를 정리하지만 별도 검증의 성공 여부를 판정하는 DAG scheduler는 아니다.
Python graphlib는 ready 노드와 완료 통지를 제공하지만, `done`은 성공을 뜻하지 않고 정의하지 않은 선행 ID도
자동 추가한다. 따라서 노드 정의·순환·부분 선택 검증과 성공 여부에 따른 gate는 별도로 필요하다.
[pytest: Fixture order](https://docs.pytest.org/en/stable/reference/fixtures.html#fixture-instantiation-order),
[Python: graphlib](https://docs.python.org/3.15/library/graphlib.html)

Git index를 변경하는 단계에는 실제 선후 관계가 있고, 그 뒤의 독립 조회에는 병렬 실행의 여지가 있다.
따라서 [DAG 지원](../../tests/support/dag/)으로 성공 의존과 실패 전파를 명시한다.
차단된 후속을 새 실패로 세지 않아야 최초 결함과 그 영향을 구분할 수 있다.

처리되지 않은 TaskGroup 예외는 형제 작업을 취소할 수 있다. 일반 검증 실패는 결과로 회수하되 외부 취소는 전파하고,
active 작업의 정리가 끝난 뒤 공유 Client·workspace 수명을 종료한다. SDK의 취소 scope는 같은 owner task에서
진입·종료하며, 그래프 노드마다 live Client를 새로 만들거나 서로 다른 task에서 수명을 나눠 소유하지 않는다.
[Python: Task groups](https://docs.python.org/3.15/library/asyncio-task.html#task-groups)

fail-fast는 전체 실행을 빨리 끝내려는 정책이므로 의존성에 따른 가지 차단과 구분한다.
동시 실행은 시간 단축만으로 추정하지 않고 동기화 지점과 노드별 관측으로 확인한다.
현재 실행·보고 의미는 [테스트 안내](../testing.md#의존-시나리오-실행과-보고),
구체 선후 관계는 [stdio 시나리오](../../tests/system/test_dag_workflow.py)가 소유한다.

## 개선 판단과 증거의 한계

| 관측 자료 | 판단할 내용 | 피해야 할 해석 |
| --- | --- | --- |
| 계약별 검증 근거 | 정상·입력 거부·권한·부분 성공·재시작·취소의 검증과 공백 | 테스트 함수 수 증가를 품질 향상으로 간주 |
| line/branch coverage | 실행하지 않은 줄·분기 목적지와 계층별 범위 | 실행 사실을 assertion 정확성이나 결함 검출력과 동일시 |
| mutation | 핵심 처리의 변이를 검출했는지와 survivor 원인 | import/setup 실패를 검출 성공으로 계산하거나 분모가 다른 점수를 단순 비교 |
| 반복·순서·병렬 실행 | 최초 실패와 실행 조건·seed·worker·환경 | 재시도 통과로 최초 실패를 삭제하거나 의존 단계를 임의 순서로 뒤집기 |
| 실행 비용 | 같은 검증 범위·준비·worker 조건의 wall-clock과 setup/call/teardown | 후속 차단이나 검증 생략으로 짧아진 실행을 개선이라고 주장 |

coverage.py의 branch 측정은 분기 목적지를 관측한다. 기본 합계는 line·branch 기회를 함께 반영하므로 이를
순수 branch 비율로 표시하지 않는다. mutation은 killed·survived 외에 동등 변이·timeout·실행 오류·미실행을
구분해 해석한다. 숫자 quota를 채우려고 의미 없는 assertion을 늘리지 않으며 도구의 설치·실행 사실은 별도 검증한다.
[coverage: Branch measurement](https://coverage.readthedocs.io/en/7.17.0/branch.html),
[Cosmic Ray: Theory](https://cosmic-ray.readthedocs.io/en/latest/theory.html)

서버 기능 검증과 선택적인 외부 클라이언트 수용 검증의 책임은
[테스트 안내](../testing.md#서버-기능과-외부-클라이언트-검증)를 따른다.
