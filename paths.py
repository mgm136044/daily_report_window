"""Where the tool's files live — which is two places, not one.

Every module here computes its paths from `__file__`, which is correct while
the tool runs from a checkout and wrong the moment it is packaged: PyInstaller
extracts the bundle to a temporary directory that is deleted on exit. Left
alone, a frozen build would write `state/`, `work/` and `logs/` into that
temporary directory — so the ledger would vanish between runs and the job would
regenerate the same fortnight every night, forever, without ever failing.

So the two kinds of file have to be told apart:

  - **resources** ship with the tool and are never written: prompts, the
    scheduler templates, the example configurations.
  - **data** is written and must outlive the process: `config.toml`, `.env`,
    `state/`, `work/`, `logs/`.

Running from a checkout, both answers are the source directory — which is
exactly today's behaviour, unchanged. The split only becomes visible when
frozen, and that is the point: this module is correct on its own terms and
would be worth having even if the packaging never happened.
"""

from __future__ import annotations

import os
import sys

_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))


def bundled() -> bool:
    """True when running from a PyInstaller-style one-file/one-dir build."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_root() -> str:
    """Read-only files that shipped with the tool."""
    if bundled():
        return sys._MEIPASS  # type: ignore[attr-defined]
    return _SOURCE_DIR


def data_root() -> str:
    """Files the tool writes, which must survive the process.

    `DAILY_REPORT_HOME` overrides it. That exists for the packaged case, where
    a user may want the ledger somewhere other than `%LOCALAPPDATA%`, and it
    makes the frozen layout testable from a checkout without freezing anything.
    """
    override = os.environ.get("DAILY_REPORT_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if bundled():
        base = (os.environ.get("LOCALAPPDATA") if os.name == "nt" else "") \
            or os.path.join(os.path.expanduser("~"), ".local", "share")
        return os.path.join(base, "daily-report")
    return _SOURCE_DIR


def resource(*parts: str) -> str:
    return os.path.join(resource_root(), *parts)


def data(*parts: str) -> str:
    return os.path.join(data_root(), *parts)


def command_argv(subcommand: str, *arguments: str) -> list[str]:
    """How to invoke one of this tool's own commands from inside it.

    The windows shell out to run `doctor` and to regenerate a day. Built as
    `[sys.executable, "-X", "utf8", "doctor.py"]` that works from a checkout
    and does something quietly absurd when frozen: `sys.executable` is
    `daily-report-gui.exe`, `doctor.py` is not in the bundle, and the argument
    is not a command — so the dispatcher falls through to its default and
    **opens a second copy of the window**. The button appears to work, blocks
    until the duplicate is closed, and reports exit code 0 with no diagnostics.
    """
    if not bundled():
        return [sys.executable, "-X", "utf8",
                resource(f"{subcommand}.py"), *arguments]
    console = os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                           "daily-report.exe")
    if not os.path.exists(console):
        console = os.path.abspath(sys.executable)
    return [console, subcommand, *arguments]


def ensure_data_root() -> str:
    """Create the data directory, and return it.

    Running from a checkout the data root is the source directory, which
    obviously exists — so nothing needed this and nothing noticed it was
    missing. A packaged install writes to `%LOCALAPPDATA%\\daily-report`, which
    does not exist until something makes it, and the first thing to write there
    is the setup wizard's `.env`:

        .env 를 쓰지 못했습니다: [Errno 2] No such file or directory

    Anything that writes into the data root calls this first.
    """
    root = data_root()
    os.makedirs(root, exist_ok=True)
    return root
