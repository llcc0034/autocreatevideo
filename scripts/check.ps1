$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendRoot = Join-Path $ProjectRoot 'backend'
$FrontendRoot = Join-Path $ProjectRoot 'frontend'
$PythonPath = Join-Path $BackendRoot '.venv\Scripts\python.exe'
$OpenApiPath = Join-Path $ProjectRoot 'openapi.yaml'
$LegacyOpenApiPath = Join-Path $BackendRoot 'openapi.yaml'
. (Join-Path $PSScriptRoot 'ffmpeg_runtime.ps1')
. (Join-Path $PSScriptRoot 'node_runtime.ps1')
. (Join-Path $PSScriptRoot 'openapi_contract.ps1')

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

function Assert-NoLegacyConditioningFieldNames([string]$RootPath) {
    $ResolvedRoot = [System.IO.Path]::GetFullPath($RootPath).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $LegacyNames = @(
        ('default' + '_h3' + '_mode'),
        ('workflow' + '_h3' + '_modes'),
        ('h3' + '_modes'),
        ('h3' + '_mode')
    )
    $LegacyPattern = '(?i)(?:' + (($LegacyNames | ForEach-Object {
        [regex]::Escape($_)
    }) -join '|') + ')'
    $AllowedExtensions = @(
        '.py', '.pyi', '.ts', '.tsx', '.vue', '.js', '.mjs', '.cjs',
        '.ps1', '.psm1', '.json', '.yaml', '.yml', '.md'
    )
    $SourceRoots = @(
        (Join-Path $RootPath 'backend'),
        (Join-Path $RootPath 'frontend'),
        (Join-Path $RootPath 'scripts'),
        (Join-Path $RootPath 'docs')
    )
    $CandidateFiles = @(
        foreach ($SourceRoot in $SourceRoots) {
            if (Test-Path -LiteralPath $SourceRoot -PathType Container) {
                Get-ChildItem -LiteralPath $SourceRoot -Recurse -File | Where-Object {
                    $AllowedExtensions -contains $_.Extension.ToLowerInvariant() -and
                    $_.FullName -notmatch '[\\/](?:\.venv|node_modules|dist|\.runtime|__pycache__)[\\/]' -and
                    $_.FullName -notmatch '[\\/]backend[\\/]studio[\\/]migrations[\\/]' -and
                    $_.FullName -notmatch '[\\/]docs[\\/]e2e-short-drama-ref2va-dual-clock-test-[^\\/]+\.md$'
                }
            }
        }
        foreach ($RootFileName in @('README.md', 'openapi.yaml')) {
            $RootFile = Join-Path $RootPath $RootFileName
            if (Test-Path -LiteralPath $RootFile -PathType Leaf) {
                Get-Item -LiteralPath $RootFile
            }
        }
    )
    $Violations = @(
        foreach ($CandidateFile in $CandidateFiles) {
            Select-String -LiteralPath $CandidateFile.FullName -Pattern $LegacyPattern -AllMatches |
                ForEach-Object {
                    $ResolvedFile = [System.IO.Path]::GetFullPath($_.Path)
                    $RelativePath = if ($ResolvedFile.StartsWith(
                        $ResolvedRoot, [System.StringComparison]::OrdinalIgnoreCase
                    )) {
                        $ResolvedFile.Substring($ResolvedRoot.Length)
                    } else {
                        $ResolvedFile
                    }
                    '{0}:{1}: {2}' -f $RelativePath, $_.LineNumber, $_.Line.Trim()
                }
        }
    )
    if ($Violations.Count -gt 0) {
        throw (
            "Legacy model-specific conditioning field names remain in formal source. " +
            "Django migrations and the named historical E2E reports are the only exclusions.`n" +
            ($Violations -join "`n")
        )
    }
}

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Run scripts\setup.ps1 first.'
}
Assert-AutoPromptOpenApiAuthority `
    -RootContractPath $OpenApiPath `
    -LegacyContractPath $LegacyOpenApiPath
Assert-NoLegacyConditioningFieldNames -RootPath $ProjectRoot

