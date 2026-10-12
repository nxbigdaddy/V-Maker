# V-Maker 全量测试：前端类型检查与单测、生产构建、后端 pytest。
#
# 每一步都会跑完（不会一失败就退出），最后统一汇报，便于一次看清所有问题。
# 直接调用仓库内的本地二进制，不依赖 PATH 上是否有 pnpm。
#
# 用法：pnpm test:all   或   powershell -ExecutionPolicy Bypass -File scripts/test-all.ps1

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = if ($env:VMAKER_PYTHON) { $env:VMAKER_PYTHON } else { 'python' }
$results = [System.Collections.Generic.List[object]]::new()

function Invoke-Step {
    param([string]$Name, [scriptblock]$Body)
    Write-Host ''
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    & $Body
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    $results.Add([pscustomobject]@{ Step = $Name; Exit = $code })
    if ($code -ne 0) { Write-Host "[失败] $Name (exit $code)" -ForegroundColor Red }
    else { Write-Host "[通过] $Name" -ForegroundColor Green }
}

Invoke-Step '脚本编码检查 (PowerShell BOM)' {
    node scripts/check-script-encoding.mjs
}

Invoke-Step '前端类型检查 (tsc -b)' {
    node node_modules/typescript/bin/tsc -b --pretty false
}

Invoke-Step '前端单元测试 (vitest)' {
    node node_modules/vitest/vitest.mjs run
}

Invoke-Step '前端生产构建 (vite build)' {
    node node_modules/vite/bin/vite.js build
}

# 桌面壳：本机没有 Rust 与 MSVC 生成工具，无法真正构建，所以至少把「不需要编译器就能查」的部分查掉。
# 放在生产构建之后，因为它要求 dist/index.html 已经存在。
Invoke-Step 'Tauri 配置静态检查' {
    node scripts/check-tauri-config.mjs
}

Invoke-Step '后端测试 (pytest)' {
    # 本机全局 pytest_asyncio 与 pytest 不兼容，禁用外部插件自动加载。
    $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
    & $python -m pytest -q
}

Write-Host ''
Write-Host '===== 全量测试汇总 =====' -ForegroundColor Cyan
$failed = 0
foreach ($item in $results) {
    $ok = $item.Exit -eq 0
    if (-not $ok) { $failed += 1 }
    $mark = if ($ok) { '通过' } else { '失败' }
    $color = if ($ok) { 'Green' } else { 'Red' }
    Write-Host ("  [{0}] {1}" -f $mark, $item.Step) -ForegroundColor $color
}

if ($failed -gt 0) {
    Write-Host ''
    Write-Host "$failed 个步骤失败。" -ForegroundColor Red
    exit 1
}
Write-Host ''
Write-Host '全部通过。' -ForegroundColor Green
exit 0
