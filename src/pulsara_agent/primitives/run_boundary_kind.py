"""Low-level Host run-boundary kind vocabulary."""

from __future__ import annotations

from enum import StrEnum


class HostRunBoundaryKind(StrEnum):
    PRE_RUN = "pre_run"
    PRE_RUNTIME_REQUEST = "pre_runtime_request"
    PRE_INTERACTION_RESUME = "pre_interaction_resume"


__all__ = ["HostRunBoundaryKind"]
