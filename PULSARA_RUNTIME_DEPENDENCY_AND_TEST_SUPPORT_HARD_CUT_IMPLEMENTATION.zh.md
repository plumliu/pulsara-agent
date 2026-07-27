# Pulsara Runtime Dependency Rule 与 Test-Support Hard Cut 实施规格

_状态：D4-0 至 D4-5 已完成（2026-07-26）_

_起草日期：2026-07-26_

_债务编号：`D4`_

本文档冻结 `PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md` 中 D4 的代码落地方案：

1. 建立可执行的 package dependency rule；
2. 建立低层 contracts/ports，切断 `runtime` 与 `tools` 的 concrete 双向依赖；
3. 收回 tool execution、terminal、MCP、subagent 与 artifact 的 composition ownership；
4. 将 `MockMcpClientManager`、in-memory runtime wiring 与 in-memory governance UOW 移入 `tests/support`；
5. 删除 production `HostCore.durable`、`build_agent_runtime_wiring(..., durable=...)` 与所有 selectable in-memory product branch；
6. 删除为规避 import cycle 而存在的 `runtime` / `tools` lazy facade；
7. 将 builtin descriptor、permission、recovery、Long-Horizon action taxonomy 收敛为一个生产 catalog。

这不是目录整理，也不是兼容性重构。D4 是一次 source-level hard cut：旧 import、旧 factory、旧 constructor 参数在切换后直接不存在，不提供 deprecation shim、historical decoder allowlist 或运行时 fallback。

---

## 0. 结论先行

当前真实问题不是“有几个局部 import cycle”，而是四个 ownership 边界叠在一起：

```text
RuntimeSession
  ├─ 构造 concrete built-in tools
  ├─ 构造 ToolExecutor
  ├─ 暴露 terminal / artifact / subagent / MCP concrete owner
  └─ 被 tools 反向持有或读取

tools
  ├─ 定义 ToolCall / ToolExecutionResult 等共享 contract
  ├─ 定义 concrete tool
  ├─ 定义 ToolExecutor runtime orchestration
  └─ 反向导入 runtime services
```

因此目标不能只是把 local import 移到文件顶部。最终边界必须是：

```text
primitives + message/event schema
                ↓
        process-local ports
                ↓
 capability contracts + concrete tools
                ↓
 runtime domain services + ToolExecutor
                ↓
      host/cli composition roots

tests/support
    └─ 只在测试侧实现 fake ports/composition
```

核心选择如下：

- `ToolCall`、`ToolExecutionResult`、`ToolRuntimeContext` 等不再由 concrete `tools` package 反向拥有；它们进入 `ports/tool_execution.py`。
- `ToolExecutor` 是 runtime orchestration，不是 tool implementation；它移入 `runtime/tool_executor.py`。
- concrete built-in tools 不接收 `RuntimeSession`、`PermissionState`、`TerminalSessionManager`、`McpServerSupervisor` 或 `SubagentRuntime`；它们只接收 closed protocol。
- `RuntimeSession.create_tool_executor()` 删除；唯一 composition owner 是 `runtime/tool_composition.py`。
- production `HostCore` 不再有 durable/non-durable 分支；测试使用显式 test composition，不通过产品布尔值选择 fake architecture。
- `runtime/__init__.py` 与 `tools/__init__.py` 不再承担大型 convenience facade。调用方改用 owning module 的 direct import。

### 0.1 反向审查闭环索引

| 审查问题 | 本规格冻结位置 |
|---|---|
| 全局SCC与D4范围矛盾 | 3.2只gate五组target DAG；3.3冻结全局SCC diagnostic与短期cutover ledger |
| D3同事务port不可执行 | 5.8.1 sealed MEMORY_UOW transaction borrow、closed driver与release lifecycle |
| canonical mutation repository无迁移落点 | 5.8.2 pure factory / PostgreSQL repository / writer三文件拆分 |
| MCP exact binding只能从concrete tool读取 | 5.3 origin-aware binding union与registry exact identity |
| artifact port缺descriptor policy | 5.4 resolved artifact processing policy与三个immutable view |
| builtin permission默认值双真源 | 7节由descriptor独占defaults，catalog只保存action/terminal rule |
| ToolResult receipt被误称durable event | 8.3迁到process-local `replay/tool_result_receipts.py` |
| 中央DTO只有名字 | 5.3、5.4、5.6、5.8.3、7、9.4冻结closed字段、validator与identity规则 |
| Memory UOW同一physical transaction仍无公开可调用协议 | 5.8.1冻结owner-scoped `MemoryUowTransactionScope`与同connection repository bundle；mutation port只保留append |
| MCP completed outcome丢失application error与metadata | 5.6补齐`result_state`、`normalized_is_error`与递归冻结metadata，并冻结一致性矩阵 |
| Host manifest查询维度丢失 | 9.4保留`workspace_root`、`memory_domain_id`、`include_closed`、`limit`四个参数 |
| terminal三个port只有名称 | 5.5冻结command、8种process action、3种monitor action的完整request/outcome union与prepared owner join |
| subagent九种command/outcome只有名称 | 5.7冻结九种request DTO、action-specific success payload与rejected/not-ready分支 |
| SCC baseline只能发现package edge增长 | 3.3以canonical AST import observation为增长真源；package edge/SCC仅为派生诊断 |
| Host build request混入live object | 9.4拆成immutable `HostRuntimeBuildFact`与process-local `HostRuntimeLiveBindings`，只对前者计算语义fingerprint |
| Memory UOW repository对象可在scope外继续使用raw connection | 5.8.1增加共享`MemoryUowScopeLease`与六个borrower facade；先撤销并drain全部facade，再commit/rollback与归还connection |
| MCP port不能独立持有resume payload或按durable commit结果settle lease | 5.6增加port-owned pending handle、registry commit receipt/physical handoff seam与完整FULL/NONE/UNKNOWN/PARTIAL矩阵 |
| terminal/subagent closed outcome仍含自由字符串与重复owner字段 | 5.5、5.7下沉closed vocabulary，monitor outcome只引用prepared carrier，不再复制其事实 |
| D4-0 additive DTO会形成shadow Python class identity | 12节将D4-0冻结为最终类型所有权迁移；旧路径只能exact import/re-export，D4-2/D4-3只切行为与删除临时路径 |
| type-ownership gate把TypeAlias/union alias误当class | 3.4增加`symbol_kind`互斥矩阵；class-like校验module/qualname，alias-like校验唯一AST owner与canonical shape |
| MCP physical handle被错误要求拥有stable event candidate | 5.1与5.6冻结`ToolExecutionTerminalRegistry`为唯一candidate owner；registry receipt exact join后才通知MCP port settle lease |

---

## 1. 范围与非目标

### 1.1 本阶段必须完成

- runtime/tools concrete cycle被切为runtime composition到concrete tools的单向依赖；
  `tools -> runtime`反向边归零；
- built-in tool constructor 中的 `RuntimeSession` 归零；
- `tools` 到 `runtime` 的 import 归零，包括 `TYPE_CHECKING` import；
- message schema 与 event replay/reducer 分层；
- primitives 到 event candidate 的反向边归零；
- capability provider 到 runtime/tools concrete implementation 的反向边归零；
- production selectable in-memory composition 归零；
- production mock class 归零；
- package dependency rule 进入普通 `pytest` gate；
- package facade 长期契约同步更新；
- D4 在债务文档中只在最终 gate 全绿后标记 `CLOSED`。

### 1.2 本阶段明确不做

- 不拆分 `AgentRuntime` / `HostSession` 的状态机；那属于 D6；
- 不迁移 `LoopState.scratchpad` owner；
- 不修改 provider-visible tool schema、tool name、tool result render semantics 或 permission 行为；
- 不修改 durable event schema、event type、event fingerprint 或 PostgreSQL migration registry；
- 不重做 terminal monitor、MCP lease、subagent lifecycle 或 ProviderInput coordinator；
- 不关闭 D5 compaction-memory extension；
- 不因为“文件太长”而顺带拆分无关模块；
- 不禁止单元测试直接使用 `InMemoryEventLog`、`InMemoryGraphStore` 等低层 deterministic fake。禁止的是 production composition root 选择整套 fake runtime。

### 1.3 行为冻结

D4 前后以下 observable behavior 必须一致：

- builtin tool names、descriptions、JSON Schema、binding fingerprint；
- capability descriptor fingerprint；
- permission ALLOW / WAIT / DENY 结果；
- Long-Horizon action classification；
- ToolResult event batch、terminal projection 与 artifact semantics；
- MCP suspension/resume lifecycle；
- terminal completion reservation、monitor registration/cancel；
- subagent parent/child tool exposure；
- durable Host open/resume/close 与 manifest 行为。

如果上述 fingerprint 因“只移动 Python 类型”而变化，视为 contract drift，必须 fail gate，而不是更新 golden 值掩盖。

---

## 2. 当前代码真值

以下盘点以 2026-07-26 `main` 为准。

### 2.1 runtime/tools 是真实双向依赖

当前有 8 个 `tools` 文件直接导入 concrete runtime：

| 文件 | 当前反向依赖 |
|---|---|
| `tools/executor.py` | `runtime.tool_artifacts.ToolResultArtifactService` |
| `tools/adapters/mcp.py` | `runtime.mcp.supervisor`、`runtime.mcp.types` |
| `tools/builtins/artifact.py` | 整个 `RuntimeSession` |
| `tools/builtins/registry.py` | `RuntimeSession`、`PermissionState` |
| `tools/builtins/terminal.py` | permission、terminal manager/model、artifact helpers、notification account |
| `tools/builtins/terminal_process.py` | permission、terminal manager/status/process error、terminal risk |
| `tools/builtins/terminal_monitor.py` | permission、terminal manager、notification/monitor coordinator |
| `tools/builtins/subagent.py` | 整个 `SubagentRuntime` 与 runtime exceptions/types |

反方向至少有 6 个 runtime 文件导入 tools contract、registry 或 executor：

| 文件 | 当前依赖 |
|---|---|
| `runtime/agent.py` | tools facade 的 `ToolCall`、result、executor、context |
| `runtime/session.py` | `Tool` / `AsyncTool`，并在 `create_tool_executor()` 中 local import concrete registry/executor |
| `runtime/tool_loop.py` | tools facade 与 `tools.executor` |
| `runtime/permission.py` | `tools.base.ToolCall` |
| `runtime/tool_action.py` | `tools.base.ToolCall` |
| `runtime/tool_artifacts.py` | `tools.base` result/candidate DTO |

`tools/__init__.py` 的模块注释已经直接承认 lazy facade 是为了规避这些 cycle；`RuntimeSession.create_tool_executor()` 的 local import 是同一问题的另一种表现。

### 2.2 `RuntimeSession` 被当成 built-in service locator

`tools/builtins/registry.py::build_core_tool_registry(runtime_session, ...)` 当前从一个参数读取：

- workspace root；
- runtime session ID；
- artifact archive/index；
- terminal manager 与 Host/conversation owner；
- terminal notification account；
- terminal monitor coordinator；
- subagent runtime；
- `default_event_metadata["subagent"]` 自由字典；
- extra MCP/custom tool bindings。

`ArtifactReadTool` 更直接持有整个 `RuntimeSession`。这使 tool 可以读取任何未来新增 session field，也使授权依赖无法由 constructor type 证明。

### 2.3 ToolExecutor 放错 ownership 层

`tools/executor.py` 不只是调用 tool。它负责：

- `ToolResultStartEvent` / text delta；
- cancellation/error normalization；
- result semantics builder；
- artifact persistence；
- terminal prepared result；
- runtime event recorder；
- async/sync execution boundary。

这些都是 runtime lifecycle orchestration。把它放在 `tools` 后，必然要求 `tools` 反向导入 runtime writer/artifact owner。

### 2.4 capability/tool taxonomy 有多份真源

当前 tool-name 语义散落在：

- `capability/builtin_provider.py::_BUILTIN_DESCRIPTORS`；
- `tools/builtins/registry.py` 的手写注册顺序；
- `runtime/permission.py::READ_ONLY_ALLOWED_TOOL_NAMES` 与 action sets；
- `runtime/tool_taxonomy.py` 的 file/terminal/plan/subagent sets；
- `runtime/tool_action.py::builtin_tool_action_policy()` 的 name switch；
- `runtime/recovery.py::_classify_severity()`；
- `capability/result_contracts.py::builtin_result_render_contract()`。

现有 drift tests 只能覆盖局部集合相等，不能证明新增工具在每个 owner 中都完成注册。

### 2.5 message/event/replay 仍形成 schema 反向边

- `event/events.py` 合法依赖 `message.blocks`；
- 但 `message/assembler.py` 与 `message/reducer.py` 又依赖 `event.events`；
- `event_log/in_memory.py` 与 `event_log/postgres.py` 直接依赖 `message.reducer`；
- `primitives/governance_evidence.py` 反向依赖 `event.candidates.CandidatePayload`；
- `primitives/runtime_event_vocabulary.py` 嵌入 `ToolResultBlock`。

因此 `message` 目录同时拥有 schema 与 event replay，package name 无法证明依赖方向。

### 2.6 selectable in-memory product mode 仍存在

生产 API 当前公开：

- `HostCore(durable: bool = True)`；
- `build_agent_runtime_wiring(..., durable: bool)`；
- `build_in_memory_runtime_wiring()`；
- `InMemoryMemoryWriteUnitOfWork`；
- runtime lazy facade 的 `build_in_memory_runtime_wiring` export；
- `runtime.mcp.MockMcpClientManager` export。

`HostCore` 内至少 12 个 durable 条件分支覆盖 rollout feasibility、resume、PostgreSQL/retrieval/projection acquisition、manifest publication、close 与 inspector path。这不是一个测试参数，而是两套产品 architecture 共用一个 composition root。

### 2.7 现有测试依赖面

- 2 个大 Host 测试模块直接构造 `HostCore(durable=False)`，其 helper 被大量用例复用；
- 4 个测试模块直接使用 `build_in_memory_runtime_wiring()`；
- MCP 测试从 production package 导入 `MockMcpClientManager`；
- `tests/support/runtime_session.py` 已经是显式 in-memory session factory，但仍借用 production in-memory artifact index；
- `tests/support/memory_uow.py` 仍继承 production `InMemoryMemoryWriteUnitOfWork`。

因此 hard cut 必须先提供 test-side replacement，再删除 production branch；不能反过来让大量组件测试临时依赖 PostgreSQL。

### 2.8 D3 已闭环行为，但留下 lower-layer -> runtime contract 边

D3 的 durable projection 行为已经闭环；D4 不重新设计 job、receipt、surface 或 cutover
状态机。但当前物理归属仍把低层 schema/adapter 反向连到
`runtime/projection_jobs`：

| 当前 importer | 当前被反向依赖的 runtime owner |
|---|---|
| `storage/session_bootstrap.py`、`storage/runtime_write_admission.py` | `runtime/projection_jobs/contracts.py` |
| `storage/migrations/runner.py` | contracts、migration state、pre-activation、migration transform |
| `graph/postgres.py`、`graph/durable_facade.py` | contracts、graph relation lowering、mutation writer |
| `memory/canonical/unit_of_work.py` | contracts、concrete mutation writer |
| `memory/governance/executor.py` | contracts |

这批边不能以“D3 已完成”为由长期放进 exception。D4 必须只移动 ownership，保持
D3 durable schema/fingerprint与事务语义不变：

- event-safe projection DTO移到 top-level `projection_jobs/contracts.py`；
- process-local commit/read/migration protocol移到 `ports/projection_jobs.py`；
- graph relation lowering由 `graph/projection_relations.py` 拥有；
- storage migration runner只依赖 migration port，不再 local import runtime implementation；
- memory UOW只接收borrower-scoped `CanonicalMutationCommitPort`，graph非UOW路径只接收
  process-scoped `CanonicalMutationWriterPort`；二者都不接收concrete writer。

`runtime/projection_jobs` 继续拥有 worker、repository、handler与service实现；D4 不删除或
改写这些状态机。

### 2.9 D3 transaction seam 与文件真值

进一步静态审阅确认：

- `VerifiedPostgresTransactionHandle` 仅暴露schema binding、owner、generation与borrower四个
  字符串identity，没有任何合法方式绑定或使用UOW的physical connection；
- `MemoryWriteUnitOfWork` 明确在一条`psycopg.Connection`内写decision、graph与outbox；
- `OutboxRepository.append_decision()` 当前直接构造
  `CanonicalMutationV2Writer(connection=self.connection, ...)`；
- connection provider取得`RuntimeWriteAdmissionGuardHandle`后立即丢弃返回值；
- `runtime/projection_jobs/canonical_mutation.py` 不是pure helper，而是包含advisory lock、
  sequence head CAS、mutation/delivery insert与confirmation的完整PostgreSQL repository；
- 该repository的现有调用方至少是`migration_transform.py`、`mutation_writer.py`、
  `postgres_repository.py`。

因此D4不能只移动Protocol名字。它必须建立第5.8节的sealed同事务能力，并按pure factory、
SQL repository、runtime writer三个owner拆文件。

### 2.10 Binding、artifact 与 receipt 真值

- 当前 `ToolBindingContract` 只保存聚合fingerprint；MCP resume/Host refresh/child index仍从
  concrete tool的`binding_identity`或`getattr()`取得server/slot/snapshot/generation；
- `ToolResultArtifactService.process_result()` 直接读取
  `CapabilityDescriptor.artifact_mode`，并在descriptor缺失时回退DEFAULT；preview/archive
  limits来自另一份runtime options；
- `CurrentToolResultReceiptItem/Batch` 继承`FrozenRuntimeStateBase`，无schema version，只在
  `AgentRuntime` process-local path构造与消费，event schema并不持久化它们。

这三项分别要求origin-aware binding union、低层resolved artifact policy与process-local replay
receipt owner，不能用类型改名掩盖现有behavior input。

### 2.11 第二轮 port surface 真值

最终反向审阅继续确认了以下不能在实施阶段临场决定的行为：

- memory UOW不只append mutation；它在同一connection构造graph、decision、governance event
  outbox、mutation outbox、lifecycle与write service，并直接执行event-context/row-lock SQL；
- MCP normal completion在`normalized.is_error=True`时仍保留output、artifacts与metadata，只把
  ToolResult state设为ERROR；
- Host resumable query当前同时使用workspace root、memory domain、include closed与limit；
- terminal public surface是1个command、8个process action与3个monitor action，并携带completion
  reservation、observation receipt、monitor registration/cancellation等process owner；
- subagent九个tool的request与success payload各不相同，尤其task batch、task wait、child phase/result
  不能压成generic mapping；
- package edge baseline无法发现已有package pair下新增module import；必须以AST observation为真源；
- Host wiring输入同时混有semantic config与supervisor/runtime live object，单一“完整字段fingerprint”
  无法成立。

因此第5.5、5.6、5.7、5.8.1与9.4节冻结的是现有behavior的完整carrier，而不是未来可选优化。

---

## 3. 最终依赖模型

### 3.1 层次

最终冻结以下 import 方向：

```text
L0  primitives
      - durable/event-safe facts
      - pure value identity/canonical codec

L1  message schema
      - Msg/content blocks only

L2  event schema
      - AgentEvent/candidates/event-safe receipts

L3  replay + ports + projection contracts
      - pure event reducers/assemblers
      - process-local Protocol and carrier
      - D3 event-safe projection DTO；无 worker/service implementation

L4  capability contracts + concrete tools
      - descriptors/catalog/result semantic builders
      - filesystem/memory/terminal/MCP/subagent tool adapters

L5  runtime domain services
      - RuntimeSession, ToolExecutor, terminal/MCP/subagent owners
      - provider input, compaction, projection jobs

L6  host/cli/inspector composition

TEST tests/support
      - fake ports, mocks, in-memory whole-runtime composition
```

该层次是长期目标，不是 D4 对整个仓库一次性作出的 DAG 承诺。当前真实 package graph
仍有一个覆盖 `runtime`、`llm`、`memory`、`host`、`storage`、`graph`、`event_log`、
`capability`、`tools` 等 package 的大型 SCC；其中 `llm.control -> runtime`、
`runtime.session -> llm`、`memory.candidates.projection_outbox -> runtime` 等边属于 D5/D6
后续 ownership，而不在本次修改面内。

D4 只对 3.2 节列出的 target DAG 作 fail-closed gate。全局 package SCC 由 scanner 生成
冻结 diagnostic baseline，用于证明 D4 没有扩大债务和为 D5/D6 提供输入；D4 不以“全局 SCC
归零”为完成条件，也不得在结论中声称已经做到这一点。

### 3.2 D4 target DAG 硬规则

D4 的 production fail-closed 集合固定为：

1. `tools` 不得导入 `runtime`、`host`、`cli` 或 `inspector`；
2. `capability` 不得导入 concrete `runtime` 或 concrete `tools` implementation；
3. `event` schema 不得导入 `replay`、`runtime`、`tools` 或 `host`；
4. `storage`、`graph`、`memory` 不得导入 `runtime.projection_jobs`；它们只能依赖
   top-level `projection_jobs` contracts 或命名 port；
5. `src/pulsara_agent` 不得导入 `tests` 或 `tests.support`。

本次为完成上述 cut 而新增的低层 package 还必须满足：

6. `ports` 不得导入 concrete `runtime`、`tools`、`host`、`cli` 或 `inspector`；
7. `projection_jobs` top-level contract package不得导入 `runtime.projection_jobs`；
8. `primitives/memory_candidate.py` 不得导入 `event`；迁移后的 `event/candidates.py`
   只能单向依赖该 primitive；
9. `replay` 可以依赖 message/event schema，但 message/event schema不得反向依赖 replay；
10. `runtime` 不得通过 package facade、dynamic import 或 `TYPE_CHECKING` 绕回旧
    `tools.executor`/`tools.base` owner；
11. `TYPE_CHECKING`、函数内 import、`importlib.import_module()` 与 package `__getattr__`
    受相同规则；
12. 禁止用 `Any`、裸 callable 或 `dict[str, object]` 伪装跨层 service locator；跨层行为
    必须由命名 protocol 和 closed method surface表达；
13. 低层 port不得返回 `RuntimeSession`、`AgentRuntimeWiring`、`HostSession` 等高层 concrete
    carrier。Host composition contract是第9节明确隔离的 composition-boundary exception，
    不属于低层 `ports`。

这些规则没有对 `runtime <-> llm`、`runtime <-> memory` 或其他 D5/D6 package 边作隐含
许可或清理承诺；它们只是不属于 D4 completion gate。

### 3.3 全局 SCC diagnostic baseline

scanner以每一条canonical AST import observation为最小增长真源；package edge和SCC只是由这些
observation折叠出的诊断。先冻结：

```python
@dataclass(frozen=True, slots=True)
class CanonicalAstImportObservationFact:
    source_module: str
    target_module: str
    import_kind: Literal[
        "import", "from", "import_module", "dunder_import", "package_getattr"
    ]
    enclosing_qualname: str
    normalized_import_ast_fingerprint: str
    equal_ast_occurrence_ordinal: int
    source_package: str
    target_package: str
    observation_id: str
    observation_fingerprint: str

@dataclass(frozen=True, slots=True)
class PackageSccDiagnosticBaseline:
    ordered_residual_scc_import_observations: tuple[
        CanonicalAstImportObservationFact, ...
    ]
    residual_scc_import_observation_count: int
    residual_scc_import_observations_accumulator: str
    package_members: tuple[str, ...]
    ordered_internal_edges: tuple[tuple[str, str], ...]
    members_accumulator: str
    edges_accumulator: str
    baseline_fingerprint: str

@dataclass(frozen=True, slots=True)
class D4TargetEdgeCutoverEntry:
    import_observation_id: str
    import_observation_fingerprint: str
    source_module: str
    target_module: str
    owning_cutover_phase: Literal["D4-1", "D4-2", "D4-3", "D4-4"]
    entry_fingerprint: str
```

规则：

- `normalized_import_ast_fingerprint`覆盖解析后的import kind、resolved absolute target与imported
  names/aliases；不覆盖行号、列号、注释或格式；
- `enclosing_qualname`是module、class或function的稳定AST owner path；同一owner内完全相同的
  normalized import按AST traversal顺序获得`equal_ast_occurrence_ordinal`，因此重复语句不会合并；
- `observation_id = H("d4-import-observation-id:v1", source_module, target_module,
  import_kind, enclosing_qualname, normalized_import_ast_fingerprint,
  equal_ast_occurrence_ordinal)`；fingerprint由central factory重算，caller不得自报；
- source line/path只属于诊断attribution，不进入observation identity；移动无关代码行不会伪造新债务；
- scanner始终从全量canonical observations建图；baseline在D4-0最终类型owner安装完成后，
  只冻结所有非target residual SCC内部的exact observations；
