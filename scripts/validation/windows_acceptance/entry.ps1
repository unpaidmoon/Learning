param(
    [ValidateSet('Auto', 'Manual', 'Report', 'Collect')]
    [string]$Mode = 'Auto',
    [switch]$AutomatedOnly
)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$kitRoot = Split-Path -Parent $PSScriptRoot
$config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'kit.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$candidate = Join-Path $kitRoot $config.release_id
$python = Join-Path $candidate 'runtime\python\python.exe'
$resultRoot = Join-Path $kitRoot '验收结果'
[void][System.IO.Directory]::CreateDirectory($resultRoot)
$utf8 = New-Object System.Text.UTF8Encoding($false)
function Save-Json([string]$Path, $Value) {
    [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 12), $utf8)
}
function Check-Tools {
    foreach ($file in $config.tools) {
        $path = Join-Path $PSScriptRoot $file.path
        if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256) {
            throw "ACCEPTANCE_TOOL_HASH_MISMATCH: $($file.path)"
        }
    }
    if ((Get-FileHash -LiteralPath $python -Algorithm SHA256).Hash.ToLowerInvariant() -ne $config.python_sha256) {
        throw 'ACCEPTANCE_PYTHON_HASH_MISMATCH'
    }
    if ((Get-FileHash -LiteralPath (Join-Path $candidate 'desktop-release.json') -Algorithm SHA256).Hash.ToLowerInvariant() -ne $config.release_manifest_sha256) {
        throw 'ACCEPTANCE_RELEASE_IDENTITY_MISMATCH'
    }
}
try {
    if ($Mode -eq 'Report') {
        Get-Content -LiteralPath (Join-Path $resultRoot '结果.txt') -Encoding UTF8
        exit 0
    }
    if ($Mode -eq 'Collect') {
        $files = @('环境.json', '自动摘要.json', '离线摘要.json', '结果.txt', '手动回报.txt') |
            ForEach-Object { Join-Path $resultRoot $_ } | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
        if (-not $files) { throw 'NO_ACCEPTANCE_RESULT_AVAILABLE' }
        $destination = Join-Path $resultRoot ('验收摘要-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6) + '.zip')
        Compress-Archive -LiteralPath $files -DestinationPath $destination
        Write-Host ('已汇总摘要：' + $destination)
        Write-Host '摘要不包含学习数据库、账号配置、浏览器目录、运行日志或手动窗口截图。'
        exit 0
    }
    Check-Tools
    if ($Mode -eq 'Auto') {
        $os = Get-CimInstance Win32_OperatingSystem
        $machine = Get-CimInstance Win32_ComputerSystem
        $commands = foreach ($name in @('python.exe', 'python3.exe', 'node.exe', 'rustc.exe', 'pnpm.cmd', 'uv.exe')) {
            $command = Get-Command $name -ErrorAction SilentlyContinue
            [ordered]@{ name = $name; found = [bool]$command; source = if ($command) { $command.Source } else { $null } }
        }
        Save-Json (Join-Path $resultRoot '环境.json') ([ordered]@{
            os = $os.Caption; version = $os.Version; build = $os.BuildNumber
            manufacturer = $machine.Manufacturer; model = $machine.Model
            memory_bytes = $machine.TotalPhysicalMemory; logical_processors = $machine.NumberOfLogicalProcessors
            powershell = $PSVersionTable.PSVersion.ToString()
            elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
            external_commands = @($commands); release_id = $config.release_id
            clean_environment = 'requires_user_confirmation; command discovery is recorded, not a full installed-software audit'
        })
        $identity = 'guest-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
        Write-Host '正在校验固定运行包并检查独立窗口、后台恢复和草稿保存，请等待。'
        & $python -I (Join-Path $PSScriptRoot 'Run-PortableRecovery.py') --candidate $candidate --run-id $identity --output-root $resultRoot
        $testExit = $LASTEXITCODE
        $summaryPath = Join-Path (Join-Path $resultRoot $identity) 'summary.json'
        if (-not (Test-Path -LiteralPath $summaryPath -PathType Leaf)) {
            throw ('验收未生成摘要，请保留屏幕上的错误。退出码：' + $testExit)
        }
        $summary = Get-Content -LiteralPath $summaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Save-Json (Join-Path $resultRoot '自动摘要.json') $summary
        Save-Json (Join-Path $resultRoot 'latest.json') ([ordered]@{
            release_id = $config.release_id; profile = $summary.owned_profile; port = $summary.backend_port
        })
        $passed = @($summary.assertions | Where-Object { $_.passed }).Count
        $text = @(
            'AI 辅助学习 Windows 虚拟机验收'
            ('运行包：' + $config.release_id)
            ('系统：' + $os.Caption + ' ' + $os.Version)
            ('虚拟机／设备型号：' + $machine.Model)
            ('自动检查：' + $summary.status + '，通过 ' + $passed + '/' + @($summary.assertions).Count + ' 项')
            ('错误：' + $summary.error)
            '自动流程只使用自造文字和草稿，模型请求 0，系统启动项修改 0。'
            '请另回报未安装开发工具的环境条件，以及手动检查的逐项结果；未做项写“未测”。'
        ) -join [Environment]::NewLine
        [System.IO.File]::WriteAllText((Join-Path $resultRoot '结果.txt'), $text, $utf8)
        $manual = @(
            '请填写或直接在聊天回复下列结果；passed / failed / 未测。'
            '1. 本虚拟机未安装 Python、Node、Rust 等开发工具：'
            '2. 自动检查的结果与失败提示：'
            '3. TXT／PDF阅读、视频播放及全屏：'
            '4. 确认截图加入草稿、关闭窗口再打开后草稿保留：'
            '5. 从软件菜单／托盘明确退出后，再打开仍可读取原内容：'
            '6. 登录Windows后启动（仅在虚拟机中启用并实际重登后判断）：'
            '7. 系统睡眠／唤醒后恢复（未执行则写未测）：'
            '8. 无GPU／缺驱动时的本地处理提示（未执行则写未测）：'
            '其他现象：'
        ) -join [Environment]::NewLine
        [System.IO.File]::WriteAllText((Join-Path $resultRoot '手动回报.txt'), $manual, $utf8)
        Write-Host $text
        if ($testExit -ne 0 -or $summary.status -ne 'passed') { exit 1 }
        Write-Host '正在用独立离线窗口检查实际交互书、参数图和回忆卡片；云电脑网络连接保持不变。'
        $offlineOutput = Join-Path (Join-Path $resultRoot $identity) 'offline'
        [void][System.IO.Directory]::CreateDirectory($offlineOutput)
        & $python -I (Join-Path $PSScriptRoot 'offline_probe.py') --kit $kitRoot --output $offlineOutput --application-db (Join-Path $summary.owned_profile 'data/business.sqlite3')
        $offlineExit = $LASTEXITCODE
        $offlineSummary = Get-Content -LiteralPath (Join-Path $offlineOutput 'offline-summary.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        Save-Json (Join-Path $resultRoot '离线摘要.json') $offlineSummary
        [System.IO.File]::AppendAllText((Join-Path $resultRoot '结果.txt'), ([Environment]::NewLine + '离线实际成果检查：' + $offlineSummary.status), $utf8)
        if ($offlineExit -ne 0 -or $offlineSummary.status -ne 'passed') { exit 1 }
        $fixtureDirectory = Join-Path (Split-Path -Parent $summaryPath) 'synthetic-course'
        Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot '验收素材') -File | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $fixtureDirectory $_.Name)
        }
    }
    if ($AutomatedOnly) { exit 0 }
    Write-Host '正在打开独立的手动验收窗口。检查步骤见包内“验收说明.txt”。'
    & $python -I (Join-Path $PSScriptRoot 'manual_launch.py') --kit $kitRoot
    exit $LASTEXITCODE
} catch {
    Write-Host ('验收入口未完成：' + $_.Exception.Message)
    exit 1
}
