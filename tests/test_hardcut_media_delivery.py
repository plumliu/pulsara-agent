"""Media physical outcomes and the shared native/UI delivery boundaries."""

import asyncio
from datetime import datetime, timezone
from io import BytesIO
import json

from PIL import Image
import pytest

from pulsara_agent.conversation_kernel.image_validation import HostPromptImageValidator
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.prompt_storage import (
    PostgresCanonicalImageReferenceReadPort,
)
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.tool_outputs import PostgresToolOutputReadPort
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import LLMImagePart
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from tests.support.model_config import scheduled_task_service
from tests.support.postgres import verified_postgres_provider
from tests.support.round3 import direct_tool_invocation_context
from tests.support.tool_outputs import _install_tool_call
from tests.test_hardcut_entry_parts import read
from tests.test_hardcut_tool_settlement import executor

pytestmark = pytest.mark.postgres


@pytest.fixture
def repository(stage2_migrated_postgres_database):
    return ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )


@pytest.mark.parametrize(
    "supports_images,missing", [(True, False), (False, False), (True, True)]
)
def test_view_image_physical_value_is_saved_before_capability_projection(
    repository, tmp_path, supports_images, missing
):
    data = BytesIO()
    Image.new("RGB", (17, 13), "navy").save(data, format="PNG")
    path = tmp_path / "picture.png"
    path.write_bytes(data.getvalue())
    arguments = (
        {"target": {"kind": "IMAGE_REF", "image_ref": random_id("image")}}
        if missing
        else {"target": {"kind": "PATH", "path": path.name}}
    )
    workspace = random_id("workspace")
    lease, turn, assistant, call, attempt = _install_tool_call(
        repository, workspace, tool_name="view_image", arguments=arguments
    )
    result_id, entry_id = random_id("result"), random_id("entry")

    async def scenario():
        validator = HostPromptImageValidator()
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id=random_id("host"),
            session_id=lease.guard.session_id,
            live_bus=LiveAgentEventBus(),
            image_validator=validator,
            image_reference_read_port=PostgresCanonicalImageReferenceReadPort(
                repository.connection_provider
            ),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
            scheduled_tasks=scheduled_task_service(),
        )
        borrow, context = direct_tool_invocation_context(
            port,
            session_id=lease.guard.session_id,
            workspace_id=workspace,
            tool_name="view_image",
            tool_call_id=call,
            attempt_id=attempt,
            turn_id=turn,
            assistant_entry_id=assistant,
        )
        from dataclasses import replace

        context = replace(
            context,
            input_modalities=("text", "image") if supports_images else ("text",),
        )
        try:
            physical = await port.invoke(
                tool_name="view_image",
                arguments=arguments,
                tool_call_id=call,
                attempt_id=attempt,
                turn_id=turn,
                assistant_entry_id=assistant,
                invocation_context=context,
            )
        finally:
            borrow.close()
            await port.aclose()
            await validator.aclose()
        # A frozen result must survive source deletion and an exhausted old
        # picture allowance. Neither is permission to read or render again.
        path.unlink()
        settled = await executor(repository, lease)._settle_known_tool_result(
            session_id=lease.guard.session_id,
            turn_id=turn,
            assistant_entry_id=assistant,
            tool_name="view_image",
            tool_call_id=call,
            invocation_arguments=arguments,
            call_ordinal=0,
            scope_kind=ModelInputScopeKind.ROOT,
            result_id=result_id,
            result_entry_id=entry_id,
            attempt_id=attempt,
            workspace_id=workspace,
            result=physical,
            observed_at=datetime.now(timezone.utc),
            observation_origin=ToolObservationOrigin.BUILTIN,
            live_sink=None,
            tool_result_block_id=random_id("block"),
            live_attribution={},
            input_modalities=context.input_modalities,
        )
        return physical, settled.settlement

    physical, settlement = asyncio.run(scenario())
    value = (
        PostgresToolOutputReadPort(
            repository.connection_provider,
            session_id=lease.guard.session_id,
            workspace_id=workspace,
        )
        .read_result({"kind": "SESSION_ENTRY", "entry_id": entry_id})
        .value
    )
    from tests.test_media_business_output_schema import validate_media_js_value

    asyncio.run(validate_media_js_value("view_image", value, physical.state))
    if missing:
        assert (
            physical.state == "APPLICATION_ERROR" and value["meta"]["status"] == "ERROR"
        )
        assert all(part["type"] == "text" for part in value["content"])
        assert "图片不存在或已不可用" in value["content"][0]["value"]
    else:
        assert physical.state == "SUCCESS" and value["meta"]["status"] == "SUCCESS"
        assert [part["type"] for part in value["content"]] == ["text", "image"]
        assert len(physical.pending_images) == 1
        assert physical.pending_images[0].immutable_bytes == data.getvalue()
        if supports_images:
            assert any(
                isinstance(part, LLMImagePart) for part in settlement.canonical_content
            )
        else:
            assert settlement.canonical_content is None
            assert (
                "当前模型不支持图片输入，未交付实际图片。"
                in settlement.public_projection.canonical_body
            )
            assert "ERROR" in settlement.public_projection.canonical_body
    with read(repository) as connection:
        from pulsara_agent.terminal_protocol.canonical_v3 import _owner_display_content

        row = connection.execute(
            "SELECT * FROM pulsara_v3.transcript_entries WHERE id=%s", (entry_id,)
        ).fetchone()
        wire = _owner_display_content(connection, row)
    assert wire["content_media_type"] == "application/vnd.pulsara.tool-result+json"
    assert json.loads(wire["inline_content"]) == json.loads(json.dumps(value))


