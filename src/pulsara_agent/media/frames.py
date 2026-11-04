"""Local video evidence, executed inside the existing cancellable IO worker.

PyAV owns demux/seek/decode; source owns descriptor identity and display
geometry; Pillow owns raster transforms/encoding. This adapter only selects
real presentation instants and composes their ordered evidence. No inference,
ASR, persistent media identity, or total-video limit is introduced.
"""

from __future__ import annotations
from dataclasses import dataclass, replace
import io
import json
import math
from typing import Callable, Literal
from PIL import Image, ImageDraw

from .source import MediaError, MediaInfo, MediaSource, display_video_frame, prepare_video_decode
from .frames_layout import Cell, Layout, maximum_cell_width, pack_rows, row_candidates
from .frames_selection import (
    MotionEvidenceOverflow,
    Sample,
    detect_changes,
    select_samples,
    features,
    distance,
    sharpness,
)


@dataclass(frozen=True)
class FrameBudget:
    """Call-local physical/result capacities, supplied by the runtime owner.

    image_pixels is the total output-pixel allocation, maximum_bytes the total
    encoded allocation, analysis_frames the low-resolution working-set slots.
    These capacities never reject a media file for its total length or size.
    """

    maximum_bytes: int
    image_pixels: int
    maximum_images: int
    analysis_frames: int
    maximum_frames: int | None = None

    def __post_init__(self):
        if self.maximum_frames is not None and (
            isinstance(self.maximum_frames, bool)
            or not isinstance(self.maximum_frames, int)
            or self.maximum_frames < 1
        ):
            raise ValueError("frame allocation must be positive")
        if any(
            isinstance(v, bool) or not isinstance(v, int) or v < 1
            for v in (
                self.maximum_bytes,
                self.image_pixels,
                self.maximum_images,
                self.analysis_frames,
            )
        ):
            raise ValueError("frame capacities must be positive integers")


@dataclass(frozen=True)
class FrameImage:
    data: bytes
    mime: str
    width: int
    height: int


@dataclass(frozen=True)
class FrameReadResult:
    images: tuple[FrameImage, ...]
    text: str
    processed_range: tuple[float, float]
    complete: bool


def _check(cancelled):
    if cancelled and cancelled():
        raise InterruptedError("video reading cancelled")


def _crop(image, crop):
    if crop is None:
        return image
    left, top, right, bottom = crop
    return image.crop(
        (
            math.floor(left * image.width),
            math.floor(top * image.height),
            math.ceil(right * image.width),
            math.ceil(bottom * image.height),
        )
    )


def _frame_end(frame, info):
    duration = getattr(frame, "duration", None)
    if duration is not None and duration > 0 and frame.time_base is not None:
        # Add PTS ticks before mapping to seconds. Separate floating additions
        # can land on nextafter(end, start) and incorrectly exclude the last frame.
        return float((frame.pts + duration) * frame.time_base) - info.timeline_origin
    return info.video_end


def _visible(source, info, target, cancelled, *, range_end=math.inf):
    """Return actual frame visible at target, including a boundary predecessor."""
    with source.open_container() as container:
        stream = container.streams[info.video_stream_index]
        container.seek(
            max(0, int((target + info.timeline_origin) / stream.time_base)),
            stream=stream,
            backward=True,
        )
        previous = None
        prepare_video_decode(stream, float(info.sample_aspect_ratio))
        for frame in container.decode(stream):
            _check(cancelled)
            if frame.pts is None:
                continue
            t = info.frame_time(frame)
            if t > target:
                if previous is not None:
                    return previous[0], t, display_video_frame(previous[1], info)
                if t >= range_end:
                    raise MediaError(
                        "NO_VIDEO_IN_RANGE",
                        "The video track starts after the requested interval",
                    )
                return t, _frame_end(frame, info), display_video_frame(frame, info)
            previous = (t, frame)
        if previous is not None:
            until = _frame_end(previous[1], info)
            if until is not None and target >= until:
                raise MediaError(
                    "MEDIA_FRAME_UNAVAILABLE",
                    "The selected video stream has ended before this position",
                )
            return previous[0], until, display_video_frame(previous[1], info)
    raise MediaError(
        "MEDIA_DECODE_FAILED", "No displayable video frame in requested range"
    )


