"""Dependency-free exact inventory classifier shared by build and Runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


EXPECTED_BUNDLED_SKILL_NAMES = (
    "pulsara-mcp-installer",
    "pulsara-plugin-installer",
    "pulsara-skill-creator",
    "pulsara-skill-installer",
)
if len(EXPECTED_BUNDLED_SKILL_NAMES) != len(set(EXPECTED_BUNDLED_SKILL_NAMES)):
    raise RuntimeError("bundled Skill inventory contains duplicate official names")


@dataclass(frozen=True, slots=True)
class BundledInventoryEntry:
    name: str
    is_directory: bool
    has_regular_skill_document: bool

    def __post_init__(self) -> None:
        if not self.name or self.name.startswith("."):
            raise ValueError("bundled inventory entry is not visible")
        if self.has_regular_skill_document and not self.is_directory:
            raise ValueError("non-directory bundled entry has a Skill document")


@dataclass(frozen=True, slots=True)
class BundledInventoryClassification:
    valid: bool
    observed_names: tuple[str, ...]
    diagnostics: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.observed_names != tuple(sorted(self.observed_names)):
            raise ValueError("bundled inventory names are not sorted")
        if self.valid == bool(self.diagnostics):
            raise ValueError("bundled inventory classification conflicts")


def classify_bundled_skill_inventory(
    entries: Iterable[BundledInventoryEntry],
) -> BundledInventoryClassification:
    """Classify all non-dot immediate entries against the exact official set."""

    frozen = tuple(sorted(entries, key=lambda item: item.name))
    names = tuple(item.name for item in frozen)
    valid = (
        len(names) == len(set(names))
        and set(names) == set(EXPECTED_BUNDLED_SKILL_NAMES)
        and all(item.is_directory and item.has_regular_skill_document for item in frozen)
    )
    if valid:
        return BundledInventoryClassification(True, names, ())
    return BundledInventoryClassification(
        False,
        names,
        (
            "Bundled Skill inventory does not exactly match the official set: "
            f"expected={EXPECTED_BUNDLED_SKILL_NAMES!r}, observed={names!r}",
        ),
    )


__all__ = [
    "BundledInventoryClassification",
    "BundledInventoryEntry",
    "EXPECTED_BUNDLED_SKILL_NAMES",
    "classify_bundled_skill_inventory",
]
