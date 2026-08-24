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

function New-DatabaseEncryptionKey {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return ([Convert]::ToBase64String($bytes)).TrimEnd('=')
}

function Get-EnvironmentEntry([string]$Path, [string]$Name) {
    $match = Get-Content -LiteralPath $Path -Encoding UTF8 | Where-Object {
        $_ -match "^$([Regex]::Escape($Name))="
    } | Select-Object -Last 1
    if (-not $match) { return '' }
    return ($match -split '=', 2)[1].Trim()
}

function Set-EnvironmentEntry([string]$Path, [string]$Name, [string]$Value) {
    $text = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    $pattern = "(?m)^$([Regex]::Escape($Name))=.*$"
    if ($text -match $pattern) {
        $text = $text -replace $pattern, "$Name=$Value"
    } else {
        $text = $text.TrimEnd() + [Environment]::NewLine + "$Name=$Value" + [Environment]::NewLine
    }
    $temporary = Join-Path (Split-Path -Parent $Path) ".env.setup-$PID.tmp"
    [IO.File]::WriteAllText($temporary, $text, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
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
$backendEnvPath = Join-Path $BackendRoot '.env'
if (-not (Test-Path -LiteralPath $backendEnvPath)) {
    Copy-Item -LiteralPath (Join-Path $BackendRoot '.env.example') -Destination $backendEnvPath
    Set-EnvironmentEntry -Path $backendEnvPath -Name 'DJANGO_SECRET_KEY' -Value (New-StrongDjangoSecret)
    Set-EnvironmentEntry -Path $backendEnvPath -Name 'AUTOPROMPT_JWT_SIGNING_KEY' -Value (New-StrongDjangoSecret)
    Set-EnvironmentEntry -Path $backendEnvPath -Name 'AUTOPROMPT_CONFIG_MASTER_KEYS' -Value "local-v1:$(New-DatabaseEncryptionKey)"
    Set-EnvironmentEntry -Path $backendEnvPath -Name 'AUTOPROMPT_CONFIG_ACTIVE_KEY_ID' -Value 'local-v1'
    Write-Host 'Generated an independent Django secret for this installation.' -ForegroundColor Green
    Write-Host 'Generated an independent JWT signing key for this installation.' -ForegroundColor Green
    Write-Host 'Generated an independent database-encryption keyring; it is not stored in the database.' -ForegroundColor Green
}

$configuredJwtSigningKey = Get-EnvironmentEntry -Path $backendEnvPath -Name 'AUTOPROMPT_JWT_SIGNING_KEY'
if (-not $configuredJwtSigningKey) {
    Set-EnvironmentEntry -Path $backendEnvPath -Name 'AUTOPROMPT_JWT_SIGNING_KEY' -Value (New-StrongDjangoSecret)
    Write-Warning 'Added a missing independent JWT signing key. The next restart invalidates existing browser access/refresh tokens and requires signing in again.'
}

$configuredKeyring = Get-EnvironmentEntry -Path $backendEnvPath -Name 'AUTOPROMPT_CONFIG_MASTER_KEYS'
$configuredActiveKey = Get-EnvironmentEntry -Path $backendEnvPath -Name 'AUTOPROMPT_CONFIG_ACTIVE_KEY_ID'
if ([bool]$configuredKeyring -xor [bool]$configuredActiveKey) {
    throw 'Database-encryption keyring configuration is incomplete; refusing to guess or replace it.'
}
if (-not $configuredKeyring) {
    Push-Location $BackendRoot
    try {
        $migrationState = (& $PythonPath manage.py showmigrations studio 2>&1 | Out-String)
        Assert-NativeSuccess 'Secret schema migration-state inspection'
    } finally {
        Pop-Location
    }
    if ($migrationState -match '(?m)^\s*\[X\]\s+0048_access_control_encrypted_configuration\s*$') {
        Push-Location $BackendRoot
        try {
            $secretCountOutput = (& $PythonPath manage.py shell -c "from studio.models import SecretVersion; print(SecretVersion.objects.count())" 2>&1 | Out-String)
            Assert-NativeSuccess 'Encrypted-secret row inspection'
        } finally {
            Pop-Location
        }
        $secretCountLine = ($secretCountOutput -split "`r?`n" | Where-Object { $_ -match '^\d+$' } | Select-Object -Last 1)
        if ($null -eq $secretCountLine) {
            throw 'Could not prove that the encrypted secret store is empty; refusing to generate a replacement key.'
        }
        if ([int]$secretCountLine -gt 0) {
            throw 'Encrypted secrets already exist but their keyring is missing. Restore the original keyring; automatic replacement would make ciphertext unrecoverable.'
        }
    }
    $databaseEncryptionKey = New-DatabaseEncryptionKey
    Set-EnvironmentEntry -Path $backendEnvPath -Name 'AUTOPROMPT_CONFIG_MASTER_KEYS' -Value "local-v1:$databaseEncryptionKey"
    Set-EnvironmentEntry -Path $backendEnvPath -Name 'AUTOPROMPT_CONFIG_ACTIVE_KEY_ID' -Value 'local-v1'
    Write-Host 'Generated an independent database-encryption keyring; it is not stored in the database.' -ForegroundColor Green
}

# A non-empty placeholder or malformed/restored keyring is not usable. Validate
# the key material and active id before migrations or secret import can write.
Push-Location $BackendRoot
try {
    & $PythonPath manage.py shell -c "from studio.services.secret_store import load_keyring; load_keyring(); print('database-keyring-ok')"
    Assert-NativeSuccess 'Database-encryption keyring validation'
} finally {
    Pop-Location
}

Push-Location $BackendRoot
try {
    & $PythonPath manage.py migrate
    Assert-NativeSuccess 'Database migration'
    & $PythonPath manage.py bootstrap_superadmin --if-missing
    Assert-NativeSuccess 'Initial super administrator bootstrap'
    if ($WithDemoProject) {
        & $PythonPath manage.py seed_data --with-demo-project
        Assert-NativeSuccess 'Explicit local demo seeding'
    }
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
    Write-Host 'No demo account was created. On a fresh install the initial super administrator password is printed once above and must be changed at first login; an existing administrator is never reset.' -ForegroundColor Green
}
Write-Host 'Setup complete. Use scripts/restart.ps1 to start the API, frontend, and all five worker roles.'
