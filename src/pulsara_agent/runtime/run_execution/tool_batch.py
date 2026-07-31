"""Process-local owner for exactly one ordered tool batch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from pulsara_agent.ports.run_execution import AttemptState, RunActivationIdentity
from pulsara_agent.primitives._context_base import context_fingerprint
from pulsara_agent.runtime.run_execution.owner import RunActivationCoordinator


ToolBatchDisposition = Literal[
    "completed",
    "suspended",
    "terminalization_pending",
    "reconciliation_required",
]


@dataclass(slots=True)
class ToolBatchAttempt:
    attempt_id: str
    activation_identity: RunActivationIdentity
    ordered_tool_call_ids: tuple[str, ...]
    batch_fingerprint: str
    state: AttemptState = "prepared"
    disposition: ToolBatchDisposition | None = None

    @classmethod
    def install(
        cls,
        coordinator: RunActivationCoordinator,
        *,
        ordered_tool_call_ids: tuple[str, ...],
    ) -> "ToolBatchAttempt":
        if coordinator.activation_identity is None:
            raise RuntimeError("tool batch requires a committed activation identity")
        if coordinator.segment_state != "active" or coordinator.phase != "safe_point":
            raise RuntimeError("tool batch can start only at an active safe point")
        if coordinator.active_attempt is not None:
            raise RuntimeError("activation already owns another one-step attempt")
        if not ordered_tool_call_ids:
            raise ValueError("tool batch requires at least one tool-call ID")
        fingerprint = context_fingerprint(
            "run-tool-batch-attempt:v1",
            {
                "activation_fingerprint": (
                    coordinator.activation_identity.activation_fingerprint
                ),
                "ordered_tool_call_ids": ordered_tool_call_ids,
            },
        )
        attempt = cls(
            attempt_id=f"tool-batch-attempt:{uuid4().hex}",
            activation_identity=coordinator.activation_identity,
            ordered_tool_call_ids=ordered_tool_call_ids,
            batch_fingerprint=fingerprint,
        )
        coordinator.active_attempt = attempt
        coordinator.phase = "tool_batch"
        return attempt

    def begin_dispatch(self, coordinator: RunActivationCoordinator) -> None:
        self._require_owner(coordinator)
        if self.state != "prepared":
            raise RuntimeError("tool-batch dispatch is not in PREPARED")
        self.state = "committing"

    def settle(
        self,
        coordinator: RunActivationCoordinator,
        *,
        disposition: ToolBatchDisposition,
    ) -> None:
        self._require_owner(coordinator)
        if self.state not in {"prepared", "committing"}:
            raise RuntimeError("tool-batch attempt is already settled")
        self.disposition = disposition
        self.state = "unknown" if disposition == "reconciliation_required" else "full"
        coordinator.active_attempt = None
        if disposition == "suspended":
            coordinator.phase = "suspending"
        elif disposition == "terminalization_pending":
            coordinator.phase = "completed"
        else:
            coordinator.phase = "safe_point"

    def _require_owner(self, coordinator: RunActivationCoordinator) -> None:
        if coordinator.active_attempt is not self:
            raise RuntimeError("stale tool-batch attempt owner")
        if coordinator.activation_identity != self.activation_identity:
            raise RuntimeError("tool-batch activation identity changed")


__all__ = ["ToolBatchAttempt", "ToolBatchDisposition"]
