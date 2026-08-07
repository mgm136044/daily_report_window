# 윈도우 이식 — "본체는 이식 가능하다" 가 틀렸던 이야기

**날짜**: 2026-08-07
**유형**: 기능 추가 (플랫폼 지원)

## 무슨 일이 있었나

`platform_support.py` 는 처음부터 이식을 염두에 두고 쓰여 있었다. 모듈 도입부가 이렇게 말한다.

> 수집·처리 본체 — 세션 로그 읽기, 프로젝트 롤업, 살균, 요약, 노션 적재 — 는 **이미 이식 가능하다.** 이식 불가능한 것은 실행 껍데기다. (…) 플랫폼을 추가하려면 아래에 클래스를 하나 구현하고 `current()` 에 등록하면 된다. **이 모듈 밖은 아무것도 바꿀 필요가 없다.**

`Windows` 클래스 독스트링은 한 발 더 나아가, 필요한 조각을 이미 다 나열해 두고 있었다 — `schtasks`, `msvcrt.locking`, 스레드 타이머, PowerShell 알림, `SetThreadExecutionState`, `icacls`. 그리고 왜 안 넣었는지도 적어 뒀다.

> 이 프로젝트에서 나온 심각한 결함은 전부 **문서를 읽어서가 아니라 실제로 돌려서** 나왔다.

이 문장이 맞았다. 다만 예상과 다른 방향으로 맞았다.

윈도우 기기에서 기존 테스트 스위트를 그대로 돌리는 것으로 시작했다. **80개 중 14개가 실패했다.** 그중 `platform_support.Windows` 가 미구현이라서 실패한 것은 3개뿐이었다. 나머지 11개는 전부 **"이 모듈 밖은 아무것도 바꿀 필요가 없다" 고 적힌 그 바깥**이었다.

## 본체에서 나온 결함 일곱 개

### 1. 모든 프로젝트가 사라진다 — 그리고 아무것도 실패하지 않는다

```python
if not cwd or not cwd.startswith("/") or is_excluded(cwd):
    return None
```

`project_roots.project_root()` 의 첫 줄이다. 세션 기록이 `D:\work\app` 을 담고 있으면 이 검사가 그것을 **상대 경로로 판정하고 버린다.** 기기의 모든 세션이 여기서 죽는다.

같은 함수의 순회 종료 조건도 같은 가정 위에 있었다.

```python
while current and current != "/":
```

`os.path.dirname("D:\\")` 는 `"D:\\"` 다. 윈도우 경로는 `"/"` 에 **절대 닿지 않는다.** 설령 첫 검사를 통과했더라도 순회는 드라이브 루트까지 올라가 바닥으로 빠져나가며 `None` 을 돌려준다.

문제는 이게 **실패로 보이지 않는다**는 점이다. 수집이 돌고, 정제가 돌고, 살균이 돌고, `claude -p` 가 호출되고, 노션에 행이 생긴다. 종료 코드는 0 이다. 보고서에는 "활동 없음" 이라고 적혀 있다. 매일.

`platform_support.py` 가 존재하는 이유는 **조용히 안 도는 예약 작업**을 막기 위해서였다. 그 실패가 아무도 안 보던 문으로 들어왔다.

### 2. 제외 목록이 통째로 꺼진다

```python
probe = path if path.endswith("/") else path + "/"
return any(fragment in probe for fragment in load()["exclude"]["paths"])
```

목록은 `/node_modules/`, `/.git/`, `/private/tmp/` 처럼 **슬래시로** 쓰여 있다. 역슬래시 경로에 대고 부분문자열 검사를 하면 **하나도 안 걸린다.**

이것도 실패처럼 보이지 않는다. 제외할 게 없는 깨끗한 기기처럼 보인다. 실제로는 요약기가 자기 전사본을 남기는 임시 폴더를 막던 규칙까지 같이 꺼져서, **작업이 매일 밤 자기 자신의 요약 작업을 보고**하게 된다.

