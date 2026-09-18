"""Focused product, prompt, wire, and source semantics for memory governance v3."""

from __future__ import annotations

from pulsara_agent.llm.input import FrozenPromptContent, LLMImagePart

import asyncio
import json
from datetime import datetime, timezone
from hashlib import sha256
from time import monotonic
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog
from pulsara_agent.conversation_kernel.auxiliary_model import (
    DirectKernelAuxiliaryJsonModel,
    _materialize_auxiliary_final_wire,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    MAXIMUM_GOVERNANCE_OUTPUT_TOKENS,
    FrozenMemoryGovernanceEvidence,
    FrozenMemoryGovernanceProducerCut,
    FrozenMemoryGovernanceSourceBlock,
    FrozenMemoryGovernanceSourceCoverage,
    FrozenMemoryGovernanceSourceItem,
    FrozenMemoryGovernanceTerminalFence,
    FrozenMemoryProposal,
    FrozenMemoryPublicFactProjection,
    MemoryDecisionReasonCode,
    MemoryFactKind,
    MemoryGovernanceChronology,
    MemoryGovernanceEvidenceRole,
    MemoryGovernanceSourceBlockKind,
    MemoryKindHint,
    MODEL_GOVERNANCE_SKIP_REASON_CODES,
    canonical_json_bytes,
    legal_memory_final_kinds,
    memory_fact_semantic_digest,
    prepare_memory_candidate,
)
from pulsara_agent.conversation_kernel.memory.governor import (
    AdvisoryMemoryGovernor,
    _finalize_governance_source_envelope,
    _governance_output_schema,
    _governance_packet_variants,
    _parse_governance_decision,
    _selected_governance_relation_targets,
)
from pulsara_agent.llm.input import LLMMessage, LLMTextPart, MessageRole
from pulsara_agent.memory.product_contract import (
    MEMORY_GOVERNANCE_CONTRACT_ID,
    MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3,
    MEMORY_COHESIVE_UNIT_GUIDE,
    MEMORY_CONTEXT_PRODUCT_GUIDE,
    MEMORY_KIND_PRODUCT_DEFINITIONS,
    MEMORY_RETRIEVAL_AUTHORING_GUIDE,
)
from pulsara_agent.memory.scope import CTX_GLOBAL
from pulsara_agent.memory.scope import (
    MemoryDomainContext,
    freeze_memory_read_context_binding,
)
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.blob import (
    CanonicalContentPublisher,
    PostgresCanonicalBlobStore,
)
from pulsara_agent.conversation_kernel.prompt_content import (
    PROMPT_BODY_CODEC,
    PROMPT_BODY_MEDIA_TYPE,
    freeze_canonical_prompt,
)
from pulsara_agent.conversation_kernel.prompt_storage import (
    insert_canonical_prompt_refs,
    materialize_canonical_prompt,
)
from pulsara_agent.conversation_kernel.repository import (
    AssistantDataBlock,
    AssistantTextBlock,
    AssistantToolCallBlock,
    ConversationKernelConflict,
    ConversationKernelRepository,
    build_prepared_tool_result_acceptance,
)
from pulsara_agent.model_input.contracts import (
    CanonicalInputOriginKind,
    CanonicalModelInputIdentity,
    CanonicalModelInputSnapshot,
    FrozenProviderInputItem,
    FrozenProviderInputItemKind,
    ModelInputScopeKind,
    canonical_model_input_identity_fingerprint,
    canonical_model_input_snapshot_fingerprint,
    provider_input_item_text,
)
from pulsara_agent.conversation_kernel.memory.governor import _causal_source_item
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.reader import CanonicalProviderInputReader
from pulsara_agent.ports.artifact import (
    ToolOutputArtifactDisposition,
    ToolResultDisplayKind,
)
from pulsara_agent.ports.tool_execution import ToolOutputSourceCoverage
from pulsara_agent.primitives.context import freeze_json
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.tool_observation import ToolObservationOrigin
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from pulsara_agent.ports.system_prompt import DEFAULT_SYSTEM_PROMPT
from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy
from pulsara_agent.primitives.model_call import ModelCallPurpose
from tests.support.model_config import (
    start_test_root_turn,
    test_model_binding,
    test_model_runtime,
)
from tests.support.postgres import verified_postgres_provider


def _proposal(
    *,
    context_id: str = CTX_GLOBAL,
    kind_hint: MemoryKindHint = MemoryKindHint.AUTO,
    basis: tuple[str, ...] = (),
) -> FrozenMemoryProposal:
    return FrozenMemoryProposal(
        statement="Use concise release notes",
        context_id=context_id,
        kind_hint=kind_hint,
        based_on_memory_ids=basis,
    )


def _target() -> FrozenMemoryPublicFactProjection:
    statement = "Use detailed release notes"
    return FrozenMemoryPublicFactProjection(
        fact_id="memory:target",
        context_id=CTX_GLOBAL,
        fact_kind=MemoryFactKind.RESPONSE_PREFERENCE,
        lifecycle="ACTIVE",
        statement=statement,
        recorded_at="2026-01-01T00:00:00Z",
        fact_semantic_digest=memory_fact_semantic_digest(
            kind=MemoryFactKind.RESPONSE_PREFERENCE,
            statement=statement,
        ),
    )


def _auxiliary(api: str) -> DirectKernelAuxiliaryJsonModel:
    return DirectKernelAuxiliaryJsonModel(
        test_model_runtime(
            api_key="test-only",
            base_url="https://example.invalid/v1",
            model_id="test-pro",
            wire_api=api,
        ),
    )


def _origin_binding():
    return test_model_binding(test_model_runtime())


def _timeout() -> OpenAITransportTimeoutPolicy:
    return OpenAITransportTimeoutPolicy(1, 1, 1, 1, 5)


def _id(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _repository(database) -> ConversationKernelRepository:
    return ConversationKernelRepository(
        verified_postgres_provider(database.runtime_dsn)
    )


def _lease(repository: ConversationKernelRepository):
    lease = repository.acquire_host_writer(
        session_id=_id("session"),
        workspace_id=_id("workspace"),
        writer_owner_id=_id("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )
    repository.update_session_model_call_binding(
        lease.guard,
        binding=_origin_binding(),
        deadline_monotonic=monotonic() + 30,
    )
    return lease


def _start_human_turn(
    repository: ConversationKernelRepository,
    lease,
    text: str,
) -> tuple[str, str]:
    turn_id = _id("turn")
    entry_id = _id("entry")
    start_test_root_turn(
        repository,
        lease.guard,
        command_id=_id("command"),
        turn_id=turn_id,
        entry_id=entry_id,
        context_binding_revision_id=_id("revision"),
        permission_snapshot_id=_id("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        model_call_binding=_origin_binding(),
        content=FrozenPromptContent.text(text),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    return turn_id, entry_id


def _complete_turn(
    repository: ConversationKernelRepository,
    lease,
    turn_id: str,
    *,
    text: str = "ack",
) -> str:
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    entry_id = _id("entry")
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=entry_id,
        parent_content=InlineContent.from_bytes(text.encode()),
        blocks=(
            AssistantTextBlock(
                _id("block"),
                InlineContent.from_bytes(text.encode()),
            ),
        ),
        complete_turn=True,
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    return entry_id


def _permission_fingerprint(
    repository: ConversationKernelRepository,
    lease,
    turn_id: str,
) -> str:
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            """SELECT permission_snapshot_fingerprint
               FROM pulsara_v3.turns WHERE session_id=%s AND id=%s""",
            (lease.guard.session_id, turn_id),
        ).fetchone()
    assert row is not None
    return str(row[0])


def _install_running_main_candidate(
    repository: ConversationKernelRepository,
    lease,
    *,
    user_text: str,
    statement: str,
):
    turn_id, _ = _start_human_turn(repository, lease, user_text)
    cut = repository.prepare_provider_input_cut(
        lease.guard,
        turn_id=turn_id,
        deadline_monotonic=monotonic() + 30,
    )
    assistant_entry_id = _id("entry")
    tool_call_id = _id("call")
    public_text = "I can retain this advisory detail."
    public_data = '{"status":"proposal prepared"}'
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=assistant_entry_id,
        parent_content=InlineContent.from_bytes((public_text + public_data).encode()),
        blocks=(
            AssistantTextBlock(
                _id("block"), InlineContent.from_bytes(public_text.encode())
            ),
            AssistantDataBlock(
                _id("block"), InlineContent.from_bytes(public_data.encode())
            ),
            AssistantToolCallBlock(
                _id("block"),
                tool_call_id,
                "remember",
                freeze_json({"statement": statement, "context_target": "GLOBAL"}),
            ),
        ),
        occurred_at=datetime.now(timezone.utc),
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    attempt = repository.accept_tool_attempt(
        lease.guard,
        attempt_id=_id("attempt"),
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        authorization_kind="policy",
        authorization_reference="allow",
        actor_kind="runtime",
        actor_id="tool:test",
        remote_idempotency_key=None,
        retry_of_attempt_id=None,
        permission_snapshot_fingerprint=_permission_fingerprint(
            repository, lease, turn_id
        ),
        occurred_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    memory_candidate = prepare_memory_candidate(
        candidate_id=_id("candidate"),
        memory_domain_id="u_local",
        origin_workspace_id=workspace_id,
        origin_session_id=lease.guard.session_id,
        producer_entry_id=assistant_entry_id,
        producer_tool_call_id=tool_call_id,
        proposal=FrozenMemoryProposal(
            statement=statement,
            context_id=CTX_GLOBAL,
            kind_hint=MemoryKindHint.FACT,
        ),
    )
    result = build_prepared_tool_result_acceptance(
        guard=lease.guard,
        workspace_id=workspace_id,
        result_id=_id("result"),
        result_entry_id=_id("entry"),
        turn_id=turn_id,
        assistant_entry_id=assistant_entry_id,
        tool_call_id=tool_call_id,
        attempt_id=attempt.attempt_id,
        result_state="SUCCESS",
        canonical_preview_content=InlineContent.from_bytes(b"submitted for review"),
        artifact_disposition=ToolOutputArtifactDisposition.NOT_REQUIRED,
        artifact_id=None,
        artifact_blob_descriptor=None,
        source_coverage=ToolOutputSourceCoverage.COMPLETE,
        display_kind=ToolResultDisplayKind.COMPLETE,
        source_coverage_reason=None,
        artifact_unavailability_reason=None,
        observed_at=datetime.now(timezone.utc),
        observation_duration_microseconds=None,
        observation_origin_kind=ToolObservationOrigin.BUILTIN,
        trusted_tool_reported_duration_microseconds=None,
        actor_id="remember",
        memory_candidate=memory_candidate,
    )
    repository.accept_tool_result(
        lease.guard,
        candidate=result,
        deadline_monotonic=monotonic() + 30,
    )
    return turn_id, memory_candidate


def _insert_exact_source_entry(
    repository: ConversationKernelRepository,
    lease,
    *,
    turn_id: str,
    entry_kind: str,
    body: str | FrozenPromptContent,
) -> tuple[str, str]:
    event_type = {
        "USER_STEER": "UserSteerAccepted",
        "TOOL_RESULT": "ToolResultAccepted",
        "TERMINAL_OBSERVATION": "TerminalObservationAccepted",
    }[entry_kind]
    entry_id = _id("entry")
    event_id = _id("event")
    if not isinstance(body, (str, FrozenPromptContent)):
        raise TypeError("test source body must be text or frozen prompt content")
    encoded = body.encode() if isinstance(body, str) else b""
    media_type = "text/plain"
    codec = "utf-8"
    canonical_prompt = None
    if entry_kind == "USER_STEER":
        content = FrozenPromptContent.text(body) if isinstance(body, str) else body
        canonical_prompt = freeze_canonical_prompt(content)
    elif not isinstance(body, str):
        raise TypeError("only human steer entries accept frozen prompt content")
    now = datetime.now(timezone.utc)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.BACKGROUND_WORK,
        row_factory=dict_row,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        session = connection.execute(
            """SELECT workspace_id, latest_entry_sequence, latest_event_sequence
               FROM pulsara_v3.sessions WHERE id=%s FOR UPDATE""",
            (lease.guard.session_id,),
        ).fetchone()
        assert session is not None
        image_blob_ids: tuple[str, ...] = ()
        if canonical_prompt is not None:
            publication = materialize_canonical_prompt(
                connection,
                publisher=CanonicalContentPublisher(repository.connection_provider),
                workspace_id=str(session["workspace_id"]),
                prompt=canonical_prompt,
            )
            assert isinstance(publication.body, InlineContent)
            encoded = publication.body.canonical_bytes
            media_type = PROMPT_BODY_MEDIA_TYPE
            codec = PROMPT_BODY_CODEC
            image_blob_ids = publication.image_blob_ids
        entry_sequence = int(session["latest_entry_sequence"]) + 1
        event_sequence = int(session["latest_event_sequence"]) + 1
        connection.execute(
            """INSERT INTO pulsara_v3.transcript_entries (
                   entry_owner_kind, id, session_id, workspace_id, turn_id, entry_sequence,
                   entry_kind, conversation_scope_kind, inline_content,
                   content_digest, content_size, content_media_type, content_codec
               ) VALUES ('EXECUTED_TURN', %s,%s,%s,%s,%s,%s,'ROOT',%s,%s,%s,%s,%s)""",
            (
                entry_id,
                lease.guard.session_id,
                str(session["workspace_id"]),
                turn_id,
                entry_sequence,
                entry_kind,
                encoded,
                "sha256:" + sha256(encoded).hexdigest(),
                len(encoded),
                media_type,
                codec,
            ),
        )
        if image_blob_ids:
            insert_canonical_prompt_refs(
                connection,
                session_id=lease.guard.session_id,
                workspace_id=str(session["workspace_id"]),
                image_blob_ids=image_blob_ids,
                transcript_entry_id=entry_id,
            )
        connection.execute(
            """INSERT INTO pulsara_v3.agent_events (
                   event_id, workspace_id, session_id, event_sequence, namespace,
                   event_type, schema_major, schema_minor, occurred_at, actor_kind,
                   actor_id, sensitivity_class, projection_profile, payload,
                   subject_entry_id
               ) VALUES (%s,%s,%s,%s,'pulsara.core',%s,1,0,%s,%s,%s,
                         'PUBLIC','DEFAULT',%s,%s)""",
            (
                event_id,
                str(session["workspace_id"]),
                lease.guard.session_id,
                event_sequence,
                event_type,
                now,
                "human" if entry_kind == "USER_STEER" else "runtime",
                "test",
                json.dumps(
                    {"source": "PROMPT_QUEUE"}
                    if entry_kind == "USER_STEER"
                    else {"test_source": True}
                ),
                entry_id,
            ),
        )
        connection.execute(
            """UPDATE pulsara_v3.sessions
               SET latest_entry_sequence=%s, latest_event_sequence=%s
               WHERE id=%s""",
            (entry_sequence, event_sequence, lease.guard.session_id),
        )
    return entry_id, event_id


class _ForbiddenGovernanceModel:
    calls = 0

    def prepare_json_call(self, **_kwargs: object):
        self.calls += 1
        raise AssertionError("source-incomplete governance opened provider planning")

    def prepare_first_fitting_json_call(self, **_kwargs: object):
        self.calls += 1
        raise AssertionError("source-incomplete governance opened provider planning")

    async def complete_prepared_json(self, _prepared, *, terminal_fence):
        assert terminal_fence is not None
        self.calls += 1
        raise AssertionError("source-incomplete governance opened provider transport")


def test_governance_v3_prompt_is_the_shared_complete_product_contract() -> None:
    assert MEMORY_GOVERNANCE_CONTRACT_ID.endswith(".v3")
    assert "governance.v2" not in MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3
    assert tuple(name for name, _ in MEMORY_KIND_PRODUCT_DEFINITIONS) == tuple(
        item.value for item in MemoryFactKind
    )
    for name, meaning in MEMORY_KIND_PRODUCT_DEFINITIONS:
        assert name in MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3
        assert meaning in DEFAULT_SYSTEM_PROMPT
    assert MEMORY_CONTEXT_PRODUCT_GUIDE in DEFAULT_SYSTEM_PROMPT
    assert MEMORY_COHESIVE_UNIT_GUIDE in DEFAULT_SYSTEM_PROMPT
    assert MEMORY_RETRIEVAL_AUTHORING_GUIDE in DEFAULT_SYSTEM_PROMPT
    for required in (
        "advisory dataset",
        "HUMAN_ASSERTION",
        "POST_PROPOSAL_HUMAN",
        "PRIMARY_OBSERVATION",
        "MEMORY_READ_EXPOSURE",
        "NON_HUMAN_CONTEXT",
        "BASED_ON",
        "ACCEPT_AND_SUPERSEDE",
        "TAXONOMY_CORRECTION",
        "ACCEPT_AND_CONTRADICT",
        "public_summary",
        "target-independent",
        "Never narrate why you selected ACCEPT",
    ):
        assert required in MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3
    for reason in MODEL_GOVERNANCE_SKIP_REASON_CODES:
        assert reason.value in MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3
    for required in (
        "ACCEPT is the default",
        "not a second retention-value approval",
        "lockfiles",
        "shadow fact",
        "What and Who/subject",
        "does not execute, track, or guarantee completion",
        "date verification",
        "not answer presentation",
        "future-facing human instruction",
        "Mere association with",
    ):
        assert required in MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3


def test_remember_descriptor_and_root_prompt_share_taxonomy() -> None:
    remember = next(
        item for item in builtin_tool_catalog() if item.descriptor.name == "remember"
    )
    descriptor = remember.descriptor.description
    schema_text = repr(remember.descriptor.input_schema)
    for kind, _meaning in MEMORY_KIND_PRODUCT_DEFINITIONS:
        assert kind in descriptor or kind in schema_text
        assert kind in DEFAULT_SYSTEM_PROMPT
        assert kind in MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3


def test_legal_final_kinds_reuse_the_closed_shape_validator() -> None:
    ordinary = legal_memory_final_kinds(_proposal())
    assert ordinary == (
        MemoryFactKind.USER_PROFILE,
        MemoryFactKind.RESPONSE_PREFERENCE,
        MemoryFactKind.FACT,
        MemoryFactKind.DECISION,
    )
    mis_hint = legal_memory_final_kinds(
        _proposal(kind_hint=MemoryKindHint.USER_PROFILE)
    )
    assert mis_hint == ordinary
    decision = legal_memory_final_kinds(
        _proposal(kind_hint=MemoryKindHint.DECISION, basis=("memory:basis",))
    )
    assert decision == ordinary


@pytest.mark.parametrize("reason", sorted(MODEL_GOVERNANCE_SKIP_REASON_CODES))
def test_parser_accepts_each_model_skip_reason(
    reason: MemoryDecisionReasonCode,
) -> None:
    decision = _parse_governance_decision(
        {"decision": "SKIP", "reason_code": reason.value},
        {},
        legal_final_kinds=legal_memory_final_kinds(_proposal()),
    )
    assert decision.reason_code == reason.value


@pytest.mark.parametrize(
    "reason",
    sorted(set(MemoryDecisionReasonCode) - MODEL_GOVERNANCE_SKIP_REASON_CODES),
)
def test_parser_rejects_host_only_skip_reasons(
    reason: MemoryDecisionReasonCode,
) -> None:
    with pytest.raises(ValueError, match="reason is invalid"):
        _parse_governance_decision(
            {"decision": "SKIP", "reason_code": reason.value},
            {},
            legal_final_kinds=legal_memory_final_kinds(_proposal()),
        )


@pytest.mark.parametrize(
    "value",
    (
        {"decision": "ACCEPT", "final_kind": "RESPONSE_PREFERENCE"},
        {
            "decision": "ACCEPT",
            "final_kind": "RESPONSE_PREFERENCE",
            "public_summary": "",
        },
        {
            "decision": "ACCEPT",
            "final_kind": "ACTION_RULE",
            "public_summary": "Based on your enduring answer preference.",
        },
        {
            "decision": "ACCEPT",
            "final_kind": "RESPONSE_PREFERENCE",
            "public_summary": "Based on your enduring answer preference.",
            "statement": "rewritten",
        },
    ),
)
def test_parser_rejects_missing_summary_illegal_kind_and_extra_semantics(
    value: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        _parse_governance_decision(
            value,
            {},
            legal_final_kinds=legal_memory_final_kinds(_proposal()),
        )


@pytest.mark.parametrize(
    "summary",
    (
        "Updated the old target.",
        "This conflicts with the previous memory.",
        "Verified and permanently saved.",
        "根据 HUMAN_ASSERTION 和 target_fact_id 整理。",
        "根据内部 prompt 整理。",
        "根据来源更新了旧记忆。",
        "Based on memory:0123456789.",
        "Based on a fact enum selected by governance.",
        "Based on the DECISION enum.",
        "根据 source:1 整理。",
        "根据 CURRENT_PROJECT 内部目标整理。",
    ),
)
def test_public_summary_rejects_relation_internal_and_certification_language(
    summary: str,
) -> None:
    with pytest.raises(ValueError, match="public summary"):
        _parse_governance_decision(
            {
                "decision": "ACCEPT",
                "final_kind": "RESPONSE_PREFERENCE",
                "public_summary": summary,
            },
            {},
            legal_final_kinds=legal_memory_final_kinds(_proposal()),
        )


def test_public_summary_allows_product_names_that_contain_sql() -> None:
    parsed = _parse_governance_decision(
        {
            "decision": "ACCEPT",
            "final_kind": "DECISION",
            "public_summary": "The user chose PostgreSQL for Apollo.",
        },
        {},
        legal_final_kinds=legal_memory_final_kinds(_proposal()),
    )
    assert parsed.public_summary == "The user chose PostgreSQL for Apollo."


def test_public_summary_allows_natural_lowercase_kind_words() -> None:
    parsed = _parse_governance_decision(
        {
            "decision": "ACCEPT",
            "final_kind": "DECISION",
            "public_summary": "Based on the user's stated decision for this year.",
        },
        {},
        legal_final_kinds=legal_memory_final_kinds(_proposal()),
    )
    assert parsed.public_summary == (
        "Based on the user's stated decision for this year."
    )


def test_relation_parser_requires_exact_allowlist_and_target_independent_summary() -> (
    None
):
    target = _target()
    output = {
        "decision": "ACCEPT_AND_SUPERSEDE",
        "final_kind": "RESPONSE_PREFERENCE",
        "target_fact_id": target.fact_id,
        "supersede_mode": "SAME_KIND_REPLACEMENT",
        "public_summary": "Based on your explicit enduring answer preference.",
    }
    with pytest.raises(ValueError, match="outside the frozen allowlist"):
        _parse_governance_decision(
            output,
            {},
            legal_final_kinds=legal_memory_final_kinds(_proposal()),
        )
    accepted = _parse_governance_decision(
        output,
        {target.fact_id: target},
        legal_final_kinds=legal_memory_final_kinds(_proposal()),
    )
    assert accepted.public_summary == output["public_summary"]


def test_only_the_relation_target_selected_by_the_model_reaches_settlement() -> None:
    target = _target()
    plain = _parse_governance_decision(
        {
            "decision": "ACCEPT",
            "final_kind": "RESPONSE_PREFERENCE",
            "public_summary": "Based on your explicit enduring answer preference.",
        },
        {target.fact_id: target},
        legal_final_kinds=legal_memory_final_kinds(_proposal()),
    )
    assert _selected_governance_relation_targets(plain, {target.fact_id: target}) == ()

    related = _parse_governance_decision(
        {
            "decision": "ACCEPT_AND_SUPERSEDE",
            "final_kind": "RESPONSE_PREFERENCE",
            "target_fact_id": target.fact_id,
            "supersede_mode": "SAME_KIND_REPLACEMENT",
            "public_summary": "Based on your explicit enduring answer preference.",
        },
        {target.fact_id: target},
        legal_final_kinds=legal_memory_final_kinds(_proposal()),
    )
    assert _selected_governance_relation_targets(related, {target.fact_id: target}) == (
        target,
    )


def test_optional_skip_summary_uses_the_same_public_product_validator() -> None:
    with pytest.raises(ValueError, match="public summary"):
        _parse_governance_decision(
            {
                "decision": "SKIP",
                "reason_code": "LOW_VALUE",
                "public_summary": "The provider rejected memory:internal.",
            },
            {},
            legal_final_kinds=legal_memory_final_kinds(_proposal()),
        )


@pytest.mark.parametrize("api", ("openai_chat_completions", "openai_responses"))
def test_auxiliary_governance_uses_exact_system_user_shape_and_final_wire(
    api: str,
) -> None:
    auxiliary = _auxiliary(api)
    prepared = auxiliary.prepare_json_call(
        purpose=ModelCallPurpose.MEMORY_GOVERNANCE,
        messages=(
            LLMMessage.system(MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3),
            LLMMessage.user('{"contract":"pulsara.advisory-memory-governance.v3"}'),
        ),
        maximum_input_tokens=32_768,
        maximum_input_bytes=128 * 1024,
        maximum_output_tokens=2_048,
        timeout_policy=_timeout(),
        origin_binding=_origin_binding(),
        maximum_result_bytes=8 * 1024,
    )
    assert prepared.context.system_prompt == MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3
    assert len(prepared.context.messages) == 1
    assert prepared.context.messages[0].role is MessageRole.USER
    assert prepared.context.tools == ()
    fixed, final, ordered, sources = _materialize_auxiliary_final_wire(
        call=prepared.call,
        context=prepared.context,
    )
    assert prepared.final_wire_utf8_bytes == len(canonical_json_bytes(final))
    assert prepared.estimated_input_tokens == (
        prepared.call.target.token_estimator.estimate_final_wire_json_components(
            fixed_context=fixed,
            ordered_input_items=ordered,
            ordered_input_sources=sources,
        ).total_input_tokens
    )


def test_governance_output_budget_leaves_room_for_responses_reasoning() -> None:
    assert MAXIMUM_GOVERNANCE_OUTPUT_TOKENS == 8_192


def test_auxiliary_governance_rejects_user_only_or_noncanonical_system() -> None:
    auxiliary = _auxiliary("openai_responses")
    common = {
        "purpose": ModelCallPurpose.MEMORY_GOVERNANCE,
        "maximum_input_tokens": 32_768,
        "maximum_input_bytes": 128 * 1024,
        "maximum_output_tokens": 2_048,
        "timeout_policy": _timeout(),
        "origin_binding": _origin_binding(),
    }
    with pytest.raises(ValueError, match="exact stable SYSTEM"):
        auxiliary.prepare_json_call(
            messages=(LLMMessage.user("{}"),),
            **common,
        )
    with pytest.raises(ValueError, match="exact stable SYSTEM"):
        auxiliary.prepare_json_call(
            messages=(LLMMessage.system("different"), LLMMessage.user("{}")),
            **common,
        )


def test_auxiliary_variant_admission_selects_first_exact_final_wire_fit() -> None:
    auxiliary = _auxiliary("openai_responses")
    small_messages = (
        LLMMessage.system(MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3),
        LLMMessage.user('{"candidate":"small"}'),
    )
    small = auxiliary.prepare_json_call(
        purpose=ModelCallPurpose.MEMORY_GOVERNANCE,
        messages=small_messages,
        maximum_input_tokens=32_768,
        maximum_input_bytes=256 * 1024,
        maximum_output_tokens=2_048,
        timeout_policy=_timeout(),
        origin_binding=_origin_binding(),
    )
    selected = auxiliary.prepare_first_fitting_json_call(
        purpose=ModelCallPurpose.MEMORY_GOVERNANCE,
        message_variants=(
            (
                LLMMessage.system(MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3),
                LLMMessage.user(json.dumps({"body": "x" * 20_000})),
            ),
            small_messages,
        ),
        maximum_input_tokens=32_768,
        maximum_input_bytes=small.final_wire_utf8_bytes,
        maximum_output_tokens=2_048,
        timeout_policy=_timeout(),
        origin_binding=_origin_binding(),
    )
    assert selected is not None
    prepared, ordinal = selected
    assert ordinal == 1
    assert prepared.final_wire_utf8_bytes == small.final_wire_utf8_bytes


def test_packet_shedding_preserves_human_anchors_and_orders_optional_removal() -> None:
    optional = FrozenMemoryGovernanceSourceItem(
        source_entry_id="entry:old-context",
        chronology=MemoryGovernanceChronology.BEFORE_PROPOSAL,
        source_product_label="主模型当时看到的助手上下文",
        evidence_role=MemoryGovernanceEvidenceRole.ASSISTANT_CONTEXT,
        public_kind="助手回复",
        blocks=(
            FrozenMemoryGovernanceSourceBlock(
                MemoryGovernanceSourceBlockKind.TEXT,
                "old optional context",
            ),
        ),
    )
    anchor = FrozenMemoryGovernanceSourceItem(
        source_entry_id="entry:human",
        chronology=MemoryGovernanceChronology.AFTER_PROPOSAL,
        source_product_label="候选提出后的用户原话",
        evidence_role=MemoryGovernanceEvidenceRole.POST_PROPOSAL_HUMAN,
        public_kind="用户补充",
        blocks=(
            FrozenMemoryGovernanceSourceBlock(
                MemoryGovernanceSourceBlockKind.TEXT,
                "这不是一次性要求。",
            ),
        ),
        anchor=True,
    )
    target = _target()
    base = {
        "contract": MEMORY_GOVERNANCE_CONTRACT_ID,
        "candidate": {"statement": "Use concise release notes"},
        "source_coverage": {
            "causal_context_complete": True,
            "origin_turn_human_source_complete": True,
            "post_proposal_human_source_complete": True,
            "omitted_causal_items": 0,
            "omitted_assistant_or_tool_items": 0,
            "omitted_post_proposal_human_items": 0,
            "relation_authority": True,
        },
        "all_model_visible_memory": ({"memory_id": "memory:visible"},),
    }
    variants = _governance_packet_variants(
        base=base,
        source_items=(optional, anchor),
        citation_items=(
            {
                "source": "cited-observation:1",
                "evidence_role": "PRIMARY_OBSERVATION",
                "result_state": "SUCCESS",
                "body": "large cited body",
                "truncated": False,
            },
        ),
        related_items=({"memory_id": target.fact_id},),
        targets={target.fact_id: target},
        legal_final_kinds=("FACT", "DECISION"),
    )
    payloads = tuple(json.loads(item.packet) for item in variants)
    assert payloads[0]["allowed_relation_targets"] == [{"memory_id": target.fact_id}]
    assert payloads[1]["allowed_relation_targets"] == []
    assert payloads[1]["source_coverage"]["relation_authority"] is False
    assert payloads[0]["output_schema"]["field_constraints"]["decision"][
        "allowed_values"
    ] == [
        "SKIP",
        "ACCEPT",
        "ACCEPT_AND_SUPERSEDE",
        "ACCEPT_AND_CONTRADICT",
    ]
    assert payloads[1]["output_schema"]["field_constraints"]["decision"][
        "allowed_values"
    ] == ["SKIP", "ACCEPT"]
    assert len(payloads[1]["ordered_source_items"]) == 2
    assert len(payloads[2]["ordered_source_items"]) == 1
    assert payloads[2]["ordered_source_items"][0]["source_anchor"] is True
    assert payloads[-1]["ordered_source_items"][0]["blocks"][0]["text"] == (
        "这不是一次性要求。"
    )
    assert payloads[-1]["cited_tool_evidence"][0]["body"] == ""
    assert payloads[-1]["cited_tool_evidence"][0]["truncated"] is True
    assert payloads[-1]["all_model_visible_memory"] == [{"memory_id": "memory:visible"}]


@pytest.mark.parametrize("source_gap", ("truncated_producer", "omitted_suffix"))
def test_incomplete_nonhuman_source_cannot_authorize_relations(
    source_gap: str,
) -> None:
    historical_item = FrozenProviderInputItem(
        FrozenProviderInputItemKind.USER,
        "entry:user",
        0,
        "turn:test",
        (LLMTextPart("The user supplied the source context."),),
        input_origin=CanonicalInputOriginKind.HUMAN_MESSAGE,
    )
    identity = CanonicalModelInputIdentity(
        session_id="session:test",
        turn_id="turn:test",
        initial_entry_id="entry:user",
        context_binding_revision_id="revision:test",
        provider_input_through_sequence=0,
        conversation_scope_kind=ModelInputScopeKind.ROOT,
        scope_subagent_task_id=None,
        identity_fingerprint=canonical_model_input_identity_fingerprint(
            session_id="session:test",
            turn_id="turn:test",
            initial_entry_id="entry:user",
            context_binding_revision_id="revision:test",
            provider_input_through_sequence=0,
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        ),
    )
    historical = CanonicalModelInputSnapshot(
        identity=identity,
        items=(historical_item,),
        canonical_expanded_bytes=len(
            provider_input_item_text(historical_item).encode("utf-8")
        ),
        snapshot_fingerprint=canonical_model_input_snapshot_fingerprint(
            identity=identity,
            items=(historical_item,),
            canonical_expanded_bytes=len(
                provider_input_item_text(historical_item).encode("utf-8")
            ),
            closures=(),
            late_outcomes=(),
        ),
    )
    producer = FrozenMemoryGovernanceSourceItem(
        source_entry_id="entry:producer",
        chronology=MemoryGovernanceChronology.PRODUCER_OUTPUT,
        source_product_label="助手回复上下文",
        evidence_role=MemoryGovernanceEvidenceRole.ASSISTANT_CONTEXT,
        public_kind="助手回复",
        blocks=(
            FrozenMemoryGovernanceSourceBlock(
                MemoryGovernanceSourceBlockKind.TEXT,
                "" if source_gap == "truncated_producer" else "complete context",
                truncated=source_gap == "truncated_producer",
            ),
        ),
        item_omitted_after=int(source_gap == "truncated_producer"),
    )
    evidence = FrozenMemoryGovernanceEvidence(
        origin_workspace_id="workspace:test",
        terminal_fence=FrozenMemoryGovernanceTerminalFence(
            source_turn_id="turn:test",
            source_entry_id="entry:producer",
            source_entry_event_id="event:producer",
            source_entry_event_sequence=2,
            terminal_status="COMPLETED",
            terminal_outcome="entry:final",
            terminal_event_id="event:terminal",
            terminal_event_sequence=3,
        ),
        producer_cut=FrozenMemoryGovernanceProducerCut(
            session_id="session:test",
            turn_id="turn:test",
            producer_entry_id="entry:producer",
            producer_entry_sequence=1,
            context_binding_revision_id="revision:test",
            provider_input_through_sequence=0,
        ),
        producer_call_context=(),
        producer_public_output=(producer,),
        post_proposal_turn_suffix=(),
        tool_result_evidence=(),
        basis_items=(),
        model_visible_items=(),
        model_visible_complete=True,
        source_coverage=FrozenMemoryGovernanceSourceCoverage(
            causal_context_complete=False,
            origin_turn_human_source_complete=True,
            post_proposal_human_source_complete=True,
            omitted_assistant_or_tool_items=int(source_gap == "omitted_suffix"),
            relation_authority=False,
        ),
    )
    finalized = _finalize_governance_source_envelope(
        evidence,
        historical=historical,
    )
    assert finalized.source_coverage.omitted_assistant_or_tool_items > 0
    assert not finalized.source_coverage.relation_authority

    candidate = prepare_memory_candidate(
        candidate_id="candidate:source-gap",
        memory_domain_id="u_local",
        origin_workspace_id=evidence.origin_workspace_id,
        origin_session_id="session:test",
        producer_entry_id="entry:producer",
        producer_tool_call_id="call:remember",
        proposal=_proposal(),
    )

    class _EmptyQuery:
        @staticmethod
        def find_active_semantic(**_kwargs: object):
            return None

    class _ImmediateIO:
        @staticmethod
        async def run(operation, *args: object, **kwargs: object):
            return operation(*args, **kwargs)

    class _PacketOwner:
        _query = _EmptyQuery()
        _io = _ImmediateIO()
        _embedding_port = None
        _read_binding = object()

    async def materialize_packet():
        return await AdvisoryMemoryGovernor._governance_packet(  # noqa: SLF001
            _PacketOwner(),
            candidate,
            evidence=finalized,
            deadline_monotonic=monotonic() + 30,
        )

    variants = asyncio.run(materialize_packet())
    assert all(not item.allowed_targets for item in variants)
    schema = json.loads(variants[0].packet)["output_schema"]
    assert schema["field_constraints"]["decision"]["allowed_values"] == [
        "SKIP",
        "ACCEPT",
    ]
    with pytest.raises(ValueError, match="outside the frozen allowlist"):
        _parse_governance_decision(
            {
                "decision": "ACCEPT_AND_SUPERSEDE",
                "final_kind": "RESPONSE_PREFERENCE",
                "target_fact_id": "memory:target",
                "supersede_mode": "SAME_KIND_REPLACEMENT",
                "public_summary": "用户明确表达了这项回答偏好。",
            },
            {},
            legal_final_kinds=legal_memory_final_kinds(candidate.proposal),
        )


def test_output_schema_is_a_flat_constraint_not_an_example_object() -> None:
    schema = _governance_output_schema(
        legal_final_kinds=("FACT", "DECISION"),
        allowed_target_ids=("memory:target",),
    )
    assert "never mention relation selection" in str(
        schema["field_constraints"]["public_summary"]["contract"]
    )
    fields = schema["field_constraints"]
    assert fields["final_kind"] == {
        "type": "string",
        "allowed_values": ("FACT", "DECISION"),
    }
    assert fields["target_fact_id"]["allowed_values"] == ("memory:target",)
    assert "accept" not in schema
    assert "skip" not in schema
    assert "one flat top-level object" in schema["shape"]
    assert "one flat JSON object" in MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3
    assert (
        "Formal replacement wording is not required"
        in MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3
    )
    assert (
        "keeps both exact same-context endpoints ACTIVE"
        in MEMORY_GOVERNANCE_SYSTEM_PROMPT_V3
    )


def test_no_v1_builder_provider_token_counter_or_provider_business_branch_exists() -> (
    None
):
    root = __import__("pathlib").Path(__file__).resolve().parents[1] / "src"
    production = "\n".join(path.read_text() for path in root.rglob("*.py"))
    assert "advisory-memory-governance.v1" not in production
    assert "_MAXIMUM_GOVERNANCE_TURN_ITEMS" not in production
    assert "offer_candidate_wake" not in production
    assert "provider_token_count" not in production
    governor = (
        root / "pulsara_agent" / "conversation_kernel" / "memory" / "governor.py"
    ).read_text()
    assert "config.provider" not in governor
    assert "config.api" not in governor
    tool_execution = (
        root / "pulsara_agent" / "conversation_kernel" / "tool_execution.py"
    ).read_text()
    runner = (root / "pulsara_agent" / "conversation_kernel" / "runner.py").read_text()
    assert "offer_governance_wake" not in tool_execution
    assert "offer_governance_wake" in runner


@pytest.mark.parametrize(
    ("item_kind", "origin", "public_kind"),
    (
        (
            FrozenProviderInputItemKind.USER,
            CanonicalInputOriginKind.PLAN_CONTINUATION,
            "Planning continuation",
        ),
        (
            FrozenProviderInputItemKind.PLAN_CONTINUATION,
            CanonicalInputOriginKind.PLAN_CONTINUATION,
            "Planning continuation",
        ),
        (
            FrozenProviderInputItemKind.USER,
            CanonicalInputOriginKind.SUBAGENT_OBJECTIVE,
            "Delegated task objective",
        ),
    ),
)
def test_canonical_plan_and_runtime_user_shapes_never_become_human_evidence(
    item_kind: FrozenProviderInputItemKind,
    origin: CanonicalInputOriginKind,
    public_kind: str,
) -> None:
    item = FrozenProviderInputItem(
        item_kind=item_kind,
        source_entry_id=_id("entry"),
        source_entry_sequence=1,
        source_turn_id=_id("turn"),
        content=(LLMTextPart("Use zsh from now on"),),
        input_origin=origin,
    )
    projected = _causal_source_item(item)
    assert projected is not None
    assert projected.evidence_role is (MemoryGovernanceEvidenceRole.NON_HUMAN_CONTEXT)
    assert "User input" not in projected.source_product_label
    assert projected.public_kind == public_kind


@pytest.mark.postgres
def test_terminal_claim_waits_for_terminal_occurrence_and_freezes_exact_fence(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    turn_id, candidate = _install_running_main_candidate(
        repository,
        lease,
        user_text="Remember my editor",
        statement="The user uses a compact editor layout",
    )
    assert (
        repository.claim_memory_candidate_for_governance(
            lease.guard,
            candidate_id=candidate.candidate_id,
            processing_started_at=datetime.now(timezone.utc),
            deadline_monotonic=monotonic() + 30,
        )
        is None
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.memory_candidates WHERE id=%s",
            (candidate.candidate_id,),
        ).fetchone() == ("PENDING",)

    final_entry_id = _complete_turn(repository, lease, turn_id)
    claimed = repository.claim_memory_candidate_for_governance(
        lease.guard,
        candidate_id=candidate.candidate_id,
        processing_started_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    assert claimed is not None
    assert claimed.terminal_fence.terminal_status == "COMPLETED"
    assert claimed.terminal_fence.terminal_outcome == final_entry_id
    assert claimed.terminal_fence.source_entry_id == candidate.producer_entry_id
    assert (
        claimed.terminal_fence.source_entry_event_sequence
        < claimed.terminal_fence.terminal_event_sequence
    )
    assert repository.confirm_memory_governance_terminal_fence(
        lease.guard,
        candidate=claimed,
        deadline_monotonic=monotonic() + 30,
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.BACKGROUND_WORK,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        session = connection.execute(
            """SELECT workspace_id, latest_event_sequence
               FROM pulsara_v3.sessions WHERE id=%s FOR UPDATE""",
            (lease.guard.session_id,),
        ).fetchone()
        assert session is not None
        duplicate_sequence = int(session[1]) + 1
        connection.execute(
            """INSERT INTO pulsara_v3.agent_events (
                   event_id, workspace_id, session_id, event_sequence,
                   namespace, event_type, schema_major, schema_minor,
                   occurred_at, actor_kind, actor_id, sensitivity_class,
                   projection_profile, payload, subject_turn_id
               ) SELECT %s, workspace_id, session_id, %s, namespace, event_type,
                        schema_major, schema_minor, occurred_at, actor_kind, actor_id,
                        sensitivity_class, projection_profile, payload, subject_turn_id
               FROM pulsara_v3.agent_events WHERE event_id=%s""",
            (
                _id("event"),
                duplicate_sequence,
                claimed.terminal_fence.terminal_event_id,
            ),
        )
        connection.execute(
            """UPDATE pulsara_v3.sessions SET latest_event_sequence=%s WHERE id=%s""",
            (duplicate_sequence, lease.guard.session_id),
        )
    assert not repository.confirm_memory_governance_terminal_fence(
        lease.guard,
        candidate=claimed,
        deadline_monotonic=monotonic() + 30,
    )


@pytest.mark.postgres
def test_earlier_running_candidate_does_not_block_later_terminal_candidate(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)

    healthy_turn, healthy = _install_running_main_candidate(
        repository,
        lease,
        user_text="Remember the project database",
        statement="The project uses PostgreSQL",
    )
    _complete_turn(repository, lease, healthy_turn)

    _running_turn, running = _install_running_main_candidate(
        repository,
        lease,
        user_text="Remember a still-running thought",
        statement="This candidate is not terminal yet",
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.BACKGROUND_WORK,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        connection.execute(
            """UPDATE pulsara_v3.memory_candidates
               SET accepted_at=accepted_at - interval '1 hour' WHERE id=%s""",
            (running.candidate_id,),
        )

    claimed = repository.claim_memory_candidate_for_governance(
        lease.guard,
        processing_started_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    assert claimed is not None
    assert claimed.prepared.candidate_id == healthy.candidate_id
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT status FROM pulsara_v3.memory_candidates WHERE id=%s",
            (running.candidate_id,),
        ).fetchone() == ("PENDING",)


@pytest.mark.postgres
def test_candidate_digest_drift_fails_before_evidence_or_provider_open_without_starvation(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)

    corrupt_turn, corrupt = _install_running_main_candidate(
        repository,
        lease,
        user_text="Remember the first setting",
        statement="The first setting is enabled",
    )
    _complete_turn(repository, lease, corrupt_turn)
    healthy_turn, healthy = _install_running_main_candidate(
        repository,
        lease,
        user_text="Remember the healthy setting",
        statement="The healthy setting is enabled",
    )
    _complete_turn(repository, lease, healthy_turn)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.BACKGROUND_WORK,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        connection.execute(
            """UPDATE pulsara_v3.memory_candidates
               SET statement='mutated without acceptance digest',
                   accepted_at=accepted_at - interval '1 hour'
               WHERE id=%s""",
            (corrupt.candidate_id,),
        )

    claimed = repository.claim_memory_candidate_for_governance(
        lease.guard,
        processing_started_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    assert claimed is not None
    assert claimed.prepared.candidate_id == healthy.candidate_id
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            """SELECT status, decision_reason_code
               FROM pulsara_v3.memory_candidates WHERE id=%s""",
            (corrupt.candidate_id,),
        ).fetchone() == ("ABANDONED", "ABANDONED_REFERENCE_DRIFT")

    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.BACKGROUND_WORK,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        connection.execute(
            """UPDATE pulsara_v3.memory_candidates
               SET statement='mutated after claim without acceptance digest'
               WHERE id=%s""",
            (healthy.candidate_id,),
        )
    with pytest.raises(
        ConversationKernelConflict,
        match="acceptance digest does not match",
    ):
        repository.read_memory_governance_evidence(
            lease.guard,
            candidate=claimed,
            deadline_monotonic=monotonic() + 30,
        )
    assert not repository.confirm_memory_governance_terminal_fence(
        lease.guard,
        candidate=claimed,
        deadline_monotonic=monotonic() + 30,
    )


@pytest.mark.postgres
def test_interrupted_origin_is_claimable_with_product_terminal_outcome(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    turn_id, candidate = _install_running_main_candidate(
        repository,
        lease,
        user_text="Remember this cautiously",
        statement="The user reported a tentative setting",
    )
    repository.interrupt_turn(
        lease.guard,
        turn_id=turn_id,
        reason="USER_INTERRUPTED",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=monotonic() + 30,
    )
    claimed = repository.claim_memory_candidate_for_governance(
        lease.guard,
        candidate_id=candidate.candidate_id,
        processing_started_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    assert claimed is not None
    assert claimed.terminal_fence.terminal_status == "INTERRUPTED"
    assert claimed.terminal_fence.terminal_outcome == "USER_INTERRUPTED"


@pytest.mark.postgres
@pytest.mark.parametrize("corruption", ("missing", "duplicate", "mismatched"))
def test_terminal_occurrence_corruption_is_ineligible_and_does_not_block_healthy_work(
    stage2_migrated_postgres_database,
    corruption: str,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    turn_id, corrupt = _install_running_main_candidate(
        repository,
        lease,
        user_text="Remember corrupt fence",
        statement="This source fence will be corrupted",
    )
    _complete_turn(repository, lease, turn_id)
    with psycopg.connect(
        stage2_migrated_postgres_database.admin_dsn,
        autocommit=True,
    ) as connection:
        if corruption == "missing":
            connection.execute(
                """DELETE FROM pulsara_v3.agent_events
                   WHERE session_id=%s AND subject_turn_id=%s
                     AND event_type='TurnCompleted'""",
                (lease.guard.session_id, turn_id),
            )
        elif corruption == "mismatched":
            connection.execute(
                """UPDATE pulsara_v3.agent_events
                   SET payload=jsonb_build_object('final_entry_id','entry:wrong')
                   WHERE session_id=%s AND subject_turn_id=%s
                     AND event_type='TurnCompleted'""",
                (lease.guard.session_id, turn_id),
            )
        else:
            row = connection.execute(
                """SELECT workspace_id, max(event_sequence)
                   FROM pulsara_v3.agent_events WHERE session_id=%s
                   GROUP BY workspace_id""",
                (lease.guard.session_id,),
            ).fetchone()
            assert row is not None
            next_sequence = int(row[1]) + 1
            connection.execute(
                """INSERT INTO pulsara_v3.agent_events (
                       event_id, workspace_id, session_id, event_sequence,
                       namespace, event_type, schema_major, schema_minor,
                       occurred_at, actor_kind, actor_id, sensitivity_class,
                       projection_profile, payload, subject_turn_id
                   ) SELECT %s, workspace_id, session_id, %s, namespace, event_type,
                            schema_major, schema_minor, occurred_at, actor_kind, actor_id,
                            sensitivity_class, projection_profile, payload, subject_turn_id
                   FROM pulsara_v3.agent_events
                   WHERE session_id=%s AND subject_turn_id=%s
                     AND event_type='TurnCompleted'""",
                (_id("event"), next_sequence, lease.guard.session_id, turn_id),
            )
            connection.execute(
                """UPDATE pulsara_v3.sessions SET latest_event_sequence=%s
                   WHERE id=%s""",
                (next_sequence, lease.guard.session_id),
            )

    assert (
        repository.claim_memory_candidate_for_governance(
            lease.guard,
            candidate_id=corrupt.candidate_id,
            processing_started_at=datetime.now(timezone.utc),
            deadline_monotonic=monotonic() + 30,
        )
        is None
    )
    healthy_turn, healthy = _install_running_main_candidate(
        repository,
        lease,
        user_text="Remember healthy fence",
        statement="This source fence is healthy",
    )
    _complete_turn(repository, lease, healthy_turn)
    claimed = repository.claim_memory_candidate_for_governance(
        lease.guard,
        processing_started_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    assert claimed is not None
    assert claimed.prepared.candidate_id == healthy.candidate_id


@pytest.mark.postgres
def test_main_candidate_source_is_exact_block_preserving_exhaustive_and_terminal_bounded(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    turn_id, candidate = _install_running_main_candidate(
        repository,
        lease,
        user_text="The project may use PostgreSQL.",
        statement="The project always uses PostgreSQL",
    )
    for ordinal in range(35):
        _insert_exact_source_entry(
            repository,
            lease,
            turn_id=turn_id,
            entry_kind="TERMINAL_OBSERVATION",
            body=f"runtime observation {ordinal}",
        )
    correction = "Actually it only sometimes uses https://db.example.test/docs"
    correction_entry_id, _ = _insert_exact_source_entry(
        repository,
        lease,
        turn_id=turn_id,
        entry_kind="USER_STEER",
        body=correction,
    )
    repository.interrupt_turn(
        lease.guard,
        turn_id=turn_id,
        reason="USER_CORRECTED_DURING_TURN",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=monotonic() + 30,
    )
    claimed = repository.claim_memory_candidate_for_governance(
        lease.guard,
        candidate_id=candidate.candidate_id,
        processing_started_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    assert claimed is not None
    evidence = repository.read_memory_governance_evidence(
        lease.guard,
        candidate=claimed,
        deadline_monotonic=monotonic() + 30,
    )
    assert evidence.producer_cut is not None
    assert evidence.producer_cut.provider_input_through_sequence == 1
    assert tuple(
        block.block_kind for block in evidence.producer_public_output[0].blocks
    ) == (
        MemoryGovernanceSourceBlockKind.TEXT,
        MemoryGovernanceSourceBlockKind.DATA,
    )
    assert len(evidence.post_proposal_turn_suffix) > 32
    correction_item = next(
        item
        for item in evidence.post_proposal_turn_suffix
        if item.source_entry_id == correction_entry_id
    )
    assert correction_item.evidence_role is (
        MemoryGovernanceEvidenceRole.POST_PROPOSAL_HUMAN
    )
    assert correction_item.blocks[0].text == correction
    assert "https://db.example.test/docs" in correction_item.blocks[0].text
    assert evidence.source_coverage.post_proposal_human_source_complete

    before = (
        evidence.post_proposal_turn_suffix,
        evidence.source_coverage,
    )
    late_entry_id, _ = _insert_exact_source_entry(
        repository,
        lease,
        turn_id=turn_id,
        entry_kind="TOOL_RESULT",
        body="late result after terminal",
    )
    reread = repository.read_memory_governance_evidence(
        lease.guard,
        candidate=claimed,
        deadline_monotonic=monotonic() + 30,
    )
    assert all(
        item.source_entry_id != late_entry_id
        for item in reread.post_proposal_turn_suffix
    )
    assert (reread.post_proposal_turn_suffix, reread.source_coverage) == before


@pytest.mark.postgres
def test_memory_governance_projects_canonical_human_text_and_omits_image_payload(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    turn_id, candidate = _install_running_main_candidate(
        repository,
        lease,
        user_text="Remember a visual preference",
        statement="The user prefers compact diagrams",
    )
    image = LLMImagePart("image/png", b"validated-image", 1, 1)
    steer_entry_id, _ = _insert_exact_source_entry(
        repository,
        lease,
        turn_id=turn_id,
        entry_kind="USER_STEER",
        body=FrozenPromptContent(
            (LLMTextPart("use this"), image, LLMTextPart("as the example"))
        ),
    )
    repository.interrupt_turn(
        lease.guard,
        turn_id=turn_id,
        reason="USER_CORRECTED_DURING_TURN",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=monotonic() + 30,
    )
    claimed = repository.claim_memory_candidate_for_governance(
        lease.guard,
        candidate_id=candidate.candidate_id,
        processing_started_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    assert claimed is not None

    original_read = PostgresCanonicalBlobStore.read_exact_in_connection

    def reject_image_payload(*args, **kwargs):
        if kwargs.get("expected_media_type") == "image/png":
            raise AssertionError("memory governance must not read image payload bytes")
        return original_read(*args, **kwargs)

    monkeypatch.setattr(
        PostgresCanonicalBlobStore,
        "read_exact_in_connection",
        reject_image_payload,
    )
    evidence = repository.read_memory_governance_evidence(
        lease.guard,
        candidate=claimed,
        deadline_monotonic=monotonic() + 30,
    )

    marker = next(
        item
        for item in evidence.post_proposal_turn_suffix
        if item.source_entry_id == steer_entry_id
    )
    assert marker.blocks[0].text == ""
    assert marker.truncated
    # The terminal suffix converts any incomplete human source to its existing
    # one-item omission marker; this is not the number of image occurrences.
    assert marker.item_omitted_after == 1
    assert not evidence.source_coverage.post_proposal_human_source_complete
    assert not evidence.source_coverage.relation_authority


@pytest.mark.postgres
@pytest.mark.parametrize("failure", ("ambiguous_occurrence", "oversized_body"))
def test_incomplete_post_proposal_human_source_fails_closed_before_model_semantics(
    stage2_migrated_postgres_database,
    failure: str,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository)
    turn_id, candidate = _install_running_main_candidate(
        repository,
        lease,
        user_text="Remember a preference",
        statement="Always answer in one sentence",
    )
    body = "Only for this turn; do not retain it"
    if failure == "oversized_body":
        body += " x" * 20_000
    human_entry_id, human_event_id = _insert_exact_source_entry(
        repository,
        lease,
        turn_id=turn_id,
        entry_kind="USER_STEER",
        body=body,
    )
    if failure == "ambiguous_occurrence":
        with repository.connection_provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            session = connection.execute(
                """SELECT workspace_id, latest_event_sequence
                   FROM pulsara_v3.sessions WHERE id=%s FOR UPDATE""",
                (lease.guard.session_id,),
            ).fetchone()
            assert session is not None
            duplicate_sequence = int(session[1]) + 1
            connection.execute(
                """INSERT INTO pulsara_v3.agent_events (
                       event_id, workspace_id, session_id, event_sequence,
                       namespace, event_type, schema_major, schema_minor,
                       occurred_at, actor_kind, actor_id, sensitivity_class,
                       projection_profile, payload, subject_entry_id
                   ) SELECT %s, workspace_id, session_id, %s, namespace, event_type,
                            schema_major, schema_minor, occurred_at, actor_kind, actor_id,
                            sensitivity_class, projection_profile, payload, subject_entry_id
                   FROM pulsara_v3.agent_events WHERE event_id=%s""",
                (_id("event"), duplicate_sequence, human_event_id),
            )
            connection.execute(
                """UPDATE pulsara_v3.sessions SET latest_event_sequence=%s
                   WHERE id=%s""",
                (duplicate_sequence, lease.guard.session_id),
            )
    repository.interrupt_turn(
        lease.guard,
        turn_id=turn_id,
        reason="USER_WITHDREW_MEMORY",
        occurred_at=datetime.now(timezone.utc),
        actor_id="runtime:test",
        deadline_monotonic=monotonic() + 30,
    )
    claimed = repository.claim_memory_candidate_for_governance(
        lease.guard,
        candidate_id=candidate.candidate_id,
        processing_started_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    assert claimed is not None
    evidence = repository.read_memory_governance_evidence(
        lease.guard,
        candidate=claimed,
        deadline_monotonic=monotonic() + 30,
    )
    assert not evidence.source_coverage.post_proposal_human_source_complete
    assert not evidence.source_coverage.relation_authority
    assert evidence.source_coverage.omitted_post_proposal_human_items == 1
    marker = next(
        item
        for item in evidence.post_proposal_turn_suffix
        if item.source_entry_id == human_entry_id
    )
    assert marker.truncated
    assert marker.anchor

    model = _ForbiddenGovernanceModel()
    io_owner = KernelSessionIO()
    workspace_id = repository.read_session_workspace_id(
        lease.guard, deadline_monotonic=monotonic() + 30
    )
    governor = AdvisoryMemoryGovernor(
        repository=repository,
        guard=lease.guard,
        read_binding=freeze_memory_read_context_binding(
            domain=MemoryDomainContext("u_local", "transient"),
            host_workspace_id=workspace_id,
        ),
        model=model,
        input_reader=CanonicalProviderInputReader(repository.connection_provider),
        io_owner=io_owner,
        deadline_factory=KernelExecutionDeadlineFactory(),
    )

    async def settle_without_provider() -> None:
        try:
            await governor._govern(  # noqa: SLF001 - focused owner contract test
                claimed,
                deadline_monotonic=monotonic() + 30,
            )
        finally:
            await io_owner.aclose(deadline_monotonic=monotonic() + 30)

    asyncio.run(settle_without_provider())
    assert model.calls == 0
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            """SELECT status, decision_reason_code
               FROM pulsara_v3.memory_candidates WHERE id=%s""",
            (candidate.candidate_id,),
        ).fetchone() == ("SKIPPED", "INSUFFICIENT_SOURCE_SUPPORT")
