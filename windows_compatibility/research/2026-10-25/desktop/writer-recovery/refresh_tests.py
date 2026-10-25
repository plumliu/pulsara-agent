from pathlib import Path
import json
import os
import shutil

source=Path('/mnt/c/python_workspace/pulsara-agent')
record=source/'output/phase-b/desktop/writer-recovery/test-stage.json'
root=Path(json.loads(record.read_text())['root'])
assert root.parent==Path('/home/plumwsl') and root.name.startswith('.desktop-recovery-')
assert root.resolve()==root and root.stat().st_uid==os.getuid()
for name in ('desktop_app/session_controller.py','desktop_app/renderer_bridge.py','desktop_app/http_server.py'):
    shutil.copy2(source/'src/pulsara_agent'/name,root/'src/pulsara_agent'/name)
shutil.copy2(source/'tests/test_desktop_renderer_bridge.py',root/'tests/test_desktop_renderer_bridge.py')
print(root)
