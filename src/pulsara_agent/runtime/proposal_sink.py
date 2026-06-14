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


@dataclass(slots=True)
class MemoryProposalSink:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _candidates: list[CandidatePoolProposal] = field(default_factory=list)

    def deposit(self, candidate: CandidatePoolProposal) -> None:
        with self._lock:
            self._candidates.append(candidate)

    def drain(self) -> list[CandidatePoolProposal]:
        with self._lock:
            drained = self._candidates
            self._candidates = []
            return drained

    def pending_count(self) -> int:
        with self._lock:
            return len(self._candidates)
