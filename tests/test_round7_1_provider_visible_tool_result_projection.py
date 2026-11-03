"""Round 7.1 logical ToolResult projection and FULL-delivery gates."""

from __future__ import annotations
from pulsara_agent.identity import derive_id

import ast
from dataclasses import replace
import json
from pathlib import Path

import pytest

from pulsara_agent.llm.input import LLMTextPart

from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.llm.adapters.openai.chat_completions import (
    chat_semantic_wire_group,
)
from pulsara_agent.llm.adapters.openai.responses import responses_semantic_wire_group
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from pulsara_agent.model_input.contracts import (
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputCompileFailureKind,
    ProviderToolCall,
    StructuredModelInputCompileError,
    ToolResultProviderRenderMode,
)
from pulsara_agent.model_input.lowering import (
    LoweredCanonicalItem,
    lower_canonical_item,
)
from pulsara_agent.primitives.context import canonical_json_bytes, freeze_json
from pulsara_agent.primitives.tool_observation import (
    MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES,
    ToolObservationOrigin,
)
from pulsara_agent.primitives.tool_result_projection import (
    FrozenToolResultDeliveryRequirement,
    MAXIMUM_TOOL_RESULT_MEMORY_PROVENANCE_UTF8_BYTES,
    ToolResultDeliveryRequirement,
    ToolResultFullDeliveryReason,
    ToolResultLogicalMessageKind,
    classify_tool_result_delivery,
    decode_provider_tool_result_observation,
    full_required_tool_result_delivery,
    project_tool_result_storage_body,
    render_provider_tool_result_logical_message,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from tests.test_round3_structured_model_input_compiler import (
    _compile_and_install_append,
    _prepared_request,
    _snapshot,
    _sources,
    _tool_result,
    _user,
)
from tests.support.round3 import new_test_provider_input_continuity_owner


ROOT = Path(__file__).resolve().parents[1]


def test_plan_question_cancellation_projects_user_visible_reason() -> None:
    carrier = {
        "status": "cancelled", "plan_control": "QUESTION_CANCELLED",
        "cancelled_by": "USER_CANCELLED", "message": "用户取消了问题",
        "interaction_id": "storage-only-interaction",
    }
    projected = json.loads(project_tool_result_storage_body(
        json.dumps(carrier), ToolObservationOrigin.PLAN_CONTROL,
    ))
    assert projected == {
        "status": "cancelled", "plan_control": "QUESTION_CANCELLED",
        "cancelled_by": "USER_CANCELLED", "message": "用户取消了问题",
    }
    for field in ("status", "cancelled_by", "message"):
        invalid = dict(carrier)
        invalid.pop(field)
        with pytest.raises(ValueError, match="cancellation carrier is invalid"):
            project_tool_result_storage_body(
                json.dumps(invalid), ToolObservationOrigin.PLAN_CONTROL,
            )


def _body_with_exact_logical_bytes(
    target: int,
    *,
    seed: str,
) -> tuple[object, str]:
    template = _tool_result("", sequence=2, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu')
    metadata = template.tool_result_context
    assert metadata is not None and template.tool_call_id is not None

    def quote(body: str) -> int:
        return render_provider_tool_result_logical_message(
            message_kind=ToolResultLogicalMessageKind.TOOL_RESULT,
            tool_call_id=template.tool_call_id,
            body=body,
            result_state=metadata.result_state,
            timing=metadata.timing,
            model_visible_memory_ids=(),
        ).logical_utf8_bytes

    base = quote("")
    unit = quote(seed) - base
    if unit < 1 or target < base:
        raise AssertionError("test target cannot be materialized")
    repeats, remainder = divmod(target - base, unit)
    body = (seed * repeats) + ("a" * remainder)
    assert quote(body) == target
    return (
        replace(
            template,
            content=(LLMTextPart(body),),
            tool_result_body_text=body,
        ),
        body,
    )


@pytest.mark.parametrize("target", (39_999, 40_000, 40_001))
@pytest.mark.parametrize("seed", ("a", "中", "🙂", '"', "\\"))
def test_round7_1_full_variant_uses_exact_logical_utf8_quote(
    target: int,
    seed: str,
) -> None:
    item, _body = _body_with_exact_logical_bytes(target, seed=seed)
    if target > MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES:
        with pytest.raises(StructuredModelInputCompileError) as failure:
            lower_canonical_item(item)
        assert failure.value.kind is ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE
        return
    lowered = lower_canonical_item(item)
    full = tuple(
        variant
        for variant in lowered.tool_result_variants
        if variant.mode is ToolResultProviderRenderMode.FULL
    )
    assert bool(full) is (target <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES)
    if full:
        assert full[0].utf8_bytes == target


def test_round7_1_oversized_frozen_projection_has_a_typed_failure() -> None:
    item, _body = _body_with_exact_logical_bytes(40_001, seed="🙂")
    request = _prepared_request(_snapshot(_user("question"), item), _sources())
    with pytest.raises(StructuredModelInputCompileError) as failure:
        StructuredModelInputCompiler().compile(request)
    assert failure.value.kind is ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE


def test_round7_1_complete_raw_body_must_fit_its_whole_message() -> None:
    item = _tool_result("a" * 40_000, sequence=2, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu')
    assert item.tool_result_context.output_omitted is False
    with pytest.raises(StructuredModelInputCompileError) as failure:
        lower_canonical_item(item)
    assert failure.value.kind is ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE


def test_round7_1_frozen_projection_has_exactly_one_full_representation() -> None:
    item = _tool_result("small frozen body", sequence=2, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu')
    lowered = lower_canonical_item(item)
    assert len(lowered.tool_result_variants) == 1
    assert lowered.tool_result_variants[0].mode is ToolResultProviderRenderMode.FULL
    with pytest.raises(ValueError, match="one FULL"):
        LoweredCanonicalItem(item, None, lowered.tool_result_variants * 2)


def test_round7_1_tool_result_carriers_and_delivery_requirement_are_closed() -> None:
    user = _user("question")
    with pytest.raises(ValueError, match="carrier union"):
        LoweredCanonicalItem(user, None)

    result = _tool_result("body", sequence=2, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu')
    with pytest.raises(ValueError, match="carrier union"):
        LoweredCanonicalItem(result, None)

    with pytest.raises(TypeError, match="requirement is invalid"):
        FrozenToolResultDeliveryRequirement(  # type: ignore[arg-type]
            "FULL_REQUIRED",
            ToolResultFullDeliveryReason.RESULT_PROJECTION,
        )
    with pytest.raises(TypeError, match="reason is invalid"):
        FrozenToolResultDeliveryRequirement(  # type: ignore[arg-type]
            ToolResultDeliveryRequirement.FULL_REQUIRED,
            "RESULT_PROJECTION",
        )


def test_round7_1_fifty_memory_ids_share_the_canonical_eight_kib_bound() -> None:
    memory_ids = tuple(derive_id("memory", "test-fixture", f"memory:{index:064x}") for index in range(50))
    assert len(canonical_json_bytes(memory_ids)) <= (
        MAXIMUM_TOOL_RESULT_MEMORY_PROVENANCE_UTF8_BYTES
    )
    item = _tool_result("memory search result", sequence=2, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu')
    metadata = item.tool_result_context
    assert metadata is not None
    item = replace(
        item,
        tool_result_context=replace(
            metadata,
            model_visible_memory_fact_ids=memory_ids,
        ),
    )
    lowered = lower_canonical_item(
        item,
    )
    assert lowered.tool_result_variants
    assert lowered.tool_result_variants[0].mode is ToolResultProviderRenderMode.FULL

    oversized = tuple(value + ("x" * 128) for value in memory_ids)
    assert len(canonical_json_bytes(oversized)) > (
        MAXIMUM_TOOL_RESULT_MEMORY_PROVENANCE_UTF8_BYTES
    )
    assert item.tool_call_id is not None
    with pytest.raises(ValueError, match="memory provenance header"):
        render_provider_tool_result_logical_message(
            message_kind=ToolResultLogicalMessageKind.TOOL_RESULT,
            tool_call_id=item.tool_call_id,
            body="memory search result",
            result_state=metadata.result_state,
            timing=metadata.timing,
            model_visible_memory_ids=oversized,
        )


def test_round7_1_compatible_append_keeps_installed_prefix_and_actual_mode() -> None:
    compiler = StructuredModelInputCompiler()
    owner = new_test_provider_input_continuity_owner()
    user = _user("question")
    first = _prepared_request(_snapshot(user), _sources())
    _first, installed = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=first,
    )
    call = ProviderToolCall("call:2", "test_tool", freeze_json({}))
    request_item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        'entry:yym6qf3l4xz6437gradtmhjrke',
        2,
        'turn:klmmazz67vfgsfsvg6ceqgdsfu',
        (LLMTextPart(""),),
        tool_calls=(call,),
    )
    result_item, _body = _body_with_exact_logical_bytes(20_000, seed="中")
    result_item = replace(
        result_item,
        source_entry_id='entry:nvhyc6ehb7acx2htbxgk6cev4q',
        source_entry_sequence=3,
        tool_call_id=call.tool_call_id,
    )
    second = _prepared_request(
        _snapshot(user, request_item, result_item),
        _sources(),
    )
    appended, after = _compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=second,
    )
    decision = appended.compiled_input.tool_result_decisions[0]
    assert decision.first_legal_mode is ToolResultProviderRenderMode.FULL
    assert decision.selected_mode is ToolResultProviderRenderMode.FULL
    assert decision.reason_code == "SELECTED_FULL"
    assert after.messages[: len(installed.messages)] == installed.messages


def test_round7_1_parallel_siblings_all_keep_their_frozen_projections() -> None:
    calls = tuple(ProviderToolCall(f"call:{index}", "test_tool", freeze_json({})) for index in range(1,4))
    request_item = FrozenProviderInputItem(FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        'entry:k5zfbzct6mhkogz33gyubxx3pa', 1, 'turn:klmmazz67vfgsfsvg6ceqgdsfu',
        (LLMTextPart(""),), tool_calls=calls)
    results = tuple(replace(_tool_result("frozen-" + "x" * 12000,
        sequence=index+1, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu'),
        tool_call_id=calls[index-1].tool_call_id) for index in range(1,4))
    request = _prepared_request(_snapshot(request_item, *results), _sources(), budget=1)
    compiler = StructuredModelInputCompiler()
    initial = compiler.compile(request)
    floors = compiler.next_wire_render_floors(request=request, compiled=initial)
    assert floors is not None and floors[1] == ()
    constrained = compiler.compile(request, source_render_floors=floors[0],
        tool_render_floors=floors[1], wire_selection=initial)
    assert all(decision.delivery_requirement is ToolResultDeliveryRequirement.FULL_REQUIRED
        and decision.selected_mode is ToolResultProviderRenderMode.FULL
        for decision in constrained.tool_result_decisions)
    assert tuple(message for message in constrained.messages if message.tool_call_id is not None) == tuple(
        message for message in initial.messages if message.tool_call_id is not None)


@pytest.mark.parametrize(
    "reason",
    [
        ToolResultFullDeliveryReason.RESULT_PROJECTION,
        ToolResultFullDeliveryReason.IMAGE_ATTACHMENT,
    ],
)
def test_round7_1_full_required_has_closed_not_inlineable_and_budget_failures(
    reason,
) -> None:
    oversized, _body = _body_with_exact_logical_bytes(40_001, seed="中")
    oversized = replace(
        oversized,
        tool_result_delivery=full_required_tool_result_delivery(reason),
    )
    with pytest.raises(StructuredModelInputCompileError) as missing:
        StructuredModelInputCompiler().compile(
            _prepared_request(
                _snapshot(_user("question"), oversized),
                _sources(),
            )
        )
    assert missing.value.kind is (
        ModelInputCompileFailureKind.FULL_REQUIRED_TOOL_RESULT_NOT_INLINEABLE
    )

    inlineable, _body = _body_with_exact_logical_bytes(20_000, seed="a")
    inlineable = replace(
        inlineable,
        tool_result_delivery=full_required_tool_result_delivery(reason),
    )
    request = _prepared_request(_snapshot(_user("question"), inlineable), _sources(), budget=100)
    candidate = StructuredModelInputCompiler().compile(request)
    assert candidate.tool_result_decisions[0].selected_mode is ToolResultProviderRenderMode.FULL
    decision = candidate.tool_result_decisions[0]
    assert decision.delivery_requirement is ToolResultDeliveryRequirement.FULL_REQUIRED
    assert decision.selected_mode is ToolResultProviderRenderMode.FULL
    # Final-wire rejection is covered by the real dispatch/runner integration;
    # the semantic compiler must preserve this required body until that boundary.


def test_round7_1_all_frozen_projections_require_full_delivery() -> None:
    for tool_name in ("read_file", "query_capabilities", "third_party_tool", "manage_capability"):
        for state in ("SUCCESS", "SYSTEM_ERROR", "CANCELLED"):
            assert classify_tool_result_delivery(tool_name=tool_name, result_state=state) == (
                full_required_tool_result_delivery(ToolResultFullDeliveryReason.RESULT_PROJECTION))
    assert classify_tool_result_delivery(tool_name="view_image", result_state="SUCCESS",
        has_image_attachment=True) == full_required_tool_result_delivery(ToolResultFullDeliveryReason.IMAGE_ATTACHMENT)


def test_round7_1_logical_quote_is_not_chat_or_responses_wire_bytes() -> None:
    item, _body = _body_with_exact_logical_bytes(39_999, seed='"\\🙂')
    metadata = item.tool_result_context
    assert metadata is not None and item.tool_call_id is not None
    rendered = render_provider_tool_result_logical_message(
        message_kind=ToolResultLogicalMessageKind.TOOL_RESULT,
        tool_call_id=item.tool_call_id,
        body=item.tool_result_body_text,
        result_state=metadata.result_state,
        timing=metadata.timing,
        model_visible_memory_ids=(),
    )
    chat = canonical_json_bytes(chat_semantic_wire_group(rendered.message))
    responses = canonical_json_bytes(responses_semantic_wire_group(rendered.message))
    assert rendered.logical_utf8_bytes == 39_999
    assert len(chat) > rendered.logical_utf8_bytes
    assert len(responses) > rendered.logical_utf8_bytes
    assert chat != responses


def test_tool_result_wire_has_no_citation_handle_or_legacy_null_slot() -> None:
    item, _body = _body_with_exact_logical_bytes(512, seed="memory")
    metadata = item.tool_result_context
    assert metadata is not None and item.tool_call_id is not None
    rendered = render_provider_tool_result_logical_message(
        message_kind=ToolResultLogicalMessageKind.TOOL_RESULT,
        tool_call_id=item.tool_call_id,
        body=item.tool_result_body_text,
        result_state=metadata.result_state,
        timing=metadata.timing,
        model_visible_memory_ids=('memory:zop43vgzo2odv6e7hqu23cb2aa',),
    )
    payload = json.loads(rendered.content)
    inner = payload["pulsara_tool_result"]
    assert "citation_handle" not in inner
    assert inner["model_visible_memory_ids"] == ['memory:zop43vgzo2odv6e7hqu23cb2aa']
    inner["citation_handle"] = None
    with pytest.raises(ValueError, match="member contract"):
        decode_provider_tool_result_observation(json.dumps(payload))


def test_round7_1_architecture_and_oracle_guards() -> None:
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 25
    assert len(LIVE_EVENT_TYPES) == 20
    assert len(SUBJECT_SLOTS) == 8
    assert len(APPEND_GUARDS) == 1
    assert len(CONVERSATION_KERNEL_RELATIONS) == 30

    production = ROOT / "src/pulsara_agent"
    definitions: list[tuple[Path, int]] = []
    for path in production.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign | ast.AnnAssign):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else (node.target,)
                )
                if any(
                    isinstance(target, ast.Name)
                    and target.id == "MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES"
                    for target in targets
                ):
                    definitions.append((path, node.lineno))
    assert tuple(path for path, _line in definitions) == (
        production / "primitives/tool_observation.py",
    )

    pure_path = production / "primitives/tool_result_projection.py"
    pure = pure_path.read_text(encoding="utf-8")
    pure_tree = ast.parse(pure, filename=str(pure_path))
    imported_modules = {
        node.module
        for node in ast.walk(pure_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    } | {
        alias.name
        for node in ast.walk(pure_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    for forbidden in (
        "conversation_kernel",
        "model_input.compiler",
        "httpx",
        "repository",
        "Postgres",
    ):
        assert all(forbidden not in module for module in imported_modules)

    migration = (
        production / "storage/migrations/sql/0000_conversation_kernel_baseline.sql"
    ).read_text(encoding="utf-8")
    assert "tool_result_delivery" not in migration


@pytest.mark.parametrize("effect_class, expected_outcome", [
    ("read_only", "NO_RELIABLE_OBSERVATION"), ("filesystem_write", "UNKNOWN"),
])
def test_explicit_failure_facts_survive_body_publication_failure(effect_class, expected_outcome) -> None:
    from time import monotonic
    from pulsara_agent.conversation_kernel.tool_execution import _tool_invocation_failure
    from pulsara_agent.conversation_kernel.tool_outputs import KnownOutputPublicationFailure
    from pulsara_agent.ports.tool_execution import ToolOutputSourceFormatHint
    from tests.support.tool_outputs import RecordingOutputPublisher, output_processor

    result = _tool_invocation_failure(RuntimeError("connection closed " * 9000), effect_class)
    facts = dict(result.required_facts)
    assert facts["physical_outcome"] == expected_outcome
    assert facts["retry_performed"] is False
    original = json.loads(result.content)
    assert facts["guidance"] == original["guidance"]
    preview = canonical_json_bytes({**facts, "message_preview": "connection closed"}).decode()
    processor = output_processor(RecordingOutputPublisher(failure=KnownOutputPublicationFailure("unavailable")))
    body = processor.materialize_body(session_id='session:336wrfwkamrr3up6kjtsgvgcte',
        workspace_id='workspace:336wrfwkamrr3up6kjtsgvgcte',
        result_entry_id='entry:2b6xhmrsvzz7q4sgugd5cuq34a', public_output=result.content.decode(),
        output_body_candidate=None, public_format=ToolOutputSourceFormatHint.JSON,
        deadline_monotonic=monotonic()+5)
    projection = processor.prepare(body=body, native_output=preview, output_omitted=True)
    assert projection.output_body is None and projection.output_unavailability_reason is not None
    assert projection.canonical_preview.canonical_bytes.decode() == preview
    item = _tool_result(preview, sequence=2, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu')
    item = replace(item, tool_result_context=replace(item.tool_result_context,
        result_state="SYSTEM_ERROR", output_omitted=True, output_available=False,
        output_unavailability_reason=projection.output_unavailability_reason))
    lowered = lower_canonical_item(item)
    assert len(lowered.tool_result_variants) == 1
    actual = decode_provider_tool_result_observation(lowered.tool_result_variants[0].message.content[0].text)["body"]
    assert actual == preview
    assert json.loads(actual)["physical_outcome"] == expected_outcome


@pytest.mark.parametrize("state", ["SUCCESS", "APPLICATION_ERROR", "SYSTEM_ERROR", "CANCELLED",
    "INVALID_ARGUMENTS", "PERMISSION_DENIED", "TOOL_UNAVAILABLE", "CANCELLED_BEFORE_DISPATCH"])
def test_shared_page_budget_covers_near_boundary_error_states(state):
    from pulsara_agent.primitives.tool_result_budget import ToolResultPageBudget
    from pulsara_agent.primitives.tool_result_projection import _maximum_tool_result_page_timing
    call_id = derive_id("call", "near-boundary-result-state")
    budget = ToolResultPageBudget(call_id)
    low, high = 0, MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
    while low < high:
        middle = (low + high + 1) // 2
        if budget.fits("x" * middle):
            low = middle
        else:
            high = middle - 1
    body = "x" * low
    assert budget.fits(body) and not budget.fits(body + "x")
    for kind in ToolResultLogicalMessageKind:
        rendered = render_provider_tool_result_logical_message(message_kind=kind,
            tool_call_id=call_id, body=body, result_state=state,
            timing=_maximum_tool_result_page_timing(), model_visible_memory_ids=())
        assert rendered.logical_utf8_bytes <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES
