[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackupPath,
    [switch]$ConfirmRestore,
    [switch]$DryRun,
    [string]$ProjectRoot = '',
    [string]$PreRestoreBackupRoot = '',
    [string]$BackendEnvPath = '',
    [string]$DatabaseUrl = '',
    [string]$MediaRoot = '',
    [string]$ComfyUIOutputDir = '',
    [switch]$SkipMedia,
    [switch]$SkipComfyUIOutput,
    [switch]$SkipConfig
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'backup_common.ps1')

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
Assert-AutoPromptAbsoluteOverride -Name 'ProjectRoot' -Value $ProjectRoot
Assert-AutoPromptAbsoluteOverride -Name 'PreRestoreBackupRoot' -Value $PreRestoreBackupRoot
Assert-AutoPromptAbsoluteOverride -Name 'BackendEnvPath' -Value $BackendEnvPath
Assert-AutoPromptAbsoluteOverride -Name 'MediaRoot' -Value $MediaRoot
Assert-AutoPromptAbsoluteOverride -Name 'ComfyUIOutputDir' -Value $ComfyUIOutputDir

$project = Get-AutoPromptTrimmedPath -Path $ProjectRoot
$backupRoot = Get-AutoPromptTrimmedPath -Path (Get-AutoPromptFullPath -Path $BackupPath -BasePath (Get-Location).Path)
if (-not (Test-Path -LiteralPath $backupRoot -PathType Container)) {
    throw "Backup directory does not exist: $backupRoot"
}
if (Test-Path -LiteralPath (Join-Path $backupRoot '.incomplete')) {
    throw 'Backup contains .incomplete and cannot be restored.'
}
Assert-AutoPromptNoReparsePoint -Path $backupRoot -StopRoot $backupRoot -Purpose 'Backup root'

$manifestPath = Join-Path $backupRoot 'manifest.json'
$manifestHashPath = Join-Path $backupRoot 'manifest.sha256'
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf) -or -not (Test-Path -LiteralPath $manifestHashPath -PathType Leaf)) {
    throw 'Backup is missing manifest.json or manifest.sha256.'
}
$hashLine = (Get-Content -LiteralPath $manifestHashPath -Raw -Encoding ASCII).Trim()
if ($hashLine -notmatch '^([0-9a-fA-F]{64})\s+\*?manifest\.json$') {
    throw 'manifest.sha256 has an invalid format.'
}
$expectedManifestHash = $Matches[1].ToLowerInvariant()
$actualManifestHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualManifestHash -ne $expectedManifestHash) {
    throw "Manifest SHA-256 mismatch. Expected=$expectedManifestHash Actual=$actualManifestHash"
}

$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ([int]$manifest.format_version -ne 1 -or [string]$manifest.application -ne 'AutoPrompt Studio') {
    throw 'Unsupported or invalid backup manifest.'
}
if ([string]::IsNullOrWhiteSpace([string]$manifest.backup_id)) {
    throw 'Backup manifest has no backup_id.'
}

$verifiedEntries = New-Object 'System.Collections.Generic.List[object]'
$seenRelativePaths = @{}
foreach ($entry in @($manifest.files)) {
    $relative = [string]$entry.relative_path
    if ([string]::IsNullOrWhiteSpace($relative) -or $relative -notmatch '^data/') {
        throw "Manifest contains an invalid data path: $relative"
    }
    $key = $relative.ToLowerInvariant()
    if ($seenRelativePaths.ContainsKey($key)) { throw "Manifest contains a duplicate file path: $relative" }
    $seenRelativePaths[$key] = $true
    $filePath = Join-AutoPromptContainedPath -Root $backupRoot -RelativePath $relative -Purpose 'Manifest file'
    if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) { throw "Backup file is missing: $relative" }
    Assert-AutoPromptNoReparsePoint -Path $filePath -StopRoot $backupRoot -Purpose 'Backup file'
    $fileInfo = Get-Item -LiteralPath $filePath -Force
    if ([long]$fileInfo.Length -ne [long]$entry.bytes) {
        throw "Backup file length mismatch: $relative"
    }
    if ([string]$entry.sha256 -notmatch '^[0-9a-fA-F]{64}$') {
        throw "Manifest SHA-256 is invalid for: $relative"
    }
    $actualHash = (Get-FileHash -LiteralPath $filePath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne ([string]$entry.sha256).ToLowerInvariant()) {
        throw "Backup file SHA-256 mismatch: $relative"
    }
    $verifiedEntries.Add([pscustomobject]@{
        relative_path = $relative
        category = [string]$entry.category
        file_path = $filePath
        bytes = [long]$entry.bytes
        sha256 = $actualHash
    })
}

