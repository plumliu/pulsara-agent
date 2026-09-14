"""Host-owned, process-local provider-input continuity state.

This owner is deliberately small.  It linearizes a prepared immutable input
against the currently installed prefix and returns an opaque one-shot permit.
It never reads or writes PostgreSQL and never opens a provider transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field, replace
from enum import StrEnum
from threading import RLock
from uuid import uuid4

from pulsara_agent.capability.contracts import FrozenToolCapabilityExposurePlan
from pulsara_agent.model_input.continuity import (
    FrozenProviderInputAppendPlanningInput,
    FrozenProviderInputEpochView,
    PreparedProviderInputAppendCandidate,
    ProcessLocalCanonicalFrontier,
    ProcessLocalProviderInputInstallPermit,
    ProviderInputAdmissionPredecessorKind,
    ProviderInputContinuityScope,
    ProviderInputDispatchAnchor,
    provider_input_logical_bytes,
    provider_input_prefix_fingerprint,
)
from pulsara_agent.llm.provider_replay import ProviderAssistantReplayFragment
from pulsara_agent.llm.request import FrozenProviderWireInputPlan
from pulsara_agent.model_input.contracts import (
    compiled_message_placements_fingerprint,
)


class ProviderInputContinuityConflict(RuntimeError):
    pass


_INSTALL_AUTHORITY_SEAL = object()
_ASSISTANT_REPLAY_RESERVATION_SEAL = object()


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
        candidate: PreparedProviderInputAppendCandidate,
        execution: object,
    ) -> None:
        self._owner._consume_install_permit(
            permit,
            candidate=candidate,
            execution=execution,
        )

    def require_registered_plan(
        self,
        *,
        candidate: PreparedProviderInputAppendCandidate,
        wire_input_plan: FrozenProviderWireInputPlan,
        tool_exposure_plan: FrozenToolCapabilityExposurePlan,
    ) -> None:
        """Prove that preflight owns the exact plan registered for this CAS."""

        self._owner._require_registered_plan(
            candidate=candidate,
            wire_input_plan=wire_input_plan,
            tool_exposure_plan=tool_exposure_plan,
        )


MAXIMUM_ROOT_SCOPES = 1
MAXIMUM_CHILD_SCOPES = 4
MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES = 64 << 20
MAXIMUM_HOST_INSTALLED_BYTES = 320 << 20
MAXIMUM_HOST_INSTALLED_AND_PREPARED_BYTES = 640 << 20


@dataclass(frozen=True, slots=True, init=False)
class ProcessLocalAssistantReplayFragmentReservation:
    """Opaque Host-owned capacity claim for one completed replay fragment."""

    scope: ProviderInputContinuityScope
    epoch_nonce: str
    epoch_revision: int
    fragment: ProviderAssistantReplayFragment
    additional_resident_bytes: int
    reservation_nonce: str
    _already_bound: bool

    def __init__(
        self,
        *,
        scope: ProviderInputContinuityScope,
        epoch_nonce: str,
        epoch_revision: int,
        fragment: ProviderAssistantReplayFragment,
        additional_resident_bytes: int,
        already_bound: bool,
        reservation_nonce: str,
        _seal: object,
    ) -> None:
        if _seal is not _ASSISTANT_REPLAY_RESERVATION_SEAL:
            raise TypeError("assistant replay reservation is Host-owned")
        if (
            epoch_revision < 1
            or additional_resident_bytes < 0
            or not reservation_nonce
        ):
            raise ValueError("assistant replay reservation identity is invalid")
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "epoch_nonce", epoch_nonce)
        object.__setattr__(self, "epoch_revision", epoch_revision)
        object.__setattr__(self, "fragment", fragment)
        object.__setattr__(
            self, "additional_resident_bytes", additional_resident_bytes
        )
        object.__setattr__(self, "reservation_nonce", reservation_nonce)
        object.__setattr__(self, "_already_bound", already_bound)


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
    replay_reservation: ProcessLocalAssistantReplayFragmentReservation | None = None


@dataclass(frozen=True, slots=True)
class _IssuedProviderInputInstallPermit:
    permit: ProcessLocalProviderInputInstallPermit
    candidate: PreparedProviderInputAppendCandidate = dataclass_field(repr=False)
    execution: object = dataclass_field(repr=False)


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
        self._issued_permits: dict[str, _IssuedProviderInputInstallPermit] = {}
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
        return self._freeze_planning_input(
            scope=scope,
            canonical_frontier=canonical_frontier,
            dispatch_anchor=dispatch_anchor,
            detached_destination_projection=False,
        )

    def freeze_destination_projection_planning_input(
        self,
        *,
        scope: ProviderInputContinuityScope,
        canonical_frontier: ProcessLocalCanonicalFrontier,
        dispatch_anchor: ProviderInputDispatchAnchor,
    ) -> FrozenProviderInputAppendPlanningInput:
        """Freeze a non-installable Tier-3 projection outside A's prefix."""

        return self._freeze_planning_input(
            scope=scope,
            canonical_frontier=canonical_frontier,
            dispatch_anchor=dispatch_anchor,
            detached_destination_projection=True,
        )

    def _freeze_planning_input(
        self,
        *,
        scope: ProviderInputContinuityScope,
        canonical_frontier: ProcessLocalCanonicalFrontier,
        dispatch_anchor: ProviderInputDispatchAnchor,
        detached_destination_projection: bool,
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
            if slot.replay_reservation is not None:
                raise ProviderInputContinuityConflict(
                    "provider-input scope is settling an assistant replay fragment"
                )
            if slot.state is _SlotState.CLOSED:
                raise ProviderInputContinuityConflict("provider-input scope is closed")
            predecessor_view = (
                None if detached_destination_projection else slot.installed
            )
            return _planning_input_from_predecessor(
                scope=scope,
                predecessor_view=predecessor_view,
                canonical_frontier=canonical_frontier,
                dispatch_anchor=dispatch_anchor,
            )

    def freeze_planning_sibling(
        self,
        *,
        basis: FrozenProviderInputAppendPlanningInput,
        canonical_frontier: ProcessLocalCanonicalFrontier,
        dispatch_anchor: ProviderInputDispatchAnchor,
    ) -> FrozenProviderInputAppendPlanningInput:
        """Build a variant against the exact predecessor frozen by ``basis``."""

        self._require_scope(basis.scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(basis.scope)
            if (
                slot is None
                or slot.state in {_SlotState.CLOSED, _SlotState.PREPARED}
                or slot.replay_reservation is not None
                or slot.installed is not basis.predecessor_view
            ):
                raise ProviderInputContinuityConflict(
                    "provider-input planning predecessor changed during variants"
                )
            return _planning_input_from_predecessor(
                scope=basis.scope,
                predecessor_view=basis.predecessor_view,
                canonical_frontier=canonical_frontier,
                dispatch_anchor=dispatch_anchor,
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
            if slot.replay_reservation is not None:
                raise ProviderInputContinuityConflict(
                    "continuity scope is settling an assistant replay fragment"
                )
            installed = slot.installed
            if (
                candidate.wire_input_plan.compiled_semantic_fingerprint
                != candidate.resulting_compiled_input.compiled_semantic_fingerprint
                or candidate.wire_input_plan.message_placements_fingerprint
                != compiled_message_placements_fingerprint(
                    candidate.resulting_compiled_input.message_placements
                )
                or candidate.wire_input_plan.context_id
                != candidate.resulting_compiled_input.context_id
                or candidate.wire_input_plan.materialization.tool_items
                != tuple(
                    item.wire_tool
                    for item in candidate.direct_native_projection_set.projections
                )
            ):
                raise ProviderInputContinuityConflict(
                    "provider wire plan does not exact-join compiled input"
                )
            candidate_bytes = provider_input_logical_bytes(
                system_prompt=candidate.resulting_compiled_input.system_prompt,
                tools=candidate.resulting_compiled_input.tools,
                messages=candidate.resulting_compiled_input.messages,
            )
            if candidate_bytes > MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES:
                raise ProviderInputContinuityConflict(
                    "provider-input epoch exceeds its logical bound"
                )
            candidate_bytes = max(
                candidate_bytes,
                candidate.wire_input_plan.quote.final_wire_utf8_bytes,
            )
            installed_bytes = sum(
                _slot_installed_and_reserved_bytes(current)
                for current in self._slots.values()
            )
            current_bytes = (
                0
                if installed is None
                else _view_resident_bytes(installed)
            )
            if (
                installed_bytes - current_bytes + candidate_bytes
                > MAXIMUM_HOST_INSTALLED_BYTES
            ):
                raise ProviderInputContinuityConflict(
                    "Host provider-input resident bound is exhausted"
                )
            prepared_bytes = sum(
                max(
                    provider_input_logical_bytes(
                        system_prompt=current.prepared.resulting_compiled_input.system_prompt,
                        tools=current.prepared.resulting_compiled_input.tools,
                        messages=current.prepared.resulting_compiled_input.messages,
                    ),
                    current.prepared.wire_input_plan.quote.final_wire_utf8_bytes,
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
                    or candidate.planning.predecessor_view is not None
                ):
                    raise ProviderInputContinuityConflict(
                        "initial append candidate has a predecessor"
                    )
            else:
                if (
                    candidate.expected_epoch_revision != installed.epoch_revision
                    or candidate.planning.predecessor_view is not installed
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
                        candidate.direct_native_projection_set
                        != installed.direct_native_projection_set
                    ):
                        raise ProviderInputContinuityConflict(
                            "successor rewrote the installed native tool projection"
                        )
                    if (
                        compiled.messages[: len(installed.messages)]
                        != installed.messages
                    ):
                        raise ProviderInputContinuityConflict(
                            "successor rewrote the installed message prefix"
                        )
                    old_wire = installed.wire_input_plan.materialization
                    new_wire = candidate.wire_input_plan.materialization
                    if (
                        old_wire.root_policy_value != new_wire.root_policy_value
                        or old_wire.tool_items != new_wire.tool_items
                        or new_wire.ordered_input_items[
                            : len(old_wire.ordered_input_items)
                        ]
                        != old_wire.ordered_input_items
                    ):
                        raise ProviderInputContinuityConflict(
                            "successor rewrote the installed provider wire prefix"
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
        candidate: PreparedProviderInputAppendCandidate,
        execution: object,
    ) -> ProcessLocalProviderInputInstallPermit:
        self._require_scope(candidate.scope)
        with self._lock:
            slot = self._slots.get(candidate.scope)
            if slot is None or slot.prepared is not candidate:
                raise ProviderInputContinuityConflict(
                    "prepared append candidate does not exact-join its scope"
                )
            scope = candidate.scope
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
                message_placements=compiled.message_placements,
                wire_input_plan=candidate.wire_input_plan,
                tool_exposure_plan=candidate.tool_exposure_plan,
                canonical_frontier=candidate.resulting_canonical_frontier,
                source_heads=candidate.resulting_source_heads,
                final_estimate=compiled.final_estimate,
                logical_bytes=provider_input_logical_bytes(
                    system_prompt=compiled.system_prompt,
                    tools=compiled.tools,
                    messages=compiled.messages,
                ),
                semantic_prefix_fingerprint=provider_input_prefix_fingerprint(
                    system_prompt=compiled.system_prompt,
                    tools=compiled.tools,
                    messages=compiled.messages,
                ),
                assistant_replay_fragments=(
                    ()
                    if slot.installed is None
                    or candidate.epoch_nonce != slot.installed.epoch_nonce
                    else slot.installed.assistant_replay_fragments
                ),
                tool_result_decisions=compiled.tool_result_decisions,
            )
            slot.installed = view
            slot.prepared = None
            slot.state = _SlotState.INSTALLED
            permit = ProcessLocalProviderInputInstallPermit(
                scope=scope,
                epoch_nonce=view.epoch_nonce,
                epoch_revision=view.epoch_revision,
                permit_nonce=f"provider-input-permit:{uuid4().hex}",
            )
            self._issued_permits[permit.permit_nonce] = (
                _IssuedProviderInputInstallPermit(
                    permit=permit,
                    candidate=candidate,
                    execution=execution,
                )
            )
            return permit

    def reserve_assistant_replay_fragment(
        self,
        *,
        scope: ProviderInputContinuityScope,
        epoch_nonce: str,
        epoch_revision: int,
        fragment: ProviderAssistantReplayFragment,
    ) -> ProcessLocalAssistantReplayFragmentReservation:
        """Reserve exact epoch/Host capacity before canonical assistant mutation."""

        self._require_scope(scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(scope)
            view = None if slot is None else slot.installed
            if (
                slot is None
                or slot.state is not _SlotState.INSTALLED
                or view is None
                or view.epoch_nonce != epoch_nonce
                or view.epoch_revision != epoch_revision
            ):
                raise ProviderInputContinuityConflict(
                    "assistant replay fragment targets a stale epoch"
                )
            if slot.replay_reservation is not None:
                raise ProviderInputContinuityConflict(
                    "assistant replay fragment reservation is already active"
                )
            existing = tuple(
                item
                for item in view.assistant_replay_fragments
                if item.assistant_entry_id == fragment.assistant_entry_id
            )
            if existing:
                if existing != (fragment,):
                    raise ProviderInputContinuityConflict(
                        "assistant replay fragment identity conflicts"
                    )
                already_bound = True
                additional_bytes = 0
            else:
                already_bound = False
                current_view_bytes = _view_resident_bytes(view)
                new_view_bytes = _view_resident_bytes(
                    view,
                    additional_fragment=fragment,
                )
                if new_view_bytes > MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES:
                    raise ProviderInputContinuityConflict(
                        "assistant replay fragment exhausts the epoch byte bound"
                    )
                additional_bytes = new_view_bytes - current_view_bytes
                host_bytes = sum(
                    _slot_installed_and_reserved_bytes(current)
                    for current in self._slots.values()
                )
                if host_bytes + additional_bytes > MAXIMUM_HOST_INSTALLED_BYTES:
                    raise ProviderInputContinuityConflict(
                        "assistant replay fragment exhausts the Host byte bound"
                    )
                prepared_bytes = sum(
                    max(
                        provider_input_logical_bytes(
                            system_prompt=(
                                current.prepared.resulting_compiled_input.system_prompt
                            ),
                            tools=current.prepared.resulting_compiled_input.tools,
                            messages=current.prepared.resulting_compiled_input.messages,
                        ),
                        current.prepared.wire_input_plan.quote.final_wire_utf8_bytes,
                    )
                    for current in self._slots.values()
                    if current.prepared is not None
                )
                if host_bytes + additional_bytes + prepared_bytes > (
                    MAXIMUM_HOST_INSTALLED_AND_PREPARED_BYTES
                ):
                    raise ProviderInputContinuityConflict(
                        "assistant replay fragment exhausts the Host aggregate bound"
                    )
            reservation = ProcessLocalAssistantReplayFragmentReservation(
                scope=scope,
                epoch_nonce=epoch_nonce,
                epoch_revision=epoch_revision,
                fragment=fragment,
                additional_resident_bytes=additional_bytes,
                already_bound=already_bound,
                reservation_nonce=f"assistant-replay-reservation:{uuid4().hex}",
                _seal=_ASSISTANT_REPLAY_RESERVATION_SEAL,
            )
            slot.replay_reservation = reservation
            return reservation

    def promote_assistant_replay_fragment(
        self,
        reservation: ProcessLocalAssistantReplayFragmentReservation,
    ) -> None:
        """Promote a pre-admitted fragment after the exact assistant is FULL."""

        self._require_scope(reservation.scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(reservation.scope)
            view = None if slot is None else slot.installed
            if (
                slot is None
                or slot.replay_reservation is not reservation
                or slot.state is not _SlotState.INSTALLED
                or view is None
                or view.epoch_nonce != reservation.epoch_nonce
                or view.epoch_revision != reservation.epoch_revision
            ):
                raise ProviderInputContinuityConflict(
                    "assistant replay reservation targets a stale epoch"
                )
            fragment = reservation.fragment
            existing = tuple(
                item
                for item in view.assistant_replay_fragments
                if item.assistant_entry_id == fragment.assistant_entry_id
            )
            if reservation._already_bound:
                if existing != (fragment,):
                    raise ProviderInputContinuityConflict(
                        "bound assistant replay fragment identity drifted"
                    )
            else:
                if existing:
                    raise ProviderInputContinuityConflict(
                        "assistant replay fragment appeared after reservation"
                    )
                # Capacity was charged while the canonical mutation was still
                # impossible.  Promotion contains no fallible allocator gate.
                slot.installed = replace(
                    view,
                    assistant_replay_fragments=(
                        *view.assistant_replay_fragments,
                        fragment,
                    ),
                )
            slot.replay_reservation = None

    def release_assistant_replay_fragment_reservation(
        self,
        reservation: ProcessLocalAssistantReplayFragmentReservation,
    ) -> None:
        """Release a NONE/CONFLICT/failed pre-commit capacity claim."""

        self._require_scope(reservation.scope)
        with self._lock:
            slot = self._slots.get(reservation.scope)
            if slot is None or slot.state is _SlotState.CLOSED:
                return
            if slot.replay_reservation is reservation:
                slot.replay_reservation = None
                return
            if slot.replay_reservation is not None:
                raise ProviderInputContinuityConflict(
                    "assistant replay reservation identity conflicts"
                )

    def _consume_install_permit(
        self,
        permit: ProcessLocalProviderInputInstallPermit,
        *,
        candidate: PreparedProviderInputAppendCandidate,
        execution: object,
    ) -> None:
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            issued = self._issued_permits.get(permit.permit_nonce)
            if (
                issued is None
                or issued.permit is not permit
                or issued.candidate is not candidate
                or issued.execution is not execution
            ):
                raise ProviderInputContinuityConflict(
                    "provider-input install permit was not issued for this execution"
                )
            del self._issued_permits[permit.permit_nonce]

    def _require_registered_plan(
        self,
        *,
        candidate: PreparedProviderInputAppendCandidate,
        wire_input_plan: FrozenProviderWireInputPlan,
        tool_exposure_plan: FrozenToolCapabilityExposurePlan,
    ) -> None:
        self._require_scope(candidate.scope)
        with self._lock:
            if self._closed:
                raise ProviderInputContinuityConflict("continuity owner is closed")
            slot = self._slots.get(candidate.scope)
            if (
                slot is None
                or slot.prepared is not candidate
                or candidate.wire_input_plan is not wire_input_plan
                or candidate.tool_exposure_plan is not tool_exposure_plan
            ):
                raise ProviderInputContinuityConflict(
                    "provider wire plan was not registered for this candidate"
                )

    def discard(self, candidate: PreparedProviderInputAppendCandidate) -> None:
        self._require_scope(candidate.scope)
        with self._lock:
            slot = self._slots.get(candidate.scope)
            if slot is None or slot.prepared is not candidate:
                raise ProviderInputContinuityConflict(
                    "prepared append discard does not exact-join"
                )
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
                slot.replay_reservation = None
                slot.state = _SlotState.CLOSED
            self._issued_permits = {
                nonce: issued
                for nonce, issued in self._issued_permits.items()
                if issued.permit.scope != scope
            }

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for slot in self._slots.values():
                slot.installed = None
                slot.prepared = None
                slot.replay_reservation = None
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


def _view_resident_bytes(
    view: FrozenProviderInputEpochView,
    *,
    additional_fragment: ProviderAssistantReplayFragment | None = None,
) -> int:
    materialized_fragments = {
        item.replay_fragment_fingerprint
        for item in view.wire_input_plan.replacements
    }
    fragments = view.assistant_replay_fragments + (
        () if additional_fragment is None else (additional_fragment,)
    )
    unmaterialized = sum(
        item.logical_utf8_bytes
        for item in fragments
        if item.fragment_fingerprint not in materialized_fragments
    )
    return max(
        view.logical_bytes,
        view.wire_input_plan.quote.final_wire_utf8_bytes,
    ) + unmaterialized


def _planning_input_from_predecessor(
    *,
    scope: ProviderInputContinuityScope,
    predecessor_view: FrozenProviderInputEpochView | None,
    canonical_frontier: ProcessLocalCanonicalFrontier,
    dispatch_anchor: ProviderInputDispatchAnchor,
) -> FrozenProviderInputAppendPlanningInput:
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
            # compiler using the frozen compatibility fact. Planning records
            # the full rematerialization input and never repairs a rewrite.
            delta = canonical_frontier.ordered_item_fingerprints
    return FrozenProviderInputAppendPlanningInput(
        planning_nonce=f"provider-input-planning:{uuid4().hex}",
        scope=scope,
        predecessor=predecessor,
        predecessor_view=predecessor_view,
        dispatch_anchor=dispatch_anchor,
        canonical_delta_fingerprints=delta,
    )


def _slot_installed_and_reserved_bytes(slot: _Slot) -> int:
    installed = 0 if slot.installed is None else _view_resident_bytes(slot.installed)
    reserved = (
        0
        if slot.replay_reservation is None
        else slot.replay_reservation.additional_resident_bytes
    )
    return installed + reserved


__all__ = [
    "HostProviderInputContinuityOwner",
    "MAXIMUM_CHILD_SCOPES",
    "MAXIMUM_HOST_INSTALLED_AND_PREPARED_BYTES",
    "MAXIMUM_HOST_INSTALLED_BYTES",
    "MAXIMUM_PROVIDER_INPUT_EPOCH_BYTES",
    "MAXIMUM_ROOT_SCOPES",
    "ProviderInputContinuityConflict",
    "ProcessLocalAssistantReplayFragmentReservation",
    "ProcessLocalProviderInputInstallAuthority",
]
