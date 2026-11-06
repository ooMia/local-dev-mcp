# Git workflow와 명시적 버전 관리 조사

최신 채택 결정은 [ADR 0005](../adr/0005-assessed-squash-and-batched-releases.md), 현재 규칙·도구 계약은 소유 문서를 따른다. 아래는 2026-11-04 조사 당시의 근거와 후보다.

조사일: 2026-11-04. 상태: 1차 조사 근거 및 당시 의사결정 후보. 채택된 개발 규약이나 구현 계약이 아니다.
후속 답변에서 사용자는 작업 종류 중심 브랜치·누적 릴리스·실제 SemVer 평가·origin/main부터의 커밋 재구성을 선택했다.
초기 major 전제와 이력 보존 추천은 후속 방향으로 대체되었다. 최신 근거·선택지는
[병합 검사와 이력 재구성 조사](git-workflow-enforcement-and-history-2026-11-04.md)를 참조한다.

현재 문서 권한은 [ADR 0002](../adr/0002-document-authority.md), 개발 규약은
[conventions](../conventions.md), 공개 Git 입력·실패 계약은 [Git 계약](../tools/git/README.md)이 소유한다.

## 현재 저장소에서 확인한 사실

- 로컬 main은 d0f4d2f1d164a2527180f6649874bcc844025c6f이며 조사 전 작업 트리는 깨끗했다.
- 로컬 브랜치 총 9개 중 main 외 7개는 모든 커밋이 main에 도달한다.
- wip/github-automation-20261030에는 main에 직접 도달하지 않는 커밋 9개가 있다.
  git cherry에도 9개가 남지만, 이미 선택 이관된 기능의 의미적 동등성까지 판정한 것은 아니다.
- origin/main 대비 ahead 145는 로컬 remote-tracking ref를 기준으로 한 값이다.
  이번 조사에서 fetch나 원격 상태 검증을 하지 않았다.
- pyproject.toml의 패키지 버전은 0.1.0이며 mcp/server.py에도 같은 값이 하드코딩되어 있다.
- merge_project_branch는 현재 local main fast-forward만 지원한다.
  squash와 버전 갱신은 아직 구현되지 않았다.
- 기존 commit·branch 이력을 재작성하거나 브랜치를 삭제하지 않았다.

## 구분할 개념

작업 브랜치는 하나의 검토 가능한 변경을 모으는 임시 공간이다.
유지보수 브랜치는 이미 릴리스된 특정 버전 계열의 수정 수명을 관리한다.
버전 영향은 공개 호환성에 따라 다음 버전의 어느 부분을 바꿀지에 관한 결정이다.
이 세 개념을 major/minor/patch라는 이름만으로 같은 것으로 취급하지 않는다.

일반적인 Git·SemVer 용어는 프로젝트 고유 용어가 아니므로 GLOSSARY.md에 기계적으로 추가하지 않는다.
이 프로젝트만의 새로운 작업·결과 개념이 합의되면 해당 용어를 그때 정의한다.

## 오픈소스의 1차 근거

| 사례 | 확인된 관례 | 참고할 점 |
| --- | --- | --- |
| CPython | main에 대한 topic PR을 squash 병합하며 유지보수 대상은 3.12 같은 계열로 분리한다. | 작업 단위와 지원 계열을 구분하고 최종 squash 메시지를 정리한다. |
| pytest | main에서 your-bugfix-branch-name을 생성하는 예제를 제공하고, 호환성 영향에 따른 major/minor/micro와 1.2.x backport 흐름을 별도로 설명한다. | topic 이름과 다음 버전 영향을 같은 축으로 강제하지 않는다. 1.2.x는 예시 버전이다. |
| Ruff | 아직 API 안정성을 선언하지 않았고 breaking change를 minor에 담는 custom versioning을 명시한다. | 0.x에서의 정책을 프로젝트가 분명히 선언해야 한다. 이 정책을 표준 SemVer의 major 정의로 복제하지 않는다. |

