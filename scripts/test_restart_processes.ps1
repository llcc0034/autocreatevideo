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
