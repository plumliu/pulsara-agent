"""Lossy Host-local embedding maintenance for canonical memory facts."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime, timezone
from time import monotonic
from typing import Protocol

from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.conversation_kernel.repository import ConversationKernelRepository
from pulsara_agent.memory.scope import FrozenMemoryReadContextBinding


MAXIMUM_EMBEDDING_SCAN = 100
MAXIMUM_EMBEDDING_CALLS = 5
MAXIMUM_EMBEDDING_BATCH = 10


class MemoryEmbeddingMaintenancePort(Protocol):
    async def embed_memory_batch(
        self, texts: Sequence[str], *, timeout_seconds: float
    ) -> Sequence[Sequence[float]] | None: ...


class MemoryEmbeddingMaintainer:
    """Best-effort progress without a durable queue, lease, or retry graph."""

    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        read_binding: FrozenMemoryReadContextBinding,
        io_owner: KernelSessionIO,
        deadline_factory: KernelExecutionDeadlineFactory,
        embedding_port: MemoryEmbeddingMaintenancePort,
        session_id: str,
    ) -> None:
        self._repository = repository
        self._read_binding = read_binding
        self._io = io_owner
        self._deadlines = deadline_factory
        self._embedding_port = embedding_port
        self._session_id = session_id
        self._wake = asyncio.Event()
        self._closing = False
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("memory embedding maintainer is already started")
        self._task = asyncio.create_task(
            self._run(), name=f"memory-embedding:{self._session_id}"
        )
        self._wake.set()

    def offer_wake(self) -> None:
        if not self._closing:
            self._wake.set()

    async def aclose(self, *, deadline_monotonic: float) -> None:
        self._closing = True
        self._wake.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        expired = max(0.0, deadline_monotonic - monotonic())
        done, _ = await asyncio.wait((task,), timeout=expired)
        if not done:
            raise TimeoutError("memory embedding maintainer exceeded Host close deadline")
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._closing:
            await self._wake.wait()
            self._wake.clear()
            progressed, backlog = await self._maintain_once()
            if progressed and backlog and not self._closing:
                self._wake.set()

    async def _maintain_once(self) -> tuple[bool, bool]:
        deadline = self._deadlines.deadline(
            KernelWatchdogOwner.MEMORY_FACT_EMBEDDING_BATCH
        )
        try:
            rows = await self._io.run(
                self._repository.list_unembedded_memory_facts,
                read_binding=self._read_binding,
                limit=MAXIMUM_EMBEDDING_SCAN,
                deadline_monotonic=deadline,
            )
        except Exception:
            return False, False
        processed = 0
        progressed = False
        for offset in range(0, min(len(rows), 50), MAXIMUM_EMBEDDING_BATCH):
            if offset // MAXIMUM_EMBEDDING_BATCH >= MAXIMUM_EMBEDDING_CALLS:
                break
            batch = rows[offset : offset + MAXIMUM_EMBEDDING_BATCH]
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            try:
                vectors = await self._embedding_port.embed_memory_batch(
                    tuple(item[2] for item in batch), timeout_seconds=remaining
                )
            except Exception:
                break
            if vectors is None or len(vectors) != len(batch):
                break
            for (fact_id, semantic_digest, _body), vector in zip(
                batch, vectors, strict=True
            ):
                try:
                    installed = await self._io.run(
                        self._repository.upsert_memory_embedding,
                        read_binding=self._read_binding,
                        fact_id=fact_id,
                        fact_semantic_digest=semantic_digest,
                        vector=vector,
                        embedded_at=datetime.now(timezone.utc),
                        deadline_monotonic=deadline,
                    )
                except Exception:
                    continue
                progressed = progressed or installed
            processed += len(batch)
        return progressed, len(rows) >= processed and len(rows) >= 50
