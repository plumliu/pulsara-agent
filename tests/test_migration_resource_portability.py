"""Checkout line endings cannot change baseline SQL content or execution."""
from hashlib import sha256
from importlib.resources import files

import pytest

from pulsara_agent.storage.migrations import registry


@pytest.mark.parametrize("line_ending", [b"\n", b"\r\n"])
def test_text_resources_have_the_existing_lf_identity_and_execution(tmp_path, monkeypatch, line_ending):
    definition = registry.POSTGRES_BASELINE
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
    assert definition.expected_sha256 == sha256(canonical[definition.resource_name]).hexdigest()
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
