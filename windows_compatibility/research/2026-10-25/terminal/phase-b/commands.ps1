# Run at the repository root. Uses the project's local uv-managed environment.
$repoRoot = (Get-Location).Path
$evidenceRoot = Join-Path $repoRoot 'windows_compatibility/research/2026-10-25/terminal/phase-b'
$helperDirectory = Join-Path $repoRoot 'output/phase-b/terminal/helper'
New-Item -ItemType Directory -Path $helperDirectory -Force | Out-Null
$helperExe = Join-Path $helperDirectory 'pulsara-interop.exe'
$childExe = Join-Path $helperDirectory 'interop-child.exe'
$compiler = Join-Path $env:SystemRoot 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
& $compiler /nologo /platform:x64 /target:exe /r:System.Runtime.Serialization.dll "/out:$helperExe" (Join-Path $repoRoot 'desktop/native/windows-interop-helper/Program.cs')
if ($LASTEXITCODE -ne 0) { throw 'helper build failed' }
& $compiler /nologo /platform:x64 /target:exe "/out:$childExe" (Join-Path $repoRoot 'tests/fixtures/windows_interop_child.cs')
if ($LASTEXITCODE -ne 0) { throw 'fixture build failed' }
& .venv/Scripts/python.exe "$evidenceRoot/scripts/native_control_check.py" $helperExe
& .venv/Scripts/python.exe "$evidenceRoot/scripts/verify_owners.py" --candidate-only
& .venv/Scripts/python.exe "$evidenceRoot/scripts/verify_interop.py"

# The historical negative runners use Git HEAD as the original module source.
# Their archived results were captured at 9e1150b7 before production edits.
# Do not rerun these expecting a failure once HEAD contains the fixes:
# .venv/Scripts/python.exe .../scripts/verify_posix.py
# .venv/Scripts/python.exe .../scripts/verify_output_baseline.py
# .venv/Scripts/python.exe .../scripts/verify_supervisor_baseline.py
