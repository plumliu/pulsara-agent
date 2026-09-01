"""Provider-neutral immutable contracts for advisory memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
import unicodedata
from typing import Mapping, Sequence

from pulsara_agent.memory.scope import (
    CTX_USER,
    FrozenMemoryReadScopeBinding,
    MemoryScopeKind,
)
from pulsara_agent.retrieval.tokenizer import (
    MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_ID,
    MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_VERSION,
    MemoryRetrievalTokenizerV1,
)


MAXIMUM_MEMORY_STATEMENT_BYTES = 8 * 1024
MAXIMUM_RESPONSE_PREFERENCE_STATEMENT_BYTES = 2 * 1024
MAXIMUM_MEMORY_APPLIES_WHEN_BYTES = 4 * 1024
MAXIMUM_MEMORY_EXCLUSION_ITEMS = 8
MAXIMUM_MEMORY_EXCLUSION_ITEM_BYTES = 2 * 1024
MAXIMUM_MEMORY_EXCLUSION_BYTES = 8 * 1024
MAXIMUM_MEMORY_REFERENCE_ITEMS = 8
MAXIMUM_MEMORY_CANDIDATE_BYTES = 32 * 1024
MAXIMUM_MODEL_VISIBLE_MEMORY_FACT_IDS = 128
MAXIMUM_MODEL_VISIBLE_MEMORY_PROVENANCE_BYTES = 16 * 1024
MAXIMUM_GOVERNANCE_PRODUCER_TURN_BYTES = 32 * 1024
MAXIMUM_GOVERNANCE_CITATION_PREVIEW_BYTES = 64 * 1024
MAXIMUM_GOVERNANCE_VISIBLE_MEMORY_BYTES = 64 * 1024
MAXIMUM_GOVERNANCE_FINAL_WIRE_BYTES = 128 * 1024
MAXIMUM_GOVERNANCE_INPUT_TOKENS = 32_768
MAXIMUM_GOVERNANCE_OUTPUT_BYTES = 8 * 1024
MAXIMUM_GOVERNANCE_OUTPUT_TOKENS = 4_096


class MemoryFactKind(StrEnum):
    FACT = "FACT"
    USER_PROFILE = "USER_PROFILE"
    RESPONSE_PREFERENCE = "RESPONSE_PREFERENCE"
    ACTION_RULE = "ACTION_RULE"
    DECISION = "DECISION"


class MemoryKindHint(StrEnum):
    AUTO = "AUTO"
    FACT = "FACT"
    USER_PROFILE = "USER_PROFILE"
    RESPONSE_PREFERENCE = "RESPONSE_PREFERENCE"
    ACTION_RULE = "ACTION_RULE"
    DECISION = "DECISION"


class MemoryProducerKind(StrEnum):
    MAIN_AGENT_REMEMBER = "MAIN_AGENT_REMEMBER"
    CHEAP_HINT_REFLECTION = "CHEAP_HINT_REFLECTION"


class MemoryCandidateStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    ACCEPTED = "ACCEPTED"
    APPLIED_TO_EXISTING = "APPLIED_TO_EXISTING"
    SKIPPED = "SKIPPED"
    ABANDONED = "ABANDONED"


class MemoryDecisionKind(StrEnum):
    SKIP = "SKIP"
    ACCEPT = "ACCEPT"
    ACCEPT_AND_SUPERSEDE = "ACCEPT_AND_SUPERSEDE"
    ACCEPT_AND_CONTRADICT = "ACCEPT_AND_CONTRADICT"


class MemoryDecisionReasonCode(StrEnum):
    """Closed public/terminal reason vocabulary for one governance decision."""

    DUPLICATE = "DUPLICATE"
    INSUFFICIENT_SOURCE_SUPPORT = "INSUFFICIENT_SOURCE_SUPPORT"
    TEMPORARY_OR_EPHEMERAL = "TEMPORARY_OR_EPHEMERAL"
    LOW_VALUE = "LOW_VALUE"
    MULTI_ATOM_STATEMENT = "MULTI_ATOM_STATEMENT"
    USER_PROFILE_SCOPE_OR_KIND_MISMATCH = "USER_PROFILE_SCOPE_OR_KIND_MISMATCH"
    UNSAFE_RESPONSE_PREFERENCE = "UNSAFE_RESPONSE_PREFERENCE"
    UNSUPPORTED_STRUCTURE = "UNSUPPORTED_STRUCTURE"
    RECALLED_MEMORY_ECHO = "RECALLED_MEMORY_ECHO"
    MODEL_VISIBLE_MEMORY_PROVENANCE_OVERFLOW = (
        "MODEL_VISIBLE_MEMORY_PROVENANCE_OVERFLOW"
    )
    RESPONSE_PREFERENCE_CAPACITY_EXCEEDED = (
        "RESPONSE_PREFERENCE_CAPACITY_EXCEEDED"
    )
    SKIPPED_DUPLICATE = "SKIPPED_DUPLICATE"
    SKIPPED_DUPLICATE_BASIS_UNAPPLIED = "SKIPPED_DUPLICATE_BASIS_UNAPPLIED"
    SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT = (
        "SKIPPED_DUPLICATE_RELATION_ALREADY_PRESENT"
    )
    ABANDONED_GOVERNANCE_FAILURE = "ABANDONED_GOVERNANCE_FAILURE"
    ABANDONED_INVALID_OUTPUT = "ABANDONED_INVALID_OUTPUT"
    ABANDONED_KIND_CONFLICT = "ABANDONED_KIND_CONFLICT"
    ABANDONED_REFERENCE_DRIFT = "ABANDONED_REFERENCE_DRIFT"
    ABANDONED_RELATION_CONTRACT_CONFLICT = (
        "ABANDONED_RELATION_CONTRACT_CONFLICT"
    )
    ABANDONED_TARGET_DRIFT = "ABANDONED_TARGET_DRIFT"
    ABANDONED_RETRIEVAL_INPUT_UNSUPPORTED = (
        "ABANDONED_RETRIEVAL_INPUT_UNSUPPORTED"
    )


MODEL_GOVERNANCE_SKIP_REASON_CODES = frozenset(
    {
        MemoryDecisionReasonCode.DUPLICATE,
        MemoryDecisionReasonCode.INSUFFICIENT_SOURCE_SUPPORT,
        MemoryDecisionReasonCode.TEMPORARY_OR_EPHEMERAL,
        MemoryDecisionReasonCode.LOW_VALUE,
        MemoryDecisionReasonCode.MULTI_ATOM_STATEMENT,
        MemoryDecisionReasonCode.USER_PROFILE_SCOPE_OR_KIND_MISMATCH,
        MemoryDecisionReasonCode.UNSAFE_RESPONSE_PREFERENCE,
        MemoryDecisionReasonCode.UNSUPPORTED_STRUCTURE,
        MemoryDecisionReasonCode.RECALLED_MEMORY_ECHO,
    }
)


class MemoryRelationKind(StrEnum):
    BASED_ON = "BASED_ON"
    SUPERSEDES = "SUPERSEDES"
    CONTRADICTS = "CONTRADICTS"


class MemorySupersedeMode(StrEnum):
    SAME_KIND_REPLACEMENT = "SAME_KIND_REPLACEMENT"
    TAXONOMY_CORRECTION = "TAXONOMY_CORRECTION"


class MemoryCitationVisibility(StrEnum):
    USER_SAFE = "USER_SAFE"
    WORKSPACE_BOUND = "WORKSPACE_BOUND"


class MemoryCitationEvidenceKind(StrEnum):
    PRIMARY_OBSERVATION = "PRIMARY_OBSERVATION"
    MEMORY_READ_EXPOSURE = "MEMORY_READ_EXPOSURE"


class MemoryGovernanceEvidenceRole(StrEnum):
    HUMAN_ASSERTION = "HUMAN_ASSERTION"
    POST_PROPOSAL_HUMAN = "POST_PROPOSAL_HUMAN"
    PRIMARY_OBSERVATION = "PRIMARY_OBSERVATION"
    MEMORY_READ_EXPOSURE = "MEMORY_READ_EXPOSURE"
    ASSISTANT_CONTEXT = "ASSISTANT_CONTEXT"
    NON_HUMAN_CONTEXT = "NON_HUMAN_CONTEXT"
    TOOL_CONTEXT_ONLY = "TOOL_CONTEXT_ONLY"


class MemoryGovernanceChronology(StrEnum):
    BEFORE_PROPOSAL = "BEFORE_PROPOSAL"
    PRODUCER_OUTPUT = "PRODUCER_OUTPUT"
    AFTER_PROPOSAL = "AFTER_PROPOSAL"


class MemoryGovernanceSourceBlockKind(StrEnum):
    TEXT = "TEXT"
    DATA = "DATA"


class ModelVisibleMemoryProvenanceDisposition(StrEnum):
    COMPLETE = "COMPLETE"
    OVERFLOW = "OVERFLOW"


class AutomaticMemoryTriggerDisposition(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    DISABLED_BY_EXPLICIT_USER_DIRECTIVE = "DISABLED_BY_EXPLICIT_USER_DIRECTIVE"
    SKIPPED_LOW_INFORMATION = "SKIPPED_LOW_INFORMATION"


class MemoryUsePolicy(StrEnum):
    ENABLED = "ENABLED"
    WRITE_DISABLED_BY_USER = "WRITE_DISABLED_BY_USER"
    ALL_DISABLED_BY_USER = "ALL_DISABLED_BY_USER"

    @property
    def allows_reads(self) -> bool:
        return self is not MemoryUsePolicy.ALL_DISABLED_BY_USER

    @property
    def allows_writes(self) -> bool:
        return self is MemoryUsePolicy.ENABLED


def strongest_memory_use_policy(
    current: MemoryUsePolicy,
    candidate: MemoryUsePolicy,
) -> MemoryUsePolicy:
    """Return the monotonic policy winner for one ordered ROOT run."""

    strength = {
        MemoryUsePolicy.ENABLED: 0,
        MemoryUsePolicy.WRITE_DISABLED_BY_USER: 1,
        MemoryUsePolicy.ALL_DISABLED_BY_USER: 2,
    }
    return candidate if strength[candidate] > strength[current] else current


@dataclass(frozen=True, slots=True)
class FrozenMemoryTriggerPolicy:
    automatic_recall: AutomaticMemoryTriggerDisposition
    memory_use: MemoryUsePolicy


class MemoryGovernanceConfirmation(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


class MemoryGovernanceSettlementBranch(StrEnum):
    ACCEPTANCE = "ACCEPTANCE"
    SKIP = "SKIP"
    RESPONSE_PREFERENCE_CAPACITY_SKIP = "RESPONSE_PREFERENCE_CAPACITY_SKIP"
    EXACT_DUPLICATE_SKIP = "EXACT_DUPLICATE_SKIP"
    EXISTING_SOURCE_RELATION = "EXISTING_SOURCE_RELATION"


class ExistingSourceRelationDisposition(StrEnum):
    APPLY_NEW_RELATION = "APPLY_NEW_RELATION"
    CONFIRM_EXISTING_RELATION = "CONFIRM_EXISTING_RELATION"


@dataclass(frozen=True, slots=True)
class FrozenMemoryProposal:
    statement: str
    scope_kind: MemoryScopeKind
    scope_id: str
    kind_hint: MemoryKindHint = MemoryKindHint.AUTO
    applies_when: str | None = None
    do_not_apply_when: tuple[str, ...] = ()
    based_on_memory_ids: tuple[str, ...] = ()
    cited_tool_result_handles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        statement = normalize_memory_text(self.statement)
        applies = (
            None if self.applies_when is None else normalize_memory_text(self.applies_when)
        )
        exclusions = tuple(normalize_memory_text(item) for item in self.do_not_apply_when)
        basis = _unique_ids(self.based_on_memory_ids, "basis")
        citations = _unique_ids(self.cited_tool_result_handles, "citation")
        object.__setattr__(self, "statement", statement)
        object.__setattr__(self, "applies_when", applies)
        object.__setattr__(self, "do_not_apply_when", exclusions)
        object.__setattr__(self, "based_on_memory_ids", basis)
        object.__setattr__(self, "cited_tool_result_handles", citations)
        _bounded_text(statement, 1, MAXIMUM_MEMORY_STATEMENT_BYTES, "statement")
        if applies is not None:
            _bounded_text(applies, 1, MAXIMUM_MEMORY_APPLIES_WHEN_BYTES, "applies_when")
        if len(exclusions) > MAXIMUM_MEMORY_EXCLUSION_ITEMS:
            raise ValueError("do_not_apply_when exceeds item bound")
        exclusion_bytes = 0
        for item in exclusions:
            _bounded_text(item, 1, MAXIMUM_MEMORY_EXCLUSION_ITEM_BYTES, "do_not_apply_when")
            exclusion_bytes += len(item.encode("utf-8"))
        if exclusion_bytes > MAXIMUM_MEMORY_EXCLUSION_BYTES:
            raise ValueError("do_not_apply_when exceeds aggregate bound")
        if len(basis) > MAXIMUM_MEMORY_REFERENCE_ITEMS or len(citations) > MAXIMUM_MEMORY_REFERENCE_ITEMS:
            raise ValueError("memory references exceed item bound")
        if self.scope_kind is MemoryScopeKind.USER:
            if self.scope_id != CTX_USER:
                raise ValueError("USER memory requires ctx:user")
        elif not self.scope_id.startswith("ctx:workspace/"):
            raise ValueError("WORKSPACE memory requires exact workspace scope")
        if self.kind_hint is MemoryKindHint.USER_PROFILE and self.scope_kind is not MemoryScopeKind.USER:
            raise ValueError("USER_PROFILE hint requires USER scope")
        if self.kind_hint is MemoryKindHint.ACTION_RULE:
            if applies is None or basis:
                raise ValueError("ACTION_RULE hint requires applies_when and no basis")
        elif self.kind_hint is MemoryKindHint.DECISION:
            if applies is not None or exclusions:
                raise ValueError("DECISION hint cannot carry applicability fields")
        elif self.kind_hint is not MemoryKindHint.AUTO and (
            applies is not None or exclusions or basis
        ):
            raise ValueError(
                "non-rule/non-decision hint carries incompatible structured fields"
            )
        if self.kind_hint is MemoryKindHint.AUTO and exclusions and applies is None:
            raise ValueError("exclusions require an ACTION_RULE applicability condition")
        if self.kind_hint is MemoryKindHint.AUTO and basis and (
            applies is not None or exclusions
        ):
            raise ValueError("AUTO candidate cannot mix rule and decision structure")
        if self.kind_hint is MemoryKindHint.RESPONSE_PREFERENCE and len(statement.encode("utf-8")) > MAXIMUM_RESPONSE_PREFERENCE_STATEMENT_BYTES:
            raise ValueError("explicit RESPONSE_PREFERENCE exceeds active statement bound")
        encoded = canonical_json_bytes(self.semantic_payload())
        if len(encoded) > MAXIMUM_MEMORY_CANDIDATE_BYTES:
            raise ValueError("memory candidate exceeds canonical byte bound")

    def semantic_payload(self) -> Mapping[str, object]:
        # Opaque model-call handles and caller-supplied fact ids are intake
        # inputs, not canonical proposal content.  The prepared candidate
        # freezes their resolved relational identities separately below.
        return {
            "statement": self.statement,
            "scope_kind": self.scope_kind.value,
            "scope_id": self.scope_id,
            "kind_hint": self.kind_hint.value,
            "applies_when": self.applies_when,
            "do_not_apply_when": self.do_not_apply_when,
        }


@dataclass(frozen=True, slots=True)
class PreparedMemoryToolResultReference:
    origin_session_id: str
    tool_result_id: str
    ordinal: int
    evidence_kind: MemoryCitationEvidenceKind
    citation_visibility: MemoryCitationVisibility

    def __post_init__(self) -> None:
        if (
            not self.origin_session_id
            or not self.tool_result_id
            or not 0 <= self.ordinal < 4096
        ):
            raise ValueError("memory ToolResult reference is invalid")


@dataclass(frozen=True, slots=True)
class FrozenMemoryCitationHandle:
    """One opaque handle issued for an exact provider-input tool result."""

    handle: str
    reference: PreparedMemoryToolResultReference

    def __post_init__(self) -> None:
        if not self.handle or len(self.handle.encode("utf-8")) > 128:
            raise ValueError("memory citation handle is invalid")


@dataclass(frozen=True, slots=True)
class FrozenModelCallMemoryContext:
    visible_memory: FrozenModelVisibleMemoryProvenance
    citation_handles: tuple[FrozenMemoryCitationHandle, ...] = ()
    memory_use_policy: MemoryUsePolicy = MemoryUsePolicy.ENABLED

    def __post_init__(self) -> None:
        handles = tuple(item.handle for item in self.citation_handles)
        if len(handles) != len(set(handles)):
            raise ValueError("model-call memory citation handles are duplicated")

    def resolve(self, handles: Sequence[str]) -> tuple[PreparedMemoryToolResultReference, ...]:
        by_handle = {item.handle: item.reference for item in self.citation_handles}
        resolved: list[PreparedMemoryToolResultReference] = []
        for ordinal, handle in enumerate(_unique_ids(handles, "citation handle")):
            try:
                reference = by_handle[handle]
            except KeyError as exc:
                raise ValueError("citation handle is not visible in this model call") from exc
            resolved.append(
                PreparedMemoryToolResultReference(
                    origin_session_id=reference.origin_session_id,
                    tool_result_id=reference.tool_result_id,
                    ordinal=ordinal,
                    evidence_kind=reference.evidence_kind,
                    citation_visibility=reference.citation_visibility,
                )
            )
        return tuple(resolved)


@dataclass(frozen=True, slots=True)
class PreparedMemoryBasisReference:
    target_fact_id: str
    target_scope_kind: MemoryScopeKind
    target_scope_id: str
    ordinal: int

    def __post_init__(self) -> None:
        if not self.target_fact_id or not 0 <= self.ordinal < 8:
            raise ValueError("memory basis reference is invalid")


@dataclass(frozen=True, slots=True)
class FrozenModelVisibleMemoryProvenance:
    disposition: ModelVisibleMemoryProvenanceDisposition
    fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        ids = _unique_ids(self.fact_ids, "model-visible memory")
        object.__setattr__(self, "fact_ids", ids)
        if self.disposition is ModelVisibleMemoryProvenanceDisposition.OVERFLOW:
            if ids:
                raise ValueError("OVERFLOW provenance cannot retain a prefix subset")
            return
        if len(ids) > MAXIMUM_MODEL_VISIBLE_MEMORY_FACT_IDS:
            raise ValueError("model-visible memory provenance exceeds item bound")
        if len(canonical_json_bytes(ids)) > MAXIMUM_MODEL_VISIBLE_MEMORY_PROVENANCE_BYTES:
            raise ValueError("model-visible memory provenance exceeds byte bound")


@dataclass(frozen=True, slots=True)
class PreparedMemoryCandidateAcceptance:
    candidate_id: str
    memory_domain_id: str
    origin_workspace_id: str
    origin_session_id: str
    producer_kind: MemoryProducerKind
    producer_entry_id: str | None
    producer_tool_call_id: str | None
    trigger_user_entry_id: str | None
    producer_candidate_ordinal: int | None
    proposal: FrozenMemoryProposal = field(repr=False)
    tool_result_refs: tuple[PreparedMemoryToolResultReference, ...] = ()
    basis_refs: tuple[PreparedMemoryBasisReference, ...] = ()
    visible_memory: FrozenModelVisibleMemoryProvenance = field(
        default_factory=lambda: FrozenModelVisibleMemoryProvenance(
            ModelVisibleMemoryProvenanceDisposition.COMPLETE,
            (),
        )
    )
    candidate_acceptance_digest: str = ""

    def __post_init__(self) -> None:
        if not all((self.candidate_id, self.memory_domain_id, self.origin_workspace_id, self.origin_session_id)):
            raise ValueError("prepared memory candidate identity is incomplete")
        if self.producer_kind is MemoryProducerKind.MAIN_AGENT_REMEMBER:
            if not self.producer_entry_id or not self.producer_tool_call_id:
                raise ValueError("MAIN_AGENT_REMEMBER provenance is incomplete")
            if self.trigger_user_entry_id is not None or self.producer_candidate_ordinal is not None:
                raise ValueError("MAIN_AGENT_REMEMBER carries reflection provenance")
        else:
            if self.producer_entry_id is not None or self.producer_tool_call_id is not None:
                raise ValueError("reflection carries main-agent provenance")
            if not self.trigger_user_entry_id or self.producer_candidate_ordinal is None or not 0 <= self.producer_candidate_ordinal < 4:
                raise ValueError("reflection provenance is incomplete")
            if self.tool_result_refs or self.visible_memory.fact_ids or self.visible_memory.disposition is not ModelVisibleMemoryProvenanceDisposition.COMPLETE:
                raise ValueError("reflection cannot carry memory/tool evidence")
        if len(self.tool_result_refs) > MAXIMUM_MEMORY_REFERENCE_ITEMS:
            raise ValueError("memory candidate ToolResult references exceed their bound")
        if tuple(item.ordinal for item in self.tool_result_refs) != tuple(range(len(self.tool_result_refs))):
            raise ValueError("ToolResult reference ordinals are not contiguous")
        if tuple(item.ordinal for item in self.basis_refs) != tuple(range(len(self.basis_refs))):
            raise ValueError("basis reference ordinals are not contiguous")
        expected = prepared_memory_candidate_digest(self)
        if self.candidate_acceptance_digest != expected:
            raise ValueError("prepared memory candidate digest mismatch")


@dataclass(frozen=True, slots=True)
class FrozenMemoryGovernanceTerminalFence:
    """Existing committed occurrences that close one candidate's source turn."""

    source_turn_id: str
    source_entry_id: str
    source_entry_event_id: str
    source_entry_event_sequence: int
    terminal_status: str
    terminal_outcome: str
    terminal_event_id: str
    terminal_event_sequence: int

    def __post_init__(self) -> None:
        if not all(
            (
                self.source_turn_id,
                self.source_entry_id,
                self.source_entry_event_id,
                self.terminal_outcome,
                self.terminal_event_id,
            )
        ):
            raise ValueError("memory governance terminal fence is incomplete")
        if self.terminal_status not in {"COMPLETED", "INTERRUPTED"}:
            raise ValueError("memory governance terminal status is invalid")
        if (
            self.source_entry_event_sequence < 1
            or self.terminal_event_sequence <= self.source_entry_event_sequence
        ):
            raise ValueError("memory governance occurrence fence is unordered")


