from __future__ import annotations

from uuid import uuid4

from tests.support.postgres import connect_postgres_test_database

from pulsara_agent.settings import StorageConfig


def test_pgvector_schema_supports_hnsw_compound_key_and_cascade() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    graph_id = f"graph:test/vector-schema/{uuid4().hex}"
    memory_id = f"preference:{uuid4().hex}"
    with connect_postgres_test_database(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT extversion FROM pg_extension WHERE extname = 'vector'
                """
            )
            assert cursor.fetchone() is not None
            cursor.execute(
                """
                INSERT INTO memory_nodes (
                    graph_id, id, memory_type, scope, status, statement,
                    created_at, updated_at
                ) VALUES (%s, %s, 'Preference', 'ctx:user', 'active', 'vector test', now(), now())
                """,
                (graph_id, memory_id),
            )
            vector_literal = "[" + ",".join(["0"] * 1023 + ["1"]) + "]"
            cursor.execute(
                """
                INSERT INTO memory_vector_index (
                    graph_id, memory_id, embedding_fingerprint,
                    embedded_text_hash, builder_version, embedding
                ) VALUES (%s, %s, 'test:model:1024', 'hash:v1', 'builder:v1', %s::vector)
                """,
                (graph_id, memory_id, vector_literal),
            )
            cursor.execute(
                """
                SELECT indexdef FROM pg_indexes
                WHERE tablename = 'memory_vector_index' AND indexname = 'idx_mvi_embedding_hnsw'
                """
            )
            assert "USING hnsw" in cursor.fetchone()[0]
            cursor.execute(
                "DELETE FROM memory_nodes WHERE graph_id = %s AND id = %s",
                (graph_id, memory_id),
            )
            cursor.execute(
                "SELECT count(*) FROM memory_vector_index WHERE graph_id = %s",
                (graph_id,),
            )
            assert cursor.fetchone() == (0,)
