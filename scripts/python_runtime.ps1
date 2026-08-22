function Get-AutoPromptPythonRuntimeManifest {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $manifestPath = Join-Path $ProjectRoot 'config\python-runtime.json'
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Python runtime manifest is missing: $manifestPath"
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        throw "Python runtime manifest is invalid JSON: $manifestPath"
    }
    if ($manifest.schema_version -ne 1) {
        throw "Unsupported Python runtime manifest schema_version: $($manifest.schema_version)"
    }
    $version = [string]$manifest.version
    $platform = [string]$manifest.platform
    $packageId = [string]$manifest.package_id
    $packageName = [string]$manifest.package_name
    $downloadUrl = [string]$manifest.download_url
    $sha256 = ([string]$manifest.sha256).ToLowerInvariant()
    $runtimeSubdirectory = [string]$manifest.runtime_subdirectory
    $nugetVersion = [string]$manifest.nuget_version
    $nugetName = [string]$manifest.nuget_name
    $nugetDownloadUrl = [string]$manifest.nuget_download_url
    $nugetSha256 = ([string]$manifest.nuget_sha256).ToLowerInvariant()
    if ($version -notmatch '^3\.11\.\d+$') {
        throw "Python runtime version must be a pinned Python 3.11 release: $version"
    }
    if ($platform -ne 'amd64') {
        throw "Python runtime platform must be amd64: $platform"
    }
    if ($packageId -ne 'python') {
        throw 'Python runtime package_id must be the official Python NuGet package.'
    }
    $expectedPackageName = "python.$version.nupkg"
    if ($packageName -ne $expectedPackageName) {
        throw "Python runtime package_name must be $expectedPackageName."
    }
    $expectedDownloadUrl = "https://api.nuget.org/v3-flatcontainer/python/$version/$expectedPackageName"
    if ($downloadUrl -ne $expectedDownloadUrl) {
        throw 'Python runtime download_url must use the official pinned NuGet.org package URL.'
    }
    if ($sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Python runtime sha256 must contain exactly 64 lowercase hexadecimal characters.'
    }
    if ($runtimeSubdirectory -ne 'tools') {
        throw 'Python runtime runtime_subdirectory must be tools.'
    }
    if ($nugetVersion -notmatch '^6\.\d+\.\d+$') {
        throw "NuGet verifier version is invalid: $nugetVersion"
    }
    if ($nugetName -ne "nuget-$nugetVersion.exe") {
        throw "NuGet verifier name must be nuget-$nugetVersion.exe."
    }
    if ($nugetDownloadUrl -ne "https://dist.nuget.org/win-x86-commandline/v$nugetVersion/nuget.exe") {
        throw 'NuGet verifier download URL must use the pinned official dist.nuget.org URL.'
    }
    if ($nugetSha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'NuGet verifier sha256 must contain exactly 64 lowercase hexadecimal characters.'
    }
    return [pscustomobject]@{
        Version = $version
        Platform = $platform
        PackageId = $packageId
        PackageName = $packageName
        DownloadUrl = $downloadUrl
        Sha256 = $sha256
        RuntimeSubdirectory = $runtimeSubdirectory
        NugetVersion = $nugetVersion
        NugetName = $nugetName
        NugetDownloadUrl = $nugetDownloadUrl
        NugetSha256 = $nugetSha256
        ManifestPath = $manifestPath
        DirectoryName = "python-$version-$platform-nuget"
    }
}

function Get-AutoPromptPythonVersion {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$PythonPath)

    $output = (& $PythonPath --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $output -notmatch '^Python (\d+)\.(\d+)\.(\d+)$') {
        throw "Could not read a valid Python version from $PythonPath. Output: $output"
    }
    return [pscustomobject]@{
        Text = "$($Matches[1]).$($Matches[2]).$($Matches[3])"
        Major = [int]$Matches[1]
        Minor = [int]$Matches[2]
        Patch = [int]$Matches[3]
    }
}

function Resolve-AutoPromptPythonRuntime {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $manifest = Get-AutoPromptPythonRuntimeManifest -ProjectRoot $ProjectRoot
    $packageRoot = Join-Path $ProjectRoot ('.runtime\toolchains\' + $manifest.DirectoryName)
    $localRoot = Join-Path $packageRoot $manifest.RuntimeSubdirectory
    $localPython = Join-Path $localRoot 'python.exe'
    if (Test-Path -LiteralPath $packageRoot) {
        if (-not (Test-Path -LiteralPath $localPython -PathType Leaf)) {
            throw "Project-local Python runtime is incomplete: $localRoot. Re-run scripts\install_python_runtime.ps1."
        }
        $version = Get-AutoPromptPythonVersion -PythonPath $localPython
        if ($version.Text -ne $manifest.Version) {
            throw "Project-local Python runtime version mismatch. Expected $($manifest.Version), found $($version.Text)."
        }
        return [pscustomobject]@{
            PythonPath = $localPython
            Version = $version.Text
            Source = 'project-local'
            RuntimeRoot = $localRoot
        }
    }

    foreach ($name in @('python.exe', 'python3.exe')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        try {
            $version = Get-AutoPromptPythonVersion -PythonPath $command.Source
        } catch {
            continue
        }
        if ($version.Major -eq 3 -and $version.Minor -eq 11) {
            return [pscustomobject]@{
                PythonPath = $command.Source
                Version = $version.Text
                Source = 'system-path'
                RuntimeRoot = Split-Path -Parent $command.Source
            }
        }
    }
    return $null
}

function Repair-AutoPromptBackendVenv {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$BackendRoot,
        [Parameter(Mandatory = $true)]$Runtime
    )

    $venvRoot = Join-Path $BackendRoot '.venv'
    $venvPython = Join-Path $venvRoot 'Scripts\python.exe'
    if (Test-AutoPromptPythonExecutable -PythonPath $venvPython) {
        return $venvPython
    }

    # AppData cleanup can remove only the base interpreter while leaving the
    # project's packages and venv launcher intact. Repoint that launcher first;
    # this avoids overwriting a running executable and preserves installed deps.
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $configuration = @(
            "home = $($Runtime.RuntimeRoot)",
            'include-system-site-packages = false',
            "version = $($Runtime.Version)",
            "executable = $($Runtime.PythonPath)",
            "command = $($Runtime.PythonPath) -m venv $venvRoot"
        ) -join [Environment]::NewLine
        [System.IO.File]::WriteAllText(
            (Join-Path $venvRoot 'pyvenv.cfg'),
            $configuration + [Environment]::NewLine,
            [System.Text.UTF8Encoding]::new($false)
        )
        if (Test-AutoPromptPythonExecutable -PythonPath $venvPython) {
            return $venvPython
        }
    }

    if (Test-Path -LiteralPath $venvRoot -PathType Container) {
        & $Runtime.PythonPath -m venv --upgrade $venvRoot
    } else {
        & $Runtime.PythonPath -m venv $venvRoot
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-AutoPromptPythonExecutable -PythonPath $venvPython)) {
        throw "Backend virtual environment repair failed: $venvPython"
    }
    return $venvPython
}

function Set-AutoPromptPipCacheEnvironment {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $cacheRoot = Join-Path $ProjectRoot '.runtime\pip-cache'
    New-Item -ItemType Directory -Path $cacheRoot -Force | Out-Null
    [Environment]::SetEnvironmentVariable('PIP_CACHE_DIR', $cacheRoot, 'Process')
    return $cacheRoot
}

function Test-AutoPromptPythonExecutable {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$PythonPath)

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        return $false
    }
    try {
        & $PythonPath -c 'import sys; assert sys.version_info[:2] == (3, 11)' 2>$null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}
