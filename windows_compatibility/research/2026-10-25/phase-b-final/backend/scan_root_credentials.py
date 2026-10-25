"""Only exact configured credentials are checked; never print their values."""
import json
from pathlib import Path

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore

repository = Path('/mnt/c/python_workspace/pulsara-agent')
target = repository / 'windows_compatibility/research/2026-10-25/windows-shared-recheck/root'
files = sorted(path for path in target.iterdir() if path.is_file())
assert len(files) == 29
scrub = LocalSettingsStore(require_pulsara_home() / 'local-settings.yaml').read().credential_scrub_set()
hits = [str(path.relative_to(target)) for path in files if scrub.contains(path.read_bytes())]
result = {'checked_files': len(files), 'actual_credential_matches': len(hits), 'matching_paths': hits}
output = repository / 'output/phase-b-final/backend/root-credential-scan.json'
assert not output.exists()
output.write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result))
assert not hits
