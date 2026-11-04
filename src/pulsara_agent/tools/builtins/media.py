"""Thin media contracts; source decoding belongs to the media owners."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math

from pulsara_agent.tools.builtins.workspace import WorkspaceTool
from pulsara_agent.tools.builtins.filesystem import (
    ViewImageSource,
    ViewImageSourceKind,
    path_target_arg,
    parse_view_image_source,
)

MEDIA_TOOL_NAMES = frozenset({"read_video_frames", "read_transcript", "annotate_media"})
IMAGE_TOOL_NAMES = frozenset({"view_image", "read_video_frames"})


def media_parameters_schema(name: str) -> dict:
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
        properties.update(
            mode={
                "type": "string",
                "enum": ["overview", "detail", "motion"],
                "default": "overview",
            },
            crop={
                "type": "array",
                "items": {"type": "number", "minimum": 0, "maximum": 1},
                "minItems": 4,
                "maxItems": 4,
            },
        )
    elif name == "read_transcript":
        properties["continuation"] = {
            "type": "object",
            "minProperties": 1,
            "description": "Copy the complete next call returned by runtime; omit on the first read.",
        }
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
        mode = arguments.get("mode", "overview")
        if not isinstance(mode, str) or mode not in {"overview", "detail", "motion"}:
            raise ValueError("INVALID_ARGUMENTS: invalid mode")
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
    if name == "read_transcript" and "continuation" in arguments:
        if (
            not isinstance(arguments["continuation"], Mapping)
            or not arguments["continuation"]
        ):
            raise ValueError(
                "INVALID_ARGUMENTS: continuation must be a nonempty object"
            )


def parse_image_tool_source(
    name: str, arguments: Mapping[str, object]
) -> ViewImageSource:
    if name == "view_image":
        return parse_view_image_source(arguments)
    if name != "read_video_frames":
        raise ValueError("not an image tool")
    validate_media_arguments(name, arguments)
    return ViewImageSource(ViewImageSourceKind.PATH, path_target_arg(arguments))


@dataclass(slots=True)
class MediaTool(WorkspaceTool):
    name: str = "read_video_frames"

    def execute(self, call):
        raise RuntimeError("Media tools require the Host media execution owner")
