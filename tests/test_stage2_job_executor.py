from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from threading import Event

import pytest

from pulsara_agent.conversation_kernel.contracts import (
    JobAttemptClaimGuard,
    JobSafetyClass,
)
from pulsara_agent.conversation_kernel.jobs import KernelDurableJobExecutor
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.job_model import DirectKernelJobModel
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    DEFAULT_KERNEL_WATCHDOG_POLICY,
    KernelExecutionDeadlineFactory,
    KernelExecutionWatchdogPolicy,
)
from pulsara_agent.conversation_kernel.repository import AcceptedJobAttempt
from pulsara_agent.llm.estimator import PulsaraHeuristicTokenEstimatorV1
from pulsara_agent.primitives.model_call import ModelCallPurpose
from tests.support.model_config import test_llm_config


class _SettlementRepository:
    def __init__(self) -> None:
        self.settled: list[dict[str, object]] = []

    def settle_job_attempt(self, guard, **kwargs):
        self.settled.append({"guard": guard, **kwargs})


def _attempt() -> AcceptedJobAttempt:
    return AcceptedJobAttempt(
        guard=JobAttemptClaimGuard(
            job_id="job:1",
            attempt_id="job-attempt:1",
            claim_generation=1,
            claim_owner_id="worker:1",
            origin_session_id="session:1",
        ),
        attempt_ordinal=1,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        handler_type="TEST_HANDLER",
        safety_class=JobSafetyClass.RETRY_SAFE,
        intent_payload={},
    )


def test_stage2_job_executor_close_joins_active_handler_and_settles_attempt() -> None:
    async def exercise() -> None:
        repository = _SettlementRepository()
        started = asyncio.Event()

        async def handler(attempt: AcceptedJobAttempt) -> None:
            del attempt
            started.set()
            await asyncio.Event().wait()

        executor = object.__new__(KernelDurableJobExecutor)
        executor._repository = repository  # type: ignore[attr-defined]
        executor._handlers = {"TEST_HANDLER": handler}  # type: ignore[attr-defined]
        executor._stopping = asyncio.Event()  # type: ignore[attr-defined]
        executor._active = set()  # type: ignore[attr-defined]
        executor._embedding = None  # type: ignore[attr-defined]
        executor._owns_embedding = False  # type: ignore[attr-defined]
        executor._io = KernelSessionIO()  # type: ignore[attr-defined]

        async def polling_owner() -> None:
            await executor._stopping.wait()  # type: ignore[attr-defined]

        executor._task = asyncio.create_task(polling_owner())  # type: ignore[attr-defined]
        operation = asyncio.create_task(executor._execute(_attempt()))
        executor._active.add(operation)  # type: ignore[attr-defined]
        await asyncio.wait_for(started.wait(), timeout=1)
        await executor.aclose(timeout_seconds=1)
        assert operation.done()
        with pytest.raises(asyncio.CancelledError):
            operation.result()
        assert len(repository.settled) == 1
        assert repository.settled[0]["terminal_status"] == "FAILED"
        assert repository.settled[0]["error_code"] == "WORKER_STOPPED"
        assert repository.settled[0]["retryable"] is True

    asyncio.run(exercise())


def test_job_cancellation_settles_only_after_physical_thread_exits() -> None:
    async def exercise() -> None:
        repository = _SettlementRepository()
        started = Event()
        release = Event()

        def blocking_physical(*, deadline_monotonic: float) -> None:
            assert deadline_monotonic > 0
            started.set()
            release.wait()

        executor = object.__new__(KernelDurableJobExecutor)
        executor._repository = repository  # type: ignore[attr-defined]
        executor._io = KernelSessionIO()  # type: ignore[attr-defined]

        async def handler(_attempt: AcceptedJobAttempt) -> None:
            await executor._io.run(  # type: ignore[attr-defined]
                blocking_physical,
                deadline_monotonic=asyncio.get_running_loop().time() + 10,
            )

        executor._handlers = {"TEST_HANDLER": handler}  # type: ignore[attr-defined]
        operation = asyncio.create_task(executor._execute(_attempt()))
        assert await asyncio.to_thread(started.wait, 1)
        operation.cancel()
        await asyncio.sleep(0.05)
        assert repository.settled == []

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert len(repository.settled) == 1
        assert repository.settled[0]["error_code"] == "WORKER_STOPPED"
        await executor._io.aclose(  # type: ignore[attr-defined]
            deadline_monotonic=asyncio.get_running_loop().time() + 1
        )

    asyncio.run(exercise())


