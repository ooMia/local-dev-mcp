# Workspace 파일 탐색·읽기·변경 계약

지정한 로컬 workspace를 탐색하고 UTF-8 파일을 생성·수정·삭제한다. Git·identity·시스템 바이너리 권한 없이 사용할 수 있다.
공통 정책·필수 설정 파일의 시작 조건과 준비 상태는 [설정 계약](../../configuration.md)을 따른다.
구간 수정·삭제는 기존 UTF-8 일반 파일을 대상으로 한다. 생성은 기존 경로를 덮어쓰지 않는다.
lint·format·test 실행은 별도의 [CLI 작업 도구](../tasks/README.md)를 사용한다.

## 내부 Python Snapshot Reader

`tools.workspace.SnapshotReader(policy)`는 기능 모듈이 사용하는 **읽기 전용 Python API**다.
MCP 도구나 저장소 승인 기능을 추가하지 않는다. `read(relative)`는 Workspace의
경로 허용·거부, 일반 파일, symlink 차단 및 8 MiB 상한을 적용하고 원본 바이트를
`SnapshotObservation`으로 반환한다. `verify(observation)`는 동일 파일을 다시
관측하여 내용과 파일 상태가 다르거나 재관측할 수 없으면 `SnapshotChanged`를 발생시킨다.
이 API는 원자적인 OS Snapshot이나 외부 파일 변경 방지 잠금을 제공하지 않는다.

## 도구와 입력

| 도구 | 입력 | 용도 |
| --- | --- | --- |
| `list_workspace_entries` | `path="."`, `offset=0`, `limit=100` | 바로 아래 항목의 이름순 목록 |
| `find_workspace_paths` | `pattern`, `path="."`, `limit=100` | 재귀 파일 경로 검색 |
| `search_workspace_text` | `query`, `path="."`, `output="paths"`, `case_sensitive=true`, `limit=100` | 재귀 literal 내용 검색 |
| `read_workspace_file` | `path`, `start_line=1`, `max_lines=200` | 완전한 줄 범위 읽기 |
| `replace_workspace_file` | `path`, `read_ref`, `replacement` | 관측한 구간 전체를 새 문자열로 교체 |
| `create_workspace_file` | `path`, `content`, `request_id`, `create_parents=false` | 기존 경로를 덮어쓰지 않는 UTF-8 파일 생성 |
| `delete_workspace_file` | `path`, `read_ref`, `request_id` | 관측한 전체 파일 버전을 확인한 단일 파일 삭제 |

입력은 평평한 JSON이며 선언한 JSON 타입만 받는다. 자동 형변환·추가 필드·누락 경로의 임의 보정은 없다.
path는 workspace 상대 경로다. `.`은 root이며, 파일 변경 대상은 일반 파일이고 목록·검색 대상은 디렉터리다.
내부의 `.`·`..`는 정규화할 수 있지만 workspace 밖으로 나가는 경로·절대 경로·NUL은 거부한다.
symlink를 지난 뒤 `..`로 되돌아오는 표현도 허용하지 않는다.
생성·삭제는 요청 지문을 현재 파일 상태와 독립적으로 비교하기 위해 `..` 구성 요소를 위치와 무관하게 거부하며,
입력 경로는 최대 1024자다. 정규화한 상대 경로를 사용한다.

구조를 모르면 한 단계 목록을 사용하고, 파일명·확장자를 알면 경로 검색으로 좁힌다.
내용 검색은 기본적으로 일치 파일 경로만 반환한다. 원문이 필요할 때 `output="content"` 또는 범위 읽기를 선택한다.
내용 검색은 비어 있지 않은 단일 줄 literal이며 정규식·여러 줄 일치는 지원하지 않는다.
한 줄에 여러 번 일치해도 content 결과는 그 줄 하나다.

경로 pattern은 path 기준의 상대 경로에 대소문자를 구분해 적용한다.
`*`, `?`, `[]`, `**`를 지원하며 `**/*.py`는 바로 아래와 하위 폴더의 Python 파일을 찾는다.
쉘·정규식·중괄호 확장은 하지 않는다. 결과는 일반 파일 경로이며 디렉터리 구조는 목록으로 확인한다.

## 접근과 탐색 범위

