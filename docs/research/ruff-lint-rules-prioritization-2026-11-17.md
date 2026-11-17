# Ruff lint 규칙 조사 및 도입 우선순위 (임시 개발 문서)

> 조사 기준: Ruff 0.17.1 문서 및 프로젝트의 현재 `pyproject.toml`
> 상태: 개발 검토용. 이 문서는 정책 결정이나 `pyproject.toml` 변경 승인이 아니다.
> 작성일: 2026-11-17

## 1. 목적과 범위

현재 프로젝트는 `[tool.ruff.lint]`에서 `E`, `F`, `I`와 일부 `ANN` 규칙을 선택한다. 이번 조사의 목적은 Ruff가 제공하는 규칙 체계와 선택·예외·자동 수정 설정을 파악하고, 이 저장소의 Python MCP 서버에 실질적으로 유용한 규칙을 우선순위화하는 것이다.

Ruff 0.17.1은 900개 이상의 lint 규칙을 제공한다. 개별 규칙은 계속 추가·승격·폐기되므로 900여 개 코드 전체를 이 문서에 복제하는 것은 중복·노후화 위험이 크다. **개별 규칙의 완전하고 최신인 목록은 공식 Rules Reference를 정본으로 삼고**, 이 문서에는 해당 버전의 전체 linter family/prefix 목록, 상태 구분, 프로젝트 적합성 및 도입 판단을 기록한다.

- 공식 전체 규칙표: https://docs.astral.sh/ruff/rules/
- 공식 규칙 선택·수정 가이드: https://docs.astral.sh/ruff/linter/
- 공식 설정 레퍼런스: https://docs.astral.sh/ruff/settings/
- 공식 Preview 정책: https://docs.astral.sh/ruff/preview/

규칙표의 각 항목은 코드, 이름, 설명, category, linter, 상태를 제공한다. 상태는 Stable / Preview / Deprecated / Removed로 구분되며, 자동 수정 가능 여부와 기본 활성 여부도 표시된다. Preview·Deprecated·Removed는 Stable 규칙과 동일하게 취급하면 안 된다.

## 2. Ruff lint 규칙 전체 분류

### 2.1 공식 linter family와 selector prefix

아래는 Ruff 0.17.1 공식 Rules Reference의 linter 필터에 노출된 전체 family 목록이다. `select`에는 개별 코드뿐 아니라 prefix를 사용할 수 있다. 예를 들어 `B`는 flake8-bugbear 계열 전체, `ANN`은 flake8-annotations 계열 전체를 선택한다.

