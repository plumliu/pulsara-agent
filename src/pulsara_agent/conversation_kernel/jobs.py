"""Process-owned finite durable-job executor for the conversation kernel."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from time import monotonic
from typing import Mapping, Protocol
from uuid import uuid4

from pulsara_agent.conversation_kernel.job_model import DirectKernelJobModel
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.job_catalog import (
    BACKGROUND_COMPACTION,
    JOB_HANDLER_CATALOG,
    KernelJobHandlerContract,
    MEMORY_GOVERNANCE,
    MEMORY_INDEX_REFRESH,
    POST_COMPACTION_MEMORY_EXTRACTION,
)
from pulsara_agent.conversation_kernel.memory import MemoryIndexCoordinator
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
from pulsara_agent.retrieval.config import EmbeddingBackendConfig
from pulsara_agent.retrieval.embedding.factory import build_embedding_provider
from pulsara_agent.retrieval.embedding.protocol import EmbeddingProvider


class KernelJobHandler(Protocol):
    async def __call__(self, attempt: AcceptedJobAttempt) -> None: ...


class KernelDurableJobExecutor:
    """One process owner; cancellation drains current physical handlers."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        llm_config: LLMConfig,
        embedding_config: EmbeddingBackendConfig,
        embedding_provider: EmbeddingProvider | None = None,
        poll_interval_seconds: float = 0.25,
        claim_lease_seconds: float = STAGE2_LIMITS.job_claim_lease_ms / 1000,
        maximum_concurrency: int = STAGE2_LIMITS.job_worker_default_concurrency,
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
        self._model = DirectKernelJobModel(llm_config)
        self._embedding_config = embedding_config
        self._embedding = embedding_provider
        self._owns_embedding = embedding_provider is None
        self._index = MemoryIndexCoordinator(repository)
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
            POST_COMPACTION_MEMORY_EXTRACTION: self._memory_extraction,
            MEMORY_GOVERNANCE: self._memory_governance,
            MEMORY_INDEX_REFRESH: self._memory_index_refresh,
        }

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("job executor already started")
        self._task = asyncio.create_task(self._run(), name=self._owner)

    async def aclose(self, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("job executor close timeout must be positive")
        deadline = monotonic() + timeout_seconds
        self._stopping.set()
        task = self._task
        if task is not None:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=max(0.001, deadline - monotonic())
            )
        for operation in tuple(self._active):
            operation.cancel()
        while self._active:
            active = tuple(self._active)
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("durable job handlers did not physically exit")
            done, pending = await asyncio.wait(active, timeout=remaining)
            self._active.difference_update(done)
            for operation in done:
                if not operation.cancelled():
                    operation.exception()
            if pending:
                raise TimeoutError("durable job handlers did not physically exit")
        await self._io.aclose(deadline_monotonic=deadline)
        if self._embedding is not None and self._owns_embedding:
            await asyncio.wait_for(
                self._embedding.aclose(), timeout=max(0.001, deadline - monotonic())
            )

    async def run_once(self) -> int:
        await self._io.run(
            self._index.scan_lost_wakes, deadline_monotonic=monotonic() + 5.0
        )
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
        prepared = self._model.prepare_json_call(
            purpose=purpose,
            prompt=prompt,
            maximum_input_tokens=input_limit,
            maximum_output_tokens=output_limit,
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

    async def _memory_extraction(self, attempt: AcceptedJobAttempt) -> None:
        source = await self._io.run(
            self._repository.read_memory_extraction_job_source,
            attempt.guard,
            deadline_monotonic=_attempt_deadline(attempt),
        )
        result = await self._provider_json(
            attempt,
            purpose=ModelCallPurpose.COMPACTION_MEMORY_EXTRACTION,
            prompt=(
                'Return only JSON {"candidates": [{"kind": one of FACT, '
                'PREFERENCE, RELATION, CORRECTION, LIFECYCLE, "payload": object}]}. '
                "Return at most 32 candidates.\n"
                + json.dumps(source, ensure_ascii=False, sort_keys=True)
            ),
        )
        raw = result.get("candidates")
        if not isinstance(raw, list) or len(raw) > 32:
            raise ValueError("memory extraction candidate bundle is invalid")
        candidates: list[tuple[str, str, Mapping[str, object], str]] = []
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
                raise ValueError("memory extraction candidate is malformed")
            candidates.append(
                (
                    f"memory-candidate:{uuid4().hex}",
                    str(item.get("kind")),
                    dict(item["payload"]),
                    f"job:{uuid4().hex}",
                )
            )
        await self._io.run(
            self._repository.accept_extracted_memory_bundle,
            attempt.guard,
            candidates=tuple(candidates),
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=_terminal_deadline(),
        )

    async def _memory_governance(self, attempt: AcceptedJobAttempt) -> None:
        candidate = await self._io.run(
            self._repository.read_memory_candidate_for_governance,
            attempt.guard,
            deadline_monotonic=_attempt_deadline(attempt),
        )
        result = await self._provider_json(
            attempt,
            purpose=ModelCallPurpose.MEMORY_GOVERNANCE,
            prompt=(
                'Return only JSON. Use decision SKIP, SUBMIT, CORRECT, MERGE, '
                'SUPERSEDE, or CONTRADICT. SKIP needs a reason. Every other '
                'decision needs fact_kind and fact_payload. Lifecycle decisions '
                'may only use predecessor fact IDs already supplied by the '
                'candidate proposal.\n'
                + json.dumps(candidate, ensure_ascii=False, sort_keys=True)
            ),
        )
        decision = result.get("decision")
        allowed = {
            "SKIP",
            "SUBMIT",
            "CORRECT",
            "MERGE",
            "SUPERSEDE",
            "CONTRADICT",
        }
        if decision not in allowed:
            raise ValueError("memory governance decision is invalid")
        fact_payload = result.get("fact_payload")
        fact_id = None if decision == "SKIP" else f"memory:{uuid4().hex}"
        if fact_id is not None and not isinstance(fact_payload, dict):
            raise ValueError("memory governance fact is missing")
        lifecycle = decision in {
            "CORRECT",
            "MERGE",
            "SUPERSEDE",
            "CONTRADICT",
        }
        proposal = candidate.get("proposal_payload")
        predecessor_value = (
            proposal.get("superseded_fact_ids", ())
            if isinstance(proposal, dict)
            else ()
        )
        if not isinstance(predecessor_value, (list, tuple)):
            raise ValueError("memory lifecycle predecessor carrier is invalid")
        predecessors = tuple(str(value) for value in predecessor_value)
        if lifecycle != bool(predecessors):
            raise ValueError("memory lifecycle decision lacks exact predecessors")
        await self._io.run(
            self._repository.accept_memory_governance,
            attempt.guard,
            candidate_id=str(candidate["id"]),
            decision_id=f"memory-decision:{uuid4().hex}",
            decision=str(decision),
            lineage_payload={
                "handler": MEMORY_GOVERNANCE,
                "attempt": attempt.attempt_ordinal,
                "superseded_fact_ids": predecessors,
            },
            fact_id=fact_id,
            fact_kind=(
                str(result.get("fact_kind") or candidate["proposal_kind"])
                if fact_id
                else None
            ),
            fact_payload=(
                dict(fact_payload) if isinstance(fact_payload, dict) else None
            ),
            relations=(),
            index_handler_contract_id=self._index.handler_contract_id,
            index_handler_contract_version=self._index.handler_contract_version,
            occurred_at=datetime.now(timezone.utc),
            deadline_monotonic=_terminal_deadline(),
        )

    async def _memory_index_refresh(self, attempt: AcceptedJobAttempt) -> None:
        intent = _intent(attempt)
        if intent.get("channel") == "FTS":
            await self._io.run(
                self._index.apply_fts_refresh,
                attempt.guard,
                deadline_monotonic=_attempt_deadline(attempt),
            )
            return
        if intent.get("channel") != "VECTOR":
            raise ValueError("memory index channel is invalid")
        source = await self._io.run(
            self._repository.snapshot_memory_vector_source,
            attempt.guard,
            handler_contract_id=self._index.handler_contract_id,
            handler_contract_version=self._index.handler_contract_version,
            deadline_monotonic=_attempt_deadline(attempt),
        )
        if self._embedding is None:
            self._embedding = build_embedding_provider(self._embedding_config)
        vectors = await self._embedding.embed_batch(
            tuple(item.embedding_text for item in source.facts)
        )
        await self._io.run(
            self._repository.apply_vector_memory_index,
            attempt.guard,
            source=source,
            embeddings=vectors,
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
    "MEMORY_GOVERNANCE",
    "MEMORY_INDEX_REFRESH",
    "POST_COMPACTION_MEMORY_EXTRACTION",
]
