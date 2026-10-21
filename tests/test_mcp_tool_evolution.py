"""Frozen native contracts, sparse discovery, and live meta references."""

from copy import deepcopy
from dataclasses import replace
import json

import pytest

from pulsara_agent.capability.contracts import (
    CapabilitySourceKind,
    InstalledCapabilityEpochPredecessor,
    ToolCapabilityOrigin,
    ToolCapabilityRouteKind,
)
from pulsara_agent.capability.mcp_compatibility import (
    NativeMcpCompatibility,
    native_mcp_compatibility,
)
from pulsara_agent.conversation_kernel.context_sources import _render_mcp_catalog
from pulsara_agent.conversation_kernel.mcp.meta import ProcessLocalNewMcpToolRefOwner
from pulsara_agent.model_input.contracts import FrozenToolSpec, ModelInputScopeKind
from pulsara_agent.primitives.context import freeze_json
from tests.test_round9_unified_capability_semantics import (
    _fact,
    _finalized_plan,
    _planning_view,
)


def spec(schema, *, description="Tool", output=None):
    return FrozenToolSpec(
        "mcp__fixture__probe",
        description,
        freeze_json(schema),
        "descriptor",
        output_schema=None if output is None else freeze_json(output),
    )


BASE = {
    "type": "object",
    "properties": {"value": {"type": "string", "enum": ["a", "b"]}},
    "required": ["value"],
    "additionalProperties": False,
}


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"description": "New instructions"}, True),
        (
            {
                "properties": {
                    "value": {
                        "type": "string",
                        "enum": ["b", "a"],
                        "description": "New prose",
                    }
                }
            },
            True,
        ),
        ({"properties": {"value": {"type": "string", "enum": ["a", "b", "c"]}}}, True),
        ({"properties": {**BASE["properties"], "extra": {"type": "boolean"}}}, True),
        ({"required": []}, True),
        ({"additionalProperties": True}, True),
        (
            {
                "required": ["value", "extra"],
                "properties": {**BASE["properties"], "extra": {"type": "boolean"}},
            },
            False,
        ),
        ({"properties": {}}, False),
        ({"properties": {"value": {"type": "integer"}}}, False),
        ({"properties": {"value": {"type": "string", "enum": ["a"]}}}, False),
        ({"default": {"value": "a"}}, False),
        ({"x-mcp-header": "X-Changed"}, False),
    ],
)
def test_native_compatibility_finite_changes(change, expected):
    changed = {**deepcopy(BASE), **deepcopy(change)}
    actual = native_mcp_compatibility(spec(BASE), spec(changed))
    assert (actual is NativeMcpCompatibility.COMPATIBLE) is expected


@pytest.mark.parametrize(
    "keyword,old,new",
    [
        ("minimum", 3, 2),
        ("exclusiveMinimum", 3, 2),
        ("maximum", 3, 4),
        ("minLength", 3, 2),
        ("maxLength", 3, 4),
        ("minItems", 3, 2),
        ("maxItems", 3, 4),
        ("minProperties", 3, 2),
        ("maxProperties", 3, 4),
    ],
)
def test_simple_bounds_only_widen(keyword, old, new):
    before = {"type": "object", keyword: old}
    after = {"type": "object", keyword: new}
    assert (
        native_mcp_compatibility(spec(before), spec(after))
        is NativeMcpCompatibility.COMPATIBLE
    )
    assert (
        native_mcp_compatibility(spec(after), spec(before))
        is not NativeMcpCompatibility.COMPATIBLE
    )


def test_optional_is_not_necessarily_backward_compatible():
    before = {"type": "object", "properties": {}}
    after = {"type": "object", "properties": {"extra": {"type": "string"}}}
    assert (
        native_mcp_compatibility(spec(before), spec(after))
        is not NativeMcpCompatibility.COMPATIBLE
    )


