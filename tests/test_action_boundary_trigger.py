from __future__ import annotations

from tests.support.postgres import verified_postgres_provider

from uuid import uuid4

from tests.support.postgres import connect_postgres_test_database as _connect_or_skip

from pulsara_agent.entities.memory import ActionBoundary
from pulsara_agent.graph import InMemoryGraphStore, PostgresGraphStore
from pulsara_agent.jsonld import utc_now
from pulsara_agent.memory import InMemoryArchiveStore, PostgresMemoryQuery
from pulsara_agent.memory.canonical.index_sync import MemorySearchIndexSync
from pulsara_agent.memory.canonical.ledger import ExecutionEvidenceLedger
from pulsara_agent.memory.canonical.write_gate import MemoryWriteGate
from pulsara_agent.memory.canonical.write_service import MemoryWriteService
from pulsara_agent.ontology import memory
from pulsara_agent.memory.candidates.proposal_sink import MemoryProposalSink
from pulsara_agent.settings import StorageConfig
from pulsara_agent.tools.base import ToolCall
from pulsara_agent.tools.builtins.memory import RememberActionBoundaryTool


def test_remember_action_boundary_preserves_structured_trigger_fields() -> None:
    sink = MemoryProposalSink()
    tool = RememberActionBoundaryTool(sink=sink)

    result = tool.execute(
        ToolCall(
            id="call:boundary",
            name="remember_action_boundary",
            arguments={
                "statement": "Run tests through uv.",
                "scope": "ctx:workspace/test_project",
                "applies_when": "working on this repository",
                "do_not_apply_when": "the user asks not to run tests",
                "trigger_tools": ["terminal"],
                "trigger_actions": ["test"],
                "trigger_file_globs": ["tests/**/*.py"],
                "trigger_keywords": ["pytest", "uv"],
                "source_authority": "explicit_user_instruction",
                "verification_status": "user_confirmed",
            },
        )
    )

    proposal = sink.drain_valid()[0]
    candidate = proposal.payload.candidate
    assert result.status.value == "success"
    assert candidate.trigger_tools == ("terminal",)
    assert candidate.trigger_actions == ("test",)
    assert candidate.trigger_file_globs == ("tests/**/*.py",)
    assert candidate.trigger_keywords == ("pytest", "uv")


def test_action_boundary_triggers_are_written_to_jsonld() -> None:
    graph = InMemoryGraphStore()
    service = MemoryWriteService(
        ledger=ExecutionEvidenceLedger(
            graph=graph,
            archive=InMemoryArchiveStore(),
            gate=MemoryWriteGate(),
        )
    )

    service.ledger.submit_action_boundary(
        statement="Run tests through uv.",
        scope="ctx:workspace/test_project",
        applies_when="working on this repository",
        do_not_apply_when="the user asks not to run tests",
        trigger_tools=["terminal"],
        trigger_keywords=["pytest", "uv"],
        source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
        verification_status=memory.VerificationStatus.USER_CONFIRMED,
    )

    [document] = graph.find_by_type(memory.ACTION_BOUNDARY)
    assert document[memory.TRIGGER_TOOLS.name] == ["terminal"]
    assert document[memory.TRIGGER_KEYWORDS.name] == ["pytest", "uv"]


def test_action_boundary_structured_triggers_are_indexed_for_fts() -> None:
    dsn = StorageConfig.from_env().postgres_dsn
    _connect_or_skip(dsn).close()
    graph_id = f"graph:test/{uuid4().hex}"
    store = PostgresGraphStore(connection_provider=verified_postgres_provider(dsn))
    query = PostgresMemoryQuery(connection_provider=verified_postgres_provider(dsn))
    try:
        now = utc_now()
        store.put_jsonld(
            ActionBoundary(
                id="action-boundary:trigger-pytest",
                statement="Run tests through uv.",
                scope="ctx:workspace/test_project",
                status=memory.NodeStatus.ACTIVE,
                applies_when="working on this repository",
                do_not_apply_when="the user asks not to run tests",
                source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                confidence_level=memory.ConfidenceLevel.HIGH,
                verification_status=memory.VerificationStatus.USER_CONFIRMED,
                created_at=now,
                updated_at=now,
                gate_reason="test",
                trigger_tools=("terminal",),
                trigger_keywords=("pytest", "uv"),
            ).to_jsonld(),
            graph_id=graph_id,
        )

        assert MemorySearchIndexSync(
            connection_provider=verified_postgres_provider(dsn)
        ).sync_memory(
            "action-boundary:trigger-pytest",
            graph_id=graph_id,
        )

        hits = query.fts_candidates(
            query_text="pytest",
            scopes=["ctx:workspace/test_project"],
            types=["ActionBoundary"],
            limit=5,
            graph_id=graph_id,
        )
        assert hits
        assert hits[0][0] == "action-boundary:trigger-pytest"
        with _connect_or_skip(dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT aliases
                    FROM memory_search_index
                    WHERE graph_id = %s AND memory_id = %s
                    """,
                    (graph_id, "action-boundary:trigger-pytest"),
                )
                assert cursor.fetchone()[0] == ["terminal", "pytest", "uv"]
    finally:
        store.delete_graph(graph_id)
