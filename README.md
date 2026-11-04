# local-dev-mcp

ChatGPT chat 세션에서 지정한 로컬 개발 workspace를 다루기 위한 MCP 실행 프로젝트다.
현재 도구 준비 상태와 Git HEAD·status·diff 조회, stage·unstage·commit을 제공한다.
파일 CRUD·범용 명령 실행·원격 Git 작업·OS 격리는 제공하지 않는다.

## 준비와 실행

Python 3.15 이상·uv가 필요하며 Git 도구에는 Git이 필요하다. Chat 연결에는 별도로 설치한 tunnel-client와
사용자가 준비한 OpenAI Secure MCP Tunnel이 필요하다.

1. 이 저장소에서 `uv sync --locked`로 개발 환경을 준비한다.
2. 대상 workspace에 [`.env.example`](.env.example)의 안내로 `.env`를 준비한다.
3. 같은 위치의 `config.toml`에 대상 workspace와 필요한 명령을 허용한다.
   [설정 예시와 권한](docs/configuration.md#configtoml)을 따른다.
4. 대상 루트를 현재 디렉터리로 하여 설치된 환경의 CLI를 실행한다. 이 저장소를 대상으로 하면 다음을 사용한다.

```sh
uv run --locked local-dev-mcp connect
```

| 명령 | 역할 |
| --- | --- |
| `local-dev-mcp run` | stdio MCP 서버 실행 |
| `local-dev-mcp connect` | 시작 설정 사전 검사 후 Tunnel client 실행, client가 같은 Python 환경의 run 실행 |

CLI는 credential을 질문하지 않는다. identity는 commit에 필요하고,
서명은 `GIT_COMMIT_SIGN`으로 선택한다. 설정 변경 후에는 재시작한다.
정확한 값·생략·탐색 규칙은 [설정 계약](docs/configuration.md)을 따른다.

Git·identity·서명 준비 부족은 해당 도구만 사용 불가로 만들고 WARNING으로 안내한다.
공통 정책·workspace·필수 파일 오류는 연결을 막는다.
연결된 Chat에서 먼저 준비 상태를 확인한다.

> local-dev-mcp의 get_tool_readiness를 호출해서 사용할 수 없는 도구와 필요한 준비를 알려줘.

연결 중에는 터미널을 유지하고 Ctrl+C로 종료한다. client 시작 메시지나 종료 코드 0은 Chat 연결 성공의 증거가 아니다.
사전 검사 실패는 안전한 원인·수정 안내를 표시하며, 외부 client의 원문 로그는 전달하지 않는다.
직접 stdio로 연결하는 MCP client에는 `local-dev-mcp run`을 사용한다.

## 도구 사용

| 도구 | 역할 |
| --- | --- |
| `get_tool_readiness` | 시작 snapshot의 도구별 준비 여부·안전한 수정 안내 |
| `get_project_head` | 최신 로컬 HEAD commit SHA |
| `get_project_status` | branch/HEAD와 경로별 index·working-tree 상태 |
| `get_project_diff` | working tree 또는 staged patch |
| `stage_project_paths` | 명시한 상대 경로의 추가·수정·삭제 stage |
| `unstage_project_paths` | 작업 파일을 남기고 unstage |
| `commit_project_changes` | 현재 index 전체로 commit |

전체 stage는 `paths=["."]`로 명시한다. commit은 자동 stage하지 않으며 기존 staged 변경도 포함한다.
입력·결과·timeout·서명·실패 후 확인 방법은 [Git 계약](docs/tools/git/README.md)에 있다.
허용한 프로그램의 모든 간접 실행을 가두는 sandbox는 아니다.

## 개발

변경 전 [개발 규약](docs/conventions.md)을 읽고 [검사 명령](docs/testing.md#실행-경로)을 따른다.
의존성과 진입점은 `pyproject.toml`, 정확한 설치 버전은 `uv.lock`이 소유한다.
설치 진입점의 경로를 바꾼 뒤에는 `uv sync --locked --reinstall-package local-dev-mcp`로
개발 환경의 실행 스크립트를 갱신하고 서버를 재시작한다.

## 문서

- [개발 규약](docs/conventions.md): 명명·경계·타입·테스트·문서 유지와 변경 완료 조건.
- [설정과 운영 계약](docs/configuration.md): 환경 값·권한·시작 snapshot·종료 코드·진단·정리 보장.
- [Git 계약](docs/tools/git/README.md): 입력·명령·결과·실패 의미.
- [연결 원리](docs/design.md): CLI·MCP·Tunnel의 흐름과 import·오류·취소 처리의 이유.
- [검사와 환경](docs/testing.md): 실행 절차·fixture 자원·설치 검증의 범위.
