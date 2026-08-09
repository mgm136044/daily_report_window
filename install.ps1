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
param(
    # Supplied by setup_gui.py. The wizard collects the answers and hands them
    # over rather than writing config.toml itself — every step below stays the
    # single implementation, so the GUI cannot drift away from the script that
    # was actually verified on a real machine.
    #
    # Anything omitted is asked for interactively, exactly as before.
    [string] $Language   = "",
    [string] $Authors    = "",
    [string] $SearchRoot = "",
    [switch] $NonInteractive,

    # Set by the packaged build. A frozen install has no python.exe and no
    # run_day.py — only the executables — so the scheduled task and the Start
    # Menu shortcut have to point at those instead.
    #
    # Passing them in rather than writing a second installer is deliberate.
    # Task registration, the icacls narrowing and the skill junction were
    # verified once on a real machine; a parallel implementation for the
    # packaged case would have to be verified again, and the two would drift.
    [string] $AppExe     = "",
    [string] $AppGuiExe  = "",

    # Where config.toml, .env, state/, work/ and logs/ go. A source checkout
    # keeps them beside the code; a packaged install must not, because the
    # bundle directory is replaced wholesale on upgrade and everything written
    # into it — including the ledger and the Notion database id — would go with
    # it. Defaults to the script's own directory, which is the checkout case.
    [string] $DataDir    = ""
)

$Frozen = -not [string]::IsNullOrWhiteSpace($AppExe)
if (-not $DataDir) { $DataDir = $PSScriptRoot }
# Before anything writes into it. A source checkout's data root is the script's
# own directory and obviously exists; a packaged install's is
# %LOCALAPPDATA%\daily-report, which nothing has created yet at this point.
if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Force -Path $DataDir | Out-Null }
$ConfigPath = Join-Path $DataDir "config.toml"
$EnvPath    = Join-Path $DataDir ".env"

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Step($text) { Write-Host ""; Write-Host $text -ForegroundColor White }
function Ok($text)   { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  [!]    $text" -ForegroundColor Yellow }
function Die($text)  { Write-Host ""; Write-Host "[X] $text" -ForegroundColor Red; exit 1 }

function Ask($prompt, $default, $supplied = "") {
    # A value handed in by the wizard wins; -NonInteractive takes the default
    # rather than blocking on a prompt nobody can see behind a GUI.
    if (-not [string]::IsNullOrWhiteSpace($supplied)) { return $supplied.Trim() }
    if ($NonInteractive) { return $default }
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
    if (-not (Test-Path $EnvPath)) { return "" }
    foreach ($line in (Get-Content $EnvPath -Encoding UTF8)) {
        $trimmed = $line.Trim()
        if ($trimmed.StartsWith("$key=")) {
            return $trimmed.Substring($key.Length + 1).Trim()
        }
    }
    return ""
}

# ---------------------------------------------------------------- platform ---
Step "1/9  플랫폼 확인"
if (-not $IsWindows -and $PSVersionTable.PSVersion.Major -ge 6) {
    Die "Windows 전용 설치 스크립트입니다. macOS/Linux 는 install.sh 를 쓰세요."
}
$osName = (Get-CimInstance Win32_OperatingSystem).Caption
Ok "$osName  (PowerShell $($PSVersionTable.PSVersion))"

# ------------------------------------------------------------------ python ---
Step "2/9  Python 확인"
$python = ""
if ($Frozen) {
    # Nothing to find: the interpreter is inside the executable.
    $python  = $AppExe
    $pythonw = if ($AppGuiExe) { $AppGuiExe } else { $AppExe }
    Ok "패키지 빌드 — 별도 Python 이 필요 없습니다"
    Ok "  실행 파일: $AppExe"
}
if (-not $Frozen) {
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
    Warn "pythonw.exe 가 없어 python.exe 로 등록합니다 (예약 실행 때 콘솔 창이 잠깐 뜹니다)"
}
}

