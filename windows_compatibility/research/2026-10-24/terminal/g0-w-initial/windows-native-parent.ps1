param([string]$Record,[string]$Action)
$ErrorActionPreference = 'Stop'
function Write-ProbeEvent([string]$EventName, [int]$ChildPid = 0) {
  @{event=$EventName;pid=$PID;childPid=$ChildPid;time=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds();mode='native-dotnet';action=$Action} | ConvertTo-Json -Compress | Add-Content -LiteralPath $Record -Encoding UTF8
}
$probeDirectory = Split-Path -LiteralPath $Record
$nodeExecutable = Join-Path $probeDirectory 'node-pin.exe'
$childScript = Join-Path $probeDirectory 'windows-pe.cjs'
if ($nodeExecutable.Contains(' ') -or $childScript.Contains(' ') -or $Record.Contains(' ')) { throw 'This deliberately narrow probe requires paths without spaces' }
Write-ProbeEvent 'windows-native-parent-start'
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $nodeExecutable
$startInfo.Arguments = "$childScript child $Record native $Action"
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$child = [System.Diagnostics.Process]::Start($startInfo)
Write-ProbeEvent 'windows-native-parent-spawn' $child.Id
while (-not ([System.IO.File]::ReadAllText($Record).Contains('windows-child-start'))) { Start-Sleep -Milliseconds 10 }
Write-ProbeEvent 'windows-native-child-ready' $child.Id
if ($Action -ne 'rootexit') { if ($Action -eq 'nodecrash') { Start-Sleep -Seconds 16 } else { Start-Sleep -Seconds 8 }; Write-ProbeEvent 'windows-native-parent-finished' }
exit 0
