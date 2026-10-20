"""Describe verified native build inputs for mise's task freshness checks.

mise owns tools and task invalidation, uv owns Python, and platform compilers own
native builds. The only adapter here validates prerequisites and exposes their
exact values: mise cannot infer an Xcode/SDK or native compiler change itself.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tomllib

from prepare_resources import REPO, ROOT, output, target_platform


def describe():
    target = target_platform()
    pins = tomllib.loads((REPO / "mise.toml").read_text())["tools"]
    observed = {
        "protoc": output(["protoc", "--version"]).removeprefix("libprotoc "),
        "node": output(["node", "--version"]).removeprefix("v"),
        "uv": output(["uv", "--version"]).split()[1],
        "python": output([sys.executable, "-c", "import platform; print(platform.python_version())"]),
    }
    for name, expected in {**pins, "python": (REPO / ".python-version").read_text().strip()}.items():
        if observed[name] != expected:
            raise RuntimeError(f"Build {name} must be {expected}; use mise run desktop:resources")
    programs = ["cc", "make", "bison", "flex", "pkg-config"]
    programs += (["dylibbundler", "lipo", "otool", "xcrun", "codesign", "install_name_tool"]
                 if target[0] == "darwin"
                 else ["readelf", "patchelf", "ldd", "dpkg-deb", "dpkg-query", "xz", "ar"])
    missing = [name for name in programs if not shutil.which(name)]
    if missing:
        raise RuntimeError("Missing native build tools: " + ", ".join(missing))
    inputs = {
        "platform": list(target), "tools": observed,
        "compiler": output([*shlex.split(os.environ.get("CC", "cc")), "--version"]),
        "make": output(["make", "--version"]),
        "bison": output(["bison", "--version"]),
        "flex": output(["flex", "--version"]),
        "flags": {key: os.environ.get(key, "") for key in (
            "CC", "CFLAGS", "CPPFLAGS", "LDFLAGS", "SDKROOT", "MACOSX_DEPLOYMENT_TARGET",
        )},
    }
    if target[0] == "darwin":
        inputs["sdk"] = output(["xcrun", "--show-sdk-version"])
    else:
        # The native library collector redistributes OS packages with notices.
        # A build-host package upgrade must also invalidate those resource layers.
        inputs["system_packages"] = output(["dpkg-query", "--show", "--showformat=${binary:Package}=${Version}\\n"])
    return inputs


def main():
    inputs = describe()
    path = ROOT / "build" / "environment.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(inputs, indent=2, sort_keys=True) + "\n"
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
    print(f"Native build environment ready: {inputs['platform']}, {inputs['tools']}")


if __name__ == "__main__":
    main()
