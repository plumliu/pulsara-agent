"""New accepted supplements stay original across real Host compaction."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from io import BytesIO

from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.compaction.contracts import (
    ResolvedCompactionPolicy,
)
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import (
    LLMImagePart,
    LLMTextPart,
    MessageRole,
    PromptContent,
    PromptImagePart,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_compaction_request_start_dogfood import _seed_history
from tests.support.model_config import test_model_binding, test_model_runtime
from tests.test_http413_compaction_sdk import ByteGate, _rows, byte_gated_server


@pytest.mark.postgres
@pytest.mark.parametrize("with_image", (False, True), ids=("text", "image"))
def test_host_compaction_excludes_new_supplement_and_resends_original_once(
    tmp_path,
    monkeypatch,
    isolated_migrated_postgres_database,
    with_image,
):
    import pulsara_agent.conversation_kernel.host as host_module

    async def scenario():
        monkeypatch.setenv("PULSARA_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
        monkeypatch.setattr(
            host_module.LocalMcpManagementService, "load_configs", lambda *_a, **_k: ()
        )
        gate = ByteGate(byte_limit=4_000_000, tool_before_failure=True)
        marker = "UNCONSUMED_SUPPLEMENT_STAYS_ORIGINAL"
        out = BytesIO()
        Image.new("RGB", (7, 5), (13, 29, 47)).save(out, "PNG")
        supplement = (
            PromptContent(
                (LLMTextPart(marker), PromptImagePart(out.getvalue(), "image/png"))
            )
            if with_image
            else PromptContent.text(marker)
        )
        with byte_gated_server("openai_chat_completions", gate) as endpoint:
            runtime = test_model_runtime(
                base_url=endpoint,
                wire_api="openai_chat_completions",
                postgres_dsn=isolated_migrated_postgres_database.runtime_dsn,
            )
            core = KernelHostCore.production(model_runtime=runtime)
            root = await core.open_session(
                HostWorkspaceInput(workspace_kind="project", workspace_root=tmp_path)
            )
            try:
                await root.update_model_call_binding(test_model_binding(runtime))
                root._compaction.policy = ResolvedCompactionPolicy(
                    auto_trigger_ratio=0.99
                )
                _seed_history(root, segments=5, repetitions=250)
                original_batch = root._runner._execute_tool_batch_with_page_recovery
                original_install = root._runner._provider_dispatch.install_provider_open
                installed = []
                queued = []

                async def record_install(**kwargs):
                    dispatch = kwargs["dispatch"]
                    installed.append(
                        (
                            dispatch.canonical_read.compile_snapshot.canonical_input,
                            dispatch.append_result.compiled_input,
                        )
                    )
                    return await original_install(**kwargs)

                async def after_settled_batch(**kwargs):
                    result = await original_batch(**kwargs)
                    assert not queued
                    # A real business assistant/tool request was accepted before
                    # the new steer; this is a later input cut in the same turn.
                    assert _rows(
                        root,
                        "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s AND id=%s AND turn_id=%s",
                        (
                            root.session_id,
                            kwargs["assistant_entry_id"],
                            kwargs["turn_id"],
                        ),
                    )
                    receipt = await root.steer_active_turn(
                        command_id=random_id("command"),
                        content=supplement,
                        target_turn_id=kwargs["turn_id"],
                    )
                    assert receipt.status == "PENDING"
                    queued.append(receipt)
                    root._compaction.policy = replace(
                        root._compaction.policy, auto_trigger_ratio=0.01
                    )
                    return result

                monkeypatch.setattr(
                    root._runner,
                    "_execute_tool_batch_with_page_recovery",
                    after_settled_batch,
                )
                monkeypatch.setattr(
                    root._runner._provider_dispatch,
                    "install_provider_open",
                    record_install,
                )
                result = await root.run_turn(
                    PromptContent.text("Execute the requested work once, then finish."),
                    command_id=random_id("command"),
                    requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                )
                assert result.final_text == "HTTP413_FINISHED"
                summaries = [request for request in gate.requests if request.summary]
                assert len(summaries) == 1
                assert marker not in summaries[0].raw.decode()
                assert b"data:image/" not in summaries[0].raw
                assert all(request.status == 200 for request in gate.requests)
                assert (
                    len(gate.requests) == 3
                )  # business tool call, summary, continuation
                assert len(installed) == 2
                canonical, compiled = installed[-1]
                originals = [
                    item
                    for item in canonical.items
                    if item.input_origin is not None
                    and item.input_origin.value == "HUMAN_STEER"
                ]
                assert len(originals) == 1
                original = originals[0]
                assert tuple(
                    part.text
                    for part in original.content
                    if isinstance(part, LLMTextPart)
                ) == (marker,)
                placements = [
                    placement
                    for placement in compiled.message_placements
                    if placement.origin_entry_id == original.source_entry_id
                ]
                assert len(placements) == 1
                assert placements[0].role is MessageRole.USER
                resent = compiled.messages[placements[0].message_ordinal]
                assert resent.content[0] == original.content[0]
                assert tuple(
                    part for part in resent.content if isinstance(part, LLMImagePart)
                ) == tuple(
                    part for part in original.content if isinstance(part, LLMImagePart)
                )
                assert len(resent.content) == 1 + 2 * int(with_image)
                if with_image:
                    from pulsara_agent.model_input.lowering import image_reference_part

                    assert resent.content[1] == image_reference_part(
                        original.content[1]
                    )
                assert (
                    sum(
                        any(
                            isinstance(part, LLMTextPart) and marker in part.text
                            for part in message.content
                        )
                        for message in compiled.messages
                    )
                    == 1
                )
                images = [
                    part for part in original.content if isinstance(part, LLMImagePart)
                ]
                assert len(images) == int(with_image)
                if images:
                    encoded = base64.b64encode(images[0].immutable_bytes)
                    assert gate.requests[-1].raw.count(encoded) == 1
                    assert encoded not in summaries[0].raw
                rows = _rows(
                    root,
                    "SELECT id,entry_sequence FROM pulsara_v3.transcript_entries WHERE session_id=%s AND input_origin='HUMAN_STEER'",
                    (root.session_id,),
                )
                assert len(rows) == 1
                assert rows[0]["id"] == original.source_entry_id
                assert rows[0]["entry_sequence"] == original.source_entry_sequence
                assert (
                    len(
                        _rows(
                            root,
                            "SELECT id FROM pulsara_v3.context_snapshots WHERE session_id=%s",
                            (root.session_id,),
                        )
                    )
                    == 1
                )
                attempts = _rows(
                    root,
                    "SELECT i.source_kind, count(*) AS amount FROM pulsara_v3.tool_execution_attempts a "
                    "JOIN pulsara_v3.tool_invocations i ON i.session_id=a.session_id AND i.id=a.invocation_id "
                    "WHERE a.session_id=%s GROUP BY i.source_kind",
                    (root.session_id,),
                )
                assert {row["source_kind"]: row["amount"] for row in attempts} == {
                    "MODEL_TOOL_CALL": 1,
                    "CODE_MODE_CALL": 1,
                }
                assert (
                    tmp_path / "http413-sideeffect.txt"
                ).read_text() == "written once"
                assert root._runner._active_execution is None
            finally:
                await core.shutdown()

    asyncio.run(scenario())
