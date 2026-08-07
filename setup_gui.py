"""A setup window that collects the answers and lets install.ps1 do the work.

It writes exactly one file itself — `.env` — and hands everything else to
`install.ps1` as parameters. That division is the whole design:

  - Task registration, the `icacls` narrowing, the skill junction and the shell
    folder detection were verified on a real machine. A GUI that reimplemented
    them would need verifying again, and two implementations of a risky step
    drift until one of them is wrong.
  - `.env` is the exception because the installer *stops* when the token is
    missing — collecting it is the point of this window.

And collecting it here is not merely convenient. The README tells people not to
paste the token into a terminal, because this whole tool exists on the premise
that session logs keep everything you type. **A GUI field is not logged.** The
current alternative is hand-editing `.env`, which is where first installs
actually stall.

    python setup_gui.py
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import paths           # noqa: E402
import platform_support  # noqa: E402

POLL_MS = 100
# Written, so it follows the data root; the example it is built from ships with
# the tool and follows the resource root.
ENV_PATH = paths.data(".env")
EXAMPLE_ENV = paths.resource(".env.example")


def token_docs() -> str:
    """The token instructions that shipped with *this* copy.

    Not a URL to one repository: a fork would send its users back to the
    original, and the page would describe whatever version is on main rather
    than the one installed. The local file is always the matching one, and it
    works without a network.
    """
    import config
    language = config.load().get("report", {}).get("language", "ko")
    for name in (f"notion-setup.{language}.md", "notion-setup.md"):
        candidate = paths.resource("docs", name)
        if os.path.exists(candidate):
            return candidate
    return paths.resource("docs", "notion-setup.md")


def open_docs() -> None:
    path = token_docs()
    if os.name == "nt" and os.path.exists(path):
        os.startfile(path)  # noqa: S606 — our own file, not user input
    else:
        webbrowser.open("file:///" + path.replace(os.sep, "/"))


def probe_repo_roots() -> list[tuple[str, int]]:
    """Candidate search roots with the number of repositories under each.

    `git_search_root` defaults to the home directory, which is right on macOS
    and often wrong here — Windows projects commonly live on a second drive.
    Getting it wrong is not an error anywhere: every day simply reports zero
    commits, exactly the way a quiet fortnight does. Showing counts makes the
    wrong answer hard to pick.
    """
    import string

    roots = [os.path.expanduser("~")]
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if os.path.isdir(drive):
            roots.append(drive)

    counted = []
    for root in roots:
        counted.append((root.rstrip("\\") or root, _count_repos(root)))
    return counted


def probe_depth() -> int:
    """How deep to look, taken from the depth the collector will actually use.

    Measured on this machine: at depth 4 a drive root reported 2 repositories
    and at depth 6 it reported 4, because a path like
    `D:\\Development\\<area>\\<group>\\<project>` puts `.git` on the fifth level.
    A probe shallower than the collector recommends a search root by a count
    that is wrong in the direction that matters — it makes a good root look
    empty. Depth 8 found nothing further and cost twenty times as much.
    """
    import config
    return int(config.load().get("sources", {}).get("git_max_depth", 6))


def _count_repos(root: str, max_depth: int | None = None) -> int:
    """A bounded walk — this runs while someone is looking at a window."""
    if max_depth is None:
        max_depth = probe_depth()
    found = 0
    base = root.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, _ in os.walk(root, onerror=lambda e: None):
        if dirpath.count(os.sep) - base >= max_depth:
            dirnames[:] = []
            continue
        # the trees that make this slow, and never hold a user's own work
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in ("appdata", "windows", "$recycle.bin",
                                            "program files", "program files (x86)",
                                            "programdata", "node_modules",
                                            "system volume information")]
        if ".git" in dirnames:
            found += 1
            dirnames[:] = [d for d in dirnames if d != ".git"]
    return found


def write_env(token: str, parent_url: str) -> None:
    """Create `.env` from the example, filling in what the wizard collected.

    Written without a BOM: everything that reads it opens it as UTF-8 and a
    leading \\ufeff would corrupt the first key name.
    """
    lines = []
    with open(EXAMPLE_ENV, encoding="utf-8") as handle:
        for line in handle.read().splitlines():
            if line.startswith("DAILY_REPORT_NOTION_TOKEN="):
                line = f"DAILY_REPORT_NOTION_TOKEN={token}"
            elif line.startswith("DAILY_REPORT_PARENT_PAGE_URL="):
                line = f"DAILY_REPORT_PARENT_PAGE_URL={parent_url}"
            lines.append(line)
    with open(ENV_PATH, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    platform_support.PLATFORM.restrict(ENV_PATH, is_dir=False)


def installer_argv(language: str, authors: str, search_root: str) -> list[str]:
    return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", paths.resource("install.ps1"),
            "-Language", language,
            "-Authors", authors,
            "-SearchRoot", search_root,
            "-NonInteractive"]


# --------------------------------------------------------------------- UI ---

def build(root, tk, ttk, scrolledtext):
    root.title("하루 마감 보고서 설치")
    root.minsize(640, 560)
    font = ("Malgun Gothic", 10) if os.name == "nt" else ("", 12)
    mono = ("Consolas", 9) if os.name == "nt" else ("Menlo", 11)

    outer = ttk.Frame(root, padding=14)
    outer.pack(fill="both", expand=True)

    # --- environment -------------------------------------------------------
    env_frame = ttk.LabelFrame(outer, text=" 환경 ", padding=10)
    env_frame.pack(fill="x")
    for name, found in _environment():
        ttk.Label(env_frame, text=f"{'✅' if found else '❌'}  {name}",
                  font=font).pack(anchor="w")

    # --- what will be read -------------------------------------------------
    source_frame = ttk.LabelFrame(outer, text=" 수집 대상 ", padding=10)
    source_frame.pack(fill="x", pady=(10, 0))
    for label, where, count in _sources():
        if count < 0:
            mark, note, colour = "—", "설치되어 있지 않음", "#999"
        elif count == 0:
            mark, note, colour = "○", "경로는 있으나 기록 없음", "#b8860b"
        else:
            mark, note, colour = "✅", f"기록 {count:,}개", "#2f7d32"
        line = ttk.Frame(source_frame)
        line.pack(anchor="w", fill="x")
        tk.Label(line, text=f"{mark}  {label}", font=font, fg=colour).pack(side="left")
        tk.Label(line, text=f"  {note}", font=(font[0], 9), fg="#666").pack(side="left")
        tk.Label(source_frame, text=f"      {_shorten(where)}",
                 font=(mono[0], 8), fg="#999").pack(anchor="w")

    # --- answers -----------------------------------------------------------
    form = ttk.LabelFrame(outer, text=" 설정 ", padding=10)
    form.pack(fill="x", pady=(10, 0))
    form.columnconfigure(1, weight=1)

    language = tk.StringVar(value="ko")
    authors = tk.StringVar(value=_git_email())
    search_root = tk.StringVar(value=os.path.expanduser("~"))
    token = tk.StringVar()
    parent = tk.StringVar()

    def row(index, label, widget):
        ttk.Label(form, text=label, font=font).grid(row=index, column=0, sticky="w", pady=3)
        widget.grid(row=index, column=1, sticky="ew", padx=(10, 0), pady=3)

    row(0, "보고서 언어", ttk.Combobox(form, textvariable=language,
                                    values=["ko", "en"], state="readonly", width=8))
    row(1, "커밋 저자 이메일", ttk.Entry(form, textvariable=authors, font=font))

    root_row = ttk.Frame(form)
    root_row.columnconfigure(0, weight=1)
    ttk.Entry(root_row, textvariable=search_root, font=font).grid(row=0, column=0, sticky="ew")
    probe_label = tk.StringVar(value="")
    ttk.Label(root_row, textvariable=probe_label, font=(font[0], 9),
              foreground="#666").grid(row=1, column=0, sticky="w", pady=(2, 0))
    row(2, "git 검색 루트", root_row)

    # The token field is why this window is worth building. `show="*"` keeps it
    # off the screen; being a field at all keeps it out of the shell history and
    # the session transcript.
    row(3, "Notion 토큰", ttk.Entry(form, textvariable=token, show="*", font=font))
    row(4, "부모 페이지 URL", ttk.Entry(form, textvariable=parent, font=font))

    ttk.Button(form, text="토큰 발급 방법 열기",
               command=open_docs).grid(row=5, column=1, sticky="w",
                                       padx=(10, 0), pady=(6, 0))

    # --- output ------------------------------------------------------------
    output = scrolledtext.ScrolledText(outer, height=14, font=mono, wrap="word")
    output.pack(fill="both", expand=True, pady=(10, 0))
    output.configure(state="disabled")

    lines: queue.Queue = queue.Queue()
    busy = {"running": False}

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
                busy["running"] = False
                install_button.state(["!disabled"])
            else:
                emit(item)
        root.after(POLL_MS, drain)

    def run(argv, title):
        if busy["running"]:
            return
        busy["running"] = True
        install_button.state(["disabled"])
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

    def install():
        if not token.get().strip() or not parent.get().strip():
            emit("⚠️  Notion 토큰과 부모 페이지 URL 을 채워야 데이터베이스를 만들 수 있습니다.")
            return
        try:
            write_env(token.get().strip(), parent.get().strip())
            emit(f"✅ .env 기록됨 (본인 계정만 읽기 가능)")
        except OSError as error:
            emit(f"❌ .env 를 쓰지 못했습니다: {error}")
            return
        run(installer_argv(language.get(), authors.get().strip(), search_root.get().strip()),
            "install.ps1")

    bar = ttk.Frame(outer)
    bar.pack(fill="x", pady=(10, 0))
    install_button = ttk.Button(bar, text="설치", command=install)
    install_button.pack(side="left")
    ttk.Button(bar, text="진단만 실행",
               command=lambda: run([sys.executable, "-X", "utf8", "doctor.py"],
                                   "doctor.py")).pack(side="left", padx=(6, 0))

    # --- repository probe, off the main thread -----------------------------
    def probe():
        try:
            counted = probe_repo_roots()
        except OSError:
            return
        best = max(counted, key=lambda item: item[1], default=None)
        if best and best[1] > 0:
            search_root.set(best[0])
        probe_label.set("  ".join(f"{root} {count}개" for root, count in counted if count))

    probe_label.set("저장소 세는 중…")
    threading.Thread(target=probe, daemon=True).start()
    root.after(POLL_MS, drain)
    return root


def _environment() -> list[tuple[str, bool]]:
    """What has to be present for the job to run at all."""
    import shutil
    import summarize
    claude = summarize.claude_argv()[-1]
    return [
        (f"Python {sys.version_info.major}.{sys.version_info.minor}", True),
        ("git — 없으면 커밋이 하나도 수집되지 않습니다", bool(shutil.which("git"))),
        ("Claude Code CLI — 없으면 요약이 생성되지 않습니다",
         os.path.isfile(claude) or bool(shutil.which(claude))),
    ]


def _sources() -> list[tuple[str, str, int]]:
    """The logs this install will actually read, and how much is in each.

    Showing only "Claude Code CLI" was misleading: the desktop app writes to
    the same directory and Codex has a directory of its own, so a wizard that
    names one of them makes the other two look unsupported. A count of zero is
    informative too — it is the difference between "not installed" and
    "installed but this machine has never used it".
    """
    import glob as _glob
    import config

    cfg = config.load()
    found = []

    for label, key in (("Claude Code (CLI · 데스크톱 공용)", "claude_projects_dir"),
                       ("Codex CLI", "codex_sessions_dir")):
        root = config.expand(cfg["sources"].get(key, ""))
        count = (len(_glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True))
                 if root and os.path.isdir(root) else -1)
        found.append((label, root, count))

    for entry in cfg["sources"].get("extra_session_globs") or []:
        pattern = config.expand(entry.get("glob", ""))
        if not pattern:
            continue
        found.append((entry.get("label", "추가 세션"), pattern,
                      len(_glob.glob(pattern, recursive=True))))
    return found


def _shorten(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def _git_email() -> str:
    try:
        result = subprocess.run(["git", "config", "--global", "user.email"],
                                capture_output=True, timeout=10)
        return result.stdout.decode("utf-8", errors="replace").strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def main() -> int:
    platform_support.PLATFORM.configure_stdio()
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
