# 검사 작업의 권한·환경·출력 처리 근거

조사일: 2026-10-29. 프로젝트별 lint·format·test CLI를 MCP에서 실행할 때 필요한 권한과 환경,
진단 출력의 처리 방법을 공식 문서·설치 코드·합성 입력으로 조사했다.
이 문서는 설계 판단의 근거와 후보의 한계를 설명하며, 미구현 동작을 현재 제품 계약으로 선언하지 않는다.
현재 계약은 [설정 안내](../configuration.md), [테스트 안내](../testing.md), [개발 규약](../conventions.md)이 소유한다.

## 서로 다른 권한을 하나로 묶지 않기

| 판단 | 의미 | 다른 판단으로 확대하면 안 되는 보장 |
| --- | --- | --- |
| 작업 실행 허용 | 등록된 lint·format·test 작업을 시작할 수 있음 | 임의 CLI·인자·shell 실행 허용 |
| 자식 환경 제공 | 작업 코드가 필요한 설정·비밀을 내부에서 사용할 수 있음 | agent에게 해당 값 공개 허용 |
| 출력 공개 | 정해진 진단·결과를 MCP에 반환할 수 있음 | 자식이 출력한 모든 문자열의 무해성 |
| 직접 파일 수정 | 관측 참조와 경로 정책 아래 파일을 교체할 수 있음 | formatter·테스트의 부수 변경을 모두 추적·차단 |

