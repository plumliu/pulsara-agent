"""Semantic graph compatibility facade.

The Stage 2 production graph is PostgreSQL-only.  Keeping these old exports
lazy prevents importing a graph leaf from initializing the Oxigraph adapter.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_LAZY_MODULES = (
    "pulsara_agent.graph.in_memory",
    "pulsara_agent.graph.jsonld_codec",
    "pulsara_agent.graph.mutable",
    "pulsara_agent.graph.oxigraph",
    "pulsara_agent.graph.postgres",
    "pulsara_agent.graph.store",
)


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    for module_name in _LAZY_MODULES:
        module = import_module(module_name)
        try:
            value = getattr(module, name)
        except AttributeError:
            continue
        globals()[name] = value
        return value
    raise AttributeError(name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "DEFAULT_GRAPH_ID",
    "GraphStore",
    "InMemoryGraphStore",
    "MutableCanonicalMemoryStore",
    "OxigraphGraphStore",
    "PostgresGraphStore",
    "normalize_jsonld_document",
]
