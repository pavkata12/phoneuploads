$ErrorActionPreference = "Stop"
$env:PHONEUPLOADS_APP_ROOT = $PSScriptRoot
. (Join-Path $PSScriptRoot "common.ps1")

$data = Join-Path $PSScriptRoot "data"
New-Item -ItemType Directory -Force -Path $data | Out-Null
$flag = Join-Path $data "manual-stop.flag"
Set-Content -LiteralPath $flag -Value (Get-Date).ToString("o") -Encoding ascii

$stopped = $false
$serverPid = Get-ServerPid
if ($serverPid -ne 0) {
    Stop-Process -Id $serverPid -Force
    $stopped = $true
    Write-Output "Stopped server PID $serverPid."
}

$pattern = "*$PSScriptRoot*app.py*"
$extras = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    ($_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe") -and $_.CommandLine -like $pattern
}
foreach ($proc in @($extras)) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    $stopped = $true
    Write-Output "Stopped server PID $($proc.ProcessId)."
}

Remove-Item -LiteralPath (Join-Path $data "server.pid") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $data "listen.json") -Force -ErrorAction SilentlyContinue
if (-not $stopped) {
    Write-Output "Server was not running."
}
Write-Output "Manual stop will last until you run start.ps1 or until the next reboot, if the scheduled task is installed."
Write-Output "Completed files already on disk are left untouched."
