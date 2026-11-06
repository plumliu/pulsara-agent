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
    try:
        return subprocess.run(
            [str(value) for value in args],
            env=env,
            cwd=cwd,
            input=input,
            text=True,
            check=True,
            capture_output=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        print(exc.stdout)
        print(exc.stderr)
        raise


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
        code = r"""
import importlib.metadata
import pulsara_agent.desktop_runtime
import tempfile
from pathlib import Path
from fractions import Fraction
from time import monotonic
import av
from PIL import Image
from pulsara_agent.media.source import MediaSource, probe_media
from pulsara_agent.media.audio import extract_audio
from pulsara_agent.media.subtitles import read_subtitles
from pulsara_agent.media.frames import FrameBudget, read_video_frames
from pulsara_agent.media.worker import MediaWorker
from importlib.resources import files
async def check_media():
    owner=MediaWorker()
    try:
        with tempfile.TemporaryDirectory() as temporary:
            audio=Path(temporary).resolve()/'known speech.wav'
            audio.write_bytes(files('pulsara_agent.media').joinpath('resources/connection-test.wav').read_bytes())
            with MediaSource.open(audio) as source:
                info=await owner.run(probe_media,source,deadline_monotonic=monotonic()+30)
                assert info.audio_stream_index is not None and info.video_stream_index is None
                extracted=await owner.run(extract_audio,source,info,0,info.duration,1000000,deadline_monotonic=monotonic()+30)
                assert extracted.data.startswith(b'RIFF') and extracted.sound.state=='sound_present'
            video=Path(temporary).resolve()/'real pts.mkv'
            caption=video.with_suffix('.srt')
            caption.write_text('1\n00:00:00,000 --> 00:00:00,200\nEmbedded subtitle\n')
            with av.open(str(caption)) as subtitle_source, av.open(str(video),'w') as container:
                stream=container.add_stream('ffv1',rate=25)
                stream.width,stream.height,stream.pix_fmt=160,90,'yuv420p'
                stream.time_base=Fraction(1,1000)
                subtitle_stream=container.add_stream_from_template(subtitle_source.streams[0])
                for pts,color in [(0,'red'),(40,'blue'),(160,'red')]:
                    frame=av.VideoFrame.from_image(Image.new('RGB',(160,90),color))
                    frame.pts,frame.time_base=pts,Fraction(1,1000)
                    for packet in stream.encode(frame): container.mux(packet)
                for packet in stream.encode(): container.mux(packet)
                for packet in subtitle_source.demux():
                    if packet.dts is not None:
                        packet.stream=subtitle_stream
                        container.mux(packet)
            with MediaSource.open(video) as source:
                info=await owner.run(probe_media,source,deadline_monotonic=monotonic()+30)
                result=await owner.run(read_video_frames,source,info,start=0,end=info.duration,mode='overview',crop=None,budget=FrameBudget(1000000,1280*720,2,32),deadline_monotonic=monotonic()+30)
                assert result.images and result.processed_range==(0,info.duration)
                subtitle=await owner.run(read_subtitles,source,info,0,info.duration,maximum_text_bytes=10000,deadline_monotonic=monotonic()+30)
                assert subtitle.usable and subtitle.entries[0].text=='Embedded subtitle'
    finally:
        await owner.aclose()
    assert owner.physical_close_complete
import asyncio
asyncio.run(check_media())
print('Relocated PyAV/FFmpeg decode, VFR frames, embedded text subtitles, WAV resampling, isolated media workers and bundled speech passed')
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content('<title>Relocated Pulsara</title><p>desktop</p>')
    assert page.title() == 'Relocated Pulsara'
    browser.close()
import asyncio
from pulsara_agent.code_mode import CodeModeHost
from pulsara_agent.identity import random_id
async def check_vm():
    owner=CodeModeHost()
    async def no_tool(_):
        raise AssertionError('unexpected tool dispatch')
    try:
        cell=await owner.start(source="return 'relocated QuickJS';", source_exec_attempt_id=random_id('attempt'),
            turn_id=random_id('turn'), snapshot=None, tool_catalog={'decide': {}}, invoke=no_tool,
            decision_available=False)
        records=[]
        async with asyncio.timeout(10):
            state=await owner.wait_state(cell,10_000)
            assert state['state']=='completed',state
            while cell in owner.pending_output_cells():
                lease=await owner.lease_output(cell)
                assert lease is not None
                records.append(dict(lease.record))
                await owner.acknowledge_output(lease)
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
