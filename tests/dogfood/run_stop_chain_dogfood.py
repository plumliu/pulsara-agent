"""Real saved DeepSeek Stop acceptance, with exact physical-close evidence.

Run with ``.venv/bin/python -m tests.dogfood.run_stop_chain_dogfood``.
Only a separately created local test database and temporary home are written.
The timeout option is an observation budget for this probe, not a product cap.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import traceback
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from pulsara_agent.capability.pulsara_home import require_pulsara_home
from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.user_control import (
    UserControlOperation, UserControlRequest, UserControlTarget, UserControlTargetKind,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.adapters.openai.retrying import expand_provider_diagnostic_scrubber
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.llm.model_catalog import ModelCatalogOwner, ModelsDevCatalogClient
from pulsara_agent.llm.model_connections import ModelCallBinding, ReasoningEffortSelection
from pulsara_agent.llm.runtime import ModelRuntime
from pulsara_agent.ports.live_agent_event import ThinkingDeltaPayload, TextDeltaPayload
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.settings import LocalPostgresConfig, LocalSettingsStore
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.dogfood.run_compaction_request_start_dogfood import _rows
from tests.dogfood.run_content_revision_line_edit_dogfood import _tool_trace
from tests.dogfood.run_model_switch_handover_dogfood import (
    _ReadOnlySettingsStore, _RecordingModelRuntime, _RecordingTransport,
)
from tests.support.postgres_database import (
    create_migrated_postgres_test_database, drop_postgres_test_database,
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    if is_dataclass(value):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class Evidence(logging.Handler):
    def __init__(self, destination, saved, endpoint):
        super().__init__()
        self.destination = destination
        self.scrub = expand_provider_diagnostic_scrubber(saved.credential_scrub_set(), endpoint)
        self.data = {"started_at": _now(), "status": "preparing", "calls": [],
                     "sdk_requests": [], "cases": [], "logs": []}

    def emit(self, record):
        self.data["logs"].append({"logger": record.name, "level": record.levelname,
            "message": record.getMessage(), "exception":
            logging.Formatter().formatException(record.exc_info) if record.exc_info else None})

    def save(self):
        encoded = self.scrub.scrub_text(json.dumps(self.data, ensure_ascii=False, indent=2, default=_json))
        assert not self.scrub.contains(encoded)
        self.destination.write_text(encoded + "\n", encoding="utf-8")


def _database_preflight(home, config_path=None):
    """Verify the explicit test root or the current desktop-owned Unix socket."""
    config = None if config_path is None else json.loads(config_path.read_text())
    socket = str(home / "desktop/socket") if config is None else config["socket"]
    if config is not None:
        root = Path(config["cluster_root"]).resolve(strict=True)
        assert Path(socket).resolve(strict=True).parent == root
        assert Path(config["data_directory"]).resolve(strict=True).parent == root
        assert root != home.resolve() and home.resolve() not in root.parents
    explicit_admin, explicit_runtime = os.getenv("PULSARA_POSTGRES_ADMIN_DSN"), os.getenv("PULSARA_POSTGRES_DSN")
    if config is not None:
        explicit_admin, explicit_runtime = config["admin_dsn"], config["runtime_dsn"]
    if bool(explicit_admin) != bool(explicit_runtime):
        raise RuntimeError("both explicit local PostgreSQL test DSNs are required")
    admin = explicit_admin or make_conninfo(host=socket, port=5432, dbname="pulsara", user="pulsara_admin")
    runtime = explicit_runtime or make_conninfo(host=socket, port=5432, dbname="pulsara", user="pulsara_runtime")
    facts = []
    for label, dsn in (("admin", admin), ("runtime", runtime)):
        parts = conninfo_to_dict(dsn)
        if parts.get("host") not in {"localhost", "127.0.0.1", "::1", socket}:
            raise RuntimeError(f"{label} DSN is not a verified local test root")
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            actual = connection.execute("SELECT current_database(),current_user,inet_server_addr()::text").fetchone()
            assert actual[0] == parts["dbname"] and actual[1] == parts["user"], actual
            if parts["host"] == socket:
                assert actual[2] is None
                if label == "admin":
                    assert connection.execute("SELECT current_setting('unix_socket_directories')").fetchone()[0] == socket
                    if config is not None:
                        assert connection.execute("SELECT current_setting('data_directory')").fetchone()[0] == config["data_directory"]
            else:
                assert actual[2] in {"127.0.0.1", "::1"}, actual
        facts.append({"label": label, "dsn": dsn, "actual": actual})
    return admin, runtime, facts


class ObservedExecution:
    """Observe the production transport without replacing any returned item."""
    def __init__(self, delegate, record, case, evidence):
        self.delegate, self.record, self.case, self.evidence = delegate, record, case, evidence

    async def read_next(self):
        item = await self.delegate.read_next()
        self.record.setdefault("stream_items", []).append({"at": _now(), "kind": type(item).__name__, "value": item})
        selected = self.record["case_call_index"] == self.case["stop_on_call"]
        if selected and isinstance(item, ThinkingDeltaPayload):
            self.case["trigger_kind"] = type(item).__name__
            self.case["ready"].set()
        elif selected and self.case["name"] == "after_tool" and isinstance(item, TextDeltaPayload):
            self.case["trigger_kind"] = type(item).__name__
            self.case["ready"].set()
        return item

    async def request_cancel(self, *, reason):
        self.record.setdefault("cancel_requests", []).append({"at": _now(), "reason": reason})
        return await self.delegate.request_cancel(reason=reason)

    async def aclose(self):
        self.record["close_started_at"] = _now()
        try:
            result = await self.delegate.aclose()
        except BaseException as error:
            self.record["close_error"] = {"type": type(error).__name__, "message": str(error)}
            raise
        self.record["close_returned_at"] = _now()
        return result

    async def wait_physical_completion(self):
        result = await self.delegate.wait_physical_completion()
        self.record["physical_completion"] = result
        self.record["physical_completion_at"] = _now()
        return result


async def _case(core, runtime, connection, workspace, name, active, evidence, timeout):
    case = {"name": name, "ready": asyncio.Event(), "stop_on_call": 1 if name == "thinking" else 2,
            "provider_calls": 0}
    active["case"] = case
    session = await core.open_session(HostWorkspaceInput(workspace_kind="project", workspace_root=workspace,
        trust_workspace_mcp_config=False), system_prompt=(
        "This is a controlled stopping acceptance probe. Follow the exact user request. "
        "Use only the requested tool, never delegate or schedule work. Do not repeat a successful tool."))
    await session.update_model_call_binding(ModelCallBinding(connection.id, ReasoningEffortSelection("high")))
    marker = workspace / "stop-marker.txt"
    puzzle = ("Compare BFS and DFS on a graph containing cycles. Explain step by step their correctness, "
              "memory requirements, and appropriate use cases. Do not use tools for this explanation.")
    prompt = puzzle if name == "thinking" else (
        "First invoke exec_process exactly once, with command `printf 'STOP_TOOL_ONCE\\n' >> stop-marker.txt; "
        "printf 'STOP_TOOL_DONE\\n'` in the current workspace. Wait for its successful result. "
        "Do not repeat the command or invoke another tool. Then " + puzzle)
    public = {"name": name, "session_id": session.session_id, "host_session_id": session.host_session_id,
              "prompt": prompt, "status": "running"}
    evidence.data["cases"].append(public)
    try:
        submitted = await session.submit_prompt(command_id=random_id("command"), content=PromptContent.text(prompt),
            requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS)
        public["submission"] = submitted
        evidence.save()
        await asyncio.wait_for(case["ready"].wait(), timeout)
        turn = session.active_root_turn_id()
        assert turn is not None, "real provider delta lost its Host ROOT owner"
        public.update(turn_id=turn, trigger_kind=case["trigger_kind"], stopped_at=_now())
        if name == "after_tool":
            trace = await asyncio.to_thread(_tool_trace, session)
            public["tools_before_stop"] = trace
            assert len(trace) == 1 and trace[0]["result_state"] == "SUCCESS", trace
            assert marker.read_text() == "STOP_TOOL_ONCE\n"
        request = UserControlRequest(UserControlOperation.STOP_ACTIVE_TURN, random_id("command"),
            session.session_id, session.host_session_id, UserControlTarget(UserControlTargetKind.ROOT_TURN, turn),
            session.control_admission_deadline_ms())
        public["control_request"] = request
        initial = await session.request_stop_turn(command_id=request.command_id,
            admission_deadline_ms=request.admission_deadline_ms, expected_session_id=request.session_id,
            expected_host_session_id=request.host_session_id, target_turn_id=turn)
        public["initial_control_outcome"] = initial
        assert initial.status == "PENDING", initial
        async def settled():
            while True:
                query = await session.query_control_command(request)
                if query.outcome is not None and query.outcome.status != "PENDING":
                    return query.outcome
                await asyncio.sleep(0.05)
        final = await asyncio.wait_for(settled(), timeout)
        public["final_control_outcome"] = final
        assert final.status == "SUCCEEDED" and final.public_code == "ROOT_STOPPED", final
        original = await session.query_command(submitted.command_id)
        public["original_command_outcome"] = original
        assert original.prompt_delivery.execution.status.value == "stopped"
        assert original.prompt_delivery.execution.terminal_reason == "USER_STOPPED"
        turns = await asyncio.to_thread(_rows, session,
            "SELECT id,status,terminal_reason FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at", (session.session_id,))
        public["canonical_turns"] = turns
        assert turns == [{"id": turn, "status": "INTERRUPTED", "terminal_reason": "USER_STOPPED"}], turns
        assert session.active_root_turn_id() is None
        runtime.assert_no_provider_borrows(session_id=session.session_id, turn_id=turn)
        trace = await asyncio.to_thread(_tool_trace, session)
        public["tools_after_stop"] = trace
        assert len(trace) == (1 if name == "after_tool" else 0)
        if name == "after_tool":
            public["marker_contents"] = marker.read_text()
            assert public["marker_contents"] == "STOP_TOOL_ONCE\n"
        calls = [record for record in evidence.data["calls"] if record["case"] == name]
        assert len(calls) == case["stop_on_call"], "Stop must not create a followup request"
        assert all(record.get("close_returned_at") and record.get("physical_completion").status.value == "COMPLETED" for record in calls)
        sdk = [record for record in evidence.data["sdk_requests"] if record["case"] == name]
        assert len(sdk) == len(calls), "unexpected additional SDK request/retry"
        if name == "after_tool":
            before, after = (item["request"] for item in sdk)
            assert before["tools"] == after["tools"]
            assert after["messages"][:len(before["messages"])] == before["messages"]
            public["prefix_check"] = "exact SDK tools unchanged; complete earlier messages (including SYSTEM) retained as prefix"
        public["status"] = "passed"
        evidence.save()
    finally:
        await core.close_session(session.host_session_id)
        public["host_closed_at"] = _now()
        evidence.save()


async def run(args):
    home = require_pulsara_home()
    saved = LocalSettingsStore(home / "local-settings.yaml").read()
    connection = next((item for item in saved.model_connections if item.target.route_id == "deepseek"
        and item.target.model_id == "deepseek-flash" and item.target.wire_api.value == "openai_chat_completions"), None)
    if connection is None or saved.model_api_key(connection.id) is None:
        raise RuntimeError("saved deepseek/deepseek-flash Chat connection and credential are required")
    destination = args.output.resolve()
    if destination.exists():
        raise RuntimeError(f"evidence already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(destination, saved, connection.base_url)
    evidence.data.update(saved_home=str(home), connection_id=connection.id.value,
        route_id=connection.target.route_id, model_id=connection.target.model_id, wire_api=connection.target.wire_api.value)
    logger = logging.getLogger()
    prior_handlers = logger.handlers[:]
    logger.handlers = [evidence]
    prior_env = {key: os.environ.get(key) for key in ("PULSARA_HOME", "PULSARA_POSTGRES_ADMIN_DSN", "PULSARA_POSTGRES_DSN")}
    database = core = None
    admin = None
    try:
        admin, runtime_dsn, facts = await asyncio.to_thread(_database_preflight, home, args.postgres_config)
        evidence.data["database_preflight"] = facts
        if args.prepare_only:
            evidence.data["status"] = "prepared"
            return evidence
        os.environ.update(PULSARA_POSTGRES_ADMIN_DSN=admin, PULSARA_POSTGRES_DSN=runtime_dsn)
        database = await asyncio.to_thread(create_migrated_postgres_test_database)
        evidence.data["isolated_database"] = asdict(database)
        with TemporaryDirectory(prefix="pulsara-stop-chain-") as temporary, ExitStack() as stack:
            root = Path(temporary)
            temporary_home = root / "home"
            temporary_home.mkdir()
            os.environ["PULSARA_HOME"] = str(temporary_home)
            settings = _ReadOnlySettingsStore(replace(saved, postgres=LocalPostgresConfig(database.runtime_dsn, database.admin_dsn)))
            catalog = ModelCatalogOwner(ModelsDevCatalogClient())
            await catalog.refresh()
            native = ModelRuntime.production(settings=settings, catalog=catalog)
            runtime = _RecordingModelRuntime(native, evidence.data["calls"])
            active = {}
            original_open = _RecordingTransport.open_stream
            def observed_open(owner, *, call, context):
                execution = original_open(owner, call=call, context=context)
                case = active["case"]
                case["provider_calls"] += 1
                record = evidence.data["calls"][-1]
                record.update(case=case["name"], case_call_index=case["provider_calls"], opened_at=_now())
                return ObservedExecution(execution, record, case, evidence)
            from openai.resources.chat.completions import AsyncCompletions
            original_create = AsyncCompletions.create
            async def observed_create(owner, **kwargs):
                evidence.data["sdk_requests"].append({"case": active["case"]["name"], "at": _now(), "request": kwargs})
                return await original_create(owner, **kwargs)
            stack.enter_context(patch.object(_RecordingTransport, "open_stream", observed_open))
            stack.enter_context(patch.object(AsyncCompletions, "create", observed_create))
            core = KernelHostCore.production(model_runtime=runtime)
            try:
                for name in ("thinking", "after_tool"):
                    workspace = root / name
                    workspace.mkdir()
                    await _case(core, runtime, connection, workspace, name, active, evidence, args.timeout)
            finally:
                await core.shutdown()
                evidence.data["core_shutdown_at"] = _now()
        evidence.data["status"] = "passed"
    except BaseException:
        evidence.data.update(status="failed", failure=traceback.format_exc())
    finally:
        if database is not None:
            await asyncio.to_thread(drop_postgres_test_database, admin, database.database_name)
            evidence.data["isolated_database_dropped"] = True
        for key, value in prior_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        evidence.data["finished_at"] = _now()
        evidence.save()
        logger.handlers = prior_handlers
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/stop-chain-dogfood/report.json"))
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--postgres-config", type=Path, help="existing isolated local cluster facts: root, socket, data_directory and DSNs")
    parser.add_argument("--timeout", type=float, default=180, help="per-phase observation budget in seconds")
    args = parser.parse_args()
    evidence = asyncio.run(run(args))
    print(json.dumps({"status": evidence.data["status"], "report": str(evidence.destination)}, ensure_ascii=False))
    return 0 if evidence.data["status"] in {"passed", "prepared"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
