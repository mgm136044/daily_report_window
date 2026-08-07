"""Everything that differs by operating system, in one place.

The collection and processing core — reading session logs, rolling up
projects, sanitizing, summarizing, writing to Notion — is portable. What is
not portable is the *runtime shell*: scheduling, single-instance locking,
watchdogs, desktop notifications, keeping the machine awake, the environment
handed to the CLI, console encoding, and file permissions.

macOS and Windows are implemented. Linux raises a clear error rather than
silently doing nothing, because a scheduled job that quietly does not run is
the worst possible failure — no report, no error, no signal that anything is
wrong. That has already happened once here, and it cost most of a day to find.

To add a platform, implement a class below and register it in `current()`.
Nothing outside this module needs to change.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile


class Unsupported(RuntimeError):
    """Raised instead of degrading silently."""


class Timeout(Exception):
    """The watchdog fired.

    Deliberately **not** `TimeoutError`, which is a subclass of `OSError`.

    The watchdog is delivered asynchronously — from a signal handler on macOS,
    from a timer thread on Windows — so it lands at whatever bytecode boundary
    the run happens to be on. Those boundaries are almost always inside the
    per-transcript and per-repository loops, and every one of them is wrapped
    in `except OSError` or `except (subprocess.SubprocessError, OSError)` to
    tolerate a file vanishing mid-scan.

    A `TimeoutError` therefore got caught by the collector, logged as one more
    unreadable file, and discarded. The watchdog is one-shot, so after that it
    was gone for the day: the stalled run continued to completion, wrote a
    report, and recorded the date as done. The guard against a hung job was
    silently disarmed by the error handling of the thing it was guarding.
    """


def _decode(raw: bytes) -> str:
    """Decode console output without trusting the process locale.

    Windows console tools answer in the OEM codepage (cp949 on a Korean
    install), while `text=True` decodes with the *ANSI* codepage. The two
    differ, and the mismatch does not fail loudly — it raises
    UnicodeDecodeError deep inside subprocess's reader thread, which surfaces
    as an unrelated empty result.
    """
    for codec in ("oem", "utf-8", "cp1252"):
        try:
            return raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


class Platform:
    """What the runtime shell needs from an operating system."""

    name = "unknown"
    supported = False

    # --- single instance -------------------------------------------------
    def acquire_lock(self, path: str):
        """Return a handle if this process may run, or None if another holds it."""
        raise Unsupported(self._message("단일 실행 잠금"))

    def lock_is_held(self, path: str) -> bool | None:
        """Is someone holding the lock right now? None means "cannot tell"."""
        raise Unsupported(self._message("잠금 상태 조회"))

    # --- watchdog --------------------------------------------------------
    def watchdog(self, seconds: int):
        """Context manager that aborts the run after `seconds`."""
        raise Unsupported(self._message("워치독"))

    # --- staying awake ---------------------------------------------------
    def keep_awake_argv(self) -> list[str]:
        """Prefix that keeps the machine awake for the wrapped command."""
        return []

    def keep_awake(self):
        """Context manager holding a no-sleep assertion for the caller.

        The counterpart to `keep_awake_argv`: platforms that cannot wrap the
        command from the scheduler hold the assertion from inside the process
        instead. Exactly one of the two does the work on any given platform.
        """
        from contextlib import nullcontext
        return nullcontext()

    # --- notification ----------------------------------------------------
    def notify(self, title: str, message: str) -> None:
        raise Unsupported(self._message("알림"))

    # --- scheduler -------------------------------------------------------
    def scheduler_status(self, label: str) -> tuple[bool, str]:
        """(registered, human-readable detail)"""
        raise Unsupported(self._message("스케줄러 조회"))

    # Answering rather than raising, so `doctor.py` stays importable: its whole
    # job is to explain why nothing is running, and a diagnostic tool that
    # cannot start on the machine with the problem is no use. An empty path
    # reads as "not registered", which is the truth here.
    def scheduler_path(self, label: str) -> str:
        """Where the scheduler's own definition of this job lives."""
        return ""

    def scheduler_repair(self, label: str) -> str:
        """The command that re-registers the job, for doctor.py to print."""
        return self._message("스케줄러 등록")

    # --- the CLI child ---------------------------------------------------
    #
    # These three answer generically rather than raising, unlike the scheduler
    # and the lock above. The difference is not politeness: `summarize.py`
    # resolves the PATH at *import* time, so raising here made the module
    # unimportable on an unsupported platform — and with it the whole test
    # suite, which stopped collecting rather than reporting. The collection and
    # processing core really is portable, and its tests should run anywhere.
    #
    # `require_supported()` remains the gate. Refusing to start belongs there,
    # once, where the message can say what is actually missing.
    def default_path(self) -> str:
        """PATH the summarizer's child process gets, built rather than inherited."""
        return os.defpath.lstrip(os.pathsep)

    def child_env(self) -> dict[str, str]:
        """Environment for the Claude Code CLI child process."""
        home = os.path.expanduser("~")
        return {
            "HOME": home,
            "PATH": self.default_path(),
            "USER": os.environ.get("USER") or os.path.basename(home),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        }

    def claude_argv(self, configured: str = "") -> list[str]:
        """How to invoke the Claude Code CLI, resolved to something runnable."""
        return [configured or "claude"]

    # --- filesystem ------------------------------------------------------
    def restrict(self, path: str, is_dir: bool) -> None:
        """Narrow a path so other accounts on the machine cannot read it.

        POSIX modes are the default because they are correct wherever they
        exist, and because the alternative — raising — travels through
        `write_state()`, which runs on every completed day. Windows overrides
        this; it has no POSIX mode to set.
        """
        try:
            os.chmod(path, 0o700 if is_dir else 0o600)
        except OSError:
            pass

    def scratch_dir(self, name: str) -> str:
        """A temp directory the summarizer can run from."""
        return os.path.join(tempfile.gettempdir(), name)

    # --- console ---------------------------------------------------------
    def configure_stdio(self) -> None:
        """Make stdout/stderr able to carry the text this tool prints."""
        return None

    def _message(self, what: str) -> str:
        return (f"{what} 는 {self.name} 에서 아직 구현되지 않았습니다. "
                f"현재 지원 플랫폼: macOS, Windows. "
                f"구현하려면 platform_support.py 에 클래스를 추가하세요.")


