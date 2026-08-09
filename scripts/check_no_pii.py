"""Refuse to publish anything that carries personal information.

Runs in three places, each seeing a different amount:

  - the public repo's CI and pre-commit hook — generic patterns only
  - the export step in the private repo — generic patterns plus a private
    dictionary of the maintainer's own identifiers
  - by hand before a release

The dictionary is deliberately not in the public repo. Listing "my name, my
clients, my workspace" is exactly the information the check exists to protect,
so shipping the list would turn the safeguard into the leak. It lives outside
the tree and the checker simply does less when it is absent.

Findings are reported redacted. A CI log or a pasted issue is itself a place
where this can escape, so the report says what kind of thing was found and
where, never the value.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

# Placeholder shapes that are supposed to appear in a distributable repo.
# Checked before anything else so examples do not read as leaks.
# Each of these must span the *whole* matched value, not a fragment of it —
# `noreply@` alone does not cover `noreply@anthropic.com`, so the finding
# would survive an allowance meant to cover it.
ALLOWED = (
    re.compile(r"\b[\w.+-]+@(?:example|test|invalid)\.(?:com|org|net)\b"),
    re.compile(r"\b(?:you|your[-_.]?\w*|my|me|test|foo|bar|user|username)@[\w.-]+\b"),
    re.compile(r"\b[\w.+-]*noreply@[\w.-]+\b"),
    re.compile(r"\{\{[A-Z_]+\}\}"),
    re.compile(r"<[a-z][a-z-]*>"),
    re.compile(r"\bYOUR_[A-Z_]+"),
    re.compile(r"/(?:Users|home)/(?:username|user|you|me|USER|\{\{[A-Z_]+\}\}|<[^>]+>)"),
    re.compile(r"\bcom\.(?:example|yourname|username|\{\{[A-Z_]+\}\})\.daily-report\b"),
)

# Generic detectors. These hold for any maintainer, so they ship publicly.
GENERIC = [
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("home_path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")),
    ("launchagent_label", re.compile(r"\bcom\.[A-Za-z0-9_-]+\.daily-report\b")),
    ("krn_rrn", re.compile(r"\b\d{6}-[1-4]\d{6}\b")),
    ("krn_phone", re.compile(r"\b01[016789]-\d{3,4}-\d{4}\b")),
    ("notion_id", re.compile(r"\b[0-9a-f]{32}\b")),
    ("notion_uuid", re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
]

BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".mp4",
                   ".woff", ".woff2", ".ttf", ".ico", ".pyc")


# The sanitizer scans prose and shell commands, where over-matching is safe —
# masking one word too many costs nothing. This checker scans source code,
# where the same two heuristics fire on ordinary lines (`token = env.get(...)`,
# a regex that mentions 비밀번호). The precise token shapes are kept.
HEURISTIC_PATTERNS = {"key_assignment", "korean_secret"}


def load_credential_patterns() -> list[tuple[str, re.Pattern]]:
    """Reuse the runtime sanitizer's patterns so the two never drift apart."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (here, os.path.join(here, "src")):
        if os.path.exists(os.path.join(candidate, "sanitize.py")):
            sys.path.insert(0, candidate)
            try:
                import sanitize  # noqa: PLC0415
                return [(f"credential:{k}", p) for k, p in sanitize.PATTERNS
                        if k not in HEURISTIC_PATTERNS]
            except ImportError:
                break
    return []


def denylist_path(path: str | None = None) -> str:
    """Which private dictionary this run would use, or "" if there is none.

    Separate from loading it because *having decided* and *having written
    something down* are different facts, and only the first one belongs in a
    gate. A file with no terms in it is a maintainer who looked at the
    question and answered it; no file at all is one who never saw it.
    """
    candidates = [path] if path else [
        os.environ.get("DAILY_REPORT_PII_DENYLIST"),
        os.path.expanduser("~/.config/daily-report/pii-denylist.txt"),
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return ""


def load_denylist(path: str | None) -> list[tuple[str, re.Pattern]]:
    """Private dictionary of the maintainer's own identifiers.

    One term per line, `#` comments allowed. Absent is normal — the public
    repo has no such file and the check simply covers less.
    """
    found = denylist_path(path)
    if not found:
        return []
    terms = []
    with open(found, encoding="utf-8") as handle:
        for line in handle:
            term = line.split("#", 1)[0].strip()
            if term:
                terms.append(term)
    return [("private_term", re.compile(re.escape(t), re.IGNORECASE))
            for t in terms]


def tracked_files(root: str) -> list[str]:
    """Prefer git's view — untracked scratch files are not being published."""
    try:
        result = subprocess.run(["git", "-C", root, "ls-files"],
                                capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            return [os.path.join(root, f) for f in result.stdout.split("\n") if f]
    except (subprocess.SubprocessError, OSError):
        pass
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in
                       {".git", "node_modules", "__pycache__", ".venv"}]
        out += [os.path.join(dirpath, f) for f in filenames]
    return out


def is_allowed(line: str, start: int, end: int) -> bool:
    """Is this hit part of a deliberate placeholder?"""
    return any(m.start() <= start and m.end() >= end
               for pattern in ALLOWED for m in pattern.finditer(line))


def redact(value: str) -> str:
    """Say what was found without repeating it."""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}…{value[-1]}(len{len(value)})"


