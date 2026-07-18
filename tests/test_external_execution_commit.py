from __future__ import annotations

import asyncio

import pytest

from pulsara_agent.capability.result_semantics import (
    build_default_tool_result_semantics_registry,
)
from pulsara_agent.event import (
    EventContext,
    ExternalExecutionResultEvent,
    RequireExternalExecutionEvent,
)
from pulsara_agent.event_log import InMemoryEventLog
from pulsara_agent.message import TextBlock, ToolResultBlock, ToolResultState
from pulsara_agent.primitives.authority_materialization import PhysicalOperationKind
from pulsara_agent.primitives.tool_observation import ToolObservationTimingFact
from pulsara_agent.primitives.tool_result import ToolResultRenderVariantCode
from pulsara_agent.runtime.context_input.external import (
    ExternalToolResultIngressBuilder,
    freeze_external_tool_result_submission,
)
from pulsara_agent.runtime.external_execution import (
    ExternalExecutionCommitContractError,
    ExternalExecutionCommitPort,
    ExternalExecutionResultCandidate,
)
from tests.conftest import (
    external_tool_call_requirement_fact,
    open_test_root_rollout_run,
)
from tests.support.model_call import test_resolved_target_fact
from tests.support.runtime_session import in_memory_runtime_session


def test_external_requirement_retains_physical_owner_until_exact_result(tmp_path) -> None:
    runtime_session_id = "runtime:test"
    context = EventContext(
        run_id="run:external-commit",
        turn_id="turn:external-commit",
        reply_id="reply:external-commit",
    )
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=runtime_session_id,
    )
    requirement_fact = external_tool_call_requirement_fact(
        "call:external-commit",
        tool_name="external_lookup",
    )
    requirement = RequireExternalExecutionEvent(
        id="require-external:commit",
        **context.event_fields(),
        external_tool_calls=(requirement_fact,),
    )
    port = ExternalExecutionCommitPort(runtime)

    async def scenario() -> None:
        open_test_root_rollout_run(
            runtime,
            event_context=context,
            model_target=test_resolved_target_fact(),
        )
        runtime._adopt_unbootstrapped_in_memory_account_for_test(
            incoming_run_id=context.run_id
        )
        await port.commit_requirement(requirement)
        stored_requirement = runtime.event_log.get_by_id(requirement.id)
        assert isinstance(stored_requirement, RequireExternalExecutionEvent)
        reservation = runtime.physical_reservation_for_owner(
            operation_kind=PhysicalOperationKind.EXTERNAL_EXECUTION,
            owner_id=requirement.id,
        )
        assert reservation is not None
        assert runtime.checkpoint_dispatch_barrier_coordinator.active_producer_count == 1

        submission = freeze_external_tool_result_submission(
            result_block=ToolResultBlock(
                id="call:external-commit",
                name="external_lookup",
                output=[TextBlock(text="done")],
                state=ToolResultState.SUCCESS,
            ),
            observation_timing=ToolObservationTimingFact(
                observed_at_utc="2026-07-15T00:00:00Z",
                tool_call_id="call:external-commit",
                tool_name="external_lookup",
                tool_origin="custom",
            ),
            selected_variant_code=(
                ToolResultRenderVariantCode.EXTERNAL_GENERIC_RESULT
            ),
            domain_result=None,
            terminal_payload_timing=None,
        )
        ingress = ExternalToolResultIngressBuilder(
            build_default_tool_result_semantics_registry()
        ).bind_submission(
            requirement_event=stored_requirement,
            requirement=requirement_fact,
            submission=submission,
            owner_runtime_session_id=runtime_session_id,
        )
        result = ExternalExecutionResultCandidate(
            id="external-result:commit",
            **context.event_fields(),
            external_results=(ingress,),
        )
        drain = runtime.checkpoint_dispatch_barrier_coordinator.begin_checkpoint_drain(
            checkpoint_id="checkpoint:external-result-drain",
            checkpoint_candidate_fingerprint="sha256:" + "d" * 64,
        )
        await port.commit_result(result)
        runtime.checkpoint_dispatch_barrier_coordinator.wait_until_drained(
            drain,
            deadline_monotonic=asyncio.get_running_loop().time() + 1,
        )
        runtime.checkpoint_dispatch_barrier_coordinator.abort_before_install(drain)

        stored_result = runtime.event_log.get_by_id(result.id)
        assert isinstance(stored_result, ExternalExecutionResultEvent)
        assert len(stored_result.terminal_projections) == 1
        projection_reference = (
            stored_result.terminal_projections[0].projection_reference
        )
        assert projection_reference.semantic_join.tool_call_id == (
            "call:external-commit"
        )
        assert runtime.transcript_projection_document_registry.resolve(
            projection_reference
        ).semantic_identity.execution_semantics == ingress.execution_semantics
        assert runtime.physical_reservation_for_owner(
            operation_kind=PhysicalOperationKind.EXTERNAL_EXECUTION,
            owner_id=requirement.id,
        ) is None
        assert runtime.checkpoint_dispatch_barrier_coordinator.active_producer_count == 0

    try:
        asyncio.run(scenario())
    finally:
        runtime.close()


