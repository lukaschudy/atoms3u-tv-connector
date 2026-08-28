param(
    [switch]$SkipPrepare
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    } else {
        throw "Python 3.11 or newer is required. Install it from https://python.org/downloads/."
    }
    if ($LASTEXITCODE -ne 0) {
        throw "The isolated Python environment could not be created."
    }
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
& $Python scripts\check_python.py
if ($LASTEXITCODE -ne 0) {
    throw "The isolated Python environment must use Python 3.11 or newer."
}
& $Python -m pip install --disable-pip-version-check `
    -r requirements-tv-connector.txt `
    -r room_agent\requirements-windows.txt
if ($LASTEXITCODE -ne 0) {
    throw "The connector tooling could not be installed."
}
& $Python -m pip check
if ($LASTEXITCODE -ne 0) {
    throw "The existing .venv is incompatible. Remove it and run setup again."
}
& $Python scripts\check_python.py --installed
if ($LASTEXITCODE -ne 0) {
    throw "The existing .venv is incompatible. Remove it and run setup again."
}

Write-Host ""
Write-Host "Tooling is ready."
if (-not $SkipPrepare) {
    & $Python room_agent\setup_tv_connector.py prepare
    if ($LASTEXITCODE -ne 0) {
        throw "Connector preparation did not complete."
    }
}

Write-Host ""
Write-Host "Next steps:"
Write-Host "  .\.venv\Scripts\python.exe room_agent\setup_tv_connector.py doctor"
Write-Host "  .\.venv\Scripts\python.exe room_agent\setup_tv_connector.py flash"
