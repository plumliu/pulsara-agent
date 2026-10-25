import json,subprocess,datetime
from pathlib import Path
out=Path(__file__).resolve().parent
paths=set();linux=[];windows=[]
for name in ['network-first-results.json','network-results.json']:
 d=json.loads((out/name).read_text());r=d.get('ready');
 if r:paths.add(r['home']);linux.append({'pid':r['pid'],'start':r['starttime']})
 windows.append(d['windowsPid'])
for p in out.glob('enospc*results.json'):
 d=json.loads(p.read_text());
 if d.get('privateRoot'):paths.add(d['privateRoot'])
 if d.get('version'):paths.add('/home/plumwsl/.local/share/pulsara/runtime/'+d['version'])
payload={'paths':sorted(paths),'linux':linux}
code='''import json,os,sys
from pathlib import Path
x=json.loads(sys.argv[1]);missing=[dict(path=p,exists=Path(p).exists()) for p in x['paths']];proc=[]
for r in x['linux']:
 p=Path('/proc')/str(r['pid'])
 actual=p.joinpath('stat').read_text().rsplit(')',1)[1].split()[19] if p.exists() else None
 proc.append(dict(**r,currentStart=actual,sameIdentityLive=actual==r['start']))
stages=list(map(str,(Path.home()/'.local/share/pulsara/runtime').glob('.install-*')))
assert all(not r['exists'] for r in missing) and all(not r['sameIdentityLive'] for r in proc) and not stages
print(json.dumps(dict(paths=missing,processes=proc,installStages=stages,passed=True)))'''
p=subprocess.run(['wsl.exe','-d','Ubuntu','-u','plumwsl','--cd','/home/plumwsl','--exec','/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/python/bin/python3','-I','-c',code,json.dumps(payload)],capture_output=True,text=True);assert p.returncode==0,(p.stdout,p.stderr)
result=json.loads(p.stdout);result['windowsPids']=windows;result['observedAt']=datetime.datetime.now(datetime.timezone.utc).isoformat();(out/'cleanup-linux.json').write_text(json.dumps(result,indent=2)+'\n');print('Linux exact identities, private homes/libs/versions, install stages clear')