@dataclass(frozen=True, slots=True)
class FrozenMemoryGovernanceProducerCut:
    """Exact historical semantic cut authorized by one producer assistant entry."""

    session_id: str
    turn_id: str
    producer_entry_id: str
    producer_entry_sequence: int
    context_binding_revision_id: str
    provider_input_through_sequence: int

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.turn_id,
                self.producer_entry_id,
                self.context_binding_revision_id,
            )
        ):
            raise ValueError("memory governance producer cut is incomplete")
        if (
            self.provider_input_through_sequence < 0
            or self.producer_entry_sequence <= self.provider_input_through_sequence
        ):
            raise ValueError("memory governance producer cut is not historical")


@dataclass(frozen=True, slots=True)
class FrozenMemoryCandidateForGovernance:
    """Immutable candidate head read after the process-local claim."""

    prepared: PreparedMemoryCandidateAcceptance
    status: MemoryCandidateStatus
    processing_started_at: object
    terminal_fence: FrozenMemoryGovernanceTerminalFence

    def __post_init__(self) -> None:
        if self.status is not MemoryCandidateStatus.PROCESSING:
            raise ValueError("governance candidate must be PROCESSING")
        if self.processing_started_at is None:
            raise ValueError("governance candidate lacks claim time")
        if self.terminal_fence.source_entry_id not in {
            self.prepared.producer_entry_id,
            self.prepared.trigger_user_entry_id,
        }:
            raise ValueError("governance fence does not name the candidate source")


