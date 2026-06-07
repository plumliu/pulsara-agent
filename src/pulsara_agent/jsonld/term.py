"""JSON-LD term with compact name and absolute IRI."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.jsonld.iri import IRI


@dataclass(frozen=True, slots=True)
class Term:
    name: str
    iri: IRI

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("Term name cannot be empty")

    @property
    def value(self) -> str:
        return self.iri.value
