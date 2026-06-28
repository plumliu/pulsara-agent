"""Runtime event hook that replays canonical mutation outbox rows."""

from __future__ import annotations

from dataclasses import dataclass

from pulsara_agent.event import EventType, RunEndEvent
from pulsara_agent.graph import OxigraphGraphStore
from pulsara_agent.memory.canonical.reconcile import PostgresMemoryReconciler
from pulsara_agent.runtime.hooks import HookContext


@dataclass(slots=True)
class CanonicalMutationOutboxReplayHook:
    dsn: str
    graph_id: str | None
    oxigraph_url: str | None = None
    limit: int = 100

    async def __call__(self, context: HookContext, event) -> None:
        if not _should_replay(event):
            return
        reconciler = PostgresMemoryReconciler(
            dsn=self.dsn,
            oxigraph=OxigraphGraphStore(self.oxigraph_url) if self.oxigraph_url else None,
        )
        reconciler.replay_outbox(graph_id=self.graph_id, limit=self.limit)


def _should_replay(event) -> bool:
    if getattr(event, "type", None) in {
        EventType.REPLY_END,
        EventType.RUN_ERROR,
        EventType.EXCEED_MAX_ITERS,
    }:
        return True
    return isinstance(event, RunEndEvent)
