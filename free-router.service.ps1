# Register a Windows Task Scheduler task "FreeRouter" that starts the gateway
# on PC boot (AtStartup), hidden, under the current user, highest privileges.
# Run once:  powershell -ExecutionPolicy Bypass -File free-router.service.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
$py = (Get-Command python -ErrorAction Stop).Source
$router = Join-Path $here "router.py"
$taskName = "FreeRouter"

$action = New-ScheduledTaskAction `
    -Execute $py `
    -Argument "`"$router`"" `
    -WorkingDirectory $here

$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U `
    -RunLevel HighestAvailable

# remove old if exists
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force

# also kick it once now so it is live immediately
Start-ScheduledTask -TaskName $taskName
Write-Host "Registered and started task '$taskName'."
