"""A window that answers "is this thing working" without reading a log.

Deliberately **not** a tray icon. Building one from the standard library alone
means driving `Shell_NotifyIcon` and a window procedure through ctypes — a few
hundred lines of fiddly code — and Windows 11 hides tray icons in the overflow
by default, so the ambient awareness that would justify the cost is mostly not
there. Ambient signalling is the notifier's job, and `run_day.detect_regression`
now covers the silent cases it used to miss.

So this is a window you open. It shows what the ledger and the scheduler
already know, and it runs the same commands a person would have typed. It
holds no state of its own: everything here is a view.

tkinter is in the standard library, which is what keeps "there is nothing to
install" true. That property is worth more than a prettier toolkit.

    python status_window.py
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import config          # noqa: E402
import paths           # noqa: E402
import platform_support  # noqa: E402
import run_day         # noqa: E402

STRIP_DAYS = 14
POLL_MS = 100

# What a day in the ledger turned out to be. The ledger records completions, so
# "missing" covers both a day that failed and one that has not run yet — the
# pending-days line below is what tells those apart.
OK, EMPTY, PREINSTALL, MISSING = "ok", "empty", "preinstall", "missing"

MARK = {OK: "■", EMPTY: "▨", PREINSTALL: "·", MISSING: "□"}
COLOUR = {OK: "#2f7d32", EMPTY: "#b8860b", PREINSTALL: "#9e9e9e", MISSING: "#c62828"}


def summarize_state(state: dict, today: str) -> dict:
    """Everything the window draws, as plain data.

    Split out so it can be tested. The widgets below are not tested — the
    interesting failures live in reading the ledger, not in packing frames.
    """
    completed = state.get("completed") or {}

    days = []
    for offset in range(STRIP_DAYS, 0, -1):
        day = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=offset)).strftime("%Y-%m-%d")
        entry = completed.get(day)
        if entry is None:
            days.append((day, MISSING))
        elif not entry.get("skipped"):
            days.append((day, OK))
        elif entry["skipped"] == run_day.NO_ACTIVITY:
            days.append((day, EMPTY))
        else:
            days.append((day, PREINSTALL))

    produced = {day: entry for day, entry in completed.items() if not entry.get("skipped")}
    latest = max(produced) if produced else None

    return {
        "days": days,
        "last_run": ({"date": latest, **produced[latest]} if latest else None),
        "pending": run_day.pending_days(state),
        "regressions": [message for _, message in
                        run_day.detect_regression(state, _last_that_ran(completed, today))],
    }


def _last_that_ran(completed: dict, today: str) -> str:
    """The most recent day that executed, empty result included.

    A day that ran and found nothing is filed as skipped, so picking the last
    *successful* day would step over exactly the one worth judging.
    """
    ran = [day for day, entry in completed.items()
           if not entry.get("skipped") or entry["skipped"] == run_day.NO_ACTIVITY]
    return max(ran) if ran else today


def notion_url() -> str:
    """The database this install writes to, or "" if it is not configured."""
    try:
        from notion_upsert import load_env
        database_id = load_env(paths.data(".env")).get("DAILY_REPORT_DATABASE_ID", "")
    except (OSError, ValueError):
        return ""
    compact = database_id.replace("-", "").strip()
    return f"https://www.notion.so/{compact}" if compact else ""


def open_in_file_manager(path: str) -> None:
    if not os.path.isdir(path):
        return
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 — the path is ours, not user input
    else:
        subprocess.run(["open", path], check=False)


# --------------------------------------------------------------------- UI ---

def build(root, tk, ttk, scrolledtext):
    """Assemble the window. Kept in a function so importing this module for
    its logic never touches a display."""

    label = config.load().get("launchd", {}).get("label", "")
    today = config.logical_date(datetime.now(config.local_tz()))
    summary = summarize_state(run_day.read_state(), today)

    root.title("하루 마감 보고서")
    root.minsize(560, 460)
    font = ("Malgun Gothic", 10) if os.name == "nt" else ("", 12)
    mono = ("Consolas", 9) if os.name == "nt" else ("Menlo", 11)

    outer = ttk.Frame(root, padding=14)
    outer.pack(fill="both", expand=True)

    # --- scheduler ---------------------------------------------------------
    scheduler = ttk.LabelFrame(outer, text=" 예약 실행 ", padding=10)
    scheduler.pack(fill="x")
    scheduler_line = tk.StringVar(value="조회 중…")
    ttk.Label(scheduler, textvariable=scheduler_line, font=font,
              justify="left").pack(anchor="w")

    # --- last run ----------------------------------------------------------
    last = ttk.LabelFrame(outer, text=" 마지막 산출 ", padding=10)
    last.pack(fill="x", pady=(10, 0))
    if summary["last_run"]:
        entry = summary["last_run"]
        text = (f"{entry['date']}  ·  프로젝트 {entry.get('projects', '?')}"
                f"  세션 {entry.get('sessions', '?')}"
                f"  파일 {entry.get('files', '?')}"
                f"  커밋 {entry.get('commits', '?')}")
    else:
        text = "아직 생성된 보고서가 없습니다"
    ttk.Label(last, text=text, font=font).pack(anchor="w")

    # --- 14 day strip ------------------------------------------------------
    strip = ttk.LabelFrame(outer, text=f" 최근 {STRIP_DAYS}일 ", padding=10)
    strip.pack(fill="x", pady=(10, 0))
    row = ttk.Frame(strip)
    row.pack(anchor="w")
    for day, status in summary["days"]:
        cell = tk.Label(row, text=MARK[status], fg=COLOUR[status],
                        font=(mono[0], 14), padx=1)
        cell.pack(side="left")
        _tooltip(tk, cell, f"{day}  {status}")
    ttk.Label(strip, text="■ 정상   ▨ 활동 없음   □ 없음/실패   · 설치 이전",
              font=(font[0], 9), foreground="#666").pack(anchor="w", pady=(6, 0))

    # --- warnings ----------------------------------------------------------
    if summary["regressions"] or summary["pending"]:
        warn = ttk.LabelFrame(outer, text=" 확인 필요 ", padding=10)
        warn.pack(fill="x", pady=(10, 0))
        for message in summary["regressions"]:
            ttk.Label(warn, text=f"⚠  {message}", font=font,
                      foreground="#b8860b", wraplength=500,
                      justify="left").pack(anchor="w")
        if summary["pending"]:
            ttk.Label(warn, text=f"⚠  밀린 날짜 {len(summary['pending'])}일",
                      font=font, foreground="#b8860b").pack(anchor="w")

    # --- output ------------------------------------------------------------
    output = scrolledtext.ScrolledText(outer, height=10, font=mono, wrap="word")
    output.pack(fill="both", expand=True, pady=(10, 0))
    output.configure(state="disabled")

    lines: queue.Queue = queue.Queue()
    running = {"busy": False}

    def emit(text):
        output.configure(state="normal")
        output.insert("end", text + "\n")
        output.see("end")
        output.configure(state="disabled")

    def drain():
        while True:
            try:
                item = lines.get_nowait()
            except queue.Empty:
                break
            if item is None:
                running["busy"] = False
                for button in buttons:
                    button.state(["!disabled"])
            else:
                emit(item)
        root.after(POLL_MS, drain)

    def run_command(argv, title):
        """Long work off the main thread — tkinter is not thread safe, so the
        worker only ever puts strings on a queue and the UI polls it."""
        if running["busy"]:
            return
        running["busy"] = True
        for button in buttons:
            button.state(["disabled"])
        lines.put(f"$ {title}")

        def worker():
            try:
                process = subprocess.Popen(
                    argv, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for line in process.stdout:
                    lines.put(line.rstrip())
                process.wait()
                lines.put(f"— 종료 코드 {process.returncode}")
            except OSError as error:
                lines.put(f"실행 실패: {error}")
            finally:
                lines.put(None)

        threading.Thread(target=worker, daemon=True).start()

    # --- buttons -----------------------------------------------------------
    bar = ttk.Frame(outer)
    bar.pack(fill="x", pady=(10, 0))

    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    actions = [
        ("진단 실행",
         lambda: run_command(paths.command_argv("doctor"), "doctor")),
        (f"{yesterday} 다시 생성",
         lambda: run_command(paths.command_argv("run_day", yesterday)
                             if not paths.bundled()
                             else paths.command_argv("run", yesterday),
                             f"run {yesterday}")),
        ("로그 열기", lambda: open_in_file_manager(run_day.LOG_DIR)),
    ]
    url = notion_url()
    if url:
        actions.append(("Notion 열기", lambda: webbrowser.open(url)))

    buttons = []
    for text, command in actions:
        button = ttk.Button(bar, text=text, command=command)
        button.pack(side="left", padx=(0, 6))
        buttons.append(button)

    # --- scheduler status, off the main thread (it shells out to PowerShell) -
    def load_scheduler():
        if not label:
            scheduler_line.set("config.toml 이 없어 작업 이름을 모릅니다")
            return
        try:
            registered, detail = platform_support.PLATFORM.scheduler_status(label)
        except Exception as error:  # a diagnostic window must not die diagnosing
            scheduler_line.set(f"조회 실패: {error}")
            return
        scheduler_line.set(detail if registered else f"등록되어 있지 않습니다  ({label})")

    threading.Thread(target=load_scheduler, daemon=True).start()
    root.after(POLL_MS, drain)
    return root


def _tooltip(tk, widget, text):
    """A bare label in a borderless toplevel — ttk has no tooltip."""
    tip = {"window": None}

    def show(_event):
        if tip["window"]:
            return
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height() + 2
        window = tk.Toplevel(widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{x}+{y}")
        tk.Label(window, text=text, background="#ffffe0", relief="solid",
                 borderwidth=1, font=("Malgun Gothic", 8) if os.name == "nt" else ("", 10)
                 ).pack()
        tip["window"] = window

    def hide(_event):
        if tip["window"]:
            tip["window"].destroy()
            tip["window"] = None

    widget.bind("<Enter>", show)
    widget.bind("<Leave>", hide)


def main() -> int:
    platform_support.PLATFORM.configure_stdio()
    # Without this the window is bitmap-scaled on a high-DPI display and every
    # glyph is blurry — which reads as "cheap" long before it reads as "not
    # DPI aware".
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass

    import tkinter as tk
    from tkinter import ttk, scrolledtext

    root = tk.Tk()
    build(root, tk, ttk, scrolledtext)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
