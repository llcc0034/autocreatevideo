[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'node_runtime.ps1')
. (Join-Path $PSScriptRoot 'ffmpeg_runtime.ps1')

$manifest = Get-AutoPromptFfmpegRuntimeManifest -ProjectRoot $ProjectRoot
$runtimeRoot = Join-Path $ProjectRoot '.runtime'
$downloadRoot = Join-Path $runtimeRoot 'downloads'
$toolchainRoot = Join-Path $runtimeRoot 'toolchains'
$archivePath = Join-Path $downloadRoot $manifest.ArchiveName
$targetRoot = Join-Path $toolchainRoot $manifest.DirectoryName

function Assert-ArchiveHash([string]$Path) {
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $manifest.Sha256) {
        throw "FFmpeg archive checksum mismatch. Expected $($manifest.Sha256), found $actual. Remove the invalid archive before retrying: $Path"
    }
}

function Download-WithNode([string]$Url, [string]$Destination) {
    $nodeRuntime = Resolve-AutoPromptNodeRuntime -ProjectRoot $ProjectRoot
    if (-not $nodeRuntime) {
        throw 'PowerShell download failed and the project-local Node.js TLS fallback is unavailable.'
    }
    & $nodeRuntime.NodePath (Join-Path $PSScriptRoot 'download_https_parallel.js') $Url $Destination 8
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Destination -PathType Leaf)) {
        throw "FFmpeg download failed through Node.js TLS fallback: $Url"
    }
}

if (Test-Path -LiteralPath $targetRoot -PathType Container) {
    $resolved = Resolve-AutoPromptFfmpegRuntime -ProjectRoot $ProjectRoot
    Write-Host "Project-local FFmpeg is already installed: $($resolved.Version) at $($resolved.RuntimeRoot)" -ForegroundColor Green
    exit 0
}

New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
New-Item -ItemType Directory -Path $toolchainRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
    $temporaryArchive = "$archivePath.partial"
    try {
        # The project-local Node runtime uses OpenSSL and remains reliable when
        # the current Windows user's Schannel credential cache is damaged.
        Download-WithNode -Url $manifest.DownloadUrl -Destination $temporaryArchive
        try {
            Assert-ArchiveHash -Path $temporaryArchive
        } catch {
            Remove-Item -LiteralPath $temporaryArchive -Force
            throw
        }
        Move-Item -LiteralPath $temporaryArchive -Destination $archivePath
    } finally {
        # Keep an incomplete partial archive so the next invocation can resume.
    }
} else {
    Assert-ArchiveHash -Path $archivePath
}

$stagingRoot = Join-Path $toolchainRoot ('.install-' + $manifest.DirectoryName + '-' + $PID)
try {
    New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
    Expand-Archive -LiteralPath $archivePath -DestinationPath $stagingRoot -Force
    $extractedRoot = Join-Path $stagingRoot $manifest.ArchiveRoot
    $extractedFfmpeg = Join-Path $extractedRoot 'bin\ffmpeg.exe'
    $extractedFfprobe = Join-Path $extractedRoot 'bin\ffprobe.exe'
    if (-not (Test-Path -LiteralPath $extractedFfmpeg -PathType Leaf) -or
        -not (Test-Path -LiteralPath $extractedFfprobe -PathType Leaf)) {
        throw "The verified FFmpeg archive did not contain ffmpeg.exe and ffprobe.exe: $archivePath"
    }
    $version = Get-AutoPromptFfmpegVersion -FfmpegPath $extractedFfmpeg
    if ($version.Text -ne $manifest.Version) {
        throw "The extracted FFmpeg version is $($version.Text), expected $($manifest.Version)."
    }
    $encoders = (& $extractedFfmpeg -hide_banner -encoders 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0 -or $encoders -notmatch '(?m)^\s*V\S*\s+libx264\s') {
        throw 'The verified FFmpeg runtime does not expose the required libx264 encoder.'
    }
    $ffprobeOutput = & $extractedFfprobe -version 2>&1
    $ffprobeExitCode = $LASTEXITCODE
    $ffprobeBanner = ($ffprobeOutput | Select-Object -First 1 | Out-String).Trim()
    if ($ffprobeExitCode -ne 0 -or $ffprobeBanner -notmatch ('^ffprobe version n?' + [regex]::Escape($manifest.Version))) {
        throw "The verified FFprobe runtime does not match FFmpeg $($manifest.Version): $ffprobeBanner"
    }
    Move-Item -LiteralPath $extractedRoot -Destination $targetRoot
} finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
}

$resolved = Resolve-AutoPromptFfmpegRuntime -ProjectRoot $ProjectRoot
Write-Host "Installed project-local FFmpeg $($resolved.Version) at $($resolved.RuntimeRoot)." -ForegroundColor Green
Write-Host "Verified archive retained on E: $archivePath" -ForegroundColor Green
