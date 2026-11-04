"""Range-only PCM conversion and all-channel sound observation via PyAV."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
import io
import math
import wave

import av

from pulsara_agent.media.source import (
    CancelCheck,
    MediaError,
    MediaInfo,
    MediaSource,
    check_cancelled,
)

# -60 dBFS peak detects brief quiet signals without whole-range averaging.
# This is an observation threshold, never permission to omit paid recognition.
SILENCE_PEAK_THRESHOLD = 0.001
AUDIO_SAMPLE_RATE = 16000


@dataclass(frozen=True, slots=True)
class SoundObservation:
    start: float
    end: float
    state: str  # near_silent | sound_present | unknown
    threshold_dbfs: float = -60.0


@dataclass(frozen=True, slots=True)
class ExtractedAudio:
    data: bytes
    start: float
    end: float
    sound: SoundObservation | None
    filename: str = "audio.wav"
    mime_type: str = "audio/wav"


def extract_audio(
    source: MediaSource,
    info: MediaInfo,
    start: float,
    end: float,
    max_bytes: int,
    cancelled: CancelCheck = None,
) -> ExtractedAudio:
    if info.audio_stream_index is None:
        raise MediaError("NO_AUDIO_TRACK", "Media has no audio track.")
    samples = math.ceil((end - start) * AUDIO_SAMPLE_RATE)
    if samples * 2 + 44 > max_bytes:
        raise MediaError(
            "RESOURCE_BOUNDARY",
            "Requested PCM audio exceeds the admitted upload byte budget.",
        )
    output = bytearray(samples * 2)
    sound_present = False
    observed = 0
    decoded = False
    sound_known = True
    with source.open_container() as container:
        stream = container.streams[info.audio_stream_index]
        if stream.time_base is None:
            raise MediaError(
                "MEDIA_TIMESTAMP_UNAVAILABLE", "Audio stream has no time base."
            )
        container.seek(
            math.floor((start + info.timeline_origin) / stream.time_base),
            stream=stream,
            backward=True,
        )
        pcm = av.AudioResampler(format="s16", layout="mono", rate=AUDIO_SAMPLE_RATE)
        levels = av.AudioResampler(
            format="fltp",
            layout=stream.codec_context.layout,
            rate=stream.codec_context.sample_rate,
        )

        def consume(frame):
            nonlocal observed
            t = info.frame_time(frame)
            first = max(0, math.ceil((start - t) * AUDIO_SAMPLE_RATE - 1e-8))
            last = min(frame.samples, math.ceil((end - t) * AUDIO_SAMPLE_RATE - 1e-8))
            if last <= first:
                return
            offset = max(0, round((t - start) * AUDIO_SAMPLE_RATE) + first)
            count = min(last - first, samples - offset)
            if count > 0:
                data = bytes(frame.planes[0])[first * 2 : (first + count) * 2]
                output[offset * 2 : (offset + count) * 2] = data
                observed += count

        for frame in container.decode(stream):
            check_cancelled(cancelled)
            t = info.frame_time(frame)
            finish = t + frame.samples / frame.sample_rate
            if t >= end:
                break
            if finish <= start:
                continue
            decoded = True
            # Inspect every native channel before the ASR mono downmix, so
            # opposite phases cannot turn real sound into a silence claim.
            try:
                for level in levels.resample(frame):
                    lt = info.frame_time(level)
                    lo = max(0, math.ceil((start - lt) * level.sample_rate - 1e-8))
                    hi = min(
                        level.samples, math.ceil((end - lt) * level.sample_rate - 1e-8)
                    )
                    if hi > lo:
                        for plane in level.planes:
                            values = array("f")
                            values.frombytes(bytes(plane)[: level.samples * 4])
                            if any(
                                abs(value) >= SILENCE_PEAK_THRESHOLD
                                for value in values[lo:hi]
                            ):
                                sound_present = True
            except (ValueError, av.error.FFmpegError):
                sound_known = False
            for converted in pcm.resample(frame):
                consume(converted)
        for converted in pcm.resample(None):
            consume(converted)
    if not decoded or observed == 0:
        raise MediaError(
            "MEDIA_DECODE_FAILED",
            "Audio track produced no decodable samples in the requested range.",
        )
    check_cancelled(cancelled)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(AUDIO_SAMPLE_RATE)
        wav.writeframes(output)
    state = (
        "sound_present"
        if sound_present
        else "near_silent"
        if sound_known and observed >= samples - 1
        else "unknown"
    )
    return ExtractedAudio(
        buffer.getvalue(), start, end, SoundObservation(start, end, state)
    )
