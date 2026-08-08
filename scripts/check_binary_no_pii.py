"""Scan a built artifact for identifiers from the machine that built it.

`scripts/check_no_pii.py` reads source. This reads *binaries*, because a
PyInstaller bundle is not only the source: compiled modules keep the absolute
path they were compiled from in `co_filename`, the bootloader records the build
directory, and anything the build touched can end up embedded. A repository can
be clean and the executable built from it still carry the builder's account
name to everyone who downloads it.

Searched in UTF-8, UTF-16LE and Latin-1, because a path can appear in any of
them inside the same file.

**It only sees uncompressed bytes, and that is a real limit.** Pointed at an
Inno Setup installer — whose payload is a solid LZMA2 stream — it cannot find
anything and will report clean whatever the contents. Measured: the same marker
is found in `dist/`, found in an installer built with `Compression=none`, and
invisible in one built with the shipped `lzma2`. So scan the **staged directory
before it is compressed**, which is what CI does; scanning the compiled
installer is not a substitute and is not treated as one.

    python scripts/check_binary_no_pii.py dist/daily-report
    python scripts/check_binary_no_pii.py dist/setup.exe --extra "myname"

Exits non-zero if anything is found. Terms come from the machine it runs on —
account name, user profile, computer name — plus whatever `--extra` adds, so
it needs no dictionary and keeps none.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

# Values are never printed back in full: a leak checker that echoes what it
# found is a second copy of the leak.
def mask(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}…{value[-1]}({len(value)})"


def local_terms(extra: list[str]) -> list[tuple[str, str]]:
    """(label, term) pairs worth failing over, drawn from this machine."""
    home = os.path.expanduser("~")
    candidates = [
        ("account", os.environ.get("USERNAME") or os.environ.get("USER") or ""),
        ("home", home),
        ("home-basename", os.path.basename(home)),
        ("computer", os.environ.get("COMPUTERNAME") or ""),
        ("domain", os.environ.get("USERDOMAIN") or ""),
    ]
    candidates += [("extra", term) for term in extra]

    seen, terms = set(), []
    for label, value in candidates:
        value = (value or "").strip()
        # Two characters is not an identifier; it is a substring of everything.
        if len(value) < 3 or value.lower() in seen:
            continue
        seen.add(value.lower())
        terms.append((label, value))
    return terms


ENCODINGS = ("utf-8", "utf-16-le", "latin-1")


def scan_file(path: str, terms: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError:
        return []

    findings = []
    for label, term in terms:
        for encoding in ENCODINGS:
            try:
                needle = term.encode(encoding)
            except UnicodeEncodeError:
                continue
            if not needle:
                continue
            # case-insensitive for the single-byte forms; paths vary in case
            haystack = blob.lower() if encoding != "utf-16-le" else blob
            probe = needle.lower() if encoding != "utf-16-le" else needle
            if probe in haystack:
                findings.append((label, encoding, term))
                break
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="빌드 산출물 개인정보 검사")
    parser.add_argument("target", help="검사할 파일 또는 디렉터리")
    parser.add_argument("--extra", action="append", default=[],
                        help="추가로 찾을 문자열 (실명 등). 여러 번 지정 가능")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    terms = local_terms(args.extra)
    if not terms:
        print("검사할 식별자를 찾지 못했습니다", file=sys.stderr)
        return 2

    files = []
    if os.path.isfile(args.target):
        files = [args.target]
    else:
        for dirpath, _, filenames in os.walk(args.target):
            files += [os.path.join(dirpath, name) for name in filenames]

    if not args.quiet:
        print(f"검사 대상: {len(files)}개 파일, 식별자 {len(terms)}종 "
              f"({', '.join(label for label, _ in terms)})")

    findings: dict[str, list[tuple[str, str, str]]] = {}
    for path in files:
        hits = scan_file(path, terms)
        if hits:
            # relpath raises across drives, and this line is only reached when
            # something was found — so the success path never crashed and the
            # finding path did, replacing the report with a traceback.
            base = args.target if os.path.isdir(args.target) else os.path.dirname(path)
            try:
                label = os.path.relpath(path, base)
            except ValueError:
                label = path
            findings[label] = hits

    if not findings:
        print("✅ 빌드 산출물에서 이 기기의 식별자를 찾지 못했습니다")
        return 0

    print(f"\n❌ {len(findings)}개 파일에서 발견 — 배포 전에 해결해야 합니다:")
    for path, hits in sorted(findings.items()):
        for label, encoding, term in hits:
            print(f"  {label:<14} {encoding:<10} {mask(term)}  {path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