@dataclass(frozen=True, slots=True)
class FrozenMemoryPublicFactProjection:
    fact_id: str
    scope_kind: MemoryScopeKind
    scope_id: str
    fact_kind: MemoryFactKind
    lifecycle: str
    statement: str = field(repr=False)
    applies_when: str | None = field(default=None, repr=False)
    do_not_apply_when: tuple[str, ...] = field(default=(), repr=False)
    fact_semantic_digest: str = ""

    def __post_init__(self) -> None:
        if self.lifecycle not in {"ACTIVE", "SUPERSEDED"}:
            raise ValueError("memory public projection lifecycle is invalid")
        expected = memory_fact_semantic_digest(
            kind=self.fact_kind,
            statement=self.statement,
            applies_when=self.applies_when,
            do_not_apply_when=self.do_not_apply_when,
        )
        if self.fact_semantic_digest != expected:
            raise ValueError("memory public projection semantic digest mismatch")


@dataclass(frozen=True, slots=True)
class FrozenMemoryGovernanceSourceBlock:
    block_kind: MemoryGovernanceSourceBlockKind
    text: str = field(repr=False)
    truncated: bool = False

    def __post_init__(self) -> None:
        self.text.encode("utf-8")
        if not self.text and not self.truncated:
            raise ValueError("memory governance source block is empty")


@dataclass(frozen=True, slots=True)
class FrozenMemoryGovernanceSourceItem:
    """One call-local source item with Host-assigned chronology and authority."""

    source_entry_id: str | None
    chronology: MemoryGovernanceChronology
    source_product_label: str
    evidence_role: MemoryGovernanceEvidenceRole
    public_kind: str
    blocks: tuple[FrozenMemoryGovernanceSourceBlock, ...] = field(repr=False)
    item_omitted_before: int = 0
    item_omitted_after: int = 0
    anchor: bool = False

    def __post_init__(self) -> None:
        if not self.source_product_label or not self.public_kind or not self.blocks:
            raise ValueError("memory governance source item is incomplete")
        if min(self.item_omitted_before, self.item_omitted_after) < 0:
            raise ValueError("memory governance omission count is invalid")
        human = self.evidence_role in {
            MemoryGovernanceEvidenceRole.HUMAN_ASSERTION,
            MemoryGovernanceEvidenceRole.POST_PROPOSAL_HUMAN,
        }
        if human and any(block.truncated for block in self.blocks):
            # Incomplete human material may be represented by the repository so
            # the Host can deterministically skip before opening a provider.
            return

    @property
    def truncated(self) -> bool:
        return any(block.truncated for block in self.blocks)

    @property
    def utf8_bytes(self) -> int:
        return sum(len(block.text.encode("utf-8")) for block in self.blocks)


