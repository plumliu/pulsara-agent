"""Flash-backed memory governance decision engine."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pulsara_agent.event import EventContext, RunErrorEvent, TextBlockDeltaEvent
from pulsara_agent.event.candidates import ValidCandidatePayload
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import LLMContext, LLMOptions
from pulsara_agent.memory.candidates.pool import (
    GovernanceDecision,
    MemoryGovernanceDecisionRecord,
    PooledMemoryCandidate,
    decision_target_entry_ids,
    new_governance_batch_id,
)
from pulsara_agent.memory.governance.dedupe import candidate_fingerprint
from pulsara_agent.memory.governance.executor import MemoryGovernanceApplyResult, MemoryGovernanceExecutor
from pulsara_agent.ontology import memory


class MemoryGovernanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_session_id: str
    governance_batch_id: str
    trigger_reason: str
    candidates: list[dict[str, Any]]


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

        governance_input = MemoryGovernanceInput(
            runtime_session_id=self.executor.runtime_session_id,
            governance_batch_id=batch_id,
            trigger_reason=trigger_reason,
            candidates=[self._candidate_snapshot(candidate) for candidate in pending],
        )
        try:
            output = _parse_governance_output(await self._call_flash(governance_input))
        except Exception as exc:
            return MemoryGovernanceRunResult(
                governance_batch_id=batch_id,
                decisions=[],
                applied=[],
                error_type=type(exc).__name__,
                error_message=str(exc),
            )

        applied: list[MemoryGovernanceApplyResult] = []
        try:
            for decision in output.decisions:
                applied.append(self.executor.apply_decision(decision, governance_batch_id=batch_id))
        except Exception as exc:
            return MemoryGovernanceRunResult(
                governance_batch_id=batch_id,
                decisions=output.decisions,
                applied=applied,
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        return MemoryGovernanceRunResult(
            governance_batch_id=batch_id,
            decisions=output.decisions,
            applied=applied,
        )

    async def _call_flash(self, governance_input: MemoryGovernanceInput) -> str:
        text_parts: list[str] = []
        async for event in self.llm_runtime.stream(
            role=self.options.model_role,
            context=LLMContext(
                system_prompt=_GOVERNANCE_SYSTEM_PROMPT,
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

    def _candidate_snapshot(self, candidate: PooledMemoryCandidate) -> dict[str, Any]:
        source_events = self.executor.event_log.iter(run_id=candidate.source_run_id)
        decisions = [
            decision
            for decision in self.executor.candidate_pool.list_decisions()
            if candidate.entry_id in decision_target_entry_ids(decision.decision)
        ]
        snapshot = candidate.model_dump(mode="json")
        snapshot["source_events"] = _source_event_summaries(source_events)
        snapshot["prior_governance_decisions"] = _decision_summaries(decisions)
        snapshot["related_existing_memories"] = _related_existing_memories(
            candidate,
            self.executor.graph,
            graph_id=self.executor.graph_id,
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
    {"kind": "supersede_and_submit", "target_entry_id": "pool:...", "candidate": {...}, "superseded_memory_ids": ["preference:..."], "reason": "..."},
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

Rules:
- Prefer skip over weak memory.
- Do not invent missing facts. Correct only when the candidate snapshot provides
  enough information.
- Use source_events, user_quote, prior_governance_decisions, and related_existing_memories
  as audit evidence. They are context, not permission to invent new memory.
- If related_existing_memories already contains an active memory
  with the same durable content, prefer skip with skip_reason duplicate_existing_memory.
- supersede_and_submit requires explicit user replacement intent. Do not
  supersede on mere topical similarity. If unsure whether the new memory
  replaces an old one, use submit_as_is/correct_and_submit so both memories can
  coexist.
- superseded_memory_ids must come from related_existing_memories. Never invent a
  canonical memory id.
- Never supersede a related_existing_memories entry whose is_exact_duplicate is
  true. A statement-exact duplicate means the memory already exists; use skip
  with skip_reason duplicate_existing_memory instead.
- If no related_existing_memories entry is a clear replacement target, do not
  supersede.
- InvalidAttempt payloads usually need skip unless the raw arguments clearly
  provide all missing semantics.
- Do not output write results; the host owns write outcomes.
- Use candidate entry_id values exactly as provided.
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


__all__ = [
    "MemoryGovernanceEngine",
    "MemoryGovernanceInput",
    "MemoryGovernanceOptions",
    "MemoryGovernanceOutput",
    "MemoryGovernanceRunResult",
]
