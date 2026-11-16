"""Focused K3 image wire and v2-estimator contract tests."""

from __future__ import annotations
from tests.support.images import bound_test_image
from pulsara_agent.identity import derive_id

import asyncio
import base64
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from pulsara_agent.conversation_kernel.compaction.contracts import (
    CompactionScope,
    CompactionTrigger,
    RecentHumanMessageProof,
    freeze_compaction_canonical_range,
    provider_input_item_canonical_expanded_bytes,
)
from pulsara_agent.conversation_kernel.compaction.runtime import (
    CompactionWriteReservation,
    HostCompactionRuntimeOwner,
)
from pulsara_agent.conversation_kernel.compaction.coordinator import (
    _can_retry_compaction_with_smaller_tail,
    CompactionSuccessorHardAdmissionError,
    _target_is_known_text_only,
)
from pulsara_agent.conversation_kernel.compaction.planner import (
    CompactionPlanningError,
    CompactionReclaimUnavailable,
    compaction_effective_history_has_image,
    DestinationDialogueEntry,
    DestinationDialogueProjectionPlan,
    RecentDialogueUnit,
    project_destination_dialogue_plan_for_text_only_handover,
)
from pulsara_agent.conversation_kernel.direct_model import (
    freeze_provider_wire_measurement,
)
from pulsara_agent.conversation_kernel.host import KernelHostSession
from pulsara_agent.llm.adapters.openai.chat_completions import (
    chat_semantic_wire_group,
)
from pulsara_agent.llm.adapters.openai.responses import responses_semantic_wire_group
from pulsara_agent.llm.estimator import (
    PulsaraHeuristicTokenEstimatorV3,
    estimate_image_visual_tokens,
)
from pulsara_agent.llm.input import (
    FrozenPromptContent,
    LLMImagePart,
    LLMMessage,
    LLMTextPart,
    llm_content_logical_bytes,
)
from pulsara_agent.llm.errors import ModelTargetCapabilityMismatch
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    ModelInputCompileFailureKind,
    ProviderToolCall,
    StructuredModelInputCompileError,
)
from pulsara_agent.primitives.context import (
    canonical_json_bytes,
    freeze_json,
)


def _image(
    payload: bytes | None = None,
    *,
    media_type: str = "image/png",
    width: int = 512,
    height: int = 512,
) -> LLMImagePart:
    if payload is None:
        output = BytesIO()
        Image.new("RGB", (width, height), (11, 23, 41)).save(output, "PNG")
        payload = output.getvalue()
    return bound_test_image(media_type, payload, width, height)


def _message(*parts: LLMTextPart | LLMImagePart) -> LLMMessage:
    return LLMMessage.user_content(FrozenPromptContent(tuple(parts)))


