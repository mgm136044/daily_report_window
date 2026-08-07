# daily-report

Writes one row per day into a Notion database describing what you actually did:
which projects you worked in, what you asked for, which files were created or
changed and where, which commands ran, and what you planned next.

Nothing new is tracked. Your coding agents already record all of this — the job
reads those records, throws away the 99.9% nobody wants to read again, and turns
what is left into a report.

[한국어 README](README.ko.md) · [Design notes](docs/design.md) · [Notion setup](docs/notion-setup.md)

## What it reads

| Source | Contributes |
|---|---|
| Claude Code transcripts (`~/.claude/projects/**/*.jsonl`) | prompts, file edits, commands, plans |
| Codex CLI rollouts (`~/.codex/sessions/**/*.jsonl`) | prompts, patches, commands, plans |
| git, across every repository under a configured root | commits authored by you |
| the filesystem, in projects the day already touched | files an agent produced by running a script, which no tool log captures |

Each source is optional in practice: if Codex is not installed, or no repository
is found, that part is simply empty.

## How it runs

```
04:05  launchd (macOS) · Task Scheduler (Windows)
  ①  decide which days are outstanding   compared against state/lastrun.json
  ②  collect                             transcripts + rollouts + commits + disk
  ③  refine                              roll up per project, drop navigation noise
  ④  sanitize                            mask credentials — before the model sees it
  ⑤  summarize                           one `claude -p` call produces the report
  ⑥  sanitize                            again, before anything is published
  ⑦  upsert                              one row per date
```

A logical day runs **04:00 to 04:00**, so work done at 2 a.m. belongs to the
previous day. The job fires at 04:05, just after that day has closed.

If the machine was asleep or off, the missed days are picked up on the next
run. `launchd` collapses missed schedules into a single execution; Task
Scheduler's default is to drop them entirely, so `StartWhenAvailable` is set to
give it the same property. Either way the job keeps its own ledger and
backfills from it.

## Requirements

