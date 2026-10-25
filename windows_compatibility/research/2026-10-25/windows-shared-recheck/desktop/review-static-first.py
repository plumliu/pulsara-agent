import json,subprocess,re
from pathlib import Path
root=Path.cwd(); prefix='src/pulsara_agent/desktop_app/static/'
oldhtml=subprocess.check_output(['git','show','HEAD:'+prefix+'index.html']).decode(); newhtml=(root/prefix/'index.html').read_text()
oldname=re.search(r'src="\./(assets/index-[^"]+\.js)"',oldhtml).group(1); newname=re.search(r'src="\./(assets/index-[^"]+\.js)"',newhtml).group(1)
old=json.loads(subprocess.check_output(['git','show','HEAD:'+prefix+oldname+'.map'])); new=json.loads((root/prefix/(newname+'.map')).read_bytes())
a=dict(zip(old['sources'],old['sourcesContent'])); b=dict(zip(new['sources'],new['sourcesContent'])); changes=[]
for name in sorted(set(a)|set(b)):
 x,y=a.get(name),b.get(name)
 if x!=y: changes.append({'source':name,'oldPresent':x is not None,'newPresent':y is not None,'equalAfterCRLF':isinstance(x,str) and isinstance(y,str) and x.replace('\r\n','\n')==y.replace('\r\n','\n'),'oldLength':None if x is None else len(x),'newLength':None if y is None else len(y)})
result={'oldIndex':oldname,'newIndex':newname,'oldSources':len(a),'newSources':len(b),'changes':changes,'status':subprocess.check_output(['git','status','--short','--',prefix]).decode()}
Path('/mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/desktop/static-review.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='status'},indent=2))
