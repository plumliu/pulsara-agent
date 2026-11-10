"""Real saved models, a real four-minute idle timer, disposable database."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import psycopg
from psycopg.conninfo import make_conninfo

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.direct_model import prepare_conversation_ephemeral_request
from pulsara_agent.conversation_kernel.ephemeral import CACHE_WARMING_PROMPT
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.ephemeral import execute_ephemeral_request
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import ModelCallBinding, ReasoningEffortSelection
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.model_call import ModelCallPurpose
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_ephemeral_products_dogfood import _RecordedBorrow, _snapshot, _assert_isolation
from tests.dogfood.run_model_switch_handover_dogfood import _ReadOnlySettingsStore
from tests.support.postgres_database import create_migrated_postgres_test_database, drop_postgres_test_database


async def run():
    home = require_pulsara_home()
    saved = LocalSettingsStore().read()
    scrubber = secret_scrubber(saved)
    connections = {
        c.target.wire_api.value: c for c in saved.model_connections
        if c.target.model_id == "deepseek-flash" and c.target.route_id == "deepseek"
    }
    if set(connections) != {"openai_chat_completions", "openai_responses"}:
        raise RuntimeError("Saved DeepSeek Chat and Responses connections are required")
    admin = make_conninfo(host=str(home / "desktop/socket"), port=5432,
                         dbname="pulsara", user="pulsara_admin")
    with psycopg.connect(admin) as connection:
        db, address, data = connection.execute(
            "SELECT current_database(),inet_server_addr(),current_setting('data_directory')"
        ).fetchone()
        if db != "pulsara" or address is not None or Path(data).resolve() != (home / "desktop/postgres").resolve():
            raise RuntimeError("Local disposable PostgreSQL owner did not verify")
    original_env = {key: os.environ.get(key) for key in (
        "PULSARA_HOME", "PULSARA_POSTGRES_ADMIN_DSN", "PULSARA_POSTGRES_DSN")}
    os.environ["PULSARA_POSTGRES_ADMIN_DSN"] = admin
    os.environ["PULSARA_POSTGRES_DSN"] = make_conninfo(
        host=str(home / "desktop/socket"), port=5432, dbname="pulsara", user="pulsara_runtime")
    database = None
    original_borrow = ModelRuntime.borrow_transport
    records, results = [], []
    output = Path("output/cache-warming-20261110/dogfood.json").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {"started_at": datetime.now(timezone.utc).isoformat(),
                "records": records, "results": results}
    try:
        database = create_migrated_postgres_test_database()
        catalog = ModelCatalogOwner(ModelsDevCatalogClient())
        await catalog.refresh()
        sessions_by_wire = {}

        class CheckedBorrow(_RecordedBorrow):
            def open_stream(self, *, context):
                before = None
                if self.call.fact.purpose is ModelCallPurpose.CONVERSATION_CACHE_WARMING:
                    before, _ = _snapshot(database.admin_dsn, sessions_by_wire[self.call.target.fact.wire_api])
                execution = super().open_stream(context=context)
                if before is not None:
                    records[-1]["canonical_before_request"] = before
                return execution

        ModelRuntime.borrow_transport = lambda self, permit: CheckedBorrow(
            original_borrow(self, permit), records)
        with TemporaryDirectory(prefix="pulsara-cache-warming-") as tmp:
            root = Path(tmp).resolve()
            isolated_home, workspace = root / "home", root / "workspace"
            isolated_home.mkdir()
            workspace.mkdir()
            os.environ["PULSARA_HOME"] = str(isolated_home)
            settings = _ReadOnlySettingsStore(replace(
                saved, postgres=LocalPostgresConfig(database.runtime_dsn, database.admin_dsn),
                cache_warming_enabled=True, input_suggestions_enabled=False))
            runtime = ModelRuntime.production(settings=settings, catalog=catalog)
            core = KernelHostCore.production(model_runtime=runtime)
            try:
                # Both Hosts wait concurrently for the actual unmodified timer.
                async def exercise(wire, config):
                    binding = ModelCallBinding(config.id, ReasoningEffortSelection("high"))
                    session = await core.open_session(HostWorkspaceInput(
                        workspace_kind="project", workspace_root=workspace,
                        trust_workspace_mcp_config=False),
                        system_prompt="请简洁回答，不调用工具。")
                    try:
                        sessions_by_wire[wire] = session.session_id
                        await session.attach_controller("cache-warming-dogfood")
                        await session.update_model_call_binding(binding)
                        request_started = monotonic()
                        first = await session.run_turn(PromptContent.text(
                            "请用一句话解释模型输入前缀缓存。"))
                        # Finish independent title work before taking the baseline.
                        title_tasks = [t for t in session._ephemeral._tasks
                                       if t is not session._ephemeral._warming_task]
                        await asyncio.gather(*title_tasks)
                        task = session._ephemeral._warming_task
                        assert task is not None and not task.done()
                        idle_start, _ = _snapshot(database.admin_dsn, session.session_id)
                        source_id = first.final_source.compiled_input.context_id
                        first_record = next(r for r in records
                                            if r["purpose"] == "agent_model_loop"
                                            and r["payload"].get("model") == config.target.model_id
                                            and r["wire_api"] == wire)
                        started = monotonic()
                        prepared = prepare_conversation_ephemeral_request(
                            runtime=runtime, source=first.final_source,
                            purpose=ModelCallPurpose.CONVERSATION_CACHE_WARMING,
                            prompt=CACHE_WARMING_PROMPT,
                            timeout_policy=session._model.transport_timeout_policy)
                        observed = []
                        while not observed:
                            await asyncio.sleep(1)
                            observed = [r for r in records if r["purpose"] == "conversation_cache_warming"
                                        and r["wire_api"] == wire and r["terminal"] is not None]
                            if task.done():
                                raise AssertionError(f"warming ended without completed request: {wire}")
                        elapsed = monotonic() - started
                        assert monotonic() - request_started >= 240
                        # The production timer and interval are unmodified; elapsed
                        # here excludes the independent title settlement above.
                        ping = observed[0]
                        assert ping["terminal"]["kind"] == "COMPLETED", ping["terminal"]
                        session.invalidate_cache_warming()
                        await asyncio.gather(task, return_exceptions=True)
                        after, _ = _snapshot(database.admin_dsn, session.session_id)
                        ping["canonical_after_request"] = after
                        ping["canonical_at_idle_start"] = idle_start
                        # Writer renewal legitimately changes updated_at while
                        # waiting four minutes. Compare the request boundary,
                        # retaining every field and the whole idle snapshot.
                        isolation = _assert_isolation(database.admin_dsn, session.session_id,
                                                      ping["canonical_before_request"], after, None)
                        key = "messages" if wire == "openai_chat_completions" else "input"
                        assert ping["payload"][key][:len(first_record["payload"][key])] == first_record["payload"][key]
                        assert ping["payload"].get("tools") == first_record["payload"].get("tools")
                        if wire == "openai_responses":
                            assert ping["payload"].get("instructions") == first_record["payload"].get("instructions")
                        # A duplicate independent branch never accumulates prior pings.
                        duplicate = await execute_ephemeral_request(runtime=runtime, prepared=prepared)
                        assert duplicate.status == "COMPLETED"
                        second = await session.run_turn(PromptContent.text("再用一句话说明保持前缀稳定的作用。"))
                        task = session._ephemeral._warming_task
                        assert task is not None
                        # A real model selection update cancels without another message.
                        other = next(c for c in connections.values() if c.id != config.id)
                        await session.update_model_call_binding(ModelCallBinding(
                            other.id, ReasoningEffortSelection("high")))
                        await asyncio.gather(task, return_exceptions=True)
                        assert session._ephemeral._warming_task is None or task.done()
                        normal = [r for r in records if r["purpose"] == "agent_model_loop" and r["wire_api"] == wire]
                        assert len(normal) == 2
                        assert CACHE_WARMING_PROMPT not in json.dumps(normal[-1]["payload"], ensure_ascii=False)
                        assert normal[-1]["payload"][key][:len(first_record["payload"][key])] == first_record["payload"][key]
                        usage = await core.read_provider_call_usage_page(session_id=session.session_id)
                        assert len(usage.usages) == 2
                        results.append({"wire_api": wire, "source_context_id": source_id,
                                        "elapsed_seconds": elapsed, "first_answer": first.final_text,
                                        "next_answer": second.final_text, "isolation": isolation,
                                        "model_switch_cancelled": True, "formal_usage_rows": 2})
                        print(json.dumps({"wire_api": wire, "status": "passed", "elapsed_seconds": elapsed}), flush=True)
                    finally:
                        await core.close_session(session.host_session_id)

                async with asyncio.timeout(600):  # Harness watchdog, not a product bound.
                    await asyncio.gather(*(exercise(w, c) for w, c in connections.items()))
            finally:
                await core.shutdown()
                await catalog.aclose()
        evidence["status"] = "passed"
    except BaseException as error:
        evidence["status"] = "failed"
        evidence["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        ModelRuntime.borrow_transport = original_borrow
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if database is not None:
            drop_postgres_test_database(admin, database.database_name)
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        output.write_text(scrubber.scrub_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str)) + "\n")
    print(json.dumps({"status": evidence["status"], "evidence": str(output)}))


if __name__ == "__main__":
    asyncio.run(run())
