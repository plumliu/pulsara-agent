"""Runtime-only normalized model execution results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pulsara_agent.primitives.model_call import (
    ModelCallDiagnosticFact,
    ModelTokenUsageFact,
)


@dataclass(frozen=True, slots=True)
class TransportUsageReport:
    usage_status: Literal["reported", "missing"]
    usage: ModelTokenUsageFact | None
    provider_diagnostics: tuple[ModelCallDiagnosticFact, ...] = ()
    reported_model_id: str | None = None

    def __post_init__(self) -> None:
        if self.usage_status == "reported" and self.usage is None:
            raise ValueError("reported transport usage requires a usage fact")
        if self.usage_status == "missing" and self.usage is not None:
            raise ValueError("missing transport usage cannot contain a usage fact")
        if self.reported_model_id is not None and (
            not self.reported_model_id
            or self.reported_model_id != self.reported_model_id.strip()
        ):
            raise ValueError("reported model id must be a non-empty trimmed string")
