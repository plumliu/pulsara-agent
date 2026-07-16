"""Retained physical reservation owner for supported external execution ingress."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING

from pulsara_agent.event import (
    RequireExternalExecutionEvent,
)
from pulsara_agent.event_log.serialization import DEFAULT_EVENT_SCHEMA_REGISTRY
from pulsara_agent.primitives.authority_materialization import PhysicalOperationKind
from pulsara_agent.primitives.authority_materialization import LedgerWriteAdmissionClass
from pulsara_agent.runtime.context_input.event_slice import FrozenStoredEvent
from pulsara_agent.runtime.terminal_projection import ExternalExecutionResultCandidate

if TYPE_CHECKING:
    from pulsara_agent.runtime.session import EventWriteResult, RuntimeSession
    from pulsara_agent.runtime.state import LoopState


class ExternalExecutionCommitContractError(RuntimeError):
    """An external requirement/result pair violates its durable owner contract."""


@dataclass(frozen=True, slots=True)
class ExternalExecutionCommitPort:
    runtime_session: RuntimeSession
    state: LoopState | None = None

    async def commit_requirement(
        self,
        requirement: RequireExternalExecutionEvent,
    ) -> EventWriteResult:
        """Commit acceptance and retain headroom before external dispatch."""

        if requirement.sequence is not None:
            raise ExternalExecutionCommitContractError(
                "external requirement candidate is already committed"
            )
        await self.runtime_session.ensure_physical_operation_headroom(
            PhysicalOperationKind.EXTERNAL_EXECUTION
        )
        deadline = self.runtime_session.event_write_service.new_deadline_monotonic()
        self.runtime_session.publisher.bind_running_loop()

        def commit_dispatch() -> EventWriteResult:
            _, result = self.runtime_session.reserve_physical_operation_from_thread(
                (requirement,),
                operation_kind=PhysicalOperationKind.EXTERNAL_EXECUTION,
                reservation_id=_external_reservation_id(
                    self.runtime_session.runtime_session_id,
                    requirement.id,
                ),
                owner_id=requirement.id,
                state=self.state,
            )
            return result

        return await self.runtime_session.event_write_service.execute(
            commit_dispatch,
            deadline_monotonic=deadline,
        )

    async def commit_result(
        self,
        result: ExternalExecutionResultCandidate,
    ) -> EventWriteResult:
        """Validate the committed requirement and settle its exact reservation."""

        deadline = self.runtime_session.event_write_service.new_deadline_monotonic()
        requirement = await self._load_requirement(result, deadline_monotonic=deadline)
        prepared = (
            await self.runtime_session.tool_terminal_projection_service
            .prepare_external_result_batch(
                requirement=requirement,
                result=result,
                deadline_monotonic=deadline,
            )
        )
        reservation = self.runtime_session.physical_reservation_for_owner(
            operation_kind=PhysicalOperationKind.EXTERNAL_EXECUTION,
            owner_id=requirement.id,
        )
        if reservation is None:
            raise ExternalExecutionCommitContractError(
                "external result has no active physical reservation"
            )
        terminal_outcome = _external_terminal_outcome(result)
        self.runtime_session.publisher.bind_running_loop()

        def commit_settlement() -> EventWriteResult:
            return self.runtime_session.settle_physical_operation_from_thread(
                prepared,
                reservation=reservation,
                terminal_outcome=terminal_outcome,
                state=self.state,
            )

        return await self.runtime_session.event_write_service.execute(
            commit_settlement,
            deadline_monotonic=deadline,
            admission_class=LedgerWriteAdmissionClass.OPERATION_CONTINUATION,
            operation_owner_id=(
                self.runtime_session.physical_operation_admission_owner_id(
                    operation_kind=PhysicalOperationKind.EXTERNAL_EXECUTION,
                    owner_id=requirement.id,
                )
            ),
        )

    async def _load_requirement(
        self,
        result: ExternalExecutionResultCandidate,
        *,
        deadline_monotonic: float,
    ) -> RequireExternalExecutionEvent:
        references = tuple(item.requirement_ref for item in result.external_results)
        event_ids = {item.require_event_id for item in references}
        if len(event_ids) != 1:
            raise ExternalExecutionCommitContractError(
                "external result must settle one committed requirement"
            )
        require_event_id = next(iter(event_ids))

        def read_requirement():
            return self.runtime_session.event_log.read_raw_events_by_id(
                (require_event_id,),
                deadline_monotonic=deadline_monotonic,
            )

        rows = await self.runtime_session.context_input_io_service.execute(
            operation_name="external-execution-requirement-read",
            operation=read_requirement,
            deadline_monotonic=deadline_monotonic,
        )
        if len(rows) != 1:
            raise ExternalExecutionCommitContractError(
                "external requirement event is unavailable"
            )
        requirement = rows[0].decode_owned(DEFAULT_EVENT_SCHEMA_REGISTRY)
        if not isinstance(requirement, RequireExternalExecutionEvent):
            raise ExternalExecutionCommitContractError(
                "external result references the wrong event type"
            )
        if (
            requirement.run_id,
            requirement.turn_id,
            requirement.reply_id,
        ) != (result.run_id, result.turn_id, result.reply_id):
            raise ExternalExecutionCommitContractError(
                "external result lifecycle attribution drifted"
            )
        frozen_requirement = FrozenStoredEvent.from_stored_event(requirement)
        requirement_by_call = {
            item.tool_call_id: item for item in requirement.external_tool_calls
        }
        result_by_call = {
            item.result_block.tool_call_id: item for item in result.external_results
        }
        if set(requirement_by_call) != set(result_by_call):
            raise ExternalExecutionCommitContractError(
                "external result call set differs from its requirement"
            )
        for tool_call_id, ingress in result_by_call.items():
            reference = ingress.requirement_ref
            expected = requirement_by_call[tool_call_id]
            if (
                reference.owner_runtime_session_id
                != self.runtime_session.runtime_session_id
                or reference.require_event_id != requirement.id
                or reference.require_event_sequence != requirement.sequence
                or reference.require_event_payload_fingerprint
                != frozen_requirement.payload_fingerprint
                or reference.requirement_fingerprint
                != expected.requirement_fingerprint
            ):
                raise ExternalExecutionCommitContractError(
                    "external result requirement reference drifted"
                )
        return requirement


def _external_reservation_id(runtime_session_id: str, requirement_event_id: str) -> str:
    digest = sha256(
        f"{runtime_session_id}\x1f{requirement_event_id}".encode()
    ).hexdigest()
    return f"physical:external:{digest}"


def _external_terminal_outcome(result: ExternalExecutionResultCandidate) -> str:
    states = {item.result_block.result_state.value for item in result.external_results}
    if states == {"success"}:
        return "completed"
    if states == {"denied"}:
        return "denied"
    if "interrupted" in states:
        return "cancelled"
    return "runtime_error"


__all__ = [
    "ExternalExecutionCommitContractError",
    "ExternalExecutionCommitPort",
    "ExternalExecutionResultCandidate",
]