class MacOS(Platform):
    name = "macOS"
    supported = True

    def acquire_lock(self, path: str):
        import fcntl
        handle = open(path, "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle

    def lock_is_held(self, path: str) -> bool | None:
        result = subprocess.run(["lsof", path], capture_output=True)
        holders = [l for l in _decode(result.stdout).splitlines()[1:] if l.strip()]
        return bool(holders)

    def watchdog(self, seconds: int):
        import signal

        class _Watchdog:
            def __enter__(self_inner):
                if seconds > 0:
                    signal.signal(signal.SIGALRM, self_inner._fire)
                    signal.alarm(seconds)
                return self_inner

            def __exit__(self_inner, *exc):
                signal.alarm(0)
                return False

            def _fire(self_inner, signum, frame):
                raise Timeout(f"실행이 {seconds}초를 넘겨 중단했습니다")

        return _Watchdog()

    def keep_awake_argv(self) -> list[str]:
        # Holds the assertion only while the wrapped command runs. Without it a
        # Mac running Power Nap cycles DarkWake → Maintenance Sleep and freezes
        # the job mid-run; one run began at 04:05 and finished at 13:52.
        return ["/usr/bin/caffeinate", "-s", "-i"]

    def notify(self, title: str, message: str) -> None:
        import json as _json
        try:
            subprocess.run(
                ["osascript", "-e",
                 f"display notification {_json.dumps(message)} with title {_json.dumps(title)}"],
                capture_output=True, timeout=10)
        except (subprocess.SubprocessError, OSError):
            pass

    def scheduler_status(self, label: str) -> tuple[bool, str]:
        result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            return False, "launchd 에 등록되어 있지 않습니다"
        running = "state = running" in result.stdout
        exit_code = next((l.strip() for l in result.stdout.splitlines()
                          if "last exit code" in l), "")
        detail = f"현재: {'실행 중' if running else '대기'}    {exit_code}"
        if "USER" not in result.stdout:
            detail += "\n⚠️  plist 에 USER 가 없습니다 — 'Not logged in' 으로 매일 조용히 실패합니다"
        return True, detail

    def scheduler_path(self, label: str) -> str:
        return os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")

    def scheduler_repair(self, label: str) -> str:
        return f"launchctl bootstrap gui/$(id -u) {self.scheduler_path(label)}"

    def default_path(self) -> str:
        return "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    def child_env(self) -> dict[str, str]:
        env = {
            "HOME": os.path.expanduser("~"),
            "PATH": self.default_path(),
            # Credentials live in the login keychain under the account name, so
            # without USER the CLI reports "Not logged in" even though the user
            # is logged in. launchd passes almost no environment.
            "USER": os.environ.get("USER") or os.path.basename(os.path.expanduser("~")),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TZ": "Asia/Seoul",
        }
        if os.environ.get("SHELL"):
            env["SHELL"] = os.environ["SHELL"]
        return env

    def claude_argv(self, configured: str = "") -> list[str]:
        return [configured or "claude"]

    def restrict(self, path: str, is_dir: bool) -> None:
        try:
            os.chmod(path, 0o700 if is_dir else 0o600)
        except OSError:
            pass

    def scratch_dir(self, name: str) -> str:
        return os.path.join("/private/tmp", name)


class Windows(Platform):
    """Task Scheduler, msvcrt byte-range locks, and a thread watchdog.

    Four things here are not the obvious translation of the macOS version:

      - **The watchdog counts wall time, not running time.** `signal.alarm`
        excludes time the machine was asleep, which is why aborting a merely
        suspended run is not a concern on macOS. Windows has no equivalent, so
        the run holds a no-sleep assertion for its whole duration instead
        (`keep_awake`) and the timer is a plain wall clock.
      - **Console output is forced to UTF-8.** Reports, log lines and the
        doctor's own symbols are Korean plus emoji; a Task Scheduler run
        redirects stdout to a file encoded in the ANSI codepage, where the
        first Korean character raises UnicodeEncodeError and kills the run.
      - **Permissions are set on the directory, with inheritance.** `chmod` is
        per-file and free; `icacls` is a process spawn. Applying it per file
        would spawn one process per artifact on every run.
      - **The task must run under an interactive token.** The CLI's credentials
        are DPAPI-protected against the logged-on user; "run whether user is
        logged on or not" hands the job an S4U token that cannot decrypt them,
        which is the exact Windows shape of the macOS `USER` failure.
    """

    name = "Windows"
    supported = True

    def __init__(self) -> None:
        # icacls is a process spawn, so each directory is narrowed at most once
        # per run. Files inherit from the directory and need no call of their
        # own. Per-instance rather than per-class: a shared set would let one
        # Platform object's bookkeeping suppress another's ACL call.
        self._restricted: set[str] = set()

    # --- single instance -------------------------------------------------
    def acquire_lock(self, path: str):
        import msvcrt
        # `open` is deliberately NOT guarded, matching macOS.
        #
        # Returning None means exactly one thing: another run holds the lock,
        # and `run_day.main()` answers that by printing "이미 실행 중입니다" and
        # exiting **0**. Folding "the file could not be opened at all" into the
        # same answer turns a read-only attribute, a vanished network path or a
        # permissions problem into a successful-looking daily no-op — Task
        # Scheduler records LastTaskResult 0, no notification fires, and the
        # doctor's lock check says "정상일 수 있음". No report, no error, no
        # signal. Letting it raise costs a traceback and an exit 1, which is
        # the whole point.
        #
        # "a+" rather than "w": truncating the file is not what takes the lock,
        # and a crash between truncate and lock would leave nothing behind.
        handle = open(path, "a+")
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return None
        return handle

    def lock_is_held(self, path: str) -> bool | None:
        """Probe by trying to take it. Windows exposes no holder listing.

        Safe only because the caller (doctor.py) never holds the lock itself.
        """
        if not os.path.exists(path):
            return False
        try:
            handle = self.acquire_lock(path)
        except OSError:
            return None  # cannot tell — and the doctor says so rather than guessing
        if handle is None:
            return True
        import msvcrt
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        handle.close()
        return False

    # --- watchdog --------------------------------------------------------
    def watchdog(self, seconds: int):
        """Raise TimeoutError in the main thread from a timer thread.

        `PyThreadState_SetAsyncExc` is delivered at the next bytecode boundary,
        so it cannot interrupt a thread blocked in `subprocess.wait()`. That is
        acceptable here because every subprocess this tool spawns already
        carries its own timeout — the watchdog exists for the case where the
        *run as a whole* stops making progress, not for a single hung child.
        """
        import ctypes
        import threading

        main_thread_id = threading.main_thread().ident

        class _Watchdog:
            def __enter__(self_inner):
                self_inner.fired = False
                self_inner.timer = None
                if seconds > 0:
                    self_inner.timer = threading.Timer(seconds, self_inner._fire)
                    self_inner.timer.daemon = True
                    self_inner.timer.start()
                return self_inner

            def __exit__(self_inner, *exc):
                if self_inner.timer is not None:
                    self_inner.timer.cancel()
                # The timer can fire in the instant between the work finishing
                # and this cancel. Clearing a pending async exception here stops
                # it from landing on unrelated code later in the run — the
                # Windows shape of the "alarm not cleared" defect.
                if self_inner.fired and exc[0] is not Timeout:
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_ulong(main_thread_id), ctypes.c_void_p(0))
                return False

            def _fire(self_inner):
                self_inner.fired = True
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(main_thread_id), ctypes.py_object(Timeout))

        return _Watchdog()

    # --- staying awake ---------------------------------------------------
    def keep_awake_argv(self) -> list[str]:
        # There is no caffeinate to wrap the command with; the assertion is
        # held from inside the process instead. See keep_awake().
        return []

    def keep_awake(self):
        """Hold ES_SYSTEM_REQUIRED for the duration of the run.

        The Windows counterpart to caffeinate. Without it a machine on Modern
        Standby suspends mid-collection exactly the way a Mac on Power Nap
        does, and — unlike macOS — the wall-clock watchdog then aborts the day.
        """
        import ctypes
        from contextlib import contextmanager

        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        ES_AWAYMODE_REQUIRED = 0x00000040

        @contextmanager
        def _assertion():
            set_state = ctypes.windll.kernel32.SetThreadExecutionState
            # The return value is the *previous* execution state, or 0 if the
            # call failed — so a zero here means the request was refused, not
            # that nothing was held before. Away mode keeps the machine working
            # with the screen off and is not granted on every SKU; a refusal
            # falls back to plain "do not sleep" rather than giving up.
            granted = set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
            if not granted:
                granted = set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            try:
                yield granted != 0
            finally:
                set_state(ES_CONTINUOUS)

        return _assertion()

    # --- notification ----------------------------------------------------
    def notify(self, title: str, message: str) -> None:
        """Toast if the runtime allows it, balloon otherwise.

        Both paths are attempted inside one PowerShell process: WinRT toasts
        need an AppUserModelID that is registered in the Start menu, which is
        not guaranteed, and a failure there must not swallow the alert. A job
        that fails silently is worse than none.
        """
        script = _POWERSHELL_NOTIFY.format(title=_ps_literal(title),
                                           message=_ps_literal(message))
        _powershell(script, timeout=30)

    # --- scheduler -------------------------------------------------------
    def scheduler_status(self, label: str) -> tuple[bool, str]:
        """Ask the ScheduledTasks module, not schtasks.

        `schtasks /Query /V` prints localized field names — on a Korean install
        every label to parse is Korean — while Get-ScheduledTask returns
        objects whose property names are fixed.
        """
        import json as _json

        ok, out = _powershell(_POWERSHELL_TASK_STATUS.format(label=_ps_literal(label)),
                              timeout=60)
        if not ok or not out.strip():
            return False, "작업 스케줄러에 등록되어 있지 않습니다"
        try:
            info = _json.loads(out.strip())
        except ValueError:
            return False, f"작업 상태를 해석하지 못했습니다: {out.strip()[:200]}"
        if info.get("error"):
            return False, "작업 스케줄러에 등록되어 있지 않습니다"

        state = info.get("State") or "?"
        # Task Scheduler reports "never run" as a result code and a date in
        # 1999 rather than as an absence, so both have to be translated or the
        # doctor's output reads like a failure on a healthy install.
        last_run = info.get("LastRunTime") or ""
        if last_run.startswith(("1899-", "1999-")):
            last_run = ""
        detail = (f"현재: {'실행 중' if state == 'Running' else state}    "
                  f"마지막 결과 {_task_result(info.get('LastTaskResult'))}    "
                  f"마지막 실행 {last_run or '없음'}    "
                  f"다음 실행 {info.get('NextRunTime') or '없음'}")

        # The Windows shape of the macOS "USER is missing from the plist"
        # failure: an S4U/Password principal gets a token that cannot decrypt
        # the CLI's DPAPI-protected credentials, so it reports "Not logged in"
        # every night and nothing else looks wrong.
        logon = (info.get("LogonType") or "")
        if logon not in ("Interactive", "InteractiveToken", "InteractiveOrPassword", "S4U"):
            detail += (f"\n⚠️  로그온 유형이 {logon} 입니다 — 자격 증명을 읽지 못해 "
                       f"'Not logged in' 으로 매일 조용히 실패합니다")
        elif logon == "S4U":
            detail += ("\n⚠️  '사용자의 로그온 여부에 관계없이 실행' 로 등록돼 있습니다 — "
                       "자격 증명을 읽지 못해 'Not logged in' 으로 실패합니다")
        if info.get("StartWhenAvailable") is False:
            detail += ("\n⚠️  StartWhenAvailable 이 꺼져 있습니다 — PC 가 꺼져 있던 날은 "
                       "실행 자체가 건너뛰어집니다")
        if info.get("Enabled") is False:
            detail += "\n⚠️  작업이 사용 안 함 상태입니다"
        # A job whose last run exited non-zero is a problem even when every
        # other field looks healthy — that combination is precisely what a
        # nightly failure looks like from here.
        if _task_failed(info.get("LastTaskResult")):
            detail += ("\n⚠️  마지막 실행이 실패로 끝났습니다 — logs/stderr.log 를 보세요")
        return True, detail

    def scheduler_path(self, label: str) -> str:
        return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "Tasks", label)

    def scheduler_repair(self, label: str) -> str:
        return "powershell -ExecutionPolicy Bypass -File install.ps1"

    # --- the CLI child ---------------------------------------------------
    def default_path(self) -> str:
        """Built rather than inherited, so a scheduled run matches a shell run.

        SystemRoot\\system32 is not optional padding: without it the child
        cannot load the Winsock provider and fails before running any code.
        """
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        home = os.path.expanduser("~")
        parts = [
            os.path.join(system_root, "system32"),
            system_root,
            os.path.join(system_root, "System32", "Wbem"),
            os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0"),
            os.path.join(home, ".local", "bin"),
            os.path.join(os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")), "npm"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd"),
        ]
        return os.pathsep.join(parts)

    def child_env(self) -> dict[str, str]:
        home = os.path.expanduser("~")
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        env = {
            "PATH": self.default_path(),
            "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
            "SystemRoot": system_root,
            "windir": os.environ.get("windir", system_root),
            "ComSpec": os.environ.get("ComSpec", os.path.join(system_root, "system32", "cmd.exe")),
            "SystemDrive": os.environ.get("SystemDrive", "C:"),
            # The CLI resolves its config and credential store from these.
            # USERPROFILE is the Windows shape of the macOS HOME/USER pair.
            "USERPROFILE": home,
            "HOME": home,
            "HOMEDRIVE": os.environ.get("HOMEDRIVE", os.path.splitdrive(home)[0]),
            "HOMEPATH": os.environ.get("HOMEPATH", os.path.splitdrive(home)[1]),
            "APPDATA": os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")),
            "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local")),
            "PROGRAMDATA": os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
            "ProgramFiles": os.environ.get("ProgramFiles", r"C:\Program Files"),
            "USERNAME": os.environ.get("USERNAME") or os.path.basename(home),
            "USER": os.environ.get("USERNAME") or os.path.basename(home),
            "NUMBER_OF_PROCESSORS": os.environ.get("NUMBER_OF_PROCESSORS", "1"),
            "PROCESSOR_ARCHITECTURE": os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"),
        }
        for name in ("TEMP", "TMP"):
            if os.environ.get(name):
                env[name] = os.environ[name]
        if os.environ.get("USERDOMAIN"):
            env["USERDOMAIN"] = os.environ["USERDOMAIN"]
        return env

    def claude_argv(self, configured: str = "") -> list[str]:
        """Resolve the CLI to something CreateProcess can actually start.

        `.cmd` shims (npm installs) are not executables; they have to go
        through the command interpreter. A native install is a real `.exe` and
        runs directly, so that is preferred when both exist.
        """
        import shutil

        candidates = []
        if configured:
            candidates.append(configured)
        home = os.path.expanduser("~")
        candidates += [
            os.path.join(home, ".local", "bin", "claude.exe"),
            os.path.join(home, ".local", "bin", "claude.cmd"),
        ]
        found = shutil.which("claude", path=self.default_path())
        if found:
            candidates.append(found)
        appdata = os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming"))
        candidates += [
            os.path.join(appdata, "npm", "claude.cmd"),
            os.path.join(appdata, "npm", "claude.exe"),
        ]

        resolved = next((c for c in candidates if c and os.path.isfile(c)), "")
        if not resolved:
            return ["claude"]  # let the FileNotFoundError say so
        if resolved.lower().endswith((".cmd", ".bat")):
            comspec = os.environ.get("ComSpec") or os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "system32", "cmd.exe")
            return [comspec, "/d", "/c", resolved]
        return [resolved]

    # --- filesystem ------------------------------------------------------
    def restrict(self, path: str, is_dir: bool) -> None:
        """Narrow a directory to this account, and let its files inherit.

        SYSTEM and Administrators are kept: on macOS `chmod 700` still leaves
        root able to read, and stripping them here breaks backup and servicing
        without protecting anything an administrator could not take anyway.
        Well-known SIDs rather than names, because the names are localized.
        """
        if not is_dir:
            return  # inherited from the directory's ACL
        key = os.path.normcase(os.path.abspath(path))
        if key in self._restricted:
            return
        self._restricted.add(key)

        account = os.environ.get("USERNAME") or os.path.basename(os.path.expanduser("~"))
        domain = os.environ.get("USERDOMAIN")
        principal = f"{domain}\\{account}" if domain else account
        try:
            subprocess.run(
                ["icacls", path, "/inheritance:r",
                 "/grant:r", f"{principal}:(OI)(CI)F",
                 "/grant:r", "*S-1-5-18:(OI)(CI)F",      # SYSTEM
                 "/grant:r", "*S-1-5-32-544:(OI)(CI)F",  # Administrators
                 "/Q"],
                capture_output=True, timeout=30)
        except (subprocess.SubprocessError, OSError):
            pass

    # --- console ---------------------------------------------------------
    def configure_stdio(self) -> None:
        """Force UTF-8 out.

        Under Task Scheduler stdout is a redirected file opened in the ANSI
        codepage. Every message this tool prints is Korean, and the doctor
        prints emoji, so the first line of output would raise
        UnicodeEncodeError and take the run down before any work happened.
        """
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except (AttributeError, OSError):
            pass
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError, OSError):
                pass