def test_compaction_recovery_requires_the_exact_admitted_writer_owner() -> None:
    async def scenario() -> None:
        host = object.__new__(KernelHostSession)
        host._session_scope_kind = ModelInputScopeKind.ROOT
        host._writer_renewal_failure = None
        host._lock = asyncio.Lock()
        host._compaction = HostCompactionRuntimeOwner()
        host._compaction_write_reservations = {}
        host._root_compaction_write_changed = asyncio.Event()
        host._root_compaction_write_changed.set()
        host._active_task = None
        host._active_turn_id = None
        host._pending_root_successor = None
        host._external_new_turn_accepting = True
        host._plan_exit_fence = False
        host._closing = False
        host._closed = False
        host._queue_wake = asyncio.Event()
        host._cell_output_wake = asyncio.Event()
        async with host._lock:
            exact = host._reserve_compaction_write_locked(
                scope_kind=ModelInputScopeKind.ROOT,
            )
        foreign = CompactionWriteReservation(ModelInputScopeKind.ROOT)
        scope = CompactionScope(
            session_id="session:5dyf7lfzp7z22pphjtostkgsjm",
            workspace_id="workspace:scvzq3m6ilgcdfi5qvvqwjwvla",
            turn_id="turn:xayicncb3vcols33532v4oov6a",
            scope_kind=ModelInputScopeKind.ROOT,
        )
        owner_task = asyncio.current_task()
        assert owner_task is not None

        with pytest.raises(RuntimeError, match="not the exact owner"):
            await host._install_compaction_fence(
                scope,
                CompactionTrigger.AUTO_ACTIVE_CONTEXT,
                "attempt:mugciis47zjypnfgrl2sal7gpi",
                owner_task,
                foreign,
            )
        with pytest.raises(RuntimeError, match="admitted writer"):
            await host._install_compaction_fence(
                scope,
                CompactionTrigger.AUTO_ACTIVE_CONTEXT,
                "attempt:gnqzrpqgvczqquxswffamywiry",
                owner_task,
                None,
            )

        await host._install_compaction_fence(
            scope,
            CompactionTrigger.AUTO_ACTIVE_CONTEXT,
            "attempt:oh4vvsmff4tb6jdshwwn5zqms4",
            owner_task,
            exact,
        )
        assert host._compaction.is_fenced(
            scope_kind=ModelInputScopeKind.ROOT,
        )
        await host._remove_compaction_fence(
            scope, "attempt:oh4vvsmff4tb6jdshwwn5zqms4", owner_task
        )
        await host._release_compaction_write_reservation(exact)

        with pytest.raises(RuntimeError, match="idle ROOT admission is busy"):
            await host._install_compaction_fence(
                scope,
                CompactionTrigger.AUTO_ACTIVE_CONTEXT,
                "attempt:gnqzrpqgvczqquxswffamywiry",
                owner_task,
                None,
            )
        host._external_new_turn_accepting = False
        host._active_task = owner_task
        host._active_turn_id = "turn:tgnq76so2tupmonbxpxhivfuya"
        with pytest.raises(RuntimeError, match="active ROOT target changed"):
            await host._install_compaction_fence(
                scope,
                CompactionTrigger.AUTO_ACTIVE_CONTEXT,
                "attempt:6rcxsrhn2han2cskeg5wb63sym",
                owner_task,
                None,
            )

        not_owner = asyncio.create_task(asyncio.sleep(0))
        host._active_task = not_owner
        with pytest.raises(RuntimeError, match="ROOT task ownership changed"):
            await host._install_compaction_fence(
                scope,
                CompactionTrigger.AUTO_ACTIVE_CONTEXT,
                "attempt:rrsveftdohjcpbj432s43cknma",
                owner_task,
                None,
            )
        await not_owner

        host._active_task = owner_task
        host._active_turn_id = scope.turn_id
        await host._install_compaction_fence(
            scope,
            CompactionTrigger.AUTO_ACTIVE_CONTEXT,
            "attempt:jjqdmoccleuuxdthym5doct3f4",
            owner_task,
            None,
        )
        await host._remove_compaction_fence(
            scope, "attempt:jjqdmoccleuuxdthym5doct3f4", owner_task
        )

    asyncio.run(scenario())


def test_chat_and_responses_mixed_image_wire_goldens_preserve_occurrences() -> None:
    image = _image()
    message = _message(LLMTextPart("before"), image, LLMTextPart("between"), image)
    encoded = base64.b64encode(image.immutable_bytes).decode("ascii")
    data_url = f"data:image/png;base64,{encoded}"

    assert chat_semantic_wire_group(message) == (
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "before"},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "auto"},
                },
                {"type": "text", "text": "between"},
                {
                    "type": "image_url",
                    "image_url": {"url": data_url, "detail": "auto"},
                },
            ],
        },
    )
    assert responses_semantic_wire_group(message) == (
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "before"},
                {
                    "type": "input_image",
                    "image_url": data_url,
                    "detail": "auto",
                },
                {"type": "input_text", "text": "between"},
                {
                    "type": "input_image",
                    "image_url": data_url,
                    "detail": "auto",
                },
            ],
        },
    )


def test_pure_image_is_one_formal_user_wire_item_and_pure_text_is_unchanged() -> None:
    pure_image = _message(_image())
    chat_image = chat_semantic_wire_group(pure_image)
    responses_image = responses_semantic_wire_group(pure_image)
    assert chat_image[0]["role"] == "user"
    assert [part["type"] for part in chat_image[0]["content"]] == ["image_url"]
    assert responses_image[0]["role"] == "user"
    assert [part["type"] for part in responses_image[0]["content"]] == ["input_image"]

    text = _message(LLMTextPart("one"), LLMTextPart("two"))
    assert chat_semantic_wire_group(text) == ({"role": "user", "content": "one\ntwo"},)
    assert responses_semantic_wire_group(text) == (
        {"role": "user", "content": "one\ntwo"},
    )


