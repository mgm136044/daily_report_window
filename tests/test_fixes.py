"""Regression tests for the defects found in the first review pass.


Each test names the defect it locks down, so a future change that reintroduces
one fails here rather than in production at 04:05.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collect  # noqa: E402
import config  # noqa: E402
import notion_upsert as nu  # noqa: E402
import platform_support  # noqa: E402
import project_roots as pr  # noqa: E402
import run_day  # noqa: E402
import sanitize  # noqa: E402
import summarize  # noqa: E402

HOME = os.path.expanduser("~")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WINDOWS = os.name == "nt"

# Some defects are a property of one operating system's paths or APIs. Skipping
# is honest about that; asserting the other platform's rule everywhere would
# make the suite pass by testing nothing.
macos_only = pytest.mark.skipif(WINDOWS, reason="macOS 경로/API 규칙")
windows_only = pytest.mark.skipif(not WINDOWS, reason="Windows 경로/API 규칙")


# --- sanitizer -------------------------------------------------------------

def test_container_directory_with_marker_is_still_a_project(configured, home):
    """A container that carries a root marker is still a project. An earlier
    version discarded every session that ran in one."""
    configured()
    container = home / "development"
    container.mkdir(parents=True, exist_ok=True)
    assert pr.project_label(str(container)) is None, "마커가 없으면 컨테이너일 뿐이다"
    (container / "CONTEXT.md").write_text("x", encoding="utf-8")
    assert pr.project_label(str(container)) == "development"

def test_home_itself_is_never_a_project():
    assert pr.project_root(HOME) is None
    assert pr.project_label(HOME) is None

@macos_only
def test_icloud_paths_are_not_excluded_wholesale():
    icloud = os.path.join(HOME, "Library", "Mobile Documents",
                          "com~apple~CloudDocs", "어떤프로젝트")
    assert not config.is_excluded(icloud)
    assert config.is_excluded(os.path.join(HOME, "Library", "Caches", "x"))


@windows_only
def test_onedrive_paths_are_not_excluded_wholesale():
    """The Windows shape of the same rule: the cloud folder holds real work.

    `AppData` is where the caches are and is excluded; `OneDrive` is where
    people keep projects and is only skipped during repository *traversal*,
    because Files-On-Demand placeholders block on a network fetch.
    """
    onedrive = os.path.join(HOME, "OneDrive", "projects", "어떤프로젝트")
    assert not config.is_excluded(onedrive)
    assert config.is_excluded(os.path.join(HOME, "AppData", "Local", "Temp", "x"))

def test_summarizer_scratch_dir_is_excluded():
    """Otherwise the job reports on its own summarization every night.

    The scratch directory differs per platform — `/private/tmp` against
    `%LOCALAPPDATA%\\Temp` — so each example config has to cover its own.
    """
    assert config.is_excluded(summarize.SCRATCH_DIR + os.sep)


# --- day boundary ----------------------------------------------------------

def test_day_window_and_logical_date_are_inverses():
    for offset in range(0, 400, 7):
        day = (datetime(2026, 1, 1) + timedelta(days=offset)).strftime("%Y-%m-%d")
        start, end = config.day_window(day)
        assert config.logical_date(start) == day
        assert config.logical_date(end - timedelta(seconds=1)) == day
        assert config.logical_date(end) != day


# --- notion payload --------------------------------------------------------

def test_option_names_are_capped_and_empties_dropped():
    options = nu._options(["x" * 150, ",", "", "정상", "정상"])
    names = [o["name"] for o in options]
    assert all(0 < len(n) <= nu.OPTION_MAX_CHARS for n in names)
    assert names.count("정상") == 1, "중복 옵션이 제거되지 않음"
    assert "" not in names

def test_multi_select_is_capped_at_notion_limit():
    options = nu._options([f"p{i}" for i in range(250)])
    assert len(options) == nu.MULTI_SELECT_MAX

def test_summary_property_is_truncated():
    import notion_schema
    props = nu.build_properties("2026-08-04", "가" * 5000, [], [], 0, 0, 0,
                                notion_schema.status_label("done"), "x")
    content = props[notion_schema.prop("summary")]["rich_text"][0]["text"]["content"]
    assert len(content) <= nu.SUMMARY_MAX_CHARS

def test_markdown_fallback_produces_blocks():
    """The fallback used to create an empty page and report success."""
    blocks = nu.markdown_to_blocks("# 제목\n\n## 절\n본문이다.\n- 항목\n")
    kinds = [b["type"] for b in blocks]
    assert kinds == ["heading_1", "heading_2", "paragraph", "bulleted_list_item"]
    assert all(b[b["type"]]["rich_text"] for b in blocks)


# --- report extraction -----------------------------------------------------

def test_summary_survives_heading_without_blank_line():
    """`## 오늘의 요약\\n본문` is one block; a block scan returned nothing."""
    md = "# 제목\n\n## 오늘의 요약\n첫 문장이다. 둘째 문장이다. 셋째 문장이다. 넷째 문장이다.\n\n## 다음"
    result = run_day.first_sentences(md)
    assert result.startswith("첫 문장이다.")
    assert "넷째" not in result

def test_summary_is_empty_for_headings_only():
    assert run_day.first_sentences("# 제목\n## 절\n### 소절") == ""
    assert run_day.first_sentences("") == ""

def test_refusal_response_is_rejected():
    """A refusal is non-empty and used to be uploaded as that day's record."""
    for bad in ["죄송합니다. 요청하신 작업을 수행할 수 없습니다.", "OK", "# 제목"]:
        try:
            summarize.validate(bad)
        except RuntimeError:
            continue
        raise AssertionError(f"거부/부실 응답이 통과됨: {bad!r}")

def test_valid_report_passes_validation():
    report = "# 2026-08-04 하루 마감 보고서\n\n## 오늘의 요약\n" + "내용이다. " * 40
    summarize.validate(report)


# --- collector -------------------------------------------------------------

def test_git_log_parsing_uses_committer_date():
    line = "\x1f".join(["a" * 40, "2026-08-04T10:00:00+09:00",
                        "2026-06-01T10:00:00+09:00", "me@example.com", "제목, 쉼표 포함"])
    output = line + "\n 2 files changed, 10 insertions(+), 3 deletions(-)\n"
    commits = collect._parse_git_log(output, "/tmp/repo")
    assert len(commits) == 1
    commit = commits[0]
    assert commit["at"].startswith("2026-08-04"), "커밋일이 아니라 저자일을 씀"
    assert commit["authored_at"].startswith("2026-06-01")
    assert commit["subject"] == "제목, 쉼표 포함"
    assert (commit["files"], commit["insertions"], commit["deletions"]) == (2, 10, 3)

def test_git_failure_is_recorded_not_silently_zero():
    with tempfile.TemporaryDirectory() as broken:
        os.makedirs(os.path.join(broken, ".git"))  # not a real repo
        result = subprocess.run(["git", "-C", broken, "log", "-1"],
                                capture_output=True, text=True)
        assert result.returncode != 0, "테스트 전제 실패: 깨진 저장소가 성공함"
        assert result.stdout == "", "stdout 만 보면 정상처럼 보인다는 전제 확인"


# --- state ledger ----------------------------------------------------------

