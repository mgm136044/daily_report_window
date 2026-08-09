"""Run the full pipeline for one day, or backfill every day still missing.

Backfill is a first-class feature, not a convenience. launchd coalesces missed
calendar runs into exactly one execution, so a Mac that was off for three days
comes back and fires once. Without a lastrun ledger, the other two days are
gone permanently.
"""

from __future__ import annotations

import json
import os
import re
import time
import sys
import traceback
from datetime import datetime, timedelta

import paths  # noqa: E402 — needed before the log redirect below

# Everything here is written, so it all follows the data root. From a checkout
# that is the source directory, exactly as before; frozen, it is a directory
# that outlives the process — a bundle's extraction directory is deleted on
# exit, which would silently discard the ledger after every single run.
HERE = paths.data_root()
STATE_DIR = paths.data("state")
WORK_DIR = paths.data("work")
LOG_DIR = paths.data("logs")
LASTRUN_PATH = os.path.join(STATE_DIR, "lastrun.json")
LOCK_PATH = os.path.join(STATE_DIR, "run.lock")

MAX_BACKFILL_DAYS = 14
FIRST_RUN_DAYS = 1
SUMMARY_SENTENCES = 3
WORK_RETENTION_DAYS = 14
STATE_RETENTION_DAYS = 120
LOG_MAX_BYTES = 8 * 1024 * 1024


_redirected = False


def redirect_output() -> None:
    """Bind stdout and stderr to logs/, the way the macOS plist does.

    Idempotent, because two entry points reach it and only one of them can
    reach it early. See the call sites at the bottom of the imports and in
    `main()`.

    A Task Scheduler Exec action has no StandardOutPath — its output simply
    goes nowhere. Doing the redirection here rather than wrapping the command
    in `cmd /c … >> log` keeps the quoting sane and, more usefully, lets the
    job run under pythonw.exe so nothing flashes a console window at 4 a.m.
    pythonw has no stdout at all, so this has to happen before anything prints.

    Called at **import time**, above the local imports below, for the same
    reason: a failure while importing the collector would otherwise produce a
    non-zero exit code and not one byte anywhere explaining it.

    Also rotates, which launchd never needed: on macOS a log this job appends
    to forever is at least visible in Console. Here nothing else would trim it.
    """
    global _redirected
    if _redirected:
        return
    _redirected = True

    paths.ensure_data_root()
    os.makedirs(LOG_DIR, exist_ok=True)
    for name, attribute in (("stdout.log", "stdout"), ("stderr.log", "stderr")):
        path = os.path.join(LOG_DIR, name)
        try:
            if os.path.getsize(path) > LOG_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        # line buffered, so a hung run still shows how far it got
        handle = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
        setattr(sys, attribute, handle)
    print(f"\n===== {datetime.now().isoformat(timespec='seconds')} =====")


def say(message: str) -> None:
    """Print progress that must never be able to abort the run.

    Whoever is reading this output may stop reading. The status window runs
    the job as a child and reads its stdout through a pipe; closing the window
    closes the read end, and the next write raises. The report was already
    published by then, so letting that exception out turned a finished day
    into a recorded failure.

    Only for lines that are commentary on work already done. Anything whose
    failure should stop the run has no business going through here.
    """
    try:
        print(message)
    except OSError:
        pass


# Guarded on __main__ as well as the flag: doctor.py and the tests import this
# module, and neither should have its output diverted because of whatever
# happens to be on their own command line.
#
# This is the *early* call, and it only fires for `python run_day.py --log`,
# where being above the imports below means an import failure still lands in
# the log. The packaged build cannot reach it — there `cli.py` imports this
# module, so __name__ is "run_day" — and `main()` calls again for that case.
# Missing the second call is how the frozen scheduled task ran with its output
# going nowhere at all: no console under pythonw, and an empty logs/.
if __name__ == "__main__" and "--log" in sys.argv:  # noqa: E402
    redirect_output()

import collect  # noqa: E402
import config  # noqa: E402
import notion_schema  # noqa: E402
import platform_support  # noqa: E402
import refine  # noqa: E402
import sanitize  # noqa: E402
import summarize  # noqa: E402
from notion_upsert import load_env, upsert  # noqa: E402


