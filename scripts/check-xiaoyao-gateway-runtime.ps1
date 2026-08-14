[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8723,
    [string]$ExpectedHost = "0.0.0.0"
)

$listener = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
$expectedAddresses = @()
try {
    $expectedAddresses = @([System.Net.IPAddress]::Parse($ExpectedHost))
} catch {
    try {
        $expectedAddresses = @([System.Net.Dns]::GetHostAddresses($ExpectedHost))
    } catch {
        $expectedAddresses = @()
    }
}

$listenerAddresses = @($listener | ForEach-Object { $_.LocalAddress.ToString() })
$expectedHostListening = $false
foreach ($expectedAddress in $expectedAddresses) {
    $address = $expectedAddress.ToString()
    if ($listenerAddresses -contains $address) {
        $expectedHostListening = $true
        break
    }

    if ($expectedAddress.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and
        $listenerAddresses -contains "0.0.0.0") {
        $expectedHostListening = $true
        break
    }

    if ($expectedAddress.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6 -and
        $listenerAddresses -contains "::") {
        $expectedHostListening = $true
        break
    }
}
$health = $false

try {
    $response = Invoke-WebRequest `
        -Uri "http://127.0.0.1:$Port/health" `
        -TimeoutSec 3 `
        -UseBasicParsing
    $health = $response.StatusCode -eq 200
} catch {
    $health = $false
}

[pscustomobject]@{
    port = $Port
    listening = $listener.Count -gt 0
    health = $health
    expected_host_resolved = $expectedAddresses.Count -gt 0
    expected_host_listening = $expectedHostListening
}

if ($listener.Count -eq 0 -or -not $health -or
    $expectedAddresses.Count -eq 0 -or -not $expectedHostListening) {
    exit 1
}
