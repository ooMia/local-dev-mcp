# Workflow MVP Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline.

**Goal:** 기존 도구로 기능별 squash, PR 출처, 품질 게이트와 배포물 검사를 구현한다.
**Architecture:** config.toml의 검사 recipe를 공통 CLI와 Git hooks/CI가 재사용한다.
**Tech Stack:** Python 표준 라이브러리, 기존 uv/Ruff/Pyright/pytest, Git, GitHub CLI.
**Spec:** [ADR 0006](../../adr/0006-native-pr-workflow-mvp.md).

## Global Constraints

- 새 runtime/development dependency를 추가하지 않는다.
- 기존 명시적 TOML 평가와 누적 릴리스 계산을 유지한다.
- 사용자 hook을 덮어쓰거나 main을 변경하지 않는다.
- 원격 설정의 배포와 로컬 구현 검증을 구별한다.

## Review Focus

- 작업 파일과 index가 다르거나 검사 중 바뀌면 main 갱신을 거부한다.
- 검증되지 않은 ref 변경과 오래된 검사 기록을 거부한다.
- linked worktree는 공통 hook과 검증 기록을 사용한다.
- PR head가 바뀌면 기존 요청으로 병합하지 않는다.
- 실제 wheel/sdist의 내부 평가 파일 유입을 거부한다.

## Tasks

- [x] CLI·실제 Git hook 테스트를 먼저 실패시키고 검사 실행·출처 검증을 구현한다.
- [x] main ref guard의 실패를 확인하고 기준 SHA/tree에 결합한 검증 기록을 구현한다.
- [x] 실제 배포 archive와 conditional GitHub merge 호출을 검증한다.
- [x] 기존 CI에 공통 검사·배포물 검사를 연결하고 native ruleset 입력을 제공한다.
- [x] ADR·운영 계약을 정합화하고 전체 lint/typecheck/unit/integration을 통과한다.

## Execution ledger

- Ruling: 모든 main tree에서 평가 파일을 금지하는 보수적 제안은 채택하지 않는다.
  기존 릴리스 소비와 artifact 제외로 요구를 구현하고 기존 평가 스키마 재작성을 피한다.
- Ruling: 현재 연결은 단일 workspace의 파일/Git/등록 recipe만 제공한다.
  독립 feature branch에서 작업하며 main을 유지한다. 임의 명령이나 테스트로 이 제약을 우회하지 않는다.
- Baseline: 492 unit tests passed; 기존 dependency deprecation warning 1건.

- Review: 임시 Git index 전달과 assume-unchanged/skip-worktree 거부를 실제 실패 테스트로 고쳤다.
  global core.hooksPath도 탐지하며 기존 사용자 hook을 보존한다. 재검토에서 추가 blocker 없음.
- Ruling: 실제 전체 integration이 89.61초, 최종 147.45초로 MCP Git 쓰기 제한 60초를 넘었다.
  approve-main --squash에서 먼저 품질을 검사하고 통합 때 정확한 base/tree 결과를 재사용한다.
  기존 MCP timeout·권한은 변경하지 않는다.
- Final validation: Ruff lint/format check, Pyright 0 errors;
  unit 494 passed; integration 521 passed, 81 skipped; 실제 uv wheel/sdist 검사 포함.
  기존 dependency deprecation warning은 유지된다.
- Activation pending: 연결된 MCP는 hook 설치·gh·push·임의 CLI 실행을 제공하지 않는다.
  테스트를 통해 실제 workspace에 설치하는 우회는 하지 않았다.
  실제 main·원격 설정을 변경하지 않았고 원격 수용 검증을 완료했다고 주장하지 않는다.
