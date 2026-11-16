"""Thin media contracts; source decoding belongs to the media owners."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math

from pulsara_agent.tools.builtins.workspace import WorkspaceTool
from pulsara_agent.tools.builtins.filesystem import (
    path_target_arg,
)

MEDIA_TOOL_NAMES = frozenset({"read_video_frames", "read_transcript", "annotate_media"})
IMAGE_TOOL_NAMES = frozenset({"view_image", "read_video_frames"})


def media_business_output_schema(name: str) -> dict:
    """Describe the complete JS business value, including actual failure values."""
    from pulsara_agent.ports.tool_execution import tool_business_value_schema
    from pulsara_agent.tools.builtins.filesystem_contracts import path_target_schema

    def record(properties, required):
        return {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }

    def part(kind, value):
        return record({"type": {"const": kind}, "value": value}, ["type", "value"])

    def numbers(length):
        return {
            "type": "array",
            "items": {"type": "number"},
            "minItems": length,
            "maxItems": length,
        }

    text = part("text", {"type": "string"})
    image_target = media_parameters_schema("view_image")["properties"]["target"]
    image = part("image", image_target["oneOf"][1])
    target = path_target_schema()
    meta, required_meta = {}, ()
    if name == "view_image":
        target = image_target
        success_content = {
            "type": "array",
            "prefixItems": [text, image],
            "minItems": 2,
            "maxItems": 2,
        }
        failure_content = {"type": "array", "items": text}
    elif name == "read_video_frames":
        time_range = numbers(2)
        detail = record(
            {
                "t": {"type": "number"},
                "visible_range": time_range,
                "dt": {"type": "number"},
            },
            ["t", "visible_range"],
        )
        cell = record(
            {
                "n": {"type": "integer", "minimum": 1},
                "t": {"type": "number"},
                "cell_rect": numbers(4),
                "dt": {"type": "number"},
                "visible_range": time_range,
                "representative_range": time_range,
            },
            ["n", "t", "cell_rect"],
        )
        collage = record({"frames": {"type": "array", "items": cell}}, ["frames"])
        success_content = failure_content = {
            "type": "array",
            "items": {
                "oneOf": [text, image, part("object", {"oneOf": [detail, collage]})]
            },
        }
    elif name == "read_transcript":
        segment = record(
            {
                "from": {"type": "number"},
                "to": {"type": "number"},
                "text": {"type": "string"},
                "speaker": {"type": "string"},
                "visible_range": record(
                    {"from": {"type": "number"}, "to": {"type": "number"}},
                    ["from", "to"],
                ),
            },
            ["from", "to", "text"],
        )
        meta = {"source": {"enum": ["subtitle", "asr"]}, "language": {"type": "string"}}
        required_meta = ("source",)
        success_content = failure_content = {
            "type": "array",
            "items": {"oneOf": [text, part("object", segment)]},
        }
    else:
        raise ValueError("unknown JS media tool")
    return {
        "description": (
            "Complete JS business value. Business failures throw with this failure value "
            "in err.result when available; failures without a business value omit it. "
            "Image success is independent of the current model's image capability."
        ),
        "oneOf": [
            tool_business_value_schema(
                fields={"target": target},
                required_fields=("target",),
                meta=meta,
                required_meta=required_meta,
                content_schema=success_content,
            ),
            tool_business_value_schema(
                fields={"target": target},
                meta=meta,
                content_schema=failure_content,
                statuses=("ERROR", "CANCELLED"),
            ),
        ],
    }


def media_parameters_schema(name: str) -> dict:
    if name == "view_image":
        from pulsara_agent.identity import ID_PAYLOAD_PATTERN
        from pulsara_agent.tools.builtins.filesystem_contracts import path_target_schema

        return {
            "type": "object",
            "properties": {
                "target": {
                    "oneOf": [
                        path_target_schema(),
                        {
                            "type": "object",
                            "properties": {
                                "kind": {"const": "IMAGE_REF"},
                                "image_ref": {
                                    "type": "string",
                                    "pattern": f"^image:{ID_PAYLOAD_PATTERN}$",
                                },
                            },
                            "required": ["kind", "image_ref"],
                            "additionalProperties": False,
                        },
                    ]
                }
            },
            "required": ["target"],
            "additionalProperties": False,
        }
    properties = {
        "target": {
            "type": "object",
            "properties": {
                "kind": {"const": "PATH", "type": "string"},
                "path": {"type": "string", "minLength": 1},
            },
            "required": ["kind", "path"],
            "additionalProperties": False,
        },
        "from": {"type": "number", "minimum": 0},
        "to": {"type": "number", "exclusiveMinimum": 0},
    }
    required = ["target"]
    if name == "read_video_frames":
        properties["from"]["description"] = (
            "Start in seconds (decimals supported). Defaults to 0 for overview/motion; required for detail."
        )
        properties["to"]["description"] = (
            "Exclusive end in seconds. Defaults to media end for overview/motion; detail requires an explicit end at most 2 seconds after from."
        )
        properties.update(
            action={
                "type": "string",
                "enum": ["overview", "detail", "motion"],
                "default": "overview",
                "description": "overview locates content; detail reads start/middle/end within an explicit range of at most 2 seconds; motion examines changes and order.",
            },
            crop={
                "type": "array",
                "description": "Normalized [left, top, right, bottom] corners of the displayed frame: 0 <= left < right <= 1 and 0 <= top < bottom <= 1.",
                "items": {"type": "number", "minimum": 0, "maximum": 1},
                "minItems": 4,
                "maxItems": 4,
            },
        )
    elif name == "read_transcript":
        pass
    elif name == "annotate_media":
        properties["text"] = {"type": "string", "minLength": 1}
        required = ["target", "from", "to", "text"]
    else:
        raise ValueError("unknown media tool")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def validate_media_arguments(name: str, arguments: Mapping[str, object]) -> None:
    if name == "view_image":
        from pulsara_agent.tools.builtins.filesystem import parse_view_image_source

        parse_view_image_source(arguments)
        return
    schema = media_parameters_schema(name)
    if set(arguments) - set(schema["properties"]) or not set(
        schema["required"]
    ).issubset(arguments):
        raise ValueError("INVALID_ARGUMENTS: unexpected or missing media fields")
    path_target_arg(arguments)
    for field in ("from", "to"):
        if field in arguments:
            value = arguments[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(
                    f"INVALID_RANGE: {field} must be a finite nonnegative number"
                )
    if (
        "from" in arguments
        and "to" in arguments
        and arguments["from"] >= arguments["to"]
    ):
        raise ValueError("INVALID_RANGE: from must precede to")
    if name == "read_video_frames":
        action = arguments.get("action", "overview")
        if not isinstance(action, str) or action not in {
            "overview",
            "detail",
            "motion",
        }:
            raise ValueError("INVALID_ARGUMENTS: invalid action")
        if action == "detail":
            from pulsara_agent.media.frames_detail import validate_detail_range

            validate_detail_range(arguments.get("from"), arguments.get("to"))
        if "crop" in arguments:
            crop = arguments["crop"]
            if (
                not isinstance(crop, (list, tuple))
                or len(crop) != 4
                or any(
                    type(v) not in (float, int) or not math.isfinite(v) for v in crop
                )
                or not (0 <= crop[0] < crop[2] <= 1 and 0 <= crop[1] < crop[3] <= 1)
            ):
                raise ValueError(
                    "INVALID_CROP: expected [left,top,right,bottom] within the display"
                )
    if name == "annotate_media" and (
        not isinstance(arguments["text"], str) or not arguments["text"].strip()
    ):
        raise ValueError("INVALID_ARGUMENTS: text must contain non-whitespace text")


@dataclass(slots=True)
class MediaTool(WorkspaceTool):
    name: str = "read_video_frames"

    def execute(self, call):
        raise RuntimeError("Media tools require the Host media execution owner")
