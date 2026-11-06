# Agent-operated private GitHub PR publication

이 기능은 [PR Body Generator](../workflow/pr-body.md)와 [ADR 0006](../../adr/0006-native-pr-workflow-mvp.md)을 재사용한다.
기존 LOCAL-DEV-MCP가 호스트에서 실행하는 `git`과 인증된 `gh`를 이용하므로 ChatGPT의 별도 GitHub 플러그인에
private 저장소 권한을 부여할 필요가 없다. 기본적으로 `gh`의 호스트 인증을 재사용하며 MCP 결과에 토큰을 반환하지 않는다.

`connect`는 Tunnel이 실행하는 MCP 자식에게 호스트의 인증 설정 **위치**(`HOME`, `USERPROFILE`,
`APPDATA`, `LOCALAPPDATA`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `GH_CONFIG_DIR`)를
전달한다. 같은 셸에서 명시적으로 `export GH_TOKEN="$(gh auth token --hostname github.com)"`을
설정하고 `connect`를 실행하면 해당 `GH_TOKEN`을 Tunnel/MCP/gh 환경으로 전달한다.
보호된 workspace `.env`에 `GH_TOKEN`이 함께 존재하면 `.env` 값이 우선한다.
`GITHUB_TOKEN` 및 그 밖의 주변 환경 인증값은 자동 전달하지 않는다.
인증 파일을 복사하거나 토큰을 도구 결과에 출력하지 않는다.

`gh auth token --hostname github.com`은 기존 인증 토큰을 **조회**하는 명령이지 신규 발급 명령이 아니다.
호스트의 Keychain 인증을 Tunnel이 이용할 수 없다면, 같은 셸의 `GH_TOKEN`을 주입하거나
보호된 `.env`에 토큰을 설정할 수 있다. 이 토큰은 Tunnel 프로세스와 자식 환경에도 존재하므로
기본 Keychain 방식보다 노출
범위가 넓다. `GH_TOKEN`은 기존 로그인 자격증명보다 우선하며 정적 환경 토큰은 자동 갱신되지 않는다.
만료·철회 시 새 토큰 설정과 재시작이 필요하다. **`gh` 인증 성공은 HTTPS `git push` 인증 성공을 보장하지 않는다.**
SSH 키 또는 호스트 Git Credential Helper 설정도 별도 검증한다.

공식 문서: https://cli.github.com/manual/gh_auth_token 및 https://cli.github.com/manual/gh_help_environment
`gh` 종료 코드 4는 인증이 필요하다는 뜻이며, 경로 전달만으로 호스트 계정의 로그인 상태를 보장하지 않는다.
인증 컨텍스트를 전달하도록 구현을 변경한 경우에는 `connect`를 다시 시작해야 반영된다.

Tunnel 또는 하위 실행기가 `HOME`을 누락하면 OS 계정의 홈 디렉터리를 안전하게 복원한다.
상대경로 `GH_CONFIG_DIR`·`XDG_CONFIG_HOME`·`XDG_STATE_HOME`은 프로젝트 내부에
인증 상태가 생성되지 않도록 원격 `gh` 호출에서 제외한다.
`diagnose_project_remote_auth`는 저장소가 Clean하지 않아도 호출 가능하며,
실제 비밀값이나 설정 파일 경로를 반환하지 않고 GitHub CLI 로그인, private 저장소 API 접근,
Git 원격 전송 인증 상태(`git_transport`)를 구분한다.
원격 API 인증 성공은 Git HTTPS 인증 성공을 의미하지 않는다. Git 원격 명령
(`ls-remote`, `fetch`, `push`)에 한해 실행 시점의 `gh auth git-credential`을
Git Credential Helper로 지정한다. 변경은 해당 Git 프로세스에만 적용되므로
호스트·전역 `gitconfig`를 수정하거나 새 인증 파일을 생성하지 않는다.
다른 환경의 Credential Helper가 이 GitHub 인증에 앞서 사용되지 않도록
해당 호출에서는 GitHub.com에 대한 기존 Helper 목록을 초기화한다.
SSH origin에는 HTTPS Credential Helper를 적용하지 않는다.
인증 부재 시에는 별도 GitHub App 권한 확대가 아니라 호스트 `gh`
로그인 상태 또는 승인된 자격증명 공급 방법을 검토해야 한다.

## 노출하는 MCP 기능

| 도구 | 동작 | 독립 권한 |
| --- | --- | --- |
| `diagnose_project_remote_auth` | 설정 경로·GitHub CLI 인증 상태와 저장소 접근을 비밀값 없이 구분 | `git.remote.read` |
| `inspect_project_remote` | `origin`·실제 GitHub `main`·로컬 `main`·`origin/main`·기능 HEAD와 원격 기능 Ref 조회 | `git.remote.read` |
| `fetch_project_remote_main` | 관측한 SHA의 원격 `main`을 `origin/main`으로 Fetch | `git.remote.write` |
| `push_project_feature` | 현재 브랜치를 동일 이름의 원격 기능 Ref로 비강제 Push | `git.remote.write` |
| `create_project_draft_pr` | 같은 SHA의 원격 기능에서 `main` 대상으로 Draft PR 생성 | `gh.pr.create` |

