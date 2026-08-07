<#
    One-time installer for Windows. Safe to re-run: it never overwrites
    config.toml, .env, or an existing database, and every step reports what it
    decided.

        powershell -ExecutionPolicy Bypass -File install.ps1

    No elevation is needed. The scheduled task runs as the current user with an
    interactive token, and the skill is linked with a junction rather than a
    symbolic link precisely so that stays true.

    Written for Windows PowerShell 5.1, which is what a stock Windows 11 has:
    no `&&`, no ternary, no null-coalescing.

    THIS FILE MUST BE SAVED AS UTF-8 **WITH** A BOM.

    Windows PowerShell 5.1 has no default encoding for scripts: without a BOM
    it decodes the file in the machine's ANSI codepage, which on a Korean
    install is cp949. Every Korean string below then turns to mojibake, and the
    mangled bytes happen to unbalance a quote — so the script does not merely
    print nonsense, it fails to parse at all, hundreds of lines away from
    anything that looks related. tests/test_fixes.py locks this down.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Step($text) { Write-Host ""; Write-Host $text -ForegroundColor White }
function Ok($text)   { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  [!]    $text" -ForegroundColor Yellow }
function Die($text)  { Write-Host ""; Write-Host "[X] $text" -ForegroundColor Red; exit 1 }

function Ask($prompt, $default) {
    $answer = Read-Host "  $prompt [$default]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $default }
    return $answer.Trim()
}

# tomllib rejects a byte-order mark, and Set-Content -Encoding UTF8 writes one
# in PowerShell 5.1. A config.toml written the obvious way fails to parse with
# an error that points at the first line and explains nothing.
function Write-Utf8NoBom($path, $text) {
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $text, $encoding)
}

function Read-EnvValue($key) {
    if (-not (Test-Path .env)) { return "" }
    foreach ($line in (Get-Content .env -Encoding UTF8)) {
        $trimmed = $line.Trim()
        if ($trimmed.StartsWith("$key=")) {
            return $trimmed.Substring($key.Length + 1).Trim()
        }
    }
    return ""
}

# ---------------------------------------------------------------- platform ---
Step "1/8  플랫폼 확인"
if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) {
    Die "Windows 전용 설치 스크립트입니다. macOS/Linux 는 install.sh 를 쓰세요."
}
$osName = (Get-CimInstance Win32_OperatingSystem).Caption
Ok "$osName  (PowerShell $($PSVersionTable.PSVersion))"

