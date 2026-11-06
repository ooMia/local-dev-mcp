# 문서 진단 MCP 계약

[Document Core 실험 계약](../../contracts/document-core.md)은 소스·Git·MCP와 독립적이다.
이 공개 도구는 선택된 workspace에 등록된 초기 문서 집합만 읽는 어댑터다.

## diagnose_documents

- 입력: 필수 `repository_id: string` 하나. 초기 허용 값은 `local-dev-mcp`이며,
  서버 시작 시 명시적으로 선택한 이름이 `local-dev-mcp`인 workspace에만 결합한다.
  호출자가 파일 경로나 Git URL을 전달하여 접근 범위를 바꿀 수 없다.
- 초기 관리 대상: [Migration Guide](../../migration-guide.md) JSON 레지스트리의 5개 문서.
- 승인 관계: 초기에는 **없음(미승인)**. 링크·경로·문서 종류에서 관계를 추론하지 않는다.
  `relations: []`는 승인된 빈 관계 집합을 뜻하지 않는다.
- 접근: 기존 workspace 허용·거부 정책, symlink 차단과 크기 상한을 사용한다.
  파일의 UTF-8 원문을 수집하고 종료 전 재확인한다.
- 부작용: 파일 쓰기, 커밋, 자동 승인, 후보 편입, Git 원격 요청 없음.

## 진단 의미

정상적으로 수집된 초기 Snapshot의 결과는 `completion = "Blocked"`이다.
진단은 완료되었지만 **관계 승인 절차가 완료되지 않아 전체 정합성 검사는 차단되었다**는 의미다.
이에 따라 문서별 `outcome`은 `null`, `evaluatedDocuments`는 0이다.

- `schemaVersion`: MCP 응답 스키마 버전
- `snapshot.formatVersion`: 정규화된 관리 상태의 해시 형식 버전
- `snapshot.fingerprint`: SHA-256. 저장소 ID·절대 경로·HEAD·실행 시각을 제외한다.
- `ruleSet`: 현재 진단 규칙 집합의 식별자와 버전
- `summary`: 전체·수집·평가 문서 수, 상태별 집계, 승인 관계 수
- `documents[]`: 명시된 namespace·ID, 저장소 상대 Location, 원문 SHA-256 Revision, 결과 상태
- `coverage[]`: Snapshot/Document/Relation 검사의 수행·차단 여부
- `findings[]`: 안정적인 규칙 코드와 Finding Key, severity, 참조 기반 evidence

초기 차단 Finding의 코드는 `RELATIONS_APPROVAL_PENDING`이고, Key는 Snapshot 간 안정적이다.
Coverage에서 파일 수집 완료와 실제 정책 검증 완료를 혼동해서는 안 된다.

Fingerprint는 `formatVersion`, 정렬된 문서 Identity·Location·Revision, 승인 관계,
레지스트리 승인 상태를 canonical JSON으로 직렬화하고 SHA-256으로 계산한다.
Markdown Guide의 설명 문단만 바뀌면 Fingerprint가 달라지지 않지만,
동일 실행 중 원문 변경이 탐지되면 `SNAPSHOT_CHANGED`로 실패한다.
변경 탐지는 가능한 범위의 재확인이지 원자적 파일시스템 Snapshot 보장은 아니다.

## 오류와 현재 범위

미등록 Repository, 접근 거부, 레지스트리 오류, 읽기 실패, Snapshot 변경은 도구 실행 오류다.
정상 진단이 `Blocked`인 것과 구분하며, 실행 오류에서 유효한 DiagnosticResult를 반환하지 않는다.
원문이나 비밀값은 진단 근거에 포함하지 않는다.

초기 JSON 스키마는 `relationApproval.status=pending`과 `relations=[]`만 허용한다.
승인 이력을 임의로 `approved`로 변경해 검증을 우회할 수 없으며,
향후 승인·검증 완료 경로는 사용자 결정과 별도 검증 후 확장한다.
과거 진단 저장이나 자동 Finding 비교, CLI 공개도 초기 범위에 포함하지 않는다.
