# Design notes

[한국어](design.ko.md) · [README](../README.md)

Why this tool is built the way it is. Everything below was measured, not
assumed; where a number appears, it came from running the thing.

## The shape of the problem

Coding agents already keep a detailed record of what they did. Claude Code
writes a JSONL transcript per session; Codex writes one per thread. Both
capture the prompt, every tool call, and every file touched. The record exists
— it is simply unreadable, spread across hundreds of files, and mixed with
megabytes of tool output nobody wants to see again.

So this is not a tracking problem. It is a compression and attribution
problem.

## Volume: the raw day does not fit, the useful day does

A single day touches 300–470 MB of transcripts. That number is why people
assume this needs chunking, embeddings, or map-reduce summarisation.

It does not. Most of that volume is `tool_result` payloads — file dumps,
search output, command stdout. Those say what the agent *saw*, not what it
*did*. Discard them, keep prompts, tool inputs and file-change records, and a
day compresses to **30–180 KB**, which is 8,000–45,000 tokens. That fits in a
single model call with room to spare.

Measured across four days:

| Raw candidate files | After extraction | Ratio |
|---|---|---|
| 471 MB | 68 KB | 7,100× |
| 374 MB | 211 KB | 1,810× |
| 331 MB | 128 KB | 2,655× |
| 314 MB | 66 KB | 4,859× |

Extraction takes under a second. Hierarchical summarisation is an
optimisation to reach for if a day ever stops fitting, not a starting design.

## Time: the file's timestamp is not the record's timestamp

A session transcript is appended to whenever that session resumes. One file
here spans **34 days**. Selecting files by modification time and treating
their contents as "today" pulls in a month of unrelated work.

Every record carries its own timestamp, so filtering happens per record. File
mtime is still useful, but only as a cheap pre-filter: a file untouched since
before the window cannot contain records inside it.

## Noise: three quarters of the shell is navigation

Of the shell commands captured on a real day, **74%** are `ls`, `cd`, `cat`,
`grep`, `find` and friends. They describe how the agent looked around, not
what it accomplished, and they dominate the byte count. Dropping them, plus
de-duplicating near-identical commands, halves the digest without losing
anything a reader would want.

## Attribution: a working directory is not a project

The transcript records `cwd` per record, which is often several levels below
the project — `…/v4/docs/04_reports`, `…/assets/photos`. Naming a day's work
after those produces a report nobody can read.

Each `cwd` is walked upward to the nearest directory carrying a project marker
(`.git`, `pyproject.toml`, `package.json`, `CONTEXT.md`, …), stopping at
configured container directories. Two rules earned themselves the hard way:

- **A container that also carries a marker is still a project.** An earlier
  version discarded every session that ran in one.
- **A container is never itself the answer** — and that has to be checked
  before the "a child of a container is a project" rule, or `~/Downloads`
  returns itself simply because its parent is a container.

Korean and other non-ASCII folder names need one more step. macOS returns
path components from the filesystem decomposed (NFD, jamo by jamo) while the
session records carry them composed (NFC). The two look identical on screen
and are different strings, so the same folder splits into two projects that
never merge. Everything is normalised to NFC at ingestion.

## Coverage: agents only log what they touched themselves

Both agents record files they edited through their own file tools. When an
agent instead runs a script — a build, a renderer, a heredoc that writes forty
files — the log holds one shell command and nothing about what it produced.

Measured on one day: the tool logs knew 40 files while 143 had actually
changed in the window. After removing files that git had merely touched
(`pull`, `checkout`), **72 were real work**, including every document produced
that day by one project. The report had described that project as having four
files.

So a third source exists: a filesystem sweep over the projects the day already
touched, in its own field. Its provenance is weaker — an mtime says a file
changed, not who changed it or why — so the summarizer is told to report these
as output without claiming how they were made.

Distinguishing real work from git noise needs care. `git status --porcelain`
does not list ignored files, so "absent from status" cannot mean "unchanged".
The test is *tracked and clean*: an untracked or gitignored file is outside
git's control and its mtime means what it says.

## Codex: `exec` takes JavaScript, not a command

Codex's `exec` tool does not receive a shell command. It receives JavaScript
that calls `tools.exec_command({cmd: "…"})`, often several times in one block
with loops and bookkeeping around it. Stored whole, shell invocations became
80% of collected bytes, with single entries reaching 35 KB, while the part
that says what actually ran was buried inside.

Only the `cmd:` values are extracted. All three JS string forms have to be
handled — supporting just `"` and `'` silently discarded 3.7% of command
sites, and the losses clustered on backtick-quoted sub-delegations, which are
the most significant commands of the day.

Plans arrive the same way. `tools.update_plan({plan:[{step:…}]})` is called
from inside `exec`, *and* Codex separately emits a `function_call` named
`update_plan` with plain JSON arguments. Handling only the first form lost
every instance of the second.

A rollout can also be a subagent thread rather than a person's session, and
its "user messages" are the orchestrator's instructions. One further detail
decides whether that filter works at all: a subagent rollout carries a
**second `session_meta` at the end** — the parent's, whose source is `cli`.
Letting the last one win flips the thread back to "not a subagent". On this
machine that affected 90 of 136 multi-meta files, and on one day 96% of
collected prompts were machine chatter as a result.

## Credentials: session logs contain them

