"""Use current sources in a private ext4 tree, never update the shared clone."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
import os

windows = Path("/mnt/c/python_workspace/pulsara-agent")
directory = Path(tempfile.mkdtemp(prefix=".g0backend-", dir="/home/plumwsl"))
result = {"directory": str(directory)}
try:
    shutil.copytree(windows / "src/pulsara_agent", directory / "src/pulsara_agent", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "static"))
    (directory / "src/pulsara_agent/desktop_app/static").symlink_to(windows / "src/pulsara_agent/desktop_app/static", target_is_directory=True)
    bootstrap = "import sys;sys.path.insert(0,sys.argv.pop(1));import pytest;raise SystemExit(pytest.main(sys.argv[1:]))"
    if "--oauth-diagnose" in sys.argv:
        # Bound only this faulty local fixture observation so cleanup exposes its
        # callback task exception instead of waiting forever for a user callback.
        bootstrap = "import sys,asyncio;sys.path.insert(0,sys.argv.pop(1));original=asyncio.run\nasync def bounded(coro):return await asyncio.wait_for(coro,20)\nasyncio.run=lambda coro:original(bounded(coro))\nimport pytest;raise SystemExit(pytest.main(sys.argv[1:]))"
    command = ["/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python", "-c", bootstrap, str(directory / "src"), "-q", "-p", "no:cacheprovider",
               *[str(windows / "tests" / name) for name in (("test_desktop_wsl_backend.py",) if "--quick" in sys.argv else (
                   "test_desktop_guest_ops.py", "test_desktop_wsl_backend.py", "test_desktop_runtime.py", "test_desktop_http_surface.py", "test_mcp_oauth.py",
               ))]]
    if "--oauth-diagnose" in sys.argv:
        command.extend(["-k", "oauth_redirect_and_status", "-vv", "-s", "-x"])
    environment = {**os.environ, "PYTHONPATH": str(directory / "src")}
    if "--quick" in sys.argv or "--full-progress" in sys.argv:
        command.extend(["-vv", "-s", "-x"])
        with subprocess.Popen(command, cwd=directory, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=environment) as child:
            lines = []
            for line in child.stdout:
                lines.append(line)
                print(line, end="", flush=True)
            child.wait()
            result.update(exit=child.returncode, stdout="".join(lines), stderr="")
    else:
        completed = subprocess.run(command, cwd=directory, capture_output=True, text=True, env=environment)
        result.update(exit=completed.returncode, stdout=completed.stdout, stderr=completed.stderr)
finally:
    assert directory.resolve().parent == Path("/home/plumwsl") and directory.name.startswith(".g0backend-")
    shutil.rmtree(directory)
    result["removed"] = not directory.exists()
    (windows / "output/windows-g0/pg_admission_r3/phase-b-pg" / ("oauth-diagnose.json" if "--oauth-diagnose" in sys.argv else "backend-quick-result.json" if "--quick" in sys.argv else "backend-full-progress.json" if "--full-progress" in sys.argv else "backend-test-result.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(result["exit"])
