param(
  [string]$BaseUrl = 'http://127.0.0.1:1420',
  [string]$Widths = '320,420,700,760,1280',
  [string]$Heights = '420,760',
  [string]$Scenarios = '',
  [string]$Report = 'ui-layout-results.json',
  # Text the page must contain (used by the unreachable-backend check to prove the message names the
  # address that is actually configured instead of a hardcoded one).
  [string]$ExpectText = '',
  # Registers the "backend unreachable" scenario. It can only pass when the app under test points at
  # a dead API port, so it must never be part of a normal run.
  [switch]$Offline,
  # Production preview serves dist/ only, while the probe lives in scripts/. This switch stages the
  # probe into the built output and removes it afterwards, so "check the build" is repeatable
  # instead of relying on a manual copy.
  [switch]$StageProbe
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
if (!(Test-Path $chrome)) { throw 'Chrome not found' }

$staged = Join-Path $root 'dist\scripts\ui-probe.html'
if ($StageProbe) {
  $source = Join-Path $root 'scripts\ui-probe.html'
  if (!(Test-Path $source)) { throw "probe source missing: $source" }
  if (!(Test-Path (Join-Path $root 'dist'))) { throw 'dist/ missing - run the production build first' }
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $staged) | Out-Null
  Copy-Item $source $staged -Force
}

try {
  $url = "$BaseUrl/scripts/ui-probe.html?widths=$Widths&heights=$Heights"
  if ($Scenarios) { $url += '&scenarios=' + [uri]::EscapeDataString($Scenarios) }
  if ($ExpectText) { $url += '&expectText=' + [uri]::EscapeDataString($ExpectText) }
  if ($Offline) { $url += '&offline=1' }
  $dump = Join-Path $env:TEMP ('vmaker-layout-' + [guid]::NewGuid().ToString('N') + '.html')
  $err = "$dump.log"
  # PowerShell treats native stderr as an error even for harmless Chrome diagnostics.
  $ErrorActionPreference = 'Continue'
  & $chrome --headless=new --disable-gpu --virtual-time-budget=1200000 --dump-dom $url 2> $err | Out-File -Encoding utf8 $dump
  $ErrorActionPreference = 'Stop'
  $html = [System.IO.File]::ReadAllText($dump)
  $match = [regex]::Match($html, '<script id="probe-result" type="application/json">(.*?)</script>', 'Singleline')
  if (!$match.Success) { throw "Probe did not return JSON. Inspect $dump and $err" }
  $result = $match.Groups[1].Value | ConvertFrom-Json
  $match.Groups[1].Value | Out-File -Encoding utf8 $Report
  Write-Host ("UI layout: {0}; {1} cases; report: {2}" -f $result.status, $result.cases.Count, $Report)
  $result.cases | Where-Object { $_.status -ne 'passed' } | ForEach-Object {
    Write-Host ("{0}x{1} {2}" -f $_.width, $_.height, $_.scenario)
    $_.issues | ForEach-Object { Write-Host ("  {0}: {1}" -f $_.kind, $_.detail) }
  }
  if ($result.status -ne 'passed' -or $result.cases.Count -eq 0) { exit 1 }
  exit 0
} finally {
  # The probe is a development tool; never leave it inside the build output.
  if ($StageProbe -and (Test-Path $staged)) {
    Remove-Item $staged -Force
    $dir = Split-Path -Parent $staged
    if ((Test-Path $dir) -and -not (Get-ChildItem $dir -Force)) { Remove-Item $dir -Force }
  }
}
