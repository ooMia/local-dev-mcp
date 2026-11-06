# 격리형 Worktree와 브랜치 게시

이 기능은 [로컬 Git 계약](README.md)과 [원격 PR 계약](remote-pr.md)을 확장한다.
기존 브랜치를 별도 linked Worktree에 준비하고 그 Worktree에서 원격 작업을 실행한다.
원본 checkout은 전환하지 않으며 원본 브랜치·HEAD·인덱스와 clean 상태를 전후 확인한다.
Git hooks·필터나 다른 프로세스의 부작용을 OS 수준으로 차단하거나 복구하지는 않는다.

## 도구와 입력

| 도구 | 입력 | 권한 |
| --- | --- | --- |
| `prepare_project_worktree` | `branch`, `expected_branch_head`, `expected_branch`, `expected_head` | `git`, `git.worktree.write` |
| `inspect_project_worktree` | `branch` | `git` |
| `start_project_worktree_task` | 준비 입력 4개, `task`, `request_id`, `targets=null`, `all_tests=false` | `git`, `git.worktree.write`, 선택 recipe 권한 |
| `prepare_project_worktree_assessment` | 준비 입력 4개, `assessment_id`, `assessment_toml` | `git`, `git.worktree.write` |
| `commit_project_worktree_assessment` | 준비 입력 4개, `assessment_id`, `expected_digest`, `message` | 시작 commit 권한, `git.worktree.write` |
| `inspect_project_branch_remote` | `branch`, `expected_branch_head` | `git`, `gh`, `git.remote.read` |
| `push_project_branch` | 준비 입력 4개, `expected_remote_main`, `expected_remote_feature=null` | `git`, `gh`, `git.remote.write` |
| `create_project_branch_draft_pr` | 준비 입력 4개, `expected_remote_main`, `title_text`, `purpose=null`, `exceptions=null` | `git`, `gh`, `gh.pr.create` |

`branch`는 게시할 기존 topic 브랜치이고 `expected_branch_head`는 그 전체 SHA다.
`expected_branch`·`expected_head`는 원본 checkout의 현재 브랜치와 전체 SHA다.
Worktree 준비·평가 작성·원격 쓰기는 두 checkout 모두 clean해야 실행한다.
평가 커밋은 아래 절차에서 정한 새 평가 하나만 대상 변경으로 허용한다. `main`은 준비 대상이 될 수 없지만 원본 checkout일 수 있다.
새 브랜치가 필요하면 먼저 `create_project_branch_from_ref`로 확인된 원본 SHA에서 생성한다.

## 준비와 관측

경로는 서버가 `.lab/worktrees/` 아래에 결정하며 호출자가 임의 경로를 지정할 수 없다.
원본 Git의 ignore 규칙이 Worktree와 준비 기록을 모두 제외해야 한다.
부모·대상·기록 경로의 symlink, 기존 경로, 다른 Worktree의 브랜치 점유, SHA 변경은 거부한다.
현재 정책의 허용·금지 경로와 checkout 경로를 검사하고 보호 이름의 tracked 파일은 materialize하지 않는다.

준비 기록은 checkout 전에 생성하고 checkout 검증 후 완료로 표시한다.
기존 경로를 자동 채택하거나 덮어쓰지 않는다. 같은 요청을 다시 보내도 준비를 반복하지 않으며
응답을 잃었으면 먼저 `inspect_project_worktree`를 호출한다.

| state | 의미 | 다음 행동 |
| --- | --- | --- |
| `absent` | 대상 경로·준비 기록·Git 등록이 없음 | 관측한 브랜치와 SHA로 준비 |
| `partial` | 일부 파일/기록/등록만 남거나 연결·브랜치·Git 작업 상태가 일치하지 않음 | 남은 상태를 조사; 자동 재시도·삭제 금지 |
| `ready` | 완료 기록·Git 등록·공통 저장소·양방향 Git 연결·브랜치가 일치 | `clean=true`와 정확한 HEAD를 확인 후 원격 조회 |

