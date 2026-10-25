$ErrorActionPreference='Stop'
$items=@(Get-CimInstance Win32_Process -Filter "name='pulsara.exe'" | Where-Object {$_.ExecutablePath -ieq 'C:\Users\plumlocal\AppData\Local\Programs\pulsara\pulsara.exe'} | ForEach-Object {[pscustomobject]@{pid=$_.ProcessId;parent=$_.ParentProcessId;auxiliary=[bool]($_.CommandLine -match '--type=');maintenance=[bool]($_.CommandLine -match '--pulsara-maintenance-check');creation=[string]$_.CreationDate.ToUniversalTime().ToFileTimeUtc()}})
$ids=@($items | ForEach-Object {$_.pid})
$tcp=@(Get-NetTCPConnection -ErrorAction SilentlyContinue | Where-Object {$ids -contains $_.OwningProcess -and ($_.RemotePort -eq 40363 -or $_.LocalPort -eq 40363) -and ($_.RemoteAddress -eq '127.0.0.1' -or $_.LocalAddress -eq '127.0.0.1')} | Select-Object OwningProcess,LocalAddress,LocalPort,RemoteAddress,RemotePort,State)
$last=(Get-WinEvent -LogName System -MaxEvents 1).RecordId
$power=@()
if (123 -gt 0) {
 $query="*[System[(EventRecordID > 123) and (Provider[@Name='Microsoft-Windows-Kernel-Power'] or Provider[@Name='Microsoft-Windows-Power-Troubleshooter']) and (EventID=1 or EventID=42 or EventID=107 or EventID=506 or EventID=507)]]"
 $power=@(Get-WinEvent -LogName System -FilterXPath $query -ErrorAction SilentlyContinue | ForEach-Object {[pscustomobject]@{id=$_.Id;recordId=$_.RecordId;provider=$_.ProviderName;utc=$_.TimeCreated.ToUniversalTime().ToString('o')}})
}
[pscustomobject]@{processes=$items;loopback=$tcp;eventBookmark=$last;powerEvents=$power} | ConvertTo-Json -Depth 5 -Compress
