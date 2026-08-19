"""Round 5A.2 durable provider replay contracts and architecture gates."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from pulsara_agent.conversation_kernel.jobs import JOB_HANDLER_CATALOG
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.llm.provider_replay import (
    MAXIMUM_PROVIDER_DISPATCH_COMPOSITE_BYTES,
    MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES,
    ProviderAssistantReplayCodecKind,
    build_prepared_durable_provider_assistant_replay,
    build_provider_replay_target_compatibility,
    provider_replay_id,
)
from pulsara_agent.model_input.provider_replay import (
    ProviderReplayHydrationError,
    ProviderReplayHydrationFailureKind,
    decode_provider_replay_fragment,
    freeze_provider_replay_manifest,
    quote_provider_dispatch_composite_bytes,
)
from pulsara_agent.ports.provider_stream import (
    freeze_provider_adapter_completed_replay_payload,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "src/pulsara_agent"
KERNEL = PRODUCTION / "conversation_kernel"


def _target(
    *,
    api: str = "openai_chat_completions",
    endpoint: str = "1",
    model: str = "model-a",
    binding: str | None = None,
):
    return build_provider_replay_target_compatibility(
        wire_api=api,
        endpoint_identity_fingerprint="sha256:" + endpoint * 64,
        normalized_model_identifier=model,
        transport_binding_id=binding or api,
    )


def _frozen_object(value: dict[str, object]) -> FrozenJsonObjectFact:
    frozen = freeze_json(value)
    assert isinstance(frozen, FrozenJsonObjectFact)
    return frozen


def _candidate(
    item: FrozenJsonObjectFact,
    *,
    api: str = "openai_chat_completions",
):
    return build_prepared_durable_provider_assistant_replay(
        session_id="session:test",
        workspace_id="workspace:test",
        assistant_entry_id="entry:test",
        target=_target(api=api),
        public_projection_fingerprint="sha256:" + "2" * 64,
        ordered_items=(item,),
    )


def test_round5a2_replay_target_is_closed_and_process_stable() -> None:
    baseline = _target()
    assert baseline == _target()
    assert baseline.replay_target_fingerprint != _target(endpoint="3").replay_target_fingerprint
    assert baseline.replay_target_fingerprint != _target(model="model-b").replay_target_fingerprint
    assert baseline.replay_target_fingerprint != _target(binding="other-chat-binding").replay_target_fingerprint
    responses = _target(api="openai_responses")
    assert baseline.replay_target_fingerprint != responses.replay_target_fingerprint
    assert baseline.codec_kind is ProviderAssistantReplayCodecKind.CHAT_CLOSED_REASONING_FIELDS
    assert responses.codec_kind is ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS


def test_round5a2_private_body_roundtrips_without_repr_disclosure() -> None:
    sentinel = "private-round5a2-sentinel"
    item = _frozen_object(
        {
            "role": "assistant",
            "content": "public",
            "reasoning_content": sentinel,
        }
    )
    adapter_payload = freeze_provider_adapter_completed_replay_payload(
        codec_kind=ProviderAssistantReplayCodecKind.CHAT_CLOSED_REASONING_FIELDS,
        ordered_items=(item,),
    )
    candidate = _candidate(item)
    fragment = candidate.fragment()
    manifest = freeze_provider_replay_manifest(
        replay_id=provider_replay_id(
            session_id=candidate.session_id,
            assistant_entry_id=candidate.assistant_entry_id,
            wire_api=candidate.wire_api,
        ),
        assistant_entry_id=candidate.assistant_entry_id,
        wire_api=candidate.wire_api,
        codec_kind=candidate.codec_kind.value,
        provider_replay_contract_fingerprint=(
            candidate.provider_replay_contract_fingerprint
        ),
        replay_target_fingerprint=candidate.replay_target_fingerprint,
        public_projection_fingerprint=candidate.public_projection_fingerprint,
        payload_digest=candidate.payload_digest,
        payload_size=candidate.payload_size,
        item_count=candidate.item_count,
        fragment_fingerprint=candidate.fragment_fingerprint,
    )
    decoded = decode_provider_replay_fragment(
        manifest=manifest,
        payload_bytes=candidate.payload_bytes,
    )
    assert decoded == fragment
    for carrier in (adapter_payload, candidate, fragment, decoded):
        assert sentinel not in repr(carrier)


def test_round5a2_chat_payload_boundary_is_exact() -> None:
    empty = _frozen_object(
        {"role": "assistant", "content": None, "reasoning_content": ""}
    )
    overhead = _candidate(empty).payload_size
    body_size = MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES - overhead
    exact = _frozen_object(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "x" * body_size,
        }
    )
    assert _candidate(exact).payload_size == MAXIMUM_PROVIDER_REPLAY_PAYLOAD_BYTES
    over = _frozen_object(
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "x" * (body_size + 1),
        }
    )
    with pytest.raises(ValueError, match="payload"):
        _candidate(over)


def test_round5a2_responses_item_count_and_allowlist_are_closed() -> None:
    target = _target(api="openai_responses")
    items = tuple(
        _frozen_object(
            {
                "type": "reasoning",
                "id": f"reasoning:{index}",
                "status": "completed",
                "summary": [],
            }
        )
        for index in range(4_096)
    )
    candidate = build_prepared_durable_provider_assistant_replay(
        session_id="session:test",
        workspace_id="workspace:test",
        assistant_entry_id="entry:test",
        target=target,
        public_projection_fingerprint="sha256:" + "2" * 64,
        ordered_items=items,
    )
    assert candidate.item_count == 4_096
    with pytest.raises(ValueError, match="payload"):
        build_prepared_durable_provider_assistant_replay(
            session_id="session:test",
            workspace_id="workspace:test",
            assistant_entry_id="entry:test",
            target=target,
            public_projection_fingerprint="sha256:" + "2" * 64,
            ordered_items=(*items, items[0]),
        )
    with pytest.raises(ValueError, match="item type"):
        build_prepared_durable_provider_assistant_replay(
            session_id="session:test",
            workspace_id="workspace:test",
            assistant_entry_id="entry:test",
            target=target,
            public_projection_fingerprint="sha256:" + "2" * 64,
            ordered_items=(_frozen_object({"type": "hosted_tool_call"}),),
        )


@pytest.mark.parametrize("delta", (-1, 0))
def test_round5a2_dispatch_composite_accepts_exact_boundary(delta: int) -> None:
    canonical = 1 << 20
    metadata = 8 << 10
    selected = (
        MAXIMUM_PROVIDER_DISPATCH_COMPOSITE_BYTES
        - canonical
        - metadata
        + delta
    )
    assert quote_provider_dispatch_composite_bytes(
        canonical_compile_bytes=canonical,
        manifest_metadata_bytes=metadata,
        selected_payload_bytes=selected,
    ) == MAXIMUM_PROVIDER_DISPATCH_COMPOSITE_BYTES + delta


def test_round5a2_dispatch_composite_rejects_boundary_plus_one_typed() -> None:
    with pytest.raises(ProviderReplayHydrationError) as captured:
        quote_provider_dispatch_composite_bytes(
            canonical_compile_bytes=MAXIMUM_PROVIDER_DISPATCH_COMPOSITE_BYTES,
            manifest_metadata_bytes=0,
            selected_payload_bytes=1,
        )
    assert (
        captured.value.kind
        is ProviderReplayHydrationFailureKind.RESOURCE_BOUNDARY
    )


def test_round5a2_metadata_read_and_writer_paths_are_sealed() -> None:
    dispatch_source = inspect.getsource(CanonicalProviderInputReader.read_frozen_dispatch)
    hydrate_source = inspect.getsource(
        CanonicalProviderInputReader.hydrate_selected_provider_replays
    )
    assert "payload_bytes" not in dispatch_source
    assert "payload_bytes" in hydrate_source
    assert "manifest_cut.scope.session_id" in hydrate_source

    insert_paths = tuple(
        path
        for path in sorted(PRODUCTION.rglob("*.py"))
        if "INSERT INTO pulsara_v3.provider_assistant_replay_fragments"
        in path.read_text(encoding="utf-8")
    )
    assert insert_paths == (KERNEL / "_repository/conversation.py",)
    repository_source = (KERNEL / "_repository/conversation.py").read_text(
        encoding="utf-8"
    )
    assert "UPDATE pulsara_v3.provider_assistant_replay_fragments" not in repository_source
    assert "UPDATE pulsara_v3.transcript_entries\n" not in repository_source


def test_round5a2_has_no_vendor_or_remote_state_branch_and_oracle_is_exact() -> None:
    paths = (
        PRODUCTION / "llm/provider_replay.py",
        PRODUCTION / "model_input/provider_replay.py",
        KERNEL / "direct_model.py",
        KERNEL / "reader.py",
        KERNEL / "assistant_settlement.py",
    )
    forbidden_text = (
        "previous_response_id",
        "openrouter",
        "deepseek",
        "dashscope",
        "qwen",
        "kimi",
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        assert not any(token in source.lower() for token in forbidden_text), path
        authority_names = {
            node.id.lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
        } | {
            node.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert not any(
            token in name
            for token in ("checkpoint", "receipt", "repair")
            for name in authority_names
        ), path
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 31
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 13
    assert len(APPEND_GUARDS) == 2
    assert len(CONVERSATION_KERNEL_RELATIONS) == 26
    assert len(JOB_HANDLER_CATALOG) == 1