def test_large_typed_business_wire_hydrates_through_existing_read_content(repository):
    """The UI carrier uses ordinary chunks, without an inline payload duplicate."""
    from time import monotonic
    from pulsara_agent.conversation_kernel.content_parts import ResultPart
    from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
    from pulsara_agent.ports.tool_execution import ToolBusinessValue
    from pulsara_agent.terminal_protocol.canonical_v3 import CanonicalProtocolReader
    from pulsara_agent.terminal_protocol.generated_v3 import (
        terminal_kernel_v3_pb2 as wire,
    )
    from pulsara_agent.terminal_protocol.v3_gateway import TerminalKernelProtocolServer

    workspace = random_id("workspace")
    lease, turn, assistant, call, attempt = _install_tool_call(
        repository,
        workspace,
        tool_name="read_transcript",
        arguments={"target": {"kind": "PATH", "path": "captions.srt"}},
    )
    entry = random_id("entry")
    text = "Original subtitle wording 不改写。\r\n" * 6000
    value = ToolBusinessValue(
        {
            "target": {"kind": "PATH", "path": "captions.srt"},
            "meta": {"source": "subtitle"},
        },
        (ResultPart("object", {"from": 0, "to": 1000, "text": text}),),
    )
    asyncio.run(
        executor(repository, lease)._settle_known_tool_result(
            session_id=lease.guard.session_id,
            turn_id=turn,
            assistant_entry_id=assistant,
            tool_name="read_transcript",
            tool_call_id=call,
            invocation_arguments={},
            call_ordinal=0,
            scope_kind=ModelInputScopeKind.ROOT,
            result_id=random_id("result"),
            result_entry_id=entry,
            attempt_id=attempt,
            workspace_id=workspace,
            result=KernelToolResult("SUCCESS", value),
            observed_at=datetime.now(timezone.utc),
            observation_origin=ToolObservationOrigin.BUILTIN,
            live_sink=None,
            tool_result_block_id=random_id("block"),
            live_attribution={},
        )
    )
    reader = CanonicalProtocolReader(repository.connection_provider)
    snapshot = reader.snapshot(
        session_id=lease.guard.session_id,
        maximum_entries=20,
        maximum_control_items=20,
        deadline_monotonic=monotonic() + 30,
    )
    reference = next(
        item.content for item in snapshot.entries if item.entry_id == entry
    )
    assert reference.kind == wire.CANONICAL_BLOB and not reference.inline_content
    assert reference.media_type == "application/vnd.pulsara.tool-result+json"

    async def hydrate():
        chunks, offset = [], 0
        while True:
            response = await TerminalKernelProtocolServer.read_canonical_content(
                connection_provider=repository.connection_provider,
                protocol_reader=reader,
                session_id=lease.guard.session_id,
                request=wire.ReadContentRequest(
                    request_id=random_id("request"),
                    entry_id=entry,
                    offset_bytes=offset,
                    limit_bytes=17000,
                ),
            )
            assert response.HasField("content")
            assert (
                response.content.digest == reference.digest
                and response.content.media_type == reference.media_type
            )
            chunks.append(response.content.content)
            offset += len(response.content.content)
            if response.content.complete:
                return b"".join(chunks)

    hydrated = asyncio.run(hydrate())
    assert len(hydrated) == reference.size
    decoded = json.loads(hydrated)
    assert decoded["meta"] == {"status": "SUCCESS", "source": "subtitle"}
    assert decoded["content"][0]["value"]["text"] == text
