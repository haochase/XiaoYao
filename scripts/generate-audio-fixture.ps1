param(
    [string]$CultureName = 'zh-CN',
    [string]$Text = ''
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$assetDirectory = Join-Path $workspaceRoot 'assets\audio'
$wavePath = Join-Path $assetDirectory 'companion-greeting-zh-cn.wav'
$manifestPath = Join-Path $assetDirectory 'companion-greeting-zh-cn.json'

Add-Type -AssemblyName System.Speech
New-Item -ItemType Directory -Force $assetDirectory | Out-Null
if (-not $Text) {
    $Text = -join [char[]]@(
        0x4F60, 0x597D, 0xFF0C, 0x6211, 0x5728, 0x8FD9, 0x91CC, 0x3002,
        0x4ECA, 0x5929, 0x60F3, 0x5148, 0x804A, 0x70B9, 0x4EC0, 0x4E48,
        0xFF1F
    )
}

$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
    16000,
    [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
    [System.Speech.AudioFormat.AudioChannel]::Mono
)
$synthesizer = New-Object System.Speech.Synthesis.SpeechSynthesizer
$actualVoiceName = ''
try {
    $synthesizer.SelectVoiceByHints(
        [System.Speech.Synthesis.VoiceGender]::Female,
        [System.Speech.Synthesis.VoiceAge]::Adult,
        0,
        [System.Globalization.CultureInfo]::GetCultureInfo($CultureName)
    )
    if ($synthesizer.Voice.Culture.Name -ne $CultureName) {
        throw "SAPI selected an unexpected culture: $($synthesizer.Voice.Culture)"
    }
    $actualVoiceName = $synthesizer.Voice.Name
    $synthesizer.SetOutputToWaveFile($wavePath, $format)
    $synthesizer.Speak($Text)
    $synthesizer.SetOutputToNull()
} finally {
    $synthesizer.Dispose()
}

$waveBytes = [System.IO.File]::ReadAllBytes($wavePath)
if ($waveBytes.Length -lt 44) {
    throw 'Generated wave file is shorter than a PCM WAV header'
}

$byteRate = [System.BitConverter]::ToUInt32($waveBytes, 28)
$dataLength = $null
$chunkOffset = 12
while ($chunkOffset + 8 -le $waveBytes.Length) {
    $chunkName = [System.Text.Encoding]::ASCII.GetString($waveBytes, $chunkOffset, 4)
    $chunkLength = [System.BitConverter]::ToUInt32($waveBytes, $chunkOffset + 4)
    $chunkDataOffset = $chunkOffset + 8
    if ($chunkDataOffset + $chunkLength -gt $waveBytes.Length) {
        throw "Generated wave file has an invalid $chunkName chunk length"
    }
    if ($chunkName -eq 'data') {
        $dataLength = $chunkLength
        break
    }
    $chunkOffset = $chunkDataOffset + $chunkLength
    if ($chunkLength % 2 -ne 0) {
        $chunkOffset++
    }
}
if ($byteRate -eq 0 -or $dataLength -le 0) {
    throw 'Generated wave file has no PCM data'
}

$manifest = [ordered]@{
    text = $Text
    voice = $actualVoiceName
    sample_rate = 16000
    channels = 1
    sample_width_bytes = 2
    duration_seconds = $dataLength / $byteRate
    sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $wavePath).Hash.ToLowerInvariant()
}
$manifestJson = $manifest | ConvertTo-Json
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($manifestPath, $manifestJson, $utf8WithoutBom)

[PSCustomObject]@{
    WavePath = $wavePath
    ManifestPath = $manifestPath
    DurationSeconds = $manifest.duration_seconds
    Sha256 = $manifest.sha256
}
