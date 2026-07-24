# Pulsara 运行时架构债务重基线审计

> 审计日期：2026-07-22
> 代码基线：`main@dca11e75a150489a6fe167a39cd92189ca51b84d`
> 原始来源：`ARCHITECTURE_DEBT_AUDIT.zh.md` 第 14 节依赖表的后半部分
> 范围：从 `Async LiveRuntimeEventWriter` 到 `Compaction-memory extension`

## 1. 目的

原债务审计写于多轮 durable ownership hard cut 之前。此后 Pulsara 已完成 session-owned event writer、materialization account、governance event outbox、provider-input generation、ContextSource、terminal monitor 等大规模改造。

因此，依赖表后半部分不能再被当作一张未经复核的待办清单。本审计逐项回答：

1. 原债务描述的风险是否仍存在；
2. 当前生产代码是否已经通过另一种设计闭环；
3. 若仍有债务，剩余边界究竟是什么；
4. 哪些工作是 correctness hard cut，哪些只是需要数据证明的性能优化；
5. 当前合理的实施顺序和完成门槛是什么。

本审计只评价当前代码真值，不因为类名、旧文档标题或文件体积就认定债务仍然存在。

## 2. 状态定义

| 状态 | 含义 |
|---|---|
| `CLOSED` | 原问题已经在生产路径闭环，不应再次立项 |
| `SUPERSEDED` | 原问题真实存在过，但已被另一种更合适的设计替代 |
| `PARTIAL` | 主体闭环，仍有可独立描述的窄尾巴 |
| `OPEN` | 当前生产路径仍保留原风险或 ownership 方向错误 |
| `PERF-GATED` | correctness 已成立，是否继续重构只能由性能数据决定 |

## 3. 总结结论

| 原工作项 | 当前状态 | 当前结论 | 建议优先级 |
|---|---|---|---|
| Async LiveRuntimeEventWriter | `CLOSED` + `PERF-GATED` | session-owned writer correctness与compaction唯一写入/发布边界已闭环；原生 async PostgreSQL只由 profiling决定 | 无 correctness hard cut |
| Governance events 同 UOW | `SUPERSEDED` | 已由 memory UOW 内 durable stable-candidate outbox + session-owned accounted dispatcher 闭环，不应改回直接跨 owner 写 ledger | 无新 hard cut |
| CustomEvent typed 化 | `CLOSED` | 7 个 production事实及 MCP closure已 typed；`CustomEvent`/`EventType.CUSTOM`与旧 decoder已物理删除 | 已完成 |
| Hook/outbox 重构 | `OPEN` | hook 失败仍只在内存，且部分 async hook 直接执行同步存储 I/O | 高，依赖 migration runner |
| Runtime dependency-cycle cleanup | `OPEN` | lazy facade、local import 与 runtime/tools 双向依赖仍是生产结构 | 高但需分步 |
| AgentRuntime coordinator 拆分 | `OPEN`，已有基础 | `RunWorkingSet` 与若干 coordinator 已出现，但 production scratchpad 和大范围 orchestration 仍集中 | 中后期 |
| 删除 legacy MCP / in-memory product mode | `PARTIAL` | 手写 MCP transport 已删除；mock MCP 与 `durable=False` 产品分支仍在生产包 | 中 |
| Schema hot-path hard cut | `CLOSED` | migration registry/ledger、verify-only startup与verified connection provider已落地；constructor/UOW DDL及raw-DSN adapter入口已删除 | 已完成 |
| Compaction-memory extension | `OPEN`，correctness 已改善 | event-first candidate projection 已闭环，但 compaction core 仍直接拥有 memory candidate 语义 | 中后期 |

最重要的重基线结论有三个：

1. **不要重建 writer。** 当前 writer 已经具备原审计要求的大部分 correctness 属性。
2. **不要把 governance 改回“memory UOW 直接插入 agent_events”。** durable outbox 是当前 owner 模型下更正确的解法。
3. **Schema hot-path 已按 reset-only V1 完成。** 后续新增hook/outbox table必须继续走同一migration registry，不得恢复constructor/UOW bootstrap。

