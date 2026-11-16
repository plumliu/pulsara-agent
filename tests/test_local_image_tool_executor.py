"""Focused scheduling contracts for builtin view_image batches."""

from __future__ import annotations
from tests.support.model_config import scheduled_task_service
from tests.support.images import bound_test_image
from pulsara_agent.identity import derive_id

import asyncio
from dataclasses import replace
from types import MethodType, SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.assembler import CompletedToolCallBlock
from pulsara_agent.conversation_kernel.tool_contracts import (
    KernelToolAuthorization,
    KernelToolAuthorizationKind,
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
from pulsara_agent.tools.builtins.filesystem import (
    parse_view_image_source,
)
from tests.support.round3 import direct_tool_invocation_context


def _call(ordinal: int) -> CompletedToolCallBlock:
    return CompletedToolCallBlock(
        derive_id("block", "test-fixture", f"block:{ordinal}"),
        f"call:{ordinal}",
        "view_image",
        freeze_json({"target": {"kind": "PATH", "path": f"/tmp/{ordinal}.png"}}),
    )


def _reference_call(ordinal: int) -> CompletedToolCallBlock:
    return CompletedToolCallBlock(
        derive_id("block", "test-fixture", f"block:{ordinal}"),
        f"call:{ordinal}",
        "view_image",
        freeze_json(
            {
                "target": {
                    "kind": "IMAGE_REF",
                    "image_ref": derive_id("image", "test-fixture", str(ordinal)),
                }
            }
        ),
    )


class _NonmatchingHookView:
    def selected_definitions(self, _event_type):
        definition = SimpleNamespace(
            matcher=FrozenHookMatcherFact("exec_process", False)
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
        "turn_id": "turn:nzc36cttgo7xxki7bcjc7ops7a",
        "assistant_entry_id": "entry:5qidalqvkujk5ykzzzfpwie2qy",
        "calls": calls,
        "canonical_facts": SimpleNamespace(run_permission_snapshot=object()),
        "canonical_identity": object(),
        "request": SimpleNamespace(memory_context=object()),
        "agent_invocation_snapshot": None,
        "continuity_scope": object(),
        "surface_borrow": object(),
        "call_ordinal_offset": 0,
        "hook_scope": hook_scope,
        "hook_view": hook_view,
    }


def test_present_nonmatching_hook_scope_keeps_contiguous_views_concurrent() -> None:
    async def scenario() -> None:
        events: list[str] = []
        calls = (_call(0), _call(1))
        executor = _partition_executor(
            {call.tool_call_id: KernelToolAuthorizationKind.ALLOW for call in calls},
            events,
        )
        scope = HookDispatchScopeRef(
            object(), object(), HookScopeKind.ROOT, "session:cbauiajfisf4gvpdotrq4qgxhq"
        )

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
            {call.tool_call_id: KernelToolAuthorizationKind.ALLOW for call in calls},
            events,
        )

        result = await executor._execute_partitioned_batch(**_partition_kwargs(calls))

        assert result.tool_call_count == 3
        assert events == [
            "authorize:call:0",
            "authorize:call:1",
            "authorize:call:2",
            "concurrent:call:0,call:1,call:2",
        ]

    asyncio.run(scenario())


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

        result = await executor._execute_partitioned_batch(**_partition_kwargs(calls))

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
                    call.arguments,
                ),
                allow,
            )
            for call in calls
        )

        result = await executor._execute_concurrent_view_segment(
            turn_id="turn:nzc36cttgo7xxki7bcjc7ops7a",
            assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
            calls=calls,
            preauthorizations=prepared,
            canonical_facts=object(),
            canonical_identity=object(),
            request=object(),
            agent_invocation_snapshot=None,
            continuity_scope=object(),
            surface_borrow=object(),
            call_ordinal_offset=0,
        )

        assert result.tool_call_count == 2
        assert events.index("physical:call:1") < events.index("physical:call:0")
        assert events.index("settled:call:0") < events.index("settled:call:1")

    asyncio.run(scenario())


@pytest.mark.parametrize("modalities", [("text",), None, ("text", "image")])
def test_view_image_physical_failure_is_independent_of_model_modality(
    tmp_path, modalities
):
    async def scenario():
        validator = HostPromptImageValidator()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=derive_id("host", "image-test"),
            session_id=derive_id("session", "image-test"),
            live_bus=LiveAgentEventBus(),
            scheduled_tasks=scheduled_task_service(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            image_validator=validator,
        )
        borrow, context = direct_tool_invocation_context(
            port,
            session_id=port._session_id,
            tool_name="view_image",
            tool_call_id="image-call",
            attempt_id=derive_id("attempt", "image-test"),
            turn_id=derive_id("turn", "image-test"),
            assistant_entry_id=derive_id("entry", "image-test"),
        )
        try:
            result = await port.invoke(
                tool_name="view_image",
                arguments={"target": {"kind": "PATH", "path": "missing.png"}},
                tool_call_id=context.tool_call_id,
                attempt_id=context.attempt_id,
                turn_id=context.turn_id,
                assistant_entry_id=context.assistant_entry_id,
                invocation_context=replace(context, input_modalities=modalities),
            )
            assert result.state == "APPLICATION_ERROR"
            assert result.content.public_fields["target"] == {
                "kind": "PATH",
                "path": "missing.png",
            }
            assert "无法读取图片" in result.content.content[0].value
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
            await validator.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("modalities", [("text",), ("text", "image")])
