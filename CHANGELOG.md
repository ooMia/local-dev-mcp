# Changelog

<!-- towncrier release notes start -->

## [0.2.0]

### major
- **cli-workspace-runtime**: 초기 echo CLI를 workspace 고정 stdio MCP 실행으로 전환

  origin/main의 run은 Hello 출력 뒤 종료했으나 현재는 명시적 설정과 서버 수명을 요구한다. 기존 출력·즉시 종료와 옛 cli/core.server import에 의존하면 비호환이다. 0.x 초기 개발 정책으로 다음 minor를 제안하며 Stable API 파괴를 이미 입증한 것으로 확대하지 않는다.

  Contract: CLI 동작·설정 준비·기존 Python import 경로

  Migration: workspace의 config.toml과 .env를 준비하고 stdout을 MCP 프로토콜 전용으로 사용한다. 실행은 local-dev-mcp run 또는 connect를 사용하고 옛 내부 import 대신 문서화한 진입점·MCP를 사용한다.
### minor
- **agent-pr-publication**: Expose guarded MCP tools for private GitHub feature publication

  Adds opt-in remote read, main fetch, non-force feature push and Draft PR creation using the existing host git/gh authentication; local main and existing release contracts remain unchanged.

  Contract: MCP GitHub remote operations

  Migration: Restart the LOCAL-DEV-MCP server after enabling git, gh and scoped remote permissions in config.toml.
- **chat-pr-lifecycle**: Complete chat-operated PR status and Ready transitions with safe post-squash main acquisition

  Adds exact-head CI observation and independently permissioned Ready transition without changing deferred merge CI enforcement. Previously unobservable remote commits now have an explicit unknown-ancestry state and guarded object acquisition before tracking updates.

  Contract: MCP GitHub PR lifecycle and remote-main observation

  Migration: Restart server and refresh chat tool discovery. Enable gh.pr.ready explicitly for Ready transitions. Remote ancestry fields can now be null when the observed remote commit is not yet local; fetch before treating them as booleans.
- **deterministic-pr-merge**: Share deterministic PR squash messages and actual-result verification across CLI and MCP

  Closes the gap after Draft PR publication with independent opt-in merging, reviewed head/message binding and readback instead of trusting GitHub defaults or request completion.

  Contract: GitHub PR merge messages and result observation

  Migration: Restart the server to discover the new tools; explicitly enable gh.pr.merge only when remote merging is intended. CLI merge-pr now reports structured verification and exits nonzero for unconfirmed or mismatched results.
- **development-release-workflow**: 명시적 영향 평가가 필요한 squash와 누적 릴리스 도구 추가

  새 개발 CLI와 opt-in 통합 검사를 추가한다. 미설정 workspace의 fast-forward는 유지하며 설정한 workspace는 새 squash commit SHA·mode·평가 ID를 받는다. 실제 호환성 분류는 명시적 판단이며 tool type에서 추론하지 않는다.

  Contract: 개발 CLI와 opt-in Git 통합 결과
- **document-diagnosis**: 등록한 문서 Guide의 읽기 전용 진단 제공

  새 MCP 진단을 추가한다. 관계 미승인의 정상 Blocked 결과를 명시하며 내부 Document Core가 Stable Python API가 됐다고 선언하지 않는다.

  Contract: 문서 진단 MCP; 내부 Core는 Experimental
- **elicitation-diagnostics-probes**: Add diagnostic-only MCP elicitation capability and multi-round probes

  New read-only capability diagnostics and no-op Form/MRTR probes report negotiated client support, but cannot authorize Git operations or replace explicit main integration approval. Existing MCP write permissions and merge contracts remain unchanged.

  Contract: MCP diagnostic elicitation tools and protocol capability reporting

  Migration: Restart the MCP server to discover diagnostic tools. Only clients declaring the corresponding Form capability can exercise that probe; unsupported clients receive a blocked result.
- **git-commit-history**: Expose bounded read-only Git commit history for source-preserving PR reconstruction

  The origin main is an ancestor of 17 unpublished local-main commits and 10 subsequent feature commits. Existing comparison tools omit individual source commit IDs, preventing safe feature-scoped reconstruction without arbitrary shell execution. Add exact-SHA-validated read-only commit title and parent listing with a fixed 64-commit limit.

  Contract: Local Git MCP commit history