@pytest.mark.parametrize(
    "before,after",
    [
        ({"type": "string", "default": "a"}, {}),
        (False, {"default": "a"}),
        ({"default": {}}, {"default": {"x": 1}}),
        ({"pattern": "a"}, {"pattern": "b"}),
        ({"$schema": "https://json-schema.org/draft/2020-12/schema"}, {}),
    ],
)
def test_execution_metadata_changes_are_unconfirmed_even_with_empty_schema(
    before, after
):
    old = {"type": "object", "properties": {"value": before}}
    new = {"type": "object", "properties": {"value": after}}
    assert (
        native_mcp_compatibility(spec(old), spec(new))
        is NativeMcpCompatibility.UNCONFIRMED
    )


def test_new_property_inherits_old_extra_property_execution_metadata():
    before = {
        "type": "object",
        "additionalProperties": {"type": "string", "default": "a"},
    }
    after = {**before, "properties": {"x": {"type": "string", "default": "b"}}}
    assert (
        native_mcp_compatibility(spec(before), spec(after))
        is NativeMcpCompatibility.UNCONFIRMED
    )


def test_schema_traversal_never_erases_data_or_follows_negative_references():
    before = {
        "type": "object",
        "properties": {"value": {"const": {"description": "old"}}},
    }
    after = deepcopy(before)
    after["properties"]["value"]["const"]["description"] = "new"
    assert (
        native_mcp_compatibility(spec(before), spec(after))
        is not NativeMcpCompatibility.COMPATIBLE
    )
    before = {
        "type": "object",
        "$defs": {"t": {"enum": ["a"]}},
        "not": {"$ref": "#/$defs/t"},
    }
    after = deepcopy(before)
    after["$defs"]["t"]["enum"].append("b")
    assert (
        native_mcp_compatibility(spec(before), spec(after))
        is NativeMcpCompatibility.UNCONFIRMED
    )
    after = deepcopy(before)
    after["$defs"]["t"]["description"] = "new documentation"
    assert (
        native_mcp_compatibility(spec(before), spec(after))
        is NativeMcpCompatibility.COMPATIBLE
    )
    assert (
        native_mcp_compatibility(spec({"const": True}), spec({"const": 1}))
        is not NativeMcpCompatibility.COMPATIBLE
    )


def test_output_contract_change_requires_meta_but_prose_does_not():
    output = {"type": "object", "properties": {"value": {"type": "string"}}}
    annotated = {**output, "description": "new documentation"}
    changed = {"type": "object", "properties": {"value": {"type": "integer"}}}
    assert (
        native_mcp_compatibility(
            spec(BASE, output=output), spec(BASE, output=annotated)
        )
        is NativeMcpCompatibility.COMPATIBLE
    )
    assert (
        native_mcp_compatibility(spec(BASE, output=output), spec(BASE, output=changed))
        is NativeMcpCompatibility.OUTPUT_CHANGED
    )


def predecessor(plan):
    return InstalledCapabilityEpochPredecessor(
        1,
        "epoch:evolution",
        plan.direct_tool_surface,
        plan.direct_projection_set,
        plan.mcp_catalog_route_projection,
    )


def fact(schema=BASE):
    return _fact(
        source_kind=CapabilitySourceKind.MCP_SERVER,
        source_id="fixture",
        name="probe",
        origin=ToolCapabilityOrigin.MCP,
        schema=deepcopy(schema),
    )


