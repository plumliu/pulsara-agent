# Pulsara 运行时架构债务重基线审计

> 审计日期：2026-07-22
> D5 完成复核：2026-07-28
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
| Hook/outbox 重构 | `CLOSED` | timeline/evidence、canonical mutation surfaces、seed repair、shutdown physical owner与eventual working-context均由durable owner闭环 | 已完成 |
| Runtime dependency-cycle cleanup | `CLOSED`（D4 scope） | target DAG、ports、test-support、durable Host与facade hard cut已闭环；剩余全局SCC仅作为D6 diagnostic baseline | 已完成 |
| AgentRuntime coordinator 拆分 | `OPEN`，已有基础 | `RunWorkingSet` 与若干 coordinator 已出现，但 production scratchpad 和大范围 orchestration 仍集中 | 中后期 |
| 删除 legacy MCP / in-memory product mode | `CLOSED` | legacy transport、production mock、`durable=False`与in-memory product composition均已删除；fake world只在tests/support | 已完成 |
| Schema hot-path hard cut | `CLOSED` | migration registry/ledger、verify-only startup与verified connection provider已落地；constructor/UOW DDL及raw-DSN adapter入口已删除 | 已完成 |
| Compaction-memory extension | `CLOSED` | summary 与 extraction 已拆为两次调用；Call B 由 durable job、exact human evidence、budget、RESULT_READY 与 governance 完整拥有 | 已完成 |

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

## 8. Durable hook/projection jobs（`CLOSED`）

状态：**CLOSED（2026-07-25，DPJ0–DPJ5）**。

生产路径现已按语义拆分：

| 类型 | 唯一 owner |
|---|---|
| canonical invariant / committed reducer | RuntimeSession writer transaction/reducer |
| timeline / tool-result evidence | EventLog-driven durable projection jobs |
| search / vector / Oxigraph | canonical mutation V2 surface delivery |
| UI/CLI 当前进程观察 | lightweight best-effort subscriber |
| publisher integration | O(1) coalesced wake only |

已完成的主体：

- migration v5-v8冻结job、receipt、target head、lease、surface delivery与activation/cutover；
- per-trigger source horizon严格等于trigger sequence，seed checkpoint与jobs同transaction；
- timeline使用incremental paged persistent reducer，evidence使用single-assignment exact join；
- applied/superseded result均有immutable receipt，restart可恢复pending/retry/expired lease；
- job dead-letter与typed repair CAS进入Inspector/CLI；
- canonical mutation V2统一search/vector/Oxigraph，不再有replay hook或surface-specific worker；
- `RunTimelinePersistenceHook`、`ExecutionEvidencePersistenceHook`、
  `CanonicalMutationOutboxReplayHook`和旧execution-evidence writer已物理删除；
- publisher subscriber不执行DB/archive/Oxigraph/embedding I/O；
- Host run不等待projection，restart dogfood证明普通backlog最终完成。

最终复核补齐：

- seeder按events/bytes选择最长非空前缀，failure/repair/resolution为同transaction exact chain；
- stable authority keyset分页、bounded dirty session/kind hint与满页立即续扫共同保证恢复与低延迟；
- projection close timeout使Host保持`CLOSING/CLOSE_BLOCKED`并保留dependency lease；
- working-context每个planned model step至多一个session-owned bounded async operation；
- rebuild decommission exact-read durable FULL receipt并重验surface/target/handler；
- malformed arguments使用strict parser与递归不可变carrier；
- canonical mutation sequence在首次head尚不存在时也由transaction advisory lock串行分配。

完整bootstrap cutover、Turn/Artifact base document、global-limit前过滤leased/conflicted target、
restart Host dogfood与结构 benchmark均已通过。

关闭范围只限D3 derived projection ownership。RuntimeHookManager仍可承载best-effort
operational callbacks；它们的process-local diagnostic不是durable job，也不需要升级。
Compaction candidate projection outbox属于D5，不因D3关闭而自动关闭。

## 9. Runtime dependency-cycle cleanup

状态：**CLOSED（2026-07-26，D4 scope）**。

### 9.1 已完成的硬切

