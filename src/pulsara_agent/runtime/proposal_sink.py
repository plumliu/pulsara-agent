"""Thread-safe sink for memory candidate envelopes awaiting loop drain.

The ``remember_*`` tools run in worker threads during tool execution; they
must not write durable storage or emit events there (re-entrant publish +
sequence ordering live on the agent loop). Instead they deposit candidate-pool
envelopes here. A ``MemoryHooks`` drain point on the main loop later persists
those envelopes to the durable candidate pool.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from pulsara_agent.memory.candidate_pool import CandidatePoolProposal

MEMORY_INVALID_RETRY_LIMIT = 3


@dataclass(frozen=True, slots=True)
class MemoryRetryState:
    intent_fingerprint: str
    retry_count: int
    retry_limit: int = MEMORY_INVALID_RETRY_LIMIT

    @property
    def retry_allowed(self) -> bool:
        return self.retry_count < self.retry_limit

    @property
    def remaining_retries(self) -> int:
        return max(self.retry_limit - self.retry_count, 0)


@dataclass(slots=True)
class _StagedInvalidAttempt:
    proposal: CandidatePoolProposal


@dataclass(slots=True)
class MemoryProposalSink:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _valid_candidates: list[CandidatePoolProposal] = field(default_factory=list)
    _invalid_by_intent: dict[str, _StagedInvalidAttempt] = field(default_factory=dict)
    _retry_counts_by_intent: dict[str, int] = field(default_factory=dict)
    _satisfied_intents: set[str] = field(default_factory=set)

    def deposit(self, candidate: CandidatePoolProposal) -> None:
        self.deposit_valid(candidate, candidate.intent_fingerprint)

    def deposit_valid(self, candidate: CandidatePoolProposal, intent_fingerprint: str | None) -> None:
        if intent_fingerprint is not None:
            candidate = candidate.model_copy(update={"intent_fingerprint": intent_fingerprint})
        with self._lock:
            if intent_fingerprint:
                self._invalid_by_intent.pop(intent_fingerprint, None)
                self._retry_counts_by_intent.pop(intent_fingerprint, None)
                self._satisfied_intents.add(intent_fingerprint)
            self._valid_candidates.append(candidate)

    def record_invalid(self, candidate: CandidatePoolProposal, intent_fingerprint: str) -> MemoryRetryState:
        candidate = candidate.model_copy(update={"intent_fingerprint": intent_fingerprint})
        with self._lock:
            retry_count = self._retry_counts_by_intent.get(intent_fingerprint, 0) + 1
            self._retry_counts_by_intent[intent_fingerprint] = retry_count
            if intent_fingerprint not in self._satisfied_intents:
                self._invalid_by_intent[intent_fingerprint] = _StagedInvalidAttempt(
                    proposal=candidate,
                )
            return MemoryRetryState(
                intent_fingerprint=intent_fingerprint,
                retry_count=retry_count,
            )

    def drain(self) -> list[CandidatePoolProposal]:
        return self.drain_valid()

    def drain_valid(self) -> list[CandidatePoolProposal]:
        with self._lock:
            drained = self._valid_candidates
            self._valid_candidates = []
            return drained

    def finalize_invalid_attempts(self) -> list[CandidatePoolProposal]:
        with self._lock:
            drained = [
                staged.proposal
                for intent, staged in self._invalid_by_intent.items()
                if intent not in self._satisfied_intents
            ]
            self._invalid_by_intent = {}
            self._retry_counts_by_intent = {}
            self._satisfied_intents = set()
            return drained

    def pending_count(self) -> int:
        with self._lock:
            return len(self._valid_candidates) + len(self._invalid_by_intent)
