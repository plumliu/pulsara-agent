"""Canonical content for one formal Cell output record."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Mapping

from pulsara_agent.llm.input import LLMTextPart
from pulsara_agent.identity import require_id
from pulsara_agent.ports.tool_execution import (
    freeze_tool_json_object,
    thaw_tool_json_object,
)


CELL_OUTPUT_MEDIA_TYPE = "application/vnd.pulsara.terminal-observation+json"
CELL_OUTPUT_CODEC = "utf-8"


def cell_output_provider_text(text: str) -> str:
    """Project JS code units to legal provider text without changing stored data."""
    return text.encode("utf-8", errors="backslashreplace").decode("utf-8")


@dataclass(frozen=True, slots=True)
class CellOutputResourceQuote:
    canonical_expanded_bytes: int


@dataclass(frozen=True, slots=True)
class CellOutputProviderContent:
    parts: tuple[LLMTextPart, ...]


@dataclass(frozen=True, slots=True)
class CellOutputContent:
    """One immutable formal Cell record at the runtime/canonical boundary."""

    cell_id: str
    source_turn_id: str
    source_exec_attempt_id: str
    record_sequence: int
    record: Mapping[str, object] = field(repr=False)
    body: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        require_id(self.cell_id, "cell")
        require_id(self.source_turn_id, "turn")
        require_id(self.source_exec_attempt_id, "attempt")
        if type(self.record_sequence) is not int or self.record_sequence < 1:
            raise ValueError("Cell output record sequence is invalid")
        if not isinstance(self.record, Mapping):
            raise TypeError("Cell output record must be an object")
        object.__setattr__(self, "record", freeze_tool_json_object(self.record))
        payload = {
            "parts": [
                {"type": "text", "text": cell_output_provider_text(self.storage_text)}
            ]
        }
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        object.__setattr__(self, "body", body)

    @property
    def storage_kind(self) -> str:
        kind = self.record.get("kind")
        if (
            kind in {"log", "return"}
            and set(self.record) == {"kind", "text"}
            and isinstance(self.record["text"], str)
        ):
            return kind
        if (
            kind == "error"
            and set(self.record) == {"kind", "error"}
            and isinstance(self.record["error"], Mapping)
        ):
            return "diagnostic"
        if kind == "tool_result":
            return "tool_result"
        raise ValueError("Cell output record has an unsupported shape")

    @property
    def storage_text(self) -> str:
        """The sole readable payload; source and record kind stay with the queue."""
        if self.storage_kind in {"log", "return"}:
            return self.record["text"]
        value = thaw_tool_json_object(self.record)
        payload = (
            value["error"]
            if self.storage_kind == "diagnostic"
            else {k: v for k, v in value.items() if k != "kind"}
        )
        return json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_storage(
        cls,
        *,
        cell_id: str,
        source_turn_id: str,
        source_exec_attempt_id: str,
        record_sequence: int,
        kind: str,
        text: str,
    ) -> "CellOutputContent":
        if kind in {"log", "return"}:
            record = dict(kind=kind, text=text)
        elif kind == "diagnostic":
            record = dict(kind="error", error=json.loads(text))
        elif kind == "tool_result":
            record = dict(json.loads(text), kind="tool_result")
        else:
            raise ValueError("Cell output kind is invalid")
        value = cls(
            cell_id, source_turn_id, source_exec_attempt_id, record_sequence, record
        )
        if value.storage_kind != kind or value.storage_text != text:
            raise ValueError("Cell output payload does not match its record kind")
        return value

    @property
    def content(self) -> CellOutputProviderContent:
        return CellOutputProviderContent(
            (LLMTextPart(cell_output_provider_text(self.storage_text)),)
        )

    @property
    def resource_quote(self) -> CellOutputResourceQuote:
        return CellOutputResourceQuote(len(self.body))

    @property
    def image_occurrences(self) -> tuple:
        return ()
