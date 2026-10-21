"""Desktop home resolution must not initialize capability implementation owners."""

import json
import subprocess
import sys


def test_home_resolution_imports_only_its_own_capability_module(tmp_path):
    code = """
import json, sys
from pulsara_agent.capability.pulsara_home import require_pulsara_home
home = require_pulsara_home(sys.argv[1])
print(json.dumps({"home": str(home), "modules": sorted(
    name for name in sys.modules if name.startswith("pulsara_agent.capability")
)}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path.resolve())],
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == {
        "home": str(tmp_path.resolve()),
        "modules": ["pulsara_agent.capability", "pulsara_agent.capability.pulsara_home"],
    }
