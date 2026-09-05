from dataclasses import asdict

import pytest

from pulsara_agent.conversation_kernel.memory.management import (
    MemoryManagementFact,
    MemoryManagementSelection,
    decode_cursor,
    deletion_graph,
    encode_cursor,
    normalized_search,
    relative_role,
    validate_product_record,
    with_end,
)


def edge(source, target, kind="BASED_ON"):
    return dict(
        id=f"{kind}:{source}:{target}",
        source_fact_id=source,
        target_fact_id=target,
        relation_kind=kind,
    )


@pytest.mark.parametrize(
    "kind,seed,deleted,restored",
    [
        ("BASED_ON", "a", {"a"}, set()),
        ("BASED_ON", "b", {"a", "b"}, set()),
        ("SUPERSEDES", "a", {"a"}, {"b"}),
        ("SUPERSEDES", "b", {"b"}, set()),
        ("CONTRADICTS", "a", {"a"}, set()),
        ("CONTRADICTS", "b", {"b"}, set()),
    ],
)
def test_single_edge(kind, seed, deleted, restored):
    plan = deletion_graph([seed], [edge("a", "b", kind)])
    assert plan.delete_ids == deleted
    assert plan.restore_ids == restored
    assert len(plan.relation_ids) == 1


def test_diamond_cycles_and_long_chain():
    edges = [
        edge("a", "b"),
        edge("a", "c"),
        edge("b", "d"),
        edge("c", "d"),
        edge("d", "a"),
    ]
    assert deletion_graph(["d"], edges).delete_ids == {"a", "b", "c", "d"}
    edges = [edge(str(i + 1), str(i)) for i in range(2000)]
    assert len(deletion_graph(["0"], edges).delete_ids) == 2001


def test_supersede_chain_and_multiple_incoming():
    edges = [edge("a", "b", "SUPERSEDES"), edge("b", "c", "SUPERSEDES")]
    assert deletion_graph(["a"], edges).restore_ids == {"b"}
    assert deletion_graph(["b"], edges).blocked_ancestry == (("c", "a"),)
    assert deletion_graph(["a", "b"], edges).blocked_ancestry == ()
    assert deletion_graph(["a", "b"], edges).restore_ids == {"c"}
    edges.append(edge("d", "c", "SUPERSEDES"))
    assert deletion_graph(["b"], edges).restore_ids == set()


@pytest.mark.parametrize(
    "source,expected", [(True, "CONFLICTS_WITH"), (False, "CONFLICTS_WITH")]
)
def test_relative_conflict_is_symmetric(source, expected):
    assert relative_role("CONTRADICTS", selected_is_source=source) == expected


def test_context_search_and_cursor_contract():
    with pytest.raises(ValueError):
        MemoryManagementSelection("global", "ctx:global")
    assert normalized_search("  100%_\\  ") == "100%_\\"
    with pytest.raises(ValueError):
        normalized_search("中" * 342)
    filters = {"search": "中" * 341, "context": "ctx:global"}
    cursor = encode_cursor(filters, ("instant", "id"))
    assert decode_cursor(cursor, filters, 2) == ("instant", "id")
    with pytest.raises(ValueError):
        decode_cursor(cursor, {"search": "different"}, 2)


def test_confirmation_keeps_complete_text_and_subsecond_updated_at():
    fact = MemoryManagementFact(
        "memory:a",
        "ctx:global",
        "FACT",
        "ACTIVE",
        "完整正文",
        "2026-09-05T00:00:00Z",
        "2026-09-05T00:00:00.123456+00:00",
    )
    record = {"type": "FACT_DELETE", "fact": asdict(fact)}
    assert validate_product_record(record) == record
    assert list(with_end([record]))[-1] == {"type": "END", "counts": {"FACT_DELETE": 1}}
    with pytest.raises(ValueError):
        validate_product_record({**record, "fingerprint": "unneeded"})
