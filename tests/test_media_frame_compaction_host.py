"""Real media is acquired once; compaction and NEXT deliver frozen C results."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json

import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import (
    LLMImagePart,
    LLMTextPart,
    MessageRole,
    PromptContent,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import (
    test_model_binding as model_binding,
    test_model_runtime as model_runtime,
)
from tests.dogfood.run_compaction_request_start_dogfood import _seed_history, _rows
from tests.support.round3 import CallbackScriptedKernelModel
from pulsara_agent.llm.result import TransportUsageReport
from pulsara_agent.ports.live_agent_event import (
    TextStartPayload,
    TextDeltaPayload,
    TextEndPayload,
    live_digest,
)
from pulsara_agent.ports.provider_stream import (
    ProviderNormalizedTerminalKind,
    ProviderPhysicalCompletion,
    ProviderPhysicalCompletionStatus,
    ProviderStreamTerminal,
)
from tests.test_media_overview_policy import make_video
from tests.test_round4_plan_host import _text, _tool_call

pytestmark = pytest.mark.postgres


class _SummaryTransport:
    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()

    def open_stream(self, *, call, context):
        self.calls.append(call)
        owner = self
        text = "Earlier investigation finished; continue the exact current request."
        block = random_id("block")

        class Execution:
            def __init__(self):
                self.items = [
                    TextStartPayload(block),
                    TextDeltaPayload(block, text),
                    TextEndPayload(block, text, len(text.encode()), live_digest(text)),
                    ProviderStreamTerminal(
                        terminal_kind=ProviderNormalizedTerminalKind.COMPLETED,
                        usage=TransportUsageReport(usage_status="missing", usage=None),
                    ),
                ]

            async def read_next(self):
                await owner.release.wait()
                return self.items.pop(0) if self.items else None

            async def aclose(self):
                self.items.clear()

            async def request_cancel(self, *, reason):
                owner.release.set()
                await self.aclose()

            async def wait_physical_completion(self):
                return ProviderPhysicalCompletion(
                    ProviderPhysicalCompletionStatus.COMPLETED, None
                )

        return Execution()


class _Model(CallbackScriptedKernelModel):
    def __init__(self):
        super().__init__(self._stream)
        self.root_requests = []
        self.summary = _SummaryTransport()

    def resolve_compaction_summary_call(self, **kwargs):
        call = super().resolve_compaction_summary_call(**kwargs)
        self.summary.binding_id = call.target.transport.binding_id
        call = replace(call, target=replace(call.target, transport=self.summary))
        self.model_runtime.register_test_call_override(call)
        return call


def _tool_native_text(request, call_id):
    results = [
        message
        for message in request.compiled_input.messages
        if message.role is MessageRole.TOOL_RESULT and message.tool_call_id == call_id
    ]
    assert len(results) == 1
    return "".join(
        part.text for part in results[0].content if isinstance(part, LLMTextPart)
    )


def _tool_images(request, call_id):
    images = []
    for message in request.compiled_input.messages:
        owner = None
        for part in message.content:
            if isinstance(part, LLMTextPart):
                try:
                    value = json.loads(part.text)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(value, dict)
                    and "tool_call_id" in value
                    and "image" in value
                ):
                    owner = value["tool_call_id"]
            elif isinstance(part, LLMImagePart) and owner == call_id:
                images.append(part)
    return tuple(images)


@pytest.mark.parametrize("action", ["overview", "detail"])
def test_host_compaction_preserves_complete_result_and_native_next_without_reexecution(
    tmp_path,
    monkeypatch,
    isolated_migrated_postgres_database,
    action,
):
    import pulsara_agent.conversation_kernel.host as host_module
    import pulsara_agent.media.execution as runtime_module
    import pulsara_agent.conversation_kernel.native_tool_delivery as delivery

    path = make_video(tmp_path / "states.mkv", ["red"] * 7 + ["blue"], rate=4)
    arguments = {
        "target": {"kind": "PATH", "path": str(path)},
        "from": 0,
        "to": 2,
        "action": action,
    }
    # A small per-page allowance exercises NEXT; physical media selection stays
    # complete and the provider still admits its ordinary full request budget.
    monkeypatch.setattr(delivery, "NATIVE_TOOL_PAGE_TOKENS", 1000)

    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        runtime = model_runtime(
            postgres_dsn=isolated_migrated_postgres_database.runtime_dsn,
            model_id="test-pro",
            wire_api="openai_chat_completions",
            input_modalities=("text", "image"),
        )

        class Model(_Model):
            def __init__(self):
                super().__init__()
                self._model_runtime = runtime
                self._preparer = DirectKernelModelPort(model_runtime=runtime)
                self.call = None
                self.pages = []
                self.images = []
                self.summary.release.set()

            async def _stream(self, request):
                self.root_requests.append(request)
                if self.call is None:
                    self.initial_binding = request.cut.context_binding_revision_id
                    self.call = random_id("call")
                    events = _tool_call("read_video_frames", self.call, arguments)
                else:
                    root._compaction.policy = replace(root._compaction.policy, automatic_enabled=False)
                    body = _tool_native_text(request, self.call)
                    assert "status: SUCCESS" in body, body
                    self.pages.append(body)
                    self.images.extend(_tool_images(request, self.call))
                    following = json.loads(body.rsplit("\nnext: ", 1)[1])
                    if following is None:
                        events = _text("FROZEN_MEDIA_DONE")
                    else:
                        self.call = random_id("call")
                        events = _tool_call(
                            "read_file", self.call, {"target": following}
                        )
                for event in events:
                    yield event

        model = Model()
        monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **_: model)
        monkeypatch.setattr(
            host_module.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ()
        )
        core = KernelHostCore.production(model_runtime=runtime)
        root = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        acquired = []
        original = runtime_module.execute_media
        try:
            await root.update_model_call_binding(model_binding(runtime))
            root._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False)
            _seed_history(root, segments=5, repetitions=150)
            forced = replace(root._compaction.policy, automatic_enabled=True, auto_trigger_ratio=0.001)

            async def acquire(*args, **kwargs):
                result = await original(*args, **kwargs)
                acquired.append(result)
                path.unlink()
                root._compaction.policy = forced
                return result

            monkeypatch.setattr(runtime_module, "execute_media", acquire)
            async with asyncio.timeout(90):
                outcome = await root.run_turn(
                    PromptContent.text("Inspect this exact video."),
                    command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
            assert outcome.final_text == "FROZEN_MEDIA_DONE"
            assert len(acquired) == 1 and acquired[0].state == "SUCCESS"
            assert len(model.summary.calls) == 1
            assert (
                model.root_requests[1].cut.context_binding_revision_id
                != model.initial_binding
            )
            # Compaction may adopt a new prefix only at its explicit successor;
            # later NEXT calls keep that successor's system and tools unchanged.
            successor = model.root_requests[1].compiled_input
            for request in model.root_requests[2:]:
                assert request.compiled_input.system_prompt == successor.system_prompt
                assert request.compiled_input.tools == successor.tools
            rows = _rows(
                root,
                "SELECT r.result_entry_id,r.result_state,r.source_result_entry_id FROM pulsara_v3.tool_results r JOIN pulsara_v3.transcript_entries e ON e.id=r.result_entry_id WHERE r.session_id=%s ORDER BY e.entry_sequence",
                (root.session_id,),
            )
            assert all(row["result_state"] == "SUCCESS" for row in rows)
            source = rows[0]["result_entry_id"]
            value = (
                PostgresToolOutputReadPort(
                    root.repository.connection_provider,
                    session_id=root.session_id,
                    workspace_id=root.workspace.workspace_key,
                )
                .read_result({"kind": "SESSION_ENTRY", "entry_id": source})
                .value
            )
            refs = [
                part["value"]["image_ref"]
                for part in value["content"]
                if part["type"] == "image"
            ]
            assert refs == [image.image_ref for image in model.images]
            assert all(row["source_result_entry_id"] == source for row in rows)
            if action == "detail":
                assert len(model.pages) > 1
                indexes = [
                    part["value"]["t"]
                    for part in value["content"]
                    if part["type"] == "object"
                ]
                assert indexes == [0, 1, 1.75]
                assert refs[0] == refs[1] and refs[1] != refs[2]
        finally:
            model.summary.release.set()
            await core.shutdown()

    asyncio.run(scenario())


def test_stop_parallel_physical_reads_releases_workers_and_keeps_host_usable(
    tmp_path,
    monkeypatch,
    isolated_migrated_postgres_database,
):
    import pulsara_agent.conversation_kernel.host as host_module
    from pulsara_agent.media.frames import prepare_frame_plan
    from tests.support.round3 import CallbackScriptedKernelModel

    paths = [
        make_video(
            tmp_path / f"cancel-{index}.mkv", ["red"] * 20 + ["blue"] * 20, rate=10
        )
        for index in range(2)
    ]

    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        runtime = model_runtime(
            postgres_dsn=isolated_migrated_postgres_database.runtime_dsn,
            input_modalities=("text", "image"),
            wire_api="openai_chat_completions",
        )

        async def stream(request):
            if len(model.requests) == 1:
                events = [
                    event
                    for path in paths
                    for event in _tool_call(
                        "read_video_frames",
                        random_id("call"),
                        {
                            "target": {"kind": "PATH", "path": str(path)},
                            "from": 0,
                            "to": 4,
                            "action": "overview",
                        },
                    )
                ]
            else:
                events = _text("HOST_STILL_USABLE")
            for event in events:
                yield event

        model = CallbackScriptedKernelModel(stream)
        model._model_runtime = runtime
        model._preparer = DirectKernelModelPort(model_runtime=runtime)
        monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **_: model)
        monkeypatch.setattr(
            host_module.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ()
        )
        core = KernelHostCore.production(model_runtime=runtime)
        root = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
        )
        entered = asyncio.Event()
        worker = root._tools._media_worker
        original = worker.run

        async def hold(operation, *args, **kwargs):
            if operation is prepare_frame_plan:
                previous = kwargs.get("on_started")

                def started():
                    entered.set()
                    if previous is not None:
                        return previous()

                kwargs["on_started"] = started
            return await original(operation, *args, **kwargs)

        monkeypatch.setattr(worker, "run", hold)
        running = None
        command = random_id("command")
        try:
            await root.update_model_call_binding(model_binding(runtime))
            root._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False)
            running = asyncio.create_task(
                root.run_turn(
                    PromptContent.text("Inspect both videos."),
                    command_id=command,
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
            )
            await asyncio.wait_for(entered.wait(), 20)
            target = root.active_root_turn_id()
            assert target is not None and not running.done()
            await root.request_stop_turn(
                command_id=random_id("command"),
                target_turn_id=target,
                admission_deadline_ms=root.control_admission_deadline_ms(),
                expected_session_id=root.session_id,
                expected_host_session_id=root.host_session_id,
            )
            async with asyncio.timeout(30):
                await asyncio.gather(running, return_exceptions=True)
            assert (await root.query_command(command)).public_code == "USER_STOPPED"
            assert not worker._active and not worker._processes
            assert root._image_validator._active_process is None
            outcome = await root.run_turn(
                PromptContent.text("Continue after cancellation."),
                command_id=random_id("command"),
            )
            assert outcome.final_text == "HOST_STILL_USABLE"
        finally:
            if running is not None:
                await asyncio.gather(running, return_exceptions=True)
            await core.shutdown()

    asyncio.run(scenario())
