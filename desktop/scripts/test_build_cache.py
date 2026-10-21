"""Pulsara's resource boundaries through real mise, uv, and wheel installation.

The graph fixture replaces expensive native actions, not mise's freshness engine.
The wheel test uses real uv/hatchling and a disposable Python environment.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import py_compile
import shlex
import shutil
import subprocess
import sys
import tomllib

import pytest

import prepare_resources as resources


CONFIG = tomllib.loads((resources.REPO / "mise.toml").read_text())
MISE = shutil.which("mise")


@pytest.fixture
def graph(tmp_path):
    assert MISE, "Install mise before running the desktop build integration tests"
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "recorder.py").write_text('''
import json, pathlib, subprocess, sys
name = sys.argv[1]
root = pathlib.Path.cwd()
def write(path, text):
    path = root / path
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text() != text:
        path.write_text(text)
with (root / 'runs.jsonl').open('a') as log:
    log.write(json.dumps(name) + '\\n')
if name == 'desktop:environment':
    write('desktop/build/environment.json', (root / 'compiler.txt').read_text())
elif name == 'desktop:requirements':
    write('desktop/build/requirements.txt', (root / 'runtime.txt').read_text())
else:
    task = json.loads(subprocess.check_output(['mise', 'tasks', 'info', name, '--json']))
    outputs = task['outputs']
    if isinstance(outputs, list):
        for output in outputs:
            if output == 'desktop/resources/playwright':
                (root / output).mkdir(parents=True, exist_ok=True)
            else:
                write(output, name)
if name == 'desktop:application':
    write('desktop/resources/python/current-app.txt', (root / 'app.txt').read_text())
if name == 'desktop:python-dependencies' and (root / 'fail-python').exists():
    sys.exit(1)
''')
    lines = ["[settings.task]", "source_freshness_hash_contents = true"]
    for name, task in CONFIG["tasks"].items():
        lines.append(f"[tasks.{json.dumps(name)}]")
        for key in ("depends", "sources", "outputs"):
            if key in task:
                lines.append(f"{key} = {json.dumps(task[key])}")
        command = f"{shlex.quote(sys.executable)} recorder.py {shlex.quote(name)}"
        lines.append(f"run = {json.dumps(command)}")
    (root / "mise.toml").write_text("\n".join(lines) + "\n")
    for name, content in {
        "compiler.txt": "compiler 1", "runtime.txt": "runtime 1", "app.txt": "app 1",
        ".python-version": "3.12.15", "uv.lock": "fixture dev requirements",
        "desktop/scripts/prepare_resources.py": "native recipe 1",
        "tools/prepare_ripgrep.py": "ripgrep recipe 1",
        "frontend/package.json": "{}", "frontend/package-lock.json": "{}",
        "desktop/package.json": "{}", "desktop/package-lock.json": "{}",
    }.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    env = {**os.environ, "MISE_CONFIG_DIR": str(tmp_path / "config"),
           "MISE_STATE_DIR": str(tmp_path / "state"),
           "MISE_CACHE_DIR": str(tmp_path / "cache"),
           "MISE_TRUSTED_CONFIG_PATHS": str(root), "MISE_YES": "1"}

    def run(task="desktop:resources", *, force=False, check=True):
        args = [MISE, "run", "--jobs", "1"]
        if force:
            args.append("--force")
        result = subprocess.run([*args, task], cwd=root, env=env, capture_output=True, text=True)
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    def counts():
        names = [json.loads(line) for line in (root / "runs.jsonl").read_text().splitlines()]
        return {name: names.count(name) for name in CONFIG["tasks"]}

    return root, run, counts


def test_warm_build_and_application_edits_reuse_only_dependency_resources(graph):
    root, run, counts = graph
    run()
    assert counts()["desktop:postgres"] == 1
    run()
    for task in ("postgres", "python-dependencies", "browsers", "ripgrep", "frontend-dependencies", "shell-dependencies"):
        assert counts()[f"desktop:{task}"] == 1
    assert counts()["desktop:application"] == 2
    (root / "app.txt").write_text("new prompt and UI")
    os.utime(root / "app.txt", (1, 1))
    (root / "uv.lock").write_text("dev-only change; runtime export unchanged")
    run()
    assert counts()["desktop:postgres"] == counts()["desktop:python-dependencies"] == 1
    assert (root / "desktop/resources/python/current-app.txt").read_text() == "new prompt and UI"


def test_dependency_and_toolchain_changes_invalidate_their_real_graph_boundaries(graph):
    root, run, counts = graph
    run()
    (root / "runtime.txt").write_text("runtime 2")
    run()
    assert counts()["desktop:postgres"] == 1
    assert counts()["desktop:python-dependencies"] == counts()["desktop:browsers"] == 2
    (root / "compiler.txt").write_text("compiler 2")
    run()
    assert counts()["desktop:postgres"] == 2
    assert counts()["desktop:python-dependencies"] == 3
    # A checkout/tar restore can preserve an old mtime; contents still matter.
    recipe = root / "desktop/scripts/prepare_resources.py"
    recipe.write_text("native recipe 2")
    os.utime(recipe, (1, 1))
    run()
    assert counts()["desktop:postgres"] == 3


def test_missing_outputs_and_failed_builds_cannot_be_treated_as_cache_hits(graph):
    root, run, counts = graph
    run()
    (root / "desktop/resources/postgres/bin/pg_config").unlink()
    shutil.rmtree(root / "desktop/resources/playwright")
    run()
    assert counts()["desktop:postgres"] == counts()["desktop:browsers"] == 2
    (root / "runtime.txt").write_text("runtime 2")
    (root / "fail-python").touch()
    assert run(check=False).returncode != 0
    (root / "fail-python").unlink()
    run()
    assert counts()["desktop:python-dependencies"] == 3
    assert counts()["desktop:browsers"] == 3


def test_real_uv_export_ignores_project_version_and_dev_only_changes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("pyproject.toml", "uv.lock", ".python-version"):
        shutil.copyfile(resources.REPO / name, repo / name)
    monkeypatch.setattr(resources, "REPO", repo)
    monkeypatch.setattr(resources, "ROOT", repo / "desktop")
    resources.export_requirements()
    requirements = repo / "desktop/build/requirements.txt"
    first = requirements.read_text()
    timestamp = requirements.stat().st_mtime_ns
    project = repo / "pyproject.toml"
    metadata = tomllib.loads(project.read_text())
    dev_only = next(item for item in metadata["dependency-groups"]["dev"] if item.startswith("pytest-xdist"))
    project.write_text(project.read_text()
                       .replace(f'version = "{metadata["project"]["version"]}"', 'version = "99.0.0"')
                       .replace(f'    "{dev_only}",\n', ""))
    subprocess.run(["uv", "lock", "--default-index", "https://pypi.org/simple"], cwd=repo, check=True)
    resources.export_requirements()
    assert requirements.read_text() == first
    assert requirements.stat().st_mtime_ns == timestamp
    dependency = next(line for line in project.read_text().splitlines() if '"pillow' in line.lower())
    project.write_text(project.read_text().replace(dependency + "\n", ""))
    subprocess.run(["uv", "lock", "--default-index", "https://pypi.org/simple"], cwd=repo, check=True)
    resources.export_requirements()
    assert requirements.read_text() != first
    assert "pillow==" not in requirements.read_text().lower()


def test_real_wheel_install_always_refreshes_same_version_application(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    package = repo / "src/probe"
    package.mkdir(parents=True)
    (repo / "pyproject.toml").write_text('''
[project]
name = "pulsara-agent"
version = "0.1.0"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["src/probe"]
''')
    (repo / ".venv").symlink_to(resources.REPO / ".venv", target_is_directory=True)
    target = tmp_path / "resources"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(target / "python")], check=True, capture_output=True)
    monkeypatch.setattr(resources, "REPO", repo)
    monkeypatch.setattr(resources, "RESOURCES", target)
    (package / "__init__.py").write_text('VALUE = "first"\ndef location(): pass\n')
    obsolete = package / "obsolete.py"
    obsolete.write_text("obsolete = True\n")
    resources.prepare_application()
    # The wheel is already compiled before the first runtime import. uv owns
    # both compilation and replacement of caches for the same package version.
    cache, = (target / "python/lib").glob("python*/site-packages/probe/__pycache__/__init__.*.pyc")
    first_bytecode = cache.read_bytes()
    (package / "__init__.py").write_text('VALUE = "second"\ndef location(): pass\n')
    obsolete.unlink()
    resources.prepare_application()
    assert cache.read_bytes() != first_bytecode
    relocated = tmp_path / "relocated resources"
    # electron-builder copies extraResources without preserving source mtimes.
    shutil.copytree(target, relocated, symlinks=True, copy_function=shutil.copyfile)
    relocated_cache = relocated / cache.relative_to(target)
    relocated_source = relocated_cache.parent.parent / "__init__.py"
    os.utime(relocated_source, (1, 1))
    bytecode = relocated_cache.read_bytes()
    cache_mtime = relocated_cache.stat().st_mtime_ns
    code = '''
import importlib.util, pathlib, sys
def reject_source_compilation(event, args):
    if event == "compile" and str(args[1]).endswith("/probe/__init__.py"):
        raise AssertionError("the packaged cache must survive source timestamp changes")
sys.addaudithook(reject_source_compilation)
import probe
assert probe.VALUE == "second"
assert importlib.util.find_spec("probe.obsolete") is None
assert pathlib.Path(probe.location.__code__.co_filename) == pathlib.Path(probe.__file__)
'''
    # CPython checks source content and relocates the loaded code filename to
    # its current path. The first runtime import needs neither compile nor write.
    subprocess.run([str(relocated / "python/bin/python3"), "-I", "-B", "-c", code], check=True)
    assert relocated_cache.read_bytes() == bytecode
    assert relocated_cache.stat().st_mtime_ns == cache_mtime


def test_stdlib_checked_hash_survives_builder_copy_and_checks_changed_source(tmp_path):
    # A checkout may itself live under a directory called site-packages. Only
    # this stdlib's own uv subtree belongs to the exclusion owner.
    stdlib = tmp_path / "site-packages" / "prepared" / "lib" / "python3.12"
    stdlib.mkdir(parents=True)
    source = stdlib / "pulsara_stdlib_cache_probe.py"
    source.write_text('VALUE = "first"\ndef location(): pass\n')
    cache = Path(py_compile.compile(
        str(source), doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    ))
    assert int.from_bytes(cache.read_bytes()[4:8], "little") == 0
    assert int.from_bytes(cache.read_bytes()[8:12], "little") == int(source.stat().st_mtime)

    # A valid timestamp cache must be replaced, not accepted by compileall's
    # timestamp freshness shortcut. This runs the real target CPython compiler.
    resources.compile_python_stdlib(Path(sys.executable), stdlib)
    assert int.from_bytes(cache.read_bytes()[4:8], "little") == 3
    packaged = tmp_path / "packaged" / "lib" / "python3.12"
    shutil.copytree(stdlib, packaged, copy_function=shutil.copyfile)
    copied_source = packaged / source.name
    copied_cache = packaged / cache.relative_to(stdlib)
    os.utime(copied_source, (1, 1))
    before = copied_cache.read_bytes()
    before_mtime = copied_cache.stat().st_mtime_ns
    code = '''
import os, pathlib, sys
root = pathlib.Path(sys.argv[1])
source = root / "pulsara_stdlib_cache_probe.py"
sys.path.insert(0, str(root))
def observe(event, args):
    if event == "compile" and args[1] == str(source):
        raise AssertionError("the copied checked-hash cache must avoid source compilation")
    if event == "open" and isinstance(args[0], str) and args[0].startswith(str(root)):
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise AssertionError("the copied checked-hash cache must avoid writes")
sys.addaudithook(observe)
import pulsara_stdlib_cache_probe as probe
assert probe.VALUE == "first"
assert pathlib.Path(probe.location.__code__.co_filename) == source
'''
    # No -B: correctness must prevent compilation/writes rather than suppressing
    # writes. Isolated mode only prevents external Python environment injection.
    subprocess.run([sys.executable, "-I", "-c", code, str(packaged)], check=True)
    assert copied_cache.read_bytes() == before
    assert copied_cache.stat().st_mtime_ns == before_mtime

    copied_source.write_text('VALUE = "other"\ndef location(): pass\n')
    os.utime(copied_source, (1, 1))
    assert copied_source.stat().st_size == source.stat().st_size
    code = '''
import sys
sys.path.insert(0, sys.argv[1])
import pulsara_stdlib_cache_probe as probe
assert probe.VALUE == "other"
'''
    subprocess.run([sys.executable, "-I", "-c", code, str(packaged)], check=True)
    assert copied_cache.read_bytes() != before
    assert int.from_bytes(copied_cache.read_bytes()[4:8], "little") == 3


def test_stdlib_compilation_leaves_uv_site_packages_caches_alone(tmp_path):
    stdlib = tmp_path / "stdlib"
    site = stdlib / "site-packages"
    site.mkdir(parents=True)
    source = site / "uv_cache_probe.py"
    source.write_text("VALUE = 1\n")
    cache = Path(py_compile.compile(str(source), doraise=True))
    before = cache.read_bytes()
    before_mtime = cache.stat().st_mtime_ns
    # Recompiling this unrelated owner would fail. The entire site-packages
    # subtree remains uv's responsibility, including existing cache metadata.
    source.write_text("invalid syntax (\n")
    resources.compile_python_stdlib(Path(sys.executable), stdlib)
    assert cache.read_bytes() == before
    assert cache.stat().st_mtime_ns == before_mtime
