param(
    [ValidatePattern('^[A-Z]$')]
    [string]$IdfDriveLetter = 'I',

    [switch]$Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$vendorRoot = Join-Path $workspaceRoot '.vendor'
$xiaozhiRoot = Join-Path $vendorRoot 'xiaozhi-esp32-main'
$idfRepository = Join-Path $vendorRoot 'esp-idf\v6.0.2\esp-idf'
$activationScript = Join-Path $vendorRoot `
    'esp-idf\activate_idf_v6.0.2.ps1\Microsoft.v6.0.2.PowerShell_profile.ps1'
$shimDirectory = Join-Path $vendorRoot 'idf-shim'
$shimPath = Join-Path $shimDirectory 'idf.py'
$driveName = "$IdfDriveLetter`:"
$driveRoot = "$driveName\"
$mappingCreated = $false
$previousLocation = Get-Location

function Get-SubstTarget {
    param([string]$Name)

    $prefix = "$Name\: => "
    $line = subst.exe | Where-Object { $_.StartsWith($prefix) }
    if ($null -eq $line) {
        return $null
    }
    return $line.Substring($prefix.Length)
}

$existingTarget = Get-SubstTarget -Name $driveName
if ($null -ne $existingTarget) {
    $expectedTarget = [System.IO.Path]::GetFullPath($idfRepository).TrimEnd('\')
    if ($existingTarget.TrimEnd('\') -ne $expectedTarget) {
        throw "$driveName is already mapped to $existingTarget"
    }
} else {
    subst.exe $driveName $idfRepository
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to map $driveName to $idfRepository"
    }
    $mappingCreated = $true
}

try {
    if (-not (Test-Path $activationScript)) {
        throw 'Run scripts/setup-esp-idf.ps1 before building firmware'
    }
    if (-not (Test-Path (Join-Path $xiaozhiRoot 'scripts\build.py'))) {
        throw "xiaozhi source snapshot is missing: $xiaozhiRoot"
    }

    Set-StrictMode -Off
    . $activationScript
    Set-StrictMode -Version Latest
    $env:IDF_PATH = $driveRoot

    $idfExecutable = (Get-Command 'idf.py.exe' -CommandType Application).Source
    New-Item -ItemType Directory -Force $shimDirectory | Out-Null
    Copy-Item -Force $idfExecutable $shimPath
    $env:PATH = "$shimDirectory;$env:PATH"

    $buildNinja = Join-Path $xiaozhiRoot 'build\build.ninja'
    $shortIdfMarker = "$IdfDriveLetter`:/components"
    $staleLongPathBuild = (Test-Path $buildNinja) -and -not (
        Select-String -Quiet -SimpleMatch $shortIdfMarker $buildNinja
    )
    if ($Clean -or $staleLongPathBuild) {
        Set-Location $xiaozhiRoot
        & $shimPath fullclean
        if ($LASTEXITCODE -ne 0) {
            throw "idf.py fullclean failed with exit code $LASTEXITCODE"
        }
    }

    $python = Join-Path $env:IDF_PYTHON_ENV_PATH 'Scripts\python.exe'
    Set-Location $xiaozhiRoot
    & $python scripts\build.py `
        'waveshare/esp32-s3-audio-board' `
        --name 'esp32-s3-audio-board'
    if ($LASTEXITCODE -ne 0) {
        throw "xiaozhi build failed with exit code $LASTEXITCODE"
    }

    $mergedImage = Join-Path $xiaozhiRoot 'build\merged-binary.bin'
    $applicationImage = Join-Path $xiaozhiRoot 'build\xiaozhi.bin'
    if (-not (Test-Path $mergedImage) -or -not (Test-Path $applicationImage)) {
        throw 'Firmware build completed without the expected binary images'
    }

    [PSCustomObject]@{
        Board = 'waveshare/esp32-s3-audio-board'
        BuildName = 'esp32-s3-audio-board'
        MergedImage = $mergedImage
        MergedImageSha256 = (Get-FileHash -Algorithm SHA256 $mergedImage).Hash
        ApplicationImage = $applicationImage
        ApplicationImageSha256 = (
            Get-FileHash -Algorithm SHA256 $applicationImage
        ).Hash
    }
} finally {
    Set-Location $previousLocation
    if ($mappingCreated) {
        subst.exe $driveName /D
    }
}