def _scan(source, info, start, end, capacity, crop, cancelled, *, first_interval=None):
    samples = []
    with source.open_container() as container:
        stream = container.streams[info.video_stream_index]
        container.seek(
            max(0, int((start + info.timeline_origin) / stream.time_base)),
            stream=stream,
            backward=True,
        )
        previous = None
        prepare_video_decode(stream, float(info.sample_aspect_ratio))
        for frame in container.decode(stream):
            _check(cancelled)
            if frame.pts is None:
                continue
            t = info.frame_time(frame)
            if previous is not None:
                pt, previous_frame = previous
                if t > start and pt < end:
                    if first_interval is not None and not samples:
                        first_interval((start, min(t, end)))
                    image = _crop(display_video_frame(previous_frame, info), crop)
                    image.thumbnail((256, 256), Image.Resampling.LANCZOS)
                    samples.append(Sample(pt, image, 0, min(t, end)))
                    if len(samples) >= capacity:
                        return samples, min(t, end), t >= end
            if t >= end:
                return samples, end, True
            previous = (t, frame)
        if previous is not None and previous[0] < end:
            until = _frame_end(previous[1], info)
            actual_end = min(end, until) if until is not None else previous[0]
            if actual_end > start:
                if first_interval is not None and not samples:
                    first_interval((start, actual_end))
                image = _crop(display_video_frame(previous[1], info), crop)
                image.thumbnail((256, 256), Image.Resampling.LANCZOS)
                samples.append(Sample(previous[0], image, 0, actual_end))
            return samples, actual_end, actual_end >= end
    return samples, start, False


def _canvas(aspect, pixels):
    scale = min(1280, math.sqrt(pixels * max(aspect, 1 / aspect)))
    width = int(scale if aspect >= 1 else scale * aspect)
    height = int(scale / aspect if aspect >= 1 else scale)
    return max(1, width), max(1, height)


