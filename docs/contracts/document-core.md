# Document Core — Experimental Contract

이 계약은 Stage 1~4에서 검증하는 Python 내부 API에만 적용한다. MCP 도구나 CLI 명령을 추가하거나 공개
호환성 보장을 선언하지 않는다. [ADR 0004](../adr/0004-document-core-identity-source-boundary.md)의
결정을 구현 가능한 값 계약으로 표현한다.

## 값 계약

| 값 | 정의 | 불변조건 |
| --- | --- | --- |
| `DocumentIdentity(namespace, value)` | 명시적 논리 ID | 두 값이 함께 정체성을 구성하며, 경로/Revision과 무관 |
| `DocumentRevision(value)` | 소스가 제공한 불투명 버전 ID | Core는 해시/Commit/시간 규칙을 강제하지 않음 |
| `DocumentLocation(source, locator)` | 현재 소스와 그 안의 위치 | 정체성이나 파일 접근 권한이 아님 |
| `Document(identity, revision, location, content)` | 한 위치에서 관측한 문서 버전 | 값 객체, 변경 불가능 |

모델의 label 값은 비어 있거나 양끝 공백·제어 문자를 포함할 수 없다.
텍스트 내용은 빈 문자열이어도 된다. 동일 namespace·ID 중복에 대한 문서 집합 수준 검증은
Stage 1의 책임이 아니다.

## Markdown 입력 어댑터

`from_markdown(markdown: str, *, identity: DocumentIdentity, location: DocumentLocation) -> Document`

- 호출자가 ID와 Location을 제공한다. ID가 없으면 경로나 첫 제목으로 자동 보완하지 않는다.
- 이미 읽은 **원문 문자열**을 그대로 저장한다. 파일을 열거나 Markdown을 파싱·정규화하지 않는다.
- Revision은 `sha256:` + 원문을 UTF-8로 인코딩한 바이트의 SHA-256 소문자 16진수이다.
  따라서 LF와 CRLF의 차이도 다른 Revision으로 반영된다.
- 단순 경로 변경은 명시적으로 공급한 Identity 및 동일 내용의 Revision을 변경하지 않는다.
- 외부 어댑터는 다른 불투명 Revision 체계를 공급할 수 있으므로 이를 Core가 Git SHA라고 가정하지 않는다.

## Stage 1 수락 기준

1. 명시적 namespace+ID가 유지되고 Markdown 안의 `id` 메타데이터에 의해 덮어쓰이지 않는다.
2. 원문이 동일한 파일 이동은 Identity와 Revision 유지, Location 변경으로 표현된다.
3. 원문 편집은 Identity 유지, Revision 변경으로 표현된다.
4. 동일 로컬 ID의 서로 다른 namespace를 구분한다.
5. 값 객체가 불변이며 잘못된 label 및 문자열이 아닌 원문은 거부된다.
6. 순수 단위 테스트로 Core/어댑터를 검증하며 Git·MCP·OS 파일 접근을 요구하지 않는다.
7. 패키지 루트와 Core를 import할 때 `app.cli`를 미리 불러오지 않는다.
   기존 `from local_dev_mcp import main` 호출은 유지하며, 호출 시 기존 CLI로 연결한다.

`tests/document_core/test_document.py`가 Stage 1 수락 검사를 소유한다.
Stage 1 자체에는 relation graph, policy/ValidationResult, Markdown 파일 수집, 외부 ID resolver가 포함되지 않는다.

## Stage 2: directed relation graph

`DocumentReference(identity: DocumentIdentity, revision: DocumentRevision | None = None)`는
논리 ID를 가리키며, `revision`이 있으면 **정확히 같은 버전**을 요구한다.
`DocumentRelation(source: DocumentIdentity, target: DocumentReference, kind: str)`는
방향이 있는 간선이다. `kind`는 호출자가 제공하는 일반 문자열이며, `references`,
`governs`, `supersedes` 등의 의미·권한을 Core가 해석하지 않는다.

`DocumentGraph(documents: Iterable[Document], relations: Iterable[DocumentRelation])`는
입력 시점의 문서·관계들을 불변 튜플로 보유하는 **단일 Snapshot**이다.
- 한 Snapshot은 각 논리 ID에 문서 버전을 하나만 보유한다. 동일 Identity의 중복은
  Revision이 달라도 오류로 거부한다. 여러 버전의 동시 보관·이력 탐색은 아직 지원하지 않는다.
- `resolve(reference)`는 ID가 존재하고 선택한 Revision까지 일치할 때만 해당 Document를 반환한다.
  존재하지 않거나 고정 Revision이 다른 경우에는 모두 `None`이다. 이 시점에서 오류 심각도를 정하지 않는다.
- `outgoing(identity)` / `incoming(identity)`는 방향별 Relation 튜플을 입력 순서대로 반환한다.
  역방향 간선을 임의로 추가하거나 중복 간선을 제거하지 않는다.
- 누락된 source/target, 순환, 반복 관계를 **보존**한다. 허용 여부는 Stage 3 정책 검증이 결정한다.
- Graph는 파일 시스템·Git·MCP·Markdown에 직접 의존하지 않으며 외부 상태를 수정하지 않는다.

### Stage 2 수락 기준

1. 논리 참조는 주어진 Snapshot의 문서를 해석하고 Revision을 고정하면 내용 변경 시 불일치를 식별한다.
2. namespace가 다른 동일한 로컬 ID를 혼동하지 않는다.
3. incoming/outgoing은 간선 방향을 유지하며 관계 종류를 임의 해석하지 않는다.
4. 누락·순환·반복 관계는 Stage 3 판단을 위해 보존한다.
5. 동일 Identity를 가진 두 문서를 하나의 Snapshot에 등록하면 명시적으로 거부한다.
6. 테스트는 Git·파일 읽기·MCP 서비스 없이 실행한다.

