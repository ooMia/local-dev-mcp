# PR 통합과 기본 브랜치 품질

채택 이유와 변경 범위는 [ADR 0006](../../adr/0006-native-pr-workflow-mvp.md)을 따른다.
이 개발용 CLI는 MCP 서버·dotenv를 시작하지 않는다. Git·uv·인증된 gh와 기존 개발환경을 사용한다.
새 Python dependency, 보존 태그, 별도 GitHub App 등록은 필요하지 않다.

최초 정리 기준선은 [별도 초기 게시 절차](initial-baseline.md)를 따른다. 이후에는 아래 정상 PR 통합을 사용한다.

PR 본문을 생성·갱신하는 별도 계약은 [PR Body Generator](pr-body.md)를 참고한다.

## 설치와 첫 활성화

소스를 editable 설치해 사용하는 경우, 먼저 workflow 구현을 main에 반영한 뒤 선택한 저장소 root에서 실행한다.
아직 이 module이 없는 main으로 checkout하면 hook이 불러올 Python 코드도 사라질 수 있다.
기능 브랜치의 임시 구현만 있는 상태를 실제 설치 완료로 취급하지 않는다.

```sh
uv sync --locked
uv run --locked python -m local_dev_mcp.tools.workflow check
uv run --locked python -m local_dev_mcp.tools.workflow install
uv run --locked python -m local_dev_mcp.tools.workflow doctor
```

install은 공통 Git 디렉터리의 local-dev-mcp/hooks에 네 가지 프로젝트 hook을 설치하고,
이 저장소의 core.hooksPath를 --local로 지정한다. system/global 설정과 기존 사용자 hook 파일은 수정하지 않는다.
extensions.worktreeConfig가 이미 활성화돼 있으면 현재 worktree의 --worktree 설정도 같은 경로로 지정한다.
다른 worktree의 고유 설정은 바꾸지 않는다. 그 worktree에서도 doctor를 실행하고 필요하면 install한다.
기존 공통 local 값과 현재 worktree 값은 local-dev-mcp/hooks-config-backup.json에 보존한다.
반복 설치는 최초 백업을 유지한다. 복원이 필요하면 백업의 각 값을 원래 local/worktree scope로 되돌린다.
이 프로젝트에서는 기존 전역 hook을 자동 연결하지 않는다. 다른 저장소에는 원래 전역 설정이 계속 적용된다.

doctor는 읽기 전용 JSON으로 유효 hook 경로·각 wrapper 내용과 실행 권한·Python 경로·백업 위치를 보고한다.
ready=true일 때 종료 0, 설치 누락·설정 불일치·누락/변경된 hook이면 종료 2다.
설치도 마지막에 동일 진단을 실행하며 유효 경로가 맞지 않으면 성공을 반환하지 않는다.
복잡한 include 또는 명령행 override로 우선순위가 달라지면 진단 결과를 확인한다.
wrapper는 설치에 사용한 Python 경로를 기록하므로 가상환경을 옮겼으면 다시 설치한다.
설치가 되기 전에는 이 제약이 적용되지 않는다.

원격 활성화는 이 변경의 workflow가 main에 도입되고 GitHub Actions에서 Project quality가 한 번 실행된 뒤,
관리 권한이 있는 gh 로그인 환경에서 수행한다.

```sh
uv run --locked python -m local_dev_mcp.tools.workflow configure-github
```

이 명령은 현재 gh 저장소의 기본 브랜치가 main인지 확인하고, 실제 workflow run의 check suite에서
GitHub Actions App ID를 조회한다. 저장소 병합 방법을 squash로 제한하고 다음 native ruleset 두 개를 생성·갱신한다.
같은 이름의 관리 규칙만 갱신하며 다른 ruleset은 유지한다. API 실패는 설정의 무변경을 뜻하지 않는다.

| 규칙 | 동작 |
| --- | --- |
| local-dev-mcp-main-gates | PR·squash·선형 이력·최신 Project quality 검사·대화 해결을 요구하고 강제 push·삭제를 막는다. bypass 없음 |
| local-dev-mcp-main-writer | main ref 갱신을 제한하며 실제 GitHub Actions App만 이 제한을 bypass한다 |

