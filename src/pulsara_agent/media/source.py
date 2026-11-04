"""Descriptor-bound media source and the original presentation timeline.

PyAV/FFmpeg owns demuxing, stream discovery, codec availability and display
metadata. Pulsara binds the authorized file version and chooses product tracks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
import io
import math
import os
from pathlib import Path
import stat
from typing import Any

import av
from PIL import Image

from pulsara_agent.local_source_binding import open_absolute_directory_nofollow

CancelCheck = Callable[[], bool] | None


class MediaError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def check_cancelled(cancelled: CancelCheck) -> None:
    if cancelled is not None and cancelled():
        raise MediaError("CANCELLED", "Media processing was cancelled.")


def exact_seconds(value: int | float | Fraction) -> Fraction:
    """Read public decimal seconds once; keep native coordinates rational.

    Native PTS never passes through this conversion. Public JSON numbers use
    their decimal spelling, rather than their incidental binary float error.
    """
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MediaError("INVALID_RANGE", "Media time must be a finite number.")
    if isinstance(value, float) and not math.isfinite(value):
        raise MediaError("INVALID_RANGE", "Media time must be a finite number.")
    return Fraction(str(value))


@dataclass(frozen=True, slots=True)
class FramePosition:
    """Native source coordinate, not an allocated media identity."""

    pts: int
    time_base: Fraction

    def __post_init__(self) -> None:
        if (
            isinstance(self.pts, bool)
            or not isinstance(self.pts, int)
            or not isinstance(self.time_base, Fraction)
            or self.time_base <= 0
        ):
            raise MediaError(
                "MEDIA_TIMESTAMP_UNAVAILABLE", "Invalid native frame coordinate."
            )

    @property
    def absolute_time(self) -> Fraction:
        return self.pts * self.time_base

    @classmethod
    def from_frame(cls, frame: Any) -> FramePosition:
        if frame.pts is None or frame.time_base is None:
            raise MediaError(
                "MEDIA_TIMESTAMP_UNAVAILABLE",
                "Decoded frame has no presentation timestamp or time base.",
            )
        return cls(frame.pts, Fraction(frame.time_base))


@dataclass(frozen=True, slots=True)
class SourceVersion:
    """Existing stat comparison facts for strict source continuation only."""

    dev: int
    ino: int
    size: int
    modified_ns: int
    changed_ns: int


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    # Transient descriptor integrity, never a durable source/cache identity.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _deny_external_io(url: str, flags: int, options: dict) -> None:
    raise MediaError(
        "MEDIA_FORMAT_UNSUPPORTED",
        "Media must be self-contained; external resources are not permitted.",
    )


class _DescriptorReader(io.RawIOBase):
    """Independent seek position over one bound descriptor, including concurrency."""

    def __init__(self, fd: int) -> None:
        super().__init__()
        self.fd = os.dup(fd)
        self.position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        position = offset
        if whence == os.SEEK_CUR:
            position += self.position
        elif whence == os.SEEK_END:
            position += os.fstat(self.fd).st_size
        elif whence != os.SEEK_SET:
            raise ValueError("Invalid seek origin")
        if position < 0:
            raise ValueError("Negative media seek")
        self.position = position
        return position

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = max(0, os.fstat(self.fd).st_size - self.position)
        data = os.pread(self.fd, size, self.position)
        self.position += len(data)
        return data

    def readinto(self, buffer: Any) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def close(self) -> None:
        if not self.closed:
            os.close(self.fd)
        super().close()


@dataclass(slots=True)
class MediaSource:
    path: Path
    fd: int
    _info: os.stat_result
    _closed: bool = False

    @classmethod
    def open(cls, path: Path) -> MediaSource:
        # Match file-reading resolution; thereafter each component is no-follow.
        canonical = path.absolute()
        parent = open_absolute_directory_nofollow(canonical.parent)
        try:
            fd = os.open(
                canonical.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=parent,
            )
        finally:
            os.close(parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise MediaError(
                    "MEDIA_FORMAT_UNSUPPORTED",
                    "Media source must be a regular local file.",
                )
            source = cls(canonical, fd, info)
            source.assert_unchanged()
            return source
        except BaseException:
            os.close(fd)
            raise

    @property
    def modified_ns(self) -> int:
        return self._info.st_mtime_ns

    @property
    def size(self) -> int:
        return self._info.st_size

    @property
    def version(self) -> SourceVersion:
        self.assert_unchanged()
        return SourceVersion(*_identity(self._info))

    def assert_unchanged(self) -> None:
        if self._closed:
            raise MediaError("MEDIA_SOURCE_CHANGED", "Media source is closed.")
        try:
            parent = open_absolute_directory_nofollow(self.path.parent)
            try:
                current = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            finally:
                os.close(parent)
            same = (
                _identity(current)
                == _identity(self._info)
                == _identity(os.fstat(self.fd))
            )
        except OSError as exc:
            raise MediaError(
                "MEDIA_SOURCE_CHANGED", "Media source was removed or replaced."
            ) from exc
        if not same:
            raise MediaError(
                "MEDIA_SOURCE_CHANGED", "Media source was modified or replaced."
            )

    @contextmanager
    def open_container(self) -> Iterator[Any]:
        self.assert_unchanged()
        try:
            with _DescriptorReader(self.fd) as stream:
                try:
                    container = av.open(
                        stream,
                        mode="r",
                        io_open=_deny_external_io,
                        options={"protocol_whitelist": ""},
                    )
                except av.error.InvalidDataError as exc:
                    raise MediaError(
                        "MEDIA_FORMAT_UNSUPPORTED",
                        f"Media format could not be opened: {exc}",
                    ) from exc
                with container:
                    yield container
        except av.error.FFmpegError as exc:
            raise MediaError(
                "RESOURCE_BOUNDARY"
                if isinstance(exc, av.error.MemoryError)
                else "MEDIA_DECODE_FAILED",
                f"Media decode failed: {exc}",
            ) from exc
        finally:
            self.assert_unchanged()

    def clone(self) -> MediaSource:
        self.assert_unchanged()
        return MediaSource(self.path, os.dup(self.fd), self._info)

    def close(self) -> None:
        if not self._closed:
            os.close(self.fd)
            self._closed = True

    def __enter__(self) -> MediaSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class MediaRange:
    start: float
    end: float


def validate_range(
    start: object, end: object, duration: float | None, *, annotation: bool = False
) -> MediaRange:
    def number(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )

    if annotation and duration is None:
        raise MediaError(
            "MEDIA_DURATION_UNKNOWN", "Cannot annotate media with unknown duration."
        )
    if start is None:
        start = 0
    if end is None:
        if duration is None:
            raise MediaError(
                "INVALID_RANGE", "Media duration is unknown; supply a finite to value."
            )
        end = duration
    if not number(start) or not number(end) or not 0 <= start < end:
        raise MediaError(
            "INVALID_RANGE", "Range requires finite numbers satisfying 0 <= from < to."
        )
    if duration is not None and end > duration:
        raise MediaError(
            "INVALID_RANGE", f"Range ends beyond media duration {duration}."
        )
    return MediaRange(float(start), float(end))


@dataclass(frozen=True, slots=True)
class MediaInfo:
    duration: float | None
    timeline_origin: Fraction
    video_stream_index: int | None
    audio_stream_index: int | None
    subtitle_stream_indices: tuple[int, ...]
    width: int | None
    height: int | None
    rotation: int = 0
    sample_aspect_ratio: Fraction = Fraction(1)
    video_end: Fraction | None = None
    video_start: Fraction | None = None

    def __post_init__(self) -> None:
        # Direct typed callers may supply decimal seconds; native probing below
        # supplies Fractions directly and never round-trips through a float.
        object.__setattr__(self, "timeline_origin", exact_seconds(self.timeline_origin))
        for name in ("video_end", "video_start"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, exact_seconds(value))

    def frame_time(self, frame: Any) -> float:
        return float(FramePosition.from_frame(frame).absolute_time - self.timeline_origin)


_TEXT_SUBTITLE_CODECS = frozenset(
    {
        "ass",
        "ssa",
        "subrip",
        "srt",
        "mov_text",
        "webvtt",
        "text",
        "microdvd",
        "mpl2",
        "jacosub",
        "sami",
        "realtext",
        "subviewer",
        "subviewer1",
        "vplayer",
        "pjs",
    }
)


def _decodable(stream: Any) -> bool:
    try:
        return bool(stream.codec_context.codec.is_decoder)
    except (AttributeError, av.error.FFmpegError, ValueError):
        return False


def prepare_video_decode(stream: Any, sample_aspect_ratio: float = 1.0) -> None:
    from pulsara_agent.media.resources import admit_video_geometry

    context = stream.codec_context
    admit_video_geometry(context.width, context.height, sample_aspect_ratio)
    # Supported FFmpeg decoder configuration avoids per-frame thread copies.
    context.thread_count = 1


def probe_media(
    source: MediaSource, cancelled: CancelCheck = None, *, display_facts: bool = True
) -> MediaInfo:
    check_cancelled(cancelled)
    with source.open_container() as container:
        formats = set(container.format.name.split(","))
        if formats & {"image2", "image2pipe"} or any(
            name.endswith("_pipe") for name in formats
        ):
            raise MediaError(
                "MEDIA_FORMAT_UNSUPPORTED", "Still images are not video or audio media."
            )
        streams = tuple(container.streams)
        real_video = [
            s
            for s in streams
            if s.type == "video"
            and not s.disposition & av.stream.Disposition.attached_pic
        ]
        audio = [s for s in streams if s.type == "audio"]

        def select(items: list[Any], decode: bool = False) -> tuple[Any, Any]:
            ordered = sorted(
                items,
                key=lambda s: (
                    not bool(s.disposition & av.stream.Disposition.default),
                    s.index,
                ),
            )
            for candidate in ordered:
                check_cancelled(cancelled)
                if not _decodable(candidate):
                    continue
                if not decode:
                    return candidate, None
                try:
                    with source.open_container() as check:
                        chosen = check.streams[candidate.index]
                        prepare_video_decode(
                            chosen, float(chosen.sample_aspect_ratio or 1)
                        )
                        first = next(check.decode(chosen), None)
                    if first is not None:
                        return candidate, first
                except MediaError as exc:
                    if exc.code != "MEDIA_DECODE_FAILED":
                        raise
            return (ordered[0], None) if ordered else (None, None)

        video, first_video = select(real_video, decode=display_facts)
        sound, first_audio = select(audio)
        subtitle = tuple(
            s.index
            for s in streams
            if s.type == "subtitle" and s.codec_context.name in _TEXT_SUBTITLE_CODECS
        )
        known_starts = [
            s.start_time * Fraction(s.time_base)
            for s in streams
            if s.type in {"audio", "video"}
            and s.start_time is not None
            and s.time_base is not None
        ]
        if container.start_time is not None:
            origin = Fraction(container.start_time, av.time_base)
        elif known_starts:
            origin = min(known_starts)
        else:
            # Missing metadata needs packet PTS only, never an audio decode or
            # sound observation merely for subtitle/cache/annotation access.
            wanted = {s.index for s in (video, sound) if s is not None}
            first_times = {}
            if wanted:
                with source.open_container() as check:
                    for packet in check.demux(*(check.streams[i] for i in wanted)):
                        check_cancelled(cancelled)
                        if (
                            packet.stream.index not in first_times
                            and packet.pts is not None
                            and packet.time_base is not None
                        ):
                            first_times[packet.stream.index] = (
                                packet.pts * Fraction(packet.time_base)
                            )
                            if len(first_times) == len(wanted):
                                break
            origin = min(first_times.values(), default=Fraction(0))

        def stream_end(stream):
            if stream is None or stream.duration is None or stream.time_base is None:
                return None
            return (
                ((stream.start_time or 0) + stream.duration) * Fraction(stream.time_base)
                - origin
            )

        ends = [
            value
            for value in (stream_end(video), stream_end(sound))
            if value is not None and value > 0
        ]
        duration = (
            max(ends)
            if ends
            else Fraction(container.duration, av.time_base)
            if container.duration is not None
            else None
        )
        if not ends and duration is not None and "matroska" in formats:
            # FFmpeg's Matroska demuxer exposes Segment Duration as the maximum
            # presentation endpoint; MOV/TS expose elapsed duration instead.
            duration -= origin
        if duration is not None and duration <= 0:
            duration = None
        sar = (
            Fraction(video.sample_aspect_ratio or 1)
            if video is not None
            else Fraction(1)
        )
        rotation = first_video.rotation if first_video is not None else 0
        width = height = None
        if video is not None:
            width, height = (
                round(video.codec_context.width * sar),
                video.codec_context.height,
            )
            if abs(rotation) % 180 == 90:
                width, height = height, width
        check_cancelled(cancelled)
        return MediaInfo(
            float(duration) if duration is not None else None,
            origin,
            video.index if video is not None else None,
            sound.index if sound is not None else None,
            subtitle,
            width,
            height,
            rotation,
            sar,
            stream_end(video),
            (
                FramePosition.from_frame(first_video).absolute_time - origin
                if first_video is not None
                else None
            ),
        )


def display_video_frame(frame: Any, info: MediaInfo) -> Image.Image:
    from pulsara_agent.media.resources import admit_video_geometry

    admit_video_geometry(frame.width, frame.height, float(info.sample_aspect_ratio))
    image = frame.to_image().convert("RGB")
    sar = (
        Fraction(frame.sample_aspect_ratio or info.sample_aspect_ratio)
        if hasattr(frame, "sample_aspect_ratio")
        else info.sample_aspect_ratio
    )
    if sar != 1:
        image = image.resize(
            (max(1, round(image.width * sar)), image.height), Image.Resampling.LANCZOS
        )
    rotation = frame.rotation or info.rotation
    if rotation:
        image = image.rotate(rotation, expand=True)
    return image
