"""Memory presentation preserves selected facts and settles small write receipts."""

from dataclasses import replace
import asyncio
import json
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel._repository import memory as memory_repository
from pulsara_agent.conversation_kernel.memory.contracts import MemoryFactKind, MemoryRelationKind
from pulsara_agent.conversation_kernel.memory.writes import (
    MemoryRelatedPreview,
    PreparedMemoryRelationWrite,
    PreparedRememberWrite,
    memory_mutation_manifest,
)
from pulsara_agent.conversation_kernel.memory_tools import (
    KernelMemoryToolPort,
    _bounded_memory_relations,
    _memory_page_budget,
    _memory_search_display,
)
from pulsara_agent.identity import random_id
from pulsara_agent.memory.scope import CTX_GLOBAL
from pulsara_agent.memory.targets import memory_target
from pulsara_agent.primitives.context import canonical_json_bytes, freeze_json
from pulsara_agent.primitives.tool_result_budget import ToolResultPageBoundary, ToolResultPageBudget


def budget(maximum):
    return ToolResultPageBudget(
        tool_call_id=random_id("call"),
        result_target={"kind": "SESSION_ENTRY", "session_id": random_id("session"), "entry_id": random_id("entry")},
        accepts_provider_body=lambda body, _ids: len(body.encode("utf-8")) <= maximum,
    )


def search_payload(count):
    ids = tuple(random_id("memory") for _ in range(count))
    return {
        "requested_filters": {"kind": None},
        "retrieval_summary": {"status": "COMPLETE", "match": "EXACT", "ranking": "SPARSE_ONLY", "relation_check": "COMPLETE"},
        "memories": [{
            "target": memory_target(identity), "kind": "FACT", "context_product_label": "global",
            "current_context": False, "statement": "\\\"材料\n" * 600,
            "recorded_at": "2026-11-03T00:00:00Z", "filter_match": "EXACT",
        } for identity in ids],
        "relation_warnings": [], "advisory": True, "may_be_stale_or_incomplete": True,
    }, ids


def test_search_keeps_complete_selected_body_and_packs_native_excerpts():
    payload, ids = search_payload(8)
    original = canonical_json_bytes(payload)
    allowance = budget(1800)
    native = _memory_search_display(payload, allowance, ids)
    assert canonical_json_bytes(payload) == original
    assert len(payload["memories"]) == 8
    assert 0 < native["displayed_memories"] < 8
    assert native["omitted_memories"] == 8 - native["displayed_memories"]
    assert allowance.fits(canonical_json_bytes(native).decode(), model_visible_memory_fact_ids=ids, include_read=True)
    for actual, full in zip(native["memories"], payload["memories"]):
        assert actual["target"] == full["target"]
        assert full["statement"].startswith(actual["statement_excerpt"])
        assert actual["statement_truncated"]
        assert actual["inspect"] == {"tool": "get_memory", "arguments": {"target": full["target"]}}


def test_search_does_not_return_empty_success_when_first_reference_cannot_fit():
    payload, ids = search_payload(2)
    with pytest.raises(ToolResultPageBoundary):
        _memory_search_display(payload, budget(100), ids)


