# Event Log / Runtime Storage Contract

_Created: 2026-07-04_

本文档定义 Pulsara runtime event log 与 Postgres runtime truth storage 的长期契约。它描述“事实如何落盘”，不描述 model/tool loop 的执行顺序；执行顺序见 [AGENT_RUNTIME_LOOP_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md)。

相关代码：

- [src/pulsara_agent/event/events.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event/events.py)
- [src/pulsara_agent/event_log/protocol.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event_log/protocol.py)
- [src/pulsara_agent/event_log/postgres.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event_log/postgres.py)
- [src/pulsara_agent/event_log/serialization.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/event_log/serialization.py)
- [src/pulsara_agent/storage/migrations/](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/migrations)
- [tests/test_event_message_system.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_event_message_system.py)
- [tests/test_inspector.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_inspector.py)

---

## 1. 核心立场

Runtime truth 是 append-only typed event log。

Canonical facts：

- `sessions`
- `runs`
- `turns`
- `agent_events`
- artifacts / tool result artifact index

`runs` / `turns` / inspector projections 是索引和摘要，不是 transcript 真源。任何需要解释 agent 历史的路径必须能从 `agent_events.payload` 重建。

---

## 2. AgentEvent typed payload

所有 runtime event 必须是 `AgentEvent` union 中的 typed Pydantic model。

序列化规则：

- 写入时使用 `dump_agent_event(event)`；
- 读取时使用 `load_agent_event(payload)`；
- payload 必须包含 `type`；
- unknown type 必须失败，而不是静默降级为 `CUSTOM`。

生产 schema 不再包含 `CustomEvent` 或 `EventType.CUSTOM`。Diagnostic、audit 与
compatibility 事实同样必须选择一个 bounded typed event；test-only non-transcript fixture
不得进入 default `AgentEvent` union 或 production decoder。当前 hard-cut event schema
generation 为 `6`，旧 CUSTOM payload 不属于 supported replay world，升级必须执行
reset-only PostgreSQL/Oxigraph event-world workflow。

### 2.1 Typed runtime audit vocabulary

以下 runtime facts 是 explicit non-transcript event domain：

- MCP input-required expired、binding-changed、resume-failed、resolution-submitted 与
  interaction-closed；
- context compaction requested；
- mid-turn context compaction skipped；
- tool-result evidence projection failed；
- typed MCP `ToolExecutionSuspendedEvent` source branch。

每个 producer 必须预先冻结 deterministic ID 与 typed payload；`NONE` 重试复用同一
candidate，`FULL` 采用 committed receipt，`PARTIAL/UNKNOWN` 进入 reconciliation。不得用
旧字符串 event name、metadata 字典或 ledger post-scan 补造这些事实。

### 2.2 RunStart MCP installation attribution

新schema下每个`RunStartEvent`必须携带非空`mcp_installation_id`与
`mcp_installation_owner_runtime_session_id`。Parent run的owner指向自身runtime session；subagent child使用独立
EventLog时，owner指向拥有MCP supervisor/installation audit的parent runtime session。Inspector只能用这两个字段
跨ledger join，不得从当前live supervisor猜历史surface。

当本run首次引用尚未durable的installation时，`RunStartEvent`与全部pending
`McpCapabilitySnapshotInstalledEvent`必须通过一次`RuntimeSession.emit_many()`原子提交。commit acknowledgement前
不得清除pending audit、不得启动model call；失败时整批不可见，不能留下引用悬空installation的半启动run。
Canonical empty installation是schema内建sentinel，不需要另写installed event。

`EventPublicationAfterCommitError`不是commit失败。RunStart/resume caller必须从其
`result.committed_events` acknowledge已提交installation audit，然后再向上传播observer failure；不得把同一stable
audit event id留在pending queue供下一run以不同context重复写入。

已有run的approval/plan/MCP resume没有第二条`RunStartEvent`。若resume前safe point安装了新的MCP surface，HostSession
必须使用原suspended run context单独`emit_many()` pending installed audit，并在commit acknowledgement后才把
pending state切回active或继续model/tool loop。pre-commit失败保留原pending state、lease与audit供同一host action重试；
post-commit publication failure清除committed audit ownership但仍向上传播；
不得因为“没有新RunStart”而跳过durable installation attribution。