- **guarded-git-capabilities**: 관측 HEAD에 결합한 Git 상태·index·commit·branch/ref 도구 추가

  기준 scaffold에 없던 로컬 Git MCP를 추가한다. 서명과 부분 실패, 보호 경로의 공개 제한을 계약으로 정의한다. 코드 존재를 원격 GitHub 동작·push 성공으로 해석하지 않는다.

  Contract: 로컬 Git MCP와 선택적 OpenPGP 서명
- **isolated-pr-worktrees**: 격리형 Worktree 준비와 지정 브랜치 원격 게시 도구를 추가한다.

  원본 checkout을 전환하지 않고 과거 기능 브랜치를 검증하여 게시할 수 있도록 준비 상태 조회와 기존 원격 검사 재사용 경로를 제공한다.

  Contract: MCP managed worktree preparation and branch publication

  Migration: 새 도구와 git.worktree.write 권한은 MCP 서버 재시작 후 적용된다. 관리 경로 .lab/worktrees는 Git에서 ignore되어야 한다.
- **issue-8-monorepo-contract**: 모노레포 레시피 실행 위치와 대상 경로, Git 변경 검토·보호 커밋 및 관리 Worktree 검증을 개선한다.

  기존 Git workspace와 명령 권한을 유지하면서 선택적 recipe cwd 및 recipe-relative targets를 추가한다. 기존 cwd 미지정 작업은 유지되며, staged 범위 검증과 untracked 미리보기는 opt-in이다. 관리 Worktree의 원본 변경 보존과 로컬 이력 통합도 명시적 검증으로 확장한다.

  Contract: MCP task recipes, Git guarded commits and managed worktree

  Migration: 모노레포 작업에 cwd를 명시한 경우 targets/default_targets는 해당 recipe cwd 상대경로다. 설정 변경이나 새 MCP 입력을 사용할 때는 서버를 재시작한다.
- **local-history-consolidation**: 원본 snapshot을 보존해 별도 로컬 후보 브랜치에 기능 단위 이력을 구성하는 MCP 도구를 추가한다.

  현재 checkout과 main을 이동하지 않고 검토한 원본 구간을 서명된 커밋으로 합쳐 로컬 개발 이력을 정리할 수 있다. 후보 생성은 품질 승인이나 원격 게시를 수행하지 않는다.

  Contract: MCP local history consolidation
- **managed-worktree-finalization**: 병합 결과가 검증된 관리형 Worktree를 안전하게 종료하고 선택적으로 로컬 topic 브랜치를 삭제한다.

  기존 병합·준비 계약을 변경하지 않고, 정확한 PR 출처·HEAD·브랜치·Git 등록·작업 상태·ignored 파일을 확인한 후 명시적 종료만 허용한다. 외부에서 Worktree를 정상 제거해 남은 고아 준비 기록도 제한적으로 정리할 수 있다.

  Contract: MCP managed worktree finalization

  Migration: 새 finalize_project_worktree 도구를 이용하려면 MCP 연결을 다시 시작한다. 브랜치 삭제는 delete_branch=true를 명시한 경우만 수행한다.
- **opt-in-worktrunk-integration**: Add opt-in Worktrunk checkout, inspection, review and cleanup workflows

  New MCP tools and optional backend=worktrunk paths reuse existing Git registration, permissions, task quality and remote publication gates. Native worktree tools and default backend values remain available. Removal is fail-closed before and after non-force Worktrunk execution, and read-only plans do not approve deletion or merging.

  Contract: MCP Worktrunk opt-in lifecycle, Git worktree review and task backend

  Migration: Restart the MCP server to discover new tools. Install a compatible wt executable and explicitly enable wt and git.worktree.write for CLI-backed write operations. Existing Native callers require no migration; run_hooks remains opt-in.
- **policy-aware-preflight**: main 통합 선행 조건과 정책 참조를 읽기 전용 MCP로 진단한다.

  기존 Git 쓰기 및 PR 병합 계약은 유지하면서, 정확한 로컬 HEAD·브랜치·조상·평가의 충족 여부와 별도로 확인해야 할 원격 PR 및 CI 조건을 구분한다. 사전 평가는 승인이나 권한을 부여하지 않는다.

  Contract: MCP Git main integration policy preflight

  Migration: 새 inspect_project_policy 도구를 사용하려면 MCP 연결을 재시작한다. PR·CI 확인과 최종 병합은 기존 도구와 정책을 따른다.
