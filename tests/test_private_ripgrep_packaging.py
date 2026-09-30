"""Actual offline source build and immutable dependency preparation boundaries."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_offline_wheel_editable_sdist_require_preparation_and_package_platform_resource(tmp_path):
    source = tmp_path / 'source with spaces'
    source.mkdir()
    for name in ['pyproject.toml', 'README.md']:
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / 'tools', source / 'tools', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'src', source / 'src', ignore=shutil.ignore_patterns('_vendor', '__pycache__'))
    # Each real backend entry point fails offline before explicit preparation.
    for expression in ["build_wheel('dist')", "build_editable('dist')", "build_sdist('dist')"]:
        command = [sys.executable, '-c', f"from hatchling.build import *; {expression}"]
        failed = subprocess.run(command, cwd=source, capture_output=True, text=True)
        assert failed.returncode != 0
        assert 'prepare_ripgrep.py explicitly' in failed.stderr
    shutil.copytree(ROOT / 'src/pulsara_agent/_vendor', source / 'src/pulsara_agent/_vendor')
    for expression in ["build_wheel('dist')", "build_editable('editable')", "build_sdist('dist')"]:
        built = subprocess.run([sys.executable, '-c', f"from hatchling.build import *; print({expression})"], cwd=source, capture_output=True, text=True)
        assert built.returncode == 0, built.stderr
    wheel = next((source / 'dist').glob('*.whl'))
    assert wheel.name.endswith('py3-none-macosx_11_0_arm64.whl')
    with zipfile.ZipFile(wheel) as archive:
        info = archive.getinfo('pulsara_agent/_vendor/ripgrep/rg')
        assert info.external_attr >> 16 & 0o111
        assert archive.read('pulsara_agent/_vendor/ripgrep/LICENSE-MIT')
        assert archive.read('pulsara_agent/_vendor/ripgrep/COPYING')
    with tarfile.open(next((source / 'dist').glob('*.tar.gz'))) as archive:
        assert not any('_vendor/ripgrep' in member.name for member in archive)
        assert any(member.name.endswith('tools/prepare_ripgrep.py') for member in archive)


def test_prepare_checksum_failure_writes_no_resources(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('prepare_ripgrep', ROOT / 'tools/prepare_ripgrep.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import io
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(b'corrupted immutable asset'))
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        module.prepare(tmp_path)
    assert not (tmp_path / 'src').exists()
