"""Mixed tool batches settle real results and continue to the model's reply."""

from __future__ import annotations

import asyncio
import json

from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import ResolvedCompactionPolicy
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import LLMImagePart, PromptContent
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_binding as model_binding, test_model_runtime as model_runtime
from tests.support.round3 import CallbackScriptedKernelModel
from tests.test_compaction_request_start_integration import _rows
from tests.test_media_frame_compaction_host import _delivered_page, _tool_public_result
from tests.test_media_overview_policy import make_video
from tests.test_round4_plan_host import _text, _tool_call


pytestmark = pytest.mark.postgres


@pytest.mark.parametrize(
    "serial_tool,image_tool,serial_first",
    [
        pytest.param("exec_process", "read_video_frames", True, id="terminal-before-video"),
        pytest.param("exec_process", "read_video_frames", False, id="terminal-after-video"),
        pytest.param("read_file", "read_video_frames", True, id="file-before-video"),
        pytest.param("annotate_media", "read_video_frames", True, id="annotation-before-video"),
        pytest.param("read_transcript", "read_video_frames", True, id="transcript-error-before-video"),
        pytest.param("exec_process", "view_image", True, id="terminal-before-images"),
    ],
)
def test_mixed_tools_deliver_all_results_and_complete_turn(
    tmp_path, monkeypatch, isolated_migrated_postgres_database,
    serial_tool, image_tool, serial_first,
):
    import pulsara_agent.conversation_kernel.host as host_module

    path = make_video(tmp_path / "states.mkv", ["red"] * 20 + ["blue"] * 20 + ["green"] * 20)
    image = tmp_path / "state.png"
    Image.new("RGB", (320, 180), "red").save(image)
    note = tmp_path / "note.txt"
    note.write_text("MIXED_BATCH_FILE_OK", encoding="utf-8")
    target = {"kind": "PATH", "path": str(path)}
    serial_arguments = {
        "exec_process": {"command": "printf 'MIXED_BATCH_TERMINAL_OK\\n'"},
        "read_file": {"target": {"kind": "PATH", "path": str(note)}},
        "annotate_media": {"target": target, "from": 0, "to": 2, "text": "The opening screen is red."},
        "read_transcript": {"target": target, "from": 0, "to": 2},
    }[serial_tool]
    serial_call = (random_id("call"), serial_tool, serial_arguments)
    image_calls = [
        (
            random_id("call"), image_tool,
            {"target": target, "from": start, "to": start + 2, "mode": "detail"}
            if image_tool == "read_video_frames"
            else {"target": {"kind": "PATH", "path": str(image)}},
        )
        for start in (0, 2, 4)
    ]
    calls = [serial_call, *image_calls] if serial_first else [*image_calls, serial_call]
    serial_state = "APPLICATION_ERROR" if serial_tool == "read_transcript" else "SUCCESS"

    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        runtime = model_runtime(
            postgres_dsn=isolated_migrated_postgres_database.runtime_dsn,
            model_id="test-pro", wire_api="openai_chat_completions",
            input_modalities=("text", "image"),
        )

        async def stream(request):
            if len(model.requests) == 1:
                events = [event for call_id, name, arguments in calls
                          for event in _tool_call(name, call_id, arguments)]
            else:
                assert len(model.requests) == 2
                serial_result = _tool_public_result(request, serial_call[0])
                assert serial_result["result_state"] == serial_state
                if serial_tool == "exec_process":
                    body = json.loads(serial_result["body"])
                    assert body["exit_code"] == 0
                    assert body["output"] == "MIXED_BATCH_TERMINAL_OK\n"
                elif serial_tool == "read_file":
                    assert "MIXED_BATCH_FILE_OK" in serial_result["body"]
                elif serial_tool == "annotate_media":
                    assert json.loads(serial_result["body"])["media_annotation"] == "added"
                else:
                    assert json.loads(serial_result["body"])["error"] == "NO_AUDIO_TRACK"
                for call_id, _, arguments in image_calls:
                    if image_tool == "read_video_frames":
                        _, body, attachments = _delivered_page(request, call_id)
                        assert body["requested_range"] == [arguments["from"], arguments["to"]]
                        assert body["next"] is None and attachments
                        assert all(arguments["from"] <= frame["t"] < arguments["to"]
                                   for frame in body["frames"])
                    else:
                        assert _tool_public_result(request, call_id)["result_state"] == "SUCCESS"
                assert any(isinstance(part, LLMImagePart)
                           for message in request.compiled_input.messages for part in message.content)
                events = _text("MIXED_TOOL_BATCH_COMPLETED")
            for event in events:
                yield event

        model = CallbackScriptedKernelModel(stream)
        model._model_runtime = runtime
        model._preparer = DirectKernelModelPort(model_runtime=runtime)
        monkeypatch.setattr(host_module, "DirectKernelModelPort", lambda **_: model)
        monkeypatch.setattr(host_module.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ())
        core = KernelHostCore.production(model_runtime=runtime)
        root = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path))
        try:
            await root.update_model_call_binding(model_binding(runtime))
            root._compaction.policy = ResolvedCompactionPolicy(automatic_enabled=False)
            async with asyncio.timeout(90):
                result = await root.run_turn(
                    PromptContent.text("Inspect the supplied media and report your findings."),
                    command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
            assert result.final_text == "MIXED_TOOL_BATCH_COMPLETED"
            assert result.tool_call_count == len(calls)
            assert len(model.requests) == 2
            assert _rows(root, "SELECT status,terminal_reason FROM pulsara_v3.turns WHERE id=%s",
                         (result.turn_id,)) == [{"status": "COMPLETED", "terminal_reason": "COMPLETED"}]
            results = _rows(root, """
                SELECT r.tool_call_id,r.result_state FROM pulsara_v3.tool_results r
                JOIN pulsara_v3.transcript_entries e ON e.id=r.result_entry_id AND e.session_id=r.session_id
                WHERE r.session_id=%s ORDER BY e.entry_sequence
            """, (root.session_id,))
            assert results == [
                {"tool_call_id": call_id, "result_state": serial_state if call_id == serial_call[0] else "SUCCESS"}
                for call_id, _, _ in calls
            ]
        finally:
            await core.shutdown()

    asyncio.run(scenario())
