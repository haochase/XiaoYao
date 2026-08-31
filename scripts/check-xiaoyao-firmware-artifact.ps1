[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ImagePath,
    [string]$ExpectedSha256
)

$ErrorActionPreference = "Stop"

function Get-Sha256 {
    param([string]$Path)

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        return (($algorithm.ComputeHash($stream) | ForEach-Object {
            $_.ToString("X2")
        }) -join "")
    } finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
}

try {
    $expectedSha256WasProvided = $PSBoundParameters.ContainsKey("ExpectedSha256")
    if ($expectedSha256WasProvided -and
        -not [regex]::IsMatch($ExpectedSha256, "^[0-9a-fA-F]{64}$")) {
        throw "ExpectedSha256 must contain exactly 64 hexadecimal characters."
    }

    if (-not (Test-Path -LiteralPath $ImagePath -PathType Leaf)) {
        throw "Firmware image does not exist: $ImagePath"
    }

    $image = Get-Item -LiteralPath $ImagePath
    if ($image.Length -le 0) {
        throw "Firmware image is empty: $($image.FullName)"
    }

    $actualSha256 = Get-Sha256 -Path $image.FullName
    $normalizedExpectedSha256 = if ($expectedSha256WasProvided) {
        $ExpectedSha256.ToUpperInvariant()
    } else {
        $null
    }

    if ($expectedSha256WasProvided -and $actualSha256 -ne $normalizedExpectedSha256) {
        throw "Firmware image SHA256 does not match the expected value."
    }

    [pscustomobject]@{
        image_path = $image.FullName
        length_bytes = $image.Length
        sha256 = $actualSha256
        expected_sha256 = $normalizedExpectedSha256
        sha256_matches_expected = $true
    } | ConvertTo-Json -Compress
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
