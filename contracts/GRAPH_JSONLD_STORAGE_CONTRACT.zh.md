# Graph / JSON-LD / Ontology / Storage Contract

_Created: 2026-07-04_

本文档定义 Pulsara semantic graph substrate 的基础契约：typed entities 如何序列化为 JSON-LD、ontology/context 如何合并、GraphStore 如何隔离 named graph、PostgreSQL 如何同步投影 memory read models、Oxigraph 如何作为 SPARQL materialization 面。

相关代码：

- [src/pulsara_agent/jsonld/](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/jsonld)
- [src/pulsara_agent/ontology/](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/ontology)
- [src/pulsara_agent/entities/](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/entities)
- [src/pulsara_agent/graph/store.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/graph/store.py)
- [src/pulsara_agent/graph/postgres.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/graph/postgres.py)
- [src/pulsara_agent/graph/oxigraph.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/graph/oxigraph.py)
- [src/pulsara_agent/graph/jsonld_codec.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/graph/jsonld_codec.py)
- [src/pulsara_agent/storage/memory_schema.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/memory_schema.py)
- [src/pulsara_agent/storage/postgres_memory_projection.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/postgres_memory_projection.py)
- [tests/test_ontology_registry.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_ontology_registry.py)
- [tests/test_execution_evidence_ledger.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_execution_evidence_ledger.py)
- [tests/test_durable_memory.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_durable_memory.py)
- [tests/test_memory_schema.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_memory_schema.py)
- [tests/test_oxigraph_materializer.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_oxigraph_materializer.py)

---

## 1. 核心立场

Pulsara semantic graph substrate 有三层：

```text
typed entity dataclass
  -> compact JSON-LD document
  -> GraphStore storage / projections / materialization
```

这层 substrate 不决定 governance、permission、recall 或 lifecycle；它只负责：

- stable id；
- stable ontology terms；
- graph isolation；
- JSON-LD normalization；
- storage/projection consistency。

业务 authority 由上层契约决定：

- governed memory authority：governance + PostgreSQL UOW；
- runtime semantic authority：event-log-derived runtime projections；
- Oxigraph authority：异步 materialized read surface。

---

## 2. JSON-LD entity contract

所有 typed graph entity 必须序列化为 compact JSON-LD object。

最低字段：

- `@context`
- `@id`
- `@type`

规则：

- `JsonLdEntity.to_jsonld()` 必须输出 `@type` list。
- `Term.name` 是 compact field/type name；`Term.value` 是 absolute IRI。
- `NodeRef(id).to_jsonld()` 必须输出 `{"@id": id}`。
- 空 `Term.name`、空 `NodeRef.id` 必须拒绝。
- properties 中的 `NodeRef`、enum、datetime 等必须经 `jsonld_value(...)` 转成 JSON-LD 友好值。

Entity id 是跨 projection / outbox / graph 的稳定键。不得用数据库自增 id 替代 semantic id。

---

## 3. Ontology registry

`CORE_CONTEXT` 必须合并四个 ontology family：

- memory：`mem`
- context：`ctx`
- runtime：`rt`
- capability：`cap`

合并规则：

- 冲突 key 必须 raise；
- `graph` prefix 固定为 `https://pulsara.dev/graph/`；
- memory ontology 不得重新导出 runtime evidence/timeline terms；
- runtime ontology 与 memory ontology 必须保持 family 分离。

新增 term 时必须：

- 放入正确 ontology family；
- 更新 context；
- 添加 registry 测试；
- 若 term 进入 Postgres projection，同步更新 projection allowlist。

---

## 4. JSON-LD normalization / RDF codec

Graph stores 写入前必须 normalize compact JSON-LD。

规则：

- document 必须有非空 string `@id`。
- `graph_id=None` 统一映射到 `graph:default`。
- `graph_id=""` 必须拒绝。
- compact id 可按 context expand；未知 compact id fallback 为 `urn:pulsara:<quoted-id>`。
- `graph:<name>` 必须 expand 到 `https://pulsara.dev/graph/<name>`。
- `FORCE_LIST_KEYS` 中的关系字段 round-trip 时必须保持 list，即使只有一个元素。
- blank node 可以用于 event span / structured subobject，但不得成为 top-level entity id。

PostgresGraphStore 与 OxigraphGraphStore 的 JSON-LD round-trip 不要求 byte-for-byte 保留输入顺序，但必须保留语义字段、id、type 与 forced-list shape。

---

## 5. GraphStore protocol

`GraphStore` 提供 semantic graph boundary：

- `put_jsonld(document, graph_id=None)`
- `get_jsonld(node_id, graph_id=None)`
- `has_jsonld(node_id, graph_id=None)`
- `find_by_type(type_name, graph_id=None)`
- `query(sparql, bindings=None)`
- `update(sparql)`
- `delete_graph(graph_id)`

Named graph 隔离是硬契约：

- default graph lookup 不扫描 named graphs；
- named graph lookup 不 fallback 到 default graph；
- 同一个 node id 可以同时存在于 default 与 named graph，payload 可不同；
- `delete_graph(graph_id)` 只删除该 graph 的 documents/projections/outbox rows。

---

