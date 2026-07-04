"""Namespace helper for ontology modules."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.jsonld.iri import IRI
from pulsara_agent.jsonld.term import Term


@dataclass(frozen=True, slots=True)
class Namespace:
    base: str

    def __post_init__(self) -> None:
        if not self.base:
            raise ValueError("Namespace base cannot be empty")

    def iri(self, name: str) -> IRI:
        if not name:
            raise ValueError("IRI name cannot be empty")
        return IRI(f"{self.base}{name}")

    def term(self, name: str) -> Term:
        return Term(name=name, iri=self.iri(name))
