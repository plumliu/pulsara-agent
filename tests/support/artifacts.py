"""Test-owned tool-result artifact index."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulsara_agent.runtime.tool_artifacts import ToolResultArtifactRecord


@dataclass(slots=True)
class FakeToolResultArtifactIndex:
    records: dict[str, ToolResultArtifactRecord] = field(default_factory=dict)

    def put(self, record: ToolResultArtifactRecord) -> None:
        existing = self.records.get(record.id)
        if existing is not None and existing != record:
            raise ValueError(
                f"tool result artifact record {record.id!r} already exists with different data"
            )
        self.records[record.id] = record

    def get_for_session(
        self, artifact_id: str, *, session_id: str
    ) -> ToolResultArtifactRecord | None:
        matches = [
            record
            for record in self.records.values()
            if record.artifact_id == artifact_id and record.session_id == session_id
        ]
        if not matches:
            return None
        return sorted(
            matches,
            key=lambda record: (record.run_id, record.tool_call_id, record.ordinal),
        )[0]


__all__ = ["FakeToolResultArtifactIndex"]