- D4-0至D4-5均重新派生package graph/SCC，再比较residual SCC observation
  set/count/accumulator；
- 新增合法、单向且不进入任何SCC/target forbidden edge的DAG import允许；新增或修改任何
  residual-SCC observation则失败，即使它落在baseline已有的同一package pair；删除允许并记录；
- 新建package/member若扩大existing SCC或形成新SCC同样失败；
- package member、package edge与SCC变化仅是派生报告，不能替代module-level observation gate；
- baseline本身是 diagnostic/growth guard，不是 residual import allowlist；
- target DAG中的 forbidden edge没有 exception，即使它已经存在于 baseline也必须在对应
  vertical cut阶段清零；
- D4-5把剩余全局 SCC连同 exact edge清单移交D5/D6，但不得写成“D4已消除跨 package SCC”。

因此不再维护一张假装穷尽全仓库的 source-module residual allowlist。D4 only gate是3.2节
的 target DAG，global baseline只防止债务增长并提供后续审计事实。

为使D4-0至D4-4各自可合入，另设短期`D4TargetEdgeCutoverLedger`。它是按
`import_observation_id`排序的`D4TargetEdgeCutoverEntry` tuple；每项必须exact join baseline中的
observation ID/fingerprint和target DAG forbidden edge。每个后续阶段只能删除由本阶段完成的
entry，不能新增、改写或以新行号生成替代entry。它不是最终dependency exception，也不承载
D5/D6边：

```text
D4-0  freeze exact target-edge cutover ledger
D4-1  删除 event/replay、lower-layer/projection entries
D4-2  删除 runtime旧tools-contract/executor entries
D4-3  删除 tools/runtime、capability/concrete entries
D4-4  删除 production/test-support与facade entries
D4-5  ledger文件和loader一并删除，target DAG直接要求零命中
```

### 3.4 D4-0 类型所有权 cutover ledger

D4-0同时冻结另一张短期、非durable的type ownership ledger：

```python
class D4TypeOwnershipSymbolKind(StrEnum):
    CLASS = "class"
    ENUM = "enum"
    PROTOCOL = "protocol"
    ASSIGNMENT_TYPE_ALIAS = "assignment_type_alias"
    UNION_ALIAS = "union_alias"
    PEP695_TYPE_ALIAS = "pep695_type_alias"

@dataclass(frozen=True, slots=True)
class D4TypeOwnershipCutoverEntry:
    symbol_name: str
    symbol_kind: D4TypeOwnershipSymbolKind
    old_module: str
    final_owner_module: str
    final_qualname: str | None
    canonical_alias_shape_fingerprint: str | None
    temporary_consumer_modules: tuple[str, ...]
    delete_reexport_in_phase: Literal["D4-1", "D4-2", "D4-3", "D4-4"]
    entry_fingerprint: str
```

central factory只对稳定symbol kind、module、条件字段、consumer与phase编码；Python object ID只在
本次test process作为join authority，不进入fingerprint。所有branch都先强制：

```text
getattr(old_module, symbol_name) is getattr(final_owner_module, symbol_name)
```

之后按`symbol_kind`执行互斥矩阵：

| symbol kind | final-owner检查 | shape检查 |
|---|---|---|
| `CLASS / ENUM / PROTOCOL` | `__module__ == final_owner_module`且`__qualname__ == final_qualname` | `canonical_alias_shape_fingerprint is None`；AST中恰好一个matching class定义 |
| `ASSIGNMENT_TYPE_ALIAS` | 不读取alias对象的`__module__/__qualname__`；final AST中恰好一个`Assign/AnnAssign`定义 | `get_origin()/get_args()`递归canonical shape必须等于ledger fingerprint |
| `UNION_ALIAS` | 同assignment alias；额外要求root origin是`typing.Union`或`types.UnionType` | union member的ordered canonical shape必须精确匹配 |
| `PEP695_TYPE_ALIAS` | `TypeAliasType.__module__ == final_owner_module`、`__name__ == symbol_name`；不要求不存在的`__qualname__` | `__value__`和`__type_params__`的canonical shape必须精确匹配 |

entry validator要求class-like branch的`final_qualname`非空且alias shape为空；三种alias branch要求
`final_qualname is None`且shape fingerprint非空。`symbol_kind`不允许由loader根据运行时对象猜测，必须
由final-owner AST closed classifier得出并与ledger逐项相等。

alias canonicalizer禁止使用`repr()`。它递归编码closed node union：

```text
runtime type       -> ("type", __module__, __qualname__)
typing origin      -> ("origin", canonical origin identity, ordered argument nodes)
Literal value      -> ("literal", canonical JSON scalar type tag + value)
ForwardRef         -> ("forward_ref", exact forward argument)
TypeVar/ParamSpec  -> (kind, stable name, bound/constraints/default shape)
None/Ellipsis      -> dedicated closed node
```

unsupported alias node fail closed。`typing.Literal`参数顺序、union member顺序与nested tuple顺序均保留；
canonicalizer的ID/version/fingerprint进入ledger fixture。assignment/union alias没有final-owner runtime
metadata这一事实不能被当作失败，也不能用`typing._LiteralGenericAlias`等private implementation class
名作为identity。

旧module AST只允许该symbol的direct import/re-export和`__all__`列举，不得出现同名class、type alias
重定义、subclass、wrapper factory或module-level compatibility conversion。D4-0之后新增entry失败；
后续phase只能删除到期entry。D4-5删除ledger与loader并扫描全仓库，确认所有consumer直接导入最终
owner。该ledger只解决迁移期间import兼容，不是允许两套类型并存的exception。

gate fixture必须至少包含真实`SubagentStatus = Literal[...]`、一个`A | B` union、一个Protocol、
一个StrEnum与Python 3.12 `type Alias = ...`；并显式证明assignment alias即使运行时
`__module__ == "typing"`、`__qualname__ == "Literal"`仍可通过alias branch，而shape/member顺序或
final-owner AST定义发生变化会失败。

---

## 4. 可执行 dependency rule

### 4.1 新增 scanner

新增 `tests/support/dependency_rules.py`，使用 Python AST 扫描 `src/pulsara_agent/**/*.py`。

scanner 必须识别：

- `import x.y`；
- `from x.y import z`；
- 相对 import，经当前 module 解析为绝对 module；
- 函数体、class body、`TYPE_CHECKING` 内 import；
- `import_module("literal")`；
- `__import__("literal")`；
- package `__getattr__` + literal routing table；
- 无法静态解析的 dynamic import。

无法解析的 production dynamic import 默认拒绝；只有 plugin/provider discovery 的现有 closed registry 可使用独立 exact allowlist。

### 4.2 输出

每条 observation 至少包含：

```python
@dataclass(frozen=True, slots=True)
class DependencyObservationAttribution:
    semantic: CanonicalAstImportObservationFact
    source_path: str
    line: int
    column: int
    under_type_checking: bool
    local_scope: bool
```

target DAG失败信息必须打印具体 `file:line` 与命中的规则。SCC diagnostic必须打印最短
package cycle及构成该cycle的具体 import observation，但现存非target SCC本身不使D4失败。
attribution不进入baseline semantic；baseline只保存3.3节canonical fact，避免行号变化改写债务
identity。

### 4.3 gate

新增 `tests/test_dependency_architecture.py`：

gate按phase单调收紧，不能在D4-0预装一个注定被当前代码打红的final assertion：

| 从阶段起 | 新增且以后永久保留的gate |
|---|---|
| D4-0 | scanner fixture覆盖所有import kind；`test_canonical_ast_import_observation_detects_same_scc_package_pair_growth`；`test_legal_acyclic_import_does_not_count_as_scc_growth`；`test_d4_target_edge_cutover_ledger_is_exact_and_non_growing`；`test_d4_type_owner_identity_is_exact_and_non_growing`；`test_package_scc_diagnostic_baseline_does_not_grow` |
| D4-1 | `test_event_schema_does_not_import_replay`；`test_lower_layers_do_not_import_runtime_projection_jobs`；D4-1 ledger entries为零 |
| D4-2 | `test_runtime_does_not_import_old_tools_contract_or_executor_owners`；D4-2 ledger entries为零 |
| D4-3 | `test_runtime_tools_have_one_direction_only`；`test_capability_does_not_import_concrete_runtime_or_tools`；`test_builtin_tool_catalog_is_exhaustive`；D4-3 ledger entries为零 |
| D4-4 | `test_production_never_imports_test_support`；`test_runtime_and_tools_facades_have_no_lazy_router`；`test_production_composition_has_no_in_memory_or_mock_binding`；`test_host_core_has_no_durable_selector`；D4-4 ledger entries为零 |
| D4-5 | 删除两张cutover ledger测试与fixture，新增`test_d4_target_dependency_dag_has_no_exceptions`与`test_d4_has_no_temporary_type_reexports` |

该文件进入普通 `uv run pytest`，不加 marker，不依赖网络/PostgreSQL。

---

## 5. Tool contracts 与 ports

### 5.1 `ports/tool_execution.py`

将以下 process-local contract 从 `tools/base.py` 迁入新文件：

- `ToolCall`；
- `ToolExecutionResult`；
- `PreparedToolTerminalResult`；
- `ToolExecutionSuspended`；
- `ToolRuntimeContext`；
- `ToolResultArtifactCandidate`；
- `Tool` / `AsyncTool` Protocol。

这些不是 durable facts，不注册schema version。物理carrier可以按下述规则递归冻结，但
thaw后的canonical JSON值、provider payload与event fingerprint不得改变。

同时删除 execution binding 对 model-visible descriptor 的第二份 ownership：

- `Tool` / `AsyncTool` 不再要求 `description`、`parameters`、`is_read_only`、
  `is_concurrency_safe`；
- model-visible description/schema与read-only/concurrency语义只来自已冻结的
  `CapabilityDescriptor`；
- concrete binding只声明稳定 `name` 并执行 call；
- `ToolRegistry.tool_specs()` 删除，provider tool definitions只由 capability exposure lowering；
- `runtime/tool_loop.py` 删除读取 concrete tool flags 的 fallback。production call没有 exact
  descriptor时fail closed；unknown tool只形成现有typed unknown-tool result，不能自行推断并发性。

这使 catalog成为真源，而不是“catalog与每个class各写一份、靠测试希望它们相等”。

`ToolRuntimeContext.permission_policy: dict | None` 改为 typed snapshot：

```python
@dataclass(frozen=True, slots=True)
class ToolPermissionInvocation:
    permission_snapshot_id: str
    permission_mode: PermissionMode
    permission_policy_fingerprint: str
    terminal_access: Literal["off", "ask", "allow"]
    network_isolated: bool
    source_run_permission_snapshot_fingerprint: str

class ToolInvocationOwnerKind(StrEnum):
    HOST_MAIN_RUN = "host_main_run"
    SUBAGENT_CHILD = "subagent_child"

@dataclass(frozen=True, slots=True)
class ToolRuntimeContext:
    runtime_session_id: str
    event_context: EventContext
    context_id: str | None
    model_call_index: int | None
    permission: ToolPermissionInvocation
    owner_kind: ToolInvocationOwnerKind
```

规则：

- production tool execution 中 `permission` 与 `owner_kind` required；
- `ToolPermissionInvocation` 只能由中央 factory从 committed
  `RunPermissionSnapshotFact` 重算；caller不能自报 fingerprint/terminal access；
- ToolExecutor 只从 committed run permission snapshot 构造；
- terminal tools 不再读取 mutable `PermissionState`；
- component test 通过 `tests/support/capability.py` 构造最小合法 context；
- 不允许 caller 同时传旧字典与新 typed permission。

`ToolCall.arguments`、`ToolExecutionResult.metadata` 与 artifact candidate metadata在跨过
executor admission时必须递归冻结；concrete tool若需要普通dict，只能获得单次owned thawed
copy。Tool descriptor JSON Schema也必须以递归不可变carrier进入catalog，避免注册后漂移。

#### 5.1.1 Stable tool-event candidate owner

现有`runtime/tool_execution.py::ToolExecutionTerminalRegistry`已经保存
`stable_candidates: tuple[AgentEvent, ...]`，但close drain只处理terminal/unknown并假设每批都有
`ToolResultEndEvent`。D4-0先把下列process-local join carrier迁到`ports/tool_execution.py`；D4-2
再把registry升级为suspension与terminal两类stable candidate的唯一owner：

```python
class ToolExecutionStableCandidateKind(StrEnum):
    SUSPENSION = "suspension"
    TERMINAL = "terminal"

class ToolExecutionNonePolicy(StrEnum):
    ABANDON_ON_NONE = "abandon_on_none"
    RETRY_SAME_CANDIDATE = "retry_same_candidate"

class ToolExecutionCandidateConfirmationKind(StrEnum):
    FULL = "full"
    NONE = "none"
    UNKNOWN = "unknown"
    PARTIAL = "partial"

class ToolExecutionStableCandidateOwnerState(StrEnum):
    ADMITTED = "admitted"
    SUSPENSION_CANDIDATE_FROZEN = "suspension_candidate_frozen"
    SUSPENDED = "suspended"
    TERMINAL_CANDIDATE_FROZEN = "terminal_candidate_frozen"
    RETRY_WAIT = "retry_wait"
    COMMIT_OUTCOME_UNKNOWN = "commit_outcome_unknown"
    DURABLE_FULL_AWAITING_PHYSICAL_HANDOFF = (
        "durable_full_awaiting_physical_handoff"
    )
    RECONCILIATION_REQUIRED = "reconciliation_required"

@dataclass(frozen=True, slots=True)
class ToolExecutionStableCandidateOwnerIdentity:
    registry_instance_id: str
    owner_id: str
    owner_generation: int
    runtime_session_id: str
    run_id: str
    tool_call_id: str
    rollout_reservation_id: str
    rollout_reservation_fingerprint: str
    candidate_kind: ToolExecutionStableCandidateKind
    none_policy: ToolExecutionNonePolicy
    ordered_candidate_event_ids: tuple[str, ...]
    candidate_batch_fingerprint: str
    physical_owner_kind: Literal["mcp_pending"] | None
    physical_owner_identity_fingerprint: str | None
    identity_fingerprint: str

@dataclass(frozen=True, slots=True)
class ToolExecutionStableCandidateCommitReceipt:
    owner_identity: ToolExecutionStableCandidateOwnerIdentity
    confirmation_kind: ToolExecutionCandidateConfirmationKind
    write_attempt_generation: int
    committed_event_references: tuple[ContextEventReferenceFact, ...]
    publication_summary: Literal[
        "not_applicable",
        "completed",
        "enqueued",
        "unavailable",
        "failed_after_commit",
    ]
    retry_scheduled: bool
    reconciliation_required: bool
    receipt_fingerprint: str

@dataclass(frozen=True, slots=True)
class ToolExecutionPhysicalOwnerHandoffReceipt:
    candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity
    source_commit_receipt_fingerprint: str
    physical_owner_kind: Literal["mcp_pending"]
    physical_owner_identity_fingerprint: str
    handoff_generation: int
    physical_disposition: Literal["retained", "confirmed", "released"]
    exact_retry_required: bool
    reconciliation_required: bool
    receipt_fingerprint: str
```

`ToolExecutionStableCandidateOwnerIdentity`只引用candidate IDs/fingerprint；真正的immutable
`tuple[AgentEvent, ...]`只存在registry private owner中。MCP handle、terminal settlement、Agent
scratchpad与RuntimeSession write result都不得复制event tuple。central registry factory从完整candidate
重算batch fingerprint，owner generation每次freeze递增；相同tool call的successor suspension不得复用
上一代identity。registry admission必须对每个candidate做owned deep copy/canonical encode并保存sealed
bytes/fingerprint；每次retry前重算并逐字节比较，任何caller后续mutation都进入reconciliation，不能
改变已冻结写入内容。

freeze policy固定为：

| candidate | allowed predecessor | NONE policy |
|---|---|---|
| initial suspension | `ADMITTED` | `ABANDON_ON_NONE`，保持既有fail-closed语义 |
| successor suspension | `SUSPENDED` | `RETRY_SAME_CANDIDATE` |
| terminal ToolResult/closure | `ADMITTED`或`SUSPENDED` | `RETRY_SAME_CANDIDATE` |

registry状态增加`DURABLE_FULL_AWAITING_PHYSICAL_HANDOFF`与
`RECONCILIATION_REQUIRED`，并冻结以下算法：

1. `freeze_suspension/freeze_terminal`先保存exact candidate tuple，再返回owner identity；若绑定MCP
   handle，physical owner kind/fingerprint required且必须由port handle identity取得。
2. 所有写入、NONE bounded backoff retry与UNKNOWN/PARTIAL confirmation只由registry执行；
   `drain_pending()`必须包含`SUSPENSION_CANDIDATE_FROZEN`、
   `TERMINAL_CANDIDATE_FROZEN`与unknown/reconciliation owner，不能再只寻找`ToolResultEndEvent`。
3. suspension reconcile调用typed suspension commit port；terminal reconcile调用typed terminal commit
   port。两者共享absolute deadline和exact candidate tuple，但不把suspension伪装成terminal。
   receipt factory直接检查`RuntimeSession` write result：存在publication errors时归一化为
   `failed_after_commit`；否则保留`completed/enqueued/unavailable`。publication summary不参与
   FULL/NONE/UNKNOWN/PARTIAL判断，也不能把durable FULL降格为NONE。
4. FULL后registry生成commit receipt并进入`DURABLE_FULL_AWAITING_PHYSICAL_HANDOFF`；在收到exact
   physical handoff receipt前不得清空candidate或删除owner。无physical owner的普通tool可立即finalize。
5. NONE + `RETRY_SAME_CANDIDATE`保留tuple/identity并安排bounded retry；NONE +
   `ABANDON_ON_NONE`生成receipt后等待physical owner release，再由既有fail-closed terminalization取得
   新一代terminal candidate。
6. UNKNOWN/PARTIAL同时保留candidate owner并latch RuntimeSession；若关联MCP，MCP handle也进入
   reconciliation。任何一边缺失都阻止Host close静默成功。
7. MCP port返回`ToolExecutionPhysicalOwnerHandoffReceipt`后，registry按owner identity/generation、
   source commit receipt fingerprint、handoff generation与physical owner fingerprint重验：suspension FULL转`SUSPENDED`并清除candidate；
   terminal FULL或initial NONE release删除owner；retry/reconciliation继续保留。

Host close顺序固定为：停止新tool/resume admission，等待manager active borrow的`finally`归还，drain
registry stable candidates，逐receipt调用physical owner settlement，再由registry完成handoff，最后才
关闭MCP pending leases/Supervisor。deadline耗尽时两类owner都保留并使close blocked。

### 5.2 `ports/tool_result_semantics.py`

当前 `tools/base.py` 通过 `TYPE_CHECKING` 依赖
`capability/result_semantics.py::ToolResultSemanticsRuntimeInput`，三个terminal tool又直接
导入该模块的 runtime-input factory。只移动 `ToolCall` 无法切断这条边。

新增中立process-local模块并迁移：

- `ToolResultSemanticsRuntimeInput` protocol；
- `FrozenToolResultSemanticsRuntimeInput`；
- terminal/artifact等 domain submission runtime-input factory。

`capability/result_semantics.py` 只保留 builder contract/binding/registry，改为依赖该port；
concrete tools也只依赖port。所有既有 durable `ToolResult*Fact`、builder ID/version、render
contract fingerprint保持不变。

### 5.3 `ports/tool_registry.py`

迁移 binding contract，并把当前仅存在于 concrete `McpCapabilityTool` 上的 exact MCP
identity正式纳入 origin-aware union。基础字段保持现有 hash输入和值不变：

```python
class ToolBindingOrigin(StrEnum):
    BUILTIN = "builtin"
    MCP = "mcp"
    CUSTOM = "custom"
    WORKFLOW = "workflow"
    SUBAGENT_SYSTEM = "subagent_system"

@dataclass(frozen=True, slots=True)
class ToolBindingContractBase:
    tool_name: str
    origin: ToolBindingOrigin
    contract_id: str
    contract_version: str
    binding_fingerprint: str

@dataclass(frozen=True, slots=True)
class BuiltinToolBindingContract:
    binding_kind: Literal["builtin"]
    base: ToolBindingContractBase
    contract_fact_fingerprint: str

@dataclass(frozen=True, slots=True)
class McpToolBindingContract:
    binding_kind: Literal["mcp"]
    base: ToolBindingContractBase
    binding_identity: McpBindingIdentityFact
    original_tool_name: str
    contract_fact_fingerprint: str

@dataclass(frozen=True, slots=True)
class CustomToolBindingContract:
    binding_kind: Literal["custom"]
    base: ToolBindingContractBase
    contract_fact_fingerprint: str

ToolBindingContract = (
    BuiltinToolBindingContract
    | McpToolBindingContract
    | CustomToolBindingContract
)
```

这里冻结两层identity，避免“为类型搬家更新golden”：

- `base.binding_fingerprint` 是现有 provider/runtime compatibility semantic，继续按当前
  `tool-binding-contract:v1` payload计算；
- `contract_fact_fingerprint` 是新的process-local structural proof，覆盖base完整payload和union
  branch全部字段。

MCP branch的 `binding_identity`字段必须与现有 `binding_attributes` 中的
`server_id/slot_id/snapshot_id/discovery_generation` 逐项相等，因此此次类型迁移不改变既有
`binding_fingerprint`。`original_tool_name`进入`contract_fact_fingerprint`与execution request，
但不反向改写旧 compatibility fingerprint。禁止 caller先提供fingerprint、再附加未参与任一
central proof的字段。

branch/origin矩阵唯一为：builtin branch允许base origin
`builtin|workflow|subagent_system`；MCP branch只允许`mcp`；custom branch只允许`custom`。
workflow/subagent system仍进入builtin catalog，不伪装成dynamic custom binding。

registry read port定义为：

```python
class ToolRegistryReadPort(Protocol):
    def names(self) -> tuple[str, ...]: ...
    def get(self, name: str) -> Tool | AsyncTool: ...
    def binding_contract(self, name: str) -> ToolBindingContract | None: ...
    def mcp_bindings(self) -> tuple[McpToolBindingContract, ...]: ...
```

`tools/registry.py::ToolRegistry` 是唯一 concrete implementation。`capability/runtime.py` 只依赖 read port，不再 TYPE_CHECK concrete `ToolRegistry`。

Host MCP refresh/resume、`AgentRuntime` binding-change gate与child reverse index只能读取
`McpToolBindingContract.binding_identity`。以下路径全部禁止：

- `isinstance(tool, McpCapabilityTool)`；
- `getattr(tool, "binding_identity", ...)`；
- `hasattr(tool, "binding_identity")` 推断 origin；
- 从 descriptor metadata/free dict反解析 server/slot/snapshot/generation。

dynamic binding安装时必须原子构造 concrete tool与上述 contract；registry拒绝 tool name、
origin、original MCP name或 exact identity不一致的pair。

### 5.4 artifact ports

新增 `ports/artifact.py`。`CapabilityArtifactMode` 的closed value集合下沉为
`ToolArtifactMode`（字符串值保持 `default|never|always|large_output|structured_json`），
`CapabilityDescriptor.artifact_mode`直接引用该低层 enum，避免port反向依赖capability。

先冻结三个 process-local、recursively immutable view：

```python
@dataclass(frozen=True, slots=True)
class ToolArtifactRecordView:
    artifact_id: str
    role: str
    media_type: str
    size_bytes: int
    stored_complete: bool
    loss_reason: str | None
    content_digest: str | None
    record_view_fingerprint: str

@dataclass(frozen=True, slots=True)
class ToolArtifactInfoView:
    record: ToolArtifactRecordView
    stored_at_utc: str
    created_at_utc: str | None
    info_view_fingerprint: str

@dataclass(frozen=True, slots=True)
class ToolArtifactTextSliceView:
    info: ToolArtifactInfoView
    text: str
    offset_chars: int
    returned_chars: int
    total_chars: int | None
    has_more: bool
    slice_view_fingerprint: str
```

所有字符串、nested metadata与view均由runtime adapter深拷贝/冻结，三层fingerprint由中央
factory覆盖各自完整字段；tool永远看不到archive、index row、session ID或mutable metadata。
这些view是process-local read result，不注册schema version或进入event semantic；durable
authority仍是artifact/index row与ToolResult event reference。

artifact processing的行为输入不是 `CapabilityDescriptor`，而是在 execution-surface freeze
时由 descriptor与resolved runtime options共同派生的低层policy：

