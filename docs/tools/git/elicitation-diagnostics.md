# MCP Elicitation Capability 진단 (T3)

현재 `fastmcp==4.1.1`과 MCP `2026-07-28` 환경에서 직접 `ctx.elicit()`를
호출하면 `elicitation via server-initiated requests is unavailable on 2026-07-28 connections`
오류가 관측된다. 이 프로토콜은 서버 시작 요청 대신 `InputRequiredResult` 및
클라이언트 재요청의 `inputResponses`를 사용하는 **다중 왕복** 방식이다.

참고: https://modelcontextprotocol.io/specification/2026-07-28/client/elicitation

## 등록된 도구

- `diagnose_client_elicitation`: 읽기 전용. `Context.request_context.meta`에서
  `io.modelcontextprotocol/clientCapabilities`를 확인하고, SDK Session의
  Capability와 프로토콜 버전을 비교한다. 실제 Metadata 값·자격 증명은 반환하지 않는다.
- `probe_client_elicitation`: 레거시 연결에서만 서버 시작 Form 진단을 수행하며, 현대 프로토콜에서는 차단한다.
- `probe_client_elicitation_mrtr`: `2026-07-28`과 요청별 Form Capability가 확인된 경우에만
  `InputRequiredResult`로 진단 Form을 요청한다. 재호출에서 `inputResponses` 및 검증된
  `requestState`를 해석한다. 어떠한 응답도 Git 권한이나 Main 병합 승인을 생성하지 않는다.

`elicitation: {}`는 표준 Form 지원 선언으로 판별한다. URL-only 선언은 Form 지원이 아니다.
세션에 Elicitation Capability가 있어도 **각 요청의 `_meta`가 없으면 지원으로 간주하지 않는다**.
테스트용 FastMCP 클라이언트에서 호출자가 Form Metadata를 전달했더라도 서버가
관측한 Capability 항목에는 `elicitation`이 없을 수 있다. 호출자 입력이 아니라
실제로 도착한 세션/요청 데이터를 기준으로 판단한다.

## 현재 보장과 차단 범위

- Accept/Decline/Cancel은 서로 다른 결과로 처리하며, 어느 경우에도 Main 통합 승인을 발급하지 않는다.
- SDK 프로토콜에 맞지 않으면 진단 도구가 `blocked`로 반환한다.
- `tests/mcp/test_client_elicitation_mrtr.py`는 FastMCP 인프로세스 클라이언트 콜백을 통해
  실제 MRTR 두 단계 왕복과 Accept·Decline·Cancel의 비파괴 처리를 검증한다.
  이 테스트의 Capability는 명시적으로 주입하므로 **ChatGPT 실제 사용자 UI 테스트를 대체하지 않는다**.
- `merge_project_worktrunk`는 계속 차단한다. 진단 MRTR 구현 자체는 사용자 승인 근거가 아니다.
  실제 ChatGPT 클라이언트의 Capability 선언·폼 표시·응답 왕복과 승인 주체 확인 후에만
  계획 Digest와 단회성 권한에 결합하는 별도 승인 계약을 활성화할 수 있다.
- 진단을 활성화하려면 설치된 MCP 서버를 재시작/재로드해야 한다.

## 테스트

`tests/mcp/test_client_elicitation.py`:
표준 Form·URL·빈 Capability 호환성, 선언 누락 및 미지원 프로토콜의 차단,
Accept/Decline/Cancel 결과의 순수 해석, 실제 서버에서의 메타데이터 관측.

현대 프로토콜의 인프로세스 MRTR 진단은 PASS하더라도, ChatGPT 사용자 승인 Form
라운드트립은 여전히 **UNVERIFIED/BLOCKED**이며 `main` 통합은 허용되지 않는다.
