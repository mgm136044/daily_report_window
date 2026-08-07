---
name: daily-report
description: 하루 마감 보고서 자동화(daily-report)를 운영할 때 사용한다. 상태 확인, 특정 날짜 보고서 재생성, 밀린 날짜 처리, 수집 제외 폴더·프로젝트 표시명 변경, 안 돌았을 때 원인 진단. 트리거 — "하루 마감", "일일 보고서", "오늘 정리해줘", "어제 보고서 다시", "보고서 왜 안 올라왔어", "노션에 안 들어갔어", "제외 폴더 추가", "daily report", "일지 자동화 상태". 자동화 본체는 launchd 가 매일 정해진 시각에 알아서 돌리므로, 이 스킬은 사람이 개입할 때만 쓴다.
---

# 하루 마감 보고서 운영

Claude Code 와 Codex CLI 의 세션 기록, git 커밋, 디스크 변경에서 하루 작업을 뽑아 요약하고
Notion 에 날짜별 한 행으로 넣는 자동화의 **운영 창구**다. 자동화 자체는 이 스킬 없이 돈다.

## 프로젝트 위치부터 정한다

설치 위치는 사람마다 다르다(직접 클론, 플러그인 설치, 심링크). 명령을 돌리기 전에
루트를 먼저 정한다 — 스킬이 있는 자리에서 `run_day.py` 가 나올 때까지 위로 올라간다.

```bash
PROJECT="${DAILY_REPORT_DIR:-${CLAUDE_PLUGIN_ROOT:-}}"
if [ -z "$PROJECT" ]; then
  D="$(readlink -f ~/.claude/skills/daily-report 2>/dev/null)"
  while [ -n "$D" ] && [ "$D" != "/" ] && [ ! -f "$D/run_day.py" ]; do D="$(dirname "$D")"; done
  PROJECT="$D"
fi
[ -f "$PROJECT/run_day.py" ] || echo "프로젝트를 못 찾음 — DAILY_REPORT_DIR 을 설정할 것"
cd "$PROJECT"
```

이 아래의 모든 명령은 그 디렉터리에서 실행한다.

## 먼저 알아야 할 것

**논리적 하루는 `config.toml` 의 `day.boundary_hour` 로 정해진다**(기본 04:00 ~ 다음날 04:00).
새벽 2시 작업은 전날로 잡힌다. 그래서 그 시각 직후에 돈다.
"오늘"을 시계로 추론하지 말고 `config.logical_date()` 를 쓴다.

**Notion MCP 로 이 데이터베이스가 안 보일 수 있다.** 자동화가 쓰는 워크스페이스와
Claude Code 의 Notion MCP 가 붙어 있는 워크스페이스가 다르면 **404 가 정상이다.**
권한 문제가 아니다. 그 경우 건드리려면 `.env` 의 토큰으로 REST 를 직접 호출해야 한다.

## 요청 → 할 일

| 사용자가 말하는 것 | 하는 일 |
|---|---|
| "상태", "잘 돌고 있어?", "확인해줘" | `python3 doctor.py` |
| "왜 안 돌았어", "노션에 안 들어갔어" | `python3 doctor.py --full` 후 아래 진단 절차 |
| "오늘 것 만들어줘", "지금까지 정리해줘" | `python3 run_day.py $(오늘 논리적 날짜)` |
| "어제 것 다시", "8/3 다시 뽑아줘" | `python3 run_day.py 2026-08-03` — 멱등이라 덮어쓴다 |
| "밀린 것 처리해줘" | `python3 run_day.py` (인자 없음) |
| "며칠치 다시 만들어줘" | 상태 장부에서 해당 날짜 삭제 후 `run_day.py` |
| "X 폴더 빼줘" | `config.toml` 의 `exclude.paths` 에 추가 |
| "프로젝트 이름 바꿔줘" | `config.toml` 의 `[labels] rename` |
| "보고서가 너무 길어/짧아" | `config.toml` 의 `report.target_chars`, 그다음 `prompts/<언어>.md` |
| "커밋이 0으로만 나와" | `config.toml` 의 `git.authors` 확인 — 아래 5번 |
| "Codex 작업이 안 보여" | `config.toml` 의 `sources.codex_sessions_dir` 와 아래 7번 |

