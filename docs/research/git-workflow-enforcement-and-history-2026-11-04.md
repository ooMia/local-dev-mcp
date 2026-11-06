# Git workflow: 병합 검사·누적 릴리스·이력 재구성 조사

최신 채택 결정은 [ADR 0005](../adr/0005-assessed-squash-and-batched-releases.md), 현재 규칙·도구 계약은 소유 문서를 따른다. 아래는 2026-11-04 조사 당시의 근거와 후보다.

조사일: 2026-11-04. 상태: 사용자 선택에 따른 방향 정리와 미확정 설계 후보.
이 문서는 채택 ADR·현재 제품 계약이 아니며 구현·이력 변경·원격 설정 적용의 완료를 뜻하지 않는다.

## 갱신된 사용자 방향

사용자의 후속 답변이 [1차 조사](git-workflow-versioning-2026-11-04.md)의 초기 major 전제와 이력 보존 추천을 대체했다.

- 작업 브랜치는 작업 종류 중심을 선호한다.
- main 등 기본 브랜치의 병합 시 SemVer 영향을 평가하고, 평가·버전 관리 절차가 빠지면 병합이 완료되지 않게 강제하기를 원한다.
- 버전 숫자는 여러 병합을 모아 릴리스할 때 가장 큰 영향으로 한 번 갱신한다. 각 병합의 영향과 최대 영향을 쉽게 확인해야 한다.
- 현재 작업을 일괄 major로 단정하지 않는다. 최종 변경과 공개 계약을 비교해 적절한 버전을 평가한다.
- origin/main을 부모로 관련 파일 변경을 의미 있게 묶어 커밋을 재구성한다. 에이전트가 이력에서 변화의 이유·흐름을 읽을 수 있어야 한다.
- 외부 라이브러리·시스템 도구를 사용하거나 local-dev-mcp 내부 도구로 조합할 수 있다.

## 1차 근거의 벤치마킹

| 프로젝트·도구 | 직접 확인한 동작 | local-dev-mcp에 적용할 때 추가로 필요한 것 |
| --- | --- | --- |
| Trio | main 대상 PR에서 src/trio의 테스트 외 변경이 있으면 newsfragments 경로 변경을 요구한다. skip newsfragment label 예외가 있다. pyproject에는 breaking/feature/bugfix/doc/deprecated/removal/misc 등 Towncrier 유형이 있다. | 해당 검사는 모든 PR의 SemVer 기록·내용을 보장하지 않는다. 우리 프로젝트의 더 강한 누락 검사와 분류 규칙이 필요하다. |
| Microsoft Conductor | uv와 Towncrier를 사용한다. 일반 PR은 fragment를 추가하고 release-prep은 버전·취합 changelog·uv.lock을 함께 검사한다. CI와 integration test가 같은 검사 script를 호출한다. | upstream의 added/fixed/changed/removed 분류는 major/minor/patch 판정과 다르다. 예외 label·Dependabot 우회 여부도 그대로 복사하지 않는다. |
| Changesets | PR별 변경 파일로 릴리스 영향을 기록하고 여러 변경의 최대 bump를 계산한다. blocking status 검사와 empty changeset을 지원한다. | changed packages가 없으면 검사 성공하므로 모든 PR의 평가 강제와 같지 않다. non-npm도 package.json 원장이 필요해 단일 Python 프로젝트에는 추가 비용이다. |
| Commitizen | Conventional Commits 검사·changelog·다음 버전 계산을 제공하고 uv version provider는 pyproject.toml과 uv.lock을 갱신한다. | 최종 squash 메시지를 검사해야 한다. 메시지 규격만으로 실제 API 호환성을 증명하지 못하며, 별도 평가 근거가 필요하다. |

