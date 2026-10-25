"""Check only actual saved credential values; never print any of those values."""
import json
from pathlib import Path
from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore

root = Path(__file__).resolve().parent
scrub = LocalSettingsStore(require_pulsara_home() / "local-settings.yaml").read().credential_scrub_set()
files = [path for path in root.rglob("*") if path.is_file()]
hits = [str(path.relative_to(root)) for path in files if scrub.contains(path.read_bytes())]
result = {"checked_files": len(files), "actual_credential_matches": len(hits), "matching_paths": hits}
target = root / "credential-scan-final.json"
assert not target.exists()
target.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result))
assert not hits
