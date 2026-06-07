"""JSON-LD value serialization helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pulsara_agent.jsonld.iri import IRI
from pulsara_agent.jsonld.node_ref import NodeRef
from pulsara_agent.jsonld.term import Term


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def jsonld_value(value: Any) -> Any:
    if isinstance(value, IRI):
        return value.value
    if isinstance(value, Term):
        return value.name
    if isinstance(value, NodeRef):
        return value.to_jsonld()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, list):
        return [jsonld_value(item) for item in value]
    if isinstance(value, tuple):
        return [jsonld_value(item) for item in value]
    if isinstance(value, dict):
        return {
            _key_name(key): jsonld_value(item)
            for key, item in value.items()
        }
    return value


def _key_name(key: Any) -> Any:
    if isinstance(key, Term):
        return key.name
    if isinstance(key, IRI):
        return key.value
    return key