@pytest.mark.parametrize(
    ("wire_kind", "counted_content"),
    (
        (
            "chat",
            [
                {"type": "text", "text": "caption"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,",
                        "detail": "auto",
                    },
                },
            ],
        ),
        (
            "responses",
            [
                {"type": "input_text", "text": "caption"},
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64,",
                    "detail": "auto",
                },
            ],
        ),
    ),
)
def test_v2_final_wire_quote_elides_only_formal_payload_and_adds_d1(
    wire_kind: str,
    counted_content: list[dict[str, object]],
) -> None:
    estimator = PulsaraHeuristicTokenEstimatorV3()
    image = _image()
    source = _message(LLMTextPart("caption"), image)
    if wire_kind == "chat":
        item = chat_semantic_wire_group(source)[0]
    else:
        item = responses_semantic_wire_group(source)[0]
    fixed = {"model": "model", "input": []}
    estimate = estimator.estimate_final_wire_json_components(
        fixed_context=fixed,
        ordered_input_items=(item,),
        ordered_input_sources=(source,),
    )
    visual = estimate_image_visual_tokens(width=512, height=512)
    expected_counted = {"role": "user", "content": counted_content}
    assert estimate.visual_image_tokens == visual == 316
    assert estimate.text_and_framing_tokens == (
        3
        + estimator.estimate_json(fixed)
        + 4
        + estimator.estimate_json(expected_counted)
    )
    assert estimate.total_input_tokens == estimate.text_and_framing_tokens + visual
    assert len(canonical_json_bytes(item)) > len(canonical_json_bytes(expected_counted))


def test_v2_estimator_counts_repeated_images_and_does_not_scan_text() -> None:
    estimator = PulsaraHeuristicTokenEstimatorV3()
    image = _image(width=1_024, height=1_024)
    repeated = _message(image, image, image)
    item = responses_semantic_wire_group(repeated)[0]
    estimate = estimator.estimate_final_wire_json_components(
        fixed_context={},
        ordered_input_items=(item,),
        ordered_input_sources=(repeated,),
    )
    assert estimate.visual_image_tokens == 3 * 1_198

    text = _message(LLMTextPart("data:image/png;base64," + "A" * 4_096))
    text_item = responses_semantic_wire_group(text)[0]
    text_estimate = estimator.estimate_final_wire_json_components(
        fixed_context={},
        ordered_input_items=(text_item,),
        ordered_input_sources=(text,),
    )
    assert text_estimate.visual_image_tokens == 0
    assert text_estimate.total_input_tokens == (
        3
        + estimator.estimate_json({})
        + estimator.estimate_wire_json_component(text_item)
    )


def test_v2_final_wire_traversal_rejects_source_or_payload_drift() -> None:
    estimator = PulsaraHeuristicTokenEstimatorV3()
    source = _message(_image(b"abc"))
    item = responses_semantic_wire_group(source)[0]

    with pytest.raises(ValueError, match="base64 differs"):
        estimator.estimate_final_wire_json_components(
            fixed_context={},
            ordered_input_items=(item,),
            ordered_input_sources=(_message(_image(b"abd")),),
        )
    with pytest.raises(ValueError, match="MIME"):
        estimator.estimate_final_wire_json_components(
            fixed_context={},
            ordered_input_items=(item,),
            ordered_input_sources=(_message(_image(b"abc", media_type="image/jpeg")),),
        )
    with pytest.raises(ValueError, match="do not align"):
        estimator.estimate_final_wire_json_components(
            fixed_context={},
            ordered_input_items=(item,),
            ordered_input_sources=(),
        )
    with pytest.raises(ValueError, match="validated semantic source"):
        estimator.estimate_final_wire_json_components(
            fixed_context={},
            ordered_input_items=(item,),
            ordered_input_sources=(None,),
        )
    with pytest.raises(ValueError, match="validated semantic source"):
        estimator.estimate_final_wire_json_components(
            fixed_context={},
            ordered_input_items=(item,),
            ordered_input_sources=(_message(LLMTextPart("not an image")),),
        )


