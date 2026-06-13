"""Oxigraph-backed GraphStore implementation."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pulsara_agent.graph.store import DEFAULT_GRAPH_ID
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
    capability.PROVIDES_TOOL.name,
    capability.PROVIDES_SKILL.name,
    capability.REQUIRES.name,
}


@dataclass(slots=True)
class OxigraphGraphStore:
    """HTTP SPARQL GraphStore for a local or remote Oxigraph server."""

    base_url: str = "http://localhost:7878"
    timeout_seconds: float = 10.0
    default_context: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        if self.default_context is None:
            self.default_context = CORE_CONTEXT

    def put_jsonld(self, document: dict[str, Any], graph_id: str | None = None) -> None:
        node_id = document.get("@id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("JSON-LD document must include a string @id")
        context = document.get("@context") or self.default_context
        subject = _iri_token(_expand_id(node_id, context))
        graph = _iri_token(_expand_graph_id(_graph_key(graph_id), self.default_context))
        triples = _triples_for_document(document, self.default_context)
        update = f"DELETE WHERE {{ GRAPH {graph} {{ {subject} ?p ?o . }} }}"
        if triples:
            update += ";\nINSERT DATA { GRAPH " + graph + " {\n" + "\n".join(triples) + "\n} }"
        self.update(update)

    def get_jsonld(self, node_id: str, graph_id: str | None = None) -> dict[str, Any]:
        graph_key = _graph_key(graph_id)
        subject_iri = _expand_id(node_id, self.default_context)
        sparql = f"""
SELECT ?p ?o ?bp ?bo WHERE {{
  GRAPH {_iri_token(_expand_graph_id(graph_key, self.default_context))} {{
    {_iri_token(subject_iri)} ?p ?o .
    OPTIONAL {{
      ?o ?bp ?bo .
      FILTER(isBlank(?o))
    }}
  }}
}}
"""
        rows = self.query(sparql)
        if not rows:
            raise KeyError(node_id)
        return _document_from_rows(subject_iri, rows, self.default_context)

    def has_jsonld(self, node_id: str, graph_id: str | None = None) -> bool:
        graph_key = _graph_key(graph_id)
        sparql = f"""
ASK {{
  GRAPH {_iri_token(_expand_graph_id(graph_key, self.default_context))} {{
    {_iri_token(_expand_id(node_id, self.default_context))} ?p ?o .
  }}
}}
"""
        result = self._sparql_query(sparql)
        return bool(result.get("boolean"))

    def find_by_type(self, type_name, graph_id: str | None = None) -> list[dict[str, Any]]:
        graph_key = _graph_key(graph_id)
        type_iri = getattr(type_name, "value", None) or _expand_type(str(type_name.name), self.default_context)
        sparql = f"""
