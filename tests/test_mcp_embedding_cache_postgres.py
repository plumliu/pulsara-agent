"""MCP embedding cache contracts at the verified PostgreSQL boundary."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from threading import Barrier
from time import monotonic

import psycopg
import pytest

from pulsara_agent.capability.mcp_embedding_cache import (
    PostgresMcpToolEmbeddingCache,
    mcp_tool_embedding_cache_key,
)
from pulsara_agent.retrieval.config import RetrievalConnection
from pulsara_agent.retrieval.embedding.validation import MEMORY_EMBEDDING_DIMENSIONS
from pulsara_agent.storage.migrations.errors import (
    PostgresSchemaError,
    PostgresSchemaFailureCode,
)
from pulsara_agent.storage.schema_verification_service import (
    acquire_verified_postgres_access_sync,
)


def _deadline():
    return monotonic() + 30


def _vector(x, y):
    return (x, y, *(0.0,) * (MEMORY_EMBEDDING_DIMENSIONS - 2))


def test_key_uses_full_canonical_encoding_input():
    connection = RetrievalConnection(
        "https://EXAMPLE.invalid/v1/embeddings?space=x",
        "test-model",
        "openai_embedding",
        api_key="test-credential",
    )
    body = "find_pages\n查询页面"
    expected = sha256(
        '["https://example.invalid/v1/embeddings?space=x","test-model",'
        '"openai_embedding",1024,"find_pages\\n查询页面"]'.encode()
    ).digest()
    assert mcp_tool_embedding_cache_key(connection, body) == expected
    assert (
        mcp_tool_embedding_cache_key(replace(connection, api_key="rotated"), body)
        == expected
    )
    for changed in (
        replace(connection, endpoint="https://example.invalid/v1/embeddings?space=y"),
        replace(connection, model_id="different-model"),
    ):
        assert mcp_tool_embedding_cache_key(changed, body) != expected
    assert mcp_tool_embedding_cache_key(connection, body + " ") != expected


@pytest.mark.postgres
def test_reopened_cache_ranks_only_visible_hits(isolated_migrated_postgres_database):
    dsn = isolated_migrated_postgres_database.runtime_dsn
    hidden, first, second, low, missing = (bytes([n]) * 32 for n in range(5))
    with acquire_verified_postgres_access_sync(
        dsn, deadline_monotonic=_deadline()
    ) as access:
        cache = PostgresMcpToolEmbeddingCache(access.connection_provider)
        assert (
            cache.lookup([first, missing], deadline_monotonic=_deadline())
            == frozenset()
        )
        cache.insert(
            {
                hidden: _vector(1, 0),
                first: _vector(1, 1),
                second: _vector(1, -1),
                low: _vector(0, 1),
            },
            deadline_monotonic=_deadline(),
        )
        cache.insert({low: _vector(1, 0)}, deadline_monotonic=_deadline())

    with acquire_verified_postgres_access_sync(
        dsn, deadline_monotonic=_deadline()
    ) as access:
        cache = PostgresMcpToolEmbeddingCache(access.connection_provider)
        visible = [missing, second, low, first, second]
        assert cache.lookup(visible, deadline_monotonic=_deadline()) == {
            first,
            second,
            low,
        }
        ranked = cache.rank(visible, _vector(1, 0), deadline_monotonic=_deadline())
        assert list(ranked) == [second, first, low]
        assert ranked == pytest.approx({second: 2**-0.5, first: 2**-0.5, low: 0.0})
        assert (
            cache.rank([missing], _vector(1, 0), deadline_monotonic=_deadline()) == {}
        )


@pytest.mark.postgres
def test_rank_preserves_cosine_for_extreme_provider_vector_magnitudes(
    isolated_migrated_postgres_database,
):
    dsn = isolated_migrated_postgres_database.runtime_dsn
    tiny, huge = (bytes([n]) * 32 for n in range(2))
    with acquire_verified_postgres_access_sync(
        dsn, deadline_monotonic=_deadline()
    ) as access:
        cache = PostgresMcpToolEmbeddingCache(access.connection_provider)
        cache.insert(
            {tiny: _vector(1e-50, 0), huge: _vector(1e50, 1e50)},
            deadline_monotonic=_deadline(),
        )
        visible = [tiny, huge]
        for magnitude in (1e-50, 1e50):
            assert cache.rank(
                visible, _vector(magnitude, 0), deadline_monotonic=_deadline()
            ) == pytest.approx({tiny: 1.0, huge: 2**-0.5})


@pytest.mark.postgres
def test_concurrent_batches_converge_and_failed_batch_is_atomic(
    isolated_migrated_postgres_database,
):
    dsn = isolated_migrated_postgres_database.runtime_dsn
    left, right, uncommitted = (bytes([n]) * 32 for n in range(3))
    with acquire_verified_postgres_access_sync(
        dsn, deadline_monotonic=_deadline()
    ) as access:
        cache = PostgresMcpToolEmbeddingCache(access.connection_provider)
        barrier = Barrier(2)

        def insert(keys):
            barrier.wait(timeout=5)
            cache.insert(
                dict.fromkeys(keys, _vector(1, 0)), deadline_monotonic=_deadline()
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            list(workers.map(insert, [(left, right), (right, left)]))
        assert cache.lookup([left, right], deadline_monotonic=_deadline()) == {
            left,
            right,
        }
        with pytest.raises(psycopg.errors.CheckViolation):
            cache.insert(
                {uncommitted: _vector(1, 0), b"wrong length": _vector(1, 0)},
                deadline_monotonic=_deadline(),
            )
        assert (
            cache.lookup([uncommitted], deadline_monotonic=_deadline()) == frozenset()
        )


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("ddl", "code"),
    [
        (
            "ALTER TABLE pulsara_v3.mcp_tool_embeddings ALTER embedding TYPE public.vector(512)",
            PostgresSchemaFailureCode.CATALOG_DRIFT,
        ),
        (
            "GRANT UPDATE ON pulsara_v3.mcp_tool_embeddings TO PUBLIC",
            PostgresSchemaFailureCode.PRIVILEGE_MISSING,
        ),
    ],
)
def test_verifier_rejects_cache_schema_or_grant_drift(
    isolated_migrated_postgres_database,
    ddl,
    code,
):
    db = isolated_migrated_postgres_database
    with psycopg.connect(db.admin_dsn) as admin:
        admin.execute(ddl)
    with pytest.raises(PostgresSchemaError) as exc:
        acquire_verified_postgres_access_sync(
            db.runtime_dsn, deadline_monotonic=_deadline()
        )
    assert exc.value.code is code
