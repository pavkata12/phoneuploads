# Shared helpers for the phone upload server scripts.
# Callers set PHONEUPLOADS_APP_ROOT to their own folder before dot-sourcing.

function Get-AppRoot {
    if ($env:PHONEUPLOADS_APP_ROOT) {
        return $env:PHONEUPLOADS_APP_ROOT
    }
    return (Split-Path -Parent $PSCommandPath)
}

function Get-VenvPython {
    $python = Join-Path (Get-AppRoot) ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Python virtual environment is missing. Run install.ps1 first."
    }
    return $python
}

function Get-VenvPythonw {
    $pythonw = Join-Path (Get-AppRoot) ".venv\Scripts\pythonw.exe"
    if (Test-Path -LiteralPath $pythonw) {
        return $pythonw
    }
    return Get-VenvPython
}

function Get-AppPort {
    $envFile = Join-Path (Get-AppRoot) ".env"
    if (-not (Test-Path -LiteralPath $envFile)) {
        return 8787
    }
    $text = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($envFile))
    if ($text.Length -gt 0 -and [int]$text[0] -eq 0xFEFF) {
        $text = $text.Substring(1)
    }
    foreach ($line in ($text -split "`r?`n")) {
        if ($line -match '^\s*PORT\s*=\s*"?(\d+)') {
            return [int]$Matches[1]
        }
    }
    return 8787
}

function Test-ProcessAlive {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return $false }
    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Get-ServerPid {
    $pidFile = Join-Path (Get-AppRoot) "data\server.pid"
    if (Test-Path -LiteralPath $pidFile) {
        $raw = ([System.IO.File]::ReadAllText($pidFile)).Trim()
        $parsed = 0
        if ([int]::TryParse($raw, [ref]$parsed) -and (Test-ProcessAlive $parsed)) {
            return $parsed
        }
    }
    $rootPattern = "*$(Get-AppRoot)*app.py*"
    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        ($_.Name -eq "python.exe" -or $_.Name -eq "pythonw.exe") -and $_.CommandLine -like $rootPattern
    }
    foreach ($proc in $processes) {
        return [int]$proc.ProcessId
    }
    return 0
}

function Get-WatchdogPid {
    $rootPattern = "*$(Get-AppRoot)*service-loop.ps1*"
    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -eq "powershell.exe" -and $_.CommandLine -like $rootPattern
    }
    foreach ($proc in $processes) {
        if ([int]$proc.ProcessId -ne $PID) {
            return [int]$proc.ProcessId
        }
    }
    return 0
}

function Get-Listeners {
    param([int]$Port)
    $properties = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()
    return @($properties.GetActiveTcpListeners() | Where-Object { $_.Port -eq $Port })
}

function Test-TcpOpen {
    param(
        [string]$TargetHost,
        [int]$Port,
        [int]$TimeoutMs = 800
    )
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $result = $client.BeginConnect($TargetHost, $Port, $null, $null)
        $ok = $result.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
        if (-not $ok) { return $false }
        $client.EndConnect($result) | Out-Null
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Read-ListenInfo {
    $path = Join-Path (Get-AppRoot) "data\listen.json"
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $text = [System.Text.Encoding]::UTF8.GetString([System.IO.File]::ReadAllBytes($path))
    if ($text.Length -gt 0 -and [int]$text[0] -eq 0xFEFF) { $text = $text.Substring(1) }
    return $text | ConvertFrom-Json
}

function Test-ManualStop {
    $flag = Join-Path (Get-AppRoot) "data\manual-stop.flag"
    if (-not (Test-Path -LiteralPath $flag)) { return $false }
    $boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
    $written = (Get-Item -LiteralPath $flag).LastWriteTime
    if ($written -lt $boot) {
        Remove-Item -LiteralPath $flag -Force
        return $false
    }
    return $true
}

function Start-UploadServerProcess {
    param([switch]$Local)
    $python = Get-VenvPythonw
    $app = Join-Path (Get-AppRoot) "app.py"
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $python
    $startInfo.Arguments = '"' + $app + '"'
    $startInfo.WorkingDirectory = Get-AppRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.EnvironmentVariables["PYTHONUTF8"] = "1"
    $startInfo.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    if ($Local) {
        $startInfo.EnvironmentVariables["HOST"] = "127.0.0.1"
    } elseif ($startInfo.EnvironmentVariables.ContainsKey("HOST")) {
        $startInfo.EnvironmentVariables.Remove("HOST")
    }
    return [System.Diagnostics.Process]::Start($startInfo)
}
