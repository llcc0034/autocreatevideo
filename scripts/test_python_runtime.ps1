$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'python_runtime.ps1')

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Assert-Equal([object]$Actual, [object]$Expected, [string]$Message) {
    if ($Actual -ne $Expected) {
        throw "$Message Expected=<$Expected> Actual=<$Actual>"
    }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$manifest = Get-AutoPromptPythonRuntimeManifest -ProjectRoot $projectRoot
Assert-True ($manifest.Version -match '^3\.11\.') 'The pinned runtime must remain on Python 3.11.'
Assert-Equal $manifest.PackageName ("python.$($manifest.Version).nupkg") 'The package name must match the pinned version.'
Assert-Equal $manifest.RuntimeSubdirectory 'tools' 'The official package runtime must resolve from its tools directory.'

$runtime = Resolve-AutoPromptPythonRuntime -ProjectRoot $projectRoot
Assert-True ($null -ne $runtime) 'A Python 3.11 runtime must be resolvable.'
Assert-True (Test-AutoPromptPythonExecutable -PythonPath $runtime.PythonPath) 'The resolved Python runtime must execute.'
$cacheRoot = Set-AutoPromptPipCacheEnvironment -ProjectRoot $projectRoot
Assert-Equal $env:PIP_CACHE_DIR $cacheRoot 'pip cache must be redirected into the E-drive project runtime.'
Assert-True (Test-Path -LiteralPath $cacheRoot -PathType Container) 'pip cache directory must exist.'
$venvPython = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
Assert-True (Test-AutoPromptPythonExecutable -PythonPath $venvPython) 'The backend virtual environment must use a working E-drive base interpreter.'
$basePrefix = (& $venvPython -c 'import sys; print(sys.base_prefix)' 2>&1 | Out-String).Trim()
Assert-Equal $basePrefix $runtime.RuntimeRoot 'The backend venv must resolve its base interpreter from the pinned runtime.'

Write-Host "Python runtime tests passed with $($runtime.Version) from $($runtime.Source)."
