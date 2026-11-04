from __future__ import annotations

import asyncio
from functools import wraps
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
import wave

import pytest

from pulsara_agent.media.asr import AsrError, AsrTimestamp, AsrTranscript
from pulsara_agent.media.asr_cache import (
    AsrCacheBlock,
    AsrCacheRange,
    AsrMediaKey,
    asr_cache_id,
)
from pulsara_agent.media.audio import ExtractedAudio, SoundObservation, extract_audio
from pulsara_agent.media.source import MediaError, MediaInfo, MediaSource, probe_media
from pulsara_agent.media.subtitles import (
    SubtitleEntry,
    SubtitleReadResult,
    read_subtitles,
)
from pulsara_agent.media.transcript import TranscriptService, missing_ranges, plan_gaps
from pulsara_agent.media.transcript_work import TranscriptWork
from pulsara_agent.primitives.tool_result_budget import (
    ToolResultPageBudget,
    ToolResultPageBoundary,
)


@dataclass
class Source:
    path: Path = Path("/test/audio.wav")
    modified_ns: int = 1793000000123456789
    size: int = 1024

    def assert_unchanged(self):
        pass

    def clone(self):
        return self

    def close(self):
        pass


def info(duration=3600, audio=0):
    return MediaInfo(duration, 0, None, audio, (), None, None)


class Cache:
    def __init__(self):
        self.blocks = []
        self.read_ids = []
        self.fail_write = False

    def query_ranges(self, key, start, end, **_):
        return tuple(
            AsrCacheRange(
                b.id,
                b.from_seconds,
                b.to_seconds,
                b.payload["request_range"]["from"],
                b.payload["request_range"]["to"],
            )
            for b in self.blocks
            if b.key == key and b.from_seconds < end and b.to_seconds > start
        )

    def query_block(self, key, block_id, **_):
        self.read_ids.append(block_id)
        return next((b for b in self.blocks if b.id == block_id and b.key == key), None)

    def touch(self, *_args, **_kwargs):
        pass

    def save_success(self, key, start, end, provenance, payload, **_):
        if self.fail_write:
            raise OSError("cache unavailable")
        block_id = asr_cache_id(key, start, end)
        block = next((b for b in self.blocks if b.id == block_id), None)
        if block is None:
            block = AsrCacheBlock(
                block_id,
                key,
                start,
                end,
                provenance,
                payload,
                datetime.now(timezone.utc),
            )
            self.blocks.append(block)
            self.blocks.sort(key=lambda b: (b.from_seconds, b.to_seconds, b.id))
        return block


def seed(cache, start, end, text="", *, source=None, items=None, sound=None):
    source = source or Source()
    key = AsrMediaKey("default", str(source.path), source.modified_ns, source.size)
    return cache.save_success(
        key,
        start,
        end,
        {},
        {
            "text": text,
            "granularity": "segment" if text else None,
            "items": items
            if items is not None
            else (
                [{"start": start, "end": end, "text": text, "speaker": None}]
                if text
                else []
            ),
            "request_range": {"from": start, "to": end},
            "sound": sound,
            "diagnostics": [],
        },
    )


async def direct(operation, *args, **kwargs):
    kwargs.pop("deadline_monotonic", None)
    return operation(*args, **kwargs)


class Harness:
    def __init__(self, *, subtitle=None, response=None):
        self.cache = Cache()
        self.work = TranscriptWork()
        self.requests = []
        self.subtitle = subtitle or SubtitleReadResult(False, None, None, ())
        self.response = response
        self.extractions = []

    async def media(self, operation, *args, **kwargs):
        if operation is read_subtitles:
            return self.subtitle
        if operation is extract_audio:
            a, b = args[2:4]
            self.extractions.append((a, b))
            return ExtractedAudio(
                b"audio", a, b, SoundObservation(a, b, "sound_present")
            )
        return await direct(operation, *args, **kwargs)

    async def asr(self, _config, _audio, *, duration, **_):
        self.requests.append(duration)
        if self.response:
            return self.response(duration, len(self.requests))
        return AsrTranscript("", (), None)

    def service(self, **kwargs):
        return TranscriptService(
            work=self.work,
            cache=self.cache,
            asr_config=object(),
            run_media=self.media,
            run_cache=direct,
            maximum_upload_bytes=16 * 1024 * 1024,
            maximum_response_bytes=4 * 1024 * 1024,
            deadline_monotonic=monotonic() + 30,
            transcribe=self.asr,
            **kwargs,
        )


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


