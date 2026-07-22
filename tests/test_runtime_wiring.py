import asyncio
from threading import Event, Timer
from time import monotonic
from uuid import uuid4

import pytest

from tests.support.model_stream import (
    make_text_block_segment_event,
)

from pulsara_agent.event import EventContext
from pulsara_agent.event.candidates import PreferenceCandidate, ValidCandidatePayload
from pulsara_agent.llm import ModelRole
from tests.support import test_llm_config
from tests.support.capability import preview_capability_plan
from pulsara_agent.llm.request import LLMOptions
from pulsara_agent.memory import load_run_timeline, summarize_run_timeline
from pulsara_agent.memory.candidates.pool import (
    CandidateOrigin,
    PooledMemoryCandidate,
    SubmitAsIsDecision,
)
from pulsara_agent.memory.governance.executor import (
    GovernanceDecisionExecutionIdentity,
)
from pulsara_agent.memory.scope import MemoryDomainContext, workspace_scope
from pulsara_agent.ontology import memory
from pulsara_agent.runtime import (
    AgentRuntimeWiring,
    build_agent_runtime_wiring,
    build_durable_runtime_wiring,
    build_in_memory_runtime_wiring,
)
from pulsara_agent.runtime.permission import (
    ApprovalPolicy,
    EffectivePermissionPolicy,
    PermissionProfile,
    TerminalAccess,
)
from pulsara_agent.capability import (
    BuiltinToolCapabilityProvider,
    LocalSkillCapabilityProvider,
)
from pulsara_agent.settings import PulsaraSettings, StorageConfig
from tests.conftest import emit_test_accepted_model_reply, open_test_root_rollout_run
from tests.support.model_call import test_resolved_target_fact
from tests.support.settings import compatibility_storage_config
from pulsara_agent.memory.canonical.unit_of_work import (
    InMemoryMemoryWriteUnitOfWork,
)


def _governance_execution_identity(
    candidate: PooledMemoryCandidate,
) -> GovernanceDecisionExecutionIdentity:
    return GovernanceDecisionExecutionIdentity(
        batch_input_fingerprint="sha256:test-governance-batch-input",
        batch_input_reference_fingerprint="sha256:test-governance-batch-reference",
        governance_model_call_id="model_call:test-governance",
        decision_index=0,
        allowed_candidate_entry_ids=frozenset({candidate.entry_id}),
        allowed_scopes=frozenset({"ctx:user"}),
    )


def test_in_memory_runtime_wiring_persists_run_timeline(tmp_path) -> None:
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        wiring = build_in_memory_runtime_wiring(
            tmp_path,
            runtime_session_id=f"runtime:test:{uuid4().hex}",
        )
    ctx = _event_context("in-memory-wiring")

    asyncio.run(_emit_timeline_events(wiring.runtime_session, ctx, "hello wiring"))

    timeline = load_run_timeline(
        graph=wiring.graph,
        archive=wiring.archive,
        run_id=ctx.run_id,
        runtime_session_id=wiring.runtime_session.runtime_session_id,
        graph_id=wiring.graph_id,
    )
    summary = summarize_run_timeline(timeline)

    assert wiring.event_log is wiring.runtime_session.event_log
    assert wiring.graph_id is None
    assert isinstance(
        wiring.memory_governance_executor.memory_write_uow_factory(),
        InMemoryMemoryWriteUnitOfWork,
    )
    assert summary.assistant_text == "hello wiring"
    assert summary.status == "completed"