def test_epoch_baseline_compatibility_demotion_absence_rollback_and_cold_adoption():
    original = fact()
    cold = _finalized_plan(view=_planning_view(builtin_facts=(), mcp_facts=(original,)))
    expanded = fact(
        {**BASE, "properties": {**BASE["properties"], "extra": {"type": "boolean"}}}
    )
    compatible = _finalized_plan(
        view=_planning_view(
            builtin_facts=(), mcp_facts=(expanded,), predecessor=predecessor(cold)
        )
    )
    assert (
        compatible.mcp_catalog_route_projection.routes[0].route
        is ToolCapabilityRouteKind.DIRECT
    )
    breaking = fact(
        {
            "type": "object",
            "required": ["number"],
            "properties": {"number": {"type": "integer"}},
        }
    )
    meta = _finalized_plan(
        view=_planning_view(
            builtin_facts=(), mcp_facts=(breaking,), predecessor=predecessor(compatible)
        )
    )
    assert (
        meta.mcp_catalog_route_projection.routes[0].route
        is ToolCapabilityRouteKind.NEW_MCP_META_ONLY
    )
    missing = _finalized_plan(
        view=_planning_view(
            builtin_facts=(), mcp_facts=(), predecessor=predecessor(meta)
        )
    )
    assert (
        missing.mcp_catalog_route_projection.routes[0].public_reason_code.value
        == "REMOVED"
    )
    restored = _finalized_plan(
        view=_planning_view(
            builtin_facts=(), mcp_facts=(original,), predecessor=predecessor(missing)
        )
    )
    assert (
        restored.mcp_catalog_route_projection.routes[0].route
        is ToolCapabilityRouteKind.NEW_MCP_META_ONLY
    )
    for plan in (compatible, meta, missing, restored):
        assert plan.direct_tool_surface == cold.direct_tool_surface
        assert plan.direct_projection_set == cold.direct_projection_set
    fresh = _finalized_plan(
        view=_planning_view(builtin_facts=(), mcp_facts=(breaking,))
    )
    assert (
        fresh.mcp_catalog_route_projection.routes[0].route
        is ToolCapabilityRouteKind.DIRECT
    )


def test_catalog_is_sparse_and_ignores_meta_schema_churn_and_removed_server():
    original = fact()
    cold = _finalized_plan(view=_planning_view(builtin_facts=(), mcp_facts=(original,)))
    # Use the same complete catalog that owns the planning cut.
    from pulsara_agent.conversation_kernel.mcp.contracts import build_catalog_snapshot

    empty_catalog = build_catalog_snapshot(
        owner_epoch=1, catalog_revision=1, entries=()
    )
    normal = json.loads(
        _render_mcp_catalog(
            empty_catalog,
            cold.mcp_catalog_route_projection,
            cold.dispatch_view.planning_input.mcp.source_snapshots,
        )[0]
    )
    assert original.canonical_tool_spec.name not in json.dumps(normal)
    changed = fact(
        {
            "type": "object",
            "required": ["new"],
            "properties": {"new": {"type": "integer"}},
        }
    )
    meta = _finalized_plan(
        view=_planning_view(
            builtin_facts=(), mcp_facts=(changed,), predecessor=predecessor(cold)
        )
    )
    # Fact integrity belongs to its factory; do not replace a frozen semantic digest.
    from pulsara_agent.capability.contracts import freeze_tool_capability_fact

    prose = freeze_tool_capability_fact(
        identity=changed.identity,
        origin=changed.origin,
        canonical_tool_spec=replace(
            changed.canonical_tool_spec, description="New prose"
        ),
    )
    current = _finalized_plan(
        view=_planning_view(
            builtin_facts=(), mcp_facts=(prose,), predecessor=predecessor(meta)
        )
    )
    assert _render_mcp_catalog(
        empty_catalog,
        meta.mcp_catalog_route_projection,
        meta.dispatch_view.planning_input.mcp.source_snapshots,
    ) == _render_mcp_catalog(
        empty_catalog,
        current.mcp_catalog_route_projection,
        current.dispatch_view.planning_input.mcp.source_snapshots,
    )
    removed = _finalized_plan(
        view=_planning_view(
            builtin_facts=(), mcp_facts=(), predecessor=predecessor(current)
        )
    )
    value = json.loads(
        _render_mcp_catalog(
            empty_catalog,
            removed.mcp_catalog_route_projection,
            removed.dispatch_view.planning_input.mcp.source_snapshots,
        )[0]
    )
    assert value["servers"][0]["native_unavailable_tools"][0]["reason"] == "REMOVED"


