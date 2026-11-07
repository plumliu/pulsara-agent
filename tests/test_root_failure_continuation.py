"""ROOT availability after an accepted tool attempt fails canonically."""

import asyncio
import json
import threading
from pathlib import Path

import pytest
import psycopg

from pulsara_agent.conversation_kernel.host import KernelHostCore
from pulsara_agent.conversation_kernel.direct_model import DirectKernelModelPort
from pulsara_agent.conversation_kernel.repository import (
    ConversationKernelConflict,
    ConversationKernelRepository,
)
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import PromptContent
from pulsara_agent.model_input.continuity import ProviderInputContinuityScope
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.live_agent_event import (
    TextDeltaPayload,
    TextEndPayload,
    TextStartPayload,
    live_digest,
)
from pulsara_agent.primitives.permission import PermissionMode
from pulsara_agent.workspace_identity import HostWorkspaceInput
from tests.support.model_config import test_model_binding, test_model_runtime
from tests.support.round3 import CallbackScriptedKernelModel
from tests.test_agent_code_mode_execution import _settled_formal_records

pytestmark = pytest.mark.postgres


async def _wait_root_idle(session):
    async def wait():
        while session._active_task is not None:
            await asyncio.sleep(0.01)
        if session._delivery_task.done():
            session._delivery_task.result()

    await asyncio.wait_for(wait(), timeout=10)


def _assert_root_outcomes(dsn, session_id):
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT status FROM pulsara_v3.turns WHERE session_id=%s ORDER BY accepted_at",
            (session_id,),
        ).fetchall()
        # Source Cell acceptance, formal Cell error delivery, then queued recovery.
        assert rows == [("COMPLETED",), ("COMPLETED",), ("COMPLETED",)]


def _assert_no_result(dsn, session_id, attempt_id):
    assert attempt_id is not None
    with psycopg.connect(dsn) as connection:
        assert connection.execute(
            "SELECT i.source_kind, i.tool_name, i.call_sequence "
            "FROM pulsara_v3.tool_execution_attempts a "
            "JOIN pulsara_v3.tool_invocations i ON i.session_id=a.session_id AND i.id=a.invocation_id "
            "WHERE a.session_id=%s AND a.id=%s",
            (session_id, attempt_id),
        ).fetchall() == [("CODE_MODE_CALL", "run_agent", 2)]
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.tool_results WHERE session_id=%s AND attempt_id=%s",
            (session_id, attempt_id),
        ).fetchone() == (0,)


