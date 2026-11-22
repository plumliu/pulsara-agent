"""Native memory CRUD and complete shared retrieval/observation values."""

from __future__ import annotations
import asyncio
from dataclasses import dataclass, replace
import json
import math
from time import monotonic
from typing import Sequence, TYPE_CHECKING

from pulsara_agent.conversation_kernel.io import (
    KernelSessionIO,
    PhysicalToolInvocationDisposition,
)
from pulsara_agent.conversation_kernel.execution_watchdogs import (
    KernelExecutionDeadlineFactory,
    KernelWatchdogOwner,
)
from pulsara_agent.conversation_kernel.history_search import (
    PostgresHistoryQuery,
    FrozenHistoryReadScope,
)
from pulsara_agent.conversation_kernel.history_excerpt import HistoryCandidate
from pulsara_agent.conversation_kernel.memory.recall import (
    PostgresMemoryQuery,
    MemoryQueryResult,
    MemoryRetrievalDisposition,
)
from pulsara_agent.conversation_kernel.memory.contracts import (
    MemoryFactKind,
    MemoryObservationState,
    MemoryObservationText,
    MemoryObservationCandidate,
    AutomaticMemoryTriggerDisposition,
    FrozenMemoryTriggerPolicy,
    MemoryUsePolicy,
    memory_title,
    memory_statement,
    memory_timestamp,
)
from pulsara_agent.conversation_kernel.memory.hints import (
    CheapMemoryWriteHintMatcher,
    MemoryWriteOptOut,
    TurnMemoryUseOptOut,
    normalize_memory_trigger_text,
)
from pulsara_agent.conversation_kernel.memory.management import MemoryManagementError
from pulsara_agent.conversation_kernel.content_parts import ResultPart
from pulsara_agent.conversation_kernel.tool_contracts import (
    KernelToolResult,
)
from pulsara_agent.memory.scope import CTX_GLOBAL, FrozenMemoryReadContextBinding
from pulsara_agent.memory.targets import memory_target, parse_memory_target
from pulsara_agent.ports.session_content import (
    session_content_range,
    session_entry_target,
)
from pulsara_agent.ports.tool_execution import (
    ToolBusinessValue,
    freeze_tool_json_object,
    thaw_tool_json_object,
)
from pulsara_agent.retrieval.config import (
    AdvisoryMemoryFeatureConfig,
    DenseRecallPurpose,
    EmbeddingBackendConfig,
    RetrievalConnection,
    RerankBackendConfig,
)
from pulsara_agent.retrieval.embedding.factory import build_embedding_provider
from pulsara_agent.retrieval.embedding.protocol import EmbeddingProvider
from pulsara_agent.retrieval.decision import DecisionService
from pulsara_agent.retrieval.errors import EmbeddingServiceError
from pulsara_agent.retrieval.rerank.factory import build_rerank_provider
from pulsara_agent.retrieval.rerank.protocol import RerankProvider, RerankPurpose
from pulsara_agent.settings import LocalSettingsStore, LocalSettingsUnavailable

if TYPE_CHECKING:
    from pulsara_agent.conversation_kernel.repository import (
        ConversationKernelRepository,
    )
    from pulsara_agent.conversation_kernel.memory.embedding_maintainer import (
        MemoryEmbeddingMaintainer,
    )

MEMORY_READ_TOOL_NAMES = frozenset({"query_memory"})
MEMORY_WRITE_TOOL_NAMES = frozenset({"remember", "manage_memory"})
MEMORY_TOOL_NAMES = MEMORY_READ_TOOL_NAMES | MEMORY_WRITE_TOOL_NAMES
MEMORY_POINT_READ_TIMEOUT_SECONDS = 10.0
MEMORY_SEARCH_TIMEOUT_SECONDS = 20.0
AUTOMATIC_DECISION_MINIMUM_PROBABILITY = 0.7


@dataclass(frozen=True, slots=True)
class FrozenQueryEmbedding:
    vector: tuple[float, ...]
    connection: RetrievalConnection