def memory_governance_source_item_payload(
    item: FrozenMemoryGovernanceSourceItem,
    *,
    ordinal: int,
) -> Mapping[str, object]:
    """The sole provider-visible codec for one governance source item."""

    return {
        "source": f"source:{ordinal}",
        "chronology": item.chronology.value,
        "source_product_label": item.source_product_label,
        "evidence_role": item.evidence_role.value,
        "public_kind": item.public_kind,
        "source_anchor": item.anchor,
        "blocks": tuple(
            {
                "kind": block.block_kind.value,
                "text": block.text,
                "truncated": block.truncated,
            }
            for block in item.blocks
        ),
        "omitted_before": item.item_omitted_before,
        "omitted_after": item.item_omitted_after,
    }


def memory_governance_source_projection_bytes(
    items: Sequence[FrozenMemoryGovernanceSourceItem],
) -> int:
    """Exact canonical bytes of the complete provider-visible source array."""

    return len(
        canonical_json_bytes(
            tuple(
                memory_governance_source_item_payload(item, ordinal=ordinal)
                for ordinal, item in enumerate(items, start=1)
            )
        )
    )


@dataclass(frozen=True, slots=True)
class FrozenMemoryGovernanceToolEvidence:
    ordinal: int
    evidence_kind: MemoryCitationEvidenceKind
    result_state: str
    observed_at_iso: str
    observation_duration_microseconds: int | None
    tool_reported_duration_microseconds: int | None
    body: str = field(repr=False)
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.ordinal < 0 or not self.result_state or not self.observed_at_iso:
            raise ValueError("memory governance ToolResult evidence is invalid")
        if any(
            value is not None and value < 0
            for value in (
                self.observation_duration_microseconds,
                self.tool_reported_duration_microseconds,
            )
        ):
            raise ValueError("memory governance duration is invalid")


@dataclass(frozen=True, slots=True)
class FrozenMemoryGovernanceSourceCoverage:
    causal_context_complete: bool
    origin_turn_human_source_complete: bool
    post_proposal_human_source_complete: bool
    omitted_causal_items: int = 0
    omitted_assistant_or_tool_items: int = 0
    omitted_post_proposal_human_items: int = 0
    relation_authority: bool = True

    def __post_init__(self) -> None:
        if min(
            self.omitted_causal_items,
            self.omitted_assistant_or_tool_items,
            self.omitted_post_proposal_human_items,
        ) < 0:
            raise ValueError("memory governance source coverage count is invalid")
        if not (
            self.origin_turn_human_source_complete
            and self.post_proposal_human_source_complete
        ) and self.relation_authority:
            raise ValueError("incomplete human source cannot authorize relations")


@dataclass(frozen=True, slots=True)
class FrozenMemoryGovernanceEvidence:
    origin_workspace_id: str
    terminal_fence: FrozenMemoryGovernanceTerminalFence
    producer_cut: FrozenMemoryGovernanceProducerCut | None
    producer_call_context: tuple[FrozenMemoryGovernanceSourceItem, ...]
    producer_public_output: tuple[FrozenMemoryGovernanceSourceItem, ...]
    post_proposal_turn_suffix: tuple[FrozenMemoryGovernanceSourceItem, ...]
    tool_result_evidence: tuple[FrozenMemoryGovernanceToolEvidence, ...]
    basis_items: tuple[FrozenMemoryPublicFactProjection, ...]
    model_visible_items: tuple[FrozenMemoryPublicFactProjection, ...]
    model_visible_complete: bool
    source_coverage: FrozenMemoryGovernanceSourceCoverage

    def __post_init__(self) -> None:
        if not self.origin_workspace_id:
            raise ValueError("memory governance evidence lacks origin workspace")
        if tuple(item.ordinal for item in self.tool_result_evidence) != tuple(
            range(len(self.tool_result_evidence))
        ):
            raise ValueError("memory governance citation projection is unordered")
        if self.producer_cut is not None and (
            self.producer_cut.turn_id != self.terminal_fence.source_turn_id
            or self.producer_cut.producer_entry_id
            != self.terminal_fence.source_entry_id
        ):
            raise ValueError("memory governance producer cut/fence drifted")
        if any(
            item.chronology is not expected
            for values, expected in (
                (
                    self.producer_call_context,
                    MemoryGovernanceChronology.BEFORE_PROPOSAL,
                ),
                (
                    self.producer_public_output,
                    MemoryGovernanceChronology.PRODUCER_OUTPUT,
                ),
                (
                    self.post_proposal_turn_suffix,
                    MemoryGovernanceChronology.AFTER_PROPOSAL,
                ),
            )
            for item in values
        ):
            raise ValueError("memory governance source chronology drifted")
        if memory_governance_source_projection_bytes(
            (
                *self.producer_call_context,
                *self.producer_public_output,
                *self.post_proposal_turn_suffix,
            )
        ) > MAXIMUM_GOVERNANCE_PRODUCER_TURN_BYTES:
            raise ValueError("memory governance origin source exceeds its bound")
        if len(
            canonical_json_bytes(
                tuple(
                    (
                        item.evidence_kind.value,
                        item.result_state,
                        item.observed_at_iso,
                        item.observation_duration_microseconds,
                        item.tool_reported_duration_microseconds,
                        item.body,
                        item.truncated,
                    )
                    for item in self.tool_result_evidence
                )
            )
        ) > MAXIMUM_GOVERNANCE_CITATION_PREVIEW_BYTES:
            raise ValueError("memory governance citation evidence exceeds its bound")
        visible_bytes = len(
            canonical_json_bytes(
                tuple(memory_public_fact_payload(item) for item in self.model_visible_items)
            )
        )
        if self.model_visible_complete and (
            len(self.model_visible_items) > MAXIMUM_MODEL_VISIBLE_MEMORY_FACT_IDS
            or visible_bytes > MAXIMUM_GOVERNANCE_VISIBLE_MEMORY_BYTES
        ):
            raise ValueError("complete model-visible memory projection exceeds its bound")
        post_human_complete = all(
            not item.truncated
            and item.item_omitted_before == 0
            and item.item_omitted_after == 0
            for item in self.post_proposal_turn_suffix
            if item.evidence_role
            is MemoryGovernanceEvidenceRole.POST_PROPOSAL_HUMAN
        ) and self.source_coverage.omitted_post_proposal_human_items == 0
        if self.source_coverage.post_proposal_human_source_complete != (
            post_human_complete
        ):
            raise ValueError("post-proposal human coverage is inconsistent")


def memory_public_fact_payload(item: FrozenMemoryPublicFactProjection) -> Mapping[str, object]:
    return {
        "memory_id": item.fact_id,
        "scope": item.scope_kind.value,
        "kind": item.fact_kind.value,
        "lifecycle": item.lifecycle,
        "statement": item.statement,
        "applies_when": item.applies_when,
        "do_not_apply_when": item.do_not_apply_when,
    }


def memory_response_preference_item_payload(
    *, memory_id: str, scope_kind: MemoryScopeKind | str, statement: str
) -> Mapping[str, object]:
    """The sole canonical item codec shared by capacity and compiler freeze."""

    scope = (
        scope_kind.value if isinstance(scope_kind, MemoryScopeKind) else scope_kind
    )
    return {
        "memory_id": memory_id,
        "kind": MemoryFactKind.RESPONSE_PREFERENCE.value,
        "scope": scope,
        "statement": statement,
        "advisory": True,
    }


@dataclass(frozen=True, slots=True)
class FrozenMemoryGovernanceDecision:
    decision_kind: MemoryDecisionKind
    final_kind: MemoryFactKind | None = None
    reason_code: str | None = None
    public_summary: str | None = None
    related_target_fact_id: str | None = None
    supersede_mode: MemorySupersedeMode | None = None

    def __post_init__(self) -> None:
        if self.public_summary is not None:
            summary = normalize_memory_text(self.public_summary)
            _bounded_text(summary, 1, 2048, "governance public summary")
            object.__setattr__(self, "public_summary", summary)
        if self.decision_kind is MemoryDecisionKind.SKIP:
            if (
                self.final_kind is not None
                or self.related_target_fact_id is not None
                or self.supersede_mode is not None
                or not self.reason_code
            ):
                raise ValueError("memory SKIP decision union is invalid")
            try:
                MemoryDecisionReasonCode(self.reason_code)
            except ValueError as exc:
                raise ValueError("memory SKIP reason is outside the closed union") from exc
            return
        if self.final_kind is None or self.reason_code is not None:
            raise ValueError("memory acceptance decision union is invalid")
        if self.public_summary is None:
            raise ValueError("memory acceptance requires a public formation summary")
        if self.decision_kind is MemoryDecisionKind.ACCEPT:
            if self.related_target_fact_id is not None or self.supersede_mode is not None:
                raise ValueError("plain memory acceptance carries a relation")
        elif self.decision_kind is MemoryDecisionKind.ACCEPT_AND_SUPERSEDE:
            if not self.related_target_fact_id or self.supersede_mode is None:
                raise ValueError("memory supersede decision is incomplete")
        elif self.decision_kind is MemoryDecisionKind.ACCEPT_AND_CONTRADICT:
            if not self.related_target_fact_id or self.supersede_mode is not None:
                raise ValueError("memory contradiction decision is invalid")


