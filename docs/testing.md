# 검사 실행과 테스트 환경

테스트 배치·명명·격리·순차 실행 판단은 [개발 규약](conventions.md#타입과-테스트)이 소유한다.
이 문서는 검사 명령과 테스트 환경의 준비·정리·증거 범위를 설명한다.

## 실행 경로

```sh
uv sync --locked
uv run --locked pyright
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
uv run --locked pytest                         # 일반 회귀
uv run --locked pytest -n 0                    # 동일 회귀의 순차 재현
uv run --locked pytest -n 0 --durations=15      # setup/call/teardown 병목 확인
uv run --locked pytest -n 0 -m packaging        # 설치 패키지 검증
```

pytest 기본 선택은 `pyproject.toml`을 따른다. worker 수는 `-n 2`처럼 조절하며,
`-n 0`과 `-m packaging`은 해당 기본 옵션을 덮어쓴다.
일반 회귀는 개발 환경의 설치·진입점을 사용하므로 별도 가상환경을 수동 준비하지 않는다.
Pylance와 CLI의 버전별 진단은 다를 수 있으며 저장소의 타입 검사 기준은 lock된 CLI다.

## 테스트 자원과 정리

fixture가 Git workspace·stdio 설정을 테스트별로 준비해 index·HEAD·hooks·설정 변경을 격리한다.
일반 GPG 홈·생성 키·서비스는 worker별 session에서 사용하고, 잠긴 키·daemon 상태 변경은 전용 환경으로 분리한다.
자원 준비·정리의 구현은 [공유 fixture](../tests/conftest.py)를 참조한다.

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
