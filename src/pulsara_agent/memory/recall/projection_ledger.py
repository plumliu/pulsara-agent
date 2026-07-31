"""Run-local guard against writing recalled memory back as new memory."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pulsara_agent.memory.recall.service import RecallItem
from pulsara_agent.memory.hooks.run_owner import MemoryProjectionLedgerOwner


@dataclass(frozen=True, slots=True)
class ProjectionLedger:
    def record(
        self, ledger: MemoryProjectionLedgerOwner, items: Sequence[RecallItem]
    ) -> None:
        ledger.generation += 1
        ledger.surfaced_ids = {item.memory_id for item in items}
        ledger.surfaced_fingerprints = {
            _normalize(item.snippet) for item in items if item.snippet
        }

    def is_echo(
        self, candidate_statement: str, ledger: MemoryProjectionLedgerOwner
    ) -> bool:
        candidate = _normalize(candidate_statement)
        if not candidate:
            return False
        fingerprints = self._fingerprints(ledger)
        for fingerprint in fingerprints:
            if not fingerprint:
                continue
            if candidate == fingerprint:
                return True
            if len(candidate) >= 24 and candidate in fingerprint:
                return True
            if len(fingerprint) >= 24 and fingerprint in candidate:
                return True
        return False

    def surfaced_ids(self, ledger: MemoryProjectionLedgerOwner) -> set[str]:
        return set(ledger.surfaced_ids)

    def _fingerprints(self, ledger: MemoryProjectionLedgerOwner) -> set[str]:
        return set(ledger.surfaced_fingerprints)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())
