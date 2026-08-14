param(
    [ValidatePattern('^[A-Z]$')]
    [string]$IdfDriveLetter = 'I',

    [Parameter(Mandatory)]
    [string]$OtaUrl,

    [string]$XiaozhiSourcePath,

    [switch]$Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$profileRenderer = Join-Path $workspaceRoot 'tools\firmware_profile.py'
if (-not (Test-Path $profileRenderer)) {
    throw "XiaoYao profile renderer is missing: $profileRenderer"
}
$hostPython = (Get-Command 'python' -CommandType Application).Source
$vendorRoot = & $hostPython $profileRenderer --select-vendor-root $workspaceRoot
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($vendorRoot)) {
    throw 'Unable to locate the XiaoYao vendor directory'
}
if ([string]::IsNullOrWhiteSpace($XiaozhiSourcePath)) {
    $xiaozhiRoot = Join-Path $vendorRoot 'xiaozhi-esp32-main'
} else {
    $xiaozhiRoot = [System.IO.Path]::GetFullPath($XiaozhiSourcePath)
}
$idfRepository = Join-Path $vendorRoot 'esp-idf\v6.0.2\esp-idf'
$activationScript = Join-Path $vendorRoot `
    'esp-idf\activate_idf_v6.0.2.ps1\Microsoft.v6.0.2.PowerShell_profile.ps1'
$shimDirectory = Join-Path $vendorRoot 'idf-shim'
$shimPath = Join-Path $shimDirectory 'idf.py'
$firmwarePatch = Join-Path $workspaceRoot 'firmware\patches\0001-xiaoyao-waveshare-profile.patch'
$profileTemplate = Join-Path $xiaozhiRoot `
    'main\boards\waveshare\esp32-s3-audio-board\xiaoyao.config.json'
$localProfile = Join-Path $xiaozhiRoot `
    'main\boards\waveshare\esp32-s3-audio-board\xiaoyao.local.config.json'
$driveName = "$IdfDriveLetter`:"
$driveRoot = "$driveName\"
$mappingCreated = $false
$localProfileCreated = $false
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

function Apply-XiaoYaoPatch {
    param(
        [string]$SourceRoot,
        [string]$PatchPath
    )

    $applyArgs = @('apply')
    if (-not (Test-Path (Join-Path $SourceRoot '.git'))) {
        $applyArgs += '--no-index'
    }

    & git -C $SourceRoot @applyArgs --check $PatchPath
    if ($LASTEXITCODE -eq 0) {
        & git -C $SourceRoot @applyArgs $PatchPath
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to apply XiaoYao firmware patch: $PatchPath"
        }
        return
    }

    & git -C $SourceRoot @applyArgs --reverse --check $PatchPath
    if ($LASTEXITCODE -ne 0) {
        throw "XiaoYao firmware patch does not match source snapshot: $PatchPath"
    }
    Write-Host 'XiaoYao firmware patch is already applied.'
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
    if (-not (Test-Path $firmwarePatch)) {
        throw "XiaoYao firmware patch is missing: $firmwarePatch"
    }
    Apply-XiaoYaoPatch -SourceRoot $xiaozhiRoot -PatchPath $firmwarePatch
    if (-not (Test-Path $profileTemplate)) {
        throw "XiaoYao firmware template is missing after patch application: $profileTemplate"
    }
    if (Test-Path $localProfile) {
        throw "Temporary XiaoYao profile already exists: $localProfile"
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
    & $python $profileRenderer `
        --template $profileTemplate `
        --ota-url $OtaUrl `
        --output $localProfile
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to render the temporary XiaoYao profile"
    }
    $localProfileCreated = $true

    Set-Location $xiaozhiRoot
    & $python scripts\build.py `
        'waveshare/esp32-s3-audio-board' `
        -c 'xiaoyao.local.config.json' `
        --name 'esp32-s3-audio-board'
    if ($LASTEXITCODE -ne 0) {
        throw "xiaozhi build failed with exit code $LASTEXITCODE"
    }

    $mergedImage = Join-Path $xiaozhiRoot 'build\merged-binary.bin'
    $applicationImage = Join-Path $xiaozhiRoot 'build\xiaozhi.bin'
    if (-not (Test-Path $mergedImage) -or -not (Test-Path $applicationImage)) {
        throw 'Firmware build completed without the expected binary images'
    }

    $result = [PSCustomObject]@{
        Board = 'waveshare/esp32-s3-audio-board'
        BuildName = 'esp32-s3-audio-board'
        MergedImage = $mergedImage
        MergedImageSha256 = (Get-FileHash -Algorithm SHA256 $mergedImage).Hash
        ApplicationImage = $applicationImage
        ApplicationImageSha256 = (
            Get-FileHash -Algorithm SHA256 $applicationImage
        ).Hash
    }
    Write-Host "Merged image SHA256: $($result.MergedImageSha256)"
    Write-Host "Application image SHA256: $($result.ApplicationImageSha256)"
    $result
} finally {
    Set-Location $previousLocation
    if ($localProfileCreated -and (Test-Path $localProfile)) {
        Remove-Item -LiteralPath $localProfile -Force
    }
    if ($mappingCreated) {
        subst.exe $driveName /D
    }
}
