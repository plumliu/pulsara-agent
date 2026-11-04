"""Bounded presentation-frame selection over PyAV's own seek/decode machinery.

Only this adapter's two presentation frames and the current returned raster are
retained. FFmpeg owns codec/reference buffering; the existing MediaWorker owns
physical memory, cancellation deadlines and process cleanup. Sparse requests
seek between distant dependencies; continuous windows never become per-frame
seeks. No full-media index or cross-operation decoder authority is retained.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from fractions import Fraction
import math

from PIL import Image

from .source import (
    CancelCheck,
    FramePosition,
    MediaError,
    MediaInfo,
    MediaSource,
    check_cancelled,
    display_video_frame,
    exact_seconds,
    prepare_video_decode,
)


@dataclass(frozen=True, slots=True)
class FrameTarget:
    t: Fraction
    before: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "t", exact_seconds(self.t))
        if not isinstance(self.before, bool):
            raise MediaError("INVALID_ARGUMENT", "Frame boundary kind must be boolean.")


@dataclass(frozen=True, slots=True)
class VisibleFrame:
    position: FramePosition
    t: Fraction
    end: Fraction | None
    image: Image.Image
    next_position: FramePosition | None


class VideoDecoder:
    def __init__(
        self,
        source: MediaSource,
        info: MediaInfo,
        cancelled: CancelCheck = None,
    ) -> None:
        self.source = source
        self.info = info
        self.cancelled = cancelled
        self._context = None
        self._container = None
        self._stream = None
        self._iterator = None
        self._current = None
        self._following = None
        self._last_native_time = None
        self._natural = False
        self._fresh = False
        self._first = (
            info.video_start + info.timeline_origin
            if info.video_start is not None
            else None
        )
        self._seek_work = 1
        self._preroll = Fraction(0)
        # Work observations are process-local scheduling evidence, not limits
        # on media length, logical work, or the number of future requests.
        self.decoded_frames = 0
        self.seeks = 0
        self.rewinds = 0
        self.opens = 0

    def __enter__(self) -> VideoDecoder:
        if self.info.video_stream_index is None:
            raise MediaError("NO_VIDEO_TRACK", "This source has no usable video track.")
        self._open()
        return self

    def __exit__(self, *exc: object) -> None:
        self._close(*exc)

    def _check(self) -> None:
        check_cancelled(self.cancelled)

    def _discard_iterator(self) -> None:
        iterator, self._iterator = self._iterator, None
        if iterator is not None:
            iterator.close()
        self._current = self._following = None
        self._last_native_time = None

    def _close(self, *exc: object) -> None:
        self._discard_iterator()
        context, self._context = self._context, None
        self._container = self._stream = None
        if context is not None:
            context.__exit__(*(exc or (None, None, None)))

    def _open(self) -> None:
        self._check()
        self.source.assert_unchanged()
        context = self.source.open_container()
        container = context.__enter__()
        self._context, self._container = context, container
        try:
            self._stream = container.streams[self.info.video_stream_index]
            if self._stream.time_base is None or self._stream.time_base <= 0:
                raise MediaError(
                    "MEDIA_TIMESTAMP_UNAVAILABLE", "Video stream has no valid time base."
                )
            prepare_video_decode(self._stream, float(self.info.sample_aspect_ratio))
            self.opens += 1
            self._fresh = True
        except BaseException:
            self._close()
            raise

    def _native(self):
        self._check()
        try:
            frame = next(self._iterator)
        except StopIteration:
            return None
        self.decoded_frames += 1
        position = FramePosition.from_frame(frame)
        if (
            self._last_native_time is not None
            and position.absolute_time < self._last_native_time
        ):
            raise MediaError(
                "MEDIA_TIMESTAMP_UNAVAILABLE",
                "Decoded video presentation timestamps move backwards.",
            )
        self._last_native_time = position.absolute_time
        return position, frame

    def _finish_run(self) -> None:
        """Finalize current equal-PTS run, retaining only its last picture."""
        while self._current is not None:
            following = self._native()
            if following is None:
                self._following = None
                return
            if following[0].absolute_time == self._current[0].absolute_time:
                self._current = following
            else:
                self._following = following
                return

    def _begin(self, at: Fraction | None) -> bool:
        self._check()
        self.source.assert_unchanged()
        self._discard_iterator()
        if at is None:
            # Some open-GOP streams do not decode their leading dependencies
            # after even a start-time seek. Reopening uses the demuxer's natural
            # beginning; exactly one bound container remains active.
            if not self._fresh:
                self._close()
                self._open()
            self._natural = True
        else:
            try:
                self._container.seek(
                    math.floor(at / self._stream.time_base),
                    stream=self._stream,
                    backward=True,
                    any_frame=False,
                )
            except OverflowError as exc:
                raise MediaError(
                    "RESOURCE_BOUNDARY",
                    "Requested video timestamp exceeds the decoder's native timestamp range.",
                ) from exc
            # PyAV.seek flushes the codec contexts itself. Discarding our old
            # iterator above is the only additional state cleanup required.
            self.seeks += 1
            self._natural = False
        self._iterator = iter(self._container.decode(self._stream))
        self._fresh = False
        self._current = self._native()
        if self._current is None:
            if self._natural:
                raise MediaError("MEDIA_DECODE_FAILED", "No displayable video frame.")
            # A successful demux seek can still yield no displayable picture
            # when the indexed entry lacks required decode dependencies.
            return False
        if self._natural:
            self._first = self._current[0].absolute_time
        self._finish_run()
        return True

    def _advance(self) -> bool:
        self._current, self._following = self._following, None
        if self._current is None:
            return False
        self._finish_run()
        return True

    def _end(self) -> Fraction | None:
        if self._following is not None:
            return self._following[0].absolute_time
        position, frame = self._current
        duration = getattr(frame, "duration", None)
        if duration is not None and duration > 0:
            return (position.pts + duration) * position.time_base
        if self.info.video_end is not None:
            end = self.info.video_end + self.info.timeline_origin
            if end > position.absolute_time:
                return end
        return None

    @staticmethod
    def _late(start: Fraction, target: Fraction, before: bool) -> bool:
        return start >= target if before else start > target

    def _contains(self, target: Fraction, before: bool) -> bool:
        start = self._current[0].absolute_time
        end = self._end()
        if before:
            return start < target and end is not None and target <= end
        return start <= target and (target < end if end is not None else target == start)

    def _selected_corrupt(self) -> bool:
        return bool(getattr(self._current[1], "is_corrupt", False))

    def _seek_target(self, target: Fraction, before: bool) -> None:
        if self._first is None:
            # Video probing normally supplies this exact fact. A metadata-only
            # caller needs one natural first observation before distinguishing
            # a delayed track from a seek which dropped open-GOP dependencies.
            self._begin(None)
        started = self.decoded_frames
        first_decoded = None
        backstep = None
        seek_at = target
        while True:
            if self._first is not None and seek_at <= self._first:
                begun = self._begin(None)
            else:
                begun = self._begin(seek_at)
            if not begun:
                tick = Fraction(self._stream.time_base)
                backstep = tick if backstep is None else backstep * 2
                seek_at = target - backstep
                self.rewinds += 1
                continue
            start = self._current[0].absolute_time
            if first_decoded is None:
                first_decoded = start
            while not self._late(start, target, before):
                if self._contains(target, before):
                    break
                if not self._advance():
                    # Restore the terminal interval: callers need its truthful
                    # endpoint rather than an invented held frame after EOF.
                    raise MediaError(
                        "MEDIA_FRAME_UNAVAILABLE",
                        "Video has no confirmed visible frame at this position.",
                    )
                start = self._current[0].absolute_time
            late = self._late(start, target, before)
            genuinely_before_first = (
                self._first is not None
                and (target <= self._first if before else target < self._first)
            )
            if late and genuinely_before_first:
                if before:
                    raise MediaError("NO_VIDEO_IN_RANGE", "Video starts after this interval.")
                if not self._selected_corrupt():
                    break
            elif not late and not self._selected_corrupt():
                break
            if self._natural:
                raise MediaError(
                    "MEDIA_DECODE_FAILED",
                    "Natural video decode cannot provide an uncorrupted target frame.",
                )
            lead = max(Fraction(self._stream.time_base), start - target)
            backstep = lead if backstep is None else max(lead, backstep * 2)
            seek_at = target - backstep
            self.rewinds += 1
        self._seek_work = max(1, self.decoded_frames - started)
        self._preroll = max(Fraction(0), target - first_decoded)

    def _locate(self, target: Fraction, before: bool) -> None:
        if self._current is None:
            self._seek_target(target, before)
            return
        if self._contains(target, before) and not self._selected_corrupt():
            return
        if self._late(self._current[0].absolute_time, target, before):
            self._seek_target(target, before)
            return
        if self._following is None:
            self._seek_target(target, before)
            return
        interval = self._following[0].absolute_time - self._current[0].absolute_time
        gap = target - self._following[0].absolute_time
        if gap > max(self._preroll, interval):
            self._seek_target(target, before)
            return
        started = self.decoded_frames
        while self._advance():
            if self._contains(target, before) and not self._selected_corrupt():
                return
            if (
                self._late(self._current[0].absolute_time, target, before)
                or self.decoded_frames - started >= self._seek_work
            ):
                break
        self._seek_target(target, before)

    def _visible(self) -> VisibleFrame:
        self._check()
        position, frame = self._current
        if self._selected_corrupt():
            raise MediaError("MEDIA_DECODE_FAILED", "Selected video frame is corrupt.")
        end = self._end()
        image = display_video_frame(frame, self.info)
        self._check()
        return VisibleFrame(
            position,
            position.absolute_time - self.info.timeline_origin,
            end - self.info.timeline_origin if end is not None else None,
            image,
            self._following[0] if self._following is not None else None,
        )

    def visible_many(
        self,
        targets: Iterable[Fraction | FramePosition | FrameTarget],
        *,
        range_end: Fraction | None = None,
    ) -> Iterator[VisibleFrame]:
        """Yield ordered target observations without retaining their rasters.

        Fraction targets are on the original relative timeline. Native source
        coordinates avoid a float round trip when rendering a prepared plan.
        At a shared boundary, strict-before requests precede inclusive requests.
        """
        last = None
        for request in targets:
            if isinstance(request, FramePosition):
                target, before = request.absolute_time, False
            else:
                request = request if isinstance(request, FrameTarget) else FrameTarget(request)
                target = request.t + self.info.timeline_origin
                before = request.before
            order = target, not before
            if last is not None and order < last:
                raise MediaError("INVALID_ARGUMENT", "Frame targets must be presentation ordered.")
            last = order
            self._locate(target, before)
            if isinstance(request, FramePosition) and self._current[0] != request:
                raise MediaError(
                    "MEDIA_FRAME_UNAVAILABLE",
                    "Native video position is not a presentation-frame boundary.",
                )
            if range_end is not None and (
                self._current[0].absolute_time - self.info.timeline_origin
                >= exact_seconds(range_end)
            ):
                raise MediaError("NO_VIDEO_IN_RANGE", "Video starts after this interval.")
            yield self._visible()
        self.source.assert_unchanged()

    def scan(
        self,
        start: Fraction | FramePosition,
        end: Fraction,
        capacity: int,
        *,
        first_interval: Callable[[tuple[float, float]], None] | None = None,
    ) -> Iterator[VisibleFrame]:
        """Yield one bounded continuous window; caller retains only thumbnails."""
        position = start if isinstance(start, FramePosition) else None
        start = (
            position.absolute_time - self.info.timeline_origin
            if position is not None
            else exact_seconds(start)
        )
        end = exact_seconds(end)
        if end <= start or isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise MediaError("INVALID_ARGUMENT", "Invalid continuous frame allocation.")
        self._locate(start + self.info.timeline_origin, False)
        if position is not None and self._current[0] != position:
            raise MediaError(
                "MEDIA_FRAME_UNAVAILABLE",
                "Native video window does not start at a presentation-frame boundary.",
            )
        for index in range(capacity):
            if self._current[0].absolute_time - self.info.timeline_origin >= end:
                break
            interval_end = self._end()
            if interval_end is None:
                raise MediaError(
                    "MEDIA_TIMESTAMP_UNAVAILABLE", "Final video frame has no known display endpoint."
                )
            relative_end = interval_end - self.info.timeline_origin
            if first_interval is not None and index == 0:
                first_interval((float(start), float(min(end, relative_end))))
            yield self._visible()
            if relative_end >= end or not self._advance():
                break
        self.source.assert_unchanged()
