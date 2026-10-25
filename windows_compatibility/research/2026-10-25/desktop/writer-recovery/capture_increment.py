from pathlib import Path
import difflib
import json
import shutil

repo=Path('/mnt/c/python_workspace/pulsara-agent')
out=repo/'output/phase-b/desktop/writer-recovery'
base=out/'baseline-source'
installed=Path('/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.7/python/lib/python3.12/site-packages')
names=[]
for module in ('session_controller.py','renderer_bridge.py','http_server.py'):
    relative='src/pulsara_agent/desktop_app/'+module
    target=base/relative;target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(installed/'pulsara_agent/desktop_app'/module,target)
    names.append(relative)
mapping=json.loads((repo/'src/pulsara_agent/desktop_app/static/assets/index-rI4KiH0F.js.map').read_text())
for name,content in zip(mapping['sources'],mapping['sourcesContent']):
    for relative in ('frontend/app/pulsara-app.tsx','frontend/components/workbench-view.tsx'):
        if name.endswith(relative):
            target=base/relative;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(content)
            names.append(relative)
assert len(names)==5,names
relative='frontend/app/styles/workbench.css'
text=(repo/relative).read_text()
target=base/relative;target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(''.join(line for line in text.splitlines(keepends=True) if not line.startswith('.runtime-error-details ')))
names.append(relative)
names += ['desktop/assets/'+name for name in ('startup.html','startup.js','startup.css')]
diff=''.join(''.join(difflib.unified_diff((base/name).read_text().splitlines(True),(repo/name).read_text().splitlines(True),fromfile='before/'+name,tofile='after/'+name)) for name in names)
(out/'recovery-candidate.diff').write_text(diff)
(out/'increment-sources.json').write_text(json.dumps({'pythonBase':'read-only installed alpha7','frontendTsxBase':'actual alpha8 emitted source map','frontendCssBase':'current file minus only the three newly added runtime-error-details rules','startupBase':'read-only installed alpha7 app.asar','files':names,'codeHashesRecorded':False},indent=2)+'\n')
print(f'{len(names)} production files; {len(diff.encode())} byte incremental diff')
