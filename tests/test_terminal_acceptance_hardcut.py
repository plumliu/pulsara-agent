"""A complete durable tool result alone consumes the real process observation."""

import asyncio
from datetime import datetime, timezone
from time import monotonic
from types import SimpleNamespace

import pytest

from pulsara_agent.conversation_kernel.tool_contracts import KernelToolResult
from pulsara_agent.conversation_kernel.tool_execution import ToolBatchExecutor
from pulsara_agent.conversation_kernel.tool_outputs import (
    ToolOutputProcessor,
    PostgresToolOutputReadPort,
)
from pulsara_agent.conversation_kernel.tool_runtime import _terminal_acceptance
from pulsara_agent.conversation_kernel.tool_runtime_terminal import (
    _terminal_execution_result,
)
from pulsara_agent.identity import random_id
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.ports.tool_execution import ToolCall
from pulsara_agent.ports.tool_output import ToolResultSaveError
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.terminal_process import TerminalManager, TerminalRequest
from tests.test_hardcut_tool_result_storage import prepare

pytestmark = pytest.mark.postgres


class InlineIO:
    async def run(self, operation, *args, **kwargs):
        return operation(*args, **kwargs)


class LiveBus:
    def offer_nowait(self, **event):
        pass

    def offer_settlement_nowait(self, **event):
        pass


@pytest.fixture
def repository(stage2_migrated_postgres_database):
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository,
    )
    from tests.support.postgres import verified_postgres_provider

    return ConversationKernelRepository(
        verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    )


def settle(repository, lease, candidate, physical):
    execution = object.__new__(ToolBatchExecutor)
    execution._repository, execution._writer_lease = repository, lease
    execution._io, execution._live_bus = InlineIO(), LiveBus()
    execution._tool_output_processor = ToolOutputProcessor()
    execution._canonical_deadline = lambda: monotonic() + 60
    execution._tools = SimpleNamespace()
    result = KernelToolResult(
        "SUCCESS",
        physical.business_value,
        acceptance_succeeded=physical.acceptance_succeeded,
    )
    return execution._settle_known_tool_result(
        session_id=candidate.session_id,
        turn_id=candidate.turn_id,
        assistant_entry_id=candidate.assistant_entry_id,
        tool_name="exec_process",
        tool_call_id=candidate.tool_call_id,
        invocation_arguments={"command": "printf actual-output; exit 7"},
        call_ordinal=0,
        scope_kind=ModelInputScopeKind.ROOT,
        result_id=candidate.result_id,
        result_entry_id=candidate.result_entry_id,
        attempt_id=candidate.attempt_id,
        workspace_id=candidate.workspace_id,
        result=result,
        observed_at=datetime.now(timezone.utc),
        observation_origin=ToolObservationOrigin.BUILTIN,
        live_sink=None,
        tool_result_block_id=random_id("block"),
        live_attribution={},
    )


@pytest.mark.parametrize("failure", ["save_failed", "lost_ack"])
def test_real_process_position_advances_only_after_complete_accept(
    repository, tmp_path, monkeypatch, failure
):
    from pulsara_agent.conversation_kernel._repository import tools

    manager = TerminalManager(tmp_path)
    owner = random_id("host")
    manager.activate_owner(owner)
    lease, candidate = prepare(repository)
    try:
        observed = manager.execute(
            TerminalRequest("printf actual-output; exit 7"),
            owner_host_session_id=owner,
            decision_deadline_monotonic=monotonic() + 30,
        )
        assert observed.exit_code == 7
        callbacks = []
        advance = _terminal_acceptance(manager, owner, observed)

        def accepted():
            callbacks.append(True)
            advance()

        physical = _terminal_execution_result(
            ToolCall(candidate.tool_call_id, "exec_process"),
            observed,
            acceptance_succeeded=accepted,
        )

        def pending():
            return manager.wait_process(
                observed.process_id,
                timeout_seconds=0,
                max_output_chars=None,
                since_cursor=manager.model_observation_cursor(
                    observed.process_id, owner_host_session_id=owner
                ),
                owner_host_session_id=owner,
            ).output

        assert pending() == "actual-output"
        if failure == "save_failed":

            def fail(*args, **kwargs):
                raise OSError("disk failure after completed process")

            monkeypatch.setattr(tools, "write_content_parts", fail)
            with pytest.raises(ToolResultSaveError):
                asyncio.run(settle(repository, lease, candidate, physical))
            assert callbacks == [] and pending() == "actual-output"
        else:
            accept = repository.accept_tool_result
            commits = []

            def lose_ack(*args, **kwargs):
                commits.append(True)
                accept(*args, **kwargs)
                raise TimeoutError("ACK lost after durable complete value")

            monkeypatch.setattr(repository, "accept_tool_result", lose_ack)
            asyncio.run(settle(repository, lease, candidate, physical))
            assert commits == [True] and callbacks == [True] and pending() == ""
            value = (
                PostgresToolOutputReadPort(
                    repository.connection_provider,
                    session_id=candidate.session_id,
                    workspace_id=candidate.workspace_id,
                )
                .read_result(
                    {"kind": "SESSION_ENTRY", "entry_id": candidate.result_entry_id}
                )
                .value
            )
            assert (
                value["meta"]["status"] == "SUCCESS" and value["meta"]["exit_code"] == 7
            )
            assert value["content"] == ({"type": "text", "value": "actual-output"},)
    finally:
        manager.release_owner_and_join(owner)