```python
@dataclass(frozen=True, slots=True)
class ToolResultArtifactProcessingPolicy:
    descriptor_id: str
    descriptor_fingerprint: str
    artifact_mode: ToolArtifactMode
    source_reference_policy: Literal["none", "reuse_input_artifact"]
    fallback_media_type: Literal[
        "text/plain; charset=utf-8", "application/json"
    ]
    archive_threshold_bytes: int
    complete_preview_body_chars: int
    large_preview_chars: int
    huge_output_chars: int
    huge_preview_chars: int
    streaming_live_head_cap_chars: int
    max_inline_chars: int | None
    policy_contract_version: Literal["tool-result-artifact-processing:v1"]
    policy_fingerprint: str
```

中央 factory强制：

- `STRUCTURED_JSON` 唯一映射到 `application/json`；其他mode映射text；
- `artifact_read` 使用 `NEVER + reuse_input_artifact`，保留当前“不递归归档但附回source ref”行为；
- `max_inline_chars`与所有resolved preview/archive bounds都进入policy fingerprint；
- policy中的 descriptor fingerprint必须与同一execution-surface entry exact join；
- capability refresh后旧in-flight call继续使用已冻结policy，新call使用新surface；不得pre-send漂移。

ports定义为：

```python
class ToolArtifactReadPort(Protocol):
    def lookup(self, artifact_id: str) -> ToolArtifactRecordView | None: ...
    def info(self, artifact_id: str) -> ToolArtifactInfoView: ...
    def read_text(
        self, artifact_id: str, *, offset_chars: int, max_chars: int
    ) -> ToolArtifactTextSliceView: ...

class ToolResultArtifactProcessingPort(Protocol):
    def process_result(
        self,
        result: ToolExecutionResult,
        *,
        event_context: EventContext,
        tool_call: ToolCall,
        policy: ToolResultArtifactProcessingPolicy,
    ) -> tuple[ToolExecutionResult, tuple[ToolResultArtifactRef, ...]]: ...
```

`RuntimeToolArtifactReadPort` 在 `runtime/tool_artifacts.py` 中实现并冻结 session scope。`ArtifactReadTool` 只持有 read port，不能看到 archive、index 或 runtime session ID。

`ToolResultArtifactService.process_result(..., descriptor=None)`兼容入口删除；production和test
均必须传exact policy。这样 descriptor ownership仍在capability，artifact执行规则在低层policy，
不存在 `ports -> capability` 反向边或缺descriptor时回退默认值的第二真源。

### 5.5 terminal ports

新增 `ports/terminal.py`，承载 process-local terminal value、public input union 与三个完整行为
协议。现有 `terminal_public_api.py` 中的strict Pydantic request classes迁入该低层owner；原模块只
保留description与schema factory re-export，不得复制字段或validator。

基础owner与closed错误先冻结为：

```python
@dataclass(frozen=True, slots=True)
class TerminalPortInvocationOwner:
    runtime_session_id: str
    run_id: str
    tool_call_id: str
    tool_name: Literal["terminal", "terminal_process", "terminal_monitor"]
    event_context: EventContext
    owner_kind: ToolInvocationOwnerKind
    permission: ToolPermissionInvocation
    owner_fingerprint: str

class TerminalPortRejectCode(StrEnum):
    MALFORMED_ARGUMENTS = "malformed_arguments"
    ACCESS_OFF = "terminal_access_off"
    HARDLINE_COMMAND = "hardline_terminal_command"
    HARDLINE_PROCESS_INPUT = "hardline_terminal_process_input"
    PROCESS_NOT_FOUND = "process_not_found"
    PROCESS_INPUT_REJECTED = "process_input_rejected"
    PROCESS_CAPACITY_EXHAUSTED = "process_capacity_exhausted"
    MONITOR_OWNER_UNAVAILABLE = "monitor_owner_unavailable"
    MONITOR_CAPACITY_EXHAUSTED = "monitor_capacity_exhausted"
    MONITOR_DUPLICATE = "monitor_duplicate"
    MONITOR_NOT_FOUND = "monitor_not_found"
    CHILD_MONITOR_UNSUPPORTED = "child_monitor_unsupported"
    CONTRACT_MISMATCH = "contract_mismatch"

@dataclass(frozen=True, slots=True)
class TerminalCommandRejectedOutcome:
    outcome_kind: Literal["rejected"]
    command: str | None
    terminal_session_id: str | None
    failure_stage: Literal[
        "argument_validation",
        "permission",
        "adapter_initialization",
        "execution",
        "completion_reservation",
    ]
    reject_code: TerminalPortRejectCode
    sanitized_message: str
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class TerminalProcessRejectedOutcome:
    outcome_kind: Literal["rejected"]
    requested_action: str
    process_id: str | None
    status: Literal["malformed_arguments", "blocked", "not_found", "error"]
    reject_code: TerminalPortRejectCode
    sanitized_message: str
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class TerminalMonitorRejectedOutcome:
    outcome_kind: Literal["rejected"]
    requested_action: str
    process_id: str | None
    monitor_id: str | None
    status: Literal["malformed_arguments", "blocked", "not_found", "error"]
    reject_code: TerminalPortRejectCode
    sanitized_message: str
    outcome_fingerprint: str

class TerminalBackendType(StrEnum):
    LOCAL = "local"

class TerminalIOMode(StrEnum):
    PIPE = "pipe"
    PTY = "pty"

class TerminalStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"
    BLOCKED = "blocked"
    KILLED = "killed"

@dataclass(frozen=True, slots=True)
class TerminalResult:
    status: TerminalStatus
    output: str
    exit_code: int
    cwd: str
    timed_out: bool
    truncated: bool
    error: str | None
    process_id: str | None
    full_output_text: str | None
    metadata: FrozenJsonObjectFact
    observation_semantic: TerminalProcessObservationSemanticFact | None
    completion_event_reference: ContextEventReferenceFact | None

@dataclass(frozen=True, slots=True)
class TerminalProcessInfo:
    process_id: str
    terminal_session_id: str
    command: str
    cwd: str
    backend_type: TerminalBackendType
    io_mode: TerminalIOMode
    status: TerminalStatus
    exit_code: int | None
    timed_out: bool
    stdin_closed: bool
    started_at_monotonic: float
    ended_at_monotonic: float | None
    duration_seconds: float

@dataclass(frozen=True, slots=True)
class TerminalProcessLog:
    process: TerminalProcessInfo
    output: str
    truncated: bool
    full_output_text: str | None
    observation_semantic: TerminalProcessObservationSemanticFact | None
    completion_event_reference: ContextEventReferenceFact | None
```

现有`TerminalRequest`硬改名为下述唯一`TerminalCommandRequest`；不保留两个等价request。
`TerminalResult`、`TerminalProcessInfo`与`TerminalProcessLog`保留当前全部字段，但`metadata`
在port boundary递归冻结为`FrozenJsonObjectFact`。status仍是
`running | success | error | timeout | blocked | killed`，不引入`completed`别名。

#### 5.5.1 `terminal` command port

```python
@dataclass(frozen=True, slots=True)
class TerminalCommandRequest:
    command: str
    workdir: str | None
    terminal_session_id: str
    yield_time_ms: int
    max_output_chars: int
    tty: bool
    max_lifetime_seconds: int | None
    request_fingerprint: str

@dataclass(frozen=True, slots=True)
class TerminalCommandCompletedOutcome:
    outcome_kind: Literal["completed"]
    result: TerminalResult
    terminal_session_id: str
    backend_type: TerminalBackendType
    prepared_completion_reservation: PreparedTerminalNotificationReservation | None
    outcome_fingerprint: str

TerminalCommandOutcome = TerminalCommandCompletedOutcome | TerminalCommandRejectedOutcome

class TerminalOutputDeltaSink(Protocol):
    def emit(self, text_delta: str) -> None: ...

class TerminalCommandPort(Protocol):
    def execute(
        self,
        *,
        request: TerminalCommandRequest,
        owner: TerminalPortInvocationOwner,
        output_sink: TerminalOutputDeltaSink | None,
    ) -> TerminalCommandOutcome: ...
```

`prepared_completion_reservation`只在result为`running`且后台process取得completion account slot时
required；其他status必须为`None`。reservation的origin tool call/runtime/run必须与owner逐项相等。
streaming callback不再塞进`TerminalRequest.metadata`；tool把它包装成窄
`TerminalOutputDeltaSink`，runtime port在调用动态作用域内使用，且sink/object identity不进入request
或outcome fingerprint。event recorder、origin context与completion-reservation requirement同样由
runtime port从owner及construction-time dependencies派生，不进入自由metadata。

#### 5.5.2 `terminal_process` 八分支port

request直接复用并唯一拥有当前strict discriminated union：

```python
TerminalProcessRequest = (
    TerminalProcessListInput
    | TerminalProcessLogInput
    | TerminalProcessPollInput
    | TerminalProcessWaitInput
    | TerminalProcessWriteInput
    | TerminalProcessSubmitInput
    | TerminalProcessCloseStdinInput
    | TerminalProcessKillInput
)

@dataclass(frozen=True, slots=True)
class TerminalProcessInventoryOutcome:
    outcome_kind: Literal["inventory"]
    processes: tuple[TerminalProcessInfo, ...]
    live_process_count: int
    finished_process_count: int
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class TerminalProcessLogOutcome:
    outcome_kind: Literal["log"]
    log: TerminalProcessLog
    observation_receipt: TerminalProcessObservationReceiptFact
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class TerminalProcessObservationOutcome:
    outcome_kind: Literal["observation"]
    action: Literal["poll", "wait", "write", "submit", "close_stdin"]
    result: TerminalResult
    observation_receipt: TerminalProcessObservationReceiptFact | None
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class TerminalProcessKilledOutcome:
    outcome_kind: Literal["killed"]
    action: Literal["kill"]
    result: TerminalResult
    completion_observation_receipt: TerminalProcessObservationReceiptFact
    outcome_fingerprint: str

TerminalProcessOutcome = (
    TerminalProcessInventoryOutcome
    | TerminalProcessLogOutcome
    | TerminalProcessObservationOutcome
    | TerminalProcessKilledOutcome
    | TerminalProcessRejectedOutcome
)

class TerminalProcessPort(Protocol):
    def execute(
        self,
        *,
        request: TerminalProcessRequest,
        owner: TerminalPortInvocationOwner,
    ) -> TerminalProcessOutcome: ...
```

branch matrix不可退化为generic dict：

| request | 唯一success branch | 额外join |
|---|---|---|
| `list` | `TerminalProcessInventoryOutcome` | 无process ID；count必须从tuple重算 |
| `log` | `TerminalProcessLogOutcome` | receipt覆盖exact output cursor/completion ref |
| `poll` / `wait` | `TerminalProcessObservationOutcome` | action精确相等；有模型可见observation时receipt required |
| `write` / `submit` / `close_stdin` | `TerminalProcessObservationOutcome` | exact process owner；submit唯一追加newline，close只产生EOF |
| `kill` | `TerminalProcessKilledOutcome` | required terminal receipt引用exact completion authority，使ToolResult terminal batch同批释放completion head |

`wait.timeout_seconds`仍为1至30秒；port不得内部循环wait。`list`是唯一不要求process ID的分支。

#### 5.5.3 `terminal_monitor` 三分支port

```python
TerminalMonitorLifecycleState: TypeAlias = Literal[
    "active_ready",
    "active_pending_delivery",
    "active_completion_only",
    "terminal_pending_delivery",
    "terminated",
    "reconciliation_required",
]

TerminalMonitorRequest = (
    TerminalMonitorRegisterInput
    | TerminalMonitorListInput
    | TerminalMonitorCancelInput
)

@dataclass(frozen=True, slots=True)
class TerminalMonitorRegisteredOutcome:
    outcome_kind: Literal["registered"]
    prepared_registration: PreparedTerminalProcessMonitorRegistration
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class TerminalMonitorInventoryItem:
    monitor_id: str
    process_id: str
    lifecycle_state: TerminalMonitorLifecycleState
    observation_ordinal: int
    has_pending_observation: bool
    item_fingerprint: str

@dataclass(frozen=True, slots=True)
class TerminalMonitorInventoryOutcome:
    outcome_kind: Literal["inventory"]
    items: tuple[TerminalMonitorInventoryItem, ...]
    omitted_monitor_count: int
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class TerminalMonitorCancelledOutcome:
    outcome_kind: Literal["cancelled"]
    prepared_cancellation: PreparedTerminalProcessMonitorCancellation
    outcome_fingerprint: str

TerminalMonitorOutcome = (
    TerminalMonitorRegisteredOutcome
    | TerminalMonitorInventoryOutcome
    | TerminalMonitorCancelledOutcome
    | TerminalMonitorRejectedOutcome
)

class TerminalMonitorPort(Protocol):
    def execute(
        self,
        *,
        request: TerminalMonitorRequest,
        owner: TerminalPortInvocationOwner,
    ) -> TerminalMonitorOutcome: ...
```

register factory必须从public input唯一派生现有typed conditions/delivery/lifetime policy；completion
始终启用，expiry保持bounded。prepared registration、notification reservation、initial observation、
owner tool call与process origin必须exact join。cancel返回的是尚待ToolResult terminal batch消费的
prepared cancellation，不得在tool adapter提前commit；FIRING monitor继续由原physical owner吸收
cancel intent。list最多返回现有contract允许的8项并正确报告omitted count。

`TerminalMonitorLifecycleState`的唯一owner是
`primitives/terminal_observation.py`；durable core、ToolResult summary与port inventory都导入同一alias，
不得各自保留inline `Literal`或`str`。`TerminalMonitorRegisteredOutcome`只拥有
`prepared_registration`：initial observation与notification reservation只能从该carrier读取；
`TerminalMonitorCancelledOutcome`同理只拥有`prepared_cancellation`，monitor ID与cancel outcome由其
中央factory重算。outer outcome fingerprint覆盖nested prepared carrier fingerprint，不复制字段形成
第二真源。

`runtime/terminal/tool_port.py::RuntimeTerminalToolPort`实现三个protocol并包装manager、Host owner、
notification account与monitor coordinator。Host/session owner scope在adapter construction时冻结，
每个request仍携带本次call/run authority以完成prepared carrier join；concrete tool不得再接收或传递
owner host session ID、conversation ID、manager、account或coordinator。

规则：

- permission 从 `ToolRuntimeContext.permission` 读取；
- hardline classifier 移入 `capability/terminal_risk.py`，permission gate 与 tool adapter共用；
- completion reservation 仍与原 ToolResult terminal batch 原子提交；
- monitor prepared carrier 仍由 runtime coordinator 产生；
- public parser rejection由`ports.terminal.build_terminal_rejected_outcome()` pure factory构造，
  capacity/duplicate/not-found等runtime expected rejection由port构造；两者一律lower为closed
  tool-specific rejected branch。architecture invariant exception仍fail closed，不得伪装adapter error；
- tool adapter只把closed outcome渲染成现有`ToolExecutionResult`、semantics input和artifact candidates，
  不自行访问manager或重建receipt；
- port 只隐藏 concrete owner，不改变 TM0-TM5 lifecycle。

### 5.6 MCP execution port

新增 `ports/mcp.py`。所有类型均为 process-local frozen carrier，不注册event schema；nested
arguments、metadata与adapter payload使用现有 MCP canonical JSON freezer。

`FrozenMcpJsonDict`不是另一套JSON实现：它是`FrozenJsonObjectFact`的nominal type alias，由
`freeze_mcp_json_object()`唯一构造；该factory拒绝non-finite number、非字符串key及unsupported
object，并对mapping/list递归复制冻结。thaw只发生在调用MCP SDK的最后一步。

```python
FrozenMcpJsonDict: TypeAlias = FrozenJsonObjectFact

class McpToolRejectCode(StrEnum):
    BINDING_UNAVAILABLE = "binding_unavailable"
    BINDING_IDENTITY_MISMATCH = "binding_identity_mismatch"
    LEASE_ACQUIRE_FAILED = "lease_acquire_failed"
    PENDING_LEASE_BORROW_FAILED = "pending_lease_borrow_failed"
    RESOLUTION_IDENTITY_MISMATCH = "resolution_identity_mismatch"
    REQUEST_TIMEOUT = "request_timeout"
    PROTOCOL_ERROR = "protocol_error"
    ADAPTER_ERROR = "adapter_error"

@dataclass(frozen=True, slots=True)
class McpInvocationOwner:
    runtime_session_id: str
    run_id: str
    tool_call_id: str
    event_context: EventContext

@dataclass(frozen=True, slots=True)
class McpToolExecutionRequest:
    owner: McpInvocationOwner
    exposed_tool_name: str
    original_tool_name: str
    binding: McpToolBindingContract
    frozen_arguments: FrozenMcpJsonDict
    timeout_ms: int
    request_fingerprint: str

@dataclass(frozen=True, slots=True)
class McpToolResumeRequest:
    owner: McpInvocationOwner
    pending_handle: "McpPendingExecutionHandle"
    binding: McpToolBindingContract
    source_suspension_event_reference: ContextEventReferenceFact
    source_suspension: McpInputRequiredSuspensionFact
    prepared_resolution: PreparedMcpInputRequiredResolution
    timeout_ms: int
    request_fingerprint: str

class McpPendingHandleState(StrEnum):
    PREPARED_SUSPENSION = "prepared_suspension"
    SUSPENSION_COMMIT_IN_FLIGHT = "suspension_commit_in_flight"
    PENDING_CONFIRMED = "pending_confirmed"
    RESUME_IN_FLIGHT = "resume_in_flight"
    SUCCESSOR_SUSPENSION_FROZEN = "successor_suspension_frozen"
    TERMINAL_CANDIDATE_FROZEN = "terminal_candidate_frozen"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    ABORTED = "aborted"
    COMPLETED = "completed"

@dataclass(frozen=True, slots=True)
class McpPendingExecutionHandleIdentity:
    handle_id: str
    interaction_id: str
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    prepared_suspension_fingerprint: str
    predecessor_handle_id: str | None
    handle_generation: int
    identity_fingerprint: str

@dataclass(frozen=True, slots=True)
class McpPreparedSuspensionCommitView:
    interaction: McpInputRequiredInteractionSemanticFact
    binding_identity: McpBindingIdentityFact
    pending_lease_reservation: McpPendingLeaseReservationIdentityFact
    request_envelope: McpInputRequiredRequestEnvelopeFact
    deadline_monotonic: float | None
    tool_observation_timing_seed: FrozenJsonObjectFact | None
    prepared_suspension_fingerprint: str
    view_fingerprint: str

class McpPendingExecutionHandle(Protocol):
    """Opaque, borrower-scoped process owner; not event-serializable."""
    @property
    def identity(self) -> McpPendingExecutionHandleIdentity: ...
    @property
    def state(self) -> McpPendingHandleState: ...
    @property
    def suspension_commit_view(self) -> McpPreparedSuspensionCommitView: ...

class McpPendingTerminalReason(StrEnum):
    COMPLETED_RESULT = "completed_result"
    PERMISSION_DENIED = "permission_denied"
    BINDING_CHANGED = "binding_changed"
    INTERACTION_EXPIRED = "interaction_expired"
    MAXIMUM_ROUNDS_EXCEEDED = "maximum_rounds_exceeded"
    RESUME_UNSUPPORTED = "resume_unsupported"
    HOST_ABORT = "host_abort"
    CHILD_PENDING_UNSUPPORTED = "child_pending_unsupported"
    PUBLICATION_TERMINALIZATION = "publication_terminalization"

@dataclass(frozen=True, slots=True)
class McpPreparedTerminalSettlement:
    pending_handle_identity: McpPendingExecutionHandleIdentity
    reason: McpPendingTerminalReason
    candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity
    settlement_generation: int
    settlement_fingerprint: str

@dataclass(frozen=True, slots=True)
class McpPendingHandleTransitionOutcome:
    resulting_state: McpPendingHandleState
    handoff_receipt: ToolExecutionPhysicalOwnerHandoffReceipt
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class McpToolCompletedOutcome:
    outcome_kind: Literal["completed"]
    result_state: ToolResultState
    normalized_is_error: bool
    normalized_output: str
    frozen_display_payload: FrozenJsonObjectFact | None
    normalized_metadata: FrozenMcpJsonDict
    artifact_candidates: tuple[ToolResultArtifactCandidate, ...]
    semantics_input: ToolResultSemanticsRuntimeInput
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class McpToolSuspendedOutcome:
    outcome_kind: Literal["suspended"]
    pending_handle: McpPendingExecutionHandle
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class McpToolRejectedOutcome:
    outcome_kind: Literal["rejected"]
    error_code: McpToolRejectCode
    sanitized_message: str
    retryable_in_same_live_owner: bool
    outcome_fingerprint: str

McpToolExecutionOutcome = (
    McpToolCompletedOutcome | McpToolSuspendedOutcome | McpToolRejectedOutcome
)
```

request factory必须验证：

- `owner.tool_call_id`、exposed name、original name与 binding exact join；
- binding必须是 `McpToolBindingContract`，其 `binding_identity`进入request fingerprint；
- resume的handle、source suspension event/fact、prepared resolution与当前binding全部精确匹配；
  `source_suspension_event_reference`必须exact-read后得到nested `source_suspension`，其interaction、
  reservation、binding、prepared fingerprint必须逐项等于handle identity；
- suspension candidate binding只接受`candidate_kind=SUSPENSION`；terminal settlement只接受
  `candidate_kind=TERMINAL`。两者的runtime/run/call/reservation与physical owner fingerprint必须和
  invocation/handle逐项相等，owner generation必须是registry当前generation；
- timeout使用已冻结descriptor/resolved call policy，caller不能任意续期；
- rejected message走现有closed sanitizer，不得持久化或返回raw exception。
- completed factory必须递归冻结normalized MCP metadata，再由中央factory加入稳定的
  `provider_kind`、`server_id`与`original_tool_name`；mutable nested dict/list不得越过port；
- `normalized_is_error is False` iff `result_state is ToolResultState.SUCCESS`；
  `normalized_is_error is True` iff `result_state is ToolResultState.ERROR`；其他result state在
  completed branch非法；
- MCP protocol/application返回的`is_error=True`仍是completed outcome：必须保留合法output、
  display payload、artifact candidates和metadata，只在最终ToolResult使用ERROR state；它不得改走
  rejected。rejected只表示binding/lease/request/adapter没有形成一个合法MCP result。

port本身为：

```python
class McpToolExecutionPort(Protocol):
    async def execute(
        self, request: McpToolExecutionRequest
    ) -> McpToolExecutionOutcome: ...

    def bind_suspension_candidate(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity,
    ) -> None: ...

    def confirm_suspension_commit(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        commit_receipt: ToolExecutionStableCandidateCommitReceipt,
    ) -> McpPendingHandleTransitionOutcome: ...

    async def resume(
        self, request: McpToolResumeRequest
    ) -> McpToolExecutionOutcome: ...

    def prepare_terminal_settlement(
        self,
        *,
        pending_handle: McpPendingExecutionHandle,
        reason: McpPendingTerminalReason,
        candidate_owner_identity: ToolExecutionStableCandidateOwnerIdentity,
    ) -> McpPreparedTerminalSettlement: ...

    def confirm_terminal_commit(
        self,
        *,
        settlement: McpPreparedTerminalSettlement,
        commit_receipt: ToolExecutionStableCandidateCommitReceipt,
    ) -> McpPendingHandleTransitionOutcome: ...
```

outcome 是 closed union：

- completed唯一承载result state、normalized output、display payload、metadata、artifact candidates与
  semantics input，并无损覆盖当前application-error结果；
- suspended唯一承载port-owned pending handle；prepared suspension、raw original request/state与pending
  lease owner只能由该handle持有，outer outcome不得复制第二份；
- rejected唯一承载stable code和sanitized message。

`runtime/mcp/tool_execution_port.py` 唯一拥有：

- binding lease acquire/release；
- pending lease promotion/borrow/return/abort/complete；
- process-local pending handle registry、原始request/state与每轮prepared suspension；
- manager call/resume；
- raw MCP result normalization；
- lease failure redaction；
- registry commit receipt到pending lease confirm/abort/complete的physical settlement。

`McpPendingExecutionHandle`的concrete constructor与mutable state均为port private；公开protocol不提供
`complete/abort/revoke`，也不暴露manager、Supervisor或raw lease。handle内部必须保存exact
`PreparedMcpInputRequiredSuspension`，因此resume不再从Agent scratchpad或Supervisor回读
`owned_original_request_json_bytes`、`owned_request_state_json_bytes`、pending reservation或binding。
公开的`suspension_commit_view`是中央factory从该prepared carrier投影的immutable、无raw bytes视图，
只供Agent构造suspension fact/candidate；其fingerprint必须与handle identity中的prepared fingerprint
exact join。pending payload只保存该handle及durable suspension reference；`AgentRuntime`不得调用
`McpServerSupervisor.pending_lease_reservation()`、`borrow_pending_lease()`或
`complete_pending_lease()`。

