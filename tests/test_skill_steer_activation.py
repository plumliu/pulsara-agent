"""Queued user inputs retain their independent explicit Skill references."""

import asyncio
from datetime import datetime, timezone
from time import monotonic

import pytest

from pulsara_agent.capability.resolver import _explicit_skill_names
from pulsara_agent.conversation_kernel.contracts import PromptDeliveryMode
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner
from pulsara_agent.identity import derive_id
from pulsara_agent.llm.input import FrozenPromptContent, MessageRole, join_text_content
from tests.support.model_config import frozen_test_prompt
from tests.support.round3 import StaticContextSourceCollector, StructuredToolPort
from tests.test_stage2_conversation_runner import (
    _AssertingTool,
    _BlockingFirstCallModel,
    _name,
    _PostAdmissionPendingSteerReadGate,
    _ScriptedModel,
    _acquire_bound_host_writer,
    _text_stream,
    enqueue_test_prompt,
    test_model_resolution_snapshot,
    verified_postgres_provider,
)


pytestmark = pytest.mark.postgres


class _RecordingSkillCollector(StaticContextSourceCollector):
    def __init__(self):
        self.selections = []

    def complete_frozen_sources(self, frozen, **kwargs):
        self.selections.append(
            _explicit_skill_names(kwargs["skill_reference_surfaces"])
        )
        return super().complete_frozen_sources(frozen, **kwargs)


@pytest.mark.parametrize("before_first_dispatch", [False, True])
@pytest.mark.parametrize(
    "messages,selected",
    [
        (("Use /alpha", "One more requirement."), ("alpha",)),
        (("Use /alpha", "Also use /beta"), ("alpha", "beta")),
        (("```\n/quoted", "Use /beta"), ("beta",)),
    ],
)
def test_adopted_user_batch_preserves_skill_references(
    stage2_migrated_postgres_database,
    monkeypatch,
    before_first_dispatch,
    messages,
    selected,
):
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    lease = _acquire_bound_host_writer(
        repository,
        session_id=session_id,
        workspace_id=_name("workspace"),
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    command_id = _name("command")
    turn_id = derive_id("turn", "command", session_id, command_id)
    collector = _RecordingSkillCollector()
    if before_first_dispatch:
        model = _ScriptedModel([_text_stream("final answer")])
        gate = _PostAdmissionPendingSteerReadGate(
            repository.read_pending_prompt_steer_facts, target_turn_id=turn_id
        )
        monkeypatch.setattr(repository, "read_pending_prompt_steer_facts", gate)
    else:
        model = _BlockingFirstCallModel(
            [_text_stream("first answer"), _text_stream("final answer", block="text:2")]
        )
        gate = None
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository,
        writer_lease=lease,
        model=model,
        tools=StructuredToolPort(_AssertingTool(provider, session_id), tool_names=()),
        live_bus=LiveAgentEventBus(),
        context_source_collector=collector,
    )

    async def exercise():
        task = asyncio.create_task(
            runner.run_turn(frozen_test_prompt("Use /base"), command_id=command_id)
        )
        try:
            if gate is not None:
                assert await asyncio.to_thread(gate.started.wait, 5)
            else:
                await asyncio.wait_for(model.started.wait(), timeout=5)
            for text in messages:
                steer_command = _name("command")
                enqueue_test_prompt(
                    repository,
                    lease.guard,
                    command_id=steer_command,
                    queue_item_id=_name("queue"),
                    client_submission_id=steer_command,
                    delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
                    target_turn_id=turn_id,
                    permission_snapshot_id=None,
                    requested_permission_mode=None,
                    model_call_binding=None,
                    content=FrozenPromptContent.text(text),
                    occurred_at=datetime.now(timezone.utc),
                    actor_id="test",
                    deadline_monotonic=monotonic() + 10,
                )
        finally:
            if gate is not None:
                gate.release.set()
            else:
                model.release.set()
        return await asyncio.wait_for(task, 20)

    result = asyncio.run(exercise())
    assert result.final_text == "final answer"
    expected = tuple(sorted((*selected, "base"))) if before_first_dispatch else selected
    assert collector.selections[-1] == expected
    latest = model.requests[-1].compiled_input
    if not before_first_dispatch:
        first = model.requests[0].compiled_input
        assert latest.system_prompt == first.system_prompt
        assert latest.tools == first.tools
        assert latest.messages[: len(first.messages)] == first.messages
    delivered_users = [
        join_text_content(message.content)
        for message in latest.messages
        if message.role is MessageRole.USER
        and "pulsara_runtime_observation" not in join_text_content(message.content)
    ]
    assert delivered_users[-len(messages):] == list(messages)
