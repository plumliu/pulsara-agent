"""Source positions for atomic frame/index pages, without a paging session.

The frame owner reconstructs the immutable quality plan. These typed values only
bind its position to the authorized source/query; they carry no read authority,
undelivered body, independent identity or process lifetime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
import json
from math import isfinite
from typing import TYPE_CHECKING

from .source import MediaError, MediaInfo, MediaSource, exact_seconds

if TYPE_CHECKING:
    from .frames import FramePlan, FrameReadResult
    from .source import FramePosition


@dataclass(frozen=True, slots=True)
class FramePagePosition:
    next_observation: int = 0
    window_start: FramePosition | None = None
    next_global_grid_ordinal: int | None = None

    def __post_init__(self) -> None:
        if type(self.next_observation) is not int or self.next_observation < 0:
            raise MediaError("INVALID_ARGUMENTS", "Invalid next frame observation position")
        if self.next_global_grid_ordinal is not None and (
            type(self.next_global_grid_ordinal) is not int
            or self.next_global_grid_ordinal < 0
            or self.next_observation != 0
            or self.window_start is not None
        ):
            raise MediaError("INVALID_ARGUMENTS", "Invalid next detail grid position")


def _crop_value(crop) -> list[float]:
    return list(crop) if crop is not None else [0, 0, 1, 1]


def _source_version(source: MediaSource) -> dict[str, int]:
    # Reuse the descriptor owner's exact source comparison, not a new digest.
    return asdict(source.version)


def _position_value(position: FramePagePosition, mode: str) -> dict[str, object]:
    if mode == "detail":
        if (
            position.next_global_grid_ordinal is None
            or position.next_global_grid_ordinal < 1
            or position.next_observation != 0
            or position.window_start is not None
        ):
            raise MediaError("INVALID_ARGUMENTS", "A detail continuation must advance its global grid")
        return {"next_global_grid_ordinal": position.next_global_grid_ordinal}
    if position.next_global_grid_ordinal is not None:
        raise MediaError("INVALID_ARGUMENTS", "Only detail pages have a global grid position")
    value: dict[str, object] = {"next_observation": position.next_observation}
    if mode == "motion":
        frame = position.window_start
        value["window_start"] = None if frame is None else {
            "pts": frame.pts,
            "time_base": [frame.time_base.numerator, frame.time_base.denominator],
        }
    elif position.window_start is not None:
        raise MediaError("INVALID_ARGUMENTS", "Only motion pages have a source window position")
    return value


def _frame_position(value: object):
    from .source import FramePosition

    if not isinstance(value, Mapping) or set(value) != {"pts", "time_base"}:
        raise MediaError("INVALID_ARGUMENTS", "Invalid exact motion frame position")
    ticks, base = value["pts"], value["time_base"]
    if (
        type(ticks) is not int
        or not isinstance(base, (list, tuple))
        or len(base) != 2
        or any(type(part) is not int for part in base)
        or base[0] <= 0
        or base[1] <= 0
    ):
        raise MediaError("INVALID_ARGUMENTS", "Invalid exact motion timestamp/time base")
    scale = Fraction(base[0], base[1])
    if (scale.numerator, scale.denominator) != tuple(base):
        raise MediaError("INVALID_ARGUMENTS", "Motion time base must be in canonical rational form")
    return FramePosition(ticks, scale)


def read_frame_continuation(
    value: object,
    *,
    source: MediaSource,
    info: MediaInfo,
    start: float,
    end: float,
    mode: str,
    crop,
    selection_revision: str,
) -> FramePagePosition:
    """Validate a copied source position against the entire original query."""
    fields = {
        "source_path", "source_version", "request_from", "request_to", "mode",
        "crop", "selection_revision", "stream", "position",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise MediaError("INVALID_ARGUMENTS", "Invalid nonempty frame continuation")
    version = value["source_version"]
    actual_version = _source_version(source)
    if (
        not isinstance(version, Mapping)
        or set(version) != set(actual_version)
        or any(type(part) is not int for part in version.values())
    ):
        raise MediaError("INVALID_ARGUMENTS", "Invalid frame continuation source version")
    if value["source_path"] != str(source.path) or dict(version) != actual_version:
        raise MediaError("MEDIA_SOURCE_CHANGED", "Media changed since this frame continuation")
    if value["selection_revision"] != selection_revision:
        raise MediaError("FRAME_CONTINUATION_UNAVAILABLE", "The frame selection policy is no longer reproducible")
    if (
        any(type(value[key]) not in (int, float) or not isfinite(value[key])
            for key in ("request_from", "request_to"))
        or value["request_from"] != start
        or value["request_to"] != end
        or value["mode"] != mode
        or not isinstance(value["crop"], (list, tuple))
        or len(value["crop"]) != 4
        or any(type(part) not in (int, float) or not isfinite(part) for part in value["crop"])
        or list(value["crop"]) != _crop_value(crop)
        or type(value["stream"]) is not int
        or value["stream"] != info.video_stream_index
    ):
        raise MediaError("INVALID_ARGUMENTS", "Frame continuation belongs to a different query or video track")
    position = value["position"]
    if mode == "detail":
        if not isinstance(position, Mapping) or set(position) != {"next_global_grid_ordinal"}:
            raise MediaError("INVALID_ARGUMENTS", "Invalid detail continuation position fields")
        ordinal = position["next_global_grid_ordinal"]
        result = FramePagePosition(next_global_grid_ordinal=ordinal)
        if ordinal is None or ordinal < 1:
            raise MediaError("INVALID_ARGUMENTS", "A detail continuation must advance its global grid")
        return result
    position_fields = {"next_observation", "window_start"} if mode == "motion" else {"next_observation"}
    if not isinstance(position, Mapping) or set(position) != position_fields:
        raise MediaError("INVALID_ARGUMENTS", "Invalid frame continuation position fields")
    ordinal = position["next_observation"]
    window = position.get("window_start")
    frame = None if window is None else _frame_position(window)
    result = FramePagePosition(ordinal, frame)
    if mode != "motion" and ordinal == 0:
        raise MediaError("INVALID_ARGUMENTS", "A frame continuation must advance its observation position")
    if mode == "motion":
        if frame is None and ordinal == 0:
            raise MediaError("INVALID_ARGUMENTS", "A motion continuation must advance a source or observation position")
        if frame is not None:
            relative = frame.absolute_time - info.timeline_origin
            if not exact_seconds(start) < relative < exact_seconds(end):
                raise MediaError("INVALID_ARGUMENTS", "Motion source window is outside the remaining request")
    return result


def make_frame_next(
    *,
    source: MediaSource,
    info: MediaInfo,
    start: float,
    end: float,
    mode: str,
    crop,
    selection_revision: str,
    position: FramePagePosition,
) -> dict[str, object]:
    """Return a complete next call; no position ID or undelivered image list."""
    return {
        "tool": "read_video_frames",
        "arguments": {
            "target": {"kind": "PATH", "path": str(source.path)},
            "from": start,
            "to": end,
            "mode": mode,
            **({"crop": list(crop)} if crop is not None else {}),
            "continuation": {
                "source_path": str(source.path),
                "source_version": _source_version(source),
                "request_from": start,
                "request_to": end,
                "mode": mode,
                "crop": _crop_value(crop),
                "selection_revision": selection_revision,
                "stream": info.video_stream_index,
                "position": _position_value(position, mode),
            },
        },
    }


def trim_detail_page(candidate: FrameReadResult, count: int) -> FrameReadResult:
    """Project an unbound encoded detail prefix without changing its images.

    Detail images have fixed geometry and first-seen image numbering. Removing
    trailing observations therefore retains an exact prefix of encoded images;
    no selected source position, burned image number or coordinate changes.
    """
    body = json.loads(candidate.text)
    if (
        not isinstance(body, dict) or body.get("video_frames") is not True
        or body.get("next") is not None
        or "grid_delivery" in body or "plan_delivery" in body
    ):
        raise ValueError("Only an unbound detail candidate can be trimmed")
    frames = body.get("frames")
    if not isinstance(frames, list) or type(count) is not int or not 1 <= count <= len(frames):
        raise ValueError("A detail projection must retain a nonempty observation prefix")
    retained = frames[:count]
    images = [frame.get("image") for frame in retained]
    if any(type(image) is not int or not 1 <= image <= len(candidate.images) for image in images):
        raise ValueError("Detail observation image positions are invalid")
    image_count = max(images)
    if set(images) != set(range(1, image_count + 1)):
        raise ValueError("Detail images must retain their first-seen prefix order")
    if count == len(frames):
        return candidate
    body["frames"] = retained
    return replace(
        candidate, images=candidate.images[:image_count],
        text=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        complete=False,
    )


def with_frame_page_continuation(
    candidate: FrameReadResult,
    *,
    source: MediaSource,
    info: MediaInfo,
    plan: FramePlan,
    position: FramePagePosition,
    count: int,
    selection_revision: str,
) -> FrameReadResult:
    """Bind this rendered slice, its actual images and its next in one body."""
    offset = 0 if plan.mode == "detail" else position.next_observation
    stop = offset + count
    if type(count) is not int or count < 1 or not 0 <= offset < stop <= plan.observation_count:
        raise MediaError("INVALID_ARGUMENTS", "Frame page position is outside its selection plan")
    if plan.mode == "detail":
        grid_start = position.next_global_grid_ordinal or 0
        if (
            position.next_observation != 0 or position.window_start is not None
            or plan.detail_grid_start != grid_start or plan.detail_grid_count is None
        ):
            raise MediaError("INVALID_ARGUMENTS", "Detail page belongs to a different global grid position")
        delivered = plan.priority[stop - 1]
        next_ordinal = delivered.detail_next_grid_ordinal
        if (
            type(next_ordinal) is not int
            or not grid_start < next_ordinal <= plan.detail_grid_count
        ):
            raise ValueError("Delivered detail observation has no advancing global grid position")
        following = (
            FramePagePosition(next_global_grid_ordinal=next_ordinal)
            if next_ordinal < plan.detail_grid_count else None
        )
    elif stop < plan.observation_count:
        following = FramePagePosition(stop, position.window_start)
    elif plan.next_window_start is not None:
        following = FramePagePosition(0, plan.next_window_start)
    else:
        following = None
    start, end = plan.requested_range
    next_call = None if following is None else make_frame_next(
        source=source, info=info, start=start, end=end, mode=plan.mode, crop=plan.crop,
        selection_revision=selection_revision, position=following,
    )
    body = json.loads(candidate.text)
    if not isinstance(body, dict) or body.get("video_frames") is not True:
        raise ValueError("A frame page requires its actual frame index")
    frames = body.get("frames")
    selected = sorted(enumerate(plan.priority[offset:stop], offset), key=lambda pair: pair[1].t)
    if not isinstance(frames, list) or len(frames) != count or len(candidate.images) < 1:
        raise ValueError("Rendered frame page and its selection slice differ")
    for frame, (rank, observation) in zip(frames, selected, strict=True):
        if frame.get("t") != float(observation.t):
            raise ValueError("Rendered frame time differs from its selected source observation")
        frame["observation_position"] = (
            observation.detail_grid_ordinal if plan.mode == "detail" else rank
        )
    complete = following is None and plan.complete and not plan.video_ended
    body["next"] = next_call
    body["complete"] = complete
    if plan.mode == "detail":
        body["grid_delivery"] = {
            "position": grid_start,
            "next_position": next_ordinal,
            "targets": plan.detail_grid_count,
        }
    else:
        body["plan_delivery"] = {
            "position": offset,
            "next_position": stop,
            "observations": plan.observation_count,
            "window_complete": stop == plan.observation_count,
        }
    limitations = body.setdefault("limitations", [])
    if following is not None:
        limitations.append(
            "Detail grid observations remain; copy next. Analysis range is not continuous delivered coverage."
            if plan.mode == "detail" else
            "Selected observations remain undelivered; copy next. Analysis range is not continuous delivered coverage."
        )
    if plan.mode == "overview" and offset == 0 and count == 1 and plan.observation_count > 1:
        limitations.append("Overview orientation is incomplete: this page contains only one observation.")
    return replace(candidate, text=json.dumps(body, ensure_ascii=False, separators=(",", ":")), complete=complete)
