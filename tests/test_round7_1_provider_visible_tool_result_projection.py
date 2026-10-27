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
    StructuredModelInputLimits,
    ToolResultProviderRenderMode,
    compiled_tool_result_source_fingerprint,
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
    BEST_AVAILABLE_TOOL_RESULT_DELIVERY,
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
        "cancelled_by": "USER_STEER", "message": "被用户取消",
        "interaction_id": "storage-only-interaction",
    }
    projected = json.loads(project_tool_result_storage_body(
        json.dumps(carrier), ToolObservationOrigin.PLAN_CONTROL,
    ))
    assert projected == {
        "status": "cancelled", "plan_control": "QUESTION_CANCELLED",
        "cancelled_by": "USER_STEER", "message": "被用户取消",
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
    lowered = lower_canonical_item(
        item,
        artifact_read_available=True,
        limits=StructuredModelInputLimits(),
    )
    full = tuple(
        variant
        for variant in lowered.tool_result_variants
        if variant.mode is ToolResultProviderRenderMode.FULL
    )
    assert bool(full) is (target <= MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES)
    if full:
        assert full[0].utf8_bytes == target


def test_round7_1_no_full_variant_reports_actual_first_mode() -> None:
    item, _body = _body_with_exact_logical_bytes(40_001, seed="🙂")
    request = _prepared_request(
        _snapshot(_user("question"), item),
        _sources(),
    )
    compiled = StructuredModelInputCompiler().compile(request)
    decision = compiled.tool_result_decisions[0]
    assert decision.source_entry_fingerprint == (
        compiled_tool_result_source_fingerprint(item)
    )
    assert decision.first_legal_mode is ToolResultProviderRenderMode.COMPACT
    assert decision.selected_mode is ToolResultProviderRenderMode.COMPACT
    assert decision.reason_code == "FULL_INELIGIBLE_RESULT_BOUND"
    assert decision.delivery_requirement is ToolResultDeliveryRequirement.BEST_AVAILABLE


def test_round7_1_complete_canonical_body_can_still_have_no_full_variant() -> None:
    item = _tool_result("a" * 40_000, sequence=2, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu')
    lowered = lower_canonical_item(
        item,
        artifact_read_available=True,
        limits=StructuredModelInputLimits(),
    )
    assert item.tool_result_context is not None
    assert item.tool_result_context.display_kind.value == "COMPLETE"
    assert lowered.tool_result_variants[0].mode is ToolResultProviderRenderMode.COMPACT
    assert all(
        variant.mode is not ToolResultProviderRenderMode.FULL
        for variant in lowered.tool_result_variants
    )


def test_round7_1_variant_validator_accepts_ordered_subsets_without_full() -> None:
    item = _tool_result("x" * 40_000, sequence=2, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu')
    lowered = lower_canonical_item(
        item,
        artifact_read_available=True,
        limits=StructuredModelInputLimits(),
    )
    by_mode = {variant.mode: variant for variant in lowered.tool_result_variants}
    for mode in (
        ToolResultProviderRenderMode.COMPACT,
        ToolResultProviderRenderMode.REF_ONLY,
    ):
        text = by_mode[mode].message.content[0].text
        body = json.loads(text)["pulsara_tool_result"]["body"]
        target, _ = json.JSONDecoder().raw_decode(body.split("target=", 1)[1])
        assert target == {"kind": "ARTIFACT", "artifact_id": item.tool_result_context.artifact_id}
        assert "offset/limit" not in text
    ref_and_omitted = (
        by_mode[ToolResultProviderRenderMode.REF_ONLY],
        by_mode[ToolResultProviderRenderMode.OMITTED_BODY],
    )
    assert LoweredCanonicalItem(item, None, ref_and_omitted).tool_result_variants == (
        ref_and_omitted
    )
    assert LoweredCanonicalItem(
        item,
        None,
        (by_mode[ToolResultProviderRenderMode.OMITTED_BODY],),
    ).tool_result_variants
    with pytest.raises(ValueError, match="ordered subset"):
        LoweredCanonicalItem(item, None, tuple(reversed(ref_and_omitted)))


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
            ToolResultFullDeliveryReason.ARTIFACT_PAGE,
        )
    with pytest.raises(TypeError, match="reason is invalid"):
        FrozenToolResultDeliveryRequirement(  # type: ignore[arg-type]
            ToolResultDeliveryRequirement.FULL_REQUIRED,
            "ARTIFACT_PAGE",
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
        artifact_read_available=True,
        limits=StructuredModelInputLimits(),
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
    result_item, _body = _body_with_exact_logical_bytes(40_001, seed="中")
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
    assert decision.first_legal_mode is ToolResultProviderRenderMode.COMPACT
    assert decision.selected_mode is ToolResultProviderRenderMode.COMPACT
    assert decision.reason_code == "FULL_INELIGIBLE_RESULT_BOUND"
    assert after.messages[: len(installed.messages)] == installed.messages


def test_round7_1_parallel_siblings_degrade_without_downgrading_required_result() -> (
    None
):
    calls = tuple(
        ProviderToolCall(f"call:{index}", "test_tool", freeze_json({}))
        for index in range(1, 4)
    )
    request_item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.ASSISTANT_TOOL_REQUEST,
        'entry:k5zfbzct6mhkogz33gyubxx3pa',
        1,
        'turn:klmmazz67vfgsfsvg6ceqgdsfu',
        (LLMTextPart(""),),
        tool_calls=calls,
    )
    results = tuple(
        replace(
            _tool_result(
                ("required-" if index == 1 else "ordinary-") + ("x" * 12_000),
                sequence=index + 1,
                turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu',
            ),
            tool_call_id=calls[index - 1].tool_call_id,
            tool_result_delivery=(
                full_required_tool_result_delivery(
                    ToolResultFullDeliveryReason.ARTIFACT_PAGE
                )
                if index == 1
                else BEST_AVAILABLE_TOOL_RESULT_DELIVERY
            ),
        )
        for index in range(1, 4)
    )
    snapshot = _snapshot(request_item, *results)
    request = _prepared_request(snapshot, _sources(), budget=1)
    compiler = StructuredModelInputCompiler()
    initial = compiler.compile(request)
    assert all(item.selected_mode is ToolResultProviderRenderMode.FULL for item in initial.tool_result_decisions)
    floors = compiler.next_wire_render_floors(request=request, compiled=initial)
    constrained = compiler.compile(request, source_render_floors=floors[0], tool_render_floors=floors[1], wire_selection=initial)
    decisions = constrained.tool_result_decisions
    assert decisions[0].delivery_requirement is (
        ToolResultDeliveryRequirement.FULL_REQUIRED
    )
    assert decisions[0].selected_mode is ToolResultProviderRenderMode.FULL
    assert any(
        decision.selected_mode is not ToolResultProviderRenderMode.FULL
        for decision in decisions[1:]
    )
    tool_result_ids = tuple(
        message.tool_call_id
        for message in constrained.messages
        if message.tool_call_id is not None
    )
    assert tool_result_ids == tuple(call.tool_call_id for call in calls)


@pytest.mark.parametrize(
    "reason",
    [
        ToolResultFullDeliveryReason.ARTIFACT_PAGE,
        ToolResultFullDeliveryReason.ARTIFACT_EXPORT_LOCATION,
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


def test_round7_1_full_delivery_classifier_is_request_and_result_derived() -> None:
    assert classify_tool_result_delivery(
        tool_name="artifact_read", result_state="SUCCESS"
    ) == full_required_tool_result_delivery(ToolResultFullDeliveryReason.ARTIFACT_PAGE)
    assert (
        classify_tool_result_delivery(tool_name="artifact_read", result_state="ERROR")
        == BEST_AVAILABLE_TOOL_RESULT_DELIVERY
    )
    assert (
        classify_tool_result_delivery(
            tool_name="third_party_tool", result_state="SUCCESS"
        )
        == BEST_AVAILABLE_TOOL_RESULT_DELIVERY
    )
    for tool_name in ("list_capabilities",):
        assert classify_tool_result_delivery(
            tool_name=tool_name,
            result_state="SUCCESS",
        ) == full_required_tool_result_delivery(
            ToolResultFullDeliveryReason.MCP_DIRECTORY_PAGE
        )

    assert classify_tool_result_delivery(
        tool_name="inspect_capability",
        result_state="SUCCESS",
        public_arguments={
            "target": {"kind": "MCP_TOOL", "server_id": "s", "tool_name": "t"}
        },
    ) == full_required_tool_result_delivery(
        ToolResultFullDeliveryReason.MCP_INSPECT_SCHEMA
    )

    assert (
        classify_tool_result_delivery(
            tool_name="inspect_capability",
            result_state="SUCCESS",
            public_arguments={
                "target": {
                    "kind": "HOOK_SOURCE",
                    "scope": "USER",
                    "source_kind": "LOCAL",
                }
            },
        )
        == BEST_AVAILABLE_TOOL_RESULT_DELIVERY
    )

    for reason in ToolResultFullDeliveryReason:
        requirement = full_required_tool_result_delivery(reason)
        assert requirement.requirement is ToolResultDeliveryRequirement.FULL_REQUIRED
        assert requirement.reason is reason


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
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 30
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 11
    assert len(APPEND_GUARDS) == 1
    assert len(CONVERSATION_KERNEL_RELATIONS) == 31

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
    ("read_only", "NO_RELIABLE_OBSERVATION"),
    ("filesystem_write", "UNKNOWN"),
])
def test_explicit_failure_facts_survive_preview_and_all_provider_variants(
    effect_class, expected_outcome,
) -> None:
    from time import monotonic
    from pulsara_agent.conversation_kernel.tool_execution import _tool_invocation_failure
    from pulsara_agent.conversation_kernel.tool_artifacts import (
        ToolOutputArtifactProcessor, KnownArtifactPublicationFailure,
    )
    from tests.test_round1_tool_output_artifact import _RecordingPublisher

    result = _tool_invocation_failure(RuntimeError("connection closed " * 9000), effect_class)
    original = json.loads(result.content)
    processor = ToolOutputArtifactProcessor(
        object(), publisher=_RecordingPublisher(failure=KnownArtifactPublicationFailure("unavailable")),
    )
    projection = processor.prepare(
        session_id='session:336wrfwkamrr3up6kjtsgvgcte',
        workspace_id='workspace:336wrfwkamrr3up6kjtsgvgcte',
        result_entry_id='entry:2b6xhmrsvzz7q4sgugd5cuq34a',
        public_output=result.content.decode(), candidate=None,
        artifact_inline_result=False, deadline_monotonic=monotonic()+5,
    )
    preview = projection.canonical_preview.canonical_bytes.decode()
    parsed = json.loads(preview)
    assert parsed['physical_outcome'] == expected_outcome
    assert parsed['retry_performed'] is False
    assert parsed['guidance'] == original['guidance']
    assert len(preview.encode()) < len(result.content)
    item = _tool_result(preview, sequence=2, turn_id='turn:klmmazz67vfgsfsvg6ceqgdsfu')
    item = replace(item, tool_result_context=replace(
        item.tool_result_context,
        result_state='SYSTEM_ERROR', artifact_id=None,
        artifact_disposition=projection.artifact_disposition,
        artifact_unavailability_reason=projection.artifact_unavailability_reason,
    ))
    lowered = lower_canonical_item(item, artifact_read_available=False, limits=StructuredModelInputLimits())
    assert any(v.mode is ToolResultProviderRenderMode.OMITTED_BODY for v in lowered.tool_result_variants)
    for variant in lowered.tool_result_variants:
        body = decode_provider_tool_result_observation(variant.message.content[0].text)['body']
        assert f'"physical_outcome":"{expected_outcome}"' in body
        assert '"retry_performed":false' in body
        assert original['guidance'] in body
