# 설정과 운영 계약

사용자는 동작 의도·identity·외부 자원 선택·권한을 정한다. 프로그램 탐색·Git argv·wrapper·비대화형 옵션은 구현이 맡는다.
이 문서는 설정과 사용자에게 보이는 시작·종료·진단·프로세스 보장을 소유한다. 연결 원리는 [실행 흐름](design.md)을 참조한다.
개발 시 설정의 None·오류 소유권 기준은 [개발 규약](conventions.md#오류와-none)을 따른다.
실제 값을 얻는 CLI와 선택 기준은 [`.env.example`](../.env.example)에 있다. 실제 키·이름·이메일을 Chat에 붙여 넣지 않는다.

## 파일별 책임

| 파일 | 책임 |
| --- | --- |
| `.env` | Tunnel 연결값·Git identity·서명 의도·선택할 GnuPG 자원 |
| `config.toml` | 개발 workspace 접근과 호스트 기능의 논리적 시스템 명령 권한 |
| `pyproject.toml` | Python·의존성·설치 진입점·검사 기준 |
| `uv.lock` | 재현 가능한 패키지 버전 |

정책 파일 이름은 `config.toml`로 고정한다. CLI·환경 변수로 정책을 확대하지 않는다.
`.env`도 실행 디렉터리의 파일만 읽으며 상위 검색·변수 보간·ambient 값 fallback은 없다.

## Workspace와 호스트 서명 권한

workspace 접근 권한은 개발 프로젝트의 파일·Git 작업 범위를 사용하는 권한이다.
호스트 서명 권한은 선택한 키 저장소·키로 commit을 서명·검증하는 권한이며 두 범위는 구분한다.
`filesystem.allowed_paths`는 개발 workspace 사용을 허용하고, 각 Git 작업 대상은 해당 workspace로 제한한다.
`system.allowed_commands`의 `gpg`는 선택한 호스트 GnuPG·키 저장소·키를 서명과 검증에 사용하는 권한이다.
키 저장소를 workspace용 allowed_paths에 추가할 필요는 없으며 일반 파일 도구의 작업 대상으로 확대하지 않는다.
시작 설정과 직접 Git 실행 모두 gpg 허용을 확인한다. 홈의 유효성 검사는 유지하고 workspace 포함 검사는 적용하지 않는다.

기본은 기존 호스트 설치·키·공유 daemon 재사용이다. 별도 키 복사·생성·전용 환경은 준비하지 않는다.
실행 파일·키 저장소는 시작 시 선택·고정하며 실패 시 다른 키·홈·unsigned 방식으로 바꾸지 않는다.
호스트 업데이트·키 잠금·서비스 변화의 영향을 받아들이고 공유 daemon을 강제 종료하지 않는다.
현재 정책은 개발 workspace를 OS 수준에서 격리하지 않는다.

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
| `run` | 프로젝트 경로·git 허용, Git 실행 경로, identity, 서명 플래그, 자식 환경 |
| `run`의 서명 true | gpg 허용, 키 fingerprint, 명시 또는 탐색한 홈의 유효성, GPG·wrapper 실행 경로 |
| `connect` | tunnel-client 허용, Tunnel 두 값, client 실행 경로·환경. 시작 전 별도 프로세스로 run의 정적 설정도 검사 |

공통 정책 파일의 문법·스키마 오류, workspace 접근 거부, 필수 .env를 읽을 수 없는 경우는 시작 실패다.
Git 미설치·미허용은 Git 도구만, identity·서명 설정의 알려진 누락·무효·준비 오류는 commit만 사용 불가로 만든다.
예상하지 못한 내부 예외는 준비 부족으로 숨기지 않고 시작 실패로 처리한다.
identity 없이 Git 조회·stage/unstage를 사용할 수 있으며 Git 없이도 서버와 준비 상태 조회는 시작할 수 있다.
Git 저장소 여부는 실제 Git 작업에서 검사한다.
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
client 시작 메시지·종료 코드 0은 원격 readiness나 실제 Chat 연결 성공을 뜻하지 않는다. Chat에서 실제 도구 호출을 확인한다.

## 도구 준비 상태

`get_tool_readiness()`는 인자 없는 읽기 전용 도구다. Git·identity·GPG 없이 공통 workspace 권한만으로 조회한다.
`project`, 설정을 고정한 UTC `snapshot_at`, `scope="startup_snapshot_only"`, `tools`를 반환한다.
각 도구 항목은 `name`, `ready`, `issue`이며 조회 도구 자신도 포함한다.
준비된 항목의 issue는 null이다. 준비 부족은 안전한 `operation`, `target`, `reason`, `action`,
`restart_required=true`를 담으며 실제 identity·키·외부 원문은 포함하지 않는다.

ready는 시작 설정·권한·선택한 실행 파일의 정적 준비만 뜻한다. 조회는 파일 재로딩·외부 명령 실행·키 잠금 확인을 하지 않는다.
서명 설정이 준비됐어도 실제 키 사용이나 Git 작업은 실패할 수 있다. 설정 수정은 재시작 후 반영하며 자동 복구하지 않는다.

준비되지 않은 도구도 공개 목록에 유지한다. 호출은 권한 확인 뒤 정적 준비 상태를 먼저 확인하고,
사용 불가이면 인자 검증·동적 준비·본문 전에 MCP 오류를 반환한다. 준비 부족과 잘못된 인자가 함께 있으면 준비 오류가 우선한다.
내부 Python 실행도 필요한 정책·준비값을 확인하며 권한 middleware만으로 보호된다고 가정하지 않는다.

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

[system]
allowed_commands = ["git", "tunnel-client"]
```

서명 true인 경우 `allowed_commands`에 `gpg`만 추가한다. 키 저장소 경로를 `allowed_paths`에 추가하지 않는다.
Git workspace는 실행 디렉터리로 고정되며 호스트 서명 권한이 작업 범위를 확대하지 않는다.
workspace 허용 경로는 설정 위치 기준으로 resolve한다. GnuPG 홈도 resolve하되 존재·디렉터리 유효성만 검사한다.
논리 명령 `gpg` 허용은 선택한 실행 파일의 진위 검증을 뜻하지 않는다. PATH와 override는 사용자가 신뢰하고 관리한다.

두 정책 항목은 문자열 목록이다. 빈 문자열·공백만 있는 원소는 거부하고 원소의 공백을 자동 제거하지 않는다.
빈 목록은 파싱 가능하다. workspace 미허용은 시작을 막고, git/gpg 미허용은 해당 기능의 준비 부족으로 기록한다.
connect에 필요한 tunnel-client 미허용은 connect 실패다.
알 수 없는 필드는 거부하지 않지만 제한을 제공하지도 않는다. 접근 제한은 위 두 정책 항목으로 판단하며
파일 패턴 필터나 `.gitignore`를 접근 정책으로 사용하지 않는다.

## 프로세스 환경

`settings.py:read_dotenv()`가 정확한 `.env`를 읽고, `runtime.py:ServerSettings`가 서버별 불변 설정을 만든다.
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

`run`은 FastMCP import 전에 전체 환경을 최소화하고 내부 `FASTMCP_ENV_FILE=os.devnull`로 라이브러리의 자동 dotenv 로딩을 끈다.
그 뒤 애플리케이션이 필요한 설정을 명시적으로 읽는다. 이 내부 제어값과 Tunnel credential은 Git에 전달하지 않는다.
Tunnel 자식에는 인증 환경이 일시 상속될 수 있으나 run이 서버 구성 전에 제거한다.

GnuPG 홈의 기존 설정은 읽는다. Git global/system config를 다시 활성화하는 것은 아니다.
identity·서명 형식·키·wrapper는 준비한 설정이 우선하며 hooks·필터는 저장소 local 설정을 유지한다.
준비값은 해당 자식과 hooks 등에서 보일 수 있다. 환경 선택은 호스트 credential 저장소 전체의 격리가 아니다.

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
원문 로그 비노출과 diff 본문의 credential 자동 마스킹은 다른 문제이며, 후자는 제공하지 않는다.
자동 재시도·rollback은 제공하지 않는다.

공식 근거: [Git 설정](https://git-scm.com/docs/git-config),
[GnuPG 홈 조회](https://www.gnupg.org/documentation/manuals/gnupg/Invoking-gpgconf.html),
[키 목록](https://www.gnupg.org/documentation/manuals/gnupg/Operational-GPG-Commands.html),
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
