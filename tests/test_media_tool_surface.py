from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.conversation_kernel.image_validation import HostPromptImageValidator
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.tool_contracts import (
    FrozenImageToolResourceAllowance,
    FrozenImageToolResourceIncrement,
    KernelToolAuthorizationKind,
)
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.tool_surface import CodeModeToolDirectory
from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    frozen_tool_result_public_text,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.tool_result_projection import (
    project_tool_result_storage_body,
)
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.tools.builtins.media import (
    MEDIA_TOOL_NAMES,
    validate_media_arguments,
)
from tests.support.model_config import scheduled_task_service
from tests.support.round3 import (
    direct_tool_invocation_context,
    prepare_test_direct_tool_surface,
)
from tests.test_media_frames import video
from tests.test_media_source import wav

SESSION = derive_id("session", "test", "media-surface")
TURN = derive_id("turn", "test", "media-surface")
ENTRY = derive_id("entry", "test", "media-surface")
ATTEMPT = derive_id("attempt", "test", "media-surface")


def port_at(path, validator=None, **owners):
    return DirectKernelToolPort(
        workspace_root=path,
        host_owner_id=derive_id("host", "test", "media-surface"),
        session_id=SESSION,
        live_bus=LiveAgentEventBus(),
        scheduled_tasks=scheduled_task_service(),
        authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        image_validator=validator,
        **owners,
    )


async def invoke(port, name, arguments, *, image=False):
    borrow, context = direct_tool_invocation_context(
        port,
        session_id=SESSION,
        tool_name=name,
        tool_call_id="external-provider-call",
        attempt_id=ATTEMPT,
        turn_id=TURN,
        assistant_entry_id=ENTRY,
    )
    try:
        if image:

            class Quote:
                def quote(self, **kwargs):
                    assert isinstance(kwargs["content"], FrozenPromptContent)
                    self.content = kwargs["content"]
                    return FrozenImageToolResourceIncrement(0, 0, 0, 0)

            allowance = FrozenImageToolResourceAllowance(
                0,
                context.tool_call_id,
                borrow.execution_binding(name).executor_binding_fingerprint,
                4 << 20,
                4 << 20,
                8 << 20,
                100_000,
                Quote(),
            )
            context = replace(
                context,
                input_modalities=("text", "image"),
                image_resource_allowance=allowance,
            )
        return await port.invoke(
            tool_name=name,
            arguments=arguments,
            tool_call_id=context.tool_call_id,
            attempt_id=ATTEMPT,
            turn_id=TURN,
            assistant_entry_id=ENTRY,
            invocation_context=context,
        )
    finally:
        borrow.close()


def test_closed_media_fields_and_root_only_catalog():
    for name, count in [
        ("read_video_frames", 6),
        ("read_transcript", 4),
        ("annotate_media", 4),
    ]:
        entry = builtin_tool_catalog_entry(name)
        assert len(entry.descriptor.input_schema["properties"]) == count
        assert len(entry.availability_requirement.allowed_invocation_owners) == 1
    for value in [True, "1", float("nan"), float("inf"), -1]:
        with pytest.raises(ValueError):
            validate_media_arguments(
                "read_transcript",
                {"target": {"kind": "PATH", "path": "x"}, "from": value},
            )
    with pytest.raises(ValueError):
        validate_media_arguments(
            "annotate_media",
            {"target": {"kind": "PATH", "path": "x"}, "from": 0, "to": 1, "text": "  "},
        )
    detail = {"target": {"kind": "PATH", "path": "x"}, "mode": "detail"}
    for bounds in ({}, {"from": 0}, {"to": 1}, {"from": 12.32, "to": 14.320001}):
        with pytest.raises(ValueError, match="2 seconds"):
            validate_media_arguments("read_video_frames", dict(detail, **bounds))
    validate_media_arguments("read_video_frames", dict(detail, **{"from": 12.32, "to": 14.32}))
    for name in ("read_video_frames", "read_transcript"):
        for value in [None, {}]:
            with pytest.raises(ValueError):
                validate_media_arguments(
                    name,
                    {"target": {"kind": "PATH", "path": "x"}, "continuation": value},
                )


def test_media_hidden_subagent_code_mode_and_forged_entry_denied(tmp_path):
    async def run():
        port = port_at(tmp_path)
        root = prepare_test_direct_tool_surface(
            port, conversation_scope_kind=ModelInputScopeKind.ROOT
        )
        sub = prepare_test_direct_tool_surface(
            port, conversation_scope_kind=ModelInputScopeKind.SUBAGENT
        )
        assert MEDIA_TOOL_NAMES <= {s.name for s in root.model_surface.tool_specs}
        assert not MEDIA_TOOL_NAMES & {s.name for s in sub.model_surface.tool_specs}
        assert not MEDIA_TOOL_NAMES & {
            s.name for s in CodeModeToolDirectory.freeze(root).tool_specs
        }
        borrow = port.borrow_tool_surface(sub)
        try:
            for name in MEDIA_TOOL_NAMES:
                auth = await port.authorize(
                    tool_name=name,
                    arguments={},
                    tool_call_id="forged",
                    turn_id=TURN,
                    assistant_entry_id=ENTRY,
                    permission_snapshot=SimpleNamespace(),
                    surface_borrow=borrow,
                    memory_context=SimpleNamespace(),
                )
                assert auth.kind is KernelToolAuthorizationKind.PERMISSION_DENIED
        finally:
            borrow.close()
            await port.aclose()

    asyncio.run(run())


