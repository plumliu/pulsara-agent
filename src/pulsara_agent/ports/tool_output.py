"""A result's frozen UTF-8 body and its exact, scope-bound read boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Protocol

from pulsara_agent.identity import require_id
from pulsara_agent.ports.session_content import session_entry_target, session_entry_target_schema
from pulsara_agent.ports.tool_execution import (
    ToolOutputSourceCoverage,
    ToolOutputSourceCoverageReason,
    ToolOutputSourceFormatHint,
    freeze_tool_json_object,
)


class ToolOutputUnavailableReason(StrEnum):
    BODY_TOO_LARGE = "BODY_TOO_LARGE"
    BODY_PUBLICATION_FAILED = "BODY_PUBLICATION_FAILED"
    BODY_PUBLICATION_UNCONFIRMED = "BODY_PUBLICATION_UNCONFIRMED"


class ToolOutputContentError(RuntimeError):
    """The exact accepted source exists, but its body cannot be read faithfully."""


@dataclass(frozen=True, slots=True)
class ToolOutputBodyCandidate:
    text: str
    source_format: ToolOutputSourceFormatHint
    source_coverage: ToolOutputSourceCoverage
    source_coverage_reason: ToolOutputSourceCoverageReason | None = None

    def __post_init__(self) -> None:
        self.text.encode("utf-8")
        if not isinstance(self.source_format, ToolOutputSourceFormatHint):
            raise TypeError("output body format must be explicit TEXT or JSON")
        if not isinstance(self.source_coverage, ToolOutputSourceCoverage):
            raise TypeError("output source coverage must use its closed vocabulary")
        if (self.source_coverage is ToolOutputSourceCoverage.COMPLETE) != (
            self.source_coverage_reason is None
        ):
            raise ValueError("output body coverage and reason do not agree")
        if self.source_coverage_reason is not None and not isinstance(
            self.source_coverage_reason, ToolOutputSourceCoverageReason
        ):
            raise TypeError("output coverage reason must use its closed vocabulary")


@dataclass(frozen=True, slots=True)
class ToolOutputBodyView:
    target: Mapping[str, object]
    content: bytes
    media_type: str
    codec: str
    source_coverage: ToolOutputSourceCoverage
    source_coverage_reason: ToolOutputSourceCoverageReason | None = None
    model_visible_memory_fact_ids: tuple[str, ...] = ()
    required_facts: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.codec != "utf-8":
            raise ToolOutputContentError("output_body_codec_failed")
        self.content.decode("utf-8")
        object.__setattr__(self, "target", freeze_tool_json_object(self.target))
        object.__setattr__(self, "required_facts", freeze_tool_json_object(self.required_facts))


class ToolOutputReadPort(Protocol):
    def read_body(self, target: Mapping[str, object]) -> ToolOutputBodyView: ...


def session_entry_source_target_schema() -> dict[str, object]:
    """Share the existing Target; exact result reads do not grant history access."""
    return session_entry_target_schema()


def parse_tool_output_target(
    target: Mapping[str, object], *, session_id: str
) -> dict[str, str]:
    if not isinstance(target, Mapping) or set(target) - {"kind", "session_id", "entry_id"}:
        raise ValueError("output source must be a SESSION_ENTRY Target")
    if target.get("kind") != "SESSION_ENTRY" or "entry_id" not in target:
        raise ValueError("output source must be a SESSION_ENTRY Target")
    require_id(session_id, "session")
    require_id(target["entry_id"], "entry")
    selected_session = target.get("session_id", session_id)
    require_id(selected_session, "session")
    if selected_session != session_id:
        raise KeyError("output source is outside this session")
    return session_entry_target(session_id, target["entry_id"])


def tool_output_read_call(session_id: str, entry_id: str) -> dict[str, object]:
    require_id(session_id, "session")
    require_id(entry_id, "entry")
    return {"tool": "read_file", "arguments": {"target": session_entry_target(session_id, entry_id)}}


__all__ = [
    "ToolOutputBodyCandidate", "ToolOutputBodyView", "ToolOutputContentError",
    "ToolOutputReadPort", "ToolOutputUnavailableReason", "parse_tool_output_target",
    "session_entry_source_target_schema", "tool_output_read_call",
]
