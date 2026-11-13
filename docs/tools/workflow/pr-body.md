# PR Body Generator

기존 [ADR 0005](../../adr/0005-assessed-squash-and-batched-releases.md)와 [ADR 0006](../../adr/0006-native-pr-workflow-mvp.md)의 공개 통합·출처·릴리스 평가 계약을 유지한다.
개발 CLI의 `pr-body collect/sync`는 신규 `.changes` 평가를 추가하는 일반 PR 전용이다. 평가를 소비하는 릴리스 PR은 별도 MCP `create_project_draft_pr` 경로에서 검증된 릴리스 영수증을 사용해 지원한다. CLI의 release collect/sync까지 확장한 것은 아니다.
새로운 GitHub App, 버전 계산 방식 또는 별도 보존 Ref를 추가하지 않는다.

## 소유권과 데이터 출처

| PR 정보 | 원본 | 정책 |
| --- | --- | --- |
| Purpose | 이번 PR의 최고 영향 평가 reason | 복수의 동급 근거면 명시적 리뷰 입력 필요 |
| Changes | 기준 브랜치와 HEAD 사이의 새 .changes/*.toml의 summary | 기존 누적 평가 제외 |
| Release impact | 같은 신규 평가의 impact + ID | 예상 다음 릴리스 버전과 혼동 금지 |
| Commits | Git merge-base 이후의 원본 SHA·subject | 5개 이하는 전체 SHA 링크, 6개 이상은 숫자와 PR Commits 링크 |
| Migration | 신규 평가 migration | 내용이 있을 때만 |
| 기타 조건부 예외 | 에이전트가 문서 근거를 검토해 입력 | Activation, Compatibility, Verification gap, Review blocker |

일반적인 테스트·린트 성공 현황과 checklist는 PR에 반복하지 않는다. GitHub Project quality의 CI 결과가 해당 상태를 소유한다.
특정 보장의 검증 공백이나 병합 후 필수 조치처럼 리뷰 판단을 바꾸는 정보만 조건부로 제공한다.

## 수집과 최초 렌더링

로컬 main과 origin/main은 서로 다를 수 있다. GitHub PR의 실제 base는 원격 main이므로 로컬 main을 기준으로 작성하면 안 된다.
collect는 로컬 Git fetch를 수행하지 않으며, 인증된 gh repo view로 저장소 URL을 확인한 뒤 gh api의 현재 원격 main SHA와 지정된 base Ref를 대조한다.
또한 PR Head가 base 전체를 포함하지 않으면 생성하지 않는다. 기존 local main은 자동 Reset·Force Push·Rebase하지 않는다.

```bash
git fetch origin main
git rev-list --left-right --count main...origin/main
git log --oneline --left-right main...origin/main
git merge-base --is-ancestor origin/main HEAD
```

마지막 명령이 실패하면 feature 브랜치에서 원격 변경을 검토해 통합한 후 재검증한다. 원격 main과 origin/main이 다르다면 fetch를 재실행한다.

```bash
uv run --locked python -m local_dev_mcp.tools.workflow pr-body collect \
  --base origin/main --head HEAD > /tmp/local-dev-pr-facts.json

uv run --locked python -m local_dev_mcp.tools.workflow pr-body render \
  --input /tmp/local-dev-pr-facts.json > /tmp/local-dev-pr-body.md

gh pr create --base main --head feat/your-feature --draft \
  --title 'feat(workflow): describe one logical change' \
  --body-file /tmp/local-dev-pr-body.md
```

원하는 의미 설명이 필요하면 선택적 context JSON을 만들고 render에 --context 경로를 준다:

```json
{
  "purpose": "Describe the reason that is not captured by the assessments.",
  "exceptions": [
    {
      "type": "Activation",
      "detail": "Enable repository rules after the first PR integration.",
      "evidence": "docs/adr/0006-native-pr-workflow-mvp.md"
    }
  ]
}
```

evidence는 검토 근거 입력이며 PR 본문에는 반복 출력되지 않는다. 실제 근거 여부를 코드가 의미적으로 확인하지는 않는다.
빈 예외 목록은 빈 섹션을 만들지 않는다. 검증 불가능한 내용은 사실로 기재하지 않는다.

## 기존 PR의 본문 재생성

```bash
uv run --locked python -m local_dev_mcp.tools.workflow pr-body sync \
  --pr 123 --base main --base-ref origin/main
```

sync는 gh pr view에서 PR body·base/head SHA를 조회하고 로컬 원격 추적 Ref(origin/main) 및 HEAD와 일치하는 경우에만 동작한다. --base는 GitHub PR의 대상 브랜치 이름, --base-ref는 로컬 비교 Ref다.
목적과 조건부 예외 영역은 바이트 그대로 유지한다. 자동 생성 영역은 해시로 무결성을 검사하며 외부 편집이 있거나 마커가 손상되면 거부한다.
PR Head가 변경되면 기본적으로 중단하며 목적·예외를 사람이 다시 검토한 경우에만 --accept-new-head를 명시한다.
원격 내용을 다시 읽어 변경 여부를 확인한 직후 gh pr edit --body-file -로 전송한다. GitHub CLI에 원자적인 비교·교체(CAS) 기능은 없으므로 마지막 확인과 쓰기 사이의 경쟁은 완전히 차단하지 못한다.
PR 번호가 확정된 경우 6개 이상의 커밋은 PR Commits 탭 링크로 연결한다.

## 안전 경계

- PR을 생성하거나 편집하는 작업은 명시적 명령으로만 실행한다. 이 CLI는 push·merge·release·ruleset 설정을 수행하지 않는다.
- 수집 대상은 Git에 커밋된 데이터다. 작업 파일의 미커밋 편집은 PR 문서에 반영되지 않는다.
- 원본 Commit의 SHA가 GitHub에 push되지 않은 경우 커밋 URL의 접근성을 증명할 수 없다. PR 생성 후 확인한다.
- 생성기가 받은 TOML 평가의 의미상 정확성은 기존 릴리스 평가·리뷰 계약의 책임이다.
- Review blocker는 정보를 표시하는 종류일 뿐 GitHub 병합 제한을 자동 설치하지 않는다. 실제 병합 제한은 별도 CI·Ruleset 계약이 적용한다.
