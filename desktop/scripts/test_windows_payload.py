"""The Windows builder verifies immutable native-Linux inputs before packaging."""
import hashlib
import json

import pytest

from prepare_windows_resources import validate_payload


def test_payload_accepts_exact_version_architecture_and_archive_bytes(tmp_path):
    archive = tmp_path / "payload.tar"
    archive.write_bytes(b"fixed native resource archive")
    expected = hashlib.sha256(archive.read_bytes()).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps({"version": "0.1.0-alpha.6", "platform": "linux-x86_64", "sha256": expected}))
    validate_payload(tmp_path, "0.1.0-alpha.6")
    archive.write_bytes(b"changed bytes")
    with pytest.raises(ValueError, match="checksum"):
        validate_payload(tmp_path, "0.1.0-alpha.6")


@pytest.mark.parametrize("version,platform", [("other", "linux-x86_64"), ("0.1.0-alpha.6", "win32"), ("0.1.0-alpha.6", "linux-aarch64")])
def test_payload_refuses_wrong_build_inputs(tmp_path, version, platform):
    (tmp_path / "manifest.json").write_text(json.dumps({"version": version, "platform": platform, "sha256": "a" * 64}))
    with pytest.raises(ValueError, match="version and architecture"):
        validate_payload(tmp_path, "0.1.0-alpha.6")