def test_real_frames_and_annotation_use_production_dispatch(tmp_path):
    path = video(tmp_path / "states.mkv", ["red"] * 4 + ["blue"] * 4 + ["red"] * 12, rate=10)

    async def run():
        validator = HostPromptImageValidator()
        port = port_at(tmp_path, validator)
        try:
            for bounds in ({}, {"from": 0}, {"to": 1}, {"from": 0, "to": 5}):
                rejected = await invoke(port, "read_video_frames", {
                    "target": {"kind": "PATH", "path": str(tmp_path / "unopened.mkv")},
                    "mode": "detail", **bounds,
                }, image=True)
                assert rejected.state == "APPLICATION_ERROR"
                error = json.loads(rejected.content)
                assert error["error"] == "INVALID_RANGE"
                assert "2 seconds" in error["message"]
                assert "overview" in error["message"] and "motion" in error["message"]
            frames = await invoke(
                port,
                "read_video_frames",
                {"target": {"kind": "PATH", "path": str(path)}, "mode": "detail", "from": 0, "to": 1},
                image=True,
            )
            assert frames.state == "SUCCESS", frames.content
            assert isinstance(frames.content, FrozenPromptContent)
            assert (
                len([p for p in frames.content.parts if isinstance(p, LLMImagePart)])
                == 2
            )
            body = json.loads(
                frozen_tool_result_public_text(
                    frames.content, tool_name="read_video_frames"
                )
            )
            assert body["video_frames"] is True
            assert body["media_version"] == {
                "modified_ns": str(path.stat().st_mtime_ns),
                "size": path.stat().st_size,
            }
            assert body["frames"][0]["t"] == 0
            assert body["frames"][-1]["t"] > 0
            assert [frame["t"] for frame in body["frames"]] == [0, 0.5, 0.9]
            assert [frame["image"] for frame in body["frames"]] == [1, 2, 1]
            annotation = await invoke(
                port,
                "annotate_media",
                {
                    "target": {"kind": "PATH", "path": str(path)},
                    "from": 0,
                    "to": 0.1,
                    "text": "Changed scene",
                },
            )
            assert annotation.state == "SUCCESS"
            raw = json.loads(annotation.content)
            assert raw == {
                "media_annotation": "added",
                "canonical_path": str(path.resolve()),
                "file_mtime_ns": str(path.stat().st_mtime_ns),
            }
            public = project_tool_result_storage_body(
                annotation.content.decode(),
                ToolObservationOrigin.BUILTIN,
                tool_name="annotate_media",
                result_state="SUCCESS",
            )
            assert json.loads(public) == {"media_annotation": "added"}
            assert (
                project_tool_result_storage_body(
                    annotation.content.decode(), ToolObservationOrigin.BUILTIN
                )
                == annotation.content.decode()
            )
        finally:
            await port.aclose()
            await validator.aclose()

    asyncio.run(run())


def test_audio_annotation_and_no_video_modality_boundary(tmp_path):
    path = tmp_path / "sound.wav"
    wav(path)

    async def run():
        port = port_at(tmp_path)
        try:
            result = await invoke(
                port,
                "annotate_media",
                {
                    "target": {"kind": "PATH", "path": str(path)},
                    "from": 0,
                    "to": 0.05,
                    "text": "Audio note",
                },
            )
            assert result.state == "SUCCESS", result.content
            result = await invoke(
                port,
                "read_video_frames",
                {"target": {"kind": "PATH", "path": str(path)}},
                image=True,
            )
            assert json.loads(result.content)["error"] == "NO_VIDEO_TRACK"
        finally:
            await port.aclose()

    asyncio.run(run())


