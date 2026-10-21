param(
    [switch]$NoBrowser,
    [switch]$NoHarness,
    [int]$Port = 8765,
    [double]$TimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskPython = Join-Path $taskRoot 'backend/.venv/Scripts/python.exe'
$taskEntry = Join-Path $taskRoot 'scripts/app.py'
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    throw 'PROJECT_ENVIRONMENT_MISSING: 请先按 README 初始化项目环境。'
}
$taskArguments = @($taskEntry, 'launch', '--backend-port', "$Port", '--timeout', "$TimeoutSeconds")
if (-not $NoBrowser) { $taskArguments += '--open' }
if ($NoHarness) { $taskArguments += '--no-harness' } else { $taskArguments += '--harness' }
& $taskPython @taskArguments
if ($LASTEXITCODE -ne 0) { throw 'LAUNCH_FAILED: 学习应用未就绪，请查看上方错误。' }
Write-Output '关闭网页后后台继续；使用学习页面中的“安全退出”停止本次实例。'
