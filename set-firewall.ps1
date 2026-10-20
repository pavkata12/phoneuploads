param(
    [Parameter(Mandatory = $true)]
    [int]$Port
)

$ErrorActionPreference = "Stop"
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Administrator rights are required to change Windows Firewall. The server bind address is still restricted without this rule."
    exit 1
}
if ($Port -lt 1024 -or $Port -gt 65535) {
    Write-Error "Port must be between 1024 and 65535."
    exit 1
}

$adapters = @(Get-NetAdapter | Where-Object {
        $_.InterfaceDescription -match "Tailscale" -or $_.Name -match "Tailscale"
    })
if ($adapters.Count -eq 0) {
    Write-Error "No Tailscale network adapter was found. Install Tailscale, sign in, then run set-firewall.ps1 again."
    exit 2
}
$names = @($adapters | ForEach-Object { $_.Name })

Get-NetFirewallRule -DisplayName "PhoneUploads (Tailscale only)" -ErrorAction SilentlyContinue |
    Remove-NetFirewallRule

New-NetFirewallRule `
    -DisplayName "PhoneUploads (Tailscale only)" `
    -Description "Allows the personal upload server only on the Tailscale adapter from tailnet addresses 100.64.0.0/10. The application also refuses to listen on 0.0.0.0 or a LAN address." `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $Port `
    -InterfaceAlias $names `
    -RemoteAddress "100.64.0.0/10" `
    -Profile Any `
    -EdgeTraversalPolicy Block | Out-Null

Write-Output "Firewall allow rule created for TCP $Port on: $($names -join ', ')"
Write-Output "Remote addresses allowed by this rule: 100.64.0.0/10"
Write-Output "Windows Firewall profiles:"
Get-NetFirewallProfile | Select-Object Name, Enabled, DefaultInboundAction | Format-Table -AutoSize | Out-String | Write-Output
Write-Output "This rule is defense in depth. It is not, by itself, proof that the port is unreachable on the LAN."
Write-Output "Run status.ps1 after the server starts. The listen address must be the Tailscale IP, not 0.0.0.0."
