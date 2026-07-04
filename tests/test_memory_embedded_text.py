from pulsara_agent.memory.canonical.embedded_text import build_embedded_memory_text


def test_embedded_text_is_deterministic_versioned_and_excludes_echo_fields() -> None:
    node = {
        "memory_type": "Preference",
        "scope": "ctx:user",
        "statement": "  Prefer   concise summaries. ",
        "summary": "Short answers",
        "applies_when": None,
        "do_not_apply_when": "When detail is requested",
    }
    document = {
        "triggerKeywords": ["brief", "concise"],
        "triggerTools": "memory_search",
        "evidence": "must not be embedded",
        "recalled_projection": "must not echo",
        "updatedAt": "2026-01-01T00:00:00Z",
    }

    first = build_embedded_memory_text(node, document=document)
    second = build_embedded_memory_text(node, document=dict(reversed(list(document.items()))))

    assert first == second
    assert "Statement: Prefer concise summaries." in first.text
    assert "Aliases: brief, concise, memory_search" in first.text
    assert "must not" not in first.text
    assert first.builder_version == "memory-embedded-text:v1"
    assert len(first.text_hash) == 64


def test_embedded_text_hash_changes_with_builder_input() -> None:
    before = build_embedded_memory_text(
        {"memory_type": "Preference", "scope": "ctx:user", "statement": "Prefer concise."}
    )
    after = build_embedded_memory_text(
        {"memory_type": "Preference", "scope": "ctx:user", "statement": "Prefer detail."}
    )

    assert before.text_hash != after.text_hash
