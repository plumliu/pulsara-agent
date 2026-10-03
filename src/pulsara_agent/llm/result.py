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
    usage_status: Literal["reported", "partial", "missing", "invalid"]
    usage: ModelTokenUsageFact | None
    provider_diagnostics: tuple[ModelCallDiagnosticFact, ...] = ()
    reported_model_id: str | None = None

    def __post_init__(self) -> None:
        if self.usage_status not in {"reported", "partial", "missing", "invalid"}:
            raise ValueError("transport usage status is invalid")
        if self.usage is not None:
            fields = self.usage.model_dump()
            expected = "reported" if self.usage.input_tokens is not None and self.usage.output_tokens is not None else "partial"
            if not any(value is not None for value in fields.values()) or self.usage_status != expected:
                raise ValueError("transport usage status differs from its observed fields")
        if self.usage_status in {"reported", "partial"} and self.usage is None:
            raise ValueError("reported transport usage requires a usage fact")
        if self.usage_status in {"missing", "invalid"} and self.usage is not None:
            raise ValueError("missing transport usage cannot contain a usage fact")
        if self.reported_model_id is not None and (
            not self.reported_model_id
            or self.reported_model_id != self.reported_model_id.strip()
        ):
            raise ValueError("reported model id must be a non-empty trimmed string")
