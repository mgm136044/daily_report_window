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


# --- 프라이버시 검토에서 나온 결함 ------------------------------------------


def test_the_marker_is_not_redacted_a_second_time():
    """The sanitizer was destroying its own output.

    `_mask` wrote `<REDACTED:gitlab_token:glpat-…len26>`, and `key_assignment`
    — which runs later and matches a bare `TOKEN` followed by `:` — then
    matched that marker: the result was `<REDACTED=<REDACTED>`. Most rule names
    contain `token` or `key`, so this hit roughly half of them, and what it
    removed was exactly the kind and length that make a finding diagnosable.
    """
    for text in ["glpat-abcdefghijklmnopqrst",
                 "ntn_abcdefghijklmnopqrstuvwx",
                 "ghp_abcdefghijklmnopqrstuvwx",
                 "Bearer abcdefghijklmnopqrstuvwxyz",
                 "AIza" + "B" * 35]:   # the rule's exact length, so len matches
        cleaned, found = sanitize.redact(text)
        assert found, f"미탐지: {text}"
        assert cleaned.count("<REDACTED") == 1, f"마커가 다시 살균됨: {cleaned}"
        kind = next(iter(found))
        assert kind in cleaned, f"종류가 지워짐: {cleaned}"
        assert f"len{len(text)}" in cleaned, f"길이가 지워짐: {cleaned}"


def test_identity_matches_keep_no_recognisable_head():
    """The head that makes a credential finding actionable is, for an identity,
    the disclosure itself — a resident registration number's first six digits
    are the holder's date of birth, and they survived masking."""
    cleaned, _ = sanitize.redact("901231-1234567")
    assert "901231" not in cleaned, f"생년월일이 남음: {cleaned}"
    assert "len14" in cleaned, "무엇이 지워졌는지도 알 수 없으면 진단이 안 된다"

    cleaned, _ = sanitize.redact("010-1234-5678")
    assert "010-12" not in cleaned, f"번호 앞자리가 남음: {cleaned}"

    cleaned, _ = sanitize.redact("someone.private@example2.co.kr")
    assert "someon" not in cleaned, f"주소 앞부분이 남음: {cleaned}"

    # a credential still keeps its head, which is how you tell which key leaked
    cleaned, _ = sanitize.redact("glpat-abcdefghijklmnopqrst")
    assert "glpat-" in cleaned


def test_encryption_prose_is_not_treated_as_a_password():
    """`암호` is a prefix of ordinary words. `암호화(AES-256)` was rewritten to
    `암호화(AES-256)=<REDACTED>`, so a sentence about encryption came back from
    the sanitizer damaged."""
    for text in ["암호화(AES-256) 를 적용했다",
                 "암호화폐 시세를 2026년에 확인",
                 "암호화된 백업을 만들었다",
                 "암호화 알고리즘은 AES 를 쓴다"]:
        cleaned, found = sanitize.redact(text)
        assert cleaned == text, f"정상 문장이 훼손됨: {cleaned}"
        assert not found, f"오탐: {text} → {dict(found)}"


def test_real_korean_passwords_are_still_caught():
    """The fix above must not be a way of switching the rule off."""
    for text in ["암호는 P@ssw0rd1 이다", "암호: abc123def", "암호 P@ssw0rd1",
                 "비밀번호는 Hunter2Hunter2", "비번 abc123def"]:
        cleaned, found = sanitize.redact(text)
        assert found, f"미탐지: {text}"
        assert "P@ssw0rd1" not in cleaned and "abc123def" not in cleaned
        assert "Hunter2Hunter2" not in cleaned


def test_modern_token_and_webhook_shapes_are_covered():
    """A fine-grained PAT is what GitHub issues by default now, and the classic
    rule cannot see one: `github_pat_` does not begin with `gh` + [pousr]. A
    webhook URL has no prefix to recognise at all — the URL is the credential.
    """
    # The webhook samples are assembled rather than written out. They are
    # synthetic — all zeroes and X's, addressing nothing — but GitHub's push
    # protection matches the *shape*, and it blocked this commit. The choices
    # were to split the literal or to allow-list a secret-shaped string in a
    # public repository forever, and the second is a habit worth not having.
    slack_host = "https://hooks." + "slack.com/services"
    discord_host = "https://discord" + ".com/api/webhooks"
    samples = {
        "github_fine_grained_pat":
            "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz012345",
        "slack_webhook": f"{slack_host}/T00000000/B00000000/{'X' * 24}",
        "discord_webhook": f"{discord_host}/123456789012345678/{'a' * 26}",
        "email": "someone.private@example2.co.kr",
    }
    for kind, text in samples.items():
        cleaned, found = sanitize.redact(text)
        assert kind in found, f"{kind} 미탐지: {text}"
        assert text not in cleaned


def test_package_specifiers_are_not_mistaken_for_addresses():
    """`@[\\w-]+\\.[\\w.]{2,}` is the obvious way to write an email and it also
    matches `react@18.2.0`. Package specifiers are among the most common things
    in a collected shell command, so the report would come back with its own
    install lines redacted. The top-level domain has to be alphabetic."""
    for text in ["npm install react@18.2.0 vite@5.0.11",
                 "pip install ruff@0.4.2",
                 "npx @vitest/coverage-v8@2.1.9"]:
        cleaned, found = sanitize.redact(text)
        assert cleaned == text, f"명령이 훼손됨: {cleaned}"
        assert not found, f"오탐: {text} → {dict(found)}"


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
