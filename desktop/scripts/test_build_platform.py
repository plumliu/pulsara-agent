"""Build admission follows architecture and dependencies, not Ubuntu releases."""

import pytest

import prepare_resources as resources


@pytest.mark.parametrize("version,architecture,kernel", [
    ("22.04", "x86_64", "6.8.0-generic"),
    ("26.04", "x86_64", "6.18.33.2-microsoft-standard-WSL2"),
    ("26.04", "aarch64", "6.8.0-generic"),
    (None, "x86_64", "6.8.0-generic"),
])
def test_ubuntu_release_does_not_gate_native_or_wsl_builds(
    monkeypatch, version, architecture, kernel,
):
    monkeypatch.setattr(resources.sys, "platform", "linux")
    monkeypatch.setattr(resources.platform, "machine", lambda: architecture)
    monkeypatch.setattr(resources.platform, "release", lambda: kernel)
    release = {"ID": "ubuntu"}
    if version is not None:
        release["VERSION_ID"] = version
    monkeypatch.setattr(resources.platform, "freedesktop_os_release", lambda: release)

    assert resources.target_platform() == ("linux", architecture)


def test_non_ubuntu_build_still_requires_an_explicit_dependency_path(monkeypatch):
    monkeypatch.setattr(resources.sys, "platform", "linux")
    monkeypatch.setattr(resources.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(resources.platform, "freedesktop_os_release", lambda: {"ID": "debian"})

    with pytest.raises(RuntimeError, match="must be built on Ubuntu"):
        resources.target_platform()


@pytest.mark.parametrize("platform,architecture", [("linux", "riscv64"), ("win32", "AMD64")])
def test_unsupported_native_targets_remain_rejected(monkeypatch, platform, architecture):
    monkeypatch.setattr(resources.sys, "platform", platform)
    monkeypatch.setattr(resources.platform, "machine", lambda: architecture)

    with pytest.raises(RuntimeError, match="Build natively"):
        resources.target_platform()


@pytest.mark.parametrize("version,accepted", [("14.7", False), ("15.0", True)])
def test_macos_pg_search_minimum_remains_enforced(monkeypatch, version, accepted):
    monkeypatch.setattr(resources.sys, "platform", "darwin")
    monkeypatch.setattr(resources.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(resources.platform, "mac_ver", lambda: (version, (), "arm64"))

    if accepted:
        assert resources.target_platform() == ("darwin", "arm64")
    else:
        with pytest.raises(RuntimeError, match="requires macOS 15 or newer"):
            resources.target_platform()
