"""Process-owned finite durable-job executor for the conversation kernel."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from time import monotonic
from typing import Mapping, Protocol
from uuid import uuid4

from pulsara_agent.conversation_kernel.job_model import DirectKernelJobModel
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.job_catalog import (
    BACKGROUND_COMPACTION,
    JOB_HANDLER_CATALOG,
    KernelJobHandlerContract,
)
from pulsara_agent.conversation_kernel.limits import STAGE2_LIMITS
from pulsara_agent.conversation_kernel.repository import (
    AcceptedJobAttempt,
    ConversationKernelConflict,
    ConversationKernelRepository,
    JobAttemptTerminalized,
    JobCancellationRequested,
    StaleJobClaim,
)
from pulsara_agent.llm.config import LLMConfig
from pulsara_agent.primitives.model_call import ModelCallPurpose


class KernelJobHandler(Protocol):
    async def __call__(self, attempt: AcceptedJobAttempt) -> None: ...


class KernelDurableJobExecutor:
    """One process owner; cancellation drains current physical handlers."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        llm_config: LLMConfig,
        poll_interval_seconds: float = 0.25,
        claim_lease_seconds: float = STAGE2_LIMITS.job_claim_lease_ms / 1000,
        maximum_concurrency: int = STAGE2_LIMITS.job_worker_default_concurrency,
        deadline_factory: KernelExecutionDeadlineFactory | None = None,
    ) -> None:
        if (
            poll_interval_seconds <= 0
            or claim_lease_seconds <= 0
            or not 1
            <= maximum_concurrency
            <= STAGE2_LIMITS.job_worker_hard_concurrency
        ):
            raise ValueError("job executor timing must be finite and positive")
        self._repository = repository
        self._deadlines = deadline_factory or KernelExecutionDeadlineFactory()
        self._model = DirectKernelJobModel(llm_config)
        self._poll = poll_interval_seconds
        self._lease = claim_lease_seconds
        self._maximum_concurrency = maximum_concurrency
        self._io = KernelSessionIO(maximum_concurrency=maximum_concurrency)
        self._owner = f"kernel-job-worker:{uuid4().hex}"
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._active: set[asyncio.Task[None]] = set()
        self._handlers: dict[str, KernelJobHandler] = {
            BACKGROUND_COMPACTION: self._background_compaction,
        }

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("job executor already started")
        self._task = asyncio.create_task(self._run(), name=self._owner)

    async def aclose(
        self,
        *,
        timeout_seconds: float | None = None,
        deadline_monotonic: float | None = None,
    ) -> None:
        if timeout_seconds is not None and deadline_monotonic is not None:
            raise ValueError("job executor close accepts one deadline authority")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("job executor close timeout must be positive")
        deadline = (
            deadline_monotonic
            if deadline_monotonic is not None
            else (
                monotonic() + timeout_seconds
                if timeout_seconds is not None
                else self._deadlines.deadline(
                    KernelWatchdogOwner.DURABLE_JOB_EXECUTOR_CLOSE
                )
            )
        )
        self._stopping.set()
        close_error: BaseException | None = None
        task = self._task
        if task is not None:
            try:
                if await _join_close_task(task, deadline_monotonic=deadline):
                    close_error = TimeoutError(
                        "durable job poll owner exited after close deadline"
                    )
            except BaseException as exc:
                close_error = exc
        active_snapshot = tuple(self._active)
        for operation in active_snapshot:
            operation.cancel()
        for operation in active_snapshot:
            try:
                if await _join_close_task(operation, deadline_monotonic=deadline):
                    close_error = close_error or TimeoutError(
                        "durable job handler exited after close deadline"
                    )
            except BaseException as exc:
                close_error = close_error or exc
        try:
            await self._io.aclose(deadline_monotonic=deadline)
        except BaseException as exc:
            close_error = close_error or exc
        if close_error is not None:
            raise close_error

    async def run_once(self) -> int:
        started = 0
        for contract in JOB_HANDLER_CATALOG:
            if len(self._active) >= self._maximum_concurrency:
                break
            candidate = await self._io.run(
                self._repository.prepare_job_claim_candidate,
                handler_type=contract.handler_type,
                deadline_monotonic=monotonic() + 5.0,
            )
            if candidate is None:
                continue
            try:
                attempt = await self._io.run(
                    self._repository.claim_due_job,
                    handler_type=contract.handler_type,
                    claim_owner_id=self._owner,
                    lease_seconds=self._lease,
                    expected_job_id=candidate,
                    deadline_monotonic=monotonic() + 5.0,
                )
            except Exception:
                attempt = await self._io.run(
                    self._repository.confirm_active_job_claim,
                    job_id=candidate,
                    handler_type=contract.handler_type,
                    claim_owner_id=self._owner,
                    deadline_monotonic=monotonic() + 5.0,
                )
            if attempt is None:
                continue
            task = asyncio.create_task(
                self._execute(attempt),
                name=f"{self._owner}:{attempt.guard.attempt_id}",
            )
            self._active.add(task)
            task.add_done_callback(self._active.discard)
            started += 1
        return started

    async def _run(self) -> None:
        while not self._stopping.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll)
            except TimeoutError:
                pass

    async def _execute(self, attempt: AcceptedJobAttempt) -> None:
        handler = self._handlers[attempt.handler_type]
        try:
            if attempt.cancel_requested:
                await self._settle_cancel(attempt)
                return
            remaining = max(
                0.001,
                (attempt.deadline_at - datetime.now(timezone.utc)).total_seconds(),
            )
            async with asyncio.timeout(remaining):
                await handler(attempt)
        except asyncio.CancelledError:
            # Process shutdown does not cancel the durable product intent.  A
            # RETRY_SAFE attempt is terminalized and the aggregate is allowed
            # to follow its finite retry policy.
            await asyncio.shield(
                self._settle_failure(
                    attempt, "FAILED", "WORKER_STOPPED", True
                )
            )
            raise
        except (StaleJobClaim, ConversationKernelConflict):
            return
        except JobAttemptTerminalized:
            return
        except JobCancellationRequested:
            await self._settle_cancel(attempt)
            return
        except BaseException as exc:
            await self._settle_failure(
                attempt,
                "FAILED",
                _closed_error_code(exc),
                True,
            )

    async def _provider_json(
        self,
        attempt: AcceptedJobAttempt,
        *,
        purpose: ModelCallPurpose,
        prompt: str,
    ) -> Mapping[str, object]:
        input_limit = attempt.provider_input_token_limit
        output_limit = attempt.provider_output_token_limit
        if input_limit is None or output_limit is None:
            raise ValueError("provider-backed job lacks finite attempt limits")
        remaining = (
            attempt.deadline_at - datetime.now(timezone.utc)
        ).total_seconds()
        timeout_policy = self._deadlines.policy.durable_job_transport(remaining)
        prepared = self._model.prepare_json_call(
            purpose=purpose,
            prompt=prompt,
            maximum_input_tokens=input_limit,
            maximum_output_tokens=output_limit,
            timeout_policy=timeout_policy,
        )
        await self._io.run(
            self._repository.mark_job_provider_call_started,
            attempt.guard,
            input_tokens=prepared.estimated_input_tokens,
            requested_output_tokens=output_limit,
            deadline_monotonic=_attempt_deadline(attempt),
        )
        return await self._model.complete_prepared_json(prepared)

    async def _background_compaction(self, attempt: AcceptedJobAttempt) -> None:
        intent = _intent(attempt)
        source = await self._io.run(
            self._repository.read_compaction_job_source,
            attempt.guard,
            deadline_monotonic=_attempt_deadline(attempt),
        )
        result = await self._provider_json(
            attempt,
            purpose=ModelCallPurpose.CONTEXT_COMPACTION_SUMMARY,
            prompt=(
                'Return only JSON {"summary": string}. Preserve durable facts, '
                "tool outcomes, unresolved ambiguity, and user intent.\n"
                + json.dumps(
                    {"intent": intent, "conversation": source},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            ),
        )
        summary = result.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("compaction provider omitted summary")
        await self._io.run(
            self._repository.accept_compaction_job_result,
            attempt.guard,
            summary=summary,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=_terminal_deadline(),
        )

    async def _settle_failure(
        self,
        attempt: AcceptedJobAttempt,
        status: str,
        code: str,
        retryable: bool,
    ) -> None:
        await self._io.run(
            self._repository.settle_job_attempt,
            attempt.guard,
            terminal_status=status,
            result_payload=None,
            error_code=code,
            retryable=retryable,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=_terminal_deadline(),
        )

    async def _settle_cancel(self, attempt: AcceptedJobAttempt) -> None:
        await self._io.run(
            self._repository.settle_job_attempt,
            attempt.guard,
            terminal_status="CANCELLED",
            result_payload=None,
            error_code="CANCEL_REQUESTED",
            retryable=False,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=_terminal_deadline(),
        )


async def _join_close_task(
    task: asyncio.Task[object], *, deadline_monotonic: float
) -> bool:
    """Return whether the close watchdog elapsed, after exact task join."""

    deadline_expired = False
    if not task.done():
        remaining = deadline_monotonic - monotonic()
        if remaining > 0:
            done, _pending = await asyncio.wait((task,), timeout=remaining)
            deadline_expired = not done
        else:
            deadline_expired = True
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancellation can detach a waiter, never the job close owner.
            continue
        except BaseException:
            break
    if not task.cancelled():
        task.result()
    return deadline_expired


def _intent(attempt: AcceptedJobAttempt) -> Mapping[str, object]:
    if attempt.intent_payload is None:
        raise ValueError("job attempt intent is missing")
    return dict(attempt.intent_payload)


def _closed_error_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "ATTEMPT_TIMEOUT"
    if isinstance(exc, ValueError):
        return "HANDLER_CONTRACT_MISMATCH"
    return "HANDLER_OPERATION_FAILED"


def _attempt_deadline(attempt: AcceptedJobAttempt) -> float:
    remaining = (attempt.deadline_at - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise TimeoutError("job attempt deadline expired")
    return monotonic() + remaining


def _terminal_deadline() -> float:
    return monotonic() + 5.0


__all__ = [
    "BACKGROUND_COMPACTION",
    "JOB_HANDLER_CATALOG",
    "KernelDurableJobExecutor",
    "KernelJobHandlerContract",
]
