"""Renderer-neutral process-local Terminal observation installation carriers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json


TERMINAL_OBSERVATION_CANONICAL_HARD_BYTES = 32_000


class TerminalObservationKind(StrEnum):
    PROGRESS = "PROGRESS"
    HEARTBEAT = "HEARTBEAT"
    COMPLETION = "COMPLETION"
    EXPIRY = "EXPIRY"


class TerminalDeliveryCoverage(StrEnum):
    COMPLETE = "COMPLETE"
    HEAD_TAIL = "HEAD_TAIL"


@dataclass(frozen=True, slots=True)
class ExistingTurnInstallation:
    turn_id: str
    entry_id: str

    def __post_init__(self) -> None:
        if not self.turn_id or not self.entry_id:
            raise ValueError("existing turn installation target is incomplete")


@dataclass(frozen=True, slots=True)
class NewTurnInstallation:
    turn_id: str
    context_binding_revision_id: str
    initial_entry_id: str

    def __post_init__(self) -> None:
        if not all(
            (self.turn_id, self.context_binding_revision_id, self.initial_entry_id)
        ):
            raise ValueError("new turn installation target is incomplete")


PreparedInstallationTarget = ExistingTurnInstallation | NewTurnInstallation


@dataclass(frozen=True, slots=True)
class TerminalObservationContentV1:
    observation_id: str
    monitor_id: str
    process_id: str
    observation_ordinal: int
    observation_kind: TerminalObservationKind
    process_status: str
    exit_code: int | None
    output_disposition: str
    gap_before_output: bool
    delivery_coverage: TerminalDeliveryCoverage
    available_source_utf8_bytes: int
    included_source_utf8_bytes: int
    omitted_by_delivery_bound_utf8_bytes: int
    output: str
    schema_version: str = "terminal_observation.v1"
    host_scoped: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != "terminal_observation.v1" or self.host_scoped is not True:
            raise ValueError("terminal observation contract identity is invalid")
        if not self.observation_id or not self.monitor_id or not self.process_id:
            raise ValueError("terminal observation identity is incomplete")
        if self.observation_ordinal < 1:
            raise ValueError("terminal observation ordinal is invalid")
        if self.process_status not in {
            "running",
            "success",
            "error",
            "timeout",
            "blocked",
            "killed",
        }:
            raise ValueError("terminal observation process status is invalid")
        if self.output_disposition not in {
            "CURRENT_SNAPSHOT",
            "EXACT_DELTA",
            "GAP",
            "INVALID_CURSOR",
            "UNAVAILABLE",
        }:
            raise ValueError("terminal observation output disposition is invalid")
        if min(
            self.available_source_utf8_bytes,
            self.included_source_utf8_bytes,
            self.omitted_by_delivery_bound_utf8_bytes,
        ) < 0:
            raise ValueError("terminal observation byte counts must be non-negative")
        rendered_size = len(self.output.encode("utf-8"))
        if rendered_size < self.included_source_utf8_bytes:
            raise ValueError("terminal observation included byte count is invalid")
        if (
            self.included_source_utf8_bytes
            + self.omitted_by_delivery_bound_utf8_bytes
            != self.available_source_utf8_bytes
        ):
            raise ValueError("terminal observation coverage counts do not balance")
        if (
            self.delivery_coverage is TerminalDeliveryCoverage.COMPLETE
            and self.omitted_by_delivery_bound_utf8_bytes
        ):
            raise ValueError("complete terminal observation cannot omit source bytes")
        if (
            self.delivery_coverage is TerminalDeliveryCoverage.HEAD_TAIL
            and not self.omitted_by_delivery_bound_utf8_bytes
        ):
            raise ValueError("head-tail terminal observation must omit source bytes")
        if len(self.canonical_bytes()) > TERMINAL_OBSERVATION_CANONICAL_HARD_BYTES:
            raise ValueError("terminal observation exceeds canonical byte bound")

    def canonical_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "monitor_id": self.monitor_id,
            "process_id": self.process_id,
            "observation_ordinal": self.observation_ordinal,
            "observation_kind": self.observation_kind.value,
            "process_status": self.process_status,
            "exit_code": self.exit_code,
            "output_disposition": self.output_disposition,
            "gap_before_output": self.gap_before_output,
            "delivery_coverage": self.delivery_coverage.value,
            "available_source_utf8_bytes": self.available_source_utf8_bytes,
            "included_source_utf8_bytes": self.included_source_utf8_bytes,
            "omitted_by_delivery_bound_utf8_bytes": (
                self.omitted_by_delivery_bound_utf8_bytes
            ),
            "output": self.output,
            "host_scoped": self.host_scoped,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_mapping(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class TerminalObservationInstallationAttempt:
    session_id: str
    workspace_id: str
    writer_generation: int
    content: TerminalObservationContentV1
    content_digest: str
    retained_from_cursor: str
    through_cursor: str
    target: PreparedInstallationTarget
    occurred_at: datetime
    actor_id: str
    candidate_fingerprint: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.session_id,
                self.workspace_id,
                self.actor_id,
                self.retained_from_cursor,
                self.through_cursor,
                self.candidate_fingerprint,
            )
        ):
            raise ValueError("terminal observation installation is incomplete")
        if self.writer_generation < 1:
            raise ValueError("terminal observation writer generation is invalid")
        if not self.content_digest.startswith("sha256:"):
            raise ValueError("terminal observation digest is invalid")


__all__ = [
    "ExistingTurnInstallation",
    "NewTurnInstallation",
    "PreparedInstallationTarget",
    "TERMINAL_OBSERVATION_CANONICAL_HARD_BYTES",
    "TerminalDeliveryCoverage",
    "TerminalObservationContentV1",
    "TerminalObservationInstallationAttempt",
    "TerminalObservationKind",
]