## 4. 审计方法与证据边界

本次审计使用以下标准：

- 区分 production composition root、offline doctor、component-test adapter 和普通测试 fixture；
- 检查 canonical commit、process-local owner、durable recovery 与 publication 是否由同一条生产路径闭合；
- 检查接口声明是否真的被 composition root 使用，而不是仅存在一个未接线的类；
- 对 import、direct EventLog write、schema SQL、`CustomEvent` 和 scratchpad 做静态搜索；
- 读取已有定向回归测试，确认设计意图是否已有故障窗口测试。

以下内容不单独构成债务：

- pytest 使用 `InMemoryEventLog`；
- diagnostic-only、bounded 且不参与 recovery/projection 的扩展事件；
- 大文件本身；
- 在专用 worker 上使用同步 PostgreSQL 驱动；
- durable outbox 带来的有界、可恢复的暂时不可见；
- compaction 复用同一次模型输出提取 memory candidate 的产品行为。

真正需要收口的是 production 可选择的第二架构、双重事实源、无 durable owner 的失败窗口和不受约束的依赖方向。

## 5. Async LiveRuntimeEventWriter

### 5.1 原债务假设

原审计假设 runtime 仍是：async API 直接执行同步 `EventLog.append/extend`，commit、reducer 和 observer failure 混成一个结果，thread writer 与 async writer 也没有共同的 serialization boundary。

这个假设已经不再成立。

### 5.2 当前已完成的部分

当前生产路径已经具备：

- `runtime/event_write_service.py::RuntimeEventWriteService`
  - session-owned bounded FIFO；
  - async 与 blocking caller 进入同一物理队列；
  - blocking ledger 操作运行在 `critical_ledger_executor()`；
  - caller cancellation 后继续取得真实物理结果；
  - absolute deadline 与 producer/checkpoint admission。
- `runtime/session.py::EventWriteResult`
  - 区分 committed events、reducer high-water、reconciliation 和 publication error；
  - observer failure不再伪装成 ledger commit failure。
- `RuntimeSession.write_events()` / `write_events_from_thread()`
  - 共享相同 writer owner；
  - conditional append、materialization account、committed reducer 与 ordered publisher 在一个 command boundary 中处理。
- `event_log/postgres_pool.py`
  - process-owned bounded connection pool；
  - critical-write reserve 与 bounded-read lane；
  - 不再为每批事件无界创建连接。

换言之，原债务中的 **serialization、commit/reducer/publication 分层、thread/async 共用 owner、pool** 已经落地。

### 5.3 Writer 尾巴已关闭

`DirectEventLogCompactionEventCommitPort`、Host/mid-turn sequence post-scan与二次
publication已经删除。Compaction service required接收 RuntimeSession port，并从 exact
commit receipt形成 attempt result。

Production direct EventLog mutation由 exact AST inventory冻结为四个 owner：

- RuntimeSession event batch commit；
- RuntimeSession projection checkpoint；
- ledger materialization atomic primitive；
- quiescent/offline subagent checkpoint doctor。

两个 LLM recovery test-only direct branches已经迁出生产路径。Alias、bound method、
`getattr`、同名函数或文件级例外均被 architecture test拒绝。因此本节 correctness债务
`CLOSED`；原生 async driver只保留独立 `PERF-GATED` 判断。

### 5.4 原生 async PostgreSQL 是否仍是债务

当前 pool 是同步 `psycopg_pool.ConnectionPool`，不是 `AsyncConnectionPool`。但同步调用已被隔离到专用 bounded worker，不再直接阻塞 Host event loop。

因此：

- “event loop 上直接同步数据库 I/O”已经闭环；
- “必须改成原生 async driver”不再是 correctness 要求；
- 只有 PERF0 证明 executor queue、thread handoff 或 sync pool 明显占据 writer service time 时，才值得改造。

不能仅凭旧工作项名称再次启动一轮 writer 重写。

### 5.5 已满足的完成门槛

1. [x] `ContextCompactionService` production constructor 不再拥有 direct EventLog fallback。
2. [x] 删除 `_publish_compaction_events_after()` 及其 hard-coded event filter。
3. [x] direct `event_log.append/extend` exact AST guard只允许 frozen owner inventory。
4. [ ] 原生 async adapter仅在独立性能提案提供 queue wait、transaction wall time 与
   event-loop responsiveness before/after 后考虑；它不是 correctness未完成项。

结论：**关闭原“Async LiveRuntimeEventWriter”correctness债务；原生 async 化保留为
PERF-GATED。**

## 6. Governance events 同 UOW

### 6.1 当前实现不是原审计要求的直接同表事务

`MemoryWriteUnitOfWork` 当前在同一 PostgreSQL connection/transaction 中提交：

- canonical memory graph mutation；
- governance decision；
- canonical mutation outbox；
- `memory_governance_event_outbox` 的稳定 runtime event candidate batch。

`GovernanceEventOutboxRepository.append_batch()` 冻结：

- exact ordered event payload；
- event IDs；
- governance batch/decision identity；
- payload fingerprint；
- stable outbox ID。

UOW FULL 后，`GovernanceEventOutboxDispatcher` 才通过 `RuntimeSession.write_events_from_thread()` 进入唯一 accounted ledger writer；失败会把 ticket 保留为 pending/failed，并可幂等 retry。

`tests/test_memory_governance.py::test_postgres_governance_event_outbox_retries_after_memory_uow_commit` 已覆盖“memory 已提交、第一次 ledger dispatch 失败、随后精确重试”的原故障窗口。

### 6.2 为什么当前方案比直接插入 agent_events 更合适

直接让 memory UOW 写 `agent_events` 会重新引入另一套 owner，必须在 memory transaction 内复制：

- materialization account CAS；
- physical reservation/charge；
- session writer ordering；
- committed reducer fold；
- ordered publication；
- cancellation/UNKNOWN confirmation。

当前 transactional outbox 将“不可丢失的事件 candidate”与 memory mutation 原子绑定，再把 ledger materialization 交回唯一 RuntimeSession owner。它允许短暂不可见，但不再允许永久 split-brain。

### 6.3 当前判断

原工作项应标记为：

> `SUPERSEDED`：由同 UOW stable-candidate outbox + session-owned accounted dispatcher 替代。

不需要再建立 transaction-aware `agent_events` repository。后续只需保留：

- pending ticket 的 bounded retry/health 指标；
- restart/reopen 后的 dispatch recovery；
- outbox schema 迁移由统一 migration runner 接管；
- memory UOW 与 outbox repository 必须继续共享同一 transaction。

这些是现有实现的运维与迁移责任，不是新的 governance atomicity hard cut。

## 7. Typed event vocabulary（`CLOSED`）

原 7 个 production CustomEvent已经替换为 bounded typed events，并新增
`McpInputRequiredInteractionClosedEvent`。MCP suspension source也已从自由 payload切到
required typed fact。`CustomEvent` class、`EventType.CUSTOM`、default union/decoder与旧
字符串 constructor均已删除；test-only non-transcript fixture不进入 production registry。

Event schema generation已 bump，采用 reset-only PostgreSQL/Oxigraph event-world，不保留旧
CUSTOM decoder。MCP lifecycle、Inspector、recovery与 transcript-domain classification均已
同步。结论：D2 vocabulary债务 `CLOSED`。

## 8. Hook/outbox 重构

### 8.1 当前代码真值

`runtime/hooks.py::RuntimeHookManager` 当前：

- 顺序执行所有匹配 hook；
- 没有 criticality、delivery mode 或 idempotency contract；
- exception 只写入 process-local `errors: list[HookDispatchError]`。

`runtime/publisher.py::RuntimeEventPublisher` 也按 subscriber 顺序等待，并只在内存保存 subscriber errors。

两个典型 production hook 仍有同步存储工作：

