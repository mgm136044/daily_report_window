# daily-report

하루가 끝나면 그날 실제로 한 일을 Notion 데이터베이스에 **날짜별 한 행**으로 적는다.
어떤 프로젝트에서 무엇을 요청했고, 어떤 파일을 어디에 만들거나 고쳤고, 어떤 명령을 돌렸고,
다음에 뭘 하기로 했는지까지.

새로 기록을 시작하는 게 아니다. 코딩 에이전트가 **이미 전부 기록해 두고 있다.**
그 기록을 읽어서, 다시 볼 일 없는 99.9% 를 버리고, 남은 것으로 보고서를 만든다.

[English README](README.md) · [설계 노트](docs/design.ko.md) · [Notion 준비](docs/notion-setup.ko.md)

## 무엇을 읽는가

| 소스 | 여기서 얻는 것 |
|---|---|
| Claude Code 전사본 (`~/.claude/projects/**/*.jsonl`) | 프롬프트, 파일 편집, 명령, 계획 |
| Codex CLI 롤아웃 (`~/.codex/sessions/**/*.jsonl`) | 프롬프트, 패치, 명령, 계획 |
| git — 설정한 루트 아래 모든 저장소 | 본인이 작성한 커밋 |
| 파일시스템 — 그날 이미 활동이 있는 프로젝트 한정 | 에이전트가 **스크립트를 돌려** 만든 파일. 도구 로그에는 안 남는다 |

전부 없어도 된다. Codex 를 안 쓰면 그 부분은 그냥 비고, 저장소가 없으면 커밋이 0 이다.

## 어떻게 도는가

```
04:05  launchd (macOS) · 작업 스케줄러 (Windows)
  ①  밀린 날짜 판정      state/lastrun.json 과 대조
  ②  수집               전사본 + 롤아웃 + 커밋 + 디스크 변경
  ③  정제               프로젝트 단위로 묶고, 탐색 노이즈 제거
  ④  살균               자격증명 마스킹 — 모델에 넣기 전
  ⑤  요약               claude -p 한 번으로 보고서 생성
  ⑥  살균               게시 전 재확인
  ⑦  적재               날짜 키 upsert (같은 날짜면 덮어씀)
```

논리적 하루는 **04:00 ~ 다음날 04:00** 이다. 새벽 2시 작업은 전날로 잡힌다.
그래서 하루가 완전히 닫힌 직후인 04:05 에 돈다.

PC 가 자거나 꺼져 있었으면 다음 실행에서 밀린 날짜를 채운다. `launchd` 는 놓친 일정을
**한 번으로 합쳐 버리고**, 작업 스케줄러는 기본값이 **아예 건너뛰는 것**이라
`StartWhenAvailable` 을 켜서 같은 성질로 맞춘다. 어느 쪽이든 작업이 자체 장부를 들고 백필한다.

## 준비물

