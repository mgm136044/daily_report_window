"""Notion property names, defined once.

`setup_notion_db.py` creates the database with these names and
`notion_upsert.py` queries by them. They agree because they come from here: a
one-character difference makes the date lookup miss, and a missed lookup is not
an error — it appends a new row every day instead of updating one, silently,
until someone notices the database has duplicates going back a week.

The language is **pinned at creation time**. Changing it later renames nothing
in Notion; it only makes the writer look for properties that do not exist. So it
lives in `[notion] schema_language`, separate from `report.language`, which
controls the prose and can be changed freely.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config  # noqa: E402

# Logical keys are the same in every language; only the surface names differ.
SCHEMAS: dict[str, dict] = {
    "ko": {
        "db_title": "하루 마감 보고서",
        "weekdays": ["월", "화", "수", "목", "금", "토", "일"],
        "props": {
            "title": "이름",
            "date": "날짜",
            "summary": "요약",
            "projects": "프로젝트",
            "tags": "태그",
            "sessions": "세션 수",
            "commits": "커밋 수",
            "files": "생성 파일 수",
            "status": "상태",
            "created_at": "생성 시각",
            "source": "원본",
        },
        "status": {"done": "완료", "draft": "초안", "failed": "실패"},
    },
    "en": {
        "db_title": "Daily report",
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "props": {
            "title": "Name",
            "date": "Date",
            "summary": "Summary",
            "projects": "Projects",
            "tags": "Tags",
            "sessions": "Sessions",
            "commits": "Commits",
            "files": "Files",
            "status": "Status",
            "created_at": "Created",
            "source": "Source",
        },
        "status": {"done": "Done", "draft": "Draft", "failed": "Failed"},
    },
}

DEFAULT_LANGUAGE = "ko"

STATUS_COLORS = {"done": "green", "draft": "yellow", "failed": "red"}


def language() -> str:
    """Which language the *database* speaks.

    Falls back to report.language so an install that never pinned one still
    works, and to Korean if that is a language with no schema.
    """
    cfg = config.load()
    pinned = (cfg.get("notion") or {}).get("schema_language")
    if pinned in SCHEMAS:
        return pinned
    reporting = (cfg.get("report") or {}).get("language", DEFAULT_LANGUAGE)
    return reporting if reporting in SCHEMAS else DEFAULT_LANGUAGE


def schema() -> dict:
    return SCHEMAS[language()]


def props() -> dict[str, str]:
    """Logical key → the property name Notion actually holds."""
    return schema()["props"]


def prop(key: str) -> str:
    return props()[key]


def db_title() -> str:
    return schema()["db_title"]


def weekday(index: int) -> str:
    return schema()["weekdays"][index]


def status_label(key: str) -> str:
    return schema()["status"][key]


def properties_definition() -> dict[str, dict]:
    """The `properties` payload for creating the database."""
    name = props()
    return {
        name["title"]: {"title": {}},
        name["date"]: {"date": {}},
        name["summary"]: {"rich_text": {}},
        name["projects"]: {"multi_select": {"options": []}},
        name["tags"]: {"multi_select": {"options": []}},
        name["sessions"]: {"number": {"format": "number"}},
        name["commits"]: {"number": {"format": "number"}},
        name["files"]: {"number": {"format": "number"}},
        name["status"]: {
            "select": {
                "options": [
                    {"name": status_label(key), "color": color}
                    for key, color in STATUS_COLORS.items()
                ]
            }
        },
        name["created_at"]: {"date": {}},
        name["source"]: {"rich_text": {}},
    }