handle及包含handle的request/outcome均明确不可event/json/pickle/dataclasses-asdict序列化；process-local
fingerprint只编码`McpPendingExecutionHandleIdentity.identity_fingerprint`，不能编码object ID、repr或
mutable state。identity中的prepared fingerprint与reservation/binding是两个独立authority的join，
不是raw payload副本；raw bytes只存在于handle private owner，commit view不得提供thaw/raw accessor。

MCP handle不保存`AgentEvent`、candidate IDs tuple或batch fingerprint。Agent从commit view构造
suspension candidate后，先由`ToolExecutionTerminalRegistry.freeze_suspension()`取得
`ToolExecutionStableCandidateOwnerIdentity`，再在同一无`await`同步段调用
`bind_suspension_candidate()`；terminal candidate同理由registry freeze后把owner identity交给
`prepare_terminal_settlement()`。candidate owner identity中的physical owner fingerprint必须等于
handle identity fingerprint，否则两边都fail closed。若同步bind失败，registry仍是candidate owner，
Host close可按physical fingerprint定位port handle，不形成owner gap。

提交边界冻结为两类：

| 边界 | FULL | NONE | UNKNOWN / PARTIAL |
|---|---|---|---|
| initial suspension | registry先确认exact FULL，再由port confirm reservation；handoff后handle进入`PENDING_CONFIRMED` | registry按`ABANDON_ON_NONE`冻结abandon receipt，port release lease后registry才转入fail-closed terminalization | registry与handle同时保留并进入reconciliation |
| resumed successor suspension | registry FULL receipt后port把successor设为`PENDING_CONFIRMED` | registry独占同一candidate并bounded retry；handle只保留raw successor state/lease，绝不再次调用manager | registry candidate与handle physical owner同时latch |
| terminal ToolResult / closure batch | registry FULL receipt后port complete pending lease；handoff receipt返回后registry才删除owner | registry独占terminal candidate并bounded retry；handle保持`TERMINAL_CANDIDATE_FROZEN` | 两个owner都保留，禁止重复resume或另造terminal result |

`ToolExecutionStableCandidateCommitReceipt`只能由registry从`RuntimeSession` typed write outcome与
private candidate tuple中央lower，不接受caller自报。FULL要求committed refs与owner identity中的ordered
IDs/batch fingerprint逐项相等；NONE要求零committed refs；PARTIAL即使已提交部分也不得settle physical
owner。MCP port每次settlement返回generic `ToolExecutionPhysicalOwnerHandoffReceipt`，registry完成exact
join后才清空candidate/owner；`McpPendingHandleTransitionOutcome`不再复制lease disposition、retry或
reconciliation字段。

permission deny、binding change、expiry、maximum rounds、Host abort与child pending unsupported虽然不会
调用`resume()`，仍必须先通过`prepare_terminal_settlement()`冻结reason与exact terminal batch，再把
RuntimeSession confirmation交给`confirm_terminal_commit()`。普通resume得到completed/application-error
outcome也遵循同一路径。这样“模型结果已构造”不再等价于“pending lease可以释放”。

resume调用借用的短期active borrow与长期pending lease严格分离：port必须在manager
`resume_suspended_request()` physical operation退出的`finally`中归还active borrow，不得等待candidate
freeze、durable FULL或publication。FULL/NONE/UNKNOWN/PARTIAL只决定长期pending lease和handle state；
active borrow count不得被当成durable confirmation owner。这保持当前生产实现的正确时序，并防止慢
EventLog write长期占用manager borrow。

resume adapter/protocol failure若按现有lifecycle写`McpInputRequiredResumeFailedEvent`并允许用户重试，
不得调用terminal settlement：port必须先归还本次pending borrow，再把同一handle恢复为
`PENDING_CONFIRMED`。只有borrow return本身无法确认时才进入`RECONCILIATION_REQUIRED`。该审计event
的FULL/NONE不改变原pending lease的durable suspension authority，也不能促使Agent重新读取
Supervisor；下一次resolution仍携带同一个handle。caller cancellation同样只detach waiter，port-owned
resume/confirmation owner继续收口。

进程重启后没有live handle，继续沿现有typed recovered-lease-unavailable closure收口；不得从durable
fact重建opaque manager state或重新acquire同名binding。Host close必须先drain stable candidate
registry，再用commit receipt drain handle registry；无法在deadline内完成任一侧handoff时进入
reconciliation并阻止静默资源释放。

`McpCapabilityTool` 只保存 `McpToolBindingContract` 与 execution port，不再保存
`McpServerSupervisor`。tool constructor必须验证自己的name与binding name一致。

`capability/providers/mcp.py` 只保留 provider projection和 pure descriptor builder。`build_mcp_installation()` 移到 `runtime/mcp/installation.py`，以消除 capability -> runtime/tools concrete import。

`McpInstalledCapabilitySnapshot.tools: tuple[object, ...]` 删除，`descriptors`收紧为
`tuple[CapabilityDescriptor, ...]`。新的process-local snapshot保存
`ordered_binding_installations: tuple[RuntimeToolBindingInstallation, ...]`；每项同时携带tool、
descriptor ID/fingerprint、`McpToolBindingContract`与artifact policy。snapshot另存的
`binding_identities`必须与installations中MCP contract identity做set-equality，descriptor projection
也必须与installation逐项相等。Host refresh不能再把raw tool tuple交给registry后补建contract。

Host 替换 MCP binding时使用 `McpToolBindingContract` union branch与exact identity筛选；不得
再检查 concrete tool class或读取attribute。normal resume、binding-changed audit和child reverse
index都共享同一 registry identity owner。

### 5.7 subagent command port

新增 `ports/subagent.py`，使用九种closed command与action-specific outcome，而不是把
`SubagentRuntime`整体暴露给tools，也不得以`SubagentToolSucceeded(payload: dict)`重新制造
service locator。

durable facts与process-local port共享的closed vocabulary由低层
`primitives/subagent.py`唯一拥有；D4-0把当前`runtime/subagent/types.py`中的alias物理迁入该模块，
runtime facts/reducer与ports都反向消费它：

```python
SubagentStatus: TypeAlias = Literal[
    "running", "suspended", "completed", "failed", "cancelled"
]
SubagentTaskStatus: TypeAlias = Literal[
    "created", "waiting_dependency", "running",
    "blocked_dependency_failed", "completed", "failed", "cancelled",
]
SubagentRole: TypeAlias = Literal[
    "worker", "verifier", "synthesizer", "orchestrator"
]
SubagentContextMode: TypeAlias = Literal["isolated", "fork"]
SubagentEdgeKind: TypeAlias = Literal[
    "spawn", "send", "followup", "wait", "cancel", "result", "suspend", "resume"
]
SubagentResultSource: TypeAlias = Literal["explicit", "inferred", "none"]
SubagentCapabilityProfileName: TypeAlias = Literal[
    "general_worker", "research_worker", "review_worker", "verification_worker",
    "synthesizer", "orchestrator",
]
SubagentTaskProfileName: TypeAlias = Literal[
    "general_worker", "research_worker", "review_worker", "verification_worker"
]
SubagentCommandKind: TypeAlias = Literal[
    "spawn_agent", "wait_agent", "stop_agent", "list_agents",
    "create_agent_tasks", "wait_agent_tasks", "stop_agent_task",
    "report_agent_phase", "report_agent_result",
]
```

这些alias不是新的durable事实，不进入schema registry；它们只收紧现有合法值集合。`phase`、
`display_role`、human label、reason/message等真正开放文本继续是`str`。任何DTO若语义属于上述closed
集合，不得退回`str`或复制一份不同的inline literal。

所有command共享以下owner；`event_context`、context/model-call attribution和tool call identity
由tool boundary构造并进入command fingerprint：

```python
@dataclass(frozen=True, slots=True)
class SubagentCommandOwner:
    runtime_session_id: str
    run_id: str
    tool_call_id: str
    tool_name: str
    event_context: EventContext
    parent_context_id: str | None
    parent_model_call_index: int | None
    invocation_owner_kind: ToolInvocationOwnerKind
    permission: ToolPermissionInvocation
    bound_child_subagent_run_id: str | None
    owner_fingerprint: str

@dataclass(frozen=True, slots=True)
class SpawnAgentCommand:
    command_kind: Literal["spawn_agent"]
    owner: SubagentCommandOwner
    task: str
    label: str | None
    role: SubagentRole
    context_mode: SubagentContextMode
    command_fingerprint: str

@dataclass(frozen=True, slots=True)
class WaitAgentCommand:
    command_kind: Literal["wait_agent"]
    owner: SubagentCommandOwner
    subagent_run_id: str
    timeout_seconds: float | None
    command_fingerprint: str

@dataclass(frozen=True, slots=True)
class StopAgentCommand:
    command_kind: Literal["stop_agent"]
    owner: SubagentCommandOwner
    subagent_run_id: str
    reason: str | None
    command_fingerprint: str

@dataclass(frozen=True, slots=True)
class ListAgentsCommand:
    command_kind: Literal["list_agents"]
    owner: SubagentCommandOwner
    maximum_items: int
    include_edges: bool
    command_fingerprint: str

@dataclass(frozen=True, slots=True)
class CreateAgentTaskSpec:
    task: str
    profile: SubagentTaskProfileName
    task_key: str | None
    label: str | None
    display_role: str | None
    depends_on: tuple[str, ...]
    spec_fingerprint: str

@dataclass(frozen=True, slots=True)
class CreateAgentTasksCommand:
    command_kind: Literal["create_agent_tasks"]
    owner: SubagentCommandOwner
    ordered_tasks: tuple[CreateAgentTaskSpec, ...]
    command_fingerprint: str

@dataclass(frozen=True, slots=True)
class WaitAgentTasksCommand:
    command_kind: Literal["wait_agent_tasks"]
    owner: SubagentCommandOwner
    task_ids: tuple[str, ...]
    settle: Literal["all", "first"]
    timeout_seconds: float | None
    include_consumed: bool
    command_fingerprint: str

@dataclass(frozen=True, slots=True)
class StopAgentTaskCommand:
    command_kind: Literal["stop_agent_task"]
    owner: SubagentCommandOwner
    task_id: str
    reason: str | None
    command_fingerprint: str

@dataclass(frozen=True, slots=True)
class ReportAgentPhaseCommand:
    command_kind: Literal["report_agent_phase"]
    owner: SubagentCommandOwner
    subagent_run_id: str
    phase: str
    message: str | None
    progress: FrozenJsonObjectFact | None
    command_fingerprint: str

@dataclass(frozen=True, slots=True)
class ReportAgentResultCommand:
    command_kind: Literal["report_agent_result"]
    owner: SubagentCommandOwner
    subagent_run_id: str
    summary: str
    output_preview: str | None
    diagnostics: tuple[FrozenJsonObjectFact, ...]
    command_fingerprint: str

SubagentToolCommand = (
    SpawnAgentCommand
    | WaitAgentCommand
    | StopAgentCommand
    | ListAgentsCommand
    | CreateAgentTasksCommand
    | WaitAgentTasksCommand
    | StopAgentTaskCommand
    | ReportAgentPhaseCommand
    | ReportAgentResultCommand
)
```

factory必须执行当前strict parsing全部规则：non-empty string、role/context/profile closed enum、
`max_items` 1..100、non-negative timeout policy、unique task key、reserved `task:` prefix、dependency
resolution与cycle rejection。`report_*` command中的run ID必须等于owner绑定的child run ID，main
agent不能自报child identity。所有nested progress/diagnostics递归冻结。

success/not-ready/rejected output也逐action冻结：

```python
@dataclass(frozen=True, slots=True)
class SubagentSpawnedOutcome:
    outcome_kind: Literal["spawned"]
    subagent_run_id: str
    child_runtime_session_id: str
    label: str | None
    role: SubagentRole
    context_mode: SubagentContextMode
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentCollectedResultView:
    subagent_run_id: str
    task_id: str | None
    status: Literal["completed", "failed", "cancelled"]
    result_id: str
    summary: str
    output_preview: str | None
    result_artifact_id: str | None
    artifact_ids: tuple[str, ...]
    result_source: Literal["explicit", "inferred"]
    diagnostics: tuple[FrozenJsonObjectFact, ...]
    view_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentWaitCompletedOutcome:
    outcome_kind: Literal["wait_completed"]
    result: SubagentCollectedResultView
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentRunTerminalWithoutResultOutcome:
    outcome_kind: Literal["terminal_without_result"]
    subagent_run_id: str
    task_id: str | None
    status: Literal["failed", "cancelled"]
    reason_code: str
    terminal_event_id: str
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentRunStoppedOutcome:
    outcome_kind: Literal["run_stopped"]
    subagent_run_id: str
    status: Literal["cancelled", "completed", "failed"]
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentTaskProjectionView:
    item_kind: Literal["task"]
    task_id: str
    subagent_run_id: str | None
    child_runtime_session_id: str | None
    status: SubagentTaskStatus
    pending_state: str | None
    label: str | None
    task_key: str | None
    profile_id: str
    display_role: str | None
    objective_preview: str
    depends_on: tuple[str, ...]
    has_child_run: bool
    run_index: int | None
    phase: str | None
    result_id: str | None
    result_artifact_id: str | None
    delivered: bool
    consumed_by_wait: bool
    item_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentRunProjectionView:
    item_kind: Literal["run"]
    subagent_run_id: str
    child_runtime_session_id: str
    status: SubagentStatus
    label: str | None
    role: SubagentRole
    phase: str | None
    result_id: str | None
    result_artifact_id: str | None
    delivered: bool
    consumed_by_wait: bool
    item_fingerprint: str

SubagentProjectionItemView = SubagentTaskProjectionView | SubagentRunProjectionView

@dataclass(frozen=True, slots=True)
class SubagentGraphEdgeView:
    edge_id: str
    edge_kind: SubagentEdgeKind
    subagent_run_id: str
    source_tool_call_id: str | None
    source_tool_name: str | None
    result_id: str | None
    returned_to_tool_call_id: str | None
    created_at: str
    edge_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentInventoryOutcome:
    outcome_kind: Literal["inventory"]
    parent_runtime_session_id: str
    items: tuple[SubagentProjectionItemView, ...]
    edges: tuple[SubagentGraphEdgeView, ...]
    total_items: int
    total_edges: int
    items_truncated: bool
    edges_truncated: bool
    diagnostics: tuple[FrozenJsonObjectFact, ...]
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentTaskStartedView:
    task_id: str
    task_key: str | None
    label: str | None
    profile: SubagentTaskProfileName
    status: SubagentTaskStatus
    subagent_run_id: str | None
    child_runtime_session_id: str | None
    view_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentTaskBatchAcceptedOutcome:
    outcome_kind: Literal["task_batch_accepted"]
    batch_id: str
    started_count: int
    tasks: tuple[SubagentTaskStartedView, ...]
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentTaskWaitResultView:
    task_id: str
    task_key: str | None
    status: Literal["completed", "failed", "cancelled", "blocked_dependency_failed"]
    subagent_run_id: str | None
    child_runtime_session_id: str | None
    result_id: str | None
    summary: str | None
    output_preview: str | None
    result_artifact_id: str | None
    artifact_ids: tuple[str, ...]
    result_source: Literal["explicit", "inferred", "none"]
    consumed: bool
    view_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentTasksWaitedOutcome:
    outcome_kind: Literal["tasks_waited"]
    settle: Literal["all", "first"]
    results: tuple[SubagentTaskWaitResultView, ...]
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentTaskStoppedOutcome:
    outcome_kind: Literal["task_stopped"]
    task_id: str
    status: SubagentTaskStatus
    subagent_run_id: str | None
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentPhaseReportedOutcome:
    outcome_kind: Literal["phase_reported"]
    subagent_run_id: str
    phase: str
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentResultSubmittedOutcome:
    outcome_kind: Literal["result_submitted"]
    subagent_run_id: str
    result_id: str
    summary: str
    outcome_fingerprint: str

@dataclass(frozen=True, slots=True)
class SubagentToolNotReadyOutcome:
    outcome_kind: Literal["not_ready"]
    command_kind: Literal["wait_agent"]
    subagent_run_ids: tuple[str, ...]
    outcome_fingerprint: str

class SubagentToolRejectCode(StrEnum):
    MALFORMED_ARGUMENTS = "malformed_arguments"
    NOT_FOUND = "not_found"
    LIMIT_EXCEEDED = "limit_exceeded"
    INVALID_TRANSITION = "invalid_transition"
    BATCH_PREFLIGHT_FAILED = "batch_preflight_failed"
    BATCH_START_FAILED = "batch_start_failed"
    OWNER_MISMATCH = "owner_mismatch"
    CONTRACT_MISMATCH = "contract_mismatch"

@dataclass(frozen=True, slots=True)
class SubagentToolRejectedOutcome:
    outcome_kind: Literal["rejected"]
    command_kind: SubagentCommandKind
    reject_code: SubagentToolRejectCode
    sanitized_message: str
    batch_id: str | None
    failed_stage: Literal["preflight", "post_commit_start"] | None
    failed_task_keys: tuple[str, ...]
    diagnostics: tuple[FrozenJsonObjectFact, ...]
    outcome_fingerprint: str

SubagentToolOutcome = (
    SubagentSpawnedOutcome
    | SubagentWaitCompletedOutcome
    | SubagentRunTerminalWithoutResultOutcome
    | SubagentRunStoppedOutcome
    | SubagentInventoryOutcome
    | SubagentTaskBatchAcceptedOutcome
    | SubagentTasksWaitedOutcome
    | SubagentTaskStoppedOutcome
    | SubagentPhaseReportedOutcome
    | SubagentResultSubmittedOutcome
    | SubagentToolNotReadyOutcome
    | SubagentToolRejectedOutcome
)

class SubagentControlPort(Protocol):
    async def execute(
        self,
        command: SubagentToolCommand,
    ) -> SubagentToolOutcome: ...
```

`runtime/subagent/tool_port.py` 吸收当前 `tools/builtins/subagent.py` 中依赖 graph/tasks/runtime exceptions 的 orchestration；tool 文件只负责 JSON Schema、strict parsing 与 provider result rendering。

每种command只有上表对应的success branch；例如`create_agent_tasks`不能返回generic
`SubagentSpawnedOutcome`，`wait_agent`不能返回inventory。batch ID、planned task ID与repair ID仍由
runtime port按当前算法创建；durable facts/event reducer不迁入process-local outcome。existing
`SubagentResult`/graph projection由port立即深冻为view，tool adapter不得在返回后再次读取runtime。
所有outcome/command fingerprint由central factory从完整closed字段重算；rejected message与
diagnostics走closed sanitizer，不得直接冻结`str(exc)`或mutable exception payload。

这样不会建立一组与 durable subagent facts 平行的真源：command/outcome 仅是 process-local invocation carrier，durable truth仍由现有 subagent events/reducer拥有。

### 5.8 D3 projection contracts 与 ports

新增：

```text
projection_jobs/contracts.py
  durable DTO / enum / central fingerprint factories

ports/projection_jobs.py
  RuntimeWriteAdmissionGuard
  RuntimeSessionOwnerBootstrapPort
  CanonicalMutationCommitPort
  DurableProjectionSeedCommitPort
  pre-activation / source / result read ports
```

#### 5.8.1 Canonical mutation同事务能力

现有 `VerifiedPostgresTransactionHandle` 只有四个字符串property，不能合法操作 UOW 已打开的
physical transaction。它删除，不得通过downcast、隐藏 `_connection`、全局 token-to-connection
registry或另开connection替代。

最终public边界不是“给UOW一个不能使用的opaque borrow”，而是owner-scoped
`MemoryUowTransactionScope`。scope由memory package拥有，并一次性装配所有绑定同一physical
connection的typed repositories：

```python
@dataclass(frozen=True, slots=True)
class CanonicalMutationTransactionIdentity:
    schema_binding_fingerprint: str
    connection_provider_borrower_id: str
    transaction_owner_id: str
    transaction_generation: int
    backend_pid: int
    admission_epoch_fingerprint: str
    admission_guard_lock_identity_fingerprint: str
    identity_fingerprint: str

class CanonicalMutationCommitPort(Protocol):
    @property
    def transaction_identity(self) -> CanonicalMutationTransactionIdentity: ...

    def append_bundle(
        self, *, bundle: PreparedCanonicalMutationBundleFact
    ) -> CanonicalMutationBundleAppendReceiptFact: ...

class CanonicalMutationWriterPort(Protocol):
    def append_bundle(
        self,
        *,
        bundle: PreparedCanonicalMutationBundleFact,
        deadline_monotonic: float,
    ) -> CanonicalMutationBundleAppendReceiptFact: ...

class MemoryUowFacadeKind(StrEnum):
    GRAPH = "graph"
    DECISIONS = "decisions"
    MUTATION_OUTBOX = "mutation_outbox"
    RUNTIME_EVENT_OUTBOX = "runtime_event_outbox"
    LIFECYCLE = "lifecycle"
    WRITE_SERVICE = "write_service"

class MemoryUowScopeLeaseState(StrEnum):
    ACTIVE = "active"
    REVOKING = "revoking"
    REVOKED = "revoked"
    RELEASED = "released"
    RECONCILIATION_REQUIRED = "reconciliation_required"

@dataclass(frozen=True, slots=True)
class MemoryUowScopeLeaseIdentity:
    scope_id: str
    scope_generation: int
    transaction_identity: CanonicalMutationTransactionIdentity
    owner_thread_identity: str
    identity_fingerprint: str

@dataclass(frozen=True, slots=True)
class MemoryUowScopedFacadeIdentity:
    facade_id: str
    facade_kind: MemoryUowFacadeKind
    scope_lease_identity_fingerprint: str
    facade_generation: int
    identity_fingerprint: str

class MemoryUowScopeLease(Protocol):
    """Shared borrower gate; revoke is private to the scope owner."""
    @property
    def identity(self) -> MemoryUowScopeLeaseIdentity: ...
    @property
    def state(self) -> MemoryUowScopeLeaseState: ...

    def borrow_operation(
        self,
        *,
        facade_identity: MemoryUowScopedFacadeIdentity,
    ) -> ContextManager[None]: ...

class MemoryUowScopedFacade(Protocol):
    @property
    def facade_identity(self) -> MemoryUowScopedFacadeIdentity: ...

class MemoryUowGraphFacade(GraphStore, MemoryUowScopedFacade, Protocol): ...
class MemoryUowDecisionFacade(
    GovernanceDecisionRepository, MemoryUowScopedFacade, Protocol
): ...
class MemoryUowMutationOutboxFacade(
    GovernanceOutboxRepository, MemoryUowScopedFacade, Protocol
): ...
class MemoryUowRuntimeEventOutboxFacade(
    GovernanceRuntimeEventOutboxRepository, MemoryUowScopedFacade, Protocol
): ...

class MemoryUowLifecycleFacade(MemoryUowScopedFacade, Protocol):
    def supersede(
        self, *, old_id: str, new_id: str,
        governance_batch_id: str, graph_id: str | None = None,
    ) -> list[AgentEvent]: ...
    def mark_stale(
        self, *, node_id: str,
        governance_batch_id: str, graph_id: str | None = None,
    ) -> list[AgentEvent]: ...
    def link_contradiction(
        self, *, left_id: str, right_id: str,
        governance_batch_id: str, graph_id: str | None = None,
    ) -> list[AgentEvent]: ...

class MemoryUowWriteServiceFacade(MemoryUowScopedFacade, Protocol):
    def submit(
        self,
        candidate: MemoryCandidate | Mapping[str, Any],
        *,
        event_context: EventContext,
    ) -> MemoryWriteOutcome: ...

@dataclass(frozen=True, slots=True)
class MemoryUowRepositoryBundle:
    scope_lease_identity: MemoryUowScopeLeaseIdentity
    graph: MemoryUowGraphFacade
    decisions: MemoryUowDecisionFacade
    outbox: MemoryUowMutationOutboxFacade
    runtime_events: MemoryUowRuntimeEventOutboxFacade
    lifecycle: MemoryUowLifecycleFacade
    memory_write_service: MemoryUowWriteServiceFacade
    bundle_identity_fingerprint: str

@dataclass(frozen=True, slots=True)
class LockedCanonicalMemoryView:
    memory_id: str
    frozen_document: FrozenJsonObjectFact
    revision: int
    view_fingerprint: str

class MemoryUowTransactionScope(Protocol):
    @property
    def transaction_identity(self) -> CanonicalMutationTransactionIdentity: ...
    @property
    def repositories(self) -> MemoryUowRepositoryBundle: ...
    @property
    def scope_lease_identity(self) -> MemoryUowScopeLeaseIdentity: ...
    @property
    def active(self) -> bool: ...
    @property
    def resolved_graph_id(self) -> str: ...

    def ensure_event_context_rows(self, context: EventContext) -> None: ...
    def lock_canonical_memory(
        self, memory_id: str
    ) -> LockedCanonicalMemoryView | None: ...
    def assert_active(self) -> None: ...

@dataclass(frozen=True, slots=True)
class MemoryUowScopeRequest:
    runtime_session_id: str
    workspace_root: str | None
    graph_id: str
    session_bootstrap_state: RuntimeSessionBootstrapStateFact
    transaction_owner_id: str
    transaction_generation: int
    surface_plan: CanonicalMutationSurfacePlanFact
    memory_write_gate_contract_fingerprint: str
    deadline_monotonic: float
    request_fingerprint: str

class MemoryUowTransactionScopeFactory(Protocol):
    def open_scope(
        self, *, request: MemoryUowScopeRequest
    ) -> ContextManager[MemoryUowTransactionScope]: ...
```

