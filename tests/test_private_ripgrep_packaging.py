"""Actual offline source build and immutable dependency preparation boundaries."""
from __future__ import annotations
import importlib.util
from pathlib import Path
import platform
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
    for name in ['pyproject.toml', 'README.md', 'mise.toml', '.gitignore']:
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / 'tools', source / 'tools', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'src', source / 'src', ignore=shutil.ignore_patterns('_vendor', '__pycache__', 'static'))
    frontend = source / 'frontend'
    dependency = frontend / 'node_modules' / 'fixture-package'
    dependency.mkdir(parents=True)
    (dependency / 'index.js').write_text('installed dependency must not ship')
    (frontend / 'package.json').write_text('{"name":"source-fixture"}')
    for name in ('desktop/node_modules/fixture-package/index.js',
                 'desktop/resources/python/bin/python3',
                 'desktop/release/fixture.dmg'):
        artifact = source / name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text('local build artifact must not ship')
    for name in ('frontend/package-lock.json', 'desktop/package.json', 'desktop/package-lock.json'):
        artifact = source / name
        artifact.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, artifact)
    identity_source = source / 'desktop/src/identity-core.mjs'
    identity_source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'desktop/src/identity-core.mjs', identity_source)
    # Each real backend entry point fails offline before explicit preparation.
    for expression in ["build_wheel('dist')", "build_editable('dist')", "build_sdist('dist')"]:
        command = [sys.executable, '-c', f"from hatchling.build import *; {expression}"]
        failed = subprocess.run(command, cwd=source, capture_output=True, text=True)
        assert failed.returncode != 0
        assert 'prepare_ripgrep.py explicitly' in failed.stderr
    shutil.copytree(ROOT / 'src/pulsara_agent/_vendor', source / 'src/pulsara_agent/_vendor')
    prepared_identity = source / 'src/pulsara_agent/_vendor/code_mode/identity-core.mjs'
    prepared_identity.write_text('outdated prepared identity vocabulary')
    stale = subprocess.run(
        [sys.executable, '-c', "from hatchling.build import build_editable; build_editable('editable')"],
        cwd=source, capture_output=True, text=True,
    )
    assert stale.returncode != 0
    assert 'prepare_code_mode.py explicitly' in stale.stderr
    prepared_identity.write_bytes(identity_source.read_bytes())
    # Editable setup precedes renderer generation in the production task graph.
    editable = subprocess.run(
        [sys.executable, '-c', "from hatchling.build import build_editable; print(build_editable('editable'))"],
        cwd=source, capture_output=True, text=True,
    )
    assert editable.returncode == 0, editable.stderr
    missing_renderer = subprocess.run(
        [sys.executable, '-c', "from hatchling.build import build_wheel; build_wheel('dist')"],
        cwd=source, capture_output=True, text=True,
    )
    assert missing_renderer.returncode != 0
    assert 'npm --prefix frontend run build:desktop' in missing_renderer.stderr
    renderer = source / 'src/pulsara_agent/desktop_app/static'
    renderer_files = {
        'index.html': b'<script src="/assets/ui.js"></script>',
        'assets/ui.js': b'window.pulsara = true;',
        'assets/ui.css': b'body { color: black; }',
        'assets/font.woff2': b'fixture font',
        'assets/font-licenses/OFL.txt': b'fixture license',
    }
    for name, content in renderer_files.items():
        path = renderer / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    # Build both directly from the prepared tree: the intentionally vendor-free
    # sdist cannot be implicitly used as a prepared wheel source by uv.
    built = subprocess.run(
        ['uv', 'build', '--offline', '--no-build-isolation', '--sdist', '--wheel', '--python', sys.executable],
        cwd=source, capture_output=True, text=True,
    )
    assert built.returncode == 0, built.stderr
    wheel = next((source / 'dist').glob('*.whl'))
    expected = {
        ('Darwin', 'arm64'): 'macosx_11_0_arm64',
        ('Linux', 'x86_64'): 'linux_x86_64',
        ('Linux', 'aarch64'): 'linux_aarch64',
    }[(platform.system(), platform.machine())]
    assert wheel.name.endswith(f'py3-none-{expected}.whl')
    with zipfile.ZipFile(wheel) as archive:
        info = archive.getinfo('pulsara_agent/_vendor/ripgrep/rg')
        assert info.external_attr >> 16 & 0o111
        assert archive.read('pulsara_agent/_vendor/ripgrep/LICENSE-MIT')
        assert archive.read('pulsara_agent/_vendor/ripgrep/COPYING')
        assert archive.read('pulsara_agent/_vendor/ripgrep/LICENSE-PCRE2')
        assert archive.read('pulsara_agent/_vendor/code_mode/identity-core.mjs') == identity_source.read_bytes()
        prefix = 'pulsara_agent/desktop_app/static/'
        assert {name.removeprefix(prefix) for name in archive.namelist() if name.startswith(prefix)} == set(renderer_files)
        for name, content in renderer_files.items():
            assert archive.read(prefix + name) == content
    installed = tmp_path / 'isolated installed wheel'
    installation = subprocess.run(
        ['uv', 'pip', 'install', '--offline', '--no-deps', '--python', sys.executable,
         '--target', str(installed), str(wheel)],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert installation.returncode == 0, installation.stdout + installation.stderr
    # The packaged worker must use the shipped shared module without reaching
    # into desktop/dist or any repository node_modules directory.
    worker = subprocess.run([sys.executable, '-I', '-c', '''
import asyncio
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
import pulsara_agent.code_mode.host as module
from pulsara_agent.code_mode import CodeModeHost, CodeModeToolResult
from pulsara_agent.identity import is_valid_id, random_id
assert Path(module.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())

async def main():
    host = CodeModeHost()
    requests, output = [], []
    original = host._accept_request
    def accept(cell, message):
        requests.append((message['type'], message['request_id']))
        original(cell, message)
    host._accept_request = accept
    async def invoke(call):
        return CodeModeToolResult(value=call.sequence)
    try:
        cell_id = await host.start(source='try { await tools.read_flie({}); } catch (error) { console.log(error.message); } return await tools.read_file({});',
            source_exec_attempt_id=random_id('attempt'), turn_id=random_id('turn'),
            snapshot=object(), tool_catalog={'decide': {}, 'read_file': {}}, invoke=invoke,
            decision_available=False)
        cell = host._cells[cell_id]
        async with asyncio.timeout(15):
            while True:
                lease = await host.lease_output(cell_id) if cell_id in host._cells else None
                if lease is not None:
                    output.append(dict(lease.record))
                    await host.acknowledge_output(lease)
                if cell.terminal_event.is_set() and not cell.pending and not cell.output:
                    break
                await asyncio.sleep(.001)
        assert cell.state == 'completed', cell.error
        assert output == [
            {'kind': 'log', 'text': 'Unknown tool "read_flie". Did you mean "read_file"?'},
            {'kind': 'return', 'text': '1'},
        ], output
        assert [kind for kind, _ in requests] == ['name_hint', 'call'], requests
        assert all(is_valid_id(identity, 'request') for _, identity in requests), requests
        assert len({identity for _, identity in requests}) == 2, requests
    finally:
        await host.aclose()
asyncio.run(main())
''', str(installed)], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert worker.returncode == 0, worker.stdout + worker.stderr
    with zipfile.ZipFile(next((source / 'editable').glob('*.whl'))) as archive:
        assert not any('/desktop_app/static/' in name for name in archive.namelist())
    with tarfile.open(next((source / 'dist').glob('*.tar.gz'))) as archive:
        names = archive.getnames()
        assert not any('_vendor/ripgrep' in name for name in names)
        assert not any('node_modules' in name.split('/') for name in names)
        assert not any('/desktop/resources/' in name or '/desktop/release/' in name for name in names)
        assert not any('/desktop_app/static/' in name for name in names)
        assert any(name.endswith('frontend/package.json') for name in names)
        assert any(name.endswith('frontend/package-lock.json') for name in names)
        assert any(name.endswith('desktop/package-lock.json') for name in names)
        assert any(name.endswith('tools/prepare_ripgrep.py') for name in names)
        assert any(name.endswith('desktop/src/identity-core.mjs') for name in names)


def test_prepare_checksum_failure_writes_no_resources(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('prepare_ripgrep', ROOT / 'tools/prepare_ripgrep.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import io
    monkeypatch.setattr(module.urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(b'corrupted immutable asset'))
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        module.prepare(tmp_path)
    assert not (tmp_path / 'src').exists()


@pytest.mark.parametrize(('machine', 'metadata', 'error'), [
    ('x86_64', 'Class: ELF64\nMachine: AArch64\n', 'architecture'),
    ('aarch64', 'Class: ELF32\nMachine: AArch64\n', 'architecture'),
    ('aarch64', 'Class: ELF64\nMachine: AArch64\n INTERP ', 'system loader'),
    ('x86_64', 'Class: ELF64\nMachine: Advanced Micro Devices X86-64\n (NEEDED) libc.so', 'shared libraries'),
])
def test_linux_build_rejects_foreign_or_dynamically_linked_rg(tmp_path, monkeypatch, machine, metadata, error):
    spec = importlib.util.spec_from_file_location('hatch_build', ROOT / 'tools/hatch_build.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *args, **kwargs: metadata.encode())
    with pytest.raises(ValueError, match=error):
        module._validate_executable_platform(tmp_path / 'rg', ('Linux', machine))
