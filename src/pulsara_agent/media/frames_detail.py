"""Exact, lazy detail targets on the original media timeline.

The half-second stride is an explicit temporal quality choice for readable
spatial sampling, not a source-duration or provider-budget limit. Source owners
prove presentation intervals; only those intervals may consume several targets.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .frames_decode import FrameTarget
from .source import MediaError


DETAIL_TARGET_STEP = Fraction(1, 2)


def _ceil(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


@dataclass(frozen=True, slots=True)
class DetailGrid:
    start: Fraction
    end: Fraction

    @property
    def short(self) -> bool:
        return self.end - self.start <= 2 * DETAIL_TARGET_STEP

    @property
    def count(self) -> int:
        return 3 if self.short else _ceil((self.end - self.start) / DETAIL_TARGET_STEP) + 1

    def target(self, ordinal: int) -> FrameTarget:
        if type(ordinal) is not int or not 0 <= ordinal < self.count:
            raise MediaError("INVALID_ARGUMENTS", "Detail target is outside its global time grid")
        if ordinal == self.count - 1:
            return FrameTarget(self.end, before=True)
        if self.short:
            return FrameTarget(self.start if ordinal == 0 else (self.start + self.end) / 2)
        return FrameTarget(self.start + ordinal * DETAIL_TARGET_STEP)

    def after_interval(self, ordinal: int, end: Fraction) -> int:
        """First unconsumed target after a verified source presentation interval.

        Normal targets equal to the interval end remain unconsumed. The final
        target is strict-before, so an interval reaching the request end also
        consumes that terminal observation of the same source frame.
        """
        self.target(ordinal)
        if end >= self.end:
            return self.count
        if self.short:
            following = ordinal + 1
            while following < self.count - 1 and self.target(following).t < end:
                following += 1
            return following
        return min(self.count - 1, max(ordinal + 1, _ceil((end - self.start) / DETAIL_TARGET_STEP)))
