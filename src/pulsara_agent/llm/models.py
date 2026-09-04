"""One exact executable model profile; there are no model-role slots."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.llm.model_catalog import WireApi
from pulsara_agent.llm.provider import RouteWireProfile


@dataclass(frozen=True, slots=True)
class ModelProfile:
    id: str
    route_id: str
    wire_api: WireApi
    base_url: str
    route_wire_profile: RouteWireProfile

    def __post_init__(self) -> None:
        for name, value in (
            ("model ID", self.id),
            ("route ID", self.route_id),
            ("base URL", self.base_url),
        ):
            if not value or value != value.strip():
                raise ValueError(f"{name} must be non-empty canonical text")
        if self.route_wire_profile.wire_api != self.wire_api.value:
            raise ValueError("model profile and route/wire profile disagree")


__all__ = ["ModelProfile"]
