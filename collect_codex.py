"""Collect one logical day's work from Codex CLI rollout logs.

Codex stores each thread as `~/.codex/sessions/YYYY/MM/DD/rollout-<iso>-<uuid>.jsonl`.
Every line is `{timestamp, type, payload}`.

Two things shape this collector:

  - `cwd` appears only once per file, in the `session_meta` record. Every other
    record has to inherit it, so the file is read in order and the meta is
    captured before anything else is attributed.
  - A rollout can be a subagent thread rather than a user session. Its
    "user messages" are the orchestrator's instructions, not the person's, so
    counting them as prompts floods the report with machine chatter.
"""

from __future__ import annotations

import glob
import json
import os
import re
from collections import defaultdict

import config

TOOL_NAME_KEYS = ("exec", "exec_command", "shell")

# task_complete fires once per turn, so a busy day produces hundreds. Only the
# later ones describe where the work landed; the rest are intermediate steps.
OUTCOME_MAX_CHARS = 400
OUTCOMES_KEPT = 12
COMMAND_MAX_CHARS = 300


def _new_bucket() -> dict:
    return {
        "sessions": set(), "branches": set(), "prompts": [], "commands": [],
        "files_added": set(), "files_updated": set(), "files_deleted": set(),
        "plans": [], "outcomes": [], "subagent_threads": 0,
        "first_ts": None, "last_ts": None,
    }


_CMD_KEY = re.compile(r'["\']?cmd["\']?\s*:\s*')
_PLAN_KEY = re.compile(r"tools\.update_plan\s*\(")
_STEP_KEY = re.compile(r'["\']?step["\']?\s*:\s*')


MAX_LITERAL_CHARS = 4000


def _scan_quoted(rest: str, quote: str) -> str | None:
    """Read a quote-delimited literal, honouring backslash escapes.

    Bounded: an unterminated quote used to run to the end of a 30 KB block and
    return the whole thing as if it were one command.
    """
    end = 1
    while end < len(rest) and end <= MAX_LITERAL_CHARS:
        if rest[end] == "\\":
            end += 2
            continue
        if rest[end] == quote:
            return rest[1:end]
        end += 1
    return None


def _decode_string_at(text: str, index: int) -> str | None:
    """Decode the string literal starting at `index`, if there is one.

    Handles all three JS forms. Only supporting `"` and `'` silently discarded
    3.7% of real command sites — and the losses clustered on backtick-quoted
    sub-delegations (`codex exec …`), which are the most significant commands
    of the day.
    """
    rest = text[index:].lstrip()
    if not rest:
        return None
    if rest[0] == "`":
        return _scan_quoted(rest, "`")
    if rest[0] == "'":
        return _scan_quoted(rest, "'")
    if rest[0] == '"':
        try:
            decoded, _ = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            # JS allows escapes JSON rejects (e.g. \') — fall back to scanning
            return _scan_quoted(rest, '"')
        return decoded if isinstance(decoded, str) else None
    return None


def extract_commands(payload: dict) -> list[str]:
    """Pull the real shell commands out of Codex's JS tool wrapper.

    Codex's `exec` does not take a command — it takes JavaScript that calls
    `tools.exec_command({cmd: "..."})`, often several times in one block with
    surrounding loops and bookkeeping. Storing the block whole made shell
    invocations 80% of the collected bytes (up to 35 KB each) while burying the
    part that says what was actually run.
    """
    if payload.get("name") not in TOOL_NAME_KEYS:
        return []
    raw = payload.get("input") or payload.get("arguments") or ""
    if not isinstance(raw, str):
        raw = json.dumps(raw, ensure_ascii=False)

    commands = []
    for match in _CMD_KEY.finditer(raw):
        value = _decode_string_at(raw, match.end())
        if value and value.strip():
            commands.append(value.strip())
    if commands:
        return commands
    # A block with no `cmd:` is orchestration scaffolding. Returning the raw JS
    # as a "command" put things like `store("wave3_prefixes",{plan:...` into the
    # shell-command list.
    return []


def extract_plan_steps(payload: dict) -> list[str]:
    """Plan steps arrive two different ways and both have to be handled.

    Inside an `exec` block Codex calls `tools.update_plan({plan:[{step:…}]})`.
    But it *also* emits a standalone `function_call` named `update_plan` whose
    arguments are plain JSON. Requiring the literal `tools.update_plan(` threw
    away every one of those — 100 out of 100 in the real logs.
    """
    raw = payload.get("input") or payload.get("arguments") or ""
    if not isinstance(raw, str):
        return []
    if payload.get("name") == "update_plan":
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return [str(item.get("step")).strip()
                    for item in parsed.get("plan") or []
                    if isinstance(item, dict) and item.get("step")]
    if not _PLAN_KEY.search(raw):
        return []
    steps = []
    for match in _STEP_KEY.finditer(raw):
        value = _decode_string_at(raw, match.end())
        if value and value.strip():
            steps.append(value.strip())
    return steps


