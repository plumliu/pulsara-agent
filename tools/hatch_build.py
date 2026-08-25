"""Hatch wheel gate for the exact read-only bundled Skill inventory."""

from __future__ import annotations

import importlib.util
from pathlib import Path
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
        del version, build_data
        classification = _classify_source_inventory(Path(self.root))
        if not classification.valid:
            raise RuntimeError("; ".join(classification.diagnostics))
