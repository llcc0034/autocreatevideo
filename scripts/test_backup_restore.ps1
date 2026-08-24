[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$scriptFiles = @(
    (Join-Path $PSScriptRoot 'backup_common.ps1'),
    (Join-Path $PSScriptRoot 'backup.ps1'),
    (Join-Path $PSScriptRoot 'restore.ps1')
)

foreach ($scriptFile in $scriptFiles) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($scriptFile, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -gt 0) {
        $messages = ($parseErrors | ForEach-Object { $_.Message }) -join '; '
        throw "PowerShell AST parse failed for $scriptFile`: $messages"
    }
}
Write-Host 'PowerShell AST validation passed.' -ForegroundColor Green

. (Join-Path $PSScriptRoot 'backup_common.ps1')
$postgresDescriptor = Get-AutoPromptDatabaseDescriptor `
    -DatabaseUrl 'postgresql://backup_user:p%40ss@127.0.0.1:5432/autoprompt?sslmode=require' `
    -BackendRoot (Join-Path (Split-Path -Parent $PSScriptRoot) 'backend')
if ($postgresDescriptor.Display -match 'p%40ss|p@ss' -or $postgresDescriptor.SafeConnectionUrl -match 'p%40ss|p@ss') {
    throw 'PostgreSQL descriptor leaked a password into a display or child-process URL.'
}
if ($postgresDescriptor.Password -ne 'p@ss') {
    throw 'PostgreSQL descriptor did not decode the password for temporary PGPASSWORD injection.'
}

$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$tempRoot = Join-Path $tempBase ('autoprompt-backup-test-' + [Guid]::NewGuid().ToString('N'))
$tempRoot = [System.IO.Path]::GetFullPath($tempRoot)
$originalProcessPath = $env:PATH
$testBackendRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'
$testPython = Get-AutoPromptPython -BackendRoot $testBackendRoot
$env:PATH = (Split-Path -Parent $testPython) + [System.IO.Path]::PathSeparator + $env:PATH
if (-not $tempRoot.StartsWith($tempBase, [System.StringComparison]::OrdinalIgnoreCase) -or (Split-Path -Leaf $tempRoot) -notlike 'autoprompt-backup-test-*') {
    throw 'Refusing to create an unsafe temporary test directory.'
}

