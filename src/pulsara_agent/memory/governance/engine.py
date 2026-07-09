"""Flash-backed memory governance decision engine."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pulsara_agent.event import EventContext, RunErrorEvent, TextBlockDeltaEvent
from pulsara_agent.event.candidates import ValidCandidatePayload
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    GovernanceDecision,
    MemoryGovernanceDecisionRecord,
    PooledMemoryCandidate,
    decision_target_entry_ids,
    new_governance_batch_id,
)
from pulsara_agent.memory.governance.dedupe import candidate_fingerprint
from pulsara_agent.memory.governance.executor import MemoryGovernanceApplyResult, MemoryGovernanceExecutor
from pulsara_agent.memory.governance.relatedness import (
    GovernanceRelatednessService,
    RelatednessAvailability,
    RelatednessBatchResult,
    RelatednessExecutionContext,
)
from pulsara_agent.memory.scope import format_scope_list
from pulsara_agent.ontology import memory


class MemoryGovernanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_session_id: str
    governance_batch_id: str
    trigger_reason: str
    candidates: list[dict[str, Any]]
    allowed_scopes: list[str] = Field(default_factory=list)


class MemoryGovernanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = ""
    decisions: list[GovernanceDecision] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MemoryGovernanceOptions:
    model_role: ModelRole = ModelRole.FLASH
    llm_options: LLMOptions = field(
        default_factory=lambda: LLMOptions(temperature=0, max_output_tokens=1_024)
    )
    limit: int = 20


@dataclass(frozen=True, slots=True)
class MemoryGovernanceRunResult:
    governance_batch_id: str
    decisions: list[GovernanceDecision]
    applied: list[MemoryGovernanceApplyResult]
    relatedness_diagnostics: dict[str, Any] = field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None


@dataclass(slots=True)
class MemoryGovernanceEngine:
    """Restricted Flash governance layer.

    The engine only decides. It does not write GraphStore directly; every
    decision is applied by ``MemoryGovernanceExecutor``.
    """

    llm_runtime: LLMRuntime
    executor: MemoryGovernanceExecutor
    options: MemoryGovernanceOptions = field(default_factory=MemoryGovernanceOptions)
    relatedness_service: GovernanceRelatednessService | None = None

    async def run_pending(
        self,
        *,
        trigger_reason: str,
        governance_batch_id: str | None = None,
        limit: int | None = None,
    ) -> MemoryGovernanceRunResult:
        batch_id = governance_batch_id or new_governance_batch_id()
        pending = [
            candidate
            for candidate in self.executor.candidate_pool.list_pending()
            if candidate.source_session_id == self.executor.runtime_session_id
        ][: limit or self.options.limit]
        if not pending:
            return MemoryGovernanceRunResult(governance_batch_id=batch_id, decisions=[], applied=[])

        if self.relatedness_service is not None:
            try:
                relatedness = await self.relatedness_service.collect_batch(
                    pending,
                    graph_id=self.executor.graph_id,
                )
            except Exception as exc:
                relatedness = RelatednessBatchResult.unavailable(
                    pending,
                    warning=f"relatedness_service_failed:{type(exc).__name__}",
                )
            snapshots = [
                self._candidate_snapshot(candidate, relatedness=relatedness)
                for candidate in pending
            ]
            execution_context = relatedness.execution_context(batch_id)
            relatedness_diagnostics = dict(relatedness.diagnostics)
        else:
            # Product wiring always supplies the Postgres-backed service.  This
            # compatibility path keeps in-memory governance useful as a test double.
            snapshots = [self._candidate_snapshot(candidate) for candidate in pending]
            execution_context = _legacy_execution_context(batch_id, snapshots)
            relatedness_diagnostics = {"mode": "in_memory_test_double"}

        governance_input = MemoryGovernanceInput(
            runtime_session_id=self.executor.runtime_session_id,
            governance_batch_id=batch_id,
            trigger_reason=trigger_reason,
            candidates=snapshots,
            allowed_scopes=sorted(self.executor.allowed_write_scopes),
        )
        try:
            output = _parse_governance_output(await self._call_flash(governance_input))
        except Exception as exc:
            return MemoryGovernanceRunResult(
                governance_batch_id=batch_id,
                decisions=[],
                applied=[],
                relatedness_diagnostics=relatedness_diagnostics,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

        applied: list[MemoryGovernanceApplyResult] = []
        try:
            for decision in output.decisions:
                applied.append(
                    self.executor.apply_decision(
                        decision,
                        governance_batch_id=batch_id,
                        relatedness_context=execution_context,
                    )
                )
        except Exception as exc:
            return MemoryGovernanceRunResult(
                governance_batch_id=batch_id,
                decisions=output.decisions,
                applied=applied,
                relatedness_diagnostics=relatedness_diagnostics,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        return MemoryGovernanceRunResult(
            governance_batch_id=batch_id,
            decisions=output.decisions,
            applied=applied,
            relatedness_diagnostics=relatedness_diagnostics,
        )

    async def _call_flash(self, governance_input: MemoryGovernanceInput) -> str:
        text_parts: list[str] = []
        async for event in self.llm_runtime.stream(
            role=self.options.model_role,
            context=LLMContext(
                system_prompt=_governance_system_prompt(self.executor.allowed_write_scopes),
                messages=(
                    LLMMessage.user(
                        "Govern these memory candidates and output JSON only:\n"
                        + json.dumps(governance_input.model_dump(mode="json"), ensure_ascii=False)
                    ),
                ),
                tools=(),
            ),
            event_context=EventContext(
                run_id=f"run:governance-planner/{governance_input.governance_batch_id}",
                turn_id=f"turn:governance-planner/{governance_input.governance_batch_id}",
                reply_id=f"reply:governance-planner/{governance_input.governance_batch_id}",
            ),
            options=self.options.llm_options,
        ):
            if isinstance(event, TextBlockDeltaEvent):
                text_parts.append(event.delta)
            elif isinstance(event, RunErrorEvent):
                raise RuntimeError(event.message)
        return "".join(text_parts)

    def _candidate_snapshot(
        self,
        candidate: PooledMemoryCandidate,
        *,
        relatedness: RelatednessBatchResult | None = None,
    ) -> dict[str, Any]:
        if candidate.origin is CandidateOrigin.COMPACTION:
            source_events = _compaction_source_events(self.executor.event_log, candidate)
        else:
            source_events = self.executor.event_log.iter(run_id=candidate.source_run_id)
        decisions = [
            decision
            for decision in self.executor.candidate_pool.list_decisions()
            if candidate.entry_id in decision_target_entry_ids(decision.decision)
        ]
        snapshot = candidate.model_dump(mode="json")
        snapshot["source_events"] = _source_event_summaries(source_events)
        if candidate.origin is CandidateOrigin.COMPACTION:
            snapshot["compaction_evidence_view"] = _compaction_evidence_view(candidate, source_events)
            snapshot["attribution_context"] = {
                "source_run_id": candidate.source_run_id,
                "source_turn_id": candidate.source_turn_id,
                "source_reply_id": candidate.source_reply_id,
            }
        snapshot["prior_governance_decisions"] = _decision_summaries(decisions)
        if relatedness is None:
            snapshot["related_existing_memories"] = _related_existing_memories(
                candidate,
                self.executor.graph,
                graph_id=self.executor.graph_id,
            )
            snapshot["relatedness_lifecycle_actions_allowed"] = bool(
                snapshot["related_existing_memories"]
            )
        else:
            candidate_relatedness = relatedness.for_candidate(candidate.entry_id)
            snapshot["related_existing_memories"] = candidate_relatedness.prompt_view()
            snapshot["relatedness_lifecycle_actions_allowed"] = (
                candidate_relatedness.availability is RelatednessAvailability.FULL
            )
        if isinstance(candidate.payload, ValidCandidatePayload):
            snapshot["content_key"] = candidate_fingerprint(candidate.payload.candidate)
        return snapshot


def _parse_governance_output(text: str) -> MemoryGovernanceOutput:
    payload = json.loads(_json_object_text(text))
    return MemoryGovernanceOutput.model_validate(payload)


def _json_object_text(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Memory governance response did not contain a JSON object")
    return stripped[start : end + 1]


_GOVERNANCE_SYSTEM_PROMPT = """
You are Pulsara's restricted Memory Governance Agent.

