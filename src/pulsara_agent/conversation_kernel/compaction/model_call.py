"""One-shot primary-model execution for a Round 5B semantic handoff."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import Lock

from pulsara_agent.capability.contracts import FrozenNativeToolProjectionSet
from pulsara_agent.conversation_kernel.assembler import (
    CompletedDataBlock,
    CompletedTextBlock,
    CompletedToolCallBlock,
    ProviderStreamAssembler,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionActiveRequestLocation,
    FrozenCompactionActiveRequest,
    FrozenCompactionCanonicalRead,
    FrozenCompactionSourceView,
    FrozenRetainedHistoricalRequest,
    ProviderPrefixCutProof,
    canonical_compaction_range_digest,
    compaction_summary_message_prefix_fingerprint,
)
from pulsara_agent.conversation_kernel.compaction.planner import (
    DestinationDialogueProjection,
)
from pulsara_agent.conversation_kernel.compaction.prompt import (
    summary_request_fingerprint,
)
from pulsara_agent.conversation_kernel.provider_dispatch import (
    PreparedWireMeasurementDecision,
)
from pulsara_agent.conversation_kernel.direct_model import (
    provider_wire_profile_fingerprint,
)
from pulsara_agent.llm.input import (
    LLMMessage,
    LLMToolCall,
    MessageRole,
    ToolSpec,
    llm_content_identity_value,
)
from pulsara_agent.model_input.lowering import lower_retained_request_content
from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.llm.request import (
    MAXIMUM_PROVIDER_WIRE_INPUT_BYTES,
    FrozenProviderWireInputPlan,
    LLMContext,
    provider_wire_input_plan_identity_fingerprint,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.provider import RouteWireProfile
from pulsara_agent.llm.validation import validate_model_context_shape_for_call
from pulsara_agent.model_input.contracts import (
    CanonicalModelInputIdentity,
    FrozenCompiledMessagePlacement,
    FrozenModelInputSemanticProjection,
    FrozenToolSpec,
    ModelInputCompileBinding,
    compiled_message_placements_fingerprint,
)
from pulsara_agent.model_input.continuity import decode_runtime_observation
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead,
)
from pulsara_agent.ports.provider_stream import (
    ProviderModelExecutionFailed,
    ProviderModelOutputIncomplete,
    ProviderNormalizedTerminalKind,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    context_fingerprint,
    thaw_json,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.primitives.model_call import (
    ResolvedModelTargetFact,
    TokenEstimatorFact,
)


class _SummaryCallState(StrEnum):
    PREPARED = "PREPARED"
    OPENING = "OPENING"
    CLOSED = "CLOSED"
    DISCARDED = "DISCARDED"


@dataclass(frozen=True, slots=True)
class RawCompactionSummaryResponse:
    text: str
    tool_calls: tuple[LLMToolCall, ...]


@dataclass(frozen=True, slots=True)
class PreparedCompactionSummarySemanticInput:
    """Ephemeral structural input; semantic over-budget is intentionally legal."""

    context_id: str
    canonical_input_identity: CanonicalModelInputIdentity = field(repr=False)
    system_prompt: str = field(repr=False)
    messages: tuple[LLMMessage, ...] = field(repr=False)
    message_placements: tuple[FrozenCompiledMessagePlacement, ...] = field(repr=False)
    tools: tuple[FrozenToolSpec, ...] = field(repr=False)
    final_estimate: TokenEstimate
    compile_binding_fingerprint: str
    compiled_semantic_fingerprint: str

    def __post_init__(self) -> None:
        if not self.context_id or len(self.message_placements) != len(self.messages):
            raise ValueError("summary semantic input shape is invalid")
        if tuple(item.message_ordinal for item in self.message_placements) != tuple(
            range(len(self.messages))
        ):
            raise ValueError("summary semantic placement order is invalid")
        if any(
            placement.role is not message.role
            for placement, message in zip(
                self.message_placements, self.messages, strict=True
            )
        ):
            raise ValueError("summary semantic placement role drifted")
        if len(self.final_estimate.message_tokens_by_index) != len(self.messages):
            raise ValueError("summary semantic token breakdown is invalid")
        for value in (
            self.compile_binding_fingerprint,
            self.compiled_semantic_fingerprint,
        ):
            if not value.startswith("sha256:"):
                raise ValueError("summary semantic fingerprint is invalid")


class _CompactionSummarySourceSeal:
    pass


_COMPACTION_SUMMARY_SOURCE_SEAL = _CompactionSummarySourceSeal()


@dataclass(frozen=True, slots=True)
class InstalledPrefixSummarySourceProof:
    source_view: FrozenCompactionSourceView = field(repr=False)
    source_projection: FrozenModelInputSemanticProjection = field(repr=False)
    prefix_proof: ProviderPrefixCutProof
    _seal: _CompactionSummarySourceSeal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _COMPACTION_SUMMARY_SOURCE_SEAL:
            raise ValueError("installed-prefix summary proof is not sealed")


@dataclass(frozen=True, slots=True)
class DestinationProjectionSummarySourceProof:
    canonical_source: FrozenCompactionCanonicalRead = field(repr=False)
    source_projection: FrozenModelInputSemanticProjection = field(repr=False)
    target_fact: ResolvedModelTargetFact
    route_wire_profile: RouteWireProfile = field(repr=False)
    estimator_fact: TokenEstimatorFact
    effective_input_budget_tokens: int
    resolved_trigger_tokens: int
    source_through_sequence: int
    cumulative_source_digest: str
    projection: DestinationDialogueProjection = field(repr=False)
    active_request: FrozenCompactionActiveRequest | None = field(repr=False)
    projection_message_ordinal: int
    active_request_message_ordinal: int | None
    _seal: _CompactionSummarySourceSeal = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        source = self.canonical_source
        if (
            self._seal is not _COMPACTION_SUMMARY_SOURCE_SEAL
            or self.effective_input_budget_tokens < 1
            or not 0 < self.resolved_trigger_tokens < self.effective_input_budget_tokens
            or self.source_through_sequence
            != source.safe_head_range.source_through_sequence
            or self.cumulative_source_digest
            != canonical_compaction_range_digest(
                source.lineage_base, source.safe_head_range
            )
            or self.projection_message_ordinal < 0
            or self.source_projection.canonical_input_identity
            != source.dispatch_read.compile_snapshot.canonical_input.identity
            or not self.source_projection.compile_binding_fingerprint
        ):
            raise ValueError("destination-projection summary proof is invalid")
        active = self.active_request
        if active is None:
            if (
                source.turn_status not in {"COMPLETED", "INTERRUPTED"}
                or self.active_request_message_ordinal is not None
            ):
                raise ValueError("destination-projection summary proof is invalid")
        elif (
            source.turn_status != "RUNNING"
            or active.location is not CompactionActiveRequestLocation.SNAPSHOT_EXACT
            or active.content is None
            or active.entry_id
            != source.dispatch_read.compile_snapshot.canonical_input.identity.initial_entry_id
            or self.active_request_message_ordinal
            != self.projection_message_ordinal + 1
        ):
            raise ValueError("destination-projection summary proof is invalid")


CompactionSummarySourceProof = (
    InstalledPrefixSummarySourceProof | DestinationProjectionSummarySourceProof
)


@dataclass(frozen=True, slots=True)
class PreparedCompactionSummarySemantic:
    call: ResolvedModelCall = field(repr=False)
    source_proof: CompactionSummarySourceProof = field(repr=False)
    compile_binding: ModelInputCompileBinding = field(repr=False)
    native_projection_set: FrozenNativeToolProjectionSet = field(repr=False)
    summary_request: str = field(repr=False)
    semantic_input: PreparedCompactionSummarySemanticInput = field(repr=False)
    semantic_fingerprint: str

    def __post_init__(self) -> None:
        compiled = self.semantic_input
        binding = self.compile_binding
        if self.call.fact.purpose is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY:
            raise ValueError("summary semantic carrier has the wrong purpose")
        if (
            self.call.target.fact != binding.target_fact
            or compiled.compile_binding_fingerprint != binding.binding_fingerprint
            or compiled.tools != binding.tool_surface.tool_specs
            or not self.summary_request
            or not self.semantic_fingerprint.startswith("sha256:")
        ):
            raise ValueError("summary semantic carrier does not exact-join")
        proof = self.source_proof
        if isinstance(proof, InstalledPrefixSummarySourceProof):
            source = proof.source_projection
            count = proof.prefix_proof.summary_prefix_message_count
            expected_request_placement = _synthetic_summary_placement(
                message_ordinal=count,
                proof=proof.prefix_proof,
                summary_request=self.summary_request,
            )
            if (
                source.canonical_input_identity
                != proof.source_view.canonical_dispatch_read.compile_snapshot.canonical_input.identity
                or source.system_prompt
                != proof.source_view.materialized_system_prompt()
                or source.messages != proof.source_view.materialized_messages()
                or source.tools != binding.tool_surface.tool_specs
                or source.compile_binding_fingerprint != binding.binding_fingerprint
                or proof.prefix_proof.source_view_fingerprint
                != proof.source_view.source_view_fingerprint
                or not 0 < count <= len(source.messages)
                or compaction_summary_message_prefix_fingerprint(
                    source.messages[:count]
                )
                != proof.prefix_proof.summary_prefix_messages_fingerprint
                or compiled.canonical_input_identity != source.canonical_input_identity
                or compiled.system_prompt != source.system_prompt
                or len(compiled.messages) < count + 1
                or compiled.messages[:count] != source.messages[:count]
                or compiled.message_placements[:count]
                != source.message_placements[:count]
                or compiled.messages[count] != LLMMessage.user(self.summary_request)
                or compiled.message_placements[count] != expected_request_placement
            ):
                raise ValueError("installed-prefix summary proof does not exact-join")
        else:
            identity = proof.canonical_source.dispatch_read.compile_snapshot.canonical_input.identity
            projection_ordinal = proof.projection_message_ordinal
            active_ordinal = proof.active_request_message_ordinal
            summary_ordinal = (
                projection_ordinal + 1
                if active_ordinal is None
                else active_ordinal + 1
            )
            current_messages, current_placements, active_placement = (
                _destination_source_messages_and_active_placement(
                    source_projection=proof.source_projection,
                    active_request=proof.active_request,
                )
            )
            expected_summary_placement = _ephemeral_summary_placement(
                message_ordinal=summary_ordinal,
                role=MessageRole.USER,
                domain="destination-request",
                identity={
                    "prompt": summary_request_fingerprint(self.summary_request)
                },
            )
            if (
                self.call.target.fact != proof.target_fact
                or self.call.target.model_profile.route_wire_profile
                != proof.route_wire_profile
                or binding.estimator.fact != proof.estimator_fact
                or binding.effective_input_budget_tokens
                != proof.effective_input_budget_tokens
                or proof.source_projection.system_prompt != compiled.system_prompt
                or proof.source_projection.tools != compiled.tools
                or proof.source_projection.compile_binding_fingerprint
                != binding.binding_fingerprint
                or compiled.canonical_input_identity != identity
                or compiled.messages[:projection_ordinal] != current_messages
                or compiled.message_placements[:projection_ordinal]
                != current_placements
                or projection_ordinal >= len(compiled.messages)
                or summary_ordinal >= len(compiled.messages)
                or compiled.messages[projection_ordinal]
                != LLMMessage(
                    role=MessageRole.USER,
                    content=proof.projection.content,
                )
                or compiled.messages[summary_ordinal]
                != LLMMessage.user(self.summary_request)
                or compiled.message_placements[projection_ordinal].origin_entry_id
                is not None
                or compiled.message_placements[summary_ordinal]
                != expected_summary_placement
            ):
                raise ValueError(
                    "destination-projection summary proof does not exact-join"
                )
            if proof.active_request is None:
                if active_ordinal is not None or active_placement is not None:
                    raise ValueError(
                        "destination-projection summary proof does not exact-join"
                    )
            elif (
                active_ordinal is None
                or active_ordinal >= len(compiled.messages)
                or compiled.messages[active_ordinal]
                != _active_request_message(proof.active_request)
                or compiled.message_placements[active_ordinal].origin_entry_id
                != proof.active_request.entry_id
                or active_placement is None
                or compiled.message_placements[active_ordinal]
                != replace(active_placement, message_ordinal=active_ordinal)
            ):
                raise ValueError(
                    "destination-projection summary proof does not exact-join"
                )
        estimate = binding.estimator.estimate_frozen_input(
            system_prompt=compiled.system_prompt,
            messages=compiled.messages,
            tools=compiled.tools,
        )
        if estimate != compiled.final_estimate:
            raise ValueError("summary semantic estimate changed")

    @property
    def canonical_read(self) -> FrozenCanonicalProviderDispatchRead:
        proof = self.source_proof
        if isinstance(proof, InstalledPrefixSummarySourceProof):
            return proof.source_view.canonical_dispatch_read
        return proof.canonical_source.dispatch_read

    @property
    def tool_choice(self) -> str:
        return "auto"


class PreparedCompactionSummaryCall:
    """Sealed one-shot transport authority with no tool/runtime authority."""

    def __init__(
        self,
        *,
        semantic: PreparedCompactionSummarySemantic,
        wire_input_plan: FrozenProviderWireInputPlan,
    ) -> None:
        call = semantic.call
        compiled = semantic.semantic_input
        if (
            call.fact.purpose is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
            or wire_input_plan.compiled_semantic_fingerprint
            != compiled.compiled_semantic_fingerprint
            or wire_input_plan.message_placements_fingerprint
            != compiled_message_placements_fingerprint(compiled.message_placements)
            or wire_input_plan.resolved_target_semantic_fingerprint
            != call.target.fact.target_fingerprint
            or wire_input_plan.materialization.tool_items
            != tuple(
                item.wire_tool for item in semantic.native_projection_set.projections
            )
        ):
            raise ValueError("prepared summary call does not exact-join")
        tools = tuple(_thaw_tool(item) for item in compiled.tools)
        context = LLMContext(
            messages=compiled.messages,
            context_id=compiled.context_id,
            resolved_model_call_id=call.resolved_model_call_id,
            target_fingerprint=call.target.fact.target_fingerprint,
            model_call_index=compiled.canonical_input_identity.provider_input_through_sequence
            + 1,
            tools=tools,
            system_prompt=compiled.system_prompt,
            compiler_estimated_input_tokens=(
                compiled.final_estimate.total_input_tokens
            ),
            provider_wire_input_plan=wire_input_plan,
            tool_choice="auto",
        )
        validate_model_context_shape_for_call(call=call, context=context)
        if (
            semantic.compile_binding.estimator.estimate_frozen_input(
                system_prompt=compiled.system_prompt,
                messages=compiled.messages,
                tools=compiled.tools,
            )
            != compiled.final_estimate
        ):
            raise ValueError(
                "summary pre-send estimate differs from its semantic input"
            )
        self._semantic = semantic
        self._wire_input_plan = wire_input_plan
        self._context = context
        self._state = _SummaryCallState.PREPARED
        self._lock = Lock()
        self.request_fingerprint = context_fingerprint(
            "pulsara.prepared-compaction-summary-call.v1",
            {
                "purpose": ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY.value,
                "tool_choice": "auto",
                "call": call.fact,
                "source": _summary_source_identity_value(semantic.source_proof),
                "semantic": semantic.semantic_fingerprint,
                "wire": provider_wire_input_plan_identity_fingerprint(wire_input_plan),
                "estimate": _estimate_value(compiled.final_estimate),
            },
        )

    @property
    def semantic(self) -> PreparedCompactionSummarySemantic:
        return self._semantic

    @property
    def wire_input_plan(self) -> FrozenProviderWireInputPlan:
        return self._wire_input_plan

    async def open_once(self) -> RawCompactionSummaryResponse:
        with self._lock:
            if self._state is not _SummaryCallState.PREPARED:
                raise RuntimeError("compaction summary call is not openable")
            self._state = _SummaryCallState.OPENING
        call = self._semantic.call
        execution = call.target.transport.open_stream(
            call=call,
            context=self._context,
        )
        assembler = ProviderStreamAssembler(
            session_id=(
                self._semantic.canonical_read.compile_snapshot.canonical_input.identity.session_id
            ),
            turn_id=(
                self._semantic.canonical_read.compile_snapshot.canonical_input.identity.turn_id
            ),
            live_bus=_NullLiveBus(),  # type: ignore[arg-type]
            proposed_entry_id=f"compaction-summary:{self.request_fingerprint[7:39]}",
            conversation_scope_kind=(
                self._semantic.canonical_read.compile_snapshot.canonical_input.identity.conversation_scope_kind.value
            ),
            scope_subagent_task_id=(
                self._semantic.canonical_read.compile_snapshot.canonical_input.identity.scope_subagent_task_id
            ),
        )
        terminal: ProviderStreamTerminal | None = None
        response: RawCompactionSummaryResponse | None = None
        try:
            while True:
                item = await execution.read_next()
                if item is None:
                    break
                if isinstance(item, ProviderStreamTerminal):
                    terminal = item
                    break
                assembler.apply(item)
            if terminal is None:
                raise RuntimeError("summary stream lacks an explicit terminal")
            if (
                terminal.terminal_kind
                is ProviderNormalizedTerminalKind.OUTPUT_INCOMPLETE
            ):
                assert terminal.incomplete_reason is not None
                raise ProviderModelOutputIncomplete(terminal.incomplete_reason)
            if terminal.terminal_kind is ProviderNormalizedTerminalKind.PROVIDER_ERROR:
                assert terminal.error is not None
                raise ProviderModelExecutionFailed(terminal.error)
            completed = assembler.complete()
            if any(isinstance(item, CompletedDataBlock) for item in completed.blocks):
                raise RuntimeError("summary response contains non-text data")
            tool_calls = tuple(
                LLMToolCall(
                    id=item.tool_call_id,
                    name=item.tool_name,
                    arguments=canonical_json_bytes(thaw_json(item.arguments)).decode(
                        "utf-8"
                    ),
                )
                for item in completed.blocks
                if isinstance(item, CompletedToolCallBlock)
            )
            text = "".join(
                item.text
                for item in completed.blocks
                if isinstance(item, CompletedTextBlock)
            )
            response = RawCompactionSummaryResponse(text=text, tool_calls=tool_calls)
        except BaseException:
            with self._lock:
                self._state = _SummaryCallState.DISCARDED
            raise
        finally:
            drain = asyncio.create_task(
                _drain_summary_execution(execution),
                name=f"kernel-compaction-summary-drain:{self.request_fingerprint[7:23]}",
            )
            try:
                completion = await asyncio.shield(drain)
            except asyncio.CancelledError:
                completion = await drain
                raise
            if completion.status is not ProviderPhysicalCompletionStatus.COMPLETED:
                with self._lock:
                    self._state = _SummaryCallState.DISCARDED
                raise RuntimeError("summary provider physical operation did not exit")
        assert response is not None
        with self._lock:
            self._state = _SummaryCallState.CLOSED
        return response

    def discard(self) -> None:
        with self._lock:
            if self._state is _SummaryCallState.PREPARED:
                self._state = _SummaryCallState.DISCARDED
            elif self._state is not _SummaryCallState.DISCARDED:
                raise RuntimeError("started summary call cannot be discarded")


def prepare_compaction_summary_semantic(
    *,
    call: ResolvedModelCall,
    source_view: FrozenCompactionSourceView,
    source_projection: FrozenModelInputSemanticProjection,
    prefix_proof: ProviderPrefixCutProof,
    native_projection_set: FrozenNativeToolProjectionSet,
    summary_request: str,
) -> PreparedCompactionSummarySemantic:
    if (
        call.fact.purpose is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
        or call.target.fact != source_view.normal_compile_binding.target_fact
        or source_projection.system_prompt != source_view.materialized_system_prompt()
        or source_projection.messages != source_view.materialized_messages()
        or source_projection.tools
        != source_view.normal_compile_binding.tool_surface.tool_specs
        or source_projection.compile_binding_fingerprint
        != source_view.normal_compile_binding.binding_fingerprint
        or prefix_proof.source_view_fingerprint != source_view.source_view_fingerprint
    ):
        raise ValueError("summary semantic inputs do not exact-join")
    count = prefix_proof.summary_prefix_message_count
    prefix = source_projection.messages[:count]
    if (
        compaction_summary_message_prefix_fingerprint(prefix)
        != prefix_proof.summary_prefix_messages_fingerprint
    ):
        raise ValueError("summary prefix proof changed")
    if not summary_request:
        raise ValueError("summary request is empty")
    messages = prefix + (LLMMessage.user(summary_request),)
    placements = source_projection.message_placements[:count] + (
        _synthetic_summary_placement(
            message_ordinal=count,
            proof=prefix_proof,
            summary_request=summary_request,
        ),
    )
    binding = source_view.normal_compile_binding
    estimate = binding.estimator.estimate_frozen_input(
        system_prompt=source_projection.system_prompt,
        messages=messages,
        tools=source_projection.tools,
    )
    context_id = context_fingerprint(
        "pulsara.compaction-summary-context-id.v1",
        {
            "call": call.resolved_model_call_id,
            "source": source_view.source_view_fingerprint,
            "prefix": prefix_proof.proof_fingerprint,
        },
    )
    values = {
        "context_id": context_id,
        "canonical_input_identity": source_projection.canonical_input_identity,
        "system_prompt": source_projection.system_prompt,
        "messages": messages,
        "message_placements": placements,
        "tools": source_projection.tools,
        "final_estimate": estimate,
        "compile_binding_fingerprint": binding.binding_fingerprint,
    }
    compiled = PreparedCompactionSummarySemanticInput(
        **values,
        compiled_semantic_fingerprint=context_fingerprint(
            "pulsara.compaction-summary-semantic-input.v1",
            {
                "context": context_id,
                "canonical": source_projection.canonical_input_identity.identity_fingerprint,
                "messages": compaction_summary_message_prefix_fingerprint(messages),
                "placements": compiled_message_placements_fingerprint(placements),
                "estimate": _estimate_value(estimate),
                "binding": binding.binding_fingerprint,
            },
        ),
    )
    semantic_fingerprint = context_fingerprint(
        "pulsara.prepared-compaction-summary-semantic.v1",
        {
            "call": call.fact,
            "source": source_view.source_view_fingerprint,
            "prefix": prefix_proof.proof_fingerprint,
            "native": native_projection_set.projection_set_fingerprint,
            "prompt": summary_request_fingerprint(summary_request),
            "compiled": compiled.compiled_semantic_fingerprint,
        },
    )
    return PreparedCompactionSummarySemantic(
        call=call,
        source_proof=InstalledPrefixSummarySourceProof(
            source_view=source_view,
            source_projection=source_projection,
            prefix_proof=prefix_proof,
            _seal=_COMPACTION_SUMMARY_SOURCE_SEAL,
        ),
        compile_binding=binding,
        native_projection_set=native_projection_set,
        summary_request=summary_request,
        semantic_input=compiled,
        semantic_fingerprint=semantic_fingerprint,
    )


def _destination_source_messages_and_active_placement(
    *,
    source_projection: FrozenModelInputSemanticProjection,
    active_request: FrozenCompactionActiveRequest | None,
) -> tuple[
    tuple[LLMMessage, ...],
    tuple[FrozenCompiledMessagePlacement, ...],
    FrozenCompiledMessagePlacement | None,
]:
    if active_request is None:
        active_matches: tuple[
            tuple[LLMMessage, FrozenCompiledMessagePlacement], ...
        ] = ()
    else:
        active_matches = tuple(
            (message, placement)
            for message, placement in zip(
                source_projection.messages,
                source_projection.message_placements,
                strict=True,
            )
            if placement.origin_entry_id == active_request.entry_id
        )
    if active_request is not None and (
        len(active_matches) != 1
        or active_request.content is None
        or active_matches[0][0] != _active_request_message(active_request)
    ):
        raise ValueError("destination active request has no exact source placement")
    current: list[tuple[LLMMessage, FrozenCompiledMessagePlacement]] = []
    for message, placement in zip(
        source_projection.messages,
        source_projection.message_placements,
        strict=True,
    ):
        if placement.origin_entry_id is not None:
            continue
        try:
            decode_runtime_observation(message)
        except ValueError:
            continue
        current.append((message, placement))
    current_messages = tuple(item[0] for item in current)
    current_placements = tuple(
        replace(item[1], message_ordinal=index) for index, item in enumerate(current)
    )
    return (
        current_messages,
        current_placements,
        None if active_request is None else active_matches[0][1],
    )


def _active_request_message(
    active_request: FrozenCompactionActiveRequest,
) -> LLMMessage:
    if active_request.content is None:
        raise ValueError("active request content is absent")
    request = FrozenRetainedHistoricalRequest(
        item_kind=active_request.item_kind,
        input_origin=active_request.input_origin,
        content=active_request.content,
    )
    return LLMMessage(
        role=MessageRole.USER,
        content=lower_retained_request_content(request),
    )


def prepare_destination_projection_summary_semantic(
    *,
    call: ResolvedModelCall,
    canonical_source: FrozenCompactionCanonicalRead,
    compile_binding: ModelInputCompileBinding,
    native_projection_set: FrozenNativeToolProjectionSet,
    source_projection: FrozenModelInputSemanticProjection,
    projection: DestinationDialogueProjection,
    active_request: FrozenCompactionActiveRequest | None,
    summary_request: str,
    resolved_trigger_tokens: int,
) -> PreparedCompactionSummarySemantic:
    """Build one sealed B summary source without impersonating installed replay."""

    identity = canonical_source.dispatch_read.compile_snapshot.canonical_input.identity
    current_source_messages, current_source_placements, active_request_placement = (
        _destination_source_messages_and_active_placement(
            source_projection=source_projection,
            active_request=active_request,
        )
    )
    if (
        call.fact.purpose is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
        or call.target.fact != compile_binding.target_fact
        or source_projection.canonical_input_identity != identity
        or source_projection.tools != compile_binding.tool_surface.tool_specs
        or source_projection.compile_binding_fingerprint
        != compile_binding.binding_fingerprint
    ):
        raise ValueError("destination projection summary inputs do not exact-join")
    if active_request is None:
        if (
            canonical_source.turn_status not in {"COMPLETED", "INTERRUPTED"}
            or active_request_placement is not None
        ):
            raise ValueError("destination projection summary inputs do not exact-join")
    elif (
        canonical_source.turn_status != "RUNNING"
        or active_request.location
        is not CompactionActiveRequestLocation.SNAPSHOT_EXACT
        or active_request.content is None
        or active_request.entry_id != identity.initial_entry_id
        or active_request_placement is None
        or active_request_placement.origin_entry_id != active_request.entry_id
    ):
        raise ValueError("destination projection summary inputs do not exact-join")
    normalized_source_placements = tuple(
        replace(item, message_ordinal=index)
        for index, item in enumerate(current_source_placements)
    )
    projection_ordinal = len(current_source_messages)
    projection_message = LLMMessage(
        role=MessageRole.USER,
        content=projection.content,
    )
    message_values = [*current_source_messages, projection_message]
    placement_values = [
        *normalized_source_placements,
        _ephemeral_summary_placement(
            message_ordinal=projection_ordinal,
            role=MessageRole.USER,
            domain="destination-projection",
            identity={
                "source": canonical_source.dispatch_read.composite_fingerprint,
                "content": llm_content_identity_value(projection.content),
            },
        ),
    ]
    active_ordinal: int | None = None
    if active_request is not None:
        assert active_request_placement is not None
        active_ordinal = len(message_values)
        message_values.append(_active_request_message(active_request))
        placement_values.append(
            replace(active_request_placement, message_ordinal=active_ordinal)
        )
    summary_ordinal = len(message_values)
    message_values.append(LLMMessage.user(summary_request))
    placement_values.append(
        _ephemeral_summary_placement(
            message_ordinal=summary_ordinal,
            role=MessageRole.USER,
            domain="destination-request",
            identity={"prompt": summary_request_fingerprint(summary_request)},
        )
    )
    messages = tuple(message_values)
    placements = tuple(placement_values)
    estimate = compile_binding.estimator.estimate_frozen_input(
        system_prompt=source_projection.system_prompt,
        messages=messages,
        tools=source_projection.tools,
    )
    source_digest = canonical_compaction_range_digest(
        canonical_source.lineage_base,
        canonical_source.safe_head_range,
    )
    proof = DestinationProjectionSummarySourceProof(
        canonical_source=canonical_source,
        source_projection=source_projection,
        target_fact=call.target.fact,
        route_wire_profile=call.target.model_profile.route_wire_profile,
        estimator_fact=compile_binding.estimator.fact,
        effective_input_budget_tokens=(compile_binding.effective_input_budget_tokens),
        resolved_trigger_tokens=resolved_trigger_tokens,
        source_through_sequence=(
            canonical_source.safe_head_range.source_through_sequence
        ),
        cumulative_source_digest=source_digest,
        projection=projection,
        active_request=active_request,
        projection_message_ordinal=projection_ordinal,
        active_request_message_ordinal=active_ordinal,
        _seal=_COMPACTION_SUMMARY_SOURCE_SEAL,
    )
    context_id = context_fingerprint(
        "pulsara.destination-compaction-summary-context-id.v1",
        {
            "call": call.resolved_model_call_id,
            "source": canonical_source.dispatch_read.composite_fingerprint,
            "source_digest": source_digest,
            "messages": compaction_summary_message_prefix_fingerprint(messages),
        },
    )
    values = {
        "context_id": context_id,
        "canonical_input_identity": identity,
        "system_prompt": source_projection.system_prompt,
        "messages": messages,
        "message_placements": placements,
        "tools": source_projection.tools,
        "final_estimate": estimate,
        "compile_binding_fingerprint": compile_binding.binding_fingerprint,
    }
    compiled = PreparedCompactionSummarySemanticInput(
        **values,
        compiled_semantic_fingerprint=context_fingerprint(
            "pulsara.destination-compaction-summary-semantic-input.v1",
            {
                "context": context_id,
                "canonical": identity.identity_fingerprint,
                "messages": compaction_summary_message_prefix_fingerprint(messages),
                "placements": compiled_message_placements_fingerprint(placements),
                "estimate": _estimate_value(estimate),
                "binding": compile_binding.binding_fingerprint,
            },
        ),
    )
    return PreparedCompactionSummarySemantic(
        call=call,
        source_proof=proof,
        compile_binding=compile_binding,
        native_projection_set=native_projection_set,
        summary_request=summary_request,
        semantic_input=compiled,
        semantic_fingerprint=context_fingerprint(
            "pulsara.prepared-destination-compaction-summary-semantic.v1",
            {
                "call": call.fact,
                "source": _summary_source_identity_value(proof),
                "native": native_projection_set.projection_set_fingerprint,
                "prompt": summary_request_fingerprint(summary_request),
                "compiled": compiled.compiled_semantic_fingerprint,
            },
        ),
    )


def promote_compaction_summary_call(
    semantic: PreparedCompactionSummarySemantic,
    *,
    decision: PreparedWireMeasurementDecision,
    predecessor_summary_wire_plan: FrozenProviderWireInputPlan | None = None,
) -> PreparedCompactionSummaryCall:
    if decision.candidate is not semantic:
        raise ValueError("summary wire decision belongs to another candidate")
    plan = decision.wire_input_plan
    if plan is None:
        raise ValueError("summary wire decision is not executable")
    if (
        plan.quote is not decision.quote
        or plan.wire_api
        != semantic.call.target.model_profile.route_wire_profile.wire_api
        or plan.route_wire_profile_fingerprint
        != provider_wire_profile_fingerprint(semantic.call)
        or decision.quote.estimator_fingerprint
        != semantic.compile_binding.estimator_fingerprint
        or decision.quote.effective_input_budget_tokens
        != semantic.compile_binding.effective_input_budget_tokens
        or decision.quote.final_wire_estimated_input_tokens
        > decision.quote.effective_input_budget_tokens
        or decision.quote.final_wire_utf8_bytes > MAXIMUM_PROVIDER_WIRE_INPUT_BYTES
    ):
        raise ValueError("summary wire decision failed hard admission")
    if predecessor_summary_wire_plan is None:
        _require_summary_wire_prefix(semantic, plan)
    else:
        old = predecessor_summary_wire_plan.materialization
        new = plan.materialization
        if (
            old.root_policy_value != new.root_policy_value
            or old.tool_items != new.tool_items
            or new.ordered_input_items[: len(old.ordered_input_items)]
            != old.ordered_input_items
            or len(new.ordered_input_items) <= len(old.ordered_input_items)
        ):
            raise ValueError("summary repair rewrote its first request")
    return PreparedCompactionSummaryCall(
        semantic=semantic,
        wire_input_plan=plan,
    )


_SUMMARY_TOOL_DENIAL = (
    "The requested tool was not executed and returned no information. Do not "
    "retry it or call any other tool. For this response, the current checkpoint "
    "request supersedes earlier requests to perform task work. Return the requested "
    "semantic handoff now as plain text and preserve the actual task status. The "
    "fact that this summary-only call did not execute a tool is not evidence that "
    "the user's task is queued, deferred, blocked, or waiting for a later turn."
)


def prepare_compaction_summary_repair_semantic(
    initial: PreparedCompactionSummarySemantic,
    *,
    tool_calls: tuple[LLMToolCall, ...],
) -> PreparedCompactionSummarySemantic:
    """Append one provider-valid ephemeral denial group to the same request."""

    if not tool_calls:
        raise ValueError("summary repair requires at least one denied tool call")
    compiled = initial.semantic_input
    suffix = (
        LLMMessage.assistant_turn(tool_calls=tool_calls),
        *tuple(
            LLMMessage.tool_result(_SUMMARY_TOOL_DENIAL, tool_call_id=item.id)
            for item in tool_calls
        ),
    )
    messages = compiled.messages + suffix
    placements = list(compiled.message_placements)
    tool_calls_fingerprint = context_fingerprint(
        "pulsara.compaction-summary-repair-tool-calls.v1",
        tuple((item.id, item.name, item.arguments) for item in tool_calls),
    )
    for message in suffix:
        ordinal = len(placements)
        placements.append(
            _ephemeral_summary_placement(
                message_ordinal=ordinal,
                role=message.role,
                domain="repair",
                identity={
                    "initial": initial.semantic_fingerprint,
                    "message": compaction_summary_message_prefix_fingerprint(
                        (message,)
                    ),
                    "ordinal": ordinal,
                },
            )
        )
    frozen_placements = tuple(placements)
    binding = initial.compile_binding
    estimate = binding.estimator.estimate_frozen_input(
        system_prompt=compiled.system_prompt,
        messages=messages,
        tools=compiled.tools,
    )
    values = {
        "context_id": context_fingerprint(
            "pulsara.compaction-summary-repair-context-id.v1",
            {
                "initial": initial.semantic_fingerprint,
                "tool_calls": tool_calls_fingerprint,
            },
        ),
        "canonical_input_identity": compiled.canonical_input_identity,
        "system_prompt": compiled.system_prompt,
        "messages": messages,
        "message_placements": frozen_placements,
        "tools": compiled.tools,
        "final_estimate": estimate,
        "compile_binding_fingerprint": compiled.compile_binding_fingerprint,
    }
    repaired = PreparedCompactionSummarySemanticInput(
        **values,
        compiled_semantic_fingerprint=context_fingerprint(
            "pulsara.compaction-summary-repair-semantic-input.v1",
            {
                "context": values["context_id"],
                "canonical": compiled.canonical_input_identity.identity_fingerprint,
                "messages": compaction_summary_message_prefix_fingerprint(messages),
                "placements": compiled_message_placements_fingerprint(
                    frozen_placements
                ),
                "estimate": _estimate_value(estimate),
                "binding": compiled.compile_binding_fingerprint,
            },
        ),
    )
    semantic_fingerprint = context_fingerprint(
        "pulsara.prepared-compaction-summary-repair-semantic.v1",
        {
            "initial": initial.semantic_fingerprint,
            "compiled": repaired.compiled_semantic_fingerprint,
            "tool_calls": tool_calls_fingerprint,
        },
    )
    return PreparedCompactionSummarySemantic(
        call=initial.call,
        source_proof=initial.source_proof,
        compile_binding=initial.compile_binding,
        native_projection_set=initial.native_projection_set,
        summary_request=initial.summary_request,
        semantic_input=repaired,
        semantic_fingerprint=semantic_fingerprint,
    )


def _require_summary_wire_prefix(
    semantic: PreparedCompactionSummarySemantic,
    plan: FrozenProviderWireInputPlan,
) -> None:
    proof = semantic.source_proof
    if not isinstance(proof, InstalledPrefixSummarySourceProof):
        return
    predecessor = proof.source_view.predecessor_epoch_view
    if predecessor is None:
        return
    old = predecessor.wire_input_plan.materialization
    new = plan.materialization
    if (
        old.root_policy_value != new.root_policy_value
        or old.tool_items != new.tool_items
        or not new.ordered_input_items
    ):
        raise ValueError("summary wire root or tools changed within an epoch")
    # The synthetic request lowers to one final user item on both supported APIs.
    actual_prefix = new.ordered_input_items[:-1]
    old_items = old.ordered_input_items
    if (
        len(actual_prefix) < len(old_items)
        or actual_prefix[: len(old_items)] != old_items
    ):
        raise ValueError("summary actual wire input rewrote the installed prefix")


def _summary_source_identity_value(
    proof: CompactionSummarySourceProof,
) -> object:
    if isinstance(proof, InstalledPrefixSummarySourceProof):
        return {
            "kind": "INSTALLED_PREFIX",
            "source_view": proof.source_view.source_view_fingerprint,
            "prefix": proof.prefix_proof.proof_fingerprint,
        }
    return {
        "kind": "DESTINATION_PROJECTION",
        "canonical": proof.canonical_source.dispatch_read.composite_fingerprint,
        "source_digest": proof.cumulative_source_digest,
        "target": proof.target_fact.target_fingerprint,
        "trigger": proof.resolved_trigger_tokens,
        "projection": llm_content_identity_value(proof.projection.content),
        "active_request": (
            None
            if proof.active_request is None
            else proof.active_request.canonical_value()
        ),
    }


def _synthetic_summary_placement(
    *,
    message_ordinal: int,
    proof: ProviderPrefixCutProof,
    summary_request: str,
) -> FrozenCompiledMessagePlacement:
    return _ephemeral_summary_placement(
        message_ordinal=message_ordinal,
        role=MessageRole.USER,
        domain="request",
        identity={
            "prompt": summary_request_fingerprint(summary_request),
            "text": summary_request,
            "prefix": proof.proof_fingerprint,
        },
    )


def _ephemeral_summary_placement(
    *,
    message_ordinal: int,
    role: MessageRole,
    domain: str,
    identity: object,
) -> FrozenCompiledMessagePlacement:
    origin = context_fingerprint(
        f"pulsara.compaction-summary-ephemeral-{domain}-item.v1",
        identity,
    )
    return FrozenCompiledMessagePlacement(
        message_ordinal=message_ordinal,
        origin_entry_id=None,
        origin_item_fingerprint=origin,
        within_origin_ordinal=0,
        role=role,
    )


def _thaw_tool(item: object) -> ToolSpec:
    parameters = thaw_json(item.parameters)  # type: ignore[attr-defined]
    if not isinstance(parameters, dict):
        raise TypeError("summary tool schema is not an object")
    return ToolSpec(
        item.name,  # type: ignore[attr-defined]
        item.description,  # type: ignore[attr-defined]
        parameters,
    )


async def _drain_summary_execution(execution: object):
    await execution.aclose()  # type: ignore[attr-defined]
    return await execution.wait_physical_completion()  # type: ignore[attr-defined]


class _NullLiveBus:
    def offer_nowait(self, **values: object) -> None:
        del values


def _estimate_value(estimate: TokenEstimate) -> dict[str, object]:
    return {
        "system": estimate.system_tokens,
        "messages": estimate.message_tokens,
        "message_by_index": estimate.message_tokens_by_index,
        "tools": estimate.tool_tokens,
        "envelope": estimate.envelope_tokens,
        "visual_image": estimate.visual_image_tokens,
        "total": estimate.total_input_tokens,
    }


__all__ = [
    "CompactionSummarySourceProof",
    "DestinationProjectionSummarySourceProof",
    "InstalledPrefixSummarySourceProof",
    "PreparedCompactionSummaryCall",
    "PreparedCompactionSummarySemantic",
    "PreparedCompactionSummarySemanticInput",
    "RawCompactionSummaryResponse",
    "promote_compaction_summary_call",
    "prepare_compaction_summary_repair_semantic",
    "prepare_compaction_summary_semantic",
    "prepare_destination_projection_summary_semantic",
]
