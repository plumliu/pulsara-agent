"""Real saved DeepSeek Chat/Responses: temporary calls and canonical isolation.

Uses the desktop's verified local socket and a new disposable database. Saved
settings remain read-only; the output is explicitly requested diagnostic data.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.ephemeral import SUGGESTION_PROMPT
from pulsara_agent.conversation_kernel.direct_model import prepare_suggestion_request
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.llm.adapters.openai.chat_completions import (
    build_chat_completions_payload,
)
from pulsara_agent.llm.adapters.openai.responses import build_responses_payload
from pulsara_agent.llm.connection_probe import probe_model_connection
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ReasoningEffortSelection,
)
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.storage.migrations.runner import PostgresMigrationRunner
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.ephemeral_failure_probe import run_unknown_model_failure_probe
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingExecution,
)


class _RecordedBorrow:
    def __init__(self, delegate, records):
        self.delegate, self.records, self.call = delegate, records, delegate.call

    def open_stream(self, *, context):
        call = self.call
        build = (
            build_chat_completions_payload
            if call.target.fact.wire_api == "openai_chat_completions"
            else build_responses_payload
        )
        record = {
            "purpose": call.fact.purpose.value,
            "wire_api": call.target.fact.wire_api,
            "payload": build(call=call, context=context),
            "normalized_blocks": [],
            "terminal": None,
        }
        self.records.append(record)
        return _RecordingExecution(self.delegate.open_stream(context=context), record)

    def close(self):
        self.delegate.close()


def _snapshot(dsn, session_id):
    with psycopg.connect(dsn) as connection:
        tables = connection.execute(
            "SELECT table_name FROM information_schema.columns WHERE table_schema='pulsara_v3' AND column_name='session_id' ORDER BY table_name"
        ).fetchall()
        counts = {
            name: connection.execute(
                sql.SQL(
                    "SELECT count(*) FROM pulsara_v3.{} WHERE session_id=%s"
                ).format(sql.Identifier(name)),
                (session_id,),
            ).fetchone()[0]
            for (name,) in tables
        }
        session = connection.execute(
            "SELECT to_jsonb(s) FROM pulsara_v3.sessions s WHERE id=%s", (session_id,)
        ).fetchone()[0]
        title = session.pop("title")
        return {"counts": counts, "session_except_title": session}, title


def _assert_isolation(dsn, session_id, before, after, suggestion):
    # The normal asynchronous history index catches up after the final answer.
    # Check its actual provenance/content separately, instead of treating its
    # legitimate projection insertion as temporary-response persistence.
    with psycopg.connect(dsn) as connection:
        ids = {
            row[0]
            for row in connection.execute(
                "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s",
                (session_id,),
            )
        }
        documents = connection.execute(
            "SELECT entry_ids, body FROM pulsara_v3.history_search_documents WHERE session_id=%s",
            (session_id,),
        ).fetchall()
        for entries, body in documents:
            assert set(entries) <= ids
            assert SUGGESTION_PROMPT not in body
            assert not suggestion or suggestion not in body

    def semantic(snapshot):
        counts = {
            k: v
            for k, v in snapshot["counts"].items()
            if k != "history_search_documents"
        }
        session = dict(snapshot["session_except_title"])
        # Existing writer lease heartbeat belongs to Host liveness, not a model
        # request. Its owner/generation and all semantic timestamps stay checked.
        session.pop("writer_lease_expires_at")
        return counts, session

    assert semantic(before) == semantic(after), (
        "temporary result changed canonical state beyond title"
    )
    return {
        "history_documents_checked": len(documents),
        "only_canonical_entry_ids": True,
        "no_temporary_content": True,
    }


async def _settled(session):
    # Harness watchdog only: production requests use the existing transport policy.
    async with asyncio.timeout(240):
        while session._ephemeral._tasks:
            await asyncio.gather(*tuple(session._ephemeral._tasks))
            await asyncio.sleep(0)
    return session.read_ephemeral_results()


async def run():
    home = require_pulsara_home()
    saved = LocalSettingsStore().read()
    scrubber = secret_scrubber(saved)
    configured = {
        c.target.wire_api.value: c
        for c in saved.model_connections
        if c.target.model_id == "deepseek-flash" and c.target.route_id == "deepseek"
    }
    if set(configured) != {"openai_chat_completions", "openai_responses"}:
        raise RuntimeError(
            "This run requires both saved DeepSeek wire connections; none were changed."
        )
    # Same paths and role names as desktop/src/postgres.ts BundledPostgres.settings().
    socket = home / "desktop" / "socket"
    root_admin = make_conninfo(
        host=str(socket), port=5432, dbname="pulsara", user="pulsara_admin"
    )
    with psycopg.connect(root_admin, autocommit=True) as connection:
        database, address, data_dir = connection.execute(
            "SELECT current_database(), inet_server_addr(), current_setting('data_directory')"
        ).fetchone()
        if (
            database != "pulsara"
            or address is not None
            or Path(data_dir).resolve() != (home / "desktop" / "postgres").resolve()
        ):
            raise RuntimeError("desktop PostgreSQL target did not verify")
        name = "pulsara_ephemeral_" + uuid4().hex[:12]
        connection.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    admin = make_conninfo(
        host=str(socket), port=5432, dbname=name, user="pulsara_admin"
    )
    runtime_dsn = make_conninfo(
        host=str(socket), port=5432, dbname=name, user="pulsara_runtime"
    )
    original_home = os.environ.get("PULSARA_HOME")
    original_borrow = ModelRuntime.borrow_transport
    records, results = [], []
    output = Path("output/ephemeral-products-20261023/dogfood.json").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "database": admin,
        "records": records,
        "results": results,
    }
    try:
        PostgresMigrationRunner(admin_dsn=admin, runtime_dsn=runtime_dsn).migrate(
            deadline_monotonic=monotonic() + 240
        )
        catalog = ModelCatalogOwner(ModelsDevCatalogClient())
        await catalog.refresh()
        ModelRuntime.borrow_transport = lambda self, permit: _RecordedBorrow(
            original_borrow(self, permit), records
        )
        with TemporaryDirectory(prefix="pulsara-ephemeral-") as tmp:
            root = Path(tmp).resolve()
            isolated_home, workspace = root / "home", root / "workspace"
            isolated_home.mkdir()
            workspace.mkdir()
            os.environ["PULSARA_HOME"] = str(isolated_home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
            )
            runtime = ModelRuntime.production(settings=settings, catalog=catalog)
            core = KernelHostCore.production(model_runtime=runtime)
            try:
                for wire, connection in configured.items():
                    binding = ModelCallBinding(
                        connection.id, ReasoningEffortSelection("high")
                    )
                    resolved = runtime.freeze_resolution_snapshot().validate(binding)
                    await probe_model_connection(
                        resolved=resolved,
                        catalog=catalog.selectable(),
                        route_wires=runtime.route_wires,
                        api_key=saved.model_api_key(connection.id),
                    )
                    session = await core.open_session(
                        HostWorkspaceInput(
                            workspace_kind="project",
                            workspace_root=workspace,
                            trust_workspace_mcp_config=False,
                        ),
                        system_prompt="请直接回答用户问题，保持简洁。本轮不需要调用工具。",
                    )
                    await session.attach_controller("ephemeral-dogfood")
                    await session.update_model_call_binding(binding)
                    before = len(records)
                    first = await session.run_turn(
                        PromptContent.text(
                            "我在设计长时间运行的编程助手。为什么保持模型输入前缀一致有助于复用缓存？请用两句话解释。"
                        )
                    )
                    boundary, _ = _snapshot(admin, session.session_id)
                    state = await _settled(session)
                    after, title = _snapshot(admin, session.session_id)
                    evidence["last_boundary_comparison"] = {
                        "before": boundary,
                        "after": after,
                        "state": state,
                        "title": title,
                    }
                    projection_check = _assert_isolation(
                        admin,
                        session.session_id,
                        boundary,
                        after,
                        state["suggestion"]["text"],
                    )
                    assert state["suggestion"]["state"] == "ready", state
                    assert state["title"]["state"] == "adopted" and title, state
                    first_calls = records[before:]
                    main = next(
                        r for r in first_calls if r["purpose"] == "agent_model_loop"
                    )
                    suggestion = next(
                        r
                        for r in first_calls
                        if r["purpose"] == "conversation_suggestion"
                    )
                    title_call = next(
                        r for r in first_calls if r["purpose"] == "session_title"
                    )
                    key = "messages" if wire == "openai_chat_completions" else "input"
                    assert (
                        suggestion["payload"][key][: len(main["payload"][key])]
                        == main["payload"][key]
                    )
                    assert suggestion["payload"].get("tools") == main["payload"].get(
                        "tools"
                    )
                    assert suggestion["payload"]["tool_choice"] == "auto"
                    assert not title_call["payload"].get("tools")
                    # Use this real accepted native response and prefix, with a
                    # narrower test-only input allowance. No huge online prompt.
                    source = first.final_source
                    prepared = prepare_suggestion_request(
                        runtime=runtime,
                        source=source,
                        prompt=SUGGESTION_PROMPT,
                        timeout_policy=session._model.transport_timeout_policy,
                    )
                    temporary_tokens = (
                        prepared.context.ephemeral_wire_input.budget_input_tokens
                    )
                    narrow = replace(
                        source,
                        call_target=replace(
                            source.call_target,
                            input_budget=replace(
                                source.call_target.input_budget,
                                effective_input_budget_tokens=temporary_tokens - 1,
                            ),
                        ),
                    )
                    scope = ProviderInputContinuityScope(
                        session.session_id, ModelInputScopeKind.ROOT
                    )
                    cohort = session._input_continuity.current_cohort(scope)
                    anchor = session._input_continuity.current_usage_anchor(scope)
                    capacity_before, _ = _snapshot(admin, session.session_id)
                    opened = len(records)
                    try:
                        prepare_suggestion_request(
                            runtime=runtime,
                            source=narrow,
                            prompt=SUGGESTION_PROMPT,
                            timeout_policy=session._model.transport_timeout_policy,
                        )
                    except ValueError as error:
                        assert "exceeds model budget" in str(error), str(error)
                        capacity_error = str(error)
                    else:
                        raise AssertionError(
                            "over-budget temporary branch was admitted"
                        )
                    assert len(records) == opened
                    assert session._input_continuity.current_cohort(scope) is cohort
                    assert (
                        session._input_continuity.current_usage_anchor(scope) is anchor
                    )
                    assert not session._compaction.is_fenced(
                        scope_kind=ModelInputScopeKind.ROOT, scope_subagent_task_id=None
                    )
                    capacity_after, _ = _snapshot(admin, session.session_id)
                    _assert_isolation(
                        admin,
                        session.session_id,
                        capacity_before,
                        capacity_after,
                        state["suggestion"]["text"],
                    )
                    second = await session.run_turn(
                        PromptContent.text(
                            "那如果只修改最后一条用户消息，会影响前面已经缓存的前缀吗？请简短回答。"
                        )
                    )
                    second_state = await _settled(session)
                    normal = [
                        r
                        for r in records[before:]
                        if r["purpose"] == "agent_model_loop"
                    ]
                    assert len(normal) == 2
                    assert (
                        normal[1]["payload"][key][: len(main["payload"][key])]
                        == main["payload"][key]
                    )
                    assert SUGGESTION_PROMPT not in json.dumps(
                        normal[1]["payload"], ensure_ascii=False
                    )
                    page = await core.read_provider_call_usage_page(
                        session_id=session.session_id
                    )
                    assert len(page.usages) == 2, page.to_dict()
                    count = len(records)
                    for _ in range(3):
                        session.read_ephemeral_results()
                    assert len(records) == count
                    failure_before, _ = _snapshot(admin, session.session_id)
                    recording_borrow = ModelRuntime.borrow_transport
                    ModelRuntime.borrow_transport = original_borrow
                    try:
                        provider_failure = await run_unknown_model_failure_probe(
                            wire_api=wire,
                            probe=lambda: probe_model_connection(
                                resolved=resolved,
                                catalog=catalog.selectable(),
                                route_wires=runtime.route_wires,
                                api_key=saved.model_api_key(connection.id),
                            ),
                        )
                    finally:
                        ModelRuntime.borrow_transport = recording_borrow
                    failure_after, _ = _snapshot(admin, session.session_id)
                    _assert_isolation(
                        admin,
                        session.session_id,
                        failure_before,
                        failure_after,
                        second_state["suggestion"]["text"],
                    )
                    results.append(
                        {
                            "wire_api": wire,
                            "session_id": session.session_id,
                            "title": title,
                            "first_answer": first.final_text,
                            "suggestion": state,
                            "second_answer": second.final_text,
                            "second_suggestion": second_state,
                            "unchanged_canonical_after_temporary": True,
                            "database_counts": after["counts"],
                            "history_projection_check": projection_check,
                            "real_provider_failure": provider_failure,
                            "capacity_admission": {
                                "source": "real accepted final/native replay",
                                "test_only_input_budget": temporary_tokens - 1,
                                "error": capacity_error,
                                "provider_opens": 0,
                                "continuity_and_usage_unchanged": True,
                            },
                            "durable_provider_usage_count": len(page.usages),
                            "idle_reads_provider_calls": 0,
                        }
                    )
                    await core.close_session(session.host_session_id)
            finally:
                await core.shutdown()
        evidence["status"] = "passed"
    except BaseException as error:
        evidence["status"] = "failed"
        evidence["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        output.write_text(
            scrubber.scrub_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str)
            )
            + "\n"
        )
        ModelRuntime.borrow_transport = original_borrow
        if original_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = original_home
        with psycopg.connect(root_admin, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
    print(
        json.dumps(
            {
                "evidence": str(output),
                "status": evidence["status"],
                "wires": [r["wire_api"] for r in results],
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(run())