You review pending memory candidate-pool entries and output governance decisions
as JSON only. You are not the main assistant. You do not call tools. You do not
write memory directly. The host will validate your decisions and apply them via
MemoryWriteService, MemoryWriteGate, and the ledger.

Return this shape:
{
  "reason": "short summary of the batch decision",
  "decisions": [
    {"kind": "submit_as_is", "target_entry_id": "pool:...", "reason": "..."},
    {"kind": "supersede_and_submit", "target_entry_id": "pool:...", "candidate": {...}, "superseded_memory_ids": ["preference:..."], "replacement_evidence_refs": ["candidate_user_quote"], "reason": "..."},
    {"kind": "contradict_and_submit", "target_entry_id": "pool:...", "candidate": {...}, "contradicted_memory_ids": ["preference:..."], "reason": "..."},
    {"kind": "skip", "target_entry_ids": ["pool:..."], "reason": "...", "skip_reason": "not_durable"}
  ]
}

Allowed decision kinds:
- submit_as_is: use only for a valid candidate that is durable and specific.
- skip: use for invalid attempts that cannot be safely corrected, non-durable text,
  projection echo, duplicates, or weak memories.
- correct_and_submit: use when an invalid or flawed candidate can be corrected
  from the provided candidate payload and user quote.
- merge_and_submit: use when multiple candidates should become one cleaner typed
  candidate.
