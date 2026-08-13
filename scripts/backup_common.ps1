$script:AutoPromptDirectorySeparators = @(
    [System.IO.Path]::DirectorySeparatorChar,
    [System.IO.Path]::AltDirectorySeparatorChar
)

function Get-AutoPromptFullPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BasePath
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'Path cannot be empty.'
    }
    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) {
        $Path
    } else {
        Join-Path $BasePath $Path
    }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Get-AutoPromptTrimmedPath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($full.Equals($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $root
    }
    return $full.TrimEnd($script:AutoPromptDirectorySeparators)
}

function Test-AutoPromptPathWithin {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Candidate,
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$AllowEqual
    )

    $candidateFull = Get-AutoPromptTrimmedPath -Path $Candidate
    $rootFull = Get-AutoPromptTrimmedPath -Path $Root
    if ($candidateFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return [bool]$AllowEqual
    }
    $prefix = $rootFull.TrimEnd($script:AutoPromptDirectorySeparators) + [System.IO.Path]::DirectorySeparatorChar
    return $candidateFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-AutoPromptAbsoluteOverride {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowEmptyString()][string]$Value
    )

    if (-not [string]::IsNullOrWhiteSpace($Value) -and -not [System.IO.Path]::IsPathRooted($Value)) {
        throw "$Name must be an absolute path when explicitly supplied: $Value"
    }
}

function Assert-AutoPromptSafeDirectory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Purpose
    )

    $full = Get-AutoPromptTrimmedPath -Path $Path
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($full.Equals($root, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Purpose cannot be a filesystem root: $full"
    }

    $project = Get-AutoPromptTrimmedPath -Path $ProjectRoot
    if ($full.Equals($project, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Purpose cannot be the project root: $full"
    }
    if (Test-AutoPromptPathWithin -Candidate $project -Root $full -AllowEqual) {
        throw "$Purpose cannot be an ancestor of the project root: $full"
    }
    return $full
}

function Get-AutoPromptRelativePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )

    $pathFull = [System.IO.Path]::GetFullPath($Path)
    $rootFull = (Get-AutoPromptTrimmedPath -Path $Root) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $pathFull.StartsWith($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path is outside the expected root. Path=$pathFull Root=$Root"
    }
    return $pathFull.Substring($rootFull.Length).Replace('\', '/')
}

function Join-AutoPromptContainedPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Purpose
    )

    if ([string]::IsNullOrWhiteSpace($RelativePath) -or [System.IO.Path]::IsPathRooted($RelativePath)) {
        throw "$Purpose must be a non-empty relative path: $RelativePath"
    }
    $nativeRelative = $RelativePath.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
    $combined = [System.IO.Path]::GetFullPath((Join-Path $Root $nativeRelative))
    if (-not (Test-AutoPromptPathWithin -Candidate $combined -Root $Root)) {
        throw "$Purpose escapes its allowed root: $RelativePath"
    }
    return $combined
}

function Assert-AutoPromptNoReparsePoint {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$StopRoot,
        [Parameter(Mandatory = $true)][string]$Purpose
    )

    $current = [System.IO.Path]::GetFullPath($Path)
    $stop = Get-AutoPromptTrimmedPath -Path $StopRoot
    while ($true) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Purpose contains a reparse point, which is not followed for backup or restore: $current"
            }
        }
        if ($current.Equals($stop, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = Split-Path -Parent $current
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent.Equals($current, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "$Purpose is not contained by the expected root."
        }
        $current = Get-AutoPromptTrimmedPath -Path $parent
    }
}

function Get-AutoPromptEnvValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$' -and $Matches[1] -eq $Name) {
            $value = $Matches[2].Trim()
            if ($value.Length -ge 2) {
                $first = $value.Substring(0, 1)
                $last = $value.Substring($value.Length - 1, 1)
                if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
            }
            return $value
        }
    }
    return $null
}

