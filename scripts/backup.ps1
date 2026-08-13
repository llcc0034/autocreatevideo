[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [string]$DestinationRoot = '',
    [string]$BackupName = '',
    [string]$BackendEnvPath = '',
    [string]$DatabaseUrl = '',
    [string]$MediaRoot = '',
    [string]$ComfyUIOutputDir = '',
    [switch]$SkipMedia,
    [switch]$SkipComfyUIOutput,
    [switch]$IncludeConfig,
    [string[]]$ConfigPath = @(),
    [switch]$AllowMissingSources,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'backup_common.ps1')

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
Assert-AutoPromptAbsoluteOverride -Name 'ProjectRoot' -Value $ProjectRoot
Assert-AutoPromptAbsoluteOverride -Name 'DestinationRoot' -Value $DestinationRoot
Assert-AutoPromptAbsoluteOverride -Name 'BackendEnvPath' -Value $BackendEnvPath
Assert-AutoPromptAbsoluteOverride -Name 'MediaRoot' -Value $MediaRoot
Assert-AutoPromptAbsoluteOverride -Name 'ComfyUIOutputDir' -Value $ComfyUIOutputDir
foreach ($item in $ConfigPath) {
    Assert-AutoPromptAbsoluteOverride -Name 'ConfigPath' -Value $item
}