`ready`만으로 게시 가능함을 의미하지 않는다. 수정된 Worktree는 `clean=false`다.
준비·게시 실패는 일부 적용됐을 수 있으며 자동 reset·stash·prune·Worktree 제거를 하지 않는다.
준비 기록은 인증이나 사용자 승인 토큰이 아니다. 같은 OS 계정이 기록·Git 메타데이터를 수정하는 행위까지 방어하지 않는다.

## 게시 절차와 실행 경계

1. 원본 status와 대상 local ref의 전체 SHA를 관측한다.
2. Worktree를 준비하고 상태를 조회한다.
3. `inspect_project_branch_remote`로 대상 브랜치의 실제 원격 refs와 기준선 관계를 확인한다.
4. 기존 검증·평가 계약을 충족한 대상에만 `push_project_branch`를 호출한다.
5. `create_project_branch_draft_pr`로 게시하고 반환된 SHA·URL을 확인한다.

Push의 source는 mutable HEAD 대신 관측한 전체 SHA다. 원격 목적지는 동일 이름의 topic ref로 고정한다.
원격 main·tracking main·미공개 local main 혼입·비강제 Push·중복 PR 검사는 기존 원격 계약을 재사용한다.
PR 생성 후에는 이력 길이와 관계없이 실제 base/head/body 및 Draft 상태를 확인한다.
게시 중 Git/gh와 hooks는 대상 Worktree를 실행 위치로 사용한다.

실행 설정·권한·환경은 서버 시작 snapshot에서 가져온다. 과거 Worktree의 config.toml이나 .env를 다시 읽지 않는다.
인증 파일·비밀 환경 파일을 Worktree에 복사하지 않는다. PR 템플릿은 현재 원본 checkout에서 읽고,
평가·커밋은 대상 SHA에서 수집한다. 템플릿의 정책 위반·symlink는 거부한다.

## Worktree 검사 실행

`start_project_worktree_task`는 현재 서버의 [등록 작업](../tasks/README.md)을 준비된 Worktree에서 실행한다.
목록은 `list_project_tasks`, 결과 조회·취소는 기존 `get_project_task`·`cancel_project_task`를 사용한다.
원본 작업과 Worktree 작업은 같은 단일 실행 슬롯과 request 장부를 공유한다.
실행 파일·recipe·환경은 서버 시작 snapshot을 유지하며 대상 `.env`·`config.toml`을 다시 읽지 않는다.
호출자는 임의 경로·명령·옵션을 지정할 수 없다. 이번 인터페이스에서는 `kind="format"` 작업을 거부한다.

원본·대상은 시작 시 clean하고 관측한 SHA여야 한다. 제출 준비와 프로세스 시작 직전에 상태를 검사한다.
종료·취소 뒤 대상 branch/HEAD/clean 상태와 원본 branch/HEAD/index/clean 상태를 다시 확인한다.
프로세스가 종료돼도 검증이 진행 중이면 job은 `running`이고 슬롯을 유지한다.
test selector는 대상 Worktree 상대 경로이며 매핑된 정책과 실제 물리 경로의 접근 정책을 모두 검사한다.

Worktree job 결과에는 `worktree`가 추가된다. `branch`, `head`, `original_branch`, `original_head`,
원본 상대 `path`, `verified`, `verification_error`를 제공한다.
`verified=true`는 해당 Git 상태 검사 성공이며 테스트 수·성공이나 의미상 호환성을 보장하지 않는다.
`null`은 검증 전, `false`는 검사 실패·중단이다. 종료 코드 0이어도 Git 검증 실패는 `succeeded`로 보고하지 않는다.
진단은 기존 마스킹·보관 상한을 따른다. 외부 recipe가 파일을 바꿀 수 있으며 실패 시 자동 복원하지 않는다.

