"""Process-local owner for exactly one model step."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from pulsara_agent.ports.run_execution import AttemptState, RunActivationIdentity
from pulsara_agent.runtime.run_execution.owner import RunActivationCoordinator


ModelStepDisposition = Literal[
    "reply_ready",
    "replan_required",
    "terminal_stop",
    "model_error",
    "reconciliation_required",
]


@dataclass(slots=True)
class ModelStepAttempt:
    attempt_id: str
    activation_identity: RunActivationIdentity
    model_step_ordinal: int
    state: AttemptState = "prepared"
    disposition: ModelStepDisposition | None = None

    @classmethod
    def install(
        cls,
        coordinator: RunActivationCoordinator,
        *,
        model_step_ordinal: int,
    ) -> "ModelStepAttempt":
        if coordinator.activation_identity is None:
            raise RuntimeError("model step requires a committed activation identity")
        if coordinator.segment_state != "active" or coordinator.phase != "safe_point":
            raise RuntimeError("model step can start only at an active safe point")
        if coordinator.active_attempt is not None:
            raise RuntimeError("activation already owns another one-step attempt")
        attempt = cls(
            attempt_id=f"model-step-attempt:{uuid4().hex}",
            activation_identity=coordinator.activation_identity,
            model_step_ordinal=model_step_ordinal,
        )
        coordinator.active_attempt = attempt
        coordinator.phase = "model_step"
        return attempt

    def begin_dispatch(self, coordinator: RunActivationCoordinator) -> None:
        self._require_owner(coordinator)
        if self.state != "prepared":
            raise RuntimeError("model-step dispatch is not in PREPARED")
        self.state = "committing"

    def settle(
        self,
        coordinator: RunActivationCoordinator,
        *,
        disposition: ModelStepDisposition,
    ) -> None:
        self._require_owner(coordinator)
        if self.state not in {"prepared", "committing"}:
            raise RuntimeError("model-step attempt is already settled")
        self.disposition = disposition
        self.state = "unknown" if disposition == "reconciliation_required" else "full"
        coordinator.active_attempt = None
        coordinator.phase = (
            "completed" if disposition == "terminal_stop" else "safe_point"
        )

    def _require_owner(self, coordinator: RunActivationCoordinator) -> None:
        if coordinator.active_attempt is not self:
            raise RuntimeError("stale model-step attempt owner")
        if coordinator.activation_identity != self.activation_identity:
            raise RuntimeError("model-step activation identity changed")


__all__ = ["ModelStepAttempt", "ModelStepDisposition"]