- `RunTimelinePersistenceHook` 在 async `__call__` 中读取 EventLog、写 artifact、写 graph、写 mutation outbox；
- `CanonicalMutationOutboxReplayHook` 在 async `__call__` 中直接构造同步 reconciler 并 replay PostgreSQL/Oxigraph outbox。

writer 现在能正确报告 publication error，但 hook failure 本身仍没有 durable job、lease、retry schedule 或 dead-letter owner。进程退出后，`hook_manager.errors` 无法恢复。

### 8.2 应怎样重构

首先按语义拆分：

| 类型 | 正确 owner |
|---|---|
| canonical invariant / committed reducer | RuntimeSession writer transaction/reducer，不应注册成 hook |
| durable derived projection | content-addressed/idempotent projection job outbox |
| UI/CLI 当前进程观察 | lightweight best-effort publisher subscriber |
| maintenance wake signal | 只唤醒 durable worker，不在 subscriber 内完成完整 I/O |

推荐建立通用但不过度抽象的 projection-job schema：

- stable job key / projection kind；
- source runtime/session/event high-water；
- payload/reference fingerprint；
- status、attempt、lease owner、lease expiry；
- next retry、last bounded error；
- dead-letter/repair disposition。

不要通过“hook 失败后再向同一 ledger 写 `RuntimeObserverFailedEvent`”形成递归 publication。health table 或独立 durable job state更合适。

### 8.3 前置依赖

- writer correctness 已经满足，不再是 blocker；
- **migration runner 仍是 blocker**，因为 job/lease/dead-letter table 不能继续由 hook constructor 热创建；
- projection handler 必须有 idempotency contract。

### 8.4 完成门槛

1. async publisher loop 中不直接执行同步 DB/archive/Oxigraph I/O。
2. timeline 与 canonical mutation replay 要么成为 durable job，要么由现有 canonical outbox worker明确拥有。
3. restart 后 pending/failed projection job 可恢复。
4. best-effort subscriber failure 不影响 canonical commit，也不被误报为 durable projection已完成。
5. `RuntimeHookManager.errors` 只用于短期 diagnostics，不再是唯一 failure record。

结论：**仍是 OPEN；在 schema migration hard cut 后实施。**

## 9. Runtime dependency-cycle cleanup

### 9.1 当前仍有明确生产证据

`runtime/__init__.py` 和 `tools/__init__.py` 的模块注释都明确说明 facade 被设计为 lazy，以规避 runtime/tools/memory 的 import cycle。

当前双向依赖仍包括：

- tools executor/built-ins 导入 runtime session、permission、terminal、subagent、MCP supervisor；
- runtime agent/session/tool loop/permission/artifact service 导入 concrete tools DTO、registry 和 executor；
- `RuntimeSession.create_tool_executor()` 仍使用 local import 延迟触发 cycle；
- event/message/primitives 与 runtime/memory 的静态依赖表面仍形成大型 strongly-connected region。

仓库当前也没有 import-linter/dependency rule 作为新增依赖的阻断门槛。

### 9.2 已有进展

不能说“完全没做”：

- `primitives` 已承载大量 frozen facts；
- terminal、provider input、authority materialization 等区域已经出现 typed ports/coordinator；
- test support 目录已经存在；
- 若干 composition root 已能注入 commit port，而不是直接持有整个 RuntimeSession。

但这些局部改进尚未改变 package-level 方向，lazy facade 仍是产品启动条件。

### 9.3 推荐实施方式

不要先移动大量文件。先冻结可执行依赖规则：

```text
contracts/primitives
    -> message schema
    -> event schema
    -> replay/reducers
    -> runtime domain services
    -> host/cli/inspector composition
```

tools 不接收全能 `RuntimeSession`，而按能力注入小 port：

- workspace/file access；
- artifact read/write；
- terminal process/monitor control；
- runtime event/result commit；
- subagent control；
- memory query/mutation proposal。

第一步应加入 import architecture test，即使初始使用 explicit allowlist；之后每次迁移一个边界就缩小 allowlist。没有自动规则的“逐步清理”很容易再次回流。