`GovernanceDecisionRepository`、`GovernanceOutboxRepository`与
`GovernanceRuntimeEventOutboxRepository`沿用当前`GovernanceWriteUnitOfWork`窄方法集，但公开bundle
字段必须是上述scope facade，不得是raw concrete repository。每个facade保存同一个
`MemoryUowScopeLease`的borrower reference与自己的closed facade identity；它的每一个public method都
必须先进入`lease.borrow_operation()`，重验scope ID、generation、transaction identity、facade kind与
`ACTIVE`状态，再调用module-private delegate。禁止使用`__getattr__`、公开`delegate/connection/cursor`
property或把concrete repository直接cast成facade。

`MemoryWriteUnitOfWork.__enter__()`只调用`scope_factory.open_scope()`，随后把六个现有public属性逐项
绑定到`scope.repositories`中的facade；`ensure_event_context_rows()`与
`lock_canonical_memory()`委托scope自己的typed operation。UOW不再自行构造
`PostgresGraphStore`、decision/outbox/event repositories或cursor。caller可以暂存任意facade引用，
但scope进入`REVOKING`后该引用的下一次调用必须稳定抛出typed
`MemoryUowScopeLeaseReleasedError`，绝不能碰到已经归还pool或被下一generation复用的connection。
为保持现有`GovernanceWriteUnitOfWork`行为，UOW将`LockedCanonicalMemoryView.frozen_document`
thaw为本次caller独占的deep copy并返回`(document, revision)`；scope/SQL owner不共享mutable row。
现有session owner bootstrap仍由UOW通过注入的bootstrap port先执行；只有FULL的exact
`RuntimeSessionBootstrapStateFact`能进入scope request，scope必须重验其runtime/workspace与
request相等。mutation commit port只被scope factory注入`OutboxRepository`，不作为UOW或bundle
public属性暴露。

为了让scope factory合法构造这些repository，低层storage只向exact factory authority开放一个
sealed、lexically bounded connection seam：

```python
@final
class MemoryUowScopeFactoryAuthority:
    """Opaque process identity; constructor owned by production composition."""
    __slots__ = ("_nonce",)

@final
class CanonicalMutationDriverAuthority:
    __slots__ = ("_nonce",)

@dataclass(frozen=True, slots=True)
class MemoryUowPhysicalTransactionRequest:
    transaction_owner_id: str
    transaction_generation: int
    deadline_monotonic: float
    scope_request_fingerprint: str
    request_fingerprint: str

class MemoryUowPhysicalTransactionCapability(Protocol):
    @property
    def transaction_identity(self) -> CanonicalMutationTransactionIdentity: ...
    @property
    def active(self) -> bool: ...

    def borrow_for_scope_factory(
        self, *, authority: MemoryUowScopeFactoryAuthority
    ) -> ContextManager[Connection]: ...

    def borrow_for_mutation_driver(
        self, *, authority: CanonicalMutationDriverAuthority
    ) -> ContextManager[Connection]: ...

    def issue_canonical_mutation_commit_port(
        self, *, authority: MemoryUowScopeFactoryAuthority
    ) -> CanonicalMutationCommitPort: ...

class VerifiedPostgresConnectionProviderProtocol(Protocol):
    def memory_uow_physical_transaction(
        self,
        *,
        request: MemoryUowPhysicalTransactionRequest,
        scope_factory_authority: MemoryUowScopeFactoryAuthority,
        mutation_driver: "PostgresCanonicalMutationTransactionDriverPort",
    ) -> ContextManager[MemoryUowPhysicalTransactionCapability]: ...

class PostgresCanonicalMutationTransactionDriverPort(Protocol):
    @property
    def driver_authority(self) -> CanonicalMutationDriverAuthority: ...

    def append_on_transaction(
        self,
        *,
        transaction: MemoryUowPhysicalTransactionCapability,
        bundle: PreparedCanonicalMutationBundleFact,
    ) -> CanonicalMutationBundleAppendReceiptFact: ...
```

`PostgresMemoryUowTransactionScopeFactory`位于
`memory/canonical/postgres_uow_scope.py`。它由composition获得唯一
`MemoryUowScopeFactoryAuthority`，并在construction时绑定connection provider、mutation driver与
exact `MemoryWriteGate`；gate contract fingerprint必须等于scope request。进入
`memory_uow_physical_transaction()`后，以exact object
identity借用connection并在同一lexical scope内构造：

1. `PostgresGraphStore(connection)`；
2. `CandidateDecisionRepository(connection)`；
3. `GovernanceEventOutboxRepository(connection, runtime_session_id)`；
4. 接收`CanonicalMutationCommitPort`的`OutboxRepository`；
5. 基于同一graph的`MemoryLifecycle`与`MemoryWriteService`；
6. 实现event-context SQL与canonical-memory row lock的scope methods。

文件归属不得倒置：`CanonicalMutationTransactionIdentity`、commit/writer port、两个opaque
authority、`MemoryUowPhysicalTransactionRequest/Capability`和driver port位于
`ports/projection_jobs.py`；`MemoryUowScopeRequest`、repository bundle、scope与scope factory位于
`memory/canonical/uow_contracts.py`。scope factory从完整scope request唯一lower物理request，后者
只含transaction checkout所需identity/deadline；storage因此只依赖ports，不导入memory package。

这些raw对象只保存在`postgres_uow_scope.py`的private delegate set中；factory用六个显式wrapper
包裹后，公开`MemoryUowRepositoryBundle`只返回facade。facade、bundle和scope均不得暴露delegate，
也不得支持pickle/dataclasses.asdict等generic live-object serialization。factory authority按对象
身份比较、不可序列化；任意字符串/fingerprint不能替代。只有该factory module可调用
`borrow_for_scope_factory()`；architecture test禁止其他production caller。这样UOW的完整SQL行为
有正式实现路径，同时`CanonicalMutationCommitPort`、Outbox、executor与其他consumer看不到
generic connection。

physical capability内部绑定exact connection、borrower facade、backend PID、normal
`RuntimeWriteAdmissionGuardHandle`、owner/generation和注入的mutation driver。commit port调用
driver时，driver以exact `CanonicalMutationDriverAuthority`在调用动态作用域内借用同一
connection，只能执行closed `append PreparedCanonicalMutationBundleFact`；它不能接收任意
SQL/object name/callable/cursor。scope-factory authority与driver authority是两个不同nominal
token，不能互换。driver implementation仍唯一位于
`runtime/projection_jobs/mutation_writer.py`。

生命周期固定为：

```text
SCOPE_ISSUED
  -> ACTIVE(owner_id, generation, exact connection + normal-write guard)
  -> SHARED_SCOPE_LEASE_ACTIVE
  -> SIX_FACADES_BOUND + MUTATION_PORT_ISSUED
  -> EXITING
      -> REVOKING (atomically reject new facade/commit-port borrows)
      -> drain exact in-flight facade operations
      -> REVOKED (all six facades + mutation port)
      -> COMMIT | ROLLBACK
  -> COMMITTED | ROLLED_BACK
  -> RELEASED
```

硬规则：

- `PostgresMemoryUowTransactionScopeFactory`是唯一scope/repository/commit-port issuer；每个UOW
  generation恰好一个scope、一个bundle与一个mutation port；
- `MemoryWriteUnitOfWork`只拥有scope context manager；commit/rollback由scope的`__exit__`依据
  exception状态执行，UOW consumer与repository均不能直接调用；
- `revoke`从所有public protocol物理删除；只有private scope-lease owner能原子进入`REVOKING`；
  internal capability必须在commit/rollback之前撤销六个facade与mutation port，之后任何operation
  fail closed；
- `borrow_operation()`在进入和退出时维护bounded active-operation count。scope exit先禁止新borrow，
  再在原scope absolute deadline内等待count归零；超时必须cancel/rollback并discard physical
  connection、标记`RECONCILIATION_REQUIRED`并抛错，不能把仍被operation使用的connection归还pool；
- facade operation不得跨owner thread/task转移；若实现允许线程切换，则operation borrow必须显式
  携带owner token并由相同borrow关闭，不能只比较thread ID；
- mutation port每次append都重验borrower未释放、UOW owner/generation、backend PID、transaction
  status与exact normal admission guard；
- repository bundle构造时验证六种`facade_kind`恰好各一项、所有facade lease identity object及
  fingerprint与bundle nested identity完全相等；禁止从另一个connection/scope混入graph、decision、
  两类outbox、lifecycle、write service或mutation port；
- bundle append、canonical head CAS、mutation row与delivery rows继续使用同一physical
  transaction；
- port只有`append_bundle`，不暴露`execute/cursor/sql/callable` generic escape hatch；
- receipt仍为现有D3 DTO/fingerprint；process identity不进入durable mutation semantic；
- architecture guard只允许`memory/canonical/postgres_uow_scope.py`以exact authority借用UOW
  connection，只允许`storage/postgres_transaction_capability.py`与
  `runtime/projection_jobs/mutation_writer.py`访问sealed issuance/driver internals。

`PostgresGraphStore`、`CandidateDecisionRepository`、`OutboxRepository`、
`GovernanceEventOutboxRepository`、`MemoryLifecycle`和`MemoryWriteService`都只允许出现在scope
factory的private delegate fields。AST/type architecture gate禁止它们成为
`MemoryUowRepositoryBundle`字段或`MemoryWriteUnitOfWork`公开属性的runtime value。回归必须分别在
scope退出后保留并调用graph、decision、mutation outbox、runtime-event outbox、lifecycle、write
service六种facade，同时覆盖commit、rollback与pooled connection被下一generation复用三种情况；
六者都必须在任何SQL前以相同stable error fail closed。

`OutboxRepository` constructor改为接收本次UOW的 `CanonicalMutationCommitPort`；它不再构造
`CanonicalMutationV2Writer(connection=...)`。`DurableGraphFacade`的非UOW mutation path则接收
process-scoped `CanonicalMutationWriterPort`，后者自行借用合法
`PROJECTION_MAINTENANCE` transaction；两种port
不可互换或伪装成同一connection owner。

#### 5.8.2 Canonical mutation三文件拆分

当前 `runtime/projection_jobs/canonical_mutation.py` 实际是完整PostgreSQL allocator/repository，
不是pure helper。最终物理落点固定为：

```text
projection_jobs/canonical_mutation.py
  pure sequence-key、semantic、bundle、surface-plan normalization/fingerprint factory

runtime/projection_jobs/postgres_canonical_mutation_repository.py
  从旧 canonical_mutation.py 原样迁移的 advisory lock、head CAS、
  mutation/delivery insert、exact confirmation SQL repository

runtime/projection_jobs/mutation_writer.py
  PostgresCanonicalMutationTransactionDriver、borrower-scoped commit行为、
  process-scoped writer orchestration；不再拥有pure DTO factory
```

`migration_transform.py`、`mutation_writer.py`、`postgres_repository.py` 三个现有调用方必须
改从新 SQL repository owner导入。旧文件只在所有call site迁移后删除；不得把repository
误搬进top-level contract package。

#### 5.8.3 Projection migration preparation port

storage migration runner当前函数内import runtime pre-activation/transform。D4-0先冻结完整
process-local contract：

```python
@dataclass(frozen=True, slots=True)
class ProjectionMigrationTransactionIdentity:
    database_target_fingerprint: str
    database_oid: int
    backend_pid: int
    current_head_version: int
    current_registry_prefix_fingerprint: str
    maintenance_operation_id: str
    maintenance_epoch_fingerprint: str
    transaction_generation: int
    identity_fingerprint: str

class ProjectionMigrationTransactionCapability(Protocol):
    @property
    def transaction_identity(self) -> ProjectionMigrationTransactionIdentity: ...
    def assert_active(self) -> None: ...
    def borrow_for_port(
        self, *, authority: ProjectionMigrationPortAuthority
    ) -> ContextManager[Connection]: ...

@final
class ProjectionMigrationPortAuthority:
    """Opaque process identity issued with the migration port instance."""
    __slots__ = ("_nonce",)

@dataclass(frozen=True, slots=True)
class ProjectionMigrationReadinessView:
    legacy_surface_binding_plan_ready: bool
    timeline_coverage_ready: bool
    evidence_coverage_ready: bool
    authority_fingerprint: str

@dataclass(frozen=True, slots=True)
class ProjectionMigrationPreparationReportView:
    preparation_kind: Literal[
        "legacy_surface_binding_plan.v1",
        "run_timeline_pre_activation_coverage.v1",
        "tool_result_evidence_pre_activation_coverage.v1",
    ]
    target_migration_version: int
    maintenance_operation_id: str
    maintenance_epoch_fingerprint: str
    durable_authority_fingerprint: str
    item_count: int

class ProjectionMigrationPreparationPort(Protocol):
    @property
    def port_authority(self) -> ProjectionMigrationPortAuthority: ...

    def readiness(
        self,
        *,
        transaction: ProjectionMigrationTransactionCapability,
        current_head_version: int,
        database_target_fingerprint: str,
    ) -> ProjectionMigrationReadinessView: ...

    def apply_transform(
        self,
        *,
        transaction: ProjectionMigrationTransactionCapability,
        version: int,
        maintenance_epoch: RuntimeWriteAdmissionEpochFact,
        resulting_registry_prefix_fingerprint: str,
    ) -> None: ...

    def protected_relation_resource_for_version(self, version: int) -> str: ...

    def prepare_legacy_surface_bindings(
        self, *, deadline_monotonic: float
    ) -> ProjectionMigrationPreparationReportView: ...

    def drain_pre_activation(
        self,
        *,
        kind: DurableProjectionKind,
        deadline_monotonic: float,
    ) -> ProjectionMigrationPreparationReportView: ...
```

`ProjectionMigrationTransactionCapability` 由runner在已验证admin/runtime target、migration
advisory lock、exact maintenance epoch与当前transaction内唯一签发；只对注入的projection
migration implementation开放sealed physical borrow，transaction退出后失效。concrete handle
不公开无约束connection/cursor；`borrow_for_port()`要求exact opaque port authority object，
并重验backend PID、head/prefix、maintenance operation、epoch与generation。CLI和runner由
composition root取得同一 `PostgresProjectionMigrationPreparationPort`，不得各自local import
runtime coordinator。readiness/report所有字段由implementation从durable plan/coverage receipt
重算，caller不能自报。

`PostgresMigrationRunner` constructor接收required port；当target registry包含projection
migrations而port缺失时，在打开mutation transaction前返回typed
`PROJECTION_PREPARATION_PORT_REQUIRED`，不得回退local import。`pulsara db projections ...`
由CLI composition创建同一implementation；普通runtime schema verify只读top-level migration
state，不构造runtime projection service。

物理迁移规则：

1. `runtime/projection_jobs/contracts.py` 中的 event-safe facts原样迁移到top-level package；
2. process-local `Protocol` 迁到ports；旧 `VerifiedPostgresTransactionHandle` 删除并由上述
   sealed transaction capability取代；
3. pure normalization/fingerprint factory从现有`mutation_writer.py`及其他pure helper迁到
   `projection_jobs/canonical_mutation.py`；旧`canonical_mutation.py`的SQL repository迁到
   `runtime/projection_jobs/postgres_canonical_mutation_repository.py`；
4. `runtime/projection_jobs/migration_state.py` 的stable state/pure classifier迁到
   `projection_jobs/migration_state.py`；
5. `graph_relation.py` 的JSON-LD/quad lowering迁到`graph/projection_relations.py`；
6. storage migration runner只调用`ProjectionMigrationPreparationPort`，实现仍由runtime
   composition注入；禁止runner函数内import pre-activation/transform implementation；
7. `CanonicalMutationV2Writer` 继续是runtime implementation，但memory UOW只持有本次UOW
   borrower-scoped `CanonicalMutationCommitPort`，graph facade持有独立process writer port；
8. `storage/postgres_connection_provider.py::_admit_runtime_transaction()` 必须返回并保留
   `RuntimeWriteAdmissionGuardHandle`，不能再取得后丢弃；MEMORY_UOW checkout用它构造sealed
   transaction handle；
9. migration runner与CLI只依赖注入的`ProjectionMigrationPreparationPort`，不得函数内import
   `runtime.projection_jobs.pre_activation|migration_transform|projection_handlers`。

迁移不得修改任何 `schema_version`、domain-separated hash、SQL payload、transaction boundary
或D3 result receipt。Python module path不属于 durable identity，也不得被新加入fingerprint。
durable fact registration composition必须显式import新`projection_jobs.contracts`一次；旧模块删除后，
historical decoder registry的ordered `(schema_version, schema_fingerprint)`集合必须逐字相等，
不得同时import新旧模块造成duplicate registration。

### 5.9 禁止 mega-port

不得定义：

```python
class RuntimeServices(Protocol):
    def get(self, name: str) -> object: ...
```

也不得把 `RuntimeSession` 改名为 `RuntimePort` 后继续透传。每个 concrete tool constructor 只能收到它实际调用的方法集合。

---

## 6. ToolExecutor 与唯一 composition owner

### 6.1 ToolExecutor 迁移

`tools/executor.py` 移到 `runtime/tool_executor.py`。

它继续拥有：

- ToolResult start/delta preparation；
- sync/async invoke；
- cancellation/error normalization；
- artifact processing port；
- result semantic builder；
- prepared terminal result。

它不再通过 tools facade导入 contract，而从 `ports` 导入。

### 6.2 删除 `RuntimeSession.create_tool_executor()`

新增 `runtime/tool_composition.py`：

```python
@dataclass(frozen=True, slots=True)
class RuntimeToolBindingInstallation:
    tool: Tool | AsyncTool
    binding_contract: ToolBindingContract
    descriptor_id: str
    descriptor_fingerprint: str
    artifact_processing_policy: ToolResultArtifactProcessingPolicy
    installation_fingerprint: str

@dataclass(frozen=True, slots=True)
class RuntimeToolCompositionInput:
    workspace_root: Path
    runtime_session_id: str
    artifact_read_port: ToolArtifactReadPort
    artifact_processing_port: ToolResultArtifactProcessingPort
    terminal_command_port: TerminalCommandPort
    terminal_process_port: TerminalProcessPort
    terminal_monitor_port: TerminalMonitorPort
    subagent_control_port: SubagentControlPort | None
    subagent_exposure: MainSubagentTools | ChildSubagentTools | NoSubagentTools
    memory_proposal_sink: MemoryProposalSink | None
    memory_recall_service: MemoryRecallService | None
    memory_query: MemoryQuery | None
    graph_id: str | None
    memory_read_scopes: frozenset[str] | None
    dynamic_tool_installations: tuple[RuntimeToolBindingInstallation, ...]
```

`build_runtime_tool_executor(input, recorder)` 是唯一 production factory。

builtin catalog也先lower为同一 `RuntimeToolBindingInstallation`。factory逐项验证tool name、
origin-aware binding、descriptor ID/fingerprint与artifact policy exact join；不得把raw
`extra_tool_bindings: tuple[Tool, ...]` 注入RuntimeSession后再从tool attribute补齐语义。

允许 `runtime/tool_composition.py` 在 composition boundary读取 `RuntimeSession` 的各个字段来构造上述 input，但它必须逐字段赋值；不得把 session存入 input或 tool。

### 6.3 capability refresh

`AgentRuntime.refresh_capability_runtime()` 继续重建 registry，但复用同一套 frozen runtime ports。MCP tool binding替换后：

- descriptor names == registry names；
- binding contract fingerprint exact match；
- terminal/artifact/subagent ports不重建；
- permission state不进入 tool constructor；
- provider tools变化仍按现有 generation compatibility触发合法 rollover。

---

## 7. 单一 builtin tool catalog

新增 `capability/builtin_catalog.py`，定义唯一生产 catalog：

```python
class BuiltinToolBindingKind(StrEnum):
    FILESYSTEM = "filesystem"
    ARTIFACT_READ = "artifact_read"
    MEMORY_PROPOSAL = "memory_proposal"
    MEMORY_RECALL = "memory_recall"
    MEMORY_QUERY = "memory_query"
    PLAN_WORKFLOW = "plan_workflow"
    TERMINAL_COMMAND = "terminal_command"
    TERMINAL_PROCESS = "terminal_process"
    TERMINAL_MONITOR = "terminal_monitor"
    TODO_LOCAL_STATE = "todo_local_state"
    SUBAGENT_CONTROL = "subagent_control"

class BuiltinToolAvailabilityKind(StrEnum):
    ALWAYS = "always"
    REQUIRES_ARTIFACT_READ_PORT = "requires_artifact_read_port"
    REQUIRES_MEMORY_PROPOSAL_PORT = "requires_memory_proposal_port"
    REQUIRES_MEMORY_RECALL_PORT = "requires_memory_recall_port"
    REQUIRES_MEMORY_QUERY_PORT = "requires_memory_query_port"
    REQUIRES_TERMINAL_PORTS = "requires_terminal_ports"
    REQUIRES_MAIN_SUBAGENT_CONTROL = "requires_main_subagent_control"
    REQUIRES_CHILD_REPORT_CONTROL = "requires_child_report_control"

@dataclass(frozen=True, slots=True)
class BuiltinToolAvailabilityRequirement:
    kind: BuiltinToolAvailabilityKind
    allowed_invocation_owners: tuple[ToolInvocationOwnerKind, ...]
    requirement_fingerprint: str

@dataclass(frozen=True, slots=True)
class BuiltinToolCatalogEntry:
    name: str
    descriptor: CapabilityDescriptor
    binding_contract: BuiltinToolBindingContract
    execution_binding_kind: BuiltinToolBindingKind
    availability_requirement: BuiltinToolAvailabilityRequirement
    permission_contract: BuiltinToolPermissionContract
    recovery_contract: BuiltinToolRecoveryContract
    tool_family: Literal[
        "filesystem", "artifact", "memory_read", "memory_write",
        "terminal", "plan", "subagent_parent", "subagent_child", "local_state"
]
```

`BuiltinToolAvailabilityRequirement` 由中央 factory重算。required port由closed
`AVAILABILITY_PORT_REQUIREMENT_BY_KIND`表从kind唯一派生，不在DTO中重复保存字符串；kind与
`execution_binding_kind` 具有固定矩阵。main subagent tools只允许Host main owner，child report
tools只允许child owner。composition root按requirement过滤catalog，缺required port时不允许
“注册后运行时报错”。

2026-07-26 builtin matrix穷尽如下；同一cell内名称按字典序冻结：

| Tool names | Binding kind | Availability kind | Invocation owners |
|---|---|---|---|
| `artifact_read` | `ARTIFACT_READ` | `REQUIRES_ARTIFACT_READ_PORT` | main, child |
| `edit_file`, `read_file`, `search_files`, `write_file` | `FILESYSTEM` | `ALWAYS` | main, child |
| `terminal` | `TERMINAL_COMMAND` | `REQUIRES_TERMINAL_PORTS` | main, child |
| `terminal_process` | `TERMINAL_PROCESS` | `REQUIRES_TERMINAL_PORTS` | main, child |
| `terminal_monitor` | `TERMINAL_MONITOR` | `REQUIRES_TERMINAL_PORTS` | main, child；child autonomous scheduling仍由terminal port fail closed |
| `todo` | `TODO_LOCAL_STATE` | `ALWAYS` | main, child |
| `ask_plan_question`, `enter_plan`, `exit_plan` | `PLAN_WORKFLOW` | `ALWAYS` | main, child |
| `memory_search` | `MEMORY_RECALL` | `REQUIRES_MEMORY_RECALL_PORT` | main, child |
| `memory_explain`, `memory_get` | `MEMORY_QUERY` | `REQUIRES_MEMORY_QUERY_PORT` | main, child |
| `remember_action_boundary`, `remember_claim`, `remember_decision`, `remember_observation`, `remember_preference` | `MEMORY_PROPOSAL` | `REQUIRES_MEMORY_PROPOSAL_PORT` | main, child |
| `create_agent_tasks`, `list_agents`, `spawn_agent`, `stop_agent`, `stop_agent_task`, `wait_agent`, `wait_agent_tasks` | `SUBAGENT_CONTROL` | `REQUIRES_MAIN_SUBAGENT_CONTROL` | main only |
| `report_agent_phase`, `report_agent_result` | `SUBAGENT_CONTROL` | `REQUIRES_CHILD_REPORT_CONTROL` | child only |

