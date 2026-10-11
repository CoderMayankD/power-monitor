# Removes Power Monitor. Logs in C:\ProgramData\PowerMonitor\logs are kept.
#Requires -RunAsAdministrator
$TaskName = 'PowerMonitor'
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
    Where-Object { $_.CommandLine -match 'PowerMonitor(UI)?\.ps1' } |
    ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }
Remove-Item -Force -ErrorAction SilentlyContinue -LiteralPath @(
    (Join-Path $env:PUBLIC 'Desktop\Power Monitor.lnk'),
    (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\Power Monitor.lnk'),
    (Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\StartUp\Power Monitor.lnk')
)
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue -LiteralPath (Join-Path $env:ProgramFiles 'PowerMonitor')
Write-Host "Power Monitor removed. Logs kept in $(Join-Path $env:ProgramData 'PowerMonitor\logs')"
