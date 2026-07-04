"""Combined ontology registry for graph storage."""

from __future__ import annotations

from typing import Any

from pulsara_agent.ontology import capability, context, memory, runtime


GRAPH_BASE = "https://pulsara.dev/graph/"


def merge_contexts(*contexts: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "graph": GRAPH_BASE,
    }
    for ctx in contexts:
        for key, value in ctx.items():
            existing = merged.get(key)
            if existing is not None and existing != value:
                raise ValueError(f"Conflicting JSON-LD context key: {key}")
            merged[key] = value
    return merged


CORE_CONTEXT: dict[str, Any] = merge_contexts(
    memory.CONTEXT,
    context.CONTEXT,
    runtime.CONTEXT,
    capability.CONTEXT,
)