### 9.4 完成门槛

- import-linter 或等价 AST rule 在 CI 执行；
- runtime/tools 不再双向导入 concrete implementations；
- built-in tool constructor 不接收 concrete `RuntimeSession`；
- event schema 不依赖 replay/assembler/reducer；
- permission/tool taxonomy 只有一个生产 registry；
- 最终删除 runtime/tools lazy export workaround。

结论：**仍是 OPEN，是 AgentRuntime 拆分的真实前置，而不是可以顺手整理的目录问题。**

## 10. AgentRuntime coordinator 拆分

### 10.1 当前进展

原审计建议先建立 typed `RunWorkingSet`。这一步已经部分完成：

- `runtime/run_entry.py::RunWorkingSet` 已拥有 committed run 的 model target、permission、plan、capability exposure、resume boundary 和 execution activation；
- Host ingress、provider-input generation、terminal monitor、ledger materialization 等领域已有专门 coordinator。

这说明拆分不应从零开始，也不应另建一组与现有 coordinator 重叠的 service。

### 10.2 仍然存在的债务

当前规模为：

- `runtime/agent.py`：约 8,588 行；
- `host/session.py`：约 5,979 行。

行数不是结论，真正的证据是 `LoopState.scratchpad: dict[str, Any]` 仍保存大量 production owner state，例如：

- Host run boundary、ingress admission、current user、capability basis；
- run execution handle/borrow authority；
- plan revision与 interaction计数；
- model call/context index；
- finalization、pending RunEnd candidate 和 terminal commit state；
- suspended/resume activation state。

这些字段跨 HostSession 和 AgentRuntime 读写，没有统一 schema、generation 或 invalidation owner。现有大函数也同时处理 compile、model step、tool terminalization、pending interaction、compaction 和 run finalization。

### 10.3 重基线后的切法

先迁移 owner，再移动控制流：

1. 将 scratchpad 分成 typed attempt owner：
   - `HostRunBoundaryAttempt`；
   - `ModelStepAttempt`；
   - `InteractionSuspensionAttempt`；
   - `RunFinalizationAttempt`。
2. 每个 attempt 冻结 generation、stable candidate 和 terminal disposition；不把 durable truth搬进 process-local DTO。
3. 复用已存在的 HostIngress/ProviderInput/Monitor coordinator。
4. 再按状态机边界抽出 context/model step、tool batch、interaction resume、finalization orchestration。
5. AgentRuntime 最终只拥有 loop phase ordering；HostSession 只拥有 ingress、session resource 和 lifecycle。

不要一次性创建 `ModelStepCoordinator` 等空壳后把整个 RuntimeSession 当 service locator 注入进去。那只会把循环 import 与 scratchpad 扩散到更多文件。

### 10.4 前置依赖与完成门槛

前置：

- package ports 与 import rule 至少完成第一轮；
- production scratchpad 的 owner DTO 已建立；
- remaining CustomEvent 已 typed，避免新 coordinator 继续发自由字典事件。

完成门槛：

- production 路径不再使用任意 scratchpad key；
- safe-point phase 有唯一有序定义；
- interaction、model step、tool batch、finalization 可分别做状态机测试；
- coordinator 不直接依赖 HostSession 或全能 RuntimeSession facade；
- AgentRuntime/HostSession 缩小是 ownership 迁移的结果，不是独立 KPI。

结论：**仍是 OPEN，但应位于 dependency/ports 之后，不是下一项最先动手的债务。**

## 11. Legacy MCP / in-memory product mode

这个旧工作项实际上包含两个不同状态的子项，必须拆开评价。

### 11.1 legacy MCP transport：已闭环

旧手写 HTTP/stdio manager 文件已经删除。`runtime/mcp` 当前只保留 config、manager protocol、SDK、store、supervisor 和 typed DTO。

因此“删除 legacy MCP transport spike”应标记为 `CLOSED`。

仍有一个 `MockMcpClientManager` 位于 production package 并从 `runtime.mcp` 导出；当前用途是测试。它应迁到 `tests/support`，但不能据此重新宣称旧 transport 仍存在。

