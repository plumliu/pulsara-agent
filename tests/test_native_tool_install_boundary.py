"""Native range acceptance shares the actual process-local install boundary."""

import pytest

from pulsara_agent.conversation_kernel.input_continuity import ProviderInputContinuityConflict
from pulsara_agent.model_input.compiler import StructuredModelInputCompiler
from tests.test_round3_structured_model_input_compiler import (
    _prepare_append_candidate, _prepared_request, _snapshot, _sources, _user,
    new_test_provider_input_continuity_owner,
)


def prepared():
    owner = new_test_provider_input_continuity_owner()
    _, candidate = _prepare_append_candidate(
        compiler=StructuredModelInputCompiler(), owner=owner,
        request=_prepared_request(_snapshot(_user("native install")), _sources()),
    )
    owner.register(candidate)
    return owner, candidate


def test_failed_native_freeze_keeps_prefix_uninstalled_and_same_candidate_retryable():
    owner, candidate = prepared()
    failure = OSError("native range transaction failed")
    calls = []

    def fail():
        assert owner.current_view(candidate.scope) is None
        calls.append("failed")
        raise failure

    with pytest.raises(OSError) as rejected:
        owner.install(candidate=candidate, execution=object(), before_install=fail)
    assert rejected.value is failure
    assert owner.current_view(candidate.scope) is None
    assert owner._issued_permits == {}

    def accept():
        assert owner.current_view(candidate.scope) is None
        calls.append("accepted")

    permit = owner.install(candidate=candidate, execution=object(), before_install=accept)
    assert owner.current_view(candidate.scope).epoch_revision == permit.epoch_revision == 1
    assert calls == ["failed", "accepted"]


def test_stale_install_never_freezes_a_native_observation():
    owner, candidate = prepared()
    owner.discard(candidate)
    called = []
    with pytest.raises(ProviderInputContinuityConflict, match="exact-join"):
        owner.install(candidate=candidate, execution=object(), before_install=lambda: called.append(True))
    assert called == []
    assert owner.current_view(candidate.scope) is None


def test_safe_point_rejects_a_revoked_basis_before_native_freeze():
    from types import SimpleNamespace
    from pulsara_agent.conversation_kernel.safe_point import ProviderSafePointCoordinator

    owner, candidate = prepared()
    safe = ProviderSafePointCoordinator(repository=object(), guard=object())
    basis = SimpleNamespace(_owner=safe, _state="REVOKED")
    called = []
    with pytest.raises(RuntimeError, match="revoked"):
        safe.install_provider_input_candidate(basis, owner, candidate=candidate,
            execution=object(), before_install=lambda: called.append(True))
    assert called == []
    assert owner.current_view(candidate.scope) is None