function Get-AutoPromptRuntimeSettings {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [AllowEmptyString()][string]$BackendEnvPath,
        [AllowEmptyString()][string]$DatabaseUrl,
        [AllowEmptyString()][string]$MediaRoot,
        [AllowEmptyString()][string]$ComfyUIOutputDir
    )

    $project = Get-AutoPromptTrimmedPath -Path $ProjectRoot
    $backend = Join-Path $project 'backend'
    $envPath = if ([string]::IsNullOrWhiteSpace($BackendEnvPath)) {
        Join-Path $backend '.env'
    } else {
        [System.IO.Path]::GetFullPath($BackendEnvPath)
    }
    $rootEnv = Join-Path $project '.env'

    $db = $DatabaseUrl
    if ([string]::IsNullOrWhiteSpace($db)) { $db = $env:DATABASE_URL }
    if ([string]::IsNullOrWhiteSpace($db)) { $db = Get-AutoPromptEnvValue -Path $envPath -Name 'DATABASE_URL' }
    if ([string]::IsNullOrWhiteSpace($db)) { $db = 'sqlite:///db.sqlite3' }

    $media = if ([string]::IsNullOrWhiteSpace($MediaRoot)) {
        Join-Path $backend 'media'
    } else {
        [System.IO.Path]::GetFullPath($MediaRoot)
    }

    $comfy = $ComfyUIOutputDir
    if ([string]::IsNullOrWhiteSpace($comfy)) { $comfy = $env:COMFYUI_OUTPUT_DIR }
    if ([string]::IsNullOrWhiteSpace($comfy)) { $comfy = Get-AutoPromptEnvValue -Path $envPath -Name 'COMFYUI_OUTPUT_DIR' }
    if ([string]::IsNullOrWhiteSpace($comfy)) { $comfy = $env:COMFYUI_OUTPUT_HOST_DIR }
    if ([string]::IsNullOrWhiteSpace($comfy)) { $comfy = Get-AutoPromptEnvValue -Path $rootEnv -Name 'COMFYUI_OUTPUT_HOST_DIR' }
    if (-not [string]::IsNullOrWhiteSpace($comfy)) {
        if (-not [System.IO.Path]::IsPathRooted($comfy)) {
            throw "Configured ComfyUI output path must be absolute: $comfy"
        }
        $comfy = [System.IO.Path]::GetFullPath($comfy)
    }

    [pscustomobject]@{
        ProjectRoot = $project
        BackendRoot = [System.IO.Path]::GetFullPath($backend)
        BackendEnvPath = [System.IO.Path]::GetFullPath($envPath)
        DatabaseUrl = $db.Trim()
        MediaRoot = [System.IO.Path]::GetFullPath($media)
        ComfyUIOutputDir = $comfy
    }
}

function Get-AutoPromptDatabaseDescriptor {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$DatabaseUrl,
        [Parameter(Mandatory = $true)][string]$BackendRoot
    )

    if ($DatabaseUrl -match '^sqlite:/{3}(.*)$') {
        $raw = $Matches[1]
        $queryIndex = $raw.IndexOf('?')
        if ($queryIndex -ge 0) { $raw = $raw.Substring(0, $queryIndex) }
        $raw = [System.Uri]::UnescapeDataString($raw)
        if ($raw -eq ':memory:' -or [string]::IsNullOrWhiteSpace($raw)) {
            throw 'In-memory or empty SQLite databases cannot be backed up.'
        }
        if ($raw -match '^/[A-Za-z]:[/\\]') { $raw = $raw.Substring(1) }
        $path = if ([System.IO.Path]::IsPathRooted($raw)) {
            [System.IO.Path]::GetFullPath($raw)
        } else {
            [System.IO.Path]::GetFullPath((Join-Path $BackendRoot $raw))
        }
        return [pscustomobject]@{
            Kind = 'sqlite'
            Path = $path
            Display = $path
            ConnectionUrl = $null
            SafeConnectionUrl = $null
            Password = $null
        }
    }

    if ($DatabaseUrl -notmatch '^(postgres|postgresql)://') {
        throw 'Only sqlite:/// and postgresql:// (or postgres://) DATABASE_URL values are supported.'
    }

    $match = [regex]::Match($DatabaseUrl, '^(?<scheme>postgres(?:ql)?)://(?:(?<userinfo>[^@/]*)@)?(?<rest>.+)$')
    if (-not $match.Success) { throw 'Invalid PostgreSQL DATABASE_URL.' }
    $userinfo = $match.Groups['userinfo'].Value
    $password = $null
    $safeUserInfo = $userinfo
    if (-not [string]::IsNullOrWhiteSpace($userinfo)) {
        $passwordSeparator = $userinfo.IndexOf(':')
        if ($passwordSeparator -ge 0) {
            $safeUserInfo = $userinfo.Substring(0, $passwordSeparator)
            $password = [System.Uri]::UnescapeDataString($userinfo.Substring($passwordSeparator + 1))
        }
    }
    $prefix = $match.Groups['scheme'].Value + '://'
    $safeUrl = if ([string]::IsNullOrWhiteSpace($safeUserInfo)) {
        $prefix + $match.Groups['rest'].Value
    } else {
        $prefix + $safeUserInfo + '@' + $match.Groups['rest'].Value
    }
    $display = if ([string]::IsNullOrWhiteSpace($safeUserInfo)) {
        $safeUrl
    } else {
        $prefix + $safeUserInfo + ':***@' + $match.Groups['rest'].Value
    }
    return [pscustomobject]@{
        Kind = 'postgresql'
        Path = $null
        Display = $display
        ConnectionUrl = $DatabaseUrl
        SafeConnectionUrl = $safeUrl
        Password = $password
    }
}

function Get-AutoPromptPython {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$BackendRoot)

    $venvPython = Join-Path $BackendRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return $venvPython
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw 'Python is required for consistent SQLite backup and validation.' }
    return $command.Source
}

function Invoke-AutoPromptNative {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Step,
        [AllowNull()][string]$PostgresPassword
    )

    $previousPassword = $env:PGPASSWORD
    try {
        if ($PSBoundParameters.ContainsKey('PostgresPassword')) {
            $env:PGPASSWORD = $PostgresPassword
        }
        & $Executable @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Step failed with exit code $LASTEXITCODE."
        }
    } finally {
        $env:PGPASSWORD = $previousPassword
    }
}

function Get-AutoPromptUtcTimestamp {
    return [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
}
