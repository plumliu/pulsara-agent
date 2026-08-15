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
)
JOB_HANDLER_BY_TYPE: Mapping[str, KernelJobHandlerContract] = MappingProxyType(
    {item.handler_type: item for item in JOB_HANDLER_CATALOG}
)
if len(JOB_HANDLER_BY_TYPE) != 1:
    raise RuntimeError("Round 8 job handler catalog is not exhaustive")


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
    "job_handler_contract",
]
