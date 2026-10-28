"""Invalid canonical execution facts are not rejected user submissions."""

import asyncio
import json

import pytest

from pulsara_agent.conversation_kernel._repository.request_execution import request_execution_from_row
from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
from pulsara_agent.desktop_app.http_server import LocalHttpServer


@pytest.mark.parametrize("changed", [
    {"execution_entry_id": None},
    {"execution_initial_entry_id": "entry:another"},
    {"execution_scope_kind": "SUBAGENT_TASK"},
    {"execution_turn_status": "INTERRUPTED", "execution_turn_reason": None},
])
def test_inconsistent_request_facts_remain_a_query_failure(changed):
    row = {
        "execution_queue_id": "queue:request",
        "execution_delivery_mode": "NEW_TURN",
        "execution_queue_status": "CONSUMED",
        "execution_queue_reason": "CONSUMED",
        "execution_consumed_entry_id": "entry:initial",
        "execution_entry_id": "entry:initial",
        "execution_entry_kind": "USER_MESSAGE",
        "execution_entry_turn_id": "turn:request",
        "execution_turn_id": "turn:request",
        "execution_initial_entry_id": "entry:initial",
        "execution_scope_kind": "ROOT",
        "execution_turn_status": "RUNNING",
        "execution_turn_reason": None,
    }
    assert request_execution_from_row(row).status == "running"
    row.update(changed)
    with pytest.raises(ConversationKernelConflict):
        request_execution_from_row(row)

    async def query(_request):
        return request_execution_from_row(row)

    # The existing error middleware needs no configured server for this path.
    server = object.__new__(LocalHttpServer)
    response = asyncio.run(server._errors(None, query))
    assert response.status == 409
    error = json.loads(response.text)["error"]
    assert error["code"] == "LOCAL_RUNTIME_ERROR"
    assert error["retryable"] is True
