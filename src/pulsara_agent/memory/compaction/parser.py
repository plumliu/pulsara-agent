"""Strict extraction output codec and runtime-owned normalization."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from pulsara_agent.memory.compaction.contracts import (
    CompactionMemoryExtractionOutputFact,
    CompactionMemoryPreferenceProposalFact,
)
from pulsara_agent.memory.compaction.sanitizer import contains_compaction_secret
from pulsara_agent.primitives._context_base import (
    canonical_json_bytes,
    context_fingerprint,
)


PARSER_CONTRACT_FINGERPRINT = context_fingerprint(
    "compaction-memory-extraction-parser-contract:v1",
    {
        "duplicate_keys": "reject",
        "non_finite": "reject",
        "outer_fence": "single-json-fence-only",
        "extra": "forbid",
        "collapse": "first-statement-merge-causal-evidence",
    },
)


class CompactionMemoryExtractionOutputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ParsedCompactionMemoryExtractionOutput:
    output: CompactionMemoryExtractionOutputFact
    canonical_json: bytes
    semantic_fingerprint: str


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise CompactionMemoryExtractionOutputError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_constant(value: str) -> object:
    raise CompactionMemoryExtractionOutputError(f"non-finite JSON value: {value}")


def _strip_single_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) < 3 or lines[-1].strip() != "```":
        raise CompactionMemoryExtractionOutputError("incomplete JSON code fence")
    if lines[0].strip().lower() not in {"```", "```json"}:
        raise CompactionMemoryExtractionOutputError("unsupported output code fence")
    inner = "\n".join(lines[1:-1]).strip()
    if "```" in inner:
        raise CompactionMemoryExtractionOutputError("nested output code fence")
    return inner


def parse_compaction_memory_extraction_output(
    text: str,
    *,
    allowed_evidence_node_ids: tuple[str, ...],
) -> ParsedCompactionMemoryExtractionOutput:
    """Parse exactly one output document and enforce evidence authority."""

    payload_text = _strip_single_json_fence(text)
    try:
        payload = json.loads(
            payload_text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=_reject_constant,
        )
    except CompactionMemoryExtractionOutputError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CompactionMemoryExtractionOutputError("invalid extraction JSON") from exc
    if not isinstance(payload, dict):
        raise CompactionMemoryExtractionOutputError(
            "extraction output must be an object"
        )
    try:
        parsed = TypeAdapter(CompactionMemoryExtractionOutputFact).validate_python(
            payload
        )
    except ValidationError as exc:
        raise CompactionMemoryExtractionOutputError(
            "invalid extraction output schema"
        ) from exc

    allowed = set(allowed_evidence_node_ids)
    causal_index = {item: index for index, item in enumerate(allowed_evidence_node_ids)}
    collapsed: dict[str, tuple[str, set[str]]] = {}
    order: list[str] = []
    for proposal in parsed.candidates:
        statement = unicodedata.normalize(
            "NFC", proposal.statement.replace("\r\n", "\n").replace("\r", "\n")
        ).strip()
        if not statement or len(statement.encode("utf-8")) > 1000:
            raise CompactionMemoryExtractionOutputError(
                "candidate statement is invalid"
            )
        if contains_compaction_secret(statement):
            raise CompactionMemoryExtractionOutputError(
                "candidate statement contains secret-like text"
            )
        if any(item not in allowed for item in proposal.evidence_node_ids):
            raise CompactionMemoryExtractionOutputError(
                "candidate cites unknown evidence"
            )
        if (
            tuple(sorted(proposal.evidence_node_ids, key=causal_index.__getitem__))
            != proposal.evidence_node_ids
        ):
            raise CompactionMemoryExtractionOutputError(
                "candidate evidence is not in causal order"
            )
        semantic = context_fingerprint(
            "compaction-memory-output-proposal-semantic:v1",
            {"kind": "Preference", "statement": statement},
        )
        if semantic not in collapsed:
            collapsed[semantic] = (statement, set())
            order.append(semantic)
        collapsed[semantic][1].update(proposal.evidence_node_ids)

    normalized: list[CompactionMemoryPreferenceProposalFact] = []
    for semantic in order:
        statement, refs = collapsed[semantic]
        ordered_refs = tuple(sorted(refs, key=causal_index.__getitem__))
        if len(ordered_refs) > 8:
            raise CompactionMemoryExtractionOutputError(
                "collapsed evidence exceeds hard bound"
            )
        normalized.append(
            CompactionMemoryPreferenceProposalFact(
                statement=statement,
                evidence_node_ids=ordered_refs,
            )
        )
    output = CompactionMemoryExtractionOutputFact(candidates=tuple(normalized))
    encoded = canonical_json_bytes(output.model_dump(mode="json"))
    return ParsedCompactionMemoryExtractionOutput(
        output=output,
        canonical_json=encoded,
        semantic_fingerprint=context_fingerprint(
            "compaction-memory-extraction-output-semantic:v1",
            output.model_dump(mode="json"),
        ),
    )


__all__ = [
    "PARSER_CONTRACT_FINGERPRINT",
    "CompactionMemoryExtractionOutputError",
    "ParsedCompactionMemoryExtractionOutput",
    "parse_compaction_memory_extraction_output",
]
