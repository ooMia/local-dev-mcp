# local-dev-mcp — 저장소 에이전트 지침

이 저장소는 ChatGPT에서 선택한 로컬 workspace의 파일·CLI 작업·Git 변경을 제어하는 MCP 서버다.
이 파일은 **저장소 전체의 지침 진입점**이며, ChatGPT MCP 세션에 자동 전달되는 문서라는 뜻은 아니다.

## 결정 권한과 작업 순서

1. 먼저 [문서 지도](docs/README.md)와 관련 [채택 ADR](docs/adr/0002-document-authority.md)을 확인한다.
2. 저장소 수준의 채택 결정이 하위 계약·설계 설명보다 우선한다. 구현 규약은 [conventions](docs/conventions.md), 현재 입력·권한·운영 계약은 [configuration](docs/configuration.md) 및 [기능별 계약](docs/README.md#권한과-소유권)이 소유한다.
3. 하위 폴더의 `AGENTS.md`는 범위에 필요한 제약만 **추가**한다. 상위 결정을 묵시적으로 재정의하지 않는다. 불일치가 있으면 원인을 확인하고 합의된 변경 범위에서 정합화한다.
4. 파일을 바꾸기 전 관련 상위 결정, 현재 코드·테스트와 그 변경의 소유 계약을 확인한다. 이후 필요한 문서와 검증을 함께 갱신한다.
5. Trail Code는 코드 탐색과 영향도 분석에 활용하되, 생성된 분석 결과는 실제 소스·테스트 및 채택된 ADR·소유 계약과 대조하여 검증한다.

## 안전과 완료 조건

- 정책 파일·비밀·경로·Git index·프로세스 수명은 [운영 계약](docs/configuration.md)과 [Workspace](docs/tools/workspace/README.md)·[Git](docs/tools/git/README.md)·[CLI 작업](docs/tools/tasks/README.md)의 제한을 따른다. 승인되지 않은 우회나 광범위한 자동 실행을 추가하지 않는다.
- 구현 구조는 [design](docs/design.md), 테스트 범위와 명령은 [testing](docs/testing.md)을 따른다. 변경한 영역의 검사와 현재 Git diff를 검토하고 검증 한계를 보고한다.
- 사용자 변경을 보존한다. 새 결정을 하위 문서의 임의 서술로 도입하지 않으며, 상위 결정을 바꾸려면 ADR의 대체 관계를 명시한다.
