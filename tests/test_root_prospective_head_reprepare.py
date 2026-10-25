"""Only a later global head can retire an unpublished accepted ROOT plan."""
from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.provider_dispatch import ProviderDispatchCoordinator
from pulsara_agent.llm.input import LLMTextPart
from pulsara_agent.llm.model_connections import ReasoningEffortSelection
from pulsara_agent.model_input.contracts import (
    CanonicalModelInputIdentity, CanonicalModelInputSnapshot,
    LateToolOutcomeObservation,
    ModelInputCompileFailureKind, ProviderToolResultClosure,
    ProviderToolResultClosureKind, StructuredModelInputCompileError,
    canonical_compile_snapshot_fingerprint, canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint, context_binding_compile_fact_fingerprint,
)
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.model_input.provider_replay import (
    FrozenCanonicalProviderDispatchRead, freeze_provider_replay_manifest,
    freeze_provider_replay_manifest_cut,
)
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource, build_run_permission_snapshot,
)
from tests.support.model_config import test_model_binding, test_model_runtime
from tests.support.round3 import static_canonical_compile_facts
from tests.test_round3_structured_model_input_compiler import _snapshot, _user


def _refreeze_fact(value, fingerprint_name, fingerprint, **changes):
    values = {field.name: getattr(value, field.name) for field in fields(value)
              if field.name != fingerprint_name}
    values.update(changes)
    provisional = object.__new__(type(value))
    for name, item in values.items():
        object.__setattr__(provisional, name, item)
    object.__setattr__(provisional, fingerprint_name, '')
    return type(value)(**values, **{fingerprint_name: fingerprint(provisional)})


def _read(facts, *, head, drift=None):
    canonical = facts.canonical_input
    identity_values = {field.name: getattr(canonical.identity, field.name)
                       for field in fields(canonical.identity)
                       if field.name != 'identity_fingerprint'}
    identity_values['provider_input_through_sequence'] = head
    if drift == 'identity':
        identity_values['initial_entry_id'] = 'different-entry'
    identity = CanonicalModelInputIdentity(**identity_values,
        identity_fingerprint=canonical_model_input_identity_fingerprint(**identity_values))
    values = dict(identity=identity, items=canonical.items,
                  canonical_expanded_bytes=canonical.canonical_expanded_bytes,
                  closures=canonical.closures, late_outcomes=canonical.late_outcomes)
    if drift == 'payload':
        values.update(items=(replace(canonical.items[0], content=(LLMTextPart('changed'),)),),
                      canonical_expanded_bytes=len('changed'))
    if drift == 'closure':
        values['closures'] = (ProviderToolResultClosure('assistant', 'call',
            ProviderToolResultClosureKind.INTERRUPTED_BEFORE_DISPATCH, head),)
    if drift == 'late-outcome':
        values['late_outcomes'] = (LateToolOutcomeObservation('assistant', 'call', 'result', head, 'SUCCESS'),)
    canonical = CanonicalModelInputSnapshot(**values,
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(**values))
    changes = {'canonical_input': canonical}
    if drift == 'permission':
        changes['run_permission_snapshot'] = build_run_permission_snapshot(
            snapshot_id=facts.run_permission_snapshot.snapshot_id,
            requested_mode=PermissionMode.READ_ONLY, effective_mode=PermissionMode.READ_ONLY,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION)
    if drift == 'context':
        changes['context_binding_fact'] = _refreeze_fact(facts.context_binding_fact,
            'fact_fingerprint', context_binding_compile_fact_fingerprint,
            revision_ordinal=facts.context_binding_fact.revision_ordinal + 1)
    compiled = _refreeze_fact(facts, 'canonical_read_cut_fingerprint',
                             canonical_compile_snapshot_fingerprint, **changes)
    manifests = ()
    if drift == 'replay':
        manifests = (freeze_provider_replay_manifest(replay_id='replay', assistant_entry_id='assistant',
            wire_api='openai_chat_completions', codec_kind='test',
            provider_replay_contract_fingerprint='sha256:a', replay_target_fingerprint='sha256:b',
            public_projection_fingerprint='sha256:c', payload_digest='sha256:d', payload_size=2,
            item_count=1, fragment_fingerprint='sha256:e'),)
    replay = freeze_provider_replay_manifest_cut(session_id=identity.session_id,
        scope=ProviderInputContinuityScope(identity.session_id, identity.conversation_scope_kind,
                                          identity.scope_subagent_task_id),
        context_binding_revision_id=identity.context_binding_revision_id,
        provider_input_through_sequence=head, manifests=manifests)
    return FrozenCanonicalProviderDispatchRead(compile_snapshot=compiled, replay_manifest_cut=replay,
        composite_fingerprint=context_fingerprint('pulsara.canonical-provider-dispatch-read:v1',
            {'compile': compiled.canonical_read_cut_fingerprint, 'replay_manifest_cut': replay.cut_fingerprint}))