BUDGET = ToolResultPageBudget("call")


@run_async
async def test_one_hour_empty_audio_advances_only_180_seconds():
    h = Harness()
    page = await h.service().read(
        Source(), info(), {}, BUDGET, memory_domain_id="default"
    )
    assert h.extractions == [(0, 180)]
    assert page.payload["processed_range"] == {"from": 0, "to": 180}
    assert (
        page.payload["next"]["arguments"]["continuation"]["source_modified_ns"]
        == "1793000000123456789"
    )
    assert page.payload["sound_observations"][0]["state"] == "sound_present"
    next_page = await h.service().read(
        Source(),
        info(),
        page.payload["next"]["arguments"],
        BUDGET,
        memory_domain_id="default",
    )
    assert h.extractions[-1] == (180, 360)
    assert next_page.payload["processed_range"] == {"from": 180, "to": 360}


@run_async
async def test_multiple_gaps_share_one_180_second_allowance():
    h = Harness()
    seed(h.cache, 180, 360)
    page = await h.service().read(
        Source(), info(), {"from": 150, "to": 600}, BUDGET, memory_domain_id="default"
    )
    assert h.extractions == [(150, 180), (360, 510)]
    assert sum(h.requests) == 180
    assert page.payload["processed_range"]["to"] == 510
    assert page.payload["next"]["arguments"]["from"] == 150


@run_async
async def test_subtitle_empty_gap_never_falls_back_to_asr_without_audio_or_configuration():
    h = Harness(subtitle=SubtitleReadResult(True, 2, "en", ()))
    service = h.service()
    service.asr_config = None
    page = await service.read(
        Source(), info(audio=None), {}, BUDGET, memory_domain_id="default"
    )
    assert page.payload["complete"] is True
    assert page.payload["source"] == "subtitle"
    assert not h.requests and not h.extractions


@run_async
async def test_no_audio_is_terminal_and_never_requires_asr_config():
    h = Harness()
    page = await h.service().read(
        Source(), info(audio=None), {}, BUDGET, memory_domain_id="default"
    )
    assert page.is_error and page.payload["error"] == "NO_AUDIO_TRACK"
    assert page.payload["next"] is None and not h.requests


@run_async
async def test_full_cached_hour_exceeds_180_seconds_and_does_not_need_asr():
    h = Harness()
    seed(h.cache, 0, 3600, "original")
    service = h.service()
    service.asr_config = None
    page = await service.read(Source(), info(), {}, BUDGET, memory_domain_id="default")
    assert page.payload["complete"] is True
    assert page.payload["processed_range"]["to"] == 3600
    assert page.payload["entries"][0]["text"] == "original"
    assert not h.requests


@run_async
async def test_long_same_time_subtitle_fragments_preserve_all_text_and_real_time():
    original = '汉字\\"\n' * 200
    h = Harness(
        subtitle=SubtitleReadResult(
            True,
            2,
            "zh",
            (SubtitleEntry(10, 20, original, 0), SubtitleEntry(10, 20, "second", 1)),
        )
    )
    budget = ToolResultPageBudget(
        "real-call", accepts_provider_body=lambda body, _: len(body.encode()) < 1350
    )
    arguments = {"from": 15, "to": 3600}
    collected = []
    for _ in range(100):
        page = await h.service().read(
            Source(), info(), arguments, budget, memory_domain_id="default"
        )
        assert budget.fits(page.body)
        for entry in page.payload["entries"]:
            collected.append(entry["text"])
            assert entry["from"] == 10 and entry["to"] == 20
            assert entry["visible_range"] == {"from": 15, "to": 20}
        if page.payload["next"] is None:
            break
        arguments = page.payload["next"]["arguments"]
    else:
        pytest.fail("continuation did not advance")
    assert "".join(collected) == original + "second"
    assert not h.requests


