"""Run-local guard against writing recalled memory back as new memory."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pulsara_agent.memory.recall import RecallItem


_SCRATCHPAD_KEY = "memory_projection_ledger"


@dataclass(frozen=True, slots=True)
class ProjectionLedger:
    def record(self, state: Any, items: Sequence[RecallItem]) -> None:
        state.scratchpad[_SCRATCHPAD_KEY] = {
            "ids": {item.memory_id for item in items},
            "fingerprints": {_normalize(item.snippet) for item in items if item.snippet},
        }

    def is_echo(self, candidate_statement: str, state: Any) -> bool:
        candidate = _normalize(candidate_statement)
        if not candidate:
            return False
        fingerprints = self._fingerprints(state)
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

    def surfaced_ids(self, state: Any) -> set[str]:
        ledger = state.scratchpad.get(_SCRATCHPAD_KEY)
        if not isinstance(ledger, dict):
            return set()
        ids = ledger.get("ids")
        if not isinstance(ids, set):
            return set()
        return {value for value in ids if isinstance(value, str)}

    def _fingerprints(self, state: Any) -> set[str]:
        ledger = state.scratchpad.get(_SCRATCHPAD_KEY)
        if not isinstance(ledger, dict):
            return set()
        fingerprints = ledger.get("fingerprints")
        if not isinstance(fingerprints, set):
            return set()
        return {value for value in fingerprints if isinstance(value, str)}


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())
