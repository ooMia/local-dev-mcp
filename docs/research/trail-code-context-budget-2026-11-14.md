# Trail Code 코드 탐색·컨텍스트 예산 실험 (2026-11-14)

## 목적과 경계

[Trail Code](https://github.com/trailhq/trail-code)의 Pull 기반 구조 탐색이 LOCAL_DEV_MCP의 기존 텍스트 검색·범위 읽기보다 실제 도구 출력 비용을 낮추는지 검증한다. 탐색 성공률을 낮추거나 잘못된 저장소의 코드를 가져오는 최적화는 채택하지 않는다.

이 문서는 **실험 당시 관측과 아직 검증하지 않은 운영 가설**이다. MCP 권한·도구 등록·자동 실행·기존 정책을 바꾸지 않는다. 실제 코드·Git HEAD·수정 내용의 진위는 [Workspace 계약](../tools/workspace/README.md)과 [테스트](../testing.md)로 확인한다.

## C0: 연결·저장소 스코프·인덱스 진단

2026-11-14 KST에 연결된 Trail Code MCP를 직접 호출한 결과:

| 검사 | 관측된 결과 | 판정 |
| --- | --- | --- |
| `trail_check_freshness` | 7개 저장소 인식, `local-dev-mcp/: STALE (30 stale)` | Stale 원인 미확인 |
| `trail_find_all` | PR URL 오류 문자열을 `remote_pr.py:L567`에서 탐색 | PASS (현재 HEAD와 일치 여부는 별도 검증) |
| `trail_find_code(in="local-dev-mcp/src/local_dev_mcp/tools/git")` | `CreateFeaturePRTool` 정의 및 코드 출력 | PASS (해당 범위 검색) |
| `trail_repo_map` | `local-dev-mcp/`를 별도 저장소로 식별, 356파일·2,985심볼 | PASS (지도 출력) |
| `trail_trace_calls(symbol="CreateFeaturePRTool.execute")` | 스코프 생략 시 여러 저장소의 `execute`를 반환하고 `local-dev-mcp/` 심볼 포함 | 부분 PASS (중복 이름·호출 누락 주의) |
| `trail_trace_calls(..., in="local-dev-mcp/src/local_dev_mcp/tools/git")` | `nothing indexed under ...`, FDE 내부 경로만 스코프 후보로 제시 | BLOCKED |
| `trail_file_api(file="remote_pr.py")` 및 전체 저장소 접두어 | `no wiring graph — run trail build first` | BLOCKED |
| LOCAL_DEV_MCP `search_workspace_text` | 같은 오류 문자열을 `remote_pr.py:567`에서 탐색 | PASS |

추가 조회: 2026-11-14 KST. **구조 그래프 전체가 없다는 설명은 관측과 일치하지 않는다.** 지도·스코프 검색·스코프 없는 호출 관계 조회는 `local-dev-mcp`를 인식한다. 반면 `file_api`와 명시적 `in` 인자를 사용한 `trace_calls`는 다른 방식으로 스코프를 해석하는 정황이 있다. 인덱스 드리프트 30건과 라우팅 오류의 인과관계는 아직 확인되지 않았다.

Trail Code가 표시하는 `tokens saved ≈ N`은 대상 **파일 전체 읽기와의 내부 추정 비교**이며, 실제 ChatGPT 사용 토큰이나 유효한 A/B 성과가 아니다. `trail_trace_calls`에서 심볼 이름의 모호성이 있으면 여러 저장소가 함께 출력될 수 있으므로 코드 수정의 근거로 사용하기 전 실제 소스를 검증한다.

### 저장소별 운영 복구 절차 (실행·설정 변경 미수행)

공식 CLI 문서에 따르면 `trail check`는 팀 지식 검사이고, **인덱스 신선도 검사는 `trail build --check`**다. `--check`는 빌드 없이 드리프트를 보고하며, 기본 `trail build`는 별도 LLM API 없이 구조 그래프를 생성한다.

```sh
trail build --check /Users/mia/Github/local-dev-mcp --json
# 결과가 missing/stale일 경우에만 해당 저장소 그래프 갱신
trail build /Users/mia/Github/local-dev-mcp
trail build --check /Users/mia/Github/local-dev-mcp --json
# CLI로 파일·호출 관계 검증
trail skeleton src/local_dev_mcp/tools/git/facades/remote_pr.py /Users/mia/Github/local-dev-mcp
trail callers CreateFeaturePRTool.execute /Users/mia/Github/local-dev-mcp
```

CLI에서 정상인데 공유 MCP에서 오류가 유지된다면, 별도의 `trail mcp /Users/mia/Github/local-dev-mcp` 프로세스를 **이 프로젝트 전용 클라이언트 연결로 등록**한 후 동일 도구를 재호출해 라우팅 문제를 분리한다. `trail mcp` 명령만 실행해도 기존 ChatGPT 플러그인 연결이 자동 전환되지는 않는다. 현재 연결에는 호스트 CLI 실행·MCP 등록 변경 수단이 없으므로 빌드·재연결을 수행했다고 표기하지 않는다. `--deep`은 필요하지 않으며 기존 공유 연결과 Git 파일을 임의로 초기화하지 않는다.

C0 통과 기준: 동일 저장소·Git HEAD를 대상으로 `find_code`, `find_all`, `file_api`, `trace_calls`, `repo_map`의 대상이 일치하며, 인덱스 신선도와 자동 갱신 동작을 확인한다. 실패 시 LOCAL_DEV_MCP의 실제 소스를 읽는다.

## C1: 실측 가능한 비교 기준

동일한 작업·같은 Git SHA에 대해 다음 두 경로를 쌍으로 기록한다.

- **baseline:** LOCAL_DEV_MCP에서 필요한 검색과 제한 범위 읽기만 수행한다. 무조건 파일 전체를 읽는 비효율적인 기준선을 만들지 않는다.
- **trail:** Trail Code 탐색을 적용하고 실제 소스 검증·실패 후 fallback까지 모두 기록한다.

최소 관측치: 작업 ID, 저장소 HEAD, 전략, 정확도 판정 근거, 도구 호출 횟수, 응답 UTF-8 바이트, Fallback 여부, 인덱스 상태. 정확한 도구 출력 토큰은 **동일 토크나이저에서 실측한 경우에만** 기록한다. 전체 모델 입력·캐시·청구 토큰과 도구 출력 토큰을 혼동하지 않는다. 원본 도구 응답은 비밀이 포함될 수 있으므로 기록하지 않고 측정치만 보존한다.

평가할 유형: 정확한 문자열 탐색, 심볼 위치, 파일 API, 호출 영향도, 저장소 구조. 최소 3개 사례씩 반복해 과제별 정확도·총 도구 호출량·오류 및 fallback을 비교한다.

기본 선택 정책은 조건부다. 범위가 좁은 정확한 문자열은 직접 검색하며, **정상·동일 저장소의 최신 그래프**가 확인된 구조 탐색만 Trail Code 후보로 둔다. 첫 Trail 조회에서 위치를 확인하지 못하면 같은 자연어 질문을 반복하기보다 다른 유형의 탐색 또는 LOCAL_DEV_MCP로 이동한다. 잘못된 저장소·Stale·미연결·근거 불충분은 성공이 아니라 fallback 및 비용으로 집계한다.

채택은 정확도 유지와 잘못된 참조 0건을 우선하고, 도구 응답 비용 중앙값 25% 감소를 잠정 목표로 한다. 유의한 측정 결과 없이 자동 라우터·새 외부 의존성을 도입하지 않는다.

## C1: 오프라인 비교 실행기 (선택적, 자동 라우팅 없음)

`src/local_dev_mcp/tools/context_budget.py`는 다른 MCP 서버를 호출하지 않는 순수 평가 도구다. 민감한 원문 대신 에이전트나 계측기에서 수집한 **응답 바이트·도구 호출 결과·검증 기록**의 쌍을 전달한다.

```sh
uv run --locked python -m local_dev_mcp.tools.context_budget /path/to/sanitized-runs.json
```

입력은 `{"schema_version": 1, "runs": [...]}` 형식이다. `runs`는 `case_id`당 `baseline`과 `trail` 정확히 한 건씩 포함한다. 각 Run에 `repo_sha`(40/64자리 전체 SHA), `strategy`, `index_state`(`not_applicable`은 baseline 전용), `correct`(외부 평가자가 판정), `fallback`, `verified_reference`, `tool_calls`, `tokenizer`, `elapsed_ms` 필드를 제공한다. 각 호출은 `name`, `outcome`(`ok|error`), `output_bytes`(실제 UTF-8 응답 길이), `output_tokens`(선택, 동일 토크나이저로 측정)로 기록한다. 실패와 fallback 호출도 누락하지 않는다.

서로 다른 SHA·정답 근거, 누락된 전략, 숫자를 가장한 boolean, 불완전한 결과는 거부한다. 바이트 절감률은 정확히 집계하지만 토큰 절감률은 **양쪽 모든 호출이 동일한 토크나이저로 계측되었을 때만** 출력한다. `model_input_tokens_measured=false`는 청구·캐시·프롬프트 토큰이 이번 평가의 측정 범위가 아님을 뜻한다. 평가자가 제출한 `correct`는 독립 검증을 대신하지 않는다.

현재는 비교 기록기와 테스트만 구현됐으며, Cross-MCP 호출 자동 계측·정확도 판정·Trail Code 라우팅 변경은 구현하지 않았다. 우선 C0의 범위 오류를 해결한 뒤 실제 15개 이상 과제를 수집하고, 그 결과가 유의할 때만 자동화를 검토한다.

## 참고 자료

- [Trail Code 공식 MCP·CLI·자동 갱신 설명](https://github.com/trailhq/trail-code#readme)
- [현재 저장소 구현·계약 지도](../README.md)
