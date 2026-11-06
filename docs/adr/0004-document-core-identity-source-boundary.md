# ADR 0004: Document Core의 정체성과 소스 어댑터 경계

- 상태: Accepted
- 범위: 범용 Document Core의 실험적 1단계 모델 및 Markdown 입력 경계
- 결정일: 2026-10-30

## 맥락

문서는 파일 이동이나 내용 수정 이후에도 같은 논리적 대상으로 식별되어야 한다.
그러나 파일명·경로 또는 Markdown Frontmatter에서 ID를 유추하도록 Core를 구성하면,
사용처에 따라 달라지는 문서 식별 규약이 범용 모델에 결합된다.

## 결정

1. 문서의 논리적 정체성은 **namespace + ID**이며, Revision과 Location은 별도 값이다.
   동일한 로컬 ID도 namespace가 다르면 서로 다른 문서다.
2. **Core는 명시적으로 전달된 ID만 사용한다.** ID를 어디서 획득하는지(Frontmatter·Manifest·외부 저장소 등)는
   Source Adapter 또는 그 호출자가 소유한다. 경로·제목·내용 해시를 논리 ID로 대체하지 않는다.
3. Revision은 Core에서 출처를 가정하지 않는 불투명 값이다. Markdown의 첫 실험 어댑터는
   전달받은 원문 UTF-8의 SHA-256을 Revision으로 사용한다. Location은 source와 locator를 함께 가지며
   현재 문서가 관측된 위치일 뿐 문서 정체성이나 파일 접근 권한이 아니다.
4. Core에는 Git, Markdown, MCP, 파일 시스템 접근, 영속 상태 및 프로젝트별 정책 검증 의존성을 두지 않는다.
   첫 실험 어댑터는 **이미 읽은 Markdown 문자열**을 받아 Core 값으로 정규화하며, 파일을 직접 읽거나
   Frontmatter에서 ID를 해석하지 않는다.
5. 첫 API는 Experimental로 취급하며, 실제 검증 근거 없이 Stable 호환 계약을 선언하지 않는다.

## 결과 및 경계

- 경로만 달라진 Markdown은 같은 Identity와 Revision을 유지할 수 있다.
- 원문이 달라지면 Markdown 어댑터의 Revision이 달라진다.
- 관계 모델, 정책 엔진, 캐시, 증분 검증, Git 과거 Revision 및 MCP 노출은 후속 단계다.
- 초기 구현은 동일 ID의 중복 발견·Revision 간 계보·원격 Source의 존재 여부를 검증하지 않는다.

현재 관측 가능한 입력·출력과 수락 기준은 [Document Core 실험 계약](../contracts/document-core.md)이 소유한다.
문서 권한 정책은 [ADR 0002](0002-document-authority.md)를 따른다.
