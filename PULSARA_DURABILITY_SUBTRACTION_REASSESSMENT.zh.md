# Pulsara durability 边界减法：增量复审与目标架构结论

> 文档性质：只读架构调研、冻结决策与 hard-cut 阶段边界；不是逐文件编码 implementation spec
>
> 复审日期：2026-08-06
>
> Pulsara 当前代码基线：37e21903b9ab24ecfd1974a4019f7b5399de9ceb
>
> 上轮调研基线：0e40febd
>
> 增量提交：37e21903（refactor: hard-cut context input audit manifests）
>
> Claude Code 对照基线：5a774a2b62d7949c1d94e0b726281554d7893cfd
>
> Codex 对照基线：6138909d6ec58b2fbe635ef973e02caecad5a5aa
>
> 本次路线修订：冻结 V1 single Host writer；合并 foreground text/tool/resume/readers authority cut；将 TUI 提升为 Protocol major hard cut；补齐 durable job side-effect safety。
>
> 反向审阅补订：分离 writer/claim fencing domain；冻结 Protocol v3 repeatable-read cut与canonical-row mutation idempotency；区分semantic de-gating与physical quiesce；禁止compaction推进transcript epoch。
>
> 最终反向审阅补订：为非transcript canonical state增加单一`control_revision`唤醒标量；冻结pending interaction为V1 process-local live control；冻结mixed/multi-tool assistant message的原子commit与ordinal lowering。
>
> 终局架构补订：将目标重命名为canonical relational conversation kernel with selective journals；区分semantic context snapshot与rebuildable projection；新增tool/job physical attempt lineage、全局blob publication、interaction subject/secret boundary、四类恢复承诺与Stage 2多PR dormant construction/单次production activation。
>
> 最终主线补订：context采用改为turn-local immutable binding revisions以保留mid-turn compaction；每条accepted provider-generated assistant entry额外归因exact `provider_input_through_sequence`；foreground每logical call最多一physical attempt，retry必须为新turn/new call；minimal job kernel前移到Stage 2单次activation；tool attempt insert纳入`control_revision`与canonical MVCC snapshot。
>
> 调研约束：除新建并修订本文档外，没有修改代码、测试、schema 或 migration；没有 stage、commit、push；没有运行全量 pytest。

## 证据标记

- **[代码确认]**：直接来自当前 HEAD 的生产代码。
- **[探针测量]**：通过仓库根目录 .venv 运行的小型只读路径探针；临时 workspace 位于系统临时目录。
- **[定向测试]**：只运行与本次增量直接相关的 6 个测试，不代表全量回归。
- **[历史意图]**：来自事故文档、架构债务文档、实施计划或 git history；只用于解释动机，不替代代码事实。
- **[推断]**：代码没有直接声明产品语义，但可由调用关系或持久化接口推导；均显式标注。
- **[无法确认]**：本地材料不足以得出可靠结论。

---

## 1. Executive verdict

### 1.1 直接结论

**Pulsara 仍然显著过度 durable。**

新提交修正了一个真实且重要的局部错误边界：它不再把 1 MiB 级的 flat context-input manifest 塞入 ContextCompiled 主事件，而是拆成：

1. 有 64 KiB 上限的 compact semantic commit；
2. 有 8 KiB 上限的 audit expectation；
3. 模型 admission 之外、best-effort 的 audit materialization。

这个方向验证了上轮减法设计的两条核心判断：

- 可重建或诊断性的材料不应成为模型调用的 semantic gate；
- acceleration/audit 失败不应否定已经接受的 canonical fact。

但是，**这次提交不是全局架构减法**。当前主路径的 EventType、EventLog 事务数、committed reducer 数、hard latch 数和 Host close reducer barrier 数均未下降；同时，每次模型调用在容量允许时新增 plan、pages、root 等 durable artifact 写入，并把这个“可选审计”纳入 close drain。新增的 non-Host RuntimeSession teardown 又形成了 retry generation、retry_wait、reconciliation_required 和第二套 11-await teardown surface。

因此本轮的最终判断是：

> 37e21903 是“语义权威边界的局部减法”，但也是“物理 durability 与生命周期 ownership 的局部加法”。它提高了对推荐方向的信心，却没有改变推荐架构。

### 1.2 当前量化快照

| 指标 | 上轮基线 0e40febd | 当前 37e21903 | 判断 |
|---|---:|---:|---|
| EventType vocabulary | 151 | 151 | 未减少 |
| SQL CREATE TABLE | 62（其中 1 个 migration ledger，61 个产品表） | 62（61 个产品表） | 未减少 |
| committed reducer 注册 | 9 | 9 | 未减少 |
| RuntimeSession reconciliation latch | 6 | 6 | 未减少 |
| 精确包含 FULL/NONE/UNKNOWN/CONFLICT 的 enum | 2 | 2 | 未减少 |
| 含 3 个以上 confirmation 类别的 enum family | 6 | 6 | 未减少 |
| text-only EventLog events | 43 | 43 | 未减少 |
| text-only EventLog append transaction | 11 | 11 | 未减少 |
| text-only 自动 audit artifact | 0 | 4，约 61.7 KB | 增加 |
| text-only durable write scope | 约 11 | 至少 15 | 增加 |
| one-tool EventLog events | 83 | 83 | 未减少 |
| one-tool EventLog append transaction | 23 | 23 | 未减少 |
| one-tool 自动 audit artifact | 0 | 8，约 116.8 KB | 增加 |
| one-tool durable write scope | 约 23 | 至少 31 | 增加 |
| text-only durable/process owner family | 至少 13 | 至少 14 | audit owner 增加 |
| one-tool durable/process owner family | 至少 16 | 至少 17 | audit owner 增加 |
| HostSession.aclose await 表达式 | 46 | 45 | 几乎不变 |
| Host close committed-reducer barrier | 4 | 4 | 未减少 |
| non-Host RuntimeSession teardown | 无独立统一 surface | 11-await surface | 增加 |

计数代码依据：

