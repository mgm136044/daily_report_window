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


def main() -> int:
    wants_setup = len(sys.argv) > 1 and sys.argv[1] in ("setup", "install", "--setup")
    if wants_setup:
        import setup_gui
        return setup_gui.main()
    import status_window
    return status_window.main()


if __name__ == "__main__":
    raise SystemExit(main())