출처:
- [CPython Git bootcamp: squash와 backport](https://devguide.python.org/getting-started/git-boot-camp/#accepting-and-merging-a-pull-request)
- [pytest 기여 안내: topic·버전·backport](https://docs.pytest.org/en/stable/contributing.html)
- [Ruff versioning](https://docs.astral.sh/ruff/versioning/)

조사한 문서들은 major/topic·minor/topic·patch/topic을 공통 topic 명명 표준으로 제시하지 않는다.
이는 모든 오픈소스에서 해당 명명을 사용하지 않는다는 주장이 아니다.
local-dev-mcp가 채택하면 명시적인 프로젝트 자체 규약으로 기록해야 한다.

## 비교할 브랜치·버전 영향 후보

| 후보 | 예시 | 이점 | 비용 |
| --- | --- | --- | --- |
| 영향 중심 | major/git-workflow, minor/ref-compare, patch/stale-head | 브랜치 목록에서 예상 버전 영향을 바로 확인하고 로컬 병합 입력과 대조하기 쉽다. | 작업 중 영향이 바뀌면 이름 또는 승인된 영향 정보를 정정해야 하고 버전 미변경 작업 규칙이 필요하다. |
| 작업 종류 중심 + 별도 영향 | feat/ref-compare, fix/stale-head; squash의 feat!, feat, fix 등 | 작업 종류와 버전 영향을 구분하고 Conventional Commits 기반 도구와 연결하기 쉽다. | 최종 메시지나 별도 영향 정보가 필수이며 브랜치명만으로 버전을 결정할 수 없다. |
| 두 축을 모두 표기 | major/refactor/git-workflow | 종류와 영향이 모두 보인다. | 이름이 길고 중복 정보의 불일치 처리 규칙이 필요하다. |

이 표는 후보이며 사용자 선택 전 특정 형식을 의무화하지 않는다.
브랜치명이 영향의 근거가 되더라도 실제 변경의 호환성 검토를 대신할 수 없다.

## uv와 초기 major의 의미

설치된 uv 0.13.1의 도움말과 공식 문서에서 uv version --bump major/minor/patch,
--dry-run, --output-format json, --no-sync를 확인했다.
uv version은 버전 갱신을 제공하며 Git squash commit·tag는 별도 조합 책임으로 설계한다.

읽기 전용 확인에서는 임시 cache 경로를 사용해 다음 dry-run을 실행했다.

```bash
uv --cache-dir /private/tmp/local-dev-mcp-workflow-uv-cache version --bump major --dry-run --frozen --offline --output-format json
uv --cache-dir /private/tmp/local-dev-mcp-workflow-uv-cache version 1.0.0rc1 --dry-run --frozen --offline --output-format json
```

결과의 version은 각각 1.0.0과 1.0.0rc1이었다. 이 명령은 pyproject.toml이나 uv.lock을 갱신하지 않았다.
--frozen은 이 관측에서 lock·sync 부수 변경을 막기 위한 옵션이며 실제 버전 갱신 절차로 채택한 것이 아니다.
실제 절차에서는 pyproject.toml·uv.lock·MCP 응답 버전의 일치와 검사 순서를 정해야 한다.

SemVer의 0.x는 초기 개발이며 1.0.0은 공개 API를 정의하는 기준이다.
1차 논의에서는 major 분류를 전제했지만, 후속 답변은 실제 공개 계약 비교로 버전을 재평가하도록 이를 대체했다.
현재 0.x 단계의 다음 릴리스인지 첫 Stable인지와 공개 안정 계약의 범위는 별도 결정이 필요하다.
실험적 내부 Document Core를 공개 MCP·CLI·설정 계약과 같은 안정성 범위로 자동 확대하지 않는다.

출처:
- [uv 패키지 버전 갱신](https://docs.astral.sh/uv/guides/package/#updating-your-version)
- [uv version CLI](https://docs.astral.sh/uv/reference/cli/#uv-version)
- [SemVer 2.0.0](https://semver.org/spec/v2.0.0.html)
- [기존 Document Core 결정](../adr/0004-document-core-identity-source-boundary.md)

## 자동화 도구에서 참고할 점

| 대안 | 확인된 동작 | 적용 시 비용 |
| --- | --- | --- |
| release-please | Conventional Commits를 읽고 release PR을 준비한다. Python strategy에서는 docs:도 릴리스 대상으로 본다. | 문서 전용 변경을 none으로 취급하려면 기본값과 다른 명시적 정책이 필요하다. |
| Changesets | 변경 파일에 package별 영향과 설명을 기록하고, 여러 변경은 최대 영향으로 집계한다. | 공식 non-npm 안내도 package.json을 요구하므로 단일 Python 프로젝트에 그대로 도입하면 별도 원장·adapter가 생긴다. |
| Python Semantic Release | commit parser로 버전을 결정하며 release branch는 stable/prerelease 채널을 구분한다. | major/라는 topic prefix를 기본 version parser로 해석한다고 볼 수 없다. |
| uv 중심의 내부 도구 | 명시적으로 확정한 영향·버전으로 uv version을 실행하도록 조합할 수 있다. | 입력 검증·최종 commit 기록·실패 후 관측은 프로젝트가 소유해야 한다. 아직 구현되지 않은 후보이다. |

출처:
- [release-please README](https://github.com/googleapis/release-please)
- [Changesets beyond npm](https://changesets.dev/guide/beyond-npm)
- [Changesets technical decisions](https://changesets.dev/guide/technical-decisions)
- [PSR multibranch releases](https://python-semantic-release.readthedocs.io/en/latest/concepts/multibranch_releases.html)
- [PSR uv integration](https://python-semantic-release.readthedocs.io/en/latest/configuration/configuration-guides/uv_integration.html)
- [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/)

브랜치 삭제나 최종 squash 메시지 편집 뒤에도 영향 판단이 남도록 merged commit 또는
저장소 metadata 중 한 곳을 최종 기록으로 정할 필요가 있다. 브랜치명은 검토할 의도의 표현이며
실제 변경 내용과 불일치하면 조용히 낮은 버전으로 처리해서는 안 된다는 것이 이 조사의 설계 제안이다.
Conventional Commits 규격은 브랜치 명명 규격이 아니다.

uv와 별도 release 도구가 각각 버전을 증가시키면 이중 증가할 수 있으므로
버전 원장을 갱신하는 책임은 한쪽에 둔다. 이중 writer 방지는 local-dev-mcp에 대한 설계 판단이다.

독립 조사에서는 의존성 없는 임시 프로젝트에서 uv 0.13.1의 --no-sync 적용도 검증했다.
0.1.0에서 major 적용 뒤 pyproject.toml과 새 uv.lock의 버전은 1.0.0이었고 .venv는 생성되지 않았다.
이는 임시 프로젝트의 CLI 관측이며 실제 저장소의 의존성 lock·릴리스·호환성 검증은 아니다.

## 결정할 사항과 문서 소유권

1. 버전 영향의 기준을 브랜치명, 최종 squash 메시지 또는 별도 정보 중 어디에 둘지.
2. main 병합마다 버전을 올릴지, 여러 병합을 모아 릴리스할 때 올릴지.
3. 현재 major 작업의 목표 버전과 공개 안정 계약의 범위.
4. 이미 존재하는 main 이력을 보존할지, 일부를 재작성할지.
5. 선택된 형식에 따른 버전 미변경 작업, 영향 변경, 병렬 작업, 충돌·실패·재시도 및 tag 생성 규칙.
6. 저장소 개발 규약과 선택 workspace를 다루는 범용 MCP 기능의 적용 범위.
7. 기존 WIP의 보존·선택 이관·폐기 판단과 나머지 로컬 브랜치의 정리 순서.

합의 뒤 변경 이유와 대안은 새 ADR, 개발자가 지킬 규칙은 conventions.md,
실제 Git/uv 도구 입력·결과·실패 보장은 기능별 계약, 확인 명령은 testing.md가 소유하도록
최종 계획에 배치한다. 조사 문서에서 미결정 후보를 Accepted 결정이나 구현된 기능으로 승격하지 않는다.