$settings = Get-AutoPromptRuntimeSettings `
    -ProjectRoot $project `
    -BackendEnvPath $BackendEnvPath `
    -DatabaseUrl $DatabaseUrl `
    -MediaRoot $MediaRoot `
    -ComfyUIOutputDir $ComfyUIOutputDir
$database = Get-AutoPromptDatabaseDescriptor -DatabaseUrl $settings.DatabaseUrl -BackendRoot $settings.BackendRoot
if ([bool]$manifest.database.included -and [string]$manifest.database.kind -ne $database.Kind) {
    throw "Database type mismatch. Backup=$($manifest.database.kind) Target=$($database.Kind)"
}
if ($database.Kind -eq 'sqlite') {
    $databaseParent = Split-Path -Parent $database.Path
    $databaseParent = Assert-AutoPromptSafeDirectory -Path $databaseParent -ProjectRoot $project -Purpose 'SQLite target parent'
    Assert-AutoPromptNoReparsePoint -Path $database.Path -StopRoot ([System.IO.Path]::GetPathRoot($database.Path)) -Purpose 'SQLite restore target'
} elseif ([bool]$manifest.database.included -and $null -eq (Get-Command pg_restore -ErrorAction SilentlyContinue)) {
    throw 'pg_restore is required to restore a PostgreSQL backup.'
}
$databaseEntry = $null
if ([bool]$manifest.database.included) {
    $databaseRelative = [string]$manifest.database.backup_relative_path
    $databaseEntry = @($verifiedEntries | Where-Object { $_.relative_path -eq $databaseRelative -and $_.category -eq 'database' }) | Select-Object -First 1
    if ($null -eq $databaseEntry) { throw 'Database manifest item has no verified backup file.' }
}

$media = Assert-AutoPromptSafeDirectory -Path $settings.MediaRoot -ProjectRoot $project -Purpose 'Media restore target'
Assert-AutoPromptNoReparsePoint -Path $media -StopRoot ([System.IO.Path]::GetPathRoot($media)) -Purpose 'Media restore target'
$comfy = $settings.ComfyUIOutputDir
$comfyDataset = @($manifest.datasets | Where-Object { $_.key -eq 'comfyui_output' }) | Select-Object -First 1
$mediaDataset = @($manifest.datasets | Where-Object { $_.key -eq 'media' }) | Select-Object -First 1
if ($null -eq $mediaDataset -or $null -eq $comfyDataset) {
    throw 'Backup manifest is missing a required dataset declaration.'
}
foreach ($dataset in @($mediaDataset, $comfyDataset)) {
    $actualDatasetCount = @($verifiedEntries | Where-Object { $_.category -eq [string]$dataset.key }).Count
    if ($actualDatasetCount -ne [int]$dataset.file_count) {
        throw "Dataset file count does not match the manifest: $($dataset.key)"
    }
    if (-not [bool]$dataset.included -and $actualDatasetCount -gt 0) {
        throw "Excluded dataset unexpectedly has backup files: $($dataset.key)"
    }
}
$actualDatabaseCount = @($verifiedEntries | Where-Object { $_.category -eq 'database' }).Count
if (([bool]$manifest.database.included -and $actualDatabaseCount -ne 1) -or (-not [bool]$manifest.database.included -and $actualDatabaseCount -ne 0)) {
    throw 'Database file count does not match the manifest.'
}
$actualConfigCount = @($verifiedEntries | Where-Object { $_.category -eq 'config' }).Count
if ($actualConfigCount -ne @($manifest.config.items).Count) {
    throw 'Config file count does not match the manifest.'
}
$restoreMedia = [bool]$mediaDataset.included -and -not $SkipMedia
$restoreComfy = [bool]$comfyDataset.included -and -not $SkipComfyUIOutput
$restoreConfig = [bool]$manifest.config.included -and -not $SkipConfig
if ($restoreComfy) {
    if ([string]::IsNullOrWhiteSpace($comfy)) {
        throw 'The backup contains ComfyUI output, but no absolute ComfyUI target is configured.'
    }
    $comfy = Assert-AutoPromptSafeDirectory -Path $comfy -ProjectRoot $project -Purpose 'ComfyUI restore target'
    Assert-AutoPromptNoReparsePoint -Path $comfy -StopRoot ([System.IO.Path]::GetPathRoot($comfy)) -Purpose 'ComfyUI restore target'
}

if ($restoreMedia -and $restoreComfy) {
    if ((Test-AutoPromptPathWithin -Candidate $media -Root $comfy -AllowEqual) -or (Test-AutoPromptPathWithin -Candidate $comfy -Root $media -AllowEqual)) {
        throw 'Media and ComfyUI restore targets must not overlap.'
    }
}
if ($database.Kind -eq 'sqlite' -and $null -ne $databaseEntry) {
    if (($restoreMedia -and (Test-AutoPromptPathWithin -Candidate $database.Path -Root $media -AllowEqual)) -or ($restoreComfy -and (Test-AutoPromptPathWithin -Candidate $database.Path -Root $comfy -AllowEqual))) {
        throw 'SQLite target file must not be inside a directory target that will be replaced.'
    }
}
foreach ($targetDirectory in @($media, $comfy)) {
    if (-not [string]::IsNullOrWhiteSpace($targetDirectory) -and (Test-AutoPromptPathWithin -Candidate $backupRoot -Root $targetDirectory -AllowEqual)) {
        throw "BackupPath cannot be stored inside a restore target: $targetDirectory"
    }
}

$configTargets = New-Object 'System.Collections.Generic.List[object]'
$seenConfigTargets = @{}
if ($restoreConfig) {
    foreach ($item in @($manifest.config.items)) {
        $backupRelative = [string]$item.backup_relative_path
        $restoreRelative = [string]$item.restore_relative_path
        $sourceEntry = @($verifiedEntries | Where-Object { $_.relative_path -eq $backupRelative -and $_.category -eq 'config' }) | Select-Object -First 1
        if ($null -eq $sourceEntry) { throw "Config manifest item has no verified file: $backupRelative" }
        $target = Join-AutoPromptContainedPath -Root $project -RelativePath $restoreRelative -Purpose 'Config restore path'
        Assert-AutoPromptNoReparsePoint -Path $target -StopRoot $project -Purpose 'Config restore target'
        $targetKey = $target.ToLowerInvariant()
        if ($seenConfigTargets.ContainsKey($targetKey)) { throw "Duplicate config restore target: $restoreRelative" }
        $seenConfigTargets[$targetKey] = $true
        if ((Test-AutoPromptPathWithin -Candidate $target -Root $media -AllowEqual) -or ($restoreComfy -and (Test-AutoPromptPathWithin -Candidate $target -Root $comfy -AllowEqual))) {
            throw "Config restore path overlaps a dataset target: $restoreRelative"
        }
        if ($null -ne $databaseEntry -and $database.Kind -eq 'sqlite' -and $target.Equals($database.Path, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Config restore path overlaps the SQLite target: $restoreRelative"
        }
        $configTargets.Add([pscustomobject]@{ source = $sourceEntry.file_path; target = $target; restore_relative_path = $restoreRelative })
    }
}

$planMode = if ($DryRun) { 'dry_run' } else { 'restore' }
$plan = [pscustomobject]@{
    mode = $planMode
    backup_id = [string]$manifest.backup_id
    verified_files = $verifiedEntries.Count
    database = [pscustomobject]@{ included = [bool]$manifest.database.included; kind = $database.Kind; target = $database.Display }
    media = [pscustomobject]@{ restore = $restoreMedia; target = $media; files = @($verifiedEntries | Where-Object { $_.category -eq 'media' }).Count }
    comfyui_output = [pscustomobject]@{ restore = $restoreComfy; target = $comfy; files = @($verifiedEntries | Where-Object { $_.category -eq 'comfyui_output' }).Count }
    config = [pscustomobject]@{ restore = $restoreConfig; target_root = $project; files = $configTargets.Count; contains_secrets = [bool]$manifest.contains_sensitive_config }
}

if ($DryRun) {
    Write-Host ($plan | ConvertTo-Json -Depth 6)
    Write-Host 'Dry-run completed: hashes and absolute target boundaries are valid; no target was modified.' -ForegroundColor Green
    Write-Output 'DRY_RUN_OK'
    return
}
if (-not $ConfirmRestore) {
    throw 'Real restore is disabled unless -ConfirmRestore is explicitly supplied. Run with -DryRun first.'
}

if ([string]::IsNullOrWhiteSpace($PreRestoreBackupRoot)) {
    $PreRestoreBackupRoot = Join-Path $project 'backups\pre-restore'
}
$preRestoreRoot = Get-AutoPromptTrimmedPath -Path $PreRestoreBackupRoot
$preRestoreRoot = Assert-AutoPromptSafeDirectory -Path $preRestoreRoot -ProjectRoot $project -Purpose 'Pre-restore backup root'
foreach ($targetDirectory in @($media, $comfy)) {
    if (-not [string]::IsNullOrWhiteSpace($targetDirectory) -and (Test-AutoPromptPathWithin -Candidate $preRestoreRoot -Root $targetDirectory -AllowEqual)) {
        throw "Pre-restore backup root cannot be inside a restore target: $targetDirectory"
    }
}

Write-Warning 'Stop Django API/workers and ComfyUI before continuing. The script does not terminate running services.'
Write-Host 'Creating mandatory pre-restore backup before modifying exact targets...'
$preBackupParameters = @{
    ProjectRoot = $project
    DestinationRoot = $preRestoreRoot
    BackendEnvPath = $settings.BackendEnvPath
    DatabaseUrl = $settings.DatabaseUrl
    MediaRoot = $media
    SkipMedia = (-not $restoreMedia)
    SkipComfyUIOutput = (-not $restoreComfy)
    IncludeConfig = $restoreConfig
    AllowMissingSources = $true
}
if ($restoreComfy) { $preBackupParameters['ComfyUIOutputDir'] = $comfy }
if ($restoreConfig) {
    $preBackupParameters['ConfigPath'] = @($configTargets | ForEach-Object { $_.target })
}
$preBackupOutput = @(& (Join-Path $PSScriptRoot 'backup.ps1') @preBackupParameters)
$preBackupPath = [string]($preBackupOutput | Select-Object -Last 1)
if ([string]::IsNullOrWhiteSpace($preBackupPath) -or -not (Test-Path -LiteralPath $preBackupPath -PathType Container)) {
    throw 'Mandatory pre-restore backup did not return a valid completed path; restore was not started.'
}
Write-Host "Pre-restore backup completed: $preBackupPath" -ForegroundColor Green

$restoreStamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + ([Guid]::NewGuid().ToString('N').Substring(0, 8))
$preservedTargets = New-Object 'System.Collections.Generic.List[string]'

function Restore-VerifiedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Category,
        [Parameter(Mandatory = $true)][string]$BackupPrefix,
        [Parameter(Mandatory = $true)][string]$TargetDirectory
    )
    $targetParent = Split-Path -Parent $TargetDirectory
    if (-not (Test-Path -LiteralPath $targetParent -PathType Container)) {
        New-Item -ItemType Directory -Path $targetParent -Force | Out-Null
    }
    $leaf = Split-Path -Leaf $TargetDirectory
    $stage = Join-Path $targetParent ('.autoprompt-restore-stage-' + $restoreStamp + '-' + $leaf)
    $preserved = $TargetDirectory + '.pre-restore-' + $restoreStamp
    if ((Test-Path -LiteralPath $stage) -or (Test-Path -LiteralPath $preserved)) {
        throw "Restore staging or preserved path already exists for target: $TargetDirectory"
    }
    New-Item -ItemType Directory -Path $stage | Out-Null
    $stageReady = $false
    $targetMoved = $false
    try {
        $prefix = $BackupPrefix.TrimEnd('/') + '/'
        foreach ($entry in @($verifiedEntries | Where-Object { $_.category -eq $Category })) {
            if (-not $entry.relative_path.StartsWith($prefix, [System.StringComparison]::Ordinal)) {
                throw "Verified $Category entry is outside its declared prefix: $($entry.relative_path)"
            }
            $relative = $entry.relative_path.Substring($prefix.Length)
            $stageFile = Join-AutoPromptContainedPath -Root $stage -RelativePath $relative -Purpose "$Category staging path"
            New-Item -ItemType Directory -Path (Split-Path -Parent $stageFile) -Force | Out-Null
            Copy-Item -LiteralPath $entry.file_path -Destination $stageFile
            $stageHash = (Get-FileHash -LiteralPath $stageFile -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($stageHash -ne $entry.sha256) { throw "Staged file hash mismatch: $relative" }
        }
        $stageReady = $true
        if (Test-Path -LiteralPath $TargetDirectory) {
            Move-Item -LiteralPath $TargetDirectory -Destination $preserved
            $targetMoved = $true
            $preservedTargets.Add($preserved)
        }
        Move-Item -LiteralPath $stage -Destination $TargetDirectory
    } catch {
        if ($targetMoved -and -not (Test-Path -LiteralPath $TargetDirectory) -and (Test-Path -LiteralPath $preserved)) {
            Move-Item -LiteralPath $preserved -Destination $TargetDirectory
        }
        throw
    } finally {
        if (-not $stageReady -and (Test-Path -LiteralPath $stage)) {
            Write-Warning "Incomplete staging directory retained for diagnosis: $stage"
        }
    }
}

function Restore-VerifiedFile {
    param(
        [Parameter(Mandatory = $true)][string]$SourceFile,
        [Parameter(Mandatory = $true)][string]$ExpectedHash,
        [Parameter(Mandatory = $true)][string]$TargetFile,
        [Parameter(Mandatory = $true)][string]$Purpose
    )
    $parent = Split-Path -Parent $TargetFile
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $stage = $TargetFile + '.restore-stage-' + $restoreStamp
    $preserved = $TargetFile + '.pre-restore-' + $restoreStamp
    if ((Test-Path -LiteralPath $stage) -or (Test-Path -LiteralPath $preserved)) {
        throw "$Purpose staging or preserved path already exists: $TargetFile"
    }
    Copy-Item -LiteralPath $SourceFile -Destination $stage
    $stageHash = (Get-FileHash -LiteralPath $stage -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($stageHash -ne $ExpectedHash) { throw "$Purpose staged file hash mismatch." }
    $targetMoved = $false
    try {
        if (Test-Path -LiteralPath $TargetFile) {
            Move-Item -LiteralPath $TargetFile -Destination $preserved
            $targetMoved = $true
            $preservedTargets.Add($preserved)
        }
        Move-Item -LiteralPath $stage -Destination $TargetFile
    } catch {
        if ($targetMoved -and -not (Test-Path -LiteralPath $TargetFile) -and (Test-Path -LiteralPath $preserved)) {
            Move-Item -LiteralPath $preserved -Destination $TargetFile
        }
        throw
    }
}

if ($null -ne $databaseEntry -and $database.Kind -eq 'sqlite') {
    $python = Get-AutoPromptPython -BackendRoot $settings.BackendRoot
    $sqliteMaintenance = Join-Path $PSScriptRoot 'sqlite_maintenance.py'
    Invoke-AutoPromptNative -Executable $python -Arguments @($sqliteMaintenance, 'validate', $databaseEntry.file_path) -Step 'SQLite restore source validation'
    Restore-VerifiedFile -SourceFile $databaseEntry.file_path -ExpectedHash $databaseEntry.sha256 -TargetFile $database.Path -Purpose 'SQLite restore'
} elseif ($null -ne $databaseEntry -and $database.Kind -eq 'postgresql') {
    $pgRestore = (Get-Command pg_restore -ErrorAction Stop).Source
    Invoke-AutoPromptNative -Executable $pgRestore -Arguments @('--single-transaction', '--clean', '--if-exists', '--no-owner', '--no-privileges', '--exit-on-error', '--dbname', $database.SafeConnectionUrl, '--no-password', $databaseEntry.file_path) -Step 'PostgreSQL restore' -PostgresPassword $database.Password
}

if ($restoreMedia) {
    Restore-VerifiedDirectory -Category 'media' -BackupPrefix ([string]$mediaDataset.backup_prefix) -TargetDirectory $media
}
if ($restoreComfy) {
    Restore-VerifiedDirectory -Category 'comfyui_output' -BackupPrefix ([string]$comfyDataset.backup_prefix) -TargetDirectory $comfy
}
if ($restoreConfig) {
    foreach ($configTarget in $configTargets) {
        $configEntry = @($verifiedEntries | Where-Object { $_.file_path -eq $configTarget.source }) | Select-Object -First 1
        Restore-VerifiedFile -SourceFile $configTarget.source -ExpectedHash $configEntry.sha256 -TargetFile $configTarget.target -Purpose 'Config restore'
    }
}

Write-Host 'Restore completed.' -ForegroundColor Green
Write-Host "Mandatory pre-restore backup: $preBackupPath"
if ($preservedTargets.Count -gt 0) {
    Write-Host 'Original filesystem targets were preserved and were not deleted:' -ForegroundColor Yellow
    foreach ($path in $preservedTargets) { Write-Host "  $path" }
}
Write-Host 'Restart services, run scripts\check.ps1, and inspect representative media before manually removing any preserved copy.'