@dataclass(frozen=True, slots=True)
class PreparedMemoryFactDraft:
    fact_id: str
    memory_domain_id: str
    scope_kind: MemoryScopeKind
    scope_id: str
    source_candidate_id: str
    fact_kind: MemoryFactKind
    statement: str
    applies_when: str | None
    do_not_apply_when: tuple[str, ...]
    fact_semantic_digest: str
    search_contract_id: str
    search_contract_version: int
    search_terms: tuple[str, ...]

    def __post_init__(self) -> None:
        if not all((self.fact_id, self.memory_domain_id, self.source_candidate_id)):
            raise ValueError("prepared memory fact identity is incomplete")
        expected = memory_fact_semantic_digest(
            kind=self.fact_kind,
            statement=self.statement,
            applies_when=self.applies_when,
            do_not_apply_when=self.do_not_apply_when,
        )
        if self.fact_semantic_digest != expected:
            raise ValueError("prepared memory fact semantic digest mismatch")
        if self.search_contract_id != MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_ID:
            raise ValueError("prepared memory fact tokenizer contract is invalid")
        if (
            self.search_contract_version
            != MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_VERSION
        ):
            raise ValueError("prepared memory fact tokenizer version is invalid")
        expected_terms = MemoryRetrievalTokenizerV1().tokenize(
            self.statement,
            self.applies_when,
            *self.do_not_apply_when,
        )
        if self.search_terms != expected_terms:
            raise ValueError("prepared memory fact search terms drifted")


@dataclass(frozen=True, slots=True)
class FrozenMemoryFactSettlementIdentity:
    fact_id: str
    memory_domain_id: str
    scope_kind: MemoryScopeKind
    scope_id: str
    fact_kind: MemoryFactKind
    statement: str
    applies_when: str | None
    do_not_apply_when: tuple[str, ...]
    fact_semantic_digest: str
    expected_lifecycle: str

    def __post_init__(self) -> None:
        if self.expected_lifecycle not in {"ACTIVE", "SUPERSEDED"}:
            raise ValueError("memory settlement lifecycle is invalid")
        expected = memory_fact_semantic_digest(
            kind=self.fact_kind,
            statement=self.statement,
            applies_when=self.applies_when,
            do_not_apply_when=self.do_not_apply_when,
        )
        if self.fact_semantic_digest != expected:
            raise ValueError("memory settlement semantic identity is invalid")


@dataclass(frozen=True, slots=True)
class PreparedMemoryRelationDraft:
    relation_id: str
    decision_candidate_id: str
    source_scope_kind: MemoryScopeKind
    source_scope_id: str
    source_fact_id: str
    source_fact_kind: MemoryFactKind
    relation_kind: MemoryRelationKind
    target: FrozenMemoryFactSettlementIdentity
    supersede_mode: MemorySupersedeMode | None
    ordinal: int | None
    expected_target_lifecycle_before: str
    expected_target_lifecycle_after: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.relation_id,
                self.decision_candidate_id,
                self.source_fact_id,
                self.source_scope_id,
            )
        ):
            raise ValueError("prepared memory relation identity is incomplete")
        if self.expected_target_lifecycle_before != "ACTIVE":
            raise ValueError("prepared relation target must start ACTIVE")
        expected_after = (
            "SUPERSEDED"
            if self.relation_kind is MemoryRelationKind.SUPERSEDES
            else "ACTIVE"
        )
        if self.expected_target_lifecycle_after != expected_after:
            raise ValueError("prepared relation target transition is invalid")
        if (self.relation_kind is MemoryRelationKind.SUPERSEDES) != (
            self.supersede_mode is not None
        ):
            raise ValueError("prepared relation supersede mode union is invalid")
        if self.relation_kind is MemoryRelationKind.BASED_ON:
            if self.ordinal is None or self.ordinal < 0:
                raise ValueError("BASED_ON relation requires an ordinal")
        elif self.ordinal is not None:
            raise ValueError("unordered memory relation carries an ordinal")
        expected_id = memory_relation_id(
            memory_domain_id=self.target.memory_domain_id,
            source_scope_kind=self.source_scope_kind,
            source_scope_id=self.source_scope_id,
            source_fact_id=self.source_fact_id,
            relation_kind=self.relation_kind,
            target_scope_kind=self.target.scope_kind,
            target_scope_id=self.target.scope_id,
            target_fact_id=self.target.fact_id,
            supersede_mode=self.supersede_mode,
        )
        if self.relation_id != expected_id:
            raise ValueError("prepared memory relation id drifted")


@dataclass(frozen=True, slots=True)
class PreparedMemoryCandidateTerminalDraft:
    status: MemoryCandidateStatus
    decision_kind: MemoryDecisionKind
    final_kind: MemoryFactKind | None
    decision_reason_code: str | None
    decision_public_summary: str | None
    related_target_fact_id: str | None
    duplicate_winner_fact_id: str | None
    accepted_fact_id: str | None
    applied_existing_fact_id: str | None

    def __post_init__(self) -> None:
        if self.status is MemoryCandidateStatus.ACCEPTED:
            if (
                self.decision_kind is MemoryDecisionKind.SKIP
                or self.final_kind is None
                or self.accepted_fact_id is None
                or self.decision_reason_code is not None
                or self.duplicate_winner_fact_id is not None
                or self.applied_existing_fact_id is not None
            ):
                raise ValueError("accepted memory terminal draft is invalid")
        elif self.status is MemoryCandidateStatus.SKIPPED:
            if (
                self.decision_kind is not MemoryDecisionKind.SKIP
                or self.decision_reason_code is None
                or self.final_kind is not None
                or self.accepted_fact_id is not None
                or self.applied_existing_fact_id is not None
            ):
                raise ValueError("skipped memory terminal draft is invalid")
        else:
            raise ValueError("initial governance terminal draft is invalid")


@dataclass(frozen=True, slots=True)
class PreparedMemoryGovernanceAcceptance:
    candidate_id: str
    candidate_acceptance_digest: str
    memory_domain_id: str
    origin_workspace_id: str
    scope_kind: MemoryScopeKind
    scope_id: str
    decision: FrozenMemoryGovernanceDecision
    fact: PreparedMemoryFactDraft | None
    expected_candidate_status: MemoryCandidateStatus
    target: FrozenMemoryFactSettlementIdentity | None
    basis_targets: tuple[FrozenMemoryFactSettlementIdentity, ...]
    relation_drafts: tuple[PreparedMemoryRelationDraft, ...]
    terminal_draft: PreparedMemoryCandidateTerminalDraft
    compatible_settlement_branches: tuple[MemoryGovernanceSettlementBranch, ...]

    @property
    def settlement_branch(self) -> MemoryGovernanceSettlementBranch:
        """Primary write branch retained for narrow existing call sites."""

        return self.compatible_settlement_branches[0]

    def __post_init__(self) -> None:
        acceptance = self.decision.decision_kind is not MemoryDecisionKind.SKIP
        if acceptance != (self.fact is not None):
            raise ValueError("governance fact/decision union is invalid")
        if self.expected_candidate_status is not MemoryCandidateStatus.PROCESSING:
            raise ValueError("governance candidate head is not PROCESSING")
        if not self.compatible_settlement_branches or len(
            self.compatible_settlement_branches
        ) != len(set(self.compatible_settlement_branches)):
            raise ValueError("governance settlement branches are invalid")
        if acceptance != (
            MemoryGovernanceSettlementBranch.ACCEPTANCE
            in self.compatible_settlement_branches
        ):
            raise ValueError("governance acceptance branch is invalid")
        if not acceptance and self.compatible_settlement_branches != (
            MemoryGovernanceSettlementBranch.SKIP,
        ):
            raise ValueError("governance SKIP branch is invalid")
        if self.fact is not None and (
            self.fact.source_candidate_id != self.candidate_id
            or self.fact.memory_domain_id != self.memory_domain_id
            or self.fact.scope_kind is not self.scope_kind
            or self.fact.scope_id != self.scope_id
            or self.fact.fact_kind is not self.decision.final_kind
        ):
            raise ValueError("prepared memory fact does not join candidate")
        if self.target is None and self.decision.related_target_fact_id is not None:
            raise ValueError("prepared governance target is absent")
        if self.target is not None and (
            self.target.fact_id != self.decision.related_target_fact_id
            or self.target.memory_domain_id != self.memory_domain_id
        ):
            raise ValueError("prepared governance target identity drifted")
        if tuple(item.expected_lifecycle for item in self.basis_targets) != (
            "ACTIVE",
        ) * len(self.basis_targets):
            raise ValueError("prepared governance basis is not ACTIVE")
        if tuple(item.ordinal for item in self.relation_drafts) not in {
            (),
            tuple(range(len(self.relation_drafts))),
            (None,),
        }:
            raise ValueError("prepared governance relations are unordered")
        if any(
            item.decision_candidate_id != self.candidate_id
            or item.source_fact_id != (self.fact.fact_id if self.fact else None)
            for item in self.relation_drafts
        ):
            raise ValueError("prepared governance relation source drifted")
        if acceptance != (
            self.terminal_draft.status is MemoryCandidateStatus.ACCEPTED
        ):
            raise ValueError("governance terminal draft does not join decision")