# ------------------------------------------------------------------ python ---
Step "2/8  Python 확인"
$python = ""
# `py -3` first: the bare `python` on PATH is usually the Microsoft Store stub,
# which resolves and then refuses to run.
foreach ($candidate in @(@('py', '-3'), @('python'), @('python3'))) {
    $exe = $candidate[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    $args = @()
    if ($candidate.Count -gt 1) { $args = $candidate[1..($candidate.Count - 1)] }
    try {
        $probe = & $exe @args -c "import sys; print(sys.executable if sys.version_info >= (3,11) else '')" 2>$null
    } catch { continue }
    if ($probe -and $probe.Trim()) { $python = $probe.Trim(); break }
}
if (-not $python) {
    Die "Python 3.11 이상이 필요합니다 (tomllib). https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요."
}
$pythonVersion = (& $python --version)
Ok "$python ($pythonVersion)"

# pythonw.exe runs the scheduled job without flashing a console window. It sits
# next to python.exe in every normal install; if it does not, the console
# version still works and is better than not installing.
$pythonw = Join-Path (Split-Path $python -Parent) "pythonw.exe"
if (-not (Test-Path $pythonw)) {
    $pythonw = $python
    Warn "pythonw.exe 가 없어 python.exe 로 등록합니다 (매일 04:05 에 콘솔 창이 잠깐 뜹니다)"
}

# ------------------------------------------------------------------ claude ---
Step "3/8  Claude Code CLI 확인"
$claudeBin = ""
$claudeCandidates = @(
    (Join-Path $HOME ".local\bin\claude.exe"),
    (Join-Path $HOME ".local\bin\claude.cmd"),
    (Join-Path $env:APPDATA "npm\claude.cmd")
)
$onPath = Get-Command claude -ErrorAction SilentlyContinue
if ($onPath) { $claudeCandidates = @($onPath.Source) + $claudeCandidates }
foreach ($candidate in $claudeCandidates) {
    if ($candidate -and (Test-Path $candidate)) { $claudeBin = $candidate; break }
}
if ($claudeBin) {
    Ok "claude: $claudeBin"
} else {
    Warn "claude 를 찾지 못했습니다. 수집은 되지만 요약이 생성되지 않습니다."
}

# ------------------------------------------------------------------ config ---
Step "4/8  config.toml"
if (Test-Path config.toml) {
    Ok "이미 있음 — 건드리지 않습니다"
} else {
    $language = Ask '보고서 언어 (ko/en)' 'ko'
    if ($language -ne 'ko' -and $language -ne 'en') { $language = 'ko' }
    # A Korean (or any non-ASCII) account name reduces to the empty string
    # here, which would produce the label "com..daily-report".
    $slug = ($env:USERNAME.ToLower() -replace '[^a-z0-9]', '')
    if (-not $slug) { $slug = 'user' }
    $label = "com.$slug.daily-report"

    $text = Get-Content -Raw -Encoding UTF8 config.windows.example.toml
    $text = $text.Replace('label = "com.example.daily-report"', "label = `"$label`"")
    $text = [regex]::Replace($text, '(?m)^language = "en"$', "language = `"$language`"")
    $text = [regex]::Replace($text, '(?m)^schema_language = "en"$', "schema_language = `"$language`"")
    if ($claudeBin) {
        $escaped = $claudeBin.Replace('\', '\\')
        $text = $text.Replace('claude_bin = ""', "claude_bin = `"$escaped`"")
    }
    Write-Utf8NoBom (Join-Path $PSScriptRoot "config.toml") $text
    Ok "config.windows.example.toml -> config.toml  (언어 $language, Label $label)"

    # Ask the machine where its shell folders really are, rather than guessing.
    #
    # Two things make guessing wrong. A localized Windows has both `Documents`
    # and `문서`; and OneDrive's Known Folder Move, on by default for many
    # setups, relocates them inside OneDrive — so the real Documents folder is
    # `C:\Users\you\OneDrive\문서`. Left undeclared, that folder is a direct
    # child of a container and every PowerShell profile edit under it gets
    # reported as work on a project called "문서".
    $shellFolders = @()
    foreach ($folder in @('MyDocuments', 'Desktop', 'MyPictures', 'MyMusic', 'MyVideos')) {
        $resolved = [Environment]::GetFolderPath($folder)
        if ($resolved -and ($shellFolders -notcontains $resolved)) { $shellFolders += $resolved }
    }
    $addedContainers = @()
    $addedWalk = @()
    $text = Get-Content -Raw -Encoding UTF8 config.toml
    foreach ($resolved in $shellFolders) {
        $forward = $resolved.Replace('\', '/')
        if ($text -notmatch [regex]::Escape("`"$forward`"")) {
            $text = $text.Replace('    "~/Documents",', "    `"$forward`",`r`n    `"~/Documents`",")
            $addedContainers += $resolved
        }
        $leaf = Split-Path $resolved -Leaf
        if ($leaf -and ($text -notmatch [regex]::Escape("`"/$leaf/`""))) {
            $text = $text.Replace('    "/$Recycle.Bin/",', "    `"/$leaf/`",`r`n    `"/`$Recycle.Bin/`",")
            $addedWalk += $leaf
        }
    }
    Write-Utf8NoBom (Join-Path $PSScriptRoot "config.toml") $text
    if ($addedContainers.Count -gt 0) {
        Ok "이 PC 의 실제 셸 폴더 경로 추가: $($addedContainers -join ', ')"
    } else {
        Ok "셸 폴더 경로는 예시에 이미 포함돼 있습니다"
    }

    # git.authors has no usable default: empty collects nothing, and guessing
    # the wrong identity silently attributes other people's commits.
    $suggested = ""
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $suggested = (git config --global user.email)
    }
    if ($suggested) {
        $answer = Ask "커밋 저자로 쓸 이메일 (쉼표로 여러 개)" $suggested
        $emails = @()
        foreach ($piece in $answer.Split(',')) {
            $piece = $piece.Trim()
            if ($piece) { $emails += "`"$piece`"" }
        }
        if ($emails.Count -gt 0) {
            $rendered = "authors = [" + ($emails -join ", ") + "]"
            $text = Get-Content -Raw -Encoding UTF8 config.toml
            Write-Utf8NoBom (Join-Path $PSScriptRoot "config.toml") $text.Replace("authors = []", $rendered)
            Ok "git.authors 설정됨 ($($emails.Count)개)"
        }
    } else {
        Warn "config.toml 의 [git] authors 를 직접 채우세요 — 비어 있으면 커밋이 하나도 집계되지 않습니다"
    }

    # Where the repositories actually are.
    #
    # `~` is the right default on macOS and often wrong here: Windows users
    # commonly keep projects on a second drive. Finding nothing there is not an
    # error anywhere in the collector — every day just reports zero commits,
    # which is indistinguishable from a quiet fortnight. So ask, and make the
    # default an answer that was measured rather than assumed.
    Write-Host "  git 저장소를 찾는 중 (얕은 탐색)…"
    $best = $HOME
    $bestCount = -1
    $roots = @($HOME)
    foreach ($drive in (Get-PSDrive -PSProvider FileSystem)) {
        if ($drive.Root -match '^[A-Za-z]:\\$' -and $drive.Used -ne $null) {
            $roots += $drive.Root
        }
    }
    foreach ($candidate in ($roots | Select-Object -Unique)) {
        try {
            $found = @(Get-ChildItem -LiteralPath $candidate -Directory -Filter '.git' `
                       -Recurse -Depth 4 -Force -ErrorAction SilentlyContinue)
        } catch { $found = @() }
        Write-Host "    $candidate → $($found.Count)개"
        if ($found.Count -gt $bestCount) { $bestCount = $found.Count; $best = $candidate }
    }
    $searchRoot = Ask 'git 저장소를 찾을 최상위 폴더' $best.TrimEnd('\')
    $forwardRoot = $searchRoot.Replace('\', '/')
    $text = Get-Content -Raw -Encoding UTF8 config.toml
    $text = [regex]::Replace($text, '(?m)^git_search_root = ".*"$',
                             "git_search_root = `"$forwardRoot`"")
    Write-Utf8NoBom (Join-Path $PSScriptRoot "config.toml") $text
    Ok "git_search_root = $forwardRoot"
}

$label = & $python -c "import tomllib;print(tomllib.load(open('config.toml','rb'))['launchd']['label'])"
$label = $label.Trim()
Ok "작업 이름: $label"

# --------------------------------------------------------------------- env ---
Step "5/8  .env"
New-Item -ItemType Directory -Force -Path logs, state, work | Out-Null
if (Test-Path .env) {
    Ok "이미 있음 — 건드리지 않습니다"
} else {
    Write-Utf8NoBom (Join-Path $PSScriptRoot ".env") (Get-Content -Raw -Encoding UTF8 .env.example)
    Ok ".env.example -> .env"
}
# Outside the branch above: re-running the installer must re-assert this, or a
# .env that was created by hand — or whose ACL was reset by a copy, a restore
# or a move between drives — stays readable by every account on the machine.
# The macOS equivalent of chmod 600: drop inherited access, keep the owner plus
# SYSTEM and Administrators (who could take ownership regardless).
$principal = "$($env:USERDOMAIN)\$($env:USERNAME)"
icacls .env /inheritance:r /grant:r "${principal}:F" "*S-1-5-18:F" "*S-1-5-32-544:F" /Q | Out-Null
Ok ".env 권한: 본인 계정만 읽기 가능"

$token  = Read-EnvValue "DAILY_REPORT_NOTION_TOKEN"
$parent = Read-EnvValue "DAILY_REPORT_PARENT_PAGE_URL"
if (-not $token -or -not $parent) {
    Write-Host ""
    Write-Host "  .env 를 채운 뒤 이 스크립트를 다시 실행하세요:"
    Write-Host ""
    Write-Host "    DAILY_REPORT_NOTION_TOKEN=      노션 내부 연결의 Installation access token"
    Write-Host "    DAILY_REPORT_PARENT_PAGE_URL=   데이터베이스를 만들 부모 페이지 URL"
    Write-Host ""
    Write-Host "  발급 절차: docs\notion-setup.ko.md (English: docs\notion-setup.md)"
    Write-Host "  토큰은 그 파일 안에만 두세요. 터미널에 붙여넣으면 세션 로그에 남습니다."
    exit 0
}
Ok "토큰 $($token.Substring(0, [Math]::Min(8, $token.Length)))… ($($token.Length)자) · 부모 페이지 설정됨"

# ---------------------------------------------------------------- database ---
Step "6/8  Notion 데이터베이스"
$databaseId = Read-EnvValue "DAILY_REPORT_DATABASE_ID"
if ($databaseId) {
    Ok "이미 설정됨: $databaseId"
} else {
    # `2>&1` merges native stderr into the pipeline as ErrorRecords, and with
    # $ErrorActionPreference = 'Stop' that is a *terminating* error — so the
    # friendly Die below was unreachable exactly when it was needed, and the
    # installer aborted with a raw .NET exception having already written
    # config.toml and .env but not registered anything.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & $python setup_notion_db.py 2>&1
    $exit = $LASTEXITCODE
    $ErrorActionPreference = $previous
    if ($exit -ne 0) { Die "데이터베이스 생성 실패:`n$($output -join "`n")" }
    $output | ForEach-Object { Write-Host "     $_" }
    $match = [regex]::Match(($output -join "`n"),
                            '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
    if (-not $match.Success) {
        Die "출력에서 데이터베이스 ID 를 찾지 못했습니다. 위 출력을 보고 .env 에 직접 넣으세요."
    }
    $databaseId = $match.Value
    $lines = Get-Content .env -Encoding UTF8
    $updated = $lines | ForEach-Object {
        if ($_.StartsWith("DAILY_REPORT_DATABASE_ID=")) { "DAILY_REPORT_DATABASE_ID=$databaseId" } else { $_ }
    }
    Write-Utf8NoBom (Join-Path $PSScriptRoot ".env") (($updated -join "`r`n") + "`r`n")
    Ok ".env 에 기록됨: $databaseId"
}

# --------------------------------------------------------------- scheduler ---
Step "7/8  작업 스케줄러 등록"
$sid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
$xml = Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot "templates\schtasks.xml.template")
# Escaped, not interpolated: a project directory containing `&` is legal on
# Windows and would otherwise produce XML the scheduler rejects.
function Xml($text) { [System.Security.SecurityElement]::Escape($text) }
$xml = $xml.Replace('{{LABEL}}', (Xml $label))
$xml = $xml.Replace('{{PROJECT_DIR}}', (Xml $PSScriptRoot))
$xml = $xml.Replace('{{PYTHON}}', (Xml $pythonw))
$xml = $xml.Replace('{{USER_SID}}', (Xml $sid))
# Any past date works: only the time of day is read from a daily trigger.
$xml = $xml.Replace('{{START_BOUNDARY}}', '2020-01-01T04:05:00')

$existing = Get-ScheduledTask -TaskName $label -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $label -Confirm:$false
}
try {
    Register-ScheduledTask -TaskName $label -Xml $xml -Force | Out-Null
} catch {
    Die "작업 등록 실패: $($_.Exception.Message)"
}
$info = Get-ScheduledTask -TaskName $label
Ok "작업 '$label' 등록됨 (매일 04:05, 로그온 유형 $($info.Principal.LogonType))"
Ok "실행: $pythonw -X utf8 -u `"$PSScriptRoot\run_day.py`" --log"

# ------------------------------------------------------------------- skill ---
Step "8/8  Claude Code 스킬"
$skillRoot = Join-Path $HOME ".claude\skills"
$skillLink = Join-Path $skillRoot "daily-report"
New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
$existingLink = Get-Item $skillLink -ErrorAction SilentlyContinue
if ($existingLink -and -not $existingLink.LinkType) {
    Warn "$skillLink 이 링크가 아니라서 건드리지 않았습니다"
} else {
    if ($existingLink) { Remove-Item $skillLink -Recurse -Force }
    # A junction, not a symbolic link: symlinks need admin or Developer Mode,
    # and asking for elevation to install a per-user scheduled job is worse
    # than the junction's one limitation (it cannot cross to a network share).
    New-Item -ItemType Junction -Path $skillLink -Target (Join-Path $PSScriptRoot "skills\daily-report") | Out-Null
    Ok "$skillLink -> skills\daily-report"
}

# ------------------------------------------------------------------ verify ---
Write-Host ""
& $python doctor.py

$yesterday = (Get-Date).AddDays(-1).ToString('yyyy-MM-dd')
Write-Host ""
Write-Host "다음:"
Write-Host "  $python summarize.py x y --preflight    무인 인증 확인 (실제 호출 1회)"
Write-Host "  $python run_day.py $yesterday          어제 하루치 시험 실행"
Write-Host ""
Write-Host "첫 예약 실행은 내일 04:05 입니다. 그 전에 위 시험 실행으로 확인하세요."
