"""Pre-parse byte and JSON-shape bounds for MCP transports."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True, slots=True)
class McpWireBounds:
    maximum_stdio_frame_bytes: int = 16 * 1024 * 1024
    maximum_http_json_body_bytes: int = 16 * 1024 * 1024
    maximum_sse_event_data_bytes: int = 16 * 1024 * 1024
    maximum_buffered_transport_bytes_per_slot: int = 32 * 1024 * 1024
    maximum_wire_json_nodes: int = 65_536
    maximum_wire_json_depth: int = 128
    maximum_schema_utf8_bytes: int = 256 * 1024
    maximum_schema_nodes: int = 4_096
    maximum_schema_depth: int = 64
    maximum_discovery_candidate_bytes_per_server: int = 32 * 1024 * 1024
    maximum_discovery_candidate_bytes_per_host: int = 128 * 1024 * 1024


DEFAULT_MCP_WIRE_BOUNDS = McpWireBounds()


class McpWireBoundExceeded(ValueError):
    pass


def bounded_json_loads(
    data: bytes | bytearray,
    *,
    maximum_bytes: int,
    maximum_nodes: int,
    maximum_depth: int,
) -> Any:
    if len(data) > maximum_bytes:
        raise McpWireBoundExceeded("MCP JSON body exceeds the byte bound")
    # Prove the structural bound before ``json.loads`` allocates the Python
    # object graph.  The scanner is a small JSON grammar, not a heuristic
    # bracket counter: quoted delimiters, escapes, numbers and object keys are
    # all handled explicitly, while its only retained state is the bounded
    # recursion stack and counters.
    _BoundedJsonScanner(
        data,
        maximum_nodes=maximum_nodes,
        maximum_depth=maximum_depth,
    ).scan()
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("MCP wire JSON is invalid") from exc
    validate_json_shape(
        value, maximum_nodes=maximum_nodes, maximum_depth=maximum_depth
    )
    return value


class _BoundedJsonScanner:
    _WHITESPACE = frozenset(b" \t\r\n")
    _HEX = frozenset(b"0123456789abcdefABCDEF")

    def __init__(
        self,
        data: bytes | bytearray,
        *,
        maximum_nodes: int,
        maximum_depth: int,
    ) -> None:
        if maximum_nodes < 1 or maximum_depth < 1:
            raise ValueError("MCP JSON structural bounds must be positive")
        self._data = data
        self._length = len(data)
        self._position = 0
        self._nodes = 0
        self._maximum_nodes = maximum_nodes
        self._maximum_depth = maximum_depth

    def scan(self) -> None:
        self._skip_whitespace()
        if self._position >= self._length:
            raise ValueError("MCP wire JSON is invalid")
        try:
            self._scan_value(1)
        except RecursionError as exc:  # defensive if a future bound drifts
            raise McpWireBoundExceeded("MCP JSON depth bound exceeded") from exc
        self._skip_whitespace()
        if self._position != self._length:
            raise ValueError("MCP wire JSON is invalid")

    def _add_node(self, depth: int) -> None:
        if depth > self._maximum_depth:
            raise McpWireBoundExceeded("MCP JSON depth bound exceeded")
        self._nodes += 1
        if self._nodes > self._maximum_nodes:
            raise McpWireBoundExceeded("MCP JSON node bound exceeded")

    def _scan_value(self, depth: int) -> None:
        self._add_node(depth)
        if self._position >= self._length:
            raise ValueError("MCP wire JSON is invalid")
        current = self._data[self._position]
        if current == ord("{"):
            self._scan_object(depth)
        elif current == ord("["):
            self._scan_array(depth)
        elif current == ord('"'):
            self._scan_string()
        elif current == ord("t"):
            self._scan_literal(b"true")
        elif current == ord("f"):
            self._scan_literal(b"false")
        elif current == ord("n"):
            self._scan_literal(b"null")
        elif current == ord("-") or ord("0") <= current <= ord("9"):
            self._scan_number()
        else:
            raise ValueError("MCP wire JSON is invalid")

    def _scan_object(self, depth: int) -> None:
        self._position += 1
        self._skip_whitespace()
        if self._consume(ord("}")):
            return
        while True:
            if self._position >= self._length or self._data[self._position] != ord('"'):
                raise ValueError("MCP wire JSON is invalid")
            # ``validate_json_shape`` counts mapping keys as nodes, so the
            # pre-allocation quote uses the same definition.
            self._add_node(depth + 1)
            self._scan_string()
            self._skip_whitespace()
            if not self._consume(ord(":")):
                raise ValueError("MCP wire JSON is invalid")
            self._skip_whitespace()
            self._scan_value(depth + 1)
            self._skip_whitespace()
            if self._consume(ord("}")):
                return
            if not self._consume(ord(",")):
                raise ValueError("MCP wire JSON is invalid")
            self._skip_whitespace()

    def _scan_array(self, depth: int) -> None:
        self._position += 1
        self._skip_whitespace()
        if self._consume(ord("]")):
            return
        while True:
            self._scan_value(depth + 1)
            self._skip_whitespace()
            if self._consume(ord("]")):
                return
            if not self._consume(ord(",")):
                raise ValueError("MCP wire JSON is invalid")
            self._skip_whitespace()

    def _scan_string(self) -> None:
        if not self._consume(ord('"')):
            raise ValueError("MCP wire JSON is invalid")
        while self._position < self._length:
            current = self._data[self._position]
            self._position += 1
            if current == ord('"'):
                return
            if current < 0x20:
                raise ValueError("MCP wire JSON is invalid")
            if current != ord("\\"):
                continue
            if self._position >= self._length:
                raise ValueError("MCP wire JSON is invalid")
            escaped = self._data[self._position]
            self._position += 1
            if escaped in b'"\\/bfnrt':
                continue
            if escaped != ord("u") or self._position + 4 > self._length:
                raise ValueError("MCP wire JSON is invalid")
            if any(
                item not in self._HEX
                for item in self._data[self._position : self._position + 4]
            ):
                raise ValueError("MCP wire JSON is invalid")
            self._position += 4
        raise ValueError("MCP wire JSON is invalid")

    def _scan_literal(self, literal: bytes) -> None:
        end = self._position + len(literal)
        if self._data[self._position : end] != literal:
            raise ValueError("MCP wire JSON is invalid")
        self._position = end

    def _scan_number(self) -> None:
        if self._consume(ord("-")) and self._position >= self._length:
            raise ValueError("MCP wire JSON is invalid")
        if self._consume(ord("0")):
            if self._position < self._length and ord("0") <= self._data[
                self._position
            ] <= ord("9"):
                raise ValueError("MCP wire JSON is invalid")
        else:
            if self._position >= self._length or not (
                ord("1") <= self._data[self._position] <= ord("9")
            ):
                raise ValueError("MCP wire JSON is invalid")
            self._position += 1
            while self._position < self._length and ord("0") <= self._data[
                self._position
            ] <= ord("9"):
                self._position += 1
        if self._consume(ord(".")):
            self._scan_digits()
        if self._position < self._length and self._data[self._position] in b"eE":
            self._position += 1
            if self._position < self._length and self._data[self._position] in b"+-":
                self._position += 1
            self._scan_digits()

    def _scan_digits(self) -> None:
        start = self._position
        while self._position < self._length and ord("0") <= self._data[
            self._position
        ] <= ord("9"):
            self._position += 1
        if self._position == start:
            raise ValueError("MCP wire JSON is invalid")

    def _skip_whitespace(self) -> None:
        while (
            self._position < self._length
            and self._data[self._position] in self._WHITESPACE
        ):
            self._position += 1

    def _consume(self, expected: int) -> bool:
        if self._position < self._length and self._data[self._position] == expected:
            self._position += 1
            return True
        return False


def validate_json_shape(
    value: object, *, maximum_nodes: int, maximum_depth: int
) -> int:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > maximum_nodes:
            raise McpWireBoundExceeded("MCP JSON node bound exceeded")
        if depth > maximum_depth:
            raise McpWireBoundExceeded("MCP JSON depth bound exceeded")
        if isinstance(current, dict):
            stack.extend((key, depth + 1) for key in current)
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif not isinstance(current, str | int | float | bool | type(None)):
            raise ValueError("MCP JSON contains a non-JSON value")
    return nodes


def validate_schema(value: object, bounds: McpWireBounds) -> None:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > bounds.maximum_schema_utf8_bytes:
        raise McpWireBoundExceeded("MCP tool schema exceeds the byte bound")
    validate_json_shape(
        value,
        maximum_nodes=bounds.maximum_schema_nodes,
        maximum_depth=bounds.maximum_schema_depth,
    )


def result_type_presence(value: object) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        return False, None
    result = value.get("result")
    if not isinstance(result, dict) or "resultType" not in result:
        return False, None
    raw = result["resultType"]
    return True, raw if isinstance(raw, str) else None


__all__ = [
    "DEFAULT_MCP_WIRE_BOUNDS",
    "McpWireBoundExceeded",
    "McpWireBounds",
    "bounded_json_loads",
    "result_type_presence",
    "validate_json_shape",
    "validate_schema",
]
