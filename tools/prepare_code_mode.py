"""Prepare existing mise Node and locked QuickJS assets for offline wheels.

mise owns the pinned Node distribution and npm owns SDK integrity. Pulsara only
copies their release assets; an installed Host never downloads dependencies.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tomllib

QUICKJS_VERSION = "3.6.2"


def prepare(repository: Path) -> None:
    version = tomllib.loads((repository / "mise.toml").read_text())["tools"]["node"]
    executable = shutil.which("node")
    if executable is None:
        raise RuntimeError("Run mise install before preparing Code Mode")
    node = Path(executable).resolve()
    actual = subprocess.check_output([str(node), "--version"], text=True).strip()
    if actual != f"v{version}":
        raise RuntimeError(f"Code Mode build Node must be {version}; use mise exec")
    license_file = node.parent.parent / "LICENSE"
    if not license_file.is_file():
        raise RuntimeError("The mise Node distribution must include its upstream LICENSE")
    package = repository / "tools/code_mode_node"
    subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                   cwd=package, check=True)
    sdk = package / "node_modules/quickjs-wasi"
    if json.loads((sdk / "package.json").read_text())["version"] != QUICKJS_VERSION:
        raise RuntimeError("Unexpected quickjs-wasi package version")
    target = repository / "src/pulsara_agent/_vendor/code_mode"
    target.mkdir(parents=True, exist_ok=True)
    destination = target / "quickjs-wasi"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    for filename in ("package.json", "LICENSE", "quickjs.wasm"):
        shutil.copy2(sdk / filename, destination / filename)
    shutil.copytree(sdk / "dist", destination / "dist")
    shutil.copy2(node, target / "node")
    shutil.copy2(license_file, target / "NODE-LICENSE.txt")
    (target / "node").chmod(0o755)
    print(f"Prepared Node {version} and quickjs-wasi {QUICKJS_VERSION}")


if __name__ == "__main__":
    prepare(Path(__file__).resolve().parents[1])
