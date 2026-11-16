"""Exact-scope process-local Hook buffering and independent model messages."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from threading import Lock
from typing import Protocol

from pulsara_agent.hooks.config_parser import DEFAULT_ADDITIONAL_CONTEXT_LIMIT
from pulsara_agent.hooks.contracts import (
    FrozenHookDefinition,
    HookContextEntry,
    HookDiagnostic,
    HookDispatchCausalRef,
    HookDispatchScopeRef,
    HookScopeKind,
    HookSecretScrubber,
    QueuedPromptRef,
)
from pulsara_agent.primitives.context import canonical_json_bytes
from pulsara_agent.model_input.contracts import ModelInputTokenEstimator


MAXIMUM_HOOK_CONTEXT_MESSAGE_BYTES = 1024 * 1024

class HookContextDiagnosticPort(Protocol):
    def offer(self, diagnostics: tuple[HookDiagnostic, ...]) -> None: ...


@dataclass(frozen=True, slots=True)
class HookContextOccurrenceRef:
    scope_kind: HookScopeKind
    session_id: str | None
    event_dispatch_ordinal: int
    causal_identity: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class FrozenHookContextContribution:
    definition: FrozenHookDefinition
    occurrence: HookContextOccurrenceRef
    source_ordinal: int
    text: str
    secret_scrubber: HookSecretScrubber = field(repr=False, compare=False)
    continuation_reason: bool = False
    prompt_reservation: PendingHookContextReservation | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True, slots=True)
class FrozenHookContextBatch:
    scope_kind: HookScopeKind
    session_id: str | None
    contributions: tuple[FrozenHookContextContribution, ...]


@dataclass(frozen=True, slots=True)
class PreparedHookContextMessage:
    text: str
    reservation: "HookContextReservation"


class HookContextReservation:
    __slots__ = ("_owner", "batch", "_settled")

    def __init__(
        self, owner: "HookContextOwner", batch: FrozenHookContextBatch
    ) -> None:
        self._owner = owner
        self.batch = batch
        self._settled = False

    def retire(self) -> None:
        if not self._settled:
            self._settled = True
            self._owner._retire_reservation(self)


class PendingHookContextReservation:
    """Call-local occurrence reservation settled by the lifecycle owner."""

    __slots__ = (
        "_owner",
        "_key",
        "_values",
        "_committed",
        "_prompt_candidate_id",
        "_retired",
        "_prompt_scope",
        "_prompt_causal_ref",
    )

    def __init__(
        self,
        owner: "HookContextOwner",
        key: tuple[int, int, HookScopeKind, str | None],
        values: tuple[FrozenHookContextContribution, ...],
        *,
        prompt_scope: HookDispatchScopeRef | None = None,
        prompt_causal_ref: HookDispatchCausalRef | None = None,
    ) -> None:
        self._owner = owner
        self._key = key
        self._values = values
        self._committed = False
        self._prompt_candidate_id: str | None = None
        self._retired = False
        self._prompt_scope = prompt_scope
        self._prompt_causal_ref = prompt_causal_ref

    def accept_prompt_entries(self, entries: tuple[HookContextEntry, ...]) -> None:
        """Keep synchronous and late outputs on the exact ingress attempt."""

        if self._prompt_scope is None or self._prompt_causal_ref is None:
            raise ValueError("Hook reservation is not an input candidate")
        values = tuple(
            replace(item, prompt_reservation=self)
            for item in _freeze_contributions(
                scope=self._prompt_scope,
                causal_ref=self._prompt_causal_ref,
                entries=entries,
                continuation_reason=False,
            )
        )
        self._owner._offer_prompt_values(self, values)

    def commit(self) -> None:
        if self._prompt_scope is not None:
            self._owner._activate_prompt(self)
            return
        if self._retired or self._committed:
            return
        self._committed = True
        self._owner._commit_pending(self._key, self._values)

    def commit_prompt_bound(self, prompt_candidate_id: str) -> None:
        """Keep a queued-prompt contribution outside every live turn bucket."""

        if not prompt_candidate_id:
            raise ValueError("queued Hook context requires an exact prompt candidate")
        if self._prompt_scope is None:
            raise ValueError("queued Hook context requires an input reservation")
        self._owner._bind_prompt(self, prompt_candidate_id)

    def retire(self) -> None:
        if self._prompt_scope is not None:
            self._owner._retire_prompt(self)
            return
        if self._retired:
            return
        self._retired = True
        if self._committed:
            self._owner._remove_pending_values(self._key, self._values)


class _NullDiagnosticPort:
    def offer(self, diagnostics: tuple[HookDiagnostic, ...]) -> None:
        del diagnostics


class HookContextOwner:
    """One Host-owned buffer; never a canonical or replay authority."""

    def __init__(
        self, diagnostic_port: HookContextDiagnosticPort | None = None
    ) -> None:
        self._diagnostics = diagnostic_port or _NullDiagnosticPort()
        self._lock = Lock()
        self._pending: dict[
            tuple[int, int, HookScopeKind, str | None],
            list[FrozenHookContextContribution],
        ] = {}
        self._prompt_bound: dict[
            tuple[tuple[int, int, HookScopeKind, str | None], str],
            list[PendingHookContextReservation],
        ] = {}
        self._live_scopes: set[tuple[int, int, HookScopeKind, str | None]] = set()
        self._reservations: set[HookContextReservation] = set()
        self._closed = False

    def register_scope(self, scope: HookDispatchScopeRef) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Hook context owner is closed")
            self._live_scopes.add(_scope_key(scope))

    def accept_sync(
        self,
        *,
        scope: HookDispatchScopeRef,
        causal_ref: HookDispatchCausalRef,
        entries: tuple[HookContextEntry, ...],
    ) -> None:
        reservation = self.prepare_sync(
            scope=scope, causal_ref=causal_ref, entries=entries
        )
        if reservation is not None:
            reservation.commit()

    def prepare_prompt(
        self, *, scope: HookDispatchScopeRef, causal_ref: HookDispatchCausalRef
    ) -> PendingHookContextReservation:
        reservation = PendingHookContextReservation(
            self, _scope_key(scope), (),
            prompt_scope=scope, prompt_causal_ref=causal_ref,
        )
        if isinstance(causal_ref, QueuedPromptRef):
            reservation.commit_prompt_bound(causal_ref.queue_item_id)
        return reservation

    def prepare_sync(
        self,
        *,
        scope: HookDispatchScopeRef,
        causal_ref: HookDispatchCausalRef,
        entries: tuple[HookContextEntry, ...],
    ) -> PendingHookContextReservation | None:
        values = _freeze_contributions(
            scope=scope,
            causal_ref=causal_ref,
            entries=entries,
            continuation_reason=False,
        )
        if not values:
            return None
        return PendingHookContextReservation(self, _scope_key(scope), values)

    async def accept_background(
        self,
        *,
        scope: HookDispatchScopeRef,
        causal_ref: HookDispatchCausalRef,
        entry: HookContextEntry,
    ) -> None:
        self._accept(scope=scope, causal_ref=causal_ref, entries=(entry,))

    def accept_continuation(
        self,
        *,
        scope: HookDispatchScopeRef,
        causal_ref: HookDispatchCausalRef,
        source_entry: HookContextEntry,
        reason: str | None,
    ) -> None:
        text = reason or "Lifecycle Hook requested one additional pass."
        self._accept(
            scope=scope,
            causal_ref=causal_ref,
            entries=(
                HookContextEntry(
                    source_entry.definition,
                    source_entry.source_ordinal,
                    source_entry.event_dispatch_ordinal,
                    text,
                    source_entry.secret_scrubber,
                ),
            ),
            continuation_reason=True,
        )

    def freeze_for_target(
        self,
        *,
        scope_kind: str,
        session_id: str | None,
        estimator: ModelInputTokenEstimator,
    ) -> PreparedHookContextMessage | None:
        expected_kind = (
            HookScopeKind.ROOT if scope_kind == "ROOT" else HookScopeKind.SUBAGENT
        )
        with self._lock:
            matching_keys = tuple(
                key
                for key in self._pending
                if key[2] is expected_kind and key[3] == session_id
            )
            pending = [
                item for key in matching_keys for item in self._pending.pop(key, ())
            ]
        if not pending:
            return None
        pending.sort(
            key=lambda item: (
                item.occurrence.event_dispatch_ordinal,
                item.source_ordinal,
                item.definition.source_local_definition_ordinal,
            )
        )
        admitted: list[FrozenHookContextContribution] = []
        omitted: list[HookDiagnostic] = []
        for item in pending:
            try:
                item = replace(item, text=item.secret_scrubber.scrub_text(item.text))
            except (UnicodeError, ValueError):
                omitted.append(
                    HookDiagnostic(
                        "HOOK_CONTEXT_SECRET_SCRUB_FAILED",
                        "Hook context was omitted because its secret boundary failed",
                        event_type=item.definition.event_type,
                        source_label=item.definition.provenance.display_label,
                    )
                )
                continue
            threshold = (
                DEFAULT_ADDITIONAL_CONTEXT_LIMIT
                if item.continuation_reason
                else item.definition.additional_context_limit
            )
            if threshold and estimator.estimate_text(item.text) > threshold:
                omitted.append(
                    HookDiagnostic(
                        "HOOK_CONTEXT_HANDLER_LIMIT_OMITTED",
                        "Hook context exceeded its handler token threshold",
                        event_type=item.definition.event_type,
                        source_label=item.definition.provenance.display_label,
                        event_dispatch_ordinal=item.occurrence.event_dispatch_ordinal,
                        source_ordinal=item.source_ordinal,
                        definition_ordinal=(
                            item.definition.source_local_definition_ordinal
                        ),
                    )
                )
                continue
            admitted.append(item)
        while admitted:
            batch = FrozenHookContextBatch(
                expected_kind, session_id, tuple(admitted)
            )
            text = render_hook_context_batch(batch)
            if len(text.encode("utf-8")) <= MAXIMUM_HOOK_CONTEXT_MESSAGE_BYTES:
                reservation = HookContextReservation(self, batch)
                with self._lock:
                    self._reservations.add(reservation)
                if omitted:
                    self._diagnostics.offer(tuple(omitted))
                return PreparedHookContextMessage(text, reservation)
            dropped = admitted.pop(0)
            omitted.append(
                HookDiagnostic(
                    "HOOK_CONTEXT_BUFFER_OMITTED",
                    "oldest complete Hook context entry exceeded the source bound",
                    event_type=dropped.definition.event_type,
                    source_label=dropped.definition.provenance.display_label,
                )
            )
        if omitted:
            self._diagnostics.offer(tuple(omitted))
        return None

    def activate_prompt_candidate(
        self,
        *,
        scope: HookDispatchScopeRef,
        prompt_candidate_id: str,
    ) -> tuple[PendingHookContextReservation, ...]:
        """Move one queued admission into its now-exclusive live scope."""

        key = _scope_key(scope)
        prompt_key = (key, prompt_candidate_id)
        with self._lock:
            reservations = tuple(self._prompt_bound.pop(prompt_key, ()))
            for reservation in reservations:
                self._activate_prompt_locked(reservation)
            return reservations

    def retire_prompt_candidate(
        self,
        *,
        scope: HookDispatchScopeRef,
        prompt_candidate_id: str,
    ) -> None:
        """Drop a process-local candidate that can no longer reach a provider."""

        key = _scope_key(scope)
        with self._lock:
            for reservation in tuple(self._prompt_bound.pop((key, prompt_candidate_id), ())):
                self._retire_prompt_locked(reservation)

    def retire_scope(self, scope: HookDispatchScopeRef) -> None:
        key = _scope_key(scope)
        with self._lock:
            self._live_scopes.discard(key)
            self._pending.pop(key, None)
            prompt_keys = tuple(
                prompt_key for prompt_key in self._prompt_bound if prompt_key[0] == key
            )
            for prompt_key in prompt_keys:
                for reservation in self._prompt_bound.pop(prompt_key, ()):
                    self._retire_prompt_locked(reservation)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._live_scopes.clear()
            self._pending.clear()
            for bucket in self._prompt_bound.values():
                for reservation in bucket:
                    reservation._retired = True
                    reservation._values = ()
            self._prompt_bound.clear()
            reservations = tuple(self._reservations)
            self._reservations.clear()
        for reservation in reservations:
            reservation._settled = True

    def _accept(
        self,
        *,
        scope: HookDispatchScopeRef,
        causal_ref: HookDispatchCausalRef,
        entries: tuple[HookContextEntry, ...],
        continuation_reason: bool = False,
    ) -> None:
        key = _scope_key(scope)
        values = _freeze_contributions(
            scope=scope,
            causal_ref=causal_ref,
            entries=entries,
            continuation_reason=continuation_reason,
        )
        self._commit_pending(key, values)

    def _commit_pending(
        self,
        key: tuple[int, int, HookScopeKind, str | None],
        values: tuple[FrozenHookContextContribution, ...],
    ) -> None:
        if not values:
            return
        with self._lock:
            if self._closed or key not in self._live_scopes:
                return
            self._append_pending_locked(key, values)

    def _offer_prompt_values(
        self,
        reservation: PendingHookContextReservation,
        values: tuple[FrozenHookContextContribution, ...],
    ) -> None:
        with self._lock:
            if (
                self._closed or reservation._retired
                or reservation._key not in self._live_scopes
            ):
                return
            if reservation._committed:
                self._append_pending_locked(reservation._key, values)
            else:
                bucket = list((*reservation._values, *values))
                self._bound_bucket_locked(reservation._key, bucket)
                reservation._values = tuple(bucket)

    def _bind_prompt(
        self, reservation: PendingHookContextReservation, prompt_candidate_id: str
    ) -> None:
        with self._lock:
            if (
                self._closed or reservation._retired or reservation._committed
                or reservation._key not in self._live_scopes
            ):
                return
            if reservation._prompt_candidate_id is not None:
                if reservation._prompt_candidate_id != prompt_candidate_id:
                    raise ValueError("Hook input candidate identity changed")
                return
            reservation._prompt_candidate_id = prompt_candidate_id
            self._prompt_bound.setdefault(
                (reservation._key, prompt_candidate_id), []
            ).append(reservation)

    def _activate_prompt(self, reservation: PendingHookContextReservation) -> None:
        with self._lock:
            self._activate_prompt_locked(reservation)

    def _activate_prompt_locked(self, reservation: PendingHookContextReservation) -> None:
        if (
            self._closed or reservation._retired or reservation._committed
            or reservation._key not in self._live_scopes
        ):
            return
        reservation._committed = True
        self._append_pending_locked(reservation._key, reservation._values)
        reservation._values = ()

    def _retire_prompt(self, reservation: PendingHookContextReservation) -> None:
        with self._lock:
            self._retire_prompt_locked(reservation)
            prompt_key = (reservation._key, reservation._prompt_candidate_id)
            bucket = self._prompt_bound.get(prompt_key)
            if bucket is not None:
                bucket[:] = [item for item in bucket if item is not reservation]
                if not bucket:
                    self._prompt_bound.pop(prompt_key, None)

    def _retire_prompt_locked(self, reservation: PendingHookContextReservation) -> None:
        reservation._retired = True
        reservation._values = ()
        bucket = self._pending.get(reservation._key)
        if bucket is not None:
            bucket[:] = [item for item in bucket if item.prompt_reservation is not reservation]
            if not bucket:
                self._pending.pop(reservation._key, None)

    def _append_pending_locked(
        self, key: tuple[int, int, HookScopeKind, str | None],
        values: tuple[FrozenHookContextContribution, ...],
    ) -> None:
        if values:
            bucket = self._pending.setdefault(key, [])
            bucket.extend(values)
            self._bound_bucket_locked(key, bucket)

    def _bound_bucket_locked(
        self, key: tuple[int, int, HookScopeKind, str | None],
        bucket: list[FrozenHookContextContribution],
    ) -> None:
        while bucket:
            batch = FrozenHookContextBatch(key[2], key[3], tuple(bucket))
            text = render_hook_context_batch(batch)
            if len(text.encode("utf-8")) <= MAXIMUM_HOOK_CONTEXT_MESSAGE_BYTES:
                break
            bucket.pop(0)
            self._diagnostics.offer((HookDiagnostic(
                "HOOK_CONTEXT_BUFFER_OMITTED",
                "oldest complete Hook context entry was dropped",
            ),))

    def _retire_reservation(self, reservation: HookContextReservation) -> None:
        with self._lock:
            self._reservations.discard(reservation)

    def _remove_pending_values(
        self,
        key: tuple[int, int, HookScopeKind, str | None],
        values: tuple[FrozenHookContextContribution, ...],
    ) -> None:
        identities = {id(item) for item in values}
        with self._lock:
            bucket = self._pending.get(key)
            if bucket is None:
                return
            bucket[:] = [item for item in bucket if id(item) not in identities]
            if not bucket:
                self._pending.pop(key, None)


def render_hook_context_batch(batch: FrozenHookContextBatch) -> str:
    items = []
    for contribution in batch.contributions:
        text = contribution.text
        if contribution.continuation_reason:
            text = "This continuation was requested by a Hook. Reason: " + text
        text = f"Hook source: {contribution.definition.provenance.display_label}\n{text}"
        for item in batch.contributions:
            text = item.secret_scrubber.scrub_text(text)
        if text:
            items.append({"event":contribution.definition.event_type.external_name,"text":text})
    if not items:
        return ""
    return canonical_json_bytes({"pulsara_hook_context":items}).decode("utf-8")




def _scope_key(
    scope: HookDispatchScopeRef,
) -> tuple[int, int, HookScopeKind, str | None]:
    return (
        id(scope.host_session_owner),
        id(scope.workspace_owner),
        scope.kind,
        scope.session_id,
    )


def _freeze_contributions(
    *,
    scope: HookDispatchScopeRef,
    causal_ref: HookDispatchCausalRef,
    entries: tuple[HookContextEntry, ...],
    continuation_reason: bool,
) -> tuple[FrozenHookContextContribution, ...]:
    return tuple(
        FrozenHookContextContribution(
            entry.definition,
            HookContextOccurrenceRef(
                scope.kind,
                scope.session_id,
                entry.event_dispatch_ordinal,
                _causal_identity(causal_ref),
            ),
            entry.source_ordinal,
            entry.text,
            entry.secret_scrubber,
            continuation_reason,
        )
        for entry in entries
        if entry.text
    )


def _causal_identity(causal_ref: HookDispatchCausalRef) -> tuple[object, ...]:
    values: list[object] = [type(causal_ref).__name__]
    for name in getattr(causal_ref, "__dataclass_fields__", {}):
        value = getattr(causal_ref, name)
        if isinstance(value, (str, int, bool, type(None))):
            values.append((name, value))
        else:
            values.append((name, "process-local", id(value)))
    return tuple(values)



__all__ = [
    "FrozenHookContextBatch",
    "FrozenHookContextContribution",
    "HookContextOccurrenceRef",
    "HookContextOwner",
    "PendingHookContextReservation",
    "HookContextReservation",
    "PreparedHookContextMessage",
    "render_hook_context_batch",
]