catalog gate要求上述name set与`builtin_tool_descriptors()`逐字相等；新增builtin必须在同一PR
选择既有closed branch或先修改本契约，不能落入默认binding/availability。

permission defaults不再复制 descriptor。`CapabilityDescriptor.permission_category` 与
`CapabilityDescriptor.is_read_only` 是唯一默认真源；permission contract只描述action override
与terminal-specific rule：

```python
@dataclass(frozen=True, slots=True)
class BuiltinToolPermissionContract:
    ordered_action_overrides: tuple[BuiltinActionPermissionOverride, ...]
    terminal_rule: BuiltinTerminalPermissionRule
    contract_fingerprint: str

@dataclass(frozen=True, slots=True)
class BuiltinActionPermissionOverride:
    discriminator_field: str
    discriminator_value: str
    permission_category: str
    allowed_in_read_only: bool

class BuiltinTerminalPermissionRuleKind(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    ALL_ACTIONS = "all_actions"
    CLOSED_ACTION_SET = "closed_action_set"

@dataclass(frozen=True, slots=True)
class BuiltinTerminalPermissionRule:
    kind: BuiltinTerminalPermissionRuleKind
    ordered_terminal_access_actions: tuple[str, ...]
    ordered_scheduling_permission_actions: tuple[str, ...]
    rule_fingerprint: str

@dataclass(frozen=True, slots=True)
class BuiltinToolRecoveryContract:
    severity: Literal[
        "read_only", "bounded_write", "terminal", "unknown_effect"
    ]
    include_in_unfinished_recovery: bool
    contract_fingerprint: str
```

因此`terminal_process.list|log|poll|wait`、`terminal_monitor.list`等action-level语义也由
catalog明确表达，不允许在`permission.py`再保留一组字符串set。override匹配必须由closed
classifier执行；缺字段/未知action使用default fail-closed分类。

V1 recovery event只稳定保存tool name，没有可安全依赖的action carrier，因此不定义
`BuiltinActionRecoveryOverride`。unfinished-call severity只按catalog entry的单一`severity`
解析；未来若要action级recovery，必须先新增typed durable call-argument authority，不能从
free-form segments临时猜测。

为保持当前行为，recovery matrix固定为：

- `terminal|terminal_process|terminal_monitor`：`terminal`；
- `edit_file|write_file`：`bounded_write`；
- `artifact_read|read_file|search_files`：`read_only`；
- 其余builtin：`unknown_effect`；
- `ask_plan_question|enter_plan|exit_plan` additionally
  `include_in_unfinished_recovery=False`，其余为True。

permission validator必须证明：

- 非override调用的category/read-only逐字来自entry descriptor；
- 每个override discriminator值属于descriptor JSON Schema的closed enum；
- override values排序唯一，unknown action fail closed；
- terminal/scheduling action tuple各自排序唯一、有固定32项上限，且都属于同一closed schema；
- scheduling action必须同时属于terminal-access action，`terminal_monitor.register|cancel`按当前
  permission behavior进入该集合，read-only `list`不进入scheduling集合；
- terminal rule与binding kind矩阵一致，非terminal binding只能`NOT_APPLICABLE`；
- catalog fingerprint覆盖descriptor fingerprint、override与terminal rule；
- 不存在 `default_permission_category` 或 `allowed_in_read_only_by_default` 第二字段。

V1 action override矩阵只包含：

| Tool | Actions | Override |
|---|---|---|
| `terminal_process` | `list`, `log`, `poll`, `wait` | `allowed_in_read_only=True`, category=`terminal_process_observe` |
| `terminal_monitor` | `list` | `allowed_in_read_only=True`, category=`terminal_monitor_observe` |

三件terminal工具的其他调用均使用各自descriptor默认值；terminal access rule覆盖全部terminal
actions，scheduling permission rule只覆盖`terminal_monitor.register|cancel`。这张表取代当前
`TERMINAL_*_READ_ONLY_ACTIONS`多份set，但不改变permission结果。

catalog 是以下语义的唯一 name-level owner：

- descriptor；
- result render contract；
- Long-Horizon action policy contract；
- permission category与 read-only product grant；
- recovery severity；
- tool family；
- main/child/conditional availability；
- execution binding contract。

`CapabilityDescriptor.input_schema` 与 `metadata` 在catalog中必须是 recursively immutable
carrier；`BuiltinToolCapabilityProvider` 每次只投影owned deep copy。禁止把catalog内部dict
直接交给adapter或test修改。

runtime action classifier仍可有 executable implementation registry，但它必须按 descriptor 中的 classifier contract resolve，不得再按 tool name复制 policy。

删除：

- `runtime/tool_taxonomy.py`；
- `runtime/tool_action.py::builtin_tool_action_policy()` 的 name switch；
- `runtime/recovery.py` 的 name sets；
- `runtime/permission.py` 的独立 builtin name allowlist。

组合期强校验：

```text
catalog callable names
    == capability descriptor names
    == active ToolRegistry binding names
       （按 availability requirement过滤）

entry.binding_contract.tool_name
    == entry.name

catalog descriptor fingerprint
    == capability execution-surface descriptor fingerprint

catalog input-schema fingerprint
    == final provider tool-definition schema fingerprint
```

concrete tool不再参与read-only/concurrency/schema/description equality，因为这些字段已经从
execution protocol物理删除。dynamic MCP/custom binding必须提供独立的descriptor artifact与
binding contract，并在同一 execution-surface freeze中完成exact join。

MCP/custom dynamic tool不进入 builtin catalog；它们由动态 descriptor + binding contract exact join拥有。

---

## 8. message/event/replay 分层

### 8.1 message replay 移出 schema package

新增 top-level `replay/`：

- `replay/message_assembler.py`：由 `message/assembler.py` 迁入；
- `replay/message_reducer.py`：由 `message/reducer.py` 迁入。

`message/` 只保留 `blocks.py`、`message.py` 与 schema facade。

更新 runtime、event_log、inspector、tests 的 direct imports。旧 module删除，不保留 re-export。

### 8.2 candidate payload下沉

将 `event/candidates.py` 中被 primitives 依赖的 candidate payload union、candidate value schema移动到 `primitives/memory_candidate.py`。event candidate wrapper可依赖该 primitive，不得反向。

### 8.3 ToolResult receipt归位

`primitives/runtime_event_vocabulary.py` 中嵌入完整 `ToolResultBlock` 的
`CurrentToolResultReceiptItem` / `CurrentToolResultBatchReceipt` 继承
`FrozenRuntimeStateBase`，没有schema version，只由 `AgentRuntime` 本次tool batch与compaction
audit使用。它们是 process-local replay carrier，不是durable event fact。

迁移到 `replay/tool_result_receipts.py`：

- 保持现有字段、validator、bounded item count与fingerprint算法逐字不变；
- `runtime/agent.py` 从replay owner direct import；
- event schema不得import或嵌入该receipt；
- 不注册historical decoder，不新增/修改event schema；
- durable authority仍是receipt内引用的`ToolResultEndEvent`与terminal projection event，receipt
  本身不持久化，也不得被Inspector当作独立event truth。

---

## 9. Test-support hard cut

### 9.1 新增测试 composition

新增 `tests/support/runtime_factory.py`：

- `build_component_test_runtime_wiring()`；
- `build_component_test_agent_runtime_wiring()`；
- `ComponentTestRuntimeWiring`（如生产 `RuntimeWiring` 已足够则直接复用）；
- explicit in-memory event/graph/archive/candidate/outbox composition；
- 只使用 test-side fake governance UOW；
- 默认 `allow_unbootstrapped_test_events=True`，并在返回 receipt中标记非 durability evidence。

名字必须包含 `test` 或 `component_test`；禁止继续叫 `build_in_memory_runtime_wiring`，避免被误认为产品入口。

### 9.2 Mock MCP

将 `MockMcpClientManager` 与 handler type从 `runtime/mcp/manager.py` 移到 `tests/support/mcp.py`。

production `runtime/mcp/manager.py` 只保留 `McpClientManager` protocol。`runtime/mcp/__init__.py` 删除 mock export。

### 9.3 Fake governance UOW

将以下实现从 `memory/canonical/unit_of_work.py` 移到 `tests/support/memory_uow.py`：

- `_PoolDecisionRepository`；
- `_NoopOutboxRepository`；
- `InMemoryMemoryWriteUnitOfWork`。

测试类改名 `FakeMemoryWriteUnitOfWork`，直接实现 production `GovernanceWriteUnitOfWork` protocol，不继承 production fake。

### 9.4 Component Host composition

Host composition contract不属于低层`ports`：它必然引用runtime wiring和Host resource
lease，放入`ports/`会违反第3节自己的层次。新增
`host/composition_contract.py`，只用于Host composition boundary：

```python
@dataclass(frozen=True, slots=True)
class HostRuntimeBuildFact:
    resolved_settings_semantic_fingerprint: str
    workspace_root: str
    workspace_identity_fingerprint: str
    runtime_session_id: str | None
    graph_id: str | None
    memory_domain_id: str | None
    memory_domain_semantic_fingerprint: str | None
    model_role: ModelRole
    llm_options_semantic_fingerprint: str | None
    system_prompt: str | None
    system_prompt_fingerprint: str | None
    memory_reflection: bool
    memory_reflection_options_fingerprint: str | None
    enable_workspace_skills: bool
    capability_runtime_semantic_fingerprint: str | None
    terminal_binding_semantic_fingerprint: str
    permission_policy_semantic_fingerprint: str
    mcp_supervisor_contract_fingerprint: str
    mcp_installation_semantic_fingerprint: str
    fact_fingerprint: str

@dataclass(frozen=True, slots=True)
class HostRuntimeLiveBindingIdentity:
    binding_kind: Literal[
        "settings",
        "memory_domain",
        "llm_options",
        "memory_reflection_options",
        "capability_runtime",
        "terminal_runtime",
        "permission_policy",
        "mcp_supervisor",
        "mcp_installation",
    ]
    process_owner_id: str
    binding_generation: int
    semantic_contract_fingerprint: str
    identity_fingerprint: str

@dataclass(frozen=True, slots=True)
class HostRuntimeLiveBindings:
    settings: PulsaraSettings
    settings_identity: HostRuntimeLiveBindingIdentity
    memory_domain: MemoryDomainContext | None
    memory_domain_identity: HostRuntimeLiveBindingIdentity | None
    llm_options: LLMOptions | None
    llm_options_identity: HostRuntimeLiveBindingIdentity | None
    memory_reflection_options: MemoryReflectionOptions | None
    memory_reflection_options_identity: HostRuntimeLiveBindingIdentity | None
    capability_runtime_override: CapabilityRuntime | None
    capability_runtime_identity: HostRuntimeLiveBindingIdentity | None
    terminal_binding: TerminalRuntimeBinding
    terminal_binding_identity: HostRuntimeLiveBindingIdentity
    permission_policy: EffectivePermissionPolicy
    permission_policy_identity: HostRuntimeLiveBindingIdentity
    mcp_supervisor: McpServerSupervisor
    mcp_supervisor_identity: HostRuntimeLiveBindingIdentity
    mcp_installation: McpInstalledCapabilitySnapshot
    mcp_installation_identity: HostRuntimeLiveBindingIdentity
    ordered_binding_identity_fingerprints: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class HostRuntimeBuildAdmission:
    build_fact: HostRuntimeBuildFact
    live_bindings: HostRuntimeLiveBindings
    reopen_deadline_monotonic: float | None
    admission_generation: int
    admission_fingerprint: str

class HostProcessResourceLease(Protocol):
    @property
    def lease_id(self) -> str: ...
    @property
    def postgres_access_lease(self) -> VerifiedPostgresAccessLease: ...
    @property
    def retrieval_resources(self) -> RetrievalRuntimeResources: ...
    @property
    def projection_service(self) -> RuntimeProjectionServicePort: ...
    @property
    def governance_coordinator(self) -> MemoryGovernanceCoordinator: ...
    @property
    def schema_binding_fingerprint(self) -> str: ...
    @property
    def lease_generation(self) -> int: ...
    @property
    def resource_fingerprint(self) -> str: ...
    @property
    def released(self) -> bool: ...

    async def release(self, *, deadline_monotonic: float) -> None: ...

@dataclass(frozen=True, slots=True)
class HostAgentRuntimeWiringOutcome:
    agent_runtime_wiring: AgentRuntimeWiring
    runtime_session_id: str
    process_resource_lease_id: str
    schema_binding_fingerprint: str
    outcome_fingerprint: str

class HostSessionManifestStorePort(Protocol):
    def get(self, runtime_session_id: str) -> SessionManifest | None: ...
    def list_resumable(
        self,
        *,
        workspace_root: str | Path | None,
        memory_domain_id: str | None,
        include_closed: bool,
        limit: int,
    ) -> tuple[ResumableSessionSummary, ...]: ...
    def upsert_open_manifest(
        self,
        *,
        runtime_session_id: str,
        conversation_id: str,
        workspace: ResolvedWorkspace,
        model_role: ModelRole,
        permission_policy: EffectivePermissionPolicy,
        created_by: str,
    ) -> SessionManifest: ...
    def touch(self, runtime_session_id: str) -> None: ...
    def mark_closed(self, runtime_session_id: str) -> None: ...

class RuntimeProjectionServicePort(Protocol):
    @property
    def accepting(self) -> bool: ...
    async def start(self) -> None: ...
    def wake(self, runtime_session_id: str | None = None) -> None: ...
    async def aclose(self, *, deadline_monotonic: float) -> None: ...

class HostRuntimeComposition(Protocol):
    async def acquire_process_resources(
        self, *, deadline_monotonic: float
    ) -> HostProcessResourceLease: ...

    def build_agent_runtime_wiring(
        self,
        *,
        admission: HostRuntimeBuildAdmission,
        resources: HostProcessResourceLease,
    ) -> HostAgentRuntimeWiringOutcome: ...

    def session_manifest_store(
        self, *, resources: HostProcessResourceLease
    ) -> HostSessionManifestStorePort: ...
```

字段/invariant冻结：

- `HostRuntimeBuildFact`必须拥有全部会改变wiring/provider semantics的当前
  `build_agent_runtime_wiring()`输入；只允许递归immutable值或semantic fingerprint，禁止live
  object、deadline、secret-bearing settings、`**kwargs`或`dict[str, object]`；
- `fact_fingerprint`只从fact完整字段由中央factory重算。live binding object、process owner ID、
  generation与reopen deadline绝不进入该semantic fingerprint；
- 每个live object必须有closed `binding_kind`的`HostRuntimeLiveBindingIdentity`；identity中的
  `semantic_contract_fingerprint`必须与build fact对应字段相等。optional object与identity必须同时
  present或同时absent；MCP supervisor identity必须匹配build fact的supervisor contract并与
  installation server/snapshot generation exact join；
- 每个typed slot只接受同名`binding_kind`，例如terminal object不能携带settings identity；object
  rebinding后必须生成新generation/identity，不能沿用旧process identity；
- `ordered_binding_identity_fingerprints`顺序由closed kind registry固定，不接受caller排序；
  `HostRuntimeLiveBindings`与`HostRuntimeBuildAdmission`是process-local、不可pickle/asdict/event
  serialize的carrier，不拥有semantic fingerprint；
- `admission_fingerprint`是operational CAS，只覆盖build fact fingerprint、ordered live binding
  identities、admission generation与deadline representation；不得被当作provider/durable semantic；
- build admission必须拥有全部当前行为输入；composition不得回读`HostCore` mutable fields；
- process resource lease中的PostgreSQL、retrieval、projection、governance都来自同一production
  composition attempt，schema fingerprint与borrower lease exact join；
- build outcome的runtime session ID必须等于constructed wiring，resource lease ID/fingerprint必须
  等于输入lease；
- `HostCore`只持有composition与上述lease/outcome，不再分别实现第二套
  `_get_postgres_access_lease/_get_retrieval_resources/_get_projection_service` composition logic；
- resource release有单一owner且close-blocked语义沿用现有projection physical owner contract；
- production lease显式禁止pickle/asdict/generic event serialization，release后任何resource
  checkout与build admission fail closed；
- test composition可实现同一protocol，但test lease必须使用不同nominal class并标记
  `durability_evidence=False`，不能塞入production outcome。

manifest port的四个查询参数逐项保留当前生产语义：`workspace_root`与`memory_domain_id`共同限定
workspace/domain，`include_closed`决定closed-session visibility，`limit`在store内执行bounded
ordering。任何adapter不得只按workspace过滤或在Host侧先取无界结果再补过滤。

这是唯一允许的较粗 composition contract，且只能被 `host/core.py` 持有；不得下传到 RuntimeSession/AgentRuntime/tool。

production 实现位于 `host/production_composition.py`，始终要求 verified PostgreSQL、retrieval resources、projection service与 durable manifest。

`tests/support/host.py::component_test_host_core()` 注入 test implementation。test implementation使用相同 Host lifecycle control flow，但以 in-memory manifest/resource lease满足 contract；它不是 production export，也不得被 `src/` import。

静态规则：

- `HostCore` dataclass constructor不再public，唯一production issuer是
  `HostCore.production(...)`；
- module-private `_from_component_test_composition(...)` 不进入`__all__`，AST guard固定
  唯一外部call site为`tests/support/host.py`；
- production call site不得传 `_composition` 或直接构造`HostRuntimeComposition`；
- tests只能通过 `tests.support.host` 构造 component Host；
- test Host不得作为 durable correctness evidence。

### 9.5 保留与迁移矩阵

| 对象 | 最终位置 | 说明 |
|---|---|---|
| `InMemoryEventLog` | 可暂留 production低层模块 | pure deterministic adapter，非产品 composition selector |
| `InMemoryGraphStore` | 同上 | 组件 fake |
| `InMemoryArchiveStore` | 同上 | 组件 fake |
| `InMemoryCandidatePool` | 同上 | 组件 fake |
| `InMemoryToolResultArtifactIndex` | `tests/support/artifacts.py` | 仅测试/benchmark使用 |
| whole in-memory runtime wiring | `tests/support/runtime_factory.py` | production删除 |
| in-memory governance UOW | `tests/support/memory_uow.py` | production删除 |
| mock MCP manager | `tests/support/mcp.py` | production删除 |

---

## 10. Production Host durable hard cut

### 10.1 API

最终 production入口：

```python
core = HostCore.production(
    settings=settings,
    scratch_root=scratch_root,
)
```

删除：

- `HostCore.durable` field；
- `HostCore(settings=..., durable=True|False)`；
- `build_agent_runtime_wiring(..., durable=...)`；
- `build_in_memory_runtime_wiring()`；
- `runtime.__all__["build_in_memory_runtime_wiring"]`。

`build_agent_runtime_wiring()` 直接调用 durable wiring，不再用 conditional expression。

### 10.2 Host control flow

删除 `if self.durable` / `if not self.durable` 后：

- rollout feasibility always verified；
- open always acquires verified Postgres access；
- retrieval/projection resources always resolved；
- resume/list/repair always available；
- manifest open/touch/close always执行；
- close retry/tombstone always durable；
- inspector/CLI不再选择另一 composition。

component tests通过 injected test composition满足同一 method call，不在 HostCore 中恢复布尔分支。

### 10.3 durable session兼容

D4 不改变 durable schema与event payload，因此：

- 旧 durable session可由新 binary reopen；
- 不需要 PostgreSQL migration；
- 不允许旧 binary与新 binary在同一 Python process热切换；
- deploy使用 process restart；
- 已运行的 model/tool owner先按现有 Host close contract drain。

---

## 11. Facade hard cut

### 11.1 `runtime/__init__.py`

删除 `_LAZY_EXPORTS`、`__getattr__` 与 convenience symbol routing。

最终只保留 package docstring和空 `__all__`，或至多 eager export不触发任何高层 import的少量 stable protocol。V1选择空 facade，所有调用方 direct import owning module。

### 11.2 `tools/__init__.py`

删除 lazy router。只 eager export `ToolRegistry`；tool contracts从 `pulsara_agent.ports.tool_execution` 导入，built-in class从具体 module导入。

删除 public：

- `ToolExecutor`；
- `build_core_tool_registry`；
- built-in convenience re-export；
- `ToolCall` 等 port DTO convenience re-export。

### 11.3 长期契约

`contracts/PACKAGE_FACADE_CONTRACT.zh.md` 当前明确要求 runtime/tools保持 lazy；D4 必须原子改写该契约，说明：

- package facade不再用于规避 cycle；
- runtime/tools direct module import是唯一支持路径；
- 新 facade export需要 architecture test证明 eager import无副作用且不产生反向边。

---

## 12. 分阶段实施

每阶段必须独立全绿；不得在一个阶段留下“新 guard + 旧 production producer”半迁移。

### D4-0：依赖快照与最终类型所有权迁移

内容：

- 新增 AST scanner、D4 target DAG policy与全局 SCC diagnostic baseline；
- 新增 `ports`与低层primitive package，并把本规格全部protocol/carrier的Python class定义物理迁到
  最终owner；D4-0不是复制一份additive shadow DTO；
- 完整迁移origin-aware binding union、artifact policy/views、MCP request/outcome/pending handle、
  terminal三组closed request/outcome、subagent九种command/action outcome、owner-scoped memory UOW
  scope/facade、projection migration port与Host semantic/live split carriers；
- 旧owner module只允许`from final.owner import X as X`的exact temporary re-export；所有symbol要求
  `old.X is final.X`，class/enum/protocol再检查module/qualname，assignment/union/PEP 695 alias按3.4节
  检查唯一AST owner与canonical shape；禁止subclass、wrapper、copy、adapter class或重复定义；
- 新增`D4TypeOwnershipCutoverLedger`，逐symbol记录kind、old path、final path、branch-specific identity/
  shape observation、temporary consumer与最迟删除phase；未登记re-export或ledger增长均失败；
- 新增 closed builtin binding/availability/permission/recovery DTO与 set-equality shadow validator；
- 从全量canonical AST import observations记录runtime/tools target entry、residual SCC exact
  observation golden与派生global SCC member/edge accumulator；
- 不切production behavior binding；现有实现通过exact re-export继续运行，D4-2/D4-3只切constructor、
  composition和import path并删除re-export，不再迁移类型所有权。

Gate：

```bash
uv run pytest -q \
  tests/test_dependency_architecture.py \
  tests/test_d4_port_contracts.py \
  tests/test_d4_type_owner_identity.py \
  tests/test_tools.py \
  tests/test_capability_surface.py
```

独立完成标准：任何新增/改写residual SCC canonical import observation都会失败，即使package
pair已存在；合法acyclic import允许，target DAG新增forbidden edge仍失败。现存target edge只能由
exact observation cutover ledger暂时承载并按owner phase单调删除。global SCC只做派生non-growth
diagnostic，D4-0不要求清除D5/D6现存边。
所有最终owner DTO都必须有constructor/validator/fingerprint test，不能只提交空Protocol或省略号
签名。旧路径/最终路径分别import后的对象identity必须相同；class-like symbol比较MRO、module、qualname与
Pydantic/dataclass metadata，alias-like symbol比较唯一AST owner与canonical origin/args/value shape；
factory return type也必须指向同一最终symbol。

### D4-1：Schema/replay 与 D3 lower-layer contract cut

内容：

- message assembler/reducer移入replay；
- candidate payload与ToolResult receipt归位；
- D3 projection durable DTO迁到top-level contracts；
- projection process protocols迁到ports；
- canonical mutation pure factory、PostgreSQL repository与writer按三文件拆分；
- MEMORY_UOW安装owner-scoped scope factory、共享revocable lease与同connection scoped facade bundle，
  同事务回归覆盖graph/decision/event outbox/mutation outbox/lifecycle/write service、commit、rollback、
  六种retained facade的release后调用与wrong generation；
- migration runner/CLI通过完整`ProjectionMigrationPreparationPort`调用，删除runtime local import；
- graph relation lowering归graph；
- storage/graph/memory对`runtime.projection_jobs`的反向import归零；
- schema/golden fingerprints逐项证明不变。

Gate：

```bash
uv run pytest -q \
  tests/test_block_assembler.py \
  tests/test_event_message_system.py \
  tests/test_durable_projection_architecture.py \
  tests/test_durable_projection_jobs.py \
  tests/test_durable_projection_seed.py \
  tests/test_durable_projection_timeline.py \
  tests/test_durable_projection_postgres.py \
  tests/test_projection_job_transaction_capability.py \
  tests/test_memory_uow_transaction_scope.py \
  tests/test_projection_migration_port.py \
  tests/test_schema_migrations.py \
  tests/test_dependency_architecture.py
```

