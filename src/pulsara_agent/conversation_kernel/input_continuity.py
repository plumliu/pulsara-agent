"""Host-owned, process-local provider-input continuity state.

This owner is deliberately small.  It linearizes a prepared immutable input
against the currently installed prefix and returns an opaque one-shot permit.
It never reads or writes PostgreSQL and never opens a provider transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from uuid import uuid4

from pulsara_agent.model_input.continuity import (
    FrozenProviderInputAppendPlanningInput,
    FrozenProviderInputEpochView,
    PreparedProviderInputAppendCandidate,
    ProcessLocalCanonicalFrontier,
    ProcessLocalProviderInputInstallPermit,
    ProviderInputAdmissionPredecessorKind,
    ProviderInputContinuityScope,
    ProviderInputDispatchAnchor,
    provider_input_append_planning_fingerprint,
    provider_input_logical_utf8_bytes,
    provider_input_prefix_fingerprint,
)


class ProviderInputContinuityConflict(RuntimeError):
    pass


_INSTALL_AUTHORITY_SEAL = object()


class ProcessLocalProviderInputInstallAuthority:
    """Narrow verifier for permits issued by one Host continuity owner.

    The permit DTO remains provider-neutral, but matching public fields are
    not authority.  Only the exact object installed by this owner can be
    consumed, exactly once, immediately before provider open.
    """

    __slots__ = ("_owner",)

    def __init__(
        self,
        owner: "HostProviderInputContinuityOwner",
        *,
        _seal: object,
    ) -> None:
        if _seal is not _INSTALL_AUTHORITY_SEAL:
            raise TypeError("provider-input install authority is Host-owned")
        self._owner = owner

    def consume(
        self,
        permit: ProcessLocalProviderInputInstallPermit,
        *,
        candidate_fingerprint: str,
        execution_fingerprint: str,
    ) -> None:
        self._owner._consume_install_permit(
            permit,
            candidate_fingerprint=candidate_fingerprint,
            execution_fingerprint=execution_fingerprint,
        )


MAXIMUM_ROOT_SCOPES = 1
MAXIMUM_CHILD_SCOPES = 4
MAXIMUM_HOST_INSTALLED_BYTES = 320 << 20
MAXIMUM_HOST_INSTALLED_AND_PREPARED_BYTES = 640 << 20


class _SlotState(StrEnum):
    EMPTY = "EMPTY"
    PREPARED = "PREPARED"
    INSTALLED = "INSTALLED"
    CLOSED = "CLOSED"


@dataclass(slots=True)
class _Slot:
    state: _SlotState = _SlotState.EMPTY
    installed: FrozenProviderInputEpochView | None = None
    prepared: PreparedProviderInputAppendCandidate | None = None


class HostProviderInputContinuityOwner:
    """Own at most one ROOT and four child prefix epochs for one Host."""

    def __init__(
        self,
        *,
        session_id: str,
        maximum_child_scopes: int = MAXIMUM_CHILD_SCOPES,
    ) -> None:
        if not session_id or maximum_child_scopes < 1:
            raise ValueError("provider-input continuity owner bound is invalid")
        self._session_id = session_id
        self._maximum_child_scopes = maximum_child_scopes
        self._lock = RLock()
        self._slots: dict[ProviderInputContinuityScope, _Slot] = {}
        self._issued_permits: dict[str, ProcessLocalProviderInputInstallPermit] = {}
        self._install_authority = ProcessLocalProviderInputInstallAuthority(
            self,
            _seal=_INSTALL_AUTHORITY_SEAL,
        )
        self._closed = False

    @property
    def install_authority(self) -> ProcessLocalProviderInputInstallAuthority:
        return self._install_authority

    def freeze_planning_input(
        self,
        *,
        scope: ProviderInputContinuityScope,
        canonical_frontier: ProcessLocalCanonicalFrontier,
        dispatch_anchor: ProviderInputDispatchAnchor,
    ) -> FrozenProviderInputAppendPlanningInput:
        self._require_scope(scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(scope)
            if slot is None:
                self._admit_scope_locked(scope)
                slot = self._slots[scope]
            if slot.state is _SlotState.PREPARED:
                raise ProviderInputContinuityConflict(
                    "provider-input scope already owns a prepared candidate"
                )
            if slot.state is _SlotState.CLOSED:
                raise ProviderInputContinuityConflict("provider-input scope is closed")
            predecessor_view = slot.installed
            if predecessor_view is None:
                predecessor = ProviderInputAdmissionPredecessorKind.EMPTY
                delta = canonical_frontier.ordered_item_fingerprints
            else:
                predecessor = ProviderInputAdmissionPredecessorKind.INSTALLED
                old = predecessor_view.canonical_frontier
                if (
                    old.context_base_semantic_identity
                    == canonical_frontier.context_base_semantic_identity
                    and canonical_frontier.ordered_item_fingerprints[
                        : len(old.ordered_item_fingerprints)
                    ]
                    == old.ordered_item_fingerprints
                ):
                    delta = canonical_frontier.ordered_item_fingerprints[
                        len(old.ordered_item_fingerprints) :
                    ]
                else:
                    # A legal context-base reset must be evaluated by the pure
                    # compiler using the frozen compatibility fact.  Planning
                    # records the complete rematerialization input; it never
                    # silently repairs a same-base prefix rewrite.
                    delta = canonical_frontier.ordered_item_fingerprints
            fingerprint = provider_input_append_planning_fingerprint(
                scope=scope,
                predecessor=predecessor,
                predecessor_view=predecessor_view,
                dispatch_anchor=dispatch_anchor,
                canonical_delta_fingerprints=delta,
            )
            return FrozenProviderInputAppendPlanningInput(
                scope=scope,
                predecessor=predecessor,
                predecessor_view=predecessor_view,
                dispatch_anchor=dispatch_anchor,
                canonical_delta_fingerprints=delta,
                planning_fingerprint=fingerprint,
            )

    def register(self, candidate: PreparedProviderInputAppendCandidate) -> None:
        self._require_scope(candidate.scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(candidate.scope)
            if slot is None or slot.state is _SlotState.CLOSED:
                raise ProviderInputContinuityConflict("continuity scope is unavailable")
            if slot.state is _SlotState.PREPARED:
                raise ProviderInputContinuityConflict(
                    "continuity scope already owns a prepared candidate"
                )
            installed = slot.installed
            candidate_bytes = provider_input_logical_utf8_bytes(
                system_prompt=candidate.resulting_compiled_input.system_prompt,
                tools=candidate.resulting_compiled_input.tools,
                messages=candidate.resulting_compiled_input.messages,
            )
            if candidate_bytes > (64 << 20):
                raise ProviderInputContinuityConflict(
                    "provider-input epoch exceeds its logical bound"
                )
            installed_bytes = sum(
                0 if current.installed is None else current.installed.logical_utf8_bytes
                for current in self._slots.values()
            )
            current_bytes = 0 if installed is None else installed.logical_utf8_bytes
            if (
                installed_bytes - current_bytes + candidate_bytes
                > MAXIMUM_HOST_INSTALLED_BYTES
            ):
                raise ProviderInputContinuityConflict(
                    "Host provider-input resident bound is exhausted"
                )
            prepared_bytes = sum(
                provider_input_logical_utf8_bytes(
                    system_prompt=current.prepared.resulting_compiled_input.system_prompt,
                    tools=current.prepared.resulting_compiled_input.tools,
                    messages=current.prepared.resulting_compiled_input.messages,
                )
                for current in self._slots.values()
                if current.prepared is not None
            )
            if (
                installed_bytes + prepared_bytes + candidate_bytes
                > MAXIMUM_HOST_INSTALLED_AND_PREPARED_BYTES
            ):
                raise ProviderInputContinuityConflict(
                    "Host provider-input aggregate bound is exhausted"
                )
            if installed is None:
                if (
                    candidate.expected_epoch_revision != 0
                    or candidate.predecessor_prefix_fingerprint is not None
                ):
                    raise ProviderInputContinuityConflict(
                        "initial append candidate has a predecessor"
                    )
            else:
                if (
                    candidate.expected_epoch_revision != installed.epoch_revision
                    or candidate.predecessor_prefix_fingerprint
                    != installed.semantic_prefix_fingerprint
                ):
                    raise ProviderInputContinuityConflict(
                        "successor append candidate is stale"
                    )
                if candidate.compatibility == installed.compatibility:
                    if (
                        candidate.reset_reason is not None
                        or candidate.epoch_nonce != installed.epoch_nonce
                    ):
                        raise ProviderInputContinuityConflict(
                            "compatible successor changed epoch identity"
                        )
                    installed.canonical_frontier.require_prefix_of(
                        candidate.resulting_canonical_frontier
                    )
                    compiled = candidate.resulting_compiled_input
                    if compiled.system_prompt != installed.system_prompt:
                        raise ProviderInputContinuityConflict(
                            "successor rewrote the installed system root"
                        )
                    if compiled.tools != installed.tools:
                        raise ProviderInputContinuityConflict(
                            "successor rewrote the installed tool surface"
                        )
                    if (
                        compiled.messages[: len(installed.messages)]
                        != installed.messages
                    ):
                        raise ProviderInputContinuityConflict(
                            "successor rewrote the installed message prefix"
                        )
                else:
                    if candidate.reset_reason is None:
                        raise ProviderInputContinuityConflict(
                            "incompatible successor lacks an explicit reset reason"
                        )
                    if candidate.epoch_nonce == installed.epoch_nonce:
                        raise ProviderInputContinuityConflict(
                            "reset successor reused the installed epoch identity"
                        )
                    if (
                        installed.canonical_frontier.context_base_semantic_identity
                        == candidate.resulting_canonical_frontier.context_base_semantic_identity
                    ):
                        installed.canonical_frontier.require_prefix_of(
                            candidate.resulting_canonical_frontier
                        )
            slot.prepared = candidate
            slot.state = _SlotState.PREPARED

    def install(
        self,
        *,
        candidate_fingerprint: str,
        execution_fingerprint: str,
    ) -> ProcessLocalProviderInputInstallPermit:
        with self._lock:
            matches = tuple(
                (scope, slot)
                for scope, slot in self._slots.items()
                if slot.prepared is not None
                and slot.prepared.candidate_fingerprint == candidate_fingerprint
            )
            if len(matches) != 1:
                raise ProviderInputContinuityConflict(
                    "prepared append candidate is absent or ambiguous"
                )
            scope, slot = matches[0]
            candidate = slot.prepared
            assert candidate is not None
            compiled = candidate.resulting_compiled_input
            revision = candidate.expected_epoch_revision + 1
            view = FrozenProviderInputEpochView(
                scope=scope,
                epoch_nonce=candidate.epoch_nonce,
                epoch_revision=revision,
                compatibility=candidate.compatibility,
                system_prompt=compiled.system_prompt,
                tools=compiled.tools,
                messages=compiled.messages,
                canonical_frontier=candidate.resulting_canonical_frontier,
                source_heads=candidate.resulting_source_heads,
                final_estimate=compiled.final_estimate,
                logical_utf8_bytes=provider_input_logical_utf8_bytes(
                    system_prompt=compiled.system_prompt,
                    tools=compiled.tools,
                    messages=compiled.messages,
                ),
                semantic_prefix_fingerprint=provider_input_prefix_fingerprint(
                    system_prompt=compiled.system_prompt,
                    tools=compiled.tools,
                    messages=compiled.messages,
                ),
            )
            slot.installed = view
            slot.prepared = None
            slot.state = _SlotState.INSTALLED
            permit = ProcessLocalProviderInputInstallPermit(
                scope=scope,
                epoch_nonce=view.epoch_nonce,
                epoch_revision=view.epoch_revision,
                candidate_fingerprint=candidate_fingerprint,
                execution_fingerprint=execution_fingerprint,
                permit_nonce=f"provider-input-permit:{uuid4().hex}",
            )
            self._issued_permits[permit.permit_nonce] = permit
            return permit

    def _consume_install_permit(
        self,
        permit: ProcessLocalProviderInputInstallPermit,
        *,
        candidate_fingerprint: str,
        execution_fingerprint: str,
    ) -> None:
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            issued = self._issued_permits.get(permit.permit_nonce)
            if (
                issued is not permit
                or permit.candidate_fingerprint != candidate_fingerprint
                or permit.execution_fingerprint != execution_fingerprint
            ):
                raise ProviderInputContinuityConflict(
                    "provider-input install permit was not issued for this execution"
                )
            del self._issued_permits[permit.permit_nonce]

    def discard(self, candidate_fingerprint: str) -> None:
        with self._lock:
            matches = tuple(
                slot
                for slot in self._slots.values()
                if slot.prepared is not None
                and slot.prepared.candidate_fingerprint == candidate_fingerprint
            )
            if len(matches) != 1:
                raise ProviderInputContinuityConflict(
                    "prepared append discard does not exact-join"
                )
            slot = matches[0]
            slot.prepared = None
            slot.state = (
                _SlotState.EMPTY if slot.installed is None else _SlotState.INSTALLED
            )

    def current_view(
        self, scope: ProviderInputContinuityScope
    ) -> FrozenProviderInputEpochView | None:
        self._require_scope(scope)
        with self._lock:
            slot = self._slots.get(scope)
            return None if slot is None else slot.installed

    def discard_scope(self, scope: ProviderInputContinuityScope) -> None:
        self._require_scope(scope)
        with self._lock:
            slot = self._slots.pop(scope, None)
            if slot is not None:
                slot.installed = None
                slot.prepared = None
                slot.state = _SlotState.CLOSED
            self._issued_permits = {
                nonce: permit
                for nonce, permit in self._issued_permits.items()
                if permit.scope != scope
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for slot in self._slots.values():
                slot.installed = None
                slot.prepared = None
                slot.state = _SlotState.CLOSED
            self._slots.clear()
            self._issued_permits.clear()

    def _require_scope(self, scope: ProviderInputContinuityScope) -> None:
        if scope.session_id != self._session_id:
            raise ProviderInputContinuityConflict(
                "continuity scope belongs to another session"
            )

    def _admit_scope_locked(self, scope: ProviderInputContinuityScope) -> None:
        root_count = sum(item.scope_kind.value == "ROOT" for item in self._slots)
        child_count = len(self._slots) - root_count
        if scope.scope_kind.value == "ROOT" and root_count >= MAXIMUM_ROOT_SCOPES:
            raise ProviderInputContinuityConflict(
                "ROOT continuity scope already exists"
            )
        if (
            scope.scope_kind.value != "ROOT"
            and child_count >= self._maximum_child_scopes
        ):
            raise ProviderInputContinuityConflict(
                "child continuity scope capacity is exhausted"
            )
        self._slots[scope] = _Slot()


__all__ = [
    "HostProviderInputContinuityOwner",
    "MAXIMUM_CHILD_SCOPES",
    "MAXIMUM_HOST_INSTALLED_AND_PREPARED_BYTES",
    "MAXIMUM_HOST_INSTALLED_BYTES",
    "MAXIMUM_ROOT_SCOPES",
    "ProviderInputContinuityConflict",
    "ProcessLocalProviderInputInstallAuthority",
]
