[CmdletBinding()]
param(
    [switch]$WithDemoProject,
    [switch]$WithLocalTranslationModel
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendRoot = Join-Path $ProjectRoot 'backend'
$FrontendRoot = Join-Path $ProjectRoot 'frontend'
$PythonPath = Join-Path $BackendRoot '.venv\Scripts\python.exe'
. (Join-Path $PSScriptRoot 'node_runtime.ps1')
. (Join-Path $PSScriptRoot 'ffmpeg_runtime.ps1')
. (Join-Path $PSScriptRoot 'python_runtime.ps1')

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

function New-StrongDjangoSecret {
    $bytes = New-Object byte[] 48
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($bytes)
}

$NodeRuntime = Resolve-AutoPromptNodeRuntime -ProjectRoot $ProjectRoot
if (-not $NodeRuntime) {
    & (Join-Path $PSScriptRoot 'install_node_runtime.ps1')
    Assert-NativeSuccess 'Project-local Node.js runtime installation'
    $NodeRuntime = Resolve-AutoPromptNodeRuntime -ProjectRoot $ProjectRoot
}
$npmCacheRoot = Set-AutoPromptNpmCacheEnvironment -ProjectRoot $ProjectRoot
Write-Host "Node runtime: $($NodeRuntime.Version) from $($NodeRuntime.Source); npm cache: $npmCacheRoot" -ForegroundColor DarkGray

$FfmpegRuntime = Resolve-AutoPromptFfmpegRuntime -ProjectRoot $ProjectRoot
if (-not $FfmpegRuntime) {
    & (Join-Path $PSScriptRoot 'install_ffmpeg_runtime.ps1')
    Assert-NativeSuccess 'Project-local FFmpeg runtime installation'
    $FfmpegRuntime = Resolve-AutoPromptFfmpegRuntime -ProjectRoot $ProjectRoot
}
Set-AutoPromptFfmpegEnvironment -Runtime $FfmpegRuntime
Write-Host "FFmpeg runtime: $($FfmpegRuntime.Version) from $($FfmpegRuntime.Source)" -ForegroundColor DarkGray

$PythonRuntime = Resolve-AutoPromptPythonRuntime -ProjectRoot $ProjectRoot
if (-not $PythonRuntime) {
    & (Join-Path $PSScriptRoot 'install_python_runtime.ps1')
    Assert-NativeSuccess 'Project-local Python runtime installation'
    $PythonRuntime = Resolve-AutoPromptPythonRuntime -ProjectRoot $ProjectRoot
}
$pipCacheRoot = Set-AutoPromptPipCacheEnvironment -ProjectRoot $ProjectRoot
Write-Host "Python runtime: $($PythonRuntime.Version) from $($PythonRuntime.Source); pip cache: $pipCacheRoot" -ForegroundColor DarkGray

$PythonPath = Repair-AutoPromptBackendVenv -BackendRoot $BackendRoot -Runtime $PythonRuntime
if (-not (Test-AutoPromptPythonExecutable -PythonPath $PythonPath)) {
    throw "Backend virtual environment remains unusable after repair: $PythonPath"
}

& $PythonPath -m pip install -r (Join-Path $BackendRoot 'requirements.txt')
Assert-NativeSuccess 'Backend dependency installation'
& $PythonPath -m pip install -r (Join-Path $ProjectRoot 'requirements-tools.txt')
Assert-NativeSuccess 'Tool dependency installation'
if (-not (Test-Path -LiteralPath (Join-Path $BackendRoot '.env'))) {
    $backendEnvPath = Join-Path $BackendRoot '.env'
    Copy-Item -LiteralPath (Join-Path $BackendRoot '.env.example') -Destination $backendEnvPath
    $strongSecret = New-StrongDjangoSecret
    $environmentText = Get-Content -LiteralPath $backendEnvPath -Raw -Encoding UTF8
    $environmentText = $environmentText -replace '(?m)^DJANGO_SECRET_KEY=.*$', "DJANGO_SECRET_KEY=$strongSecret"
    Set-Content -LiteralPath $backendEnvPath -Value $environmentText -Encoding UTF8
    Write-Host 'Generated a unique 384-bit Django secret for this installation.' -ForegroundColor Green
}

Push-Location $BackendRoot
try {
    & $PythonPath manage.py migrate
    Assert-NativeSuccess 'Database migration'
    if ($WithDemoProject) {
        & $PythonPath manage.py seed_data --with-demo-project
    } else {
        & $PythonPath manage.py seed_data
    }
    Assert-NativeSuccess 'Base data seeding'
    & $PythonPath manage.py import_asset_library --manifest (Join-Path $ProjectRoot 'assets\library\manifest.json')
    Assert-NativeSuccess 'Asset library import'
} finally {
    Pop-Location
}

Push-Location $FrontendRoot
try {
    & $NodeRuntime.NpmPath ci
    Assert-NativeSuccess 'Frontend dependency installation'
} finally {
    Pop-Location
}

if ($WithLocalTranslationModel) {
    & (Join-Path $PSScriptRoot 'download_local_translation_model.ps1') -Runtime cpu
    Assert-NativeSuccess 'Official local Qwen translation model installation'
} else {
    Write-Host 'Optional local director-field translation model was not downloaded.' -ForegroundColor Yellow
    Write-Host 'Install it later with:'
    Write-Host '  powershell -ExecutionPolicy Bypass -File .\scripts\download_local_translation_model.ps1 -Runtime cpu'
}

if ($WithDemoProject) {
    Write-Host 'Local demo seeding was explicitly requested. A newly created demo password is shown only by seed_data once; an existing demo password is never reset.' -ForegroundColor Yellow
} else {
    Write-Host 'No demo account was created. Create the first production administrator interactively:' -ForegroundColor Green
    Write-Host '  Set-Location .\backend'
    Write-Host '  .\.venv\Scripts\python manage.py createsuperuser'
}
Write-Host 'Setup complete. Use restart-all.cmd, or start Django, both workers, and Vue as documented in README.md.'