def collect(date_str: str) -> dict:
    cfg = config.load()
    root = config.expand(cfg["sources"].get("codex_sessions_dir", "~/.codex/sessions"))
    if not os.path.isdir(root):
        return {"projects": {}, "stats": {"rollouts_total": 0, "rollouts_scanned": 0,
                                          "records_in_day": 0, "available": False}}

    start, end = config.day_window(date_str)
    prompt_cap = cfg["noise"]["prompt_max_chars"]
    buckets: dict[str, dict] = defaultdict(_new_bucket)
    files_seen = files_scanned = records_in_day = 0

    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        files_seen += 1
        try:
            if os.path.getmtime(path) < start.timestamp():
                continue
        except OSError:
            continue
        files_scanned += 1

        meta = {"cwd": None, "branch": None, "rollout_id": None, "is_subagent": False}
        pending: list[tuple] = []

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
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue

                if record.get("type") == "session_meta":
                    # A subagent rollout carries a second meta at the end — the
                    # parent orchestrator's — whose source is "cli". Letting the
                    # last one win flipped 90 of 136 multi-meta files back to
                    # "not a subagent", so machine-written instructions were
                    # counted as the person's prompts (96% of one day's).
                    if "subagent" in (payload.get("source") or {}):
                        meta["is_subagent"] = True
                    if meta["cwd"]:          # first meta defines the thread
                        continue
                    meta["cwd"] = config.nfc(payload.get("cwd") or "")
                    meta["branch"] = (payload.get("git") or {}).get("branch")
                    # `session_id` is the thread *family* id and is missing on
                    # 371 of 1205 metas; `id` is per-rollout and always present
                    meta["rollout_id"] = payload.get("id") or payload.get("session_id")
                    continue

                stamp = config.parse_iso(record.get("timestamp"))
                if stamp is None or not (start <= stamp.astimezone(config.local_tz()) < end):
                    continue
                records_in_day += 1
                pending.append((stamp, payload))

        if not pending or not meta["cwd"] or config.is_excluded(meta["cwd"]):
            continue
        _absorb(buckets[meta["cwd"]], meta, pending, prompt_cap)

    projects = {}
    for cwd, bucket in buckets.items():
        projects[cwd] = {
            "sessions": sorted(bucket["sessions"]),
            "branches": sorted(b for b in bucket["branches"] if b),
            "prompts": bucket["prompts"],
            "bash_commands": [c[:COMMAND_MAX_CHARS] + (" …[truncated]" if len(c) > COMMAND_MAX_CHARS else "")
                              for c in bucket["commands"]],
            "files_added": sorted(bucket["files_added"]),
            "files_updated": sorted(bucket["files_updated"]),
            "files_deleted": sorted(bucket["files_deleted"]),
            "plans": bucket["plans"],
            # sorted by time, not by glob order — the point is the latest ones
            "outcomes": [m for _, m in sorted(bucket["outcomes"])[-OUTCOMES_KEPT:]],
            "subagent_threads": bucket["subagent_threads"],
            "first_ts": bucket["first_ts"].isoformat() if bucket["first_ts"] else None,
            "last_ts": bucket["last_ts"].isoformat() if bucket["last_ts"] else None,
        }

    return {
        "projects": projects,
        "stats": {"rollouts_total": files_seen, "rollouts_scanned": files_scanned,
                  "records_in_day": records_in_day, "available": True},
    }


def _absorb(bucket: dict, meta: dict, pending: list[tuple], prompt_cap: int) -> None:
    if meta["rollout_id"]:
        bucket["sessions"].add(meta["rollout_id"])
    if meta["branch"]:
        bucket["branches"].add(meta["branch"])
    if meta["is_subagent"]:
        bucket["subagent_threads"] += 1

    for stamp, payload in pending:
        if bucket["first_ts"] is None or stamp < bucket["first_ts"]:
            bucket["first_ts"] = stamp
        if bucket["last_ts"] is None or stamp > bucket["last_ts"]:
            bucket["last_ts"] = stamp

        ptype = payload.get("type")

        if ptype == "user_message" and not meta["is_subagent"]:
            text = payload.get("message") or ""
            if text.strip():
                bucket["prompts"].append(text[:prompt_cap] + (" …[truncated]" if len(text) > prompt_cap else ""))

        elif ptype == "patch_apply_end":
            if payload.get("success") is False:
                continue
            for path, change in (payload.get("changes") or {}).items():
                kind = (change or {}).get("type", "update") if isinstance(change, dict) else "update"
                target = {"add": "files_added", "delete": "files_deleted"}.get(kind, "files_updated")
                bucket[target].add(config.nfc(path))

        elif ptype in ("custom_tool_call", "function_call"):
            bucket["plans"] += extract_plan_steps(payload)
            bucket["commands"] += extract_commands(payload)

        elif ptype == "task_complete":
            message = payload.get("last_agent_message") or ""
            if message.strip():
                bucket["outcomes"].append((stamp.isoformat(), message[:OUTCOME_MAX_CHARS]))


def main() -> int:
    import sys
    if len(sys.argv) < 2:
        print("usage: collect_codex.py <YYYY-MM-DD>")
        return 2
    data = collect(sys.argv[1])
    stats = data["stats"]
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    print(f"{sys.argv[1]}: 롤아웃 {stats['rollouts_scanned']}/{stats['rollouts_total']}, "
          f"당일 레코드 {stats['records_in_day']:,}, cwd {len(data['projects'])}개 "
          f"→ {len(payload)/1024:.1f} KB")
    for cwd, p in data["projects"].items():
        print(f"  {cwd}")
        print(f"    프롬프트{len(p['prompts'])} 명령{len(p['bash_commands'])} "
              f"추가{len(p['files_added'])} 수정{len(p['files_updated'])} "
              f"삭제{len(p['files_deleted'])} 계획{len(p['plans'])} "
              f"결과{len(p['outcomes'])} 서브에이전트{p['subagent_threads']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
