"""Sanitizer and leak-checker tests.

pii-allow-file: exercising a redactor requires credential-shaped strings.
Every value here is synthetic and corresponds to no real account.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sanitize  # noqa: E402

HOME = os.path.expanduser("~")


def _load_checker():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "scripts", "check_no_pii.py")
    spec = importlib.util.spec_from_file_location("check_no_pii", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prefixed_env_var_names_are_redacted():
    """The `\\b` anchor let OPENAI_API_KEY= through while catching API_KEY=."""
    for text in ["OPENAI_API_KEY=sk-abcdefghijklmnop",
                 "DB_PASSWORD=Hunter2Hunter2",
                 "NOTION_SECRET_KEY=abcdefghijklmnop",
                 "MY_ACCESS_TOKEN=abcdefghijklmnop"]:
        cleaned, found = sanitize.redact(text)
        assert found, f"미탐지: {text}"
        assert "Hunter2Hunter2" not in cleaned
        assert "abcdefghijklmnop" not in cleaned


def test_colon_separator_does_not_leak_value_head():
    """`PASSWORD: abc=def` used to keep `abc` by splitting on `=` first."""
    cleaned, found = sanitize.redact("PASSWORD: abc=defghijk")
    assert found
    assert "abc" not in cleaned


def test_new_credential_shapes_are_covered():
    samples = {
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3In0.abcdefghijklmno",
        "connection_string": "postgres://admin:Hunter2Pass@db.example.com:5432/app",
        "openai_project_key": "sk-proj-abcdefghijklmnopqrstuvwx",
        "huggingface_token": "hf_abcdefghijklmnopqrstuvwxyz",
        "gitlab_token": "glpat-abcdefghijklmnopqrst",
        "stripe_key": "sk_live_abcdefghijklmnopqrst",
        "ssh_key_body": "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAA",
        "krn_rrn": "901231-1234567",
        "krn_phone": "010-1234-5678",
    }
    for kind, text in samples.items():
        _, found = sanitize.redact(text)
        assert kind in found, f"{kind} 미탐지: {text}"


def test_ordinary_korean_prose_is_untouched():
    text = "오늘은 발표자료를 13장으로 재편했다. 비밀번호는 안전하게 관리해야 한다."
    cleaned, found = sanitize.redact(text)
    assert cleaned == text, f"정상 문장이 훼손됨: {cleaned}"
    assert not found


def test_dict_keys_are_redacted_too():
    """Project names are dict keys and become Notion properties."""
    payload = {"ntn_abcdefghijklmnopqrstuvwx": {"note": "ok"}}
    cleaned, found = sanitize.redact_structure(payload)
    assert "notion_token" in found
    assert not any("ntn_abcdefghij" in k for k in cleaned)


# --- project rollup --------------------------------------------------------


def test_codex_data_is_sanitized_like_claude_data():
    payload = {"projects": {"p": {"outcomes": ["토큰은 ntn_abcdefghijklmnopqrstuvwx 이다"]}}}
    cleaned, found = sanitize.redact_structure(payload)
    assert "notion_token" in found
    assert "ntn_abcdefghij" not in json.dumps(cleaned, ensure_ascii=False)


def test_checker_blocks_real_identifiers():
    chk = _load_checker()
    detectors = chk.GENERIC + chk.load_credential_patterns()
    for line in ["authors = [\"real.person@company.co.kr\"]",
                 "/Users/someperson/dev/project",
                 "<string>com.someperson.daily-report</string>"]:
        assert chk.scan_text(line, detectors), f"차단되지 않음: {line}"


def test_checker_allows_deliberate_placeholders():
    """Examples must survive, or a distributable repo cannot be published."""
    chk = _load_checker()
    detectors = chk.GENERIC + chk.load_credential_patterns()
    for line in ["Co-Authored-By: Someone <noreply@anthropic.com>",
                 "DAILY_REPORT_EMAIL=you@example.com",
                 "path = /Users/username/project",
                 "<string>com.example.daily-report</string>",
                 "<string>{{USER}}</string>"]:
        assert not chk.scan_text(line, detectors), f"오탐: {line}"


def test_checker_redacts_its_own_findings():
    """A CI log or a pasted issue is itself somewhere this can escape."""
    chk = _load_checker()
    secret = "someone.private@company.example2.co.kr"
    findings = chk.scan_text(f"email = {secret}", chk.GENERIC)
    assert findings
    for _, _, value in findings:
        assert secret not in value, "검사기가 발견 내용을 그대로 출력함"
        assert "len" in value


def test_checker_honours_line_and_file_exemptions():
    chk = _load_checker()
    detectors = chk.GENERIC
    assert not chk.scan_text("/Users/someone/x  # pii-allow: 예시 경로", detectors)
    assert chk.file_exemption("# pii-allow-file: 합성 값만 들어 있다\nrest") == \
        "합성 값만 들어 있다"
    assert chk.file_exemption("아무 표시 없음") is None


def test_checker_skips_sanitizer_heuristics():
    """The sanitizer over-matches on purpose; source code is not prose."""
    chk = _load_checker()
    kinds = {k for k, _ in chk.load_credential_patterns()}
    assert "credential:key_assignment" not in kinds
    assert "credential:notion_token" in kinds, "정밀 패턴까지 빠지면 안 된다"
