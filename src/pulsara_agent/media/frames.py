"""Deterministic video evidence plans and independently admitted image pages.

PyAV owns decoding through frames_decode; the source owns exact presentation
positions and file identity. Selection happens once before delivery sizing.
Pillow and the ordered-row DP only render the requested immutable plan slice.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
import io
import json
import math
from typing import Callable, Literal

from PIL import Image, ImageDraw

from pulsara_agent.llm.input import MAXIMUM_PROMPT_IMAGE_PIXELS
from .source import FramePosition, MediaError, MediaInfo, MediaSource
from .frames_decode import FrameTarget, VideoDecoder
from .frames_layout import Cell, Layout, maximum_cell_width, pack_rows, row_candidates
from .frames_selection import (
    Sample,
    detect_changes,
    distance,
    features,
    overview_priority,
    sharpness,
)

FrameMode = Literal["overview", "detail", "motion"]
# This policy changes only at a declared selection-contract revision. Its
# physical analysis slots reuse the existing total image-raster allocation;
# neither provider remaining allowance nor the current page changes the plan.
FRAME_SELECTION_REVISION = "frames-v1"
FRAME_ANALYSIS_FRAMES = max(3, MAXIMUM_PROMPT_IMAGE_PIXELS // (256 * 256))


@dataclass(frozen=True)
class FrameBudget:
    """Call-local physical render and low-resolution working-set admission."""

    maximum_bytes: int
    image_pixels: int
    maximum_images: int
    analysis_frames: int

    def __post_init__(self):
        if any(
            type(value) is not int or value < 1
            for value in (
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


@dataclass(frozen=True)
class FrameObservation:
    position: FramePosition
    t: Fraction
    end: Fraction | None
    representative_range: tuple[Fraction, Fraction] | None = None


@dataclass(frozen=True)
class FramePlan:
    priority: tuple[FrameObservation, ...]
    requested_range: tuple[float, float]
    processed_range: tuple[Fraction, Fraction]
    mode: FrameMode
    crop: tuple[float, float, float, float] | None
    display_aspect: Fraction
    complete: bool
    video_ended: bool
    next_window_start: FramePosition | None = None
    scanned_ranges: tuple[tuple[Fraction, Fraction], ...] = ()
    observed_cut_times: tuple[Fraction, ...] = ()
    probe_count: int = 0
    max_probe_gap: float = 0.0

    @property
    def observation_count(self) -> int:
        return len(self.priority)


def _check(cancelled):
    if cancelled and cancelled():
        raise InterruptedError("video reading cancelled")


def _fraction(value) -> Fraction:
    return value if isinstance(value, Fraction) else Fraction(str(value))


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


def _analysis_view(image):
    scale = min(1, 256 / image.width, 256 / image.height)
    return image.resize(
        (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
        Image.Resampling.LANCZOS,
    )


def _canvas(aspect, pixels=1280 * 720):
    aspect = float(aspect)
    scale = min(1280, math.sqrt(pixels * max(aspect, 1 / aspect)))
    width = int(scale if aspect >= 1 else scale * aspect)
    height = int(scale / aspect if aspect >= 1 else scale)
    return max(1, width), max(1, height)


def scene_dimensions(
    aspect: Fraction | float, *, floor: bool = False
) -> tuple[int, int]:
    """Aspect-preserving cell satisfying both scene axes, including odd ratios."""
    aspect = _fraction(aspect)
    long, short = (160, 90) if floor else (320, 180)
    required_width, required_height = (long, short) if aspect >= 1 else (short, long)
    width = max(required_width, math.ceil(required_height * aspect))
    height = max(required_height, math.ceil(Fraction(width, 1) / aspect))
    return width, height


def overview_allocation(aspect: Fraction | float) -> int:
    width, height = _canvas(aspect)
    cell_width, cell_height = scene_dimensions(aspect)
    # Independent first/last observations remain meaningful when an unusual
    # aspect needs separate pages rather than one preferred canvas.
    return max(2, (width // cell_width) * (height // cell_height))


def overview_probe_count(aspect: Fraction | float, duration: Fraction | float) -> int:
    return min(2 * overview_allocation(aspect) - 1, max(3, math.ceil(duration) + 1))


def _validate_request(start, end, mode, crop):
    if mode not in ("overview", "detail", "motion"):
        raise MediaError("INVALID_ARGUMENTS", "Unknown frame-reading mode")
    if (
        any(
            isinstance(v, bool)
            or not isinstance(v, (int, float, Fraction))
            or not math.isfinite(v)
            for v in (start, end)
        )
        or start < 0
        or end <= start
    ):
        raise MediaError("INVALID_ARGUMENTS", "Invalid video interval")
    if crop is not None and (
        len(crop) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in crop)
        or not (0 <= crop[0] < crop[2] <= 1 and 0 <= crop[1] < crop[3] <= 1)
    ):
        raise MediaError(
            "INVALID_ARGUMENTS", "crop must be normalized [left,top,right,bottom]"
        )


def prepare_frame_plan(
    source: MediaSource,
    info: MediaInfo,
    *,
    start: float,
    end: float,
    mode: FrameMode = "overview",
    crop: tuple[float, float, float, float] | None = None,
    window_start: FramePosition | None = None,
    analysis_frames: int = FRAME_ANALYSIS_FRAMES,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[tuple[float, float]], None] | None = None,
) -> FramePlan:
    """Prepare a bounded, exact source plan without any delivery allowance."""
    _check(cancelled)
    source.assert_unchanged()
    _validate_request(start, end, mode, crop)
    if type(analysis_frames) is not int or analysis_frames < 1:
        raise ValueError("analysis admission must be positive")
    if info.video_stream_index is None:
        raise MediaError("NO_VIDEO_TRACK", "This source has no usable video track")
    if mode != "motion" and window_start is not None:
        raise MediaError(
            "INVALID_ARGUMENTS", "Only motion has a source window position"
        )
    start_time, requested_end = _fraction(start), _fraction(end)
    end_time = (
        min(requested_end, info.video_end)
        if info.video_end is not None
        else requested_end
    )
    if start_time >= end_time:
        raise MediaError(
            "NO_VIDEO_IN_RANGE", "The selected video track has ended before this range"
        )
    if info.video_start is not None and info.video_start >= end_time:
        raise MediaError(
            "NO_VIDEO_IN_RANGE", "The selected video track starts after this range"
        )
    video_ended = end_time < requested_end
    crop = tuple(crop) if crop is not None else None
    samples: list[Sample] = []
    observations: dict[Fraction, FrameObservation] = {}
    analysis_images: dict[Fraction, Image.Image] = {}
    display_aspect = None

    def publish(span):
        if progress is not None:
            progress((float(span[0]), float(span[1])))

    def take(visible, window=0, required=()):
        nonlocal display_aspect
        _check(cancelled)
        if visible.t not in analysis_images:
            admit_count(len(analysis_images) + 1)
            image = _crop(visible.image, crop)
            if display_aspect is None:
                display_aspect = Fraction(image.width, image.height)
            analysis_images[visible.t] = _analysis_view(image)
        sample = Sample(
            visible.t, analysis_images[visible.t], window, visible.end, required
        )
        observations[visible.t] = FrameObservation(
            visible.position, visible.t, visible.end
        )
        return sample

    def admit_count(count):
        if count > analysis_frames:
            raise MediaError(
                "RESOURCE_BOUNDARY",
                "The fixed frame analysis plan exceeds this call's low-resolution working-set admission",
            )

    scanned = []
    next_window = None
    complete = True
    processed_start, processed_end = start_time, end_time
    with VideoDecoder(source, info, cancelled=cancelled) as decoder:
        if mode == "overview":
            publish((start_time, end_time))
            # Crop geometry is known before decoding, so probe work cannot be
            # changed by the images' eventual encoded cost or page allowance.
            width, height = info.width, info.height
            if crop is not None:
                width = math.ceil(crop[2] * width) - math.floor(crop[0] * width)
                height = math.ceil(crop[3] * height) - math.floor(crop[1] * height)
            aspect = Fraction(width, height)
            probe_start = (
                max(start_time, info.video_start)
                if info.video_start is not None
                else start_time
            )
            if probe_start >= end_time:
                raise MediaError(
                    "NO_VIDEO_IN_RANGE",
                    "The selected video track starts after this range",
                )
            count = overview_probe_count(aspect, end_time - probe_start)
            targets = [
                FrameTarget(
                    probe_start + (end_time - probe_start) * k / (count - 1),
                    before=k == count - 1,
                )
                for k in range(count)
            ]
            for visible in decoder.visible_many(targets, range_end=end_time):
                if visible.t not in observations:
                    admit_count(len(samples) + 1)
                    samples.append(take(visible, len(samples) + 1))
                del visible
            samples.sort(key=lambda sample: sample.t)
            priority = overview_priority(samples, overview_allocation(aspect))
        elif mode == "motion":
            scan_start = (
                start_time
                if window_start is None
                else window_start.absolute_time - info.timeline_origin
            )
            if scan_start < start_time or scan_start >= end_time:
                raise MediaError(
                    "INVALID_ARGUMENTS", "Motion window is outside the request"
                )
            last = None
            last_next_position = None
            for visible in decoder.scan(
                window_start if window_start is not None else scan_start,
                end_time,
                FRAME_ANALYSIS_FRAMES,
                first_interval=publish,
            ):
                if (
                    last is None
                    and window_start is not None
                    and visible.position != window_start
                ):
                    raise MediaError(
                        "INVALID_ARGUMENTS",
                        "Motion continuation is not an exact source-frame boundary",
                    )
                admit_count(len(samples) + 1)
                samples.append(take(visible))
                last = observations[visible.t]
                last_next_position = visible.next_position
                del visible
            if last is not None:
                processed_start = scan_start
                if last.end is None:
                    raise MediaError(
                        "MEDIA_FRAME_UNAVAILABLE",
                        "The final visible interval has no verifiable end",
                    )
                processed_end = min(end_time, last.end)
                complete = last.end >= end_time or last_next_position is None
                next_window = None if complete else last_next_position
                if not complete and next_window is None:
                    raise MediaError(
                        "MEDIA_FRAME_UNAVAILABLE",
                        "The motion window has no exact next source position",
                    )
                if complete and processed_end < end_time:
                    video_ended = True
                scanned.append((max(scan_start, samples[0].t), processed_end))
            priority = tuple(range(len(samples)))
        else:
            publish((start_time, end_time))
            base_samples = []
            roles = (0, 1, 2)
            targets = (
                FrameTarget(start_time),
                FrameTarget((start_time + end_time) / 2),
                FrameTarget(end_time, before=True),
            )
            base_frames = iter(decoder.visible_many(targets, range_end=end_time))
            for role in roles:
                visible = next(base_frames)
                base_samples.append((role, take(visible, role + 1)))
                del visible
            if next(base_frames, None) is not None:
                raise RuntimeError(
                    "Decoder returned more detail observations than requested"
                )
            per_position = max(1, FRAME_ANALYSIS_FRAMES // len(base_samples))
            combined = {}
            for role, base in base_samples:
                duration = base.end - base.t if base.end is not None else Fraction(0)
                radius = min(
                    (end_time - start_time) / 6, duration * max(1, per_position // 2)
                )
                left = max(start_time, base.t - radius)
                right = min(end_time, max(base.end or base.t, base.t + radius))
                nearby = {}
                if left < right:
                    # The independently resolved base owns one of this location's
                    # slots even when dense pre-roll consumes the scan prefix.
                    for visible in decoder.scan(left, right, max(1, per_position - 1)):
                        nearby[visible.t] = take(visible, role + 1)
                        del visible
                    if nearby:
                        last = nearby[max(nearby)]
                        scanned.append(
                            (max(left, min(nearby)), min(right, last.end or last.t))
                        )
                nearby[base.t] = base
                if len(nearby) > per_position:
                    del nearby[
                        max(
                            (t for t in nearby if t != base.t),
                            key=lambda t: abs(t - base.t),
                        )
                    ]
                ordered = sorted(nearby.values(), key=lambda sample: sample.t)
                center = next(
                    i for i, sample in enumerate(ordered) if sample.t == base.t
                )
                stable = {center}
                base_view = features(base.image)
                for step in (-1, 1):
                    position = center + step
                    while 0 <= position < len(ordered):
                        global_change, local_change = distance(
                            base_view, features(ordered[position].image)
                        )
                        before, after = sorted(
                            (ordered[position], ordered[position - step]),
                            key=lambda sample: sample.t,
                        )
                        if (
                            before.end is None
                            or before.end < after.t
                            or global_change > 3
                            or local_change > 0.15
                        ):
                            break
                        stable.add(position)
                        position += step
                best = max(
                    stable,
                    key=lambda i: (
                        sharpness(ordered[i].image),
                        -abs(ordered[i].t - base.t),
                        -ordered[i].t,
                    ),
                )
                ordered[best] = replace(ordered[best], required=(role,))
                for sample in ordered:
                    previous = combined.get(sample.t)
                    if previous is None:
                        combined[sample.t] = sample
                    elif sample.required:
                        combined[sample.t] = replace(
                            previous,
                            required=tuple(
                                sorted(set(previous.required + sample.required))
                            ),
                        )
                admit_count(len(combined))
            samples = sorted(combined.values(), key=lambda sample: sample.t)
            changes = detect_changes(samples)
            required = {
                role: i for i, sample in enumerate(samples) for role in sample.required
            }
            selected = []
            for role in (0, 2, 1):
                if role in required and required[role] not in selected:
                    selected.append(required[role])
            for i in changes.candidates:
                if i not in selected:
                    selected.append(i)
            priority = tuple(selected)
    if not samples or display_aspect is None:
        raise MediaError(
            "MEDIA_DECODE_FAILED", "No visible source frames in requested interval"
        )
    ordered_observations = [observations[samples[i].t] for i in priority]
    if mode == "overview":
        by_time = sorted(ordered_observations, key=lambda observation: observation.t)
        ranges = {}
        for i, observation in enumerate(by_time):
            before = start_time if i == 0 else (by_time[i - 1].t + observation.t) / 2
            after = (
                end_time
                if i + 1 == len(by_time)
                else (observation.t + by_time[i + 1].t) / 2
            )
            ranges[observation.t] = (max(start_time, before), min(end_time, after))
        ordered_observations = [
            replace(observation, representative_range=ranges[observation.t])
            for observation in ordered_observations
        ]
    changes = detect_changes(samples) if mode != "overview" else None
    source.assert_unchanged()
    return FramePlan(
        priority=tuple(ordered_observations),
        requested_range=(float(start_time), float(requested_end)),
        processed_range=(processed_start, processed_end),
        mode=mode,
        crop=crop,
        display_aspect=display_aspect,
        complete=complete,
        video_ended=video_ended,
        next_window_start=next_window,
        scanned_ranges=tuple(scanned),
        observed_cut_times=tuple(samples[i].t for i in changes.cuts) if changes else (),
        probe_count=len(samples),
        max_probe_gap=max(
            (float(b.t - a.t) for a, b in zip(samples, samples[1:])), default=0.0
        ),
    )


def _layout_page(plan, observations, budget, floor, cancelled):
    count, aspect = len(observations), plan.display_aspect
    minimum = scene_dimensions(aspect, floor=floor)
    if floor and plan.mode != "overview":
        raise MediaError(
            "RESOURCE_BOUNDARY",
            "Only a sparse overview permits scene-floor degradation; detail and motion retain their mode-specific readable evidence",
        )
    if floor and count != 1:
        raise MediaError(
            "INVALID_ARGUMENTS",
            "Scene-floor degradation is only permitted for a single observation",
        )
    if plan.mode == "detail" and not floor:
        # Spatial detail uses the original clear-image target, not scene-only
        # collage cells; pagination preserves each endpoint independently.
        width, height = _canvas(aspect)
        if width < minimum[0] or height < minimum[1]:
            raise MediaError(
                "RESOURCE_BOUNDARY",
                "Clear detail images do not fit the physical page allocation",
            )
        if count > budget.maximum_images:
            raise MediaError(
                "RESOURCE_BOUNDARY",
                "Detail image count exceeds the physical page allocation",
            )
        return Layout(
            tuple(Cell(i, i, (0, 0, width, height)) for i in range(count)),
            tuple((width, height) for _ in range(count)),
            0,
            count * width * height,
        )
    if count == 1:
        width, height = minimum
        return Layout(
            (Cell(0, 0, (0, 0, width, height)),), ((width, height),), 0, width * height
        )
    width, height = _canvas(aspect)
    width, height = max(width, minimum[0]), max(height, minimum[1])
    if plan.mode == "overview":
        tier = min(8, max(1, math.ceil(math.sqrt(count))))
        while tier > 1 and maximum_cell_width(width, tier) < minimum[0]:
            tier -= 1
        rows = row_candidates([tier] * count, width, height, float(aspect), minimum)
        return pack_rows(
            rows,
            width,
            height,
            maximum_pages=budget.maximum_images,
            cancelled=cancelled,
        )
    # Motion has equal cells, with no density/novelty-dependent geometry.
    options = []
    for columns in range(1, count + 1):
        rows = math.ceil(count / columns)
        cell_width = min(
            width // columns,
            int(height * aspect / rows),
            int(math.sqrt(budget.image_pixels * aspect / (columns * rows))),
        )
        cell_height = math.ceil(Fraction(cell_width, 1) / aspect)
        if cell_width >= minimum[0] and cell_height >= minimum[1]:
            options.append(
                (
                    cell_width,
                    -abs(
                        math.log(
                            (columns * cell_width)
                            / (rows * cell_height)
                            / float(aspect)
                        )
                    ),
                    columns,
                    rows,
                    cell_height,
                )
            )
    if not options:
        raise MediaError(
            "RESOURCE_BOUNDARY",
            "Readable ordered cells do not fit the physical page allocation",
        )
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
    return Layout(
        cells,
        ((columns * cell_width, rows * cell_height),),
        0,
        count * cell_width * cell_height,
    )


def frame_page_fits_geometry(
    plan: FramePlan,
    *,
    offset: int,
    count: int,
    budget: FrameBudget,
    floor: bool = False,
) -> bool:
    """Cheap parent-side physical preflight using the renderer's exact geometry.

    This only skips impossible image-count/pixel candidates before launching
    the existing one-shot worker. Encoded bytes and provider quotes still
    require the real image page; no estimate admits a result.
    """
    if (
        type(offset) is not int
        or type(count) is not int
        or count < 1
        or not 0 <= offset < offset + count <= plan.observation_count
    ):
        return False
    try:
        layout = _layout_page(
            plan, plan.priority[offset : offset + count], budget, floor, None
        )
    except (ValueError, MediaError):
        return False
    return (
        len(layout.page_sizes) <= budget.maximum_images
        and sum(w * h for w, h in layout.page_sizes) <= budget.image_pixels
    )


def render_frame_page(
    source: MediaSource,
    info: MediaInfo,
    plan: FramePlan,
    *,
    offset: int,
    count: int,
    budget: FrameBudget,
    floor: bool = False,
    cancelled: Callable[[], bool] | None = None,
) -> FrameReadResult:
    """Render only the given priority slice; never select or analyze again."""
    _check(cancelled)
    source.assert_unchanged()
    if (
        type(offset) is not int
        or type(count) is not int
        or count < 1
        or not 0 <= offset < offset + count <= plan.observation_count
    ):
        raise MediaError(
            "INVALID_ARGUMENTS", "Frame page is outside the fixed selection plan"
        )
    selected = sorted(
        plan.priority[offset : offset + count], key=lambda observation: observation.t
    )
    try:
        layout = _layout_page(plan, selected, budget, floor, cancelled)
    except ValueError as exc:
        raise MediaError("RESOURCE_BOUNDARY", str(exc)) from exc
    if (
        len(layout.page_sizes) > budget.maximum_images
        or sum(w * h for w, h in layout.page_sizes) > budget.image_pixels
    ):
        raise MediaError(
            "RESOURCE_BOUNDARY", "Readable page exceeds its physical image allocation"
        )
    pages = [Image.new("RGB", size, "#202020") for size in layout.page_sizes]
    index = []
    with VideoDecoder(source, info, cancelled=cancelled) as decoder:
        visible_frames = decoder.visible_many(
            [observation.position for observation in selected]
        )
        for cell, observation in zip(layout.cells, selected, strict=True):
            visible = next(visible_frames)
            _check(cancelled)
            if visible.position != observation.position or visible.t != observation.t:
                raise MediaError(
                    "MEDIA_SOURCE_CHANGED",
                    "Selected presentation frame changed during rendering",
                )
            original = _crop(visible.image, plan.crop)
            left, top, right, bottom = cell.rect
            rendered = original.resize(
                (right - left, bottom - top), Image.Resampling.LANCZOS
            )
            page = pages[cell.page]
            page.paste(rendered, (left, top))
            draw = ImageDraw.Draw(page)
            box = draw.textbbox((left + 4, top + 3), str(cell.index + 1))
            draw.rectangle((left, top, box[2] + 5, box[3] + 4), fill="black")
            draw.text((left + 4, top + 3), str(cell.index + 1), fill="white")
            width, height = layout.page_sizes[cell.page]
            item = {
                "n": cell.index + 1,
                "image": cell.page + 1,
                "t": float(observation.t),
                "cell_rect": [
                    left / width,
                    top / height,
                    right / width,
                    bottom / height,
                ],
            }
            if index:
                item["dt"] = float(observation.t - selected[cell.index - 1].t)
            start, end = plan.processed_range
            if observation.t < start or (
                observation.end is not None and observation.end > end
            ):
                item["visible_range"] = [
                    float(max(observation.t, start)),
                    float(min(observation.end or end, end)),
                ]
            if observation.representative_range is not None:
                item["representative_range"] = list(
                    map(float, observation.representative_range)
                )
            index.append(item)
            # Release the converted source/crop before requesting the next
            # full-resolution raster; zip must not retain a previous image.
            del original, rendered, visible
        if next(visible_frames, None) is not None:
            raise RuntimeError(
                "Decoder returned more render observations than requested"
            )
    images = []
    for page in pages:
        _check(cancelled)
        stream = io.BytesIO()
        page.save(stream, format="JPEG", quality=90)
        images.append(
            FrameImage(stream.getvalue(), "image/jpeg", page.width, page.height)
        )
    if sum(len(image.data) for image in images) > budget.maximum_bytes:
        raise MediaError(
            "RESOURCE_BOUNDARY",
            "Encoded frame page exceeds the physical byte allocation",
        )
    limitations = [
        "Sampling does not rule out unobserved brief changes; ordered does not mean every source frame."
    ]
    if plan.mode == "detail":
        limitations.append(
            "Local sharpness is a pixel proxy; unselected pages and text are not fully read."
        )
    if info.video_start is not None and info.video_start > _fraction(
        plan.requested_range[0]
    ):
        limitations.append(
            "The video track starts after the requested start; the leading range has no video frames."
        )
    if plan.video_ended:
        limitations.append(
            "The video track ends before the requested endpoint; the trailing range has no further video frames."
        )
    if floor:
        limitations.append(
            "Scene-floor image dimensions were used because a target-size atomic page did not fit; small text may be unreadable."
        )
    body = {
        "video_frames": True,
        "target": {"kind": "PATH", "path": str(source.path)},
        "media_version": {"modified_ns": str(source.modified_ns), "size": source.size},
        "requested_range": list(plan.requested_range),
        "processed_range": list(map(float, plan.processed_range)),
        "sampling": "sparse" if plan.mode == "overview" else "ordered",
        "complete": False,
        "display_size": [info.width, info.height],
        "crop": list(plan.crop) if plan.crop else [0, 0, 1, 1],
        "frames": index,
        "next": None,
        "limitations": limitations,
        "scanned_ranges": [[float(a), float(b)] for a, b in plan.scanned_ranges],
        "observed_cut_times": list(map(float, plan.observed_cut_times)),
        "probe_count": plan.probe_count,
        "maximum_probe_gap": plan.max_probe_gap,
    }
    if plan.mode == "overview":
        duration = "unknown" if info.duration is None else f"{info.duration:g}s"
        body["metadata"] = (
            f"{duration} · {info.width}×{info.height} · "
            + ("有音轨" if info.audio_stream_index is not None else "无音轨")
            + " · "
            + ("有文字字幕" if info.subtitle_stream_indices else "无文字字幕")
        )
    source.assert_unchanged()
    return FrameReadResult(
        tuple(images),
        json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        tuple(map(float, plan.processed_range)),
        False,
    )


def read_video_frames(
    source: MediaSource,
    info: MediaInfo,
    *,
    start: float,
    end: float,
    mode: FrameMode = "overview",
    crop: tuple[float, float, float, float] | None = None,
    budget: FrameBudget,
    continuation: object | None = None,
    cancelled: Callable[[], bool] | None = None,
    accepts: Callable[[FrameReadResult], bool] | None = None,
    progress: Callable[[tuple[float, float]], None] | None = None,
) -> FrameReadResult:
    """Convenience admission over the same prepare/render/source-page pipeline."""
    from .frames_paging import (
        FramePagePosition,
        read_frame_continuation,
        with_frame_page_continuation,
    )

    position = (
        FramePagePosition()
        if continuation is None
        else read_frame_continuation(
            continuation,
            source=source,
            info=info,
            start=start,
            end=end,
            mode=mode,
            crop=crop,
            selection_revision=FRAME_SELECTION_REVISION,
        )
    )
    plan = prepare_frame_plan(
        source,
        info,
        start=start,
        end=end,
        mode=mode,
        crop=crop,
        window_start=position.window_start,
        analysis_frames=budget.analysis_frames,
        cancelled=cancelled,
        progress=progress,
    )
    remaining = plan.observation_count - position.next_observation
    if remaining <= 0:
        raise MediaError(
            "INVALID_ARGUMENTS",
            "Frame continuation is outside the fixed selection plan",
        )
    for count in range(remaining, 0, -1):
        for floor in (False, True) if count == 1 and mode == "overview" else (False,):
            if not frame_page_fits_geometry(
                plan,
                offset=position.next_observation,
                count=count,
                budget=budget,
                floor=floor,
            ):
                continue
            try:
                candidate = render_frame_page(
                    source,
                    info,
                    plan,
                    offset=position.next_observation,
                    count=count,
                    budget=budget,
                    floor=floor,
                    cancelled=cancelled,
                )
            except MediaError as exc:
                if exc.code == "RESOURCE_BOUNDARY":
                    continue
                raise
            candidate = with_frame_page_continuation(
                candidate,
                source=source,
                info=info,
                plan=plan,
                position=position,
                count=count,
                selection_revision=FRAME_SELECTION_REVISION,
            )
            if accepts is None or accepts(candidate):
                return candidate
    raise MediaError(
        "RESOURCE_BOUNDARY",
        "One readable source frame, its complete index and continuation do not fit this call",
    )