- EventType 为 151 个成员：[events.py](src/pulsara_agent/event/events.py#L298)。
- 9 个 reducer 注册点：[session.py](src/pulsara_agent/runtime/session.py#L838)、[session.py](src/pulsara_agent/runtime/session.py#L903)、[session.py](src/pulsara_agent/runtime/session.py#L957)、[session.py](src/pulsara_agent/runtime/session.py#L1009)、[session.py](src/pulsara_agent/runtime/session.py#L1053)、[session.py](src/pulsara_agent/runtime/session.py#L1127)、[session.py](src/pulsara_agent/runtime/session.py#L1171)、[session.py](src/pulsara_agent/runtime/session.py#L1254)、[session.py](src/pulsara_agent/runtime/session.py#L1330)。
- 6 个 latch 聚合为 reconciliation_required：[session.py](src/pulsara_agent/runtime/session.py#L526)、[session.py](src/pulsara_agent/runtime/session.py#L2387)。
- Host close 主体为 4992–5185，共 45 个 await，包含 4 次 drain_open_committed_reducer_barrier：[host/session.py](src/pulsara_agent/host/session.py#L4992)。
- non-Host teardown 为 6719–6786，共 11 个 await：[runtime/session.py](src/pulsara_agent/runtime/session.py#L6719)。

### 1.3 过度设计最集中的边界

1. **foreground reply 被建模成跨进程事务恢复。**
   ModelStart、stream segments、terminal projection、control disposition、ReplyEnd、RunEnd 各自拥有 durable fact、确认和恢复分支，即使产品完全可以把 crash 解释成 interruption。

2. **derivation 被升级为 authority。**
   transcript projection、tool terminal projection、provider-input generation、terminal notification/monitor projection、presentation、checkpoint head 等，大多能由更基础 transcript/EventLog 事实重建，却参与主线 gate、repair 或 close。

3. **写入确认再次被 durable 化。**
   stable candidate、reservation、account、receipt、head、fingerprint 和 FULL/NONE/UNKNOWN/CONFLICT family 用来证明另一个 durable fact；确认过程失败后再创建 repair owner。

4. **process-local lifecycle 被要求具备 crash-recovery 形状。**
   model stream、tool execution、child RuntimeSession、temporary resume RuntimeSession 都拥有 generation、retry、reconciliation 和 close lineage。

5. **session close 被当作全图 terminalization coordinator。**
   close 不只是停止 ingress、取消 foreground task、flush canonical transcript，而是试图收敛 terminal monitor、tool terminal owner、governance、compaction、provider input、reducer repair、checkpoint 和 presentation。

### 1.4 最值得先删除的三类机制

1. **foreground model/reply 的 exact recovery 链**
   删除 ModelStart/ModelEnd/terminal projection/control disposition/recovered ReplyEnd 等跨进程 continuation 语义；crash 后只生成 coarse interrupted turn。

2. **可重建 projection 的 semantic gate、checkpoint repair 与 committed-reducer repair 链**
   checkpoint 失败只影响 reopen 性能，不能阻止 final reply 或 RunEnd；先移除读取方和 gate，再删除 owner。

3. **每次模型调用自动生成并永久保留的 context-input audit artifact plane**
   保留 compact semantic commit 的边界经验；audit 改为显式 doctor、采样或有产品承诺的后台 job，并从 Host close 的成功/追平型 semantic wait 移除；owner尚存期间仍需bounded physical quiesce。

### 1.5 必须保留的 durability

- 已接受的 user input；
- 唯一 accepted final assistant reply；
- 已对模型或用户公开的完整 assistant tool-request message，包括mixed text与全部有序tool calls；
- physical dispatch前已经commit的tool execution attempt；
- 已返回的 tool result；
- 外部 side effect 的最小、可审计、不可静默重试记录；
- turn/session 的 completed 或 interrupted 粗粒度状态；
- durable prompt queue 中已接受的 item；
- 真正跨 Host 生命周期继续运行的 terminal monitor job；
- 已被turn-local immutable binding revision引用的versioned long-horizon context snapshot，以及memory extraction job；
- durable job intent、每次physical attempt、remote identity与retry lineage；
- subagent 的 durable task/message/result boundary，而不是其 coroutine/executor 状态；
- memory governance 中有长期产品价值的用户/模型事实；
- 必须跨设备、跨进程恢复的 session metadata；
- 由一个全局content-addressed blob contract发布、被canonical row以外键引用的大内容。

### 1.6 唯一推荐

**继续推荐“中等 hard cut”，但最终定位收紧为：canonical relational conversation kernel + selective immutable journals。**

这不是Event Sourcing，也不是把所有事实压进一条transcript。它使用直接关系型schema保存conversation facts，用窄attempt journal保存真实physical effect lineage；execution coroutine、consumer proof与derived delivery仍留在进程内。

它不是功能最少的方案，而是复杂度/产品价值比最好的方案：

- 保留 Pulsara 的 long-horizon、subagent、terminal monitor、durable prompt queue、memory governance 和 resumable Host session；
- 不再承诺 foreground execution state 的跨进程 exact continuation；
- 对外部 side effect 使用tool-call intent、dispatch-before committed attempt、result-after-return三段事实；只有attempt存在而result缺失才是outcome_unknown，call存在但attempt不存在可证明未dispatch；
- final assistant reply 只有一个数据库 commit point；
- open执行conversation rehydrate，再开启新turn；不做execution replay；
- V1 冻结为每个 session 同时只有一个 Host writer；PostgreSQL writer generation/lease只fence Host-owned foreground与session-control mutation，旧 generation一律fail closed；
- background worker完全独立于Host writer generation，只以job attempt的`claim_generation`提交progress/result；它不得直接追加session transcript，当前Host需要用自己的writer generation显式接受job result；
- 第一次生产 authority 切换必须同时覆盖 user、assistant、tool call/attempt/result、context binding revisions、interrupted/unknown、最小 open/resume、TUI/Inspector/context 读取方与minimal job kernel；不得按“模型最后是否调用 tool”拆成两个阶段，也不得让foreground-reachable background work留在旧authority；
- TUI 同步执行 Protocol major hard cut，从 Presentation Foundation 的 root/cursor/page 权威切到 transcript snapshot/page/sequence；canonical snapshot/page使用同一MVCC read cut，并保留由canonical target row实现的最小command idempotency；不保留在线兼容层或通用receipt graph；
- `sessions.control_revision`作为唯一canonical control wake-up high-water；Host-owned、用户可见且可能不追加transcript的control mutation在原transaction内递增，包括tool attempt insert与public remote-identity update；Observe同时比较entry sequence与control revision；不保留transition history或per-section cursor；
- V1 pending approval/plan/MCP input request是同一Host内可level-read的process-local live control，不进入canonical snapshot；Host crash/takeover后request消失、turn interrupted，只有accepted decision进入`interaction_decisions`；
- 一个completed assistant tool-request message的text与全部calls原子commit后才允许任何invoke；每logical call最多一foreground physical attempt，retry必须在新turn中生成new call；results可并行、分别commit，但follow-up model必须等全部call terminal，并按原call ordinal lowering；
- durable job拆成aggregate job与immutable attempt lineage；lease过期不等于可以重做：只有显式 retry-safe handler能创建下一attempt自动重执行；可查询远端状态的handler只能重新观察；非幂等handler丢失lease后当前attempt进入outcome_unknown；
- compaction在V1只新增immutable context snapshot/binding revision，不删除、重写、重排transcript，也不改变transcript epoch；turn可在provider safe point换用新revision以保留mid-turn budget recovery，每条accepted assistant message引用exact revision并保存本次pre-dispatch conversation cut的`provider_input_through_sequence`；未采用snapshot可按retention删除，被revision采用后不能重新生成冒充；
- 所有大内容通过唯一blob publication contract发布；prompt、tool result、job、context snapshot与memory不各自维护hold/receipt/confirmation图；
- close最终压缩为3个阶段；在旧Foundation/owner仍存在的过渡阶段，只删除semantic completion wait，仍需bounded physical cancel/join后才能释放session-owned资源；
- PostgreSQL 仍是产品事实 authority，但不再由通用 agent_events 充当所有 subsystem 的 authority。

本次修订对原路线主线阻塞的最终处置为：

| 原阻塞 | 冻结后的处理 | 不允许的退路 |
|---|---|---|
| turn开始时不知道是否会call tool | 全部foreground item一次authority cut | text新schema/tool旧EventLog分流 |
| 新schema先于resume/open | 第一个新row与最小transcript-only open同release | 让旧resume修新running turn |
| TUI仍依赖root/cursor/page/GAP | Protocol v3 + Python transcript service + Go sequence cache同步cut | 先删Foundation、以后再迁TUI；在线v2 shim |
| session writer语义未定 | V1单Host writer + DB generation/lease fencing | 用fingerprint/CAS暗中容忍multiwriter |
| job lease过期可能复制effect | retry-safe重做；queryable只观察；non-idempotent unknown | 所有expired lease自动回pending |
| Host writer与job worker generation交叉 | writer与claim是两个独立fencing domain；job result由Host显式接受进transcript | worker result绑定Host generation或直接写transcript |
| v3 snapshot没有一致read cut | canonical snapshot/page绑定一个repeatable-read MVCC cut与明确sequence upper bound | 多次read拼成表面一致的snapshot |
| v3 mutation ACK丢失会重复submit | canonical turn/queue/accepted-interaction-decision row保存session-wide command id；query直接读target row | 重建terminal command receipt状态机 |
| Stage 1 de-gate等同于不join physical I/O | 只取消成功/追平等待；owner存在时仍stop admission并bounded cancel/join | pool/artifact/session资源先于operation释放 |
| compaction与transcript epoch混淆 | compaction只追加context snapshot；epoch只随reset或显式retention变化 | compaction删除/重写canonical transcript |
| 非transcript canonical control变化不会推进entry sequence | session只保留一个`control_revision`；Observe比较sequence + revision | per-section cursor/history或只靠可丢edge hint |
| pending interaction既未持久化又被称为canonical snapshot state | V1 request是process-local live control；只持久化accepted decision | 暗中恢复suspended interaction owner |
| multi-tool message仍按单call durable unit描述 | mixed text + 全部calls作为一个assistant message原子commit；result按call精确配对 | 为守住固定4次transaction逐call先写先执行 |
| compaction被同时称为durable truth与可删除cache | 未被binding revision引用的snapshot可GC；已引用的summary/source/compiler/model contract为immutable semantic artifact | 删除后重新生成不同summary并冒充连续性 |
| 单turn context binding会删除mid-turn compaction | turn-local immutable binding revisions；只在safe point推进current pointer；assistant output绑定exact revision + per-call conversation cut | 同turn永久锁定初始snapshot或恢复ModelStart lifecycle |
| tool call与physical effect混成一条事实 | assistant call表达intent；`tool_execution_attempts`在dispatch前commit；result引用exact attempt | call存在就推断已dispatch，或无result一律unknown |
| 同call多attempt与唯一tool result冲突 | foreground每call最多一attempt；retry是new turn/new call，attempt状态由row/result/turn派生 | 覆盖旧attempt、丢弃physical outcome或新增per-attempt observation graph |
| job row覆盖多次真实执行 | `durable_jobs`保存intent/aggregate；`durable_job_attempts`保存claim、remote identity、result与retry lineage | mutable attempt summary或JSON覆盖旧effect lineage |
| Stage 2与Stage 4之间的job authority空窗 | minimal job schema/claim/result-accept与foreground-reachable handlers在Stage 2激活；Stage 4只收口剩余disabled handlers并删旧graph | 旧job到新conversation bridge或默认丢失background能力 |
| tool attempt不推进control high-water | attempt insert在同transaction递增`control_revision`；snapshot在同一MVCC cut读取attempt/result/turn | 只靠可丢notification或永久显示not-dispatched |
| binding revision不能证明某次model call看到了哪个delta cut | 每条provider-generated assistant保存exact revision + `provider_input_through_sequence`；entry sequence按commit顺序分配且不可预留 | 用assistant自身sequence或共享revision推断result是否参与历史input |
| 每个domain各造artifact hold/proof | 全局content-addressed blob publication + canonical FK + orphan grace GC | queue/tool/job/context各自复制preparation owner |
| “replay”混合四种不同承诺 | conversation rehydrate、context rematerialization、effect reconciliation、audit reproduction分别冻结；execution replay不支持 | 用历史decoder暗示coroutine可恢复 |

最近五轮反向审阅通过收紧transaction、read cut、fencing、live-control、physical attempt、semantic context、per-call provider conversation cut、job activation、blob与schema-evolution边界闭环，没有新增stable candidate、receipt、checkpoint、repair owner或兼容projection。新增的`control_revision`只是sessions row上的当前高水位，不保存transition history，也不成为新的resume owner；新增attempt row保存的是physical effect这一不可替代的产品事实，不是executor transition graph；`provider_input_through_sequence`只是accepted assistant row上的标量归因，不恢复ModelStart/ModelEnd或provider lifecycle journal。

---

### 1.7 调研范围、增量与验证方法

#### 1.7.1 仓库状态

**Pulsara**

- 当前 HEAD：37e21903。
- 相对上轮 0e40febd 只有 1 个新 commit。
- diff：77 files，+9,900 / -6,435。
- src/pulsara_agent：+6,150 / -4,687，净增 1,463 行。
- 当前工作树在新建本文档前为 clean。

**Claude Code**

- 当前 HEAD：5a774a2。
- 相比上轮复核没有代码变化。
- 工作树只有用户已有的未跟踪 .DS_Store，本次未触碰。
- 该仓库 README 明确声明其 src 来自 2026-03-31 泄漏的 source map，因此它是可读代码快照，不是可验证的官方发布仓库：[README.md](../claude-code/README.md#L3)。本文把直接可读实现标为“代码确认”，但不把仓库 provenance 外推为官方承诺。

**Codex**

- 当前 HEAD：6138909d6e。
- 相比上轮复核没有代码变化。
- 工作树 clean。
- 代码完整可追踪，作为主要成熟产品对照。

#### 1.7.2 增量提交规模

37e21903 的主要生产变更：

- 删除旧 flat manifest：
  - runtime/context_input/manifest.py：删除 1,087 行。
- 新增 compact commit：
  - [context_input_commit.py](src/pulsara_agent/primitives/context_input_commit.py#L1)：296 行。
  - [commit.py](src/pulsara_agent/runtime/context_input/commit.py#L1)：471 行。
- 新增 audit plane：
  - [context_input_audit_storage.py](src/pulsara_agent/primitives/context_input_audit_storage.py#L1)：397 行。
  - [audit_storage.py](src/pulsara_agent/runtime/context_input/audit_storage.py#L1)：418 行。
  - [audit_materializer.py](src/pulsara_agent/runtime/context_input/audit_materializer.py#L1)：1,282 行。
  - [audit_gc.py](src/pulsara_agent/runtime/context_input/audit_gc.py#L1)：340 行。
  - [audit_doctor.py](src/pulsara_agent/runtime/context_input/audit_doctor.py#L1)：177 行。
- 新增 non-Host teardown port：
  - [runtime_session_teardown.py](src/pulsara_agent/ports/runtime_session_teardown.py#L1)：82 行。

五个 audit plane 文件共 2,614 行；compact commit 两个文件共 767 行；加 teardown port 后，这 8 个新核心文件共 3,463 行。context-input 整体 slice 相对上轮净增约 1,193 行；subagent/session/resume/teardown slice 净增 508 行。

这不是“行数多所以设计错误”的论证。行数只用于回答：本次是否发生了可观测的架构减法。答案是：旧的大对象被删除，但生产代码总量和 owner surface 没有净减。

#### 1.7.3 只读探针

探针使用：

- 仓库根目录 .venv/bin/python；
- tests/support 的真实 in_memory_runtime_session；
- AgentRuntime、ScriptedTransport 和真实 event writer；
- text-only reply；
- 一个明确标记 read-only、concurrency-safe 的自定义 tool；
- 等待 context_input_io_service 的 audit operation 物理退出后计数；
- 不创建或修改仓库文件。

测得：

| 路径 | EventLog event | EventLog transaction | batch sizes | ContextCompiled bytes | audit artifact |
|---|---:|---:|---|---|---|
| text-only | 43 | 11 | 6/3/3/3/2/3/6/4/5/3/5 | 14,293 | 4 个，61,702 bytes |
| one-tool | 83 | 23 | 6/3/3/3/2/3/6/4/5/3/3/2/2/4/3/3/3/3/5/4/5/3/5 | 14,285；14,436 | 8 个，116,820 bytes |

由于 artifact 中包含运行身份和完整 source component，具体字节数会随输入略变；对象数和写入拓扑是稳定结论。

#### 1.7.4 定向测试

只运行以下 6 个测试：

- compact commit / expectation physical bounds；
- plan-first、root-last materialization；
- bounded best-effort audit lane；
- provider replay 与成功 run 不依赖 optional audit；
- audit operational failure 不导致 live run 失败；
- audit plane 不得 latch 或 fail live runtime。

结果：**6 passed in 1.30s**。

测试证明新局部边界按当前意图工作；它们不证明“每次模型调用都应写 audit artifact”，也不证明全局 durability 设计正确。

---

### 1.8 新提交 37e21903 的架构复审

#### 1.8.1 做对了什么

##### 1.8.1.1 删除 flat manifest 主事件

旧设计把编译输入的大量材料直接放入 ContextCompiled。新设计把 compiled branch 限制为三个 compact carrier：

- semantic_commit；
- provider_input_preparation_install；
- audit_expectation。

代码：[events.py](src/pulsara_agent/event/events.py#L1308)。完整 ContextCompiled candidate 还有 256 KiB hard bound：[events.py](src/pulsara_agent/event/events.py#L1453)。

compact semantic commit 的 canonical bytes 上限为 64 KiB：[context_input_commit.py](src/pulsara_agent/primitives/context_input_commit.py#L38)、[context_input_commit.py](src/pulsara_agent/primitives/context_input_commit.py#L132)、[context_input_commit.py](src/pulsara_agent/primitives/context_input_commit.py#L182)。

audit expectation 上限为 8 KiB：[context_input_commit.py](src/pulsara_agent/primitives/context_input_commit.py#L192)、[context_input_commit.py](src/pulsara_agent/primitives/context_input_commit.py#L238)。

这解决的是一个真实边界：semantic event 不应随可展开诊断载荷无限增长。

##### 1.8.1.2 audit 不再阻塞 model admission

ModelCallStart 已提交并安装 live cursor 后，LLM runtime 才尝试 offer audit；整个 offer 被 try/except 包裹，失败被明确视为可重建或 unavailable：[llm/runtime.py](src/pulsara_agent/llm/runtime.py#L390)、[llm/runtime.py](src/pulsara_agent/llm/runtime.py#L404)、[llm/runtime.py](src/pulsara_agent/llm/runtime.py#L437)、[llm/runtime.py](src/pulsara_agent/llm/runtime.py#L449)。

ContextInputIoService 的接口名和行为也明确是 nowait：

- 只允许一个 session audit operation；
- session capacity 满时 typed skip；
- process resident permit 不足时 typed skip；
- 使用独立 best-effort executor；
- caller 不等待资源。

代码：[io_service.py](src/pulsara_agent/runtime/context_input/io_service.py#L145)、[io_service.py](src/pulsara_agent/runtime/context_input/io_service.py#L161)。

materializer 的结果是 MATERIALIZED、SKIPPED_SOURCE_CAPTURE、SKIPPED_PHYSICAL_BOUND、FAILED_OPERATIONALLY，而不是 Runtime reconciliation latch：[audit_materializer.py](src/pulsara_agent/runtime/context_input/audit_materializer.py#L70)。

##### 1.8.1.3 loader 接受 exact、reconstructed、unavailable

load_context_input_audit 先尝试 exact root/plan/pages；artifact 缺失、storage unavailable 或 deadline 失败时，可以从 canonical provider payload 重建 semantic view；只有调用方显式 require_exact 才报错：[replay.py](src/pulsara_agent/runtime/context_input/replay.py#L491)、[replay.py](src/pulsara_agent/runtime/context_input/replay.py#L637)、[replay.py](src/pulsara_agent/runtime/context_input/replay.py#L647)、[replay.py](src/pulsara_agent/runtime/context_input/replay.py#L679)。

这是本次最值得保留的设计经验：**诊断精度可以降级，canonical execution 不应降级。**

##### 1.8.1.4 没有新增 SQL table 或 durable audit job

本次 schema catalog generation 从 9 调整为 11，但 SQL migration 没有新增表；当前仍为 62 个 CREATE TABLE：[serialization.py](src/pulsara_agent/event_log/serialization.py#L28)。

计划文档也明确写明不增加 compatibility shim、audit durable job、manifest repair owner 或新 DB table：[PULSARA_CONTEXT_INPUT_MANIFEST_REFERENCE_PAGING_HARD_CUT_PLAN.zh.md](PULSARA_CONTEXT_INPUT_MANIFEST_REFERENCE_PAGING_HARD_CUT_PLAN.zh.md#L7)。这是意图证据；实际代码也没有专用 durable audit job。

#### 1.8.2 没有做减法、甚至放大的部分

##### 1.8.2.1 每次模型调用新增 durable artifact fan-out

materializer 的物理顺序是：

> plan put
>
> → N 个 page put
>
> → 对每个 page 做 exact read-back
> → root put

代码：[audit_materializer.py](src/pulsara_agent/runtime/context_input/audit_materializer.py#L1138)。

本次简单 text reply 生成：

- 1 个 plan：39,697 bytes；
- 2 个 pages：9,902 + 10,246 bytes；
- 1 个 root：1,857 bytes；
- 合计 4 个对象，61,702 bytes。

one-tool 有两次 model call，生成：

- 2 个 plan；
- 4 个 pages；
- 2 个 root；
- 合计 8 个对象，116,820 bytes。

PostgreSQL deterministic put 每次都获取自己的 artifact connection scope、执行 lock/insert/confirm：[postgres_archive.py](src/pulsara_agent/memory/artifacts/postgres_archive.py#L176)、[postgres_archive.py](src/pulsara_agent/memory/artifacts/postgres_archive.py#L196)、[postgres_archive.py](src/pulsara_agent/memory/artifacts/postgres_archive.py#L338)。

因此：

- foreground latency 未必增加，因为 offer 是异步的；
- 但每 turn 的 durable write、storage、connection checkout、read-back 和 close work 确实增加；
- text-only 的 durable object 从 43 个 event 变成 47 个 event/artifact；
- one-tool 从 83 变成 91；
- 如果按独立 durable write scope 计，分别至少为 15 和 31。

这就是 durability amplification：为了保留诊断详情，又引入 plan、page、root、deterministic confirmation 和 GC。

##### 1.8.2.2 completed audit 当前永久保留

ResolvedContextInputAuditMaintenancePolicy 把 completed_root_retention 固定为 retained，其他值直接 validation failure：[audit_gc.py](src/pulsara_agent/runtime/context_input/audit_gc.py#L31)、[audit_gc.py](src/pulsara_agent/runtime/context_input/audit_gc.py#L48)。

GC 只删除没有 completion root 的旧 plan-owned pages，并要求：

- session close confirmed；
- run owners drained；
- context input I/O drained；
- materialization account 没有 active barrier/reservation。

代码：[audit_gc.py](src/pulsara_agent/runtime/context_input/audit_gc.py#L57)、[audit_gc.py](src/pulsara_agent/runtime/context_input/audit_gc.py#L148)、[audit_gc.py](src/pulsara_agent/runtime/context_input/audit_gc.py#L180)。

所以正常成功的每次模型调用都留下长期 artifact；GC 本身又依赖 close 与 materialization account。

##### 1.8.2.3 optional audit 仍然是物理 close gate

drain_pending 把 _audit_operation 加入必须完成的 tasks；deadline 到达会抛 PendingContextInputIoError：[io_service.py](src/pulsara_agent/runtime/context_input/io_service.py#L359)。

Host close 明确等待该 drain：[host/session.py](src/pulsara_agent/host/session.py#L5111)。non-Host teardown 也等待它：[runtime/session.py](src/pulsara_agent/runtime/session.py#L6769)。

因此代码中的“optional”只表示“不阻塞 model admission”，不表示“不阻塞 Host teardown”。这是边界不一致：

- 如果 audit 真是 operational-only，close应取消其业务完成要求；仍在使用session资源的operation必须cancel并bounded join，只有先隔离全部资源访问能力后才可abandon；
- plan-first/root-last 已经能让不完整写入被 GC；
- 不应为了诊断 artifact 的物理退出让用户的 session close 失败。

##### 1.8.2.4 non-Host teardown 形成新 lineage

新 port 把 purpose 分为 RESUME_RECOVERY 和 CHILD_TERMINAL，并定义 retryable 与 reconciliation-required 两类错误：[runtime_session_teardown.py](src/pulsara_agent/ports/runtime_session_teardown.py#L10)、[runtime_session_teardown.py](src/pulsara_agent/ports/runtime_session_teardown.py#L15)。

child lease 保存：

- active / closing / retry_wait / closed / reconciliation_required；
- physical_teardown_generation；
- physical_teardown_task；
- failure_code。

代码：[subagent/execution.py](src/pulsara_agent/runtime/subagent/execution.py#L98)。

teardown lineage 最多做 3 次 physical attempt；取消、deadline exhausted 或未知异常都可转 reconciliation_required：[subagent/execution.py](src/pulsara_agent/runtime/subagent/execution.py#L610)、[subagent/execution.py](src/pulsara_agent/runtime/subagent/execution.py#L657)、[subagent/execution.py](src/pulsara_agent/runtime/subagent/execution.py#L721)、[subagent/execution.py](src/pulsara_agent/runtime/subagent/execution.py#L749)。

RuntimeSession.teardown_non_host_runtime_session 本身依次等待 provider input、audit/context I/O、reducer、checkpoint、compaction、subagent、transcript、prompt queue、presentation 等 11 个 await：[runtime/session.py](src/pulsara_agent/runtime/session.py#L6719)。

它没有新增数据库状态机，但它重现了同一 amplification 形状：

> child session 需要关闭
>
> → 新建 purpose capability
>
> → 新建 physical lineage owner
>
> → 新建 retry generation
>
> → 新建 retry_wait / reconciliation state
> → parent release 又依赖 lineage 收敛

在推荐的 crash = interruption 语义下，foreground child executor 不需要跨进程精确 terminalization；process-local close 只需 bounded cancel/join，失败时把 child 标成 interrupted。

#### 1.8.3 对新提交的最终处置建议

**保留：**

- 不再恢复 flat manifest；
- ContextCompiled 的 compact bound；
- semantic authority 与 optional diagnostic 的分离；
- loader 的 exact/reconstructed/unavailable 降级模型；
- audit failure 不 latch live runtime 的规则。

**短期降级：**

- audit 从“每次 model call 自动生成”改成显式 doctor、采样或 session opt-in；
- completed artifact 增加明确 TTL/retention product policy；
- close到达时允许放弃audit成功/materialized语义；仍使用session资源的operation先cancel并bounded join，或先被彻底隔离资源访问后才abandon。

**中等 hard cut 最终删除：**

- ContextInputAuditExpectationFact 从 foreground semantic event 中移出；
- llm/runtime.py 的自动 offer；
- ContextInputIoService 的audit slot和业务完成型close drain dependency；过渡期physical quiesce随owner保留到owner删除；
- audit_materializer/audit_storage/audit_gc/audit_doctor 这套 2,614 行 plane，除非产品明确承诺逐次 exact input audit；
- child teardown retry/reconciliation lineage，改成 process-local bounded interruption。

**不要删除的正确部分：**

- compact commit 不能被旧 flat manifest 替回；
- 如果过渡期仍保留 EventLog/provider exact replay，ContextCompileInputCommitFact 和 ProviderInputPreparationInstallFact 可暂时保留；
- 当 resume 改成 transcript-only 后，再评估 compact compiler commit 是否还有产品价值；不能因为它当前已经实现就默认永久保留。

---

## 2. Current-state truth map

本节描述当前代码实际拥有的 reply、tool、finalization 和 reopen，不把设计文档中的目标状态当成已实现状态。

### 2.1 当前总图

当前 foreground turn 不是“写 user message，执行，写 assistant message”三段，而是下图中的多权威流水线：

~~~text
accepted ingress
  -> RunStart + window/account/materialization genesis
  -> provider-input generation/append
  -> ContextCompiled + projection request/ready
  -> ReplyStart + ModelCallStart + rollout/physical reservations
  -> durable model stream blocks/segments
  -> terminal projection artifact + committed reference
  -> ModelCallEnd + reservation settlement + ReplyEnd
  -> control disposition + execution permit
  -> committed transcript/authority reducers
  -> optional tool result terminal projection
  -> optional follow-up model call
  -> context window/account close + RunEnd
  -> final-output materialization
  -> terminal presentation / TUI delivery
~~~

代码事实：

- RunStart 自身携带 user message、permission snapshot、model target、long-horizon contract、transcript seed、terminal RunEnd stable id 和 run-entry boundary，而不只是“turn started”标志：[run_entry.py](src/pulsara_agent/runtime/run_entry.py#L318)、[events.py](src/pulsara_agent/event/events.py#L536)。
- Model lifecycle Start batch 同时容纳 ReplyStart、rollout reservation、provider-input companions 和 ModelCallStart：[lifecycle.py](src/pulsara_agent/llm/lifecycle.py#L180)、[runtime.py](src/pulsara_agent/llm/runtime.py#L345)。
- provider stream 的 text/tool singleton 和 segment 会变成 durable AgentEvent，而不只是 live UI delta：[segment.py](src/pulsara_agent/llm/segment.py#L620)。
- terminal batch 包含 terminal projection committed event、ModelCallEnd、可选 provider generation close、rollout settlement、ReplyEnd：[runtime.py](src/pulsara_agent/llm/runtime.py#L1144)。
- ModelCallControlDispositionResolvedEvent 再把 ModelCallStart/End、result fingerprint、activation 和 termination intent 连接起来，并生成 permit：[control.py](src/pulsara_agent/llm/control.py#L60)。
- RunEnd 有 terminalization matrix validation，不是简单 turn status：[events.py](src/pulsara_agent/event/events.py#L783)。

### 2.2 普通 text-only turn

#### 2.2.1 实测 durable event

最小成功 text-only turn 产生 43 个 EventLog event：

| event family | 数量 | 业务含义 |
|---|---:|---|
| RUN_START / RUN_END | 2 | run 边界 |
| REPLY_START / REPLY_END | 2 | reply 生命周期 |
| MODEL_CALL_START / END | 2 | model call 生命周期 |
| MODEL_CALL_TERMINAL_PROJECTION_COMMITTED | 1 | 完整 model terminal document 的 durable reference |
| MODEL_CALL_CONTROL_DISPOSITION_RESOLVED | 1 | terminal result 到下一控制动作的 durable disposition |
| PROVIDER_INPUT_GENERATION_STARTED / APPEND_COMMITTED | 2 | provider input owner |
| CONTEXT_COMPILED | 1 | compiled context compact commit 与 audit expectation |
| CONTEXT_WINDOW_OPENED / CLOSED | 2 | long-horizon window |
| PROJECTION_REQUESTED / READY | 2 | context projection |
| TEXT_BLOCK_START / SEGMENT / END | 3 | 模型输出布局与文本 |
| ROLLOUT_BUDGET_ACCOUNT_OPENED / CLOSED | 2 | turn budget account |
| ROLLOUT_BUDGET_RESERVATION_CREATED / SETTLED | 2 | model call budget reservation |
| PHYSICAL_OPERATION_RESERVATION_CREATED / SETTLED | 14 | 7 对 physical operation accounting |
| PHYSICAL_OPERATION_CHARGE_APPLIED | 1 | physical charge |
| LEDGER_MATERIALIZATION_ACCOUNT_GENESIS | 1 | ledger materialization account |
| LEDGER_MATERIALIZATION_CONSUMER_REGISTERED | 2 | materialization consumers |
| LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED | 1 | consumer horizon |
| SUBAGENT_GRAPH_CHECKPOINT_COMMITTED | 1 | 即使没有 subagent 也写 graph checkpoint |
| CAPABILITY_EXPOSURE_RESOLVED | 1 | capability exposure |
| **合计** | **43** | **11 个 EventLog transaction** |

Event vocabulary 定义入口为 [events.py](src/pulsara_agent/event/events.py#L298)；上述数量来自当前 HEAD 的真实 AgentRuntime 探针，不是静态猜测。

新提交还为唯一一次 model call 自动写 4 个 artifact：

| artifact | 数量 | 本次样本字节 |
|---|---:|---:|
| audit plan | 1 | 39,697 |
| audit pages | 2 | 20,148 |
| completion root | 1 | 1,857 |
| **合计** | **4** | **61,702** |

因此当前最小 text reply 的物理下界是：

- 43 个 EventLog rows；
- 4 个 audit artifact；
- 11 次 EventLog append transaction；
- 至少 4 次独立 artifact put；
- 合计至少 47 个 durable object、15 个 durable write scope。

“至少”是因为 projection document、final-output artifact、checkpoint CAS 和数据库内部 outbox 是否发生，取决于 composition 与阈值；这里没有把无法由本探针稳定观测的写入硬算进去。

#### 2.2.2 逐阶段 ownership

| 阶段 | durable truth / candidate | derived projection / checkpoint | process-local owner | repair / restart | close dependency |
|---|---|---|---|---|---|
| user submit | RunStart，内含 current_user_message 与 transcript seed | runs/transcript/materialization projection | RunOwner、run activation | resume 查 running run | ingress close、active run drain |
| provider input | ProviderInputGenerationStarted、ProviderInputAppendCommitted、ContextCompiled | provider-input generation store、projection request/ready | generation coordinator、context compiler、I/O service | reopen generation recovery | provider producer quiesce、context I/O drain |
| ModelStart | ReplyStart、ModelCallStart、rollout与physical reservations | live semantic cursor | model stream execution registry | model stream recovery | model stream registry drain |
| stream | TextBlockStart/Segment/End | terminal projection reducer/document | segment coalescer、transport owner | incomplete stream recovery | physical transport exit |
| terminal projection | committed projection reference、ModelCallEnd、ReplyEnd、settlements | artifact document、checkpoint head | terminal projection reducer | same-candidate terminal retry | reducer repair、checkpoint maintenance |
| control | durable disposition | execution permit / publication | disposition owner | missing disposition recovery | pending candidate must be zero |
| transcript acceptance | EventLog + transcript reducer high-water | transcript projection/checkpoint | committed reducer registry | committed reducer repair + post-fold | 4 reducer barriers、checkpoint drains |
| finalization | ContextWindowClosed、account closed、RunEnd | final output / presentation | stable RunFinalizationOwner | repair-driven same candidate retry | run-finalization drain |
| TUI delivery | terminal presentation / queue state | rendered snapshot | application services | publication maintenance | command、queue、presentation drains |

关键证据：

- 9 个 committed reducer 注册覆盖 transcript、provider input、prompt queue、tool terminal、MCP、long horizon、authority materialization shadow、terminal notification、terminal monitor：[session.py](src/pulsara_agent/runtime/session.py#L838)、[session.py](src/pulsara_agent/runtime/session.py#L1330)。
- terminal projection 不只存文本；它构建 semantic join、document fingerprint、artifact id、sha256 和 byte count，再提交 reference event：[terminal_projection.py](src/pulsara_agent/llm/terminal_projection.py#L400)。
- final RunEnd candidate 在 AgentRuntime 中具有 stable id 和单独 finalization owner：[agent.py](src/pulsara_agent/runtime/agent.py#L5979)。
- crash recovery 会恢复 incomplete model stream，并可合成 ReplyEnd：[model_stream_recovery.py](src/pulsara_agent/runtime/model_stream_recovery.py#L538)。

#### 2.2.3 一个回复涉及多少 owner

按“拥有独立 admission、candidate、task/worker、checkpoint、repair、drain 或 restart branch 的 family”计，text-only turn 至少涉及 14 个 owner family：

1. Host ingress / run boundary；
2. RunOwner / activation；
3. stable run finalization；
4. provider-input generation；
5. context input compile/I/O/audit；
6. model stream physical execution；
7. model terminal projection；
8. model control disposition；
9. rollout budget account/reservation；
10. physical operation accounting；
11. committed reducer registry/repair/post-fold；
12. runtime and transcript checkpoint maintenance；
13. long-horizon window/projection；
14. transcript/final-output/presentation delivery。

这是保守下界，不把 EventLog writer、artifact archive、projection-job worker、memory governance 和 terminal application 的每个子 owner 单独计数。Host close 的实际 wait surface 证明它们不是概念上的“一个 Runtime owner”：[host/session.py](src/pulsara_agent/host/session.py#L4992)。

### 2.3 一个 one-tool loop

#### 2.3.1 实测 durable event

路径为“第一次 model call 发出一个 read-only tool call → tool 返回结果 → 第二次 model call 给 final text”。实测 83 个 EventLog event、23 次 EventLog transaction：

| event family | 数量 |
|---|---:|
| RUN_START / RUN_END | 2 |
| REPLY_START / REPLY_END | 4 |
| MODEL_CALL_START / END | 4 |
| MODEL_CALL_TERMINAL_PROJECTION_COMMITTED | 2 |
| MODEL_CALL_CONTROL_DISPOSITION_RESOLVED | 2 |
| PROVIDER_INPUT_GENERATION_STARTED / APPEND_COMMITTED | 3 |
| CONTEXT_COMPILED | 2 |
| CONTEXT_PROJECTION_REWRITE_PAGE | 1 |
| CONTEXT_WINDOW_OPENED / CLOSED | 2 |
| PROJECTION_REQUESTED / READY | 4 |
| TOOL_CALL_START / ARGUMENTS / END | 3 |
| TOOL_RESULT_START / TEXT / END | 3 |
| TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED | 1 |
| TEXT_BLOCK_START / SEGMENT / END | 3 |
| CAPABILITY_GATE_DECISION / EXPOSURE_RESOLVED | 2 |
| ROLLOUT account open / close | 2 |
| ROLLOUT reservation create / settle | 6 |
| PHYSICAL reservation create / settle | 28 |
| PHYSICAL charge | 4 |
| LEDGER materialization genesis / horizon / consumer registration | 4 |
| SUBAGENT_GRAPH_CHECKPOINT_COMMITTED | 1 |
| **合计** | **83** |

两次 model call 又写 8 个 context-input audit artifact，共 116,820 bytes。于是 one-tool 最低是：

- 83 个 EventLog rows；
- 8 个 audit objects；
- 23 个 EventLog transaction；
- 至少 8 个 artifact write scope；
- 合计至少 91 个 durable object、31 个 durable write scope。

相对 text-only，增加的不是单纯 6 个 tool transcript block，而是第二套 provider-input/context/model/projection/control/reservation 生命周期。

#### 2.3.2 Tool 的实际 commit 边界

当前 tool call 来自 durable model stream：ToolCallStart、arguments、ToolCallEnd 在模型 terminalization 前已经进入 EventLog：[segment.py](src/pulsara_agent/llm/segment.py#L650)。

ToolExecutor 在真正调用 concrete tool 之前先追加 ToolResultStartEvent：[tool_executor.py](src/pulsara_agent/runtime/tool_executor.py#L126)、[tool_executor.py](src/pulsara_agent/runtime/tool_executor.py#L211)。tool 返回后再产生 result content/end，并由单独 terminal projection owner构建：

- tool result document；
- semantic join；
- artifact reference；
- ToolResultTerminalProjectionCommittedEvent；
- ToolResultEnd reference。

代码：[terminal_projection.py](src/pulsara_agent/runtime/terminal_projection.py#L620)。

这意味着当前实现已经具备推荐语义所需的两个最小事实雏形：

1. execute 前有 durable call/start；
2. return 后有 durable result。

但是它在两者外面又增加 stable candidate owner、terminal projection artifact、confirmation state、physical handoff、post-fold repair 和 close drain。ToolExecutionStableCandidateOwnerState 包含 admitted、candidate frozen、retry wait、commit outcome unknown、durable full awaiting physical handoff、reconciliation required：[tool_execution.py](src/pulsara_agent/ports/tool_execution.py#L181)。

one-tool 路径至少涉及 17 个 owner family：text-only 的 14 个，加 tool execution stable candidate、tool terminal projection/artifact、tool capability/terminal monitor handoff。外部 process tool 还可能增加 terminal session/monitor owner。

#### 2.3.3 one-tool测量不是实际tool message形状的上界

当前代码明确支持一个assistant message同时包含text与多个tool calls。provider-neutral `LLMMessage`同时保存`content`和有序`tool_calls` tuple，[input.py](src/pulsara_agent/llm/input.py#L56)；Chat Completions lowering把它们放回同一个assistant payload，[chat_completions.py](src/pulsara_agent/llm/adapters/openai/chat_completions.py#L509)；Responses lowering也同时处理text与全部calls，[responses.py](src/pulsara_agent/llm/adapters/openai/responses.py#L475)。

Runtime提取一个reply中的全部tool blocks，再以其原顺序安装`ToolBatchAttempt`：[agent.py](src/pulsara_agent/runtime/agent.py#L4396)、[agent.py](src/pulsara_agent/runtime/agent.py#L4484)。concrete execution为每个call创建独立task，并按`FIRST_COMPLETED`消费，所以terminal results可以乱序到达：[agent.py](src/pulsara_agent/runtime/agent.py#L8692)。当前transcript reconstruction最终仍把同一`reply_id`下的text/call blocks组合成一个assistant message并按call id配对result：[transcript.py](src/pulsara_agent/runtime/context_input/transcript.py#L419)。

因此83 events/23 transactions只量化“一个call、一个result”的样本，不证明durable unit应是单call。目标schema必须把完整assistant tool-request message作为execute barrier；否则逐call先commit/先execute会让一个合法provider message被crash切成不可重建的半条消息。

### 2.4 Projection、checkpoint 与“证明另一个 durable fact”的 durable fact

#### 2.4.1 可从更基础事实重建

| 当前 projection/checkpoint | 基础事实 | 是否可重建 | 推荐定位 |
|---|---|---|---|
| transcript projection | accepted transcript events/rows | 是 | cache/read model |
| provider-input generation projection | canonical transcript + compiler version + compaction checkpoint | 大多可以；exact historical bytes未必 | execution-local；必要时保存最终 request hash |
| model terminal projection document | model stream blocks | 是；若只保留 final assistant message则无须重建旧布局 | accepted reply 本体，而不是第二层 projection |
| tool terminal projection document | tool call/result transcript | 是 | UI render cache |
| runtime projection checkpoints | EventLog prefix | 是 | acceleration only |
| transcript projection checkpoint | transcript/EventLog prefix | 是 | acceleration only |
| prompt queue checkpoint | canonical queue item/status | 是 | 可删除 checkpoint，直接查 queue |
| subagent graph checkpoint | canonical subagent task/message/result | 是 | acceleration；最小任务表可直接查询 |
| terminal presentation state | transcript + active process status | 是 | UI cache，永不 gate Runtime |
| context-input audit view | provider request/canonical transcript；exact source detail可能丢失 | semantic view可重建，exact audit未必 | opt-in diagnostic |
| final-output materialization | accepted assistant reply + terminal status | 是 | query/view，不是 commit prerequisite |

EventLog replay 本身已能构建 message、timeline 与 provenance：[message_assembler.py](src/pulsara_agent/replay/message_assembler.py#L180)、[timeline.py](src/pulsara_agent/replay/timeline.py#L160)。事故文档也实测 terminal notification reducer 可以从 durable checkpoint 之后的 ledger exact replay：[PULSARA_TERMINAL_COMPLETION_FINALIZATION_FAILURE_INCIDENT.zh.md](PULSARA_TERMINAL_COMPLETION_FINALIZATION_FAILURE_INCIDENT.zh.md#L463)。

#### 2.4.2 proof-of-proof

以下 durable/process facts 的主要作用不是表达产品发生了什么，而是证明另一个 fact 是否成功：

- stable event candidate id 证明 retry 仍是同一候选；
- reservation/account 证明物理写入/预算已占用和结算；
- terminal projection reference 证明 artifact document 与 event join；
- checkpoint head + validation base + fingerprint 证明 projection cache successor；
- repair plan fingerprint + repair receipt fingerprint 证明 reducer repair；
- ModelCallControlDisposition source_result_fingerprint 证明 terminal result 到 permit；
- audit root 证明所有 pages 已写并 exact read-back；
- final-output receipt 证明 RunEnd 对应的输出已 materialize；
- FULL/NONE/UNKNOWN/CONFLICT confirmation 证明数据库是否接受候选。

其中数据库 commit 是否成功确实可能 UNKNOWN；但 Pulsara 把这一真实的不确定性复制到 run boundary、projection、tool execution、checkpoint、migration、finalization等多个 domain owner，而不是在最底层 storage commit 上收敛一次。

#### 2.4.3 TUI presentation不是薄read model

当前TUI authority contract也放大了projection边界：

- protocol major固定为2：[codec.py](src/pulsara_agent/terminal_protocol/codec.py#L70)；
- detach会访问RuntimeSession上的Presentation Foundation并释放attachment retention root：[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L390)；
- snapshot通过application service取得bundle，校验control cursor generation/revision并borrow confirmed root：[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L447)；
- history page围绕root/cursor/outcome读取，而不是按canonical transcript sequence简单分页：[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L1717)；
- Go decoder在一个snapshot上联合验证active head、confirmed root、projection contract、root/cursor pair、resident entries与rank spine：[carriers_gen.go](clients/terminal/internal/protocolvalue/carriers_gen.go#L864)。

因此Presentation Foundation既是Runtime close dependency，也是Python/Go wire-level read authority。虽然其中多数内容可由transcript重建，删除它必须与Protocol major、Go cache和reconnect/GAP语义同步hard cut；不能先把它de-gate/drop，再把“TUI直接查transcript”留给后续。

#### 2.4.4 mutation idempotency当前已存在，但被receipt graph放大

v2 wire已经携带`command_id`、submit-specific `client_submission_id`，并支持ACK unknown后的command query：[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1486)、[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1568)。这是必须保留的产品边界。

当前server却为它建立独立`terminal_command_receipts` authority，保存request semantic fingerprint、receipt revision、outcome payload/fingerprint和PENDING_CONFIRMATION/RECONCILIATION_REQUIRED等状态；唯一键为session + client instance + command id：[command_receipt.py](src/pulsara_agent/runtime/terminal_application/command_receipt.py#L194)、[0011_terminal_presentation_queue.sql](src/pulsara_agent/storage/migrations/sql/0011_terminal_presentation_queue.sql#L142)。目标减法不是删除idempotency，而是把command identity落在turn、queue item或accepted interaction decision canonical row上，让query直接返回产品事实，并删除第二套receipt lifecycle。

#### 2.4.5 current control view证明sequence之外还需要一个唤醒维度

当前v2把session lifecycle、run、pending interaction与prompt queue作为独立control sections编码，并为每节携带source version/fingerprint：[codec.py](src/pulsara_agent/terminal_protocol/codec.py#L1000)。wire还定义了带`control_generation`、`control_revision`和transition accumulator的`ControlProjectionCursor`：[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1037)。这套per-section version、fingerprint、transition history属于待删amplification，但它揭示了一个真实需求：queue、turn control或session lifecycle可以在没有新transcript entry时改变。

目标Protocol v3不需要继承整套cursor graph，只需把它压成`sessions.control_revision`一个数据库高水位。若Observe只比较`latest_sequence`，丢失control edge notification后就没有任何level-triggered条件迫使observer刷新。

#### 2.4.6 pending interaction当前混合了live working state与durable recovery authority

`RunActivationWorkingState`的类注释明确称其为“Short-lived”“not a durable fact source”，但其中直接保存pending tool calls、interaction kind/payload与source candidate：[state.py](src/pulsara_agent/runtime/state.py#L118)。Host读取的pending interaction来自当前suspended run owner的live view：[session.py](src/pulsara_agent/host/session.py#L884)，TUI再把approval、plan与MCP request编码为`PendingInteraction`：[codec.py](src/pulsara_agent/terminal_protocol/codec.py#L830)。另一方面，现体系又为interaction resume建立durable transition、recovery与reconciliation路径：[session.py](src/pulsara_agent/host/session.py#L4183)。

这正是减法需要明确切开的边界：V1保留同一Host内可重新查询的live request，但不保留其跨Host execution continuation；accepted decision是产品事实，尚未回答的request不是。canonical snapshot不得再把live request伪装成durable control row。

### 2.5 FULL/NONE/UNKNOWN/CONFLICT 与 latch 量化

严格按 literal enum 成员计，完整拥有 FULL、NONE、UNKNOWN、CONFLICT 四个值的 family 有 2 个：

1. RuntimeProjectionCheckpointDisposition：[projection_checkpoint_maintenance.py](src/pulsara_agent/runtime/projection_checkpoint_maintenance.py#L97)；
2. BoundaryBatchCommitStatus；它还多一个 PARTIAL：[run_boundary.py](src/pulsara_agent/primitives/run_boundary.py#L439)。

若把 UNRESOLVED 或 PARTIAL_UNTRUSTED 视为 UNKNOWN 等价类，则至少有 6 个 3+/4-way confirmation family：

| family | states | 证据 |
|---|---|---|
| RuntimeProjectionCheckpointDisposition | FULL/NONE/UNKNOWN/CONFLICT | [projection_checkpoint_maintenance.py](src/pulsara_agent/runtime/projection_checkpoint_maintenance.py#L97) |
| BoundaryBatchCommitStatus | NONE/FULL/PARTIAL/CONFLICT/UNKNOWN | [run_boundary.py](src/pulsara_agent/primitives/run_boundary.py#L439) |
| DurableProjectionCommitConfirmation | FULL/NONE/CONFLICT/UNRESOLVED | [contracts.py](src/pulsara_agent/projection_jobs/contracts.py#L69) |
| DurableRunExistence | NONE/FULL/UNKNOWN/PARTIAL_UNTRUSTED | [run_entry.py](src/pulsara_agent/primitives/run_entry.py#L29) |
| ToolExecutionCandidateConfirmationKind | FULL/NONE/UNKNOWN/PARTIAL | [tool_execution.py](src/pulsara_agent/ports/tool_execution.py#L191) |
| PostgresCommitConfirmation | FULL/NONE/CONFLICT/UNRESOLVED | [runner.py](src/pulsara_agent/storage/migrations/runner.py#L83) |

RuntimeSession 另有 6 个 hard reconciliation latch，最终聚合为一个 reconciliation_required gate：

- generic committed reducer；
- ledger；
- context input；
- memory governance；
- publication；
- mandatory audit。

定义与聚合：[session.py](src/pulsara_agent/runtime/session.py#L526)、[session.py](src/pulsara_agent/runtime/session.py#L2387)。

这两个数字回答不同问题：

- exact four-state enum = 2；
- confirmation family = 6；
- global hard latch source = 6；
- outcome DTO、owner state、retry/reconciliation state远多于 6，不应混在一个不精确数字里。

### 2.6 Host close 的真实阶段

HostSession.aclose 目前约 193 行、45 个 await、4 次 committed-reducer barrier。按语义可分为至少 6 个阶段：

| 阶段 | 当前等待对象 | 代码 |
|---|---|---|
| 1. 停 ingress 与 terminal producers | ingress、terminal notification dispatch、monitor workers、terminal sessions | [session.py](src/pulsara_agent/host/session.py#L5006) |
| 2. 多次 reducer fixed point | 三次 early reducer barrier、terminal notification owners、active run | [session.py](src/pulsara_agent/host/session.py#L5036) |
| 3. run/control/physical owner | commands、queue、interaction、suspended run、activation reconciliation、finalization、model stream、tool terminal | [session.py](src/pulsara_agent/host/session.py#L5050) |
| 4. governance/projection/provider/subagent/MCP | memory outbox、compaction、candidate projection、provider quiesce、context audit、subagent、MCP | [session.py](src/pulsara_agent/host/session.py#L5080) |
| 5. final producer/reducer fixed point | 第四次 reducer barrier、event writer、reducer repair、post-fold | [session.py](src/pulsara_agent/host/session.py#L5145) |
| 6. acceleration/UI | runtime checkpoint、subagent graph checkpoint、transcript checkpoint、prompt queue checkpoint、presentation | [session.py](src/pulsara_agent/host/session.py#L5166) |

这不是一个普通 session shutdown，而是全图分布式 terminalization barrier。任何新 producer 都必须被插入正确 fixed point；事故风险来自 ordering completeness，而非单个 await 写错。

新 non-Host teardown 又复制了一个 11-await 子图：provider quiesce → mandatory audit → reducer barrier → runtime checkpoint → compaction → graph/transcript/prompt checkpoints → presentation → context I/O：[session.py](src/pulsara_agent/runtime/session.py#L6719)。

### 2.7 Restart/resume 的真实语义

当前 resume 不是“加载 transcript 后开始新 turn”，而是恢复并终结旧 execution graph：

1. 打开 PostgresEventLog 与 artifact store；
2. repair incomplete model streams；
3. repair missing control dispositions；
4. 查询 canonical running runs；
5. 构造完整 temporary RuntimeSession；
6. materialize dormant RunOwner；
7. 恢复/终结 MCP input-required；
8. 要求 long-horizon window/account/projection state完整存在；
9. 要求 active reservations 已先恢复；
10. 生成 ContextWindowClosed + RolloutBudgetAccountClosed + recovered RunEnd；
11. 写入并确认 terminal batch；
12. materialize final output；
13. 要求 publication 可用；
14. teardown temporary RuntimeSession。

代码：[resume.py](src/pulsara_agent/host/resume.py#L87)、[resume.py](src/pulsara_agent/host/resume.py#L142)、[resume.py](src/pulsara_agent/host/resume.py#L200)、[resume.py](src/pulsara_agent/host/resume.py#L243)、[resume.py](src/pulsara_agent/host/resume.py#L293)、[resume.py](src/pulsara_agent/host/resume.py#L319)、[resume.py](src/pulsara_agent/host/resume.py#L346)。

当前`sessions`基表没有Host writer lease或generation，仅有id、workspace root、created_at与metadata：[0002_runtime_truth_baseline.sql](src/pulsara_agent/storage/migrations/sql/0002_runtime_truth_baseline.sql#L77)。terminal protocol中的`controller_generation`存在于attachment/command binding层：[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L292)、[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1486)，代码不能据此确认数据库层已有single-writer fencing。推荐方案因此显式新增最小session writer generation/lease，而不是继续用candidate fingerprint/CAS间接容忍多writer。

因此当前语义虽然最终把 dangling run 标成 RECOVERED_INTERRUPTED，但实现手段仍是恢复原 execution state machine 的大量内部 authority，精确生成其 terminal successors。这正是推荐方案要切断的边界：**产品只需要 interrupted fact，当前实现却要求恢复“如何正确结束旧 executor”。**

### 2.8 完整 durability amplification 因果链

本轮最清晰的历史链是：

~~~text
产品需求：terminal process 完成后，模型应收到结果并继续 finalization
  -> durable TerminalProcessCompleted / ToolResult terminal fact
  -> 为消费它，建立 committed semantic reducer
  -> 为加速 reopen，reducer callback 同步写 runtime projection checkpoint
  -> 为确认 checkpoint successor，引入 validation base/head/fingerprint/CAS
  -> JSONB tuple/list 表示漂移使 checkpoint 失败
  -> event 已 FULL、semantic store已推进、registration high-water未推进
  -> RuntimeSession hard reconciliation latch
  -> post-tool context fail closed，下一次 ModelStart 不发生
  -> RunEnd 也被同一 latch 拒绝
  -> stable RunEnd candidate 进入 repair-driven retry
  -> finalization owner无限重试并挂住 Host close
  -> 修复再新增 committed-reducer repair、post-fold、checkpoint maintenance、
     多个 reducer barrier、provider quiesce 与 temporary recovery teardown
~~~

事故代码证据和现场因果在 [PULSARA_TERMINAL_COMPLETION_FINALIZATION_FAILURE_INCIDENT.zh.md](PULSARA_TERMINAL_COMPLETION_FINALIZATION_FAILURE_INCIDENT.zh.md#L19)、[PULSARA_TERMINAL_COMPLETION_FINALIZATION_FAILURE_INCIDENT.zh.md](PULSARA_TERMINAL_COMPLETION_FINALIZATION_FAILURE_INCIDENT.zh.md#L257)、[PULSARA_TERMINAL_COMPLETION_FINALIZATION_FAILURE_INCIDENT.zh.md](PULSARA_TERMINAL_COMPLETION_FINALIZATION_FAILURE_INCIDENT.zh.md#L350)、[PULSARA_TERMINAL_COMPLETION_FINALIZATION_FAILURE_INCIDENT.zh.md](PULSARA_TERMINAL_COMPLETION_FINALIZATION_FAILURE_INCIDENT.zh.md#L402)。

本次新提交形成了同构的小链：

~~~text
产品/诊断需求：以后能检查某次 model call 的 exact context input
  -> ContextCompiled 中保存 audit expectation
  -> plan + pages + root 分层 durable artifact
  -> 每页写后 exact read-back确认
  -> incomplete artifacts 需要 retention policy 与 GC
  -> GC 需要 session close + materialization account drained
  -> audit operation 进入 Host 和 non-Host close drain
  -> optional diagnostic 再次成为生命周期 barrier
~~~

这就是本文所称的 **durability amplification**：第一个产品事实并非主要成本；证明、恢复、关闭和观察它的衍生机制逐层放大，最终反过来控制主路径。

---

## 3. Incident pattern analysis

### 3.1 至少九类重复故障/repair

| 类别 | 代表证据 | 局部触发 | 共同架构选择 |
|---|---|---|---|
| 1. durable FULL，但 reducer/checkpoint失败 | 当前 terminal finalization 事故；[incident](PULSARA_TERMINAL_COMPLETION_FINALIZATION_FAILURE_INCIDENT.zh.md#L257) | JSONB tuple/list successor drift | Event durability 后仍要求 derived reducer/checkpoint同步健康 |
| 2. physical operation 未退出，logical owner已关闭或 waiter 已取消 | model worker cancellation、unknown commit close tests；[test_llm_runtime.py](tests/test_llm_runtime.py#L2468)、[test_llm_runtime.py](tests/test_llm_runtime.py#L2543) | cancellation/transport I/O晚退 | caller、logical task与physical I/O被拆成多代 owner |
| 3. candidate 已提交，confirmation 丢失 | tool/model terminal UNKNOWN、same-candidate retry；[test_llm_runtime.py](tests/test_llm_runtime.py#L1806)、[test_terminal_completion_finalization_incident.py](tests/test_terminal_completion_finalization_incident.py#L403) | commit ack/connection不确定 | 每个 domain 都复制确认状态机 |
| 4. restart 恢复出另一 terminal outcome | incomplete stream合成 ModelEnd/ReplyEnd，dangling run合成 recovered RunEnd；[resume.py](src/pulsara_agent/host/resume.py#L110)、[resume.py](src/pulsara_agent/host/resume.py#L293) | crash window | 恢复 execution transition，不只记录 interruption |
| 5. close 顺序遗漏晚到 producer | 四段 reducer barrier和“所有 producers quiescent”注释；[host/session.py](src/pulsara_agent/host/session.py#L5145) | terminal/governance/compaction/subagent晚到 | Host close承担全图 fixed-point coordinator |
| 6. acceleration checkpoint 阻塞 semantic mainline | checkpoint事故与 hard-fence tests；[test_terminal_completion_finalization_incident.py](tests/test_terminal_completion_finalization_incident.py#L823) | checkpoint lag/I/O/CAS | acceleration被提升为 admission authority |
| 7. process-local identity 在 reopen 无法自然重建 | model recovery plan、dormant RunOwner、child teardown generation | task/coroutine/lease消失 | process state被要求具备 durable identity |
| 8. observer/UI publication反向阻塞 Runtime | publication latch tests；[test_runtime_publication_maintenance.py](tests/test_runtime_publication_maintenance.py#L44) | publication unavailable | observation failure被纳入 semantic mutation gate |
| 9. recovery owner 自身需要 repair/teardown owner | committed reducer repair receipt、temporary RuntimeSession teardown、child teardown retry lineage；[committed_reducer_repair.py](src/pulsara_agent/runtime/committed_reducer_repair.py#L98)、[subagent/execution.py](src/pulsara_agent/runtime/subagent/execution.py#L610) | repair task失败、close/cancel | 恢复过程也被建模成必须精确收敛的事务 |

### 3.2 git history 显示的是同一压力方向

相关提交不是单一模块偶发：

- 0e40febd：terminal finalization repair；
- 77f4558b、2c0e92c7：durable terminal process monitoring；
- 4bd90d92、0cfc45d6、2d7b1d67：MCP close ownership；
- 99ba42d3：durable conversation resume；
- 47e3a3bc：approval resume；
- 27f1b1a7、123ca092：Host/session/terminal lifecycle hardening。

这些提交解决的局部问题往往真实存在，但历史趋势是：每当 crash、cancel、late producer 或 ambiguous commit 出现，就把更多 transition 提升为 stable candidate/owner/reconciliation，而不是缩窄产品承诺。

架构债务 rebase 文档把大量 repair、checkpoint、outbox、durable job 和 owner 收口标为 CLOSED：[PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md](archived_docs/PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md#L208)、[PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md](archived_docs/PULSARA_RUNTIME_ARCHITECTURE_DEBT_REBASE.zh.md#L542)。这说明现有体系内部的一致性工作做得很认真；它不回答“这些 authority 是否应该存在”。本次调研针对的正是后一个问题。

### 3.3 测试数量不是设计正确性的证据

相关路径至少有 163 个 test files 命中 runtime/terminal/finalization/reducer/checkpoint/resume/provider/projection/close 关键词。事故修复测试明确冻结了：

- stable checkpoint candidate across outcomes；
- checkpoint owner rebind；
- checkpoint close blocked；
- reducer repair waiter detach；
- repair plan rebind/retry；
- RunEnd finalization等待 exact repair receipt；
- child teardown retry/reconciliation；
- Host close dependency order。

代表路径：[test_terminal_completion_finalization_incident.py](tests/test_terminal_completion_finalization_incident.py#L403)、[test_terminal_completion_finalization_incident.py](tests/test_terminal_completion_finalization_incident.py#L1090)、[test_terminal_completion_finalization_incident.py](tests/test_terminal_completion_finalization_incident.py#L1879)、[test_subagent_runtime.py](tests/test_subagent_runtime.py#L379)。

这些测试证明复杂机制按其 contract 工作；它们同时量化了该 contract 需要多少状态和故障分支。减法阶段应删除随 owner 一起失去产品意义的测试，而不是让旧测试成为不可变需求。

### 3.4 共同根因

这些事故的共同根因不是“async 很难”“PostgreSQL 不可靠”或某个 reducer 写错，而是一个更上游的架构选择：

> Pulsara 把 foreground agent execution 当成需要跨进程 exact continuation 的分布式事务，并把可重建 projection、UI observation、budget/accounting、physical lifecycle 与 semantic transcript 一起纳入其 correctness envelope。

于是每个真实 crash window都会产生五个后续问题：

1. 候选是否已经写入？
2. derived reducer是否已 fold？
3. checkpoint是否追上？
4. physical owner是否退出？
5. close/restart由谁完成未结束的 successor？

如果产品语义改成“canonical transcript boundary 之外的 crash = explicit interruption”，问题会收敛为两个：

1. 哪些 transcript/tool audit facts已经 committed？
2. 未完成 external side effect 是否 outcome_unknown？

这不是降低工程标准，而是把 correctness 用在用户真正可观察、跨进程必须保留的事实上。

---

## 4. Claude Code / Codex comparison

### 4.1 证据边界

Claude Code 本地仓库是泄漏 source-map 还原快照，且部分产品服务端逻辑不可见。因此：

- 能直接从 TypeScript 看到的 transcript、stream、recovery 和 close 行为标为“代码确认”；
- side-effect delivery guarantee 等无法由客户端仓库闭合的结论标为“推断”或“无法确认”；
- 不把 grep 没找到某机制等价为产品绝不存在该机制。

Codex 本地 Rust 仓库可追踪 rollout persistence、stream handler、tool dispatch、abort、resume 与 shutdown；本文仍只声称代码覆盖范围内的事实。

### 4.2 逐项对照

| 语义边界 | Pulsara | Claude Code | Codex | 证据路径 | 可借鉴结论 |
|---|---|---|---|---|---|
| conversation/session history 保存什么 | 151 类 EventType 组成 universal ledger，并派生 transcript、run、projection、checkpoint、job、presentation | **[代码确认]** JSONL transcript 的 canonical chain只包括 user、assistant、attachment、system；progress明确是 ephemeral | **[代码确认]** JSONL rollout持久化 completed ResponseItem、turn lifecycle、compaction、session/world/context等选择性 item | Pulsara [events.py](src/pulsara_agent/event/events.py#L298)；Claude [sessionStorage.ts](../claude-code/src/utils/sessionStorage.ts#L128)；Codex [policy.rs](../codex/codex-rs/rollout/src/policy.rs#L7) | durable vocabulary应由可恢复产品事实驱动，不由所有内部 transition 驱动 |
| accepted user input | RunStart 内嵌 user input 及大量 run authority | **[代码确认]** 在进入 query loop 前写 transcript；普通交互 await写入 | **[代码确认]** user ResponseItem属于 persisted rollout item | Pulsara [run_entry.py](src/pulsara_agent/runtime/run_entry.py#L318)；Claude [QueryEngine.ts](../claude-code/src/QueryEngine.ts#L430)；Codex [policy.rs](../codex/codex-rs/rollout/src/policy.rs#L36) | user acceptance可直接是 transcript commit，无需完整 RunStart authority bundle |
| model stream delta | text/thinking/data/tool segments是 durable semantic events | **[代码确认]** text_delta、input_json_delta只更新内存 streaming state；completed Message才进入 transcript | **[代码确认]** AgentMessageContentDelta、PlanDelta、Reasoning delta、RawResponseItem、ItemStarted均 transient | Pulsara [segment.py](src/pulsara_agent/llm/segment.py#L620)；Claude [messages.ts](../claude-code/src/utils/messages.ts#L2927)；Codex [policy.rs](../codex/codex-rs/rollout/src/policy.rs#L117) | 原始 delta、transport segmentation和live layout无需 durable |
| final assistant reply 何时 durable | terminal projection committed + ModelCallEnd + ReplyEnd + control disposition + reducer/final output + RunEnd共同参与“完成” | **[代码确认]** assistant按 completed content block进入 JSONL；streaming路径 stop_reason可能稍后才到，不是单独 durable gate | **[代码确认]** completed non-tool ResponseItem finalize后 record_conversation_items | Pulsara [runtime.py](src/pulsara_agent/llm/runtime.py#L1144)；Claude [QueryEngine.ts](../claude-code/src/QueryEngine.ts#L687)；Codex [stream_events_utils.rs](../codex/codex-rs/core/src/stream_events_utils.rs#L358) | 一个 completed assistant message commit足以成为 transcript boundary；transport stop metadata可为附属诊断 |
| tool call 保存什么 | ToolCall start/arguments/end + terminal model projection + control disposition | **[代码确认]** tool_use是 assistant transcript content block | **[代码确认]** FunctionCall/LocalShellCall/CustomToolCall等 completed ResponseItem持久化 | Pulsara [segment.py](src/pulsara_agent/llm/segment.py#L650)；Claude [sessionStorage.ts](../claude-code/src/utils/sessionStorage.ts#L1025)；Codex [policy.rs](../codex/codex-rs/rollout/src/policy.rs#L38) | 保存已公开的完整 call，不保存 argument delta |
| tool call 与 execute 的顺序 | durable model call/tool event后进入 ToolExecutor，但又有 stable candidate/terminal owner | **[代码确认/推断]** transcript chain保存 tool_use；本地代码可见 tool workflow，但跨所有工具的 fsync-before-effect保证无法确认 | **[代码确认]** completed tool call先 record，再创建 tool future | Pulsara [tool_execution.py](src/pulsara_agent/ports/tool_execution.py#L181)；Codex [stream_events_utils.rs](../codex/codex-rs/core/src/stream_events_utils.rs#L326) | 需要的是 call-before-effect顺序，不是通用 exactly-once transaction |
| tool result 保存什么 | ToolResult start/text/end + terminal projection document/reference | **[代码确认]** tool_result作为 user transcript message，并以 originating assistant UUID连接 | **[代码确认]** tool future返回后将 FunctionCallOutput等 ResponseItem写 rollout | Pulsara [tool_executor.py](src/pulsara_agent/runtime/tool_executor.py#L126)；Claude [sessionStorage.ts](../claude-code/src/utils/sessionStorage.ts#L1028)；Codex [turn.rs](../codex/codex-rs/core/src/session/turn.rs#L1891) | durable result应是模型看到的 canonical output；UI artifact可以派生 |
| exactly-once side effect | 通过 stable candidate、reservation、confirmation、repair追求强 continuation，但无法把任意外部系统纳入同一原子事务 | **[无法确认]** 客户端代码没有显示通用跨进程 exactly-once protocol；不同 tool可能另有幂等语义 | **[代码确认 + 推断]** call-before-dispatch/result-after-return；未见把任意外部 side effect与 rollout原子提交的通用协议 | Pulsara [tool_execution.py](src/pulsara_agent/ports/tool_execution.py#L191)；Codex [stream_events_utils.rs](../codex/codex-rs/core/src/stream_events_utils.rs#L345)、[turn.rs](../codex/codex-rs/core/src/session/turn.rs#L1900) | 应明确承诺“可审计但不跨系统 exactly-once”；幂等只由具体 tool/idempotency key提供 |
| model stream 中 crash | restart repair incomplete stream、合成 terminal events/disposition，再修 dangling run | **[代码确认]** 检测 interrupted prompt/turn，过滤 unresolved tool use，追加 synthetic continuation message | **[代码确认]** active task可被 aborted，写 interrupted marker并 flush，再发 TurnAborted | Pulsara [model_stream_recovery.py](src/pulsara_agent/runtime/model_stream_recovery.py#L538)、[resume.py](src/pulsara_agent/host/resume.py#L110)；Claude [conversationRecovery.ts](../claude-code/src/utils/conversationRecovery.ts#L158)；Codex [tasks/mod.rs](../codex/codex-rs/core/src/tasks/mod.rs#L829) | crash可以是 explicit interruption；无需恢复 delta cursor或原 transport |
| tool已完成、final reply未生成时 crash | 尝试恢复 terminal outcome、control、follow-up/finalization owner | **[代码确认 + 推断]** persisted tool_result后会把 turn识别为 interrupted并以新 synthetic prompt继续；不是恢复旧 coroutine | **[代码确认 + 推断]** rollout已有 call/result；缺 final message时历史保留，下一 turn从历史继续；未见旧 tool future跨进程恢复 | Pulsara [resume.py](src/pulsara_agent/host/resume.py#L87)；Claude [conversationRecovery.ts](../claude-code/src/utils/conversationRecovery.ts#L186)；Codex [recorder.rs](../codex/codex-rs/rollout/src/recorder.rs#L933) | 恢复 transcript并新开 model call即可；不要恢复原 execution owner |
| side effect发生但 result未提交时 crash | 试图通过 tool stable candidate和physical handoff区分状态 | **[无法确认]** unresolved tool_use会被过滤，但无法由 transcript确认外部 effect是否发生 | **[推断]** rollout只显示 call无 result；无证据能证明外部 effect未发生 | Claude [conversationRecovery.ts](../claude-code/src/utils/conversationRecovery.ts#L186)；Codex call/result两边界同上 | Pulsara目标额外保存一个dispatch前attempt：无attempt=未dispatch，attempt无result=outcome_unknown；仍不伪造exactly-once |
| resume 的单位 | 重建 RuntimeSession、RunOwner、window/account/reservations、terminal successors，再 materialize output | **[代码确认]** 加载 transcript parent chain并按消息恢复；interruption通过新增 synthetic message进入新 query | **[代码确认]** 加载 rollout items，reconstruct conversation history/context；不是恢复旧 Rust future | Pulsara [resume.py](src/pulsara_agent/host/resume.py#L142)；Claude [sessionStorage.ts](../claude-code/src/utils/sessionStorage.ts#L2288)、[conversationRecovery.ts](../claude-code/src/utils/conversationRecovery.ts#L204)；Codex [recorder.rs](../codex/codex-rs/rollout/src/recorder.rs#L933)、[session/mod.rs](../codex/codex-rs/core/src/session/mod.rs#L1340) | resume应恢复 transcript与少量产品 checkpoint，不恢复 executor |
| UI observation 是否 authority | publication、presentation、notification可进入 latch或 close gate | **[代码确认]** progress明确不持久化、不参与 parent chain | **[代码确认]**大量 begin/delta/warning/UI事件明确 transient | Pulsara [publication_maintenance.py](src/pulsara_agent/runtime/publication_maintenance.py#L180)；Claude [sessionStorage.ts](../claude-code/src/utils/sessionStorage.ts#L134)；Codex [policy.rs](../codex/codex-rs/rollout/src/policy.rs#L117) | TUI projection不得反向阻断 Runtime；重连从 transcript snapshot恢复 |
| per-reducer checkpoint/confirmation | 9 reducers、独立 checkpoint/repair/post-fold owner | **[代码确认范围内]** transcript是append chain；未见为每个 transcript reducer建立通用 durable checkpoint状态机 | **[代码确认范围内]** rollout writer有一次 reopen/retry，但没有每个消费者一套 checkpoint confirmation | Pulsara [session.py](src/pulsara_agent/runtime/session.py#L838)；Claude [sessionStorage.ts](../claude-code/src/utils/sessionStorage.ts#L606)；Codex [recorder.rs](../codex/codex-rs/rollout/src/recorder.rs#L1610) | 可重建消费者共享 canonical history，不应各自成为 semantic gate |
| session close | 45 awaits、4 reducer barriers、全图 terminalization | **[代码确认]** flush queued transcript，再 best-effort补 metadata | **[代码确认]** abort active tasks、terminate processes、shutdown MCP/guardian，最后 flush/shutdown persistence | Pulsara [host/session.py](src/pulsara_agent/host/session.py#L4992)；Claude [sessionStorage.ts](../claude-code/src/utils/sessionStorage.ts#L443)；Codex [handlers.rs](../codex/codex-rs/core/src/session/handlers.rs#L599) | close只需停止 ingress、bounded cancel/join foreground、flush canonical records；后台 durable job不应绑死 Host |
| interrupted turn 是否产品状态 | 有 recovered interrupted terminalization，但由复杂 repair graph生成 | **[代码确认]** 显式 interrupted_prompt/turn detection | **[代码确认]** interrupted history marker和 TurnAborted | Pulsara [resume.py](src/pulsara_agent/host/resume.py#L293)；Claude [conversationRecovery.ts](../claude-code/src/utils/conversationRecovery.ts#L260)；Codex [tasks/mod.rs](../codex/codex-rs/core/src/tasks/mod.rs#L860) | Pulsara可把已有 RECOVERED_INTERRUPTED产品概念直接化，删除其 execution repair过程 |

### 4.3 对照结论

Claude Code 与 Codex 并非“不持久化”：

- 两者都保存 canonical conversation；
- 两者都保存 tool call/result；
- Codex还保存 reasoning、turn lifecycle、compaction、world/context与部分 subagent communication；
- 两者都有 writer flush/retry和 resume reconstruction。

差异在于它们没有把每个 provider delta、UI progress、consumer checkpoint和 close successor都升级为 foreground semantic authority。最值得借鉴的不是 JSONL 格式，而是三个边界：

1. **completed item，而非 transport delta，是 durable unit；**
2. **resume重建 history，而非恢复 coroutine；**
3. **interrupted是合法 terminal state，而非必须被隐形 repair成精确完成。**

对 Pulsara 独有能力的含义不是“照抄成熟产品并删除功能”，而是为每项能力找到最小事实：

| Pulsara 独有/更强能力 | 最小 durable boundary |
|---|---|
| Long-Horizon compaction | immutable summary/source range/generation contract；未被binding revision引用时可GC，被引用后是semantic context authority；不改写transcript |
| subagent | task accepted、parent/child relation、message/result、completed/interrupted；不保存 executor/teardown generation |
| terminal monitor/autonomous wake | command/process audit + durable notification job；前台 wait task和UI ticks不 durable |
| durable prompt queue | queue item、ordering key、accepted/claimed/completed/cancelled；无独立 projection checkpoint |
| memory governance | accepted memory fact、supersede/delete lineage、真正后台 extraction job；不保存每次 live fold |
| resumable Host session | session metadata、canonical conversation、pending prompt/job aggregate+attempt；只rehydrate，不恢复旧 foreground run |

---

## 5. Overdesign findings

### P1-1：foreground model execution 被建成 exact-recovery transaction

**当前机制**

ModelStart、durable stream segments、terminal projection、ModelEnd、ReplyEnd、control disposition、RunEnd分别有 stable identity、confirmation、repair和恢复逻辑。

**原始需求**

进程重启后不丢已接受对话，并能解释上一 turn 没有完成。

**amplification 链**

~~~text
不丢 conversation
  -> persist user/reply
  -> persist every model transition
  -> recover terminal projection
  -> recover control disposition
  -> recover stable finalization candidate
  -> temporary RuntimeSession + teardown
  -> Host close等待所有 repair owner
~~~

**删除后损失**

- 不能从最后一个 delta 精确续传；
- crash 时可能丢失尚未 accepted 的部分 assistant text；
- 不能声称原 model call跨进程继续。

这些损失与 Claude/Codex 的实际边界一致，并可用 explicit interrupted UX解释。

**推荐**

删除 foreground exact recovery。保留 accepted user input、completed assistant reply、tool call/result与 turn interrupted。ModelStart/End、ReplyStart/End、Disposition降为 operational telemetry或合并进 turn row。

### P1-2：checkpoint acceleration 成为 semantic gate

**当前机制**

semantic reducer success之后仍要维护独立 checkpoint candidate/head/confirmation；lag/conflict可以产生 hard fence、reconciliation或 close blocked。

**原始需求**

避免 reopen 从 sequence 1 重放大 ledger。

**amplification 链**

~~~text
加快 reopen
  -> reducer checkpoint
  -> successor validation base + CAS + fingerprint
  -> FULL/NONE/UNKNOWN/CONFLICT
  -> checkpoint maintenance owner
  -> reducer fold receipt / post-fold handoff
  -> repair owner与close drain
  -> checkpoint故障阻止回复和RunEnd
~~~

**删除/降级后损失**

- checkpoint丢失时 reopen更慢；
- 需要从 canonical transcript或job表重建 derived view；
- 对极长 session需分页/周期性 compact snapshot。

**推荐**

Checkpoint只允许影响 reopen latency。任何 checkpoint write/read/CAS失败不得阻止 user acceptance、assistant commit、tool result、RunEnd/turn completion或 close。直接删除能由小型 canonical table查询替代的 checkpoint。长历史的模型生成summary另行建模为immutable semantic context snapshot：创建失败不回滚旧事实，被binding revision引用后却不能当普通checkpoint删除。

### P1-3：tool stable candidate制造了无法兑现的 exactly-once外观

**当前机制**

ToolExecutionStableCandidateOwner把 terminal/suspension candidate、FULL/NONE/UNKNOWN/PARTIAL、physical handoff和reconciliation分层；但外部系统 side effect无法与 Pulsara EventLog原子提交。

**原始需求**

防止 crash/retry静默重复写文件、发请求或执行命令。

**amplification 链**

~~~text
避免重复side effect
  -> stable tool candidate
  -> pre/post durable facts
  -> commit confirmation
  -> physical handoff owner
  -> UNKNOWN/PARTIAL recovery
  -> close必须等待owner
  -> 仍无法证明外部系统是否执行
~~~

**删除后损失**

- 不再承诺跨进程 exactly-once；
- attempt已写但result缺失时无法自动判定成功/失败。

**推荐**

把“不知道”变成产品语义，并保留最小真实dispatch事实：完整assistant message先commit；每个physical invoke前再commit一个窄`tool_execution_attempts` row；result-after-return并引用exact attempt。call存在但attempt不存在证明未dispatch；attempt存在但result缺失才是outcome_unknown。默认禁止自动重试；展示command/tool、参数摘要、actor、时间与attempt id，由用户或新模型turn显式决定。具体具备idempotency key的tool可以单独实现，但不得提升为全局Runtime proof protocol。

### P1-4：Host close 是全图 terminalization coordinator

**当前机制**

close有45个await、4个reducer barrier和至少6个语义阶段；任何 producer/repair/checkpoint/presentation owner都可能阻塞。

**原始需求**

退出时不丢 canonical事实、不遗留子进程或后台任务。

**amplification 链**

~~~text
安全退出
  -> 等每个owner
  -> owner close期间还能produce
  -> 增加producer barrier
  -> late producer触发reducer repair
  -> 再增加fixed-point barrier
  -> 新owner必须插入正确顺序
  -> ordering遗漏成为运行时事故
~~~

**删除后损失**

- operational audit/cache可能来不及完成；
- foreground任务在deadline后直接被标 interrupted；
- 真正 durable job由另一个 worker在下次启动继续，不由 Host close完成。

**推荐**

压缩为3段：stop ingress；bounded cancel/join foreground并写interrupted/unknown；flush Host-owned transcript/tool/queue/job-control authorization commits后关闭session资源。background worker及其claim lease属于独立lifecycle，不由session close完成、释放或换代。

### P1-5：resume在恢复 execution，而非恢复 conversation

**当前机制**

resume修 model stream、control disposition、RunOwner、MCP continuation、long-horizon window/account/reservation、recovered RunEnd、final-output projection，再 teardown temporary RuntimeSession。

**原始需求**

用户可重开 session，看到上下文并继续工作。

**amplification 链**

~~~text
session可resume
  -> 查dangling run
  -> 重建旧RuntimeSession
  -> 恢复所有内部account/projection
  -> 生成精确terminal successors
  -> materialize final output/publication
  -> temporary session也需11-await teardown
~~~

**删除后损失**

- 不再精确补齐旧 event timeline；
- 旧内部 reservations、model call outcome只作为历史 diagnostic，或 reset后不存在。

**推荐**

open时用一条幂等 UPDATE 把 running turn置 interrupted，附 interruption reason/time；加载 canonical transcript；新 user/auto-continuation产生新 turn。不要构造 temporary RuntimeSession。

### P1-6：foreground authority cut 被错误地拆成可独立上线的 text/tool/resume/UI 阶段

**当前机制与原始需求**

当前普通 Agent 在提交 user input 时尚不知道模型会返回纯文本还是 tool call；同一 provider call 可以在 stream 后半段才作出 tool choice。原路线却试图让 text turn 先进入新 schema、tool turn 暂留 EventLog，并把 transcript-only resume 与 TUI 读取切换放在后续阶段。原始需求只是用 vertical slice 降低改造风险，但这个切法跨越了 authority 的不可分割边界。

当前 TUI 也不是“换一个 transcript query”即可迁移：Python gateway 的 detach/snapshot/history page 依赖 Presentation Foundation root、retention-root lease、confirmed cursor 与 rebase；Go 客户端把 active head、confirmed root、root/cursor pair 和 resident page cache 当作协议不变量。[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L390)、[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L447)、[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L1717)、[carriers_gen.go](clients/terminal/internal/protocolvalue/carriers_gen.go#L864)、[state.go](clients/terminal/internal/presentation/state.go#L1)、[cache.go](clients/terminal/internal/presentation/cache.go#L1)。

**amplification 链**

~~~text
希望先切最简单text path
  -> turn开始时预选new/old authority
  -> 模型后来才决定是否调用tool
  -> 同一session出现两套transcript authority
  -> context/TUI/compaction需要merge reader
  -> crash时旧resume无法解释新running turn
  -> 再增加compat projection/reconciliation
  -> hard cut重新长出第二套authority
~~~

**删除/降级后损失**

- 第一个生产切换包会比单纯 text-only patch 大；
- Python protocol、Go client、Inspector、context compiler 与 resume 必须在一个 release train 中完成；
- rollback只能是 binary + DB snapshot/reset 的整体回退。

**推荐**

把 foreground user/assistant/tool call/tool result/interrupted/unknown 作为一个不可拆的 authority unit，一次 hard cut；同一 release 同时完成最小 open/resume 和所有 production readers。若需要先验证 direct schema，只允许在启动前明确选择、全 session 不暴露工具的 `NO_TOOLS` 实验模式中做 pre-production spike；它不能成为普通 Agent 的生产阶段，也不能与旧 tool authority 混用。

### P1-7：Host writer generation与background job generation必须是两个fencing domain

**目标方案中的缺口**

旧稿要求job-control mutation也携带当前Host `writer_generation`，同时又承诺durable job跨Host继续。若worker result绑定Host generation，Host takeover会错误废止仍持有合法job lease的worker；若worker绕过fencing直接写transcript，又会破坏single-writer约束。

**风险链**

~~~text
background job跨Host继续
  -> job创建于writer generation N
  -> Host takeover到generation N+1
  -> 合法worker用N提交result被拒绝
  -> 为挽救result引入compatible winner/repair
  OR worker绕过Host直接写session transcript
  -> single-writer边界失效
~~~

**推荐**

冻结两个完全独立的conditional-mutation domain：`writer_generation`只保护turn、transcript、foreground tool attempt/result、prompt/queue admission、job enqueue/cancel authorization和session metadata；`claim_generation`只保护job attempt claim、progress、result、failure与lease settlement。background worker只写job/attempt-owned row/blob/message namespace，不直接追加session transcript。当前Host要把job result公开给模型或用户时，必须以当前writer generation做一次显式accept transaction。Host takeover不改变已有job attempt claim generation，job reclaim也不改变Host writer generation。

**边界损失**

job completed与“结果已进入conversation”成为两个明确事实，可能有可见延迟；这是避免worker成为第二session writer所必须的产品边界，不需要receipt或reconciliation owner。

### P1-8：Protocol v3 snapshot需要一个线性化的canonical read cut

**目标方案中的缺口**

旧稿让snapshot同时返回suffix、retention边界、latest sequence与turn/control状态，却没有规定它们来自同一个PostgreSQL MVCC snapshot。多次独立query可形成high-water与rows互相矛盾的response，而notification又只是可丢hint。

**风险链**

~~~text
删除Presentation root
  -> 多次query拼snapshot
  -> 并发assistant/tool commit落在query之间
  -> latest=10但suffix只到9，或反之
  -> client把不完整response当完整high-water
  -> edge notification又已丢失
  -> canonical entry长期不可见
~~~

**推荐**

canonical snapshot使用一个read-only `REPEATABLE READ` transaction：在同一MVCC cut中读取`transcript_epoch`、retention lower bound、`latest_sequence`、`control_revision`和canonical session/turn/tool-attempt/queue/accepted-decision state，只返回`entry_sequence <= latest_sequence`的rows。history page携带epoch与明确`cut_sequence`，并只返回该cut内的rows。operational spinner、transport、live process progress与pending interaction走独立endpoint/stream，不伪装成canonical snapshot成员。canonical Observe同时提交observed sequence与observed control revision，任一落后即返回snapshot-required。这里不引入root、per-section cursor、transition history、fingerprint、checkpoint或durable read receipt。

**边界损失**

一次snapshot会持有一个短read transaction，page只展示其固定cut而不是“边翻页边追最新”；新内容通过新的high-water/snapshot进入，换来可验证的一致性。

### P1-9：Protocol v3必须保留canonical-row级mutation idempotency

**当前代码事实与目标缺口**

v2 `CommandBinding`已经携带`command_id`，`SubmitPromptCommand`还携带`client_submission_id`，并暴露command query：[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1486)、[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1568)。当前实现把它扩大成`terminal_command_receipts`表、pending/confirmation/reconciliation outcome与receipt fingerprint：[command_receipt.py](src/pulsara_agent/runtime/terminal_application/command_receipt.py#L194)、[0011_terminal_presentation_queue.sql](src/pulsara_agent/storage/migrations/sql/0011_terminal_presentation_queue.sql#L142)。旧稿删除这套graph时没有明确保留user acceptance ACK丢失所需的最小幂等键。

**风险链**

~~~text
删除generic command receipt
  -> user/queue commit成功但ACK丢失
  -> client reconnect后重新submit
  -> server无法定位第一次canonical target
  -> 第二个user turn/queue item被接受
  -> 为修duplicate再引入receipt/reconciliation
~~~

**推荐**

turn、prompt queue item或interaction decision的canonical row直接保存`command_id`，submit prompt另保存`client_submission_id`；`(session_id, command_id)`唯一。相同ID与相同typed semantic input返回已有canonical target；相同ID但不同input返回conflict，且conflict response本身不需要durable row。command query按`(session_id, command_id)`直接构造target id/status/reference；不依赖原client connection，也不建立通用receipt、query token、outcome fingerprint或PENDING_CONFIRMATION状态机。只读query不要求writer generation；创建新target或cancel/decision mutation仍要求当前writer generation。

**边界损失**

不会永久保存每次rejected/transport-level command outcome；但canonical mutation ACK unknown可恢复，且审计事实就在turn/queue/accepted-decision row上。自然幂等的stop/detach等命令可直接按当前state响应，不需要统一receipt table。

### P1-10：semantic de-gating不能提前释放仍被physical operation使用的资源

**目标方案中的缺口**

Stage 1正确地要移除audit/checkpoint/presentation对reply与close的“业务完成”要求，但旧稿同时写成close不再drain这些owner。Foundation尚未删除时，其executor、PostgreSQL cursor或artifact I/O仍可能使用session-owned pool/store；若close先释放依赖，会重现“logical owner已关闭、physical operation晚退”的事故类别。

**风险链**

~~~text
cache/audit不再是semantic gate
  -> close完全不等对应owner
  -> DB pool/artifact store先关闭
  -> in-flight physical operation晚到
  -> released resource被访问或task泄漏
  -> 为收尾再增加teardown generation/repair owner
~~~

**推荐**

Stage 1只删除success、catch-up、materialized、checkpoint high-water等semantic completion wait；同时停止这些subsystem的新admission，并在共享deadline内cancel/join所有仍使用session资源的physical task。audit默认关闭后不再创建新operation，已有operation只要求物理退出，不要求成功。直到Stage 3整个owner/executor被物理删除，对应close await才归零。超deadline后必须先隔离/终止其资源访问能力，再释放pool/store；不能通过后台abandon让task继续触碰session object。

**边界损失**

Stage 1的close await数不会立刻达到最终预算，极端I/O仍消耗bounded shutdown deadline；这是lifecycle safety，不是durability authority，也不需要stable candidate或repair owner。

### P1-11：Protocol v3必须可靠唤醒不推进transcript sequence的canonical control变化

**目标方案中的缺口**

旧稿让snapshot返回session lifecycle、turn、tool attempt与queue等canonical control state，却只用`latest_sequence`做level-triggered observation。queue accepted/cancelled/claimed、session closing、tool attempt insert，以及不伴随entry append的turn状态变化都可能不推进transcript sequence。当前v2之所以拥有独立control cursor，正是因为这些变化不等价于history append：[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1027)。

**风险链**

~~~text
删除Presentation control cursor
  -> Observe只比较transcript latest_sequence
  -> queue/turn/session/tool-attempt control-only transaction commit
  -> edge notification丢失
  -> entry sequence保持不变
  -> observer永远没有level-triggered refresh条件
  -> user-visible canonical control长期陈旧
~~~

**推荐**

在`sessions`只增加一个`control_revision BIGINT`当前值。每个由当前Host提交、用户可见、会改变canonical control view且没有靠新entry sequence可靠唤醒的transaction，在校验`writer_generation`后原子递增它。必须覆盖queue admission/cancel/claim、accepted interaction decision、session lifecycle、turn control mutation、`tool_execution_attempts` insert，以及任何会进入public attempt view的一次性remote-identity更新。terminal tool result本身append transcript entry，因而由entry sequence唤醒；turn interrupted不append entry时必须推进control revision。context snapshot/binding revision及current pointer属于context-semantic内部状态，不进入Protocol v3 public control view，也不递增control revision；context compiler与Inspector用bounded level read获取它们，下一accepted assistant entry仍由entry sequence唤醒。background worker progress、pending live interaction、spinner、transport或UI observation同样不得递增它。若一个transaction同时改变entry和control，仍可递增revision；它不增加第二个transaction。

canonical snapshot在同一read-only repeatable-read cut返回`latest_sequence`与`control_revision`。Observe request携带`observed_sequence`和`observed_control_revision`；server也必须在一个数据库statement或短read-only transaction的同一MVCC cut读取current pair，任一值更高即返回`snapshot_required`及该pair。LISTEN/NOTIFY或内存notification只负责提前唤醒，timeout与每次唤醒后都要重新读取这两个high-water。不得保存control transition history、per-section revision、fingerprint、receipt、checkpoint或consumer ACK。

**边界损失**

每个control mutation会多更新sessions row中的一个整数，可能增加极小的row contention；V1 single Host writer使其上界清晰。换来的不是第二authority，而是可丢notification之上的最小level-triggered同步条件。

### P1-12：V1必须选择pending interaction的唯一ownership

**目标方案中的矛盾**

最小durable truth和目标schema只保留accepted `interaction_decisions`，但旧稿的canonical snapshot又笼统承诺返回interaction state。当前代码同时把pending interaction放在short-lived working state中，[state.py](src/pulsara_agent/runtime/state.py#L118)，又用suspended run/transition owner恢复它，[session.py](src/pulsara_agent/host/session.py#L4183)。如果不做选择，实现者只能重新引入`interaction_requests`或旧resume graph来兑现snapshot承诺。

**风险链**

~~~text
approval/plan/MCP需要等待用户
  -> pending request没有canonical row
  -> snapshot仍承诺跨reconnect/cross-Host展示
  -> restart时只能重建旧suspended execution
  -> 新增request candidate/resume receipt/reconciliation
  -> transcript-only resume重新失效
~~~

**推荐**

V1明确选择process-local语义：pending approval、plan question、plan exit和MCP input request属于当前Host的live control。它保存`live_interaction_id`并可由同一Host上的level-readable live endpoint重新查询；TUI notification只是query hint。canonical snapshot不包含pending request，只包含已经accepted的interaction decision或其command target。

resolution必须同时携带当前`writer_generation`、`live_interaction_id`与稳定`command_id`。Host先在进程内确认该live request仍存在且匹配，再在一个数据库transaction中写`interaction_decisions`、执行session-wide command id幂等约束并递增`control_revision`。ACK unknown直接查询decision row。Host crash、writer takeover或close后旧live request消失，running turn变interrupted；旧resolution因generation或live id不匹配而失败，不构造旧RuntimeSession、不恢复provider/tool/MCP continuation。

如果未来产品明确要求“Host restart后继续回答同一request”，必须把它作为新的产品能力审议，并新增最小canonical `interaction_requests` row及其retention/security规则；V1不允许通过operational cache、suspended owner或隐藏compat path获得该语义。

**边界损失**

Host crash后用户需要在新turn重新触发approval/question/MCP input；已经提交的decision仍可审计，但不会复活旧execution。这是transcript-only resume与跨进程interaction continuation之间的明确取舍。

### P1-13：完整assistant tool-request message而不是单call才是execute前durable unit

**目标方案中的缺口**

旧稿只用one-tool四transaction描述tool loop。真实provider reply可以在同一assistant message中混合text与多个有序calls；当前`LLMMessage`和两个OpenAI adapter都支持该形状，[input.py](src/pulsara_agent/llm/input.py#L56)、[chat_completions.py](src/pulsara_agent/llm/adapters/openai/chat_completions.py#L509)、[responses.py](src/pulsara_agent/llm/adapters/openai/responses.py#L475)。Runtime还会并行执行一批calls并让results乱序完成：[agent.py](src/pulsara_agent/runtime/agent.py#L8692)。

**风险链**

~~~text
provider完成mixed/multi-call assistant message
  -> Runtime逐call commit并立即invoke
  -> call 1 effect发生，call 2/3尚未durable
  -> process crash
  -> transcript只剩非法半条provider message
  -> 无法按原ordinal重建provider input
  -> 为猜测缺失calls/results再引入recovery owner
~~~

**推荐**

一个completed assistant tool-request message使用stable`assistant_message_id`，把可公开text、全部call id/name/arguments、block ordinal与call ordinal在**一个PostgreSQL transaction**中完整提交；同一message内call id唯一，call row不可脱离parent message单独可见。任何tool invoke都必须发生在storage adapter已确认该message transaction committed之后；ACK unknown只按assistant message id读取唯一winner，不持久化FULL/receipt状态。

message commit只证明模型提出了call，不证明Runtime已经开始physical dispatch。每个需要实际invoke的call还必须先commit一个`tool_execution_attempts` row；closed pre-dispatch terminal（invalid arguments、permission denied、tool unavailable、cancelled before dispatch）可以直接产生无attempt的result。同一live turn只有在该message的每个call都有terminal result后才能发起follow-up provider call。context compiler按原call ordinal生成calls和对应results，不按result commit sequence或physical finish time排序。

crash在message commit前意味着没有任何call可invoke；message commit后，call没有attempt可证明未dispatch，attempt存在但没有result才解释为outcome_unknown。整个turn都变interrupted且不自动继续或重跑。一个tool round含N个calls时，正常上界是一次message commit + N次attempt commit + N次独立result commit；完整turn预算为`2 + tool-request message数 + 2 × tool call数`，one-tool为5。这个预算不能替代message atomicity或attempt-before-dispatch gate。

**边界损失**

一个tool-request transaction会比单call row更大，N个并行attempt/result会增加2N次commit；这是保留provider message原子性、dispatch ambiguity boundary与精确pairing的必要成本。不能为了压低transaction计数把message拆成可先执行的calls，也不能删除attempt而让“尚未dispatch”和“可能已产生effect”重新混为一谈。

### P1-14：semantic context snapshot不是普通可删除projection

**目标方案中的矛盾**

旧稿同时把completed compaction snapshot列入最小durable truth，又要求删除整表后语义不变。当前summary由模型生成，并会作为后续provider context的正文；相同source range重新生成不能保证相同语义。它与全文索引、UI projection不同，是来源可重建但结果不确定的semantic derived artifact。

另外，“每turn只有一个context binding”会删除已有mid-turn budget recovery：当前Runtime会在tool follow-up provider call前再次评估并触发compaction，[agent.py](src/pulsara_agent/runtime/agent.py#L3838)、[agent.py](src/pulsara_agent/runtime/agent.py#L5244)。首次model call可行不代表加入大tool result后仍可行；减法不应把这个合法safe-point能力退化为必然target-infeasible。

**推荐**

把目标关系命名为`context_snapshots`，而不是暗示纯性能cache的checkpoint。snapshot必须保存immutable bounded summary正文或全局blob reference、source sequence range/hash、snapshot schema、compiler/prompt/model contract与stable snapshot id。

snapshot创建本身不能回滚已经accepted的reply。未被任何binding revision采用的completed snapshot可以按固定retention删除；一旦某个running turn在provider safe point以immutable `TurnContextBindingRevision`采用它，该revision必须以外键引用exact snapshot，之后不得原地更新、重新生成替换或GC。新的compaction总是创建新snapshot id和新binding revision，不覆盖旧revision。

V1允许一个turn拥有有序、immutable的context binding revisions，并以`turns.current_context_binding_revision_id`指向下一次provider call要使用的revision。revision 0与user/turn acceptance同transaction安装；同一revision可跨多model calls复用。只有在provider safe point、旧model call已physical exit、上一assistant tool-request message已commit且其全部calls已terminal时，才能用writer generation在一个transaction中插入下一revision并推进current pointer。每条accepted provider-generated assistant message（tool-request或final）必须精确引用生成它的binding revision，并保存该次pre-dispatch固定conversation cut的`provider_input_through_sequence`；revision证明base，through sequence证明该次调用实际可见的delta upper bound。因此mid-turn compaction可为后续tool follow-up切换base，又不需要持久化ModelStart/ModelEnd。

每个revision的`source_through_sequence`必须严格早于该turn的accepted user entry，context rematerialization使用该revision的snapshot/full-history base + `(source_through_sequence, current provider cut]`的exact conversation delta，确保当前turn的user、assistant tool-request与tool result不被summary替代。若mid-turn snapshot生成失败，可继续复用当前revision或在合法时创建full-history revision；只有当旧history压缩后仍无法容纳当前turn exact delta时，才在provider dispatch前返回typed target-infeasible。已采用snapshot缺失或integrity失败是未来model admission的真实semantic continuity错误，但不得追溯否决旧reply、Host close或UI读取。

**边界损失**

被binding revision引用的snapshot不再满足“整表可删”测试，并会形成真实retention成本；这是模型生成summary参与后续决策的直接后果。可删除性仍适用于unreferenced snapshot、search index、presentation与diagnostic audit，不能推广到所有derived data。

### P1-15：tool intent与physical dispatch必须由attempt事实分开

**目标方案中的缺口**

assistant tool-request message只证明模型提出了操作；tool result只证明Runtime拿到了终态。二者之间没有记录physical dispatch ambiguity boundary时，call无result会把“等待approval”“尚未send”和“side effect可能发生”全部压成unknown。

**推荐**

新增窄的`tool_execution_attempts`关系。每次实际invoke之前，当前Host使用writer generation提交stable `attempt_id`、parent assistant message/call reference、effect/safety class、authorization subject/decision reference、actor、`dispatch_committed_at`、redacted argument digest，以及具体tool能预先生成的idempotency/launch key。storage确认attempt winner后才允许physical dispatch；commit ACK unknown只按attempt id读取canonical row，不建立candidate/receipt owner。若remote operation id只能在send后获得，只允许以`NULL -> exact value`的一次性conditional update安装，不能覆盖或充当dispatch-before证明。

结果row必须引用exact attempt；只有closed pre-dispatch terminal允许attempt reference为空，并带上述封闭reason。因而crash解释被严格冻结为：

~~~text
tool call, no attempt, no result       -> NOT_DISPATCHED / turn interrupted
tool call, no attempt, terminal result -> known pre-dispatch terminal
attempt, no result                     -> OUTCOME_UNKNOWN / never auto-retry
attempt, result                        -> known terminal outcome
~~~

V1冻结为一个logical call最多一个foreground physical attempt，由`UNIQUE(tool_call_id)`或同等外键约束保证。foreground attempt不保存started/terminal/unknown可变status：attempt row存在代表Runtime已commit dispatch ambiguity boundary；result row存在代表terminal-known；turn已interrupted且attempt无result时才派生outcome_unknown。可选remote operation identity只允许一次`NULL -> exact value`安装，不将attempt升级为可变执行状态机。

显式retry必须在新turn中产生新logical call，再为该新call创建其唯一attempt；新attempt可以用`retry_of_attempt_id`引用旧attempt并记录actor/reason，但不得挂在旧call下。V1不提供“原call直接再执行”的UI/Runtime命令；用户或新model turn只能inspect/abandon，或提出新call。这样一个call的唯一provider-visible `tool_result`永远只对应一个physical outcome，不需要再增“per-attempt terminal observation”第四层authority。V1仍不承诺外部exactly-once，attempt只是诚实记录Runtime跨过了可能产生effect的边界。

若旧attempt在turn interrupted后经显式、read-only effect reconciliation查明exact outcome，当前Host可以在该call尚无result时追加它唯一的`tool_result`；不能覆盖已有result，也不能改变旧turn的interrupted状态。result拥有自己的session entry sequence。对任一accepted assistant，以`result.entry_sequence <= assistant.provider_input_through_sequence`判断result是否位于该次conversation cut；不能使用assistant自身sequence或binding revision代替。若result晚于某个已经使用unknown closure的assistant cut，历史context attribution保持不变，新的result在未来provider input中按其实际sequence降级为typed late-effect observation，而不是倒插并改写已经发生的provider语义。这样旧attempt A与retry call B都可各自拥有唯一结果，但仍不需要同call多attempt或独立per-attempt observation表。

### P1-16：durable job aggregate不能覆盖physical attempt lineage

**目标方案中的缺口**

单个`durable_jobs` row中的mutable claim generation与attempt summary无法同时表达多次claim、remote operation id、显式retry与旧non-idempotent unknown。覆盖字段会丢失历史effect lineage，把它塞入JSON又会重新形成无约束复合authority。

**推荐**

拆成两个窄关系：

~~~text
durable_jobs
  immutable intent / safety class / aggregate state
  current attempt reference / accepted terminal result

durable_job_attempts
  attempt ordinal / claim generation / lease owner+expiry
  remote or idempotency identity / started / terminal / unknown
  immutable result or error reference / retry_of_attempt_id
~~~

每次可能执行physical work的attempt在dispatch前commit。一个job最多有一个active attempt，由PostgreSQL partial unique/conditional update保证；worker只按attempt id + claim generation更新attempt-owned progress/terminal状态。job aggregate与terminal attempt在同一transaction归并，但不能删除旧attempt。

`RETRY_SAFE`自动重执行也必须创建下一attempt并保留`retry_of`；`REMOTE_QUERYABLE`可由新claim继续查询同一remote identity但不能重新dispatch；`NON_IDEMPOTENT` lease丢失后当前attempt与job aggregate进入outcome_unknown。显式retry总是新attempt。它们仍不需要target head、stable candidate、result receipt或repair companion graph。

### P1-17：所有大内容应共享一个blob publication contract

**目标方案中的缺口**

删除queue artifact preparation hold后，prompt、tool result、job、context snapshot和memory仍可能引用大内容。若没有替代边界，canonical row可能引用尚未完成或已被GC的object；若各domain自行修补，则会再次长出五套hold/receipt/confirmation。

**推荐**

建立唯一purpose-neutral `BlobRepository`/`blobs`关系：

~~~text
immutable content-addressed write
  -> canonical digest + byte size validation
  -> canonical transaction locks/verifies blob row
  -> domain row installs ordinary FK reference
  -> ON DELETE RESTRICT is final retention guard
  -> unreferenced blob older than fixed orphan grace may be GC
~~~

若bytes与canonical row都在PostgreSQL，可在同一transaction写入；若大对象必须先写，预写只产生unreferenced content-addressed blob，不产生durable domain hold。V1 orphan grace固定为24小时；GC只枚举超过grace且当前无任何FK引用的blob，并让数据库约束处理与late canonical install的竞态。所有consumer验证同一digest/size/codec contract，不再各自证明publication。

### P1-18：interaction decision必须绑定durable subject且隔离secret

**目标方案中的缺口**

pending request是process-local，但accepted decision若只保存`live_interaction_id`，Host退出后就失去可审计subject。MCP form/URL或external input又可能包含不应进入普通durable row的secret，不能因为decision需要幂等就保存可恢复明文。

**推荐**

`interaction_decisions`使用closed subject union：approval引用durable assistant tool call；plan resolution在一个command-addressable transaction中同时创建canonical user/conversation item与引用该item的decision；MCP/external secret decision引用对应durable tool call/attempt，持久化accepted/denied/cancelled disposition、redacted subject与必要keyed commitment，不保存secret value或可恢复sealed response。同一logical resolution只有一条session-wide command identity，不能让conversation item与decision各自竞争unique command id。

secret plaintext和revocable handle始终属于当前Host process-local owner。ACK unknown查询只返回“该command的decision已accepted/denied及subject identity”，永远不返回secret。若Host在decision commit后、physical consumer使用secret前crash，turn interrupted；新Host不会恢复值、继续旧MCP operation或要求数据库解密它。

### P1-19：binding revision必须与每次provider conversation cut分层

**目标方案中的缺口**

binding revision描述可跨多个model call复用的base、snapshot与compiler/lowering contract，不描述某次physical provider dispatch读取到的exact conversation delta upper bound。若用assistant自身`entry_sequence`或共享revision推断输入，会出现确定性错误：provider在sequence 100冻结输入并开始stream，late tool result随后commit为101，assistant最后commit为102；模型没有看见101，但`assistant.sequence=102`会让实现误判它已经使用该result。

**推荐**

每条accepted provider-generated assistant entry必须同时保存：

~~~text
context_binding_revision_id
provider_input_through_sequence
~~~

`provider_input_through_sequence`来自physical dispatch前的固定conversation read cut，并由process-local、immutable prepared-input handle原样携带到assistant commit；caller不能在commit时重新读取latest sequence或自行填写。该handle只在当前provider operation存活，process crash后直接消失，不持久化ModelStart、ModelEnd、provider request、candidate、receipt或operation journal。assistant commit验证revision属于同一turn、cut不早于该turn user entry且严格小于新assistant entry sequence；tool-request与final assistant都遵守同一规则。

这不会增加新的row、transaction或后台owner：两个字段随既有accepted assistant transaction写入。它也不声称保存历史provider request的逐字bytes；revision证明semantic base/lowering contract，through sequence只证明本次conversation entry upper bound。

provider safe point只保护input构造、binding revision推进与下一次physical dispatch准入；它不是session-wide canonical write lock，也不得把数据库锁或排他的semantic-write lease从pre-dispatch read持有到provider stream结束。late result等canonical mutation可以在模型运行期间按各自authority正常commit，`provider_input_through_sequence`是accepted assistant判断这些mutation是否属于本次conversation cut的唯一因果边界；实现不得依赖“模型运行期间没有其他semantic write”来省略该字段或推断历史可见性。

这个标量能够成为exact set boundary，前提是session entry sequence按**commit order**分配：canonical entry transaction先锁定/conditional-update session high-water，再取`latest_sequence + 1`、插入row并在同一transaction推进high-water。禁止在transaction外预留sequence、使用可能乱序commit的non-transactional `nextval`，或让晚提交row取得不大于已发布high-water的sequence。parallel tool execution仍可并发，但canonical result commits在这一窄sequence分配点串行化；rollback不发布entry或high-water。于是pre-dispatch cut为H后，未来commit的任何entry都严格大于H。

late-effect判定冻结为：

~~~text
result.entry_sequence <= assistant.provider_input_through_sequence
  -> result位于该assistant的conversation cut内；是否进入具体wire位置由binding revision的versioned lowering contract决定

result.entry_sequence > assistant.provider_input_through_sequence
  -> result对该assistant确定为late outcome
~~~

若已有accepted assistant的cut早于late result，历史attribution永不改写；未来provider lowering按result实际sequence生成typed late-effect observation。若未来provider输入出现会影响此类correctness判断、但既不属于binding revision也不由entry sequence覆盖的dynamic semantic state，届时才把这两个字段提升为窄closed `ProviderSemanticInputCut`；V1不预建通用fact、fingerprint graph或provider lifecycle journal。

### P2-1：context-input exact audit从可选诊断放大为默认 durability

**当前机制**

每个 model call自动 plan/pages/root materialize、read-back、永久保留，并在 Host/non-Host close drain。

**原始需求**

doctor能解释某次 provider input由哪些 source component构成，避免主事件过大。

**amplification 链**

见 2.8 第二条完整链。

**删除/降级后损失**

- 未采样 call不能逐 byte复盘 source layout；
- 只能从 transcript/request metadata重建 semantic view；
- 某些低层 compiler bug调查信息减少。

**推荐**

默认关闭；只由显式 doctor、debug session、采样或合规策略启用；设TTL；close可放弃业务成功，但owner存在时先bounded physical quiesce；若产品没有逐次exact audit承诺，则删除整个 audit plane，只保留 request hash、compiler version和token count作为 operational metadata。

### P2-2：derived projection job被过度普遍化

**当前机制**

timeline/evidence/notification/presentation等消费者拥有 durable jobs、lease、retry、dead-letter、repair CAS和close dependency。

**原始需求**

后台派生数据最终可用，失败可恢复。

**amplification 链**

~~~text
derived view最终可用
  -> durable job
  -> seed/outbox/horizon
  -> lease/retry/dead-letter
  -> target confirmation
  -> repair/decommission
  -> Host close physical-owner safety
~~~

**删除后损失**

- UI/timeline cache在失败后可能延迟或重建；
- 非关键 analytics不再有逐条 exactly-once materialization。

**推荐**

只保留明确跨 Host 生命周期、产品承诺 eventual completion 的 job：terminal monitor notification、compaction/memory extraction、真正后台 subagent。普通 reply、foreground tool loop、TUI projection和可同步查询的 evidence不进入 durable job system。

### P2-3：reservation/account覆盖过多普通 I/O

**当前机制**

最小 text turn写7对 physical reservation/settlement和一组 rollout/ledger account；one-tool写14对 physical reservation/settlement。

**原始需求**

容量控制、资源核算、crash后识别未结算操作。

**amplification 链**

~~~text
限制资源
  -> durable account
  -> per-operation reservation
  -> charge/settlement
  -> active reservation recovery
  -> RunEnd要求全settled
  -> resume/close都需恢复account
~~~

**删除后损失**

- 无法对每次内部 DB/artifact I/O做跨进程精确核算；
- crash时process-local permit自然消失。

**推荐**

并发、内存、连接池、stream buffer使用process-local semaphore/budget。只持久化具有产品账务意义的 token/cost usage汇总，以及 durable job的粗粒度attempt计数。不要对每个普通写入建立 ledger reservation。

### P2-4：publication/TUI observation拥有反向否决权

**当前机制**

critical publication unavailable可以 latch Runtime；presentation、queue delivery和notification都进入 close。当前 wire protocol major 为 2：[codec.py](src/pulsara_agent/terminal_protocol/codec.py#L70)。Gateway 的 detach 会释放 Presentation Foundation retention-root lease，snapshot 会借用 confirmed root并携带 control cursor/rebase，history page 会围绕 root/cursor/outcome读取；Go 客户端还校验 active head、confirmed root、root/cursor pair与resident entries，并维护root-indexed cache。这是一套完整的durable presentation contract，而不是canonical transcript的薄查询层：[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L390)、[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L447)、[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L1717)、[carriers_gen.go](clients/terminal/internal/protocolvalue/carriers_gen.go#L864)、[s2_state_test.go](clients/terminal/internal/presentation/s2_state_test.go#L1)。

**原始需求**

用户不能看不到已经完成的结果；TUI重连要一致。

**amplification 链**

~~~text
可靠显示
  -> durable presentation/notification
  -> publication acknowledgment
  -> critical latch
  -> mainline fail closed
  -> publication repair/close drain
~~~

**删除后损失**

- live push可能漏一次；
- TUI需在 reconnect后查询 transcript snapshot。
- 现有 root/cursor/page/GAP wire contract 与 Go cache 不能原样复用，需要一次 Protocol major 迁移。

**推荐**

canonical reply commit后Runtime即成功。第一轮 foreground hard cut 同时发布新的 transcript application service、snapshot/page/sequence cursor 语义、Protocol major 与 Go sequence-indexed cache；丢通知或 GAP 时重拉 committed transcript。删除 root identity、projection contract fingerprint、retention-root lease 和 continuity receipt 的 semantic authority；UI永不成为 reply/RunEnd gate。具体 Protocol v3 边界见 7.3。

### P2-5：semantic context snapshot不能拥有改写transcript epoch的权力

**目标方案中的冲突**

context snapshot一旦被任一turn binding revision引用就是semantic derived authority，但它仍不拥有canonical conversation retention。若compaction能删除、重写或重排canonical transcript，它会同时控制provider语义与用户history边界，重新形成双重authority。

**风险链**

~~~text
缩短long-horizon context
  -> compaction snapshot
  -> compaction顺便删除/重写transcript
  -> transcript epoch推进
  -> reconnect cursor失效/GAP
  -> snapshot同时成为解释旧history与provider context所必需
  -> semantic context与conversation retention耦合
~~~

**推荐**

V1 compaction只追加completed context snapshot，记录immutable source sequence range/hash与生成contract；它不删除、不改写、不重排`transcript_entries`，也不推进`transcript_epoch`。context compiler可以在turn的binding revision引用后用exact snapshot替代旧entries进入provider request，但TUI/Inspector仍查询完整canonical transcript。epoch只因database reset或显式产品retention transaction变化；V1默认不做canonical transcript retention。未来retention必须单独定义用户可见策略、lower bound、export/audit与GAP语义，不能借compaction实现。

**边界损失**

V1不会仅靠compaction回收transcript row存储；需要分页、索引和独立retention产品决策。unreferenced snapshot可删除，但被binding revision引用的snapshot必须随该revision保留；缺失时不能悄悄重新生成不同summary。

### P2-6：停止把rehydrate、rematerialize、reconcile与replay混为一谈

**目标方案中的缺口**

删除historical event decoder是正确方向，但“resume/replay”仍被用来描述conversation读取、context构造、effect查询与审计复现。它们的稳定性承诺完全不同；若不拆名，implementation很容易重新为“replay”保存provider bytes、coroutine transition或consumer receipt。

**推荐**

冻结四个公开概念和一个明确禁项：

| 行为 | V1承诺 |
|---|---|
| Conversation rehydrate | 读取并验证所有accepted canonical conversation facts；保证 |
| Context rematerialization | 按versioned compiler、exact binding revision/context snapshot与本次固定conversation cut构造新输入；不保证逐字复现历史provider request |
| Effect reconciliation | 只查询已有tool/job attempt、remote identity与result；默认不重新执行 |
| Audit reproduction | 显式debug、采样或合规模式；不属于正常resume |
| Execution replay | 明确不支持；不恢复model/tool coroutine、transport cursor或pending interaction |

canonical row使用closed kind与小型schema version。关系型字段演进通过SQL migration rewrite；确有历史payload时只允许per-domain、有限版本的upcaster registry。未知future version fail closed，旧version必须有golden fixture。不得保留universal AgentEvent historical decoder，也不得依赖“当前binary碰巧还能解析旧JSON”。

### P2-7：Stage 2冻结单次production activation，不冻结单个巨大PR

**目标方案中的误读风险**

foreground、resume、context、Inspector与Protocol v3必须在第一次production写新authority时同时可用；这不等于数千行schema/Python/Go迁移只能在一个无法独立审查的提交中完成。

**推荐**

允许多PR dormant construction，但只允许一次production authority activation：

~~~text
new schema + repositories, production composition disabled
  -> fresh-DB test-only conversation runner
  -> context / Inspector / query readers
  -> Protocol v3 Python + Go consumer
  -> isolated fresh-DB dogfood
  -> one reset + production activation
  -> delete old EventLog execution graph
~~~

每个construction PR必须独立全绿，且dormant代码不能被普通Host配置、feature flag或session metadata激活。全过程不dual-write、不让同一session混用authority、不建立online translator。真正的coherent cut gate放在activation release，而不是用“单个PR”替代架构原子性。

### P3-1：fingerprint、reference、receipt超出信任边界所需

**当前机制**

同一 process内大量 immutable DTO也生成 fingerprint，并层层校验 plan、candidate、receipt、join、document、head和binding identity。

**原始需求**

防止错误对象重绑、跨进程 payload drift和幂等冲突。

**删除后损失**

- process-local编程错误少一层运行时检测；
- 跨存储边界仍需 DB key、content hash或版本。

**推荐**

只在真正跨进程/跨表/内容寻址边界保留 hash：artifact integrity、external idempotency key、compaction source range。process-local owner identity、attempt generation、presentation join、repair plan/receipt使用类型和对象引用，不进 durable schema。

### P3-2：event vocabulary保存 UI、transport、timing与运行归因

**当前机制**

151类 event混合产品事实、模型transport布局、projection lifecycle、accounting、debug attribution与UI foundation。

**原始需求**

完整 replay、Inspector解释、确定性重建。

**删除后损失**

- 无法逐 segment重放原流式动画；
- 某些精细 timing/归因只在日志或trace中保留；
- 旧 Inspector页面需重写。

**推荐**

durable product vocabulary压至24以内。stream layout、delta batching、checkpoint attempt、repair attempt、delivery tick、observer state进入 structured operational log/trace，明确不参与 resume和semantic gate。

---

## 6. 三个候选目标架构

### 6.1 方案一：保守减法——EventLog core保留

**durable truth**

- 保留 PostgreSQL EventLog作为 authority；
- 合并 Run/Reply/Model lifecycle event；
- 保留 canonical user/assistant/tool/turn events；
- 保留少量 long-horizon/subagent/job events；
- checkpoint全部 soft化。

**crash semantics**

- model crash写 interrupted；
- 不恢复 delta；
- 仍可用 EventLog查旧 run并补一条 coarse terminal event。

**tool side effect**

- call-before-execute、result-after-return；
- outcome_unknown不自动重试；
- 保留一个简化 tool execution owner。

**resume**

- 从 EventLog重建 transcript；
- 不恢复 model stream/control；
- 可保留单一 reopen repair把 running run转 interrupted。

**close**

- 4阶段，目标不超过24个await；
- 保留 EventLog writer/reducer flush，但删除 acceleration drain。

**migration**

- reset-only或一次 event vocabulary hard cut；
- 仍需保留 EventLog serialization、replay和部分 reducer基础设施。

**优点**

- 实施风险最低；
- Inspector与long-horizon较易复用；
- 删除现有复杂 owner时可沿当前 event seam纵切。

**缺点**

- universal event abstraction仍会诱导新 transition event；
- transcript仍是 projection而非直接 schema；
- 很难把表数和 vocabulary压到真正小的范围。

### 6.2 方案二：中等 hard cut——relational conversation kernel + selective journals

**durable truth**

PostgreSQL直接保存：

1. sessions，含writer lease、transcript epoch与单一control revision；
2. turns，含client command/submission identity；
3. transcript_entries及原子隶属于assistant message的有序text/tool-call blocks；每条accepted provider-generated assistant entry还保存exact context binding revision与该次pre-dispatch固定的`provider_input_through_sequence`；
4. unique-per-call tool_execution_attempts及exact tool results；
5. accepted interaction_decisions；pending request不落库；
6. durable_jobs + durable_job_attempts；
7. prompt_queue_items；
8. immutable context_snapshots + turn-local context binding revisions；unreferenced可GC，被revision引用后是semantic derived authority；
9. memory_facts/governance lineage；
10. subagent_tasks/messages/results；
11. purpose-neutral blobs与integrity metadata。

不是每项都必须独立表；原则是每个 row表达一个产品事实，而不是某 reducer是否处理过。

**crash semantics**

- running model/tool foreground turn在open时变 interrupted；
- 未 accepted delta丢弃；
- 不恢复旧 coroutine、transport、control disposition或finalization candidate。

**writer与authority切换**

- V1 每个 session 同时只允许一个 Host writer；DB generation/lease只fenceHost-owned foreground/session-control mutation；
- background job attempt claim/progress/result只由独立claim generation保护，worker不能直接写transcript；
- 当前Host以writer generation显式接受job result后，结果才进入conversation；
- observer attachment可以有多个，但 controller命令只通过当前 Host writer提交；
- 第一次production activation一次覆盖全部foreground item、最小rehydrate、context/Inspector、TUI Protocol major与minimal job kernel；所有foreground-reachable background capability已迁移或明确禁用，此前只允许dormant construction；
- 不按模型最终是否选择 tool 分流，不让同一 session 出现 EventLog/new transcript 双 authority。
- Protocol v3 canonical read使用repeatable-read cut；Observe同时比较entry sequence与单一session control revision；client mutation ACK unknown直接按canonical target row和session-wide command id恢复；
- pending interaction只属于当前Host live control；同Host reconnect可重新query，Host crash/takeover后消失并令turn interrupted。

compaction只追加context snapshot/binding revision，不删除/重写transcript、不改变epoch；turn可在provider safe point追加revision以支持mid-turn budget recovery，每条accepted assistant message绑定exact revision与该次provider call固定的conversation cut，canonical transcript retention在V1默认关闭。

**tool side effect**

- completed assistant tool-request message的mixed text与全部ordered calls在一次transaction完整commit后，才允许任何call执行；
- 每个physical invoke前先commit exact tool execution attempt；call无attempt可证明未dispatch，attempt无result才是outcome_unknown；
- 每logical call最多一foreground attempt；显式retry必须为新turn/new call，新attempt只做cross-call retry attribution；
- parallel tool return可按完成顺序分别commit result，但每个result精确绑定parent assistant message/call id与attempt id；
- follow-up provider call等待该message全部calls拥有terminal result，并按原call ordinal lowering；
- call无attempt = not_dispatched；attempt无result = outcome_unknown；
- UI默认禁用一键自动重试，要求显式确认；
- tool-specific idempotency key可选，不升级为通用 exactly-once。

后台 durable job 同样不承诺通用 exactly-once：job row保存intent/aggregate，job-attempt row保存每次claim、remote identity与retry lineage；只有显式 retry-safe handler可在 lease expiry后创建下一attempt自动重试；可查询远端状态的 monitor先重新观察；非幂等 attempt直接进入 outcome_unknown。

**resume**

- load session + transcript + pending prompt/job；
-把残留 running turn幂等置 interrupted；
- 不加载或重建pending interaction request；accepted decision保留为canonical audit，旧live request消失；
- 需要自动继续时，创建新的 synthetic/user-visible turn，而不是复活旧 run。

**close**

3阶段：

1. stop ingress；
2. bounded cancel/join foreground及仍使用session资源的physical operation，写 interrupted/unknown；
3. flush Host-owned transcript/tool/queue/job-control authorization commits并关闭session资源；background worker/claim独立存活。

**migration**

- 推荐 reset-only hard cut；
- 不做 old EventLog → new transcript双写；
- 不建 compatibility reducer；
- 允许以多个独立全绿PR构建production-disabled schema/repository、fresh-DB runner、readers与Protocol v3；只有最后一次reset + activation release可把新authority接入普通Host；
- 如必须保留用户数据，只做一次离线 export/import，将可证明的 accepted facts投影到新 schema；不能证明的 running transition统一 imported_interrupted。

**优点**

- 保留 Pulsara独有产品能力；
- 明确删除大多数 repair、checkpoint、confirmation和close graph；
- PostgreSQL事务仍提供强 canonical commit；
- side effect语义可理解、可审计。

**缺点**

- 是一次真实 hard cut；
- 第一个 foreground production activation 不能拆成 text/tool/resume/TUI 四次上线；但它们可以在此前用多个dormant construction PR协同建设和测试；
- 放弃已有 event-level replay与精确内部历史。

### 6.3 方案三：激进 transcript-first——append log/file为主

**durable truth**

- 每 session一个 append-only JSONL/SQLite transcript；
- user/assistant/tool/compaction/interrupted少量 record；
- durable jobs另用极小 SQLite/Postgres queue；
- memory/subagent结果作为 transcript或artifact。

**crash semantics**

与方案二相同，但不承诺数据库级多设备协调。

**tool side effect**

与方案二相同；本地 append前后边界。

**resume**

读文件/SQLite，截断最后损坏record，继续新 turn。

**close**

2阶段：cancel foreground；flush/fdatasync并停资源。

**migration**

reset-only；基本删除 PostgreSQL EventLog、projection jobs和大部分 schema。

**优点**

- 最小延迟和最少代码；
- 极易理解；
- 与本地 coding agent工作负载相符。

**缺点**

- 与 Pulsara已有PostgreSQL、多Host/后台能力和治理需求冲突最大；
- durable prompt queue、terminal monitor、subagent协调、memory governance仍会迫使再引入数据库；
- 很可能最终形成“JSONL transcript + PostgreSQL jobs/memory”双 authority；
- 实施与数据迁移风险最高。

### 6.4 评价矩阵

评分中 5 表示该维度最好；“exactly-once承诺强度”不按越强越好评分，而是列实际承诺。数量是目标架构的**预算与审查阈值**，不是 correctness gate，也不是第一阶段即可达到。不能通过把不相干类型塞进巨型 JSON row、合并无关业务类型或把代码搬到生成文件来“满足数字”。

| 维度 | 当前 | 保守减法 | 中等 hard cut | 激进 transcript-first |
|---|---:|---:|---:|---:|
| 正常 reply 延迟 | 2/5 | 3/5 | **5/5** | 5/5 |
| steady-state text turn durable transaction（无新compaction） | 至少15 write scope | 5–7 | **2** | 1–2 |
| steady-state one-tool durable boundary/write（无新compaction） | 至少31 | 10–14 | **5** | 4 |
| durable event vocabulary | 151 | 60–80 | **≤24** | ≤12 |
| 产品 SQL tables | 61 | 35–45 | **≤24** | ≤12 |
| text/tool owner family | ≥14 / ≥17 | 8 / 11 | **3 / 5** | 2 / 4 |
| restart branch family | ≥8 | 4–5 | **2–3** | 1–2 |
| Host close | ≥6 bands、45 awaits、4 barriers | 4 phases、≤24 awaits | **3 phases、≤12 awaits** | 2 phases、≤8 awaits |
| exactly-once承诺 | 意图强，外部端到端不可证 | storage commit强；外部不承诺 | **canonical DB commit强；外部明确不承诺** | local append强度；外部不承诺 |
| crash后用户体验 | 隐形repair，可能hang | interrupted较明确 | **明确interrupted / outcome_unknown** | 明确但协作能力较弱 |
| side-effect重复风险 | 机制复杂但外部仍有unknown | 中 | **中低：默认禁自动重试 + 审计** | 中 |
| 可观测性 | 细但噪声极高 | 高 | **高：产品审计 + operational trace分层** | 中 |
| 实施风险 | — | 3/5 | **2/5；首个authority cut跨DB/Python/Go，前后仍可vertical减法** | 1/5 |
| 预计净删生产代码 | — | 8k–14k | **≥22k** | ≥30k |
| 长期维护成本 | 1/5 | 3/5 | **5/5** | 4/5，但双authority风险 |

表中的5只指一个tool-request message、一个attempt、一个result、一个final的样本。设B为tool-request messages、C为全部calls/terminal results、E为实际physical attempts（`0 <= E <= C`），完整turn精确预算为`2 + B + C + E`，上界`2 + B + 2C`；单round N calls的上界是`2N + 3`。closed pre-dispatch terminal没有attempt，因此不会被错误收取E。预算不得覆盖“message先原子commit、attempt先于dispatch、全部result terminal后才follow-up”的correctness gate。

代码删除量是基于当前 owner文件和调用面的 inventory target，不是未经实施即可保证的精确 LOC。`≤24` tables/EventType 与净删 `≥22k` LOC 用于暴露架构回弹、触发审查和衡量方向，不得决定数据正确性或上线安全；correctness 由单 authority、commit、fencing、crash、side-effect、resume 与 reconnect 行为 gate 决定。

### 6.5 推荐选择

只推荐 **方案二：中等 hard cut**。

原因不是它“最完整”，而是：

- 方案一删得不够深，保留 universal EventLog会持续诱发新 durable transition；
- 方案三对 Pulsara真实的后台 job、subagent、memory governance和多进程 PostgreSQL foundation删除过度，后续很可能再造第二 authority；
- 方案二把 durability集中到canonical conversation、physical attempt journal、semantic context snapshot和真正后台 job，正好覆盖产品价值高、跨进程不可丢的事实；
- 它允许 PostgreSQL继续提供原子 commit、查询、并发和治理，又不要求所有 execution transition都成为 event-sourced transaction。

---

## 7. 推荐方案：中等 hard cut

### 7.1 架构原则

推荐架构是四层，并使用一个共享blob boundary：

~~~text
canonical conversation facts
  accepted commands / turns / conversation items
  tool calls / execution attempts / results
  interaction decisions / revision-referenced context snapshots / memory facts

durable work journals
  jobs / job attempts / leases / immutable results

mutable coordination
  session writer lease / prompt queue head / current turn status

disposable derived planes
  presentation / search / indexes / sampled audit / telemetry

shared blob publication
  immutable content-addressed bytes / canonical FK / orphan GC
~~~

依赖方向只能向下读取：

- process-local execution提交canonical conversation fact或durable work attempt；
- UI/Inspector读取 canonical truth并可消费 operational telemetry；
- operational failure不能反向改变 canonical fact；
- background job只以自己的claim generation更新job/attempt-owned row或blob，不携带Host writer generation、不直接写session transcript；
- job result进入conversation必须由当前Host以writer generation显式接受；
- 不建立“projection确认 canonical truth”“UI receipt确认 Runtime成功”之类反向边。

### 7.2 冻结的 18 项决策

#### 决策 1：最小 durable truth

必须持久化：

1. accepted user input及其稳定`command_id`/`client_submission_id`，用于canonical-row级submit幂等；
2. accepted final assistant reply；
3. 已经向模型/用户公开的completed assistant tool-request message：stable message id、可公开text与全部有序calls；单call不是这个边界；
4. physical dispatch前提交的tool execution attempt：stable attempt id、unique call subject、effect class、authorization/actor、时间与可用remote/idempotency identity；每logical call最多一attempt；
5. tool返回后向模型公开的完整 result，精确绑定parent assistant message、call id、call ordinal与physical attempt；只有closed pre-dispatch terminal可使用无attempt result union；
6. turn/session 的 running、completed、interrupted；
7. durable prompt queue item与顺序；
8. accepted approval/plan/MCP/external-input interaction decision及其command id、durable subject与redacted/commitment boundary；pending request与secret本身不在V1 durable truth中；
9. 真正后台job的immutable intent/safety class/aggregate terminal state，以及每次job attempt的claim、lease、remote identity、result/error与retry lineage；
10. immutable context snapshot的bounded summary、source range/hash与compiler/prompt/model contract，以及turn-local immutable context binding revisions；每条accepted provider-generated assistant message保存exact revision attribution与pre-dispatch冻结的`provider_input_through_sequence`，二者共同证明base和本次conversation delta cut；未采用snapshot可GC，被revision采用后必须保留exact identity与正文；
11. subagent task/message/result/completed/interrupted；
12. memory governance的长期事实和显式 lineage；
13. global blob本体与integrity metadata；canonical domain只保存受外键保护的blob reference；
14. session 当前 Host writer generation、lease owner与expiry，用于跨进程 fencing；
15. durable job attempt当前 claim generation与lease，用于拒绝 stale result，而不是恢复 handler内部执行；
16. session transcript epoch与retention lower bound；V1中它们只由reset或显式retention改变，不由compaction改变；
17. session当前`control_revision`标量，只用于唤醒未推进entry sequence的canonical control变化，包括tool attempt insert、public remote-identity update与turn interruption；不保存transition history或consumer position；
18. canonical closed payload的domain schema version，以及binding revision/context snapshot/compiler contract所需的version identity。

不持久化“某个消费者已观察上述事实”的证明，除非该观察本身是用户承诺的后台工作。

#### 决策 2：明确改回 process-local 的状态

- raw provider stream与所有 delta；
- text/thinking/data/tool argument的transport segment和batch layout；
- ModelStart/ModelEnd attempt；
- ReplyStart/ReplyEnd attempt；
- control disposition与execution permit；
- provider-input generation coroutine；
- context compilation中间树、source page、live cursor；
- foreground tool future、suspension/terminal candidate owner；
- pending approval/plan/MCP input request、live interaction payload与其等待future；同Host可查询，跨Host不恢复；
- physical operation permit/reservation；
- rollout/token preflight reservation；
- reducer live high-water、post-fold receipt；
- checkpoint candidate/head/retry；
- publication/UI delivery state；
- Host close ordering state；
- temporary recovery session；
- child RuntimeSession teardown generation/retry task；
- process-local fingerprint、executor/coroutine attempt id和waiter identity；这里不包括durable tool/job physical attempt id。

#### 决策 3：Model stream crash后的唯一语义

**turn = interrupted；未 accepted delta全部丢弃；旧 model call永不跨进程继续。**

如果用户已经看过 live partial text，TUI在重连后显示：

> 上一次回复在生成中断；未完成内容没有保存。

可选地把 partial text送到 operational crash log，但不得进入下次模型 context、不得冒充 assistant reply。

如果crash发生在pending interaction期间，pending request随旧Host消失，turn同样变interrupted；accepted decision若已commit则保留，但既不恢复request，也不继续旧execution。

#### 决策 4：tool产生 side effect、final reply未提交

对一个已完整commit的assistant tool-request message，逐call区分可证事实：

- 有 durable tool result：rehydrate时 conversation包含 call/attempt/result；turn标 interrupted；新 turn可基于 result继续生成 final reply，不重跑 tool。
- 有 durable call、没有attempt、没有result：证明Runtime没有跨过physical dispatch boundary；显示not_dispatched/interrupted，不得伪称side effect unknown。
- 有 durable attempt、没有result：显示 outcome_unknown；禁止自动重跑；用户或模型必须在新 turn显式选择 inspect/abandon，或以新call表达retry intent。
- closed pre-dispatch terminal result：允许无attempt，但reason只能是invalid_arguments / permission_denied / tool_unavailable / cancelled_before_dispatch，并精确绑定call与相关decision subject。

mixed/multi-call message必须作为完整message保留。已commit results、attempt-without-result与call-without-attempt可以并存；每个call只按自己的attempt/result事实解释，不能因为同batch另一个call成功就推断它已执行或未执行。无法从本地数据库证明外部 effect是否发生时，绝不自动写“failed”或“not executed”。

下一个新turn不得把上述悬空tool call原样发给provider。`ContextRematerializer`必须在provider lowering层使用唯一、确定性、versioned的interruption closure：已知result按原call ordinal精确降级；call无attempt时生成`interrupted_before_dispatch`的provider-only closure unit；attempt无result时生成`outcome_unknown_do_not_retry`的provider-only closure unit并绑定attempt id。它们只用于满足各provider的call/result闭合协议，不是canonical `tool_results`、不追加transcript row、不声称获得了外部返回值、不授权自动retry。只有在原assistant message的全部calls按ordinal形成合法provider-visible closure后，才能附加新user/continuation item并开始新model call。

#### 决策 5：accepted final reply的唯一 commit point

assistant tool-request message拥有一个独立但非final的commit point：stable`assistant_message_id`、可公开text及全部tool calls/ordinals在一个transaction中插入，turn保持running或waiting。任何invoke必须等storage adapter确认该完整message已经commit；不存在逐call先执行的入口，也不为commit confirmation建立durable FULL状态。

必须冻结的逻辑约束是：assistant message id在session内唯一；同parent的block ordinal与call ordinal各自唯一且immutable，call ordinals覆盖provider给出的固定顺序；`(assistant_message_id, tool_call_id)`唯一；terminal result以这个pair为外键且最多一个。parent与全部blocks/calls必须all-or-nothing可见。物理schema可以用child rows或严格typed bounded payload，但不能弱化这些约束。

每个实际invoke还拥有一个独立的physical-attempt commit point。attempt row在dispatch前写入并受`attempt_id`、call FK与writer generation约束；storage ACK unknown只能exact query该row。只有attempt commit被确认后Runtime才可调用tool adapter。这个row不是对remote receive的证明，也不需要后续confirmation state；它只标记“从此以后local crash无法证明effect未发生”的保守边界。tool result transaction引用exact attempt，或使用closed pre-dispatch terminal branch。

一个 PostgreSQL transaction：

1. INSERT assistant transcript entry，使用 stable entry_id和turn_id唯一约束；
2. UPDATE turns SET status = completed, final_entry_id = ...；
3. commit。

该 transaction成功即 accepted。TUI notification、projection、checkpoint、RunEnd alias、final-output materialization都不是 commit条件。

如果 connection在 commit后断开、ack unknown，只允许 persistence adapter按 stable entry_id读取已经提交的唯一 canonical winner；这是唯一共享的 storage uncertainty处理，不创建 compatible-winner state或 domain-specific repair owner。

user acceptance使用对称但更简单的边界：client在retry时复用同一`command_id`，canonical command-addressable action/target row保存它，数据库在session范围执行`UNIQUE(session_id, command_id)`；turn或prompt queue item直接是该row，或引用同一canonical base row。相同command id和相同typed input返回已有turn/queue target；相同id但text、delivery mode或其他semantic input不同则返回conflict，不写第二个target。query直接读canonical row。`client_submission_id`可同时保留用于客户端本地submission identity，但不得再要求通用`terminal_command_receipts`、receipt revision、query token或confirmation state。

#### 决策 6：是否保留 RunStart/ModelStart/ModelEnd/ReplyEnd/Disposition/RunEnd全套

**不保留。**

- RunStart → turns row + accepted user entry；
- RunEnd + ReplyEnd → turns.status/final_entry_id或 interruption_reason；
- ModelStart/ModelEnd → operational span；
- ReplyStart → 无；
- control disposition → process-local branch decision；
- model usage → assistant entry/turn上的附属 usage summary；
- provider错误 → interrupted/error summary，必要时 operational log。

若 Inspector需要“第几次 model attempt”，从 trace读取；它不是 resume authority。

#### 决策 7：event删除、合并、operational降级

**删除或不再作为 durable product event：**

- ModelCallStartEvent；
- ModelCallEndEvent；
- ModelCallTerminalProjectionCommittedEvent；
- ModelCallControlDispositionResolvedEvent；
- ReplyStartEvent；
- ReplyEndEvent；
- ProviderInputGenerationStartedEvent；
- ProviderInputAppendCommittedEvent；
- ContextCompiledEvent（最终仅保留可选 request hash/compaction ref字段）；
- ProjectionRequestedEvent / ProjectionReadyEvent / rewrite-page lifecycle；
- Text/Thinking/Data Block Start/Segment/End；
- PhysicalOperationReservationCreated/Settled/ChargeApplied；
- LedgerMaterializationAccountGenesis/ConsumerRegistered/HorizonAdvanced；
- per-turn SubagentGraphCheckpointCommitted；
- checkpoint/repair/publication attempt event；
- terminal presentation delivery event。

**合并：**

- RunStart + user transcript acceptance → turn/user transaction；
- RunEnd + ReplyEnd + final-output receipt → assistant/turn completion transaction；
- ToolCallStart + arguments + ToolCallEnd → 一个完整 tool_call entry；
- physical tool execution admission/dispatch handoff → 一个dispatch前commit的tool_execution_attempt row；
- ToolResultStart + chunks + ToolResultEnd + terminal projection → 一个 tool_result entry，可引用global blob并精确引用attempt；
- rollout account/reservation/settlement → turn/model usage summary；
- terminal process start/completion/notification → terminal audit + optional durable notification job。

**保留的窄 event vocabulary，如果实现仍选择 append-style records：**

- UserAccepted；
- AssistantAccepted；
- ToolCallAccepted；
- ToolExecutionAttemptStarted；
- ToolResultAccepted；
- TurnInterrupted；
- PromptQueued/Consumed/Cancelled；
- CompactionCommitted；
- SubagentTaskAccepted/Message/Result/Interrupted；
- MemoryFactAccepted/Superseded/Deleted；
- DurableJobQueued/AttemptStarted/Succeeded/Failed/Cancelled/OutcomeUnknown；
- TerminalProcessStarted/ObservedCompleted/OutcomeUnknown。

目标 vocabulary 预算为24。超过预算必须触发架构审查，说明该类型为何是跨进程产品事实、为何不能并入现有 canonical item；这不是 correctness gate，也不得通过巨型 JSON payload或合并不相干语义来规避审查。

#### 决策 8：删除哪些 projection/checkpoint

删除：

- transcript projection作为第二 authority；直接查询 transcript_entries；
- tool terminal projection；直接render tool call/result；
- model terminal projection artifact/reference；assistant reply本体就是 authority；
- provider-input generation projection；
- terminal presentation durable foundation；
- prompt queue checkpoint；
- subagent graph checkpoint；
- terminal notification/monitor reducer checkpoint；
- authority materialization shadow；
- per-reducer runtime projection checkpoint。

保留：

- immutable completed context snapshot；它不删除、覆盖、重排canonical transcript，未被binding revision引用时是可GC materialization，被引用后是semantic derived authority；
- 必要的 memory/search index checkpoint，但它只影响搜索新鲜度；
- truly background job的lease/status，不称为 projection checkpoint。

只有rebuildable projection必须满足“删除整表后语义不变”。被binding revision引用的context snapshot不适用：重新生成可能改变summary语义，因此它必须通过revision与blob FK保留。它的创建失败不回滚旧reply；它的缺失也不能由另一个新summary冒充compatible winner。

V1明确禁用“compaction hard rewrite transcript”：completed snapshot记录source sequence range/hash与versioned generation contract，context compiler采用exact snapshot缩短provider input，但`transcript_entries`原序列与`transcript_epoch`不变。canonical transcript retention是独立产品能力；默认不启用，只有显式retention transaction或database reset可以推进epoch/retained lower bound。

#### 决策 9：fingerprint边界

只持久化：

- blob content hash；
- context snapshot source range/hash与generation contract identity；
- external tool idempotency key（仅具体 tool支持时）；
- schema/version与必要 request hash；
- memory fact identity。

只在进程内使用：

- stable candidate fingerprint；
- repair plan/receipt fingerprint；
- owner/permit fingerprint；
- reducer state fingerprint；
- presentation join fingerprint；
- provider stream segment attribution fingerprint；
- checkpoint validation-base fingerprint；
- Host wiring/admission fingerprint，只要它不跨进程参与产品恢复。

#### 决策 10：哪些多状态机不可避免

只接受四类小durable状态族：

1. turn：running / completed / interrupted；
2. durable job aggregate：pending / active / succeeded / failed / cancelled / outcome_unknown；
3. durable job attempt：leased / terminal / outcome_unknown；
4. prompt/subagent task：pending / active / completed / interrupted / cancelled / outcome_unknown。

foreground `tool_execution_attempts`不是第五个可变状态机。每call最多一个attempt；`attempt absent`、`attempt present + running turn`、`result present`、`attempt present + interrupted turn + result absent`分别派生not-dispatched、started/potentially-dispatched、terminal-known与outcome-unknown视图。只有attempt row、turn与tool result是durable facts，不再为这些派生状态写status transition。

pending interaction没有durable状态机：它只在当前Host内是present/absent的live value。`interaction_decisions`只有accepted事实和必要decision kind，不保存request pending/resuming/reconciliation阶段。

durable job不是“lease超时一律回pending”：

- `RETRY_SAFE`：handler幂等，或所有effect受可验证 idempotency key保护；lease丢失后旧attempt保留，自动重执行创建带`retry_of`的下一attempt；
- `REMOTE_QUERYABLE`：新owner只能围绕旧attempt的stable remote identity查询并提交 observation，不能直接重做原effect；
- `NON_IDEMPOTENT`：lease丢失后原attempt与job aggregate进入 outcome_unknown，不自动回pending；显式retry必须创建新attempt并记录actor/reason。

首次claim在一个transaction中创建/选择current job attempt并安装其lease；每次同attempt lease换代递增attempt row上的claim generation。handler提交progress/result时必须匹配attempt id + current generation。stale generation提交直接失败，但不为此创建 receipt、confirmation、repair 或第二套attempt companion graph。

`claim_generation`与session `writer_generation`没有父子关系：Host takeover不撤销合法job attempt claim，job reclaim也不改变session writer。worker的progress/result/failure/lease settlement predicate只检查job id + attempt id + claim generation；它不得检查创建job时的writer generation。相反，Host enqueue、cancel request以及把completed job result接受进transcript时只检查当前writer generation，不检查worker的旧Host身份。

FULL/NONE/UNKNOWN/CONFLICT只允许在最底层 PostgreSQL commit adapter短暂存在，用 stable primary key读取确认；它不是 durable row，也不复制到 projection、tool、run、checkpoint domain。

#### 决策 11：最简 Host close等待什么

仅等待：

1. ingress确实停止；
2. foreground model/tool/subagent task收到cancel并在共享deadline内退出；
3. 未完成turn被一次transaction标interrupted；call-without-attempt保持not_dispatched，attempt-without-result才投影为outcome_unknown；
4. canonical Host-owned transcript/tool/queue/job enqueue/cancel/acceptance writer flush；
5. owned child processes/MCP连接收到terminate/close；
6. DB pool和operational logger flush到合理deadline。

不等待：

- checkpoint追平；
- projection job完成；
- context audit materialize；
- UI delivery；
- reducer repair；
- background compaction/memory job完成；
- terminal notification最终送达。

“不等待完成”不等于“允许physical task在资源释放后继续运行”。只要audit/checkpoint/presentation owner仍存在并可能使用session-owned DB pool、artifact store、executor或Runtime object，close就必须先stop admission，再在共享deadline内cancel/join其physical operation；不要求materialize成功或high-water追平。Stage 3物理删除owner后，相应join才从close消失。超deadline的operation必须先失去访问session资源的能力，不能被detach到后台继续使用已关闭依赖。

目标是把当前45个close await、4个committed-reducer barrier和至少6个逻辑band压成3阶段、≤12个await表达式、0 committed-reducer barrier。这里减少的是等待owner与semantic barrier，不是通过detach尚未physical exit的task伪造close完成。

#### 决策 12：Resume单位

**Conversation rehydrate，不做 execution replay。**

当前 Host 的 open transaction必须同时：

1. 获取或换代 session writer lease并原子递增 writer_generation；
2. 把旧 generation遗留的 running turn幂等置 interrupted，并在同transaction递增control_revision；
3. 保留完整已commit assistant tool-request message、tool execution attempts与已知results；call无attempt解释为not_dispatched，attempt无result才解释为outcome_unknown；
4. 丢弃旧Host的pending interaction request，不恢复approval/plan/MCP continuation；
5. 之后所有Host-owned turn、transcript、foreground tool attempt/result、prompt/queue、accepted interaction decision、job enqueue/cancel authorization与session metadata mutation都携带当前 writer_generation条件。

随后：

- 加载turn已exact采用的context snapshot，或为新turn选择最新eligible completed snapshot；绝不重新生成后冒充已采用snapshot；
- 加载其后的 transcript entries；
- 对interrupted turn中的每个未闭合tool call按决策4生成provider-only interruption closure；不写入canonical result，不从新降级原tool call；
- 加载 pending prompt queue与durable jobs；
- 不加载pending interaction request；同Host尚存时它只从live endpoint读取，Host takeover后不存在；
- TUI展示 interrupted/unknown；
- 只有新 turn才能调用模型或重试 tool。

observer-only attachment不获取 writer lease，也不改变 turn状态；旧 Host即使仍存活，其 generation上的任何 mutation都会被数据库拒绝。

background worker不属于rehydrate/open fencing domain：它只用job attempt的`claim_generation`提交progress/result/failure/lease settlement。Host takeover不应使合法worker result失败。worker也不得把result直接写进transcript；当前Host若要继续conversation，先读取immutable completed job/attempt result，再以当前writer generation创建显式accepted transcript item或新turn。accepted entry保存`source_job_id`并受session内唯一约束，或使用同等stable acceptance command id，确保accept ACK unknown不会把同一job result导入两次。

#### 决策 13：terminal monitor、subagent、compaction的最小边界

**terminal monitor**

- durable terminal audit：command/tool id、launch token、started_at；
- 如果承诺跨 Host notification，创建一个 terminal_monitor job；
- completion observation写 completed/result summary；
- 无法重新绑定真实进程时写 outcome_unknown/interrupted；
- lease丢失后只允许按稳定 launch token重新查询进程/托管服务状态，绝不把“重新claim monitor job”等同于重启命令；
- stdout delta和spinner不 durable，必要输出通过global blob contract保存。

**subagent**

- foreground child：task、messages、result；crash=interrupted；
- 只有产品明确声明“Host关闭后仍继续”的 background child才是 durable job；
- background child若可能执行非幂等tool，默认不是retry-safe；lease丢失时task进入 outcome_unknown/interrupted，不能自动从头执行；
- 只有纯计算、幂等或外部状态可查询的background child才允许新claim generation继续；
- background child的message/result写入job/attempt-owned namespace并校验attempt claim generation；它不能直接写parent session transcript；
- 不保存 child coroutine、RuntimeSession、teardown generation、retry_wait或physical lease owner；
- parent/current Host从durable result读取，并以当前writer generation显式接受进conversation；不等待旧 child executor复活。

**compaction**

- 一次 transaction写immutable context replacement summary或blob reference、source transcript range/hash、snapshot schema、compiler/prompt/model contract；“replacement”只指provider context选择，不指改写canonical transcript；
- 只有 completed snapshot可供turn选择；snapshot source upper bound必须早于该turn user entry；provider dispatch前，turn以immutable binding revision采用exact snapshot/full-history base，并以base + post-source exact delta构造输入，同时冻结该次read cut的`provider_input_through_sequence`；同一revision可跨tool-loop model calls复用，但预算压力可在safe point创建新snapshot/revision并推进turn current pointer；每条accepted assistant message精确引用生成它的revision并原样归因该次cut；
- unreferenced snapshot超过retention可GC；被binding revision引用的snapshot受FK保护，不能删除、覆盖或重新生成替换；
- failed attempt只进operational log；
- memory extraction可作为独立 durable job；
- 以 immutable source range/hash为输入、按唯一snapshot id提交的compaction/memory extraction可声明为retry-safe；
- compaction永不删除、重写、重排source transcript entry，也不推进transcript epoch；
- V1默认保留全部canonical transcript，retention若未来启用必须是独立、显式的产品transaction；
- snapshot生成/写入失败不追溯阻止已有reply、turn completion或close；若没有previous snapshot/full transcript能满足provider token budget，则新的provider dispatch以typed target-infeasible停止。

#### 决策 14：PostgreSQL authority与V1 single-writer fencing

PostgreSQL继续是 authority，但从 universal EventLog改为直接 transcript/job schema。

理由：

- Pulsara需要 durable prompt queue、background job、subagent coordination、memory governance和多进程查询；
- 这些需求本来就需要数据库；
- 删除的是“一切必须先成为 AgentEvent”的抽象，不是数据库事务；
- direct schema让唯一性、外键、状态查询和清理策略更直接。

V1 同时冻结以下并发约束：

1. 每个 session 在任一时刻只有一个可写 Host；
2. 可以有多个 observer attachment，但只有 controller命令通过当前 Host进入 mutation path；
3. `sessions` 保存 `writer_generation`、`writer_lease_owner_id`、`writer_lease_expires_at`与单一`control_revision`；
4. acquire/takeover在一个 PostgreSQL transaction中校验lease并递增generation；
5. turn、transcript、foreground tool attempt/result、prompt/queue/accepted interaction decision、job enqueue/cancel authorization和可写session metadata的mutation都校验当前generation；
6. lease换代后旧writer在上述Host-owned domain的commit全部失败，不做兼容winner或跨writer reconciliation；
7. lease renewal只是当前Host中的process-local heartbeat；renew失败立即停止新mutation并中断foreground，不创建durable lease-repair owner；
8. close只需停止当前writer、完成有界foreground终止并释放/等待lease过期，不需要证明所有observer已关闭。

`control_revision`不构成第三个fencing domain。它只在上述Host-owned transaction改变用户可见canonical control、且该变化可能不通过transcript sequence唤醒observer时递增；它不授权mutation、不保护background worker，也不保存变化历史。single writer使递增无需额外CAS/reconciliation。

两个fencing domain必须在schema port和SQL predicate层完全分开：

| domain | generation来源 | 允许保护的mutation | 明确不得保护 |
|---|---|---|---|
| session writer | `sessions.writer_generation` | turn/transcript、foreground tool attempt/result、prompt/accepted interaction decision、job enqueue/cancel request、job result acceptance | worker claim/progress/result/failure/lease settlement |
| background job | `durable_job_attempts.claim_generation` | attempt claim、progress、immutable result/error、failure、lease settlement | session transcript、turn completion、prompt admission、session metadata |

job row可以记录`created_by_writer_generation`作为审计，但worker commit predicate不得检查它。Host cancel只写`cancel_requested`授权事实；实际cancel/failure/settlement由current attempt + claim generation提交。completed job result进入conversation是一个新的Host-ownedaccept transaction，而不是worker transaction的副作用。

因此V1的可实施fencing边界只有两个：每session单一Host writer拥有conversation/session-control mutation，background worker只拥有job/attempt progress与immutable result；两种generation彼此独立。worker不能直接把结果写入conversation，只有当前Host以current writer generation显式接受后，结果才成为canonical conversation fact。

当前基线的`sessions`表只有id、workspace root、created time与metadata，并没有Host writer lease/generation列：[0002_runtime_truth_baseline.sql](src/pulsara_agent/storage/migrations/sql/0002_runtime_truth_baseline.sql#L77)。因此这里是推荐目标schema中的最小新增fencing事实，不是对现有能力的误读；也不应为它再造独立candidate/receipt表族。

当前 terminal protocol 已有 attachment role与 `controller_generation`，并允许 command携带 expected controller generation：[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L292)、[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1486)。它约束的是连接/应用层 controller takeover，不等同于这里新增的数据库 Host `writer_generation`。目标实现可以在命令入口校验二者，但不能把现有 controller generation误当成 durable writer fencing。

#### 决策 15：接受 reset-only hard cut

**接受，并推荐。**

- 不做双写；
- 不做旧 EventLog到新 transcript的在线 compatibility reducer；
- 不允许旧/new owner同时拥有一个 turn；
- 开发/当前部署数据库在切换点reset；
- 如果存在必须保留的真实用户session，只允许一次离线 export/import：仅导入能证明 accepted 的 user/assistant/tool/memory facts；running、partial、conflicting状态统一导入为 interrupted，并保留原始冷归档供审计。

#### 决策 16：防止 external side effect静默重复

1. completed assistant tool-request message的text与全部ordered calls原子提交后，才允许其中任何call invoke；
2. assistant_message_id与call_id表达provider intent；同message保存固定call ordinal，physical retry不得覆盖或改写call；
3. 每次实际invoke前创建stable attempt_id并commit `tool_execution_attempts`；每logical call最多一个foreground attempt，每个result精确引用assistant_message_id + call_id + attempt_id，并有唯一model-visible terminal row；
4. 同一message的全部calls都有success/error/denied/cancelled terminal row后，才进入下一 model call；provider lowering按call ordinal，不按result完成顺序；
5. call无attempt在rehydrate后显示not_dispatched/interrupted；attempt无result才显示outcome_unknown；
6. Runtime永不自动重试 outcome_unknown；
7. 显式 retry必须创建新turn中的新call id及该call的唯一新attempt id，并在新attempt上记录retry_of_attempt_id、actor、reason；不得为旧call创建第二attempt，旧attempt/result lineage不变；
8. read-only/retry-safe只由tool descriptor显式声明；默认不是；
9. 支持remote idempotency key/status lookup的tool可使用，但策略属于具体tool；
10. UI显示完整assistant message、每个命令、目标、ordinal、时间、已知结果和未知窗口；
11. workspace类side effect在retry前可先做read-only inspection，但inspection不能伪称原操作未发生；
12. attempt commit ACK unknown只exact query该attempt row；不创建dispatch receipt、confirmation或reconciliation owner。
13. interrupted attempt的late exact outcome只能在result尚不存在时由current writer追加该call唯一result；旧turn保持interrupted。是否可能参与某条历史assistant的input，只能比较`result.entry_sequence`与该assistant已冻结的`provider_input_through_sequence`，不能使用assistant自身sequence或binding revision推断。若result晚于已有assistant cut，future lowering按result实际sequence生成late-effect observation，不能倒插改写历史provider context。

#### 决策 17：durable、rehydrate与replay的词义

V1只承诺：

- **conversation rehydrate**：恢复全部accepted canonical conversation facts；
- **context rematerialization**：按versioned compiler、conversation facts与exact turn binding revision/context snapshot构造新的provider input；不承诺历史request逐字复现；
- **effect reconciliation**：查询tool/job attempt、remote identity与result，不默认重新执行；
- **audit reproduction**：只在显式debug、采样或合规模式提供；
- **execution replay**：明确不支持。

canonical relation与closed polymorphic payload必须有domain schema version。兼容演进只允许SQL migration rewrite或有限per-domain upcaster；未知版本fail closed。禁止重新建立universal historical event decoder，也禁止把process-local trace当成rehydrate输入。

#### 决策 18：全局blob publication与retention

所有大正文共用一个content-addressed `blobs` owner。publisher计算canonical digest/size/codec并完成immutable write；canonical domain transaction验证blob row并安装普通外键。所有domain reference均`ON DELETE RESTRICT`，不存在queue/tool/job/context/memory专属hold、receipt或confirmation。

同库小内容优先与canonical row同transaction写入。需要预写的大内容允许先形成unreferenced blob；V1在24小时orphan grace后才允许GC。GC只选择当前无FK引用的blob，并以数据库约束作为最终竞态裁决。blob write失败只终止尚未提交的对应canonical mutation，绝不回滚其他已经accepted的conversation fact。

### 7.3 TUI 与 Protocol hard-cut boundary

#### 7.3.1 当前代码事实：TUI 是 Presentation Foundation 的消费者

这次迁移不能被估算为“把 TUI query 改成读 `transcript_entries`”。当前代码确认：

- wire `PROTOCOL_MAJOR = 2`：[codec.py](src/pulsara_agent/terminal_protocol/codec.py#L70)；
- connection detach 会触及 `terminal_presentation_foundation_service`、释放 attachment retention owner并清除 root lease：[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L390)；
- snapshot 经 `services.query.snapshot_bundle()`，校验 control generation/revision，并借用 `active_head.confirmed_root_identity`：[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L447)；
- history page围绕 foundation snapshot、root/cursor和多种 outcome工作：[gateway.py](src/pulsara_agent/terminal_protocol/gateway.py#L1717)；
- Go decoder要求 active head、confirmed root、root/cursor pair、projection contract fingerprint、resident vector与rank spine互相一致：[carriers_gen.go](clients/terminal/internal/protocolvalue/carriers_gen.go#L864)；
- Go presentation层维护自己的 state/cache/page/reconnect语义：[state.go](clients/terminal/internal/presentation/state.go#L1)、[cache.go](clients/terminal/internal/presentation/cache.go#L1)、[s2_state_test.go](clients/terminal/internal/presentation/s2_state_test.go#L1)；
- v2 control snapshot把session lifecycle、run、pending interaction、queue等拆成section source versions，并另有`ControlProjectionCursor.control_revision`与transition accumulator：[codec.py](src/pulsara_agent/terminal_protocol/codec.py#L1000)、[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1027)；目标只借鉴“control可独立变化”这一需求，不保留cursor graph；
- mutation wire已有`command_id`、submit-specific `client_submission_id`和command query：[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1486)、[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1568)；
- 当前server用独立`terminal_command_receipts`表保存pending outcome、semantic fingerprint、receipt revision与outcome payload，唯一键还包含client instance：[command_receipt.py](src/pulsara_agent/runtime/terminal_application/command_receipt.py#L194)、[0011_terminal_presentation_queue.sql](src/pulsara_agent/storage/migrations/sql/0011_terminal_presentation_queue.sql#L142)。

因此删除 Presentation Foundation 必然同时改变 Python application service、protobuf contract、Go cache与reconnect测试；先删 Foundation、后补 TUI 会直接让现有客户端失去 authority source。

#### 7.3.2 冻结的目标协议语义

第一次 foreground authority cut 同时发布 **Protocol v3**；它是 incompatible major hard cut，不在 server内维护 v2→v3 presentation compatibility shim。

Python侧提供一个窄的 `TranscriptApplicationService`（逻辑边界名称，不要求沿用此类名）。每个canonical snapshot使用一个短的read-only `REPEATABLE READ` transaction；所有metadata、canonical control state和entry rows来自同一PostgreSQL MVCC snapshot，不把多次autocommit query拼成一个response。pending live interaction不属于这组canonical state，见7.3.5。

`sessions.latest_sequence`定义为该session按**commit order**发布的canonical entry high-water，并且在任一MVCC cut内必须等于可见的最大已提交entry sequence。每个canonical entry transaction先锁定session row或执行等价的原子conditional update，在同一transaction内取得`latest_sequence + 1`、插入entry并推进high-water；rollback既不发布entry也不推进high-water。禁止在transaction外预留sequence、使用可能让sequence 101先于100提交的non-transactional `nextval`，或异步追赶head。parallel tool execution仍可并行，只在最终canonical entry commit的窄分配点串行化。bounded snapshot suffix必须以该latest entry结尾，较老部分按容量裁剪并用`has_more_before`表示。

`control_revision`定义为`sessions` row上的单调整数：当前Host每次提交用户可见且不依靠新entry sequence唤醒的canonical control mutation时，在同一transaction中递增。它覆盖queue admission/cancel/claim、accepted interaction decision、session lifecycle、不随entry append发生的turn control变化、`tool_execution_attempts` insert，以及进入public attempt view的一次性remote-identity更新；terminal tool result通过entry sequence唤醒，turn interrupted通过control revision唤醒。它不覆盖context snapshot/binding revision/current pointer、pending live interaction、background job worker progress、spinner、transport或UI observation。前者由context/Inspector level read消费而不进入TUI control view，后者属于live/operational plane。`control_revision`没有event history、per-section child revision、fingerprint或checkpoint。

每次provider dispatch也使用一个短read-only `REPEATABLE READ` transaction冻结当前`context_binding_revision_id`与同一MVCC cut中的`latest_sequence = H`，并只把`entry_sequence <= H`的合法conversation delta交给compiler/lowering。immutable blob正文可以在验证引用属于该cut后分页hydrate，但不能把H之后才commit的entry混入本次input。process-local immutable prepared-input handle持有revision与H直至assistant commit；commit只消费该handle，不能重新读取latest sequence或由caller自报cut。accepted provider-generated assistant entry将H写入`provider_input_through_sequence`。这只是每条assistant row上的窄attribution，不是Protocol/TUI cursor，也不引入durable ModelStart、ModelEnd、provider request、candidate、receipt或operation journal。

它从canonical session/turn/transcript/tool/queue/accepted-interaction-decision facts形成三类响应：

1. **snapshot**：先在同一read transaction中冻结`session_id`、`transcript_epoch`、`retained_from_sequence`、`latest_sequence`与`control_revision`，再读取`entry_sequence <= latest_sequence`的retained suffix，以及同一MVCC cut中的turn、tool attempts、queue、session lifecycle与accepted interaction decisions；tool attempt的started/terminal-known/outcome-unknown视图只能由该cut中的attempt/result/turn facts派生；`writer_generation`可以作为后续mutation hint返回，但也必须来自该cut；snapshot明确不返回pending interaction request；
2. **history page**：request携带`transcript_epoch`和明确`cut_sequence`，按每session单调递增的`entry_sequence`做`before`/`after`分页，只返回`entry_sequence <= cut_sequence`的稳定entry；page response回显epoch/cut，不返回presentation root、active head、projection contract fingerprint、retention-root lease或continuity receipt；
3. **canonical observation**：客户端提交`observed_sequence + observed_control_revision`。server在一个statement或短read-only transaction的同一MVCC cut读取current pair；任一值更高就立即返回`snapshot_required`与current pair，即使此前edge notification丢失。等待超时、收到LISTEN/NOTIFY或内存hint后都重新读取pair。spinner、transport retry、token delta、live process progress与pending interaction等live state走独立endpoint/stream，不并入canonical snapshot，也不构成semantic acknowledgment。

每个history page response也使用自己的read-only repeatable-read transaction：先验证request epoch仍等于当前epoch、cut未超出该epoch的已知high-water且cursor未落在retention lower bound之前，再在同一MVCC cut读rows。新commit可以推进全局high-water，但不会混入旧cut的page；客户端需要新snapshot/high-water cycle才能看到它。

cursor可以是opaque carrier，但其语义只能等价于 `(session_id, transcript_epoch, entry_sequence)`：

- `transcript_epoch`只在database reset或显式产品retention transaction改变可读history边界时变化；V1默认不启用retention；
- compaction只追加context snapshot，绝不改变`transcript_epoch`、`retained_from_sequence`或任何entry sequence；
- Host takeover只改变 `writer_generation`，不得让合法read cursor整体失效；
- `writer_generation`用于mutation fencing，不是history排序，也不是presentation root generation；
- entry ordering由数据库sequence/唯一约束给出，不创建另一套root identity或cursor fingerprint authority。

`control_revision`不进入history cursor，因为它不描述entry顺序或retention；它只与observed sequence组成canonical observation token。client收到`snapshot_required`后获取一个新一致snapshot，而不是按revision replaycontrol transitions。

#### 7.3.3 GAP、reconnect 与 Go client

v3 GAP只有两个 canonical原因：client cursor早于 `retained_from_sequence`，或cursor/epoch不属于当前可读history。收到GAP后，Go client必须：

1. 丢弃本地 durable page cache；
2. 请求fresh snapshot；
3. 以snapshot suffix重新建 sequence-indexed bounded cache；
4. 如需更老history，再用`before_sequence`分页。

客户端cursor超前于server high-water属于stale/corrupt client state，同样fresh snapshot；operational live stream漏包只丢spinner/progress，不制造canonical GAP或反写Runtime。

Go端应删除root-indexed resident cache、rank-basis join与confirmed-root validity规则，改为以`entry_sequence`索引的bounded transcript cache。attachment observer/controller、heartbeat与secret transport可以保留，但mutation binding必须最终在Host入口携带并校验当前数据库 `writer_generation`。现有wire `controller_generation`仍只管理attachment controller权利，二者不可混用。

#### 7.3.4 Mutation ACK unknown的最小幂等边界

Protocol v3保留`command_id`、submit-specific `client_submission_id`与command query能力，但删除generic durable receipt authority：

1. `command_id`由client为一次logical mutation稳定生成，重连、timeout或ACK unknown retry必须复用；
2. 创建turn、queue item或interaction decision的同一transaction把`command_id`写入canonical target row，并由`UNIQUE(session_id, command_id)`保证session-wide唯一；
3. submit prompt同时保存`client_submission_id`，供客户端本地submission关联；server幂等winner仍以session + command id为准，不依赖旧client instance或attachment；
4. 相同command id、相同typed semantic input返回现有canonical target；相同id、不同input返回conflict，不创建第二target，也不持久化conflict receipt；
5. `QueryCommand`直接按session + command id读取turn/queue/accepted interaction-decision row并构造target id、status与durable references；read-only query不要求writer generation；pending request没有command target row；
6. 第一次创建、cancel或decision mutation必须携带当前writer generation；generation换代后，旧client仍可query第一次成功的target，但不能提交新mutation；
7. stop、detach等自然幂等且不创建canonical target的命令按当前canonical state返回，不强制进入一张通用command table。

`UNIQUE(session_id, command_id)`是跨turn/queue/accepted-interaction-decision target的**逻辑不变量**，不能只在各物理表分别unique后假称session-wide。Stage 2 SQL spec必须选择可由PostgreSQL原子执行的形状，例如让command-addressable user mutation共享一个canonical base relation/partition key，再由turn、queue或accepted-decision row引用。该base row若存在，保存的是用户action本身与typed semantic input，不保存pending request/outcome、receipt revision或consumer observation；应用层“先查再插”不能替代数据库唯一约束。

目标schema不保留`terminal_command_receipts`、receipt revision、outcome fingerprint、query token、PENDING_CONFIRMATION、RECONCILIATION_REQUIRED或compatible-winner state。typed semantic equality优先直接比较canonical fields；只有payload过大时才允许一个内容hash辅助比较，且hash不是新的owner或recovery authority。

#### 7.3.5 Pending interaction的V1 live-control边界

pending approval、plan question/exit与MCP input request不属于canonical snapshot。当前Host维护一个带`live_interaction_id`的process-local current value，并提供level-readable live-control query；notification只是让TUI更快调用query。同一Host上TUI断线重连后可以重新得到current request，但这个能力不跨writer generation。

resolution command携带`expected_writer_generation`、`live_interaction_id`与`command_id`。Host必须先确认当前live value exact match，再用writer generation做accepted decision transaction；该transaction写`interaction_decisions`、应用command id唯一约束并递增`control_revision`。decision不能只引用会随Host消失的live id：approval绑定durable assistant tool call；plan resolution在同一transaction创建canonical user/conversation item并由decision引用；MCP/external secret response只保存closed disposition、durable redacted tool-call/attempt subject与必要keyed commitment，不保存secret plaintext或可恢复sealed response。command-addressable base action是唯一幂等owner，不能为plan item和decision各生成一条command row。

ACK unknown查询只返回accepted/denied/cancelled状态与durable subject，不返回secret。secret handle在当前Host内由revocable process-local owner消费；若Host在decision commit后、使用secret前crash，旧turninterrupted，新Host不恢复值或继续旧operation。一旦Host crash、close或takeover，旧live value消失，旧resolution fail closed；open只把旧running turn置interrupted，不从transcript、trace、audit或suspended owner重建request。

因此Protocol v3 DTO必须把canonical snapshot/observation与live interaction query分开。前者可跨Host恢复；后者只保证“当前Host仍活着时可重新读取”。V1 schema明确没有`interaction_requests`。未来产品若改变承诺，需要新的architecture decision和schema hard cut，不能复用operational DTO暗中改变durability。

#### 7.3.6 独立验收

- Python/Go contract tests明确拒绝v2/v3混连；
- snapshot、向前/向后page、空history、retention GAP、client-ahead、reconnect均由跨语言fixture覆盖；
- 并发entry/turn commit发生在snapshot各SQL之间时，response的metadata、canonical control与suffix仍来自同一MVCC cut；不得出现latest=10但rows只到9或反向组合；
- history page严格回显并遵守epoch/cut sequence；翻页期间的新commit不混入旧cut；
- canonical observation在transcript notification丢失时通过sequence发现推进，在queue/turn/session/accepted-decision/tool-attempt control-only notification丢失时通过control revision发现推进；任一落后都要求fresh snapshot；
- kill TUI而Host继续产生reply/tool items，重连只靠canonical rows完整恢复；
- notification丢失不影响turn completed；
- user acceptance transaction commit后丢ACK，client用同一command id retry/query只得到原turn/queue item；不同input复用同一id稳定conflict；
- TUI在同一Host重连时可query当前pending live interaction；kill/takeover Host后request不在canonical snapshot中，旧resolution被拒绝且turn显示interrupted；
- command query只读取canonical target row，删除`terminal_command_receipts`后仍通过；
- Host writer takeover后旧controller即使持有旧socket也无法mutation，但observer仍能用不受takeover影响的history cursor读取；
- Protocol切换不引入dual query、shadow Presentation Foundation或在线root→sequence translator。

### 7.4 目标 schema

目标审查预算为24个产品表；下面是逻辑关系，不要求一项一表。超过预算需要逐项证明产品价值，但表数本身不判定正确性；也禁止用一个无约束巨型 JSON 表隐藏多个彼此独立的authority：

| 逻辑关系 | 作用 | authority |
|---|---|---|
| sessions | workspace/session metadata、lifecycle、writer generation/lease fencing、transcript epoch/retention lower bound、commit-ordered latest sequence、单一control revision | canonical |
| turns | user turn、status、final entry、interruption、command/client submission id、current context binding revision pointer | canonical |
| turn_context_binding_revisions | turn-local immutable revision ordinal、FULL_HISTORY/SNAPSHOT base union、exact snapshot/source/compiler binding、safe-point install/advance；旧revision不覆盖 | semantic attribution |
| transcript_entries | user/assistant message/tool result；commit-ordered append-only sequence；assistant tool-request message是原子parent；provider-generated assistant entry绑定exact context revision与`provider_input_through_sequence` | canonical |
| assistant_message_blocks | 同一assistant message的ordered text/tool calls；message/block/call ordinal与exact call identity；与parent同transaction插入 | canonical child；不要求独立物理表 |
| tool_execution_attempts | dispatch前commit的attempt id、unique call subject、effect/authorization/actor、pre-generated idempotency key、一次性remote identity、cross-call retry attribution；无可变status | canonical effect journal |
| tool_results | call唯一terminal result；exact call/attempt join；closed pre-dispatch terminal是唯一无attempt分支；late reconciliation保留实际entry sequence | canonical conversation child；可并入entry payload |
| durable_jobs | terminal monitor、background subagent、background compaction precompute/post-compaction memory extraction的intent、safety class、aggregate state与accepted result | canonical job aggregate |
| durable_job_attempts | attempt ordinal、claim generation/lease、remote identity、terminal/unknown、result/error与retry_of | canonical work journal |
| prompt_queue_items | durable ingress order/status、command/client submission id | canonical |
| interaction_decisions | accepted decision、durable subject、command id、redacted disposition/keyed commitment；不保存pending request或secret | canonical |
| context_snapshots | immutable bounded summary/blob ref、source range/hash、schema/compiler/prompt/model contract；unreferenced可GC，被binding revision引用后受FK保护 | semantic derived authority |
| subagent_tasks | parent/child task与job-owned message/result refs；background write按job attempt claim generation | canonical job domain |
| memory_facts | accepted/superseded/deleted memory | canonical |
| memory_relations | 必要graph relation | canonical |
| blobs | purpose-neutral immutable content、digest/size/codec；所有domain使用FK与ON DELETE RESTRICT | canonical shared storage boundary |
| search_indexes | 可重建全文/vector索引 | derived、non-gating |
| schema_migrations | physical schema version | infrastructure |

可以把 subagent task建模为 durable_jobs的一个type，也可以独立表；选择标准是查询与约束，不允许同时拥有两份状态。

V1没有`interaction_requests`关系。`assistant_message_blocks`是逻辑约束面：实现可以选择有外键/ordinal约束的child rows，或严格有界、typed且整message原子写入的payload；无论物理形状如何，都不能让单个call在parent message commit前可见或可执行，也不能用无约束巨型JSON规避call identity与ordinal唯一性。

`turn_context_binding_revisions`是为保留mid-turn compaction所需的最小semantic attribution，不是ModelCall lifecycle。每个turn的revision ordinal从0单调递增，`UNIQUE(turn_id, revision_ordinal)`；`turns.current_context_binding_revision_id`只能指向本turn且已committed的revision。initial revision必须与user/turn acceptance在同一transaction安装，避免产生“turn存在但首个provider call没有base”的中间态；后续revision只在provider safe point以writer generation新增并原子推进current pointer。base是closed union：`FULL_HISTORY`不引用snapshot并从该turn合法history lower bound开始取exact entries；`SNAPSHOT`必须引用exact `context_snapshot_id`与`source_through_sequence`。两个分支都保存compiler/schema/lowering contract。每条accepted provider-generated assistant entry必须同时引用exact revision并保存prepared-input handle中的`provider_input_through_sequence`；该cut不得早于本turn user entry sequence，且必须严格小于新assistant entry sequence。user、tool result与audit entry不得伪造这两个provider attribution字段。snapshot source upper bound严格早于turn user entry，当前turn始终作为exact delta。旧revision、其contract与所引用snapshot均不可覆盖。

`tool_results`可与transcript entry同row，但physical-attempt reference与无attempt terminal union必须受数据库约束；`tool_execution_attempts.call_id`必须唯一，显式retry的新attempt必须属于新call。若interrupted旧attempt的late exact result在后续assistant entry之后才落盘，result仍只占该call唯一terminal row并保留实际entry sequence；provider lowering逐条比较result sequence与历史assistant的`provider_input_through_sequence`，明确区分“位于当次conversation cut内”与“该assistant不可能看见的late outcome”，并只在未来cut将后者表达为typed late-effect observation。`durable_job_attempts`不得退化成`durable_jobs.attempt_summary JSON`。

### 7.5 保留、删除、合并、process-local、operational-only清单

#### 保留/重塑

- PostgreSQL verified connection、migration runner与transaction；
- sessions、turns、global blobs；
- prompt_queue_items，但去掉独立account/checkpoint ownership；
- working_context_summaries重塑为immutable context_snapshots + turn-local immutable binding revisions；
- tool_execution_records重塑为tool_execution_attempts + exact tool results；
- memory_nodes/relations/governance事实，后续再做单独schema减法；
- terminal process audit中有外部process审计价值的launch token、command摘要、started/completed/unknown字段；
- background compaction precompute、post-compaction memory extraction等真正后台 job；当前turn为了下一次provider call执行的safe-point compaction仍是process-local foreground operation，只在成功时提交immutable snapshot/revision；
- durable_jobs重塑为job aggregate，并增加窄durable_job_attempts lineage；
- stable primary key、unique constraint、blob hash与foreign key；
- structured operational logs/traces。

#### 目标删除的生产文件/子系统

在其消费者先切走后，目标物理删除：

- [model_stream_recovery.py](src/pulsara_agent/runtime/model_stream_recovery.py#L1)；
- [model_control_recovery.py](src/pulsara_agent/runtime/model_control_recovery.py#L1)；
- [committed_reducer_repair.py](src/pulsara_agent/runtime/committed_reducer_repair.py#L1)；
- runtime/committed_reducer_post_fold.py；
- [projection_checkpoint_maintenance.py](src/pulsara_agent/runtime/projection_checkpoint_maintenance.py#L1)；
- [terminal_projection.py](src/pulsara_agent/llm/terminal_projection.py#L1)；
- [terminal_projection.py](src/pulsara_agent/runtime/terminal_projection.py#L1)；
- runtime/authority_materialization/；
- generic runtime/projection_jobs/ 与 projection_jobs/，由单一窄 durable_jobs worker取代；
- runtime/context_input/audit_materializer.py；
- runtime/context_input/audit_storage.py；
- runtime/context_input/audit_gc.py；
- runtime/context_input/audit_doctor.py；
- primitives/context_input_audit_storage.py；
- [runtime_session_teardown.py](src/pulsara_agent/ports/runtime_session_teardown.py#L1)；
- [command_receipt.py](src/pulsara_agent/runtime/terminal_application/command_receipt.py#L1) 的generic pending/confirmation/reconciliation receipt store；command query改为读canonical target row；
- stable RunFinalization repair service；
- [runtime/terminal_presentation/](src/pulsara_agent/runtime/terminal_presentation/) 整个Foundation，包括history tree/root/checkpoint/retention/projection/viewport/restore owner；
- [presentation_history.py](src/pulsara_agent/primitives/presentation_history.py#L1)、[presentation_view.py](src/pulsara_agent/primitives/presentation_view.py#L1)、[presentation_checkpoint_storage.py](src/pulsara_agent/primitives/presentation_checkpoint_storage.py#L1) 中只为root/head/rank/cursor/checkpoint服务的fact；
- [terminal_presentation.py](src/pulsara_agent/ports/terminal_presentation.py#L1) 的v2 root/page ports，以窄transcript snapshot/page port替换；
- [state.go](clients/terminal/internal/presentation/state.go#L1) 与 [cache.go](clients/terminal/internal/presentation/cache.go#L1) 的root-indexed state/cache contract，重写为sequence-indexed cache；
- [terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1) 中v2 presentation root/head/cursor/page carriers及其generated/fingerprint fixtures，随Protocol v3 hard cut替换；
- replay中只为旧 event grammar服务的assembler/reducer；
- Inspector中展示candidate/receipt/checkpoint owner的旧路径。

对 context audit 的删除取决于 Open Question 1；即使保留 opt-in audit，也必须从foreground event与close semantic completion gate移出；只要其physical task仍使用session资源，bounded cancel/join保留到owner被删除或迁入真正独立的process/job lifecycle。

#### 目标删除的表

reset-only后删除：

- agent_events；
- runtime_projection_checkpoints；
- ledger_materialization_accounts；
- background_derived_work_budget_accounts；
- background_derived_work_budget_reservations；
- background_derived_work_budget_settlements；
- durable_projection_kind_activations；
- durable_projection_pre_activation_contracts；
- durable_projection_pre_activation_coverage_pages；
- durable_projection_pre_activation_coverage_receipts；
- durable_projection_pre_activation_session_cutovers；
- durable_projection_repair_actions；
- durable_projection_result_receipts；
- durable_projection_seed_failure_resolutions；
- durable_projection_seed_failures；
- durable_projection_session_cutovers；
- durable_projection_target_authority_conflicts；
- durable_projection_target_execution_leases；
- durable_projection_target_heads；
- prompt_queue_accounts；
- prompt_queue_artifact_preparation_holds；由全局blob publication + FK + orphan grace替代；
- terminal_command_receipts；
- runtime_write_admission_epochs；
- runtime_write_guard_secrets；
- 与old EventLog write protection专用的runtime_write_protected_relations。

canonical_mutation与memory表不在第一轮一刀切删除：先确认哪些row是用户长期事实，再把它们压入memory_facts；派生的sequence head、surface delivery、migration binding plan/page/receipt随后删除。

#### 合并

- runs并入turns；session级run统计可query；
- tool_result_artifacts并入global blobs + transcript/tool-result FK；
- terminal process audit与terminal monitor job共享launch/audit id；
- durable projection jobs与明确承诺跨Host完成的compaction/background work统一为durable_jobs + durable_job_attempts；当前turn为满足下一provider budget而同步等待的safe-point compaction不创建job row；
- queue content reference可并入prompt_queue_items或global blob FK；
- memory candidate/governance decision在产品允许时压成事实+decision lineage，不保留每步worker transition。

#### process-local

- model transport、delta assembler、token streaming；
- foreground tool execution和parallel batch；
- pending approval/plan/MCP request、live interaction id、等待future与同Host live-control view；
- context compiler工作树与provider request builder；
- capability resolution/exposure与permission evaluation结果；只有最终批准/拒绝有审计需要时写turn metadata；
- resource semaphore、connection lane、physical byte charge；
- live reducer/view cache；
- TUI render tree、spinner、progress、scroll state；
- close cancellation task group；
- foreground subagent executor；
- retry/backoff for transient provider calls within one process generation。

#### operational-only

- ModelStart/End timestamps、TTFT、token deltas；
- transport retry、HTTP request id、SDK diagnostics；
- context component布局和page plan；
- checkpoint attempt/latency/failure；
- reducer fold timing与cache lag；
- repair attempt；
- UI delivery/ack；
- close stage timing；
- owner/task inventory；
- process-local fingerprints；
- sampled exact provider input audit。

operational-only数据可写日志、trace或有TTL的debug archive，但必须满足三条：不参与resume；不进入semantic gate；丢失不阻止close。

### 7.6 应删除或改写的测试

随 owner物理删除，而不是保留其contract：

- tests/test_terminal_completion_finalization_incident.py 中 checkpoint candidate、repair receipt、finalization retry owner测试；
- tests/test_terminal_completion_incident_architecture.py 中强制 repair/checkpoint close order测试；
- tests/test_runtime_committed_writer.py 中 per-reducer confirmation/repair contract；
- tests/test_context_transcript_projection.py 与 tests/test_transcript_projection_contract.py 中把projection当第二 authority的测试；
- tests/test_durable_projection_* 中 foreground/UI/evidence projection job部分；
- tests/test_llm_runtime.py 中“恢复同一 provider outcome”“UNKNOWN owner阻塞close”“physical owner跨waiter cancellation”测试；
- tests/test_subagent_runtime.py 中 child RuntimeSession teardown generation/retry/reconciliation测试；
- context audit测试中默认每call materialize、root永久保留、close要求业务成功/追平的部分；保留“owner存在时physical task必须在资源释放前退出”的lifecycle test；
- terminal command receipt测试中pending confirmation、reconciliation、compatible winner与receipt fingerprint contract；改写为canonical-row idempotency/query test；
- interaction recovery测试中跨Host恢复pending request、suspended run continuation、resume link/receipt与reconciliation contract；改写为same-Host live query、stale resolution拒绝和crash→interrupted test；
- Python terminal protocol中要求Presentation Foundation root/head/retention lease/contract fingerprint的snapshot、page、GAP测试；
- Go `clients/terminal/internal/presentation` 中root-indexed cache、confirmed-root rank spine与v2 GAP rebuild contract测试，改写为v3 sequence/page语义。

必须保留或重写为行为测试：

- user input commit先于model call；
- user acceptance commit ACK丢失后同command id只返回原canonical target；不同input复用同id为conflict；
- final assistant transaction唯一；
- mixed text + 全部ordered tool calls作为一个assistant message原子commit先于任何effect；
- 每个physical invoke前先committool attempt；call无attempt=not_dispatched，attempt无result=outcome_unknown且不重试；
- parallel tool results精确绑定attempt；全部calls terminal后才follow-up，provider lowering按call ordinal而非完成顺序；
- crash => interrupted；
- checkpoint/audit/UI failure不影响reply；
- durable job aggregate/attempt restart、retry lineage与remote identity；
- stale Host writer generation mutation被数据库拒绝；
- Host takeover不影响合法background claim result；worker不能直接写transcript，当前Host显式accept job result；
- non-idempotent job lease丢失=>outcome_unknown且不重试；
- context snapshot source/contract一致、source upper bound早于所属turn user entry，且snapshot commit、binding revision install/advance与GC-unreferenced都不改变transcript entry或epoch；rematerialization精确拼接post-source/current-turn delta，被revision引用的snapshot不可GC或重新生成替换；
- global blob publication保证所有canonical FK只指向已验证immutable bytes，24小时orphan GC不删除任何referenced blob；
- Protocol v3 snapshot/page使用一致MVCC read cut，Observe同时比较entry sequence/control revision，ACK unknown query只读canonical target；
- pending interaction同Host可level-query，Host crash/takeover后不出现在canonical snapshot且旧resolution失败；
- TUI Protocol v3 snapshot/page/GAP/reconnect只读 canonical transcript；
- Stage 1 de-gate后，in-flight audit/checkpoint/presentation physical I/O仍在DB pool/artifact store释放前bounded退出。

---

## 8. 分阶段减法路线

### 8.1 总纪律

每个阶段都遵循同一删除顺序：

1. 让产品读取方不再依赖旧 projection/owner；
2. 移除旧 semantic gate与业务完成/追平型close wait；
3. 停止产生旧 durable fact；
4. 删除 repair/reconciliation owner；
5. 删除 event/table/test。

physical lifecycle遵循另一条不可跳过的顺序：stop admission → cancel/terminate → bounded join不再访问session-owned resource → release DB pool/artifact store/executor → 删除owner后再删除对应close await。de-gate只说明operation成功与否不影响canonical语义，不授权在physical operation仍存活时释放其依赖。

禁止的过渡方式：

- 同一 turn双写 EventLog和transcript schema；
- 新建 compatibility reducer把旧event实时翻译到新表；
- 用新的stable candidate/receipt/fingerprint包住旧owner；
- 为了rollback让两个authority长期共存；
- 先删owner但保留所有依赖，然后再造临时repair owner。

允许的rollback只有：回退到上一发布版本，并恢复该阶段前的数据库快照或重新reset。代码路径内部不承担双版本兼容。

另加一条 **coherent authority cut rule**：凡是第一次把某类生产 canonical fact写入新schema，该release必须同时具备写入、open/resume、context compilation、Inspector、TUI snapshot/page/reconnect、compaction source读取与writer fencing。旧owner的物理文件可以在下一阶段删除，但新数据不能先于所有正确读取/恢复语义进入生产。

这条规则约束production activation，不禁止dormant construction。schema/repository、test-only runner、reader、Protocol/Go consumer可以分多个PR进入tree，只要普通Host composition不可达、没有feature flag按session启用、没有dual-write，并且每个PR独立全绿。

vertical slice只能沿产品模式切，不沿运行后才知道的model outcome切。启动前明确禁用全部tool的`NO_TOOLS`模式可以作为pre-production spike；普通Agent中的“这一次模型碰巧只回text”不能作为authority分流条件。

### 8.2 阶段总览

| 阶段 | vertical slice | 首要删除结果 | 独立 correctness gate |
|---|---|---|---|
| 0 | 产品语义与并发约束冻结 | 单writer、partial、subagent、terminal、audit决策不再漂移 | 决策可转成行为test；基线可重复 |
| 1 | 低风险真减法 | audit退出默认；acceleration/presentation failure解除semantic latch | 故障不否决canonical commit；旧owner physical I/O仍在资源释放前bounded退出 |
| 2 | conversation kernel + minimal job kernel单次production activation | user/assistant/tool attempt+result/context revisions/interrupted一次激活；foreground-reachable background work同步切换；TUI v3 | 一个turn一个authority；无old/new job bridge；sequence+control唤醒、interaction live边界、attempt-before-effect、crash/reconnect/fencing全通过 |
| 3 | 删除exact execution recovery与derived authority | recovery/reducer/checkpoint/Presentation Foundation物理删除；close压缩 | 无temporary RuntimeSession；3段close；0 reducer barrier |
| 4 | 后台handler迁移收口与legacy graph删除 | 复用Stage 2已激活的窄job aggregate + attempt journal；迁移剩余被显式禁用的handlers并删旧projection-job graph | stale claim拒绝；attempt lineage完整；所有产品handler只有新authority；unsafe lease loss=>unknown |
| 5 | universal EventLog退役 | event/replay/projection/旧schema物理删除 | production import graph无旧authority；行为gate全通过 |

### 8.3 阶段 0：冻结产品语义与基线

**目标**

把本报告的18项决策变成不可漂移的architecture gate；特别冻结以下产品约束，不实现新repair owner：

- V1 每session单Host writer，observer可多attach；
- live partial assistant默认不durable；
- foreground subagent随Host interruption，不跨进程复活executor；
- terminal restart只重新查询/绑定可证明存在的process，不自动重启command；
- context-input exact audit默认关闭；
- external side effect unknown默认禁止自动retry；
- Host writer generation与job-attempt claim generation是独立fencing domain；
- Protocol v3 canonical snapshot/page使用repeatable-read sequence cut；mutation ACK unknown由canonical row idempotency处理；
- Protocol v3 canonical Observe比较`entry_sequence + control_revision`；revision只有一个当前值，不保存section/history；
- V1 pending interaction是same-Host process-local live control；Host crash/takeover后request消失，accepted decision才durable；
- completed assistant tool-request message的mixed text与全部ordered calls原子commit；每个physical invoke前commitattempt；parallel results全部terminal后才follow-up；
- V1 compaction不改写transcript、不推进epoch；被binding revision引用的context snapshot exact保留；每次provider dispatch冻结exact revision与commit-ordered conversation sequence cut，accepted assistant原样保存二者；canonical retention默认关闭；
- per-session entry sequence只在canonical entry transaction内按commit order分配并推进high-water；禁止预留、乱序commit或异步head；
- job aggregate与attempt lineage分表，global blob publication是所有大内容的唯一boundary；
- conversation rehydrate/context rematerialization/effect reconciliation/audit reproduction分名，execution replay禁止；
- Stage 1只de-gate业务完成，旧owner physical quiesce保留到owner删除。

**删除内容**

无。只建立待删inventory和测量，不把当前复杂机制标成永久contract。

**保留不变量**

- 当前生产行为不变；
- 仓库仍可运行；
- 不触碰用户数据库。

**代码修改面**

后续实现时只允许增加静态/计数测试和metrics；本调研阶段没有修改。

**数据/reset策略**

- 保存迁移head、必要冷归档和可恢复数据样本；
- 明确最终cutover使用fresh/reset schema；
- 禁止用生产dual-write收集基线。

**独立 gate**

- 两条探针可稳定复现43/83 EventLog event量级；
- table、EventType、owner、close-await计数由CI脚本读取；
- 有一份明确的old-data export policy；
- 单writer lease/takeover、partial text、foreground/background subagent、terminal process、audit与unknown UX均有冻结的行为判定，不再留给实现者通过CAS/receipt猜测。

**回滚边界**

无运行时变更，无需回滚。

### 8.4 阶段 1：先做低风险真减法

**目标**

在不切换canonical authority、不触碰TUI数据源的前提下，先证明所有acceleration、audit与observer failure都不能反向否决foreground语义；同时减少默认I/O和close依赖。

**删除内容**

1. context-input exact audit改为默认关闭，只允许显式doctor/debug、采样或合规策略offer；
2. audit materialization、read-back、GC的成功/完整度从Runtime latch与Host/non-Host close semantic gate移除；
3. checkpoint、derived projection、presentation publication、search index failure从assistant/tool canonical success判定移除；
4. Host close不再等待audit materialized、checkpoint追平、presentation published等业务完成度；但仍stop其admission并bounded cancel/join正在使用session资源的physical task；
5. 删除所有只为“acceleration尚未追平”而触发的foreground admission拒绝分支。

本阶段**不删除 Presentation Foundation**，因为v2 Python gateway与Go client仍以它为数据源；它的物理删除属于阶段3，且只能发生在阶段2 Protocol v3 reader已经切换之后。

本阶段也不追求对应close await归零：audit/checkpoint/presentation owner仍在时，close await的语义从“等它成功/追平”改成“停止admission，并确认physical operation已退出或已被剥夺session resource访问能力”。Stage 3删除owner/executor后才删除这些await。

若某adapter无法在deadline内cancel，也无法通过关闭独立connection/process等方式确保它不再访问session resource，则该owner的physical wait在Stage 1不得删除；这个slice对该owner视为未通过，而不是用detach task或新teardown generation掩盖。

**保留不变量**

- 当前EventLog仍是本阶段canonical authority；
- EventLog本体损坏、唯一commit无法确认或PostgreSQL transaction失败仍可fail closed；
- 解除的是可重建cache/audit/UI的gate，不把真正canonical corruption伪装成soft failure；
- 当前text/tool行为与可见transcript内容不变；
- DB pool、artifact store、executor与Runtime object只在相关physical task退出后释放。

**代码修改面**

- runtime/context_input audit offer/materializer/storage与composition；
- runtime/session.py reconciliation latch聚合；
- runtime/projection_checkpoint_maintenance.py与publication maintenance的调用方；
- host/session.py和runtime/session.py close drain；
- 故障注入tests与默认configuration。

**数据/reset策略**

无authority schema迁移。已有audit/checkpoint/presentation数据保留到后续reset；新默认关闭只停止自动offer，不做background backfill或online cleanup owner。

**独立 gate**

在checkpoint write/read timeout、audit archive unavailable、presentation publication failure、TUI disconnect、search index lag任一故障下：

- 已接受user input不回滚；
- 已完成assistant/tool canonical commit不被拒绝；
- RunEnd/当前等价completion可以完成；
- Host close不等待这些owner的业务成功、materialized或high-water追平；
- in-flight operation收到cancel/stop，并在共享deadline内退出后才关闭其DB/artifact/session依赖；
- 人工阻塞一个audit/checkpoint/presentation I/O时，不得出现pool已close后task仍发query/write；
- Runtime不新增对应hard latch。

默认每model call audit artifact写入从4降为0；这是I/O目标，不是canonical correctness的替代证据。

**回滚边界**

本阶段没有新authority；可回退binary/config。若回退要求旧close drain，已有旧表仍在，因而不需要compat writer。

### 8.5 阶段 2：conversation kernel + minimal job kernel单次production activation

**目标**

在一个production activation中把全部foreground canonical item切到direct schema；此前允许多个production-disabled construction PR：

~~~text
turn + accepted user
  -> immutable context binding revision selection
  -> pre-dispatch freeze exact revision + provider_input_through_sequence
  -> process-local provider stream
  -> accepted final assistant(exact revision + frozen cut)
  OR atomic assistant tool-request message(text + ordered calls)
       -> per-call execution attempt commit
       -> only then physical invoke
       -> per-call terminal results(can complete out of order, exact attempt join)
       -> all calls terminal
       -> provider safe point may install a newer immutable context binding revision
       -> next provider call freezes its own exact revision + conversation cut
       -> accepted final assistant(exact revision + frozen cut)
  -> completed / interrupted / outcome_unknown
~~~

这里合并旧路线的text slice、tool slice和最小resume slice；模型是否调用tool不再决定storage authority。

Stage 2工程上按以下顺序构建，每一步可独立PR、独立全绿，但前五步都必须保持普通Host composition不可达：

1. fresh conversation schema、minimal `durable_jobs`/`durable_job_attempts` schema、claim repository、job-result acceptance port、global blob contract与migration，默认dormant；
2. 只供fresh-DB tests使用的conversation runner；
3. context/Inspector/query readers；
4. Protocol v3 Python service与Go consumer；
5. isolated fresh-DB dogfood；
6. 一次reset + production activation，随后才允许普通Host写新authority。

不得用feature flag按session混用authority，不dual-write，不建立online EventLog translator。下面的“同时交付”指第6步activation release，不要求前五步压成一个巨大代码提交。

**activation时同时交付的不可拆工作面**

1. `sessions/turns/turn_context_binding_revisions/transcript_entries/assistant message blocks/tool_execution_attempts/tool_results/context_snapshots/blobs/durable_jobs/durable_job_attempts`直接schema、commit-ordered entry sequence/session high-water、control revision、assistant exact revision + `provider_input_through_sequence`、`(session_id, command_id)`、assistant-message/call/attempt/result pairing、job/claim fencing与final winner唯一约束；
2. session Host writer lease/generation，只对Host-owned foreground/session-control mutation做数据库fencing；
3. Host open时原子 acquire/takeover + 旧running→interrupted；不构造旧RuntimeSession，不恢复provider/tool；
4. user、final assistant、完整assistant tool-request message、逐call execution attempt与terminal result、turn context binding revisions/current pointer、interrupted/unknown全部走同一authority；
5. Python transcript application service以及context compiler、Inspector、compaction source、prompt/session query等全部production readers；
6. Protocol v3 repeatable-read snapshot/page/sequence cut、sequence+control-revision observation、GAP、live interaction query与canonical-row command query/idempotency contract；
7. Go client sequence-indexed cache、control revision invalidation、same-Host live interaction、reconnect/GAP/controller mutation/ACK unknown迁移；
8. minimal job claim/result-accept service，以及所有从新foreground/Host可以创建或消费的background capability：terminal monitor notification、background compaction precompute、post-compaction memory extraction、background subagent及其必要result acceptance；当前turn所需的safe-point context snapshot generation不建job，只在成功时提交snapshot/revision；
9. crash、user acceptance ACK loss、snapshot并发commit、control-only lost notification、pending interaction takeover、mixed/multi-tool attempt/result batch、single-writer takeover、TUI reconnect、job claim/takeover、blob GC与side-effect黑盒tests。

可选的`NO_TOOLS` direct-schema spike只能在启动前明确选择且整个session无tool exposure，用来测SQL/commit路径；它不进入普通Agent生产，不产生与EventLog tool turn混合的session。

**停止产生/删除依赖**

从cutover开始，所有foreground turn停止写 RunStart、ReplyStart、ModelStart、stream segment、terminal projection、Disposition、ReplyEnd/RunEnd、window/account/reservation、tool start/chunk/end和foreground checkpoint/audit event。旧background模块可以暂时留在tree中，但只能处于production-unreachable/dormant状态；任何可被新Host或foreground创建、查询、等待或接受结果的background work，在activation时必须已使用minimal job kernel。若某capability尚未迁移，必须在同一release中从production catalog/admission明确禁用；禁止读旧job authority的bridge、dual consumer或“先写旧表再导入新conversation”。

**保留不变量**

- provider调用前user input已commit，并已通过prepared-input owner从同一MVCC cut冻结exact binding revision与`provider_input_through_sequence`；
- 一个turn最多一个accepted final assistant；assistant+completed turn为一个transaction；
- 一个assistant tool-request message的mixed text与全部calls/ordinals原子commit后才允许任何invoke；
- 每个实际invoke前先committool execution attempt；parallel results可分别commit并精确pairing；全部calls拥有terminal result后才进入follow-up provider call，lowering按call ordinal；
- message commit后call无attempt在rehydrate时解释为not_dispatched；attempt无result才解释为outcome_unknown；二者都不自动invoke；message transaction未commit则任何call都不得已执行；
- open只做conversation rehydrate，不做旧execution replay；
- context rematerialization对interrupted multi-tool message按原call ordinal生成provider-only closed interruption units，不向provider发送悬空call，不伪造canonical result；
- turn可在provider safe point追加immutable context binding revision并推进current pointer；旧revision不可覆盖，每条accepted assistant message绑定exact revision并原样保存本次pre-dispatch cut；assistant commit不得重读latest sequence；所有被revision引用的snapshot不可替换/GC，unreferenced snapshot可按retention删除；
- pending interaction不在canonical snapshot/open中恢复；same-Host live query可重取，takeover后旧resolution失败；
- 同一session任一时刻只有当前writer_generation可以提交Host-owned mutation；background job worker不在该domain内且不能写transcript；
- foreground-reachable durable background work只使用Stage 2 minimal job kernel；不存在旧job result到新conversation的bridge，未迁移capability不可出现在production catalog/admission；
- 同command id重试不创建第二个turn/queue item，query不依赖generic receipt；
- TUI snapshot metadata/control/rows与sequence/control revision来自同一MVCC cut；page绑定epoch和cut sequence；Observe任一high-water落后即要求snapshot；
- TUI、context、Inspector、compaction看到同一个canonical顺序；compaction不改写该顺序或epoch；所有大内容只通过global blob FK读取。

**代码修改面**

- storage migration/persistence ports与composition；
- Host submit/open/resume/lease takeover；
- runtime/run_entry.py、runtime/agent.py、runtime/tool_loop.py、llm runtime/segment assembly；
- context compiler、Inspector、context snapshot producer/source readers、binding revision repository、pre-dispatch immutable prepared-input handle与safe-point pointer advance；
- minimal durable job aggregate/attempt repositories、claim lane、result-accept port，以及foreground-reachable terminal monitor/background-compaction/background-subagent handlers；foreground safe-point compaction走独立process-local调用与terminal snapshot commit；
- purpose-neutral blob repository、canonical FK publication与orphan GC；
- terminal_protocol gateway/schema/generated carriers、command query与canonical target lookup；
- `clients/terminal` protocolvalue/presentation/state/cache/app；
- direct-schema test factories和故障注入harness。

**数据/reset策略**

fresh/reset schema production cut；同一turn绝不双写。若必须保留真实数据，只在停机窗口做一次离线export/import：accepted完整assistant message/facts导入，running/partial/conflict与pending interaction统一导入为interrupted；旧tool effect只有在可证明dispatch时才导入attempt，call无attempt导入not_dispatched，attempt无result导入outcome_unknown；已被历史assistant output采用的context summary连同exact正文/contract与binding attribution导入，无法证明adoption的snapshot可归档；只有可证明intent/attempt/result的background work导入minimal job kernel，其余标记unknown或冷归档；旧DB保持只读冷归档。

**独立 gate**

- 普通Agent暴露tool后，模型动态选择纯text或tool，两条路径都只写direct schema；
- steady-state（无需新compaction）text目标2个canonical transaction，one-tool physical happy path目标5个；完整turn为`2+B+C+E`、上界`2+B+2C`（B=tool-request messages，C=全部calls/results，E=实际attempts），单round N calls上界`2N+3`；需要新context snapshot时分别报告snapshot commit与binding revision install/pointer advance；这些是预算，二者agent_events写入均为0；
- 第一个新schema running turn在任意delta crash后即可由同版本open原子标interrupted；
- assistant tool-message/attempt commit/physical invoke/每个result/final的crash windows符合7.2；message或对应attempt未commit时该external invoke count必须为0；
- user/queue acceptance commit后丢ACK，同command id retry/query返回唯一target，不同input冲突且无receipt row；
- 两个Host竞争同session时只有一个generation提交成功，takeover后旧writer所有Host-owned mutation失败；合法background claim不受影响；
- Protocol v3 Python/Go contract、repeatable-read snapshot、cut-bound page、sequence+control revision level-trigger、GAP、reconnect通过；transcript或control通知丢失都不影响最终可见性；
- 并发commit故障注入不能产生high-water与suffix/control不属于同一read cut的response；
- canonical entry sequence只能在entry transaction内按commit order分配；并发tool result/final commits、rollback与ACK loss不能产生低sequence晚提交、published high-water空洞或cut后entry落入旧cut；
- queue/session/turn control-only mutation不追加entry时仍递增control revision并唤醒observer；不生成control event/history；
- tool-request message commit通过entry sequence可见；tool attempt insert递增control revision；terminal tool result通过entry sequence可见；turn interrupted递增control revision；snapshot在同一MVCC cut读取entry、attempt、result与turn facts；
- pending interaction在same-Host reconnect可重新query；Host kill/takeover后canonical snapshot不含request、running turn interrupted且旧resolution fail closed；
- mixed text + 2个以上calls只出现为一个完整assistant message；attempt/results乱序/部分commit时pairing和call ordinal稳定，未全部terminal绝不follow-up；
- restart后新turn的provider input对已知result、call-without-attempt、attempt-without-result按原ordinal形成合法closed message sequence；synthetic closure只在provider lowering中存在，canonical `tool_results`数量不增加；
- initial revision 0与user/turn acceptance原子可见；initial与mid-turn context snapshot/revision commit都不改变retained lower bound或epoch；只有safe point能新增后续revision并推进turn pointer，source upper bound必须早于turn user entry并精确拼接全部current-turn delta；每条accepted assistant绑定当时revision与pre-dispatch `provider_input_through_sequence`，二者都来自同一prepared-input handle；旧revision/被引用snapshot的删除或replacement被数据库拒绝；
- outcome_unknown的旧call只能通过新turn/new call重试；旧remote outcome晚到时只能填充旧call尚不存在的唯一result，旧turn与已accepted assistant attribution不变；通过result sequence与每条assistant cut比较后，future lowering只对明确late的outcome生成typed late-effect observation；
- terminal monitor、background compaction precompute、post-compaction memory extraction或background subagent从新Host创建work后，job/attempt/result只出现在minimal job kernel，Protocol v3/context/Inspector不需读旧projection-job authority；foreground safe-point compaction不创建job；
- canonical row永不引用missing/unverified blob；24小时orphan GC与late install竞态由FK/RESTRICT稳定结算；
- context、Inspector、compaction读取不需要merge EventLog与transcript。

transaction/row计数是架构预算；单authority、唯一commit、crash语义、fencing与reader一致性才是correctness gate。

**回滚边界**

只能整体回退binary + DB snapshot/reset；不让旧binary读取新schema，不保留v2 server translator，不以dual-write作为rollback机制。

### 8.6 阶段 3：删除 exact execution recovery、derived authority并压缩 close

**目标**

阶段2已让所有foreground writer/reader转到direct schema；本阶段按“先断依赖、再删owner、最后删表/test”的顺序，物理删除旧exact-recovery图和Presentation Foundation，把Host close收缩为对真实process-local execution与canonical writer负责。

**删除内容**

- model stream/control disposition recovery、dormant RunOwner与recovered terminal successor；
- pending interaction request的suspended-run recovery、resume link/receipt、MCP continuation replay与reconciliation owner；accepted decision row保留；
- stable RunFinalization candidate/repair-driven retry与temporary RuntimeSession teardown；
- ToolExecutionStableCandidateOwner、terminal/suspension confirmation、physical handoff与tool terminal projection；
- 9个foreground committed reducer、post-fold receipt、committed reducer repair；
- per-reducer runtime checkpoint maintenance、authority materialization与foreground projection jobs；
- transcript/tool/provider-input/final-output projection作为第二authority；
- terminal Presentation Foundation、root/head/retention owner、Protocol v2 snapshot/page/GAP server、ControlProjectionCursor/per-section source version/fingerprint与Go root/control cache；目标只保留DB sessions.control_revision当前值；
- terminal command receipt store与PENDING_CONFIRMATION/RECONCILIATION/compatible-winner query path；v3 query已直接读取canonical target；
- Host close中的reducer、checkpoint、audit、presentation、publication与repair fixed-point drain。

**保留不变量**

- stage2 direct conversation/tool-attempt/result/context-binding facts继续是唯一foreground authority；
- completed turn不可改写，running只一次变interrupted；
- call无attempt始终not_dispatched；attempt无result始终unknown且不自动retry；
- Host crash/takeover后pending interaction request不存在，open不恢复suspended interaction execution；
- rebuildable checkpoint完全缺失时仍能按transcript分页open；被binding revision引用的context snapshot不属于可删除checkpoint；
- TUI v3、Inspector与context不import旧presentation/reducer；
- unreferenced context snapshot删除不改变transcript或epoch；被binding revision引用的snapshot受FK保护；
- foreground退出遵守bounded cancel/join，owned OS process/MCP连接收到stop。

**代码修改面**

- host/resume.py、host/session.py；
- host/mcp_recovery.py、runtime/run_execution/interaction.py、interaction_transition.py中跨Hostpending-request continuation；
- runtime/session.py composition/close/latches/reducer registration；
- runtime/model_stream_recovery.py、runtime/model_control_recovery.py、run finalization/repair；
- runtime/terminal_projection.py、llm/terminal_projection.py与tool execution owner；
- runtime/projection_checkpoint_maintenance.py、committed_reducer_repair.py、post_fold、authority_materialization、foreground projection jobs；
- terminal presentation service、v2 Python protocol分支与Go presentation root/cache；
- runtime/terminal_application/command_receipt.py与terminal_command_receipts schema/test；
- old owner contract、repair order、checkpoint、v2 reconnect tests。

**数据/reset策略**

阶段2已经reset/cutover；旧EventLog和projection表可以暂留到阶段5供只读冷审计或production-unreachable的dormant migration tooling使用，但不得被任何production foreground/background handler读写。checkpoint、presentation root、candidate与receipt不迁移；物理drop可与对应import graph清零同commit发布。

**独立 gate**

- production open/resume不构造RuntimeSession、不调用provider/tool，除旧running→interrupted外无repair写；
- production open/resume不materialize pending approval/plan/MCP request；same-Host live query与accepted decision query不依赖旧recovery owner；
- production foreground import graph不含model/control recovery、stable candidate、committed reducer/checkpoint、Presentation Foundation；
- checkpoint/presentation数据全空或表不存在时text/tool/resume/TUI仍正确；
- Host close只有3个logical phase、0 committed-reducer barrier；await数≤12是审查预算；
- Stage 1保留的audit/checkpoint/presentation physical cancel/join只有在对应owner/executor已删除且无task可产生后才归零；
- DB pool/artifact store释放后，task inventory与故障注入均证明没有旧owner继续访问session resource；
- physical operation超deadline时产品状态清晰，close不启动第二代repair owner。

**回滚边界**

以stage2 schema snapshot和对应binary整体回滚。被删owner不允许通过feature flag重新消费新transcript row；需要回退旧体系时恢复pre-stage2旧DB/binary，而不是在线reverse projection。

#### 8.6.1 删除顺序一：resume/finalization recovery

**目标**

在阶段2最小transcript-only open已经上线后，删除“精确结束旧executor”的全部旧恢复路径。此处不能先于阶段2单独上线。

**删除内容**

先让open/TUI接受interrupted状态，再删除：

- ModelStreamRecoveryService；
- ModelCallControlDispositionRecoveryService；
- materialize_dormant_run_owner；
- recovered ContextWindowClosed/account close/RunEnd batch；
- recovered final-output materialization；
- temporary RuntimeSession与NonHost teardown capability；
- stable RunFinalization repair/retry owner；
- active reservation恢复对resume的gate。
- PendingInteractionResumeLink、Pending*Authority的跨Hostrequest恢复、interaction transition repair/reconciliation；accepted interaction decision的direct row/query不删除。

**保留不变量**

- open幂等；
- completed turn永不被改写；
- running turn一次性变interrupted；
- tool call无attempt解释为not_dispatched；tool attempt无result解释为outcome_unknown；
- pending interaction不重建，旧resolution在新writer generation稳定失败；
- transcript order与compaction range保持一致。

**代码修改面**

- host/resume.py大幅改写；
- runtime/model_stream_recovery.py删除；
- runtime/model_control_recovery.py删除；
- runtime/run_execution/finalization.py简化/删除；
- runtime/session.py non-Host teardown删除；
- Inspector resume状态。

**数据/reset策略**

新schema没有旧execution candidate。若导入旧数据，只有accepted facts；running/partial一律interrupted。

**独立 gate**

- resume不构造RuntimeSession；
- resume本身不调用provider/tool；
- 除一次running→interrupted transaction外无修复写；
- 同session重复resume不产生第二条interruption；
- transcript加载结果与crash前已commit prefix一致。

**回滚边界**

source + DB snapshot；没有online reverse projection。

#### 8.6.2 删除顺序二：Host close压缩

**目标**

3阶段、0 reducer barrier、0 checkpoint/audit/UI hard wait；≤12 awaits是结构审查预算，不是用机械合并coroutine取得的correctness证明。

**删除内容**

- 四次 drain_open_committed_reducer_barrier；
- run-finalization repair drain；
- committed-reducer repair/post-fold drain；
- runtime/transcript/prompt/subagent checkpoint drain；
- context audit drain；
- terminal presentation drain；
- generic projection job completion wait；
- child teardown retry generation/reconciliation lineage。

**保留不变量**

- 不再接受新foreground work；
- owned process收到terminate；
- canonical pending write flush；
-未完成turn/tool有明确interrupted/unknown；
- durable job row/lease在Host退出后仍可检查；是否允许新worker执行由Stage 2已激活的handler safety class与attempt retry规则决定，close本身不把lease超时的非幂等work改回pending。

**代码修改面**

- host/session.py aclose；
- runtime/session.py close/teardown；
- runtime/subagent/execution.py；
- MCP/terminal process supervisor的bounded stop接口；
- writer flush接口。

**数据/reset策略**

无特殊迁移；close状态不作为恢复authority。durable job保留`expires_at`供阶段4按safety class处理；close不自行把expired lease回pending。

**独立 gate**

- AST await count目标≤12，超出需解释但不替代行为gate；
- reducer barrier调用=0；
- background job人工阻塞不延长Host close；
- audit/checkpoint/archive故障不阻塞close；
- p95 idle close<1s，hard deadline≤5s；
- deadline后turn状态仍清晰。

**回滚边界**

旧close无法安全读取新execution schema，因此仍以binary + snapshot为单位回滚。

### 8.7 阶段 4：完成background handler迁移并删除legacy projection-job graph

**目标**

Stage 2已激活`durable_jobs` aggregate、`durable_job_attempts` journal、claim repository与result-accept port，并迁移了所有foreground-reachable handlers。本阶段不创建第二次job authority cut；它只迁移在Stage 2被明确禁用、因而production-unreachable的剩余handlers，然后物理删除旧projection-job schema/runtime/tests。目标job kernel仍只承载必须跨Host生命周期存在的work；不是把旧projection jobs改名，也不把lease等同于external effect exactly-once。job claim domain与session writer domain完全独立，Host takeover不能使合法background result失效。

**删除内容**

- durable projection activation/cutover/coverage/seed/target head/result receipt/repair表；
- projection-specific lease与confirmation；
- foreground reply/tool/TUI/evidence job；
- child RuntimeSession作为background continuation载体；
- 旧projection-specific target attempt/head、stable candidate、result receipt、repair action等companion graph；它们由一个通用但窄的physical `durable_job_attempts`关系替代，不复制旧proof graph。

**保留不变量**

- terminal monitor承诺的notification可restart，但原terminal command绝不因monitor lease过期而重启；
- background subagent只有pure/idempotent handler才可创建下一execution attempt；queryable handler只能围绕原attempt重新观察；
- compaction/memory extraction最终有completed/failed结果；
- durable prompt queue顺序与claim可恢复；
- job handler safety class显式，默认`NON_IDEMPOTENT`；
- stale attempt claim generation不能commit progress/result；
- non-idempotent lease loss使current attempt与job aggregate变outcome_unknown，不自动pending；
- worker只写job/attempt-owned result/blob/message，不写session transcript；
- 当前Host以writer generation显式接受completed job result后，结果才进入conversation；
- job enqueue/cancel request用writer generation，attempt claim/progress/result/failure/settlement只用attempt claim generation，二者predicate不交叉；
- 旧attempt永不被下一retry覆盖；job aggregate只引用current/accepted terminal attempt。

**代码修改面**

- projection_jobs/contracts/runtime；
- terminal monitor coordinator；
- subagent task storage/runtime；
- compaction memory extraction；
- prompt queue；
- memory governance background executor；
- Host job-result acceptance application service与transcript integration。

这个清单同时包含“删除旧wiring”和“迁移Stage 2曾明确禁用的剩余handler”；不允许借本阶段重新改造Stage 2已激活的job schema/claim/result-accept语义。

`durable_jobs` row最少保存：job id/type、payload/blob ref、aggregate status、safety class、current attempt id、accepted result/error ref、cancel request、created/updated time。`created_by_writer_generation`可作审计，但worker conditional commit不得检查它。

`durable_job_attempts` row最少保存：job id、attempt id/ordinal、claim generation、lease owner/expiry、started/terminal time、stable remote/idempotency identity、status、immutable result/error ref、retry_of_attempt_id。一个job最多一个active attempt；job aggregate与terminal attempt在同transaction更新。唯一性与generation conditional update直接由这两张窄表和PostgreSQL约束完成，不增设receipt/confirmation/target-head表，也不允许`attempt_summary JSON`代替历史rows。

handler规则：

1. `RETRY_SAFE`：lease expiry/loss后terminalize旧attempt并创建带`retry_of_attempt_id`的下一attempt；不得覆盖旧row；
2. `REMOTE_QUERYABLE`：新owner可换代同attempt claim并按stable remote/launch id查询，只提交observation；未查明则unknown，不重做effect；
3. `NON_IDEMPOTENT`：lease expiry/loss直接conditional update当前attempt与job aggregate为outcome_unknown；只有用户/policy显式创建新attempt才可再执行，且记录`retry_of_attempt_id`、actor和reason。

Host cancel只提交`cancel_requested`授权事实；current attempt claim owner观察后以attempt id + claim generation提交cancelled/failure/settlement。worker完成只提交immutable attempt result并在同transaction推进job aggregate。若conversation需要该结果，当前Host创建独立accepted transcript entry/turn mutation；entry直接保存`source_job_id`并保证session内唯一，或复用stable acceptance command id，因此这个accept可以在Host takeover后发生、ACK unknown后幂等重试，但不能被worker transaction隐式完成。

**数据/reset策略**

复用Stage 2已激活的durable_jobs + durable_job_attempts表，不再执行schema authority reset。对Stage 2曾禁用的剩余handler，只有能证明尚未开始的旧product work可在停机迁移为pending job；每个已leased/started record必须导入独立attempt，可能触发外部effect或证据不足的attempt导入为outcome_unknown，其余冷归档。禁止同时运行旧/new worker或在迁移后保留bridge。

**独立 gate**

- `RETRY_SAFE` worker在attempt claim后crash，lease过期后旧attempt保留且下一attempt引用它；
- `REMOTE_QUERYABLE` worker crash后围绕同一remote identity只query，不重新launch/invoke；
- `NON_IDEMPOTENT` worker crash/lease loss后current attempt与job进入outcome_unknown，自动invoke count不增加；
- stale generation result commit被数据库拒绝；
- job由Host generation N enqueue且attempt被worker claim后，Host takeover到N+1；worker以current attempt claim generation仍可commit result；
- worker result commit后session transcript保持不变；只有N+1 Host显式accept后才新增canonical entry；
- job result acceptance commit后丢ACK，以同一source job/command identity重试不产生第二个entry；
- stale Host generation不能enqueue/cancel/accept result，stale attempt claim generation不能progress/result/settle；两种失败互不传播；
- result commit后不重复执行；
- Host close不等待job完成；
- foreground model/tool execution本身没有job row；只有用户/产品明确承诺跨Host完成的background work由foreground提交job intent；
- Stage 2已迁移的foreground-reachable handler在本阶段前后使用相同job/attempt authority，无二次cutover或语义变化；
- 旧projection-job table/runtime/import与capability registry命中全部为0；所有产品background handler已使用minimal job kernel；
- job aggregate只使用pending/active/succeeded/failed/cancelled/outcome_unknown，attempt使用leased/terminal/outcome_unknown closed state；
- compaction job成功/失败/retry attempt都不改变transcript epoch或entries；completed context snapshot被binding revision引用后受外键保护；
- 无target-head/receipt/repair companion row。

**回滚边界**

job payload/version必须与binary同版本；rollback使用snapshot，不做双worker。

### 8.8 阶段 5：最后退役 universal EventLog与旧schema

**目标**

物理删除已经没有产品消费者的通用ledger、replay、serializer、projection和migration关系。

**删除内容**

- agent_events及151类EventType旧grammar；
- EventLog writer/physical accounting/materialization account；
- replay timeline/message reducer中旧event拼装；
- runs旧projection；
- durable projection表族；
- canonical mutation中纯delivery/head/migration-binding关系；
- old Inspector candidate/checkpoint/receipt页面；
- 所有仅验证已删除owner的测试。

**保留不变量**

- session/conversation/tool-attempt/job-attempt/memory/context-snapshot/blob可直接查询；
- cold archive可供历史审计，但生产不读；
- schema migration仍由verified runner执行；
- canonical closed payload保留有限per-domain upcaster/golden，不以旧EventLog historical decoder替代；
- PostgreSQL仍是唯一线上authority。

**代码修改面**

- event/；
- event_log/；
- replay/；
- storage migrations；
- Inspector；
- test support/factories；
- production composition。

**数据/reset策略**

最终reset-only schema。旧DB只读冷归档，不挂到production connection pool。

**独立 gate**

- production import graph没有旧event_log/reducer/checkpoint/repair模块；
- text/tool指标达标；
- fresh database完成全部行为gate；
- 没有compat shim、dual-write或background backfill owner；
- old DB只读冷归档不在production connection pool，旧binary不能打开新schema；
- EventType/产品表目标≤24、净删production code目标≥22,000行作为architecture review budget单独报告；即使数值达标，若仍存在巨型JSON authority、通用receipt graph或第二transcript source，本阶段仍失败；数值未达标但全部correctness gate通过时必须逐项审查，而不是伪造合并。

**回滚边界**

最终cutover前保留完整旧DB snapshot与旧binary；cutover后若回滚，整体恢复二者，不允许新旧schema混用。

### 8.9 进入实施规格的边界

本文到此作为architecture baseline，不把SQL/DTO伪代码继续扩写成隐藏的implementation spec。下一份规格应独立选择一个可交付范围：

- **Stage 0/1 spec**：配置默认、semantic latch移除点、各owner stop-admission/physical cancel/join contract、resource release order与故障注入；或
- **Stage 2 spec**：foreground direct schema和Protocol v3 coherent production activation；允许多个dormant construction PR，但不能拆成text/tool/TUI/rehydrate子上线。

若选择Stage 2，规格在编码前必须逐项冻结：

1. user/turn、assistant/final、完整assistant tool-request message及ordered blocks、逐call tool execution attempt与terminal result、context snapshot、binding revision install/current-pointer advance、accepted interaction decision、open/takeover、job enqueue/cancel、job-result acceptance的SQL transaction boundary；provider-generated assistant transaction还必须消费唯一prepared-input handle并保存其exact revision/cut；
2. session-wide command id物理唯一形状、same-input comparison与canonical query SQL；
3. per-session entry sequence在canonical entry transaction内按commit order分配、`latest_sequence` high-water定义、rollback/并发commit规则、`control_revision`递增mutation集合与observation pair、retention lower bound与gap规则；
4. writer lease acquire/renew/takeover predicates，以及与job-attempt claim predicates完全分离的ports；
5. durable job aggregate/attempt claim/result/retry lineage/cancel request与source-job acceptance唯一约束；
6. Protocol v3 canonical snapshot/page/observation/query DTO与独立same-Host live-interaction DTO、read-only repeatable-read transaction和Go cache transition；
7. assistant message/call ordinal/attempt/result pairing约束、pre-dispatch provider conversation cut的prepared-input owner、assistant `context_binding_revision_id + provider_input_through_sequence`字段矩阵、全call terminal follow-up query、按assistant cut判定的versioned provider-only interruption/late-effect lowering contract，以及`2+B+C+E <= 2+B+2C`预算测量；
8. global blob canonical encode/digest/FK/RESTRICT、24小时orphan grace与GC竞态；
9. conversation rehydrate、context rematerialization、effect reconciliation、audit reproduction与明确禁止execution replay的API/命名边界；
10. reset/cold archive、cross-language fixtures、ACK unknown、concurrent transcript/control read、lost notification、pending interaction takeover、mixed/multi-tool crash、stale generation和physical resource shutdown fault matrix。

该规格不得引入兼容reducer、command receipt graph、control transition log/per-section cursor、durable interaction request、read snapshot root、compaction epoch rewrite或跨domain generation binding。

---

## 9. 验收指标

### 9.1 架构预算与观测目标

以下数字用于量化durability amplification是否真正下降。标为“审查预算”的项目不直接判定correctness；偏离时要求解释和architecture review。标为“结构gate”的项目反映本方案明确禁止的依赖，可以直接阻止阶段完成。

主路径量化目标是：在无新compaction的steady state，普通text turn从当前至少15个durable write scope降到2个，one-tool physical happy path从至少31个降到5个；Host close从45个await、4个reducer barrier压到3个逻辑band、0 barrier，并以≤12个await作为结构审查预算。

| 指标 | 当前实测/静态值 | 推荐目标 | 属性 |
|---|---:|---:|---|
| steady-state text durable transaction/write scope（无新compaction） | ≥15 | 2 | 审查预算 |
| text EventLog transaction对照 | 11 | 2 | 审查预算 |
| text durable object/fact | ≥47 | ≤4（turn、user、initial binding revision、final assistant） | 审查预算 |
| steady-state one-tool durable transaction/write scope（无新compaction） | ≥31 | 5 | 审查预算 |
| one-tool EventLog transaction对照 | 23 | 5 | 审查预算 |
| one-tool durable object/fact | ≥91 | ≤7（不展开assistant message内部typed blocks） | 审查预算 |
| 单个N-call tool round canonical transaction | 当前未单独测量 | N+E+3，E≤N；physical上界2N+3（完整turn上界2+B+2C） | 审查预算；message/attempt correctness优先 |
| 单个N-call one-round logical product item | 当前未单独测量 | N + E + 5，E≤N；全部physical时为2N + 5（turn、user、initial binding revision、tool-request message、E attempts、N results、final） | 审查预算；assistant block child row数单独报告 |
| canonical control observation authority | v2 per-section version/fingerprint/control cursor | 1个session control revision、0 transition history | 结构gate |
| durable pending interaction request/恢复owner | 当前存在suspended/recovery graph | 0 | 结构gate |
| EventType vocabulary | 151 | ≤24 | 审查预算 |
| 产品SQL tables | 61 | ≤24 | 审查预算 |
| text owner family | ≥14 | ≤3 | 审查预算 |
| one-tool owner family | ≥17 | ≤5 | 审查预算 |
| foreground committed reducers | 9 | 0 | 结构gate |
| mainline hard reconciliation latches | 6 | 0 | 结构gate |
| restart branch family | ≥8 | ≤3 | 审查预算 |
| Host close logical bands | ≥6 | 3 | 行为/结构gate |
| Host close awaits | 45 | ≤12 | 审查预算 |
| committed-reducer barriers | 4 | 0 | 结构gate |
| 默认context audit artifact/model call | 4 | 0 | 默认配置gate |
| 预计production LOC | 当前HEAD | 净删≥22k | 审查预算 |

对象/fact百分比使用当前下界，因此实际降幅可能更高。`EventType ≤24`、产品表`≤24`、Host close `≤12 awaits`与净删`≥22k` production LOC全部只是architecture review budget和减法信号，不是correctness的替代品。它们不能通过巨型JSON、合并无关类型、生成代码迁移、删除可观测性或机械合并coroutine来取巧，也不能让一个行为错误的cut通过。

### 9.2 Correctness gates

下列条件是production cut的硬gate：

1. **单authority**：普通Agent暴露tool后，无论模型动态选择text还是tool，同一turn及同一session transcript都只来自direct schema；不存在EventLog/new transcript merge reader或dual write；
2. **切换原子性**：允许多PR dormant construction，但第一次写新foreground row的production activation已经具备同版本open/rehydrate、context、Inspector、compaction source、TUI v3 reader与minimal job kernel；所有foreground-reachable background capability已切换或在production明确禁用，不存在old/new job bridge；
3. **single Host writer**：每session只有一个当前writer generation；takeover后旧generation的turn/transcript/foreground tool attempt/result/queue/accepted-interaction-decision/job-control authorization mutation全部被PostgreSQL拒绝；
4. **fencing domain独立**：Host takeover不改变合法job-attempt claim；worker progress/result/failure只校验attempt id + claim generation且不能写transcript；当前Host接受job result时只校验writer generation；
5. **client mutation幂等**：turn/queue/accepted-interaction-decision canonical row持有session-wide command id；user acceptance ACK unknown后同id/same input返回原target，同id/different input稳定conflict；query不依赖receipt row；
6. **final唯一commit**：assistant entry与turn completed同transaction，一个turn最多一个winner；ack unknown只按stable primary key读winner；
7. **crash语义唯一**：model stream、pending interaction或未完成foreground execution跨进程后只变interrupted，不恢复coroutine、cursor、request、candidate或provider outcome；
8. **side effect不静默重做**：call无attempt可证明not-dispatched，attempt无result与non-idempotent job-attempt lease loss才是outcome_unknown；foreground每call最多一attempt，显式retry必须是新turn/new call；late exact outcome只能填充旧call尚不存在的唯一result，不能覆盖、倒插或改写旧turn；自动invoke增量为0；
9. **job retry安全与lineage**：stale attempt claim generation不能commit；retry-safe重执行创建新attempt并保留retry_of，remote-queryable只observe旧remote identity，旧attempt永不被覆盖；
10. **canonical read cut与唤醒一致**：per-session entry sequence只在canonical entry transaction内按commit order分配并与session high-water原子推进；Protocol v3 snapshot metadata/control/rows/tool attempts、latest sequence与control revision来自同一repeatable-read MVCC cut；page绑定epoch/cut sequence；Observe比较sequence + revision，tool attempt insert及其public remote-identity更新推进revision，任一notification丢失都不造成永久漏读；
11. **semantic context与per-call cut边界**：compaction只追加immutable context snapshot/binding revision，不删除/重写/重排transcript，不改变epoch/retention lower bound；initial revision与user/turn原子安装；revision source upper bound早于turn user entry，rematerialization拼接全部current-turn exact delta；只有provider safe point可新增revision并原子推进turn current pointer。每次provider dispatch从同一pre-dispatch MVCC cut冻结exact revision与`provider_input_through_sequence`，accepted provider-generated assistant只消费该prepared-input handle并原样保存二者；因而mid-turn compaction可用，且late result不能借assistant自身sequence或共享revision伪装成历史input。unreferenced snapshot可GC，被revision引用的snapshot受FK保护且不能重新生成替换；
12. **derived plane不反向否决**：checkpoint、projection、presentation、audit、TUI delivery、search index failure不能改变accepted user/assistant/tool fact；
13. **close有界且physical safe**：最终stop ingress、bounded foreground termination/marking、canonical flush/resource stop三段完成；Stage 1不等待旧owner业务成功，但仍在资源释放前bounded cancel/join其physical task，Stage 3删owner后相应await才归零；
14. **control revision保持最小**：只有Host-owned用户可见且不靠entry sequence唤醒的canonical control transaction递增sessions当前值，包括tool attempt insert、public remote-identity update与turn interrupted；context snapshot/binding revision/current pointer不进入public control projection且不递增revision；没有control event/history、per-section cursor、receipt、checkpoint或background-worker写入；
15. **pending interaction ownership唯一**：request只存在于当前Host live control；same-Host可level-query，crash/takeover后消失且turn interrupted；canonical snapshot/open不恢复request，accepted decision绑定durable subject，secret只保存redacted disposition/keyed commitment；
16. **multi-tool message与attempt原子边界**：mixed text与全部calls/ordinals在一个assistant message transaction中commit；每个physical invoke前exact unique-per-call attempt commit；result精确绑定attempt，全部call terminal后才follow-up，provider lowering不按physical completion排序；rehydrate后的悬空call必须用versioned provider-only interruption closure按原ordinal闭合，不伪造canonical result；若旧result在后续assistant之后晚到，future lowering只能按实际sequence表达typed late-effect observation；
17. **blob publication唯一**：所有大内容只引用已验证immutable blob；canonical FK与ON DELETE RESTRICT阻止missing/dangling reference，24小时orphan GC不能删除referenced blob；
18. **恢复承诺分层**：conversation rehydrate与context rematerialization有versioned contract；effect reconciliation默认只query；execution replay不存在；audit reproduction不进入正常open；
19. **schema evolution封闭**：canonical closed payload只用SQL migration或有限per-domain upcaster演进；unknown version fail closed，production没有universal historical event decoder。

### 9.3 主路径性能与I/O预算

- model首token前最多1次canonical write：user/turn acceptance；
- final text完成后只有1次canonical write transaction；
- 每个assistant tool-request message 1次原子commit，每个call 1次terminal-result commit，每个实际physical call另有1次attempt commit；通式`2+B+C+E`且`E<=C`，上界`2+B+2C`；
- 上述2/5与transaction通式是复用已有binding revision或无需compaction的steady-state预算；需要新semantic snapshot时，snapshot commit与binding revision install/pointer advance分别单独报告，不得伪装成checkpoint I/O或从correctness计数中删除；
- attempt transaction保存“Runtime已经跨过dispatch ambiguity boundary”这一不可替代的产品事实；即使one-tool目标因此是5而不是4，也不得删除attempt来混淆not-dispatched与outcome-unknown；
- foreground call graph中 checkpoint write = 0；
- foreground call graph中 audit artifact write = 0（opt-in debug除外）；
- foreground call graph中 durable job enqueue = 0，除非该tool明确启动用户承诺的background work；
- TUI delivery不增加canonical transaction；
- p50/p95 text reply数据库等待相对阶段0不回退；目标是顺序写次数下降，而不是只优化单次SQL；
- idle Host close p95 < 1秒；任何close有统一≤5秒hard deadline；
- conversation rehydrate使用direct bounded query；context rematerialization时间随“binding revision source cut之后的exact conversation delta长度”增长，不随全部历史event/reducer数增长。

### 9.4 静态架构 guardrails

CI应直接失败于真正违反目标边界的结构：

- foreground模块import checkpoint repair、projection job或resume recovery owner；
- Host close出现 committed-reducer barrier；
- domain模块新定义 FULL/NONE/UNKNOWN/CONFLICT；唯一例外是storage adapter内部；
- 新增 stable candidate + receipt 配对类型；
- UI/publication调用 Runtime reconciliation latch；
- context audit自动从每个ModelStart触发；
- 同一turn或同一session foreground transcript同时写/合并读取agent_events和transcript_entries；
- checkpoint failure可到达assistant commit/turn completion拒绝分支；
- operational log/trace被resume读取；
- Host-owned canonical mutation没有writer_generation predicate，或stale Host generation仍可commit；
- background job worker mutation检查writer generation、合法attempt claim在Host takeover后失效，或worker可直接append session transcript；
- Host job enqueue/cancel/result-acceptance只检查claim generation或绕过writer generation；
- expired `NON_IDEMPOTENT` job attempt自动回pending/reexecute，或retry覆盖旧attempt row；
- Stage 2 production Host仍能创建/查询/接受旧projection-job authority，存在old-job→new-conversation bridge，或尚未迁移的background capability仍在catalog/admission可达；
- v3 canonical snapshot/page用多个autocommit read拼接、没有epoch/cut sequence/control revision，Observe只比较entry sequence，或把operational state混入canonical read cut；
- canonical entry sequence在transaction外预留、用允许乱序commit的nextval分配、异步更新session high-water，或rollback后仍发布sequence/high-water；
- 新增control transition event/history、per-section revision/cursor/fingerprint/receipt，或background worker递增session control revision；
- canonical snapshot/open包含pending interaction request，resume重建approval/plan/MCP request，或schema出现`interaction_requests`但没有新的产品决策；
- interaction resolution不同时校验current writer generation与process-local live interaction id；
- assistant tool call在完整parent message transaction commit前可见/可invoke，或mixed message逐callcommit；
- tool adapter在exact `tool_execution_attempts` row commit前被调用，call无attempt被标unknown，或attempt无result被标not-dispatched；
- 同一foreground logical call可建立多个physical attempts，显式retry不创建新turn/new call，foreground attempt保存可变started/terminal/unknown status，或late outcome覆盖既有result/改写旧turn与历史assistant attribution；
- 任一call尚无terminal result时发起follow-up provider call，result未绑定exact attempt、只能按完成sequence pairing，或丢失original call ordinal；
- restart后把悬空assistant tool call直接发给provider，把interruption closure持久化为canonical tool result，或用它授权自动retry；
- canonical submit target缺少`UNIQUE(session_id, command_id)`，或重新引入generic command receipt/confirmation/reconciliation table；
- compaction mutation删除/覆盖/重排transcript entry，或推进transcript epoch/retention lower bound；
- user/turn acceptance没有原子安装revision 0，current revision pointer跨turn/指向未committed revision，被binding revision引用的context snapshot被删除、原地更新、重新生成替换，turn在非provider safe point推进current revision，provider-generated assistant entry缺少exact revision或`provider_input_through_sequence`、caller在assistant commit时重新读取/自报cut、cut早于turn user entry或不小于assistant sequence、用assistant自身sequence/revision判断late outcome，或snapshot source覆盖所属turn user entry/漏拼current-turn exact delta；
- `durable_jobs.attempt_summary`或同类JSON覆盖多次physical attempt lineage；
- canonical prompt/tool/job/context/memory row引用未验证blob，domain新增专属artifact hold/receipt，或GC能删除仍被FK引用的blob；
- interaction decision只引用process-local live id，plan answer未成为canonical item，或MCP/external secret plaintext进入普通durable row/query response；
- production open调用execution replay、读取operational trace恢复coroutine，或引入universal historical event decoder；
- provider operation从pre-dispatch read到stream结束持有数据库锁或session-wide semantic-write lease，以“模型运行期间没有其他canonical write”为正确性前提，或据此省略assistant的`provider_input_through_sequence`；
- Stage 1在audit/checkpoint/presentation physical task退出前释放其DB/artifact/session资源；
- v3 server依赖Presentation Foundation root/head，或保留online v2→v3 transcript translator。

以下静态检查生成review report，不应仅凭数值让CI correctness失败：

- durable product record type超过24；
- 产品表超过24；
- owner/transaction/await/LOC预算偏离9.1；
- 一个JSON payload聚合多个有独立lifecycle、unique key或retention policy的产品事实；
- 删除行主要被新的generic abstraction、generated carrier或compat code抵消。

review必须逐项检查语义，既不能因“25”自动判错，也不能因“24”自动判对。

### 9.5 行为与故障注入矩阵

| 场景 | 注入点 | 必须观察到 | 明确禁止 |
|---|---|---|---|
| text-only reply | 普通Agent暴露tool，模型动态选择text | 同一direct schema；目标2 transaction；user + assistant + completed turn；TUI可读 | turn开始前预选text authority、Model/Reply lifecycle rows、checkpoint/audit writes |
| one-tool reply | 同一普通Agent，模型动态选择tool后再final | 同一direct schema；目标5 transaction；完整assistant message → attempt-before-dispatch → result-before-follow-up | 回退EventLog tool authority、merge reader、foreground durable job、为守4次删除attempt |
| mixed multi-tool message atomicity | assistant同时返回text + calls A/B/C；在message transaction每个insert点kill | 要么整message不可见且invoke count=0，要么text与A/B/C及ordinals全部可见后才invoke | 只持久化A、逐call先写先执行、半条provider message |
| tool-request message commit ACK丢失 | 完整message transaction commit后、Runtime收到success前断连接 | persistence adapter按stable assistant_message_id读取完整唯一winner；同进程确认后才创建attempt；若进程死亡则rehydrate只见完整message且所有call not_dispatched | 写第二message、按部分call猜winner、增加confirmation owner、无attempt就invoke |
| multi-tool out-of-order/partial results | A/B/C attempts已commit并行；C、A result commit后B side effect窗口kill | parent message完整；C/A精确pair到attempt；B attempt无result=outcome_unknown；turn interrupted；无follow-up；新context按A/B/C ordinal使用known results + provider-only unknown closure形成合法闭合sequence | 按result sequence重排、直接发送悬空call、伪造canonical result、自动重跑B、部分result触发follow-up |
| user acceptance ACK丢失 | turn/queue transaction commit后断连接 | 同command id retry/query返回原target；canonical user item只有1个 | 第二turn/queue item、generic durable receipt repair |
| command id conflict | 同command id改text/delivery mode重试 | stable conflict；原target不变；不写conflict row | compatible winner、覆盖原input |
| model stream crash | 任意第N个delta后kill | user保留；turn interrupted；partial assistant不进context；可显式重试 | 恢复旧stream cursor、合成伪completed reply |
| final reply commit前crash | assistant transaction开始前/rollback | 无assistant entry；turn interrupted | TUI显示未commit reply |
| final reply commit ack丢失 | DB commit后断连接 | 读stable entry_id后采用唯一completed winner | 写第二个assistant、启动repair owner |
| final reply commit后crash | transaction已commit、通知前kill | resume直接显示completed assistant | publication失败把turn改成failed |
| tool message commit后、attempt前crash | 完整assistant tool-request message commit后kill | 每个call无attempt=not_dispatched；turn interrupted；显式选择 | 自动invoke、标outcome_unknown、伪造failed result |
| tool attempt commit后、physical send前crash | exact attempt transaction commit后kill | attempt无result=conservative outcome_unknown；默认不重试 | 声称not-dispatched、删除attempt、自动重试 |
| tool side effect后、result commit前crash | concrete effect成功后kill | exact attempt + outcome_unknown + 参数/时间/actor；默认不重试 | 静默重复effect、声称exactly-once |
| tool result commit后、final reply前crash | result commit后kill | call/attempt/result保留；turn interrupted；新turn可继续 | 再执行tool |
| foreground explicit retry | 旧call的唯一attempt为outcome_unknown；用户或新model turn选择retry | 新turn产生new call及其唯一attempt；新attempt以`retry_of_attempt_id`跨call归因；旧call/attempt不变 | 在旧call下创建第二attempt、覆盖旧attempt/result、把retry当原turn continuation |
| interrupted attempt late exact outcome | retry call或后续assistant entry已经accepted后，旧remote query得到exact terminal outcome | 仅在旧call尚无result时追加其唯一result并保留实际entry sequence；逐条与历史assistant的`provider_input_through_sequence`比较；旧turn仍interrupted；future lowering只对明确late的outcome使用typed observation | 覆盖旧result、用assistant自身sequence/revision猜测可见性、倒插到历史provider input、把late result绑定retry call、创建per-attempt observation graph |
| Host close during model | stream进行中close | bounded cancel；turn interrupted；canonical writer flush | 等checkpoint、audit、UI |
| Host close during tool | tool可取消/不可立即取消 | bounded join；超时后unknown/interrupted；进程资源terminate | 无限等待physical lineage |
| Stage 1 close with derived I/O | audit/checkpoint/presentation正在DB或artifact I/O时close | 不等业务成功/追平；stop admission+bounded cancel/join；task退出后才release pool/store | de-gate后直接释放resource、后台task晚到访问 |
| conversation rehydrate | DB残留running turn或旧pending interaction | acquire新writer generation；一次幂等running→interrupted并推进control revision；加载conversation/context binding；无pending request | temporary RuntimeSession、execution replay、恢复interaction/provider/tool调用 |
| stale Host writer | Host B takeover后让Host A继续commit | A的turn/transcript/tool/queue/job-control authorization全部被DB拒绝；observer read仍可用 | compatible winner、跨writer reconciliation、双final |
| Host takeover during background job | job由Host A enqueue且attempt claimed，Host B takeover后worker完成 | worker按current attempt/claim generation正常commit result；旧attempt保留；transcript不变；B显式accept后才新增entry | result绑定A generation失败、worker直接写transcript |
| terminal monitor restart | monitor job attempt leased后kill worker | 新claim按原attempt launch token查询；completion/unknown可审计；已知通知最终发送 | 重新launch原command、覆盖旧attempt、Host close等待job完成 |
| Stage 2 background authority cut | fresh Host创建terminal monitor、compaction-memory或background subagent work | job、attempt与result只落minimal job kernel；current Host通过result-accept port纳入conversation | 写/读旧projection-job authority、online bridge、未迁移capability仍在catalog可调用 |
| initial context binding genesis | user/turn acceptance任意insert点kill | 要么turn、user与revision 0全不可见，要么三者同transaction可见且turn pointer exact指向revision 0 | turn存在但无binding、pointer跨turn、首个provider call临场创造无归因base |
| mid-turn context binding advance | 初始call成功，加入大tool result后预算超限；在compaction/safe-point transaction各阶段kill | 当前turn exact delta始终保留；成功时新snapshot/revision与pointer原子可见，下一assistant绑定新revision；失败时继续旧revision或typed infeasible | summary包含当前turn、非safe-point换版、覆盖旧revision、恢复ModelStart/End lifecycle |
| provider input exact cut late-result race | pre-dispatch在revision R冻结H=100并开始stream；旧tool result随后commit为101；assistant最终commit为102 | assistant保存R与`provider_input_through_sequence=100`；result 101明确不属于该assistant input，历史attribution不改，future lowering按late-effect处理 | 在assistant commit时重读101、以assistant sequence 102或共享revision R判定result已参与input、写ModelStart/End journal |
| commit-ordered sequence allocator | parallel tool result/final entry竞争分配sequence；在lock、insert、commit、rollback边界注入故障 | canonical commit顺序与entry sequence严格一致；rollback不推进session high-water；pre-dispatch cut H之后commit的任一entry都大于H | transaction外reserve、低sequence晚commit、high-water空洞、异步head追平或sequence repair owner |
| illegal context revision advance | model仍active、tool-request未commit、任一call未terminal或stale writer generation时尝试换版 | 数据库/port拒绝；current pointer与旧revision不变 | 先推进pointer再等待physical exit、用repair补齐、让assistant缺失exact revision |
| context snapshot failure | snapshot transaction前故障 | 继续用上一eligible snapshot或完整transcript；已有reply不受阻；若token不可行则新dispatch typed infeasible；transcript/epoch不变 | half snapshot成为authority、删除source entries、生成失败回滚旧reply |
| context snapshot commit ack丢失 | transaction后断连接 | 按snapshot id/source range/contract查询唯一winner；后续safe-point另行安装binding revision；transcript/epoch不变 | duplicate generation/repair owner、把snapshot commit等同pointer advance、推进retention |
| referenced context snapshot GC | binding revision已引用snapshot后运行GC | snapshot与blob保持；删除被FK/RESTRICT拒绝；context rematerialization exact | 删除后重新生成不同summary、静默回退完整transcript |
| orphan blob GC race | prewrite blob接近24小时grace时canonical row尝试安装FK | 要么canonical transaction先锁定/引用成功，要么GC先删除且canonical mutation安全失败 | canonical row引用missing blob、per-domain hold/receipt |
| TUI snapshot concurrent commit | snapshot metadata/control/rows各SQL之间commit sequence 10 | response要么完整cut到9，要么完整cut到10 | latest=10/suffix到9、control与rows跨revision |
| canonical control-only notification丢失 | queue cancel/claim、session closing、turn control或tool attempt insert commit但不append entry；丢notify | control revision推进；Observe pair检测revision落后并返回snapshot-required；fresh snapshot在同一cut显示exact attempt/result/turn-derived state | observed sequence相等就继续等待、永久显示not-dispatched |
| TUI page concurrent commit | 以epoch E/cut 9翻页时commit 10 | page只含E且sequence≤9；新cycle再见10 | 新row混入旧cut、root/receipt repair |
| TUI reconnect | kill UI、Runtime继续并丢transcript/control notification | Protocol v3从一致snapshot/page重建；sequence/revision pair发现任一推进；无状态反写Runtime | UI ack成为semantic gate、永久漏掉已commit entry/control |
| pending interaction same-Host reconnect | approval/plan/MCP request存活时kill TUI连接 | live-control query重新返回同一live interaction id；canonical snapshot不含request | 为重连写durable request/receipt |
| pending interaction Host crash/takeover | request显示后kill Host并由新Host acquire writer | turn interrupted、request消失、旧resolution被拒绝；accepted decision若已commit仍可query | 恢复suspended request/coroutine、旧generation接受resolution |
| TUI retention GAP | cursor早于retained_from或client-ahead | 丢弃sequence cache，fresh snapshot，再按before_sequence分页 | root repair、Runtime latch、把GAP当canonical corruption |
| subagent foreground crash | child中途kill | task interrupted；已有messages/results保留 | 恢复child coroutine |
| retry-safe background subagent | pure/idempotent job attempt lease后kill | 旧attempt保留；新attempt引用retry_of并可执行；Host generation变化不影响current claim；job accepted result唯一 | 覆盖旧attempt、同时两个active attempt commit、检查writer generation |
| non-idempotent background subagent | attempt可能产生effect后kill/lease loss | attempt与job aggregate outcome_unknown；自动invoke count不增加；显式新attempt留retry_of | 自动回pending、覆盖旧attempt或从头重跑 |
| prompt queue restart | item pending/claimed时kill | pending或过期claim可恢复，顺序稳定 | checkpoint缺失阻断queue |
| memory index failure | vector/index write失败 | canonical memory fact仍accepted；index标stale可重建 | memory acceptance回滚或Runtime latch |

### 9.6 Side-effect安全验收

必须有跨进程黑盒测试，而不只测试owner状态：

1. fake external service记录真实invoke count；
2. 在完整assistant tool-request message、每个tool execution attempt、remote receive/response与每个result commit边界kill；
3. 对每个crash样本执行conversation rehydrate + effect reconciliation；
4. 断言Runtime自动invoke count增量始终为0；
5. 显式retry后才允许增量1，并在新turn中产生new call id、该call的唯一new attempt id与cross-call retry_of_attempt_id；旧call下的attempt数仍为1；
6. 支持idempotency key的tool验证remote dedupe；
7. 不支持的tool必须显示unknown，不得用本地receipt伪造remote certainty；
8. 对durable job重复同一实验：retry-safe创建新attempt并保留旧attempt，remote-queryable只query原remote identity，non-idempotent自动invoke增量必须为0；
9. stale Host writer与stale job-attempt claim都必须在各自SQL conditional mutation处失败，而不是先写入再由repair撤销；
10. Host takeover发生在job执行中时，current attempt claim result仍可commit；worker不能追加transcript，只有current Host显式accept后conversation才变化。
11. mixed message包含至少3个calls并并行乱序完成；断言message transaction rollback时所有call invoke count为0，commit后每个invoke均有先行attempt且result与parent/call/attempt exact pairing；call无attempt与attempt无result得到不同状态；
12. 在任一call terminal前尝试follow-up model必须被拒绝；全部terminal后provider input按原call ordinal构造，与physical finish order无关。
13. 旧attempt进入outcome_unknown后创建新turn/new call retry，再让旧remote outcome晚到；断言两个call各自最多一个attempt/一个result，旧turn不被改成completed；对每条历史assistant用其`provider_input_through_sequence`判断该result是否late，只有明确晚于cut时才在future lowering成为typed late-effect observation，历史assistant revision/cut attribution不变。

### 9.7 Definition of Done

架构减法完成必须同时满足；这是最终completion definition，9.1的数字预算不能替代以下任何一项：

- text-only、one-tool与mixed/multi-tool loop均走direct canonical schema，每个accepted product fact只有一个canonical commit owner；
- crash、reply、tool side effect、conversation rehydrate、close语义与7.2完全一致；
- Pulsara独有能力的最小durable boundary通过restart测试；
- 旧event/reducer/checkpoint/repair/close owner物理删除，不只是unused；
- 对应表、event、tests删除；
- 没有双写、compat reducer、新stable receipt subsystem；
- 9.2全部correctness gate、9.4全部hard guardrail和9.5行为矩阵通过；
- 9.1预算逐项报告；任何偏离有architecture review结论，不能通过schema/JSON/生成代码取巧；
- fresh/reset database端到端通过；
- Protocol v3 Python/Go client同时cutover，v2 Presentation Foundation不在production import graph；
- Protocol v3 canonical snapshot/page通过repeatable-read并发commit test；sequence+control revision Observe至少通过queue mutation、tool attempt insert/public remote-identity update与turn interruption的lost-notification test；fresh snapshot在同一MVCC cut读取call/attempt/result/turn并派生public state；user acceptance ACK unknown通过canonical-row command query/idempotency test；
- TUI只用canonical conversation snapshot/page、public control high-water与独立operational stream完成fresh attach、GAP与reconnect重建；没有presentation root、UI ACK或derived delivery state反向成为Runtime authority；
- pending interaction只通过same-Host live query恢复可见性；Host crash/takeover后request消失、turn interrupted、stale resolution拒绝，production schema/import graph没有`interaction_requests`或request recovery owner；
- mixed text + 全部calls原子commit；每个physical invoke有先行attempt；parallel result exact attempt pairing、partial crash、all-terminal follow-up barrier与ordinal lowering通过跨进程测试；
- foreground每logical call最多一个physical attempt；显式retry只通过新turn/new call表达；late exact outcome不覆盖既有result、不改写旧turn或历史provider attribution，并通过cross-call lineage测试；
- single Host writer与job-attempt claim两个独立fencing domain、job aggregate/attempt lineage与三类safety规则通过跨进程故障注入；
- Stage 2 activation已包含minimal job schema/claim/result-accept与全部foreground-reachable handlers；未迁移capability在catalog/admission不可达；Stage 4只删除旧graph且不发生第二次job authority cut；
- initial context revision与user/turn原子安装；mid-turn只在provider safe point新增revision并推进pointer；每次provider dispatch从固定MVCC cut建立唯一prepared-input handle，每条accepted provider-generated assistant原样保存exact revision与`provider_input_through_sequence`；100→101→102 late-result race和并发commit-order sequence测试通过；compaction不修改transcript/epoch，unreferenced snapshot可GC，被revision引用的snapshot exact保留并通过current-turn-delta rematerialization；
- global blob publication/FK/RESTRICT/24小时orphan GC通过missing-blob与late-install竞态测试，production没有per-domain hold/receipt owner；
- conversation rehydrate、context rematerialization、effect reconciliation、audit reproduction的API与测试语义分离，execution replay明确不存在；
- Stage 1保留的physical quiesce test证明DB pool/artifact store释放后没有旧owner task继续访问；Stage 3删owner后对应close await才归零；
- checkpoint、audit、UI故障不会阻断任何foreground canonical path；
- 文档中标记为target delete的旧owner、表、event与contract test要么已物理删除，要么阶段gate明确未完成，不能宣布架构cut完成；
- 删除不得通过给旧owner换名、覆盖旧attempt、把独立authority塞进巨型JSON或再包一层generic receipt来宣称完成；inventory、import/AST gate与行为测试必须共同证明旧恢复图确实消失。

---

## 10. Open questions

以下问题不能仅由当前代码回答，必须由产品/运营作决定；其余技术事实不再作为“开放问题”。V1 single Host writer、数据库generation/lease fencing和多observer/单controller mutation path已经在决策14冻结，不在本节重新开放。

### 10.1 是否对每次 model call承诺 exact context-input audit

默认建议：**不承诺**。采用显式debug session、低比例采样和短TTL。

如果合规或可解释性要求逐次exact audit，必须明确：

- retention期限；
- 最大session/storage预算；
- 哪些敏感输入可保存；
- close时是否允许放弃本次audit的业务成功承诺；即使允许，physical operation仍须在session资源释放前退出或被隔离；
- 用户是否能导出/删除。

即使答案为“必须保留”，它仍是独立后台/审计plane，不能进入model admission、reply commit或Host close hard gate。

### 10.2 哪些tool属于高后果side effect，以及unknown UX

需要产品确认：

- 写文件、shell、网络、邮件、支付、云资源等风险分级；
- outcome_unknown时默认按钮和文案；
- 哪些tool允许read-only inspection；
- 哪些tool支持remote idempotency/status lookup；
- 显式retry由用户、模型还是policy批准。

默认建议：未声明即有side effect；unknown不自动重试。

### 10.3 live partial assistant text是否有产品保存承诺

推荐默认：stream只为live体验，crash后允许消失。

如果产品承诺“用户看到的每个字都可恢复”，那会重新引入partial transcript语义，但仍不应恢复transport cursor；需要把partial message明确显示为 interrupted draft，且不得作为accepted assistant输入下个turn。这个承诺会显著提高I/O与隐私成本。

### 10.4 subagent是否必须在Host退出后继续

需按模式决定：

- foreground delegation：Host结束即interrupted；
- explicitly backgrounded delegation：durable job，可由新worker开始新attempt；
- 不允许默认把每个child升级为durable execution。

默认建议：只有用户明确要求后台化的subagent才跨Host。

### 10.5 terminal process能否跨Host可靠重新绑定

这取决于terminal supervisor和操作系统process托管产品承诺：

- 若能通过稳定launch token重新绑定，terminal_monitor job可继续观察；
- 若不能，restart后必须显示process outcome_unknown/interrupted；
- 不应由Runtime内部owner推断进程仍活着。

### 10.6 reset-only是否适用于现有真实数据

技术推荐是reset-only；产品需确认是否已有必须保留的用户session/memory。

若必须保留，只做一次离线、可审计export/import：

- accepted facts导入；
- partial/running/conflict统一interrupted；
- 原始EventLog冷归档；
- 不做online dual-write或compat replay。

### 10.7 memory governance哪些事实必须同步可见

需决定：

- 用户显式save/delete是否必须与回复同transaction可见；
- 自动extraction可延迟多久；
- search index允许多旧；
- supersede/conflict如何展示。

默认建议：用户显式memory mutation是canonical transaction；自动extraction/index是durable background job或可重建derived work，不阻塞reply。

### 10.8 canonical transcript未来是否需要retention

这是真正的产品/合规决策，但**不阻塞V1**：V1默认保留全部canonical transcript，compaction不承担retention。

未来若需要删除旧history，必须单独定义：

- 用户可见retention期限与删除提示；
- export、legal hold、memory/side-effect audit保留边界；
- 一次retention transaction如何原子推进`retained_from_sequence`与`transcript_epoch`；
- TUI cursor GAP与context source range如何解释已删除区间；
- background job/result引用旧entry时的清理策略；
- 被binding revision引用的context snapshot及其blob必须保留多久，若未来删除source transcript是否仍需保留summary/source commitment与revision attribution。

在这些产品语义冻结前，不允许以compaction、checkpoint GC或存储压力为由隐式删除/重写transcript。

---

## 最终结论

37e21903证明 Pulsara团队已经意识到“诊断材料不能占据semantic event主路径”：compact commit、bounded carrier、best-effort audit和degraded loader都是正确方向。但当前实现随后又让每次model call默认写plan/pages/root、永久保留，并让optional audit参与close；同时又新增non-Host teardown retry lineage。因此它是局部authority减法、全局physical ownership加法。

Pulsara当前事故不是一组互不相关的边角bug，而是同一选择的重复后果：**把foreground execution、derived projection和observer delivery都纳入跨进程exact continuation。**

最合适的目标不是继续完善这套恢复图，也不是删除Pulsara的long-horizon、subagent、terminal monitor和memory能力，而是把边界重新冻结为：

> PostgreSQL只保存canonical relational conversation facts、最小control high-water、tool/job physical attempt journals、revision-referenced semantic context snapshots、global blob references、coarse interruption和真正后台job；
>
> model/tool foreground execution留在进程内；
>
> crash就是一次明确interruption；
>
> side effect未知就明确显示unknown且不自动重试；
>
> checkpoint、audit和UI永不成为semantic gate。
