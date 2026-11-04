# local-dev-mcp

ChatGPT가 지정한 로컬 개발 workspace를 MCP로 다루도록 지원하는 프로젝트다.

- 모든 변경 전에 [개발 규약](docs/conventions.md)을 읽고 변경 완료 조건까지 적용한다.
- 설정·권한·시작 동작을 바꿀 때는 [설정 계약](docs/configuration.md)을 확인한다.
- Git 도구를 바꿀 때는 [Git 계약](docs/tools/git/README.md)을 확인한다.
- 호출 경계를 바꿀 때는 [실행 흐름](docs/design.md)을, 검증할 때는 [테스트 안내](docs/testing.md)를 확인한다.

이 파일은 저장소를 수정하는 coding agent의 진입점이다. MCP를 사용하는 Chat 세션에 자동 전달되는 문서는 아니다.
