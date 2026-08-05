param(
    [switch]$ForceInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$vendorRoot = Join-Path $workspaceRoot '.vendor'
$eimPath = Join-Path $vendorRoot 'eim-cli-windows-x64.exe'
$idfBase = Join-Path $vendorRoot 'esp-idf'
$idfRepository = Join-Path $idfBase 'v6.0.2\esp-idf'
$activationDirectory = Join-Path $idfBase 'activate_idf_v6.0.2.ps1'
$activationScript = Join-Path $activationDirectory 'Microsoft.v6.0.2.PowerShell_profile.ps1'
$configurationPath = Join-Path $idfBase 'eim_config.toml'
$eimMetadataPath = Join-Path $idfBase 'tools\eim_idf.json'
$shimDirectory = Join-Path $vendorRoot 'idf-shim'
$shimPath = Join-Path $shimDirectory 'idf.py'

$eimUrl = 'https://github.com/espressif/idf-im-ui/releases/download/v0.12.6/eim-cli-windows-x64.exe'
$expectedEimHash = '1658FBEA7B0DDD842414A2AD73B227CA7002AF18A35E0088DE2CF339CF60720F'
$expectedIdfCommit = '7101770dc6db2667b3c477cc31365dd1acd6db4e'

New-Item -ItemType Directory -Force $vendorRoot | Out-Null
if (-not (Test-Path $eimPath)) {
    Invoke-WebRequest -Headers @{ 'User-Agent' = 'XiaoYao-Setup' } `
        -Uri $eimUrl `
        -OutFile $eimPath
}

$actualEimHash = (Get-FileHash -Algorithm SHA256 $eimPath).Hash
if ($actualEimHash -ne $expectedEimHash) {
    throw "EIM checksum mismatch: expected $expectedEimHash, got $actualEimHash"
}

if ($ForceInstall -or -not (Test-Path $activationScript)) {
    New-Item -ItemType Directory -Force $idfBase | Out-Null
    $installArguments = @(
        'install',
        '-p', $idfBase,
        '--esp-idf-json-path', $eimMetadataPath,
        '-i', 'v6.0.2',
        '-t', 'esp32s3',
        '-n', 'true',
        '--do-not-track', 'true',
        '--config-file-save-path', $configurationPath,
        '--activation-script-path-override', $activationDirectory,
        '--cleanup', 'true'
    )
    & $eimPath @installArguments
    if ($LASTEXITCODE -ne 0) {
        throw "EIM installation failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path $activationScript)) {
    throw "ESP-IDF activation script is missing: $activationScript"
}

$actualIdfCommit = (& git -C $idfRepository rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $actualIdfCommit -ne $expectedIdfCommit) {
    throw "ESP-IDF commit mismatch: expected $expectedIdfCommit, got $actualIdfCommit"
}

Set-StrictMode -Off
. $activationScript
Set-StrictMode -Version Latest
$idfVersion = (& idf.py --version).Trim()
if ($idfVersion -ne 'ESP-IDF v6.0.2') {
    throw "Unexpected ESP-IDF version: $idfVersion"
}

$idfExecutable = (Get-Command 'idf.py.exe' -CommandType Application).Source
New-Item -ItemType Directory -Force $shimDirectory | Out-Null
Copy-Item -Force $idfExecutable $shimPath

& $shimPath --version
if ($LASTEXITCODE -ne 0) {
    throw 'The Python subprocess-compatible idf.py shim failed validation'
}

[PSCustomObject]@{
    EimVersion = (& $eimPath --version).Trim()
    EimSha256 = $actualEimHash
    IdfVersion = $idfVersion
    IdfCommit = $actualIdfCommit
    IdfRepository = $idfRepository
    IdfToolsPath = $env:IDF_TOOLS_PATH
    SubprocessShim = $shimPath
}
