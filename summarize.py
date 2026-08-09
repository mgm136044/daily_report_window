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


ENGINES = ("claude", "codex")


def engine() -> str:
    """Which CLI writes the report. `summary.engine`, defaulting to Claude.

    A typo here would otherwise be discovered at 04:05 as a KeyError, so an
    unknown name is refused by name with the alternatives listed.
    """
    name = (config.load().get("summary", {}).get("engine") or "claude").strip().lower()
    if name not in ENGINES:
        raise RuntimeError(
            f"[summary] engine 값이 잘못됐습니다: {name!r} "
            f"(가능한 값: {', '.join(ENGINES)})")
    return name


def claude_argv() -> list[str]:
    """How to start the CLI on this machine.

    A native Windows install is an `.exe` under `~/.local/bin` that is not on
    PATH for a scheduled task, and an npm install is a `.cmd` shim that
    CreateProcess cannot start directly. `summary.claude_bin` overrides both
    when the CLI lives somewhere else entirely.
    """
    configured = config.load().get("summary", {}).get("claude_bin", "") or ""
    return platform_support.PLATFORM.claude_argv(configured)


def codex_argv() -> list[str]:
    """The same for Codex, where the override matters more — see
    `platform_support.Windows.codex_argv`: nothing puts `codex` on PATH."""
    configured = config.load().get("summary", {}).get("codex_bin", "") or ""
    return platform_support.PLATFORM.codex_argv(configured)


def engine_argv() -> list[str]:
    return claude_argv() if engine() == "claude" else codex_argv()

# The summarizer is itself a Claude Code session, so it writes a transcript of
# its own. Running it from a scratch directory puts that transcript under an
# excluded path, which stops the job from reporting on itself every night —
# while still letting real work on this project show up in the report.
SCRATCH_DIR = platform_support.PLATFORM.scratch_dir("daily-report-summarizer")

# A refusal or a stray sentence is not a report. Anything shorter than this, or
# without a heading, is treated as a failed run rather than uploaded.
MIN_REPORT_CHARS = 200

import paths  # noqa: E402

# Ships with the tool and is never written, so it follows the resource root —
# which inside a frozen bundle is the extraction directory, not the executable's.
PROMPTS_DIR = paths.resource("prompts")

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
    """Confirm the configured CLI is reachable and authenticated.

    Whichever engine is going to be spent on the run is the one checked —
    checking Claude while Codex does the work would report health about a
    program the job never starts.
    """
    return _preflight_claude() if engine() == "claude" else _preflight_codex()


def _preflight_claude() -> tuple[bool, str]:
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


def _preflight_codex() -> tuple[bool, str]:
    """The same question, asked the way Codex answers it.

    Through `-o` rather than stdout, because that is the path the real run
    takes — a check that exercises a different route can pass while the route
    that matters is broken.
    """
    import tempfile

    argv = codex_argv()
    cfg = config.load().get("summary", {})
    os.makedirs(scratch_dir(), exist_ok=True)

    # Before spending a model call: does this build have the flags at all?
    help_text = codex_help()
    if not help_text:
        return False, (f"{argv[-1]} 를 실행하지 못했습니다 — "
                       f"[summary] codex_bin 에 전체 경로를 넣어 주세요 "
                       f"(PATH 에 codex 심이 없는 설치가 많습니다)")
    missing = codex_missing_flags(help_text)
    if missing:
        reasons = "\n".join(f"  {flag}: {CODEX_REQUIRED_FLAGS[flag]}" for flag in missing)
        return False, (f"설치된 codex 에 필요한 옵션이 없습니다: {', '.join(missing)}\n"
                       f"{reasons}\n"
                       f"codex 를 업데이트하거나 [summary] engine 을 claude 로 두세요.")

    try:
        with tempfile.TemporaryDirectory(dir=scratch_dir()) as work:
            out_path = os.path.join(work, "preflight.md")
            result = subprocess.run(
                codex_command(out_path, cfg),
                env=child_env(), cwd=scratch_dir(), capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                input="reply with exactly: OK", timeout=120)
            answered = os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except OSError:
        return False, (f"{argv[-1]} 를 찾을 수 없습니다 — "
                       f"[summary] codex_bin 에 전체 경로를 넣어 주세요 "
                       f"(PATH 에 codex 심이 없습니다)")
    except subprocess.TimeoutExpired:
        return False, "인증 확인이 120초 안에 끝나지 않았습니다"

    combined = (result.stdout or "") + (result.stderr or "")
    if "not logged in" in combined.lower() or "sign in" in combined.lower():
        return False, "codex 에 로그인되어 있지 않습니다 (`codex login`)"
    if result.returncode != 0:
        return False, f"exit {result.returncode}: {_codex_detail(result)}"
    if not answered:
        return False, f"-o 파일이 비어 있습니다: {_codex_detail(result)}"
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
    text = _run_claude(prompt, cfg) if engine() == "claude" else _run_codex(prompt, cfg)
    report = strip_preamble(text)
    validate(report)
    return report


def _run_claude(prompt: str, cfg: dict) -> str:
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
    return text