@run_async
async def test_pending_cached_text_precedes_next_paid_window_and_same_next_is_observational():
    h = Harness()
    seed(h.cache, 0, 180, "A" * 2000)
    budget = ToolResultPageBudget(
        "call", accepts_provider_body=lambda body, _: len(body) < 1300
    )
    page = await h.service().read(
        Source(), info(), {}, budget, memory_domain_id="default"
    )
    arguments = page.payload["next"]["arguments"]
    repeat1 = await h.service().read(
        Source(), info(), arguments, budget, memory_domain_id="default"
    )
    repeat2 = await h.service().read(
        Source(), info(), arguments, budget, memory_domain_id="default"
    )
    assert repeat1.body == repeat2.body
    assert not h.requests
    h.cache.blocks.clear()
    with pytest.raises(MediaError) as error:
        await h.service().read(
            Source(), info(), arguments, budget, memory_domain_id="default"
        )
    assert error.value.code == "TRANSCRIPT_CONTINUATION_INVALID"
    assert not h.requests


@run_async
async def test_completed_boundary_next_cannot_reidentify_cleaned_cache():
    h = Harness()
    page = await h.service().read(
        Source(), info(), {}, BUDGET, memory_domain_id="default"
    )
    h.cache.blocks.clear()
    with pytest.raises(MediaError, match="cleared"):
        await h.service().read(
            Source(),
            info(),
            page.payload["next"]["arguments"],
            BUDGET,
            memory_domain_id="default",
        )
    assert len(h.requests) == 1


@run_async
async def test_partial_failure_preserves_successful_prefix_and_later_cache_without_retry_next():
    def response(duration, index):
        if index == 2:
            raise AsrError(
                "ASR_FAILED", "remote result unknown", remote_result_unknown=True
            )
        return AsrTranscript(
            "prefix", (AsrTimestamp(0, duration, "prefix"),), "segment"
        )

    h = Harness(response=response)
    seed(h.cache, 30, 60)
    later = seed(h.cache, 210, 240, "later cached original")
    page = await h.service().read(
        Source(), info(240), {}, BUDGET, memory_domain_id="default"
    )
    assert page.is_error and page.payload["processed_range"]["to"] == 60
    assert page.payload["next"] is None
    assert page.payload["errors"][0]["remote_result_unknown"] is True
    assert later in h.cache.blocks
    assert page.payload["entries"][0]["text"] == "prefix"
    assert h.extractions == [(0, 30), (60, 210)]
    assert sum(h.requests) == 180


@run_async
async def test_smallest_page_boundary_prevents_paid_request():
    h = Harness()
    budget = ToolResultPageBudget("call", accepts_provider_body=lambda _body, _: False)
    with pytest.raises(ToolResultPageBoundary):
        await h.service().read(Source(), info(), {}, budget, memory_domain_id="default")
    assert not h.requests and not h.extractions


@run_async
async def test_cache_write_failure_delivers_original_once_and_never_resubmits():
    h = Harness(
        response=lambda duration, _: AsrTranscript(
            "real", (AsrTimestamp(0, duration, "real", "speaker"),), "segment"
        )
    )
    h.cache.fail_write = True
    page = await h.service().read(
        Source(), info(10), {}, BUDGET, memory_domain_id="default"
    )
    assert page.payload["complete"] is True
    assert page.payload["entries"][0]["speaker"] == "speaker"
    assert page.payload["entries"][0]["text"] == "real"
    assert len(h.requests) == 1 and not h.cache.blocks


@run_async
async def test_real_pyav_wav_extract_and_timestamp_map_pipeline(tmp_path):
    path = tmp_path / "short.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 32000)
    cache = Cache()
    calls = []

    async def transcribe(_config, data, *, duration, **_):
        assert data.startswith(b"RIFF")
        calls.append(duration)
        return AsrTranscript(
            "spoken", (AsrTimestamp(0.1, 0.3, "spoken", "A"),), "segment"
        )

    service = TranscriptService(
        work=TranscriptWork(),
        cache=cache,
        asr_config=SimpleNamespace(endpoint="https://example.com", model_id="test"),
        run_media=direct,
        run_cache=direct,
        maximum_upload_bytes=16 * 1024 * 1024,
        maximum_response_bytes=4 * 1024 * 1024,
        deadline_monotonic=monotonic() + 30,
        transcribe=transcribe,
    )
    with MediaSource.open(path) as source:
        page = await service.read(
            source,
            probe_media(source),
            {"from": 1, "to": 2},
            BUDGET,
            memory_domain_id="default",
        )
    assert page.payload["complete"] is True
    assert page.payload["entries"][0]["from"] == 1.1
    assert page.payload["entries"][0]["to"] == 1.3
    assert calls == [1]
    assert cache.blocks[0].payload["sound"]["state"] == "near_silent"


