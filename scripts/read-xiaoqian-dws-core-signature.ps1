param(
    [Parameter(Mandatory = $true)]
    [string]$CorePath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$securityModule = Join-Path `
    $PSHOME `
    'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1'
Import-Module -Name $securityModule -Force -ErrorAction Stop

$signature = Get-AuthenticodeSignature -LiteralPath $CorePath
$certificate = $signature.SignerCertificate
$publisherName = ''
$thumbprint = ''
if ($null -ne $certificate) {
    $publisherName = $certificate.GetNameInfo(
        [System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName,
        $false
    )
    $thumbprint = $certificate.Thumbprint.ToUpperInvariant()
}

[ordered]@{
    status = [string]$signature.Status
    publisher_name = $publisherName
    signer_thumbprint = $thumbprint
} | ConvertTo-Json -Compress
