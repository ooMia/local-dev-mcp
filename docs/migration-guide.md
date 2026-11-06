# Git Migration Guide

이 문서는 초기 Document Core 관리 대상의 명시적인 논리 ID와 위치를 등록한다.
JSON 블록은 진단 입력의 레지스트리이며, Markdown 설명과 링크로부터
ID나 관계·권한을 자동 추론하지 않는다.

## 관리 대상

```json
{
  "schemaVersion": 1,
  "documents": [
    {"namespace": "local-dev-mcp", "id": "root-agents", "path": "AGENTS.md"},
    {"namespace": "local-dev-mcp", "id": "docs-index", "path": "docs/README.md"},
    {"namespace": "local-dev-mcp", "id": "adr-0002", "path": "docs/adr/0002-document-authority.md"},
    {"namespace": "local-dev-mcp", "id": "adr-0004", "path": "docs/adr/0004-document-core-identity-source-boundary.md"},
    {"namespace": "local-dev-mcp", "id": "document-core-contract", "path": "docs/contracts/document-core.md"}
  ],
  "relations": [],
  "relationApproval": {
    "status": "pending",
    "record": null
  }
}
```

## 승인과 검증 이력

- **초기 상태:** 관리 대상 5개 문서의 위치와 논리 ID만 등록한다.
- **관계 승인:** 아직 없음. `relations: []`는 관계가 없다는 증명이 아니다.
- **검증 의미:** 승인 상태가 `pending`인 동안 전체 관계 검증 결과는 `Blocked`다.
- **후속 작업:** 사용자가 근거를 검토하고 관계를 승인한 후에만 승인 이력과 레지스트리를 갱신한다.

관계 후보·제안·근거 및 재검증 이력은 승인 후 이 문서에 추가한다.
본문의 Markdown 링크는 후보나 승인된 관계로 자동 편입되지 않는다.
