"""Complete subtitle/ASR range acquisition with existing cache and work owners.

PyAV owns decoding; the ASR adapter owns real uploads and response validation.
This owner acquires the full requested range before model delivery is paged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .asr import prepare_and_transcribe, transcribe_audio
from .asr_cache import AsrCacheBlock, AsrMediaKey
from .source import MediaError, MediaInfo, MediaSource, validate_range
from .subtitles import read_subtitles
from .transcript_work import PreparedTranscriptWork, TranscriptWork
from .transcript_recognition import AcceptedAsrGap
from .transcript_ranges import missing_ranges


@dataclass(frozen=True, slots=True)
class TranscriptItem:
    key: str
    index: int
    start: float
    end: float
    text: str
    source: str
    speaker: str | None = None

    def public_value(self, request: tuple[float, float]) -> dict[str, object]:
        value = {"from": self.start, "to": self.end, "text": self.text}
        if self.speaker is not None:
            value["speaker"] = self.speaker
        if self.start < request[0] or self.end > request[1]:
            value["visible_range"] = {
                "from": max(self.start, request[0]),
                "to": min(self.end, request[1]),
            }
        return value


@dataclass(frozen=True, slots=True)
class TranscriptReadResult:
    source: str | None
    language: str | None
    entries: tuple[TranscriptItem, ...]
    requested_range: tuple[float, float]
    processed_to: float
    diagnostics: tuple[str, ...] = ()
    errors: tuple[dict[str, object], ...] = ()
    sound_observations: tuple[dict[str, object], ...] = ()


def completed_prefix(blocks, start: float, end: float) -> float:
    at = start
    for block in sorted(
        blocks, key=lambda block: (block.from_seconds, block.to_seconds, block.id)
    ):
        if block.to_seconds <= at:
            continue
        if block.from_seconds > at:
            break
        at = min(end, max(at, block.to_seconds))
    return at


def plan_gaps(
    gaps: tuple[tuple[float, float], ...], *, maximum_audio_bytes: int
) -> tuple[tuple[float, float], ...]:
    # 16kHz, 16-bit mono WAV: each batch must fit the real upload boundary.
    capacity = (maximum_audio_bytes - 44) / 32000
    if capacity <= 0:
        raise MediaError(
            "MEDIA_RESOURCE_BOUNDARY",
            "Audio admission cannot hold a WAV header and sample",
        )
    result = []
    for start, end in gaps:
        while start < end:
            stop = min(end, start + capacity)
            if stop <= start:
                raise MediaError(
                    "MEDIA_RESOURCE_BOUNDARY",
                    "Audio admission cannot make temporal progress",
                )
            result.append((start, stop))
            start = stop
    return tuple(result)


# Display-only pause threshold. Keep the provider’s individual word times in cache.
_WORD_GROUP_PAUSE_SECONDS = 0.5


def _join_words(previous: str, following: str) -> str:
    if (
        previous
        and following
        and previous[-1].isascii()
        and following[0].isascii()
        and previous[-1].isalnum()
        and following[0].isalnum()
    ):
        return previous + " " + following
    return previous + following


def _cache_items(
    blocks: tuple[AsrCacheBlock, ...], start: float, end: float, *, initial: bool
) -> tuple[TranscriptItem, ...]:
    result = []
    observed: dict[
        tuple[str, str | None], list[tuple[float, float, str, float, float]]
    ] = {}
    for block in sorted(
        blocks, key=lambda block: (block.from_seconds, block.to_seconds, block.id)
    ):
        rows = []
        request = block.payload["request_range"]
        for index, raw in enumerate(block.payload["items"]):
            a, b = raw["start"], raw["end"]
            if (
                b <= start
                or a >= end
                or b <= block.from_seconds
                or a >= block.to_seconds
                or (not initial and a < start)
            ):
                continue
            identity = (raw["text"], raw.get("speaker"))
            prior = observed.setdefault(identity, [])
            multiplicity: dict[str, int] = {}
            for old_from, old_to, owner, audio_from, audio_to in prior:
                if (
                    old_from < b
                    and old_to > a
                    and audio_from < request["to"]
                    and audio_to > request["from"]
                ):
                    multiplicity[owner] = multiplicity.get(owner, 0) + 1
            occurrence = multiplicity.pop(block.id, 0)
            duplicate = any(count > occurrence for count in multiplicity.values())
            # Count every raw source occurrence, including a context duplicate.
            # Coarse equal timestamps must preserve repeated words within an
            # original response and the greatest observed repeat multiplicity.
            prior.append((a, b, block.id, request["from"], request["to"]))
            if duplicate:
                continue
            rows.append((index, raw))
        if block.payload["granularity"] != "word":
            result.extend(
                TranscriptItem(
                    block.id,
                    index,
                    raw["start"],
                    raw["end"],
                    raw["text"],
                    "asr",
                    raw.get("speaker"),
                )
                for index, raw in rows
            )
            continue
        group = None
        for index, raw in rows:
            if group is not None and (
                group.speaker != raw.get("speaker")
                or raw["start"] >= group.end + _WORD_GROUP_PAUSE_SECONDS
                or group.text.endswith(("\n", "\r"))
                or group.text.rstrip().endswith(
                    (".", "!", "?", "。", "！", "？", ";", "；", "\n")
                )
            ):
                result.append(group)
                group = None
            if group is None:
                group = TranscriptItem(
                    block.id,
                    index,
                    raw["start"],
                    raw["end"],
                    raw["text"],
                    "asr",
                    raw.get("speaker"),
                )
            else:
                group = TranscriptItem(
                    group.key,
                    group.index,
                    group.start,
                    max(group.end, raw["end"]),
                    _join_words(group.text, raw["text"]),
                    "asr",
                    group.speaker,
                )
        if group is not None:
            result.append(group)
    return tuple(
        sorted(result, key=lambda item: (item.start, item.end, item.key, item.index))
    )


def _request_bound(row: object, side: str) -> float:
    value = getattr(row, f"request_{side}_seconds", None)
    if value is not None:
        return value
    payload = getattr(row, "payload", None)
    if payload is not None:
        return payload["request_range"][side]
    return getattr(row, "from_seconds" if side == "from" else "to_seconds")


class TranscriptService:
    def __init__(
        self,
        *,
        work: TranscriptWork | None,
        cache: object,
        asr_config: object,
        run_media: Callable[..., Awaitable[Any]],
        maximum_upload_bytes: int,
        maximum_response_bytes: int,
        deadline_monotonic: float,
        run_cache: Callable[..., Awaitable[Any]] | None = None,
        run_asr_media: Callable[..., Awaitable[Any]] | None = None,
        run_asr_cache: Callable[..., Awaitable[Any]] | None = None,
        credential_scrub_set: object = None,
        transcribe: Callable[..., Awaitable[Any]] = transcribe_audio,
        load_asr_settings: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self.cache = cache
        if work is not None and not isinstance(work, TranscriptWork):
            raise ValueError("TranscriptService requires its Core-owned work owner")
        self.work = work
        self.asr_config = asr_config
        self.run_media = run_media
        self.run_cache = run_cache or run_media
        self.run_asr_media = run_asr_media or run_media
        self.run_asr_cache = run_asr_cache or self.run_cache
        self.maximum_upload_bytes = maximum_upload_bytes
        self.maximum_response_bytes = maximum_response_bytes
        self.deadline = deadline_monotonic
        self.transcribe = transcribe
        self.prepare_transcribe = (
            prepare_and_transcribe if transcribe is transcribe_audio else None
        )
        self.credential_scrub_set = credential_scrub_set
        self.load_asr_settings = load_asr_settings

    async def _cache_call(
        self, operation: Callable[..., Any], *args: object, **kwargs: object
    ) -> Any:
        return await self.run_cache(
            operation, *args, deadline_monotonic=self.deadline, **kwargs
        )

    def _prepare_gap(
        self,
        source: MediaSource,
        info: MediaInfo,
        key: AsrMediaKey,
        start: float,
        end: float,
    ) -> PreparedTranscriptWork:
        accepted = AcceptedAsrGap(
            source.clone(),
            info,
            key,
            start,
            end,
            self.cache,
            self.asr_config,
            self.run_asr_media,
            self.run_asr_cache,
            self.transcribe,
            self.prepare_transcribe,
            self.maximum_upload_bytes,
            self.maximum_response_bytes,
            self.deadline,
            self.credential_scrub_set,
        )
        # Cleanup also runs when task cancellation precedes its first resume.
        return PreparedTranscriptWork(accepted.run(), accepted.source.close)

    async def read(
        self,
        source: MediaSource,
        info: MediaInfo,
        arguments: Mapping[str, object],
        *,
        memory_domain_id: str,
        on_started: Callable[[float, float], None] = lambda _a, _b: None,
        cancelled: Callable[[], bool] = lambda: False,
        preferred_language: str | None = None,
    ) -> TranscriptReadResult:
        if set(arguments) - {"target", "from", "to"}:
            raise MediaError("INVALID_ARGUMENTS", "Unknown read_transcript arguments")
        span = validate_range(
            arguments.get("from", 0), arguments.get("to"), info.duration
        )
        start, end = span.start, span.end
        if cancelled():
            raise asyncio.CancelledError
        source.assert_unchanged()
        subtitles = await self.run_media(
            read_subtitles,
            source,
            info,
            start,
            end,
            preferred_language=preferred_language,
            cancelled=cancelled,
        )
        key = AsrMediaKey(
            memory_domain_id, str(source.path), source.modified_ns, source.size
        )
        diagnostics = list(subtitles.diagnostics)
        if subtitles.usable:
            if self.cache is not None:
                try:
                    await self._cache_call(self.cache.touch, key)
                except Exception as exc:
                    diagnostics.append(
                        f"ASR cache use refresh failed: {type(exc).__name__}: {exc}"
                    )
            on_started(start, end)
            source.assert_unchanged()
            return TranscriptReadResult(
                "subtitle",
                subtitles.language,
                tuple(
                    TranscriptItem(
                        f"subtitle:{subtitles.stream_index}",
                        item.position,
                        item.start,
                        item.end,
                        item.text,
                        "subtitle",
                    )
                    for item in subtitles.entries
                ),
                (start, end),
                end,
                tuple(diagnostics),
            )
        if info.audio_stream_index is None:
            return TranscriptReadResult(
                None,
                None,
                (),
                (start, end),
                start,
                tuple(diagnostics),
                (
                    {
                        "from": start,
                        "to": end,
                        "error": "NO_AUDIO_TRACK",
                        "message": "此媒体没有音轨，也没有可用文字字幕。",
                    },
                ),
            )
        if self.cache is None:
            raise MediaError(
                "ASR_CACHE_UNAVAILABLE",
                "The transcription cache is unavailable; no audio was submitted",
            )
        ranges = tuple(await self._cache_call(self.cache.query_ranges, key, start, end))
        audio_until = start
        for row in sorted(ranges, key=lambda row: _request_bound(row, "from")):
            if _request_bound(row, "from") < audio_until:
                diagnostics.append(
                    "Overlapping ASR blocks retain their original wording; differing boundary text has not been reconciled."
                )
                break
            audio_until = max(audio_until, _request_bound(row, "to"))
        blocks: dict[str, AsrCacheBlock] = {}
        for row in ranges:
            block = await self._cache_call(self.cache.query_block, key, row.id)
            if block is None:
                raise MediaError(
                    "ASR_CACHE_UNAVAILABLE", "Transcript cache changed during this read"
                )
            blocks[block.id] = block
        await self._cache_call(self.cache.touch, key)
        errors = []
        try:
            while missing_ranges(tuple(blocks.values()), start, end):
                if cancelled():
                    raise asyncio.CancelledError
                if self.work is None:
                    errors.append(
                        {
                            "error": "ASR_RUNTIME_UNAVAILABLE",
                            "message": "The shared ASR execution owner is unavailable; no audio was submitted.",
                        }
                    )
                    break
                if self.asr_config is None and self.load_asr_settings is not None:
                    settings = await self.load_asr_settings()
                    self.load_asr_settings = None
                    self.asr_config = settings.asr
                    self.credential_scrub_set = settings.credential_scrub_set()
                if self.asr_config is None:
                    errors.append(
                        {
                            "error": "ASR_NOT_CONFIGURED",
                            "message": "Configure an ASR service to transcribe missing audio.",
                        }
                    )
                    break
                # This is an internal upload batch, never a model continuation.
                gap_start, gap_end = plan_gaps(
                    missing_ranges(tuple(blocks.values()), start, end),
                    maximum_audio_bytes=self.maximum_upload_bytes,
                )[0]
                on_started(gap_start, gap_end)

                def prepare(a: float, b: float):
                    return self._prepare_gap(source, info, key, a, b)

                outcomes = await self.work.run(key, gap_start, gap_end, prepare)
                obtained = []
                for _a, _b, outcome in outcomes:
                    obtained.extend(outcome.blocks)
                    diagnostics.extend(outcome.diagnostics)
                    errors.extend(outcome.errors)
                for block in obtained:
                    blocks[block.id] = block
                if errors:
                    break
                # A real gap that yielded no successful coverage is a failure,
                # never permission to silently repeat paid or uncertain work.
                if missing_ranges(tuple(blocks.values()), gap_start, gap_end):
                    errors.append(
                        {
                            "from": gap_start,
                            "to": gap_end,
                            "error": "ASR_INCOMPLETE",
                            "message": "The recognition outcome did not establish complete coverage; it was not retried.",
                        }
                    )
                    break
        except (MediaError, OSError, ValueError, TimeoutError) as exc:
            errors.append(
                {"error": getattr(exc, "code", type(exc).__name__), "message": str(exc)}
            )
        source.assert_unchanged()
        values = tuple(
            sorted(
                blocks.values(),
                key=lambda block: (block.from_seconds, block.to_seconds, block.id),
            )
        )
        observations = []
        for block in values:
            sound = block.payload.get("sound")
            if sound and sound.get("state") in {"near_silent", "sound_present"}:
                observations.append(
                    {
                        "from": max(start, block.from_seconds),
                        "to": min(end, block.to_seconds),
                        "state": sound["state"],
                    }
                )
        return TranscriptReadResult(
            "asr",
            None,
            _cache_items(values, start, end, initial=True),
            (start, end),
            completed_prefix(values, start, end),
            tuple(diagnostics),
            tuple(errors),
            tuple(observations),
        )