### 2.3 Context input durable carriers

`RunStartEvent.current_user_message`、`ToolResultEndEvent.observation_timing/render_profile/essential_result/terminal_payload_timing`、
`ContextCompiledEvent.input_audit|input_failure`均为required typed contract。Replay不得从`metadata`、tool output JSON或当前
descriptor补造缺失字段。

Context compiler读取event时必须使用atomic range snapshot：同一次读取返回high-water与canonical stored bytes。每个
`FrozenStoredEvent`的wrapper identity必须与decoded payload完全一致；range不连续、同ID异payload或structural latch一律
fail closed。EventLog reader返回owned copies，修改调用者对象不能改变stored payload、slice fingerprint或另一projector输入。

Production PostgreSQL range read是deadline-aware contract：同一repeatable-read transaction必须设置不超过caller绝对deadline的
connect/statement timeout。async caller通过RuntimeSession-owned bounded I/O owner执行sync driver调用；外层task取消不等于物理
query已停止，Host close必须等待物理operation退出后才能释放session resources。

高频派生路径不得用`EventLog.iter()`扫描整本session ledger。Model result materialization使用
`read_raw_model_call_events(resolved_model_call_id, max_events, max_payload_bytes, deadline_monotonic)`读取该call的
Start/semantic/End facts；PostgreSQL必须使用`session_id + resolved_model_call_id + sequence`的expression/generated-column
索引，不能先扫描session再在Python中过滤。Model control attribution使用一次exact-ID bounded read取得Start/End，I/O必须发生在
control linearization lock外。Model stream subscription默认从handle安装时冻结的cursor观察process-local bounded committed history；
不得为每个subscriber读取历史ledger，超出retained history时返回typed lagged状态并由bounded materializer恢复。
正常model stream commit必须使用handle-owned confirmed source/durable cursors。Adapter-private raw delta先由唯一Coordinator按kind/block/media continuity聚合为bounded segment，再按durable event/candidate bytes/oldest age组成transaction batch；四类旧durable DeltaEvent已删除。Segment是`non_transcript`，其wall-clock布局不得进入transcript semantic accumulator。不得逐source item查询Start、End或已提交semantic history，也不得在successful append后按ID回读同一event。只有reopen、UNKNOWN或recovery允许一次bounded per-call reconstruction。

Provider-input append必须保存对每个historical replacement source head的typed disposition。
普通append reducer禁止删除head；changed optional source被allocation省略时，只能由
`SOURCE_DISPOSITION_REWRITE_REQUIRED` authority在old-close + rollover + new-start + initial
append + ModelStart原子批中删除exact predecessor heads。Reducer必须重算disposition set、
predecessor-head fingerprints与resulting head set；source absence不能成为删除事件。

Explicit Long-Horizon observation rollover不能只保存planner自报proof。Commit reducer必须以
old core、old incremental lifecycle state、new initial append和resulting core重算source stable
state、active/protected/eligible partition roots、transitive coverage与resulting effective-head
set；任一drop、duplicate或artifact/proof drift使批次fail closed。

一次sanitizer adoption产生的完整candidate tuple必须在acknowledgement与任何await前归Coordinator/handle所有。Model stream terminal projection持久化`ModelStreamSettlementMeasurementFact`：adapter/synthetic source count/bytes、singleton/segment count、segment/candidate bytes和actual semantic batch count；每个batch引用exact `PhysicalOperationChargeAppliedEvent` identity。Durable actual candidate bytes唯一取自writer-prepared charge fact，包含RuntimeSession metadata overlay；Coordinator的pre-overlay prospective bytes只用于admission，不是历史measurement真源。Terminal source与`PhysicalOperationSettlementFact.model_stream_measurement_fingerprint`必须一致，历史Inspector只输出bounded aggregate。

`PhysicalOperationChargeAppliedEvent`的stored charge必须使用`base + business_event_count * per_event`确定公式。Writer在transaction内分配sequence并构造stored envelope后、commit前验证actual bytes不超过本次quote；低估必须rollback。禁止固定小常量、post-commit才发现低估，或让retry重算出不同candidate payload。

