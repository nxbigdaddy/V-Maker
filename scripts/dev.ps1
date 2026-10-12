$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = if ($env:VMAKER_PYTHON) { $env:VMAKER_PYTHON } else { "python" }

Write-Host "Starting V-Maker backend..." -ForegroundColor Cyan
$backend = Start-Process -FilePath $python `
    -ArgumentList @("-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "8765", "--reload") `
    -WorkingDirectory $root `
    -PassThru

try {
    Write-Host "Starting V-Maker frontend..." -ForegroundColor Cyan
    pnpm dev:web
}
finally {
    if ($backend -and -not $backend.HasExited) {
        Stop-Process -Id $backend.Id
    }
}
