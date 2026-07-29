"""Event-safe capability exposure owner identity.

This tiny module exists so run-entry facts may embed a complete capability
resolve basis without creating a ``run_entry <-> capability`` import cycle.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pulsara_agent.primitives.run_boundary_kind import HostRunBoundaryKind


class CapabilityExposureOwnerKind(StrEnum):
    HOST_BOUNDARY = "host_boundary"
    SUBAGENT_RUN_START = "subagent_run_start"


class CapabilityExposureOwnerFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_kind: CapabilityExposureOwnerKind
    owner_id: str = Field(min_length=1)
    host_boundary_kind: HostRunBoundaryKind | None
    runtime_session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_owner(self) -> "CapabilityExposureOwnerFact":
        if self.owner_kind is CapabilityExposureOwnerKind.HOST_BOUNDARY:
            if self.host_boundary_kind is None:
                raise ValueError("host exposure owner requires boundary kind")
        elif self.host_boundary_kind is not None:
            raise ValueError("subagent exposure owner cannot carry boundary kind")
        return self


__all__ = ["CapabilityExposureOwnerFact", "CapabilityExposureOwnerKind"]
