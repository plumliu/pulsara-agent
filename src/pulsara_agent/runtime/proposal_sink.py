"""Thread-safe sink for memory write candidates awaiting an agent-loop drain.

The ``remember_*`` tools run in worker threads during tool execution; they
must not write the graph or emit events there (re-entrant publish + sequence
ordering live on the agent loop). Instead they *deposit* validated typed
candidates here. A ``MemoryHooks`` drain point on the main loop later pulls the
candidates and routes them through ``MemoryWriteService.submit`` at a safe point.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from pulsara_agent.event.candidates import MemoryCandidate


@dataclass(slots=True)
class MemoryProposalSink:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _candidates: list[MemoryCandidate] = field(default_factory=list)

    def deposit(self, candidate: MemoryCandidate) -> None:
        with self._lock:
            self._candidates.append(candidate)

    def drain(self) -> list[MemoryCandidate]:
        with self._lock:
            drained = self._candidates
            self._candidates = []
            return drained

    def pending_count(self) -> int:
        with self._lock:
            return len(self._candidates)
