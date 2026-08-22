function Get-AutoPromptNodeRuntimeManifest {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $manifestPath = Join-Path $ProjectRoot 'config\node-runtime.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Node runtime manifest is missing: $manifestPath"
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Node runtime manifest is invalid JSON: $manifestPath"
    }
    if ($manifest.schema_version -ne 1) {
        throw "Unsupported Node runtime manifest schema_version: $($manifest.schema_version)"
    }
    $version = [string]$manifest.version
    $platform = [string]$manifest.platform
    $archiveName = [string]$manifest.archive_name
    $downloadUrl = [string]$manifest.download_url
    $sha256 = ([string]$manifest.sha256).ToLowerInvariant()
    if ($version -notmatch '^22\.\d+\.\d+$') {
        throw "Node runtime version must be a pinned Node.js 22 release: $version"
    }
    if ($platform -ne 'win-x64') {
        throw "Node runtime platform must be win-x64: $platform"
    }
    $expectedArchiveName = "node-v$version-$platform.zip"
    if ($archiveName -ne $expectedArchiveName) {
        throw "Node runtime archive_name must be $expectedArchiveName."
    }
    $expectedDownloadUrl = "https://nodejs.org/download/release/v$version/$archiveName"
    if ($downloadUrl -ne $expectedDownloadUrl) {
        throw "Node runtime download_url must use the official pinned Node.js release URL."
    }
    if ($sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Node runtime sha256 must contain exactly 64 lowercase hexadecimal characters.'
    }
    return [pscustomobject]@{
        Version = $version
        Platform = $platform
        ArchiveName = $archiveName
        DownloadUrl = $downloadUrl
        Sha256 = $sha256
        ManifestPath = $manifestPath
        DirectoryName = "node-v$version-$platform"
    }
}

function Get-AutoPromptNodeVersion {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$NodePath)

    $output = (& $NodePath --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $output -notmatch '^v(\d+)\.(\d+)\.(\d+)$') {
        throw "Could not read a valid Node.js version from $NodePath. Output: $output"
    }
    return [pscustomobject]@{
        Text = $output
        Major = [int]$Matches[1]
        Minor = [int]$Matches[2]
        Patch = [int]$Matches[3]
    }
}

function Resolve-AutoPromptNodeRuntime {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $manifest = Get-AutoPromptNodeRuntimeManifest -ProjectRoot $ProjectRoot
    $localRoot = Join-Path $ProjectRoot ('.runtime\toolchains\' + $manifest.DirectoryName)
    $localNode = Join-Path $localRoot 'node.exe'
    $localNpm = Join-Path $localRoot 'npm.cmd'
    $hasLocalRoot = Test-Path -LiteralPath $localRoot
    $hasLocalNode = Test-Path -LiteralPath $localNode -PathType Leaf
    $hasLocalNpm = Test-Path -LiteralPath $localNpm -PathType Leaf
    if ($hasLocalRoot -or $hasLocalNode -or $hasLocalNpm) {
        if (-not $hasLocalNode -or -not $hasLocalNpm) {
            throw "Project-local Node runtime is incomplete: $localRoot. Re-run scripts\install_node_runtime.ps1."
        }
        $version = Get-AutoPromptNodeVersion -NodePath $localNode
        if ($version.Text -ne ('v' + $manifest.Version)) {
            throw "Project-local Node runtime version mismatch. Expected v$($manifest.Version), found $($version.Text)."
        }
        return [pscustomobject]@{
            NodePath = $localNode
            NpmPath = $localNpm
            Version = $version.Text
            Source = 'project-local'
            RuntimeRoot = $localRoot
        }
    }

    $npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npmCommand) {
        return $null
    }
    $systemNode = Join-Path (Split-Path -Parent $npmCommand.Source) 'node.exe'
    if (-not (Test-Path -LiteralPath $systemNode -PathType Leaf)) {
        $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
        if (-not $nodeCommand) {
            throw 'npm.cmd is available, but its Node.js executable cannot be resolved.'
        }
        $systemNode = $nodeCommand.Source
    }
    $version = Get-AutoPromptNodeVersion -NodePath $systemNode
    if ($version.Major -ne 22) {
        throw "AutoPrompt requires Node.js 22. Found $($version.Text) at $systemNode."
    }
    return [pscustomobject]@{
        NodePath = $systemNode
        NpmPath = $npmCommand.Source
        Version = $version.Text
        Source = 'system-path'
        RuntimeRoot = Split-Path -Parent $systemNode
    }
}

function Set-AutoPromptNpmCacheEnvironment {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $cacheRoot = Join-Path $ProjectRoot '.runtime\npm-cache'
    New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
    [Environment]::SetEnvironmentVariable('NPM_CONFIG_CACHE', $cacheRoot, 'Process')
    return $cacheRoot
}
