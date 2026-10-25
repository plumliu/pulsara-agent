"""Read-only exact-byte application source/version check; no user settings or PG reads."""
import argparse,json,subprocess,datetime
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--resources',required=True);p.add_argument('--output',required=True);a=p.parse_args()
root=Path.cwd().resolve(); resources=Path(a.resources).resolve(strict=True); py=resources/'python/bin/python3'
paths=['conversation_kernel/provider_dispatch.py','hooks/executor.py','conversation_kernel/mcp/sdk_facade.py']
code='import json,importlib.metadata,pulsara_agent;print(json.dumps({"path":pulsara_agent.__file__,"runtime":pulsara_agent.__version__,"metadata":importlib.metadata.version("pulsara-agent")}))'
run=subprocess.run([str(py),'-I','-c',code],check=True,capture_output=True,text=True); observed=json.loads(run.stdout); package=Path(observed['path']).parent
assert package.is_relative_to(resources) and observed['runtime']==observed['metadata']=='0.1.0a10',observed
rows=[]
for name in paths:
 source=root/'src/pulsara_agent'/name; installed=package/name
 row={'relative':name,'source':str(source),'bundled':str(installed),'sourceBytes':source.stat().st_size,'bundledBytes':installed.stat().st_size,'exactBytesEqual':source.read_bytes()==installed.read_bytes()};assert row['exactBytesEqual'],row;rows.append(row)
result={'observedAt':datetime.datetime.now(datetime.timezone.utc).isoformat(),'head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'resourceRoot':str(resources),'version':observed,'threeProductionFiles':rows,'sourceByteChecksUseWSLExt4Clone':True,'noSourceFingerprints':True,'passed':True}
out=Path(a.output);assert not out.exists(),out;out.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
