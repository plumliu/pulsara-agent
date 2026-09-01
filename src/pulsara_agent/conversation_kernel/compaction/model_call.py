"""One-shot primary-model execution for a Round 5B semantic handoff."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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
    FrozenCompactionSourceView,
    ProviderPrefixCutProof,
    compaction_summary_message_prefix_fingerprint,
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
from pulsara_agent.llm.input import LLMMessage, LLMToolCall, MessageRole, ToolSpec
from pulsara_agent.llm.estimator import TokenEstimate
from pulsara_agent.llm.request import (
    MAXIMUM_PROVIDER_WIRE_INPUT_BYTES,
    FrozenProviderWireInputPlan,
    LLMContext,
    provider_wire_input_plan_identity_fingerprint,
)
from pulsara_agent.llm.resolution import ResolvedModelCall
from pulsara_agent.llm.validation import validate_model_context_shape_for_call
from pulsara_agent.model_input.contracts import (
    CanonicalModelInputIdentity,
    FrozenCompiledMessagePlacement,
    FrozenModelInputSemanticProjection,
    FrozenToolSpec,
    ModelInputCompileBinding,
    compiled_message_placements_fingerprint,
)
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


@dataclass(frozen=True, slots=True)
class PreparedCompactionSummarySemantic:
    call: ResolvedModelCall = field(repr=False)
    source_view: FrozenCompactionSourceView = field(repr=False)
    source_projection: FrozenModelInputSemanticProjection = field(repr=False)
    prefix_proof: ProviderPrefixCutProof
    compile_binding: ModelInputCompileBinding = field(repr=False)
    native_projection_set: FrozenNativeToolProjectionSet = field(repr=False)
    summary_request: str = field(repr=False)
    semantic_input: PreparedCompactionSummarySemanticInput = field(repr=False)
    semantic_fingerprint: str

    def __post_init__(self) -> None:
        source = self.source_projection
        compiled = self.semantic_input
        binding = self.compile_binding
        count = self.prefix_proof.summary_prefix_message_count
        expected_request = LLMMessage.user(self.summary_request)
        expected_request_placement = _synthetic_summary_placement(
            message_ordinal=count,
            proof=self.prefix_proof,
            summary_request=self.summary_request,
        )
        if (
            self.call.fact.purpose is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
            or self.call.target.fact != binding.target_fact
            or source.canonical_input_identity
            != self.source_view.canonical_dispatch_read.compile_snapshot.canonical_input.identity
            or source.system_prompt != self.source_view.materialized_system_prompt()
            or source.messages != self.source_view.materialized_messages()
            or source.tools != binding.tool_surface.tool_specs
            or source.compile_binding_fingerprint != binding.binding_fingerprint
            or self.prefix_proof.source_view_fingerprint
            != self.source_view.source_view_fingerprint
            or not self.summary_request
            or not 0 < count <= len(source.messages)
            or compaction_summary_message_prefix_fingerprint(source.messages[:count])
            != self.prefix_proof.summary_prefix_messages_fingerprint
            or compiled.canonical_input_identity != source.canonical_input_identity
            or compiled.system_prompt != source.system_prompt
            or compiled.tools != source.tools
            or compiled.compile_binding_fingerprint != binding.binding_fingerprint
            or len(compiled.messages) < count + 1
            or compiled.messages[:count] != source.messages[:count]
            or compiled.message_placements[:count] != source.message_placements[:count]
            or compiled.messages[count] != expected_request
            or compiled.message_placements[count] != expected_request_placement
            or not self.semantic_fingerprint.startswith("sha256:")
        ):
            raise ValueError("summary semantic carrier does not exact-join")
        estimate = binding.estimator.estimate_frozen_input(
            system_prompt=compiled.system_prompt,
            messages=compiled.messages,
            tools=compiled.tools,
        )
        if estimate != compiled.final_estimate:
            raise ValueError("summary semantic estimate changed")

    @property
    def canonical_read(self) -> FrozenCanonicalProviderDispatchRead:
        return self.source_view.canonical_dispatch_read

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
                "source_view": semantic.source_view.source_view_fingerprint,
                "prefix": semantic.prefix_proof.proof_fingerprint,
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
                self._semantic.source_view.canonical_dispatch_read.compile_snapshot.canonical_input.identity.session_id
            ),
            turn_id=(
                self._semantic.source_view.canonical_dispatch_read.compile_snapshot.canonical_input.identity.turn_id
            ),
            live_bus=_NullLiveBus(),  # type: ignore[arg-type]
            proposed_entry_id=f"compaction-summary:{self.request_fingerprint[7:39]}",
            conversation_scope_kind=(
                self._semantic.source_view.canonical_dispatch_read.compile_snapshot.canonical_input.identity.conversation_scope_kind.value
            ),
            scope_subagent_task_id=(
                self._semantic.source_view.canonical_dispatch_read.compile_snapshot.canonical_input.identity.scope_subagent_task_id
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
        source_view=source_view,
        source_projection=source_projection,
        prefix_proof=prefix_proof,
        compile_binding=binding,
        native_projection_set=native_projection_set,
        summary_request=summary_request,
        semantic_input=compiled,
        semantic_fingerprint=semantic_fingerprint,
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
        or plan.wire_api != semantic.call.target.model_profile.provider_profile.wire_api
        or plan.provider_profile_fingerprint
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
        source_view=initial.source_view,
        source_projection=initial.source_projection,
        prefix_proof=initial.prefix_proof,
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
    predecessor = semantic.source_view.predecessor_epoch_view
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
        "total": estimate.total_input_tokens,
    }


__all__ = [
    "PreparedCompactionSummaryCall",
    "PreparedCompactionSummarySemantic",
    "PreparedCompactionSummarySemanticInput",
    "RawCompactionSummaryResponse",
    "promote_compaction_summary_call",
    "prepare_compaction_summary_repair_semantic",
    "prepare_compaction_summary_semantic",
]
