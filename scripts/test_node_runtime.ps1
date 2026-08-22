$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'node_runtime.ps1')

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

$projectRoot = Split-Path -Parent $PSScriptRoot
$manifest = Get-AutoPromptNodeRuntimeManifest -ProjectRoot $projectRoot
Assert-True ($manifest.Version -match '^22\.') 'The pinned runtime must remain on Node.js 22.'
Assert-Equal $manifest.ArchiveName ("node-v$($manifest.Version)-win-x64.zip") 'The archive name must match the pinned version.'

$runtime = Resolve-AutoPromptNodeRuntime -ProjectRoot $projectRoot
Assert-True ($null -ne $runtime) 'A complete Node.js/npm runtime must be resolvable.'
Assert-True (Test-Path -LiteralPath $runtime.NodePath -PathType Leaf) 'node.exe must exist.'
Assert-True (Test-Path -LiteralPath $runtime.NpmPath -PathType Leaf) 'npm.cmd must exist.'
Assert-True ($runtime.Version -match '^v22\.') 'The resolved runtime must be Node.js 22.'

$cacheRoot = Set-AutoPromptNpmCacheEnvironment -ProjectRoot $projectRoot
Assert-Equal $env:NPM_CONFIG_CACHE $cacheRoot 'npm cache must be redirected into the E-drive project runtime.'
Assert-True (Test-Path -LiteralPath $cacheRoot -PathType Container) 'npm cache directory must exist.'

Write-Host "Node runtime tests passed with $($runtime.Version) from $($runtime.Source)."
