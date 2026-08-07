"""One command that answers "is this working, and if not, why".

Ordered so the first FAIL is usually the cause: the scheduler has to be
registered before it can run, the job has to be authenticated before it can
summarize, and Notion has to be reachable before anything lands. Each check
prints what it looked at, so the output is evidence rather than a verdict.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402
import notion_schema  # noqa: E402
import platform_support  # noqa: E402
import run_day  # noqa: E402
import summarize  # noqa: E402
from notion_upsert import load_env  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
LABEL = config.load().get("launchd", {}).get("label", "com.example.daily-report")
SCHEDULER_FILE = platform_support.PLATFORM.scheduler_path(LABEL)
LOG_TAIL_LINES = 12

OK, WARN, FAIL = "✅", "⚠️ ", "❌"
problems: list[str] = []

# Recovery hints are meant to be pasted, so they have to be runnable on the
# machine reading them.
PY = f'"{sys.executable}"' if os.name == "nt" else "python3"
EXAMPLE = os.path.basename(config.EXAMPLE_PATH)


def report(status: str, title: str, detail: str = "") -> None:
    print(f"{status} {title}")
    if detail:
        for line in detail.splitlines():
            print(f"     {line}")
    if status == FAIL:
        problems.append(title)


def check_scheduler() -> None:
    """Registration first: nothing else matters if it never fires.

    The registration *file* is checked separately from the scheduler's own
    answer because the two can disagree — a definition on disk that was never
    loaded looks installed to anyone reading the folder.
    """
    repair = platform_support.PLATFORM.scheduler_repair(LABEL)
    if not os.path.exists(SCHEDULER_FILE):
        report(FAIL, "스케줄러 등록",
               f"등록 파일 없음: {SCHEDULER_FILE}\n복구: {repair}")
        return
    registered, detail = platform_support.PLATFORM.scheduler_status(LABEL)
    if not registered:
        report(FAIL, "스케줄러 등록", f"{detail}\n복구: {repair}")
        return
    # Registered but mis-registered: the job runs and fails the same way every
    # night. macOS loses USER, Windows loses the interactive token — the same
    # defect wearing two hats, and neither reports anything on its own.
    if "⚠️" in detail:
        report(FAIL, "스케줄러 설정", detail)
        return
    report(OK, "스케줄러 등록", detail)


def check_config() -> None:
    """Settings that are syntactically fine but produce a useless report."""
    cfg = config.load()
    if config.using_example():
        report(FAIL, "설정 파일",
               f"config.toml 이 없어 예시 설정으로 돌고 있습니다 ({config.EXAMPLE_PATH})\n"
               f"복구: {EXAMPLE} 를 config.toml 로 복사하세요")
    authors = [a for a in cfg.get("git", {}).get("authors", []) if a and a.strip()]
    if not authors:
        report(WARN, "git 저자 목록",
               "비어 있어 커밋을 전혀 수집하지 않습니다.\n"
               "config.toml 의 [git] authors 에 본인 이메일을 넣으세요:\n"
               "  git log --format='%ae' | sort | uniq -c | sort -rn | head")
    else:
        report(OK, "git 저자 목록", f"{len(authors)}개 등록됨")

    check_repo_discovery(cfg)

    language = cfg.get("report", {}).get("language", "ko")
    prompt = os.path.join(HERE, "prompts", f"{language}.md")
    if os.path.exists(prompt):
        report(OK, "보고서 프롬프트", f"{language} ({os.path.getsize(prompt):,} bytes)")
    else:
        report(FAIL, "보고서 프롬프트", f"{prompt} 가 없습니다")


def check_repo_discovery(cfg: dict) -> None:
    """Does the configured search root actually contain any repositories?

    `git_search_root` defaults to the home directory, which is right on macOS
    and often wrong on Windows, where projects commonly live on a second drive
    (`D:\\work`). Finding nothing there is not an error anywhere in the
    collector — every day simply reports zero commits, which reads exactly like
    a quiet fortnight. Nothing else in this tool would ever tell you.
    """
    import collect  # local: importing it pulls in the whole collection stack

    root = config.expand(cfg["sources"]["git_search_root"])
    started = time.time()
    try:
        repos = collect.find_repos()
    except OSError as error:
        report(FAIL, "저장소 탐색", f"{root} 를 훑지 못했습니다: {error}")
        return
    elapsed = time.time() - started

    if not repos:
        report(FAIL, "저장소 탐색",
               f"{root} 아래에서 git 저장소를 하나도 찾지 못했습니다 ({elapsed:.1f}초)\n"
               f"커밋은 영원히 0 으로 나오고, 그건 조용한 2주와 구별되지 않습니다.\n"
               f"config.toml 의 [sources] git_search_root 를 실제 작업 폴더로 바꾸세요"
               + (" (예: \"D:/work\")" if os.name == "nt" else "") + "\n"
               f"제외된 트리 안에 있다면 [sources] extra_repo_roots 에 직접 적으세요.")
        return
    detail = f"{len(repos)}개 ({elapsed:.1f}초, 루트 {root})"
    if elapsed > 60:
        report(WARN, "저장소 탐색", detail + "\n탐색이 느립니다 — walk_exclude 를 넓히세요")
    else:
        report(OK, "저장소 탐색", detail)


def check_last_run() -> None:
    state = run_day.read_state()
    completed = state.get("completed", {})
    real = {k: v for k, v in completed.items() if not v.get("skipped")}
    if not real:
        report(WARN, "실행 이력", "성공 기록이 없습니다 (아직 한 번도 안 돌았을 수 있음)")
        return
    latest = max(real)
    entry = real[latest]
    when = entry.get("at", "?")
    detail = (f"마지막 처리 날짜: {latest}  (실행 시각 {when})\n"
              f"프로젝트 {entry.get('projects','?')}개 · 세션 {entry.get('sessions','?')} · "
              f"파일 {entry.get('files','?')} · 커밋 {entry.get('commits','?')} · "
              f"보고서 {entry.get('report_chars','?')}자")
    findings = entry.get("digest_findings") or {}
    if findings:
        detail += f"\n살균 탐지: {findings}"
    report(OK, "실행 이력", detail)

    pending = run_day.pending_days(state)
    if pending:
        report(WARN, "밀린 날짜", f"{len(pending)}일: {', '.join(pending)}\n"
                                 f"처리: {PY} {os.path.join(HERE, 'run_day.py')}")
    else:
        report(OK, "밀린 날짜", "없음")


def check_stale_lock() -> None:
    lock = run_day.LOCK_PATH
    if not os.path.exists(lock):
        report(OK, "실행 잠금", "잠금 없음")
        return
    held = platform_support.PLATFORM.lock_is_held(lock)
    if held is None:
        report(WARN, "실행 잠금", "잠금 상태를 확인할 수 없습니다")
    elif held:
        report(WARN, "실행 잠금", "다른 실행이 잠금을 쥐고 있습니다 (정상일 수 있음)")
    else:
        report(OK, "실행 잠금", "잠금 파일은 있으나 아무도 쥐고 있지 않음 (정상)")


def check_tooling() -> None:
    """Can this process actually start the two binaries it shells out to?

    Both are found through PATH, and a scheduled task does not get the PATH a
    login shell has. A missing `git` is not an error anywhere in the collector
    — every repository simply fails and the day looks quiet — so it is checked
    here instead.
    """
    try:
        result = subprocess.run(["git", "--version"], capture_output=True, timeout=20)
        version = result.stdout.decode("utf-8", errors="replace").strip()
        if result.returncode != 0:
            # A stub that exists and refuses to run looks like a working git to
            # anything that only checks whether the command was found.
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            report(FAIL, "git", f"git 이 exit {result.returncode} 로 끝났습니다: {detail[:120]}")
        else:
            report(OK, "git", version or "확인됨")
    except (subprocess.SubprocessError, OSError):
        report(FAIL, "git", "git 을 찾지 못했습니다 — 커밋이 하나도 수집되지 않습니다")

    # Resolve the CLI the same way the summarizer will. A check that cannot
    # pass is a check people learn to ignore, so "found on PATH" is a pass.
    argv = summarize.claude_argv()
    target = argv[-1]
    resolved = target if os.path.isabs(target) else (
        shutil.which(target, path=summarize.REQUIRED_PATH) or "")
    if resolved and os.path.isfile(resolved):
        report(OK, "Claude Code CLI", resolved)
    else:
        report(FAIL, "Claude Code CLI",
               f"{target} 을 찾지 못했습니다 (PATH: {summarize.REQUIRED_PATH})\n"
               f"config.toml 의 [summary] claude_bin 에 전체 경로를 넣으세요.")


def check_auth() -> None:
    ok, message = summarize.preflight()
    if ok:
        report(OK, "헤드리스 인증", "claude -p 정상")
    else:
        report(FAIL, "헤드리스 인증", f"{message}\n"
                                     "USER 환경변수 누락이면 plist 를 확인하세요")


def check_notion() -> None:
    try:
        env = load_env(os.path.join(HERE, ".env"))
    except FileNotFoundError as error:
        report(FAIL, "Notion 설정", str(error))
        return
    token = env.get("DAILY_REPORT_NOTION_TOKEN", "")
    database_id = env.get("DAILY_REPORT_DATABASE_ID", "")
    if not token or not database_id:
        report(FAIL, "Notion 설정", ".env 에 토큰 또는 데이터베이스 ID 가 비어 있습니다")
        return

    request = urllib.request.Request(
        f"https://api.notion.com/v1/databases/{database_id}",
        headers={"Authorization": f"Bearer {token}", "Notion-Version": "2026-03-11"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        hint = {401: "토큰이 만료됐거나 잘못됨",
                404: "연결이 데이터베이스에 공유되어 있지 않음 (••• > Add connections)",
                403: "권한 부족 — Configuration 에서 insert content 확인"}.get(error.code, "")
        report(FAIL, "Notion 접근", f"HTTP {error.code} {hint}")
        return
    except OSError as error:
        report(FAIL, "Notion 접근", f"연결 실패: {error}")
        return

    title = "".join(t.get("plain_text", "") for t in data.get("title", []))
    report(OK, "Notion 접근", f"데이터베이스 '{title}'  "
                             f"공개URL: {data.get('public_url') or '없음(비공개)'}")
    check_schema(token, data.get("data_sources") or [])


def check_schema(token: str, sources: list[dict]) -> None:
    """The writer queries by property name, and a miss is not an error.

    A renamed or missing date property makes the lookup find nothing, so every
    run creates a new row instead of updating one. Nothing fails, and the
    duplicates are only noticed days later.
    """
    if not sources:
        report(WARN, "Notion 속성", "data_source 를 찾지 못했습니다")
        return
    request = urllib.request.Request(
        f"https://api.notion.com/v1/data_sources/{sources[0]['id']}",
        headers={"Authorization": f"Bearer {token}", "Notion-Version": "2026-03-11"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            detail = json.loads(response.read().decode())
    except (urllib.error.HTTPError, OSError) as error:
        report(WARN, "Notion 속성", f"조회 실패: {error}")
        return

    live = set(detail.get("properties", {}))
    expected = set(notion_schema.props().values())
    missing = sorted(expected - live)
    if missing:
        report(FAIL, "Notion 속성",
               f"데이터베이스에 없는 속성: {', '.join(missing)}\n"
               f"현재 스키마 언어: {notion_schema.language()} "
               f"([notion] schema_language)\n"
               "이름이 어긋나면 날짜 조회가 빗나가 매일 새 행이 쌓입니다. "
               "노션에서 속성 이름을 맞추거나 설정을 되돌리세요.")
        return
    report(OK, "Notion 속성", f"{len(expected)}개 일치 (언어 {notion_schema.language()})")


def check_disk() -> None:
    total = 0
    count = 0
    for name in os.listdir(run_day.WORK_DIR) if os.path.isdir(run_day.WORK_DIR) else []:
        path = os.path.join(run_day.WORK_DIR, name)
        if os.path.isfile(path):
            total += os.path.getsize(path)
            count += 1
    logs = 0
    for name in os.listdir(run_day.LOG_DIR) if os.path.isdir(run_day.LOG_DIR) else []:
        path = os.path.join(run_day.LOG_DIR, name)
        if os.path.isfile(path):
            logs += os.path.getsize(path)
    status = WARN if logs > 50 * 1024 * 1024 else OK
    report(status, "디스크",
           f"work/ {count}개 {total/1024/1024:.1f} MB (보존 {run_day.WORK_RETENTION_DAYS}일)  "
           f"logs/ {logs/1024/1024:.1f} MB")


def show_logs() -> None:
    for name in ("stdout.log", "stderr.log"):
        path = os.path.join(run_day.LOG_DIR, name)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            print(f"\n--- {name}: 비어 있음 ---")
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        print(f"\n--- {name} (마지막 {min(len(lines), LOG_TAIL_LINES)}줄) ---")
        for line in lines[-LOG_TAIL_LINES:]:
            print(f"  {line}")


def main() -> int:
    platform_support.PLATFORM.configure_stdio()
    now = datetime.now(config.local_tz())
    if not platform_support.PLATFORM.supported:
        print(f"⚠️  {platform_support.PLATFORM.name} 는 아직 지원하지 않습니다 "
              f"(지원: macOS, Windows)")
    print(f"하루 마감 보고서 상태 점검  ({now:%Y-%m-%d %H:%M}, "
          f"{platform_support.PLATFORM.name})")
    print(f"논리적 오늘: {config.logical_date(now)}  (하루 경계 "
          f"{config.load()['day']['boundary_hour']:02d}:00)\n")

    check_scheduler()
    check_config()
    check_tooling()
    check_last_run()
    check_stale_lock()
    check_notion()
    check_disk()
    if "--auth" in sys.argv or "--full" in sys.argv:
        check_auth()      # spends a real CLI call, so opt-in
    else:
        print(f"{OK} 헤드리스 인증  (--auth 로 실제 확인)")

    if "--logs" in sys.argv or "--full" in sys.argv or problems:
        show_logs()

    print()
    if problems:
        print(f"{FAIL} 문제 {len(problems)}건: {', '.join(problems)}")
        return 1
    print(f"{OK} 이상 없음")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
