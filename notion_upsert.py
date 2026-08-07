"""Upsert one day's report into the Notion database, keyed by the date property.

Notion has no unique constraint and no upsert primitive, so the flow is
query-then-create-or-update. Two or more hits means something already went
wrong; that is reported loudly rather than silently picking one.

Body is written as markdown in a single request when the API accepts it, and
falls back to the block API otherwise. Which path was taken is printed, so the
fallback never happens invisibly.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

NOTION_API = "https://api.notion.com"
NOTION_VERSION = "2026-03-11"
HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")

sys.path.insert(0, HERE)

import config  # noqa: E402
import notion_schema  # noqa: E402

SUMMARY_MAX_CHARS = 2000  # Notion caps a single rich_text object at 2000
MAX_TRIES = 5
BACKOFF_CAP_SEC = 30
REQUEST_TIMEOUT_SEC = 60
TRANSIENT_CODES = {429, 500, 502, 503, 529}


def load_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        setup = "install.ps1" if os.name == "nt" else "install.sh"
        raise FileNotFoundError(
            f".env 가 없습니다: {path} — {setup} 을 실행하거나 "
            f".env.example 을 .env 로 복사한 뒤 값을 채우세요")
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def call(token: str, method: str, path: str, body: dict | None = None,
         retries: int = MAX_TRIES) -> tuple[int, dict]:
    """Call the Notion API, retrying transient failures with exponential backoff.

    `retries=1` disables retrying. Page creation must use it: Notion has no
    idempotency key, so a create that succeeded server-side but returned 502
    would be retried into a second row, and the date query would then find two
    and refuse to write that date ever again.
    """
    tries = max(1, retries)
    for attempt in range(tries):
        request = urllib.request.Request(
            NOTION_API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SEC) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read().decode())
            except (ValueError, OSError):
                payload = {"code": "unparseable_error"}
            if error.code not in TRANSIENT_CODES or attempt == tries - 1:
                return error.code, payload
            retry_after = error.headers.get("Retry-After") if error.headers else None
            delay = float(retry_after) if retry_after else min(2 ** attempt, BACKOFF_CAP_SEC)
            time.sleep(delay)
    return -1, {"code": "exhausted_retries"}


def resolve_data_source(token: str, database_id: str) -> str:
    """Look up the data source id at runtime; it must never be hardcoded."""
    status, data = call(token, "GET", f"/v1/databases/{database_id}")
    if status != 200:
        raise RuntimeError(f"database lookup failed {status}: {data.get('code')}")
    sources = data.get("data_sources") or []
    if not sources:
        raise RuntimeError("database has no data sources")
    if len(sources) > 1:
        print(f"경고: data source가 {len(sources)}개입니다. 첫 번째를 씁니다.", file=sys.stderr)
    return sources[0]["id"]


def find_by_date(token: str, data_source_id: str, date_str: str) -> list[str]:
    body = {
        "filter": {"property": notion_schema.prop("date"), "date": {"equals": date_str}},
        "page_size": 5,
    }
    status, data = call(token, "POST", f"/v1/data_sources/{data_source_id}/query", body)
    if status != 200:
        raise RuntimeError(f"query failed {status}: {data.get('code')} {data.get('message')}")
    return [row["id"] for row in data.get("results", [])]


OPTION_MAX_CHARS = 100


def clean_option(value: str) -> str:
    """Sanitize a select/multi-select option name.

    Notion rejects commas outright and caps option names at 100 characters.
    An empty name is a 400, so callers must drop whatever comes back empty.
    """
    cleaned = " ".join(value.replace(",", " ").split())
    return cleaned[:OPTION_MAX_CHARS]


def markdown_to_blocks(markdown: str) -> list[dict]:
    """Minimal markdown → Notion blocks, used only when the markdown API fails.

    Deliberately plain: headings, bullets, and paragraphs. The point is to not
    lose the day's report when the one-shot path is unavailable, not to render
    it beautifully.
    """
    blocks: list[dict] = []

    def rich(text: str) -> list[dict]:
        return [{"type": "text", "text": {"content": text[i:i + 2000]}}
                for i in range(0, max(len(text), 1), 2000)]

    for line in markdown.splitlines():
        text = line.rstrip()
        if not text.strip():
            continue
        stripped = text.lstrip()
        if stripped.startswith("### "):
            kind, content = "heading_3", stripped[4:]
        elif stripped.startswith("## "):
            kind, content = "heading_2", stripped[3:]
        elif stripped.startswith("# "):
            kind, content = "heading_1", stripped[2:]
        elif stripped.startswith(("- ", "* ")):
            kind, content = "bulleted_list_item", stripped[2:]
        else:
            kind, content = "paragraph", stripped
        blocks.append({"object": "block", "type": kind,
                       kind: {"rich_text": rich(content)}})
    return blocks


def append_blocks(token: str, page_id: str, blocks: list[dict]) -> None:
    """Append in batches; Notion accepts at most 100 children per request."""
    for start in range(0, len(blocks), 100):
        status, data = call(token, "PATCH", f"/v1/blocks/{page_id}/children",
                            {"children": blocks[start:start + 100]})
        if status != 200:
            raise RuntimeError(f"본문 블록 추가 실패 {status}: {data.get('code')} {data.get('message')}")


MULTI_SELECT_MAX = 100


def _options(values: list[str]) -> list[dict]:
    seen, out = set(), []
    for value in values:
        name = clean_option(value)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name})
        if len(out) >= MULTI_SELECT_MAX:
            break
    return out


def build_properties(date_str: str, summary: str, projects: list[str],
                     tags: list[str], sessions: int, commits: int,
                     files: int, status_label: str, source_path: str) -> dict:
    day = datetime.strptime(date_str, "%Y-%m-%d")
    name = notion_schema.props()
    return {
        name["title"]: {"title": [{"type": "text", "text": {
            "content": f"{date_str} ({notion_schema.weekday(day.weekday())})"}}]},
        name["date"]: {"date": {"start": date_str}},
        name["summary"]: {"rich_text": [{"type": "text",
                                         "text": {"content": summary[:SUMMARY_MAX_CHARS]}}]},
        # clean_option can return "" (a name of only commas), and an empty
        # option name is a 400 — so filter after cleaning, not before.
        # Notion also caps a multi-select at 100 entries.
        name["projects"]: {"multi_select": _options(projects)},
        name["tags"]: {"multi_select": _options(tags)},
        name["sessions"]: {"number": sessions},
        name["commits"]: {"number": commits},
        name["files"]: {"number": files},
        name["status"]: {"select": {"name": status_label}},
        # local time, not a fixed offset: this timestamp is read next to the
        # reader's own clock.
        name["created_at"]: {"date": {
            "start": datetime.now(config.local_tz()).isoformat(timespec="seconds")}},
        name["source"]: {"rich_text": [{"type": "text", "text": {"content": source_path}}]},
    }


def create_row(token: str, data_source_id: str, props: dict, markdown: str) -> tuple[bool, dict]:
    """Create the row with a one-shot markdown body. Returns (used_markdown, response).

    Not retried — see `call`. If the markdown path is rejected the body is
    written as blocks instead; an earlier version silently created a page with
    no body at all and reported success, which lost the day permanently.
    """
    body = {
        "parent": {"type": "data_source_id", "data_source_id": data_source_id},
        "properties": props,
        "markdown": markdown,
    }
    status, data = call(token, "POST", "/v1/pages", body, retries=1)
    if status == 200:
        return True, data

    print(f"  markdown 경로 실패 {status} — {data.get('code')}: "
          f"{str(data.get('message'))[:160]}", file=sys.stderr)
    body.pop("markdown")
    status, data = call(token, "POST", "/v1/pages", body, retries=1)
    if status != 200:
        raise RuntimeError(f"create failed {status}: {data.get('code')} {data.get('message')}")
    append_blocks(token, data["id"], markdown_to_blocks(markdown))
    return False, data


def update_row(token: str, page_id: str, props: dict, markdown: str) -> bool:
    status, data = call(token, "PATCH", f"/v1/pages/{page_id}", {"properties": props})
    if status != 200:
        raise RuntimeError(f"property update failed {status}: {data.get('code')}")

    status, data = call(token, "PATCH", f"/v1/pages/{page_id}/markdown", {
        "type": "replace_content",
        "replace_content": {"new_str": markdown, "allow_deleting_content": True},
    })
    if status == 200:
        return True

    print(f"  markdown 교체 실패 {status} — {data.get('code')}: "
          f"{str(data.get('message'))[:160]}", file=sys.stderr)
    # Erase the stale body first, otherwise the fallback appends a second copy
    # underneath yesterday's text and the page silently grows every night.
    status, data = call(token, "PATCH", f"/v1/pages/{page_id}", {"erase_content": True})
    if status != 200:
        raise RuntimeError(f"본문 비우기 실패 {status}: {data.get('code')} {data.get('message')}")
    append_blocks(token, page_id, markdown_to_blocks(markdown))
    return False


def upsert(token: str, database_id: str, date_str: str, markdown: str,
           summary: str, projects: list[str], tags: list[str],
           sessions: int, commits: int, files: int,
           status_label: str, source_path: str) -> None:
    data_source_id = resolve_data_source(token, database_id)
    hits = find_by_date(token, data_source_id, date_str)
    props = build_properties(date_str, summary, projects, tags,
                             sessions, commits, files, status_label, source_path)

    if len(hits) > 1:
        raise RuntimeError(f"{date_str} 행이 {len(hits)}개입니다. 중복을 먼저 정리하세요: {hits}")

    if hits:
        used_markdown = update_row(token, hits[0], props, markdown)
        print(f"갱신 완료 — page {hits[0]}  (본문: {'markdown 1회' if used_markdown else '폴백'})")
        return

    try:
        used_markdown, data = create_row(token, data_source_id, props, markdown)
    except RuntimeError:
        # The create is not retried, so a failure here may still have landed
        # server-side. Look before trying again, or the next attempt makes a
        # second row and this date becomes unwritable forever.
        recheck = find_by_date(token, data_source_id, date_str)
        if recheck:
            used_markdown = update_row(token, recheck[0], props, markdown)
            print(f"생성 실패 후 확인하니 행이 있어 갱신함 — page {recheck[0]} "
                  f"(본문: {'markdown 1회' if used_markdown else '폴백'})")
            return
        raise
    print(f"생성 완료 — page {data['id']}  (본문: {'markdown 1회' if used_markdown else '폴백'})")
    print(f"URL: {data.get('url')}")


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: notion_upsert.py <YYYY-MM-DD> <report.md> [stats.json]", file=sys.stderr)
        return 2
    date_str, report_path = sys.argv[1], sys.argv[2]
    stats = json.load(open(sys.argv[3], encoding="utf-8")) if len(sys.argv) > 3 else {}

    env = load_env(ENV_PATH)
    token = env.get("DAILY_REPORT_NOTION_TOKEN", "")
    database_id = env.get("DAILY_REPORT_DATABASE_ID", "")
    if not token or not database_id:
        print("DAILY_REPORT_NOTION_TOKEN / DAILY_REPORT_DATABASE_ID 를 .env에 넣으세요", file=sys.stderr)
        return 1

    markdown = open(report_path, encoding="utf-8").read()
    summary = stats.get("summary") or first_paragraph(markdown)

    upsert(
        token, database_id, date_str, markdown, summary,
        projects=stats.get("projects", []),
        tags=stats.get("tags", []),
        sessions=stats.get("sessions", 0),
        commits=stats.get("commits", 0),
        files=stats.get("files", 0),
        status_label=stats.get("status") or notion_schema.status_label("done"),
        source_path=stats.get("source", report_path),
    )
    return 0


def first_paragraph(markdown: str) -> str:
    for block in markdown.split("\n\n"):
        text = block.strip()
        if text and not text.startswith("#"):
            return " ".join(text.split())
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