현재 [명령 정책](../../src/local_dev_mcp/settings.py)의 `require_command`는 문자열의 정확한 목록 포함을 검사한다.
[등록 경계](../../src/local_dev_mcp/mcp/registry.py)는 도구별 요구 명령을 검사하지만, 실행할 CLI 문법을 분석하거나
자식 프로세스 전체를 격리하지는 않는다. 이 경계는 [현재 집행 한계](../configuration.md#집행-범위와-한계)와 일치한다.

`uv run`은 임의 명령뿐 아니라 Python script·module도 실행할 수 있고, 앞선 옵션은 uv가, 실행 명령 뒤의 옵션은
대상 프로그램이 해석한다. `--with`는 실행 의존성을 바꾸고 `--project`·`--directory`는 대상 선택에 영향을 준다.
따라서 `uv.run.pytest` 같은 이름은 uv가 제공하는 권한 체계가 아니라 이 프로젝트가 정의할 논리적 capability다.
[uv CLI](https://docs.astral.sh/uv/reference/cli/#uv-run)

**설계상 권장:** capability 하나를 등록된 불변 작업 recipe와 결합하고, 호출 시에는 테스트 대상처럼
합의한 typed 입력만 받는다. 임의 argv의 공통 prefix를 분석해 권한을 추론하거나, 세부 권한이 없을 때 광범위한
`uv` 허용으로 대체하면 의미가 불분명해진다. 이름을 작업 의미로 정할지 실제 launcher 구조로 정할지는 별도 선택이다.
이것은 유지보수를 위한 제안이며 OS sandbox나 신뢰하지 않는 프로젝트 실행의 격리를 뜻하지 않는다.

## 환경값의 로딩과 마스킹 대상의 소유권

현재 [CLI](../../src/local_dev_mcp/app/cli.py)는 SDK import 전에 ambient 환경을 최소화한다.
[최소 환경](../../src/local_dev_mcp/execution.py)은 PATH·운영체제·임시 경로 일부만 보존하며 HOME·locale·VIRTUAL_ENV를
일괄 전달하지 않는다. [설정 reader](../../src/local_dev_mcp/settings.py)의 dotenv 처리는 지정된 루트 `.env`만 읽고
보간과 `os.environ` 변경을 하지 않는다. [서버 snapshot](../../src/local_dev_mcp/app/runtime.py)은 필요한 Git 준비값을
만들지만 dotenv 전체를 모든 자식에게 주입하지 않는다.

`python-dotenv`의 `dotenv_values`는 환경을 변경하지 않고 dict를 반환하므로 source 병합과 snapshot을 한곳에서
소유할 수 있다. 반면 기본 `load_dotenv()`는 상위 경로 탐색과 환경 변경을 하므로 기존 reader와 같은 계약이 아니다.
[python-dotenv](https://bbc2.github.io/python-dotenv/#load-configuration-without-altering-the-environment)

uv의 dotenv 로딩은 `--env-file`·`UV_ENV_FILE`로 지정하며, 복수 파일에서는 뒤의 파일이 우선하고 이미 자식 환경에
있는 값은 dotenv보다 우선한다. 따라서 서버와 runner가 각각 dotenv를 읽으면 우선순위·읽은 시점·parser 차이를
추가로 관리해야 한다. [uv 환경 파일](https://docs.astral.sh/uv/concepts/configuration-files/#environment-variable-files)

| 환경 소유 방식 | 유지보수상 이점 | 명확히 정해야 할 경계 |
| --- | --- | --- |
| 서버가 프로젝트 공통 env profile을 한 번 읽어 snapshot 제공 | 한 parser·병합 규칙과 같은 값으로 실행 환경·비밀 catalog 구성 가능 | 재시작 전 변경 미반영, profile에 들어간 모든 작업 코드가 그 값을 사용 가능 |
| 프로젝트 runner가 dotenv 로딩 | 기존 프로젝트의 실행 명령과 로딩 관습을 재사용 | sanitizer용 source 선언 필요, 실행 시점 재로딩과 catalog snapshot의 불일치 가능 |

**설계상 권장:** 테스트마다 변수 이름을 열거하기보다 프로젝트가 사용할 env source/profile을 한 번 정의하고,
그 profile을 쓰는 작업은 동일한 환경 snapshot을 받도록 한다. 단, 서버 운영 `.env`에는 Tunnel credential과 Git
metadata가 함께 있으므로 이를 모든 검사 작업에 통째로 전달하는 것은 현재보다 권한이 넓어진다.
프로젝트 작업용 source를 한 번 구분하면 변수별·테스트별 전달 목록의 유지보수 부담 없이 이 차이를 표현할 수 있다.

마스킹 catalog는 **실제로 주입한 환경변수들의 합집합**과 구분할 필요가 있다. 등록된 source의 private·미분류 값을
중앙에서 모으면, 프로젝트 코드가 직접 읽은 값이나 다른 실패 경로에 등장한 값도 같은 공개 경계에서 처리할 수 있다.
실행 환경을 늘리거나 호출 체인을 따라 변수 목록을 전달할 이유는 없다. 이미 운영에 쓰는 `.env`의 private 값도
catalog source 후보지만, catalog 등록이 자식에게 그 값을 제공한다는 뜻은 아니다.

공개 운영값은 profile에서 한 번 분류하는 방식을 검토할 수 있다. 예를 들어 검증된 PORT·DEBUG 같은 운영값과
기능상 공개하는 Git metadata는 private 기본값의 예외로 표현할 수 있다. 이름이 PORT라는 이유만으로 모든 프로젝트에서
자동 공개하거나, 숫자·boolean처럼 보이는 값이라는 이유만으로 private 분류를 없애는 정책은 근거가 부족하다.
같은 문자열이 public 변수와 private 변수에 동시에 있으면 public 분류가 private 등록을 지우지 않아야 한다는 것도
설계상 고려사항이다. 실제 분류 규칙과 profile schema는 아직 제품 계약으로 확정하지 않는다.

가상환경 activation도 환경 소유권과 분리한다. Python은 가상환경의 interpreter·script를 직접 실행하면 activation이
필요 없다고 설명하며, uv project 작업은 기본적으로 VIRTUAL_ENV보다 프로젝트 환경을 사용한다. 서버 자신의 venv를
대상 프로젝트 환경으로 오인하지 않고 등록한 runner가 선택한 workspace에서 환경을 찾도록 해야 한다는 근거다.
[Python venv](https://docs.python.org/3/library/venv.html#how-venvs-work),
[uv 프로젝트 환경](https://docs.astral.sh/uv/concepts/projects/config/#project-environment-path)

## CI 제공자들이 공개하는 마스킹의 한계

| 1차 자료 | 확인한 동작·제약 | 이 프로젝트에 주는 시사점 |
| --- | --- | --- |
| [GitHub Actions](https://docs.github.com/en/actions/reference/security/secure-use#use-secrets-for-sensitive-information) | 주로 등록된 값의 정확한 일치에 의존하며 JSON 같은 구조화 secret과 변환된 값은 자동 redaction을 깨뜨릴 수 있음. 파생·인코딩 값도 등록하도록 안내 | 원래 값 catalog와 지원하는 표현 변환을 구분하고, 알려지지 않은 변환까지 숨긴다고 보장하지 않기 |
| [GitLab 변수](https://docs.gitlab.com/ci/variables/#mask-a-cicd-variable) | UI masked 값은 단일행·공백 없음·8자 이상 등의 조건. shell escaping으로 출력이 바뀌면 미마스킹 가능 | 짧은 값·멀티라인·escaping을 생략해도 안전하다고 간주하지 않기 |
| [Azure 출력](https://learn.microsoft.com/en-us/azure/devops/pipelines/scripts/logging-commands?view=azure-devops) | 등록 secret의 일부 조각은 가독성 때문에 마스킹하지 않음. 파생값을 등록한 이후의 로그만 처리 | 완전한 값과 임의 substring 은닉을 구분하고, 최초 출력 전에 catalog 준비 |

이들 제공자도 마스킹을 비밀 유출 전체의 방지 보장으로 설명하지 않는다. 직접 값 출력·command-line secret 전달을
피하고 신뢰하는 작업 코드를 실행하라는 조건이 함께 있다. 이 프로젝트에서도 sanitizer는 우발적인 로그 노출을 줄이는
출력 처리이며, 악의적 프로그램의 인코딩·네트워크 전송·다른 파일 접근을 가두는 경계로 설명하면 안 된다.

GitHub runner의 [SecretMasker](https://github.com/actions/runner/blob/main/src/Sdk/DTLogging/Logging/SecretMasker.cs)는
등록 값·encoder를 한곳에서 관리하고 일치 위치들의 겹친 구간을 합쳐 치환한다.
[HostContext](https://github.com/actions/runner/blob/main/src/Runner.Common/HostContext.cs)는 JSON·URI·Base64 등
유한한 encoder들을 등록한다. 이는 중앙 catalog와 변환 등록의 실례이지 해당 C# 구현의 Python 도입이나 모든
표현의 은닉을 보장하는 근거는 아니다. 조사한 링크는 이동하는 main이므로 도입 시점의 코드 대조가 필요하다.

## 정확한 값 치환·탐지 라이브러리·공개 요약의 역할

| 후보 | 적합한 역할 | 한계·유지보수 비용 |
| --- | --- | --- |
| 중앙 catalog의 literal 치환 | 이미 알고 있는 private 값과 명시한 변환 표현을 결정적으로 처리 | 미등록 값·부분 조각·임의 변환은 알 수 없음. 겹침·stream·상한 경계 처리가 필요 |
| [Pydantic SecretStr](https://docs.pydantic.dev/latest/api/types/#pydantic.types.SecretStr) | catalog 객체의 repr·일반 serialization에 원문이 실리는 실수 감소. 이미 설치한 의존성 | `get_secret_value()`로 꺼낸 값이나 임의 CLI stdout을 자동 정화하지 않음 |
| [Yelp detect-secrets](https://github.com/Yelp/detect-secrets#plugins) | regex·entropy·keyword 기반으로 미리 등록하지 않은 secret 후보를 탐색·감사 | 탐지 정밀도 조정과 baseline 관리 필요. 알려진 값의 stream redactor를 대신하지 않음 |
| 선별된 구조화 요약 | 상태·counts·진단 code·허용된 위치 등 공개 필드를 좁힘 | message·test ID·property에도 원문 값이 들어갈 수 있어 구조화만으로 비밀 제거가 되지 않음 |

**설계상 권장:** 이미 사용하는 의존성과 작은 중앙 sanitizer를 우선 검토하고, detector를 필수 runtime 의존성으로
추가할지는 별도 결함 검출 효과와 비용으로 판단한다. 알려진 secret 값을 숨기는 일과 unknown secret을 추정하는 일은
서로 다른 책임이다. 탐지 결과가 없음을 무유출 증거로 삼는 것은 이 조사로 정당화되지 않는다.

Ruff JSON과 pytest JUnit XML은 원문 공개 허가가 아니다. 설치한 Ruff에 합성 stdin을 주었을 때 message에 소스 식별자가
포함됐고, 설치한 pytest의 `_pytest/junitxml.py`는 failure message·traceback을 저장한다. 기본 `junit_logging=no`도
failure 본문을 제거하지 않는다. source snippet·fix text·skip 이유·parametrization ID·user property도 같은 검토 대상이다.
pytest는 기본 기능으로 JUnit 보고서를 만들 수 있다.
[pytest 출력](https://docs.pytest.org/en/stable/how-to/output.html#creating-junitxml-format-files)

## 합성 입력으로 확인한 경계

실제 `.env`·사용자 key·실행 credential은 읽지 않았다. 표준 Python의 `json.dumps`·`urllib.parse.quote`·Base64와
인위적인 quote·backslash·newline·한글 포함 문자열을 사용해 정확한 치환의 범위를 확인했다.
이는 후보 알고리즘의 작은 관측이며 제품 sanitizer 구현이나 보안 성능 평가가 아니다.

| 합성 입력 | 관측 |
| --- | --- |
| 등록한 원래 문자열 그대로 출력 | literal 치환으로 가려짐 |
| JSON escape·URL encode·Base64 표현 | 원래 값만 치환하면 세 표현 모두 남음 |
| 위 표현들을 추가 등록 | 해당 합성 표현 네 종류는 가려짐 |
| 하나의 secret을 두 I/O chunk로 나눠 각각 치환 | 두 조각을 합치면 원래 secret이 남음 |
| 원문을 secret 중간에서 먼저 자른 뒤 치환 | catalog와 완전 일치하지 않는 앞 조각이 남음 |
| private 값 `1`을 출력 전체에서 치환 | 테스트 수·시간 같은 정상 숫자도 바뀌어 가독성이 손상됨 |

지원 표현의 유한한 등록은 변환을 무한히 추측하는 방식보다 비용을 설명하기 쉽다. 다만 nested encoding·shell별 escaping·
문자 정규화·일부 문자 출력·프로세스가 새로 발급한 값은 이 합성 관측의 범위 밖이다.

## 공개 전 처리 순서와 적용 경계

**설계상 권장:** source snapshot과 private catalog를 먼저 준비하고, 자식의 stdout·stderr를 각 stream의 문맥을
유지하며 처리한다. sanitizer를 지난 내용만 job 결과 보관·기본 로그·MCP text·structuredContent에 넘긴다.
공개 직전 한 필드만 치환하면 다른 공개 경로와 보관된 raw 로그가 빠질 수 있다. 도구가 직접 만드는 XML 같은 임시
보고서는 raw 데이터를 포함할 수 있으므로 일반 공개 artifact와 구분해야 한다.

chunk마다 독립적인 `replace`를 호출하거나 raw 보관 상한에서 먼저 자르면 알려진 secret도 놓칠 수 있다.
최장 지원 표현의 경계 문맥을 처리할 때까지 공개를 보류하고, 치환 위치의 겹침을 처리한 뒤 표시 상한을 적용하는
방식이 필요하다. UTF-8 문자 분할도 함께 고려한다. 전체 원문을 무제한 보관하라는 뜻은 아니다. 처리 가능한 catalog·
값 길이·임시 문맥의 한도를 넘으면 해당 free-form 출력을 숨기고 counts·상태와 안전한 원인을 반환하는 대안이 있다.

제품은 서버가 소유하는 프로젝트 공통 `.env` snapshot을 사용한다. 환경 전달과 비공개 값 catalog는 별도로 파생하며,
추가 공개 분류는 프로젝트에서 한 번 관리한다. 정확한 기본값·예외는 [환경 계약](../configuration.md#프로세스-환경)을 따른다.
지원하는 유한한 표현과 짧은 비공개 값·크기 초과·디코딩 실패 때의 보류는 [작업 진단 계약](../tools/tasks/README.md#진단과-출력-보호)이 소유한다.

catalog에 없는 비밀과 임의 변환에 대한 완전한 무유출, 자식 프로세스의 격리, 실제 ChatGPT 연결·native Tasks 호환은
이 조사로 증명하지 않았다. 이 한계와 실제 제품의 실행·출력 계약을 구분해야 한다.
