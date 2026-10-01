[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $root 'native-host\com.sina.externalcall.json'
$registryPath = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.sina.externalcall'

if (Test-Path -LiteralPath $registryPath) {
    Remove-Item -LiteralPath $registryPath -Force
}
if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
    Remove-Item -LiteralPath $manifestPath -Force
}

Write-Output 'Removed only the ExternalCall native-host registration and generated manifest.'
