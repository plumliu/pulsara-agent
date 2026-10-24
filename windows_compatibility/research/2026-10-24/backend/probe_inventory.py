"""Read-only source/lock evidence. Does not emulate a Windows runtime."""
from pathlib import Path
import ast
import json
import tomllib
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
lock = tomllib.loads((ROOT / "uv.lock").read_text())
names = {"aiohttp", "google-re2", "greenlet", "psycopg-binary", "pillow", "playwright", "pydantic-core", "pywin32", "cryptography", "cffi"}
packages = []
for package in lock["package"]:
    if package["name"] not in names:
        continue
    wheels = [unquote(wheel["url"].rsplit("/", 1)[-1]) for wheel in package.get("wheels", [])]
    packages.append({"name": package["name"], "version": package["version"], "windows_cp312_or_abi3_or_py3_wheels": [n for n in wheels if "win" in n and ("cp312" in n or "abi3" in n or "-py3-" in n)]})

direct = []
dirfd = []
for path in sorted((ROOT / "src/pulsara_agent").rglob("*.py")):
    if "terminal_process" in path.parts:
        continue
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr in {"O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC", "getuid", "geteuid", "fchmod", "killpg"}:
            direct.append({"path": str(path.relative_to(ROOT)), "line": node.lineno, "attribute": node.attr})
        if isinstance(node, ast.Call) and any(k.arg in {"dir_fd", "src_dir_fd", "dst_dir_fd"} for k in node.keywords):
            dirfd.append({"path": str(path.relative_to(ROOT)), "line": node.lineno})
data = {"kind": "static source evidence; no Windows execution", "packages": packages, "direct_unix_surface": direct, "dir_fd_calls": dirfd}
(OUT / "inventory.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"packages": packages, "direct_unix_surface_count": len(direct), "dir_fd_call_count": len(dirfd)}, ensure_ascii=False, indent=2))