LongHorizon live store启动使用
`read_raw_events_by_types(..., active_runs_only=True)`在同一数据库快照中取得尚无`RunEndEvent`的run所拥有的
reducer-relevant facts与whole-ledger high-water。后者是sparse selection，store必须把未返回event解释为确定性no-op并推进
全局high-water，不能把selection当作新的authority。已关闭run不进入production process state；其事实仍可由Inspector或
privileged replay从canonical ledger恢复。两类sync PostgreSQL读取都必须通过session-owned bounded I/O service执行并参与
Host close drain。

RuntimeSession中央lifecycle validation不得读取whole-session ledger。RunStart/window/rollout identity优先来自committed incremental store；
compaction Started/terminal、settlement与resume RunStart使用exact-ID read；resume source exposure使用按run/type索引的bounded sparse snapshot。
LongHorizon store对普通semantic/UI event只推进contiguous high-water，不复制reducer maps、LRU或projection state。

全部production async event mutation必须进入同一个RuntimeSession-owned FIFO writer。Writer负责durable commit/confirm、同步reducer fold和ordered
publication handoff；event loop不得直接执行PostgreSQL事务或同步等待writer使用的跨线程锁。Process-owned blocking runtime必须拆成
`critical_ledger`与`auxiliary_io`两条物理lane：event commit/confirm只能进入前者，context read、manifest/artifact I/O只能进入后者。
辅助I/O饱和不得占用critical worker。每个RuntimeSession仍独立拥有queue、operation identity、absolute deadline、cancel/detach、
reconciliation latch与close drain。

每个critical write attempt只分配一个稳定absolute deadline。该deadline从入队开始覆盖commit与stable confirmation，不能在异常或取消分支
重新获得一个完整timeout。异步waiter必须在queued operation到期时CAS删除该operation并立即得到typed NONE；已经越过physical-start
linearization point的operation继续由critical owner在原deadline内收口。同步`confirm_event_batch()`仅允许critical worker owner调用；async
continuation必须显式携带原attempt deadline并进入同一FIFO。独立的service-owned reconciliation/retry可以创建新attempt，但不能伪装成原写入
attempt的confirmation。

PostgreSQL event writes必须使用带critical-write reserve的bounded connection pool或writer-owned persistent/reconnectable connection；bounded reader
并发必须低于pool max size，为durable writer保留connection lease。Writer的absolute deadline必须覆盖queue、connection lease、advisory lock、statement、
commit acknowledgement与stable confirmation；commit phase要为confirmation预留时间。事务使用transaction-local `lock_timeout`和
`statement_timeout`，UNKNOWN不得伪装成NONE。禁止每个25ms semantic batch新建连接。
同一事务内parent session/run/turn identity按唯一key至多验证一次；已成功提交的parent identity可process-local缓存，commit失败不得污染缓存。

PRE_RUN transcript projection使用最新可读`ContextCompactionCompletedEvent + summary artifact`作为durable projection checkpoint，
随后仅按`session_id + event_type + sequence`索引读取`keep_after_sequence`之后的bounded transcript/recovery control facts；全部目标
assistant replies必须由一次`read_raw_replies_snapshot()`在同一frozen high-water下批量读取，并使用aggregate events/bytes cap与server-side cursor，
不得逐reply建立连接。无checkpoint的首个window同样受control events/bytes cap，
不得回退整本ledger。Window compaction的attempt/failure/pending lifecycle由增量store维护，source refs使用同一repeatable-read
snapshot返回whole-ledger high-water与exact IDs。Child rollout从child-owned增量state与parent account store读取，不跨ledger full fold。

`RunStartEvent.new_run_boundary.transcript`必须冻结**当前实际采用的durable transcript checkpoint basis**，不得只记录“本次
PRE_RUN是否产生compaction terminal”。因此本次preflight未触发或失败时，新的run仍可引用更早的可读completed checkpoint；
`checkpoint_*`与`preflight_compaction_*`是两组独立归因。Context snapshot collector必须从RunStart冻结的checkpoint恢复，再读取
events/bytes双cap的contiguous authority delta；不得因本次`preflight_compaction_terminal_event_id`为空退回sequence 1。没有任何
checkpoint的bootstrap只能在显式cap内进行，超限fail closed并要求先推进compaction/checkpoint。

