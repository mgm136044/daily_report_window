"""Collect one logical day's work signal from Claude Code session logs and git.

Two things this deliberately does NOT do:

  - It does not select files by mtime. A single session transcript can span
    weeks, so mtime is only a cheap pre-filter; every record is then checked
    against its own timestamp.
  - It does not keep tool_result payloads. Those are where the megabytes live
    (file dumps, search output) and they carry almost no signal about what the
    day was actually about.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

import collect_codex
import collect_fs
import config

GIT_TIMEOUT_SEC = 20

# A cut string invites the model to finish it from world knowledge. One report
# turned a prompt truncated at "**1. GM Safe" into "GM Safety Alert Seat" — a
# real product, but not something the log said.
TRUNCATION_MARK = " …[truncated]"
FILE_TOOLS = {"Write", "NotebookEdit"}
EDIT_TOOLS = {"Edit"}


def parse_ts(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _cut(text: str, limit: int) -> str:
    """Truncate, leaving a visible mark so nobody completes the missing part."""
    if len(text) <= limit:
        return text
    return text[:limit] + TRUNCATION_MARK


def _new_bucket() -> dict:
    return {
        "sessions": set(), "branches": set(), "skills": set(), "slugs": set(),
        "prompts": [], "writes": set(), "edits": set(), "tracked": set(),
        "bash": [], "todos": [], "first_ts": None, "last_ts": None,
    }


def collect_sessions(date_str: str) -> dict:
    """Walk every transcript, keeping only records inside the logical day."""
    cfg = config.load()
    start, end = config.day_window(date_str)
    root = config.expand(cfg["sources"]["claude_projects_dir"])
    prompt_cap = cfg["noise"]["prompt_max_chars"]

    buckets: dict[str, dict] = defaultdict(_new_bucket)
    files_seen = files_scanned = records_in_day = 0

    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        files_seen += 1
        try:
            # cheap pre-filter only — a file touched before the day started
            # cannot contain records inside it
            if os.path.getmtime(path) < start.timestamp():
                continue
        except OSError:
            # Claude Code prunes old transcripts on its own schedule, so a file
            # can disappear between the glob and the stat
            continue
        files_scanned += 1

        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue

                stamp = parse_ts(record.get("timestamp")
                                 or (record.get("snapshot") or {}).get("timestamp"))
                if stamp is None or not (start <= stamp.astimezone(config.local_tz()) < end):
                    continue
                records_in_day += 1

                # file-history-delta carries no cwd, but its backup dir is an
                # absolute path, so it can place itself
                if record.get("type") == "file-history-delta":
                    backup = record.get("backup") or {}
                    parent = config.nfc(backup.get("realParentDir") or "")
                    name = config.nfc(os.path.basename(record.get("trackingPath") or ""))
                    if parent and name and not config.is_excluded(parent):
                        buckets[parent]["tracked"].add(os.path.join(parent, name))
                    continue

                cwd = config.nfc(record.get("cwd") or "")
                if not cwd or config.is_excluded(cwd):
                    continue
                bucket = buckets[cwd]

                if bucket["first_ts"] is None or stamp < bucket["first_ts"]:
                    bucket["first_ts"] = stamp
                if bucket["last_ts"] is None or stamp > bucket["last_ts"]:
                    bucket["last_ts"] = stamp
                if record.get("sessionId"):
                    bucket["sessions"].add(record["sessionId"])
                if record.get("gitBranch"):
                    bucket["branches"].add(record["gitBranch"])
                if record.get("slug"):
                    bucket["slugs"].add(record["slug"])

                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                content = message.get("content")

                if role == "user" and isinstance(content, str):
                    bucket["prompts"].append(_cut(content, prompt_cap))
                    continue
                if not isinstance(content, list):
                    continue

                for block in content:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind == "text" and role == "user":
                        bucket["prompts"].append(_cut(block.get("text") or "", prompt_cap))
                    elif kind == "tool_use":
                        _absorb_tool_use(bucket, block)

    projects = {}
    for cwd, bucket in buckets.items():
        projects[cwd] = {
            "sessions": sorted(bucket["sessions"]),
            "branches": sorted(bucket["branches"]),
            "slugs": sorted(bucket["slugs"]),
            "skills_used": sorted(bucket["skills"]),
            "prompts": bucket["prompts"],
            "files_written": sorted(bucket["writes"]),
            "files_edited": sorted(bucket["edits"]),
            "files_tracked": sorted(bucket["tracked"]),
            "bash_commands": bucket["bash"],
            "todos": bucket["todos"],
            "first_ts": bucket["first_ts"].isoformat() if bucket["first_ts"] else None,
            "last_ts": bucket["last_ts"].isoformat() if bucket["last_ts"] else None,
        }

    return {
        "projects": projects,
        "stats": {
            "transcripts_total": files_seen,
            "transcripts_scanned": files_scanned,
            "records_in_day": records_in_day,
        },
    }


def _absorb_tool_use(bucket: dict, block: dict) -> None:
    name = block.get("name")
    payload = block.get("input") or {}
    if name in FILE_TOOLS and payload.get("file_path"):
        bucket["writes"].add(config.nfc(payload["file_path"]))
    elif name in EDIT_TOOLS and payload.get("file_path"):
        bucket["edits"].add(config.nfc(payload["file_path"]))
    elif name == "Bash" and payload.get("command"):
        bucket["bash"].append(payload["command"])
    elif name == "TodoWrite":
        for item in payload.get("todos") or []:
            if isinstance(item, dict) and item.get("content"):
                bucket["todos"].append(item["content"])
    elif name == "Skill" and payload.get("skill"):
        bucket["skills"].add(payload["skill"])


def collect_labeled_sessions(date_str: str) -> dict:
    """Sessions whose `cwd` carries no project meaning, grouped by a fixed label.

    Claude's local agent mode writes its transcripts under Application Support
    with a `cwd` pointing at a scratch folder, so the ordinary rollup would
    either drop them (the path is excluded) or invent a nonsense project name.
    The conversation itself is real work, so it is kept under a configured
    label instead.
    """
    cfg = config.load()
    entries = cfg["sources"].get("extra_session_globs") or []
    start, end = config.day_window(date_str)
    prompt_cap = cfg["noise"]["prompt_max_chars"]

    labeled: dict[str, dict] = {}
    for entry in entries:
        pattern = config.expand(entry.get("glob", ""))
        label = entry.get("label")
        if not pattern or not label:
            continue
        bucket = labeled.setdefault(label, _new_bucket())
        for path in glob.glob(pattern, recursive=True):
            try:
                if os.path.getmtime(path) < start.timestamp():
                    continue
                handle = open(path, encoding="utf-8", errors="replace")
            except OSError:
                continue
            with handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    stamp = parse_ts(record.get("timestamp"))
                    if stamp is None or not (start <= stamp.astimezone(config.local_tz()) < end):
                        continue
                    if bucket["first_ts"] is None or stamp < bucket["first_ts"]:
                        bucket["first_ts"] = stamp
                    if bucket["last_ts"] is None or stamp > bucket["last_ts"]:
                        bucket["last_ts"] = stamp
                    if record.get("sessionId"):
                        bucket["sessions"].add(record["sessionId"])
                    message = record.get("message")
                    if not isinstance(message, dict):
                        continue
                    content = message.get("content")
                    if message.get("role") == "user" and isinstance(content, str):
                        bucket["prompts"].append(_cut(content, prompt_cap))
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text" and message.get("role") == "user":
                                bucket["prompts"].append(_cut(block.get("text") or "", prompt_cap))
                            elif block.get("type") == "tool_use":
                                _absorb_tool_use(bucket, block)

    out = {}
    for label, bucket in labeled.items():
        if not (bucket["prompts"] or bucket["writes"] or bucket["edits"] or bucket["bash"]):
            continue
        out[label] = {
            "sessions": sorted(bucket["sessions"]), "branches": [], "slugs": [],
            "skills_used": sorted(bucket["skills"]), "prompts": bucket["prompts"],
            "files_written": sorted(bucket["writes"]), "files_edited": sorted(bucket["edits"]),
            "files_tracked": sorted(bucket["tracked"]), "bash_commands": bucket["bash"],
            "todos": bucket["todos"],
            "first_ts": bucket["first_ts"].isoformat() if bucket["first_ts"] else None,
            "last_ts": bucket["last_ts"].isoformat() if bucket["last_ts"] else None,
        }
    return out


def find_repos() -> list[str]:
    cfg = config.load()
    root = config.expand(cfg["sources"]["git_search_root"])
    max_depth = cfg["sources"]["git_max_depth"]
    repos = []
    for dirpath, dirnames, _ in os.walk(root, onerror=lambda e: None):
        if config.is_walk_excluded(dirpath):
            dirnames[:] = []
            continue
        if dirpath[len(root):].count(os.sep) >= max_depth:
            dirnames[:] = []
            continue
        if ".git" in dirnames:
            repos.append(config.nfc(dirpath))
            dirnames[:] = [d for d in dirnames if d != ".git"]

    # repositories that live under a skipped tree have to be named explicitly
    for extra in cfg["sources"].get("extra_repo_roots", []) or []:
        path = os.path.normpath(config.expand(extra))
        if os.path.isdir(os.path.join(path, ".git")) and path not in repos:
            repos.append(config.nfc(path))
    return repos


def collect_git(date_str: str) -> dict:
    """Commits authored by the configured identities inside the logical day.

    Author filtering is mandatory, not cosmetic: vendored forks in the home
    directory contribute hundreds of upstream commits that are not this
    person's work.
    """
    start, end = config.day_window(date_str)
    authors = [a for a in config.load()["git"].get("authors", []) if a and a.strip()]
    if not authors:
        # An empty list must mean "none", not "everyone". Without --author flags
        # git returns every commit in the tree, so a fresh install that had not
        # filled this in yet would pull hundreds of upstream commits out of
        # vendored forks and report them as the user's work.
        return {"commits": [], "repos_scanned": 0, "failures": [],
                "skipped": "git.authors 가 비어 있어 커밋을 수집하지 않았습니다"}

    args = ["--all", f"--since={start.isoformat()}", f"--until={end.isoformat()}"]
    for author in authors:
        args += ["--author", author]

    commits, repos, failures = [], find_repos(), []
    for repo in repos:
        try:
            # `core.quotepath=false` so non-ASCII paths arrive as themselves
            # rather than as \344\270\255 escapes, and the output is decoded as
            # UTF-8 explicitly: `text=True` would use the process locale, which
            # on a Korean Windows install is cp949 and raises UnicodeDecodeError
            # on the first Korean commit subject — losing the whole repository
            # with no error anyone would connect to the cause.
            result = subprocess.run(
                ["git", "-C", repo, "-c", "core.quotepath=false", "log", *args,
                 "--pretty=format:%H\x1f%cI\x1f%aI\x1f%ae\x1f%s", "--shortstat", "--name-only"],
                capture_output=True, timeout=GIT_TIMEOUT_SEC)
        except (subprocess.SubprocessError, OSError) as error:
            failures.append({"repo": repo, "reason": f"{type(error).__name__}"})
            continue
        # a non-zero exit is indistinguishable from "no commits" if only stdout
        # is read — dubious ownership, a stale index.lock, or a denied path all
        # look like a quiet day otherwise
        if result.returncode != 0:
            reason = result.stderr.decode("utf-8", errors="replace").strip()
            failures.append({"repo": repo,
                             "reason": reason[:120] or f"exit {result.returncode}"})
            continue
        commits += _parse_git_log(result.stdout.decode("utf-8", errors="replace"), repo)

    return {"commits": commits, "repos_scanned": len(repos), "failures": failures}


def _parse_git_log(output: str, repo: str) -> list[dict]:
    """Parse the log stream.

    `at` is the committer date because that is what `--since/--until` filter
    on. Using the author date here would put a rebased or amended commit on a
    day the window never selected, so it would appear under the wrong date.
    """
    commits, current = [], None
    for line in output.splitlines():
        if "\x1f" in line:
            if current:
                commits.append(current)
            sha, committed, authored, email, subject = line.split("\x1f", 4)
            current = {"repo": repo, "project": os.path.basename(repo), "sha": sha[:8],
                       "at": committed, "authored_at": authored,
                       "author": email, "subject": subject,
                       "files": 0, "insertions": 0, "deletions": 0}
        elif current and line.strip():
            stripped = line.strip()
            if "changed" in stripped and ("insertion" in stripped or "deletion" in stripped
                                          or "file" in stripped):
                for count, key in ((" file", "files"), ("insertion", "insertions"),
                                   ("deletion", "deletions")):
                    for part in stripped.split(","):
                        if count in part:
                            digits = "".join(c for c in part if c.isdigit())
                            if digits:
                                current[key] = int(digits)
            else:
                # --name-only lines: the paths this commit touched. git answers
                # with forward slashes, so the join has to be normalized or the
                # result never matches what os.walk produced on Windows.
                current.setdefault("paths", []).append(
                    config.nfc(os.path.normpath(os.path.join(repo, stripped))))
    if current:
        commits.append(current)
    return commits


def collect(date_str: str) -> dict:
    start, end = config.day_window(date_str)
    sessions = collect_sessions(date_str)
    labeled = collect_labeled_sessions(date_str)
    codex = collect_codex.collect(date_str)
    git = collect_git(date_str)

    # Only the projects the day already touched are scanned, so the filesystem
    # sweep stays bounded and cannot wander into unrelated trees.
    from project_roots import project_root
    roots, committed = set(), set()
    for cwd in list(sessions["projects"]) + list(codex["projects"]):
        root = project_root(cwd)
        if root:
            roots.add(root)
    for commit in git["commits"]:
        roots.add(commit["repo"])
        # a file created and committed the same day is tracked-and-clean, so
        # without this it would be dropped from the disk sweep entirely
        committed.update(commit.get("paths") or [])

    # drop roots nested inside another root, or the inner tree gets walked
    # twice and its files counted under two project labels
    ordered = sorted(roots)
    top_level = [r for r in ordered
                 if not any(r != other
                            and config.key(r).startswith(
                                config.key(other.rstrip("/" + os.sep) + os.sep))
                            for other in ordered)]
    disk = collect_fs.collect(date_str, top_level, committed)
    if sessions["stats"]["transcripts_total"] == 0:
        # finding zero transcripts at all means the source is unreadable or
        # moved, not that nothing happened — treating it as a quiet day would
        # permanently mark the date complete with no content
        raise RuntimeError(
            f"세션 기록을 하나도 찾지 못했습니다: {config.expand(config.load()['sources']['claude_projects_dir'])}")

    return {
        "date": date_str,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "projects": sessions["projects"],
        "labeled_projects": labeled,
        "codex_projects": codex["projects"],
        "disk_changes": disk["roots"],
        "git": git,
        "stats": {**sessions["stats"], "repos_scanned": git["repos_scanned"],
                  "commits": len(git["commits"]),
                  "git_failures": len(git.get("failures", [])),
                  "codex_rollouts": codex["stats"]["rollouts_scanned"],
                  "codex_records": codex["stats"]["records_in_day"],
                  "disk_files": disk["stats"]["files"],
                  "disk_roots": disk["stats"]["roots_scanned"],
                  "git_skipped": git.get("skipped")},
        "git_failures": git.get("failures", []),
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: collect.py <YYYY-MM-DD> [out.json]", file=sys.stderr)
        return 2
    date_str = sys.argv[1]
    data = collect(date_str)
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w", encoding="utf-8") as handle:
            handle.write(payload)
        stats = data["stats"]
        print(f"{date_str}: Claude 전사본 {stats['transcripts_scanned']}/{stats['transcripts_total']}"
              f"(레코드 {stats['records_in_day']:,}) · "
              f"Codex 롤아웃 {stats['codex_rollouts']}(레코드 {stats['codex_records']:,}) · "
              f"커밋 {stats['commits']}건 · 디스크 {stats['disk_files']}개 "
              f"→ {len(payload)/1024:.1f} KB")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
