"""Focused scheduling contracts for builtin view_image batches."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.assembler import CompletedToolCallBlock
from pulsara_agent.conversation_kernel.tool_contracts import (
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
    FrozenImageToolResourceAllowance,
    FrozenImageToolResourceIncrement,
    PreparedResolvedToolInvocation,
)
from pulsara_agent.conversation_kernel.image_validation import HostPromptImageValidator
from pulsara_agent.conversation_kernel.prompt_storage import (
    CanonicalImageReferenceResourceExceeded,
    CanonicalImageReferenceUnavailable,
)
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_surface import (
    BuiltinExecutionPolicyRef,
    PreparedToolExecutionBinding,
)
from pulsara_agent.conversation_kernel.tool_execution import (
    ToolBatchExecutionResult,
    ToolBatchExecutor,
)
from pulsara_agent.hooks.contracts import (
    FrozenHookMatcherFact,
    HookDispatchScopeRef,
    HookScopeKind,
)
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart, LLMTextPart
from pulsara_agent.tools.builtins.filesystem import (
    ViewImageSourceKind,
    parse_view_image_source,
)
from tests.support.round3 import direct_tool_invocation_context


def _call(ordinal: int) -> CompletedToolCallBlock:
    return CompletedToolCallBlock(
        f"block:{ordinal}",
        f"call:{ordinal}",
        "view_image",
        freeze_json({"path": f"/tmp/{ordinal}.png"}),
    )


def _reference_call(ordinal: int) -> CompletedToolCallBlock:
    return CompletedToolCallBlock(
        f"block:{ordinal}",
        f"call:{ordinal}",
        "view_image",
        freeze_json({"image_ref": "sha256:" + f"{ordinal:064x}"}),
    )


class _NonmatchingHookView:
    def selected_definitions(self, _event_type):
        definition = SimpleNamespace(
            matcher=FrozenHookMatcherFact("terminal", False)
        )
        return ((0, definition),)


class _AuthorizationPort:
    def __init__(self, kinds, events: list[str]) -> None:
        self._kinds = kinds
        self._events = events

    def prepare_resolved_invocation(self, *, tool_name, arguments, **_kwargs):
        source = parse_view_image_source(arguments)
        return PreparedResolvedToolInvocation(
            tool_name,
            tool_name,
            tool_name,
            tool_name,
            freeze_json(source.provider_value()),
        )

    async def authorize(self, *, tool_call_id, **_kwargs):
        self._events.append(f"authorize:{tool_call_id}")
        kind = self._kinds[tool_call_id]
        return KernelToolAuthorization(kind, f"policy:{tool_call_id}")


def _partition_executor(kinds, events: list[str]):
    executor = object.__new__(ToolBatchExecutor)
    executor._tools = _AuthorizationPort(kinds, events)

    async def execute_one(_self, *, calls, _preauthorization=None, **_kwargs):
        call = calls[0]
        assert _preauthorization is not None
        events.append(f"serial:{call.tool_call_id}")
        return ToolBatchExecutionResult(1)

    async def concurrent(_self, *, calls, **_kwargs):
        events.append("concurrent:" + ",".join(call.tool_call_id for call in calls))
        return ToolBatchExecutionResult(len(calls))

    executor.execute = MethodType(execute_one, executor)
    executor._execute_concurrent_view_segment = MethodType(concurrent, executor)
    return executor


def _partition_kwargs(calls, *, hook_scope=None, hook_view=None):
    return {
        "turn_id": "turn:1",
        "assistant_entry_id": "entry:assistant",
        "calls": calls,
        "canonical_facts": SimpleNamespace(run_permission_snapshot=object()),
        "canonical_identity": object(),
        "request": SimpleNamespace(memory_context=object()),
        "subagent_parent_context_subject": None,
        "continuity_scope": object(),
        "surface_borrow": object(),
        "last_assistant_message": None,
        "image_call_allowances": (),
        "call_ordinal_offset": 0,
        "hook_scope": hook_scope,
        "hook_view": hook_view,
    }


def test_present_nonmatching_hook_scope_keeps_contiguous_views_concurrent() -> None:
    async def scenario() -> None:
        events: list[str] = []
        calls = (_call(0), _call(1))
        executor = _partition_executor(
            {
                call.tool_call_id: KernelToolAuthorizationKind.ALLOW
                for call in calls
            },
            events,
        )
        scope = HookDispatchScopeRef(object(), object(), HookScopeKind.ROOT)

        result = await executor._execute_partitioned_batch(
            **_partition_kwargs(
                calls,
                hook_scope=scope,
                hook_view=_NonmatchingHookView(),
            )
        )

        assert result.tool_call_count == 2
        assert events == [
            "authorize:call:0",
            "authorize:call:1",
            "concurrent:call:0,call:1",
        ]

    asyncio.run(scenario())


def test_path_and_reference_calls_share_one_concurrent_view_segment() -> None:
    async def scenario() -> None:
        events: list[str] = []
        calls = (_call(0), _reference_call(1), _call(2))
        executor = _partition_executor(
            {
                call.tool_call_id: KernelToolAuthorizationKind.ALLOW
                for call in calls
            },
            events,
        )

        result = await executor._execute_partitioned_batch(
            **_partition_kwargs(calls)
        )

        assert result.tool_call_count == 3
        assert events == [
            "authorize:call:0",
            "authorize:call:1",
            "authorize:call:2",
            "concurrent:call:0,call:1,call:2",
        ]

    asyncio.run(scenario())


def test_image_allowances_exact_join_call_ordinal_identity_and_binding() -> None:
    call = _call(0)
    binding = PreparedToolExecutionBinding(
        tool_name="view_image",
        descriptor_fingerprint="descriptor",
        executor_binding_fingerprint="binding",
        execution_policy=BuiltinExecutionPolicyRef("view_image", "catalog"),
    )
    surface = SimpleNamespace(execution_binding=lambda _name: binding)
    allowance = FrozenImageToolResourceAllowance(
        call_ordinal=0,
        tool_call_id=call.tool_call_id,
        executor_binding_fingerprint="binding",
        canonical_bytes=1,
        logical_bytes=1,
        wire_bytes=1,
        input_tokens=1,
        quote_owner=object(),
    )

    ToolBatchExecutor._validate_image_call_allowances(
        calls=(call,),
        call_ordinal_offset=0,
        surface_borrow=surface,
        allowances=(allowance,),
    )
    mismatched = FrozenImageToolResourceAllowance(
        call_ordinal=0,
        tool_call_id=call.tool_call_id,
        executor_binding_fingerprint="other-binding",
        canonical_bytes=1,
        logical_bytes=1,
        wire_bytes=1,
        input_tokens=1,
        quote_owner=object(),
    )
    with pytest.raises(RuntimeError, match="do not exact-join"):
        ToolBatchExecutor._validate_image_call_allowances(
            calls=(call,),
            call_ordinal_offset=0,
            surface_borrow=surface,
            allowances=(mismatched,),
        )


def test_confirmation_call_is_a_barrier_before_later_authorization() -> None:
    async def scenario() -> None:
        events: list[str] = []
        calls = (_call(0), _call(1), _call(2))
        executor = _partition_executor(
            {
                "call:0": KernelToolAuthorizationKind.ALLOW,
                "call:1": KernelToolAuthorizationKind.REQUIRE_CONFIRMATION,
                "call:2": KernelToolAuthorizationKind.ALLOW,
            },
            events,
        )

        result = await executor._execute_partitioned_batch(
            **_partition_kwargs(calls)
        )

        assert result.tool_call_count == 3
        assert events == [
            "authorize:call:0",
            "authorize:call:1",
            "concurrent:call:0",
            "serial:call:1",
            "authorize:call:2",
            "concurrent:call:2",
        ]

    asyncio.run(scenario())


def test_reverse_physical_completion_still_releases_settlement_in_call_order() -> None:
    async def scenario() -> None:
        calls = (_call(0), _call(1))
        events: list[str] = []
        second_is_physical = asyncio.Event()
        executor = object.__new__(ToolBatchExecutor)

        async def execute_one(
            _self,
            *,
            calls,
            _settlement_gate,
            _settlement_release,
            _physical_complete,
            **_kwargs,
        ):
            call = calls[0]
            if call.tool_call_id == "call:0":
                await second_is_physical.wait()
            _physical_complete()
            events.append(f"physical:{call.tool_call_id}")
            if call.tool_call_id == "call:1":
                second_is_physical.set()
            await _settlement_gate.wait()
            events.append(f"settled:{call.tool_call_id}")
            _settlement_release.set()
            return ToolBatchExecutionResult(1)

        executor.execute = MethodType(execute_one, executor)
        allow = KernelToolAuthorization(KernelToolAuthorizationKind.ALLOW, "policy")
        prepared = tuple(
            (
                PreparedResolvedToolInvocation(
                    call.tool_name,
                    call.tool_name,
                    call.tool_name,
                    call.tool_name,
                    call.arguments,
                ),
                allow,
            )
            for call in calls
        )

        result = await executor._execute_concurrent_view_segment(
            turn_id="turn:1",
            assistant_entry_id="entry:assistant",
            calls=calls,
            preauthorizations=prepared,
            canonical_facts=object(),
            canonical_identity=object(),
            request=object(),
            subagent_parent_context_subject=None,
            continuity_scope=object(),
            surface_borrow=object(),
            last_assistant_message=None,
            image_call_allowances=(),
            call_ordinal_offset=0,
        )

        assert result.tool_call_count == 2
        assert events.index("physical:call:1") < events.index("physical:call:0")
        assert events.index("settled:call:0") < events.index("settled:call:1")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("modalities", "expected_error"),
    (
        (("text",), "MODEL_IMAGE_INPUT_UNSUPPORTED"),
        (None, "IMAGE_READ_FAILED"),
        (("text", "image"), "IMAGE_READ_FAILED"),
    ),
)
def test_view_image_uses_the_frozen_target_modality_tristate_before_read(
    tmp_path: Path,
    modalities: tuple[str, ...] | None,
    expected_error: str,
) -> None:
    async def scenario() -> None:
        validator = HostPromptImageValidator()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:image-modalities",
            session_id="session:image-modalities",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            image_validator=validator,
        )
        borrow, context = direct_tool_invocation_context(
            port,
            session_id="session:image-modalities",
            tool_name="view_image",
            tool_call_id="call:image",
            attempt_id="attempt:image",
            turn_id="turn:image",
            assistant_entry_id="entry:assistant",
        )
        try:
            binding = borrow.execution_binding("view_image")
            allowance = FrozenImageToolResourceAllowance(
                call_ordinal=0,
                tool_call_id="call:image",
                executor_binding_fingerprint=(
                    binding.executor_binding_fingerprint
                ),
                canonical_bytes=1 << 20,
                logical_bytes=1 << 20,
                wire_bytes=1 << 20,
                input_tokens=1 << 20,
                quote_owner=SimpleNamespace(),
            )
            result = await port.invoke(
                tool_name="view_image",
                arguments={"path": "missing.png"},
                tool_call_id="call:image",
                attempt_id="attempt:image",
                turn_id="turn:image",
                assistant_entry_id="entry:assistant",
                invocation_context=replace(
                    context,
                    input_modalities=modalities,
                    image_resource_allowance=allowance,
                ),
            )
            assert isinstance(result.content, bytes)
            assert expected_error in result.content.decode("utf-8")
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
            await validator.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("read_error", "expected_error"),
    (
        (None, None),
        (CanonicalImageReferenceUnavailable, "IMAGE_REFERENCE_UNAVAILABLE"),
        (CanonicalImageReferenceResourceExceeded, "IMAGE_RESOURCE_EXCEEDED"),
    ),
)
def test_view_image_reference_uses_the_canonical_port_without_local_validation(
    tmp_path: Path,
    read_error,
    expected_error,
) -> None:
    image = LLMImagePart("image/png", b"canonical-image", 4, 3)

    class ReferencePort:
        def read_image(self, **kwargs):
            assert kwargs["session_id"] == "session:image-reference"
            assert kwargs["workspace_id"] == "workspace:test"
            assert kwargs["image_ref"] == image.content_digest
            assert kwargs["maximum_encoded_bytes"] > len(image.immutable_bytes)
            if read_error is not None:
                raise read_error("reference read rejected")
            return image

    class QuoteOwner:
        def quote(self, *, source, content, **_kwargs):
            assert source.kind is ViewImageSourceKind.IMAGE_REF
            assert source.value == image.content_digest
            assert content == FrozenPromptContent((LLMTextPart("Image loaded."), image))
            return FrozenImageToolResourceIncrement(1, 1, 1, 1)

    async def scenario() -> None:
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:image-reference",
            session_id="session:image-reference",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            image_reference_read_port=ReferencePort(),
        )
        borrow, context = direct_tool_invocation_context(
            port,
            session_id="session:image-reference",
            tool_name="view_image",
            tool_call_id="call:image-reference",
            attempt_id="attempt:image-reference",
            turn_id="turn:image-reference",
            assistant_entry_id="entry:assistant",
        )
        binding = borrow.execution_binding("view_image")
        allowance = FrozenImageToolResourceAllowance(
            call_ordinal=0,
            tool_call_id="call:image-reference",
            executor_binding_fingerprint=binding.executor_binding_fingerprint,
            canonical_bytes=1 << 20,
            logical_bytes=1 << 20,
            wire_bytes=1 << 20,
            input_tokens=1 << 20,
            quote_owner=QuoteOwner(),
        )
        try:
            result = await port.invoke(
                tool_name="view_image",
                arguments={"image_ref": image.content_digest},
                tool_call_id="call:image-reference",
                attempt_id="attempt:image-reference",
                turn_id="turn:image-reference",
                assistant_entry_id="entry:assistant",
                invocation_context=replace(
                    context,
                    input_modalities=("text", "image"),
                    image_resource_allowance=allowance,
                ),
            )
            if expected_error is None:
                assert result.state == "SUCCESS"
                assert result.content == FrozenPromptContent(
                    (LLMTextPart("Image loaded."), image)
                )
            else:
                assert result.state == "APPLICATION_ERROR"
                assert result.content == (
                    '{"error":"' + expected_error + '"}'
                ).encode("utf-8")
                assert result.physical_observation is not None
                assert result.physical_timing == "ON_TIME"
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)

    asyncio.run(scenario())


def test_view_image_reference_rejects_text_only_target_before_database_read(
    tmp_path: Path,
) -> None:
    reads = 0

    class ReferencePort:
        def read_image(self, **_kwargs):
            nonlocal reads
            reads += 1
            raise AssertionError("text-only target must not read the image")

    async def scenario() -> None:
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:image-reference-text-only",
            session_id="session:image-reference-text-only",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            image_reference_read_port=ReferencePort(),
        )
        borrow, context = direct_tool_invocation_context(
            port,
            session_id="session:image-reference-text-only",
            tool_name="view_image",
            tool_call_id="call:image-reference-text-only",
            attempt_id="attempt:image-reference-text-only",
            turn_id="turn:image-reference-text-only",
            assistant_entry_id="entry:assistant",
        )
        binding = borrow.execution_binding("view_image")
        allowance = FrozenImageToolResourceAllowance(
            call_ordinal=0,
            tool_call_id="call:image-reference-text-only",
            executor_binding_fingerprint=binding.executor_binding_fingerprint,
            canonical_bytes=1 << 20,
            logical_bytes=1 << 20,
            wire_bytes=1 << 20,
            input_tokens=1 << 20,
            quote_owner=SimpleNamespace(),
        )
        try:
            result = await port.invoke(
                tool_name="view_image",
                arguments={"image_ref": "sha256:" + "1" * 64},
                tool_call_id="call:image-reference-text-only",
                attempt_id="attempt:image-reference-text-only",
                turn_id="turn:image-reference-text-only",
                assistant_entry_id="entry:assistant",
                invocation_context=replace(
                    context,
                    input_modalities=("text",),
                    image_resource_allowance=allowance,
                ),
            )
            assert result.state == "APPLICATION_ERROR"
            assert result.content == b'{"error":"MODEL_IMAGE_INPUT_UNSUPPORTED"}'
            assert reads == 0
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"path": "x.png", "image_ref": "sha256:" + "0" * 64},
        {"path": None},
        {"image_ref": "sha256:not-a-digest"},
        {"path": "x.png", "unexpected": True},
    ),
)
def test_view_image_source_parser_rejects_every_non_union_shape(arguments) -> None:
    with pytest.raises(ValueError):
        parse_view_image_source(arguments)


def test_concurrent_view_window_queues_tail_until_physical_completion() -> None:
    async def scenario() -> None:
        limit = STAGE2_LIMITS.foreground_io_hard_concurrency
        calls = tuple(_call(index) for index in range(limit + 3))
        release = asyncio.Event()
        started: list[str] = []
        active = 0
        maximum_active = 0
        executor = object.__new__(ToolBatchExecutor)

        async def execute_one(
            _self,
            *,
            calls,
            _settlement_gate,
            _settlement_release,
            _physical_complete,
            **_kwargs,
        ):
            nonlocal active, maximum_active
            call = calls[0]
            started.append(call.tool_call_id)
            active += 1
            maximum_active = max(maximum_active, active)
            physically_active = True
            try:
                await release.wait()
                active -= 1
                physically_active = False
                _physical_complete()
                await _settlement_gate.wait()
                _settlement_release.set()
                return ToolBatchExecutionResult(1)
            finally:
                if physically_active:
                    active -= 1

        executor.execute = MethodType(execute_one, executor)
        allow = KernelToolAuthorization(KernelToolAuthorizationKind.ALLOW, "policy")
        prepared = tuple(
            (
                PreparedResolvedToolInvocation(
                    call.tool_name,
                    call.tool_name,
                    call.tool_name,
                    call.tool_name,
                    call.arguments,
                ),
                allow,
            )
            for call in calls
        )
        running = asyncio.create_task(
            executor._execute_concurrent_view_segment(
                turn_id="turn:1",
                assistant_entry_id="entry:assistant",
                calls=calls,
                preauthorizations=prepared,
                canonical_facts=object(),
                canonical_identity=object(),
                request=object(),
                subagent_parent_context_subject=None,
                continuity_scope=object(),
                surface_borrow=object(),
                last_assistant_message=None,
                image_call_allowances=(),
                call_ordinal_offset=0,
            )
        )
        while len(started) < limit:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert len(started) == limit
        release.set()
        result = await running
        assert result.tool_call_count == len(calls)
        assert maximum_active <= limit
        assert active == 0

    asyncio.run(scenario())


def test_cancelling_concurrent_view_segment_drains_started_calls() -> None:
    async def scenario() -> None:
        limit = STAGE2_LIMITS.foreground_io_hard_concurrency
        calls = tuple(_call(index) for index in range(limit + 2))
        entered = asyncio.Event()
        physical = asyncio.Event()
        active = 0
        executor = object.__new__(ToolBatchExecutor)

        async def execute_one(_self, *, _physical_complete, **_kwargs):
            nonlocal active
            active += 1
            if active == limit:
                entered.set()
            try:
                await physical.wait()
                _physical_complete()
                return ToolBatchExecutionResult(1)
            finally:
                active -= 1

        executor.execute = MethodType(execute_one, executor)
        allow = KernelToolAuthorization(KernelToolAuthorizationKind.ALLOW, "policy")
        prepared = tuple(
            (
                PreparedResolvedToolInvocation(
                    call.tool_name,
                    call.tool_name,
                    call.tool_name,
                    call.tool_name,
                    call.arguments,
                ),
                allow,
            )
            for call in calls
        )
        running = asyncio.create_task(
            executor._execute_concurrent_view_segment(
                turn_id="turn:1",
                assistant_entry_id="entry:assistant",
                calls=calls,
                preauthorizations=prepared,
                canonical_facts=object(),
                canonical_identity=object(),
                request=object(),
                subagent_parent_context_subject=None,
                continuity_scope=object(),
                surface_borrow=object(),
                last_assistant_message=None,
                image_call_allowances=(),
                call_ordinal_offset=0,
            )
        )
        await entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert active == 0

    asyncio.run(scenario())
