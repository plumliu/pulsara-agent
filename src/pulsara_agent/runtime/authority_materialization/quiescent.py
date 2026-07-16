"""Account-aware commits for quiescent restart recovery."""

from __future__ import annotations

from hashlib import sha256
from typing import Sequence

from pulsara_agent.event import AgentEvent, EventContext
from pulsara_agent.event_log import EventLog
from pulsara_agent.primitives.authority_materialization import PhysicalOperationKind
from pulsara_agent.runtime.authority_materialization.account import (
    LedgerMaterializationAccountStore,
    LedgerMaterializationCoordinator,
    MaterializationAccountContractError,
)
from pulsara_agent.runtime.authority_materialization.contracts import (
    build_default_authority_materialization_contract_bundle,
)


def commit_quiescent_accounted_batch(
    *,
    event_log: EventLog,
    business_events: Sequence[AgentEvent],
    owner_scope: str,
    deadline_monotonic: float | None = None,
) -> tuple[AgentEvent, ...]:
    """Commit one finite recovery batch with the materialization account CAS."""

    events = tuple(business_events)
    if not events:
        raise ValueError("quiescent recovery batch cannot be empty")
    account = event_log.read_materialization_account_state(
        deadline_monotonic=deadline_monotonic
    )
    if account is None:
        raise MaterializationAccountContractError(
            "quiescent recovery requires the durable materialization account"
        )
    contracts = build_default_authority_materialization_contract_bundle()
    store = LedgerMaterializationAccountStore(
        state=account,
        charge_contract=contracts.charge_contract,
    )
    coordinator = LedgerMaterializationCoordinator(
        runtime_session_id=event_log.runtime_session_id,
        event_log=event_log,
        store=store,
        charge_contract=contracts.charge_contract,
        limits=contracts.limits,
    )
    digest = sha256(
        "\x1f".join(f"{event.id}\x1e{event.type}" for event in events).encode(
            "utf-8"
        )
    ).hexdigest()
    first = events[0]
    committed = coordinator.commit_one_shot_operation(
        context=EventContext(
            run_id=first.run_id,
            turn_id=first.turn_id,
            reply_id=first.reply_id,
        ),
        business_events=events,
        reservation_id=f"recovery_physical:{digest}",
        owner_id=f"{owner_scope}:{digest}",
        burst_contract=(
            contracts.burst_registry.unique_binding_for_operation(
                PhysicalOperationKind.RUNTIME_INTERNAL_WRITE
            ).contract
        ),
        deadline_monotonic=deadline_monotonic,
    )
    return committed.stored_events


__all__ = ["commit_quiescent_accounted_batch"]
