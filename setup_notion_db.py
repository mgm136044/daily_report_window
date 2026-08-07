"""Create (or find) the daily-report Notion database under the parent page.

Idempotent: if a child database with DB_TITLE already exists under the parent,
it is reused instead of creating a duplicate. The schema is defined here so the
property names always match what the writer queries with.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import notion_schema  # noqa: E402

NOTION_API = "https://api.notion.com"
NOTION_VERSION = "2026-03-11"
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
REQUEST_TIMEOUT_SEC = 30

# Title and property names come from notion_schema so the writer queries the
# same strings this creates. See that module for why they may never drift.


def load_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def page_id_from_url(url: str) -> str:
    """Extract the 32-hex page id from a Notion URL and dash-format it."""
    tail = url.split("?")[0].rstrip("/").split("/")[-1]
    raw = tail.split("-")[-1] if "-" in tail else tail
    raw = raw.replace("-", "")
    if len(raw) != 32:
        raise ValueError(f"page id not found in url: {url}")
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def call(token: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
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
            return error.code, json.loads(error.read().decode())
        except (ValueError, OSError):
            return error.code, {"code": "unparseable_error"}


def find_existing_database(token: str, parent_id: str) -> str | None:
    """Return the id of a child database titled DB_TITLE, else None."""
    status, data = call(token, "GET", f"/v1/blocks/{parent_id}/children?page_size=100")
    if status != 200:
        return None
    for block in data.get("results", []):
        if block.get("type") != "child_database":
            continue
        if block["child_database"].get("title") == notion_schema.db_title():
            return block["id"]
    return None


def create_database(token: str, parent_id: str) -> tuple[int, dict]:
    body = {
        "parent": {"type": "page_id", "page_id": parent_id},
        "title": [{"type": "text", "text": {"content": notion_schema.db_title()}}],
        "initial_data_source": {"properties": notion_schema.properties_definition()},
    }
    return call(token, "POST", "/v1/databases", body)


def main() -> int:
    env = load_env(ENV_PATH)
    token = env.get("DAILY_REPORT_NOTION_TOKEN", "")
    url = env.get("DAILY_REPORT_PARENT_PAGE_URL", "")
    if not token:
        print("DAILY_REPORT_NOTION_TOKEN is empty", file=sys.stderr)
        return 1
    if not url:
        print("DAILY_REPORT_PARENT_PAGE_URL is empty", file=sys.stderr)
        return 1

    parent_id = page_id_from_url(url)

    existing = find_existing_database(token, parent_id)
    if existing:
        print(f"이미 존재하는 데이터베이스를 재사용합니다: {existing}")
        database_id = existing
    else:
        status, data = create_database(token, parent_id)
        if status != 200:
            print(f"생성 실패 {status} — {data.get('code')}: {data.get('message')}", file=sys.stderr)
            return 1
        database_id = data["id"]
        print(f"데이터베이스 생성 완료: {database_id}")
        print(f"URL: {data.get('url')}")

    # data_source id must be resolved at runtime, never hardcoded
    status, data = call(token, "GET", f"/v1/databases/{database_id}")
    if status != 200:
        print(f"조회 실패 {status} — {data.get('code')}", file=sys.stderr)
        return 1
    sources = data.get("data_sources") or []
    print(f"data_sources: {[s.get('id') for s in sources]}")
    for source in sources:
        status, detail = call(token, "GET", f"/v1/data_sources/{source['id']}")
        if status == 200:
            print(f"속성 {len(detail.get('properties', {}))}개: "
                  f"{sorted(detail.get('properties', {}).keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
