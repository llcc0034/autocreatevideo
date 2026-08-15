function Get-AutoPromptRestartEnvValue {
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

function Resolve-AutoPromptComfyUIRootFromOutputDirectory {
    [CmdletBinding()]
    param([AllowEmptyString()][string]$OutputDirectory)

    if ([string]::IsNullOrWhiteSpace($OutputDirectory) -or
        -not [System.IO.Path]::IsPathRooted($OutputDirectory)) {
        return $null
    }

    try {
        $output = [System.IO.DirectoryInfo]::new([System.IO.Path]::GetFullPath($OutputDirectory))
    } catch {
        return $null
    }
    if ($output.Name -ine 'output' -or $null -eq $output.Parent -or $output.Parent.Name -ine 'ComfyUI' -or
        $null -eq $output.Parent.Parent) {
        return $null
    }

    $portableRoot = $output.Parent.Parent.FullName
    $python = Join-Path $portableRoot 'python_embeded\python.exe'
    $main = Join-Path $portableRoot 'ComfyUI\main.py'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath $main -PathType Leaf)) {
        return $null
    }
    return [System.IO.Path]::GetFullPath($portableRoot)
}

function Resolve-AutoPromptComfyUIRoot {
    [CmdletBinding()]
    param(
        [AllowEmptyString()][string]$ExplicitRoot,
        [AllowEmptyString()][string]$ProcessOutputDirectory,
        [Parameter(Mandatory = $true)][string]$BackendEnvPath,
        [AllowEmptyString()][string]$ProcessOutputHostDirectory,
        [Parameter(Mandatory = $true)][string]$RootEnvPath
    )

    if (-not [string]::IsNullOrWhiteSpace($ExplicitRoot)) {
        return [pscustomobject]@{ Root = $ExplicitRoot; Source = 'explicit' }
    }

    $candidates = @(
        [pscustomobject]@{ Value = $ProcessOutputDirectory; Source = 'COMFYUI_OUTPUT_DIR' },
        [pscustomobject]@{ Value = (Get-AutoPromptRestartEnvValue -Path $BackendEnvPath -Name 'COMFYUI_OUTPUT_DIR'); Source = 'backend/.env COMFYUI_OUTPUT_DIR' },
        [pscustomobject]@{ Value = $ProcessOutputHostDirectory; Source = 'COMFYUI_OUTPUT_HOST_DIR' },
        [pscustomobject]@{ Value = (Get-AutoPromptRestartEnvValue -Path $RootEnvPath -Name 'COMFYUI_OUTPUT_HOST_DIR'); Source = '.env COMFYUI_OUTPUT_HOST_DIR' }
    )
    foreach ($candidate in $candidates) {
        $root = Resolve-AutoPromptComfyUIRootFromOutputDirectory -OutputDirectory $candidate.Value
        if (-not [string]::IsNullOrWhiteSpace($root)) {
            return [pscustomobject]@{ Root = $root; Source = $candidate.Source }
        }
    }
    return $null
}
