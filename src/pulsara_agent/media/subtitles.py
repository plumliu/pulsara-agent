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


@dataclass(frozen=True, slots=True)
class SubtitleReadResult:
    usable: bool
    stream_index: int | None
    language: str | None
    entries: tuple[SubtitleEntry, ...]
    diagnostics: tuple[str, ...] = ()


def read_subtitles(
    source: MediaSource,
    info: MediaInfo,
    start: float,
    end: float,
    preferred_language: str | None = None,
    cancelled: CancelCheck = None,
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
) -> SubtitleReadResult:
    entries = []
    with source.open_container() as container:
        stream = container.streams[index]
        position = 0

        def result():
            return SubtitleReadResult(
                True,
                index,
                stream.metadata.get("language"),
                tuple(entries),
                diagnostics,
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
                if text:
                    entries.append(SubtitleEntry(cue_start, cue_end, text, current))
        source.assert_unchanged()
        return result()