- **macOS 또는 Windows 10/11.** [지원 플랫폼](#지원-플랫폼) 참고.
- **Python 3.11+.** 표준 라이브러리만 쓴다. 설치할 패키지가 없다.
- **로그인된 Claude Code CLI 또는 Codex CLI.** 요약을 `claude -p` 나
  `codex exec` 가 만든다 — [요약 엔진](#요약-엔진) 참고. 둘 다 없으면 수집은
  되지만 보고서가 안 나온다.
- **Notion 내부 연결 토큰**과 데이터베이스를 만들 부모 페이지.

## 설치

macOS:

```bash
bash install.sh
```

Windows (관리자 권한 필요 없음):

```bash
powershell -ExecutionPolicy Bypass -File install.ps1
```

<details>
<summary><b>윈도우 — 갓 클론한 상태에서 처음부터</b></summary>

먼저 있어야 하는 것: Windows 10/11, Python 3.11+(`tomllib` 이 거기서 생겼다), git,
그리고 **로그인된** Claude Code CLI — 없으면 수집은 되지만 보고서가 안 만들어진다.
관리자 권한은 어느 단계에서도 필요 없다.

**1.** 클론하고 설치기를 돌린다.

```bash
powershell -ExecutionPolicy Bypass -File install.ps1
```

9단계 중 5단계에서 멈춘다. 다음 단계에 **본인만 발급할 수 있는 토큰**이 필요하기 때문이다.
멈추기 전에 셋을 묻는다 — 보고서 언어, 커밋 저자 이메일, 저장소를 찾을 최상위 폴더.
마지막 것은 드라이브별 저장소 개수를 세어서 보여준다. **`~` 는 윈도우에서 대개 틀린
답이다.**

**2.** 노션 연결을 만든다 — [docs/notion-setup.ko.md](docs/notion-setup.ko.md).
실패의 대부분은 두 가지다. **개인 액세스 토큰(PAT)이 아니라 "내부 연결"의 설치 토큰**이어야
하고(PAT 는 만료되고, 만료되면 작업이 조용히 죽는다), 부모 페이지를 그 연결에 공유해야
한다(`•••` → Add connections).

**3.** 토큰을 넘긴다. `.env` 의 `DAILY_REPORT_NOTION_TOKEN` 과
`DAILY_REPORT_PARENT_PAGE_URL` 을 직접 채우거나(`DAILY_REPORT_DATABASE_ID` 는 비워 둔다 —
설치기가 채운다), 마법사를 쓴다.

```bash
py -3 setup_gui.py
```

마법사는 사실상 이 입력란 하나 때문에 있다. **터미널은 타이핑한 것을 남기고 GUI 입력란은
남기지 않는다** — 이 도구가 존재하는 이유가 세션 로그가 전부 남기 때문이다.

**4.** 설치기를 다시 돌린다. 데이터베이스를 만들고, ID 를 되써 넣고, 04:05 작업을 등록하고,
시작 메뉴 바로 가기를 만들고, 스킬을 연결하고, `doctor.py` 로 끝낸다. **다시 돌려도 안전하다.**

**5.** 확인한다.

```bash
py -3 -X utf8 doctor.py
```

첫 예약 실행은 다음 04:05 다. 설치했다고 그 이전 2주를 소급 생성하지는 않는다 —
가장 최근에 닫힌 하루만 만든다.

</details>

두 번에 나눠 돈다. 중간에 **본인만 발급할 수 있는 토큰**이 필요하기 때문이다.

**1차 실행**은 플랫폼과 Python 3.11+ 를 확인하고, 예시 설정에서 `config.toml` 을 만들고
(보고서 언어와 커밋 저자 이메일을 물어본다), 권한 600 으로 `.env` 를 만든 뒤,
**무엇을 채워야 하는지 알려주고 멈춘다.**

**`.env` 를 채운다** — [docs/notion-setup.ko.md](docs/notion-setup.ko.md) 를 따라 내부 연결을
만들고 부모 페이지를 그 연결에 공유한다. 토큰은 그 파일 안에만 둔다. 로그가 남는 터미널에
붙여 넣지 않는다 — **이 도구가 존재하는 이유가 세션 로그에 타이핑한 게 전부 남기 때문이다.**

**2차 실행**은 Notion 데이터베이스를 만들고(스키마를 코드가 만들어야 쓰는 쪽 속성 이름과
어긋나지 않는다), 그 ID 를 `.env` 에 되써 넣고, 예약 작업을 생성·등록하고,
스킬을 `~/.claude/skills/` 에 링크한 뒤, 마지막으로 `doctor.py` 를 돌려 결과를 보여준다.

Windows 쪽 설치기는 한 가지를 더 한다. **이 PC 의 셸 폴더가 실제로 어디인지 물어본다.**
한국어 Windows 는 `Documents` 와 `문서` 를 **둘 다** 갖고 있고, OneDrive 의 알려진 폴더 이동이
켜져 있으면 진짜 문서 폴더는 `~/OneDrive/문서` 다. 이걸 모르면 PowerShell 프로필을 한 줄
고친 것이 "문서" 라는 프로젝트의 작업으로 보고된다.

다시 돌려도 안전하다. `config.toml`, `.env`, 이미 만든 데이터베이스는 덮어쓰지 않는다.

그다음 무인 요약이 되는지 확인하고 하루치를 손으로 돌려 본다.

```bash
python3 summarize.py x y --preflight
python3 run_day.py 2026-08-04
```

`git.authors` 는 기본값을 줄 수 없는 유일한 값이다. 비어 있으면 커밋을 하나도 안 모으고,
반대로 필터를 없애면 홈에 있는 남의 저장소 포크가 수백 건의 남의 커밋을 쏟아 넣는다.

```bash
git log --format='%ae' | sort | uniq -c | sort -rn | head
```

> **macOS — plist 의 `EnvironmentVariables` 에서 `USER` 를 빼면 안 된다.** Claude Code CLI 는
> 로그인 키체인에서 **계정 이름으로** 자격증명을 찾기 때문에, `USER` 가 없으면 로그인돼
> 있어도 "Not logged in" 으로 죽는다. `launchd` 는 환경변수를 거의 안 넘기고,
> 이 실패는 **터미널에서 절대 재현되지 않는다** — 거기선 셸이 그 값을 넘겨주기 때문이다.

> **Windows — 작업을 "사용자의 로그온 여부에 관계없이 실행" 으로 바꾸면 안 된다.**
> 같은 실패의 윈도우판이다. CLI 의 자격증명은 로그온한 사용자에게 묶여 보호되는데,
> 그 설정은 자격증명을 풀 수 없는 토큰을 준다. 결과는 똑같다 — 매일 밤 "Not logged in",
> 다른 건 전부 정상으로 보인다. `doctor.py` 가 이걸 검사한다.

처음 설치했다고 그 이전 2주를 소급 생성하지는 않는다. 가장 최근에 닫힌 하루만 만들고,
나머지는 "설치 이전 날짜" 로 장부에 적어 다시 건드리지 않는다.

## 쓰는 법

```bash
python3 run_day.py                 # 밀린 날짜 전부 (launchd 가 부르는 형태)
python3 run_day.py 2026-08-04      # 특정 날짜만
python3 doctor.py                  # 잘 돌고 있는지, 아니면 왜 안 되는지
```

같은 날짜를 다시 돌리면 행이 하나 더 생기는 게 아니라 **덮어쓴다.**
보고서가 마음에 안 들면 그냥 다시 돌리면 된다.

### 윈도우: 설치한 뒤 여는 법

**시작 메뉴에서 "하루 마감 보고서" 를 찾으면 된다.** `install.ps1` 이 만들어 둔다.
누르면 상태 창이 열린다 — 마지막 실행, 다음 실행, 최근 14일 스트립, 그리고 진단·재생성·
로그·노션 버튼. `pythonw` 로 뜨므로 뒤에 빈 콘솔이 따라오지 않는다.

직접 부르려면:

```bash
pythonw -X utf8 status_window.py
```

`python` 이 아니라 **`pythonw`** 다. `python` 으로 열면 창 뒤에 콘솔이 하나 남는다.

창은 둘 다 순수 tkinter다 — 의존성을 들이지 않는다.
**"설치할 패키지가 없다" 는 성질이 더 나은 툴킷보다 값어치가 있다.**

설치 마법사는 바로 가기를 만들지 않는다. 한 번 쓰고 마는 것이라, 다시 필요하면 직접 부른다:

```bash
python setup_gui.py
```

설치 마법사. 값을 모아서 `install.ps1` 을 부른다 — **다시 구현하지 않는다.**
작업 등록과 권한 좁히기는 한 번 검증했고, 위험한 단계의 구현이 두 벌이 되면 어긋난다.
마법사가 직접 하는 건 토큰 입력란 하나이고 그게 핵심이다 — README 가 토큰을 터미널에
붙여넣지 말라는 이유는 세션 로그에 남기 때문인데, **GUI 입력란은 로그에 안 남는다.**

**무인 실행에는 아무것도 필요 없다.** 본체는 예약 작업이고, 이 둘은 사람이 실제로
앞에 있는 두 순간 — 첫 설치, 그리고 "어젯밤에 왜 안 돌았지" — 를 위한 것이다.

## 설정

전부 `config.toml` 에 있다. 설정하려고 코드를 고칠 일은 없다.
모든 키의 설명은 `config.example.toml`(macOS) 또는 `config.windows.example.toml`(Windows)
안에 있다. 기본값은 이식할 수 없어서 파일이 둘이다 — 캐시·클라우드·임시 폴더가 어디 있느냐는
**그 OS 의 사실**이고, 맥의 목록을 윈도우에 적용하면 존재하지 않는 경로만 제외하게 된다.
중요한 것만 추리면:

| 키 | 하는 일 |
|---|---|
| `git.authors` | 어떤 커밋이 내 것인가. **여기 없는 저자는 전부 제외된다** |
| `exclude.paths` | 아예 수집하지 않을 경로 — 고객사 작업, 대외비 폴더 |
| `projects.containers` | 프로젝트를 *담는* 폴더. 자기 자신은 프로젝트가 아니다 |
| `labels.rename` | 폴더 이름이 보고서에서 불친절할 때 쓸 표시명 |
| `report.language` | `ko` 또는 `en` — `prompts/<언어>.md` 를 고른다 |
| `day.boundary_hour` | 하루의 경계 |

## 안 될 때

`python3 doctor.py --full` 이 일곱 가지를 **원인 순서대로** 검사한다 — 스케줄러 등록,
설정, 실행 이력, 남은 잠금, Notion 접근, 디스크, 무인 인증. 판정만 내지 않고
**무엇을 봤는지**를 같이 출력하므로 첫 실패가 대개 원인이다.

## 요약 엔진

보고서를 쓰는 CLI 를 고른다. 수집기는 어느 쪽이든 상관없다 — 이 설정은 요약만
결정한다.

```toml
[summary]
engine = "claude"   # 또는 "codex"
codex_bin = ""      # 자동 탐지 실패 시 전체 경로
codex_model = ""    # 비우면 ~/.codex/config.toml 의 선택을 따른다
```

| | Claude Code | Codex |
|---|---|---|
| 호출 | `claude -p` | `codex exec --ephemeral -o <파일> -` |
| 프롬프트 | stdin | stdin |
| 답변 | stdout | `-o` 파일 |

**둘 다 프롬프트를 stdin 으로 받는다.** 바쁜 하루의 digest 는 175KB 쯤 되고
Windows 명령줄 상한은 32,767자다. 인자로 넘기는 방식은 서서히 나빠지는 게 아니라
그냥 실패한다.

Codex 는 답변을 stdout 이 아니라 `-o` 파일에서 읽는다. `codex exec` 의 stdout 은
배너·프롬프트 에코·답변·토큰 수가 섞인 세션 로그라, 거기서 보고서를 뽑으려면 산문을
파싱해야 한다. 같은 이유로 **실패 메시지에 codex 의 stdout 은 인용하지 않는다** —
에코된 프롬프트가 곧 digest 라 앞을 자르든 뒤를 자르든 그날 수집한 내용이 로그와
알림으로 샌다. `claude -p` 는 에코가 없어 그쪽은 stdout 을 인용한다.

`--ephemeral` 은 선택이 아니다. 없으면 요약 세션이 `~/.codex/sessions` 에 남아
다음 날 자기 자신을 수집한다. Claude 쪽은 제외된 스크래치 디렉터리에서 돌려 같은
결과를 얻지만, Codex 롤아웃은 작업 디렉터리와 무관하게 기록되므로 명시해야 한다.

**Codex 는 PATH 에 아무것도 안 남기는 설치가 많다.** 자동 탐지는 PATH, npm,
`~/.local/bin`, 그리고 Windows 데스크톱 설치의
`%LOCALAPPDATA%\OpenAI\Codex\bin\<빌드해시>\codex.exe` 를 훑는다. 빌드 해시는
버전마다 바뀌고 여러 개가 함께 있을 수 있어 최신 것을 고른다. 못 찾으면
`codex_bin` 에 전체 경로를 넣는다.

`doctor` 는 **설정된 엔진만** 점검한다. 이 통합은 codex 0.147 계열에서 확인했고,
필요한 옵션이 설치된 codex 에 없으면 어느 옵션이 왜 필요한지 이름을 대고 멈춘다 —
`codex exec --help` 를 읽어 확인하므로 모델 호출이 들지 않는다.

## 프라이버시

세션 로그에는 자격증명이 들어 있다. 여기서 실제 프롬프트와 셸 명령 30일치를 훑었더니
살아 있는 API 토큰이 평문으로 나왔다. **명령줄에 토큰을 한 번이라도 붙여 넣은 사람은
자기 로그에 그걸 갖고 있다.**

- **요약을 위해 digest 는 선택한 엔진의 API 로 전송된다.** 보고서를 `claude -p` 나
  `codex exec` 가 쓰기 때문에, 살균을 거친 digest 전체 — 프롬프트·셸 명령·파일 경로 —
  가 해당 CLI 를 통해 모델에게 간다. `engine = "claude"` 면 Anthropic API 로,
  `engine = "codex"` 면 OpenAI API 로 간다. 기기 밖으로 아예 나가지 않는 것은
  `exclude.paths` 로 처음부터 수집하지 않은 것뿐이다.
- **Notion 에 게시되는 건 모델이 쓴 산문뿐이다.** 원본 프롬프트와 명령은 올라가지 않는다.
- 살균은 두 번 돈다 — 모델이 보기 전의 digest 에, 그리고 Notion 에 쓰기 전의 보고서에.
- `exclude.paths` 는 **수집 자체를 막는다.** 정규식으로 가리는 것보다 강하다.
  대외비 폴더는 여기 넣는다.
- `work/`, `state/`, `logs/`, `.env` 는 gitignore 된다. `work/` 에는 프롬프트 원문이 있다.
- `scripts/check_no_pii.py` 가 공개 전에 저장소를 훑는다. **자기 탐지 결과도 마스킹해서
  출력한다** — 안 그러면 유출 방지 장치가 유출원이 된다.

## 지원 플랫폼

**macOS 와 Windows 10/11** 이 구현·검증돼 있다. Linux 는 시작 시점에 "지원 안 함" 을
분명히 던진다. **새벽 4시에 조용히 안 도는 것보다 낫다.**

`platform_support.py` 가 플랫폼에 딸린 것 전부를 클래스 하나 뒤로 몰아넣는다.

| | macOS | Windows |
|---|---|---|
| 스케줄러 | launchd (`.plist`) | 작업 스케줄러 (`.xml`) |
| 밀린 날짜 | 놓친 일정을 한 번으로 합침 | `StartWhenAvailable` |
| 단일 실행 | `fcntl.flock` | `msvcrt.locking` |
| 워치독 | `signal.alarm` (실행 시간) | 스레드 타이머 (벽시계) |
| 절전 방지 | `caffeinate` 래퍼 | `SetThreadExecutionState` |
| 알림 | `osascript` | 토스트, 실패 시 풍선 도움말 |
| 권한 | `chmod 700` | `icacls` + 상속 |
| 자격증명 | 로그인 키체인 (`USER` 필요) | DPAPI (대화형 토큰 필요) |

> **"수집·처리 본체는 이식 가능하다" 는 말은 사실이 아니었다.** Windows 를 붙이면서
> `platform_support.py` 밖에서 결함 일곱 개가 나왔고, 그중 둘은 **작업이 매일 정상 종료하면서
> "활동 없음" 이라고 쓰는** 종류였다. 자세한 내용은
> [개발 노트](docs/development-notes/windows_port_development_note.md).

플랫폼 추가는 그 클래스를 구현하고 **예약 실행이 실제로 뜨는지 확인하는** 일이다.

## 테스트

```bash
python3 -m pytest tests/ -q
```

각 테스트는 **실제로 한 번씩 터졌던 결함**에 대응한다. 임시 홈 디렉터리, 진짜 git 저장소,
손으로 쓴 전사본 같은 합성 픽스처로 돌아가므로, 이 도구를 한 번도 안 돌려 본 기기에서도 통과한다.

## 파일

| 파일 | 역할 |
|---|---|
| `run_day.py` | 파이프라인 · 백필 · 잠금 · 워치독 |
| `collect.py` | Claude Code 전사본 + git 커밋 |
| `collect_codex.py` | Codex 롤아웃 |
| `collect_fs.py` | 디스크에서 바뀐 파일 |
| `refine.py` | 프로젝트 롤업 · 노이즈 제거 · 병합 |
| `project_roots.py` | 작업 디렉터리 → 프로젝트 |
| `sanitize.py` | 자격증명 마스킹 |
| `summarize.py` | 무인 요약 |
| `notion_upsert.py` | 날짜 키 upsert |
| `notion_schema.py` | 속성 이름 — 만드는 쪽과 쓰는 쪽이 공유 |
| `setup_notion_db.py` | 데이터베이스 생성 (최초 1회) |
| `doctor.py` | 진단 |
| `platform_support.py` | OS 의존 부분 전부 |
| `config.py` | 설정 로딩, 논리적 날짜 |
| `paths.py` | 배포되는 자원과 쓰이는 데이터의 분리 |
| `status_window.py` | 상태·진단 창 (tkinter, 표준 라이브러리만) |
| `setup_gui.py` | 설치 마법사 — 값만 모아 `install.ps1` 에 넘긴다 |
| `cli.py` · `gui.py` · `daily-report.spec` | 패키징 진입점과 PyInstaller 스펙 |
| `install.sh` | macOS 최초 설치. 다시 돌려도 안전 |
| `install.ps1` | Windows 최초 설치. 다시 돌려도 안전 |
| `config.example.toml` | macOS 기본 설정 |
| `config.windows.example.toml` | Windows 기본 설정 |
| `templates/launchagent.plist.template` | launchd 작업 정의 |
| `templates/schtasks.xml.template` | 작업 스케줄러 작업 정의 |

## 알려진 한계

- **보고서 말고는 전부 한국어다.** `report.language`(`ko`/`en`)가 정하는 건 **보고서 본문의
  언어뿐**이다. 설치 스크립트 둘, `doctor.py`, 런타임 메시지, 그리고 창 둘
  (`status_window.py`, `setup_gui.py`)은 한국어만 있다 — 사용자에게 보이는 문자열 348개.
  설정과 기기 탐지는 로케일과 무관하지만 **문구는 그렇지 않다.**
- **Linux 미구현** — [지원 플랫폼](#지원-플랫폼) 참고.
- **Windows 워치독은 벽시계 시간을 센다.** macOS 의 `signal.alarm` 은 프로세스가 실제로
  돈 시간만 세므로 잠들었다 깬 실행을 죽이지 않는다. Windows 에는 대응물이 없어서,
  실행 내내 절전 금지를 걸어 두는 것으로 대신한다. 강제로 잠들면 그날은 중단되고
  다음 실행에서 백필된다.
- **Windows 작업은 로그온해 있어야 돈다.** 잠금 화면은 로그온 상태로 친다. 로그아웃하거나
  꺼져 있던 날은 `StartWhenAvailable` 과 장부로 복구한다.
- **두 에이전트 밖에서 한 작업은 관찰만 되고 귀속되지 않는다.** 디스크에서 바뀐 파일은
  산출물로 보고하되 "누가 만들었다" 고 단정하지 않는다.

## 문서

- [docs/design.ko.md](docs/design.ko.md) — 왜 이렇게 만들었는지, 그 근거가 된 실측값
- [docs/notion-setup.ko.md](docs/notion-setup.ko.md) — 연결 생성과 토큰 발급 절차
- [skills/daily-report/SKILL.md](skills/daily-report/SKILL.md) — 대화로 운영하는 Claude Code 스킬

## 라이선스

MIT