class Linux(Platform):
    """Not implemented. `systemd --user` timers would be the natural fit."""

    name = "Linux"
    supported = False


# --- PowerShell helpers ------------------------------------------------------
#
# Scripts are handed over as -EncodedCommand (UTF-16LE, base64). Passing them
# as plain arguments means the Korean text in a notification travels through
# the console codepage and through PowerShell's own quoting rules, and both
# have already mangled it.

_POWERSHELL_NOTIFY = """
$ErrorActionPreference = 'Stop'
$title = {title}
$message = {message}
try {{
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
    $nodes = $template.GetElementsByTagName('text')
    $nodes.Item(0).AppendChild($template.CreateTextNode($title)) | Out-Null
    $nodes.Item(1).AppendChild($template.CreateTextNode($message)) | Out-Null
    $appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
        [Windows.UI.Notifications.ToastNotification]::new($template))
}} catch {{
    Add-Type -AssemblyName System.Windows.Forms
    $icon = New-Object System.Windows.Forms.NotifyIcon
    $icon.Icon = [System.Drawing.SystemIcons]::Warning
    $icon.Visible = $true
    $icon.ShowBalloonTip(10000, $title, $message, [System.Windows.Forms.ToolTipIcon]::Warning)
    Start-Sleep -Seconds 6
    $icon.Dispose()
}}
"""

