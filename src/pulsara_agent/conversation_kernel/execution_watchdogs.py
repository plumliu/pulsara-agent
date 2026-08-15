"""Closed process-local watchdog policy for the conversation Kernel.

These values bound one physical owner or one canonical operation.  They are
not turn budgets and are never serialized into canonical rows or events.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Callable

from pulsara_agent.llm.adapters.openai.client import OpenAITransportTimeoutPolicy


class KernelWatchdogOwner(StrEnum):
    PROVIDER_DISPATCH_PLANNING = "PROVIDER_DISPATCH_PLANNING"
    FOREGROUND_CANONICAL = "FOREGROUND_CANONICAL"
    WRITER_RENEWAL = "WRITER_RENEWAL"
    NONTERMINAL_TOOL_INVOCATION = "NONTERMINAL_TOOL_INVOCATION"
    TERMINAL_FOREGROUND_DECISION = "TERMINAL_FOREGROUND_DECISION"
    HOST_SESSION_CLOSE = "HOST_SESSION_CLOSE"
    DURABLE_JOB_EXECUTOR_CLOSE = "DURABLE_JOB_EXECUTOR_CLOSE"
    BLOB_GC_CLOSE = "BLOB_GC_CLOSE"
    MEMORY_GOVERNANCE_ATTEMPT = "MEMORY_GOVERNANCE_ATTEMPT"
    MEMORY_HINT_REVIEW_ATTEMPT = "MEMORY_HINT_REVIEW_ATTEMPT"
    MEMORY_GOVERNOR_CLOSE = "MEMORY_GOVERNOR_CLOSE"
    MEMORY_AUTO_QUERY_EMBEDDING = "MEMORY_AUTO_QUERY_EMBEDDING"
    MEMORY_EXPLICIT_QUERY_EMBEDDING = "MEMORY_EXPLICIT_QUERY_EMBEDDING"
    MEMORY_EXPLICIT_RERANK = "MEMORY_EXPLICIT_RERANK"
    MEMORY_EXPLICIT_RECALL_TOTAL = "MEMORY_EXPLICIT_RECALL_TOTAL"
    MEMORY_FACT_EMBEDDING_BATCH = "MEMORY_FACT_EMBEDDING_BATCH"
    MEMORY_RETRIEVAL_DISABLE_CLOSE = "MEMORY_RETRIEVAL_DISABLE_CLOSE"


@dataclass(frozen=True, slots=True)
class KernelExecutionWatchdogPolicy:
    provider_dispatch_planning_attempt_seconds: float = 120.0
    foreground_canonical_attempt_seconds: float = 120.0
    writer_renew_attempt_seconds: float = 10.0
    writer_renew_safety_margin_seconds: float = 5.0
    provider_connect_seconds: float = 120.0
    provider_write_seconds: float = 120.0
    provider_pool_seconds: float = 120.0
    provider_stream_idle_seconds: float = 600.0
    nonterminal_tool_attempt_seconds: float = 600.0
    terminal_foreground_decision_seconds: float = 120.0
    host_session_close_join_seconds: float = 120.0
    durable_job_executor_close_seconds: float = 120.0
    blob_gc_close_seconds: float = 120.0
    memory_governance_attempt_seconds: float = 300.0
    memory_hint_review_attempt_seconds: float = 120.0
    memory_governor_close_seconds: float = 120.0
    memory_auto_query_embedding_seconds: float = 3.0
    memory_explicit_query_embedding_seconds: float = 4.0
    memory_explicit_rerank_seconds: float = 4.0
    memory_explicit_recall_total_seconds: float = 8.0
    memory_fact_embedding_batch_seconds: float = 30.0
    memory_retrieval_disable_close_seconds: float = 120.0
    writer_lease_seconds: float = 30.0
    writer_renew_interval_seconds: float = 10.0

    # Deliberately absent product budgets.  Huge integer sentinels are not a
    # valid substitute for this closed absence contract.
    turn_total_seconds: None = None
    model_calls_per_turn: None = None
    tool_calls_per_turn: None = None
    foreground_provider_total_seconds: None = None
    plan_human_wait_seconds: None = None
    terminal_process_lifetime_seconds: None = None

    def __post_init__(self) -> None:
        positive = (
            self.provider_dispatch_planning_attempt_seconds,
            self.foreground_canonical_attempt_seconds,
            self.writer_renew_attempt_seconds,
            self.writer_renew_safety_margin_seconds,
            self.provider_connect_seconds,
            self.provider_write_seconds,
            self.provider_pool_seconds,
            self.provider_stream_idle_seconds,
            self.nonterminal_tool_attempt_seconds,
            self.terminal_foreground_decision_seconds,
            self.host_session_close_join_seconds,
            self.durable_job_executor_close_seconds,
            self.blob_gc_close_seconds,
            self.memory_governance_attempt_seconds,
            self.memory_hint_review_attempt_seconds,
            self.memory_governor_close_seconds,
            self.memory_auto_query_embedding_seconds,
            self.memory_explicit_query_embedding_seconds,
            self.memory_explicit_rerank_seconds,
            self.memory_explicit_recall_total_seconds,
            self.memory_fact_embedding_batch_seconds,
            self.memory_retrieval_disable_close_seconds,
            self.writer_lease_seconds,
            self.writer_renew_interval_seconds,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("Kernel watchdog fields must be positive")
        if (
            self.writer_renew_interval_seconds
            + self.writer_renew_attempt_seconds
            + self.writer_renew_safety_margin_seconds
            >= self.writer_lease_seconds
        ):
            raise ValueError("writer renewal timing does not fit inside its lease")
        if self.terminal_foreground_decision_seconds <= 30.0:
            raise ValueError("Terminal decision watchdog must exceed the public yield bound")

    @property
    def foreground_transport(self) -> OpenAITransportTimeoutPolicy:
        return OpenAITransportTimeoutPolicy(
            connect_seconds=self.provider_connect_seconds,
            write_seconds=self.provider_write_seconds,
            pool_seconds=self.provider_pool_seconds,
            read_idle_seconds=self.provider_stream_idle_seconds,
            total_seconds=None,
        )

    def durable_job_transport(
        self, remaining_attempt_seconds: float
    ) -> OpenAITransportTimeoutPolicy:
        """Bind wire fields to the existing finite durable-attempt owner."""

        if remaining_attempt_seconds <= 0:
            raise ValueError("durable job attempt has no transport budget")
        return OpenAITransportTimeoutPolicy(
            connect_seconds=min(
                self.provider_connect_seconds, remaining_attempt_seconds
            ),
            write_seconds=min(
                self.provider_write_seconds, remaining_attempt_seconds
            ),
            pool_seconds=min(self.provider_pool_seconds, remaining_attempt_seconds),
            read_idle_seconds=min(
                self.provider_stream_idle_seconds, remaining_attempt_seconds
            ),
            total_seconds=remaining_attempt_seconds,
        )

    def seconds_for(self, owner: KernelWatchdogOwner) -> float:
        mapping = {
            KernelWatchdogOwner.PROVIDER_DISPATCH_PLANNING: self.provider_dispatch_planning_attempt_seconds,
            KernelWatchdogOwner.FOREGROUND_CANONICAL: self.foreground_canonical_attempt_seconds,
            KernelWatchdogOwner.WRITER_RENEWAL: self.writer_renew_attempt_seconds,
            KernelWatchdogOwner.NONTERMINAL_TOOL_INVOCATION: self.nonterminal_tool_attempt_seconds,
            KernelWatchdogOwner.TERMINAL_FOREGROUND_DECISION: self.terminal_foreground_decision_seconds,
            KernelWatchdogOwner.HOST_SESSION_CLOSE: self.host_session_close_join_seconds,
            KernelWatchdogOwner.DURABLE_JOB_EXECUTOR_CLOSE: self.durable_job_executor_close_seconds,
            KernelWatchdogOwner.BLOB_GC_CLOSE: self.blob_gc_close_seconds,
            KernelWatchdogOwner.MEMORY_GOVERNANCE_ATTEMPT: self.memory_governance_attempt_seconds,
            KernelWatchdogOwner.MEMORY_HINT_REVIEW_ATTEMPT: self.memory_hint_review_attempt_seconds,
            KernelWatchdogOwner.MEMORY_GOVERNOR_CLOSE: self.memory_governor_close_seconds,
            KernelWatchdogOwner.MEMORY_AUTO_QUERY_EMBEDDING: self.memory_auto_query_embedding_seconds,
            KernelWatchdogOwner.MEMORY_EXPLICIT_QUERY_EMBEDDING: self.memory_explicit_query_embedding_seconds,
            KernelWatchdogOwner.MEMORY_EXPLICIT_RERANK: self.memory_explicit_rerank_seconds,
            KernelWatchdogOwner.MEMORY_EXPLICIT_RECALL_TOTAL: self.memory_explicit_recall_total_seconds,
            KernelWatchdogOwner.MEMORY_FACT_EMBEDDING_BATCH: self.memory_fact_embedding_batch_seconds,
            KernelWatchdogOwner.MEMORY_RETRIEVAL_DISABLE_CLOSE: self.memory_retrieval_disable_close_seconds,
        }
        return mapping[owner]


class KernelExecutionDeadlineFactory:
    """Issue absolute deadlines only for the closed owner vocabulary."""

    __slots__ = ("policy", "_clock")

    def __init__(
        self,
        policy: KernelExecutionWatchdogPolicy | None = None,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.policy = policy or KernelExecutionWatchdogPolicy()
        self._clock = clock

    def deadline(self, owner: KernelWatchdogOwner) -> float:
        if not isinstance(owner, KernelWatchdogOwner):
            raise TypeError("watchdog deadline owner is not in the closed vocabulary")
        return self._clock() + self.policy.seconds_for(owner)


DEFAULT_KERNEL_WATCHDOG_POLICY = KernelExecutionWatchdogPolicy()


__all__ = [
    "DEFAULT_KERNEL_WATCHDOG_POLICY",
    "KernelExecutionDeadlineFactory",
    "KernelExecutionWatchdogPolicy",
    "KernelWatchdogOwner",
    "OpenAITransportTimeoutPolicy",
]
