"""Exercise prepared payloads after relocation, without the developer environment.

This smoke owns only a disposable PostgreSQL cluster and copied runtime. It never
loads saved Pulsara settings or connects to the user's database.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def run(args, *, env, cwd, input=None):
    return subprocess.run(
        [str(value) for value in args],
        env=env,
        cwd=cwd,
        input=input,
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()


def smoke(resources: Path):
    with tempfile.TemporaryDirectory(
        prefix="pulsara-payload-", dir="/tmp"
    ) as temporary:
        root = Path(temporary)
        payload = root / "relocated resources"
        if sys.platform == "darwin":
            # APFS clones preserve independent files without doubling the build's
            # disk footprint; modifying the copy can never alter the source.
            subprocess.run(["/bin/cp", "-cR", resources, payload], check=True)
        else:
            shutil.copytree(resources, payload, symlinks=True)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("PYTHON", "DYLD_", "LD_", "PULSARA_", "PG"))
        }
        env.update(
            PATH="/usr/bin:/bin",
            HOME=str(root),
            PLAYWRIGHT_BROWSERS_PATH=str(payload / "playwright"),
        )
        versions = json.loads((payload / "versions.json").read_text())
        binaries = payload / "postgres" / "bin"
        for executable in ("postgres", "initdb", "pg_ctl", "psql", "pg_isready"):
            result = run([binaries / executable, "--version"], env=env, cwd=root)
            assert versions["postgresql"] in result, result
        data, sockets = root / "data", root / "socket"
        sockets.mkdir()
        run(
            [
                binaries / "initdb",
                "-D",
                data,
                "-U",
                "pulsara_smoke",
                "--auth=trust",
                "--locale=C",
                "--encoding=UTF8",
            ],
            env=env,
            cwd=root,
        )
        log = root / "postgres.log"
        try:
            run(
                [
                    binaries / "pg_ctl",
                    "-D",
                    data,
                    "-l",
                    log,
                    "-o",
                    f"-k {sockets} -c listen_addresses='' -c shared_preload_libraries=pg_search",
                    "-w",
                    "start",
                ],
                env=env,
                cwd=root,
            )
            sql = """
CREATE EXTENSION vector;
CREATE EXTENSION pg_search;
SELECT extname || '=' || extversion FROM pg_extension WHERE extname IN ('vector', 'pg_search') ORDER BY extname;
CREATE TABLE documents (id bigint PRIMARY KEY, body text, embedding vector(3));
INSERT INTO documents VALUES (1, 'desktop portability check', '[1,2,3]'), (2, 'unrelated example', '[4,5,6]');
CREATE INDEX documents_search ON documents USING bm25 (id, body) WITH (key_field='id');
SELECT 'search=' || count(*) FROM documents WHERE body @@@ pdb.match('portability');
SELECT 'vector=' || (embedding <-> '[1,2,3]'::vector)::text FROM documents WHERE id=1;
"""
            result = run(
                [
                    binaries / "psql",
                    "-XAt",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-h",
                    sockets,
                    "-U",
                    "pulsara_smoke",
                    "postgres",
                ],
                env=env,
                cwd=root,
                input=sql,
            )
            for expected in (
                f"pg_search={versions['pg_search']}",
                f"vector={versions['vector']}",
                "search=1",
                "vector=0",
            ):
                assert expected in result.splitlines(), result
            print(result)
        except Exception:
            if log.exists():
                print(log.read_text())
            raise
        finally:
            if (data / "postmaster.pid").exists():
                run(
                    [binaries / "pg_ctl", "-D", data, "-m", "fast", "-w", "stop"],
                    env=env,
                    cwd=root,
                )
        code = """
import importlib.metadata
import pulsara_agent.desktop_runtime
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content('<title>Relocated Pulsara</title><p>desktop</p>')
    assert page.title() == 'Relocated Pulsara'
    browser.close()
import asyncio
from pulsara_agent.code_mode import CodeModeHost
async def check_vm():
    owner=CodeModeHost()
    async def no_tool(_):
        raise AssertionError('unexpected tool dispatch')
    try:
        cell=await owner.start(source="return 'relocated QuickJS';", source_exec_attempt_id='smoke',
            turn_id='smoke', snapshot=None, tool_catalog={}, invoke=no_tool)
        records=[]
        async with asyncio.timeout(10):
            while True:
                page=await owner.observe(cell,100)
                records.extend(page.output)
                await owner.acknowledge(page)
                if page.state in {'completed','failed','terminated'} and not page.pending_calls and not page.has_more:
                    assert page.state=='completed',page.error
                    break
        assert records==[{'kind':'return','text':'relocated QuickJS'}]
    finally:
        await owner.aclose()
asyncio.run(check_vm())
print('Python application, offline Node/QuickJS and relocated Chromium passed')
"""
        print(
            run(
                [payload / "python" / "bin" / "python3", "-I", "-c", code],
                env=env,
                cwd=root,
            )
        )
        print("Relocated PostgreSQL, vector, pg_search, Python and Chromium passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "resources",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "resources",
    )
    smoke(parser.parse_args().resources.resolve())