def test_governance_events_from_runtime_wiring_do_not_block_next_emit(tmp_path) -> None:
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        wiring = build_in_memory_runtime_wiring(
            tmp_path,
            runtime_session_id=f"runtime:test:{uuid4().hex}",
        )
    runtime = wiring.runtime_session
    source_ctx = _event_context("governance-publisher-gap")
    candidate = wiring.candidate_pool.append_candidate(
        PooledMemoryCandidate(
            payload=ValidCandidatePayload(
                candidate=PreferenceCandidate(
                    candidate_id=f"candidate:test:{uuid4().hex}",
                    statement="The user dislikes egg tarts.",
                    scope="ctx:user",
                    source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                    verification_status=memory.VerificationStatus.USER_CONFIRMED,
                )
            ),
            origin=CandidateOrigin.REFLECTION,
            source_session_id=runtime.runtime_session_id,
            source_run_id=source_ctx.run_id,
            source_turn_id=source_ctx.turn_id,
            source_reply_id=source_ctx.reply_id,
            user_quote="我特别讨厌吃蛋挞",
        )
    )

    async def run() -> None:
        open_test_root_rollout_run(
            runtime,
            event_context=source_ctx,
            model_target=test_resolved_target_fact(),
        )
        runtime._adopt_unbootstrapped_in_memory_account_for_test(  # noqa: SLF001
            incoming_run_id=source_ctx.run_id
        )
        source = await runtime.emit(
            make_text_block_segment_event(
                **source_ctx.event_fields(), block_id="text:1", delta="bind"
            )
        )
        result = wiring.memory_governance_executor.apply_decision(
            SubmitAsIsDecision(
                target_entry_id=candidate.entry_id,
                reason="Explicit durable preference.",
            ),
            governance_batch_id=f"governance:test:{uuid4().hex}",
            execution_identity=_governance_execution_identity(candidate),
        )
        governance_sequences = tuple(event.sequence for event in result.events)
        assert all(sequence is not None for sequence in governance_sequences)
        assert governance_sequences == tuple(
            range(governance_sequences[0], governance_sequences[0] + 2)  # type: ignore[arg-type]
        )
        assert source.sequence is not None
        assert governance_sequences[0] > source.sequence  # type: ignore[operator]

        final = await asyncio.wait_for(
            runtime.emit(
                make_text_block_segment_event(
                    **source_ctx.event_fields(), block_id="text:2", delta="after"
                )
            ),
            timeout=0.5,
        )
        assert final.sequence is not None
        assert final.sequence > governance_sequences[-1]  # type: ignore[operator]
        durable_account = runtime.event_log.read_materialization_account_state()
        assert durable_account is not None
        assert durable_account.ledger_through_sequence == (
            runtime.event_log.next_sequence() - 1
        )
        assert runtime.materialization_account_store.snapshot() == durable_account

    asyncio.run(run())


