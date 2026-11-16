"""Accepted session call references, independent of pages, Cells and epochs."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from pulsara_agent.capability.contracts import CapabilityKind, CapabilitySourceKind, capability_identity, capability_source_ref
from pulsara_agent.conversation_kernel.mcp.meta import ProcessLocalNewMcpToolRefOwner
from pulsara_agent.identity import random_id, require_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind


def _identity(name):
    return capability_identity(kind=CapabilityKind.TOOL, source=capability_source_ref(CapabilitySourceKind.MCP_SERVER, 'fixture'), stable_name=name)


def _scope(kind=ModelInputScopeKind.ROOT):
    return dict(conversation_scope_kind=kind, session_id=random_id('session'))


def prepare(owner, scope, name='probe', binding=('schema', 'target', 'account')):
    return owner.prepare(**scope, capability_identity=_identity(name), binding=binding, result_entry_id=random_id('entry'))


@pytest.mark.parametrize('kind', tuple(ModelInputScopeKind))
def test_accepting_full_query_result_immediately_allows_same_session_calls(kind):
    owner, scope = ProcessLocalNewMcpToolRefOwner(), _scope(kind)
    prepared = prepare(owner, scope)
    token = prepared.ref.opaque_token
    require_id(token, 'mcpref')
    with pytest.raises(LookupError):
        owner.resolve_callable(token, **scope)
    owner.settle(prepared=prepared, committed=True)
    # No provider-page acknowledgement, epoch identity, or Cell identity is a
    # calling condition. All callers in this session reuse the accepted ref.
    assert owner.resolve_callable(token, **scope) is prepared.ref
    assert prepare(owner, scope).ref is prepared.ref
    with pytest.raises(LookupError):
        owner.resolve_callable(token, **_scope(kind))


def test_aborted_query_does_not_create_call_qualification():
    owner, scope = ProcessLocalNewMcpToolRefOwner(), _scope()
    prepared = prepare(owner, scope)
    owner.settle(prepared=prepared, committed=False)
    with pytest.raises(LookupError):
        owner.resolve_callable(prepared.ref.opaque_token, **scope)


def test_concurrent_queries_reuse_one_binding_with_independent_settlement():
    owner, scope, barrier = ProcessLocalNewMcpToolRefOwner(), _scope(), Barrier(8)
    def inspect(_):
        barrier.wait()
        return prepare(owner, scope)
    with ThreadPoolExecutor(max_workers=8) as executor:
        values = list(executor.map(inspect, range(8)))
    assert all(value.ref is values[0].ref for value in values)
    for value in values[1:]:
        owner.settle(prepared=value, committed=False)
    owner.settle(prepared=values[0], committed=True)
    assert owner.resolve_callable(values[0].ref.opaque_token, **scope) is values[0].ref


def test_changed_tool_only_retires_affected_ref_and_unknown_directory_preserves_it():
    owner, scope = ProcessLocalNewMcpToolRefOwner(), _scope()
    a, b = prepare(owner, scope, 'a'), prepare(owner, scope, 'b')
    for item in (a, b):
        owner.settle(prepared=item, committed=True)
    owner.reconcile(**scope, server_ids={'fixture'}, complete_server_ids=set(), bindings={})
    assert owner.resolve_callable(a.ref.opaque_token, **scope) is a.ref
    owner.reconcile(**scope, server_ids={'fixture'}, complete_server_ids={'fixture'}, bindings={
        _identity('a'): ('new schema', 'target', 'account'), _identity('b'): b.ref.binding})
    with pytest.raises(LookupError):
        owner.resolve_callable(a.ref.opaque_token, **scope)
    assert owner.resolve_callable(b.ref.opaque_token, **scope) is b.ref
    changed = prepare(owner, scope, 'a', ('new schema', 'target', 'account'))
    owner.settle(prepared=changed, committed=True)
    owner.reconcile(**scope, server_ids={'fixture'}, complete_server_ids={'fixture'}, bindings={_identity('b'): b.ref.binding})
    with pytest.raises(LookupError):
        owner.resolve_callable(changed.ref.opaque_token, **scope)


def test_session_reference_inventory_remains_usable_beyond_old_epoch_cohort():
    owner, scope = ProcessLocalNewMcpToolRefOwner(), _scope()
    values = [prepare(owner, scope, str(index)) for index in range(1030)]
    for item in values:
        owner.settle(prepared=item, committed=True)
    assert all(owner.resolve_callable(item.ref.opaque_token, **scope) is item.ref for item in values)