# What the scheduled task and the shortcut will actually invoke. One place, so
# the frozen and source layouts diverge here and nowhere else.
if ($Frozen) {
    $TaskCommand   = $AppExe
    $TaskArguments = "run --log"
    $GuiCommand    = if ($AppGuiExe) { $AppGuiExe } else { $AppExe }
    $GuiArguments  = ""
} else {
    $TaskCommand   = $pythonw
    $TaskArguments = "-X utf8 -u `"$PSScriptRoot\run_day.py`" --log"
    $GuiCommand    = $pythonw
    $GuiArguments  = "-X utf8 `"$PSScriptRoot\status_window.py`""
}

# ------------------------------------------------------------------ claude ---
Step "3/9  요약 엔진 확인"
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

# Codex can write the report instead. Looked for the same way, except that the
# desktop install leaves nothing on PATH and lives under a build hash that
# changes with every version — so the directory is globbed and the newest
# taken, which is what platform_support.Windows.codex_argv does at run time.
$codexBin = ""
$codexCandidates = @(
    (Join-Path $HOME ".local\bin\codex.exe"),
    (Join-Path $HOME ".local\bin\codex.cmd"),
    (Join-Path $env:APPDATA "npm\codex.cmd")
)
$codexOnPath = Get-Command codex -ErrorAction SilentlyContinue
if ($codexOnPath) { $codexCandidates = @($codexOnPath.Source) + $codexCandidates }
$codexCandidates += (Get-ChildItem (Join-Path $env:LOCALAPPDATA "OpenAI\Codex\bin") `
                        -Filter codex.exe -Recurse -Depth 1 -ErrorAction SilentlyContinue |
                     Sort-Object LastWriteTime -Descending | ForEach-Object { $_.FullName })
foreach ($candidate in $codexCandidates) {
    if ($candidate -and (Test-Path $candidate)) { $codexBin = $candidate; break }
}

# Claude stays the default where both exist: it is the path this project has
# the most hours on. Codex is chosen only when it is the only one here, which
# is the case that used to be told "요약이 생성되지 않습니다" — wrong advice for
# someone who has a perfectly good CLI installed.
$engine = "claude"
if ($claudeBin) {
    Ok "claude: $claudeBin"
    if ($codexBin) { Ok "codex 도 있습니다 — engine 을 codex 로 바꾸면 그쪽을 씁니다" }
} elseif ($codexBin) {
    $engine = "codex"
    Ok "codex: $codexBin  (claude 가 없어 요약 엔진을 codex 로 설정합니다)"
} else {
    Warn "claude 도 codex 도 찾지 못했습니다. 수집은 되지만 요약이 생성되지 않습니다."
}

# ------------------------------------------------------------------ config ---
Step "4/9  config.toml"
if (Test-Path $ConfigPath) {
    Ok "이미 있음 — 건드리지 않습니다"
} else {
    $language = Ask '보고서 언어 (ko/en)' 'ko' $Language
    if ($language -ne 'ko' -and $language -ne 'en') { $language = 'ko' }
    # A Korean (or any non-ASCII) account name reduces to the empty string
    # here, which would produce the label "com..daily-report".
    $slug = ($env:USERNAME.ToLower() -replace '[^a-z0-9]', '')
    if (-not $slug) { $slug = 'user' }
    $label = "com.$slug.daily-report"

    $text = Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot "config.windows.example.toml")
    $text = $text.Replace('label = "com.example.daily-report"', "label = `"$label`"")
    $text = [regex]::Replace($text, '(?m)^language = "en"$', "language = `"$language`"")
    $text = [regex]::Replace($text, '(?m)^schema_language = "en"$', "schema_language = `"$language`"")
    if ($claudeBin) {
        $escaped = $claudeBin.Replace('\', '\\')
        $text = $text.Replace('claude_bin = ""', "claude_bin = `"$escaped`"")
    }
    # Recorded whichever engine wins, because nothing puts codex on PATH: a
    # scheduled task that has to find it by globbing a build hash is one
    # version bump away from not finding it, and the path is known right now.
    if ($codexBin) {
        $escapedCodex = $codexBin.Replace('\', '\\')
        $text = $text.Replace('codex_bin = ""', "codex_bin = `"$escapedCodex`"")
    }
    if ($engine -ne 'claude') {
        $text = $text.Replace('engine = "claude"', "engine = `"$engine`"")
    }
    Write-Utf8NoBom $ConfigPath $text
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
    # Downloads has no SpecialFolder enum — it is a Known Folder, and the only
    # way to ask for it is by GUID. It is one of the largest and noisiest trees
    # in a home directory, so guessing "~/Downloads" and being wrong is
    # expensive.
    try {
        $downloads = (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders' `
                      -Name '{374DE290-123F-4565-9164-39C4925E467B}' -ErrorAction Stop).'{374DE290-123F-4565-9164-39C4925E467B}'
        $downloads = [Environment]::ExpandEnvironmentVariables($downloads)
        if ($downloads -and ($shellFolders -notcontains $downloads)) { $shellFolders += $downloads }
    } catch { }

    # Cloud folders, which cannot be named in advance: a work account produces
    # `OneDrive - Contoso`, and a machine can have both that and a personal one.
    $cloud = @()
    foreach ($name in @('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value -and ($cloud -notcontains $value)) { $cloud += $value }
    }
    foreach ($dir in (Get-ChildItem -LiteralPath $HOME -Directory -Force -ErrorAction SilentlyContinue |
                      Where-Object { $_.Name -like 'OneDrive*' -or $_.Name -like 'iCloud*' -or $_.Name -like 'Dropbox*' -or $_.Name -like 'Google Drive*' })) {
        if ($cloud -notcontains $dir.FullName) { $cloud += $dir.FullName }
    }
    $addedContainers = @()
    $addedWalk = @()
    $text = Get-Content -Raw -Encoding UTF8 $ConfigPath
    foreach ($resolved in $shellFolders) {
        $forward = $resolved.Replace('\', '/')
        if ($text -notmatch [regex]::Escape("`"$forward`"")) {
            $text = $text.Replace('    "~/Documents",', "    `"$forward`",`r`n    `"~/Documents`",")
            $addedContainers += $resolved
        }
        # Anchored to the home directory, matching the rest of the list: a bare
        # "/문서/" would match any project containing a folder of that name, and
        # an excluded tree reports nothing — it just stops being scanned.
        $leaf = Split-Path $resolved -Leaf
        if ($leaf -and ($text -notmatch [regex]::Escape("`"~/$leaf/`""))) {
            $text = $text.Replace('    "~/AppData/",', "    `"~/AppData/`",`r`n    `"~/$leaf/`",")
            $addedWalk += $leaf
        }
    }
    # A cloud root is never a project itself and must not be walked, but work
    # *inside* it still has to be collected — so it goes to `never` and
    # walk_exclude, never to `exclude.paths`.
    $addedCloud = @()
    foreach ($root in $cloud) {
        $forward = $root.Replace('\', '/')
        if ($text -notmatch [regex]::Escape("`"$forward`"")) {
            $text = $text.Replace('    "%OneDrive%",', "    `"%OneDrive%`",`r`n    `"$forward`",")
            $addedCloud += (Split-Path $root -Leaf)
        }
        if ($text -notmatch [regex]::Escape("`"$forward/`"")) {
            $text = $text.Replace('    "%OneDrive%/",', "    `"%OneDrive%/`",`r`n    `"$forward/`",")
        }
    }
    Write-Utf8NoBom $ConfigPath $text
    if ($addedCloud.Count -gt 0) {
        Ok "클라우드 폴더 추가: $($addedCloud -join ', ')"
    }
    if ($addedWalk.Count -gt 0) {
        Ok "탐색 제외에 추가: $($addedWalk -join ', ')"
    }
    if ($addedContainers.Count -gt 0) {
        Ok "이 PC 의 실제 셸 폴더 경로 추가: $($addedContainers -join ', ')"
    } else {
        Ok "셸 폴더 경로는 예시에 이미 포함돼 있습니다"
    }

    # Every fixed drive, not just the two the example happens to name.
    #
    # A drive root has to be both a container (so a project sitting directly on
    # it is one) and never a project itself. The example lists C: and D:
    # because that is what the machine it was written on had — on a machine
    # whose work lives on E:, `E:\work\app` walks up to `E:\`, finds no
    # container, and is dropped. Silently, like everything else in this file.
    $addedDrives = @()
    $text = Get-Content -Raw -Encoding UTF8 $ConfigPath
    foreach ($drive in (Get-PSDrive -PSProvider FileSystem)) {
        if ($drive.Root -notmatch '^[A-Za-z]:\\$' -or $null -eq $drive.Used) { continue }
        $letter = "$($drive.Name.ToUpper()):/"
        if ($text -match [regex]::Escape("`"$letter`"")) { continue }
        # One replacement, two lists: `    "C:/",` appears in both containers
        # and never, and both of them need every drive.
        $text = $text.Replace('    "C:/",', "    `"C:/`",`r`n    `"$letter`",")
        $addedDrives += $letter
    }
    Write-Utf8NoBom $ConfigPath $text
    if ($addedDrives.Count -gt 0) {
        Ok "드라이브 추가: $($addedDrives -join ', ')"
    }

    # git.authors has no usable default: empty collects nothing, and guessing
    # the wrong identity silently attributes other people's commits.
    $suggested = ""
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $suggested = (git config --global user.email)
    }
    if ($suggested -or $Authors) {
        $answer = Ask "커밋 저자로 쓸 이메일 (쉼표로 여러 개)" $suggested $Authors
        $emails = @()
        foreach ($piece in $answer.Split(',')) {
            $piece = $piece.Trim()
            if ($piece) { $emails += "`"$piece`"" }
        }
        if ($emails.Count -gt 0) {
            $rendered = "authors = [" + ($emails -join ", ") + "]"
            $text = Get-Content -Raw -Encoding UTF8 $ConfigPath
            Write-Utf8NoBom $ConfigPath $text.Replace("authors = []", $rendered)
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
    $best = $HOME
    $bestCount = -1
    $roots = @()
    # Skipped entirely when the wizard already asked — the probe walks several
    # drives and there is no reason to pay for it twice.
    if (-not $SearchRoot) {
        Write-Host "  git 저장소를 찾는 중 (얕은 탐색)…"
        $roots = @($HOME)
        foreach ($drive in (Get-PSDrive -PSProvider FileSystem)) {
            if ($drive.Root -match '^[A-Za-z]:\\$' -and $drive.Used -ne $null) {
                $roots += $drive.Root
            }
        }
    }
    foreach ($candidate in ($roots | Select-Object -Unique)) {
        try {
            # Depth 6 matches sources.git_max_depth, which is what the
            # collector will actually use. A shallower probe recommends a root
            # by a count that is wrong in the direction that matters: measured
            # here, a drive root reported 2 repositories at depth 4 and 4 at
            # depth 6, because `D:\<area>\<group>\<project>\.git` sits on the
            # fifth level. Depth 8 found nothing more and cost twenty times as
            # much.
            $found = @(Get-ChildItem -LiteralPath $candidate -Directory -Filter '.git' `
                       -Recurse -Depth 6 -Force -ErrorAction SilentlyContinue)
        } catch { $found = @() }
        Write-Host "    $candidate → $($found.Count)개"
        if ($found.Count -gt $bestCount) { $bestCount = $found.Count; $best = $candidate }
    }
    $searchRoot = Ask 'git 저장소를 찾을 최상위 폴더' $best.TrimEnd('\') $SearchRoot
    $forwardRoot = $searchRoot.Replace('\', '/')
    $text = Get-Content -Raw -Encoding UTF8 $ConfigPath
    $text = [regex]::Replace($text, '(?m)^git_search_root = ".*"$',
                             "git_search_root = `"$forwardRoot`"")
    Write-Utf8NoBom $ConfigPath $text
    Ok "git_search_root = $forwardRoot"
}

# Scoped to the [launchd] table, not the first `label =` in the file.
#
# `label` is not a unique key in this configuration: every
# [[sources.extra_session_globs]] entry has one too, and they come first. An
# unscoped match named the scheduled task "Claude Desktop (agent mode)" — which
# registers, runs, and is findable only by someone who already suspects it.
$configText = Get-Content -Raw -Encoding UTF8 $ConfigPath
$launchdTable = [regex]::Match($configText, '(?ms)^\[launchd\][^\[]*')
if (-not $launchdTable.Success) { Die "config.toml 에 [launchd] 절이 없습니다: $ConfigPath" }
$labelMatch = [regex]::Match($launchdTable.Value, '(?m)^\s*label\s*=\s*"([^"]+)"')
if (-not $labelMatch.Success) { Die "config.toml 에서 [launchd] label 을 읽지 못했습니다: $ConfigPath" }
$label = $labelMatch.Groups[1].Value
Ok "작업 이름: $label"

# --------------------------------------------------------------------- env ---
Step "5/9  .env"
New-Item -ItemType Directory -Force -Path (Join-Path $DataDir 'logs'), (Join-Path $DataDir 'state'), (Join-Path $DataDir 'work') | Out-Null
if (Test-Path $EnvPath) {
    Ok "이미 있음 — 건드리지 않습니다"
} else {
    Write-Utf8NoBom $EnvPath (Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot ".env.example"))
    Ok ".env.example -> .env"
}
# Outside the branch above: re-running the installer must re-assert this, or a
# .env that was created by hand — or whose ACL was reset by a copy, a restore
# or a move between drives — stays readable by every account on the machine.
# The macOS equivalent of chmod 600: drop inherited access, keep the owner plus
# SYSTEM and Administrators (who could take ownership regardless).
$principal = "$($env:USERDOMAIN)\$($env:USERNAME)"
icacls $EnvPath /inheritance:r /grant:r "${principal}:F" "*S-1-5-18:F" "*S-1-5-32-544:F" /Q | Out-Null
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
Step "6/9  Notion 데이터베이스"
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
    if ($Frozen) { $output = & $AppExe setup-db 2>&1 }
    else         { $output = & $python setup_notion_db.py 2>&1 }
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
    $lines = Get-Content $EnvPath -Encoding UTF8
    $updated = $lines | ForEach-Object {
        if ($_.StartsWith("DAILY_REPORT_DATABASE_ID=")) { "DAILY_REPORT_DATABASE_ID=$databaseId" } else { $_ }
    }
    Write-Utf8NoBom $EnvPath (($updated -join "`r`n") + "`r`n")
    Ok ".env 에 기록됨: $databaseId"
}

# --------------------------------------------------------------- scheduler ---
Step "7/9  작업 스케줄러 등록"
$sid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
$xml = Get-Content -Raw -Encoding UTF8 (Join-Path $PSScriptRoot "templates\schtasks.xml.template")
# Escaped, not interpolated: a project directory containing `&` is legal on
# Windows and would otherwise produce XML the scheduler rejects.
function Xml($text) { [System.Security.SecurityElement]::Escape($text) }
$xml = $xml.Replace('{{LABEL}}', (Xml $label))
$xml = $xml.Replace('{{PROJECT_DIR}}', (Xml $PSScriptRoot))
$xml = $xml.Replace('{{COMMAND}}', (Xml $TaskCommand))
$xml = $xml.Replace('{{ARGUMENTS}}', (Xml $TaskArguments))
$xml = $xml.Replace('{{USER_SID}}', (Xml $sid))
# The time of day comes from the config this install will actually run with —
# read back rather than remembered, because on an upgrade the file already
# exists and step 4 leaves it alone, so whatever the person set is what the
# trigger has to say.
#
# Rejected rather than defaulted when it is unusable: a typo silently falling
# back to 04:05 is a scheduled job firing at a time nobody chose, which is
# exactly the kind of quiet wrongness this project is organised against.
$scheduleTime = '04:05'
$configText = Get-Content -Raw -Encoding UTF8 $ConfigPath
$match = [regex]::Match($configText, '(?m)^\s*schedule_time\s*=\s*"(\d{1,2}):(\d{2})"')
if ($match.Success) {
    $hour = [int]$match.Groups[1].Value
    $minute = [int]$match.Groups[2].Value
    if ($hour -gt 23 -or $minute -gt 59) {
        Die "[run] schedule_time 이 시각이 아닙니다: $($match.Groups[0].Value)"
    }
    $boundary = 4
    $boundaryMatch = [regex]::Match($configText, '(?m)^\s*boundary_hour\s*=\s*(\d{1,2})')
    if ($boundaryMatch.Success) { $boundary = [int]$boundaryMatch.Groups[1].Value }
    if ($hour -lt $boundary) {
        Die ("[run] schedule_time ($($match.Groups[1].Value):$($match.Groups[2].Value)) 이 " +
             "[day] boundary_hour ($boundary) 보다 이릅니다. " +
             "그 시각에는 보고할 하루가 아직 닫히지 않았습니다.")
    }
    $scheduleTime = '{0:d2}:{1:d2}' -f $hour, $minute
}
# Any past date works: only the time of day is read from a daily trigger.
$xml = $xml.Replace('{{START_BOUNDARY}}', "2020-01-01T${scheduleTime}:00")

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
Ok "작업 '$label' 등록됨 (매일 $scheduleTime, 로그온 유형 $($info.Principal.LogonType))"
Ok "실행: $TaskCommand $TaskArguments"

# --------------------------------------------------------------- shortcut ---
Step "8/9  시작 메뉴 바로 가기"
# pythonw so opening the status window does not flash a console behind it.
$startMenu = [Environment]::GetFolderPath('Programs')
$shortcut = Join-Path $startMenu "하루 마감 보고서.lnk"
try {
    $wscript = New-Object -ComObject WScript.Shell
    $link = $wscript.CreateShortcut($shortcut)
    $link.TargetPath       = $GuiCommand
    $link.Arguments        = $GuiArguments
    $link.WorkingDirectory = $PSScriptRoot
    # No em-dash: the shortcut's Description travels through a COM interface
    # that drops it to the ANSI codepage, and it shows up as "?" in the tooltip.
    $link.Description      = "하루 마감 보고서 상태 확인과 진단"
    $link.Save()
    Ok "$shortcut"
} catch {
    Warn "바로 가기를 만들지 못했습니다: $($_.Exception.Message)"
    Warn "직접 실행: $GuiCommand $GuiArguments"
}

# ------------------------------------------------------------------- skill ---
Step "9/9  Claude Code 스킬"
$skillRoot = Join-Path $HOME ".claude\skills"
$skillLink = Join-Path $skillRoot "daily-report"
$skillSource = Join-Path $PSScriptRoot "skills\daily-report"
if ($Frozen) {
    # Inside a bundle the skill lives under _internal, which is replaced
    # wholesale on upgrade — a junction into it would dangle. Copying costs a
    # few kilobytes and survives.
    #
    # The path is the same as the source case: this script *is* `_internal\
    # install.ps1` when frozen, so $PSScriptRoot already ends in `_internal`.
    # Adding it again pointed at `_internal\_internal\skills`, which does not
    # exist — and the failure is a Warn, so the skill was simply never
    # installed and nothing said so.
    $skillSource = Join-Path $PSScriptRoot "skills\daily-report"
}
New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
$existingLink = Get-Item $skillLink -ErrorAction SilentlyContinue
if (-not (Test-Path $skillSource)) {
    Warn "스킬 원본을 찾지 못했습니다: $skillSource"
} elseif ($existingLink -and -not $existingLink.LinkType -and -not $Frozen) {
    Warn "$skillLink 이 링크가 아니라서 건드리지 않았습니다"
} elseif ($Frozen) {
    if ($existingLink) { Remove-Item $skillLink -Recurse -Force }
    Copy-Item $skillSource $skillLink -Recurse -Force
    Ok "$skillLink (복사본 — 번들은 업그레이드 때 통째로 교체된다)"
} else {
    if ($existingLink) { Remove-Item $skillLink -Recurse -Force }
    # A junction, not a symbolic link: symlinks need admin or Developer Mode,
    # and asking for elevation to install a per-user scheduled job is worse
    # than the junction's one limitation (it cannot cross to a network share).
    New-Item -ItemType Junction -Path $skillLink -Target $skillSource | Out-Null
    Ok "$skillLink -> skills\daily-report"
}

# ------------------------------------------------------------------ verify ---
Write-Host ""
if ($Frozen) { & $AppExe doctor } else { & $python doctor.py }

$yesterday = (Get-Date).AddDays(-1).ToString('yyyy-MM-dd')
Write-Host ""
Write-Host "다음:"
Write-Host "  $python summarize.py x y --preflight    무인 인증 확인 (실제 호출 1회)"
Write-Host "  $python run_day.py $yesterday          어제 하루치 시험 실행"
Write-Host ""
Write-Host "첫 예약 실행은 내일 $scheduleTime 입니다. 그 전에 위 시험 실행으로 확인하세요."
