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


def validate_requested_day(answer: str, today: str) -> tuple[str, str]:
    """Read a hand-typed date. Returns (day, "") or ("", why not).

    Module level, and returning a message rather than showing one, because
    this is the part worth testing — the widgets are not. `today` is the
    *logical* day, which is still in progress: a report for it would be built
    from an unfinished day and would read as a quiet one rather than an
    incomplete one, which is indistinguishable afterwards.
    """
    answer = (answer or "").strip()
    if not answer:
        return "", "날짜를 넣어 주세요."
    try:
        day = datetime.strptime(answer, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return "", f"날짜로 읽을 수 없습니다: {answer}\n\nYYYY-MM-DD 형식으로 넣어 주세요."
    if day >= today:
        boundary = config.load()["day"]["boundary_hour"]
        yesterday = (datetime.strptime(today, "%Y-%m-%d")
                     - timedelta(days=1)).strftime("%Y-%m-%d")
        return "", (f"{day} 은 아직 끝나지 않은 하루입니다.\n\n"
                    f"하루 경계가 {boundary:02d}:00 이라 지금 진행 중인 날짜는 {today} 이고,\n"
                    f"마감된 가장 최근 날짜는 {yesterday} 입니다.")
    return day, ""


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

def build(root, tk, ttk, scrolledtext, simpledialog, messagebox):
    """Assemble the window. Kept in a function so importing this module for
    its logic never touches a display."""

    label = config.load().get("launchd", {}).get("label", "")
    today = config.logical_date(datetime.now(config.local_tz()))

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

    # Everything below reads the ledger, and the ledger changes while this
    # window is open — a report generated from the buttons used to leave the
    # panels showing the state from before it ran, so the only way to see the
    # result was to close the window and open it again. The widgets are
    # therefore built empty and filled by `refresh()`, which is also what runs
    # when a command finishes.

    # --- last run ----------------------------------------------------------
    last = ttk.LabelFrame(outer, text=" 마지막 산출 ", padding=10)
    last.pack(fill="x", pady=(10, 0))
    last_line = tk.StringVar()
    ttk.Label(last, textvariable=last_line, font=font).pack(anchor="w")

    # --- 14 day strip ------------------------------------------------------
    strip = ttk.LabelFrame(outer, text=f" 최근 {STRIP_DAYS}일 ", padding=10)
    strip.pack(fill="x", pady=(10, 0))
    row = ttk.Frame(strip)
    row.pack(anchor="w")
    ttk.Label(strip, text="■ 정상   ▨ 활동 없음   □ 없음/실패   · 설치 이전",
              font=(font[0], 9), foreground="#666").pack(anchor="w", pady=(6, 0))

    # --- warnings ----------------------------------------------------------
    # Always created, never conditionally, because a warning can appear while
    # the window is open — a frame that does not exist yet cannot be filled in
    # from a refresh without repacking everything below it.
    warn = ttk.LabelFrame(outer, text=" 확인 필요 ", padding=10)
    warn_packed = {"shown": False}

    def paint(summary: dict) -> None:
        """Put one reading of the ledger on screen."""
        entry = summary["last_run"]
        last_line.set(
            f"{entry['date']}  ·  프로젝트 {entry.get('projects', '?')}"
            f"  세션 {entry.get('sessions', '?')}"
            f"  파일 {entry.get('files', '?')}"
            f"  커밋 {entry.get('commits', '?')}"
            if entry else "아직 생성된 보고서가 없습니다")

        for cell in row.winfo_children():
            cell.destroy()
        for day, status in summary["days"]:
            cell = tk.Label(row, text=MARK[status], fg=COLOUR[status],
                            font=(mono[0], 14), padx=1)
            cell.pack(side="left")
            _tooltip(tk, cell, f"{day}  {status}")

        for child in warn.winfo_children():
            child.destroy()
        for message in summary["regressions"]:
            ttk.Label(warn, text=f"⚠  {message}", font=font,
                      foreground="#b8860b", wraplength=500,
                      justify="left").pack(anchor="w")
        if summary["pending"]:
            ttk.Label(warn, text=f"⚠  밀린 날짜 {len(summary['pending'])}일",
                      font=font, foreground="#b8860b").pack(anchor="w")
        wanted = bool(summary["regressions"] or summary["pending"])
        if wanted and not warn_packed["shown"]:
            warn.pack(fill="x", pady=(10, 0), before=output)
        elif not wanted and warn_packed["shown"]:
            warn.pack_forget()
        warn_packed["shown"] = wanted

    # --- output ------------------------------------------------------------
    output = scrolledtext.ScrolledText(outer, height=10, font=mono, wrap="word")
    output.pack(fill="both", expand=True, pady=(10, 0))
    output.configure(state="disabled")

    lines: queue.Queue = queue.Queue()
    scheduler_result: queue.Queue = queue.Queue()
    running = {"busy": False}
    # Recomputed by refresh() rather than captured once — see its docstring.
    current = {"today": today, "yesterday": ""}
    yesterday_button: dict = {"widget": None}
    repaired = {"done": False}

    def emit(text):
        output.configure(state="normal")
        output.insert("end", text + "\n")
        output.see("end")
        output.configure(state="disabled")

    def refresh():
        """Re-read the ledger and repaint.

        `today` is recomputed too. It is the *logical* day, so a window left
        open across the 04:00 boundary was drawing the wrong fortnight and
        offering to rebuild the wrong date — which mattered more once a run
        could be started from here.
        """
        now = config.logical_date(datetime.now(config.local_tz()))
        current["today"] = now
        current["yesterday"] = (datetime.strptime(now, "%Y-%m-%d")
                                - timedelta(days=1)).strftime("%Y-%m-%d")
        yesterday_button["widget"].configure(
            text=f"{current['yesterday']} 다시 생성")
        paint(summarize_state(run_day.read_state(), now))

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
                # A finished command is the only thing that changes the ledger
                # while this window is open. Without this the panels kept
                # showing the state from before the run, and the only way to
                # see what had just been produced was to close and reopen.
                refresh()
            else:
                emit(item)
        # Same trip back to the main thread for the scheduler worker, which
        # used to write its StringVar from the thread it ran on.
        try:
            text, needs_repair = scheduler_result.get_nowait()
            scheduler_line.set(text)
            if needs_repair and not repaired["done"]:
                repaired["done"] = True
                emit("예약 작업이 없습니다 — 다시 등록합니다.")
                run_command(register_argv(), "install -NonInteractive")
        except queue.Empty:
            pass
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

    def run_argv(day: str) -> list[str]:
        """How to ask this install to build one day's report."""
        return (paths.command_argv("run", day) if paths.bundled()
                else paths.command_argv("run_day", day))

    def register_argv() -> list[str]:
        """How to (re)register the scheduled task.

        There is no `install.py`, so `command_argv` only answers this for a
        packaged build; from a checkout the installer is the PowerShell script
        itself.
        """
        if paths.bundled():
            return paths.command_argv("install", "-NonInteractive")
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", paths.resource("install.ps1"), "-NonInteractive"]

    def ask_for_a_date():
        """Build any closed day, not only the most recent one.

        The button beside this one is fixed to yesterday, which covers the
        common case and answers nothing else — a day missed while the machine
        was off, or one whose report was worth regenerating, had to be run
        from a console with a path nobody has memorised. Notion is keyed by
        date, so regenerating updates that day's row instead of adding one.
        """
        answer = simpledialog.askstring(
            "보고서 생성", "생성할 날짜 (YYYY-MM-DD)",
            initialvalue=current["yesterday"], parent=root)
        if answer is None:
            return
        day, problem = validate_requested_day(answer, current["today"])
        if problem:
            messagebox.showerror("보고서 생성", problem, parent=root)
            return
        run_command(run_argv(day), f"run {day}")

    def rebuild_yesterday():
        day = current["yesterday"]
        run_command(run_argv(day), f"run {day}")

    actions = [
        ("진단 실행",
         lambda: run_command(paths.command_argv("doctor"), "doctor")),
        # Labelled by refresh(), which knows the current logical day. Built
        # with the date baked in, this went stale the moment the window was
        # left open across the boundary hour.
        ("", rebuild_yesterday),
        ("다른 날짜…", ask_for_a_date),
        ("예약 작업 등록",
         lambda: run_command(register_argv(), "install -NonInteractive")),
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
        if command is rebuild_yesterday:
            yesterday_button["widget"] = button

    # --- scheduler status, off the main thread (it shells out to PowerShell) -
    #
    # The result goes back through a queue, for the reason stated 130 lines
    # above and then not followed here: tkinter is not thread safe. This
    # worker was calling `scheduler_line.set()` directly, and the panel came
    # up **empty** — no status, not even the "조회 중…" it starts with. An
    # empty box in the one panel that says whether anything is scheduled is
    # the exact failure this window exists to prevent.
    def load_scheduler():
        if not label:
            scheduler_result.put(("config.toml 이 없어 작업 이름을 모릅니다", False))
            return
        try:
            registered, detail = platform_support.PLATFORM.scheduler_status(label)
        except Exception as error:  # a diagnostic window must not die diagnosing
            scheduler_result.put((f"조회 실패: {error}", False))
            return
        if registered:
            # An empty detail would leave the panel blank and say nothing, so
            # the absence is reported rather than rendered.
            scheduler_result.put(
                (detail.strip() or f"등록되어 있으나 상태를 읽지 못했습니다  ({label})",
                 False))
            return

        # Configured but unscheduled is the shape an upgrade leaves behind, and
        # it is the one state where nothing happens and nothing says so.
        #
        # The uninstaller removes the task — correctly, since it is about to
        # delete the executable it points at. Reinstalling then launches the
        # GUI, which finds config.toml and a filled .env and opens *this*
        # window rather than the wizard. Only the wizard runs install.ps1, and
        # only install.ps1 registers the task. So the upgrade completes, looks
        # finished, and 04:05 never comes again.
        #
        # Re-registering restores what the person already chose; it is not a
        # new decision, so it does not need asking about. Nothing is repaired
        # when setup never finished — that case belongs to the wizard.
        try:
            import gui
            configured = gui.is_configured()
        except Exception:
            configured = False
        scheduler_result.put((f"등록되어 있지 않습니다  ({label})", configured))

    refresh()          # first paint — the panels are built empty
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
    from tkinter import messagebox, scrolledtext, simpledialog, ttk

    root = tk.Tk()
    build(root, tk, ttk, scrolledtext, simpledialog, messagebox)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