`config.probe()` 로 경로를 정규화(구분자 통일 + 소문자화)한 뒤 비교하도록 고쳤다. 윈도우 경로는 대소문자를 구분하지 않으므로 소문자화도 함께 필요하다 — `D:\Dev\app` 과 `d:\dev\app` 은 같은 디렉터리다.

### 3. 한글 커밋 제목 하나가 저장소를 통째로 날린다

```python
subprocess.run([...], capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC)
```

`text=True` 는 **프로세스 로케일**로 디코딩한다. 한국어 윈도우에서는 cp949 다. git 이 UTF-8 로 내놓은 한글 커밋 제목이 들어오면 `UnicodeDecodeError` 가 나는데, 그게 `subprocess` 의 **리더 스레드 안에서** 터진다. 호출한 쪽에서는 예외가 아니라 그냥 이상한 결과로 보인다.

테스트 스위트 첫 실행 로그에 실제로 찍혔다.

```
UnicodeDecodeError: 'cp949' codec can't decode byte 0xec in position 634
```

바이트로 받아서 UTF-8 로 명시적으로 디코딩하도록 바꿨다. 겸사겸사 `-c core.quotepath=false` 를 붙였다 — 기본값은 비ASCII 경로를 `\355\225\234` 같은 8진 이스케이프로 내놓는데, **이건 macOS 에서도 한글 파일명을 망가뜨리고 있던 잠재 결함이었다.**

### 4. git 경로와 walk 경로가 영원히 안 맞는다

git 은 항상 슬래시로 답한다. 윈도우 저장소 경로에 이어 붙이면 `D:\repo\docs/a.md` 가 나오는데, `os.walk` 가 내놓은 `D:\repo\docs\a.md` 와 같지 않다.

`collect_fs` 는 이 비교로 **"추적 중이고 깨끗한" 파일**을 걸러낸다. 비교가 늘 빗나가면 `git pull` 이 건드린 파일이 전부 그날의 작업으로 보고된다. `_git_path()` 헬퍼를 만들어 `os.path.normpath` 를 통과시켰다.

### 5. 보고서를 만든 **다음에** 크래시

```python
source_path=os.path.relpath(digest_path, os.path.expanduser("~")),
```

`os.path.relpath` 는 드라이브가 다르면 값을 돌려주는 대신 `ValueError` 를 던진다. 이 도구는 `D:` 에 두고 홈은 `C:` 에 있는 게 흔한 배치다.

가장 비싼 결함이다. 수집·정제·살균이 끝나고 **모델 호출까지 지불한 뒤** 노션에 쓰기 직전에 터진다. `source_reference()` 로 분리해서 홈 기준 → 프로젝트 기준 → 절대 경로 순으로 후퇴하게 했다.

### 6. 첫 줄에서 죽는 로그

예약 실행은 stdout 을 파일로 리다이렉트한다. 그 파일은 ANSI 코드페이지로 열린다. 이 도구가 출력하는 메시지는 **전부 한글**이고 `doctor.py` 는 이모지까지 쓴다. 첫 줄에서 `UnicodeEncodeError` 가 나고, 아무 작업도 하기 전에 실행이 끝난다.

`Windows.configure_stdio()` 가 콘솔 코드페이지를 65001 로 올리고 stdout/stderr 을 UTF-8 로 재설정한다. 예약 작업 인자에도 `-X utf8` 을 넣었다.

### 7. 작업 스케줄러에는 `StandardOutPath` 가 없다

plist 는 stdout/stderr 파일 경로를 선언할 수 있다. 작업 스케줄러의 `Exec` 액션은 못 한다 — 출력이 그냥 사라진다.

`cmd /c "… >> log"` 로 감싸는 대신 `run_day.py --log` 를 만들어 프로세스가 자기 로그 파일을 직접 연다. 인용 지옥을 피하고, 덤으로 **`pythonw.exe` 로 돌릴 수 있게 된다** — 매일 새벽 4시 5분에 콘솔 창이 번쩍이지 않는다. `pythonw` 는 stdout 이 아예 없으므로, 이 리다이렉션은 **무엇을 출력하기 전에** 일어나야 한다.