def test_external_requirement_precommit_failure_releases_all_owners(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_session_id = "runtime:external-none"
    context = EventContext(
        run_id="run:external-none",
        turn_id="turn:external-none",
        reply_id="reply:external-none",
    )
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=runtime_session_id,
    )
    requirement = RequireExternalExecutionEvent(
        id="require-external:none",
        **context.event_fields(),
        external_tool_calls=(
            external_tool_call_requirement_fact(
                "call:external-none",
                tool_name="external_lookup",
            ),
        ),
    )

    async def scenario() -> None:
        open_test_root_rollout_run(
            runtime,
            event_context=context,
            model_target=test_resolved_target_fact(),
        )
        runtime._adopt_unbootstrapped_in_memory_account_for_test(
            incoming_run_id=context.run_id
        )

        def fail_before_commit(self, *args, **kwargs):
            raise RuntimeError("ledger unavailable")

        monkeypatch.setattr(
            InMemoryEventLog,
            "extend_with_materialization_state",
            fail_before_commit,
        )
        with pytest.raises(Exception, match="not committed"):
            await ExternalExecutionCommitPort(runtime).commit_requirement(requirement)
        assert runtime.event_log.get_by_id(requirement.id) is None
        assert runtime.physical_reservation_for_owner(
            operation_kind=PhysicalOperationKind.EXTERNAL_EXECUTION,
            owner_id=requirement.id,
        ) is None
        assert runtime.checkpoint_dispatch_barrier_coordinator.active_producer_count == 0

    try:
        asyncio.run(scenario())
    finally:
        runtime.close()


def test_external_requirement_commit_then_raise_confirms_one_owner(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_session_id = "runtime:external-confirm"
    context = EventContext(
        run_id="run:external-confirm",
        turn_id="turn:external-confirm",
        reply_id="reply:external-confirm",
    )
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=runtime_session_id,
    )
    requirement_fact = external_tool_call_requirement_fact(
        "call:external-confirm",
        tool_name="external_lookup",
    )
    requirement = RequireExternalExecutionEvent(
        id="require-external:confirm",
        **context.event_fields(),
        external_tool_calls=(requirement_fact,),
    )
    original = InMemoryEventLog.extend_with_materialization_state

    async def scenario() -> None:
        open_test_root_rollout_run(
            runtime,
            event_context=context,
            model_target=test_resolved_target_fact(),
        )
        runtime._adopt_unbootstrapped_in_memory_account_for_test(
            incoming_run_id=context.run_id
        )
        calls = 0

        def commit_then_raise(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            original(self, *args, **kwargs)
            raise RuntimeError("connection outcome unknown")

        monkeypatch.setattr(
            InMemoryEventLog,
            "extend_with_materialization_state",
            commit_then_raise,
        )
        await ExternalExecutionCommitPort(runtime).commit_requirement(requirement)
        assert calls == 1
        assert sum(event.id == requirement.id for event in runtime.event_log.iter()) == 1
        assert runtime.checkpoint_dispatch_barrier_coordinator.active_producer_count == 1

        monkeypatch.setattr(
            InMemoryEventLog,
            "extend_with_materialization_state",
            original,
        )
        stored = runtime.event_log.get_by_id(requirement.id)
        assert isinstance(stored, RequireExternalExecutionEvent)
        submission = freeze_external_tool_result_submission(
            result_block=ToolResultBlock(
                id="call:external-confirm",
                name="external_lookup",
                output=[TextBlock(text="done")],
                state=ToolResultState.SUCCESS,
            ),
            observation_timing=ToolObservationTimingFact(
                observed_at_utc="2026-07-15T00:00:00Z",
                tool_call_id="call:external-confirm",
                tool_name="external_lookup",
                tool_origin="custom",
            ),
            selected_variant_code=ToolResultRenderVariantCode.EXTERNAL_GENERIC_RESULT,
            domain_result=None,
            terminal_payload_timing=None,
        )
        ingress = ExternalToolResultIngressBuilder(
            build_default_tool_result_semantics_registry()
        ).bind_submission(
            requirement_event=stored,
            requirement=requirement_fact,
            submission=submission,
            owner_runtime_session_id=runtime_session_id,
        )
        await ExternalExecutionCommitPort(runtime).commit_result(
            ExternalExecutionResultCandidate(
                id="external-result:confirm",
                **context.event_fields(),
                external_results=(ingress,),
            )
        )

    try:
        asyncio.run(scenario())
    finally:
        runtime.close()


def test_restart_restores_external_reservation_and_operation_owner(tmp_path) -> None:
    runtime_session_id = "runtime:external-restart"
    context = EventContext(
        run_id="run:external-restart",
        turn_id="turn:external-restart",
        reply_id="reply:external-restart",
    )
    event_log = InMemoryEventLog(runtime_session_id=runtime_session_id)
    first = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=runtime_session_id,
        event_log=event_log,
    )
    requirement = RequireExternalExecutionEvent(
        id="require-external:restart",
        **context.event_fields(),
        external_tool_calls=(
            external_tool_call_requirement_fact(
                "call:external-restart",
                tool_name="external_lookup",
            ),
        ),
    )

    async def commit_requirement() -> None:
        open_test_root_rollout_run(
            first,
            event_context=context,
            model_target=test_resolved_target_fact(),
        )
        first._adopt_unbootstrapped_in_memory_account_for_test(
            incoming_run_id=context.run_id
        )
        await ExternalExecutionCommitPort(first).commit_requirement(requirement)

    asyncio.run(commit_requirement())
    second = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=runtime_session_id,
        event_log=event_log,
        archive=first.archive,
        tool_result_artifacts=first.tool_result_artifacts,
    )
    key = (PhysicalOperationKind.EXTERNAL_EXECUTION, requirement.id)
    try:
        restored = second.physical_reservation_for_owner(
            operation_kind=key[0],
            owner_id=key[1],
        )
        assert restored is not None
        assert second.checkpoint_dispatch_barrier_coordinator.active_producer_count == 1
        assert key in second._physical_operation_admission_tokens
    finally:
        restored_token = second._physical_operation_admission_tokens.pop(key)
        second.checkpoint_dispatch_barrier_coordinator.release_write_admission(
            restored_token
        )
        second._physical_reservation_facts.pop(key)
        second.close()
        original_token = first._physical_operation_admission_tokens.pop(key)
        first.checkpoint_dispatch_barrier_coordinator.release_write_admission(
            original_token
        )
        first._physical_reservation_facts.pop(key)
        first.close()