`read_raw_range_snapshot()`与`read_raw_run_events()`的`max_events/max_payload_bytes`是physical read contract，不是读取全量后由
caller过滤的提示。PostgreSQL实现必须在SQL层使用indexed predicate与`LIMIT max_events + 1`，并在同一deadline下校验payload bytes；
Context live authority、child parent attribution与legacy preflight compaction不得绕过该边界。
全部production bounded reader必须通过同一process-owned PostgreSQL pool的`bounded_read` lane；EventLog实现中不得直接
`psycopg.connect()`。Compacted-window context使用`read_context_authority_bundle()`在一个repeatable-read snapshot中冻结唯一high-water，
并一起返回primary delta、run-scoped sparse、session-scoped sparse与exact-ID channels。各channel必须共享同一high-water，且hot path不得在
bundle前后另行调用`next_sequence()`或旧range/sparse helper重新冻结边界。
同一run/basis的live authority prefix应保存在session-owned bounded immutable cache中；后续compile只读取cached high-water之后的delta。
Checkpoint/window basis改变时必须换cache key，cache miss/failure不得改变semantic fingerprint。Active observation rollup artifact在rewrite时
write-confirm一次，后续compile只允许bounded verified cache或read-confirm，不得重复put-confirm-get。

generation>1 context window必须使用multi-range authority：primary range从window source through之后开始，RunStart/capability/plan/memory/rollout等
事实通过exact-ID或indexed sparse named ranges加入。Window source document必须携带pairing-safe normalized retained transcript baseline。Provider context
已经compacted后，production compile/replay不得继续复制、解码或fingerprint当前run从RunStart开始的完整semantic prefix。

---

## 3. Sequence

每个 runtime session 内 event sequence 必须连续、单调递增。

Postgres 约束：

- `agent_events.sequence` 非空；
- `(session_id, sequence)` 唯一；
- `PostgresEventLog.extend()` 在一个事务内为 batch 分配 sequence；
- 同一 runtime session 的 append 必须使用 advisory transaction lock。

`event.sequence=None` 表示事件尚未 canonicalized。写入后返回的 event 必须带 canonical sequence。

live `append/extend` 只接受 `sequence=None` 的 event。带 sequence 的 input 必须失败；未来若需要 repair/import，必须提供独立的 offline authority，且不得复用 live EventLog API。

`append/extend` 同时冻结 conditional atomic contract：

- 可选 `expected_last_sequence` 必须在 session advisory transaction lock 内比较；
- mismatch 在任何 insert 前抛 `EventLogWriteConflict`，当前 batch 零写入；
- batch 先完整 materialize / validate，event id 在 batch 内唯一；session 内一个event id只能命名一个immutable payload；
- 一个 batch 获得连续 sequence，不能与其它 `append/extend` 交错；
- validation、serialization、parent-row、insert 或 projection sync 失败时整批 rollback；
- 空 batch 是 no-op。

单事件`append()`同时是精确幂等的commit-confirmation入口：若同一session中已有相同id，且除canonical `sequence`外的完整typed payload一致，返回已存储event，不再分配新sequence；同id不同payload抛`EventIdConflict`。该确认必须先于`expected_last_sequence`冲突判断，使“commit成功但ack丢失”的重试可以确认原事实。`get_by_id()`提供只读确认能力。

`extend()`的atomic batch不能把幂等语义扩成“部分复用、部分新增”。`confirm_batch()`必须在同一个session lock/transaction内返回候选的确认结果与当时的canonical high-water。Runtime writer在不确定batch commit后使用该结果：整批全部存在、payload一致、sequence按原顺序连续时，视为已commit，并至少catch up reducers/publisher到`max(confirmed_batch_end, confirmation_high_water, conditional_conflict.actual_last_sequence)`；只确认到部分batch或sequence不连续时必须latch `ledger_reconciliation_required`并阻止后续mutation。同id payload冲突保持稳定`EventIdConflict`类型，不能降格成普通commit error或吞成成功。

`ledger_reconciliation_required`与单个committed reducer的`reconciliation_required`是两种不同故障。普通reducer rebuild只能清自己的process-local drift，绝不能解除ledger atomicity/sequence结构异常；ledger latch只能通过专门的ledger verify/repair authority解除，V1未提供live repair时必须关闭并重新打开session。

