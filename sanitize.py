"""Redact credentials before anything leaves this machine.

This is not a precaution. Scanning 30 days of this machine's own prompts and
shell commands turned up three live Notion tokens sitting in plain text, so
without this step the job would publish a Notion token into Notion.

It runs at two points, deliberately:
  - on the digest, so the model never sees a secret in the first place
  - on the finished report, to catch anything that came back out

Regexes only catch known shapes, so the architecture carries the real defense:
only the model's prose goes to Notion, never the raw prompts and commands.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from functools import lru_cache

# Ordered most-specific first so a token matches its own rule, not a generic one.
#
# The assignment rule deliberately allows a prefix before the keyword. An
# earlier version anchored it with `\b`, which matched `API_KEY=` but silently
# let `OPENAI_API_KEY=` and `DB_PASSWORD=` through, because `_` is a word
# character so there is no boundary in front of the keyword. Real .env files
# are almost entirely prefixed names, so that gap covered the most common
# leak shape there is.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")),
    ("notion_token", re.compile(r"\b(?:ntn_[A-Za-z0-9]{20,}|secret_[A-Za-z0-9]{30,})")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    # Fine-grained PATs are the default GitHub now offers, and the classic rule
    # above cannot see them: `github_pat_` does not start with `gh` + [pousr].
    ("github_fine_grained_pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{15,}")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    # A webhook URL is a bearer credential with no token prefix to recognise it
    # by — the whole URL is the secret, and anyone holding it can post as the
    # integration. They travel in shell commands more often than tokens do,
    # because `curl -d ... <url>` is how people test them.
    ("slack_webhook", re.compile(
        r"https://hooks\.slack\.com/(?:services|workflows)/[A-Za-z0-9_/+\-]{20,}")),
    ("discord_webhook", re.compile(
        r"https://(?:[a-z]+\.)?discord(?:app)?\.com/api/webhooks/\d+/[\w\-]{20,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}")),
    ("stripe_key", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{20,}")),
    # The shapes a developer's machine actually holds, which is what this tool
    # reads. `.npmrc` is already treated as a file too sensitive to name in a
    # report (collect_fs._is_noise) — the token that lives in it was not.
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}")),
    ("docker_pat", re.compile(r"\bdckr_pat_[A-Za-z0-9_\-]{20,}")),
    ("google_oauth_secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{20,}")),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}")),
    ("pypi_token", re.compile(r"\bpypi-[A-Za-z0-9_\-]{40,}")),
    ("openai_project_key", re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    # base64 body of an OpenSSH key, which often appears without its header
    ("ssh_key_body", re.compile(r"\bb3BlbnNzaC1rZXktdjEA[A-Za-z0-9+/=]{20,}")),
    ("connection_string", re.compile(
        r"\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp|ftp|ssh)://"
        r"[^\s:/@]+:[^\s@/]+@")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    # Written the way people actually write them, not only with a hyphen. A
    # detector that covers one spelling of a resident registration number
    # covers the one nobody was worried about.
    ("krn_rrn", re.compile(r"\b\d{6}[\s\-–—]?[1-4]\d{6}\b")),
    ("krn_phone", re.compile(
        r"(?:\+?82[\s\-]?1|\b01)[016789][\s\-]?\d{3,4}[\s\-]?\d{4}\b")),
    # This tool's own setup hands the user a Notion database id and a parent
    # page URL to paste, so those are unusually likely to be sitting in the
    # session logs it then reads. `check_no_pii.py` has had these two since
    # the beginning; the runtime sanitizer never did, and the comment there
    # about the two never drifting apart only points one way.
    ("notion_uuid", re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("notion_id", re.compile(r"\b[0-9a-f]{32}\b")),
    # The top-level domain has to be alphabetic. Written the obvious way,
    # `@[\w-]+\.[\w.]{2,}`, it also matches a package specifier —
    #   react@18.2.0    # pii-allow: 주소가 아니라 패키지 지정자 예시
    # — and those are among the most common things in a collected shell
    # command, so the report would come back with its own install lines
    # redacted. `check_no_pii.py` still carries the loose form and flags the
    # example above, which is the demonstration and the reason for the marker.
    ("email", re.compile(
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}\b")),
    # prefixed or bare secret-ish variable names, `=` or `:` separated
    # `(?!<REDACTED)` keeps this rule off a marker an earlier rule just wrote.
    #
    # Spacing the marker's fields was not enough, and the comment in `_mask`
    # said it was. `token: ntn_…` becomes `token: <REDACTED notion_token …>`,
    # and `<REDACTED` is nine characters with no space in it — a perfectly good
    # value as far as this pattern is concerned. The result was
    # `token=<REDACTED> notion_token ntn_AA… len28>`: the value gone twice
    # over, and the kind and length left dangling behind an unbalanced
    # bracket. `.env` lines and shell history are almost entirely `KEY=value`,
    # so this was the common case rather than an edge one.
    ("key_assignment", re.compile(
        r"(?i)(?:^|[^A-Za-z0-9_])"
        r"([A-Za-z0-9_]*(?:API[_-]?KEY|ACCESS[_-]?TOKEN|AUTH[_-]?TOKEN|"
        r"SECRET[_-]?KEY|CLIENT[_-]?SECRET|PRIVATE[_-]?KEY|PASSWORD|PASSWD|"
        r"SECRET|TOKEN)[A-Za-z0-9_]*)"
        r"\s*[=:]\s*[\"']?(?!<REDACTED)([^\s\"'&;,)]{8,})")),
    # Korean prose form. Requires the value to look like a secret (contains a
    # digit or symbol and no spaces) so ordinary sentences are not damaged.
    #
    # `암호` is split out from the others because it is a prefix of ordinary
    # words — 암호화, 암호화폐 — where the rest of the compound then reads as
    # the value. `암호화(AES-256)` was rewritten to `암호화(AES-256)=<REDACTED>`,
    # so a sentence about encryption came out of the sanitizer damaged. The
    # bare stem therefore needs a particle, a separator or a space after it;
    # the unambiguous words do not, because nothing is built on top of them.
    ("korean_secret", re.compile(
        r"(?:(?:비밀번호|패스워드|비번)\s*(?:는|은|가|이|:|=)?\s*"
        r"|암호\s*(?:는|은|가|이|:|=)\s*|암호\s+)"
        r"[\"']?((?=[^\s\"']*[0-9!@#$%^&*])[^\s\"']{6,})")),
]

# Kinds whose leading characters are the sensitive part rather than a label for
# it. A resident registration number's first six digits are the holder's date
# of birth, so the head that makes a credential finding actionable is, for
# these, the disclosure itself.
IDENTITY_KINDS = frozenset({"krn_rrn", "krn_phone", "email"})


def _mask(kind: str, matched: str) -> str:
    """Keep enough to recognise what was removed, not enough to use it.

    The separator is whichever of `=` or `:` comes first. Preferring `=`
    unconditionally leaked the head of the value whenever the separator was
    `:` and the value itself contained an `=`, e.g. `PASSWORD: abc=defghijk`
    kept `abc`.

    The marker separates its fields with spaces rather than colons, because
    `key_assignment` runs later in the list and matches a bare `TOKEN` — so it
    matched the marker this function had just produced. `glpat-…` came out as
    `<REDACTED=<REDACTED>`: the sanitizer's own second pass destroyed the kind
    and length that make a finding diagnosable, on roughly half the rules,
    since most of their names contain `token` or `key`.

    **Spacing alone did not finish the job, and this paragraph used to claim it
    had.** A bare `ntn_…` came out clean, which is what the test checked; but
    `token: ntn_…` still collapsed, because `<REDACTED` is nine characters with
    no space in it and `key_assignment` was happy to treat it as a value. The
    lookahead on that rule is what actually closes it. Named here because the
    wrong version of this comment is why nobody looked again.
    """
    if kind in ("key_assignment", "korean_secret"):
        cut = min((i for i in (matched.find("="), matched.find(":")) if i >= 0),
                  default=-1)
        name = matched[:cut] if cut >= 0 else matched.split()[0]
        return f"{name.strip()}=<REDACTED>"
    if kind in IDENTITY_KINDS:
        return f"<REDACTED {kind} len{len(matched)}>"
    return f"<REDACTED {kind} {matched[:6]}… len{len(matched)}>"


@lru_cache(maxsize=1)
def _home_patterns() -> tuple[re.Pattern, ...]:
    """The home directory, as it can appear inside a collected path."""
    home = os.path.expanduser("~")
    if not home or home in ("/", os.sep):
        return ()
    variants = {home, home.replace("\\", "/")}
    flags = re.IGNORECASE if os.name == "nt" else 0
    return tuple(re.compile(re.escape(v), flags) for v in sorted(variants, key=len,
                                                                 reverse=True))


def abbreviate_home(text: str) -> tuple[str, int]:
    """Replace the home directory with `~`, and count how often.

    **A home directory is a person's name.** `C:\\Users\\<이름>\\...` and
    `/Users/<이름>/...` identify their owner as surely as an email address,
    and file paths are in the digest deliberately — the report is about which
    files were touched, so removing the paths would remove the report.

    So the prefix is shortened rather than the path removed. That keeps every
    part that carries meaning and drops the one part that carries an identity.

    The prompt template already asks the model to abbreviate paths this way.
    Asking is not the same as doing: a model that ignores the instruction puts
    the account name in the Notion row, and nothing downstream would notice.
    This makes it true before the model ever sees it.
    """
    count = 0
    for pattern in _home_patterns():
        text, replaced = pattern.subn("~", text)
        count += replaced
    return text, count


def redact(text: str) -> tuple[str, Counter]:
    findings: Counter = Counter()
    if not text:
        return text, findings
    # First, before any pattern runs: a redaction marker must not be able to
    # swallow the home path, and a shortened path is what every later rule
    # should be matching against.
    text, shortened = abbreviate_home(text)
    if shortened:
        findings["home_path"] += shortened
    for kind, pattern in PATTERNS:
        def replace(match: re.Match, kind=kind) -> str:
            findings[kind] += 1
            return _mask(kind, match.group(0))
        text = pattern.sub(replace, text)
    return text, findings


def redact_structure(value):
    """Walk a JSON-like structure, redacting every string in it."""
    findings: Counter = Counter()
    if isinstance(value, str):
        cleaned, found = redact(value)
        return cleaned, found
    if isinstance(value, list):
        out = []
        for item in value:
            cleaned, found = redact_structure(item)
            out.append(cleaned)
            findings.update(found)
        return out, findings
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            # keys carry data too — project names become Notion property
            # values, so a key left unredacted bypasses sanitizing entirely
            clean_key, key_found = (redact(key) if isinstance(key, str) else (key, Counter()))
            cleaned, found = redact_structure(item)
            out[clean_key] = cleaned
            findings.update(key_found)
            findings.update(found)
        return out, findings
    return value, findings


def describe(findings: Counter) -> str:
    if not findings:
        return "탐지 0건"
    return ", ".join(f"{kind} {count}건" for kind, count in findings.most_common())


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: sanitize.py <file.json|file.md> [out]", file=sys.stderr)
        return 2
    source = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else source

    if source.endswith(".json"):
        with open(source, encoding="utf-8") as handle:
            data = json.load(handle)
        cleaned, findings = redact_structure(data)
        payload = json.dumps(cleaned, ensure_ascii=False, indent=1)
    else:
        with open(source, encoding="utf-8") as handle:
            payload, findings = redact(handle.read())

    with open(target, "w", encoding="utf-8") as handle:
        handle.write(payload)
    print(f"살균: {describe(findings)} → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