def test_catalog_budget_prioritizes_native_exceptions_across_all_servers():
    from pulsara_agent.conversation_kernel.context_sources import (
        _bounded_mcp_catalog_provider_body,
    )
    from pulsara_agent.conversation_kernel.mcp.contracts import build_catalog_snapshot

    def item(server, name, schema):
        return _fact(
            source_kind=CapabilitySourceKind.MCP_SERVER,
            source_id=server,
            name=name,
            origin=ToolCapabilityOrigin.MCP,
            schema=schema,
        )

    original = tuple(item(server, "probe_" + server, BASE) for server in ("a", "b"))
    cold = _finalized_plan(view=_planning_view(builtin_facts=(), mcp_facts=original))
    breaking = {
        "type": "object",
        "required": ["number"],
        "properties": {"number": {"type": "integer"}},
    }
    current = tuple(
        item(server, "probe_" + server, breaking) for server in ("a", "b")
    ) + tuple(item("a", f"new_{index:03d}_" + "x" * 70, BASE) for index in range(30))
    meta = _finalized_plan(
        view=_planning_view(
            builtin_facts=(),
            mcp_facts=tuple(
                sorted(
                    current,
                    key=lambda fact: (
                        fact.identity.source.stable_source_id,
                        fact.identity.stable_name,
                    ),
                )
            ),
            predecessor=predecessor(cold),
        )
    )
    catalog = build_catalog_snapshot(owner_epoch=1, catalog_revision=1, entries=())
    payload = json.loads(
        _bounded_mcp_catalog_provider_body(
            catalog,
            meta.mcp_catalog_route_projection,
            meta.dispatch_view.planning_input.mcp.source_snapshots,
            maximum_bytes=2600,
        )
    )
    assert len(payload["servers"]) == 2
    assert all(len(row["native_unavailable_tools"]) == 1 for row in payload["servers"])
    assert payload["servers"][0]["omitted_meta_tool_count"] > 0


@pytest.mark.parametrize("commit_old", [False, True])
def test_meta_ref_churn_reclaims_capacity_and_aba_full_delivery_is_isolated(
    monkeypatch,
    commit_old,
):
    monkeypatch.setattr(
        "pulsara_agent.conversation_kernel.mcp.meta.MAXIMUM_LIVE_NEW_MCP_TOOL_REFS_PER_EPOCH",
        1,
    )
    owner = ProcessLocalNewMcpToolRefOwner()
    scope = dict(
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        continuity_epoch_nonce="epoch:evolution",
    )

    def prepare(version, entry):
        return owner.prepare(
            **scope,
            capability_identity_fingerprint="identity",
            tool_semantic_fingerprint=version,
            mcp_execution_policy_fingerprint="policy",
            tool_route_fingerprint="route",
            result_entry_id=entry,
        )

    old = prepare("A", "old-result")
    if commit_old:
        owner.settle(prepared=old, committed=True)
    for index in range(1_025):
        owner.retire_unroutable(**scope, allowed=set())
        item = prepare(str(index), f"result-{index}")
        owner.settle(prepared=item, committed=True)
        assert owner.install_full_result(**scope, result_entry_id=f"result-{index}")
        assert owner.resolve_callable(item.ref.opaque_token, **scope) is item.ref
    owner.retire_unroutable(**scope, allowed=set())
    current = prepare("A", "new-result")
    assert current.ref.opaque_token == old.ref.opaque_token
    owner.settle(
        prepared=old, committed=True
    )  # Delayed settlement cannot touch the new record.
    assert not owner.install_full_result(**scope, result_entry_id="old-result")
    with pytest.raises(LookupError, match="DORMANT"):
        owner.resolve_callable(current.ref.opaque_token, **scope)
    owner.settle(prepared=current, committed=True)
    assert owner.install_full_result(**scope, result_entry_id="new-result")
    assert owner.resolve_callable(current.ref.opaque_token, **scope) is current.ref


