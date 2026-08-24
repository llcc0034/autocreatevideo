function Get-AutoPromptNormalizedOpenApiDocument {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    if (-not (Test-Path -LiteralPath $resolvedPath -PathType Leaf)) {
        throw "OpenAPI contract file does not exist: $resolvedPath"
    }

    $rawBytes = [System.IO.File]::ReadAllBytes($resolvedPath)
    $strictUtf8 = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        $text = $strictUtf8.GetString($rawBytes)
    } catch [System.Text.DecoderFallbackException] {
        throw "OpenAPI contract must be valid UTF-8: $resolvedPath"
    }

    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) {
        $text = $text.Substring(1)
    }
    $normalizedText = $text.Replace("`r`n", "`n").Replace("`r", "`n")
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $normalizedBytes = $utf8NoBom.GetBytes($normalizedText)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = [System.BitConverter]::ToString(
            $sha256.ComputeHash($normalizedBytes)
        ).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha256.Dispose()
    }

    return [pscustomobject]@{
        Path = $resolvedPath
        Text = $normalizedText
        Bytes = $normalizedBytes
        Sha256 = $digest
    }
}

function Assert-AutoPromptOpenApiAuthority {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$RootContractPath,

        [Parameter(Mandatory = $true)]
        [string]$LegacyContractPath
    )

    $rootPath = [System.IO.Path]::GetFullPath($RootContractPath)
    $legacyPath = [System.IO.Path]::GetFullPath($LegacyContractPath)
    if (-not (Test-Path -LiteralPath $rootPath -PathType Leaf)) {
        throw "The authoritative root OpenAPI contract is missing: $rootPath"
    }
    if (Test-Path -LiteralPath $legacyPath) {
        throw "Legacy OpenAPI contract must not exist: $legacyPath. The repository root openapi.yaml is the only machine contract."
    }
}

function Assert-AutoPromptOpenApiContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$CommittedPath,

        [Parameter(Mandatory = $true)]
        [string]$GeneratedPath
    )

    $committed = Get-AutoPromptNormalizedOpenApiDocument -Path $CommittedPath
    $generated = Get-AutoPromptNormalizedOpenApiDocument -Path $GeneratedPath
    $matches = $committed.Bytes.Length -eq $generated.Bytes.Length
    if ($matches) {
        for ($index = 0; $index -lt $committed.Bytes.Length; $index++) {
            if ($committed.Bytes[$index] -ne $generated.Bytes[$index]) {
                $matches = $false
                break
            }
        }
    }
    if ($matches) {
        return
    }

    # PowerShell treats a negative max-substrings argument as "do not split",
    # which would collapse every drift to a misleading line 1 report.  The
    # default split behavior preserves the line sequence needed here (including
    # a trailing empty item), so do not pass -1 as the count.
    $committedLines = @($committed.Text -split "`n")
    $generatedLines = @($generated.Text -split "`n")
    $lineCount = [System.Math]::Max($committedLines.Count, $generatedLines.Count)
    $firstDifferentLine = 1
    for ($lineIndex = 0; $lineIndex -lt $lineCount; $lineIndex++) {
        $committedLine = if ($lineIndex -lt $committedLines.Count) { $committedLines[$lineIndex] } else { $null }
        $generatedLine = if ($lineIndex -lt $generatedLines.Count) { $generatedLines[$lineIndex] } else { $null }
        if ($committedLine -cne $generatedLine) {
            $firstDifferentLine = $lineIndex + 1
            break
        }
    }

    throw (
        "OpenAPI contract drift detected at normalized line {0}. " +
        "Committed SHA-256={1}; generated SHA-256={2}. " +
        "The authoritative root openapi.yaml was not modified. " +
        "Review the API change, then explicitly regenerate it from backend with " +
        "'.\.venv\Scripts\python.exe manage.py spectacular --validate --fail-on-warn --file ..\openapi.yaml'."
    ) -f $firstDifferentLine, $committed.Sha256, $generated.Sha256
}