_POWERSHELL_TASK_STATUS = """
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
try {{
    $task = Get-ScheduledTask -TaskName {label}
    $info = Get-ScheduledTaskInfo -TaskName {label}
    [pscustomobject]@{{
        State            = $task.State.ToString()
        Enabled          = [bool]$task.Settings.Enabled
        LogonType        = $task.Principal.LogonType.ToString()
        StartWhenAvailable = [bool]$task.Settings.StartWhenAvailable
        LastRunTime      = if ($info.LastRunTime) {{ $info.LastRunTime.ToString('yyyy-MM-dd HH:mm') }} else {{ $null }}
        LastTaskResult   = $info.LastTaskResult
        NextRunTime      = if ($info.NextRunTime) {{ $info.NextRunTime.ToString('yyyy-MM-dd HH:mm') }} else {{ $null }}
    }} | ConvertTo-Json -Compress
}} catch {{
    '{{"error":"not found"}}'
}}
"""


# Task Scheduler's own status values, which it reports through the same field
# as a program's exit code. 267011 is not a failure — it is "has not run yet",
# which every fresh install shows.
_TASK_RESULTS = {
    0: "정상 (0)",
    1: "실패 (1)",
    267008: "준비됨",
    267009: "실행 중",
    267010: "실행 대기 중",
    267011: "아직 실행된 적 없음",
    267014: "사용자가 중단함",
    2147750687: "이미 실행 중이라 건너뜀",
}


