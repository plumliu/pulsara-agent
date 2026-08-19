"""Round 8 advisory-memory product, authority, and PostgreSQL contracts."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from math import nan
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg import IsolationLevel
from psycopg.errors import CheckViolation

from pulsara_agent.capability.builtin_catalog import builtin_tool_catalog
from pulsara_agent.conversation_kernel.contracts import InlineContent
from pulsara_agent.conversation_kernel.memory.contracts import (
    FrozenMemoryGovernanceDecision,
    FrozenModelCallMemoryContext,
    FrozenModelVisibleMemoryProvenance,
    FrozenMemoryPublicFactProjection,
    FrozenMemoryProposal,
    MemoryCandidateStatus,
    MemoryDecisionKind,
    MemoryFactKind,
    MemoryKindHint,
    ModelVisibleMemoryProvenanceDisposition,
    MemoryUsePolicy,
    MemoryProducerKind,
    MemorySupersedeMode,
    PreparedMemoryBasisReference,
    memory_fact_semantic_digest,
    prepare_memory_candidate,
    prepare_memory_governance_acceptance,
    strongest_memory_use_policy,
)
from pulsara_agent.conversation_kernel.memory.recall import (
    MEMORY_EMBEDDING_CONTRACT_ID,
    MEMORY_EMBEDDING_CONTRACT_VERSION,
    MemoryDenseCandidateDisposition,
    PostgresMemoryQuery,
)
from pulsara_agent.conversation_kernel.memory.reflection import (
    CheapHintEligibleEntry,
    CheapMemoryHintSetV1,
    MemoryWriteOptOut,
    PreparedCheapHintReflectionHandoff,
    TurnMemoryUseOptOut,
    cheap_hint_handoff_fingerprint,
)
from pulsara_agent.conversation_kernel.memory.governor import (
    _prepare_reflection_batch,
)
from pulsara_agent.conversation_kernel.repository import (
    AssistantTextBlock,
    ConversationKernelRepository,
)
from pulsara_agent.conversation_kernel.memory_tools import (
    AutomaticMemoryTriggerDisposition,
    KernelMemoryToolPort,
    _sensitive_profile_is_eligible,
)
from pulsara_agent.conversation_kernel.live import LiveAgentEventBus
from pulsara_agent.conversation_kernel.runner import KernelToolAuthorizationKind
from pulsara_agent.conversation_kernel.tool_policy import (
    DefaultToolDispatchAuthorizationPolicy,
)
from pulsara_agent.conversation_kernel.tool_runtime import DirectKernelToolPort
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.vocabulary import (
    APPEND_GUARDS,
    COMMITTED_EVENT_DESCRIPTORS,
    LIVE_EVENT_TYPES,
    SUBJECT_SLOTS,
)
from pulsara_agent.memory.scope import (
    CTX_USER,
    MemoryDomainContext,
    MemoryScopeKind,
    freeze_memory_read_scope_binding,
    workspace_scope,
)
from pulsara_agent.primitives.permission import DEFAULT_PERMISSION_MODE
from pulsara_agent.primitives.run_permission import (
    RunPermissionAdmissionSource,
    build_run_permission_snapshot,
)
from pulsara_agent.model_input.contracts import ModelInputScopeKind
from pulsara_agent.retrieval.tokenizer import MemoryRetrievalTokenizerV1
from pulsara_agent.retrieval.config import (
    AdvisoryMemoryFeatureConfig,
    EmbeddingBackendConfig,
    RerankBackendConfig,
)
from pulsara_agent.retrieval.embedding.validation import (
    freeze_v1_embedding_vector,
)
from pulsara_agent.storage.migrations.manifest import CONVERSATION_KERNEL_RELATIONS
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane
from tests.support.postgres import verified_postgres_provider


pytestmark = pytest.mark.postgres


def _name(prefix: str) -> str:
    return f"{prefix}:{uuid4().hex}"


def _repository(database) -> ConversationKernelRepository:
    return ConversationKernelRepository(verified_postgres_provider(database.runtime_dsn))


def _lease(repository, *, workspace_id: str, domain: str = "u_local"):
    return repository.acquire_host_writer(
        session_id=_name("session"),
        workspace_id=workspace_id,
        memory_domain_id=domain,
        writer_owner_id=_name("host"),
        lease_seconds=30,
        deadline_monotonic=monotonic() + 30,
    )


def _completed_human_entry(repository, lease, text: str) -> str:
    now = datetime.now(timezone.utc)
    turn_id = _name("turn")
    entry_id = _name("entry")
    repository.start_root_turn(
        lease.guard,
        command_id=_name("command"),
        turn_id=turn_id,
        entry_id=entry_id,
        context_binding_revision_id=_name("revision"),
        permission_snapshot_id=_name("permission"),
        requested_permission_mode=DEFAULT_PERMISSION_MODE,
        content=InlineContent.from_bytes(text.encode()),
        occurred_at=now,
        deadline_monotonic=monotonic() + 30,
    )
    cut = repository.prepare_provider_input_cut(
        lease.guard, turn_id=turn_id, deadline_monotonic=monotonic() + 30
    )
    repository.commit_assistant_message(
        lease.guard,
        cut=cut,
        entry_id=_name("entry"),
        parent_content=InlineContent.from_bytes(b"ack"),
        blocks=(
            AssistantTextBlock(
                _name("block"), InlineContent.from_bytes(b"ack")
            ),
        ),
        complete_turn=True,
        occurred_at=now,
        actor_id="model:test",
        deadline_monotonic=monotonic() + 30,
    )
    return entry_id


def _claim_candidate(
    repository,
    lease,
    *,
    statement: str,
    kind_hint: MemoryKindHint,
    scope_kind: MemoryScopeKind = MemoryScopeKind.USER,
    scope_id: str = CTX_USER,
    applies_when: str | None = None,
    based_on: tuple[str, ...] = (),
    domain: str = "u_local",
):
    trigger_entry_id = _completed_human_entry(repository, lease, statement)
    candidate = prepare_memory_candidate(
        candidate_id=_name("candidate"),
        memory_domain_id=domain,
        origin_workspace_id=repository.read_session_workspace_id(
            lease.guard, deadline_monotonic=monotonic() + 30
        ),
        origin_session_id=lease.guard.session_id,
        producer_kind=MemoryProducerKind.CHEAP_HINT_REFLECTION,
        trigger_user_entry_id=trigger_entry_id,
        producer_candidate_ordinal=0,
        proposal=FrozenMemoryProposal(
            statement=statement,
            scope_kind=scope_kind,
            scope_id=scope_id,
            kind_hint=kind_hint,
            applies_when=applies_when,
            based_on_memory_ids=based_on,
        ),
        basis_refs=tuple(
            PreparedMemoryBasisReference(
                target_fact_id=fact_id,
                target_scope_kind=MemoryScopeKind.USER,
                target_scope_id=CTX_USER,
                ordinal=ordinal,
            )
            for ordinal, fact_id in enumerate(based_on)
        ),
    )
    repository.accept_reflection_memory_candidates(
        lease.guard,
        candidates=(candidate,),
        deadline_monotonic=monotonic() + 30,
    )
    claimed = repository.claim_memory_candidate_for_governance(
        lease.guard,
        candidate_id=candidate.candidate_id,
        processing_started_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    assert claimed is not None
    return claimed.prepared


def _settle(repository, lease, candidate, decision):
    evidence = repository.read_memory_governance_evidence(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    relation_targets = ()
    if decision.related_target_fact_id is not None:
        binding = freeze_memory_read_scope_binding(
            domain=MemoryDomainContext(candidate.memory_domain_id, "transient"),
            host_workspace_id=candidate.origin_workspace_id,
        )
        target = PostgresMemoryQuery(repository.connection_provider).get(
            read_binding=binding,
            fact_id=decision.related_target_fact_id,
            deadline_monotonic=monotonic() + 30,
        )
        assert target is not None
        relation_targets = (
            FrozenMemoryPublicFactProjection(
                fact_id=target.fact_id,
                scope_kind=MemoryScopeKind(target.scope_kind),
                scope_id=target.scope_id,
                fact_kind=MemoryFactKind(target.fact_kind),
                # A second settlement may observe the exact relation winner
                # after it applied the prepared ACTIVE -> SUPERSEDED effect.
                lifecycle="ACTIVE",
                statement=target.statement,
                applies_when=target.applies_when,
                do_not_apply_when=target.do_not_apply_when,
                fact_semantic_digest=target.fact_semantic_digest,
            ),
        )
    prepared = prepare_memory_governance_acceptance(
        candidate=candidate,
        decision=decision,
        basis_items=evidence.basis_items,
        relation_targets=relation_targets,
    )
    result = repository.accept_memory_governance(
        lease.guard,
        prepared=prepared,
        decided_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    settlement = None
    while hasattr(result, "settlement_fingerprint"):
        settlement = result
        result = repository.settle_existing_source_memory_relation(
            lease.guard,
            prepared=prepared,
            settlement=settlement,
            decided_at=datetime.now(timezone.utc),
            deadline_monotonic=monotonic() + 30,
        )
    assert (
        repository.confirm_memory_governance_winner(
            prepared=prepared,
            existing_settlement=settlement,
            deadline_monotonic=monotonic() + 30,
        ).value
        == "FULL"
    )
    return prepared, result


def test_round8_closed_taxonomy_tokenizer_and_process_local_architecture() -> None:
    assert tuple(item.value for item in MemoryFactKind) == (
        "FACT",
        "USER_PROFILE",
        "RESPONSE_PREFERENCE",
        "ACTION_RULE",
        "DECISION",
    )
    assert len(COMMITTED_EVENT_DESCRIPTORS) == 31
    assert len(LIVE_EVENT_TYPES) == 24
    assert len(SUBJECT_SLOTS) == 13
    assert len(APPEND_GUARDS) == 2
    assert len(CONVERSATION_KERNEL_RELATIONS) == 25
    tokenizer = MemoryRetrievalTokenizerV1()
    terms = tokenizer.tokenize(
        "请记住 FastAPI routes live at src/api/user_profile.py and error E_CONN_42"
    )
    assert "fastapi" in terms
    assert "src/api/user_profile.py" in terms
    assert "user_profile" in terms
    assert "e_conn_42" in terms
    assert "记住" in terms
    assert tokenizer.tokenize("Do not use yarn") != tokenizer.tokenize("Use yarn")
    assert tokenizer.tokenize("不使用 yarn") != tokenizer.tokenize("使用 yarn")
    assert "前" in tokenizer.tokenize("修改前立即备份")
    assert "后" in tokenizer.tokenize("修改后立即备份")
    contraction_terms = tokenizer.tokenize(
        "Don't use C++ / C# at foo/bar.py:42, please。"
    )
    assert "not" in contraction_terms
    assert {"don", "t", "'", "/", ",", "。"}.isdisjoint(contraction_terms)
    assert {"c++", "c#", "foo/bar.py:42"} <= set(contraction_terms)

    root = Path(__file__).resolve().parents[1] / "src/pulsara_agent"
    production = "\n".join(path.read_text() for path in root.rglob("*.py"))
    for forbidden in (
        "MEMORY_GOVERNANCE\"",
        "MEMORY_INDEX_REFRESH\"",
        "POST_COMPACTION_MEMORY_EXTRACTION\"",
        "MemoryFactAccepted",
        "MemoryRelationAccepted",
        "MemoryFactLifecycleChanged",
    ):
        assert forbidden not in production


def test_round8_memory_remote_credentials_never_fallback_to_main_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "PULSARA_EMBEDDING_API_KEY",
        "PULSARA_RERANK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PULSARA_API_KEY", "main-model-secret")
    monkeypatch.setenv("PULSARA_DASHSCOPE_API_KEY", "generic-dashscope-secret")

    assert EmbeddingBackendConfig.from_env().api_key == ""
    assert RerankBackendConfig.from_env().api_key == ""

    monkeypatch.setenv("PULSARA_EMBEDDING_API_KEY", "embedding-only")
    monkeypatch.setenv("PULSARA_RERANK_API_KEY", "rerank-only")
    assert EmbeddingBackendConfig.from_env().api_key == "embedding-only"
    assert RerankBackendConfig.from_env().api_key == "rerank-only"


def test_round8_opt_out_and_hint_matchers_are_closed() -> None:
    write_opt_out = MemoryWriteOptOut()
    turn_opt_out = TurnMemoryUseOptOut()
    for text in (
        "don't save this message",
        "please don't remember what I just said",
        "don't add this to memory",
        "不要记住这条消息",
        "别把这件事写入记忆",
    ):
        assert write_opt_out.excludes(text)
        assert not turn_opt_out.excludes(text)
    for text in (
        "I don't remember where the config is",
        "don't save files under /tmp",
        "不要记录日志",
        "请不要保存这个文件",
        "don't forget this",
        "不要忘记这个",
    ):
        assert not write_opt_out.excludes(text)
    for text in (
        "don't use saved memory for this answer",
        "answer without using memory",
        "本轮不使用记忆",
        "这次不要参考历史记忆",
        "不用记忆回答",
    ):
        assert turn_opt_out.excludes(text)
        assert not write_opt_out.excludes(text)
    assert not turn_opt_out.excludes("do not use memory mapping")
    assert not turn_opt_out.excludes(
        "for this answer, do not use memory mapping in the implementation"
    )
    assert not turn_opt_out.excludes("answer without using memory allocation")
    assert not turn_opt_out.excludes("不要使用内存")
    hints = CheapMemoryHintSetV1().match("Please remember that I prefer terse answers")
    assert hints
    assert all(item.signal_code and item.normalized_excerpt for item in hints)
    assert not CheapMemoryHintSetV1().match("Please don't run the tests yet")
    assert CheapMemoryHintSetV1().match("I like Sichuan food")

    policy = MemoryUsePolicy.ENABLED
    for candidate in (
        MemoryUsePolicy.WRITE_DISABLED_BY_USER,
        MemoryUsePolicy.ENABLED,
        MemoryUsePolicy.ALL_DISABLED_BY_USER,
        MemoryUsePolicy.ENABLED,
    ):
        policy = strongest_memory_use_policy(
            policy,
            candidate,
        )
    assert policy is MemoryUsePolicy.ALL_DISABLED_BY_USER


def test_round8_memory_use_policy_is_enforced_without_changing_tool_surface(
    tmp_path: Path,
) -> None:
    class MemoryPort:
        tool_names = frozenset(
            {"remember", "memory_search", "memory_get", "memory_explain"}
        )

        async def invoke(self, **_kwargs: object) -> object:
            raise AssertionError("denied memory tool reached physical invoke")

    def context(policy: MemoryUsePolicy) -> FrozenModelCallMemoryContext:
        return FrozenModelCallMemoryContext(
            FrozenModelVisibleMemoryProvenance(
                ModelVisibleMemoryProvenanceDisposition.COMPLETE,
                (),
            ),
            memory_use_policy=policy,
        )

    async def exercise() -> None:
        port = DirectKernelToolPort(
            workspace_root=tmp_path,
            host_owner_id="host:memory-policy",
            session_id="session:memory-policy",
            live_bus=LiveAgentEventBus(),
            authorization_policy=DefaultToolDispatchAuthorizationPolicy(),
        )
        port.bind_memory_port(MemoryPort())  # type: ignore[arg-type]
        surface = port.snapshot_tool_surface(
            conversation_scope_kind=ModelInputScopeKind.ROOT,
            scope_subagent_task_id=None,
        )
        advertised = {item.name for item in surface.model_surface.tool_specs}
        assert {
            "remember",
            "memory_search",
            "memory_get",
            "memory_explain",
            "read_file",
        } <= advertised
        borrow = port.borrow_tool_surface(surface)
        permission = build_run_permission_snapshot(
            snapshot_id="permission:memory-policy",
            requested_mode=DEFAULT_PERMISSION_MODE,
            effective_mode=DEFAULT_PERMISSION_MODE,
            admission_source=RunPermissionAdmissionSource.USER_SUBMISSION,
        )
        try:
            write_denied = await port.authorize(
                tool_name="remember",
                arguments={},
                tool_call_id="call:remember",
                turn_id="turn:memory-policy",
                assistant_entry_id="entry:assistant",
                permission_snapshot=permission,
                surface_borrow=borrow,
                memory_context=context(MemoryUsePolicy.WRITE_DISABLED_BY_USER),
            )
            assert (
                write_denied.kind
                is KernelToolAuthorizationKind.PERMISSION_DENIED
            )
            read_allowed = await port.authorize(
                tool_name="memory_search",
                arguments={"query": "deployment preference"},
                tool_call_id="call:search",
                turn_id="turn:memory-policy",
                assistant_entry_id="entry:assistant",
                permission_snapshot=permission,
                surface_borrow=borrow,
                memory_context=context(MemoryUsePolicy.WRITE_DISABLED_BY_USER),
            )
            assert read_allowed.kind is KernelToolAuthorizationKind.ALLOW
            for name in (
                "remember",
                "memory_search",
                "memory_get",
                "memory_explain",
            ):
                denied = await port.authorize(
                    tool_name=name,
                    arguments={},
                    tool_call_id=f"call:{name}:all-disabled",
                    turn_id="turn:memory-policy",
                    assistant_entry_id="entry:assistant",
                    permission_snapshot=permission,
                    surface_borrow=borrow,
                    memory_context=context(MemoryUsePolicy.ALL_DISABLED_BY_USER),
                )
                assert denied.kind is KernelToolAuthorizationKind.PERMISSION_DENIED
            ordinary = await port.authorize(
                tool_name="read_file",
                arguments={"path": "README.md"},
                tool_call_id="call:ordinary",
                turn_id="turn:memory-policy",
                assistant_entry_id="entry:assistant",
                permission_snapshot=permission,
                surface_borrow=borrow,
                memory_context=context(MemoryUsePolicy.ALL_DISABLED_BY_USER),
            )
            assert ordinary.kind is KernelToolAuthorizationKind.ALLOW
        finally:
            borrow.close()
            await port.aclose(timeout_seconds=2)

    asyncio.run(exercise())


def test_round8_reflection_can_select_but_cannot_rewrite_human_text() -> None:
    entry = CheapHintEligibleEntry(
        entry_id="entry:reflection",
        entry_sequence=1,
        public_text="I Prefer concise replies.",
        adjacent_assistant_text="",
        hints=CheapMemoryHintSetV1().match("I Prefer concise replies."),
    )
    values = {
        "session_id": "session:reflection",
        "workspace_id": "ctx:workspace/reflection",
        "memory_domain_id": "u_local",
        "workspace_scope_id": "ctx:workspace/reflection",
        "turn_id": "turn:reflection",
        "permission_snapshot_fingerprint": "sha256:" + "1" * 64,
        "provider_trust_domain_identity": "sha256:" + "2" * 64,
        "eligible_entries": (entry,),
        "final_assistant_text": "ack",
    }
    provisional = object.__new__(PreparedCheapHintReflectionHandoff)
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    object.__setattr__(provisional, "handoff_fingerprint", "")
    handoff = PreparedCheapHintReflectionHandoff(
        **values,
        handoff_fingerprint=cheap_hint_handoff_fingerprint(provisional),
    )

    accepted = _prepare_reflection_batch(
        handoff,
        {
            "candidates": [
                {
                    "source": "user:1",
                    "statement": "I Prefer concise replies.",
                    "scope": "USER",
                    "kind_hint": "RESPONSE_PREFERENCE",
                    "applies_when": None,
                    "do_not_apply_when": [],
                }
            ]
        },
    )
    assert accepted.candidates[0].proposal.statement == "I Prefer concise replies."

    with pytest.raises(ValueError, match="rewrote"):
        _prepare_reflection_batch(
            handoff,
            {
                "candidates": [
                    {
                        "source": "user:1",
                        "statement": "i prefer concise replies.",
                        "scope": "USER",
                        "kind_hint": "RESPONSE_PREFERENCE",
                        "applies_when": None,
                        "do_not_apply_when": [],
                    }
                ]
            },
        )


def test_round8_duplicate_relations_taxonomy_correction_and_basis_are_exact(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository, workspace_id=_name("workspace"))

    old_candidate = _claim_candidate(
        repository,
        lease,
        statement="The user likes Sichuan food",
        kind_hint=MemoryKindHint.RESPONSE_PREFERENCE,
    )
    _, old = _settle(
        repository,
        lease,
        old_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.RESPONSE_PREFERENCE,
        ),
    )
    assert old.fact_id is not None

    correct_candidate = _claim_candidate(
        repository,
        lease,
        statement="The user likes Sichuan food",
        kind_hint=MemoryKindHint.USER_PROFILE,
    )
    _, correct = _settle(
        repository,
        lease,
        correct_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.USER_PROFILE,
        ),
    )
    assert correct.fact_id is not None

    duplicate_candidate = _claim_candidate(
        repository,
        lease,
        statement="The user likes Sichuan food",
        kind_hint=MemoryKindHint.USER_PROFILE,
    )
    _, applied = _settle(
        repository,
        lease,
        duplicate_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT_AND_SUPERSEDE,
            final_kind=MemoryFactKind.USER_PROFILE,
            related_target_fact_id=old.fact_id,
            supersede_mode=MemorySupersedeMode.TAXONOMY_CORRECTION,
        ),
    )
    # The already-existing correct fact becomes the relation source without
    # acquiring a second producer row.
    assert applied.status is MemoryCandidateStatus.APPLIED_TO_EXISTING
    assert applied.duplicate_winner_fact_id == correct.fact_id

    relation_duplicate_candidate = _claim_candidate(
        repository,
        lease,
        statement="The user likes Sichuan food",
        kind_hint=MemoryKindHint.USER_PROFILE,
    )
    _, relation_duplicate = _settle(
        repository,
        lease,
        relation_duplicate_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT_AND_SUPERSEDE,
            final_kind=MemoryFactKind.USER_PROFILE,
            related_target_fact_id=old.fact_id,
            supersede_mode=MemorySupersedeMode.TAXONOMY_CORRECTION,
        ),
    )
    assert relation_duplicate.status is MemoryCandidateStatus.SKIPPED
    assert relation_duplicate.duplicate_winner_fact_id == correct.fact_id

    plain_candidate = _claim_candidate(
        repository,
        lease,
        statement="The user likes Sichuan food",
        kind_hint=MemoryKindHint.USER_PROFILE,
    )
    _, plain = _settle(
        repository,
        lease,
        plain_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.USER_PROFILE,
        ),
    )
    assert plain.status is MemoryCandidateStatus.SKIPPED
    assert plain.duplicate_winner_fact_id == correct.fact_id

    decision_candidate = _claim_candidate(
        repository,
        lease,
        statement="We selected PostgreSQL for durable storage",
        kind_hint=MemoryKindHint.DECISION,
        based_on=(correct.fact_id,),
    )
    _, decision = _settle(
        repository,
        lease,
        decision_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.DECISION,
        ),
    )
    assert decision.fact_id is not None
    duplicate_basis = _claim_candidate(
        repository,
        lease,
        statement="We selected PostgreSQL for durable storage",
        kind_hint=MemoryKindHint.DECISION,
        based_on=(correct.fact_id,),
    )
    _, basis_loser = _settle(
        repository,
        lease,
        duplicate_basis,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.DECISION,
        ),
    )
    assert basis_loser.status is MemoryCandidateStatus.SKIPPED
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.memory_relations WHERE relation_kind='BASED_ON'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT source_candidate_id FROM pulsara_v3.memory_facts WHERE id=%s",
            (correct.fact_id,),
        ).fetchone() == (correct_candidate.candidate_id,)


def test_round8_reverse_contradiction_confirms_the_unordered_relation_winner(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository, workspace_id=_name("workspace"))

    first_candidate = _claim_candidate(
        repository,
        lease,
        statement="The release is scheduled for Tuesday",
        kind_hint=MemoryKindHint.FACT,
    )
    _, first = _settle(
        repository,
        lease,
        first_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.FACT,
        ),
    )
    assert first.fact_id is not None

    second_candidate = _claim_candidate(
        repository,
        lease,
        statement="The release is scheduled for Wednesday",
        kind_hint=MemoryKindHint.FACT,
    )
    _, second = _settle(
        repository,
        lease,
        second_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT_AND_CONTRADICT,
            final_kind=MemoryFactKind.FACT,
            related_target_fact_id=first.fact_id,
        ),
    )
    assert second.fact_id is not None and second.relation_id is not None

    reverse_candidate = _claim_candidate(
        repository,
        lease,
        statement="The release is scheduled for Tuesday",
        kind_hint=MemoryKindHint.FACT,
    )
    _, reverse = _settle(
        repository,
        lease,
        reverse_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT_AND_CONTRADICT,
            final_kind=MemoryFactKind.FACT,
            related_target_fact_id=second.fact_id,
        ),
    )
    assert reverse.status is MemoryCandidateStatus.SKIPPED
    assert reverse.duplicate_winner_fact_id == first.fact_id
    assert reverse.relation_id == second.relation_id

    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            """
            SELECT id, source_fact_id, target_fact_id
            FROM pulsara_v3.memory_relations
            WHERE relation_kind='CONTRADICTS'
            """
        ).fetchall()
    assert row == [(second.relation_id, second.fact_id, first.fact_id)]


def test_round8_scope_sparse_recall_and_cross_origin_provenance_redaction(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    workspace_a = _name("workspace")
    workspace_b = _name("workspace")
    lease_a = _lease(repository, workspace_id=workspace_a)
    candidate = _claim_candidate(
        repository,
        lease_a,
        statement="中文服务的错误码 E_CONN_42 位于 src/api/user_profile.py",
        kind_hint=MemoryKindHint.FACT,
    )
    _, accepted = _settle(
        repository,
        lease_a,
        candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.FACT,
        ),
    )
    assert accepted.fact_id is not None

    query = PostgresMemoryQuery(repository.connection_provider)
    binding_a = freeze_memory_read_scope_binding(
        domain=MemoryDomainContext("u_local", "transient"),
        host_workspace_id=workspace_a,
    )
    sparse = query._sparse(
        read_binding=binding_a,
        terms=MemoryRetrievalTokenizerV1().tokenize(
            "E_CONN_42 user_profile.py 中文服务"
        ),
        scope_filter=None,
        kind_filter=None,
        limit=20,
        automatic=False,
        deadline_monotonic=monotonic() + 30,
    )
    assert sparse and sparse[0].fact_id == accepted.fact_id
    result = query.search(
        read_binding=binding_a,
        query="E_CONN_42 user_profile.py 中文服务",
        deadline_monotonic=monotonic() + 30,
    )
    assert result.facts and result.facts[0].fact_id == accepted.fact_id, result
    same = query.provenance(
        read_binding=binding_a,
        fact_id=accepted.fact_id,
        deadline_monotonic=monotonic() + 30,
    )
    assert same is not None and same.provenance_disposition == "SAME_ORIGIN"
    assert same.producer_entry_id is not None

    binding_b = freeze_memory_read_scope_binding(
        domain=MemoryDomainContext("u_local", "transient"),
        host_workspace_id=workspace_b,
    )
    foreign = query.provenance(
        read_binding=binding_b,
        fact_id=accepted.fact_id,
        deadline_monotonic=monotonic() + 30,
    )
    assert foreign is not None
    assert foreign.provenance_disposition == "CROSS_ORIGIN_REDACTED"
    assert foreign.producer_session_id is None
    assert foreign.producer_turn_id is None
    assert foreign.producer_entry_id is None
    assert foreign.tool_result_ids == ()


def test_round8_tool_schema_proposal_shape_and_vector_contract_are_closed() -> None:
    memory_writes = tuple(
        item
        for item in builtin_tool_catalog()
        if item.descriptor.provider_kind.value == "memory"
        and not item.descriptor.is_read_only
    )
    assert tuple(item.descriptor.name for item in memory_writes) == ("remember",)
    schema = memory_writes[0].descriptor.input_schema
    assert schema["additionalProperties"] is False
    assert schema["properties"]["scope"]["enum"] == ("USER", "WORKSPACE")
    assert schema["properties"]["kind_hint"]["enum"] == (
        "AUTO",
        "FACT",
        "USER_PROFILE",
        "RESPONSE_PREFERENCE",
        "ACTION_RULE",
        "DECISION",
    )
    assert "force" not in schema["properties"]
    assert "authority" not in schema["properties"]

    with pytest.raises(ValueError, match="ACTION_RULE"):
        FrozenMemoryProposal(
            statement="Run the formatter",
            scope_kind=MemoryScopeKind.USER,
            scope_id=CTX_USER,
            kind_hint=MemoryKindHint.ACTION_RULE,
        )
    with pytest.raises(ValueError, match="incompatible structured fields"):
        FrozenMemoryProposal(
            statement="The deployment target is prod",
            scope_kind=MemoryScopeKind.USER,
            scope_id=CTX_USER,
            kind_hint=MemoryKindHint.FACT,
            based_on_memory_ids=("memory:one",),
        )
    with pytest.raises(ValueError, match="USER_PROFILE"):
        FrozenMemoryProposal(
            statement="The user maintains the backend in this repository",
            scope_kind=MemoryScopeKind.WORKSPACE,
            scope_id="ctx:workspace/example",
            kind_hint=MemoryKindHint.USER_PROFILE,
        )
    FrozenMemoryProposal(
        statement="Use the release checklist before deployment",
        scope_kind=MemoryScopeKind.USER,
        scope_id=CTX_USER,
        kind_hint=MemoryKindHint.ACTION_RULE,
        applies_when="before a production deployment",
    )

    assert len(freeze_v1_embedding_vector([1.0] * 1024)) == 1024
    for invalid in ([1.0] * 1023, [0.0] * 1024, [nan] * 1024):
        with pytest.raises(ValueError):
            freeze_v1_embedding_vector(invalid)


def test_round8_workspace_domain_visibility_origin_claim_and_relation_endpoints(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    domain = _name("domain").replace(":", "_")
    other_domain = _name("domain").replace(":", "_")
    project_a = "/tmp/round8/project-a"
    project_b = "/tmp/round8/project-b"
    workspace_a = workspace_scope(project_a)
    workspace_b = workspace_scope(project_b)
    lease_a = _lease(repository, workspace_id=workspace_a, domain=domain)
    lease_b = _lease(repository, workspace_id=workspace_b, domain=domain)
    _lease(repository, workspace_id=workspace_a, domain=other_domain)

    user_candidate = _claim_candidate(
        repository,
        lease_a,
        statement="The user prefers PostgreSQL examples",
        kind_hint=MemoryKindHint.USER_PROFILE,
        domain=domain,
    )
    _, user_fact = _settle(
        repository,
        lease_a,
        user_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT, final_kind=MemoryFactKind.USER_PROFILE
        ),
    )
    workspace_candidate = _claim_candidate(
        repository,
        lease_a,
        statement="This repository deploys from src/release.py",
        kind_hint=MemoryKindHint.FACT,
        scope_kind=MemoryScopeKind.WORKSPACE,
        scope_id=workspace_a,
        domain=domain,
    )
    _, workspace_fact = _settle(
        repository,
        lease_a,
        workspace_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT, final_kind=MemoryFactKind.FACT
        ),
    )
    assert user_fact.fact_id is not None and workspace_fact.fact_id is not None

    binding_a = freeze_memory_read_scope_binding(
        domain=MemoryDomainContext(domain, "project", project_a),
        host_workspace_id=workspace_a,
    )
    binding_b = freeze_memory_read_scope_binding(
        domain=MemoryDomainContext(domain, "project", project_b),
        host_workspace_id=workspace_b,
    )
    binding_foreign_domain = freeze_memory_read_scope_binding(
        domain=MemoryDomainContext(other_domain, "project", project_a),
        host_workspace_id=workspace_a,
    )
    query = PostgresMemoryQuery(repository.connection_provider)
    assert query.get(
        read_binding=binding_b,
        fact_id=user_fact.fact_id,
        deadline_monotonic=monotonic() + 30,
    ) is not None
    assert query.get(
        read_binding=binding_b,
        fact_id=workspace_fact.fact_id,
        deadline_monotonic=monotonic() + 30,
    ) is None
    assert query.get(
        read_binding=binding_foreign_domain,
        fact_id=user_fact.fact_id,
        deadline_monotonic=monotonic() + 30,
    ) is None

    decision_candidate = _claim_candidate(
        repository,
        lease_a,
        statement="We selected a migration checklist",
        kind_hint=MemoryKindHint.DECISION,
        scope_kind=MemoryScopeKind.WORKSPACE,
        scope_id=workspace_a,
        based_on=(user_fact.fact_id,),
        domain=domain,
    )
    _, decision_fact = _settle(
        repository,
        lease_a,
        decision_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT, final_kind=MemoryFactKind.DECISION
        ),
    )
    assert decision_fact.fact_id is not None
    assert query.direct_relations(
        read_binding=binding_a,
        fact_id=user_fact.fact_id,
        deadline_monotonic=monotonic() + 30,
    )
    assert query.direct_relations(
        read_binding=binding_b,
        fact_id=user_fact.fact_id,
        deadline_monotonic=monotonic() + 30,
    ) == ()

    trigger_entry_id = _completed_human_entry(
        repository, lease_a, "Please remember my editor setting"
    )
    pending = prepare_memory_candidate(
        candidate_id=_name("pending"),
        memory_domain_id=domain,
        origin_workspace_id=workspace_a,
        origin_session_id=lease_a.guard.session_id,
        producer_kind=MemoryProducerKind.CHEAP_HINT_REFLECTION,
        trigger_user_entry_id=trigger_entry_id,
        producer_candidate_ordinal=0,
        proposal=FrozenMemoryProposal(
            statement="The user uses a compact editor layout",
            scope_kind=MemoryScopeKind.USER,
            scope_id=CTX_USER,
            kind_hint=MemoryKindHint.USER_PROFILE,
        ),
    )
    repository.accept_reflection_memory_candidates(
        lease_a.guard,
        candidates=(pending,),
        deadline_monotonic=monotonic() + 30,
    )
    assert repository.claim_memory_candidate_for_governance(
        lease_b.guard,
        candidate_id=pending.candidate_id,
        processing_started_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    ) is None
    assert repository.claim_memory_candidate_for_governance(
        lease_a.guard,
        candidate_id=pending.candidate_id,
        processing_started_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    ) is not None


def test_round8_response_preference_capacity_and_atomic_replacement(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository, workspace_id=_name("workspace"))
    accepted_ids: list[str] = []
    for ordinal in range(16):
        candidate = _claim_candidate(
            repository,
            lease,
            statement=f"Preference {ordinal}: keep answer shape stable",
            kind_hint=MemoryKindHint.RESPONSE_PREFERENCE,
        )
        _, accepted = _settle(
            repository,
            lease,
            candidate,
            FrozenMemoryGovernanceDecision(
                MemoryDecisionKind.ACCEPT,
                final_kind=MemoryFactKind.RESPONSE_PREFERENCE,
            ),
        )
        assert accepted.fact_id is not None
        accepted_ids.append(accepted.fact_id)

    overflow_candidate = _claim_candidate(
        repository,
        lease,
        statement="Preference 17: answer using a table",
        kind_hint=MemoryKindHint.RESPONSE_PREFERENCE,
    )
    _, overflow = _settle(
        repository,
        lease,
        overflow_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.RESPONSE_PREFERENCE,
        ),
    )
    assert overflow.status is MemoryCandidateStatus.SKIPPED
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT decision_reason_code FROM pulsara_v3.memory_candidates WHERE id=%s",
            (overflow_candidate.candidate_id,),
        ).fetchone() == ("RESPONSE_PREFERENCE_CAPACITY_EXCEEDED",)

    replacement_candidate = _claim_candidate(
        repository,
        lease,
        statement="Preference replacement: start with the conclusion",
        kind_hint=MemoryKindHint.RESPONSE_PREFERENCE,
    )
    _, replacement = _settle(
        repository,
        lease,
        replacement_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT_AND_SUPERSEDE,
            final_kind=MemoryFactKind.RESPONSE_PREFERENCE,
            related_target_fact_id=accepted_ids[0],
            supersede_mode=MemorySupersedeMode.SAME_KIND_REPLACEMENT,
        ),
    )
    assert replacement.status is MemoryCandidateStatus.ACCEPTED
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pulsara_v3.memory_facts WHERE memory_domain_id='u_local' "
            "AND scope_kind='USER' AND scope_id='ctx:user' "
            "AND fact_kind='RESPONSE_PREFERENCE' AND lifecycle='ACTIVE'"
        ).fetchone() == (16,)


def test_round8_search_document_is_trigger_sealed_and_not_repository_input(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    lease = _lease(repository, workspace_id=_name("workspace"))
    candidate = _claim_candidate(
        repository,
        lease,
        statement="The service code is src/runtime/memory_index.py",
        kind_hint=MemoryKindHint.FACT,
    )
    evidence = repository.read_memory_governance_evidence(
        lease.guard,
        candidate=candidate,
        deadline_monotonic=monotonic() + 30,
    )
    prepared = prepare_memory_governance_acceptance(
        candidate=candidate,
        decision=FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT, final_kind=MemoryFactKind.FACT
        ),
        basis_items=evidence.basis_items,
        relation_targets=(),
    )
    fact = prepared.fact
    assert fact is not None
    now = datetime.now(timezone.utc)
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.BACKGROUND_WORK,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        column = connection.execute(
            "SELECT is_nullable, is_generated FROM information_schema.columns "
            "WHERE table_schema='pulsara_v3' AND table_name='memory_facts' "
            "AND column_name='search_document'"
        ).fetchone()
        assert column == ("NO", "NEVER")
        connection.execute(
            """
            UPDATE pulsara_v3.memory_candidates
            SET status='ACCEPTED', decision_kind='ACCEPT', final_kind=%s,
                accepted_fact_id=%s, decided_at=%s, accepted_fact_at=%s
            WHERE id=%s AND status='PROCESSING'
            """,
            (fact.fact_kind.value, fact.fact_id, now, now, candidate.candidate_id),
        )
        connection.execute(
            """
            INSERT INTO pulsara_v3.memory_facts (
                id, memory_domain_id, scope_kind, scope_id, source_candidate_id,
                lifecycle, fact_kind, statement, applies_when, do_not_apply_when,
                fact_semantic_digest, accepted_at, updated_at,
                search_contract_id, search_contract_version, search_terms,
                search_document
            ) VALUES (%s,%s,%s,%s,%s,'ACTIVE',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                      to_tsvector('simple', 'forged_only'))
            """,
            (
                fact.fact_id,
                fact.memory_domain_id,
                fact.scope_kind.value,
                fact.scope_id,
                candidate.candidate_id,
                fact.fact_kind.value,
                fact.statement,
                fact.applies_when,
                list(fact.do_not_apply_when),
                fact.fact_semantic_digest,
                now,
                now,
                fact.search_contract_id,
                fact.search_contract_version,
                list(fact.search_terms),
            ),
        )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        document = connection.execute(
            "SELECT search_document::text FROM pulsara_v3.memory_facts WHERE id=%s",
            (fact.fact_id,),
        ).fetchone()[0]
        assert "forged_only" not in document
        assert "memory_index.py" in document

    with pytest.raises(CheckViolation):
        with repository.connection_provider.connection(
            lane=PostgresConnectionLane.BACKGROUND_WORK,
            deadline_monotonic=monotonic() + 30,
        ) as connection:
            connection.execute(
                "UPDATE pulsara_v3.memory_facts SET search_document=to_tsvector('simple','changed') "
                "WHERE id=%s",
                (fact.fact_id,),
            )


def test_round8_embedding_cache_revalidates_scope_digest_and_vector_shape(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    project = "/tmp/round8/vector-project"
    workspace = workspace_scope(project)
    domain = _name("domain").replace(":", "_")
    lease = _lease(repository, workspace_id=workspace, domain=domain)
    candidate = _claim_candidate(
        repository,
        lease,
        statement="Vector recall fixture for Chinese food preferences",
        kind_hint=MemoryKindHint.USER_PROFILE,
        domain=domain,
    )
    _, accepted = _settle(
        repository,
        lease,
        candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT, final_kind=MemoryFactKind.USER_PROFILE
        ),
    )
    assert accepted.fact_id is not None
    binding = freeze_memory_read_scope_binding(
        domain=MemoryDomainContext(domain, "project", project),
        host_workspace_id=workspace,
    )
    digest_value = memory_fact_semantic_digest(
        kind=MemoryFactKind.USER_PROFILE,
        statement="Vector recall fixture for Chinese food preferences",
        applies_when=None,
        do_not_apply_when=(),
    )
    assert repository.upsert_memory_embedding(
        read_binding=binding,
        fact_id=accepted.fact_id,
        fact_semantic_digest=digest_value,
        vector=[1.0] * 1024,
        embedded_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    assert not repository.upsert_memory_embedding(
        read_binding=binding,
        fact_id=accepted.fact_id,
        fact_semantic_digest="sha256:" + "0" * 64,
        vector=[1.0] * 1024,
        embedded_at=datetime.now(timezone.utc),
        deadline_monotonic=monotonic() + 30,
    )
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        row = connection.execute(
            "SELECT fact_semantic_digest, embedding_contract_id, "
            "embedding_contract_version, vector_dims(embedding) "
            "FROM pulsara_v3.memory_embeddings WHERE memory_domain_id=%s AND fact_id=%s",
            (domain, accepted.fact_id),
        ).fetchone()
    assert row == (
        digest_value,
        MEMORY_EMBEDDING_CONTRACT_ID,
        MEMORY_EMBEDDING_CONTRACT_VERSION,
        1024,
    )
    recalled = PostgresMemoryQuery(repository.connection_provider).search(
        read_binding=binding,
        query="lexically unrelated probe",
        limit=1,
        query_embedding=[1.0] * 1024,
        deadline_monotonic=monotonic() + 30,
    )
    assert recalled.facts[0].fact_id == accepted.fact_id
    assert recalled.dense_disposition is (
        MemoryDenseCandidateDisposition.PARTIAL_BOUNDED_SCAN
    )
    bounded = PostgresMemoryQuery(repository.connection_provider).dense_candidates(
        read_binding=binding,
        vector=[1.0] * 1024,
        scope_filter=None,
        kind_filter=None,
        limit=1,
        purpose="EXPLICIT_SEARCH",
        automatic=False,
        deadline_monotonic=monotonic() + 30,
    )
    assert bounded.facts[0].fact_id == accepted.fact_id
    assert bounded.disposition is MemoryDenseCandidateDisposition.BOUNDED_TOP_K
    with repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,
        deadline_monotonic=monotonic() + 30,
    ) as connection:
        connection.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            str(row[0])
            for row in connection.execute(
                "EXPLAIN SELECT fact_id FROM pulsara_v3.memory_embeddings "
                "ORDER BY embedding <=> %s::public.vector ASC LIMIT 1",
                ("[" + ",".join("1" for _ in range(1024)) + "]",),
            ).fetchall()
        )
    assert "idx_pulsara_v3_memory_embeddings_hnsw" in plan


def test_round8_sensitive_user_profile_requires_explicit_relevance() -> None:
    class Item:
        fact_kind = MemoryFactKind.USER_PROFILE.value
        statement = "The user's exact home address is 100 Example Street"

    assert not _sensitive_profile_is_eligible(Item(), "show Python examples")
    assert _sensitive_profile_is_eligible(Item(), "which address should delivery use?")

    class Ordinary:
        fact_kind = MemoryFactKind.USER_PROFILE.value
        statement = "The user likes Sichuan food"

    assert _sensitive_profile_is_eligible(Ordinary(), "show Python examples")
    assert _sensitive_profile_is_eligible(Ordinary(), "suggest a film")


def test_round8_preference_head_and_automatic_recall_are_separate_advisory_sources(
    stage2_migrated_postgres_database,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    workspace_id = _name("workspace")
    domain = _name("domain").replace(":", "_")
    lease = _lease(repository, workspace_id=workspace_id, domain=domain)

    preference_candidate = _claim_candidate(
        repository,
        lease,
        statement="Start answers with the conclusion",
        kind_hint=MemoryKindHint.RESPONSE_PREFERENCE,
        domain=domain,
    )
    _, preference = _settle(
        repository,
        lease,
        preference_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.RESPONSE_PREFERENCE,
        ),
    )
    profile_candidate = _claim_candidate(
        repository,
        lease,
        statement="The user likes Sichuan food",
        kind_hint=MemoryKindHint.USER_PROFILE,
        domain=domain,
    )
    _, profile = _settle(
        repository,
        lease,
        profile_candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.USER_PROFILE,
        ),
    )
    assert preference.fact_id is not None and profile.fact_id is not None

    io_owner = KernelSessionIO()
    binding = freeze_memory_read_scope_binding(
        domain=MemoryDomainContext(domain, "transient"),
        host_workspace_id=workspace_id,
    )
    port = KernelMemoryToolPort(
        repository=repository,
        session_id=lease.guard.session_id,
        read_binding=binding,
        embedding_config=EmbeddingBackendConfig(api_key=""),
        feature_config=AdvisoryMemoryFeatureConfig(
            automatic_dense=False,
            explicit_rerank=False,
            cheap_hint_reflection=False,
        ),
        io_owner=io_owner,
    )

    async def exercise() -> tuple[object, object, object]:
        try:
            head = await port.freeze_response_preference_source()
            recall = await port.freeze_automatic_recall_source(
                "Suggest Sichuan food for dinner"
            )
            skipped = await port.freeze_automatic_recall_source("short")
            with pytest.raises(RuntimeError, match="escaped its frozen"):
                await port.invoke(
                    tool_name="memory_search",
                    arguments={"query": "must not run"},
                    invocation_context=SimpleNamespace(
                        session_id=lease.guard.session_id,
                        memory_context=FrozenModelCallMemoryContext(
                            FrozenModelVisibleMemoryProvenance(
                                ModelVisibleMemoryProvenanceDisposition.COMPLETE,
                                (),
                            ),
                            memory_use_policy=(
                                MemoryUsePolicy.ALL_DISABLED_BY_USER
                            ),
                        ),
                    ),
                )
            return head, recall, skipped
        finally:
            await port.aclose()
            await io_owner.aclose(deadline_monotonic=monotonic() + 30)

    head, recall, skipped = asyncio.run(exercise())
    assert head.model_visible_memory_fact_ids == (preference.fact_id,)
    head_payload = json.loads(head.variants[0].text)
    assert head_payload["items"][0]["memory_id"] == preference.fact_id
    assert recall.model_visible_memory_fact_ids == (profile.fact_id,)
    recall_payload = json.loads(recall.variants[0].text)
    assert recall_payload["items"][0]["memory_id"] == profile.fact_id
    assert skipped.absence_kind.value == "EXPLICIT_EMPTY"
    write_opt_out = port.classify_memory_trigger("don't remember this")
    assert write_opt_out.automatic_recall is (
        AutomaticMemoryTriggerDisposition.ELIGIBLE
    )
    assert write_opt_out.memory_use is MemoryUsePolicy.WRITE_DISABLED_BY_USER
    all_opt_out = port.classify_memory_trigger(
        "don't use saved memory for this answer"
    )
    assert all_opt_out.automatic_recall is (
        AutomaticMemoryTriggerDisposition.DISABLED_BY_EXPLICIT_USER_DIRECTIVE
    )
    assert all_opt_out.memory_use is MemoryUsePolicy.ALL_DISABLED_BY_USER
    low_information = port.classify_memory_trigger("short")
    assert low_information.automatic_recall is (
        AutomaticMemoryTriggerDisposition.SKIPPED_LOW_INFORMATION
    )
    assert low_information.memory_use is MemoryUsePolicy.ENABLED


def test_round8_preference_head_uses_one_repeatable_read_composite(
    stage2_migrated_postgres_database,
) -> None:
    delegate = verified_postgres_provider(
        stage2_migrated_postgres_database.runtime_dsn
    )
    calls: list[dict[str, object]] = []

    class TracingProvider:
        @contextmanager
        def connection(self, **kwargs):
            calls.append(dict(kwargs))
            with delegate.connection(**kwargs) as connection:
                yield connection

    query = PostgresMemoryQuery(TracingProvider())
    snapshot = query.response_preference_snapshot(
        read_binding=freeze_memory_read_scope_binding(
            domain=MemoryDomainContext(
                _name("domain").replace(":", "_"), "transient"
            ),
            host_workspace_id=_name("workspace"),
        ),
        deadline_monotonic=monotonic() + 30,
    )
    assert snapshot.facts == ()
    assert snapshot.contradictions == ()
    assert len(calls) == 1
    assert calls[0]["lane"] is PostgresConnectionLane.MEMORY_QUERY
    assert calls[0]["isolation_level"] is IsolationLevel.REPEATABLE_READ


def test_round8_optional_provider_and_relation_failures_remain_advisory(
    stage2_migrated_postgres_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(stage2_migrated_postgres_database)
    workspace_id = _name("workspace")
    domain = _name("domain").replace(":", "_")
    lease = _lease(repository, workspace_id=workspace_id, domain=domain)
    candidate = _claim_candidate(
        repository,
        lease,
        statement="The project uses PostgreSQL for advisory recall",
        kind_hint=MemoryKindHint.FACT,
        domain=domain,
    )
    _, fact = _settle(
        repository,
        lease,
        candidate,
        FrozenMemoryGovernanceDecision(
            MemoryDecisionKind.ACCEPT,
            final_kind=MemoryFactKind.FACT,
        ),
    )
    assert fact.fact_id is not None

    def fail_provider(_config):
        raise RuntimeError("optional provider constructor failed")

    monkeypatch.setattr(
        "pulsara_agent.conversation_kernel.memory_tools.build_embedding_provider",
        fail_provider,
    )
    monkeypatch.setattr(
        "pulsara_agent.conversation_kernel.memory_tools.build_rerank_provider",
        fail_provider,
    )
    io_owner = KernelSessionIO()
    port = KernelMemoryToolPort(
        repository=repository,
        session_id=lease.guard.session_id,
        read_binding=freeze_memory_read_scope_binding(
            domain=MemoryDomainContext(domain, "transient"),
            host_workspace_id=workspace_id,
        ),
        embedding_config=EmbeddingBackendConfig(api_key="embedding-only"),
        rerank_config=RerankBackendConfig(api_key="rerank-only"),
        feature_config=AdvisoryMemoryFeatureConfig(
            automatic_dense=True,
            explicit_rerank=True,
            cheap_hint_reflection=False,
        ),
        io_owner=io_owner,
    )

    def fail_relation_enrichment(**_kwargs):
        raise TimeoutError("optional contradiction projection timed out")

    async def exercise():
        try:
            automatic = await port.freeze_automatic_recall_source(
                "Which database does this project use for advisory recall?"
            )
            explicit = await port._search(
                {"query": "PostgreSQL advisory recall", "limit": 5}
            )
            monkeypatch.setattr(
                port._query,
                "active_contradictions",
                fail_relation_enrichment,
            )
            unavailable = await port.freeze_automatic_recall_source(
                "Which database does this project use for advisory recall?"
            )
            explicit_without_relations = await port._search(
                {"query": "PostgreSQL advisory recall", "limit": 5}
            )
            return automatic, explicit, unavailable, explicit_without_relations
        finally:
            await port.aclose()
            await io_owner.aclose(deadline_monotonic=monotonic() + 30)

    automatic, explicit, unavailable, explicit_without_relations = asyncio.run(
        exercise()
    )
    assert automatic.model_visible_memory_fact_ids == (fact.fact_id,)
    explicit_payload = json.loads(explicit.content)
    assert explicit.state == "SUCCESS"
    assert explicit_payload["dense_result"] == "UNAVAILABLE"
    assert explicit_payload["rerank"] == "FAILED_FALLBACK"
    assert explicit_payload["relation_enrichment"] == "COMPLETE"
    assert unavailable.absence_kind.value == "UNAVAILABLE"
    relation_payload = json.loads(explicit_without_relations.content)
    assert explicit_without_relations.state == "SUCCESS"
    assert relation_payload["memories"]
    assert relation_payload["relation_enrichment"] == "UNAVAILABLE"
