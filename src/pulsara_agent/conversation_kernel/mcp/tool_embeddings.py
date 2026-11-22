"""Host-owned advisory embeddings for the MCP tools visible to a query."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from itertools import islice
import logging
from time import monotonic

from pulsara_agent.capability.mcp_embedding_cache import (
    PostgresMcpToolEmbeddingCache,
    mcp_tool_embedding_cache_key,
)
from pulsara_agent.conversation_kernel.host_tasks import (
    _join_task_beyond_logical_deadline,
)
from pulsara_agent.conversation_kernel.io import KernelSessionIO
from pulsara_agent.process_credential_boundary import ProcessCredentialScrubSet
from pulsara_agent.retrieval.config import EmbeddingBackendConfig, RetrievalConnection
from pulsara_agent.retrieval.embedding.factory import build_embedding_provider
from pulsara_agent.retrieval.embedding.validation import validate_embedding_inputs
from pulsara_agent.retrieval.errors import EmbeddingServiceError
from pulsara_agent.settings import LocalSettingsUnavailable
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane


_LOG = logging.getLogger(__name__)
_CONFIG = EmbeddingBackendConfig()


class _ConnectionChanged(EmbeddingServiceError):
    pass


class _ProtectedEmbeddingInput(EmbeddingServiceError):
    pass


class McpToolEmbeddings:
    """Read current scores and warm misses through one Host-local batch worker."""

    def __init__(
        self,
        cache: PostgresMcpToolEmbeddingCache,
        *,
        connection: Callable[[], RetrievalConnection | None],
        requests: asyncio.Semaphore,
        credential_scrub_set: Callable[
            [], ProcessCredentialScrubSet
        ] = ProcessCredentialScrubSet,
    ) -> None:
        self._cache = cache
        self._connection = connection
        self._requests = requests
        self._credential_scrub_set = credential_scrub_set
        self._io = KernelSessionIO()
        self._pending: dict[bytes, str] = {}
        self._pending_connection: RetrievalConnection | None = None
        self._worker: asyncio.Task[None] | None = None
        self._query_requests: set[asyncio.Task[Sequence[float]]] = set()
        self._close_task: asyncio.Task[None] | None = None
        self._closing = False

    def current_connection(self) -> RetrievalConnection | None:
        if self._closing:
            return None
        try:
            connection = self._connection()
        except LocalSettingsUnavailable:
            return None
        return connection if connection is not None and connection.complete else None

    def _require_current(self, connection: RetrievalConnection) -> None:
        if self.current_connection() != connection:
            raise _ConnectionChanged("MCP embedding connection changed")

    def _observe_saved_credentials(self, scrub: ProcessCredentialScrubSet) -> None:
        for value in self._credential_scrub_set().values:
            scrub.observe(value)

    def _provider(
        self, connection: RetrievalConnection, scrub: ProcessCredentialScrubSet
    ):
        def before_request(texts):
            # LocalSettings owns the saved credential inventory. Reading its
            # immutable snapshot here does not enter the HTTP credential gate.
            self._observe_saved_credentials(scrub)
            self._require_current(connection)
            if scrub.contains(connection.model_id) or any(
                scrub.contains(text) for text in texts
            ):
                raise _ProtectedEmbeddingInput(
                    "MCP embedding input contains a saved credential"
                )

        return build_embedding_provider(
            connection,
            config=_CONFIG,
            semaphore=self._requests,
            before_request=before_request,
        )

    async def scores(
        self,
        query: str,
        *,
        connection: RetrievalConnection | None,
        texts: Sequence[str],
        deadline_monotonic: float | None = None,
    ) -> dict[str, float]:
        if connection is None or not texts:
            return {}
        deadline = monotonic() + _CONFIG.timeout_seconds
        if deadline_monotonic is not None:
            deadline = min(deadline, deadline_monotonic)
        scrub = ProcessCredentialScrubSet()
        scrub.observe(connection.api_key)
        try:
            self._require_current(connection)
            self._observe_saved_credentials(scrub)
            documents = {}
            for text in dict.fromkeys(texts):
                try:
                    validate_embedding_inputs((text,))
                except (EmbeddingServiceError, UnicodeError):
                    # Each oversized or invalid document keeps its lexical path.
                    continue
                if scrub.contains(text):
                    continue
                documents[mcp_tool_embedding_cache_key(connection, text)] = text
            if not documents:
                return {}
            cached = await self._io.run(
                self._cache.lookup, tuple(documents), deadline_monotonic=deadline
            )
            self._require_current(connection)
            self._offer(connection, documents, cached)
            if (
                not cached
                or scrub.contains(query)
                or scrub.contains(connection.model_id)
            ):
                return {}
            vector = await self._embed_query(query, connection, scrub, deadline)
            self._require_current(connection)
            ranking = await self._io.run(
                self._cache.rank,
                tuple(key for key in documents if key in cached),
                vector,
                deadline_monotonic=deadline,
            )
            self._require_current(connection)
            return {documents[key]: score for key, score in ranking.items()}
        except (_ConnectionChanged, _ProtectedEmbeddingInput):
            return {}
        except Exception as exc:
            self._log_failure("query", scrub, exc)
            return {}

    async def _embed_query(
        self,
        query: str,
        connection: RetrievalConnection,
        scrub: ProcessCredentialScrubSet,
        deadline: float,
    ) -> Sequence[float]:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("MCP embedding query deadline expired before admission")
        request = asyncio.create_task(
            self._provider(connection, scrub).embed(query), name="mcp-embedding-query"
        )
        self._query_requests.add(request)
        request.add_done_callback(self._query_requests.discard)
        try:
            async with asyncio.timeout(remaining):
                return await asyncio.shield(request)
        except (TimeoutError, asyncio.CancelledError):
            if not request.done() and not request.cancelling():
                request.cancel()
            _, cancellation, _ = await _join_task_beyond_logical_deadline(
                request, deadline_monotonic=deadline
            )
            if cancellation is not None:
                raise cancellation
            raise

    def _offer(
        self,
        connection: RetrievalConnection,
        documents: dict[bytes, str],
        cached: frozenset[bytes],
    ) -> None:
        missing = {key: text for key, text in documents.items() if key not in cached}
        if not missing:
            return
        if self._pending_connection != connection:
            self._pending = {}
            self._pending_connection = connection
        self._pending.update(missing)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._drain(), name="mcp-tool-embeddings"
            )

    async def _drain(self) -> None:
        # Only one background batch competes for the shared request budget.
        # The remaining documents are input data in memory, without tasks per batch.
        while self._pending and not self._closing:
            pending, connection = self._pending, self._pending_connection
            if connection is None or connection != self.current_connection():
                pending.clear()
                break
            batch = dict(islice(pending.items(), _CONFIG.batch_size))
            scrub = ProcessCredentialScrubSet()
            scrub.observe(connection.api_key)
            try:
                await self._fill_batch(connection, batch, scrub)
            except _ConnectionChanged:
                pass
            except Exception as exc:
                self._log_failure("warm", scrub, exc)
            finally:
                for key in batch:
                    # Keep the keys pending until the attempt has settled.
                    pending.pop(key, None)
        self._pending_connection = None

    async def _fill_batch(
        self,
        connection: RetrievalConnection,
        batch: dict[bytes, str],
        scrub: ProcessCredentialScrubSet,
    ) -> None:
        deadline = monotonic() + _CONFIG.timeout_seconds
        # A concurrent lookup may have observed the same miss before an
        # earlier batch committed. Recheck before asking the provider again.
        cached = await self._io.run(
            self._cache.lookup,
            tuple(batch),
            deadline_monotonic=deadline,
            lane=PostgresConnectionLane.BACKGROUND_WORK,
        )
        self._require_current(connection)
        self._observe_saved_credentials(scrub)
        provider = self._provider(connection, scrub)
        async with asyncio.timeout(deadline - monotonic()):
            while not scrub.contains(connection.model_id):
                missing = {
                    key: text
                    for key, text in batch.items()
                    if key not in cached and not scrub.contains(text)
                }
                if not missing:
                    return
                try:
                    vectors = await provider.embed_batch(tuple(missing.values()))
                except _ProtectedEmbeddingInput:
                    # Admission observed a newly saved secret. The request was
                    # never sent, and the next pass excludes the affected inputs.
                    continue
                break
            else:
                return
        # These keys still belong to the space that admitted the request,
        # even if settings changed while the response was in flight.
        await self._io.run(
            self._cache.insert,
            dict(zip(missing, vectors, strict=True)),
            deadline_monotonic=deadline,
        )

    def _log_failure(
        self, phase: str, scrub: ProcessCredentialScrubSet, exc: Exception
    ) -> None:
        try:
            self._observe_saved_credentials(scrub)
            message = scrub.scrub_text(str(exc))
        except Exception:
            _LOG.debug("MCP tool embedding %s failed (%s)", phase, type(exc).__name__)
            return
        _LOG.debug(
            "MCP tool embedding %s failed (%s): %s", phase, type(exc).__name__, message
        )

    def stop_admission(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._pending.clear()
        self._pending_connection = None
        self._io.stop_admission()
        for task in self._active_tasks():
            if not task.done() and not task.cancelling():
                task.cancel()

    def _active_tasks(self) -> tuple[asyncio.Task, ...]:
        tasks = tuple(self._query_requests)
        if self._worker is not None:
            return (*tasks, self._worker)
        return tasks

    async def aclose(self, *, deadline_monotonic: float | None = None) -> None:
        self.stop_admission()
        if deadline_monotonic is None:
            deadline_monotonic = monotonic() + _CONFIG.timeout_seconds
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(deadline_monotonic), name="mcp-tool-embeddings-close"
            )
        late, cancellation, error = await _join_task_beyond_logical_deadline(
            self._close_task, deadline_monotonic=deadline_monotonic
        )
        if cancellation is not None:
            raise cancellation
        if error is not None:
            raise error
        if late:
            raise TimeoutError("MCP embedding requests exited after close deadline")

    async def _close(self, deadline: float) -> None:
        await asyncio.gather(*self._active_tasks(), return_exceptions=True)
        await self._io.aclose(deadline_monotonic=deadline)

    @property
    def physical_close_complete(self) -> bool:
        return self._io.physical_close_complete and all(
            task.done() for task in self._active_tasks()
        )
