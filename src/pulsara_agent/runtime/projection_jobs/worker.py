"""Bounded execution of one durable projection job lease."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from typing import Protocol

from pulsara_agent.projection_jobs.contracts import (
    DurableProjectionCommitConfirmation,
    DurableProjectionFailureKind,
    DurableProjectionSettlementOutcome,
    LeasedDurableProjectionJob,
    PreparedDurableProjectionResultFact,
)
from pulsara_agent.runtime.projection_jobs.postgres_repository import (
    PostgresDurableProjectionRepository,
)


class DurableProjectionPreparedResultFactory(Protocol):
    """Pure/source-reading handler boundary used outside a settlement transaction."""

    def __call__(
        self,
        leased_job: LeasedDurableProjectionJob,
        *,
        deadline_monotonic: float,
    ) -> PreparedDurableProjectionResultFact: ...


@dataclass(frozen=True, slots=True)
class DurableProjectionWorkerAttempt:
    repository: PostgresDurableProjectionRepository
    prepared_result_factory: DurableProjectionPreparedResultFactory

    def execute(
        self,
        leased_job: LeasedDurableProjectionJob,
    ) -> DurableProjectionSettlementOutcome:
        """Hydrate/compute outside SQL, then atomically settle the stable lease."""

        physical = leased_job.delivery_policy.physical_policy
        lease_remaining = max(
            0.0,
            (leased_job.lease_expires_at - datetime.now(timezone.utc)).total_seconds(),
        )
        attempt_deadline = monotonic() + min(
            lease_remaining,
            physical.maximum_physical_attempt_seconds,
        )
        try:
            prepared = self.prepared_result_factory(
                leased_job,
                deadline_monotonic=attempt_deadline,
            )
        except TimeoutError as error:
            return self.repository.settle_failure(
                lease=leased_job,
                failure_kind=DurableProjectionFailureKind.DEADLINE_EXCEEDED,
                error=error,
                deadline_monotonic=(
                    monotonic() + physical.result_commit_timeout_seconds
                ),
            )
        except LookupError as error:
            return self.repository.settle_failure(
                lease=leased_job,
                failure_kind=DurableProjectionFailureKind.SOURCE_NOT_READY,
                error=error,
                deadline_monotonic=(
                    monotonic() + physical.result_commit_timeout_seconds
                ),
            )
        except ValueError as error:
            return self.repository.settle_failure(
                lease=leased_job,
                failure_kind=(DurableProjectionFailureKind.SOURCE_AUTHORITY_CONFLICT),
                error=error,
                deadline_monotonic=(
                    monotonic() + physical.result_commit_timeout_seconds
                ),
            )
        except BaseException as error:
            return self.repository.settle_failure(
                lease=leased_job,
                failure_kind=(
                    DurableProjectionFailureKind.TRANSIENT_STORAGE_UNAVAILABLE
                ),
                error=error,
                deadline_monotonic=(
                    monotonic() + physical.result_commit_timeout_seconds
                ),
            )
        outcome = self.repository.settle_success(
            lease=leased_job,
            prepared=prepared,
            deadline_monotonic=(monotonic() + physical.result_commit_timeout_seconds),
        )
        if outcome.confirmation is DurableProjectionCommitConfirmation.UNRESOLVED:
            # The durable lease remains the recovery owner. Do not manufacture
            # a second state transition from an unconfirmed settlement.
            return outcome
        return outcome


__all__ = [
    "DurableProjectionPreparedResultFactory",
    "DurableProjectionWorkerAttempt",
]
