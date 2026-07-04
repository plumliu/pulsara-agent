"""Channel-level recall candidates and diagnostics."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ChannelCandidate:
    memory_id: str
    channel: str
    raw_score: float
    rank: int
    embedding_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateBatch:
    candidates: tuple[ChannelCandidate, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    def channel_rows(self) -> tuple[tuple[str, list[tuple[str, float]]], ...]:
        channel_order: list[str] = []
        rows: dict[str, list[tuple[str, float]]] = {}
        for candidate in self.candidates:
            if candidate.channel not in rows:
                rows[candidate.channel] = []
                channel_order.append(candidate.channel)
            rows[candidate.channel].append((candidate.memory_id, candidate.raw_score))
        return tuple((channel, rows[channel]) for channel in channel_order)
