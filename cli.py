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

import os
import sys

import platform_support

# Before anything that might print — and `config` prints at *import* time when
# there is no config.toml. The spec asks for UTF-8 mode as well; this is the
# half that also fixes the console codepage, and neither alone was enough.
platform_support.PLATFORM.configure_stdio()

COMMANDS = ("run", "doctor", "collect", "summarize", "setup-db",
            "install", "uninstall")

USAGE = """사용법: daily-report [명령] [인자…]

  run [YYYY-MM-DD]   밀린 날짜 전부, 또는 특정 날짜 (기본 명령)
  doctor [--full]    상태 점검
  install            설정·작업 등록·바로 가기 (설치기가 부른다)
  uninstall          예약 작업·바로 가기 제거 (제거기가 부른다)
  setup-db           노션 데이터베이스 생성 (설치 중 1회)
  collect YYYY-MM-DD [out.json]
  summarize <digest.json> <report.md> | --preflight
"""


def remove_installation() -> int:
    """Take the scheduled task and the shortcut away.

    Deleting the program without this leaves a task pointing at an executable
    that is gone. It does not disappear — it fires at 04:05 every night,
    fails to start, and records the failure, forever. Task Scheduler shows a
    red entry nobody can explain and nothing else does.

    Deliberately leaves the data alone. `config.toml`, `.env` and the ledger
    are the user's, and a reinstall that finds them intact keeps its Notion
    database rather than creating a second one.
    """
    import config
    import subprocess

    if os.name != "nt":
        print("uninstall 명령은 Windows 전용입니다.", file=sys.stderr)
        return 2

    label = config.load().get("launchd", {}).get("label", "")
    if not label:
        print("config.toml 에서 작업 이름을 읽지 못했습니다 — 수동으로 제거하세요.",
              file=sys.stderr)
        return 1

    script = (
        "$ErrorActionPreference = 'SilentlyContinue';"
        f"$label = '{label}';"
        "$task = Get-ScheduledTask -TaskName $label;"
        "if ($task) { Unregister-ScheduledTask -TaskName $label -Confirm:$false;"
        "  Write-Host \"작업 제거: $label\" } else { Write-Host '등록된 작업 없음' };"
        "$link = Join-Path ([Environment]::GetFolderPath('Programs')) "
        "'하루 마감 보고서.lnk';"
        "if (Test-Path $link) { Remove-Item $link -Force; "
        "  Write-Host '바로 가기 제거' }"
    )
    import base64
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-EncodedCommand", encoded],
        capture_output=True)
    print(result.stdout.decode("utf-8", errors="replace").strip())
    print(f"설정과 기록은 남겨 둡니다: {__import__('paths').data_root()}")
    return 0


def run_installer(argv: list[str]) -> int:
    """Drive the bundled install.ps1, telling it to point at this executable.

    The packaged build has no python.exe and no run_day.py, so a scheduled
    task registered the ordinary way would reference files that are not there.
    Rather than write a second installer for the frozen case — and then have to
    verify task registration, the permission narrowing and the skill link all
    over again — the one that works is handed the two paths that differ.
    """
    import paths
    import platform_support

    if os.name != "nt":
        print("install 명령은 Windows 전용입니다. macOS 는 install.sh 를 쓰세요.",
              file=sys.stderr)
        return 2

    script = paths.resource("install.ps1")
    if not os.path.exists(script):
        print(f"install.ps1 을 찾지 못했습니다: {script}", file=sys.stderr)
        return 1

    executable = os.path.abspath(sys.executable)
    gui = os.path.join(os.path.dirname(executable), "daily-report-gui.exe")
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", script,
               "-AppExe", executable,
               # config.toml, .env and the ledger must not land inside the
               # bundle: it is replaced wholesale on upgrade, and the Notion
               # database id would go with it.
               "-DataDir", paths.data_root()]
    if os.path.exists(gui):
        command += ["-AppGuiExe", gui]
    command += argv

    import subprocess
    # `cwd` has to exist before subprocess will start anything — a missing one
    # is WinError 267 from CreateProcess, not from the installer, which reads
    # as "the installer is broken" rather than "the folder is not there yet".
    return subprocess.run(command, cwd=paths.ensure_data_root()).returncode


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
    if command == "setup-db":
        import setup_notion_db
        return setup_notion_db.main()
    if command == "install":
        return run_installer(argv)
    if command == "uninstall":
        return remove_installation()
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