class KernelMemoryToolPort:
    def __init__(
        self,
        *,
        repository: ConversationKernelRepository,
        session_id: str,
        read_binding: FrozenMemoryReadContextBinding,
        embedding_config: EmbeddingBackendConfig,
        embedding_gate: asyncio.Semaphore | None = None,
        rerank_config: RerankBackendConfig | None = None,
        feature_config: AdvisoryMemoryFeatureConfig | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        rerank_provider: RerankProvider | None = None,
        io_owner: KernelSessionIO,
        settings: LocalSettingsStore,
        decision_service: DecisionService | None = None,
    ) -> None:
        self._repository = repository
        self._session_id = session_id
        self._read_binding = read_binding
        self._query = PostgresMemoryQuery(repository.connection_provider)
        self._history = PostgresHistoryQuery(repository.connection_provider)
        self._embedding_config = embedding_config
        self._rerank_config = rerank_config or RerankBackendConfig()
        self._feature_config = feature_config or AdvisoryMemoryFeatureConfig()
        self._io = io_owner
        self._deadlines = KernelExecutionDeadlineFactory()
        self._deadline_factory_bound = False
        self._embedding = embedding_provider
        self._rerank = rerank_provider
        self._decision_service = decision_service
        self._embedding_gate = embedding_gate or asyncio.Semaphore(embedding_config.max_concurrent)
        self._rerank_gate = asyncio.Semaphore(self._rerank_config.max_concurrent)
        self._remote_tasks: set[asyncio.Task[object]] = set()
        self._write_opt_out = MemoryWriteOptOut()
        self._turn_use_opt_out = TurnMemoryUseOptOut()
        self._write_hint_matcher = CheapMemoryWriteHintMatcher()
        self._closed = False
        self._embedding_maintainer: MemoryEmbeddingMaintainer | None = None
        self._settings = settings
        self._installed_observation_reader = lambda: MemoryObservationState()

    @property
    def tool_names(self) -> frozenset[str]:
        return MEMORY_TOOL_NAMES

    def bind_embedding_maintainer(self, maintainer: MemoryEmbeddingMaintainer) -> None:
        if self._embedding_maintainer is not None:
            raise RuntimeError("memory embedding maintainer is already bound")
        self._embedding_maintainer = maintainer

    def offer_embedding_wake(self) -> None:
        maintainer = self._embedding_maintainer
        if maintainer is not None:
            maintainer.offer_wake()

    def bind_deadline_factory(
        self, deadline_factory: KernelExecutionDeadlineFactory
    ) -> None:
        """Install the Host policy once without widening the public ctor seam."""

        if self._deadline_factory_bound:
            raise RuntimeError("memory deadline factory is already bound")
        if (
            self._remote_tasks
            or self._embedding is not None
            or self._rerank is not None
        ):
            raise RuntimeError("memory deadline factory was bound after admission")
        self._deadlines = deadline_factory
        self._deadline_factory_bound = True

    def classify_automatic_trigger(
        self, text: str
    ) -> AutomaticMemoryTriggerDisposition:
        return self.classify_memory_trigger(text).automatic_recall

    def classify_memory_trigger(self, text: str) -> FrozenMemoryTriggerPolicy:
        normalized = normalize_memory_trigger_text(text)
        if self._turn_use_opt_out.excludes(normalized):
            return FrozenMemoryTriggerPolicy(
                AutomaticMemoryTriggerDisposition.DISABLED_BY_EXPLICIT_USER_DIRECTIVE,
                MemoryUsePolicy.ALL_DISABLED_BY_USER,
                False,
            )
        memory_use = (
            MemoryUsePolicy.WRITE_DISABLED_BY_USER
            if self._write_opt_out.excludes(normalized)
            else MemoryUsePolicy.ENABLED
        )
        automatic = (
            AutomaticMemoryTriggerDisposition.SKIPPED_LOW_INFORMATION
            if len(normalized) < 8
            else AutomaticMemoryTriggerDisposition.ELIGIBLE
        )
        return FrozenMemoryTriggerPolicy(
            automatic,
            memory_use,
            memory_use.allows_writes and self._write_hint_matcher.matches(normalized),
        )

    async def install_memory_embedding(self, connection, operation):
        return await self._settings.install_embedding_if_current(connection, operation)

    async def embed_memory_batch(
        self,
        texts: Sequence[str],
        *,
        timeout_seconds: float,
        connection: RetrievalConnection,
    ) -> Sequence[Sequence[float]] | None:
        if not texts or self._closed or self.embedding_connection() != connection:
            return None
        try:
            provider = self._embedding_provider_for(connection)
            return await self._run_remote_exact(
                provider.embed_batch(tuple(texts)),
                timeout_seconds=max(0.001, timeout_seconds),
                name="memory-fact-embedding-batch",
            )
        except Exception:
            return None

    def embedding_connection(self):
        try:
            retrieval = self._settings.read().memory_retrieval
        except LocalSettingsUnavailable:
            return None
        return retrieval.embedding if retrieval.embedding_enabled else None

    def ranking_enabled(self):
        try:
            return self._settings.read().memory_retrieval.ranking_mode != "off"
        except LocalSettingsUnavailable:
            return False

    async def _embedding_provider(self) -> EmbeddingProvider | None:
        connection = self.embedding_connection()
        if connection is None or self._closed:
            return None
        return self._embedding_provider_for(connection)

    def _embedding_provider_for(
        self, connection: RetrievalConnection
    ) -> EmbeddingProvider:
        if self._embedding is not None:
            return self._embedding

        def require_current(_texts: Sequence[str]) -> None:
            if self._closed or self.embedding_connection() != connection:
                raise EmbeddingServiceError(
                    "memory embedding connection changed or closed"
                )

        return build_embedding_provider(
            connection,
            config=self._embedding_config,
            semaphore=self._embedding_gate,
            before_request=require_current,
        )

    async def _rerank_provider(self) -> RerankProvider | None:
        if not self._feature_config.explicit_rerank or self._closed:
            return None
        try:
            retrieval = self._settings.read().memory_retrieval
        except LocalSettingsUnavailable:
            return None
        if retrieval.ranking_mode == "off":
            return None
        if self._rerank is not None:
            return self._rerank
        return build_rerank_provider(
            getattr(retrieval, retrieval.ranking_mode),
            config=self._rerank_config,
            semaphore=self._rerank_gate,
            decision_service=self._decision_service,
        )

    async def _frozen_query_embedding(self, provider, text):
        connection = getattr(provider, "connection", self.embedding_connection())
        vector = await provider.embed(text)
        if connection is None or self.embedding_connection() != connection:
            return None
        return FrozenQueryEmbedding(tuple(vector), connection)

    async def _query_embedding(
        self, normalized: str, *, automatic: bool
    ) -> FrozenQueryEmbedding | None:
        embedding = None
        if not automatic or self._feature_config.automatic_dense:
            try:
                embedding = await self._embedding_provider()
            except Exception:
                # Dense recall is optional; a constructor/configuration error
                # degrades this trigger to the local sparse channel.
                embedding = None
        if embedding is not None:
            try:
                return await self._run_remote_exact(
                    self._frozen_query_embedding(embedding, normalized),
                    timeout_seconds=self._deadlines.policy.seconds_for(
                        KernelWatchdogOwner.MEMORY_AUTO_QUERY_EMBEDDING
                        if automatic
                        else KernelWatchdogOwner.MEMORY_EXPLICIT_QUERY_EMBEDDING
                    ),
                    name="memory-automatic-query-embedding",
                )
            except Exception:
                pass
        return None

    @property
    def physical_close_complete(self) -> bool:
        return self._closed and all(task.done() for task in self._remote_tasks)

    async def aclose(self) -> None:
        self._closed = True
        tasks = tuple(self._remote_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            cancellation = await _join_remote_tasks(tasks)
            if cancellation is not None:
                raise cancellation

    async def _run_remote_exact(
        self,
        operation,
        *,
        timeout_seconds: float,
        name: str,
    ):
        if self._closed:
            operation.close()
            raise RuntimeError("memory retrieval owner is closed")
        task = asyncio.create_task(operation, name=name)
        self._remote_tasks.add(task)
        try:
            try:
                async with asyncio.timeout(timeout_seconds):
                    return await asyncio.shield(task)
            except TimeoutError:
                task.cancel()
                await _join_remote_tasks((task,))
                raise
            except asyncio.CancelledError:
                task.cancel()
                await _join_remote_tasks((task,))
                raise
        finally:
            self._remote_tasks.discard(task)

    def _canonical_deadline(self, maximum_seconds: float) -> float:
        return min(
            monotonic() + maximum_seconds,
            self._deadlines.deadline(KernelWatchdogOwner.FOREGROUND_CANONICAL),
        )

    def _remaining_for(
        self, total_deadline: float, owner: KernelWatchdogOwner
    ) -> float:
        operation_deadline = min(total_deadline, self._deadlines.deadline(owner))
        remaining = operation_deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{owner.value} deadline expired")
        return remaining

    async def _rerank_explicit(
        self,
        query: str,
        result,
        *,
        total_deadline: float,
        purpose: RerankPurpose = "recall",
    ):
        try:
            retrieval_before = self._settings.read().memory_retrieval
        except LocalSettingsUnavailable:
            if purpose == "automatic_recall":
                return replace(
                    result,
                    facts=tuple(
                        item
                        for item in result.facts
                        if not isinstance(item, HistoryCandidate)
                    ),
                )
            return result
        if retrieval_before.ranking_mode == "off":
            return result
        decision_admission = (
            purpose == "automatic_recall"
            and retrieval_before.ranking_mode == "decision"
        )

        def fallback(disposition):
            # A failed/unavailable Decision must not admit unjudged history.
            # Ordinary memory retains its existing local retrieval fallback.
            facts = tuple(
                item
                for item in result.facts
                if not decision_admission or not isinstance(item, HistoryCandidate)
            )
            return replace(result, facts=facts, rerank_disposition=disposition)

        try:
            provider = await self._rerank_provider()
            if (
                provider is not None
                and hasattr(provider, "connection")
                and provider.connection
                != getattr(retrieval_before, retrieval_before.ranking_mode)
            ):
                raise ValueError("rerank configuration changed before admission")
        except Exception:
            return fallback("FAILED_FALLBACK")
        if provider is None or not result.facts:
            return fallback(result.rerank_disposition)
        selected = result.facts
        remote_query, documents = query, tuple(_rerank_text(item) for item in selected)
        try:
            rows = await self._run_remote_exact(
                provider.rerank(
                    remote_query,
                    documents,
                    candidate_ids=tuple(str(index) for index in range(len(selected))),
                    purpose=purpose,
                ),
                timeout_seconds=self._remaining_for(
                    total_deadline,
                    KernelWatchdogOwner.MEMORY_EXPLICIT_RERANK,
                ),
                name="memory-explicit-rerank",
            )
            retrieval_after = self._settings.read().memory_retrieval
            mode = retrieval_before.ranking_mode
            if retrieval_after.ranking_mode != mode or getattr(
                retrieval_after, mode
            ) != getattr(retrieval_before, mode):
                raise ValueError("rerank configuration changed")
            rows = sorted(rows, key=lambda item: (-item.score, item.index))
            indexes = tuple(item.index for item in rows)
            if (
                len(indexes) != len(selected)
                or len(set(indexes)) != len(indexes)
                or set(indexes) != set(range(len(selected)))
                or any(not math.isfinite(item.score) for item in rows)
                or (
                    decision_admission
                    and any(not 0 <= item.score <= 1 for item in rows)
                )
            ):
                raise ValueError("rerank response does not cover the exact candidates")
        except Exception:
            return fallback("FAILED_FALLBACK")
        else:
            ordered = tuple(
                selected[row.index]
                for row in rows
                if not decision_admission
                or row.score >= AUTOMATIC_DECISION_MINIMUM_PROBABILITY
            )
            disposition = "APPLIED"
        return replace(result, facts=tuple(ordered), rerank_disposition=disposition)

    def bind_installed_observation_reader(self, reader):
        self._installed_observation_reader = reader

    def _context_id(self, value):
        if value == "GLOBAL":
            return CTX_GLOBAL
        if (
            value == "CURRENT_PROJECT"
            and self._read_binding.current_project_context_id is not None
        ):
            return self._read_binding.current_project_context_id
        raise ValueError(
            "当前会话只可保存到可读的全局或当前项目范围；快速会话仅可用GLOBAL。"
        )

    async def invoke(self, *, tool_name, arguments, invocation_context):
        if self._closed or invocation_context.session_id != self._session_id:
            return _result("SYSTEM_ERROR", "记忆服务当前不可用。")
        if invocation_context.conversation_scope_kind != "ROOT":
            return _result("PERMISSION_DENIED", "只有ROOT可以调用记忆工具。")
        policy = invocation_context.memory_context.memory_use_policy
        if (tool_name in MEMORY_READ_TOOL_NAMES and not policy.allows_reads) or (
            tool_name in MEMORY_WRITE_TOOL_NAMES and not policy.allows_writes
        ):
            return _result("PERMISSION_DENIED", "本次用户输入已停用相应记忆操作。")
        try:
            if tool_name == "remember":
                return await self._remember(arguments)
            if tool_name == "query_memory":
                return await self._query_memory(arguments)
            if tool_name == "manage_memory":
                return await self._manage_memory(arguments)
        except (ValueError, TypeError, MemoryManagementError) as exc:
            return _result("APPLICATION_ERROR", str(exc))
        except asyncio.CancelledError:
            return _result("CANCELLED", "记忆操作已取消。")
        except Exception as exc:
            return _result(
                "SYSTEM_ERROR",
                "记忆操作未能确认完成："
                + str(exc)
                + "；请先查询当前记忆后再决定，不要自动重复写入。",
            )
        raise KeyError(tool_name)

    async def _mutation(self, operation, **arguments):
        outcome = await self._io.run_tool_invocation(
            operation,
            deadline_monotonic=self._canonical_deadline(MEMORY_SEARCH_TIMEOUT_SECONDS),
            **arguments,
        )
        if outcome.disposition is PhysicalToolInvocationDisposition.RAISED:
            raise outcome.error
        return outcome

    @staticmethod
    def _physical_result(result, outcome):
        return replace(
            result,
            physical_timing=outcome.timing.value,
            caller_cancelled_while_running=outcome.caller_cancelled,
        )

    async def _remember(self, arguments):
        if set(arguments) - {"context_kind", "kind", "title", "statement"} or not {
            "context_kind",
            "kind",
            "title",
        } <= set(arguments):
            raise ValueError("remember只接受context_kind、kind、title和statement。")
        kind = MemoryFactKind(arguments["kind"])
        if (
            kind is not MemoryFactKind.RESPONSE_PREFERENCE
            and "statement" not in arguments
        ):
            raise ValueError("普通记忆须提供statement。")
        title = memory_title(arguments["title"])
        statement = memory_statement(kind, arguments.get("statement", ""))
        outcome = await self._mutation(
            self._repository.create_memory,
            memory_domain_id=self._read_binding.memory_domain_id,
            context_id=self._context_id(arguments["context_kind"]),
            kind=kind,
            title=title,
            statement=statement,
            read_binding=self._read_binding,
        )
        record = outcome.value
        self.offer_embedding_wake()
        parts = [ResultPart("text", "已保存记忆。")]
        try:
            candidates = await self._search_memories(record.retrieval_text, limit=4)
            related = [item for item in candidates if item.fact_id != record.fact_id][
                :3
            ]
            parts.extend(ResultPart("object", item.value()) for item in related)
        except asyncio.CancelledError:
            parts.append(ResultPart("text", "记忆已保存，相关记忆推荐已取消。"))
        except Exception as exc:
            parts.append(
                ResultPart("text", "记忆已保存，相关记忆推荐未完成：" + str(exc))
            )
        return self._physical_result(
            _result(
                "SUCCESS",
                parts,
                {
                    "target": memory_target(record.fact_id),
                    "meta": {"updated_at": record.updated_at},
                },
            ),
            outcome,
        )

    async def _query_memory(self, arguments):
        if "target" in arguments:
            if set(arguments) != {"target"}:
                raise ValueError("Target精确查询只接受target。")
            identity = parse_memory_target(arguments["target"])
            item = await self._io.run(
                self._query.get,
                read_binding=self._read_binding,
                fact_id=identity,
                deadline_monotonic=self._canonical_deadline(
                    MEMORY_POINT_READ_TIMEOUT_SECONDS
                ),
            )
            if item is None:
                return _result(
                    "APPLICATION_ERROR", "这条记忆已不存在或不在当前可读范围。"
                )
            items = (item,)
        else:
            if (
                set(arguments) - {"query", "limit"}
                or "query" not in arguments
                or not isinstance(arguments["query"], str)
                or not arguments["query"].strip()
            ):
                raise ValueError("关键词查询须提供非空query和可选limit。")
            limit = arguments.get("limit", 5)
            if type(limit) is not int or limit < 1:
                raise ValueError("limit须为正整数。")
            items = await self._search_memories(arguments["query"].strip(), limit=limit)
        return _result(
            "SUCCESS",
            [ResultPart("object", item.value()) for item in items]
            or [ResultPart("text", "未找到相关记忆。")],
        )

    async def _manage_memory(self, arguments):
        if not {"target", "action"} <= set(arguments):
            raise ValueError("manage_memory须提供target和action。")
        identity = parse_memory_target(arguments["target"])
        action = arguments["action"]
        if action == "delete":
            if set(arguments) != {"target", "action"}:
                raise ValueError("delete只接受target和action。")
        elif action == "edit":
            if set(arguments) - {
                "target",
                "action",
                "statement",
                "context_kind",
            } or not ({"statement", "context_kind"} & set(arguments)):
                raise ValueError("edit至少提供statement或context_kind。")
            if "statement" in arguments and not isinstance(arguments["statement"], str):
                raise TypeError("statement须为字符串。")
        else:
            raise ValueError("action须为edit或delete。")
        timestamps = await self._io.run(
            self._repository.read_memory_result_timestamps,
            session_id=self._session_id,
            fact_ids=(identity,),
            deadline_monotonic=self._canonical_deadline(
                MEMORY_POINT_READ_TIMEOUT_SECONDS
            ),
        )
        state = self._installed_observation_reader()
        for item in (*(state.preferences or ()), *state.recall):
            if (
                item.get("target", {}).get("kind") == "MEMORY"
                and item["target"]["memory_id"] == identity
            ):
                stamp = memory_timestamp(item["updated_at"])
                if identity not in timestamps or timestamps[identity] < stamp:
                    timestamps[identity] = stamp
        if identity not in timestamps:
            return _result(
                "APPLICATION_ERROR", "请先用query_memory查询该记忆，再决定是否操作。"
            )
        common = dict(
            memory_domain_id=self._read_binding.memory_domain_id,
            fact_id=identity,
            expected_updated_at=timestamps[identity],
            read_binding=self._read_binding,
        )
        if action == "delete":
            outcome = await self._mutation(self._repository.delete_memory, **common)
            return self._physical_result(_result("SUCCESS", "已删除记忆。"), outcome)
        kwargs = {}
        if "statement" in arguments:
            kwargs["statement"] = arguments["statement"]
        if "context_kind" in arguments:
            kwargs["context_id"] = self._context_id(arguments["context_kind"])
        outcome = await self._mutation(
            self._repository.update_memory, **common, **kwargs
        )
        mutation = outcome.value
        if mutation.changed:
            self.offer_embedding_wake()
        return self._physical_result(
            _result(
                "SUCCESS",
                "已修改记忆。" if mutation.changed else "记忆未发生变化。",
                {
                    "target": memory_target(identity),
                    "meta": {"updated_at": mutation.record.updated_at},
                },
            ),
            outcome,
        )

    async def _search_memories(self, query, *, limit):
        deadline = self._canonical_deadline(MEMORY_SEARCH_TIMEOUT_SECONDS)
        vector = await self._query_embedding(query, automatic=False)
        count = max(limit, 20)
        sparse = await self._io.run(
            self._query.sparse_candidates,
            read_binding=self._read_binding,
            query=query,
            limit=count,
            automatic=False,
            deadline_monotonic=deadline,
        )
        dense = ()
        if vector is not None and self.embedding_connection() == vector.connection:
            try:
                dense = (
                    await self._io.run(
                        self._query.dense_candidates,
                        read_binding=self._read_binding,
                        vector=vector.vector,
                        embedding_contract=vector.connection.embedding_contract,
                        limit=count,
                        purpose=DenseRecallPurpose.EXPLICIT_SEARCH,
                        automatic=False,
                        deadline_monotonic=deadline,
                    )
                ).facts
            except Exception:
                pass
        candidates = self._query.fuse_candidates(sparse, dense)[:count]
        candidates = await self._io.run(
            self._query.canonical_refetch,
            read_binding=self._read_binding,
            ranked=candidates,
            deadline_monotonic=deadline,
        )
        ranked = await self._rerank_explicit(
            query,
            MemoryQueryResult(MemoryRetrievalDisposition.COMPLETE, candidates),
            total_deadline=deadline,
        )
        # One final lightweight read after any remote ranking; changed records
        # are omitted without looping or refilling the selected business scope.
        return await self._io.run(
            self._query.canonical_refetch,
            read_binding=self._read_binding,
            ranked=ranked.facts[:limit],
            deadline_monotonic=deadline,
        )

    async def _mixed_candidates(self, query, canonical_facts):
        scope = None
        if (
            self._settings.read().memory_retrieval.history_enabled
            and canonical_facts is not None
        ):
            scope = FrozenHistoryReadScope(
                self._session_id,
                self._read_binding.memory_domain_id,
                session_content_range(canonical_facts),
                tuple(
                    dict.fromkeys(
                        item.source_entry_id
                        for item in canonical_facts.canonical_input.items
                        if item.source_entry_id is not None
                    )
                ),
            )
        deadline = self._canonical_deadline(MEMORY_SEARCH_TIMEOUT_SECONDS)
        operations = [
            self._io.run(
                self._query.sparse_candidates,
                read_binding=self._read_binding,
                query=query,
                limit=20,
                automatic=True,
                deadline_monotonic=deadline,
            ),
            self._query_embedding(query, automatic=True),
        ]
        if scope is not None:
            operations.append(
                self._io.run(
                    self._history.automatic_candidates,
                    scope=scope,
                    query=query,
                    deadline_monotonic=deadline,
                )
            )
        tasks = tuple(asyncio.create_task(operation) for operation in operations)
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            pending = tuple(task for task in tasks if not task.done())
            for task in pending:
                task.cancel()
            if pending:
                await _join_remote_tasks(pending)
        sparse, vector = outcomes[:2]
        if isinstance(sparse, BaseException):
            raise sparse
        history = () if scope is None else outcomes[2]
        if isinstance(history, BaseException):
            raise history
        dense = ()
        if (
            not isinstance(vector, BaseException)
            and vector is not None
            and self.embedding_connection() == vector.connection
        ):
            try:
                dense = (
                    await self._io.run(
                        self._query.dense_candidates,
                        read_binding=self._read_binding,
                        vector=vector.vector,
                        embedding_contract=vector.connection.embedding_contract,
                        limit=20,
                        purpose=DenseRecallPurpose.AUTOMATIC_ROOT,
                        automatic=True,
                        deadline_monotonic=self._canonical_deadline(
                            MEMORY_SEARCH_TIMEOUT_SECONDS
                        ),
                    )
                ).facts
            except Exception:
                pass
        memory = await self._io.run(
            self._query.canonical_refetch,
            read_binding=self._read_binding,
            ranked=self._query.fuse_candidates(sparse, dense),
            automatic=True,
            deadline_monotonic=self._canonical_deadline(MEMORY_SEARCH_TIMEOUT_SECONDS),
        )
        memory = tuple(
            item for item in memory if _sensitive_profile_is_eligible(item, query)
        )
        pool = _elastic_candidates(memory, history, 15, 5)
        ranked = await self._rerank_explicit(
            query,
            MemoryQueryResult(MemoryRetrievalDisposition.COMPLETE, pool),
            total_deadline=self._canonical_deadline(MEMORY_SEARCH_TIMEOUT_SECONDS),
            purpose="automatic_recall",
        )
        selected = (
            ranked.facts[:5]
            if ranked.rerank_disposition == "APPLIED"
            else _elastic_candidates(
                tuple(
                    item
                    for item in ranked.facts
                    if not isinstance(item, HistoryCandidate)
                ),
                tuple(
                    item for item in ranked.facts if isinstance(item, HistoryCandidate)
                ),
                4,
                1,
            )
        )
        chosen_memory = tuple(
            item for item in selected if not isinstance(item, HistoryCandidate)
        )
        valid_memory = await self._io.run(
            self._query.canonical_refetch,
            read_binding=self._read_binding,
            ranked=chosen_memory,
            automatic=True,
            deadline_monotonic=self._canonical_deadline(MEMORY_SEARCH_TIMEOUT_SECONDS),
        )
        chosen_history = tuple(
            item for item in selected if isinstance(item, HistoryCandidate)
        )
        valid_history = (
            ()
            if scope is None or not chosen_history
            else await self._io.run(
                self._history.canonical_refetch,
                scope=scope,
                candidates=chosen_history,
                deadline_monotonic=self._canonical_deadline(
                    MEMORY_SEARCH_TIMEOUT_SECONDS
                ),
            )
        )
        return tuple(
            _history_value(item) if isinstance(item, HistoryCandidate) else item.value()
            for item in selected
            if (isinstance(item, HistoryCandidate) and item in valid_history)
            or (not isinstance(item, HistoryCandidate) and item in valid_memory)
        )

    async def prepare_memory_observation(
        self, query, *, previous_state, canonical_facts=None, trigger_policy=None
    ):
        trigger_policy = trigger_policy or self.classify_memory_trigger(query)
        if not trigger_policy.memory_use.allows_reads:
            return MemoryObservationCandidate((), previous_state)
        try:
            preferences = tuple(
                freeze_tool_json_object(item.value())
                for item in await self._io.run(
                    self._query.response_preference_snapshot,
                    read_binding=self._read_binding,
                    deadline_monotonic=self._canonical_deadline(
                        MEMORY_POINT_READ_TIMEOUT_SECONDS
                    ),
                )
            )
            old_memories = tuple(
                item
                for item in previous_state.recall
                if item.get("target", {}).get("kind") == "MEMORY"
            )
            current = await self._io.run(
                self._query.current_records,
                read_binding=self._read_binding,
                fact_ids=tuple(item["target"]["memory_id"] for item in old_memories),
                deadline_monotonic=self._canonical_deadline(
                    MEMORY_POINT_READ_TIMEOUT_SECONDS
                ),
            )
            selected = (
                ()
                if trigger_policy.automatic_recall
                is not AutomaticMemoryTriggerDisposition.ELIGIBLE
                else await self._mixed_candidates(query, canonical_facts)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return MemoryObservationCandidate(
                (MemoryObservationText("本次记忆信息暂不可用：" + str(exc)),), None
            )
        notices = []
        invalid = set()
        for item in old_memories:
            identity = item["target"]["memory_id"]
            now = current.get(identity)
            reason = (
                "该记忆已删除。"
                if now is None
                else "该记忆已不可用。"
                if not self._read_binding.can_read(now.context_id)
                else "该记忆已修改，先前内容已失效。"
                if freeze_tool_json_object(now.value()) != item
                else None
            )
            if reason:
                invalid.add(identity)
                notices.append(
                    {"target": item["target"], "title": item["title"], "notice": reason}
                )
        valid = tuple(
            item
            for item in previous_state.recall
            if item.get("target", {}).get("kind") != "MEMORY"
            or item["target"]["memory_id"] not in invalid
        )
        added = []
        for value in selected:
            value = freeze_tool_json_object(value)
            if value not in valid and value not in added:
                added.append(value)
        transmitted = None
        append = False
        if previous_state.preferences != preferences:
            prior = previous_state.preferences
            if prior is not None:
                prior_ids = [item["target"]["memory_id"] for item in prior]
                unchanged = [
                    item
                    for item in preferences
                    if item["target"]["memory_id"] in prior_ids
                ]
                append = tuple(unchanged) == prior
            transmitted = (
                tuple(item for item in preferences if item not in (prior or ()))
                if append
                else preferences
            )
        body = render_memory_observation(
            transmitted, append, tuple(added), tuple(notices)
        )
        return MemoryObservationCandidate(
            () if body is None else (MemoryObservationText(body),),
            MemoryObservationState(preferences, (*valid, *added)),
        )


def _result(state, content, fields=None):
    parts = (
        (ResultPart("text", content),) if isinstance(content, str) else tuple(content)
    )
    return KernelToolResult(state=state, content=ToolBusinessValue(fields or {}, parts))


def _rerank_text(item):
    return (
        json.dumps(item.projection(), ensure_ascii=False)
        if isinstance(item, HistoryCandidate)
        else item.retrieval_text
    )


def _history_value(item):
    return {
        **item.projection(),
        "target": session_entry_target(item.q1_entry_id),
        "messages": [
            {
                "target": session_entry_target(message["entry_id"]),
                **{key: message[key] for key in ("role", "label", "at", "text")},
            }
            for message in item.messages
        ],
    }


def render_memory_observation(preferences, append, recall, notices):
    chunks = []

    def emit(label, key, value):
        payload = [
            thaw_tool_json_object(freeze_tool_json_object(item)) for item in value
        ]
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        chunks.append(
            label
            + "\n"
            + key
            + ":\n"
            + "\n".join("  " + line for line in text.splitlines())
        )

    if preferences is not None:
        emit(
            "新增回答偏好" if append else "当前完整回答偏好",
            "response_preferences",
            preferences,
        )
    if recall:
        emit("新增召回内容", "recall", recall)
    if notices:
        emit("先前召回记忆的变化", "notices", notices)
    return "\n\n".join(chunks) if chunks else None


_SENSITIVE_PROFILE_RELEVANCE: tuple[tuple[frozenset[str], frozenset[str]], ...] = (
    (
        frozenset(
            {
                "health",
                "medical",
                "diagnosis",
                "allerg",
                "pregnan",
                "disability",
                "健康",
                "疾病",
                "诊断",
                "过敏",
                "怀孕",
                "残疾",
            }
        ),
        frozenset(
            {
                "health",
                "medical",
                "doctor",
                "medicine",
                "allerg",
                "food",
                "meal",
                "eat",
                "diet",
                "restaurant",
                "safe",
                "safety",
                "accurate",
                "健康",
                "医疗",
                "医生",
                "药",
                "过敏",
                "食物",
                "吃",
                "饮食",
                "餐",
                "安全",
                "准确",
            }
        ),
    ),
    (
        frozenset(
            {
                "address",
                "location",
                "postcode",
                "zip code",
                "住址",
                "地址",
                "位置",
                "定位",
                "邮编",
            }
        ),
        frozenset(
            {
                "address",
                "location",
                "direction",
                "route",
                "travel",
                "weather",
                "timezone",
                "shipping",
                "delivery",
                "地址",
                "位置",
                "路线",
                "出行",
                "旅行",
                "天气",
                "时区",
                "寄送",
                "配送",
            }
        ),
    ),
    (
        frozenset(
            {
                "identity",
                "passport",
                "phone",
                "email",
                "contact",
                "身份证",
                "护照",
                "电话",
                "邮箱",
                "联系方式",
            }
        ),
        frozenset(
            {
                "identity",
                "passport",
                "phone",
                "email",
                "contact",
                "account",
                "身份证",
                "护照",
                "电话",
                "邮箱",
                "联系",
                "账号",
            }
        ),
    ),
    (
        frozenset(
            {
                "religion",
                "politic",
                "sexual",
                "race",
                "ethnic",
                "宗教",
                "政治",
                "性取向",
                "种族",
                "民族",
            }
        ),
        frozenset(
            {
                "religion",
                "politic",
                "sexual",
                "race",
                "ethnic",
                "宗教",
                "政治",
                "性取向",
                "种族",
                "民族",
            }
        ),
    ),
)


def _sensitive_profile_is_eligible(item, query: str) -> bool:
    if item.fact_kind != MemoryFactKind.USER_PROFILE.value:
        return True
    statement = item.retrieval_text.casefold()
    lowered_query = query.casefold()
    matched_sensitive_category = False
    for sensitive_markers, relevance_markers in _SENSITIVE_PROFILE_RELEVANCE:
        if not any(marker in statement for marker in sensitive_markers):
            continue
        matched_sensitive_category = True
        if any(marker in lowered_query for marker in relevance_markers):
            return True
    return not matched_sensitive_category


async def _join_remote_tasks(tasks) -> asyncio.CancelledError | None:
    """Keep this owner's calls attached through SDK cleanup and repeated Stop."""
    joining = asyncio.gather(*tasks, return_exceptions=True)
    cancellation = None
    while not joining.done():
        try:
            await asyncio.shield(joining)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
    joining.result()
    return cancellation


def _elastic_candidates(memory, history, memory_quota, history_quota):
    total = memory_quota + history_quota
    m = min(len(memory), memory_quota)
    h = min(len(history), history_quota)
    m += min(len(memory) - m, total - m - h)
    h += min(len(history) - h, total - m - h)
    return (*memory[:m], *history[:h])
