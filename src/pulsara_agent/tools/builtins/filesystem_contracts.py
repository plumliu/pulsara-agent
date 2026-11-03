"""Closed model-facing filesystem schemas shared with the builtin catalog."""

from __future__ import annotations

from pulsara_agent.identity import REVISION_PATTERN
from pulsara_agent.ports.tool_output import session_entry_source_target_schema


DEFAULT_READ_LINES = 2_000
MAX_READ_LINES = 2_000
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 1_000


def path_target_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "const": "PATH"},
            "path": {"type": "string", "minLength": 1},
        },
        "required": ["kind", "path"],
        "additionalProperties": False,
    }


def _object(properties: dict[str, object], required: list[str]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _text_continuation(*, path: bool) -> dict[str, object]:
    properties: dict[str, object] = {
        "line": {"type": "integer", "minimum": 1},
        "char_offset": {"type": "integer", "minimum": 0},
    }
    required = ["line", "char_offset"]
    if path:
        properties["content_revision"] = {
            "type": "string",
            "pattern": "^" + REVISION_PATTERN + "$",
        }
        required.append("content_revision")
    return _object(properties, required)


def read_file_parameters_schema() -> dict[str, object]:
    limit = {
        "type": "integer",
        "minimum": 1,
        "maximum": MAX_READ_LINES,
        "default": DEFAULT_READ_LINES,
    }
    return {
        "oneOf": [
            _object(
                {
                    "target": path_target_schema(),
                    "offset": {"type": "integer", "minimum": 1, "default": 1},
                    "limit": limit,
                },
                ["target"],
            ),
            _object(
                {
                    "target": path_target_schema(),
                    "limit": limit,
                    "continuation": _text_continuation(path=True),
                },
                ["target", "continuation"],
            ),
            _object({"target": session_entry_source_target_schema()}, ["target"]),
            _object(
                {
                    "target": session_entry_source_target_schema(),
                    "continuation": _text_continuation(path=False),
                },
                ["target", "continuation"],
            ),
        ]
    }


def write_file_parameters_schema() -> dict[str, object]:
    return {
        "oneOf": [
            _object(
                {"target": path_target_schema(), "content": {"type": "string"}},
                ["target", "content"],
            ),
            _object(
                {
                    "target": path_target_schema(),
                    "source": session_entry_source_target_schema(),
                },
                ["target", "source"],
            ),
        ]
    }


def _search_continuation(*, content: bool) -> dict[str, object]:
    properties: dict[str, object] = {
        "path": {"type": "string", "minLength": 1},
        "partial": {"type": "boolean"},
    }
    if content:
        properties["line"] = {"type": "integer", "minimum": 1}
    return _object(properties, ["path", "partial"])


def _search_properties() -> dict[str, object]:
    return {
        "target": path_target_schema(),
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_SEARCH_LIMIT,
            "default": DEFAULT_SEARCH_LIMIT,
        },
    }


def search_content_parameters_schema() -> dict[str, object]:
    common = {
        **_search_properties(),
        "pattern": {"type": "string", "minLength": 1},
        "file_glob": {"type": "string", "minLength": 1},
    }
    return {
        "oneOf": [
            _object(
                {
                    **common,
                    "output_mode": {
                        "type": "string",
                        "enum": ["content"],
                        "default": "content",
                    },
                    "continuation": _search_continuation(content=True),
                },
                ["pattern"],
            ),
            _object(
                {
                    **common,
                    "output_mode": {"type": "string", "enum": ["files_only", "count"]},
                    "continuation": _search_continuation(content=False),
                },
                ["pattern", "output_mode"],
            ),
        ]
    }


def find_files_parameters_schema() -> dict[str, object]:
    return _object(
        {
            **_search_properties(),
            "glob": {"type": "string", "minLength": 1},
            "continuation": _search_continuation(content=False),
        },
        ["glob"],
    )
