# Installs Power Monitor: a background logger (SYSTEM scheduled task) plus a desktop window.
# Run Install.bat (it asks for administrator permission and runs this script).
#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'

$Src = $PSScriptRoot
$Dest = Join-Path $env:ProgramFiles 'PowerMonitor'
$LogDir = Join-Path $env:ProgramData 'PowerMonitor\logs'
$PS = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$TaskName = 'PowerMonitor'

Write-Host "Installing Power Monitor to $Dest ..."
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -match 'PowerMonitor(UI)?\.ps1' } |
    ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }

New-Item -ItemType Directory -Force -Path $Dest, $LogDir | Out-Null
foreach ($f in 'PowerMonitor.ps1', 'PowerMonitorUI.ps1', 'Uninstall.ps1', 'Uninstall.bat', 'README.txt') {
    Copy-Item -LiteralPath (Join-Path $Src $f) -Destination $Dest -Force
}
Get-ChildItem -LiteralPath $Dest | Unblock-File   # clear the "downloaded from the internet" mark

# Background logger: starts at boot as SYSTEM, restarted if it ever exits.
$action = New-ScheduledTaskAction -Execute $PS `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Dest\PowerMonitor.ps1`" -LogDir `"$LogDir`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description 'Power Monitor: logs power consumption to daily CSV files' | Out-Null
Start-ScheduledTask -TaskName $TaskName

# Window shortcuts: Desktop (all users), Start menu, and open at login.
$shell = New-Object -ComObject WScript.Shell
$shortcuts = @(
    (Join-Path $env:PUBLIC 'Desktop\Power Monitor.lnk'),
    (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\Power Monitor.lnk'),
    (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\StartUp\Power Monitor.lnk')
)
foreach ($path in $shortcuts) {
    $s = $shell.CreateShortcut($path)
    $s.TargetPath = $PS
    $s.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Dest\PowerMonitorUI.ps1`" -LogDir `"$LogDir`""
    $s.WorkingDirectory = $Dest
    $s.IconLocation = "$env:SystemRoot\System32\perfmon.exe,0"
    $s.Description = 'Current power draw and units consumed'
    $s.Save()
}

Write-Host 'Waiting for the first readings...'
Start-Sleep -Seconds 15
$today = Join-Path $LogDir ((Get-Date).ToString('yyyy-MM-dd') + '.csv')
$state = (Get-ScheduledTask -TaskName $TaskName).State
if ($state -eq 'Running' -and (Test-Path -LiteralPath $today)) {
    Write-Host ''
    Write-Host 'Power Monitor is running and starts automatically at every boot.' -ForegroundColor Green
    Write-Host "  Logs:    $LogDir"
    Write-Host "  Window:  'Power Monitor' on the Desktop and Start menu (also opens at login)"
    Write-Host ''
    Get-Content -LiteralPath $today -Tail 2
    Start-Process -FilePath $PS -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Dest\PowerMonitorUI.ps1`" -LogDir `"$LogDir`""
} else {
    Write-Host "The logger did not start correctly (task state: $state)." -ForegroundColor Red
    Write-Host "Check Task Scheduler > PowerMonitor > History, or run it by hand to see errors:"
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$Dest\PowerMonitor.ps1`" -LogDir `"$env:TEMP\pm-test`""
    exit 1
}
