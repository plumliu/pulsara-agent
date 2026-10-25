"""Build/install the real candidate wheel into a private ext4 test directory."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import os

repository = Path("/mnt/c/python_workspace/pulsara-agent")
output = repository / "output/windows-g0/pg_admission_r3/phase-b-pg"
directory = Path(tempfile.mkdtemp(prefix=".pg-lf-", dir="/home/plumwsl"))
source = directory / "source"
source.mkdir()
for name in ("pyproject.toml", "README.md", "LICENSE"):
    if (repository / name).exists():
        shutil.copy2(repository / name, source / name)
shutil.copytree(repository / "tools", source / "tools", ignore=shutil.ignore_patterns("__pycache__"))
shutil.copytree(repository / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "static", "ripgrep"))
package = source / "src/pulsara_agent"
(package / "desktop_app/static").symlink_to(repository / "src/pulsara_agent/desktop_app/static", target_is_directory=True)
shutil.copytree(Path("/home/plumwsl/workspaces/pulsara_agent/src/pulsara_agent/_vendor/ripgrep"), package / "_vendor/ripgrep")
python = "/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python"
build = subprocess.run([python, "-m", "hatchling", "build", "-t", "wheel", "-d", str(directory / "wheel")], cwd=source, capture_output=True, text=True)
facts = {"directory": str(directory), "build": {"exit": build.returncode, "stdout": build.stdout, "stderr": build.stderr}}
if build.returncode:
    (output / "lf-fresh-build.json").write_text(json.dumps(facts, indent=2))
    raise SystemExit(build.returncode)
wheel, = (directory / "wheel").glob("*.whl")
uv = "/home/plumwsl/.local/share/mise/installs/uv/0.13.4/uv-x86_64-unknown-linux-gnu/uv"
installed_python = "/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.6/python/bin/python3"
installed = directory / "installed"
installation = subprocess.run([uv, "pip", "install", "--python", installed_python, "--target", str(installed), "--no-deps", str(wheel)], capture_output=True, text=True)
facts["installation"] = {"exit": installation.returncode, "stdout": installation.stdout, "stderr": installation.stderr}
(output / "lf-fresh-build.json").write_text(json.dumps(facts, indent=2))
if installation.returncode:
    raise SystemExit(installation.returncode)
home = directory / "home"
home.mkdir(mode=0o700)
config = {"directory": str(directory), "package": str(installed), "home": str(home), "python": installed_python,
          "uid": os.getuid(), "wheel": str(wheel)}
(output / "lf-fresh-config.json").write_text(json.dumps(config))
print(json.dumps(config))