모든 도구는 `git`과 `gh` 실행 권한도 필요하다. `config.toml`의 `system.allowed_commands`에 논리 권한이
명시돼 있어야 하며, 서버 시작 시 설정과 프로그램 준비 상태를 확인한다. **변경한 설정과 새 MCP 도구 등록은 서버 재시작 후 적용된다.**

현재 checkout을 전환하지 않고 과거 브랜치를 게시하려면 [격리형 Worktree 도구](worktrees.md)를 사용한다.

## 에이전트 작업 절차

1. `get_project_status`로 작업 트리 Clean 및 기능 브랜치 HEAD 확보
2. `inspect_project_remote`로 private GitHub에 대한 `gh` 인증, 원격 대상 URL, 실제 main SHA, 원격 기능 브랜치 SHA 관측
3. `tracking_is_fresh=false`이면 `fetch_project_remote_main` 호출. 이 단계에서 로컬 main은 업데이트하지 않는다
4. `feature_contains_remote=true`인지 확인. 거짓이면 **임의의 rebase/merge/reset을 수행하지 않고**
   차이와 충돌 가능성을 분석한다. 기능 브랜치 통합 정책이 확정되기 전에는 publish하지 않는다
5. 등록된 `format_check`, `lint`, `typecheck`, `unit`, `integration` 작업으로 현재 HEAD 변경을 검증
6. `push_project_feature`에 현재 브랜치·HEAD·원격 main·원격 feature의 직전 전체 SHA를 전달한다
7. `create_project_draft_pr`로 제목과 필요한 경우만 검토된 목적/예외를 입력한다. PR은 항상 Draft 상태로 생성한다
8. 생성된 PR URL, 실제 Head, GitHub Checks를 확인한다. Ready/병합은 별도 정책과 권한에 맡긴다

`main`을 직접 Fetch 대상으로 checkout하거나 `git push`로 업데이트하지 않는다.
Force Push, `--no-verify`, 태그 Push, 자동 PR 병합, Ruleset 구성은 이 도구의 기능이 아니다.

## 보장 및 한계

- `origin`은 `github.com/OWNER/REPO`에 대응하는 HTTPS/SSH URL만 허용하고 `gh repo view` 결과와 일치해야 한다.
- 원격 `main` SHA는 `gh api`에서 매 작업 확인한다. `origin/main`이 오래됐으면 Push와 PR 생성은 거부한다.
- Fetch는 원격 추적 Ref만 갱신한다. Non-fast-forward remote update는 자동 강제하지 않는다.
- Push는 `feat/`, `fix/`, `refactor/`, `docs/`, `test/`, `build/`, `ci/`, `chore/` 접두사의 현재 브랜치만 허용한다.
- 로컬 `main`의 미공개 커밋을 기능 브랜치가 포함한다면 원격 `main`을 조상으로 가지더라도 의도치 않은 변경 유입을 막기 위해 게시를 거부한다.
- 동시 변경은 사전 확인과 비강제 Push로 거부한다. 네트워크 결과가 불확실하면 재시도 전에 Ref를 다시 조회한다.
- Draft PR은 새로 추가된 `.changes` 평가와 원본 커밋에서 생성한다. Purpose가 중복 최상위 이유로 모호할 때는 입력을 요구한다.
- PR 중복 방지를 위해 동일 Head 브랜치의 기존 PR을 조회한다. 폐쇄된 PR의 재사용·재개방은 자동화하지 않는다.
- 인증된 호스트의 Git/gh 프로세스는 실제 파일·네트워크 권한을 가진다. MCP 정책은 OS-level sandbox가 아니다.
- GitHub 앱별 요금제·리포지터리 보호 기능 활성화 여부까지 이 단계에서 보장하지는 않는다.
- 현재는 원격 main이 기능 브랜치보다 앞선 경우 자동 역사 재작성/병합을 제공하지 않는다.
  이 경계는 별도 설계·테스트 후에 기능 브랜치 전용 동기화 도구로 확장할 수 있다.

## 수동 검증과 테스트

단위 테스트는 인자·권한·SHA 충돌·중복 PR을 모의 원격에서 검증한다.
실제 private 원격에서의 인증, Fetch, Push, Draft PR 생성과 GitHub Actions 수용은 서버 재시작 후
이 MCP 도구로 수행해야 확인할 수 있다. 모의 성공을 GitHub PR 성공으로 표시하지 않는다.