## 자주 하는 작업

### 특정 날짜 재생성

```bash
python3 run_day.py 2026-08-03
```

약 2~3분 걸린다(요약에 LLM 1회). 같은 날짜로 몇 번을 돌려도 Notion 행은 하나다.

### 며칠치 다시 만들기

`run_day.py` 는 완료로 기록된 날짜를 건너뛴다. 다시 만들려면 장부에서 지운다.

```bash
python3 - <<'PY'
import json
p = "state/lastrun.json"
s = json.load(open(p, encoding="utf-8"))
for d in ["2026-08-02", "2026-08-03"]:
    s["completed"].pop(d, None)
json.dump(s, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
PY
python3 run_day.py
```

### 수집 제외 폴더 추가

`config.toml` 의 `[exclude] paths` 에 부분 문자열로 넣는다. 앞뒤 슬래시를 붙이는 게 안전하다.

```toml
paths = [
    ...,
    "/client-name/",
]
```

바꾼 뒤에는 이미 올라간 날짜를 재생성해야 반영된다. 그냥 두면 과거 행에는 남아 있다.

> `[exclude] paths` 는 **수집 자체를 막는다**. `[sources] walk_exclude` 와 혼동하지 말 것 —
> 후자는 저장소 탐색(`os.walk`)에서만 빼는 것으로, 그 폴더의 작업은 보고서에 그대로 남는다.

### 프로젝트 표시명

```toml
[labels]
rename = { ".claude" = "Claude Code 설정", "RESEARCH" = "리서치" }
```

폴더 이름이 그대로 보고서에 뜨므로, 불친절한 것만 여기에 넣는다.

### 프로젝트가 이상하게 잡힐 때

`~/Downloads` 처럼 담는 폴더가 프로젝트로 뜨면 `config.toml` 의 `[projects] containers` 에 넣는다.
반대로 진짜 프로젝트가 안 잡히면 그 폴더에 마커(`.git`, `CONTEXT.md`, `pyproject.toml` 등)가
있는지 확인한다. 판별은 `project_roots.project_label()` 로 즉시 시험할 수 있다.

```bash
python3 -c "import sys;sys.path.insert(0,'.');import project_roots;print(project_roots.project_label('/경로'))"
```

## 진단 절차

`doctor.py --full` 이 순서대로 검사하고, 첫 실패가 대개 원인이다. 그래도 안 잡히면:

1. **로그가 비어 있는데 프로세스가 살아 있다** → 어딘가에서 멈춘 것이다.
   ```bash
   PID=$(pgrep -f run_day.py); sample $PID 2 -mayDie | sed -n '/Call graph/,/Total/p' | head -30
   ```
   과거에 `os.walk` 가 클라우드·TCC 보호 폴더를 열다 무한 대기한 적이 있다. 지금은 워치독
   (`run.watchdog_sec`)이 걸려 있지만, 새 경로에서 재발할 수 있다. 그 경로를
   `[sources] walk_exclude` 에 넣는다.

2. **시작은 했는데 몇 시간째 안 끝난다** → 기기가 도중에 잠들었을 수 있다.
   - **macOS**: `ps -o lstart= -p $(pgrep -f run_day.py)` 로 시작 시각을 보고,
     `pmset -g log | grep Sleep` 으로 그 사이 슬립이 있었는지 확인한다. plist 에
     `caffeinate -s -i` 래퍼가 있는지도 본다 — 빠지면 Power Nap 이 작업을 얼린다.
     워치독은 시스템이 자는 동안 시간을 세지 않아 이 경우엔 발동하지 않는다.
   - **Windows**: `powercfg /sleepstudy` 로 슬립 이력을 본다. 여기서는 워치독이 벽시계를
     세므로 오래 잠들었으면 그날은 중단되고 다음 실행에서 백필된다 — 매달려 있는 것보다 낫다.

