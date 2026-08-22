$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'ffmpeg_runtime.ps1')

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) {
        throw $Message
    }
}

function Assert-Equal([object]$Actual, [object]$Expected, [string]$Message) {
    if ($Actual -ne $Expected) {
        throw "$Message Expected=<$Expected> Actual=<$Actual>"
    }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$manifest = Get-AutoPromptFfmpegRuntimeManifest -ProjectRoot $projectRoot
Assert-True ($manifest.Version -match '^\d+\.\d+') 'The FFmpeg runtime version must be pinned.'
Assert-Equal $manifest.Provider 'btbn' 'The Windows FFmpeg provider must be pinned.'
Assert-Equal $manifest.Variant 'gpl-shared-9.0' 'The GPL shared build is required for libx264.'
Assert-Equal $manifest.ArchiveName ("ffmpeg-$($manifest.BuildVersion)-win64-$($manifest.Variant).zip") 'The archive name must match the pinned release build.'

$runtime = Resolve-AutoPromptFfmpegRuntime -ProjectRoot $projectRoot
Assert-True ($null -ne $runtime) 'A complete FFmpeg/FFprobe runtime must be resolvable.'
Assert-True (Test-Path -LiteralPath $runtime.FfmpegPath -PathType Leaf) 'ffmpeg.exe must exist.'
Assert-True (Test-Path -LiteralPath $runtime.FfprobePath -PathType Leaf) 'ffprobe.exe must exist.'

Set-AutoPromptFfmpegEnvironment -Runtime $runtime
Assert-Equal $env:FFMPEG_BINARY $runtime.FfmpegPath 'FFMPEG_BINARY must use the resolved E-drive binary.'
Assert-Equal $env:FFPROBE_BINARY $runtime.FfprobePath 'FFPROBE_BINARY must use the resolved E-drive binary.'
Assert-Equal (Get-Command ffmpeg.exe).Source $runtime.FfmpegPath 'Process PATH must resolve the pinned ffmpeg.exe.'
Assert-Equal (Get-Command ffprobe.exe).Source $runtime.FfprobePath 'Process PATH must resolve the pinned ffprobe.exe.'
$encoders = (& $runtime.FfmpegPath -hide_banner -encoders 2>&1 | Out-String)
Assert-True ($LASTEXITCODE -eq 0 -and $encoders -match '(?m)^\s*V\S*\s+libx264\s') 'The runtime must expose libx264 for assembly and distribution.'
$ffprobeOutput = & $runtime.FfprobePath -version 2>&1
$ffprobeExitCode = $LASTEXITCODE
$ffprobeBanner = ($ffprobeOutput | Select-Object -First 1 | Out-String).Trim()
Assert-True ($ffprobeExitCode -eq 0 -and $ffprobeBanner -match ('^ffprobe version n?' + [regex]::Escape($manifest.Version))) 'ffprobe must match the pinned FFmpeg version.'

Write-Host "FFmpeg runtime tests passed with $($runtime.Version) from $($runtime.Source)."
