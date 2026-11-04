"""Disposable successful ASR blocks; PostgreSQL owns atomic storage and ordering.

Callers own source/permission checks and ASR response validation. This owner
checks the complete frozen payload again before publishing coverage, and never
starts ASR or binds a cache transaction to a conversation transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import re
from threading import RLock

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from pulsara_agent.identity import derive_id, require_id
from pulsara_agent.storage.postgres_connection_provider import (
    PostgresConnectionLane,
    VerifiedPostgresConnectionProviderProtocol,
)


@dataclass(frozen=True, slots=True)
class AsrMediaKey:
    memory_domain_id: str
    source_path: str
    source_modified_ns: int
    source_size: int

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", self.memory_domain_id):
            raise ValueError("invalid memory access domain")
        if not self.source_path or not Path(self.source_path).is_absolute():
            raise ValueError("ASR cache source must be a verified absolute path")
        if (
            any(
                type(value) is not int
                for value in (self.source_modified_ns, self.source_size)
            )
            or self.source_size < 0
        ):
            raise ValueError("invalid media stat facts")

    def values(self):
        return (
            self.memory_domain_id,
            self.source_path,
            self.source_modified_ns,
            self.source_size,
        )


@dataclass(frozen=True, slots=True)
class AsrCacheBlock:
    id: str
    key: AsrMediaKey
    from_seconds: float
    to_seconds: float
    provenance: dict
    payload: dict
    last_used_at: datetime


@dataclass(frozen=True, slots=True)
class AsrCacheRange:
    id: str
    from_seconds: float
    to_seconds: float
    request_from_seconds: float | None = None
    request_to_seconds: float | None = None


_KEY_WHERE = "memory_domain_id=%s AND source_path=%s AND source_modified_ns=%s AND source_size=%s"
_MUTATION_LOCK = "LOCK TABLE pulsara_v3.asr_cache IN SHARE ROW EXCLUSIVE MODE"


def _range(start, end):
    if (
        type(start) not in (int, float)
        or type(end) not in (int, float)
        or not math.isfinite(start)
        or not math.isfinite(end)
        or start < 0
        or end <= start
    ):
        raise ValueError("invalid ASR effective range")
    # PostgreSQL interval equality treats signed zero as the same boundary.
    # Stable semantic IDs must agree before an existing row resolves a retry.
    return 0.0 if start == 0 else float(start), float(end)


def _validate_payload(payload, start, end):
    expected = {"text", "granularity", "items", "request_range", "sound", "diagnostics"}
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("invalid complete ASR payload")
    if (
        not isinstance(payload["text"], str)
        or payload["granularity"] not in (None, "word", "segment")
        or not isinstance(payload["items"], list)
        or not isinstance(payload["diagnostics"], list)
        or any(not isinstance(item, str) for item in payload["diagnostics"])
    ):
        raise ValueError("invalid ASR transcript payload")
    requested = payload["request_range"]
    if not isinstance(requested, dict) or set(requested) != {"from", "to"}:
        raise ValueError("invalid ASR request range")
    request_from, request_to = _range(requested["from"], requested["to"])
    if request_from > start or request_to < end:
        raise ValueError("ASR request does not cover effective range")
    previous = request_from
    for item in payload["items"]:
        if not isinstance(item, dict) or set(item) != {
            "start",
            "end",
            "text",
            "speaker",
        }:
            raise ValueError("invalid ASR timestamp item")
        item_from, item_to = _range(item["start"], item["end"])
        if (
            item_from < previous
            or item_from < request_from
            or item_to > request_to
            or not isinstance(item["text"], str)
            or (item["speaker"] is not None and not isinstance(item["speaker"], str))
        ):
            raise ValueError("invalid ASR timestamp item")
        previous = item_from
    if payload["text"].strip() and (
        not payload["items"] or payload["granularity"] is None
    ):
        raise ValueError("nonempty ASR text requires actual timestamps")
    if payload["sound"] is not None and not isinstance(payload["sound"], dict):
        raise ValueError("invalid ASR sound observation")
    json.dumps(payload, allow_nan=False)


def _block(row):
    key = AsrMediaKey(
        *(
            row[name]
            for name in (
                "memory_domain_id",
                "source_path",
                "source_modified_ns",
                "source_size",
            )
        )
    )
    return AsrCacheBlock(
        row["id"],
        key,
        row["from_seconds"],
        row["to_seconds"],
        row["provenance"],
        row["payload"],
        row["last_used_at"],
    )


def asr_cache_id(key: AsrMediaKey, from_seconds: float, to_seconds: float) -> str:
    """Allocate at the successful-block boundary, including uncached delivery."""
    start, end = _range(from_seconds, to_seconds)
    return derive_id(
        "asr-cache",
        "successful-media-range",
        *map(str, key.values()),
        start.hex(),
        end.hex(),
    )


class PostgresAsrCache:
    def __init__(self, provider: VerifiedPostgresConnectionProviderProtocol):
        self._provider = provider
        self._lock = RLock()

    def _connection(self, deadline_monotonic):
        return self._provider.connection(
            lane=PostgresConnectionLane.ARTIFACT,
            deadline_monotonic=deadline_monotonic,
            row_factory=dict_row,
        )

    def query(
        self,
        key: AsrMediaKey,
        from_seconds: float,
        to_seconds: float,
        *,
        deadline_monotonic: float,
    ) -> tuple[AsrCacheBlock, ...]:
        start, end = _range(from_seconds, to_seconds)
        with self._connection(deadline_monotonic) as connection:
            rows = connection.execute(
                f"SELECT * FROM pulsara_v3.asr_cache WHERE {_KEY_WHERE} AND from_seconds < %s AND to_seconds > %s AND box(point(from_seconds,0),point(to_seconds,0)) && box(point(%s,0),point(%s,0)) ORDER BY from_seconds,to_seconds,id",
                (*key.values(), end, start, start, end),
            ).fetchall()
        return tuple(_block(row) for row in rows)

    def query_ranges(
        self,
        key: AsrMediaKey,
        from_seconds: float,
        to_seconds: float,
        *,
        deadline_monotonic: float,
    ) -> tuple[AsrCacheRange, ...]:
        """Return coverage/source bounds without returning transcript text."""
        start, end = _range(from_seconds, to_seconds)
        with self._connection(deadline_monotonic) as connection:
            rows = connection.execute(
                f"SELECT id,from_seconds,to_seconds,(payload #>> '{{request_range,from}}')::double precision AS request_from_seconds,(payload #>> '{{request_range,to}}')::double precision AS request_to_seconds FROM pulsara_v3.asr_cache WHERE {_KEY_WHERE} AND from_seconds < %s AND to_seconds > %s AND box(point(from_seconds,0),point(to_seconds,0)) && box(point(%s,0),point(%s,0)) ORDER BY from_seconds,to_seconds,id",
                (*key.values(), end, start, start, end),
            ).fetchall()
        return tuple(
            AsrCacheRange(
                row["id"],
                row["from_seconds"],
                row["to_seconds"],
                row["request_from_seconds"],
                row["request_to_seconds"],
            )
            for row in rows
        )

    def query_block(
        self, key: AsrMediaKey, block_id: str, *, deadline_monotonic: float
    ) -> AsrCacheBlock | None:
        require_id(block_id, "asr-cache")
        with self._connection(deadline_monotonic) as connection:
            row = connection.execute(
                f"SELECT * FROM pulsara_v3.asr_cache WHERE {_KEY_WHERE} AND id=%s",
                (*key.values(), block_id),
            ).fetchone()
        return None if row is None else _block(row)

    def save_success(
        self,
        key: AsrMediaKey,
        from_seconds: float,
        to_seconds: float,
        provenance: dict,
        payload: dict,
        *,
        deadline_monotonic: float,
    ) -> AsrCacheBlock:
        start, end = _range(from_seconds, to_seconds)
        _validate_payload(payload, start, end)
        if not isinstance(provenance, dict):
            raise ValueError("invalid ASR provenance")
        json.dumps(provenance, allow_nan=False)
        block_id = asr_cache_id(key, start, end)
        with self._lock, self._connection(deadline_monotonic) as connection:
            # Short native lock orders refresh/write/cleanup even when another
            # process owns its own cache runtime. No durable coordination rows.
            connection.execute(_MUTATION_LOCK)
            row = connection.execute(
                "INSERT INTO pulsara_v3.asr_cache (id,memory_domain_id,source_path,source_modified_ns,source_size,from_seconds,to_seconds,provenance,payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (memory_domain_id,source_path,source_modified_ns,source_size,from_seconds,to_seconds) DO UPDATE SET last_used_at=statement_timestamp() RETURNING *",
                (
                    block_id,
                    *key.values(),
                    start,
                    end,
                    Jsonb(provenance),
                    Jsonb(payload),
                ),
            ).fetchone()
        return _block(row)

    def touch(self, key: AsrMediaKey, *, deadline_monotonic: float) -> int:
        with self._lock, self._connection(deadline_monotonic) as connection:
            connection.execute(_MUTATION_LOCK)
            result = connection.execute(
                f"UPDATE pulsara_v3.asr_cache SET last_used_at=statement_timestamp() WHERE {_KEY_WHERE}",
                key.values(),
            )
            return result.rowcount

    def clear_old(self, memory_domain_id: str, *, deadline_monotonic: float) -> int:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", memory_domain_id):
            raise ValueError("invalid memory access domain")
        with self._lock, self._connection(deadline_monotonic) as connection:
            connection.execute(_MUTATION_LOCK)
            # One server instant, grouped across all blocks of each version.
            result = connection.execute(
                """WITH old_media AS (
                SELECT memory_domain_id,source_path,source_modified_ns,source_size
                FROM pulsara_v3.asr_cache WHERE memory_domain_id=%s
                GROUP BY memory_domain_id,source_path,source_modified_ns,source_size
                HAVING MAX(last_used_at) < statement_timestamp() - interval '24 hours'
            ) DELETE FROM pulsara_v3.asr_cache AS cache USING old_media AS old
              WHERE cache.memory_domain_id=old.memory_domain_id AND cache.source_path=old.source_path
                AND cache.source_modified_ns=old.source_modified_ns AND cache.source_size=old.source_size""",
                (memory_domain_id,),
            )
            return result.rowcount