- supersede_and_submit: use ONLY when the user explicitly asked to replace or
  change an existing Preference (for example, "change my preference to X" or
  "stop using Y, use Z"). Provide the new candidate and superseded_memory_ids
  using canonical memory ids from related_existing_memories. v1 allows only
  Preference, same scope, and a single superseded memory id.
- contradict_and_submit: use when a new durable Preference clearly conflicts
  with exactly one active same-scope Preference from related_existing_memories,
  but the user did not explicitly ask to replace the old one. This keeps both
  memories active and links a non-destructive contradiction warning.

Rules:
- First decide durability, validity, and scope. Ordinary canonical submission is
  the main path. Semantic relatedness is an advisory side path, not a required
  precondition for submit_as_is, correct_and_submit, merge_and_submit, or skip.
- Branch into duplicate/coexist/contradiction/supersede reasoning only when the
  candidate has a credible related_existing_memories target. If none is shown,
  provider evidence is incomplete, or the relationship is uncertain, continue
  the ordinary non-destructive governance path.
- Prefer skip over weak memory.
- Do not invent missing facts. Correct only when the candidate snapshot provides
  enough information.
- Candidate scopes must be one of allowed_scopes. If a candidate uses a scope
  outside allowed_scopes, skip rather than rewriting it.
- Use ctx:user only for durable user-wide preferences or habits.
- Use the exact allowed ctx:workspace/<id> scope only for durable current-project facts or decisions.
- Do not submit durable memory for one-off task details.
- Use source_events, user_quote, prior_governance_decisions, and related_existing_memories
  as audit evidence. They are context, not permission to invent new memory.
- If related_existing_memories already contains an active memory
  with the same durable content, prefer skip with skip_reason duplicate_existing_memory.
- supersede_and_submit requires explicit user replacement intent. Do not
  supersede on mere topical similarity. If unsure whether the new memory
  replaces an old one, use submit_as_is/correct_and_submit so both memories can
  coexist.