| Family | Prefix | 주요 관심 영역 |
|---|---|---|
| Airflow | `AIR` | Airflow DAG/task 사용 규약 |
| eradicate | `ERA` | 주석 처리된 코드 |
| FastAPI | `FAST` | FastAPI route/dependency 규약 |
| flake8-2020 | `YTT` | Python 버전 판별 오류 |
| flake8-annotations | `ANN` | 타입 annotation |
| flake8-async | `ASYNC` | 비동기 코드의 blocking·취소·checkpoint 문제 |
| flake8-bandit | `S` | 보안 취약 패턴 |
| flake8-blind-except | `BLE` | 과도하게 넓은 예외 처리 |
| flake8-boolean-trap | `FBT` | boolean 인자·매개변수 설계 |
| flake8-bugbear | `B` | 버그 가능성 및 위험한 관용구 |
| flake8-builtins | `A` | builtin 이름 shadowing |
| flake8-commas | `COM` | trailing comma |
| flake8-comprehensions | `C4` | 불필요하거나 비효율적인 comprehension |
| flake8-copyright | `CPY` | 저작권 헤더 |
| flake8-datetimez | `DTZ` | timezone-naive datetime |
| flake8-debugger | `T10` | debugger 잔존 |
| flake8-django | `DJ` | Django 특화 규약 |
| flake8-errmsg | `EM` | 예외 메시지 구성 |
| flake8-executable | `EXE` | 실행 파일 shebang |
| flake8-fixme | `FIX` | FIXME/TODO 주석 |
| flake8-future-annotations | `FA` | future annotations |
| flake8-gettext | `INT` | gettext/i18n |
| flake8-implicit-str-concat | `ISC` | 암묵적 문자열 연결 |
| flake8-import-conventions | `ICN` | import alias 규약 |
| flake8-logging | `LOG` | logging API 사용 |
| flake8-logging-format | `G` | logging format 문자열 |
| flake8-no-pep420 | `INP` | namespace package 제한 |
| flake8-pie | `PIE` | 불필요한 코드·간결성 |
| flake8-print | `T20` | print 호출 |
| flake8-pyi | `PYI` | stub 파일 규약 |
| flake8-pytest-style | `PT` | pytest 사용 스타일 |
| flake8-quotes | `Q` | 문자열 quote 스타일 |
| flake8-raise | `RSE` | raise 문 구성 |
| flake8-return | `RET` | return 구조 |
| flake8-self | `SLF` | private member 접근 |
| flake8-simplify | `SIM` | 코드 단순화 |
| flake8-slots | `SLOT` | __slots__ 관련 규약 |
| flake8-tidy-imports | `TID` | import 경계·상대 import 제한 |
| flake8-todos | `TD` | TODO 주석 형식 |
| flake8-type-checking | `TC` | TYPE_CHECKING import |
| flake8-unused-arguments | `ARG` | 미사용 함수 인자 |
| flake8-use-pathlib | `PTH` | pathlib 사용 |
| flynt | `FLY` | f-string 변환 |
| isort | `I` | import 정렬 |
| mccabe | `C90` | 순환 복잡도 |
| NumPy | `NPY` | NumPy 특화 규약 |
| pandas-vet | `PD` | pandas 특화 규약 |
| pep8-naming | `N` | 이름 규약 |
| Perflint | `PERF` | 성능 관련 패턴 |
| pycodestyle | `E`, `W` | PEP 8 오류·경고 |
| pydoclint | `DOC` | docstring 내용·일관성 |
| pydocstyle | `D` | docstring 스타일 |
| Pyflakes | `F` | 미사용·미정의 이름 등 정적 오류 |
| pygrep-hooks | `PGH` | suppression·패턴 규약 |
| Pylint | `PL` | 광범위한 코드 품질·오류·복잡도 |
| pyupgrade | `UP` | 최신 Python 문법·API |
| refurb | `FURB` | 현대적이고 간결한 Python 관용구 |
| Ruff | `RUF` | Ruff 고유 규칙 |
| tryceratops | `TRY` | 예외 처리 설계 |

프로젝트에서 사용하지 않는 프레임워크 전용 규칙(AIR, DJ, FAST, NPY, PD 등)은 해당 의존성을 실제로 도입하기 전에는 우선 선택하지 않는다. 전체 코드와 현재 상태는 공식 규칙표에서 확인한다.

### 2.2 규칙 category

공식 규칙표는 규칙을 다음 성격으로 분류한다.

- correctness: 실제 동작 오류·잘못된 코드 가능성
- suspicious: 의도하지 않았을 가능성이 높은 패턴
- security: 보안상 위험한 사용
- performance: 불필요한 비용 또는 성능 저하
- complexity: 복잡하거나 불필요한 코드 구조
- style: 일관된 코드 작성 규약
- pedantic: 엄격한 품질·명시성 요구
- restriction: 사용 자체를 제한하는 API·패턴
- formatting: 포매터와 겹치는 형식 규칙

도입 우선순위는 category 하나만으로 결정하지 않는다. 오탐 가능성, 코드베이스 적합성, 팀이 실제로 지킬 수 있는 규약인지, 안전한 자동 수정이 가능한지 함께 평가한다.

## 3. [tool.ruff.lint] 주요 설정 기능

