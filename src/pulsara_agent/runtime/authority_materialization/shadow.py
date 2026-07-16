"""AP0 process-local physical admission shadow.

The shadow deliberately never authorizes or blocks dispatch.  It evaluates the
same finite burst contracts that AP4 will make authoritative and records where
the transitional fixed guards disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Sequence

from pulsara_agent.event import AgentEvent, SubagentGraphCheckpointCommittedEvent
from pulsara_agent.event_log.serialization import canonical_event_payload_bytes
from pulsara_agent.primitives.authority_materialization import (
    AuthorityMaterializationLimits,
    PhysicalBurstContractFact,
    PhysicalChargeContractFact,
)
from pulsara_agent.primitives.context import context_fingerprint


@dataclass(frozen=True, slots=True)
class ShadowAdmissionObservation:
    owner_id: str
    burst_contract_id: str
    burst_contract_version: str
    burst_contract_fingerprint: str
    used_events_before: int
    used_payload_bytes_before: int
    active_reserved_events_before: int
    active_reserved_payload_bytes_before: int
    candidate_reserved_events: int
    candidate_reserved_payload_bytes: int
    would_admit: bool
    observation_fingerprint: str


@dataclass(frozen=True, slots=True)
class AuthorityMaterializationShadowSnapshot:
    through_sequence: int
    reclaimable_through_sequence: int
    used_since_reclaimable_events: int
    used_since_reclaimable_payload_bytes: int
    active_candidate_count: int
    active_reserved_events: int
    active_reserved_payload_bytes: int
    fixed_graph_delta_event_bound: int
    fixed_graph_delta_payload_byte_bound: int
    resolved_max_burst_events: int
    resolved_max_burst_payload_bytes: int
    latest_observations: tuple[ShadowAdmissionObservation, ...]


class AuthorityMaterializationShadowAccount:
    reducer_id = "authority_materialization_shadow:ap0-v1"

    def __init__(
        self,
        *,
        through_sequence: int,
        candidate_payload_bytes: int,
        limits: AuthorityMaterializationLimits,
        charge_contract: PhysicalChargeContractFact,
        fixed_graph_delta_event_bound: int,
        fixed_graph_delta_payload_byte_bound: int,
        resolved_max_burst_events: int,
        resolved_max_burst_payload_bytes: int,
    ) -> None:
        if through_sequence < 0 or candidate_payload_bytes < 0:
            raise ValueError("shadow bootstrap usage must be non-negative")
        self._lock = RLock()
        self._through_sequence = through_sequence
        self._charged_payload_bytes = candidate_payload_bytes + (
            through_sequence
            * (
                charge_contract.fixed_sequence_wrapper_charge_bytes_per_event
                + charge_contract.fixed_schema_wrapper_charge_bytes_per_event
            )
        )
        self._limits = limits
        self._charge_contract = charge_contract
        self._consumer_horizons: dict[str, int] = {
            "subagent_graph": 0,
            "transcript": 0,
        }
        self._active: dict[str, PhysicalBurstContractFact] = {}
        self._latest: list[ShadowAdmissionObservation] = []
        self._fixed_graph_delta_event_bound = fixed_graph_delta_event_bound
        self._fixed_graph_delta_payload_byte_bound = (
            fixed_graph_delta_payload_byte_bound
        )
        self._resolved_max_burst_events = resolved_max_burst_events
        self._resolved_max_burst_payload_bytes = resolved_max_burst_payload_bytes

    @property
    def through_sequence(self) -> int:
        with self._lock:
            return self._through_sequence

    def apply_committed(self, events: Sequence[AgentEvent]) -> None:
        with self._lock:
            ordered = tuple(sorted(events, key=_stored_sequence))
            if not ordered:
                return
            expected = self._through_sequence + 1
            if _stored_sequence(ordered[0]) != expected:
                raise ValueError("shadow reducer input is not contiguous")
            for event in ordered:
                sequence = _stored_sequence(event)
                if sequence != self._through_sequence + 1:
                    raise ValueError("shadow reducer input contains a sequence gap")
                self._charged_payload_bytes += len(
                    canonical_event_payload_bytes(event)
                ) + (
                    self._charge_contract.fixed_sequence_wrapper_charge_bytes_per_event
                    + self._charge_contract.fixed_schema_wrapper_charge_bytes_per_event
                )
                self._through_sequence = sequence
                if isinstance(event, SubagentGraphCheckpointCommittedEvent):
                    self._consumer_horizons["subagent_graph"] = max(
                        self._consumer_horizons["subagent_graph"],
                        event.checkpoint.through_sequence,
                    )

    def observe_candidate(
        self,
        *,
        owner_id: str,
        contract: PhysicalBurstContractFact,
    ) -> ShadowAdmissionObservation:
        if not owner_id:
            raise ValueError("shadow candidate owner id is required")
        with self._lock:
            if owner_id in self._active:
                raise ValueError("shadow candidate owner is already active")
            active_events = sum(
                item.max_total_reserved_events for item in self._active.values()
            )
            active_bytes = sum(
                item.max_total_reserved_payload_bytes for item in self._active.values()
            )
            used_events = self._through_sequence - min(
                self._consumer_horizons.values()
            )
            used_bytes = self._charged_payload_bytes
            would_admit = (
                used_events
                + active_events
                + contract.max_total_reserved_events
                <= self._limits.max_unreclaimable_ledger_events
                - self._limits.maintenance_reserved_events
                and used_bytes
                + active_bytes
                + contract.max_total_reserved_payload_bytes
                <= self._limits.max_unreclaimable_charged_payload_bytes
                - self._limits.maintenance_reserved_payload_bytes
            )
            payload = {
                "owner_id": owner_id,
                "burst_contract_id": contract.contract_id,
                "burst_contract_version": contract.contract_version,
                "burst_contract_fingerprint": contract.contract_fingerprint,
                "used_events_before": used_events,
                "used_payload_bytes_before": used_bytes,
                "active_reserved_events_before": active_events,
                "active_reserved_payload_bytes_before": active_bytes,
                "candidate_reserved_events": contract.max_total_reserved_events,
                "candidate_reserved_payload_bytes": (
                    contract.max_total_reserved_payload_bytes
                ),
                "would_admit": would_admit,
            }
            observation = ShadowAdmissionObservation(
                **payload,
                observation_fingerprint=context_fingerprint(
                    "authority-materialization-shadow-observation:v1", payload
                ),
            )
            self._active[owner_id] = contract
            self._latest.append(observation)
            del self._latest[:-64]
            return observation

    def release_candidate(self, owner_id: str) -> None:
        with self._lock:
            self._active.pop(owner_id, None)

    def snapshot(self) -> AuthorityMaterializationShadowSnapshot:
        with self._lock:
            reclaimable = min(self._consumer_horizons.values())
            return AuthorityMaterializationShadowSnapshot(
                through_sequence=self._through_sequence,
                reclaimable_through_sequence=reclaimable,
                used_since_reclaimable_events=self._through_sequence - reclaimable,
                used_since_reclaimable_payload_bytes=self._charged_payload_bytes,
                active_candidate_count=len(self._active),
                active_reserved_events=sum(
                    item.max_total_reserved_events for item in self._active.values()
                ),
                active_reserved_payload_bytes=sum(
                    item.max_total_reserved_payload_bytes
                    for item in self._active.values()
                ),
                fixed_graph_delta_event_bound=self._fixed_graph_delta_event_bound,
                fixed_graph_delta_payload_byte_bound=(
                    self._fixed_graph_delta_payload_byte_bound
                ),
                resolved_max_burst_events=self._resolved_max_burst_events,
                resolved_max_burst_payload_bytes=self._resolved_max_burst_payload_bytes,
                latest_observations=tuple(self._latest),
            )


def _stored_sequence(event: AgentEvent) -> int:
    if event.sequence is None or event.sequence < 1:
        raise ValueError("shadow reducer requires committed events")
    return event.sequence


__all__ = [
    "AuthorityMaterializationShadowAccount",
    "AuthorityMaterializationShadowSnapshot",
    "ShadowAdmissionObservation",
]