- Use supersede_and_submit only when relatedness_lifecycle_actions_allowed is
  true. Include replacement_evidence_refs. Use "candidate_user_quote" only
  when the candidate has a direct user_quote that states replacement intent;
  otherwise cite an evidence/source event id shown in the snapshot.
- superseded_memory_ids must come from related_existing_memories. Never invent a
  canonical memory id.
- Never supersede a related_existing_memories entry whose is_exact_duplicate is
  true. A statement-exact duplicate means the memory already exists; use skip
  with skip_reason duplicate_existing_memory instead.
- If no related_existing_memories entry is a clear replacement target, do not
  supersede.
- contradict_and_submit is non-destructive but still noisy if wrong. Use it
  only for a clear same-subject conflict where both preferences cannot be true
  together, such as "likes egg tarts" vs "hates egg tarts".
- contradicted_memory_ids must come from related_existing_memories. Never invent
  a canonical memory id.
- Use contradict_and_submit only when relatedness_lifecycle_actions_allowed is
  true.
- Never contradict an exact duplicate, a different scope, a temporary mood,
  a story/roleplay context, or a narrower variant where both statements could
  be true. If subject match is uncertain, choose submit_as_is/correct_and_submit
  so both memories coexist without a warning.
- v2 allows only a single contradiction target. If more than one existing memory
  would need a contradiction edge, choose submit_as_is/correct_and_submit rather
  than contradict_and_submit.
- InvalidAttempt payloads usually need skip unless the raw arguments clearly
  provide all missing semantics.
- Do not output write results; the host owns write outcomes.
- Use candidate entry_id values exactly as provided.
- Compaction-origin candidates are model-inferred observations from context
  compression. Treat them as weaker than explicit user memory-tool proposals.
  Submit only if durable, repeated, project-relevant, and useful for future
  runs. Prefer skip for one-off task details, transient implementation steps,
  secrets, projection echoes, or overgeneralized habits.
- For origin=compaction, do not use supersede_and_submit or
  contradict_and_submit in V1. Use submit_as_is, skip, merge_and_submit, or
  correct_and_submit. Replacement evidence refs from compaction windows are not
  accepted by the executor in V1.
- If there is nothing to govern, decisions=[].

Few-shot examples:

Example A: durable valid preference
Input candidate:
{
  "entry_id": "pool:pref",
  "payload": {
    "payload_kind": "valid",
    "candidate": {
      "kind": "Preference",
      "candidate_id": "candidate:pref",
      "statement": "The user prefers concise summaries.",
      "scope": "ctx:user",
      "source_authority": "explicit_user_instruction",
      "verification_status": "user_confirmed",
      "evidence_ids": []
    }
  }
}
Output:
{
  "reason": "The candidate is a clear durable user preference.",
  "decisions": [
    {
      "kind": "submit_as_is",
      "target_entry_id": "pool:pref",
      "reason": "Explicit user-confirmed durable preference."
    }
  ]
}

Example B: invalid action boundary with insufficient correction evidence
Input candidate:
{
  "entry_id": "pool:bad",
  "payload": {
    "payload_kind": "invalid",
    "attempted_tool_name": "remember_action_boundary",
    "attempted_kind": "ActionBoundary",
    "raw_arguments": {"statement": "Do not do that."},
    "validation_error": "missing applies_when and do_not_apply_when"
  }
}
Output:
{
  "reason": "The failed attempt lacks enough semantics to reconstruct a safe action boundary.",
  "decisions": [
    {
      "kind": "skip",
      "target_entry_ids": ["pool:bad"],
      "reason": "Cannot safely infer missing action-boundary conditions.",
      "skip_reason": "invalid_attempt"
    }
  ]
}

