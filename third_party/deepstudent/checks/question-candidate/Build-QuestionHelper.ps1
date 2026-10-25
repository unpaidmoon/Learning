[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$projectRoot = (Get-Item -LiteralPath $PSScriptRoot).Parent.Parent.Parent.Parent.FullName
if ($env:RUSTFLAGS -or $env:CARGO_ENCODED_RUSTFLAGS) { throw 'QBANK_AMBIENT_RUSTFLAGS_NOT_ALLOWED' }
$rustVersion = & rustup run stable rustc --version
if ($LASTEXITCODE -ne 0 -or $rustVersion -notmatch '^rustc 1\.96\.0 ') { throw 'QBANK_RUST_TOOLCHAIN_MISMATCH' }
$cargoVersion = & rustup run stable cargo --version
if ($LASTEXITCODE -ne 0 -or $cargoVersion -notmatch '^cargo 1\.96\.0 ') { throw 'QBANK_CARGO_TOOLCHAIN_MISMATCH' }
$extract = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'extraction-manifest.json') | ConvertFrom-Json
$actual = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'src/upstream_pure.rs') -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $extract.output_sha256) { throw 'QBANK_EXTRACTED_SOURCE_HASH_MISMATCH' }
$cache = Join-Path $projectRoot '.local/cache/workbench/ds-question-candidate'
$target = Join-Path $cache 'target'
New-Item -ItemType Directory -Path $cache -Force | Out-Null
$buildArgs = @('run','stable','cargo','rustc','--offline','--locked','--manifest-path',(Join-Path $PSScriptRoot 'Cargo.toml'),'--release','--target','x86_64-pc-windows-msvc','--target-dir',$target,'--bin','learning-question-helper','--','-C','target-feature=+crt-static')
& rustup @buildArgs
if ($LASTEXITCODE -ne 0) { throw 'QBANK_HELPER_BUILD_FAILED' }
$binary = Join-Path $target 'x86_64-pc-windows-msvc/release/learning-question-helper.exe'
$imports = @()
$dumpbinVersion = $null
$vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path -LiteralPath $vswhere) {
 $installation = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
 if ($installation) {
  $compiler = Get-ChildItem -LiteralPath (Join-Path $installation 'VC/Tools/MSVC') -Directory | Sort-Object Name -Descending | Select-Object -First 1
  $dumpbin = Join-Path $compiler.FullName 'bin/Hostx64/x64/dumpbin.exe'
  if (Test-Path -LiteralPath $dumpbin) {
   $text = & $dumpbin /DEPENDENTS $binary
   if ($LASTEXITCODE -ne 0) { throw 'QBANK_IMPORT_INSPECTION_FAILED' }
   $text | Set-Content -LiteralPath (Join-Path $cache 'imports.txt') -Encoding UTF8
   $dumpbinVersion = $text | Select-Object -First 1
   $imports = @($text | Where-Object { $_ -match '^\s+([A-Za-z0-9._-]+\.dll)\s*$' } | ForEach-Object { $_.Trim() })
  }
 }
}
$record = [ordered]@{scope='candidate-only';helper_version='0.1.0';protocol_version=1;source_commit=$extract.source_commit;rustc=$rustVersion;cargo=$cargoVersion;target='x86_64-pc-windows-msvc';static_crt=$true;command=@('rustup')+$buildArgs;binary_path=$binary;binary_size=(Get-Item -LiteralPath $binary).Length;binary_sha256=(Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant();dumpbin=$dumpbinVersion;imported_dlls=$imports;input_limit_bytes=262144;output_limit_bytes=65536;source_files=@('Cargo.toml','Cargo.lock','src/lib.rs','src/main.rs','src/upstream_pure.rs') | ForEach-Object {[ordered]@{path=$_;sha256=(Get-FileHash -LiteralPath (Join-Path $PSScriptRoot $_) -Algorithm SHA256).Hash.ToLowerInvariant()}}}
$record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $cache 'build.json') -Encoding UTF8
$record | ConvertTo-Json -Depth 8