def test_gap_union_does_not_swallow_tiny_unprocessed_gap():
    h = Harness()
    a = seed(h.cache, 0, 1)
    b = seed(h.cache, 1.00000001, 2)
    assert missing_ranges((a, b), 0, 2) == ((1, 1.00000001),)
    assert (
        sum(
            end - start
            for start, end in plan_gaps(
                ((150, 180), (360, 600)), maximum_audio_bytes=16 * 1024 * 1024
            )
        )
        == 180
    )


@run_async
async def test_overlapping_concurrent_reads_share_work_across_models_and_cancel_one_waiter():
    h = Harness()
    accepted = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def transcribe(config, _audio, *, duration, **_):
        calls.append((config, duration))
        if len(calls) == 1:
            accepted.set()
            await release.wait()
        return AsrTranscript("", (), None)

    first = h.service()
    first.asr_config = "A"
    first.transcribe = transcribe
    second = h.service()
    second.asr_config = "B"
    second.transcribe = transcribe
    one = asyncio.create_task(
        first.read(
            Source(),
            info(300),
            {"from": 0, "to": 180},
            BUDGET,
            memory_domain_id="default",
        )
    )
    await accepted.wait()
    two = asyncio.create_task(
        second.read(
            Source(),
            info(300),
            {"from": 90, "to": 270},
            BUDGET,
            memory_domain_id="default",
        )
    )
    # The second call must actually join before the original waiter departs.
    for _ in range(20):
        await asyncio.sleep(0)
        if len(calls) == 2:
            break
    one.cancel()
    with pytest.raises(asyncio.CancelledError):
        await one
    release.set()
    page = await two
    assert sorted(calls) == [("A", 180), ("B", 90)]
    assert page.payload["complete"] is True
    assert page.payload["processed_range"] == {"from": 90, "to": 270}
    assert sorted(h.extractions) == [(0, 180), (180, 270)]


@run_async
async def test_last_waiter_cancellation_settles_shared_request_without_cache_coverage():
    h = Harness()
    accepted = asyncio.Event()
    settled = asyncio.Event()

    async def transcribe(*_args, **_kwargs):
        accepted.set()
        try:
            await asyncio.Event().wait()
        finally:
            settled.set()

    service = h.service()
    service.transcribe = transcribe
    task = asyncio.create_task(
        service.read(Source(), info(), {}, BUDGET, memory_domain_id="default")
    )
    await accepted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert settled.is_set()
    assert not h.cache.blocks


@run_async
async def test_real_subtitle_physical_chunks_and_same_time_source_positions(tmp_path):
    path = tmp_path / "subtitles.srt"
    original = "字幕" * 400
    path.write_text(
        f"1\n00:00:01,000 --> 00:00:02,000\n{original}\n\n2\n00:00:01,000 --> 00:00:02,000\nsecond\n"
    )
    h = Harness()
    service = TranscriptService(
        work=h.work,
        cache=h.cache,
        asr_config=None,
        run_media=direct,
        run_cache=direct,
        maximum_upload_bytes=16 * 1024 * 1024,
        maximum_response_bytes=256,
        deadline_monotonic=monotonic() + 30,
        transcribe=h.asr,
    )
    arguments = {"from": 0, "to": 3}
    text = []
    with MediaSource.open(path) as source:
        metadata = probe_media(source)
        for _ in range(100):
            page = await service.read(
                source, metadata, arguments, BUDGET, memory_domain_id="default"
            )
            text.extend(entry["text"] for entry in page.payload["entries"])
            if page.payload["next"] is None:
                break
            arguments = page.payload["next"]["arguments"]
        else:
            pytest.fail("subtitle source page did not progress")
    assert "".join(text) == original + "second"
    assert not h.requests


@run_async
async def test_joined_waiter_retains_first_accepted_deadline_and_does_not_resubmit():
    h = Harness()
    accepted = asyncio.Event()
    cancelled = asyncio.Event()
    calls = []

    async def transcribe(*_args, **_kwargs):
        calls.append(1)
        accepted.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    first = h.service()
    first.transcribe = transcribe
    first.deadline = monotonic() + 0.05
    second = h.service()
    second.transcribe = transcribe
    second.deadline = monotonic() + 10
    a = asyncio.create_task(
        first.read(Source(), info(180), {}, BUDGET, memory_domain_id="default")
    )
    await accepted.wait()
    b = asyncio.create_task(
        second.read(Source(), info(180), {}, BUDGET, memory_domain_id="default")
    )
    pa, pb = await asyncio.gather(a, b)
    assert pa.payload["errors"] == pb.payload["errors"]
    assert pa.payload["errors"][0]["error"] == "MEDIA_PROCESSING_DEADLINE_EXPIRED"
    assert pa.payload["next"] is None and pb.payload["next"] is None
    assert calls == [1] and cancelled.is_set() and not h.cache.blocks


