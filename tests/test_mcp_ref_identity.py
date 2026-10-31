"""MCP references use canonical IDs through discovery and META invocation."""

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

from jsonschema import Draft202012Validator
import pytest

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog_entry
from pulsara_agent.capability.contracts import CapabilityKind, CapabilitySourceKind, capability_identity, capability_source_ref

def _identity(name):
    return capability_identity(kind=CapabilityKind.TOOL, source=capability_source_ref(CapabilitySourceKind.MCP_SERVER, "fixture"), stable_name=name)
from pulsara_agent.capability.mcp_projection import (
    MAXIMUM_NEW_MCP_TOOL_REF_CHARACTERS,
    McpInspectionDescriptorValues,
    render_inspected_new_mcp_tool_provider_result,
)
from pulsara_agent.conversation_kernel.mcp.meta import (
    McpToolRefConsumerKind,
    ProcessLocalNewMcpToolRefOwner,
)
from pulsara_agent.identity import random_id, require_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.primitives.context import freeze_json, thaw_json


def inspection_values():
    return McpInspectionDescriptorValues(
        server_id='fixture', remote_tool_name='probe', provider_tool_name='mcp__fixture__probe',
        description='Read the fixture.', input_schema=freeze_json({'type': 'object'}),
        output_schema=None, effect_kind='READ_ONLY',
    )


def prepare(owner, scope, **overrides):
    parameters = dict(
        scope, capability_identity=_identity('identity'), tool_semantic_fingerprint='schema',
        mcp_execution_policy_fingerprint='policy', tool_route_fingerprint='route',
        result_entry_id=random_id('entry'),
    )
    parameters.update(overrides)
    return owner.prepare(
        **parameters,
    )


@pytest.mark.parametrize('scope_kind,consumer', [
    (ModelInputScopeKind.ROOT, McpToolRefConsumerKind.MODEL_EPOCH),
    (ModelInputScopeKind.SUBAGENT, McpToolRefConsumerKind.MODEL_EPOCH),
    (ModelInputScopeKind.ROOT, McpToolRefConsumerKind.CODE_CELL),
])
def test_canonical_ref_is_copied_from_inspection_and_keeps_installation_and_scope_checks(scope_kind, consumer):
    owner = ProcessLocalNewMcpToolRefOwner()
    scope = dict(
        conversation_scope_kind=scope_kind, session_id=random_id('session'),
        continuity_epoch_nonce=random_id('epoch'), consumer_kind=consumer,
        consumer_id=random_id('cell') if consumer is McpToolRefConsumerKind.CODE_CELL else None,
    )
    prepared = prepare(owner, scope)
    token = prepared.ref.opaque_token
    assert require_id(token, 'mcpref') == token
    assert len(token) == MAXIMUM_NEW_MCP_TOOL_REF_CHARACTERS == 33
    repeated = prepare(owner, scope)
    assert repeated.ref is prepared.ref
    owner.settle(prepared=repeated, committed=False)
    inspected = json.loads(render_inspected_new_mcp_tool_provider_result(inspection_values(), tool_ref=token))
    arguments = {'target': inspected['invocation']['target'], 'arguments': {}}
    validator = Draft202012Validator(thaw_json(builtin_tool_catalog_entry('use_new_mcp_tool').descriptor.input_schema))
    validator.validate(arguments)
    owner.settle(prepared=prepared, committed=True)
    with pytest.raises(LookupError, match='DORMANT'):
        owner.resolve_callable(token, **scope)
    assert owner.install_full_result(result_entry_id=prepared.result_entry_id, **scope)
    assert owner.resolve_callable(arguments['target']['tool_ref'], **scope) is prepared.ref
    with pytest.raises(LookupError, match='STALE'):
        owner.resolve_callable(token, **{**scope, 'session_id': random_id('session')})
    with pytest.raises(LookupError, match='NOT_FOUND'):
        ProcessLocalNewMcpToolRefOwner().resolve_callable(token, **scope)


@pytest.mark.parametrize('changes', [
    {'conversation_scope_kind': ModelInputScopeKind.SUBAGENT},
    {'session_id': 'session:bbbbbbbbbbbbbbbbbbbbbbbbba'},
    {'continuity_epoch_nonce': 'epoch:bbbbbbbbbbbbbbbbbbbbbbbbba'},
    {'consumer_kind': McpToolRefConsumerKind.MODEL_EPOCH, 'consumer_id': None},
    {'consumer_id': 'cell:bbbbbbbbbbbbbbbbbbbbbbbbba'},
    {'runtime_generation': 2},
    {'capability_identity': _identity('other-identity')},
    {'tool_semantic_fingerprint': 'other-schema'},
    {'mcp_execution_policy_fingerprint': 'other-policy'},
    {'tool_route_fingerprint': 'other-route'},
])
def test_distinct_bindings_have_independent_refs(changes):
    owner = ProcessLocalNewMcpToolRefOwner()
    scope = dict(
        conversation_scope_kind=ModelInputScopeKind.ROOT, session_id=random_id('session'),
        continuity_epoch_nonce=random_id('epoch'), consumer_kind=McpToolRefConsumerKind.CODE_CELL,
        consumer_id=random_id('cell'), runtime_generation=1,
    )
    original = prepare(owner, scope)
    changed = prepare(owner, scope, **changes)
    assert changed.ref.opaque_token != original.ref.opaque_token
    assert prepare(owner, scope).ref is original.ref
    assert prepare(owner, scope, **changes).ref is changed.ref