- **pr-body-generator**: Generate concise PR bodies from Git history and change assessments

  Adds a developer CLI for deterministic PR metadata, preserved reviewed context and guarded PR body synchronization without changing the existing release calculation.

  Contract: Developer workflow
- **release-mcp-capabilities**: Chat에서 릴리스 평가 요약과 검증된 버전 준비를 실행하는 MCP 도구 추가

  커밋된 평가·정확한 HEAD와 Digest로 권장 버전을 제시하고 명시적 uv.release 권한 및 clean release 브랜치에서 준비 파일을 작성한다. 기존 Release CLI의 계산·검증 로직을 공유하며 기존 MCP·CLI 계약은 유지한다.

  Contract: 공개 MCP get_release_plan 및 prepare_project_release, 설정 명령 권한 uv.release
- **shared-quality-profiles**: Expose shared quality check profiles for Worktrunk and registered task recipes

  Adds opt-in named verification profiles and evidence binding without removing existing lint, format or test recipes or weakening their scoped permissions. A profile identifies required check names and does not grant execution or merge approval.

  Contract: MCP task catalogue quality profiles and Worktrunk verification evidence

  Migration: Restart the MCP server when enabling shared profile metadata. Existing task calls remain supported; new Worktrunk quality verification requires a fresh set of completed, exact-source full-scope jobs.
- **workflow-mvp**: Add PR integration, main quality guards and artifact metadata checks

  Adds an optional developer workflow CLI while preserving existing MCP and release assessment contracts.

  Contract: Developer workflow
- **workspace-and-task-capabilities**: 관측 참조 기반 Workspace 파일 도구와 등록 CLI 작업 추가

  초기 비교 기준에 존재하지 않았던 탐색·읽기·관측 수정·생성·삭제와 작업 실행/취소/진단 기능을 추가한다. 권한·receipt·부분 결과는 새 공개 계약으로 명시한다. 기존 CLI 실행 의미의 비호환은 별도 기록으로 평가한다.

  Contract: MCP Workspace 및 CLI 작업 입력·결과·권한
- **worktree-assessments**: 격리 Worktree의 명시적 평가 작성과 Digest 기반 커밋 도구를 추가한다.

  기존 checkout과 공개 호출 계약을 유지하며 과거 기능 브랜치에 검토한 평가 하나를 기록·커밋하는 선택적 MCP 기능을 추가한다.

  Contract: MCP Git managed worktree assessment
- **worktree-task-execution**: 관리 Worktree에서 등록된 검사 작업을 실행하는 MCP 도구를 추가한다.

  기존 작업 실행·조회·취소 계약과 단일 실행 제한을 유지하면서 정확한 브랜치·SHA에 결합된 선택적 검사 경로와 Git 상태 검증 결과를 추가한다.

  Contract: MCP managed worktree checks
### patch
- **gh-auth-diagnostics**: Restore GitHub CLI authentication and add opt-in token injection for Tunnel

  The Tunnel connection returned gh exit 4 despite forwarding configuration paths, and gh recreated state files in the workspace. Restore the OS account home, reject relative auth paths, expose non-secret diagnostics and optionally forward only a GH_TOKEN explicitly supplied in the protected workspace .env, without forwarding ambient token variables.

  Contract: MCP GitHub authentication environment and diagnostics

  Migration: Restart the LOCAL-DEV-MCP connection to expose diagnose_project_remote_auth and the safer host account environment.
- **gh-token-shell-inheritance**: Allow explicitly exported GH_TOKEN to authenticate Tunnel-hosted GitHub CLI

  The Tunnel boundary accepted a GH_TOKEN only from workspace .env, blocking the existing host pattern of exporting a token obtained with gh auth token. Reuse the explicitly exported GH_TOKEN in the child, retain protected .env precedence, and keep unrelated ambient credentials isolated.

  Contract: Authenticated private GitHub publication via LOCAL-DEV-MCP Tunnel

  Migration: Start the updated LOCAL-DEV-MCP connect command in the same shell as export GH_TOKEN; no GitHub plugin permission or token file is required.
- **historical-validation-profiles**: 이력 게시 대상의 당시 검사 범위를 실행할 고정 recipe를 추가한다.

  루트 conftest.py와 테스트 marker 도입 이전 브랜치가 현재 recipe에서 실패하거나 테스트를 전부 제외하는 실제 문제를 수정한다. 기존 recipe와 MCP 호출 계약을 유지하며, 과거 검사 문서에 대응하는 명시적 프로필을 추가한다.

  Contract: Repository task configuration for historical publication
