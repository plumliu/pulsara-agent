"""IRI value object."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IRI:
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("IRI cannot be empty")

    def __str__(self) -> str:
        return self.value
