"""Explicit development replacement of the verified unused failed candidate."""
from pathlib import Path
import fcntl
import json
import os
import shutil

assert os.getuid() == 1000
base = Path('/home/plumwsl/.local/share/pulsara/runtime')
target = base / '0.1.0-alpha.6'
assert base.resolve(strict=True) == base
assert target.resolve(strict=True) == target and target.is_dir() and not target.is_symlink()
assert target.stat().st_uid == os.getuid()
marker = (target / '.pulsara-payload.sha256').read_text().strip()
assert marker == '23781d9893174f015b4e37899ecff52a8ee9f7bd6f608d30ad8dfb2a62e52238'
assert not Path('/home/plumwsl/.pulsara/desktop/postgres').exists()
with (base / '.install.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    shutil.rmtree(target)
print(json.dumps({'removed':str(target),'previousPayload':marker,'explicitDevelopmentReplacement':True,'dataHomePreserved':Path('/home/plumwsl/.pulsara').is_dir()}))
