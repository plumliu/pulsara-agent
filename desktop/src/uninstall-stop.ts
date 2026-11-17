import {execFile} from 'node:child_process';
import {promisify} from 'node:util';

/** Explicit uninstall only. Windows owns handles, exit waits and termination. */
export async function stopInstalledDesktop(): Promise<void> {
  const script = `
$ErrorActionPreference = 'Stop'
$target = $env:PULSARA_UNINSTALL_EXECUTABLE
$self = [int]$env:PULSARA_UNINSTALL_PID
$processes = @(Get-Process | Where-Object { $_.Path -eq $target -and $_.Id -ne $self } | ForEach-Object {
  # Retain the actual Windows process handle across the graceful stop request.
  $candidate = $_
  try { $null = $candidate.Handle; $candidate } catch { if (!$candidate.HasExited) { throw } }
})
if ($processes.Count -eq 0) { exit 0 }
# Ask the actual singleton owner to use its ordinary shutdown path.
$request = Start-Process -FilePath $target -ArgumentList '--pulsara-uninstall-stop' -PassThru
if (!$request.WaitForExit(30000)) { $request.Kill(); throw 'Shutdown request did not finish' }
foreach ($process in $processes) {
  if (!$process.WaitForExit(30000)) {
    $process.Kill()
    if (!$process.WaitForExit(30000)) { throw 'Pulsara did not exit' }
  }
}
`;
  await promisify(execFile)('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
    env:{...process.env, PULSARA_UNINSTALL_EXECUTABLE:process.execPath, PULSARA_UNINSTALL_PID:String(process.pid)},
    windowsHide:true,
  });
}
