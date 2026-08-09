"""Copy the publishable subset of this repository into a clean directory.

Allow-list, not deny-list. "Leave this one out" eventually forgets a file; "put
only these in" cannot, because a new file is not on the list and does not travel.

The cost of an allow-list is the opposite failure — a legitimate new file
silently missing from the release. So every tracked file must be classified as
either allowed or deliberately excluded *with a reason*, and anything
unclassified stops the export. Nothing leaks by accident and nothing vanishes by
accident.

It does not commit, and it does not push. Publishing stays a human action.

    python3 scripts/export_public.py --dest ~/development/daily-report-public
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Everything that ships. Paths are repo-relative; a trailing /* includes a whole
# directory. Listing files individually is the point — it is a decision per file.
ALLOW = [
    "README.md",
    "README.ko.md",
    "LICENSE",
    "install.sh",
    "install.ps1",
    ".gitignore",
    ".gitattributes",
    ".env.example",
    "config.example.toml",
    "config.windows.example.toml",
    "cli.py",
    "gui.py",
    "paths.py",
    "daily-report.spec",
    "installer.iss",
    "packaging/winget/*",
    "scripts/check_binary_no_pii.py",
    "scripts/check_workflow_powershell.py",
    ".github/workflows/*",
    "collect.py",
    "collect_codex.py",
    "collect_fs.py",
    "config.py",
    "doctor.py",
    "notion_schema.py",
    "notion_upsert.py",
    "platform_support.py",
    "project_roots.py",
    "refine.py",
    "run_day.py",
    "sanitize.py",
    "setup_gui.py",
    "setup_notion_db.py",
    "status_window.py",
    "summarize.py",
    "prompts/*",
    "templates/*",
    "skills/daily-report/*",
    ".claude-plugin/*",
    "scripts/check_no_pii.py",
    "scripts/pre-commit",
    "scripts/export_public.py",
    "tests/*",
    "docs/design.md",
    "docs/design.ko.md",
    "docs/notion-setup.md",
    "docs/notion-setup.ko.md",
    # The one development note that ships.
    #
    # The rule below withholds the others because they are work records —
    # client names, project names and dates in every sentence. This one is not:
    # it is the account of what the Windows port cost, written against a public
    # codebase, and README and docs/design.md both link to it. It carries no
    # identifier, which the leak checker confirms rather than the author.
    "docs/development-notes/windows_port_development_note.md",
]

# Deliberately withheld, each with the reason it is withheld. The reason is not
# decoration: it is what a future maintainer reads before overriding it.
EXCLUDE = [
    ("docs/development-notes/*",
     "작업 기록 — 고객사·프로젝트·날짜가 문장 단위로 들어 있다"),
    ("docs/public-release-plan.md",
     "내부 계획 — '무엇이 유출되는가' 목록 자체가 유출 지도다"),
    ("docs/windows-ui-plan.md",
     "미착수 로드맵 — 공개하면 약속이 된다. 단계가 끝나면 그때 ALLOW 로 옮긴다"),
    ("config.toml", "개인 설정 (배포되는 것은 config.example.toml)"),
    (".env", "자격증명"),
    ("com.*.plist", "설치된 LaunchAgent 사본 — 사용자명과 절대경로"),
    ("work/*", "수집 산출물 — 사용자 프롬프트 원문"),
    ("state/*", "실행 장부"),
    ("logs/*", "실행 로그"),
    ("fixtures/*", "실데이터 픽스처"),
    (".DS_Store", "macOS 부산물"),
]


def matches(path: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return pattern
        # "dir/*" should also cover "dir/sub/file"
        if pattern.endswith("/*") and path.startswith(pattern[:-1]):
            return pattern
    return None


def tracked_files() -> list[str]:
    result = subprocess.run(["git", "-C", ROOT, "ls-files"],
                            capture_output=True, text=True, check=True)
    return [line for line in result.stdout.split("\n") if line]


def classify(paths: list[str]) -> tuple[list[str], list[tuple[str, str]], list[str]]:
    allowed, excluded, unknown = [], [], []
    exclude_patterns = [pattern for pattern, _ in EXCLUDE]
    for path in paths:
        if matches(path, ALLOW):
            allowed.append(path)
            continue
        pattern = matches(path, exclude_patterns)
        if pattern:
            reason = next(r for p, r in EXCLUDE if p == pattern)
            excluded.append((path, reason))
            continue
        unknown.append(path)
    return allowed, excluded, unknown


def copy_out(paths: list[str], dest: str) -> int:
    total = 0
    for path in paths:
        source = os.path.join(ROOT, path)
        target = os.path.join(dest, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
        total += os.path.getsize(source)
    return total


def clean_dest(dest: str) -> None:
    """Empty the destination but keep its .git — the public repo's history."""
    for name in os.listdir(dest):
        if name == ".git":
            continue
        path = os.path.join(dest, name)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="공개용 내보내기 (커밋·푸시하지 않음)")
    parser.add_argument("--dest", required=True, help="내보낼 디렉터리")
    parser.add_argument("--no-denylist", action="store_true",
                        help="개인 사전 없이 일반 패턴만으로 검사한다 "
                             "(고객사명·프로젝트명은 검사되지 않는다)")
    parser.add_argument("--force", action="store_true",
                        help="대상이 비어 있지 않아도 진행 (.git 은 보존)")
    args = parser.parse_args()

    dest = os.path.abspath(os.path.expanduser(args.dest))
    if os.path.abspath(dest) == ROOT:
        print("❌ 원본 저장소로는 내보낼 수 없습니다", file=sys.stderr)
        return 1

    allowed, excluded, unknown = classify(tracked_files())

    if unknown:
        print("❌ 분류되지 않은 파일이 있습니다. 넣을지 뺄지 정한 뒤 다시 실행하세요:\n")
        for path in unknown:
            print(f"     {path}")
        print("\n   넣으려면 ALLOW 에, 빼려면 EXCLUDE 에 이유와 함께 추가하세요.")
        return 1

    missing = [p for p in ALLOW if "*" not in p and not os.path.exists(os.path.join(ROOT, p))]
    if missing:
        print(f"❌ 허용 목록에 있으나 존재하지 않는 파일 (이름이 바뀐 것 아닌가요?): {missing}",
              file=sys.stderr)
        return 1

    if os.path.exists(dest) and [n for n in os.listdir(dest) if n != ".git"]:
        if not args.force:
            print(f"❌ 대상이 비어 있지 않습니다: {dest}\n"
                  "   --force 로 비우고 진행할 수 있습니다 (.git 은 보존)", file=sys.stderr)
            return 1
        clean_dest(dest)
    os.makedirs(dest, exist_ok=True)

    total = copy_out(allowed, dest)
    print(f"내보냄: {len(allowed)}개 파일, {total/1024:.0f} KB → {dest}")

    print(f"\n제외 {len(excluded)}개:")
    reasons: dict[str, int] = {}
    for _, reason in excluded:
        reasons[reason] = reasons.get(reason, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {count:3d}개  {reason}")

    # The header used to say "개인 사전 포함" whether or not one existed, and the
    # checker exits 0 when it does not — so the line claimed a check that had
    # not run. This is the one moment private material becomes public, and the
    # generic patterns are exactly the ones that cannot see a client's name.
    print("\n유출 검사:")
    checker = os.path.join(HERE, "check_no_pii.py")
    command = [sys.executable, checker, dest, "--no-history"]
    if not args.no_denylist:
        command.append("--require-denylist")
    result = subprocess.run(command)
    if result.returncode != 0:
        print("\n❌ 검사에서 걸렸습니다. 내보낸 파일은 그대로 두었으니 확인 후 다시 실행하세요.",
              file=sys.stderr)
        return 1

    print(f"""
✅ 내보내기 완료. 커밋과 푸시는 사람이 합니다.

  cd {dest}
  python3 -m pytest tests/ -q             # 내보낸 사본만으로 통과하는지
  git init -b main                        # 히스토리 없이 새로 시작
  git config user.email "<GitHub noreply 주소>"   # 저장소 한정 — 전역 설정을 건드리지 않는다
  git config user.name  "<표시 이름>"
  ln -sf ../../scripts/pre-commit .git/hooks/pre-commit
  git add -A && git status                # 무엇이 들어가는지 눈으로 확인
  git log -p                              # 첫 커밋 뒤, 공개 전 마지막 육안 확인

  # 커밋 뒤 히스토리까지 포함한 최종 검사. 공개하기로 한 신원만 면제되고,
  # 다른 신원으로 들어간 커밋은 그대로 걸린다.
  python3 scripts/check_no_pii.py . --expect-author "<표시 이름> <noreply 주소>"
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
