"""Real canonical storage preserves ordered multi-image video evidence."""

from datetime import datetime, timezone
import json
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.prompt_content import (
    freeze_canonical_prompt,
    PROMPT_BODY_MEDIA_TYPE,
    PROMPT_BODY_CODEC,
)
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.repository import (
    AssistantToolCallBlock,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    PostgresCanonicalImageReferenceReadPort,
)
from pulsara_agent.llm.input import FrozenPromptContent, LLMTextPart, LLMImagePart
from pulsara_agent.model_input.contracts import (
    ToolAttachmentSource,
    ToolAttachmentSourceMember,
)
from pulsara_agent.model_input.compiler import _tool_attachment_message
from pulsara_agent.llm.adapters.openai.chat_completions import chat_semantic_wire_group
from pulsara_agent.llm.adapters.openai.responses import responses_semantic_wire_group
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from tests.test_kernel_image_input_k2_postgres import (
    _repository,
    _bound_session,
    _direct_intent,
    _id,
    _workspace,
    _rows,
    _image,
    _png,
)

pytestmark = pytest.mark.postgres


def test_multi_image_video_commit_provider_wire_history_and_image_ref(
    stage2_migrated_postgres_database,
):
    repository = _repository(stage2_migrated_postgres_database)
    lease, runtime, _binding = _bound_session(repository)
    intent, _accepted = _direct_intent(
        repository, lease, runtime, FrozenPromptContent.text("inspect sequence")
    )
    assistant_entry_id, call_id, attempt_id = _id("entry"), _id("call"), _id("attempt")
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic() + 30
    )
    arguments = {
        "target": {"kind": "PATH", "path": "/tmp/sequence.mkv"},
        "from": 0,
        "to": 0.2,
        "mode": "motion",
    }
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes(b"inspect"),
        blocks=(
            AssistantToolCallBlock(
                block_id=_id("block"),
                tool_call_id=call_id,
                tool_name="read_video_frames",
                arguments=freeze_json(arguments),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    permission = str(
        _rows(
            repository,
            "SELECT permission_snapshot_fingerprint FROM pulsara_v3.turns WHERE id=%s",
            (intent.turn_id,),
        )[0]["permission_snapshot_fingerprint"]
    )
    repository.accept_tool_attempt(
        lease.guard,
        attempt_id=attempt_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=call_id,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="read_video_frames",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        permission_snapshot_fingerprint=permission,
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    workspace = _workspace(repository, lease)
    first = _image(workspace_id=workspace)
    middle = _image(_png(size=(8, 6)), workspace_id=workspace)
    # A/B/A uses three independent timeline occurrences with shared A bytes.
    body = json.dumps(
        {
            "video_frames": True,
            "frames": [
                {"image": 1, "t": 0},
                {"image": 2, "t": 0.04},
                {"image": 3, "t": 0.08},
            ],
            "next": None,
        }
    )
    prompt = freeze_canonical_prompt(
        FrozenPromptContent((LLMTextPart(body), first, middle, first)),
        workspace_id=workspace,
    )
    entry_id = _id("entry")
    prepared = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace,
        result_id=_id("result"),
        result_entry_id=entry_id,
        turn_id=intent.turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=call_id,
        attempt_id=attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(
            prompt.body, media_type=PROMPT_BODY_MEDIA_TYPE, codec=PROMPT_BODY_CODEC
        ),
        canonical_prompt=prompt,
        output_body=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        output_omitted=False,
        source_coverage_reason=None,
        output_unavailability_reason=None,
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=1,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="read_video_frames",
    )
    repository.accept_tool_result(
        lease.guard, candidate=prepared, deadline_monotonic=monotonic() + 30
    )
    assert repository.confirm_tool_result_winner(
        lease.guard, candidate=prepared, deadline_monotonic=monotonic() + 30
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=intent.turn_id, deadline_monotonic=monotonic() + 30
    )
    snapshot = CanonicalProviderInputReader(
        repository.connection_provider
    ).read_frozen_snapshot(cut, deadline_monotonic=monotonic() + 30)
    result = next(i for i in snapshot.items if i.source_entry_id == entry_id)
    assert result.tool_result_body_text == body
    images = tuple(p for p in result.content if isinstance(p, LLMImagePart))
    assert images == (first, middle, first)
    carrier = _tool_attachment_message(
        ToolAttachmentSource(
            assistant_entry_id, (ToolAttachmentSourceMember(0, result),)
        )
    )
    assert tuple(p for p in carrier.content if isinstance(p, LLMImagePart)) == images
    for wire in (
        chat_semantic_wire_group(carrier),
        responses_semantic_wire_group(carrier),
    ):
        assert str(wire).count("base64,") == 3
    rows = _rows(
        repository,
        "SELECT ref_ordinal FROM pulsara_v3.canonical_image_refs WHERE transcript_entry_id=%s",
        (entry_id,),
    )
    assert len(rows) == 3
    reader = PostgresCanonicalImageReferenceReadPort(
        repository.connection_provider,
        session_id=lease.guard.session_id,
        workspace_id=workspace,
    )
    recovered = reader.read_image(
        session_id=lease.guard.session_id,
        workspace_id=workspace,
        image_ref=middle.image_ref,
        maximum_encoded_bytes=4 << 20,
        deadline_monotonic=monotonic() + 30,
    )
    assert recovered == middle