def test_v3_fact_preserves_d1_and_formal_wire_accounting() -> None:
    fact = PulsaraHeuristicTokenEstimatorV3().fact
    assert fact.image_grid_pixels == 28
    assert fact.image_scale_numerator == 7
    assert fact.image_scale_denominator == 8
    assert fact.image_min_tokens == 256


def _recent(entry_id: str, sequence: int, *parts: LLMTextPart | LLMImagePart):
    return RecentHumanMessageProof(
        entry_id=entry_id,
        entry_sequence=sequence,
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        content=FrozenPromptContent(tuple(parts)),
    )


def test_optional_tail_retry_is_limited_to_closed_resource_failures() -> None:
    assert not _can_retry_compaction_with_smaller_tail(
        CompactionReclaimUnavailable("no raw-token improvement")
    )
    assert _can_retry_compaction_with_smaller_tail(
        CompactionSuccessorHardAdmissionError(
            "compaction successor lacks executable final-wire admission"
        )
    )
    assert _can_retry_compaction_with_smaller_tail(
        StructuredModelInputCompileError(
            ModelInputCompileFailureKind.COMPILE_WORKING_SET_EXCEEDED
        )
    )
    assert not _can_retry_compaction_with_smaller_tail(
        StructuredModelInputCompileError(
            ModelInputCompileFailureKind.SOURCE_CONTRACT_INVALID
        )
    )
    assert not _can_retry_compaction_with_smaller_tail(
        CompactionPlanningError("destination modality invariant failed")
    )
    assert not _can_retry_compaction_with_smaller_tail(RuntimeError("drift"))


def test_dry_wire_measurement_closes_text_only_target_before_adoption() -> None:
    target_fact = SimpleNamespace(input_modalities=("text",))
    call = SimpleNamespace(target=SimpleNamespace(fact=target_fact))
    binding = SimpleNamespace(
        target_fact=target_fact,
        binding_fingerprint="binding:2vbfj7ycdmvjuwmj3feeoqytyi",
        tool_surface=SimpleNamespace(tool_specs=()),
    )
    semantic_input = SimpleNamespace(
        compile_binding_fingerprint="binding:2vbfj7ycdmvjuwmj3feeoqytyi",
        tools=(),
        messages=(_message(_image()),),
    )

    with pytest.raises(
        ModelTargetCapabilityMismatch,
        match="does not declare image input",
    ):
        freeze_provider_wire_measurement(
            call=call,
            binding=binding,
            native_projection_set=SimpleNamespace(),
            semantic_input=semantic_input,
            replay_hydration=None,
        )

    mismatch = ModelTargetCapabilityMismatch(
        "model target does not declare image input"
    )
    assert not _can_retry_compaction_with_smaller_tail(mismatch)


def test_large_image_tail_charges_descriptor_and_full_payload_separately() -> None:
    output = BytesIO()
    Image.new("RGB", (1024, 1024), (11, 23, 41)).save(output, "PNG", compress_level=0)
    payload = output.getvalue()
    assert len(payload) > 2 << 20
    image = bound_test_image("image/png", payload, 1024, 1024)
    scope = CompactionScope(
        "session:lx6ipiiqg2ltlxyfpzwtn7fbla",
        "workspace:5dnu2gwcmqvakometislaf54zy",
        "turn:puzavdnlu2fqm3ofrkorfnmlqy",
        ModelInputScopeKind.ROOT,
    )

    def item_with_occurrences(count: int) -> FrozenProviderInputItem:
        return FrozenProviderInputItem(
            FrozenProviderInputItemKind.USER,
            derive_id("entry", "test-fixture", f"entry:i36:{count}"),
            1,
            "turn:puzavdnlu2fqm3ofrkorfnmlqy",
            (image,) * count,
            input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
        )

    admitted = item_with_occurrences(1)
    admitted_tail = freeze_compaction_canonical_range(
        scope=scope,
        effective_materialization_lineage_floor=0,
        source_through_sequence=1,
        ordered_items=(admitted,),
        closures=(),
        late_outcomes=(),
    )
    admitted_c = provider_input_item_canonical_expanded_bytes(admitted)
    admitted_l = llm_content_logical_bytes(admitted.content)
    assert 0 < admitted_tail.canonical_utf8_bytes < admitted_c
    assert 0 < admitted_c < 16 << 20
    assert 0 < admitted_l < 64 << 20

    overbound = item_with_occurrences(4)
    overbound_tail = freeze_compaction_canonical_range(
        scope=scope,
        effective_materialization_lineage_floor=0,
        source_through_sequence=1,
        ordered_items=(overbound,),
        closures=(),
        late_outcomes=(),
    )
    overbound_c = provider_input_item_canonical_expanded_bytes(overbound)
    overbound_l = llm_content_logical_bytes(overbound.content)
    assert (
        admitted_tail.canonical_utf8_bytes
        < overbound_tail.canonical_utf8_bytes
        < overbound_c
    )
    assert overbound_c > 12 << 20
    assert overbound_l > admitted_l