def test_job_model_prepares_the_final_context_with_target_token_estimator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pulsara_agent.conversation_kernel.job_model as job_model

    target = SimpleNamespace(
        fact=SimpleNamespace(target_fingerprint="sha256:" + "2" * 64),
        token_estimator=PulsaraHeuristicTokenEstimatorV1(),
    )
    monkeypatch.setattr(job_model, "resolve_model_target", lambda **_: target)
    monkeypatch.setattr(
        job_model,
        "resolve_model_call",
        lambda **_: SimpleNamespace(
            resolved_model_call_id="model-call:job", target=target
        ),
    )
    monkeypatch.setattr(
        job_model,
        "validate_model_context_for_call",
        lambda *, call, context: SimpleNamespace(
            estimate=call.target.token_estimator.estimate_context(context)
        ),
    )
    model = DirectKernelJobModel(
        test_llm_config(
            api_key="test",
            base_url="https://example.invalid/v1",
            pro_model="test-pro",
            flash_model="test-flash",
            api="openai_chat_completions",
        )
    )
    prepared = model.prepare_json_call(
        purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY,
        prompt="x",
        maximum_input_tokens=4096,
        maximum_output_tokens=64,
        timeout_policy=(
            DEFAULT_KERNEL_WATCHDOG_POLICY.durable_job_transport(45.0)
        ),
    )
    assert prepared.estimated_input_tokens > len(b"x") // 4
    assert (
        prepared.context.compiler_estimated_input_tokens
        == prepared.estimated_input_tokens
    )

    with pytest.raises(ValueError, match="requires an attempt total timeout"):
        model.prepare_json_call(
            purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY,
            prompt="x",
            maximum_input_tokens=4096,
            maximum_output_tokens=64,
            timeout_policy=DEFAULT_KERNEL_WATCHDOG_POLICY.foreground_transport,
        )


def test_job_provider_admission_is_installed_before_the_only_physical_call() -> None:
    async def exercise() -> None:
        order: list[str] = []

        class Repository:
            def mark_job_provider_call_started(
                self,
                _guard,
                *,
                input_tokens,
                requested_output_tokens,
                deadline_monotonic,
            ) -> None:
                assert input_tokens == 338
                assert requested_output_tokens == 64
                assert deadline_monotonic > 0
                order.append("admitted")

        class Model:
            def prepare_json_call(self, **kwargs):
                timeout = kwargs["timeout_policy"]
                assert timeout.connect_seconds == 7.0
                assert timeout.write_seconds == 8.0
                assert timeout.pool_seconds == 9.0
                assert timeout.read_idle_seconds == 11.0
                order.append("prepared")
                return SimpleNamespace(estimated_input_tokens=338)

            async def complete_prepared_json(self, _prepared):
                assert order == ["prepared", "admitted"]
                order.append("sent")
                return {"ok": True}

        executor = object.__new__(KernelDurableJobExecutor)
        executor._repository = Repository()  # type: ignore[attr-defined]
        executor._model = Model()  # type: ignore[attr-defined]
        executor._io = KernelSessionIO()  # type: ignore[attr-defined]
        executor._deadlines = KernelExecutionDeadlineFactory(  # type: ignore[attr-defined]
            KernelExecutionWatchdogPolicy(
                provider_connect_seconds=7.0,
                provider_write_seconds=8.0,
                provider_pool_seconds=9.0,
                provider_stream_idle_seconds=11.0,
            )
        )
        attempt = replace(
            _attempt(),
            provider_input_token_limit=4096,
            provider_output_token_limit=64,
        )
        result = await executor._provider_json(
            attempt,
            purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY,
            prompt="x",
        )
        assert result == {"ok": True}
        assert order == ["prepared", "admitted", "sent"]
        await executor._io.aclose(  # type: ignore[attr-defined]
            deadline_monotonic=asyncio.get_running_loop().time() + 1
        )

    asyncio.run(exercise())
