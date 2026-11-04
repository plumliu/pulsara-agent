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


def overview_priority(samples: Sequence[Sample], allocation: int) -> tuple[int, ...]:
    """Frozen sparse evidence, in endpoint/temporal-refinement priority order.

    Groups only compare sampled pixels with their fixed first observation.
    They do not establish an unchanged interval or identify semantic scenes.
    The allocation is the orientation profile's cell geometry, never the
    delivery page's remaining allowance.
    """
    if not samples:
        return ()
    if allocation < min(2, len(samples)):
        raise ValueError("overview allocation must preserve temporal endpoints")
    views = [features(sample.image) for sample in samples]
    groups: list[list[int]] = []
    for i in range(len(samples)):
        if groups:
            global_change, local_change = distance(views[groups[-1][0]], views[i])
            if global_change <= 3 and local_change <= 0.15:
                groups[-1].append(i)
                continue
        groups.append([i])
    representatives, novelty = [], []
    for group_index, group in enumerate(groups):
        midpoint = (samples[group[0]].t + samples[group[-1]].t) / 2
        representatives.append(
            max(
                group,
                key=lambda i: (
                    sharpness(samples[i].image),
                    -abs(samples[i].t - midpoint),
                    -samples[i].t,
                ),
            )
        )
        neighboring = (
            j for j in (group_index - 1, group_index + 1) if 0 <= j < len(groups)
        )
        novelty.append(
            max(
                (
                    max(global_change / 10, local_change / 0.2)
                    for global_change, local_change in (
                        distance(views[group[0]], views[groups[j][0]])
                        for j in neighboring
                    )
                ),
                default=0.0,
            )
        )
    selected = [0] if len(samples) == 1 else [0, len(samples) - 1]
    represented = {
        group_index
        for group_index, group in enumerate(groups)
        if any(index in group for index in selected)
    }
    while len(selected) < allocation and len(represented) < len(groups):

        def coverage(group_index):
            t = samples[representatives[group_index]].t
            left = max(samples[i].t for i in selected if samples[i].t < t)
            right = min(samples[i].t for i in selected if samples[i].t > t)
            # The largest eligible gap comes first. Native PTS quantization
            # must not prefer refining a shorter but better-centered gap.
            return right - left, min(t - left, right - t), novelty[group_index], -t

        group_index = max(
            (i for i in range(len(groups)) if i not in represented),
            key=coverage,
        )
        selected.append(representatives[group_index])
        represented.add(group_index)
    return tuple(selected)