$FfmpegRuntime = Resolve-AutoPromptFfmpegRuntime -ProjectRoot $ProjectRoot
if ($null -eq $FfmpegRuntime) {
    throw 'A complete FFmpeg/FFprobe runtime was not found. Run scripts\install_ffmpeg_runtime.ps1 to install the pinned runtime under .runtime\toolchains.'
}
Set-AutoPromptFfmpegEnvironment -Runtime $FfmpegRuntime
$NodeRuntime = Resolve-AutoPromptNodeRuntime -ProjectRoot $ProjectRoot
if ($null -eq $NodeRuntime) {
    throw 'A complete Node.js 22/npm runtime was not found. Run scripts\install_node_runtime.ps1 to install the pinned runtime under .runtime\toolchains.'
}
$NpmPath = $NodeRuntime.NpmPath
Set-AutoPromptNpmCacheEnvironment -ProjectRoot $ProjectRoot | Out-Null

& $PythonPath (Join-Path $PSScriptRoot 'validate_asset_library.py')
Assert-NativeSuccess 'Asset library validation'
& $PythonPath (Join-Path $PSScriptRoot 'test_validate_asset_library.py')
Assert-NativeSuccess 'Asset library provenance tamper tests'
& $PythonPath (Join-Path $PSScriptRoot 'validate_production_deployment.py')
Assert-NativeSuccess 'Production deployment static validation'
& $PythonPath (Join-Path $PSScriptRoot 'test_validate_production_deployment.py')
Assert-NativeSuccess 'Production deployment tamper tests'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'test_openapi_contract.ps1')
Assert-NativeSuccess 'OpenAPI contract drift guard tests'
Push-Location $BackendRoot
try {
    $PreviousDebug = $env:DJANGO_DEBUG
    $PreviousSecret = $env:DJANGO_SECRET_KEY
    $PreviousJwtSecret = $env:AUTOPROMPT_JWT_SIGNING_KEY
    $PreviousHosts = $env:DJANGO_ALLOWED_HOSTS
    $PreviousHsts = $env:SECURE_HSTS_SECONDS
    $PreviousHstsSubdomains = $env:SECURE_HSTS_INCLUDE_SUBDOMAINS
    $PreviousHstsPreload = $env:SECURE_HSTS_PRELOAD
    try {
        $env:DJANGO_DEBUG = 'false'
        $env:DJANGO_SECRET_KEY = 'qa-only-7ba89b64754d9c1bb7b8901081db0f53edb1a24e576ef42c'
        $env:AUTOPROMPT_JWT_SIGNING_KEY = 'qa-jwt-only-091d9b3e5b8f4d22a4c1738ed8f86e741b3c0e0f8f774def'
        $env:DJANGO_ALLOWED_HOSTS = 'localhost,127.0.0.1'
        $env:SECURE_HSTS_SECONDS = '31536000'
        $env:SECURE_HSTS_INCLUDE_SUBDOMAINS = 'true'
        $env:SECURE_HSTS_PRELOAD = 'true'
        & $PythonPath manage.py check --deploy --fail-level WARNING
        Assert-NativeSuccess 'Django deployment check'
    } finally {
        $env:DJANGO_DEBUG = $PreviousDebug
        $env:DJANGO_SECRET_KEY = $PreviousSecret
        $env:AUTOPROMPT_JWT_SIGNING_KEY = $PreviousJwtSecret
        $env:DJANGO_ALLOWED_HOSTS = $PreviousHosts
        $env:SECURE_HSTS_SECONDS = $PreviousHsts
        $env:SECURE_HSTS_INCLUDE_SUBDOMAINS = $PreviousHstsSubdomains
        $env:SECURE_HSTS_PRELOAD = $PreviousHstsPreload
    }
    & $PythonPath manage.py makemigrations --check --dry-run
    Assert-NativeSuccess 'Django migration drift check'
    $OpenApiTempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $GeneratedOpenApiPath = [System.IO.Path]::GetFullPath((
        Join-Path $OpenApiTempBase ('autoprompt-openapi-' + [guid]::NewGuid().ToString('N') + '.yaml')
    ))
    if (
        -not $GeneratedOpenApiPath.StartsWith($OpenApiTempBase, [System.StringComparison]::OrdinalIgnoreCase) -or
        (Split-Path -Leaf $GeneratedOpenApiPath) -notlike 'autoprompt-openapi-*.yaml'
    ) {
        throw 'Refusing to use an unsafe temporary OpenAPI path.'
    }
    try {
        & $PythonPath manage.py spectacular --validate --fail-on-warn --file $GeneratedOpenApiPath
        Assert-NativeSuccess 'Dynamic OpenAPI generation and validation'
        Assert-AutoPromptOpenApiContract `
            -CommittedPath $OpenApiPath `
            -GeneratedPath $GeneratedOpenApiPath
        Write-Host 'Root openapi.yaml matches the dynamically generated contract.' -ForegroundColor Green
    } finally {
        if (Test-Path -LiteralPath $GeneratedOpenApiPath -PathType Leaf) {
            Remove-Item -LiteralPath $GeneratedOpenApiPath -Force
        }
    }
    $PreviousTestSecret = $env:DJANGO_SECRET_KEY
    $PreviousTestJwtSecret = $env:AUTOPROMPT_JWT_SIGNING_KEY
    try {
        # Keep JWT/HMAC checks meaningful without depending on a developer's
        # local .env secret length or leaking that secret into test output.
        $env:DJANGO_SECRET_KEY = 'qa-only-7ba89b64754d9c1bb7b8901081db0f53edb1a24e576ef42c'
        $env:AUTOPROMPT_JWT_SIGNING_KEY = 'qa-jwt-only-091d9b3e5b8f4d22a4c1738ed8f86e741b3c0e0f8f774def'
        & $PythonPath manage.py test
        Assert-NativeSuccess 'Django test suite'
    } finally {
        $env:DJANGO_SECRET_KEY = $PreviousTestSecret
        $env:AUTOPROMPT_JWT_SIGNING_KEY = $PreviousTestJwtSecret
    }
} finally {
    Pop-Location
}

