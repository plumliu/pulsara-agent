"""Frozen identity encoding and live-owner collision contracts."""

from __future__ import annotations

import base64
from hashlib import sha256
import json

import pytest

from pulsara_agent.identity import (
    content_revision,
    derive_id,
    is_valid_id,
    random_id,
    require_id,
)


@pytest.mark.parametrize(
    "kind,role,parts,expected",
    [
        (
            "task",
            "spawn",
            ("attempt:" + "a" * 26, "0"),
            "task:htl27gtxmwe2htij6arwsvscbq",
        ),
        (
            "entry",
            "root-user",
            ("turn:" + "a" * 26,),
            "entry:bfsgawrz4vz4mqz5utg5ld4zey",
        ),
        (
            "image",
            "content",
            ("workspace:" + "a" * 26, "image/png", "test-codec", "sha256:" + "0" * 64),
            "image:3rabtsk47f56hr576ld5zfdbja",
        ),
    ],
)
def test_frozen_derivation_vectors(kind, role, parts, expected):
    assert derive_id(kind, role, *parts) == expected
    assert require_id(expected, kind) == expected


def test_random_encoding_keeps_all_128_bits(monkeypatch):
    raw = bytes(range(16))
    monkeypatch.setattr(
        "pulsara_agent.identity.secrets.token_bytes",
        lambda size: raw if size == 16 else pytest.fail("wrong entropy width"),
    )
    value = random_id("command")
    assert is_valid_id(value, "command")
    assert base64.b32decode(value.split(":")[1].upper() + "======") == raw


@pytest.mark.parametrize(
    "value",
    [
        "task:abc",
        "task:" + "0" * 32,
        "task:" + "a" * 25 + "b",
        "TASK:" + "a" * 26,
        "task:" + "a" * 26 + "=",
        "task:" + "a" * 26 + "\n",
        " task:" + "a" * 26,
        "image:" + "a" * 26,
    ],
)
def test_exact_kind_canonical_encoding_only(value):
    assert not is_valid_id(value, "task")
    with pytest.raises(ValueError, match="INVALID_ID"):
        require_id(value, "task")