@run_async
async def test_cache_read_failure_is_not_an_empty_cache_and_never_submits_asr():
    h = Harness()

    def fail(*_args, **_kwargs):
        raise OSError("database read failed")

    h.cache.query_ranges = fail
    with pytest.raises(OSError, match="database read failed"):
        await h.service().read(Source(), info(), {}, BUDGET, memory_domain_id="default")
    assert not h.requests and not h.extractions


@run_async
async def test_work_close_stops_claims_and_settles_prepared_source_cleanup():
    from pulsara_agent.media.transcript_work import PreparedTranscriptWork

    work = TranscriptWork()
    entered = asyncio.Event()
    cleaned = []
    key = AsrMediaKey("default", "/test/audio.wav", 1, 2)

    def prepare(_a, _b):
        async def wait():
            entered.set()
            await asyncio.Event().wait()

        return PreparedTranscriptWork(wait(), lambda: cleaned.append(True))

    task = asyncio.create_task(work.run(key, 0, 1, prepare))
    await entered.wait()
    await work.aclose()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert work.physical_close_complete and cleaned == [True]
    with pytest.raises(MediaError, match="closing"):
        await work.run(key, 0, 1, prepare)


@run_async
async def test_partial_claim_preparation_failure_does_not_leak_new_work():
    from pulsara_agent.media.transcript_work import PreparedTranscriptWork

    work = TranscriptWork()
    key = AsrMediaKey("default", "/test/audio.wav", 1, 2)
    accepted = asyncio.Event()
    release = asyncio.Event()
    cleaned = []

    def middle(_a, _b):
        async def wait():
            accepted.set()
            await release.wait()
            return None

        return PreparedTranscriptWork(wait(), lambda: cleaned.append("middle"))

    task = asyncio.create_task(work.run(key, 1, 2, middle))
    await accepted.wait()

    def failing(a, _b):
        if a == 2:
            raise OSError("clone failed")

        async def wait():
            await asyncio.Event().wait()

        return PreparedTranscriptWork(wait(), lambda: cleaned.append("first-gap"))

    with pytest.raises(OSError, match="clone failed"):
        await work.run(key, 0, 3, failing)
    assert cleaned == ["first-gap"]
    assert len(work._active) == 1
    release.set()
    await task
    await work.aclose()
    assert sorted(cleaned) == ["first-gap", "middle"]


@run_async
async def test_overlapping_cache_pages_merge_chronologically_and_keep_earlier_block_later_items():
    h = Harness()
    a, middle, later = "A" * 800, "B" * 800, "C" * 800
    seed(
        h.cache,
        0,
        10,
        a + later,
        items=[
            {"start": 1, "end": 2, "text": a, "speaker": None},
            {"start": 7, "end": 9, "text": later, "speaker": None},
        ],
    )
    seed(
        h.cache,
        4,
        8,
        middle,
        items=[{"start": 4, "end": 5, "text": middle, "speaker": None}],
    )
    budget = ToolResultPageBudget(
        "call", accepts_provider_body=lambda body, _: len(body) < 1900
    )
    arguments = {"from": 0, "to": 10}
    text = []
    times = []
    for _ in range(30):
        page = await h.service().read(
            Source(), info(10), arguments, budget, memory_domain_id="default"
        )
        assert budget.fits(page.body)
        text.extend(item["text"] for item in page.payload["entries"])
        times.extend(item["from"] for item in page.payload["entries"])
        if page.payload["next"] is None:
            break
        arguments = page.payload["next"]["arguments"]
    else:
        pytest.fail("overlap page did not advance")
    assert "".join(text) == a + middle + later
    assert times == sorted(times)
    assert not h.requests


