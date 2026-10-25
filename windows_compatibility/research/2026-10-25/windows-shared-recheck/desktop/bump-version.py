from pathlib import Path
import subprocess,json,datetime,re
root=Path.cwd(); out=root/'output/windows-shared-recheck/desktop'
files=['pyproject.toml','src/pulsara_agent/__init__.py','uv.lock','desktop/package.json','desktop/package-lock.json','frontend/package.json','frontend/package-lock.json']
def call(args):
 p=subprocess.run(args,capture_output=True,text=True,encoding='utf-8'); assert p.returncode==0,(args,p.stdout,p.stderr); return p.stdout.strip()
record={'time':datetime.datetime.now(datetime.timezone.utc).isoformat(),'windowsHead':call(['git','rev-parse','HEAD']),'windowsStatus':call(['git','status','--short']),'wslHead':call(['wsl.exe','-d','Ubuntu','-u','plumwsl','--cd','/home/plumwsl/workspaces/pulsara_agent','--exec','git','rev-parse','HEAD']),'wslStatus':call(['wsl.exe','-d','Ubuntu','-u','plumwsl','--cd','/home/plumwsl/workspaces/pulsara_agent','--exec','git','status','--short'])}
assert record['windowsHead']==record['wslHead']=='0ea98e63d90043773f42bd9bb9bbb56951fca8b4'
assert record['windowsStatus']==record['wslStatus']==''
(out/'source-before.json').write_text(json.dumps(record,indent=2)+'\n')
for name in files:
 p=root/name; data=p.read_bytes(); backup=out/'canonical-before'/name; backup.parent.mkdir(parents=True,exist_ok=True); backup.write_bytes(data)
 if name.endswith('.json'):
  obj=json.loads(data); assert obj['version']=='0.1.0-alpha.9'; obj['version']='0.1.0-alpha.10'
  if name.endswith('lock.json'): assert obj['packages']['']['version']=='0.1.0-alpha.9'; obj['packages']['']['version']='0.1.0-alpha.10'
  expected=2 if name.endswith('lock.json') else 1
  assert data.count(b'"version": "0.1.0-alpha.9"')==expected
  changed=data.replace(b'"version": "0.1.0-alpha.9"',b'"version": "0.1.0-alpha.10"'); assert json.loads(changed)==obj
 else:
  if name=='uv.lock':
   pattern=rb'(name = "pulsara-agent"\r?\nversion = ")0\.1\.0a9(")'; changed,n=re.subn(pattern,rb'\g<1>0.1.0a10\2',data); assert n==1
  else:
   assert data.count(b'"0.1.0a9"')==1; changed=data.replace(b'"0.1.0a9"',b'"0.1.0a10"')
 p.write_bytes(changed)
 for_wsl='/mnt/c/python_workspace/pulsara-agent/'+name
 call(['wsl.exe','-d','Ubuntu','-u','plumwsl','--cd','/home/plumwsl/workspaces/pulsara_agent','--exec','cp',for_wsl,name])
(out/'version-change.json').write_text(json.dumps({'files':files,'python':'0.1.0a10','desktop':'0.1.0-alpha.10','bothClones':True,'dependencyVersionsChanged':False},indent=2)+'\n')
print('Seven canonical files synchronized, alpha10; dependency values unchanged')
