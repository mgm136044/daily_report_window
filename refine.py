"""Turn a raw collection into something worth handing to a summarizer.

Three jobs, in order of how much they matter:

  1. Roll every `cwd` up to its project. Leaf directories like `04_보고서` or
     `assets/photos` are not projects, and labelling a day's work after them
     produces a report nobody can read.
  2. Drop navigation noise. Measured on real days, ~74% of captured shell
     commands are `ls`/`cd`/`cat`-class inspection that says nothing about
     what was accomplished.
  3. Attach the day's commits to the project they belong to.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict

import config
from project_roots import project_label, project_root


def _noise_matcher() -> re.Pattern:
    prefixes = config.load()["noise"]["bash_command_prefixes"]
    escaped = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
    return re.compile(rf"^\s*(?:{escaped})(?:\s|$)")


def filter_commands(commands: list[str]) -> tuple[list[str], int]:
    """Return (kept, dropped_count) after removing noise and near-duplicates."""
    cfg = config.load()["noise"]
    noise = _noise_matcher()
    prefix_len = cfg["dedupe_prefix_chars"]
    cap = cfg["command_max_chars"]

    kept, seen, dropped = [], set(), 0
    for command in commands:
        collapsed = " ".join(command.split())
        if not collapsed or noise.match(collapsed):
            dropped += 1
            continue
        key = collapsed[:prefix_len]
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        kept.append(collapsed[:cap] + (" …[truncated]" if len(collapsed) > cap else ""))
    return kept, dropped


def _dedupe(items: list[str]) -> tuple[list[str], int]:
    """Drop repeats, preserving order.

    Codex re-sends the whole plan on every `update_plan`, so a day of work
    yields the same handful of steps a hundred times over.
    """
    seen, out = set(), []
    for item in items:
        key = " ".join(str(item).split())[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out, len(items) - len(out)


def _empty_project() -> dict:
    return {
        "cwds": set(), "sessions": set(), "branches": set(), "slugs": set(),
        "skills_used": set(), "prompts": [], "files_written": set(),
        "files_edited": set(), "files_tracked": set(), "bash_commands": [],
        "todos": [], "commits": [], "plans": [], "outcomes": [],
        "tools": set(), "codex_sessions": set(), "files_on_disk": set(),
        "first_ts": None, "last_ts": None,
    }


def _local_time(iso_value: str | None) -> str | None:
    """Render a timestamp in the user's local time.

    The logs are UTC. Handing that to the summarizer made it gamble on the
    conversion every time — in one report it turned 08:15Z into "저녁" correctly
    and 04:28Z into "오전" wrongly (that is 13:28 local). Converting here means
    the model never has to.
    """
    stamp = config.parse_iso(iso_value)
    if stamp is None:
        return None
    return stamp.astimezone(config.local_tz()).strftime("%Y-%m-%d %H:%M")


def _merge_time(bucket: dict, key: str, value: str | None) -> None:
    if not value:
        return
    current = bucket[key]
    if current is None or (value < current if key == "first_ts" else value > current):
        bucket[key] = value


def refine(raw: dict) -> dict:
    merged: dict[str, dict] = defaultdict(_empty_project)
    dropped_cwds, noise_dropped = [], 0

    for cwd, data in raw["projects"].items():
        label = project_label(cwd)
        if label is None:
            dropped_cwds.append(cwd)
            continue
        bucket = merged[label]
        bucket["cwds"].add(cwd)
        bucket["sessions"].update(data["sessions"])
        bucket["branches"].update(data["branches"])
        bucket["slugs"].update(data["slugs"])
        bucket["skills_used"].update(data["skills_used"])
        bucket["prompts"] += data["prompts"]
        bucket["files_written"].update(data["files_written"])
        bucket["files_edited"].update(data["files_edited"])
        bucket["files_tracked"].update(data["files_tracked"])
        bucket["todos"] += data["todos"]
        bucket["bash_commands"] += data["bash_commands"]
        bucket["tools"].add("Claude Code")
        for key in ("first_ts", "last_ts"):
            _merge_time(bucket, key, data.get(key))

    # Already-labelled sessions bypass the rollup — their cwd is a scratch
    # folder, so there is nothing to roll up to.
    for label, data in (raw.get("labeled_projects") or {}).items():
        bucket = merged[label]
        bucket["sessions"].update(data["sessions"])
        bucket["skills_used"].update(data["skills_used"])
        bucket["prompts"] += data["prompts"]
        bucket["files_written"].update(data["files_written"])
        bucket["files_edited"].update(data["files_edited"])
        bucket["files_tracked"].update(data["files_tracked"])
        bucket["bash_commands"] += data["bash_commands"]
        bucket["todos"] += data["todos"]
        bucket["tools"].add("Claude Code")
        for key in ("first_ts", "last_ts"):
            _merge_time(bucket, key, data.get(key))

    # Codex work rolls into the same project label, so a day spent on one
    # project through both tools reads as one story rather than two.
    for cwd, data in (raw.get("codex_projects") or {}).items():
        label = project_label(cwd)
        if label is None:
            dropped_cwds.append(cwd)
            continue
        bucket = merged[label]
        bucket["cwds"].add(cwd)
        bucket["codex_sessions"].update(data["sessions"])
        bucket["branches"].update(data["branches"])
        bucket["prompts"] += data["prompts"]
        bucket["files_written"].update(data["files_added"])
        bucket["files_edited"].update(data["files_updated"])
        bucket["files_tracked"].update(data["files_deleted"])
        bucket["bash_commands"] += data["bash_commands"]
        bucket["plans"] += data["plans"]
        bucket["outcomes"] += data["outcomes"]
        bucket["tools"].add("Codex")
        for key in ("first_ts", "last_ts"):
            _merge_time(bucket, key, data.get(key))

    # Disk-observed changes attach to the project whose root they sit under.
    # They are kept apart from tool-recorded files because their provenance is
    # weaker — an mtime says a file changed, not who changed it.
    for root, paths in (raw.get("disk_changes") or {}).items():
        label = project_label(root)
        if label is None:
            continue
        bucket = merged[label]
        bucket["cwds"].add(root)
        bucket["files_on_disk"].update(paths)

    for commit in raw["git"]["commits"]:
        label = project_label(commit["repo"]) or commit["project"]
        merged[label]["commits"].append(commit)

    # Files are attributed to a project by cwd, but disk observations are
    # attributed by path. When work in one cwd writes into another project's
    # tree, the same file landed in both — as a tool record here and a disk
    # observation there. Subtract across the whole day, not per label.
    tool_recorded = set()
    for bucket in merged.values():
        tool_recorded |= bucket["files_written"] | bucket["files_edited"] | bucket["files_tracked"]

    projects = {}
    for label, bucket in merged.items():
        commands, dropped = filter_commands(bucket["bash_commands"])
        noise_dropped += dropped
        plans, _ = _dedupe(bucket["plans"])
        projects[label] = {
            "root": project_root(sorted(bucket["cwds"])[0]) if bucket["cwds"] else None,
            "tools": sorted(bucket["tools"]),
            "session_count": len(bucket["sessions"]) + len(bucket["codex_sessions"]),
            "branches": sorted(bucket["branches"]),
            "titles": sorted(bucket["slugs"]),
            "skills_used": sorted(bucket["skills_used"]),
            "prompts": bucket["prompts"],
            "files_written": sorted(bucket["files_written"]),
            "files_edited": sorted(bucket["files_edited"]),
            "files_tracked": sorted(bucket["files_tracked"]),
            "files_on_disk": sorted(bucket["files_on_disk"] - tool_recorded),
            "bash_commands": commands,
            "todos": bucket["todos"],
            "plans": plans,
            "outcomes": bucket["outcomes"],
            "commits": bucket["commits"],
            "active_from": _local_time(bucket["first_ts"]),
            "active_to": _local_time(bucket["last_ts"]),
        }

    # busiest projects first — the summarizer reads top-down
    ordered = dict(sorted(projects.items(),
                          key=lambda kv: (len(kv[1]["prompts"]),
                                          len(kv[1]["files_written"]) + len(kv[1]["files_edited"])),
                          reverse=True))

    unique_files = set()
    for p in ordered.values():
        unique_files |= set(p["files_written"]) | set(p["files_edited"])
        unique_files |= set(p["files_tracked"]) | set(p["files_on_disk"])
    files_total = len(unique_files)
    return {
        "date": raw["date"],
        "window": raw["window"],
        "projects": ordered,
        "stats": {
            "projects": len(ordered),
            "sessions": sum(p["session_count"] for p in ordered.values()),
            "files": files_total,
            "commits": sum(len(p["commits"]) for p in ordered.values()),
            "noise_commands_dropped": noise_dropped,
            "cwds_dropped": len(dropped_cwds),
        },
        "dropped_cwds": dropped_cwds,
    }


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: refine.py <raw.json> <out.json>", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as handle:
        raw = json.load(handle)
    data = refine(raw)
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    with open(sys.argv[2], "w", encoding="utf-8") as handle:
        handle.write(payload)
    stats = data["stats"]
    before = os.path.getsize(sys.argv[1]) / 1024
    print(f"{data['date']}: 프로젝트 {stats['projects']}개, 세션 {stats['sessions']}, "
          f"파일 {stats['files']}, 커밋 {stats['commits']} | "
          f"노이즈 명령 {stats['noise_commands_dropped']}건·cwd {stats['cwds_dropped']}개 제거 | "
          f"{before:.1f} KB → {len(payload)/1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
