"""Real-provider mixed supplement and explicit Stop at a physical tool boundary.

The model emits each native batch. A diagnostic gate pauses the second actual
write just before its physical invocation; the first write and its result have
already settled. A real child model sends its own message and completes while
ROOT is paused. Saved settings are read-only; the database is local and unique.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from unittest.mock import patch

import psycopg
from psycopg import sql

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.user_control import (
    UserControlOperation,
    UserControlRequest,
    UserControlTarget,
    UserControlTargetKind,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.adapters.openai import chat_completions, responses
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import (
    ModelCallBinding,
    ReasoningEffortSelection,
)
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.credentials import secret_scrubber
from tests.dogfood.run_compaction_request_start_dogfood import _rows
from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace
from tests.dogfood.run_known_agents_handoff_dogfood import (
    HeldExecution,
    eventually,
    make_database,
)
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore,
    _RecordingModelRuntime,
    _RecordingTransport,
)

OUTPUT = Path("output/unified-safe-point-20261030/provider")
HUMAN = "HUMAN_SUPPLEMENT_SAFETYPOINT"
MESSAGE = "AGENT_MESSAGE_SAFETYPOINT"
FINAL = "AGENT_COMPLETION_SAFETYPOINT"
SENDER = "SENDER_GATE_SAFETYPOINT"


def same_prefix(first, following, wire):
    if wire == "openai_responses":
        return (
            first["instructions"] == following["instructions"]
            and first["tools"] == following["tools"]
            and following["input"][: len(first["input"])] == first["input"]
        )
    return (
        first["tools"] == following["tools"]
        and following["messages"][: len(first["messages"])] == first["messages"]
    )


def verify_call(call, wire, connection_id):
    assert call["wire_api"] == wire and call["connection_id"] == connection_id
    assert call["model_id"] == "deepseek-flash" and call["reasoning"] == {
        "kind": "effort",
        "value": "high",
    }
    assert call.get("terminal") and call["terminal"]["kind"] == "COMPLETED"
    assert call.get("adapter_payloads"), "missing serialized provider request"
    payload = dict(call["adapter_payloads"][-1])
    payload.update(payload.pop("extra_body", {}))
    assert payload.get("model") == "deepseek-flash", payload.get("model")
    assert (
        payload.get("reasoning_effort") or payload.get("reasoning", {}).get("effort")
    ) == "high"


def queue_rows(root, turn_id):
    return _rows(
        root,
        """SELECT id,queue_sequence,delivery_mode,input_origin,source_session_id,
        source_work_id,target_turn_id,status,consumed_entry_id FROM pulsara_v3.prompt_queue_items
        WHERE session_id=%s AND target_turn_id=%s AND delivery_mode='STEER_ACTIVE_TURN'
        ORDER BY queue_sequence,id""",
        (root.session_id, turn_id),
    )


async def run(admin_dsn, runtime_dsn, label):
    home = require_pulsara_home()
    saved = LocalSettingsStore(home / "local-settings.yaml").read()
    scrubber = secret_scrubber(saved)
    connections = {
        item.target.wire_api.value: item
        for item in saved.model_connections
        if item.target.model_id == "deepseek-flash"
        and item.target.route_id == "deepseek"
    }
    for wire in ("openai_chat_completions", "openai_responses"):
        if wire not in connections or saved.model_api_key(connections[wire].id) is None:
            raise RuntimeError(
                f"missing saved DeepSeek/deepseek-flash connection or credential: {wire}"
            )
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / f"{label}.json"
    if destination.exists():
        raise RuntimeError(f"evidence file already exists: {destination}")
    report = {"saved_home": str(home), "cases": [], "calls": [], "status": "running"}

    def save():
        destination.write_text(
            scrubber.scrub_text(
                json.dumps(report, ensure_ascii=False, indent=2, default=str)
            )
            + "\n"
        )

    root_dsn, db_name, admin, runtime_dsn, endpoint = make_database(
        admin_dsn, runtime_dsn
    )
    report["database"] = {
        "name": db_name,
        "host": endpoint[0],
        "port": endpoint[1],
        "isolated": True,
    }
    previous_home = os.environ.get("PULSARA_HOME")
    current = {}
    original_stream = _RecordingTransport.open_stream
    original_invoke = DirectKernelToolPort.invoke

    def observed_builder(original):
        def build(*, call, context):
            payload = original(call=call, context=context)
            records = [
                item
                for item in report["calls"]
                if item["resolved_model_call_id"] == call.resolved_model_call_id
            ]
            if records:
                records[-1].setdefault("adapter_payloads", []).append(payload)
            return payload

        return build

    def observed_stream(owner, *, call, context):
        execution = original_stream(owner, call=call, context=context)
        record = report["calls"][-1]
        if (
            current.get("sender_gate") is not None
            and not current.get("sender_held")
            and SENDER in json.dumps(record["provider_input"], ensure_ascii=False)
            and not any(tool.name == "run_agent" for tool in context.tools)
        ):
            current["sender_held"] = True
            record["diagnostic_sender_read_gate"] = True
            current["sender_started"].set()
            return HeldExecution(execution, current["sender_gate"])
        return execution

    async def observed_invoke(owner, **kwargs):
        context = kwargs["invocation_context"]
        arguments = kwargs["arguments"]
        path = str(arguments.get("target", {}).get("path", ""))
        if (
            kwargs["tool_name"] == "write_file"
            and context.session_id == current.get("root_id")
            and path.endswith(current.get("second_file", "/NO_MATCH"))
            and not current.get("second_held")
        ):
            current["second_held"] = True
            current["gate_metadata"] = {
                "tool_call_id": kwargs["tool_call_id"],
                "attempt_id": kwargs["attempt_id"],
                "assistant_entry_id": kwargs["assistant_entry_id"],
                "turn_id": kwargs["turn_id"],
                "arguments": arguments,
            }
            current["tool_waiting"].set()
            await current["tool_gate"].wait()
        return await original_invoke(owner, **kwargs)

    try:
        with (
            TemporaryDirectory(prefix="pulsara-unified-safe-point-") as temporary,
            patch.object(_RecordingTransport, "open_stream", observed_stream),
            patch.object(DirectKernelToolPort, "invoke", observed_invoke),
            patch.object(
                chat_completions,
                "build_chat_completions_payload",
                observed_builder(chat_completions.build_chat_completions_payload),
            ),
            patch.object(
                responses,
                "build_responses_payload",
                observed_builder(responses.build_responses_payload),
            ),
        ):
            temp = Path(temporary)
            local_home = temp / "home"
            local_home.mkdir()
            os.environ["PULSARA_HOME"] = str(local_home)
            settings = _ReadOnlySettingsStore(
                replace(saved, postgres=LocalPostgresConfig(runtime_dsn, admin))
            )
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            native = ModelRuntime.production(settings=settings, catalog=catalog)
            core = KernelHostCore.production(
                model_runtime=_RecordingModelRuntime(native, report["calls"])
            )
            try:
                for wire in ("openai_chat_completions", "openai_responses"):
                    for scenario in ("supplements", "stop"):
                        case = {
                            "wire": wire,
                            "scenario": scenario,
                            "status": "running",
                            "call_start": len(report["calls"]),
                        }
                        report["cases"].append(case)
                        current.clear()
                        current.update(
                            sender_gate=asyncio.Event()
                            if scenario == "supplements"
                            else None,
                            sender_started=asyncio.Event(),
                            tool_waiting=asyncio.Event(),
                            tool_gate=asyncio.Event(),
                            second_file="second.txt",
                            sender_held=False,
                            second_held=False,
                        )
                        workspace = temp / f"{wire}-{scenario}"
                        workspace.mkdir()
                        root = None
                        running = None
                        try:
                            async with asyncio.timeout(600):
                                root = await core.open_session(
                                    HostWorkspaceInput(
                                        workspace_kind="project",
                                        workspace_root=workspace,
                                    )
                                )
                                current["root_id"] = root.session_id
                                await root.attach_controller(
                                    "unified-safe-point-dogfood"
                                )
                                binding = ModelCallBinding(
                                    connections[wire].id,
                                    ReasoningEffortSelection("high"),
                                )
                                native.freeze_resolution_snapshot().validate(binding)
                                await root.update_model_call_binding(binding)
                                case.update(
                                    root=root.session_id,
                                    connection_id=connections[wire].id.value,
                                    model="deepseek-flash",
                                    reasoning="high",
                                )
                                if scenario == "supplements":
                                    sender_instruction = (
                                        f"{SENDER}. Call native send_agent_message to ROOT with the exact message {MESSAGE}, "
                                        f"then give exactly {FINAL} as your final response. Do not call other tools."
                                    )
                                    create_arguments = {
                                        "name": "Sender",
                                        "instruction": sender_instruction,
                                    }
                                    result = await root.run_turn(
                                        PromptContent.text(
                                            "Call native run_agent exactly once using the following exact JSON object as its arguments. "
                                            "Copy the entire instruction string verbatim; do not abbreviate or execute it yourself. "
                                            + json.dumps(
                                                create_arguments, ensure_ascii=False
                                            )
                                            + " After acceptance reply briefly."
                                        ),
                                        command_id=random_id("command"),
                                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                                    )
                                    case["creation_reply"] = result.final_text
                                    children = await root._io.run(
                                        root.repository.list_root_agent_session_ids,
                                        root_session_id=root.session_id,
                                        deadline_monotonic=monotonic() + 30,
                                    )
                                    assert len(children) == 1, children
                                    case["sender"] = children[0]
                                    work = await root._io.run(
                                        root.repository.list_agent_works,
                                        root_session_id=root.session_id,
                                        agent_id=case["sender"],
                                        limit=10,
                                        deadline_monotonic=monotonic() + 30,
                                    )
                                    case["sender_accepted_work"] = work
                                    assert (
                                        len(work) == 1
                                        and work[0]["instruction"] == sender_instruction
                                    ), (
                                        "model did not submit the requested full sender instruction"
                                    )
                                    await asyncio.wait_for(
                                        current["sender_started"].wait(), 90
                                    )
                                prompt = (
                                    "Use native write_file twice in one single parallel tool-call batch, not sequential provider calls. "
                                    "Write exactly FIRST_REAL_EFFECT to first.txt and exactly SECOND_REAL_EFFECT to second.txt. "
                                    "Call write_file once per file, first.txt then second.txt. Afterwards briefly report results."
                                )
                                case["trigger_prompt"] = prompt
                                running = asyncio.create_task(
                                    root.run_turn(
                                        PromptContent.text(prompt),
                                        command_id=random_id("command"),
                                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                                    )
                                )
                                gate_wait = asyncio.create_task(
                                    current["tool_waiting"].wait()
                                )
                                try:
                                    done, _ = await asyncio.wait(
                                        (gate_wait, running),
                                        timeout=120,
                                        return_when=asyncio.FIRST_COMPLETED,
                                    )
                                    if gate_wait not in done:
                                        if running in done:
                                            result = await running
                                            case["premature_final"] = result.final_text
                                        raise AssertionError(
                                            "model did not produce the requested gated tool batch"
                                        )
                                finally:
                                    if not gate_wait.done():
                                        gate_wait.cancel()
                                turn_id = root.active_root_turn_id()
                                case["turn_id"] = turn_id
                                case["gate"] = current["gate_metadata"]
                                case["gate_attempt_before"] = _rows(
                                    root,
                                    """SELECT a.id,i.tool_name,r.result_state
                                    FROM pulsara_v3.tool_execution_attempts a
                                    JOIN pulsara_v3.tool_invocations i ON i.session_id=a.session_id AND i.id=a.invocation_id
                                    LEFT JOIN pulsara_v3.tool_results r ON r.session_id=a.session_id AND r.attempt_id=a.id
                                    WHERE a.session_id=%s AND a.id=%s""",
                                    (root.session_id, case["gate"]["attempt_id"]),
                                )
                                assert (
                                    len(case["gate_attempt_before"]) == 1
                                    and case["gate_attempt_before"][0]["result_state"]
                                    is None
                                )
                                case["batch"] = _rows(
                                    root,
                                    """SELECT block_ordinal,tool_call_id,tool_name,tool_arguments
                                    FROM pulsara_v3.assistant_message_blocks WHERE session_id=%s AND assistant_entry_id=%s
                                      AND block_kind='TOOL_CALL' ORDER BY block_ordinal""",
                                    (
                                        root.session_id,
                                        case["gate"]["assistant_entry_id"],
                                    ),
                                )
                                assert len(case["batch"]) >= 2, case["batch"]
                                batch_writes = [
                                    item
                                    for item in case["batch"]
                                    if item["tool_name"] == "write_file"
                                ]
                                assert len(batch_writes) == 2, case["batch"]
                                assert [
                                    item["tool_arguments"]
                                    .get("target", {})
                                    .get("path", "")
                                    .rsplit("/", 1)[-1]
                                    for item in batch_writes
                                ] == ["first.txt", "second.txt"], batch_writes
                                assert (
                                    batch_writes[1]["tool_call_id"]
                                    == case["gate"]["tool_call_id"]
                                ), batch_writes
                                calls = report["calls"][case["call_start"] :]
                                candidates = [
                                    call
                                    for call in calls
                                    if call["wire_api"] == wire
                                    and call["purpose"] == "agent_model_loop"
                                    and any(
                                        block.get("tool_call_id")
                                        == case["gate"]["tool_call_id"]
                                        for block in call["normalized_blocks"]
                                    )
                                ]
                                assert len(candidates) == 1, (
                                    "missing actual model batch"
                                )
                                first = candidates[0]
                                verify_call(first, wire, connections[wire].id.value)
                                case["provider_batch_sequence"] = first["sequence"]
                                assert (
                                    workspace / "first.txt"
                                ).read_text() == "FIRST_REAL_EFFECT"
                                assert not (workspace / "second.txt").exists()
                                first_trace = await asyncio.to_thread(_tool_trace, root)
                                case["before_gate_trace"] = first_trace
                                assert any(
                                    t["tool_name"] == "write_file"
                                    and t["arguments"]
                                    .get("target", {})
                                    .get("path", "")
                                    .endswith("first.txt")
                                    and t["result_state"] == "SUCCESS"
                                    for t in first_trace
                                )
                                save()
                                if scenario == "supplements":
                                    queued = await root.submit_prompt(
                                        command_id=random_id("command"),
                                        content=PromptContent.text(HUMAN),
                                        requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
                                    )
                                    redirected = await root.steer_queued_prompt(
                                        command_id=random_id("command"),
                                        source_queue_item_id=queued.prompt_delivery.queue_item_id,
                                        target_turn_id=turn_id,
                                    )
                                    case["human_queued"] = asdict(queued)
                                    case["human_redirected"] = asdict(redirected)
                                    assert redirected.status in ("PENDING", "SUCCEEDED")
                                    current["sender_gate"].set()

                                    async def all_pending():
                                        rows = queue_rows(root, turn_id)
                                        return (
                                            rows
                                            if [r["input_origin"] for r in rows]
                                            == [
                                                "HUMAN_STEER",
                                                "AGENT_MESSAGE",
                                            ]
                                            else None
                                        )

                                    case["pending_before_release"] = await eventually(
                                        all_pending, timeout=120
                                    )
                                    assert all(
                                        row["status"] == "PENDING"
                                        for row in case["pending_before_release"]
                                    )
                                    assert all(
                                        row["source_session_id"] == case["sender"]
                                        for row in case["pending_before_release"][1:]
                                    )
                                    case["sender_trace"] = await asyncio.to_thread(
                                        _tool_trace,
                                        core._live_agent_session(case["sender"]),
                                    )
                                    sent = [
                                        t
                                        for t in case["sender_trace"]
                                        if t["tool_name"] == "send_agent_message"
                                        and t["arguments"].get("message") == MESSAGE
                                        and t["result"].get("delivery") == "queued"
                                    ]
                                    assert len(sent) == 1, case["sender_trace"]
                                    calls = report["calls"][case["call_start"] :]
                                    sender_calls = [
                                        call
                                        for call in calls
                                        if any(
                                            block.get("tool_call_id")
                                            == sent[0]["tool_call_id"]
                                            for block in call["normalized_blocks"]
                                        )
                                    ]
                                    finals = [
                                        call
                                        for call in calls
                                        if any(
                                            block.get("kind") == "text"
                                            and FINAL in block.get("text", "")
                                            for block in call["normalized_blocks"]
                                        )
                                    ]
                                    assert len(sender_calls) == 1 and finals, (
                                        "missing actual sender call or final"
                                    )
                                    verify_call(
                                        sender_calls[0],
                                        wire,
                                        connections[wire].id.value,
                                    )
                                    verify_call(
                                        finals[-1], wire, connections[wire].id.value
                                    )
                                    case["sender_provider_calls"] = {
                                        "message": sender_calls[0]["sequence"],
                                        "final": finals[-1]["sequence"],
                                    }
                                    save()
                                    current["tool_gate"].set()
                                    case["root_final"] = (
                                        await asyncio.wait_for(running, 180)
                                    ).final_text
                                    case["queue_after"] = queue_rows(root, turn_id)
                                    assert not any(row["input_origin"] == "AGENT_MATERIAL" for row in case["queue_after"]), "Work final must not automatically enter ROOT input"
                                    assert [
                                        r["input_origin"] for r in case["queue_after"]
                                    ] == [
                                        "HUMAN_STEER",
                                        "AGENT_MESSAGE",
                                    ]
                                    assert all(
                                        r["status"] == "CONSUMED"
                                        and r["consumed_entry_id"]
                                        for r in case["queue_after"]
                                    )
                                    case["root_trace"] = await asyncio.to_thread(
                                        _tool_trace, root
                                    )
                                    case["gate_attempt_after"] = _rows(
                                        root,
                                        """SELECT a.id,r.result_state FROM pulsara_v3.tool_execution_attempts a
                                        LEFT JOIN pulsara_v3.tool_results r ON r.session_id=a.session_id AND r.attempt_id=a.id
                                        WHERE a.session_id=%s AND a.id=%s""",
                                        (root.session_id, case["gate"]["attempt_id"]),
                                    )
                                    batch = [
                                        x
                                        for x in case["root_trace"]
                                        if x["tool_call_id"]
                                        in {b["tool_call_id"] for b in case["batch"]}
                                    ]
                                    assert len(batch) == len(case["batch"]) and all(
                                        x["result_state"] == "SUCCESS" for x in batch
                                    ), batch
                                    assert (
                                        workspace / "second.txt"
                                    ).read_text() == "SECOND_REAL_EFFECT"
                                    calls = report["calls"][case["call_start"] :]
                                    later = [
                                        x
                                        for x in calls
                                        if x["sequence"] > first["sequence"]
                                        and all(
                                            marker
                                            in json.dumps(
                                                x["provider_input"], ensure_ascii=False
                                            )
                                            for marker in (HUMAN, MESSAGE)
                                        )
                                    ]
                                    assert later, (
                                        "no actual subsequent provider input containing all supplements"
                                    )
                                    following = later[0]
                                    key = (
                                        "input"
                                        if wire == "openai_responses"
                                        else "messages"
                                    )
                                    suffix = following["provider_input"][key][
                                        len(first["provider_input"][key]) :
                                    ]
                                    suffix_text = json.dumps(suffix, ensure_ascii=False)
                                    positions = [
                                        suffix_text.find(marker)
                                        for marker in (HUMAN, MESSAGE)
                                    ]
                                    case["provider_calls"] = {
                                        "batch": first["sequence"],
                                        "following": following["sequence"],
                                        "suffix_markers_positions": positions,
                                        "prefix_equal": same_prefix(
                                            first["provider_input"],
                                            following["provider_input"],
                                            wire,
                                        ),
                                    }
                                    assert case["provider_calls"]["prefix_equal"], case[
                                        "provider_calls"
                                    ]
                                    assert all(
                                        p >= 0 for p in positions
                                    ) and positions == sorted(positions), positions
                                    verify_call(
                                        following, wire, connections[wire].id.value
                                    )
                                else:
                                    stop_command = random_id("command")
                                    admission_deadline = (
                                        root.control_admission_deadline_ms()
                                    )
                                    control_request = UserControlRequest(
                                        UserControlOperation.STOP_ACTIVE_TURN,
                                        stop_command,
                                        root.session_id,
                                        root.host_session_id,
                                        UserControlTarget(
                                            UserControlTargetKind.ROOT_TURN, turn_id
                                        ),
                                        admission_deadline,
                                    )
                                    outcome = await root.request_stop_turn(
                                        command_id=stop_command,
                                        admission_deadline_ms=admission_deadline,
                                        expected_session_id=root.session_id,
                                        expected_host_session_id=root.host_session_id,
                                        target_turn_id=turn_id,
                                    )
                                    case["stop_accepted"] = asdict(outcome)

                                    async def stop_done():
                                        result = (
                                            await root.query_control_command(
                                                control_request
                                            )
                                        ).outcome
                                        return (
                                            result
                                            if result is not None
                                            and result.status != "PENDING"
                                            else None
                                        )

                                    case["stop_final"] = asdict(
                                        await eventually(stop_done)
                                    )
                                    await asyncio.gather(
                                        running, return_exceptions=True
                                    )
                                    case["turn_terminal"] = _rows(
                                        root,
                                        "SELECT status,terminal_reason FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
                                        (root.session_id, turn_id),
                                    )
                                    case["root_trace"] = await asyncio.to_thread(
                                        _tool_trace, root
                                    )
                                    case["gate_attempt_after"] = _rows(
                                        root,
                                        """SELECT a.id,r.result_state FROM pulsara_v3.tool_execution_attempts a
                                        LEFT JOIN pulsara_v3.tool_results r ON r.session_id=a.session_id AND r.attempt_id=a.id
                                        WHERE a.session_id=%s AND a.id=%s""",
                                        (root.session_id, case["gate"]["attempt_id"]),
                                    )
                                    assert (
                                        case["stop_final"]["status"] == "SUCCEEDED"
                                    ), case["stop_final"]
                                    assert (
                                        case["turn_terminal"][0]["status"]
                                        == "INTERRUPTED"
                                    )
                                    assert (
                                        workspace / "first.txt"
                                    ).read_text() == "FIRST_REAL_EFFECT"
                                    assert not (workspace / "second.txt").exists()
                                case["status"] = "passed"
                        except Exception as error:
                            case["status"] = "failed"
                            case["error"] = f"{type(error).__name__}: {error}"
                            if root is not None:
                                try:
                                    case[
                                        "root_trace_on_failure"
                                    ] = await asyncio.to_thread(_tool_trace, root)
                                    if case.get("turn_id"):
                                        case["queue_on_failure"] = queue_rows(
                                            root, case["turn_id"]
                                        )
                                    if case.get("sender"):
                                        sender = core._live_agent_session(
                                            case["sender"]
                                        )
                                        if sender is not None:
                                            case[
                                                "sender_trace_on_failure"
                                            ] = await asyncio.to_thread(
                                                _tool_trace, sender
                                            )
                                except Exception as trace_error:
                                    case["trace_error"] = (
                                        f"{type(trace_error).__name__}: {trace_error}"
                                    )
                            print(
                                scrubber.scrub_text(
                                    f"{wire}/{scenario}: {case['error']}"
                                ),
                                flush=True,
                            )
                        finally:
                            if current.get("sender_gate") is not None:
                                current["sender_gate"].set()
                            current["tool_gate"].set()
                            save()
                            if root is not None:
                                await core.close_session(root.host_session_id)
            finally:
                await core.shutdown()
    except BaseException as error:
        report["fatal_error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        if previous_home is None:
            os.environ.pop("PULSARA_HOME", None)
        else:
            os.environ["PULSARA_HOME"] = previous_home
        with psycopg.connect(root_dsn, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db_name))
            )
        report["database"]["dropped"] = True
        report["status"] = (
            "passed"
            if len(report["cases"]) == 4
            and all(x["status"] == "passed" for x in report["cases"])
            else "failed"
        )
        save()
    return report["status"] == "passed"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-dsn", required=True)
    parser.add_argument("--runtime-dsn", required=True)
    parser.add_argument("--label", required=True)
    args = parser.parse_args()
    if not args.label.replace("-", "").isalnum():
        parser.error("label must be alphanumeric with optional hyphens")
    raise SystemExit(
        0 if asyncio.run(run(args.admin_dsn, args.runtime_dsn, args.label)) else 1
    )
