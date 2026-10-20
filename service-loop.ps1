$ErrorActionPreference = "Continue"
$env:PHONEUPLOADS_APP_ROOT = $PSScriptRoot
. (Join-Path $PSScriptRoot "common.ps1")

$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "watchdog.log"

function Write-Watchdog {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date).ToString("o"), $Message
    if ((Test-Path -LiteralPath $log) -and ((Get-Item -LiteralPath $log).Length -gt 1000000)) {
        $old = "$log.1"
        if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Force }
        Move-Item -LiteralPath $log -Destination $old -Force
    }
    Add-Content -LiteralPath $log -Value $line -Encoding utf8
}

function Get-TailscaleIpv4 {
    $candidates = @()
    $command = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    $candidates += @(
        (Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Tailscale\tailscale.exe")
    )
    foreach ($exe in $candidates) {
        if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { continue }
        try {
            $output = & $exe ip -4 2>$null
        } catch {
            continue
        }
        foreach ($line in @($output)) {
            $ip = "$line".Trim()
            if ($ip -match '^100\.(6[4-9]|[7-9][0-9]|1[0-1][0-9]|12[0-7])\.\d{1,3}\.\d{1,3}$') {
                return $ip
            }
        }
    }
    return $null
}

$other = Get-WatchdogPid
if ($other -ne 0) {
    Write-Watchdog "Another watchdog is already running ($other). Exiting."
    exit 0
}

Write-Watchdog "Watchdog started."
while ($true) {
    try {
        if (Test-ManualStop) {
            Start-Sleep -Seconds 5
            continue
        }
        $serverPid = Get-ServerPid
        if ($serverPid -ne 0) {
            $info = Read-ListenInfo
            if ($info -and $info.mode -eq "tailscale") {
                $current = Get-TailscaleIpv4
                if ($current -and $info.host -and ($info.host -ne $current)) {
                    Write-Watchdog "Tailscale IP changed from $($info.host) to $current. Restarting the server."
                    Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Seconds 2
                    continue
                }
            }
            Start-Sleep -Seconds 10
            continue
        }
        Write-Watchdog "Starting server."
        $proc = Start-UploadServerProcess
        $proc.WaitForExit()
        Write-Watchdog "Server exited with code $($proc.ExitCode)."
        Start-Sleep -Seconds 5
    } catch {
        Write-Watchdog "Watchdog error: $($_.Exception.Message)"
        Start-Sleep -Seconds 10
    }
}
