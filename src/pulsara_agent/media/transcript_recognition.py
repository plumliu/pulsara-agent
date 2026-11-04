"""One accepted ASR gap's source, deadline, charging and cache boundary.

The Core work owner alone keeps this value alive across independent waiters.
Its source is independently bound, and its first configuration/deadline never
change when another caller joins. Pending work has no durable representation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .asr import AsrRequestObservation
from .asr_cache import AsrCacheBlock, AsrMediaKey, asr_cache_id
from .audio import ExtractedAudio, extract_audio
from .source import MediaError, MediaInfo, MediaSource
from .transcript_ranges import missing_ranges


@dataclass(frozen=True, slots=True)
class AsrBlockOutcome:
    blocks: tuple[AsrCacheBlock, ...] = ()
    errors: tuple[dict[str, object], ...] = ()
    diagnostics: tuple[str, ...] = ()
    unpersisted: tuple[str, ...] = ()


@dataclass(slots=True)
class _RecognitionProgress:
    successful: dict[str, AsrCacheBlock] = field(default_factory=dict)
    failures: list[dict[str, object]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    uncached: set[str] = field(default_factory=set)
    request: AsrRequestObservation = field(default_factory=AsrRequestObservation)
    audio: ExtractedAudio | None = None
    start: float = 0
    end: float = 0

    def outcome(self) -> AsrBlockOutcome:
        return AsrBlockOutcome(
            tuple(self.successful.values()),
            tuple(self.failures),
            tuple(self.warnings),
            tuple(self.uncached),
        )


@dataclass(frozen=True, slots=True)
class AcceptedAsrGap:
    source: MediaSource
    info: MediaInfo
    key: AsrMediaKey
    start: float
    end: float
    cache: Any
    config: object
    run_media: Callable[..., Awaitable[Any]]
    run_cache: Callable[..., Awaitable[Any]]
    transcribe: Callable[..., Awaitable[Any]]
    prepare_transcribe: Callable[..., Awaitable[Any]] | None
    maximum_upload_bytes: int
    maximum_response_bytes: int
    deadline: float
    credential_scrub_set: object

    async def run(self) -> AsrBlockOutcome:
        progress = _RecognitionProgress(start=self.start, end=self.end)
        try:
            async with asyncio.timeout_at(self.deadline):
                await self._recognize(progress)
        except TimeoutError:
            # Validation can finish before SDK close/cache settlement expires.
            # Keep that actual response; it is not an unknown remote result.
            if progress.request.result is not None and progress.audio is not None:
                self.source.assert_unchanged()
                block = self._local_block(progress, progress.request.result)
                progress.successful.setdefault(block.id, block)
                progress.uncached.add(block.id)
                progress.warnings.append(
                    "ASR completed before the deadline; cache persistence could not be confirmed."
                )
            elif not progress.successful or progress.start < progress.end:
                unknown = progress.request.remote_result_unknown
                progress.failures.append(
                    {
                        "from": progress.start,
                        "to": progress.end,
                        "error": "MEDIA_PROCESSING_DEADLINE_EXPIRED",
                        "message": (
                            "The original ASR deadline expired; the remote result is unknown and may have been billed."
                            if unknown
                            else "The original ASR deadline expired before any unconfirmed remote submission."
                        ),
                        "remote_result_unknown": unknown,
                        "request_accounting": progress.request.public_value(),
                    }
                )
        finally:
            self.source.close()
        return progress.outcome()

    async def _cache(self, operation, *args):
        return await self.run_cache(operation, *args, deadline_monotonic=self.deadline)

    async def _recognize(self, progress: _RecognitionProgress) -> None:
        # Recheck under the accepted in-flight claim before preparing paid audio.
        current = await self._cache(
            self.cache.query_ranges, self.key, self.start, self.end
        )
        for row in current:
            block = await self._cache(self.cache.query_block, self.key, row.id)
            if block is None:
                raise MediaError(
                    "ASR_CACHE_UNAVAILABLE",
                    "ASR cache changed before recognition admission",
                )
            progress.successful[block.id] = block
        for start, end in missing_ranges(current, self.start, self.end):
            progress.start, progress.end = start, end
            progress.audio = None
            progress.request = AsrRequestObservation()
            try:
                await self._recognize_range(progress)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if getattr(exc, "code", None) == "MEDIA_SOURCE_CHANGED":
                    raise
                progress.failures.append(
                    {
                        "from": start,
                        "to": end,
                        "error": getattr(exc, "code", "ASR_FAILED"),
                        "message": str(exc),
                        "remote_result_unknown": bool(
                            getattr(exc, "remote_result_unknown", False)
                        ),
                        "request_accounting": progress.request.public_value(),
                    }
                )

    async def _recognize_range(self, progress: _RecognitionProgress) -> None:
        async def prepare_audio():
            progress.audio = await self.run_media(
                extract_audio,
                self.source,
                self.info,
                progress.start,
                progress.end,
                max_bytes=self.maximum_upload_bytes,
                deadline_monotonic=self.deadline,
            )
            return progress.audio

        request = {
            "maximum_upload_bytes": self.maximum_upload_bytes,
            "maximum_response_bytes": self.maximum_response_bytes,
            "credential_scrub_set": self.credential_scrub_set,
            "deadline_monotonic": self.deadline,
            "observation": progress.request,
        }
        if self.prepare_transcribe is not None:
            audio, result = await self.prepare_transcribe(
                self.config, prepare_audio, **request
            )
        else:
            audio = await prepare_audio()
            result = await self.transcribe(
                self.config,
                audio.data,
                duration=audio.end - audio.start,
                filename=audio.filename,
                mime_type=audio.mime_type,
                **request,
            )
        self.source.assert_unchanged()
        progress.request.result = result
        block = self._local_block(progress, result)
        progress.successful[block.id] = block
        progress.uncached.add(block.id)
        try:
            saved = await self._cache(
                self.cache.save_success,
                self.key,
                progress.start,
                progress.end,
                block.provenance,
                block.payload,
            )
            progress.successful[block.id] = saved
            progress.uncached.remove(block.id)
        except Exception as exc:
            progress.warnings.append(
                f"ASR cache write failed: {type(exc).__name__}: {exc}"
            )

    def _local_block(self, progress: _RecognitionProgress, result) -> AsrCacheBlock:
        audio = progress.audio
        assert audio is not None
        sound = None if audio.sound is None else asdict(audio.sound)
        payload = {
            "text": result.text,
            "granularity": result.granularity,
            "items": [
                {
                    "start": item.start + audio.start,
                    "end": item.end + audio.start,
                    "text": item.text,
                    "speaker": item.speaker,
                }
                for item in result.items
            ],
            "request_range": {"from": audio.start, "to": audio.end},
            "sound": sound,
            "diagnostics": list(result.diagnostics),
        }
        provenance = {
            "endpoint": getattr(self.config, "endpoint", None),
            "model": getattr(self.config, "model_id", None),
            "response_model": result.model,
            "language": result.language,
            "selected_audio_stream": self.info.audio_stream_index,
            "timeline_origin": self.info.timeline_origin,
            "asr_requests": progress.request.public_value(),
        }
        return AsrCacheBlock(
            asr_cache_id(self.key, progress.start, progress.end),
            self.key,
            progress.start,
            progress.end,
            provenance,
            payload,
            datetime.now(timezone.utc),
        )