[Stage 2 단위 테스트](../../tests/document_core/test_relations.py)는 위 동작을 검증한다.
Stage 2 자체는 관계 규칙·검증 결과·오류 정책을 제공하지 않는다. Stage 3의 인터페이스는 아래를 참조한다.

## Stage 3: 프로젝트별 검증 규칙과 구조화된 결과

- `ValidationOutcome`: `Verified`, `Inconsistent`, `Unverified`.
  이는 **판단의 검증 상태**이며, 심각도나 빌드 차단 여부를 의미하지 않는다.
- `ValidationResult(subject, rule, code, outcome, evidence=())`: 문서의 논리 ID 및 선택적
  Revision을 가진 `DocumentReference`를 subject로 지정한다. `rule`은 평가한 규칙의
  식별자, `code`는 규칙이 정의한 결과 코드다. `evidence`는
  `ValidationEvidence(kind, detail)`의 불변 튜플이다.
- `ValidationRule = Callable[[DocumentGraph], Iterable[ValidationResult]]`:
  규칙 구현과 프로젝트별 해석은 **호출자 소유**다. Core가 관계 이름에 따라 검증 규칙을 자동 선택하지 않는다.
- `validate(graph, rules)`는 호출자가 제공한 순서로 규칙을 실행하고
  반환된 결과를 발생 순서대로 불변 튜플에 모은다. 규칙이 없거나 아무 결과도 없으면 빈 튜플이며,
  이를 전체 검증 성공 선언으로 취급하지 않는다. 검증 실행 중 오류나 잘못된 결과 타입은
  정상 성공으로 은폐하지 않고 예외를 전파한다.

### Stage 3 수락 기준

1. 같은 DocumentGraph에 서로 다른 프로젝트 규칙을 주입하면 해당 프로젝트의 의미에 따라
   `Verified`, `Inconsistent`, `Unverified`를 구별해 반환할 수 있다.
2. `subject`의 ID와 선택적 Revision, `rule`, `code`, `evidence`가 결과에 보존된다.
3. 결과 순서는 입력한 규칙 순서와 각 규칙의 발생 순서를 따른다. 자동 캐시·동시 실행은 하지 않는다.
4. Core는 `governs`/ `supersedes` 등 관계 이름의 의미·심각도·차단 정책을 강제하지 않는다.
5. 예외 또는 유효하지 않은 규칙 출력은 검증 성공으로 오인하지 않는다.
6. 파일·Git·MCP 없이 순수 테스트를 실행한다.

[Stage 3 수락 테스트](../../tests/document_core/test_validation.py)가 서로 다른 프로젝트 규칙,
누락·Revision 불일치·실행 실패·구조화된 결과를 검증한다.
아직 공통 정책 DSL·영속 저장소·증분 검증·MCP 공개 어댑터는 없다.


## Stage 4: Markdown → Document → Relation Graph → ValidationResult 통합 검증

Stage 4는 Stage 1~3의 **기존 공개 Python 객체와 함수만 조합**하는 수락 시나리오이다.
별도 CLI·MCP 도구·새로운 오케스트레이터 API를 제공하지 않는다.

- 테스트는 격리한 임시 디렉터리에 ADR·Contract·Article·Reference Markdown을 작성하고
  테스트 호출자에서 파일을 읽는다. 파일 경로에 대한 권한·영속적 파일 수집 책임을 Core에 부여하지 않는다.
- 호출자가 각 문서의 명시적 `DocumentIdentity`와 `DocumentLocation`을 공급한 뒤
  `from_markdown`으로 Document를 만든다. 관계도 호출자가 `DocumentRelation`으로 선언한다.
  **Markdown 본문이나 Frontmatter에서 관계·ID를 자동으로 추출하지 않는다.**
- `DocumentGraph`는 제공된 단일 Snapshot을 구성하고, `validate`는
  테스트 프로젝트가 제공한 독립된 ADR/Contract 및 Article/Reference 규칙을 평가한다.
- 존재하는 Revision 고정 참조는 `Verified`, 원문 변경으로 고정 Revision이 불일치하면
  해당 테스트 정책에서 `Inconsistent`를 반환한다. 외부 참조 누락은 프로젝트 정책에
  따라 `Unverified` 또는 `Inconsistent`로 다르게 분류한다.
- 증거는 실제 Snapshot 관측과 관련 참조를 남기며, 결과 상태의 의미를
  Core의 기본 심각도나 배포 차단 규칙으로 확대하지 않는다.

### Stage 4 수락 기준

1. 디스크의 Markdown 입력이 명시적 ID를 유지하며 Document·Graph·Result로 이어진다.
2. ADR/Contract와 Article/Reference라는 서로 다른 문서 유형과 규칙을 같은 Core로 평가한다.
3. 계약 파일의 내용 변경이 고정된 Revision 관계를 불일치로 바꾸는 것을 확인한다.
4. 외부 참조의 누락을 프로젝트마다 다르게 판단해도 Core를 수정하지 않는다.
5. 기존 Stage 1~3 테스트와 전체 회귀가 통과하고, 예외적인 미검증 범위를 보고한다.

[Stage 4 통합 테스트](../../tests/system/test_document_core_vertical_slice.py)는 테스트의 실제 파일 접근을
포함한 Python 경계 검증이다. **실제 프로젝트 문서의 자동 수집·관계 추론·MCP 수용 검증이 아니다.**
