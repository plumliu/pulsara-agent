from pulsara_agent.entities.memory import Preference
from pulsara_agent.graph.jsonld_codec import normalize_jsonld_document
from pulsara_agent.jsonld import NodeRef, utc_now
from pulsara_agent.ontology import memory
from pulsara_agent.ontology.registry import CORE_CONTEXT
from pulsara_agent.storage.postgres_memory_projection import (
    iter_relation_rows,
    memory_node_projection,
)


def test_memory_node_projection_extracts_complete_canonical_memory_fields() -> None:
    now = utc_now()
    document = normalize_jsonld_document(
        Preference(
            id="preference:test",
            statement="The user prefers concise summaries.",
            scope="ctx:user",
            status=memory.NodeStatus.ACTIVE,
            confidence_level=memory.ConfidenceLevel.HIGH,
            verification_status=memory.VerificationStatus.USER_CONFIRMED,
            source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
            created_at=now,
            updated_at=now,
            gate_reason="explicit user preference",
            evidence=(NodeRef("evidence:test"),),
        ).to_jsonld(),
        CORE_CONTEXT,
    )

    projection = memory_node_projection(document)

    assert projection == {
        "memory_type": "Preference",
        "scope": "ctx:user",
        "status": "active",
        "statement": "The user prefers concise summaries.",
        "summary": None,
        "source_authority": "explicit_user_instruction",
        "verification_status": "user_confirmed",
        "confidence_level": "high",
        "applies_when": None,
        "do_not_apply_when": None,
        "created_at": now,
        "updated_at": now,
        "stale_after": None,
        "expires_at": None,
    }


def test_memory_node_projection_skips_incomplete_memory_documents() -> None:
    document = normalize_jsonld_document(
        {
            "@context": CORE_CONTEXT,
            "@id": "preference:incomplete",
            "@type": [memory.PREFERENCE.name],
            memory.STATEMENT.name: "Prefer concise summaries.",
        },
        CORE_CONTEXT,
    )

    assert memory_node_projection(document) is None


def test_relation_projection_extracts_edges_from_any_source_document() -> None:
    document = normalize_jsonld_document(
        {
            "@context": CORE_CONTEXT,
            "@id": "evidence:test",
            "@type": ["Evidence"],
            memory.SUPPORTS.name: {"@id": "preference:test"},
            "rt:provides": {"@id": "evidence:other"},
        },
        CORE_CONTEXT,
    )

    assert set(iter_relation_rows(document)) == {
        ("supports", "preference:test"),
        ("rt:provides", "evidence:other"),
    }


def test_relation_projection_excludes_non_allowlisted_id_fields() -> None:
    document = normalize_jsonld_document(
        {
            "@context": CORE_CONTEXT,
            "@id": "preference:test",
            "@type": [memory.PREFERENCE.name],
            memory.SUPPORTS.name: {"@id": "evidence:keep"},
            "someNewLink": {"@id": "preference:ghost"},
        },
        CORE_CONTEXT,
    )

    assert set(iter_relation_rows(document)) == {
        ("supports", "evidence:keep"),
    }
