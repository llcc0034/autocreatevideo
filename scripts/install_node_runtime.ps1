[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'node_runtime.ps1')

$manifest = Get-AutoPromptNodeRuntimeManifest -ProjectRoot $ProjectRoot
$runtimeRoot = Join-Path $ProjectRoot '.runtime'
$downloadRoot = Join-Path $runtimeRoot 'downloads'
$toolchainRoot = Join-Path $runtimeRoot 'toolchains'
$archivePath = Join-Path $downloadRoot $manifest.ArchiveName
$targetRoot = Join-Path $toolchainRoot $manifest.DirectoryName

function Assert-ArchiveHash([string]$Path) {
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $manifest.Sha256) {
        throw "Node.js archive checksum mismatch. Expected $($manifest.Sha256), found $actual. Remove the invalid archive before retrying: $Path"
    }
}

function Download-WithBootstrapNode([string]$Url, [string]$Destination) {
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $nodeCommand) {
        throw 'PowerShell download failed and no bootstrap node.exe is available for the TLS fallback.'
    }
    & $nodeCommand.Source (Join-Path $PSScriptRoot 'download_https.js') $Url $Destination
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        throw "Node.js download failed through bootstrap Node: $Url"
    }
}

if (Test-Path -LiteralPath $targetRoot -PathType Container) {
    $resolved = Resolve-AutoPromptNodeRuntime -ProjectRoot $ProjectRoot
    Write-Host "Project-local Node.js is already installed: $($resolved.Version) at $($resolved.RuntimeRoot)" -ForegroundColor Green
    exit 0
}

New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
New-Item -ItemType Directory -Path $toolchainRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
    $temporaryArchive = "$archivePath.$PID.download"
    try {
        try {
            Invoke-WebRequest -Uri $manifest.DownloadUrl -OutFile $temporaryArchive
        } catch {
            if (Test-Path -LiteralPath $temporaryArchive) {
                Remove-Item -LiteralPath $temporaryArchive -Force
            }
            Download-WithBootstrapNode -Url $manifest.DownloadUrl -Destination $temporaryArchive
        }
        Assert-ArchiveHash -Path $temporaryArchive
        Move-Item -LiteralPath $temporaryArchive -Destination $archivePath
    } finally {
        if (Test-Path -LiteralPath $temporaryArchive) {
            Remove-Item -LiteralPath $temporaryArchive -Force
        }
    }
} else {
    Assert-ArchiveHash -Path $archivePath
}

$stagingRoot = Join-Path $toolchainRoot ('.install-' + $manifest.DirectoryName + '-' + $PID)
try {
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $stagingRoot -Force
    $extractedRoot = Join-Path $stagingRoot $manifest.DirectoryName
    $extractedNode = Join-Path $extractedRoot 'node.exe'
    $extractedNpm = Join-Path $extractedRoot 'npm.cmd'
    if (-not (Test-Path -LiteralPath $extractedNode -PathType Leaf) -or
        -not (Test-Path -LiteralPath $extractedNpm -PathType Leaf)) {
        throw "The verified Node.js archive did not contain node.exe and npm.cmd: $archivePath"
    }
    $version = Get-AutoPromptNodeVersion -NodePath $extractedNode
    if ($version.Text -ne ('v' + $manifest.Version)) {
        throw "The extracted Node.js version is $($version.Text), expected v$($manifest.Version)."
    }
    Move-Item -LiteralPath $extractedRoot -Destination $targetRoot
} finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}

$resolved = Resolve-AutoPromptNodeRuntime -ProjectRoot $ProjectRoot
$npmVersion = (& $resolved.NpmPath --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "npm validation failed for $($resolved.NpmPath)."
}
Write-Host "Installed project-local Node.js $($resolved.Version) with npm $npmVersion at $($resolved.RuntimeRoot)." -ForegroundColor Green
Write-Host "Verified archive retained on E: $archivePath" -ForegroundColor Green
