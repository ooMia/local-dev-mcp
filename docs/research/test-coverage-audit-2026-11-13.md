# 테스트 실행·스킵 원인·커버리지 관측 — 2026-11-13

## 현재 HEAD 재검증 (2026-11-13)

- 브랜치: `feat/worktrunk`, HEAD `3a4748ba731c79a2c50552acb79519daa79cc2c6`. 기준 코드 이후 추가된 커밋은 이 감사 문서 작성 1건뿐이다.
- 등록된 MCP 검사 recipe를 동일 범위(`all_tests=true`)로 재실행했다.
- Unit: **621 passed, 0 failed, 0 skipped** (`-n 0 -m unit`; 1.00초).
- Integration: **799 passed, 0 failed, 0 skipped** (`-n 4 -m "integration and not packaging"`; 282.55초).
- **합계 1,420 PASS, FAIL/ERROR 0, SKIP 0**. Packaging 1건과 실제 ChatGPT E2E는 포함되지 않았다.
- 초기 감사의 DB 관련 SKIP 81건은 재현되지 않았다. 당시에는 테스트 준비 조건을 충족하지 못했으나, 이번 실행에서는 DB 의존 테스트도 수행됐다. DB 준비 상태가 변한 시점·변경 주체는 이 테스트 결과만으로 알 수 없다.
- 아래 커버리지 수치는 **SKIP 81건이 존재하던 최초 감사의 잠정 계측치**이며, 이번 1,420 PASS 실행의 최신 커버리지가 아니다. MCP에 등록된 고정 recipe에는 커버리지 실행이 없어 이 재검증과 동일 조건으로 다시 계측하지 않았다.
- 테스트 재검증 중 소스·테스트의 추적 파일을 수정하지 않았으며, 완료 후 이 감사 문서만 보완했다. DB 자격 증명이나 볼륨을 변경하지 않았다.

## 최초 감사의 대상과 실행 범위

- 저장소: `local-dev-mcp`, 브랜치 `feat/worktrunk`, 기준 HEAD `6ed87454ac10a41e9d250bbbbf2accb00a8f243a`
- 환경: macOS 개발 호스트, 잠긴 `uv.lock`과 등록된 로컬 MCP 검사 recipe
- Unit: `start_project_task(task="unit", all_tests=true)`, `-n 0 -m unit`
- Integration: `start_project_task(task="integration", all_tests=true)`, `-n 4 -m "integration and not packaging"`
- Packaging: [설치 패키지 검증](../../tests/system/test_packaging.py) 1건은 선택식에서 명시적으로 제외. 실제 ChatGPT E2E는 로컬 pytest 결과의 대체물이 아니다.

| 항목 | Unit | Integration | 합계 |
| --- | ---: | ---: | ---: |
| PASS | 621 | 718 | 1,339 |
| FAIL / ERROR | 0 | 0 | 0 |
| SKIP | 0 | 81 | 81 |
| 테스트 선택 합계 | 621 | 799 | 1,420 |

실행하지 않은 Packaging 1건을 포함한 수집 대상은 1,421건이다.
일반 회귀의 미실행 대상을 성공으로 간주하지 않는다.
계측 없는 Integration 기준 실행에서도 `718 passed, 81 skipped`가 재현됐다.

## 최초 감사에서 관측한 스킵의 원인

임시 pytest 리포팅 Hook에서 `terminalreporter.stats["skipped"]`의 사유를 집계한 결과,
**81건 모두 `Local Podman database not configured`**였다. 다른 스킵 사유는 관측되지 않았다.

이 조건은 [GitHub Delivery 테스트 헬퍼](../../tests/support/gh_delivery.py)의
`connection_options()`가 `infra/postgres/.env`의 존재를 검사하는 경계다.
설정 파일이 없으면 DB 접속·트랜잭션·재시작 복구·Worker 처리가 테스트되지 않고 `pytest.skip()`된다.
따라서 결과의 의미는 81건 PASS도 FAIL도 아닌 **환경 미준비로 인한 검증 공백(BLOCKED)**이다.

실행 조건은 [PostgreSQL 18/Podman 개발 가이드](../../infra/postgres/README.md)를 따른다.
테스트용 `.env`·자격 증명과 Podman Compose 준비가 필요하며,
기존 볼륨을 삭제하는 `reset-to-18.sh --discard-data`를 이 진단의 일환으로 자동 실행하지 않는다.
이번 재검증에서는 같은 Integration 프로필의 799건이 모두 PASS로 전환됐다. DB 데이터 정리와 격리의 신뢰성은 별도 검증한다.

## Python 커버리지 (초기 감사의 잠정 관측치; 최신 재측정 필요)