def test_search_port_and_shared_projector_preserve_full_body_and_domain_excerpt():
    from pulsara_agent.conversation_kernel.memory.recall import MemoryQueryResult, MemoryQueryRow, MemoryRetrievalDisposition
    from pulsara_agent.conversation_kernel.tool_result_display import project_native_tool_result
    async def run():
        payload, ids = search_payload(8)
        rows = tuple(MemoryQueryRow(fact_id=item["target"]["memory_id"], memory_domain_id="u_local",
            context_id=CTX_GLOBAL, fact_kind="FACT", lifecycle="ACTIVE", statement=item["statement"] + str(index),
            recorded_at=item["recorded_at"], fact_semantic_digest="sha256:" + "0" * 64)
            for index, item in enumerate(payload["memories"]))
        recalled = MemoryQueryResult(disposition=MemoryRetrievalDisposition.COMPLETE, facts=rows,
            attempted_stages=(), dense_available=False)
        class ImmediateIO:
            async def run(self, operation, **kwargs):
                return operation(**kwargs)
        port = object.__new__(KernelMemoryToolPort)
        port._deadlines = SimpleNamespace(deadline=lambda _owner: monotonic() + 5)
        port._io, port._read_binding = ImmediateIO(), object()
        port._query = SimpleNamespace(active_contradictions=lambda **kwargs: ())
        async def embedding():
            return None
        async def recall(**kwargs):
            return recalled
        async def rerank(query, result, **kwargs):
            return result
        port._embedding_provider, port._parallel_recall, port._rerank_explicit = embedding, recall, rerank
        allowance = budget(1800)
        result = await port._search({"query": "retained facts", "limit": 8}, SimpleNamespace(page_budget=allowance))
        original = json.loads(result.content)
        native = json.loads(result.native_output)
        assert [item["statement"] for item in original["memories"]] == [row.statement for row in rows]
        projected, omitted = project_native_tool_result(public_output=result.content.decode(),
            native_output=result.native_output.decode(), page_complete=result.page_complete,
            required_facts=result.required_facts, output_available=True, output_unavailability_reason=None,
            page_budget=allowance, model_visible_memory_fact_ids=result.model_visible_memory_fact_ids)
        rendered = json.loads(projected)
        assert omitted and rendered.pop("read") == allowance.read
        assert rendered == native
        assert rendered["memories"][0]["inspect"]["tool"] == "get_memory"
        assert result.model_visible_memory_fact_ids == ids
        assert allowance.fits(projected, model_visible_memory_fact_ids=ids)
    asyncio.run(run())


def test_search_warning_projection_discloses_partiality():
    payload, ids = search_payload(1)
    payload["relation_warnings"] = [{
        "kind": "ACTIVE_CONTRADICTION", "source": memory_target(ids[0]),
        "target": memory_target(random_id("memory")),
    } for _ in range(20)]
    native = _memory_search_display(payload, budget(1800), ids)
    assert native["omitted_relation_warnings"] > 0
    assert native["retrieval_summary"]["relation_check"] == "PARTIAL"
    assert len(payload["relation_warnings"]) == 20


def test_production_memory_context_must_have_its_allocated_budget():
    with pytest.raises(ToolResultPageBoundary, match="allocated"):
        _memory_page_budget(SimpleNamespace())
    assert isinstance(_memory_page_budget(None), ToolResultPageBudget)


def test_exposure_limit_does_not_expand_to_fit_extra_warning_endpoints():
    ids = tuple(random_id("memory") for _ in range(50))
    relation = SimpleNamespace(source_fact_id=ids[0], target_fact_id=random_id("memory"))
    selected, exposed = _bounded_memory_relations(ids, (relation,))
    assert selected == ()
    assert exposed == ids


def test_get_discloses_bounded_relations_and_retains_original_exposure():
    identity = random_id("memory")
    row = SimpleNamespace(fact_id=identity, fact_kind="FACT", context_id=CTX_GLOBAL,
                          lifecycle="SUPERSEDED", statement="The original statement.",
                          recorded_at="2026-11-03T00:00:00Z", user_edited_at="2026-11-03T01:00:00Z")
    relations = tuple(SimpleNamespace(relation_id=random_id("relation"),
                         source_fact_id=identity, target_fact_id=random_id("memory"),
                         relation_kind="SUPERSEDES", supersede_mode="SAME_KIND_REPLACEMENT")
                      for _ in range(100))
    class ImmediateIO:
        async def run(self, operation, **kwargs):
            return operation(**kwargs)
    port = object.__new__(KernelMemoryToolPort)
    port._io = ImmediateIO()
    port._read_binding = object()
    port._query = SimpleNamespace(get=lambda **kwargs: row, direct_relations=lambda **kwargs: relations)
    port._canonical_deadline = lambda duration: monotonic() + duration
    result = asyncio.run(port._get({"target": memory_target(identity)}))
    payload = json.loads(result.content)
    assert payload["relation_check"] == "PARTIAL"
    assert len(payload["relations"]) == 100
    assert payload["statement"] == row.statement
    assert payload["lifecycle"] == "SUPERSEDED" and payload["user_edit"]["edited_at"] == row.user_edited_at
    assert result.model_visible_memory_fact_ids == (identity,)
    assert result.public_format.value == "JSON"


