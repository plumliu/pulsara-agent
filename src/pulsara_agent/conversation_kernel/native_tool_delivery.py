"""Ordered pages over complete accepted values, and opaque NEXT authentication.

Positions refer to the deterministic text projection of public fields followed
by the ordered content occurrences. They are neither source-file offsets nor
stored page bodies. Product admission reuses the one v3 token estimator.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import hmac
import json
import secrets
from typing import Callable, Mapping

from pulsara_agent.identity import require_id
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV3, estimate_image_visual_tokens
from pulsara_agent.llm.input import LLMImagePart
from pulsara_agent.ports.tool_execution import encode_tool_public_json, thaw_tool_json_object

NATIVE_TOOL_PAGE_TOKENS = 12_000
_NEXT_KEY = secrets.token_bytes(32)
_ESTIMATOR = PulsaraHeuristicTokenEstimatorV3()


@dataclass(frozen=True, order=True, slots=True)
class NativeResultPosition:
    segment: int
    offset: int = 0

    def __post_init__(self):
        if type(self.segment) is not int or type(self.offset) is not int or min(self.segment, self.offset) < 0:
            raise ValueError("invalid tool-result position")

    def value(self):
        return [self.segment, self.offset]

    @classmethod
    def parse(cls, value):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("invalid tool-result position")
        return cls(*value)


def next_tool_result_target(*, session_id: str, entry_id: str, position: NativeResultPosition) -> dict[str, str]:
    require_id(session_id, "session")
    require_id(entry_id, "entry")
    payload = encode_tool_public_json({"session": session_id, "entry": entry_id, "position": position.value()})
    signature = hmac.digest(_NEXT_KEY, b"pulsara:tool-result-next\0" + payload, sha256)
    return {"kind": "NEXT", "token": base64.urlsafe_b64encode(signature + payload).decode("ascii").rstrip("=")}


def resolve_tool_result_next(target: Mapping[str, object], *, session_id: str) -> tuple[str, NativeResultPosition]:
    require_id(session_id, "session")
    if not isinstance(target, Mapping) or set(target) != {"kind", "token"} or target["kind"] != "NEXT":
        raise ValueError("the continuation must be the complete NEXT Target")
    token = target["token"]
    if not isinstance(token, str) or not token or "=" in token:
        raise ValueError("the NEXT reference is invalid or expired")
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != token:
            raise ValueError("noncanonical NEXT token")
        signature, payload = raw[:32], raw[32:]
        expected = hmac.digest(_NEXT_KEY, b"pulsara:tool-result-next\0" + payload, sha256)
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid NEXT authentication")
        value = json.loads(payload)
        if set(value) != {"session", "entry", "position"} or value["session"] != session_id:
            raise ValueError("NEXT belongs to another session")
        require_id(value["entry"], "entry")
        return value["entry"], NativeResultPosition.parse(value["position"])
    except (ValueError, TypeError, UnicodeError, KeyError) as error:
        raise ValueError("the NEXT reference is invalid or expired") from error


@dataclass(frozen=True, slots=True)
class NativeProjectionSegment:
    kind: str
    text: str = ""
    image_ref: str | None = None
    file_text_index: int | None = None

    @property
    def splittable(self):
        return self.kind in {"fields", "text", "object"}


def tool_result_segments(value: Mapping[str, object]) -> tuple[NativeProjectionSegment, ...]:
    public = json.loads(encode_tool_public_json(thaw_tool_json_object(value)))
    fields = {key: item for key, item in public.items() if key != "content"}
    result = [NativeProjectionSegment("fields", encode_tool_public_json(fields, indent=2, sort_keys=True).decode() + "\n")]
    file_index = 0
    for part in public["content"]:
        kind, item = part["type"], part["value"]
        if kind == "text":
            item = item.encode("utf-16-le", errors="surrogatepass").decode("utf-16-le", errors="surrogatepass")
            text = item.encode("utf-8", errors="backslashreplace").decode("utf-8")
            result.append(NativeProjectionSegment(kind, text, file_text_index=file_index))
            file_index += len(text)
        elif kind == "object":
            result.append(NativeProjectionSegment(kind, encode_tool_public_json(item, indent=2, sort_keys=True).decode() + "\n"))
        elif kind == "target":
            result.append(NativeProjectionSegment(kind, encode_tool_public_json(item, sort_keys=True).decode() + "\n"))
        elif kind == "image":
            result.append(NativeProjectionSegment(kind, image_ref=require_id(item["image_ref"], "image")))
        else:
            raise ValueError("unknown public content kind")
    return tuple(result)


def _normalize_position(segments, position):
    if position.segment > len(segments):
        raise ValueError("tool result position exceeds saved content")
    index, offset = position.segment, position.offset
    while index < len(segments):
        segment = segments[index]
        if not segment.splittable:
            if offset != 0:
                raise ValueError("indivisible content cannot have a text offset")
            break
        if offset > len(segment.text):
            raise ValueError("tool result text position exceeds saved content")
        if offset < len(segment.text):
            break
        index, offset = index + 1, 0
    if index == len(segments) and offset:
        raise ValueError("terminal tool result position is invalid")
    return NativeResultPosition(index, offset)


@dataclass(frozen=True, slots=True)
class NativeToolResultRange:
    start: NativeResultPosition
    end: NativeResultPosition
    image_errors: tuple[tuple[int, str], ...] = ()

    def value(self):
        return {"start": self.start.value(), "end": self.end.value(),
                "image_errors": {str(index): reason for index, reason in self.image_errors}}

    @classmethod
    def parse(cls, value):
        if not isinstance(value, Mapping) or set(value) != {"start", "end", "image_errors"}:
            raise ValueError("invalid native result range")
        errors = value["image_errors"]
        if not isinstance(errors, Mapping):
            raise ValueError("invalid native result image observations")
        if any(not isinstance(key, str) or not key.isdecimal() or not isinstance(reason, str) or not reason for key, reason in errors.items()):
            raise ValueError("invalid native result image observations")
        return cls(NativeResultPosition.parse(value["start"]), NativeResultPosition.parse(value["end"]),
                   tuple(sorted((int(index), reason) for index, reason in errors.items())))


@dataclass(frozen=True, slots=True)
class NativeToolPage:
    source_entry_id: str
    range: NativeToolResultRange
    body: str
    images: tuple[LLMImagePart, ...]
    next: Mapping[str, object] | None
    status: str
    estimated_tokens: int
    page_error: str | None = None
    image_positions: tuple[tuple[int, int], ...] = ()
    observed_at: datetime | None = None
    observation_duration_microseconds: int | None = None
    continuation: bool = False

    @property
    def canonical_text(self):
        if self.observed_at is None:
            raise ValueError("native page lacks its actual observation time")
        return render_native_tool_page(self, observed_at=self.observed_at,
            duration_microseconds=self.observation_duration_microseconds, continuation=self.continuation)

    def projection_facts(self):
        return {**self.range.value(), "next": None if self.next is None else dict(self.next), "page_error": self.page_error}


IMAGE_INPUT_UNSUPPORTED_TEXT = "当前模型不支持图片输入，未交付实际图片。"
_IMAGE_UNAVAILABLE = "图片不存在或已不可用，未交付实际图片。"
_IMAGE_TEMPORARY = "暂时无法读取图片，未交付实际图片。"
_IMAGE_CORRUPT = "图片数据无法解码或验证，未交付实际图片。"


def _image_error(error):
    if isinstance(error, TimeoutError):
        return _IMAGE_TEMPORARY
    if isinstance(error, (LookupError, KeyError)):
        return _IMAGE_UNAVAILABLE
    from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
    if isinstance(error, (ValueError, TypeError, ConversationKernelConflict)):
        return _IMAGE_CORRUPT
    return _IMAGE_TEMPORARY


def _file_line_text(text, *, segment, offset, source_value, source_arguments, source_tool_name):
    if source_tool_name != "read_file" or source_arguments.get("target", {}).get("kind") != "PATH" or segment.kind != "text":
        return text
    total = source_value.get("meta", {}).get("total_lines")
    if type(total) is not int:
        return text
    requested = source_arguments.get("offset", 1)
    if type(requested) is not int or requested == 0:
        raise ValueError("saved file invocation has invalid line offset")
    first = requested if requested > 0 else max(1, total + requested + 1)
    all_text = "".join(part["value"] for part in source_value["content"] if part["type"] == "text")
    before = (segment.file_text_index or 0) + offset
    line = first + all_text[:before].count("\n")
    result = []
    # LF is the only source line boundary. A chunk starting mid-line still
    # belongs to that original line, including CR and literal digit| prefixes.
    for piece in text.split("\n"):
        result.append(str(line) + "|" + piece)
        line += 1
    if text.endswith("\n"):
        result.pop()
        return "\n".join(result) + "\n"
    return "\n".join(result)


def project_native_tool_page(*, session_id: str, entry_id: str, value: Mapping[str, object],
        image_reader: Callable[[str], LLMImagePart], source_tool_name: str = "",
        source_arguments: Mapping[str, object] | None = None,
        start: NativeResultPosition = NativeResultPosition(0), supports_images: bool = True,
        frozen_range: NativeToolResultRange | None = None,
        frozen_next: Mapping[str, object] | None = None,
        image_dimensions: Callable[[str], tuple[int, int]] | None = None) -> NativeToolPage:
    """Choose one ordered 12k page, or restore exactly an accepted range."""
    require_id(entry_id, "entry")
    segments = tool_result_segments(value)
    position = _normalize_position(segments, start if frozen_range is None else frozen_range.start)
    beginning = position
    end_limit = None if frozen_range is None else _normalize_position(segments, frozen_range.end)
    if end_limit is not None and end_limit < position:
        raise ValueError("native range ends before its start")
    errors = {} if frozen_range is None else dict(frozen_range.image_errors)
    if end_limit is not None and any(index < position.segment or index >= end_limit.segment + bool(end_limit.offset) for index in errors):
        raise ValueError("image observation lies outside its saved native range")
    body, images, tokens = [], [], 0
    image_positions = []
    has_unit = False
    while position.segment < len(segments) and (end_limit is None or position < end_limit):
        index, offset = position.segment, position.offset
        segment = segments[index]
        if segment.kind == "image":
            image = None
            failure = errors.get(index)
            if frozen_range is None and not supports_images:
                failure = IMAGE_INPUT_UNSUPPORTED_TEXT
            if failure is None:
                try:
                    image = image_reader(segment.image_ref)
                except Exception as error:
                    if frozen_range is not None:
                        raise
                    failure = _image_error(error)
            # The saved dimensions are the product quote even when the current
            # model cannot receive, or the byte reader cannot decode, the image.
            try:
                width, height = (image_dimensions(segment.image_ref) if image_dimensions is not None
                    else ((image.width, image.height) if image is not None
                          else ((loaded := image_reader(segment.image_ref)).width, loaded.height)))
                charge = estimate_image_visual_tokens(width=width, height=height)
            except Exception:
                charge = _ESTIMATOR.estimate_text(failure or _IMAGE_UNAVAILABLE)
            if frozen_range is None and has_unit and tokens + charge > NATIVE_TOOL_PAGE_TOKENS:
                break
            reference = encode_tool_public_json({"kind": "IMAGE_REF", "image_ref": segment.image_ref}).decode()
            body.append("图片引用：" + reference + "\n")
            image_positions.append((index, sum(len(fragment) for fragment in body)))
            if failure is not None:
                errors[index] = failure
                body.append(failure + "\n")
            else:
                images.append(image)
            tokens += charge
            position = NativeResultPosition(index + 1)
            has_unit = True
            if frozen_range is None and charge > NATIVE_TOOL_PAGE_TOKENS:
                break
            continue
        remaining = segment.text[offset:]
        if end_limit is not None and end_limit.segment == index:
            remaining = segment.text[offset:end_limit.offset]
        charge = _ESTIMATOR.estimate_text(remaining)
        if frozen_range is None and tokens + charge > NATIVE_TOOL_PAGE_TOKENS:
            if not segment.splittable:
                if not has_unit:
                    raise ValueError("the complete Target does not fit a native result page")
                break
            low, high = 0, len(remaining)
            while low < high:
                middle = (low + high + 1) // 2
                if tokens + _ESTIMATOR.estimate_text(remaining[:middle]) <= NATIVE_TOOL_PAGE_TOKENS:
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                break
            remaining = remaining[:low]
            charge = _ESTIMATOR.estimate_text(remaining)
        if segment.kind in {"object", "fields"} and (offset > 0 or len(remaining) < len(segment.text)):
            body.append("[JSON 正文片段]\n")
        body.append(_file_line_text(remaining, segment=segment, offset=offset,
            source_value=value, source_arguments=source_arguments or {}, source_tool_name=source_tool_name))
        tokens += charge
        has_unit = has_unit or bool(remaining)
        position = (_normalize_position(segments, NativeResultPosition(index, offset + len(remaining)))
            if segment.splittable else NativeResultPosition(index + 1))
        if frozen_range is None and position.segment == index:
            break
    if end_limit is not None and position != end_limit:
        raise ValueError("saved native result range could not be restored exactly")
    if position == beginning and position.segment < len(segments):
        raise ValueError("no readable native result prefix could be admitted")
    end = _normalize_position(segments, position)
    projection = NativeToolResultRange(beginning, end, tuple(sorted(errors.items())))
    if frozen_range is not None:
        if (end.segment == len(segments)) != (frozen_next is None):
            raise ValueError("saved native continuation disagrees with its range")
        next_target = frozen_next
    else:
        next_target = (None if end.segment == len(segments) else next_tool_result_target(
            session_id=session_id, entry_id=entry_id, position=end))
    status = "ERROR" if errors else str(value["meta"]["status"])
    return NativeToolPage(entry_id, projection, "".join(body), tuple(images), next_target, status, tokens,
        image_positions=tuple(image_positions))


def validate_native_projection(value: Mapping[str, object], projection: Mapping[str, object]) -> NativeToolResultRange:
    if not isinstance(projection, Mapping) or set(projection) != {"start", "end", "image_errors", "next", "page_error"}:
        raise ValueError("native projection has invalid fields")
    selected = NativeToolResultRange.parse({key: projection[key] for key in ("start", "end", "image_errors")})
    segments = tool_result_segments(value)
    start, end = _normalize_position(segments, selected.start), _normalize_position(segments, selected.end)
    if start != selected.start or end != selected.end or end < start:
        raise ValueError("native projection positions are invalid")
    if any(index < start.segment or index >= end.segment + bool(end.offset) or segments[index].kind != "image"
           for index, _reason in selected.image_errors):
        raise ValueError("native projection image observation lies outside the selected range")
    page_error = projection["page_error"]
    if page_error is not None and (not isinstance(page_error, str) or not page_error or start != end):
        raise ValueError("failed native page must leave its starting position unchanged")
    next_target = projection["next"]
    if next_target is not None and (not isinstance(next_target, Mapping) or set(next_target) != {"kind", "token"}
            or next_target["kind"] != "NEXT" or not isinstance(next_target["token"], str) or not next_target["token"]):
        raise ValueError("invalid saved native continuation")
    if page_error is None and (end.segment == len(segments)) != (next_target is None):
        raise ValueError("native continuation does not match the saved endpoint")
    return selected


def failed_native_tool_page(*, session_id: str, entry_id: str, start: NativeResultPosition,
                            value: Mapping[str, object], error: Exception) -> NativeToolPage:
    start = _normalize_position(tool_result_segments(value), start)
    detail = str(error).encode("utf-8", errors="backslashreplace").decode("utf-8")
    reason = "完整结果已保存；当前页未交付。" + detail + " 不应重做原业务操作。"
    return NativeToolPage(entry_id, NativeToolResultRange(start, start), "", (),
        next_tool_result_target(session_id=session_id, entry_id=entry_id, position=start), "ERROR", 0, reason)


def render_native_tool_page(page: NativeToolPage, *, observed_at, duration_microseconds: int | None,
                            continuation: bool = False) -> str:
    from pulsara_agent.primitives.tool_observation import canonical_utc_timestamp
    status = ("ERROR" if page.page_error or page.range.image_errors else "SUCCESS") if continuation else page.status
    prefix = "observed_at_utc: " + canonical_utc_timestamp(observed_at) + "\n"
    prefix += "observation_duration_microseconds: " + ("null" if duration_microseconds is None else str(duration_microseconds)) + "\n\n"
    prefix += "meta:\n  status: " + status + "\n\ncontent:\n"
    body = page.page_error if page.page_error is not None else page.body
    return (prefix + body + "\ntool_result: " + encode_tool_public_json({"kind": "SESSION_ENTRY", "entry_id": page.source_entry_id}).decode()
            + "\nnext: " + encode_tool_public_json(None if page.next is None else dict(page.next)).decode())


def prepare_unavailable_native_page(page: NativeToolPage, *, session_id: str, error: str) -> NativeToolPage:
    """The final-admission error page leaves every source unit undelivered."""
    return replace(page, range=NativeToolResultRange(page.range.start, page.range.start),
        body="", images=(), next=next_tool_result_target(session_id=session_id,
            entry_id=page.source_entry_id, position=page.range.start), status="ERROR", estimated_tokens=0,
        page_error="完整结果已保存；当前页未交付。" + error + " 不应重做原业务操作。", image_positions=())


def prepare_unsupported_native_page(page: NativeToolPage) -> NativeToolPage:
    """Use typed selected image positions; never infer media from text markers."""
    if not page.image_positions:
        return page
    body = page.body
    errors = dict(page.range.image_errors)
    for index, offset in reversed(page.image_positions):
        previous = errors.get(index)
        end = offset if previous is None else offset + len(previous) + 1
        body = body[:offset] + IMAGE_INPUT_UNSUPPORTED_TEXT + "\n" + body[end:]
        errors[index] = IMAGE_INPUT_UNSUPPORTED_TEXT
    return replace(page, body=body, images=(), range=replace(page.range, image_errors=tuple(sorted(errors.items()))),
        status="ERROR", image_positions=())


def bind_native_observation(page: NativeToolPage, *, observed_at: datetime,
                            duration_microseconds: int | None, continuation: bool = False) -> NativeToolPage:
    return replace(page, observed_at=observed_at, observation_duration_microseconds=duration_microseconds,
                   continuation=continuation)
