from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from threading import Event
from time import monotonic

import pytest

from pulsara_agent.capability.runtime import CapabilityRuntime
from pulsara_agent.memory.artifacts.archive import InMemoryArchiveStore
from pulsara_agent.memory.foundation.records import ArtifactContentConflict
from pulsara_agent.llm import LLMRuntime, ModelRole
from pulsara_agent.llm.registry import LLMTransportRegistry
from pulsara_agent.primitives.context import context_fingerprint
from pulsara_agent.primitives.long_horizon import (
    LongHorizonContextAllocationPolicyFact,
)
from pulsara_agent.runtime import AgentRuntime, LoopBudget
from pulsara_agent.runtime.long_horizon.rollup import (
    ObservationRollupRendererRegistry,
)
from pulsara_agent.runtime.context_input.compiler import (
    provider_neutral_payload_fingerprint,
)
from pulsara_agent.runtime.context_input.event_slice import ContextEventSlice
from pulsara_agent.runtime.context_input.manifest import (
    ContextInputManifestWriteService,
    PendingContextInputManifestWriteError,
    build_context_input_manifest_candidate,
)
from pulsara_agent.runtime.context_input.replay import (
    ContextInputReplayError,
    ContextInputReplayStatus,
    load_context_input_manifest,
    replay_context_input,
    replay_compiled_context,
)
from pulsara_agent.runtime.context_input.snapshot import bind_context_invocation
from pulsara_agent.event import ContextCompiledEvent, EventContext
from tests.conftest import open_test_root_rollout_run
from tests.support import test_llm_config, test_model_limits
from tests.support.runtime_session import in_memory_runtime_session
from tests.test_agent_runtime_loop import (
    ScriptedTransport,
    make_llm_runtime,
    run_agent_task,
)


async def _candidate(tmp_path, monkeypatch):
    import pulsara_agent.runtime.agent as agent_module

    captured = []
    original = agent_module.prepare_live_context_snapshot

    async def capture(**kwargs):
        prepared = await original(**kwargs)
        captured.append(prepared)
        return prepared

    monkeypatch.setattr(agent_module, "prepare_live_context_snapshot", capture)
    agent = AgentRuntime(
        capability_runtime=CapabilityRuntime(),
        runtime_session=in_memory_runtime_session(tmp_path),
        llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
    )
    await run_agent_task(agent, "manifest me")
    prepared = captured[0]
    compiled = next(
        event
        for event in agent.runtime_session.event_log.iter()
        if isinstance(event, ContextCompiledEvent) and event.status == "compiled"
    )
    assert compiled.input_audit is not None
    manifest = load_context_input_manifest(
        audit=compiled.input_audit,
        archive=agent.runtime_session.archive,
    )
    prepared = replace(
        prepared,
        invocation=bind_context_invocation(
            fact=manifest.snapshot,
            resolved_call=prepared.invocation.resolved_call,
            materialized_tool_specs=prepared.invocation.materialized_tool_specs,
        ),
    )
    return build_context_input_manifest_candidate(manifest), prepared, agent


