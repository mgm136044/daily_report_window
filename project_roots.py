"""Roll a working directory up to the project it belongs to.

The session log records `cwd` per record, which is often a deep subdirectory
(`.../v4/docs/04_보고서`). Naming a day's work after that leaf produces
meaningless labels, so each cwd is walked upward to the nearest directory that
looks like a project root and stops there.
"""

from __future__ import annotations

import os
import unicodedata

import config

HOME = os.path.expanduser("~")

# A directory is a project root if it holds one of these markers.
ROOT_MARKERS = (
    ".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "CONTEXT.md", "CLAUDE.md", ".venv",
)

# Which directories merely hold projects, and which are never projects, is
# policy rather than logic — it changes as the user's folders change — so both
# lists live in config.toml.
#
# A container that *also* carries a root marker is still a project: a folder
# sitting directly under a container can carry CONTEXT.md and be real work, and
# an earlier version silently discarded every session that ran in one.
def _dirs(name: str) -> set[str]:
    values = config.load().get("projects", {}).get(name, []) or []
    return {config.key(os.path.normpath(config.expand(v))) for v in values}


def container_dirs() -> set[str]:
    return _dirs("containers")


def never_project_dirs() -> set[str]:
    return _dirs("never") | {config.key(HOME), config.key("/")}


def is_excluded(path: str) -> bool:
    """Exclusion policy lives in config.toml so it can change without code."""
    return config.is_excluded(path)


def _is_anchor(path: str) -> bool:
    """True at the top of the tree — `/` on macOS, `D:\\` on Windows.

    The walk used to stop at the literal string "/", which on Windows is never
    reached: `os.path.dirname("D:\\")` is `"D:\\"`, so the loop ran to the
    drive root and fell out the bottom returning None. Combined with the
    absolute-path test below, every Windows session was dropped and the report
    read "활동 없음" every single day.
    """
    return os.path.dirname(path) == path


def project_root(cwd: str) -> str | None:
    """Return the project root for cwd, or None if it should be dropped."""
    if not cwd or not os.path.isabs(cwd) or is_excluded(cwd):
        return None

    containers, never = container_dirs(), never_project_dirs()
    current = config.nfc(os.path.normpath(cwd))
    while current and not _is_anchor(current):
        if config.key(current) in never:
            return None
        has_marker = any(os.path.exists(os.path.join(current, m)) for m in ROOT_MARKERS)
        if has_marker:
            return current
        # A container is never itself the answer. This has to be checked before
        # the parent rule below, or `~/Downloads` returns itself simply because
        # its parent (`~`) is a container.
        if config.key(current) in containers:
            return None
        parent = os.path.dirname(current)
        if parent == current:
            break
        # a direct child of a container is itself a project even without a marker
        if config.key(parent) in containers:
            return current
        current = parent
    return None


def project_label(cwd: str) -> str | None:
    """Human-facing project name for a cwd, or None to drop it."""
    root = project_root(cwd)
    if root is None or config.key(root) in never_project_dirs():
        return None
    return config.display_label(config.nfc(os.path.basename(root)))