PostgreSQL 是 production EventLog 真源和上述事务/CAS/跨进程语义的权威实现。`InMemoryEventLog` 仅是 pytest test double，只需复现会影响 reducer/writer 单测结论的最小单进程 contract；它不是 product backend，也不能替代 PostgreSQL transaction/restart 验收。

---

## 4. Parent rows

写 event 前必须确保 parent rows 存在：

- session row；
- run row；
- turn row。

少数 run-entry 前置步骤会先持久化 artifact，再原子提交首个 `RunStartEvent`。典型例子是 subagent child 冻结
capability descriptor artifacts。此时 production EventLog 必须先通过
`ensure_runtime_session_owner()`只创建 session owner row；该操作不得伪造 run/turn/event，也不得推进 sequence。
首个 event batch 仍负责原子创建 run/turn rows 与 canonical events。InMemory test double 对此 API 是 no-op。

边界规则：

- run id 已属于另一个 runtime session 时必须失败。
- turn id 已属于另一个 session/run 时必须失败。
- 不允许一个 event 写入导致跨 session/run 混线。

---

## 5. Run projection

`runs` 表是从 canonical events 同步出来的 summary。

同步规则：

- `RUN_START` 将 run 标为 `running`，清空 terminal fields。
- `RUN_END` 将 status / stop_reason / completed_at 写到 `runs`。
- `repair_run_projection()` 可以从 event log 重建 projection。

禁止只改 `runs` 来表达 run 结束、abort 或 recovery。必须先写 canonical `RUN_END`，再同步 projection。

---

## 6. Iteration / replay

`EventLog.iter()` 必须：

- 默认返回当前 runtime session 全部 events；
- 支持按 run / turn / reply 过滤；
- 支持 `after_sequence`；
- 按 sequence 升序返回。

`EventLog.replay(reply_id)` 只重放一个 assistant reply，对应 `ReplyStartEvent` 到 completed content blocks。完整 prior transcript 必须走 transcript reducer 契约，而不是直接拼接所有 reply replay。

---

## 7. Artifacts

Runtime artifact storage 是 event log 的旁路 payload store。

规则：

- event 保存 artifact refs；
- artifact 表保存 text/binary body、digest、size、metadata；
- `tool_result_artifacts` 是 tool result 与 artifact 的可查询索引；
- tool result artifact ref 缺失真实 artifact 是 inspector error；
- context/model replay 不应内联巨大 artifact body，只能用 preview + read_more。

---

## 8. Schema ownership

Versioned PostgreSQL migration registry是runtime truth physical schema的唯一创建/升级入口。`PostgresEventLog` required接收verified connection provider；constructor、append、projection checkpoint与reopen path均不得执行DDL或做grant repair。

它拥有：

- sessions；
- runs；
- turns；
- agent_events；
- artifacts；
- tool_result_artifacts；
- tool_execution_records；
- working_context_summaries。

Memory canonical substrate schema 不属于本契约；见 [MEMORY_SURFACES_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/MEMORY_SURFACES_CONTRACT.zh.md)。

Host在构造EventLog之前必须完成exact-head fast verification。每个direct/pooled/replacement physical connection在可见前重新验证database、role、search path、server与registry prefix。Migration ledger只证明physical schema，不证明历史event JSON payload兼容；event hard cut仍需独立decoder/migration/reset。完整契约见 [POSTGRES_SCHEMA_MIGRATION_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/POSTGRES_SCHEMA_MIGRATION_CONTRACT.zh.md)。

---

## 9. 禁止事项

- 不允许 JSONL transcript 成为生产 truth。
- 不允许 unknown event type 静默通过。
- 不允许跳过 session advisory lock 并发写同一 runtime session。
- 不允许 live caller 提交预编号 event。
- 不允许把 conditional write conflict 当普通异常后盲目重试同一批 events。
- 不允许把 `runs` projection 当 canonical transcript truth。
- 不允许直接更新 projection 表来代替 typed event。
- 不允许跨 session 复用 run/turn id。
- 不允许 inspector 或 resume 读取 live scratchpad 来解释历史。
- 不允许context replay解析tool-result JSON来补造typed execution semantics。
- Production package 中 direct EventLog event-row/checkpoint mutation只允许以下四个
  exact owner：
  `RuntimeSession._commit_reduce_enqueue()`、
  `RuntimeSession._persist_runtime_projection_checkpoint()`、
  `LedgerMaterializationCoordinator._commit_atomic()` 与 privileged
  `verify_or_rebuild_subagent_graph_checkpoint()`。文件后缀、同名函数、receiver alias、
  bound-method/getattr escape均不构成授权；exact AST inventory是架构门控。
