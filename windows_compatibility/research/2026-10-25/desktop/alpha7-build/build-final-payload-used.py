"""Refresh only this build's private ext4 source and resources from Windows."""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import shutil
import sys

source = Path('/mnt/c/python_workspace/pulsara-agent')
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--record',type=Path,default=source/'output/windows-b/host-setup/build-stage.json')
record=parser.parse_args().record.resolve()
assert record.is_relative_to(source/'output')
facts = json.loads(record.read_text())
root = Path(facts['root'])
assert root.parent == Path('/home/plumwsl') and root.name.startswith('.pulsara-wsl-build-')
assert root.resolve() == root and root.stat().st_uid == os.getuid() == 1000
snapshot = root / 'repository'
assert snapshot == Path(facts['snapshot']) and snapshot.resolve() == snapshot
previous = {key:facts[key] for key in ('snapshotStarted','payloadFinished','manifest') if key in facts}
previous['reason'] = 'user-approved local alpha.7 successor with ROOT stale-head/cancellation correction; alpha.6 and its evidence remain preserved, no same-version live overwrite'
facts.setdefault('previousCandidates', []).append(previous)
facts['snapshotStarted'] = datetime.now(timezone.utc).isoformat()
shutil.rmtree(snapshot / 'src')
shutil.copytree(source / 'src', snapshot / 'src', ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
native = Path(facts['nativeRipgrepSource'])
shutil.copytree(native,snapshot/'src/pulsara_agent/_vendor/ripgrep',dirs_exist_ok=True,symlinks=True)
for name in ('pyproject.toml','uv.lock','README.md','.gitignore','.python-version'):
 shutil.copy2(source/name,snapshot/name)
for name in ('prepare_resources.py','prepare_wsl_payload.py','pulsara-cli.sh'):
 shutil.copy2(source/'desktop/scripts'/name,snapshot/'desktop/scripts'/name)
shutil.copy2(source/'desktop/package.json',snapshot/'desktop/package.json')
facts['snapshotFinished'] = datetime.now(timezone.utc).isoformat()
facts['phase'] = 'final-source-snapshot-wheel-building'
record.write_text(json.dumps(facts,indent=2)+'\n')
os.environ['PATH'] = '/home/plumwsl/.local/share/mise/installs/uv/0.13.4/.mise-bins:/home/plumwsl/.local/share/mise/installs/node/22.23.3/bin:/home/plumwsl/workspaces/pulsara_agent/.venv/bin:/usr/bin:/bin'
sys.path.insert(0,str(snapshot/'desktop/scripts'))
import prepare_resources
prepare_resources.RESOURCES = root/'resources'
prepare_resources.prepare_application()
facts['wheelInstalledAt'] = datetime.now(timezone.utc).isoformat()
facts['phase'] = 'final-wheel-installed-payload-building'
record.write_text(json.dumps(facts,indent=2)+'\n')
import prepare_wsl_payload
prepare_wsl_payload.package(root/'resources',source/'desktop/resources/wsl-payload')
manifest = source/'desktop/resources/wsl-payload/manifest.json'
facts['manifest'] = json.loads(manifest.read_text())
facts['payloadFinished'] = datetime.now(timezone.utc).isoformat()
facts['phase'] = 'final-payload-ready-awaiting-final-helper'
manifest.replace(record.parent/'payload-manifest-awaiting-final-scope.json')
facts['manifestWithheldFromApp'] = True
record.write_text(json.dumps(facts,indent=2)+'\n')
print(json.dumps(facts,indent=2))
