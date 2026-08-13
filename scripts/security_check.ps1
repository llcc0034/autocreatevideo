$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $ProjectRoot 'backend\.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $PythonPath)) {
    throw 'Run scripts\setup.ps1 first.'
}

& $PythonPath -m pip_audit -r (Join-Path $ProjectRoot 'backend\requirements.txt')
if ($LASTEXITCODE -ne 0) {
    throw "Python dependency audit failed with exit code $LASTEXITCODE."
}
Push-Location (Join-Path $ProjectRoot 'frontend')
try {
    npm audit --omit=dev --audit-level=high
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend dependency audit failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

Write-Host 'Dependency security checks passed.'
