"""Batching must preserve which candidate is first allowed to fail validation."""

from datetime import datetime, timezone
from time import monotonic

import psycopg
from psycopg.types.json import Jsonb
import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import AssistantTextBlock
from pulsara_agent.conversation_kernel.repository_errors import ConversationKernelConflict
from pulsara_agent.identity import random_id
from pulsara_agent.llm.provider_replay import build_prepared_durable_provider_assistant_replay
from pulsara_agent.llm.request import provider_assistant_public_projection_fingerprint
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.terminal_protocol.canonical_v3 import (
    CanonicalProtocolReader, MAXIMUM_CONTROL_ITEMS, MAXIMUM_HISTORY_PAGE_BYTES,
)
from pulsara_agent.terminal_protocol.generated_v3 import terminal_kernel_v3_pb2 as wire
from tests.support.model_config import build_test_provider_replay_target
from tests.test_conversation_fork import new_session, repo as repo, turn
from tests.test_turn_interruption_history import stop


pytestmark = pytest.mark.postgres


def _old_replay(repository, lease):
    _, cut, _ = turn(repository, lease.guard, "old", finish=False)
    entry_id = random_id("entry")
    replay = build_prepared_durable_provider_assistant_replay(
        session_id=lease.guard.session_id,
        workspace_id=repository.read_session_workspace_id(
            lease.guard, deadline_monotonic=monotonic() + 30,
        ),
        assistant_entry_id=entry_id,
        target=build_test_provider_replay_target(model_id="model", transport_binding_id="test-wire"),
        public_projection_fingerprint=provider_assistant_public_projection_fingerprint(text="old", tool_calls=()),
        ordered_items=(freeze_json({"role": "assistant", "content": "old", "reasoning_content": "old reasoning"}),),
    )
    repository.commit_assistant_message(
        lease.guard, cut=cut, entry_id=entry_id,
        parent_content=InlineContent.from_bytes(b"manifest"),
        blocks=(AssistantTextBlock(random_id("block"), InlineContent.from_bytes(b"old")),),
        provider_replay=replay, complete_turn=True,
        occurred_at=datetime.now(timezone.utc), actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    return entry_id


@pytest.mark.parametrize("corruption", ["replay", "notice"])
@pytest.mark.parametrize("operation", ["snapshot", "history_page"])
def test_deeper_corruption_stays_behind_the_original_byte_admission_boundary(
    repo, stage2_migrated_postgres_database, corruption, operation,
):
    lease = new_session(repo)
    session_id = lease.guard.session_id
    if corruption == "replay":
        old_entry = _old_replay(repo, lease)
    else:
        old_turn, _, old_entry = turn(repo, lease.guard, "old", complete=False)
        stop(repo, lease.guard, old_turn)
    # Descending candidates: new answer (accepted), large new USER (rejected),
    # old assistant (not touched). The old reader validates the first rejected
    # candidate too; this test deliberately leaves it intact.
    turn(repo, lease.guard, "new question " * 700, answer="new answer " * 300)
    reader = CanonicalProtocolReader(repo.connection_provider)

    def read(maximum_entries, maximum_bytes=MAXIMUM_HISTORY_PAGE_BYTES):
        common = dict(session_id=session_id, maximum_entries=maximum_entries,
                      maximum_serialized_bytes=maximum_bytes, deadline_monotonic=monotonic() + 30)
        if operation == "snapshot":
            return reader.snapshot(maximum_control_items=MAXIMUM_CONTROL_ITEMS, **common)
        return reader.history_page(cut_sequence=4, event_sequence_cut=head.event_sequence_cut,
                                   before_entry_sequence=5, request_id="admission", **common)

    head = reader.snapshot(session_id=session_id, maximum_entries=1,
                           maximum_control_items=MAXIMUM_CONTROL_ITEMS,
                           deadline_monotonic=monotonic() + 30)
    expected = read(1)
    budget = (expected.ByteSize() if operation == "snapshot"
              else wire.ServerFrame(history_page=expected).ByteSize())
    assert budget >= 1024
    assert [entry.entry_sequence for entry in expected.entries] == [4]
    assert read(256, budget).SerializeToString(deterministic=True) == expected.SerializeToString(deterministic=True)

    # Tamper only with this fixture's database, using its administrative owner.
    with psycopg.connect(stage2_migrated_postgres_database.admin_dsn) as connection:
        if corruption == "replay":
            connection.execute(
                "UPDATE pulsara_v3.provider_assistant_replay_fragments SET payload_digest=%s "
                "WHERE session_id=%s AND assistant_entry_id=%s",
                ("sha256:" + "0" * 64, session_id, old_entry),
            )
        else:
            connection.execute(
                "UPDATE pulsara_v3.agent_events SET payload=%s "
                "WHERE session_id=%s AND subject_turn_id=%s AND event_type='TurnInterrupted'",
                (Jsonb({"reason": "USER_STOPPED", "public_detail": "mismatched"}), session_id, old_turn),
            )
    assert read(256, budget).SerializeToString(deterministic=True) == expected.SerializeToString(deterministic=True)
    error, match = ((RuntimeError, "replay body is corrupt") if corruption == "replay"
                    else (ConversationKernelConflict, "winner and occurrence disagree"))
    with pytest.raises(error, match=match):
        read(256)
