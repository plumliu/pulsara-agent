"""Hatch wheel gate for the exact read-only bundled Skill inventory."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import platform
import subprocess
import stat
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


def _load_inventory_contract(repository_root: Path):
    path = (
        repository_root
        / "src"
        / "pulsara_agent"
        / "capability"
        / "bundled_inventory.py"
    )
    module_name = "_pulsara_bundled_inventory_build_contract"
    specification = importlib.util.spec_from_file_location(module_name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("bundled Skill build contract cannot be loaded")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _classify_source_inventory(repository_root: Path):
    contract = _load_inventory_contract(repository_root)
    root = repository_root / "src" / "pulsara_agent" / "bundled_skills"
    entries = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.name.startswith("."):
            continue
        metadata = child.lstat()
        is_directory = stat.S_ISDIR(metadata.st_mode)
        regular_document = False
        if is_directory:
            document = child / "SKILL.md"
            try:
                document_metadata = document.lstat()
            except FileNotFoundError:
                pass
            else:
                regular_document = stat.S_ISREG(document_metadata.st_mode)
        entries.append(
            contract.BundledInventoryEntry(
                child.name,
                is_directory,
                regular_document,
            )
        )
    return contract.classify_bundled_skill_inventory(entries)


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        del version
        root = Path(self.root)
        executable = root / "src/pulsara_agent/_vendor/ripgrep/rg"
        # Supported publication target: tested official Mach-O minos 11.0 arm64.
        # Other upstream assets are preparable, but cannot be published unverified.
        if (platform.system(), platform.machine()) != ("Darwin", "arm64"):
            raise RuntimeError(
                "No validated Pulsara ripgrep wheel target for this platform"
            )
        try:
            architectures = subprocess.check_output(["/usr/bin/lipo", "-archs", str(executable)], timeout=60).strip()
            if architectures != b"arm64":
                raise ValueError("prepared rg does not match the arm64 wheel target")
            metadata = subprocess.check_output(["/usr/bin/otool", "-l", str(executable)], timeout=60)
            if b"minos 11.0\n" not in metadata:
                raise ValueError("prepared rg minimum OS does not match the 11.0 wheel target")
            result = subprocess.run(
                [str(executable), "--version"],
                capture_output=True,
                timeout=60,
                check=True,
            )
            if result.stdout.splitlines()[0].split()[:2] != [b"ripgrep", b"15.2.0"]:
                raise ValueError("incorrect private rg version")
            for name in ("COPYING", "LICENSE-MIT", "LICENSE-PCRE2"):
                if not (executable.parent / name).is_file():
                    raise ValueError(f"missing ripgrep license: {name}")
        except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                "Run .venv/bin/python tools/prepare_ripgrep.py explicitly before offline build/install"
            ) from exc
        if self.target_name == "wheel":
            build_data["pure_python"] = False
            build_data["tag"] = "py3-none-macosx_11_0_arm64"
            build_data["force_include"][str(executable.parent)] = (
                "pulsara_agent/_vendor/ripgrep"
            )
        classification = _classify_source_inventory(Path(self.root))
        if not classification.valid:
            raise RuntimeError("; ".join(classification.diagnostics))