## 6. PostgreSQL GraphStore

`PostgresGraphStore` 是生产 canonical substrate 的同步写面。

写入 `put_jsonld(...)` 必须在同一 cursor/transaction 中：

1. normalize JSON-LD；
2. upsert `graph_documents(graph_id, id, type, payload)`；
3. refresh typed memory projections。

Projection tables：

- `memory_nodes`
- `memory_relations`

Projection refresh 规则：

- 非 governed memory entity 不进入 `memory_nodes`；
- 缺少 required memory projection keys 的 document 不进入 `memory_nodes`；
- relation rows 只投影 allowlisted predicates；
- 每次 document refresh 先删除该 source 的旧 relation rows，再插入新 relation rows；
- `set_status(...)` 必须更新 JSON-LD payload 与 `memory_nodes` projection。

`PostgresGraphStore.query/update` 不提供 raw SPARQL；typed reads 走 `MemoryQuery` / recall read models。

---

## 7. Memory substrate schema

Memory substrate 的 PostgreSQL 初始化分成两个权限边界：

- `MEMORY_SUBSTRATE_BOOTSTRAP_SQL` 是 deployment/admin owner，只安装 pgvector；
- `MEMORY_SUBSTRATE_SCHEMA_SQL` 是普通应用角色 owner，只验证 prerequisite 并创建/升级 Pulsara 自有 function、table 与 index。

它必须包含：

- `graph_documents`
- `memory_nodes`
- `memory_relations`
- `memory_write_outbox`
- `memory_search_index`
- `memory_vector_index`
- `recall_traces`
- `recall_usages`

`CREATE EXTENSION IF NOT EXISTS vector` 必须只存在于 versioned bootstrap SQL 与部署初始化 artifact 中。Host、repository、worker 和普通 schema ensure 路径不得执行它，也不得要求 `PULSARA_POSTGRES_DSN` 角色拥有 `CREATE EXTENSION` 权限。Bundled Docker 在 fresh database init 时执行 bootstrap；existing/managed PostgreSQL 由管理员在目标 database 一次性执行同一声明式 SQL。Runtime schema 在 extension 缺失时必须以稳定 prerequisite error fail closed，不能退化为无向量表的半初始化状态。

`memory_vector_index` primary key 必须包含：

- graph id；
- memory id；
- embedding fingerprint。

这保证 schema 允许未来 embedding model isolation。

---

## 8. Oxigraph GraphStore

`OxigraphGraphStore` 是 SPARQL materialization 面。

规则：

- `put_jsonld(...)` 用 SPARQL update 先删除同 subject 旧 triples，再插入新 triples。
- `get_jsonld(...)` 从 named graph 查询 subject triples 并重建 compact JSON-LD。
- `has_jsonld(...)` 使用 ASK。
- `find_by_type(...)` 使用 RDF type 查询。
- `query(...)` 返回 JSON-LD-like bindings。
- `bindings` 当前不支持，传入必须 raise `NotImplementedError`。
- HTTP error 必须带 response body 进入 RuntimeError，便于诊断。

Oxigraph 不得成为 governed memory 的同步写 authority；它只由 outbox/materializer/reconcile 路径维护。

---

## 9. InMemory graph boundary

InMemory graph store 只允许测试/兼容使用。

它必须遵守与 GraphStore 一致的 named graph 语义：

- `None` -> default graph；
- empty graph id rejected；
- default/named graph 隔离；
- `find_by_type` 返回 defensive copies；
- `delete_graph` 只删除目标 graph。

它不得成为 production fallback。

---

## 10. 禁止事项

- 不允许空 `@id` / 空 `graph_id` 进入 graph store。
- 不允许 default graph lookup 扫描 named graph。
- 不允许 memory/runtime/capability/context ontology family 混写 term。
- 不允许把 Oxigraph 当作生产 governed memory 同步写入口。
- 不允许 PostgresGraphStore raw SPARQL update 绕过 typed mutation path。
- 不允许非 governed memory entity 进入 `memory_nodes`。
- 不允许 relation projection 包含未 allowlist 的任意 JSON-LD field。
- 不允许新增 schema table 后不更新 schema tests 与契约。

---

## 11. 测试守护

最低测试门槛：

- `CORE_CONTEXT` 包含 memory/context/runtime/capability/graph prefix。
- ontology families 的 type IRI 分离。
- memory ontology 不导出 runtime evidence/timeline terms。
- `NodeRef` / `Term` 拒绝空值。
- default graph 与 named graph put/get/has/find/delete 隔离。
- `graph_id=None` 写 default graph。
- `graph_id=""` 被拒绝。
- `find_by_type` 返回 defensive copies。
- PostgresGraphStore 写 canonical memory 后 `graph_documents` 与 `memory_nodes` 同步。
- `set_status` 同步 JSON-LD payload 与 projection。
- memory bootstrap 声明 pgvector extension，runtime schema 包含 prerequisite guard 与 required tables，且生产 runtime 不引用 privileged bootstrap SQL。
- Oxigraph materializer 能 round-trip JSON-LD document。
- Oxigraph unavailable/failure 通过 outbox failed status 暴露，而不是悄悄吞掉。
