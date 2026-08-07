"""Turn a refined digest into a human-readable daily report via headless Claude Code.

Four things here are not optional, each learned by watching it fail:

  - The child environment is **built, not inherited**, and the identity keys
    must be in it. Credentials live in the macOS login keychain under the
    account name, so without `USER` the CLI reports "Not logged in" even though
    the user is logged in; on Windows the same role is played by `USERPROFILE`
    and `APPDATA`. Both schedulers pass almost no environment, so a job that
    inherits would hit this every night. The keys per platform live in
    `platform_support`.
  - Timeouts are enforced in Python. macOS ships no `timeout` binary.
  - Both pipes are UTF-8 explicitly. `text=True` encodes stdin and decodes
    stdout with the *process locale* — cp949 on a Korean Windows install —
    and the prompt is a Korean digest measured in hundreds of kilobytes.
  - The target date is injected into the prompt. The job may run after
    midnight or during a backfill, so the model must never infer "today" from
    a clock it cannot see.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import config
import platform_support

REQUIRED_PATH = platform_support.PLATFORM.default_path()


def claude_argv() -> list[str]:
    """How to start the CLI on this machine.

    A native Windows install is an `.exe` under `~/.local/bin` that is not on
    PATH for a scheduled task, and an npm install is a `.cmd` shim that
    CreateProcess cannot start directly. `summary.claude_bin` overrides both
    when the CLI lives somewhere else entirely.
    """
    configured = config.load().get("summary", {}).get("claude_bin", "") or ""
    return platform_support.PLATFORM.claude_argv(configured)

# The summarizer is itself a Claude Code session, so it writes a transcript of
# its own. Running it from a scratch directory puts that transcript under an
# excluded path, which stops the job from reporting on itself every night —
# while still letting real work on this project show up in the report.
SCRATCH_DIR = platform_support.PLATFORM.scratch_dir("daily-report-summarizer")

# A refusal or a stray sentence is not a report. Anything shorter than this, or
# without a heading, is treated as a failed run rather than uploaded.
MIN_REPORT_CHARS = 200

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

# Weekday names per language. The prompt is a document, not code, so it lives
# in prompts/<lang>.md — an English user changes one config line, not the
# source.
WEEKDAY_NAMES = {
    "ko": ["월", "화", "수", "목", "금", "토", "일"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}


def load_prompt_template(language: str) -> str:
    path = os.path.join(PROMPTS_DIR, f"{language}.md")
    if not os.path.exists(path):
        available = sorted(f[:-3] for f in os.listdir(PROMPTS_DIR) if f.endswith(".md"))
        raise RuntimeError(
            f"프롬프트 파일이 없습니다: {path} (사용 가능: {', '.join(available)})")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def child_env() -> dict[str, str]:
    """Environment for the CLI child process.

    Built explicitly rather than inherited, so a scheduled run behaves the same
    as a terminal run. Which keys are load-bearing differs by platform, so the
    set itself comes from there.
    """
    return platform_support.PLATFORM.child_env()


def scratch_dir() -> str:
    os.makedirs(SCRATCH_DIR, exist_ok=True)
    return SCRATCH_DIR


def preflight() -> tuple[bool, str]:
    """Confirm the CLI is reachable and authenticated before spending a run."""
    argv = claude_argv()
    try:
        result = subprocess.run(
            [*argv, "-p", "--max-turns", "1"],
            env=child_env(), cwd=scratch_dir(), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            input="reply with exactly: OK", timeout=120)
    except OSError:
        # FileNotFoundError on macOS; on Windows a `.cmd` shim started directly
        # fails with WinError 193 instead, which is the same problem.
        return False, f"{argv[-1]} 를 찾을 수 없습니다 (PATH: {REQUIRED_PATH})"
    except subprocess.TimeoutExpired:
        return False, "인증 확인이 120초 안에 끝나지 않았습니다"
    combined = (result.stdout or "") + (result.stderr or "")
    if "Not logged in" in combined:
        return False, "로그인되어 있지 않습니다 (USER 환경변수 누락일 가능성이 높습니다)"
    if result.returncode != 0:
        return False, f"exit {result.returncode}: {combined.strip()[:200]}"
    return True, "ok"


def build_prompt(digest: dict) -> str:
    from datetime import datetime
    report = config.load().get("report", {})
    language = report.get("language", "ko")
    date_str = digest["date"]
    names = WEEKDAY_NAMES.get(language, WEEKDAY_NAMES["en"])
    weekday = names[datetime.strptime(date_str, "%Y-%m-%d").weekday()]
    return load_prompt_template(language).format(
        date_str=date_str, weekday=weekday,
        target_chars=report.get("target_chars", 5000),
        digest=json.dumps(digest, ensure_ascii=False, indent=1))


def summarize(digest: dict) -> str:
    cfg = config.load()["summary"]
    prompt = build_prompt(digest)
    try:
        # The prompt goes on stdin, not argv. A busy day already produces
        # ~175 KB and macOS caps a whole argv at 1 MB, so argv would start
        # failing outright on heavy days; it is also readable by any
        # same-uid process through `ps`.
        result = subprocess.run(
            [*claude_argv(), "-p", "--max-turns", str(cfg["max_turns"])],
            env=child_env(), cwd=scratch_dir(), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            input=prompt, timeout=cfg["model_timeout_sec"])
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"요약이 {cfg['model_timeout_sec']}초 안에 끝나지 않았습니다")
    except OSError as error:
        raise RuntimeError(f"{claude_argv()[-1]} 를 실행하지 못했습니다: {error}")

    # in-run failures surface on stdout, not stderr — check both
    combined = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"claude -p exit {result.returncode}: {combined.strip()[:300]}")
    text = (result.stdout or "").strip()
    if not text:
        raise RuntimeError(f"요약이 비어 있습니다: {combined.strip()[:300]}")

    report = strip_preamble(text)
    validate(report)
    return report


def validate(report: str) -> None:
    """Reject anything that is not actually a report.

    A refusal ("죄송합니다, …") or a stray sentence is non-empty and would
    otherwise be published as that day's record, overwriting nothing but
    standing in for a day that never got summarized.
    """
    if not any(line.startswith("# ") for line in report.splitlines()):
        raise RuntimeError(f"보고서에 제목이 없습니다 (거부 응답 가능성): {report[:200]}")
    if len(report) < MIN_REPORT_CHARS:
        raise RuntimeError(f"보고서가 너무 짧습니다 ({len(report)}자): {report[:200]}")


def strip_preamble(text: str) -> str:
    """Drop any chatty lead-in before the first markdown heading."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("# "):
            return "\n".join(lines[index:]).strip()
    return text


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: summarize.py <digest.json> <report.md>", file=sys.stderr)
        return 2
    if "--preflight" in sys.argv:
        ok, message = preflight()
        print(f"preflight: {message}")
        return 0 if ok else 1

    with open(sys.argv[1], encoding="utf-8") as handle:
        digest = json.load(handle)
    report = summarize(digest)
    with open(sys.argv[2], "w", encoding="utf-8") as handle:
        handle.write(report + "\n")
    print(f"{digest['date']}: 보고서 {len(report):,}자 → {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