def codex_command(out_path: str, cfg: dict) -> list[str]:
    """`codex exec` with the four flags that make it usable unattended.

    Every one of these was settled by running it, not by reading:

      - **`-` puts the prompt on stdin.** Codex will take a prompt as an
        argument and that is not an option here: a busy day's digest is
        ~175 KB and Windows caps an entire command line at 32,767 characters,
        so argv would fail outright rather than degrade.
      - **`-o FILE` is where the answer is read from.** stdout is a session
        log — a banner, the whole prompt echoed back, the answer, a token
        count — so taking the report off stdout would mean parsing prose out
        of a transcript.
      - **`--ephemeral` keeps the run out of `~/.codex/sessions`.** Without it
        the job collects its own summarization every night. Claude gets this
        from running in an excluded scratch directory, but a Codex rollout is
        written regardless of the working directory, so it has to be asked
        for. Verified by counting rollouts before and after: unchanged.
      - **`-s read-only`** — the summarizer has no reason to run anything, and
        `--skip-git-repo-check` because the scratch directory is not a repo.

    `-C` is belt to `--ephemeral`'s braces: should a rollout ever appear, its
    cwd is the scratch directory, which `exclude.paths` already covers.
    """
    command = [*codex_argv(), "exec",
               "--skip-git-repo-check", "-s", "read-only",
               "--color", "never", "--ephemeral",
               "-C", scratch_dir(), "-o", out_path]
    model = (cfg.get("codex_model") or "").strip()
    if model:
        command += ["-m", model]
    return [*command, "-"]


# The flags `codex_command` depends on, and what breaks without each. Checked
# against the installed CLI rather than assumed, because this integration was
# written against one version on one machine — 0.147.0-alpha.6.5 — and a flag
# that a different build does not have makes `codex exec` exit with a usage
# error at 04:05, naming a token rather than the problem.
CODEX_REQUIRED_FLAGS = {
    "-o": "최종 답변을 파일로 받지 못합니다 (stdout 은 세션 로그라 파싱 대상이 아닙니다)",
    "--ephemeral": "요약 세션이 ~/.codex/sessions 에 남아 다음 날 자기 자신을 수집합니다",
    "--skip-git-repo-check": "스크래치 디렉터리가 git 저장소가 아니라 거부됩니다",
    "--color": "ANSI 색 코드가 보고서에 섞일 수 있습니다",
}


def codex_help() -> str:
    """`codex exec --help`, or "" if it cannot be run. Cheap: no model call."""
    try:
        result = subprocess.run([*codex_argv(), "exec", "--help"],
                                env=child_env(), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or "") + (result.stderr or "")


def codex_missing_flags(help_text: str | None = None) -> list[str]:
    """Which required flags this installed Codex does not advertise.

    Asked once, at diagnosis time, so an incompatible CLI is a sentence during
    setup instead of a usage error in the middle of the night. An empty help
    text means the CLI could not be run at all, which is a different failure
    and is reported by the caller that found it.
    """
    text = codex_help() if help_text is None else help_text
    if not text:
        return []
    return [flag for flag in CODEX_REQUIRED_FLAGS if flag not in text]


def _codex_detail(result) -> str:
    """A short reason for a failure, from stderr and stderr only.

    `codex exec` echoes the entire prompt back on stdout, and the prompt is
    the digest. The Claude path quotes `stdout[:300]` and is right to —
    `claude -p` echoes nothing — but here **no slice of stdout is safe**. Not
    the head, obviously; and not the tail either, which is where this first
    landed: the echo runs right up to the answer and only about thirty
    characters of token accounting follow it, so the last 300 are still the
    day's collected material. A test caught that.

    So stdout is never quoted, and an empty stderr says that plainly rather
    than reaching for the one stream it must not touch.
    """
    detail = (result.stderr or "").strip()
    if detail:
        return detail[-300:]
    return ("stderr 가 비어 있습니다 — codex 의 stdout 은 프롬프트를 되받아 찍으므로 "
            "인용하지 않습니다")


def _run_codex(prompt: str, cfg: dict) -> str:
    import tempfile

    os.makedirs(scratch_dir(), exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch_dir()) as work:
        out_path = os.path.join(work, "report.md")
        try:
            result = subprocess.run(
                codex_command(out_path, cfg),
                env=child_env(), cwd=scratch_dir(), capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                input=prompt, timeout=cfg["model_timeout_sec"])
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"요약이 {cfg['model_timeout_sec']}초 안에 끝나지 않았습니다")
        except OSError as error:
            raise RuntimeError(f"{codex_argv()[-1]} 를 실행하지 못했습니다: {error}")

        if result.returncode != 0:
            raise RuntimeError(
                f"codex exec exit {result.returncode}: {_codex_detail(result)}")
        try:
            with open(out_path, encoding="utf-8") as handle:
                text = handle.read().strip()
        except OSError:
            # exit 0 with no file means the flag did not do what it says, which
            # is worth distinguishing from a model that answered with nothing
            raise RuntimeError(
                f"codex 가 -o 파일을 쓰지 않았습니다: {_codex_detail(result)}")
    if not text:
        raise RuntimeError(f"요약이 비어 있습니다: {_codex_detail(result)}")
    return text


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
