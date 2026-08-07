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
    """Has this install ever been set up?

    Both files, not either. `config.toml` alone means the installer wrote it
    and then stopped at the token gate — which is the state a first run is
    most likely to be in, and the one where showing a status window full of
    blanks is least useful.
    """
    import os
    import paths
    return (os.path.exists(paths.data("config.toml"))
            and os.path.exists(paths.data(".env")))


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
