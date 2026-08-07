#!/usr/bin/env bash
#
# One-time installer. Safe to re-run: it never overwrites config.toml, .env, or
# an existing database, and every step reports what it decided.
#
#   bash install.sh
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✅ %s\n' "$*"; }
warn() { printf '  ⚠️  %s\n' "$*"; }
die()  { printf '\n❌ %s\n' "$*" >&2; exit 1; }

interactive() { [ -t 0 ] && [ -t 1 ]; }

ask() {  # ask <prompt> <default>
    local answer
    if ! interactive; then printf '%s' "$2"; return; fi
    read -r -p "  $1 [$2]: " answer </dev/tty
    printf '%s' "${answer:-$2}"
}

# ---------------------------------------------------------------- platform ---
step "1/8  플랫폼 확인"
[ "$(uname -s)" = "Darwin" ] || die "macOS 전용입니다. 스케줄러(launchd)와 슬립 방지(caffeinate)가 macOS 전용이라,
   다른 OS 에서는 수집은 되지만 예약 실행이 되지 않습니다. platform_support.py 를 참고하세요."
ok "macOS $(sw_vers -productVersion)"

# ------------------------------------------------------------------ python ---
step "2/8  Python 확인"
PYTHON=""
for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
    path="$(command -v "$candidate" 2>/dev/null)" || continue
    if "$path" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        PYTHON="$path"; break
    fi
done
[ -n "$PYTHON" ] || die "Python 3.11 이상이 필요합니다 (tomllib). brew install python 후 다시 실행하세요."
ok "$PYTHON ($("$PYTHON" --version))"

# ------------------------------------------------------------------ claude ---
step "3/8  Claude Code CLI 확인"
if command -v claude >/dev/null 2>&1; then
    ok "claude $(claude --version 2>/dev/null | head -1)"
else
    warn "claude 를 찾지 못했습니다. 수집은 되지만 요약이 생성되지 않습니다."
fi

# ------------------------------------------------------------------ config ---
step "4/8  config.toml"
if [ -f config.toml ]; then
    ok "이미 있음 — 건드리지 않습니다"
else
    LANG_CHOICE="$(ask '보고서 언어 (ko/en)' 'ko')"
    case "$LANG_CHOICE" in ko|en) ;; *) LANG_CHOICE="ko" ;; esac
    LABEL_DEFAULT="com.$(whoami).daily-report"
    "$PYTHON" - "$LANG_CHOICE" "$LABEL_DEFAULT" <<'PY'
import re, sys
language, label = sys.argv[1], sys.argv[2]
text = open("config.example.toml", encoding="utf-8").read()
text = text.replace('label = "com.example.daily-report"', f'label = "{label}"')
# report.language and notion.schema_language both default to "en" in the example
text = re.sub(r'(?m)^language = "en"$', f'language = "{language}"', text)
text = re.sub(r'(?m)^schema_language = "en"$', f'schema_language = "{language}"', text)
open("config.toml", "w", encoding="utf-8").write(text)
PY
    ok "config.example.toml → config.toml  (언어 $LANG_CHOICE, Label $LABEL_DEFAULT)"

    # git.authors has no usable default: empty collects nothing, and guessing
    # the wrong identity silently attributes other people's commits.
    SUGGESTED="$(git config --global user.email 2>/dev/null || true)"
    if [ -n "$SUGGESTED" ] && interactive; then
        ANSWER="$(ask "커밋 저자로 쓸 이메일 (쉼표로 여러 개)" "$SUGGESTED")"
        "$PYTHON" - "$ANSWER" <<'PY'
import sys
emails = [e.strip() for e in sys.argv[1].split(",") if e.strip()]
text = open("config.toml", encoding="utf-8").read()
rendered = "authors = [" + ", ".join(f'"{e}"' for e in emails) + "]"
open("config.toml", "w", encoding="utf-8").write(text.replace("authors = []", rendered))
PY
        ok "git.authors 설정됨"
    else
        warn "config.toml 의 [git] authors 를 직접 채우세요 — 비어 있으면 커밋이 하나도 집계되지 않습니다"
    fi
fi

LABEL="$("$PYTHON" -c 'import tomllib;print(tomllib.load(open("config.toml","rb"))["launchd"]["label"])')"
ok "LaunchAgent Label: $LABEL"

# --------------------------------------------------------------------- env ---
step "5/8  .env"
if [ -f .env ]; then
    ok "이미 있음 — 건드리지 않습니다"
