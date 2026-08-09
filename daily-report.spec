# PyInstaller build. -*- mode: python ; coding: utf-8 -*-
#
#     pyinstaller daily-report.spec --noconfirm
#
# **one-dir, not one-file.** A one-file build extracts the whole bundle to a
# temporary directory on every launch: slower to start, and far more likely to
# be flagged by antivirus, because "executable unpacks itself and runs from
# temp" is what packers do. Neither is a good property for something that runs
# unattended at 04:05.
#
# Two executables share one directory: a console one for the job and the
# diagnostics, a windowed one for the status and setup windows. They cannot be
# the same binary — a console subsystem executable flashes a window, and a
# windowed one has no stdout for the log.

import os

RESOURCES = [
    # (source, destination inside the bundle). These are read at runtime
    # through paths.resource(), which resolves to sys._MEIPASS when frozen.
    ("prompts", "prompts"),
    ("templates", "templates"),
    ("skills", "skills"),
    # The setup wizard's "토큰 발급 방법 열기" button opens one of these, and
    # install.ps1 prints their paths at the token gate. Left out, the one
    # button that tells a new user how to get the Notion token — in the window
    # whose whole reason to exist is the token field — opened nothing.
    #
    # Named one by one, never `("docs", "docs")`.
    #
    # That is what this used to say, and it collected the *directory*. In the
    # public checkout the directory holds these four files and nothing else, so
    # CI built a correct bundle and the mistake was invisible. In the private
    # tree it holds twenty-eight, and the extra twenty-four are the development
    # notes and the release plan — the files `export_public.py` refuses to
    # publish, in its own words because they carry client names, project names
    # and dates sentence by sentence.
    #
    # This spec's usage comment tells you to run `pyinstaller daily-report.spec`.
    # Run that from the private tree and every one of those files goes into the
    # installer, and from there onto the disk of everyone who installs it.
    # Neither leak checker would notice: `check_binary_no_pii.py` looks only
    # for identifiers belonging to the *build machine*, and `check_no_pii.py`
    # was never pointed at `dist/`.
    ("docs/design.md", "docs"),
    ("docs/design.ko.md", "docs"),
    ("docs/notion-setup.md", "docs"),
    ("docs/notion-setup.ko.md", "docs"),
    ("config.example.toml", "."),
    ("config.windows.example.toml", "."),
    (".env.example", "."),
    ("install.ps1", "."),
    ("install.sh", "."),
]

# tkinter is imported inside main() so the logic stays importable headless;
# PyInstaller's static analysis cannot see through that, so it is named here.
GUI_IMPORTS = ["tkinter", "tkinter.ttk", "tkinter.scrolledtext", "tkinter.font"]

# Nothing here talks to the network except through urllib, and nothing needs a
# scientific stack. Excluding these keeps the bundle from quietly absorbing
# whatever else is installed in the build environment.
EXCLUDES = ["numpy", "pandas", "matplotlib", "PIL", "pytest", "setuptools", "pip"]

# The frozen build has no command line to carry `-X utf8 -u`, and losing them
# is not cosmetic. Measured: without utf8 mode the first line the job prints —
# a warning emitted by `config` at *import* time, before any code has had a
# chance to call configure_stdio() — comes out as mojibake, and under the
# scheduler that line is written to a file in the ANSI codepage where Korean
# raises UnicodeEncodeError outright.
#
# `-u` matters for the same reason it does in the plist: a redirected log that
# block-buffers stays empty while the job runs, which is exactly when someone
# is reading it.
RUNTIME_OPTIONS = [("X utf8", None, "OPTION"), ("u", None, "OPTION")]


cli_analysis = Analysis(
    ["cli.py"],
    pathex=[],
    binaries=[],
    datas=RESOURCES,
    hiddenimports=[],
    excludes=EXCLUDES,
    noarchive=False,
)

gui_analysis = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=[],
    datas=RESOURCES,
    hiddenimports=GUI_IMPORTS,
    excludes=EXCLUDES,
    noarchive=False,
)

cli_pyz = PYZ(cli_analysis.pure)
gui_pyz = PYZ(gui_analysis.pure)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    RUNTIME_OPTIONS,
    exclude_binaries=True,
    name="daily-report",
    console=True,
    disable_windowed_traceback=False,
    upx=False,          # UPX compression is a reliable way to get flagged
)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    RUNTIME_OPTIONS,
    exclude_binaries=True,
    name="daily-report-gui",
    console=False,
    disable_windowed_traceback=False,
    upx=False,
)

COLLECT(
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.datas,
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.datas,
    strip=False,
    upx=False,
    name="daily-report",
)