@pytest.mark.parametrize('drift', [None, 'equal-head-payload', 'backward-head', 'binding', 'identity',
                                  'payload', 'closure', 'late-outcome', 'permission', 'context', 'replay'])
def test_activation_retires_only_a_head_only_measurement(drift):
    _activation(drift=drift)


@pytest.mark.parametrize('failure_at', ['handle', 'borrow', 'hook', 'planning'])
def test_head_only_retirement_failure_propagates_without_repeating_cleanup(failure_at):
    _activation(failure_at=failure_at)


def _activation(*, drift=None, failure_at=None):
    facts = static_canonical_compile_facts(_snapshot(_user('prompt')))
    head = facts.canonical_input.identity.provider_input_through_sequence
    prepared_read = _read(facts, head=head)
    actual_head = head if drift == 'equal-head-payload' else head - 1 if drift == 'backward-head' else head + 1
    actual_read = _read(facts, head=actual_head, drift='payload' if drift == 'equal-head-payload' else drift)
    binding = test_model_binding(test_model_runtime())
    observed_binding = replace(binding, reasoning=ReasoningEffortSelection('high')) if drift == 'binding' else binding
    calls = {key: 0 for key in ('handle', 'borrow', 'hook', 'planning')}
    failure = OSError('exact retirement failed')

    def settle(name):
        calls[name] += 1
        if failure_at == name:
            raise failure

    handle = SimpleNamespace(cut=object(), close=lambda: settle('handle'))
    borrow = SimpleNamespace(close=lambda: settle('borrow'))
    hook = SimpleNamespace(retire=lambda: settle('hook'))
    prepared = SimpleNamespace(
        admission=SimpleNamespace(canonical_read=prepared_read,
            candidate=SimpleNamespace(exact_turn_id='turn', model_call_binding=binding)),
        planning=object(), take_resources=lambda: (borrow, hook, object()))

    class IO:
        async def run(self, fn, *args, **kwargs):
            return fn(*args, **kwargs)

    owner = object.__new__(ProviderDispatchCoordinator)
    owner._io = IO()
    owner._safe_point = SimpleNamespace(freeze_provider_input=lambda **kwargs: handle)
    owner._repository = SimpleNamespace(read_turn_model_call_binding=lambda *args, **kwargs: observed_binding)
    owner._writer_lease = SimpleNamespace(guard=object())
    owner._continuity = SimpleNamespace(abort_planning=lambda planning: settle('planning'))
    async def read(*args, **kwargs):
        return actual_read
    owner.read_dispatch_read = read

    async def scenario():
        if failure_at:
            with pytest.raises(OSError) as caught:
                await owner.activate_prospective_root_input(prepared, deadline=1)
            assert caught.value is failure
        elif drift is None:
            assert await owner.activate_prospective_root_input(prepared, deadline=1) is None
        else:
            with pytest.raises(StructuredModelInputCompileError) as caught:
                await owner.activate_prospective_root_input(prepared, deadline=1)
            assert caught.value.kind is ModelInputCompileFailureKind.CANONICAL_PREFIX_CONFLICT
        assert calls == {'handle': 1, 'borrow': 1, 'hook': 1, 'planning': 1}
    asyncio.run(scenario())
