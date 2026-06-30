"""Application-owned lifecycle for loop-bound retrieval resources."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from pulsara_agent.retrieval.config import RetrievalConfig
from pulsara_agent.retrieval.embedding.factory import build_embedding_provider
from pulsara_agent.retrieval.embedding.protocol import EmbeddingProvider
from pulsara_agent.retrieval.rerank.factory import build_rerank_provider
from pulsara_agent.retrieval.rerank.protocol import RerankProvider


class RetrievalWorker(Protocol):
    """Transport-neutral seam for an in-process or external worker adapter."""

    async def run(self) -> None: ...

    def wake(self) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class RetrievalRuntimeResources:
    """Own providers and background retrieval tasks for one HostCore.

    Host sessions borrow these resources. They must never close them.
    """

    embedding: EmbeddingProvider | None = None
    rerank: RerankProvider | None = None
    close_timeout_seconds: float = 5.0
    _workers: list[RetrievalWorker] = field(default_factory=list, init=False, repr=False)
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)
    _close_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def accepting(self) -> bool:
        return not self._closed

    def attach_worker(self, worker: RetrievalWorker) -> None:
        if self._started:
            raise RuntimeError("Retrieval workers must be attached before resources start")
        if self._closed:
            raise RuntimeError("Retrieval resources are closed")
        self._workers.append(worker)

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("Retrieval resources are closed")
        if self._started:
            return
        self._started = True
        for worker in self._workers:
            self.create_task(worker.run(), name=f"retrieval:{type(worker).__name__}")

    def create_task(
        self,
        coroutine: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        if self._closed:
            coroutine.close()
            raise RuntimeError("Retrieval resources are closed")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def wake_workers(self) -> None:
        if self._closed:
            return
        for worker in self._workers:
            worker.wake()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True

            for worker in reversed(self._workers):
                await _bounded_call(worker.aclose, timeout=self.close_timeout_seconds)

            live_tasks = [task for task in self._tasks if not task.done()]
            if live_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*live_tasks, return_exceptions=True),
                        timeout=self.close_timeout_seconds,
                    )
                except TimeoutError:
                    for task in live_tasks:
                        task.cancel()
                    await asyncio.gather(*live_tasks, return_exceptions=True)

            # Providers close only after borrowers/workers have drained.
            for provider in (self.rerank, self.embedding):
                if provider is not None:
                    await _bounded_call(provider.aclose, timeout=self.close_timeout_seconds)


def build_retrieval_runtime_resources(config: RetrievalConfig) -> RetrievalRuntimeResources:
    """Build configured providers without making missing secrets break non-retrieval tests."""

    embedding = build_embedding_provider(config.embedding) if config.embedding.api_key else None
    rerank = build_rerank_provider(config.rerank) if config.rerank.api_key else None
    return RetrievalRuntimeResources(embedding=embedding, rerank=rerank)


async def _bounded_call(call: Callable[[], Awaitable[None]], *, timeout: float) -> None:
    try:
        await asyncio.wait_for(call(), timeout=timeout)
    except TimeoutError:
        return
