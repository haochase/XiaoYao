[CmdletBinding()]
param()

$syncPort = 8731
$devicePort = 8723
$syncListener = @(
    Get-NetTCPConnection -LocalPort $syncPort -State Listen -ErrorAction SilentlyContinue
)
$syncListenerAddresses = @(
    $syncListener |
        ForEach-Object { $_.LocalAddress.ToString() } |
        Sort-Object -Unique
)
$nonLoopbackListenerAddresses = @(
    $syncListenerAddresses |
        Where-Object { $_ -notin @("127.0.0.1", "::1") }
)
$syncLoopbackOnly = (
    $syncListener.Count -gt 0 -and
    $syncListenerAddresses.Count -gt 0 -and
    $nonLoopbackListenerAddresses.Count -eq 0
)
$syncExpectedLoopback = $syncListenerAddresses -contains "127.0.0.1"

function Test-SuccessEndpoint([string]$Uri) {
    try {
        $response = Invoke-WebRequest -Uri $Uri -TimeoutSec 3 -UseBasicParsing
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

$health = Test-SuccessEndpoint "http://127.0.0.1:8731/health"
$ready = Test-SuccessEndpoint "http://127.0.0.1:8731/ready"
$deviceOpenApiAvailable = $false
$deviceSyncRoutesAbsent = $false

try {
    $deviceResponse = Invoke-WebRequest `
        -Uri "http://127.0.0.1:8723/openapi.json" `
        -TimeoutSec 3 `
        -UseBasicParsing
    if ($deviceResponse.StatusCode -eq 200) {
        $deviceOpenApiAvailable = $true
        $deviceOpenApi = $deviceResponse.Content | ConvertFrom-Json -ErrorAction Stop
        $devicePaths = @($deviceOpenApi.paths.PSObject.Properties.Name)
        $deviceSyncRoutesAbsent = (
            $devicePaths -notcontains "/v1/projects/{project_id}/sync" -and
            $devicePaths -notcontains "/v1/projects/{project_id}/sync/status"
        )
    }
} catch {
    $deviceOpenApiAvailable = $false
    $deviceSyncRoutesAbsent = $false
}

[pscustomobject]@{
    sync_port = $syncPort
    sync_listening = $syncListener.Count -gt 0
    sync_loopback_only = $syncLoopbackOnly
    sync_expected_loopback = $syncExpectedLoopback
    health = $health
    ready = $ready
    device_port = $devicePort
    device_openapi_available = $deviceOpenApiAvailable
    device_sync_routes_absent = $deviceSyncRoutesAbsent
}

if (
    $syncListener.Count -eq 0 -or
    -not $syncLoopbackOnly -or
    -not $syncExpectedLoopback -or
    -not $health -or
    -not $ready -or
    -not $deviceOpenApiAvailable -or
    -not $deviceSyncRoutesAbsent
) {
    exit 1
}
