"""Complete accepted tool values and their exact session-scoped read boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol

from pulsara_agent.identity import require_id
from pulsara_agent.ports.tool_execution import (FrozenToolJsonDict, freeze_tool_json_object,
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
                 result_id: str, result_entry_id: str, cause: Exception) -> None:
        self.result_id = require_id(result_id, "result")
        self.result_entry_id = require_id(result_entry_id, "entry")
        self.known_value = freeze_tool_json_object(known_value)
        self.result_state = result_state
        self.cause = cause
        detail = str(cause).encode("utf-8", errors="backslashreplace").decode("utf-8")
        super().__init__(
            f"操作结果已取得（执行状态：{result_state}），完整结果已保存，"
            f'但本次展示失败：{detail}；完整结果 Target：'
            f'{{"kind":"SESSION_ENTRY","entry_id":"{result_entry_id}"}}；不应重做业务操作。'
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", freeze_tool_json_object(self.target))
        object.__setattr__(self, "value", freeze_tool_json_object(self.value))


class ToolOutputReadPort(Protocol):
    def read_result(self, target: Mapping[str, object]) -> ToolResultView: ...


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
    "ToolResultView", "ToolResultSaveError", "AcceptedToolResultProjectionError",
    "ToolOutputBodyCandidate", "ToolOutputContentError", "ToolOutputReadPort",
    "ToolOutputUnavailableReason", "parse_tool_output_target", "session_entry_source_target_schema",
]
