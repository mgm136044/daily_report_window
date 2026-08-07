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