@pytest.mark.parametrize(
    "read_error",
    [None, CanonicalImageReferenceUnavailable, CanonicalImageReferenceResourceExceeded],
)
def test_view_image_reference_reads_global_asset_without_local_validation(
    tmp_path, modalities, read_error
):
    image = bound_test_image("image/png", b"canonical-image", 4, 3)
    reads = []

    class ReferencePort:
        def read_image(self, **kwargs):
            assert set(kwargs) == {
                "image_ref",
                "maximum_encoded_bytes",
                "deadline_monotonic",
            }
            assert kwargs["image_ref"] == image.image_ref
            assert kwargs["maximum_encoded_bytes"] > len(image.immutable_bytes)
            reads.append(kwargs)
            if read_error is not None:
                raise read_error("reference read rejected")
            return image

    async def scenario():
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=derive_id("host", "reference-test"),
            session_id=derive_id("session", "reference-test"),
            live_bus=LiveAgentEventBus(),
            scheduled_tasks=scheduled_task_service(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            image_reference_read_port=ReferencePort(),
        )
        borrow, context = direct_tool_invocation_context(
            port,
            session_id=port._session_id,
            tool_name="view_image",
            tool_call_id="image-call",
            attempt_id=derive_id("attempt", "reference-test"),
            turn_id=derive_id("turn", "reference-test"),
            assistant_entry_id=derive_id("entry", "reference-test"),
        )
        try:
            result = await port.invoke(
                tool_name="view_image",
                arguments={
                    "target": {"kind": "IMAGE_REF", "image_ref": image.image_ref}
                },
                tool_call_id=context.tool_call_id,
                attempt_id=context.attempt_id,
                turn_id=context.turn_id,
                assistant_entry_id=context.assistant_entry_id,
                invocation_context=replace(context, input_modalities=modalities),
            )
            assert len(reads) == 1
            assert result.content.public_fields["target"] == {
                "kind": "IMAGE_REF",
                "image_ref": image.image_ref,
            }
            if read_error is None:
                assert result.state == "SUCCESS"
                assert [part.type for part in result.content.content] == [
                    "text",
                    "image",
                ]
                assert result.pending_images == (image,)
            else:
                assert result.state == "APPLICATION_ERROR"
                assert all(part.type == "text" for part in result.content.content)
                assert result.physical_observation is not None
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "arguments",
    (
        {},
        {"target": {"kind": "PATH", "path": "x.png", "image_ref": "image:" + "a" * 26}},
        {"target": {"kind": "PATH", "path": None}},
        {"target": {"kind": "IMAGE_REF", "image_ref": "sha256:not-a-digest"}},
        {"target": {"kind": "PATH", "path": "x.png"}, "unexpected": True},
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
                    call.arguments,
                ),
                allow,
            )
            for call in calls
        )
        running = asyncio.create_task(
            executor._execute_concurrent_view_segment(
                turn_id="turn:nzc36cttgo7xxki7bcjc7ops7a",
                assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
                calls=calls,
                preauthorizations=prepared,
                canonical_facts=object(),
                canonical_identity=object(),
                request=object(),
                agent_invocation_snapshot=None,
                continuity_scope=object(),
                surface_borrow=object(),
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
                    call.arguments,
                ),
                allow,
            )
            for call in calls
        )
        running = asyncio.create_task(
            executor._execute_concurrent_view_segment(
                turn_id="turn:nzc36cttgo7xxki7bcjc7ops7a",
                assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
                calls=calls,
                preauthorizations=prepared,
                canonical_facts=object(),
                canonical_identity=object(),
                request=object(),
                agent_invocation_snapshot=None,
                continuity_scope=object(),
                surface_borrow=object(),
                call_ordinal_offset=0,
            )
        )
        await entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "dependencies",
        "dependency_missing",
        "preparation_deadline",
        "deadline",
        "missing",
        "text_only",
    ],
)
def test_html_view_image_uses_production_static_owner(tmp_path, monkeypatch, mode):
    from io import BytesIO
    from PIL import Image
    from pulsara_agent.conversation_kernel.visualization_screenshot import (
        VisualizationScreenshotError,
    )

    async def scenario():
        path = tmp_path / "chart.html"
        path.write_text("<h1>" + "x" * 10000 + "</h1>")
        output = BytesIO()
        Image.new("RGB", (2, 2), "red").save(output, format="PNG")
        if mode in {"dependencies", "dependency_missing", "preparation_deadline"}:
            path.write_text(
                '<link rel="stylesheet" href="app.css"><img src="image.png"><script src="app.js"></script>'
            )
            (tmp_path / "app.css").write_text("body{background:url(image.png)}")
            (tmp_path / "app.js").write_text("window.loaded=true")
            if mode != "dependency_missing":
                (tmp_path / "image.png").write_bytes(output.getvalue())
        validator = HostPromptImageValidator()
        if mode == "preparation_deadline":
            from unittest.mock import AsyncMock

            monkeypatch.setattr(
                validator,
                "freeze_local_image",
                AsyncMock(side_effect=TimeoutError("preparation deadline")),
            )
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=derive_id("host", "html-test"),
            session_id=derive_id("session", "html-test"),
            live_bus=LiveAgentEventBus(),
            scheduled_tasks=scheduled_task_service(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            image_validator=validator,
        )
        borrow, context = direct_tool_invocation_context(
            port,
            session_id=port._session_id,
            tool_name="view_image",
            tool_call_id="html-call",
            attempt_id=derive_id("attempt", "html-test"),
            turn_id=derive_id("turn", "html-test"),
            assistant_entry_id=derive_id("entry", "html-test"),
        )
        rendered = []

        async def screenshot(html, *, deadline_monotonic):
            rendered.append(html)
            if mode == "deadline":
                raise VisualizationScreenshotError("PREVIEW_DEADLINE_EXPIRED")
            return output.getvalue()

        port._visualization_screenshots.render = screenshot
        try:
            result = await port.invoke(
                tool_name="view_image",
                arguments={
                    "target": {
                        "kind": "PATH",
                        "path": "missing.html" if mode == "missing" else "chart.html",
                    }
                },
                tool_call_id=context.tool_call_id,
                attempt_id=context.attempt_id,
                turn_id=context.turn_id,
                assistant_entry_id=context.assistant_entry_id,
                invocation_context=replace(
                    context,
                    input_modalities=("text",)
                    if mode == "text_only"
                    else ("text", "image"),
                ),
            )
            if mode in {"success", "dependencies", "text_only"}:
                assert result.state == "SUCCESS"
                if mode == "dependencies":
                    assert b"data:image/png;base64," in rendered[0]
                    assert b"data:text/css;charset=utf-8;base64," in rendered[0]
                    assert b"data:text/javascript;charset=utf-8;base64," in rendered[0]
                assert [part.type for part in result.content.content] == [
                    "text",
                    "image",
                ]
                assert result.pending_images[0].immutable_bytes == output.getvalue()
                assert "HTML静态截图" in result.content.content[0].value
            else:
                assert result.state == "APPLICATION_ERROR"
                assert all(part.type == "text" for part in result.content.content)
                if mode == "deadline":
                    assert "PREVIEW_DEADLINE_EXPIRED" in result.content.content[0].value
            if mode in {"missing", "dependency_missing", "preparation_deadline"}:
                assert not rendered
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
            await validator.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "dimensions,size",
    [
        ('width="1200" height="380" viewBox="0 0 1200 380"', (1200, 380)),
        ('viewBox="0 0 120 600"', (120, 600)),
        ('width="100%" height="100%" viewBox="0 0 600 120"', (600, 120)),
        ('width="300" viewBox="0 0 600 120"', (300, 60)),
    ],
)
def test_svg_view_image_real_browser_preserves_complete_frame(
    tmp_path, dimensions, size
):
    from io import BytesIO
    from PIL import Image

    async def scenario():
        (tmp_path / "完整图.svg").write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" {dimensions}>'
            '<rect width="100%" height="100%" fill="#123456"/>'
            "<script>document.documentElement.remove()</script>"
            '<image href="http://127.0.0.1:1/private.png"/>'
            "</svg>"
        )
        validator = HostPromptImageValidator()
        port = DirectKernelToolPort(
            scheduled_tasks=scheduled_task_service(),
            workspace_root=tmp_path,
            host_owner_id="host:hi4pnm7cfz5jwlcsfvvcx4rpjq",
            session_id="session:eghkqizzionnli27oxvk6kkcgu",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            image_validator=validator,
        )
        borrow, context = direct_tool_invocation_context(
            port,
            session_id="session:eghkqizzionnli27oxvk6kkcgu",
            tool_name="view_image",
            tool_call_id="call:svg",
            attempt_id="attempt:fxwn2ydacal2miwr3c4gega7wm",
            turn_id="turn:4s6voic575fjaadstkw7z4cttq",
            assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
        )
        try:
            result = await port.invoke(
                tool_name="view_image",
                arguments={"target": {"kind": "PATH", "path": "完整图.svg"}},
                tool_call_id="call:svg",
                attempt_id="attempt:fxwn2ydacal2miwr3c4gega7wm",
                turn_id="turn:4s6voic575fjaadstkw7z4cttq",
                assistant_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
                invocation_context=replace(context, input_modalities=("text", "image")),
            )
            assert result.state == "SUCCESS", result.content
            part = result.pending_images[0]
            assert (part.width, part.height) == size
            pixels = Image.open(BytesIO(part.immutable_bytes)).convert("RGB")
            for point in (
                (0, 0),
                (size[0] - 1, size[1] - 1),
                (size[0] // 2, size[1] // 2),
            ):
                assert pixels.getpixel(point) == (18, 52, 86)
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)
            await validator.aclose()

    asyncio.run(scenario())
