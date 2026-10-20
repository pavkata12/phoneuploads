$ErrorActionPreference = "Stop"
$taskName = "PhoneUploadServer"
$loop = Join-Path $PSScriptRoot "service-loop.ps1"
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot ".venv\Scripts\pythonw.exe"))) {
    throw "Run install.ps1 before registering the scheduled task."
}

$argument = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$loop`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = "PT45S"
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force `
    -Description "Starts the personal phone upload server after sign-in and restarts it if it exits." | Out-Null

Write-Output "Scheduled task '$taskName' is registered for user $env:USERNAME."
Write-Output "It runs at sign-in, waits 45 seconds, and does not open a window."
Write-Output "The laptop user has to be signed in. This task does not run at the lock screen before sign-in."
Write-Output "Remove it with .\unregister-task.ps1."
