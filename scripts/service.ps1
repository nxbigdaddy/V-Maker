param(
  [Parameter(Position = 0)]
  [ValidateSet('start', 'stop', 'restart', 'status', 'logs', 'check')]
  [string]$Command = 'status',
  [int]$Port = 8765,
  [int]$WaitSeconds = 40,
  [int]$Tail = 40
)

# V-Maker backend service manager.
#
# Why this exists: the launcher used to start uvicorn with `start ... cmd /k`, which leaves a window
# nobody can query and a process nobody can reliably stop. This script owns the process instead:
# one PID file, one log file, one place to ask "is it up, which process, which port".
#
# NOTE: keep this file ASCII-only. Windows PowerShell reads a BOM-less file as ANSI, so non-ASCII
# comments break parsing (scripts/check-script-encoding.mjs enforces the BOM rule for other files).

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $root 'backend\data'
$logDir = Join-Path $dataDir 'logs'
$pidFile = Join-Path $dataDir 'backend.pid'
$outLog = Join-Path $logDir 'backend.out.log'
$errLog = Join-Path $logDir 'backend.err.log'
$baseUrl = "http://127.0.0.1:$Port"
$python = if ($env:VMAKER_PYTHON) { $env:VMAKER_PYTHON } else { 'python' }

function Get-ListenerPid {
  $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Select-Object -First 1
  if ($listener) { return [int]$listener.OwningProcess }
  return 0
}

function Test-Health {
  try {
    $response = Invoke-WebRequest -UseBasicParsing "$baseUrl/api/health" -TimeoutSec 3
    return ($response.StatusCode -eq 200)
  } catch {
    return $false
  }
}

function Get-RecordedPid {
  if (!(Test-Path $pidFile)) { return 0 }
  $raw = (Get-Content $pidFile -Raw -ErrorAction SilentlyContinue)
  $value = 0
  if ([int]::TryParse(($raw -as [string]).Trim(), [ref]$value)) { return $value }
  return 0
}

