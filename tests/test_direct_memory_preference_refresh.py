"""Same-turn preference refresh compares complete typed sources, not DTO hashes."""

from types import SimpleNamespace

from pulsara_agent.conversation_kernel.context_sources import build_memory_context_source
from pulsara_agent.conversation_kernel.memory.dispatch import MemoryDispatchSupport
from pulsara_agent.model_input.contracts import (
    ContextSourceAbsenceKind,
    ContextSourceKind,
    ModelInputScopeKind,
)
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope


def test_same_turn_preference_refresh_detects_change_empty_and_cold_epoch() -> None:
    support = MemoryDispatchSupport(
        compiler=None,  # type: ignore[arg-type]
        io_owner=None,  # type: ignore[arg-type]
        memory_projection=None,
        input_reader=None,  # type: ignore[arg-type]
        deadline_factory=None,  # type: ignore[arg-type]
    )
    scope = ProviderInputContinuityScope(
        session_id="session:memory-refresh",
        scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
    )
    empty = build_memory_context_source(
        kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
        texts=None,
        absence_kind=ContextSourceAbsenceKind.EXPLICIT_EMPTY,
    )
    present = build_memory_context_source(
        kind=ContextSourceKind.MEMORY_RESPONSE_PREFERENCE_HEAD,
        texts=('{"items":["concise"]}',),
    )
    first = SimpleNamespace(predecessor_view=SimpleNamespace(epoch_nonce="epoch:1"))
    assert support.preference_changed_since_installed(
        scope=scope, planning=first, desired=empty
    )
    support.note_installed_preference(
        scope=scope,
        epoch_nonce="epoch:1",
        sources=SimpleNamespace(candidates=(), absent_facts=(empty,)),  # type: ignore[arg-type]
    )
    assert not support.preference_changed_since_installed(
        scope=scope, planning=first, desired=empty
    )
    assert support.preference_changed_since_installed(
        scope=scope, planning=first, desired=present
    )
    support.note_installed_preference(
        scope=scope,
        epoch_nonce="epoch:1",
        sources=SimpleNamespace(candidates=(present,), absent_facts=()),  # type: ignore[arg-type]
    )
    assert support.preference_changed_since_installed(
        scope=scope, planning=first, desired=empty
    )
    next_epoch = SimpleNamespace(
        predecessor_view=SimpleNamespace(epoch_nonce="epoch:2")
    )
    assert support.preference_changed_since_installed(
        scope=scope, planning=next_epoch, desired=present
    )