$project = Get-AutoPromptTrimmedPath -Path $ProjectRoot
$settings = Get-AutoPromptRuntimeSettings `
    -ProjectRoot $project `
    -BackendEnvPath $BackendEnvPath `
    -DatabaseUrl $DatabaseUrl `
    -MediaRoot $MediaRoot `
    -ComfyUIOutputDir $ComfyUIOutputDir
$database = Get-AutoPromptDatabaseDescriptor -DatabaseUrl $settings.DatabaseUrl -BackendRoot $settings.BackendRoot

if ([string]::IsNullOrWhiteSpace($DestinationRoot)) {
    $DestinationRoot = Join-Path $project 'backups'
}
$destination = Get-AutoPromptTrimmedPath -Path $DestinationRoot
$destination = Assert-AutoPromptSafeDirectory -Path $destination -ProjectRoot $project -Purpose 'Backup destination root'
Assert-AutoPromptNoReparsePoint -Path $destination -StopRoot ([System.IO.Path]::GetPathRoot($destination)) -Purpose 'Backup destination'

if ([string]::IsNullOrWhiteSpace($BackupName)) {
    $BackupName = 'autoprompt-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + ([Guid]::NewGuid().ToString('N').Substring(0, 8))
}
if ($BackupName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,100}$') {
    throw 'BackupName may contain only letters, numbers, dot, underscore, and hyphen (maximum 101 characters).'
}
$backupRoot = [System.IO.Path]::GetFullPath((Join-Path $destination $BackupName))
if (-not (Test-AutoPromptPathWithin -Candidate $backupRoot -Root $destination)) {
    throw 'Resolved backup path escaped DestinationRoot.'
}

$warnings = New-Object 'System.Collections.Generic.List[string]'
$planItems = New-Object 'System.Collections.Generic.List[object]'
$sourceDirectories = New-Object 'System.Collections.Generic.List[string]'

function Add-PlanItem {
    param([string]$Key, [string]$Source, [bool]$Included, [string]$Reason)
    $planItems.Add([pscustomobject]@{ key = $Key; source = $Source; included = $Included; reason = $Reason })
}

if ($database.Kind -eq 'sqlite') {
    $dbExists = Test-Path -LiteralPath $database.Path -PathType Leaf
    if (-not $dbExists -and -not $AllowMissingSources) {
        throw "SQLite database does not exist: $($database.Path)"
    }
    if ($dbExists) {
        Assert-AutoPromptNoReparsePoint -Path $database.Path -StopRoot (Split-Path -Parent $database.Path) -Purpose 'SQLite source'
    }
    Add-PlanItem -Key 'database' -Source $database.Display -Included $dbExists -Reason $(if ($dbExists) { '' } else { 'source_missing' })
} else {
    if ($null -eq (Get-Command pg_dump -ErrorAction SilentlyContinue)) {
        throw 'pg_dump is required when DATABASE_URL uses PostgreSQL.'
    }
    Add-PlanItem -Key 'database' -Source $database.Display -Included $true -Reason ''
}

$media = Assert-AutoPromptSafeDirectory -Path $settings.MediaRoot -ProjectRoot $project -Purpose 'Media source'
$mediaExists = Test-Path -LiteralPath $media -PathType Container
if (-not $SkipMedia -and -not $mediaExists -and -not $AllowMissingSources) {
    throw "Media directory does not exist: $media"
}
if (-not $SkipMedia -and $mediaExists) { $sourceDirectories.Add($media) }
Add-PlanItem -Key 'media' -Source $media -Included (-not $SkipMedia -and $mediaExists) -Reason $(if ($SkipMedia) { 'explicitly_skipped' } elseif (-not $mediaExists) { 'source_missing' } else { '' })

$comfy = $settings.ComfyUIOutputDir
$comfyExists = $false
if (-not $SkipComfyUIOutput) {
    if ([string]::IsNullOrWhiteSpace($comfy)) {
        if (-not $AllowMissingSources) {
            throw 'COMFYUI_OUTPUT_DIR is not configured. Supply -ComfyUIOutputDir or use -SkipComfyUIOutput.'
        }
    } else {
        $comfy = Assert-AutoPromptSafeDirectory -Path $comfy -ProjectRoot $project -Purpose 'ComfyUI output source'
        $comfyExists = Test-Path -LiteralPath $comfy -PathType Container
        if (-not $comfyExists -and -not $AllowMissingSources) {
            throw "ComfyUI output directory does not exist: $comfy"
        }
        if ($comfyExists) { $sourceDirectories.Add($comfy) }
    }
}
Add-PlanItem -Key 'comfyui_output' -Source $comfy -Included (-not $SkipComfyUIOutput -and $comfyExists) -Reason $(if ($SkipComfyUIOutput) { 'explicitly_skipped' } elseif ([string]::IsNullOrWhiteSpace($comfy) -or -not $comfyExists) { 'source_missing_or_unconfigured' } else { '' })

$resolvedConfigPaths = New-Object 'System.Collections.Generic.List[string]'
if ($IncludeConfig) {
    if ($ConfigPath.Count -eq 0) {
        $ConfigPath = @(
            (Join-Path $project '.env'),
            (Join-Path $settings.BackendRoot '.env'),
            (Join-Path $project 'config')
        )
    }
    foreach ($candidate in $ConfigPath) {
        $full = [System.IO.Path]::GetFullPath($candidate)
        if (-not (Test-AutoPromptPathWithin -Candidate $full -Root $project)) {
            throw "ConfigPath must stay inside ProjectRoot so it can be restored safely: $full"
        }
        if (-not (Test-Path -LiteralPath $full)) {
            if ($AllowMissingSources) {
                $warnings.Add("Config source is missing and was skipped: $full")
                continue
            }
            throw "Config source does not exist: $full"
        }
        Assert-AutoPromptNoReparsePoint -Path $full -StopRoot $project -Purpose 'Config source'
        $resolvedConfigPaths.Add($full)
    }
}
Add-PlanItem -Key 'config' -Source $project -Included ($IncludeConfig -and $resolvedConfigPaths.Count -gt 0) -Reason $(if (-not $IncludeConfig) { 'opt_in_not_requested' } elseif ($resolvedConfigPaths.Count -eq 0) { 'no_existing_items' } else { 'contains_secrets_review_access_controls' })

foreach ($source in $sourceDirectories) {
    if (Test-AutoPromptPathWithin -Candidate $destination -Root $source -AllowEqual) {
        throw "Backup destination cannot be inside a source directory: destination=$destination source=$source"
    }
}

$planMode = if ($DryRun) { 'dry_run' } else { 'backup' }
$plan = [pscustomobject]@{
    mode = $planMode
    backup_path = $backupRoot
    database_kind = $database.Kind
    items = @($planItems | ForEach-Object { $_ })
    config_contains_secrets = [bool]$IncludeConfig
}
if ($DryRun) {
    Write-Host ($plan | ConvertTo-Json -Depth 6)
    Write-Output 'DRY_RUN_OK'
    return
}

if (Test-Path -LiteralPath $backupRoot) {
    throw "Backup path already exists; existing backups are never overwritten: $backupRoot"
}
New-Item -ItemType Directory -Path $backupRoot | Out-Null
$incompleteMarker = Join-Path $backupRoot '.incomplete'
Set-Content -LiteralPath $incompleteMarker -Value ('created_utc=' + (Get-AutoPromptUtcTimestamp)) -Encoding UTF8

$fileEntries = New-Object 'System.Collections.Generic.List[object]'
$configEntries = New-Object 'System.Collections.Generic.List[object]'
$categoryCounts = @{}
$categoryBytes = @{}

function Register-BackupFile {
    param(
        [Parameter(Mandatory = $true)][string]$DestinationFile,
        [Parameter(Mandatory = $true)][string]$Category
    )
    $relative = Get-AutoPromptRelativePath -Path $DestinationFile -Root $backupRoot
    $info = Get-Item -LiteralPath $DestinationFile -Force
    $hash = (Get-FileHash -LiteralPath $DestinationFile -Algorithm SHA256).Hash.ToLowerInvariant()
    $fileEntries.Add([pscustomobject]@{
        relative_path = $relative
        category = $Category
        bytes = [long]$info.Length
        sha256 = $hash
    })
    if (-not $categoryCounts.ContainsKey($Category)) { $categoryCounts[$Category] = 0; $categoryBytes[$Category] = [long]0 }
    $categoryCounts[$Category] = [int]$categoryCounts[$Category] + 1
    $categoryBytes[$Category] = [long]$categoryBytes[$Category] + [long]$info.Length
}

function Copy-SourceFileIntoBackup {
    param(
        [Parameter(Mandatory = $true)][string]$SourceFile,
        [Parameter(Mandatory = $true)][string]$BackupRelativePath,
        [Parameter(Mandatory = $true)][string]$Category
    )
    $destinationFile = Join-AutoPromptContainedPath -Root $backupRoot -RelativePath $BackupRelativePath -Purpose 'Backup file path'
    $parent = Split-Path -Parent $destinationFile
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Copy-Item -LiteralPath $SourceFile -Destination $destinationFile
    Register-BackupFile -DestinationFile $destinationFile -Category $Category
}

function Copy-SourceDirectoryIntoBackup {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$BackupPrefix,
        [Parameter(Mandatory = $true)][string]$Category
    )
    Assert-AutoPromptNoReparsePoint -Path $SourceRoot -StopRoot $SourceRoot -Purpose "$Category source"
    $items = @(Get-ChildItem -LiteralPath $SourceRoot -Force -Recurse | Sort-Object FullName)
    foreach ($item in $items) {
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Category source contains a reparse point and was not followed: $($item.FullName)"
        }
        if ($item.PSIsContainer) { continue }
        $relative = Get-AutoPromptRelativePath -Path $item.FullName -Root $SourceRoot
        Copy-SourceFileIntoBackup -SourceFile $item.FullName -BackupRelativePath ($BackupPrefix.TrimEnd('/') + '/' + $relative) -Category $Category
    }
}

try {
    $databaseIncluded = [bool]($planItems | Where-Object { $_.key -eq 'database' }).included
    $databaseBackupRelative = $null
    if ($databaseIncluded -and $database.Kind -eq 'sqlite') {
        $databaseBackupRelative = 'data/database/db.sqlite3'
        $databaseDestination = Join-AutoPromptContainedPath -Root $backupRoot -RelativePath $databaseBackupRelative -Purpose 'SQLite backup path'
        New-Item -ItemType Directory -Path (Split-Path -Parent $databaseDestination) -Force | Out-Null
        $python = Get-AutoPromptPython -BackendRoot $settings.BackendRoot
        $sqliteBackupCode = "import sqlite3,sys; src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); result=dst.execute('PRAGMA integrity_check').fetchone()[0]; dst.close(); src.close(); raise SystemExit(0 if result=='ok' else 'SQLite integrity_check failed: '+str(result))"
        Invoke-AutoPromptNative -Executable $python -Arguments @('-c', $sqliteBackupCode, $database.Path, $databaseDestination) -Step 'SQLite online backup'
        Register-BackupFile -DestinationFile $databaseDestination -Category 'database'
    } elseif ($databaseIncluded -and $database.Kind -eq 'postgresql') {
        $databaseBackupRelative = 'data/database/postgresql.dump'
        $databaseDestination = Join-AutoPromptContainedPath -Root $backupRoot -RelativePath $databaseBackupRelative -Purpose 'PostgreSQL backup path'
        New-Item -ItemType Directory -Path (Split-Path -Parent $databaseDestination) -Force | Out-Null
        $pgDump = (Get-Command pg_dump -ErrorAction Stop).Source
        Invoke-AutoPromptNative -Executable $pgDump -Arguments @('--format=custom', '--no-owner', '--no-privileges', '--file', $databaseDestination, '--dbname', $database.SafeConnectionUrl, '--no-password') -Step 'PostgreSQL backup' -PostgresPassword $database.Password
        Register-BackupFile -DestinationFile $databaseDestination -Category 'database'
    }

    if (-not $SkipMedia -and $mediaExists) {
        Copy-SourceDirectoryIntoBackup -SourceRoot $media -BackupPrefix 'data/media' -Category 'media'
    }
    if (-not $SkipComfyUIOutput -and $comfyExists) {
        Copy-SourceDirectoryIntoBackup -SourceRoot $comfy -BackupPrefix 'data/comfyui-output' -Category 'comfyui_output'
    }

    if ($IncludeConfig) {
        $seenConfigFiles = @{}
        foreach ($configSource in $resolvedConfigPaths) {
            $configFiles = if (Test-Path -LiteralPath $configSource -PathType Container) {
                @(Get-ChildItem -LiteralPath $configSource -File -Force -Recurse | Sort-Object FullName)
            } else {
                @(Get-Item -LiteralPath $configSource -Force)
            }
            foreach ($configFile in $configFiles) {
                Assert-AutoPromptNoReparsePoint -Path $configFile.FullName -StopRoot $project -Purpose 'Config source file'
                $restoreRelative = Get-AutoPromptRelativePath -Path $configFile.FullName -Root $project
                $key = $restoreRelative.ToLowerInvariant()
                if ($seenConfigFiles.ContainsKey($key)) { continue }
                $seenConfigFiles[$key] = $true
                $backupRelative = 'data/config/' + $restoreRelative
                Copy-SourceFileIntoBackup -SourceFile $configFile.FullName -BackupRelativePath $backupRelative -Category 'config'
                $configEntries.Add([pscustomobject]@{
                    backup_relative_path = $backupRelative
                    restore_relative_path = $restoreRelative
                })
            }
        }
    }

    if (-not $databaseIncluded) { $warnings.Add('Database source was missing; this backup cannot restore a database.') }
    if (-not $mediaExists -and -not $SkipMedia) { $warnings.Add('Media source was missing and was not included.') }
    if (-not $comfyExists -and -not $SkipComfyUIOutput) { $warnings.Add('ComfyUI output source was missing or unconfigured and was not included.') }

    $manifest = [ordered]@{
        format_version = 1
        application = 'AutoPrompt Studio'
        backup_id = $BackupName
        created_utc = Get-AutoPromptUtcTimestamp
        source_project_root = $project
        contains_sensitive_config = [bool]($IncludeConfig -and $configEntries.Count -gt 0)
        database = [ordered]@{
            kind = $database.Kind
            included = $databaseIncluded
            source = $database.Display
            backup_relative_path = $databaseBackupRelative
        }
        datasets = @(
            [ordered]@{ key = 'media'; included = (-not $SkipMedia -and $mediaExists); source = $media; backup_prefix = 'data/media'; file_count = [int]($categoryCounts['media']); total_bytes = [long]($categoryBytes['media']) },
            [ordered]@{ key = 'comfyui_output'; included = (-not $SkipComfyUIOutput -and $comfyExists); source = $comfy; backup_prefix = 'data/comfyui-output'; file_count = [int]($categoryCounts['comfyui_output']); total_bytes = [long]($categoryBytes['comfyui_output']) }
        )
        config = [ordered]@{
            included = [bool]($IncludeConfig -and $configEntries.Count -gt 0)
            items = @($configEntries | ForEach-Object { $_ })
        }
        files = @($fileEntries | Sort-Object relative_path)
        warnings = @($warnings | ForEach-Object { $_ })
        restore_requirements = @(
            'Verify manifest.sha256 and every file SHA-256 before restore.',
            'Stop Django workers and ComfyUI before a real restore.',
            'Use restore.ps1 -ConfirmRestore; the script creates a pre-restore backup and preserves displaced paths.'
        )
    }
    $manifestPath = Join-Path $backupRoot 'manifest.json'
    $manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
    $manifestHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath (Join-Path $backupRoot 'manifest.sha256') -Value ($manifestHash + '  manifest.json') -Encoding ASCII
    Remove-Item -LiteralPath $incompleteMarker -Force
    Write-Host "Backup completed: $backupRoot" -ForegroundColor Green
    Write-Host "Files: $($fileEntries.Count); sensitive config included: $([bool]$manifest.contains_sensitive_config)"
    Write-Output $backupRoot
} catch {
    Write-Warning "Backup did not complete. The non-overwritable diagnostic directory is retained with .incomplete: $backupRoot"
    throw
}
