$ErrorActionPreference = 'Stop'

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

function Get-RestartFunctionText([string]$Path, [string]$Name) {
    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $Path,
        [ref]$tokens,
        [ref]$parseErrors
    )
    if ($parseErrors.Count -gt 0) {
        throw "Could not parse restart.ps1: $($parseErrors[0].Message)"
    }
    $functionAst = $ast.Find({
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $node.Name -eq $Name
    }, $true)
    if ($null -eq $functionAst) {
        throw "Function $Name was not found in restart.ps1."
    }
    return $functionAst.Extent.Text
}

$restartPath = Join-Path $PSScriptRoot 'restart.ps1'

# Import only the functions under test. Dot-sourcing restart.ps1 would execute
# the restart workflow and is intentionally forbidden in this test.
Invoke-Expression (Get-RestartFunctionText -Path $restartPath -Name 'Stop-ProcessTree')
Invoke-Expression (Get-RestartFunctionText -Path $restartPath -Name 'Stop-RecordedServices')
Invoke-Expression (Get-RestartFunctionText -Path $restartPath -Name 'Assert-JwtSigningKeyConfiguration')
Invoke-Expression (Get-RestartFunctionText -Path $restartPath -Name 'Read-ExecutionCutoverReview')

# Probe tests use a local fake clock and HTTP response, never start a service.
& {
    Invoke-Expression (Get-RestartFunctionText -Path $restartPath -Name 'Test-Http')
    Invoke-Expression (Get-RestartFunctionText -Path $restartPath -Name 'Wait-Http')
    $script:probeClock = [datetime]'2026-01-01T00:00:00'
    $script:probeStatus = 200
    $script:probeTimeouts = [System.Collections.Generic.List[int]]::new()
    function Get-Date { return $script:probeClock }
    function Start-Sleep { param([int]$Seconds) $script:probeClock = $script:probeClock.AddSeconds($Seconds) }
    function Invoke-WebRequest {
        param([string]$Uri, [switch]$UseBasicParsing, [int]$TimeoutSec)
        $script:probeTimeouts.Add($TimeoutSec)
        $script:probeClock = $script:probeClock.AddSeconds([Math]::Min(7, $TimeoutSec))
        if ($TimeoutSec -lt 7) { throw 'fixture HTTP timeout' }
        return [pscustomobject]@{ StatusCode = $script:probeStatus }
    }
    Wait-Http 'slow valid readiness' 'http://fixture.invalid/ready' 20
    Assert-Equal $script:probeTimeouts.Count 1 'A slow healthy probe must not be abandoned and duplicated.'
    Assert-Equal $script:probeTimeouts[0] 20 'The request receives the remaining startup budget.'
    $script:probeTimeouts.Clear()
    $script:probeStatus = 503
    $notReadyError = $null
    try { Wait-Http 'unhealthy readiness' 'http://fixture.invalid/ready' 10 } catch { $notReadyError = $_ }
    Assert-True ($null -ne $notReadyError) 'A real unhealthy response must still fail startup.'
    Assert-Equal $script:probeTimeouts[0] 10 'The first probe receives the deadline budget.'
    Assert-Equal $script:probeTimeouts[1] 2 'A retry only receives the remaining budget, not a fresh timeout.'
    Assert-Equal $script:probeTimeouts.Count 2 'The deadline still bounds failed retries.'
}

