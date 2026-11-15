"""Embedding maintenance checks the exact current title/statement input."""
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic

import psycopg
from psycopg.rows import dict_row
import pytest

from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.identity import random_id
from pulsara_agent.memory.scope import MemoryDomainContext, freeze_memory_read_context_binding
from tests.retrieval_fixtures import connection as retrieval_connection
from tests.support.postgres import verified_postgres_provider

pytestmark = pytest.mark.postgres
VECTOR = (1.0, *([0.0] * 1023))


@pytest.fixture
def context(stage2_migrated_postgres_database, request):
    database = stage2_migrated_postgres_database
    repository = ConversationKernelRepository(verified_postgres_provider(database.runtime_dsn))
    binding = freeze_memory_read_context_binding(
        domain=MemoryDomainContext(request.function.__name__, "transient"),
        host_workspace_id=random_id("workspace"))
    return repository, database, binding


def insert(database, binding, title, statement, *, kind="FACT"):
    fact = random_id("memory")
    with psycopg.connect(database.admin_dsn) as connection:
        connection.execute("""INSERT INTO pulsara_v3.memory_facts
            (id,memory_domain_id,context_id,fact_kind,title,statement)
            VALUES (%s,%s,'ctx:global',%s,%s,%s)""",
            (fact, binding.memory_domain_id, kind, title, statement))
    return fact


def pending(repository, binding, contract):
    return repository.list_unembedded_memory_facts(read_binding=binding, limit=100,
        embedding_contract=contract, deadline_monotonic=monotonic()+30)


def install(repository, binding, contract, fact, digest):
    return repository.upsert_memory_embedding(read_binding=binding, fact_id=fact,
        fact_semantic_digest=digest, vector=VECTOR, embedded_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic()+30, embedding_contract=contract)


def test_complete_title_statement_input_and_empty_preference_statement(context):
    repository, database, binding = context
    first = insert(database, binding, "第一标题", "\t相同正文\r\n  内部缩进\r\n")
    second = insert(database, binding, "第二标题", "\t相同正文\r\n  内部缩进\r\n")
    preference = insert(database, binding, "先给结论", "this must be normalized away", kind="RESPONSE_PREFERENCE")
    contract = retrieval_connection("embedding").embedding_contract
    rows = {row[0]: row[1:] for row in pending(repository, binding, contract)}
    assert rows[first][1] == "title: 第一标题\nstatement: 相同正文\n  内部缩进"
    assert rows[second][1] == "title: 第二标题\nstatement: 相同正文\n  内部缩进"
    assert rows[first][0] != rows[second][0]
    assert rows[preference][1] == "title: 先给结论\nstatement: "
    assert rows[first][0] == "sha256:" + sha256(rows[first][1].encode()).hexdigest()
    assert install(repository, binding, contract, first, rows[first][0])
    assert first not in {row[0] for row in pending(repository, binding, contract)}
    assert {second, preference} == {row[0] for row in pending(repository, binding, contract)}


def test_changed_statement_rejects_late_vector_and_schedules_current_input(context):
    repository, database, binding = context
    fact = insert(database, binding, "项目数据库", "PostgreSQL")
    contract = retrieval_connection("embedding").embedding_contract
    _, old_digest, old_body = pending(repository, binding, contract)[0]
    assert install(repository, binding, contract, fact, old_digest)
    with psycopg.connect(database.admin_dsn) as connection:
        connection.execute("UPDATE pulsara_v3.memory_facts SET statement='SQLite' WHERE id=%s", (fact,))
    assert not install(repository, binding, contract, fact, old_digest)
    current = pending(repository, binding, contract)
    assert len(current) == 1 and current[0][0] == fact
    _, digest, body = current[0]
    assert digest != old_digest and body == "title: 项目数据库\nstatement: SQLite" and body != old_body
    assert install(repository, binding, contract, fact, digest)
    assert pending(repository, binding, contract) == ()
    with psycopg.connect(database.admin_dsn, row_factory=dict_row) as connection:
        row = connection.execute("SELECT fact_semantic_digest FROM pulsara_v3.memory_embeddings WHERE fact_id=%s", (fact,)).fetchone()
    assert row["fact_semantic_digest"] == digest


def test_deleted_memory_cannot_be_resurrected_by_late_vector(context):
    repository, database, binding = context
    fact = insert(database, binding, "暂存事实", "将删除")
    contract = retrieval_connection("embedding").embedding_contract
    _, digest, _ = pending(repository, binding, contract)[0]
    assert install(repository, binding, contract, fact, digest)
    with psycopg.connect(database.admin_dsn) as connection:
        connection.execute("DELETE FROM pulsara_v3.memory_facts WHERE id=%s", (fact,))
    assert not install(repository, binding, contract, fact, digest)
    assert pending(repository, binding, contract) == ()
    with psycopg.connect(database.admin_dsn) as connection:
        assert connection.execute("SELECT 1 FROM pulsara_v3.memory_embeddings WHERE fact_id=%s", (fact,)).fetchone() is None


def test_embedding_connection_change_replaces_only_derived_vector(context):
    repository, database, binding = context
    fact = insert(database, binding, "连接切换", "同一正文")
    old = retrieval_connection("embedding", model="first").embedding_contract
    new = retrieval_connection("embedding", model="second").embedding_contract
    _, digest, body = pending(repository, binding, old)[0]
    assert install(repository, binding, old, fact, digest)
    assert pending(repository, binding, old) == ()
    assert pending(repository, binding, new) == ((fact, digest, body),)
    assert install(repository, binding, new, fact, digest)
    assert pending(repository, binding, new) == ()
    with psycopg.connect(database.admin_dsn) as connection:
        assert connection.execute("SELECT count(*) FROM pulsara_v3.memory_embeddings WHERE fact_id=%s", (fact,)).fetchone() == (1,)