def test_text_only_projection_p_preserves_order_occurrences_and_is_idempotent() -> None:
    source = FrozenPromptContent(
        (
            LLMTextPart("before"),
            _image(b"same"),
            LLMTextPart("between"),
            _image(b"same"),
        )
    )

    original = FrozenProviderInputItem(
        FrozenProviderInputItemKind.USER,
        derive_id("entry", "projection-p"),
        1,
        derive_id("turn", "projection-p"),
        source.parts,
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
    )
    plan = DestinationDialogueProjectionPlan(
        (
            RecentDialogueUnit(
                original.source_turn_id,
                (1,),
                (DestinationDialogueEntry("user", original),),
            ),
        ),
        None,
    )
    projected = project_destination_dialogue_plan_for_text_only_handover(plan)
    assert projected.units[0].entries[0].content == (
        LLMTextPart("before"),
        LLMTextPart(
            "[历史图片引用；此处未包含图像内容]\n"
            f'target={{"kind":"IMAGE_REF","image_ref":"{_image(b"same").image_ref}"}}\n'
            "media_type: image/png; width: 512; height: 512"
        ),
        LLMTextPart("between"),
        LLMTextPart(
            "[历史图片引用；此处未包含图像内容]\n"
            f'target={{"kind":"IMAGE_REF","image_ref":"{_image(b"same").image_ref}"}}\n'
            "media_type: image/png; width: 512; height: 512"
        ),
    )
    assert (
        project_destination_dialogue_plan_for_text_only_handover(projected) == projected
    )
    assert plan.units[0].entries[0].source is original
    assert original.content == source.parts


def test_history_and_recent_image_checks_exclude_only_the_exact_active_request() -> (
    None
):
    old_image = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.USER,
        source_entry_id="entry:hsbhejqlvgm3oe42nqmhybryyu",
        source_entry_sequence=1,
        source_turn_id="turn:uyenyjpuysm57ziiygjzxjhcum",
        content=(_image(b"old"),),
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
    )
    active_image = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.USER,
        source_entry_id="entry:2b6xhmrsvzz7q4sgugd5cuq34a",
        source_entry_sequence=2,
        source_turn_id="turn:kezb35l4prynpikloa5x3helji",
        content=(_image(b"active"),),
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
    )

    def canonical_read(*items: FrozenProviderInputItem) -> SimpleNamespace:
        canonical = SimpleNamespace(
            identity=SimpleNamespace(
                initial_entry_id="entry:2b6xhmrsvzz7q4sgugd5cuq34a"
            ),
            items=items,
        )
        return SimpleNamespace(
            dispatch_read=SimpleNamespace(
                compile_snapshot=SimpleNamespace(canonical_input=canonical)
            )
        )

    assert compaction_effective_history_has_image(canonical_read(active_image)) is False
    assert (
        compaction_effective_history_has_image(canonical_read(old_image, active_image))
        is True
    )


