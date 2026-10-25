"""Read-only newline/resource comparison; never opens a database connection."""
import json
from pathlib import Path
import sys

repo = Path('/mnt/c/python_workspace/pulsara-agent')
stage = json.loads((repo/'output/windows-b/host-setup/build-stage.json').read_text())
relative = Path('pulsara_agent/storage/migrations')
sources = {
 'windows_checkout':repo/'src'/relative,
 'linux_checkout':Path('/home/plumwsl/workspaces/pulsara_agent/src')/relative,
 'build_snapshot':Path(stage['snapshot'])/'src'/relative,
 'build_resources':Path(stage['resources'])/'python/lib/python3.12/site-packages'/relative,
 'installed':Path.home()/'.local/share/pulsara/runtime/0.1.0-alpha.6/python/lib/python3.12/site-packages'/relative,
}
files = ['sql/0000_conversation_kernel_baseline.sql','resources/0000_conversation_kernel_expected_catalog_v1.json','resources/0000_conversation_kernel_runtime_grants_v1.json']
result = {'readOnly':True,'databaseOpened':False,'files':{}}
for filename in files:
 values = {name:(base/filename).read_bytes() for name,base in sources.items()}
 baseline = values['linux_checkout']
 rows = {}
 for name,data in values.items():
  rows[name] = {'path':str(sources[name]/filename),'bytes':len(data),'CRLF':data.count(b'\r\n'),'bareCR':data.count(b'\r')-data.count(b'\r\n'),'LF':data.count(b'\n'),'equalsLinuxBytes':data==baseline,'equalsLinuxAfterCRLFtoLF':data.replace(b'\r\n',b'\n')==baseline}
  if filename.endswith('.json'):
   rows[name]['equalsLinuxParsedJSON'] = json.loads(data)==json.loads(baseline)
 result['files'][filename] = rows
result['installedObservedCatalogExpected'] = json.loads((sources['installed']/files[1]).read_bytes())['observed_catalog_fingerprint']
print(json.dumps(result,indent=2))
