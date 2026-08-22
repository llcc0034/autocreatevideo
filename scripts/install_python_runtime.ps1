[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'node_runtime.ps1')
. (Join-Path $PSScriptRoot 'python_runtime.ps1')

$manifest = Get-AutoPromptPythonRuntimeManifest -ProjectRoot $ProjectRoot
$downloadRoot = Join-Path $ProjectRoot '.runtime\downloads'
$toolchainRoot = Join-Path $ProjectRoot '.runtime\toolchains'
$packagePath = Join-Path $downloadRoot $manifest.PackageName
$packagePartialPath = "$packagePath.partial"
$nugetPath = Join-Path $downloadRoot $manifest.NugetName
$nugetPartialPath = "$nugetPath.partial"
$targetPackageRoot = Join-Path $toolchainRoot $manifest.DirectoryName
$nugetConfigPath = Join-Path $ProjectRoot '.runtime\nuget.config'

function Assert-FileHash([string]$Path, [string]$Expected, [string]$Label) {
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Expected) {
        throw "$Label checksum mismatch. Expected $Expected, found $actual. Invalid file: $Path"
    }
}

function Get-PinnedFile(
    [string]$Path,
    [string]$PartialPath,
    [string]$Url,
    [string]$ExpectedHash,
    [string]$Label,
    [string]$NodePath
) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        Assert-FileHash -Path $Path -Expected $ExpectedHash -Label $Label
        return
    }
    & $NodePath (Join-Path $PSScriptRoot 'download_https.js') $Url $PartialPath
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $PartialPath -PathType Leaf)) {
        throw "$Label download failed: $Url"
    }
    try {
        Assert-FileHash -Path $PartialPath -Expected $ExpectedHash -Label $Label
    } catch {
        $quarantine = "$PartialPath.invalid-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Move-Item -LiteralPath $PartialPath -Destination $quarantine
        throw
    }
    Move-Item -LiteralPath $PartialPath -Destination $Path
}

if (Test-Path -LiteralPath $targetPackageRoot -PathType Container) {
    $resolved = Resolve-AutoPromptPythonRuntime -ProjectRoot $ProjectRoot
    Write-Host "Project-local Python is already installed: $($resolved.Version) at $($resolved.RuntimeRoot)" -ForegroundColor Green
    exit 0
}

$nodeRuntime = Resolve-AutoPromptNodeRuntime -ProjectRoot $ProjectRoot
if (-not $nodeRuntime) {
    throw 'Project-local Node.js is required to download and verify the Python runtime.'
}
New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
New-Item -ItemType Directory -Path $toolchainRoot -Force | Out-Null

Get-PinnedFile -Path $packagePath -PartialPath $packagePartialPath -Url $manifest.DownloadUrl `
    -ExpectedHash $manifest.Sha256 -Label 'Python NuGet package' -NodePath $nodeRuntime.NodePath
Get-PinnedFile -Path $nugetPath -PartialPath $nugetPartialPath -Url $manifest.NugetDownloadUrl `
    -ExpectedHash $manifest.NugetSha256 -Label 'NuGet verifier' -NodePath $nodeRuntime.NodePath

$nugetSignature = Get-AuthenticodeSignature -LiteralPath $nugetPath
if ($nugetSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
    [string]$nugetSignature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
    throw "NuGet verifier Authenticode validation failed: $($nugetSignature.Status) $($nugetSignature.StatusMessage)"
}

$nugetConfig = @"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <config>
    <add key="globalPackagesFolder" value="$ProjectRoot\.runtime\nuget-packages" />
    <add key="httpCachePath" value="$ProjectRoot\.runtime\nuget-http-cache" />
  </config>
</configuration>
"@
[System.IO.File]::WriteAllText($nugetConfigPath, $nugetConfig, [System.Text.UTF8Encoding]::new($false))
$verificationOutput = (& $nugetPath verify $packagePath -Signatures -Verbosity detailed -ConfigFile $nugetConfigPath 2>&1 | Out-String)
if ($LASTEXITCODE -ne 0 -or
    $verificationOutput -notmatch 'Python Software Foundation' -or
    $verificationOutput -notmatch 'NuGet\.org Repository') {
    throw "Python package signature verification failed.`n$verificationOutput"
}

$stageRoot = Join-Path $toolchainRoot ('.python-stage-' + [Guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $stageRoot | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($packagePath, $stageRoot)
    $stageRuntimeRoot = Join-Path $stageRoot $manifest.RuntimeSubdirectory
    if (-not (Test-Path -LiteralPath (Join-Path $stageRuntimeRoot 'python.exe') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $stageRuntimeRoot 'Lib\venv\__init__.py') -PathType Leaf) -or
        -not (Test-Path -LiteralPath (Join-Path $stageRuntimeRoot 'Lib\ensurepip\__init__.py') -PathType Leaf)) {
        throw 'Verified Python package extraction is incomplete.'
    }
    Move-Item -LiteralPath $stageRoot -Destination $targetPackageRoot
} finally {
    if (Test-Path -LiteralPath $stageRoot) {
        $resolvedToolchains = (Resolve-Path -LiteralPath $toolchainRoot).Path
        $resolvedStage = (Resolve-Path -LiteralPath $stageRoot).Path
        if ($resolvedStage.StartsWith($resolvedToolchains + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedStage -Recurse -Force
        }
    }
}

$resolved = Resolve-AutoPromptPythonRuntime -ProjectRoot $ProjectRoot
& $resolved.PythonPath -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) {
    throw "ensurepip validation failed for $($resolved.PythonPath)."
}
$pipVersion = (& $resolved.PythonPath -m pip --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "pip validation failed for $($resolved.PythonPath)."
}
Write-Host "Installed signed project-local Python $($resolved.Version) at $($resolved.RuntimeRoot)." -ForegroundColor Green
Write-Host $pipVersion -ForegroundColor DarkGray
Write-Host "Verified packages retained on E: $packagePath and $nugetPath" -ForegroundColor Green
