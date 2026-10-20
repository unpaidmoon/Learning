#requires -Version 7.0
<#
Read-only documentation and cached-input preparation check.
Does not launch applications, import model packages, install, download, or call APIs.
Exit 0 means document preparation passed, not that V1-V7 passed or are authorized.
#>
[CmdletBinding()]
param(
    [string]$RepositoryRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $PSScriptRoot '../..')
    ),
    [string]$VideoPath = 'D:/Download/26822053135-1-192.mp4',
    [string]$ModelSnapshot = (
        Join-Path $env:USERPROFILE (
            '.cache/huggingface/hub/' +
            'models--mobiuslabsgmbh--faster-whisper-large-v3-turbo/' +
            'snapshots/0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf'
        )
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$prepRoot = (Resolve-Path -LiteralPath $RepositoryRoot).Path
$prepErrors = [System.Collections.Generic.List[string]]::new()
$prepWarnings = [System.Collections.Generic.List[string]]::new()
$prepPlans = @(
    'V1-Harness最小路线执行方案.md',
    'V2-持久化与故障窗口执行方案.md',
    'V3-数学与试用价值执行方案.md',
    'V4-公式局部选择执行方案.md',
    'V5-文档格式与来源执行方案.md',
    'V6-字幕与前台并行执行方案.md',
    'V7-资产迁移与Windows运行执行方案.md'
)
$prepRequired = @(
    'docs/验证/README.md',
    'docs/验证/执行准备清单.md',
    'docs/验证/样本清单.md',
    'docs/验证/记录模板/run-record.template.json',
    'docs/验证/记录模板/case-result.template.json'
) + @($prepPlans | ForEach-Object { 'docs/验证/' + $_ })

foreach ($prepRelative in $prepRequired) {
    if (-not (Test-Path -LiteralPath (Join-Path $prepRoot $prepRelative) -PathType Leaf)) {
        $prepErrors.Add('Missing preparation file: ' + $prepRelative)
    }
}

if ($prepErrors.Count -eq 0) {
    try {
        $prepRunTemplate = Get-Content -LiteralPath (
            Join-Path $prepRoot 'docs/验证/记录模板/run-record.template.json'
        ) -Raw | ConvertFrom-Json
        $prepCaseTemplate = Get-Content -LiteralPath (
            Join-Path $prepRoot 'docs/验证/记录模板/case-result.template.json'
        ) -Raw | ConvertFrom-Json
        if ($prepRunTemplate.execution_status -ne 'not_started') {
            $prepErrors.Add('Run template must not claim execution.')
        }
        if ($prepCaseTemplate.status -ne 'not_run') {
            $prepErrors.Add('Case template must not claim a result.')
        }
        if ($null -ne $prepRunTemplate.budgets.observed_outbound_model_requests) {
            $prepErrors.Add('Unmeasured outbound request count must be null.')
        }
        foreach ($prepUsageName in @(
            'input_total', 'cache_hit', 'cache_miss', 'output_total', 'reasoning'
        )) {
            if ($null -ne $prepCaseTemplate.usage.$prepUsageName) {
                $prepErrors.Add('Unmeasured usage must be null: ' + $prepUsageName)
            }
        }
    }
    catch {
        $prepErrors.Add('Invalid record template: ' + $_.Exception.Message)
    }
}

$prepTools = @(
    foreach ($prepName in @('node', 'pnpm', 'uv', 'python', 'ffmpeg', 'ffprobe', 'nvidia-smi')) {
        $prepCommand = Get-Command $prepName -ErrorAction SilentlyContinue |
            Select-Object -First 1
        [pscustomobject]@{
            tool = $prepName
            discovered = ($null -ne $prepCommand)
            version_executed = $false
        }
    }
)

$prepWheelPath = Join-Path $prepRoot (
    'research/probes/harness-0.2.1rc2/' +
    'deepseek_harness_runtime_bin-0.2.1rc2-py3-none-win_amd64.whl'
)
$prepExpectedHash = 'D38C18F5117859FEEDAAA791A0EEA37B8644BC187118E7E6F3AFA231A5381CB7'
$prepWheel = [ordered]@{
    exists = (Test-Path -LiteralPath $prepWheelPath -PathType Leaf)
    bytes = $null
    sha256 = $null
    matches_reviewed_hash = $null
    runtime_started = $false
}
if ($prepWheel.exists) {
    $prepWheel.bytes = (Get-Item -LiteralPath $prepWheelPath).Length
    $prepWheel.sha256 = (Get-FileHash -LiteralPath $prepWheelPath -Algorithm SHA256).Hash
    $prepWheel.matches_reviewed_hash = ($prepWheel.sha256 -eq $prepExpectedHash)
    if (-not $prepWheel.matches_reviewed_hash) {
        $prepWarnings.Add('Cached runtime hash differs; V1 must not use it as reviewed.')
    }
}
else {
    $prepWarnings.Add('Cached runtime absent; obtain authorized artifact before V1.')
}

$prepVideo = [ordered]@{
    exists = (Test-Path -LiteralPath $VideoPath -PathType Leaf)
    bytes = $null
    decoded = $false
}
if ($prepVideo.exists) {
    $prepVideo.bytes = (Get-Item -LiteralPath $VideoPath).Length
}
else {
    $prepWarnings.Add('Registered video unavailable; media/ASR prerequisites remain missing.')
}

$prepModel = [ordered]@{
    snapshot_entry_exists = $false
    is_symbolic_link = $null
    target_exists = $null
    target_bytes = $null
    weights_read = $false
    model_loaded = $false
}
$prepModelEntryPath = Join-Path $ModelSnapshot 'model.bin'
try {
    $prepModelEntry = Get-Item -LiteralPath $prepModelEntryPath -Force
    $prepModel.snapshot_entry_exists = $true
    $prepModel.is_symbolic_link = ($null -ne $prepModelEntry.LinkTarget)
    $prepModelTarget = $prepModelEntry
    if ($prepModel.is_symbolic_link) {
        $prepModelTarget = $prepModelEntry.ResolveLinkTarget($true)
    }
    $prepModel.target_exists = ($null -ne $prepModelTarget -and $prepModelTarget.Exists)
    if ($prepModel.target_exists) {
        $prepModel.target_bytes = $prepModelTarget.Length
    }
    else {
        $prepWarnings.Add('Model snapshot target unavailable; do not infer usable weights.')
    }
}
catch {
    $prepWarnings.Add('Model target metadata not verified; no model loading attempted.')
}

$prepResult = [ordered]@{
    check_kind = 'read_only_validation_preparation'
    document_preparation_passed = ($prepErrors.Count -eq 0)
    validation_plan_count = $prepPlans.Count
    validation_execution_status = 'not_started'
    ready_to_execute_experiments = $false
    authorization_checked_by_script = $false
    tool_path_discovery = $prepTools
    cached_runtime = $prepWheel
    registered_video = $prepVideo
    registered_model = $prepModel
    remaining_execution_preconditions = @(
        'Explicit execution/installation/real-input authorization as applicable',
        'Reviewed validation runner, learning profile, fixtures and outbound budget guard',
        'Human-reviewed expectations and user-confirmed real images/handwriting',
        'Implemented business features for V2-V7 application-level checks',
        'Fixed dependency closures, actual documents/audio truth, and delivery environment'
    )
    errors = @($prepErrors)
    warnings = @($prepWarnings)
}
$prepResult | ConvertTo-Json -Depth 8
if ($prepErrors.Count -gt 0) {
    exit 1
}
exit 0
