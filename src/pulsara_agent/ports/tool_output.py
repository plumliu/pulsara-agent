"""Complete accepted tool values and their exact session-scoped read boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Mapping, Protocol

from pulsara_agent.identity import require_id
from pulsara_agent.ports.tool_execution import (FrozenToolJsonDict, encode_tool_public_json, freeze_tool_json_object,
    ToolOutputSourceCoverage, ToolOutputSourceCoverageReason, ToolOutputSourceFormatHint)


class ToolOutputUnavailableReason(StrEnum):
    BODY_PUBLICATION_FAILED = "BODY_PUBLICATION_FAILED"
    BODY_PUBLICATION_UNCONFIRMED = "BODY_PUBLICATION_UNCONFIRMED"


class ToolResultSaveError(RuntimeError):
    """Process-local known outcome whose complete save failed or is uncertain.

    No readable result identity is attached. Retaining this value is diagnostic
    context for the existing error channel, never an accepted-result receipt.
    """

    def __init__(self, *, reason: ToolOutputUnavailableReason,
                 known_value: Mapping[str, object], result_state: str, cause: Exception) -> None:
        if not isinstance(reason, ToolOutputUnavailableReason):
            raise TypeError("result save error requires the actual publication reason")
        self.reason = reason
        self.known_value = freeze_tool_json_object(known_value)
        self.result_state = result_state
        self.cause = cause
        failure = ("完整结果保存失败" if reason is ToolOutputUnavailableReason.BODY_PUBLICATION_FAILED
                   else "完整结果保存状态未确认")
        super().__init__(f"操作结果已取得（执行状态：{result_state}），但{failure}；不应重做业务操作。")


class ToolOutputContentError(RuntimeError):
    """An accepted result cannot be read completely and faithfully."""


class AcceptedToolResultProjectionError(ToolOutputContentError):
    """A known, accepted value whose display projection failed locally."""

    def __init__(self, *, known_value: Mapping[str, object], result_state: str,
                 result_id: str, result_entry_id: str, cause: Exception,
                 source_result_entry_id: str | None = None, next_target: Mapping[str, object] | None = None) -> None:
        self.result_id = require_id(result_id, "result")
        self.result_entry_id = require_id(result_entry_id, "entry")
        self.known_value = freeze_tool_json_object(known_value)
        self.result_state = result_state
        self.cause = cause
        self.source_result_entry_id = (result_entry_id if source_result_entry_id is None
            else require_id(source_result_entry_id, "entry"))
        self.next_target = None if next_target is None else freeze_tool_json_object(next_target)
        detail = str(cause).encode("utf-8", errors="backslashreplace").decode("utf-8")
        continuation = "" if next_target is None else "; next: " + encode_tool_public_json(dict(next_target)).decode("utf-8")
        super().__init__(
            f"操作结果已取得（执行状态：{result_state}），完整结果已保存，"
            f'但本次展示失败：{detail}；完整结果 Target：'
            f'{{"kind":"SESSION_ENTRY","entry_id":"{self.source_result_entry_id}"}}{continuation}；不应重做业务操作。'
        )


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
class ToolResultView:
    target: Mapping[str, object]
    value: FrozenToolJsonDict
    result_id: str | None = None
    source_tool_name: str = ""
    source_arguments: Mapping[str, object] = field(default_factory=dict)
    observed_at: datetime | None = None
    observation_duration_microseconds: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", freeze_tool_json_object(self.target))
        object.__setattr__(self, "value", freeze_tool_json_object(self.value))
        object.__setattr__(self, "source_arguments", freeze_tool_json_object(self.source_arguments))


@dataclass(frozen=True, slots=True)
class NativeToolContinuation:
    source: ToolResultView
    position: tuple[int, int]

    def __post_init__(self):
        if not isinstance(self.source, ToolResultView):
            raise TypeError("native continuation needs an actual saved tool result")
        if len(self.position) != 2 or any(type(item) is not int or item < 0 for item in self.position):
            raise ValueError("native continuation position is invalid")


class ToolOutputReadPort(Protocol):
    def read_result(self, target: Mapping[str, object]) -> ToolResultView: ...
    def read_native_page(self, target: Mapping[str, object]) -> NativeToolContinuation: ...


def next_target_schema() -> dict[str, object]:
    """The opaque continuation is copied unchanged; its position is not public."""
    return {"type": "object", "properties": {"kind": {"const": "NEXT"}, "token": {"type": "string"}},
            "required": ["kind", "token"], "additionalProperties": False}


def session_entry_source_target_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {"kind": {"const": "SESSION_ENTRY"}, "entry_id": {"type": "string"}},
        "required": ["kind", "entry_id"],
        "additionalProperties": False,
    }


def parse_tool_output_target(target: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(target, Mapping) or set(target) != {"kind", "entry_id"}:
        raise ValueError("tool result must be a minimal SESSION_ENTRY Target")
    if target["kind"] != "SESSION_ENTRY":
        raise ValueError("tool result must be a SESSION_ENTRY Target")
    require_id(target["entry_id"], "entry")
    return {"kind": "SESSION_ENTRY", "entry_id": target["entry_id"]}


__all__ = [
    "ToolResultView", "NativeToolContinuation", "ToolResultSaveError", "AcceptedToolResultProjectionError",
    "ToolOutputBodyCandidate", "ToolOutputContentError", "ToolOutputReadPort",
    "ToolOutputUnavailableReason", "parse_tool_output_target", "session_entry_source_target_schema", "next_target_schema",
]