`coverage.py 7.17.0`을 사용해 pytest 설정 단계부터 `local_dev_mcp` 소스를
line·branch 동시 계측했다. Unit 단일 프로세스와 Integration의 xdist 4 worker 데이터를 합산했다.
결과 JSON과 원본 데이터는 무시 디렉터리 `.lab/`에 저장했으며 테스트 전용
`conftest.py` 계측 코드는 실행 후 **원래 내용으로 복원**했다.

| 지표 | 관측 값 |
| --- | ---: |
| Line | **8,785 / 10,996 = 79.89%** |
| Branch | **2,209 / 3,522 = 62.72%** |
| Line+Branch 합산 기회 | **10,994 / 14,518 = 75.73%** |
| Missed line | 2,211 |
| Missed branch | 1,313 |
| 계측 JSON | `.lab/coverage-audit-20261113.json` |

**한계:** 일부 `local_dev_mcp` 모듈이 계측 시작 전 이미 import됐다는
`module-not-measured` 경고가 있었다. 별도 stdio·CLI·Python 자식 프로세스도 기본 설정에서는
측정되지 않는다. 계측 집계 시 JSON 출력 파일의 이름이 Coverage 데이터 파일 접두사와 겹쳐
`SQLITE_NOTADB` 경고가 발생했다(해당 JSON을 데이터 파일로 잘못 시도).
계측 결과는 성능·품질 게이트로 사용하기 전에 별도 보고서 이름과
프로세스 시작 단계에서 시작하는 재현 가능한 CLI 측정으로 재확인할 필요가 있다.

### 관심 영역 비교

| 소스 영역 | Line | Branch | 합산 |
| --- | ---: | ---: | ---: |
| `tools/git` | 2,881/3,191 | 689/930 | 86.63% |
| `tools/tasks` | 433/459 | 127/148 | 92.26% |
| `tools/workspace` | 1,295/1,408 | 310/376 | 89.97% |
| `tools/gh` | 1,564/2,787 | 384/1,062 | **50.61%** |
| `tools/workflow.py` | 149/314 | 29/136 | 39.56% |
| `tools/_workflow_hooks.py` | 18/81 | 0/34 | 15.65% |

특히 `tools/gh/delivery/`의 DB 상태·체크포인트·Claim·Recovery 모듈에는
10~20%대 관측 결과가 많다. 이는 위 81건의 스킵 및 자식 프로세스 계측 공백과
연관될 수 있으므로, 곧바로 테스트 부재로 단정하지 않는다.
현재 DB 의존 81건이 실제로 실행되는 환경이므로 동일 범위의 재계측을 우선해 커버리지 증가 여부를 확인한다.

## 후속 검증 우선순위

1. **DB Integration 재검증 완료:** 81건의 초기 스킵은 이번 실행에서 사라졌다. 향후 DB 필수 CI의 준비 실패가 SKIP으로 묻히지 않도록 검증 정책을 분리하고, 테스트용 데이터 격리·정리 상태를 확인한다. 인증 정보는 로그·보고서에 기록하지 않는다.
2. **정규 커버리지 재측정 (최우선 미완료):** 현재 `[tool.coverage.run]`의 source·branch 설정을 재사용하고, `uv run --locked coverage run -m pytest -n 0 -m 'not packaging and not e2e'`로 import 이전부터 순차 계측한다. `coverage json -o .lab/coverage-report.json`은 입력 데이터 파일과 이름을 분리한다. 현재 MCP에는 coverage recipe가 등록되어 있지 않으므로 CLI 실행 또는 명시적인 recipe 추가·서버 재시작이 필요하다. 자식 프로세스 수집은 별도 opt-in으로 검증한다.
3. **조건부 기능 검증 분리:** Packaging 1건을 명시적 별도 프로필로 기록하고, DB 필수 CI에서는 설정 누락을 조용히 SKIP하지 않도록 별도 정책을 검토한다.
4. **나머지 미측정 분석:** DB 의존 범위가 해소된 뒤 `workflow.py`, Hooks, 릴리스 CLI 등 실제로 남는 미측정 분기를 정상·거부·실패 사례 중심으로 평가한다.
5. **의존성 경고 확인:** `beartype`의 `@runtime_checkable` 관련 DeprecationWarning은 현재 실패는 아니지만 향후 Python 버전 호환성 검토 항목으로 분리한다.

현재 FAIL과 SKIP이 0건이어도 과거의 스킵 원인이 제거된 코드 변경이라고 단정할 수 없다. 또한 초기 커버리지를 이번 실행의 수치로 재사용하지 않는다.
