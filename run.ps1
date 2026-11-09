# Yuketang auto-detect + auto-learn launcher for PowerShell (ASCII text on purpose)
#
#   cd D:\code\yuketang-bot
#   .\run.ps1 --help
#   .\run.ps1 --auto --cookies "csrftoken=..; sessionid=.."
#   .\run.ps1 --auto --cookies @cookie.txt
#
# If script execution is blocked, either use .\run.bat instead, or run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1 --auto

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

function Test-Python([string]$exe) {
    if (-not $exe) { return $false }
    if ($exe -notmatch '[\\/]') {
        $cmd = Get-Command $exe -ErrorAction SilentlyContinue
        if (-not $cmd) { return $false }
    } elseif (-not (Test-Path $exe)) { return $false }
    try {
        $null = & $exe -c "import sys" 2>&1
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$candidates = @()
if ($env:YKT_PYTHON) { $candidates += $env:YKT_PYTHON }
$candidates += (Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-next-primary-runtime\dependencies\python\python.exe')
$candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe')
$candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')
$candidates += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')
$candidates += 'C:\Python312\python.exe'
$candidates += 'C:\Python311\python.exe'
$candidates += 'py'
$candidates += 'python'

$py = $null
foreach ($c in $candidates) {
    if (Test-Python $c) { $py = $c; break }
}

if (-not $py) {
    Write-Host ''
    Write-Host '[X] No usable Python 3.9+ found.' -ForegroundColor Red
    Write-Host '    Install: https://www.python.org/downloads/windows/  (check "Add python.exe to PATH")'
    Write-Host '    Or: $env:YKT_PYTHON="C:\Python312\python.exe"  then rerun .\run.ps1'
    Write-Host '    Note: the python.exe under WindowsApps is a Store stub and does nothing.'
    Write-Host ''
    exit 2
}

Write-Host "[i] python: $py" -ForegroundColor DarkGray
& $py 'main.py' @args
$rc = $LASTEXITCODE
if ($rc -eq 0) { exit 0 }
$hint = switch ($rc) {
    2 { '[X] exit 2: cookie missing - paste the browser Cookie into config.json cookie_string, or use --cookies' }
    3 { '[X] exit 3: session expired - grab a fresh cookie' }
    4 { '[X] exit 4: endpoint detection failed - try --redetect --dump' }
    5 { '[X] exit 5: no course selected - use --course / --index / --auto' }
    6 { '[!] exit 6: some leaves failed - rerunning usually finishes them' }
    default { '' }
}
if ($hint) { Write-Host $hint -ForegroundColor Yellow }
exit $rc
