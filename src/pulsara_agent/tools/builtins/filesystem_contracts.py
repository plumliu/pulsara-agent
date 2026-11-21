"""Closed file-tool business inputs; delivery paging is owned by NEXT."""

from pulsara_agent.ports.tool_output import session_entry_source_target_schema


def path_target_schema():
    return _object(
        {"kind": {"const": "PATH"}, "path": {"type": "string", "minLength": 1}},
        ["kind", "path"],
    )


def _object(properties, required):
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def read_file_parameters_schema(*, native=True):
    path = _object(
        {
            "target": path_target_schema(),
            "offset": {
                "type": "integer",
                "not": {"const": 0},
                "description": "Starting LF line (nonzero, default 1). Negative offsets count from the end. Reading proceeds forward.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "description": "Positive maximum number of lines selected by this call. Omit to read through the end of the file.",
            },
        },
        ["target"],
    )
    if not native:
        return path
    from pulsara_agent.ports.tool_output import next_target_schema

    next_target = next_target_schema()
    next_target["description"] = "NEXT Target returned by the native result page being continued."
    return {
        "type": "object",
        "oneOf": [path, _object({"target": next_target}, ["target"])],
    }


def write_file_parameters_schema():
    return {
        "type": "object",
        "oneOf": [
            _object(
                {"target": path_target_schema(), "content": {"type": "string"}},
                ["target", "content"],
            ),
            _object(
                {
                    "target": path_target_schema(),
                    "tool_result_target": session_entry_source_target_schema(),
                },
                ["target", "tool_result_target"],
            ),
        ],
    }


def edit_file_parameters_schema():
    return {
        "type": "object",
        "oneOf": [
            _object(
                {"target": path_target_schema(), "content": {"type": "string"}},
                ["target", "content"],
            ),
            _object(
                {
                    "target": path_target_schema(),
                    "old_text": {"type": "string"},
                    "new_text": {"type": "string"},
                },
                ["target", "old_text", "new_text"],
            ),
        ],
    }


def search_content_parameters_schema():
    return _object(
        {
            "target": path_target_schema(),
            "pattern": {"type": "string", "minLength": 1},
            "file_glob": {"type": "string", "minLength": 1},
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_only", "count"],
            },
        },
        ["pattern"],
    )


def find_files_parameters_schema():
    return _object(
        {"target": path_target_schema(), "glob": {"type": "string", "minLength": 1}},
        ["glob"],
    )


def file_tool_result_schema(name, *, output_mode=None):
    """Complete JS business values; native continuation is not a file result."""
    from pulsara_agent.ports.tool_execution import tool_business_value_schema

    fields = {"target": path_target_schema()}
    meta = {}
    content = None
    if name == "read_file":
        meta = {
            "total_lines": {
                "type": "integer",
                "minimum": 0,
                "description": "Total LF lines in the source file.",
            },
            "file_size": {"type": "integer", "minimum": 0},
            "had_utf8_bom": {"type": "boolean"},
        }
    elif name == "write_file":
        meta = {"bytes_written": {"type": "integer", "minimum": 0}}
    elif name == "edit_file":
        pass
    elif name in {"find_files", "search_file_content"}:
        text = _object({"type": {"const": "text"}, "value": {"type": "string"}}, ["type", "value"])
        target = _object({"type": {"const": "target"}, "value": path_target_schema()}, ["type", "value"])
        line = _object({"type": {"const": "object"}, "value": _object({
            "target": path_target_schema(), "line": {"type": "integer", "minimum": 1},
            "text": {"type": "string"},
        }, ["target", "line", "text"])}, ["type", "value"])
        count = _object({"type": {"const": "object"}, "value": _object({
            "target": path_target_schema(), "count": {"type": "integer", "minimum": 1},
        }, ["target", "count"])}, ["type", "value"])
        if name == "find_files" or output_mode == "files_only":
            parts = [text, target]
        elif output_mode == "content":
            parts = [text, line]
        elif output_mode == "count":
            parts = [text, count]
        elif output_mode is None:
            parts = [text, target, line, count]
        else:
            raise ValueError("Unknown search output mode")
        content = {"type": "array", "items": {"oneOf": parts}}
    else:
        raise ValueError("Unknown file tool")
    return {"oneOf": [
        tool_business_value_schema(fields=fields, required_fields=("target",), meta=meta,
            required_meta=tuple(meta), content_schema=content),
        tool_business_value_schema(fields=fields, required_fields=("target",),
            content_schema=content, statuses=("ERROR", "CANCELLED")),
    ]}