- Online compaction、MCP audit、Host/Agent recovery不得直接调用 EventLog adapter，也不得
  在 commit 后扫描 sequence range 再二次发布；它们只能消费
  `RuntimeSession.write_event(s)_with_deadline()` 返回的 exact receipt。
- `ProjectionReadyEvent`必须持久化`projection_kind`；context authority先定位当前run最新`ProjectionRequestedEvent`，再按
  `projection_id + role + scope`确认其后唯一terminal。Ready只能从该event的summary/kind与frozen wrapper
  created-at/sequence重建memory正文/timing；Failed表示本次无memory candidate。terminal缺失、不唯一或最新Failed时不得
  回读live memory state或复用旧Ready。

---

## 10. 测试守护

最低测试门槛：

- typed event dump/load roundtrip。
- unknown event type fails。
- Postgres append assigns contiguous sequence。
- batch extend sequence contiguous。
- conditional expected sequence mismatch produces zero writes。
- concurrent PostgreSQL batches do not interleave。
- PostgreSQL batch failure rolls back every event and projection mutation。
- pytest fake passes the bounded shared CAS/atomic batch contract；数据库事务和恢复只由 PostgreSQL tests 证明。
- run/turn parent rows are created。
- run id cannot belong to two sessions。
- `RUN_START` / `RUN_END` sync run projection。
- projection repair rebuilds stale/missing run status from events。
- `iter()` filters by run/turn/reply/after_sequence。
- `replay(reply_id)` reconstructs assistant message blocks and usage。
- inspector detects sequence gaps and stale run projection。

---

## 11. Run-boundary atomic ledger contract

新 schema 中 `RunStartEvent` 必须且只能携带一种 entry：Host 的 `new_run_boundary` 或 child 的
`subagent_run_entry`；同时 required `current_user_message` 与预生成的 `terminal_run_end_event_id`。Host current user
正文只从 typed carrier replay，不从 metadata fallback。

Host new run 的 `RunStartEvent` 与其首次引用的 pending MCP installation audits 是同一原子 batch。live interaction
resume 的 pending audits、continuation exposure 与 `RunInteractionResumeBoundaryEvent` 也是同一原子 batch，且顺序必须为
audit -> exposure -> boundary。

所有 boundary batch 在调用前生成稳定 event IDs。commit await 遇到任何 `BaseException` 时，原critical writer attempt必须在同一absolute
deadline内完成stable confirmation，并返回typed `FULL | NONE | UNKNOWN` outcome：full进入committed repair/terminalization；none才能安全
重试；partial/不连续确认转换为ledger reconciliation latch与UNKNOWN。caller不得从event loop同步查询PostgreSQL，也不得为confirmation重新分配
deadline。commit后publication failure属于FULL，不是commit failure。

Boundary attempt必须保留完整候选event payload及payload fingerprint，不能只保存ID。confirmation必须区分
`none/full/partial/conflict/unknown`；同ID不同payload是conflict并latch。confirmation自身异常也必须解析Host-owned
attempt completion，禁止stop/close无界等待。

RunStart full commit后，process owner安装与失败terminalizer仍属于同一个commit owner：owner安装失败必须先用RunStart
冻结的terminal ID写RunEnd。Host不得仅凭LoopStatus确认run结束；只有RunEnd durable confirmation才能解析run completion、
清理active pointer并退休execution handles。RunEnd持续失败时session保留owner与stable candidate并fail closed。

`RunEndEvent.id` 必须等于唯一 RunStart 冻结的 terminal ID。Context compaction Started 也必须冻结唯一 terminal ID，
Completed/Failed 与 Started、boundary attribution一一对应。

---

## 12. Versioned raw event rows and checkpoint snapshots

