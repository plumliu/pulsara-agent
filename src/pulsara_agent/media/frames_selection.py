"""Pixel-only candidate evidence, confined to actually scanned PTS windows.

Thresholds are initial low-resolution sampling parameters, not semantic event
recognizers. Their noise/interval dependence is reported by the frame reader.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Sequence
from PIL import Image, ImageChops, ImageFilter, ImageStat


@dataclass(frozen=True)
class Sample:
    t: float
    image: Image.Image
    window: int
    end: float | None = None
    required: tuple[int, ...] = ()


@dataclass(frozen=True)
class Changes:
    candidates: tuple[int, ...]
    round_trips: tuple[tuple[int, int, int], ...]
    cuts: tuple[int, ...]
    global_scores: tuple[float, ...]
    local_scores: tuple[float, ...]


def features(image: Image.Image) -> tuple[Image.Image, Image.Image]:
    ratio = image.width / image.height
    size = (
        (64, max(1, round(64 / ratio)))
        if ratio >= 1
        else (max(1, round(64 * ratio)), 64)
    )
    return image.resize(size).convert("HSV"), image.resize(
        (size[0] * 4, size[1] * 4)
    ).convert("L")


def distance(
    a: tuple[Image.Image, Image.Image], b: tuple[Image.Image, Image.Image]
) -> tuple[float, float]:
    hsv_a, lum_a = a
    hsv_b, lum_b = b
    total = 0.0
    for x, y in zip(hsv_a.get_flattened_data(), hsv_b.get_flattened_data()):
        hue = min(abs(x[0] - y[0]), 256 - abs(x[0] - y[0])) * 2
        total += min(x[1], y[1]) / 255 * hue + abs(x[1] - y[1]) + abs(x[2] - y[2])
    global_score = total / (3 * hsv_a.width * hsv_a.height)
    diff = ImageChops.difference(lum_a, lum_b)
    local = 0.0
    for top in range(0, diff.height, 16):
        for left in range(0, diff.width, 16):
            block = diff.crop(
                (left, top, min(left + 16, diff.width), min(top + 16, diff.height))
            )
            histogram = block.histogram()
            local = max(local, sum(histogram[13:]) / (block.width * block.height))
    return global_score, local


def sharpness(image: Image.Image) -> float:
    return ImageStat.Stat(image.convert("L").filter(ImageFilter.FIND_EDGES)).var[0]


def detect_changes(samples: Sequence[Sample]) -> Changes:
    views = [features(s.image) for s in samples]
    scores, local = [0.0] * len(samples), [0.0] * len(samples)
    candidates, cuts, trips = set(), [], []
    accumulator, last = 0.0, float("-inf")
    # Thresholds are frozen for this initial 64-long-edge/256 luminance
    # implementation; no DSH source or its fps-specific defaults are copied.
    for i in range(1, len(samples)):
        if samples[i].window != samples[i - 1].window:
            accumulator = 0.0
            last = float("-inf")
            continue
        scores[i], local[i] = distance(views[i - 1], views[i])
    for i in range(1, len(samples)):
        if samples[i].window != samples[i - 1].window:
            accumulator = 0.0
            last = float("-inf")
            continue
        following = (
            scores[i + 1]
            if i + 1 < len(samples) and samples[i + 1].window == samples[i].window
            else None
        )
        if (
            following is not None
            and scores[i] >= 10
            and scores[i] > 2 * max(1, scores[i - 1], following)
            and samples[i].t - last >= 0.15
        ):
            cuts.append(i)
            candidates.update((i - 1, i))
            accumulator, last = 0.0, samples[i].t
        else:
            accumulator += scores[i]
            if accumulator >= 90 and samples[i].t - last >= 0.1:
                candidates.add(i)
                accumulator, last = 0.0, samples[i].t
        if local[i] >= 0.2:
            candidates.update((i - 1, i))
    # Baseline, maximum observed deviation, return: scans reset at every gap.
    # Search is linear and keeps no cross-window history or state identities.
    baseline = 0
    peak = 0
    maximum = 0.0
    for i in range(1, len(samples)):
        if samples[i].window != samples[i - 1].window:
            baseline, peak, maximum = i, i, 0.0
            continue
        g, local_score = distance(views[baseline], views[i])
        deviation = max(g / 10, local_score / 0.2)
        if deviation > maximum:
            maximum, peak = deviation, i
        if maximum >= 1 and deviation <= 0.35 and peak > baseline and i > peak:
            trips.append((baseline, peak, i))
            candidates.update((baseline, peak, i))
            baseline, peak, maximum = i, i, 0.0
    # A short excursion can also take place inside an entirely new scene.
    # Check consecutive observed transitions against their own local baseline.
    # Keep the widest excursion above for gradual changes without sharp edges.
    previous_edge = None
    returns = {after for _, _, after in trips}
    for i in range(1, len(samples)):
        if samples[i - 1].window != samples[i].window:
            previous_edge = None
            continue
        if scores[i] < 10 and local[i] < 0.2:
            continue
        if previous_edge is not None and i not in returns:
            before = previous_edge - 1
            g, luma = distance(views[before], views[i])
            if max(g / 10, luma / 0.2) <= 0.35:

                def deviation(index):
                    g, luma = distance(views[before], views[index])
                    return max(g / 10, luma / 0.2)

                middle = max(range(previous_edge, i), key=deviation)
                trips.append((before, middle, i))
                candidates.update((before, middle, i))
        previous_edge = i
    trips.sort()
    return Changes(
        tuple(sorted(candidates)),
        tuple(trips),
        tuple(cuts),
        tuple(scores),
        tuple(local),
    )


def uniform_indices(samples: Sequence[Sample], count: int) -> list[int]:
    if count <= 0 or not samples:
        return []
    if count == 1:
        return [0]
    a, b = samples[0].t, samples[-1].t
    # Nearest visible interval start, rather than fabricated target timestamps.
    result = set()
    cursor = 0
    for k in range(count):
        target = b if k == count - 1 else a + (b - a) * k / (count - 1)
        while cursor + 1 < len(samples) and samples[cursor + 1].t <= target:
            cursor += 1
        result.add(cursor)
    return sorted(result)


class MotionEvidenceOverflow(ValueError):
    """The observed process needs more cells than this window can deliver."""


def select_samples(
    samples: Sequence[Sample], changes: Changes, count: int, mode: str
) -> list[int]:
    if len(samples) <= count:
        return list(range(len(samples)))
    if mode == "detail":
        required = {
            role: i for i, sample in enumerate(samples) for role in sample.required
        }
        selected = {required[role] for role in (0, 2) if role in required}
        if len(selected) > count:
            raise ValueError("detail endpoints exceed image allowance")
        if 1 in required and len(selected) < count:
            selected.add(required[1])
        for i in sorted(
            range(len(samples)),
            key=lambda i: (-sharpness(samples[i].image), samples[i].t),
        ):
            if len(selected) >= count:
                break
            selected.add(i)
        return sorted(selected)
    if mode == "motion":
        protected = set()
        final = len(samples) - 1
        last_change = max(changes.candidates, default=-1)
        for before, middle, after in changes.round_trips:
            # A later independently observed return can also be the temporal
            # endpoint when no later change was observed. This removes only
            # adjacent same-state redundancy; the B evidence remains intact.
            if last_change <= after:
                global_change, local_change = distance(
                    features(samples[after].image), features(samples[final].image)
                )
                if global_change <= 3.5 and local_change <= 0.07:
                    after = final
            protected.update((before, middle, after))
        protected.update((0, final))
        if len(protected) > count:
            raise MotionEvidenceOverflow(
                "observed round trip and temporal endpoints exceed image allowance"
            )
        # The uniform skeleton reserves endpoints; observed round-trip groups
        # consume only slots reserved for supplementary evidence.
        selected = (
            set(uniform_indices(samples, max(2, count - len(protected) + 2)))
            | protected
        )
        if len(selected) > count:
            raise MotionEvidenceOverflow(
                "motion skeleton and observed process exceed image allowance"
            )
    else:
        selected = set(uniform_indices(samples, max(2, (count + 1) // 2)))
    # Time partitions alternate; high motion at one point cannot monopolize
    # all slots. Adjacent equivalent states may be omitted, never ABA facts.
    bins: list[list[int]] = [[] for _ in range(max(1, len(selected) - 1))]
    span = samples[-1].t - samples[0].t
    for i in changes.candidates:
        bucket = (
            min(len(bins) - 1, int((samples[i].t - samples[0].t) / span * len(bins)))
            if span
            else 0
        )
        bins[bucket].append(i)
    while len(selected) < count and any(bins):
        for bucket in bins:
            if bucket and len(selected) < count:
                selected.add(bucket.pop(0))
    if len(selected) < count:
        for i in uniform_indices(samples, count):
            selected.add(i)
            if len(selected) == count:
                break
    return sorted(selected)
