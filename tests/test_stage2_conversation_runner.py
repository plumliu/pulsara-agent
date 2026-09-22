from __future__ import annotations

from tests.support.model_config import frozen_test_prompt
from pulsara_agent.llm.input import FrozenPromptContent

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
import psycopg
from PIL import Image

from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE, PermissionMode
from pulsara_agent.conversation_kernel.contracts import (
    InlineContent,
    PromptDeliveryMode,
)
from pulsara_agent.conversation_kernel.live import (
    LiveAgentEventBus,
    LiveObservationKind,
    LiveSettlementKind,
)
from pulsara_agent.conversation_kernel.context_sources import (
    ContextSourceRegistry,
    build_memory_context_source,
)
from pulsara_agent.conversation_kernel.cancellation import ActiveTurnCancellationIntent
from pulsara_agent.conversation_kernel.cold_epoch import (
    CanonicalColdContinuationSeed,
    AdoptedCompactionContinuationSeed,
    SubagentInitialSeed,
)
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
import pulsara_agent.conversation_kernel.input_continuity as input_continuity
from pulsara_agent.conversation_kernel.input_continuity import (
    HostProviderInputContinuityOwner,
    ProviderInputContinuityConflict,
    _issue_root_bootstrap_lease_source,
)
from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionDisposition,
    CompactionTrigger,
    ResolvedCompactionHeadroomBounds,
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.compaction.coordinator import (
    AutomaticCompactionTriggerCandidate,
)
from pulsara_agent.conversation_kernel.provider_dispatch import (
    PreparedWireMeasurementDecision,
)
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.compaction.prompt import (
    compaction_summary_request,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    CompactionWriteReservation,
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    AutomaticMemoryTriggerDisposition,
    FrozenMemoryTriggerPolicy,
    MemoryUsePolicy,
)
from pulsara_agent.conversation_kernel.memory.hints import (
    CheapMemoryWriteHintMatcher,
    MEMORY_WRITE_HINT_BODY,
    MemoryWriteOptOut,
    TurnMemoryUseOptOut,
)
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    ToolCallDeltaPayload,
    ToolCallEndPayload,
    ToolCallStartPayload,
    live_digest,
)
from pulsara_agent.conversation_kernel.repository import (
    AcceptedEntry,
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.safe_point import ProviderSafePointCoordinator
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.runner import (
    ConversationKernelRunner,
    FrozenPostResponseResourceQuote,
    OutputResourceInterruption,
    _stable_id,
)
from pulsara_agent.conversation_kernel.tool_contracts import (
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    KernelToolResult,
)
from pulsara_agent.conversation_kernel.tool_artifacts import (
    PostgresToolArtifactReadPort,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.todo_runtime import (
    PreparedTodoRootRunActivation,
)
from pulsara_agent.conversation_kernel.vocabulary import LiveEventType
from pulsara_agent.llm.input import (
    LLMImagePart,
    LLMTextPart,
    MessageRole,
    join_text_content,
    text_part_values,
)
from pulsara_agent.primitives.context import context_fingerprint, freeze_json, thaw_json
from pulsara_agent.llm.provider import RouteWireProfile
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.llm.provider_sanitization import sanitize_provider_failure
from pulsara_agent.llm.model_catalog import (
    ReasoningEffortChoices,
    ReasoningSelectableControls,
)
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ModelConnectionId,
    ReasoningEffortSelection,
)
from pulsara_agent.primitives.model_call import ModelCallPurpose, ModelTokenUsageFact
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    ContextBindingBaseKind,
    ContextRenderMode,
    ContextRenderVariant,
    ContextSourceCandidate,
    ContextSourceKind,
    ModelInputScopeKind,
    ModelInputCompileFailureKind,
    StructuredModelInputCompileError,
    provider_input_item_text,
)
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.continuity import (
    ProviderInputContinuityScope,
    decode_runtime_observation,
)
from pulsara_agent.ports.provider_stream import (
    ProviderModelOutputIncomplete,
    ProviderNormalizedTerminalKind,
    ProviderOutputIncompleteReason,
    ProviderPhysicalCompletion,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.ports.user_control_feedback import (
    UserControlFeedbackContentV1,
    UserControlFeedbackInstallationAttempt,
    UserControlProcessFact,
)
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.postgres import verified_postgres_provider
from tests.support.model_config import (
    enqueue_test_prompt,
    include_test_runtime_connections,
    start_test_root_turn,
    test_model_binding,
    test_model_limits,
    test_model_resolution_snapshot,
    test_model_runtime,
)
from tests.support.round3 import (
    CallbackScriptedKernelModel,
    Round10TestSubagentRuntime,
    ScriptedKernelModel,
    StaticContextSourceCollector,
    StructuredToolPort,
    completed_provider_execution_for_test,
    seal_test_direct_tool_port,
)
from tests.support.subagents import (
    accept_active_subagent_fixture,
    run_admitted_subagent_fixture,
)


pytestmark = pytest.mark.postgres


def _name(prefix: str) -> str:
    if prefix == "workspace":
        return f"ctx:workspace/{uuid4().hex}"
    return f"{prefix}:{uuid4().hex}"


def _acquire_bound_host_writer(repository, **kwargs):
    """Create/reacquire a session with the one production-shaped test binding."""

    lease = repository.acquire_host_writer(**kwargs)
    measured_before = getattr(repository, "host_write_transactions", None)
    repository.update_session_model_call_binding(
        lease.guard,
        binding=test_model_binding(test_model_runtime()),
        deadline_monotonic=monotonic() + 30,
    )
    if measured_before is not None:
        repository.host_write_transactions = measured_before
    return lease


def _turn_permission_fingerprint(
    repository: ConversationKernelRepository,
    *,
    session_id: str,
    turn_id: str,
) -> str:
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        row = connection.execute(
            "SELECT permission_snapshot_fingerprint FROM pulsara_v3.turns "
            "WHERE session_id = %s AND id = %s",
            (session_id, turn_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _seed_artifact_result_with_memory_provenance(
    repository: ConversationKernelRepository,
    *,
    lease: object,
    workspace_id: str,
    memory_ids: tuple[str, ...],
) -> str:
    guard = lease.guard
    turn_id = _name("turn")
    start_test_root_turn(
        repository,
        guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        permission_snapshot_id=_name("permission-snapshot"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text("read an artifact page"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    cut = repository.prepare_provider_input_cut(
        guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    assistant_entry_id = _name("entry")
    tool_call_id = _name("call")
    repository.commit_assistant_message(
        guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b"artifact_read"),
        blocks=(
            AssistantToolCallBlock(
                block_id=_name("block"),
                tool_call_id=tool_call_id,
                tool_name="artifact_read",
                arguments=freeze_json({"artifact_id": _name("artifact")}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    attempt_id = _name("attempt")
    repository.accept_tool_attempt(
        guard,
        attempt_id=attempt_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="executor",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        permission_snapshot_fingerprint=_turn_permission_fingerprint(
            repository,
            session_id=guard.session_id,
            turn_id=turn_id,
        ),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    result_id = _name("result")
    observed_at = datetime.now(timezone.utc)
    candidate = build_prepared_tool_result_acceptance(
        guard=guard,
        workspace_id=workspace_id,
        result_id=result_id,
        result_entry_id=_name("entry"),
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(
            b'{"status":"success","text":"bounded page"}'
        ),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None,
        artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        model_visible_memory_fact_ids=memory_ids,
        observed_at=observed_at,
        observation_duration_microseconds=1,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="runtime:test",
    )
    repository.accept_tool_result(
        guard,
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    final_cut = repository.prepare_provider_input_cut(
        guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    repository.commit_assistant_message(
        guard,
        cut=final_cut,
        entry_id=_name("entry"),
        parent_content=InlineContent.from_bytes(b"page observed"),
        blocks=(
            AssistantTextBlock(
                block_id=_name("block"),
                text=InlineContent.from_bytes(b"page observed"),
            ),
        ),
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    return result_id


class _ScriptedModel(ScriptedKernelModel):
    pass


class _RecordingColdEpochAssembler:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate
        self.semantic_seeds: list[object] = []
        self.finalized = 0

    def prepare_semantic(self, **kwargs):
        self.semantic_seeds.append(kwargs["seed"])
        return self._delegate.prepare_semantic(**kwargs)

    def bind_prepared_wire(self, prepared, **kwargs):
        self.finalized += 1
        return self._delegate.bind_prepared_wire(prepared, **kwargs)


class _CompactionSummaryExecution:
    def __init__(
        self,
        value: str | list[object],
        *,
        usage: TransportUsageReport | None = None,
    ) -> None:
        if isinstance(value, str):
            block_id = "compaction-summary:text"
            self._items = [
                TextStartPayload(block_id),
                TextDeltaPayload(block_id, value),
                TextEndPayload(
                    block_id,
                    value,
                    len(value.encode("utf-8")),
                    live_digest(value),
                ),
            ]
        else:
            self._items = list(value)
        self._items.append(
            ProviderStreamTerminal(
                terminal_kind=ProviderNormalizedTerminalKind.COMPLETED,
                usage=usage or TransportUsageReport(usage_status="missing", usage=None),
            )
        )

    async def read_next(self):
        return self._items.pop(0) if self._items else None

    async def aclose(self) -> None:
        self._items.clear()

    async def wait_physical_completion(self) -> ProviderPhysicalCompletion:
        return ProviderPhysicalCompletion(
            ProviderPhysicalCompletionStatus.COMPLETED,
            None,
        )


class _CompactionSummaryTransport:
    def __init__(
        self,
        text: str | list[list[object] | str],
        *,
        usage_modes: list[str] | None = None,
    ) -> None:
        self._scripts = list(text) if isinstance(text, list) else [text]
        self._usage_modes = list(usage_modes or ())
        self.calls: list[object] = []
        self.contexts: list[object] = []
        self.usage_reports: list[TransportUsageReport] = []

    def open_stream(self, *, call, context):
        self.calls.append(call)
        self.contexts.append(context)
        if not self._scripts:
            raise AssertionError("unexpected extra compaction summary request")
        mode = self._usage_modes.pop(0) if self._usage_modes else "missing"
        quote = context.provider_wire_input_plan.quote
        if mode == "missing":
            usage = TransportUsageReport(usage_status="missing", usage=None)
        else:
            input_tokens = quote.final_wire_estimated_input_tokens
            if mode == "reported_different":
                input_tokens += 12_345
            cached_tokens = input_tokens // 2 if mode == "reported_cached" else None
            usage = TransportUsageReport(
                usage_status="reported",
                usage=ModelTokenUsageFact(
                    input_tokens=input_tokens,
                    cached_input_tokens=cached_tokens,
                    output_tokens=7,
                    total_tokens=input_tokens + 7,
                ),
            )
        self.usage_reports.append(usage)
        return _CompactionSummaryExecution(self._scripts.pop(0), usage=usage)


class _BlockingCompactionSummaryExecution:
    def __init__(self, transport: _BlockingCompactionSummaryTransport) -> None:
        self._transport = transport

    async def read_next(self):
        self._transport.started.set()
        await self._transport.release.wait()
        return None

    async def aclose(self) -> None:
        self._transport.release.set()

    async def wait_physical_completion(self) -> ProviderPhysicalCompletion:
        return ProviderPhysicalCompletion(
            ProviderPhysicalCompletionStatus.COMPLETED,
            None,
        )


class _BlockingCompactionSummaryTransport:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.contexts: list[object] = []

    def open_stream(self, *, call, context):
        del call
        self.contexts.append(context)
        return _BlockingCompactionSummaryExecution(self)


class _CompactionScriptedModel(_ScriptedModel):
    def __init__(
        self,
        calls: list[list[object]],
        summary: str | list[list[object] | str],
        *,
        summary_usage_modes: list[str] | None = None,
    ) -> None:
        super().__init__(calls)
        self.summary_transport = _CompactionSummaryTransport(
            summary,
            usage_modes=summary_usage_modes,
        )

    def resolve_compaction_summary_call(self, **kwargs):
        call = super().resolve_compaction_summary_call(**kwargs)
        self.summary_transport.binding_id = call.target.transport.binding_id
        self.summary_transport.contract_version = call.target.transport.contract_version
        resolved = replace(
            call,
            target=replace(call.target, transport=self.summary_transport),
        )
        self._model_runtime.register_test_call_override(resolved)
        return resolved


class _LimitedCompactionScriptedModel(_CompactionScriptedModel):
    def __init__(
        self,
        calls: list[list[object]],
        summary: str,
        *,
        total_context_tokens: int = 256_000,
        max_input_tokens: int = 47_192,
    ) -> None:
        super().__init__(calls, summary)
        limits = test_model_limits(
            total_context_tokens=total_context_tokens,
            max_input_tokens=max_input_tokens,
            max_output_tokens=1_000,
            default_output_tokens=1_000,
            input_safety_margin_tokens=0,
        )
        self._preparer = DirectKernelModelPort(
            model_runtime=test_model_runtime(
                api_key="sk-fixture-secret",
                base_url="https://example.invalid/v1",
                model_id="test-pro",
                wire_api="openai_chat_completions",
                limits=limits,
            ),
        )


def _context_snapshot_payload(request) -> dict[str, object]:
    matches = tuple(
        message
        for message in request.compiled_input.messages
        if message.role is MessageRole.USER
        and message.content
        and isinstance(message.content[0], LLMTextPart)
        and message.content[0].text.startswith(
            "This Runtime-authored context snapshot contains quoted retained content."
        )
    )
    assert len(matches) == 1
    message = matches[0]
    value = json.loads(message.content[0].text.split("\n", 1)[1])
    assert isinstance(value, dict)
    sections: list[dict[str, object]] = []
    index = 1
    while index < len(message.content):
        opener = message.content[index]
        assert isinstance(opener, LLMTextPart)
        prefix = "\n[PULSARA_RETAINED_CONTENT "
        assert opener.text.startswith(prefix) and opener.text.endswith("]\n")
        metadata = json.loads(opener.text[len(prefix) : -2])
        index += 1
        parts: list[object] = []
        while message.content[index] != LLMTextPart("\n[/PULSARA_RETAINED_CONTENT]\n"):
            part = message.content[index]
            parts.append(
                json.loads(part.text) if isinstance(part, LLMTextPart) else part
            )
            index += 1
        sections.append({"metadata": metadata, "parts": tuple(parts)})
        index += 1
    value["sections"] = tuple(sections)
    active = tuple(
        section
        for section in sections
        if section["metadata"] == {"role": "user", "section": "active"}
    )
    assert len(active) <= 1
    continuation = value["continuation"]
    assert isinstance(continuation, dict)
    continuation["active_request"] = (
        None
        if not active
        else {
            "text": "".join(
                part for part in active[0]["parts"] if isinstance(part, str)
            )
        }
    )
    return value


def _hook_context_bodies(request) -> tuple[str, ...]:
    return tuple(
        decoded.body
        for message in request.compiled_input.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in join_text_content(message.content)
        for decoded in (decode_runtime_observation(message),)
        if decoded.source_kind is ContextSourceKind.HOOK_CONTEXT
    )


class _NativePlanningDeadlineModel(_ScriptedModel):
    def freeze_native_tool_eligibility(self, **kwargs):
        del kwargs
        raise TimeoutError("injected native Tool planning deadline")


class _ChangingRegistryCollector(StaticContextSourceCollector):
    def __init__(self) -> None:
        self._reads = 0

    @property
    def registry_fingerprint(self) -> str:
        self._reads += 1
        if self._reads == 1:
            return super().registry_fingerprint
        return "sha256:" + ("0" * 64)


class _RecordingHookReservation:
    def __init__(self) -> None:
        self.retire_calls = 0

    def retire(self) -> None:
        self.retire_calls += 1
        if self.retire_calls > 1:
            raise AssertionError("Hook reservation was retired more than once")


class _PostCompactionHookContextCollector(StaticContextSourceCollector):
    def __init__(self, text: str) -> None:
        self._text = text
        self.reservations: list[_RecordingHookReservation] = []

    def freeze_hook_context_source(self, **_kwargs: object):
        binding = ContextSourceRegistry().binding(ContextSourceKind.HOOK_CONTEXT)
        variants = tuple(
            ContextRenderVariant(
                mode,
                text,
                len(text.encode("utf-8")),
                context_fingerprint(
                    "context-render-variant:v1",
                    {"mode": mode.value, "text": text},
                ),
            )
            for mode, text in (
                (ContextRenderMode.FULL, self._text),
                (ContextRenderMode.COMPACT, self._text),
            )
        )
        instance_id = "context-source:hook-context:test"
        semantic = context_fingerprint(
            "context-source-candidate:v1",
            {
                "source_kind": ContextSourceKind.HOOK_CONTEXT.value,
                "source_instance_id": instance_id,
                "source_contract_fingerprint": binding.contract_fingerprint,
                "variants": tuple(item.semantic_fingerprint for item in variants),
            },
        )
        candidate = ContextSourceCandidate(
            source_kind=ContextSourceKind.HOOK_CONTEXT,
            source_instance_id=instance_id,
            source_contract_version=binding.contract_version,
            source_contract_fingerprint=binding.contract_fingerprint,
            source_semantic_fingerprint=semantic,
            channel=binding.channel,
            trust_class=binding.trust,
            budget_class=binding.budget,
            placement_ordinal=binding.placement,
            degradation_priority=binding.degradation,
            variants=variants,
            lifecycle=binding.lifecycle,
            domain_semantic_fingerprint=context_fingerprint(
                "test:hook-context-domain:v1", self._text
            ),
        )
        reservation = _RecordingHookReservation()
        self.reservations.append(reservation)
        return candidate, reservation


class _FailingOperationalExtension:
    def offer_operational_nowait(self, _offer) -> None:
        raise RuntimeError("injected best-effort observer failure")


class _RevocableStructuredToolPort(StructuredToolPort):
    def __init__(self, delegate: object, *, tool_names: tuple[str, ...] = ()) -> None:
        super().__init__(delegate, tool_names=tool_names)
        self.revoked = False

    def borrow_tool_surface(self, prepared):
        if self.revoked:
            raise RuntimeError("injected tool surface revocation")
        borrow = super().borrow_tool_surface(prepared)
        original_validate = borrow._validate

        def validate(current, tool_name):
            if self.revoked:
                raise RuntimeError("injected tool surface revocation")
            return original_validate(current, tool_name)

        borrow._validate = validate
        return borrow

    def validate_tool_surface_borrow(self, borrow, prepared):
        if self.revoked:
            raise RuntimeError("injected tool surface revocation")
        return super().validate_tool_surface_borrow(borrow, prepared)


class _RecordingBorrowToolPort(StructuredToolPort):
    def __init__(self, delegate: object, *, tool_names: tuple[str, ...] = ()) -> None:
        super().__init__(delegate, tool_names=tool_names)
        self.release_calls: list[int] = []

    def borrow_tool_surface(self, prepared):
        borrow = super().borrow_tool_surface(prepared)
        original_release = borrow._release
        record_index = len(self.release_calls)
        self.release_calls.append(0)

        def release(current):
            self.release_calls[record_index] += 1
            if self.release_calls[record_index] > 1:
                raise AssertionError("tool surface borrow released more than once")
            original_release(current)

        borrow._release = release
        return borrow


class _SurfaceRevokingCollector(StaticContextSourceCollector):
    def __init__(self, tools: _RevocableStructuredToolPort) -> None:
        self._tools = tools

    def complete_frozen_sources(self, frozen, **kwargs):
        result = super().complete_frozen_sources(frozen, **kwargs)
        self._tools.revoked = True
        return result


class _MeasuredRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.host_write_transactions = 0

    def _writer_transaction(self, guard, *, deadline_monotonic):
        self.host_write_transactions += 1
        return super()._writer_transaction(guard, deadline_monotonic=deadline_monotonic)


class _LostAssistantAckRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self._lost_once = False

    def commit_assistant_message(self, *args, **kwargs):
        accepted = super().commit_assistant_message(*args, **kwargs)
        if not self._lost_once:
            self._lost_once = True
            raise OSError("injected lost assistant commit acknowledgement")
        return accepted


class _LostCompactionAdoptionAckRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.lost_once = False

    def adopt_context_snapshot(self, *args, **kwargs):
        accepted = super().adopt_context_snapshot(*args, **kwargs)
        if not self.lost_once:
            self.lost_once = True
            raise OSError("injected lost compaction adoption acknowledgement")
        return accepted


class _BlockingAssistantCommitRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.started = Event()
        self.release = Event()

    def commit_assistant_message(self, *args, **kwargs):
        self.started.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("assistant commit test release timed out")
        return super().commit_assistant_message(*args, **kwargs)


class _TransientNoneAssistantRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.commit_calls = 0
        self.confirm_calls = 0

    def commit_assistant_message(self, *args, **kwargs):
        self.commit_calls += 1
        if self.commit_calls == 1:
            raise OSError("injected pre-write assistant failure")
        return super().commit_assistant_message(*args, **kwargs)

    def confirm_assistant_message_winner(self, *args, **kwargs):
        self.confirm_calls += 1
        if self.confirm_calls == 1:
            return None
        return super().confirm_assistant_message_winner(*args, **kwargs)


class _ConflictingAssistantCommitRepository(ConversationKernelRepository):
    def commit_assistant_message(self, *args, **kwargs):
        raise ConversationKernelConflict("injected assistant candidate conflict")


class _CountingAssistantCommitRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.assistant_commit_calls = 0

    def commit_assistant_message(self, *args, **kwargs):
        self.assistant_commit_calls += 1
        return super().commit_assistant_message(*args, **kwargs)


class _CancellingFirstSteerRepository(ConversationKernelRepository):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.cancelled_once = False

    def consume_prepared_prompt_steer(self, guard, *, candidate, deadline_monotonic):
        if not self.cancelled_once:
            self.cancelled_once = True
            self.cancel_prompt(
                guard,
                queue_item_id=candidate.queue_item_id,
                occurred_at=datetime.now(timezone.utc),
                actor_id="test:concurrent-cancel",
                deadline_monotonic=deadline_monotonic,
            )
        return super().consume_prepared_prompt_steer(
            guard,
            candidate=candidate,
            deadline_monotonic=deadline_monotonic,
        )


class _ExpiredSteerCompiler(StructuredModelInputCompiler):
    def compile_new_epoch(self, request, **kwargs):
        if len(request.canonical_input.items) > 1:
            kwargs["deadline_monotonic"] = monotonic() - 1
        return super().compile_new_epoch(request, **kwargs)

    def compile_installed_append(self, request, **kwargs):
        if len(request.canonical_input.items) > 1:
            kwargs["deadline_monotonic"] = monotonic() - 1
        return super().compile_installed_append(request, **kwargs)


class _OnlyOneSteerCompiler(StructuredModelInputCompiler):
    def __init__(self) -> None:
        super().__init__()
        self.failures: list[tuple[int, ModelInputCompileFailureKind]] = []

    def _require_one_steer(self, request) -> int:
        steer_count = sum(
            item.input_origin is not None and item.input_origin.value == "HUMAN_STEER"
            for item in request.canonical_input.items
        )
        if steer_count > 1:
            self.failures.append(
                (
                    steer_count,
                    ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET,
                )
            )
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET
            )
        return steer_count

    def compile_new_epoch(self, request, **kwargs):
        steer_count = self._require_one_steer(request)
        try:
            return super().compile_new_epoch(request, **kwargs)
        except StructuredModelInputCompileError as exc:
            self.failures.append((steer_count, exc.kind))
            raise

    def compile_installed_append(self, request, **kwargs):
        steer_count = self._require_one_steer(request)
        try:
            return super().compile_installed_append(request, **kwargs)
        except StructuredModelInputCompileError as exc:
            self.failures.append((steer_count, exc.kind))
            raise


class _FullRequiredBudgetCompiler(StructuredModelInputCompiler):
    @staticmethod
    def _raise_required_full_budget() -> None:
        raise StructuredModelInputCompileError(
            ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET
        )

    def compile_new_epoch(self, request, **kwargs):
        del request, kwargs
        self._raise_required_full_budget()

    def compile_installed_append(self, request, **kwargs):
        del request, kwargs
        self._raise_required_full_budget()


class _AssertingTool:
    def __init__(self, provider, session_id: str) -> None:
        self._provider = provider
        self._session_id = session_id
        self.invocations: list[str] = []

    async def authorize(
        self, *, tool_name, arguments, tool_call_id, turn_id, assistant_entry_id
    ):
        del turn_id, assistant_entry_id
        del tool_name, arguments, tool_call_id
        return KernelToolAuthorization(KernelToolAuthorizationKind.ALLOW, "test-policy")

    async def request_confirmation(self, **kwargs):
        del kwargs
        raise AssertionError("test policy never requests human confirmation")

    async def invoke(
        self,
        *,
        tool_name,
        arguments,
        tool_call_id,
        attempt_id,
        turn_id,
        assistant_entry_id,
        invocation_context,
        live_sink=None,
    ):
        del turn_id, assistant_entry_id, invocation_context, live_sink
        with self._provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=monotonic() + 10,
        ) as connection:
            row = connection.execute(
                """
                SELECT a.id, b.tool_name
                FROM pulsara_v3.tool_execution_attempts a
                JOIN pulsara_v3.assistant_message_blocks b
                  ON b.session_id = a.session_id
                 AND b.assistant_entry_id = a.assistant_entry_id
                 AND b.tool_call_id = a.tool_call_id
                WHERE a.session_id = %s AND a.id = %s
                """,
                (self._session_id, attempt_id),
            ).fetchone()
        assert row == (attempt_id, tool_name)
        assert tool_call_id
        self.invocations.append(attempt_id)
        return KernelToolResult(state="SUCCESS", content=b"tool-ok")


class _DenyingTool(_AssertingTool):
    async def authorize(
        self, *, tool_name, arguments, tool_call_id, turn_id, assistant_entry_id
    ):
        del turn_id, assistant_entry_id
        del tool_name, arguments, tool_call_id
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.PERMISSION_DENIED,
            "test-policy:deny",
            "denied before dispatch",
        )


class _SemanticInvalidArgumentsThenSuccessTool(_AssertingTool):
    async def invoke(self, **kwargs):
        result = await super().invoke(**kwargs)
        if len(self.invocations) == 1:
            return KernelToolResult(
                state="INVALID_ARGUMENTS",
                content=b'{"error":"dependency reference is unknown"}',
            )
        return result


class _ConfirmationWithoutControllerTool(_AssertingTool):
    async def authorize(self, **kwargs):
        del kwargs
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.REQUIRE_CONFIRMATION,
            "test-policy:require-confirmation",
            "confirmation required",
        )

    async def request_confirmation(self, **kwargs):
        del kwargs
        return KernelToolAuthorization(
            KernelToolAuthorizationKind.PERMISSION_DENIED,
            "interaction:no-controller",
            "no controller is attached",
        )


class _BlockingTool(_AssertingTool):
    def __init__(self, provider, session_id: str) -> None:
        super().__init__(provider, session_id)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().invoke(**kwargs)


class _LargeTool(_AssertingTool):
    async def invoke(self, **kwargs):
        await super().invoke(**kwargs)
        return KernelToolResult(state="SUCCESS", content=b"z" * (70 << 10))


class _ThirtyKiBTool(_AssertingTool):
    async def invoke(self, **kwargs):
        await super().invoke(**kwargs)
        return KernelToolResult(state="SUCCESS", content=b"z" * (30 << 10))


class _BlockingSourceCollector(StaticContextSourceCollector):
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.freeze_calls = 0
        self.complete_calls = 0

    def freeze_non_trigger_sources(self, **kwargs):
        self.freeze_calls += 1
        self.started.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError("test source capture was not released")
        return super().freeze_non_trigger_sources(**kwargs)

    def complete_frozen_sources(self, frozen, **kwargs):
        self.complete_calls += 1
        return super().complete_frozen_sources(frozen, **kwargs)


class _PostAdmissionPendingSteerReadGate:
    """Pause the first pending read after the target ROOT writer commits."""

    def __init__(self, delegate: object, *, target_turn_id: str) -> None:
        self._delegate = delegate
        self._target_turn_id = target_turn_id
        self.started = Event()
        self.release = Event()
        self.calls = 0
        self._blocked = False

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if not self._blocked and kwargs.get("target_turn_id") == self._target_turn_id:
            self._blocked = True
            self.started.set()
            if not self.release.wait(timeout=5):
                raise TimeoutError("post-admission pending read was not released")
        return self._delegate(*args, **kwargs)  # type: ignore[operator]


class _PolicyMemoryProjection:
    def __init__(self) -> None:
        self._write = MemoryWriteOptOut()
        self._all = TurnMemoryUseOptOut()
        self._hint = CheapMemoryWriteHintMatcher()
        self.preference_calls = 0
        self.recall_calls = 0

    def classify_memory_trigger(self, text: str) -> FrozenMemoryTriggerPolicy:
        if self._all.excludes(text):
            return FrozenMemoryTriggerPolicy(
                AutomaticMemoryTriggerDisposition.DISABLED_BY_EXPLICIT_USER_DIRECTIVE,
                MemoryUsePolicy.ALL_DISABLED_BY_USER,
                False,
            )
        memory_use = (
            MemoryUsePolicy.WRITE_DISABLED_BY_USER
            if self._write.excludes(text)
            else MemoryUsePolicy.ENABLED
        )
        return FrozenMemoryTriggerPolicy(
            (
                AutomaticMemoryTriggerDisposition.SKIPPED_LOW_INFORMATION
                if len(" ".join(text.split())) < 8
                else AutomaticMemoryTriggerDisposition.ELIGIBLE
            ),
            memory_use,
            memory_use.allows_writes and self._hint.matches(text),
        )

    def classify_automatic_trigger(
        self, text: str
    ) -> AutomaticMemoryTriggerDisposition:
        return self.classify_memory_trigger(text).automatic_recall

    async def freeze_response_preference_source(self):
        self.preference_calls += 1
        return build_memory_context_source(
            kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
            texts=(' {"items":[]} ',),
        )

    async def freeze_automatic_recall_source(self, _query: str):
        self.recall_calls += 1
        return build_memory_context_source(
            kind=ContextSourceKind.MEMORY_RECALL,
            texts=(
                '{"items":[]}',
                '{"items":[]}',
                '{"items":[]}',
            ),
        )

class _DelayedPreparedExecution:
    def __init__(
        self,
        delegate,
        started: asyncio.Event,
        release: asyncio.Event,
        *,
        append_candidate,
        install_authority,
    ) -> None:
        self._delegate = delegate
        self._started = started
        self._release = release
        self._append_candidate = append_candidate
        self._install_authority = install_authority

    def discard(self) -> None:
        self._delegate.discard()

    async def open_once(self, permit):
        if (
            permit.epoch_nonce != self._append_candidate.epoch_nonce
            or permit.epoch_revision
            != self._append_candidate.expected_epoch_revision + 1
        ):
            raise RuntimeError("delayed execution permit mismatch")
        self._install_authority.consume(
            permit,
            candidate=self._append_candidate,
            execution=self,
        )
        self._started.set()
        await self._release.wait()
        if self._delegate._opened:  # noqa: SLF001
            raise RuntimeError("scripted execution already opened")
        self._delegate._opened = True  # noqa: SLF001
        for item in self._delegate._items:  # noqa: SLF001
            yield item
        self._delegate._completion = completed_provider_execution_for_test(  # noqa: SLF001
            self._delegate._request  # noqa: SLF001
        )

    def take_completed_result_once(self):
        return self._delegate.take_completed_result_once()


class _BlockingFirstCallModel(_ScriptedModel):
    def __init__(self, calls: list[list[object]]) -> None:
        super().__init__(calls)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    def preflight_execution(
        self,
        request,
        *,
        append_candidate,
        install_authority,
    ):
        prepared = super().preflight_execution(
            request,
            append_candidate=append_candidate,
            install_authority=install_authority,
        )
        if len(self.requests) == 1:
            return _DelayedPreparedExecution(
                prepared,
                self.started,
                self.release,
                append_candidate=append_candidate,
                install_authority=install_authority,
            )
        return prepared


class _HeadroomOrderingReader(CanonicalProviderInputReader):
    def __init__(self, provider, *, blob_reader) -> None:
        super().__init__(provider, blob_reader=blob_reader)
        self.operations: list[str] = []

    def read_compaction_headroom_preflight(self, cut, *, deadline_monotonic: float):
        self.operations.append("headroom")
        return super().read_compaction_headroom_preflight(
            cut, deadline_monotonic=deadline_monotonic
        )

    def read_frozen_dispatch(
        self,
        cut,
        *,
        deadline_monotonic: float,
        _connection=None,
        _prospective_root_candidate=None,
    ):
        self.operations.append("dispatch")
        return super().read_frozen_dispatch(
            cut,
            deadline_monotonic=deadline_monotonic,
            _connection=_connection,
            _prospective_root_candidate=_prospective_root_candidate,
        )


class _SequencedDirectKernelModel(DirectKernelModelPort):
    def __init__(
        self,
        *,
        model_runtime,
        scripts: tuple[tuple[dict[str, object], ...], ...],
    ):
        super().__init__(model_runtime=model_runtime)
        self._scripts = scripts
        self.requests = []

    def preflight_execution(
        self,
        request,
        *,
        append_candidate,
        install_authority,
    ):
        self.requests.append(request)
        adapter = request.prepared_call.call.target.transport._adapter
        script = list(self._scripts[request.model_call_index - 1])
        if request.prepared_call.call.target.model_profile.wire_api.value == (
            "openai_chat_completions"
        ):
            adapter._mock_chunks = script
        else:
            adapter._mock_events = script
        return super().preflight_execution(
            request,
            append_candidate=append_candidate,
            install_authority=install_authority,
        )


class _CompactionSequencedDirectKernelModel(_SequencedDirectKernelModel):
    def __init__(
        self,
        *,
        model_runtime,
        scripts: tuple[tuple[dict[str, object], ...], ...],
        summary: str,
    ) -> None:
        super().__init__(model_runtime=model_runtime, scripts=scripts)
        self.summary_transport = _CompactionSummaryTransport(summary)
        self._next_script = 0

    def preflight_execution(
        self,
        request,
        *,
        append_candidate,
        install_authority,
    ):
        self.requests.append(request)
        adapter = request.prepared_call.call.target.transport._adapter
        script = list(self._scripts[self._next_script])
        self._next_script += 1
        if request.prepared_call.call.target.model_profile.wire_api.value == (
            "openai_chat_completions"
        ):
            adapter._mock_chunks = script
        else:
            adapter._mock_events = script
        return DirectKernelModelPort.preflight_execution(
            self,
            request,
            append_candidate=append_candidate,
            install_authority=install_authority,
        )

    def resolve_compaction_summary_call(self, **kwargs):
        call = super().resolve_compaction_summary_call(**kwargs)
        self.summary_transport.binding_id = call.target.transport.binding_id
        self.summary_transport.contract_version = call.target.transport.contract_version
        resolved = replace(
            call,
            target=replace(call.target, transport=self.summary_transport),
        )
        self._model_runtime.register_test_call_override(resolved)
        return resolved


class _NearBoundReplayContinuityOwner(HostProviderInputContinuityOwner):
    def reserve_assistant_replay_fragment(self, **kwargs):
        view = self.current_view(kwargs["scope"])
        assert view is not None
        fragment = kwargs["fragment"]
        original = input_continuity.MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES
        input_continuity.MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES = (
            max(
                view.logical_bytes,
                view.wire_input_plan.quote.final_wire_utf8_bytes,
            )
            + fragment.logical_utf8_bytes
            - 1
        )
        try:
            return super().reserve_assistant_replay_fragment(**kwargs)
        finally:
            input_continuity.MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES = original


class _FailingPostConsumptionReader:
    def __init__(self, delegate: CanonicalProviderInputReader) -> None:
        self._delegate = delegate
        self.calls = 0

    def read_frozen_compile_snapshot(self, cut, *, deadline_monotonic):
        return self._delegate.read_frozen_compile_snapshot(
            cut, deadline_monotonic=deadline_monotonic
        )

    def read_compaction_headroom_preflight(self, cut, *, deadline_monotonic):
        return self._delegate.read_compaction_headroom_preflight(
            cut, deadline_monotonic=deadline_monotonic
        )

    def read_prospective_root_dispatch(self, candidate, *, deadline_monotonic):
        return self._delegate.read_prospective_root_dispatch(
            candidate, deadline_monotonic=deadline_monotonic
        )

    def read_frozen_dispatch(self, cut, *, deadline_monotonic):
        self.calls += 1
        # Post-writer activation and steer preparation account for the first
        # two frozen-cut reads.  Fail only the exact read after the canonical
        # consumption transaction.
        if self.calls == 3:
            raise RuntimeError("injected post-consumption canonical mismatch")
        return self._delegate.read_frozen_dispatch(
            cut, deadline_monotonic=deadline_monotonic
        )

    def read_owner_issued_dispatch_observation(self, cut, *, deadline_monotonic):
        self.calls += 1
        if self.calls == 3:
            raise RuntimeError("injected post-consumption canonical mismatch")
        return self._delegate.read_owner_issued_dispatch_observation(
            cut, deadline_monotonic=deadline_monotonic
        )

    def hydrate_selected_provider_replays(self, **kwargs):
        return self._delegate.hydrate_selected_provider_replays(**kwargs)


class _RecordingReplayHydrationReader:
    def __init__(
        self, delegate: CanonicalProviderInputReader, *, fail_hydration: bool = False
    ) -> None:
        self._delegate = delegate
        self._fail_hydration = fail_hydration
        self.dispatch_deadlines: list[float] = []
        self.hydration_deadlines: list[float] = []
        self.hydration_selected_counts: list[int] = []

    def read_frozen_compile_snapshot(self, cut, *, deadline_monotonic):
        return self._delegate.read_frozen_compile_snapshot(
            cut, deadline_monotonic=deadline_monotonic
        )

    def read_compaction_headroom_preflight(self, cut, *, deadline_monotonic):
        return self._delegate.read_compaction_headroom_preflight(
            cut, deadline_monotonic=deadline_monotonic
        )

    def read_prospective_root_dispatch(self, candidate, *, deadline_monotonic):
        self.dispatch_deadlines.append(deadline_monotonic)
        return self._delegate.read_prospective_root_dispatch(
            candidate, deadline_monotonic=deadline_monotonic
        )

    def read_frozen_dispatch(self, cut, *, deadline_monotonic):
        self.dispatch_deadlines.append(deadline_monotonic)
        return self._delegate.read_frozen_dispatch(
            cut, deadline_monotonic=deadline_monotonic
        )

    def read_owner_issued_dispatch_observation(self, cut, *, deadline_monotonic):
        self.dispatch_deadlines.append(deadline_monotonic)
        return self._delegate.read_owner_issued_dispatch_observation(
            cut, deadline_monotonic=deadline_monotonic
        )

    def hydrate_selected_provider_replays(self, **kwargs):
        deadline = kwargs["deadline_monotonic"]
        self.hydration_deadlines.append(deadline)
        if self._fail_hydration:
            raise TimeoutError("injected selected hydration deadline")
        result = self._delegate.hydrate_selected_provider_replays(**kwargs)
        self.hydration_selected_counts.append(
            0 if result is None else len(result.selected_manifests)
        )
        return result


def _text_stream(text: str, *, block: str = "text:1") -> list[object]:
    return [
        TextStartPayload(block),
        TextDeltaPayload(block, text),
        TextEndPayload(block, text, len(text.encode("utf-8")), live_digest(text)),
    ]


def _tool_stream() -> list[object]:
    arguments = '{"command":"true"}'
    return [
        ToolCallStartPayload("call:1", "call:1", "terminal"),
        ToolCallDeltaPayload("call:1", "call:1", arguments),
        ToolCallEndPayload(
            block_identity="call:1",
            tool_call_id="call:1",
            tool_name="terminal",
            arguments_json=arguments,
            utf8_bytes=len(arguments.encode("utf-8")),
            digest=live_digest(arguments),
        ),
    ]


def _many_tool_stream(count: int) -> list[object]:
    result: list[object] = []
    arguments = "{}"
    for ordinal in range(count):
        tool_call_id = f"call:output-resource:{ordinal}"
        result.extend(
            (
                ToolCallStartPayload(tool_call_id, tool_call_id, "terminal"),
                ToolCallDeltaPayload(tool_call_id, tool_call_id, arguments),
                ToolCallEndPayload(
                    block_identity=tool_call_id,
                    tool_call_id=tool_call_id,
                    tool_name="terminal",
                    arguments_json=arguments,
                    utf8_bytes=len(arguments.encode("utf-8")),
                    digest=live_digest(arguments),
                ),
            )
        )
    return result


def _named_tool_stream(
    *,
    tool_name: str,
    tool_call_id: str,
    arguments: dict[str, object],
) -> list[object]:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return [
        ToolCallStartPayload(tool_call_id, tool_call_id, tool_name),
        ToolCallDeltaPayload(tool_call_id, tool_call_id, encoded),
        ToolCallEndPayload(
            block_identity=tool_call_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_json=encoded,
            utf8_bytes=len(encoded.encode("utf-8")),
            digest=live_digest(encoded),
        ),
    ]


def _summary_tool_stream(tool_call_id: str) -> list[object]:
    arguments = '{"command":"never-dispatch"}'
    return [
        ToolCallStartPayload(tool_call_id, tool_call_id, "terminal"),
        ToolCallDeltaPayload(tool_call_id, tool_call_id, arguments),
        ToolCallEndPayload(
            block_identity=tool_call_id,
            tool_call_id=tool_call_id,
            tool_name="terminal",
            arguments_json=arguments,
            utf8_bytes=len(arguments.encode("utf-8")),
            digest=live_digest(arguments),
        ),
    ]


def _todo_tool_stream() -> list[object]:
    arguments = json.dumps(
        {
            "items": [
                {"text": "Inspect exact path", "status": "completed"},
                {"text": "Run retained gates", "status": "in_progress"},
            ]
        },
        separators=(",", ":"),
    )
    return [
        ToolCallStartPayload("call:todo", "call:todo", "todo"),
        ToolCallDeltaPayload("call:todo", "call:todo", arguments),
        ToolCallEndPayload(
            block_identity="call:todo",
            tool_call_id="call:todo",
            tool_name="todo",
            arguments_json=arguments,
            utf8_bytes=len(arguments.encode("utf-8")),
            digest=live_digest(arguments),
        ),
    ]


def _round5a1_chat_scripts() -> tuple[tuple[dict[str, object], ...], ...]:
    arguments = '{"command":"true"}'
    return (
        (
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": "opaque-chat-tool"},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": "checking",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call:1",
                                    "type": "function",
                                    "function": {
                                        "name": "terminal",
                                        "arguments": arguments,
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ),
        (
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": "opaque-chat-final"},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "chat final"},
                        "finish_reason": "stop",
                    }
                ]
            },
        ),
    )


def _round5a1_responses_scripts() -> tuple[tuple[dict[str, object], ...], ...]:
    return (
        (
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "reasoning",
                            "id": "reasoning:tool",
                            "status": "completed",
                            "summary": [],
                            "encrypted_content": "opaque-responses-tool",
                        },
                        {
                            "type": "message",
                            "id": "message:tool",
                            "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "checking"}],
                        },
                        {
                            "type": "function_call",
                            "id": "function:1",
                            "status": "completed",
                            "call_id": "call:1",
                            "name": "terminal",
                            "arguments": '{"command":"true"}',
                        },
                    ],
                },
            },
        ),
        (
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "reasoning",
                            "id": "reasoning:final",
                            "status": "completed",
                            "summary": [],
                            "encrypted_content": "opaque-responses-final",
                        },
                        {
                            "type": "message",
                            "id": "message:final",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "responses final"}
                            ],
                        },
                    ],
                },
            },
        ),
    )


