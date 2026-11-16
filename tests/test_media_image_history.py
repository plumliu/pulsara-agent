"""Complete physical video evidence retains timeline order through C and D1."""

import asyncio
from datetime import datetime, timezone
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.prompt_storage import (
    PostgresCanonicalImageReferenceReadPort,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.tool_outputs import (
    PostgresToolOutputReadPort,
    prepare_native_tool_observation,
)
from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import LLMImagePart
from pulsara_agent.llm.adapters.openai.chat_completions import chat_semantic_wire_group
from pulsara_agent.llm.adapters.openai.responses import responses_semantic_wire_group
from pulsara_agent.model_input.compiler import _tool_attachment_message
from pulsara_agent.model_input.contracts import (
    ModelInputScopeKind,
    ToolAttachmentSource,
    ToolAttachmentSourceMember,
)
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from tests.support.tool_outputs import _install_tool_call
from tests.test_hardcut_entry_parts import read
from tests.test_hardcut_media_contract import _video, _execute
from tests.test_hardcut_media_delivery import repository as repository
from tests.test_hardcut_tool_settlement import executor
from tests.test_native_tool_delivery_postgres import freeze_page

pytestmark = pytest.mark.postgres


def test_multi_image_video_commit_provider_wire_history_and_image_ref(
    repository, tmp_path
):
    path = tmp_path / "sequence.mkv"
    _video(path, ["red"] * 5 + ["blue"] * 4 + ["red"])
    arguments = {
        "target": {"kind": "PATH", "path": path.name},
        "from": 0,
        "to": 1,
        "action": "detail",
    }
    physical = asyncio.run(_execute(tmp_path, "read_video_frames", arguments))
    assert physical.state == "SUCCESS"
    assets = {image.image_ref: image for image in physical.pending_images}
    images = tuple(
        assets[part.value["image_ref"]]
        for part in physical.content.content
        if part.type == "image"
    )
    assert len(images) == 3 and images[0] == images[2] and images[0] != images[1]
    workspace = random_id("workspace")
    lease, turn, assistant, call, attempt = _install_tool_call(
        repository, workspace, tool_name="read_video_frames", arguments=arguments
    )
    entry = random_id("entry")
    # Acceptance and every later read consume only this completed value.
    path.unlink()
    asyncio.run(
        executor(repository, lease)._settle_known_tool_result(
            session_id=lease.guard.session_id,
            turn_id=turn,
            assistant_entry_id=assistant,
            tool_name="read_video_frames",
            tool_call_id=call,
            invocation_arguments=arguments,
            call_ordinal=0,
            scope_kind=ModelInputScopeKind.ROOT,
            result_id=random_id("result"),
            result_entry_id=entry,
            attempt_id=attempt,
            workspace_id=workspace,
            result=KernelToolResult(
                physical.state, physical.content, pending_images=physical.pending_images
            ),
            observed_at=datetime.now(timezone.utc),
            observation_origin=ToolObservationOrigin.BUILTIN,
            live_sink=None,
            tool_result_block_id=random_id("block"),
            live_attribution={},
        )
    )
    page = prepare_native_tool_observation(
        repository,
        lease.guard,
        workspace_id=workspace,
        result_entry_id=entry,
        deadline_monotonic=monotonic() + 30,
    )
    assert page.next is None and page.images == images
    freeze_page(repository, lease.guard, result_entry_id=entry, page=page)
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn, deadline_monotonic=monotonic() + 30
    )
    snapshot = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    result = next(item for item in snapshot.items if item.source_entry_id == entry)
    assert (
        tuple(part for part in result.content if isinstance(part, LLMImagePart))
        == images
    )
    carrier = _tool_attachment_message(
        ToolAttachmentSource(assistant, (ToolAttachmentSourceMember(0, result),))
    )
    for wire in (
        chat_semantic_wire_group(carrier),
        responses_semantic_wire_group(carrier),
    ):
        assert str(wire).count("base64,") == 3
    value = (
        PostgresToolOutputReadPort(
            repository.connection_provider,
            session_id=lease.guard.session_id,
            workspace_id=workspace,
        )
        .read_result({"kind": "SESSION_ENTRY", "entry_id": entry})
        .value
    )
    assert [
        part["value"]["image_ref"]
        for part in value["content"]
        if part["type"] == "image"
    ] == [image.image_ref for image in images]
    with read(repository) as connection:
        refs = connection.execute(
            "SELECT blob_id FROM pulsara_v3.content_parts WHERE entry_id=%s AND type='image' ORDER BY ordinal",
            (entry,),
        ).fetchall()
        assert [row["blob_id"] for row in refs] == [image.image_ref for image in images]
    reader = PostgresCanonicalImageReferenceReadPort(repository.connection_provider)
    assert (
        reader.read_image(
            image_ref=images[1].image_ref,
            maximum_encoded_bytes=4 << 20,
            deadline_monotonic=monotonic() + 30,
        )
        == images[1]
    )