launchd 가 한 번도 필요로 하지 않았던 로그 회전도 여기 넣었다. macOS 에서는 무한히 자라는 로그라도 Console 에서 보이지만, 윈도우에서는 아무도 그걸 줄여 주지 않는다.

## 실행 껍데기: macOS 와 다르게 간 곳

| | macOS | Windows | 왜 |
|---|---|---|---|
| 스케줄러 | launchd plist | 작업 스케줄러 XML | `schtasks` 플래그로는 `StartWhenAvailable`·`WakeToRun`·배터리 설정에 닿을 수 없다 |
| 밀린 날짜 | 놓친 일정을 **한 번으로 합침** | `StartWhenAvailable` | 작업 스케줄러의 기본값은 **아예 건너뛰는 것**이다. 켜 두지 않으면 장부에 백필할 근거 자체가 생기지 않는다 |
| 잠금 | `fcntl.flock` | `msvcrt.locking` 바이트 범위 | `"w"` 가 아니라 `"a+"` 로 연다. 잠금을 잡는 건 truncate 가 아니다 |
| 워치독 | `signal.alarm` | 스레드 타이머 + `PyThreadState_SetAsyncExc` | 아래 참고 |
| 절전 방지 | `caffeinate` 래퍼 | `SetThreadExecutionState` | 감쌀 바이너리가 없다. 프로세스 안에서 직접 건다 |
| 알림 | `osascript` | 토스트 → 실패 시 풍선 | WinRT 토스트는 시작 메뉴에 등록된 AppUserModelID 를 요구하는데 보장되지 않는다. **조용히 실패하는 알림은 알림이 아니다** |
| 스케줄러 조회 | `launchctl print` | `Get-ScheduledTask` | `schtasks /Query /V` 는 필드 이름이 지역화된다 — 한국어 설치본에서는 파싱 대상이 전부 한글이다 |
| 권한 | `chmod 700` | `icacls` + 상속 | `chmod` 는 파일당 공짜지만 `icacls` 는 프로세스 생성이다. 디렉터리에 한 번 걸고 파일은 상속시킨다 |

### 워치독이 세는 시간이 다르다

`signal.alarm` 은 **프로세스가 실제로 돈 시간**을 센다. 그래서 macOS 에서는 잠들었다 깬 실행을 워치독이 죽이지 않는다 — 설계 문서가 명시적으로 "그게 올바른 동작" 이라고 적어 둔 부분이다.

윈도우에는 대응물이 없다. 스레드 타이머는 벽시계다. 그래서 **실행 내내 절전 금지를 거는 것**(`keep_awake`)이 macOS 에서보다 더 중요해졌다. 그래도 강제로 잠들면 그날은 중단되고, 다음 실행에서 백필된다. `config.windows.example.toml` 과 README 에 이 차이를 적어 뒀다.

`PyThreadState_SetAsyncExc` 는 다음 바이트코드 경계에서 전달되므로 `subprocess.wait()` 에 묶인 스레드를 깨우지 못한다. 이 도구가 띄우는 모든 자식 프로세스가 자체 타임아웃을 갖고 있어서 받아들일 만하다 — 워치독은 자식 하나가 매달린 경우가 아니라 **실행 전체가 진척을 멈춘 경우**를 위한 것이다.

### 자격증명: `USER` 의 윈도우판

macOS 결함은 유명하다. plist 에 `USER` 가 없으면 CLI 가 로그인 키체인을 못 찾고 "Not logged in" 을 뱉는다.

윈도우판은 **작업의 로그온 유형**이다. "사용자의 로그온 여부에 관계없이 실행" 으로 등록하면 S4U 토큰을 받는데, 그 토큰으로는 DPAPI 로 보호된 CLI 자격증명을 풀 수 없다. 증상이 **완전히 똑같다** — 매일 밤 "Not logged in", 다른 건 전부 정상.