def test_target_projection_image_history_excludes_the_complete_first_call_group() -> (
    None
):
    current_turn = derive_id("turn", "image-projection-test", "current")

    def item(label, sequence, origin, *parts):
        return FrozenProviderInputItem(
            item_kind=FrozenProviderInputItemKind.USER,
            source_entry_id=derive_id("entry", "image-projection-test", label),
            source_entry_sequence=sequence,
            source_turn_id=current_turn,
            content=parts,
            input_origin=origin,
        )

    # Canonical-level image fixtures exercise projection; Work delegation is text-only.
    initial_material = item(
        "initial-material",
        2,
        CanonicalInputOriginKind.AGENT_MATERIAL,
        _image(b"initial-material"),
    )
    instruction = item(
        "instruction",
        3,
        CanonicalInputOriginKind.AGENT_INSTRUCTION,
        LLMTextPart("KEEP_CURRENT_REQUEST"),
    )
    material = item(
        "material", 4, CanonicalInputOriginKind.AGENT_MATERIAL, _image(b"material")
    )
    group = (initial_material, instruction, material)

    def read(items):
        return SimpleNamespace(
            dispatch_read=SimpleNamespace(
                compile_snapshot=SimpleNamespace(
                    canonical_input=SimpleNamespace(
                        identity=SimpleNamespace(
                            initial_entry_id=instruction.source_entry_id
                        ),
                        items=items,
                    )
                )
            )
        )

    # These images remain exact current input; a text-only destination cannot
    # treat them as historical references merely because instruction is text.
    assert compaction_effective_history_has_image(read(group))
    assert not compaction_effective_history_has_image(
        read(group), first_call_history_through_sequence=1
    )
    historical_image = replace(
        material,
        source_entry_id=derive_id("entry", "image-projection-test", "history"),
        source_entry_sequence=1,
        source_turn_id=derive_id("turn", "image-projection-test", "previous"),
    )
    assert compaction_effective_history_has_image(
        read((historical_image, *group)), first_call_history_through_sequence=1
    )


@pytest.mark.parametrize(
    ("modalities", "expected"),
    (
        (None, False),
        (("image",), False),
        (("audio",), False),
        (("text", "image"), False),
        (("text",), True),
    ),
)
def test_text_only_handover_requires_known_text_without_image(
    modalities: tuple[str, ...] | None,
    expected: bool,
) -> None:
    target = SimpleNamespace(
        target=SimpleNamespace(fact=SimpleNamespace(input_modalities=modalities))
    )
    assert _target_is_known_text_only(target) is expected


def test_canonical_assistant_charge_includes_complete_arguments() -> None:
    arguments = freeze_json({"path": "\\\\quoted", "line": 7})
    item = FrozenProviderInputItem(
        item_kind=FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        source_entry_id="entry:5qidalqvkujk5ykzzzfpwie2qy",
        source_entry_sequence=4,
        source_turn_id="turn:kezb35l4prynpikloa5x3helji",
        content=(LLMTextPart("answer"),),
        tool_calls=(ProviderToolCall("call:1", "read_file", arguments),),
    )
    expected_canonical = len(b"answer") + len(
        canonical_json_bytes({"path": "\\\\quoted", "line": 7})
    )

    assert provider_input_item_canonical_expanded_bytes(item) == expected_canonical


def test_typed_tool_result_canonical_charge_counts_visible_text_and_image_bytes() -> (
    None
):
    from tests.support.model_input import _tool_result

    image = _image(b"typed-image-bytes", width=12, height=8)
    content = FrozenPromptContent((LLMTextPart("attached"), image))
    item = replace(
        _tool_result(
            "plain",
            sequence=7,
            turn_id="turn:hei4tpo3ncy7wcxg6s4x3caeae",
        ),
        content=content.parts,
        tool_result_body_text="attached",
        tool_call_ordinal=0,
        tool_call_arguments=freeze_json(
            {"target": {"kind": "PATH", "path": "/tmp/image.png"}}
        ),
        tool_call_name="view_image",
    )

    assert provider_input_item_canonical_expanded_bytes(item) == (
        len(b"attached") + len(image.immutable_bytes)
    )

    late_content = FrozenPromptContent(
        (
            LLMTextPart(
                '{"tool_call_id":"call:7","result_state":"SUCCESS",'
                '"result":"Image loaded."}'
            ),
            image,
        )
    )
    late = replace(
        item,
        item_kind=FrozenProviderInputItemKind.LATE_TOOL_OUTCOME,
        content=late_content.parts,
    )
    assert provider_input_item_canonical_expanded_bytes(late) == (
        len(late_content.parts[0].text.encode()) + len(image.immutable_bytes)
    )