Example C: merge two duplicate preferences
Output:
{
  "reason": "Two candidate preferences express the same durable preference.",
  "decisions": [
    {
      "kind": "merge_and_submit",
      "target_entry_ids": ["pool:a", "pool:b"],
      "reason": "Merge duplicate wording into one stable preference.",
      "candidate": {
        "kind": "Preference",
        "candidate_id": "candidate:merged",
        "statement": "The user prefers concise summaries.",
        "scope": "ctx:user",
        "source_authority": "explicit_user_instruction",
        "verification_status": "user_confirmed",
        "evidence_ids": []
      }
    }
  ]
}
""".strip()


def _governance_system_prompt(allowed_scopes: frozenset[str]) -> str:
    return "\n".join(
        [
            _GOVERNANCE_SYSTEM_PROMPT,
            "",
            "Allowed scopes for this run: " + format_scope_list(allowed_scopes) + ".",
        ]
    )


_KIND_TO_TERM = {
    "Claim": memory.CLAIM,
    "Preference": memory.PREFERENCE,
    "Observation": memory.OBSERVATION,
    "ActionBoundary": memory.ACTION_BOUNDARY,
    "Decision": memory.DECISION,
}


def _source_event_summaries(events, *, limit: int = 80) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for event in events[:limit]:
        item: dict[str, Any] = {
            "event_id": event.id,
            "event_type": type(event).__name__,
            "sequence": event.sequence,
            "reply_id": event.reply_id,
        }
        for attr in (
            "tool_call_id",
            "tool_call_name",
            "state",
            "delta",
            "message",
            "code",
            "status",
            "stop_reason",
            "error_message",
        ):
            if not hasattr(event, attr):
                continue
            value = getattr(event, attr)
            if value is None:
                continue
            if hasattr(value, "value"):
                value = value.value
            if isinstance(value, str) and len(value) > 1_000:
                value = value[:1_000]
            item[attr] = value
        summaries.append(item)
    if len(events) > limit:
        summaries.append({"event_type": "TRUNCATED", "omitted_event_count": len(events) - limit})
    return summaries


def _compaction_source_events(event_log, candidate: PooledMemoryCandidate, *, limit: int = 80):
    metadata = candidate.metadata or {}
    included_run_ids = _bounded_strings(metadata.get("included_run_ids"), limit=5)
    through_sequence = _int_or_none(metadata.get("through_sequence"))
    keep_after_sequence = _int_or_none(metadata.get("keep_after_sequence"))
    events = []
    seen: set[str] = set()
    for run_id in included_run_ids:
        for event in event_log.iter(run_id=run_id):
            if event.id in seen:
                continue
            sequence = event.sequence
            if through_sequence is not None and sequence is not None and sequence > through_sequence:
                continue
            if keep_after_sequence is not None and sequence is not None and sequence > keep_after_sequence:
                continue
            seen.add(event.id)
            events.append(event)
    if not events and through_sequence is not None:
        for event in event_log.iter():
            sequence = event.sequence
            if sequence is None or sequence > through_sequence:
                continue
            seen.add(event.id)
            events.append(event)
            if len(events) >= limit:
                break
    events.sort(key=lambda event: (event.sequence or 0, event.id))
    return events[:limit]


def _compaction_evidence_view(candidate: PooledMemoryCandidate, source_events) -> dict[str, Any]:
    metadata = candidate.metadata or {}
    return {
        "kind": "context_compaction",
        "compaction_id": metadata.get("compaction_id"),
        "summary_artifact_id": metadata.get("summary_artifact_id") or candidate.source_artifact_id,
        "summary_excerpt": metadata.get("summary_excerpt"),
        "summary_excerpt_chars": metadata.get("summary_excerpt_chars"),
        "summary_excerpt_truncated": metadata.get("summary_excerpt_truncated", False),
        "included_run_ids": _bounded_strings(metadata.get("included_run_ids"), limit=5),
        "included_run_count": metadata.get("included_run_count"),
        "included_run_ids_truncated": metadata.get("included_run_ids_truncated", False),
        "through_sequence": metadata.get("through_sequence"),
        "keep_after_sequence": metadata.get("keep_after_sequence"),
        "source_event_id": candidate.source_event_id or metadata.get("source_event_id"),
        "source_event_sequence": metadata.get("source_event_sequence"),
        "source_event_count": len(source_events),
    }


def _bounded_strings(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value[:limit]]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _decision_summaries(decisions: list[MemoryGovernanceDecisionRecord]) -> list[dict[str, Any]]:
    return [
        {
            "decision_id": decision.decision_id,
            "governance_batch_id": decision.governance_batch_id,
            "decision": decision.decision.model_dump(mode="json"),
            "write_outcome": decision.write_outcome.model_dump(mode="json"),
            "created_at": decision.created_at,
        }
        for decision in decisions
    ]


def _related_existing_memories(
    candidate: PooledMemoryCandidate,
    graph,
    *,
    graph_id: str | None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if not isinstance(candidate.payload, ValidCandidatePayload):
        return []
    memory_candidate = candidate.payload.candidate
    term = _KIND_TO_TERM.get(memory_candidate.kind)
    if term is None:
        return []
    candidate_tokens = _overlap_tokens(memory_candidate.statement, candidate.user_quote)
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for record in graph.find_by_type(term, graph_id=graph_id):
        scope = str(record.get(memory.SCOPE.name, ""))
        status = str(record.get(memory.STATUS.name, ""))
        if scope != memory_candidate.scope:
            continue
        if status != memory.NodeStatus.ACTIVE.value:
            continue
        statement = str(record.get(memory.STATEMENT.name, ""))
        memory_id = str(record.get("@id", ""))
        exact_duplicate = _normalize(statement) == _normalize(memory_candidate.statement)
        overlap = len(candidate_tokens & _overlap_tokens(statement))
        # Token overlap is only a v1 stopgap for subject relatedness; a future
        # structured subject key should replace this prompt-ranking hint.
        scored.append(
            (
                overlap,
                memory_id,
                {
                    "memory_id": record.get("@id"),
                    "memory_type": memory_candidate.kind,
                    "statement": statement,
                    "scope": scope,
                    "status": status,
                    "verification_status": record.get(memory.VERIFICATION_STATUS.name),
                    "is_exact_duplicate": exact_duplicate,
                },
            )
        )
    scored.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in scored[:limit]]


def _overlap_tokens(*texts: Any) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "are",
        "as",
        "be",
        "for",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "user",
        "with",
    }
    tokens: set[str] = set()
    for text in texts:
        for token in re.findall(r"[\w]+", str(text or "").casefold()):
            if len(token) < 2 or token in stopwords:
                continue
            tokens.add(token)
    return tokens


def _normalize(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _legacy_execution_context(
    governance_batch_id: str,
    snapshots: list[dict[str, Any]],
) -> RelatednessExecutionContext:
    # In-memory test-double path: no semantic relatedness service is wired
    # (product wiring always supplies the Postgres-backed service). Token
    # overlap remains advisory *prompt* context — it is already present in each
    # snapshot's ``related_existing_memories`` for Flash — but it must NEVER
    # authorize a destructive lifecycle action. Per relatedness design Decision
    # 7, a prompt-only basis cannot grant ``allows_lifecycle``. Fail closed:
    # every entry is UNAVAILABLE with an empty allowlist, so the executor blocks
    # supersede/contradict (``relatedness_evidence_unavailable``) while
    # submit_as_is / merge / skip / correct continue to work unaffected.
    entry_ids = [str(snapshot["entry_id"]) for snapshot in snapshots]
    return RelatednessExecutionContext(
        governance_batch_id=governance_batch_id,
        allowlists=MappingProxyType({entry_id: frozenset() for entry_id in entry_ids}),
        availability=MappingProxyType(
            {entry_id: RelatednessAvailability.UNAVAILABLE for entry_id in entry_ids}
        ),
    )


__all__ = [
    "MemoryGovernanceEngine",
    "MemoryGovernanceInput",
    "MemoryGovernanceOptions",
    "MemoryGovernanceOutput",
    "MemoryGovernanceRunResult",
]
