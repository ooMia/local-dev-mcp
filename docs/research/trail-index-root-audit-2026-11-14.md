# Trail Index Root Audit — 2026-11-14

상태: **부분 검증, 상위 폴더의 캐시 구성 미확인. 정리·purge 미수행.**

## 목적

M4(Linked Worktree별 Trail 그래프 격리) 이전에 상위 폴더의 `graft/`가 실제로 무엇을 저장하는지 확인하고, 잘못된 조회 루트 선택을 재현 가능한 근거로 판단한다. **캐시의 존재나 디렉터리 크기만으로 삭제를 결정하지 않는다.**

## 관측된 경로

사용자가 2026-11-14 제공한 `ls -ld` 출력:

| 경로 | 디렉터리 항목 | 해석 |
| --- | --- | --- |
| `/Users/mia/Github/graft` | `drwxr-xr-x@ 3 ... 96`, 11월 14일 02:07 | 상위 폴더에 `graft/`가 존재한다. **내용·총 사용량·정확한 그래프 역할은 아직 미확인** |
| `/Users/mia/Github/local-dev-mcp/graft` | `drwxr-xr-x@ 167 ... 5344`, 11월 10일 06:09 | 개별 프로젝트의 `graft/`가 존재한다. **정확한 유효성은 별도 검사 필요** |

`ls -ld`의 96/5344는 전체 캐시 데이터 크기나 심볼 수가 아니다. 상위 폴더가 여러 Git 저장소를 포함하면 Trail의 공식 동작상 `graft/workspace.json`을 이용한 **federated workspace 인덱스**일 가능성이 있다. 실제 파일 목록을 확인하기 전에는 그것으로 확정하지 않는다.

## LOCAL_DEV_MCP / Trail 읽기 전용 결과

- `self-local-dev-mcp` 관리 루트 `/Users/mia/Github/local-dev-mcp`: `get_project_index_status` = `structural_state=unknown`, `deep_state=stale`, `pending_changes=true`, `reason=check_unrecognized`. **그래프 오염이나 손상으로 판정할 수 없음**. 현재 검사 출력 파서·실행 버전 점검 필요.
- 별도 `local-dev-mcp` 인스턴스의 관리 루트 `/Users/mia/Github/stt-lab`: `structural_state=unavailable`, `reason=trail_not_configured`. 해당 프로젝트의 Trail 설정 상태이지 상위 `Github/graft`의 오류 증거가 아니다.
- 공유 `TRAIL_CODE`의 `trail_check_freshness`는 `Session terminated`로 응답. 공유 MCP 실제 루트·라우팅 결과의 확인은 **BLOCKED**.
- 기존 Git 작업 트리의 수정 `config.toml`은 보존했다.

## 격리 회귀 실행

`tests/mcp/test_trail_nested_worktree_probe.py` 및 `tests/mcp/test_trail_index_e2e.py`를 등록된 integration 작업으로 직접 수행:

- **2 passed, 5 warnings** (2026-11-14, 약 6.89초, exit 0).
- 임시 Git 프로젝트에서 명시적 루트의 독립 그래프 생성·Fresh 판정 및 기본 M1–M3 갱신 경계를 확인했다.
- 이 성공은 **실제 `/Users/mia/Github` federation 루트에서 질의 라우팅이 정확하다는 증거가 아니다.** 테스트 코드는 그래프의 바이트 차이도 검증하지만, 실제 동일 심볼의 CLI 조회 결과까지 비교하는 M4 검증은 후속 과제다.

## 외부 근거와 실패 가능성

- [Trail Code README](https://github.com/trailhq/Trail-Code/blob/main/README.md): 여러 독립 Git 저장소의 상위 폴더에서 `trail build`는 각 자식의 그래프와 상위 `graft/workspace.json` 인덱스를 구성한다. 상위 캐시가 있다고 해서 잘못된 캐시인 것은 아니다.
- [Trail Code 이슈 #453](https://github.com/trailhq/Trail-Code/issues/453): 상위 checkout 내부의 nested linked worktree가 명시적 root 없이 **상위 그래프를 반환**하는 재현 사례. 한시적 회피책은 해당 worktree의 명시적 루트를 사용하는 것이다.
- 이슈의 버전·재현 환경이 현재 설치 버전과 동일한지는 아직 미확인. 디렉터리 삭제가 이 원인을 영구 해결한다는 근거는 없다.

## 다음 읽기 전용 증거 (현재 연결에서 상위 폴더 직접 접근 불가)

아래 명령은 기존 시스템의 파일을 삭제하거나 다시 빌드하지 않는다.

```sh
# macOS terminal; 파일 이름과 사용 중인 CLI 버전만 확인
trail --version
find "$HOME/Github/graft" -maxdepth 2 -type f -print
find "$HOME/Github/local-dev-mcp/graft" -maxdepth 2 -type f -name 'workspace.json' -print

# 구조 Fresh 상태의 차이를 비교; --check는 재빌드가 아닌 검사 명령
trail build --check "$HOME/Github"
trail build --check "$HOME/Github/local-dev-mcp"
```

그 다음 **명시적 프로젝트 루트 조회**와 **상위 federated 루트 조회**에서 동일 심볼/파일의 실제 소스 위치가 일치하는지 비교한다. 최신 Worktree에서는 명시적 Worktree 루트 질의까지 확장한다. 설치 버전에서 `--check`나 조회의 부작용이 의심되면 먼저 도움말·경로 상태를 확인한다.

## 결정 기준

| 증거 | 조치 |
| --- | --- |
| 상위 `workspace.json`이 정상이고 명시적 자식 루트 질의가 올바름 | **보존. purge 금지.** M4 Target Resolver와 실제 조회 회귀에 집중 |
| 잘못된 그래프가 반환되지만 명시적 루트 지정으로 정상화 | 캐시 삭제보다 **라우팅/루트 전달 오류** 수정 |
| 명시적 루트에서도 오래된 심볼이 반환되고 `--check`가 stale 보고 | **해당 프로젝트만** 증분 재빌드 → 확인 후 한 번의 no-reuse 재빌드 검토 |
| 같은 루트의 재빌드 후에도 교차 그래프 재현 | 잘못된 캐시를 특정하고 백업·복구 범위를 확정한 뒤 선택적 정리 판단 |
| 상위 폴더의 `graft/` 내용이 아직 미확인 | **파괴적 조치 보류** |

### 현재 결론

**Purge 불필요로 확정한 것도, 필요로 확정한 것도 아니다.** 상위 `graft/` 존재만으로는 근거가 부족하고, 기존 E2E는 성공했다. 상위 `workspace.json` 및 두 루트의 명시적 조회가 확인될 때까지 `/Users/mia/Github/graft`와 프로젝트별 `graft/`를 그대로 둔다.

연결된 연구 범위: [M4 Worktree 인덱스 경계 및 회귀 매트릭스](./trail-worktree-index-m4-2026-11-14.md).