`agent_events` 每行必须持久化非空的 `event_schema_version`、`event_schema_fingerprint` 和
`event_domain_contract_fingerprint`。历史读取必须先构造 `RawStoredEventEnvelope`，验证 wrapper identity、canonical
payload bytes 与三项 schema/domain identity，然后才能绑定 historical decoder。Inspector、replay、checkpoint reducer 不得先调用
当前 `AgentEvent` union。缺失行级 identity、decoder binding 不可用或 domain fingerprint 漂移均 fail closed。

`confirm_batch()` 先比较 candidate 的 exact schema/domain identity 与忽略数据库分配 sequence 后的 canonical raw bytes；
同 ID 不同 identity/payload 是 `EventIdConflict`，不得先用 current union 解码后再猜测兼容。

Checkpoint restore必须使用two-phase bounded read：第一阶段在repeatable-read snapshot中返回catalog metadata，第二阶段只为已选candidate读取一次
exact suffix。两阶段冻结相同requested high-water、reducer contract与selected raw-envelope identity：

- observed ledger high-water；
- confirmed/contract-compatible checkpoint catalog；
- selected candidate从checkpoint through-sequence到requested high-water的contiguous bounded delta；其他candidate不得加载重叠suffix。

第二阶段允许观察到更高的ledger high-water，但不得把requested prefix之后的event混入delta，也不得悄悄更换selected candidate。Checkpoint event参与all-event ledger continuity，但是
subagent graph reducer 的 deterministic no-op，不参与 graph semantic accumulator。

---

## 13. Memory governance lifecycle storage contract

Governance hard cut新增的 durable lifecycle包括 producer candidate-attribution events、
`MemoryCandidateEvidenceRejectedEvent`、`MemoryGovernanceBatchPreparedEvent`、唯一
Completed/Failed/Blocked terminal event，以及 governance `ModelCallStartEvent`上的 required
`GovernanceModelInputAttributionFact`。所有事件必须进入 current AgentEvent union、versioned raw
envelope registry与historical decoder；未知 schema/domain binding fail closed。

Reflection/compaction producer commit使用 EventLog transaction companion，在同一 PostgreSQL
connection/transaction内完成 producer event append、ledger materialization account advance与
`memory_candidate_projection_outbox` insert。Governance Prepared commit同样原子完成 event、claim
`PREPARING -> PREPARED` CAS与preparation locator transition。Terminal commit原子完成 lifecycle
event、全部 claim terminal transition与preparation terminal transition。任何 companion失败必须使
整批 rollback；不得先写 event后补业务 row。

正常 governance preparation禁止 `EventLog.iter()` 或整 run/session扫描。Source attribution使用
带 sequence、event schema fingerprint、payload fingerprint与stored-envelope fingerprint的 exact
refs，并要求每个 sequence不超过 frozen authority high-water。Recovery只允许 bounded exact-ID、
per-call与artifact reads。Prepared artifact confirmed但event未提交时保留 PREPARING owner；
Prepared FULL后 claim和locator必须同时可恢复。

Claim、preparation、projection-outbox与evidence-rejection表属于 durable recovery state，不是可丢
cache。数据库 reset hard cut后，缺表、claim/event payload冲突、同 candidate 双 open owner或
terminal event仍有open claim均 fail closed。

---

## 14. Provider input generation atomic storage contract

`ContextInputManifest` artifact是完整ordered provider projection的唯一durable artifact owner；不得另建独立
projection artifact writer。`ContextCompiledEvent(status="compiled")`必须引用已确认manifest和prepared
provider input，`ModelCallStartEvent`必须携带required committed provider input reference。

初始generation、普通append与rollover使用互斥commit guard。RuntimeSession writer在同一事务中验证predecessor
scope/core/preparation、持久化COW vector artifacts所需引用，并原子提交：

```text
initial:  generation start + append + ModelStart
append:   append + ModelStart
rollover: old close + rollover authority + new start + append + ModelStart
```

事件中的ordered projection ref、causal validation、frame placement、transcript delta proof、resulting frontier、
vector root/prefix fingerprint与ModelStart reference必须逐项join。Accepted continuation还必须绑定exact terminal
projection、disposition、appended ordinal range与range accumulator；只保存“consumed fingerprint”不足以清除
pending owner。