### D4-2：Tool contract + executor vertical cut

内容：

- production imports切到D4-0已拥有类型的`ports/tool_execution.py`与
  `ports/tool_result_semantics.py`，删除`tools/base.py`临时re-export；
- ToolCall/result metadata递归冻结；
- concrete execution binding删除descriptor fields；
- 迁移`tools/executor.py`到runtime；
- 升级`ToolExecutionTerminalRegistry`为两类stable candidate owner，suspension NONE policy、generic
  confirmation与close drain先以现有producer shadow/contract test接入；
- 更新runtime、capability、tools与tests import；本阶段不允许重新定义或包装D4-0 carrier；
- 删除`RuntimeSession.create_tool_executor()`；
- 安装`runtime/tool_composition.py`；
- 删除旧文件，不留shim。

Gate：

```bash
uv run pytest -q \
  tests/test_tools.py \
  tests/test_runtime_session.py \
  tests/test_agent_runtime_loop.py \
  tests/test_tool_execution_stable_candidate_owner.py \
  tests/test_capability_surface.py \
  tests/test_long_horizon_tool_action.py \
  tests/test_dependency_architecture.py
```

### D4-3：Artifact/terminal/MCP/subagent ports + catalog cut

这是不可拆分vertical migration：D4-0已定义的port、runtime adapter、tool constructor与composition
必须同一提交切换。

内容：

- artifact read/processing port；
- execution-surface freeze生成exact artifact processing policy，删除descriptor/default fallback；
- terminal command/process/monitor port；
- terminal八种process action与三种monitor action逐branch outcome/prepared-owner join；
- MCP execution port、pending handle confirmation/settlement、application-error completed mapping、
  origin-aware binding union与installation builder行为切换；
- MCP pending handle与registry candidate owner完成vertical handoff；任何MCP handle/scratchpad不保存
  stable event tuple，resume active borrow在manager operation退出时立即归还；
- subagent九种command与action-specific outcome port行为切换；
- typed permission invocation；
- builtin catalog成为唯一taxonomy/model-visible descriptor owner，descriptor独占默认
  permission/read-only语义；
- tools -> runtime imports归零；
- capability -> runtime/tools imports归零。
- 删除terminal/MCP/subagent旧类型re-export；`D4TypeOwnershipCutoverLedger`对应项归零。

Gate：

```bash
uv run pytest -q \
  tests/test_tools.py \
  tests/test_permission_policy.py \
  tests/test_capability_surface.py \
  tests/test_capability_mcp.py \
  tests/test_mcp_host_lifecycle.py \
  tests/test_tool_artifact_processing_policy.py \
  tests/test_tool_binding_contracts.py \
  tests/test_mcp_tool_execution_port.py \
  tests/test_tool_execution_stable_candidate_owner.py \
  tests/test_terminal_tool_ports.py \
  tests/test_terminal_runtime.py \
  tests/test_terminal_monitor_tm1_tm5.py \
  tests/test_subagent_tool_port.py \
  tests/test_subagent_runtime.py \
  tests/test_dependency_architecture.py
```

### D4-4：Test-support、durable Host 与 facade原子切换

内容：

- mock MCP移入tests/support；
- fake governance UOW移入tests/support；
- component test runtime/Host factory落地；
- Host build semantic fact与live bindings分离；manifest四维查询保持原行为；
- 全部测试不再导入production fake composition；
- `HostCore` always durable；
- wiring删除durable selector/in-memory factory；
- production mock/fake symbols删除；
- runtime/tools lazy facade删除；
- CLI、benchmarks、tests全部direct import；
- 长期contracts同步。

Gate：

```bash
uv run pytest -q \
  tests/test_cli_host.py \
  tests/test_host_core.py \
  tests/test_host_resume.py \
  tests/test_host_lifecycle_contract.py \
  tests/test_host_composition_contract.py \
  tests/test_runtime_wiring.py \
  tests/test_memory_governance_engine.py \
  tests/test_context_compaction.py \
  tests/test_package_facade.py \
  tests/test_dependency_architecture.py
```

若仓库当前没有`tests/test_package_facade.py`，D4-4必须新增，不得只依赖import smoke。

### D4-5：最终收紧与 debt closure

内容：

- D4 target DAG五组forbidden edge与production/test-support exception全部清零；
- 删除D4 target-edge cutover ledger及其loader，最终gate直接扫描零命中；
- 删除`D4TypeOwnershipCutoverLedger`及全部temporary old-path re-export；最终owner以外不得存在同名
  class definition或compatibility shim；
- global SCC diagnostic baseline不得增长，并输出remaining D5/D6 package edges供下一债务使用；
- full pytest；
- migrated PostgreSQL Host integration；
- frozen dogfood最小验证；
- contracts/debt文档更新；
- static grep/AST DoD审计。

Gate：

```bash
uv run pytest -q

uv run python -m benchmarks.suites.run_core_dogfood validate

# 真实 API 只需验证 composition-sensitive 场景，不因 D4 重跑所有历史 real-LLM：
PULSARA_RUN_CORE_DOGFOOD=1 uv run python -m benchmarks.suites.run_core_dogfood run \
  --scenario durable-resume \
  --scenario subagent-delegation \
  --scenario workspace-patch \
  --env-file .env \
  --confirm-network
```

---

## 13. 逐文件修改清单

### 13.1 新增 production 文件

| 文件 | 修改 |
|---|---|
| `src/pulsara_agent/ports/__init__.py` | 空、eager、无 lazy router；只声明 package。 |
| `src/pulsara_agent/ports/tool_execution.py` | ToolCall/result/context/tool protocols，以及stable candidate owner/commit/handoff process-local carriers。 |
| `src/pulsara_agent/ports/tool_result_semantics.py` | process-local semantics input protocol、frozen carrier与domain submission factories。 |
| `src/pulsara_agent/ports/tool_registry.py` | binding contract与registry read port。 |
| `src/pulsara_agent/ports/artifact.py` | artifact read/process ports与view DTO。 |
| `src/pulsara_agent/ports/terminal.py` | terminal value、prepared carriers与三个行为port；monitor outcome不复制prepared事实。 |
| `src/pulsara_agent/ports/mcp.py` | MCP execute/resume request/outcome、pending handle、registry commit receipt/physical handoff seam与port。 |
| `src/pulsara_agent/ports/subagent.py` | 9种command、outcome与control port。 |
| `src/pulsara_agent/ports/projection_jobs.py` | D3 commit/read/migration protocols与process handles。 |
| `src/pulsara_agent/projection_jobs/__init__.py` | low-level contract package；不得导入runtime implementation。 |
| `src/pulsara_agent/projection_jobs/contracts.py` | 原D3 durable facts/enums/factories，schema identity不变。 |
| `src/pulsara_agent/projection_jobs/canonical_mutation.py` | pure canonical mutation normalization/fingerprint。 |
| `src/pulsara_agent/projection_jobs/migration_state.py` | migration preparation/coverage stable state与pure classifier。 |
| `src/pulsara_agent/replay/__init__.py` | replay package，禁止反向导入runtime。 |
| `src/pulsara_agent/replay/message_assembler.py` | 原 message assembler。 |
| `src/pulsara_agent/replay/message_reducer.py` | 原 message reducer。 |
| `src/pulsara_agent/replay/tool_result_receipts.py` | process-local current ToolResult batch receipt；不注册event schema。 |
| `src/pulsara_agent/graph/projection_relations.py` | immutable relation fact的PostgreSQL/JSON-LD lowering与read composition。 |
| `src/pulsara_agent/runtime/tool_executor.py` | 原 tools executor，runtime-owned。 |
| `src/pulsara_agent/runtime/tool_composition.py` | 唯一 core tool registry/executor composition。 |
| `src/pulsara_agent/runtime/terminal/tool_port.py` | owner-scoped terminal port implementation。 |
| `src/pulsara_agent/runtime/mcp/tool_execution_port.py` | MCP lease/call/resume、raw request/state pending handle、durable confirmation settlement与close drain owner。 |
| `src/pulsara_agent/runtime/mcp/installation.py` | MCP descriptor + binding installation composition。 |
| `src/pulsara_agent/runtime/subagent/tool_port.py` | subagent command execution adapter。 |
| `src/pulsara_agent/runtime/projection_jobs/postgres_canonical_mutation_repository.py` | canonical mutation allocator/head CAS/mutation与delivery SQL repository。 |
| `src/pulsara_agent/memory/canonical/uow_contracts.py` | `MemoryUowScopeRequest`、共享revocable lease、六个scoped facade、transaction scope与bundle protocol。 |
| `src/pulsara_agent/memory/canonical/postgres_uow_scope.py` | 唯一scope factory；private raw delegates、facade wrappers、operation borrow/drain及commit/rollback/revoke owner。 |
| `src/pulsara_agent/host/composition_contract.py` | Host-level coarse composition protocol；不属于低层ports。 |
| `src/pulsara_agent/host/production_composition.py` | durable Host composition implementation。 |
| `src/pulsara_agent/capability/builtin_catalog.py` | 唯一 builtin catalog。 |
| `src/pulsara_agent/capability/terminal_risk.py` | pure hardline/risk classifier，供permission与tools共用。 |
| `src/pulsara_agent/primitives/memory_candidate.py` | event-neutral candidate payload schema。 |
| `src/pulsara_agent/primitives/subagent.py` | subagent status/task/role/context/edge/profile/result-source closed vocabulary唯一owner。 |
| `src/pulsara_agent/storage/postgres_transaction_capability.py` | sealed MEMORY_UOW transaction borrow与revocation owner。 |
| `src/pulsara_agent/storage/migrations/transaction_capability.py` | migration-runner issued projection migration transaction capability。 |

### 13.2 修改 production 文件：tools/runtime 主链

| 文件 | 修改 |
|---|---|
| `src/pulsara_agent/tools/registry.py` | 从ports导入contract；实现RegistryReadPort。 |
| `src/pulsara_agent/tools/builtins/registry.py` | input改为explicit ports/composition DTO；删除RuntimeSession/PermissionState import、`isinstance(RuntimeSession)`与metadata字典解析。 |
| `src/pulsara_agent/tools/builtins/artifact.py` | constructor改为`ToolArtifactReadPort`。 |
| `src/pulsara_agent/tools/builtins/filesystem.py` | ToolCall/result从ports；删除class-owned descriptor schema/flags。 |
| `src/pulsara_agent/tools/builtins/memory.py` | ToolCall/result从ports；保留proposal行为，descriptor由catalog拥有。 |
| `src/pulsara_agent/tools/builtins/memory_query.py` | ToolCall/result从ports；删除class-owned descriptor fields。 |
| `src/pulsara_agent/tools/builtins/plan.py` | ToolCall/result从ports；workflow行为不变。 |
| `src/pulsara_agent/tools/builtins/terminal.py` | constructor改为`TerminalCommandPort`；删除manager/permission/artifact concrete imports。 |
| `src/pulsara_agent/tools/builtins/terminal_process.py` | 使用`TerminalProcessPort`与typed invocation permission。 |
| `src/pulsara_agent/tools/builtins/terminal_monitor.py` | 使用`TerminalMonitorPort`；capacity rejection走port outcome。 |
| `src/pulsara_agent/tools/builtins/subagent.py` | 只parse/render；调用`SubagentControlPort.execute()`。 |
| `src/pulsara_agent/terminal_public_api.py` | description/schema facade改从`ports.terminal`读取唯一strict input union；删除重复request class。 |
| `src/pulsara_agent/tools/builtins/todo.py` | ToolCall/result从ports；descriptor fields移入catalog。 |
| `src/pulsara_agent/tools/builtins/workspace.py` | ToolCall/result从ports；不新增runtime依赖。 |
| `src/pulsara_agent/tools/adapters/mcp.py` | 持有`McpToolExecutionPort`；suspended结果只传pending handle；active resume borrow在manager operation的`finally`归还；删除supervisor/manager lease logic。 |
| `src/pulsara_agent/runtime/agent.py` | 从ports/runtime executor direct import；调用tool composition；MCP durable write后回传typed confirmation，删除Supervisor pending-lease回读/complete。 |
| `src/pulsara_agent/runtime/session.py` | 删除tools.base import、`create_tool_executor()`；raw extra bindings改为typed installations；close按candidate registry→MCP handoff→Supervisor顺序drain。 |
| `src/pulsara_agent/runtime/tool_loop.py` | 从ports和runtime executor import。 |
| `src/pulsara_agent/runtime/tool_artifacts.py` | 实现两个artifact ports；删除tools.base import；移出in-memory index。 |
| `src/pulsara_agent/runtime/tool_execution.py` | prepared result从ports导入；registry成为suspension/terminal stable candidate唯一owner，补齐NONE retry、generic reconcile与physical handoff。 |
| `src/pulsara_agent/runtime/permission.py` | ToolCall从ports；catalog驱动permission；删除PermissionState下传工具。 |
| `src/pulsara_agent/runtime/tool_action.py` | ToolCall从ports；按descriptor contract resolve；删除builtin name switch。 |
| `src/pulsara_agent/runtime/recovery.py` | catalog驱动severity；删除taxonomy sets。 |
| `src/pulsara_agent/runtime/terminal/models.py` | value types迁往ports后删除或仅保留runtime-internal state。 |
| `src/pulsara_agent/primitives/terminal_observation.py` | 抽取唯一`TerminalMonitorLifecycleState` alias，durable schema合法值不变。 |
| `src/pulsara_agent/runtime/terminal/monitor.py` | prepared carrier从ports导入；runtime state消费同一lifecycle alias。 |
| `src/pulsara_agent/runtime/terminal/notification.py` | reservation/capacity contract从ports导入。 |
| `src/pulsara_agent/runtime/mcp/manager.py` | 只保留production manager protocol。 |
| `src/pulsara_agent/runtime/mcp/supervisor.py` | pending lease只由execution port private handle registry访问；不再向Agent暴露reservation/borrow/complete。 |
| `src/pulsara_agent/runtime/mcp/types.py` | installed snapshot以typed binding installations取代raw `tuple[object]` tools，并校验identity set。 |
| `src/pulsara_agent/runtime/mcp/__init__.py` | 删除Mock export；installation/tool port按需要direct export，不使用lazy。 |
| `src/pulsara_agent/runtime/subagent/runtime.py` | 实现command port需要的domain operation；不依赖tools。 |
| `src/pulsara_agent/runtime/subagent/types.py` | D4-0删除closed alias定义并exact re-export`primitives.subagent`；D4-3迁完consumer后删除临时re-export。 |
| `src/pulsara_agent/runtime/subagent/execution.py` | MCP child reverse index只消费registry的`McpToolBindingContract`，删除tool attribute introspection。 |
| `src/pulsara_agent/runtime/projection_jobs/coverage.py` | contracts import改为top-level。 |
| `src/pulsara_agent/runtime/projection_jobs/inspection.py` | contracts import改为top-level。 |
| `src/pulsara_agent/runtime/projection_jobs/migration_transform.py` | 通过migration transaction capability实现transform port；canonical repository改为新SQL owner。 |
| `src/pulsara_agent/runtime/projection_jobs/mutation_writer.py` | 实现sealed transaction driver/commit行为与process writer port；pure factory迁出。 |
| `src/pulsara_agent/runtime/projection_jobs/postgres_repository.py` | contracts/ports改为低层owner；canonical repository import改新路径。 |
| `src/pulsara_agent/runtime/projection_jobs/pre_activation.py` | 实现完整migration preparation port；contracts/migration state改为低层owner。 |
| `src/pulsara_agent/runtime/projection_jobs/projection_handlers.py` | contracts与graph lowering改为新owner。 |
| `src/pulsara_agent/runtime/projection_jobs/registry.py` | contracts import改为top-level。 |
| `src/pulsara_agent/runtime/projection_jobs/repository.py` | repository protocols改为ports，facts改为top-level。 |
| `src/pulsara_agent/runtime/projection_jobs/result.py` | result factories依赖top-level contracts。 |
| `src/pulsara_agent/runtime/projection_jobs/seeder.py` | seed commit port/facts改为低层owner。 |
| `src/pulsara_agent/runtime/projection_jobs/service.py` | 组合worker/ports；仍是唯一process service owner。 |
| `src/pulsara_agent/runtime/projection_jobs/source.py` | source reader port/facts改为低层owner。 |
| `src/pulsara_agent/runtime/projection_jobs/surface.py` | surface facts/commit port改为低层owner。 |
| `src/pulsara_agent/runtime/projection_jobs/surface_handlers.py` | contracts与graph relation lowering改为新owner。 |
| `src/pulsara_agent/runtime/projection_jobs/worker.py` | handler/repository protocols改为ports。 |

### 13.3 修改 production 文件：capability/schema/composition

| 文件 | 修改 |
|---|---|
| `src/pulsara_agent/capability/builtin_provider.py` | 从catalog投影descriptor；删除runtime.tool_action import。 |
| `src/pulsara_agent/capability/descriptor.py` | `artifact_mode`改用低层`ToolArtifactMode`；input schema/metadata递归冻结，event payload值不变。 |
| `src/pulsara_agent/capability/call_classifier.py` | default category/read-only只读descriptor；action override/terminal rule只读catalog contract。 |
| `src/pulsara_agent/capability/providers/mcp.py` | 删除installation/supervisor/tool binding composition，只保留pure provider projection。 |
| `src/pulsara_agent/capability/runtime.py` | 接收ToolRegistryReadPort，不TYPE_CHECK concrete tools。 |
| `src/pulsara_agent/capability/result_contracts.py` | result contract成为catalog entry字段，不再保留第二个name switch。 |
| `src/pulsara_agent/capability/result_semantics.py` | runtime-input protocol/factory迁出，只保留builder registry；ToolCall/result从ports导入。 |
| `src/pulsara_agent/message/__init__.py` | schema-only facade。 |
| `src/pulsara_agent/event/candidates.py` | 引用primitive candidate payload；删除反向owner。 |
| `src/pulsara_agent/event/events.py` | 不导入process-local ToolResult receipt；event schema/fingerprint不变。 |
| `src/pulsara_agent/primitives/governance_evidence.py` | CandidatePayload改从primitives导入。 |
| `src/pulsara_agent/primitives/runtime_event_vocabulary.py` | 删除ToolResultBlock依赖；process-local receipt迁到replay。 |
| `src/pulsara_agent/event_log/in_memory.py` | reducer改从replay导入。 |
| `src/pulsara_agent/event_log/postgres.py` | reducer改从replay导入。 |
| `src/pulsara_agent/inspector/service.py` | reducer改从replay direct import。 |
| `src/pulsara_agent/runtime/context_input/transcript.py` | assembler/reducer改从replay import。 |
| `src/pulsara_agent/runtime/transcript.py` | reducer改从replay import。 |
| `src/pulsara_agent/runtime/hooks.py` | assembler改从replay import。 |
| `src/pulsara_agent/runtime/compaction/service.py` | assembler/reducer改从replay import。 |
| `src/pulsara_agent/runtime/subagent/runtime.py` | assembler/reducer改从replay import；command port adapter调用其domain API。 |
| `src/pulsara_agent/graph/postgres.py` | relation lowering改从graph owner导入；projection DTO改从top-level contracts。 |
| `src/pulsara_agent/graph/durable_facade.py` | constructor接收process-scoped`CanonicalMutationWriterPort`，删除concrete runtime writer import。 |
| `src/pulsara_agent/memory/canonical/unit_of_work.py` | constructor接收`MemoryUowTransactionScopeFactory`；从scope repository bundle逐项绑定现有属性；不持有connection或构造repository；删除concrete writer与production fake UOW。 |
| `src/pulsara_agent/memory/governance/executor.py` | projection facts改从top-level contracts导入。 |
| `src/pulsara_agent/storage/session_bootstrap.py` | projection facts/port改从低层owner导入。 |
| `src/pulsara_agent/storage/runtime_write_admission.py` | admission DTO/port改从低层owner导入。 |
| `src/pulsara_agent/storage/postgres_connection_provider.py` | normal admission返回guard handle；新增只向exact scope-factory authority开放的MEMORY_UOW physical transaction capability。 |
| `src/pulsara_agent/storage/migrations/runner.py` | 接收`ProjectionMigrationPreparationPort`并签发migration transaction capability；删除全部runtime local import。 |
| `src/pulsara_agent/runtime/wiring.py` | 删除in-memory factory/import/branch；agent wiring always durable；构造tool ports与UOW scope factory。 |
| `src/pulsara_agent/host/core.py` | 删除durable field/branches及分散resource builders；中央factory分别构造Host build fact/live bindings/admission。 |
| `src/pulsara_agent/host/session_manifest.py` | `SessionManifestStore`实现typed Host manifest port；完整保留workspace/domain/include-closed/limit查询，SQL行为不变。 |
| `src/pulsara_agent/host/session.py` | MCP binding replacement读取`McpToolBindingContract` exact identity，不import/inspect concrete MCP tool。 |
| `src/pulsara_agent/cli.py` | direct module imports；HostCore.production；projection prepare由composition提供port；inspect使用runtime tool composition。 |
| `src/pulsara_agent/runtime/__init__.py` | 删除lazy facade，V1空facade。 |
| `src/pulsara_agent/tools/__init__.py` | 删除lazy facade，只保留最小eager export。 |

### 13.4 删除 production 文件/符号

| 对象 | 动作 |
|---|---|
| `src/pulsara_agent/tools/base.py` | 删除。 |
| `src/pulsara_agent/tools/executor.py` | 删除。 |
| `src/pulsara_agent/message/assembler.py` | 删除。 |
| `src/pulsara_agent/message/reducer.py` | 删除。 |
| `src/pulsara_agent/runtime/projection_jobs/contracts.py` | 迁移完成后删除旧路径。 |
| `src/pulsara_agent/runtime/projection_jobs/canonical_mutation.py` | SQL repository迁到`postgres_canonical_mutation_repository.py`且pure helper迁出后删除。 |
| `src/pulsara_agent/runtime/projection_jobs/migration_state.py` | 迁移完成后删除旧路径。 |
| `src/pulsara_agent/runtime/projection_jobs/graph_relation.py` | lowering归graph后删除。 |
| `src/pulsara_agent/runtime/tool_taxonomy.py` | 删除。 |
| `src/pulsara_agent/runtime/terminal_risk.py` | pure classifier迁到capability owner后删除旧路径。 |
| `ToolRegistry.tool_specs()` | 删除；provider schema只从capability descriptor lowering。 |
| concrete tool的`description`/`parameters`/read-only/concurrency flags | 删除；builtin catalog或dynamic descriptor是唯一owner。 |
| `build_in_memory_runtime_wiring` | production定义与export删除。 |
| `InMemoryMemoryWriteUnitOfWork` | production定义删除。 |
| `_PoolDecisionRepository`、`_NoopOutboxRepository` | 从production UOW移到test-support并改名为显式fake。 |
| `InMemoryToolResultArtifactIndex` | production定义删除。 |
| `MockMcpClientManager` | production定义/export删除。 |
| `HostCore.durable` | 字段删除。 |
| `build_agent_runtime_wiring.durable` | 参数删除。 |
| runtime/tools `_LAZY_EXPORTS`、`__getattr__` | 删除。 |

### 13.5 新增/修改 test support

