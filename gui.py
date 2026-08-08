"""Windowed entry point for the packaged build.

Separate from `cli.py` because the two need different subsystems: a console
executable attached to a terminal, and a windowed one with no console at all.
Which window to open is the only decision here.

    daily-report-gui          status
    daily-report-gui setup    the setup wizard
"""

from __future__ import annotations

import sys

import platform_support

# Same reason as cli.py: `config` warns on stderr while being imported, which
# happens before either window's main() gets a chance to set the encoding.
platform_support.PLATFORM.configure_stdio()


def is_configured() -> bool:
    """Has this install ever been set up *far enough to run*?

    Not "do both files exist". `install.ps1` copies `.env.example` to `.env`
    before it reaches the token gate, so the state it stops in — config written,
    token still missing — has both files present and was classified as
    configured. Someone who ran the installer and got as far as being told to
    fill in a token then opened the status window instead of the wizard, which
    is the one moment the wizard is for.

    A database id is the honest test: it is written only after the database has
    actually been created, which is the last thing setup does.
    """
    import os
    import paths

    if not os.path.exists(paths.data("config.toml")):
        return False
    try:
        from notion_upsert import load_env
        env = load_env(paths.data(".env"))
    except (OSError, ValueError):
        return False
    return bool(env.get("DAILY_REPORT_DATABASE_ID", "").strip()
                and env.get("DAILY_REPORT_NOTION_TOKEN", "").strip())


def main() -> int:
    argument = sys.argv[1] if len(sys.argv) > 1 else ""
    if argument in ("status", "--status"):
        import status_window
        return status_window.main()
    # No argument and nothing configured: open the wizard. Double-clicking the
    # application before setting anything up should start setting it up, not
    # show an empty dashboard and leave the person to find the other window.
    if argument in ("setup", "install", "--setup") or not is_configured():
        import setup_gui
        return setup_gui.main()
    import status_window
    return status_window.main()


if __name__ == "__main__":
    raise SystemExit(main())
