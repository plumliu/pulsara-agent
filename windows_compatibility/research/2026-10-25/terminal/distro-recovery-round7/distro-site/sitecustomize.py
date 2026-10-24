"""Output-only gates around the actual bundled DesktopApplication methods."""

import asyncio
import json
import os
from pathlib import Path
import threading
import time

record = os.environ.get("G0_KERNEL_RECORD")
if record:
    home = Path(os.environ["PULSARA_HOME"])
    mode = os.environ.get("G0_KERNEL_MODE", "normal")
    gate = os.environ.get("G0_KERNEL_CLOSE_GATE") == "1"
    log_lock = threading.Lock()

    def log(event, **facts):
        with log_lock:
            with open(record, "a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(
                        dict(
                            event=event,
                            time=time.time(),
                            pid=os.getpid(),
                            pidNamespace=os.readlink("/proc/self/ns/pid"),
                            starttime=Path("/proc/self/stat")
                            .read_text()
                            .rsplit(")", 1)[1]
                            .split()[19],
                            **facts,
                        )
                    )
                    + "\n"
                )

    log("kernel-process-started", home=str(home))
    from pulsara_agent.desktop_app.application import DesktopApplication

    original_start = DesktopApplication.start
    original_close = DesktopApplication.aclose

    async def start(application):
        log("original-application-start-entered")
        if mode == "unicode-failure":
            raise RuntimeError("G0 中文：启动故障")
        await original_start(application)
        log("original-application-start-returned", origin=application.origin)

    async def close(application):
        log("original-application-close-entered")
        if gate:
            (home / "close.entered").touch()
            while not (home / "close.release").exists():
                await asyncio.sleep(0.01)
        await original_close(application)
        log("original-application-close-returned")
        if mode == "close-failure":
            raise RuntimeError("G0 中文：收尾故障")

    DesktopApplication.start = start
    DesktopApplication.aclose = close
