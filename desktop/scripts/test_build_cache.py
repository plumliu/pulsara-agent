"""Pulsara's resource boundaries through real mise, uv, and wheel installation.

The graph fixture replaces expensive native actions, not mise's freshness engine.
The wheel test uses real uv/hatchling and a disposable Python environment.
"""
from __future__ import annotations

import json
import os
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
    (package / "__init__.py").write_text('VALUE = "first"\n')
    obsolete = package / "obsolete.py"
    obsolete.write_text("obsolete = True\n")
    resources.prepare_application()
    (package / "__init__.py").write_text('VALUE = "second"\n')
    obsolete.unlink()
    resources.prepare_application()
    code = 'import importlib.util,probe; assert probe.VALUE == "second"; assert importlib.util.find_spec("probe.obsolete") is None'
    subprocess.run([str(target / "python/bin/python3"), "-I", "-c", code], check=True)
