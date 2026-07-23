from pulsara_agent.storage import MEMORY_SUBSTRATE_TABLES
from pulsara_agent.storage.migrations.manifest import (
    POSTGRES_LATEST_SCHEMA_MANIFEST,
)
from pulsara_agent.storage.migrations.registry import (
    POSTGRES_MIGRATION_REGISTRY,
)


def test_memory_substrate_is_owned_by_cumulative_manifest() -> None:
    relation_names = {
        str(item["relation_name"])
        for item in POSTGRES_LATEST_SCHEMA_MANIFEST.owned_relations
    }
    assert set(MEMORY_SUBSTRATE_TABLES) <= relation_names


def test_memory_manifest_freezes_vector_and_function_contracts() -> None:
    manifest = POSTGRES_LATEST_SCHEMA_MANIFEST
    assert tuple(
        item["extension_name"] for item in manifest.required_extensions
    ) == ("vector",)
    assert tuple(item["type_name"] for item in manifest.required_types) == (
        "vector",
    )
    assert tuple(
        item["function_name"] for item in manifest.required_functions
    ) == ("pulsara_jsonb_text_array",)
    vector_relation = next(
        item
        for item in manifest.owned_relations
        if item["relation_name"] == "memory_vector_index"
    )
    embedding = next(
        item for item in vector_relation["columns"] if item["column_name"] == "embedding"
    )
    assert embedding["type_schema"] == "public"
    assert embedding["type_name"] == "vector"
    assert embedding["type_modifier"] == 1024


def test_migration_resources_are_checksum_verified() -> None:
    POSTGRES_MIGRATION_REGISTRY.verify_resources()
