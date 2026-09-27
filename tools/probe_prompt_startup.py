"""Measure queued prompt preparation with saved providers in a disposable DB.

Timing wrappers are probe-local: no durable events or product profiling state.
Run with the repository Python; pass a saved connection ID and an output path.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import uuid4

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.workspace_identity import HostWorkspaceInput

from run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore, _binding, _create_database, _drop_database,
)
from run_pr04_prompt_queue_dogfood import _secrets, _write


def instrument(owner, name, spans, clock):
    original = getattr(owner, name)

    async def measured(*args, **kwargs):
        start = perf_counter()
        try:
            return await original(*args, **kwargs)
        finally:
            spans.append({"phase": name, "start_ms": (start-clock[0])*1000,
                          "duration_ms": (perf_counter()-start)*1000})

    setattr(owner, name, measured)


async def run(args):
    saved = LocalSettingsStore().read()
    connection = next(c for c in saved.model_connections if c.id.value == args.connection_id)
    secrets = _secrets(saved)
    name, _, admin, runtime_dsn = _create_database(saved)
    previous_home = os.environ.get("PULSARA_HOME")
    report = {"connection_id": args.connection_id, "samples": []}
    try:
        with TemporaryDirectory(prefix="pulsara-startup-probe-") as temporary:
            home = Path(temporary)
            os.environ["PULSARA_HOME"] = str(home)
            store = _ReadOnlySettingsStore(replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin)))
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            runtime = ModelRuntime.production(settings=store, catalog=catalog)
            core = KernelHostCore.production(model_runtime=runtime)
            try:
                session = await core.open_session(HostWorkspaceInput(
                    workspace_kind="project", workspace_root=home,
                    trust_workspace_mcp_config=False,
                ))
                await session.attach_controller("startup-probe")
                await session.update_model_call_binding(_binding(runtime, connection))
                spans, clock = [], [perf_counter()]
                for owner, methods in (
                    (session, ("_settle_queued_root_admission",)),
                    (session._runner._provider_dispatch, (
                        "prepare_prospective_root_candidate_family",
                        "prepare_prospective_root_candidate", "measure_prepared_wire_candidate",
                    )),
                    (session._memory_tools, (
                        "freeze_automatic_recall_source", "freeze_response_preference_source",
                        "_parallel_recall", "_run_remote_exact",
                    )),
                ):
                    for method in methods:
                        instrument(owner, method, spans, clock)
                for text in ("你好", "接下来我要测试一下runtime，请只回复PULSARA_OK，不要使用工具。"):
                    spans.clear()
                    clock[0] = perf_counter()
                    receipt = await session.submit_prompt(command_id=f"command:probe:{uuid4().hex}", content=PromptContent.text(text))
                    submit_ms = (perf_counter()-clock[0])*1000
                    if receipt.status == "REJECTED":
                        raise RuntimeError("startup probe prompt was rejected")
                    # This watchdog bounds the diagnostic, not a product turn.
                    async with asyncio.timeout(120):
                        while not any(s["phase"] == "_settle_queued_root_admission" for s in spans):
                            await asyncio.sleep(0.01)
                        task = session._active_task
                        result = await asyncio.shield(task) if task else None
                    if result is None or ("runtime" in text and "PULSARA_OK" not in result.final_text):
                        raise RuntimeError("startup probe did not receive the expected final answer")
                    report["samples"].append({"prompt": text, "submit_ms": submit_ms,
                        "receipt": receipt.status, "spans": list(spans),
                        "final_text": result.final_text if result else None})
            finally:
                await core.shutdown()
    finally:
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        _drop_database(saved, name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    _write(args.output, report, secrets)
    print(args.output.read_text())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