def test_queued_root_continues_after_dependent_work_admission_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_migrated_postgres_database,
) -> None:
    import pulsara_agent.conversation_kernel.host as kernel_host

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PULSARA_HOME", str(home))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root_calls = 0
    session = None
    release_child = asyncio.Event()
    release_failure = threading.Event()
    recovery_requested = False
    recovered = asyncio.Event()
    conflict_injected = False
    failed_attempt = None
    first_work = None
    accepted_works = []
    child_calls = 0

    async def stream(request):
        nonlocal child_calls
        assert (
            request.compiled_input.canonical_input_identity.conversation_scope_kind
            is ModelInputScopeKind.SUBAGENT
        )
        child_calls += 1
        await release_child.wait()
        text = "CHILD_DONE"
        yield TextStartPayload("text:done")
        yield TextDeltaPayload("text:done", text)
        yield TextEndPayload("text:done", text, len(text), live_digest(text))

    original_admit = ConversationKernelRepository.admit_agent_requests

    def fail_dependent_admission(self, *args, requests, source_tool_attempt_id, **kwargs):
        # The real tool attempt already exists; reject only its downstream
        # dependent Work admission while retaining the first admitted Work.
        nonlocal conflict_injected, failed_attempt, first_work
        if any(request.depends_on for request in requests):
            assert release_failure.wait(timeout=10)
            conflict_injected = True
            failed_attempt = source_tool_attempt_id
            raise ConversationKernelConflict("injected dependent Work admission conflict")
        rows = original_admit(self, *args, requests=requests,
                              source_tool_attempt_id=source_tool_attempt_id, **kwargs)
        accepted_works.extend(row["work_id"] for row in rows)
        first_work = rows[0]["work_id"]
        return rows

    runtime = test_model_runtime(
        model_id="test-pro",
        wire_api="openai_chat_completions",
        postgres_dsn=isolated_migrated_postgres_database.runtime_dsn,
    )

    class CodeModeWorkModel(DirectKernelModelPort):
        """Exercise real ROOT wire planning, replay persistence and epoch install."""
        def __init__(self):
            super().__init__(model_runtime=runtime)
            self.child = CallbackScriptedKernelModel(stream)
            self.requests = []

        def preflight_execution(self, request, **kwargs):
            nonlocal root_calls
            if (
                request.compiled_input.canonical_input_identity.conversation_scope_kind
                is ModelInputScopeKind.SUBAGENT
            ):
                return self.child.preflight_execution(request, **kwargs)
            self.requests.append(request)
            root_calls += 1
            if root_calls == 1:
                arguments = {"code": (
                    'const first = await tools.run_agent([{instruction:"wait for release",context:{mode:"none"}}]);'
                    'await tools.run_agent([{instruction:"use predecessor",depends_on:[first.works[0]],context:{mode:"none"}}]);'
                )}
                delta = {
                    "reasoning_content": "delegate safely",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call:batch:{root_calls}",
                            "type": "function",
                            "function": {
                                "name": "exec_cell",
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            elif recovery_requested:
                recovered.set()
                delta = {"content": "RECOVERED"}
            else:
                delta = {"content": "CELL_ACCEPTED" if root_calls == 2 else "CELL_FAILURE_OBSERVED"}
            request.prepared_call.call.target.transport._adapter._mock_chunks = [
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "tool_calls"
                            if root_calls == 1
                            else "stop",
                        }
                    ]
                },
            ]
            return super().preflight_execution(request, **kwargs)

    model = CodeModeWorkModel()
    monkeypatch.setattr(kernel_host, "DirectKernelModelPort", lambda **_: model)
    monkeypatch.setattr(
        kernel_host.LocalMcpManagementService, "load_configs", lambda *_a, **_kw: ()
    )
    monkeypatch.setattr(
        ConversationKernelRepository, "admit_agent_requests", fail_dependent_admission
    )

    async def scenario():
        nonlocal session, recovery_requested
        core = KernelHostCore.production(model_runtime=runtime)
        session = await core.open_session(
            HostWorkspaceInput(workspace_kind="project", workspace_root=workspace)
        )
        try:
            await session.update_model_call_binding(test_model_binding(runtime))
            await session.submit_prompt(
                content=PromptContent.text("delegate then create successor"),
                command_id=random_id("command"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )

            # The accepted Cell outlives its source turn. Release the injected
            # admission failure only after that turn has canonically completed.
            async def wait_source_completed():
                while root_calls < 2 or session._active_task is not None:
                    if session._delivery_task.done():
                        session._delivery_task.result()
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_source_completed(), timeout=10)
            release_failure.set()
            records = await _settled_formal_records(session, expected=1)
            assert len(records) == 1
            assert records[0]["kind"] == "error"
            assert records[0]["error"]["code"] == "bridge_error"
            assert "injected dependent Work admission conflict" in records[0]["error"]["message"]

            async def wait_failure_delivered():
                while root_calls < 3 or session._active_task is not None:
                    if session._delivery_task.done():
                        session._delivery_task.result()
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_failure_delivered(), timeout=10)
            assert root_calls == 3
            assert conflict_injected
            scope = ProviderInputContinuityScope(
                session.session_id, ModelInputScopeKind.ROOT
            )
            prior_view = session._input_continuity.current_view(scope)
            assert prior_view is not None
            recovery_requested = True
            await session.submit_prompt(
                command_id=random_id("command"),
                content=PromptContent.text("continue normally"),
                requested_permission_mode=PermissionMode.BYPASS_PERMISSIONS,
            )

            async def wait_recovered():
                while not recovered.is_set():
                    if session._delivery_task.done():
                        session._delivery_task.result()
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_recovered(), timeout=10)
            release_child.set()
            await _wait_root_idle(session)
            # Completion, not a provider preflight or a model self-report, is
            # the availability oracle; unknown effects remain unknown.
            _assert_root_outcomes(
                isolated_migrated_postgres_database.runtime_dsn, session.session_id
            )
            _assert_no_result(
                isolated_migrated_postgres_database.runtime_dsn,
                session.session_id,
                failed_attempt,
            )
            assert accepted_works == [first_work]
            assert child_calls == 1
            assert root_calls == 4
            with psycopg.connect(isolated_migrated_postgres_database.runtime_dsn) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM pulsara_v3.prompt_queue_items q "
                    "JOIN pulsara_v3.sessions s ON s.id=q.session_id "
                    "WHERE s.root_session_id=%s AND q.input_origin='AGENT_INSTRUCTION'",
                    (session.session_id,),
                ).fetchone() == (1,)
            next_view = session._input_continuity.current_view(scope)
            assert next_view is not None
            assert next_view.epoch_nonce == prior_view.epoch_nonce
            before = model.requests[2].wire_input_plan.materialization
            after = model.requests[3].wire_input_plan.materialization
            assert after.root_policy_value == before.root_policy_value
            assert after.tool_items == before.tool_items
            assert (
                after.ordered_input_items[: len(before.ordered_input_items)]
                == before.ordered_input_items
            )
        finally:
            release_failure.set()
            release_child.set()
            await core.close_session(session.host_session_id)
            await core.shutdown()

    asyncio.run(scenario())