writer의 bypass는 별도 gates 규칙을 우회하지 않는다. 새 App이나 PAT 발급 대신 GitHub의 GITHUB_TOKEN을 사용한다.
이 권한은 특정 workflow 파일이 아닌 Actions App에 부여된다. write 권한을 부여하는 workflow 변경은 신뢰하는 코드 변경으로 검토해야 한다.
요금제·계정·조직 정책이 API를 거부하면 활성화되지 않은 상태로 보고한다. 활성화 전에 ruleset 쓰기 권한을 확인한다.
설정 복구가 필요하면 저장소 Settings의 Rulesets에서 이 두 관리 규칙을 수정·비활성화할 수 있다.

## 기능 통합

로컬 후보를 `main`에 통합하기 전에 `inspect_project_policy(operation="integrate_main", source_ref, expected_main_head, expected_source_head)`로 브랜치·조상·커밋된 영향 평가를 진단한다. 이는 **읽기 전용 로컬 사전 평가**로서 Draft PR, 정확한 HEAD의 CI, Ready, 최종 출처 및 병합 승인을 검증하지 않는다. 차단된 경우 허용된 다음 경로를 수행하며 `consolidated/*` 후보나 최초 기준선 예외를 일반 PR 통합 대신 적용하지 않는다.

작업 브랜치에서 구현·테스트·리팩터링 커밋을 만들고 기능별 PR을 연다. PR은 개발 커밋·설명·리뷰의 참조 지점이다.
별도 develop는 만들지 않는다. GitHub에 제출하지 않은 편집까지 자동 보존하지 않으며 과거 main 이력을 재작성하지 않는다.

원격 규칙 활성화 후에는 Actions 통합 workflow를 요청한다.

```sh
gh workflow run integrate.yml --ref main -f pr_number=123
```