# The JWT preflight must provide Django's module selector to raw python, restore
# the caller environment afterwards, and remain before every destructive stop.
$preflightRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('autoprompt-restart-preflight-' + [guid]::NewGuid().ToString('N'))
$hadOriginalSettingsModule = Test-Path Env:DJANGO_SETTINGS_MODULE
$originalSettingsModule = $env:DJANGO_SETTINGS_MODULE
try {
    New-Item -ItemType Directory -Path $preflightRoot -Force | Out-Null
    $fakePython = Join-Path $preflightRoot 'python.cmd'
    Set-Content -LiteralPath $fakePython -Encoding ASCII -Value @(
        '@echo off',
        'if not "%DJANGO_SETTINGS_MODULE%"=="autoprompt_backend.settings" exit /b 17',
        'exit /b 0'
    )
    $env:DJANGO_SETTINGS_MODULE = 'fixture.original.settings'
    Assert-JwtSigningKeyConfiguration `
        -PythonPath $fakePython `
        -WorkingDirectory $preflightRoot
    Assert-Equal $env:DJANGO_SETTINGS_MODULE 'fixture.original.settings' 'JWT preflight must restore the caller settings module.'

    $tokens = $null
    $parseErrors = $null
    $restartAst = [System.Management.Automation.Language.Parser]::ParseFile(
        $restartPath,
        [ref]$tokens,
        [ref]$parseErrors
    )
    $topLevelCommands = @($restartAst.FindAll({
        param($node)
        if (-not ($node -is [System.Management.Automation.Language.CommandAst])) { return $false }
        $parent = $node.Parent
        while ($null -ne $parent) {
            if ($parent -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
                return $false
            }
            $parent = $parent.Parent
        }
        return $true
    }, $true))
    $preflightCall = $topLevelCommands | Where-Object {
        $_.GetCommandName() -eq 'Assert-JwtSigningKeyConfiguration'
    } | Select-Object -First 1
    $stopCall = $topLevelCommands | Where-Object {
        $_.GetCommandName() -eq 'Stop-RecordedServices'
    } | Select-Object -First 1
    Assert-True ($null -ne $preflightCall) 'restart.ps1 must invoke the JWT preflight.'
    Assert-True ($null -ne $stopCall) 'restart.ps1 must invoke recorded-service cleanup.'
    Assert-True ($preflightCall.Extent.StartOffset -lt $stopCall.Extent.StartOffset) 'JWT preflight must run before recorded services are stopped.'
    $cutoverCalls = @($topLevelCommands | Where-Object { $_.GetCommandName() -eq 'Invoke-ExecutionCutover' })
    Assert-Equal $cutoverCalls.Count 2 'Cutover has exactly one readonly preflight and one stopped-writer execution.'
    Assert-True ($cutoverCalls[0].Extent.StartOffset -lt $stopCall.Extent.StartOffset) 'Review must be checked before stopping services.'
    $lastStop = $topLevelCommands | Where-Object { $_.GetCommandName() -eq 'Stop-PortListener' } |
        Sort-Object { $_.Extent.StartOffset } | Select-Object -Last 1
    Assert-True ($cutoverCalls[1].Extent.StartOffset -gt $lastStop.Extent.StartOffset) 'Execute cleanup only after all platform stop operations.'
    $migrationCall = $topLevelCommands | Where-Object { $_.Extent.Text -match 'manage.py migrate --noinput' } | Select-Object -First 1
    Assert-True ($cutoverCalls[1].Extent.StartOffset -lt $migrationCall.Extent.StartOffset) 'Reviewed cleanup precedes schema migration.'
    Assert-True ($cutoverCalls[0].Extent.Text -notmatch '-Execute') 'Preflight is read only.'
    Assert-True ($cutoverCalls[1].Extent.Text -match '-Execute') 'Stopped-writer phase explicitly executes reviewed cleanup.'

    $reviewPath = Join-Path $preflightRoot 'cutover-review.json'
    $reviewFixture = @{
        plan_sha256 = ('a' * 64)
        obsolete_prompt_version_ids = @('a4107821-f3ef-4f2f-9841-36a20c5db239')
        operator = 'isolated-cutover'
        confirmation = 'DELETE_REVIEWED_0144_EXECUTION_DATA_WITHOUT_BACKUP'
    }
    $reviewFixture | ConvertTo-Json | Set-Content -LiteralPath $reviewPath -Encoding UTF8
    $parsedReview = Read-ExecutionCutoverReview -Path $reviewPath
    Assert-Equal $parsedReview.plan_sha256 ('a' * 64) 'Review preserves the exact approved hash.'
    foreach ($invalidField in @('plan_sha256', 'confirmation', 'obsolete_prompt_version_ids')) {
        $invalidReview = $reviewFixture.Clone()
        $invalidReview[$invalidField] = 'invalid'
        $invalidReview | ConvertTo-Json | Set-Content -LiteralPath $reviewPath -Encoding UTF8
        $rejected = $false
        try { Read-ExecutionCutoverReview -Path $reviewPath | Out-Null } catch { $rejected = $true }
        Assert-True $rejected "Invalid $invalidField must be rejected without stopping services."
    }
    $reviewFixture.extra_command = 'must never become executable input'
    $reviewFixture | ConvertTo-Json | Set-Content -LiteralPath $reviewPath -Encoding UTF8
    $rejected = $false
    try { Read-ExecutionCutoverReview -Path $reviewPath | Out-Null } catch { $rejected = $true }
    Assert-True $rejected 'Arbitrary command hooks are not supported by the cutover review.'
} finally {
    if ($hadOriginalSettingsModule) {
        $env:DJANGO_SETTINGS_MODULE = $originalSettingsModule
    } else {
        Remove-Item Env:DJANGO_SETTINGS_MODULE -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $preflightRoot) {
        Remove-Item -LiteralPath $preflightRoot -Recurse -Force
    }
}

