"""Closed Stage 2 first-party durable-job catalog.

The catalog, not an enqueue caller or provider payload, owns every retry,
deadline, safety, and provider-request bound installed in PostgreSQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from pulsara_agent.conversation_kernel.contracts import JobSafetyClass


BACKGROUND_COMPACTION = "BACKGROUND_COMPACTION"
POST_COMPACTION_MEMORY_EXTRACTION = "POST_COMPACTION_MEMORY_EXTRACTION"
MEMORY_GOVERNANCE = "MEMORY_GOVERNANCE"
MEMORY_INDEX_REFRESH = "MEMORY_INDEX_REFRESH"


@dataclass(frozen=True, slots=True)
class KernelJobHandlerContract:
    handler_type: str
    safety_class: JobSafetyClass
    retry_policy_id: str
    retry_policy_version: int
    maximum_attempts: int
    attempt_timeout_ms: int
    calls_provider: bool
    input_token_limit: int | None
    output_token_limit: int | None

    def __post_init__(self) -> None:
        if (
            not self.handler_type
            or not self.retry_policy_id
            or self.retry_policy_version < 1
            or self.maximum_attempts < 1
            or self.attempt_timeout_ms < 1
        ):
            raise ValueError("job handler contract must be finite and named")
        if self.safety_class is not JobSafetyClass.RETRY_SAFE:
            raise ValueError("Stage 2 production handlers must be retry-safe")
        if self.calls_provider != (
            self.input_token_limit is not None
            and self.output_token_limit is not None
        ):
            raise ValueError("provider-backed job limit union is invalid")
        if self.calls_provider and min(
            int(self.input_token_limit or 0), int(self.output_token_limit or 0)
        ) < 1:
            raise ValueError("provider job limits must be positive")


JOB_HANDLER_CATALOG = (
    KernelJobHandlerContract(
        BACKGROUND_COMPACTION,
        JobSafetyClass.RETRY_SAFE,
        "bounded-exponential",
        1,
        3,
        45_000,
        True,
        32_000,
        2_048,
    ),
    KernelJobHandlerContract(
        POST_COMPACTION_MEMORY_EXTRACTION,
        JobSafetyClass.RETRY_SAFE,
        "bounded-exponential",
        1,
        3,
        45_000,
        True,
        32_000,
        2_048,
    ),
    KernelJobHandlerContract(
        MEMORY_GOVERNANCE,
        JobSafetyClass.RETRY_SAFE,
        "bounded-exponential",
        1,
        3,
        30_000,
        True,
        16_000,
        1_024,
    ),
    KernelJobHandlerContract(
        MEMORY_INDEX_REFRESH,
        JobSafetyClass.RETRY_SAFE,
        "bounded-exponential",
        1,
        3,
        30_000,
        False,
        None,
        None,
    ),
)
JOB_HANDLER_BY_TYPE: Mapping[str, KernelJobHandlerContract] = MappingProxyType(
    {item.handler_type: item for item in JOB_HANDLER_CATALOG}
)
if len(JOB_HANDLER_BY_TYPE) != 4:
    raise RuntimeError("Stage 2 job handler catalog is not exhaustive")


def job_handler_contract(handler_type: str) -> KernelJobHandlerContract:
    try:
        return JOB_HANDLER_BY_TYPE[handler_type]
    except KeyError as exc:
        raise ValueError("job handler is not in the Stage 2 catalog") from exc


__all__ = [
    "BACKGROUND_COMPACTION",
    "JOB_HANDLER_BY_TYPE",
    "JOB_HANDLER_CATALOG",
    "KernelJobHandlerContract",
    "MEMORY_GOVERNANCE",
    "MEMORY_INDEX_REFRESH",
    "POST_COMPACTION_MEMORY_EXTRACTION",
    "job_handler_contract",
]