@dataclass(frozen=True, slots=True)
class PreparedExistingSourceRelationSettlement:
    parent: PreparedMemoryGovernanceAcceptance = field(repr=False, compare=False)
    candidate_id: str
    existing_source: FrozenMemoryFactSettlementIdentity
    target: FrozenMemoryFactSettlementIdentity
    relation_kind: MemoryRelationKind
    supersede_mode: MemorySupersedeMode | None
    prepared_relation_id: str
    settled_target_lifecycle: str
    disposition: ExistingSourceRelationDisposition
    existing_relation_id: str | None
    existing_relation_decision_candidate_id: str | None
    existing_relation_source_fact_id: str | None
    existing_relation_target_fact_id: str | None

    def __post_init__(self) -> None:
        if self.relation_kind not in {
            MemoryRelationKind.SUPERSEDES,
            MemoryRelationKind.CONTRADICTS,
        }:
            raise ValueError("existing-source settlement relation is invalid")
        if (self.relation_kind is MemoryRelationKind.SUPERSEDES) != (
            self.supersede_mode is not None
        ):
            raise ValueError("existing-source supersede mode union is invalid")
        if self.existing_source.expected_lifecycle != "ACTIVE":
            raise ValueError("existing memory source must be frozen ACTIVE")
        expected_target_lifecycle = (
            "SUPERSEDED"
            if self.relation_kind is MemoryRelationKind.SUPERSEDES
            else "ACTIVE"
        )
        if self.settled_target_lifecycle != expected_target_lifecycle:
            raise ValueError("existing-source target settlement is invalid")
        expected_relation_id = memory_relation_id(
            memory_domain_id=self.existing_source.memory_domain_id,
            source_scope_kind=self.existing_source.scope_kind,
            source_scope_id=self.existing_source.scope_id,
            source_fact_id=self.existing_source.fact_id,
            relation_kind=self.relation_kind,
            target_scope_kind=self.target.scope_kind,
            target_scope_id=self.target.scope_id,
            target_fact_id=self.target.fact_id,
            supersede_mode=self.supersede_mode,
        )
        if self.prepared_relation_id != expected_relation_id:
            raise ValueError("existing-source relation identity is invalid")
        confirms = (
            self.disposition
            is ExistingSourceRelationDisposition.CONFIRM_EXISTING_RELATION
        )
        if confirms != (
            self.existing_relation_id is not None
            and self.existing_relation_decision_candidate_id is not None
            and self.existing_relation_source_fact_id is not None
            and self.existing_relation_target_fact_id is not None
        ):
            raise ValueError("existing-source relation confirmation union is invalid")
        if confirms and self.existing_relation_id != self.prepared_relation_id:
            raise ValueError("existing-source relation identity drifted")
        if self.parent.candidate_id != self.candidate_id:
            raise ValueError("existing-source settlement parent conflicts")


def prepare_memory_candidate(
    *,
    candidate_id: str,
    memory_domain_id: str,
    origin_workspace_id: str,
    origin_session_id: str,
    producer_kind: MemoryProducerKind,
    proposal: FrozenMemoryProposal,
    producer_entry_id: str | None = None,
    producer_tool_call_id: str | None = None,
    trigger_user_entry_id: str | None = None,
    producer_candidate_ordinal: int | None = None,
    tool_result_refs: Sequence[PreparedMemoryToolResultReference] = (),
    basis_refs: Sequence[PreparedMemoryBasisReference] = (),
    visible_memory: FrozenModelVisibleMemoryProvenance | None = None,
) -> PreparedMemoryCandidateAcceptance:
    provisional = object.__new__(PreparedMemoryCandidateAcceptance)
    values = {
        "candidate_id": candidate_id,
        "memory_domain_id": memory_domain_id,
        "origin_workspace_id": origin_workspace_id,
        "origin_session_id": origin_session_id,
        "producer_kind": producer_kind,
        "producer_entry_id": producer_entry_id,
        "producer_tool_call_id": producer_tool_call_id,
        "trigger_user_entry_id": trigger_user_entry_id,
        "producer_candidate_ordinal": producer_candidate_ordinal,
        "proposal": proposal,
        "tool_result_refs": tuple(tool_result_refs),
        "basis_refs": tuple(basis_refs),
        "visible_memory": visible_memory or FrozenModelVisibleMemoryProvenance(ModelVisibleMemoryProvenanceDisposition.COMPLETE, ()),
    }
    for key, value in values.items():
        object.__setattr__(provisional, key, value)
    object.__setattr__(provisional, "candidate_acceptance_digest", "")
    digest = prepared_memory_candidate_digest(provisional)
    return PreparedMemoryCandidateAcceptance(**values, candidate_acceptance_digest=digest)


def prepared_memory_candidate_digest(candidate: PreparedMemoryCandidateAcceptance) -> str:
    return digest(
        "pulsara:memory-candidate-acceptance:v1",
        {
            "candidate_id": candidate.candidate_id,
            "memory_domain_id": candidate.memory_domain_id,
            "origin_workspace_id": candidate.origin_workspace_id,
            "origin_session_id": candidate.origin_session_id,
            "producer_kind": candidate.producer_kind.value,
            "producer_entry_id": candidate.producer_entry_id,
            "producer_tool_call_id": candidate.producer_tool_call_id,
            "trigger_user_entry_id": candidate.trigger_user_entry_id,
            "producer_candidate_ordinal": candidate.producer_candidate_ordinal,
            "proposal": candidate.proposal.semantic_payload(),
            "tool_result_refs": tuple(
                (
                    r.origin_session_id,
                    r.tool_result_id,
                    r.ordinal,
                    r.evidence_kind.value,
                    r.citation_visibility.value,
                )
                for r in candidate.tool_result_refs
            ),
            "basis_refs": tuple(
                (r.target_fact_id, r.target_scope_kind.value, r.target_scope_id, r.ordinal)
                for r in candidate.basis_refs
            ),
            "visible_memory": (
                candidate.visible_memory.disposition.value,
                candidate.visible_memory.fact_ids,
            ),
        },
    )