어느 깊이든 `.git`, `.env`, `.env.*`를 보호하며 정확한 이름 `.env.example`만 예외다.
대소문자만 바꾼 보호 이름도 거부해 대소문자를 구분하지 않는 파일시스템에서 우회하지 못하게 한다.
목록·검색은 보호 항목을 제외하고, 직접 접근은 내용을 열기 전에 거부한다.
다른 이름의 소스에 들어 있는 secret을 자동 탐지·마스킹하는 기능은 아니다.

각 대상은 [경로별 권한](../../configuration.md#파일-접근-정책)을 적용한다. 허용된 하위 경로에 도달하는 데 필요한
조상 디렉터리 이름은 탐색할 수 있지만, 그 안의 다른 파일을 읽을 권한을 주지는 않는다.
루트 `config.toml`과 모든 `.gitignore`의 직접 수정은 기본적으로 금지한다.
사용자가 시작할 때 `--allow-policy-edits`를 명시하면 이 두 종류의 기본 쓰기 금지만 해제한다.
별도의 읽기 전용·ignore·접근 금지와 모든 관측·경로 검사는 유지한다. [설정과 적용 시점](../../configuration.md#정책-파일-편집)을 따른다.

symlink는 목록에 종류만 표시하며 링크 자체나 중간 링크를 통한 내용 접근·순회는 하지 않는다.
FIFO·장치 등 일반 파일이 아닌 대상도 읽지 않는다. Git이 링크 자체를 entry로 다루는 동작은 별개다.
경로는 workspace와 결합하고 실제 접근 직전에 다시 검사한다. 이 검사는 OS sandbox나 외부 writer의 배타 잠금이 아니다.
이름 규칙은 다른 이름으로 복제된 내용이나 모든 파일 별칭의 원본을 추적하는 기능이 아니다.

탐색은 workspace 루트와 하위 `.gitignore`를 호출마다 읽는다. 상위 workspace 밖·global exclude·`.git/info/exclude`는 읽지 않는다.
하위 규칙의 우선순위와 재포함을 적용하되 제외된 디렉터리에는 들어가지 않는다. 보호·symlink 규칙이 우선한다.
Git index를 읽지 않으므로 이미 추적 중인 파일도 패턴에 맞으면 탐색에서 제외될 수 있다.
보호 대상이 아닌 ignored 파일은 정확한 경로로 직접 읽을 수 있다.
최종 ignore 규칙에 해당하는 파일은 직접 수정에서는 읽기 전용이다. 단순히 패턴에 언급되었는지가 아니라
하위 규칙·재포함·부모 디렉터리 제외까지 적용한 결과로 판단한다. 읽기와 수정은 같은 ignore 해석을 공유한다.
서버는 허용된 하위 경로의 정책을 판단하는 데 필요한 `.gitignore`를 내부에서 읽을 수 있으며 그 원문을 결과에 포함하지 않는다.
ignore의 읽기·형식 오류는 조용히 필터를 끄지 않고 호출 오류로 반환한다.

## 결과와 제한

탐색·읽기 결과에는 `project`, `observed_at`, `complete`, `reason`, `next_action`이 있다.
complete는 해당 호출의 필터와 범위 안에서 확인을 마쳤다는 뜻이며 workspace 전체나 실행 성공을 보장하지 않는다.

| 도구 | 추가 결과 |
| --- | --- |
| 목록 | `path`, `entries`(상대 path·file/directory/symlink/other kind), `next_offset` |
| 경로 검색 | `path`, `pattern`, `paths` |
| 내용 검색 | `path`, `output`, `paths`, `matches`(path·line·preview·preview_truncated), `skipped` |
| 읽기 | `path`, `content`, `start_line`, `end_line`, `total_lines`, `next_start_line`, `version`, `read_ref` |

내용 검색의 paths와 matches 중 선택한 output에 해당하는 목록만 채운다.
skipped는 binary·encoding·too_large·io·special별 건너뜀 개수이며 파일별 원문 오류 목록은 제공하지 않는다.
보호·ignore·symlink는 선언한 탐색 범위에서 제외한다. 일반 파일의 바이너리·비UTF-8·크기 초과는 건너뜀으로 요약한다.
개별 접근 실패나 작업 예산 소진은 complete=false로 표시하며, 일치가 없어도 부재의 증거가 아니다.
시작 경로 자체의 접근 실패·잘못된 인자·ignore 오류는 호출 전체의 안전한 MCP 오류다.

초기 상한은 다음과 같다. 새 설정 항목 없이 적용하며 정확한 전체 일치 수는 계산하지 않는다.

| 대상 | 상한 |
| --- | --- |
| 목록·경로 검색·paths 내용 검색 | 기본 100개, 최대 200개 |
| content 내용 검색 | 최대 100개 일치 줄, preview는 줄별 UTF-8 512 bytes |
| 읽기 | 기본 200줄, 최대 500줄, 본문 UTF-8 16 KiB |
| 읽기·내용 검색의 파일 | 8 MiB |
| 수정의 replacement | UTF-8 16 KiB, NUL 금지 |
| 생성의 content | UTF-8 16 KiB, NUL 금지; 빈 문자열 허용 |
| 수정 후 파일 | 8 MiB |
| 최종 구조화 결과 | UTF-8 JSON 32 KiB |
| 탐색 작업 | 호출당 10,000항목 |
| 내용 읽기 작업 예산 | 호출당 32 MiB; ignore 읽기도 소비량에 포함 |
| 협력적 실행 예산 | 10초, 파일·청크·줄 처리 사이 확인 |

reason은 result_limit·response_limit·line_limit·scan_limit·deadline·read_errors로 중단 이유를 구분한다.
목록은 next_offset으로 현재 정렬 목록의 다음 페이지를 요청한다. 호출 사이 파일 추가·삭제에 대한 snapshot pagination은 아니다.
재귀 검색은 별도 cursor 없이 path·pattern·query를 좁혀 재요청한다.
preview가 잘렸다면 완전한 원문이나 편집 근거로 사용하지 말고 해당 줄 범위를 읽는다.
OS 파일 접근 자체가 막히거나 취소 정리가 지연되면 정확히 10초 안의 반환은 보장하지 않는다.

원문은 structuredContent에 담고 content 텍스트에는 짧은 안내만 반환해 두 벌의 전체 소스를 보내지 않는다.
client의 저장·표시·컨텍스트 정책까지 서버가 통제하지는 않는다.

## 읽기와 원문 참조

UTF-8 원문의 탭·공백·LF/CRLF·마지막 개행 유무를 보존하며 줄 번호를 본문에 삽입하지 않는다.
version은 전체 원본 바이트의 SHA-256이며 Git 객체 ID가 아니다. 범위 밖의 변경도 식별값을 바꾼다.
UTF-8 디코딩 실패·NUL은 오류다. 문자 손실·인코딩 추측·공백 정정은 하지 않는다.

한 읽기에서 전체 해시와 반환 구간을 얻고, 접근 전후의 파일 식별·크기·수정 상태를 비교한다.
변경이 감지되면 결과·참조를 발급하지 않고 재조회를 안내한다. OS의 원자적 snapshot은 아니며 모든 동시 변경의 완전한 감지는 아니다.
읽기 완료 뒤의 변경 때문에 기존 참조를 현재 내용으로 자동 갱신하지 않는다.

본문·응답 상한에 도달하면 완전한 줄까지 반환하고 next_start_line을 제공한다.
첫 요청 줄 하나가 상한보다 크면 잘린 줄 대신 오류를 반환한다.
빈 파일의 첫 줄 읽기는 빈 content·end_line=null과 유효한 read_ref를 반환하므로 내용을 삽입할 수 있다.
비어 있지 않은 파일의 EOF 밖 요청에는 read_ref=null을 반환하고 유효한 줄 범위를 안내한다.

유효한 반환 구간은 추측하기 어려운 read_ref를 받는다. 보관하는 것은 반환 구간의 원문·경로·줄 위치·전체 version이며,
파일 전체 내용은 장기 보관하지 않는다. 참조는 서버 인스턴스·workspace에 결합되지만 Chat 대화별 인증 격리를 뜻하지 않는다.
같은 서버·workspace의 유효한 참조를 전달받은 다른 호출자도 사용할 수 있다. 참조가 경로·권한 검사를 대체하지 않는다.
참조 목록·별도 참조 조회 도구는 공개하지 않는다. 수정에는 같은 서버·workspace·경로의 유효한 참조가 필요하다.

참조는 프로세스 메모리에 최대 256개·원문 합계 8 MiB·발급 후 30분간 보관한다.
Python 객체·metadata까지 정확히 8 MiB라는 의미는 아니다. 사용해도 유효기간을 연장하지 않으며 오래된 발급 순으로 퇴출한다.
만료·퇴출·재시작 후에는 재조회하며 디스크 복구·현재 파일로의 자동 대체는 하지 않는다. 정상 종료 시 저장소를 비운다.
참조 자체는 잠금이 아니다. 수정 시 현재 파일의 버전을 다시 검사한다.

## 관측 구간 수정과 연속 호출

`replace_workspace_file(path, read_ref, replacement)`는 참조가 보관한 구간 전체를 replacement로 바꾼다.
구간 밖의 바이트는 유지하며 개행·공백을 자동 보정하지 않는다. replacement에 마지막 개행을 넣지 않으면 뒤의 원문과
같은 줄로 이어질 수 있으므로 원하는 줄바꿈을 명시한다. 빈 문자열 교체는 기존 파일의 내용을 비우며 파일 자체를 삭제하지 않는다.
수정으로 새로 생기는 줄을 기존 읽기·응답 상한 안에서 재관측할 수 없으면 교체 전에 `invalid_replacement`로 거부한다.

전체 파일의 현재 SHA-256이 관측 당시 version과 다르면 구간 밖 변경도 `stale_version`으로 거부한다.
서버 내부의 동일 파일 수정은 직렬 실행한다. 완성된 임시 파일을 같은 디렉터리에 준비하고 flush·fsync와
기존 mode 보존을 거친 뒤 교체하므로 교체 전 준비 실패가 원본을 부분적으로 덮어쓰지 않는다.
외부 편집기는 이 잠금을 공유하지 않으며 버전 검사와 교체는 별도 OS 연산이다.

성공 후 실제 파일을 재관측하면 `content`, `start_line`, `end_line`, 새 `read_ref`를 반환한다.
다음 수정은 이 반환 구간과 새 참조를 사용한다. 읽기 상한으로 구간이 제한되면 참조도 반환된 완전한 줄만 가리킨다.

### 수정 결과와 다음 행동

수정 결과에는 `project`, `path`, `outcome`, `changed`, `version_before`, `version_after`, `observed_at`과
관측 결과가 있다. `code`, `reason`, `next_action`, `next_call`은 실패 원인과 안전한 다음 행동을 안내한다.
`next_call`이 있으면 도구명과 인자를 제공한다. 권한·정책 수정이 필요한 경우에는 같은 호출을 반복하지 않는다.

| outcome | 의미 | 다음 행동 |
| --- | --- | --- |
| `applied` | 파일 교체와 후속 관측을 완료함 | 반환된 원문과 새 참조로 계속 수정 |
| `unchanged` | 교체할 결과가 현재 바이트와 같아 이 요청은 쓰지 않음 | 새 관측 참조가 있으면 계속 수정 |
| `rejected` | 이 요청은 원본을 교체하지 않음 | 원인에 따라 입력·정책을 수정하거나 새로 읽기 |
| `applied_unverified` | 교체는 완료했으나 후속 관측을 완료하지 못함 | 같은 쓰기를 반복하지 않고 현재 파일부터 읽기 |

`changed`는 이 요청의 교체 여부다. `rejected`나 `unchanged`여도 외부 편집자가 파일을 바꾸지 않았다는 뜻은 아니다.
구조화된 거부 결과와 별개로 잘못된 JSON 타입·알 수 없는 필드 등은 MCP 입력 오류가 될 수 있다.
`cleanup_warning`이 있으면 임시 파일 정리 상태를 확인하되 실제 교체 여부와 혼동하지 않는다.

### 사용자 편집 후 재개

사용자가 수정 구간 안이나 밖을 편집하면 기존 참조로 계속 쓰지 않는다. 현재 내용을 다시 읽고,
의도한 위치와 수정안을 확인한 뒤 새 read_ref로 요청한다. 줄 삽입·삭제가 있었다면 과거 줄 번호를 그대로 재적용 위치로 사용하지 않는다.
참조 만료·퇴출·서버 재시작도 새 관측으로 재개한다. 파일이 삭제·이동됐으면 현재 대상을 다시 선택하며 자동으로 재생성하지 않는다.

응답 유실·취소·교체 후 확인 실패는 무변경의 증거가 아니다. 현재 파일을 조회해 적용 여부부터 판단하고,
사용자의 최신 내용을 과거 상태로 자동 복원하거나 수정안을 자동 병합하지 않는다.

## 파일 생성·삭제와 요청 재전송

새 생성·삭제 의도에는 새 `request_id`를 사용한다. 요청 ID는 ASCII 영숫자·점·밑줄·콜론·하이픈으로 최대 128자다.
같은 서버·workspace에서 같은 ID와 같은 입력을 재전송하면 보관된 결과를 반환하고 파일 작업을 반복하지 않는다.
다른 입력으로 같은 ID를 재사용하면 거부한다. 이 판단은 현재 파일이 없어졌거나 같은 경로에 새 파일이 생긴 경우에도
이전 receipt를 먼저 확인한다. `replayed=true`는 이전 요청 결과이며 현재 파일 상태를 새로 관측했다는 뜻이 아니다.
거부 원인을 고치거나 새 `read_ref`·부모 생성 옵션으로 의도적으로 다시 시도할 때는 새 요청 ID를 사용한다.

```json
{"path":"src/parsing/token.py","content":"def token(text):\n    return text.strip()\n","request_id":"token-source-1","create_parents":true}
```

`create_workspace_file`은 완성한 UTF-8 내용을 기존 대상의 덮어쓰기 없이 게시한다. 파일·디렉터리·symlink 등
기존 경로가 있으면 거부하며, 같은 내용이라는 이유로 이 요청이 생성했다고 추정하지 않는다.
`content`는 빈 문자열을 허용하며 NUL 없는 UTF-8 최대 16 KiB다. 각 줄은 구조화된 읽기 응답에도 들어가야 한다.
부모가 없을 때 기본 호출은 생성하지 않고 필요한 재호출 방법을 안내한다. `create_parents=true`이면 필요한 부모를
허용·쓰기 가능·nonignored 범위에서만 생성한다. 중간에 실패하면 이 요청이 만든 디렉터리가 남을 수 있으며
`created_directories`로 알린다. 사용자가 내용을 추가했을 수 있으므로 디렉터리를 무조건 되돌려 지우지 않는다.

성공 뒤 실제 파일을 읽어 확인한 경우 `content`, `version_after`, 새 `read_ref`를 반환한다.
긴 결과는 기존 읽기 상한을 따르므로 새 참조가 가리키는 관측 구간을 확인한다.
생성이 적용된 뒤 확인이나 정리가 실패한 결과를 무변경으로 해석해 다시 생성하지 않는다.

```json
{"path":"src/parsing/old_token.py","read_ref":"<current-read-ref>","request_id":"old-token-delete-1"}
```

`delete_workspace_file`은 같은 경로의 유효한 관측 참조와 현재 전체 SHA-256을 확인한 뒤 일반 파일 하나를 삭제한다.
참조 발급 뒤 구간 밖에서 바뀐 내용도 `stale_version`으로 거부한다. 디렉터리·재귀 삭제·symlink 대상 삭제는 지원하지 않는다.
삭제는 working tree에만 적용하며 Git index·HEAD를 자동 변경하지 않는다. 자체 백업·휴지통·복구 도구도 제공하지 않는다.
Git에 이미 기록된 버전은 별도로 복구할 수 있지만 최신 unstaged 수정·untracked 파일 내용의 보존은 보장하지 않는다.
자동 commit·stage·stash로 복구 원본을 만들지 않는다.

생성·삭제 결과는 기존 수정 결과 필드에 `operation`, `request_id`, `replayed`, `created_directories`를 추가한다.
`changed`는 원래 요청이 대상 파일을 바꿨는지 나타내며 재전송 때 파일을 다시 바꿨다는 뜻은 아니다.
파일 생성이 거부되고 `changed=false`여도 먼저 만든 부모 폴더는 남을 수 있으므로 `created_directories`를 함께 확인한다.
`code`, `reason`, `next_action`, `next_call`, `cleanup_warning`을 확인해 충돌·부분 적용·후속 관측 실패를 구분한다.

Receipt는 최대 128개·완료 후 30분간 서버 메모리에 보관한다. 요청 지문 장부는 최대 4096건의 tombstone을 유지한다.
결과가 만료·퇴출된 요청은 새 작업으로 다시 실행하지 않고 오류로 안내하며, 장부가 가득 차면 새 요청을 거부한다.
재시작 뒤에는 receipt와 장부가 사라진다. 요청 ID는 인증된 Chat 대화 ID나 영구 중복 방지 키가 아니다.

생성·수정·삭제는 같은 경로의 서버 잠금과 현재 파일 접근 정책을 공유한다. 외부 IDE는 이 잠금을 공유하지 않으며,
버전 확인과 삭제는 별도 OS 연산이다. 외부 편집까지 막는 원자적 조건부 삭제나 다중 파일 transaction을 보장하지 않는다.
