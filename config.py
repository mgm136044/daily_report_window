"""Load config.toml once and expose it as plain data.

Kept separate from the modules that use it so paths and policy can change
without touching logic.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import paths

# The settings file is written by the installer and edited by the user, so it
# follows the data root; the examples ship with the tool and follow the
# resource root. From a checkout these are the same directory.
HERE = paths.resource_root()
CONFIG_PATH = os.environ.get("DAILY_REPORT_CONFIG") or paths.data("config.toml")


def example_for(platform_name: str) -> str:
    """The starting configuration for a platform.

    The defaults are not portable and cannot be made so: `exclude.paths` and
    `projects.containers` describe where a *particular* operating system keeps
    caches, cloud folders and temp directories. A Mac's list applied on Windows
    excludes nothing that exists there, which is worse than having no list.
    """
    name = "config.windows.example.toml" if platform_name == "nt" else "config.example.toml"
    path = os.path.join(HERE, name)
    return path if os.path.exists(path) else os.path.join(HERE, "config.example.toml")


EXAMPLE_PATH = example_for(os.name)


@lru_cache(maxsize=1)
def source_path() -> str:
    """Which file the settings came from.

    A fresh clone has no config.toml, and refusing to start would mean the test
    suite cannot run before installation. Falling back to the example keeps that
    working — but silently running on example defaults would collect no commits
    at all, so it says so, and doctor.py treats it as a problem.
    """
    if os.path.exists(CONFIG_PATH):
        return CONFIG_PATH
    copy = ("Copy-Item" if os.name == "nt" else "cp")
    print(f"config.toml 이 없어 예시 설정으로 동작합니다: {EXAMPLE_PATH}\n"
          f"  {copy} {os.path.basename(EXAMPLE_PATH)} config.toml 후 "
          f"[git] authors 를 채우세요", file=sys.stderr)
    return EXAMPLE_PATH


def using_example() -> bool:
    return source_path() == EXAMPLE_PATH


@lru_cache(maxsize=1)
def load() -> dict:
    with open(source_path(), "rb") as handle:
        return tomllib.load(handle)


# --- carrying new settings into an existing config ---------------------------
#
# The installer leaves an existing config.toml alone, which is right: it
# describes somebody's machine and their choices, and an upgrade has no
# business overwriting either. The cost is that a setting added in a new
# version never reaches anyone who already installed — `[summary] engine`
# shipped in 0.2.0 and `[run] schedule_time` in 0.2.2, and an install from
# before then has neither, so both features are invisible to exactly the people
# who have been using the tool longest.
#
# So the missing keys are carried over instead: added, with the comment that
# explains them, and never replacing anything already there. Every default
# equals the behaviour that install already had, so nothing changes by being
# written down — it only becomes visible and editable.

def _bracket_delta(line: str) -> int:
    """Net bracket depth of a line, ignoring brackets inside strings."""
    depth = quote = 0
    previous = ""
    for char in line:
        if quote:
            if char == quote and previous != "\\":
                quote = 0
        elif char in "\"'":
            quote = char
        elif char == "#":
            break
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        previous = char
    return depth


def _entries(text: str) -> list[tuple[str, str, list[str]]]:
    """Every key the file declares, as (section, key, lines to reproduce it).

    The lines include the comment directly above the key. A setting arriving
    without the sentence that explains it is a setting nobody edits on purpose,
    and these files are meant to be read.

    Array-of-tables sections are skipped. `[[sources.extra_session_globs]]` is
    a list of entries rather than a set of settings, so "the same key in the
    same section" does not identify anything and merging into it is meaningless.
    """
    entries: list[tuple[str, str, list[str]]] = []
    lines = text.splitlines()
    section, pending, index = "", [], 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            pending.append(line)
            index += 1
            continue
        header = re.match(r"\[(\[?)([^\]]+)\]\]?\s*$", stripped)
        if header:
            section = "" if header.group(1) else header.group(2)
            pending = []
            index += 1
            continue
        name = re.match(r"([A-Za-z0-9_\-]+|\"[^\"]+\")\s*=", stripped)
        if not name or not section:
            pending = []
            index += 1
            continue
        comment: list[str] = []
        for previous in reversed(pending):
            if previous.strip().startswith("#"):
                comment.insert(0, previous)
            else:
                break
        pending = []
        start, depth = index, _bracket_delta(line)
        while depth > 0 and index + 1 < len(lines):
            index += 1
            depth += _bracket_delta(lines[index])
        entries.append((section, name.group(1).strip('"'),
                        comment + lines[start:index + 1]))
        index += 1
    return entries


def _insert(lines: list[str], section: str, block: list[str]) -> list[str]:
    """Put `block` at the end of `section`, creating the section if needed."""
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"[{section}]":
            start = index
            break
    if start is None:
        return lines + ([""] if lines and lines[-1].strip() else []) \
            + [f"[{section}]"] + block
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.match(r"\[\[?[^\]]+\]\]?\s*$", lines[index].strip()):
            end = index
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return lines[:end] + [""] + block + lines[end:]


def merge_missing_keys(config_text: str, example_text: str) -> tuple[str, list[str]]:
    """Add the settings the example declares and this config does not.

    Insertion only. Existing values, comments, ordering and whitespace come
    through untouched — an upgrade that rewrote what somebody had chosen would
    be worse than one that added nothing.
    """
    present = {(s, k) for s, k, _ in _entries(config_text)}
    lines, added = config_text.splitlines(), []
    for section, key, block in _entries(example_text):
        if (section, key) in present:
            continue
        lines = _insert(lines, section, block)
        present.add((section, key))
        added.append(f"{section}.{key}")
    text = "\n".join(lines)
    if config_text.endswith("\n") or not config_text:
        text += "\n"
    return text, added


def missing_keys(config_path: str = "", example_path: str = "") -> list[str]:
    """Which settings this install has never been offered. Read-only."""
    config_path = config_path or CONFIG_PATH
    example_path = example_path or EXAMPLE_PATH
    if not os.path.exists(config_path):
        return []
    # A fresh clone runs on the example itself, which is never behind itself.
    if os.path.abspath(config_path) == os.path.abspath(example_path):
        return []
    with open(config_path, encoding="utf-8") as handle:
        current = handle.read()
    with open(example_path, encoding="utf-8") as handle:
        example = handle.read()
    return merge_missing_keys(current, example)[1]


def upgrade_file(config_path: str = "", example_path: str = "") -> tuple[list[str], str]:
    """Write the missing settings into config.toml. Returns (added, message).

    Fails closed. The merged text has to parse as TOML before it replaces
    anything, and the original is kept beside it as `.bak` — this is the one
    file the tool cannot regenerate, since the Notion database id lives next to
    it and the exclusion lists are hand-tuned against a particular machine.
    """
    config_path = config_path or CONFIG_PATH
    example_path = example_path or EXAMPLE_PATH
    if not os.path.exists(config_path):
        return [], f"config.toml 이 없습니다: {config_path}"
    if os.path.abspath(config_path) == os.path.abspath(example_path):
        return [], "예시 설정으로 동작 중이라 옮길 것이 없습니다"

    with open(config_path, encoding="utf-8") as handle:
        current = handle.read()
    with open(example_path, encoding="utf-8") as handle:
        example = handle.read()
    merged, added = merge_missing_keys(current, example)
    if not added:
        return [], "새로 추가할 설정이 없습니다"

    try:
        tomllib.loads(merged)
    except tomllib.TOMLDecodeError as error:
        return [], (f"병합 결과가 TOML 로 읽히지 않아 중단했습니다: {error}\n"
                    f"  설정은 그대로 두었습니다: {config_path}")

    backup = config_path + ".bak"
    with open(backup, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(current)
    with open(config_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(merged)
    # Imported here rather than at module level: `config` is the one module
    # everything else imports first, and it has no other reason to pull the
    # platform layer in behind it.
    import platform_support  # noqa: PLC0415
    for path in (config_path, backup):
        platform_support.PLATFORM.restrict(path, is_dir=False)
    return added, f"설정 {len(added)}개를 추가했습니다 (원본: {backup})"


def expand(path: str) -> str:
    """`~` and environment variables, both.

    Variables matter more on Windows than the `~` does. `%APPDATA%` is not
    reliably `~/AppData/Roaming` — it is redirected on domain-joined machines
    and by roaming profiles — and `%OneDrive%` is the only dependable way to
    find a folder that may be called `OneDrive`, `OneDrive - Contoso`, or a
    localized variant. Writing those paths out by hand works on the machine
    they were written on and quietly matches nothing elsewhere.
    """
    return os.path.expanduser(os.path.expandvars(path))


# Windows differs from macOS in two ways that matter to every path comparison
# in this file, and both fail silently rather than loudly.
WINDOWS = os.name == "nt"


def key(path: str) -> str:
    """A comparable form of a path, for set membership and prefix tests.

    Windows paths are case-insensitive, so `D:\\Dev\\app` and `d:\\dev\\app`
    are the same directory while being different strings. Comparing them raw
    made a configured container match nothing.
    """
    return os.path.normcase(path) if WINDOWS else path


def probe(path: str) -> str:
    """A path prepared for substring matching against the exclusion lists.

    The lists are written with forward slashes (`/node_modules/`) because that
    is what a path looks like everywhere except Windows. Matching them against
    a backslash path finds nothing at all — which does not look like a bug,
    it looks like a machine with nothing to exclude, and it silently disables
    every exclusion including the one that stops the tool reporting on itself.
    """
    if not path:
        return "/"
    text = path.replace(os.sep, "/") if WINDOWS else path
    if WINDOWS:
        text = text.lower()
    return text if text.endswith("/") else text + "/"


@lru_cache(maxsize=8)
def _fragments(raw: tuple[str, ...]) -> tuple[str, ...]:
    """Prepare the exclusion fragments for comparison against `probe()` output.

    A fragment may start with `~`, which anchors it to the home directory. That
    matters more than it sounds: an unanchored `/Templates/` is a substring of
    any path containing a directory called Templates — including this project's
    own `templates/` — and the tree then vanishes from the sweep with no signal
    at all. The Windows defaults need entries for the home directory's legacy
    junctions, and those names (`Templates`, `Documents`, `Links`, `Recent`)
    are far too ordinary to match unanchored.
    """
    expanded = tuple(
        (os.path.expanduser(f) if f.startswith("~") else f) for f in raw
    )
    if not WINDOWS:
        return expanded
    return tuple(f.replace("\\", "/").lower() for f in expanded)


def local_tz() -> timezone:
    """The reporting timezone.

    Defaults to whatever the machine is set to, so a fresh install is correct
    without editing anything. An explicit offset in config wins — useful when
    the reports should follow a fixed timezone regardless of where the laptop
    happens to be.
    """
    configured = load()["day"].get("timezone_offset_hours")
    if configured is not None:
        return timezone(timedelta(hours=configured))
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return timezone(offset)


def day_window(date_str: str) -> tuple[datetime, datetime]:
    """Return the [start, end) datetimes of the logical day.

    The boundary hour shifts the day so late-night work belongs to the day it
    felt like, not the calendar day the clock had already rolled into.
    """
    boundary = load()["day"]["boundary_hour"]
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=local_tz())
    start += timedelta(hours=boundary)
    return start, start + timedelta(days=1)


def logical_date(moment: datetime) -> str:
    """Which logical day does this instant belong to?"""
    boundary = load()["day"]["boundary_hour"]
    return (moment.astimezone(local_tz()) - timedelta(hours=boundary)).strftime("%Y-%m-%d")


def parse_iso(value):
    """Parse an ISO timestamp, tolerating the trailing Z form."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


