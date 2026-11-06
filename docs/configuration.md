# 설정과 운영 계약

사용자는 동작 의도·identity·외부 자원 선택·권한을 정한다. 프로그램 탐색·Git argv·wrapper·비대화형 옵션은 구현이 맡는다.
이 문서는 설정과 사용자에게 보이는 시작·종료·진단·프로세스 보장을 소유한다. 연결 원리는 [실행 흐름](design.md)을 참조한다.
개발 시 설정의 None·오류 소유권 기준은 [개발 규약](conventions.md#오류와-none)을 따른다.
실제 값을 얻는 CLI와 선택 기준은 [`.env.example`](../.env.example)에 있다. 실제 키·이름·이메일을 Chat에 붙여 넣지 않는다.

## 파일별 책임

| 파일 | 책임 |
| --- | --- |
| `.env` | Tunnel·Git 설정과 등록 작업에 전달할 프로젝트 공통 환경 |
| `config.toml` | workspace 접근·논리 명령 권한·CLI 작업 recipe·공개 환경 이름 분류 |
| `pyproject.toml` | Python·의존성·설치 진입점·검사 기준 |
| `uv.lock` | 재현 가능한 패키지 버전 |

정책 파일 이름은 `config.toml`로 고정한다. 선택한 workspace의 `config.toml`과 `.env`만 읽으며
호출 디렉터리·상위 디렉터리 검색이나 ambient 값 fallback은 없다. `.env`의 변수 보간도 하지 않는다.

## Workspace 선택

```sh
local-dev-mcp --workspace /path/to/workspace run
local-dev-mcp --workspace ../target-project connect
```

`--workspace PATH`는 `run`·`connect` 앞에 두는 공통 옵션이다. 생략하면 호출 시 현재 디렉터리를 사용한다.
상대 경로는 호출 디렉터리를 기준으로 한 번 해석하고 실제 절대 경로로 고정한다. 호출 프로세스의 현재 디렉터리는 바꾸지 않는다.
workspace는 설치된 패키지나 이 저장소의 위치와 별개이며, 파일·Git 작업과 설정 로딩의 기준이 된다.

대상을 선택해도 접근 권한은 추가되지 않는다. 선택한 workspace의 정책이 그 경로와 필요한 명령을 허용해야 한다.
빈 문자열·NUL이 포함된 경로는 CLI 사용 오류로 거부한다. 대상에 필요한 파일이 없거나 설정이 잘못되었거나
접근이 거부되면 시작에 실패하며, 호출 디렉터리나 다른 설정으로 대체하지 않는다.

connect는 사전 검사와 Tunnel이 실행할 자식 run에 같은 절대 workspace 경로를 명시적으로 전달한다.
사전 검사·Tunnel client의 현재 디렉터리도 선택한 workspace로 설정하되, 대상 선택을 자식의 현재 디렉터리에 맡기지 않는다.
각 프로세스가 설정을 읽는 시점은 [시작 snapshot](#시작-snapshot)을 따른다.

## 정책 파일 편집

```sh
local-dev-mcp --workspace /path/to/workspace --allow-policy-edits connect
local-dev-mcp --workspace /path/to/workspace --allow-policy-edits run
```

`--allow-policy-edits`는 `run`·`connect` 앞에 두며 생략하면 false다. 이 시작 플래그는 루트 `config.toml`과
workspace 내 모든 `.gitignore`의 기본 직접 쓰기 금지만 해제한다. `allowed_paths`·`denied_paths`·`read_only_paths`,
최종 ignore 결과·기본 보호 이름·workspace 경계·symlink 검사·필수 `read_ref`와 버전 일치는 유지한다.
따라서 읽기 전용으로 지정되거나 ignore된 정책 파일은 플래그를 사용해도 수정하지 못한다.

MCP 호출·환경 변수·설정 파일로 실행 중인 모드를 바꾸지 않는다. connect는 사전 검사와 자식 run에도 같은 플래그를 전달한다.
`get_tool_readiness`와 `list_project_tasks`의 `policy_edits_allowed`로 현재 시작 모드를 확인한다.
`config.toml`을 편집해도 기존 권한·recipe·환경 snapshot은 재시작까지 유지한다.
`.gitignore`는 다음 작업에서 다시 읽으므로 편집 직후 탐색·쓰기 허용 결과가 달라질 수 있다.

## Workspace와 호스트 서명 권한

workspace 접근 권한은 개발 프로젝트의 파일·Git 작업 범위를 사용하는 권한이다.
호스트 서명 권한은 선택한 키 저장소·키로 commit을 서명·검증하는 권한이며 두 범위는 구분한다.
파일 경로의 허용·읽기 전용·접근 금지는 [파일 접근 정책](#파일-접근-정책)으로 정하고, 각 Git 작업 대상은 해당 workspace로 제한한다.
`system.allowed_commands`의 `gpg`는 선택한 호스트 GnuPG·키 저장소·키를 서명과 검증에 사용하는 권한이다.
키 저장소를 workspace용 allowed_paths에 추가할 필요는 없으며 일반 파일 도구의 작업 대상으로 확대하지 않는다.
시작 설정과 직접 Git 실행 모두 gpg 허용을 확인한다. 홈의 유효성 검사는 유지하고 workspace 포함 검사는 적용하지 않는다.

기본은 기존 호스트 설치·키·공유 daemon 재사용이다. 별도 키 복사·생성·전용 환경은 준비하지 않는다.
실행 파일·키 저장소는 시작 시 선택·고정하며 실패 시 다른 키·홈·unsigned 방식으로 바꾸지 않는다.
호스트 업데이트·키 잠금·서비스 변화의 영향을 받아들이고 공유 daemon을 강제 종료하지 않는다.
현재 정책은 개발 workspace를 OS 수준에서 격리하지 않는다.

파일 도구는 경로별 workspace 권한을 사용한다. Git·identity가 준비되지 않아도 동작한다.
파일 보호·symlink·ignore·원문 참조의 제한은 [Workspace 계약](tools/workspace/README.md)이 소유한다.

## 서명 기능과 내부 의존성

서명 기능은 선택한 키로 commit의 OpenPGP 서명을 생성하고, 관측한 commit의 서명을 검증하는 기능이다.
개별 키 저장소 daemon 운영은 사용자에게 제공하는 기능 단위가 아니다.
사용자는 서명 의도·identity·키를 선택한다. 프로그램·홈 위치는 시작 시 탐색하고 필요한 경우에만 override한다.
keyboxd·gpg-agent는 선택한 GnuPG 설치와 키 저장소의 내부 의존성이다.
별도 .env 변수·allowed_commands 항목·공개 MCP 도구는 만들지 않는다.
GnuPG가 필요한 daemon을 자동 시작할 수 있으며, MCP는 공유 daemon을 감시·강제 종료하지 않는다.
MCP 종료 후에도 daemon이 남을 수 있다. 직접 Git/GPG 프로세스의 정리와는 구분한다.
입력 창·passphrase 전달·자동 키 다운로드·시스템 설치·키 생성/가져오기는 제공하지 않는다.

`GIT_GPG_HOME` 생략·빈 문자열은 기본 홈 탐색이고, 명시적 홈은 우선한다.
홈 탐색에는 선택한 gpg와 인접한 gpgconf, 필요하면 PATH 후보를 검사한다.
`--list-components`가 보고한 gpg의 실제 경로가 선택한 gpg와 일치하는 후보만 사용하고,
그 후보의 `--list-dirs homedir` 결과를 사용한다. ambient GNUPGHOME은 상속하지 않으며,
별도 홈을 사용하려면 GIT_GPG_HOME을 지정한다. 일치·조회 실패 시 다른 키 저장소를 채택하지 않는다.
명시적 홈이 있으면 gpgconf를 요구하지 않는다. 탐색 결과도 실제 경로로 해석해 존재하는 디렉터리인지 검사한다.
탐색 과정에서 키 저장소를 만들거나 권한을 확대하지 않는다.

기본 홈 탐색용 두 고정 조회는 논리적 `gpg` 허용에 포함한다.
이는 직접 바이너리별 허용 원칙의 명시적 예외이며 임의 gpgconf 명령·설정 변경·키 관리 권한을 뜻하지 않는다.
시작 로더는 동기식이므로 이 짧은 조회는 subprocess.run의 10초 timeout·종료 대기를 사용한다.
Git/GPG 비동기 작업은 기존 shellous를 사용하고 새로운 프로세스 관리 계층을 만들지 않는다.

## 시작 snapshot

각 프로세스는 자기 시작 시 필요한 설정을 읽어 고정한다.

| 프로세스 | 시작 시 검사·고정 |
| --- | --- |
| `run` | 프로젝트 경로·정책 편집 모드, Git 준비, identity·서명, recipe별 권한·실행 파일, 공통 작업 환경·출력 보호 catalog |
| `run`의 서명 true | gpg 허용, 키 fingerprint, 명시 또는 탐색한 홈의 유효성, GPG·wrapper 실행 경로 |
| `connect` | tunnel-client 허용, Tunnel 두 값, client 실행 경로·환경. 시작 전 별도 프로세스로 run의 정적 설정도 검사 |

`connect`는 기본 프로세스 환경 및 Tunnel 전용 자격증명 외에 호스트의 GitHub CLI 인증
**설정 위치**(`HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`,
`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `GH_CONFIG_DIR`)를 자식에 전달한다.
같은 셸에서 명시적으로 주입한 `GH_TOKEN`도 Tunnel/MCP/gh 프로세스에 전달된다.
보호된 workspace `.env`에 `GH_TOKEN`이 명시되면 `.env` 값이 우선한다.
`GITHUB_TOKEN` 및 그 밖의 주변 인증 토큰은 전파하지 않는다. `GH_TOKEN`은 저장된 `gh` 로그인보다 우선하며
정적 환경 토큰은 자동 갱신되지 않는다. 이 방식은 GitHub CLI API 인증을 제공하지만,
HTTPS/SSH `git push`의 자격증명 설정까지 보증하지 않는다.
기존 `gh` 로그인 저장소를 읽을 권한은 같은 OS 계정의 호스트 정책에 따른다.
인증 방법이 어느 쪽이든 GitHub 원격에 실제 접근할 수 있는지는 별도로 검증한다.

공통 정책 파일의 문법·스키마 오류, workspace 접근 거부, 필수 .env를 읽을 수 없는 경우는 시작 실패다.
Git 미설치·미허용은 Git 도구만, identity·서명 설정의 알려진 누락·무효·준비 오류는 commit만 사용 불가로 만든다.
예상하지 못한 내부 예외는 준비 부족으로 숨기지 않고 시작 실패로 처리한다.
identity 없이 Git 조회·stage/unstage를 사용할 수 있으며 Git 없이도 서버와 준비 상태 조회는 시작할 수 있다.
Git 저장소 여부는 실제 Git 작업에서 검사한다.
등록 작업의 명령 권한·실행 파일 부족은 그 recipe에 한정하고 `list_project_tasks`로 안내한다.
job이 하나 준비되지 않았다는 이유로 다른 job이나 파일·Git 도구를 비활성화하지 않는다.
서명 false이면 GPG 값이 잘못되어 있어도 선택·검증하지 않으며 gpg 권한·프로그램·agent도 요구하지 않는다.
`.env`·`config.toml`·PATH 수정은 재시작 후 반영한다. 실행 중 설정 파일을 다시 읽거나 PATH를 재탐색하지 않는다.

connect와 자식 run은 각자 시작 시 snapshot을 만든다. 두 프로세스가 파일 변경에 대해 원자적으로 같은 snapshot을 공유하지는 않는다.
자식이 재시작되면 그 새 프로세스는 새 설정을 읽는다. connect가 살아 있다는 사실만으로 자식 서버의 시작 성공을 판정하지 않는다.

HEAD/index·파일·symlink·실행 파일 삭제·agent 종료·키 잠금/만료 같은 외부 상태는 계속 변한다.
작업 직전 재확인하며 실패해도 다른 키·실행 파일·unsigned 방식으로 자동 대체하지 않는다.
시작 시 설정 검증은 이후 모든 실행의 성공이나 격리를 보장하지 않는다.

## 연결 사전 검사와 상태 해석

connect는 Tunnel client를 시작하기 전에 별도 Python 프로세스에서 run의 정적 시작 설정을 검사한다.
검사는 10초 제한이며 전체 시작 실패이면 client를 시작하지 않고 안전한 원인·조치를 보고한다.
개별 기능의 준비 부족은 원인별로 중복 없이 WARNING으로 전달하고 연결을 진행한다.
실제 자식 run도 자신의 snapshot을 만들고 같은 방식으로 진단한다. 이 경고 자체는 실패 종료 코드를 만들지 않는다.
도구 등록·MCP initialize·원격 인증·동적 키 사용 가능성까지 검사하지는 않는다.
검사 프로세스와 실제 자식 run도 각자 설정을 읽으므로 검사 성공이 이후 자식 시작 성공을 보장하지 않는다.

client의 health listen은 `127.0.0.1:0`을 사용하지만 CLI는 readiness 상태나 실제 주소를 조회·출력하지 않는다.
client 시작 메시지·종료 코드 0은 해당 프로세스의 상태다. 서버는 호출자가 ChatGPT인지 판정하거나
클라이언트의 결과 활용을 관측하지 않는다. 실제 ChatGPT 사용 확인은 [외부 클라이언트 검증](testing.md#서버-기능과-외부-클라이언트-검증)에 속한다.

## 도구 준비 상태

`get_tool_readiness()`는 인자 없는 읽기 전용 도구다. Git·identity·GPG 없이 공통 workspace 권한만으로 조회한다.
`project`, 설정을 고정한 UTC `snapshot_at`, `scope="startup_snapshot_only"`, `policy_edits_allowed`, `tools`를 반환한다.
각 도구 항목은 `name`, `ready`, `issue`이며 조회 도구 자신도 포함한다.
준비된 항목의 issue는 null이다. 준비 부족은 안전한 `operation`, `target`, `reason`, `action`,
`restart_required=true`를 담으며 실제 identity·키·외부 원문은 포함하지 않는다.

ready는 시작 설정·권한·선택한 실행 파일의 정적 준비만 뜻한다. 조회는 파일 재로딩·외부 명령 실행·키 잠금 확인을 하지 않는다.
서명 설정이 준비됐어도 실제 키 사용이나 Git 작업은 실패할 수 있다. 설정 수정은 재시작 후 반영하며 자동 복구하지 않는다.

준비되지 않은 도구도 공개 목록에 유지한다. 호출은 권한 확인 뒤 정적 준비 상태를 먼저 확인하고,
사용 불가이면 인자 검증·동적 준비·본문 전에 MCP 오류를 반환한다. 준비 부족과 잘못된 인자가 함께 있으면 준비 오류가 우선한다.
내부 Python 실행도 필요한 정책·준비값을 확인하며 권한 middleware만으로 보호된다고 가정하지 않는다.
CLI 작업 도구의 준비와 개별 recipe의 준비는 구분한다. 실행 대상의 준비 상태는 `list_project_tasks`에서 확인한다.

## `.env` 계약

값 획득 CLI·발급 위치·조회 결과의 선택 기준은 [`.env.example`](../.env.example)을 따른다.

| 값 | 필수 조건 | 의미·기본값 |
| --- | --- | --- |
| `CONTROL_PLANE_TUNNEL_ID` | connect | 연결할 Tunnel 식별자. 기본값 없음 |
| `CONTROL_PLANE_API_KEY` | connect | Tunnels Read+Use 권한의 runtime key. 기본값 없음 |
| `GIT_USER_NAME` | commit | author·committer에 사용할 이름. 기본값·자동 상속 없음 |
| `GIT_USER_EMAIL` | commit | author·committer에 사용할 이메일. 기본값·자동 상속 없음 |
| `GIT_COMMIT_SIGN` | 선택 | true/false만 허용하며 생략 시 false. 빈 값은 오류 |
| `GIT_SIGNING_KEY` | 서명 true | full fingerprint와 선택적 !. 기본값 없음 |
| `GIT_GPG_HOME` | 서명 true에서 선택 override | 생략·빈 값은 기본 홈 탐색. 명시한 절대 경로 우선 |
| `GIT_GPG_PROGRAM` | 서명 true에서 선택 override | 생략·빈 값은 PATH 탐색. 명시한 절대 경로 우선 |

Tunnel ID는 `tunnel_` 뒤 소문자 영숫자 32자다. runtime key의 실제 인증 유효성을 로컬에서 판정하지 않는다.
admin key는 일반 runtime 설정에 필요하지 않다.

identity는 author·committer에 같은 한 쌍을 사용한다. 누락·빈 값·앞뒤 공백·NUL·CR·LF·꺾쇠를 거부한다.
이름 내부 공백과 Unicode는 허용하며 RFC 이메일 형식·도메인 검사는 추가하지 않는다.
값을 strip해 바꾸거나 Git local/global/ambient identity로 fallback하지 않는다. Git identity 사전 검사는 호출 때 유지한다.

GPG 프로그램·홈의 생략 또는 빈 문자열은 탐색을 뜻하지만, 값 없이 변수 이름만 선언하면 무효다.
None·빈 문자열·false는 서로 다른 입력이다.

서명 true는 OpenPGP 서명 필수다. false는 저장소의 `commit.gpgSign=true`보다 우선해 `--no-gpg-sign`을 요청한다.
서명 true에서 실패했을 때 unsigned로 재시도하는 것은 어느 경우에도 허용하지 않는다.

fingerprint는 40/64자리 hex와 선택적인 `!`만 받는다. `!`는 정확한 primary/subkey 선택이고 자동으로 붙이지 않는다.
개인키 본문·passphrase는 `.env`에 넣지 않는다. MCP는 키를 생성·가져오기·저장하거나 `.git/config`를 수정하지 않는다.

## `config.toml`

기본 unsigned 구성:

```toml
[filesystem]
allowed_paths = ["."]
read_only_paths = ["vendor", "docs/reference"]
denied_paths = ["credentials.json"]

[system]
allowed_commands = ["git", "tunnel-client"]
```

서명 true인 경우 `allowed_commands`에 `gpg`를 추가한다. Chat에서 `prepare_project_release`로 버전과 릴리스 파일을 갱신하려면 별도로 `uv.release`를 명시적으로 허용하고 `uv`를 설치해야 한다. `get_release_plan`의 조회에는 `git`만 필요하다. 이 권한은 등록된 CLI 작업의 `uv.run.*`과 별개이며, 서버 재시작 후 적용된다. 버전 준비는 임의 명령 실행이나 Git push 권한을 부여하지 않는다. 키 저장소 경로를 `allowed_paths`에 추가하지 않는다.
Git workspace는 시작 시 선택한 경로로 고정되며 호스트 서명 권한이 작업 범위를 확대하지 않는다.
원격 PR 메시지 계획 조회는 `git`, `gh`, `git.remote.read`가 필요하다. 명시적 병합은 별도
`gh.pr.merge`가 있어야 하며 `git.remote.write` 또는 `gh.pr.create`로 대체되지 않는다.
권한은 자동 활성화하지 않는다. 입력·결과 및 재시작 이후 절차는 [원격 PR 계약](tools/git/remote-pr.md#명시적-pr-병합)을 따른다.
workspace 허용 경로는 선택한 workspace의 설정 위치 기준으로 resolve한다.
GnuPG 홈도 resolve하되 존재·디렉터리 유효성만 검사한다.
논리 명령 `gpg` 허용은 선택한 실행 파일의 진위 검증을 뜻하지 않는다. PATH와 override는 사용자가 신뢰하고 관리한다.

경로 정책과 `allowed_commands`는 문자열 목록이다. 빈 문자열·공백만 있는 원소는 거부하고 원소의 공백을 자동 제거하지 않는다.
`allowed_paths`와 `allowed_commands`는 필수이며, `read_only_paths`·`denied_paths`는 생략 시 빈 목록이다.
잘못된 명시값을 빈 목록이나 기본값으로 대체하지 않는다.
빈 목록은 파싱 가능하다. 사용할 workspace 범위가 없으면 시작을 막고, git/gpg 미허용은 해당 기능의 준비 부족으로 기록한다.
connect에 필요한 tunnel-client 미허용은 connect 실패다.
기존 `filesystem`·`system`의 알 수 없는 필드는 거부하지 않지만 제한을 제공하지도 않는다.
`jobs.<이름>`과 `jobs_environment`는 알 수 없는 필드를 거부한다.

## CLI 작업 설정

설치된 CLI를 고정 argv로 등록한다. 다음은 이 Python 프로젝트에 적용할 수 있는 예시이며, 다른 workspace는
자신의 runner와 옵션을 지정한다. 설정 파일을 자동 생성·수정하거나 필요한 CLI를 자동 설치하지 않는다.

```toml
[system]
allowed_commands = [
  "git", "tunnel-client",
  "uv.run.ruff.lint", "uv.run.ruff.format",
  "uv.run.pytest.unit", "uv.run.pytest.integration",
]

[jobs.lint]
kind = "lint"
permission = "uv.run.ruff.lint"
argv = ["uv", "run", "--locked", "ruff", "check", "--quiet", "--output-format", "concise", "src", "tests", "conftest.py"]

[jobs.format]
kind = "format"
permission = "uv.run.ruff.format"
argv = ["uv", "run", "--locked", "ruff", "format", "--quiet", "src", "tests", "conftest.py"]

[jobs.unit]
kind = "test"
level = "unit"
permission = "uv.run.pytest.unit"
argv = ["uv", "run", "--locked", "pytest", "--verbosity=-1", "--tb=short", "--no-header", "-rfE", "-n", "0", "-m", "unit"]
default_targets = ["tests"]

[jobs.integration]
kind = "test"
level = "integration"
permission = "uv.run.pytest.integration"
argv = ["uv", "run", "--locked", "pytest", "--verbosity=-1", "--tb=short", "--no-header", "-rfE", "-n", "4", "-m", "integration and not packaging"]
default_targets = ["tests"]

[jobs_environment]
public_names = ["APP_MODE"]
```

이 예시의 `[system]`은 기존 table에 합친다. 같은 table을 중복 선언하지 않는다.
recipe의 `permission`과 정확히 같은 값을 `allowed_commands`에서 허용해야 한다. `uv`만 허용했다고
`uv.run.pytest.unit`을 허용하지 않으며, launcher의 간접 실행 전체를 추가 검사하는 기능도 아니다.

| 항목 | 의미와 기본값 |
| --- | --- |
| `jobs` | 생략·빈 table은 등록 작업 없음. 최대 64개 |
| `jobs.<이름>` | lowercase snake_case 이름, 최대 64자 |
| `kind` | 필수 `lint`, `format`, `test` 중 하나 |
| `permission` | 필수 dotted 논리 권한, 최대 128자. 실제 executable 이름과 별개 |
| `argv` | 필수 1..128개 문자열. 첫 원소는 executable, 이후는 고정 옵션·인자 |
| `level` | test에서만 필수 `unit`, `integration`, `e2e` |
| `default_targets` | test에서만 필수인 1..128개 전체 실행 대상. 호출의 `all_tests=true`일 때 사용 |
| `timeout_seconds` | 생략 시 300, 정수 1..3600. 종료 정리 시간까지 포함한 정확한 반환 시각은 아님 |
| `jobs_environment.public_names` | 생략·`[]`는 추가 공개 분류 없음. 중복 없는 환경 이름 최대 128개 |

argv·기본 대상은 항목당 최대 4096자·목록별 UTF-8 합계 32 KiB이며, 빈 문자열·공백뿐인 값·NUL을 거부한다.
test의 기본 대상은 base argv에 넣지 않는다. 명시한 `targets`가 기본 대상을 대체해야 선택 실행이 전체 실행으로 넓어지지 않는다.
`level`은 분류이며 worker 옵션을 자동 삽입하지 않는다. 대상 프로젝트의 E2E는 해당 runner의 병렬 옵션을 설정한다.

실행 cwd는 선택 workspace다. executable에 경로 구분자가 있으면 workspace 기준 절대 호출 경로로 고정하고,
이름만 있으면 고정한 작업 환경의 PATH에서 찾는다. Python 가상환경 선택을 보존하기 위해 실행 파일 symlink의
호출 경로를 유지한다. 환경 활성화·라이브러리별 옵션 변환·shell 확장은 하지 않는다.
`npm run` 등에 필요한 `--`도 고정 argv에 명시한다. 실행·조회·복구 계약은 [CLI 작업](tools/tasks/README.md)을 따른다.

위 pytest 예시는 프로젝트 기본 `-v`를 `--verbosity=-1`로 덮어 성공 항목별 출력을 줄이고, 실패·경고·집계를 유지한다.
`--show-capture=no`·`--no-summary`로 원인을 생산 단계에서 없애는 방식은 기본으로 사용하지 않는다.
Ruff는 `--quiet`와 concise 진단을 사용한다. 진단 생략과 상세 조회는 CLI 옵션과 별개인 [응답 계약](tools/tasks/README.md#결과와-다음-행동)이다.

## 파일 접근 정책

| 항목 | 의미 | 생략·빈 목록 |
| --- | --- | --- |
| `filesystem.allowed_paths` | 파일 접근을 허용하는 범위 | 생략은 설정 오류, `[]`는 허용 없음 |
| `filesystem.read_only_paths` | 허용 범위 안에서 직접 파일 수정을 금지 | 둘 다 읽기 전용 예외 없음 |
| `filesystem.denied_paths` | 조회·수정을 모두 금지 | 둘 다 추가 접근 금지 없음 |

각 원소는 literal 파일·디렉터리 경로이며 디렉터리 지정은 하위 경로에도 적용한다. glob 패턴은 사용하지 않는다.
상대 경로는 선택한 workspace를 기준으로 해석한다. 허용 경로를 넓게 지정해도 도구가 workspace 밖을 대상으로 삼을 수 없다.
기본 보호 이름과 `denied_paths`가 허용보다 우선하며, `read_only_paths`가 쓰기 허용보다 우선한다.
더 구체적인 허용 경로로 읽기 전용 제한을 해제하지 않는다. 읽기 전용 목록만으로 허용 범위 밖의 읽기가 가능해지지도 않는다.
읽기 전용·접근 금지 범위는 대소문자만 다른 경로도 보수적으로 제한한다.

루트 전체를 허용하지 않고 `allowed_paths = ["src", "docs"]`처럼 하위 경로만 허용해도 서버를 시작할 수 있다.
목록·탐색은 허용된 경로로 이동하는 데 필요한 조상 디렉터리를 보여주되, 다른 파일의 내용 접근을 허용하지 않는다.
설정 파일은 시작 snapshot이고, `.gitignore` 규칙은 각 작업에서 읽는다.

파일 생성·구간 수정·삭제는 모두 직접 쓰기 정책을 적용한다. 없는 부모 폴더도 명시적인 생성 요청과 각 경로의 쓰기 허용이 필요하다.
생성·삭제의 요청 ID, 충돌·영구 삭제·보관 수명은 [Workspace 변경 계약](tools/workspace/README.md#파일-생성삭제와-요청-재전송)을 따른다.

`.gitignore`의 최종 제외 결과는 직접 파일 수정의 읽기 전용 제한으로도 적용한다.
ignored라는 이유만으로 비밀 파일의 읽기가 금지되지는 않는다. 비밀 파일은 기본 보호 또는 `denied_paths`로 구분한다.
루트 `config.toml`과 모든 `.gitignore`의 기본 직접 쓰기 금지는 [정책 파일 편집](#정책-파일-편집) 플래그로 해제할 수 있다.
보호 이름·symlink·ignore 해석은 [Workspace 계약](tools/workspace/README.md#접근과-탐색-범위)을 따른다.

읽기 전용은 직접 파일 수정의 제한이다. Git 작업의 index·commit과 hooks·필터의 부수 동작은
[Git 계약](tools/git/README.md)을, 등록된 포맷팅·테스트 등의 부수 동작은 [CLI 작업 계약](tools/tasks/README.md)을 따른다.
이들 외부 프로그램의 모든 OS 파일 접근을 직접 파일 도구의 경로 정책으로 가두지는 않는다.

`.env` 원문은 agent에게 제공하지 않지만 내부 기능은 필요한 값을 사용할 수 있다.
Credential과 미분류 값은 비밀로 취급하고, identity·서명 설정·공개키 fingerprint 등 기능상 공개가 필요한 metadata는
정해진 결과로 구분한다. 경로 보호는 다른 소스에 복제된 비밀의 자동 탐지·마스킹을 뜻하지 않는다.

## 프로세스 환경

`settings.py:read_dotenv()`가 정확한 `.env`를 읽고, `app/runtime.py:ServerSettings`가 서버별 불변 설정을 만든다.
`execution.py`는 서버 시작과 자식 실행이 공유하는 최소 환경을 선택한다.
바이너리 탐색은 `tools/_execution.py`, Git 전용 환경 구성은 `tools/git/_environment.py`,
Tunnel 환경 구성은 `tools/tunnel_client/_client.py`가 담당한다.

| 대상 | 전달 환경 |
| --- | --- |
| 최소 환경 | 존재하는 PATH·SYSTEMROOT·WINDIR·TMPDIR·TMP·TEMP |
| Tunnel | 최소 환경과 선택한 Tunnel 두 값 |
| Git | 시작 시 고정한 최소 환경과 Git global/system config 차단·prompt/optional lock 설정 |
| commit | Git 환경과 준비한 author/committer 네 변수 |
| 서명 commit | commit 환경과 GNUPGHOME·내부 LOCAL_DEV_MCP_GPG_PROGRAM |
| CLI 작업 | 최소 환경과 `.env`의 서버 전용 설정을 제외한 값 전체. 모든 recipe가 같은 snapshot 사용 |

`run`은 FastMCP import 전에 전체 환경을 최소화하고 내부 `FASTMCP_ENV_FILE=os.devnull`로 라이브러리의 자동 dotenv 로딩을 끈다.
그 뒤 애플리케이션이 필요한 설정을 명시적으로 읽는다. 이 내부 제어값과 Tunnel credential은 Git에 전달하지 않는다.
Tunnel 자식에는 인증 환경이 일시 상속될 수 있으나 run이 서버 구성 전에 제거한다.

GnuPG 홈의 기존 설정은 읽는다. Git global/system config를 다시 활성화하는 것은 아니다.
identity·서명 형식·키·wrapper는 준비한 설정이 우선하며 hooks·필터는 저장소 local 설정을 유지한다.
준비값은 해당 자식과 hooks 등에서 보일 수 있다. 환경 선택은 호스트 credential 저장소 전체의 격리가 아니다.

작업 환경은 변수 이름을 job마다 열거하지 않는다. `.env`의 값 없는 선언은 전달하지 않고 빈 문자열은 유지한다.
Tunnel 두 값과 기존 Git 여섯 설정은 서버 전용으로 분리해 작업 환경에 자동 전달하지 않는다.
그 밖의 프로젝트 값은 공통 전달하며, 호스트 환경 전체를 상속하거나 서버의 virtualenv를 대상 환경으로 추정하지 않는다.
HOME·locale 등이 필요하면 프로젝트 공통 `.env`에서 명시한다. 대상의 `uv run`·`npm run` 등이 해당 프로젝트 환경을 준비한다.

출력 보호용 catalog는 작업 환경 전달 여부와 별개로 같은 `.env` snapshot의 비공개 값을 사용한다.
형식이 유효한 Git identity·서명 플래그·공개 fingerprint는 공개 metadata이며, 프로젝트의 비밀이 아닌 운영 값은
`jobs_environment.public_names`에서 한 번 분류한다. 이 예외로 서버 credential을 공개 분류할 수는 없다.
구체적인 보호 범위·출력 보류 조건은 [작업 진단](tools/tasks/README.md#진단과-출력-보호)을 따른다.

## GPG 준비와 피드백

서명 true에서만 시작 시 gpg 권한·홈 유효성·키 선택 형식·설치된 wrapper를 확인한다.
wrapper는 서버와 같은 Python 환경의 `local-dev-mcp-gpg`만 사용하며 PATH로 대체하지 않는다. `uv sync --locked`로 등록한다.
호출 직전에는 secret key와 GnuPG 서비스 접근을 비대화형으로 조회한다. 성공해도 잠금 해제나 이후 서명 성공을 보장하지 않는다.

고정 옵션은 `--batch --no-tty --pinentry-mode error`,
`--no-auto-key-retrieve --auto-key-locate clear --no-auto-key-import`다.
wrapper는 shell 없이 `os.execv`로 GPG로 교체한다. 새 supervisor나 agent 관리 기능이 아니다.
필요한 keyboxd·gpg-agent의 자동 시작은 GnuPG에 맡긴다. 미리 수동 실행할 필요는 없으며
GnuPG 설치·홈 설정·실행 권한 문제로 시작할 수 없으면 동적 도구 오류를 반환한다.
MCP는 daemon을 직접 관리하거나 종료하지 않는다. 자동 시작은 키 잠금 해제나 입력 창 허용을 뜻하지 않는다.
`use-keyboxd` 홈은 키 저장소 접근에 keyboxd가 필요하지만 개별 daemon 설정을 MCP에 추가하지 않는다.
[GnuPG의 키 저장소 설명](https://gnupg.org/documentation/manuals/gnupg/GPG-Configuration.html)과
[gpgconf 문서](https://gnupg.org/documentation/manuals/gnupg26/gpgconf.1.html)를 따른다.

정책 거부는 수정할 config.toml 항목을, 설정 누락은 해당 변수와 조회 CLI를, 실행 파일 부재는 설치·PATH 확인을 안내한다.
동적 실패는 GPG·OS가 확인한 범위만 설명한다. 키/GnuPG 서비스 접근 실패를 곧바로 ‘passphrase 미승인’으로 단정하지 않는다.
GPG 원문·개인 값은 응답·기본 진단에 그대로 싣지 않으며 확인 명령의 `<HOME>`은 사용자가 고정 설정으로 바꾼다.

## 종료 코드

| 코드 | 의미 | 적용 사례 |
| --- | --- | --- |
| 0 | 성공 | stdio EOF, client 정상 종료, help |
| 1 | 일반 실행 실패 | 내부 오류, 원인 미분류 client 실패, 직접 프로세스 종료 확인 실패 |
| 2 | CLI 사용 오류 | argparse가 거부한 인자·명령 |
| 3 | 설정·정책 오류 | 필수 파일/값 부재, 형식 오류, 직접 작업의 정책 거부 |
| 4 | 로컬 I/O 오류 | 파일 읽기 권한·장치·프로세스 I/O 실패 |
| 5 | 실행 의존성 오류 | 필요한 실행 파일 부재·실행 불가 |
| 130 | 중단 | 일반 CLI KeyboardInterrupt 또는 client 직접 종료가 확인된 취소 |

client의 비정상 종료는 1로 변환하고 원래 코드·신호는 별도 진단에 보존한다.
종료 코드는 실패 분류이며 자동 재시도 가능 여부를 표현하지 않는다.
실행 중 도구 실패는 MCP 오류 응답이며 서버 종료 코드로 바꾸지 않는다.

## 진단 출력

CLI 진단은 UTC timestamp·INFO/WARNING/ERROR와 작업·안전한 대상·원인·수정 조치를 stderr에 표시한다.
시작·종료 상태와 중단은 INFO, 개별 기능의 비치명 준비 부족은 WARNING, 전체 실패는 ERROR다. argparse의 help·사용 오류는 표준 형식을 유지한다.
run의 stdout은 MCP 프로토콜 전용이다.
임의의 외부 오류 원문·credential·기본 traceback은 표시하지 않으며 기본 로그 파일·debug 모드는 제공하지 않는다.
client의 stdin·stdout·stderr는 DEVNULL이다. 사전 검사도 안전한 실패·경고 필드만 전달하며 제3자 stderr와 비정상 응답 원문은 버린다.
기본 콘솔의 상태·오류 메시지는 애플리케이션이 작성한 진단이며 외부 client 로그를 전달한 것이 아니다.

## 프로세스 정리 보장

Ctrl+C 뒤 Tunnel client의 직접 종료가 확인되면 130, 정리를 확인할 수 없으면 1로 종료한다.
이 보장은 직접 관리하는 프로세스까지이며 client가 만든 MCP를 포함한 전체 자식 트리 회수를 뜻하지 않는다.
라이브러리 정리를 기다리므로 timeout은 정리 시간까지 포함한 정확한 종료 시각을 보장하지 않는다.
MCP 자체의 SIGKILL이나 호스트 종료 시 정리를 보장하지 않는다.
GnuPG 공유 daemon은 [서명 기능의 수명주기](#서명-기능과-내부-의존성)를 따른다.
Git 작업별 시간 예산·쓰기 실패·후속 관측의 의미는 [Git 계약](tools/git/README.md#결과실패크기-제한)이 소유한다.

## 집행 범위와 한계

명령 허용 목록은 직접 선택한 실행을 검사하며 hooks·필터·GPG가 간접 실행하는 모든 프로그램을 차단하지 않는다.
허용 경로도 Git 내부 메타데이터·키 저장소·외부 서비스 전체를 가두는 OS sandbox가 아니다.
자동 키 다운로드 옵션을 꺼도 hooks 등 전체 네트워크를 차단했다는 뜻은 아니다.
Git·Tunnel의 원문 로그 비노출과 diff 본문의 credential 자동 마스킹은 다른 문제이며, 후자는 제공하지 않는다.
등록 CLI 작업은 알려진 `.env` 값에 한정한 보호 진단을 제공하며, 일반 비밀 탐지나 모든 OS 접근 격리를 보장하지 않는다.
자동 재시도·rollback은 제공하지 않는다.

공식 근거: [Git 설정](https://git-scm.com/docs/git-config),
[GnuPG 홈 조회](https://www.gnupg.org/documentation/manuals/gnupg/Invoking-gpgconf.html),
[키 목록](https://www.gnupg.org/documentation/manuals/gnupg/Operational-GPG-Commands.html),
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