### 11.2 in-memory product mode：仍未闭环

当前生产 API 仍公开：

- `HostCore(durable: bool = True)`；
- `build_agent_runtime_wiring(..., durable: bool)`；
- `build_in_memory_runtime_wiring()`；
- `InMemoryMemoryWriteUnitOfWork`；
- runtime lazy facade 对 in-memory factory 的 export。

虽然 factory 会发出 compatibility/test-only warning，但它仍是 production composition 的合法分支。大量测试也通过 `HostCore(durable=False)` 或 production factory 构造另一套架构。

债务不是“仓库里有 fake”，而是“产品 composition root 用布尔值选择 fake architecture”。

### 11.3 推荐 hard cut

1. 在 `tests/support/runtime_factory.py` 建立明确的 component-test composition。
2. 测试直接依赖该 factory，不经过 production `durable` flag。
3. production `HostCore` 永远 durable，删除字段与分支。
4. `build_agent_runtime_wiring()` 删除 `durable` 参数，只构造 production wiring。
5. `MockMcpClientManager` 与 in-memory UOW/factory 移入 test support；底层通用 in-memory data structures可以保留在对应 test-support 模块。

### 11.4 完成门槛

- `src/pulsara_agent` 不含 `durable=False` 产品分支；
- production export 不包含 mock manager/in-memory runtime factory；
- unit/component tests仍无需 PostgreSQL即可运行；
- durable integration tests使用显式 migrated PostgreSQL fixture。

结论：**旧工作项为 PARTIAL：legacy transport 已关，in-memory selectable product mode 仍开。**

## 12. Schema hot-path hard cut

状态：**CLOSED（2026-07-22，reset-only V1）**。

长期authority已转移到
[POSTGRES_SCHEMA_MIGRATION_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/POSTGRES_SCHEMA_MIGRATION_CONTRACT.zh.md)；根目录实施规格只保留落地背景。

### 12.1 已完成的边界

- packaged `0000..0004` migration registry是唯一physical schema真源；
- `pulsara_schema_migrations`持久化SQL checksum、migration contract和累计registry prefix；
- `pulsara db status|migrate|verify`已落地，只有migrate读取admin DSN并拥有mutation authority；
- Host/Inspector/checkpoint/benchmark composition在资源分配前借用process-owned verify-only service；
- 所有production PostgreSQL adapter required接收verified connection provider，不再接受raw DSN；
- direct/pool/reconnect physical connection在可见前验证database、role、search path、server、head与prefix；
- EventLog、Graph、memory/governance stores及UOW中的runtime DDL、`ensure_schema()`和旧SQL exports已删除；
- runtime role仅拥有所需DML/USAGE/EXECUTE权限，DDL denial由integration gate验证；
- PostgreSQL tests使用per-worker fresh migrated database；durable benchmark使用verified lease并在measurement外验证clone；
- Docker init只创建受限runtime role，pgvector及全部Pulsara objects由migration拥有。

### 12.2 Adoption 目标 supersession

早期“explicit baseline/adopt existing database”目标已被reset-only V1明确撤销：

- ledger缺失但存在任何Pulsara-reserved object时返回`schema_unmanaged_database`；
- 不从当前table形状推断migration history；
- 不提供`--adopt-existing`、runtime lazy baseline或startup auto-migrate；
- hard cut重置PostgreSQL，并在canonical world变化时同步重置Oxigraph projection；
- 未来若需保留旧数据，必须另立offline export/import或migration规格。

### 12.3 后续维护规则

Schema hot-path债务关闭不表示schema不再演进。后续Hook/outbox、typed event或memory physical schema变化必须追加immutable migration、manifest与grant policy，并通过same runner；任何constructor/UOW bootstrap回流都视为architecture regression。

## 13. Compaction-memory extension

### 13.1 correctness 已经比原审计成熟

当前 compaction candidate producer 已具备：

