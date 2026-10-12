$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$python = if ($env:VMAKER_PYTHON) { $env:VMAKER_PYTHON } else { 'python' }
$problems = 0

function Test-Tool([string]$Name, [string]$Hint) {
    if (Get-Command $Name -ErrorAction SilentlyContinue) {
        Write-Host "[OK] $Name" -ForegroundColor Green
    } else {
        Write-Host "[MISSING] $Name - $Hint" -ForegroundColor Yellow
        $script:problems += 1
    }
}

Write-Host 'V-Maker environment diagnostics' -ForegroundColor Cyan
Test-Tool $python 'Install Python 3.12+ or set VMAKER_PYTHON'
Test-Tool 'pnpm' 'Install pnpm 9+'
Test-Tool 'ffmpeg' 'Optional: draft ZIP is used when unavailable'
Test-Tool 'rustc' 'Optional: required for Tauri desktop builds'
Test-Tool 'cargo' 'Optional: required for Tauri desktop builds'

if (Get-Command $python -ErrorAction SilentlyContinue) {
    & $python -c "import fastapi, pydantic, uvicorn; print('[OK] Python backend packages')"
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[MISSING] Python packages. Run: python -m pip install -e ".[dev]"' -ForegroundColor Yellow
        $problems += 1
    }
}

if (Test-Path (Join-Path $root 'node_modules')) {
    Write-Host '[OK] Frontend dependencies' -ForegroundColor Green
} else {
    Write-Host '[MISSING] node_modules. Run: pnpm install' -ForegroundColor Yellow
    $problems += 1
}

if ($problems -eq 0) {
    Write-Host 'Environment satisfies full development requirements.' -ForegroundColor Green
} else {
    Write-Host "Found $problems missing items. FFmpeg and Rust are optional for the web MVP." -ForegroundColor Yellow
}
exit 0