def test_external_result_reference_mismatch_preserves_reservation(tmp_path) -> None:
    runtime_session_id = "runtime:test"
    context = EventContext(
        run_id="run:external-mismatch",
        turn_id="turn:external-mismatch",
        reply_id="reply:external-mismatch",
    )
    runtime = in_memory_runtime_session(
        tmp_path,
        runtime_session_id=runtime_session_id,
    )
    requirement_fact = external_tool_call_requirement_fact(
        "call:external-mismatch",
        tool_name="external_lookup",
    )
    requirement = RequireExternalExecutionEvent(
        id="require-external:mismatch",
        **context.event_fields(),
        external_tool_calls=(requirement_fact,),
    )
    port = ExternalExecutionCommitPort(runtime)

    async def scenario() -> None:
        open_test_root_rollout_run(
            runtime,
            event_context=context,
            model_target=test_resolved_target_fact(),
        )
        runtime._adopt_unbootstrapped_in_memory_account_for_test(
            incoming_run_id=context.run_id
        )
        await port.commit_requirement(requirement)
        stored_requirement = runtime.event_log.get_by_id(requirement.id)
        assert isinstance(stored_requirement, RequireExternalExecutionEvent)
        submission = freeze_external_tool_result_submission(
            result_block=ToolResultBlock(
                id="call:external-mismatch",
                name="external_lookup",
                output=[TextBlock(text="done")],
                state=ToolResultState.SUCCESS,
            ),
            observation_timing=ToolObservationTimingFact(
                observed_at_utc="2026-07-15T00:00:00Z",
                tool_call_id="call:external-mismatch",
                tool_name="external_lookup",
                tool_origin="custom",
            ),
            selected_variant_code=(
                ToolResultRenderVariantCode.EXTERNAL_GENERIC_RESULT
            ),
            domain_result=None,
            terminal_payload_timing=None,
        )
        ingress = ExternalToolResultIngressBuilder(
            build_default_tool_result_semantics_registry()
        ).bind_submission(
            requirement_event=stored_requirement,
            requirement=requirement_fact,
            submission=submission,
            owner_runtime_session_id="runtime:wrong",
        )
        result = ExternalExecutionResultCandidate(
            id="external-result:mismatch",
            **context.event_fields(),
            external_results=(ingress,),
        )
        with pytest.raises(
            ExternalExecutionCommitContractError,
            match="reference drifted",
        ):
            await port.commit_result(result)
        assert runtime.physical_reservation_for_owner(
            operation_kind=PhysicalOperationKind.EXTERNAL_EXECUTION,
            owner_id=requirement.id,
        ) is not None
        valid_ingress = ExternalToolResultIngressBuilder(
            build_default_tool_result_semantics_registry()
        ).bind_submission(
            requirement_event=stored_requirement,
            requirement=requirement_fact,
            submission=submission,
            owner_runtime_session_id=runtime_session_id,
        )
        await port.commit_result(
            ExternalExecutionResultCandidate(
                id="external-result:mismatch-cleanup",
                **context.event_fields(),
                external_results=(valid_ingress,),
            )
        )

    try:
        asyncio.run(scenario())
    finally:
        runtime.close()
