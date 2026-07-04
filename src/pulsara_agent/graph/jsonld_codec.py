"""Shared JSON-LD normalization helpers for graph stores."""

from __future__ import annotations

import json
import urllib.parse
from copy import deepcopy
from typing import Any

from pulsara_agent.ontology import capability, memory, runtime
from pulsara_agent.ontology.registry import CORE_CONTEXT


RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
XSD_BOOLEAN = "http://www.w3.org/2001/XMLSchema#boolean"
XSD_INTEGER = "http://www.w3.org/2001/XMLSchema#integer"
GRAPH_BASE = "https://pulsara.dev/graph/"
FORCE_LIST_KEYS = {
    runtime.PRODUCED.name,
    runtime.PROVIDES.name,
    memory.SUPPORTS.name,
    memory.CONTRADICTS.name,
    memory.SUPERSEDES.name,
    memory.HAS_EVIDENCE.name,
    memory.BASED_ON.name,
    memory.DERIVED_FROM.name,
    memory.TRIGGER_TOOLS.name,
    memory.TRIGGER_ACTIONS.name,
    memory.TRIGGER_FILE_GLOBS.name,
    memory.TRIGGER_SCOPES.name,
    memory.TRIGGER_KEYWORDS.name,
    memory.NEGATIVE_TOOLS.name,
    memory.NEGATIVE_ACTIONS.name,
    memory.NEGATIVE_FILE_GLOBS.name,
    capability.PROVIDES_TOOL.name,
    capability.PROVIDES_SKILL.name,
    capability.REQUIRES.name,
}


def normalize_jsonld_document(
    document: dict[str, Any],
    default_context: Any = CORE_CONTEXT,
) -> dict[str, Any]:
    """Return the document shape produced after RDF-style expand/compact."""

    node_id = document.get("@id")
    if not isinstance(node_id, str) or not node_id:
        raise ValueError("JSON-LD document must include a string @id")

    input_context = document.get("@context") or default_context
    subject_iri = expand_id(node_id, input_context)
    return _document_from_expanded_values(
        subject_iri,
        _expanded_values_for_node(document, input_context),
        default_context,
    )


def triples_for_document(document: dict[str, Any], default_context: Any) -> list[str]:
    context = document.get("@context") or default_context
    subject = iri_token(expand_id(str(document["@id"]), context))
    blank_counter = [0]
    return _triples_for_node(subject, document, context, blank_counter)


def document_from_rows(subject_iri: str, rows: list[dict[str, Any]], context: Any) -> dict[str, Any]:
    values: dict[str, list[Any]] = {}
    blank_values: dict[str, dict[str, list[Any]]] = {}
    seen_main: set[tuple[str, str]] = set()

    for row in rows:
        predicate = row_iri(row["p"], context)
        obj = row["o"]
        object_key = json.dumps(obj, sort_keys=True)
        if (predicate, object_key) not in seen_main:
            seen_main.add((predicate, object_key))
            values.setdefault(predicate, []).append(obj)
        if isinstance(obj, dict) and obj.get("_type") == "bnode" and "bp" in row and "bo" in row:
            blank_values.setdefault(str(obj["id"]), {}).setdefault(row_iri(row["bp"], context), []).append(row["bo"])

    document: dict[str, Any] = {
        "@context": deepcopy(context),
        "@id": compact_iri(subject_iri, context),
    }
    for predicate, objects in values.items():
        if predicate == RDF_TYPE:
            document["@type"] = [compact_type(row_iri(obj, context), context) for obj in objects]
            continue
        key = compact_predicate(predicate, context)
        decoded = [
            _decode_object(obj, blank_values, context)
            for obj in objects
        ]
        document[key] = decoded if len(decoded) != 1 or key in FORCE_LIST_KEYS else decoded[0]
    return document


def binding_to_jsonld(binding: dict[str, Any], context: Any) -> Any:
    binding_type = binding.get("type")
    if binding_type == "uri":
        return {"@id": compact_iri(str(binding["value"]), context)}
    if binding_type == "bnode":
        return {"_type": "bnode", "id": str(binding["value"])}
    if binding_type == "literal":
        datatype = binding.get("datatype")
        value = binding.get("value", "")
        if datatype == XSD_BOOLEAN:
            return value == "true"
        if datatype == XSD_INTEGER:
            return int(value)
        return value
    return binding.get("value")