3. **"Not logged in" 이 뜬다** → 자격증명을 못 읽는 것이다. 플랫폼마다 원인이 다르다.
   - **macOS**: plist 의 `EnvironmentVariables` 에 `USER` 가 있는지 본다. 없으면 로그인돼
     있어도 죽는다.
   - **Windows**: 작업의 로그온 유형이 `Interactive` 인지 본다. "사용자의 로그온 여부에
     관계없이 실행" 이면 DPAPI 로 보호된 자격증명을 풀 수 없다. `doctor.py` 가 검사한다.

   어느 쪽이든 **터미널에서는 절대 재현되지 않는다** — 대화형 세션이 그 값을 넘겨주기 때문.

4. **Notion 404** → 부모 페이지에 연결이 공유되어 있는지. 페이지 `•••` → Connections.
   **403** 이면 권한 부족(Configuration 에서 insert content). **401** 이면 토큰 문제.

5. **커밋이 계속 0** → 정상일 수 있다. `config.toml` 의 `git.authors` 에 있는 저자만 센다.
   비어 있으면 **하나도 세지 않는다.** 본인 주소를 찾으려면:
   ```bash
   git log --format='%ae' | sort | uniq -c | sort -rn | head
   ```
   반대로 저자 필터를 지우면 안 된다 — 홈에 있는 외부 저장소 포크가 남의 커밋 수백 건을 섞어 넣는다.

6. **행이 두 개 생겼다** → `upsert` 가 그 날짜에 대해 영구히 예외를 던진다. Notion 에서 하나를
   지우고 다시 실행한다.

7. **Codex 작업이 보고서에 없다** → `python3 collect_codex.py 2026-08-03` 로 단독 확인한다.
   `cwd 0개` 면 그날 Codex 를 안 썼거나, 세션의 `cwd` 가 `exclude.paths` 에 걸린 것이다.
   명령이 이상하게 보이면 `extract_commands()` 를 의심한다 — Codex 는 셸 명령이 아니라
   JavaScript 를 넘기므로 그 안의 `cmd:` 를 뽑아내는데, 새 래퍼 형태가 나오면 놓칠 수 있다.

## 건드릴 때 지켜야 할 것

- **`.env` 는 절대 커밋하지 않는다.** `work/`, `state/`, `logs/`, `fixtures/`, `config.toml` 도
  마찬가지다. `work/` 에는 사용자 프롬프트 원문이 들어 있다.
- **토큰 값을 출력하지 않는다.** 확인이 필요하면 앞 8자와 길이만 보여준다.
- **살균기(`sanitize.py`)를 약화시키지 않는다.** 세션 기록에는 실제로 살아 있는 자격증명이
  들어 있다 — 30일치 스캔에서 유효한 토큰이 평문으로 검출된 적이 있다.
- **코드를 고쳤으면 `python3 -m pytest tests/ -q` 를 돌린다.** 각 테스트가 과거에 실제로
  터졌던 결함 하나씩을 막는다.
- **공개용으로 뭔가 내보내기 전에 `python3 scripts/check_no_pii.py` 를 돌린다.**

## 파일

| 파일 | 역할 |
|---|---|
| `doctor.py` | 상태 점검·진단 |
| `run_day.py` | 파이프라인·백필·잠금·워치독 |
| `collect.py` | Claude Code 전사본 + git 수집 |
| `collect_codex.py` | Codex 롤아웃 수집 |
| `collect_fs.py` | 디스크에서 바뀐 파일 |
| `refine.py` | 프로젝트 롤업·노이즈 제거·병합 |
| `project_roots.py` | 작업 디렉터리 → 프로젝트 |
| `sanitize.py` | 자격증명 마스킹 |
| `summarize.py` | 헤드리스 요약 |
| `notion_upsert.py` | 날짜 키 upsert |
| `setup_notion_db.py` | 데이터베이스 생성 (최초 1회) |
| `platform_support.py` | OS 의존 부분 전부 |
| `config.toml` | 모든 설정 |

설계 근거와 측정값은 `docs/design.ko.md`, 설치 절차는 `README.ko.md` 에 있다.
