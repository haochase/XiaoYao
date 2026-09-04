param(
    [ValidatePattern('^[A-Z]$')]
    [string]$IdfDriveLetter = 'I',

    [Parameter(Mandatory)]
    [string]$OtaUrl,

    [string]$XiaozhiSourcePath,

    [string]$IdfRepositoryPath,

    [switch]$Clean
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$profileRenderer = Join-Path $workspaceRoot 'tools\firmware_profile.py'
if (-not (Test-Path $profileRenderer)) {
    throw "XiaoYao profile renderer is missing: $profileRenderer"
}

function Resolve-ProfilePython {
    $candidates = @(
        Get-Command 'python' -CommandType Application |
            ForEach-Object Source |
            Select-Object -Unique
    )
    foreach ($candidate in $candidates) {
        & $candidate $profileRenderer --select-vendor-root $workspaceRoot 2>$null |
            Out-Null
        if ($LASTEXITCODE -eq 0) {
            return $candidate
        }
    }
    throw 'No local Python interpreter can run the XiaoYao profile renderer'
}

$hostPython = Resolve-ProfilePython
$vendorRoot = & $hostPython $profileRenderer --select-vendor-root $workspaceRoot
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($vendorRoot)) {
    throw 'Unable to locate the XiaoYao vendor directory'
}
if ([string]::IsNullOrWhiteSpace($XiaozhiSourcePath)) {
    $xiaozhiRoot = Join-Path $vendorRoot 'xiaozhi-esp32-main'
} else {
    $xiaozhiRoot = [System.IO.Path]::GetFullPath($XiaozhiSourcePath)
}
if ([string]::IsNullOrWhiteSpace($IdfRepositoryPath)) {
    $idfRepository = Join-Path $vendorRoot 'esp-idf\v6.0.2\esp-idf'
    $activationScript = Join-Path $vendorRoot `
        'esp-idf\activate_idf_v6.0.2.ps1\Microsoft.v6.0.2.PowerShell_profile.ps1'
} else {
    $idfRepository = [System.IO.Path]::GetFullPath($IdfRepositoryPath)
    $activationScript = Join-Path $idfRepository 'export.ps1'
}
$profileTemplate = Join-Path $workspaceRoot 'firmware\xiaoyao.config.json'
$localProfile = Join-Path $xiaozhiRoot `
    'main\boards\waveshare\esp32-s3-audio-board\xiaoyao.local.config.json'
$driveName = "$IdfDriveLetter`:"
$driveRoot = "$driveName\"
$mappingCreated = $false
$localProfileCreated = $false
$previousLocation = Get-Location
$previousEnvironment = @{}
Get-ChildItem Env: | ForEach-Object {
    $previousEnvironment[$_.Name] = $_.Value
}

function Get-SubstTarget {
    param([string]$Name)

    $prefix = "$Name\: => "
    $line = subst.exe | Where-Object { $_.StartsWith($prefix) }
    if ($null -eq $line) {
        return $null
    }
    return $line.Substring($prefix.Length)
}

function Assert-XiaoYaoBuildOutput {
    param(
        [string]$SdkconfigPath,
        [string]$ImagePath,
        [datetime]$BuildStartedAt,
        [string]$ExpectedOtaUrl
    )

    if (-not (Test-Path $SdkconfigPath)) {
        throw "Build completed without sdkconfig: $SdkconfigPath"
    }
    $requiredSettings = @(
        'CONFIG_USE_CUSTOM_WAKE_WORD=y',
        'CONFIG_SR_MN_CN_MULTINET6_QUANT=y',
        'CONFIG_CUSTOM_WAKE_WORD="ni hao xiao yao"',
        'CONFIG_CUSTOM_WAKE_WORD_THRESHOLD=50',
        'CONFIG_XIAOYAO_WEBSOCKET_ONLY=y',
        'CONFIG_XIAOYAO_VAD_EVENTS=y',
        'CONFIG_XIAOYAO_PERSISTENT_CONTROL_CHANNEL=y',
        'CONFIG_CAMERA_OV2640=y',
        'CONFIG_SPIRAM=y',
        ('CONFIG_OTA_URL="' + $ExpectedOtaUrl + '"')
    )
    foreach ($setting in $requiredSettings) {
        if (-not (Select-String -LiteralPath $SdkconfigPath -SimpleMatch $setting -Quiet)) {
            throw "Build sdkconfig is missing required XiaoYao setting: $setting"
        }
    }
    if (-not (Test-Path $ImagePath)) {
        throw "Build completed without merged image: $ImagePath"
    }
    if ((Get-Item $ImagePath).LastWriteTime -lt $BuildStartedAt) {
        throw "Merged image predates this build: $ImagePath"
    }
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
        throw "ESP-IDF activation script is missing: $activationScript"
    }
    if (-not (Test-Path (Join-Path $xiaozhiRoot 'scripts\build.py'))) {
        throw "xiaozhi source snapshot is missing: $xiaozhiRoot"
    }
    & $hostPython $profileRenderer --apply-vendor-profile $xiaozhiRoot
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to apply the XiaoYao source profile'
    }
    if (-not (Test-Path $profileTemplate)) {
        throw "XiaoYao firmware template is missing: $profileTemplate"
    }
    if (Test-Path $localProfile) {
        throw "Temporary XiaoYao profile already exists: $localProfile"
    }

    if (-not [string]::IsNullOrWhiteSpace($env:IDF_PYTHON_ENV_PATH)) {
        $idfPythonScripts = Join-Path $env:IDF_PYTHON_ENV_PATH 'Scripts'
        if (Test-Path $idfPythonScripts) {
            $env:PATH = "$idfPythonScripts;$env:PATH"
        }
    }

    Set-StrictMode -Off
    . $activationScript
    Set-StrictMode -Version Latest
    $env:IDF_PATH = $driveRoot

    $python = Join-Path $env:IDF_PYTHON_ENV_PATH 'Scripts\python.exe'
    $idfScript = Join-Path $idfRepository 'tools\idf.py'
    if (-not (Test-Path $idfScript)) {
        throw "Specified ESP-IDF repository does not contain idf.py: $idfScript"
    }
    $idfVersion = (& $python $idfScript --version | Select-Object -Last 1).Trim()
    if ($LASTEXITCODE -ne 0 -or $idfVersion -ne 'ESP-IDF v6.0.2') {
        throw "Unexpected ESP-IDF repository version: $idfVersion"
    }
    $env:XIAOYAO_IDF_SCRIPT = $idfScript

    $buildNinja = Join-Path $xiaozhiRoot 'build\build.ninja'
    $shortIdfMarker = "$IdfDriveLetter`:/components"
    $staleLongPathBuild = (Test-Path $buildNinja) -and -not (
        Select-String -Quiet -SimpleMatch $shortIdfMarker $buildNinja
    )
    if ($Clean -or $staleLongPathBuild) {
        Set-Location $xiaozhiRoot
        & $python $idfScript fullclean
        if ($LASTEXITCODE -ne 0) {
            throw "idf.py fullclean failed with exit code $LASTEXITCODE"
        }
    }

    & $python $profileRenderer `
        --template $profileTemplate `
        --ota-url $OtaUrl `
        --output $localProfile
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to render the temporary XiaoYao profile"
    }
    $localProfileCreated = $true

    Set-Location $xiaozhiRoot
    $buildStartedAt = Get-Date
    & $python scripts\build.py `
        'waveshare/esp32-s3-audio-board' `
        -c 'xiaoyao.local.config.json' `
        --name 'esp32-s3-audio-board'
    if ($LASTEXITCODE -ne 0) {
        throw "xiaozhi build failed with exit code $LASTEXITCODE"
    }

    $mergedImage = Join-Path $xiaozhiRoot 'build\merged-binary.bin'
    $applicationImage = Join-Path $xiaozhiRoot 'build\xiaozhi.bin'
    $buildSdkconfig = Join-Path $xiaozhiRoot 'sdkconfig'
    if (-not (Test-Path $mergedImage) -or -not (Test-Path $applicationImage)) {
        throw 'Firmware build completed without the expected binary images'
    }
    Assert-XiaoYaoBuildOutput `
        -SdkconfigPath $buildSdkconfig `
        -ImagePath $mergedImage `
        -BuildStartedAt $buildStartedAt `
        -ExpectedOtaUrl $OtaUrl

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
    Get-ChildItem Env: | ForEach-Object {
        if (-not $previousEnvironment.ContainsKey($_.Name)) {
            [Environment]::SetEnvironmentVariable($_.Name, $null, 'Process')
        }
    }
    foreach ($entry in $previousEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable(
            $entry.Key,
            $entry.Value,
            'Process'
        )
    }
}