Push-Location $FrontendRoot
try {
    & $NpmPath run typecheck
    Assert-NativeSuccess 'Vue typecheck'
    & $NpmPath run build
    Assert-NativeSuccess 'Vue production build'
    & $NpmPath run test:translations
    Assert-NativeSuccess 'Translation review and provider tests'
    & $NpmPath run test:dialogue
    Assert-NativeSuccess 'Dialogue adapter tests'
    & $NpmPath run test:adapters
    Assert-NativeSuccess 'Backend adapter tests'
    & $NpmPath run test:bible-images
    Assert-NativeSuccess 'Bible image history tests'
    & $NpmPath run test:deferred-generation
    Assert-NativeSuccess 'Deferred generation diagnostics tests'
    & $NpmPath run test:shot-detail-field
    Assert-NativeSuccess 'Storyboard detailed description tests'
    & $NpmPath run test:manual
    Assert-NativeSuccess 'Versioned production operator manual tests'
    & $NpmPath run test:settings-guide
    Assert-NativeSuccess 'System settings beginner guide and bilingual presentation tests'
    & $NpmPath run test:workflow-selection
    Assert-NativeSuccess 'Production workflow selection entry tests'
    & $NpmPath run test:saved-workflow-import
    Assert-NativeSuccess 'Saved Comfy workflow import tests'
    & $NpmPath run test:workflow-inputs
    Assert-NativeSuccess 'Workflow execution input and smoke fixture presentation tests'
    & $NpmPath run test:task-grouping
    Assert-NativeSuccess 'Task grouping tests'
    & $NpmPath run test:projects
    Assert-NativeSuccess 'Series project management tests'
    & $NpmPath run test:assets
    Assert-NativeSuccess 'Asset management tests'
    & $NpmPath run test:platform
    Assert-NativeSuccess 'Platform configuration, workflow and ProductionRun tests'
} finally {
    Pop-Location
}

powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'test_backup_restore.ps1')
Assert-NativeSuccess 'Backup and restore safety tests'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'test_setup_bootstrap.ps1')
Assert-NativeSuccess 'Fresh-install setup bootstrap ordering tests'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'test_restart_config.ps1')
Assert-NativeSuccess 'Restart configuration tests'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'test_restart_processes.ps1')
Assert-NativeSuccess 'Restart process cleanup tests'

Write-Host 'All checks passed.'
