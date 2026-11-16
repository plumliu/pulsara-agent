"""Current assistant history fixtures shared without importing a tool test suite."""

from datetime import datetime, timezone
from time import monotonic
from pulsara_agent.identity import random_id
from pulsara_agent.conversation_kernel.repository import AssistantToolCallBlock
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from tests.support.model_config import (
    start_test_root_turn,
    test_model_binding,
    test_model_runtime,
)


def saved_calls(repo, lease, blocks):
    turn_id = random_id("turn")
    start_test_root_turn(
        repo,
        lease.guard,
        command_id=random_id("command"),
        turn_id=turn_id,
        entry_id=random_id("entry"),
        context_binding_revision_id=random_id("binding"),
        permission_snapshot_id=random_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        content=FrozenPromptContent.text("working"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
        model_call_binding=test_model_binding(test_model_runtime()),
    )
    cut = repo.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    entry = random_id("entry")
    repo.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=entry,
        blocks=blocks,
        complete_turn=False,
        actor_id="model:test",
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    return entry


def call_block(name, arguments):
    return AssistantToolCallBlock(
        random_id("block"), random_id("call"), name, freeze_json(arguments)
    )
