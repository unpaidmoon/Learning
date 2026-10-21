param(
    [string]$NodeSource,
    [string]$PnpmSource,
    [string]$UvSource,
    [switch]$SkipBrowsers
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$toolRoot = Join-Path $projectRoot '.tools'
$nodeVersion = 'v24.14.0'
$pnpmVersion = '11.21.0'
$uvVersion = '0.12.1'
$nodePath = Join-Path $toolRoot 'node/node.exe'
$uvPath = Join-Path $toolRoot 'uv/uv.exe'
$pnpmPath = Join-Path $toolRoot 'pnpm/bin/pnpm.cjs'

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "工具执行失败（退出码 $LASTEXITCODE）：$Executable"
    }
}

if (-not $NodeSource) {
    $NodeSource = if (Test-Path -LiteralPath $nodePath) { $nodePath } else { (Get-Command node.exe -ErrorAction Stop).Source }
}
if (-not $UvSource) {
    $UvSource = if (Test-Path -LiteralPath $uvPath) { $uvPath } else { (Get-Command uv.exe -ErrorAction Stop).Source }
}
if (-not $PnpmSource) {
    $cachedPnpm = if (Test-Path -LiteralPath $pnpmPath) {
        Join-Path $toolRoot 'pnpm'
    } else {
        Join-Path $env:LOCALAPPDATA "node/corepack/v1/pnpm/$pnpmVersion"
    }
    if (Test-Path -LiteralPath (Join-Path $cachedPnpm 'bin/pnpm.cjs')) { $PnpmSource = $cachedPnpm }
}
if (-not $PnpmSource -or -not (Test-Path -LiteralPath (Join-Path $PnpmSource 'bin/pnpm.cjs'))) {
    throw "未找到固定 pnpm 缓存。请用 -PnpmSource 指定 pnpm $pnpmVersion 的解包目录；不会修改全局工具。"
}
if ((& $NodeSource --version) -ne $nodeVersion) { throw "需要 Node $nodeVersion。" }
if ((& $NodeSource (Join-Path $PnpmSource 'bin/pnpm.cjs') --version) -ne $pnpmVersion) {
    throw "需要 pnpm $pnpmVersion。"
}
if ((& $UvSource --version) -notmatch "^uv $([regex]::Escape($uvVersion))\b") { throw "需要 uv $uvVersion。" }

# Verified existing tools become project-local copies; no PATH/HOME changes,
# installer, model download, or probe environment is part of this bootstrap.
New-Item -ItemType Directory -Path (Join-Path $toolRoot 'node'), (Join-Path $toolRoot 'uv') -Force | Out-Null
if (-not (Test-Path -LiteralPath $nodePath)) { Copy-Item -LiteralPath $NodeSource -Destination $nodePath }
if (-not (Test-Path -LiteralPath $uvPath)) { Copy-Item -LiteralPath $UvSource -Destination $uvPath }
if (-not (Test-Path -LiteralPath $pnpmPath)) {
    Copy-Item -LiteralPath $PnpmSource -Destination (Join-Path $toolRoot 'pnpm') -Recurse
}
if ((& $nodePath --version) -ne $nodeVersion) { throw '本项目 Node 版本不符。' }
if ((& $nodePath $pnpmPath --version) -ne $pnpmVersion) { throw '本项目 pnpm 版本不符。' }
if ((& $uvPath --version) -notmatch "^uv $([regex]::Escape($uvVersion))\b") { throw '本项目 uv 版本不符。' }

$manifest = [ordered]@{
    node = @{ version = $nodeVersion; sha256 = (Get-FileHash -LiteralPath $nodePath -Algorithm SHA256).Hash }
    pnpm = @{ version = $pnpmVersion; entry_sha256 = (Get-FileHash -LiteralPath $pnpmPath -Algorithm SHA256).Hash }
    uv = @{ version = $uvVersion; sha256 = (Get-FileHash -LiteralPath $uvPath -Algorithm SHA256).Hash }
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $toolRoot 'toolchain.json') -Encoding utf8

Push-Location $projectRoot
try {
    Invoke-Checked $uvPath @('sync', '--project', 'backend', '--frozen')
    Invoke-Checked $nodePath @($pnpmPath, 'install', '--frozen-lockfile')
    $pythonPath = Join-Path $projectRoot 'backend/.venv/Scripts/python.exe'
    Invoke-Checked $pythonPath @('-c', 'import sys, sqlite3; print("Python", sys.version.split()[0]); print("SQLite", sqlite3.sqlite_version)')
    if (-not $SkipBrowsers) {
        Invoke-Checked $nodePath @($pnpmPath, 'exec', 'playwright-cli', 'install-browser', 'chromium')
    }
    Write-Output '项目锁定环境准备完成；工具位于 .tools，不修改全局环境。'
} finally {
    Pop-Location
}
