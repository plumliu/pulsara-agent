"""Ordered storyboard packing over integer geometry.

Pillow owns raster composition; this module owns the product's ordered-row
candidate set and its finite dynamic program. No third-party implementation
is copied. The optimum is relative to the generated row candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import Callable, Sequence


@dataclass(frozen=True)
class Row:
    start: int
    sizes: tuple[tuple[int, int], ...]
    height: int
    area: int

    @property
    def end(self) -> int:
        return self.start + len(self.sizes)


@dataclass(frozen=True)
class Cell:
    index: int
    page: int
    rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class Layout:
    cells: tuple[Cell, ...]
    page_sizes: tuple[tuple[int, int], ...]
    score: float
    area: int


def maximum_cell_width(width: int, tier: int) -> int:
    # §6.3. Tier 2 has its own 1/2 ceiling. Tiers 3–8 use 1/(k−1).
    divisor = tier if tier <= 2 else tier - 1
    return width // divisor


def row_candidates(
    tiers: Sequence[int],
    width: int,
    height: int,
    aspect: float,
    minimum: tuple[int, int],
) -> tuple[tuple[Row, ...], ...]:
    """Nominal and full-row sizes inside each tier's readable interval."""
    result = []
    for start in range(len(tiers)):
        rows: dict[tuple[tuple[int, int], ...], Row] = {}
        lows, highs, nominal = [], [], []
        for end in range(start, len(tiers)):
            tier = tiers[end]
            if not 1 <= tier <= 8:
                raise ValueError("overview tiers must be in [1,8]")
            lower = 0.5 if tier == 1 else 1 / (tier + 1)
            # Ceil the readability floor; never shrink it to make a row fit.
            low = max(ceil(width * lower), minimum[0], ceil(minimum[1] * aspect))
            high = min(maximum_cell_width(width, tier), int(height * aspect))
            if low > high:
                break
            lows.append(low)
            highs.append(high)
            nominal.append(max(low, min(high, int(width / tier))))
            if sum(lows) > width:
                break
            variants = [tuple(nominal)]
            # Bounded bisection is on integer pixel widths, not normalized
            # state. It terminates when the two representable scales meet.
            lo, hi = 0, width * 8
            while lo < hi:
                mid = (lo + hi + 1) // 2
                sizes = tuple(
                    max(a, min(b, c * mid // width))
                    for a, b, c in zip(lows, highs, nominal)
                )
                if sum(sizes) <= width:
                    lo = mid
                else:
                    hi = mid - 1
            variants.append(
                tuple(
                    max(a, min(b, c * lo // width))
                    for a, b, c in zip(lows, highs, nominal)
                )
            )
            for widths in variants:
                sizes = tuple((w, ceil(w / aspect)) for w in widths)
                row_height = max(h for _, h in sizes)
                if sum(w for w, _ in sizes) <= width and row_height <= height:
                    rows[sizes] = Row(
                        start, sizes, row_height, sum(w * h for w, h in sizes)
                    )
        result.append(tuple(rows.values()))
    return tuple(result)


def pack_rows(
    rows: Sequence[Sequence[Row]],
    width: int,
    height: int,
    *,
    penalty: float = 1.5,
    maximum_pages: int | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> Layout:
    """DP[i,h,pages] with page count only when a physical page bound exists.

    Tracking count prevents a high-scoring but inadmissible prefix from
    eliminating a feasible one when this call has an image-count allowance.
    """
    n = len(rows)
    states: list[dict[tuple[int, int], tuple[float, tuple | None]]] = [
        {} for _ in range(n + 1)
    ]
    states[0][(0, 0)] = (0.0, None)
    height_states: list[dict[int, set[int]]] = [{} for _ in range(n + 1)]
    height_states[0][0] = {0}
    for i in range(n):
        if cancelled and cancelled():
            raise InterruptedError("video layout cancelled")
        for (used, pages), (score, _) in tuple(states[i].items()):
            for row in rows[i]:
                if row.start != i or row.end > n or row.end <= i or row.height > height:
                    raise ValueError("invalid row candidate")
                for new_page in (False, True):
                    if not pages and not new_page:
                        continue
                    y = 0 if new_page else used
                    count = pages + int(new_page)
                    if y + row.height > height or (
                        maximum_pages is not None and count > maximum_pages
                    ):
                        continue
                    value = (
                        score + row.area / (width * height) - penalty * int(new_page)
                    )
                    key = (y + row.height, count)
                    destination = states[row.end]
                    siblings = height_states[row.end].setdefault(key[0], set())
                    # Same prefix/height with fewer pages and no lower score
                    # has every future option of this state. Exact dominance
                    # keeps long candidate lists tractable without a state cap.
                    if any(
                        p <= count and destination[(key[0], p)][0] >= value
                        for p in siblings
                    ):
                        continue
                    for p in tuple(siblings):
                        if p >= count and destination[(key[0], p)][0] <= value:
                            del destination[(key[0], p)]
                            siblings.remove(p)
                    destination[key] = (value, (i, used, pages, row, y, new_page))
                    siblings.add(count)
    if not states[n]:
        raise ValueError("no readable ordered layout fits image allowance")
    key, (score, _) = max(states[n].items(), key=lambda item: item[1][0])
    cells, page_extents = [], {}
    i, (used, pages) = n, key
    while i:
        predecessor = states[i][(used, pages)][1]
        before, old_used, old_pages, row, y, new_page = predecessor
        page = pages - 1
        x = 0
        for offset, (w, h) in enumerate(row.sizes):
            cells.append(Cell(before + offset, page, (x, y, x + w, y + h)))
            right, bottom = page_extents.get(page, (0, 0))
            page_extents[page] = (max(right, x + w), max(bottom, y + h))
            x += w
        i, used, pages = before, old_used, old_pages
    cells.sort(key=lambda cell: cell.index)
    extents = tuple(page_extents[p] for p in range(len(page_extents)))
    area = sum((c.rect[2] - c.rect[0]) * (c.rect[3] - c.rect[1]) for c in cells)
    if len(cells) != n or any(c.rect[2] > width or c.rect[3] > height for c in cells):
        raise ValueError("invalid DP geometry")
    # Recompute the exact optimized score from the final, cropped geometry.
    # Denominator remains the declared canvas as specified, not the crop.
    actual = area / (width * height) - penalty * len(extents)
    if abs(actual - score) > 1e-8:
        raise ValueError("DP geometry score mismatch")
    return Layout(tuple(cells), extents, actual, area)