def prepare_memory_governance_acceptance(
    *,
    candidate: PreparedMemoryCandidateAcceptance,
    decision: FrozenMemoryGovernanceDecision,
    basis_items: Sequence[FrozenMemoryPublicFactProjection] = (),
    relation_targets: Sequence[FrozenMemoryPublicFactProjection] = (),
    tokenizer: MemoryRetrievalTokenizerV1 | None = None,
) -> PreparedMemoryGovernanceAcceptance:
    fact: PreparedMemoryFactDraft | None = None
    target: FrozenMemoryFactSettlementIdentity | None = None
    frozen_basis: tuple[FrozenMemoryFactSettlementIdentity, ...] = ()
    relation_drafts: tuple[PreparedMemoryRelationDraft, ...] = ()
    branches = (MemoryGovernanceSettlementBranch.SKIP,)
    if decision.decision_kind is not MemoryDecisionKind.SKIP:
        assert decision.final_kind is not None
        validate_final_kind_shape(candidate.proposal, decision.final_kind)
        semantic_digest = memory_fact_semantic_digest(
            kind=decision.final_kind,
            statement=candidate.proposal.statement,
            applies_when=candidate.proposal.applies_when,
            do_not_apply_when=candidate.proposal.do_not_apply_when,
        )
        tokenizer_owner = tokenizer or MemoryRetrievalTokenizerV1()
        terms = tokenizer_owner.tokenize(
            candidate.proposal.statement,
            candidate.proposal.applies_when,
            *candidate.proposal.do_not_apply_when,
        )
        fact = PreparedMemoryFactDraft(
            fact_id=_stable_id(
                "memory",
                candidate.candidate_id,
                semantic_digest,
            ),
            memory_domain_id=candidate.memory_domain_id,
            scope_kind=candidate.proposal.scope_kind,
            scope_id=candidate.proposal.scope_id,
            source_candidate_id=candidate.candidate_id,
            fact_kind=decision.final_kind,
            statement=candidate.proposal.statement,
            applies_when=candidate.proposal.applies_when,
            do_not_apply_when=candidate.proposal.do_not_apply_when,
            fact_semantic_digest=semantic_digest,
            search_contract_id=MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_ID,
            search_contract_version=MEMORY_RETRIEVAL_TOKENIZER_CONTRACT_VERSION,
            search_terms=terms,
        )
        basis_by_id = {item.fact_id: item for item in basis_items}
        if len(basis_by_id) != len(tuple(basis_items)):
            raise ValueError("prepared governance basis is duplicated")
        expected_basis_ids = tuple(item.target_fact_id for item in candidate.basis_refs)
        if set(basis_by_id) != set(expected_basis_ids):
            raise ValueError("prepared governance basis identity drifted")
        ordered_basis: list[FrozenMemoryFactSettlementIdentity] = []
        for reference in candidate.basis_refs:
            item = basis_by_id[reference.target_fact_id]
            if (
                item.scope_kind is not reference.target_scope_kind
                or item.scope_id != reference.target_scope_id
                or item.lifecycle != "ACTIVE"
            ):
                raise ValueError("prepared governance basis scope/lifecycle drifted")
            ordered_basis.append(
                freeze_memory_fact_settlement_identity(
                    memory_domain_id=candidate.memory_domain_id,
                    item=item,
                )
            )
        frozen_basis = tuple(ordered_basis)
        if fact.fact_kind is not MemoryFactKind.DECISION and frozen_basis:
            raise ValueError("non-DECISION governance acceptance carries basis")

        targets_by_id = {item.fact_id: item for item in relation_targets}
        if len(targets_by_id) != len(tuple(relation_targets)):
            raise ValueError("prepared governance targets are duplicated")
        if decision.related_target_fact_id is not None:
            try:
                selected = targets_by_id[decision.related_target_fact_id]
            except KeyError as exc:
                raise ValueError("prepared governance target is not frozen") from exc
            target = freeze_memory_fact_settlement_identity(
                memory_domain_id=candidate.memory_domain_id,
                item=selected,
            )
            if target.expected_lifecycle != "ACTIVE":
                raise ValueError("prepared governance target is not ACTIVE")
            if target.scope_kind is not fact.scope_kind or target.scope_id != fact.scope_id:
                raise ValueError("prepared governance relation crosses exact scope")
            same_kind = target.fact_kind is fact.fact_kind
            if decision.decision_kind is MemoryDecisionKind.ACCEPT_AND_CONTRADICT:
                if not same_kind:
                    raise ValueError("prepared contradiction crosses memory kind")
                relation_kind = MemoryRelationKind.CONTRADICTS
            elif decision.decision_kind is MemoryDecisionKind.ACCEPT_AND_SUPERSEDE:
                expected_same_kind = (
                    decision.supersede_mode
                    is MemorySupersedeMode.SAME_KIND_REPLACEMENT
                )
                if same_kind != expected_same_kind:
                    raise ValueError("prepared supersede mode/kind matrix drifted")
                relation_kind = MemoryRelationKind.SUPERSEDES
            else:
                raise ValueError("plain acceptance carries a relation target")
            relation_drafts = (
                _prepare_memory_relation_draft(
                    candidate_id=candidate.candidate_id,
                    source=fact,
                    target=target,
                    relation_kind=relation_kind,
                    supersede_mode=decision.supersede_mode,
                    ordinal=None,
                ),
            )
        elif targets_by_id:
            raise ValueError("unused governance relation targets are forbidden")
        elif decision.decision_kind is MemoryDecisionKind.ACCEPT:
            relation_drafts = tuple(
                _prepare_memory_relation_draft(
                    candidate_id=candidate.candidate_id,
                    source=fact,
                    target=item,
                    relation_kind=MemoryRelationKind.BASED_ON,
                    supersede_mode=None,
                    ordinal=ordinal,
                )
                for ordinal, item in enumerate(frozen_basis)
            )

        branch_values = [MemoryGovernanceSettlementBranch.ACCEPTANCE]
        if fact.fact_kind is MemoryFactKind.RESPONSE_PREFERENCE or (
            target is not None
            and target.fact_kind is MemoryFactKind.RESPONSE_PREFERENCE
        ):
            branch_values.append(
                MemoryGovernanceSettlementBranch.RESPONSE_PREFERENCE_CAPACITY_SKIP
            )
        if decision.decision_kind is MemoryDecisionKind.ACCEPT:
            branch_values.append(MemoryGovernanceSettlementBranch.EXACT_DUPLICATE_SKIP)
        else:
            branch_values.append(
                MemoryGovernanceSettlementBranch.EXISTING_SOURCE_RELATION
            )
        branches = tuple(branch_values)

    terminal_draft = (
        PreparedMemoryCandidateTerminalDraft(
            status=MemoryCandidateStatus.SKIPPED,
            decision_kind=MemoryDecisionKind.SKIP,
            final_kind=None,
            decision_reason_code=decision.reason_code,
            decision_public_summary=decision.public_summary,
            related_target_fact_id=None,
            duplicate_winner_fact_id=None,
            accepted_fact_id=None,
            applied_existing_fact_id=None,
        )
        if fact is None
        else PreparedMemoryCandidateTerminalDraft(
            status=MemoryCandidateStatus.ACCEPTED,
            decision_kind=decision.decision_kind,
            final_kind=fact.fact_kind,
            decision_reason_code=None,
            decision_public_summary=decision.public_summary,
            related_target_fact_id=decision.related_target_fact_id,
            duplicate_winner_fact_id=None,
            accepted_fact_id=fact.fact_id,
            applied_existing_fact_id=None,
        )
    )
    values = {
        "candidate_id": candidate.candidate_id,
        "candidate_acceptance_digest": candidate.candidate_acceptance_digest,
        "memory_domain_id": candidate.memory_domain_id,
        "origin_workspace_id": candidate.origin_workspace_id,
        "scope_kind": candidate.proposal.scope_kind,
        "scope_id": candidate.proposal.scope_id,
        "decision": decision,
        "fact": fact,
        "expected_candidate_status": MemoryCandidateStatus.PROCESSING,
        "target": target,
        "basis_targets": frozen_basis,
        "relation_drafts": relation_drafts,
        "terminal_draft": terminal_draft,
        "compatible_settlement_branches": branches,
    }
    return PreparedMemoryGovernanceAcceptance(**values)


def freeze_memory_fact_settlement_identity(
    *,
    memory_domain_id: str,
    item: FrozenMemoryPublicFactProjection,
) -> FrozenMemoryFactSettlementIdentity:
    return FrozenMemoryFactSettlementIdentity(
        fact_id=item.fact_id,
        memory_domain_id=memory_domain_id,
        scope_kind=item.scope_kind,
        scope_id=item.scope_id,
        fact_kind=item.fact_kind,
        statement=item.statement,
        applies_when=item.applies_when,
        do_not_apply_when=item.do_not_apply_when,
        fact_semantic_digest=item.fact_semantic_digest,
        expected_lifecycle=item.lifecycle,
    )


def _prepare_memory_relation_draft(
    *,
    candidate_id: str,
    source: PreparedMemoryFactDraft,
    target: FrozenMemoryFactSettlementIdentity,
    relation_kind: MemoryRelationKind,
    supersede_mode: MemorySupersedeMode | None,
    ordinal: int | None,
) -> PreparedMemoryRelationDraft:
    relation_id = memory_relation_id(
        memory_domain_id=source.memory_domain_id,
        source_scope_kind=source.scope_kind,
        source_scope_id=source.scope_id,
        source_fact_id=source.fact_id,
        relation_kind=relation_kind,
        target_scope_kind=target.scope_kind,
        target_scope_id=target.scope_id,
        target_fact_id=target.fact_id,
        supersede_mode=supersede_mode,
    )
    return PreparedMemoryRelationDraft(
        relation_id=relation_id,
        decision_candidate_id=candidate_id,
        source_scope_kind=source.scope_kind,
        source_scope_id=source.scope_id,
        source_fact_id=source.fact_id,
        source_fact_kind=source.fact_kind,
        relation_kind=relation_kind,
        target=target,
        supersede_mode=supersede_mode,
        ordinal=ordinal,
        expected_target_lifecycle_before="ACTIVE",
        expected_target_lifecycle_after=(
            "SUPERSEDED"
            if relation_kind is MemoryRelationKind.SUPERSEDES
            else "ACTIVE"
        ),
    )


