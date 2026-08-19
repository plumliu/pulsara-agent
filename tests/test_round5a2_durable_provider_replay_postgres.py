"""PostgreSQL atomicity and privilege tests for Round 5A.2 replay."""

from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4

import psycopg
import pytest

from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
)
from pulsara_agent.llm.provider_replay import (
    ProviderReplayDisposition,
    build_prepared_durable_provider_assistant_replay,
    build_provider_replay_target_compatibility,
)
from pulsara_agent.llm.request import (
    provider_assistant_public_projection_fingerprint,
)
from pulsara_agent.primitives.context import FrozenJsonObjectFact, freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.postgres import verified_postgres_provider


pytestmark = pytest.mark.postgres


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _open_turn(repository: ConversationKernelRepository, lease):
    turn_id = _name("turn")
    repository.start_root_turn(
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=_name("entry"),
        context_binding_revision_id=_name("revision"),
        permission_snapshot_id=_name("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        content=InlineContent.from_bytes(b"prompt"),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    return turn_id, repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )


def _native_candidate(*, session_id: str, workspace_id: str, entry_id: str):
    frozen = freeze_json(
        {
            "role": "assistant",
            "content": "answer",
            "reasoning_content": "private-native-carrier",
        }
    )
    assert isinstance(frozen, FrozenJsonObjectFact)
    target = build_provider_replay_target_compatibility(
        wire_api="openai_chat_completions",
        endpoint_identity_fingerprint="sha256:" + "1" * 64,
        normalized_model_identifier="test-model",
        transport_binding_id="openai_chat_completions",
    )
    return build_prepared_durable_provider_assistant_replay(
        session_id=session_id,
        workspace_id=workspace_id,
        assistant_entry_id=entry_id,
        target=target,
        public_projection_fingerprint=(
            provider_assistant_public_projection_fingerprint(
                text="answer", tool_calls=()
            )
        ),
        ordered_items=(frozen,),
    )


def _commit_arguments(repository, lease, *, workspace_id: str):
    _turn_id, cut = _open_turn(repository, lease)
    entry_id = _name("assistant")
    content = InlineContent.from_bytes(b"answer")
    blocks = (AssistantTextBlock(_name("block"), content),)
    replay = _native_candidate(
        session_id=lease.guard.session_id,
        workspace_id=workspace_id,
        entry_id=entry_id,
    )
    occurred_at = datetime.now(timezone.utc)
    return {
        "cut": cut,
        "entry_id": entry_id,
        "parent_content": content,
        "blocks": blocks,
        "provider_wire_api": "openai_chat_completions",
        "provider_replay_disposition": ProviderReplayDisposition.NATIVE_REPLAY,
        "provider_replay": replay,
        "complete_turn": True,
        "occurred_at": occurred_at,
        "actor_id": "model:test",
    }


def test_round5a2_native_composite_confirms_exact_and_is_runtime_immutable(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    arguments = _commit_arguments(repository, lease, workspace_id=workspace_id)
    assert repository.confirm_assistant_message_winner(
        lease.guard,
        **arguments,
        deadline_monotonic=monotonic() + 30,
    ) is None
    accepted = repository.commit_assistant_message(
        lease.guard,
        **arguments,
        deadline_monotonic=monotonic() + 30,
    )
    confirmed = repository.confirm_assistant_message_winner(
        lease.guard,
        **arguments,
        deadline_monotonic=monotonic() + 30,
    )
    assert confirmed == accepted
    with pytest.raises(ConversationKernelConflict):
        repository.confirm_assistant_message_winner(
            lease.guard,
            **{
                **arguments,
                "provider_replay_disposition": (
                    ProviderReplayDisposition.PUBLIC_SEMANTIC_ONLY
                ),
                "provider_replay": None,
            },
            deadline_monotonic=monotonic() + 30,
        )

    for statement, values in (
        (
            "UPDATE pulsara_v3.transcript_entries SET provider_replay_fragment_id = NULL "
            "WHERE session_id = %s AND id = %s",
            (session_id, arguments["entry_id"]),
        ),
        (
            "UPDATE pulsara_v3.provider_assistant_replay_fragments "
            "SET payload_digest = %s WHERE session_id = %s",
            ("sha256:" + "0" * 64, session_id),
        ),
    ):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(
                stage2_migrated_postgres_database.runtime_dsn
            ) as connection:
                connection.execute(statement, values)


def test_round5a2_deferred_fk_rejects_replay_without_assistant(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    candidate = _native_candidate(
        session_id=session_id,
        workspace_id=workspace_id,
        entry_id=_name("missing-assistant"),
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with psycopg.connect(
            stage2_migrated_postgres_database.runtime_dsn
        ) as connection:
            connection.execute(
                """
                INSERT INTO pulsara_v3.provider_assistant_replay_fragments (
                    id, session_id, workspace_id, assistant_entry_id,
                    wire_api, codec_kind, provider_replay_contract_fingerprint,
                    replay_target_fingerprint, public_projection_fingerprint,
                    payload_bytes, payload_digest, payload_size, item_count,
                    fragment_fingerprint
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    candidate.replay_id,
                    candidate.session_id,
                    candidate.workspace_id,
                    candidate.assistant_entry_id,
                    candidate.wire_api,
                    candidate.codec_kind.value,
                    candidate.provider_replay_contract_fingerprint,
                    candidate.replay_target_fingerprint,
                    candidate.public_projection_fingerprint,
                    candidate.payload_bytes,
                    candidate.payload_digest,
                    candidate.payload_size,
                    candidate.item_count,
                    candidate.fragment_fingerprint,
                ),
            )


class _FailAfterReplayRepository(ConversationKernelRepository):
    fail_events = False

    def _append_events(self, *args, **kwargs):
        if self.fail_events:
            raise RuntimeError("injected post-replay transaction failure")
        return super()._append_events(*args, **kwargs)


def test_round5a2_transaction_failure_rolls_back_assistant_blocks_and_replay(
    stage2_migrated_postgres_database,
) -> None:
    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = _FailAfterReplayRepository(provider)
    session_id = _name("session")
    workspace_id = _name("workspace")
    lease = repository.acquire_host_writer(
        session_id=session_id,
        workspace_id=workspace_id,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    arguments = _commit_arguments(repository, lease, workspace_id=workspace_id)
    repository.fail_events = True
    with pytest.raises(RuntimeError, match="post-replay"):
        repository.commit_assistant_message(
            lease.guard,
            **arguments,
            deadline_monotonic=monotonic() + 30,
        )
    with provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.transcript_entries "
            "WHERE session_id = %s AND id = %s",
            (session_id, arguments["entry_id"]),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.assistant_message_blocks "
            "WHERE session_id = %s AND assistant_entry_id = %s",
            (session_id, arguments["entry_id"]),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.provider_assistant_replay_fragments "
            "WHERE session_id = %s AND assistant_entry_id = %s",
            (session_id, arguments["entry_id"]),
        ).fetchone() == (0,)
