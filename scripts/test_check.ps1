$ErrorActionPreference = 'Stop'
$CheckPath = Join-Path $PSScriptRoot 'check.ps1'
$Tokens = $null
$ParseErrors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile($CheckPath, [ref]$Tokens, [ref]$ParseErrors)
if ($ParseErrors.Count -gt 0) { throw $ParseErrors[0].Message }

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

# Invalid or empty selections must stop before runtime loading or test discovery.
foreach ($InvalidLabels in @(@(), @(''), @('studio'), @('other.tests'), @('--keepdb'),
        @('studio.tests;Write-Output unsafe'), @('studio.tests', '../tests'))) {
    $Rejected = $false
    try { & $CheckPath -BackendTestLabels $InvalidLabels }
    catch { $Rejected = $_.Exception.Message -like 'BackendTestLabels *' }
    Assert-True $Rejected 'An invalid selection must not fall through to full checks.'
}

# Load only the real helper definitions; never launch a database in this guard.
$Functions = @($Ast.FindAll({ param($Node)
    $Node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $true))
foreach ($Name in @('Assert-NativeSuccess', 'Get-AutoPromptTestDatabase', 'Invoke-AutoPromptBackendTests')) {
    $Definition = @($Functions | Where-Object Name -eq $Name)
    Assert-True ($Definition.Count -eq 1) "Expected one $Name implementation."
    . ([scriptblock]::Create($Definition[0].Extent.Text))
}
$BackendRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'backend'
$InitialLocation = (Get-Location).Path
$script:ProbeStatus = 0
$script:ProbeEvidence = '{"vendor":"sqlite","row_locks":false}'
$script:TestCalls = 0
$script:ProbeCalls = 0
$PythonPath = {
    Assert-True ((Get-Location).Path -eq $BackendRoot) 'Django must run in backend.'
    Assert-True ($env:PYTHONIOENCODING -eq 'utf-8') 'Django output must preserve UTF-8.'
    Assert-True ($env:DJANGO_SECRET_KEY -like 'qa-only-*') 'Use only the test signing key.'
    Assert-True ($env:AUTOPROMPT_JWT_SIGNING_KEY -like 'qa-jwt-only-*') 'Use only the test JWT key.'
    if ($args[0] -eq '-c') {
        $script:ProbeCalls += 1
        Assert-True ($args[1] -match 'from django.db import connection') 'Inspect resolved Django configuration.'
        $global:LASTEXITCODE = $script:ProbeStatus
        return $script:ProbeEvidence
    }
    $script:TestCalls += 1
    $script:ObservedArguments = @($args)
    $global:LASTEXITCODE = $script:NativeStatus
}
$SavedSecret = $env:DJANGO_SECRET_KEY
$SavedJwt = $env:AUTOPROMPT_JWT_SIGNING_KEY
$SavedEncoding = $env:PYTHONIOENCODING
try {
    foreach ($Original in @($null, 'test-original-value')) {
        foreach ($Status in @(0, 7)) {
            $env:DJANGO_SECRET_KEY = $Original
            $env:AUTOPROMPT_JWT_SIGNING_KEY = $Original
            $env:PYTHONIOENCODING = $Original
            $script:NativeStatus = $Status
            $Failed = $false
            try { Invoke-AutoPromptBackendTests -Labels @('studio.tests.test_one', 'studio.tests.test_two.Example.test_case') }
            catch {
                if ($_.Exception.Message -ne 'Django test suite failed with exit code 7.') { throw }
                $Failed = $true
            }
            Assert-True ($Failed -eq ($Status -ne 0)) 'Native failures must propagate.'
            Assert-True (($script:ObservedArguments -join '|') -eq 'manage.py|test|studio.tests.test_one|studio.tests.test_two.Example.test_case|--noinput|--verbosity|2') 'Pass labels as separate arguments without command interpolation.'
            Assert-True ($env:DJANGO_SECRET_KEY -eq $Original) 'Restore the previous signing key.'
            Assert-True ($env:AUTOPROMPT_JWT_SIGNING_KEY -eq $Original) 'Restore the previous JWT key.'
            Assert-True ($env:PYTHONIOENCODING -eq $Original) 'Restore the previous output encoding.'
            Assert-True ((Get-Location).Path -eq $InitialLocation) 'Restore cwd on success and failure.'
        }
    }
    $script:NativeStatus = 0
    Invoke-AutoPromptBackendTests
    Assert-True (($script:ObservedArguments -join '|') -eq 'manage.py|test|--noinput|--verbosity|2') 'No labels must preserve full Django discovery.'

    # Strict target acceptance must fail before Django can create/drop a test DB.
    foreach ($Evidence in @(
        '{"vendor":"sqlite","row_locks":false}',
        '{"vendor":"mysql","row_locks":true}',
        '{"vendor":"postgresql","row_locks":false}'
    )) {
        $script:ProbeEvidence = $Evidence
        $Before = $script:TestCalls
        $Rejected = $false
        try { Invoke-AutoPromptBackendTests -RequirePostgreSql }
        catch { $Rejected = $_.Exception.Message -like 'PostgreSQL with row locks is required;*' }
        Assert-True ($Rejected -and $script:TestCalls -eq $Before) 'Strict target mismatch must not run tests.'
        Assert-True ((Get-Location).Path -eq $InitialLocation) 'Restore cwd after database rejection.'
        Assert-True ($env:DJANGO_SECRET_KEY -eq $Original -and $env:AUTOPROMPT_JWT_SIGNING_KEY -eq $Original -and
            $env:PYTHONIOENCODING -eq $Original) 'Restore all temporary environment values after rejection.'
    }
    $script:ProbeEvidence = '{"vendor":"postgresql","row_locks":true}'
    $Before = $script:TestCalls
    Invoke-AutoPromptBackendTests -Labels @('studio.tests.test_one') -RequirePostgreSql
    Assert-True ($script:TestCalls -eq ($Before + 1)) 'Valid PostgreSQL evidence must run the selected tests once.'

    # Missing, malformed, or untrusted evidence must not silently become SQLite
    # or be echoed into a log (it could contain a connection credential).
    foreach ($Evidence in @('', 'connection-password-sentinel', '{}', 'null',
        '{"vendor":"postgresql","row_locks":"true"}',
        '[{"vendor":"postgresql","row_locks":true}]',
        '{"vendor":"connection-password-sentinel","row_locks":true}')) {
        $script:ProbeEvidence = $Evidence
        $Before = $script:TestCalls
        $Rejected = $false
        try { Invoke-AutoPromptBackendTests }
        catch { $Rejected = $_.Exception.Message -eq 'Django test database configuration probe returned invalid evidence.' }
        Assert-True ($Rejected -and $script:TestCalls -eq $Before) 'Invalid evidence must fail closed without exposing raw output.'
    }
    $script:ProbeStatus = 2
    $Before = $script:TestCalls
    $Rejected = $false
    try { Invoke-AutoPromptBackendTests }
    catch { $Rejected = $_.Exception.Message -eq 'Django test database configuration probe failed with exit code 2.' }
    Assert-True ($Rejected -and $script:TestCalls -eq $Before) 'Probe failures must stop before test discovery.'
    Assert-True ($script:ProbeCalls -eq 17) 'Every test invocation must check its actual backend once.'
} finally {
    $env:DJANGO_SECRET_KEY = $SavedSecret
    $env:AUTOPROMPT_JWT_SIGNING_KEY = $SavedJwt
    $env:PYTHONIOENCODING = $SavedEncoding
}

$Commands = @($Ast.FindAll({ param($Node)
    $Node -is [System.Management.Automation.Language.CommandAst]
}, $true))
$Runtime = @($Commands | Where-Object { $_.GetCommandName() -eq 'Set-AutoPromptFfmpegEnvironment' })
$Invocations = @($Commands | Where-Object { $_.GetCommandName() -eq 'Invoke-AutoPromptBackendTests' })
Assert-True ($Runtime.Count -eq 1 -and $Invocations.Count -eq 2) 'Full and selected suites must share one media runtime and test runner.'
foreach ($Invocation in $Invocations) {
    Assert-True ($Runtime[0].Extent.StartOffset -lt $Invocation.Extent.StartOffset) 'Initialize media runtime before either test path.'
    Assert-True ($Invocation.Extent.Text -match '-RequirePostgreSql:\$RequirePostgreSql') 'Forward the target requirement in full and selected modes.'
}
$SelectedBranch = @($Ast.FindAll({ param($Node)
    $Node -is [System.Management.Automation.Language.IfStatementAst] -and
    $Node.Extent.Text -match 'Selected backend tests passed'
}, $true))
Assert-True ($SelectedBranch.Count -eq 1 -and $SelectedBranch[0].Extent.Text -match 'Full project checks were not run\.' -and
    $SelectedBranch[0].Extent.Text -match '(?m)^\s*return\s*$') 'Selected checks must explicitly report partial coverage and return.'
$NodeRuntime = @($Commands | Where-Object { $_.GetCommandName() -eq 'Resolve-AutoPromptNodeRuntime' })
Assert-True ($NodeRuntime.Count -eq 1 -and $SelectedBranch[0].Extent.EndOffset -lt $NodeRuntime[0].Extent.StartOffset) 'Backend-only checks must not depend on Node or frontend work.'
Write-Host 'Check scope and backend runtime tests passed.' -ForegroundColor Green