def prepare_existing_source_relation_settlement(
    *,
    parent: PreparedMemoryGovernanceAcceptance,
    existing_source: FrozenMemoryFactSettlementIdentity,
    target: FrozenMemoryFactSettlementIdentity,
    disposition: ExistingSourceRelationDisposition,
    existing_relation_id: str | None = None,
    existing_relation_decision_candidate_id: str | None = None,
    existing_relation_source_fact_id: str | None = None,
    existing_relation_target_fact_id: str | None = None,
) -> PreparedExistingSourceRelationSettlement:
    decision = parent.decision
    if decision.decision_kind is MemoryDecisionKind.ACCEPT_AND_SUPERSEDE:
        relation_kind = MemoryRelationKind.SUPERSEDES
    elif decision.decision_kind is MemoryDecisionKind.ACCEPT_AND_CONTRADICT:
        relation_kind = MemoryRelationKind.CONTRADICTS
    else:
        raise ValueError("plain acceptance cannot settle an existing source relation")
    assert decision.related_target_fact_id is not None
    fact = parent.fact
    assert fact is not None
    if (
        existing_source.fact_id == target.fact_id
        or existing_source.memory_domain_id != parent.memory_domain_id
        or target.memory_domain_id != parent.memory_domain_id
        or existing_source.scope_kind is not parent.scope_kind
        or existing_source.scope_id != parent.scope_id
        or existing_source.fact_kind is not fact.fact_kind
        or existing_source.statement != fact.statement
        or existing_source.applies_when != fact.applies_when
        or existing_source.do_not_apply_when != fact.do_not_apply_when
        or existing_source.fact_semantic_digest != fact.fact_semantic_digest
        or target.fact_id != decision.related_target_fact_id
    ):
        raise ValueError("existing-source settlement does not join prepared acceptance")
    if relation_kind is MemoryRelationKind.CONTRADICTS:
        if (
            target.scope_kind is not existing_source.scope_kind
            or target.scope_id != existing_source.scope_id
            or target.fact_kind is not existing_source.fact_kind
        ):
            raise ValueError("existing contradiction endpoint matrix is invalid")
    else:
        same_scope = (
            target.scope_kind is existing_source.scope_kind
            and target.scope_id == existing_source.scope_id
        )
        same_kind = target.fact_kind is existing_source.fact_kind
        if not same_scope or (
            decision.supersede_mode is MemorySupersedeMode.SAME_KIND_REPLACEMENT
            and not same_kind
        ) or (
            decision.supersede_mode is MemorySupersedeMode.TAXONOMY_CORRECTION
            and same_kind
        ):
            raise ValueError("existing supersede endpoint matrix is invalid")
    relation_id = memory_relation_id(
        memory_domain_id=existing_source.memory_domain_id,
        source_scope_kind=existing_source.scope_kind,
        source_scope_id=existing_source.scope_id,
        source_fact_id=existing_source.fact_id,
        relation_kind=relation_kind,
        target_scope_kind=target.scope_kind,
        target_scope_id=target.scope_id,
        target_fact_id=target.fact_id,
        supersede_mode=decision.supersede_mode,
    )
    values = {
        "parent": parent,
        "candidate_id": parent.candidate_id,
        "existing_source": existing_source,
        "target": target,
        "relation_kind": relation_kind,
        "supersede_mode": decision.supersede_mode,
        "prepared_relation_id": relation_id,
        "settled_target_lifecycle": (
            "SUPERSEDED"
            if relation_kind is MemoryRelationKind.SUPERSEDES
            else "ACTIVE"
        ),
        "disposition": disposition,
        "existing_relation_id": existing_relation_id,
        "existing_relation_decision_candidate_id": (
            existing_relation_decision_candidate_id
        ),
        "existing_relation_source_fact_id": existing_relation_source_fact_id,
        "existing_relation_target_fact_id": existing_relation_target_fact_id,
    }
    return PreparedExistingSourceRelationSettlement(**values)


def memory_relation_id(
    *,
    memory_domain_id: str,
    source_scope_kind: MemoryScopeKind,
    source_scope_id: str,
    source_fact_id: str,
    relation_kind: MemoryRelationKind,
    target_scope_kind: MemoryScopeKind,
    target_scope_id: str,
    target_fact_id: str,
    supersede_mode: MemorySupersedeMode | None,
) -> str:
    if relation_kind is MemoryRelationKind.CONTRADICTS:
        source_endpoint = (
            source_scope_kind.value,
            source_scope_id,
            source_fact_id,
        )
        target_endpoint = (
            target_scope_kind.value,
            target_scope_id,
            target_fact_id,
        )
        if target_endpoint < source_endpoint:
            (
                source_scope_kind,
                target_scope_kind,
                source_scope_id,
                target_scope_id,
                source_fact_id,
                target_fact_id,
            ) = (
                target_scope_kind,
                source_scope_kind,
                target_scope_id,
                source_scope_id,
                target_fact_id,
                source_fact_id,
            )
    identity = canonical_json_bytes(
        (
            memory_domain_id,
            source_scope_kind.value,
            source_scope_id,
            source_fact_id,
            relation_kind.value,
            target_scope_kind.value,
            target_scope_id,
            target_fact_id,
            None if supersede_mode is None else supersede_mode.value,
        )
    )
    return "memory-relation:" + sha256(identity).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    encoded = "\0".join(parts).encode("utf-8")
    return f"{prefix}:" + sha256(encoded).hexdigest()


def memory_fact_semantic_digest(
    *, kind: MemoryFactKind, statement: str, applies_when: str | None, do_not_apply_when: Sequence[str]
) -> str:
    return digest(
        "pulsara:memory-fact-semantic:v1",
        {
            "fact_kind": kind.value,
            "statement": normalize_memory_text(statement),
            "applies_when": None if applies_when is None else normalize_memory_text(applies_when),
            "do_not_apply_when": tuple(normalize_memory_text(x) for x in do_not_apply_when),
        },
    )


def validate_final_kind_shape(proposal: FrozenMemoryProposal, kind: MemoryFactKind) -> None:
    has_applies = proposal.applies_when is not None
    has_exclusions = bool(proposal.do_not_apply_when)
    has_basis = bool(proposal.based_on_memory_ids)
    if kind is MemoryFactKind.ACTION_RULE:
        if not has_applies or has_basis:
            raise ValueError("ACTION_RULE structure is invalid")
    elif has_applies or has_exclusions:
        raise ValueError("only ACTION_RULE can carry applicability fields")
    if kind is MemoryFactKind.DECISION:
        pass
    elif has_basis:
        raise ValueError("only DECISION can carry BASED_ON references")
    if kind is MemoryFactKind.USER_PROFILE and proposal.scope_kind is not MemoryScopeKind.USER:
        raise ValueError("USER_PROFILE requires USER scope")
    if kind is MemoryFactKind.RESPONSE_PREFERENCE and len(proposal.statement.encode("utf-8")) > MAXIMUM_RESPONSE_PREFERENCE_STATEMENT_BYTES:
        raise ValueError("RESPONSE_PREFERENCE exceeds statement bound")


def legal_memory_final_kinds(
    proposal: FrozenMemoryProposal,
) -> tuple[MemoryFactKind, ...]:
    """Return the unique shape validator's complete closed legal-kind set."""

    legal: list[MemoryFactKind] = []
    for kind in MemoryFactKind:
        try:
            validate_final_kind_shape(proposal, kind)
        except ValueError:
            continue
        legal.append(kind)
    return tuple(legal)


def visible_scope_predicate(binding: FrozenMemoryReadScopeBinding) -> tuple[tuple[str, str], ...]:
    return tuple((item.kind.value, item.scope_id) for item in binding.readable_scopes)


def normalize_memory_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(domain: str, value: object) -> str:
    return "sha256:" + sha256(domain.encode("utf-8") + b"\x00" + canonical_json_bytes(value)).hexdigest()


def _unique_ids(values: Sequence[str], name: str) -> tuple[str, ...]:
    result = tuple(str(item) for item in values)
    if any(not item for item in result) or len(result) != len(set(result)):
        raise ValueError(f"{name} identities are empty or duplicated")
    return result


def _bounded_text(value: str, minimum: int, maximum: int, name: str) -> None:
    size = len(value.encode("utf-8"))
    if not minimum <= size <= maximum:
        raise ValueError(f"{name} is outside its UTF-8 bound")


__all__ = [name for name in globals() if name.startswith("Memory") or name.startswith("Prepared") or name.startswith("Frozen") or name.startswith("MAXIMUM_")] + [
    "canonical_json_bytes",
    "digest",
    "freeze_memory_fact_settlement_identity",
    "memory_fact_semantic_digest",
    "memory_governance_source_item_payload",
    "memory_governance_source_projection_bytes",
    "legal_memory_final_kinds",
    "memory_public_fact_payload",
    "memory_response_preference_item_payload",
    "memory_relation_id",
    "normalize_memory_text",
    "prepare_memory_candidate",
    "prepare_existing_source_relation_settlement",
    "prepare_memory_governance_acceptance",
    "prepared_memory_candidate_digest",
    "strongest_memory_use_policy",
    "validate_final_kind_shape",
    "visible_scope_predicate",
]
