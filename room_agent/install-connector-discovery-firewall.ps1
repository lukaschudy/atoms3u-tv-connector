[CmdletBinding()]
param(
    [string]$InterfaceAlias = "Wi-Fi 2"
)

$ErrorActionPreference = "Stop"
$displayName = "Room Agent Connector Discovery"
$existing = Get-NetFirewallRule -DisplayName $displayName -ErrorAction SilentlyContinue

if ($null -ne $existing) {
    Remove-NetFirewallRule -DisplayName $displayName
}

New-NetFirewallRule `
    -DisplayName $displayName `
    -Description "Allow ATOMS3U discovery only from the laptop-owned local hotspot." `
    -Direction Inbound `
    -Action Allow `
    -Protocol UDP `
    -LocalPort 8781 `
    -Profile Any `
    -RemoteAddress LocalSubnet `
    -InterfaceAlias $InterfaceAlias | Out-Null

Write-Host "Room Agent connector discovery is allowed on $InterfaceAlias only."