- `ports`、低层 primitives、replay receipt 与 Host composition contract 已建立最终类型 owner；旧路径不存在 compatibility re-export；
- `ToolExecutor` 由 runtime 唯一拥有，concrete tools 只消费 artifact、terminal、MCP、subagent 与 registry closed ports；
- `tools -> runtime`、`capability -> concrete runtime/tools`、`event -> replay/runtime`、`storage|graph|memory -> runtime.projection_jobs`、`src -> tests` 五组 target DAG 反向边归零；
- canonical mutation pure factory、PostgreSQL repository、transaction capability 与 writer 已按层分离；Memory UOW 只借用 revocable scoped facade；
- builtin descriptor、binding、permission、recovery、Long-Horizon action taxonomy 收敛到单一 catalog；
- runtime/tools lazy facade、temporary type/edge cutover ledger与旧 package owner均物理删除；
- production Host 只接受 durable composition；test fake composition、mock MCP 与 in-memory governance UOW 仅存在于 `tests/support`。

### 9.2 可执行门控

普通 pytest 运行 canonical AST dependency scanner。D4 target DAG 不再使用 exception；global package SCC只保存 module-level canonical observation baseline，新增同 package-pair import也会失败。

经 D5 向下收缩后，最终 residual baseline 为 391 条 observation，fingerprint：

```text
sha256:3714e6d2b587364c3636a249feb2fc6d2171edfc2f5c802278e957562e7126cc
```

该 baseline 中仍存在跨 `runtime/llm/memory/host/storage/graph/event_log/capability` 的全局 SCC。D5 已删除其负责的旧边并阻止新增 residual edge；剩余部分属于 D6 AgentRuntime/HostSession ownership 拆分。D4/D5 的关闭不宣称全仓库跨 package SCC 已消除，也不得重新引入 lazy facade。

### 9.3 验证结果

- D4 专项最终 `65 passed`；
- 全量 pytest 首轮 `2504 passed, 27 failed, 2 skipped`，27 项旧 fixture迁移后按用户要求只复跑失败集合并全部通过；
- migrated PostgreSQL Host backlog/restart与open/resume/close通过；
- frozen manifest validate通过；`durable-resume`、`subagent-delegation`、`workspace-patch`真实 provider dogfood通过；
- static grep/AST、正反import-order smoke、changed-file format/lint与diff check通过。

结论：**D4 target scope 已关闭；D5 已向下收缩 baseline，remaining global SCC由D6负责。**

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

状态：**CLOSED（2026-07-26）**。

### 11.1 legacy MCP transport：已闭环

旧手写 HTTP/stdio manager 文件已经删除。`runtime/mcp` 当前只保留 config、manager protocol、SDK、store、supervisor 和 typed DTO。

因此“删除 legacy MCP transport spike”应标记为 `CLOSED`。

`MockMcpClientManager` 已迁入 `tests/support/mcp.py`，production package不再导出或构造mock transport。

### 11.2 in-memory product mode：已闭环

- `HostCore.production()`只安装`ProductionHostComposition`，不再接受`durable`布尔分支；
- production `build_agent_runtime_wiring()`不再选择in-memory world；
- whole in-memory runtime factory与fake governance UOW分别位于`tests/support/runtime_factory.py`和`tests/support/memory_uow.py`；
- component tests通过显式test composition借用fake world，production package不反向依赖tests；
- durable integration统一使用migrated PostgreSQL fixture与verified connection provider。

底层通用的in-memory EventLog/GraphStore仍可作为数据结构存在；它们不再组成可由产品composition root选择的第二套架构。

结论：**legacy MCP与selectable in-memory product mode均已关闭。**

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

### 13.1 最终裁决

D5 已按 `PULSARA_POST_COMPACTION_MEMORY_EXTRACTION_HARD_CUT_IMPLEMENTATION.zh.md`
完成。原“一次 compaction 调用同时返回 summary 与 memory extension”的方向被 supersede；生产
路径现在固定为两次职责分离的调用：

```text
Call A: summary-only context continuity
  -> ContextCompactionCompletedEvent
  -> same-batch ContextCompactionMemoryExtractionRequestedEvent

Call B: optional durable derived work
  -> exact direct-human evidence manifest
  -> target-aware input budget + session background budget
  -> independent model lifecycle
  -> RESULT_READY
  -> Completed event + receipt/head/candidate outbox/job success
  -> candidate pool -> governance -> optional canonical memory write
```