_EXTENDED = "\\\\?\\"
_EXTENDED_UNC = "\\\\?\\UNC\\"


def nfc(path: str) -> str:
    """Put a path from a session log into the form the rest of the code compares.

    Two normalizations, one per platform, both of which produce a string that
    *looks* identical to the right answer and is not equal to it.

    **Unicode form.** macOS returns Korean path components decomposed (NFD,
    jamo-by-jamo) from the filesystem, while the session records carry them
    composed (NFC). The same folder would otherwise split into two projects
    that never merge.

    **Extended-length prefix.** Codex records working directories as
    `\\\\?\\D:\\work\\app`, not `D:\\work\\app`. Every container and never rule
    in `project_roots.py` is a string comparison against paths written without
    it, so none of them matched: the walk upward ran past the home directory —
    which is a git repository here, and therefore carries a root marker — and
    returned **the home directory itself as a project named after the Windows
    account**. Measured: `\\\\?\\C:\\Users\\<account>\\some-project` produced the
    label `<account>`, and so did every other extended path on the machine.
    Wrong attribution, and an account name published to Notion.
    """
    if not path:
        return path
    if path.startswith(_EXTENDED_UNC):
        path = "\\\\" + path[len(_EXTENDED_UNC):]
    elif path.startswith(_EXTENDED):
        path = path[len(_EXTENDED):]
    return unicodedata.normalize("NFC", path)


def is_excluded(path: str) -> bool:
    target = probe(path)
    return any(fragment in target
               for fragment in _fragments(tuple(load()["exclude"]["paths"])))


def is_walk_excluded(path: str) -> bool:
    """Extra exclusions that apply only to filesystem traversal.

    Under launchd a background process cannot answer a TCC prompt, so opening
    a protected or cloud-backed directory can block forever instead of
    failing. This never reproduces from a terminal, where the permission is
    already inherited.
    """
    if is_excluded(path):
        return True
    target = probe(path)
    return any(f in target
               for f in _fragments(tuple(load()["sources"].get("walk_exclude", []))))


def display_label(name: str) -> str:
    """Human-facing name for a project folder.

    Folder names are not always readable — `.claude` is real work on the
    tooling, but it reads like noise in a report.
    """
    return (load().get("labels", {}).get("rename", {}) or {}).get(name, name)