def test_governance_apply_runs_off_host_event_loop(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        wiring = build_in_memory_runtime_wiring(
            tmp_path,
            runtime_session_id=f"runtime:test:{uuid4().hex}",
        )
    executor = wiring.memory_governance_executor
    candidate = wiring.candidate_pool.append_candidate(
        PooledMemoryCandidate(
            payload=ValidCandidatePayload(
                candidate=PreferenceCandidate(
                    candidate_id=f"candidate:test:{uuid4().hex}",
                    statement="The user prefers non-blocking governance.",
                    scope="ctx:user",
                    source_authority=memory.SourceAuthority.EXPLICIT_USER_INSTRUCTION,
                    verification_status=memory.VerificationStatus.USER_CONFIRMED,
                )
            ),
            origin=CandidateOrigin.REFLECTION,
            source_session_id=wiring.runtime_session.runtime_session_id,
            source_run_id="run:test:governance-worker",
            source_turn_id="turn:test:governance-worker",
            source_reply_id="reply:test:governance-worker",
        )
    )
    started = Event()
    release = Event()
    executor_type = type(executor)
    original_apply = executor_type.apply_decision

    def blocking_apply(self, decision, **kwargs):
        started.set()
        release.wait(timeout=2.0)
        return original_apply(self, decision, **kwargs)

    monkeypatch.setattr(executor_type, "apply_decision", blocking_apply)

    async def run() -> None:
        fallback_release = Timer(0.5, release.set)
        fallback_release.daemon = True
        fallback_release.start()
        before = monotonic()
        task = asyncio.create_task(
            executor.apply_decision_async(
                SubmitAsIsDecision(
                    target_entry_id=candidate.entry_id,
                    reason="Verify auxiliary ownership.",
                ),
                execution_identity=_governance_execution_identity(candidate),
            )
        )
        try:
            for _ in range(50):
                if started.is_set():
                    break
                await asyncio.sleep(0.002)
            assert started.is_set()
            await asyncio.sleep(0)
            assert monotonic() - before < 0.25
            release.set()
            result = await task
            assert result.events
        finally:
            release.set()
            fallback_release.cancel()

    asyncio.run(run())


def test_agent_runtime_wiring_uses_in_memory_runtime_wiring_without_external_services(
    tmp_path,
) -> None:
    runtime_session_id = f"runtime:test:{uuid4().hex}"
    graph_id = f"graph:test/{uuid4().hex}"
    settings = _settings_for_storage(compatibility_storage_config())
    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=False,
        model_role=ModelRole.FLASH,
        options=LLMOptions(),
        system_prompt="test prompt",
        runtime_session_id=runtime_session_id,
        graph_id=graph_id,
    )

    assert isinstance(wiring, AgentRuntimeWiring)
    assert wiring.agent_runtime.runtime_session is wiring.runtime_wiring.runtime_session
    assert (
        wiring.runtime_wiring.runtime_session.runtime_session_id == runtime_session_id
    )
    assert wiring.runtime_wiring.graph_id == graph_id
    assert wiring.agent_runtime.model_role.name == "FLASH"
    assert wiring.agent_runtime.options == LLMOptions()
    assert wiring.agent_runtime.system_prompt == "test prompt"
    assert [
        type(provider) for provider in wiring.agent_runtime.capability_runtime.providers
    ] == [
        BuiltinToolCapabilityProvider,
        LocalSkillCapabilityProvider,
    ]
    assert wiring.agent_runtime.workspace_kind == "transient"
    assert (
        wiring.agent_runtime.permission_policy.profile is PermissionProfile.TRUSTED_HOST
    )
    assert wiring.agent_runtime.permission_policy.approval is ApprovalPolicy.NEVER
    assert wiring.agent_runtime.permission_policy.terminal is TerminalAccess.ALLOW
    assert "terminal" in wiring.agent_runtime.tool_executor.registry.names()


def test_default_capability_runtime_uses_terminal_path_for_active_skill_health(
    tmp_path,
) -> None:
    skill_dir = tmp_path / ".agents" / "skills" / "terminal-cli"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: terminal-cli
