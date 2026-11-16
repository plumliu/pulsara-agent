"""Adapt the SDK's accepted formal content into Pulsara's ordered value.

The SDK owns protocol parsing and schemas. Pulsara owns audience selection,
per-image asset validation, and the complete business value; structuredContent
is a separate opaque business JSON value.
"""

from __future__ import annotations

import base64
import binascii
import json

from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.conversation_kernel.prompt_content import bind_prompt_images
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.ports.tool_execution import ToolBusinessValue, encode_tool_public_json


def _assistant_visible(item):
    annotations = item.get("annotations")
    audience = annotations.get("audience") if isinstance(annotations, dict) else None
    return audience is None or audience == [] or "assistant" in audience


def _recognized_image_blob(item):
    """Use Pillow's format acceptors without opening/decoding in the Host.

    A missing MIME must not disguise a recognizable damaged image as ordinary
    binary. Full validation and pixel accounting remain in the image worker.
    """
    from PIL import Image
    encoded = item.get("blob")
    if not isinstance(encoded, str):
        return False
    try:
        prefix = base64.b64decode(encoded, validate=True)[:16]
    except (binascii.Error, ValueError):
        return False
    Image.init()
    return any(accept is not None and accept(prefix)
        for name, (_factory, accept) in Image.OPEN.items() if name in {"PNG", "JPEG", "WEBP"})


async def _image(item, validator, deadline):
    encoded = item.get("data", item.get("blob"))
    if not isinstance(encoded, str):
        raise ValueError("图片数据缺失")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Base64 格式无效") from exc
    if validator is None:
        raise ValueError("图片验证服务不可用")
    try:
        image = await validator.freeze_local_image(payload, deadline_monotonic=deadline)
    except EOFError as exc:
        # Host close can stop the decoder after the remote result arrived.
        # This physical failure belongs to the image, not the known remote
        # outcome or the surrounding text and structured business value.
        raise ValueError(str(exc) or "图片验证进程已退出") from exc
    declared = item.get("mimeType")
    if declared is not None and declared != image.media_type:
        raise ValueError("声明的 MIME 与实际图片格式不一致")
    return bind_prompt_images(FrozenPromptContent((image,))).parts[0]


async def adapt_call_result(payload, *, validator, deadline):
    parts, images = [], []
    failed = payload.get("isError") is True
    for item in payload.get("content", ()):
        if not _assistant_visible(item):
            continue
        annotations = item.get("annotations")
        if isinstance(annotations, dict):
            public = {key: value for key, value in annotations.items() if key != "audience"}
            if public:
                parts.append(ResultPart("text", "附带说明：" + encode_tool_public_json(public).decode("utf-8")))
        if item.get("type") == "text":
            parts.append(ResultPart("text", item["text"]))
        elif item.get("type") == "image":
            try:
                image = await _image(item, validator, deadline)
            except (ValueError, OSError, TimeoutError) as exc:
                failed = True
                parts.append(ResultPart("text", f"图片数据无法读取：{exc}，未交付实际图片。"))
            else:
                images.append(image)
                parts.append(ResultPart("image", {"kind": "IMAGE_REF", "image_ref": image.image_ref}))
        else:
            # Ordinary links/audio/embedded resources retain all public data,
            # without implicitly fetching, playing, or parsing their payload.
            public = {key: value for key, value in item.items() if key not in {"annotations", "_meta"}}
            parts.append(ResultPart("text", encode_tool_public_json(public).decode("utf-8")))
    fields = {"structuredContent": payload["structuredContent"]} if "structuredContent" in payload else {}
    return ToolBusinessValue(fields, tuple(parts)), tuple(images), "ERROR" if failed else "SUCCESS"


async def adapt_resource_result(payload, *, validator, deadline):
    parts, images = [], []
    failed = False
    for item in payload.get("contents", ()):
        uri = item.get("uri", "")
        mime = item.get("mimeType")
        parts.append(ResultPart("text", f"资源：{uri}\n" + (f"MIME：{mime}\n" if mime is not None else "")))
        if "text" in item:
            parts.append(ResultPart("text", item["text"]))
            continue
        declared_image = (isinstance(mime, str) and mime.startswith("image/")) or (mime is None and _recognized_image_blob(item))
        if declared_image or mime is None:
            try:
                image = await _image(item, validator, deadline)
            except (ValueError, OSError, TimeoutError) as exc:
                if declared_image:
                    failed = True
                    parts.append(ResultPart("text", f"图片数据无法读取：{exc}，未交付实际图片。"))
                    continue
            else:
                images.append(image)
                parts.append(ResultPart("image", {"kind": "IMAGE_REF", "image_ref": image.image_ref}))
                continue
        parts.append(ResultPart("text", "Base64：" + item.get("blob", "")))
    return ToolBusinessValue(content=tuple(parts)), tuple(images), "ERROR" if failed else "SUCCESS"


async def adapt_known_result(known, *, validator, deadline):
    if known.kind == "TEXT":
        return ToolBusinessValue(content=(ResultPart("text", known.content.decode("utf-8")),)), (), known.state
    # The SDK already validated the formal response and the producer chose its
    # exact semantic kind. Decode that one wire representation, never guess a
    # Pulsara-internal carrier from the presence of content/error-looking keys.
    payload = json.loads(known.content)
    if known.kind == "TOOL":
        return await adapt_call_result(payload, validator=validator, deadline=deadline)
    if known.kind == "RESOURCE":
        return await adapt_resource_result(payload, validator=validator, deadline=deadline)
    raise TypeError("unknown MCP formal result kind")


__all__ = ["adapt_call_result", "adapt_resource_result", "adapt_known_result"]