def row_iri(value: Any, context: Any) -> str:
    if isinstance(value, dict) and "@id" in value:
        return expand_id(str(value["@id"]), context)
    if isinstance(value, str):
        return value
    raise TypeError(f"Expected IRI binding, got {value!r}")


def expand_graph_id(graph_id: str, context: Any) -> str:
    if graph_id.startswith("graph:"):
        return GRAPH_BASE + urllib.parse.quote(graph_id.split(":", 1)[1], safe="/")
    return expand_id(graph_id, context)


def graph_key(graph_id: str | None) -> str:
    from pulsara_agent.graph.store import DEFAULT_GRAPH_ID

    if graph_id is None:
        return DEFAULT_GRAPH_ID
    if not graph_id:
        raise ValueError("graph_id must be a non-empty string or None")
    return graph_id


def expand_id(identifier: str, context: Any) -> str:
    if "://" in identifier or identifier.startswith("urn:"):
        return identifier
    prefix, sep, suffix = identifier.partition(":")
    prefixes = _prefixes(context)
    if sep and prefix in prefixes:
        return prefixes[prefix] + suffix
    return "urn:pulsara:" + urllib.parse.quote(identifier, safe="")


def expand_type(type_name: str, context: Any) -> str:
    mapping = context if isinstance(context, dict) else CORE_CONTEXT
    value = mapping.get(type_name)
    if isinstance(value, str) and ("://" in value or value.startswith("urn:")):
        return value
    return expand_id(type_name, context)


def expand_term(term_name: str, context: Any) -> str:
    mapping = context if isinstance(context, dict) else CORE_CONTEXT
    value = mapping.get(term_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("@id"), str):
        return str(value["@id"])
    return expand_id(term_name, context)


def compact_iri(iri: str, context: Any) -> str:
    for prefix, base in sorted(_prefixes(context).items(), key=lambda item: len(item[1]), reverse=True):
        if iri.startswith(base):
            return f"{prefix}:{iri[len(base):]}"
    if iri.startswith(GRAPH_BASE):
        return f"graph:{iri[len(GRAPH_BASE):]}"
    return iri


def compact_type(iri: str, context: Any) -> str:
    mapping = context if isinstance(context, dict) else CORE_CONTEXT
    for key, value in mapping.items():
        if isinstance(value, str) and value == iri:
            return str(key)
    return compact_iri(iri, context)


def compact_predicate(iri: str, context: Any) -> str:
    mapping = context if isinstance(context, dict) else CORE_CONTEXT
    for key, value in mapping.items():
        if isinstance(value, str) and value == iri:
            return str(key)
        if isinstance(value, dict) and value.get("@id") == iri:
            return str(key)
    return compact_iri(iri, context)


def iri_token(iri: str) -> str:
    return f"<{iri}>"


