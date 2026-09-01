"""Pure contracts for the process-local Round 5B compaction workflow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Literal

from pulsara_agent.conversation_kernel.contracts import (
    CanonicalContent,
    CommittedEventDraft,
    CommittedEventSubject,
)
from pulsara_agent.conversation_kernel.assembler import (
    MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.vocabulary import (
    CommittedEventType,
    SubjectSlot,
)
from pulsara_agent.model_input.contracts import (
    FrozenProviderInputItem,
    LateToolOutcomeObservation,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
    MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS,
    ModelInputCompileBinding,
    ModelInputScopeKind,
    ProviderToolResultClosure,
    late_tool_outcome_observation_leaf,
    provider_input_item_fingerprint,
    provider_input_item_logical_utf8_bytes,
    provider_tool_result_closure_leaf,
)
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputEpochView,
    MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES,
    provider_input_prefix_fingerprint,
)
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
)
from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.llm.input import LLMMessage
from pulsara_agent.llm.request import FrozenProviderWireInputQuote
from pulsara_agent.primitives.context import canonical_json_bytes, context_fingerprint


COMPACTION_SOURCE_LINEAGE_CONTRACT = "pulsara.compaction-source-lineage.v1"
COMPACTION_CANONICAL_RANGE_CONTRACT = "pulsara.compaction-canonical-range.v1"
COMPACTION_SNAPSHOT_COMPILER_CONTRACT = (
    "pulsara.context-snapshot-carrier.v2-durable-continuation"
)
COMPACTION_SUMMARY_PROMPT_CONTRACT = (
    "pulsara.context-compaction-summary.v4-temporal-handoff"
)
COMPACTION_MODEL_CONTRACT = "pulsara.primary-model-compaction.v1"
CONTEXT_SNAPSHOT_MEDIA_TYPE = "application/vnd.pulsara.context-snapshot+json"
CONTEXT_SNAPSHOT_CODEC = "utf-8"


def compaction_summary_message_prefix_fingerprint(
    messages: tuple[LLMMessage, ...],
) -> str:
    """Hash provider-neutral semantic messages without serializing DTO objects."""

    return context_fingerprint(
        "pulsara.compaction-summary-semantic-prefix.v1",
        {
            "count": len(messages),
            "semantic": provider_input_prefix_fingerprint(
                system_prompt="", tools=(), messages=messages
            ),
        },
    )


class CompactionTrigger(StrEnum):
    MANUAL = "MANUAL"
    AUTO_ACTIVE_CONTEXT = "AUTO_ACTIVE_CONTEXT"
    MID_TURN_FOLLOWUP = "MID_TURN_FOLLOWUP"


class CompactionTargetBranch(StrEnum):
    ACTIVE_INSTALLATION = "ACTIVE_INSTALLATION"
    IDLE_BASE_ONLY = "IDLE_BASE_ONLY"


class CompactionContinuationMode(StrEnum):
    RESUME_ACTIVE_TURN = "RESUME_ACTIVE_TURN"
    AWAIT_NEXT_USER = "AWAIT_NEXT_USER"


class CompactionActiveRequestLocation(StrEnum):
    SNAPSHOT_EXACT = "SNAPSHOT_EXACT"
    CANONICAL_SUFFIX = "CANONICAL_SUFFIX"


class CompactionDisposition(StrEnum):
    COMPACTED = "COMPACTED"
    NOT_NEEDED = "NOT_NEEDED"
    DEFERRED_TO_SAFE_POINT = "DEFERRED_TO_SAFE_POINT"
    REJECTED_BUSY = "REJECTED_BUSY"
    FAILED = "FAILED"


class CompactionAttemptPhase(StrEnum):
    PREPARING = "PREPARING"
    STREAMING = "STREAMING"
    REPAIRING = "REPAIRING"
    VALIDATED = "VALIDATED"
    ADOPTION_PREPARED = "ADOPTION_PREPARED"
    SETTLING = "SETTLING"
    ACTIVE_EPOCH_INSTALLED = "ACTIVE_EPOCH_INSTALLED"
    IDLE_BASE_ADOPTED = "IDLE_BASE_ADOPTED"
    DISCARDED = "DISCARDED"
    CLOSED = "CLOSED"


class CompactionLineageBaseKind(StrEnum):
    FULL_HISTORY_GENESIS = "FULL_HISTORY_GENESIS"
    CURRENT_SNAPSHOT = "CURRENT_SNAPSHOT"


class CompactionConfirmationKind(StrEnum):
    FULL = "FULL"
    NONE = "NONE"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True, slots=True)
class PreparedManualCompactionCommand:
    """Stable semantic command accepted before process-local execution starts."""

    session_id: str
    command_id: str
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    target_turn_id: str
    expected_active_turn_id: str | None
    force: bool
    semantic_digest: str

    def __post_init__(self) -> None:
        if not self.session_id or not self.command_id or not self.target_turn_id:
            raise ValueError("manual compaction command identity is incomplete")
        if (self.scope_kind is ModelInputScopeKind.ROOT) != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("manual compaction command scope union is invalid")
        expected = manual_compaction_command_semantic_digest(
            session_id=self.session_id,
            command_id=self.command_id,
            scope_kind=self.scope_kind,
            scope_subagent_task_id=self.scope_subagent_task_id,
            target_turn_id=self.target_turn_id,
            expected_active_turn_id=self.expected_active_turn_id,
            force=self.force,
        )
        if self.semantic_digest != expected:
            raise ValueError("manual compaction command digest mismatch")


def manual_compaction_command_semantic_digest(
    *,
    session_id: str,
    command_id: str,
    scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    target_turn_id: str,
    expected_active_turn_id: str | None,
    force: bool,
) -> str:
    return context_fingerprint(
        "pulsara:compact-context-command:v1",
        {
            "session_id": session_id,
            "command_id": command_id,
            "scope_kind": scope_kind.value,
            "scope_subagent_task_id": scope_subagent_task_id,
            "target_turn_id": target_turn_id,
            "expected_active_turn_id": expected_active_turn_id,
            "force": force,
        },
    )


def build_prepared_manual_compaction_command(
    *,
    session_id: str,
    command_id: str,
    scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    target_turn_id: str,
    expected_active_turn_id: str | None,
    force: bool,
) -> PreparedManualCompactionCommand:
    return PreparedManualCompactionCommand(
        session_id=session_id,
        command_id=command_id,
        scope_kind=scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        target_turn_id=target_turn_id,
        expected_active_turn_id=expected_active_turn_id,
        force=force,
        semantic_digest=manual_compaction_command_semantic_digest(
            session_id=session_id,
            command_id=command_id,
            scope_kind=scope_kind,
            scope_subagent_task_id=scope_subagent_task_id,
            target_turn_id=target_turn_id,
            expected_active_turn_id=expected_active_turn_id,
            force=force,
        ),
    )


def manual_compaction_stable_suffix(*, session_id: str, command_id: str) -> str:
    """Return the stable row-id suffix for one semantic manual command."""

    if not session_id or not command_id:
        raise ValueError("manual compaction stable identity is incomplete")
    return context_fingerprint(
        "pulsara:manual-compaction-stable-identity:v1",
        {"session_id": session_id, "command_id": command_id},
    ).removeprefix("sha256:")


class CompactionSourceCompatibility(StrEnum):
    EMPTY_COLD = "EMPTY_COLD"
    COMPATIBLE_APPEND = "COMPATIBLE_APPEND"
    PENDING_NON_COMPACTION_RESET = "PENDING_NON_COMPACTION_RESET"


@dataclass(frozen=True, slots=True)
class ResolvedCompactionPolicy:
    enabled: bool = True
    automatic_enabled: bool = True
    manual_enabled: bool = True
    auto_trigger_ratio: float = 0.85
    post_compaction_target_ratio: float = 0.55
    minimum_reclaim_tokens: int = 16_384
    maximum_retained_tool_groups: int = 3
    maximum_retained_tail_utf8_bytes: int = 2 << 20
    maximum_recent_human_messages: int = 3
    maximum_recent_human_utf8_bytes: int = 65_536
    maximum_runtime_handoff_utf8_bytes: int = 32_768
    planning_attempt_seconds: float = 120.0
    maximum_consecutive_auto_failures: int = 3

    def __post_init__(self) -> None:
        if not 0 < self.auto_trigger_ratio < 1:
            raise ValueError("compaction trigger ratio must be between zero and one")
        if not 0 < self.post_compaction_target_ratio < self.auto_trigger_ratio:
            raise ValueError("compaction target must be below its trigger")
        if self.minimum_reclaim_tokens < 1:
            raise ValueError("compaction minimum reclaim must be positive")
        if self.maximum_retained_tool_groups < 1:
            raise ValueError("retained tool-group policy must be positive")
        if self.maximum_recent_human_messages < 1:
            raise ValueError("recent human-message policy must be positive")
        if not 8 << 10 <= self.maximum_runtime_handoff_utf8_bytes <= 256 << 10:
            raise ValueError("runtime-handoff byte bound is invalid")
        if self.maximum_retained_tail_utf8_bytes < 1:
            raise ValueError("retained-tail byte bound is invalid")
        if self.planning_attempt_seconds <= 0:
            raise ValueError("compaction planning watchdog must be positive")
        if self.maximum_consecutive_auto_failures != 3:
            raise ValueError("automatic compaction failure circuit must be exact three")


@dataclass(frozen=True, slots=True)
class CompatibleAppendCompactionProjection:
    append_only_messages: tuple[LLMMessage, ...] = field(repr=False)
    final_estimate: TokenEstimate
    logical_utf8_bytes: int

    def __post_init__(self) -> None:
        if self.logical_utf8_bytes < 0:
            raise ValueError("compatible compaction projection is invalid")


@dataclass(frozen=True, slots=True)
class ColdRebuildCompactionProjection:
    system_prompt: str
    full_messages: tuple[LLMMessage, ...] = field(repr=False)
    final_estimate: TokenEstimate
    logical_utf8_bytes: int

    def __post_init__(self) -> None:
        if not self.system_prompt or self.logical_utf8_bytes < 0:
            raise ValueError("cold compaction projection is invalid")


FrozenCompactionProviderProjection = (
    CompatibleAppendCompactionProjection | ColdRebuildCompactionProjection
)


@dataclass(frozen=True, slots=True)
class ResolvedCompactionHeadroomBounds:
    """One mechanically-derived quote for the largest legal admission."""

    maximum_canonical_items: int
    maximum_canonical_utf8_bytes: int
    maximum_epoch_logical_utf8_bytes: int
    reserved_canonical_items: int
    reserved_canonical_utf8_bytes: int
    reserved_epoch_logical_utf8_bytes: int
    resolved_hard_bound_set_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not 0 < self.reserved_canonical_items < self.maximum_canonical_items
            or not 0
            < self.reserved_canonical_utf8_bytes
            < self.maximum_canonical_utf8_bytes
            or not 0
            < self.reserved_epoch_logical_utf8_bytes
            < self.maximum_epoch_logical_utf8_bytes
        ):
            raise ValueError("compaction headroom bounds are invalid")
        values = {
            "maximum_canonical_items": self.maximum_canonical_items,
            "maximum_canonical_utf8_bytes": self.maximum_canonical_utf8_bytes,
            "maximum_epoch_logical_utf8_bytes": (self.maximum_epoch_logical_utf8_bytes),
            "reserved_canonical_items": self.reserved_canonical_items,
            "reserved_canonical_utf8_bytes": self.reserved_canonical_utf8_bytes,
            "reserved_epoch_logical_utf8_bytes": (
                self.reserved_epoch_logical_utf8_bytes
            ),
        }
        if self.resolved_hard_bound_set_fingerprint != context_fingerprint(
            "pulsara.compaction-resource-headroom.v1", values
        ):
            raise ValueError("compaction headroom fingerprint mismatch")

    @property
    def soft_canonical_item_limit(self) -> int:
        return self.maximum_canonical_items - self.reserved_canonical_items

    @property
    def soft_canonical_utf8_byte_limit(self) -> int:
        return self.maximum_canonical_utf8_bytes - self.reserved_canonical_utf8_bytes

    @property
    def soft_epoch_logical_utf8_byte_limit(self) -> int:
        return (
            self.maximum_epoch_logical_utf8_bytes
            - self.reserved_epoch_logical_utf8_bytes
        )


def resolved_compaction_headroom_bounds() -> ResolvedCompactionHeadroomBounds:
    # These reserves are admission maxima, not tuning knobs.  In particular,
    # the canonical and epoch byte reserves must cover one largest assistant
    # message that the live assembler can legally accept; otherwise a response
    # below the old soft boundary could make the next reader call cross its
    # hard limit before compaction gets another chance to run.
    maximum_next_admission_bytes = max(
        STAGE2_LIMITS.prompt_hard_bytes,
        MAXIMUM_COMPLETED_ASSISTANT_MESSAGE_UTF8_BYTES,
        STAGE2_LIMITS.tool_result_hard_bytes,
    )
    values = {
        "maximum_canonical_items": MAXIMUM_CANONICAL_PROVIDER_INPUT_ITEMS,
        "maximum_canonical_utf8_bytes": MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES,
        "maximum_epoch_logical_utf8_bytes": MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES,
        "reserved_canonical_items": 296,
        "reserved_canonical_utf8_bytes": maximum_next_admission_bytes,
        "reserved_epoch_logical_utf8_bytes": maximum_next_admission_bytes,
    }
    return ResolvedCompactionHeadroomBounds(
        **values,
        resolved_hard_bound_set_fingerprint=context_fingerprint(
            "pulsara.compaction-resource-headroom.v1", values
        ),
    )


@dataclass(frozen=True, slots=True)
class CompactionPhysicalWorkingSetReport:
    post_base_item_count: int
    post_base_canonical_utf8_bytes: int
    continuity_epoch_logical_utf8_bytes: int
    resolved_hard_bound_set_fingerprint: str

    def __post_init__(self) -> None:
        if (
            min(
                self.post_base_item_count,
                self.post_base_canonical_utf8_bytes,
                self.continuity_epoch_logical_utf8_bytes,
            )
            < 0
        ):
            raise ValueError("compaction working-set report is invalid")


@dataclass(frozen=True, slots=True)
class FrozenCompactionHeadroomPreflight:
    """Metadata-only quote bound to one prepared canonical cut."""

    session_id: str
    turn_id: str
    context_binding_revision_id: str
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None
    effective_materialization_lineage_floor: int
    provider_input_through_sequence: int
    post_base_item_count: int
    post_base_canonical_utf8_bytes: int
    resolved_hard_bound_set_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or not self.turn_id
            or not self.context_binding_revision_id
            or (self.scope_kind is ModelInputScopeKind.ROOT)
            != (self.scope_subagent_task_id is None)
            or min(
                self.effective_materialization_lineage_floor,
                self.provider_input_through_sequence,
                self.post_base_item_count,
                self.post_base_canonical_utf8_bytes,
            )
            < 0
            or self.effective_materialization_lineage_floor
            > self.provider_input_through_sequence
        ):
            raise ValueError("compaction headroom preflight is invalid")


def freeze_compaction_headroom_preflight(
    *,
    session_id: str,
    turn_id: str,
    context_binding_revision_id: str,
    scope_kind: ModelInputScopeKind,
    scope_subagent_task_id: str | None,
    effective_materialization_lineage_floor: int,
    provider_input_through_sequence: int,
    post_base_item_count: int,
    post_base_canonical_utf8_bytes: int,
) -> FrozenCompactionHeadroomPreflight:
    bounds = resolved_compaction_headroom_bounds()
    return FrozenCompactionHeadroomPreflight(
        session_id=session_id,
        turn_id=turn_id,
        context_binding_revision_id=context_binding_revision_id,
        scope_kind=scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        effective_materialization_lineage_floor=(
            effective_materialization_lineage_floor
        ),
        provider_input_through_sequence=provider_input_through_sequence,
        post_base_item_count=post_base_item_count,
        post_base_canonical_utf8_bytes=post_base_canonical_utf8_bytes,
        resolved_hard_bound_set_fingerprint=(
            bounds.resolved_hard_bound_set_fingerprint
        ),
    )


def _compaction_projection_identity_digest(
    projection: FrozenCompactionProviderProjection,
    compile_binding: ModelInputCompileBinding,
    predecessor: FrozenProviderInputEpochView | None,
) -> str:
    estimate = projection.final_estimate
    estimate_value = {
        "system": estimate.system_tokens,
        "messages": estimate.message_tokens,
        "message_by_index": estimate.message_tokens_by_index,
        "tools": estimate.tool_tokens,
        "envelope": estimate.envelope_tokens,
        "total": estimate.total_input_tokens,
    }
    if isinstance(projection, CompatibleAppendCompactionProjection):
        return context_fingerprint(
            "pulsara.compatible-append-compaction-projection.v1",
            {
                "predecessor": (
                    None
                    if predecessor is None
                    else predecessor.semantic_prefix_fingerprint
                ),
                "append": provider_input_prefix_fingerprint(
                    system_prompt="", tools=(), messages=projection.append_only_messages
                ),
                "estimate": estimate_value,
                "logical_bytes": projection.logical_utf8_bytes,
            },
        )
    return context_fingerprint(
        "pulsara.cold-rebuild-compaction-projection.v1",
        {
            "system": projection.system_prompt,
            "input": provider_input_prefix_fingerprint(
                system_prompt=projection.system_prompt,
                tools=compile_binding.tool_surface.tool_specs,
                messages=projection.full_messages,
            ),
            "estimate": estimate_value,
            "logical_bytes": projection.logical_utf8_bytes,
        },
    )


def _compaction_working_set_identity_digest(
    report: CompactionPhysicalWorkingSetReport,
) -> str:
    return context_fingerprint(
        "pulsara.compaction-working-set-report.v1",
        {
            "items": report.post_base_item_count,
            "canonical_bytes": report.post_base_canonical_utf8_bytes,
            "epoch_bytes": report.continuity_epoch_logical_utf8_bytes,
            "resolved_hard_bounds": report.resolved_hard_bound_set_fingerprint,
        },
    )


@dataclass(frozen=True, slots=True)
class FrozenCompactionSourceView:
    compatibility: CompactionSourceCompatibility
    canonical_dispatch_read: FrozenCanonicalProviderDispatchRead = field(repr=False)
    normal_compile_binding: ModelInputCompileBinding
    predecessor_epoch_view: FrozenProviderInputEpochView | None = field(repr=False)
    provider_projection: FrozenCompactionProviderProjection = field(repr=False)
    provider_wire_quote: FrozenProviderWireInputQuote
    physical_working_set: CompactionPhysicalWorkingSetReport
    source_view_fingerprint: str

    def __post_init__(self) -> None:
        compatible = (
            self.compatibility is CompactionSourceCompatibility.COMPATIBLE_APPEND
        )
        if compatible != isinstance(
            self.provider_projection, CompatibleAppendCompactionProjection
        ):
            raise ValueError("compaction source compatibility/projection drifted")
        if compatible and self.predecessor_epoch_view is None:
            raise ValueError("compatible compaction view lacks its predecessor")
        if (
            self.compatibility is CompactionSourceCompatibility.EMPTY_COLD
            and self.predecessor_epoch_view is not None
        ):
            raise ValueError("empty-cold compaction view has a predecessor")
        canonical = self.canonical_dispatch_read.compile_snapshot.canonical_input
        if (
            canonical.identity.conversation_scope_kind
            is not self.normal_compile_binding.tool_surface.conversation_scope_kind
        ):
            raise ValueError("compaction source tool surface belongs to another scope")
        if (
            self.provider_wire_quote.estimator_fingerprint
            != self.normal_compile_binding.estimator_fingerprint
            or self.provider_wire_quote.effective_input_budget_tokens
            != self.normal_compile_binding.effective_input_budget_tokens
            or self.provider_wire_quote.semantic_estimated_input_tokens
            != self.provider_projection.final_estimate.total_input_tokens
        ):
            raise ValueError("compaction source wire quote does not exact-join")
        expected = context_fingerprint(
            "pulsara.frozen-compaction-source-view.v1",
            {
                "compatibility": self.compatibility.value,
                "dispatch_read": self.canonical_dispatch_read.composite_fingerprint,
                "compile_binding": self.normal_compile_binding.binding_fingerprint,
                "predecessor": (
                    None
                    if self.predecessor_epoch_view is None
                    else self.predecessor_epoch_view.semantic_prefix_fingerprint
                ),
                "projection": _compaction_projection_identity_digest(
                    self.provider_projection,
                    self.normal_compile_binding,
                    self.predecessor_epoch_view,
                ),
                "working_set": _compaction_working_set_identity_digest(
                    self.physical_working_set
                ),
            },
        )
        if self.source_view_fingerprint != expected:
            raise ValueError("compaction source view fingerprint mismatch")

    def materialized_system_prompt(self) -> str:
        if isinstance(self.provider_projection, CompatibleAppendCompactionProjection):
            assert self.predecessor_epoch_view is not None
            return self.predecessor_epoch_view.system_prompt
        return self.provider_projection.system_prompt

    def materialized_messages(self) -> tuple[LLMMessage, ...]:
        if isinstance(self.provider_projection, CompatibleAppendCompactionProjection):
            assert self.predecessor_epoch_view is not None
            return (
                self.predecessor_epoch_view.messages
                + self.provider_projection.append_only_messages
            )
        return self.provider_projection.full_messages

    @property
    def exact_safe_canonical_head(self) -> int:
        identity = (
            self.canonical_dispatch_read.compile_snapshot.canonical_input.identity
        )
        return identity.provider_input_through_sequence


@dataclass(frozen=True, slots=True)
class CompactionScope:
    session_id: str
    workspace_id: str
    turn_id: str
    scope_kind: ModelInputScopeKind
    scope_subagent_task_id: str | None

    def __post_init__(self) -> None:
        if not self.session_id or not self.workspace_id or not self.turn_id:
            raise ValueError("compaction scope identity is incomplete")
        if (self.scope_kind is ModelInputScopeKind.ROOT) != (
            self.scope_subagent_task_id is None
        ):
            raise ValueError("compaction scope union is invalid")

    @property
    def fingerprint(self) -> str:
        return context_fingerprint(
            "pulsara.compaction-scope.v1",
            {
                "session_id": self.session_id,
                "workspace_id": self.workspace_id,
                "turn_id": self.turn_id,
                "scope_kind": self.scope_kind.value,
                "scope_subagent_task_id": self.scope_subagent_task_id,
            },
        )


@dataclass(frozen=True, slots=True)
class CompactionSourceLineageBase:
    kind: CompactionLineageBaseKind
    scope: CompactionScope
    binding_revision_id: str
    binding_revision_ordinal: int
    persisted_revision_genesis_marker: int
    effective_materialization_lineage_floor: int
    snapshot_id: str | None = None
    prior_source_digest: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.binding_revision_id
            or self.binding_revision_ordinal < 0
            or self.persisted_revision_genesis_marker < 0
            or self.effective_materialization_lineage_floor < 0
        ):
            raise ValueError("compaction lineage base is incomplete")
        is_genesis = self.kind is CompactionLineageBaseKind.FULL_HISTORY_GENESIS
        if is_genesis != (
            self.snapshot_id is None and self.prior_source_digest is None
        ):
            raise ValueError("compaction lineage base union is invalid")
        if is_genesis and self.effective_materialization_lineage_floor != 0:
            raise ValueError("FULL_HISTORY lineage must start at zero")
        if not is_genesis and (
            not self.snapshot_id
            or self.prior_source_digest is None
            or not self.prior_source_digest.startswith("sha256:")
        ):
            raise ValueError("snapshot lineage identity is invalid")

    @property
    def fingerprint(self) -> str:
        return context_fingerprint(
            "pulsara.compaction-source-lineage-base.v1",
            {
                "kind": self.kind.value,
                "scope": self.scope.fingerprint,
                "binding_revision_id": self.binding_revision_id,
                "binding_revision_ordinal": self.binding_revision_ordinal,
                "effective_floor": self.effective_materialization_lineage_floor,
                "snapshot_id": self.snapshot_id,
                "prior_source_digest": self.prior_source_digest,
            },
        )


@dataclass(frozen=True, slots=True)
class FrozenCompactionCanonicalRange:
    scope: CompactionScope
    effective_materialization_lineage_floor: int
    source_through_sequence: int
    ordered_items: tuple[FrozenProviderInputItem, ...] = field(repr=False)
    closures: tuple[ProviderToolResultClosure, ...] = field(repr=False)
    late_outcomes: tuple[LateToolOutcomeObservation, ...] = field(repr=False)
    canonical_utf8_bytes: int

    def __post_init__(self) -> None:
        if (
            self.effective_materialization_lineage_floor < 0
            or self.source_through_sequence
            < self.effective_materialization_lineage_floor
            or self.canonical_utf8_bytes < 0
        ):
            raise ValueError("compaction canonical range coordinate is invalid")
        for item in self.ordered_items:
            sequence = item.source_entry_sequence
            if sequence is None or not (
                self.effective_materialization_lineage_floor
                < sequence
                <= self.source_through_sequence
            ):
                raise ValueError("compaction range contains an out-of-cut item")


@dataclass(frozen=True, slots=True)
class FrozenCompactionCanonicalRead:
    """One repeatable-read cut used by the pure compaction planner."""

    scope: CompactionScope
    turn_status: Literal["RUNNING", "COMPLETED", "INTERRUPTED"]
    dispatch_read: FrozenCanonicalProviderDispatchRead = field(repr=False)
    lineage_base: CompactionSourceLineageBase
    safe_head_range: FrozenCompactionCanonicalRange = field(repr=False)

    def __post_init__(self) -> None:
        identity = self.dispatch_read.compile_snapshot.canonical_input.identity
        if (
            identity.session_id != self.scope.session_id
            or identity.turn_id != self.scope.turn_id
            or identity.conversation_scope_kind is not self.scope.scope_kind
            or identity.scope_subagent_task_id != self.scope.scope_subagent_task_id
            or self.lineage_base.scope != self.scope
            or self.safe_head_range.scope != self.scope
            or self.safe_head_range.source_through_sequence
            != identity.provider_input_through_sequence
            or self.safe_head_range.effective_materialization_lineage_floor
            != self.lineage_base.effective_materialization_lineage_floor
        ):
            raise ValueError("compaction canonical read does not exact-join")


def freeze_compaction_canonical_read(
    *,
    scope: CompactionScope,
    turn_status: Literal["RUNNING", "COMPLETED", "INTERRUPTED"],
    dispatch_read: FrozenCanonicalProviderDispatchRead,
    lineage_base: CompactionSourceLineageBase,
    safe_head_range: FrozenCompactionCanonicalRange,
) -> FrozenCompactionCanonicalRead:
    return FrozenCompactionCanonicalRead(
        scope=scope,
        turn_status=turn_status,
        dispatch_read=dispatch_read,
        lineage_base=lineage_base,
        safe_head_range=safe_head_range,
    )


def freeze_compaction_canonical_range(
    *,
    scope: CompactionScope,
    effective_materialization_lineage_floor: int,
    source_through_sequence: int,
    ordered_items: tuple[FrozenProviderInputItem, ...],
    closures: tuple[ProviderToolResultClosure, ...],
    late_outcomes: tuple[LateToolOutcomeObservation, ...],
) -> FrozenCompactionCanonicalRange:
    selected = tuple(
        item
        for item in ordered_items
        if item.source_entry_sequence is not None
        and effective_materialization_lineage_floor
        < item.source_entry_sequence
        <= source_through_sequence
    )
    selected_closures = tuple(
        item
        for item in closures
        if item.target_provider_input_through_sequence <= source_through_sequence
        and any(
            candidate.item_kind.value == "ASSISTANT_TOOL_REQUEST"
            and candidate.source_entry_id == item.assistant_entry_id
            and candidate.source_entry_sequence is not None
            and candidate.source_entry_sequence <= source_through_sequence
            for candidate in selected
        )
    )
    selected_late = tuple(
        item
        for item in late_outcomes
        if effective_materialization_lineage_floor
        < item.result_entry_sequence
        <= source_through_sequence
    )
    canonical_bytes = sum(
        provider_input_item_logical_utf8_bytes(item) for item in selected
    )
    values = {
        "scope": scope,
        "effective_materialization_lineage_floor": (
            effective_materialization_lineage_floor
        ),
        "source_through_sequence": source_through_sequence,
        "ordered_items": selected,
        "closures": selected_closures,
        "late_outcomes": selected_late,
        "canonical_utf8_bytes": canonical_bytes,
    }
    return FrozenCompactionCanonicalRange(**values)


def _compaction_canonical_range_identity_digest(
    canonical_range: FrozenCompactionCanonicalRange,
) -> str:
    return context_fingerprint(
        COMPACTION_CANONICAL_RANGE_CONTRACT,
        {
            "scope": canonical_range.scope.fingerprint,
            "floor": canonical_range.effective_materialization_lineage_floor,
            "through": canonical_range.source_through_sequence,
            "items": tuple(
                provider_input_item_fingerprint(item)
                for item in canonical_range.ordered_items
            ),
            "closures": tuple(
                provider_tool_result_closure_leaf(item)
                for item in canonical_range.closures
            ),
            "late_outcomes": tuple(
                late_tool_outcome_observation_leaf(item)
                for item in canonical_range.late_outcomes
            ),
            "canonical_utf8_bytes": canonical_range.canonical_utf8_bytes,
        },
    )


def canonical_compaction_range_digest(
    lineage: CompactionSourceLineageBase,
    canonical_range: FrozenCompactionCanonicalRange,
) -> str:
    if canonical_range.scope != lineage.scope:
        raise ValueError("compaction range belongs to another lineage")
    if (
        canonical_range.effective_materialization_lineage_floor
        != lineage.effective_materialization_lineage_floor
    ):
        raise ValueError("compaction range floor differs from lineage")
    return context_fingerprint(
        COMPACTION_SOURCE_LINEAGE_CONTRACT,
        {
            "scope": lineage.scope.fingerprint,
            "base": lineage.fingerprint,
            "new_source_through_sequence": canonical_range.source_through_sequence,
            "range": _compaction_canonical_range_identity_digest(canonical_range),
        },
    )


@dataclass(frozen=True, slots=True)
class CompleteToolGroup:
    assistant_entry_id: str
    assistant_entry_sequence: int
    ordered_tool_call_ids: tuple[str, ...]
    ordered_result_or_closure_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not self.assistant_entry_id
            or self.assistant_entry_sequence < 1
            or not self.ordered_tool_call_ids
            or len(self.ordered_tool_call_ids) != len(set(self.ordered_tool_call_ids))
            or len(self.ordered_tool_call_ids)
            != len(self.ordered_result_or_closure_fingerprints)
        ):
            raise ValueError("complete tool group is invalid")

    @property
    def fingerprint(self) -> str:
        return context_fingerprint(
            "pulsara.complete-tool-group.v1",
            {
                "assistant_entry_id": self.assistant_entry_id,
                "assistant_entry_sequence": self.assistant_entry_sequence,
                "tool_call_ids": self.ordered_tool_call_ids,
                "results": self.ordered_result_or_closure_fingerprints,
            },
        )


@dataclass(frozen=True, slots=True)
class ProtectedTailSelectionFact:
    source_view_fingerprint: str
    retained_groups: tuple[CompleteToolGroup, ...]
    earliest_retained_assistant_entry_id: str | None
    source_through_sequence: int
    protected_tail_message_start_index: int
    protected_tail_selection_fingerprint: str

    def __post_init__(self) -> None:
        if (
            not self.source_view_fingerprint.startswith("sha256:")
            or self.source_through_sequence < 0
            or self.protected_tail_message_start_index < 0
            or (not self.retained_groups)
            != (self.earliest_retained_assistant_entry_id is None)
        ):
            raise ValueError("protected tail selection is invalid")
        if self.retained_groups and (
            self.earliest_retained_assistant_entry_id
            != self.retained_groups[0].assistant_entry_id
        ):
            raise ValueError("protected tail earliest group differs")
        expected = context_fingerprint(
            "pulsara.protected-tail-selection.v1",
            {
                "source_view": self.source_view_fingerprint,
                "groups": tuple(item.fingerprint for item in self.retained_groups),
                "earliest": self.earliest_retained_assistant_entry_id,
                "source_through_sequence": self.source_through_sequence,
                "start": self.protected_tail_message_start_index,
            },
        )
        if self.protected_tail_selection_fingerprint != expected:
            raise ValueError("protected tail selection fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class ProviderPrefixCutProof:
    source_view_fingerprint: str
    summary_prefix_message_count: int
    summary_prefix_messages_fingerprint: str
    source_through_sequence: int
    protected_tail_selection_fingerprint: str
    proof_fingerprint: str

    def __post_init__(self) -> None:
        if (
            self.summary_prefix_message_count < 1
            or self.source_through_sequence < 0
            or not self.source_view_fingerprint.startswith("sha256:")
            or not self.summary_prefix_messages_fingerprint.startswith("sha256:")
            or not self.protected_tail_selection_fingerprint.startswith("sha256:")
        ):
            raise ValueError("provider prefix cut proof is incomplete")
        expected = context_fingerprint(
            "pulsara.provider-prefix-cut-proof.v1",
            {
                "source_view": self.source_view_fingerprint,
                "message_count": self.summary_prefix_message_count,
                "messages": self.summary_prefix_messages_fingerprint,
                "through": self.source_through_sequence,
                "tail": self.protected_tail_selection_fingerprint,
            },
        )
        if self.proof_fingerprint != expected:
            raise ValueError("provider prefix proof fingerprint mismatch")


@dataclass(frozen=True, slots=True)
class RecentHumanMessageProof:
    entry_id: str
    entry_sequence: int
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FrozenCompactionSummary:
    body: str = field(repr=False)
    body_utf8_bytes: int
    body_digest: str

    def __post_init__(self) -> None:
        encoded = self.body.encode("utf-8")
        if not encoded or len(encoded) != self.body_utf8_bytes:
            raise ValueError("frozen summary byte quote is invalid")
        if self.body_digest != context_fingerprint(
            "pulsara.frozen-compaction-summary.v2-guided-freeform", self.body
        ):
            raise ValueError("frozen summary digest mismatch")


@dataclass(frozen=True, slots=True)
class FrozenCompactionActiveRequest:
    """Runtime-owned location of the exact request driving one active turn."""

    entry_id: str
    entry_sequence: int
    location: CompactionActiveRequestLocation
    text: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.entry_id or self.entry_sequence < 0:
            raise ValueError("compaction active request identity is incomplete")
        snapshot_exact = self.location is CompactionActiveRequestLocation.SNAPSHOT_EXACT
        if snapshot_exact != (self.text is not None):
            raise ValueError("compaction active request location/text union is invalid")
        if self.text is not None:
            self.text.encode("utf-8")
            if not self.text:
                raise ValueError("compaction active request text is empty")

    def canonical_value(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "entry_sequence": self.entry_sequence,
            "location": self.location.value,
            "text": self.text,
        }


@dataclass(frozen=True, slots=True)
class CompactionSnapshotCarrier:
    continuation_mode: CompactionContinuationMode
    handoff_instruction: str = field(repr=False)
    active_request: FrozenCompactionActiveRequest | None = field(repr=False)
    earlier_context_summary: str = field(repr=False)
    recent_user_messages: tuple[str, ...] = field(repr=False)
    body: bytes = field(repr=False)
    content_digest: str

    def __post_init__(self) -> None:
        resume = self.continuation_mode is CompactionContinuationMode.RESUME_ACTIVE_TURN
        if resume != (self.active_request is not None):
            raise ValueError("snapshot continuation/active-request union is invalid")
        self.handoff_instruction.encode("utf-8")
        if not self.handoff_instruction:
            raise ValueError("snapshot handoff instruction is empty")
        expected = canonical_json_bytes(
            {
                "continuation": {
                    "mode": self.continuation_mode.value,
                    "instruction": self.handoff_instruction,
                    "active_request": (
                        None
                        if self.active_request is None
                        else self.active_request.canonical_value()
                    ),
                },
                "earlier_context_summary": self.earlier_context_summary,
                "recent_user_messages": self.recent_user_messages,
            }
        )
        if self.body != expected:
            raise ValueError("snapshot carrier is not canonical JSON")
        if self.content_digest != "sha256:" + sha256(self.body).hexdigest():
            raise ValueError("snapshot carrier digest mismatch")


@dataclass(frozen=True, slots=True)
class ContextSnapshotDraft:
    snapshot_id: str
    session_id: str
    workspace_id: str
    source_through_sequence: int
    source_digest: str
    compiler_contract: str
    prompt_contract: str
    model_contract: str
    content: CanonicalContent = field(repr=False)


@dataclass(frozen=True, slots=True)
class TurnContextBindingRevisionDraft:
    binding_revision_id: str
    session_id: str
    turn_id: str
    revision_ordinal: int
    context_snapshot_id: str
    source_through_sequence: int
    base_kind: Literal["SNAPSHOT"] = "SNAPSHOT"


@dataclass(frozen=True, slots=True)
class ExpectedCompactionPredecessorRevision:
    binding_revision_id: str
    revision_ordinal: int
    base_kind: Literal["FULL_HISTORY", "SNAPSHOT"]
    context_snapshot_id: str | None
    source_through_sequence: int

    def __post_init__(self) -> None:
        if (
            not self.binding_revision_id
            or self.revision_ordinal < 0
            or self.source_through_sequence < 0
            or (self.base_kind == "FULL_HISTORY") != (self.context_snapshot_id is None)
        ):
            raise ValueError("compaction predecessor revision is invalid")

    @property
    def effective_materialization_lineage_floor(self) -> int:
        """Return the semantic range floor without weakening row-exact CAS.

        A FULL_HISTORY revision-zero row stores the turn-local genesis marker
        (initial entry sequence minus one) for exact predecessor confirmation.
        It is not the lower bound of the same-scope history that a first
        compaction may summarize.  Snapshot successors, by contrast, start at
        the immutable source cut of the installed snapshot.
        """

        return 0 if self.base_kind == "FULL_HISTORY" else self.source_through_sequence


@dataclass(frozen=True, slots=True)
class CompactionCanonicalAdoptionFactoryInput:
    scope: CompactionScope
    target_branch: CompactionTargetBranch
    expected_turn_status: Literal["RUNNING", "COMPLETED", "INTERRUPTED"]
    predecessor: ExpectedCompactionPredecessorRevision
    snapshot_id: str
    binding_revision_id: str
    event_id: str
    source_through_sequence: int
    source_digest: str
    snapshot_content: CanonicalContent = field(repr=False)
    compiler_contract: str
    prompt_contract: str
    model_contract: str
    occurred_at: datetime
    actor_id: str


@dataclass(frozen=True, slots=True)
class PreparedCompactionCanonicalAdoption:
    scope: CompactionScope
    target_branch: CompactionTargetBranch
    expected_turn_status: Literal["RUNNING", "COMPLETED", "INTERRUPTED"]
    predecessor: ExpectedCompactionPredecessorRevision
    snapshot: ContextSnapshotDraft
    binding: TurnContextBindingRevisionDraft
    event: CommittedEventDraft

    def __post_init__(self) -> None:
        if (
            self.snapshot.session_id != self.scope.session_id
            or self.snapshot.workspace_id != self.scope.workspace_id
            or self.binding.session_id != self.scope.session_id
            or self.binding.turn_id != self.scope.turn_id
            or self.binding.revision_ordinal != self.predecessor.revision_ordinal + 1
            or self.binding.context_snapshot_id != self.snapshot.snapshot_id
            or self.binding.source_through_sequence
            != self.snapshot.source_through_sequence
            or self.event.event_type is not CommittedEventType.COMPACTION_ADOPTED
            or self.event.subject
            != CommittedEventSubject(
                SubjectSlot.CONTEXT_BINDING_REVISION,
                self.binding.binding_revision_id,
            )
            or dict(self.event.payload)
            != {"revision_ordinal": self.binding.revision_ordinal}
        ):
            raise ValueError("compaction adoption row drafts do not exact-join")
        if self.target_branch is CompactionTargetBranch.ACTIVE_INSTALLATION:
            if self.expected_turn_status != "RUNNING":
                raise ValueError("active compaction requires a running turn")
        elif self.expected_turn_status == "RUNNING":
            raise ValueError("idle compaction requires a terminal turn")


@dataclass(frozen=True, slots=True)
class CompactionCanonicalWritePreconditions:
    scope: CompactionScope
    expected_turn_status: Literal["RUNNING", "COMPLETED", "INTERRUPTED"]
    expected_safe_head: int
    provider_safe: bool

    def __post_init__(self) -> None:
        if self.expected_safe_head < 0 or not self.provider_safe:
            raise ValueError("compaction write preconditions are not provider-safe")


def build_prepared_compaction_canonical_adoption(
    value: CompactionCanonicalAdoptionFactoryInput,
) -> PreparedCompactionCanonicalAdoption:
    if (
        value.source_through_sequence
        < value.predecessor.effective_materialization_lineage_floor
        or not value.source_digest.startswith("sha256:")
        or not value.actor_id
    ):
        raise ValueError("compaction adoption factory input is invalid")
    snapshot = ContextSnapshotDraft(
        snapshot_id=value.snapshot_id,
        session_id=value.scope.session_id,
        workspace_id=value.scope.workspace_id,
        source_through_sequence=value.source_through_sequence,
        source_digest=value.source_digest,
        compiler_contract=value.compiler_contract,
        prompt_contract=value.prompt_contract,
        model_contract=value.model_contract,
        content=value.snapshot_content,
    )
    binding = TurnContextBindingRevisionDraft(
        binding_revision_id=value.binding_revision_id,
        session_id=value.scope.session_id,
        turn_id=value.scope.turn_id,
        revision_ordinal=value.predecessor.revision_ordinal + 1,
        context_snapshot_id=value.snapshot_id,
        source_through_sequence=value.source_through_sequence,
    )
    event = CommittedEventDraft(
        event_id=value.event_id,
        event_type=CommittedEventType.COMPACTION_ADOPTED,
        subject=CommittedEventSubject(
            SubjectSlot.CONTEXT_BINDING_REVISION,
            value.binding_revision_id,
        ),
        actor_kind="runtime",
        actor_id=value.actor_id,
        sensitivity_class="PUBLIC",
        projection_profile="DEFAULT",
        occurred_at=value.occurred_at,
        payload={"revision_ordinal": binding.revision_ordinal},
    )
    return PreparedCompactionCanonicalAdoption(
        scope=value.scope,
        target_branch=value.target_branch,
        expected_turn_status=value.expected_turn_status,
        predecessor=value.predecessor,
        snapshot=snapshot,
        binding=binding,
        event=event,
    )


@dataclass(frozen=True, slots=True)
class CompactionAdoptionConfirmation:
    kind: CompactionConfirmationKind
    revision_ordinal: int | None = None

    def __post_init__(self) -> None:
        if (self.kind is CompactionConfirmationKind.FULL) != (
            self.revision_ordinal is not None
        ):
            raise ValueError("compaction confirmation union is invalid")


@dataclass(frozen=True, slots=True)
class CompactionOutcome:
    disposition: CompactionDisposition
    target_turn_id: str
    snapshot_id: str | None
    revision_ordinal: int | None
    public_code: str


__all__ = [
    "COMPACTION_CANONICAL_RANGE_CONTRACT",
    "COMPACTION_MODEL_CONTRACT",
    "COMPACTION_SNAPSHOT_COMPILER_CONTRACT",
    "COMPACTION_SOURCE_LINEAGE_CONTRACT",
    "COMPACTION_SUMMARY_PROMPT_CONTRACT",
    "CONTEXT_SNAPSHOT_CODEC",
    "CONTEXT_SNAPSHOT_MEDIA_TYPE",
    "ColdRebuildCompactionProjection",
    "CompactionAdoptionConfirmation",
    "CompactionAttemptPhase",
    "CompactionActiveRequestLocation",
    "CompactionCanonicalAdoptionFactoryInput",
    "CompactionCanonicalWritePreconditions",
    "CompactionConfirmationKind",
    "CompactionContinuationMode",
    "CompactionDisposition",
    "CompactionLineageBaseKind",
    "CompactionOutcome",
    "CompactionPhysicalWorkingSetReport",
    "CompactionScope",
    "CompactionSnapshotCarrier",
    "CompactionSourceCompatibility",
    "CompactionSourceLineageBase",
    "CompactionTargetBranch",
    "CompactionTrigger",
    "CompatibleAppendCompactionProjection",
    "CompleteToolGroup",
    "ContextSnapshotDraft",
    "ExpectedCompactionPredecessorRevision",
    "FrozenCompactionCanonicalRange",
    "FrozenCompactionCanonicalRead",
    "FrozenCompactionActiveRequest",
    "FrozenCompactionHeadroomPreflight",
    "FrozenCompactionProviderProjection",
    "FrozenCompactionSourceView",
    "PreparedCompactionCanonicalAdoption",
    "PreparedManualCompactionCommand",
    "ProtectedTailSelectionFact",
    "ProviderPrefixCutProof",
    "RecentHumanMessageProof",
    "ResolvedCompactionHeadroomBounds",
    "ResolvedCompactionPolicy",
    "TurnContextBindingRevisionDraft",
    "FrozenCompactionSummary",
    "build_prepared_compaction_canonical_adoption",
    "build_prepared_manual_compaction_command",
    "canonical_compaction_range_digest",
    "compaction_summary_message_prefix_fingerprint",
    "freeze_compaction_canonical_range",
    "freeze_compaction_canonical_read",
    "freeze_compaction_headroom_preflight",
    "manual_compaction_command_semantic_digest",
    "manual_compaction_stable_suffix",
    "resolved_compaction_headroom_bounds",
]