출처:
- [Trio newsfragment workflow](https://github.com/python-trio/trio/blob/main/.github/workflows/check-newsfragment.yml); 조회한 workflow blob SHA: a599b30ea665c7aa1401f83528acb8c9ec58fde8
- [Trio Towncrier 설정](https://github.com/python-trio/trio/blob/main/pyproject.toml)
- [Conductor changelog workflow](https://github.com/microsoft/conductor/blob/main/.github/workflows/changelog.yml)
- [Conductor 공통 검사 script](https://github.com/microsoft/conductor/blob/main/.github/scripts/enforce-changelog-contract.sh)
- [Conductor 변경 기록 계약](https://github.com/microsoft/conductor/blob/main/changelog.d/README.md)
- [Changesets 자동화](https://changesets.dev/guide/automating)
- [Changesets CLI](https://changesets.dev/guide/cli)
- [Changesets beyond npm](https://changesets.dev/guide/beyond-npm)
- [Commitizen version provider](https://commitizen-tools.github.io/commitizen/config/bump/)
- [Commitizen bump 명령](https://github.com/commitizen-tools/commitizen/blob/master/docs/commands/bump.md)

확인한 workflow 파일의 존재는 실제 저장소 보호 설정에서 required check로 지정됐다는 증거가 아니다.
이들은 기능과 공개 구현의 표본이며 전체 오픈소스의 사용 비율 조사도 아니다.

## 병합과 릴리스의 서로 다른 완료 조건

두 사용자 요구를 함께 만족시키는 제안은 다음과 같다.

1. 일반 병합에서는 변경 영향·호환성 근거·검증·필요한 migration을 기록하고 검사한다.
2. 기록 누락·무효·후속 변경으로 오래된 검사 결과는 병합을 거부한다.
3. 일반 병합은 패키지 버전을 올리지 않는다.
4. 릴리스는 마지막 릴리스 기준 이후 모든 미소비 기록과 최종 후보를 검토하고 최대 영향을 계산한다.
5. 선택한 버전 갱신 담당 도구 한 곳이 버전·lock·서버 식별 버전을 일치시키고 changelog를 취합한다.

이 구분은 아직 최종 합의가 필요한 해석이다. “숫자를 매 병합 바꿔야 한다”와 “여러 병합 뒤 한 번 바꾼다”를 동시에 강제할 수는 없다.

누적 화면 예시는 다음과 같다. 아래는 합성 설명이며 현재 코드의 확정 분류가 아니다.

| 변경 | 영향 | 이유 |
| --- | --- | --- |
| 기존 MCP 필수 입력을 변경 | major | 기존 클라이언트 호출이 거부됨 |
| 선택적인 새 MCP 도구를 추가 | minor | 기존 호출 계약을 유지하며 기능 추가 |
| 기존 동작의 오류 수정 | patch | 공개 계약 범위 내의 잘못된 구현 수정 |
| 공개 동작을 바꾸지 않는 문서 수정 | none | 배포 영향 없음으로 명시 평가 |

major 1건·minor 2건·patch 3건처럼 수와 근거를 보여주고 major의 원인이 된 변경을 연결한다.
안정판 x>0에서는 최대 영향으로 한 번 증가시키는 정책을 사용할 수 있다.
0.x 정책, revert·아직 배포하지 않은 API의 수정 및 여러 변경의 상호작용은 별도 규칙이 필요하다.
단순 최대값 계산은 전체 릴리스와 마지막 공개 기준 사이의 호환성 검토를 대체하지 않는다.

### 후보 A: Towncrier + 작은 공통 검사·릴리스 도구

- 작업별 파일에 명시적 영향과 사용자가 읽을 근거를 기록한다.
- Towncrier는 분산 작성한 설명을 릴리스 때 취합한다.
- 공통 Python 검사는 해당 병합에서 추가·갱신한 기록, 유효한 유형·본문 및 미소비 기록의 보존을 확인한다.
- 동일 검사를 local-dev-mcp 병합 경로와 CI에서 재사용한다.
- 누적 계획 조회는 영향별 개수·최대 영향·그 원인·비교 기준 SHA를 반환한다.
- 버전 변경은 uv version이 단독으로 맡도록 조합한다.

장점은 작업 종류·commit 제목과 SemVer 판단을 구분해 이유를 저장할 수 있다는 것이다.
비용은 Towncrier 자체에 없는 SemVer 규칙·상태 검사를 작은 내부 코드로 유지해야 한다는 점이다.
유형 이름·파일 schema·도구 이름·등록 범위는 미확정이다.

Towncrier check는 changelog 자체가 바뀌면 검사를 생략하므로 이를 그대로 “모든 병합에서 평가 강제”로 설명하면 안 된다.
Conductor처럼 일반 변경과 릴리스 변경의 검사를 구분해야 한다.
Towncrier build는 기본적으로 소비한 fragment를 지우고 git rm을 사용할 수 있다.
따라서 릴리스 도구는 파일뿐 아니라 index 부작용도 포함해 상태와 결과를 검증해야 한다.
[공식 Towncrier CLI](https://towncrier.readthedocs.io/en/stable/cli.html)

### 후보 B: Commitizen + 최종 squash 메시지 검사

- feat/fix 및 !/BREAKING CHANGE를 최종 squash 메시지에서 검사한다.
- Commitizen이 누적 commit으로 다음 버전을 계산하고 changelog를 만든다.
- uv provider를 쓰면 Commitizen이 버전 갱신 담당이다. 같은 작업에서 uv version bump를 또 실행하지 않는다.
- 충분한 호환성 근거를 메시지 본문에 요구하고 실제 계약 비교를 별도 수행한다.

장점은 기존 Python 도구의 계산·메시지 검사를 활용하고 별도 fragment 부담을 줄인다는 것이다.
비용은 최종 메시지에 판단이 결합되고 의미상 잘못된 feat/fix/!를 구문 검사만으로 잡지 못한다는 점이다.
작업 중 commit 제목의 규칙을 지킨 사실만으로 최종 main의 squash 메시지가 검증됐다고 볼 수 없다.

## 강제할 수 있는 범위

- local-dev-mcp의 병합 기능 안에서는 사전 검증 실패 때 merge를 시작하지 않도록 할 수 있다.
  현재 fast-forward 전용 merge_project_branch는 이 기능을 갖고 있지 않다.
- 로컬 hooks는 보조 안내·검사로 쓸 수 있으나 직접 Git 실행·hook 변경을 막는 OS 권한 경계가 아니다.
- GitHub에서 모든 통합 경로에 강제하려면 PR·required status check·직접 push/bypass 정책을 적절히 설정해야 한다.
  CLI 도구 코드만 추가해 서버 측 보호가 켜졌다고 주장하지 않는다.
- 후보 SHA·base SHA에 묶인 검사여야 하며 대상이 바뀌면 재검사해야 한다. merge queue를 사용하면 merge_group 대상도 별도로 처리해야 한다.
- 기록의 존재·형식·일관성은 자동화할 수 있다. 행동 호환성 전체의 올바른 분류를 도구만으로 증명할 수는 없다.

출처: [GitHub 보호 브랜치](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
현재 ooMia/local-dev-mcp의 보호 설정은 확인·변경하지 않았다.

## 원격 기준과 이번 버전의 재평가

읽기 전용 git ls-remote로 실제 원격을 확인했다.

| 기준 | 전체 SHA |
| --- | --- |
| origin/main | 2ae0de2d520725b17355d0f2fca90dd64240a11d |
| origin/develop | 74f76a7393159b5e2e17616c381705493dab7db2 |
| 현재 local main | d0f4d2f1d164a2527180f6649874bcc844025c6f |

원격 태그 목록도 조회했으며 결과는 비어 있었다.
GitHub 연결의 저장소/Release 조회는 404였으므로 Release·패키지 게시가 없다고 판정한 것은 아니다.
git ls-remote는 fetch나 tracking ref 변경을 하지 않았다.

origin/main은 14개 파일의 초기 scaffold다. 당시 버전도 0.1.0이고 현재도 같다.
기존 local-dev-mcp run은 Hello를 출력하고 종료하지만 현재는 설정을 요구하는 stdio MCP 서버로 동작한다.
root main() export는 유지됐으나 local_dev_mcp.cli와 core.server 경로는 삭제됐다.
따라서 실제 행동·import 비호환은 관측되지만 기존의 보호된 Stable 계약을 깨뜨렸는지는 별도 판단이다.

SemVer는 0.y.z를 초기 개발로 두고 1.0.0에서 public API를 정의한다.
초기 scaffold 대비 변경량이 크거나 비호환이 있다는 사실만으로 1.0.0이 의무가 되지 않는다.
계속 초기 개발이면 0.2.0이 합리적인 후보이고, 지금부터 안정 계약을 약속한다면 첫 1.0.0이 대안이다.
현재 어떤 번호도 최종 결정·적용하지 않았다.
[SemVer 규격 및 초기 개발 FAQ](https://semver.org/spec/v2.0.0.html)

릴리스 호환성의 정식 비교 기준은 마지막 보호 대상 릴리스의 immutable artifact/SHA다.
origin/main은 이력 재구성의 부모이며 자동으로 마지막 사용자 릴리스가 되는 것은 아니다.
MCP·CLI·설정의 공개 범위와 실험적 Document/Event/GitHub 내부 모델을 구분해야 한다.

## 커밋 재구성 후보

관측한 main 대비 origin/main delta는 145개 commit, merge commit 0개, 279개 변경 경로,
40,644 insertions·133 deletions다. 보존할 main tree는 94bafaef7a1b8aaa064ae36a4314a1bc61a6ae2c이다.

새 이력은 원격 main을 부모로 다음 의미 단위로 적층하는 후보를 검토했다.

1. workspace 고정 MCP 시작·권한·Git 실행·서명 준비
2. bounded 파일 탐색·읽기·관측 참조
3. 관측 기반 수정·생성·삭제와 receipt
4. 등록 CLI 작업·환경 분리·진단 처리
5. DAG·설치 패키지·개발 흐름 검증
6. Git branch/ref 비교·병합·선택 이관
7. Document Core와 읽기 전용 문서 진단
8. Event Core와 격리 PostgreSQL 개발환경
9. 검증된 webhook 수락·영속 queue
10. 봉인 plan·intent·checkpoint·lineage와 복구 판정
11. 명시적으로 binding한 read-only issue worker
12. durable attempt로 제한한 label worker와 crash/TCP/TLS 검증

이는 폴더별 일괄 복사가 아니다. runtime·MCP 수동 등록·settings·pyproject/lock·공유 fixture·문서는
후속 기능을 미리 import하지 않도록 단계별로 조립하고 각 커밋의 관련 검증을 함께 구성해야 한다.
검증 결과에 따라 단위를 합치거나 나누며 12개를 고정 quota로 삼지 않는다.
수정·실험의 시간 순서를 재현하는 대안보다 최종 기능·계약을 따라 읽기 쉬운 이력을 우선하는 제안이다.

재구성 완료의 검증은 old main과 동일한 tracked tree라는 조건과 각 중간 커밋의 동작·검사다.
동일 tree만으로 중간 커밋의 실행 가능성을 보장할 수 없다.
이번에 작성하는 workflow 규약·검사 기능은 기존 제품 tree 재구성과 구분한 후속 commit으로 두면 두 검증을 분리할 수 있다.

원래 145개 커밋과 branch refs·untracked 연구 문서는 외부 복구 사본에 보존해야 한다.
WIP는 9개 고유 SHA가 있어도 이미 이식한 소스가 포함되어 있어 전체를 다시 병합하면 안 된다.
특히 과거 SQLite 기반 GitHub MCP/운영 CLI와 현재 PostgreSQL 내부 delivery 모델은 다른 범위다.
원본 main을 재구성 원본으로 고정하고 WIP의 미반영 기능은 별도 분석·결정 대상으로 남기는 제안이다.

동일 origin/main 위에 새 커밋을 쌓으면 원격 main 업데이트는 그래프상 fast-forward가 가능하다.
기존 로컬 main 교체, 원격 develop 처리 및 push는 각각 별도 작업이다.
이번에는 branch/index/commit/원격을 변경하지 않았다.

## 최종 계획에 남은 결정

- 영향 기록의 저장·검사·취합: Towncrier 중심 후보 A 또는 Commitizen 중심 후보 B.
- 강제 범위: local-dev-mcp 내부만인지, 동일 검사를 원격 required check로도 준비할지.
- 안정성 단계: 초기 개발 유지와 0.x 정책인지, 첫 Stable 공개 계약 확정인지.
- 선택에 따른 none·revert·누적 계획 변경·릴리스 소비·tag 및 실패 후 관측 규칙.
- 기존 fast-forward 도구의 호환 처리와 새 기능의 공개/내부 등록 범위.

합의된 설계 이유는 ADR, 개발자 규칙은 conventions, Git/uv 기능의 실제 보장은 소유 계약,
검사 명령은 testing 문서에 반영할 계획이다. 연구 문서를 최종 규약으로 오인하지 않는다.