ALLOW_LINE = re.compile(r"pii-allow(?::\s*(.+))?")
ALLOW_FILE = re.compile(r"pii-allow-file:\s*(.+)")


def file_exemption(text: str) -> str | None:
    """A whole file may be exempt if it says so near the top, with a reason.

    Used for files that must contain credential-shaped strings to do their
    job — the sanitizer's test fixtures, for instance.
    """
    head = "\n".join(text.splitlines()[:30])
    match = ALLOW_FILE.search(head)
    return match.group(1).strip() if match else None


def scan_text(text: str, detectors: list[tuple[str, re.Pattern]]) -> list[tuple[int, str, str]]:
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if ALLOW_LINE.search(line):
            continue
        for kind, pattern in detectors:
            for match in pattern.finditer(line):
                if is_allowed(line, match.start(), match.end()):
                    continue
                findings.append((number, kind, redact(match.group(0))))
    return findings


def scan_commits(root: str, detectors: list[tuple[str, re.Pattern]],
                 limit: int = 200, expect_author: str = "") -> list[tuple[str, str, str]]:
    """Commit metadata and messages publish too, and cannot be edited later.

    `expect_author` is the one identity the repository is *meant* to publish
    under. Anything committed as that exact author is not reported, because a
    repository hosted at github.com/<account>/ already discloses the account —
    reporting it would be noise, and a check that always fails is a check people
    learn to ignore. Any other author still reports, which is what catches a
    commit accidentally made under a personal address.
    """
    try:
        result = subprocess.run(
            ["git", "-C", root, "log", f"-{limit}", "--format=%H%x1f%an <%ae>%x1f%B%x1e"],
            capture_output=True, text=True, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return []
    if result.returncode != 0:
        return []
    findings = []
    for chunk in result.stdout.split("\x1e"):
        if not chunk.strip():
            continue
        parts = chunk.strip().split("\x1f")
        if len(parts) < 3:
            continue
        sha, author, body = parts[0][:8], parts[1], parts[2]
        targets = [(body, "message")]
        if author.strip() != expect_author.strip():
            targets.append((author, "author"))
        for target, where in targets:
            for _, kind, value in scan_text(target, detectors):
                findings.append((sha, f"{where}:{kind}", value))
    return findings


def check_translation_pairs(root: str, files: list[str]) -> list[str]:
    """Warn when an English document changed but its Korean twin did not.

    Translations drift silently, and a stale translation eventually says
    something the maintainer no longer means.
    """
    warnings = []
    for path in files:
        if not path.endswith(".md") or ".ko." in path:
            continue
        twin = path[:-3] + ".ko.md"
        if not os.path.exists(twin):
            continue
        try:
            def last_commit(p):
                r = subprocess.run(["git", "-C", root, "log", "-1", "--format=%ct", "--", p],
                                   capture_output=True, text=True, timeout=20)
                return int(r.stdout.strip() or 0)
            if last_commit(path) > last_commit(twin):
                warnings.append(f"{os.path.relpath(path, root)} 가 "
                                f"{os.path.relpath(twin, root)} 보다 최근에 바뀌었습니다")
        except (subprocess.SubprocessError, OSError, ValueError):
            continue
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="공개 전 개인정보 유출 검사")
    parser.add_argument("root", nargs="?", default=".", help="검사할 저장소 경로")
    parser.add_argument("--denylist", help="개인 사전 파일 (기본: 환경변수 또는 ~/.config)")
    parser.add_argument("--no-history", action="store_true", help="커밋 히스토리 검사 생략")
    parser.add_argument("--expect-author", default=os.environ.get("DAILY_REPORT_EXPECT_AUTHOR", ""),
                        help="이 저장소가 공개하기로 한 커밋 신원 "  # pii-allow: 아래는 형식 예시
                             "(예: \"name <id+user@users.noreply.github.com>\"). "  # pii-allow: 합성 예시
                             "이 신원의 커밋은 보고하지 않고, 다른 신원은 그대로 보고한다")
    parser.add_argument("--quiet", action="store_true", help="발견된 것만 출력")
    parser.add_argument("--require-denylist", action="store_true",
                        help="개인 사전이 없으면 검사하지 않고 실패한다 "
                             "(비공개 저장소에서 공개본을 내보낼 때 쓴다)")
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    detectors = GENERIC + load_credential_patterns()
    denylist = load_denylist(args.denylist)
    detectors += denylist

    # Absent is normal in the public repo's CI and not normal on the way out
    # of the private one.
    #
    # The generic patterns cannot see a client's name or a project codename —
    # only the private dictionary can, and that dictionary is deliberately not
    # in the tree. So when it is missing this check silently becomes a much
    # weaker one and still exits 0, while the export step printed
    # "유출 검사 (개인 사전 포함)" above it. Somebody reading that line has been
    # told a check ran that did not.
    if args.require_denylist and not denylist_path(args.denylist):
        print("❌ 개인 사전을 찾지 못해 검사를 중단합니다.\n"
              "   일반 패턴은 고객사명·프로젝트명·코드네임을 알지 못합니다.\n"
              "   한 줄에 한 항목으로 만들어 두세요:\n"
              "     ~/.config/daily-report/pii-denylist.txt\n"
              "   또는 DAILY_REPORT_PII_DENYLIST 로 경로를 지정하세요.\n"
              "   정말 일반 패턴만으로 내보내려면 --no-denylist 를 쓰세요.",
              file=sys.stderr)
        return 2

    files = [f for f in tracked_files(root)
             if not f.endswith(BINARY_SUFFIXES) and os.path.isfile(f)]

    if not args.quiet:
        found = denylist_path(args.denylist)
        if denylist:
            dictionary = f" (개인 사전 {len(denylist)}개 포함)"
        elif found:
            # The distinction matters: this run had a dictionary and it was
            # empty, which covers no client name at all. Saying "사전 없음"
            # would hide a decision; saying nothing would hide the weakness.
            dictionary = f" (개인 사전이 비어 있음 — 일반 패턴만: {found})"
        else:
            dictionary = " (개인 사전 없음 — 일반 패턴만)"
        print(f"검사 대상: {len(files)}개 파일, 탐지기 {len(detectors)}종{dictionary}")

    by_kind: dict[str, int] = {}
    file_findings: list[tuple[str, int, str, str]] = []
    exempted: list[tuple[str, str]] = []
    for path in files:
        try:
            text = open(path, encoding="utf-8", errors="strict").read()
        except (OSError, UnicodeDecodeError):
            continue
        reason = file_exemption(text)
        if reason:
            exempted.append((os.path.relpath(path, root), reason))
            continue
        for number, kind, value in scan_text(text, detectors):
            file_findings.append((os.path.relpath(path, root), number, kind, value))
            by_kind[kind] = by_kind.get(kind, 0) + 1

    if exempted and not args.quiet:
        print(f"파일 면제 {len(exempted)}건:")
        for rel, reason in exempted:
            print(f"  {rel}  — {reason}")

    commit_findings = ([] if args.no_history
                       else scan_commits(root, detectors, expect_author=args.expect_author))
    for _, kind, _ in commit_findings:
        by_kind[kind] = by_kind.get(kind, 0) + 1

    if file_findings:
        print(f"\n파일에서 {len(file_findings)}건:")
        shown: dict[str, int] = {}
        for rel, number, kind, value in file_findings:
            shown[kind] = shown.get(kind, 0) + 1
            if shown[kind] <= 3:
                print(f"  {kind:24s} {rel}:{number}  {value}")
        for kind, count in sorted(by_kind.items()):
            if count > 3:
                print(f"  {kind:24s} … 외 {count - 3}건")

    if commit_findings:
        print(f"\n커밋에서 {len(commit_findings)}건 "
              f"(커밋 {len({c[0] for c in commit_findings})}개) "
              f"— 히스토리는 나중에 고칠 수 없습니다:")
        shown = {}
        for sha, kind, value in commit_findings:
            shown[kind] = shown.get(kind, 0) + 1
            if shown[kind] <= 3:
                print(f"  {kind:24s} {sha}  {value}")

    for warning in check_translation_pairs(root, files):
        print(f"\n번역 불일치: {warning}")

    total = len(file_findings) + len(commit_findings)
    print()
    if total:
        print(f"❌ 유출 후보 {total}건 — 공개하기 전에 해결해야 합니다.")
        if not denylist:
            print("   개인 사전이 없어 일반 패턴만 검사했습니다. 내보내기 전에는 사전을 붙이세요.")
        return 1
    print("✅ 탐지 0건")
    if not denylist:
        print("   (개인 사전 없이 검사했습니다 — 공개 저장소 CI 에서는 정상입니다)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
