"""Checkout line endings cannot change clean-v0 identity or SQL execution."""
from hashlib import sha256
from importlib.resources import files

import pytest

from pulsara_agent.storage.migrations import registry
from pulsara_agent.storage.migrations.contracts import build_migration_universe_identity


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_text_resources_have_the_existing_lf_identity_and_execution(tmp_path, monkeypatch, line_ending):
    definition = registry.POSTGRES_MIGRATION_REGISTRY.definition(0)
    resources = {
        "pulsara_agent.storage.migrations.sql": (definition.resource_name,),
        "pulsara_agent.storage.migrations.resources": (definition.catalog_resource_name, definition.grant_resource_name),
    }
    canonical = {}
    roots = {}
    for package, names in resources.items():
        root = tmp_path / package
        root.mkdir()
        roots[package] = root
        for name in names:
            payload = files(package).joinpath(name).read_bytes().replace(b"\r\n", b"\n")
            canonical[name] = payload
            (root / name).write_bytes(payload.replace(b"\n", line_ending))
    monkeypatch.setattr(registry, "files", roots.__getitem__)
    expected = build_migration_universe_identity(
        baseline_sql_sha256=sha256(canonical[definition.resource_name]).hexdigest(),
        catalog_sha256=sha256(canonical[definition.catalog_resource_name]).hexdigest(),
        grant_sha256=sha256(canonical[definition.grant_resource_name]).hexdigest(),
    )
    actual = build_migration_universe_identity(
        baseline_sql_sha256=registry._checksum("pulsara_agent.storage.migrations.sql", definition.resource_name),
        catalog_sha256=registry._checksum("pulsara_agent.storage.migrations.resources", definition.catalog_resource_name),
        grant_sha256=registry._checksum("pulsara_agent.storage.migrations.resources", definition.grant_resource_name),
    )
    assert actual == expected == definition.identity
    assert definition.resource_bytes() == canonical[definition.resource_name]
    assert definition.resource_text() == canonical[definition.resource_name].decode("utf-8")
    definition.verify_auxiliary_resources()

    # Only transport line endings are equivalent. A changed body/manifest is
    # still rejected against the already selected immutable definition.
    baseline = roots["pulsara_agent.storage.migrations.sql"] / definition.resource_name
    baseline.write_bytes(baseline.read_bytes() + b"-- changed SQL body\n")
    with pytest.raises(ValueError, match="SQL checksum mismatch"):
        definition.resource_bytes()
    auxiliary = roots["pulsara_agent.storage.migrations.resources"] / definition.grant_resource_name
    auxiliary.write_bytes(auxiliary.read_bytes() + b" ")
    with pytest.raises(ValueError, match="resource checksum mismatch"):
        definition.verify_auxiliary_resources()