def literal_token(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _expanded_values_for_node(document: dict[str, Any], context: Any) -> dict[str, list[Any]]:
    values: dict[str, list[Any]] = {}
    seen: set[tuple[str, str]] = set()
    for key, value in document.items():
        if key in {"@context", "@id"}:
            continue
        items = value if isinstance(value, list) else [value]
        predicate = RDF_TYPE if key == "@type" else expand_term(key, context)
        for item in items:
            normalized = _expanded_object(item, context, key == "@type")
            object_key = json.dumps(normalized, sort_keys=True)
            if (predicate, object_key) in seen:
                continue
            seen.add((predicate, object_key))
            values.setdefault(predicate, []).append(normalized)
    return values


def _expanded_object(value: Any, context: Any, is_type: bool = False) -> Any:
    if is_type:
        return {"@id": compact_iri(expand_type(str(value), context), context)}
    if isinstance(value, dict):
        node_id = value.get("@id")
        if isinstance(node_id, str):
            expanded = {"@id": compact_iri(expand_id(node_id, context), context)}
            for key, item in value.items():
                if key == "@id":
                    continue
                expanded[compact_predicate(expand_term(key, context), context)] = _expanded_object(item, context)
            return expanded
        return _document_properties_from_expanded_values(_expanded_values_for_node(value, context), context)
    return value


def _document_from_expanded_values(
    subject_iri: str,
    values: dict[str, list[Any]],
    context: Any,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "@context": deepcopy(context),
        "@id": compact_iri(subject_iri, context),
    }
    document.update(_document_properties_from_expanded_values(values, context))
    return document


def _document_properties_from_expanded_values(
    values: dict[str, list[Any]],
    context: Any,
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for predicate, objects in values.items():
        if predicate == RDF_TYPE:
            document["@type"] = [compact_type(row_iri(obj, context), context) for obj in objects]
            continue
        key = compact_predicate(predicate, context)
        decoded = [
            _decode_normalized_object(obj, context)
            for obj in objects
        ]
        document[key] = decoded if len(decoded) != 1 or key in FORCE_LIST_KEYS else decoded[0]
    return document


def _decode_normalized_object(value: Any, context: Any) -> Any:
    if isinstance(value, dict) and "@id" in value and len(value) == 1:
        return {"@id": compact_iri(expand_id(str(value["@id"]), context), context)}
    if isinstance(value, dict):
        decoded: dict[str, Any] = {}
        for key, item in value.items():
            if key == "@id":
                decoded[key] = compact_iri(expand_id(str(item), context), context)
                continue
            decoded[compact_predicate(expand_term(key, context), context)] = _decode_normalized_object(item, context)
        return decoded
    if isinstance(value, list):
        return [_decode_normalized_object(item, context) for item in value]
    return value


def _triples_for_node(
    subject: str,
    document: dict[str, Any],
    context: Any,
    blank_counter: list[int],
) -> list[str]:
    triples: list[str] = []
    for key, value in document.items():
        if key in {"@context", "@id"}:
            continue
        values = value if isinstance(value, list) else [value]
        predicate = iri_token(RDF_TYPE if key == "@type" else expand_term(key, context))
        for item in values:
            object_token, nested = _object_token(item, context, blank_counter, key == "@type")
            triples.append(f"{subject} {predicate} {object_token} .")
            triples.extend(nested)
    return triples


def _object_token(
    value: Any,
    context: Any,
    blank_counter: list[int],
    is_type: bool = False,
) -> tuple[str, list[str]]:
    if is_type:
        return iri_token(expand_type(str(value), context)), []
    if isinstance(value, dict):
        node_id = value.get("@id")
        if isinstance(node_id, str):
            subject = iri_token(expand_id(node_id, context))
            nested = []
            if len(value) > 1:
                nested = _triples_for_node(subject, value, context, blank_counter)
            return subject, nested
        blank_id = f"_:pulsara{blank_counter[0]}"
        blank_counter[0] += 1
        return blank_id, _triples_for_node(blank_id, value, context, blank_counter)
    if isinstance(value, bool):
        return f'"{str(value).lower()}"^^{iri_token(XSD_BOOLEAN)}', []
    if isinstance(value, int):
        return f'"{value}"^^{iri_token(XSD_INTEGER)}', []
    return literal_token(str(value)), []


def _decode_object(value: Any, blank_values: dict[str, dict[str, list[Any]]], context: Any) -> Any:
    if isinstance(value, dict) and value.get("_type") == "bnode":
        properties = blank_values.get(str(value["id"]), {})
        return {
            compact_predicate(predicate, context): (
                [_decode_object(item, blank_values, context) for item in objects]
                if len(objects) != 1
                else _decode_object(objects[0], blank_values, context)
            )
            for predicate, objects in properties.items()
        }
    return value


def _prefixes(context: Any) -> dict[str, str]:
    mapping = context if isinstance(context, dict) else CORE_CONTEXT
    return {
        key: value
        for key, value in mapping.items()
        if isinstance(key, str) and isinstance(value, str) and value.endswith(("/", "#"))
    }
