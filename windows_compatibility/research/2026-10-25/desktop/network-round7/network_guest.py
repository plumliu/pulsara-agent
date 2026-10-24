"""Reuse root's real HTTP owners; add only finite network-control workloads."""

import asyncio
from datetime import datetime, timedelta, timezone
import functools  # noqa: F401 -- preserve executed research snapshot
import json
import os
from pathlib import Path
import runpy
import shutil
import ssl  # noqa: F401 -- preserve executed research snapshot
import subprocess
import sys  # noqa: F401 -- preserve executed research snapshot

ROOT = Path(
    "/mnt/c/python_workspace/pulsara-agent/output/windows-g0/root/round5/http_guest.py"
)
pending = []
real_run = asyncio.run
asyncio.run = lambda coroutine: pending.append(coroutine)
try:
    namespace = runpy.run_path(str(ROOT), run_name="g0_network_root_fixture")
finally:
    asyncio.run = real_run
assert len(pending) == 1
ns = namespace["main"].__globals__
home = ns["HOME"]
assert (
    home.resolve() == home
    and home.parent == Path.home()
    and home.name.startswith(".g0-http-")
)
original_emit = ns["emit"]


def emit(event, **values):
    if event == "inspect":
        # The synthetic upload contents are uninteresting; retain exact size,
        # modes, uid and stage names without repeating base64 at every poll.
        values["imports"] = [
            {k: v for k, v in f.items() if k != "contentBase64"}
            for f in values["imports"]
        ]
    original_emit(
        event,
        pid=os.getpid(),
        starttime=Path("/proc/self/stat").read_text().rsplit(")", 1)[1].split()[19],
        timeMs=int(datetime.now(timezone.utc).timestamp() * 1000),
        **{key: value for key, value in values.items() if key != "pid"},
    )


ns["emit"] = emit
original_subprocess_run = subprocess.run
validity = {}


def openssl(args, *a, **kw):
    if isinstance(args, list) and args[:2] == ["openssl", "req"] and "-x509" in args:
        modified = list(args)
        modified.remove("-x509")
        index = modified.index("-days")
        del modified[index : index + 2]
        cert = Path(args[args.index("-out") + 1])
        csr = cert.with_suffix(".csr")
        modified[modified.index("-out") + 1] = str(csr)
        original_subprocess_run(modified, *a, **kw)
        now = datetime.now(timezone.utc)
        before = now - timedelta(minutes=1)
        after = now + timedelta(seconds=15)
        signed = [
            "openssl",
            "x509",
            "-req",
            "-in",
            str(csr),
            "-signkey",
            str(args[args.index("-keyout") + 1]),
            "-out",
            str(cert),
            "-not_before",
            before.strftime("%Y%m%d%H%M%SZ"),
            "-not_after",
            after.strftime("%Y%m%d%H%M%SZ"),
        ]
        result = original_subprocess_run(signed, *a, **kw)
        csr.unlink()
        validity.update(validFrom=before.isoformat(), validTo=after.isoformat())
        emit("natural-certificate-window", **validity)
        return result
    return original_subprocess_run(args, *a, **kw)


subprocess.run = openssl
server_class = ns["LocalHttpServer"]
original_start = server_class.start
active = {}
sequence = 0


async def start(server):
    from aiohttp import web

    async def stream(request):
        global sequence
        kind = request.match_info["kind"]
        token = str(id(request))
        active[token] = kind
        transport = request.transport
        emit(
            "network-handler-opened",
            kind=kind,
            token=token,
            active=list(active.values()),
        )
        try:
            if kind == "pending":
                # Read-only control workload; no product mutator is cancelled or
                # replayed. Observe the actual socket close instead of copying API logic.
                while transport and not transport.is_closing():
                    await asyncio.sleep(0.05)
                raise asyncio.CancelledError
            headers = {"Cache-Control": "no-store"}
            if kind == "sse":
                headers["Content-Type"] = "text/event-stream"
            else:
                headers.update(
                    {
                        "Content-Type": "application/octet-stream",
                        "Content-Disposition": 'attachment; filename="n7-controlled.bin"',
                        "Content-Length": str(64 * 1024 * 1024),
                    }
                )
            response = web.StreamResponse(headers=headers)
            await response.prepare(request)
            count = 0
            while True:
                if kind == "sse":
                    sequence += 1
                    value = f"data: {json.dumps(dict(pid=os.getpid(), seq=sequence, timeMs=int(datetime.now(timezone.utc).timestamp() * 1000)))}\n\n".encode()
                else:
                    value = b"N" * 65536
                await response.write(value)
                count += len(value)
                if kind != "sse" and count >= 64 * 1024 * 1024:
                    break
                await asyncio.sleep(0.1)
            return response
        except (ConnectionResetError, BrokenPipeError):
            raise asyncio.CancelledError
        finally:
            active.pop(token, None)
            emit(
                "network-handler-closed",
                kind=kind,
                token=token,
                transportClosing=bool(transport is None or transport.is_closing()),
                active=list(active.values()),
            )

    server._app.router.add_get("/g0/{kind:sse|download|pending}", stream)
    return await original_start(server)


server_class.start = start


async def main():
    try:
        await pending[0]
    finally:
        subprocess.run = original_subprocess_run
        server_class.start = original_start
        assert not active, active
        assert (
            home.resolve() == home
            and home.parent == Path.home()
            and home.name.startswith(".g0-http-")
            and home.stat().st_uid == os.getuid()
        )
        shutil.rmtree(home)
        emit("network-private-home-cleaned", home=str(home), exists=home.exists())


real_run(main())
