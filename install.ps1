param(
    [string]$StoragePath,
    [int]$Port = 8787,
    [switch]$ResetPassword,
    [switch]$SkipFirewall,
    [switch]$RegisterTask
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location -LiteralPath $Root

function Test-RealPython {
    param([string]$Path)
    if (-not $Path -or $Path -like "*\WindowsApps\*") { return $false }
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    & $Path -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
    return $LASTEXITCODE -eq 0
}

function Find-Python {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe")
    )
    foreach ($name in @("py", "python")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source -notlike "*\WindowsApps\*") {
            if ($name -eq "py") {
                $printed = & py -3 -c "import sys; print(sys.executable)" 2>$null
                if ($printed) { $candidates = @($printed.Trim()) + $candidates }
            } else {
                $candidates = @($command.Source) + $candidates
            }
        }
    }
    foreach ($candidate in $candidates) {
        if (Test-RealPython $candidate) { return $candidate }
    }
    return $null
}

function Convert-SecureToPlain {
    param([Security.SecureString]$Secure)
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

Write-Output "Phone upload server setup"
Write-Output "Folder: $Root"

$python = Find-Python
if (-not $python) {
    Write-Output "Python 3.10 or newer was not found."
    $answer = Read-Host "Install Python 3.12 for the current user with winget? (y/n)"
    if ($answer -notmatch '^(y|yes)$') {
        throw "Install Python 3.12 and run install.ps1 again."
    }
    winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
    $python = Find-Python
    if (-not $python) {
        throw "Python was installed but was not found. Close this window, open a new PowerShell, and run install.ps1 again."
    }
}
Write-Output "Using Python: $python"

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python -m venv (Join-Path $Root ".venv")
}
& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

if (-not $StoragePath) {
    $defaultStorage = if (Test-Path -LiteralPath "D:\") { "D:\PhoneUploads" } else { Join-Path $env:USERPROFILE "PhoneUploads" }
    $entered = Read-Host "Folder for uploaded files [$defaultStorage]"
    $StoragePath = if ([string]::IsNullOrWhiteSpace($entered)) { $defaultStorage } else { $entered }
}
if ($Port -lt 1024 -or $Port -gt 65535) { throw "Port must be between 1024 and 65535." }

$passwordFile = Join-Path $Root "data\password.hash"
$needPassword = $ResetPassword -or -not (Test-Path -LiteralPath $passwordFile)
$arguments = @((Join-Path $Root "setup_config.py"), "--storage", $StoragePath, "--port", "$Port")
if ($needPassword) {
    while ($true) {
        $first = Read-Host "Password (at least 12 characters, with a letter and a digit)" -AsSecureString
        $second = Read-Host "Repeat password" -AsSecureString
        $plain = Convert-SecureToPlain $first
        $repeat = Convert-SecureToPlain $second
        if ($plain -ne $repeat) {
            Write-Output "The passwords did not match."
            continue
        }
        break
    }
    $env:PHONEUPLOADS_NEW_PASSWORD = $plain
    $plain = $null
    $repeat = $null
} else {
    $arguments += "--keep-secrets"
    Write-Output "Keeping the existing password. Pass -ResetPassword to change it."
}

try {
    & $venvPython @arguments
    if ($LASTEXITCODE -ne 0) { throw "Configuration was not saved." }
} finally {
    Remove-Item Env:PHONEUPLOADS_NEW_PASSWORD -ErrorAction SilentlyContinue
}

if (-not $SkipFirewall) {
    $firewall = Join-Path $Root "set-firewall.ps1"
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    if ($isAdmin) {
        & $firewall -Port $Port
    } else {
        Write-Output "Windows will ask for administrator approval to add a Tailscale-only firewall rule."
        $command = "-NoProfile -ExecutionPolicy Bypass -File `"$firewall`" -Port $Port"
        $elevated = Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -PassThru -ArgumentList $command
        if (-not $elevated -or $elevated.ExitCode -ne 0) {
            Write-Output "Firewall rule was not added. The server still refuses to listen on the LAN."
            Write-Output "You can run set-firewall.ps1 later from an elevated PowerShell window."
        }
    }
}

if ($RegisterTask) {
    & (Join-Path $Root "register-task.ps1")
} else {
    $taskAnswer = Read-Host "Start the server automatically after you sign in to Windows? (y/n)"
    if ($taskAnswer -match '^(y|yes)$') {
        & (Join-Path $Root "register-task.ps1")
    }
}

Write-Output ""
Write-Output "Setup finished."
Write-Output "Uploads will be stored in: $StoragePath"
Write-Output "Next: connect Tailscale, then run .\start.ps1"
Write-Output "For a test on this PC only, run .\start.ps1 -Local"
Write-Output "Check exposure with .\status.ps1"