| 文件 | 修改 |
|---|---|
| `tests/support/runtime_factory.py` | component runtime/agent wiring。 |
| `tests/support/host.py` | component Host composition factory。 |
| `tests/support/mcp.py` | MockMcpClientManager。 |
| `tests/support/artifacts.py` | InMemoryToolResultArtifactIndex。 |
| `tests/support/memory_uow.py` | 独立Fake UOW，不继承production fake。 |
| `tests/support/runtime_session.py` | 使用test artifact index与ports contract。 |
| `tests/support/capability.py` | typed ToolRuntimeContext/permission snapshot factory。 |
| `tests/support/dependency_rules.py` | canonical AST import observation factory、attribution与派生package/SCC scanner。 |
| `tests/support/d4_target_edge_cutover_ledger.py` | D4-0至D4-4按exact import observation ID/fingerprint的短期migration ledger；D4-5删除。 |
| `tests/support/d4_type_ownership_cutover_ledger.py` | symbol-kind-aware old/final identity或alias shape与删除phase清单；仅D4-0至D4-4存在，D4-5删除。 |
| `tests/test_dependency_architecture.py` | module observation growth、package/import/fake/facade hard gates。 |
| `tests/test_d4_port_contracts.py` | D4-0全部closed request/outcome/scope/Host carrier的constructor、validator、fingerprint与serialization gate。 |
| `tests/test_d4_type_owner_identity.py` | class-like identity/module/qualname与alias-like AST/shape分支、factory return type及无shadow definition gate。 |
| `tests/test_package_facade.py` | minimal eager facade与direct import smoke。 |
| `tests/test_projection_job_transaction_capability.py` | mutation driver same-connection、guard/generation与port release hard gate。 |
| `tests/test_memory_uow_transaction_scope.py` | 六个facade同lease/connection、scope-owned revoke/drain/commit/rollback及六类retained reference fail-closed。 |
| `tests/test_projection_migration_port.py` | runner/CLI injection、sealed transaction、readiness/transform/resource contract。 |
| `tests/test_tool_artifact_processing_policy.py` | descriptor-to-policy lowering、no fallback、preview/archive golden。 |
| `tests/test_tool_binding_contracts.py` | origin union、MCP exact identity、legacy/new fingerprint split与registry join。 |
| `tests/test_mcp_tool_execution_port.py` | success/application-error/suspended/rejected、raw payload handle ownership、registry receipt/handoff settlement、active-borrow finally与metadata deep freeze。 |
| `tests/test_tool_execution_stable_candidate_owner.py` | initial/successor suspension与terminal candidate ownership、NONE policy、UNKNOWN/PARTIAL、physical handoff和close drain。 |
| `tests/test_terminal_tool_ports.py` | 三个port全branch、prepared owner exact join、kill completion receipt与expected rejection。 |
| `tests/test_subagent_tool_port.py` | 九种command parser、action-specific outcome、child owner与batch failure mapping。 |
| `tests/test_host_composition_contract.py` | semantic/live split、binding rebind identity、manifest四维查询与serialization rejection。 |

### 13.6 必须迁移的测试模块

以下模块不得继续从 `pulsara_agent.runtime` / `pulsara_agent.tools` convenience facade或production fake入口导入：

- `tests/test_action_boundary_trigger.py`；
- `tests/test_agent_runtime_loop.py`；
- `tests/test_capability_mcp.py`；
- `tests/test_capability_surface.py`；
- `tests/test_cli_host.py`；
- `tests/test_context_candidates.py`；
- `tests/test_context_input_facts.py`；
- `tests/test_context_input_manifest.py`；
- `tests/test_context_snapshot_builder.py`；
- `tests/test_context_compaction.py`；
- `tests/test_durable_memory_producer.py`；
- `tests/test_durable_projection_host_dogfood.py`；
- `tests/test_event_log_contract.py`；
- `tests/test_host_core.py`；
- `tests/test_host_lifecycle_contract.py`；
- `tests/test_host_resume.py`；
- `tests/test_host_retrieval_lifecycle.py`；
- `tests/test_inspector.py`；
- `tests/test_long_horizon_status_candidate.py`；
- `tests/test_long_horizon_tool_action.py`；
- `tests/test_long_horizon_window_rollout.py`；
- `tests/test_mcp_host_lifecycle.py`；
- `tests/test_mcp_sdk_discovery.py`；
- `tests/test_memory_explain.py`；
- `tests/test_memory_graph_recall.py`；
- `tests/test_memory_governance_engine.py`；
- `tests/test_memory_reflection.py`；
- `tests/test_permission_policy.py`；
- `tests/test_plan_workflow.py`；
- `tests/test_provider_input_hard_cut.py`；
- `tests/test_recall_v1.py`；
- `tests/test_retrieval_runtime.py`；
- `tests/test_runtime_committed_writer.py`；
- `tests/test_runtime_event_architecture.py`；
- `tests/test_runtime_hooks.py`；
- `tests/test_runtime_observation_prefix_continuity.py`；
- `tests/test_runtime_publication_maintenance.py`；
- `tests/test_runtime_publisher.py`；
- `tests/test_runtime_session.py`；
- `tests/test_runtime_timeline.py`；
- `tests/test_runtime_wiring.py`；
- `tests/test_subagent_execution_registry.py`；
- `tests/test_subagent_postgres_integration.py`；
- `tests/test_subagent_runtime.py`；
- `tests/test_terminal_monitor_tm1_tm5.py`；
- `tests/test_terminal_public_api_hard_cut.py`；
- `tests/test_terminal_runtime.py`；
- `tests/test_tools.py`；
- `benchmarks/durable-runtime/generators/provider_input_prefix.py`；
- `benchmarks/suites/runner.py`。

机械 import修改不允许顺带更改测试断言。依赖 fake composition 的测试改用 `tests.support.runtime_factory` / `tests.support.host`；durable integration继续使用 migrated PostgreSQL fixture。

---

## 14. 验证矩阵

### 14.1 静态 grep必须为零

```bash
! rg -n 'from pulsara_agent\.runtime|import pulsara_agent\.runtime' \
  src/pulsara_agent/tools

! rg -n 'RuntimeSession|PermissionState|TerminalSessionManager|McpServerSupervisor|SubagentRuntime' \
  src/pulsara_agent/tools

! rg -n 'build_in_memory_runtime_wiring|InMemoryMemoryWriteUnitOfWork|MockMcpClientManager' \
  src/pulsara_agent

! rg -n 'durable:\s*bool|if (not )?self\.durable|durable=self\.durable' \
  src/pulsara_agent/host/core.py src/pulsara_agent/runtime/wiring.py

! rg -n '_LAZY_EXPORTS|def __getattr__' \
  src/pulsara_agent/runtime/__init__.py src/pulsara_agent/tools/__init__.py

! rg -n 'message\.(assembler|reducer)' src/pulsara_agent

! rg -n 'from pulsara_agent\.event' src/pulsara_agent/primitives

! rg -n 'runtime\.projection_jobs' \
  src/pulsara_agent/storage src/pulsara_agent/graph src/pulsara_agent/memory

! rg -n 'VerifiedPostgresTransactionHandle|CanonicalMutationV2Writer\(.*connection' \
  src/pulsara_agent/memory src/pulsara_agent/graph

! rg -n 'def revoke\(' src/pulsara_agent/ports/projection_jobs.py

! rg -n 'HostRuntimeBuildRequest' src/pulsara_agent

! rg -n 'runtime\.projection_jobs\.(canonical_mutation|pre_activation|migration_transform)' \
  src/pulsara_agent/storage

! rg -n 'getattr\([^\n]*binding_identity|hasattr\([^\n]*binding_identity|isinstance\([^\n]*McpCapabilityTool' \
  src/pulsara_agent/runtime src/pulsara_agent/host

! rg -n 'descriptor\s*=\s*None|descriptor:\s*CapabilityDescriptor' \
  src/pulsara_agent/runtime/tool_artifacts.py

! rg -n 'event\.tool_result_receipts|CurrentToolResult(BatchReceipt|ReceiptItem)' \
  src/pulsara_agent/event

! rg -n 'from tests|import tests' src/pulsara_agent
```

这里使用shell `!` 将“无匹配”的`rg`退出码1转换为gate成功；任意命中都会使该行失败。
architecture test负责跨平台执行，不以手工grep替代CI。

### 14.2 contract golden

必须冻结 D4 前后：

- ordered builtin descriptor fingerprints；
- ordered tool binding fingerprints；
- ordered MCP binding exact identity/fingerprint pairs；
- tool JSON Schema fingerprints；
- result render contract fingerprints；
- resolved artifact processing policy fingerprints；
- action classifier contract fingerprints；
- read-only allowed names；
- descriptor default permission/read-only与action override matrix；
- recovery severity matrix；
- main/child subagent tool sets；
- terminal family names；
- residual-SCC canonical AST import observation IDs/fingerprints与derived package-edge/SCC accumulator；
- terminal command/process/monitor request-to-outcome branch matrix；
- terminal monitor lifecycle alias vector与prepared-carrier single-owner matrix；
- MCP success与application-error completed metadata/result-state mapping；
- MCP pending handle suspension/terminal FULL/NONE/UNKNOWN/PARTIAL transition matrix；
- tool stable candidate kind/NONE policy/commit receipt/physical handoff matrix；
- subagent九种command与action-specific outcome matrix；
- subagent closed vocabulary ordered values；
- memory UOW六种facade kind、shared lease transition与retained-reference failure code matrix；
- Host build fact semantic fingerprint与ordered process-local live binding identities（两者分开比较）。

golden变化只能由独立行为规格授权。D4本身没有这种授权。

### 14.3 import smoke

至少覆盖不同顺序：

```python
import pulsara_agent.runtime.session
import pulsara_agent.tools.builtins.terminal
import pulsara_agent.capability.runtime

# 反向顺序
import pulsara_agent.capability.runtime
import pulsara_agent.tools.registry
import pulsara_agent.runtime.agent
```

不得依赖某个模块先被import才成功。

### 14.4 component与integration边界

- 普通unit/component tests：无PostgreSQL、无Oxigraph、无API key；
- durable tests：显式 `postgres` fixture + migrated schema；
- HostCore production tests：verified Postgres composition；
- Host lifecycle纯控制流 tests：`tests.support.host`；
- fake测试结果不得被命名为 durable proof。

---

## 15. 失败与回滚规则

### 15.1 不允许的中间态

- 新 ToolExecutor已在runtime，但runtime仍从tools facade导入旧executor；
- terminal tool同时接受port和manager；
- registry同时接受RuntimeSession和composition input；
- capability catalog与旧name sets同时作为authority；
- production wiring同时保留durable flag与test factory；
- `MockMcpClientManager`同时存在production和tests/support；
- 新 facade direct import与旧lazy router共存。

### 15.2 PR失败回滚

每个D4阶段是source atomic commit。若gate失败，回滚该阶段整个commit，不得保留兼容shim让后续阶段“再清理”。

### 15.3 deploy

D4-4 后需要process restart：

1. 停止新Host open/run admission；
2. drain active model/tool/terminalization owner；
3. close HostCore；
4. 部署新binary；
5. 新binary执行schema verify-only；
6. reopen durable sessions。

因为event/schema不变，不需要database reset或migration。

---

## 16. 长期契约更新

D4-4 必须同步：

- `contracts/PACKAGE_FACADE_CONTRACT.zh.md`：删除lazy facade要求，冻结direct import；
- `contracts/BUILTIN_TOOLS_CONTRACT.zh.md`：catalog与port-owned binding；
- `contracts/CAPABILITY_SURFACE_CONTRACT.zh.md`：descriptor/catalog/registry exact join、
  origin-aware binding union与execution-surface artifact policy；
- `contracts/PERMISSION_POLICY_CONTRACT.zh.md`：descriptor独占default category/read-only，catalog
  只拥有action override与terminal rule；
- `contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md`：ToolExecutor由runtime拥有，tool composition不再由
  RuntimeSession拥有；stable suspension/terminal candidate由registry唯一拥有，candidate commit与
  physical owner handoff无owner gap；
- `contracts/MCP_CAPABILITY_CONTRACT.zh.md`：MCP tool只持有execution port，不持有supervisor；
  registry binding正式携带exact server/slot/snapshot/generation identity；completed outcome无损保留
  application-error output/artifact/metadata；port-owned pending handle保存raw resume state，durable
  registry receipt决定lease confirm/abort/complete，active borrow在manager call的finally归还；
- `contracts/WORKSPACE_TERMINAL_LIFECYCLE_CONTRACT.zh.md`：三个terminal port冻结完整action/outcome、
  prepared owner与owner scope；monitor lifecycle closed alias与prepared carrier各自只有一个owner；
- `contracts/HOST_RESUME_CONTRACT.zh.md`：production Host always durable；build semantic/live carrier
  分离且manifest查询保留workspace/domain/closed/limit维度；
- `contracts/APP_SETTINGS_CLI_ENTRY_CONTRACT.zh.md`：无non-durable Host入口；
- `contracts/RUNTIME_SEMANTIC_GRAPH_CONTRACT.zh.md`：projection contracts/ports归属改变，D3行为不变；
- `contracts/MEMORY_SURFACES_CONTRACT.zh.md`：memory UOW依赖borrower-scoped commit port，graph依赖
  process writer port；UOW公开repository是共享revocable scope lease下的六个facade，scope外不可使用；
- `contracts/GOVERNANCE_WRITE_OUTBOX_CONTRACT.zh.md`：UOW通过sealed same-physical-transaction
  capability append canonical mutation，不持有concrete writer；
- `contracts/GRAPH_JSONLD_STORAGE_CONTRACT.zh.md`：immutable relation lowering由graph owner持有；
- `contracts/POSTGRES_SCHEMA_MIGRATION_CONTRACT.zh.md`：runner通过sealed migration transaction与
  完整preparation port接入D3 migration，禁止runtime local import；
- `contracts/RUNTIME_EVENT_PUBLISHING_HOOKS_CONTRACT.zh.md`：projection service module ownership变化但O(1) wake语义不变；
- `PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md`：最终将D4标记CLOSED，并保留D5/D6 OPEN。

`contracts/README.zh.md` 增加 dependency/ports contract索引；必须新增：

- `contracts/PACKAGE_DEPENDENCY_AND_PORTS_CONTRACT.zh.md`：同时冻结D4-0最终类型owner、临时exact
  re-export规则与D4-5零shim终态。

长期契约与production切换必须同PR，不能留到D4-5才补写。

---

## 16.1 最终验证记录

2026-07-26 的 D4-5 审计记录如下：

| Gate | 结果 |
|---|---|
| D4-0 至 D4-4 阶段门控 | 全部通过；最终 D4 专项集合 `65 passed` |
| 全量 pytest | 首轮 `2504 passed, 27 failed, 2 skipped`；27 项均为旧 test-support/composition fixture，迁移后 last-failed 集合 `26 passed, 1 failed`，最后单项 `1 passed`。遵循用户要求不重复运行 28 分钟全量套件。 |
| PostgreSQL durable Host | migrated PostgreSQL Host backlog/restart、open/resume/close 失败节点定向复跑通过 |
| Core dogfood manifest | `PASS suite=pulsara-core-dogfood-v1 scenarios=6`，suite fingerprint `93a1dbbe1708f1a1adcd8ae0bb3c1fe5d401600c79baf6fc636a15428a33fcb4` |
| Real provider dogfood | `workspace-patch`、`subagent-delegation`、`durable-resume` 均在 current runner 下通过；首次暴露并修复 runner 将 `limit_events=0` 误解释为 timeline page size 1 的证据投影问题 |
| Static architecture | target DAG、temporary owner ledger、lazy facade、`src -> tests`、`tools -> runtime` grep/AST 全绿；正向与反向 import-order smoke 通过 |
| Formatting/lint | changed/untracked Python `219 files already formatted`；ruff lint 与 `git diff --check` 通过 |
| Residual SCC | post-review hard cut 后 canonical observation count `393`，fingerprint `sha256:ec2213041bae7005c00d035ae5ebbe84258e1063580799c8c21ecf5d1fe9349b`；删除了 `runtime.permission -> capability.builtin_catalog` 的直接反向 observation，permission 仅消费 classifier 的 catalog-derived typed classification；仅作为 D5/D6 diagnostic baseline，不宣称全仓库 SCC 已消除 |
| Post-review ownership faults | MCP/Host close、terminal capacity compensation、artifact terminalization、catalog taxonomy、subagent commit cancellation与typed terminal owner专项门控：`90 passed` + `155 passed`；移除最后一组permission category strings后定向复跑`141 passed`；compileall通过 |
| MCP post-return lowering | bad terminal metadata、malformed successor与正常successor atomic replacement：`7 passed`；D4 port/type、MCP Host lifecycle及Agent resume companion gates：`30 passed`；两个failure probe的physical resume count均固定为1 |

全量 pytest 首轮之后仅复跑失败节点，是本任务显式要求的验证策略；所有首轮通过节点保持不变，所有首轮失败节点均取得修复后的通过证据。

## 17. Definition of Done

D4 只有同时满足以下全部条件才可标记完成。

### 17.1 Dependency

- [x] 普通pytest执行AST dependency rule；
- [x] D4 target DAG五组forbidden edge无exception；
- [x] 临时target-edge cutover ledger及loader已删除；
- [x] 临时type-ownership cutover ledger与old-path re-export已删除；D4-0起所有symbol满足
  `old.X is final.X`；class/enum/protocol通过module/qualname gate，assignment/union/PEP 695 alias通过
  唯一AST owner与canonical shape gate，不把typing alias误当class；
- [x] runtime/tools concrete双向依赖中`tools -> runtime`方向归零；
- [x] tools -> runtime所有import归零，包括TYPE_CHECKING/local import；
- [x] event schema不依赖replay/reducer；
- [x] primitives不依赖event candidates；
- [x] capability provider不依赖runtime/tools concrete implementation；
- [x] storage/graph/memory不依赖`runtime.projection_jobs`；
- [x] D3 durable fact registry前后ordered schema vectors一致；
- [x] src不依赖tests/support；
- [x] global package SCC diagnostic baseline未增长，并保存remaining D5/D6 edge报告；
- [x] residual-SCC canonical AST import observation set未增长；同一SCC package pair下新增module
  import也会失败，合法acyclic import不被误报；
- [x] D4完成声明不包含“全仓库跨package SCC已消除”。

### 17.2 D3 lower-layer transaction与migration

- [x] canonical mutation pure factory、PostgreSQL repository、writer/capability已三文件分离；
- [x] memory UOW只通过owner-scoped transaction scope取得六个scoped facade；UOW与公开bundle均不持有
  raw connection或concrete connection-bound repository；
- [x] graph、decision、event outbox、mutation outbox、lifecycle与write service逐项证明绑定同一scope
  lease与physical transaction，所有public operation重验ACTIVE owner/generation；
- [x] scope先revoke/drain全部facade与mutation port，再commit/rollback并归还connection；分别保留六种
  facade后调用均在SQL前fail closed，且commit port没有public revoke；
- [x] wrong borrower、owner、generation、backend PID、driver authority或admission guard均拒绝；
- [x] commit port没有generic SQL/cursor/connection escape hatch；
- [x] storage/graph/memory不import concrete projection implementation；
- [x] migration runner/CLI只调用注入的完整`ProjectionMigrationPreparationPort`；
- [x] runner无runtime projection local import；D3 SQL/result receipt/schema fingerprints不变。

### 17.3 Tool ownership

- [x] ToolExecutor由runtime唯一拥有；
- [x] RuntimeSession不再构造ToolExecutor；
- [x] built-in constructor不接收RuntimeSession；
- [x] artifact/terminal/MCP/subagent使用closed ports；
- [x] permission invocation使用typed run snapshot；
- [x] concrete tool不拥有description/input schema/read-only/concurrency第二真源；
- [x] MCP capability tool不持有supervisor；
- [x] MCP port-owned pending handle唯一持有raw request/state与pending lease；Agent不回读/complete
  Supervisor lease；suspension和terminal batch的FULL/NONE/UNKNOWN/PARTIAL矩阵全部回归；
- [x] `ToolExecutionTerminalRegistry`是suspension/terminal stable `AgentEvent` tuple唯一owner；successor
  suspension与terminal NONE由registry重试，MCP handle/settlement/scratchpad只保存owner identity；
- [x] registry FULL/initial-NONE/UNKNOWN receipt先exact join MCP physical handle，port返回handoff receipt后
  registry才清除candidate；缺任一owner时Host close blocked；
- [x] resume active borrow总在manager physical operation的`finally`归还，不等待EventLog write或
  publication；durable confirmation只控制长期pending lease；
- [x] execute/pending-promotion/resume cancellation分别释放ordinary lease、abort未确认promotion、归还
  active borrow并恢复`PENDING_CONFIRMED`；Host close在Supervisor前drain execution port handle registry；
- [x] provider resume返回后先不可逆进入`RESUME_RESULT_RECEIVED`；terminal outcome由原handle缓存为
  `TERMINAL_RESULT_FROZEN`，successor prepared/handle/outcome先完整冻结再原子替换predecessor；lowering
  failure生成non-retryable typed protocol result或latch reconciliation，重复resume不再调用manager；
- [x] registry返回origin-aware binding union，MCP exact identity不再来自concrete attribute/getattr；
- [x] artifact processing只消费execution-surface frozen policy，不接收descriptor或default fallback；
- [x] artifact archive/index runtime failure不会遗留孤立Start，也不重执行physical tool；它生成唯一稳定
  error terminal candidate，未确认artifact refs不进入结果；
- [x] terminal tool不持有manager/account/coordinator concrete owner；
- [x] terminal三个port穷尽command、8种process action、3种monitor action；prepared reservation、
  registration、cancellation与kill completion receipt均exact join；
- [x] `TerminalRequest`无自由metadata live-owner通道；typed execution owner独立注入。Completion
  reservation拒绝会在返回前同步kill/join/retire process并生成typed capacity rejection；
- [x] terminal monitor registered/cancelled outcome不复制prepared carrier字段，inventory lifecycle使用
  durable core同一closed alias；
- [x] subagent tool不持有SubagentRuntime；
- [x] subagent port穷尽9种request DTO与action-specific success/not-ready/rejected payload，无generic dict outcome；
- [x] subagent spawn与batch commit cancellation按NONE/FULL/UNKNOWN结算capacity：release、安装committed
  child handle或保留reconciliation owner；close拒绝遗漏的unknown reservation；
- [x] subagent role/context/status/profile/edge/command vocabulary由低层primitive唯一拥有，ports/runtime
  不使用自由`str`或不同inline closed set；phase/display role等开放文本除外；
- [x] MCP completed outcome保留result state与递归冻结metadata；`is_error=True`不被降成rejected。

### 17.4 Catalog

- [x] builtin descriptor、binding、permission、recovery、action taxonomy只有一个catalog；
- [x] `BuiltinToolBindingKind`、availability requirement与composition matrix穷尽全部builtin；
- [x] descriptor是default permission category/read-only唯一owner；permission contract无重复default字段；
- [x] terminal process/monitor action-level permission与terminal scheduling rule来自同一catalog；
- [x] permission与Long-Horizon classifier不再保存独立terminal observe action/name set，只消费exact
  catalog entry/override/family/binding contract；
- [x] recovery使用catalog name-level severity，不存在无authority的action override；
- [x] descriptor/registry/catalog set-equality在composition root强校验；
- [x] D4前后所有tool/capability fingerprints不变；
- [x] `runtime/tool_taxonomy.py`删除。

### 17.5 Schema/replay ownership

- [x] current ToolResult receipt位于`replay/tool_result_receipts.py`且仍为process-local；
- [x] event schema不引用/注册该receipt，event schema vector无变化；
- [x] artifact view、terminal/MCP/subagent request/outcome与Host composition carriers均有closed字段和validator；
- [x] Host build semantic fact不含live object；每个live binding具有exact rebinding identity且不进入semantic fingerprint；
- [x] manifest port完整保留workspace root、memory domain、include closed与limit四维查询；
- [x] 不存在只有名字、`def execute(...): ...`式省略参数/return branch，或以
  `dict[str, object]`代替closed carrier的D4中央契约；Protocol method body的标准`...`不属于占位。

### 17.6 Test support

- [x] MockMcpClientManager只在tests/support；
- [x] whole in-memory runtime factory只在tests/support；
- [x] fake governance UOW只在tests/support；
- [x] production HostCore没有durable字段/分支；
- [x] production wiring没有durable参数或in-memory factory；
- [x] unit/component tests无需PostgreSQL；
- [x] durable integration使用migrated PostgreSQL fixture。

### 17.7 Facade

- [x] runtime/tools无`_LAZY_EXPORTS`、`__getattr__`；
- [x] production与tests改用direct owning-module imports；
- [x] package facade contract已更新；
- [x] 任意导入顺序smoke通过。

### 17.8 Verification

- [x] D4-0至D4-5每阶段gate有记录且全绿；
- [x] `uv run pytest -q`全绿；
- [x] PostgreSQL durable Host open/resume/close integration全绿；
- [x] core dogfood manifest validate通过；
- [x] `durable-resume`、`subagent-delegation`、`workspace-patch`真实dogfood通过；
- [x] static grep/AST审计无旧symbol；
- [x] D4 debt状态仅在上述全部完成后改为CLOSED。

---

## 18. 最终裁决

D4 的完成形态不是“import cycle少了一点”，而是五个可验证事实：

```text
D4 target DAG五组反向边归零；全局SCC只保留冻结的D5/D6 diagnostic
Concrete tools只知道ports
Runtime唯一拥有execution orchestration
Production Host只有durable composition
Tests通过tests/support显式借用fake world
```

这一步完成后，D6 才能在不继续传播 `RuntimeSession` service-locator 与循环import的前提下拆分 AgentRuntime/HostSession owner；D5 也能通过明确port接入compaction-memory extension，而不再把memory实现反向导入runtime核心。

在此之前，不应开始 D6 coordinator拆分。否则只会把当前依赖环分散到更多文件。
