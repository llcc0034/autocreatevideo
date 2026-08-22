function Get-AutoPromptFfmpegRuntimeManifest {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $manifestPath = Join-Path $ProjectRoot 'config\ffmpeg-runtime.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "FFmpeg runtime manifest is missing: $manifestPath"
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "FFmpeg runtime manifest is invalid JSON: $manifestPath"
    }
    if ($manifest.schema_version -ne 1) {
        throw "Unsupported FFmpeg runtime manifest schema_version: $($manifest.schema_version)"
    }
    $version = [string]$manifest.version
    $platform = [string]$manifest.platform
    $provider = [string]$manifest.provider
    $releaseTag = [string]$manifest.release_tag
    $buildVersion = [string]$manifest.build_version
    $variant = [string]$manifest.variant
    $archiveName = [string]$manifest.archive_name
    $archiveRoot = [string]$manifest.archive_root
    $downloadUrl = [string]$manifest.download_url
    $sha256 = ([string]$manifest.sha256).ToLowerInvariant()
    if ($version -notmatch '^\d+\.\d+(?:\.\d+)?$') {
        throw "FFmpeg runtime version must be pinned: $version"
    }
    if ($platform -ne 'win-x64') {
        throw "FFmpeg runtime platform must be win-x64: $platform"
    }
    if ($provider -ne 'btbn') {
        throw "FFmpeg runtime provider must be btbn: $provider"
    }
    if ($releaseTag -notmatch '^autobuild-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}$') {
        throw "FFmpeg runtime release_tag must be pinned: $releaseTag"
    }
    if ($buildVersion -notmatch '^n\d+\.\d+\.\d+-\d+-g[0-9a-f]+$') {
        throw "FFmpeg runtime build_version must be pinned: $buildVersion"
    }
    if ($variant -ne 'gpl-shared-9.0') {
        throw "FFmpeg runtime variant must be gpl-shared-9.0: $variant"
    }
    $expectedArchiveName = "ffmpeg-$buildVersion-win64-$variant.zip"
    if ($archiveName -ne $expectedArchiveName) {
        throw "FFmpeg runtime archive_name must be $expectedArchiveName."
    }
    if ($archiveRoot -ne [IO.Path]::GetFileNameWithoutExtension($archiveName)) {
        throw 'FFmpeg runtime archive_root must match the pinned archive name.'
    }
    $expectedDownloadUrl = "https://github.com/BtbN/FFmpeg-Builds/releases/download/$releaseTag/$archiveName"
    if ($downloadUrl -ne $expectedDownloadUrl) {
        throw 'FFmpeg runtime download_url must use the pinned BtbN release asset linked by ffmpeg.org.'
    }
    if ($sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'FFmpeg runtime sha256 must contain exactly 64 lowercase hexadecimal characters.'
    }
    return [pscustomobject]@{
        Version = $version
        Platform = $platform
        Provider = $provider
        ReleaseTag = $releaseTag
        BuildVersion = $buildVersion
        Variant = $variant
        ArchiveName = $archiveName
        ArchiveRoot = $archiveRoot
        DownloadUrl = $downloadUrl
        Sha256 = $sha256
        ManifestPath = $manifestPath
        DirectoryName = "ffmpeg-$version-win64-$variant"
    }
}

function Get-AutoPromptFfmpegVersion {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$FfmpegPath)

    $versionOutput = & $FfmpegPath -version 2>&1
    $versionExitCode = $LASTEXITCODE
    $firstLine = ($versionOutput | Select-Object -First 1 | Out-String).Trim()
    if ($versionExitCode -ne 0 -or $firstLine -notmatch '^ffmpeg version n?(\d+\.\d+(?:\.\d+)?)') {
        throw "Could not read a valid FFmpeg version from $FfmpegPath. Output: $firstLine"
    }
    return [pscustomobject]@{
        Text = $Matches[1]
        Banner = $firstLine
    }
}

function Resolve-AutoPromptFfmpegRuntime {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $manifest = Get-AutoPromptFfmpegRuntimeManifest -ProjectRoot $ProjectRoot
    $localRoot = Join-Path $ProjectRoot ('.runtime\toolchains\' + $manifest.DirectoryName)
    $localBin = Join-Path $localRoot 'bin'
    $localFfmpeg = Join-Path $localBin 'ffmpeg.exe'
    $localFfprobe = Join-Path $localBin 'ffprobe.exe'
    $hasLocalRoot = Test-Path -LiteralPath $localRoot
    $hasLocalFfmpeg = Test-Path -LiteralPath $localFfmpeg -PathType Leaf
    $hasLocalFfprobe = Test-Path -LiteralPath $localFfprobe -PathType Leaf
    if ($hasLocalRoot -or $hasLocalFfmpeg -or $hasLocalFfprobe) {
        if (-not $hasLocalFfmpeg -or -not $hasLocalFfprobe) {
            throw "Project-local FFmpeg runtime is incomplete: $localRoot. Re-run scripts\install_ffmpeg_runtime.ps1."
        }
        $version = Get-AutoPromptFfmpegVersion -FfmpegPath $localFfmpeg
        if ($version.Text -ne $manifest.Version) {
            throw "Project-local FFmpeg runtime version mismatch. Expected $($manifest.Version), found $($version.Text)."
        }
        return [pscustomobject]@{
            FfmpegPath = $localFfmpeg
            FfprobePath = $localFfprobe
            Version = $version.Text
            Source = 'project-local'
            RuntimeRoot = $localRoot
            BinRoot = $localBin
        }
    }

    $ffmpegCommand = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    $ffprobeCommand = Get-Command ffprobe.exe -ErrorAction SilentlyContinue
    if (-not $ffmpegCommand -and -not $ffprobeCommand) {
        return $null
    }
    if (-not $ffmpegCommand -or -not $ffprobeCommand) {
        throw 'System PATH contains an incomplete FFmpeg runtime; ffmpeg.exe and ffprobe.exe are both required.'
    }
    $version = Get-AutoPromptFfmpegVersion -FfmpegPath $ffmpegCommand.Source
    return [pscustomobject]@{
        FfmpegPath = $ffmpegCommand.Source
        FfprobePath = $ffprobeCommand.Source
        Version = $version.Text
        Source = 'system-path'
        RuntimeRoot = Split-Path -Parent $ffmpegCommand.Source
        BinRoot = Split-Path -Parent $ffmpegCommand.Source
    }
}

function Set-AutoPromptFfmpegEnvironment {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Runtime)

    [Environment]::SetEnvironmentVariable('FFMPEG_BINARY', $Runtime.FfmpegPath, 'Process')
    [Environment]::SetEnvironmentVariable('FFPROBE_BINARY', $Runtime.FfprobePath, 'Process')
    $pathEntries = @($env:Path -split ';' | Where-Object { $_ })
    if ($pathEntries -notcontains $Runtime.BinRoot) {
        $env:Path = $Runtime.BinRoot + ';' + $env:Path
    }
}
