"""Clean-v0 direct memory writes through canonical ToolResult settlement."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from time import monotonic

import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.memory.contracts import (
    MemoryFactKind,
    MemoryRelationKind,
    PreparedMemoryBasisReference,
)
from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementError,
    MemoryManagementSelection,
)
from pulsara_agent.conversation_kernel.memory.recall import PostgresMemoryQuery
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.memory_tools import KernelMemoryToolPort
from pulsara_agent.conversation_kernel.memory.writes import (
    PreparedMemoryRelationWrite,
    PreparedRememberWrite,
)
from pulsara_agent.conversation_kernel.repository import (
    AcceptedMemoryToolResult,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.memory.scope import (
    CTX_GLOBAL,
    MemoryDomainContext,
    freeze_memory_read_context_binding,
    workspace_context_id,
)
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.retrieval.tokenizer import MemoryRetrievalTokenizerV1
from pulsara_agent.retrieval.config import EmbeddingBackendConfig
from pulsara_agent.settings import LocalSettingsStore
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.test_stage2_conversation_kernel_postgres import (
    _accept_tool_attempt,
    _name,
    _repository,
    _start_root_turn,
)


pytestmark = pytest.mark.postgres


@pytest.fixture
def direct_memory_session(stage2_migrated_postgres_database):
    repository = _repository(stage2_migrated_postgres_database)
    workspace_root = _name("workspace")
    workspace_id = workspace_context_id(workspace_root)
    lease = repository.acquire_host_writer(
        session_id=_name("session"),
        workspace_id=workspace_id,
        workspace_root=workspace_root,
        writer_owner_id=_name("host"),
        lease_seconds=120,
        deadline_monotonic=monotonic() + 30,
    )
    turn_id = _name("turn")
    _start_root_turn(
        repository,
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        content=FrozenPromptContent.text("Remember and relate these observations."),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )

    def invoke(name: str, arguments: dict, mutation):
        deadline = monotonic() + 30
        cut = repository.prepare_provider_input_cut(
            lease.guard, turn_id=turn_id, deadline_monotonic=deadline
        )
        assistant_entry_id = _name("entry")
        call_id = _name("call")
        repository.commit_assistant_message(
            lease.guard,
            cut=cut,
            entry_id=assistant_entry_id,
            parent_content=InlineContent.from_bytes(b"memory action"),
            blocks=(
                AssistantToolCallBlock(
                    _name("block"), call_id, name, freeze_json(arguments)
                ),
            ),
            occurred_at=datetime.now(timezone.utc),
            actor_id="model:test",
            deadline_monotonic=deadline,
        )
        attempt = _accept_tool_attempt(
            repository,
            lease.guard,
            attempt_id=_name("attempt"),
            assistant_entry_id=assistant_entry_id,
            tool_call_id=call_id,
            authorization_kind="policy",
            authorization_reference="allow",
            actor_kind="runtime",
            actor_id=name,
            remote_idempotency_key=None,
            retry_of_attempt_id=None,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=deadline,
        )
        candidate = build_prepared_tool_result_acceptance(
            guard=lease.guard,
            workspace_id=workspace_id,
            result_id=_name("result"),
            result_entry_id=_name("entry"),
            turn_id=turn_id,
            assistant_entry_id=assistant_entry_id,
            tool_call_id=call_id,
            attempt_id=attempt.attempt_id,
            result_state="SUCCESS",
            canonical_preview_content=InlineContent.from_bytes(b"{}"),
            artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
            artifact_id=None,
            artifact_blob_descriptor=None,
            source_coverage=ToolOutputSourceCoverage.COMPLETE,
            display_kind=ToolResultDisplayKind.COMPLETE,
            source_coverage_reason=None,
            artifact_unavailability_reason=None,
            observed_at=datetime.now(timezone.utc),
            observation_duration_microseconds=None,
            observation_origin_kind=ToolObservationOrigin.BUILTIN,
            trusted_tool_reported_duration_microseconds=None,
            actor_id=name,
            memory_mutation=mutation,
        )
        accepted = repository.accept_tool_result(
            lease.guard, candidate=candidate, deadline_monotonic=deadline
        )
        assert isinstance(accepted, AcceptedMemoryToolResult)
        confirmed = repository.confirm_tool_result_winner(
            lease.guard, candidate=candidate, deadline_monotonic=deadline
        )
        assert confirmed == accepted
        return accepted, json.loads(accepted.canonical_body), candidate

    def remember(
        statement: str,
        *,
        kind: MemoryFactKind = MemoryFactKind.FACT,
        based_on: tuple[str, ...] = (),
        project: bool = False,
    ):
        context_id = workspace_id if project else CTX_GLOBAL
        mutation = PreparedRememberWrite(
            memory_domain_id="u_local",
            context_id=context_id,
            kind=kind,
            statement=statement,
            basis_refs=tuple(
                PreparedMemoryBasisReference(item, context_id, ordinal)
                for ordinal, item in enumerate(based_on)
            ),
            related=(),
            retrieval_summary=freeze_json({"status": "COMPLETE"}),
            search_terms=MemoryRetrievalTokenizerV1().tokenize(statement),
        )
        return invoke(
            "remember",
            {
                "statement": statement,
                "kind": kind.value,
                "context_target": "CURRENT_PROJECT" if project else "GLOBAL",
                "based_on_memory_ids": list(based_on),
            },
            mutation,
        )

    return repository, lease, invoke, remember


def test_user_text_edit_preserves_identity_and_relations_but_refreshes_retrieval(
    direct_memory_session, tmp_path,
):
    repository, lease, invoke, remember = direct_memory_session
    _, old, _ = remember("The site is cobalt-hall on Friday.")
    _, current, _ = remember("The site is amber-hall on Monday.")
    invoke(
        "mark_memory_relation",
        {
            "source_memory_id": current["memory_id"],
            "target_memory_id": old["memory_id"],
            "relation_kind": "SUPERSEDES",
        },
        PreparedMemoryRelationWrite(
            memory_domain_id="u_local",
            source_memory_id=current["memory_id"],
            target_memory_id=old["memory_id"],
            relation_kind=MemoryRelationKind.SUPERSEDES,
        ),
    )
    selection = MemoryManagementSelection()

    def detail(fact_id):
        return repository.memory_management_detail(
            memory_domain_id="u_local", selection=selection, fact_id=fact_id,
            deadline_monotonic=monotonic() + 30,
        )

    before = detail(current["memory_id"])
    assert before["user_edited_at"] is None
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        connection.execute(
            """INSERT INTO pulsara_v3.memory_embeddings
               (memory_domain_id, fact_id, fact_semantic_digest,
                embedding_contract_id, embedding_contract_version, embedding)
               SELECT memory_domain_id, id, fact_semantic_digest, 'test', 1,
                      ('[' || repeat('0,', 1023) || '0]')::public.vector
               FROM pulsara_v3.memory_facts WHERE id=%s""",
            (current["memory_id"],),
        )
    changed = repository.memory_management_edit_statement(
        memory_domain_id="u_local", selection=selection,
        fact_id=current["memory_id"],
        statement="  The site is violet-hall on Tuesday.  ",
        expected_updated_at=before["fact"]["updated_at"],
        deadline_monotonic=monotonic() + 30,
    )
    assert changed["changed"] is True
    assert changed["fact"]["statement"] == "The site is violet-hall on Tuesday."
    assert changed["fact"]["fact_id"] == current["memory_id"]
    assert changed["fact"]["updated_at"] != before["fact"]["updated_at"]
    after = detail(current["memory_id"])
    assert after["user_edited_at"] == changed["user_edited_at"]
    assert after["source"] == before["source"]
    assert [r["relation_id"] for r in after["relations"]] == [
        r["relation_id"] for r in before["relations"]
    ]
    assert [r["owner"] for r in after["relations"]] == [
        r["owner"] for r in before["relations"]
    ]
    assert after["fact"]["lifecycle"] == "ACTIVE"
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            """SELECT f.search_terms,
                      f.search_document @@ plainto_tsquery('pg_catalog.simple', 'violet') AS new_match,
                      f.search_document @@ plainto_tsquery('pg_catalog.simple', 'amber') AS old_match,
                      e.fact_id AS embedding_fact_id
               FROM pulsara_v3.memory_facts f LEFT JOIN pulsara_v3.memory_embeddings e
                 ON e.memory_domain_id=f.memory_domain_id AND e.fact_id=f.id
               WHERE f.id=%s""", (current["memory_id"],),
        ).fetchone()
    assert "violet" in row[0] and "amber" not in row[0]
    assert row[1] is True and row[2] is False
    assert row[3] is None
    binding = freeze_memory_read_context_binding(
        domain=MemoryDomainContext("u_local", "transient"),
        host_workspace_id="workspace:foreign",
    )
    query_row = PostgresMemoryQuery(repository.connection_provider).get(
        read_binding=binding, fact_id=current["memory_id"],
        deadline_monotonic=monotonic() + 30,
    )
    assert query_row is not None and query_row.user_edited_at == changed["user_edited_at"]
    assert query_row.statement == changed["fact"]["statement"]

    async def inspect_model_explanation():
        io = KernelSessionIO()
        port = KernelMemoryToolPort(
            repository=repository, session_id=lease.guard.session_id,
            read_binding=binding, embedding_config=EmbeddingBackendConfig(),
            io_owner=io, settings=LocalSettingsStore(tmp_path / "settings.yaml"),
        )
        try:
            return json.loads((await port._get(
                {"memory_id": current["memory_id"]}, explain=True,
            )).content)
        finally:
            await port.aclose()
            await io.aclose(deadline_monotonic=monotonic() + 10)

    explanation = asyncio.run(inspect_model_explanation())
    assert explanation["user_edit"]["edited_at"] == changed["user_edited_at"]
    assert "original save" in explanation["user_edit"]["note"]
    assert explanation["statement"] == changed["fact"]["statement"]
    assert repository.memory_management_edit_statement(
        memory_domain_id="u_local", selection=selection,
        fact_id=current["memory_id"], statement=changed["fact"]["statement"],
        expected_updated_at=changed["fact"]["updated_at"],
        deadline_monotonic=monotonic() + 30,
    )["changed"] is False
    with pytest.raises(MemoryManagementError) as stale:
        repository.memory_management_edit_statement(
            memory_domain_id="u_local", selection=selection,
            fact_id=current["memory_id"], statement="A stale edit.",
            expected_updated_at=before["fact"]["updated_at"],
            deadline_monotonic=monotonic() + 30,
        )
    assert stale.value.status == 409
    # An active fact cannot take another active fact's semantic identity.
    _, other, _ = remember("Another active fact.")
    with pytest.raises(MemoryManagementError) as duplicate:
        repository.memory_management_edit_statement(
            memory_domain_id="u_local", selection=selection,
            fact_id=current["memory_id"], statement="Another active fact.",
            expected_updated_at=changed["fact"]["updated_at"],
            deadline_monotonic=monotonic() + 30,
        )
    assert duplicate.value.status == 409
    assert detail(current["memory_id"])["fact"]["statement"] == changed["fact"]["statement"]
    old_before = detail(old["memory_id"])
    assert old_before["fact"]["lifecycle"] == "SUPERSEDED"
    old_edit = repository.memory_management_edit_statement(
        memory_domain_id="u_local", selection=selection, fact_id=old["memory_id"],
        statement="The prior site was indigo-hall.",
        expected_updated_at=old_before["fact"]["updated_at"],
        deadline_monotonic=monotonic() + 30,
    )
    assert old_edit["fact"]["lifecycle"] == "SUPERSEDED"
    assert other["memory_id"] != current["memory_id"]
    preview = repository.memory_deletion_preview(
        memory_domain_id="u_local", selection=selection,
        fact_id=current["memory_id"], deadline_monotonic=monotonic() + 30,
    )
    assert b"The prior site was indigo-hall." in b"\n".join(preview)
    assert b"The site is violet-hall on Tuesday." in b"\n".join(preview)


def test_user_text_edit_obeys_original_scope_and_kind_limits(direct_memory_session):
    repository, _, _invoke, remember = direct_memory_session
    _, preference, _ = remember(
        "Please answer in concise sentences.", kind=MemoryFactKind.RESPONSE_PREFERENCE,
    )
    selection = MemoryManagementSelection()
    before = repository.memory_management_detail(
        memory_domain_id="u_local", selection=selection,
        fact_id=preference["memory_id"], deadline_monotonic=monotonic() + 30,
    )
    edit_args = dict(
        memory_domain_id="u_local", selection=selection,
        fact_id=preference["memory_id"],
        expected_updated_at=before["fact"]["updated_at"],
        deadline_monotonic=monotonic() + 30,
    )
    with pytest.raises(ValueError, match="2048"):
        repository.memory_management_edit_statement(statement="x" * 2049, **edit_args)
    with pytest.raises(MemoryManagementError) as wrong_scope:
        repository.memory_management_edit_statement(
            **{**edit_args, "selection": MemoryManagementSelection("project", "ctx:workspace/not-a-project")},
            statement="Please answer at length.",
        )
    assert wrong_scope.value.status == 404
    after = repository.memory_management_detail(
        memory_domain_id="u_local", selection=selection,
        fact_id=preference["memory_id"], deadline_monotonic=monotonic() + 30,
    )
    assert after["fact"]["statement"] == before["fact"]["statement"]
    assert after["user_edited_at"] is None


def test_memory_page_projects_fact_creator_and_relation_owner(
    direct_memory_session,
):
    repository, lease, invoke, remember = direct_memory_session
    _, original, _ = remember("The delivery date is Friday.")
    _, revised, _ = remember("The delivery date is Monday.")
    invoke(
        "mark_memory_relation",
        {
            "source_memory_id": revised["memory_id"],
            "target_memory_id": original["memory_id"],
            "relation_kind": "SUPERSEDES",
        },
        PreparedMemoryRelationWrite(
            memory_domain_id="u_local",
            source_memory_id=revised["memory_id"],
            target_memory_id=original["memory_id"],
            relation_kind=MemoryRelationKind.SUPERSEDES,
        ),
    )
    detail = repository.memory_management_detail(
        memory_domain_id="u_local",
        selection=MemoryManagementSelection(),
        fact_id=revised["memory_id"],
        deadline_monotonic=monotonic() + 30,
    )
    assert "supporting_results" not in detail
    owner = detail["relations"][0]["owner"]
    assert owner["write_tool"] == "mark_memory_relation"
    assert owner["source"]["session_id"] == lease.guard.session_id
    assert owner["source"]["entry_id"] != detail["source"]["entry_id"]

    _, dependent, _ = remember(
        "The timeline depends on the revised delivery date.",
        based_on=(revised["memory_id"],),
    )
    basis_detail = repository.memory_management_detail(
        memory_domain_id="u_local",
        selection=MemoryManagementSelection(),
        fact_id=dependent["memory_id"],
        deadline_monotonic=monotonic() + 30,
    )
    assert basis_detail["relations"][0]["owner"]["write_tool"] == "remember"
    assert basis_detail["relations"][0]["owner"]["source"] == basis_detail["source"]


def test_memory_project_list_contains_only_projects_with_persisted_facts(
    direct_memory_session,
):
    repository, lease, _invoke, remember = direct_memory_session
    def deadline():
        return monotonic() + 30
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=deadline(),
    ) as connection:
        workspace_id = connection.execute(
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (lease.guard.session_id,),
        ).fetchone()[0]
    other_workspace = _name("workspace")
    repository.acquire_host_writer(
        session_id=_name("session"),
        workspace_id=other_workspace,
        writer_owner_id=_name("host"),
        lease_seconds=120,
        deadline_monotonic=deadline(),
    )

    def projects():
        return repository.memory_management_projects(
            memory_domain_id="u_local", deadline_monotonic=deadline()
        )["items"]

    assert projects() == []
    _, global_fact, _ = remember("This preference is shared across conversations.")
    assert projects() == []
    _, project_fact, _ = remember("This project's release is on Friday.", project=True)
    assert project_fact["status"] == "SAVED", project_fact
    assert {item["workspace_id"] for item in projects()} == {workspace_id}
    selection = MemoryManagementSelection("project", workspace_id)
    detail = repository.memory_management_detail(
        memory_domain_id="u_local",
        selection=selection,
        fact_id=project_fact["memory_id"],
        deadline_monotonic=deadline(),
    )
    assert detail["source"]["session_id"] == lease.guard.session_id
    preview = repository.memory_deletion_preview(
        memory_domain_id="u_local",
        selection=selection,
        fact_id=project_fact["memory_id"],
        deadline_monotonic=deadline(),
    )
    repository.execute_memory_deletion(
        memory_domain_id="u_local",
        selection=selection,
        fact_id=project_fact["memory_id"],
        additional=(),
        expected_records=lambda: iter(preview),
        deadline_monotonic=deadline(),
    )
    assert projects() == []
    assert global_fact["memory_id"] != project_fact["memory_id"]


def test_direct_save_duplicate_relation_and_deleted_ack(direct_memory_session):
    repository, lease, invoke, remember = direct_memory_session
    saved, first, _ = remember("The release target is Friday.")
    assert first["status"] == "SAVED"
    assert saved.model_visible_memory_fact_ids == (first["memory_id"],)
    duplicate, second, _ = remember("The release target is Friday.")
    assert second["status"] == "ALREADY_PRESENT"
    assert second["memory_id"] == first["memory_id"]
    assert duplicate.model_visible_memory_fact_ids == (first["memory_id"],)
    _, updated, _ = remember("The release target is Monday.")
    relation, result, _ = invoke(
        "mark_memory_relation",
        {
            "source_memory_id": updated["memory_id"],
            "target_memory_id": first["memory_id"],
            "relation_kind": "SUPERSEDES",
        },
        PreparedMemoryRelationWrite(
            memory_domain_id="u_local",
            source_memory_id=updated["memory_id"],
            target_memory_id=first["memory_id"],
            relation_kind=MemoryRelationKind.SUPERSEDES,
        ),
    )
    assert result["status"] == "SAVED"
    assert relation.model_visible_memory_fact_ids == (
        updated["memory_id"], first["memory_id"]
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            "SELECT id,lifecycle,source_session_id,source_tool_result_id "
            "FROM pulsara_v3.memory_facts WHERE id=ANY(%s::text[]) ORDER BY id",
            ([first["memory_id"], updated["memory_id"]],),
        ).fetchall()
    assert {str(row[0]): str(row[1]) for row in rows} == {
        first["memory_id"]: "SUPERSEDED",
        updated["memory_id"]: "ACTIVE",
    }
    assert all(str(row[2]) == lease.guard.session_id for row in rows)


def test_memory_page_deletion_restores_superseded_and_cascades_basis(
    direct_memory_session,
):
    repository, _, invoke, remember = direct_memory_session
    _, original, _ = remember("The original venue is Building A.")
    _, dependent, _ = remember(
        "The map points to Building A.", based_on=(original["memory_id"],)
    )
    _, replacement, _ = remember("The venue is Building B.")
    invoke(
        "mark_memory_relation",
        {
            "source_memory_id": replacement["memory_id"],
            "target_memory_id": original["memory_id"],
            "relation_kind": "SUPERSEDES",
        },
        PreparedMemoryRelationWrite(
            memory_domain_id="u_local",
            source_memory_id=replacement["memory_id"],
            target_memory_id=original["memory_id"],
            relation_kind=MemoryRelationKind.SUPERSEDES,
        ),
    )
    selection = MemoryManagementSelection()
    preview = repository.memory_deletion_preview(
        memory_domain_id="u_local",
        selection=selection,
        fact_id=replacement["memory_id"],
        deadline_monotonic=monotonic() + 30,
    )
    relation_effects = [
        json.loads(item) for item in preview
        if json.loads(item).get("type") == "RELATION_EFFECT"
    ]
    assert relation_effects
    assert all("owner" not in item for item in relation_effects)
    assert any(
        json.loads(item).get("type") == "FACT_RESTORE" for item in preview
    )
    repository.execute_memory_deletion(
        memory_domain_id="u_local",
        selection=selection,
        fact_id=replacement["memory_id"],
        additional=(),
        expected_records=lambda: iter(preview),
        deadline_monotonic=monotonic() + 30,
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        original_row = connection.execute(
            "SELECT lifecycle FROM pulsara_v3.memory_facts WHERE id=%s",
            (original["memory_id"],),
        ).fetchone()
        dependent_row = connection.execute(
            "SELECT lifecycle FROM pulsara_v3.memory_facts WHERE id=%s",
            (dependent["memory_id"],),
        ).fetchone()
    assert original_row == ("ACTIVE",)
    assert dependent_row == ("ACTIVE",)
    preview = repository.memory_deletion_preview(
        memory_domain_id="u_local",
        selection=selection,
        fact_id=original["memory_id"],
        deadline_monotonic=monotonic() + 30,
    )
    assert sum(json.loads(item).get("type") == "FACT_DELETE" for item in preview) == 2
    repository.execute_memory_deletion(
        memory_domain_id="u_local",
        selection=selection,
        fact_id=original["memory_id"],
        additional=(),
        expected_records=lambda: iter(preview),
        deadline_monotonic=monotonic() + 30,
    )


def test_direct_writer_rejects_a_memory_body_not_owned_by_the_tool_call(
    direct_memory_session,
):
    repository, lease, invoke, _remember = direct_memory_session
    mutation = PreparedRememberWrite(
        memory_domain_id="u_local",
        context_id=CTX_GLOBAL,
        kind=MemoryFactKind.FACT,
        statement="The actual fact.",
        basis_refs=(),
        related=(),
        retrieval_summary=freeze_json({"status": "COMPLETE"}),
        search_terms=MemoryRetrievalTokenizerV1().tokenize("The actual fact."),
    )
    with pytest.raises(ConversationKernelConflict, match="does not exact-join"):
        invoke(
            "remember",
            {
                "statement": "A different fact.",
                "kind": "FACT",
                "context_target": "GLOBAL",
            },
            mutation,
        )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.memory_facts WHERE source_session_id=%s",
            (lease.guard.session_id,),
        ).fetchone() == (0,)


def test_relation_repeats_and_late_relation_failure_do_not_undo_saved_fact(
    direct_memory_session,
):
    repository, _, invoke, remember = direct_memory_session
    _, first, _ = remember("The office is in Building A.")
    _, second, _ = remember("The office is in Building B.")
    _, third, _ = remember("The office is in Building C.")

    def relate(source: str, target: str, kind: MemoryRelationKind):
        _, body, _ = invoke(
            "mark_memory_relation",
            {
                "source_memory_id": source,
                "target_memory_id": target,
                "relation_kind": kind.value,
            },
            PreparedMemoryRelationWrite(
                memory_domain_id="u_local",
                source_memory_id=source,
                target_memory_id=target,
                relation_kind=kind,
            ),
        )
        return body

    a, b, c = (item["memory_id"] for item in (first, second, third))
    assert relate(a, b, MemoryRelationKind.CONTRADICTS)["status"] == "SAVED"
    assert relate(b, a, MemoryRelationKind.CONTRADICTS)["status"] == "ALREADY_PRESENT"
    assert relate(c, a, MemoryRelationKind.SUPERSEDES)["status"] == "SAVED"
    assert relate(c, a, MemoryRelationKind.SUPERSEDES)["status"] == "ALREADY_PRESENT"
    late = relate(b, a, MemoryRelationKind.SUPERSEDES)
    assert late["advisory"] is True
    assert "error" in late
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        states = dict(connection.execute(
            "SELECT id, lifecycle FROM pulsara_v3.memory_facts "
            "WHERE id=ANY(%s::text[])",
            ([a, b, c],),
        ).fetchall())
    assert states == {a: "SUPERSEDED", b: "ACTIVE", c: "ACTIVE"}


def test_preferences_remain_storable_beyond_projection_head_and_conflicts_are_excluded(
    direct_memory_session,
):
    repository, lease, invoke, remember = direct_memory_session
    ids = [
        remember(
            f"For response style case {index:02d}, use concise wording.",
            kind=MemoryFactKind.RESPONSE_PREFERENCE,
        )[1]["memory_id"]
        for index in range(20)
    ]
    invoke(
        "mark_memory_relation",
        {
            "source_memory_id": ids[0],
            "target_memory_id": ids[1],
            "relation_kind": "CONTRADICTS",
        },
        PreparedMemoryRelationWrite(
            memory_domain_id="u_local",
            source_memory_id=ids[0],
            target_memory_id=ids[1],
            relation_kind=MemoryRelationKind.CONTRADICTS,
        ),
    )
    binding = freeze_memory_read_context_binding(
        domain=MemoryDomainContext("u_local", "transient"),
        host_workspace_id="workspace:preference-test",
    )
    snapshot = PostgresMemoryQuery(
        repository.connection_provider
    ).response_preference_snapshot(
        read_binding=binding,
        deadline_monotonic=monotonic() + 30,
    )
    assert len(snapshot.facts) == 16
    assert snapshot.selection_incomplete
    assert snapshot.conflicts_omitted
    assert {item.fact_id for item in snapshot.facts}.isdisjoint(ids[:2])
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        count = connection.execute(
            "SELECT count(*) FROM pulsara_v3.memory_facts "
            "WHERE source_session_id=%s AND fact_kind='RESPONSE_PREFERENCE'",
            (lease.guard.session_id,),
        ).fetchone()[0]
    assert count == 20


def test_remember_related_preview_returns_three_exact_context_facts(
    direct_memory_session, tmp_path,
):
    repository, lease, _invoke, remember = direct_memory_session
    ids = tuple(
        remember(f"Project Orion quarterly revenue for Q{index} is {index * 10}.")[1][
            "memory_id"
        ]
        for index in range(1, 4)
    )
    binding = freeze_memory_read_context_binding(
        domain=MemoryDomainContext("u_local", "transient"),
        host_workspace_id="workspace:related-preview-test",
    )

    async def exercise():
        io = KernelSessionIO()
        port = KernelMemoryToolPort(
            repository=repository,
            session_id=lease.guard.session_id,
            read_binding=binding,
            embedding_config=EmbeddingBackendConfig(),
            io_owner=io,
            settings=LocalSettingsStore(tmp_path / "settings.yaml"),
        )
        try:
            return await port._related_for_remember(
                "Project Orion quarterly revenue for Q4 is 40.",
                context_id=CTX_GLOBAL,
            )
        finally:
            await port.aclose()
            await io.aclose(deadline_monotonic=monotonic() + 10)

    related, coverage = asyncio.run(exercise())
    assert {item.memory_id for item in related} == set(ids)
    assert coverage["disposition"] == "COMPLETE"
    assert coverage["bounded_search_not_exhaustive"] is True


def test_direct_source_provenance_is_visible_only_in_its_origin_workspace(
    direct_memory_session,
):
    repository, lease, _invoke, remember = direct_memory_session
    _, saved, _ = remember("The design review is on Tuesday.")
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        workspace_id = connection.execute(
            "SELECT workspace_id FROM pulsara_v3.sessions WHERE id=%s",
            (lease.guard.session_id,),
        ).fetchone()[0]
    query = PostgresMemoryQuery(repository.connection_provider)
    same = query.provenance(
        read_binding=freeze_memory_read_context_binding(
            domain=MemoryDomainContext("u_local", "transient"),
            host_workspace_id=workspace_id,
        ),
        fact_id=saved["memory_id"],
        deadline_monotonic=monotonic() + 30,
    )
    foreign = query.provenance(
        read_binding=freeze_memory_read_context_binding(
            domain=MemoryDomainContext("u_local", "transient"),
            host_workspace_id="workspace:foreign",
        ),
        fact_id=saved["memory_id"],
        deadline_monotonic=monotonic() + 30,
    )
    assert same is not None and same.provenance_disposition == "SAME_ORIGIN"
    assert same.producer_session_id == lease.guard.session_id
    assert not hasattr(same, "tool_result_ids")
    assert foreign is not None
    assert foreign.provenance_disposition == "CROSS_ORIGIN_REDACTED"
    assert foreign.producer_session_id is None
    assert foreign.producer_entry_id is None


def test_stale_basis_rejects_even_an_exact_duplicate_without_a_partial_write(
    direct_memory_session,
):
    repository, lease, invoke, remember = direct_memory_session
    _, original, _ = remember("The checkpoint is at noon.")
    statement = "The checkpoint is at noon."
    accepted, body, _ = invoke(
        "remember",
        {
            "statement": statement,
            "kind": "FACT",
            "context_target": "GLOBAL",
            "based_on_memory_ids": ["memory:absent"],
        },
        PreparedRememberWrite(
            memory_domain_id="u_local",
            context_id=CTX_GLOBAL,
            kind=MemoryFactKind.FACT,
            statement=statement,
            basis_refs=(
                PreparedMemoryBasisReference("memory:absent", CTX_GLOBAL, 0),
            ),
            related=(),
            retrieval_summary=freeze_json({"status": "COMPLETE"}),
            search_terms=MemoryRetrievalTokenizerV1().tokenize(statement),
        ),
    )
    assert accepted.result_state == "APPLICATION_ERROR"
    assert "error" in body
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        rows = connection.execute(
            "SELECT id FROM pulsara_v3.memory_facts WHERE source_session_id=%s",
            (lease.guard.session_id,),
        ).fetchall()
    assert rows == [(original["memory_id"],)]


def test_exact_tool_result_confirmation_survives_memory_page_deletion(
    direct_memory_session,
):
    repository, lease, _invoke, remember = direct_memory_session
    accepted, body, candidate = remember("The release note draft is approved.")
    selection = MemoryManagementSelection()
    preview = repository.memory_deletion_preview(
        memory_domain_id="u_local",
        selection=selection,
        fact_id=body["memory_id"],
        deadline_monotonic=monotonic() + 30,
    )
    repository.execute_memory_deletion(
        memory_domain_id="u_local",
        selection=selection,
        fact_id=body["memory_id"],
        additional=(),
        expected_records=lambda: iter(preview),
        deadline_monotonic=monotonic() + 30,
    )
    assert repository.confirm_tool_result_winner(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    ) == accepted