def test_context_input_manifest_service_stores_and_confirms_identical(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        candidate, _, _ = await _candidate(tmp_path, monkeypatch)
        archive = InMemoryArchiveStore()
        service = ContextInputManifestWriteService(archive=archive)
        first = await service.persist(
            candidate,
            deadline_monotonic=monotonic() + 2,
        )
        second = await service.persist(
            candidate,
            deadline_monotonic=monotonic() + 2,
        )
        assert first.outcome == "stored"
        assert second.outcome == "confirmed_existing"
        assert first.artifact_id == second.artifact_id == candidate.artifact_id
        await service.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


@dataclass(slots=True)
class _BlockingArchive:
    inner: InMemoryArchiveStore
    entered: Event
    release: Event

    def put_text_if_absent_or_confirm_identical(self, *args, **kwargs):
        self.entered.set()
        self.release.wait()
        return self.inner.put_text_if_absent_or_confirm_identical(*args, **kwargs)

    def get_info(self, *args, **kwargs):
        return self.inner.get_info(*args, **kwargs)

    def get_text(self, *args, **kwargs):
        return self.inner.get_text(*args, **kwargs)


@dataclass(slots=True)
class _BlockingFailingArchive(_BlockingArchive):
    def put_text_if_absent_or_confirm_identical(self, *args, **kwargs):
        self.entered.set()
        self.release.wait()
        raise RuntimeError("synthetic write failure")


def test_manifest_waiter_cancellation_does_not_cancel_physical_write(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        candidate, _, _ = await _candidate(tmp_path, monkeypatch)
        entered = Event()
        release = Event()
        archive = _BlockingArchive(InMemoryArchiveStore(), entered, release)
        service = ContextInputManifestWriteService(archive=archive)
        waiter = asyncio.create_task(
            service.persist(
                candidate,
                deadline_monotonic=monotonic() + 5,
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert service.inflight_operation_count() == 1
        release.set()
        await service.drain_pending(deadline_monotonic=monotonic() + 2)
        assert archive.inner.get_text(candidate.artifact_id) == (
            candidate.canonical_bytes.decode("utf-8")
        )
        await service.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


def test_manifest_timeout_retry_keeps_old_write_owned_until_final_confirmation(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        candidate, _, _ = await _candidate(tmp_path, monkeypatch)
        entered = Event()
        release = Event()
        archive = _BlockingArchive(InMemoryArchiveStore(), entered, release)
        service = ContextInputManifestWriteService(archive=archive)
        with pytest.raises(TimeoutError):
            await service.persist(
                candidate,
                deadline_monotonic=monotonic() + 0.02,
            )
        assert await asyncio.to_thread(entered.wait, 1)
        retry = asyncio.create_task(
            service.retry_confirmation(
                artifact_id=candidate.artifact_id,
                expected_generation=1,
                deadline_monotonic=monotonic() + 2,
            )
        )
        await asyncio.sleep(0.05)
        assert not retry.done()
        assert service.inflight_operation_count() >= 1
        release.set()
        result = await retry
        assert result != "absent"
        assert result != "conflict"
        assert result.outcome == "confirmed_existing"
        await service.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


def test_manifest_provisional_absent_becomes_terminal_only_after_old_write_exits(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        candidate, _, _ = await _candidate(tmp_path, monkeypatch)
        entered = Event()
        release = Event()
        archive = _BlockingFailingArchive(InMemoryArchiveStore(), entered, release)
        service = ContextInputManifestWriteService(archive=archive)
        with pytest.raises(TimeoutError):
            await service.persist(
                candidate,
                deadline_monotonic=monotonic() + 0.02,
            )
        retry = asyncio.create_task(
            service.retry_confirmation(
                artifact_id=candidate.artifact_id,
                expected_generation=1,
                deadline_monotonic=monotonic() + 2,
            )
        )
        await asyncio.sleep(0.05)
        assert not retry.done()
        release.set()
        assert await retry == "absent"
        await service.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


def test_manifest_close_deadline_preserves_owner_then_retry_succeeds(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        candidate, _, _ = await _candidate(tmp_path, monkeypatch)
        entered = Event()
        release = Event()
        archive = _BlockingArchive(InMemoryArchiveStore(), entered, release)
        service = ContextInputManifestWriteService(archive=archive)
        persist = asyncio.create_task(
            service.persist(candidate, deadline_monotonic=monotonic() + 5)
        )
        assert await asyncio.to_thread(entered.wait, 1)
        with pytest.raises(PendingContextInputManifestWriteError):
            await service.aclose(deadline_monotonic=monotonic() + 0.02)
        assert service.pending_count() == 1
        assert service.inflight_operation_count() == 1
        release.set()
        assert (await persist).outcome == "stored"
        await service.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


def test_manifest_pending_cap_rejects_new_candidate_without_unbounded_retention(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        candidate, _, _ = await _candidate(tmp_path, monkeypatch)
        entered = Event()
        release = Event()
        archive = _BlockingArchive(InMemoryArchiveStore(), entered, release)
        service = ContextInputManifestWriteService(archive=archive, max_pending=1)
        first = asyncio.create_task(
            service.persist(candidate, deadline_monotonic=monotonic() + 5)
        )
        assert await asyncio.to_thread(entered.wait, 1)
        second = replace(
            candidate,
            artifact_id=candidate.artifact_id + ":other",
            context_id=candidate.context_id + ":other",
        )
        with pytest.raises(PendingContextInputManifestWriteError, match="max pending"):
            await service.persist(second, deadline_monotonic=monotonic() + 1)
        assert service.pending_count() == 1
        release.set()
        await first
        await service.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


@dataclass(slots=True)
class _CommitThenRaiseArchive:
    inner: InMemoryArchiveStore
    calls: int = 0

    def put_text_if_absent_or_confirm_identical(self, *args, **kwargs):
        self.calls += 1
        result = self.inner.put_text_if_absent_or_confirm_identical(*args, **kwargs)
        if self.calls == 1:
            raise RuntimeError("lost commit acknowledgement")
        return result

    def get_info(self, *args, **kwargs):
        return self.inner.get_info(*args, **kwargs)

    def get_text(self, *args, **kwargs):
        return self.inner.get_text(*args, **kwargs)


def test_manifest_commit_then_raise_is_confirmed_by_stable_identity(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        candidate, _, _ = await _candidate(tmp_path, monkeypatch)
        archive = _CommitThenRaiseArchive(InMemoryArchiveStore())
        service = ContextInputManifestWriteService(archive=archive)
        result = await service.persist(
            candidate,
            deadline_monotonic=monotonic() + 2,
        )
        assert result.outcome == "confirmed_existing"
        assert archive.calls == 1
        await service.aclose(deadline_monotonic=monotonic() + 2)

    asyncio.run(scenario())


def test_context_input_manifest_replays_same_snapshot_transcript_and_units(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        _, prepared, agent = await _candidate(tmp_path, monkeypatch)
        compiled = next(
            event
            for event in agent.runtime_session.event_log.iter()
            if isinstance(event, ContextCompiledEvent)
        )
        assert compiled.input_audit is not None
        audit = compiled.input_audit
        read = agent.runtime_session.event_log.read_raw_range_snapshot(
            minimum_sequence=audit.authority_from_sequence,
            through_sequence=audit.source_through_sequence,
        )
        event_slice = ContextEventSlice.from_read_snapshot(
            runtime_session_id=audit.source_runtime_session_id,
            minimum_sequence=audit.authority_from_sequence,
            snapshot=read,
        )
        replayed = replay_context_input(
            audit=audit,
            archive=agent.runtime_session.archive,
            event_log=agent.runtime_session.event_log,
            event_slice=event_slice,
        )
        assert replayed.manifest.snapshot == prepared.invocation.fact
        assert replayed.normalized_transcript == prepared.normalized_transcript
        assert replayed.prepared_tool_results.units == (
            prepared.prepared_tool_results.units
        )
        assert replayed.prepared_tool_results.resolved_policy == (
            prepared.prepared_tool_results.resolved_policy
        )
        assert replayed.prepared_tool_results.render_input_fingerprint == (
            prepared.prepared_tool_results.render_input_fingerprint
        )
        assert replayed.prepared_tool_results.cache_hints == ()
        assert replayed.prepared_candidates == prepared.prepared_candidates
        assert replayed.manifest.schema_version == "context-input-manifest:v6"
        frozen_projection = replayed.manifest.transcript_provider_projection

        def reject_timing_recomputation(**_kwargs):
            raise AssertionError("exact replay recomputed invocation timing")

        import pulsara_agent.runtime.context_input.compiler as compiler_module
        import pulsara_agent.runtime.context_input.provider_projection as projection_module
        import pulsara_agent.runtime.authority_materialization.transcript_restore as restore_module

        monkeypatch.setattr(
            compiler_module,
            "prepare_transcript_provider_projection",
            reject_timing_recomputation,
        )
        monkeypatch.setattr(
            projection_module,
            "_timing_header_text",
            reject_timing_recomputation,
        )
        monkeypatch.setattr(
            restore_module,
            "_checkpoint_candidates",
            reject_timing_recomputation,
        )
        monkeypatch.setattr(
            restore_module,
            "_latest_run_start",
            reject_timing_recomputation,
        )
        exact = replay_compiled_context(
            event=compiled,
            archive=agent.runtime_session.archive,
            event_log=agent.runtime_session.event_log,
            event_slice=event_slice,
        )
        assert exact.status is ContextInputReplayStatus.EXACT_REPLAY
        assert exact.inputs == replayed
        assert (
            exact.compiled_context.transcript_provider_projection
            == frozen_projection
        )
        transcript_sections = {
            section.id: section
            for section in exact.compiled_context.sections
            if section.id.startswith("transcript:")
        }
        assert tuple(transcript_sections) == tuple(
            section.section_id for section in frozen_projection.sections
        )
        for projection_section in frozen_projection.sections:
            timing = projection_section.semantic_identity.timing_semantic
            compiled_section = transcript_sections[projection_section.section_id]
            assert (
                compiled_section.metadata["timing_header_text"]
                == timing.rendered_timing_header
            )
            assert compiled_section.metadata["timing"]["age_seconds"] == (
                timing.age_seconds
            )
        assert (
            compiled.provider_neutral_payload_fingerprint
            == provider_neutral_payload_fingerprint(exact.compiled_context.llm_context)
        )

    asyncio.run(scenario())


def test_rollup_exact_replay_matches_live_payload(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        import pulsara_agent.runtime.long_horizon.run_contract as run_contract

        default_policy = run_contract.default_long_horizon_context_policy

        def low_projection_target_policy(
            *, input_budget_tokens: int
        ) -> LongHorizonContextAllocationPolicyFact:
            base = default_policy(input_budget_tokens=input_budget_tokens)
            payload = base.model_dump(
                mode="python",
                exclude={"policy_fingerprint"},
            )
            payload["tool_projection_soft_ratio_ppm"] = 70_000
            payload["tool_projection_post_rewrite_ratio_ppm"] = 50_000
            return LongHorizonContextAllocationPolicyFact(
                **payload,
                policy_fingerprint=context_fingerprint(
                    "long-horizon-context-allocation:v1",
                    payload,
                ),
            )

        monkeypatch.setattr(
            run_contract,
            "default_long_horizon_context_policy",
            low_projection_target_policy,
        )
        (tmp_path / "large.txt").write_text(("e" * 40 + "\n") * 2_000)
        transport = ScriptedTransport(
            [
                {
                    "tool_calls": [
                        {
                            "id": f"call:historical-read:{index}",
                            "name": "read_file",
                            "arguments": (
                                '{"path":"large.txt","offset":'
                                f"{((index - 1) * 150) + 1}"
                                ',"limit":150}'
                            ),
                        }
                        for index in range(1, 13)
                    ]
                },
                {"text": "historical reads complete"},
            ]
        )
        transport.api = "mock"
        limits = test_model_limits(
            total_context_tokens=64_000,
            max_input_tokens=64_000,
            max_output_tokens=4_096,
            default_output_tokens=2_048,
            input_safety_margin_tokens=4_096,
        )
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="mock",
            pro_limits=limits,
            flash_limits=limits,
        )
        registry = LLMTransportRegistry()
        registry.register(transport)
        runtime_session = in_memory_runtime_session(tmp_path)
        agent = AgentRuntime(
            capability_runtime=CapabilityRuntime(),
            runtime_session=runtime_session,
            llm_runtime=LLMRuntime(config=config, registry=registry),
        )

        result = await run_agent_task(agent, "Read twelve pages from the same large file")
        assert result.status.value == "finished", result.error_message
        compiled = next(
            event
            for event in reversed(
                runtime_session.event_log.iter(run_id=result.state.run_id)
            )
            if isinstance(event, ContextCompiledEvent)
            and event.status == "compiled"
        )
        assert compiled.input_audit is not None
        manifest = load_context_input_manifest(
            audit=compiled.input_audit,
            archive=runtime_session.archive,
        )
        assert len(manifest.prepared_rollup_units) == 1, (
            manifest.projection_state.total_projected_tokens,
            manifest.context_budget_decision,
            compiled.input_audit.tool_result_unit_count,
            tuple(
                (item.representation, item.protected_reason_codes)
                for item in manifest.projection_state.unit_projections
            ),
        )
        assert len(manifest.prepared_rollup_units[0].ordered_member_unit_ids) >= 2
        audit = compiled.input_audit
        event_slice = ContextEventSlice.from_read_snapshot(
            runtime_session_id=audit.source_runtime_session_id,
            minimum_sequence=audit.authority_from_sequence,
            snapshot=runtime_session.event_log.read_raw_range_snapshot(
                minimum_sequence=audit.authority_from_sequence,
                through_sequence=audit.source_through_sequence,
            ),
        )
        replayed = replay_compiled_context(
            event=compiled,
            archive=runtime_session.archive,
            event_log=runtime_session.event_log,
            event_slice=event_slice,
        )
        assert replayed.status is ContextInputReplayStatus.EXACT_REPLAY
        assert provider_neutral_payload_fingerprint(
            replayed.compiled_context.llm_context
        ) == compiled.provider_neutral_payload_fingerprint

        import pulsara_agent.runtime.context_input.replay as replay_module

        default_registry_factory = (
            replay_module.default_observation_rollup_renderer_registry
        )
        monkeypatch.setattr(
            replay_module,
            "default_observation_rollup_renderer_registry",
            ObservationRollupRendererRegistry,
        )
        with pytest.raises(ContextInputReplayError) as contract_mismatch:
            replay_compiled_context(
                event=compiled,
                archive=runtime_session.archive,
                event_log=runtime_session.event_log,
                event_slice=event_slice,
            )
        assert (
            contract_mismatch.value.status
            is ContextInputReplayStatus.CONTRACT_MISMATCH
        )
        assert (
            contract_mismatch.value.reason_code
            == "observation_rollup_contract_mismatch"
        )
        monkeypatch.setattr(
            replay_module,
            "default_observation_rollup_renderer_registry",
            default_registry_factory,
        )

        artifact_id = manifest.prepared_rollup_units[0].artifact_id
        runtime_session.archive.blobs.pop(artifact_id)
        with pytest.raises(ContextInputReplayError) as artifact_missing:
            replay_compiled_context(
                event=compiled,
                archive=runtime_session.archive,
                event_log=runtime_session.event_log,
                event_slice=event_slice,
            )
        assert artifact_missing.value.status is ContextInputReplayStatus.ARTIFACT_MISSING
        assert (
            artifact_missing.value.reason_code
            == "observation_rollup_artifact_missing"
        )

    asyncio.run(scenario())


def test_current_run_recent_protection_moves_to_latest_tool_batch(tmp_path) -> None:
    async def scenario() -> None:
        (tmp_path / "pages.txt").write_text(
            "\n".join(f"line-{index}" for index in range(1, 20))
        )

        def batch(name: str, offsets: range) -> dict[str, object]:
            return {
                "tool_calls": [
                    {
                        "id": f"call:{name}:{offset}",
                        "name": "read_file",
                        "arguments": (
                            '{"path":"pages.txt","offset":'
                            f"{offset}"
                            ',"limit":1}'
                        ),
                    }
                    for offset in offsets
                ]
            }

        transport = ScriptedTransport(
            [
                batch("first", range(1, 5)),
                batch("second", range(5, 9)),
                {"text": "done"},
            ]
        )
        registry = LLMTransportRegistry()
        registry.register(transport)
        limits = test_model_limits(
            total_context_tokens=64_000,
            max_input_tokens=64_000,
            max_output_tokens=512,
            default_output_tokens=128,
            input_safety_margin_tokens=1_024,
        )
        config = test_llm_config(
            api_key="sk-test",
            base_url="https://example.test/v1",
            pro_model="pro",
            flash_model="flash",
            api="scripted",
            pro_limits=limits,
            flash_limits=limits,
        )
        runtime_session = in_memory_runtime_session(tmp_path)
        agent = AgentRuntime(
            capability_runtime=CapabilityRuntime(),
            runtime_session=runtime_session,
            llm_runtime=LLMRuntime(config=config, registry=registry),
        )

        result = await run_agent_task(agent, "Read two batches")
        assert result.status.value == "finished", (
            result.error_message,
            tuple(
                (
                    type(event).__name__,
                    getattr(event, "code", None),
                    getattr(event, "message", None),
                )
                for event in runtime_session.event_log.iter(run_id=result.state.run_id)
                if type(event).__name__ == "RunErrorEvent"
            ),
        )
        compiled = next(
            event
            for event in reversed(
                runtime_session.event_log.iter(run_id=result.state.run_id)
            )
            if isinstance(event, ContextCompiledEvent)
            and event.status == "compiled"
        )
        assert compiled.input_audit is not None
        manifest = load_context_input_manifest(
            audit=compiled.input_audit,
            archive=runtime_session.archive,
        )
        recent_call_ids = {
            projection.tool_call_id
            for projection in manifest.projection_state.unit_projections
            if "current_run_recent" in projection.protected_reason_codes
        }
        assert recent_call_ids == {
            f"call:second:{offset}" for offset in range(5, 9)
        }
        assert all(
            "current_run_recent" not in projection.protected_reason_codes
            for projection in manifest.projection_state.unit_projections
            if projection.tool_call_id.startswith("call:first:")
        )

    asyncio.run(scenario())


def test_live_and_replay_subagent_selection_use_same_semantic_source(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        _, prepared, agent = await _candidate(tmp_path, monkeypatch)
        compiled = next(
            event
            for event in agent.runtime_session.event_log.iter()
            if isinstance(event, ContextCompiledEvent)
        )
        assert compiled.input_audit is not None
        audit = compiled.input_audit
        read = agent.runtime_session.event_log.read_raw_range_snapshot(
            minimum_sequence=audit.authority_from_sequence,
            through_sequence=audit.source_through_sequence,
        )
        event_slice = ContextEventSlice.from_read_snapshot(
            runtime_session_id=audit.source_runtime_session_id,
            minimum_sequence=audit.authority_from_sequence,
            snapshot=read,
        )

        replayed = replay_context_input(
            audit=audit,
            archive=agent.runtime_session.archive,
            event_log=agent.runtime_session.event_log,
            event_slice=event_slice,
        )

        assert replayed.manifest.snapshot.subagent_graph_semantic_source == (
            prepared.invocation.fact.subagent_graph_semantic_source
        )
        assert replayed.manifest.snapshot.candidate_source_selections == (
            prepared.invocation.fact.candidate_source_selections
        )

    asyncio.run(scenario())


def test_replay_unknown_or_unsupported_graph_domain_event_is_contract_mismatch(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.long_horizon.checkpoint as checkpoint_module
    from pulsara_agent.runtime.long_horizon.reducer_contract import (
        SubagentGraphReducerContractMismatch,
    )

    async def scenario() -> None:
        _, _, agent = await _candidate(tmp_path, monkeypatch)
        compiled = next(
            event
            for event in agent.runtime_session.event_log.iter()
            if isinstance(event, ContextCompiledEvent)
        )
        assert compiled.input_audit is not None
        audit = compiled.input_audit
        read = agent.runtime_session.event_log.read_raw_range_snapshot(
            minimum_sequence=audit.authority_from_sequence,
            through_sequence=audit.source_through_sequence,
        )
        event_slice = ContextEventSlice.from_read_snapshot(
            runtime_session_id=audit.source_runtime_session_id,
            minimum_sequence=audit.authority_from_sequence,
            snapshot=read,
        )

        monkeypatch.setattr(
            checkpoint_module,
            "restore_subagent_graph_from_checkpoint",
            lambda **_kwargs: (_ for _ in ()).throw(
                SubagentGraphReducerContractMismatch(
                    "unsupported historical graph event"
                )
            ),
        )
        with pytest.raises(ContextInputReplayError) as raised:
            replay_context_input(
                audit=audit,
                archive=agent.runtime_session.archive,
                event_log=agent.runtime_session.event_log,
                event_slice=event_slice,
            )
        assert raised.value.status is ContextInputReplayStatus.CONTRACT_MISMATCH
        assert raised.value.reason_code == (
            "subagent_graph_reducer_contract_mismatch"
        )

    asyncio.run(scenario())


def test_context_input_replay_classifies_missing_manifest_artifact(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        _, _, agent = await _candidate(tmp_path, monkeypatch)
        compiled = next(
            event
            for event in agent.runtime_session.event_log.iter()
            if isinstance(event, ContextCompiledEvent)
        )
        assert compiled.input_audit is not None
        audit = compiled.input_audit
        read = agent.runtime_session.event_log.read_raw_range_snapshot(
            minimum_sequence=audit.authority_from_sequence,
            through_sequence=audit.source_through_sequence,
        )
        event_slice = ContextEventSlice.from_read_snapshot(
            runtime_session_id=audit.source_runtime_session_id,
            minimum_sequence=audit.authority_from_sequence,
            snapshot=read,
        )
        with pytest.raises(ContextInputReplayError) as raised:
                replay_context_input(
                    audit=audit,
                    archive=InMemoryArchiveStore(),
                    event_log=agent.runtime_session.event_log,
                    event_slice=event_slice,
            )
        assert raised.value.status is ContextInputReplayStatus.ARTIFACT_MISSING
        assert raised.value.reason_code == "context_input_manifest_missing"

    asyncio.run(scenario())


def test_context_input_replay_classifies_audit_contract_mismatch(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        _, _, agent = await _candidate(tmp_path, monkeypatch)
        compiled = next(
            event
            for event in agent.runtime_session.event_log.iter()
            if isinstance(event, ContextCompiledEvent)
        )
        assert compiled.input_audit is not None
        audit = compiled.input_audit.model_copy(
            update={"input_manifest_fingerprint": "sha256:synthetic-mismatch"}
        )
        read = agent.runtime_session.event_log.read_raw_range_snapshot(
            minimum_sequence=audit.authority_from_sequence,
            through_sequence=audit.source_through_sequence,
        )
        event_slice = ContextEventSlice.from_read_snapshot(
            runtime_session_id=audit.source_runtime_session_id,
            minimum_sequence=audit.authority_from_sequence,
            snapshot=read,
        )
        with pytest.raises(ContextInputReplayError) as raised:
                replay_context_input(
                    audit=audit,
                    archive=agent.runtime_session.archive,
                    event_log=agent.runtime_session.event_log,
                    event_slice=event_slice,
            )
        assert raised.value.status is ContextInputReplayStatus.CONTRACT_MISMATCH
        assert raised.value.reason_code == "context_input_manifest_audit_mismatch"

    asyncio.run(scenario())


def test_context_input_replay_classifies_wrong_authority_range_as_untrusted(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        _, _, agent = await _candidate(tmp_path, monkeypatch)
        compiled = next(
            event
            for event in agent.runtime_session.event_log.iter()
            if isinstance(event, ContextCompiledEvent)
        )
        assert compiled.input_audit is not None
        audit = compiled.input_audit
        read = agent.runtime_session.event_log.read_raw_range_snapshot(
            minimum_sequence=audit.authority_from_sequence,
            through_sequence=audit.source_through_sequence,
        )
        event_slice = ContextEventSlice.from_read_snapshot(
            runtime_session_id=audit.source_runtime_session_id,
            minimum_sequence=audit.authority_from_sequence,
            snapshot=read,
        )
        wrong_slice = event_slice.subslice(from_sequence=event_slice.from_sequence + 1)
        with pytest.raises(ContextInputReplayError) as raised:
                replay_context_input(
                    audit=audit,
                    archive=agent.runtime_session.archive,
                    event_log=agent.runtime_session.event_log,
                    event_slice=wrong_slice,
            )
        assert raised.value.status is ContextInputReplayStatus.LEDGER_UNTRUSTED
        assert raised.value.reason_code == "context_input_event_slice_untrusted"

    asyncio.run(scenario())


def test_exact_replay_rederives_selection_and_candidate_authority(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.runtime.context_input.replay as replay_module
    import pulsara_agent.runtime.context_input.snapshot as snapshot_module

    async def scenario() -> None:
        _, _, agent = await _candidate(tmp_path, monkeypatch)
        compiled = next(
            event
            for event in agent.runtime_session.event_log.iter()
            if isinstance(event, ContextCompiledEvent)
        )
        assert compiled.input_audit is not None
        audit = compiled.input_audit
        read = agent.runtime_session.event_log.read_raw_range_snapshot(
            minimum_sequence=audit.authority_from_sequence,
            through_sequence=audit.source_through_sequence,
        )
        event_slice = ContextEventSlice.from_read_snapshot(
            runtime_session_id=audit.source_runtime_session_id,
            minimum_sequence=audit.authority_from_sequence,
            snapshot=read,
        )

        original_selection_builder = (
            snapshot_module.build_context_candidate_source_selections
        )
        monkeypatch.setattr(
            snapshot_module,
            "build_context_candidate_source_selections",
            lambda **_kwargs: (),
        )
        with pytest.raises(ContextInputReplayError) as selection_error:
                replay_context_input(
                    audit=audit,
                    archive=agent.runtime_session.archive,
                    event_log=agent.runtime_session.event_log,
                    event_slice=event_slice,
            )
        assert selection_error.value.status is ContextInputReplayStatus.CONTRACT_MISMATCH
        assert selection_error.value.reason_code == (
            "context_input_candidate_selection_mismatch"
        )
        monkeypatch.setattr(
            snapshot_module,
            "build_context_candidate_source_selections",
            original_selection_builder,
        )

        monkeypatch.setattr(
            replay_module,
            "build_context_candidate_authorities",
            lambda **_kwargs: (),
        )
        with pytest.raises(ContextInputReplayError) as authority_error:
                replay_context_input(
                    audit=audit,
                    archive=agent.runtime_session.archive,
                    event_log=agent.runtime_session.event_log,
                    event_slice=event_slice,
            )
        assert authority_error.value.status is ContextInputReplayStatus.CONTRACT_MISMATCH
        assert authority_error.value.reason_code == (
            "context_input_candidate_authority_mismatch"
        )

    asyncio.run(scenario())


def test_inspector_reports_exact_only_after_payload_replay(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.inspector.service as inspector_service

    async def scenario() -> None:
        _, _, agent = await _candidate(tmp_path, monkeypatch)
        compiled = next(
            event
            for event in agent.runtime_session.event_log.iter()
            if isinstance(event, ContextCompiledEvent)
        )

        class _Store:
            dsn = "postgresql://unused"

            @staticmethod
            def events_for_session(runtime_session_id):
                assert runtime_session_id == agent.runtime_session.runtime_session_id
                return agent.runtime_session.event_log.iter()

            monkeypatch.setattr(
                inspector_service,
                "PostgresArtifactStore",
                lambda _dsn: agent.runtime_session.archive,
            )
            monkeypatch.setattr(
                inspector_service,
                "PostgresEventLog",
                lambda **_kwargs: agent.runtime_session.event_log,
            )
        projection = inspector_service._context_input_replay_projection(  # noqa: SLF001
            compiled,
            _Store(),
        )
        assert projection["status"] == "exact_replay"
        assert projection["diagnostics"] == []
        assert projection["snapshot"]["fact_fingerprint"] == (
            compiled.input_audit.snapshot_fact_fingerprint
        )
        selection = projection["candidates"]["source_selections"][0]
        assert selection["reason_code"] == "no_eligible_sources"
        assert selection["eligible_source_count"] == 0
        decision = next(
            item
            for item in projection["candidates"]["collection_decisions"]
            if item["source_kind"] == "subagent_results"
        )
        assert decision["reason_code"] == "no_eligible_sources"
        assert decision["omitted_source_count"] == 0

    asyncio.run(scenario())


def test_inspector_distinguishes_zero_cap_omitted_subagent_selection(
    tmp_path, monkeypatch
) -> None:
    import pulsara_agent.inspector.service as inspector_service

    async def scenario() -> None:
        runtime_session = in_memory_runtime_session(tmp_path)
        agent = AgentRuntime(
            capability_runtime=CapabilityRuntime(),
            runtime_session=runtime_session,
            llm_runtime=make_llm_runtime(ScriptedTransport([{"text": "done"}])),
            budget=LoopBudget(max_subagent_results_per_parent_compile=0),
        )
        assert agent.subagent_runtime is not None
        seed_context = EventContext(
            run_id="run:inspector-selection-seed",
            turn_id="turn:inspector-selection-seed",
            reply_id="reply:inspector-selection-seed",
        )
        open_test_root_rollout_run(
            runtime_session,
            event_context=seed_context,
            model_target=agent.llm_runtime.resolve_target(
                role=ModelRole.PRO
            ).fact,
        )
        seeded = await agent.subagent_runtime.spawn_fake(
            task="inspector omitted result",
            event_context=seed_context,
        )
        await agent.subagent_runtime.complete_fake(
            seeded.subagent_run_id,
            summary="omitted by zero cap",
            event_context=seed_context,
        )
        result = await run_agent_task(agent, "inspect omitted selection")
        assert result.status.value == "finished"
        compiled = next(
            event
            for event in runtime_session.event_log.iter(run_id=result.state.run_id)
            if isinstance(event, ContextCompiledEvent)
        )

        class _Store:
            dsn = "postgresql://unused"

            @staticmethod
            def events_for_session(runtime_session_id):
                assert runtime_session_id == runtime_session.runtime_session_id
                return runtime_session.event_log.iter()

            monkeypatch.setattr(
                inspector_service,
                "PostgresArtifactStore",
                lambda _dsn: runtime_session.archive,
            )
            monkeypatch.setattr(
                inspector_service,
                "PostgresEventLog",
                lambda **_kwargs: runtime_session.event_log,
            )
        projection = inspector_service._context_input_replay_projection(  # noqa: SLF001
            compiled,
            _Store(),
        )
        assert projection["status"] == "exact_replay"
        selection = projection["candidates"]["source_selections"][0]
        assert selection["reason_code"] == "policy_limit"
        assert selection["eligible_source_count"] == 1
        assert selection["selected_source_ids"] == []
        assert selection["omitted_source_count"] == 1
        decision = next(
            item
            for item in projection["candidates"]["collection_decisions"]
            if item["source_kind"] == "subagent_results"
        )
        assert decision["reason_code"] == "policy_limit"
        assert decision["selected_source_ids"] == []
        assert decision["omitted_source_count"] == 1

    asyncio.run(scenario())


def test_exact_replay_rejects_provider_neutral_payload_drift(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        _, _, agent = await _candidate(tmp_path, monkeypatch)
        compiled = next(
            event
            for event in agent.runtime_session.event_log.iter()
            if isinstance(event, ContextCompiledEvent)
        )
        assert compiled.input_audit is not None
        read = agent.runtime_session.event_log.read_raw_range_snapshot(
            minimum_sequence=compiled.input_audit.authority_from_sequence,
            through_sequence=compiled.input_audit.source_through_sequence,
        )
        event_slice = ContextEventSlice.from_read_snapshot(
            runtime_session_id=compiled.input_audit.source_runtime_session_id,
            minimum_sequence=compiled.input_audit.authority_from_sequence,
            snapshot=read,
        )
        drifted = compiled.model_copy(
            update={"provider_neutral_payload_fingerprint": "sha256:" + "f" * 64}
        )
        with pytest.raises(ContextInputReplayError) as raised:
                replay_compiled_context(
                    event=drifted,
                    archive=agent.runtime_session.archive,
                    event_log=agent.runtime_session.event_log,
                    event_slice=event_slice,
            )
        assert raised.value.status is ContextInputReplayStatus.CONTRACT_MISMATCH
        assert raised.value.reason_code == "compiled_context_payload_mismatch"

    asyncio.run(scenario())


@dataclass(slots=True)
class _ConflictingManifestArchive:
    inner: InMemoryArchiveStore

    def put_text_if_absent_or_confirm_identical(self, blob_id, *args, **kwargs):
        if blob_id.startswith("context-input-manifest:"):
            raise ArtifactContentConflict("synthetic manifest conflict")
        return self.inner.put_text_if_absent_or_confirm_identical(
            blob_id, *args, **kwargs
        )

    def get_info(self, *args, **kwargs):
        return self.inner.get_info(*args, **kwargs)

    def get_text(self, *args, **kwargs):
        return self.inner.get_text(*args, **kwargs)


def test_manifest_conflict_emits_input_failure_then_latches_session(tmp_path) -> None:
    async def scenario() -> None:
        runtime_session = in_memory_runtime_session(tmp_path)
        runtime_session.context_input_manifest_service._archive = (  # noqa: SLF001
            _ConflictingManifestArchive(runtime_session.archive)
        )
        transport = ScriptedTransport([{"text": "must not run"}])
        agent = AgentRuntime(
            capability_runtime=CapabilityRuntime(),
            runtime_session=runtime_session,
            llm_runtime=make_llm_runtime(transport),
        )
        result = await run_agent_task(agent, "manifest conflict")
        compiled = [
            event
            for event in runtime_session.event_log.iter()
            if isinstance(event, ContextCompiledEvent)
        ]
        assert result.status.value == "failed"
        assert transport.contexts == []
        assert len(compiled) == 1
        assert compiled[0].status == "failed"
        assert compiled[0].input_audit is None
        assert compiled[0].input_failure is not None
        assert compiled[0].input_failure.manifest_write_outcome == "conflict"
        assert runtime_session.reconciliation_required is True

    asyncio.run(scenario())