def ensure_dirs() -> None:
    """Create the working directories owner-only.

    `work/` holds the raw digest, which contains verbatim user prompts and full
    filesystem paths, and `logs/` is written by launchd with world-readable
    defaults. Both are narrowed here rather than trusting the umask.
    """
    for path in (STATE_DIR, WORK_DIR, LOG_DIR):
        os.makedirs(path, mode=0o700, exist_ok=True)
        platform_support.PLATFORM.restrict(path, is_dir=True)
        try:
            for name in os.listdir(path):
                target = os.path.join(path, name)
                if os.path.isfile(target):
                    platform_support.PLATFORM.restrict(target, is_dir=False)
        except OSError:
            pass


def read_state() -> dict:
    if not os.path.exists(LASTRUN_PATH):
        return {"completed": {}}
    try:
        with open(LASTRUN_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"completed": {}}


def write_state(state: dict) -> None:
    """Write via a temp file and rename.

    Opening the ledger with "w" truncates it first, so a crash at that instant
    leaves an unparseable file. read_state() swallows the decode error and
    returns an empty ledger, which would make the next run regenerate two
    weeks of reports from scratch.
    """
    temp = LASTRUN_PATH + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, LASTRUN_PATH)
    platform_support.PLATFORM.restrict(LASTRUN_PATH, is_dir=False)


def prune_work_files(keep_days: int = WORK_RETENTION_DAYS) -> int:
    """Delete intermediate files older than the retention window.

    work/ grows about 1 MB a day and holds pre-sanitization prompt text, so it
    is both a disk and a privacy concern if left forever.
    """
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for name in os.listdir(WORK_DIR):
        target = os.path.join(WORK_DIR, name)
        try:
            if os.path.isfile(target) and os.path.getmtime(target) < cutoff:
                os.unlink(target)
                removed += 1
        except OSError:
            continue
    return removed


def pending_days(state: dict) -> list[str]:
    """Logical days that have closed but were never written."""
    today = config.logical_date(datetime.now(config.local_tz()))
    done = set(state.get("completed", {}))
    days = []
    for offset in range(1, MAX_BACKFILL_DAYS + 1):
        day = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=offset)).strftime("%Y-%m-%d")
        if day not in done:
            days.append(day)
    return sorted(days)


def seed_first_run(state: dict) -> dict:
    """On a brand-new install, do not backfill the fortnight before it existed.

    `launchd` bootstraps with RunAtLoad, so the job fires the moment install.sh
    registers it. With an empty ledger every day in the backfill window counts
    as outstanding, and the installer would spend fourteen model calls
    reconstructing weeks the user never asked about. Only the most recent closed
    day runs; the rest are recorded as skipped so they are never revisited.
    """
    # Keyed on the ledger *file*, not on it being empty: a corrupted ledger also
    # reads as empty, and there the right answer is to regenerate the fortnight,
    # not to write off days that were really missed.
    if os.path.exists(LASTRUN_PATH) or state.get("completed"):
        return state
    today = config.logical_date(datetime.now(config.local_tz()))
    for offset in range(FIRST_RUN_DAYS + 1, MAX_BACKFILL_DAYS + 1):
        day = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=offset)).strftime("%Y-%m-%d")
        state.setdefault("completed", {})[day] = {
            "at": datetime.now(config.local_tz()).isoformat(timespec="seconds"),
            "skipped": "설치 이전 날짜",
        }
    write_state(state)
    return state


def Watchdog(seconds: int):
    """Delegated to the platform layer — SIGALRM is not portable.

    It raises `platform_support.Timeout`, not `TimeoutError`; see that class
    for why the distinction is load-bearing.
    """
    return platform_support.PLATFORM.watchdog(seconds)


def notify(title: str, message: str) -> None:
    """Surface failures. A job that fails silently is worse than none."""
    platform_support.PLATFORM.notify(title, message)


