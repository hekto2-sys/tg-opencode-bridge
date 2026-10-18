# Registers the Telegram <-> OpenCode bridge to start when the user logs in.
$vbs = "C:\1C_dev\tg-opencode-bridge\start-bridge.vbs"
if (-not (Test-Path $vbs)) { throw "missing $vbs" }

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "OpenCode Telegram Bridge" `
    -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Telegram bridge to OpenCode" -Force | Out-Null

Get-ScheduledTask -TaskName "OpenCode Telegram Bridge" | Select-Object TaskName, State | Format-Table -AutoSize | Out-String