def test_random_ref_collision_preserves_existing_callable_binding(monkeypatch):
    owner = ProcessLocalNewMcpToolRefOwner()
    scope = dict(conversation_scope_kind=ModelInputScopeKind.ROOT,
                 session_id=random_id('session'), continuity_epoch_nonce=random_id('epoch'))
    original = prepare(owner, scope)
    owner.settle(prepared=original, committed=True)
    assert owner.install_full_result(result_entry_id=original.result_entry_id, **scope)
    fresh_token = random_id('mcpref')
    candidates = iter((original.ref.opaque_token, fresh_token))
    monkeypatch.setattr(
        'pulsara_agent.conversation_kernel.mcp.meta.random_id',
        lambda kind: next(candidates) if kind == 'mcpref' else random_id(kind),
    )
    other_scope = {**scope, 'session_id': random_id('session')}
    changed = prepare(owner, other_scope)
    assert changed.ref.opaque_token == fresh_token
    assert owner.resolve_callable(original.ref.opaque_token, **scope) is original.ref
    assert prepare(owner, other_scope).ref is changed.ref


def test_concurrent_inspections_reuse_one_binding_without_losing_settlements():
    owner = ProcessLocalNewMcpToolRefOwner()
    scope = dict(conversation_scope_kind=ModelInputScopeKind.ROOT,
                 session_id=random_id('session'), continuity_epoch_nonce=random_id('epoch'))
    barrier = Barrier(8)

    def inspect(_):
        barrier.wait()
        return prepare(owner, scope)

    with ThreadPoolExecutor(max_workers=8) as executor:
        prepared = list(executor.map(inspect, range(8)))
    first, *others = prepared
    assert all(item.ref is first.ref for item in others)
    assert len({item.settlement_token_id for item in prepared}) == len(prepared)
    for item in others:
        owner.settle(prepared=item, committed=False)
    owner.settle(prepared=first, committed=True)
    assert owner.install_full_result(result_entry_id=first.result_entry_id, **scope)
    assert owner.resolve_callable(first.ref.opaque_token, **scope) is first.ref


@pytest.mark.parametrize('invalid', [
    'mcpref_' + 'a' * 43,
    'mcpref:' + 'a' * 43,
    'mcpref:' + 'A' * 26,
    'mcpref:' + 'a' * 25 + 'b',
    'mcpref:' + 'a' * 25,
    'cell:' + 'a' * 26,
])
def test_noncanonical_refs_are_rejected_by_schema_projection_and_owner(invalid):
    owner = ProcessLocalNewMcpToolRefOwner()
    scope = dict(conversation_scope_kind=ModelInputScopeKind.ROOT,
                 session_id=random_id('session'), continuity_epoch_nonce=random_id('epoch'))
    prepared = prepare(owner, scope)
    validator = Draft202012Validator(thaw_json(builtin_tool_catalog_entry('use_new_mcp_tool').descriptor.input_schema))
    assert not validator.is_valid({'target': {'kind': 'MCP_TOOL_REF', 'tool_ref': invalid}, 'arguments': {}})
    with pytest.raises(ValueError, match='INVALID_ID'):
        replace(prepared.ref, opaque_token=invalid)
    with pytest.raises(ValueError, match='INVALID_ID'):
        render_inspected_new_mcp_tool_provider_result(inspection_values(), tool_ref=invalid)
    with pytest.raises(LookupError, match='NOT_FOUND'):
        owner.resolve_callable(invalid, **scope)


def test_refs_compare_complete_identity_even_if_boundary_digests_collide(monkeypatch):
    monkeypatch.setattr(
        'pulsara_agent.capability.contracts.capability_identity_digest',
        lambda identity: 'sha256:' + '0' * 64,
    )
    owner = ProcessLocalNewMcpToolRefOwner()
    scope = dict(conversation_scope_kind=ModelInputScopeKind.ROOT,
                 session_id=random_id('session'), continuity_epoch_nonce=random_id('epoch'))
    first = prepare(owner, scope, capability_identity=_identity('one'))
    second = prepare(owner, scope, capability_identity=_identity('two'))
    assert first.ref.capability_identity != second.ref.capability_identity
    assert first.ref.opaque_token != second.ref.opaque_token
    owner.settle(prepared=first, committed=True)
    assert owner.install_full_result(result_entry_id=first.result_entry_id, **scope)
    owner.retire_unroutable(**scope, allowed={
        (second.ref.capability_identity, 'schema', 'policy', 'route', None),
    })
    with pytest.raises(LookupError, match='NOT_FOUND'):
        owner.resolve_callable(first.ref.opaque_token, **scope)
    assert prepare(owner, scope, capability_identity=_identity('two')).ref is second.ref