def test_inspect_budget_does_not_disable_compatible_native_but_blocks_meta():
    cold = _finalized_plan(view=_planning_view(builtin_facts=(), mcp_facts=(fact(),)))
    expanded = fact(
        {**BASE, "properties": {**BASE["properties"], "extra": {"type": "boolean"}}}
    )
    compatible = _finalized_plan(
        view=_planning_view(
            builtin_facts=(),
            mcp_facts=(expanded,),
            inspect_overbound=frozenset({"probe"}),
            predecessor=predecessor(cold),
        )
    )
    assert (
        compatible.mcp_catalog_route_projection.routes[0].route
        is ToolCapabilityRouteKind.DIRECT
    )
    breaking = fact(
        {
            "type": "object",
            "required": ["number"],
            "properties": {"number": {"type": "integer"}},
        }
    )
    blocked = _finalized_plan(
        view=_planning_view(
            builtin_facts=(),
            mcp_facts=(breaking,),
            inspect_overbound=frozenset({"probe"}),
            predecessor=predecessor(compatible),
        )
    )
    route = blocked.mcp_catalog_route_projection.routes[0]
    assert route.route is ToolCapabilityRouteKind.UNAVAILABLE
    assert route.public_reason_code.value == "DESCRIPTOR_OVERBOUND"
    restored = _finalized_plan(
        view=_planning_view(
            builtin_facts=(), mcp_facts=(fact(),), predecessor=predecessor(blocked)
        )
    )
    assert (
        restored.mcp_catalog_route_projection.routes[0].route
        is ToolCapabilityRouteKind.NEW_MCP_META_ONLY
    )


def test_catalog_selected_variant_deduplication_and_explicit_clear():
    from tests import test_round3_structured_model_input_compiler as fixture
    from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
    from pulsara_agent.model_input.contracts import (
        ContextSourceKind,
        ContextRenderMode,
        ContextSourceAbsenceKind,
    )
    from pulsara_agent.model_input.continuity import (
        decode_runtime_observation,
        NoNewTriggerAnchor,
    )
    from pulsara_agent.conversation_kernel.context_sources import _collected
    from pulsara_agent.llm.input import join_text_content

    kind = ContextSourceKind.MCP_CATALOG
    compiler = StructuredModelInputCompiler()
    owner = fixture.new_test_provider_input_continuity_owner()
    initial = fixture._user("catalog", sequence=1)

    def request(full, mode, index):
        candidate = fixture._candidate(
            kind, (full, "same compact", "same reference"), initial_mode=mode
        )
        return replace(
            fixture._prepared_request(
                fixture._snapshot(initial), fixture._sources(candidate)
            ),
            model_call_index=index,
        )

    def observations(view):
        return [
            decode_runtime_observation(message)
            for message in view.messages
            if "pulsara_runtime_observation" in join_text_content(message.content)
            and decode_runtime_observation(message).source_kind is kind
        ]

    _, first = fixture._compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=request("full A", ContextRenderMode.COMPACT, 1),
    )
    _, second = fixture._compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=request("full B", ContextRenderMode.COMPACT, 2),
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    assert len(observations(first)) == len(observations(second)) == 1
    _, third = fixture._compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=request("full B", ContextRenderMode.FULL, 3),
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    assert [value.body for value in observations(third)] == ["same compact", "full B"]
    sources = fixture._sources()
    cleared = _collected(
        candidates=sources.candidates,
        diagnostics=sources.diagnostics,
        registry_fingerprint=sources.registry_fingerprint,
        absent_facts=tuple(
            value for value in sources.absent_facts if value.source_kind is not kind
        )
        + (fixture._absent(kind, ContextSourceAbsenceKind.EXPLICIT_EMPTY),),
    )
    _, fourth = fixture._compile_and_install_append(
        compiler=compiler,
        owner=owner,
        request=replace(
            fixture._prepared_request(fixture._snapshot(initial), cleared),
            model_call_index=4,
        ),
        dispatch_anchor=NoNewTriggerAnchor(None),
    )
    assert observations(fourth)[-1].presence.value == "CLEARED"
    assert fourth.messages[: len(third.messages)] == third.messages