def _large_native_replay_script(
    api: str,
    *,
    ordinal: int,
) -> tuple[dict[str, object], ...]:
    opaque = f"opaque-{ordinal}:" + "r" * 50_000
    public = f"native replay answer {ordinal}:" + "p" * 12_000
    if api == "openai_chat_completions":
        return (
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": opaque},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": public},
                        "finish_reason": "stop",
                    }
                ]
            },
        )
    if api == "openai_responses":
        return (
            {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [
                        {
                            "type": "reasoning",
                            "id": f"reasoning:{ordinal}",
                            "status": "completed",
                            "summary": [],
                            "encrypted_content": opaque,
                        },
                        {
                            "type": "message",
                            "id": f"message:{ordinal}",
                            "status": "completed",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": public}],
                        },
                    ],
                },
            },
        )
    raise AssertionError(f"unsupported test API: {api}")


def test_capability_adoption_runs_before_each_unprepared_dispatch(
    stage2_migrated_postgres_database,
):
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    tool = _AssertingTool(provider, session_id)
    observations = []

    async def adopt():
        observations.append(tuple(tool.invocations))
        return True

    model = _ScriptedModel([_tool_stream(), _text_stream("after update")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tool, tool_names=("terminal",)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        before_provider_preparation=adopt,
    )
    result = asyncio.run(
        runner.run_turn(frozen_test_prompt("call a tool and continue"))
    )
    assert result.final_text == "after update"
    assert result.model_call_count == 2
    assert len(observations) == 2
    assert observations[0] == ()
    assert len(observations[1]) == 1


def test_k3_user_control_feedback_is_measured_before_atomic_publication(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    turn_id = _stable_id("turn", session_id, command_id)
    model = _ScriptedModel([_tool_stream(), _text_stream("feedback observed")])
    tool = _AssertingTool(provider, session_id)
    installed: list[AcceptedEntry] = []
    runner: ConversationKernelRunner

    async def install_feedback_before_followup() -> bool:
        if len(model.requests) != 1 or installed:
            return True
        content = UserControlFeedbackContentV1(
            command_id=_name("control"),
            session_id=session_id,
            host_session_id="host:test",
            process_id="process:test",
            command="python worker.py",
            cwd="/tmp",
            origin_turn_id=turn_id,
            origin_subagent_task_id=None,
            target_root_turn_id=turn_id,
            process=UserControlProcessFact(
                "TERMINATION_COMPLETED", "killed", -15, "TERMINAL", False
            ),
            monitor=None,
            public_code="BACKGROUND_CONTROL_COMPLETED",
            public_detail="The process was stopped.",
        )
        installed.append(
            await runner.install_user_control_feedback(
                attempt=UserControlFeedbackInstallationAttempt(
                    session_id=session_id,
                    workspace_id=workspace_id,
                    writer_generation=lease.guard.writer_generation,
                    target_root_turn_id=turn_id,
                    entry_id=_name("entry:user-control"),
                    content=content,
                    occurred_at=datetime.now(timezone.utc),
                    actor_id="host:test",
                ),
                deadline_monotonic=monotonic() + 30,
                admitted_writer=CompactionWriteReservation(
                    ModelInputScopeKind.ROOT, None
                ),
            )
        )
        return True

    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tool, tool_names=("terminal",)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        before_provider_preparation=install_feedback_before_followup,
    )
    result = asyncio.run(
        runner.run_turn(
            frozen_test_prompt("call a tool, then observe control feedback"),
            command_id=command_id,
        )
    )

    assert result.final_text == "feedback observed"
    assert len(installed) == 1
    feedback_messages = tuple(
        message
        for placement, message in zip(
            model.requests[1].compiled_input.message_placements,
            model.requests[1].compiled_input.messages,
            strict=True,
        )
        if placement.origin_entry_id == installed[0].entry_id
    )
    assert len(feedback_messages) == 1
    assert "pulsara_user_control_feedback" in join_text_content(
        feedback_messages[0].content
    )


def test_k3_user_control_feedback_resource_failure_precedes_publication(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    feedback_entry_id = _name("entry:user-control")
    turn_id = _name("turn")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=command_id,
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        permission_snapshot_id=_name("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text("existing active input"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([])

    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        maximum_input_tokens_per_call=100,
    )
    with pytest.raises(StructuredModelInputCompileError):
        asyncio.run(
            runner.install_user_control_feedback(
                attempt=UserControlFeedbackInstallationAttempt(
                    session_id=session_id,
                    workspace_id=workspace_id,
                    writer_generation=lease.guard.writer_generation,
                    target_root_turn_id=turn_id,
                    entry_id=feedback_entry_id,
                    content=UserControlFeedbackContentV1(
                        command_id=_name("control"),
                        session_id=session_id,
                        host_session_id="host:test",
                        process_id="process:test",
                        command="python worker.py",
                        cwd="/tmp",
                        origin_turn_id=turn_id,
                        origin_subagent_task_id=None,
                        target_root_turn_id=turn_id,
                        process=UserControlProcessFact(
                            "TERMINATION_COMPLETED",
                            "killed",
                            -15,
                            "TERMINAL",
                            False,
                        ),
                        monitor=None,
                        public_code="BACKGROUND_CONTROL_COMPLETED",
                        public_detail="\\" * 28_000,
                    ),
                    occurred_at=datetime.now(timezone.utc),
                    actor_id="host:test",
                ),
                deadline_monotonic=monotonic() + 30,
                admitted_writer=CompactionWriteReservation(
                    ModelInputScopeKind.ROOT, None
                ),
            )
        )
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id=%s AND id=%s",
            (session_id, feedback_entry_id),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.agent_events "
            "WHERE session_id=%s AND event_type='UserControlFeedbackAccepted'",
            (session_id,),
        ).fetchone() == (0,)


def test_k3_active_feedback_compacts_before_atomic_publication(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _name("turn")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry:user"),
        context_binding_revision_id=_name("revision"),
        permission_snapshot_id=_name("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text("active request"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    source_cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=source_cut,
        entry_id=_name("entry:assistant"),
        parent_content=InlineContent.from_bytes(b"x" * 60_000),
        blocks=(
            AssistantTextBlock(
                block_id=_name("block"),
                text=InlineContent.from_bytes(b"x" * 60_000),
            ),
        ),
        complete_turn=False,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    model = _LimitedCompactionScriptedModel(
        [], "A concise handoff retaining the active request."
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            auto_trigger_ratio=0.90,
            post_compaction_target_ratio=0.75,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    feedback_entry_id = _name("entry:user-control")
    attempt = UserControlFeedbackInstallationAttempt(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_generation=lease.guard.writer_generation,
        target_root_turn_id=turn_id,
        entry_id=feedback_entry_id,
        content=UserControlFeedbackContentV1(
            command_id=_name("control"),
            session_id=session_id,
            host_session_id="host:test",
            process_id="process:test",
            command="python worker.py",
            cwd="/tmp",
            origin_turn_id=turn_id,
            origin_subagent_task_id=None,
            target_root_turn_id=turn_id,
            process=UserControlProcessFact(
                "TERMINATION_COMPLETED", "killed", -15, "TERMINAL", False
            ),
            monitor=None,
            public_code="BACKGROUND_CONTROL_COMPLETED",
            public_detail="\\" * 18_000,
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="host:test",
    )

    async def exercise():
        accepted = await runner.install_user_control_feedback(
            attempt=attempt,
            deadline_monotonic=monotonic() + 30,
            admitted_writer=CompactionWriteReservation(ModelInputScopeKind.ROOT, None),
            cancellation_intent=ActiveTurnCancellationIntent(
                turn_id, ModelInputScopeKind.ROOT, None
            ),
        )
        await owner.aclose()
        return accepted

    accepted = asyncio.run(exercise())

    assert accepted.entry_id == feedback_entry_id
    assert model.requests == []
    assert len(model.summary_transport.contexts) == 1
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        snapshot_count, feedback_count = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pulsara_v3.context_snapshots "
            " WHERE session_id=%s), "
            "(SELECT count(*) FROM pulsara_v3.transcript_entries "
            " WHERE session_id=%s AND id=%s)",
            (session_id, session_id, feedback_entry_id),
        ).fetchone()
    assert snapshot_count == 1
    assert feedback_count == 1
    final_cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    assert (
        final_cut.context_binding_revision_id != source_cut.context_binding_revision_id
    )
    final_read = CanonicalProviderInputReader(
        provider, blob_reader=runner._provider_dispatch._input_reader._blob_reader
    ).read_frozen_dispatch(final_cut, deadline_monotonic=monotonic() + 30)
    assert any(
        item.source_entry_id == feedback_entry_id
        for item in final_read.compile_snapshot.canonical_input.items
    )


def test_stage2_runner_commits_semantic_invalid_arguments_then_retries_and_completes(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    tool = _SemanticInvalidArgumentsThenSuccessTool(provider, session_id)
    command_id = _name("command")
    turn_id = _stable_id("turn", session_id, command_id)
    steer_command_id = _name("steer-command")
    steer_queue_item_id = _name("steer-queue")
    steer_text = "steer between the invalid result and corrected retry"
    steer_injected = False

    async def inject_steer_before_retry() -> bool:
        nonlocal steer_injected
        if len(tool.invocations) == 1 and not steer_injected:
            enqueue_test_prompt(
                repository,
                lease.guard,
                command_id=steer_command_id,
                queue_item_id=steer_queue_item_id,
                client_submission_id=steer_command_id,
                delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
                target_turn_id=turn_id,
                permission_snapshot_id=None,
                requested_permission_mode=None,
                model_call_binding=None,
                content=FrozenPromptContent.text(steer_text),
                occurred_at=datetime.now(timezone.utc),
                actor_id="test",
                deadline_monotonic=monotonic() + 10,
            )
            steer_injected = True
        return True

    model = _ScriptedModel(
        [
            _named_tool_stream(
                tool_name="create_agent_tasks",
                tool_call_id="call:semantic-invalid",
                arguments={
                    "tasks": [
                        {
                            "task_key": "reader",
                            "task": "read an unknown dependency",
                            "depends_on": ["task:missing-task"],
                        }
                    ]
                },
            ),
            _named_tool_stream(
                tool_name="create_agent_tasks",
                tool_call_id="call:semantic-corrected",
                arguments={
                    "tasks": [
                        {
                            "task_key": "reader",
                            "task": "read without an unknown dependency",
                            "depends_on": [],
                        }
                    ]
                },
            ),
            _text_stream("invalid arguments were reported without interrupting"),
        ]
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tool, tool_names=("create_agent_tasks",)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        before_provider_preparation=inject_steer_before_retry,
    )

    result = asyncio.run(
        runner.run_turn(
            frozen_test_prompt("create the dependent task"), command_id=command_id
        )
    )

    assert result.final_text == "invalid arguments were reported without interrupting"
    assert result.model_call_count == 3
    assert len(tool.invocations) == 2
    first_input, retry_input, final_input = (
        request.compiled_input for request in model.requests
    )
    assert retry_input.system_prompt == first_input.system_prompt
    assert retry_input.tools == first_input.tools
    assert retry_input.messages[: len(first_input.messages)] == first_input.messages
    assert final_input.messages[: len(retry_input.messages)] == retry_input.messages
    retry_tool_results = tuple(
        message
        for message in retry_input.messages
        if message.role is MessageRole.TOOL_RESULT
    )
    assert len(retry_tool_results) == 1
    assert "dependency reference is unknown" in join_text_content(
        retry_tool_results[0].content
    )
    assert any(
        message.role is MessageRole.USER
        and message.content
        and message.content == (LLMTextPart(steer_text),)
        for message in retry_input.messages
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        result_rows = connection.execute(
            """
            SELECT tool_call_id, result_state, attempt_id
            FROM pulsara_v3.tool_results
            WHERE session_id = %s
              AND tool_call_id IN (%s, %s)
            ORDER BY accepted_at, tool_call_id
            """,
            (
                session_id,
                "call:semantic-invalid",
                "call:semantic-corrected",
            ),
        ).fetchall()
        turn_row = connection.execute(
            """
            SELECT status, terminal_reason
            FROM pulsara_v3.turns
            WHERE session_id = %s AND id = %s
            """,
            (session_id, result.turn_id),
        ).fetchone()
        steer_row = connection.execute(
            "SELECT status, consumed_entry_id "
            "FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s AND id = %s",
            (session_id, steer_queue_item_id),
        ).fetchone()
    assert result_rows == [
        ("call:semantic-invalid", "INVALID_ARGUMENTS", tool.invocations[0]),
        ("call:semantic-corrected", "SUCCESS", tool.invocations[1]),
    ]
    assert turn_row == ("COMPLETED", "COMPLETED")
    assert steer_row is not None
    assert steer_row[0] == "CONSUMED"
    assert steer_row[1] is not None


def test_root_control_feedback_fence_keeps_turn_open_for_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    barriers: list[str] = []
    fences: list[str] = []
    settlements: list[tuple[str, bool]] = []

    async def barrier(turn_id: str) -> None:
        barriers.append(turn_id)

    async def fence(turn_id: str) -> bool:
        fences.append(turn_id)
        return len(fences) == 1

    async def settle_fence(turn_id: str, *, turn_completed: bool) -> None:
        settlements.append((turn_id, turn_completed))

    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel(
            [_text_stream("first answer"), _text_stream("feedback-aware answer")]
        ),
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        root_control_preparation_barrier=barrier,
        root_control_completion_fence=fence,
        root_control_completion_settlement=settle_fence,
    )
    resource_quotes: list[FrozenPostResponseResourceQuote] = []
    require_resources = runner._require_post_response_resources

    def observe_resources(
        quote: FrozenPostResponseResourceQuote,
        *,
        effective_input_budget_tokens: int,
    ) -> None:
        resource_quotes.append(quote)
        require_resources(
            quote,
            effective_input_budget_tokens=effective_input_budget_tokens,
        )

    monkeypatch.setattr(runner, "_require_post_response_resources", observe_resources)
    result = asyncio.run(runner.run_turn(frozen_test_prompt("question")))
    assert result.final_text == "feedback-aware answer"
    assert result.model_call_count == 2
    assert len(barriers) == 2
    assert fences == [result.turn_id, result.turn_id]
    assert settlements == [(result.turn_id, False), (result.turn_id, True)]
    assert len(resource_quotes) == 2
    assert resource_quotes[0].followup_wire is not None
    assert resource_quotes[1].followup_wire is None


def test_root_control_feedback_fence_cancellation_settles_installed_seal(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    seal_installed = asyncio.Event()
    hold_fence = asyncio.Event()
    settlements: list[tuple[str, bool]] = []

    async def fence(_turn_id: str) -> bool:
        seal_installed.set()
        await hold_fence.wait()
        return False

    async def settle_fence(turn_id: str, *, turn_completed: bool) -> None:
        settlements.append((turn_id, turn_completed))

    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_text_stream("answer")]),
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        root_control_completion_fence=fence,
        root_control_completion_settlement=settle_fence,
    )

    async def exercise() -> None:
        running = asyncio.create_task(runner.run_turn(frozen_test_prompt("question")))
        await asyncio.wait_for(seal_installed.wait(), timeout=2)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    asyncio.run(exercise())
    assert len(settlements) == 1
    assert settlements[0][1] is False


def test_stage2_runner_text_turn_has_two_entry_transactions_and_no_segments(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _MeasuredRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("answer")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    result = asyncio.run(runner.run_turn(frozen_test_prompt("question")))
    assert result.final_text == "answer"
    assert result.model_call_count == 1
    assert repository.host_write_transactions == 2
    rows = repository.rehydrate_session(
        session_id=session_id, deadline_monotonic=monotonic() + 30
    )
    assert [row["entry_kind"] for row in rows] == [
        "USER_MESSAGE",
        "ASSISTANT_MESSAGE",
    ]
    assert not any("segment" in key for row in rows for key in row)
    events = repository.events_after(
        session_id=session_id,
        after_sequence=0,
        limit=16,
        deadline_monotonic=monotonic() + 30,
    )
    assert tuple(row["event_type"] for row in events) == (
        "UserMessageAccepted",
        "AssistantMessageAccepted",
        "TurnCompleted",
    )


def test_round5b_ordinary_fresh_open_uses_only_neutral_cold_assembler(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("answer")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    recorder = _RecordingColdEpochAssembler(
        runner._provider_dispatch._cold_epoch_assembler
    )
    runner._provider_dispatch._cold_epoch_assembler = recorder

    result = asyncio.run(runner.run_turn(frozen_test_prompt("question")))

    assert result.final_text == "answer"
    assert len(recorder.semantic_seeds) == 1
    assert isinstance(recorder.semantic_seeds[0], CanonicalColdContinuationSeed)
    assert recorder.finalized == 1


@pytest.mark.parametrize(
    ("lose_adoption_ack", "retry_recent"),
    ((False, False), (True, False), (False, True)),
)
def test_round5b_active_manual_compaction_adopts_and_continues_same_run(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
    lose_adoption_ack: bool,
    retry_recent: bool,
) -> None:
    from pulsara_agent.conversation_kernel.compaction import (
        coordinator as compaction_coordinator,
    )

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = (
        _LostCompactionAdoptionAckRepository(provider)
        if lose_adoption_ack
        else ConversationKernelRepository(provider)
    )
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    summary = "A concise free-form handoff preserving the current work."
    model = _CompactionScriptedModel(
        [
            _text_stream("historical answer " + "x" * 80_000),
            _text_stream("final after compaction"),
        ],
        summary,
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    source_collector = StaticContextSourceCollector()
    freeze_non_trigger_calls = 0
    freeze_non_trigger_sources = source_collector.freeze_non_trigger_sources

    def count_non_trigger_freeze(*args, **kwargs):
        nonlocal freeze_non_trigger_calls
        freeze_non_trigger_calls += 1
        return freeze_non_trigger_sources(*args, **kwargs)

    monkeypatch.setattr(
        source_collector,
        "freeze_non_trigger_sources",
        count_non_trigger_freeze,
    )
    pre_full_attempts = 0
    if retry_recent:
        transition = compaction_coordinator.validate_compaction_wire_transition

        def reject_first_recent(**kwargs):
            nonlocal pre_full_attempts
            if kwargs["phase"] == "PRE_FULL":
                pre_full_attempts += 1
                if pre_full_attempts == 1:
                    raise compaction_coordinator.CompactionPlanningError(
                        "compaction successor exceeds its post target"
                    )
            return transition(**kwargs)

        monkeypatch.setattr(
            compaction_coordinator,
            "validate_compaction_wire_transition",
            reject_first_recent,
        )
        monkeypatch.setattr(
            compaction_coordinator,
            "_ordinary_recent_suffixes",
            lambda recent: (recent, recent),
        )

    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=source_collector,
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    cold_recorder = _RecordingColdEpochAssembler(
        runner._provider_dispatch._cold_epoch_assembler
    )
    runner._provider_dispatch._cold_epoch_assembler = cold_recorder

    async def exercise():
        first = await runner.run_turn(frozen_test_prompt("first question"))
        command_id = _name("second-command")
        turn_id = _stable_id("turn", session_id, command_id)
        _request, outcome = await owner.request_manual(
            command_id=_name("compact-command"),
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id=turn_id,
            force=True,
        )
        second = await runner.run_turn(
            frozen_test_prompt("second question"), command_id=command_id
        )
        compacted = await outcome
        await owner.aclose()
        return first, second, compacted

    first, second, compacted = asyncio.run(exercise())

    assert first.final_text.startswith("historical answer")
    assert second.final_text == "final after compaction"
    assert compacted.disposition is CompactionDisposition.COMPACTED
    assert compacted.snapshot_id is not None
    assert len(model.summary_transport.contexts) == 1
    # Initial/follow-up provider work and PRE/POST compaction own five source
    # freezes in this scenario. The injected second recent candidate adds none.
    assert freeze_non_trigger_calls == 5
    if retry_recent:
        assert pre_full_attempts == 2
    summary_context = model.summary_transport.contexts[0]
    assert summary_context.tool_choice == "auto"
    assert summary_context.messages[-1].content == (
        LLMTextPart(compaction_summary_request()),
    )
    assert len(model.requests) == 2
    successor_snapshot = _context_snapshot_payload(model.requests[1])
    assert successor_snapshot["continuation"]["mode"] == "RESUME_ACTIVE_TURN"
    assert successor_snapshot["continuation"]["instruction"].startswith(
        "HANDOFF COMPLETE / RESUME NOW"
    )
    assert successor_snapshot["continuation"]["active_request"]["text"] == (
        "second question"
    )
    assert model.requests[0].compiled_input.system_prompt == (
        model.requests[1].compiled_input.system_prompt
    )
    assert model.requests[0].compiled_input.tools == (
        model.requests[1].compiled_input.tools
    )
    from pulsara_agent.llm.request import (
        provider_wire_input_plan_identity_fingerprint,
    )

    assert provider_wire_input_plan_identity_fingerprint(
        model.requests[0].wire_input_plan,
        target_fact=model.requests[0].prepared_call.call.target.fact,
    ) != provider_wire_input_plan_identity_fingerprint(
        model.requests[1].wire_input_plan,
        target_fact=model.requests[1].prepared_call.call.target.fact,
    )
    assert any(
        isinstance(seed, AdoptedCompactionContinuationSeed)
        for seed in cold_recorder.semantic_seeds
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        snapshot_count, attempt_count = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pulsara_v3.context_snapshots "
            " WHERE session_id = %s), "
            "(SELECT count(*) FROM pulsara_v3.tool_execution_attempts "
            " WHERE session_id = %s)",
            (session_id, session_id),
        ).fetchone()
    assert snapshot_count == 1
    assert attempt_count == 0
    if lose_adoption_ack:
        assert repository.lost_once


@pytest.mark.parametrize(
    ("history", "summaries", "expected_tier", "expected_summary_models"),
    (
        ("brief history", "unused summary", None, ()),
        (
            "large history:" + "h" * 50_000,
            "small source-model handover",
            2,
            ("source-model",),
        ),
        (
            "large history:" + "h" * 50_000,
            [
                "oversized source-model handover:" + "a" * 50_000,
                "small destination-model handover",
            ],
            3,
            ("source-model", "destination-model"),
        ),
        (
            "large history:" + "h" * 50_000,
            [
                [
                    ProviderStreamTerminal(
                        terminal_kind=(
                            ProviderNormalizedTerminalKind.OUTPUT_INCOMPLETE
                        ),
                        usage=TransportUsageReport(usage_status="missing", usage=None),
                        incomplete_reason=(
                            ProviderOutputIncompleteReason.UNKNOWN_PROVIDER_INCOMPLETE
                        ),
                    )
                ],
                "small destination-model handover after source failure",
            ],
            3,
            ("source-model", "destination-model"),
        ),
        (
            "large history:" + "h" * 50_000,
            [
                [
                    ProviderStreamTerminal(
                        terminal_kind=(ProviderNormalizedTerminalKind.PROVIDER_ERROR),
                        usage=TransportUsageReport(usage_status="missing", usage=None),
                        error=sanitize_provider_failure(
                            message="source provider quota exhausted",
                            code_hint="429",
                        ),
                    )
                ],
                "small destination-model handover after source provider error",
            ],
            3,
            ("source-model", "destination-model"),
        ),
        (
            "large history:" + "h" * 50_000,
            [
                [
                    ProviderStreamTerminal(
                        terminal_kind=(ProviderNormalizedTerminalKind.PROVIDER_ERROR),
                        usage=TransportUsageReport(usage_status="missing", usage=None),
                        error=sanitize_provider_failure(
                            message="source provider quota exhausted",
                            code_hint="429",
                        ),
                    )
                ],
                _summary_tool_stream("destination-summary-call:1"),
                "small destination-model handover after denied tool call",
            ],
            3,
            ("source-model", "destination-model", "destination-model"),
        ),
    ),
    ids=(
        "tier-1",
        "tier-2",
        "tier-3-nonfit",
        "tier-3-source-incomplete",
        "tier-3-source-provider-error",
        "tier-3-destination-summary-tool-repair",
    ),
)
def test_model_switch_uses_exact_three_tier_handover_path(
    stage2_migrated_postgres_database,
    history: str,
    summaries: str | list[list[object] | str],
    expected_tier: int | None,
    expected_summary_models: tuple[str, ...],
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    source_runtime = test_model_runtime(
        model_id="source-model",
        wire_api="openai_chat_completions",
    )
    destination_runtime = test_model_runtime(
        model_id="destination-model",
        wire_api="openai_chat_completions",
        connection_id=ModelConnectionId("model-connection:" + "2" * 32),
        limits=test_model_limits(
            total_context_tokens=256_000,
            max_input_tokens=12_000,
            max_output_tokens=1_000,
            default_output_tokens=1_000,
            input_safety_margin_tokens=0,
        ),
    )
    include_test_runtime_connections(destination_runtime, source_runtime)
    active_runtime = [source_runtime]
    model = _CompactionScriptedModel(
        [_text_stream(history), _text_stream("destination answer")],
        summaries,
    )
    model._model_runtime = source_runtime
    model._preparer = DirectKernelModelPort(model_runtime=source_runtime)
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            manual_enabled=False,
            auto_trigger_ratio=0.85,
            minimum_reclaim_tokens=1,
        )
    )
    presentation_notices: list[str] = []
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=(
            lambda: active_runtime[0].freeze_resolution_snapshot()
        ),
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
        presentation_notice_sink=presentation_notices.append,
    )
    switch_results: list[object] = []
    execute_switch = runner.compaction.recover_pending_root_input

    async def record_switch(**kwargs):
        result = await execute_switch(**kwargs)
        switch_results.append(result)
        return result

    runner.compaction.recover_pending_root_input = record_switch

    async def exercise():
        first = await runner.run_turn(frozen_test_prompt("source request"))
        active_runtime[0] = destination_runtime
        model._model_runtime = destination_runtime
        model._preparer = DirectKernelModelPort(model_runtime=destination_runtime)
        repository.update_session_model_call_binding(
            lease.guard,
            binding=test_model_binding(destination_runtime),
            deadline_monotonic=monotonic() + 30,
        )
        second = await runner.run_turn(frozen_test_prompt("destination request"))
        await owner.aclose()
        return first, second

    first, second = asyncio.run(exercise())

    assert first.final_text == history
    assert second.final_text == "destination answer"
    assert tuple(
        call.target.fact.model_id for call in model.summary_transport.calls
    ) == (expected_summary_models)
    assert [
        request.prepared_call.call.target.fact.model_id for request in model.requests
    ] == [
        "source-model",
        "destination-model",
    ]
    assert len(switch_results) == (0 if expected_tier is None else 1)
    if expected_tier is not None:
        assert switch_results[0].model_switch_tier == expected_tier
    assert presentation_notices == (
        ["模型已切换。由于上下文长度变化，接下来的回答可能不如之前连贯。"]
        if expected_tier == 3
        else []
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        snapshot_count, event_count = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pulsara_v3.context_snapshots "
            " WHERE session_id = %s), "
            "(SELECT count(*) FROM pulsara_v3.agent_events "
            " WHERE session_id = %s AND event_type = 'CompactionAdopted')",
            (session_id, session_id),
        ).fetchone()
    assert (snapshot_count, event_count) == (
        (0, 0) if expected_tier is None else (1, 1)
    )


def test_k3_text_only_model_switch_uses_tier_two_for_valid_image_history(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    source_runtime = test_model_runtime(
        model_id="source-vision-model",
        wire_api="openai_chat_completions",
        input_modalities=("text", "image"),
    )
    destination_runtime = test_model_runtime(
        model_id="destination-text-model",
        wire_api="openai_chat_completions",
        connection_id=ModelConnectionId("model-connection:" + "2" * 32),
        input_modalities=("text",),
    )
    include_test_runtime_connections(destination_runtime, source_runtime)
    active_runtime = [source_runtime]
    model = _CompactionScriptedModel(
        [_text_stream("source answer"), _text_stream("destination answer")],
        "source model summary of the visual history",
    )
    model._model_runtime = source_runtime
    model._preparer = DirectKernelModelPort(model_runtime=source_runtime)
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            manual_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=(
            lambda: active_runtime[0].freeze_resolution_snapshot()
        ),
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    switch_results: list[object] = []
    execute_switch = runner.compaction.recover_pending_root_input

    async def record_switch(**kwargs):
        result = await execute_switch(**kwargs)
        switch_results.append(result)
        return result

    runner.compaction.recover_pending_root_input = record_switch
    image_output = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(image_output, "PNG")
    image = LLMImagePart("image/png", image_output.getvalue(), 7, 5)
    image_prompt = freeze_canonical_prompt(
        FrozenPromptContent((LLMTextPart("inspect"), image))
    )

    async def exercise():
        first = await runner.run_turn(image_prompt)
        active_runtime[0] = destination_runtime
        model._model_runtime = destination_runtime
        model._preparer = DirectKernelModelPort(model_runtime=destination_runtime)
        repository.update_session_model_call_binding(
            lease.guard,
            binding=test_model_binding(destination_runtime),
            deadline_monotonic=monotonic() + 30,
        )
        second = await runner.run_turn(frozen_test_prompt("destination request"))
        await owner.aclose()
        return first, second

    first, second = asyncio.run(exercise())

    assert first.final_text == "source answer"
    assert second.final_text == "destination answer"
    assert len(switch_results) == 1
    assert switch_results[0].model_switch_tier == 2
    assert [call.target.fact.model_id for call in model.summary_transport.calls] == [
        "source-vision-model"
    ]
    assert any(
        isinstance(part, LLMImagePart)
        for message in model.summary_transport.contexts[0].messages
        for part in message.content
    )
    assert not any(
        isinstance(part, LLMImagePart)
        for message in model.requests[1].compiled_input.messages
        for part in message.content
    )
    successor = _context_snapshot_payload(model.requests[1])
    assert not any(
        section["metadata"].get("section") == "recent"
        for section in successor["sections"]
    )


def test_k3_text_only_tier_two_keeps_text_recent_when_older_history_has_image(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    source_runtime = test_model_runtime(
        model_id="source-vision-model",
        wire_api="openai_chat_completions",
        input_modalities=("text", "image"),
    )
    destination_runtime = test_model_runtime(
        model_id="destination-text-model",
        wire_api="openai_chat_completions",
        connection_id=ModelConnectionId("model-connection:" + "2" * 32),
        input_modalities=("text",),
    )
    include_test_runtime_connections(destination_runtime, source_runtime)
    active_runtime = [source_runtime]
    model = _CompactionScriptedModel(
        [
            _text_stream("image turn answer"),
            _text_stream("recent one answer"),
            _text_stream("recent two answer"),
            _text_stream("recent three answer"),
            _text_stream("destination answer"),
        ],
        "source model summary of the complete visual history",
    )
    model._model_runtime = source_runtime
    model._preparer = DirectKernelModelPort(model_runtime=source_runtime)
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            manual_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=(
            lambda: active_runtime[0].freeze_resolution_snapshot()
        ),
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    switch_results: list[object] = []
    execute_switch = runner.compaction.recover_pending_root_input

    async def record_switch(**kwargs):
        result = await execute_switch(**kwargs)
        switch_results.append(result)
        return result

    runner.compaction.recover_pending_root_input = record_switch
    image_output = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(image_output, "PNG")
    image_prompt = freeze_canonical_prompt(
        FrozenPromptContent(
            (
                LLMTextPart("old visual request"),
                LLMImagePart("image/png", image_output.getvalue(), 7, 5),
            )
        )
    )

    async def exercise():
        await runner.run_turn(image_prompt)
        for text in ("recent one", "recent two", "recent three"):
            await runner.run_turn(frozen_test_prompt(text))
        active_runtime[0] = destination_runtime
        model._model_runtime = destination_runtime
        model._preparer = DirectKernelModelPort(model_runtime=destination_runtime)
        repository.update_session_model_call_binding(
            lease.guard,
            binding=test_model_binding(destination_runtime),
            deadline_monotonic=monotonic() + 30,
        )
        result = await runner.run_turn(frozen_test_prompt("destination request"))
        await owner.aclose()
        return result

    result = asyncio.run(exercise())

    assert result.final_text == "destination answer"
    assert len(switch_results) == 1
    assert switch_results[0].model_switch_tier == 2
    assert any(
        isinstance(part, LLMImagePart)
        for message in model.summary_transport.contexts[0].messages
        for part in message.content
    )
    successor = _context_snapshot_payload(model.requests[-1])
    recent_sections = [
        section
        for section in successor["sections"]
        if section["metadata"].get("section") == "recent"
    ]
    assert [section["parts"] for section in recent_sections] == [
        ("recent one",),
        ("recent two",),
        ("recent three",),
    ]
    assert not any(
        isinstance(part, LLMImagePart)
        for message in model.requests[-1].compiled_input.messages
        for part in message.content
    )


def test_k3_cold_text_only_handover_uses_one_tier_three_projection_p(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    source_runtime = test_model_runtime(
        model_id="source-vision-model",
        wire_api="openai_chat_completions",
        input_modalities=("text", "image"),
    )
    source_model = _ScriptedModel([_text_stream("source answer")])
    source_model._model_runtime = source_runtime
    source_model._preparer = DirectKernelModelPort(model_runtime=source_runtime)
    source_owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(automatic_enabled=False)
    )
    source_runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=source_runtime.freeze_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=source_model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=source_owner,
        workspace_id=workspace_id,
    )
    image_output = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(image_output, "PNG")
    image = LLMImagePart("image/png", image_output.getvalue(), 7, 5)
    image_prompt = freeze_canonical_prompt(
        FrozenPromptContent(
            (
                LLMTextPart("before"),
                image,
                LLMTextPart("between"),
                image,
            )
        )
    )

    async def seed_history():
        result = await source_runner.run_turn(image_prompt)
        await source_owner.aclose()
        return result

    first = asyncio.run(seed_history())
    assert first.final_text == "source answer"

    destination_runtime = test_model_runtime(
        model_id="destination-text-model",
        wire_api="openai_chat_completions",
        connection_id=ModelConnectionId("model-connection:" + "2" * 32),
        input_modalities=("text",),
    )
    include_test_runtime_connections(destination_runtime, source_runtime)
    repository.update_session_model_call_binding(
        lease.guard,
        binding=test_model_binding(destination_runtime),
        deadline_monotonic=monotonic() + 30,
    )
    destination_model = _CompactionScriptedModel(
        [_text_stream("destination answer")],
        "destination model summary after image omission",
    )
    destination_model._model_runtime = destination_runtime
    destination_model._preparer = DirectKernelModelPort(
        model_runtime=destination_runtime
    )
    destination_owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            manual_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    notices: list[str] = []
    destination_runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=(
            destination_runtime.freeze_resolution_snapshot
        ),
        repository=repository,
        writer_lease=lease,
        model=destination_model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=destination_owner,
        workspace_id=workspace_id,
        presentation_notice_sink=notices.append,
    )
    switch_results: list[object] = []
    execute_switch = destination_runner.compaction.recover_pending_root_input

    async def record_switch(**kwargs):
        result = await execute_switch(**kwargs)
        switch_results.append(result)
        return result

    destination_runner.compaction.recover_pending_root_input = record_switch

    async def exercise_destination():
        result = await destination_runner.run_turn(
            frozen_test_prompt("destination request")
        )
        await destination_owner.aclose()
        return result

    second = asyncio.run(exercise_destination())

    assert second.final_text == "destination answer"
    assert len(switch_results) == 1
    assert switch_results[0].model_switch_tier == 3
    assert [
        call.target.fact.model_id for call in destination_model.summary_transport.calls
    ] == ["destination-text-model"]
    summary_context = destination_model.summary_transport.contexts[0]
    assert not any(
        isinstance(part, LLMImagePart)
        for message in summary_context.messages
        for part in message.content
    )
    assert (
        sum(
            join_text_content(message.content).count("[Image omitted]")
            for message in summary_context.messages
        )
        == 2
    )
    assert not any(
        isinstance(part, LLMImagePart)
        for message in destination_model.requests[0].compiled_input.messages
        for part in message.content
    )
    successor = _context_snapshot_payload(destination_model.requests[0])
    assert not any(
        section["metadata"].get("section") == "recent"
        for section in successor["sections"]
    )
    assert notices == ["模型已切换。由于上下文长度变化，接下来的回答可能不如之前连贯。"]


def test_k3_text_only_tier_three_projects_images_when_source_summary_fails(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    source_runtime = test_model_runtime(
        model_id="source-vision-model",
        wire_api="openai_chat_completions",
        input_modalities=("text", "image"),
    )
    destination_runtime = test_model_runtime(
        model_id="destination-text-model",
        wire_api="openai_chat_completions",
        connection_id=ModelConnectionId("model-connection:" + "2" * 32),
        input_modalities=("text",),
    )
    include_test_runtime_connections(destination_runtime, source_runtime)
    source_failure = [
        ProviderStreamTerminal(
            terminal_kind=ProviderNormalizedTerminalKind.PROVIDER_ERROR,
            usage=TransportUsageReport(usage_status="missing", usage=None),
            error=sanitize_provider_failure(
                message="source provider is unavailable",
                code_hint="503",
            ),
        )
    ]
    active_runtime = [source_runtime]
    model = _CompactionScriptedModel(
        [_text_stream("source answer"), _text_stream("destination answer")],
        [source_failure, "destination summary after image omission"],
    )
    model._model_runtime = source_runtime
    model._preparer = DirectKernelModelPort(model_runtime=source_runtime)
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            manual_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    notices: list[str] = []
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=(
            lambda: active_runtime[0].freeze_resolution_snapshot()
        ),
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
        presentation_notice_sink=notices.append,
    )
    switch_results: list[object] = []
    execute_switch = runner.compaction.recover_pending_root_input

    async def record_switch(**kwargs):
        result = await execute_switch(**kwargs)
        switch_results.append(result)
        return result

    runner.compaction.recover_pending_root_input = record_switch
    image_output = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(image_output, "PNG")
    image = LLMImagePart("image/png", image_output.getvalue(), 7, 5)
    image_prompt = freeze_canonical_prompt(
        FrozenPromptContent(
            (
                LLMTextPart("before"),
                image,
                LLMTextPart("between"),
                image,
            )
        )
    )

    async def exercise():
        first = await runner.run_turn(image_prompt)
        active_runtime[0] = destination_runtime
        model._model_runtime = destination_runtime
        model._preparer = DirectKernelModelPort(model_runtime=destination_runtime)
        repository.update_session_model_call_binding(
            lease.guard,
            binding=test_model_binding(destination_runtime),
            deadline_monotonic=monotonic() + 30,
        )
        second = await runner.run_turn(frozen_test_prompt("destination request"))
        await owner.aclose()
        return first, second

    first, second = asyncio.run(exercise())

    assert first.final_text == "source answer"
    assert second.final_text == "destination answer"
    assert len(switch_results) == 1
    assert switch_results[0].model_switch_tier == 3
    assert [call.target.fact.model_id for call in model.summary_transport.calls] == [
        "source-vision-model",
        "destination-text-model",
    ]
    destination_summary = model.summary_transport.contexts[1]
    assert not any(
        isinstance(part, LLMImagePart)
        for message in destination_summary.messages
        for part in message.content
    )
    assert (
        sum(
            join_text_content(message.content).count("[Image omitted]")
            for message in destination_summary.messages
        )
        == 2
    )
    assert not any(
        isinstance(part, LLMImagePart)
        for message in model.requests[-1].compiled_input.messages
        for part in message.content
    )
    successor = _context_snapshot_payload(model.requests[-1])
    assert not any(
        section["metadata"].get("section") == "recent"
        for section in successor["sections"]
    )
    assert notices == ["模型已切换。由于上下文长度变化，接下来的回答可能不如之前连贯。"]


def test_k3_visual_tier_three_preserves_selected_image_history_and_recent(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    source_runtime = test_model_runtime(
        model_id="source-vision-model",
        wire_api="openai_chat_completions",
        input_modalities=("text", "image"),
    )
    destination_runtime = test_model_runtime(
        model_id="destination-vision-model",
        wire_api="openai_chat_completions",
        connection_id=ModelConnectionId("model-connection:" + "2" * 32),
        limits=test_model_limits(
            total_context_tokens=256_000,
            # Keep room for the full English summary instructions and recent image;
            # the 50,000-character old answer must still exceed this budget.
            max_input_tokens=16_000,
            max_output_tokens=1_000,
            default_output_tokens=1_000,
            input_safety_margin_tokens=0,
        ),
        input_modalities=("text", "image"),
    )
    include_test_runtime_connections(destination_runtime, source_runtime)
    source_failure = [
        ProviderStreamTerminal(
            terminal_kind=ProviderNormalizedTerminalKind.PROVIDER_ERROR,
            usage=TransportUsageReport(usage_status="missing", usage=None),
            error=sanitize_provider_failure(
                message="source provider is unavailable",
                code_hint="503",
            ),
        )
    ]
    active_runtime = [source_runtime]
    model = _CompactionScriptedModel(
        [
            _text_stream("old large answer:" + "h" * 50_000),
            _text_stream("recent visual answer"),
            _text_stream("destination answer"),
        ],
        [source_failure, "destination summary preserving recent visual context"],
    )
    model._model_runtime = source_runtime
    model._preparer = DirectKernelModelPort(model_runtime=source_runtime)
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            manual_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=(
            lambda: active_runtime[0].freeze_resolution_snapshot()
        ),
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    switch_results: list[object] = []
    execute_switch = runner.compaction.recover_pending_root_input

    async def record_switch(**kwargs):
        result = await execute_switch(**kwargs)
        switch_results.append(result)
        return result

    runner.compaction.recover_pending_root_input = record_switch
    image_output = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(image_output, "PNG")
    image = LLMImagePart("image/png", image_output.getvalue(), 7, 5)

    async def exercise():
        await runner.run_turn(frozen_test_prompt("old large request"))
        await runner.run_turn(
            freeze_canonical_prompt(
                FrozenPromptContent((LLMTextPart("recent visual request"), image))
            )
        )
        active_runtime[0] = destination_runtime
        model._model_runtime = destination_runtime
        model._preparer = DirectKernelModelPort(model_runtime=destination_runtime)
        repository.update_session_model_call_binding(
            lease.guard,
            binding=test_model_binding(destination_runtime),
            deadline_monotonic=monotonic() + 30,
        )
        result = await runner.run_turn(frozen_test_prompt("destination request"))
        await owner.aclose()
        return result

    result = asyncio.run(exercise())

    assert result.final_text == "destination answer"
    assert len(switch_results) == 1
    assert switch_results[0].model_switch_tier == 3
    assert [call.target.fact.model_id for call in model.summary_transport.calls] == [
        "source-vision-model",
        "destination-vision-model",
    ]
    destination_summary = model.summary_transport.contexts[1]
    source_quote = model.summary_transport.contexts[0].provider_wire_input_plan.quote
    destination_quote = destination_summary.provider_wire_input_plan.quote
    assert source_quote.final_wire_estimated_input_tokens > (
        destination_quote.effective_input_budget_tokens
    )
    assert destination_quote.final_wire_estimated_input_tokens < int(
        destination_quote.effective_input_budget_tokens
        * owner.policy.auto_trigger_ratio
    )
    assert any(
        isinstance(part, LLMImagePart)
        for message in destination_summary.messages
        for part in message.content
    )
    successor = _context_snapshot_payload(model.requests[-1])
    recent_sections = [
        section
        for section in successor["sections"]
        if section["metadata"].get("section") == "recent"
    ]
    assert any(
        isinstance(part, LLMImagePart)
        for section in recent_sections
        for part in section["parts"]
    )
    assert any(
        isinstance(part, LLMImagePart)
        for message in model.requests[-1].compiled_input.messages
        for part in message.content
    )


@pytest.mark.parametrize(
    ("canonical_capacity_delta", "admitted"),
    ((0, True), (-1, False)),
    ids=("exact-headroom", "one-byte-short"),
)
def test_k3_multipart_input_keeps_exact_canonical_service_headroom_before_open(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
    canonical_capacity_delta: int,
    admitted: bool,
) -> None:
    from pulsara_agent.conversation_kernel import provider_dispatch

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    image_output = BytesIO()
    Image.new("RGB", (7, 5), (11, 23, 41)).save(image_output, "PNG")
    image = LLMImagePart("image/png", image_output.getvalue(), 7, 5)
    prompt = freeze_canonical_prompt(
        FrozenPromptContent((LLMTextPart("before"), image, image))
    )
    quote = prompt.resource_quote
    values = {
        "maximum_canonical_items": 4_096,
        "maximum_canonical_expanded_bytes": (
            quote.canonical_expanded_bytes + (4 << 20) + canonical_capacity_delta
        ),
        "maximum_epoch_logical_bytes": 64 << 20,
        "reserved_canonical_items": 296,
        "reserved_canonical_expanded_bytes": 4 << 20,
        "reserved_epoch_logical_bytes": 4 << 20,
    }
    bounds = ResolvedCompactionHeadroomBounds(
        **values,
        resolved_hard_bound_set_fingerprint=context_fingerprint(
            "pulsara.compaction-resource-headroom.v3-expanded-content",
            {"role": "minimum_service_headroom", **values},
        ),
    )
    monkeypatch.setattr(
        provider_dispatch,
        "resolved_compaction_headroom_bounds",
        lambda: bounds,
    )
    runtime = test_model_runtime(
        wire_api="openai_chat_completions",
        input_modalities=("text", "image"),
    )
    model = _ScriptedModel([_text_stream("answer")])
    model._model_runtime = runtime
    model._preparer = DirectKernelModelPort(model_runtime=runtime)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=runtime.freeze_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        workspace_id=workspace_id,
    )

    if admitted:
        result = asyncio.run(runner.run_turn(prompt))
        assert result.final_text == "answer"
        assert len(model.requests) == 1
        assert sum(
            len(part.immutable_bytes)
            for message in model.requests[0].compiled_input.messages
            for part in message.content
            if isinstance(part, LLMImagePart)
        ) == 2 * len(image.immutable_bytes)
    else:
        with pytest.raises(StructuredModelInputCompileError) as failure:
            asyncio.run(runner.run_turn(prompt))
        assert failure.value.kind is (
            ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED
        )
        assert model.requests == []
        assert not repository.rehydrate_session(
            session_id=session_id, deadline_monotonic=monotonic() + 30
        )
        with provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            assert connection.execute(
                "SELECT "
                "(SELECT count(*) FROM pulsara_v3.turns WHERE session_id = %s), "
                "(SELECT count(*) FROM pulsara_v3.session_commands WHERE session_id = %s), "
                "(SELECT latest_entry_sequence FROM pulsara_v3.sessions WHERE id = %s)",
                (session_id, session_id, session_id),
            ).fetchone() == (0, 0, 0)


def test_model_switch_connection_identity_forces_tier_one_cold_epoch(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    source_runtime = test_model_runtime(
        model_id="same-model",
        wire_api="openai_chat_completions",
        connection_id=ModelConnectionId("model-connection:" + "0" * 32),
    )
    destination_runtime = test_model_runtime(
        model_id="same-model",
        wire_api="openai_chat_completions",
        connection_id=ModelConnectionId("model-connection:" + "2" * 32),
    )
    include_test_runtime_connections(destination_runtime, source_runtime)
    active_runtime = [source_runtime]
    model = _CompactionScriptedModel(
        [_text_stream("source answer"), _text_stream("destination answer")],
        "unused summary",
    )
    model._model_runtime = source_runtime
    model._preparer = DirectKernelModelPort(model_runtime=source_runtime)
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(enabled=False, automatic_enabled=False)
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=(
            lambda: active_runtime[0].freeze_resolution_snapshot()
        ),
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    recorder = _RecordingColdEpochAssembler(
        runner._provider_dispatch._cold_epoch_assembler
    )
    runner._provider_dispatch._cold_epoch_assembler = recorder
    scope = ProviderInputContinuityScope(
        session_id=session_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )

    async def exercise():
        first = await runner.run_turn(frozen_test_prompt("source request"))
        source_epoch = runner._continuity.current_view(scope)
        assert source_epoch is not None
        active_runtime[0] = destination_runtime
        model._model_runtime = destination_runtime
        model._preparer = DirectKernelModelPort(model_runtime=destination_runtime)
        repository.update_session_model_call_binding(
            lease.guard,
            binding=test_model_binding(destination_runtime),
            deadline_monotonic=monotonic() + 30,
        )
        second = await runner.run_turn(frozen_test_prompt("destination request"))
        destination_epoch = runner._continuity.current_view(scope)
        assert destination_epoch is not None
        await owner.aclose()
        return first, second, source_epoch, destination_epoch

    first, second, source_epoch, destination_epoch = asyncio.run(exercise())

    assert first.final_text == "source answer"
    assert second.final_text == "destination answer"
    assert len(recorder.semantic_seeds) == 2
    assert all(
        isinstance(seed, CanonicalColdContinuationSeed)
        for seed in recorder.semantic_seeds
    )
    assert source_epoch.epoch_nonce != destination_epoch.epoch_nonce
    destination_cohort = runner._continuity.current_cohort(scope)
    assert destination_cohort is not None
    assert (
        destination_cohort.target_bundle.connection.connection_id
        == ModelConnectionId("model-connection:" + "2" * 32)
    )
    assert model.summary_transport.calls == []
    assert [
        request.prepared_call.call.binding.connection_id for request in model.requests
    ] == [
        ModelConnectionId("model-connection:" + "0" * 32),
        ModelConnectionId("model-connection:" + "2" * 32),
    ]


def test_reasoning_only_change_keeps_the_installed_epoch(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    runtime = test_model_runtime(
        model_id="reasoning-model",
        wire_api="openai_chat_completions",
        reasoning=ReasoningSelectableControls(
            effort=ReasoningEffortChoices(("low", "high"))
        ),
    )
    low = ModelCallBinding(
        ModelConnectionId("model-connection:" + "0" * 32),
        ReasoningEffortSelection("low"),
    )
    high = replace(low, reasoning=ReasoningEffortSelection("high"))
    repository.update_session_model_call_binding(
        lease.guard,
        binding=low,
        deadline_monotonic=monotonic() + 30,
    )
    model = _CompactionScriptedModel(
        [_text_stream("low answer"), _text_stream("high answer")],
        "unused summary",
    )
    model._model_runtime = runtime
    model._preparer = DirectKernelModelPort(model_runtime=runtime)
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(automatic_enabled=False)
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=runtime.freeze_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    scope = ProviderInputContinuityScope(
        session_id=session_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )

    async def exercise():
        first = await runner.run_turn(frozen_test_prompt("first request"))
        first_epoch = runner._continuity.current_view(scope)
        assert first_epoch is not None
        repository.update_session_model_call_binding(
            lease.guard,
            binding=high,
            deadline_monotonic=monotonic() + 30,
        )
        second = await runner.run_turn(frozen_test_prompt("second request"))
        second_epoch = runner._continuity.current_view(scope)
        assert second_epoch is not None
        await owner.aclose()
        return first, second, first_epoch, second_epoch

    first, second, first_epoch, second_epoch = asyncio.run(exercise())

    assert first.final_text == "low answer"
    assert second.final_text == "high answer"
    assert first_epoch.epoch_nonce == second_epoch.epoch_nonce
    assert first_epoch.epoch_revision + 1 == second_epoch.epoch_revision
    assert model.summary_transport.calls == []
    assert [request.prepared_call.call.binding for request in model.requests] == [
        low,
        high,
    ]


def test_disabled_compaction_rejects_nonfit_model_switch_without_mutation(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    source_runtime = test_model_runtime(
        model_id="source-model",
        wire_api="openai_chat_completions",
    )
    destination_runtime = test_model_runtime(
        model_id="destination-model",
        wire_api="openai_chat_completions",
        connection_id=ModelConnectionId("model-connection:" + "2" * 32),
        limits=test_model_limits(
            total_context_tokens=256_000,
            max_input_tokens=12_000,
            max_output_tokens=1_000,
            default_output_tokens=1_000,
            input_safety_margin_tokens=0,
        ),
    )
    active_runtime = [source_runtime]
    model = _CompactionScriptedModel(
        [_text_stream("large history:" + "h" * 50_000)],
        "summary must not run",
    )
    model._model_runtime = source_runtime
    model._preparer = DirectKernelModelPort(model_runtime=source_runtime)
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            enabled=False,
            automatic_enabled=False,
            manual_enabled=False,
            auto_trigger_ratio=0.85,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=(
            lambda: active_runtime[0].freeze_resolution_snapshot()
        ),
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    scope = ProviderInputContinuityScope(
        session_id=session_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )

    async def exercise():
        first = await runner.run_turn(frozen_test_prompt("source request"))
        source_epoch = runner._continuity.current_view(scope)
        assert source_epoch is not None
        active_runtime[0] = destination_runtime
        model._model_runtime = destination_runtime
        model._preparer = DirectKernelModelPort(model_runtime=destination_runtime)
        repository.update_session_model_call_binding(
            lease.guard,
            binding=test_model_binding(destination_runtime),
            deadline_monotonic=monotonic() + 30,
        )
        with pytest.raises(StructuredModelInputCompileError) as raised:
            await runner.run_turn(frozen_test_prompt("destination request"))
        current_epoch = runner._continuity.current_view(scope)
        await owner.aclose()
        return first, source_epoch, current_epoch, raised.value

    first, source_epoch, current_epoch, error = asyncio.run(exercise())

    assert first.final_text.startswith("large history:")
    assert error.kind is ModelInputCompileFailureKind.MODEL_SWITCH_REQUIRES_COMPACTION
    assert current_epoch is source_epoch
    assert len(model.requests) == 1
    assert model.summary_transport.calls == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        snapshot_count, adoption_count = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pulsara_v3.context_snapshots "
            " WHERE session_id = %s), "
            "(SELECT count(*) FROM pulsara_v3.agent_events "
            " WHERE session_id = %s AND event_type = 'CompactionAdopted')",
            (session_id, session_id),
        ).fetchone()
    assert (snapshot_count, adoption_count) == (0, 0)


@pytest.mark.parametrize(
    "usage_mode",
    ("reported_equal", "reported_different", "missing", "reported_cached"),
)
def test_final_wire_compaction_trigger_and_adoption_ignore_provider_usage(
    stage2_migrated_postgres_database,
    usage_mode: str,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _CompactionScriptedModel(
        [_text_stream("historical answer " + "x" * 80_000)],
        "The same concise handoff for every provider usage variant.",
        summary_usage_modes=[usage_mode],
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )

    async def exercise():
        first = await runner.run_turn(frozen_test_prompt("identical source question"))
        outcome = await runner.compaction.compact_idle_turn(
            turn_id=first.turn_id,
            command_id="command:usage-independent-compaction",
            force=True,
        )
        await owner.aclose()
        return outcome

    outcome = asyncio.run(exercise())

    assert outcome.disposition is CompactionDisposition.COMPACTED
    assert outcome.public_code == "COMPACTED"
    assert len(model.summary_transport.contexts) == 1
    assert len(model.summary_transport.usage_reports) == 1
    report = model.summary_transport.usage_reports[0]
    assert report.usage_status == ("missing" if usage_mode == "missing" else "reported")
    if usage_mode == "reported_cached":
        assert report.usage is not None
        assert report.usage.cached_input_tokens is not None
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        snapshot_count, event_count = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pulsara_v3.context_snapshots "
            " WHERE session_id = %s), "
            "(SELECT count(*) FROM pulsara_v3.agent_events "
            " WHERE session_id = %s AND event_type = 'CompactionAdopted')",
            (session_id, session_id),
        ).fetchone()
    assert (snapshot_count, event_count) == (1, 1)


@pytest.mark.parametrize(
    "api",
    ("openai_chat_completions", "openai_responses"),
    ids=("chat", "responses"),
)
def test_final_wire_compaction_summary_prefix_search_shrinks_replay_heavy_wire(
    stage2_migrated_postgres_database,
    api: str,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    limits = test_model_limits(
        total_context_tokens=256_000,
        max_input_tokens=47_192,
        max_output_tokens=1_000,
        default_output_tokens=1_000,
        input_safety_margin_tokens=0,
    )
    profile = RouteWireProfile(
        id=f"test:{api}:compaction-prefix-wire-search",
        wire_api=api,
    )
    model = _CompactionSequencedDirectKernelModel(
        model_runtime=test_model_runtime(
            api_key="sk-fixture-secret",
            base_url="https://example.invalid/v1",
            model_id="test-pro",
            wire_api=api,
            route_wire_profile=profile,
            limits=limits,
        ),
        scripts=(
            _large_native_replay_script(api, ordinal=1),
            _large_native_replay_script(api, ordinal=2),
        ),
        summary="A concise checkpoint after exact final-wire prefix admission.",
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    summary_measurements: list[tuple[int, object]] = []
    freeze_wire_measurement = model.freeze_wire_measurement

    def record_summary_measurement(**kwargs):
        measurement = freeze_wire_measurement(**kwargs)
        if kwargs["call"].fact.purpose is ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY:
            summary_measurements.append(
                (len(kwargs["semantic_input"].messages), measurement.quote)
            )
        return measurement

    model.freeze_wire_measurement = record_summary_measurement

    async def exercise():
        first = await runner.run_turn(frozen_test_prompt("first replay-bearing answer"))
        second = await runner.run_turn(
            frozen_test_prompt("second replay-bearing answer")
        )
        outcome = await runner.compaction.compact_idle_turn(
            turn_id=second.turn_id,
            command_id="command:replay-heavy-wire-prefix-search",
            force=True,
        )
        await owner.aclose()
        return first, second, outcome

    first, second, outcome = asyncio.run(exercise())

    assert first.final_text.startswith("native replay answer 1:")
    assert second.final_text.startswith("native replay answer 2:")
    assert outcome.disposition is CompactionDisposition.COMPACTED
    assert len(model.summary_transport.contexts) == 1
    assert len(summary_measurements) >= 2
    longest_count, longest_quote = summary_measurements[0]
    assert longest_quote.semantic_estimated_input_tokens <= (
        longest_quote.effective_input_budget_tokens
    )
    assert longest_quote.final_wire_estimated_input_tokens > (
        longest_quote.effective_input_budget_tokens
    )
    assert longest_quote.replaced_generic_wire_estimated_tokens > 0
    assert longest_quote.replay_wire_estimated_tokens > 0
    admitted_count, admitted_quote = next(
        item
        for item in summary_measurements
        if item[1].final_wire_estimated_input_tokens
        <= item[1].effective_input_budget_tokens
    )
    assert admitted_count < longest_count
    assert admitted_quote.replaced_generic_wire_estimated_tokens > 0
    assert admitted_quote.replay_wire_estimated_tokens > 0
    selected_plan = model.summary_transport.contexts[0].provider_wire_input_plan
    assert selected_plan.quote == admitted_quote
    assert selected_plan.quote.final_wire_estimated_input_tokens <= (
        selected_plan.quote.effective_input_budget_tokens
    )


@pytest.mark.parametrize(
    "api",
    ("openai_chat_completions", "openai_responses"),
    ids=("chat", "responses"),
)
@pytest.mark.parametrize(
    ("image_side", "text_padding", "expected_disposition"),
    (
        (1024, 0, CompactionDisposition.COMPACTED),
        (2047, 4_000, CompactionDisposition.FAILED),
    ),
    ids=("large-image-admitted", "full-d2-overbound"),
)
def test_k4_compaction_prefix_search_keeps_large_image_mandatory_suffix(
    stage2_migrated_postgres_database,
    api: str,
    image_side: int,
    text_padding: int,
    expected_disposition: CompactionDisposition,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=120,
        deadline_monotonic=monotonic() + 30,
    )
    limits = test_model_limits(
        total_context_tokens=256_000,
        max_input_tokens=47_192,
        max_output_tokens=1_000,
        default_output_tokens=1_000,
        input_safety_margin_tokens=0,
    )
    profile = RouteWireProfile(
        id=f"test:{api}:large-image-compaction-tail",
        wire_api=api,
    )
    final_script = (
        _round5a1_chat_scripts()[1]
        if api == "openai_chat_completions"
        else _round5a1_responses_scripts()[1]
    )
    model = _CompactionSequencedDirectKernelModel(
        model_runtime=test_model_runtime(
            api_key="sk-fixture-secret",
            base_url="https://example.invalid/v1",
            model_id="test-pro",
            wire_api=api,
            route_wire_profile=profile,
            limits=limits,
            input_modalities=("text", "image"),
        ),
        scripts=(
            _large_native_replay_script(api, ordinal=1),
            _large_native_replay_script(api, ordinal=2),
            final_script,
        ),
        summary="A concise checkpoint before the large visual suffix.",
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=True,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    image_output = BytesIO()
    Image.new("RGB", (image_side, image_side), (11, 23, 41)).save(
        image_output, "PNG", compress_level=0
    )
    image = LLMImagePart("image/png", image_output.getvalue(), image_side, image_side)
    assert len(image.immutable_bytes) > 2 << 20
    visual_prompt = freeze_canonical_prompt(
        FrozenPromptContent(
            (
                LLMTextPart("visual suffix" + "x" * text_padding),
                image,
            )
        )
    )
    summary_measurements: list[tuple[int, bool, object]] = []
    freeze_wire_measurement = model.freeze_wire_measurement

    def record_summary_measurement(**kwargs):
        measurement = freeze_wire_measurement(**kwargs)
        if kwargs["call"].fact.purpose is ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY:
            messages = kwargs["semantic_input"].messages
            summary_measurements.append(
                (
                    len(messages),
                    any(
                        isinstance(part, LLMImagePart)
                        for message in messages
                        for part in message.content
                    ),
                    measurement.quote,
                )
            )
        return measurement

    model.freeze_wire_measurement = record_summary_measurement

    async def exercise():
        await runner.run_turn(frozen_test_prompt("first replay-bearing answer"))
        await runner.run_turn(frozen_test_prompt("second replay-bearing answer"))
        result = None
        failure = None
        try:
            result = await runner.run_turn(visual_prompt)
        except StructuredModelInputCompileError as error:
            failure = error
        await owner.aclose()
        return result, failure

    result, failure = asyncio.run(exercise())

    assert len(summary_measurements) >= 2
    longest_count, longest_has_image, longest_quote = summary_measurements[0]
    assert not longest_has_image
    assert longest_quote.final_wire_estimated_input_tokens > (
        longest_quote.effective_input_budget_tokens
    )
    admitted_count, admitted_has_image, admitted_quote = next(
        item
        for item in summary_measurements
        if item[2].final_wire_estimated_input_tokens
        <= item[2].effective_input_budget_tokens
    )
    assert admitted_count < longest_count
    assert not admitted_has_image
    assert admitted_quote.final_wire_estimated_input_tokens <= (
        admitted_quote.effective_input_budget_tokens
    )
    # The selected summary prefix ends before the visual USER.  Its immutable
    # bytes therefore remain in the mandatory canonical suffix and are checked
    # by the real dry successor compiler/materializer rather than the 2 MiB
    # descriptor-only retained-tail policy.
    assert not any(
        isinstance(part, LLMImagePart)
        for message in model.summary_transport.contexts[0].messages
        for part in message.content
    )
    assert not any(
        isinstance(part, LLMImagePart)
        for message in model.requests[1].compiled_input.messages
        for part in message.content
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        snapshot_count, adoption_count, image_ref_count = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pulsara_v3.context_snapshots "
            " WHERE session_id = %s), "
            "(SELECT count(*) FROM pulsara_v3.agent_events "
            " WHERE session_id = %s AND event_type = 'CompactionAdopted'), "
            "(SELECT count(*) FROM pulsara_v3.canonical_image_refs "
            " WHERE session_id = %s)",
            (session_id, session_id, session_id),
        ).fetchone()
    expected_count = 1 if expected_disposition is CompactionDisposition.COMPACTED else 0
    assert (snapshot_count, adoption_count, image_ref_count) == (
        expected_count,
        expected_count,
        expected_count,
    )
    if expected_disposition is CompactionDisposition.COMPACTED:
        assert failure is None
        assert result is not None
        assert result.final_text in {"chat final", "responses final"}
        assert (
            sum(
                isinstance(part, LLMImagePart)
                for message in model.requests[2].compiled_input.messages
                for part in message.content
            )
            == 1
        )
    else:
        assert result is None
        assert failure is not None
        assert (
            failure.kind is ModelInputCompileFailureKind.SOURCE_PHYSICAL_BOUND_EXCEEDED
        )
        assert len(model.requests) == 2


def test_final_wire_compaction_summary_promotes_semantic_overbudget_replay_fit(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulsara_agent.conversation_kernel.compaction import (
        coordinator as compaction_coordinator,
    )
    from pulsara_agent.conversation_kernel.compaction import (
        model_call as compaction_model_call,
    )

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    limits = test_model_limits(
        total_context_tokens=256_000,
        max_input_tokens=47_192,
        max_output_tokens=1_000,
        default_output_tokens=1_000,
        input_safety_margin_tokens=0,
    )
    profile = RouteWireProfile(
        id="test:chat:compaction-semantic-overbudget-wire-fit",
        wire_api="openai_chat_completions",
    )
    model = _CompactionSequencedDirectKernelModel(
        model_runtime=test_model_runtime(
            api_key="sk-fixture-secret",
            base_url="https://example.invalid/v1",
            model_id="test-pro",
            wire_api="openai_chat_completions",
            route_wire_profile=profile,
            limits=limits,
        ),
        scripts=(
            _large_native_replay_script("openai_chat_completions", ordinal=1),
            _round5a1_chat_scripts()[1],
        ),
        summary="A concise checkpoint from a wire-fit summary carrier.",
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    original_prepare = compaction_model_call.prepare_compaction_summary_semantic
    prepared_semantics: list[object] = []
    estimator_patched = False

    def prepare_semantic_overbudget(**kwargs):
        nonlocal estimator_patched
        estimator = kwargs["source_view"].normal_compile_binding.estimator
        if not estimator_patched:
            estimate_frozen_input = estimator.estimate_frozen_input

            def inflate_summary_semantic_estimate(*, system_prompt, messages, tools):
                estimate = estimate_frozen_input(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tools,
                )
                summary_request = kwargs["summary_request"]
                if (
                    not messages
                    or messages[-1].role is not MessageRole.USER
                    or messages[-1].content != (LLMTextPart(summary_request),)
                ):
                    return estimate
                addend = 100_000
                by_index = list(estimate.message_tokens_by_index)
                by_index[-1] += addend
                return replace(
                    estimate,
                    message_tokens=estimate.message_tokens + addend,
                    message_tokens_by_index=tuple(by_index),
                    total_input_tokens=estimate.total_input_tokens + addend,
                )

            monkeypatch.setattr(
                estimator,
                "estimate_frozen_input",
                inflate_summary_semantic_estimate,
            )
            estimator_patched = True
        semantic = original_prepare(**kwargs)
        prepared_semantics.append(semantic)
        return semantic

    monkeypatch.setattr(
        compaction_coordinator,
        "prepare_compaction_summary_semantic",
        prepare_semantic_overbudget,
    )

    async def exercise():
        await runner.run_turn(
            frozen_test_prompt("create a replay-bearing assistant answer")
        )
        second = await runner.run_turn(
            frozen_test_prompt("freeze an ordinary replay input")
        )
        outcome = await runner.compaction.compact_idle_turn(
            turn_id=second.turn_id,
            command_id="command:semantic-overbudget-wire-fit",
            force=True,
        )
        await owner.aclose()
        return outcome

    outcome = asyncio.run(exercise())

    assert outcome.disposition is CompactionDisposition.COMPACTED
    assert len(prepared_semantics) == 1
    semantic = prepared_semantics[0]
    selected_plan = model.summary_transport.contexts[0].provider_wire_input_plan
    assert semantic.semantic_input.final_estimate.total_input_tokens > (
        selected_plan.quote.effective_input_budget_tokens
    )
    assert selected_plan.quote.semantic_estimated_input_tokens == (
        semantic.semantic_input.final_estimate.total_input_tokens
    )
    assert selected_plan.quote.final_wire_estimated_input_tokens <= (
        selected_plan.quote.effective_input_budget_tokens
    )
    assert selected_plan.quote.replaced_generic_wire_estimated_tokens > 0
    assert selected_plan.quote.replay_wire_estimated_tokens > 0


@pytest.mark.parametrize("trigger", ("manual", "automatic"))
def test_final_wire_compaction_no_executable_summary_prefix_is_not_already_compact(
    stage2_migrated_postgres_database,
    trigger: str,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    limits = test_model_limits(
        total_context_tokens=256_000,
        max_input_tokens=9_192,
        max_output_tokens=200,
        default_output_tokens=200,
        input_safety_margin_tokens=0,
    )
    first_text = "history:" + "h" * 400
    scripts = (
        (
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": first_text},
                        "finish_reason": "stop",
                    }
                ]
            },
        ),
        _round5a1_chat_scripts()[1],
    )
    model = _CompactionSequencedDirectKernelModel(
        model_runtime=test_model_runtime(
            api_key="sk-fixture-secret",
            base_url="https://example.invalid/v1",
            model_id="test-pro",
            wire_api="openai_chat_completions",
            limits=limits,
        ),
        scripts=scripts,
        summary="This summary transport must never open.",
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=trigger == "automatic",
            auto_trigger_ratio=0.75,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    automatic_executions: list[object] = []
    execute_pending = runner.compaction._execute_compaction_fenced

    async def record_automatic_execution(**kwargs):
        execution = await execute_pending(**kwargs)
        automatic_executions.append(execution)
        return execution

    if trigger == "automatic":
        runner.compaction._execute_compaction_fenced = record_automatic_execution

    async def exercise():
        first = await runner.run_turn(frozen_test_prompt("first input"))
        if trigger == "manual":
            outcome = await runner.compaction.compact_idle_turn(
                turn_id=first.turn_id,
                command_id="command:no-executable-summary-prefix",
                force=True,
            )
            second_text = None
        else:
            second = await runner.run_turn(
                frozen_test_prompt("second input triggers compaction")
            )
            outcome = automatic_executions[0].outcome
            second_text = second.final_text
        await owner.aclose()
        return outcome, second_text

    outcome, second_text = asyncio.run(exercise())

    assert outcome.disposition is CompactionDisposition.FAILED
    assert outcome.public_code == "NO_EXECUTABLE_SUMMARY_PREFIX"
    assert model.summary_transport.contexts == []
    assert second_text == (None if trigger == "manual" else "chat final")
    assert len(automatic_executions) == (0 if trigger == "manual" else 1)
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        snapshot_count, event_count = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pulsara_v3.context_snapshots "
            " WHERE session_id = %s), "
            "(SELECT count(*) FROM pulsara_v3.agent_events "
            " WHERE session_id = %s AND event_type = 'CompactionAdopted')",
            (session_id, session_id),
        ).fetchone()
    assert (snapshot_count, event_count) == (0, 0)


@pytest.mark.parametrize("idle", (False, True), ids=("active", "idle"))
def test_round5b_manual_candidate_shrink_search_is_lifecycle_neutral(
    idle: bool,
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    normal_calls = [
        _text_stream("historical answer " + "x" * 80_000),
        _tool_stream(),
        _text_stream("tool turn complete"),
    ]
    if not idle:
        normal_calls.append(_text_stream("active turn continued"))
    model = _CompactionScriptedModel(
        normal_calls,
        [
            "first compact handoff whose successor quote is rejected",
            "compact handoff after shrinking the retained tool tail",
        ],
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
            maximum_retained_tool_groups=1,
        )
    )
    tools = _RecordingBorrowToolPort(
        _AssertingTool(provider, session_id), tool_names=("terminal",)
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=tools,
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    dispatch_pre_compact = runner.compaction._dispatch_pre_compact
    measure_wire = runner.compaction._provider_dispatch.measure_prepared_wire_candidate
    pre_compact_calls = 0
    rejected_successor_quotes = []
    measured_snapshot_successors = []

    async def record_pre_compact(**kwargs):
        nonlocal pre_compact_calls
        pre_compact_calls += 1
        return await dispatch_pre_compact(**kwargs)

    async def reject_first_successor_wire(candidate, *, deadline, **kwargs):
        decision = await measure_wire(candidate, deadline=deadline, **kwargs)
        canonical_read = getattr(candidate, "canonical_read", None)
        binding = (
            None
            if canonical_read is None
            else canonical_read.compile_snapshot.context_binding_fact
        )
        if binding is not None and binding.base_kind is ContextBindingBaseKind.SNAPSHOT:
            measured_snapshot_successors.append(decision.quote)
        if not rejected_successor_quotes and measured_snapshot_successors:
            quote = decision.quote
            over_budget = max(
                quote.effective_input_budget_tokens + 1,
                quote.replay_wire_estimated_tokens + 1,
            )
            rejected_quote = replace(
                quote,
                generic_wire_estimated_input_tokens=(
                    over_budget
                    + quote.replaced_generic_wire_estimated_tokens
                    - quote.replay_wire_estimated_tokens
                ),
                final_wire_estimated_input_tokens=over_budget,
            )
            rejected_successor_quotes.append(rejected_quote)
            return PreparedWireMeasurementDecision(
                candidate=decision.candidate,
                quote=rejected_quote,
                wire_input_plan=None,
            )
        return decision

    monkeypatch.setattr(
        runner.compaction,
        "_dispatch_pre_compact",
        record_pre_compact,
    )
    monkeypatch.setattr(
        runner.compaction._provider_dispatch,
        "measure_prepared_wire_candidate",
        reject_first_successor_wire,
    )

    async def exercise():
        nonlocal runner, dispatch_pre_compact, measure_wire
        await runner.run_turn(frozen_test_prompt("historical question"))
        tool_turn = await runner.run_turn(
            frozen_test_prompt("create one complete tool group")
        )
        # Exercise the candidate-shrink algorithm after a real cold Host
        # takeover.  The removed discard_scope shortcut is not an approved
        # provider-input rebase boundary.
        runner._continuity.close()
        cold_lease = _acquire_bound_host_writer(
            repository,
            session_id=session_id,
            workspace_id=workspace_id,
            writer_owner_id=_name("cold-host"),
            lease_seconds=30,
            deadline_monotonic=monotonic() + 30,
        )
        runner = ConversationKernelRunner(
            model_resolution_snapshot_provider=test_model_resolution_snapshot,
            repository=repository,
            writer_lease=cold_lease,
            model=model,
            tools=tools,
            live_bus=LiveAgentEventBus(),
            context_source_collector=StaticContextSourceCollector(),
            compaction_owner=owner,
            workspace_id=workspace_id,
        )
        dispatch_pre_compact = runner.compaction._dispatch_pre_compact
        measure_wire = (
            runner.compaction._provider_dispatch.measure_prepared_wire_candidate
        )
        monkeypatch.setattr(
            runner.compaction,
            "_dispatch_pre_compact",
            record_pre_compact,
        )
        monkeypatch.setattr(
            runner.compaction._provider_dispatch,
            "measure_prepared_wire_candidate",
            reject_first_successor_wire,
        )
        if idle:
            outcome = await runner.compaction.compact_idle_turn(
                turn_id=tool_turn.turn_id,
                command_id=_name("idle-compact"),
                force=True,
            )
            final_text = None
        else:
            command_id = _name("active-command")
            turn_id = _stable_id("turn", session_id, command_id)
            _request, waiter = await owner.request_manual(
                command_id=_name("active-compact"),
                scope_kind=ModelInputScopeKind.ROOT,
                scope_subagent_task_id=None,
                expected_turn_id=turn_id,
                force=True,
            )
            active_turn = await runner.run_turn(
                frozen_test_prompt("continue actively"), command_id=command_id
            )
            outcome = await waiter
            final_text = active_turn.final_text
        await owner.aclose()
        return outcome, final_text

    outcome, final_text = asyncio.run(exercise())

    assert outcome.disposition is CompactionDisposition.COMPACTED
    assert outcome.snapshot_id is not None
    # K3 freezes one summary and retries only the local recent suffixes before
    # restarting with a smaller protected tail.  Rejecting the first successor
    # must therefore measure a second candidate without another model call.
    assert len(model.summary_transport.contexts) == 1
    assert len(measured_snapshot_successors) >= 2
    assert {
        context.messages[-1].content for context in model.summary_transport.contexts
    } == {(LLMTextPart(compaction_summary_request()),)}
    assert final_text == (None if idle else "active turn continued")
    assert pre_compact_calls == 1
    assert runner._safe_point._active_handle is None  # noqa: SLF001
    assert tools._active == set()  # noqa: SLF001
    assert tools.release_calls
    assert all(count == 1 for count in tools.release_calls)
    assert len(rejected_successor_quotes) == 1
    assert rejected_successor_quotes[0].final_wire_estimated_input_tokens > (
        rejected_successor_quotes[0].effective_input_budget_tokens
    )


@pytest.mark.parametrize("retained_group_count", (0, 1))
def test_final_wire_pre_full_drift_replans_fresh_without_shrinking_tail(
    retained_group_count: int,
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulsara_agent.conversation_kernel.compaction import (
        coordinator as compaction_coordinator,
    )

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    normal_calls = [_text_stream("historical answer " + "x" * 80_000)]
    if retained_group_count:
        normal_calls.extend((_tool_stream(), _text_stream("tool turn complete")))
    normal_calls.append(_text_stream("active turn after fresh compaction replan"))
    discarded_summary = "summary from the structurally drifted source"
    selected_summary = "summary from the fresh exact source"
    model = _CompactionScriptedModel(
        normal_calls,
        [discarded_summary, selected_summary],
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
            maximum_retained_tool_groups=max(1, retained_group_count),
        )
    )
    tools = _RecordingBorrowToolPort(
        _AssertingTool(provider, session_id),
        tool_names=("terminal",) if retained_group_count else (),
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=tools,
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    transition = compaction_coordinator.validate_compaction_wire_transition
    reclaim = compaction_coordinator.validate_compaction_reclaim
    dispatch_pre_compact = runner.compaction._dispatch_pre_compact
    pre_full_attempts = 0
    numeric_reclaim_calls = 0
    numeric_calls_at_drift: list[int] = []
    pre_compact_calls = 0

    async def record_pre_compact(**kwargs):
        nonlocal pre_compact_calls
        pre_compact_calls += 1
        return await dispatch_pre_compact(**kwargs)

    def record_numeric_reclaim(**kwargs):
        nonlocal numeric_reclaim_calls
        numeric_reclaim_calls += 1
        return reclaim(**kwargs)

    def drift_first_pre_full_transition(**kwargs):
        nonlocal pre_full_attempts
        if kwargs["phase"] == "PRE_FULL":
            pre_full_attempts += 1
            if pre_full_attempts == 1:
                numeric_calls_at_drift.append(numeric_reclaim_calls)
                raise compaction_coordinator.CompactionWireTransitionDrift(
                    "injected PRE_FULL structural drift"
                )
        return transition(**kwargs)

    monkeypatch.setattr(
        compaction_coordinator,
        "validate_compaction_reclaim",
        record_numeric_reclaim,
    )
    monkeypatch.setattr(
        compaction_coordinator,
        "validate_compaction_wire_transition",
        drift_first_pre_full_transition,
    )
    monkeypatch.setattr(
        runner.compaction,
        "_dispatch_pre_compact",
        record_pre_compact,
    )

    async def exercise():
        await runner.run_turn(frozen_test_prompt("historical question"))
        if retained_group_count:
            await runner.run_turn(
                frozen_test_prompt("create one complete retained tool group")
            )
        command_id = _name("active-command")
        turn_id = _stable_id("turn", session_id, command_id)
        _request, waiter = await owner.request_manual(
            command_id=_name("active-compact"),
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id=turn_id,
            force=True,
        )
        result = await runner.run_turn(
            frozen_test_prompt("continue actively"), command_id=command_id
        )
        outcome = await waiter
        await owner.aclose()
        return result, outcome

    result, outcome = asyncio.run(exercise())

    assert result.final_text == "active turn after fresh compaction replan"
    assert outcome.disposition is CompactionDisposition.COMPACTED
    assert pre_full_attempts == 2
    assert numeric_calls_at_drift == [0]
    assert numeric_reclaim_calls == 2
    assert len(model.summary_transport.contexts) == 2
    assert len(model.summary_transport.contexts[0].messages) == len(
        model.summary_transport.contexts[1].messages
    )
    successor_snapshot = _context_snapshot_payload(model.requests[-1])
    assert successor_snapshot["earlier_context_summary"] == selected_summary
    assert successor_snapshot["earlier_context_summary"] != discarded_summary
    assert pre_compact_calls == 1
    assert runner._safe_point._active_handle is None  # noqa: SLF001
    assert tools._active == set()  # noqa: SLF001
    assert tools.release_calls
    assert all(count == 1 for count in tools.release_calls)


def test_round5b_active_manual_non_reclaim_is_not_needed_and_turn_continues(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _CompactionScriptedModel(
        [
            _text_stream("first answer " + "x" * 80_000),
            _text_stream("second turn continued"),
        ],
        "oversized handoff " + "x" * 200_000,
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )

    async def exercise():
        await runner.run_turn(frozen_test_prompt("first question"))
        command_id = _name("second-command")
        turn_id = _stable_id("turn", session_id, command_id)
        _request, waiter = await owner.request_manual(
            command_id=_name("compact-command"),
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id=turn_id,
            force=True,
        )
        second = await runner.run_turn(
            frozen_test_prompt("second question"), command_id=command_id
        )
        outcome = await waiter
        await owner.aclose()
        return second, outcome

    second, outcome = asyncio.run(exercise())

    assert second.final_text == "second turn continued"
    assert outcome.disposition is CompactionDisposition.NOT_NEEDED
    assert outcome.public_code == "CONTEXT_ALREADY_COMPACT"
    assert outcome.snapshot_id is None
    assert len(model.summary_transport.contexts) == 1
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        snapshot_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.context_snapshots WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
    assert snapshot_count == 0


def test_round5b_back_to_back_manual_request_cannot_overwrite_successor(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _CompactionScriptedModel(
        [
            _text_stream("historical answer " + "x" * 80_000),
            _text_stream("successor was consumed exactly once"),
        ],
        "A concise handoff for the first manual compaction.",
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    installed_provider_inputs: list[object] = []
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
        provider_input_installed_observer=installed_provider_inputs.append,
    )
    captured_successors: list[object] = []
    second_waiters: list[asyncio.Future[object]] = []

    async def exercise():
        await runner.run_turn(frozen_test_prompt("first question"))
        command_id = _name("second-command")
        turn_id = _stable_id("turn", session_id, command_id)
        first_request, first_waiter = await owner.request_manual(
            command_id=_name("first-compact-command"),
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id=turn_id,
            force=True,
        )
        execute_active = runner.compaction.execute_active

        async def inject_second_after_first_settlement(**kwargs):
            execution = await execute_active(**kwargs)
            manual_request = kwargs["manual_request"]
            if (
                manual_request is not None
                and manual_request.request_id == first_request.request_id
            ):
                assert execution.successor_dispatch is not None
                captured_successors.append(execution.successor_dispatch)
                _second_request, second_waiter = await owner.request_manual(
                    command_id=_name("second-compact-command"),
                    scope_kind=ModelInputScopeKind.ROOT,
                    scope_subagent_task_id=None,
                    expected_turn_id=turn_id,
                    force=True,
                )
                second_waiters.append(second_waiter)
            return execution

        runner.compaction.execute_active = inject_second_after_first_settlement
        try:
            result = await runner.run_turn(
                frozen_test_prompt("second question"), command_id=command_id
            )
            first_outcome = await first_waiter
            assert len(second_waiters) == 1
            assert not second_waiters[0].done()
        finally:
            await owner.aclose()
        deferred_outcome = await second_waiters[0]
        return result, first_outcome, deferred_outcome

    result, first_outcome, deferred_outcome = asyncio.run(exercise())

    assert result.final_text == "successor was consumed exactly once"
    assert first_outcome.disposition is CompactionDisposition.COMPACTED
    assert deferred_outcome.public_code == "HOST_CLOSING"
    assert len(model.summary_transport.contexts) == 1
    assert len(model.requests) == 2
    assert installed_provider_inputs == model.requests
    assert len(captured_successors) == 1
    captured = captured_successors[0]
    assert not captured.owns_execution_authority
    with pytest.raises(RuntimeError, match="authority is consumed"):
        captured.take_execution_authority()
    with pytest.raises(RuntimeError, match="authority is consumed"):
        captured.close()


def test_round5b_summary_tool_call_gets_one_ephemeral_repair_and_no_dispatch(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    summary = "A concise free-form handoff after the denied tool call."
    model = _CompactionScriptedModel(
        [
            _text_stream("historical answer " + "x" * 80_000),
            _text_stream("final after repaired compaction"),
        ],
        [_summary_tool_stream("summary-call:1"), summary],
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    physical_tool = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(physical_tool, tool_names=("terminal",)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )

    async def exercise():
        await runner.run_turn(frozen_test_prompt("first question"))
        command_id = _name("second-command")
        turn_id = _stable_id("turn", session_id, command_id)
        _request, outcome = await owner.request_manual(
            command_id=_name("compact-command"),
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id=turn_id,
            force=True,
        )
        result = await runner.run_turn(
            frozen_test_prompt("second question"), command_id=command_id
        )
        compacted = await outcome
        await owner.aclose()
        return result, compacted

    result, compacted = asyncio.run(exercise())

    assert result.final_text == "final after repaired compaction"
    assert compacted.disposition is CompactionDisposition.COMPACTED
    assert len(model.summary_transport.contexts) == 2
    assert all(
        context.tool_choice == "auto" for context in model.summary_transport.contexts
    )
    assert len(model.summary_transport.contexts[1].messages) > len(
        model.summary_transport.contexts[0].messages
    )
    assert physical_tool.invocations == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        attempt_count, tool_result_count = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pulsara_v3.tool_execution_attempts "
            " WHERE session_id = %s), "
            "(SELECT count(*) FROM pulsara_v3.transcript_entries "
            " WHERE session_id = %s AND entry_kind = 'TOOL_RESULT')",
            (session_id, session_id),
        ).fetchone()
    assert attempt_count == 0
    assert tool_result_count == 0


def test_round5b_second_summary_tool_call_discards_without_canonical_effect(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _CompactionScriptedModel(
        [
            _text_stream("historical answer " + "x" * 80_000),
            _text_stream("normal call survives failed compaction"),
        ],
        [
            _summary_tool_stream("summary-call:1"),
            _summary_tool_stream("summary-call:2"),
        ],
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    physical_tool = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(physical_tool, tool_names=("terminal",)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )

    async def exercise():
        await runner.run_turn(frozen_test_prompt("first question"))
        command_id = _name("second-command")
        turn_id = _stable_id("turn", session_id, command_id)
        _request, outcome = await owner.request_manual(
            command_id=_name("compact-command"),
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id=turn_id,
            force=True,
        )
        result = await runner.run_turn(
            frozen_test_prompt("second question"), command_id=command_id
        )
        failed = await outcome
        await owner.aclose()
        return result, failed

    result, failed = asyncio.run(exercise())

    assert result.final_text == "normal call survives failed compaction"
    assert failed.disposition is CompactionDisposition.FAILED
    assert len(model.summary_transport.contexts) == 2
    assert physical_tool.invocations == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        snapshot_count, attempt_count = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pulsara_v3.context_snapshots "
            " WHERE session_id = %s), "
            "(SELECT count(*) FROM pulsara_v3.tool_execution_attempts "
            " WHERE session_id = %s)",
            (session_id, session_id),
        ).fetchone()
    assert snapshot_count == 0
    assert attempt_count == 0


def test_round5b_cancelled_manual_summary_settles_detached_waiter(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _CompactionScriptedModel(
        [_text_stream("historical answer " + "x" * 80_000)],
        "unused",
    )
    blocking = _BlockingCompactionSummaryTransport()
    model.summary_transport = blocking
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )

    async def exercise():
        await runner.run_turn(frozen_test_prompt("first question"))
        command_id = _name("second-command")
        turn_id = _stable_id("turn", session_id, command_id)
        _request, outcome = await owner.request_manual(
            command_id=_name("compact-command"),
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id=turn_id,
            force=True,
        )
        task = asyncio.create_task(
            runner.run_turn(
                frozen_test_prompt("second question"), command_id=command_id
            )
        )
        await asyncio.wait_for(blocking.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        compacted = await asyncio.wait_for(outcome, timeout=1)
        blocking.release.set()
        await owner.aclose()
        return compacted

    compacted = asyncio.run(exercise())

    assert compacted.disposition is CompactionDisposition.FAILED
    assert compacted.public_code == "COMPACTION_CANCELLED"


def test_round5b_mid_turn_tool_followup_compacts_then_finishes(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    summary = "A concise free-form handoff for the mid-turn continuation."
    first_response = (
        _text_stream(
            "accepted assistant context " + "a" * 460_000,
            block="text:large-before-tool",
        )
        + _tool_stream()
    )
    model = _LimitedCompactionScriptedModel(
        [first_response, _text_stream("finished after mid-turn compaction")],
        summary,
        total_context_tokens=360_000,
        max_input_tokens=330_000,
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            auto_trigger_ratio=0.75,
            post_compaction_target_ratio=0.60,
            minimum_reclaim_tokens=1,
            maximum_retained_tail_utf8_bytes=1,
        )
    )
    tool = _ThirtyKiBTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tool, tool_names=("terminal",)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    triggers: list[object] = []
    execute = runner.compaction.execute_active

    async def record_trigger(**kwargs):
        triggers.append(kwargs["trigger"])
        return await execute(**kwargs)

    runner.compaction.execute_active = record_trigger

    async def exercise():
        # K3 first admits the worst legal result wire before executing the tool.
        # The large actual assistant then makes the settled follow-up cross the
        # configured soft trigger while its compacted successor remains executable.
        result = await runner.run_turn(frozen_test_prompt("p" * 40_000))
        await owner.aclose()
        return result

    result = asyncio.run(exercise())

    assert result.final_text == "finished after mid-turn compaction"
    assert result.model_call_count == 2
    assert result.tool_call_count == 1
    assert triggers == [CompactionTrigger.MID_TURN_FOLLOWUP]
    assert len(model.summary_transport.contexts) == 1
    successor_snapshot = _context_snapshot_payload(model.requests[-1])
    assert successor_snapshot["continuation"]["mode"] == "RESUME_ACTIVE_TURN"
    assert successor_snapshot["continuation"]["active_request"]["text"] == (
        "p" * 40_000
    )
    assert len(tool.invocations) == 1
    assert not any(
        message.role is MessageRole.TOOL_RESULT
        for message in model.requests[-1].compiled_input.messages
    )


def test_final_wire_below_trigger_compaction_precheck_reuses_one_materialization(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("ordinary final-wire dispatch")])
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(minimum_reclaim_tokens=1)
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    measurements = 0
    hydrations = 0
    source_preparations = 0
    installs = 0
    fences = 0
    freeze_measurement = model.freeze_wire_measurement
    reader = runner._provider_dispatch._input_reader
    hydrate = reader.hydrate_selected_provider_replays
    prepare_source = runner._provider_dispatch.prepare_compaction_source
    install = runner._provider_dispatch.install_provider_open
    run_fenced = owner.run_fenced

    def record_measurement(**kwargs):
        nonlocal measurements
        measurements += 1
        return freeze_measurement(**kwargs)

    def record_hydration(**kwargs):
        nonlocal hydrations
        hydrations += 1
        return hydrate(**kwargs)

    async def record_source(**kwargs):
        nonlocal source_preparations
        source_preparations += 1
        return await prepare_source(**kwargs)

    async def record_install(*args, **kwargs):
        nonlocal installs
        installs += 1
        return await install(*args, **kwargs)

    async def record_fence(*args, **kwargs):
        nonlocal fences
        fences += 1
        return await run_fenced(*args, **kwargs)

    model.freeze_wire_measurement = record_measurement
    reader.hydrate_selected_provider_replays = record_hydration
    runner._provider_dispatch.prepare_compaction_source = record_source
    runner._provider_dispatch.install_provider_open = record_install
    owner.run_fenced = record_fence

    async def exercise():
        result = await runner.run_turn(frozen_test_prompt("small ordinary request"))
        await owner.aclose()
        return result

    result = asyncio.run(exercise())

    assert result.final_text == "ordinary final-wire dispatch"
    assert measurements == 1
    assert hydrations == 1
    # A valid unpublished ROOT candidate already carries the exact measured
    # successor.  The below-trigger case must not reopen the compaction source.
    assert source_preparations == 0
    assert installs == 1
    assert fences == 0
    assert len(model.requests) == 1


def test_final_wire_successful_install_is_not_rejected_by_post_cas_clock_expiry(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pulsara_agent.conversation_kernel import provider_dispatch as dispatch_module

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("installed before the clock crossed")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    continuity = runner._provider_dispatch._continuity
    install = continuity.install

    def install_then_cross_deadline(*args, **kwargs):
        permit = install(*args, **kwargs)
        monkeypatch.setattr(dispatch_module, "monotonic", lambda: float("inf"))
        return permit

    monkeypatch.setattr(continuity, "install", install_then_cross_deadline)

    result = asyncio.run(
        runner.run_turn(frozen_test_prompt("complete the atomic install"))
    )

    assert result.final_text == "installed before the clock crossed"
    assert len(model.requests) == 1


@pytest.mark.parametrize(
    "failure_call",
    (1, 2),
    ids=("first_measurement", "measurement_after_one_accepted_turn"),
)
def test_final_wire_late_measurement_failure_closes_linear_dispatch_authority(
    failure_call: int,
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel(
        [
            *([_text_stream("initial accepted turn")] if failure_call == 2 else []),
            _text_stream("recovered after measurement failure"),
        ]
    )
    tools = _RecordingBorrowToolPort(
        _AssertingTool(provider, session_id), tool_names=()
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=tools,
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    measure = runner._provider_dispatch.measure_prepared_wire_candidate
    measurement_calls = 0

    async def fail_selected_measurement(*args, **kwargs):
        nonlocal measurement_calls
        measurement_calls += 1
        if measurement_calls == failure_call:
            raise TimeoutError("injected late final-wire measurement failure")
        return await measure(*args, **kwargs)

    runner._provider_dispatch.measure_prepared_wire_candidate = (
        fail_selected_measurement
    )

    async def exercise():
        if failure_call == 2:
            accepted = await runner.run_turn(
                frozen_test_prompt("accepted before the injected failure")
            )
            assert accepted.final_text == "initial accepted turn"
        with pytest.raises(
            TimeoutError, match="injected late final-wire measurement failure"
        ):
            await runner.run_turn(frozen_test_prompt("fail before provider open"))
        assert runner._safe_point._active_handle is None  # noqa: SLF001
        assert tools._active == set()  # noqa: SLF001
        assert tools.release_calls == [1] * failure_call

        recovered = await runner.run_turn(
            frozen_test_prompt("acquire the next safe point")
        )
        await owner.aclose()
        return recovered

    recovered = asyncio.run(exercise())

    assert recovered.final_text == "recovered after measurement failure"
    assert measurement_calls == failure_call + 1
    assert runner._safe_point._active_handle is None  # noqa: SLF001
    assert tools._active == set()  # noqa: SLF001
    assert tools.release_calls == [1] * (failure_call + 1)


def test_final_wire_post_full_hook_sibling_wins_with_one_authority_transfer(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    hook_text = "post-compaction hook context selected on final wire"
    collector = _PostCompactionHookContextCollector(hook_text)
    model = _CompactionScriptedModel(
        [
            _text_stream("historical answer " + "x" * 80_000),
            _text_stream("final after Hook sibling"),
        ],
        "A concise handoff that leaves room for the Hook sibling.",
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    measured_deadlines: list[float] = []
    install_deadlines: list[float] = []
    transferred_bases: list[object] = []
    measured = runner._provider_dispatch.measure_prepared_wire_candidate
    bind_selected = runner._provider_dispatch.bind_selected_provider_dispatch
    install = runner._provider_dispatch.install_provider_open

    async def record_measurement(*args, **kwargs):
        measured_deadlines.append(kwargs["deadline"])
        return await measured(*args, **kwargs)

    def record_transfer(*, base, sibling):
        assert sibling.candidate.cold_semantic is not None
        assert (
            sibling.candidate.cold_semantic.non_trigger_sources.hook_context_reservation
            is None
        )
        selected = bind_selected(base=base, sibling=sibling)
        transferred_bases.append(base)
        return selected

    async def record_install(*args, **kwargs):
        install_deadlines.append(kwargs["deadline"])
        return await install(*args, **kwargs)

    async def exercise():
        await runner.run_turn(frozen_test_prompt("first question"))
        runner._provider_dispatch.measure_prepared_wire_candidate = record_measurement
        runner._provider_dispatch.bind_selected_provider_dispatch = record_transfer
        runner._provider_dispatch.install_provider_open = record_install
        command_id = _name("second-command")
        turn_id = _stable_id("turn", session_id, command_id)
        _request, waiter = await owner.request_manual(
            command_id=_name("compact-command"),
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id=turn_id,
            force=True,
        )
        result = await runner.run_turn(
            frozen_test_prompt("second question"), command_id=command_id
        )
        outcome = await waiter
        await owner.aclose()
        return result, outcome

    result, outcome = asyncio.run(exercise())

    assert result.final_text == "final after Hook sibling"
    assert outcome.disposition is CompactionDisposition.COMPACTED
    assert _hook_context_bodies(model.requests[-1]) == (hook_text,)
    # The unpublished direct admission, PRE_FULL source proof, summary call,
    # post-FULL no-Hook base, and optional Hook sibling each own one exact
    # candidate measurement.
    assert len(measured_deadlines) == 5
    assert measured_deadlines == sorted(measured_deadlines)
    assert len(install_deadlines) == 1
    assert install_deadlines[0] > measured_deadlines[-1]
    assert len(transferred_bases) == 1
    base = transferred_bases[0]
    assert not base.owns_execution_authority
    with pytest.raises(RuntimeError, match="authority is consumed"):
        base.take_execution_authority()
    with pytest.raises(RuntimeError, match="authority is consumed"):
        base.close()
    # SessionStart on the first ROOT, the compacted source dispatch, and the
    # post-adoption Hook sibling each freeze one one-shot reservation.  The
    # selected sibling transfers authority once and every phase retires once.
    assert tuple(item.retire_calls for item in collector.reservations) == (1, 1, 1)
    assert len(model.requests) == 2


def test_final_wire_post_full_hook_timeout_falls_back_with_fresh_install_deadline(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    collector = _PostCompactionHookContextCollector(
        "Hook context whose optional final-wire probe times out"
    )
    model = _CompactionScriptedModel(
        [
            _text_stream("historical answer " + "x" * 80_000),
            _text_stream("final from verified no-Hook base"),
        ],
        "A concise handoff for the verified no-Hook fallback.",
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    hook_candidate = None
    hook_deadline = None
    install_deadline = None
    bind_calls = 0
    prepare_sibling = runner._provider_dispatch.prepare_hook_context_sibling
    measure = runner._provider_dispatch.measure_prepared_wire_candidate
    bind_selected = runner._provider_dispatch.bind_selected_provider_dispatch
    install = runner._provider_dispatch.install_provider_open

    async def record_sibling(*args, **kwargs):
        nonlocal hook_candidate
        sibling = await prepare_sibling(*args, **kwargs)
        # The pending ROOT is first compiled before compaction with no pending
        # Hook value.  Only the post-adoption SessionStart produces the sibling
        # whose optional wire probe is injected below.
        if sibling is not None:
            hook_candidate = sibling.candidate
        return sibling

    async def timeout_hook(candidate, **kwargs):
        nonlocal hook_deadline
        if candidate is hook_candidate:
            hook_deadline = kwargs["deadline"]
            raise TimeoutError("injected optional Hook final-wire timeout")
        return await measure(candidate, **kwargs)

    def record_bind(*args, **kwargs):
        nonlocal bind_calls
        bind_calls += 1
        return bind_selected(*args, **kwargs)

    async def record_install(*args, **kwargs):
        nonlocal install_deadline
        install_deadline = kwargs["deadline"]
        return await install(*args, **kwargs)

    async def exercise():
        await runner.run_turn(frozen_test_prompt("first question"))
        runner._provider_dispatch.prepare_hook_context_sibling = record_sibling
        runner._provider_dispatch.measure_prepared_wire_candidate = timeout_hook
        runner._provider_dispatch.bind_selected_provider_dispatch = record_bind
        runner._provider_dispatch.install_provider_open = record_install
        command_id = _name("second-command")
        turn_id = _stable_id("turn", session_id, command_id)
        _request, waiter = await owner.request_manual(
            command_id=_name("compact-command"),
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id=turn_id,
            force=True,
        )
        result = await runner.run_turn(
            frozen_test_prompt("second question"), command_id=command_id
        )
        outcome = await waiter
        await owner.aclose()
        return result, outcome

    result, outcome = asyncio.run(exercise())

    assert result.final_text == "final from verified no-Hook base"
    assert outcome.disposition is CompactionDisposition.COMPACTED
    assert _hook_context_bodies(model.requests[-1]) == ()
    assert hook_deadline is not None
    assert install_deadline is not None
    assert install_deadline > hook_deadline
    assert bind_calls == 0
    assert tuple(item.retire_calls for item in collector.reservations) == (1, 1, 1)
    assert len(model.requests) == 2


@pytest.mark.parametrize("failure_site", ("rotated_read", "hook_bind"))
def test_final_wire_post_full_failure_closes_unique_handle_and_hook_reservation(
    stage2_migrated_postgres_database,
    failure_site: str,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    collector = _PostCompactionHookContextCollector(
        "optional Hook context for post-FULL ownership failure"
    )
    model = _CompactionScriptedModel(
        [_text_stream("historical answer " + "x" * 80_000)],
        "A concise handoff before the injected post-FULL failure.",
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
        compaction_owner=owner,
        workspace_id=workspace_id,
    )

    async def exercise():
        await runner.run_turn(frozen_test_prompt("first question"))
        if failure_site == "rotated_read":
            read_dispatch = runner._provider_dispatch.read_dispatch_read

            async def fail_adopted_read(cut, **kwargs):
                if cut.context_binding_revision_id.startswith("context-binding:"):
                    raise RuntimeError("injected post-rotation read failure")
                return await read_dispatch(cut, **kwargs)

            runner._provider_dispatch.read_dispatch_read = fail_adopted_read
        else:

            def fail_hook_bind(*, base, sibling):
                del base, sibling
                raise RuntimeError("injected Hook selection bind failure")

            runner._provider_dispatch.bind_selected_provider_dispatch = fail_hook_bind
        command_id = _name("second-command")
        turn_id = _stable_id("turn", session_id, command_id)
        _request, waiter = await owner.request_manual(
            command_id=_name("compact-command"),
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            expected_turn_id=turn_id,
            force=True,
        )
        with pytest.raises(RuntimeError, match="injected"):
            await runner.run_turn(
                frozen_test_prompt("second question"), command_id=command_id
            )
        outcome = await waiter
        await owner.aclose()
        return outcome

    outcome = asyncio.run(exercise())

    assert outcome.disposition is CompactionDisposition.COMPACTED
    assert runner._safe_point._active_handle is None  # noqa: SLF001
    assert len(model.requests) == 1
    if failure_site == "hook_bind":
        # The first ROOT, compaction source, and failing post-adoption sibling
        # each relinquish their exact one-shot reservation.
        assert tuple(item.retire_calls for item in collector.reservations) == (
            1,
            1,
            1,
        )
    else:
        # The rotated read fails before the post-adoption sibling is frozen;
        # the first ROOT and compaction source reservations are still retired.
        assert tuple(item.retire_calls for item in collector.reservations) == (1, 1)
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        snapshot_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.context_snapshots WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
    assert snapshot_count == 1


@pytest.mark.parametrize("retry_recent", (False, True))
def test_round5b_proactive_auto_compaction_runs_before_next_provider_open(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
    retry_recent: bool,
) -> None:
    from pulsara_agent.conversation_kernel.compaction import (
        coordinator as compaction_coordinator,
    )

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    summary = "A concise free-form handoff for the pending manual request."
    model = _LimitedCompactionScriptedModel(
        [
            # The final-wire estimator traverses JSON at two characters per
            # token.  Keep the combined source over budget while allowing the
            # historical prefix and the post-compaction active request to fit
            # independently.
            _text_stream("historical " + "x" * 60_000),
            _text_stream("automatic compaction final"),
        ],
        summary,
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            auto_trigger_ratio=0.90,
            post_compaction_target_ratio=0.75,
            minimum_reclaim_tokens=1,
        )
    )
    source_collector = StaticContextSourceCollector()
    pending_root_freezes = 0
    second_command_id = _name("automatic-second-command")
    second_turn_id = _stable_id("turn", session_id, second_command_id)
    freeze_non_trigger_sources = source_collector.freeze_non_trigger_sources

    def record_non_trigger_freeze(*args, **kwargs):
        nonlocal pending_root_freezes
        facts = kwargs["canonical_facts"]
        if facts.canonical_input.identity.turn_id == second_turn_id:
            pending_root_freezes += 1
        return freeze_non_trigger_sources(*args, **kwargs)

    monkeypatch.setattr(
        source_collector,
        "freeze_non_trigger_sources",
        record_non_trigger_freeze,
    )
    if retry_recent:
        monkeypatch.setattr(
            compaction_coordinator,
            "_ordinary_recent_suffixes",
            lambda recent: (recent, ()),
        )
    memory_projection = _PolicyMemoryProjection()
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=source_collector,
        compaction_owner=owner,
        workspace_id=workspace_id,
        memory_projection=memory_projection,
    )
    input_reader = _HeadroomOrderingReader(
        provider,
        blob_reader=runner._provider_dispatch._input_reader._blob_reader,
    )
    runner._provider_dispatch._input_reader = input_reader
    runner.compaction._input_reader = input_reader
    prospective_family_calls = 0
    prospective_candidate_calls = 0
    prepare_family = runner._provider_dispatch.prepare_prospective_root_candidate_family
    prepare_candidate = runner._provider_dispatch.prepare_prospective_root_candidate

    async def record_prospective_family(**kwargs):
        nonlocal prospective_family_calls
        if kwargs["candidate"].exact_turn_id == second_turn_id:
            prospective_family_calls += 1
        return await prepare_family(**kwargs)

    async def record_prospective_candidate(family, **kwargs):
        nonlocal prospective_candidate_calls
        if kwargs["candidate"].exact_turn_id == second_turn_id:
            prospective_candidate_calls += 1
        selected = await prepare_candidate(family, **kwargs)
        if (
            retry_recent
            and kwargs["candidate"].exact_turn_id == second_turn_id
            and prospective_candidate_calls == 2
        ):
            raise StructuredModelInputCompileError(
                ModelInputCompileFailureKind.REQUIRED_CONTEXT_EXCEEDS_BUDGET
            )
        return selected

    runner._provider_dispatch.prepare_prospective_root_candidate_family = (
        record_prospective_family
    )
    runner._provider_dispatch.prepare_prospective_root_candidate = (
        record_prospective_candidate
    )
    triggers: list[object] = []
    trigger_candidates: list[AutomaticCompactionTriggerCandidate] = []
    source_wire_quotes: list[tuple[int, int]] = []
    prepare_precompile = runner.compaction.prepare_precompile

    async def record_precompile(**kwargs):
        decision = await prepare_precompile(**kwargs)
        if isinstance(decision, AutomaticCompactionTriggerCandidate):
            trigger_candidates.append(decision)
        return decision

    runner.compaction.prepare_precompile = record_precompile
    execute = runner.compaction.execute_active

    async def record_trigger(**kwargs):
        triggers.append(kwargs["trigger"])
        return await execute(**kwargs)

    runner.compaction.execute_active = record_trigger
    recover_pending_root = runner.compaction.recover_pending_root_input

    async def record_pending_root_recovery(**kwargs):
        triggers.append(CompactionTrigger.AUTO_ACTIVE_CONTEXT)
        return await recover_pending_root(**kwargs)

    runner.compaction.recover_pending_root_input = record_pending_root_recovery
    automatic_order: list[str] = []
    prepare_source = runner._provider_dispatch.prepare_compaction_source
    run_fenced = owner.run_fenced

    async def record_source(**kwargs):
        automatic_order.append("source")
        prepared = await prepare_source(**kwargs)
        source_wire_quotes.append(
            (
                prepared.wire_quote.final_wire_estimated_input_tokens,
                prepared.wire_quote.effective_input_budget_tokens,
            )
        )
        return prepared

    async def record_fence(*args, **kwargs):
        automatic_order.append("fence")
        return await run_fenced(*args, **kwargs)

    runner._provider_dispatch.prepare_compaction_source = record_source
    owner.run_fenced = record_fence

    async def exercise():
        first = await runner.run_turn(frozen_test_prompt("first"))
        automatic_order.clear()
        source_wire_quotes.clear()
        input_reader.operations.clear()
        second = await runner.run_turn(
            frozen_test_prompt("y" * 40_000),
            command_id=second_command_id,
        )
        await owner.aclose()
        return first, second

    first, second = asyncio.run(exercise())

    assert first.final_text.startswith("historical")
    assert second.final_text == "automatic compaction final"
    assert triggers == [CompactionTrigger.AUTO_ACTIVE_CONTEXT]
    assert automatic_order == ["fence", "source"]
    # The unpublished next ROOT request now reaches the same compaction owner
    # before its writer, so it does not first become an active auto-trigger.
    assert trigger_candidates == []
    # One pre-compaction prospective attempt discovers the hard pressure.  The
    # compaction retry loop freezes one dry family; after FULL it discards that
    # authority and freezes/measures a fresh adopted successor family.
    # Removing another recent item adds one authority-free dry compilation.
    assert prospective_family_calls == 3
    assert prospective_candidate_calls == (4 if retry_recent else 3)
    assert pending_root_freezes == 3
    assert memory_projection.preference_calls == 5
    assert memory_projection.recall_calls == 3
    # The predecessor remains executable by itself; the exact predecessor plus
    # unpublished next request is the candidate that crosses the hard budget.
    assert source_wire_quotes[0][0] <= source_wire_quotes[0][1]
    assert len(model.summary_transport.contexts) == 1
    assert (
        model.summary_transport.contexts[0].compiler_estimated_input_tokens
        <= (source_wire_quotes[0][1])
    )
    assert len(model.requests) == 2
    successor_snapshot = _context_snapshot_payload(model.requests[1])
    assert successor_snapshot["continuation"]["mode"] == "AWAIT_NEXT_USER"
    assert successor_snapshot["continuation"]["active_request"] is None
    assert (
        sum(
            message.content == (LLMTextPart("y" * 40_000),)
            for message in model.requests[1].compiled_input.messages
        )
        == 1
    )
    assert input_reader.operations.count("dispatch") >= 2


def test_round5b_idle_manual_compaction_adopts_without_successor_open(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    summary = "A concise free-form handoff for idle compaction."
    model = _CompactionScriptedModel(
        [_text_stream("idle history " + "x" * 80_000)], summary
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )

    async def exercise():
        first = await runner.run_turn(frozen_test_prompt("question"))
        outcome = await runner.compaction.compact_idle_turn(
            turn_id=first.turn_id,
            command_id=_name("idle-compact"),
            force=True,
        )
        await owner.aclose()
        return first, outcome

    first, outcome = asyncio.run(exercise())

    assert outcome.disposition is CompactionDisposition.COMPACTED
    assert outcome.snapshot_id is not None
    assert len(model.summary_transport.contexts) == 1
    assert model.summary_transport.contexts[0].messages[-1].content == (
        LLMTextPart(compaction_summary_request()),
    )
    assert len(model.requests) == 1
    scope = ProviderInputContinuityScope(
        session_id=session_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    assert runner._continuity.current_view(scope) is None
    assert (
        repository.read_turn_status(
            session_id=session_id,
            turn_id=first.turn_id,
            deadline_monotonic=monotonic() + 10,
        ).value
        == "COMPLETED"
    )

    # A fresh Host has no process-local continuation state. The durable snapshot
    # must still wait silently and then guide the first user-driven cold open.
    replacement_repository = ConversationKernelRepository(provider)
    replacement_lease = _acquire_bound_host_writer(
        replacement_repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("replacement-host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    replacement_model = _ScriptedModel([_text_stream("answer after restart")])
    replacement_runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=replacement_repository,
        writer_lease=replacement_lease,
        model=replacement_model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        workspace_id=workspace_id,
    )

    restarted = asyncio.run(
        replacement_runner.run_turn(frozen_test_prompt("question after restart"))
    )

    assert restarted.final_text == "answer after restart"
    assert len(replacement_model.requests) == 1
    restart_snapshot = _context_snapshot_payload(replacement_model.requests[0])
    assert restart_snapshot["continuation"]["mode"] == "AWAIT_NEXT_USER"
    assert restart_snapshot["continuation"]["active_request"] is None
    assert restart_snapshot["continuation"]["instruction"].startswith(
        "HANDOFF COMPLETE / AWAIT NEXT USER"
    )
    assert (
        sum(
            message.content == (LLMTextPart("question after restart"),)
            for message in replacement_model.requests[0].compiled_input.messages
        )
        == 1
    )


def test_round5b_idle_manual_non_reclaim_matches_active_not_needed(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _CompactionScriptedModel(
        [_text_stream("brief idle answer")],
        "oversized handoff " + "x" * 100_000,
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(
            automatic_enabled=False,
            minimum_reclaim_tokens=1,
        )
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )

    async def exercise():
        first = await runner.run_turn(frozen_test_prompt("question"))
        outcome = await runner.compaction.compact_idle_turn(
            turn_id=first.turn_id,
            command_id=_name("idle-compact"),
            force=True,
        )
        await owner.aclose()
        return outcome

    outcome = asyncio.run(exercise())

    assert outcome.disposition is CompactionDisposition.NOT_NEEDED
    assert outcome.public_code == "CONTEXT_ALREADY_COMPACT"
    assert outcome.snapshot_id is None
    assert len(model.summary_transport.contexts) == 1
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        snapshot_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.context_snapshots WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
    assert snapshot_count == 0


def test_round3_1_empty_epoch_absorbs_pre_first_call_steers_once(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    turn_id = _stable_id("turn", session_id, command_id)
    model = _ScriptedModel([_text_stream("one call")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    pending_read = repository.read_pending_prompt_steer_facts
    pending_read_started = Event()
    release_pending_read = Event()
    pending_read_calls = 0

    def block_first_post_admission_pending_read(*args, **kwargs):
        nonlocal pending_read_calls
        pending_read_calls += 1
        if pending_read_calls == 1:
            pending_read_started.set()
            if not release_pending_read.wait(timeout=5):
                raise TimeoutError("post-admission pending read was not released")
        return pending_read(*args, **kwargs)

    monkeypatch.setattr(
        repository,
        "read_pending_prompt_steer_facts",
        block_first_post_admission_pending_read,
    )

    async def exercise():
        task = asyncio.create_task(
            runner.run_turn(frozen_test_prompt("initial"), command_id=command_id)
        )
        # The direct prompt has passed its writer gate and now owns the first
        # provider safe point.  Steers that become addressable in this interval
        # must still enter that first exact input through the ordinary steer
        # prospective-admission owner.
        assert await asyncio.to_thread(pending_read_started.wait, 5)
        for index, text in enumerate(("steer one", "steer two"), start=1):
            steer_command = _name(f"steer-command-{index}")
            enqueue_test_prompt(
                repository,
                lease.guard,
                command_id=steer_command,
                queue_item_id=_name(f"steer-queue-{index}"),
                client_submission_id=steer_command,
                delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
                target_turn_id=turn_id,
                permission_snapshot_id=None,
                requested_permission_mode=None,
                model_call_binding=None,
                content=FrozenPromptContent.text(text),
                occurred_at=datetime.now(timezone.utc),
                actor_id="test",
                deadline_monotonic=monotonic() + 10,
            )
        release_pending_read.set()
        return await task

    result = asyncio.run(exercise())
    assert result.final_text == "one call"
    assert len(model.requests) == 1
    assert pending_read_calls >= 2
    user_messages = [
        text_part_values(message.content)[0]
        for message in model.requests[0].compiled_input.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" not in join_text_content(message.content)
    ]
    assert user_messages == ["initial", "steer one", "steer two"]


def test_memory_bad_citation_settles_and_model_can_reply_then_continue(
    stage2_migrated_postgres_database,
    tmp_path,
) -> None:
    from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
    from pulsara_agent.conversation_kernel.io import KernelSessionIO
    from pulsara_agent.memory.scope import (
        MemoryDomainContext,
        freeze_memory_read_context_binding,
    )
    from pulsara_agent.retrieval.config import EmbeddingBackendConfig
    from pulsara_agent.settings import LocalSettingsStore
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, workspace_id = _name("session"), _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    io = KernelSessionIO()
    memory = KernelMemoryToolPort(
        repository=repository,
        session_id=session_id,
        read_binding=freeze_memory_read_context_binding(
            domain=MemoryDomainContext("test", "transient"),
            host_workspace_id=workspace_id,
        ),
        embedding_config=EmbeddingBackendConfig(),
        io_owner=io,
        settings=LocalSettingsStore(tmp_path / "settings.yaml"),
    )

    class MemoryDelegate(_AssertingTool):
        async def invoke(self, *, tool_name, arguments, invocation_context, **kwargs):
            return await memory.invoke(
                tool_name=tool_name,
                arguments=arguments,
                invocation_context=invocation_context,
            )

    model = _ScriptedModel(
        [
            _named_tool_stream(
                tool_name="remember",
                tool_call_id="call:bad-citation",
                arguments={
                    "statement": "Test memory",
                    "context_target": "GLOBAL",
                    "kind_hint": "FACT",
                    "cited_tool_result_handles": ["tool:not-visible"],
                },
            ),
            _text_stream("记忆引用无效，本次未保存。"),
            _text_stream("下一轮仍可以正常回复。"),
        ]
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(
            MemoryDelegate(provider, session_id), tool_names=("remember",)
        ),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )

    async def exercise():
        try:
            first = await runner.run_turn(
                frozen_test_prompt("Please save the test memory")
            )
            assert first.final_text == "记忆引用无效，本次未保存。"
            second = await runner.run_turn(frozen_test_prompt("Continue normally"))
            assert second.final_text == "下一轮仍可以正常回复。"
        finally:
            await memory.aclose()
            await io.aclose(deadline_monotonic=monotonic() + 10)

    asyncio.run(exercise())
    assert len(model.requests) == 3
    snapshot = CanonicalProtocolReader(provider).snapshot(
        session_id=session_id,
        maximum_entries=10,
        maximum_control_items=20,
        deadline_monotonic=monotonic() + 10,
    )
    assert snapshot.control.latest_root_turn.status == "COMPLETED"
    assert not snapshot.control.active_turns
    result_entries = [
        entry for entry in snapshot.entries if entry.HasField("tool_result")
    ]
    assert len(result_entries) == 1
    assert result_entries[0].tool_result.result_state == "APPLICATION_ERROR"
    assert result_entries[0].tool_result.tool_call_id == "call:bad-citation"
    assert result_entries[0].tool_result.assistant_entry_id in {
        entry.entry_id for entry in snapshot.entries if entry.blocks
    }
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 10
    ) as c:
        assert c.execute(
            "SELECT result_state FROM pulsara_v3.tool_results WHERE session_id=%s",
            (session_id,),
        ).fetchall() == [("APPLICATION_ERROR",)]
        assert c.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at",
            (session_id,),
        ).fetchall() == [("COMPLETED",), ("COMPLETED",)]
        assert (
            c.execute(
                "SELECT count(*) FROM pulsara_v3.memory_facts AS f "
                "JOIN pulsara_v3.tool_results AS r "
                "ON r.id=f.created_by_tool_result_id WHERE r.session_id=%s",
                (session_id,),
            ).fetchone()[0]
            == 0
        )


def test_saved_remember_id_can_be_used_as_a_memory_basis_without_tool_citation(
    stage2_migrated_postgres_database,
    tmp_path,
) -> None:
    from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
    from pulsara_agent.conversation_kernel.io import KernelSessionIO
    from pulsara_agent.memory.scope import (
        MemoryDomainContext,
        freeze_memory_read_context_binding,
        workspace_context_id,
    )
    from pulsara_agent.retrieval.config import EmbeddingBackendConfig
    from pulsara_agent.settings import LocalSettingsStore

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_root = _name("workspace-root")
    workspace_id = workspace_context_id(workspace_root)
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    io = KernelSessionIO()
    memory = KernelMemoryToolPort(
        repository=repository,
        session_id=session_id,
        read_binding=freeze_memory_read_context_binding(
            domain=MemoryDomainContext("u_local", "project", workspace_root),
            host_workspace_id=workspace_id,
        ),
        embedding_config=EmbeddingBackendConfig(),
        io_owner=io,
        settings=LocalSettingsStore(tmp_path / "settings.yaml"),
    )

    class MemoryDelegate(_AssertingTool):
        async def invoke(self, *, tool_name, arguments, invocation_context, **kwargs):
            return await memory.invoke(
                tool_name=tool_name,
                arguments=arguments,
                invocation_context=invocation_context,
            )

    calls = 0

    class MemoryStructuredToolPort(StructuredToolPort):
        def offer_memory_embedding_wake(self) -> None:
            pass

    async def stream(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            items = _named_tool_stream(
                tool_name="remember",
                tool_call_id="call:remember-first",
                arguments={
                    "statement": "The design review is on Tuesday.",
                    "context_target": "CURRENT_PROJECT",
                    "kind": "FACT",
                },
            )
        elif calls == 2:
            result_messages = [
                json.loads(text_part_values(message.content)[0])["pulsara_tool_result"]
                for message in request.compiled_input.messages
                if message.content
                and "pulsara_tool_result" in join_text_content(message.content)
            ]
            assert len(result_messages) == 1
            observed = result_messages[0]
            assert "citation_handle" not in observed
            basis_id = json.loads(observed["body"])["memory_id"]
            assert observed["model_visible_memory_ids"] == [basis_id]
            items = _named_tool_stream(
                tool_name="remember",
                tool_call_id="call:remember-second",
                arguments={
                    "statement": "The design review needs a slide deck.",
                    "context_target": "CURRENT_PROJECT",
                    "kind": "FACT",
                    "based_on_memory_ids": [basis_id],
                },
            )
        else:
            items = _text_stream("两条记忆已保存。")
        for item in items:
            yield item

    model = CallbackScriptedKernelModel(stream)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=MemoryStructuredToolPort(
            MemoryDelegate(provider, session_id), tool_names=("remember",)
        ),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )

    async def exercise():
        try:
            result = await runner.run_turn(
                frozen_test_prompt("Remember both review facts")
            )
            assert result.final_text == "两条记忆已保存。"
        finally:
            await memory.aclose()
            await io.aclose(deadline_monotonic=monotonic() + 10)

    asyncio.run(exercise())
    assert calls == 3
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            "SELECT f.id, f.statement FROM pulsara_v3.memory_facts AS f "
            "JOIN pulsara_v3.tool_results AS r "
            "ON r.id=f.created_by_tool_result_id "
            "WHERE r.session_id=%s ORDER BY f.statement",
            (session_id,),
        ).fetchall()
        relations = connection.execute(
            "SELECT source_fact_id, target_fact_id FROM pulsara_v3.memory_relations "
            "WHERE relation_kind='BASED_ON' "
            "AND source_fact_id IN ("
            "SELECT f.id FROM pulsara_v3.memory_facts AS f "
            "JOIN pulsara_v3.tool_results AS r "
            "ON r.id=f.created_by_tool_result_id WHERE r.session_id=%s) "
            "ORDER BY source_fact_id",
            (session_id,),
        ).fetchall()
    assert len(rows) == 2
    by_statement = {statement: fact_id for fact_id, statement in rows}
    assert relations == [(
        by_statement["The design review needs a slide deck."],
        by_statement["The design review is on Tuesday."],
    )]


def test_round8_memory_policy_aggregates_steers_and_resets_on_next_root_message(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    projection = _PolicyMemoryProjection()
    model = _ScriptedModel(
        [
            _text_stream("first answer"),
            _text_stream("second answer"),
            _text_stream("third answer"),
        ]
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        memory_projection=projection,
    )
    pending_read = repository.read_pending_prompt_steer_facts
    pending_read_started = Event()
    release_pending_read = Event()
    blocked_turn_id: str | None = None

    def block_selected_post_admission_pending_read(*args, **kwargs):
        nonlocal blocked_turn_id
        if kwargs["target_turn_id"] == blocked_turn_id:
            blocked_turn_id = None
            pending_read_started.set()
            if not release_pending_read.wait(timeout=5):
                raise TimeoutError("post-admission pending read was not released")
        return pending_read(*args, **kwargs)

    monkeypatch.setattr(
        repository,
        "read_pending_prompt_steer_facts",
        block_selected_post_admission_pending_read,
    )

    async def exercise() -> None:
        await runner.run_turn(frozen_test_prompt("normal initial root message"))
        assert model.requests[0].memory_context.memory_use_policy is (
            MemoryUsePolicy.ENABLED
        )
        assert projection.preference_calls == 1
        assert projection.recall_calls == 1

        second_command = _name("command")
        second_turn = _stable_id("turn", session_id, second_command)
        nonlocal blocked_turn_id
        blocked_turn_id = second_turn
        second = asyncio.create_task(
            runner.run_turn(
                frozen_test_prompt("don't use saved memory for this answer"),
                command_id=second_command,
            )
        )
        assert await asyncio.to_thread(pending_read_started.wait, 5)
        steer_command = _name("steer-command")
        enqueue_test_prompt(
            repository,
            lease.guard,
            command_id=steer_command,
            queue_item_id=_name("steer-queue"),
            client_submission_id=steer_command,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=second_turn,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            model_call_binding=None,
            content=FrozenPromptContent.text("continue normally"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        release_pending_read.set()
        await second
        assert model.requests[1].memory_context.memory_use_policy is (
            MemoryUsePolicy.ALL_DISABLED_BY_USER
        )
        assert projection.preference_calls == 1
        assert projection.recall_calls == 1

        await runner.run_turn(frozen_test_prompt("normal next root message"))
        assert model.requests[2].memory_context.memory_use_policy is (
            MemoryUsePolicy.ENABLED
        )
        assert projection.preference_calls == 2
        assert projection.recall_calls == 2

    asyncio.run(exercise())
    first_input, second_input, third_input = (
        request.compiled_input for request in model.requests
    )
    assert first_input.system_prompt == second_input.system_prompt
    assert second_input.system_prompt == third_input.system_prompt
    assert first_input.tools == second_input.tools
    assert second_input.tools == third_input.tools
    assert second_input.messages[: len(first_input.messages)] == first_input.messages
    assert third_input.messages[: len(second_input.messages)] == second_input.messages
    second_suffix_observations = tuple(
        decode_runtime_observation(message)
        for message in second_input.messages[len(first_input.messages) :]
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in join_text_content(message.content)
    )
    cleared_memory_sources = {
        item.source_kind
        for item in second_suffix_observations
        if item.presence.value == "CLEARED"
    }
    assert {
        ContextSourceKind.MEMORY_RECALL,
        ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
    } <= cleared_memory_sources


def test_round8_memory_write_hint_is_gated_before_the_real_provider_wire(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)

    async def run_case(
        *,
        text: str,
        memory_enabled: bool,
        tool_names: tuple[str, ...],
        permission: PermissionMode = DEFAULT_PERMISSION_MODE,
    ) -> tuple[object, tuple[str, ...]]:
        repository = ConversationKernelRepository(provider)
        session_id = _name("session")
        lease = _acquire_bound_host_writer(
            repository,
            session_id=session_id,
            workspace_id=_name("workspace"),
            writer_owner_id=_name("host"),
            lease_seconds=30,
            deadline_monotonic=monotonic() + 30,
        )
        model = _ScriptedModel([_text_stream("done")])
        runner = ConversationKernelRunner(
            model_resolution_snapshot_provider=test_model_resolution_snapshot,
            repository=repository,
            writer_lease=lease,
            model=model,
            tools=StructuredToolPort(
                _AssertingTool(provider, session_id), tool_names=tool_names
            ),
            live_bus=LiveAgentEventBus(),
            context_source_collector=StaticContextSourceCollector(),
            memory_projection=_PolicyMemoryProjection() if memory_enabled else None,
        )
        result = await runner.run_turn(
            frozen_test_prompt(text), requested_permission_mode=permission
        )
        assert result.final_text == "done"
        assert len(model.requests) == 1
        with provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            canonical_entries = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT entry_kind FROM pulsara_v3.transcript_entries "
                    "WHERE session_id=%s ORDER BY entry_sequence",
                    (session_id,),
                ).fetchall()
            )
        return model.requests[0].compiled_input, canonical_entries

    async def exercise() -> None:
        positive, entries = await run_case(
            text="Please remember that I prefer concise answers",
            memory_enabled=True,
            tool_names=("remember",),
        )
        opt_out, _ = await run_case(
            text="Please don't remember what I just said",
            memory_enabled=True,
            tool_names=("remember",),
        )
        disabled, _ = await run_case(
            text="Please remember that I prefer concise answers",
            memory_enabled=False,
            tool_names=("remember",),
        )
        read_only, _ = await run_case(
            text="Please remember that I prefer concise answers",
            memory_enabled=True,
            tool_names=("remember",),
            permission=PermissionMode.READ_ONLY,
        )
        absent_tool, _ = await run_case(
            text="Please remember that I prefer concise answers",
            memory_enabled=True,
            tool_names=(),
        )

        def visible_hints(compiled: object) -> tuple[object, ...]:
            return tuple(
                decoded
                for message in compiled.messages  # type: ignore[attr-defined]
                if message.role is MessageRole.USER
                and message.content
                and "pulsara_runtime_observation" in join_text_content(message.content)
                for decoded in (decode_runtime_observation(message),)
                if decoded.source_kind is ContextSourceKind.MEMORY_WRITE_HINT
            )

        hints = visible_hints(positive)
        assert len(hints) == 1
        assert hints[0].body == MEMORY_WRITE_HINT_BODY
        assert entries == ("USER_MESSAGE", "ASSISTANT_MESSAGE")
        for gated in (opt_out, disabled, read_only, absent_tool):
            assert visible_hints(gated) == ()

    asyncio.run(exercise())


def test_round8_accepted_user_steer_independently_adds_one_memory_write_hint(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    turn_id = _stable_id("turn", session_id, command_id)
    model = _BlockingFirstCallModel(
        [
            _text_stream("first answer"),
            _tool_stream(),
            _text_stream("final answer", block="text:3"),
        ]
    )
    tool = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tool, tool_names=("remember", "terminal")),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        memory_projection=_PolicyMemoryProjection(),
    )

    async def exercise():
        task = asyncio.create_task(
            runner.run_turn(
                frozen_test_prompt("Inspect the current request"), command_id=command_id
            )
        )
        await asyncio.wait_for(model.started.wait(), timeout=5)
        steer_command = _name("steer-command")
        enqueue_test_prompt(
            repository,
            lease.guard,
            command_id=steer_command,
            queue_item_id=_name("steer-queue"),
            client_submission_id=steer_command,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn_id,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            model_call_binding=None,
            content=FrozenPromptContent.text(
                "Please remember that I prefer concise answers"
            ),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        model.release.set()
        return await task

    result = asyncio.run(exercise())
    assert result.final_text == "final answer"
    assert len(model.requests) == 3
    first, second, third = (request.compiled_input for request in model.requests)
    assert second.messages[: len(first.messages)] == first.messages
    assert third.messages[: len(second.messages)] == second.messages

    def hint_indexes(compiled) -> tuple[int, ...]:
        return tuple(
            index
            for index, message in enumerate(compiled.messages)
            if message.role is MessageRole.USER
            and message.content
            and "pulsara_runtime_observation" in join_text_content(message.content)
            and decode_runtime_observation(message).source_kind
            is ContextSourceKind.MEMORY_WRITE_HINT
        )

    assert hint_indexes(first) == ()
    second_hints = hint_indexes(second)
    assert len(second_hints) == 1
    steer_index = next(
        index
        for index, message in enumerate(second.messages)
        if message.role is MessageRole.USER
        and message.content
        and message.content
        == (LLMTextPart("Please remember that I prefer concise answers"),)
    )
    assert second_hints[0] == steer_index - 1
    assert hint_indexes(third) == second_hints
    assert len(tool.invocations) == 1
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id=%s AND entry_kind='USER_STEER'",
            (session_id,),
        ).fetchone() == (1,)


def test_round3_1_planning_reaches_shorter_fifo_prefix_without_recharging_base(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    turn_id = _stable_id("turn", session_id, command_id)
    collector = _BlockingSourceCollector()
    model = _BlockingFirstCallModel([_text_stream("one call")])
    compiler = _OnlyOneSteerCompiler()
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
        compiler=compiler,
    )
    collector.release.set()
    pending_gate = _PostAdmissionPendingSteerReadGate(
        repository.read_pending_prompt_steer_facts,
        target_turn_id=turn_id,
    )
    monkeypatch.setattr(repository, "read_pending_prompt_steer_facts", pending_gate)
    # Nested prefixes share the same 64 KiB canonical base.  The injected
    # target admits only one steer.  A 512 KiB planning bound admits the unique
    # base + suffix materialization, while charging the base for each of the
    # sixteen trials would fail before reaching the valid one-item prefix.
    initial = "x" * (64 << 10)
    body = b"12345678"
    queue_ids = tuple(_name(f"steer-queue-{index}") for index in range(1, 17))

    async def exercise():
        task = asyncio.create_task(
            runner.run_turn(frozen_test_prompt(initial), command_id=command_id)
        )
        assert await asyncio.to_thread(pending_gate.started.wait, 5)
        for index, queue_id in enumerate(queue_ids, start=1):
            steer_command = _name(f"steer-command-{index}")
            enqueue_test_prompt(
                repository,
                lease.guard,
                command_id=steer_command,
                queue_item_id=queue_id,
                client_submission_id=steer_command,
                delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
                target_turn_id=turn_id,
                permission_snapshot_id=None,
                requested_permission_mode=None,
                model_call_binding=None,
                content=FrozenPromptContent.text(body.decode("utf-8")),
                occurred_at=datetime.now(timezone.utc),
                actor_id="test",
                deadline_monotonic=monotonic() + 10,
            )
        monkeypatch.setattr(
            "pulsara_agent.conversation_kernel.provider_dispatch.MAXIMUM_STEER_PLANNING_CANONICAL_WORK_BYTES",
            512 << 10,
        )
        pending_gate.release.set()
        await asyncio.wait_for(model.started.wait(), timeout=5)
        user_messages = [
            text_part_values(message.content)[0]
            for message in model.requests[0].compiled_input.messages
            if message.role is MessageRole.USER
            and message.content
            and "pulsara_runtime_observation" not in join_text_content(message.content)
        ]
        with provider.connection(
            lane=PostgresConnectionLane.INSPECTOR,
            deadline_monotonic=monotonic() + 10,
        ) as connection:
            rows = connection.execute(
                "SELECT id, status FROM pulsara_v3.prompt_queue_items "
                "WHERE session_id = %s AND id = ANY(%s) ORDER BY queue_sequence",
                (session_id, list(queue_ids)),
            ).fetchall()
        task.cancel()
        model.release.set()
        await asyncio.gather(task, return_exceptions=True)
        return user_messages, rows

    try:
        user_messages, rows = asyncio.run(exercise())
    except StructuredModelInputCompileError as exc:
        pytest.fail(f"shortest prefix was rejected: {exc.kind}; {compiler.failures}")
    assert user_messages == [initial, body.decode("utf-8")]
    assert rows[0] == (queue_ids[0], "CONSUMED")
    assert len(rows) == len(queue_ids)
    assert all(status == "PENDING" for _queue_id, status in rows[1:])


def test_round3_1_expired_steer_planning_rejects_terminal_steer_and_io_closes(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    turn_id = _stable_id("turn", session_id, command_id)
    steer_command = _name("steer-command")
    steer_queue = _name("steer-queue")
    collector = _BlockingSourceCollector()
    io_owner = KernelSessionIO()
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
        compiler=_ExpiredSteerCompiler(),
        io_owner=io_owner,
    )
    collector.release.set()
    pending_gate = _PostAdmissionPendingSteerReadGate(
        repository.read_pending_prompt_steer_facts,
        target_turn_id=turn_id,
    )
    monkeypatch.setattr(repository, "read_pending_prompt_steer_facts", pending_gate)

    async def exercise() -> None:
        task = asyncio.create_task(
            runner.run_turn(frozen_test_prompt("initial"), command_id=command_id)
        )
        assert await asyncio.to_thread(pending_gate.started.wait, 5)
        enqueue_test_prompt(
            repository,
            lease.guard,
            command_id=steer_command,
            queue_item_id=steer_queue,
            client_submission_id=steer_command,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn_id,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            model_call_binding=None,
            content=FrozenPromptContent.text("must remain pending"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        pending_gate.release.set()
        with pytest.raises(StructuredModelInputCompileError) as failure:
            await task
        assert failure.value.kind is ModelInputCompileFailureKind.DEADLINE_EXPIRED
        await io_owner.aclose(deadline_monotonic=monotonic() + 1)

    asyncio.run(exercise())
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status, consumed_entry_id, terminal_reason "
            "FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s AND id = %s",
            (session_id, steer_queue),
        ).fetchone() == ("REJECTED", None, "TARGET_TURN_TERMINAL")
        assert connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s AND id = %s",
            (session_id, turn_id),
        ).fetchone() == ("INTERRUPTED",)


def test_round3_1_future_lane_does_not_block_active_steer_batch(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    future_command_id = _name("future-command")
    future_queue_item_id = _name("future-queue")
    steer_command_id = _name("steer-command")
    steer_queue_item_id = _name("steer-queue")
    turn_id = _stable_id("turn", session_id, command_id)
    collector = _BlockingSourceCollector()
    model = _ScriptedModel([_text_stream("one call")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
    )
    collector.release.set()
    pending_gate = _PostAdmissionPendingSteerReadGate(
        repository.read_pending_prompt_steer_facts,
        target_turn_id=turn_id,
    )
    monkeypatch.setattr(repository, "read_pending_prompt_steer_facts", pending_gate)

    async def exercise():
        task = asyncio.create_task(
            runner.run_turn(frozen_test_prompt("initial"), command_id=command_id)
        )
        assert await asyncio.to_thread(pending_gate.started.wait, 5)
        enqueue_test_prompt(
            repository,
            lease.guard,
            command_id=future_command_id,
            queue_item_id=future_queue_item_id,
            client_submission_id=future_command_id,
            delivery_mode=PromptDeliveryMode.NEW_TURN,
            target_turn_id=None,
            permission_snapshot_id="permission:future",
            requested_permission_mode=DEFAULT_PERMISSION_MODE,
            model_call_binding=test_model_binding(test_model_runtime()),
            content=FrozenPromptContent.text("future"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        enqueue_test_prompt(
            repository,
            lease.guard,
            command_id=steer_command_id,
            queue_item_id=steer_queue_item_id,
            client_submission_id=steer_command_id,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn_id,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            model_call_binding=None,
            content=FrozenPromptContent.text("steer now"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        pending_gate.release.set()
        return await task

    asyncio.run(exercise())
    assert collector.freeze_calls == 2
    assert collector.complete_calls == 2
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        rows = connection.execute(
            "SELECT id, status FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s ORDER BY queue_sequence",
            (session_id,),
        ).fetchall()
    assert rows == [
        (future_queue_item_id, "PENDING"),
        (steer_queue_item_id, "CONSUMED"),
    ]


def test_round3_1_installed_epoch_absorbs_two_steers_in_one_followup_call(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    turn_id = _stable_id("turn", session_id, command_id)
    model = _BlockingFirstCallModel(
        [_text_stream("first answer"), _text_stream("final answer", block="text:2")]
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )

    async def exercise():
        task = asyncio.create_task(
            runner.run_turn(frozen_test_prompt("initial"), command_id=command_id)
        )
        await asyncio.wait_for(model.started.wait(), timeout=5)
        for index, text in enumerate(("steer one", "steer two"), start=1):
            steer_command = _name(f"command-steer-{index}")
            steer_queue = _name(f"queue-steer-{index}")
            enqueue_test_prompt(
                repository,
                lease.guard,
                command_id=steer_command,
                queue_item_id=steer_queue,
                client_submission_id=steer_command,
                delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
                target_turn_id=turn_id,
                permission_snapshot_id=None,
                requested_permission_mode=None,
                model_call_binding=None,
                content=FrozenPromptContent.text(text),
                occurred_at=datetime.now(timezone.utc),
                actor_id="test",
                deadline_monotonic=monotonic() + 10,
            )
        model.release.set()
        return await task

    result = asyncio.run(exercise())
    assert result.final_text == "final answer"
    assert len(model.requests) == 2
    first = model.requests[0].compiled_input
    second = model.requests[1].compiled_input
    assert second.system_prompt == first.system_prompt
    assert second.tools == first.tools
    assert second.messages[: len(first.messages)] == first.messages
    appended_users = [
        text_part_values(message.content)[0]
        for message in second.messages[len(first.messages) :]
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" not in join_text_content(message.content)
    ]
    assert appended_users == ["steer one", "steer two"]
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s ORDER BY queue_sequence",
            (session_id,),
        ).fetchall() == [("CONSUMED",), ("CONSUMED",)]


def test_round3_1_post_consumption_read_failure_interrupts_without_open_or_recompile(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    steer_command_id = _name("steer-command")
    steer_queue_item_id = _name("steer-queue")
    turn_id = _stable_id("turn", session_id, command_id)
    collector = _BlockingSourceCollector()
    reader = _FailingPostConsumptionReader(CanonicalProviderInputReader(provider))
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        input_reader=reader,
        context_source_collector=collector,
    )
    collector.release.set()
    pending_gate = _PostAdmissionPendingSteerReadGate(
        repository.read_pending_prompt_steer_facts,
        target_turn_id=turn_id,
    )
    monkeypatch.setattr(repository, "read_pending_prompt_steer_facts", pending_gate)

    async def exercise() -> None:
        task = asyncio.create_task(
            runner.run_turn(frozen_test_prompt("initial"), command_id=command_id)
        )
        assert await asyncio.to_thread(pending_gate.started.wait, 5)
        enqueue_test_prompt(
            repository,
            lease.guard,
            command_id=steer_command_id,
            queue_item_id=steer_queue_item_id,
            client_submission_id=steer_command_id,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn_id,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            model_call_binding=None,
            content=FrozenPromptContent.text("accepted then mismatched"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        pending_gate.release.set()
        with pytest.raises(
            RuntimeError, match="injected post-consumption canonical mismatch"
        ):
            await task

    asyncio.run(exercise())
    assert reader.calls == 3
    assert collector.freeze_calls == 2
    assert collector.complete_calls == 2
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s AND id = %s",
            (session_id, steer_queue_item_id),
        ).fetchone() == ("CONSUMED",)
        assert connection.execute(
            "SELECT status, terminal_reason FROM pulsara_v3.turns "
            "WHERE session_id = %s AND id = %s",
            (session_id, turn_id),
        ).fetchone() == ("INTERRUPTED", "PROVIDER_INPUT_PLAN_CONFLICT")


def test_round3_1_pre_consumption_stale_plan_discards_and_replans_without_steer_entry(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _CancellingFirstSteerRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    steer_command_id = _name("steer-command")
    steer_queue_item_id = _name("steer-queue")
    turn_id = _stable_id("turn", session_id, command_id)
    collector = _BlockingSourceCollector()
    model = _ScriptedModel([_text_stream("initial only")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
    )
    collector.release.set()
    pending_gate = _PostAdmissionPendingSteerReadGate(
        repository.read_pending_prompt_steer_facts,
        target_turn_id=turn_id,
    )
    monkeypatch.setattr(repository, "read_pending_prompt_steer_facts", pending_gate)

    async def exercise():
        task = asyncio.create_task(
            runner.run_turn(frozen_test_prompt("initial"), command_id=command_id)
        )
        assert await asyncio.to_thread(pending_gate.started.wait, 5)
        enqueue_test_prompt(
            repository,
            lease.guard,
            command_id=steer_command_id,
            queue_item_id=steer_queue_item_id,
            client_submission_id=steer_command_id,
            delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn_id,
            permission_snapshot_id=None,
            requested_permission_mode=None,
            model_call_binding=None,
            content=FrozenPromptContent.text("cancel before consume"),
            occurred_at=datetime.now(timezone.utc),
            actor_id="test",
            deadline_monotonic=monotonic() + 10,
        )
        pending_gate.release.set()
        return await task

    result = asyncio.run(exercise())
    assert result.final_text == "initial only"
    assert repository.cancelled_once
    assert len(model.requests) == 1
    assert [
        text_part_values(message.content)[0]
        for message in model.requests[0].compiled_input.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" not in join_text_content(message.content)
    ] == ["initial"]
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status, consumed_entry_id FROM pulsara_v3.prompt_queue_items "
            "WHERE session_id = %s AND id = %s",
            (session_id, steer_queue_item_id),
        ).fetchone() == ("CANCELLED", None)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind = 'USER_STEER'",
            (session_id,),
        ).fetchone() == (0,)


def test_k3_direct_input_failure_rejects_before_user_acceptance_and_provider_open(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        maximum_input_tokens_per_call=4,
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        asyncio.run(runner.run_turn(frozen_test_prompt("reject before acceptance")))
    assert failure.value.kind is (
        ModelInputCompileFailureKind.PROTECTED_TRANSCRIPT_EXCEEDS_BUDGET
    )
    assert model.preparation_requests
    assert model.requests == []
    rows = repository.rehydrate_session(
        session_id=session_id, deadline_monotonic=monotonic() + 30
    )
    assert not rows
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        counts = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM pulsara_v3.turns WHERE session_id = %s), "
            "(SELECT count(*) FROM pulsara_v3.session_commands WHERE session_id = %s), "
            "(SELECT count(*) FROM pulsara_v3.transcript_entries WHERE session_id = %s)",
            (session_id, session_id, session_id),
        ).fetchone()
        assert counts == (0, 0, 0)
        assert connection.execute(
            "SELECT latest_entry_sequence FROM pulsara_v3.sessions WHERE id = %s",
            (session_id,),
        ).fetchone() == (0,)


@pytest.mark.parametrize("failure_type", (TimeoutError, asyncio.CancelledError))
def test_k3_soft_trigger_recovery_failure_closes_unpublished_prepared_input(
    failure_type: type[BaseException],
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("must not open")])
    tools = _RecordingBorrowToolPort(
        _AssertingTool(provider, session_id), tool_names=()
    )
    owner = HostCompactionRuntimeOwner(
        policy=ResolvedCompactionPolicy(minimum_reclaim_tokens=1)
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=tools,
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compaction_owner=owner,
        workspace_id=workspace_id,
    )
    runner.compaction.prospective_root_crosses_automatic_threshold = lambda _prepared: (
        True
    )

    async def fail_recovery(**_kwargs):
        raise failure_type("injected prospective compaction failure")

    runner.compaction.recover_pending_root_input = fail_recovery

    async def exercise() -> None:
        with pytest.raises(failure_type, match="injected prospective compaction"):
            await runner.run_turn(frozen_test_prompt("unpublished input"))
        await owner.aclose()

    asyncio.run(exercise())

    assert runner._safe_point._active_handle is None  # noqa: SLF001
    assert tools._active == set()  # noqa: SLF001
    assert tools.release_calls == [1]
    assert model.requests == []
    assert (
        repository.rehydrate_session(
            session_id=session_id, deadline_monotonic=monotonic() + 30
        )
        == ()
    )


def test_round7_1_full_required_budget_boundary_has_zero_provider_open(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        compiler=_FullRequiredBudgetCompiler(),
    )

    with pytest.raises(StructuredModelInputCompileError) as failure:
        asyncio.run(
            runner.run_turn(
                frozen_test_prompt("accepted before required FULL delivery")
            )
        )

    assert failure.value.kind is (
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET
    )
    assert model.preparation_requests
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (0,)


def test_round7_1_real_artifact_result_with_fifty_memory_ids_fails_typed_before_open(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    memory_ids = tuple(f"memory:{index:064x}" for index in range(50))
    result_id = _seed_artifact_result_with_memory_provenance(
        repository,
        lease=lease,
        workspace_id=workspace_id,
        memory_ids=memory_ids,
    )
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        maximum_input_tokens_per_call=100,
    )
    command_id = _name("command")

    with pytest.raises(StructuredModelInputCompileError) as failure:
        asyncio.run(
            runner.run_turn(
                frozen_test_prompt(
                    "new turn cannot fit the required full artifact page"
                ),
                command_id=command_id,
            )
        )

    assert failure.value.kind is (
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_EXCEEDS_INPUT_BUDGET
    )
    assert model.preparation_requests
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT result_state, cardinality(model_visible_memory_fact_ids) "
            "FROM pulsara_v3.tool_results WHERE session_id = %s AND id = %s",
            (session_id, result_id),
        ).fetchone() == ("SUCCESS", 50)
        assert (
            connection.execute(
                "SELECT status, terminal_reason FROM pulsara_v3.turns "
                "WHERE session_id = %s AND id = %s",
                (session_id, _stable_id("turn", session_id, command_id)),
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT 1 FROM pulsara_v3.session_commands "
                "WHERE session_id = %s AND command_id = %s",
                (session_id, command_id),
            ).fetchone()
            is None
        )


def test_round3_compile_failure_observer_cannot_block_turn_interruption(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        maximum_input_tokens_per_call=4,
        extensions=_FailingOperationalExtension(),  # type: ignore[arg-type]
    )
    with pytest.raises(StructuredModelInputCompileError):
        asyncio.run(runner.run_turn(frozen_test_prompt("accepted before compile")))
    assert model.requests == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        turn_count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone()[0]
    assert turn_count == 0


def test_round3_source_registry_drift_interrupts_with_zero_provider_open(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=_ChangingRegistryCollector(),
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        asyncio.run(
            runner.run_turn(frozen_test_prompt("accepted before registry drift"))
        )
    assert failure.value.kind is ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
    assert model.requests == []


def test_round3_surface_revoked_before_borrow_has_zero_provider_open(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_text_stream("must not open")])
    tools = _RevocableStructuredToolPort(
        _AssertingTool(provider, session_id), tool_names=("test_tool",)
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=tools,
        live_bus=LiveAgentEventBus(),
        context_source_collector=_SurfaceRevokingCollector(tools),
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        asyncio.run(
            runner.run_turn(frozen_test_prompt("accepted before surface revocation"))
        )
    assert failure.value.kind is ModelInputCompileFailureKind.TOOL_SURFACE_INVALID
    assert model.requests == []


def test_round9_native_planning_timeout_is_typed_before_provider_open(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _NativePlanningDeadlineModel([_text_stream("must not open")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    with pytest.raises(StructuredModelInputCompileError) as failure:
        asyncio.run(runner.run_turn(frozen_test_prompt("native planning expires")))
    assert failure.value.kind is ModelInputCompileFailureKind.DEADLINE_EXPIRED
    assert model.requests == []


def test_stage2_runner_commits_tool_message_and_attempt_before_invoke(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _MeasuredRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_tool_stream(), _text_stream("done", block="text:2")])
    tool = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tool),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    result = asyncio.run(runner.run_turn(frozen_test_prompt("run it")))
    assert result.final_text == "done"
    assert result.tool_call_count == 1
    assert len(tool.invocations) == 1
    assert repository.host_write_transactions == 5
    rows = repository.rehydrate_session(
        session_id=session_id, deadline_monotonic=monotonic() + 30
    )
    assert [row["entry_kind"] for row in rows] == [
        "USER_MESSAGE",
        "ASSISTANT_TOOL_REQUEST",
        "TOOL_RESULT",
        "ASSISTANT_MESSAGE",
    ]
    assert (
        model.requests[
            1
        ].compiled_input.canonical_input_identity.provider_input_through_sequence
        == 3
    )
    events = repository.events_after(
        session_id=session_id,
        after_sequence=0,
        limit=16,
        deadline_monotonic=monotonic() + 30,
    )
    assert tuple(row["event_type"] for row in events) == (
        "UserMessageAccepted",
        "AssistantToolRequestAccepted",
        "CapabilityDecisionAccepted",
        "ToolAttemptAccepted",
        "ToolResultAccepted",
        "AssistantMessageAccepted",
        "TurnCompleted",
    )


@pytest.mark.parametrize(
    "permission, response, expected_actor",
    [
        ("bypass-permissions", "SUBMIT", "runtime"),
        ("accept-edits", "SUBMIT", "runtime"),
        ("ask-permissions", "SUBMIT", "human"),
        ("read-only", "SUBMIT", "human"),
        ("read-only", "CANCEL", None),
        ("read-only", "NO_CONTROLLER", None),
    ],
)
def test_manage_capability_settles_through_real_attempt_and_model_followup(
    stage2_migrated_postgres_database,
    tmp_path,
    permission,
    response,
    expected_actor,
):
    from tests.test_capability_management_preparation import preparation
    from pulsara_agent.capability.management_form import (
        AcceptedCapabilityFormSubmission,
    )
    from pulsara_agent.conversation_kernel.interaction import ToolInteractionResolution
    from pulsara_agent.capability.mcp_management import LocalMcpTarget
    from pulsara_agent.primitives.permission import PermissionMode

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id, workspace_id = _name("session"), _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    service = preparation(tmp_path)
    target = LocalMcpTarget("fixture", service.workspace_root)
    args = {
        "action": "ADD_LOCAL_MCP",
        "scope": "WORKSPACE",
        "server_id": "fixture",
        "config": {
            "transport": {
                "type": "streamable_http",
                "endpoint": "https://example.org/mcp",
            }
        },
    }
    model = _ScriptedModel(
        [
            _named_tool_stream(
                tool_name="manage_capability",
                tool_call_id="call:manage",
                arguments=args,
            ),
            _text_stream("management settled; continuing normally"),
        ]
    )
    forms, adoptions = [], []

    class Interaction:
        async def request_capability_form(self, **kwargs):
            forms.append(kwargs["form"])
            assert service.mcp.inspect(target) is None
            if response == "NO_CONTROLLER":
                return ToolInteractionResolution(
                    "DENY", "interaction:no-controller", "no controller"
                )
            if response == "CANCEL":
                return ToolInteractionResolution(
                    "CANCEL", "interaction:cancel", "cancelled"
                )
            values = await kwargs["form"].prepare_submission({})
            return ToolInteractionResolution(
                "SUBMIT",
                "interaction:user-fixture",
                "submitted",
                capability_submission=AcceptedCapabilityFormSubmission(values),
            )

    class Adoption:
        async def adopt_capability_management_change(self, *, workspace_root):
            assert workspace_root == service.workspace_root
            assert service.mcp.inspect(target) is not None
            adoptions.append(workspace_root)
            return "RELOADED"

    live_bus = LiveAgentEventBus()
    tools = DirectKernelToolPort(
        workspace_root=service.workspace_root,
        host_owner_id="host:manage",
        session_id=session_id,
        live_bus=live_bus,
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
    )
    tools.bind_capability_management(service)
    seal_test_direct_tool_port(
        tools, interaction=Interaction(), capability_reload=Adoption()
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=tools,
        live_bus=live_bus,
        context_source_collector=StaticContextSourceCollector(),
    )

    async def run():
        try:
            return await runner.run_turn(
                frozen_test_prompt("configure this MCP"),
                requested_permission_mode=PermissionMode(permission),
            )
        finally:
            await tools.aclose(timeout_seconds=2)
            await service.mcp.aclose()

    result = asyncio.run(run())
    assert result.final_text == "management settled; continuing normally"
    assert len(forms) == (
        0 if permission in {"bypass-permissions", "accept-edits"} else 1
    )
    assert len(adoptions) == (0 if expected_actor is None else 1)
    rows = repository.rehydrate_session(
        session_id=session_id, deadline_monotonic=monotonic() + 30
    )
    assert sum(row["entry_kind"] == "TOOL_RESULT" for row in rows) == 1
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, deadline_monotonic=monotonic() + 30
    ) as connection:
        attempts = connection.execute(
            "SELECT actor_kind, authorization_kind FROM pulsara_v3.tool_execution_attempts WHERE session_id=%s",
            (session_id,),
        ).fetchall()
    assert attempts == (
        []
        if expected_actor is None
        else [(expected_actor, "human" if expected_actor == "human" else "machine")]
    )


def test_terminal_preflight_failure_returns_tool_result_and_model_finishes_turn(
    stage2_migrated_postgres_database,
    tmp_path: Path,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    workspace = tmp_path / "quick-workspace"
    workspace.mkdir()
    missing_workdir = tmp_path / "missing-workdir"
    sentinel = workspace / "must-not-exist"
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel(
        [
            _named_tool_stream(
                tool_name="terminal",
                tool_call_id="call:preflight",
                arguments={
                    "command": f"touch {sentinel}",
                    "workdir": str(missing_workdir),
                },
            ),
            _text_stream("terminal failed before launch; recovered normally"),
        ]
    )
    live_bus = LiveAgentEventBus()
    tools = DirectKernelToolPort(
        workspace_root=workspace,
        host_owner_id="host:terminal-preflight",
        session_id=session_id,
        live_bus=live_bus,
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
    )
    seal_test_direct_tool_port(tools)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=tools,
        live_bus=live_bus,
        context_source_collector=StaticContextSourceCollector(),
    )

    async def exercise():
        try:
            return await runner.run_turn(
                frozen_test_prompt("recover from terminal preflight failure")
            )
        finally:
            await tools.aclose(timeout_seconds=2)

    result = asyncio.run(exercise())
    assert result.final_text == "terminal failed before launch; recovered normally"
    assert result.tool_call_count == 1
    assert not sentinel.exists()
    rows = repository.rehydrate_session(
        session_id=session_id, deadline_monotonic=monotonic() + 30
    )
    assert [row["entry_kind"] for row in rows] == [
        "USER_MESSAGE",
        "ASSISTANT_TOOL_REQUEST",
        "TOOL_RESULT",
        "ASSISTANT_MESSAGE",
    ]
    tool_result = next(row for row in rows if row["entry_kind"] == "TOOL_RESULT")
    payload = json.loads(tool_result["inline_content"])
    assert payload["status"] == "error"
    assert payload["exit_code"] == -1
    assert "terminal preflight failed (ValueError)" in payload["error"]
    assert "terminal workdir does not exist" in payload["error"]
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT status, terminal_reason FROM pulsara_v3.turns "
            "WHERE session_id = %s",
            (session_id,),
        ).fetchone() == ("COMPLETED", "COMPLETED")


def test_lightweight_todo_runs_through_canonical_tool_result_settlement(
    stage2_migrated_postgres_database,
    tmp_path,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    live_bus = LiveAgentEventBus()
    tools = DirectKernelToolPort(
        workspace_root=tmp_path,
        host_owner_id="host:test",
        session_id=session_id,
        live_bus=live_bus,
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
    )
    seal_test_direct_tool_port(tools)

    async def finalize_todo(
        prepared: PreparedTodoRootRunActivation, accepted: AcceptedEntry
    ) -> None:
        assert accepted.turn_id == prepared.exact_turn_id
        tools.todo_owner.activate_root_run(prepared)

    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel(
            [_todo_tool_stream(), _text_stream("done", block="text:todo-final")]
        ),
        tools=tools,
        live_bus=live_bus,
        context_source_collector=StaticContextSourceCollector(),
        todo_admission_finalizer=finalize_todo,
    )

    async def exercise():
        try:
            return await runner.run_turn(
                frozen_test_prompt("maintain an exact TODO checklist")
            )
        finally:
            await tools.aclose(timeout_seconds=2)

    result = asyncio.run(exercise())
    assert result.final_text == "done"
    snapshot = tools.todo_owner.snapshot(
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    assert snapshot is not None and snapshot.revision == 1
    assert [item.text for item in snapshot.ordered_items] == [
        "Inspect exact path",
        "Run retained gates",
    ]
    rows = repository.rehydrate_session(
        session_id=session_id, deadline_monotonic=monotonic() + 30
    )
    tool_result = next(row for row in rows if row["entry_kind"] == "TOOL_RESULT")
    assert b"Inspect exact path" not in tool_result["inline_content"]
    acknowledgement = json.loads(tool_result["inline_content"])
    assert acknowledgement == {
        "counts": {"completed": 1, "in_progress": 1, "pending": 0, "total": 2},
        "status": "UPDATED",
    }
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        artifact = connection.execute(
            "SELECT output_artifact_disposition, output_display_kind "
            "FROM pulsara_v3.tool_results "
            "WHERE session_id = %s AND result_entry_id = %s",
            (session_id, tool_result["id"]),
        ).fetchone()
    assert artifact == ("NOT_REQUIRED", "COMPLETE")


def test_stage2_live_bus_overflow_is_nonblocking_and_returns_gap() -> None:
    bus = LiveAgentEventBus(maximum_events=2, maximum_payload_bytes=128)
    observer, generation, revision = bus.subscribe()
    assert generation == 1 and revision == 0
    for index in range(4):
        assert (
            bus.offer_nowait(
                event_type=LiveEventType.TEXT_DELTA,
                session_id="session",
                turn_id="turn",
                draft_identity="draft",
                payload=TextDeltaPayload(block_identity="block:1", delta=str(index)),
            )
            is not None
        )
    observed = bus.observe(observer, after_revision=0, maximum_events=2)
    assert observed.kind is LiveObservationKind.GAP
    assert observed.latest_revision == 4
    bus.close()
    assert (
        bus.offer_nowait(
            event_type=LiveEventType.TEXT_END,
            session_id="session",
            turn_id="turn",
            draft_identity="draft",
            payload=TextEndPayload(
                block_identity="block:1",
                final_text="",
                utf8_bytes=0,
                digest=live_digest(""),
            ),
        )
        is None
    )


def test_stage2_live_cursor_is_client_owned_and_lost_response_is_repeatable() -> None:
    bus = LiveAgentEventBus(maximum_events=8, maximum_payload_bytes=4096)
    observer, generation, revision = bus.subscribe()
    assert generation == 1 and revision == 0
    for index in range(3):
        bus.offer_nowait(
            event_type=LiveEventType.TEXT_DELTA,
            session_id="session",
            turn_id="turn",
            draft_identity="entry:future",
            payload=TextDeltaPayload(block_identity="block:1", delta=str(index)),
        )
    first = bus.observe(observer, after_revision=0, maximum_events=2)
    repeated = bus.observe(observer, after_revision=0, maximum_events=2)
    assert first == repeated
    assert first.latest_revision == 2
    tail = bus.observe(observer, after_revision=2, maximum_events=2)
    assert tuple(item.revision for item in tail.events) == (3,)
    settlement = bus.offer_settlement_nowait(
        kind=LiveSettlementKind.COMMITTED,
        session_id="session",
        turn_id="turn",
        draft_identity="entry:future",
        committed_entry_id="entry:future",
    )
    assert settlement is not None
    terminal = bus.observe(observer, after_revision=3, maximum_events=2)
    assert terminal.events == ()
    assert terminal.settlements == (settlement,)


def test_stage2_no_attempt_result_is_committed_before_any_physical_invoke(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_tool_stream(), _text_stream("done", block="text:2")])
    tools = _DenyingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tools),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    asyncio.run(runner.run_turn(frozen_test_prompt("do not dispatch")))
    assert tools.invocations == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT attempt_id, result_state FROM pulsara_v3.tool_results WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (None, "PERMISSION_DENIED")


def test_stage2_lost_assistant_commit_ack_exact_confirms_single_winner(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _LostAssistantAckRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_text_stream("one winner")]),
        tools=StructuredToolPort(_AssertingTool(provider, session_id)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    accepted = asyncio.run(runner.run_turn(frozen_test_prompt("confirm the winner")))
    assert accepted is not None
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind = 'ASSISTANT_MESSAGE'",
            (session_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT status, final_entry_id FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone() == ("COMPLETED", accepted.final_entry_id)


def test_round5a1_assistant_settlement_retries_same_candidate_after_transient_none(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _TransientNoneAssistantRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_text_stream("settled after NONE")]),
        tools=StructuredToolPort(_AssertingTool(provider, session_id)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    result = asyncio.run(
        runner.run_turn(frozen_test_prompt("retry the exact assistant candidate"))
    )
    assert result.final_text == "settled after NONE"
    assert repository.commit_calls == 2
    assert repository.confirm_calls == 1


def test_round5a1_assistant_settlement_conflict_preserves_epoch_without_hanging(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _ConflictingAssistantCommitRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_text_stream("must not win")]),
        tools=StructuredToolPort(_AssertingTool(provider, session_id)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )

    async def exercise() -> None:
        with pytest.raises(ConversationKernelConflict):
            await asyncio.wait_for(
                runner.run_turn(frozen_test_prompt("conflicting candidate")), timeout=5
            )

    asyncio.run(exercise())
    scope = ProviderInputContinuityScope(
        session_id=session_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    assert runner._continuity.current_view(scope) is not None
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind = 'ASSISTANT_MESSAGE'",
            (session_id,),
        ).fetchone() == (0,)


def test_round5a1_completed_tool_item_then_incomplete_has_zero_canonical_effect(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    calls = 0

    async def stream(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            for item in _tool_stream():
                yield item
            raise ProviderModelOutputIncomplete(
                ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT
            )
        for item in _text_stream("recovered", block="text:second"):
            yield item

    model = CallbackScriptedKernelModel(stream)
    tools = _AssertingTool(provider, session_id)
    bus = LiveAgentEventBus()
    observer_id, _generation, _revision = bus.subscribe()
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tools),
        live_bus=bus,
        context_source_collector=StaticContextSourceCollector(),
    )

    with pytest.raises(ProviderModelOutputIncomplete):
        asyncio.run(
            runner.run_turn(frozen_test_prompt("produce a virtual tool request"))
        )
    assert tools.invocations == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts AS attempt "
            "JOIN pulsara_v3.transcript_entries AS entry "
            "ON entry.session_id = attempt.session_id "
            "AND entry.id = attempt.assistant_entry_id "
            "WHERE attempt.session_id = %s "
            "AND entry.conversation_scope_kind = 'SUBAGENT_TASK'",
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind IN "
            "('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')",
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT terminal_reason FROM pulsara_v3.turns "
            "WHERE session_id = %s ORDER BY accepted_at LIMIT 1",
            (session_id,),
        ).fetchone() == ("MODEL_OUTPUT_TOKEN_LIMIT_REACHED",)
    observed = bus.observe(observer_id, after_revision=0, maximum_events=64)
    assert any(item.kind is LiveSettlementKind.ABORTED for item in observed.settlements)

    recovered = asyncio.run(
        runner.run_turn(frozen_test_prompt("continue after the failed response"))
    )
    assert recovered.final_text == "recovered"
    previous_outcomes = [
        decode_runtime_observation(message)
        for message in model.requests[1].compiled_input.messages
        if message.role is MessageRole.USER
        and message.content
        and "pulsara_runtime_observation" in join_text_content(message.content)
    ]
    assert any(
        item.source_kind is ContextSourceKind.PREVIOUS_TURN_OUTCOME
        and json.loads(item.body)["kind"] == "EXECUTION_FAILED"
        for item in previous_outcomes
    )


def test_k3_output_resource_gate_precedes_assistant_and_tool_effects(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    tools = _AssertingTool(provider, session_id)
    bus = LiveAgentEventBus(maximum_events=4_096)
    observer_id, _generation, _revision = bus.subscribe()
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_many_tool_stream(256)]),
        tools=StructuredToolPort(tools),
        live_bus=bus,
        context_source_collector=StaticContextSourceCollector(),
    )

    with pytest.raises(OutputResourceInterruption) as caught:
        asyncio.run(runner.run_turn(frozen_test_prompt("produce an oversized batch")))

    assert caught.value.reason == "CANONICAL_BYTES"
    assert caught.value.quote.canonical_upper_after > 16 << 20
    assert tools.invocations == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind IN "
            "('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST', 'TOOL_RESULT')",
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts "
            "WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT terminal_reason FROM pulsara_v3.turns "
            "WHERE session_id = %s ORDER BY accepted_at LIMIT 1",
            (session_id,),
        ).fetchone() == ("PROVIDER_OUTPUT_RESOURCE_EXHAUSTED",)
    observed = bus.observe(observer_id, after_revision=0, maximum_events=2_048)
    assert any(
        item.kind is LiveSettlementKind.ABORTED
        and item.reason_code == "OUTPUT_RESOURCE_CANONICAL_BYTES"
        for item in observed.settlements
    )


def test_round5a1_assistant_settlement_survives_caller_cancellation(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _BlockingAssistantCommitRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_text_stream("accepted exactly once")]),
        tools=StructuredToolPort(_AssertingTool(provider, session_id)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )

    async def exercise() -> None:
        task = asyncio.create_task(
            runner.run_turn(frozen_test_prompt("cancel the API waiter"))
        )
        assert await asyncio.to_thread(repository.started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        repository.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind = 'ASSISTANT_MESSAGE'",
            (session_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone() == ("COMPLETED",)


def test_round5a1_replay_fragment_binds_only_after_exact_assistant_winner(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _LostAssistantAckRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    profile = RouteWireProfile(
        id="test:chat-replay",
        wire_api="openai_chat_completions",
    )
    model = DirectKernelModelPort(
        model_runtime=test_model_runtime(
            api_key="sk-fixture-secret",
            base_url="https://example.invalid/v1",
            model_id="test-pro",
            wire_api="openai_chat_completions",
            route_wire_profile=profile,
        ),
    )
    model._model_runtime.transport_registry(model._transport_timeout).get(  # noqa: SLF001
        "openai_chat_completions"
    )._adapter._mock_chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"reasoning_content": "opaque-test-reasoning"},
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "answer"},
                    "finish_reason": "stop",
                }
            ]
        },
    ]
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    first = asyncio.run(runner.run_turn(frozen_test_prompt("first")))
    scope = ProviderInputContinuityScope(
        session_id=session_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    first_view = runner._continuity.current_view(scope)
    assert first_view is not None
    assert tuple(
        item.assistant_entry_id for item in first_view.assistant_replay_fragments
    ) == (first.final_entry_id,)

    second = asyncio.run(runner.run_turn(frozen_test_prompt("second")))
    assert second.final_text == "answer"
    second_view = runner._continuity.current_view(scope)
    assert second_view is not None
    assert tuple(
        item.assistant_entry_id for item in second_view.wire_input_plan.replacements
    ) == (first.final_entry_id,)
    assert tuple(
        item.assistant_entry_id for item in second_view.assistant_replay_fragments
    ) == (first.final_entry_id, second.final_entry_id)


@pytest.mark.parametrize(
    ("api", "scripts", "expected_text"),
    (
        (
            "openai_chat_completions",
            _round5a1_chat_scripts(),
            "chat final",
        ),
        (
            "openai_responses",
            _round5a1_responses_scripts(),
            "responses final",
        ),
    ),
)
def test_round5a1_complete_tool_loop_replays_exact_reasoning_on_second_call(
    stage2_migrated_postgres_database,
    api: str,
    scripts: tuple[tuple[dict[str, object], ...], ...],
    expected_text: str,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    profile = RouteWireProfile(
        id=f"test:{api}:tool-loop",
        wire_api=api,
    )
    model = _SequencedDirectKernelModel(
        model_runtime=test_model_runtime(
            api_key="sk-fixture-secret",
            base_url="https://example.invalid/v1",
            model_id="test-pro",
            wire_api=api,
            route_wire_profile=profile,
        ),
        scripts=scripts,
    )
    tools = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tools),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )

    result = asyncio.run(
        runner.run_turn(frozen_test_prompt("use the virtual terminal and finish"))
    )

    assert result.final_text == expected_text
    assert result.model_call_count == 2
    assert result.tool_call_count == 1
    assert len(tools.invocations) == 1
    assert len(model.requests) == 2
    second_plan = model.requests[1].wire_input_plan
    assert len(second_plan.replacements) == 1
    replacement = second_plan.replacements[0]
    second_wire = tuple(
        thaw_json(item) for item in second_plan.materialization.ordered_input_items
    )
    if api == "openai_chat_completions":
        replay_items = tuple(
            item
            for item in second_wire
            if isinstance(item, dict)
            and item.get("role") == "assistant"
            and item.get("reasoning_content") == "opaque-chat-tool"
        )
        assert len(replay_items) == 1
        assert replay_items[0]["tool_calls"][0]["id"] == "call:1"
    else:
        replay_types = tuple(
            item.get("type")
            for item in second_wire
            if isinstance(item, dict)
            and item.get("id") in {"reasoning:tool", "message:tool", "function:1"}
        )
        assert replay_types == ("reasoning", "message", "function_call")
    scope = ProviderInputContinuityScope(
        session_id=session_id,
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    view = runner._continuity.current_view(scope)
    assert view is not None
    first_fragment = next(
        item
        for item in view.assistant_replay_fragments
        if item.assistant_entry_id != result.final_entry_id
    )
    assert replacement.replay_fragment_fingerprint == (
        first_fragment.fragment_fingerprint
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts "
            "WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("api", "script"),
    (
        ("openai_chat_completions", _round5a1_chat_scripts()[1]),
        ("openai_responses", _round5a1_responses_scripts()[1]),
    ),
)
def test_round5a2_fresh_host_rehydrates_durable_native_replay(
    stage2_migrated_postgres_database,
    api: str,
    script: tuple[dict[str, object], ...],
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    first_lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host-one"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    profile = RouteWireProfile(
        id=f"test:{api}:durable-restart",
        wire_api=api,
    )

    def model() -> _SequencedDirectKernelModel:
        return _SequencedDirectKernelModel(
            model_runtime=test_model_runtime(
                api_key="sk-fixture-secret",
                base_url="https://example.invalid/v1",
                model_id="test-pro",
                wire_api=api,
                route_wire_profile=profile,
            ),
            scripts=(script,),
        )

    first_model = model()
    first_runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=first_lease,
        model=first_model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    first = asyncio.run(
        first_runner.run_turn(frozen_test_prompt("first native answer"))
    )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.provider_assistant_replay_fragments "
            "WHERE session_id = %s AND assistant_entry_id = %s",
            (session_id, first.final_entry_id),
        ).fetchone() == (1,)

    replacement_repository = ConversationKernelRepository(provider)
    replacement_lease = _acquire_bound_host_writer(
        replacement_repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host-two"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    second_model = model()
    replacement_runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=replacement_repository,
        writer_lease=replacement_lease,
        model=second_model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    cold_recorder = _RecordingColdEpochAssembler(
        replacement_runner._provider_dispatch._cold_epoch_assembler
    )
    replacement_runner._provider_dispatch._cold_epoch_assembler = cold_recorder
    asyncio.run(
        replacement_runner.run_turn(frozen_test_prompt("continue after restart"))
    )

    assert len(second_model.requests) == 1
    wire_plan = second_model.requests[0].wire_input_plan
    assert wire_plan.provider_replay_hydration_fingerprint is not None
    assert tuple(item.assistant_entry_id for item in wire_plan.replacements) == (
        first.final_entry_id,
    )
    assert len(cold_recorder.semantic_seeds) == 1
    assert isinstance(cold_recorder.semantic_seeds[0], CanonicalColdContinuationSeed)
    assert cold_recorder.finalized == 1


def test_round5a2_selected_corruption_fails_before_open_but_incompatible_target_is_cold(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host-one"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    profile = RouteWireProfile(
        id="test:chat:durable-corruption",
        wire_api="openai_chat_completions",
    )

    def model(*, base_url: str) -> _SequencedDirectKernelModel:
        return _SequencedDirectKernelModel(
            model_runtime=test_model_runtime(
                api_key="sk-fixture-secret",
                base_url=base_url,
                model_id="test-pro",
                wire_api="openai_chat_completions",
                route_wire_profile=profile,
            ),
            scripts=(_round5a1_chat_scripts()[1],),
        )

    first_runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model(base_url="https://example.invalid/v1"),
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    first = asyncio.run(
        first_runner.run_turn(frozen_test_prompt("create native replay"))
    )
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.provider_assistant_replay_fragments "
            "SET payload_digest = %s WHERE session_id = %s AND assistant_entry_id = %s",
            ("sha256:" + "0" * 64, session_id, first.final_entry_id),
        )

    exact_repository = ConversationKernelRepository(provider)
    exact_lease = _acquire_bound_host_writer(
        exact_repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host-two"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    exact_model = model(base_url="https://example.invalid/v1")
    exact_runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=exact_repository,
        writer_lease=exact_lease,
        model=exact_model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    with pytest.raises(StructuredModelInputCompileError) as captured:
        asyncio.run(
            exact_runner.run_turn(frozen_test_prompt("same target must fail closed"))
        )
    assert captured.value.kind is ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
    assert exact_model.requests == []

    cold_repository = ConversationKernelRepository(provider)
    cold_lease = _acquire_bound_host_writer(
        cold_repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host-three"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    cold_model = model(base_url="https://different.example.invalid/v1")
    cold_runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=cold_repository,
        writer_lease=cold_lease,
        model=cold_model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    cold_reader = _RecordingReplayHydrationReader(
        cold_runner._provider_dispatch._input_reader
    )
    cold_runner._provider_dispatch._input_reader = cold_reader
    cold = asyncio.run(
        cold_runner.run_turn(
            frozen_test_prompt("incompatible target uses public history")
        )
    )
    assert cold.final_text == "chat final"
    assert len(cold_model.requests) == 1
    assert cold_model.requests[0].wire_input_plan.replacements == ()
    assert (
        cold_model.requests[0].wire_input_plan.provider_replay_hydration_fingerprint
        is None
    )
    assert len(cold_reader.hydration_deadlines) == 1
    assert cold_reader.hydration_selected_counts == [0]


@pytest.mark.parametrize(
    ("api", "abrupt"),
    (
        ("openai_chat_completions", False),
        ("openai_responses", True),
    ),
)
def test_round5a2_os_process_restart_uses_only_durable_native_replay(
    stage2_migrated_postgres_database,
    api: str,
    abrupt: bool,
) -> None:
    session_id = _name("session")
    workspace_id = _name("workspace")
    probe = Path(__file__).parent / "fixtures" / "round5a2_restart_probe.py"
    environment = os.environ.copy()
    environment["ROUND5A2_TEST_RUNTIME_DSN"] = (
        stage2_migrated_postgres_database.runtime_dsn
    )
    repository_root = str(Path(__file__).parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in (repository_root, environment.get("PYTHONPATH", "")) if item
    )

    def invoke(mode: str, *, kill_after_commit: bool = False) -> dict[str, object]:
        command = [
            sys.executable,
            str(probe),
            mode,
            api,
            session_id,
            workspace_id,
        ]
        if kill_after_commit:
            command.append("--abrupt")
        completed = subprocess.run(
            command,
            cwd=repository_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        return json.loads(completed.stdout.strip().splitlines()[-1])

    created = invoke("create", kill_after_commit=abrupt)
    assert created["completed"] is True
    assert created["hydrated"] is False
    continued = invoke("continue")
    assert continued["completed"] is True
    assert continued["hydrated"] is True
    assert continued["replacement_count"] == 1


@pytest.mark.parametrize("fail_hydration", (False, True))
def test_round5a2_selected_hydration_reuses_dispatch_deadline_and_opens_once_or_zero(
    stage2_migrated_postgres_database,
    fail_hydration: bool,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    profile = RouteWireProfile(
        id="test:chat:one-planning-deadline",
        wire_api="openai_chat_completions",
    )

    def model() -> _SequencedDirectKernelModel:
        return _SequencedDirectKernelModel(
            model_runtime=test_model_runtime(
                api_key="sk-fixture-secret",
                base_url="https://example.invalid/v1",
                model_id="test-pro",
                wire_api="openai_chat_completions",
                route_wire_profile=profile,
            ),
            scripts=(_round5a1_chat_scripts()[1],),
        )

    first_lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host-one"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    asyncio.run(
        ConversationKernelRunner(
            model_resolution_snapshot_provider=test_model_resolution_snapshot,
            repository=repository,
            writer_lease=first_lease,
            model=model(),
            tools=StructuredToolPort(
                _AssertingTool(provider, session_id), tool_names=()
            ),
            live_bus=LiveAgentEventBus(),
            context_source_collector=StaticContextSourceCollector(),
        ).run_turn(frozen_test_prompt("create selected replay"))
    )

    replacement_repository = ConversationKernelRepository(provider)
    replacement_lease = _acquire_bound_host_writer(
        replacement_repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host-two"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    reader = _RecordingReplayHydrationReader(
        CanonicalProviderInputReader(provider), fail_hydration=fail_hydration
    )
    replacement_model = model()
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=replacement_repository,
        writer_lease=replacement_lease,
        model=replacement_model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        input_reader=reader,
    )
    if fail_hydration:
        with pytest.raises(StructuredModelInputCompileError) as captured:
            asyncio.run(
                runner.run_turn(
                    frozen_test_prompt("deadline expires before provider open")
                )
            )
        assert captured.value.kind is ModelInputCompileFailureKind.DEADLINE_EXPIRED
        assert replacement_model.requests == []
    else:
        asyncio.run(
            runner.run_turn(
                frozen_test_prompt("same deadline continues into hydration")
            )
        )
        assert len(replacement_model.requests) == 1
        assert (
            replacement_model.requests[
                0
            ].wire_input_plan.provider_replay_hydration_fingerprint
            is not None
        )
    assert len(reader.dispatch_deadlines) == (1 if fail_hydration else 3)
    assert len(reader.hydration_deadlines) == 1
    assert reader.dispatch_deadlines[0] == reader.hydration_deadlines[0]
    if not fail_hydration:
        # Post-writer activation performs one exact canonical reread under its
        # fresh safe-point deadline, then Reader itself issues the authority
        # carrier from a second fresh read; neither rehydrates selected replay.
        assert reader.dispatch_deadlines[1] >= reader.dispatch_deadlines[0]
        assert reader.dispatch_deadlines[2] == reader.dispatch_deadlines[1]


def test_round5a1_replay_fragment_capacity_fails_before_assistant_commit(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _CountingAssistantCommitRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    profile = RouteWireProfile(
        id="test:chat:near-bound",
        wire_api="openai_chat_completions",
    )
    model = _SequencedDirectKernelModel(
        model_runtime=test_model_runtime(
            api_key="sk-fixture-secret",
            base_url="https://example.invalid/v1",
            model_id="test-pro",
            wire_api="openai_chat_completions",
            route_wire_profile=profile,
        ),
        scripts=(_round5a1_chat_scripts()[1],),
    )
    continuity = _NearBoundReplayContinuityOwner(
        root_lease_source=_issue_root_bootstrap_lease_source(lease)
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        continuity_owner=continuity,
    )

    with pytest.raises(
        ProviderInputContinuityConflict,
        match="exhausts the epoch byte bound",
    ):
        asyncio.run(
            runner.run_turn(frozen_test_prompt("reject the oversized replay carrier"))
        )

    assert repository.assistant_commit_calls == 0
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind LIKE 'ASSISTANT%%'",
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone() == ("INTERRUPTED",)


def test_stage2_lost_tool_request_ack_confirms_before_single_dispatch(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _LostAssistantAckRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    tools = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_tool_stream(), _text_stream("done", block="text:2")]),
        tools=StructuredToolPort(tools),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    asyncio.run(runner.run_turn(frozen_test_prompt("dispatch exactly once")))
    assert len(tools.invocations) == 1
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND entry_kind = 'ASSISTANT_TOOL_REQUEST'",
            (session_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts "
            "WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (1,)


def test_stage2_subagent_runner_produces_durable_message_child(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    parent_turn_id = _name("turn")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=parent_turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        permission_snapshot_id=_name("permission-snapshot"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text("delegate"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    task_id = accept_active_subagent_fixture(
        repository,
        lease,
        parent_turn_id=parent_turn_id,
        objective="produce one message",
    )
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_text_stream("child answer")]),
        tools=StructuredToolPort(_AssertingTool(provider, session_id)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        subagent_runtime=Round10TestSubagentRuntime(
            session_id=session_id,
            task_id=task_id,
            parent_turn_id=parent_turn_id,
            objective="produce one message",
        ),
    )
    result = asyncio.run(run_admitted_subagent_fixture(runner, task_id))
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        children = connection.execute(
            "SELECT child_kind, child_ordinal, entry_id "
            "FROM pulsara_v3.subagent_task_children "
            "WHERE session_id = %s AND task_id = %s "
            "ORDER BY child_ordinal",
            (session_id, task_id),
        ).fetchall()
        assert children == [
            ("MESSAGE", 0, result.final_entry_id),
            ("RESULT", 1, result.final_entry_id),
        ]
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.agent_events "
            "WHERE session_id = %s AND event_type = 'SubagentMessageAccepted'",
            (session_id,),
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    ("api", "scripts", "expected_text"),
    (
        ("openai_chat_completions", _round5a1_chat_scripts(), "chat final"),
        ("openai_responses", _round5a1_responses_scripts(), "responses final"),
    ),
)
def test_round10_child_cold_seed_then_same_epoch_wire_prefix_is_exact(
    stage2_migrated_postgres_database,
    api: str,
    scripts: tuple[tuple[dict[str, object], ...], ...],
    expected_text: str,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    parent_turn_id = _name("turn")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=parent_turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        permission_snapshot_id=_name("permission-snapshot"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text("delegate an exact child tool loop"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    objective = "use the virtual terminal and finish"
    task_id = accept_active_subagent_fixture(
        repository,
        lease,
        parent_turn_id=parent_turn_id,
        objective=objective,
    )
    profile = RouteWireProfile(
        id=f"test:{api}:round10-child",
        wire_api=api,
    )
    model = _SequencedDirectKernelModel(
        model_runtime=test_model_runtime(
            api_key="sk-fixture-secret",
            base_url="https://example.invalid/v1",
            model_id="test-pro",
            wire_api=api,
            route_wire_profile=profile,
        ),
        scripts=scripts,
    )
    tools = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(tools),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        subagent_runtime=Round10TestSubagentRuntime(
            session_id=session_id,
            task_id=task_id,
            parent_turn_id=parent_turn_id,
            objective=objective,
        ),
    )
    cold = _RecordingColdEpochAssembler(runner._provider_dispatch._cold_epoch_assembler)
    runner._provider_dispatch._cold_epoch_assembler = cold

    result = asyncio.run(run_admitted_subagent_fixture(runner, task_id))

    assert result.final_text == expected_text
    assert result.model_call_count == 2
    assert result.tool_call_count == 1
    assert len(tools.invocations) == 1
    assert len(cold.semantic_seeds) == 1
    assert isinstance(cold.semantic_seeds[0], SubagentInitialSeed)
    assert cold.finalized == 1
    assert len(model.requests) == 2
    first, second = model.requests
    assert (
        first.compiled_input.canonical_input_identity.scope_subagent_task_id == task_id
    )
    assert first.wire_input_plan.wire_system_fingerprint == (
        second.wire_input_plan.wire_system_fingerprint
    )
    assert first.wire_input_plan.materialization.tool_items == (
        second.wire_input_plan.materialization.tool_items
    )
    first_wire = first.wire_input_plan.materialization.ordered_input_items
    second_wire = second.wire_input_plan.materialization.ordered_input_items
    assert second_wire[: len(first_wire)] == first_wire
    assert len(second_wire) > len(first_wire)


def test_round10_sole_report_result_atomically_completes_child_without_second_model_call(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    parent_turn_id = _name("turn")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=parent_turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        permission_snapshot_id=_name("permission-snapshot"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text("delegate explicit result"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    objective = "produce a sole explicit result"
    task_id = accept_active_subagent_fixture(
        repository,
        lease,
        parent_turn_id=parent_turn_id,
        objective=objective,
    )
    model = _ScriptedModel(
        [
            _named_tool_stream(
                tool_name="report_agent_result",
                tool_call_id="call:report",
                arguments={
                    "summary": "exact explicit summary",
                    "output_preview": "file.py:42",
                    "diagnostics": [{"code": "CHECKED", "severity": "info"}],
                },
            )
        ]
    )
    delegate = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(
            delegate,
            tool_names=("report_agent_result",),
        ),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        subagent_runtime=Round10TestSubagentRuntime(
            session_id=session_id,
            task_id=task_id,
            parent_turn_id=parent_turn_id,
            objective=objective,
        ),
    )
    original_accept = repository.accept_explicit_subagent_result
    lost_ack = False

    def commit_then_timeout(*args: object, **kwargs: object):
        nonlocal lost_ack
        accepted = original_accept(*args, **kwargs)
        if not lost_ack:
            lost_ack = True
            raise TimeoutError("explicit result commit ACK lost")
        return accepted

    monkeypatch.setattr(
        repository,
        "accept_explicit_subagent_result",
        commit_then_timeout,
    )
    result = asyncio.run(run_admitted_subagent_fixture(runner, task_id))
    assert lost_ack
    assert result.final_text == "exact explicit summary"
    assert result.model_call_count == 1
    assert result.tool_call_count == 1
    assert len(model.requests) == 1
    assert delegate.invocations == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.turns "
            "WHERE session_id=%s AND conversation_scope_kind='SUBAGENT_TASK'",
            (session_id,),
        ).fetchone() == ("COMPLETED",)
        assert connection.execute(
            "SELECT t.status, c.result_source, c.summary, c.output_preview "
            "FROM pulsara_v3.subagent_tasks AS t "
            "JOIN pulsara_v3.subagent_task_children AS c "
            "ON c.session_id=t.session_id AND c.task_id=t.id "
            "AND c.child_kind='RESULT' "
            "WHERE t.session_id=%s AND t.id=%s",
            (session_id, task_id),
        ).fetchone() == (
            "COMPLETED",
            "EXPLICIT",
            "exact explicit summary",
            "file.py:42",
        )
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts AS a "
            "JOIN pulsara_v3.transcript_entries AS e "
            "ON e.session_id=a.session_id AND e.id=a.assistant_entry_id "
            "WHERE a.session_id=%s AND e.conversation_scope_kind='SUBAGENT_TASK'",
            (session_id,),
        ).fetchone() == (1,)

    accepted = asyncio.run(
        runner.accept_subagent_completion(
            turn_id=parent_turn_id,
            task_id=task_id,
            command_id=_name("command"),
            actor_id="host:test",
            deadline_monotonic=monotonic() + 30,
            admitted_writer=CompactionWriteReservation(ModelInputScopeKind.ROOT, None),
        )
    )
    assert accepted is not None
    safe_point = ProviderSafePointCoordinator(repository=repository, guard=lease.guard)
    handle = safe_point.freeze_provider_input(
        turn_id=parent_turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    try:
        materialized = CanonicalProviderInputReader(provider).read_frozen_snapshot(
            handle.cut,
            deadline_monotonic=monotonic() + 30,
        )
    finally:
        handle.close()
    envelope = json.loads(provider_input_item_text(materialized.items[-1]))[
        "pulsara_inter_agent_message"
    ]
    assert envelope["content_semantics"] == "ADVISORY_COLLABORATION_DATA"
    assert "Its source is recorded" in envelope["handling"]
    assert envelope["content"]["result"]["summary"] == "exact explicit summary"
    assert (
        materialized.items[-1].input_origin
        is CanonicalInputOriginKind.INTER_AGENT_MESSAGE
    )


def test_round10_mixed_report_batch_has_zero_attempt_and_physical_effect_then_recovers(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    parent_turn_id = _name("turn")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=parent_turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        permission_snapshot_id=_name("permission-snapshot"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text("delegate mixed report rejection"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    objective = "recover after an invalid mixed result batch"
    task_id = accept_active_subagent_fixture(
        repository,
        lease,
        parent_turn_id=parent_turn_id,
        objective=objective,
    )
    mixed = _named_tool_stream(
        tool_name="report_agent_result",
        tool_call_id="call:report-mixed",
        arguments={"summary": "must not win"},
    ) + _named_tool_stream(
        tool_name="terminal",
        tool_call_id="call:effect-mixed",
        arguments={"command": "must-not-dispatch"},
    )
    model = _ScriptedModel([mixed, _text_stream("recovered inferred result")])
    delegate = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(
            delegate,
            tool_names=("report_agent_result", "terminal"),
        ),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        subagent_runtime=Round10TestSubagentRuntime(
            session_id=session_id,
            task_id=task_id,
            parent_turn_id=parent_turn_id,
            objective=objective,
        ),
    )
    result = asyncio.run(run_admitted_subagent_fixture(runner, task_id))
    assert result.final_text == "recovered inferred result"
    assert result.model_call_count == 2
    assert delegate.invocations == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts AS a "
            "JOIN pulsara_v3.transcript_entries AS e "
            "ON e.session_id=a.session_id AND e.id=a.assistant_entry_id "
            "WHERE a.session_id=%s AND e.conversation_scope_kind='SUBAGENT_TASK'",
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT result_source, summary "
            "FROM pulsara_v3.subagent_task_children "
            "WHERE session_id=%s AND task_id=%s AND child_kind='RESULT'",
            (session_id, task_id),
        ).fetchone() == ("INFERRED", "recovered inferred result")


def test_round5a1_subagent_incomplete_response_has_no_assistant_or_tool_effect(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    parent_turn_id = _name("turn")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=parent_turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        permission_snapshot_id=_name("permission-snapshot"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=test_model_binding(test_model_runtime()),
        content=FrozenPromptContent.text("delegate"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    objective = "produce one bounded answer"
    task_id = accept_active_subagent_fixture(
        repository,
        lease,
        parent_turn_id=parent_turn_id,
        objective=objective,
    )

    async def incomplete_stream(_request):
        yield TextStartPayload("text:partial")
        yield TextDeltaPayload("text:partial", "partial")
        raise ProviderModelOutputIncomplete(
            ProviderOutputIncompleteReason.OUTPUT_TOKEN_LIMIT
        )

    tools = _AssertingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=CallbackScriptedKernelModel(incomplete_stream),
        tools=StructuredToolPort(tools),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
        subagent_runtime=Round10TestSubagentRuntime(
            session_id=session_id,
            task_id=task_id,
            parent_turn_id=parent_turn_id,
            objective=objective,
        ),
    )

    with pytest.raises(ProviderModelOutputIncomplete):
        asyncio.run(run_admitted_subagent_fixture(runner, task_id))
    assert tools.invocations == []
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND conversation_scope_kind = "
            "'SUBAGENT_TASK' AND entry_kind IN "
            "('ASSISTANT_MESSAGE', 'ASSISTANT_TOOL_REQUEST')",
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts AS attempt "
            "JOIN pulsara_v3.transcript_entries AS entry "
            "ON entry.session_id = attempt.session_id "
            "AND entry.id = attempt.assistant_entry_id "
            "WHERE attempt.session_id = %s "
            "AND entry.conversation_scope_kind = 'SUBAGENT_TASK'",
            (session_id,),
        ).fetchone() == (0,)


def test_stage2_confirmation_without_controller_keeps_policy_and_result_closed(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _MeasuredRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    tools = _ConfirmationWithoutControllerTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_tool_stream(), _text_stream("done", block="text:2")]),
        tools=StructuredToolPort(tools),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    asyncio.run(runner.run_turn(frozen_test_prompt("ask before dispatch")))
    assert tools.invocations == []
    assert repository.host_write_transactions == 5
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            """
            SELECT decision, actor_kind, command_id
            FROM pulsara_v3.interaction_decisions
            WHERE session_id = %s
            """,
            (session_id,),
        ).fetchone() == ("REQUIRE_CONFIRMATION", "machine", None)
        assert connection.execute(
            """
            SELECT count(*) FROM pulsara_v3.tool_execution_attempts
            WHERE session_id = %s
            """,
            (session_id,),
        ).fetchone() == (0,)
        assert connection.execute(
            """
            SELECT attempt_id, result_state FROM pulsara_v3.tool_results
            WHERE session_id = %s
            """,
            (session_id,),
        ).fetchone() == (None, "PERMISSION_DENIED")


def test_stage2_large_assistant_content_uses_immutable_blob_reference(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    text = "x" * (70 << 10)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_text_stream(text)]),
        tools=StructuredToolPort(_AssertingTool(provider, session_id)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    asyncio.run(runner.run_turn(frozen_test_prompt("large answer")))
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        row = connection.execute(
            """
            SELECT b.blob_id, x.logical_size, octet_length(x.body)
            FROM pulsara_v3.assistant_message_blocks b
            JOIN pulsara_v3.blobs x ON x.id = b.blob_id
            WHERE b.session_id = %s AND b.block_kind = 'TEXT'
            """,
            (session_id,),
        ).fetchone()
    assert row is not None
    assert row[1:] == (len(text), len(text))


def test_stage2_cancellation_does_not_turn_a_live_tool_into_system_error(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    tools = _BlockingTool(provider, session_id)
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=_ScriptedModel([_tool_stream()]),
        tools=StructuredToolPort(tools),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )

    async def exercise() -> None:
        task = asyncio.create_task(
            runner.run_turn(frozen_test_prompt("block in a tool"))
        )
        await asyncio.wait_for(tools.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id = %s",
            (session_id,),
        ).fetchone() == ("INTERRUPTED",)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_execution_attempts WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE session_id = %s",
            (session_id,),
        ).fetchone() == (0,)


def test_round1_provider_rematerialization_uses_preview_and_scoped_artifact(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    model = _ScriptedModel([_tool_stream(), _text_stream("done", block="text:2")])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_LargeTool(provider, session_id)),
        live_bus=LiveAgentEventBus(),
        context_source_collector=StaticContextSourceCollector(),
    )
    asyncio.run(runner.run_turn(frozen_test_prompt("return a large result")))
    tool_messages = [
        item
        for item in model.requests[1].compiled_input.messages
        if item.role is MessageRole.TOOL_RESULT
    ]
    assert len(tool_messages) == 1
    preview = text_part_values(tool_messages[0].content)[0]
    assert len(preview.encode("utf-8")) <= 65_536
    assert "OUTPUT TRUNCATED / PREVIEW" in preview
    assert "If the omitted content is necessary" in preview
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 10,
    ) as connection:
        artifact_id = connection.execute(
            """
            SELECT output_artifact_id
            FROM pulsara_v3.tool_results
            WHERE session_id = %s
            """,
            (session_id,),
        ).fetchone()[0]
    read_port = PostgresToolArtifactReadPort(
        provider,
        session_id=session_id,
        workspace_id=workspace_id,
    )
    page = read_port.read_text(
        artifact_id,
        offset_chars=0,
        max_chars=32_000,
    )
    assert page.text == "z" * 32_000
    assert page.has_more
