# Retrieval Runtime Resources Contract

_Created: 2026-07-04_

本文档定义 Pulsara retrieval-side 资源生命周期契约。这里的 retrieval 包括 embedding、rerank、tokenizer 以及与它们共享生命周期的 background workers。

相关代码：

- [src/pulsara_agent/retrieval/runtime.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/retrieval/runtime.py)
- [src/pulsara_agent/retrieval/config.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/retrieval/config.py)
- [src/pulsara_agent/retrieval/embedding/protocol.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/retrieval/embedding/protocol.py)
- [src/pulsara_agent/retrieval/rerank/protocol.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/retrieval/rerank/protocol.py)
- [src/pulsara_agent/host/core.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/core.py)
- [tests/test_retrieval_runtime.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_retrieval_runtime.py)

---

## 1. 核心立场

Retrieval providers 是 async resource，不属于单个 tool call 或单个 HostSession。

所有权：

```text
HostCore
  owns RetrievalRuntimeResources
    owns embedding provider
    owns rerank provider
    owns retrieval workers
HostSession / AgentRuntime / recall service
  borrow providers
```

HostSession 不得关闭 embedding/rerank provider。

---

## 2. Provider protocols

Embedding provider contract：

- `model_id`
- `dimensions`
- `embed(text)`
- `embed_batch(texts)` preserving input order
- `aclose()`

Rerank provider contract：

- `model_id`
- `rerank(query, documents, instruction=None, top_n=None)` returning `(index, score)` pairs
- `aclose()`

Provider 必须是 async-safe 的调用对象；调用方不得在同步 worker thread 中共享 event-loop-bound client，除非通过明确的 async bridge。

---

## 3. Runtime resources

`RetrievalRuntimeResources` 是 provider 与 worker 的 lifecycle owner。

规则：

- workers 必须在 `start()` 前 attach。
- `start()` 幂等，负责创建 worker tasks。
- `create_task()` 在 closed 后必须关闭 coroutine 并失败。
- `wake_workers()` 在 closed 后 no-op。
- `aclose()` 幂等。

关闭顺序：

1. 标记 closed。
2. 反向关闭 workers。
3. 等待 live worker tasks；超时则 cancel。
4. 最后关闭 rerank provider 与 embedding provider。

这保证 worker 不会在 provider 已关闭后继续 embed/rerank。

---

## 4. HostCore ownership

`HostCore` 懒加载 retrieval resources：

- 第一次 open/resume durable session 需要 retrieval 时构建。
- 同一个 HostCore 内多个 HostSession 共享同一份 resources。
- HostCore shutdown 关闭 resources，并清空 owner 引用。
- HostCore closing 后不得启动新的 retrieval resources。

HostCore 可以 attach：

- `MemoryGovernanceCoordinator`
- `MemoryVectorIndexWorker`（仅当 embedding provider 存在）

---

## 5. Missing provider semantics

缺少 embedding/rerank API key 不应让非 retrieval 测试或非 retrieval 基础路径失败。

规则：

- `build_retrieval_runtime_resources()` 在缺少 key 时返回 `embedding=None` / `rerank=None`。
- sparse recall 仍可工作。
- dense/vector/rerank channel 必须降级，而不是整体崩溃。
- 生产 durable wiring 仍要求 PostgreSQL 与非空 Oxigraph URL；这属于 storage/canonical graph 契约，不由 retrieval provider key 决定。

---

## 6. Vector surface delivery

Vector materialization由process-owned durable projection service中的canonical mutation V2
surface handler拥有，不再有retrieval-private vector worker或outbox claim。

- Governance UOW只在resolved surface plan包含vector时创建delivery state；
- handler从HostCore-owned embedding provider借用调用能力；
- external embedding I/O期间不持PostgreSQL row lock；
- wake只唤醒durable projection service；
- 没有embedding provider时surface plan不得产生vector delivery。
- HostCore close必须先停止projection admission并drain tracked physical operations；close deadline
  到期但operation仍持有dependency时，Host保持`CLOSING`/`CLOSE_BLOCKED`并保留
  PostgreSQL/embedding/Oxigraph/retrieval leases；它必须拒绝新session与新borrower，不能恢复
  `OPEN`，也不能先释放provider再让后台operation继续。Physical owner收口后，显式重试close才可
  完成资源释放。

---

## 7. 禁止事项

- 不允许每个 tool call 创建/关闭 embedding client。
- 不允许 HostSession close 关闭 HostCore-owned retrieval providers。
- 不允许 provider 在 worker 关闭前被关闭。
- 不允许projection close只记录timeout diagnostic后继续释放dependency。
- 不允许 closed resources 接受新 task。
- 不允许缺少 reranker 时把整个 recall/governance relatedness 判为 fatal。
- 不允许在没有embedding provider时产生vector surface delivery。

---

## 8. 测试守护

最低测试门槛：

- resources share workers and providers across sessions。
- close exactly once and idempotent。
- hung tasks are cancelled with bounded shutdown。
- workers must be attached before start。
- create_task after close fails and closes coroutine。
- HostCore shares one durable projection service and one embedding provider across sessions。
- missing provider degrades dense/rerank path without breaking sparse path。
