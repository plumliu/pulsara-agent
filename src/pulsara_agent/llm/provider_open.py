"""Closed process-local authorities for physical provider opens.

Permits intentionally carry only transport-free call targets and the narrow
product evidence needed to prove who issued the call.  ``ModelRuntime`` is the
only consumer allowed to turn one into a live transport borrow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING

from pulsara_agent.llm.frozen_target import (
    FrozenEpochModelCallTarget,
    FrozenProviderPhysicalCallTarget,
)
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.llm.model_connections import ModelConnectionConfig
from pulsara_agent.llm.model_connections import ModelCallBinding
from pulsara_agent.primitives.model_call import ModelCallPurpose

if TYPE_CHECKING:
    from pulsara_agent.settings import LocalSettings


_EPOCH_PERMIT_SEAL = object()
_SUMMARY_PERMIT_SEAL = object()
_AUXILIARY_PERMIT_SEAL = object()
_PROBE_PERMIT_SEAL = object()
_SUMMARY_PROMOTION_AUTHORITY_SEAL = object()
_MEMORY_TERMINAL_FENCE_SEAL = object()


@dataclass(slots=True, init=False)
class CompactionSummaryPromotionAuthority:
    """One exact, live compaction attempt may promote one summary call."""

    attempt_id: str
    scope_kind: object
    scope_subagent_task_id: str | None
    phase: object
    semantic: object = field(repr=False)
    decision: object = field(repr=False)
    successor_destination: FrozenEpochModelCallTarget = field(repr=False)
    _consumed: bool = field(repr=False)
    _lock: Lock = field(repr=False)

    def __init__(
        self,
        *,
        attempt_id: str,
        scope_kind: object,
        scope_subagent_task_id: str | None,
        phase: object,
        semantic: object,
        decision: object,
        successor_destination: FrozenEpochModelCallTarget,
        _seal: object,
    ) -> None:
        if (
            _seal is not _SUMMARY_PROMOTION_AUTHORITY_SEAL
            or not attempt_id
            or semantic is None
            or decision is None
            or successor_destination.purpose is not ModelCallPurpose.AGENT_MODEL_LOOP
        ):
            raise TypeError("summary promotion authority is runtime-owner-issued")
        self.attempt_id = attempt_id
        self.scope_kind = scope_kind
        self.scope_subagent_task_id = scope_subagent_task_id
        self.phase = phase
        self.semantic = semantic
        self.decision = decision
        self.successor_destination = successor_destination
        self._consumed = False
        self._lock = Lock()

    def _consume_for(
        self,
        *,
        semantic: object,
        decision: object,
        successor_destination: FrozenEpochModelCallTarget,
    ) -> None:
        with self._lock:
            if self._consumed:
                raise RuntimeError("summary promotion authority is already consumed")
            if (
                semantic is not self.semantic
                or decision is not self.decision
                or successor_destination is not self.successor_destination
            ):
                raise RuntimeError("summary promotion authority subject drifted")
            self._consumed = True


@dataclass(slots=True, init=False)
class ConfirmedMemoryGovernanceTerminalFence:
    """Repository-issued, one-shot authority for one exact auxiliary open."""

    candidate: object = field(repr=False)
    origin_model_call_binding: ModelCallBinding
    durable_terminal_fence: object = field(repr=False)
    _consumed: bool = field(repr=False)
    _bound_subject: tuple[object, ...] | None = field(repr=False)
    _lock: Lock = field(repr=False)

    def __init__(
        self,
        *,
        candidate: object,
        origin_model_call_binding: ModelCallBinding,
        durable_terminal_fence: object,
        _seal: object,
    ) -> None:
        if (
            _seal is not _MEMORY_TERMINAL_FENCE_SEAL
            or candidate is None
            or durable_terminal_fence is None
        ):
            raise TypeError("memory terminal fence is repository-issued")
        self.candidate = candidate
        self.origin_model_call_binding = origin_model_call_binding
        self.durable_terminal_fence = durable_terminal_fence
        self._consumed = False
        self._bound_subject = None
        self._lock = Lock()

    def _consume_for(
        self,
        *,
        origin_model_call_binding: ModelCallBinding,
        call_target: FrozenProviderPhysicalCallTarget,
        resolved_model_call_id: str,
        context: object,
        estimated_input_tokens: int,
        final_wire_utf8_bytes: int,
        maximum_result_bytes: int,
        timeout_policy_fingerprint: str,
    ) -> None:
        with self._lock:
            if self._consumed:
                raise RuntimeError("memory terminal fence is already consumed")
            if (
                origin_model_call_binding != self.origin_model_call_binding
                or call_target.purpose is not ModelCallPurpose.MEMORY_GOVERNANCE
                or not resolved_model_call_id
                or estimated_input_tokens < 1
                or final_wire_utf8_bytes < 1
                or maximum_result_bytes < 1
                or not timeout_policy_fingerprint
            ):
                raise RuntimeError("memory terminal fence subject drifted")
            self._bound_subject = (
                call_target,
                resolved_model_call_id,
                context,
                estimated_input_tokens,
                final_wire_utf8_bytes,
                maximum_result_bytes,
                timeout_policy_fingerprint,
            )
            self._consumed = True


def _issue_compaction_summary_promotion_authority(
    *,
    attempt_id: str,
    scope_kind: object,
    scope_subagent_task_id: str | None,
    phase: object,
    semantic: object,
    decision: object,
    successor_destination: FrozenEpochModelCallTarget,
) -> CompactionSummaryPromotionAuthority:
    return CompactionSummaryPromotionAuthority(
        attempt_id=attempt_id,
        scope_kind=scope_kind,
        scope_subagent_task_id=scope_subagent_task_id,
        phase=phase,
        semantic=semantic,
        decision=decision,
        successor_destination=successor_destination,
        _seal=_SUMMARY_PROMOTION_AUTHORITY_SEAL,
    )


def _issue_confirmed_memory_governance_terminal_fence(
    *,
    candidate: object,
    origin_model_call_binding: ModelCallBinding,
    durable_terminal_fence: object,
) -> ConfirmedMemoryGovernanceTerminalFence:
    return ConfirmedMemoryGovernanceTerminalFence(
        candidate=candidate,
        origin_model_call_binding=origin_model_call_binding,
        durable_terminal_fence=durable_terminal_fence,
        _seal=_MEMORY_TERMINAL_FENCE_SEAL,
    )


class _OneShotPermit:
    __slots__ = ("_consumed", "_lock")

    def __init__(self) -> None:
        self._consumed = False
        self._lock = Lock()

    def _consume_once(self) -> None:
        with self._lock:
            if self._consumed:
                raise RuntimeError("provider-open permit is already consumed")
            self._consumed = True


@dataclass(slots=True, init=False)
class EpochAgentLoopProviderOpenPermit(_OneShotPermit):
    epoch_target: FrozenEpochModelCallTarget
    resolved_model_call_id: str
    timeout_policy: OpenAITransportTimeoutPolicy = field(repr=False)
    _installed_resource_owner: object = field(repr=False)

    def __init__(
        self,
        *,
        epoch_target: FrozenEpochModelCallTarget,
        resolved_model_call_id: str,
        timeout_policy: OpenAITransportTimeoutPolicy,
        installed_resource_owner: object,
        _seal: object,
    ) -> None:
        if (
            _seal is not _EPOCH_PERMIT_SEAL
            or epoch_target.purpose is not ModelCallPurpose.AGENT_MODEL_LOOP
            or not resolved_model_call_id
            or installed_resource_owner is None
        ):
            raise TypeError("epoch provider-open permit is owner-issued")
        _OneShotPermit.__init__(self)
        self.epoch_target = epoch_target
        self.resolved_model_call_id = resolved_model_call_id
        self.timeout_policy = timeout_policy
        self._installed_resource_owner = installed_resource_owner

    @property
    def call_target(self) -> FrozenProviderPhysicalCallTarget:
        return self.epoch_target.physical_call_target


@dataclass(slots=True, init=False)
class CompactionSummaryProviderOpenPermit(_OneShotPermit):
    summary_call_target: FrozenProviderPhysicalCallTarget
    successor_destination: FrozenEpochModelCallTarget
    resolved_model_call_id: str
    timeout_policy: OpenAITransportTimeoutPolicy = field(repr=False)
    _promotion_authority: CompactionSummaryPromotionAuthority = field(repr=False)

    def __init__(
        self,
        *,
        summary_call_target: FrozenProviderPhysicalCallTarget,
        successor_destination: FrozenEpochModelCallTarget,
        resolved_model_call_id: str,
        timeout_policy: OpenAITransportTimeoutPolicy,
        promotion_authority: CompactionSummaryPromotionAuthority,
        _seal: object,
    ) -> None:
        if (
            _seal is not _SUMMARY_PERMIT_SEAL
            or summary_call_target.purpose
            is not ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY
            or successor_destination.purpose is not ModelCallPurpose.AGENT_MODEL_LOOP
            or not resolved_model_call_id
            or not isinstance(
                promotion_authority, CompactionSummaryPromotionAuthority
            )
            or not promotion_authority._consumed
        ):
            raise TypeError("compaction summary permit is owner-issued")
        _OneShotPermit.__init__(self)
        self.summary_call_target = summary_call_target
        self.successor_destination = successor_destination
        self.resolved_model_call_id = resolved_model_call_id
        self.timeout_policy = timeout_policy
        self._promotion_authority = promotion_authority

    @property
    def call_target(self) -> FrozenProviderPhysicalCallTarget:
        return self.summary_call_target


@dataclass(slots=True, init=False)
class AuxiliaryModelProviderOpenPermit(_OneShotPermit):
    auxiliary_call_target: FrozenProviderPhysicalCallTarget
    resolved_model_call_id: str
    timeout_policy: OpenAITransportTimeoutPolicy = field(repr=False)
    _terminal_fence: ConfirmedMemoryGovernanceTerminalFence = field(repr=False)

    def __init__(
        self,
        *,
        auxiliary_call_target: FrozenProviderPhysicalCallTarget,
        resolved_model_call_id: str,
        timeout_policy: OpenAITransportTimeoutPolicy,
        terminal_fence: ConfirmedMemoryGovernanceTerminalFence,
        _seal: object,
    ) -> None:
        if (
            _seal is not _AUXILIARY_PERMIT_SEAL
            or auxiliary_call_target.purpose
            is not ModelCallPurpose.MEMORY_GOVERNANCE
            or not resolved_model_call_id
            or not isinstance(
                terminal_fence, ConfirmedMemoryGovernanceTerminalFence
            )
            or not terminal_fence._consumed
            or terminal_fence._bound_subject is None
        ):
            raise TypeError("auxiliary provider-open permit is owner-issued")
        _OneShotPermit.__init__(self)
        self.auxiliary_call_target = auxiliary_call_target
        self.resolved_model_call_id = resolved_model_call_id
        self.timeout_policy = timeout_policy
        self._terminal_fence = terminal_fence

    @property
    def call_target(self) -> FrozenProviderPhysicalCallTarget:
        return self.auxiliary_call_target


class EphemeralProbeCredentialOwner:
    """One-shot secret owner used only by an unpublished connection probe."""

    __slots__ = ("connection", "_secret", "_lock", "_closed")

    def __init__(self, connection: ModelConnectionConfig, secret: str) -> None:
        self.connection = connection
        self._secret = secret
        self._lock = Lock()
        self._closed = False

    def read_settings(self) -> "LocalSettings":
        from pulsara_agent.settings import LocalModelApiKey, LocalSettings

        with self._lock:
            if self._closed:
                raise RuntimeError("probe credential owner is closed")
            api_keys = (
                (LocalModelApiKey(self.connection.id, self._secret),)
                if self.connection.requires_api_key and self._secret
                else ()
            )
            return LocalSettings(
                model_connections=(self.connection,),
                model_api_keys=api_keys,
            )

    def read(self) -> "LocalSettings":
        return self.read_settings()

    def close(self) -> None:
        with self._lock:
            self._secret = ""
            self._closed = True


@dataclass(slots=True, init=False)
class ConnectionProbeProviderOpenPermit(_OneShotPermit):
    probe_call_target: FrozenProviderPhysicalCallTarget
    resolved_model_call_id: str
    timeout_policy: OpenAITransportTimeoutPolicy = field(repr=False)
    credential_owner: EphemeralProbeCredentialOwner = field(repr=False)

    def __init__(
        self,
        *,
        probe_call_target: FrozenProviderPhysicalCallTarget,
        resolved_model_call_id: str,
        timeout_policy: OpenAITransportTimeoutPolicy,
        credential_owner: EphemeralProbeCredentialOwner,
        _seal: object,
    ) -> None:
        if (
            _seal is not _PROBE_PERMIT_SEAL
            or probe_call_target.purpose is not ModelCallPurpose.CONNECTION_PROBE
            or probe_call_target.target_bundle.connection.connection_id
            != credential_owner.connection.id
            or not resolved_model_call_id
        ):
            raise TypeError("connection probe permit is owner-issued")
        _OneShotPermit.__init__(self)
        self.probe_call_target = probe_call_target
        self.resolved_model_call_id = resolved_model_call_id
        self.timeout_policy = timeout_policy
        self.credential_owner = credential_owner

    @property
    def call_target(self) -> FrozenProviderPhysicalCallTarget:
        return self.probe_call_target


ProviderOpenPurposePermit = (
    EpochAgentLoopProviderOpenPermit
    | CompactionSummaryProviderOpenPermit
    | AuxiliaryModelProviderOpenPermit
    | ConnectionProbeProviderOpenPermit
)


def _issue_epoch_agent_loop_provider_open_permit(
    *,
    epoch_target: FrozenEpochModelCallTarget,
    resolved_model_call_id: str,
    timeout_policy: OpenAITransportTimeoutPolicy,
    installed_resource_owner: object,
) -> EpochAgentLoopProviderOpenPermit:
    return EpochAgentLoopProviderOpenPermit(
        epoch_target=epoch_target,
        resolved_model_call_id=resolved_model_call_id,
        timeout_policy=timeout_policy,
        installed_resource_owner=installed_resource_owner,
        _seal=_EPOCH_PERMIT_SEAL,
    )


def _issue_compaction_summary_provider_open_permit(
    *,
    summary_call_target: FrozenProviderPhysicalCallTarget,
    successor_destination: FrozenEpochModelCallTarget,
    resolved_model_call_id: str,
    timeout_policy: OpenAITransportTimeoutPolicy,
    promotion_authority: CompactionSummaryPromotionAuthority,
    semantic: object,
    decision: object,
) -> CompactionSummaryProviderOpenPermit:
    if not isinstance(
        promotion_authority, CompactionSummaryPromotionAuthority
    ):
        raise TypeError("summary promotion authority is not owner-issued")
    promotion_authority._consume_for(
        semantic=semantic,
        decision=decision,
        successor_destination=successor_destination,
    )
    return CompactionSummaryProviderOpenPermit(
        summary_call_target=summary_call_target,
        successor_destination=successor_destination,
        resolved_model_call_id=resolved_model_call_id,
        timeout_policy=timeout_policy,
        promotion_authority=promotion_authority,
        _seal=_SUMMARY_PERMIT_SEAL,
    )


def _issue_auxiliary_model_provider_open_permit(
    *,
    auxiliary_call_target: FrozenProviderPhysicalCallTarget,
    resolved_model_call_id: str,
    timeout_policy: OpenAITransportTimeoutPolicy,
    terminal_fence: ConfirmedMemoryGovernanceTerminalFence,
    origin_model_call_binding: ModelCallBinding,
    context: object,
    estimated_input_tokens: int,
    final_wire_utf8_bytes: int,
    maximum_result_bytes: int,
    timeout_policy_fingerprint: str,
) -> AuxiliaryModelProviderOpenPermit:
    if not isinstance(
        terminal_fence, ConfirmedMemoryGovernanceTerminalFence
    ):
        raise TypeError("memory terminal fence is not repository-issued")
    terminal_fence._consume_for(
        origin_model_call_binding=origin_model_call_binding,
        call_target=auxiliary_call_target,
        resolved_model_call_id=resolved_model_call_id,
        context=context,
        estimated_input_tokens=estimated_input_tokens,
        final_wire_utf8_bytes=final_wire_utf8_bytes,
        maximum_result_bytes=maximum_result_bytes,
        timeout_policy_fingerprint=timeout_policy_fingerprint,
    )
    return AuxiliaryModelProviderOpenPermit(
        auxiliary_call_target=auxiliary_call_target,
        resolved_model_call_id=resolved_model_call_id,
        timeout_policy=timeout_policy,
        terminal_fence=terminal_fence,
        _seal=_AUXILIARY_PERMIT_SEAL,
    )


def _issue_connection_probe_provider_open_permit(
    *,
    probe_call_target: FrozenProviderPhysicalCallTarget,
    resolved_model_call_id: str,
    timeout_policy: OpenAITransportTimeoutPolicy,
    credential_owner: EphemeralProbeCredentialOwner,
) -> ConnectionProbeProviderOpenPermit:
    return ConnectionProbeProviderOpenPermit(
        probe_call_target=probe_call_target,
        resolved_model_call_id=resolved_model_call_id,
        timeout_policy=timeout_policy,
        credential_owner=credential_owner,
        _seal=_PROBE_PERMIT_SEAL,
    )


__all__ = [
    "AuxiliaryModelProviderOpenPermit",
    "CompactionSummaryPromotionAuthority",
    "CompactionSummaryProviderOpenPermit",
    "ConfirmedMemoryGovernanceTerminalFence",
    "ConnectionProbeProviderOpenPermit",
    "EpochAgentLoopProviderOpenPermit",
    "ProviderOpenPurposePermit",
]