description: Requires a CLI that is only visible through terminal PATH.
required_binaries: [terminal-only, missing-cli]
---
# Terminal CLI
""",
        encoding="utf-8",
    )
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    executable = bin_dir / "terminal-only"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    settings = _settings_for_storage(compatibility_storage_config())
    with pytest.warns(DeprecationWarning, match="compatibility/test-only"):
        wiring = build_agent_runtime_wiring(
            settings,
            tmp_path,
            durable=False,
            model_role=ModelRole.FLASH,
        )

    exposure = preview_capability_plan(
        wiring.agent_runtime.capability_runtime,
        workspace_root=tmp_path,
        workspace_kind="transient",
        memory_domain=wiring.agent_runtime.memory_domain,
        tool_registry=wiring.agent_runtime.tool_executor.registry,
        archive=wiring.agent_runtime.runtime_session.archive,
        runtime_session_id=wiring.agent_runtime.runtime_session.runtime_session_id,
        mcp_installation_id=wiring.agent_runtime.runtime_session.mcp_installation_id,
        user_input="$terminal-cli",
    )

    missing = [
        diagnostic
        for diagnostic in exposure.diagnostics
        if diagnostic.code == "skill_required_binary_missing"
    ]
    assert [diagnostic.message for diagnostic in missing] == [
        "Active skill requires CLI binary not found on terminal PATH: missing-cli"
    ]


def test_in_memory_runtime_wiring_uses_domain_graph_and_write_scopes(tmp_path) -> None:
    project_root = tmp_path / "repo_test"
    domain = MemoryDomainContext(
        memory_domain_id="u_test",
        workspace_kind="project",
        stable_project_key=str(project_root),
    )

    wiring = build_in_memory_runtime_wiring(
        tmp_path,
        runtime_session_id=f"runtime:test:{uuid4().hex}",
        memory_domain=domain,
    )

    assert wiring.graph_id == "graph:user/u_test"
    assert wiring.memory_governance_executor.allowed_write_scopes == frozenset(
        {"ctx:user", workspace_scope(str(project_root))}
    )


def test_agent_runtime_wiring_threads_memory_domain_to_capability_context(
    tmp_path,
) -> None:
    project_root = tmp_path / "repo_test"
    domain = MemoryDomainContext(
        memory_domain_id="u_test",
        workspace_kind="project",
        stable_project_key=str(project_root),
    )
    settings = _settings_for_storage(compatibility_storage_config())

    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=False,
        model_role=ModelRole.FLASH,
        memory_domain=domain,
        enable_workspace_skills=False,
    )

    assert wiring.agent_runtime.memory_domain == domain
    assert wiring.agent_runtime.workspace_kind == "project"
    # workspace_kind no longer influences the default; project gets bypass like everything else.
    assert (
        wiring.agent_runtime.permission_policy.profile is PermissionProfile.TRUSTED_HOST
    )
    assert wiring.agent_runtime.permission_policy.approval is ApprovalPolicy.NEVER
    assert wiring.agent_runtime.permission_policy.terminal is TerminalAccess.ALLOW


def test_agent_runtime_wiring_threads_permission_policy_to_session_registry(
    tmp_path,
) -> None:
    settings = _settings_for_storage(compatibility_storage_config())
    policy = EffectivePermissionPolicy(
        profile=PermissionProfile.READ_ONLY,
        approval=ApprovalPolicy.ON_REQUEST,
        terminal=TerminalAccess.OFF,
    )

    wiring = build_agent_runtime_wiring(
        settings,
        tmp_path,
        durable=False,
        model_role=ModelRole.FLASH,
        permission_policy=policy,
    )

    assert wiring.agent_runtime.permission_policy is policy
    assert "read_file" in wiring.agent_runtime.tool_executor.registry.names()
    # Visible-but-blocked: gate is the sole authority; tools stay registered
    # under read-only and are denied at call time, not hidden from the registry.
    assert "write_file" in wiring.agent_runtime.tool_executor.registry.names()
    assert "terminal" in wiring.agent_runtime.tool_executor.registry.names()


def test_in_memory_runtime_wiring_rejects_user_graph_without_domain(tmp_path) -> None:
    with pytest.raises(ValueError, match="graph:user"):
        build_in_memory_runtime_wiring(
            tmp_path,
            runtime_session_id=f"runtime:test:{uuid4().hex}",
            graph_id="graph:user/u_test",
        )


def test_durable_runtime_wiring_rejects_user_graph_without_domain(tmp_path) -> None:
    storage = compatibility_storage_config()

    with pytest.raises(ValueError, match="graph:user"):
        build_durable_runtime_wiring(
            _settings_for_storage(storage),
            tmp_path,
            runtime_session_id=f"runtime:test:{uuid4().hex}",
            graph_id="graph:user/u_test",
        )


async def _emit_timeline_events(runtime_session, ctx: EventContext, text: str) -> None:
    await emit_test_accepted_model_reply(
        runtime_session,
        event_context=ctx,
        assistant_text=text,
    )


def _event_context(label: str) -> EventContext:
    return EventContext(
        run_id=f"run:{label}:{uuid4().hex}",
        turn_id=f"turn:{label}:{uuid4().hex}",
        reply_id=f"reply:{label}:{uuid4().hex}",
    )


def _settings_for_storage(storage: StorageConfig) -> PulsaraSettings:
    return PulsaraSettings(
        llm=test_llm_config(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
        ),
        storage=storage,
    )
