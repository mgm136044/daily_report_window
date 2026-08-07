"""Synthetic fixtures.

Tests must pass on a machine that has never run this tool — no `~/.claude`, no
`~/.codex`, no personal projects. Anything built from the maintainer's own
folders would both fail elsewhere and leak what those folders are called.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Placeholder names used everywhere a project name is needed. Deliberately
# boring: a real folder name would say something about its owner.
PROJECT_A = "sample-project"
PROJECT_B = "another-project"
CONTAINER = "workspace"

# Exclusions kept while a synthetic tree is in use.
#
# The suite builds its fixtures under the system temp directory. On macOS that
# is `/private/var/folders/…`, which no shipped exclusion covers; on Windows it
# is `%LOCALAPPDATA%\Temp`, which sits inside AppData — a tree the shipped
# configuration excludes, and must, since it is where the summarizer's own
# transcript lands. Leaving the real lists in place there would make every test
# about project structure pass by collecting nothing.
#
# Only the operating system's own trees are dropped. The structural rules stay,
# because a test that walks into `.git` or `node_modules` is testing the wrong
# thing on either platform.
STRUCTURAL_EXCLUSIONS = ["/node_modules/", "/.git/", "/.venv/", "/__pycache__/"]


@pytest.fixture
def home(tmp_path):
    """A home directory that exists only for this test."""
    root = tmp_path / "home"
    root.mkdir()
    return root


@pytest.fixture
def project(home):
    """A directory that looks like a project: it carries a root marker."""
    path = home / "development" / PROJECT_A
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
    return path


@pytest.fixture
def git_project(home):
    """A real git repository, so `git ls-files` and `status` behave normally."""
    path = home / "development" / PROJECT_B
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "you@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)
    return path


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


@pytest.fixture
def claude_transcript(tmp_path, project):
    """One Claude Code transcript with a prompt, an edit, and a shell command."""
    def build(date="2026-08-04", hour=10):
        stamp = f"{date}T{hour:02d}:00:00.000Z"
        return write_jsonl(
            tmp_path / "claude" / "projects" / "encoded" / "session.jsonl",
            [
                {"type": "user", "timestamp": stamp, "cwd": str(project),
                 "sessionId": "s1", "gitBranch": "main",
                 "message": {"role": "user", "content": "add the parser"}},
                {"type": "assistant", "timestamp": stamp, "cwd": str(project),
                 "sessionId": "s1",
                 "message": {"role": "assistant", "content": [
                     {"type": "tool_use", "name": "Write",
                      "input": {"file_path": str(project / "parser.py")}},
                     {"type": "tool_use", "name": "Bash",
                      "input": {"command": "pytest -q"}},
                 ]}},
            ])
    return build


@pytest.fixture
def codex_rollout(tmp_path, project):
    """One Codex rollout. `subagent` marks it as a machine thread."""
    def build(date="2026-08-04", hour=11, subagent=False, trailing_parent_meta=False):
        stamp = f"{date}T{hour:02d}:00:00.000Z"
        source = ({"subagent": {"thread_spawn": {"depth": 1}}} if subagent else "cli")
        records = [
            {"type": "session_meta", "timestamp": stamp,
             "payload": {"cwd": str(project), "id": "r1", "source": source,
                         "git": {"branch": "main"}}},
            {"type": "event_msg", "timestamp": stamp,
             "payload": {"type": "user_message", "message": "review the design"}},
            {"type": "response_item", "timestamp": stamp,
             "payload": {"type": "custom_tool_call", "name": "exec",
                         "input": 'await tools.exec_command({cmd:"ruff check ."});'}},
            {"type": "response_item", "timestamp": stamp,
             "payload": {"type": "patch_apply_end", "success": True,
                         "changes": {str(project / "design.md"): {"type": "update"}}}},
        ]
        if trailing_parent_meta:
            # A subagent rollout ends with the parent orchestrator's meta,
            # whose source is "cli".
            records.append({"type": "session_meta", "timestamp": stamp,
                            "payload": {"cwd": str(project), "id": "r1", "source": "cli"}})
        return write_jsonl(tmp_path / "codex" / "2026" / "08" / "04" / "rollout-x.jsonl",
                           records)
    return build


@pytest.fixture
def configured(monkeypatch, tmp_path, home):
    """Point the loaded config at the synthetic tree.

    Mutates the cached dict rather than writing a file, so a test never depends
    on the maintainer's own config.toml.
    """
    import config

    def apply(**overrides):
        cfg = config.load()
        cfg["sources"]["claude_projects_dir"] = str(tmp_path / "claude" / "projects")
        cfg["sources"]["codex_sessions_dir"] = str(tmp_path / "codex")
        cfg["sources"]["git_search_root"] = str(home)
        cfg["sources"]["extra_session_globs"] = []
        # see STRUCTURAL_EXCLUSIONS: the synthetic tree lives inside a directory
        # the real configuration is right to exclude
        cfg["sources"]["walk_exclude"] = []
        cfg["exclude"]["paths"] = list(STRUCTURAL_EXCLUSIONS)
        cfg["projects"]["containers"] = [str(home), str(home / "development"), "/"]
        cfg["projects"]["never"] = [str(home), "/"]
        cfg["labels"]["rename"] = {}
        for section, values in overrides.items():
            cfg.setdefault(section, {}).update(values)
        return cfg

    original = json.dumps(config.load(), default=str)
    yield apply
    # restore so later tests see the real configuration
    restored = json.loads(original)
    config.load().clear()
    config.load().update(restored)