try {
    $backendRoot = Join-Path $tempRoot 'backend'
    $mediaRoot = Join-Path $backendRoot 'media'
    $comfyRoot = Join-Path $tempRoot 'comfy-output'
    $configRoot = Join-Path $tempRoot 'config'
    $unsafeConfigRoot = Join-Path $tempRoot 'unsafe-config'
    $archiveRoot = Join-Path $tempRoot 'archives'
    New-Item -ItemType Directory -Path $mediaRoot, $comfyRoot, $configRoot, $unsafeConfigRoot, $archiveRoot -Force | Out-Null

    $databasePath = Join-Path $backendRoot 'db.sqlite3'
    $createCode = "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute('create table sample(id integer primary key, value text not null)'); db.execute('insert into sample(value) values (?)', ('backup-test',)); db.commit(); db.close()"
    & $testPython -c $createCode $databasePath
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create temporary SQLite fixture.' }

    Set-Content -LiteralPath (Join-Path $mediaRoot 'sample.txt') -Value 'media fixture' -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $comfyRoot 'sample-output.txt') -Value 'comfy fixture' -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $configRoot 'runtime.json') -Value '{"fixture":true}' -Encoding UTF8
    $backendEnv = @(
        'DATABASE_URL=sqlite:///db.sqlite3',
        ('COMFYUI_OUTPUT_DIR=' + $comfyRoot.Replace('\', '/')),
        'AUTOPROMPT_CONFIG_MASTER_KEYS=test-v1:synthetic-test-material'
    )
    Set-Content -LiteralPath (Join-Path $backendRoot '.env') -Value $backendEnv -Encoding UTF8

    Set-Content `
        -LiteralPath (Join-Path $unsafeConfigRoot 'keyring.yaml') `
        -Value 'AUTOPROMPT_CONFIG_ACTIVE_KEY_ID: test-v1' `
        -Encoding UTF8
    foreach ($mode in @('dry-run', 'backup')) {
        $unsafeName = 'reject-keyring-' + $mode
        $keyringRejected = $false
        try {
            $unsafeParameters = @{
                ProjectRoot = $tempRoot
                DestinationRoot = $archiveRoot
                BackupName = $unsafeName
                MediaRoot = $mediaRoot
                ComfyUIOutputDir = $comfyRoot
                IncludeConfig = $true
                ConfigPath = @($unsafeConfigRoot)
            }
            if ($mode -eq 'dry-run') { $unsafeParameters['DryRun'] = $true }
            & (Join-Path $PSScriptRoot 'backup.ps1') @unsafeParameters | Out-Null
        } catch {
            if ($_.Exception.Message -match 'separate offline secret escrow') {
                $keyringRejected = $true
            } else {
                throw
            }
        }
        if (-not $keyringRejected) {
            throw "Backup $mode did not reject an explicitly selected database keyring file."
        }
        if (Test-Path -LiteralPath (Join-Path $archiveRoot $unsafeName)) {
            throw "Backup $mode created an archive directory before rejecting a database keyring file."
        }
    }

    $explicitEnvRejected = $false
    try {
        & (Join-Path $PSScriptRoot 'backup.ps1') `
            -ProjectRoot $tempRoot `
            -DestinationRoot $archiveRoot `
            -BackupName 'reject-explicit-env' `
            -MediaRoot $mediaRoot `
            -ComfyUIOutputDir $comfyRoot `
            -IncludeConfig `
            -ConfigPath (Join-Path $backendRoot '.env') `
            -DryRun | Out-Null
    } catch {
        if ($_.Exception.Message -match 'Deployment \.env files') {
            $explicitEnvRejected = $true
        } else {
            throw
        }
    }
    if (-not $explicitEnvRejected) {
        throw 'Backup Dry-run did not reject an explicitly selected deployment .env file.'
    }
    if (Test-Path -LiteralPath (Join-Path $archiveRoot 'reject-explicit-env')) {
        throw 'Backup Dry-run created an archive directory before rejecting a deployment .env file.'
    }

    $dryBackup = @(& (Join-Path $PSScriptRoot 'backup.ps1') `
        -ProjectRoot $tempRoot `
        -DestinationRoot $archiveRoot `
        -MediaRoot $mediaRoot `
        -ComfyUIOutputDir $comfyRoot `
        -IncludeConfig `
        -DryRun)
    if ($dryBackup[-1] -ne 'DRY_RUN_OK') { throw 'Backup Dry-run did not complete successfully.' }

    $backupOutput = @(& (Join-Path $PSScriptRoot 'backup.ps1') `
        -ProjectRoot $tempRoot `
        -DestinationRoot $archiveRoot `
        -MediaRoot $mediaRoot `
        -ComfyUIOutputDir $comfyRoot `
        -IncludeConfig)
    $backupPath = [string]$backupOutput[-1]
    if (-not (Test-Path -LiteralPath (Join-Path $backupPath 'manifest.json') -PathType Leaf)) {
        throw 'Temporary backup did not produce manifest.json.'
    }
    $manifest = Get-Content -LiteralPath (Join-Path $backupPath 'manifest.json') -Raw | ConvertFrom-Json
    $restoredConfigPaths = @($manifest.config.items | ForEach-Object { [string]$_.restore_relative_path })
    if ($restoredConfigPaths -contains '.env' -or $restoredConfigPaths -contains 'backend/.env') {
        throw 'Default configuration backup unexpectedly included a deployment .env file.'
    }
    if ($restoredConfigPaths -notcontains 'config/runtime.json') {
        throw 'Default configuration backup did not include the dedicated config directory.'
    }

    $sourceHashesBefore = @{
        database = (Get-FileHash -LiteralPath $databasePath -Algorithm SHA256).Hash
        media = (Get-FileHash -LiteralPath (Join-Path $mediaRoot 'sample.txt') -Algorithm SHA256).Hash
        comfy = (Get-FileHash -LiteralPath (Join-Path $comfyRoot 'sample-output.txt') -Algorithm SHA256).Hash
        config = (Get-FileHash -LiteralPath (Join-Path $configRoot 'runtime.json') -Algorithm SHA256).Hash
    }
    $dryRestore = @(& (Join-Path $PSScriptRoot 'restore.ps1') `
        -BackupPath $backupPath `
        -ProjectRoot $tempRoot `
        -MediaRoot $mediaRoot `
        -ComfyUIOutputDir $comfyRoot `
        -DryRun)
    if ($dryRestore[-1] -ne 'DRY_RUN_OK') { throw 'Restore Dry-run did not complete successfully.' }
    $sourceHashesAfter = @{
        database = (Get-FileHash -LiteralPath $databasePath -Algorithm SHA256).Hash
        media = (Get-FileHash -LiteralPath (Join-Path $mediaRoot 'sample.txt') -Algorithm SHA256).Hash
        comfy = (Get-FileHash -LiteralPath (Join-Path $comfyRoot 'sample-output.txt') -Algorithm SHA256).Hash
        config = (Get-FileHash -LiteralPath (Join-Path $configRoot 'runtime.json') -Algorithm SHA256).Hash
    }
    foreach ($key in $sourceHashesBefore.Keys) {
        if ($sourceHashesBefore[$key] -ne $sourceHashesAfter[$key]) {
            throw "Restore Dry-run modified the $key source fixture."
        }
    }

    $confirmGateRejected = $false
    try {
        & (Join-Path $PSScriptRoot 'restore.ps1') `
            -BackupPath $backupPath `
            -ProjectRoot $tempRoot `
            -MediaRoot $mediaRoot `
            -ComfyUIOutputDir $comfyRoot | Out-Null
    } catch {
        if ($_.Exception.Message -match 'ConfirmRestore') {
            $confirmGateRejected = $true
        } else {
            throw
        }
    }
    if (-not $confirmGateRejected) { throw 'Restore ran without the required -ConfirmRestore gate.' }

    $tamperedFile = Join-Path $backupPath 'data\media\sample.txt'
    Add-Content -LiteralPath $tamperedFile -Value 'tamper-test' -Encoding UTF8
    $tamperRejected = $false
    try {
        & (Join-Path $PSScriptRoot 'restore.ps1') `
            -BackupPath $backupPath `
            -ProjectRoot $tempRoot `
            -MediaRoot $mediaRoot `
            -ComfyUIOutputDir $comfyRoot `
            -DryRun | Out-Null
    } catch {
        if ($_.Exception.Message -match 'length mismatch|SHA-256 mismatch') {
            $tamperRejected = $true
        } else {
            throw
        }
    }
    if (-not $tamperRejected) { throw 'Restore did not reject a tampered backup file.' }

    Write-Host 'Backup creation, manifest/hash verification, tamper rejection, and restore Dry-run tests passed.' -ForegroundColor Green
} finally {
    $env:PATH = $originalProcessPath
    if (Test-Path -LiteralPath $tempRoot) {
        $resolvedTemp = [System.IO.Path]::GetFullPath($tempRoot)
        if (-not $resolvedTemp.StartsWith($tempBase, [System.StringComparison]::OrdinalIgnoreCase) -or (Split-Path -Leaf $resolvedTemp) -notlike 'autoprompt-backup-test-*') {
            throw "Refusing recursive cleanup outside the dedicated temporary test root: $resolvedTemp"
        }
        [System.IO.Directory]::Delete($resolvedTemp, $true)
    }
}
