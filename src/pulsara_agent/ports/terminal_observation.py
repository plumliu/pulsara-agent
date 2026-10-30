"""Canonical content for one formal Cell output record."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Mapping

from pulsara_agent.llm.input import LLMTextPart
from pulsara_agent.identity import require_id
from pulsara_agent.ports.tool_execution import freeze_tool_json_object


CELL_OUTPUT_MEDIA_TYPE = "application/vnd.pulsara.terminal-observation+json"
CELL_OUTPUT_CODEC = "utf-8"


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
    record: Mapping[str, object] = field(repr=False, compare=False)
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
            "schema_version": "cell_output.v1",
            "cell_id": self.cell_id,
            "source_turn_id": self.source_turn_id,
            "source_exec_attempt_id": self.source_exec_attempt_id,
            "record_sequence": self.record_sequence,
            "record": dict(self.record),
        }
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except UnicodeEncodeError:
            # JavaScript strings can contain lone UTF-16 surrogates. Escape
            # the canonical JSON without replacing any formal output value.
            body = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        object.__setattr__(self, "body", body)

    def canonical_mapping(self) -> dict[str, object]:
        return json.loads(self.body)

    def canonical_bytes(self) -> bytes:
        return self.body

    @classmethod
    def from_bytes(cls, body: bytes) -> "CellOutputContent":
        value = json.loads(body)
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "cell_id",
                "source_turn_id",
                "source_exec_attempt_id",
                "record_sequence",
                "record",
            }
            or value["schema_version"] != "cell_output.v1"
        ):
            raise ValueError("Cell output canonical schema is invalid")
        result = cls(
            **{key: item for key, item in value.items() if key != "schema_version"}
        )
        if result.body != body:
            raise ValueError("Cell output storage is not canonical")
        return result

    @property
    def content(self) -> CellOutputProviderContent:
        return CellOutputProviderContent((LLMTextPart(self.body.decode("utf-8")),))

    @property
    def resource_quote(self) -> CellOutputResourceQuote:
        return CellOutputResourceQuote(len(self.body))

    @property
    def image_occurrences(self) -> tuple:
        return ()
