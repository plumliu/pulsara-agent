"""Inspect final Windows artifacts without installing or starting Pulsara."""
import datetime,hashlib,json,subprocess,tarfile
from pathlib import Path
root=Path.cwd(); out=root/'output/windows-shared-recheck/desktop'
package=json.loads((out/'windows-package.json').read_text()); assert package['exitCode']==0
version=json.loads((root/'desktop/package.json').read_text())['version']; assert version=='0.1.0-alpha.10'
release=root/'desktop/release'; unpacked=release/'win-unpacked/resources'; archive=unpacked/'wsl-payload/payload.tar'; manifest=json.loads((unpacked/'wsl-payload/manifest.json').read_text())
assert manifest==json.loads((root/'desktop/resources/wsl-payload/manifest.json').read_text()) and manifest['version']==version
with archive.open('rb') as stream: digest=hashlib.file_digest(stream,'sha256').hexdigest()
assert digest==manifest['sha256']
relative=['conversation_kernel/provider_dispatch.py','hooks/executor.py','conversation_kernel/mcp/sdk_facade.py']; rows=[]
wslroot=Path('//wsl.localhost/Ubuntu/home/plumwsl/workspaces/pulsara_agent')
with tarfile.open(archive) as tar:
 for name in relative:
  member='payload/python/lib/python3.12/site-packages/pulsara_agent/'+name
  data=tar.extractfile(member).read(); source=(wslroot/'src/pulsara_agent'/name).read_bytes(); native=(root/'src/pulsara_agent'/name).read_bytes()
  row={'relative':name,'tarMember':member,'bytes':len(data),'exactBytesEqualWSLExt4Source':data==source,'windowsCheckoutEqualAfterCRLF':data==native.replace(b'\r\n',b'\n')}; assert row['exactBytesEqualWSLExt4Source'] and row['windowsCheckoutEqualAfterCRLF'],row; rows.append(row)
 launch=json.load(tar.extractfile('payload/desktop-launch.json')); assert launch=={'execution_domain':'wsl'}
 static=tar.extractfile('payload/python/lib/python3.12/site-packages/pulsara_agent/desktop_app/static/index.html').read(); assert static==(wslroot/'desktop/resources/python/lib/python3.12/site-packages/pulsara_agent/desktop_app/static/index.html').read_bytes()
helper=unpacked/'windows-interop/pulsara-interop.exe'; run=subprocess.run([str(helper),'--version'],capture_output=True,check=True);assert run.stdout==b'pulsara-interop 1\r\n' or run.stdout==b'pulsara-interop 1\n'
files=[release/f'Pulsara-{version}-windows-x64.exe',release/f'Pulsara-{version}-windows-x64.exe.blockmap',release/'win-unpacked/pulsara.exe',archive,unpacked/'wsl-payload/manifest.json',helper]
assert all(f.is_file() and f.stat().st_size>0 for f in files)
result={'observedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'version':version,'packageExitCode':package['exitCode'],'manifest':manifest,'packagedTarDigestVerified':True,'threeProductionFiles':rows,'packagedStaticEqualFreshLinuxResources':True,'launchManifest':launch,'helperVersionStdout':run.stdout.decode().replace('\r\n','\n'),'files':[{'path':str(f),'bytes':f.stat().st_size,'mtimeUtc':datetime.datetime.fromtimestamp(f.stat().st_mtime,datetime.timezone.utc).isoformat()} for f in files],'installerNotRunByThisAgent':True,'passed':True}
(out/'artifact-provenance.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