function Stop-Backend {
  $pids = @()
  $recorded = Get-RecordedPid
  if ($recorded -gt 0) { $pids += $recorded }
  $listening = Get-ListenerPid
  if ($listening -gt 0 -and $pids -notcontains $listening) { $pids += $listening }

  if ($pids.Count -eq 0) {
    Write-Host '[INFO] Backend is not running.'
    if (Test-Path $pidFile) { Remove-Item $pidFile -Force }
    return 0
  }

  foreach ($processId in $pids) {
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($process) {
      Write-Host "[INFO] Stopping backend PID $processId ($($process.ProcessName))..."
      Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
  }
  # Wait for the port to actually free up; reporting "stopped" while it still listens is a lie.
  for ($i = 0; $i -lt 40; $i++) {
    if ((Get-ListenerPid) -eq 0) { break }
    Start-Sleep -Milliseconds 250
  }
  if (Test-Path $pidFile) { Remove-Item $pidFile -Force }
  if ((Get-ListenerPid) -ne 0) {
    Write-Host "[ERROR] Port $Port is still listening after the stop attempt." -ForegroundColor Red
    return 1
  }
  Write-Host '[OK] Backend stopped.'
  return 0
}

function Start-Backend {
  $existing = Get-ListenerPid
  if ($existing -gt 0) {
    Write-Host "[INFO] Port $Port is already served by PID $existing - reusing it."
    Set-Content -Path $pidFile -Value $existing -Encoding ASCII
    if (Test-Health) { Write-Host "[OK] Backend is healthy on $baseUrl"; return 0 }
    Write-Host "[WARN] Something listens on $Port but /api/health did not answer 200." -ForegroundColor Yellow
    return 1
  }

  New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  if (!(Test-Path $logDir)) { Write-Host "[ERROR] Cannot create $logDir" -ForegroundColor Red; return 1 }

  Write-Host "[INFO] Starting backend with: $python"
  Write-Host "[INFO] Logs: $outLog"
  # Hidden window + PID we own: no stray console, and stop/restart can find it later.
  $process = Start-Process -FilePath $python `
    -ArgumentList @('-m', 'uvicorn', 'backend.main:app', '--host', '127.0.0.1', '--port', "$Port") `
    -WorkingDirectory $root -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog
  Set-Content -Path $pidFile -Value $process.Id -Encoding ASCII

  for ($i = 0; $i -lt ($WaitSeconds * 4); $i++) {
    if (Test-Health) {
      Write-Host "[OK] Backend is ready on $baseUrl (PID $($process.Id))"
      return 0
    }
    if ($process.HasExited) {
      Write-Host "[ERROR] Backend exited immediately (code $($process.ExitCode))." -ForegroundColor Red
      if (Test-Path $errLog) { Get-Content $errLog -Tail 20 | ForEach-Object { Write-Host "  $_" } }
      return 1
    }
    Start-Sleep -Milliseconds 250
  }
  Write-Host "[ERROR] Backend did not become healthy within $WaitSeconds seconds." -ForegroundColor Red
  if (Test-Path $errLog) { Get-Content $errLog -Tail 20 | ForEach-Object { Write-Host "  $_" } }
  return 1
}

function Get-Status {
  $listener = Get-ListenerPid
  $recorded = Get-RecordedPid
  $healthy = $false
  $details = $null
  if ($listener -gt 0) {
    try {
      $details = Invoke-RestMethod "$baseUrl/api/system/diagnostics" -TimeoutSec 5
      $healthy = $true
    } catch {
      $healthy = $false
    }
  }

  Write-Host '===== V-Maker backend ====='
  if ($listener -eq 0) {
    Write-Host "  state      : stopped"
    Write-Host "  port       : $Port (nothing listening)"
  } else {
    Write-Host "  state      : $([string]::Join('', @('running', $(if ($healthy) { ', healthy' } else { ', not answering' }))))"
    Write-Host "  port       : $Port"
    Write-Host "  pid        : $listener"
    if ($recorded -gt 0 -and $recorded -ne $listener) {
      Write-Host "  pid file   : $recorded (stale - the listener is $listener)" -ForegroundColor Yellow
    } elseif ($recorded -gt 0) {
      Write-Host "  pid file   : $recorded (managed by this script)"
    } else {
      Write-Host "  pid file   : none (started outside the manager)"
    }
  }
  Write-Host "  log        : $outLog"
  if ($details) {
    Write-Host "  schema     : v$($details.schema_version)"
    $ffmpeg = if ($details.ffmpeg.available) { "yes ($($details.ffmpeg.version), source: $($details.ffmpeg.source))" } else { 'no' }
    Write-Host "  ffmpeg     : $ffmpeg"
    $jianying = if ($details.jianying.draft_generation) { "yes ($($details.jianying.version))" } else { 'no' }
    Write-Host "  jianying   : $jianying"
    Write-Host "  python     : $python"
  }
  if ($listener -eq 0) { return 1 }
  if (!$healthy) { return 1 }
  return 0
}

switch ($Command) {
  'start' { exit (Start-Backend) }
  'stop' { exit (Stop-Backend) }
  'restart' {
    $code = Stop-Backend
    if ($code -ne 0) { exit $code }
    Start-Sleep -Milliseconds 400
    exit (Start-Backend)
  }
  'status' { exit (Get-Status) }
  'logs' {
    $target = if (Test-Path $outLog) { $outLog } else { $null }
    if (!$target) { Write-Host "[INFO] No log yet at $outLog"; exit 0 }
    Get-Content $target -Tail $Tail
    if ((Test-Path $errLog) -and (Get-Item $errLog).Length -gt 0) {
      Write-Host "--- stderr (last $Tail) ---"
      Get-Content $errLog -Tail $Tail
    }
    exit 0
  }
  'check' {
    Write-Host "[INFO] python: $python"
    $health = if (Test-Health) { 'ok' } else { 'unreachable' }
    Write-Host "[INFO] health: $health"
    exit (Get-Status)
  }
}
