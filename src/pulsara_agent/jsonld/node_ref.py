"""Reference to another JSON-LD node."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NodeRef:
    id: str

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("NodeRef id cannot be empty")

    def to_jsonld(self) -> dict[str, str]:
        return {"@id": self.id}