- typed extractor contract；
- `ContextCompactionMemoryCandidatesProposedEvent`；
- event-first candidate projection；
- `MemoryCandidateProjectionCommitPort` 与 durable projection outbox；
- producer event FULL 后幂等投影 candidate row；
- candidate payload fingerprint 与 attribution join。

因此原审计担心的“candidate append 与 audit event commit boundary 不明确”已经闭环。该工作项也不再依赖一个尚不存在的 LiveRuntimeEventWriter。

### 13.2 ownership 方向仍然错误

`runtime/compaction` 目前仍直接 import/拥有：

- `memory.candidates.pool` 的 DTO、pool 和 fingerprint；
- memory scope/domain/ontology；
- candidate extraction policy；
- candidate sink 与 projection outbox port。

`ContextCompactionPolicy` 仍内嵌 `ContextCompactionMemoryCandidatePolicy`，`ContextCompactionService` 仍负责 optional `<memory_candidates_json>` prompt、解析、normalization、candidate attribution 和 proposed event。

因此 context-maintenance core 仍依赖 memory product semantics。memory candidate schema变化会继续修改 compaction core。

### 13.3 推荐目标

保留“一次 compact 模型调用同时返回 summary 与 optional extension”的产品优化，但反转依赖：

```text
ContextCompactionCore
  -> CompactionOutputExtensionPort
       -> MemoryCompactionCandidateExtension
```

core 只拥有：

- versioned extension ID/contract；
- bounded prompt fragment；
- bounded raw extension block或artifact reference；
- summary artifact与compaction lifecycle；
- extension parse outcome/audit commit port。

memory-owned extension 拥有：

- candidate schema、scope、ontology；
- parser/normalizer/fingerprint；
- proposed event payload；
- candidate projection outbox与pool sink。

extension parse failure继续是 best-effort，不得让已经合法的 summary失败；但 parse result和零候选都必须有 typed audit outcome。

### 13.4 前置与完成门槛

前置：

- writer correctness 已满足；
- 最好先建立 contracts/ports dependency rule，防止新 extension port反向依赖 core implementation；
- schema migration runner负责 projection/outbox schema，不能在 extension constructor里建表。

完成门槛：

- `runtime/compaction` 不 import `memory.candidates.*` concrete types；
- `ContextCompactionPolicy` 不包含 memory candidate policy；
- 无 memory extension时 core 仍能完整 compaction；
- memory extension启用时仍只调用一次模型；
- proposed event、outbox、candidate projection的现有 crash/retry tests保留。

结论：**仍是 OPEN，但属于依赖方向债务，不是 correctness 紧急故障。**

## 14. 重基线后的依赖图

原依赖表把大多数工作都挂在“尚未完成的 LiveRuntimeEventWriter”上，这已经过时。当前更准确的依赖是：

```text
Schema migration runner + verify-only startup
    ├─ Durable hook/projection job outbox
    └─ 后续所有新增 PostgreSQL schema

Current RuntimeSession writer（已完成主体）
    ├─ typed event vocabulary（已闭环）
    ├─ compaction direct-port/post-scan cleanup（已闭环）
    └─ governance event outbox（已闭环）

Contracts/ports + executable import rules
    ├─ in-memory product branch -> tests/support
    ├─ compaction-memory extension boundary
    └─ typed run/model/finalization attempts
          └─ AgentRuntime / HostSession coordinator split

PERF0 writer profile
    └─ only if justified: native async PostgreSQL adapter
```

Governance same-UOW 不再位于待办图中；它是已经完成的 outbox ownership。

## 15. 推荐实施顺序

### D0：更新 architecture guards 与旧审计状态

- 在 `ARCHITECTURE_DEBT_AUDIT.zh.md` 或债务索引中把 writer/governance状态标记为已重基线；
- guard direct EventLog write、production CustomEvent 和 package dependency新增边；
- 避免后续 PR按旧依赖表重复建设。

### D1：Schema hot-path hard cut（已完成）

Migration ledger/runner/CLI、verify-only startup、verified connection provider、runtime DDL删除与受限role gate已经落地。后续durable hook schema直接在该registry上增加migration。