같은 request ID·작업·선택·네 개 ref 입력을 재제출하면 원래 job을 반환한다.
완료 후 ref가 이동했어도 과거 영수증을 재생하며 현재 상태를 재검증하거나 작업을 다시 실행하지 않는다.
새 검사가 필요하면 현재 refs를 관측하고 새 request ID를 사용한다.
검사 성공을 원격 Push·Draft PR 또는 Hook 품질 영수증의 생성으로 해석하지 않는다.

## 명시적 평가 작성과 커밋

평가 내용의 의미와 TOML 필드는 [릴리스 평가 계약](../release/README.md#설정과-평가-기록)을 따른다.
도구는 호환성을 추론하거나 다른 변경의 평가를 자동 복사·배정하지 않는다.
대상에 새 committed `.changes` 평가가 없으면 Draft PR 생성을 거부한다.

1. 원본 status와 대상 SHA를 관측한 뒤 `prepare_project_worktree_assessment`에 검토한 ID·TOML을 전달한다.
2. 반환된 `path`를 `read_workspace_file`로 읽어 원문을 검토한다. `digest`는 해당 UTF-8 bytes의 SHA-256이다.
3. 같은 원본/대상 SHA, ID, Digest와 메시지로 `commit_project_worktree_assessment`를 호출한다.
4. 반환된 `head_after`, `signing_requested`, `signature_verified`를 확인하고 새 SHA로 원격 상태를 재조회한다.

작성은 기존 경로를 덮어쓰지 않고 `.changes/<id>.toml` 하나만 만든다. Stage·commit은 하지 않는다.
ID는 최대 128자의 소문자·숫자·하이픈 구분자이며 입력은 UTF-8 16 KiB 이하로 제한한다.
기존 ID와 대상 HEAD에 이미 존재하는 동일한 구조의 평가를 다른 ID로 중복 등록하는 것을 거부한다.
이 초기 평가 인터페이스는 `supersedes=[]`만 허용한다. 기존 평가의 정정은 별도 흐름에서 수행한다.
서로 다른 판단이 의미상 중복인지는 자동 판정하지 않으며 다른 브랜치의 평가 소유권도 추론하지 않는다.

두 호출 모두 원본·매핑된 대상의 경로 정책을 검사한다. Symlink·실행 가능한 평가 파일·Git ignore 경로는 거부한다.
Commit은 새 평가 하나가 대상의 유일한 변경일 때만 진행한다. 기존 staged/untracked 변경,
Digest 불일치, Git filter로 변한 staged bytes·파일 모드는 거부한다.
시작 시 준비한 identity·서명 설정을 재사용하고 메시지에 `Assessment-Source: <평가 전 SHA>`를 남긴다.
실패 시 unsigned로 대체하거나 hooks를 우회하지 않는다.
완료 뒤 부모 SHA·단일 변경 경로·committed bytes·파일 모드와 clean 상태를 확인한다.

실패·응답 유실 후에는 파일만 생성됐거나 staged 변경 또는 commit까지 남을 수 있다.
`inspect_project_worktree`와 반환 경로의 원문을 다시 조회하며 이전 쓰기를 자동 반복하지 않는다.
Hook이 commit 내용을 바꿨다면 검증이 거부하더라도 그 commit을 자동 취소하지 않는다.
이 검사는 동시 OS 작업을 잠그거나 Git hook을 sandbox하지 않는다.

일반 파일 편집과 `start_project_task`의 workspace는 원본 checkout으로 유지된다.
Worktree 일반 편집과 제거는 이번 인터페이스에 포함하지 않는다.

원격 사전 관측과 GitHub 생성 사이에는 원자적 transaction이 없다.
관측 뒤 원격 feature가 source의 조상으로 진전되면 정상 fast-forward Push가 성공할 수 있다.
비강제 Push는 원격 이력을 덮어쓰지 않으며 결과가 불확실하면 refs/PR을 재관측한다.
GitHub Ruleset·Ready·병합을 변경하지 않는다.
