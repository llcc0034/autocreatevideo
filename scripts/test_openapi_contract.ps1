[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$scriptFiles = @(
    (Join-Path $PSScriptRoot 'openapi_contract.ps1'),
    (Join-Path $PSScriptRoot 'check.ps1'),
    $MyInvocation.MyCommand.Path
)
foreach ($scriptFile in $scriptFiles) {
    $tokens = $null
    $parseErrors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $scriptFile,
        [ref]$tokens,
        [ref]$parseErrors
    )
    if ($parseErrors.Count -gt 0) {
        $messages = ($parseErrors | ForEach-Object { $_.Message }) -join '; '
        throw "PowerShell AST parse failed for $scriptFile`: $messages"
    }
}

$checkSource = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot 'check.ps1'))
if ($checkSource -notmatch 'spectacular[^\r\n]*--file\s+\$GeneratedOpenApiPath\b') {
    throw 'check.ps1 does not generate the dynamic schema into its temporary path.'
}
if ($checkSource -notmatch 'Assert-AutoPromptOpenApiContract') {
    throw 'check.ps1 does not compare the generated schema with the root contract.'
}
if ($checkSource -notmatch 'Assert-AutoPromptOpenApiAuthority') {
    throw 'check.ps1 does not enforce the single root OpenAPI authority.'
}
if (
    $checkSource -match 'spectacular[^\r\n]*--file\s+\$OpenApiPath\b' -or
    $checkSource -match 'spectacular[^\r\n]*--file\s+openapi\.yaml\b'
) {
    throw 'check.ps1 must not overwrite the authoritative root OpenAPI contract.'
}

. (Join-Path $PSScriptRoot 'openapi_contract.ps1')

$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$tempRoot = [System.IO.Path]::GetFullPath((
    Join-Path $tempBase ('autoprompt-openapi-contract-test-' + [guid]::NewGuid().ToString('N'))
))
if (
    -not $tempRoot.StartsWith($tempBase, [System.StringComparison]::OrdinalIgnoreCase) -or
    (Split-Path -Leaf $tempRoot) -notlike 'autoprompt-openapi-contract-test-*'
) {
    throw 'Refusing to create an unsafe OpenAPI contract test directory.'
}

$committedPath = Join-Path $tempRoot 'openapi.yaml'
$generatedPath = Join-Path $tempRoot 'generated.yaml'
$legacyDirectory = Join-Path $tempRoot 'backend'
$legacyPath = Join-Path $legacyDirectory 'openapi.yaml'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$utf8Bom = New-Object System.Text.UTF8Encoding($true)

try {
    New-Item -ItemType Directory -Path $legacyDirectory -Force | Out-Null
    $fixture = "openapi: 3.0.3`ninfo:`n  title: Fixture`n"
    [System.IO.File]::WriteAllText($committedPath, $fixture, $utf8NoBom)
    [System.IO.File]::WriteAllText(
        $generatedPath,
        $fixture.Replace("`n", "`r`n"),
        $utf8Bom
    )

    Assert-AutoPromptOpenApiAuthority `
        -RootContractPath $committedPath `
        -LegacyContractPath $legacyPath
    Assert-AutoPromptOpenApiContract `
        -CommittedPath $committedPath `
        -GeneratedPath $generatedPath

    $committedHashBefore = (Get-FileHash -LiteralPath $committedPath -Algorithm SHA256).Hash
    [System.IO.File]::WriteAllText(
        $generatedPath,
        "openapi: 3.1.0`ninfo:`n  title: Fixture`n",
        $utf8NoBom
    )
    $driftRejected = $false
    try {
        Assert-AutoPromptOpenApiContract `
            -CommittedPath $committedPath `
            -GeneratedPath $generatedPath
    } catch {
        if ($_.Exception.Message -notmatch 'OpenAPI contract drift detected at normalized line 1') {
            throw
        }
        if ($_.Exception.Message -notmatch 'root openapi.yaml was not modified') {
            throw 'OpenAPI drift failure did not state that the root contract is preserved.'
        }
        $driftRejected = $true
    }
    if (-not $driftRejected) {
        throw 'A changed generated OpenAPI contract was not rejected.'
    }

    [System.IO.File]::WriteAllText(
        $generatedPath,
        "openapi: 3.0.3`ninfo:`n  title: Changed`n",
        $utf8NoBom
    )
    $laterLineDriftRejected = $false
    try {
        Assert-AutoPromptOpenApiContract `
            -CommittedPath $committedPath `
            -GeneratedPath $generatedPath
    } catch {
        if ($_.Exception.Message -notmatch 'OpenAPI contract drift detected at normalized line 3') {
            throw
        }
        $laterLineDriftRejected = $true
    }
    if (-not $laterLineDriftRejected) {
        throw 'A later-line OpenAPI drift was not reported at its actual line.'
    }

    $committedHashAfter = (Get-FileHash -LiteralPath $committedPath -Algorithm SHA256).Hash
    if ($committedHashAfter -ne $committedHashBefore) {
        throw 'OpenAPI drift validation modified the committed contract.'
    }

    [System.IO.File]::WriteAllBytes($generatedPath, [byte[]](0x6F, 0x70, 0x65, 0x6E, 0xFF))
    $invalidUtf8Rejected = $false
    try {
        Assert-AutoPromptOpenApiContract `
            -CommittedPath $committedPath `
            -GeneratedPath $generatedPath
    } catch {
        if ($_.Exception.Message -notmatch 'must be valid UTF-8') {
            throw
        }
        $invalidUtf8Rejected = $true
    }
    if (-not $invalidUtf8Rejected) {
        throw 'Invalid UTF-8 OpenAPI content was not rejected.'
    }

    [System.IO.File]::WriteAllText($legacyPath, $fixture, $utf8NoBom)
    $legacyRejected = $false
    try {
        Assert-AutoPromptOpenApiAuthority `
            -RootContractPath $committedPath `
            -LegacyContractPath $legacyPath
    } catch {
        if ($_.Exception.Message -notmatch 'Legacy OpenAPI contract must not exist') {
            throw
        }
        $legacyRejected = $true
    }
    if (-not $legacyRejected) {
        throw 'The legacy backend/openapi.yaml authority was not rejected.'
    }

    Write-Host 'OpenAPI contract drift tests passed.' -ForegroundColor Green
} finally {
    foreach ($file in @($legacyPath, $generatedPath, $committedPath)) {
        if (Test-Path -LiteralPath $file -PathType Leaf) {
            Remove-Item -LiteralPath $file -Force
        }
    }
    if (Test-Path -LiteralPath $legacyDirectory -PathType Container) {
        Remove-Item -LiteralPath $legacyDirectory -Force
    }
    if (Test-Path -LiteralPath $tempRoot -PathType Container) {
        Remove-Item -LiteralPath $tempRoot -Force
    }
}
