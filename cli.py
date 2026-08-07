"""Console entry point for the packaged build.

A frozen executable has one entry point, but this tool has several commands
that people already run by name. Rather than ship four executables, one
dispatches:

    daily-report                 run every outstanding day (the default)
    daily-report run 2026-08-04  one specific day
    daily-report doctor          diagnostics
    daily-report collect DATE    collection only, for debugging

Running from a checkout, `python run_day.py` still works exactly as before —
this file is additive.
"""

from __future__ import annotations

import sys

import platform_support

# Before anything that might print — and `config` prints at *import* time when
# there is no config.toml. The spec asks for UTF-8 mode as well; this is the
# half that also fixes the console codepage, and neither alone was enough.
platform_support.PLATFORM.configure_stdio()

COMMANDS = ("run", "doctor", "collect", "summarize")

USAGE = """사용법: daily-report [명령] [인자…]

  run [YYYY-MM-DD]   밀린 날짜 전부, 또는 특정 날짜 (기본 명령)
  doctor [--full]    상태 점검
  collect YYYY-MM-DD [out.json]
  summarize <digest.json> <report.md> | --preflight
"""


def main() -> int:
    argv = sys.argv[1:]
    command = "run"
    if argv and argv[0] in COMMANDS:
        command, argv = argv[0], argv[1:]
    elif argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0

    # Rewritten *before* the imports below. `run_day` decides at import time
    # whether to redirect its output, and every one of these modules reads
    # sys.argv directly — leaving the subcommand in place would make `run`
    # look like a date.
    sys.argv = [sys.argv[0], *argv]

    if command == "run":
        import run_day
        return run_day.main()
    if command == "doctor":
        import doctor
        return doctor.main()
    if command == "collect":
        import collect
        return collect.main()
    if command == "summarize":
        import summarize
        return summarize.main()
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
