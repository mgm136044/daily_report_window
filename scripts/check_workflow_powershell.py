"""Parse every PowerShell block in the workflow with the real PowerShell parser.

A `run:` block is not checked by anything until the job that contains it runs,
and a job gated on `if: startsWith(github.ref, 'refs/tags/')` may not run for
months — so a mistake in the release job sits there until the moment it is
needed. Twice already a PowerShell problem in this workflow surfaced as a
ParserError naming a token rather than as anything about what was wrong.

Blocks are extracted the way Actions runs them, which means **the YAML block
scalar's common indentation is stripped first**. That matters: a PowerShell
here-string needs its closing `"@` at column 0, and written inside an indented
`run: |` block it looks as though it cannot be — but the stripping puts it
there. Checking the raw file rather than the de-indented block would report a
problem that does not exist.

Each block is then written to a temporary .ps1 **with a BOM** and handed to
`[Parser]::ParseFile`. The BOM is not incidental — without it Windows
PowerShell 5.1 reads the file in the ANSI codepage and the Korean in these
blocks turns to mojibake, which is a different bug wearing the same clothes.

    python scripts/check_workflow_powershell.py .github/workflows/build.yml
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

# `${{ … }}` is filled in by Actions, not by PowerShell. Left alone it is a
# syntax error everywhere it appears, so it is replaced by a literal.
EXPRESSION = re.compile(r"\$\{\{[^}]*\}\}")

PARSE = (
    "$errors = $null; $tokens = $null; "
    "[System.Management.Automation.Language.Parser]::ParseFile("
    "'{path}', [ref]$tokens, [ref]$errors) | Out-Null; "
    "if ($errors -and $errors.Count -gt 0) {{ "
    "  $errors | ForEach-Object {{ "
    "    Write-Output \"L$($_.Extent.StartLineNumber): $($_.Message)\" }} }} "
    "else {{ Write-Output 'OK' }}"
)


def blocks(text: str):
    """(step name, shell, script) for every `run: |` block."""
    lines = text.splitlines()
    name, shell = "?", ""
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("- name:"):
            name, shell = stripped[len("- name:"):].strip(), ""
        elif stripped.startswith("shell:"):
            shell = stripped[len("shell:"):].strip()
        elif re.match(r"^\s*run: \|", line):
            indent = len(line) - len(line.lstrip())
            body, index = [], index + 1
            while index < len(lines):
                current = lines[index]
                if current.strip() and (len(current) - len(current.lstrip())) <= indent:
                    break
                body.append(current[indent + 2:] if len(current) > indent else "")
                index += 1
            yield name, shell, "\n".join(body)
            continue
        index += 1


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else ".github/workflows/build.yml"
    with open(path, encoding="utf-8") as handle:
        text = handle.read()

    failures = 0
    checked = 0
    for name, shell, script in blocks(text):
        # bash on Linux runners; only the PowerShell ones are ours to parse
        if shell not in ("pwsh", "powershell", ""):
            continue
        if shell == "" and "runs-on: windows" not in text:
            continue
        if not script.strip():
            continue
        # a default-shell block on a windows runner is pwsh, but a matrix job
        # also runs on Linux — those are plain commands, not scripts
        if shell == "" and not re.search(r"[\$\{\}]", script):
            continue

        cleaned = EXPRESSION.sub("PLACEHOLDER", script)
        handle = tempfile.NamedTemporaryFile("wb", suffix=".ps1", delete=False)
        try:
            handle.write(b"\xef\xbb\xbf" + cleaned.encode("utf-8"))
            handle.close()
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command",
                 PARSE.format(path=handle.name.replace("'", "''"))],
                capture_output=True, timeout=120)
            output = result.stdout.decode("utf-8", errors="replace").strip()
        finally:
            os.unlink(handle.name)

        checked += 1
        if output == "OK":
            print(f"  OK    {shell or 'default':<11} {name}")
        else:
            failures += 1
            print(f"  FAIL  {shell or 'default':<11} {name}")
            for line in output.splitlines():
                print(f"          {line}")

    if not checked:
        print("검사한 블록이 없습니다 — 추출이 깨졌을 수 있습니다", file=sys.stderr)
        return 2
    print(f"\n{checked}개 블록 검사, 실패 {failures}개")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
