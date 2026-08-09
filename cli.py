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
  uninstall [--purge]  예약 작업·바로 가기 제거 (제거기가 부른다)
                     --purge 는 설정·자격증명·기록까지 지운다
  setup-db           노션 데이터베이스 생성 (설치 중 1회)
  collect YYYY-MM-DD [out.json]
  summarize <digest.json> <report.md> | --preflight
"""


def purge_data() -> int:
    """Delete the data root — config, credentials, ledger, collected material.

    Only reachable through `uninstall --purge`, which the uninstaller asks
    about explicitly. Uninstalling has always kept this directory, on purpose,
    so a reinstall keeps its Notion database instead of creating a second one.
    What was missing is that nobody was told: the `.env` holds a live Notion
    token and `work/` holds verbatim prompts, and both survived a Control
    Panel uninstall with no notice at all.

    **Refuses outside a packaged install.** From a checkout the data root is
    the source directory, so this would delete the repository — the working
    tree, the tests, everything. There is no situation where that is what
    somebody meant.
    """
    import shutil

    import paths

    root = paths.data_root()
    if not paths.bundled():
        print("체크아웃에서는 --purge 를 거부합니다.\n"
              f"  데이터 루트가 소스 디렉터리입니다: {root}", file=sys.stderr)
        return 2
    if not os.path.isdir(root):
        print(f"지울 데이터가 없습니다: {root}")
        return 0
    try:
        shutil.rmtree(root)
    except OSError as error:
        print(f"데이터를 지우지 못했습니다: {error}\n  직접 지우세요: {root}",
              file=sys.stderr)
        return 1
    print(f"설정과 기록을 삭제했습니다: {root}")
    return 0


def remove_installation(argv: list[str] | None = None) -> int:
    """Take the scheduled task and the shortcut away.

    Deleting the program without this leaves a task pointing at an executable
    that is gone. It does not disappear — it fires at 04:05 every night,
    fails to start, and records the failure, forever. Task Scheduler shows a
    red entry nobody can explain and nothing else does.

    Leaves the data alone unless `--purge` says otherwise. `config.toml`,
    `.env` and the ledger are the user's, and a reinstall that finds them
    intact keeps its Notion database rather than creating a second one.
    """
    import config
    import subprocess

    purge = "--purge" in (argv or [])

    if os.name != "nt":
        print("uninstall 명령은 Windows 전용입니다.", file=sys.stderr)
        return 2

    # `config.using_example()` matters more than an empty label here. Without a
    # config.toml, `load()` silently falls back to the example — whose label is
    # `com.example.daily-report` — so this reported "등록된 작업 없음" and exited
    # 0 while the real `com.<user>.daily-report` task stayed registered. Inno
    # runs this hidden, so the orphan the [UninstallRun] block exists to prevent
    # was created invisibly.
    if config.using_example():
        print("config.toml 이 없어 작업 이름을 알 수 없습니다.\n"
              "  남아 있는 작업을 직접 지우세요:\n"
              "    Get-ScheduledTask | Where-Object { $_.TaskName -like '*daily-report*' }",
              file=sys.stderr)
        return 1
    label = config.load().get("launchd", {}).get("label", "")
    if not label:
        print("config.toml 에서 작업 이름을 읽지 못했습니다 — 수동으로 제거하세요.",
              file=sys.stderr)
        return 1

    # The label goes through the same escaper the rest of the codebase uses to
    # build PowerShell literals. It was interpolated raw here, alone among the
    # places that do this — and a label containing a quote closes the literal
    # early, so the remainder is parsed as code. The installer generates the
    # label by stripping everything but `[a-z0-9]` from the account name, so
    # this is reached by hand-editing config.toml rather than by an attacker;
    # it is still a command assembled by string concatenation, in a file that
    # already knows better twenty lines away.
    script = (
        "$ErrorActionPreference = 'SilentlyContinue';"
        f"$label = {platform_support._ps_literal(label)};"
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
    if purge:
        return purge_data()
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
    import re

    argv = sys.argv[1:]
    command = "run"
    if argv and argv[0] in COMMANDS:
        command, argv = argv[0], argv[1:]
    elif argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    elif argv and not argv[0].startswith("-") \
            and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", argv[0]):
        # Anything that is neither a command nor a date used to fall through to
        # `run`, which then tried to parse it as one: `daily-report doctr` died
        # with `ValueError: time data 'doctr' does not match format` and a
        # traceback. The usage text below was unreachable.
        print(f"알 수 없는 명령: {argv[0]}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

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
        return remove_installation(argv)
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