else
    cp .env.example .env
    chmod 600 .env
    ok ".env.example → .env (권한 600)"
fi

read_env() { "$PYTHON" -c '
import sys
key = sys.argv[1]
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line.startswith(key + "="):
        print(line.partition("=")[2].strip())
        break
' "$1"; }

TOKEN="$(read_env DAILY_REPORT_NOTION_TOKEN)"
PARENT="$(read_env DAILY_REPORT_PARENT_PAGE_URL)"
if [ -z "$TOKEN" ] || [ -z "$PARENT" ]; then
    cat <<EOF

  .env 를 채운 뒤 이 스크립트를 다시 실행하세요:

    DAILY_REPORT_NOTION_TOKEN=      노션 내부 연결의 Installation access token
    DAILY_REPORT_PARENT_PAGE_URL=   데이터베이스를 만들 부모 페이지 URL

  발급 절차: docs/notion-setup.ko.md (English: docs/notion-setup.md)
  토큰은 그 파일 안에만 두세요. 터미널에 붙여넣으면 세션 로그에 남습니다.
EOF
    exit 0
fi
ok "토큰 ${TOKEN:0:8}… (${#TOKEN}자) · 부모 페이지 설정됨"

# ---------------------------------------------------------------- database ---
step "6/8  Notion 데이터베이스"
DATABASE_ID="$(read_env DAILY_REPORT_DATABASE_ID)"
if [ -n "$DATABASE_ID" ]; then
    ok "이미 설정됨: $DATABASE_ID"
else
    OUTPUT="$("$PYTHON" setup_notion_db.py)" || die "데이터베이스 생성 실패:
$OUTPUT"
    printf '%s\n' "$OUTPUT" | sed 's/^/     /'
    DATABASE_ID="$(printf '%s' "$OUTPUT" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)"
    [ -n "$DATABASE_ID" ] || die "출력에서 데이터베이스 ID 를 찾지 못했습니다. 위 출력을 보고 .env 에 직접 넣으세요."
    "$PYTHON" - "$DATABASE_ID" <<'PY'
import sys
database_id = sys.argv[1]
lines = open(".env", encoding="utf-8").read().splitlines()
key = "DAILY_REPORT_DATABASE_ID"
lines = [f"{key}={database_id}" if line.startswith(key + "=") else line for line in lines]
open(".env", "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
    ok ".env 에 기록됨: $DATABASE_ID"
fi

# --------------------------------------------------------------- scheduler ---
step "7/8  LaunchAgent 등록"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" logs state work
TZ_NAME="$(readlink /etc/localtime 2>/dev/null | sed 's|.*zoneinfo/||')"
[ -n "$TZ_NAME" ] || TZ_NAME="UTC"

sed -e "s|{{LABEL}}|$LABEL|g" \
    -e "s|{{PROJECT_DIR}}|$HERE|g" \
    -e "s|{{PYTHON}}|$PYTHON|g" \
    -e "s|{{USER}}|$(whoami)|g" \
    -e "s|{{HOME}}|$HOME|g" \
    -e "s|{{TZ}}|$TZ_NAME|g" \
    templates/launchagent.plist.template > "$PLIST"

plutil -lint "$PLIST" >/dev/null || die "생성된 plist 가 올바르지 않습니다: $PLIST"
# bootout first, or bootstrap fails on an already-loaded label
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
ok "$PLIST 등록됨 (매일 04:05, TZ $TZ_NAME)"

# ------------------------------------------------------------------- skill ---
step "8/8  Claude Code 스킬"
SKILL_LINK="$HOME/.claude/skills/daily-report"
mkdir -p "$HOME/.claude/skills"
if [ -e "$SKILL_LINK" ] && [ ! -L "$SKILL_LINK" ]; then
    warn "$SKILL_LINK 이 심링크가 아니라서 건드리지 않았습니다"
else
    ln -sfn "$HERE/skills/daily-report" "$SKILL_LINK"
    ok "$SKILL_LINK → skills/daily-report"
fi

# ------------------------------------------------------------------ verify ---
printf '\n'
"$PYTHON" doctor.py || true

cat <<EOF

다음:
  $PYTHON summarize.py x y --preflight    무인 인증 확인 (실제 호출 1회)
  $PYTHON run_day.py $(date -v-1d +%Y-%m-%d)          어제 하루치 시험 실행
EOF
