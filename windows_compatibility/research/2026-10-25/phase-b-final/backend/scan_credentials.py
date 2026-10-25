"""Exact saved-secret containment checks, without printing secret values."""
import argparse
import json
from pathlib import Path

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.settings import LocalSettingsStore

parser = argparse.ArgumentParser()
parser.add_argument('directory', type=Path)
parser.add_argument('output', type=Path)
parser.add_argument('--expected', type=int)
args = parser.parse_args()
files = sorted(path for path in args.directory.rglob('*') if path.is_file())
if args.expected is not None:
    assert len(files) == args.expected, len(files)
assert not args.output.exists()
scrub = LocalSettingsStore(require_pulsara_home() / 'local-settings.yaml').read().credential_scrub_set()
hits = [str(path.relative_to(args.directory)) for path in files if scrub.contains(path.read_bytes())]
result = {'checked_files': len(files), 'actual_credential_matches': len(hits), 'matching_paths': hits}
args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
print(json.dumps(result))
assert not hits
