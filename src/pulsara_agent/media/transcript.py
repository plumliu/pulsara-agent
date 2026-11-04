"""Call-local subtitle/ASR reading, successful-block reuse and source paging.

PyAV owns decoding; the OpenAI SDK adapter owns requests, quota admission and
response validation. This owner only plans effective gaps, maps timestamps and
packs accepted original text. No task or pagination session survives the call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import math
from typing import Any

from pulsara_agent.identity import is_valid_id
from pulsara_agent.primitives.text_paging import TextPosition
from pulsara_agent.primitives.tool_result_budget import (
    ToolResultPageBudget,
    ToolResultPageBoundary,
)

from .asr import prepare_and_transcribe, transcribe_audio
from .asr_cache import AsrCacheBlock, AsrMediaKey, asr_cache_id
from .source import MediaError, MediaInfo, MediaSource, validate_range
from .subtitles import read_subtitles
from .transcript_paging import TranscriptItem, TranscriptPosition, json_body, pack_items
from .transcript_work import PreparedTranscriptWork, TranscriptWork
from .transcript_recognition import AcceptedAsrGap
from .transcript_ranges import missing_ranges

# Product boundary, specification 4.2: cumulative newly attempted effective audio
# in one call; cached and subtitle source time do not consume this allowance.
NEW_ASR_SECONDS_PER_CALL = 180.0


@dataclass(frozen=True, slots=True)
class TranscriptPage:
    payload: dict[str, object]
    is_error: bool = False
    page_complete: bool = True

    @property
    def body(self) -> str:
        return json_body(self.payload)


def completed_prefix(
    blocks: tuple[AsrCacheBlock, ...], start: float, end: float
) -> float:
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
    gaps: tuple[tuple[float, float], ...],
    *,
    maximum_audio_bytes: int,
    maximum_seconds: float = NEW_ASR_SECONDS_PER_CALL,
) -> tuple[tuple[float, float], ...]:
    # The extractor emits 16kHz, 16-bit mono WAV. The WAV header and sample byte
    # bound are a physical admission constraint, not an arbitrary block length.
    capacity = (maximum_audio_bytes - 44) / 32000
    if capacity <= 0:
        raise MediaError(
            "MEDIA_RESOURCE_BOUNDARY",
            "Audio admission cannot hold a WAV header and sample",
        )
    remaining = min(NEW_ASR_SECONDS_PER_CALL, maximum_seconds)
    result = []
    for start, end in gaps:
        while start < end and remaining > 0:
            stop = min(end, start + remaining, start + capacity)
            if stop <= start:
                raise MediaError(
                    "MEDIA_RESOURCE_BOUNDARY",
                    "Audio admission cannot make temporal progress",
                )
            result.append((start, stop))
            remaining -= stop - start
            start = stop
        if remaining <= 0:
            break
    return tuple(result)


def _number(value: object) -> bool:
    return type(value) in (float, int) and math.isfinite(value)


def _cursor(
    value: object, source: MediaSource, start: float, end: float
) -> dict[str, Any]:
    fields = {
        "source_path",
        "request_from",
        "request_to",
        "source_modified_ns",
        "source_size",
        "source",
        "track",
        "window_from",
        "processed_to",
        "position",
        "anchor",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise MediaError(
            "INVALID_ARGUMENTS", "Invalid nonempty transcript continuation"
        )
    if (
        value["source_path"] != str(source.path)
        or value["source_modified_ns"] != str(source.modified_ns)
        or value["source_size"] != source.size
    ):
        raise MediaError(
            "MEDIA_SOURCE_CHANGED", "Media changed since this transcript continuation"
        )
    if (
        not isinstance(value["source_modified_ns"], str)
        or type(value["source_size"]) is not int
    ):
        raise MediaError("INVALID_ARGUMENTS", "Invalid continuation source version")
    if value["request_from"] != start or value["request_to"] != end:
        raise MediaError(
            "INVALID_ARGUMENTS", "Continuation belongs to a different requested range"
        )
    if value["anchor"] is not None and (
        not isinstance(value["anchor"], str) or not value["anchor"]
    ):
        raise MediaError("INVALID_ARGUMENTS", "Invalid continuation cache anchor")
    if (
        value["source"] not in ("subtitle", "asr")
        or type(value["track"]) is not int
        or value["track"] < 0
    ):
        raise MediaError("INVALID_ARGUMENTS", "Invalid continuation source")
    if value["source"] == "asr" and not is_valid_id(value["anchor"], "asr-cache"):
        raise MediaError(
            "INVALID_ARGUMENTS", "ASR continuation requires its successful cache source"
        )
    if value["source"] == "subtitle" and (
        value["anchor"] is not None or value["position"] is None
    ):
        raise MediaError(
            "INVALID_ARGUMENTS", "Invalid subtitle continuation source position"
        )
    left, right = value["window_from"], value["processed_to"]
    if not _number(left) or not _number(right) or not start <= left <= right <= end:
        raise MediaError(
            "INVALID_ARGUMENTS", "Continuation processing range is outside this request"
        )
    position = value["position"]
    if position is not None:
        try:
            parsed = TranscriptPosition.parse(position)
        except ValueError as exc:
            raise MediaError("INVALID_ARGUMENTS", str(exc)) from exc
        if parsed.text.line != 1 or (
            value["source"] == "subtitle" and parsed.key != f"subtitle:{value['track']}"
        ):
            raise MediaError(
                "INVALID_ARGUMENTS", "Invalid original transcript character position"
            )
        if value["source"] == "asr" and value["anchor"] != position["key"]:
            raise MediaError(
                "INVALID_ARGUMENTS",
                "ASR continuation anchor must identify its text source",
            )
    elif left != right:
        raise MediaError(
            "INVALID_ARGUMENTS",
            "A completed text window must advance to its processed boundary",
        )
    return dict(value)


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
        work: TranscriptWork,
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
    ) -> None:
        self.cache = cache
        if not isinstance(work, TranscriptWork):
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
        page_budget: ToolResultPageBudget,
        *,
        memory_domain_id: str,
        on_started: Callable[[float, float], None] = lambda _a, _b: None,
        cancelled: Callable[[], bool] = lambda: False,
        preferred_language: str | None = None,
    ) -> TranscriptPage:
        if set(arguments) - {"target", "from", "to", "continuation"}:
            raise MediaError("INVALID_ARGUMENTS", "Unknown read_transcript arguments")
        span = validate_range(
            arguments.get("from", 0), arguments.get("to"), info.duration
        )
        start, end = span.start, span.end
        target = {"kind": "PATH", "path": str(source.path)}
        cursor = (
            _cursor(arguments["continuation"], source, start, end)
            if "continuation" in arguments
            else None
        )
        window_from = start if cursor is None else cursor["window_from"]
        position = (
            None
            if cursor is None or cursor["position"] is None
            else TranscriptPosition.parse(cursor["position"])
        )
        if cancelled():
            raise asyncio.CancelledError
        source.assert_unchanged()
        subtitle_options = {}
        if (
            cursor is not None
            and cursor["source"] == "subtitle"
            and position is not None
        ):
            subtitle_options = {
                "after_position": position.index,
                "text_offset": position.text.char_offset,
            }
        subtitles = await self.run_media(
            read_subtitles,
            source,
            info,
            start,
            end,
            preferred_language=preferred_language,
            cancelled=cancelled,
            maximum_text_bytes=self.maximum_response_bytes,
            **subtitle_options,
        )
        selected = "subtitle" if subtitles.usable else "asr"
        track = subtitles.stream_index if subtitles.usable else info.audio_stream_index
        if cursor is not None and (cursor["source"], cursor["track"]) != (
            selected,
            track,
        ):
            raise MediaError(
                "TRANSCRIPT_CONTINUATION_INVALID",
                "The selected transcript source changed; begin a new range read",
            )
        key = AsrMediaKey(
            memory_domain_id, str(source.path), source.modified_ns, source.size
        )
        diagnostics = list(subtitles.diagnostics)
        errors: list[dict[str, object]] = []
        blocks: tuple[AsrCacheBlock, ...] = ()
        ranges = ()
        processed_to = end if selected == "subtitle" else window_from
        attempted_asr_seconds = 0.0
        started = False
        displayed_to = window_from
        unpersisted = set()
        anchor = None if cursor is None else cursor["anchor"]
        silent_ranges: list[tuple[float, float]] = []
        sound_present_observation = None

        def observe_sound(block: AsrCacheBlock) -> None:
            nonlocal silent_ranges, sound_present_observation
            sound = block.payload.get("sound")
            if not isinstance(sound, dict):
                return
            a, b, state = sound.get("start"), sound.get("end"), sound.get("state")
            if not _number(a) or not _number(b) or a >= b:
                return
            if state == "near_silent":
                merged = []
                for left, right in sorted((*silent_ranges, (a, b))):
                    if merged and left <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
                    else:
                        merged.append((left, right))
                silent_ranges = merged
            elif (
                state == "sound_present"
                and window_from <= a < b <= end
                and sound_present_observation is None
            ):
                sound_present_observation = {
                    "from": a,
                    "to": b,
                    "state": "sound_present",
                }

        def begin(a: float, b: float) -> None:
            nonlocal started, displayed_to
            if not started or b > displayed_to:
                on_started(a, b)
                started = True
                displayed_to = b

        def continuation(
            next_position: TranscriptPosition | None, boundary: float
        ) -> dict[str, object] | None:
            if next_position is None and boundary >= end:
                return None
            return {
                "tool": "read_transcript",
                "arguments": {
                    "target": target,
                    "from": start,
                    "to": end,
                    "continuation": {
                        "source_path": str(source.path),
                        "request_from": start,
                        "request_to": end,
                        "source_modified_ns": str(source.modified_ns),
                        "source_size": source.size,
                        "source": selected,
                        "track": track,
                        "anchor": next_position.key
                        if selected == "asr" and next_position is not None
                        else anchor,
                        "window_from": window_from
                        if next_position is not None
                        else boundary,
                        "processed_to": boundary,
                        "position": None
                        if next_position is None
                        else next_position.public_value(),
                    },
                },
            }

        def payload(
            entries: list[dict[str, object]], next_position: TranscriptPosition | None
        ) -> dict[str, object]:
            if (
                selected == "subtitle"
                and next_position is None
                and subtitles.next_position is not None
            ):
                next_position = TranscriptPosition(
                    f"subtitle:{track}",
                    subtitles.next_position,
                    TextPosition(1, subtitles.next_text_offset),
                )
            page_errors = list(errors)
            if unpersisted and (next_position is not None or processed_to < end):
                page_errors.append(
                    {
                        "error": "ASR_CACHE_WRITE_FAILED",
                        "message": "The valid original text could not be cached. Further source continuation is unavailable; this page preserves the text delivered here.",
                    }
                )
            complete = next_position is None and processed_to >= end and not page_errors
            value: dict[str, object] = {
                "status": "error"
                if page_errors and processed_to <= window_from
                else "partial"
                if page_errors
                else "ok",
                "target": target,
                "media_version": {
                    "modified_ns": str(source.modified_ns),
                    "size": source.size,
                },
                "requested_range": {"from": start, "to": end},
                "source": selected,
                "processed_range": {"from": window_from, "to": processed_to},
                "entries": entries,
                "complete": complete,
                "next": None
                if page_errors
                else continuation(next_position, processed_to),
            }
            if selected == "subtitle" and subtitles.language is not None:
                value["language"] = subtitles.language
            if diagnostics:
                value["diagnostics"] = diagnostics
            if page_errors:
                value["errors"] = page_errors
                value["remaining_gaps"] = [
                    {"from": a, "to": b}
                    for a, b in missing_ranges(ranges, processed_to, end)
                ]
            if not entries:
                value["message"] = (
                    "No text was obtained; this does not establish silence or absence of speech."
                )
                observations = []
                for a, b in silent_ranges:
                    if a <= window_from < processed_to <= b:
                        observations.append(
                            {
                                "from": window_from,
                                "to": processed_to,
                                "state": "near_silent",
                            }
                        )
                if (
                    sound_present_observation is not None
                    and sound_present_observation["to"] <= processed_to
                ):
                    observations.append(sound_present_observation)
                if observations:
                    value["sound_observations"] = observations
            return value

        if selected == "subtitle":
            if self.cache is not None:
                try:
                    await self._cache_call(self.cache.touch, key)
                except Exception as exc:
                    diagnostics.append(
                        f"ASR cache use refresh failed: {type(exc).__name__}: {exc}"
                    )
            items = tuple(
                TranscriptItem(
                    f"subtitle:{track}",
                    item.position,
                    item.start,
                    item.end,
                    item.text,
                    "subtitle",
                    text_offset=item.text_offset,
                    more_text=subtitles.next_position == item.position,
                )
                for item in subtitles.entries
                if item.end > window_from and item.start < end
            )
            if subtitles.next_position is not None and subtitles.entries:
                processed_to = min(end, max(item.end for item in subtitles.entries))
            begin(window_from, processed_to)
            try:
                entries, next_position = pack_items(
                    items,
                    position=position,
                    request=(start, end),
                    budget=page_budget,
                    payload=payload,
                )
            except ToolResultPageBoundary:
                raise
            except ValueError as exc:
                raise MediaError("TRANSCRIPT_CONTINUATION_INVALID", str(exc)) from exc
            if next_position is None and subtitles.next_position is not None:
                next_position = TranscriptPosition(
                    f"subtitle:{track}",
                    subtitles.next_position,
                    TextPosition(1, subtitles.next_text_offset),
                )
                if not page_budget.fits(json_body(payload(entries, next_position))):
                    raise ToolResultPageBoundary(
                        "Subtitle source continuation does not fit"
                    )
            source.assert_unchanged()
            return TranscriptPage(payload(entries, next_position))
        if info.audio_stream_index is None:
            unavailable = {
                "status": "error",
                "error": "NO_AUDIO_TRACK",
                "target": target,
                "media_version": {
                    "modified_ns": str(source.modified_ns),
                    "size": source.size,
                },
                "message": "此媒体没有音轨，也没有可用文字字幕；无需再次调用本工具",
                "complete": False,
                "next": None,
                **({"diagnostics": diagnostics} if diagnostics else {}),
            }
            if not page_budget.fits(json_body(unavailable)):
                raise ToolResultPageBoundary(
                    "No-audio result does not fit this call's allowance"
                )
            return TranscriptPage(unavailable, is_error=True)

        if self.cache is None:
            raise MediaError(
                "ASR_CACHE_UNAVAILABLE",
                "The transcription cache is unavailable; no audio was submitted",
            )
        ranges = await self._cache_call(self.cache.query_ranges, key, window_from, end)
        anchored = None
        audio_until = window_from
        for row in sorted(ranges, key=lambda row: _request_bound(row, "from")):
            if _request_bound(row, "from") < audio_until:
                diagnostics.append(
                    "Overlapping ASR blocks retain their original wording; differing boundary text has not been reconciled."
                )
                break
            audio_until = max(audio_until, _request_bound(row, "to"))
        if anchor is not None:
            anchored = await self._cache_call(self.cache.query_block, key, anchor)
            if anchored is None:
                raise MediaError(
                    "TRANSCRIPT_CONTINUATION_INVALID",
                    "Required transcript cache was cleared; begin a new range read",
                )
            if (
                position is None
                and not anchored.from_seconds < window_from <= anchored.to_seconds
            ):
                raise MediaError(
                    "INVALID_ARGUMENTS",
                    "Continuation boundary is not covered by its successful cache anchor",
                )
        # Load source text one successful block at a time below. Range planning
        # needs only lightweight coverage metadata, never the full transcript.
        blocks = ()
        frozen_to = (
            None if cursor is None or position is None else cursor["processed_to"]
        )
        if frozen_to is not None:
            processed_to = completed_prefix(ranges, window_from, frozen_to)
            if processed_to != frozen_to:
                raise MediaError(
                    "TRANSCRIPT_CONTINUATION_INVALID",
                    "Required transcript cache was cleared; begin a new range read",
                )
        else:
            processed_to = completed_prefix(ranges, window_from, end)
        if processed_to > window_from:
            anchor = next(
                (
                    row.id
                    for row in ranges
                    if row.from_seconds < processed_to <= row.to_seconds
                ),
                None,
            )
        if processed_to > window_from:
            begin(window_from, processed_to)
        await self._cache_call(self.cache.touch, key)
        loaded = set()
        cursor_time = window_from
        if position is not None:
            if anchored is None or position.index >= len(anchored.payload["items"]):
                raise MediaError(
                    "TRANSCRIPT_CONTINUATION_INVALID",
                    "Required transcript source position was cleared",
                )
            cursor_time = anchored.payload["items"][position.index]["start"]
        while True:
            source_ranges = sorted(
                (
                    row
                    for row in ranges
                    if row.from_seconds < processed_to
                    and row.to_seconds > window_from
                    and _request_bound(row, "to") > cursor_time
                ),
                key=lambda row: (
                    _request_bound(row, "from"),
                    row.from_seconds,
                    row.to_seconds,
                    row.id,
                ),
            )
            for index, row in enumerate(source_ranges):
                if row.id in loaded:
                    continue
                block = await self._cache_call(self.cache.query_block, key, row.id)
                if block is None:
                    raise MediaError(
                        "TRANSCRIPT_CONTINUATION_INVALID",
                        "Transcript cache changed during this read",
                    )
                loaded.add(row.id)
                observe_sound(block)
                # Successful empty coverage is represented by range metadata;
                # do not retain every empty block payload across long silence.
                if block.payload["items"]:
                    blocks = (*blocks, block)
                items = _cache_items(
                    blocks, window_from, processed_to, initial=window_from == start
                )
                try:
                    _, provisional = pack_items(
                        items,
                        position=position,
                        request=(start, end),
                        budget=page_budget,
                        payload=payload,
                    )
                except ToolResultPageBoundary:
                    raise
                except ValueError:
                    # The cursor can belong to another overlapping block.
                    continue
                if provisional is not None:
                    pivot = next(
                        item.start
                        for item in items
                        if (item.key, item.index)
                        == (provisional.key, provisional.index)
                    )
                    # A later effective block can contain an earlier boundary
                    # sentence. Inspect every potentially earlier source before
                    # committing this chronological prefix.
                    if all(
                        _request_bound(later, "from") > pivot
                        for later in source_ranges[index + 1 :]
                        if later.id not in loaded
                    ):
                        break
            items = _cache_items(
                blocks, window_from, processed_to, initial=window_from == start
            )
            try:
                entries, next_position = pack_items(
                    items,
                    position=position,
                    request=(start, end),
                    budget=page_budget,
                    payload=payload,
                )
            except ToolResultPageBoundary:
                raise
            except ValueError as exc:
                raise MediaError("TRANSCRIPT_CONTINUATION_INVALID", str(exc)) from exc
            if (
                next_position is not None
                or processed_to >= end
                or errors
                or unpersisted
                or attempted_asr_seconds >= NEW_ASR_SECONDS_PER_CALL
            ):
                source.assert_unchanged()
                final = payload(entries, next_position)
                return TranscriptPage(final, is_error=bool(final.get("errors")))
            # Quote a minimum original character with a complete next before any
            # paid work. Existing text gets priority over the next audio batch.
            placeholder = TranscriptItem(
                asr_cache_id(key, processed_to, end), 0, processed_to, end, "x", "asr"
            )
            probe_position = TranscriptPosition(placeholder.key, 0, TextPosition(1, 1))
            if not page_budget.fits(
                json_body(
                    payload(
                        [
                            *entries,
                            placeholder.fragment("x", TextPosition(), (start, end)),
                        ],
                        probe_position,
                    )
                )
            ):
                if entries or processed_to > window_from:
                    return TranscriptPage(payload(entries, None))
                raise ToolResultPageBoundary(
                    "A minimum timestamped transcript and complete next cannot fit"
                )
            if self.asr_config is None:
                errors.append(
                    {
                        "from": processed_to,
                        "to": end,
                        "error": "ASR_NOT_CONFIGURED",
                        "message": "Configure an ASR service to transcribe missing audio.",
                    }
                )
                unavailable = payload(entries, None)
                if not page_budget.fits(json_body(unavailable)):
                    raise ToolResultPageBoundary(
                        "ASR configuration error does not fit this call's allowance"
                    )
                return TranscriptPage(unavailable, is_error=True)
            planned = plan_gaps(
                missing_ranges(ranges, processed_to, end),
                maximum_audio_bytes=self.maximum_upload_bytes,
                maximum_seconds=NEW_ASR_SECONDS_PER_CALL - attempted_asr_seconds,
            )
            new_blocks = []

            def prepare(a: float, b: float):
                return self._prepare_gap(source, info, key, a, b)

            # Overlapping calls share accepted work, independent of configured
            # model. A logical cancellation removes only that caller's waiter;
            # the final waiter cancels and physically settles the shared task.
            work = self.work
            # Repack after each logical gap, including any cached middle. The
            # next paid gap starts only while this page can still deliver text.
            for a, b in planned[:1]:
                attempted_asr_seconds += b - a
                begin(window_from, b)
                for _actual_from, _actual_to, outcome in await work.run(
                    key, a, b, prepare
                ):
                    new_blocks.extend(outcome.blocks)
                    errors.extend(outcome.errors)
                    diagnostics.extend(outcome.diagnostics)
                    unpersisted.update(outcome.unpersisted)
            for block in new_blocks:
                observe_sound(block)
            blocks = tuple(
                sorted(
                    {
                        block.id: block
                        for block in (*blocks, *new_blocks)
                        if block.payload["items"]
                    }.values(),
                    key=lambda block: (block.from_seconds, block.to_seconds, block.id),
                )
            )
            loaded.update(block.id for block in new_blocks)
            ranges = tuple(
                sorted(
                    {block.id: block for block in (*ranges, *new_blocks)}.values(),
                    key=lambda block: (block.from_seconds, block.to_seconds, block.id),
                )
            )
            processed_to = completed_prefix(ranges, window_from, end)
            if processed_to > window_from:
                begin(window_from, processed_to)
            if processed_to > window_from:
                anchor = next(
                    (
                        block.id
                        for block in ranges
                        if block.from_seconds < processed_to <= block.to_seconds
                    ),
                    None,
                )
            position = None