Generation ID覆盖durable scope epoch。Session close后的reopen若创建新generation，identity必须绑定前一closed
generation，不能与旧start event ID碰撞。`NONE`保留同一stable candidate重试，`FULL`按committed reducer fold，
`PARTIAL/UNKNOWN`保留owner并latch。Attribution-only更新不写空append；V1不生产unit-attribution supplement event。

完整projection与per-unit exact refs可以引用多个ledger；aggregate horizon使用content-addressed persistent set root，
每个unit只携带自身相关的exact ledger horizons。selection/query layout fingerprint不得冒充canonical ledger-prefix
proof，也不得把随ledger数量增长的完整horizon tuple重复写入每个ModelStart。

### 14.1 Runtime observation source head与rewrite hard cut

Generation committed core只保存replacement source的semantic head：source/revision/lineage、wire/causal semantic与bounded semantic document identity。Event/artifact refs、authority
horizons、replay binding、append event和vector ordinal只存在于post-commit hydration/placement attribution；两层必须从同一committed append与exact vector unit强join。相同snapshot的
attribution refresh不得写空append或推进semantic revision。

`auxiliary_frame_rebase` event/reason/authority已删除。Runtime-observation收缩只能嵌套在confirmed Long-Horizon rollover atomic batch中；rollover event携带bounded stable state、paged
partition roots、transitive coverage与physical proof，replacement artifacts必须先FULL。`NONE`复用stable candidate，`PARTIAL/UNKNOWN`保留owner并latch。Successor rewrite FULL前旧proof/
projection artifacts保持reachable；durable raw observations不删除、不改写。

### 14.2 Terminal monitor atomic storage contract

Terminal monitor durable vocabulary由registration、observation、termination、receipt application、delivery disposition，以及notification reservation created/released组成。Registration event与注册ToolResult terminal/settlement同批；cancel termination与cancel ToolResult/settlement同批；monitor observation使用`monitor_id + observation_ordinal`稳定ID并严格CAS前后core state。`NONE`重试原candidate，`FULL`才发布notification，`PARTIAL/UNKNOWN`保留physical owner并latch。

Notification account是ledger-derived唯一capacity authority：monitor lifecycle和unmonitored completion process head分别reserve；progress delivery不释放monitor slot，completion/expiry/termination释放monitor slot，最终delivery/receipt/session close释放completion slot并retire head。外层event branch决定transition方向，不另存可漂移的bool/count。所有monitor V1 registration、completion、observation、receipt和Host ingress source refs必须属于同一RuntimeSession ledger。

显式`terminal_process.kill`的ToolResult必须携带exact completion authority；即使`user_tool_kill`不产生model-deliverable notification，terminal ToolResult batch也必须原子写入`explicitly_observed` disposition并释放对应completion process-head reservation。不可交付不等于无需结算。

PostgreSQL `runtime_projection_checkpoints`只保存notification/account与monitor reducer的bounded process projection。它不是第二authority：EventLog event先FULL，projection row可安全滞后；每个row必须绑定exact `through_sequence`处的canonical ledger prefix，并在持有session transaction lock时由EventLog核对continuity accumulator。Row还必须保存前一个已验证projection state/high-water；首次row的base sequence必须为0，且base payload必须逐字段等于该projection kind的canonical genesis。Notification genesis固定runtime identity、8/8容量、reducer contract、空reservation/head/chain集合；monitor genesis固定runtime identity、reducer contract和空current records。不得由caller自报另一套“空状态”。后续row的base必须与PostgreSQL前一row的完整through-sequence/schema/state精确相等。reopen从该base读取唯一bounded typed delta、重放reducer，并要求结果逐字段等于checkpoint state，随后才读取`through_sequence + 1`后的新delta。notification account/reservation/process head与monitor registration/pending/core必须分别强join，两个projection恢复后还必须交叉核对同一active monitor core。仅重算caller自报payload hash不得建立authority。Receipt/disposition所需`ToolResultEndEvent`按exact ID读取；正常reopen禁止`EventLog.iter()`、全历史monitor event或全历史ToolResult扫描。Checkpoint写失败必须使对应committed reducer进入reconciliation，不能假装row已推进。嵌套checkpoint read/write沿用当前event-write绝对deadline，不得为每个projection重新续期。
