[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = (Get-Item -LiteralPath $PSScriptRoot).Parent.Parent.Parent.Parent.FullName
$manifestPath = Join-Path $PSScriptRoot 'Cargo.toml'
$schedulerPath = Join-Path $PSScriptRoot '../../upstream/src-tauri/src/fsrs_scheduler.rs'
$expectedSchedulerSha256 = '62aad68b7b4a2fe1f0d43d5f2059acc35970c5a30ff6672a434be310f85c6686'
$actualSchedulerSha256 = (Get-FileHash -LiteralPath $schedulerPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualSchedulerSha256 -ne $expectedSchedulerSha256) { throw 'FSRS_UPSTREAM_HASH_MISMATCH' }
if ($env:RUSTFLAGS -or $env:CARGO_ENCODED_RUSTFLAGS) { throw 'FSRS_AMBIENT_RUSTFLAGS_NOT_ALLOWED' }

# rustup run 没有 --install：不存在该工具链时失败，不安装或更新工具链。
$rustVersion = & rustup run stable rustc --version
if ($LASTEXITCODE -ne 0 -or $rustVersion -notmatch '^rustc 1\.96\.0 ') { throw 'FSRS_RUST_TOOLCHAIN_MISMATCH: require installed Rust 1.96.0' }
$cargoVersion = & rustup run stable cargo --version
if ($LASTEXITCODE -ne 0 -or $cargoVersion -notmatch '^cargo 1\.96\.0 ') { throw 'FSRS_CARGO_TOOLCHAIN_MISMATCH: require installed Cargo 1.96.0' }

$cacheRoot = Join-Path $projectRoot '.local/cache/workbench/fsrs-helper'
$targetRoot = Join-Path $cacheRoot 'target'
New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
$buildArgs = @('run', 'stable', 'cargo', 'rustc', '--manifest-path', $manifestPath, '--locked', '--release', '--target', 'x86_64-pc-windows-msvc', '--target-dir', $targetRoot, '--bin', 'learning-fsrs-helper', '--', '-C', 'target-feature=+crt-static')
& rustup @buildArgs
if ($LASTEXITCODE -ne 0) { throw 'FSRS_HELPER_BUILD_FAILED' }

$binaryPath = Join-Path $targetRoot 'x86_64-pc-windows-msvc/release/learning-fsrs-helper.exe'
$binaryFile = Get-Item -LiteralPath $binaryPath
$sourceFiles = @('Cargo.toml', 'Cargo.lock', 'src/lib.rs', 'src/main.rs') | ForEach-Object {
    $sourcePath = Join-Path $PSScriptRoot $_
    [ordered]@{ path = $_; sha256 = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToLowerInvariant() }
}
$imports = $null
$dumpbinVersion = $null
$locatorPath = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path -LiteralPath $locatorPath) {
    $installationRoot = & $locatorPath -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($installationRoot) {
        $compilerDirectory = Get-ChildItem -LiteralPath (Join-Path $installationRoot 'VC/Tools/MSVC') -Directory | Sort-Object Name -Descending | Select-Object -First 1
        $dumpbinPath = Join-Path $compilerDirectory.FullName 'bin/Hostx64/x64/dumpbin.exe'
        if (Test-Path -LiteralPath $dumpbinPath) {
            $dumpbinOutput = & $dumpbinPath /DEPENDENTS $binaryPath
            if ($LASTEXITCODE -ne 0) { throw 'FSRS_IMPORT_INSPECTION_FAILED' }
            $dumpbinOutput | Set-Content -LiteralPath (Join-Path $cacheRoot 'imports.txt') -Encoding UTF8
            $imports = @($dumpbinOutput | Where-Object { $_ -match '^\s+([A-Za-z0-9._-]+\.dll)\s*$' } | ForEach-Object { $_.Trim() })
            $dumpbinVersion = $dumpbinOutput | Select-Object -First 1
        }
    }
}
$record = [ordered]@{
    scope = 'candidate build only; no product integration acceptance'
    helper_version = '0.1.0'
    protocol_version = 1
    source_commit = '635216503c0b34bdf812e17942e0f09f51872e6f'
    original_scheduler_sha256 = $actualSchedulerSha256
    rustc = $rustVersion
    cargo = $cargoVersion
    target = 'x86_64-pc-windows-msvc'
    static_crt = $true
    build_command = @('rustup') + $buildArgs
    source_files = @($sourceFiles)
    binary_path = $binaryPath
    binary_size_bytes = $binaryFile.Length
    binary_sha256 = (Get-FileHash -LiteralPath $binaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
    dumpbin = $dumpbinVersion
    imported_dlls = $imports
}
$record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $cacheRoot 'build.json') -Encoding UTF8
$record | ConvertTo-Json -Depth 8