def first_sentences(markdown: str, count: int = SUMMARY_SENTENCES) -> str:
    """First few sentences of the report's opening prose.

    Scans line by line rather than splitting on blank lines: markdown puts a
    heading and the paragraph under it in the same block, so a block-based
    scan skips every block as "a heading" and returns nothing.
    """
    prose: list[str] = []
    for line in markdown.splitlines():
        text = line.strip()
        is_prose = bool(text) and not text.startswith(("#", "-", "*", ">", "|", "`", "1."))
        if is_prose:
            prose.append(text)
            continue
        if prose:  # prose ended — stop at the first heading/blank/list after it
            break
    if not prose:
        return ""
    joined = " ".join(" ".join(prose).split())
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", joined) if s]
    return " ".join(sentences[:count])


REGRESSION_BASELINE_DAYS = 7
REGRESSION_MIN_SAMPLES = 3
ALERT_COOLDOWN_DAYS = 7

# The zero that means "nothing happened today" rather than "nothing ran today".
NO_ACTIVITY = "활동 없음"


def _baseline(state: dict, before: str, key: str) -> list[int]:
    """The metric's recent history, most recent first, excluding `before`."""
    completed = state.get("completed") or {}
    days = sorted((day for day, entry in completed.items()
                   if day < before and not entry.get("skipped")
                   and isinstance(entry.get(key), int)),
                  reverse=True)
    return [completed[day][key] for day in days[:REGRESSION_BASELINE_DAYS]]


def detect_regression(state: dict, date_str: str, result: dict | None = None) -> list[tuple[str, str]]:
    """Metrics that used to be non-zero and today are not.

    The job already shouts when it raises. What it cannot see is the failure
    that produces a clean run and an empty report — and every silent defect
    found while porting to Windows was of exactly that shape: a path test that
    rejected every session, an exclusion list that matched nothing, a search
    root pointed at the wrong drive. Each one exited 0 and wrote "활동 없음"
    every night.

    The whole difficulty here is not detecting the zero, it is not crying wolf
    about it. A weekend really is zero. So:

      - the comparison is against a **median**, which a single heavy day cannot
        drag upward the way a mean can;
      - a metric that was already zero says nothing — this reports a *change*,
        not a low number;
      - too few samples means no verdict rather than a guess.

    Returns (cause, message) pairs. Rate limiting belongs to the notifier, not
    here, so `doctor.py` can state the condition every time it is asked.
    """
    from statistics import median

    if not config.load().get("run", {}).get("regression_alerts", True):
        return []

    if result is None:
        result = (state.get("completed") or {}).get(date_str) or {}
    skipped = result.get("skipped")
    if skipped and skipped != NO_ACTIVITY:
        return []  # the day never ran; there is nothing to compare it against

    found: list[tuple[str, str]] = []

    # Configuration that guarantees a zero forever, whatever the history says.
    authors = [a for a in config.load()["git"].get("authors", []) if a and a.strip()]
    if not authors:
        found.append(("git_authors_empty",
                      "git.authors 가 비어 커밋을 하나도 수집하지 않습니다"))

    projects = result.get("projects", 0)
    project_history = _baseline(state, date_str, "projects")
    if (projects == 0 and len(project_history) >= REGRESSION_MIN_SAMPLES
            and median(project_history) > 0):
        found.append(("projects_zero",
                      f"수집이 프로젝트를 하나도 찾지 못했습니다 "
                      f"(최근 {len(project_history)}일 중앙값 {median(project_history):g})"))

    # Only worth saying when collection itself worked — otherwise the line
    # above is the real story and this would just repeat it.
    commit_history = _baseline(state, date_str, "commits")
    if (projects > 0 and authors and result.get("commits", 0) == 0
            and len(commit_history) >= REGRESSION_MIN_SAMPLES
            and median(commit_history) > 0):
        found.append(("commits_zero",
                      f"커밋만 0 입니다 — git_search_root 또는 git.authors 확인 "
                      f"(최근 {len(commit_history)}일 중앙값 {median(commit_history):g})"))
    return found


def alert_is_due(state: dict, cause: str, today: str) -> bool:
    """Has this cause been quiet long enough to be worth saying again?

    A misconfiguration persists until someone fixes it, so an unthrottled
    alert fires every night and becomes the notification people dismiss
    without reading — which is the same as having none.
    """
    last = (state.get("alerts") or {}).get(cause)
    if not last:
        return True
    try:
        elapsed = datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")
    except ValueError:
        return True
    return elapsed >= timedelta(days=ALERT_COOLDOWN_DAYS)