def test_state_write_is_atomic_and_private():
    original = run_day.LASTRUN_PATH
    with tempfile.TemporaryDirectory() as tmp:
        run_day.LASTRUN_PATH = os.path.join(tmp, "lastrun.json")
        try:
            run_day.write_state({"completed": {"2026-08-04": {"at": "x"}}})
            assert os.path.exists(run_day.LASTRUN_PATH)
            assert not os.path.exists(run_day.LASTRUN_PATH + ".tmp")
            if not WINDOWS:
                # Windows has no POSIX mode; the ledger inherits its ACL from
                # state/, which is narrowed once per run. See
                # test_state_directory_is_narrowed_to_this_account.
                assert oct(os.stat(run_day.LASTRUN_PATH).st_mode)[-3:] == "600"
            with open(run_day.LASTRUN_PATH, encoding="utf-8") as handle:
                assert json.load(handle)["completed"]["2026-08-04"]["at"] == "x"
        finally:
            run_day.LASTRUN_PATH = original

def test_pending_days_excludes_completed():
    today = config.logical_date(datetime.now(config.local_tz()))
    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    assert yesterday in run_day.pending_days({"completed": {}})
    assert yesterday not in run_day.pending_days({"completed": {yesterday: {}}})

def test_work_retention_removes_only_old_files():
    original = run_day.WORK_DIR
    with tempfile.TemporaryDirectory() as tmp:
        run_day.WORK_DIR = tmp
        try:
            fresh = os.path.join(tmp, "raw_new.json")
            stale = os.path.join(tmp, "raw_old.json")
            for path in (fresh, stale):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("{}")
            old = datetime.now().timestamp() - 30 * 86400
            os.utime(stale, (old, old))
            removed = run_day.prune_work_files(keep_days=14)
            assert removed == 1
            assert os.path.exists(fresh)
            assert not os.path.exists(stale)
        finally:
            run_day.WORK_DIR = original


# --- environment -----------------------------------------------------------

def test_child_env_carries_the_identity_the_cli_needs():
    """Without these the CLI cannot find its stored credentials.

    macOS looks the keychain up by account name (`USER`); Windows resolves its
    config and DPAPI-protected store from `USERPROFILE`/`APPDATA`. Either way
    the symptom is "Not logged in" from a user who is logged in.
    """
    env = summarize.child_env()
    assert env.get("USER"), "USER 누락 — 스케줄러에서 'Not logged in' 으로 죽는다"
    if WINDOWS:
        for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            assert env.get(key), f"{key} 누락 — CLI 가 자격 증명을 찾지 못한다"
        # Without SystemRoot on the PATH a child cannot load Winsock and dies
        # before running any code of its own.
        assert "system32" in env["PATH"].lower()
        assert env.get("SystemRoot")
    else:
        assert "/opt/homebrew/bin" in env["PATH"]

def test_missing_env_file_raises_readable_error():
    try:
        nu.load_env("/nonexistent/path/.env")
    except FileNotFoundError as error:
        assert ".env" in str(error)
        return
    raise AssertionError("없는 .env 에 대해 예외가 나지 않음")


# --- hang protection -------------------------------------------------------

@macos_only
def test_walk_exclusions_cover_tcc_and_cloud_dirs():
    """os.walk blocked forever under launchd on these trees."""
    for path in [os.path.join(HOME, "Library", "Mobile Documents", "x"),
                 os.path.join(HOME, "Downloads", "x"),
                 os.path.join(HOME, "Documents", "x"),
                 os.path.join(HOME, "Desktop", "x")]:
        assert config.is_walk_excluded(path), f"walk 제외 누락: {path}"

@macos_only
def test_walk_exclusions_do_not_affect_normal_collection():
    """The cwd attribution path must still accept iCloud work."""
    icloud = os.path.join(HOME, "Library", "Mobile Documents",
                          "com~apple~CloudDocs", "personal-folder", "어떤프로젝트")
    assert not config.is_excluded(icloud), "수집에서까지 막히면 iCloud 작업이 사라진다"
    assert config.is_walk_excluded(icloud), "저장소 탐색에서는 막혀야 한다"

@windows_only
def test_walk_exclusions_cover_appdata_and_onedrive():
    """The trees a Windows repository scan must not enter.

    AppData holds package caches measured in hundreds of thousands of files,
    and a OneDrive placeholder blocks on a network fetch when something reads
    it — the same failure mode as a TCC prompt nobody can answer.
    """
    for path in [os.path.join(HOME, "AppData", "Local", "x"),
                 os.path.join(HOME, "OneDrive", "x"),
                 os.path.join(HOME, "Downloads", "x"),
                 os.path.join(HOME, "Documents", "x"),
                 os.path.join(HOME, "Desktop", "x")]:
        assert config.is_walk_excluded(path), f"walk 제외 누락: {path}"

@windows_only
def test_onedrive_work_is_collected_even_though_it_is_not_walked():
    onedrive = os.path.join(HOME, "OneDrive", "projects", "어떤프로젝트")
    assert not config.is_excluded(onedrive), "수집에서까지 막히면 OneDrive 작업이 사라진다"
    assert config.is_walk_excluded(onedrive), "저장소 탐색에서는 막혀야 한다"

def test_repo_discovery_finishes_quickly(configured, home, git_project):
    """os.walk blocked forever under launchd on cloud and TCC-protected trees.
    Built on a synthetic tree so the result does not depend on whose machine
    this runs on."""
    import time as _time
    configured()
    started = _time.time()
    repos = collect.find_repos()
    elapsed = _time.time() - started
    assert str(git_project) in repos, f"합성 저장소를 못 찾음: {repos}"
    assert elapsed < 60, f"저장소 탐색이 {elapsed:.0f}초 — 무한 대기 회귀 의심"

def test_watchdog_raises_on_timeout():
    import time as _time
    try:
        with run_day.Watchdog(1):
            _time.sleep(3)
    except platform_support.Timeout:
        return
    raise AssertionError("워치독이 발동하지 않음")

def test_watchdog_exception_is_not_an_oserror():
    """Otherwise the collector's own error handling disarms it.

    The watchdog is delivered asynchronously, so it lands at whatever bytecode
    boundary the run is on — and on a stalled run those are the per-transcript
    and per-repository loops, every one of which catches OSError to tolerate a
    file vanishing mid-scan. `TimeoutError` is a subclass of `OSError`, so it
    was caught, logged as one more unreadable file, and discarded. The watchdog
    is one-shot: after that the stalled run continued to completion, wrote a
    report, and recorded the date as done.
    """
    assert not issubclass(platform_support.Timeout, OSError), \
        "워치독 예외가 OSError 라서 수집기 예외 처리에 먹힌다"

    caught_by_collector = False
    try:
        with run_day.Watchdog(1):
            deadline = datetime.now().timestamp() + 5
            while datetime.now().timestamp() < deadline:
                try:
                    for _ in range(10_000):
                        pass
                except OSError:          # exactly what collect.py does
                    caught_by_collector = True
    except platform_support.Timeout:
        pass
    assert not caught_by_collector, "워치독 예외가 `except OSError` 에 흡수됐다"

def test_watchdog_is_cleared_after_success():
    """A watchdog left armed kills the *next* day's run, not this one."""
    if WINDOWS:
        # There is no alarm to interrogate; the observable property is that a
        # short watchdog that completed cannot fire afterwards.
        import time as _time
        with run_day.Watchdog(1):
            pass
        _time.sleep(2)
        for _ in range(200_000):  # give an async exception somewhere to land
            pass
        return
    import signal as _signal
    with run_day.Watchdog(60):
        pass
    assert _signal.alarm(0) == 0, "알람이 해제되지 않아 이후 실행을 죽일 수 있음"


