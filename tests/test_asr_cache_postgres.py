"""Real clean-v0 PostgreSQL success-block and whole-media retention contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from time import monotonic

import pytest

from pulsara_agent.identity import is_valid_id
from pulsara_agent.media.asr_cache import AsrMediaKey, PostgresAsrCache, asr_cache_id
from tests.support.postgres import (
    guarded_postgres_test_connection,
    verified_postgres_provider,
)

pytestmark = pytest.mark.postgres


def deadline():
    return monotonic() + 30


def payload(start, end, text="hello"):
    return {
        "text": text,
        "granularity": "segment" if text else None,
        "items": [{"start": start, "end": end, "text": text, "speaker": None}]
        if text
        else [],
        "request_range": {"from": start, "to": end},
        "sound": None,
        "diagnostics": [],
    }


def save(owner, key, start, end, text="hello", model="A"):
    return owner.save_success(
        key,
        start,
        end,
        {"endpoint": "https://example.invalid/transcriptions", "model": model},
        payload(start, end, text),
        deadline_monotonic=deadline(),
    )


def query(owner, key, start=0, end=600):
    return owner.query(key, start, end, deadline_monotonic=deadline())


@pytest.fixture
def cache(isolated_migrated_postgres_database):
    db = isolated_migrated_postgres_database
    return PostgresAsrCache(verified_postgres_provider(db.runtime_dsn)), db


def test_atomic_success_empty_duplicate_source_and_domain_isolation(cache):
    owner, db = cache
    key = AsrMediaKey("u_cache", "/tmp/sample.mp4", 10, 100)
    first = save(owner, key, 180, 240)
    empty = save(owner, key, 240, 300, "")
    third = save(owner, key, 300, 360)
    duplicate = save(owner, key, 180.0, 240.0, "replacement", "B")
    assert duplicate.id == first.id == asr_cache_id(key, 180, 240)
    assert is_valid_id(first.id, "asr-cache")
    assert (
        duplicate.payload == first.payload and duplicate.provenance == first.provenance
    )
    assert [x.id for x in query(owner, key, 220, 280)] == [first.id, empty.id]
    assert [x.id for x in query(owner, key, 240, 300)] == [empty.id]
    assert [x.id for x in query(owner, key, 330, 390)] == [third.id]
    for changed in (
        replace(key, memory_domain_id="u_other"),
        replace(key, source_modified_ns=11),
        replace(key, source_size=101),
        replace(key, source_path="/tmp/renamed.mp4"),
    ):
        assert query(owner, changed) == ()
    assert len(query(owner, key)) == 3
    with guarded_postgres_test_connection(db.runtime_dsn) as c:
        rows = c.execute(
            "SELECT count(*),count(payload),count(provenance) FROM pulsara_v3.asr_cache"
        ).fetchone()
    assert rows == (3, 3, 3)


def test_invalid_payload_never_publishes_coverage(cache):
    owner, _ = cache
    key = AsrMediaKey("u_cache", "/tmp/audio.flac", 20, 200)
    for invalid in (
        payload(1, 5),
        {
            **payload(0, 10),
            "items": [{"start": True, "end": 10, "text": "bad", "speaker": None}],
        },
        {
            **payload(0, 10),
            "items": [
                {"start": 0, "end": float("inf"), "text": "bad", "speaker": None}
            ],
        },
        {**payload(0, 10), "items": []},
        {**payload(0, 10), "diagnostics": [False]},
    ):
        with pytest.raises(ValueError):
            owner.save_success(key, 0, 10, {}, invalid, deadline_monotonic=deadline())
    assert query(owner, key) == ()
    with pytest.raises(ValueError):
        save(owner, key, float("nan"), 10)
    assert query(owner, key) == ()


def test_partial_success_independent_from_failed_other_block(cache):
    owner, _ = cache
    key = AsrMediaKey("u_cache", "/tmp/partial.wav", 30, 300)
    first = save(owner, key, 0, 60)
    with pytest.raises(ValueError):
        owner.save_success(
            key, 60, 120, {}, payload(61, 119), deadline_monotonic=deadline()
        )
    last = save(owner, key, 120, 180)
    assert [x.id for x in query(owner, key)] == [first.id, last.id]


def test_cleanup_whole_media_recent_touch_source_versions_and_domains(cache):
    owner, db = cache
    old = AsrMediaKey("u_cache", "/tmp/old.mp4", 1, 1)
    recent = replace(old, source_path="/tmp/recent.mp4")
    other_domain = replace(old, memory_domain_id="u_other")
    other_version = replace(recent, source_modified_ns=2)
    for key in (old, recent, other_domain, other_version):
        save(owner, key, 0, 60)
        save(owner, key, 60, 120)
    with guarded_postgres_test_connection(db.runtime_dsn) as c:
        c.execute(
            "UPDATE pulsara_v3.asr_cache SET last_used_at=statement_timestamp()-interval '3 days'"
        )
    assert owner.touch(recent, deadline_monotonic=deadline()) == 2
    # Touching a range is deliberately unavailable: a real read protects every block.
    assert (
        owner.touch(
            replace(old, source_path="/tmp/absent.wav"), deadline_monotonic=deadline()
        )
        == 0
    )
    assert owner.clear_old("u_cache", deadline_monotonic=deadline()) == 4
    assert len(query(owner, recent)) == 2
    assert query(owner, old) == query(owner, other_version) == ()
    assert len(query(owner, other_domain)) == 2
    assert owner.clear_old("u_cache", deadline_monotonic=deadline()) == 0
    # An in-flight recognition may naturally refill a cleaned version.
    save(owner, old, 0, 60)
    assert owner.clear_old("u_cache", deadline_monotonic=deadline()) == 0


def test_new_block_protects_all_old_blocks_and_duplicate_keeps_id(cache):
    owner, db = cache
    key = AsrMediaKey("u_cache", "/tmp/new-audio.mp3", 1, 1)
    first = save(owner, key, 0, 60)
    with guarded_postgres_test_connection(db.runtime_dsn) as c:
        c.execute(
            "UPDATE pulsara_v3.asr_cache SET last_used_at=statement_timestamp()-interval '3 days'"
        )
    second = save(owner, key, 60, 120)
    assert owner.clear_old("u_cache", deadline_monotonic=deadline()) == 0
    assert [x.id for x in query(owner, key)] == [first.id, second.id]
    with ThreadPoolExecutor(max_workers=2) as threads:
        results = list(threads.map(lambda _: save(owner, key, 120, 180), range(2)))
    assert results[0].id == results[1].id
    assert len(query(owner, key)) == 3


def test_database_read_failure_is_not_a_cache_miss_and_write_failure_not_success():
    from tests.support.postgres import UnverifiedTestPostgresConnectionProvider

    owner = PostgresAsrCache(UnverifiedTestPostgresConnectionProvider())
    key = AsrMediaKey("u_cache", "/tmp/fail.mp4", 1, 1)
    assert asr_cache_id(key, -0.0, 10) == asr_cache_id(key, 0, 10.0)
    with pytest.raises(AssertionError, match="unverified"):
        query(owner, key)
    with pytest.raises(AssertionError, match="unverified"):
        save(owner, key, 0, 10)


def test_cleanup_cutoff_is_strict_at_one_server_instant(cache, monkeypatch):
    from contextlib import contextmanager
    from datetime import timedelta

    owner, db = cache
    keys = [AsrMediaKey("u_cache", f"/tmp/cutoff-{i}.wav", 1, 1) for i in range(3)]
    for key in keys:
        save(owner, key, 0, 1)
    with guarded_postgres_test_connection(db.runtime_dsn) as connection:
        now = connection.execute("SELECT statement_timestamp()").fetchone()[0]
        for key, delta in zip(keys, (-1, 0, 1), strict=True):
            connection.execute(
                "UPDATE pulsara_v3.asr_cache SET last_used_at=%s WHERE source_path=%s",
                (
                    now - timedelta(hours=24) + timedelta(microseconds=delta),
                    key.source_path,
                ),
            )
    original_connection = owner._connection

    class FrozenServerTimeConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, statement, parameters=None):
            if statement.startswith("WITH old_media AS"):
                statement = statement.replace(
                    "statement_timestamp()", "%s::timestamptz"
                )
                parameters = (*parameters, now)
            return self.connection.execute(statement, parameters)

    @contextmanager
    def frozen_connection(deadline_monotonic):
        with original_connection(deadline_monotonic) as connection:
            yield FrozenServerTimeConnection(connection)

    monkeypatch.setattr(owner, "_connection", frozen_connection)
    assert owner.clear_old("u_cache", deadline_monotonic=deadline()) == 1
    assert query(owner, keys[0]) == ()
    assert len(query(owner, keys[1])) == len(query(owner, keys[2])) == 1


def test_refresh_waiting_before_cleanup_preserves_whole_media(cache):
    from threading import Event

    owner, db = cache
    key = AsrMediaKey("u_cache", "/tmp/concurrent.wav", 1, 1)
    save(owner, key, 0, 60)
    save(owner, key, 60, 120)
    with guarded_postgres_test_connection(db.runtime_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.asr_cache SET last_used_at=statement_timestamp()-interval '3 days'"
        )
    started = Event()

    def touch():
        with owner._lock:
            started.set()
            return owner.touch(key, deadline_monotonic=deadline())

    with ThreadPoolExecutor(max_workers=2) as threads:
        # Hold the real table lock so refresh is in flight when cleanup arrives.
        with guarded_postgres_test_connection(db.runtime_dsn) as connection:
            connection.execute(
                "LOCK TABLE pulsara_v3.asr_cache IN SHARE ROW EXCLUSIVE MODE"
            )
            refreshed = threads.submit(touch)
            assert started.wait(10)
            cleaned = threads.submit(
                owner.clear_old, "u_cache", deadline_monotonic=deadline()
            )
        assert refreshed.result(timeout=30) == 2
        assert cleaned.result(timeout=30) == 0
    assert len(query(owner, key)) == 2


def test_long_media_coverage_reads_use_native_media_range_index(cache):
    import psycopg
    from psycopg.types.json import Jsonb

    owner, db = cache
    key = AsrMediaKey("u_cache", "/tmp/long.wav", 1, 1)
    # 5,000 minute blocks represent an 83-hour source. Payloads are not read by
    # the coverage plan; the native GiST interval index avoids scanning the full source.
    with guarded_postgres_test_connection(db.runtime_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO pulsara_v3.asr_cache (id,memory_domain_id,source_path,source_modified_ns,source_size,from_seconds,to_seconds,provenance,payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    (
                        asr_cache_id(key, i * 60, (i + 1) * 60),
                        *key.values(),
                        i * 60,
                        (i + 1) * 60,
                        Jsonb({}),
                        Jsonb(payload(i * 60, (i + 1) * 60)),
                    )
                    for i in range(5000)
                ),
            )
    with psycopg.connect(db.admin_dsn, autocommit=True) as connection:
        connection.execute("ANALYZE pulsara_v3.asr_cache")
    ranges = owner.query_ranges(key, 150000, 150120, deadline_monotonic=deadline())
    assert [(item.from_seconds, item.to_seconds) for item in ranges] == [
        (150000, 150060),
        (150060, 150120),
    ]
    block = owner.query_block(key, ranges[0].id, deadline_monotonic=deadline())
    assert block is not None and block.payload["text"] == "hello"
    assert (
        owner.query_block(
            replace(key, memory_domain_id="u_other"),
            ranges[0].id,
            deadline_monotonic=deadline(),
        )
        is None
    )
    with guarded_postgres_test_connection(db.runtime_dsn) as connection:
        plan = connection.execute(
            "EXPLAIN (ANALYZE, FORMAT JSON) SELECT id,from_seconds,to_seconds,(payload #>> '{request_range,from}')::double precision AS request_from_seconds,(payload #>> '{request_range,to}')::double precision AS request_to_seconds FROM pulsara_v3.asr_cache WHERE memory_domain_id=%s AND source_path=%s AND source_modified_ns=%s AND source_size=%s AND from_seconds < %s AND to_seconds > %s AND box(point(from_seconds,0),point(to_seconds,0)) && box(point(%s,0),point(%s,0)) ORDER BY from_seconds,to_seconds,id",
            (*key.values(), 150120, 150000, 150000, 150120),
        ).fetchone()[0]
    assert "idx_asr_cache_interval" in str(plan)


def test_core_cleanup_and_touch_need_no_active_session_and_use_user_domain(cache):
    import asyncio
    from pulsara_agent.conversation_kernel.host import KernelHostCore
    from tests.support.model_config import test_model_runtime

    owner, db = cache
    local = AsrMediaKey("u_cache", "/tmp/user-owner.wav", 1, 1)
    foreign = replace(local, memory_domain_id="u_other")
    save(owner, local, 0, 60)
    save(owner, foreign, 0, 60)
    with guarded_postgres_test_connection(db.runtime_dsn) as connection:
        connection.execute(
            "UPDATE pulsara_v3.asr_cache SET last_used_at=statement_timestamp()-interval '3 days'"
        )

    async def run():
        core = KernelHostCore(
            model_runtime=test_model_runtime(postgres_dsn=db.runtime_dsn),
            memory_domain_id="u_cache",
        )
        try:
            assert core._sessions == {}
            await core.touch_asr_cache_source(
                local.source_path, local.source_modified_ns, local.source_size
            )
            assert (await core.clear_old_asr_cache())["deleted_blocks"] == 0
            with guarded_postgres_test_connection(db.runtime_dsn) as connection:
                connection.execute(
                    "UPDATE pulsara_v3.asr_cache SET last_used_at=statement_timestamp()-interval '3 days'"
                )
            result = await core.clear_old_asr_cache()
            assert result["deleted_blocks"] == 1
            assert (await core.clear_old_asr_cache())[
                "message"
            ] == "暂无可清理的旧转写缓存"
            assert core._sessions == {}
        finally:
            await core.shutdown()

    asyncio.run(run())
    assert query(owner, local) == ()
    assert len(query(owner, foreign)) == 1


def test_range_metadata_retains_actual_context_bounds_without_returning_text(cache):
    owner, _ = cache
    key = AsrMediaKey("u_cache", "/tmp/context.wav", 1, 1)
    complete = payload(5, 25, "boundary sentence")
    complete["items"] = [
        {"start": 7, "end": 12, "text": "boundary sentence", "speaker": None}
    ]
    block = owner.save_success(key, 10, 20, {}, complete, deadline_monotonic=deadline())
    ranges = owner.query_ranges(key, 10, 20, deadline_monotonic=deadline())
    assert len(ranges) == 1
    assert (
        ranges[0].id,
        ranges[0].from_seconds,
        ranges[0].to_seconds,
        ranges[0].request_from_seconds,
        ranges[0].request_to_seconds,
    ) == (block.id, 10, 20, 5, 25)
    assert not hasattr(ranges[0], "payload")