def remember_write(page_budget):
    return PreparedRememberWrite(
        memory_domain_id="u_local", context_id=CTX_GLOBAL, kind=MemoryFactKind.FACT,
        statement="A retained memory.", basis_refs=(),
        related=(MemoryRelatedPreview(random_id("memory"), MemoryFactKind.FACT, CTX_GLOBAL, "2026-11-03T00:00:00Z"),),
        retrieval_summary=freeze_json({"disposition": "COMPLETE"}), page_budget=page_budget,
    )


def test_budget_is_process_local_not_memory_semantics():
    write = remember_write(budget(3000))
    changed = replace(write, page_budget=budget(1))
    assert changed == write
    assert memory_mutation_manifest(changed) == memory_mutation_manifest(write)


def test_remember_checks_small_receipt_before_first_insert():
    operations = object.__new__(memory_repository._MemoryOperations)
    calls = []
    connection = SimpleNamespace(execute=lambda *args: calls.append(args))
    candidate = SimpleNamespace(session_id=random_id("session"), result_id=random_id("result"), tool_call_id=random_id("call"), observed_at="2026-11-03T00:00:00Z")
    with pytest.raises(ToolResultPageBoundary, match="receipt"):
        operations._settle_remember(connection, candidate=candidate, write=remember_write(budget(1)), deadline_monotonic=monotonic() + 5, owner={})
    assert calls == []


def test_remember_returns_small_references_and_quotes_both_statuses_before_insert():
    operations = object.__new__(memory_repository._MemoryOperations)
    quoted = []
    allowance = ToolResultPageBudget(
        tool_call_id=random_id("call"),
        accepts_provider_body=lambda body, _ids: quoted.append(json.loads(body)["status"]) or True,
    )
    write = remember_write(allowance)
    candidate = SimpleNamespace(session_id=random_id("session"), result_id=random_id("result"),
                                tool_call_id=allowance.tool_call_id, observed_at="2026-11-03T00:00:00Z")
    def execute(sql, parameters):
        assert quoted == ["SAVED", "ALREADY_PRESENT"]
        assert sql.lstrip().startswith("INSERT INTO pulsara_v3.memory_facts")
        return SimpleNamespace(fetchone=lambda: {"id": parameters[0]})
    outcome = operations._settle_remember(SimpleNamespace(execute=execute), candidate=candidate,
                  write=write, deadline_monotonic=monotonic() + 5, owner={})
    payload = json.loads(outcome.canonical_body)
    assert payload["status"] == "SAVED"
    assert payload["target"]["memory_id"] in outcome.visible_memory_fact_ids
    assert payload["retrieval_summary"] == {"disposition": "COMPLETE"}
    assert payload["relationship_labels_not_inferred"] is True
    assert len(payload["related_memories"]) == 1
    related = payload["related_memories"][0]
    assert "statement" not in related
    assert related["inspect"] == {"tool": "get_memory", "arguments": {"target": related["target"]}}


def test_relation_checks_receipt_before_insert_or_supersede(monkeypatch):
    source, target = random_id("memory"), random_id("memory")
    calls = []
    def execute(sql, _parameters):
        calls.append(sql)
        if "FROM pulsara_v3.memory_facts" in sql:
            return SimpleNamespace(fetchall=lambda: [{"id": identity, "context_id": CTX_GLOBAL, "lifecycle": "ACTIVE", "fact_kind": "FACT"} for identity in (source, target)])
        assert "SELECT" in sql
        return SimpleNamespace(fetchone=lambda: None)
    monkeypatch.setattr(memory_repository, "lock_canonical_identities", lambda *args, **kwargs: None)
    operations = object.__new__(memory_repository._MemoryOperations)
    write = PreparedMemoryRelationWrite("u_local", source, target, MemoryRelationKind.SUPERSEDES, page_budget=budget(1))
    candidate = SimpleNamespace(tool_call_id=random_id("call"), workspace_id=random_id("workspace"))
    with pytest.raises(ToolResultPageBoundary, match="receipt"):
        operations._settle_memory_relation(SimpleNamespace(execute=execute), candidate=candidate, write=write)
    assert calls and all(sql.lstrip().startswith("SELECT") for sql in calls)
