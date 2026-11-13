# 테스트 실행·스킵 원인·커버리지 관측 — 2026-11-13

## 대상과 실행 범위

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

## 스킵의 실제 원인

임시 pytest 리포팅 Hook에서 `terminalreporter.stats["skipped"]`의 사유를 집계한 결과,
**81건 모두 `Local Podman database not configured`**였다. 다른 스킵 사유는 관측되지 않았다.

이 조건은 [GitHub Delivery 테스트 헬퍼](../../tests/support/gh_delivery.py)의
`connection_options()`가 `infra/postgres/.env`의 존재를 검사하는 경계다.
설정 파일이 없으면 DB 접속·트랜잭션·재시작 복구·Worker 처리가 테스트되지 않고 `pytest.skip()`된다.
따라서 결과의 의미는 81건 PASS도 FAIL도 아닌 **환경 미준비로 인한 검증 공백(BLOCKED)**이다.

실행 조건은 [PostgreSQL 18/Podman 개발 가이드](../../infra/postgres/README.md)를 따른다.
테스트용 `.env`·자격 증명과 Podman Compose 준비가 필요하며,
기존 볼륨을 삭제하는 `reset-to-18.sh --discard-data`를 이 진단의 일환으로 자동 실행하지 않는다.
DB 준비 후 같은 Integration 프로필을 다시 실행하고 스킵 수 및 데이터 정리 상태를 확인해야 한다.

## Python 커버리지 (잠정 관측치)

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
DB를 준비한 뒤 동일 범위의 측정으로 얼마나 회복되는지 우선 비교한다.

## 후속 검증 우선순위

1. **DB Integration 복구:** 안전한 테스트 전용 PostgreSQL 18 환경을 준비하고 스킵 81건이 실제 PASS/FAIL로 전환되는지 확인한다. 인증 정보는 로그·보고서에 기록하지 않는다.
2. **정규 커버리지 재측정:** 현재 `[tool.coverage.run]`의 source·branch 설정을 재사용하고, `uv run --locked coverage run -m pytest -n 0 -m 'not packaging and not e2e'`로 import 이전부터 순차 계측한다. `coverage json -o .lab/coverage-report.json`은 입력 데이터 파일과 이름을 분리한다. 자식 프로세스의 수집은 별도 opt-in으로 검증한다.
3. **조건부 기능 검증 분리:** Packaging 1건을 명시적 별도 프로필로 기록하고, DB 필수 CI에서는 설정 누락을 조용히 SKIP하지 않도록 별도 정책을 검토한다.
4. **나머지 미측정 분석:** DB 의존 범위가 해소된 뒤 `workflow.py`, Hooks, 릴리스 CLI 등 실제로 남는 미측정 분기를 정상·거부·실패 사례 중심으로 평가한다.
5. **의존성 경고 확인:** `beartype`의 `@runtime_checkable` 관련 DeprecationWarning은 현재 실패는 아니지만 향후 Python 버전 호환성 검토 항목으로 분리한다.

실패가 0건이라는 사실만으로 위 스킵·계측 공백을 통과한 것으로 평가하지 않는다.
