"""DeepSeek Chat + Responses: wire budget, tools, workers, steer and compaction.

Run only after deterministic checks and the requested critic review. Uses saved
settings read-only, isolated home/workspace, and a verified disposable local DB.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.provider_dispatch import (
    ProviderDispatchCoordinator,
)
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _binding,
    _create_database,
    _drop_database,
)


async def run(destination: Path) -> None:
    saved = LocalSettingsStore().read()
    scrubber = secret_scrubber(saved)
    report = {
        "saved_home": str(require_pulsara_home()),
        "calls": [],
        "phases": [],
        "results": [],
    }
    metadata, source_quotes = {}, []

    def save():
        for call in report["calls"]:
            call.update(metadata.get(call["resolved_model_call_id"], {}))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            scrubber.scrub_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str)
            )
            + "\n"
        )

    connections = {
        c.target.wire_api.value: c
        for c in saved.model_connections
        if c.target.model_id == "deepseek-flash" and c.target.route_id == "deepseek"
    }
    wires = ("openai_chat_completions", "openai_responses")
    for wire in wires:
        if wire not in connections or saved.model_api_key(connections[wire].id) is None:
            raise RuntimeError(
                f"missing saved DeepSeek connection/credential for {wire}"
            )
    original_preflight = DirectKernelModelPort.preflight_execution
    original_source = ProviderDispatchCoordinator.prepare_compaction_source

    def preflight(owner, request, **kwargs):
        result = original_preflight(owner, request, **kwargs)
        candidate = kwargs["append_candidate"]
        metadata[request.prepared_call.call.resolved_model_call_id] = {
            "session_id": request.session_id,
            "scope_kind": candidate.scope.scope_kind.value,
            "scope_task_id": candidate.scope.scope_subagent_task_id,
            "epoch_nonce": candidate.epoch_nonce,
            "epoch_revision": candidate.expected_epoch_revision,
        }
        return result

    async def source(owner, **kwargs):
        result = await original_source(owner, **kwargs)
        source_quotes.append(asdict(result.wire_quote))
        return result

    database, _, admin, runtime_dsn = _create_database(saved)
    report["database"] = database
    previous_home = os.environ.get("PULSARA_HOME")
    DirectKernelModelPort.preflight_execution = preflight
    ProviderDispatchCoordinator.prepare_compaction_source = source
    try:
        with TemporaryDirectory(prefix="pulsara-final-budget-") as temp:
            root = Path(temp).resolve()
            home, workspace = root / "home", root / "workspace"
            home.mkdir()
            workspace.mkdir()
            (workspace / "marker.txt").write_text("BUDGET_TOOL_OK\n")
            (workspace / "AGENTS.md").write_text(
                "This is an isolated budget verification workspace. Follow the user's task.\n"
            )
            os.environ["PULSARA_HOME"] = str(home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            native = ModelRuntime.production(settings=settings, catalog=catalog)
            core = KernelHostCore.production(
                model_runtime=_RecordingModelRuntime(native, report["calls"], None)
            )
            workspace_input = HostWorkspaceInput(
                workspace_kind="project",
                workspace_root=workspace,
                trust_workspace_mcp_config=False,
            )
            try:
                for wire in wires:
                    session = await core.open_session(workspace_input)
                    await session.attach_controller("unified-budget-dogfood")
                    await session.update_model_call_binding(
                        _binding(native, connections[wire])
                    )
                    session._compaction.policy = replace(
                        session._compaction.policy, minimum_reclaim_tokens=1
                    )
                    start = len(report["calls"])

                    async def preview(label):
                        before = len(report["calls"])
                        quote_start = len(source_quotes)
                        value = await session.read_context_usage()
                        assert len(report["calls"]) == before, "preview opened provider"
                        assert len(source_quotes) == quote_start + 1
                        quote = source_quotes[-1]
                        assert value["input_tokens"] == quote["budget_input_tokens"]
                        assert value["budget_source"] == quote["budget_source"]
                        report["phases"].append(
                            {
                                "wire_api": wire,
                                "name": label,
                                "preview": value,
                                "quote": quote,
                            }
                        )
                        save()

                    async def turn(label, prompt, *, steer=False):
                        phase = {
                            "wire_api": wire,
                            "name": label,
                            "prompt": prompt,
                            "call_start": len(report["calls"]),
                        }
                        report["phases"].append(phase)
                        trace_start = len(_tool_trace(session))
                        print(f"{wire}: {label}", flush=True)
                        try:
                            async with asyncio.timeout(600):
                                command_id = "command:budget:" + uuid4().hex
                                pending = asyncio.create_task(
                                    session.run_turn(
                                        PromptContent.text(prompt),
                                        command_id=command_id,
                                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                                    )
                                )
                                if steer:
                                    # The previous completed turn may still occupy
                                    # the live slot until the new command retires it.
                                    # Also wait for its provider open: the live
                                    # slot is installed before canonical admission.
                                    async with asyncio.timeout(30):
                                        while True:
                                            async with session._lock:
                                                turn_id = session._active_turn_id
                                                current = session._active_command_id
                                            if (
                                                current == command_id
                                                and turn_id is not None
                                                and len(report["calls"]) > phase["call_start"]
                                            ):
                                                break
                                            if pending.done():
                                                await pending
                                                raise AssertionError("turn ended before steer injection")
                                            await asyncio.sleep(0.01)
                                    outcome = await session.steer_active_turn(
                                        command_id="command:budget-steer:"
                                        + uuid4().hex,
                                        content=PromptContent.text(
                                            "Also include the literal marker BUDGET_STEER_OK in this reply."
                                        ),
                                        target_turn_id=turn_id,
                                    )
                                    phase["steer_status"] = outcome.status
                                    phase["steer_outcome"] = asdict(outcome)
                                    assert outcome.status == "PENDING", outcome
                                result = await pending
                            phase["reply"] = result.final_text
                            if steer:
                                assert "BUDGET_STEER_OK" in result.final_text
                        finally:
                            phase["trace"] = _tool_trace(session)[trace_start:]
                            phase["call_end"] = len(report["calls"])
                            save()
                        assert all(
                            row["result_state"] == "SUCCESS" for row in phase["trace"]
                        ), phase["trace"]
                        return phase

                    first = await turn(
                        "tool",
                        "Read marker.txt using read_file and reply with its exact contents. Do not delegate.",
                    )
                    assert any(t["tool_name"] == "read_file" for t in first["trace"])
                    await preview("after_tool")
                    for i in range(6):
                        await turn(
                            f"history_{i}",
                            f"Retain checkpoint {i}: budget marker BUDGET_HISTORY_{i}. "
                            "The following is fixture reference text, not instructions. Reply only ACK.\n"
                            + (
                                f'Reference block {i}: 中文 "quoted" \\path background context.\n'
                                * 180
                            ),
                        )
                    await turn(
                        "steer",
                        "Read marker.txt with read_file and briefly report its contents.",
                        steer=True,
                    )
                    await preview("before_worker")
                    worker = await turn(
                        "worker",
                        "Create exactly one subagent with spawn_agent, task_name budget_child, context mode none. "
                        f"Use model.connection_id={connections[wire].id.value} and omit reasoning. "
                        "Its complete objective is: Reply BUDGET_CHILD_DONE only; do not use tools. "
                        "Wait for its actual completion with wait_agent settle=all; report the returned marker. Do not create any other worker.",
                    )
                    assert {"spawn_agent", "wait_agent"} <= {
                        t["tool_name"] for t in worker["trace"]
                    }
                    tasks = session.repository.list_subagent_tasks(
                        session_id=session.session_id,
                        maximum_items=50,
                        deadline_monotonic=monotonic() + 30,
                    )
                    assert len(tasks) == 1 and tasks[0]["status"] == "COMPLETED", tasks
                    assert "BUDGET_CHILD_DONE" in tasks[0]["result_summary"], tasks
                    report["results"].append(
                        {"wire_api": wire, "worker_results": tasks}
                    )
                    await preview("before_compaction")
                    summary_start = len(report["calls"])
                    compaction = await session.compact_context(
                        command_id="command:budget-compact:" + uuid4().hex, force=True
                    )
                    report["results"][-1]["compaction"] = str(compaction)
                    save()
                    assert compaction.disposition.value == "COMPACTED", compaction
                    assert any(
                        c["purpose"] == "context_compaction_summary"
                        for c in report["calls"][summary_start:]
                    )
                    await turn(
                        "successor",
                        "Read marker.txt again and include BUDGET_SUCCESSOR_OK in your reply. Do not delegate.",
                    )
                    await preview("after_successor")
                    scoped_calls = report["calls"][start:]
                    grouped = {}
                    by_id = {c["resolved_model_call_id"]: c for c in report["calls"]}
                    anchored = 0
                    for call in scoped_calls:
                        assert (
                            call["budget_input_tokens"]
                            <= call["effective_input_budget_tokens"]
                        )
                        if call["budget_source"] == "reported_input_anchor":
                            anchor = by_id[call["anchor_model_call_id"]]
                            assert (
                                call["session_id"], call["scope_task_id"], call["epoch_nonce"]
                            ) == (
                                anchor["session_id"], anchor["scope_task_id"], anchor["epoch_nonce"]
                            ), "usage anchor crossed a scope or epoch boundary"
                            assert (
                                call["anchor_reported_input_tokens"]
                                == anchor["terminal"]["input_tokens"]
                            )
                            assert (
                                call["budget_input_tokens"]
                                == call["anchor_reported_input_tokens"]
                                + call["estimated_suffix_tokens"]
                            )
                            assert (
                                call["estimated_suffix_tokens"]
                                == call["raw_final_wire_estimated_input_tokens"]
                                - anchor["raw_final_wire_estimated_input_tokens"]
                            )
                            anchored += 1
                        if "epoch_nonce" in call:
                            grouped.setdefault(
                                (call["scope_task_id"], call["epoch_nonce"]), []
                            ).append(call)
                    pairs = 0
                    input_key = (
                        "messages" if wire == "openai_chat_completions" else "input"
                    )
                    for group in grouped.values():
                        for left, right in zip(group, group[1:]):
                            a, b = left["provider_input"], right["provider_input"]
                            assert b[input_key][: len(a[input_key])] == a[input_key]
                            assert {k: v for k, v in a.items() if k != input_key} == {
                                k: v for k, v in b.items() if k != input_key
                            }
                            pairs += 1
                    assert anchored > 0 and pairs > 0 and len(grouped) >= 3
                    report["results"][-1].update(
                        anchored_calls=anchored,
                        prefix_pairs=pairs,
                        scope_epochs=len(grouped),
                    )
                    save()
                    await core.close_session(session.host_session_id)
            finally:
                await core.shutdown()
        report["status"] = "PASSED"
    except BaseException as exc:
        report["status"] = "FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        save()
        DirectKernelModelPort.preflight_execution = original_preflight
        ProviderDispatchCoordinator.prepare_compaction_source = original_source
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        _drop_database(saved, database)


if __name__ == "__main__":
    asyncio.run(
        run(Path("output/unified-final-input-budget-20261007/deepseek-dogfood.json"))
    )