def record_alert(state: dict, cause: str, today: str) -> None:
    state.setdefault("alerts", {})[cause] = today


def source_reference(path: str) -> str:
    """Where the row's digest came from, as short a path as is truthful.

    Relative to the home directory when that is expressible. On Windows the
    tool and the home directory can sit on different drives, and `relpath`
    raises ValueError rather than returning something — which crashed the run
    after the report had already been generated and paid for.
    """
    for base in (os.path.expanduser("~"), HERE):
        try:
            relative = os.path.relpath(path, base)
        except ValueError:
            continue
        # Compare the first *component*, not a prefix: a directory literally
        # named "..config" starts with ".." without being outside the base.
        if relative.split(os.sep)[0] != os.pardir:
            return relative
    return path


def derive_tags(digest: dict) -> list[str]:
    """Cheap, deterministic tags from what the day actually contained."""
    tags = set()
    for project in digest["projects"].values():
        if project["commits"]:
            tags.add("커밋")
        if any(s.startswith("insane-research") for s in project["skills_used"]):
            tags.add("리서치")
        written = project["files_written"] + project["files_edited"]
        if any(f.endswith(".md") for f in written):
            tags.add("문서작성")
        if any(f.endswith((".py", ".js", ".ts", ".swift", ".sh")) for f in written):
            tags.add("코드작성")
        if any(f.endswith((".html", ".css")) for f in written):
            tags.add("디자인")
    return sorted(tags)


def run_one(date_str: str, token: str, database_id: str) -> dict:
    """Collect → refine → sanitize → summarize → sanitize → upsert."""
    ensure_dirs()
    raw_path = os.path.join(WORK_DIR, f"raw_{date_str}.json")
    digest_path = os.path.join(WORK_DIR, f"digest_{date_str}.json")
    report_path = os.path.join(WORK_DIR, f"report_{date_str}.md")

    raw = collect.collect(date_str)
    with open(raw_path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False, indent=1)

    digest = refine.refine(raw)

    # sanitize BEFORE the model sees it
    digest, digest_findings = sanitize.redact_structure(digest)
    with open(digest_path, "w", encoding="utf-8") as handle:
        json.dump(digest, handle, ensure_ascii=False, indent=1)

    if not digest["projects"]:
        return {"date": date_str, "skipped": "활동 없음",
                "digest_findings": dict(digest_findings)}

    report = summarize.summarize(digest)

    # sanitize AFTER, in case the model echoed something back
    report, report_findings = sanitize.redact(report)
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(report + "\n")

    stats = digest["stats"]
    upsert(
        token, database_id, date_str, report,
        summary=first_sentences(report),
        projects=list(digest["projects"].keys()),
        tags=derive_tags(digest),
        sessions=stats["sessions"],
        commits=stats["commits"],
        files=stats["files"],
        status_label=notion_schema.status_label("done"),
        source_path=source_reference(digest_path),
    )
    return {
        "date": date_str,
        "projects": stats["projects"],
        "sessions": stats["sessions"],
        "files": stats["files"],
        "commits": stats["commits"],
        "report_chars": len(report),
        "digest_findings": dict(digest_findings),
        "report_findings": dict(report_findings),
    }


