"""Oxigraph-backed GraphStore implementation."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from pulsara_agent.graph.jsonld_codec import (
    RDF_TYPE,
    binding_to_jsonld as _binding_to_jsonld,
    document_from_rows as _document_from_rows,
    expand_graph_id as _expand_graph_id,
    expand_id as _expand_id,
    expand_type as _expand_type,
    graph_key as _graph_key,
    iri_token as _iri_token,
    triples_for_document as _triples_for_document,
)
from pulsara_agent.ontology.registry import CORE_CONTEXT


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
