$ErrorActionPreference = "Continue"
$env:PHONEUPLOADS_APP_ROOT = $PSScriptRoot
. (Join-Path $PSScriptRoot "common.ps1")

function Get-AddressKind {
    param([string]$Address)
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($Address, [ref]$parsed)) { return "unknown" }
    if ($parsed.Equals([System.Net.IPAddress]::Any) -or $parsed.Equals([System.Net.IPAddress]::IPv6Any)) {
        return "all-interfaces"
    }
    $text = $parsed.ToString()
    if ($text -eq "127.0.0.1" -or $text -eq "::1") { return "local" }
    if ($parsed.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork) {
        $bytes = $parsed.GetAddressBytes()
        if ($bytes[0] -eq 100 -and $bytes[1] -ge 64 -and $bytes[1] -le 127) { return "tailscale" }
        if ($bytes[0] -eq 10) { return "lan" }
        if ($bytes[0] -eq 192 -and $bytes[1] -eq 168) { return "lan" }
        if ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) { return "lan" }
        if ($bytes[0] -eq 169 -and $bytes[1] -eq 254) { return "lan" }
    }
    return "other"
}

$unsafe = $false
$running = $false
$port = 8787
try { $port = Get-AppPort } catch { Write-Output "Could not read the port from .env. Assuming 8787." }

Write-Output "=== Phone upload server ==="
$venv = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venv)) {
    Write-Output "NOT INSTALLED. Run install.ps1."
    exit 1
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".env"))) {
    Write-Output "Configuration file .env is missing. Run install.ps1."
}

$serverPid = 0
try { $serverPid = Get-ServerPid } catch { $serverPid = 0 }
$watchdog = 0
try { $watchdog = Get-WatchdogPid } catch { $watchdog = 0 }
$manualStop = $false
try { $manualStop = Test-ManualStop } catch { $manualStop = $false }

if ($serverPid -ne 0) {
    $running = $true
    Write-Output "Process: running, PID $serverPid"
} else {
    Write-Output "Process: stopped"
}
if ($watchdog -ne 0) {
    Write-Output "Watchdog: running, PID $watchdog"
} else {
    Write-Output "Watchdog: not running. Crash recovery is active only after register-task.ps1."
}
if ($manualStop) {
    Write-Output "Manual stop: yes. It stays stopped until start.ps1 or the next reboot."
}

$info = Read-ListenInfo
if ($info) {
    Write-Output "Recorded URL: $($info.url)"
    Write-Output "Recorded mode: $($info.mode)"
    Write-Output "Storage: $($info.storage_dir)"
}

Write-Output ""
Write-Output "=== Listen address ==="
$listeners = @(Get-Listeners -Port $port)
if ($listeners.Count -eq 0) {
    Write-Output "Nothing is listening on port $port."
} else {
    foreach ($listener in $listeners) {
        $kind = Get-AddressKind $listener.Address.ToString()
        Write-Output ("{0}:{1}  kind={2}" -f $listener.Address, $listener.Port, $kind)
        if ($kind -eq "all-interfaces" -or $kind -eq "lan" -or $kind -eq "other") { $unsafe = $true }
    }
}

Write-Output ""
Write-Output "=== Connection probes from this PC ==="
$localOpen = Test-TcpOpen -TargetHost "127.0.0.1" -Port $port
Write-Output ("127.0.0.1:{0}  {1}" -f $port, $(if ($localOpen) { "open" } else { "closed" }))
if ($info -and $info.mode -eq "tailscale" -and $localOpen) { $unsafe = $true }

try {
    $lanAddresses = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop | Where-Object {
            $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "100.*" -and $_.PrefixOrigin -ne "WellKnown"
        } | Select-Object -ExpandProperty IPAddress -Unique)
} catch {
    $lanAddresses = @()
    Write-Output "Could not list LAN addresses: $($_.Exception.Message)"
}
foreach ($lan in $lanAddresses) {
    $open = Test-TcpOpen -TargetHost $lan -Port $port
    Write-Output ("{0}:{1}  {2}" -f $lan, $port, $(if ($open) { "OPEN - unexpected" } else { "closed" }))
    if ($open) { $unsafe = $true }
}

if ($info -and $info.url -and $running) {
    try {
        $health = Invoke-WebRequest -Uri ($info.url.TrimEnd("/") + "/health") -UseBasicParsing -TimeoutSec 5
        Write-Output "Health $($info.url)health -> HTTP $($health.StatusCode) $($health.Content)"
    } catch {
        Write-Output "Health request failed: $($_.Exception.Message)"
        $unsafe = $true
    }
}

Write-Output ""
Write-Output "=== Windows Firewall ==="
Write-Output "A firewall rule alone is not sufficient. The safe result is a listen address on the Tailscale IP,"
Write-Output "with 127.0.0.1 and the LAN addresses closed. Binding is what keeps those addresses closed."
try {
    Get-NetFirewallProfile -ErrorAction Stop | ForEach-Object {
        Write-Output ("Profile {0}: enabled={1}; defaultInbound={2}" -f $_.Name, $_.Enabled, $_.DefaultInboundAction)
        if (-not $_.Enabled) {
            Write-Output "WARNING: this firewall profile is disabled. Do not turn the firewall off. The bind restriction still applies."
        }
    }
    $rule = Get-NetFirewallRule -DisplayName "PhoneUploads (Tailscale only)" -ErrorAction SilentlyContinue
    if (-not $rule) {
        Write-Output "Project firewall rule: missing. Run install.ps1 or set-firewall.ps1 from an elevated PowerShell."
    } else {
        Write-Output "Project firewall rule: present"
        Get-NetFirewallPortFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Output ("  protocol={0} localPort={1}" -f $_.Protocol, $_.LocalPort) }
        Get-NetFirewallAddressFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Output ("  remoteAddress={0}" -f ($_.RemoteAddress -join ",")) }
        Get-NetFirewallInterfaceFilter -AssociatedNetFirewallRule $rule -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Output ("  interface={0}" -f ($_.InterfaceAlias -join ",")) }
    }
} catch {
    Write-Output "Could not read firewall configuration: $($_.Exception.Message)"
}

$log = Join-Path $PSScriptRoot "logs\server.log"
if (Test-Path -LiteralPath $log) {
    Write-Output ""
    Write-Output "=== Last log lines ==="
    Get-Content -LiteralPath $log -Tail 15 -Encoding UTF8
}

Write-Output ""
if (-not $running) {
    Write-Output "RESULT: server is not running."
    exit 1
}
if ($unsafe) {
    Write-Output "RESULT: UNSAFE. The server is reachable outside the intended Tailscale or local-test address."
    exit 2
}
if ($info -and $info.mode -eq "local") {
    Write-Output "RESULT: running in local-test mode. Safe on this PC, not reachable from the phone."
    exit 0
}
Write-Output "RESULT: running, and the listen address is restricted. Confirm the address above is your Tailscale IP."
exit 0