@run_async
async def test_context_boundary_novel_earlier_item_loaded_before_later_provisional_cursor():
    h = Harness()
    seed(
        h.cache,
        0,
        10,
        "A" * 800 + "C" * 800,
        items=[
            {"start": 1, "end": 2, "text": "A" * 800, "speaker": None},
            {"start": 7, "end": 9, "text": "C" * 800, "speaker": None},
        ],
    )
    boundary = seed(
        h.cache,
        4,
        8,
        "B" * 800,
        items=[{"start": 3, "end": 5, "text": "B" * 800, "speaker": None}],
    )
    boundary.payload["request_range"] = {"from": 2, "to": 9}
    budget = ToolResultPageBudget(
        "call", accepts_provider_body=lambda body, _: len(body) < 1900
    )
    arguments = {"from": 0, "to": 10}
    text = []
    for _ in range(30):
        page = await h.service().read(
            Source(), info(10), arguments, budget, memory_domain_id="default"
        )
        text.extend(item["text"] for item in page.payload["entries"])
        if page.payload["next"] is None:
            break
        arguments = page.payload["next"]["arguments"]
    assert "".join(text) == "A" * 800 + "B" * 800 + "C" * 800


@run_async
async def test_overlap_dedup_only_cross_block_same_utterance_and_preserves_repeated_source_positions():
    h = Harness()
    seed(
        h.cache,
        0,
        10,
        "same same later",
        items=[
            {"start": 4, "end": 5, "text": "same", "speaker": None},
            {"start": 4, "end": 5, "text": "same", "speaker": None},
            {"start": 8, "end": 9, "text": "same", "speaker": None},
        ],
    )
    seed(
        h.cache,
        4,
        6,
        "same novel",
        items=[
            {"start": 4.1, "end": 5.1, "text": "same", "speaker": None},
            {"start": 5.2, "end": 5.5, "text": "novel", "speaker": None},
        ],
    )
    page = await h.service().read(
        Source(), info(10), {}, BUDGET, memory_domain_id="default"
    )
    assert [item["text"] for item in page.payload["entries"]] == [
        "same",
        "same",
        "novel",
        "same",
    ]
    assert len(page.payload["diagnostics"]) == 1


@run_async
async def test_actual_word_grouping_respects_punctuation_pause_speaker_and_raw_words_remain():
    h = Harness()
    rows = [
        {"start": 0, "end": 1, "text": "Hello", "speaker": "A"},
        {"start": 1.08, "end": 2, "text": "world.", "speaker": "A"},
        {"start": 2, "end": 3, "text": "Again", "speaker": "A"},
        {"start": 3.5, "end": 4, "text": "pause", "speaker": "A"},
        {"start": 4, "end": 5, "text": "new", "speaker": "B"},
        {"start": 4, "end": 5, "text": "new", "speaker": "B"},
    ]
    block = seed(h.cache, 0, 10, "Hello world. Again pause new new", items=rows)
    block.payload["granularity"] = "word"
    page = await h.service().read(
        Source(), info(10), {}, BUDGET, memory_domain_id="default"
    )
    assert [
        (item["text"], item["from"], item["to"], item.get("speaker"))
        for item in page.payload["entries"]
    ] == [
        ("Hello world.", 0, 2, "A"),
        ("Again", 2, 3, "A"),
        ("pause", 3.5, 4, "A"),
        ("new new", 4, 5, "B"),
    ]
    assert block.payload["items"] == rows


@run_async
async def test_cached_middle_filling_page_prevents_next_paid_gap():
    h = Harness()
    seed(h.cache, 30, 60, "M" * 5000)
    budget = ToolResultPageBudget(
        "call", accepts_provider_body=lambda body, _: len(body) < 1800
    )
    page = await h.service().read(
        Source(), info(240), {}, budget, memory_domain_id="default"
    )
    assert h.extractions == [(0, 30)]
    assert page.payload["processed_range"]["to"] == 60
    assert page.payload["next"]["arguments"]["continuation"]["position"] is not None


@run_async
async def test_uncached_long_original_is_explicit_terminal_partial_without_unusable_next():
    h = Harness(
        response=lambda duration, _: AsrTranscript(
            "X" * 5000, (AsrTimestamp(0, duration, "X" * 5000),), "segment"
        )
    )
    h.cache.fail_write = True
    budget = ToolResultPageBudget(
        "call", accepts_provider_body=lambda body, _: len(body) < 1800
    )
    page = await h.service().read(
        Source(), info(180), {}, budget, memory_domain_id="default"
    )
    assert page.is_error and page.payload["status"] == "partial"
    assert page.payload["next"] is None and page.payload["complete"] is False
    assert page.payload["errors"][0]["error"] == "ASR_CACHE_WRITE_FAILED"
    assert 0 < len(page.payload["entries"][0]["text"]) < 5000
    assert len(h.requests) == 1 and budget.fits(page.body)