def test_derivation_preserves_unicode_delimiters_and_empty_positions():
    parts = ("中文🙂", "a:b/c", "", "e\u0301")
    expected = sha256(
        json.dumps(
            ["pulsara-id-v1", "entry", "fixture", *parts],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).digest()[:16]
    assert (
        derive_id("entry", "fixture", *parts)
        == "entry:" + base64.b32encode(expected).decode().rstrip("=").lower()
    )
    assert derive_id("entry", "fixture", "é") != derive_id(
        "entry", "fixture", "e\u0301"
    )
    assert derive_id("entry", "fixture", "a", "b") != derive_id(
        "entry", "fixture", "a:b"
    )
    assert derive_id("entry", "fixture", "a") != derive_id("entry", "other", "a")
    with pytest.raises((UnicodeError, ValueError)):
        derive_id("entry", "fixture", "\ud800")
    with pytest.raises(ValueError):
        derive_id("entry", "fixture", 1)
    with pytest.raises(ValueError, match="unknown"):
        random_id("unregistered")


def test_revision_retains_full_digest():
    raw = b"exact\r\nbytes\x00"
    value = content_revision(raw)
    assert len(value.split(":")[1]) == 52
    assert (
        base64.b32decode(value.split(":")[1].upper() + "====") == sha256(raw).digest()
    )
    assert value != content_revision(raw + b" ")


@pytest.mark.parametrize("finish_first", [False, True])
def test_assembler_rejects_derived_collision_before_live_publication(
    monkeypatch, finish_first
):
    from tests.test_stage2_live_contract import _assembler
    from pulsara_agent.ports.live_agent_event import (
        TextStartPayload,
        TextEndPayload,
        live_digest,
    )

    assembler = _assembler()
    fixed = derive_id("block", "test", "collision")
    monkeypatch.setattr(
        "pulsara_agent.conversation_kernel.assembler._canonical_block_id",
        lambda *args: fixed,
    )
    assembler.apply(TextStartPayload("native:first"))
    if finish_first:
        assembler.apply(TextEndPayload("native:first", "", 0, live_digest("")))
    with pytest.raises(RuntimeError, match="IDENTITY_CONFLICT"):
        assembler.apply(TextStartPayload("native:second"))
    assert "native:second" not in assembler._seen
    assert "native:second" not in assembler._open


@pytest.mark.parametrize("kind", ["turn", "history"])
def test_tool_result_source_roundtrip_keeps_imported_group_separate_from_execution(
    kind,
):
    from dataclasses import replace
    from pulsara_agent.primitives.tool_result_projection import (
        _maximum_artifact_page_timing,
        _minimum_full_projection_timing,
        render_provider_tool_result_logical_message,
        decode_provider_tool_result_observation,
        ToolResultLogicalMessageKind,
    )
    from pulsara_agent.primitives.tool_observation import (
        tool_observation_timing_fingerprint,
        provider_visible_turn_ref,
    )

    maximum = _maximum_artifact_page_timing()
    provisional = object.__new__(type(maximum))
    for name in maximum.__dataclass_fields__:
        object.__setattr__(provisional, name, getattr(maximum, name))
    object.__setattr__(
        provisional, "source_turn_ref", derive_id(kind, "fixture", "source")
    )
    timing = replace(
        maximum,
        source_turn_ref=provisional.source_turn_ref,
        fact_fingerprint=tool_observation_timing_fingerprint(provisional),
    )
    rendered = render_provider_tool_result_logical_message(
        message_kind=ToolResultLogicalMessageKind.TOOL_RESULT,
        tool_call_id="provider-native-call",
        body="exact body",
        result_state="SUCCESS",
        timing=timing,
        model_visible_memory_ids=(),
    )
    assert (
        decode_provider_tool_result_observation(rendered.content)["observation"][
            "source_turn_ref"
        ]
        == timing.source_turn_ref
    )
    assert maximum.source_turn_ref.startswith("history:")
    assert _minimum_full_projection_timing().source_turn_ref.startswith("turn:")
    assert len(timing.source_turn_ref) <= len(maximum.source_turn_ref)
    if kind == "history":
        with pytest.raises(ValueError, match="INVALID_ID"):
            provider_visible_turn_ref(
                session_id=random_id("session"), turn_id=timing.source_turn_ref
            )


@pytest.mark.postgres
def test_blob_forced_derived_collision_preserves_original_and_rolls_back(
    stage2_migrated_postgres_database, monkeypatch
):
    from time import monotonic
    from tests.support.postgres import verified_postgres_provider
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository,
        ConversationKernelConflict,
    )
    from pulsara_agent.conversation_kernel.blob import PostgresCanonicalBlobStore

    provider = verified_postgres_provider(stage2_migrated_postgres_database.runtime_dsn)
    repository = ConversationKernelRepository(provider)
    workspace = random_id("workspace")
    lease = repository.acquire_host_writer(
        intent="NEW",
        session_id=random_id("session"),
        workspace_id=workspace,
        writer_owner_id=random_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    store = PostgresCanonicalBlobStore(provider)

    def publish(body):
        return store.publish(
            workspace_id=workspace,
            content=body,
            media_type="text/plain",
            codec="utf-8",
            deadline_monotonic=monotonic() + 30,
        )

    first = publish(b"original immutable bytes")
    assert publish(b"original immutable bytes") == first
    with monkeypatch.context() as patch:
        patch.setattr(
            "pulsara_agent.conversation_kernel.blob._blob_id",
            lambda *args, **kwargs: first.blob_id,
        )
        with pytest.raises(ConversationKernelConflict, match="IDENTITY_CONFLICT"):
            publish(b"different bytes with forced short-ID collision")
    assert (
        store.read_exact(
            blob_id=first.blob_id,
            expected_digest=first.digest,
            expected_size=first.size,
            deadline_monotonic=monotonic() + 30,
        )
        == b"original immutable bytes"
    )
    assert (
        publish(b"different bytes with forced short-ID collision").blob_id
        != first.blob_id
    )
    repository.release_host_writer(lease.guard, deadline_monotonic=monotonic() + 30)


def test_terminal_random_collision_retries_before_publication_and_keeps_sibling(
    tmp_path, monkeypatch
):
    from time import monotonic
    from pulsara_agent.terminal_process.manager import ProcessRegistry
    from tests.test_round5_long_horizon_execution_envelope import (
        _exec_sleeping_terminal,
    )

    registry = ProcessRegistry(max_live_processes=2)
    first = second = None
    try:
        first, _ = _exec_sleeping_terminal(
            registry,
            tmp_path,
            attempt_id=random_id("terminal-decision"),
            decision_deadline_monotonic=monotonic() + 10,
            yield_time_ms=0,
        )
        allocated = random_id("process")
        candidates = iter((first.process_id, allocated))

        def allocate(kind):
            return next(candidates) if kind == "process" else random_id(kind)

        monkeypatch.setattr(
            "pulsara_agent.terminal_process.manager.random_id", allocate
        )
        second, _ = _exec_sleeping_terminal(
            registry,
            tmp_path,
            attempt_id=random_id("terminal-decision"),
            decision_deadline_monotonic=monotonic() + 10,
            yield_time_ms=0,
        )
        assert second.process_id == allocated and second.process_id != first.process_id
        assert (
            registry.poll(
                first.process_id,
                max_output_chars=32,
                owner_host_session_id="round5:host",
            ).status.value
            == "running"
        )
        assert (
            registry.poll(
                second.process_id,
                max_output_chars=32,
                owner_host_session_id="round5:host",
            ).status.value
            == "running"
        )
    finally:
        registry.release_owner("round5:host", timeout_seconds=2)