def main() -> int:
    platform_support.require_supported()
    # The second `--log` call. Redundant when this module was run directly —
    # redirect_output() is idempotent — and load-bearing when it was imported,
    # which is what the packaged build does.
    if "--log" in sys.argv:
        redirect_output()
    # And this is for the redirected stream's *encoding*: the file is opened in
    # the ANSI codepage and every message below is Korean.
    platform_support.PLATFORM.configure_stdio()
    ensure_dirs()
    env_path = os.path.join(HERE, ".env")
    if not os.path.exists(env_path):
        recipe = ("install.ps1 을 실행하거나 .env.example 을 .env 로 복사"
                  if os.name == "nt"
                  else "cp .env.example .env && chmod 600 .env")
        print(f".env 가 없습니다: {env_path}\n"
              f"  {recipe} 한 뒤 값을 채우세요.", file=sys.stderr)
        return 1
    env = load_env(env_path)
    token = env.get("DAILY_REPORT_NOTION_TOKEN", "")
    database_id = env.get("DAILY_REPORT_DATABASE_ID", "")
    if not token or not database_id:
        print("DAILY_REPORT_NOTION_TOKEN / DAILY_REPORT_DATABASE_ID 미설정", file=sys.stderr)
        return 1

    # single instance: a wake-triggered run must not race the scheduled one
    lock = platform_support.PLATFORM.acquire_lock(LOCK_PATH)
    if lock is None:
        print("이미 실행 중입니다. 종료합니다.")
        return 0

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    state = read_state()
    if args:
        targets = args
    else:
        state = seed_first_run(state)
        targets = pending_days(state)
        if not targets:
            print("밀린 날짜가 없습니다.")
            return 0
        print(f"밀린 날짜 {len(targets)}일: {', '.join(targets)}")

    watchdog_sec = config.load().get("run", {}).get("watchdog_sec", 0)
    ok, failed = 0, []
    regressions: dict[str, str] = {}
    # Held for the whole run and released after. On macOS the scheduler already
    # wraps the process in caffeinate and this is a no-op; on Windows there is
    # nothing to wrap with, so the assertion is taken from inside instead.
    with platform_support.PLATFORM.keep_awake():
        for date_str in targets:
            started = datetime.now(config.local_tz())
            try:
                with Watchdog(watchdog_sec):
                    result = run_one(date_str, token, database_id)
                # Judged before the ledger gains today's entry, so the
                # baseline is history rather than a set containing the day
                # being judged.
                for cause, message in detect_regression(state, date_str, result):
                    regressions.setdefault(cause, message)
                state.setdefault("completed", {})[date_str] = {
                    "at": started.isoformat(timespec="seconds"),
                    **{k: v for k, v in result.items() if k != "date"},
                }
                write_state(state)
                ok += 1
                # Console output comes *after* the ledger, and never raises.
                #
                # The status window runs this as a child and reads its stdout
                # through a pipe. Closing that window closes the read end, so
                # the next write fails — and this line used to sit before the
                # ledger write, inside the try. A day that had already
                # published its report to Notion was therefore recorded as a
                # failure and never entered the ledger, so it stayed
                # outstanding forever while its row sat there marked done.
                # Observed on the first real install, one minute after setup.
                say(f"{date_str}: 건너뜀 ({result['skipped']})" if result.get("skipped")
                    else (f"{date_str}: 프로젝트 {result['projects']} · 세션 {result['sessions']} · "
                          f"파일 {result['files']} · 커밋 {result['commits']} · "
                          f"보고서 {result['report_chars']:,}자 · "
                          f"살균 {result['digest_findings'] or '0건'}"))
            except Exception as error:  # a failed day must not kill the rest
                failed.append(date_str)
                detail = f"{type(error).__name__}: {error}"
                print(f"{date_str}: 실패 — {detail}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)

    pruned = prune_work_files()
    if pruned:
        print(f"오래된 중간 산출물 {pruned}개 정리.")

    report_regressions(state, regressions)

    if failed:
        notify("하루 마감 보고서 실패", f"{len(failed)}일 실패: {', '.join(failed)}")
        return 1
    print(f"완료 {ok}일.")
    return 0


def report_regressions(state: dict, regressions: dict[str, str]) -> None:
    """Say something about a run that succeeded at producing nothing.

    Printed every time — the log is where someone looks on purpose. Notified
    only when the cause has been quiet for the cooldown, because the alert
    people learn to dismiss is worth less than no alert at all.
    """
    if not regressions:
        return
    today = config.logical_date(datetime.now(config.local_tz()))
    for message in regressions.values():
        print(f"⚠️  {message}")

    due = {cause: message for cause, message in regressions.items()
           if alert_is_due(state, cause, today)}
    if not due:
        print(f"   (알림은 {ALERT_COOLDOWN_DAYS}일에 한 번만 보냅니다 — 이번엔 생략)")
        return
    for cause in due:
        record_alert(state, cause, today)
    write_state(state)
    notify("하루 마감 보고서 — 확인 필요", "\n".join(due.values()))


if __name__ == "__main__":
    raise SystemExit(main())