# Status values that are not the exit code of a run that finished badly.
_TASK_NOT_A_FAILURE = {0, 267008, 267009, 267010, 267011, 2147750687}


def _task_result(code) -> str:
    if code is None:
        return "없음"
    try:
        code = int(code)
    except (TypeError, ValueError):
        return str(code)
    return _TASK_RESULTS.get(code, f"0x{code & 0xFFFFFFFF:08X} ({code})")


def _task_failed(code) -> bool:
    try:
        return int(code) not in _TASK_NOT_A_FAILURE
    except (TypeError, ValueError):
        return False


def _ps_literal(text: str) -> str:
    """A PowerShell single-quoted literal; nothing inside it is expanded."""
    return "'" + str(text).replace("'", "''") + "'"


def _powershell(script: str, timeout: int = 60) -> tuple[bool, str]:
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return False, ""
    out = result.stdout.decode("utf-8", errors="replace")
    return result.returncode == 0, out


def current() -> Platform:
    if sys.platform == "darwin":
        return MacOS()
    if sys.platform.startswith("win"):
        return Windows()
    return Linux()


PLATFORM = current()


def require_supported() -> None:
    """Fail loudly at startup rather than at 4 a.m."""
    if not PLATFORM.supported:
        raise Unsupported(
            f"{PLATFORM.name} 는 아직 지원하지 않습니다. 현재 지원 플랫폼: macOS, Windows.\n"
            f"수집·정제·요약·적재는 이식 가능하지만 스케줄러·잠금·워치독·알림이 "
            f"플랫폼마다 다릅니다. platform_support.py 를 참고하세요.")
