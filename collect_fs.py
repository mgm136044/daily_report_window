"""Find files that changed on disk but left no trace in either tool's log.

Both agents only record files they edited through their own file tools. When
an agent instead runs a script — `python3 build.py`, a heredoc that writes
forty HTML files, a renderer that emits CSV and SVG — the log holds one shell
command and nothing about what it produced.

Measured on 2026-08-04: the tool logs knew 40 files while 143 had actually
changed. After removing git-induced touches, 72 were real work, including all
42 blog posts written that day. The report said that project had 4 files.

Provenance here is weaker than a tool record — a file's mtime says it changed,
not who changed it or why — so these are kept in their own field and the
summarizer is told to treat them as supporting evidence rather than as the
account of what happened.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict

import config

GIT_TIMEOUT_SEC = 15
MAX_FILES_PER_PROJECT = 80

# Names and suffixes that change for reasons that are not work.
NOISE_NAMES = {".DS_Store", "package-lock.json", "uv.lock", "poetry.lock",
               "Thumbs.db", ".localized",
               # credential files: only the path would travel, but a report has
               # no reason to point at where the secrets live
               ".env", ".envrc", ".netrc", ".npmrc", "credentials", "id_rsa", "id_ed25519"}
NOISE_SUFFIXES = (".pyc", ".pyo", ".log", ".lock", ".tmp", ".swp", ".swo",
                  ".pack", ".idx", ".part", ".crdownload")
NOISE_DIR_PARTS = ("/.git/", "/node_modules/", "/__pycache__/", "/.venv/",
                   "/.pytest_cache/", "/.ruff_cache/", "/.obsidian/",
                   "/dist/", "/build/", "/.next/", "/.cache/",
                   # tool config trees churn constantly (plugin caches, state)
                   # and none of it is the day's work
                   "/.claude/", "/.codex/", "/.agents/")


# This tool rewrites work/, state/ and logs/ on every run. Left in, the report
# would list its own digests and ledger as the day's output. Derived from this
# file's location rather than matched by name, so a project of the user's that
# happens to have a `work/` directory is unaffected.
import paths

# Follows the *data* root, because that is where these directories actually
# are. Frozen, the executable and its artifacts live in different places, and
# deriving this from the code's location would stop excluding them.
_SELF_ROOT = paths.data_root()
SELF_ARTIFACT_DIRS = tuple(
    os.path.join(_SELF_ROOT, name) + os.sep for name in ("work", "state", "logs")
)


_SELF_ARTIFACT_KEYS = tuple(config.key(p) for p in SELF_ARTIFACT_DIRS)


def _is_noise(path: str) -> bool:
    if config.key(path).startswith(_SELF_ARTIFACT_KEYS):
        return True
    name = os.path.basename(path)
    if name in NOISE_NAMES or name.endswith(NOISE_SUFFIXES) or name.startswith(".env."):
        return True
    # NOISE_DIR_PARTS are written with forward slashes; a Windows path has to be
    # normalized before they can match anything at all.
    return any(part in config.probe(path) for part in NOISE_DIR_PARTS)


def _repo_root(path: str) -> str | None:
    current = os.path.dirname(path)
    # `current != "/"` never becomes false on Windows, where the top of the tree
    # is a drive root; the parent==current check below is what actually ends it.
    while current and os.path.dirname(current) != current:
        # a submodule or worktree has .git as a *file*, not a directory
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _git_path(repo: str, entry: str) -> str:
    """Absolute, native-separator path for something git printed.

    git always answers with forward slashes. Joining that onto a Windows repo
    path yields `D:\\repo\\docs/a.md`, which never equals the `D:\\repo\\docs\\a.md`
    that os.walk produced — so a tracked, clean file failed its lookup and was
    reported as work.
    """
    return config.nfc(os.path.normpath(os.path.join(repo, entry)))


def _git_state(repo: str) -> tuple[set[str], set[str]]:
    """Return (tracked, dirty) absolute paths for a repo.

    Only a file that is *tracked and clean* can be assumed to have been moved
    by `git pull` or `git checkout` rather than by working on it. An untracked
    or gitignored file is outside git's control entirely, so its mtime means
    what it says.

    Getting this wrong is not subtle: an earlier version checked only
    `git status --porcelain`, which does not list ignored files. A day's 42
    blog posts lived in a gitignored directory and were silently discarded as
    "clean".
    """
    def run(args):
        try:
            # Decoded explicitly: `text=True` uses the process locale, which on
            # a Korean Windows install is cp949. A single Korean filename then
            # raises UnicodeDecodeError inside subprocess's reader thread, and
            # the repository silently contributes nothing.
            result = subprocess.run(["git", "-C", repo, "-c", "core.quotepath=false", *args],
                                    capture_output=True, timeout=GIT_TIMEOUT_SEC)
        except (subprocess.SubprocessError, OSError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace")

    listing = run(["ls-files", "-z"])
    tracked = set()
    if listing:
        tracked = {_git_path(repo, e) for e in listing.split("\0") if e}

    status = run(["status", "--porcelain", "-z"])
    dirty = set()
    if status:
        # -z emits "XY path\0" and, for a rename, a bare "oldpath\0" right
        # after. Slicing [3:] off that second field produced a truncated
        # phantom path ("ginal_name.md").
        fields = [e for e in status.split("\0") if e]
        index = 0
        while index < len(fields):
            entry = fields[index]
            if len(entry) > 3:
                dirty.add(_git_path(repo, entry[3:]))
                if entry[0] in "RC" and index + 1 < len(fields):
                    dirty.add(_git_path(repo, fields[index + 1]))
                    index += 1
            index += 1
    return tracked, dirty


def collect(date_str: str, roots: list[str], committed: set[str] | None = None) -> dict:
    """Files changed inside the day window under each given project root."""
    start, end = config.day_window(date_str)
    committed = committed or set()
    found: dict[str, list[str]] = defaultdict(list)
    git_cache: dict[str, tuple[set[str], set[str]]] = {}
    scanned_roots = 0

    for root in roots:
        if not root or not os.path.isdir(root) or config.is_walk_excluded(root):
            continue
        scanned_roots += 1
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            if config.is_walk_excluded(dirpath) or _is_noise(dirpath + "/"):
                dirnames[:] = []
                continue
            for name in filenames:
                path = config.nfc(os.path.join(dirpath, name))
                if _is_noise(path):
                    continue
                try:
                    modified = os.path.getmtime(path)
                except OSError:
                    continue
                if not (start.timestamp() <= modified < end.timestamp()):
                    continue

                repo = _repo_root(path)
                if repo:
                    if repo not in git_cache:
                        git_cache[repo] = _git_state(repo)
                    tracked, dirty = git_cache[repo]
                    tracked_and_clean = path in tracked and path not in dirty
                    if tracked_and_clean and path not in committed:
                        continue
                found[root].append(path)

    trimmed = {}
    dropped = 0
    for root, paths in found.items():
        if len(paths) > MAX_FILES_PER_PROJECT:
            # keep the most recent, not the alphabetically first — cutting
            # after sort() dropped `zz_FINAL_REPORT.md` and kept 80 files of
            # `aaa_boilerplate_*`
            paths.sort(key=lambda p: -os.path.getmtime(p) if os.path.exists(p) else 0)
            dropped += len(paths) - MAX_FILES_PER_PROJECT
            paths = paths[:MAX_FILES_PER_PROJECT]
        trimmed[root] = sorted(paths)

    return {
        "roots": trimmed,
        "stats": {"roots_scanned": scanned_roots,
                  "files": sum(len(v) for v in trimmed.values()),
                  "files_dropped_over_cap": dropped},
    }


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: collect_fs.py <YYYY-MM-DD> <root> [root...]", file=sys.stderr)
        return 2
    data = collect(sys.argv[1], sys.argv[2:])
    print(json.dumps(data, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
