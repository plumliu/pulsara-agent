"""Allocation-aware JSON decoding shared by sealed wire/body readers."""

from __future__ import annotations

import json
from typing import Any


class JsonBoundExceeded(ValueError):
    pass


def bounded_json_loads(
    data: bytes | bytearray,
    *,
    maximum_bytes: int,
    maximum_nodes: int,
    maximum_depth: int,
    maximum_string_utf8_bytes: int | None = None,
) -> Any:
    if len(data) > maximum_bytes:
        raise JsonBoundExceeded("JSON body exceeds the byte bound")
    _BoundedJsonScanner(
        data,
        maximum_nodes=maximum_nodes,
        maximum_depth=maximum_depth,
        maximum_string_utf8_bytes=maximum_string_utf8_bytes,
    ).scan()
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("JSON body is invalid") from exc
    validate_json_shape(
        value,
        maximum_nodes=maximum_nodes,
        maximum_depth=maximum_depth,
        maximum_string_utf8_bytes=maximum_string_utf8_bytes,
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
        maximum_string_utf8_bytes: int | None,
    ) -> None:
        if maximum_nodes < 1 or maximum_depth < 1:
            raise ValueError("JSON structural bounds must be positive")
        self._data = data
        self._length = len(data)
        self._position = 0
        self._nodes = 0
        self._maximum_nodes = maximum_nodes
        self._maximum_depth = maximum_depth
        self._maximum_string_utf8_bytes = maximum_string_utf8_bytes

    def scan(self) -> None:
        self._skip_whitespace()
        if self._position >= self._length:
            raise ValueError("JSON body is invalid")
        try:
            self._scan_value(1)
        except RecursionError as exc:
            raise JsonBoundExceeded("JSON depth bound exceeded") from exc
        self._skip_whitespace()
        if self._position != self._length:
            raise ValueError("JSON body is invalid")

    def _add_node(self, depth: int) -> None:
        if depth > self._maximum_depth:
            raise JsonBoundExceeded("JSON depth bound exceeded")
        self._nodes += 1
        if self._nodes > self._maximum_nodes:
            raise JsonBoundExceeded("JSON node bound exceeded")

    def _scan_value(self, depth: int) -> None:
        self._add_node(depth)
        if self._position >= self._length:
            raise ValueError("JSON body is invalid")
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
            raise ValueError("JSON body is invalid")

    def _scan_object(self, depth: int) -> None:
        self._position += 1
        self._skip_whitespace()
        if self._consume(ord("}")):
            return
        while True:
            if self._position >= self._length or self._data[self._position] != ord('"'):
                raise ValueError("JSON body is invalid")
            self._add_node(depth + 1)
            self._scan_string()
            self._skip_whitespace()
            if not self._consume(ord(":")):
                raise ValueError("JSON body is invalid")
            self._skip_whitespace()
            self._scan_value(depth + 1)
            self._skip_whitespace()
            if self._consume(ord("}")):
                return
            if not self._consume(ord(",")):
                raise ValueError("JSON body is invalid")
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
                raise ValueError("JSON body is invalid")
            self._skip_whitespace()

    def _scan_string(self) -> None:
        start = self._position
        if not self._consume(ord('"')):
            raise ValueError("JSON body is invalid")
        while self._position < self._length:
            current = self._data[self._position]
            self._position += 1
            if current == ord('"'):
                if (
                    self._maximum_string_utf8_bytes is not None
                    and self._position - start - 2
                    > self._maximum_string_utf8_bytes * 6
                ):
                    # Escaping can expand a UTF-8 string by at most six source
                    # bytes per decoded code point.  This conservative pre-read
                    # fence is followed by an exact decoded-byte validation.
                    raise JsonBoundExceeded("JSON string exceeds the byte bound")
                return
            if current < 0x20:
                raise ValueError("JSON body is invalid")
            if current != ord("\\"):
                continue
            if self._position >= self._length:
                raise ValueError("JSON body is invalid")
            escaped = self._data[self._position]
            self._position += 1
            if escaped in b'"\\/bfnrt':
                continue
            if escaped != ord("u") or self._position + 4 > self._length:
                raise ValueError("JSON body is invalid")
            if any(
                item not in self._HEX
                for item in self._data[self._position : self._position + 4]
            ):
                raise ValueError("JSON body is invalid")
            self._position += 4
        raise ValueError("JSON body is invalid")

    def _scan_literal(self, literal: bytes) -> None:
        if self._data[self._position : self._position + len(literal)] != literal:
            raise ValueError("JSON body is invalid")
        self._position += len(literal)

    def _scan_number(self) -> None:
        start = self._position
        if self._consume(ord("-")) and self._position >= self._length:
            raise ValueError("JSON body is invalid")
        if self._consume(ord("0")):
            if self._position < self._length and ord("0") <= self._data[self._position] <= ord("9"):
                raise ValueError("JSON body is invalid")
        else:
            if self._position >= self._length or not ord("1") <= self._data[self._position] <= ord("9"):
                raise ValueError("JSON body is invalid")
            while self._position < self._length and ord("0") <= self._data[self._position] <= ord("9"):
                self._position += 1
        if self._consume(ord(".")):
            digit_start = self._position
            while self._position < self._length and ord("0") <= self._data[self._position] <= ord("9"):
                self._position += 1
            if self._position == digit_start:
                raise ValueError("JSON body is invalid")
        if self._position < self._length and self._data[self._position] in b"eE":
            self._position += 1
            if self._position < self._length and self._data[self._position] in b"+-":
                self._position += 1
            digit_start = self._position
            while self._position < self._length and ord("0") <= self._data[self._position] <= ord("9"):
                self._position += 1
            if self._position == digit_start:
                raise ValueError("JSON body is invalid")
        if self._position == start:
            raise ValueError("JSON body is invalid")

    def _skip_whitespace(self) -> None:
        while self._position < self._length and self._data[self._position] in self._WHITESPACE:
            self._position += 1

    def _consume(self, expected: int) -> bool:
        if self._position < self._length and self._data[self._position] == expected:
            self._position += 1
            return True
        return False


def validate_json_shape(
    value: object,
    *,
    maximum_nodes: int,
    maximum_depth: int,
    maximum_string_utf8_bytes: int | None = None,
) -> int:
    nodes = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > maximum_nodes:
            raise JsonBoundExceeded("JSON node bound exceeded")
        if depth > maximum_depth:
            raise JsonBoundExceeded("JSON depth bound exceeded")
        if isinstance(current, dict):
            stack.extend((key, depth + 1) for key in current)
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str):
            if (
                maximum_string_utf8_bytes is not None
                and len(current.encode("utf-8")) > maximum_string_utf8_bytes
            ):
                raise JsonBoundExceeded("JSON string exceeds the byte bound")
        elif not isinstance(current, int | float | bool | type(None)):
            raise ValueError("JSON contains a non-JSON value")
    return nodes


__all__ = ["JsonBoundExceeded", "bounded_json_loads", "validate_json_shape"]
