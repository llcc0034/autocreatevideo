$ErrorActionPreference = 'Stop'

$SetupPath = Join-Path $PSScriptRoot 'setup.ps1'
$Tokens = $null
$ParseErrors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $SetupPath,
    [ref]$Tokens,
    [ref]$ParseErrors
)

if ($ParseErrors.Count -gt 0) {
    throw "setup.ps1 has parse errors: $($ParseErrors[0].Message)"
}

function Test-HasIfAncestor([System.Management.Automation.Language.Ast]$Node) {
    for ($Parent = $Node.Parent; $null -ne $Parent; $Parent = $Parent.Parent) {
        if ($Parent -is [System.Management.Automation.Language.IfStatementAst]) {
            return $true
        }
    }
    return $false
}

$Assignments = $Ast.FindAll({
    param($Node)
    $Node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $Node.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
    $Node.Left.VariablePath.UserPath -eq 'SeedArguments'
}, $true)

$SeedDefinition = $Assignments | Where-Object {
    $_.Operator -eq [System.Management.Automation.Language.TokenKind]::Equals -and
    $_.Right.Extent.Text -match '[''"]manage\.py[''"]' -and
    $_.Right.Extent.Text -match '[''"]seed_data[''"]'
} | Select-Object -First 1
if ($null -eq $SeedDefinition -or (Test-HasIfAncestor $SeedDefinition)) {
    throw 'setup.ps1 must define the system-style seed command outside the demo-only branch.'
}

$DemoFlag = $Assignments | Where-Object {
    $_.Operator -eq [System.Management.Automation.Language.TokenKind]::PlusEquals -and
    $_.Right.Extent.Text -match '[''"]--with-demo-project[''"]'
} | Select-Object -First 1
if ($null -eq $DemoFlag -or -not (Test-HasIfAncestor $DemoFlag)) {
    throw 'setup.ps1 must add --with-demo-project only inside the explicit demo branch.'
}

$Commands = $Ast.FindAll({
    param($Node)
    $Node -is [System.Management.Automation.Language.CommandAst]
}, $true)
$SeedInvocation = $Commands | Where-Object {
    $_.Extent.Text -match '^&\s+\$PythonPath\s+@SeedArguments\s*$'
} | Select-Object -First 1
if ($null -eq $SeedInvocation -or (Test-HasIfAncestor $SeedInvocation)) {
    throw 'setup.ps1 must execute system-style seeding for both normal and demo installations.'
}

$AssetImport = $Commands | Where-Object {
    $_.Extent.Text -match '\bmanage\.py\s+import_asset_library\b'
} | Select-Object -First 1
if ($null -eq $AssetImport -or $SeedInvocation.Extent.StartOffset -ge $AssetImport.Extent.StartOffset) {
    throw 'setup.ps1 must seed system styles before importing the asset library.'
}

Write-Host 'Setup bootstrap ordering tests passed.' -ForegroundColor Green