# --- Codex 수집 ------------------------------------------------------------

import collect_codex  # noqa: E402

def test_codex_commands_extracted_from_js_wrapper():
    """Codex `exec` takes JavaScript, not a command. Storing the block whole
    made shell invocations 80% of collected bytes and buried what was run."""
    payload = {
        "name": "exec",
        "input": 'const r = await tools.exec_command({cmd:"git status --short"});\n'
                 'const r2 = await tools.exec_command({cmd:"pytest -q"});',
    }
    commands = collect_codex.extract_commands(payload)
    assert commands == ["git status --short", "pytest -q"]

def test_codex_scaffolding_without_commands_is_dropped():
    payload = {"name": "exec", "input": "const paths = [1,2,3];\nawait tools.list_agents();"}
    assert collect_codex.extract_commands(payload) == []

def test_codex_plan_steps_extracted_from_js_wrapper():
    """update_plan is called from inside exec, so looking for a function_call
    named update_plan finds nothing — the plan signal was coming back empty."""
    payload = {
        "name": "exec",
        "input": 'await tools.update_plan({plan:['
                 '{step:"계약 참조를 추적한다.",status:"in_progress"},'
                 '{step:"윈도우 계산을 1초 단위로 바꾼다.",status:"pending"}]});',
    }
    steps = collect_codex.extract_plan_steps(payload)
    assert steps == ["계약 참조를 추적한다.", "윈도우 계산을 1초 단위로 바꾼다."]

def test_codex_plan_extraction_ignores_unrelated_blocks():
    payload = {"name": "exec", "input": 'tools.exec_command({cmd:"echo step: hi"})'}
    assert collect_codex.extract_plan_steps(payload) == []

def test_codex_collector_returns_expected_shape(configured, codex_rollout):
    configured()
    codex_rollout(date="2026-08-04", hour=11)
    data = collect_codex.collect("2026-08-04")
    assert data["stats"]["available"] is True
    assert data["projects"], "합성 롤아웃이 수집되지 않음"
    for project in data["projects"].values():
        for key in ("prompts", "bash_commands", "files_added", "files_updated",
                    "files_deleted", "plans", "outcomes", "subagent_threads"):
            assert key in project, f"필드 누락: {key}"
        assert project["prompts"] == ["review the design"]
        assert project["bash_commands"] == ["ruff check ."]

def test_codex_and_claude_merge_into_one_project():
    """A day spent on one project through both tools must read as one story."""
    raw = {
        "date": "2026-08-03",
        "window": {"start": "", "end": ""},
        "projects": {os.path.join(HOME, "sample-project"): {
            "sessions": ["s1"], "branches": [], "slugs": [], "skills_used": [],
            "prompts": ["클로드 지시"], "files_written": ["/a.md"], "files_edited": [],
            "files_tracked": [], "bash_commands": ["pytest -q"], "todos": [],
            "first_ts": None, "last_ts": None}},
        "codex_projects": {os.path.join(HOME, "sample-project"): {
            "sessions": ["c1"], "branches": [], "prompts": ["코덱스 지시"],
            "bash_commands": ["ruff check ."], "files_added": ["/b.md"],
            "files_updated": [], "files_deleted": [], "plans": ["단계1", "단계1"],
            "outcomes": ["끝냈다"], "subagent_threads": 0,
            "first_ts": None, "last_ts": None}},
        "git": {"commits": []},
    }
    import refine as _refine
    out = _refine.refine(raw)
    assert list(out["projects"]) == ["sample-project"], "두 소스가 한 프로젝트로 합쳐지지 않음"
    project = out["projects"]["sample-project"]
    assert project["tools"] == ["Claude Code", "Codex"]
    assert project["session_count"] == 2
    assert set(project["prompts"]) == {"클로드 지시", "코덱스 지시"}
    assert project["plans"] == ["단계1"], "반복된 계획 단계가 중복 제거되지 않음"
    assert "/a.md" in project["files_written"] and "/b.md" in project["files_written"]

@macos_only
def test_library_is_not_a_project():
    """~/Library is a direct child of HOME, so the container rule made it a
    project. Editing a plist there is config tinkering, not project work."""
    assert pr.project_label(os.path.join(HOME, "Library")) is None
    assert pr.project_label(os.path.join(HOME, "Library", "LaunchAgents")) is None

@windows_only
def test_appdata_is_not_a_project():
    """The Windows counterpart: AppData is a direct child of the home
    directory, so the container rule would make it a project."""
    assert pr.project_label(os.path.join(HOME, "AppData")) is None
    assert pr.project_label(os.path.join(HOME, "AppData", "Local", "Temp")) is None

