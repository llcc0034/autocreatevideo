$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendRoot = Join-Path $ProjectRoot 'backend'
$FrontendRoot = Join-Path $ProjectRoot 'frontend'
$PythonPath = Join-Path $BackendRoot '.venv\Scripts\python.exe'

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Run scripts\setup.ps1 first.'
}

& $PythonPath (Join-Path $PSScriptRoot 'validate_asset_library.py')
Assert-NativeSuccess 'Asset library validation'
& $PythonPath (Join-Path $PSScriptRoot 'test_validate_asset_library.py')
Assert-NativeSuccess 'Asset library provenance tamper tests'
& $PythonPath (Join-Path $PSScriptRoot 'validate_production_deployment.py')
Assert-NativeSuccess 'Production deployment static validation'
& $PythonPath (Join-Path $PSScriptRoot 'test_validate_production_deployment.py')
Assert-NativeSuccess 'Production deployment tamper tests'
Push-Location $BackendRoot
try {
    $PreviousDebug = $env:DJANGO_DEBUG
    $PreviousSecret = $env:DJANGO_SECRET_KEY
    $PreviousHosts = $env:DJANGO_ALLOWED_HOSTS
    $PreviousHsts = $env:SECURE_HSTS_SECONDS
    $PreviousHstsSubdomains = $env:SECURE_HSTS_INCLUDE_SUBDOMAINS
    $PreviousHstsPreload = $env:SECURE_HSTS_PRELOAD
    try {
        $env:DJANGO_DEBUG = 'false'
        $env:DJANGO_SECRET_KEY = 'qa-only-7ba89b64754d9c1bb7b8901081db0f53edb1a24e576ef42c'
        $env:DJANGO_ALLOWED_HOSTS = 'localhost,127.0.0.1'
        $env:SECURE_HSTS_SECONDS = '31536000'
        $env:SECURE_HSTS_INCLUDE_SUBDOMAINS = 'true'
        $env:SECURE_HSTS_PRELOAD = 'true'
        & $PythonPath manage.py check --deploy --fail-level WARNING
        Assert-NativeSuccess 'Django deployment check'
    } finally {
        $env:DJANGO_DEBUG = $PreviousDebug
        $env:DJANGO_SECRET_KEY = $PreviousSecret
        $env:DJANGO_ALLOWED_HOSTS = $PreviousHosts
        $env:SECURE_HSTS_SECONDS = $PreviousHsts
        $env:SECURE_HSTS_INCLUDE_SUBDOMAINS = $PreviousHstsSubdomains
        $env:SECURE_HSTS_PRELOAD = $PreviousHstsPreload
    }
    & $PythonPath manage.py makemigrations --check --dry-run
    Assert-NativeSuccess 'Django migration drift check'
    & $PythonPath manage.py spectacular --validate --file openapi.yaml
    Assert-NativeSuccess 'OpenAPI generation and validation'
    $PreviousTestSecret = $env:DJANGO_SECRET_KEY
    try {
        # Keep JWT/HMAC checks meaningful without depending on a developer's
        # local .env secret length or leaking that secret into test output.
        $env:DJANGO_SECRET_KEY = 'qa-only-7ba89b64754d9c1bb7b8901081db0f53edb1a24e576ef42c'
        & $PythonPath manage.py test
        Assert-NativeSuccess 'Django test suite'
    } finally {
        $env:DJANGO_SECRET_KEY = $PreviousTestSecret
    }
} finally {
    Pop-Location
}

Push-Location $FrontendRoot
try {
    npm run typecheck
    Assert-NativeSuccess 'Vue typecheck'
    npm run build
    Assert-NativeSuccess 'Vue production build'
    npm run test:translations
    Assert-NativeSuccess 'Translation review and provider tests'
    npm run test:dialogue
    Assert-NativeSuccess 'Dialogue adapter tests'
    npm run test:adapters
    Assert-NativeSuccess 'Backend adapter tests'
    npm run test:creative
    Assert-NativeSuccess 'Creative draft failure recovery tests'
    npm run test:shot-detail
    Assert-NativeSuccess 'Single-shot high-detail draft UI tests'
    npm run test:visual-rules
    Assert-NativeSuccess 'Global visual rules draft tests'
    npm run test:manual
    Assert-NativeSuccess 'Versioned production operator manual tests'
} finally {
    Pop-Location
}

powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'test_backup_restore.ps1')
Assert-NativeSuccess 'Backup and restore safety tests'

Write-Host 'All checks passed.'
