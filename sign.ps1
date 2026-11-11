<#
.SYNOPSIS
  Sign omd build outputs with Authenticode (SHA-256 + RFC 3161 timestamp).

.EXAMPLE
  # CA-issued certificate exported as .pfx
  ./sign.ps1 -Files dist/omd-setup.exe,dist/omd.exe -Pfx certs/signing.pfx -Password $env:OMD_PFX_PASSWORD

.EXAMPLE
  # certificate already installed in the current user's store
  ./sign.ps1 -Files dist/omd-setup.exe -Thumbprint 0123456789ABCDEF...
#>
param(
  [Parameter(Mandatory = $true)][string[]]$Files,
  [string]$Pfx,
  [string]$Password,
  [string]$Thumbprint,
  [string]$Timestamp = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

if ($Pfx) {
  if (-not (Test-Path -LiteralPath $Pfx)) { throw "pfx not found: $Pfx" }
  $secure = ConvertTo-SecureString -String $Password -Force -AsPlainText
  $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($Pfx, $secure)
} elseif ($Thumbprint) {
  $cert = Get-ChildItem "Cert:\CurrentUser\My\$Thumbprint"
  if (-not $cert) { throw "no certificate with thumbprint $Thumbprint" }
} else {
  throw "pass -Pfx/-Password or -Thumbprint"
}

if (-not $cert.HasPrivateKey) { throw "certificate has no private key" }

foreach ($file in $Files) {
  if (-not (Test-Path -LiteralPath $file)) { throw "file not found: $file" }
  $result = Set-AuthenticodeSignature -FilePath $file -Certificate $cert `
    -HashAlgorithm SHA256 -TimestampServer $Timestamp
  $check = Get-AuthenticodeSignature -FilePath $file
  "{0}: {1} | signer={2} | timestamped={3}" -f `
    (Split-Path $file -Leaf), $check.Status, $check.SignerCertificate.Subject, ($null -ne $check.TimeStamperCertificate)
}