def _layout(samples, changes, mode, budget, capacity):
    first = samples[0].image
    aspect = first.width / first.height
    width, height = _canvas(aspect, budget.image_pixels)
    selected = select_samples(samples, changes, capacity, mode)
    if mode == "overview":
        # Smooth density in true-time neighborhoods, then quantize with
        # hysteresis. High-density partitions receive smaller nominal cells.
        span = max(samples[-1].t - samples[0].t, 1e-9)
        tiers = []
        base = min(8, max(1, math.ceil(math.sqrt(len(selected)))))
        previous = base
        for i in selected:
            density = sum(
                abs(samples[j].t - samples[i].t) <= span / max(1, len(selected))
                for j in changes.candidates
            )
            desired = min(8, max(base, base + int(math.sqrt(density))))
            tier = desired if abs(desired - previous) >= 2 else previous
            tiers.append(tier)
            previous = tier
        minimum = (
            (160, math.ceil(160 / aspect))
            if aspect >= 1
            else (math.ceil(160 * aspect), 160)
        )
        # A tier whose interval cannot meet readability is promoted explicitly.
        for i, tier in enumerate(tiers):
            while tier > 1 and maximum_cell_width(width, tier) < minimum[0]:
                tier -= 1
            tiers[i] = tier
        rows = row_candidates(tiers, width, height, aspect, minimum)
        layout = pack_rows(rows, width, height, maximum_pages=budget.maximum_images)
    elif mode == "detail":
        width, height = _canvas(aspect, budget.image_pixels / max(1, len(selected)))
        cells = tuple(Cell(i, i, (0, 0, width, height)) for i in range(len(selected)))
        layout = Layout(
            cells,
            tuple((width, height) for _ in selected),
            0,
            width * height * len(selected),
        )
    else:
        # Motion uses equal cells, never the overview variable-tier DP.
        count = len(selected)
        minimum_width = 160 if aspect >= 1 else math.ceil(160 * aspect)
        options = []
        for columns in range(1, count + 1):
            rows = math.ceil(count / columns)
            cell_width = min(
                1280 // columns,
                int(1280 * aspect / rows),
                int(math.sqrt(budget.image_pixels * aspect / (columns * rows))),
            )
            cell_height = math.ceil(cell_width / aspect)
            if (
                cell_width >= minimum_width
                and columns * cell_width * rows * cell_height <= budget.image_pixels
            ):
                options.append(
                    (
                        cell_width,
                        -abs(
                            math.log(
                                (columns * cell_width) / (rows * cell_height) / aspect
                            )
                        ),
                        columns,
                        rows,
                        cell_height,
                    )
                )
        if not options:
            raise ValueError("readable ordered motion cells exceed image allowance")
        cell_width, _, columns, rows, cell_height = max(options)
        cells = tuple(
            Cell(
                i,
                0,
                (
                    (i % columns) * cell_width,
                    (i // columns) * cell_height,
                    (i % columns + 1) * cell_width,
                    (i // columns + 1) * cell_height,
                ),
            )
            for i in range(count)
        )
        layout = Layout(
            cells,
            ((columns * cell_width, rows * cell_height),),
            0,
            count * cell_width * cell_height,
        )
    if sum(w * h for w, h in layout.page_sizes) > budget.image_pixels:
        raise ValueError("output pixels exceed call allocation")
    return selected, layout


def read_video_frames(
    source: MediaSource,
    info: MediaInfo,
    *,
    start: float,
    end: float,
    mode: Literal["overview", "detail", "motion"] = "overview",
    crop: tuple[float, float, float, float] | None = None,
    budget: FrameBudget,
    cancelled: Callable[[], bool] | None = None,
    accepts: Callable[[FrameReadResult], bool] | None = None,
    progress: Callable[[tuple[float, float]], None] | None = None,
) -> FrameReadResult:
    _check(cancelled)
    source.assert_unchanged()
    if info.video_stream_index is None:
        raise MediaError("NO_VIDEO_TRACK", "This source has no usable video track")
    if mode not in ("overview", "detail", "motion"):
        raise MediaError("INVALID_ARGUMENT", "Unknown frame-reading mode")
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise MediaError("INVALID_ARGUMENT", "Invalid video interval")
    if crop is not None and (
        len(crop) != 4
        or any(
            isinstance(v, bool)
            or not isinstance(v, (int, float))
            or not math.isfinite(v)
            for v in crop
        )
        or not (0 <= crop[0] < crop[2] <= 1 and 0 <= crop[1] < crop[3] <= 1)
    ):
        raise MediaError(
            "INVALID_ARGUMENT", "crop must be normalized [left,top,right,bottom]"
        )
    requested_end = end
    video_ended = info.video_end is not None and end > info.video_end
    if video_ended:
        end = info.video_end
        if start >= end:
            raise MediaError(
                "NO_VIDEO_IN_RANGE",
                "The selected video stream has ended before this range",
            )
    if mode != "motion" and progress is not None:
        progress((start, end))
    if mode == "motion":
        samples, processed_end, complete = _scan(
            source, info, start, end, budget.analysis_frames, crop, cancelled,
            first_interval=progress,
        )
    elif mode == "detail":
        samples = []
        positions = [(0, start), (2, math.nextafter(end, start))]
        if budget.analysis_frames >= 3:
            positions.insert(1, (1, start + (end - start) / 2))
        per_position = max(1, budget.analysis_frames // len(positions))
        for role, target in positions:
            t, until, image = _visible(source, info, target, cancelled, range_end=end)
            visible_duration = until - t if until is not None else 0
            radius = (
                min((end - start) / 6, visible_duration * max(1, per_position // 2))
                if visible_duration > 0
                else 0
            )
            image = _crop(image, crop)
            image.thumbnail((256, 256), Image.Resampling.LANCZOS)
            base = Sample(t, image, role + 1, until)
            neighborhood, _, _ = _scan(
                source,
                info,
                max(start, t - radius),
                min(end, max(t + visible_duration, t + radius)),
                per_position,
                crop,
                cancelled,
            )
            neighborhood = {
                sample.t: replace(sample, window=role + 1) for sample in neighborhood
            }
            neighborhood[t] = base
            if len(neighborhood) > per_position:
                remove = max(
                    (stamp for stamp in neighborhood if stamp != t),
                    key=lambda stamp: abs(stamp - t),
                )
                del neighborhood[remove]
            nearby = sorted(neighborhood.values(), key=lambda sample: sample.t)
            center = next(i for i, sample in enumerate(nearby) if sample.t == t)
            stable = {center}
            base_view = features(image)
            for step in (-1, 1):
                position = center + step
                while 0 <= position < len(nearby):
                    global_change, local_change = distance(
                        base_view, features(nearby[position].image)
                    )
                    neighbor = position - step
                    before, after = sorted(
                        (nearby[position], nearby[neighbor]),
                        key=lambda sample: sample.t,
                    )
                    gap = before.end is None or before.end + 1e-9 < after.t
                    if gap or global_change > 3 or local_change > 0.15:
                        break
                    stable.add(position)
                    position += step
            best = max(
                stable,
                key=lambda i: (sharpness(nearby[i].image), -abs(nearby[i].t - t)),
            )
            nearby[best] = replace(nearby[best], required=(role,))
            for sample in nearby:
                existing = next(
                    (i for i, old in enumerate(samples) if old.t == sample.t), None
                )
                if existing is None:
                    samples.append(sample)
                elif sample.required:
                    # A single source frame remains one observation, even when
                    # several requested target positions land inside it.
                    samples[existing] = replace(
                        samples[existing],
                        required=tuple(
                            sorted(set(samples[existing].required + sample.required))
                        ),
                    )
        samples.sort(key=lambda sample: sample.t)
        if len(samples) > budget.analysis_frames:
            raise MediaError(
                "RESOURCE_BOUNDARY",
                "Endpoint observations exceed this call analysis allocation",
            )
        processed_end, complete = end, True
    else:
        # A bounded continuous prefix supplies actual change candidates; seek
        # anchors guarantee full-range sparse coverage even for long videos.
        scan_capacity = max(1, budget.analysis_frames // 2)
        samples, _, _ = _scan(source, info, start, end, scan_capacity, crop, cancelled)
        anchor_slots = budget.analysis_frames - len(samples)
        if (
            anchor_slots == 0
            and samples
            and (samples[-1].end is None or samples[-1].end < end)
        ):
            raise MediaError(
                "RESOURCE_BOUNDARY",
                "Full-range sparse coverage needs another analysis slot",
            )
        for k in range(anchor_slots):
            target = (
                end
                if anchor_slots == 1
                else start + (end - start) * k / (anchor_slots - 1)
            )
            target = min(target, math.nextafter(end, start))
            if any(
                sample.t
                <= target
                < (sample.end if sample.end is not None else sample.t)
                for sample in samples
            ):
                continue
            t, until, image = _visible(source, info, target, cancelled, range_end=end)
            if all(sample.t != t for sample in samples):
                image = _crop(image, crop)
                image.thumbnail((256, 256), Image.Resampling.LANCZOS)
                samples.append(Sample(t, image, k + 1, until))
        samples.sort(key=lambda s: s.t)
        processed_end, complete = end, True
    if not samples:
        raise MediaError(
            "MEDIA_DECODE_FAILED", "No visible source frames in requested interval"
        )
    while True:
        _check(cancelled)
        result = _render_window(
            source, info, samples, start, requested_end, processed_end, complete,
            video_ended, mode, crop, budget, cancelled, accepts,
        )
        if result is not None:
            return result
        if mode != "motion" or len(samples) == 1:
            raise MediaError(
                "RESOURCE_BOUNDARY",
                "Readable evidence and complete time index do not fit this call",
            )
        # Reuse the decoded prefix. Its first omitted PTS is both the exclusive
        # end of this window and the exact start of the next invocation.
        keep = (len(samples) + 1) // 2
        processed_end = samples[keep].t
        samples = samples[:keep]
        complete = False


def _render_window(
    source, info, samples, start, requested_end, processed_end, complete,
    video_ended, mode, crop, budget, cancelled, accepts,
):
    # Keep cut/scan evidence from the actual decoded windows. Filtering the
    # output selection must never create adjacency across a discarded frame.
    scanned_ranges = []
    for window in sorted({sample.window for sample in samples}):
        group = [sample for sample in samples if sample.window == window]
        if len(group) > 1 or window == 0:
            scanned_ranges.append(
                [
                    max(start, group[0].t),
                    min(processed_end, group[-1].end or group[-1].t),
                ]
            )
    changes = detect_changes(samples)
    observed_cut_times = [samples[i].t for i in changes.cuts]
    if mode == "detail":
        useful = set(changes.candidates) | {
            i for i, sample in enumerate(samples) if sample.required
        }
        samples = [sample for i, sample in enumerate(samples) if i in useful]
    # Readable cell area sets the initial frame capacity; no fixed frame cap.
    capacity = min(len(samples), max(1, budget.image_pixels // (160 * 90)))
    if budget.maximum_frames is not None:
        capacity = min(capacity, budget.maximum_frames)
    if mode == "detail":
        capacity = min(capacity, budget.maximum_images)
    required = 1 if len(samples) == 1 else 2
    if mode == "detail" and capacity < required:
        raise MediaError(
            "RESOURCE_BOUNDARY", "Clear endpoint images do not fit this call"
        )
    target = {"kind": "PATH", "path": str(source.path)}
    while capacity >= required:
        _check(cancelled)
        try:
            selected, layout = _layout(samples, changes, mode, budget, capacity)
        except MotionEvidenceOverflow:
            # Reducing frame count further cannot retain the protected observations.
            return None
        except ValueError:
            capacity -= 1
            continue
        pages = [Image.new("RGB", size, "#202020") for size in layout.page_sizes]
        index = []
        for cell in layout.cells:
            _check(cancelled)
            sample = samples[selected[cell.index]]
            t, until, original = _visible(source, info, sample.t, cancelled)
            if abs(t - sample.t) > 1e-8:
                raise MediaError(
                    "MEDIA_SOURCE_CHANGED",
                    "Selected presentation frame changed during reading",
                )
            original = _crop(original, crop)
            left, top, right, bottom = cell.rect
            rendered = original.resize(
                (right - left, bottom - top), Image.Resampling.LANCZOS
            )
            pages[cell.page].paste(rendered, (left, top))
            draw = ImageDraw.Draw(pages[cell.page])
            label = str(cell.index + 1)
            box = draw.textbbox((left + 4, top + 3), label)
            draw.rectangle((left, top, box[2] + 5, box[3] + 4), fill="black")
            draw.text((left + 4, top + 3), label, fill="white")
            w, h = layout.page_sizes[cell.page]
            item = {
                "n": cell.index + 1,
                "image": cell.page + 1,
                "t": t,
                "cell_rect": [left / w, top / h, right / w, bottom / h],
            }
            if index:
                item["dt"] = t - index[-1]["t"]
            if t < start or (until is not None and until > processed_end):
                item["visible_range"] = [max(t, start), min(until or processed_end, processed_end)]
            if mode == "overview":
                before = (
                    start
                    if cell.index == 0
                    else (samples[selected[cell.index - 1]].t + t) / 2
                )
                after = (
                    processed_end
                    if cell.index + 1 == len(selected)
                    else (t + samples[selected[cell.index + 1]].t) / 2
                )
                item["representative_range"] = [max(start, before), min(processed_end, after)]
            index.append(item)
        images = []
        for page in pages:
            _check(cancelled)
            stream = io.BytesIO()
            page.save(stream, format="JPEG", quality=90)
            images.append(
                FrameImage(stream.getvalue(), "image/jpeg", page.width, page.height)
            )
        next_call = (
            None
            if complete
            else {
                "tool": "read_video_frames",
                "arguments": {
                    "target": target,
                    "from": processed_end,
                    "to": requested_end,
                    "mode": mode,
                    **({"crop": list(crop)} if crop is not None else {}),
                },
            }
        )
        limitations = [
            "Sampling does not rule out unobserved brief changes; ordered does not mean every source frame."
        ]
        if mode == "detail":
            limitations.append(
                "Local sharpness is a pixel proxy; unselected pages and text are not fully read."
            )
        if video_ended:
            limitations.append(
                "The selected video stream ends before the requested endpoint; no further video frames exist in the trailing range."
            )
        if not complete:
            limitations.append(
                "Only the processed prefix is represented; continue using next."
            )
        body = {
            "video_frames": True,
            "target": target,
            "media_version": {
                "modified_ns": str(source.modified_ns),
                "size": source.size,
            },
            "requested_range": [start, requested_end],
            "processed_range": [start, processed_end],
            "sampling": "sparse" if mode == "overview" else "ordered",
            "complete": complete and not video_ended,
            "display_size": [info.width, info.height],
            "crop": list(crop) if crop else [0, 0, 1, 1],
            "frames": index,
            "next": next_call,
            "limitations": limitations,
        }
        body["scanned_ranges"] = scanned_ranges
        body["observed_cut_times"] = observed_cut_times
        if mode == "overview":
            duration = "unknown" if info.duration is None else f"{info.duration:g}s"
            body["metadata"] = (
                f"{duration} · {info.width}×{info.height} · "
                + ("有音轨" if info.audio_stream_index is not None else "无音轨")
                + " · "
                + ("有文字字幕" if info.subtitle_stream_indices else "无文字字幕")
            )
        result = FrameReadResult(
            tuple(images),
            json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            (start, processed_end),
            complete and not video_ended,
        )
        source.assert_unchanged()
        if sum(len(image.data) for image in images) <= budget.maximum_bytes and (
            accepts is None or accepts(result)
        ):
            return result
        capacity -= 1
    return None
