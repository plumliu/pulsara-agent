"""Validate tagged desktop versions and assemble one draft through GitHub CLI.

electron-builder owns installer naming/metadata; packaging owns PEP 440 parsing;
GitHub owns release state, pagination and uploads. This script only checks the
three-product contract and keeps publication an explicit human action.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

from packaging.version import Version


ROOT = Path(__file__).resolve().parents[2]


def check_versions(root: Path, tag: str | None = None) -> str:
    desktop = json.loads((root / "desktop/package.json").read_text())["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+-(alpha|beta|rc)\.\d+", desktop):
        raise ValueError("Desktop release requires an alpha, beta or rc SemVer")
    if tag is not None and tag != f"v{desktop}":
        raise ValueError("Tag must match the committed desktop version")
    python = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if python != Version(desktop).public:
        raise ValueError("Python and desktop versions do not correspond")
    runtime = ast.parse((root / "src/pulsara_agent/__init__.py").read_text())
    runtime_version = next(
        ast.literal_eval(node.value)
        for node in runtime.body if isinstance(node, ast.Assign)
        if any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
    )
    if runtime_version != python:
        raise ValueError("CLI/runtime version differs from Python package")
    locked = tomllib.loads((root / "uv.lock").read_text())
    if [item["version"] for item in locked["package"] if item["name"] == "pulsara-agent"] != [python]:
        raise ValueError("uv.lock has a different application version")
    for directory in ("desktop", "frontend"):
        package = json.loads((root / directory / "package.json").read_text())
        lock = json.loads((root / directory / "package-lock.json").read_text())
        if (package["version"], lock["version"], lock["packages"][""]["version"]) != (desktop,) * 3:
            raise ValueError(f"{directory} package and lockfile versions differ")
    return desktop


def installer_names(version: str) -> tuple[str, ...]:
    return (
        f"Pulsara-{version}-macos-arm64.dmg",
        f"Pulsara-{version}-linux-amd64.deb",
        f"Pulsara-{version}-linux-arm64.deb",
    )


def assemble(root: Path, artifacts: Path, output: Path, tag: str, repository: str) -> list[Path]:
    version = check_versions(root, tag)
    expected = installer_names(version)
    files = [path for path in artifacts.rglob("*") if path.is_file()]
    if sorted(path.name for path in files) != sorted(expected):
        raise ValueError("Expected exactly one DMG and two deb installers for this tag")
    output.mkdir(parents=True, exist_ok=True)
    for path in files:
        if path.is_symlink() or path.stat().st_size == 0:
            raise ValueError("Installer must be a nonempty regular file")
        shutil.copyfile(path, output / path.name)
    checksums = []
    for name in expected:
        with (output / name).open("rb") as source:
            checksums.append(f"{hashlib.file_digest(source, 'sha256').hexdigest()}  {name}\n")
    (output / "SHA256SUMS").write_text("".join(checksums))
    template = (root / "desktop/release-notes.md").read_text()
    downloads = "\n".join(
        f"- [{label}](https://github.com/{repository}/releases/download/{tag}/{name})"
        for label, name in zip(("macOS 15+，Apple Silicon", "Ubuntu 24.04，amd64", "Ubuntu 24.04，arm64"), expected)
    )
    (output / "release-notes.md").write_text(template.replace("{{version}}", version).replace("{{downloads}}", downloads))
    return [output / name for name in (*expected, "SHA256SUMS")]


def gh(*args: str):
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout


def find_release(repository: str, tag: str):
    pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repository}/releases"))
    return next((release for page in pages for release in page if release["tag_name"] == tag), None)


def upload_draft(repository: str, tag: str, files: list[Path], notes: Path) -> str:
    release = find_release(repository, tag)
    if release is not None and not release["draft"]:
        raise ValueError("Published releases must never be edited or overwritten")
    common = (tag, "--repo", repository, "--title", f"Pulsara {tag}", "--notes-file", str(notes), "--prerelease", "--latest=false")
    if release is None:
        gh("release", "create", *common, "--draft", "--verify-tag")
    else:
        # Omitting --draft avoids ever turning a published release back into a draft.
        gh("release", "edit", *common)
    release = find_release(repository, tag)
    if release is None or not release["draft"] or not release["prerelease"]:
        raise ValueError("Assets can only be uploaded to a prerelease draft")
    gh("release", "upload", tag, "--repo", repository, *(str(path) for path in files), "--clobber")
    release = find_release(repository, tag)
    expected = {path.name: path.stat().st_size for path in files}
    actual = {asset["name"]: asset["size"] for asset in release["assets"]}
    if not release["draft"] or not release["prerelease"] or actual != expected:
        raise ValueError("Draft state or uploaded asset inventory does not match")
    return release["html_url"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "assemble", "draft"))
    parser.add_argument("--tag")
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repository")
    parser.add_argument("--resources", type=Path)
    args = parser.parse_args()
    if args.command == "check":
        version = check_versions(ROOT, args.tag)
        if args.resources:
            observed = subprocess.run(
                [str(args.resources.resolve() / "python/bin/python3"), "-I", "-c",
                 "import json, importlib.metadata; from pulsara_agent import __version__; "
                 "print(json.dumps([__version__, importlib.metadata.version('pulsara-agent')]))"],
                check=True, capture_output=True, text=True,
            )
            if json.loads(observed.stdout) != [Version(version).public] * 2:
                raise ValueError("Bundled Python/CLI version differs from release metadata")
        print(version)
        return
    if not all((args.tag, args.artifacts, args.output, args.repository)):
        parser.error("assemble/draft requires tag, artifacts, output and repository")
    files = assemble(ROOT, args.artifacts, args.output, args.tag, args.repository)
    if args.command == "draft":
        print(upload_draft(args.repository, args.tag, files, args.output / "release-notes.md"))


if __name__ == "__main__":
    main()
