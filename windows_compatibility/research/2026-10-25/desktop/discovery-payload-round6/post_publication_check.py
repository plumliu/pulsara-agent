"""Targeted post-rename runtime check; no installer or PG lifecycle duplication."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile

repo = Path("/home/plumwsl/workspaces/pulsara_agent")
spec = importlib.util.spec_from_file_location(
    "recipe", repo / "desktop/scripts/prepare_resources.py"
)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)
root = Path(tempfile.mkdtemp(prefix=".g0d6-post-", dir="/home/plumwsl"))
stage = root / "stage"
published = root / "runtime" / "0.1.0-alpha.6"
record = dict(
    root=str(root),
    pid=os.getpid(),
    starttime=Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
    uid=os.getuid(),
)
try:
    assert os.getuid() == 1000
    test = socket.socket()
    record["offlineConnectErrno"] = test.connect_ex(("192.0.2.1", 9))
    test.close()
    assert record["offlineConnectErrno"] == 101
    shutil.copytree(R.RESOURCES, stage, symlinks=True)
    (stage / "bin").mkdir()
    shutil.copy2(repo / "desktop/scripts/pulsara-cli.sh", stage / "bin/pulsara")
    R.RESOURCES = stage
    R.validate_resources()
    published.parent.mkdir(mode=0o700)
    stage.rename(published)
    published.chmod(0o700)
    assert not stage.exists()
    env = {
        **os.environ,
        "HOME": str(root),
        "PULSARA_HOME": str(root / "private-settings"),
        "UV_OFFLINE": "1",
    }

    def output(args):
        return subprocess.check_output(
            [str(arg) for arg in args], env=env, text=True
        ).strip()

    imported = json.loads(
        output(
            [
                published / "python/bin/python3",
                "-I",
                "-c",
                "import importlib.metadata,json,pulsara_agent,sys;from pulsara_agent.ripgrep import private_ripgrep;print(json.dumps(dict(package=pulsara_agent.__file__,prefix=sys.prefix,version=importlib.metadata.version('pulsara-agent'),rg=str(private_ripgrep()))))",
            ]
        )
    )
    assert Path(imported["package"]).is_relative_to(published) and Path(
        imported["rg"]
    ).is_relative_to(published)
    lib, share = R.pg_layout(published / "postgres")
    record.update(
        imported=imported,
        cli=output([published / "bin/pulsara", "--version"]),
        pgVersion=output([published / "postgres/bin/postgres", "--version"]),
        rgVersion=output([imported["rg"], "--version"]).splitlines()[0],
        pgLib=str(lib),
        pgShare=str(share),
        published=str(published),
        oldStageAbsent=True,
        passed=True,
    )
finally:
    assert (
        root.resolve() == root
        and root.parent == Path("/home/plumwsl")
        and root.name.startswith(".g0d6-post-")
    )
    shutil.rmtree(root)
    record["cleanupRootAbsent"] = not root.exists()
    print(json.dumps(record))
