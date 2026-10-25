$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$pythonPath = Join-Path $projectRoot 'backend/.venv/Scripts/python.exe'
$desktopScript = Join-Path $PSScriptRoot 'run.py'
$env:PYTHONIOENCODING = 'utf-8'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'PROJECT_ENVIRONMENT_MISSING：请先准备本项目 Python 环境。'
}
$launchRoot = Join-Path $projectRoot '.runtime/desktop/launches'
$launchPath = Join-Path $launchRoot ([guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $launchPath -Force | Out-Null
# Start-Process launches only this project's runner. The visible Electron
# application window is created after its owned backend reports readiness.
$runner = Start-Process -PassThru -FilePath $pythonPath -ArgumentList @('"' + $desktopScript + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $launchPath 'stdout.log') -RedirectStandardError (Join-Path $launchPath 'stderr.log')
# Retain the Windows process handle before a short-lived child exits.
$null = $runner.Handle
$stdoutPath = Join-Path $launchPath 'stdout.log'
$stderrPath = Join-Path $launchPath 'stderr.log'
while ($true) {
    $runner.Refresh()
    if ($runner.HasExited) {
        $runner.WaitForExit()
        $code = $runner.ExitCode
        if ($code -eq 0) { exit 0 }
        if (Test-Path -LiteralPath $stderrPath) { Get-Content -LiteralPath $stderrPath -Encoding UTF8 | ForEach-Object { [Console]::Error.WriteLine($_) } }
        [Console]::Error.WriteLine("桌面启动失败，日志：$launchPath")
        if ($null -eq $code) { exit 1 }
        exit $code
    }
    if (Test-Path -LiteralPath $stdoutPath) {
        $output = Get-Content -LiteralPath $stdoutPath -Encoding UTF8 -Raw
        if ($output -match '(?m)^LEARNING_DESKTOP_(READY |ACTIVATED)') { exit 0 }
    }
    Start-Sleep -Milliseconds 100
}
