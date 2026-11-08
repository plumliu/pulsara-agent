"""Final settlement and exact steering retain their canonical arbitration."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event, local
from time import monotonic
import asyncio

import pytest
from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel._repository.contracts import PromptIngressRejected
from pulsara_agent.conversation_kernel.contracts import InlineContent, PromptDeliveryMode
from pulsara_agent.conversation_kernel.direct_model import quote_provider_followup_wire_resources
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.prompt_content import freeze_canonical_prompt
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
from pulsara_agent.conversation_kernel.runner import ConversationKernelRunner, OutputResourceInterruption
from pulsara_agent.identity import random_id
from pulsara_agent.llm.input import FrozenPromptContent
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.model_config import enqueue_test_prompt, test_model_resolution_snapshot
from tests.support.round3 import StaticContextSourceCollector, StructuredToolPort
from tests.test_canonical_request_start_postgres import (
    accept,
    intent_for,
    root as root,
)
from tests.test_stage2_conversation_runner import (
    _AssertingTool,
    _ScriptedModel,
    _text_stream,
    frozen_test_prompt,
)


pytestmark = pytest.mark.postgres


def _now():
    return datetime.now(timezone.utc)


def _rows(repository, query, parameters):
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        return connection.execute(query, parameters).fetchall()


@pytest.mark.parametrize("first", ("steer", "final"))
def test_exact_steer_and_plain_final_have_one_transactional_winner(root, monkeypatch, first):
    repository, lease, runtime, _ = root
    intent = intent_for(lease, "original request")
    accept(repository, lease, runtime, intent)
    turn_id = intent.turn_id
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    before = _rows(repository,
        "SELECT workspace_id, permission_snapshot_id, permission_snapshot_fingerprint "
        "FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
        (lease.guard.session_id, turn_id))
    frozen = CanonicalProviderInputReader(repository.connection_provider).read_frozen_dispatch(
        cut, deadline_monotonic=monotonic() + 30
    )
    assert frozen.compile_snapshot.canonical_input.identity.conversation_scope_kind is ModelInputScopeKind.ROOT
    queue_id, command_id, final_id = random_id("queue"), random_id("command"), random_id("entry")

    def steer():
        return enqueue_test_prompt(
            repository, lease.guard, command_id=command_id, queue_item_id=queue_id,
            client_submission_id=command_id, delivery_mode=PromptDeliveryMode.STEER_ACTIVE_TURN,
            target_turn_id=turn_id, permission_snapshot_id=None,
            requested_permission_mode=None, model_call_binding=None,
            content=FrozenPromptContent.text("the exact late steer"), occurred_at=_now(),
            actor_id="user", deadline_monotonic=monotonic() + 30,
        )

    def final():
        return repository.commit_assistant_message(
            lease.guard, cut=cut, entry_id=final_id,
            parent_content=InlineContent.from_bytes(b"finished"),
            blocks=(AssistantTextBlock(random_id("block"), InlineContent.from_bytes(b"finished")),),
            complete_turn=True, occurred_at=_now(), actor_id="model:foreground",
            deadline_monotonic=monotonic() + 30,
        )

    # Pause after the existing writer transaction has acquired its real lock.
    # The competing operation uses the same production transaction owner.
    roles = local()
    winner_locked, loser_entered, release_winner = Event(), Event(), Event()
    require_writer = repository._require_writer

    def observe_writer(*args, **kwargs):
        role = getattr(roles, "value", None)
        if role == "loser":
            loser_entered.set()
        result = require_writer(*args, **kwargs)
        if role == "winner":
            winner_locked.set()
            assert release_winner.wait(10), "competing writer did not enter"
        return result

    monkeypatch.setattr(repository, "_require_writer", observe_writer)

    def run(role, operation):
        roles.value = role
        return operation()

    operations = {"steer": steer, "final": final}
    second = "final" if first == "steer" else "steer"
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(run, "winner", operations[first])
        try:
            assert winner_locked.wait(10)
            loser = pool.submit(run, "loser", operations[second])
            assert loser_entered.wait(10)
        finally:
            release_winner.set()
        winner_result = winner.result(timeout=10)
        if first == "steer":
            final_result = loser.result(timeout=10)
            assert winner_result == 1
            assert not final_result.turn_completed
            assert final_result.pending_steer_at_settlement
        else:
            assert winner_result.turn_completed
            assert not winner_result.pending_steer_at_settlement
            with pytest.raises(PromptIngressRejected) as rejected:
                loser.result(timeout=10)
            assert rejected.value.reason.value == "TARGET_STALE_OR_NON_STEERABLE"

    turn = _rows(repository,
        "SELECT status, final_entry_id FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
        (lease.guard.session_id, turn_id))[0]
    queued = _rows(repository,
        "SELECT id, status, target_turn_id, permission_snapshot_id, requested_permission_mode, "
        "model_call_binding FROM pulsara_v3.prompt_queue_items WHERE session_id=%s",
        (lease.guard.session_id,))
    if first == "steer":
        assert turn == {"status": "RUNNING", "final_entry_id": None}
        assert queued == [{"id": queue_id, "status": "PENDING", "target_turn_id": turn_id,
            "permission_snapshot_id": None, "requested_permission_mode": None,
            "model_call_binding": None}]
        facts = repository.read_pending_prompt_steer_facts(
            session_id=lease.guard.session_id, target_turn_id=turn_id,
            deadline_monotonic=monotonic() + 30,
        )
        assert len(facts) == 1
        assert facts[0].queue_item_id == queue_id
        assert facts[0].session_id == lease.guard.session_id
        assert facts[0].workspace_id == before[0]["workspace_id"]
        assert facts[0].exact_target_turn_id == turn_id
        assert isinstance(facts[0].body_storage, InlineContent)
        assert facts[0].body_storage.canonical_bytes == freeze_canonical_prompt(
            FrozenPromptContent.text("the exact late steer")
        ).body
    else:
        assert turn == {"status": "COMPLETED", "final_entry_id": final_id}
        assert queued == []
    assert _rows(repository,
        "SELECT workspace_id, permission_snapshot_id, permission_snapshot_fingerprint "
        "FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
        (lease.guard.session_id, turn_id)) == before
    assert _rows(repository,
        "SELECT id FROM pulsara_v3.transcript_entries WHERE session_id=%s "
        "AND entry_kind='ASSISTANT_MESSAGE'", (lease.guard.session_id,)) == [{"id": final_id}]
    completed = _rows(repository,
        "SELECT event_id FROM pulsara_v3.agent_events WHERE session_id=%s AND event_type='TurnCompleted'",
        (lease.guard.session_id,))
    assert len(completed) == int(first == "final")


def test_legal_final_settles_when_its_hypothetical_followup_exceeds_input_budget(root, monkeypatch):
    import pulsara_agent.conversation_kernel.runner as runner_module

    repository, lease, _, _ = root
    model = _ScriptedModel([_text_stream("finished answer " * 1024)])
    runner = ConversationKernelRunner(
        model_resolution_snapshot_provider=test_model_resolution_snapshot,
        repository=repository, writer_lease=lease, model=model,
        tools=StructuredToolPort(_AssertingTool(repository.connection_provider, lease.guard.session_id), tool_names=()),
        live_bus=LiveAgentEventBus(), context_source_collector=StaticContextSourceCollector(),
        maximum_input_tokens_per_call=1024,
    )
    followup_calls = []
    resource_quotes = []
    quote_response = runner._quote_post_response_resources

    def observe_response(**kwargs):
        quote = quote_response(**kwargs)
        resource_quotes.append(quote)
        return quote

    def observe_followup(**kwargs):
        followup_calls.append(kwargs)
        return quote_provider_followup_wire_resources(**kwargs)

    monkeypatch.setattr(runner_module, "quote_provider_followup_wire_resources", observe_followup)
    monkeypatch.setattr(runner, "_quote_post_response_resources", observe_response)
    result = asyncio.run(runner.run_turn(frozen_test_prompt("Give the completed answer.")))
    assert result.final_text == "finished answer " * 1024
    assert result.model_call_count == 1
    assert len(model.requests) == 1
    request = model.requests[0]
    assert request.wire_input_plan.quote.budget_input_tokens <= 1024
    assert result.final_source is not None
    hypothetical = quote_provider_followup_wire_resources(
        plan=request.wire_input_plan, call=request.prepared_call.call,
        actual_assistant_message=result.final_source.final_message,
        provider_replay=result.final_source.provider_replay, bounded_suffix_messages=(),
    )
    assert hypothetical.budget_input_tokens > request.wire_input_plan.quote.effective_input_budget_tokens
    assert followup_calls == []
    assert len(resource_quotes) == 1
    with pytest.raises(OutputResourceInterruption) as rejected:
        runner._require_post_response_resources(
            replace(resource_quotes[0], followup_wire=hypothetical),
            effective_input_budget_tokens=request.wire_input_plan.quote.effective_input_budget_tokens,
        )
    assert rejected.value.reason == "FOLLOWUP_INPUT_TOKENS"
    assert _rows(repository,
        "SELECT status, final_entry_id FROM pulsara_v3.turns WHERE session_id=%s AND id=%s",
        (lease.guard.session_id, result.turn_id)) == [{"status": "COMPLETED", "final_entry_id": result.final_entry_id}]
