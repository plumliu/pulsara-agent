"""Memory-candidate extraction helpers for context compaction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pulsara_agent.event.candidates import PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.event.events import ContextCompactionCompletedEvent
from pulsara_agent.memory.candidates.pool import CandidateOrigin, CandidatePool, PooledMemoryCandidate
from pulsara_agent.memory.candidates.pool import candidate_payload_fingerprint
from pulsara_agent.memory.scope import MemoryDomainContext, workspace_scope
from pulsara_agent.ontology import memory
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.governance_evidence import (
    CompactionCandidateAttributionFact,
    CompactionMemoryCandidateExtractorContractFact,
)


@dataclass(frozen=True, slots=True)
class ContextCompactionMemoryCandidatePolicy:
    enabled: bool = True
    extract_on_manual: bool = True
    extract_on_preflight: bool = True
    extract_on_mid_turn: bool = False
    missing_candidates_block_policy: Literal["ignore", "diagnostic"] = "ignore"
    max_candidates_per_compaction: int = 3
    max_summary_excerpt_chars: int = 2_000
    max_provenance_ids: int = 5
    extractor_version: str = "compaction-memory-candidates:v1"


@dataclass(frozen=True, slots=True)
class CompactionCandidateDiagnostic:
    code: str
    field: str | None = None
    message: str = ""
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class CompactionCandidateSkippedItem:
    code: str
    reason: str
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedCompactionCandidate:
    payload: ValidCandidatePayload
    intent_fingerprint: str
    raw_index: int


@dataclass(frozen=True, slots=True)
class CompactionCandidateParseResult:
    attempted_count: int
    candidates: tuple[NormalizedCompactionCandidate, ...]
    skipped: tuple[CompactionCandidateSkippedItem, ...]
    diagnostics: tuple[CompactionCandidateDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class CompactionCandidateAppendResult:
    source_event_id: str
    source_event_sequence: int
    source_artifact_id: str
    entry_ids: tuple[str, ...]
    duplicate_count: int = 0
    skipped: tuple[CompactionCandidateSkippedItem, ...] = ()
    diagnostics: tuple[CompactionCandidateDiagnostic, ...] = ()
    candidates: tuple[PooledMemoryCandidate, ...] = ()
    attributions: tuple[CompactionCandidateAttributionFact, ...] = ()


class CompactionMemoryCandidateSink(Protocol):
    @property
    def workspace_scope(self) -> str | None: ...

    @property
    def workspace_kind(self) -> str: ...

    def prepare_compaction_candidates(
        self,
        *,
        completed_event: ContextCompactionCompletedEvent,
        summary_artifact_id: str,
        summary_text: str,
        parse_result: CompactionCandidateParseResult,
        policy: ContextCompactionMemoryCandidatePolicy,
    ) -> CompactionCandidateAppendResult: ...


@dataclass(frozen=True, slots=True)
class CandidatePoolCompactionMemoryCandidateSink:
    candidate_pool: CandidatePool
    memory_domain: MemoryDomainContext
    runtime_session_id: str

    @property
    def workspace_scope(self) -> str | None:
        if self.memory_domain.workspace_kind != "project":
            return None
        assert self.memory_domain.stable_project_key is not None
        return workspace_scope(self.memory_domain.stable_project_key)

    @property
    def workspace_kind(self) -> str:
        return self.memory_domain.workspace_kind

    def prepare_compaction_candidates(
        self,
        *,
        completed_event: ContextCompactionCompletedEvent,
        summary_artifact_id: str,
        summary_text: str,
        parse_result: CompactionCandidateParseResult,
        policy: ContextCompactionMemoryCandidatePolicy,
    ) -> CompactionCandidateAppendResult:
        source_event_sequence = int(completed_event.sequence or 0)
        diagnostics: list[CompactionCandidateDiagnostic] = []
        if completed_event.sequence is None:
            diagnostics.append(
                CompactionCandidateDiagnostic(
                    code="compaction_candidate_source_event_sequence_missing",
                    message="Completed compaction event was not stored with a sequence before candidate append.",
                )
            )
        metadata_base = _candidate_metadata_base(
            completed_event=completed_event,
            summary_artifact_id=summary_artifact_id,
            summary_text=summary_text,
            policy=policy,
        )
        pending_fingerprints = {
            candidate.intent_fingerprint
            for candidate in self.candidate_pool.list_pending()
            if candidate.origin is CandidateOrigin.COMPACTION
            and candidate.source_session_id == self.runtime_session_id
            and candidate.intent_fingerprint
        }
        candidates: list[PooledMemoryCandidate] = []
        attributions: list[CompactionCandidateAttributionFact] = []
        skipped: list[CompactionCandidateSkippedItem] = []
        duplicate_count = 0
        for normalized in parse_result.candidates:
            if normalized.intent_fingerprint in pending_fingerprints:
                duplicate_count += 1
                skipped.append(
                    CompactionCandidateSkippedItem(
                        code="duplicate_pending_compaction_candidate",
                        reason="A pending compaction candidate with the same intent fingerprint already exists.",
                    )
                )
                continue
            metadata = {
                **metadata_base,
                "intent_fingerprint": normalized.intent_fingerprint,
                "raw_candidate_index": normalized.raw_index,
            }
            payload_fingerprint = candidate_payload_fingerprint(normalized.payload)
            entry_id = _compaction_candidate_entry_id(
                completed_event=completed_event,
                raw_index=normalized.raw_index,
                intent_fingerprint=normalized.intent_fingerprint,
            )
            pooled = PooledMemoryCandidate(
                entry_id=entry_id,
                payload=normalized.payload,
                origin=CandidateOrigin.COMPACTION,
                source_session_id=self.runtime_session_id,
                source_run_id=completed_event.run_id,
                source_turn_id=completed_event.turn_id,
                source_reply_id=completed_event.reply_id,
                source_event_id=completed_event.id,
                source_artifact_id=summary_artifact_id,
                intent_fingerprint=normalized.intent_fingerprint,
                metadata=metadata,
            )
            pending_fingerprints.add(normalized.intent_fingerprint)
            candidates.append(pooled)
            attributions.append(
                build_frozen_fact(
                    CompactionCandidateAttributionFact,
                    schema_version="compaction_candidate_attribution.v1",
                    candidate_entry_id=entry_id,
                    raw_candidate_index=normalized.raw_index,
                    candidate_payload=normalized.payload,
                    candidate_payload_fingerprint=payload_fingerprint,
                    intent_fingerprint=normalized.intent_fingerprint,
                )
            )
        return CompactionCandidateAppendResult(
            source_event_id=completed_event.id,
            source_event_sequence=source_event_sequence,
            source_artifact_id=summary_artifact_id,
            entry_ids=tuple(candidate.entry_id for candidate in candidates),
            duplicate_count=duplicate_count,
            skipped=tuple(skipped),
            diagnostics=tuple(diagnostics),
            candidates=tuple(candidates),
            attributions=tuple(attributions),
        )


_MEMORY_CANDIDATES_RE = re.compile(
    r"<memory_candidates_json>([\s\S]*?)</memory_candidates_json>",
    re.IGNORECASE,
)
_SECRET_LIKE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(api[_-]?key|password|token|secret|authorization)\b\s*[:=])",
    re.IGNORECASE,
)


def parse_compaction_memory_candidates(
    raw_text: str,
    *,
    workspace_scope: str | None,
    workspace_kind: str = "project",
    policy: ContextCompactionMemoryCandidatePolicy | None = None,
) -> CompactionCandidateParseResult:
    """Parse optional compaction memory candidate proposals.

    The parser is deliberately conservative: V1 only accepts Preference
    candidates, forces project workspace scope, and downgrades all authority to
    conversation_evidence/inferred.
    """

    effective_policy = policy or ContextCompactionMemoryCandidatePolicy()
    if not effective_policy.enabled:
        return CompactionCandidateParseResult(attempted_count=0, candidates=(), skipped=(), diagnostics=())
    if workspace_kind == "transient":
        return CompactionCandidateParseResult(
            attempted_count=0,
            candidates=(),
            skipped=(),
            diagnostics=(
                CompactionCandidateDiagnostic(
                    code="compaction_candidates_disabled_for_transient_workspace",
                    message="Transient workspaces do not produce compaction memory candidates in V1.",
                ),
            ),
        )
    if not workspace_scope:
        return CompactionCandidateParseResult(
            attempted_count=0,
            candidates=(),
            skipped=(),
            diagnostics=(
                CompactionCandidateDiagnostic(
                    code="compaction_candidate_workspace_scope_missing",
                    message="Project-scoped compaction candidates require a workspace scope.",
                ),
            ),
        )

    match = _MEMORY_CANDIDATES_RE.search(raw_text)
    if match is None:
        diagnostics = ()
        if effective_policy.missing_candidates_block_policy == "diagnostic":
            diagnostics = (
                CompactionCandidateDiagnostic(
                    code="compaction_candidate_block_missing",
                    message="Compaction candidate extraction was enabled but no memory candidate block was present.",
                ),
            )
        return CompactionCandidateParseResult(attempted_count=0, candidates=(), skipped=(), diagnostics=diagnostics)

    try:
        parsed = json.loads(match.group(1).strip())
    except json.JSONDecodeError:
        return CompactionCandidateParseResult(
            attempted_count=0,
            candidates=(),
            skipped=(),
            diagnostics=(
                CompactionCandidateDiagnostic(
                    code="compaction_candidate_json_malformed",
                    message="Compaction memory candidate JSON could not be parsed.",
                ),
            ),
        )

    raw_candidates = parsed.get("candidates") if isinstance(parsed, dict) else None
    if not isinstance(raw_candidates, list):
        return CompactionCandidateParseResult(
            attempted_count=0,
            candidates=(),
            skipped=(),
            diagnostics=(
                CompactionCandidateDiagnostic(
                    code="compaction_candidate_candidates_not_list",
                    field="candidates",
                    message="Compaction candidate payload must contain a candidates list.",
                ),
            ),
        )

    diagnostics: list[CompactionCandidateDiagnostic] = []
    skipped: list[CompactionCandidateSkippedItem] = []
    candidates: list[NormalizedCompactionCandidate] = []
    attempted_count = len(raw_candidates)

    for index, raw_candidate in enumerate(raw_candidates):
        if len(candidates) >= effective_policy.max_candidates_per_compaction:
            skipped.append(
                CompactionCandidateSkippedItem(
                    code="compaction_candidate_limit_exceeded",
                    reason="Candidate skipped because max_candidates_per_compaction was reached.",
                )
            )
            continue
        if not isinstance(raw_candidate, dict):
            skipped.append(
                CompactionCandidateSkippedItem(
                    code="compaction_candidate_not_object",
                    reason="Candidate item was not an object.",
                )
            )
            continue
        kind = str(raw_candidate.get("kind") or "Preference")
        if kind != "Preference":
            skipped.append(
                CompactionCandidateSkippedItem(
                    code="compaction_candidate_kind_not_supported",
                    reason="V1 compaction candidate extraction only accepts Preference candidates.",
                )
            )
            continue
        statement_value = raw_candidate.get("statement")
        if not isinstance(statement_value, str) or not statement_value.strip():
            skipped.append(
                CompactionCandidateSkippedItem(
                    code="compaction_candidate_statement_missing",
                    reason="Preference candidate statement was missing or empty.",
                )
            )
            continue
        statement = statement_value.strip()
        if _looks_secret_like(statement) or _looks_secret_like(str(raw_candidate.get("reason") or "")):
            skipped.append(
                CompactionCandidateSkippedItem(
                    code="compaction_candidate_secret_like_content",
                    reason="Candidate contained secret-like content and was redacted.",
                    redacted=True,
                )
            )
            diagnostics.append(
                CompactionCandidateDiagnostic(
                    code="compaction_candidate_secret_like_content",
                    field="statement",
                    message="Secret-like candidate content was skipped and redacted.",
                    redacted=True,
                )
            )
            continue
        payload = ValidCandidatePayload(
            candidate=PreferenceCandidate(
                candidate_id=_compaction_payload_candidate_id(
                    raw_index=index,
                    item=raw_candidate,
                    extractor_version=effective_policy.extractor_version,
                ),
                statement=statement,
                scope=workspace_scope,
                evidence_ids=(),
                source_authority=memory.SourceAuthority.CONVERSATION_EVIDENCE,
                verification_status=memory.VerificationStatus.INFERRED,
            )
        )
        candidates.append(
            NormalizedCompactionCandidate(
                payload=payload,
                intent_fingerprint=_intent_fingerprint(
                    origin="compaction",
                    scope=workspace_scope,
                    kind="Preference",
                    statement=statement,
                    extractor_version=effective_policy.extractor_version,
                ),
                raw_index=index,
            )
        )

    return CompactionCandidateParseResult(
        attempted_count=attempted_count,
        candidates=tuple(candidates),
        skipped=tuple(skipped),
        diagnostics=tuple(diagnostics),
    )


def _looks_secret_like(value: str) -> bool:
    return bool(_SECRET_LIKE_RE.search(value))


def compaction_extractor_contract(
    policy: ContextCompactionMemoryCandidatePolicy,
) -> CompactionMemoryCandidateExtractorContractFact:
    return build_frozen_fact(
        CompactionMemoryCandidateExtractorContractFact,
        schema_version="compaction_memory_candidate_extractor_contract.v1",
        extractor_id="pulsara.compaction_memory_candidates",
        extractor_version=policy.extractor_version,
        accepted_input_schema_fingerprint=context_fingerprint(
            "compaction-memory-candidate-input-schema:v1",
            "summary_memory_candidates_json",
        ),
        output_candidate_schema_fingerprint=context_fingerprint(
            "compaction-memory-candidate-output-schema:v1",
            "valid_preference_candidate",
        ),
        parsing_rules_fingerprint=context_fingerprint(
            "compaction-memory-candidate-parsing:v1",
            {
                "kind": "Preference",
                "secret_filter": "secret-like-v1",
                "max_candidates": policy.max_candidates_per_compaction,
            },
        ),
        normalization_rules_fingerprint=context_fingerprint(
            "compaction-memory-candidate-normalization:v1",
            "trim+conversation_evidence+inferred",
        ),
    )


def _compaction_payload_candidate_id(
    *,
    raw_index: int,
    item: dict[str, Any],
    extractor_version: str,
) -> str:
    payload = {key: value for key, value in item.items() if key != "candidate_id"}
    digest = context_fingerprint(
        "compaction-candidate-id:v1",
        (extractor_version, raw_index, payload),
    ).removeprefix("sha256:")
    return f"candidate:compaction:{digest}"


def _compaction_candidate_entry_id(
    *,
    completed_event: ContextCompactionCompletedEvent,
    raw_index: int,
    intent_fingerprint: str,
) -> str:
    digest = context_fingerprint(
        "compaction-candidate-entry:v1",
        (completed_event.compaction_id, raw_index, intent_fingerprint),
    ).removeprefix("sha256:")
    return f"pool:compaction:{digest}"


def _intent_fingerprint(
    *,
    origin: str,
    scope: str,
    kind: str,
    statement: str,
    extractor_version: str,
) -> str:
    normalized = {
        "origin": origin,
        "scope": scope,
        "kind": kind,
        "statement": " ".join(statement.lower().split()),
        "extractor_version": extractor_version,
    }
    encoded = json.dumps(normalized, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _candidate_metadata_base(
    *,
    completed_event: ContextCompactionCompletedEvent,
    summary_artifact_id: str,
    summary_text: str,
    policy: ContextCompactionMemoryCandidatePolicy,
) -> dict[str, Any]:
    included_run_ids, included_run_truncated = _bounded_tuple(
        completed_event.included_run_ids,
        policy.max_provenance_ids,
    )
    included_artifact_ids, included_artifact_truncated = _bounded_tuple(
        completed_event.included_artifact_ids,
        policy.max_provenance_ids,
    )
    summary_excerpt, summary_excerpt_truncated = _clip_text(summary_text, policy.max_summary_excerpt_chars)
    return {
        "source": "context_compaction",
        "compaction_id": completed_event.compaction_id,
        "trigger": completed_event.trigger,
        "reason": completed_event.reason,
        "window_id": completed_event.window_id,
        "window_number": completed_event.window_number,
        "through_sequence": completed_event.through_sequence,
        "keep_after_sequence": completed_event.keep_after_sequence,
        "included_run_ids": list(included_run_ids),
        "included_run_count": len(completed_event.included_run_ids),
        "included_run_ids_truncated": included_run_truncated,
        "included_artifact_ids": list(included_artifact_ids),
        "included_artifact_count": len(completed_event.included_artifact_ids),
        "included_artifact_ids_truncated": included_artifact_truncated,
        "summary_artifact_id": summary_artifact_id,
        "summary_excerpt": summary_excerpt,
        "summary_excerpt_chars": len(summary_excerpt),
        "summary_excerpt_truncated": summary_excerpt_truncated,
        "source_event_id": completed_event.id,
        "source_event_sequence": completed_event.sequence,
        "candidate_extractor_version": policy.extractor_version,
    }


def _bounded_tuple(values: list[str] | tuple[str, ...], max_items: int) -> tuple[tuple[str, ...], bool]:
    if max_items < 0:
        max_items = 0
    return tuple(values[:max_items]), len(values) > max_items


def _clip_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        return "", bool(text)
    if len(text) <= max_chars:
        return text, False
    marker = f"\n[SUMMARY EXCERPT TRUNCATED: kept {max_chars} of {len(text)} chars]"
    if len(marker) >= max_chars:
        return marker[:max_chars], True
    return text[: max_chars - len(marker)] + marker, True
