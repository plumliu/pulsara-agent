"""Refresh only the stopped, private CUA test application; preserve its home."""
import json
from pathlib import Path
import plistlib
import shutil
import subprocess

root = Path(__file__).resolve().parent
info = json.loads((root / 'gui-environment.json').read_text())
app = Path(info['app'])
source = root / 'package-fixed/mac-arm64/pulsara.app'
processes = subprocess.check_output(['ps', '-axo', 'pid=,command='], text=True)
assert str(app / 'Contents/MacOS') not in processes
assert str(app / 'Contents/Resources/python/bin') not in processes
assert app.parent == root / 'installed' and app.name == 'pulsara.app'
assert source.is_dir()
shutil.rmtree(app)
subprocess.run(['cp', '-cR', str(source), str(app)], check=True)
plist_path = app / 'Contents/Info.plist'
metadata = plistlib.loads(plist_path.read_bytes())
metadata['CFBundleIdentifier'] = 'dev.pulsara.desktop.macos-regression'
metadata['LSEnvironment'] = {'PULSARA_HOME': info['pulsaraHome']}
metadata.pop('CFBundleURLTypes', None)
plist_path.write_bytes(plistlib.dumps(metadata))
subprocess.run(['codesign', '--force', '--sign', '-', str(app)], check=True)
subprocess.run(['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister', '-f', str(app)], check=True)
package = app / 'Contents/Resources/python/lib/python3.12/site-packages/pulsara_agent'
comparisons = {}
for relative in ('conversation_kernel/provider_dispatch.py', 'hooks/executor.py', 'conversation_kernel/mcp/sdk_facade.py'):
    assert (package / relative).read_bytes() == (root.parents[2] / 'src/pulsara_agent' / relative).read_bytes()
    comparisons[relative] = 'complete bytes equal'
(root / 'package-fixed-provenance.json').write_text(json.dumps({'application': str(app), 'source': str(source), 'version': metadata['CFBundleShortVersionString'], 'productionSourceComparison': comparisons, 'metadataIsolation': info['metadataIsolation']}, indent=2) + '\n')
print('Private final app refreshed; three production sources exactly equal; home retained.')