| 설정 | 역할 | 도입 판단 |
|---|---|---|
| `select` | 선택 규칙 집합 지정 | 현재 명시적 선택 유지 권장 |
| `extend-select` | 기본 선택 집합에 규칙 추가 | 기존 설정 확장 시 권장 |
| `ignore` | 선택 규칙 제외 | 구체적인 사유가 있을 때만 |
| `extend-ignore` | 기존 제외 집합에 규칙 추가 | 상속·공통 설정 조정 시 |
| `per-file-ignores` | 파일 패턴별 제외 | 테스트·초기화 파일 등 필요한 범위에 한정 |
| `extend-per-file-ignores` | 기존 파일별 제외 확장 | 공통 설정을 보존해야 할 때 |
| `fixable` | 자동 수정 허용 규칙 | 기본값은 ALL |
| `extend-fixable` | 자동 수정 허용 규칙 확장 | 기본 fixable을 제한한 경우 |
| `unfixable` | 자동 수정 금지 규칙 | 의미 변경·정책상 수동 검토가 필요한 규칙 |
| `extend-safe-fixes` | 원래 unsafe인 fix를 safe로 승격 | 기본적으로 사용하지 않음 |
| `extend-unsafe-fixes` | 원래 safe인 fix를 unsafe로 강등 | 위험성이 확인된 규칙에 한해 사용 |
| `preview` | Preview 규칙 및 fix 활성화 | 안정성 우선이므로 비활성 유지 권장 |
| `explicit-preview-rules` | Preview 규칙을 정확한 코드로만 선택 | Preview를 도입할 때 오선택 방지 |
| `task-tags` | TODO 등 task 주석 태그 정의 | TD/FIX 도입 시 검토 |
| `typing-modules` | 사용자 정의 typing module 지정 | 프로젝트의 typing 추상화가 생길 때 |
| `allowed-confusables` | 허용할 혼동 가능 Unicode 문자 | 다국어 식별자 정책 필요 시 |
| `dummy-variable-rgx` | 미사용 변수 허용 패턴 | 기본 underscore 관례 유지 권장 |
| `external` | 외부 도구가 생성한 rule code 등록 | 외부 진단 도구 연동 시 |
| `logger-objects` | logging 객체로 취급할 사용자 정의 logger | 사용자 정의 logger 도입 시 |

개별 plugin은 별도 subsection으로 세부 설정을 제공한다. 예: `[tool.ruff.lint.flake8-annotations]`, `[tool.ruff.lint.isort]`. 모든 설정과 타입·기본값은 공식 Settings Reference를 기준으로 한다.

## 4. 자동 수정의 안전성

Ruff는 fix를 세 단계로 구분한다.

1. **Safe**: 기본 `ruff check --fix`가 적용한다. 런타임 의미를 보존하도록 설계되어 있다.
2. **Unsafe**: 명시적 `--unsafe-fixes` 또는 설정이 있어야 적용한다. 런타임 동작이 바뀌거나 주석이 제거될 수 있다.
3. **Display-only**: 진단은 표시하지만 자동 적용할 수 없다. 수동 수정이 필요하다.

기본 명령:

```bash
ruff check
ruff check --fix
ruff check --diff
ruff check --fix --diff
```

- `ruff check --fix`는 선택된 규칙 중 적용 가능한 **safe fix만** 기본 적용한다.
- `--unsafe-fixes`는 별도 검토와 승인 없이 CI나 일반 개발 루틴에 추가하지 않는다.
- 규칙 자체에 fix가 있다고 해도 모든 진단 사례가 자동 수정되는 것은 아니다.
- fix 가능 여부와 안전성은 규칙 및 개별 진단의 applicability를 기준으로 확인한다.
- 안전성은 Preview 모드에서 달라질 수 있다.
- `fixable = ["ALL"]`은 fix 적용 허용 범위이지, 모든 규칙을 lint 대상으로 활성화한다는 뜻이 아니다. 규칙 선택과 fix 허용은 별개다.
- 자동 수정 뒤에는 diff 검토, lint 재실행, 관련 테스트가 필요하다.