@run_async
async def test_equal_time_overlap_dedup_preserves_greatest_original_repeat_multiplicity():
    h = Harness()
    for start, end, count in [(0, 10, 2), (1, 9, 3), (2, 8, 4)]:
        seed(
            h.cache,
            start,
            end,
            " ".join(["yes"] * count),
            items=[
                {"start": 4, "end": 5, "text": "yes", "speaker": None}
                for _ in range(count)
            ],
        )
    page = await h.service().read(
        Source(), info(10), {}, BUDGET, memory_domain_id="default"
    )
    assert [item["text"] for item in page.payload["entries"]] == ["yes"] * 4


@run_async
async def test_forged_boundary_or_missing_anchor_never_skips_unprocessed_audio():
    from copy import deepcopy

    h = Harness()
    page = await h.service().read(
        Source(), info(600), {}, BUDGET, memory_domain_id="default"
    )
    arguments = page.payload["next"]["arguments"]
    for field, value in [
        ("anchor", None),
        ("window_from", 360),
        ("source", "subtitle"),
    ]:
        forged = deepcopy(arguments)
        forged["continuation"][field] = value
        if field == "window_from":
            forged["continuation"]["processed_to"] = 360
        with pytest.raises(MediaError) as failure:
            await h.service().read(
                Source(), info(600), forged, BUDGET, memory_domain_id="default"
            )
        assert failure.value.code == "INVALID_ARGUMENTS"
    assert len(h.requests) == 1


@run_async
async def test_original_deadline_before_quota_or_decode_never_claims_possible_charge():
    from pulsara_agent.media.asr import AsrClient
    from pulsara_agent.media.asr_config import AsrConnection

    for phase in ("quota", "decode", "cache"):
        h = Harness()
        client = AsrClient()
        service = h.service()
        service.asr_config = AsrConnection(
            "http://127.0.0.1:1/transcriptions", "test", "fixture-secret"
        )
        service.prepare_transcribe = client.prepare_and_transcribe
        service.deadline = monotonic() + 0.03
        if phase == "quota":
            await client._admission.acquire()
        elif phase == "decode":

            async def waiting_decode(*_args, **_kwargs):
                await asyncio.Event().wait()

            service.run_asr_media = waiting_decode
        else:

            async def waiting_cache(*_args, **_kwargs):
                await asyncio.Event().wait()

            service.run_asr_cache = waiting_cache
        try:
            page = await service.read(
                Source(), info(180), {}, BUDGET, memory_domain_id="default"
            )
        finally:
            if phase == "quota":
                client._admission.release()
        error = page.payload["errors"][0]
        assert error["remote_result_unknown"] is False
        assert error["request_accounting"] == {"attempts": []}
        assert not h.requests and not h.extractions


@run_async
async def test_original_deadline_after_valid_response_preserves_original_during_cache_settlement():
    h = Harness(
        response=lambda duration, _: AsrTranscript(
            "confirmed", (AsrTimestamp(0, duration, "confirmed"),), "segment"
        )
    )
    service = h.service()
    service.deadline = monotonic() + 0.03

    async def cache(operation, *args, **kwargs):
        if operation.__name__ == "save_success":
            await asyncio.Event().wait()
        return await direct(operation, *args, **kwargs)

    service.run_asr_cache = cache
    page = await service.read(
        Source(), info(180), {}, BUDGET, memory_domain_id="default"
    )
    assert page.payload["complete"] is True and not page.is_error
    assert page.payload["entries"][0]["text"] == "confirmed"
    assert len(h.requests) == 1 and not h.cache.blocks
    assert "cache persistence could not be confirmed" in page.payload["diagnostics"][0]


