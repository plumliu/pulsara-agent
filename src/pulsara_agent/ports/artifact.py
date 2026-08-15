"""Closed product contracts for canonical tool-output artifacts.

The durable authority is the canonical ``tool_results`` edge plus the global
immutable blob.  Nothing in this module is an execution-recovery, receipt, or
projection contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pulsara_agent.ports.tool_execution import (
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
)


class ArtifactContentError(RuntimeError):
    """The canonical edge exists but its immutable content is unavailable."""


class ToolArtifactMode(StrEnum):
    """Existing descriptor policy vocabulary retained by the builtin catalog."""

    DEFAULT = "default"
    NEVER = "never"
    ALWAYS = "always"
    LARGE_OUTPUT = "large_output"
    STRUCTURED_JSON = "structured_json"


class ToolOutputArtifactDisposition(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    AVAILABLE = "AVAILABLE"
    INCOMPLETE = "INCOMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


class ToolResultDisplayKind(StrEnum):
    COMPLETE = "COMPLETE"
    HEAD_TAIL = "HEAD_TAIL"


class ToolOutputArtifactUnavailabilityReason(StrEnum):
    ARTIFACT_CONTENT_TOO_LARGE = "ARTIFACT_CONTENT_TOO_LARGE"
    BLOB_PUBLICATION_FAILED = "BLOB_PUBLICATION_FAILED"
    BLOB_PUBLICATION_UNCONFIRMED = "BLOB_PUBLICATION_UNCONFIRMED"


@dataclass(frozen=True, slots=True)
class ToolArtifactRecordView:
    artifact_id: str
    role: str
    media_type: str
    size_bytes: int
    artifact_disposition: ToolOutputArtifactDisposition
    source_coverage: ToolOutputSourceCoverage
    display_kind: ToolResultDisplayKind
    source_coverage_reason: ToolOutputSourceCoverageReason | None
    artifact_unavailability_reason: ToolOutputArtifactUnavailabilityReason | None
    blob_id: str
    digest: str
    codec: str
    accepted_at_utc: str
    model_visible_memory_fact_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolArtifactInfoView:
    record: ToolArtifactRecordView


@dataclass(frozen=True, slots=True)
class ToolArtifactTextSliceView:
    info: ToolArtifactInfoView
    text: str
    offset_chars: int
    returned_chars: int
    total_chars: int
    has_more: bool
    next_offset_chars: int | None


class ToolArtifactReadPort(Protocol):
    """Session/workspace-scoped, read-only canonical artifact capability."""

    def lookup(self, artifact_id: str) -> ToolArtifactRecordView | None: ...

    def info(self, artifact_id: str) -> ToolArtifactInfoView: ...

    def read_text(
        self, artifact_id: str, *, offset_chars: int, max_chars: int
    ) -> ToolArtifactTextSliceView: ...


__all__ = [
    "ArtifactContentError",
    "ToolArtifactInfoView",
    "ToolArtifactMode",
    "ToolArtifactReadPort",
    "ToolArtifactRecordView",
    "ToolArtifactTextSliceView",
    "ToolOutputArtifactDisposition",
    "ToolOutputArtifactUnavailabilityReason",
    "ToolResultDisplayKind",
]
