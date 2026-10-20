param([switch]$Local)

$ErrorActionPreference = "Stop"
$env:PHONEUPLOADS_APP_ROOT = $PSScriptRoot
. (Join-Path $PSScriptRoot "common.ps1")

New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "data") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "logs") | Out-Null
$flag = Join-Path $PSScriptRoot "data\manual-stop.flag"
if (Test-Path -LiteralPath $flag) {
    Remove-Item -LiteralPath $flag -Force
}

$port = Get-AppPort
$existing = Get-ServerPid
if ($existing -ne 0) {
    Write-Output "Server is already running. PID $existing."
    $info = Read-ListenInfo
    if ($info) { Write-Output "Open $($info.url)" }
    exit 0
}

$proc = Start-UploadServerProcess -Local:$Local
$deadline = (Get-Date).AddSeconds(30)
$listeners = @()
do {
    Start-Sleep -Milliseconds 400
    if ($proc.HasExited) { break }
    $listeners = @(Get-Listeners -Port $port)
    if ($listeners.Count -gt 0) { break }
} while ((Get-Date) -lt $deadline)

if ($proc.HasExited -or $listeners.Count -eq 0) {
    Write-Output "The server stopped during startup."
    $log = Join-Path $PSScriptRoot "logs\server.log"
    if (Test-Path -LiteralPath $log) {
        Write-Output "Last lines from logs\server.log:"
        Get-Content -LiteralPath $log -Tail 40 -Encoding UTF8
    }
    exit 1
}

Write-Output "Server started. PID $($proc.Id)."
$info = Read-ListenInfo
if ($info) {
    Write-Output "Mode: $($info.mode)"
    Write-Output "Open: $($info.url)"
    if ($info.mode -eq "local") {
        Write-Output "WARNING: this is a local test. A phone cannot connect to 127.0.0.1."
        Write-Output "Run .\start.ps1 without -Local after Tailscale is connected."
    }
}
foreach ($listener in $listeners) {
    Write-Output ("Listening on {0}:{1}" -f $listener.Address, $listener.Port)
}
Write-Output "This window can be closed. Check with .\status.ps1 and stop with .\stop.ps1."