@run_async
async def test_real_sdk_upload_deadline_reports_observed_send_and_input_accounting():
    from aiohttp import web
    from pulsara_agent.media.asr import AsrClient
    from pulsara_agent.media.asr_config import AsrConnection

    release = asyncio.Event()
    entered = asyncio.Event()

    async def upload(request):
        await request.read()
        entered.set()
        await release.wait()
        return web.json_response({"text": ""})

    app = web.Application()
    app.router.add_post("/transcriptions", upload)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    h = Harness()
    service = h.service()
    service.asr_config = AsrConnection(
        f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/transcriptions",
        "test",
        "fixture-secret",
    )
    service.prepare_transcribe = AsrClient().prepare_and_transcribe
    service.deadline = monotonic() + 0.5
    try:
        page = await service.read(
            Source(), info(180), {}, BUDGET, memory_domain_id="default"
        )
        assert entered.is_set()
        error = page.payload["errors"][0]
        assert error["remote_result_unknown"] is True
        attempt = error["request_accounting"]["attempts"][0]
        assert (
            attempt["audio_bytes"] == len(b"audio") and attempt["audio_seconds"] == 180
        )
        assert (
            attempt["send_started"]
            and attempt["body_started"]
            and attempt["body_completed"]
        )
        assert attempt["multipart_bytes"] > attempt["audio_bytes"]
        assert attempt["charge_info"] is None
        assert not h.cache.blocks
    finally:
        release.set()
        await runner.cleanup()


@run_async
async def test_real_sdk_validated_response_survives_deadline_during_client_close(
    monkeypatch,
):
    import httpx2
    import pulsara_agent.media.asr as asr_module
    from pulsara_agent.media.asr import AsrClient
    from pulsara_agent.media.asr_config import AsrConnection
    from tests.test_media_asr import install_transport

    async def handler(_request):
        return httpx2.Response(
            200,
            json={"text": "known", "words": [{"start": 0, "end": 1, "word": "known"}]},
        )

    install_transport(monkeypatch, handler)
    original = asr_module.build_async_openai_client
    closing = asyncio.Event()
    release = asyncio.Event()

    def client(**kwargs):
        value = original(**kwargs)
        close = value.close

        async def blocked_close():
            closing.set()
            await release.wait()
            await close()

        value.close = blocked_close
        return value

    monkeypatch.setattr(asr_module, "build_async_openai_client", client)
    h = Harness()
    service = h.service()
    service.asr_config = AsrConnection(
        "https://asr.test/transcriptions", "test", "fixture-secret"
    )
    service.prepare_transcribe = AsrClient().prepare_and_transcribe
    service.deadline = monotonic() + 0.1
    task = asyncio.create_task(
        service.read(Source(), info(180), {}, BUDGET, memory_domain_id="default")
    )
    await closing.wait()
    await asyncio.sleep(0.12)
    release.set()
    page = await task
    assert page.payload["complete"] is True
    assert page.payload["entries"][0]["text"] == "known"
    assert not page.is_error and not h.cache.blocks


@run_async
async def test_confirmed_not_sent_retry_attempts_are_separate_from_audio_effective_range(
    monkeypatch,
):
    import httpx2
    from pulsara_agent.media.asr import AsrClient, AsrRequestObservation
    from pulsara_agent.media.asr_config import AsrConnection
    from tests.test_media_asr import install_transport
    import pulsara_agent.llm.retry as retry

    calls = []

    async def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx2.ConnectError("connection not established", request=request)
        return httpx2.Response(
            200, json={"text": "ok", "words": [{"start": 0, "end": 1, "word": "ok"}]}
        )

    install_transport(monkeypatch, handler)
    monkeypatch.setattr(retry, "compute_retry_delay", lambda **_kwargs: 0)
    observation = AsrRequestObservation()
    result = await AsrClient().transcribe_audio(
        AsrConnection("https://asr.test/transcriptions", "test", "fixture-secret"),
        b"RIFF input",
        duration=2,
        maximum_upload_bytes=4096,
        maximum_response_bytes=4096,
        deadline_monotonic=monotonic() + 3,
        observation=observation,
    )
    assert result.text == "ok" and len(observation.attempts) == 2
    attempts = observation.public_value()["attempts"]
    assert attempts[0]["confirmed_not_sent"] is True
    assert (
        attempts[0]["uploaded_audio_bytes"] == 0
        and attempts[0]["uploaded_audio_seconds"] == 0
    )
    assert attempts[1]["response_validated"] is True
    assert [attempt["audio_seconds"] for attempt in attempts] == [2, 2]
    assert all(attempt["charge_info"] is None for attempt in attempts)