`InteractiveToken` 으로 고정하고, `doctor.py` 가 이걸 검사한다. 대가는 **로그온해 있어야 돈다**는 것이다. 잠금 화면은 로그온으로 친다. 로그아웃/전원 꺼짐은 아니고, 그 날들은 `StartWhenAvailable` 과 장부가 복구한다.

## 설치기에서 실제로 돌려 보고 나온 것 둘

### install.ps1 은 BOM 이 **있어야** 한다

파싱 검사를 돌렸더니 276번째 줄에서 `Unexpected token '}'` 가 났다. 그 줄에는 아무 문제도 없었다.

Windows PowerShell 5.1 은 스크립트 파일에 **기본 인코딩이 없다.** BOM 이 없으면 기기의 ANSI 코드페이지로 읽는다 — 여기서는 cp949 다. UTF-8 로 저장된 한글 문자열이 전부 깨지고, 깨진 바이트가 우연히 따옴표 짝을 어긋내면서 **파싱 자체가 실패**한다. 오류는 원인에서 200줄 떨어진 곳에 보고된다.

```
ANSI-decoded: Die "Windows ?꾩슜 ?ㅼ튂 ?ㅽ겕由쏀듃?낅땲?? macOS/Linux ??install.sh 瑜??곗꽭??"
```

UTF-8 BOM 을 붙여 해결했다. 회귀 테스트 두 개(BOM 존재, 실제 PowerShell 파서로 파싱)를 걸어 뒀다.

반대로 **`config.toml` 과 `.env` 는 BOM 이 있으면 안 된다.** `tomllib` 이 거부한다. PowerShell 5.1 의 `Set-Content -Encoding UTF8` 은 BOM 을 붙이므로, `Write-Utf8NoBom` 헬퍼를 따로 만들었다. 같은 파일 안에서 인코딩 요구가 정반대인 파일 두 종류를 다룬다.

### 작업 XML 은 `--` 를 담을 수 없다

XML 주석 안에는 하이픈 두 개가 연속으로 올 수 없다. 주석에 `` `--log` `` 라고 적었더니 작업 스케줄러가 정의 전체를 거부했다.

```
The task XML is malformed.
(87,40)::오류: 잘못된 주석 구문입니다.
```

`ET.fromstring()` 으로 템플릿을 파싱하는 테스트를 추가했다. **이 테스트는 macOS 에서도 돈다** — 순수한 텍스트 문제이고, 맥에서 이 파일을 고치는 사람은 다른 방법으로는 절대 알 수 없다.

## 기기에서 실제로 나온 설정 문제 둘

예시 설정을 만들고 나서 **진짜 데이터로 수집을 돌려 봤더니** 프로젝트 목록에 `문서` 가 있었다.

원인은 두 가지가 겹친 것이었다.

1. **지역화된 셸 폴더.** 한국어 윈도우는 `Documents` 와 `문서` 를 **둘 다** 갖고 있다. `바탕 화면`, `사진`, `시작 메뉴` 도 마찬가지고, 여기에 `Application Data`·`My Documents` 같은 **레거시 정션**까지 홈에 깔려 있다. 정션은 이미 순회한 트리를 다시 가리키므로, 매번 같은 트리를 두 번 훑거나 권한 오류를 낸다.

2. **OneDrive 알려진 폴더 이동.** 많은 설치본에서 기본으로 켜져 있고, 셸 폴더를 OneDrive **안으로** 옮긴다. 이 기기의 진짜 문서 폴더는 `~/OneDrive/문서` 였다. `~/OneDrive` 는 컨테이너이므로 그 직속 자식인 `문서` 가 프로젝트가 됐고, PowerShell 프로필을 한 줄 고친 것이 **"문서" 라는 프로젝트의 작업**으로 보고되고 있었다.

이름을 추측하는 대신 `install.ps1` 이 `[Environment]::GetFolderPath()` 로 **이 기기의 실제 경로를 물어보고** 설정에 써 넣도록 했다. 고친 뒤 같은 데이터를 다시 돌리니 프로젝트가 `daily-report-main` 과 `WindowsPowerShell` 로 정리됐다.

