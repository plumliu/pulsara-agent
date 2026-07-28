# Package Dependency 与 Ports Contract

_Created: 2026-07-26_

_D4 hard cut: 2026-07-26_

本文档冻结 D4 后的 source-level dependency rule、process-local port ownership和 test-support
边界。它约束 Python import graph，不宣称所有历史 package SCC 已在 D4 消失。

---

## 1. 分层

```text
primitives / message schema
        ↓
event schema
        ↓
replay / reducers
        ↓
process-local ports
        ↓
capability contracts + concrete tools
        ↓
runtime domain services + ToolExecutor
        ↓
host / cli / inspector composition
```

`host/composition_contract.py` 是明确的 composition-boundary exception：它可以同时引用 Host
resource与runtime wiring，但只能由 HostCore持有，不能下传给 RuntimeSession、AgentRuntime或tool。

---

## 2. D4 Target DAG

以下 forbidden edge必须为零，包括 module/global/local import、`TYPE_CHECKING`、
`importlib.import_module()`、`__import__()`和 package `__getattr__`：

1. `tools -> runtime|host|cli|inspector`；
2. `capability -> concrete runtime|tools`；
3. `event -> replay|runtime|tools|host`，`message -> replay`；
4. `storage|graph|memory -> runtime.projection_jobs`；
5. `src -> tests`。

补充 hard cuts：

- `ports -> runtime|tools|host|cli|inspector`；
- top-level `projection_jobs` contract不得依赖runtime projection implementation；
- primitive memory candidate不得依赖event wrapper；
- runtime不得导入已删除的 `tools.base` / `tools.executor`。

Scanner对每个 import生成 canonical observation。Identity覆盖source/target module、import kind、
enclosing qualname、normalized AST fingerprint和相同 AST occurrence ordinal；文件行列只属于
attribution，不进入semantic identity。新增同一 residual SCC package pair下的 module edge也会失败。

---

## 3. Ports Ownership

`src/pulsara_agent/ports/` 只保存 process-local closed contracts：

- `tool_execution.py`：ToolCall/result/runtime context；
- `tool_registry.py`：origin-aware builtin/MCP/custom binding union；
- `tool_result_semantics.py`：render/result contract；
- `artifact.py`：artifact read与frozen processing policy；
- `terminal.py`：terminal/process/monitor closed request/outcome；
- `mcp.py`：MCP execute/resume与pending handle contract；
- `subagent.py`：九类command与action-specific outcome；
- `projection_jobs.py`：lower-layer projection/mutation ports。

Port规则：

- 不接受全能 `RuntimeSession` / `HostSession` / `AgentRuntime`；
- 不暴露 generic SQL、raw connection、cursor、supervisor、manager或coordinator；
- request/outcome使用closed enum/discriminated union，不用 `dict[str, object]`代替核心 contract；
- physical live handle必须process-local、borrower-scoped、generation-aware，离开owner scope后fail closed；
- semantic fingerprint只覆盖递归immutable语义字段，不覆盖live object或physical attribution；
- concrete implementation由唯一 composition root注入。

---

## 4. 类型 Owner

D4 已完成 hard cut，不保留 temporary re-export：

- tool execution DTO owner：`ports.tool_execution`；
- terminal public request/outcome owner：`ports.terminal`；
- subagent closed vocabulary owner：`primitives.subagent` / `ports.subagent`；
- projection durable facts owner：top-level `projection_jobs`；
- current ToolResult receipt owner：`replay.tool_result_receipts`，且仍为process-local；
- canonical mutation pure factory owner：`projection_jobs.canonical_mutation`；
- PostgreSQL repository owner：`runtime.projection_jobs.postgres_canonical_mutation_repository`；
- ToolExecutor owner：`runtime.tool_executor`；
- immutable graph relation lowering owner：`graph.projection_relations`。

旧 owner module物理删除。Class/enum/protocol以 module/qualname/identity守卫；assignment/union alias以
唯一 AST owner、exact import identity和canonical origin/args shape守卫，不把typing alias误判为class。

---

## 5. Memory UOW

Memory UOW从 owner-scoped transaction scope取得六个 facade：graph、decision、event outbox、
mutation outbox、lifecycle、write service。全部 facade共享 exact scope lease与physical transaction，
每次操作重验scope/owner/generation/facade kind。

退出顺序固定：停止新borrow，drain in-flight，revoke六个facade与mutation port，commit/rollback，
最后归还connection。公开 bundle不含raw connection或可长期保存的concrete repository；保留任一
facade到scope外都必须在SQL前fail closed。

---

## 6. MCP Stable Candidate Ownership

MCP port-owned pending handle只拥有raw request/state、pending lease与manager-call lifecycle。
`ToolExecutionTerminalRegistry`唯一拥有suspension/terminal stable `AgentEvent` tuple和exact retry。

- manager active borrow总在physical call `finally`归还；
- durable FULL/NONE/UNKNOWN/PARTIAL只控制长期pending lease；
- NONE由registry以相同candidate retry；
- FULL receipt exact join physical handle后，port settle并返回handoff receipt，registry才可清除；
- UNKNOWN/PARTIAL同时保留两个owner并latch；
- Host close必须按registry candidate、MCP physical lease的冻结顺序drain。

---

## 7. Host 与 Test Support

Production Host唯一入口是 `HostCore.production()`，唯一实现是 durable
`ProductionHostComposition`。不存在 `durable` selector、in-memory product branch或production fake。

Host build分成：

- immutable `HostRuntimeBuildFact`：只含semantic值/fingerprint；
- process-local `HostRuntimeLiveBindings`：exact object/identity/generation；
- process-local `HostRuntimeBuildAdmission`：operational CAS与deadline；
- borrower-scoped `HostProcessResourceLease`：同一attempt的PostgreSQL、retrieval、projection与governance。

Live carrier不可pickle、copy、`dataclasses.asdict()`或event serialize。Production composition拒绝
`durability_evidence=False` test lease。

Whole in-memory runtime、component Host、Mock MCP和fake governance UOW只在 `tests/support`；
`src/`不得引用它们。Unit/component tests可使用test composition，但不得把它当作durability evidence。

---

## 8. 验证

强制 gate：

- `tests/test_dependency_architecture.py`；
- `tests/test_d4_type_owner_identity.py`；
- `tests/test_d4_port_contracts.py`；
- `tests/test_memory_uow_transaction_scope.py`；
- `tests/test_tool_execution_stable_candidate_owner.py`；
- `tests/test_host_composition_contract.py`；
- `tests/test_package_facade.py`。

Residual package SCC只作为 D6 diagnostic baseline；D4/D5完成声明不得写成“全仓库跨package SCC
已消除”。

## 8. D5 compaction-memory direction

`runtime.compaction`只消费`ports.compaction_extensions`的低层extension intent/commit contracts，不得
import `memory.candidates`、`memory.governance`、memory ontology或`memory.compaction` concrete module。
Memory-owned manifest/evidence/parser/result contract与driver/settlement support facade位于
`memory.compaction`；需要RuntimeSession、D3 repository或PostgreSQL transaction的physical driver、budget
与settlement adapter位于`runtime.projection_jobs`并只向下消费这些memory contracts。Purpose-neutral model
lifecycle seam位于`ports.model_lifecycle`，`llm.commit`不得import/downcast memory/job/account DTO。

Durable projection facts继续由top-level`projection_jobs`拥有，PostgreSQL implementation位于
`runtime.projection_jobs`。Live handle必须是frozen dataclass，不能进入Pydantic/event serialization；
prepared Request只保存`FrozenEventWriteCandidate`。
