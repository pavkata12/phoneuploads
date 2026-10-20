$ErrorActionPreference = "Stop"
$taskName = "PhoneUploadServer"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Output "Removed scheduled task '$taskName'."
} else {
    Write-Output "Scheduled task '$taskName' was not registered."
}

$pattern = "*$PSScriptRoot*service-loop.ps1*"
$watchdogs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -eq "powershell.exe" -and $_.CommandLine -like $pattern
}
foreach ($proc in @($watchdogs)) {
    Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Output "Stopped watchdog PID $($proc.ProcessId)."
}
Write-Output "The upload server process, if it is running, was not stopped. Use .\stop.ps1 for that."