def test_forged_root_indirect_provenance_is_denied_before_source_read(tmp_path):
    from pulsara_agent.conversation_kernel.tool_invocations import (
        ToolInvocationSource,
        ToolInvocationSourceKind,
        current_tool_invocation,
    )
    from pulsara_agent.primitives.context import freeze_json

    async def run():
        port = port_at(tmp_path)
        borrow, context = direct_tool_invocation_context(
            port,
            session_id=SESSION,
            tool_name="annotate_media",
            tool_call_id="forged-provider-id",
            attempt_id=ATTEMPT,
            turn_id=TURN,
            assistant_entry_id=ENTRY,
        )
        source = ToolInvocationSource(
            derive_id("call", "test", "indirect"),
            ToolInvocationSourceKind.CODE_MODE_CALL,
            TURN,
            source_exec_attempt_id=ATTEMPT,
            call_sequence=1,
            tool_name="annotate_media",
            original_arguments=freeze_json({}),
        )
        token = current_tool_invocation.set(source)
        try:
            auth = await port.authorize(
                tool_name="annotate_media",
                arguments={},
                tool_call_id="forged-provider-id",
                turn_id=TURN,
                assistant_entry_id=ENTRY,
                permission_snapshot=SimpleNamespace(),
                surface_borrow=borrow,
                memory_context=SimpleNamespace(),
            )
            assert auth.kind is KernelToolAuthorizationKind.PERMISSION_DENIED
            prepared = port.prepare_resolved_invocation(
                tool_name="annotate_media", arguments={}, surface_borrow=borrow
            )
            assert (
                prepared.authorization.kind
                is KernelToolAuthorizationKind.TOOL_UNAVAILABLE
            )
            with pytest.raises(RuntimeError, match="direct ROOT"):
                await port.invoke(
                    tool_name="annotate_media",
                    arguments={
                        "target": {"kind": "PATH", "path": "missing"},
                        "from": 0,
                        "to": 1,
                        "text": "x",
                    },
                    tool_call_id="forged-provider-id",
                    attempt_id=ATTEMPT,
                    turn_id=TURN,
                    assistant_entry_id=ENTRY,
                    invocation_context=replace(context, invocation_source=source),
                )
        finally:
            current_tool_invocation.reset(token)
            borrow.close()
            await port.aclose()

    asyncio.run(run())


def test_text_only_video_denies_before_source_probe(tmp_path):
    async def run():
        port = port_at(tmp_path)
        borrow, context = direct_tool_invocation_context(
            port,
            session_id=SESSION,
            tool_name="read_video_frames",
            tool_call_id="external-provider-call",
            attempt_id=ATTEMPT,
            turn_id=TURN,
            assistant_entry_id=ENTRY,
        )
        try:
            result = await port.invoke(
                tool_name="read_video_frames",
                arguments={"target": {"kind": "PATH", "path": "missing.mkv"}},
                tool_call_id="external-provider-call",
                attempt_id=ATTEMPT,
                turn_id=TURN,
                assistant_entry_id=ENTRY,
                invocation_context=replace(context, input_modalities=("text",)),
            )
            assert result.state == "APPLICATION_ERROR"
            assert json.loads(result.content) == {
                "error": "MODEL_IMAGE_INPUT_UNSUPPORTED"
            }
        finally:
            borrow.close()
            await port.aclose()

    asyncio.run(run())


def test_video_public_projection_requires_trusted_tool_and_true_index():
    from pulsara_agent.llm.input import LLMTextPart
    from tests.support.images import bound_test_image

    image = bound_test_image("image/png", b"test-image", 1, 1)
    content = FrozenPromptContent(
        (LLMTextPart('{"video_frames":true,"frames":[]}'), image, image)
    )
    with pytest.raises(ValueError, match="exactly one"):
        frozen_tool_result_public_text(content)
    assert (
        json.loads(
            frozen_tool_result_public_text(content, tool_name="read_video_frames")
        )["video_frames"]
        is True
    )
    false_index = FrozenPromptContent((LLMTextPart('{"video_frames":false}'), image))
    with pytest.raises(ValueError, match="complete time index"):
        frozen_tool_result_public_text(false_index, tool_name="read_video_frames")


def test_media_settings_freeze_once_off_main_loop_only_for_transcript(tmp_path):
    import threading
    from time import monotonic
    from pulsara_agent.media.runtime import MediaAsrRuntime

    path = video(tmp_path / "silent-video.mkv", ["red"] * 10)
    main_thread = threading.get_ident()
    reads = []

    def read_settings():
        assert threading.get_ident() != main_thread
        reads.append(True)
        return SimpleNamespace(asr=None, credential_scrub_set=lambda: ())

    async def run():
        runtime = MediaAsrRuntime()
        validator = HostPromptImageValidator()
        port = port_at(
            tmp_path,
            validator,
            read_media_settings=read_settings,
            media_asr_runtime=runtime,
        )
        try:
            annotation = await invoke(
                port,
                "annotate_media",
                {
                    "target": {"kind": "PATH", "path": str(path)},
                    "from": 0,
                    "to": 0.05,
                    "text": "note",
                },
            )
            assert annotation.state == "SUCCESS"
            frames = await invoke(
                port,
                "read_video_frames",
                {"target": {"kind": "PATH", "path": str(path)}, "mode": "detail", "from": 0, "to": 0.05},
                image=True,
            )
            assert frames.state == "SUCCESS", frames.content
            assert reads == []
            text = await invoke(
                port, "read_transcript", {"target": {"kind": "PATH", "path": str(path)}}
            )
            assert json.loads(text.content)["error"] == "NO_AUDIO_TRACK"
            assert reads == [True]
        finally:
            await port.aclose()
            await validator.aclose()
            await runtime.aclose(deadline_monotonic=monotonic() + 5)

    asyncio.run(run())