Scanning a month of prompts and shell commands turned up live API tokens
sitting in plain text. This is not hypothetical and not unusual — anyone who
has ever pasted a token into a command has one in their logs.

Sanitisation therefore runs twice: on the digest before the model sees it, and
on the finished report before it is published. The architecture carries the
real defence, though — only the model's prose reaches the destination, never
the raw prompts and commands.

Two details matter in the patterns. `\b(API[_-]?KEY|…)` matches `API_KEY=` but
not `OPENAI_API_KEY=`, because `_` is a word character and there is no
boundary before the keyword; real `.env` files are almost entirely prefixed
names. And dictionary keys carry data too — project names become destination
properties, so a redactor that only walks values misses them entirely.

## Scheduling: starting on time and finishing are different problems

`launchd` **coalesces** missed calendar runs into exactly one execution. A
machine that was off for three days comes back and fires once, so without a
ledger of which days were written, the other two are gone permanently. The job
keeps its own record and backfills.

Starting is not enough. On a Mac running Power Nap, the system cycles between
DarkWake and Maintenance Sleep every few tens of seconds. A run can begin
exactly on schedule and then freeze mid-collection; one began at 04:05 and
finished at 13:52, almost all of it suspended. The job is wrapped in
`caffeinate`, which holds a power assertion for exactly as long as the command
runs and releases it after — a few minutes a day, not a permanent change to
the machine's settings.

The watchdog does not fire in that situation, because `signal.alarm` counts
time the process was actually running. That is the right behaviour here:
aborting a run that merely slept would lose the day's report for no reason.

## Headless authentication: `USER` is required

The Claude Code CLI looks up its credentials in the login keychain by account
name. Without `USER` in the environment it reports "Not logged in" even when
the user is logged in.

`launchd` passes almost no environment — on this machine, exactly one
variable. So a LaunchAgent hits this every single night unless the plist
declares `USER` explicitly. The failure never reproduces from a terminal,
where the shell supplies it.

## Destination: date-keyed upsert with no primitive for it

Notion has no upsert and no unique constraint, so the flow is
query-then-create-or-update against a date property. Three consequences:

- **Two or more hits means something already went wrong.** Silently picking
  one compounds the damage; the job stops and says so.
- **Page creation must not be retried.** There is no idempotency key, so a
  create that succeeded server-side but returned 502 would be retried into a
  second row — after which that date can never be written again. On failure
  the job re-queries before deciding.
- **The body must be written atomically.** Erase-then-append that dies midway
  leaves a truncated page, and the next night sees "a page exists" and
  preserves the damage.

## Reporting: what stops the summary from lying

The report is a permanent record, so an invented detail becomes a false
memory of one's own work. An audit of three generated reports found no
invented file paths and no fabricated projects, but did find overstatement:
outcomes claimed from evidence of activity, counts that matched nothing,
timezone conversions applied inconsistently.

Two of those are fixed in data rather than by instruction, which is more
reliable:

- Timestamps are converted to local time before the model sees them, so it
  never has to convert and cannot do it inconsistently.
- Truncated strings carry a visible marker, and the truncation limits were
  raised until the cases that mattered fit. Telling a model not to complete a
  cut-off phrase is weaker than not cutting it off.

The rest are prompt rules: count only what is actually in the array, never
state how an artifact was produced unless a command shows it, treat a modified
file as work done and not as work finished, and only write a "next step" when
something in the log points to one.

## What is deliberately not here

- **Anything that requires the machine to be awake at a fixed instant.** The
  job runs when it can and backfills what it missed.
- **Linux.** `platform_support.py` is where a new platform goes; `systemd
  --user` timers would be the natural fit. Shipping an unverified
  implementation would mean handing someone a job that quietly does not run.
- **Work done outside the two agents.** Files changed on disk are observed,
  but a report of work is only ever as complete as its sources.

## What porting to Windows actually cost

This document used to claim the collection and processing core was portable and
that only the runtime shell was not. Running the test suite on a real Windows
machine disproved it: seven defects sat outside `platform_support.py`, and the
two worst were invisible.

`project_root()` rejected any working directory that did not begin with `/`,
and the walk upward terminated on the literal string `"/"`, which a path rooted
at `D:\` never reaches. Between them, every session on the machine was
discarded — and nothing failed. The day was collected, refined, sanitized,
summarized and uploaded, saying that nothing had happened. It is the same class
of failure as the scheduler that does not fire, which is what
`platform_support.py` exists to prevent, arriving through a door nobody was
watching.

The other five: exclusion lists written with forward slashes matched no
backslash path, so every exclusion silently switched off — including the one
that stops the job reporting on its own summarization. `subprocess(text=True)`
decodes with the process locale, which is cp949 on a Korean install, so one
Korean commit subject raised UnicodeDecodeError inside a reader thread and lost
that repository. git's forward-slash output joined onto a Windows repo path
produced `D:\repo\docs/a.md`, which never matched what `os.walk` returned.
`os.path.relpath` raises across drives rather than returning something, and the
tool commonly sits on `D:` while the home directory is on `C:` — that one
crashed *after* the model call had been paid for. And stdout, redirected by the
scheduler into a file opened in the ANSI codepage, could not encode the Korean
the job prints.

The lesson is not that the core was badly written. It is that "portable" is a
claim about verification, not about intent, and the only way to make it is to
run the thing somewhere else.