Ruff 문서: https://docs.astral.sh/ruff/linter/#fixes

## 5. 프로젝트 기준 도입 우선순위

프로젝트 특성: Python 3.15 이상, FastMCP 기반 MCP 서버, 파일시스템·프로세스·Git·네트워크 경계와 비동기 작업을 다룬다. 보안, 예외 처리, 비동기 correctness, 경로 처리, 테스트 신뢰성이 중요하다.

### P0 — 우선 도입 검토

| Prefix / Rule | 이유 | 자동 수정 관점 |
|---|---|---|
| `B` | bugbear의 실수 유발 패턴 및 correctness 진단 | 일부 safe fix 제공 |
| `UP` | Python 3.15 기준 구식 문법·API 제거 | safe fix가 풍부해 자동화 가치 높음 |
| `ASYNC` | async 함수 안의 blocking 호출, 잘못된 async 패턴 탐지 | 일부 fix 가능, 진단 자체의 가치 높음 |
| `S` | subprocess, 역직렬화, 임시 파일, 해시 등 보안 민감 코드 탐지 | fix보다 수동 보안 검토 중심 |
| `BLE` | 넓은 except 처리로 오류를 은폐하는 문제 탐지 | 수동 수정 중심 |
| `RUF` | Ruff 고유의 correctness·성능·suspicious 규칙 | safe fix가 있는 규칙은 자동화 가치 높음 |
| `C4` | 불필요한 comprehension·생성 패턴 정리 | 다수 자동 수정 가능 |
| `SIM` | 단순화 가능한 분기·표현식 탐지 | 일부 safe fix 제공 |
| `PIE` | 불필요한 코드 및 오류 가능성 감소 | 일부 safe fix 제공 |

### P1 — P0 이후 도입 검토

| Prefix | 이유 | 주의점 |
|---|---|---|
| `RET` | return 구조의 불필요한 복잡성 제거 | 일부 패턴은 의도적일 수 있음 |
| `PTH` | 경로 조작을 pathlib로 일관화 | Path/string 경계와 외부 API 호환성 확인 |
| `PERF` | 성능상 불필요한 연산 탐지 | 가독성과 실제 성능의 trade-off 검토 |
| `G`, `LOG` | logging format과 API 사용 일관성 | logger adapter 및 사용자 정의 logger 설정 검토 |
| `TRY` | 예외 chaining·예외 설계 개선 | 현재 오류 계약과 의도적인 예외 변환 존중 |
| `TID` | import 경계와 상대 import 제한 | 패키지 구조와 테스트 import 방식 확인 |
| `TC` | TYPE_CHECKING import로 런타임 의존성 완화 | 런타임 annotation introspection 호환성 확인 |
| `ARG` | 불필요한 인자와 API 흔적 탐지 | callback/protocol 구현에서 false positive 가능 |
| `FBT` | boolean trap으로 인한 호출부 가독성 저하 | public API·내부 helper 구분 필요 |
| `PT` | pytest 테스트의 오류 가능성과 일관성 | pytest 버전·fixture 관례 확인 |
| `N` | naming 일관성 | 기존 public API 및 protocol 이름과 충돌 여부 |
| `A` | builtin shadowing 방지 | 일부 도메인 용어와 이름 충돌 가능 |

### P2 — 선택적 도입

- `D`, `DOC`: docstring 품질은 개선하지만 현재 API 전체에 적용하면 초기 수정량과 유지비가 증가할 수 있다. 공개 API부터 점진 도입한다.
- `PL`: 광범위한 규칙 집합이므로 개별 규칙을 평가한 뒤 선택한다. `PLR`의 복잡도 임계값은 일괄 강제하지 않는다.
- `C90`: 복잡도 임계값을 팀이 실제로 유지할 수 있을 때 도입한다.
- `TD`, `FIX`, `ERA`: 주석 관리 정책을 먼저 합의한다.
- `INP`, `SLF`, `SLOT`, `ICN`, `EM`, `RSE`, `ISC`, `FA`, `PYI`, `FLY`, `Q`, `COM`, `W`, `CPY`: 코드베이스와 명시적 스타일 정책에 따라 개별 도입한다.

