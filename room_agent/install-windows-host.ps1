[CmdletBinding()]
param(
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$taskName = "Room Agent iPhone Host"
$scriptDirectory = $PSScriptRoot
$hostScript = Join-Path $scriptDirectory "windows_iphone_host.py"
$venvDirectory = Join-Path $scriptDirectory ".venv"
$pythonw = Join-Path $venvDirectory "Scripts\pythonw.exe"
$python = Join-Path $venvDirectory "Scripts\python.exe"
$requirements = Join-Path $scriptDirectory "requirements-windows.txt"
$startupDirectory = [Environment]::GetFolderPath("Startup")
$startupLauncher = Join-Path $startupDirectory "RoomAgentIphoneHost.lnk"
$legacyStartupLauncher = Join-Path $startupDirectory "RoomAgentIphoneHost.vbs"

function Test-RoomAgentHost {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8770/health" -TimeoutSec 2
        return $health.status -eq "ok"
    } catch {
        return $false
    }
}

if ($Remove) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $startupLauncher -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $legacyStartupLauncher -Force -ErrorAction SilentlyContinue
    Write-Host "Removed the Room Agent startup task. Enrollments and audit data were preserved."
    exit 0
}

if (-not (Test-Path -LiteralPath $hostScript)) {
    throw "windows_iphone_host.py is missing from $scriptDirectory"
}

if (-not (Test-Path -LiteralPath $pythonw)) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -eq $launcher) {
        throw "Python Launcher was not found. Install Python 3.11 or newer first."
    }
    & $launcher.Source -3 -m venv $venvDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "The isolated Room Agent Python environment could not be created."
    }
}

if (-not (Test-Path -LiteralPath $requirements)) {
    throw "requirements-windows.txt is missing from $scriptDirectory"
}
& $python -m pip install --disable-pip-version-check --quiet -r $requirements
if ($LASTEXITCODE -ne 0) {
    throw "The Room Agent Windows dependencies could not be installed."
}

$arguments = '"{0}" --treat-tv-as-local-display --personal-browser-profile' -f $hostScript
$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument $arguments `
    -WorkingDirectory $scriptDirectory
$trigger = New-ScheduledTaskTrigger -AtLogOn
$principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$installMethod = "Scheduled Task"
$hostWasRunning = Test-RoomAgentHost
try {
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Private iPhone Action Button gateway for Room Agent" `
        -Force `
        -ErrorAction Stop | Out-Null
    Remove-Item -LiteralPath $startupLauncher -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $legacyStartupLauncher -Force -ErrorAction SilentlyContinue
    if (-not $hostWasRunning) {
        Start-ScheduledTask -TaskName $taskName
    }
} catch [Microsoft.Management.Infrastructure.CimException] {
    # Managed corporate Windows installations often deny Task Scheduler writes
    # to standard users. The per-user Startup folder needs no elevation and is
    # appropriate because the browser host must run in the interactive session.
    $installMethod = "per-user Startup launcher"
    Remove-Item -LiteralPath $legacyStartupLauncher -Force -ErrorAction SilentlyContinue
    $shortcutShell = New-Object -ComObject WScript.Shell
    $shortcut = $shortcutShell.CreateShortcut($startupLauncher)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = $arguments
    $shortcut.WorkingDirectory = $scriptDirectory
    $shortcut.WindowStyle = 7
    $shortcut.Description = "Private iPhone Action Button gateway for Room Agent"
    $shortcut.Save()
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcutShell)
    if (-not $hostWasRunning) {
        Start-Process `
            -FilePath $pythonw `
            -ArgumentList $arguments `
            -WorkingDirectory $scriptDirectory `
            -WindowStyle Hidden
    }
}
if (-not $hostWasRunning) {
    Start-Sleep -Seconds 2
}

try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:8770/health" -TimeoutSec 5
    if ($health.status -ne "ok") {
        throw "The health response was unexpected."
    }
} catch {
    $task = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
    $taskResult = if ($null -ne $task) { $task.LastTaskResult } else { "unavailable" }
    throw "Startup was installed but the gateway did not become healthy. LastTaskResult=$taskResult. $($_.Exception.Message)"
}

Write-Host "Room Agent iPhone Host is installed and healthy via $installMethod."
if ($hostWasRunning) {
    Write-Host "An existing host was left running. Sign out or restart it once to load newly installed code."
}
Write-Host "It will start automatically when this Windows user signs in."
