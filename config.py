"""Load config.toml once and expose it as plain data.

Kept separate from the modules that use it so paths and policy can change
without touching logic.
"""

from __future__ import annotations

import os
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