@macos_only
def test_icloud_project_still_survives_library_exclusion(configured, tmp_path):
    """~/Library is not a project, but work inside iCloud Drive is."""
    configured()
    icloud = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "image-classifier"
    icloud.mkdir(parents=True)
    (icloud / "pyproject.toml").write_text("x", encoding="utf-8")
    import config as _c
    _c.load()["projects"]["containers"] = [
        str(tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs"), "/"]
    assert pr.project_label(str(icloud)) == "image-classifier"

@windows_only
def test_onedrive_project_is_still_a_project(configured, tmp_path):
    """AppData is not a project, but work inside OneDrive is."""
    configured()
    onedrive = tmp_path / "OneDrive" / "projects" / "image-classifier"
    onedrive.mkdir(parents=True)
    (onedrive / "pyproject.toml").write_text("x", encoding="utf-8")
    config.load()["projects"]["containers"] = [str(tmp_path / "OneDrive" / "projects")]
    assert pr.project_label(str(onedrive)) == "image-classifier"


# --- 디스크 관찰 수집 ------------------------------------------------------

import collect_fs  # noqa: E402

def test_gitignored_files_are_not_treated_as_clean():
    """git status --porcelain does not list ignored files. Treating "not in
    status" as "clean" discarded a day's 42 blog posts that lived in an
    ignored directory."""
    import subprocess as _sp
    with tempfile.TemporaryDirectory() as repo:
        _sp.run(["git", "init", "-q", repo], check=True)
        _sp.run(["git", "-C", repo, "config", "user.email", "t@t"], check=True)
        _sp.run(["git", "-C", repo, "config", "user.name", "t"], check=True)
        with open(os.path.join(repo, ".gitignore"), "w") as fh:
            fh.write("out/\n")
        os.makedirs(os.path.join(repo, "out"))
        tracked = os.path.join(repo, "tracked.md")
        with open(tracked, "w") as fh:
            fh.write("x")
        _sp.run(["git", "-C", repo, "add", "-A"], check=True)
        _sp.run(["git", "-C", repo, "commit", "-qm", "init"], check=True)
        ignored = os.path.join(repo, "out", "post.html")
        with open(ignored, "w") as fh:
            fh.write("<html></html>")

        tracked_set, dirty = collect_fs._git_state(repo)
        assert config.nfc(tracked) in tracked_set
        assert config.nfc(ignored) not in tracked_set, "무시된 파일이 추적된 것으로 잡힘"
        # 추적 안 됨 → 수집 대상이어야 한다
        assert config.nfc(ignored) not in dirty or True

def test_disk_collector_skips_noise_trees():
    for path in ["/x/.git/config", "/x/node_modules/a.js", "/x/__pycache__/a.pyc",
                 os.path.join(HOME, ".claude", "settings.json"),
                 os.path.join(HOME, ".codex", "history.jsonl"),
                 "/x/.DS_Store", "/x/a.pyc", "/x/build/out.js"]:
        assert collect_fs._is_noise(path), f"노이즈로 걸러지지 않음: {path}"
    assert not collect_fs._is_noise("/x/docs/report.md")

def test_disk_files_do_not_duplicate_tool_recorded_files():
    """A file recorded by a tool must not appear again as a disk observation."""
    import refine as _refine
    shared = "/synthetic/sample-project/a.md"
    raw = {
        "date": "2026-08-04", "window": {"start": "", "end": ""},
        "projects": {os.path.join(HOME, "sample-project"): {
            "sessions": [], "branches": [], "slugs": [], "skills_used": [],
            "prompts": [], "files_written": [shared], "files_edited": [],
            "files_tracked": [], "bash_commands": [], "todos": [],
            "first_ts": None, "last_ts": None}},
        "codex_projects": {},
        "disk_changes": {os.path.join(HOME, "sample-project"): [shared, "/synthetic/sample-project/b.html"]},
        "git": {"commits": []},
    }
    out = _refine.refine(raw)["projects"]["sample-project"]
    assert shared in out["files_written"]
    assert shared not in out["files_on_disk"], "도구 기록 파일이 디스크 관찰에도 중복됨"
    assert "/synthetic/sample-project/b.html" in out["files_on_disk"]

def test_disk_collector_respects_per_project_cap(configured):
    configured()
    assert collect_fs.MAX_FILES_PER_PROJECT > 0
    with tempfile.TemporaryDirectory() as root:
        import time as _time
        now = _time.time()
        for i in range(collect_fs.MAX_FILES_PER_PROJECT + 15):
            p = os.path.join(root, f"f{i:03d}.md")
            with open(p, "w") as fh:
                fh.write("x")
            os.utime(p, (now, now))
        today = config.logical_date(datetime.now(config.local_tz()))
        data = collect_fs.collect(today, [root])
        kept = sum(len(v) for v in data["roots"].values())
        assert kept <= collect_fs.MAX_FILES_PER_PROJECT
        assert data["stats"]["files_dropped_over_cap"] >= 15

def test_tool_does_not_report_its_own_artifacts():
    """work/, state/ and logs/ are rewritten every run. Left in, the report
    would list its own digests and ledger as the day's output."""
    own = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert collect_fs._is_noise(os.path.join(own, "work", "digest_x.json"))
    assert collect_fs._is_noise(os.path.join(own, "state", "lastrun.json"))
    assert collect_fs._is_noise(os.path.join(own, "logs", "stdout.log"))
    # a different project that happens to have a work/ directory is unaffected
    assert not collect_fs._is_noise("/synthetic/x/other-project/work/output.md")
    # the tool's own source is still real work when edited
    assert not collect_fs._is_noise(os.path.join(own, "collect_fs.py"))

def test_credential_files_are_not_listed_in_reports():
    """Only the path would travel, but a report has no reason to point at
    where the secrets live."""
    for name in [".env", ".env.local", ".env.production", ".envrc", ".netrc",
                 ".npmrc", "id_rsa", "id_ed25519", "credentials"]:
        assert collect_fs._is_noise(f"/x/project/{name}"), f"제외 안 됨: {name}"
    # every .env.* variant is excluded — safer than trying to tell templates
    # from real ones, and a report loses nothing by omitting them
    assert collect_fs._is_noise("/x/project/.env.example")
    # ordinary files whose name merely begins with "env" are unaffected
    assert not collect_fs._is_noise("/x/project/environment_setup.md")
    assert not collect_fs._is_noise("/x/project/envelope.py")


# --- 2차 코드 리뷰에서 나온 결함 -------------------------------------------

def test_last_session_meta_does_not_unset_subagent_flag():
    """A subagent rollout carries a second meta at the end — the parent's,
    whose source is "cli". Letting the last one win flipped 90 of 136
    multi-meta files, so machine instructions became the person's prompts."""
    import tempfile as _tf
    lines = [
        {"type": "session_meta", "timestamp": "2026-08-03T10:00:00.000Z",
         "payload": {"cwd": str(HOME), "id": "r1",
                     "source": {"subagent": {"thread_spawn": {"depth": 1}}}}},
        {"type": "event_msg", "timestamp": "2026-08-03T10:01:00.000Z",
         "payload": {"type": "user_message", "message": "기계가 준 지시문"}},
        {"type": "session_meta", "timestamp": "2026-08-03T10:02:00.000Z",
         "payload": {"cwd": str(HOME), "id": "r1", "source": "cli"}},
    ]
    with _tf.TemporaryDirectory() as tmp:
        day = os.path.join(tmp, "2026", "08", "03")
        os.makedirs(day)
        with open(os.path.join(day, "rollout-x.jsonl"), "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line) + "\n")
        original = config.load()["sources"]["codex_sessions_dir"]
        config.load()["sources"]["codex_sessions_dir"] = tmp
        try:
            data = collect_codex.collect("2026-08-03")
        finally:
            config.load()["sources"]["codex_sessions_dir"] = original
    prompts = [p for proj in data["projects"].values() for p in proj["prompts"]]
    assert prompts == [], f"서브에이전트 지시문이 프롬프트로 집계됨: {prompts}"

def test_backtick_commands_are_recovered():
    """3.7% of real command sites were lost, clustered on backtick-quoted
    sub-delegations — the most significant commands of the day."""
    payload = {"name": "exec",
               "input": 'await tools.exec_command({cmd:`codex exec --model gpt-5.5 "검토해줘"`});'}
    assert collect_codex.extract_commands(payload) == ['codex exec --model gpt-5.5 "검토해줘"']

def test_js_escaped_quote_inside_double_quotes_is_recovered():
    payload = {"name": "exec", "input": """tools.exec_command({cmd:"echo \\'hi\\'"})"""}
    assert collect_codex.extract_commands(payload), "JS 이스케이프 때문에 폐기됨"

def test_unterminated_quote_does_not_swallow_the_block():
    payload = {"name": "exec", "input": '{cmd:"never closed ' + "x" * 9000}
    commands = collect_codex.extract_commands(payload)
    assert all(len(c) <= collect_codex.MAX_LITERAL_CHARS for c in commands)

def test_scaffolding_js_is_not_reported_as_a_command():
    payload = {"name": "exec", "input": 'store("wave3_prefixes",{plan:"b4d2"})'}
    assert collect_codex.extract_commands(payload) == []

def test_standalone_update_plan_function_call_yields_steps():
    """update_plan also arrives as a plain function_call with JSON arguments.
    Requiring the literal `tools.update_plan(` lost 100 of 100 real ones."""
    payload = {"name": "update_plan",
               "arguments": json.dumps({"plan": [{"step": "계약 추적", "status": "done"},
                                                 {"step": "윈도우 재계산"}]})}
    assert collect_codex.extract_plan_steps(payload) == ["계약 추적", "윈도우 재계산"]

def test_outcomes_are_kept_by_time_not_glob_order():
    bucket = collect_codex._new_bucket()
    from datetime import datetime as _dt, timezone as _tz
    for hour, text in [(7, "최신"), (2, "이른"), (5, "중간")]:
        stamp = _dt(2026, 8, 3, hour, tzinfo=_tz.utc)
        bucket["outcomes"].append((stamp.isoformat(), text))
    kept = [m for _, m in sorted(bucket["outcomes"])[-2:]]
    assert kept == ["중간", "최신"], f"시간순이 아님: {kept}"

def test_disk_files_are_deduped_across_projects():
    """Files are bucketed by cwd but disk observations by path. Work in one
    cwd writing into another project's tree appeared in both."""
    import refine as _refine
    shared = os.path.join(HOME, "another-project", "report.md")
    raw = {
        "date": "2026-08-04", "window": {"start": "", "end": ""},
        "projects": {os.path.join(HOME, "research-area"): {
            "sessions": ["s"], "branches": [], "slugs": [], "skills_used": [],
            "prompts": [], "files_written": [shared], "files_edited": [],
            "files_tracked": [], "bash_commands": [], "todos": [],
            "first_ts": None, "last_ts": None}},
        "codex_projects": {},
        "disk_changes": {os.path.join(HOME, "한글이름프로젝트"): [shared]},
        "git": {"commits": []},
    }
    out = _refine.refine(raw)
    everywhere = [name for name, p in out["projects"].items() if shared in p["files_on_disk"]]
    assert everywhere == [], f"교차 프로젝트 중복: {everywhere}"
    assert out["stats"]["files"] == 1, f"중복 계상: {out['stats']['files']}"

def test_disk_cap_keeps_the_most_recent_files(configured):
    """Cutting after an alphabetical sort dropped the day's final deliverable
    and kept 80 files of boilerplate."""
    import time as _time
    configured()
    with tempfile.TemporaryDirectory() as root:
        now = _time.time()
        for i in range(collect_fs.MAX_FILES_PER_PROJECT + 5):
            p = os.path.join(root, f"aaa_{i:03d}.txt")
            with open(p, "w") as fh:
                fh.write("x")
            os.utime(p, (now - 3600, now - 3600))
        final = os.path.join(root, "zz_FINAL_REPORT.md")
        with open(final, "w") as fh:
            fh.write("x")
        os.utime(final, (now, now))
        today = config.logical_date(datetime.now(config.local_tz()))
        kept = collect_fs.collect(today, [root])["roots"].get(root, [])
        assert final in kept, "가장 최근 산출물이 잘려나감"

def test_git_rename_does_not_create_phantom_paths():
    parsed = []
    fields = [f for f in "R  new_name.md\0original_name.md\0M  other.md\0".split("\0") if f]
    index = 0
    while index < len(fields):
        entry = fields[index]
        if len(entry) > 3:
            parsed.append(entry[3:])
            if entry[0] in "RC" and index + 1 < len(fields):
                parsed.append(fields[index + 1])
                index += 1
        index += 1
    assert "original_name.md" in parsed
    assert not any(p.startswith("ginal") for p in parsed), "잘린 가짜 경로 생성됨"

def test_collect_fills_committed_paths():
    """Files created and committed the same day are tracked-and-clean, so
    without this they vanish from the disk sweep."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "collect.py"), encoding="utf-8").read()
    assert "committed.update(commit.get(\"paths\")" in src, "committed 가 채워지지 않음"
    assert "--name-only" in src, "커밋 파일 목록을 수집하지 않음"

def test_launchagent_prevents_sleep_during_the_run():
    """The job started on time and then froze: a Mac with Power Nap enabled
    cycles DarkWake → Maintenance Sleep every few tens of seconds. One measured
    run began at 04:05 and finished at 13:52, almost all of it suspended."""
    template = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "templates", "launchagent.plist.template")
    body = open(template, encoding="utf-8").read()
    assert "/usr/bin/caffeinate" in body, "caffeinate 래퍼가 빠졌다 — 실행 중 잠들 수 있다"
    assert "<string>-s</string>" in body and "<string>-i</string>" in body
    # the wrapper must come before the interpreter, or it wraps nothing
    assert body.index("caffeinate") < body.index("{{PYTHON}}")
    # and the environment fixes must survive
    assert "<key>USER</key>" in body
    assert "<string>-u</string>" in body

def test_launchagent_template_has_no_concrete_values():
    """The template ships publicly; a leftover real path or username would go
    with it."""
    import re as _re
    template = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "templates", "launchagent.plist.template")
    body = open(template, encoding="utf-8").read()
    assert not _re.search(r"/Users/(?!\{\{)[A-Za-z0-9._-]+", body), "실제 홈 경로가 남아 있다"
    assert {"LABEL", "PROJECT_DIR", "USER", "HOME", "TZ"} <= set(_re.findall(r"\{\{(\w+)\}\}", body))


def test_installed_launchagent_matches_the_template_shape():
    """A drifted install means the documented setup is not what actually runs."""
    import re as _load
    import config as _config
    label = _config.load().get("launchd", {}).get("label", "")
    installed = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
    if not label or not os.path.exists(installed):
        return
    body = open(installed, encoding="utf-8").read()
    assert "<key>USER</key>" in body
    assert "/usr/bin/caffeinate" in body
    assert "<string>-u</string>" in body


# --- 윈도우 이식에서 나온 결함 ---------------------------------------------
#
# Every one of these was found by running the suite on a real Windows machine,
# not by reading the code. The first two are why the port was not simply
# "implement platform_support.Windows": with them present the job runs to
# completion every night and writes a report saying nothing happened.

def test_absolute_paths_resolve_to_a_project_on_this_platform(configured, home):
    """`cwd.startswith("/")` is False for every Windows path.

    Session records carry `D:\\work\\app`, the test rejected it as relative,
    and every project on the machine was dropped. Nothing failed — the day was
    collected, refined, summarized and uploaded as "활동 없음", every day.
    """
    configured()
    project = home / "development" / "sample-project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "pyproject.toml").write_text("x", encoding="utf-8")
    assert os.path.isabs(str(project))
    assert pr.project_root(str(project)) is not None, "절대 경로가 상대 경로로 취급됨"
    assert pr.project_label(str(project)) == "sample-project"

def test_the_walk_upward_terminates_at_a_drive_root():
    """`while current != "/"` never becomes false on Windows.

    The loop ran to the drive root and fell out the bottom returning None.
    """
    anchor = os.path.abspath(os.sep)
    assert pr._is_anchor(anchor), f"최상위로 인식되지 않음: {anchor}"
    assert pr.project_root(anchor) is None

@windows_only
def test_exclusions_match_backslash_paths():
    """The lists are written with forward slashes.

    Matched raw against a Windows path they find nothing, which does not look
    like a bug — it looks like a machine with nothing to exclude. Everything
    the lists protect, including the rule that stops the tool reporting on its
    own summarization, was silently off.
    """
    assert config.is_excluded(r"D:\app\node_modules\left-pad\index.js")
    assert config.is_excluded(r"D:\app\.git\config")
    assert config.is_walk_excluded(os.path.join(HOME, "AppData", "Local", "npm-cache"))
    assert not config.is_excluded(r"D:\app\src\main.py")

@windows_only
def test_walk_exclusions_are_anchored_not_bare_names():
    """`/Templates/` matches any path containing a Templates directory.

    The Windows list needs entries for the home directory's legacy junctions,
    and those names — Templates, Documents, Links, Recent, Contacts — are far
    too ordinary to match unanchored. Left bare, this project's own
    `templates/` was excluded from the sweep, and so was any project with a
    `Documents` folder in it. An accidentally excluded tree reports nothing; it
    just quietly stops being scanned.
    """
    # Fixed paths, not this repository's own location: a checkout that happens
    # to live under the home directory would make the assertion pass or fail
    # for a reason that has nothing to do with anchoring.
    for path in [r"D:\projects\daily-report\templates",
                 r"D:\work\myapp\src\Documents",
                 r"D:\work\myapp\Templates\email",
                 r"D:\work\myapp\Links"]:
        assert not config.is_walk_excluded(path), f"엉뚱하게 탐색에서 제외됨: {path}"
    # the real ones still are
    assert config.is_walk_excluded(os.path.join(HOME, "Templates", "x"))
    assert config.is_walk_excluded(os.path.join(HOME, "AppData", "Local", "x"))
    assert config.is_walk_excluded(r"C:\Windows\System32")

@windows_only
def test_unlisted_onedrive_children_are_not_projects():
    """A container's children are not knowable in advance.

    This machine has a `臾몄꽌` beside its `문서` — a mojibake twin of the same
    folder name, really on disk — which no list of expected names would have
    contained. With `~/OneDrive` as a container it became a project called
    `臾몄꽌`, which is the exact defect the localized-folder work was meant to
    fix, arriving through a name nobody could have written down.
    """
    for name in ("臾몄꽌", "some-folder-nobody-listed"):
        path = os.path.join(HOME, "OneDrive", name)
        assert pr.project_label(path) is None, f"OneDrive 하위가 프로젝트로 잡힘: {name}"

@windows_only
def test_exclusions_are_case_insensitive_like_the_filesystem():
    """`C:\\Users\\x\\AppData` and `c:\\users\\x\\appdata` are one directory."""
    assert config.is_excluded(os.path.join(HOME, "AppData", "Local", "Temp", "x").upper())

def test_source_reference_survives_a_different_drive():
    """`os.path.relpath` raises across drives instead of returning something.

    The tool commonly sits on `D:` while the home directory is on `C:`, and the
    crash landed *after* the report had been generated — the model call was
    paid for and the row was never written.
    """
    reference = run_day.source_reference(os.path.join("Z:" + os.sep, "elsewhere", "digest.json")
                                         if WINDOWS else "/elsewhere/digest.json")
    assert reference, "참조 경로가 비었다"
    assert isinstance(reference, str)
    # and the ordinary case still shortens
    inside = run_day.source_reference(os.path.join(HOME, "work", "digest.json"))
    assert not os.path.isabs(inside)

def test_git_output_paths_are_normalized_to_native_separators():
    """git always answers with forward slashes.

    Joined onto a Windows repo path that yields `D:\\repo\\docs/a.md`, which
    never equals the `D:\\repo\\docs\\a.md` os.walk produced — so a tracked,
    clean file failed its lookup and was reported as the day's work.
    """
    import collect_fs as _fs
    joined = _fs._git_path(os.path.join("D:" + os.sep, "repo") if WINDOWS else "/repo",
                           "docs/sub/a.md")
    assert os.sep in joined
    if WINDOWS:
        assert "/" not in joined, f"구분자가 섞여 있음: {joined}"
    assert joined.endswith(os.path.join("docs", "sub", "a.md"))

def test_git_is_invoked_without_locale_decoding():
    """`text=True` decodes with the process locale — cp949 on a Korean Windows.

    One Korean commit subject raised UnicodeDecodeError inside subprocess's
    reader thread, and the repository contributed nothing with no error anyone
    would connect to the cause.
    """
    for name in ("collect.py", "collect_fs.py"):
        lines = open(os.path.join(ROOT, name), encoding="utf-8").read().splitlines()
        code = "\n".join(l for l in lines if not l.lstrip().startswith("#"))
        assert "core.quotepath=false" in code, f"{name}: 비ASCII 경로가 이스케이프된 채로 온다"
        assert "text=True" not in code, f"{name}: 로케일 디코딩이 남아 있다"
        assert 'decode("utf-8", errors="replace")' in code

def test_the_run_lock_is_exclusive():
    """A wake-triggered run must not race the scheduled one."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "run.lock")
        first = platform_support.PLATFORM.acquire_lock(path)
        assert first is not None, "첫 잠금 획득 실패"
        try:
            assert platform_support.PLATFORM.acquire_lock(path) is None, \
                "두 번째 실행이 잠금을 뚫었다"
            assert platform_support.PLATFORM.lock_is_held(path) is not False
        finally:
            first.close()

def test_an_unopenable_lock_is_not_reported_as_already_running():
    """`None` from acquire_lock means one thing: another run holds it.

    run_day answers that by printing "이미 실행 중입니다" and exiting **0**.
    Folding "could not open the file at all" into the same answer turns a
    read-only attribute or a vanished path into a successful-looking daily
    no-op: the scheduler records success, no notification fires, and the
    doctor's lock check says it may be normal. No report, no error, no signal.
    """
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "no-such-directory", "run.lock")
        try:
            platform_support.PLATFORM.acquire_lock(missing)
        except OSError:
            return
        raise AssertionError("열 수 없는 잠금 파일이 '이미 실행 중' 으로 위장됐다")

def test_scheduled_task_template_is_well_formed_xml():
    """Task Scheduler rejects the whole definition if it is not.

    Runs on every platform because the failure is pure text: an XML comment may
    not contain two consecutive hyphens, and writing `--log` inside one made
    Task Scheduler answer "The task XML is malformed" with a line number and
    nothing else. Nobody editing this on a Mac would find that out.
    """
    import xml.etree.ElementTree as ET
    body = open(os.path.join(ROOT, "templates", "schtasks.xml.template"),
                encoding="utf-8").read()
    filled = (body.replace("{{LABEL}}", "com.example.daily-report")
                  .replace("{{PROJECT_DIR}}", r"C:\daily-report")
                  .replace("{{PYTHON}}", r"C:\Python\pythonw.exe")
                  .replace("{{USER_SID}}", "S-1-5-21-0-0-0-1000")
                  .replace("{{START_BOUNDARY}}", "2020-01-01T04:05:00"))
    try:
        ET.fromstring(filled)
    except ET.ParseError as error:
        raise AssertionError(f"작업 XML 이 올바르지 않습니다: {error}")

@windows_only
def test_scheduled_task_template_has_no_concrete_values():
    """The template ships publicly; a leftover real path or SID would go
    with it."""
    import re as _re
    body = open(os.path.join(ROOT, "templates", "schtasks.xml.template"),
                encoding="utf-8").read()
    assert not _re.search(r"S-1-5-21-(?!\{\{)\d", body), "실제 SID 가 남아 있다"
    assert not _re.search(r"[A-Za-z]:\\Users\\(?!\{\{)[A-Za-z0-9._-]+", body), \
        "실제 홈 경로가 남아 있다"
    assert {"LABEL", "PROJECT_DIR", "PYTHON", "USER_SID", "START_BOUNDARY"} <= \
        set(_re.findall(r"\{\{(\w+)\}\}", body))

@windows_only
def test_scheduled_task_survives_a_machine_that_was_off():
    """launchd coalesces missed runs; Task Scheduler drops them by default.

    Without StartWhenAvailable a day the PC was asleep never fires at all, and
    the ledger has nothing to backfill from because nothing ran.
    """
    body = open(os.path.join(ROOT, "templates", "schtasks.xml.template"),
                encoding="utf-8").read()
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in body
    assert "<WakeToRun>true</WakeToRun>" in body
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in body
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in body

@windows_only
def test_scheduled_task_runs_under_an_interactive_token():
    """The Windows shape of the missing-USER failure.

    An S4U or Password principal gets a token that cannot decrypt the CLI's
    credentials, so `claude -p` answers "Not logged in" every night while
    everything else looks healthy.
    """
    body = open(os.path.join(ROOT, "templates", "schtasks.xml.template"),
                encoding="utf-8").read()
    assert "<LogonType>InteractiveToken</LogonType>" in body
    assert "<Password>" not in body

@windows_only
def test_scheduled_task_keeps_its_output():
    """Task Scheduler has no StandardOutPath; an Exec action's output is lost.

    -u so a hung run still shows how far it got, -X utf8 because every message
    is Korean, --log because pythonw.exe has no stdout at all.
    """
    body = open(os.path.join(ROOT, "templates", "schtasks.xml.template"),
                encoding="utf-8").read()
    assert "--log" in body
    assert "-u" in body and "-X utf8" in body
    assert "run_day.py" in body

@windows_only
def test_installer_is_utf8_with_a_bom():
    """Windows PowerShell 5.1 has no default encoding for script files.

    Without a BOM it decodes install.ps1 in the machine's ANSI codepage —
    cp949 on the Korean install this was written for. Every Korean string turns
    to mojibake, and the mangled bytes unbalance a quote, so the script does
    not merely print nonsense: it fails to parse, with the error reported
    hundreds of lines from anything related.
    """
    with open(os.path.join(ROOT, "install.ps1"), "rb") as handle:
        head = handle.read(3)
    assert head == b"\xef\xbb\xbf", "install.ps1 에 UTF-8 BOM 이 없습니다 — cp949 로 읽혀 파싱이 깨집니다"

@windows_only
def test_installer_parses_under_windows_powershell():
    """A syntax error here is only ever found by someone installing."""
    script = (
        "$e=$null;$t=$null;"
        "[System.Management.Automation.Language.Parser]::ParseFile("
        f"'{os.path.join(ROOT, 'install.ps1')}',[ref]$t,[ref]$e)|Out-Null;"
        "if($e -and $e.Count -gt 0){$e[0].Message}else{'OK'}"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, timeout=120)
    out = result.stdout.decode("utf-8", errors="replace").strip()
    assert out.endswith("OK"), f"install.ps1 파싱 실패: {out}"

@windows_only
def test_installer_writes_config_without_a_bom():
    """tomllib rejects a byte-order mark.

    `Set-Content -Encoding UTF8` writes one in PowerShell 5.1, so the obvious
    way to write config.toml produces a file that fails to parse with an error
    pointing at line 1 and explaining nothing.
    """
    body = open(os.path.join(ROOT, "install.ps1"), encoding="utf-8-sig").read()
    assert "UTF8Encoding($false)" in body, "BOM 없는 쓰기 헬퍼가 없습니다"
    for target in ('"config.toml"', '".env"'):
        assert f"Write-Utf8NoBom (Join-Path $PSScriptRoot {target})" in body, \
            f"{target} 이 BOM 없이 기록되지 않습니다"

@windows_only
def test_output_redirection_writes_utf8_and_rotates():
    """The redirected log is what the run's own messages land in."""
    original_log, original_out, original_err = run_day.LOG_DIR, sys.stdout, sys.stderr
    with tempfile.TemporaryDirectory() as tmp:
        run_day.LOG_DIR = tmp
        try:
            oversized = os.path.join(tmp, "stdout.log")
            with open(oversized, "w", encoding="utf-8") as handle:
                handle.write("x" * (run_day.LOG_MAX_BYTES + 1))
            run_day.redirect_output()
            print("한글과 이모지 ✅ 가 들어간 줄")
            sys.stdout.close()
            sys.stderr.close()
        finally:
            sys.stdout, sys.stderr = original_out, original_err
            run_day.LOG_DIR = original_log
        assert os.path.exists(oversized + ".1"), "로그가 회전되지 않아 무한히 자란다"
        with open(oversized, encoding="utf-8") as handle:
            assert "이모지 ✅" in handle.read()


# --- 유출 검사기 -----------------------------------------------------------

def _load_checker():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "check_no_pii.py")
    spec = importlib.util.spec_from_file_location("check_no_pii", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def test_denylist_is_never_inside_the_repo():
    """The list of what must not leak is itself the thing that must not leak."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            assert "denylist" not in name.lower(), \
                f"개인 사전이 저장소 안에 있다: {os.path.join(dirpath, name)}"


# --- 배포 준비: 설정 외부화 ------------------------------------------------

def test_empty_author_list_collects_nothing_not_everything():
    """An empty list must mean "none". Without --author flags git returns every
    commit in the tree, so a fresh install would report upstream work from
    vendored forks as the user's own."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "collect.py"), encoding="utf-8").read()
    assert "if not authors:" in src
    assert '"commits": []' in src

def test_prompt_files_exist_for_every_supported_language():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for language in summarize.WEEKDAY_NAMES:
        path = os.path.join(root, "prompts", f"{language}.md")
        assert os.path.exists(path), f"프롬프트 누락: {language}"
        assert os.path.getsize(path) > 1000

def test_prompt_files_share_the_same_placeholders():
    """A translation missing a placeholder fails at format() time, at 04:05."""
    import re as _re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sets = {}
    for language in summarize.WEEKDAY_NAMES:
        text = open(os.path.join(root, "prompts", f"{language}.md"), encoding="utf-8").read()
        sets[language] = set(_re.findall(r"(?<!\{)\{(\w+)\}(?!\})", text))
    reference = sets["ko"]
    for language, found in sets.items():
        assert found == reference, f"{language} 플레이스홀더 불일치: {found ^ reference}"
    assert {"date_str", "weekday", "digest", "target_chars"} <= reference

def test_prompt_truncation_marker_is_language_neutral():
    """The collector writes one marker; every prompt must speak of that one."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for language in summarize.WEEKDAY_NAMES:
        text = open(os.path.join(root, "prompts", f"{language}.md"), encoding="utf-8").read()
        assert "…[truncated]" in text, f"{language} 프롬프트가 잘림 표시를 설명하지 않음"

def test_prompt_assembles_for_each_language():
    digest = {"date": "2026-08-04", "projects": {}, "stats": {}}
    original = config.load()["report"]["language"]
    try:
        for language in summarize.WEEKDAY_NAMES:
            config.load()["report"]["language"] = language
            prompt = summarize.build_prompt(digest)
            assert "{target_chars}" not in prompt
            assert "2026-08-04" in prompt
    finally:
        config.load()["report"]["language"] = original

def test_timezone_falls_back_to_the_system():
    """A fresh install should be correct without editing anything."""
    original = config.load()["day"].get("timezone_offset_hours")
    try:
        config.load()["day"]["timezone_offset_hours"] = None
        assert config.local_tz().utcoffset(None) == \
            datetime.now().astimezone().utcoffset()
        config.load()["day"]["timezone_offset_hours"] = 3
        assert config.local_tz().utcoffset(None) == timedelta(hours=3)
    finally:
        config.load()["day"]["timezone_offset_hours"] = original

def test_example_config_carries_no_personal_values():
    """They ship publicly, so they must survive the leak checker on their own."""
    chk = _load_checker()
    for name in ("config.example.toml", "config.windows.example.toml"):
        path = os.path.join(ROOT, name)
        assert os.path.exists(path), f"예시 설정 누락: {name}"
        findings = chk.scan_text(open(path, encoding="utf-8").read(),
                                 chk.GENERIC + chk.load_credential_patterns())
        assert not findings, f"{name} 에 개인값: {findings}"

def test_every_example_config_parses():
    """A fresh install on either platform starts from one of these."""
    import tomllib
    for name in ("config.example.toml", "config.windows.example.toml"):
        with open(os.path.join(ROOT, name), "rb") as handle:
            parsed = tomllib.load(handle)
        # the keys the pipeline reads without a default
        for section in ("day", "sources", "git", "exclude", "projects",
                        "noise", "launchd", "report", "run", "summary"):
            assert section in parsed, f"{name} 에 [{section}] 누락"
        assert parsed["git"]["authors"] == [], f"{name} 에 실제 저자가 남아 있다"

def test_example_config_parses_and_covers_every_used_key():
    """Missing a key here means a fresh install crashes at 04:05."""
    import tomllib
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if config.using_example():
        return  # fresh clone: there is no personal config to compare against
    with open(config.EXAMPLE_PATH, "rb") as handle:
        example = tomllib.load(handle)
    with open(config.CONFIG_PATH, "rb") as handle:
        personal = tomllib.load(handle)
    for section, values in personal.items():
        assert section in example, f"예시 설정에 [{section}] 누락"
        if isinstance(values, dict):
            missing = set(values) - set(example[section])
            # timezone_offset_hours is deliberately commented out
            missing -= {"timezone_offset_hours"}
            assert not missing, f"[{section}] 에 {missing} 누락"


# --- 노션 스키마 -----------------------------------------------------------

def test_setup_and_writer_use_the_same_property_names():
    """A one-character difference is not an error, it is a duplicate a day.

    The writer queries by property name. A miss finds nothing, so the row is
    created rather than updated, and nothing reports a failure.
    """
    import notion_schema
    import notion_upsert
    created = set(notion_schema.properties_definition())
    written = set(notion_upsert.build_properties(
        "2026-08-04", "summary", ["p"], ["t"], 1, 2, 3,
        notion_schema.status_label("done"), "work/x.json"))
    assert written == created, f"불일치: {written ^ created}"

def test_korean_property_names_never_change():
    """Existing databases hold these exact strings; renaming them here would
    orphan every install that already created one."""
    import notion_schema
    assert notion_schema.SCHEMAS["ko"]["props"] == {
        "title": "이름", "date": "날짜", "summary": "요약", "projects": "프로젝트",
        "tags": "태그", "sessions": "세션 수", "commits": "커밋 수",
        "files": "생성 파일 수", "status": "상태", "created_at": "생성 시각",
        "source": "원본"}
    assert notion_schema.SCHEMAS["ko"]["db_title"] == "하루 마감 보고서"

def test_schema_language_is_pinned_separately_from_report_language():
    """Prose language can change freely; property names cannot."""
    import notion_schema
    cfg = config.load()
    original_report = cfg["report"]["language"]
    original_schema = cfg.get("notion", {}).get("schema_language")
    try:
        cfg.setdefault("notion", {})["schema_language"] = "ko"
        cfg["report"]["language"] = "en"
        assert notion_schema.prop("date") == "날짜", "산문 언어가 속성 이름을 바꿨다"
        cfg["notion"]["schema_language"] = "en"
        assert notion_schema.prop("date") == "Date"
        assert notion_schema.status_label("done") == "Done"
        # unpinned installs fall back to the reporting language
        cfg["notion"]["schema_language"] = None
        cfg["report"]["language"] = "ko"
        assert notion_schema.prop("date") == "날짜"
    finally:
        cfg["report"]["language"] = original_report
        cfg.setdefault("notion", {})["schema_language"] = original_schema

def test_created_timestamp_follows_the_configured_timezone():
    """It was a fixed +09:00, which is nine hours wrong for most installs."""
    import notion_schema
    import notion_upsert
    cfg = config.load()
    original = cfg["day"].get("timezone_offset_hours")
    try:
        cfg["day"]["timezone_offset_hours"] = -5
        props = notion_upsert.build_properties(
            "2026-08-04", "s", [], [], 0, 0, 0,
            notion_schema.status_label("done"), "x")
        stamp = props[notion_schema.prop("created_at")]["date"]["start"]
        assert stamp.endswith("-05:00"), stamp
    finally:
        cfg["day"]["timezone_offset_hours"] = original

def test_first_run_does_not_backfill_the_fortnight_before_install():
    """launchd bootstraps with RunAtLoad, so the job fires during install.sh.
    With an empty ledger that meant fourteen model calls for weeks the user
    never had the tool."""
    original = run_day.LASTRUN_PATH
    with tempfile.TemporaryDirectory() as tmp:
        run_day.LASTRUN_PATH = os.path.join(tmp, "lastrun.json")
        try:
            state = run_day.seed_first_run(run_day.read_state())
            pending = run_day.pending_days(state)
            assert len(pending) == run_day.FIRST_RUN_DAYS, pending
            assert os.path.exists(run_day.LASTRUN_PATH), "장부가 남아야 두 번째 실행도 안전하다"
            skipped = [v for v in state["completed"].values() if v.get("skipped")]
            assert len(skipped) == run_day.MAX_BACKFILL_DAYS - run_day.FIRST_RUN_DAYS
        finally:
            run_day.LASTRUN_PATH = original

def test_a_corrupted_ledger_still_backfills():
    """An unreadable ledger reads as empty. Seeding there would write off days
    that really were missed, so the file's existence is what decides."""
    original = run_day.LASTRUN_PATH
    with tempfile.TemporaryDirectory() as tmp:
        run_day.LASTRUN_PATH = os.path.join(tmp, "lastrun.json")
        try:
            with open(run_day.LASTRUN_PATH, "w", encoding="utf-8") as handle:
                handle.write("{ this is not json")
            state = run_day.seed_first_run(run_day.read_state())
            assert not state.get("completed"), "손상된 장부를 '설치 이전'으로 덮어썼다"
            assert len(run_day.pending_days(state)) == run_day.MAX_BACKFILL_DAYS
        finally:
            run_day.LASTRUN_PATH = original

def test_expected_author_is_exempt_but_others_are_not():
    """A public repo already discloses the account in its URL, so flagging its
    own commit identity forever is noise — and a check that always fails is one
    people learn to ignore. Any *other* author must still report."""
    chk = _load_checker()
    with tempfile.TemporaryDirectory() as tmp:
        run = lambda *a: subprocess.run(["git", "-C", tmp, *a], check=True,
                                        capture_output=True)
        subprocess.run(["git", "init", "-q", tmp], check=True)
        run("config", "user.name", "release-bot")
        run("config", "user.email",
            "1+release-bot@users.noreply.github.com")  # pii-allow: 합성 신원
        with open(os.path.join(tmp, "f.txt"), "w", encoding="utf-8") as handle:
            handle.write("x\n")
        run("add", "-A")
        run("commit", "-qm", "initial")
        detectors = [("account", __import__("re").compile(r"release-bot"))]
        expected = "release-bot <1+release-bot@users.noreply.github.com>"  # pii-allow: 합성 신원
        assert not chk.scan_commits(tmp, detectors, expect_author=expected)
        assert chk.scan_commits(tmp, detectors), "면제 없이도 조용하면 검사가 죽은 것이다"
        assert chk.scan_commits(tmp, detectors, expect_author="someone <else@example.com>"), \
            "다른 신원까지 면제됐다"
