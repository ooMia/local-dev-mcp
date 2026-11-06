# docs/ 지침

범위: `docs/`의 모든 문서. 루트 [AGENTS.md](../AGENTS.md)와 [문서 지도](README.md)를 따른다.
이 파일은 상위 결정을 변경하지 않고 문서 작성 시의 추가 제약만 제공한다.

- 변경 전 관련 [Accepted ADR](adr/0002-document-authority.md)과 소유 계약을 확인한다. ADR과 계약이 다르면 하위 문구만 임의로 바꾸지 않는다.
- 결정은 `adr/`, 조사 근거는 `research/`, 현재 운영 계약은 `configuration.md`, 공개 기능 계약은 `tools/*/README.md`에 둔다.
- `conventions.md`는 개발 규약, `design.md`는 구현 연결, `testing.md`는 검사 절차와 증거만 소유한다.
- 다른 문서에 이미 있는 규칙을 반복하지 말고 상대 링크로 연결한다. 내부 링크·앵커와 코드 참조가 유효한지 확인한다.
- 조사 시점의 가설을 현재 보장으로 바꾸지 않는다. 채택된 결정의 변경은 새 ADR이나 명시적 대체 관계로 추적한다.