workflow는 main의 신뢰하는 코드만 checkout한다. CLI와 MCP의 [공통 병합 구현](../git/remote-pr.md#명시적-pr-병합)은
PR URL·title·정확한 head를 GitHub에서 조회한다.
draft·닫힌 PR·main 외 base를 거부한다. 최종 commit에는 기능 제목과 다음 출처가 들어간다.

```text
Source-PR: https://github.com/OWNER/REPO/pull/123
Source-Head: <GitHub에서 조회한 전체 SHA>
```

고정 REST direct squash 요청에 예상 head와 제목·본문을 명시하므로 실제 병합 시 PR head가 바뀌면 거부된다.
병합 뒤 PR의 merge SHA와 실제 커밋의 단일 부모·제목·본문을 다시 확인한다. 확인한 결과만
`merged_verified`로 보고하며 불일치·확인 불가는 JSON으로 반환하고 CLI는 종료 2다.
최신 base·필수 검사·허용 병합 방법의 서버 강제는 실제 GitHub gates 활성화에 달려 있다.
실패 시 자동 우회·강제 병합·재시도를 하지 않는다.
보호 규칙이 비활성화된 저장소에서는 이 서버 강제를 기대할 수 없다. 현재 `merge-pr`는 PR head를 고정하지만
`Project quality` 성공을 직접 조회하지 않는다. 해당 보완은 최초 게시 범위에서 보류했으므로, 후속 PR 통합은
정확한 head의 검사 결과를 별도로 확인한 경우에만 운영한다. 원격 강제 부재를 CLI의 품질 보장으로 표현하지 않는다.
원격 보호 활성화 전 bootstrap에서는 같은 명령을 직접 실행할 수 있다.

```sh
uv run --locked python -m local_dev_mcp.tools.workflow merge-pr 123 --plan-only
uv run --locked python -m local_dev_mcp.tools.workflow merge-pr 123 --expected-head <head> --expected-plan-digest <digest>
```

`--plan-only`는 원격 쓰기 없이 메시지와 검토 digest를 출력한다. expected 두 인자는 함께 전달해야 한다.
둘 다 생략하면 같은 호출에서 계획을 관측한 뒤 재확인하여 실행하므로 기존 Actions 호출은 유지된다.
MCP는 두 검토값을 항상 요구하며, [독립 병합 권한과 결과 계약](../git/remote-pr.md#명시적-pr-병합)을 따른다.

원격이 실제 요구대로 거부하는지는 활성화 후 별도 수용 검증으로 확인한다. Python 경계 테스트는
실제 GitHub의 권한·규칙 상호작용이나 영속 보존의 증거가 아니다. PR의 기록을 사용하며 별도 archive 원장은 만들지 않는다.

## 로컬 main 검사

Actions의 테스트 범위는 [단위 테스트와 정적 검사](../../testing.md#actions와-로컬-검증의-범위)로 제한한다.
아래 전체 검사·hooks·평가 검증은 로컬의 별도 책임이며 `Project quality` 성공으로 대신하지 않는다.

공통 check는 config.toml의 format_check, lint, typecheck, unit, integration recipe를 차례로 실행한다.
선택한 recipe의 argv와 전체 default_targets를 재사용한다. 하나라도 실패하면 0으로 종료하지 않는다.
서버의 등록 작업 권한을 확장하는 MCP 도구가 아니라, 신뢰하는 프로젝트 설정을 실행하는 명시적 개발 CLI다.
자동 formatter는 포함하지 않는다.

pre-commit은 main에서만 전체 검사를 실행한다. unstaged·untracked 입력을 거부하고,
검사 전후 index tree와 HEAD를 확인한 뒤 공통 Git 디렉터리에 base/tree 검증 기록을 원자적으로 남긴다.
commit-msg와 ref hook이 Source-PR을 요구한다. ref hook은 검증된 tree/후보 SHA·부모·fast-forward·출처를 검사한다.
--no-verify나 update-ref만으로 미검증 main 이동을 허용하지 않는다.

원격 main을 로컬에 반영할 때도 먼저 별도 후보에서 검사한다. main을 다른 worktree에서 checkout하지 않은 상태의 예:

```sh
git fetch origin main
git switch --detach origin/main
uv run --locked python -m local_dev_mcp.tools.workflow approve-main
git branch --force main HEAD
git switch main
```

approve-main은 clean 후보와 기존 main의 조상 관계, 새 결과 커밋들의 출처를 확인하고 전체 검사를 통과한
정확한 HEAD만 승인한다. 기존 main이나 후보가 검사 중 바뀌면 거부한다. 별도 main worktree가 있으면
Git의 checkout 보호를 우회하지 말고 해당 worktree에서 검토된 fast-forward 절차를 사용한다.

기존 MCP merge_project_branch도 설치된 hook을 거치며 메시지에 Source-PR이 필요하다.
MCP Git 쓰기 제한은 60초이므로, 긴 검사는 source 브랜치에서 먼저 승인한다.

```sh
uv run --locked python -m local_dev_mcp.tools.workflow approve-main --squash
```

이 모드는 아직 squash하지 않은 source의 clean tree와 현재 main을 검사한다. 이후 MCP가 동일 tree를
main의 단일 결과 commit으로 만들 때 pre-commit은 검증 결과를 재사용한다. main·source tree가 달라지면
재사용하지 않는다. 최종 Source-PR과 부모/tree는 ref hook이 다시 검사한다.
기본 approve-main의 정확한 후보 SHA 승인과 달리, 이 모드는 단일-parent squash 결과를 위한 tree 승인이다.
timeout을 이유로 hook을 끄거나 동일 merge를 재실행하지 않는다.

hook 제거·정책 파일 수정 권한까지 막는 OS 보안 장치는 아니다.
ref 거부 후 index나 작업 파일은 바뀌었을 수 있다. main ref와 작업 파일 전체의 불변성을 혼동하지 않는다.
검증 기록은 Git 내부 파일이며 패키지나 버전 원장에 포함하지 않는다.

## 릴리스와 배포물

[기존 평가·누적 릴리스](../release/README.md)를 유지한다. 평가 파일을 main에 누적했다가
릴리스 준비 시 소비한다. 임시 평가가 과거 Git 이력에 존재하는 것은 금지하지 않는다.
최종 version·lock·changelog와 감사용 릴리스 영수증은 기존 검사로 검증한다.

```sh
uv run --locked python -m local_dev_mcp.tools.workflow package
uv run --locked python -m local_dev_mcp.tools.workflow artifacts /path/to/dist
```

package는 임시 디렉터리에 실제 wheel·sdist를 빌드하고 둘의 파일 목록을 검사한다.
uv_build의 source-exclude와 실제 archive 검사 모두 .changes 및 .release.toml의 유입을 차단한다.
배포 검사 실패를 자동 파일 삭제로 해결하지 않는다. 검사 성공은 tag·publish 성공을 의미하지 않는다.

현재 로컬 테스트는 Git hook의 실제 거부/승인·오래된 기록·부분 staging·다중 커밋 출처와 실제 uv archive를 검증한다.
원격 configure·workflow_dispatch·규칙 거부 및 실제 GitHub PR 조회는 별도 검증 대상이다.