- **macOS, or Windows 10/11.** See [Platform support](#platform-support).
- **Python 3.11+.** Standard library only — there is nothing to install.
- **Claude Code CLI, logged in.** The summary is produced by `claude -p`.
- **A Notion internal integration token** and a page to create the database under.

## Setup

macOS:

```bash
bash install.sh
```

Windows (no elevation required):

```bash
powershell -ExecutionPolicy Bypass -File install.ps1
```

<details>
<summary><b>Windows, from a clean clone — the whole thing</b></summary>

Needed first: Windows 10/11, Python 3.11+ (`tomllib` starts there), git, and
the Claude Code CLI **logged in** — without it collection still works but no
report is written. No administrator rights at any point.

**1.** Clone and run the installer.

```bash
powershell -ExecutionPolicy Bypass -File install.ps1
```

It stops after five of its nine steps, because the next one needs a token only
you can issue. Before stopping it asks three things: the report language, the
email addresses you commit under, and the root to search for repositories —
counting the repositories under each drive so the answer is measured rather
than guessed. `~` is usually the wrong answer on Windows.

**2.** Create the Notion connection — [docs/notion-setup.md](docs/notion-setup.md).
Two details cause most failures: it must be an **internal connection's
installation token**, not a personal access token (those expire, and the job
then dies silently), and the parent page must be shared with that connection
(`•••` → Add connections).

**3.** Give it the token. Either fill `DAILY_REPORT_NOTION_TOKEN` and
`DAILY_REPORT_PARENT_PAGE_URL` in `.env` by hand — leaving
`DAILY_REPORT_DATABASE_ID` empty, the installer fills it — or use the wizard:

```bash
python setup_gui.py
```

The wizard exists mostly for this field. A terminal keeps what you type; a GUI
field does not, and this tool exists because session logs keep everything.

**4.** Run the installer again. It creates the database, writes its ID back,
registers the 04:05 task, adds the Start Menu shortcut, links the skill, and
finishes with `doctor.py`. Re-running is always safe.

**5.** Confirm.

```bash
python doctor.py
```

The first scheduled run is the next 04:05. Installing does not reconstruct the
fortnight before it existed — only the most recent closed day is generated.

</details>

It runs in two passes, because the middle step needs a token only you can issue.

**Pass 1** checks the platform, finds a Python ≥ 3.11, creates `config.toml`
from the example (asking for your report language and the email addresses you
commit under), creates `.env` with mode 600, and then stops and tells you what
to fill in.

**Fill in `.env`** — follow [docs/notion-setup.md](docs/notion-setup.md) to
create an internal connection and share a parent page with it. Keep the token in
that file only; never paste it into a terminal you have logs for, since this
tool exists because session logs keep everything you type.

**Pass 2** creates the Notion database (the schema is built by code, so property
names always match what the writer queries), writes its ID back into `.env`,
generates and registers the scheduled job, links the skill into
`~/.claude/skills/`, and finishes by running `doctor.py`.

The Windows installer does one thing more: it asks the machine where its shell
folders actually are. A localized Windows has **both** `Documents` and its
translated name, and OneDrive's Known Folder Move — on by default for many
setups — relocates them inside OneDrive. Miss that and editing a PowerShell
profile is reported as work on a project named after the documents folder.

Re-running it is safe: it never overwrites `config.toml`, `.env`, or an existing
database.

Then confirm headless summarisation works and try one day by hand:

```bash
python3 summarize.py x y --preflight
python3 run_day.py 2026-08-04
```

`git.authors` is the one setting with no usable default. Empty means no commits
are collected at all; removing the filter means every vendored fork in your home
directory pours its upstream history into your report.

```bash
git log --format='%ae' | sort | uniq -c | sort -rn | head
```

> **macOS — do not remove `USER` from the plist's `EnvironmentVariables`.** The
> Claude Code CLI looks up its credentials in the login keychain by account
> name, so without `USER` it reports "Not logged in" even when you are logged
> in. `launchd` passes almost no environment, and this failure never reproduces
> from a terminal — the shell supplies the variable there.

> **Windows — do not switch the task to "run whether user is logged on or
> not".** It is the same failure wearing different clothes. The CLI's
> credentials are protected against the logged-on user, and that setting hands
> the job a token that cannot decrypt them. The result is identical: "Not
> logged in" every night, and everything else looks healthy. `doctor.py` checks
> for it.

A first install does not reconstruct the fortnight before it existed: only the
most recent closed day is generated, and the earlier ones are recorded as
skipped.

## Running it

```bash
python3 run_day.py                 # every outstanding day (what launchd calls)
python3 run_day.py 2026-08-04      # one specific day
python3 doctor.py                  # is it working, and if not, why
```

Re-running a date overwrites its row rather than adding one. If a report reads
badly, run it again.

### Windows: opening it after installing

**Look for "하루 마감 보고서" in the Start Menu** — `install.ps1` puts it there.
It opens the status window: last run, next run, the last fourteen days as a
strip, and buttons for diagnostics, regenerating a day, the logs, and Notion.
It launches through `pythonw`, so no console trails behind it.

To start it by hand:

```bash
pythonw -X utf8 status_window.py
```

`pythonw`, not `python` — the latter leaves an empty console window behind the
GUI.

Both windows are plain tkinter, no dependency, because "there is nothing to
install" is worth more than a nicer toolkit.

The setup wizard gets no shortcut; it is a one-time thing, and if you need it
again you run it:

```bash
python setup_gui.py
```

The setup wizard. It collects the answers and then runs `install.ps1` — it does
not reimplement it, because the task registration and the permission narrowing
were verified once and two implementations of a risky step drift. The one thing
it does itself is the token field, and that is the point: the README tells you
not to paste the token into a terminal because this tool exists on the premise
that session logs keep everything you type. A GUI field is not logged.

**Nothing is required to run unattended.** The job is a scheduled task; these
are for the two moments a person is actually present — first setup, and "why
did nothing happen last night".

## Configuration

Everything lives in `config.toml`; the code is not meant to be edited to
configure it. `config.example.toml` (macOS) and `config.windows.example.toml`
(Windows) document every key. There are two because the defaults are not
portable: where a machine keeps caches, cloud folders and temp directories is a
fact about the operating system, and a Mac's list applied on Windows excludes
nothing that exists there. The keys that matter most:

| Key | Purpose |
|---|---|
| `git.authors` | Which commits are yours. Nothing else is counted. |
| `exclude.paths` | Never collected at all — client work, confidential directories. |
| `projects.containers` | Directories that *hold* projects rather than being one. |
| `labels.rename` | Display names for folders whose real name reads badly. |
| `report.language` | `ko` or `en` — selects `prompts/<language>.md`. |
| `day.boundary_hour` | Where one day ends. |

## When something goes wrong

`python3 doctor.py --full` runs seven checks in causal order — scheduler
registration, configuration, run history, stale locks, Notion reachability,
disk usage, headless authentication — and prints what it looked at rather than
just a verdict. The first failure is usually the cause.

## Privacy

Session logs contain credentials. Scanning a month of real prompts and shell
commands here turned up live API tokens in plain text; anyone who has pasted a
token into a command has one too.

- Raw prompts and commands **never leave the machine**. Only the model's prose
  is published.
- Sanitisation runs twice: on the digest before the model sees it, and on the
  finished report before it is written to Notion.
- `exclude.paths` blocks collection entirely, which is stronger than redaction.
  Confidential directories belong there, not in a regex.
- `work/`, `state/`, `logs/` and `.env` are gitignored. `work/` holds verbatim
  prompts.
- `scripts/check_no_pii.py` scans the repository for identifiers before
  publishing, and redacts its own findings so the report is not itself a leak.

## Platform support

**macOS and Windows 10/11** are implemented and verified. Linux raises a clear
"not supported" error at startup rather than failing silently at 4 a.m.

`platform_support.py` isolates everything platform-specific behind one class.

| | macOS | Windows |
|---|---|---|
| Scheduler | launchd (`.plist`) | Task Scheduler (`.xml`) |
| Missed days | coalesced into one run | `StartWhenAvailable` |
| Single instance | `fcntl.flock` | `msvcrt.locking` |
| Watchdog | `signal.alarm` (running time) | thread timer (wall clock) |
| Staying awake | `caffeinate` wrapper | `SetThreadExecutionState` |
| Notification | `osascript` | toast, balloon on failure |
| Permissions | `chmod 700` | `icacls` with inheritance |
| Credentials | login keychain (needs `USER`) | DPAPI (needs an interactive token) |

> **"The collection and processing core is portable" was not true.** Adding
> Windows turned up seven defects outside `platform_support.py`, two of which
> made the job exit cleanly every night while writing a report that said
> nothing had happened. See the
> [development note](docs/development-notes/windows_port_development_note.md).

Adding a platform means implementing that one class, fixing whatever the core
turns out to assume, and **verifying a scheduled run actually fires**.

## Tests

```bash
python3 -m pytest tests/ -q
```

Every test corresponds to a defect that actually occurred. They build synthetic
fixtures — a temporary home, a real git repository, hand-written transcripts —
so they pass on a machine that has never run this tool.

## Files

| File | Role |
|---|---|
| `run_day.py` | pipeline, backfill, lock, watchdog |
| `collect.py` | Claude Code transcripts and git commits |
| `collect_codex.py` | Codex rollouts |
| `collect_fs.py` | files changed on disk |
| `refine.py` | project rollup, noise removal, merge |
| `project_roots.py` | working directory → project |
| `sanitize.py` | credential masking |
| `summarize.py` | headless summarisation |
| `notion_upsert.py` | date-keyed upsert |
| `notion_schema.py` | property names, shared by the creator and the writer |
| `setup_notion_db.py` | database creation (once) |
| `doctor.py` | diagnostics |
| `platform_support.py` | everything OS-specific |
| `config.py` | configuration loading, logical dates |
| `paths.py` | resources that ship vs. data that is written |
| `status_window.py` | status and diagnostics window (tkinter, stdlib only) |
| `setup_gui.py` | setup wizard — collects answers, hands them to `install.ps1` |
| `cli.py` · `gui.py` · `daily-report.spec` | packaged-build entry points and PyInstaller spec |
| `install.sh` | one-time install on macOS, safe to re-run |
| `install.ps1` | one-time install on Windows, safe to re-run |
| `config.example.toml` | macOS defaults |
| `config.windows.example.toml` | Windows defaults |
| `templates/launchagent.plist.template` | launchd job definition |
| `templates/schtasks.xml.template` | Task Scheduler job definition |

## Known limitations

- **Everything except the report speaks Korean.** `report.language` (`ko` or
  `en`) selects the language of the report itself, and the documentation is
  bilingual. Everything else — both installers, `doctor.py`, the runtime
  messages, and the two windows (`status_window.py`, `setup_gui.py`) — is
  Korean only: 348 user-facing strings at last count. Configuration and machine
  detection are locale-independent; the text is not.
- **Linux is not implemented** — see [Platform support](#platform-support).
- **The Windows watchdog counts wall-clock time.** `signal.alarm` counts only
  time the process was actually running, so on macOS a run that merely slept is
  never aborted. Windows has no equivalent; the run holds a no-sleep assertion
  for its whole duration instead, and a forced sleep does abort the day — which
  the next run backfills.
- **The Windows task only runs while someone is logged on.** A locked screen
  counts. A signed-out or powered-off machine does not, and those days are
  recovered by `StartWhenAvailable` and the ledger.
- **Work done outside the two agents is only observed, not attributed.** A file
  that changed on disk is reported as output without a claim about who made it.

## Documentation

- [docs/design.md](docs/design.md) — why it is built this way, with the
  measurements behind each decision
- [docs/notion-setup.md](docs/notion-setup.md) — creating the connection and
  issuing a token
- [skills/daily-report/SKILL.md](skills/daily-report/SKILL.md) — the Claude Code
  skill for operating it conversationally

## License

MIT