# A target can exit after the initial lookup and before Stop-Process reaches it.
# That state is already stopped and must not fail the cleanup workflow.
$script:getProcessCallCount = 0
function Get-Process {
    [CmdletBinding()]
    param([int]$Id)
    $script:getProcessCallCount++
    if ($script:getProcessCallCount -eq 1) {
        return [pscustomobject]@{ Id = $Id; ProcessName = 'node' }
    }
    return $null
}
function Get-DescendantProcessIds { param([int]$ProcessId) return @() }
function Stop-Process {
    [CmdletBinding()]
    param([int]$Id, [switch]$Force)
    throw "Cannot find a process with the process identifier $Id."
}
function Start-Sleep { [CmdletBinding()] param([int]$Milliseconds) }

Stop-ProcessTree -ProcessId 41001 -Reason 'race fixture'
Assert-Equal $script:getProcessCallCount 2 'The root PID should be checked after Stop-Process reports an error.'

# Errors are not swallowed while the same PID is still present.
$script:getProcessCallCount = 0
function Get-Process {
    [CmdletBinding()]
    param([int]$Id)
    $script:getProcessCallCount++
    return [pscustomobject]@{ Id = $Id; ProcessName = 'node' }
}
$stopError = $null
try {
    Stop-ProcessTree -ProcessId 41002 -Reason 'persistent error fixture'
} catch {
    $stopError = $_
}
Assert-True ($null -ne $stopError) 'Stop-ProcessTree must preserve an error while the target PID remains alive.'

# One recorded service failure must not skip later registered services.
$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('autoprompt-restart-processes-' + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $testRoot -Force | Out-Null
    $script:fixtureStartTime = (Get-Date).AddMinutes(-1)
    $StatePath = Join-Path $testRoot 'services.json'
    [pscustomobject]@{
        services = @(
            [pscustomobject]@{
                name = 'Django API'
                pid = 42001
                process_start_time = $script:fixtureStartTime.ToString('o')
            },
            [pscustomobject]@{
                name = 'Vue frontend'
                pid = 42002
                process_start_time = $script:fixtureStartTime.ToString('o')
            }
        )
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $StatePath -Encoding UTF8

    $RestartComfyUI = $false
    $KnownProjectProcessIds = [System.Collections.Generic.HashSet[int]]::new()
    $script:stopTreeCalls = [System.Collections.Generic.List[int]]::new()
    function Get-Process {
        [CmdletBinding()]
        param([int]$Id)
        return [pscustomobject]@{
            Id = $Id
            ProcessName = 'fixture'
            StartTime = $script:fixtureStartTime
        }
    }
    function Get-DescendantProcessIds { param([int]$ProcessId) return @() }
    function Stop-ProcessTree {
        param([int]$ProcessId, [string]$Reason)
        $script:stopTreeCalls.Add($ProcessId) | Out-Null
        if ($ProcessId -eq 42001) {
            throw 'fixture stop failure'
        }
    }

    $recordedStopError = $null
    try {
        Stop-RecordedServices
    } catch {
        $recordedStopError = $_
    }
    Assert-Equal $script:stopTreeCalls.Count 2 'Every registered service should receive an independent cleanup attempt.'
    Assert-Equal $script:stopTreeCalls[0] 42001 'The first registered service should be attempted.'
    Assert-Equal $script:stopTreeCalls[1] 42002 'Cleanup should continue with the next registered service.'
    Assert-True $KnownProjectProcessIds.Contains(42001) 'The first verified registered PID should remain known for the port safety check.'
    Assert-True $KnownProjectProcessIds.Contains(42002) 'The later verified registered PID should remain known for the port safety check.'
    Assert-True ($null -ne $recordedStopError) 'A recorded service that remains alive must abort the restart after all cleanup attempts.'
    Assert-True ($recordedStopError.Exception.Message -like '*refusing to start duplicate services*') 'The abort should explain the duplicate-service safety boundary.'
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}

Write-Host 'Restart process cleanup tests passed.'
