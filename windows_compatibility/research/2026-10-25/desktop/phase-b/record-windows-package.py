"""Record the completed local Windows build and its real payload boundary."""
import hashlib
import json
from pathlib import Path
import subprocess

root = Path.cwd()
output = root/'output/windows-b/host-setup'
release = root/'desktop/release'
version = json.loads((root/'desktop/package.json').read_text())['version']
files = [release/f'Pulsara-{version}-windows-x64.exe',release/'win-unpacked/pulsara.exe',release/'win-unpacked/resources/wsl-payload/payload.tar',release/'win-unpacked/resources/wsl-payload/manifest.json',release/'win-unpacked/resources/windows-interop/pulsara-interop.exe']
assert all(path.is_file() and path.stat().st_size > 0 for path in files)
manifest = json.loads(files[3].read_text())
with files[2].open('rb') as stream:
 digest = hashlib.file_digest(stream,'sha256').hexdigest()
assert manifest['sha256'] == digest
assert manifest == json.loads((root/'desktop/resources/wsl-payload/manifest.json').read_text())
helper = subprocess.run([str(files[-1]),'--version'],check=True,capture_output=True,text=True)
assert helper.stdout == 'pulsara-interop 1\n'
result = {'exitCode':0,'command':'npm --prefix desktop run package -- --win --x64 --publish never','files':[{'path':str(path),'bytes':path.stat().st_size} for path in files],'manifest':manifest,'packagedTarDigestVerified':True,'packagedHelper':{'stdout':helper.stdout,'exitCode':helper.returncode},'nsisCompiled':True,'installerRunByThisAgent':False}
(output/'windows-package-results.json').write_text(json.dumps(result,indent=2)+'\n')
record = output/'build-stage.json'
stage = json.loads(record.read_text())
stage.update(phase='final-windows-nsis-and-unpacked-built',manifestWithheldFromApp=False,packageResult='windows-package-results.json')
record.write_text(json.dumps(stage,indent=2)+'\n')
print(json.dumps(result,indent=2))
