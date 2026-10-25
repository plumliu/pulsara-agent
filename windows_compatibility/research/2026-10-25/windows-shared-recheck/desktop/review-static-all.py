from pathlib import Path
import json,subprocess
root=Path.cwd(); prefix='src/pulsara_agent/desktop_app/static/'
oldnames=subprocess.check_output(['git','ls-tree','-r','--name-only','HEAD',prefix]).decode().splitlines(); newnames=[str(p.relative_to(root)) for p in (root/prefix).rglob('*') if p.is_file()]
def collect(names,old):
 sources={}; count=0
 for name in names:
  if not name.endswith('.js.map'): continue
  data=subprocess.check_output(['git','show','HEAD:'+name]) if old else (root/name).read_bytes()
  obj=json.loads(data); count+=1
  for key,value in zip(obj['sources'],obj.get('sourcesContent',[])):
   if value is not None: sources.setdefault(key,set()).add(value.replace('\r\n','\n'))
 return sources,count
a,ac=collect(oldnames,True);b,bc=collect(newnames,False)
changes=[{'source':k,'oldPresent':k in a,'newPresent':k in b} for k in sorted(set(a)|set(b)) if a.get(k)!=b.get(k)]
result={'baseline':'HEAD 0ea98e63','built':'WSL desktop:verify then desktop:wsl-payload','oldMapFiles':ac,'newMapFiles':bc,'oldUniqueSourceNames':len(a),'newUniqueSourceNames':len(b),'contentChangesAfterCRLFNormalization':changes,'sameSourceContentAfterCRLF':not changes,'payloadAlreadyWritten':True,'windowsTrackedStaticUntouched':True}
assert not changes,result
Path('/mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/desktop/static-review-all.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
