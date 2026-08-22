[CmdletBinding()]
param(
    [switch]$SkipComfyUI,
    [switch]$RestartComfyUI,
    [switch]$ForceComfyUIRestart,
    [switch]$ForcePortTakeover,
    [switch]$OpenBrowser,
    [switch]$SkipMigrate,
    [switch]$SkipSeedData,
    [int]$StartupTimeoutSeconds = 180,
    [string]$ComfyUIRoot = $env:AUTOPROMPT_COMFYUI_ROOT
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackendRoot = Join-Path $ProjectRoot 'backend'
$FrontendRoot = Join-Path $ProjectRoot 'frontend'
$RuntimeRoot = Join-Path $ProjectRoot '.runtime'
$LogRoot = Join-Path $RuntimeRoot 'logs'
$StatePath = Join-Path $RuntimeRoot 'services.json'
$BackendPython = Join-Path $BackendRoot '.venv\Scripts\python.exe'
$LocalTranslationModel = Join-Path $ProjectRoot '.runtime\models\Qwen\Qwen3-4B-GGUF\Qwen3-4B-Q4_K_M.gguf'
$LocalTranslationRuntime = Join-Path $ProjectRoot '.runtime\qwen-python\Scripts\python.exe'
$RunStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$ServiceRecords = [System.Collections.Generic.List[object]]::new()
$KnownProjectProcessIds = [System.Collections.Generic.HashSet[int]]::new()
. (Join-Path $PSScriptRoot 'restart_config.ps1')
. (Join-Path $PSScriptRoot 'node_runtime.ps1')
. (Join-Path $PSScriptRoot 'ffmpeg_runtime.ps1')
. (Join-Path $PSScriptRoot 'python_runtime.ps1')

if ($SkipComfyUI -and $RestartComfyUI) {
    throw '-SkipComfyUI and -RestartComfyUI cannot be used together.'
}
if ($ForceComfyUIRestart -and -not $RestartComfyUI) {
    throw '-ForceComfyUIRestart requires -RestartComfyUI.'
}
if ($StartupTimeoutSeconds -lt 10) {
    throw 'StartupTimeoutSeconds must be at least 10.'
}

function Normalize-PathEnvironment {
    # Some desktop/sandbox launchers inject both Path and PATH. Windows treats
    # them as the same variable, but Windows PowerShell 5.1 Start-Process throws
    # while copying that duplicated environment block. Collapse it once before
    # starting any child service.
    $environment = [Environment]::GetEnvironmentVariables()
    $pathKeys = @($environment.Keys | Where-Object { ([string]$_) -ieq 'PATH' })
    if ($pathKeys.Count -le 1) { return }
    $entries = foreach ($key in $pathKeys) {
        ([string]$environment[$key] -split ';') | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    }
    $normalized = (@($entries | Select-Object -Unique) -join ';')
    foreach ($key in $pathKeys) {
        [Environment]::SetEnvironmentVariable([string]$key, $null, 'Process')
    }
    [Environment]::SetEnvironmentVariable('Path', $normalized, 'Process')
}

Normalize-PathEnvironment

if (-not ('AutoPromptProcessInfo' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;

public static class AutoPromptProcessInfo
{
    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_BASIC_INFORMATION
    {
        public IntPtr Reserved1;
        public IntPtr PebBaseAddress;
        public IntPtr Reserved2_0;
        public IntPtr Reserved2_1;
        public IntPtr UniqueProcessId;
        public IntPtr InheritedFromUniqueProcessId;
    }

    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(
        IntPtr processHandle,
        int processInformationClass,
        ref PROCESS_BASIC_INFORMATION processInformation,
        int processInformationLength,
        out int returnLength);

    public static int ParentProcessId(int processId)
    {
        using (Process process = Process.GetProcessById(processId))
        {
            PROCESS_BASIC_INFORMATION info = new PROCESS_BASIC_INFORMATION();
            int returnLength;
            int status = NtQueryInformationProcess(
                process.Handle, 0, ref info, Marshal.SizeOf(info), out returnLength);
            if (status != 0) { return 0; }
            return info.InheritedFromUniqueProcessId.ToInt32();
        }
    }
}
'@
}

if (-not $SkipComfyUI) {
    $resolvedComfyUI = Resolve-AutoPromptComfyUIRoot `
        -ExplicitRoot $ComfyUIRoot `
        -ProcessOutputDirectory $env:COMFYUI_OUTPUT_DIR `
        -BackendEnvPath (Join-Path $BackendRoot '.env') `
        -ProcessOutputHostDirectory $env:COMFYUI_OUTPUT_HOST_DIR `
        -RootEnvPath (Join-Path $ProjectRoot '.env')
    if ($null -ne $resolvedComfyUI) {
        $ComfyUIRoot = $resolvedComfyUI.Root
        if ($resolvedComfyUI.Source -ne 'explicit') {
            Write-Host "ComfyUI root resolved from $($resolvedComfyUI.Source): $ComfyUIRoot" -ForegroundColor DarkGray
        }
    }
}

if ([string]::IsNullOrWhiteSpace($ComfyUIRoot)) {
    if (-not $SkipComfyUI) {
        throw 'ComfyUI root is not configured. Set AUTOPROMPT_COMFYUI_ROOT, pass -ComfyUIRoot, configure backend\.env COMFYUI_OUTPUT_DIR as <portable-root>\ComfyUI\output, or use -SkipComfyUI.'
    }
    # Join-Path below still needs a non-empty base even though ComfyUI checks
    # and launch are disabled for this run.
    $ComfyUIRoot = $ProjectRoot
}

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Assert-LastExitCode([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

function Get-DescendantProcessIds([int]$ProcessId) {
    $parentById = @{}
    foreach ($candidate in @(Get-Process -ErrorAction SilentlyContinue)) {
        try {
            $parentById[$candidate.Id] = [AutoPromptProcessInfo]::ParentProcessId($candidate.Id)
        } catch {
            # System/elevated processes can deny handle access; they cannot be
            # descendants of the non-elevated services started by this script.
        }
    }
    $pending = [System.Collections.Generic.Queue[int]]::new()
    $pending.Enqueue($ProcessId)
    $descendants = [System.Collections.Generic.List[int]]::new()
    while ($pending.Count -gt 0) {
        $parentId = $pending.Dequeue()
        foreach ($entry in $parentById.GetEnumerator()) {
            if ([int]$entry.Value -eq $parentId -and -not $descendants.Contains([int]$entry.Key)) {
                $childId = [int]$entry.Key
                $descendants.Add($childId)
                $pending.Enqueue($childId)
            }
        }
    }
    return @($descendants)
}

function Stop-ProcessTree([int]$ProcessId, [string]$Reason) {
    if ($ProcessId -le 0 -or $ProcessId -eq $PID) { return }
    # A launcher such as npm.cmd can exit after spawning its long-lived child.
    # Resolve descendants before checking the root so rollback still removes
    # processes created by this run without falling back to a broad name/port
    # kill that could terminate an unrelated application.
    $descendants = @(Get-DescendantProcessIds $ProcessId)
    $target = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if (-not $target -and $descendants.Count -eq 0) { return }
    if ($target) {
        Write-Host "Stopping $Reason (PID $ProcessId, $($target.ProcessName))..." -ForegroundColor DarkYellow
    } else {
        Write-Host "Stopping $Reason descendants (launcher PID $ProcessId already exited)..." -ForegroundColor DarkYellow
    }
    for ($index = $descendants.Count - 1; $index -ge 0; $index--) {
        Stop-Process -Id $descendants[$index] -Force -ErrorAction SilentlyContinue
    }
    if ($target) {
        try {
            Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        } catch {
            # The process can exit after Get-Process succeeds. Treat that race
            # as already stopped, but preserve access-denied and other errors
            # while the same PID is still alive.
            if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
                throw
            }
        }
    }
    Start-Sleep -Milliseconds 250
}

function Get-PortOwners([int]$Port) {
    try {
        return @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction Stop |
            Select-Object -ExpandProperty OwningProcess -Unique)
    } catch {
        $owners = @()
        foreach ($line in (& netstat.exe -ano -p tcp 2>$null)) {
            if ($line -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$") {
                $owners += [int]$Matches[1]
            }
        }
        return @($owners | Select-Object -Unique)
    }
}

function Stop-PortListener([int]$Port, [string[]]$ExpectedNames, [string]$Label) {
    foreach ($ownerId in @(Get-PortOwners $Port)) {
        $owner = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
        if (-not $owner) { continue }
        if ($ExpectedNames -notcontains $owner.ProcessName.ToLowerInvariant()) {
            throw "Port $Port is occupied by unexpected process $($owner.ProcessName) (PID $ownerId). Refusing to terminate it."
        }
        $explicitComfyRestart = $Port -eq 8188 -and $RestartComfyUI
        if (-not $KnownProjectProcessIds.Contains([int]$ownerId) -and -not $ForcePortTakeover -and -not $explicitComfyRestart) {
            throw "Port $Port is owned by an unrecorded $($owner.ProcessName) process (PID $ownerId). Refusing to terminate it. If you verified that it belongs to AutoPrompt, rerun with -ForcePortTakeover."
        }
        Stop-ProcessTree $ownerId "$Label listener on port $Port"
    }
}

function Stop-RecordedServices {
    if (-not (Test-Path -LiteralPath $StatePath)) { return }
    try {
        $state = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Write-Warning "Could not read previous runtime state: $($_.Exception.Message)"
        return
    }
    $stopErrors = [System.Collections.Generic.List[string]]::new()
    foreach ($service in @($state.services)) {
        try {
            if ($service.name -eq 'ComfyUI' -and -not $RestartComfyUI) {
                Write-Host 'Keeping the previously started ComfyUI process.' -ForegroundColor Green
                continue
            }
            $recorded = Get-Process -Id ([int]$service.pid) -ErrorAction SilentlyContinue
            if (-not $recorded) { continue }
            $expectedStart = if ($service.process_start_time) { [datetime]$service.process_start_time } else { [datetime]$service.started_at }
            if ([Math]::Abs(($recorded.StartTime - $expectedStart).TotalSeconds) -gt 10) {
                Write-Warning "Skipping stale PID $($service.pid) for $($service.name); the PID has been reused."
                continue
            }
            $KnownProjectProcessIds.Add([int]$service.pid) | Out-Null
            foreach ($childId in @(Get-DescendantProcessIds ([int]$service.pid))) {
                $KnownProjectProcessIds.Add([int]$childId) | Out-Null
            }
            Stop-ProcessTree ([int]$service.pid) "recorded $($service.name)"
        } catch {
            $message = "Could not stop recorded $($service.name) (PID $($service.pid)): $($_.Exception.Message)"
            Write-Warning $message
            if (Get-Process -Id ([int]$service.pid) -ErrorAction SilentlyContinue) {
                $stopErrors.Add($message) | Out-Null
            }
        }
    }
    if ($stopErrors.Count -gt 0) {
        throw "Recorded service cleanup is incomplete; refusing to start duplicate services. $($stopErrors -join ' | ')"
    }
}

function Stop-ProjectPythonProcesses {
    $expectedPath = [System.IO.Path]::GetFullPath($BackendPython)
    foreach ($process in @(Get-Process python, pythonw -ErrorAction SilentlyContinue)) {
        try {
            $commandLine = (Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)" -ErrorAction SilentlyContinue).CommandLine
            $isAutoPromptService = $commandLine -match 'manage\.py\s+(runserver|run_generation_worker|run_assembly_worker)(?:\s|$)'
            if ($process.Path -and [System.IO.Path]::GetFullPath($process.Path) -ieq $expectedPath -and $isAutoPromptService) {
                $KnownProjectProcessIds.Add([int]$process.Id) | Out-Null
                foreach ($childId in @(Get-DescendantProcessIds $process.Id)) {
                    $KnownProjectProcessIds.Add([int]$childId) | Out-Null
                }
                Stop-ProcessTree $process.Id 'AutoPrompt backend/worker'
            }
        } catch {
            # A process can exit between enumeration and inspection.
        }
    }
}

function Start-ServiceProcess(
    [string]$Name,
    [string]$FilePath,
    [string[]]$Arguments,
    [string]$WorkingDirectory
) {
    $safeName = $Name.ToLowerInvariant() -replace '[^a-z0-9-]', '-'
    $stdout = Join-Path $LogRoot "$RunStamp-$safeName.out.log"
    $stderr = Join-Path $LogRoot "$RunStamp-$safeName.err.log"
    $process = Start-Process -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    $startedAt = (Get-Date).ToString('o')
    $processStartTime = $startedAt
    try {
        $processStartTime = $process.StartTime.ToString('o')
    } catch {
        # The process may fail immediately. Record its PID anyway so the catch
        # block can still clean up any child it managed to spawn.
    }
    $ServiceRecords.Add([pscustomobject]@{
        name = $Name
        pid = $process.Id
        started_at = $startedAt
        process_start_time = $processStartTime
        stdout = $stdout
        stderr = $stderr
    })
    Write-Host "Started $Name (PID $($process.Id))." -ForegroundColor Green
    return $process
}

function Test-Http([string]$Url) {
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    } catch {
        return $false
    }
}

function Get-ComfyUIQueueActivity {
    try {
        $queue = Invoke-RestMethod -Uri 'http://127.0.0.1:8188/queue' -TimeoutSec 5
        return [pscustomobject]@{
            running = @($queue.queue_running).Count
            pending = @($queue.queue_pending).Count
        }
    } catch {
        return $null
    }
}

function Wait-Http([string]$Name, [string]$Url, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Http $Url) {
            Write-Host "$Name is ready: $Url" -ForegroundColor Green
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "$Name did not become ready within $TimeoutSeconds seconds. Check $LogRoot."
}

function Get-RestartMutexName {
    # Scope the lock to this checkout. Two accidental double-clicks cannot
    # interleave destructive stop/start steps, while a separate checkout can
    # still be operated independently.
    $normalizedRoot = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\').ToUpperInvariant()
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($normalizedRoot))
        $suffix = ([System.BitConverter]::ToString($hash)).Replace('-', '').Substring(0, 16)
        return "Local\AutoPromptStudioRestart-$suffix"
    } finally {
        $sha256.Dispose()
    }
}

$RestartMutexName = Get-RestartMutexName
$RestartMutex = [System.Threading.Mutex]::new($false, $RestartMutexName)
$RestartMutexAcquired = $false
try {
    $RestartMutexAcquired = $RestartMutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $RestartMutexAcquired = $true
}
if (-not $RestartMutexAcquired) {
    $RestartMutex.Dispose()
    throw "Another AutoPrompt restart is already running for $ProjectRoot. Wait for it to finish instead of starting a second copy."
}

$StateWritten = $false
$PreviousServicesStopped = $false
try {
New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null

if (-not (Test-AutoPromptPythonExecutable -PythonPath $BackendPython)) {
    throw "Backend Python virtual environment is missing or references a removed base runtime: $BackendPython. Run scripts\setup.ps1 to repair it from the pinned E-drive Python runtime."
}
if (-not (Test-Path -LiteralPath (Join-Path $BackendRoot '.env'))) {
    throw 'backend\.env is missing. Run scripts\setup.ps1 first.'
}
if (-not (Test-Path -LiteralPath (Join-Path $FrontendRoot 'node_modules'))) {
    throw "Frontend dependencies are missing. Run scripts\setup.ps1 first."
}
if (-not (Test-Path -LiteralPath $LocalTranslationModel) -or -not (Test-Path -LiteralPath $LocalTranslationRuntime)) {
    Write-Warning 'Optional local Qwen director-field translation is not installed; the studio can start, but local automatic translation will remain unavailable.'
    Write-Host 'Install it once with: powershell -ExecutionPolicy Bypass -File .\scripts\download_local_translation_model.ps1 -Runtime cpu' -ForegroundColor Yellow
}
$nodeRuntime = Resolve-AutoPromptNodeRuntime -ProjectRoot $ProjectRoot
if (-not $nodeRuntime) {
    throw 'A complete Node.js 22/npm runtime was not found. Run scripts\install_node_runtime.ps1 to install the pinned runtime under .runtime\toolchains.'
}
$npmCacheRoot = Set-AutoPromptNpmCacheEnvironment -ProjectRoot $ProjectRoot
Write-Host "Node runtime: $($nodeRuntime.Version) from $($nodeRuntime.Source); npm cache: $npmCacheRoot" -ForegroundColor DarkGray
$ffmpegRuntime = Resolve-AutoPromptFfmpegRuntime -ProjectRoot $ProjectRoot
if (-not $ffmpegRuntime) {
    throw 'A complete FFmpeg/FFprobe runtime was not found. Run scripts\install_ffmpeg_runtime.ps1 to install the pinned runtime under .runtime\toolchains.'
}
Set-AutoPromptFfmpegEnvironment -Runtime $ffmpegRuntime
Write-Host "FFmpeg runtime: $($ffmpegRuntime.Version) from $($ffmpegRuntime.Source)" -ForegroundColor DarkGray
$env:PYTHONUNBUFFERED = '1'

$ComfyPython = Join-Path $ComfyUIRoot 'python_embeded\python.exe'
$ComfyMain = Join-Path $ComfyUIRoot 'ComfyUI\main.py'
if (-not $SkipComfyUI -and (-not (Test-Path -LiteralPath $ComfyPython) -or -not (Test-Path -LiteralPath $ComfyMain))) {
    throw "ComfyUI portable installation was not found at $ComfyUIRoot. Set AUTOPROMPT_COMFYUI_ROOT, pass -ComfyUIRoot, configure backend\.env COMFYUI_OUTPUT_DIR as <portable-root>\ComfyUI\output, or use -SkipComfyUI."
}

# Refuse an explicit GPU-process restart before stopping any application
# service unless ComfyUI proves that its queue is idle.  This keeps a typo or
# stale operator assumption from interrupting an expensive render and avoids
# leaving Django/Vite down when the safety check fails.
if (-not $SkipComfyUI -and $RestartComfyUI -and @(Get-PortOwners 8188).Count -gt 0) {
    $queueActivity = Get-ComfyUIQueueActivity
    if ($null -eq $queueActivity) {
        if (-not $ForceComfyUIRestart) {
            throw 'Cannot verify the current ComfyUI queue. Refusing to restart the GPU process. After investigating, rerun with -RestartComfyUI -ForceComfyUIRestart to override.'
        }
        Write-Warning 'ComfyUI queue could not be verified; ForceComfyUIRestart permits interrupting the process.'
    } elseif (($queueActivity.running + $queueActivity.pending) -gt 0) {
        if (-not $ForceComfyUIRestart) {
            throw "ComfyUI still has $($queueActivity.running) running and $($queueActivity.pending) pending prompt(s). Wait for them to finish, or explicitly use -RestartComfyUI -ForceComfyUIRestart."
        }
        Write-Warning "ForceComfyUIRestart will interrupt $($queueActivity.running) running and discard $($queueActivity.pending) pending prompt(s)."
    }
}

Write-Step 'Stopping previous AutoPrompt application services'
Stop-RecordedServices
Stop-ProjectPythonProcesses
Stop-PortListener 8000 @('python', 'pythonw') 'Django'
Stop-PortListener 5173 @('node', 'npm', 'cmd') 'Vite'
$PreviousServicesStopped = $true

if (-not $SkipComfyUI -and $RestartComfyUI) {
    Write-Warning 'RestartComfyUI explicitly replaces the current GPU process after the queue safety check.'
    Stop-PortListener 8188 @('python', 'pythonw') 'ComfyUI'
}

if (-not $SkipMigrate) {
    Write-Step 'Applying database migrations'
    Push-Location $BackendRoot
    try {
        & $BackendPython manage.py migrate --noinput
        Assert-LastExitCode 'Database migration'
    } finally {
        Pop-Location
    }
}

if (-not $SkipSeedData) {
    Write-Step 'Synchronizing reviewed styles and workflow templates'
    Push-Location $BackendRoot
    try {
        & $BackendPython manage.py seed_data
        Assert-LastExitCode 'Base data synchronization'
    } finally {
        Pop-Location
    }
}

Write-Step 'Recovering interrupted assembly records'
Push-Location $BackendRoot
try {
    & $BackendPython manage.py recover_interrupted_assemblies --reason 'Local application services restarted'
    Assert-LastExitCode 'Assembly recovery'
} finally {
    Pop-Location
}

Write-Step 'Starting local services'
$comfyAlreadyReady = $false
if (-not $SkipComfyUI) {
    $comfyAlreadyReady = Test-Http 'http://127.0.0.1:8188/system_stats'
    if ($comfyAlreadyReady) {
        Write-Host 'ComfyUI is already healthy; keeping the current GPU process.' -ForegroundColor Green
    } else {
        if ((Get-PortOwners 8188).Count -gt 0) {
            throw 'Port 8188 is occupied but ComfyUI health check failed. Use -RestartComfyUI after confirming the current GPU task can be interrupted.'
        }
        Start-ServiceProcess 'ComfyUI' $ComfyPython @(
            '-s', 'ComfyUI\main.py', '--windows-standalone-build',
            '--listen', '127.0.0.1', '--port', '8188'
        ) $ComfyUIRoot | Out-Null
    }
    # The worker reconciles stale remote jobs as soon as it starts. Never let
    # it perform that audit while a cold ComfyUI process is still booting.
    Wait-Http 'ComfyUI' 'http://127.0.0.1:8188/system_stats' $StartupTimeoutSeconds
}

Start-ServiceProcess 'Django API' $BackendPython @('-u', 'manage.py', 'runserver', '127.0.0.1:8000', '--noreload') $BackendRoot | Out-Null
Wait-Http 'Django API' 'http://127.0.0.1:8000/api/v1/health/' $StartupTimeoutSeconds

# Workers only start after migrations, seed synchronization, assembly recovery,
# ComfyUI readiness and API liveness. Generation is started before assembly so
# the readiness endpoint observes the same deterministic order on every run.
Start-ServiceProcess 'Generation worker' $BackendPython @('-u', 'manage.py', 'run_generation_worker', '--startup-recover-minutes', '0') $BackendRoot | Out-Null
Start-ServiceProcess 'Assembly worker' $BackendPython @('-u', 'manage.py', 'run_assembly_worker') $BackendRoot | Out-Null
Start-ServiceProcess 'Vue frontend' $nodeRuntime.NpmPath @('run', 'dev', '--', '--host', '127.0.0.1', '--port', '5173', '--strictPort') $FrontendRoot | Out-Null

$stateTempPath = "$StatePath.$PID.tmp"
[pscustomobject]@{
    started_at = (Get-Date).ToString('o')
    project_root = $ProjectRoot
    services = @($ServiceRecords)
} | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $stateTempPath -Encoding UTF8
Move-Item -LiteralPath $stateTempPath -Destination $StatePath -Force
$StateWritten = $true

Write-Step 'Waiting for health checks'
Wait-Http 'Vue frontend' 'http://127.0.0.1:5173/' $StartupTimeoutSeconds

$workerRecord = $ServiceRecords | Where-Object { $_.name -eq 'Generation worker' } | Select-Object -First 1
if (-not $workerRecord -or -not (Get-Process -Id $workerRecord.pid -ErrorAction SilentlyContinue)) {
    throw "Generation worker exited during startup. Check $($workerRecord.stderr)."
}
$assemblyWorkerRecord = $ServiceRecords | Where-Object { $_.name -eq 'Assembly worker' } | Select-Object -First 1
if (-not $assemblyWorkerRecord -or -not (Get-Process -Id $assemblyWorkerRecord.pid -ErrorAction SilentlyContinue)) {
    throw "Assembly worker exited during startup. Check $($assemblyWorkerRecord.stderr)."
}

# Liveness proves that Django accepted requests; readiness additionally proves
# database/migrations, storage, ComfyUI and both durable workers are healthy.
Wait-Http 'AutoPrompt readiness' 'http://127.0.0.1:8000/api/v1/health/ready/' $StartupTimeoutSeconds

Write-Host "`nAutoPrompt Studio restarted successfully." -ForegroundColor Green
Write-Host 'Studio:  http://127.0.0.1:5173/'
Write-Host 'API:     http://127.0.0.1:8000/api/docs/'
if (-not $SkipComfyUI) { Write-Host 'ComfyUI: http://127.0.0.1:8188/' }
Write-Host "Logs:    $LogRoot"

if ($OpenBrowser) {
    try {
        Start-Process 'http://127.0.0.1:5173/'
    } catch {
        # Browser availability is not part of service readiness. Do not tear
        # down a healthy stack merely because URL association is unavailable.
        Write-Warning "Services are healthy, but the browser could not be opened automatically: $($_.Exception.Message)"
    }
}
} catch {
    Write-Warning "AutoPrompt restart failed; rolling back only the processes started by this run: $($_.Exception.Message)"
    for ($index = $ServiceRecords.Count - 1; $index -ge 0; $index--) {
        try {
            Stop-ProcessTree ([int]$ServiceRecords[$index].pid) "failed startup $($ServiceRecords[$index].name)"
        } catch {
            Write-Warning "Could not stop $($ServiceRecords[$index].name): $($_.Exception.Message)"
        }
    }
    if (($StateWritten -or $PreviousServicesStopped) -and (Test-Path -LiteralPath $StatePath)) {
        Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
    }
    if ($stateTempPath -and (Test-Path -LiteralPath $stateTempPath)) {
        Remove-Item -LiteralPath $stateTempPath -Force -ErrorAction SilentlyContinue
    }
    throw
} finally {
    if ($RestartMutexAcquired) {
        try { $RestartMutex.ReleaseMutex() } catch { }
    }
    $RestartMutex.Dispose()
}