- **project-owned-hooks**: Activate project-owned Git hooks despite user hook-path settings

  Fixes optional workflow installation and adds activation diagnostics without changing existing MCP inputs or release calculations.

  Contract: Developer workflow
- **scoped-gh-git-credentials**: Bind authenticated GitHub CLI credentials to scoped Git remote operations

  Private GitHub API access succeeded but Git ls-remote failed without a usable HTTPS credential helper. Configure gh auth git-credential only for bounded ls-remote, fetch and push invocations, without editing host gitconfig, and distinguish API from Git transport readiness in diagnostics.

  Contract: Guarded GitHub publication and authentication

  Migration: Restart the LOCAL-DEV-MCP connection to load scoped Git transport authentication and the expanded remote auth diagnostic.
- **tunnel-gh-auth-context**: Preserve host GitHub CLI configuration paths across Tunnel launch

  The isolated Tunnel child omitted HOME and gh configuration locations; remote inspection returned gh exit 4 and an untracked gh state file appeared. Forward only non-secret account-config locations while retaining token isolation.

  Contract: MCP Tunnel and GitHub authentication environment

  Migration: Restart the existing LOCAL-DEV-MCP Tunnel connection; no GitHub plugin permission change or new credential is required if host gh is already authenticated.
### none
- **actions-unit-tests**: GitHub Actions의 테스트 범위를 unit으로 제한하고 정적 검사는 유지한다.

  CI 실행 정책만 변경한다. 공개 MCP·CLI·설정 계약과 로컬 전체 검사·릴리스 검증의 동작은 유지한다. CI 성공은 통합·설치·릴리스 검증 성공을 뜻하지 않는다.

  Contract: Development CI validation scope
- **current-policy-map**: 현재 적용 정책과 부분 대체·일회성 예외를 작업별 문서 지도로 정리한다.

  ADR 원본과 실행·릴리스 계약은 변경하지 않고 루트 에이전트 지침의 중복 정책 링크를 소유 문서 인덱스로 모은다. 문서 탐색만 개선하며 사용자 API·권한·버전 영향은 없다.

  Contract: Documentation policy routing
- **documentation-link-validation**: 문서 전체의 상대 링크·앵커와 ADR 인덱스 누락을 회귀 검사한다.

  기존 고정 문서 목록의 누락을 없애고 docs 내 모든 Markdown과 주요 루트 안내의 링크 경로·앵커·ADR 등록을 검증한다. 런타임 기능·권한·사용자 API와 패키지 호환성에는 변경이 없다.

  Contract: Documentation integrity test coverage
- **experimental-delivery-and-test-evidence**: Experimental Document/Event/GitHub 내부 모델과 실패 경계 검증 정리

  기준 scaffold에 없던 실험적 내부 모델과 로컬 검증을 도입한다. GitHub semantic MCP 등록·live 외부 Apply나 안정된 라이브러리 API는 제공하지 않으므로 그 공개 계약의 변경으로 분류하지 않는다. 새 공개 문서 진단은 별도 minor 기록으로 평가했다.

  Contract: Experimental 내부 모델·로컬 테스트·개발용 PostgreSQL
- **graft-local-cache-ignore**: 로컬 Graft 그래프 캐시를 Git 이력에서 제외하고 검색용 .ignore 규칙을 기록한다.

  재생성할 수 있는 graft/ 산출물을 커밋 대상에서 제외하며 소스·MCP·CLI·사용자 구성 계약과 패키지 버전에는 영향을 주지 않는다. 캐시 파일은 로컬에 그대로 남기고 검색 규칙만 추가한다.

  Contract: Developer local Graft cache and search configuration
- **initial-baseline-preparation**: 초기 기준선 게시 정책과 평가 메타데이터를 정합화하고 Git 명령 배치를 개발 규약에 맞춘다.

  공개 MCP 호출 계약·권한·동작과 버전은 유지한다. 사용자 승인된 최초 기준선에서만 잘못된 평가의 누락 메타데이터를 보완하며, 일반 평가 불변성·PR 통합·hook 검사는 완화하지 않는다.

  Contract: Development workflow and initial baseline publication


