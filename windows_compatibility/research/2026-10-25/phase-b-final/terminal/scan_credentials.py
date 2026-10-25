"""Read original saved settings, scan exact secrets/JSON escapes, print no values."""
import json
from pathlib import Path
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore

root=Path('/mnt/c/python_workspace/pulsara-agent/windows_compatibility/research/2026-10-25/phase-b-final/terminal')
settings=require_pulsara_home()/'local-settings.yaml'
before=settings.stat()
scrub=LocalSettingsStore(settings).read().credential_scrub_set()
escaped=[json.dumps(value.decode('utf-8'),ensure_ascii=ascii)[1:-1].encode('utf-8') for value in scrub.values for ascii in (False,True)]
files=[p for p in root.rglob('*') if p.is_file()]
hits=[]
for path in files:
    raw=path.read_bytes()
    if scrub.contains(raw) or any(value and value in raw for value in escaped):hits.append(str(path.relative_to(root)))
after=settings.stat()
unchanged=(before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_ino,after.st_size,after.st_mtime_ns)
result=dict(checkedFiles=len(files),actualCredentialMatches=len(hits),matchingPaths=hits,jsonEscapesChecked=True,savedSettingsUnchanged=unchanged)
(root/'credential-scan-final.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result))
assert not hits and unchanged