SELECT ?s WHERE {{
  GRAPH {_iri_token(_expand_graph_id(graph_key, self.default_context))} {{
    ?s {_iri_token(RDF_TYPE)} {_iri_token(type_iri)} .
  }}
}}
"""
        rows = self.query(sparql)
        documents: list[dict[str, Any]] = []
        for row in rows:
            subject = row.get("s")
            if isinstance(subject, dict) and "@id" in subject:
                documents.append(self.get_jsonld(str(subject["@id"]), graph_id=graph_key))
        return documents

    def query(self, sparql: str, bindings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if bindings:
            raise NotImplementedError("OxigraphGraphStore does not yet support query bindings")
        result = self._sparql_query(sparql)
        return [
            {name: _binding_to_jsonld(binding, self.default_context) for name, binding in row.items()}
            for row in result.get("results", {}).get("bindings", [])
        ]

    def update(self, sparql: str) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/update",
            data=sparql.encode("utf-8"),
            headers={"Content-Type": "application/sparql-update"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status not in {200, 204}:
                    raise RuntimeError(f"Oxigraph update failed with status {response.status}")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Oxigraph update failed with status {exc.code}: {body}") from exc

    def delete_graph(self, graph_id: str) -> None:
        self.update(f"DROP SILENT GRAPH {_iri_token(_expand_graph_id(_graph_key(graph_id), self.default_context))}")

    def _sparql_query(self, sparql: str) -> dict[str, Any]:
        data = urllib.parse.urlencode({"query": sparql}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/query",
            data=data,
            headers={
                "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Oxigraph query failed with status {exc.code}: {body}") from exc
        return json.loads(payload)


def _triples_for_document(document: dict[str, Any], default_context: Any) -> list[str]:
    context = document.get("@context") or default_context
    subject = _iri_token(_expand_id(str(document["@id"]), context))
    blank_counter = [0]
    return _triples_for_node(subject, document, context, blank_counter)


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
        predicate = _iri_token(RDF_TYPE if key == "@type" else _expand_term(key, context))
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
        return _iri_token(_expand_type(str(value), context)), []
    if isinstance(value, dict):
        node_id = value.get("@id")
        if isinstance(node_id, str):
            subject = _iri_token(_expand_id(node_id, context))
            nested = []
            if len(value) > 1:
                nested = _triples_for_node(subject, value, context, blank_counter)
            return subject, nested
        blank_id = f"_:pulsara{blank_counter[0]}"
        blank_counter[0] += 1
        return blank_id, _triples_for_node(blank_id, value, context, blank_counter)
    if isinstance(value, bool):
        return f'"{str(value).lower()}"^^{_iri_token(XSD_BOOLEAN)}', []
    if isinstance(value, int):
        return f'"{value}"^^{_iri_token(XSD_INTEGER)}', []
    return _literal_token(str(value)), []


def _document_from_rows(subject_iri: str, rows: list[dict[str, Any]], context: Any) -> dict[str, Any]:
    values: dict[str, list[Any]] = {}
    blank_values: dict[str, dict[str, list[Any]]] = {}
    seen_main: set[tuple[str, str]] = set()

    for row in rows:
        predicate = _row_iri(row["p"], context)
        obj = row["o"]
        object_key = json.dumps(obj, sort_keys=True)
        if (predicate, object_key) not in seen_main:
            seen_main.add((predicate, object_key))
            values.setdefault(predicate, []).append(obj)
        if isinstance(obj, dict) and obj.get("_type") == "bnode" and "bp" in row and "bo" in row:
            blank_values.setdefault(str(obj["id"]), {}).setdefault(_row_iri(row["bp"], context), []).append(row["bo"])

    document: dict[str, Any] = {
        "@context": deepcopy(context),
        "@id": _compact_iri(subject_iri, context),
    }
    for predicate, objects in values.items():
        if predicate == RDF_TYPE:
            document["@type"] = [_compact_type(_row_iri(obj, context), context) for obj in objects]
            continue
        key = _compact_predicate(predicate, context)
        decoded = [
            _decode_object(obj, blank_values, context)
            for obj in objects
        ]
        document[key] = decoded if len(decoded) != 1 or key in FORCE_LIST_KEYS else decoded[0]
    return document


def _decode_object(value: Any, blank_values: dict[str, dict[str, list[Any]]], context: Any) -> Any:
    if isinstance(value, dict) and value.get("_type") == "bnode":
        properties = blank_values.get(str(value["id"]), {})
        return {
            _compact_predicate(predicate, context): (
                [_decode_object(item, blank_values, context) for item in objects]
                if len(objects) != 1
                else _decode_object(objects[0], blank_values, context)
            )
            for predicate, objects in properties.items()
        }
    return value


def _binding_to_jsonld(binding: dict[str, Any], context: Any) -> Any:
    binding_type = binding.get("type")
    if binding_type == "uri":
        return {"@id": _compact_iri(str(binding["value"]), context)}
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


def _row_iri(value: Any, context: Any) -> str:
    if isinstance(value, dict) and "@id" in value:
        return _expand_id(str(value["@id"]), context)
    if isinstance(value, str):
        return value
    raise TypeError(f"Expected IRI binding, got {value!r}")


def _expand_graph_id(graph_id: str, context: Any) -> str:
    if graph_id.startswith("graph:"):
        return GRAPH_BASE + urllib.parse.quote(graph_id.split(":", 1)[1], safe="/")
    return _expand_id(graph_id, context)


def _graph_key(graph_id: str | None) -> str:
    if graph_id is None:
        return DEFAULT_GRAPH_ID
    if not graph_id:
        raise ValueError("graph_id must be a non-empty string or None")
    return graph_id


def _expand_id(identifier: str, context: Any) -> str:
    if "://" in identifier or identifier.startswith("urn:"):
        return identifier
    prefix, sep, suffix = identifier.partition(":")
    prefixes = _prefixes(context)
    if sep and prefix in prefixes:
        return prefixes[prefix] + suffix
    return "urn:pulsara:" + urllib.parse.quote(identifier, safe="")


def _expand_type(type_name: str, context: Any) -> str:
    mapping = context if isinstance(context, dict) else CORE_CONTEXT
    value = mapping.get(type_name)
    if isinstance(value, str) and ("://" in value or value.startswith("urn:")):
        return value
    return _expand_id(type_name, context)


def _expand_term(term_name: str, context: Any) -> str:
    mapping = context if isinstance(context, dict) else CORE_CONTEXT
    value = mapping.get(term_name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("@id"), str):
        return str(value["@id"])
    return _expand_id(term_name, context)


def _compact_iri(iri: str, context: Any) -> str:
    for prefix, base in sorted(_prefixes(context).items(), key=lambda item: len(item[1]), reverse=True):
        if iri.startswith(base):
            return f"{prefix}:{iri[len(base):]}"
    if iri.startswith(GRAPH_BASE):
        return f"graph:{iri[len(GRAPH_BASE):]}"
    return iri


def _compact_type(iri: str, context: Any) -> str:
    mapping = context if isinstance(context, dict) else CORE_CONTEXT
    for key, value in mapping.items():
        if isinstance(value, str) and value == iri:
            return str(key)
    return _compact_iri(iri, context)


def _compact_predicate(iri: str, context: Any) -> str:
    mapping = context if isinstance(context, dict) else CORE_CONTEXT
    for key, value in mapping.items():
        if isinstance(value, str) and value == iri:
            return str(key)
        if isinstance(value, dict) and value.get("@id") == iri:
            return str(key)
    return _compact_iri(iri, context)


def _prefixes(context: Any) -> dict[str, str]:
    mapping = context if isinstance(context, dict) else CORE_CONTEXT
    return {
        key: value
        for key, value in mapping.items()
        if isinstance(key, str) and isinstance(value, str) and value.endswith(("/", "#"))
    }


def _iri_token(iri: str) -> str:
    return f"<{iri}>"


def _literal_token(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
