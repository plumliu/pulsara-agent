from __future__ import annotations

from tests.support.postgres import verified_postgres_provider

from uuid import uuid4

from tests.support.postgres import connect_postgres_test_database as _connect_or_skip

from pulsara_agent.graph.durable_facade import DurableGraphFacade
from pulsara_agent.entities.memory import Preference
from pulsara_agent.entities.runtime import Evidence
from pulsara_agent.graph import PostgresGraphStore
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.memory.canonical.index_sync import MemorySearchIndexSync
from pulsara_agent.memory import PostgresMemoryQuery
from pulsara_agent.ontology import memory, runtime as rt
from pulsara_agent.runtime.projection_jobs.contracts import (
    CanonicalMutationSurface,
)
from pulsara_agent.runtime.projection_jobs.mutation_writer import (
    CanonicalMutationV2Writer,
    build_surface_plan,
)
from pulsara_agent.settings import StorageConfig


def test_postgres_graph_store_projects_memory_nodes_and_runtime_source_relations() -> (
    None
):
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test:{uuid4().hex}"
    store = PostgresGraphStore(connection_provider=verified_postgres_provider(dsn))
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    now = utc_now()

    store.put_jsonld(
        Preference(
            id="preference:test-concise",
            statement="The user prefers concise summaries.",
            scope="ctx:user",
            status=memory.NodeStatus.ACTIVE,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="explicit user preference",
        ).to_jsonld(),
        graph_id=graph_id,
    )
    store.put_jsonld(
        Evidence(
            id="evidence:test-concise",
            statement="User said they prefer concise summaries.",
            source_type=rt.EvidenceSourceType.TOOL_RESULT,
            status=memory.NodeStatus.ACTIVE,
            observed_at=now,
            scope="ctx:user",
            created_from=NodeRef("tool-result:test-concise"),
        ).to_jsonld(),
        graph_id=graph_id,
    )
    evidence_document = store.get_jsonld("evidence:test-concise", graph_id=graph_id)
    evidence_document[memory.SUPPORTS.name] = [{"@id": "preference:test-concise"}]
    store.put_jsonld(evidence_document, graph_id=graph_id)

    fetched = query.fetch_nodes(["preference:test-concise"], graph_id=graph_id)

    assert len(fetched) == 1
    assert fetched[0].id == "preference:test-concise"
    assert fetched[0].evidence_ids == ("evidence:test-concise",)
    assert (memory.SUPPORTS.name, "evidence:test-concise") in fetched[0].incoming
    assert query.lexical_candidates(
        terms=["concise"],
        scopes=["ctx:user"],
        types=["Preference"],
        limit=5,
        graph_id=graph_id,
    ) == [("preference:test-concise", 2.0)]
    MemorySearchIndexSync(
        connection_provider=verified_postgres_provider(dsn)
    ).sync_memory("preference:test-concise", graph_id=graph_id)
    assert (
        query.fts_candidates(
            query_text="concise summaries",
            scopes=["ctx:user"],
            types=["Preference"],
            limit=5,
            graph_id=graph_id,
        )[0][0]
        == "preference:test-concise"
    )

    store.delete_graph(graph_id)
    assert query.fetch_nodes(["preference:test-concise"], graph_id=graph_id) == []


def test_durable_facade_delete_graph_commits_v2_projection_obligation() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    now = utc_now()
    postgres = PostgresGraphStore(connection_provider=verified_postgres_provider(dsn))
    facade = DurableGraphFacade(
        postgres=postgres,
        mutation_writer=CanonicalMutationV2Writer(
            connection_provider=verified_postgres_provider(dsn),
            surface_plan=build_surface_plan((CanonicalMutationSurface.OXIGRAPH,)),
        ),
    )

    facade.put_jsonld(
        Preference(
            id="preference:test-delete-fallback",
            statement="The user prefers concise summaries.",
            scope="ctx:user",
            status=memory.NodeStatus.ACTIVE,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="explicit user preference",
        ).to_jsonld(),
        graph_id=graph_id,
    )

    facade.delete_graph(graph_id)

    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    assert (
        query.fetch_nodes(["preference:test-delete-fallback"], graph_id=graph_id) == []
    )
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select m.mutation_kind, d.status
                from canonical_mutations_v2 as m
                join canonical_mutation_surface_deliveries as d
                  on d.mutation_id = m.mutation_id
                where m.graph_id = %s and m.mutation_kind = 'graph_delete.v2'
                order by m.mutation_sequence_number desc
                limit 1
                """,
                (graph_id,),
            )
            mutation_kind, status = cursor.fetchone()
            assert mutation_kind == "graph_delete.v2"
            assert status == "pending"


def test_durable_facade_delete_graph_without_oxigraph_does_not_emit_graph_reset_tombstone() -> (
    None
):
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    now = utc_now()
    postgres = PostgresGraphStore(connection_provider=verified_postgres_provider(dsn))
    facade = DurableGraphFacade(postgres=postgres)

    facade.put_jsonld(
        Preference(
            id="preference:test-delete-no-oxigraph",
            statement="The user prefers concise summaries.",
            scope="ctx:user",
            status=memory.NodeStatus.ACTIVE,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="explicit user preference",
        ).to_jsonld(),
        graph_id=graph_id,
    )

    facade.delete_graph(graph_id)

    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    assert (
        query.fetch_nodes(["preference:test-delete-no-oxigraph"], graph_id=graph_id)
        == []
    )
    with _connect_or_skip(dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                select count(*)
                from canonical_mutations_v2
                where graph_id = %s and mutation_kind = 'graph_reset.v2'
                """,
                (graph_id,),
            )
            (count,) = cursor.fetchone()
            assert count == 0
