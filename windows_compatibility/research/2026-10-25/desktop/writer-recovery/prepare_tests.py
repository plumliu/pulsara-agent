from pathlib import Path
import json
import shutil
import tempfile

source=Path('/mnt/c/python_workspace/pulsara-agent')
root=Path(tempfile.mkdtemp(prefix='.desktop-recovery-',dir='/home/plumwsl'))
shutil.copytree(source/'src',root/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
(root/'tests').mkdir()
for name in ('__init__.py','test_desktop_renderer_bridge.py'):
    shutil.copy2(source/'tests'/name,root/'tests'/name)
shutil.copytree(source/'tests/support',root/'tests/support',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
shutil.copy2(source/'pyproject.toml',root/'pyproject.toml')
record=source/'output/phase-b/desktop/writer-recovery/test-stage.json'
record.write_text(json.dumps({'root':str(root),'python':'/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python'},indent=2)+'\n')
print(root)