### D2：Event vocabulary 与 writer 尾巴（`CLOSED`）

- [x] typed 化 7 个 production CustomEvent，并新增 typed MCP closure；
- [x] 删除 compaction direct production fallback；
- [x] 删除 Host/mid-turn compaction post-scan 与二次 publication；
- [x] direct EventLog mutation建立 exact AST allowlist。

关闭范围只限上述四项。Typed failure audit不等于 durable projection retry job；compaction
candidate producer FULL前的 crash-to-durable-owner窗口也未因此关闭。

### D3：Durable hook/projection jobs（`OPEN`）

- timeline与canonical mutation replay脱离publisher critical path；
- durable lease/retry/dead-letter；
- UI/CLI subscriber保持轻量 best-effort。

### D4：依赖规则与 test-support hard cut

- 建 contracts/ports与import rule；
- 优先切断 runtime/tools concrete 双向依赖；
- `MockMcpClientManager` 与 in-memory runtime composition移入 tests/support；
- production HostCore删除 `durable` branch。

### D5：Compaction-memory extension（`OPEN`）

- memory-owned extension；
- 保留一次模型调用；
- 保留现有 event-first/outbox correctness。

### D6：AgentRuntime/HostSession ownership 拆分

- scratchpad -> typed attempts；
- 按实际状态机提取 coordinator；
- 不再让新 coordinator依赖全能 RuntimeSession/HostSession。

### 独立性能支线

只有在确定性 writer benchmark 证明以下一项显著后，才启动 native async PostgreSQL 改造：

- executor queue wait；
- thread handoff；
- sync pool contention；
- event-loop responsiveness；
- terminal drain latency。

如果 PostgreSQL transaction/fsync 本身占主导，把 sync driver换成 async driver不会自动降低 durable wall time。

## 16. 可执行验收清单

| 领域 | Architecture gate |
|---|---|
| Writer | live production direct `event_log.append/extend` 仅允许 RuntimeSession writer internals |
| Governance | memory UOW 内必须产生 stable event outbox ticket；dispatch failure后可精确重试 |
| Typed event vocabulary | production `CustomEvent`/`EventType.CUSTOM`与旧 7 个字符串constructor为零 |
| Hooks | publisher subscriber中无同步 DB/archive/Oxigraph重工作；durable jobs可 restart |
| Dependencies | CI import rule阻止 runtime/tools与event/replay反向边回流 |
| Agent state | production arbitrary scratchpad key最终为零 |
| Legacy/test | production composition不含 `durable=False`、mock MCP或in-memory runtime factory |
| Schema | runtime role无 DDL权限仍通过 durable Host integration |
| Compaction | `runtime/compaction` 不依赖 concrete `memory.candidates` package |

建议同时保留以下定向行为测试：

- event write cancel-after-FULL、CAS conflict、reducer catch-up与publication failure；
- governance UOW commit后ledger dispatch失败/重试；
- compaction producer event FULL后candidate projection恢复；
- hook job duplicate/idempotency、lease expiry、dead-letter；
- migrated/stale/future/checksum-drift database startup；
- production composition import与test-support separation。

## 17. 最终裁决

依赖表后半部分的九项工作，当前不能按“九项都未做”处理：

- **1 项已被更合适的方案替代并闭环**：Governance events 同 UOW；
- **1 项 correctness 已完成，仅保留独立性能门控**：Async LiveRuntimeEventWriter；
- **1 项应拆成已完成与未完成两部分**：legacy MCP 已删除，in-memory product mode仍在；
- **4 项仍是有效债务**：hooks、dependency cycles、AgentRuntime ownership、compaction-memory ownership。

Schema hot-path与 D2 event vocabulary/writer尾巴已经完成。下一步应实施 durable hook jobs与
依赖方向治理；D3与D5继续保持 `OPEN`，不要把本次 typed failure audit或 process-local
projection receipt误报为 durable outbox correctness。

这份重基线的目的不是减少债务数量，而是把工程投入重新对准仍然存在的风险。