## 검증

전부 이 윈도우 기기에서 실제로 돌린 것이다.

| 항목 | 결과 |
|---|---|
| 테스트 스위트 | 98 통과 / 5 건너뜀 (macOS 전용) — 이식 전 66 통과 / 14 실패 |
| 수집 (실데이터) | 전사본 4개, 당일 레코드 364, 디스크 56개 |
| 프로젝트 롤업 | 고치기 전 `['.local', 'daily-report-main', '문서']` → 고친 뒤 `['daily-report-main', 'WindowsPowerShell']` |
| `doctor.py` | 7개 검사 전부 실행, 한글·이모지 정상 출력 |
| 무인 인증 | `summarize.py --preflight` → `ok` (실제 `claude -p` 호출) |
| 잠금 | 두 번째 획득 시도가 정상적으로 거부됨 |
| 워치독 | 1초 설정에서 1.1초 만에 발동 |
| 절전 방지 | `SetThreadExecutionState` 획득 성공 |
| 알림 | 토스트 표시됨 |
| `icacls` | 상속 제거 + 계정/SYSTEM/Administrators 부여 확인 |
| 작업 스케줄러 | 임시 이름으로 실제 등록 → `LogonType=Interactive`, `StartWhenAvailable=True`, `WakeToRun=True`, `NextRunTime=2026-08-08 04:05` 확인 후 등록 해제 |
| `install.ps1` | PowerShell 파서로 파싱 통과 |

**아직 검증되지 않은 것 하나**: 예약 실행이 실제로 새벽 4시 5분에 떠서 끝까지 도는 것. 등록·조회·수동 실행 경로는 전부 확인했지만, 실제 무인 실행은 하루가 지나야 알 수 있다. `platform_support.Windows` 독스트링의 원래 경고가 정확히 이것이었으므로 여기 남겨 둔다.

## 변경 파일

**실행 껍데기**
- `platform_support.py` — `Windows` 클래스 전체 구현, `Platform` 인터페이스에 `keep_awake`·`lock_is_held`·`scheduler_path`·`scheduler_repair`·`default_path`·`child_env`·`claude_argv`·`configure_stdio` 추가
- `templates/schtasks.xml.template` — 신규
- `install.ps1` — 신규 (UTF-8 BOM 필수)
- `config.windows.example.toml` — 신규

**본체 (이식 가능하다고 적혀 있던 부분)**
- `project_roots.py` — `os.path.isabs`, 앵커 기반 순회 종료, 대소문자 무시 비교
- `config.py` — `probe()`·`key()`·`_fragments()`, 플랫폼별 예시 설정 선택
- `collect.py` — git 바이트 디코딩, `core.quotepath=false`, 경로 정규화
- `collect_fs.py` — 같음, `_git_path()`, 앵커 기반 순회
- `run_day.py` — `source_reference()`, `redirect_output()`, `keep_awake` 감싸기, `configure_stdio()`
- `summarize.py` — 플랫폼에서 자식 환경·CLI 경로 받아오기, UTF-8 파이프
- `doctor.py` — 플랫폼별 스케줄러 경로·복구 안내, 잠금 조회, `check_tooling()`
- `notion_upsert.py` — 플랫폼별 안내 문구
- `config.example.toml` — `summary.claude_bin` 추가

**테스트**
- `tests/conftest.py` — `STRUCTURAL_EXCLUSIONS`. 합성 픽스처가 시스템 임시 폴더 안에 사는데, 윈도우에서는 그게 실제 설정이 제외하는(그리고 제외해야 하는) 트리 안이다
- `tests/test_fixes.py` — 플랫폼별 표시 추가, 윈도우 회귀 테스트 18개

**문서**
- `README.md`, `README.ko.md` — 지원 플랫폼 표, 설치, 알려진 한계
- `docs/design.md`, `docs/design.ko.md` — "이식 가능하다" 주장 철회와 그 근거
