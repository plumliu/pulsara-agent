"""Exact recovery for prepared provider input that never reached ModelStart."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from pulsara_agent.event import (
    ContextCompiledEvent,
    ExistingGenerationPreparationAbandonedEvent,
    ScopedGenerationPreparationAbandonedEvent,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives.frozen import build_frozen_fact
from pulsara_agent.primitives.provider_input import (
    InitialGenerationCommitGuardFact,
    ProviderInputGenerationScopeBindingFact,
    RolloverGenerationCommitGuardFact,
)


@dataclass(frozen=True, slots=True)
class ProviderInputPreparationRecoveryResult:
    preparation_id: str
    outcome: str
    terminal_event_id: str | None


class ProviderInputPreparationRecoveryError(RuntimeError):
    pass


class ProviderInputPreparationRecoveryService:
    """Own stable abandonment after exact Start/append absence is proven."""

    def __init__(self, *, runtime_session, store) -> None:
        self._runtime_session = runtime_session
        self._store = store

    async def abandon_preparation(
        self,
        preparation_id: str,
        *,
        reason: str,
        deadline_monotonic: float | None = None,
    ) -> ProviderInputPreparationRecoveryResult:
        deadline = deadline_monotonic or monotonic() + 30.0
        for _attempt in range(3):
            snapshot = self._store.preparation_snapshot(preparation_id)
            if snapshot is None:
                self._store.discard_staged_resident(preparation_id)
                return ProviderInputPreparationRecoveryResult(
                    preparation_id=preparation_id,
                    outcome="already_resolved",
                    terminal_event_id=None,
                )
            read = await self._runtime_session.context_input_io_service.execute(
                operation_name=(
                    f"provider-input-preparation-recovery:{preparation_id}"
                ),
                operation=lambda: self._read_recovery_basis(snapshot),
                deadline_monotonic=deadline,
            )
            candidate, existing_outcome = self._resolve_candidate(
                snapshot=snapshot,
                read=read,
                reason=reason,
            )
            if existing_outcome is not None:
                return existing_outcome
            assert candidate is not None
            try:
                await self._runtime_session.write_event(
                    candidate,
                    expected_last_sequence=read.through_sequence,
                )
            except BaseException as exc:
                outcome = _resolved_write_outcome(exc)
                if outcome is None:
                    raise
                if outcome.status == "full":
                    return ProviderInputPreparationRecoveryResult(
                        preparation_id=preparation_id,
                        outcome="abandoned",
                        terminal_event_id=candidate.id,
                    )
                if outcome.status == "none":
                    continue
                self._runtime_session.latch_event_commit_outcome_unknown()
                raise ProviderInputPreparationRecoveryError(
                    "provider preparation abandonment requires reconciliation"
                ) from exc
            return ProviderInputPreparationRecoveryResult(
                preparation_id=preparation_id,
                outcome="abandoned",
                terminal_event_id=candidate.id,
            )
        raise ProviderInputPreparationRecoveryError(
            "provider preparation changed during abandonment recovery"
        )

    def recover_incomplete_preparations_sync(
        self,
    ) -> tuple[ProviderInputPreparationRecoveryResult, ...]:
        results = []
        for initial in self._store.active_preparation_snapshots():
            preparation_id = initial.attribution.ownership.preparation_id
            for _attempt in range(3):
                snapshot = self._store.preparation_snapshot(preparation_id)
                if snapshot is None:
                    results.append(
                        ProviderInputPreparationRecoveryResult(
                            preparation_id=preparation_id,
                            outcome="already_resolved",
                            terminal_event_id=None,
                        )
                    )
                    break
                read = self._read_recovery_basis(snapshot)
                candidate, existing_outcome = self._resolve_candidate(
                    snapshot=snapshot,
                    read=read,
                    reason="recovery_confirmed_not_started",
                )
                if existing_outcome is not None:
                    results.append(existing_outcome)
                    break
                assert candidate is not None
                try:
                    self._runtime_session.write_events_from_thread(
                        (candidate,),
                        expected_last_sequence=read.through_sequence,
                    )
                except BaseException as exc:
                    outcome = _resolved_write_outcome(exc)
                    if outcome is None:
                        raise
                    if outcome.status == "full":
                        results.append(
                            ProviderInputPreparationRecoveryResult(
                                preparation_id=preparation_id,
                                outcome="abandoned",
                                terminal_event_id=candidate.id,
                            )
                        )
                        break
                    if outcome.status == "none":
                        continue
                    self._runtime_session.latch_event_commit_outcome_unknown()
                    raise ProviderInputPreparationRecoveryError(
                        "provider preparation recovery commit is untrusted"
                    ) from exc
                results.append(
                    ProviderInputPreparationRecoveryResult(
                        preparation_id=preparation_id,
                        outcome="abandoned",
                        terminal_event_id=candidate.id,
                    )
                )
                break
            else:
                raise ProviderInputPreparationRecoveryError(
                    "provider preparation recovery did not converge"
                )
        return tuple(results)

    def _read_recovery_basis(self, snapshot):
        owner = snapshot.attribution.ownership
        event_ids = (
            snapshot.attribution.context_compiled_event_ref.event_id,
            *owner.stable_companion_event_ids,
            f"model_call_start:{owner.resolved_model_call_id}",
            _abandonment_event_id(owner.preparation_id),
        )
        through_sequence = self._runtime_session.event_log.next_sequence() - 1
        raw = self._runtime_session.event_log.read_raw_events_by_id(
            event_ids,
            deadline_monotonic=monotonic() + 30.0,
        )
        return _RecoveryRead(
            through_sequence=through_sequence,
            events=tuple(
                item.decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY) for item in raw
            ),
        )

    def _resolve_candidate(self, *, snapshot, read, reason):
        owner = snapshot.attribution.ownership
        by_id = {event.id: event for event in read.events}
        abandonment_id = _abandonment_event_id(owner.preparation_id)
        existing_abandonment = by_id.get(abandonment_id)
        if existing_abandonment is not None:
            if not isinstance(
                existing_abandonment,
                (
                    ExistingGenerationPreparationAbandonedEvent,
                    ScopedGenerationPreparationAbandonedEvent,
                ),
            ):
                self._runtime_session.latch_event_commit_outcome_unknown()
                raise ProviderInputPreparationRecoveryError(
                    "provider abandonment event ID belongs to another schema"
                )
            return None, ProviderInputPreparationRecoveryResult(
                preparation_id=owner.preparation_id,
                outcome="already_abandoned",
                terminal_event_id=abandonment_id,
            )
        expected_committed_ids = {
            *owner.stable_companion_event_ids,
            f"model_call_start:{owner.resolved_model_call_id}",
        }
        present = expected_committed_ids.intersection(by_id)
        if present:
            if present != expected_committed_ids:
                self._runtime_session.latch_event_commit_outcome_unknown()
                raise ProviderInputPreparationRecoveryError(
                    "provider start atomic batch is only partially present"
                )
            if self._store.preparation_snapshot(owner.preparation_id) is not None:
                self._runtime_session.latch_event_commit_outcome_unknown()
                raise ProviderInputPreparationRecoveryError(
                    "committed provider start did not consume preparation owner"
                )
            return None, ProviderInputPreparationRecoveryResult(
                preparation_id=owner.preparation_id,
                outcome="started",
                terminal_event_id=None,
            )
        compiled = by_id.get(snapshot.attribution.context_compiled_event_ref.event_id)
        if not isinstance(compiled, ContextCompiledEvent):
            self._runtime_session.latch_event_commit_outcome_unknown()
            raise ProviderInputPreparationRecoveryError(
                "provider preparation ContextCompiled carrier is missing"
            )
        if (
            compiled.prepared_provider_input is None
            or compiled.prepared_provider_input.preparation_ownership != owner
        ):
            self._runtime_session.latch_event_commit_outcome_unknown()
            raise ProviderInputPreparationRecoveryError(
                "provider preparation carrier identity drifted"
            )
        candidate = _build_abandonment_candidate(
            snapshot=snapshot,
            compiled=compiled,
            reason=reason,
        )
        return candidate, None


@dataclass(frozen=True, slots=True)
class _RecoveryRead:
    through_sequence: int
    events: tuple[object, ...]


def _build_abandonment_candidate(*, snapshot, compiled, reason):
    attribution = snapshot.attribution
    owner = attribution.ownership
    binding = snapshot.scope_binding
    if binding.active_preparation_id != owner.preparation_id:
        raise ProviderInputPreparationRecoveryError(
            "provider abandonment predecessor scope is no longer active"
        )
    resulting = build_frozen_fact(
        ProviderInputGenerationScopeBindingFact,
        schema_version="provider_input_generation_scope_binding.v1",
        scope_fingerprint=binding.scope_fingerprint,
        active_generation_id=binding.active_generation_id,
        latest_closed_generation_id=binding.latest_closed_generation_id,
        active_preparation_id=None,
    )
    common = {
        "id": _abandonment_event_id(owner.preparation_id),
        "run_id": compiled.run_id,
        "turn_id": compiled.turn_id,
        "reply_id": compiled.reply_id,
        "created_at": compiled.created_at,
        "metadata": compiled.metadata,
        "preparation_id": owner.preparation_id,
        "preparation_ownership_fingerprint": owner.ownership_fingerprint,
        "context_compiled_event_ref": attribution.context_compiled_event_ref,
        "resolved_model_call_id": owner.resolved_model_call_id,
        "abandonment_reason": reason,
        "predecessor_preparation_attribution_fingerprint": (
            attribution.attribution_fingerprint
        ),
        "predecessor_scope_binding_fingerprint": binding.binding_fingerprint,
        "resulting_scope_binding_fingerprint": resulting.binding_fingerprint,
    }
    prepared = compiled.prepared_provider_input
    assert prepared is not None
    guard = prepared.generation_commit_guard
    if owner.ownership_kind == "existing_append":
        return ExistingGenerationPreparationAbandonedEvent(
            **common,
            generation_id=owner.generation_id,
            expected_committed_core_state_fingerprint=(
                owner.expected_committed_core_state_fingerprint
            ),
        )
    if isinstance(guard, InitialGenerationCommitGuardFact):
        old_generation_id = None
        expected_old_core_state_fingerprint = None
    elif isinstance(guard, RolloverGenerationCommitGuardFact):
        old_generation_id = guard.old_generation_id
        expected_old_core_state_fingerprint = guard.expected_old_core_state_fingerprint
    else:
        raise ProviderInputPreparationRecoveryError(
            "scoped provider abandonment has an incompatible commit guard"
        )
    return ScopedGenerationPreparationAbandonedEvent(
        **common,
        abandonment_kind=owner.ownership_kind,
        scope_fingerprint=owner.scope_fingerprint,
        proposed_generation_id=owner.generation_id,
        old_generation_id=old_generation_id,
        expected_old_core_state_fingerprint=expected_old_core_state_fingerprint,
    )


def _abandonment_event_id(preparation_id: str) -> str:
    return f"provider_input_preparation_abandoned:{preparation_id}"


def _resolved_write_outcome(error: BaseException):
    from pulsara_agent.runtime.session import event_batch_commit_outcome_from_error

    return event_batch_commit_outcome_from_error(error)


__all__ = [
    "ProviderInputPreparationRecoveryError",
    "ProviderInputPreparationRecoveryResult",
    "ProviderInputPreparationRecoveryService",
]
