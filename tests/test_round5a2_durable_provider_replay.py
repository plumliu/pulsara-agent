"""Round 5A.2 durable provider replay contracts and architecture gates."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    provider_replay_id,
    project_provider_visible_reasoning,
)
from pulsara_agent.llm.input import LLMMessage, LLMTextPart, MessageRole
from pulsara_agent.llm.request import (
    provider_assistant_message_public_projection_fingerprint,
)
from pulsara_agent.model_input.contracts import (
    FrozenCompiledMessagePlacement,
    ModelInputScopeKind,
)
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.ports.live_agent_event import ReasoningPresentationKind
from pulsara_agent.model_input.provider_replay import (
    ProviderReplayHydrationError,
    ProviderReplayHydrationFailureKind,
    decode_provider_replay_fragment,
    freeze_provider_replay_manifest,
    freeze_provider_replay_manifest_cut,
    freeze_selected_provider_replay_hydration,
    quote_provider_dispatch_composite_bytes,
    select_compatible_provider_replay_manifests,
)
from pulsara_agent.ports.provider_stream import (
    freeze_provider_adapter_completed_replay_payload,
)
from pulsara_agent.primitives.context import (
    FrozenJsonObjectFact,
    freeze_json,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from tests.support.model_config import build_test_provider_replay_target


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = ROOT / "src/pulsara_agent"
KERNEL = PRODUCTION / "conversation_kernel"
_API_KEY_REDACTION = "<redacted:PULSARA_API_KEY>"


def _scrub_exact_api_key(value: object, *, api_key: str) -> object:
    if not api_key:
        return value
    if isinstance(value, str):
        return value.replace(api_key, _API_KEY_REDACTION)
    if isinstance(value, list):
        return [_scrub_exact_api_key(item, api_key=api_key) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_exact_api_key(item, api_key=api_key) for item in value)
    if isinstance(value, dict):
        return {
            (
                key.replace(api_key, _API_KEY_REDACTION)
                if isinstance(key, str)
                else key
            ): _scrub_exact_api_key(item, api_key=api_key)
            for key, item in value.items()
        }
    return value


def _target(
    *,
    api: str = "openai_chat_completions",
    endpoint: str = "1",
    model: str = "model-a",
    binding: str | None = None,
):
    return build_test_provider_replay_target(
        wire_api=api,
        endpoint=endpoint,
        model_id=model,
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


def _visualization_metadata_replay_input():
    session_id = "session:visualization-replay"
    assistant_entry_id = "entry:visualization-owner"
    revision_id = "context-revision:visualization-replay"
    target = _target()
    assistant = LLMMessage(
        role=MessageRole.ASSISTANT,
        content=(LLMTextPart("public"),),
    )
    metadata = LLMMessage(
        role=MessageRole.USER,
        content=(
            LLMTextPart(
                '{"pulsara_visualizations":[{"visualization_ref":"sha256:'
                + "a" * 64
                + '"}]}'
            ),
        ),
    )
    frozen_item = _frozen_object(
        {"role": "assistant", "content": "public", "reasoning_content": "thought"}
    )
    candidate = build_prepared_durable_provider_assistant_replay(
        session_id=session_id,
        workspace_id="workspace:visualization-replay",
        assistant_entry_id=assistant_entry_id,
        target=target,
        public_projection_fingerprint=(
            provider_assistant_message_public_projection_fingerprint(assistant)
        ),
        ordered_items=(frozen_item,),
    )
    manifest = freeze_provider_replay_manifest(
        replay_id=candidate.replay_id,
        assistant_entry_id=assistant_entry_id,
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
    cut = freeze_provider_replay_manifest_cut(
        session_id=session_id,
        scope=ProviderInputContinuityScope(
            session_id=session_id,
            scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ),
        context_binding_revision_id=revision_id,
        provider_input_through_sequence=2,
        manifests=(manifest,),
    )
    compiled_input = SimpleNamespace(
        canonical_input_identity=SimpleNamespace(
            session_id=session_id,
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
            context_binding_revision_id=revision_id,
            provider_input_through_sequence=2,
        ),
        messages=(assistant, metadata),
        message_placements=(
            FrozenCompiledMessagePlacement(
                message_ordinal=0,
                origin_entry_id=assistant_entry_id,
                origin_item_fingerprint="sha256:" + "a" * 64,
                within_origin_ordinal=0,
                role=MessageRole.ASSISTANT,
            ),
            FrozenCompiledMessagePlacement(
                message_ordinal=1,
                origin_entry_id=assistant_entry_id,
                origin_item_fingerprint="sha256:" + "b" * 64,
                within_origin_ordinal=1,
                role=MessageRole.USER,
            ),
        ),
    )
    return target, cut, compiled_input, candidate.fragment()


def test_visualization_metadata_is_not_part_of_native_assistant_replay() -> None:
    target, cut, compiled_input, fragment = _visualization_metadata_replay_input()

    selected, placements = select_compatible_provider_replay_manifests(
        manifest_cut=cut,
        compiled_input=compiled_input,
        replay_target=target,
    )

    assert selected == cut.manifests
    assert placements == compiled_input.message_placements[:1]
    hydration = freeze_selected_provider_replay_hydration(
        manifest_cut=cut,
        compiled_input=compiled_input,
        replay_target=target,
        selected_manifests=selected,
        selected_placements=placements,
        fragments=(fragment,),
    )
    assert hydration.selected_manifests == selected


def test_incompatible_replay_is_skipped_before_assistant_shape_validation() -> None:
    _, cut, compiled_input, _ = _visualization_metadata_replay_input()
    assistant = compiled_input.messages[0]
    compiled_input.messages = (*compiled_input.messages, assistant)
    compiled_input.message_placements = (
        *compiled_input.message_placements,
        FrozenCompiledMessagePlacement(
            message_ordinal=2,
            origin_entry_id=cut.manifests[0].assistant_entry_id,
            origin_item_fingerprint="sha256:" + "c" * 64,
            within_origin_ordinal=2,
            role=MessageRole.ASSISTANT,
        ),
    )

    assert select_compatible_provider_replay_manifests(
        manifest_cut=cut,
        compiled_input=compiled_input,
        replay_target=_target(endpoint="different"),
    ) == ((), ())
    with pytest.raises(ValueError, match="assistant placement group is invalid"):
        select_compatible_provider_replay_manifests(
            manifest_cut=cut,
            compiled_input=compiled_input,
            replay_target=_target(),
        )


def test_round5a2_dogfood_report_scrubs_only_the_exact_configured_key() -> None:
    api_key = "dogfood-exact-secret"
    report = {
        "public_answer": "diagnostic body remains visible",
        "failure": {
            "message": f"provider echoed {api_key} inside a failure",
            "unrelated": "Authorization diagnostics remain observable",
        },
        f"provider-{api_key}": [api_key, "another-secret-is-not-the-api-key"],
    }

    scrubbed = _scrub_exact_api_key(report, api_key=api_key)
    rendered = json.dumps(scrubbed, sort_keys=True)

    assert api_key not in rendered
    assert rendered.count("<redacted:PULSARA_API_KEY>") == 3
    assert "diagnostic body remains visible" in rendered
    assert "Authorization diagnostics remain observable" in rendered
    assert "another-secret-is-not-the-api-key" in rendered
    assert api_key in json.dumps(report, sort_keys=True)


def test_round5a2_replay_target_is_closed_and_process_stable() -> None:
    baseline = _target()
    assert baseline == _target()
    assert (
        baseline.replay_target_fingerprint
        != _target(endpoint="3").replay_target_fingerprint
    )
    assert (
        baseline.replay_target_fingerprint
        != _target(model="model-b").replay_target_fingerprint
    )
    assert (
        baseline.replay_target_fingerprint
        != _target(binding="other-chat-binding").replay_target_fingerprint
    )
    responses = _target(api="openai_responses")
    assert baseline.replay_target_fingerprint != responses.replay_target_fingerprint
    assert (
        baseline.codec_kind
        is ProviderAssistantReplayCodecKind.CHAT_CLOSED_REASONING_FIELDS
    )
    assert (
        responses.codec_kind
        is ProviderAssistantReplayCodecKind.RESPONSES_EXACT_OUTPUT_ITEMS
    )


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


def test_provider_visible_chat_reasoning_is_derived_from_exact_replay_text() -> None:
    candidate = _candidate(
        _frozen_object(
            {
                "role": "assistant",
                "content": "public",
                "reasoning_content": "full provider reasoning",
                "reasoning_details": [{"encrypted": "opaque"}],
            }
        )
    )

    projected = project_provider_visible_reasoning(
        codec_kind=candidate.codec_kind,
        payload_bytes=candidate.payload_bytes,
        expected_payload_digest=candidate.payload_digest,
        expected_payload_size=candidate.payload_size,
        expected_item_count=candidate.item_count,
    )

    assert [(item.presentation_kind, item.text) for item in projected] == [
        (ReasoningPresentationKind.FULL, "full provider reasoning")
    ]


def test_provider_visible_chat_top_level_reasoning_alias_is_not_duplicated() -> None:
    candidate = _candidate(
        _frozen_object(
            {
                "role": "assistant",
                "content": "answer",
                "reasoning_content": "same reasoning",
                "reasoning": "same reasoning",
            }
        )
    )
    projected = project_provider_visible_reasoning(
        codec_kind=candidate.codec_kind,
        payload_bytes=candidate.payload_bytes,
        expected_payload_digest=candidate.payload_digest,
        expected_payload_size=candidate.payload_size,
        expected_item_count=candidate.item_count,
    )
    assert [(item.presentation_kind, item.text) for item in projected] == [
        (ReasoningPresentationKind.FULL, "same reasoning")
    ]


@pytest.mark.parametrize(
    "mirror", [None, "summary one", "summary onepublic thinking", "different text"]
)
def test_provider_visible_chat_details_preserve_fragments_kinds_and_exact_mirror_dedup(
    mirror,
) -> None:
    message = {
        "role": "assistant",
        "content": "answer",
        "reasoning_details": [
            {
                "type": "reasoning.summary",
                "summary": "summary ",
                "index": 0,
                "id": "rs:one",
            },
            {"type": "reasoning.summary", "summary": "one", "index": 0},
            {"type": "reasoning.text", "text": "public thinking", "index": 1},
            {"type": "reasoning.encrypted", "data": "opaque", "text": "not public"},
            {"type": "unknown", "summary": "not public"},
            {"type": "reasoning.text", "text": None},
        ],
    }
    if mirror is not None:
        message["reasoning"] = mirror
    candidate = _candidate(_frozen_object(message))
    projected = project_provider_visible_reasoning(
        codec_kind=candidate.codec_kind,
        payload_bytes=candidate.payload_bytes,
        expected_payload_digest=candidate.payload_digest,
        expected_payload_size=candidate.payload_size,
        expected_item_count=candidate.item_count,
    )
    expected = [
        (ReasoningPresentationKind.SUMMARY, "summary one"),
        (ReasoningPresentationKind.FULL, "public thinking"),
    ]
    if mirror == "different text":
        expected.append((ReasoningPresentationKind.FULL, mirror))
    assert [(block.presentation_kind, block.text) for block in projected] == expected
    assert json.loads(candidate.payload_bytes) == [message]


def test_provider_visible_responses_reasoning_preserves_each_summary_and_content_part() -> (
    None
):
    candidate = _candidate(
        _frozen_object(
            {
                "type": "reasoning",
                "id": "reasoning:1",
                "status": "completed",
                "summary": [
                    {"type": "summary_text", "text": "first summary"},
                    {"type": "summary_text", "text": "second summary"},
                ],
                "content": [
                    {"type": "reasoning_text", "text": "first reasoning"},
                    {"type": "reasoning_text", "text": "second reasoning"},
                ],
                "encrypted_content": "opaque-carrier-is-not-product-text",
            }
        ),
        api="openai_responses",
    )

    projected = project_provider_visible_reasoning(
        codec_kind=candidate.codec_kind,
        payload_bytes=candidate.payload_bytes,
        expected_payload_digest=candidate.payload_digest,
        expected_payload_size=candidate.payload_size,
        expected_item_count=candidate.item_count,
    )

    assert [(item.presentation_kind, item.text) for item in projected] == [
        (ReasoningPresentationKind.SUMMARY, "first summary"),
        (ReasoningPresentationKind.SUMMARY, "second summary"),
        (ReasoningPresentationKind.FULL, "first reasoning"),
        (ReasoningPresentationKind.FULL, "second reasoning"),
    ]
    assert "opaque-carrier" not in repr(projected)


def test_provider_visible_reasoning_rejects_replay_integrity_drift() -> None:
    candidate = _candidate(
        _frozen_object(
            {
                "role": "assistant",
                "content": "public",
                "reasoning": "visible",
            }
        )
    )

    with pytest.raises(ValueError, match="integrity"):
        project_provider_visible_reasoning(
            codec_kind=candidate.codec_kind,
            payload_bytes=candidate.payload_bytes,
            expected_payload_digest="sha256:" + "0" * 64,
            expected_payload_size=candidate.payload_size,
            expected_item_count=candidate.item_count,
        )


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
    selected = MAXIMUM_PROVIDER_DISPATCH_COMPOSITE_BYTES - canonical - metadata + delta
    assert (
        quote_provider_dispatch_composite_bytes(
            canonical_compile_bytes=canonical,
            manifest_metadata_bytes=metadata,
            selected_payload_bytes=selected,
        )
        == MAXIMUM_PROVIDER_DISPATCH_COMPOSITE_BYTES + delta
    )


def test_round5a2_dispatch_composite_rejects_boundary_plus_one_typed() -> None:
    with pytest.raises(ProviderReplayHydrationError) as captured:
        quote_provider_dispatch_composite_bytes(
            canonical_compile_bytes=MAXIMUM_PROVIDER_DISPATCH_COMPOSITE_BYTES,
            manifest_metadata_bytes=0,
            selected_payload_bytes=1,
        )
    assert captured.value.kind is ProviderReplayHydrationFailureKind.RESOURCE_BOUNDARY


def test_round5a2_metadata_read_and_writer_paths_are_sealed() -> None:
    dispatch_source = inspect.getsource(
        CanonicalProviderInputReader.read_frozen_dispatch
    )
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
    assert (
        "UPDATE pulsara_v3.provider_assistant_replay_fragments" not in repository_source
    )
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
            node.id.lower() for node in ast.walk(tree) if isinstance(node, ast.Name)
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
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 30
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 11
    assert len(APPEND_GUARDS) == 1
    assert len(CONVERSATION_KERNEL_RELATIONS) == 29
