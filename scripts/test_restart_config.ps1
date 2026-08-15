$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'restart_config.ps1')

function Assert-Equal([object]$Actual, [object]$Expected, [string]$Message) {
    if ($Actual -ne $Expected) {
        throw "$Message Expected=<$Expected> Actual=<$Actual>"
    }
}

function Assert-Null([object]$Actual, [string]$Message) {
    if ($null -ne $Actual) {
        throw "$Message Expected=<null> Actual=<$Actual>"
    }
}

$testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('autoprompt-restart-config-' + [guid]::NewGuid().ToString('N'))
try {
    $portableRoot = Join-Path $testRoot 'ComfyUI_windows_portable'
    $output = Join-Path $portableRoot 'ComfyUI\output'
    New-Item -ItemType Directory -Path $output -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $portableRoot 'python_embeded') -Force | Out-Null
    New-Item -ItemType File -Path (Join-Path $portableRoot 'python_embeded\python.exe') -Force | Out-Null
    New-Item -ItemType File -Path (Join-Path $portableRoot 'ComfyUI\main.py') -Force | Out-Null

    $backendEnv = Join-Path $testRoot 'backend.env'
    $rootEnv = Join-Path $testRoot 'root.env'
    Set-Content -LiteralPath $backendEnv -Encoding UTF8 -Value @(
        '# restart configuration fixture',
        ('COMFYUI_OUTPUT_DIR="' + $output.Replace('\', '/') + '"')
    )
    Set-Content -LiteralPath $rootEnv -Encoding UTF8 -Value 'COMFYUI_OUTPUT_HOST_DIR=C:/not/a/portable/output'

    $resolved = Resolve-AutoPromptComfyUIRoot `
        -ExplicitRoot '' `
        -ProcessOutputDirectory '' `
        -BackendEnvPath $backendEnv `
        -ProcessOutputHostDirectory '' `
        -RootEnvPath $rootEnv
    Assert-Equal $resolved.Root ([System.IO.Path]::GetFullPath($portableRoot)) 'backend/.env output directory should resolve the portable root.'
    Assert-Equal $resolved.Source 'backend/.env COMFYUI_OUTPUT_DIR' 'resolution should report its non-secret configuration source.'

    $explicit = Resolve-AutoPromptComfyUIRoot `
        -ExplicitRoot 'D:\explicit-comfy' `
        -ProcessOutputDirectory $output `
        -BackendEnvPath $backendEnv `
        -ProcessOutputHostDirectory '' `
        -RootEnvPath $rootEnv
    Assert-Equal $explicit.Root 'D:\explicit-comfy' 'an explicit root should take precedence.'
    Assert-Equal $explicit.Source 'explicit' 'an explicit root should retain its source.'

    Assert-Null (Resolve-AutoPromptComfyUIRootFromOutputDirectory -OutputDirectory (Join-Path $portableRoot 'output')) 'the output directory must be nested directly below ComfyUI.'
    Assert-Null (Resolve-AutoPromptComfyUIRootFromOutputDirectory -OutputDirectory 'relative\ComfyUI\output') 'relative output directories must not be inferred.'

    $missingMarkers = Join-Path $testRoot 'missing-markers\ComfyUI\output'
    New-Item -ItemType Directory -Path $missingMarkers -Force | Out-Null
    Assert-Null (Resolve-AutoPromptComfyUIRootFromOutputDirectory -OutputDirectory $missingMarkers) 'portable runtime markers are required.'

    Write-Host 'Restart configuration tests passed.'
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}
