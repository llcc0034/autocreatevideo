[CmdletBinding()]
param(
    [ValidateSet('cpu', 'none')]
    [string]$Runtime = 'cpu',
    [string]$LlamaCppPythonVersion = '0.3.34',
    [string]$Destination,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path $ProjectRoot '.runtime\models\Qwen\Qwen3-4B-GGUF'
}

$ModelId = 'Qwen/Qwen3-4B-GGUF'
$Revision = 'master'
$ModelFileName = 'Qwen3-4B-Q4_K_M.gguf'
$ExpectedSize = [int64]2497280256
$ExpectedSha256 = '7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5'
$ModelUrl = "https://modelscope.cn/models/$ModelId/resolve/$Revision/$ModelFileName"
$ModelPath = Join-Path $Destination $ModelFileName
$PartialPath = "$ModelPath.part"
$RuntimeRoot = Join-Path $ProjectRoot '.runtime\qwen-python'
$RuntimePython = Join-Path $RuntimeRoot 'Scripts\python.exe'

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE."
    }
}

function Get-VerifiedFile([string]$Path, [int64]$Size, [string]$Sha256) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $file = Get-Item -LiteralPath $Path
    if ($file.Length -ne $Size) {
        return $false
    }
    $actualHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    return $actualHash -eq $Sha256
}

function Invoke-ResumableDownload([string]$Url, [string]$OutputPath, [string]$Label) {
    $parent = Split-Path -Parent $OutputPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Write-Host "Downloading $Label ..." -ForegroundColor Cyan
    & curl.exe --fail --location --retry 8 --retry-delay 2 --retry-all-errors `
        --continue-at - --output $OutputPath $Url
    Assert-NativeSuccess "$Label download"
}

New-Item -ItemType Directory -Path $Destination -Force | Out-Null

$isAlreadyValid = Get-VerifiedFile -Path $ModelPath -Size $ExpectedSize -Sha256 $ExpectedSha256
if ($isAlreadyValid -and -not $Force) {
    Write-Host "Official model is already present and verified: $ModelPath" -ForegroundColor Green
} else {
    if ($Force) {
        Remove-Item -LiteralPath $ModelPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $PartialPath -Force -ErrorAction SilentlyContinue
    }
    Invoke-ResumableDownload -Url $ModelUrl -OutputPath $PartialPath -Label "$ModelId/$ModelFileName"

    $downloadedFile = Get-Item -LiteralPath $PartialPath
    if ($downloadedFile.Length -ne $ExpectedSize) {
        throw "Model size mismatch. Expected $ExpectedSize bytes, received $($downloadedFile.Length) bytes. The partial file was kept for a resumable retry."
    }
    $actualHash = (Get-FileHash -LiteralPath $PartialPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $ExpectedSha256) {
        throw "Model SHA256 mismatch. Expected $ExpectedSha256, received $actualHash. Delete the partial file only after investigating the source."
    }
    Move-Item -LiteralPath $PartialPath -Destination $ModelPath -Force
    Write-Host "Verified official model: $ModelPath" -ForegroundColor Green
}

foreach ($sidecar in @('README.md', 'LICENSE')) {
    $sidecarUrl = "https://modelscope.cn/models/$ModelId/resolve/$Revision/$sidecar"
    $sidecarPath = Join-Path $Destination $sidecar
    & curl.exe --fail --location --retry 5 --retry-delay 2 --output $sidecarPath $sidecarUrl
    Assert-NativeSuccess "$sidecar download"
}

if ($Runtime -eq 'cpu') {
    if (-not (Test-Path -LiteralPath $RuntimePython)) {
        $backendPython = Join-Path $ProjectRoot 'backend\.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $backendPython) {
            $bootstrapPython = $backendPython
        } else {
            $bootstrapPython = (Get-Command python -ErrorAction Stop).Source
        }
        Write-Host "Creating isolated local-Qwen runtime: $RuntimeRoot" -ForegroundColor Cyan
        & $bootstrapPython -m venv $RuntimeRoot
        Assert-NativeSuccess 'Local Qwen runtime creation'
    }
    Write-Host "Installing llama-cpp-python $LlamaCppPythonVersion CPU wheel ..." -ForegroundColor Cyan
    & $RuntimePython -m pip install --disable-pip-version-check --prefer-binary `
        --only-binary=llama-cpp-python `
        --extra-index-url 'https://abetlen.github.io/llama-cpp-python/whl/cpu' `
        "llama-cpp-python==$LlamaCppPythonVersion"
    Assert-NativeSuccess 'llama-cpp-python CPU runtime installation'
    & $RuntimePython -c "from llama_cpp import Llama; import llama_cpp; print('llama-cpp-python runtime verified:', llama_cpp.__version__)"
    Assert-NativeSuccess 'llama-cpp-python import verification'
}

$manifest = [ordered]@{
    repo_id = $ModelId
    revision = $Revision
    filename = $ModelFileName
    size = $ExpectedSize
    sha256 = $ExpectedSha256
    source_url = $ModelUrl
    license = 'Apache-2.0'
    runtime = if ($Runtime -eq 'cpu') { 'llama-cpp-python-cpu' } else { 'none' }
    runtime_version = if ($Runtime -eq 'cpu') { $LlamaCppPythonVersion } else { $null }
    verified_at = [DateTimeOffset]::Now.ToString('o')
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $Destination '.autoprompt-model.json') -Encoding UTF8

Write-Host ''
Write-Host 'Local translation model installation is ready.' -ForegroundColor Green
Write-Host "Model ID : $ModelId"
Write-Host "Model dir: $Destination"
Write-Host "Model file: $ModelPath"
Write-Host "Runtime  : $Runtime"
if ($Runtime -eq 'cpu') {
    Write-Host "Runtime Python: $RuntimePython"
}