Call B 不属于 compaction correctness，也不创建 Host run ingress。没有 eligible evidence、input
budget 不足或 background budget 耗尽均由 closed no-call outcome 终结，不触发 provider。

### 13.2 已闭合的 ownership

当前生产不变量包括：

- `runtime/compaction` 不再 import 或拥有 concrete memory candidate DTO、parser、sink或outbox；
- Call A prompt与输出只包含 summary；旧 `<memory_candidates_json>` producer、event与parser已物理删除；
- Completed 与唯一 Requested trigger同批提交，无 post-scan recovery；
- lossless transcript manifest只选择 exact direct human input，summary、assistant、tool、runtime observation均不是 evidence；
- Call B 的 lease/retry/dead-letter、dispatch ordinal、ModelCall Start/End与session budget均有 durable authority；
- terminal projection是唯一 raw model-output authority；RESULT_READY跨physical retry保持stable candidate；
- extraction Completed、receipt、head、candidate outbox与job success经RuntimeSession writer同事务提交；
- model只提出 pending Preference candidate，canonical memory仍必须经过治理；
- governance在写入前exact-read RunStart并重算sanitizer，持久化完整 sanitized human Evidence；
- migration `0009`、session bootstrap、Inspector、restart/close与long real-LLM dogfood均已覆盖。

### 13.3 验证证据

机器可读 DoD 记录位于：

`benchmarks/suites/core/v1/cme5_dod_evidence.json`

它绑定 frozen dogfood suite、`manual-compaction-trail` scenario与runner fingerprints，并记录
CME0-CME5 gates、pytest closure、PostgreSQL/Oxigraph集成和real-provider结果。

结论：**D5 CLOSED。**

## 14. 重基线后的依赖图

原依赖表把大多数工作都挂在“尚未完成的 LiveRuntimeEventWriter”上，这已经过时。当前更准确的依赖是：

```text
Schema migration runner + verify-only startup（已完成）
    ├─ Durable projection jobs v5-v8（已完成）
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

### D3：Durable hook/projection jobs（`CLOSED`）

- [x] timeline/evidence脱离publisher critical path；
- [x] canonical mutation V2统一surface delivery；
- [x] seeder分页/failure隔离与fair claim；
- [x] evidence malformed arguments与base-document authority；
- [x] surface repair/decommission与Host close physical-owner safety；
- [x] exact bootstrap confirmation与eventual working-context；
- [x] Inspector/CLI、restart/final migration/Host dogfood最终复核。

### D4：依赖规则与 test-support hard cut（`CLOSED`）

- [x] 建立 contracts/ports与canonical AST import rule；
- [x] D4 target DAG forbidden edge清零，residual SCC以D6 diagnostic baseline冻结；
- [x] 切断 tools -> runtime concrete反向依赖，ToolExecutor归runtime唯一拥有；
- [x] `MockMcpClientManager`、in-memory runtime与fake governance UOW移入 tests/support；
- [x] production HostCore删除 `durable` branch并使用唯一durable composition；
- [x] runtime/tools lazy facade物理删除；
- [x] D4-5全量pytest失败集合闭环、durable integration与冻结dogfood最终确认。

### D5：Compaction-memory extension（`CLOSED`）

- [x] summary-only Call A 与 durable optional Call B职责分离；
- [x] Requested same-batch admission、D3 job与session background budget闭环；
- [x] exact direct-human evidence manifest与target-aware budget闭环；
- [x] terminal projection、RESULT_READY及atomic result settlement闭环；
- [x] pending candidate继续经过governance，不直接写canonical memory；
- [x] old one-call producer与runtime/compaction memory concrete ownership物理删除；
- [x] migration、Inspector、recovery、PostgreSQL/Oxigraph与frozen dogfood通过。

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
- **2 项由D4一并关闭**：target dependency/test-support hard cut，以及legacy MCP/in-memory product mode；
- **1 项仍是有效债务**：AgentRuntime/HostSession ownership拆分。

Schema hot-path、D2 event vocabulary/writer尾巴、D3 durable projection jobs、D4 dependency/test-support
hard cut与D5 post-compaction memory extraction均已完成。下一项是D6；D4冻结且经D5向下收缩的
global SCC baseline继续作为D6新增依赖的阻断基线。

这份重基线的目的不是减少债务数量，而是把工程投入重新对准仍然存在的风险。
