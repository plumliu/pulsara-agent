"""Release boundaries: committed versions, exact assets, and draft-only uploads."""
from __future__ import annotations

import hashlib
import json
import shutil

import pytest

from desktop.scripts import release_artifacts as release


@pytest.fixture
def release_root(tmp_path):
    root = tmp_path / "repository"
    for name in (
        "desktop/package.json", "desktop/package-lock.json", "desktop/release-notes.md",
        "frontend/package.json", "frontend/package-lock.json", "pyproject.toml", "uv.lock",
        "src/pulsara_agent/__init__.py",
    ):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(release.ROOT / name, destination)
    return root


def test_versions_match_real_committed_metadata_and_tag(release_root):
    assert release.check_versions(release_root, "v0.1.0-alpha.1") == "0.1.0-alpha.1"
    with pytest.raises(ValueError, match="Tag"):
        release.check_versions(release_root, "v0.1.0-alpha.2")
    path = release_root / "src/pulsara_agent/__init__.py"
    path.write_text('__version__ = "0.1.0a2"\n')
    with pytest.raises(ValueError, match="CLI/runtime"):
        release.check_versions(release_root)


def test_assembly_requires_all_three_installers_and_hashes_actual_bytes(release_root, tmp_path):
    source, output = tmp_path / "artifacts", tmp_path / "output"
    names = release.installer_names("0.1.0-alpha.1")
    for index, name in enumerate(names):
        path = source / f"runner-{index}" / name
        path.parent.mkdir(parents=True)
        path.write_bytes(f"installer {index}".encode())
    files = release.assemble(release_root, source, output, "v0.1.0-alpha.1", "plumliu/pulsara-agent")
    assert [path.name for path in files] == [*names, "SHA256SUMS"]
    for line, name in zip((output / "SHA256SUMS").read_text().splitlines(), names):
        assert line == f"{hashlib.sha256((output / name).read_bytes()).hexdigest()}  {name}"
    notes = (output / "release-notes.md").read_text()
    assert "{{" not in notes
    assert all(f"/releases/download/v0.1.0-alpha.1/{name}" in notes for name in names)
    assert "仍待人工验收" in notes
    extra = source / "unsupported.AppImage"
    extra.write_bytes(b"unexpected")
    with pytest.raises(ValueError, match="exactly"):
        release.assemble(release_root, source, output, "v0.1.0-alpha.1", "plumliu/pulsara-agent")
    extra.unlink()
    (source / "runner-2" / names[2]).unlink()
    with pytest.raises(ValueError, match="exactly"):
        release.assemble(release_root, source, output, "v0.1.0-alpha.1", "plumliu/pulsara-agent")


@pytest.mark.parametrize("existing", [None, True, False])
def test_github_draft_create_retry_and_published_refusal(tmp_path, monkeypatch, existing):
    tag = "v0.1.0-alpha.1"
    remote = None if existing is None else {
        "tag_name": tag, "draft": existing, "prerelease": True,
        "assets": [], "html_url": "https://github.com/plumliu/pulsara-agent/releases/tag/test",
    }
    calls = []
    files = []
    for name in (*release.installer_names("0.1.0-alpha.1"), "SHA256SUMS"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        files.append(path)
    notes = tmp_path / "notes.md"
    notes.write_text("draft notes")

    def gh(*args):
        nonlocal remote
        calls.append(args)
        if args[0] == "api":
            assert "--paginate" in args and "--slurp" in args
            return json.dumps([[remote] if remote else []])
        if args[:2] == ("release", "create"):
            assert "--draft" in args and "--prerelease" in args and "--verify-tag" in args
            assert "--latest=false" in args and args[args.index("--notes-file") + 1] == str(notes)
            remote = {"tag_name": tag, "draft": True, "prerelease": True, "assets": [], "html_url": "draft-url"}
        elif args[:2] == ("release", "edit"):
            assert not any(arg.startswith("--draft") for arg in args)
        elif args[:2] == ("release", "upload"):
            assert remote["draft"] is True
            assert all(str(path) in args for path in files)
            remote["assets"] = [{"name": path.name, "size": path.stat().st_size} for path in files]
        else:
            raise AssertionError(args)
        return ""

    monkeypatch.setattr(release, "gh", gh)
    if existing is False:
        with pytest.raises(ValueError, match="Published"):
            release.upload_draft("plumliu/pulsara-agent", tag, files, notes)
        assert all(call[0] == "api" for call in calls)
    else:
        assert release.upload_draft("plumliu/pulsara-agent", tag, files, notes) == remote["html_url"]
        assert remote["draft"] is True
        assert any(call[:2] == ("release", "upload") for call in calls)