### 기본적으로 보류

- 프레임워크 미사용 규칙: `AIR`, `DJ`, `FAST`, `NPY`, `PD` 등
- preview 규칙: 안정화·정책 검토 전 일괄 활성화하지 않는다.
- deprecated/removed 규칙: 새 설정에 포함하지 않는다.
- `ALL`: 규칙 집합 전체를 한 번에 활성화하는 실험 용도로는 가능하지만 장기 기본값으로 권장하지 않는다. 새 규칙이 추가될 때마다 lint 결과가 변하고, preview 정책 및 규칙별 예외 관리가 어려워진다.

## 6. 권장 도입 순서

1. 현재 선택 규칙과 기본 Ruff 규칙을 기준선으로 보존한다.
2. P0 family를 하나씩 추가하고 전체 lint를 실행해 진단 수와 유형을 기록한다.
3. 각 family에서 기존 코드의 실제 문제를 잡는지, 프로젝트 규약과 충돌하는지 확인한다.
4. safe fix가 가능한 진단은 별도 diff를 생성해 검토하고 테스트를 통과시킨다.
5. P1 family를 같은 방식으로 평가한다.
6. P2는 팀 규약 또는 특정 하위 영역의 필요성이 확인될 때만 추가한다.
7. Preview 활성화 여부는 별도 결정으로 분리한다.

**자동 수정 정책 권고:** 기본 `ruff check --fix`의 safe fix를 활용한다. `--unsafe-fixes`는 일상 명령·CI에 넣지 않는다. 자동 수정은 별도 변경으로 diff를 검토하고, 테스트와 typecheck로 의미 보존을 검증한다.

## 7. 프로젝트 설정 초안 (제안일 뿐, 적용하지 않음)

아래는 도입 검토를 시작하기 위한 예시다. 실제 적용 전 각 family별 lint 결과와 기존 예외를 확인해야 한다.

```toml
[tool.ruff.lint]
select = [
    "E",
    "F",
    "I",
    "ANN",
    "B",
    "UP",
    "ASYNC",
    "S",
    "BLE",
    "RUF",
    "C4",
    "SIM",
    "PIE",
]
```

이 초안은 전체 규칙 선택이 아니라 현재 설정에 P0 후보를 더한 출발점이다. `ANN`은 기존의 일부 개별 선택에서 family 전체 선택으로 바뀌므로 `ANN401` 등 영향 범위를 별도로 검토해야 한다. 보안 규칙 `S` 역시 기존 코드의 실제 사용 맥락을 확인하고 필요한 예외를 최소 범위로 설정한다.

## 8. 조사 근거와 한계

- Context7 Library: `/astral-sh/ruff/v0.17.1` (Astral 공식 Ruff 문서, High reputation)
- Context7에서 확인한 주제: rule selector semantics, preview selection, fix safety, fixable/unfixable 설정
- 공식 Rules Reference: https://docs.astral.sh/ruff/rules/
- 공식 Linter Guide: https://docs.astral.sh/ruff/linter/
- 공식 Settings Reference: https://docs.astral.sh/ruff/settings/
- 공식 Preview Guide: https://docs.astral.sh/ruff/preview/

규칙의 개수와 상태는 Ruff 버전에 따라 달라진다. 개별 규칙의 정확한 코드·상태·자동 수정 가능 여부를 실제 적용 직전에 공식 Rules Reference와 설치된 Ruff 버전으로 재확인해야 한다. 이 문서는 규칙표 전체를 정적 복제하지 않으며, 공식 규칙표의 전체 family와 필터를 통해 개별 규칙을 빠짐없이 조회할 수 있도록 한다.
