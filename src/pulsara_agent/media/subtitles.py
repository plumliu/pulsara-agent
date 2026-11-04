"""Embedded text subtitles via FFmpeg decode and maintained ASS plaintext API."""

from __future__ import annotations

from dataclasses import dataclass

import av
from pysubs2 import SSAEvent

from pulsara_agent.media.source import (
    CancelCheck,
    MediaError,
    MediaInfo,
    MediaSource,
    check_cancelled,
)


@dataclass(frozen=True, slots=True)
class SubtitleEntry:
    start: float
    end: float
    text: str
    position: int
    text_offset: int = 0


@dataclass(frozen=True, slots=True)
class SubtitleReadResult:
    usable: bool
    stream_index: int | None
    language: str | None
    entries: tuple[SubtitleEntry, ...]
    diagnostics: tuple[str, ...] = ()
    next_position: int | None = None
    next_text_offset: int = 0


def read_subtitles(
    source: MediaSource,
    info: MediaInfo,
    start: float,
    end: float,
    preferred_language: str | None = None,
    cancelled: CancelCheck = None,
    after_position: int | None = None,
    maximum_text_bytes: int | None = None,
    text_offset: int = 0,
) -> SubtitleReadResult:
    diagnostics = []
    with source.open_container() as container:
        streams = [container.streams[index] for index in info.subtitle_stream_indices]
        streams.sort(
            key=lambda s: (
                not bool(
                    preferred_language
                    and s.metadata.get("language") == preferred_language
                ),
                not bool(s.disposition & av.stream.Disposition.default),
                s.index,
            )
        )
        for selected in streams:
            check_cancelled(cancelled)
            try:
                return _read_track(
                    source,
                    info,
                    selected.index,
                    start,
                    end,
                    cancelled,
                    tuple(diagnostics),
                    after_position,
                    maximum_text_bytes,
                    text_offset,
                )
            except MediaError as exc:
                if exc.code in {
                    "CANCELLED",
                    "MEDIA_SOURCE_CHANGED",
                    "RESOURCE_BOUNDARY",
                }:
                    raise
                diagnostics.append(f"Subtitle track {selected.index}: {exc.message}")
    return SubtitleReadResult(False, None, None, (), tuple(diagnostics))


def _read_track(
    source: MediaSource,
    info: MediaInfo,
    index: int,
    start: float,
    end: float,
    cancelled: CancelCheck,
    diagnostics: tuple[str, ...],
    after_position: int | None,
    maximum_text_bytes: int | None,
    text_offset: int,
) -> SubtitleReadResult:
    entries = []
    used = 0
    with source.open_container() as container:
        stream = container.streams[index]
        position = 0

        def result(next_position=None, next_text_offset=0):
            return SubtitleReadResult(
                True,
                index,
                stream.metadata.get("language"),
                tuple(entries),
                diagnostics,
                next_position,
                next_text_offset,
            )

        # Starting at the original source preserves boundary-spanning cues and
        # stable positions without building a whole-media subtitle index.
        for packet in container.demux(stream):
            check_cancelled(cancelled)
            if packet.pts is not None and packet.time_base is not None:
                packet_start = (
                    float(packet.pts * packet.time_base) - info.timeline_origin
                )
                if packet_start >= end:
                    break
            subset = stream.codec_context.decode2(packet)
            if subset is None:
                continue
            cue_start = (
                float(subset.pts / av.time_base)
                + subset.start_display_time / 1000
                - info.timeline_origin
            )
            cue_end = (
                float(subset.pts / av.time_base)
                + subset.end_display_time / 1000
                - info.timeline_origin
            )
            if cue_end <= cue_start and packet.duration and packet.time_base:
                cue_end = cue_start + float(packet.duration * packet.time_base)
            if cue_end <= cue_start:
                raise MediaError(
                    "MEDIA_DECODE_FAILED", "Subtitle has no valid display duration."
                )
            for rect in subset:
                current = position
                position += 1
                if after_position is not None and current < after_position:
                    continue
                if not cue_start < end or not cue_end > start:
                    continue
                if rect.type not in {b"ass", b"text"}:
                    raise MediaError(
                        "MEDIA_DECODE_FAILED", "Bitmap subtitle cannot be read as text."
                    )
                # FFmpeg decodes the protocol. PyAV removes the ASS event header;
                # pysubs2 removes style overrides and standard escaped whitespace.
                try:
                    text = SSAEvent(
                        text=(rect.dialogue or rect.text).decode("utf-8")
                    ).plaintext
                except UnicodeError as exc:
                    raise MediaError(
                        "MEDIA_DECODE_FAILED", "Decoded subtitle is not UTF-8."
                    ) from exc
                offset = text_offset if current == after_position else 0
                text = text[offset:]
                if not text:
                    continue
                remaining = (
                    maximum_text_bytes - used
                    if maximum_text_bytes is not None
                    else None
                )
                data = text.encode("utf-8")
                if remaining is not None and len(data) > remaining:
                    if entries:
                        return result(current, offset)
                    fragment = data[: max(0, remaining)].decode(
                        "utf-8", errors="ignore"
                    )
                    if not fragment:
                        raise MediaError(
                            "RESOURCE_BOUNDARY",
                            "Subtitle fragment cannot fit the admitted text byte budget.",
                        )
                    entries.append(
                        SubtitleEntry(cue_start, cue_end, fragment, current, offset)
                    )
                    return result(current, offset + len(fragment))
                entries.append(SubtitleEntry(cue_start, cue_end, text, current, offset))
                used += len(data)
        source.assert_unchanged()
        return result()
