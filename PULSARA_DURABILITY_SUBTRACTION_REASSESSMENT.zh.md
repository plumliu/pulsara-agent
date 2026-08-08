# Pulsara durability 边界减法：增量复审与目标架构结论

> 文档性质：只读架构调研、冻结决策与 hard-cut 阶段边界；不是逐文件编码 implementation spec
>
> 复审日期：2026-08-07
>
> Pulsara 当前代码基线：f752a04439cf18961899ab6345929a59d0d80082
>
> 上轮调研基线：0e40febd
>
> 增量提交：37e21903（refactor: hard-cut context input audit manifests）
>
> Claude Code 对照基线：5a774a2b62d7949c1d94e0b726281554d7893cfd
>
> Codex 对照基线：6138909d6ec58b2fbe635ef973e02caecad5a5aa
>
> Grok-build extension/policy 对照基线：c68e39f60462f28d9be5e683d9cbe2c57b1a5027
>
> Claude Code / Codex 官方资料访问日期：2026-08-08
>
> 本轮 AgentEvent 复审：冻结“`AgentEvent` 是 Runtime 的 typed extension protocol，而不是 Runtime execution recovery state machine”；保留 selective committed journal，并把未coalesce的provider Text/Thinking/Data/ToolCall与ToolResult Start/Delta/End 重塑为独立的 process-local live protocol。
>
> 本次路线修订：冻结 V1 single Host writer；合并 foreground text/tool/resume/readers authority cut；将 TUI 提升为 Protocol major hard cut；补齐 durable job side-effect safety。
>
> 反向审阅补订：分离 writer/claim fencing domain；冻结 Protocol v3 repeatable-read cut与canonical-row mutation idempotency；区分semantic de-gating与physical quiesce；禁止compaction删除、重写或重排canonical transcript。
>
> 最终反向审阅补订：非transcript canonical transition由同transaction的selective committed event与`latest_event_sequence`提供level-triggered观察；冻结pending interaction为V1 process-local live control；冻结mixed/multi-tool assistant message的原子commit与ordinal lowering。
>
> 终局架构补订：将目标重命名为Canonical relational conversation kernel with selective domain, effect, and work journals；区分semantic context snapshot与rebuildable projection；新增tool/job physical attempt lineage、全局blob publication、interaction subject/secret boundary、四类恢复承诺与Stage 2多PR dormant construction/单次production activation。
>
> 最终主线补订：context采用改为turn-local immutable binding revisions以保留mid-turn compaction；每条accepted provider-generated assistant entry额外归因exact `provider_input_through_sequence`；foreground每logical call最多一physical attempt，retry必须为新turn/new call；minimal job kernel前移到Stage 2单次activation；tool attempt insert与对应committed occurrence纳入同一canonical MVCC snapshot。
>
> 最终存储与stream减法补订：completed assistant semantic blocks继续作为canonical message内容；durable `ModelStreamSegment`、coalescing persistence与stream recovery全部删除，但normalized且未coalesce的Text/Thinking/Data/ToolCall与ToolResult Start/Delta/End保留为process-local typed `LiveAgentEvent`。canonical memory graph、FTS、pgvector与现有bounded两跳relation recall全部收敛到PostgreSQL，Oxigraph及其required config、surface delivery、worker、Inspector/analytics adapter与SPARQL contract从Pulsara生产和仓库边界完整删除，不新增替代性的通用graph-query DSL。
>
> 第二轮AgentEvent反向审阅补订：stored committed occurrence与TUI observation DTO物理分离；append authority封闭为Host writer或exact job-attempt claim；pending interaction增加process-local owner epoch/revision与atomic snapshot-subscribe；ordinary post-commit hook明确无catch-up；committed subject改为数据库约束的typed nullable-FK union。以上均不增加durable projection、第三类writer或generic hook receipt graph。
>
> Blob-backed transcript补订：Protocol v3 entry content冻结为`InlineContent | CanonicalBlobReference`；大型正文只通过无状态、bounded、逐请求重新鉴权的`ReadCanonicalContent`读取。reference不是bearer capability；读取沿canonical entry/block FK验证session/workspace、capability、digest/size/codec，不增加download receipt、lease、cursor、projection或repair owner。
>
> Extension治理补订：V1 extension只能订阅exact 49类core AgentEvent projection，不能定义或发布新的Committed/Live AgentEvent；普通hook无catch-up且不能以`reliable=true`升级，V1第三方durable extension action为0。唯一pre-dispatch policy kind冻结为`ToolDispatchAuthorizationPolicy`，只返回Allow/Deny/RequireConfirmation，不重写已canonical commit的tool arguments。
>
> 调研约束：除修订本文档外，没有修改代码、测试、README、schema 或 migration；没有 stage、commit、push；没有运行全量 pytest。

## 证据标记

- **[代码确认]**：直接来自当前 HEAD 的生产代码。
- **[官方文档确认]**：来自产品当前官方文档；外部资料均记录版本/commit或访问日期。
- **[探针测量]**：通过仓库根目录 .venv 运行的小型只读路径探针；临时 workspace 位于系统临时目录。
- **[定向测试]**：只运行与本次增量直接相关的 6 个测试，不代表全量回归。
- **[历史意图]**：来自事故文档、架构债务文档、实施计划或 git history；只用于解释动机，不替代代码事实。
- **[合理推断]**：代码或官方资料没有直接声明产品语义，但可由调用关系或持久化接口推导；均显式标注。
- **[设计建议]**：本文冻结的 Pulsara 目标边界，不冒充当前能力或竞品事实。
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

当前f752a044代码真值没有推翻这项判断：151类universal durable vocabulary、11/23次text/tool EventLog transaction、execution replay、publisher/reducer反向失败路径仍在；同时，raw provider与TUI observation已经提供了可以重塑为独立typed live plane的代码先例。

本轮对事件系统作一项重要纠偏：**应删除的是 universal durable execution ledger，而不是 typed event protocol。** 当前 151 类 `AgentEvent` 被一个 base、一个 serializer registry、一个 sequence 和一套 confirmation/recovery 语义强行放在同一 durable 平面；这正是过度设计。与此同时，typed lifecycle 已经是 Inspector、TUI、审计、eval 与 hook 的产品接口，happy path 把它压缩到零会丢失真实能力。目标必须同时成立：

1. `CommittedAgentEvent` 只保存“某项用户可观察 transition 在 `event_sequence=N` 被接受”的 occurrence/audit truth，并与对应 canonical row 由同一 owner 在同一 PostgreSQL transaction 写入；
2. `LiveAgentEvent` 保存本 generation 经adapter-local解码与sanitizer/normalizer处理、但未经coalescing的Text/Thinking/ToolCall与ToolResult Start/Delta/End，以及本Host epoch的session Interaction Opened/Replaced/Closed，只走独立 process-local bus/snapshot；Data、terminal monitor与subagent progress是具名live extension；
3. `OperationalEvent` 保存 TTFT、retry、buffer/backpressure、cache 与诊断，默认不落 durable journal；
4. reservation/account/candidate/receipt/projection-ready/checkpoint/reducer-repair/delivery-ACK 等 machinery/proof event 若无独立产品语义则物理删除。

换言之，`AgentEvent` 的正式优势是 **typed extension protocol**，而不是 Runtime execution recovery state machine。

本文后续所称“raw provider delta”专指上述**未coalesce的semantic `LiveAgentEvent` delta**，不指供应商SDK wire object，也不意味着目标架构保留独立`RawProvider*`协议。

### 1.2 当前量化快照

| 指标 | 上轮基线 0e40febd | 当前 f752a044 | 判断 |
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
   ModelStart、durable stream segments、terminal projection、control disposition、ReplyEnd、RunEnd 各自拥有 durable fact、确认和恢复分支，即使产品完全可以把 crash 解释成 interruption。问题是 durability/recovery 归属，不是 Start/Delta/End 这种 process-local typed vocabulary 本身。

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
   删除 durable ModelStart/ModelEnd/terminal projection/control disposition/recovered ReplyEnd，以及`ModelStreamSegmentAccumulator`、coalescing persistence、stream policy和durable Text/Thinking/Data/Tool Segment event；crash 后只保留canonical interrupted turn。normalized且未coalesce的Text/Thinking/Data/ToolCall与ToolResult Start/Delta/End改为`LiveAgentEvent`，进入bounded process-local assembler/bus；它们有类型、有顺序、有hook价值，但没有跨进程continuation语义。

2. **可重建 projection 的 semantic gate、checkpoint repair 与 committed-reducer repair 链**
   checkpoint 失败只影响 reopen 性能，不能阻止 final reply 或 RunEnd；先移除读取方和 gate，再删除 owner。Oxigraph异步RDF mirror也属于这一类：memory truth、relation recall与治理已经由PostgreSQL承载，目标架构不再为镜像维护surface delivery或freshness gap。

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
- Host存活期间已经进入canonical tool-result/entry的terminal completion/termination事实；terminal process与monitor本身只保留process-local typed lifecycle，不另设terminal-specific committed event，也不保留跨Host rebind authority；
- 已被turn-local immutable binding revision引用的versioned long-horizon context snapshot，以及memory extraction job；
- durable job intent、每次physical attempt、remote identity与retry lineage；
- subagent 已接受的task/objective/parent-child、message、result与terminal status，而不是其 coroutine/executor、claim、attempt或跨Host continuation状态；
- memory governance 中有长期产品价值的用户/模型事实；
- PostgreSQL中的accepted memory facts、asserted relations以及支撑现有bounded两跳召回所需的FTS/pgvector read models；不保留第二份Oxigraph RDF truth或mirror；
- 必须跨设备、跨进程恢复的 session metadata；
- 由一个全局content-addressed blob contract发布、被canonical row以外键引用的大内容。
- selective committed `agent_events`：只保存已接受的用户可观察 occurrence，例如 message accepted、turn interrupted/completed、tool/job/memory/coordination lifecycle；它以exactly-one typed FK引用canonical subject，是审计与增量观察真值，不是 canonical row 的存在证明，也不用于 execution replay。

### 1.6 唯一推荐

**继续推荐“中等 hard cut”，终局命名冻结为：Canonical relational conversation kernel with selective domain, effect, and work journals。**

这不是Event Sourcing，也不是把所有事实压进一条transcript。它使用直接关系型schema保存conversation facts，用selective committed `agent_events`保存产品occurrence，用窄attempt journal保存真实physical effect lineage；execution coroutine、consumer proof与derived delivery不再进入durable truth。物理边界固定为canonical relational rows、selective committed `agent_events`、tool/job physical attempt journals、shared content-addressed blobs及其closed stateless canonical read port、process-local live `AgentEvent` stream，以及disposable derived indexes/presentation/telemetry。

它不是功能最少的方案，而是复杂度/产品价值比最好的方案：

- 保留 Pulsara 的 long-horizon、subagent、same-Host terminal monitor、durable prompt queue、memory governance 和 resumable Host session；
- subagent execution与child `RuntimeSession`绑定当前Host：completed/failed/cancelled保持terminal，Host结束时所有未terminal task都成为interrupted；reattach只读已接受事实，不恢复或自动重新委派child；
- 不再承诺 foreground execution state 的跨进程 exact continuation；
- 对外部 side effect 使用tool-call intent、dispatch-before committed attempt、result-after-return三段事实；只有attempt存在而result缺失才是outcome_unknown，call存在但attempt不存在可证明未dispatch；
- final assistant reply 只有一个数据库 commit point；
- open执行conversation rehydrate，再开启新turn；不做execution replay；
- V1 冻结为每个 session 同时只有一个 Host writer；PostgreSQL writer generation/lease只fence Host-owned foreground与session-control mutation，旧 generation一律fail closed；
- background worker完全独立于Host writer generation，只以job attempt的`claim_generation`提交progress/result；它不得直接追加session transcript，当前Host需要用自己的writer generation显式接受job result；
- 第一次生产 authority 切换必须同时覆盖 user、assistant、tool call/attempt/result、context binding revisions、interrupted/unknown、最小 open/resume、TUI/Inspector/context 读取方与minimal job kernel；不得按“模型最后是否调用 tool”拆成两个阶段，也不得让foreground-reachable background work留在旧authority；
- TUI 同步执行 Protocol major hard cut，从 Presentation Foundation 的 root/cursor/page 权威切到canonical snapshot + bounded committed observation + current provider/tool-result/session-control live owners；snapshot与`event_sequence_cut`来自同一MVCC read cut，Gateway把`event_sequence > cut`的stored occurrence与exact subject在一个bounded read transaction中组合成wire projection；Go不直接解释stored payload/subject id，GAP触发对应snapshot/rebuild，不保留在线兼容层、durable projection或通用receipt graph；
- snapshot/history/observation中的entry正文统一使用`ObservationContent = InlineContent | CanonicalBlobReference`；Go遇到reference只通过Gateway `ReadCanonicalContent`按byte range读取，reference本身不授权访问，每个chunk重新校验canonical content edge、session/workspace、principal capability与digest/size/codec；
- `sessions.latest_event_sequence`作为用户可观察canonical transition的level-triggered high-water；包括tool attempt insert与public remote-identity publication在内的transition，由持有`HostWriterGuard | JobAttemptClaimGuard`之一的canonical owner在原transaction内追加带closed typed subject FK的selective committed event并推进high-water；两类owner共用session allocator lock order，普通hook/plugin无append authority；不保留独立`control_revision`、transition history或per-section cursor；
- V1 pending approval/plan/MCP input request是同一Host内可level-read的process-local live control；`owner_epoch/live_revision`与atomic snapshot-subscribe连接snapshot及Opened/Replaced/Closed，不进入canonical snapshot或durable registry；Host crash/takeover后新epoch为空、turn interrupted，只有accepted decision进入`interaction_decisions`；
- ordinary post-commit hook只从registration cut后best-effort接收typed/redacted projection，overflow只GAP/detach且不自动journal catch-up；V1没有generic durable extension action或`reliable=true`注册开关，跨进程必达需求必须在未来以独立ADR新增具名job type；
- 经session/workspace鉴权的当前用户使用独立的first-party live projection：Runtime实际收到的raw thinking delta原样可见；tool arguments在展示阈值内完整可见，超限使用显式truncated DTO。这不给ordinary hook/plugin继承用户视图权限，也不承诺晚attach、GAP或crash后的thinking必达/重放；
- 一个completed assistant tool-request message的text与全部calls原子commit后才允许任何invoke；每logical call最多一foreground physical attempt，retry必须在新turn中生成new call；results可并行、分别commit，但follow-up model必须等全部call terminal，并按原call ordinal lowering；
- durable job拆成aggregate job与immutable attempt lineage；lease过期不等于可以重做：只有显式 retry-safe handler能创建下一attempt自动重执行；可查询远端状态的handler只能重新观察；非幂等handler丢失lease后当前attempt进入outcome_unknown；
- compaction在V1只新增immutable context snapshot/binding revision，不删除、重写或重排transcript；turn可在provider safe point换用新revision以保留mid-turn budget recovery，每条accepted assistant message引用exact revision并保存本次pre-dispatch conversation cut的`provider_input_through_sequence`；未采用snapshot可按retention删除，被revision采用后不能重新生成冒充；
- 所有大内容通过唯一blob publication contract发布；prompt、tool result、job、context snapshot与memory不各自维护hold/receipt/confirmation图；Protocol-facing transcript另有唯一closed、stateless canonical content read port，不退化为任意blob下载器；
- provider stream只在进程内由typed bounded assembler拼接；Start对象不可被后续delta原地修改，每个Delta只更新一个active assembler，End携带最终frozen block；只有completed assistant message及其ordered semantic blocks可以在provider completion后原子进入canonical transaction，transport delta、coalescing segment、seal reason与stream attribution fingerprint均不落库；
- PostgreSQL独占canonical memory graph与Agent-facing memory reads；保留当前typed lexical/FTS/vector/direct-edge/bounded两跳recall，不扩展raw SPARQL或通用graph DSL；Oxigraph、`oxigraph_url`、Oxigraph surface delivery/worker与可选adapter全部退出目标仓库；
- yielded terminal process严格绑定当前Host lease：orderly detach/close主动终止owned process group并有界drain；Host crash/takeover不按`process_id`、PID或event replay重新绑定，旧handle失效且未确认outcome只显示interrupted/unknown；
- close最终压缩为3个阶段；在旧Foundation/owner仍存在的过渡阶段，只删除semantic completion wait，仍需bounded physical cancel/join后才能释放session-owned资源；
- PostgreSQL canonical rows仍是conversation、tool、job、memory与coordination semantic truth；selective committed `agent_events`只拥有occurrence/audit truth。event不能证明canonical row已经真实，reopen也不通过event replay恢复execution。

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
| compaction与transcript retention混淆 | compaction只追加context snapshot；canonical transcript在session存续期append-only且无prefix retention | compaction删除/重写canonical transcript |
| 非transcript canonical control变化不会推进entry sequence | 同transaction追加selective committed event；Observe按`event_sequence`消费suffix或GAP后fresh snapshot | 独立control revision、per-section cursor/history或只靠可丢edge hint |
| pending interaction既未持久化又被称为canonical snapshot state | V1 request是process-local live control；只持久化accepted decision | 暗中恢复suspended interaction owner |
| multi-tool message仍按单call durable unit描述 | mixed text + 全部calls作为一个assistant message原子commit；result按call精确配对 | 为守住固定4次transaction逐call先写先执行 |
| compaction被同时称为durable truth与可删除cache | 未被binding revision引用的snapshot可GC；已引用的summary/source/compiler/model contract为immutable semantic artifact | 删除后重新生成不同summary并冒充连续性 |
| 单turn context binding会删除mid-turn compaction | turn-local immutable binding revisions；只在safe point推进current pointer；assistant output绑定exact revision + per-call conversation cut | 同turn永久锁定初始snapshot或恢复ModelStart lifecycle |
| tool call与physical effect混成一条事实 | assistant call表达intent；`tool_execution_attempts`在dispatch前commit；result引用exact attempt | call存在就推断已dispatch，或无result一律unknown |
| 同call多attempt与唯一tool result冲突 | foreground每call最多一attempt；retry是new turn/new call，attempt状态由row/result/turn派生 | 覆盖旧attempt、丢弃physical outcome或新增per-attempt observation graph |
| job row覆盖多次真实执行 | `durable_jobs`保存intent/aggregate；`durable_job_attempts`保存claim、remote identity、result与retry lineage | mutable attempt summary或JSON覆盖旧effect lineage |
| Stage 2与Stage 4之间的job authority空窗 | minimal job schema/claim/result-accept与foreground-reachable handlers在Stage 2激活；Stage 4只收口剩余disabled handlers并删旧graph | 旧job到新conversation bridge或默认丢失background能力 |
| tool attempt不推进entry high-water | attempt insert在同transaction追加`ToolAttemptAccepted`并推进`latest_event_sequence`；snapshot在同一MVCC cut读取attempt/result/turn与event cut | 只靠可丢notification、独立control cursor或永久显示not-dispatched |
| binding revision不能证明某次model call看到了哪个delta cut | 每条provider-generated assistant保存exact revision + `provider_input_through_sequence`；entry sequence按commit顺序分配且不可预留 | 用assistant自身sequence或共享revision推断result是否参与历史input |
| 每个domain各造artifact hold/proof | 全局content-addressed blob publication + canonical FK + orphan grace GC | queue/tool/job/context各自复制preparation owner |
| “replay”混合四种不同承诺 | conversation rehydrate、context rematerialization、effect reconciliation、audit reproduction分别冻结；execution replay不支持 | 用历史decoder暗示coroutine可恢复 |
| 删除delta durability后仍保留segment abstraction | 删除`ModelStreamSegmentAccumulator`、segment policy/fingerprint与全部durable stream segment event；仅保留process-local bounded assembler | 给segment换名后继续产生event candidate、terminal projection或replay identity |
| PostgreSQL truth与Oxigraph mirror存在freshness gap | PostgreSQL直接拥有memory facts/relations、FTS、pgvector与现有bounded两跳recall；Oxigraph整体退役 | 让Agent查stale SPARQL后再rebind、保留optional adapter或改成Oxigraph authority |
| blob-backed entry只返回reference但没有可执行read contract | `ObservationContent` closed union + stateless bounded `ReadCanonicalContent`；沿canonical subject/content slot授权并校验完整content与chunk digest | 返回raw blob id/private URL、让Go直接读storage、或增加download lease/receipt/projection/repair owner |

#### 1.6.1 AgentEvent反向审阅：六条finding的取舍

前五条finding已经闭环；追加的blob-backed transcript finding同样成立，但不能把修复写成新的durable projection、第三种模糊writer、普通hook receipt graph或download authority。最终取舍冻结如下：

| finding | 判断 | 采纳后的最小边界 | 明确不采纳的增肥方向 |
|---|---|---|---|
| committed suffix只有event/subject，Go无法渲染完整新entry | **成立，P1** | 分开`StoredCommittedEvent`与read-time `CommittedObservationProjection`；Gateway在一个bounded repeatable-read cut中读取event及canonical subject，Go只消费wire projection | 不新建projection table、root、checkpoint、materializer或event内完整复制message/tool result |
| background worker没有event-sequence append authority | **成立，P1** | `EventAppendGuard = HostWriterGuard | JobAttemptClaimGuard`；统一SQL allocator先锁session event head，再校验domain guard、写canonical subject/event并推进high-water | finding建议中的第三个泛化memory/governance guard不进入V1；foreground只接受现有`remember_*` proposal进入durable candidate pool，自动extraction与governance走durable job；普通hook/plugin永远不能append committed event |
| pending interaction没有typed live revision/snapshot-subscribe线性化点 | **成立，P1** | 在`LiveAgentEventBase`下增加`session.control` namespace、`SessionLiveControlSnapshot(owner_epoch,live_revision,current_interaction)`与Opened/Replaced/Closed event；atomic snapshot-and-subscribe | 不持久化request、owner epoch、live revision或event；不借它恢复旧Host coroutine |
| ordinary post-commit hook被暗示可自动catch up | **成立，P2** | ordinary hook只从process-local `registration_cut`之后best-effort接收；queue bounded，overflow只GAP/detach；TUI/audit query与durable job是另外的显式consumer | 不给普通hook durable cursor、restart replay、automatic suffix backfill或generic receipt |
| committed event subject只靠自由kind/id | **成立，P2，但必须在本文选型** | V1选择typed nullable-FK union：每行exactly one subject slot，DEFERRABLE FK + event-type/slot CHECK + `ON DELETE RESTRICT`；新增subject variant/slot必须migration | 不增加统一`canonical_subjects` identity/proof表，不允许自由字符串或仅应用层校验 |
| blob-backed transcript没有可执行读取边界 | **成立，P1** | `ObservationContent = InlineContent | CanonicalBlobReference`；Gateway提供bounded byte-range `ReadCanonicalContent`，逐请求验证canonical edge、scope/capability及digest/size/codec，Go校验chunk与完整content digest | reference不是bearer capability；不暴露任意blob read/private URL，不增加durable receipt、lease、cursor、projection、repair或content-delivery event |

这些修订仍遵守核心准则：stored event只保存occurrence/audit truth；observation projection是无持久状态的bounded read DTO；append guard只授权“谁能与canonical mutation同transaction写event”，不保存delivery/recovery state；live-control revision只在当前Host进程内线性化observer；普通hook仍不是可靠消息系统；blob读取只是canonical query的分页hydrate，不是event delivery或新authority。

最终反向审阅通过收紧transaction、read cut、fencing、live-control、physical attempt、semantic context、per-call provider conversation cut、job activation、blob、stream与memory physical store边界闭环，没有新增stable candidate、receipt、checkpoint、repair owner或兼容projection。`latest_event_sequence`只排序同transaction已接受的selective occurrence，不保存consumer position，也不成为resume owner；新增attempt row保存的是physical effect这一不可替代的产品事实，不是executor transition graph；`provider_input_through_sequence`只是accepted assistant row上的标量归因，不恢复ModelStart/ModelEnd或provider lifecycle journal；process-local assembler和PostgreSQL两跳recall也都不形成新的durable delivery plane。

---

### 1.7 调研范围、增量与验证方法

#### 1.7.1 仓库状态

**Pulsara**

- 本文原始增量审阅对象：37e21903，相对0e40febd的diff为77 files、+9,900/-6,435；`src/pulsara_agent`净增1,463行。
- 本轮代码真值复核HEAD：f752a04439cf18961899ab6345929a59d0d80082；151类EventType、43/83 event路径与主要owner结论均在该HEAD重新核对。
- 本轮开始时本文档已经是用户dirty modification；修订只叠加在该文件上，没有覆盖/撤销用户改动，也没有触碰生产代码、测试、README或migration。

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

在推荐的 crash = interruption 语义下，全部V1 child executor都不需要跨进程精确terminalization；process-local close只需bounded cancel/join，未terminal child收口为interrupted。

#### 1.8.3 对新提交的最终处置建议

**保留：**

- 不再恢复 flat manifest；
- ContextCompiled 的 compact bound；
- semantic authority 与 optional diagnostic 的分离；
- loader 的 exact/reconstructed/unavailable 降级模型；
- audit failure 不 latch live runtime 的规则。

**短期降级：**

- audit 从“每次 model call 自动生成”改成显式doctor、采样或session opt-in的短TTL best-effort诊断；
- completed artifact 增加明确 TTL/retention product policy；
- close到达时允许放弃audit成功/materialized语义；仍使用session资源的operation先cancel并bounded join，或先被彻底隔离资源访问后才abandon。

**中等 hard cut 最终删除：**

- ContextInputAuditExpectationFact 从 foreground semantic event 中移出；
- llm/runtime.py 的自动 offer；
- ContextInputIoService 的audit slot和业务完成型close drain dependency；过渡期physical quiesce随owner保留到owner删除；
- audit_materializer/audit_storage/audit_gc/audit_doctor 这套 2,614 行逐call durable plane；V1已经关闭逐次exact input承诺，显式debug/采样改走短TTL、best-effort disposable diagnostic artifact，不保留这套owner；
- child teardown retry/reconciliation lineage，改成 process-local bounded interruption。

**不要删除的正确部分：**

- compact commit 不能被旧 flat manifest 替回；
- 如果过渡期仍保留 EventLog/provider exact replay，ContextCompileInputCommitFact 和 ProviderInputPreparationInstallFact 可暂时保留；
- 当 resume 改成 transcript-only 后，再评估 compact compiler commit 是否还有产品价值；不能因为它当前已经实现就默认永久保留。

---

## 2. Current-state truth map

本节描述当前代码实际拥有的 reply、tool、finalization 和 reopen，不把设计文档中的目标状态当成已实现状态。

README当前把系统公开描述为三层存储：Runtime Ledger、Artifact & Evidence Store、Semantic Memory Surface；稳定event/evidence ID连接三层，Event System负责Resume、Inspect与Compaction，memory层同时写明“PostgreSQL truth + Oxigraph semantic graph”，并把trace用于evaluation/debugging/replay。[README.md](README.md#L72)、[README.md](README.md#L105)、[README.md](README.md#L117)、[README.md](README.md#L166) 这是**当前产品叙事与迁移输入**，不是目标架构必须保留的物理边界。代码真值说明当前Resume确实依赖EventLog/reducer，memory也仍有Oxigraph surface；目标则把三层重画为canonical rows + selective journals + disposable derived planes：Resume收窄为canonical conversation rehydrate，Inspect改读canonical snapshot/committed audit/live stream，Compaction读canonical transcript/context binding，memory只保留PostgreSQL facts/relations、FTS、pgvector、direct-edge与代码现有bounded最多两跳recall。README所称“multi-hop explicit search”不能被外推成任意深图查询能力。

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

#### 2.1.1 `AgentEvent` 代码真值与四类目标语义

**[代码确认]** `events.py` 当前有 151 个 `EventType` member，`AgentEvent` union也恰好有151个class；`serialization.py`从这个union自动建立schema version 11的universal historical registry。因此当前没有durable/process-local/operational三套base：凡进入`AgentEvent`的类型理论上都可被同一个PostgreSQL EventLog序列化、确认、replay和catch-up。[events.py](src/pulsara_agent/event/events.py#L298)、[serialization.py](src/pulsara_agent/event_log/serialization.py#L1)

**[代码确认]** 同时，仓库已经存在三条更窄的先例：

- `raw_provider.py`的`RawProviderStreamItem` union定义7个frozen、adapter-private、process-local raw item，明确禁止进入`AgentEvent` serializer；[raw_provider.py](src/pulsara_agent/llm/raw_provider.py#L1)
- `drafts.py`的`ProviderTransportSemanticDraft` union定义13个typed semantic draft（Text/Thinking/Data/ToolCall各Start/Delta/End，再加ProviderError），`sanitizing_transport.py`检查Start-before-Delta/End、重复Start、大小上限，并清洗认证头、cookie、URL credential/query/fragment；[drafts.py](src/pulsara_agent/llm/drafts.py#L1)、[sanitizing_transport.py](src/pulsara_agent/llm/sanitizing_transport.py#L1)
- `UiCommittedEventTap`和`UiOperationalActivityStore`已经实现bounded ring、非阻塞offer、cursor/generation/GAP、overflow detach和callback failure isolation；它比当前unbounded、串行await subscriber的`RuntimeEventPublisher`更接近目标live bus。[observation.py](src/pulsara_agent/runtime/terminal_presentation/observation.py#L1)、[publisher.py](src/pulsara_agent/runtime/publisher.py#L1)

前两项只是当前代码真值，不是目标要保留的双层transport协议。hard cut删除7类`RawProvider*` union及逐delta的`ProviderTransportSemanticDraft`/`SanitizedProviderSemanticEnvelope`中间层；vendor SDK object不得逃出adapter，adapter-local解码调用Runtime-owned sanitizer/normalizer后，只能跨transport port交付semantic `LiveAgentEvent`或typed terminal/usage结果。清洗、顺序、大小与secret检查保留为这个单一边界的构造约束，不再生成第二套raw/draft event vocabulary。

目标术语冻结如下；后文所有“保留/删除event”均按这四类解释：

| 类别 | 真值与持久性 | 典型内容 | 允许做什么 | 禁止做什么 |
|---|---|---|---|---|
| A. `CommittedAgentEvent` | selective durable `agent_events`；occurrence/audit truth | accepted message、turn terminal、tool/job/memory/coordination transition | Inspector历史查询、TUI增量、审计、eval、post-commit hook | 证明canonical row存在；恢复coroutine/provider transport；作为canonical constraint |
| B. `LiveAgentEvent` | 当前process/current generation/owner epoch；独立base、bus、bounded queue | normalized且未coalesce的Text/Thinking/Data/ToolCall与ToolResult Start/Delta/End；session Interaction Opened/Replaced/Closed | live TUI、Inspector tail、streaming hook、同进程bounded provider/control snapshot | 进入durable serializer/schema registry；跨进程replay；阻塞provider或live-control owner |
| C. `OperationalEvent` | process-local telemetry，默认不durable | TTFT、transport retry、buffer/backpressure、cache、redaction/diagnostic | metrics、debug、operational diagnostics hook | 混入conversation history或被当成产品事实 |
| D. machinery/proof | 无独立产品语义即删除 | reservation、account、execution candidate、receipt、projection ready、checkpoint、repair、delivery ACK | 极少数若表达真实physical effect则迁入attempt journal；已接受的memory proposal本身是work-intake row，不属于此类proof event | 为另一个durable row制造第二份“存在证明” |

敏感等级也冻结为：`S0`可公开投影；`S1`内部、默认redacted；`S2`必须经过closed capability/view profile（raw thinking、未redacted tool arguments、private URL等）；`S3`secret carrier，禁止进入任何event payload。经鉴权的first-party用户live view是一个server-minted、不可转授给plugin的S2 view profile，不是对ordinary hook的公开。当前`McpSecret`已经是sealed、不可Pydantic序列化/不可pickle的process-local carrier，serializer还会调用`assert_not_mcp_secret`；目标沿用这个方向，而不是把secret塞进metadata。[mcp_secret.py](src/pulsara_agent/ports/mcp_secret.py#L1)、[serialization.py](src/pulsara_agent/event_log/serialization.py#L1)

#### 2.1.2 151 类完整 inventory 与目标处置

下表由`AgentEvent` union顺序和生产代码constructor的AST/`rg` inventory生成。每个精确event type都列在某一行；“producer/transaction、payload/consumer、gate/value、目标”是该行所有member的共同结论，个别例外在文字中点明。当前列中的“durable”描述代码真值，不表示推荐保留。

| 当前family（数量） | 精确event type | producer与当前提交事务 | payload/敏感性；consumer | recovery/gate；Inspector/TUI/hook价值 | 目标处置 |
|---|---|---|---|---|---|
| run/context window/rewrite（7） | `RUN_START`、`CONTEXT_WINDOW_OPENED`、`CONTEXT_WINDOW_CLOSED`、`CONTEXT_WINDOW_COMPACTION_STARTED`、`CONTEXT_WINDOW_COMPACTION_COMPLETED`、`CONTEXT_WINDOW_COMPACTION_FAILED`、`CONTEXT_PROJECTION_REWRITE_PAGE` | `runtime/run_entry.py`、long-horizon/window owner；EventLog batch/companion transaction；当前全durable | user/model/permission/window/page lineage，S1–S2；run/context reducers、Inspector、resume | RunStart/window/account是admission与resume gate；run/turn lifecycle有高产品价值，rewrite page是machinery | canonical turn/context rows；只为accepted/interrupted/completed等用户可观察transition发A；compaction进度为C或job；rewrite page为D删除 |
| rollout budget（7） | `ROLLOUT_BUDGET_ACCOUNT_OPENED`、`ROLLOUT_BUDGET_ACCOUNT_CLOSED`、`CHILD_ROLLOUT_SUBACCOUNT_CLOSED`、`ROLLOUT_BUDGET_RESERVATION_CREATED`、`ROLLOUT_BUDGET_RESERVATION_SETTLED`、`ROLLOUT_PHASE_TRANSITIONED`、`SUBAGENT_ROLLOUT_BUDGET_RESOLVED` | run/model/subagent budget owners；独立或terminal batch；当前全durable | token/budget/reservation/fingerprint，S1；budget reducer、finalization、Inspector | account/reservation closure可gate model admission、RunEnd与close；主要是proof/计量 | current budget/usage放canonical row或C telemetry；V1不为它新增core A；reservation/account lifecycle D删除。未来若出现付费额度产品transition，必须另开architecture decision |
| session/capability/run/reply/gate（9） | `MCP_CAPABILITY_SNAPSHOT_INSTALLED`、`RUN_INTERACTION_RESUME_BOUNDARY`、`CAPABILITY_EXPOSURE_RESOLVED`、`RUN_END`、`REPLY_START`、`REPLY_END`、`RUN_ERROR`、`CONTEXT_COMPILED`、`CAPABILITY_GATE_DECISION` | run entry/finalizer、capability gate、context compiler；start/terminal/final batches；当前全durable | capability surface、interaction、error、compiled context，S1–S2；Inspector、runtime reducers、resume | Run/Reply/context/gate被用于execution recovery；capability decision与terminal outcome有审计价值 | capability/accepted policy decision、turn error/interrupted/completed可为A；ReplyStart/End及compiled proof改canonical/Live/C；policy执行只走`ToolDispatchAuthorizationPolicy`，不以普通hook替代且不rewrite arguments |
| model/provider input（12） | `MODEL_CALL_START`、`PROVIDER_INPUT_GENERATION_STARTED`、`PROVIDER_INPUT_APPEND_COMMITTED`、`PROVIDER_INPUT_EXISTING_PREPARATION_ABANDONED`、`PROVIDER_INPUT_SCOPED_PREPARATION_ABANDONED`、`PROVIDER_INPUT_GENERATION_ROLLOVER_RESOLVED`、`PROVIDER_INPUT_GENERATION_CLOSED`、`MODEL_CALL_TERMINAL_PROJECTION_COMMITTED`、`MODEL_CALL_END`、`PROVIDER_MODEL_STREAM_ERROR`、`MODEL_CALL_CONTROL_DISPOSITION_RESOLVED`、`MODEL_CALL_REJECTED` | `llm/runtime.py`、provider-input generation、terminal projection、control owner及`model_stream_recovery.py`；多次EventLog transaction；当前全durable | provider/model/input cut/projection fingerprint/error/disposition，S1–S2；materializer、recovery、Inspector、admission | 这是foreground execution recovery主链；reject/error有用户展示或诊断价值，但不需要独立durable call lifecycle | durable lifecycle/projection/disposition D删除；call start/end、retry/TTFT、reject/stream error为C或当前live error；accepted assistant只由canonical completion transaction + `AssistantMessageAccepted`表达，导致turn终止的拒绝由`TurnInterrupted`表达，不新增ModelRejected core type |
| model stream（13） | `TEXT_BLOCK_START`、`TEXT_BLOCK_SEGMENT`、`TEXT_BLOCK_END`、`DATA_BLOCK_START`、`DATA_BLOCK_SEGMENT`、`DATA_BLOCK_END`、`THINKING_BLOCK_START`、`THINKING_BLOCK_SEGMENT`、`THINKING_BLOCK_END`、`HINT_BLOCK`、`TOOL_CALL_START`、`TOOL_CALL_ARGUMENTS_SEGMENT`、`TOOL_CALL_END` | `llm/segment.py`/coalescer从semantic draft生成并由`llm/runtime.py`逐批commit；当前全durable | partial text/thinking/data/tool args、seal/fingerprint/ordinal，S1；thinking/tool args为S2；terminal reducer、block hooks、Inspector/TUI、recovery | 直接用于terminal projection与incomplete stream replay；live UI/hook价值极高 | durable Segment/coalescing/fingerprint全部删除；Text/Thinking/ToolCall（及已有Data extension）改B Start/Delta/End；Start immutable，Delta只进单assembler，End携带final frozen block；无production producer的`HINT_BLOCK`删除，不保留built-in slot |
| tools/interactions（16） | `TOOL_RESULT_START`、`TOOL_RESULT_TEXT_DELTA`、`TOOL_RESULT_DATA_DELTA`、`TOOL_RESULT_TERMINAL_PROJECTION_COMMITTED`、`TOOL_RESULT_END`、`TOOL_EXECUTION_SUSPENDED`、`MCP_INPUT_REQUIRED_RESOLUTION_SUBMITTED`、`MCP_CONTINUATION_DISPATCH_RESERVED`、`MCP_INPUT_REQUIRED_EXPIRED`、`MCP_INPUT_REQUIRED_BINDING_CHANGED`、`MCP_INPUT_REQUIRED_RESUME_FAILED`、`MCP_INPUT_REQUIRED_INTERACTION_CLOSED`、`REQUIRE_USER_CONFIRM`、`USER_CONFIRM_RESULT`、`REQUIRE_EXTERNAL_EXECUTION`、`EXTERNAL_EXECUTION_RESULT` | tool loop/executor、terminal projection、MCP interaction/confirmation owners；多批EventLog+transaction companion；当前全durable | tool output/delta/arguments/interaction/private URL，S1–S2，secret为S3禁止；tool loop、Inspector/TUI、resume | terminal projection、suspension/resume与confirmation gate execution；tool/interaction审计价值高 | 完整tool-request message先原子canonical commit，之后才可attempt-before-dispatch；result canonical+A；result live delta可B；accepted user/external decision为canonical+A；request本身V1 live；reservation/projection/resume proof D删除 |
| terminal monitor/notification（9） | `TERMINAL_PROCESS_COMPLETED`、`TERMINAL_PROCESS_MONITOR_REGISTERED`、`TERMINAL_PROCESS_MONITOR_OBSERVATION_COMMITTED`、`TERMINAL_PROCESS_MONITOR_TERMINATED`、`TERMINAL_PROCESS_MONITOR_RECEIPT_APPLIED`、`TERMINAL_PROCESS_OBSERVATION_DELIVERY_DISPOSITION`、`TERMINAL_PROCESS_OBSERVATION_DELIVERY_DEFERRED`、`TERMINAL_NOTIFICATION_RESERVATION_CREATED`、`TERMINAL_NOTIFICATION_RESERVATION_RELEASED` | terminal monitor、notification delivery、receipt owner；EventLog transaction；当前全durable | process/remote identity/output reference/delivery state，S1–S2；monitor worker、tool continuation、Inspector/TUI | completion可唤醒Agent；receipt/reservation参与delivery/close proof | yielded process/monitor/notification改为Host-scoped process-local；completion/observation/close走`TerminalProcessCompleted`与`TerminalMonitor*` live extension。若completion随后被接受为tool result/entry，只由`ToolResultAccepted`等既有core type记occurrence；不设terminal-specific A。registration receipt、delivery disposition、reservation与restart owner D删除，不创建job/launch-token rebind |
| prompt queue/steer（10） | `PROMPT_QUEUE_ACCEPTED`、`PROMPT_QUEUE_RESERVATION_INSTALLED`、`PROMPT_QUEUE_RESERVATION_RELEASED`、`PROMPT_QUEUE_DELIVERY_REJECTED`、`PROMPT_QUEUE_COMMITTED_TO_RUN`、`PROMPT_QUEUE_COMMITTED_TO_PROVIDER_INPUT`、`PROMPT_QUEUE_CANCELLED`、`PROMPT_QUEUE_RECONCILIATION_REQUIRED`、`PROMPT_QUEUE_CONTENT_RETIRED`、`USER_STEER_COMMITTED` | terminal application/prompt queue/steer coordinator；EventLog and queue companion；当前全durable | user prompt/content/claim/reason，S1–S2；queue reducer、TUI、resume | accepted/order/cancel是产品事实；reservation/reconciliation/checkpoint gate queue | canonical queue rows + accepted/committed/cancelled/steer A；claims用row lease；reservation/reconciliation/content-retired proof D删除或C |
| plan（6） | `PLAN_MODE_ENTERED`、`PLAN_QUESTION_ASKED`、`PLAN_QUESTION_ANSWERED`、`PLAN_EXIT_REQUESTED`、`PLAN_EXIT_RESOLVED`、`PLAN_MODE_EXITED` | plan/application interaction owner；EventLog transaction；当前全durable | question/answer/decision，S1–S2；plan reducer、TUI、resume | request/answer可suspend run；有直接用户可观察与hook价值 | current plan state是canonical row truth；未决question/request为Live control；accepted answer/exit只发一条`InteractionDecisionAccepted`，enter/exit mode不另发A，不恢复suspended coroutine |
| memory/projection（19） | `MEMORY_CANDIDATE_PROPOSED`、`MEMORY_WRITE_RESULT`、`MEMORY_WRITE_FAILED`、`MEMORY_REFLECTION_COMPLETED`、`MEMORY_REFLECTION_FAILED`、`MEMORY_SUPERSEDED`、`MEMORY_CONTRADICTION_LINKED`、`MEMORY_MARKED_STALE`、`MEMORY_MAINTENANCE_PROPOSED`、`MEMORY_MAINTENANCE_APPLIED`、`MEMORY_MAINTENANCE_REJECTED`、`MEMORY_GOVERNANCE_BATCH_PREPARED`、`MEMORY_GOVERNANCE_BATCH_COMPLETED`、`MEMORY_GOVERNANCE_BATCH_FAILED`、`MEMORY_GOVERNANCE_BATCH_BLOCKED`、`MEMORY_CANDIDATE_EVIDENCE_REJECTED`、`PROJECTION_REQUESTED`、`PROJECTION_READY`、`PROJECTION_FAILED` | memory lifecycle/governance/outbox/projection owner；EventLog + memory companion；当前全durable；三种`MEMORY_MAINTENANCE_*`无production constructor | fact/relation/candidate/evidence/reason/projection ref，S1–S2；memory reducer/governance/Inspector | accepted memory lifecycle有长期语义；candidate intake是durable work fact，batch/projection进度多为proof gate | PostgreSQL candidate/fact/relation为各自work/canonical truth；accepted write/supersede/contradiction/stale可A；自动extraction与governance为job；candidate row保留但candidate/batch/projection progress event为C或D；三个dormant maintenance type删除；不新增delete/forget语义，不扩大超过现有bounded two-hop |
| compaction（8） | `CONTEXT_COMPACTION_STARTED`、`CONTEXT_COMPACTION_COMPLETED`、`CONTEXT_COMPACTION_REQUESTED`、`MID_TURN_CONTEXT_COMPACTION_SKIPPED`、`TOOL_RESULT_EVIDENCE_PROJECTION_FAILED`、`CONTEXT_COMPACTION_MEMORY_EXTRACTION_REQUESTED`、`CONTEXT_COMPACTION_MEMORY_EXTRACTION_COMPLETED`、`CONTEXT_COMPACTION_FAILED` | compaction/context/memory extraction owners；EventLog transactions；当前全durable；`TOOL_RESULT_EVIDENCE_PROJECTION_FAILED`无production constructor | source range/summary/job/error，S1–S2；context builder、Inspector、resume | completed snapshot可影响future context；进度/failure不应否定conversation | immutable semantic context snapshot/binding revision canonical；adopted compaction可A；started/skipped/failure为C；memory extraction为job；dormant projection failure D删除 |
| subagent/task（19） | `SUBAGENT_RUN_STARTED`、`SUBAGENT_MESSAGE_SENT`、`SUBAGENT_RUN_SUSPENDED`、`SUBAGENT_RUN_COMPLETED`、`SUBAGENT_RUN_FAILED`、`SUBAGENT_RUN_CANCELLED`、`SUBAGENT_EDGE_RECORDED`、`SUBAGENT_RESULT_DELIVERED`、`SUBAGENT_TASK_CREATED`、`SUBAGENT_TASK_SCHEDULED`、`SUBAGENT_TASK_STARTED`、`SUBAGENT_TASK_BLOCKED`、`SUBAGENT_TASK_COMPLETED`、`SUBAGENT_TASK_FAILED`、`SUBAGENT_TASK_CANCELLED`、`SUBAGENT_PHASE_REPORTED`、`SUBAGENT_RESULT_SUBMITTED`、`SUBAGENT_RESULT_CONSUMED`、`SUBAGENT_GRAPH_CHECKPOINT_COMMITTED` | subagent runtime/task graph/reducer；EventLog batches；当前全durable | task/message/result/edge/phase/checkpoint，S1–S2；parent runtime、Inspector/TUI、resume | task/result有coordination语义；suspend/schedule/phase/checkpoint被用于execution recovery | Host-owned canonical task/objective/parent-child/message/result/terminal-status rows + 窄accepted lifecycle A；全部child execution、partial live output与phase progress只在当前进程；Host结束时未terminal task原子转interrupted；schedule/suspend/graph checkpoint、worker attempt/claim与跨Host recovery D删除 |
| transcript checkpoint（5） | `TRANSCRIPT_PROJECTION_CHECKPOINT_INTENT`、`TRANSCRIPT_PROJECTION_CHECKPOINT_COMMITTED`、`TRANSCRIPT_PROJECTION_CHECKPOINT_FAILED`、`TRANSCRIPT_PROJECTION_CHECKPOINT_CANCELLED`、`TRANSCRIPT_PROJECTION_CHECKPOINT_RECOVERED_INTERRUPTED` | authority materialization/checkpoint owner；EventLog transaction；当前全durable | prefix/head/fingerprint/status，S1；projection reducer、repair、close | 纯加速/proof，却能触发reconciliation与close wait；Inspector价值可由query替代 | D全删；canonical queries/可重建index取代；checkpoint failure不得影响commit |
| materialization/proof（11） | `LEDGER_MATERIALIZATION_ACCOUNT_GENESIS`、`LEDGER_MATERIALIZATION_CONSUMER_REGISTERED`、`LEDGER_MATERIALIZATION_CONSUMER_HORIZON_ADVANCED`、`LEDGER_MATERIALIZATION_CONSUMER_RETIRED`、`LEDGER_MATERIALIZATION_GENERATION_ADVANCED`、`PHYSICAL_OPERATION_RESERVATION_CREATED`、`PHYSICAL_OPERATION_CHARGE_APPLIED`、`PHYSICAL_OPERATION_RESERVATION_SUSPENDED`、`PHYSICAL_OPERATION_RESERVATION_SETTLED`、`CHECKPOINT_DISPATCH_BARRIER_INSTALLED`、`CHECKPOINT_DISPATCH_BARRIER_RELEASED` | `runtime/authority_materialization/` owners与EventLog companion transaction；当前全durable | account/horizon/reservation/barrier/fingerprint，S1；admission、reducer repair、close | 核心proof-of-proof/recovery gate；几乎无独立Inspector/TUI产品语义 | D全删；真实physical effect只保留tool/job attempt row；consumer offset若未来确需跨进程必达，归具体durable job，不恢复通用receipt graph |

分布为 `7+7+9+12+13+16+9+10+6+19+8+19+5+11=151`。这也修正“151类都没有价值”的错误结论：当前大约一半是machinery、projection或execution lifecycle，但conversation/tool/job/memory/coordination occurrence和live block lifecycle具有明确Inspector、TUI、eval与hook价值；正确减法是**分层与选择性持久化**，不是抹掉typed vocabulary。

#### 2.1.2.1 五片逐项生命周期审计与根审查

为冻结最终vocabulary，本轮按`AgentEvent` union顺序把151类拆为五片；每片先完整阅读本文，再对每一类执行class/payload、production constructor、提交batch/transaction companion、serializer/historical decoder、consumer/reducer、Inspector/TUI/hook、recovery/gate、tests/architecture guard的AST与`rg`追踪。上面的family表是151类的穷尽索引；下面是五张30/31行逐项表的根汇总，而不是按名字或测试数量猜测价值：

| union slice | A候选 | B | C | D/row-only | 代码真值摘要 |
|---|---:|---:|---:|---:|---|
| 001–030 | 4 | 0 | 7 | 19 | RunStart/End/ReplyEnd/CapabilityGate中含可重塑accepted occurrence；provider-input、checkpoint与materialization是成套recovery/proof graph |
| 031–060 | 2 | 16 | 0 | 12 | durable model/tool stream应转Live；完整tool result与accepted MCP decision可转A；reservation/projection删除 |
| 061–090 | 10 | 8 | 2 | 10 | pending interaction与terminal monitor转Live；accepted decision/queue/steer转A；delivery receipt/reservation删除 |
| 091–120 | 13 | 0 | 5 | 12 | memory fact/lifecycle、compaction adoption和generic work outcome含A；candidate/batch/projection progress不含A；plan双event合一 |
| 121–151 | 10 | 1 | 2 | 18 | subagent accepted task/message/result/status与compaction adoption含A；run/task重复、delivery receipt、checkpoint、rollout account删除 |
| **合计** | **39** | **25** | **16** | **71** | 这是旧type的主处置，不是最终registry数量；39个A候选经semantic dedup并删除可推导的unknown occurrence后冻结为决策7的26类core |

八个current type经全仓AST确认没有production constructor：`HINT_BLOCK`、`TOOL_RESULT_DATA_DELTA`、`REQUIRE_EXTERNAL_EXECUTION`、`PROMPT_QUEUE_RECONCILIATION_REQUIRED`、三种`MEMORY_MAINTENANCE_*`和`TOOL_RESULT_EVIDENCE_PROJECTION_FAILED`。其中Data tool-result variant有assembler/tests支持，目标把它收进process-local `ToolResultDelta`的closed data branch；其余不能因historical registry或test fixture存在而保留built-in durable type。`EXTERNAL_EXECUTION_RESULT`有producer组件但没有in-tree application caller，按现有产品入口不得宣传为已启用能力。

根审查对五片建议做了六项必要收紧：

1. `PROMPT_QUEUE_DELIVERY_REJECTED`保留为独立`PromptRejected`，不冒充user cancellation；
2. `TOOL_RESULT_START/TEXT_DELTA/DATA_DELTA`保留为nonblocking process-local `ToolResultStart/Delta/End` grammar，但current `TOOL_RESULT_END`中的accepted product事实只映射`ToolResultAccepted`；live End不证明commit；
3. `PLAN_EXIT_RESOLVED + PLAN_MODE_EXITED`只形成一个canonical decision transaction和一条`InteractionDecisionAccepted`；permission/current-mode是row truth；
4. candidate proposal、evidence rejection和governance batch prepared不发A。reflection/governance/extraction的六套domain terminal type也不换名保留；它们进入通用job rows，只有job aggregate真正terminal时发`JobTerminalAccepted`，中间retryable attempt terminal只留attempt row和C；
5. `CONTEXT_COMPACTION_COMPLETED`和`CONTEXT_WINDOW_COMPACTION_COMPLETED`只有在同transaction推进binding revision时才映射一条`CompactionAdopted`；summarizer完成、started、failed、skipped均不是A；
6. `SUBAGENT_RUN_*`不与task lifecycle并存。初始active/waiting吸收到`SubagentTaskAccepted.initial_status`，后续用户可观察状态走一个closed `SubagentTaskStatusAccepted`；phase是`SubagentProgress` live event。message/result各有exact canonical child subject，不能继续只引用task aggregate或在payload放自由ordinal。

因此“39个A候选”不能被理解为保留39个旧class，更不能把old payload复制到新event。决策7的26类表才是normative registry；本节的A/B/C/D只是从旧151类到目标plane/row的migration evidence。

#### 2.1.3 当前transaction、serializer、confirmation与recovery真值

- **[代码确认]** `agent_events`当前保存session sequence、event/schema/domain fingerprints、transcript prefix accumulator、ledger continuity accumulator与JSONB payload；`PostgresEventLog`锁session、分配sequence、insert并exact read-back确认。[0002_runtime_truth_baseline.sql](src/pulsara_agent/storage/migrations/sql/0002_runtime_truth_baseline.sql#L1)、[postgres.py](src/pulsara_agent/event_log/postgres.py#L1)
- **[代码确认]** 当前session append用`pg_advisory_xact_lock(hashtextextended(session_id, 0))`串行化，证明Host与worker共享一个窄sequence allocator在机制上可行；但`FrozenEventWriteCandidate`只携带event id/type/schema/fingerprint/payload，没有Host-writer或job-attempt claim guard，所以这把锁目前只解决排序，不表达谁有权共写哪类canonical subject。[postgres.py](src/pulsara_agent/event_log/postgres.py#L2090)、[event_write.py](src/pulsara_agent/ports/event_write.py#L20)
- **[代码确认]** 当前`agent_events`以必填session/run/turn/reply identity和通用JSON payload承载全部151类事件；baseline只对session/run/turn建立通用FK且使用cascade delete，没有能精确覆盖entry、tool attempt、job attempt、queue、decision、memory等target subject的closed union。因此目标typed subject integrity是schema重建，不是对现有能力重新命名。[0002_runtime_truth_baseline.sql](src/pulsara_agent/storage/migrations/sql/0002_runtime_truth_baseline.sql#L1)
- **[代码确认]** 当前已有tool-result专用的bounded artifact read先例：`ToolArtifactReadPort`暴露受限text range，runtime reader先按session解析artifact index，PostgreSQL archive也验证session owner后读取；但它以tool artifact id与字符区间为边界，没有Protocol v3所需的canonical transcript subject/content-slot、workspace/principal capability、byte-range、完整digest/size/codec绑定。因此它只能证明“窄读取可实现”，不能被当作目标`ReadCanonicalContent`已经存在。[artifact.py](src/pulsara_agent/ports/artifact.py#L448)、[tool_artifacts.py](src/pulsara_agent/runtime/tool_artifacts.py#L200)、[postgres_archive.py](src/pulsara_agent/memory/artifacts/postgres_archive.py#L276)
- **[代码确认]** `extend_with_materialization_state`已经能在一个PostgreSQL transaction内共写event、materialization state与transaction companion canonical rows。这证明“canonical row + committed occurrence event同owner同transaction”可实现；目标删除的是通用proof columns/account graph，不是这条原子共写能力。[postgres.py](src/pulsara_agent/event_log/postgres.py#L1)
- **[代码确认]** `runtime/session.py`在commit后依次fold reducer、tap presentation、enqueue publisher；confirmation unknown、fold repair或`await_delivery=True`的subscriber error可反向形成reconciliation/publication error。目标必须切断这种反向否决：canonical transaction成功后，任何event consumer/hook失败都不能把fact改成failed。[session.py](src/pulsara_agent/runtime/session.py#L1)、[event_write.py](src/pulsara_agent/ports/event_write.py#L1)
- **[代码确认]** `model_stream_recovery.py`扫描durable ledger，找出有`MODEL_CALL_START`无`MODEL_CALL_END`的call，replay Start/Segment/End，持久化terminal projection，再合成ModelCallEnd/ReplyEnd与settlement。这正是要删除的execution recovery state machine。[model_stream_recovery.py](src/pulsara_agent/runtime/model_stream_recovery.py#L1)
- **[代码确认]** architecture/contract tests把当前边界锁得很紧：schema generation必须为11；raw provider 7类不得进入`AgentEvent` union；durable `*DeltaEvent`名称被禁止而`*SegmentEvent`被要求；message/event-log tests从Start/Segment/End replay assistant/tool message；hook tests要求typed selector、sync/async顺序、deep-copy隔离与非致命error；publisher tests则确认subscriber failure可向等待者抛出但后续delivery继续。[test_runtime_event_architecture.py](tests/test_runtime_event_architecture.py#L199)、[test_event_message_system.py](tests/test_event_message_system.py#L125)、[test_runtime_hooks.py](tests/test_runtime_hooks.py#L42)、[test_runtime_publisher.py](tests/test_runtime_publisher.py#L250) 这些是当前contract证据，不是目标必须保留durable Segment/replay的理由；Stage 2/3必须用新的live ordering、same-transaction committed journal与failure-isolation guards替换。
- **[设计建议]** reopen只读canonical rows；Host crash使provider live generation与session-control owner epoch一并消失，incomplete turn按canonical规则变`interrupted`。不得补写历史Text/Thinking/ToolCall Start/End或Interaction Opened/Closed，也不得根据event证明assistant/tool row存在。

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

#### 2.4.5 current control view证明entry sequence之外还需要accepted-occurrence维度

当前v2把session lifecycle、run、pending interaction与prompt queue作为独立control sections编码，并为每节携带source version/fingerprint：[codec.py](src/pulsara_agent/terminal_protocol/codec.py#L1000)。wire还定义了带`control_generation`、`control_revision`和transition accumulator的`ControlProjectionCursor`：[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1037)。这套per-section version、fingerprint、transition history属于待删amplification，但它揭示了一个真实需求：queue、turn control或session lifecycle可以在没有新transcript entry时改变。

目标Protocol v3不继承这套cursor graph，也不新增durable `control_revision`。它使用selective `agent_events`的`latest_event_sequence`作为accepted-occurrence高水位：用户可观察的queue、turn、tool-attempt、job、memory与coordination等决策7 transition在原transaction内同时追加一个窄`StoredCommittedEvent`。V1 session detach/close没有core occurrence。若Observe只比较transcript `latest_sequence`确实会漏掉这些变化；Gateway从event cut之后level-read stored suffix，并在同一bounded MVCC cut中把event与exact subject组合成`CommittedObservationProjection`，既保留level-triggered条件和typed audit，也让Go获得可直接应用的current/immutable state，而不是新增另一套整数cursor或durable presentation projection。

#### 2.4.6 pending interaction当前混合了live working state与durable recovery authority

`RunActivationWorkingState`的类注释明确称其为“Short-lived”“not a durable fact source”，但其中直接保存pending tool calls、interaction kind/payload与source candidate：[state.py](src/pulsara_agent/runtime/state.py#L118)。Host读取的pending interaction来自当前suspended run owner的live view：[session.py](src/pulsara_agent/host/session.py#L884)，TUI再把approval、plan与MCP request编码为`PendingInteraction`：[codec.py](src/pulsara_agent/terminal_protocol/codec.py#L830)。另一方面，现体系又为interaction resume建立durable transition、recovery与reconciliation路径：[session.py](src/pulsara_agent/host/session.py#L4183)。

这正是减法需要明确切开的边界：V1保留同一Host内可通过`owner_epoch/live_revision`与atomic snapshot-subscribe重新查询/观察的live request，但不保留其跨Host execution continuation；accepted decision是产品事实，尚未回答的request不是。canonical snapshot不得再把live request伪装成durable control row。

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

### 4.1 证据边界与版本

- **[代码确认]** Pulsara代码基线为 f752a04439cf18961899ab6345929a59d0d80082。
- **[代码确认]** Codex本地仓库是OpenAI公开Rust源码，基线为6138909d6ec58b2fbe635ef973e02caecad5a5aa（2026-07-10）；本文只陈述该commit可见的wire、rollout、hook与resume实现。[Codex commit](https://github.com/openai/codex/commit/6138909d6ec58b2fbe635ef973e02caecad5a5aa)
- **[代码确认]** Grok-build本地仓库是xAI公开Rust源码，extension/policy补充基线为c68e39f60462f28d9be5e683d9cbe2c57b1a5027（2026-07-16）；只用于冻结7.3的registration、capability、hook failure与permission fallback，不把其free-form custom hook照搬为Pulsara contract。[Grok-build commit](https://github.com/xai-org/grok-build/commit/c68e39f60462f28d9be5e683d9cbe2c57b1a5027)
- **[代码确认]** Claude Code本地仓库README自述为2026-03-31 source-map泄漏还原，不是Anthropic官方源码发布。因此本地TypeScript只作辅助实现证据，不能覆盖官方文档或外推服务端保证。[README.md](../claude-code/README.md#L3)
- **[官方文档确认]** Claude Code依据[Hooks reference](https://code.claude.com/docs/en/hooks)、[Agent SDK streaming output](https://code.claude.com/docs/en/agent-sdk/streaming-output)、[Manage sessions](https://code.claude.com/docs/en/sessions)与[Application data](https://code.claude.com/docs/en/claude-directory)，访问日期2026-08-08。
- **[官方文档确认]** Codex依据[Hooks](https://learn.chatgpt.com/docs/hooks)、[App Server](https://learn.chatgpt.com/docs/app-server)与[Developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)，访问日期2026-08-08；wire/persistence细节再以同日检出的官方公开源码为准。
- **[合理推断]** “源码或文档未出现通用exactly-once、capability projection或overflow contract”只表示没有足够证据确认，绝不等同于产品不存在任何内部机制。

### 4.2 分层事实对照

| 维度 | Claude Code | OpenAI Codex | Pulsara当前代码 | 本文冻结的Pulsara目标 |
|---|---|---|---|---|
| raw provider delta生命周期 | **[官方文档确认]** Agent SDK默认产出完整AssistantMessage；开启partial messages后，额外产出包裹raw API event的StreamEvent，顺序包含message/content-block start、delta、stop，最后仍有完整AssistantMessage与ResultMessage。raw stream服务当前调用的实时UI，不被官方文档描述为session transcript replay单位。 | **[代码确认]** ResponseEvent区分OutputItemAdded/Done、OutputTextDelta、ToolCallInputDelta、Reasoning delta与Completed；turn assembler把delta发成typed EventMsg，completed ResponseItem另行处理。[common.rs](../codex/codex-rs/codex-api/src/common.rs#L73)、[turn.rs](../codex/codex-rs/core/src/session/turn.rs#L2279) | **[代码确认]** raw provider有7个private process-local type，但normalized draft随后被coalescer转成durable Start/Segment/End，形成两层stream模型。 | **[设计建议]** vendor SDK object只存在于adapter调用栈；sanitizer/normalizer直接产出未coalesce的Text/Thinking/Data/ToolCall Start/Delta/End `LiveAgentEvent`。Start immutable，Delta更新唯一assembler，End带final frozen block；不保留独立`RawProvider*`/semantic-draft协议，也无durable segment/replay。 |
| completed message/transcript durability | **[官方文档确认]** session工作时持续写本地JSONL，内容包括message、tool call与tool result；resume可选择完整session或summary。local transcript默认30天后按整份session文件清理，可用`cleanupPeriodDays`调整；这不是session内部prefix pruning。[Manage sessions](https://code.claude.com/docs/en/sessions)、[Application data](https://code.claude.com/docs/en/claude-directory) **[代码确认]** 非官方快照使用append queue写JSONL并按mtime删除过期session文件。[sessionStorage.ts](../claude-code/src/utils/sessionStorage.ts#L606)、[cleanup.ts](../claude-code/src/utils/cleanup.ts#L23) | **[代码确认]** `ThreadStore::append_items`是canonical history append API；rollout policy持久化completed ResponseItem、turn lifecycle及选择性EventMsg，raw response、ItemStarted、content/reasoning delta和HookStarted/Completed明确transient。resume读取完整rollout；compaction追加`CompactedItem.replacement_history`而不重写旧items；冷rollout只做verified zstd表示转换，archive移动文件，删除必须显式调用。[README.md](../codex/codex-rs/thread-store/README.md#L9)、[policy.rs](../codex/codex-rs/rollout/src/policy.rs#L7)、[recorder.rs](../codex/codex-rs/rollout/src/recorder.rs#L933)、[session/mod.rs](../codex/codex-rs/core/src/session/mod.rs#L3030)、[compression.rs](../codex/codex-rs/rollout/src/compression.rs#L600) | **[代码确认]** completed assistant语义依赖terminal projection、ModelCallEnd、ReplyEnd、disposition与reducer materialization，当前没有单一canonical message commit；当前production inventory未发现semantic transcript prefix-prune路径，checkpoint GC只删除不可达的加速artifact。 | **[设计建议]** provider completion后一次transaction原子写完整assistant message及ordered semantic blocks；同transaction可写一条AssistantMessageAccepted committed event。accepted canonical transcript在session存续期append-only，compaction只追加derived snapshot，不做prefix retention。 |
| typed lifecycle/event vocabulary | **[官方文档确认]** hooks覆盖session、turn、tool、permission、subagent、task、compaction、MCP elicitation、file/config/worktree等广泛lifecycle；MessageDisplay还能在assistant text显示期间触发。不能声称Claude Code“没有事件”。 | **[代码确认]** EventMsg是广泛serde-tagged typed union，包含TurnStarted/Complete/Aborted、RawResponseItem、ItemStarted/Completed、typed deltas、HookStarted/Completed及tool/collab begin/end。[protocol.rs](../codex/codex-rs/protocol/src/protocol.rs#L1273) 不能声称Codex“没有事件”。 | **[代码确认]** 151类全部进入同一个durable registry，typed vocabulary与durability/recovery policy耦合。 | **[设计建议]** 统一语义命名，但以Committed/Live/Operational不同base和transport分层；统一不等于统一持久化。 |
| hooks及pre/post tool | **[官方文档确认]** 支持command、HTTP、MCP tool、prompt和agent handler；PreToolUse可deny，PostToolUse/PostToolUseFailure可观察或反馈，所有matching handler并行；还支持async/asyncRewake。 | **[官方文档确认]** 当前release提供PreToolUse、PermissionRequest、PostToolUse、Pre/PostCompact、SessionStart/End、UserPromptSubmit、SubagentStart/Stop、Stop等；PreToolUse可block/rewrite，PostToolUse发生在tool已执行后。async字段可解析但异步command hook尚不支持，prompt/agent handler当前也会跳过。 | **[代码确认]** runtime/hooks.py已有typed selector、sync/async callback、deep-copy隔离、非致命error记录及completed-block hook，但registration没有identity/scope/schema/capability/lease、queue bound或timeout。 | **[设计建议]** 只把live streaming、post-commit domain和operational diagnostics称为普通hook；tool dispatch authorization走独立`ToolDispatchAuthorizationPolicy`，V1不提供argument rewrite。 |
| ordering与schema/version | **[官方文档确认]** raw API stream有provider order；同一event的matching hooks并行，故不提供handler完成总序。官方文档频繁标注最低Claude Code版本，且transcript明确不是稳定schema。 | **[代码确认]** EventMsg有明确tagged wire type；rollout policy从同一vocabulary选durable子集。HookRunSummary含display_order，但官方hooks文档确认matching command hooks并发启动；App Server使用versioned generated protocol/JSON-RPC surface。 | **[代码确认]** durable serializer version 11且sequence严格，但live observer和hook没有独立schema/version binding；metadata可带任意dict。 | **[设计建议]** registration必须绑定protocol namespace/type/schema version与scope；committed hook按event_sequence观察，live hook只承诺单generation发布顺序，不承诺跨handler完成顺序。 |
| durable audit与历史查询 | **[官方文档确认]** 可resume、export并定位JSONL transcript；官方警告直接解析内部JSONL可能随release破坏。Hook获得transcript_path，但文件异步写、触发时可能落后内存。未见独立、稳定的domain-event audit query承诺。 | **[官方文档确认]** App Server提供thread/list/read/turns/list/resume；**[代码确认]** rollout保留conversation与选择性lifecycle，而不是所有wire event。 | **[代码确认]** historical decoder/sequence/fingerprint可严格验证全部151类，但这套audit同时承担execution recovery和consumer proof。 | **[设计建议]** selective agent_events提供稳定、versioned、按event_sequence查询的occurrence/audit；canonical查询回答“现在是什么”，journal回答“何时接受了什么”。 |
| backpressure与hook failure isolation | **[官方文档确认]** hook有per-handler timeout；多数非2退出码是non-blocking error，async hook立即返回且在headless teardown会cancel；每次触发独立process、无dedup。官方资料没有为raw stream observer承诺GAP/overflow语义。**[代码确认]** 2026-03-31非官方快照的hook event queue上限100并drop-oldest，只作辅助证据。[hookEvents.ts](../claude-code/src/utils/hooks/hookEvents.ts#L1) | **[官方文档确认]** matching command hooks并发启动、trust后才运行，有timeout和大输出spill；不支持的hook output会标fail并继续tool call。**[代码确认]** rollout持久化失败记录error后不改变已生成ResponseItem，transient event走独立delivery。[session/mod.rs](../codex/codex-rs/core/src/session/mod.rs#L1946) | **[代码确认]** RuntimeEventPublisher使用unbounded queue且串行await subscribers；await_delivery可把subscriber error暴露为post-commit publication error。TUI observation ring反而已经有bounded/GAP/detach先例。 | **[设计建议]** 每observer bounded queue；provider/control overflow若可行则发对应LiveGap后detach，否则直接detach；ordinary post-commit hook overflow发HookGap并detach。provider与canonical commit不等待hook；普通hook无自动catch-up，V1无generic可靠action；future可靠需求独立ADR为具名job。timeout/exception只记operational诊断，close只bounded等待已开始callback。 |
| sensitive payload与capability boundary | **[官方文档确认]** hook常见input包含tool_input与transcript_path；HTTP allowlist、allowed env var、workspace trust及安全指南限制执行面，但默认不是逐字段redacted projection，文档要求hook作者跳过敏感文件。 | **[官方文档确认]** 非managed hook需要review/trust，project/plugin来源有trust policy；PreToolUse可见JSON arguments，transcript_path不是稳定接口。没有官方证据表明raw thinking/tool args按订阅者capability做字段级投影。 | **[代码确认]** sanitizer会移除auth/cookie/URL credential，McpSecret不可序列化；但通用EventBase metadata和普通hook订阅仍没有capability projection contract。 | **[设计建议]** 把authenticated first-party user view与extension view分开：用户对已投影raw thinking原样可见，tool args短则完整、长则显式截断；ordinary hook仍只见typed/redacted projection，raw thinking/未redacted tool args只给具名S2 lease，private URL只给current-controller interaction view，S3永不进入event。 |
| crash/reconnect语义 | **[官方文档确认]** session持续保存并可resume完整conversation；scheduled tasks可恢复，而background Bash/monitor不恢复。**[合理推断]** raw StreamEvent未被描述为历史重放，crash后只能从已保存conversation继续。 | **[官方文档确认]** thread/resume从persisted thread继续；fork遇到in-progress turn会记录interruption marker。**[代码确认]** delta/raw/hook event为transient，resume重构rollout history而不是旧Rust future。[rollout_reconstruction.rs](../codex/codex-rs/core/src/session/rollout_reconstruction.rs#L1) | **[代码确认]** model_stream_recovery replay durable segments、生成terminal projection并合成历史ModelCallEnd/ReplyEnd。 | **[设计建议]** Host crash后live stream消失，turn按canonical row变interrupted；不合成历史Start/End，不恢复provider cursor或foreground coroutine。 |
| UI、automation、third-party extension | **[官方文档确认]** 同一hook events跨CLI、IDE、Desktop和web触发，并支持project/plugin/managed/session scope；Agent SDK partial stream支持自建UI。 | **[官方文档确认]** App Server向客户端提供thread/turn/item notifications与history APIs；hooks、plugins和non-interactive JSONL支持automation。 | **[代码确认]** Inspector/TUI/hook能力丰富，但大多绑定universal durable replay、presentation root或raw event payload。 | **[设计建议]** TUI以canonical snapshot + stored-event/exact-subject observation projection + provider/tool-result/control live stream启动；Inspector以canonical rows回答状态、以journal回答历史、以live protocol展示当前owner；third-party hook只拿capability-scoped typed projection，不能append committed event。 |

### 4.3 hooks/extension/policy比较对Pulsara规范的直接约束

1. **[官方文档确认]** Claude Code与Codex都允许PreToolUse影响是否执行，说明pre-execution policy有真实产品价值；它们也说明普通“观察hook”和“授权/改写port”混在一个配置表会扩大失败语义。
2. **[代码确认]** Codex把hook event、handler/source/trust/status做成closed typed DTO，按normalized config hash区分Trusted/Modified；但handler key仍带group/handler位置且源码明确留有“replace this positional suffix with a durable hook id”的TODO。[hook_config.rs](../codex/codex-rs/config/src/hook_config.rs#L11)、[discovery.rs](../codex/codex-rs/hooks/src/engine/discovery.rs#L489) matching handler并发运行，多个PreToolUse rewrite以最后完成者获胜；HookStarted/Completed明确transient。[dispatcher.rs](../codex/codex-rs/hooks/src/engine/dispatcher.rs#L70)、[pre_tool_use.rs](../codex/codex-rs/hooks/src/events/pre_tool_use.rs#L128)、[policy.rs](../codex/codex-rs/rollout/src/policy.rs#L117)
3. **[代码确认]** Grok-build普通hook默认5秒、失败明确fail-open，配置identity同样按文件/event数组位置生成；其更强的tool protocol另行使用namespaced ToolId、hub-derived user/connection identity、protocol version、capability negotiation、registration generation与disconnect cleanup。[config.rs](../grok-build/crates/codegen/xai-grok-hooks/src/config.rs#L115)、[result.rs](../grok-build/crates/codegen/xai-grok-hooks/src/result.rs#L50)、[handshake.rs](../grok-build/crates/common/xai-tool-protocol/src/handshake.rs#L7)、[registration.rs](../grok-build/crates/common/xai-tool-protocol/src/registration.rs#L66)、[registry.rs](../grok-build/crates/common/xai-computer-hub-core/src/registry.rs#L168) permission classifier unavailable不会自动allow，而是转人工确认。[auto_mode.rs](../grok-build/crates/codegen/xai-grok-workspace/src/permission/auto_mode.rs#L20)
4. **[设计建议]** Pulsara因此把pre-dispatch决策从普通hook中剥离，并进一步做减法：V1只有`ToolDispatchAuthorizationPolicy`，decision为`Allow | Deny | RequireConfirmation`，machine deadline默认2秒/hard cap 5秒；unavailable转confirmation、无controller转deny。普通hook异常永远不能否定run或canonical commit，但policy没有Allow时physical dispatch必须fail closed。
5. **[设计建议]** V1 argument rewrite集合为空。已accepted assistant tool call需要改变参数时拒绝当前call并让provider产生new call；不复制Codex的completion-order rewrite，也不新增effective-input authority。
6. **[官方文档确认]** Claude Code与Codex都提供post-tool/lifecycle extension，但并未承诺所有callback跨进程必达；**[代码确认]** Codex HookStarted/Completed不进rollout durability，Grok-build普通hook registration也不构成durable consumer cursor。
7. **[设计建议]** Pulsara不为所有hook创建receipt graph，且V1第三方durable extension action为0。未来真正要求跨进程必达时，按一项产品一个ADR/schema migration新增具名job type及stable action definition/idempotency/retry contract，不能把process-local registration升级成generic tailer。
8. **[合理推断]** Claude Code/Codex/Grok-build的hook trust、scope、timeout或protocol negotiation各有成熟部分，但公开证据不足以确认逐payload capability、revocable process lease或live observer GAP是其统一协议承诺；这些可以成为Pulsara的差异化边界，前提是代码真正实现并有guard，而不是文档营销。

### 4.4 可靠结论

- **[代码确认]** Claude Code和Codex都有typed events；Codex甚至在同一个EventMsg vocabulary中同时容纳durable completed items与transient deltas/hooks。把“有typed event”与“event sourcing/execution replay”画等号不成立。
- **[官方文档确认]** 两者都持久化completed conversation/tool history并支持resume；两者也都有pre/post tool hooks、UI/automation extension surface。
- **[合理推断]** 两者公开边界更接近“completed history + transient live protocol + lifecycle hooks”，但各自仍有JSONL内部schema、hook并发/timeout、敏感input与crash窗口等trade-off；不能据此宣称它们没有durable audit或没有事件。
- **[设计建议]** Pulsara的潜在优势成立，但目前尚未实现：统一但分层的typed AgentEvent protocol，把durable committed facts、process-local live stream与capability-scoped hooks纳入同一语义体系，同时明确不把execution recovery建立在event replay之上。
- **[设计建议]** 这项优势必须以一组可测contract兑现：same-transaction canonical+event、closed Host/job append guard、exact typed subject FK、ephemeral stored-event/exact-subject observation projection、provider/control bounded GAP/detach、redacted capability projection、ordinary-hook registration-cut/no-catch-up、closed 49-type subscription-only extension、hook failure isolation、V1 generic reliable action为0及future reliable-action-as-named-job。缺任何关键项都不能把“统一协议”写成既成产品优势。

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
- rollback只能停机后再次complete reset并整体回退binary/schema；不保留用户数据reverse path。

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

冻结两个完全独立的conditional-mutation domain：`writer_generation`只保护turn、transcript、foreground tool attempt/result、prompt/queue admission、job enqueue/cancel authorization和session metadata；`claim_generation`只保护job attempt claim、progress、result、failure与lease settlement。background worker只写job/attempt-owned row/blob、automatic memory output及允许的occurrence，不直接追加session transcript。需要写session-scoped committed occurrence时，storage内部sealed appender只接受`HostWriterGuard | JobAttemptClaimGuard`：Host与worker仍分别校验自己的domain guard，但都先锁同一session event allocator并在canonical transaction内分配sequence。普通hook/plugin没有guard；没有immutable origin session的job不写session journal。当前Host要把job result公开给模型或用户时，必须以当前writer generation做一次显式accept transaction。Host takeover不改变已有job attempt claim generation，job reclaim也不改变Host writer generation。

**边界损失**

job completed与“结果已进入conversation”成为两个明确事实，可能有可见延迟；这是避免worker成为第二session writer所必须的产品边界，不需要receipt或reconciliation owner。

### P1-8：Protocol v3 snapshot需要一个线性化的canonical read cut

**目标方案中的缺口**

旧稿让snapshot同时返回suffix、event-journal retention边界、latest sequence与turn/control状态，却没有规定它们来自同一个PostgreSQL MVCC snapshot。多次独立query可形成high-water与rows互相矛盾的response，而notification又只是可丢hint。决策25进一步删除了event retention边界，但latest high-water与canonical rows仍必须来自同一cut。

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

canonical snapshot使用一个read-only `REPEATABLE READ` transaction：在同一MVCC cut中读取`latest_sequence`、`latest_event_sequence`和canonical session/turn/tool-attempt/queue/accepted-decision state，只返回`entry_sequence <= latest_sequence`的rows，并把`event_sequence_cut=latest_event_sequence`交给客户端。history page携带明确`cut_sequence`，并只返回该cut内的rows；canonical transcript与selective committed journal都没有prefix/age retention lower bound。operational spinner、transport、live process progress与pending interaction走独立endpoint/stream，不伪装成canonical snapshot成员。committed observer从`event_sequence_cut`之后level-read；Gateway在同一个bounded read cut中把minimal stored event与其exact typed-FK subject组合成`CommittedObservationProjection`，因此assistant entry无需复制正文到event：inline content立即可渲染，blob-backed content通过closed reference和唯一stateless read port确定性hydrate。committed suffix超budget、client-ahead或schema incompatibility时整体重新取snapshot，不返回半suffix；旧stored event仍按session lifetime保留。这里不引入root、durable observation/content projection、独立control revision、per-section cursor、transition history、fingerprint、checkpoint或durable read receipt/download lease。

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

Stage 1只删除success、catch-up、materialized、checkpoint high-water等semantic completion wait；同时停止这些subsystem的新admission，并在共享deadline内cancel/join所有仍使用session资源的physical task。逐call durable audit停写后不再创建新operation，已有operation只要求物理退出，不要求成功。直到Stage 3整个owner/executor被物理删除，对应close await才归零。超deadline后必须先隔离/终止其资源访问能力，再释放pool/store；不能通过后台abandon让task继续触碰session object。

**边界损失**

Stage 1的close await数不会立刻达到最终预算，极端I/O仍消耗bounded shutdown deadline；这是lifecycle safety，不是durability authority，也不需要stable candidate或repair owner。

### P1-11：Protocol v3必须可靠观察不推进transcript sequence的canonical transition

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

复用selective committed journal，不新增独立control cursor。每个决策7承诺的用户可见canonical transition由有权拥有该row的owner在同一transaction追加恰好一条对应typed `StoredCommittedEvent`；一个transaction接受多个transition时按各自类型追加并推进`sessions.latest_event_sequence`。Host-owned mutation校验`HostWriterGuard`；job/automatic-memory worker校验exact `JobAttemptClaimGuard`，两类owner共用session event allocator lock order。必须覆盖queue admission/consume/cancel/reject、accepted interaction decision、turn interrupted/completed、`tool_execution_attempts` insert，以及进入public attempt view的一次性remote-identity publication；message、tool result、job、memory与coordination acceptance只按决策7的exact 26类发event。普通hook/plugin无append authority。V1没有session lifecycle core event；session closing是current Host/process control，session row在detach后继续存在。纯CAS revision、context compiler中间态、background worker private progress、pending live interaction、spinner、transport或UI observation不发committed event。

canonical snapshot在同一read-only repeatable-read cut返回`latest_sequence`与`event_sequence_cut=latest_event_sequence`。客户端随后提交`after_event_sequence`；Gateway在一个bounded read cut中读取stored suffix及其exact typed-FK subject，并返回`CommittedObservationProjection`。client-ahead、schema incompatibility或suffix超event/byte预算返回GAP并要求fresh snapshot，不返回半suffix；这不删除stored event。LISTEN/NOTIFY或内存notification只负责提前唤醒，timeout与每次唤醒后都要level-read event high-water/observation。不得保存retention lower bound、独立control revision、durable observation projection、per-section revision、fingerprint、receipt、checkpoint或consumer ACK。

**边界损失**

每个有产品语义的transition会在既有canonical transaction中增加一个窄journal row并推进event high-water，带来可测的row与retention成本；它不增加transaction。换来的是可丢notification之上的level-triggered同步、typed audit和extension surface，而不是无语义的第二authority。

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

V1明确选择process-local语义：pending approval、plan question、plan exit和MCP input request属于当前Host的live control。`SessionLiveControlSnapshot`保存`owner_epoch + live_revision + current_interaction`，Opened/Replaced/Closed每次推进revision；`snapshot_and_subscribe()`在同一owner lock内冻结snapshot并登记更高revision observer。TUI notification只是hint，queue GAP/reconnect重新atomic snapshot-subscribe。canonical snapshot不包含pending request，只包含已经accepted的interaction decision或其command target。

resolution必须同时携带当前`writer_generation`、`expected_owner_epoch`、`expected_live_revision`、`live_interaction_id`与稳定`command_id`。Host在live owner lock内确认该request exact match，再在一个短数据库transaction中写`interaction_decisions`、执行session-wide command id幂等约束、以Host guard追加`InteractionDecisionAccepted`并推进`latest_event_sequence`；成功后清空live current value并发best-effort Closed。ACK unknown直接查询decision row。Host crash、writer takeover或close后旧epoch/request消失，running turn变interrupted；新Host从revision 0/empty开始，旧resolution因generation/epoch/revision/id不匹配而失败，不构造旧RuntimeSession、不恢复provider/tool/MCP continuation。

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

新增窄的`tool_execution_attempts`关系。每次实际invoke之前，当前Host使用writer generation提交stable `attempt_id`、parent assistant message/call reference、authorization subject/decision reference、actor、`dispatch_committed_at`、redacted argument digest，以及具体tool能预先生成的idempotency/launch key。storage确认attempt winner后才允许physical dispatch；commit ACK unknown只按attempt id读取canonical row，不建立candidate/receipt owner。若remote operation id只能在send后获得，只允许以`NULL -> exact value`的一次性conditional update安装，不能覆盖或充当dispatch-before证明。

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

### P1-17：所有大内容应共享一个blob publication与canonical read contract

**目标方案中的缺口**

删除queue artifact preparation hold后，prompt、tool result、job、context snapshot和memory仍可能引用大内容。若没有替代边界，canonical row可能引用尚未完成或已被GC的object；若各domain自行修补，则会再次长出五套hold/receipt/confirmation。publication只解决“canonical row能否安全引用bytes”，还没有回答TUI如何把blob-backed transcript entry恢复成exact可渲染内容。若Protocol只返回blob id/private URL，或要求Go复用tool-specific artifact API，access scope、range单位、codec与完整性都仍由实现者临场决定。

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

对canonical transcript另设一个**无状态只读端口**，而不是通用blob下载器：snapshot、history page与committed observation中的每个ordered content slot统一返回`InlineContent | CanonicalBlobReference`；只有后者可交给Gateway `ReadCanonicalContent(reference, offset_bytes, limit_bytes, request_context)`。reference是由exact immutable entry/block content edge导出的closed locator，不是bearer capability、object-store URL或任意`blob_id`查询键。每次bounded range read重新验证canonical subject、session/workspace、当前principal capability与reference中的digest/size/media type/codec；Go按byte offset组装，逐chunk和最终完整digest校验后才标记exact render完成。

这条read contract是canonical query的内容分页，不是event delivery或新的authority。它不写download receipt、lease、cursor、projection、repair row或`ContentDelivered` event；storage corruption只产生typed read error与`OperationalEvent`，不能反向否定原canonical commit。当前tool artifact reader可作为session-scoped bounded read的实现先例，但不能直接充当这个closed canonical content port。

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

V1正式锁定为**不承诺逐model-call exact context-input audit**。删除每call自动offer、plan/pages/root materialize/read-back、永久retention、repair/GC owner与业务完成型close drain；只保留`context_binding_revision_id + provider_input_through_sequence`作为canonical semantic attribution，以及redacted request hash、compiler version、token count等operational metadata。显式doctor/debug session或低比例采样可以产生短TTL、best-effort disposable diagnostic artifact，但缺失、写失败或过期均不是产品错误，不得阻塞provider dispatch、reply commit、conversation rehydrate或Host close。未来若出现逐call合规要求，必须以新的architecture decision重新定义pre-dispatch durability与敏感数据治理，不能在V1采样面上暗中升级承诺。

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

只保留已经具名、明确跨 Host 生命周期且承诺eventual completion的first-party job：compaction precompute与memory extraction/governance。V1第三方durable extension action为0，也不提供`reliable=true`、generic journal tailer或extension receipt/cursor。普通 reply、foreground tool loop、任何subagent execution、yielded terminal process/monitor、TUI projection和可同步查询的 evidence不进入 durable job system。未来若要增加跨进程必达extension action或“Host退出后继续的delegation”，必须作为独立产品以ADR/schema migration定义具名job type，而不是把普通hook或V1 subagent换名塞进通用job。

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

### P2-5：semantic context snapshot不能拥有改写canonical transcript的权力

**目标方案中的冲突**

context snapshot一旦被任一turn binding revision引用就是semantic derived authority，但它仍不拥有canonical conversation retention。若compaction能删除、重写或重排canonical transcript，它会同时控制provider语义与用户history边界，重新形成双重authority。

**风险链**

~~~text
缩短long-horizon context
  -> compaction snapshot
  -> compaction顺便删除/重写transcript
  -> immutable entry sequence失去referent
  -> reconnect cursor失效或history静默缺口
  -> snapshot同时成为解释旧history与provider context所必需
  -> semantic context与conversation retention耦合
~~~

**推荐**

V1与目标终局都只允许compaction追加completed context snapshot，记录immutable source sequence range/hash与生成contract；它不删除、不改写、不重排`transcript_entries`。context compiler可以在turn的binding revision引用后用exact snapshot替代旧entries进入provider request，但TUI/Inspector仍查询完整canonical transcript。所有accepted canonical entries及其canonical content在session存续期间保留；目标不实现prefix retention、age-based pruning，也不预埋`transcript_epoch`或`retained_from_sequence`。complete reset删除整个session universe；未来若产品要求整会话删除，必须另立feature/ADR与schema migration，不能借compaction实现。

**边界损失**

目标不会靠compaction或普通maintenance回收transcript row；长历史依赖分页、索引、inline/blob分层与可验证的无损物理压缩。unreferenced snapshot可删除，但被binding revision引用的snapshot必须随该revision保留；缺失时不能悄悄重新生成不同summary。

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
| Audit reproduction | 仅显式debug或采样的短TTL best-effort诊断；V1无逐call合规保证，不属于正常resume |
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
  -> one complete reset + production activation
  -> delete old EventLog execution graph
~~~

每个construction PR必须独立全绿，且dormant代码不能被普通Host配置、feature flag或session metadata激活。全过程不dual-write、不让同一session混用authority、不建立online translator。真正的coherent cut gate放在activation release，而不是用“单个PR”替代架构原子性。

### P2-8：Oxigraph mirror没有Agent-facing correctness价值

**当前代码真值**

生产memory recall已经由PostgreSQL完整承载：wiring构造`PostgresMemoryQuery`、`MemoryVectorQuery`与`GraphCandidateService`；lexical/FTS读取`memory_search_index`，dense读取PostgreSQL `memory_vector_index`，图扩展读取`memory_relations`并限制为现有bounded两跳：[wiring.py](src/pulsara_agent/runtime/wiring.py#L356)、[query.py](src/pulsara_agent/memory/canonical/query.py#L116)、[vector_query.py](src/pulsara_agent/memory/canonical/vector_query.py#L20)、[graph.py](src/pulsara_agent/memory/recall/graph.py#L63)。`DurableGraphFacade`的get/has/find/query也全部委托PostgreSQL；Oxigraph只由canonical mutation surface handler异步materialize，并被Inspector检查health：[durable_facade.py](src/pulsara_agent/graph/durable_facade.py#L34)、[surface_handlers.py](src/pulsara_agent/runtime/projection_jobs/surface_handlers.py#L188)、[service.py](src/pulsara_agent/inspector/service.py#L672)。

因此Oxigraph不是memory tool的读取真源。若Agent通过raw SPARQL查询它，positive result仍需回PostgreSQL hydrate/rebind，negative result又不能证明不存在，因为surface可能落后；这个freshness gap没有为当前`memory_search`、`memory_get`、`memory_explain`或两跳graph candidate带来产品收益，却要求required URL、surface delivery、worker、retry/dead-letter、migration和health contract长期存在。

**冻结结论**

目标架构完全删除Oxigraph，而不是把它降级为optional adapter：

- PostgreSQL `memory_facts`/`memory_relations`是canonical graph；图模型不要求图数据库作为物理authority；
- 保留当前lexical、FTS、pgvector、direct relation与bounded两跳recall，不在本hard cut扩展hop上限、recursive graph DSL或raw SPARQL tool；
- 删除`oxigraph_url`、`OxigraphGraphStore`、Oxigraph surface enum/plan/handler/worker、delivery state、Inspector health与对应tests/contracts；
- 不保留离线Inspector/analytics adapter、RDF export或可重新启用的production composition branch；未来若出现经过产品验证的新需求，作为全新能力重新立项，不以本次兼容代码为起点；
- JSON-LD/ontology类型只有在PostgreSQL canonical schema、序列化或现有typed memory语义仍实际使用时保留；不能仅为一个已删除的RDF mirror保留依赖。

**能力变化**

普通memory recall、memory tools、governance与现有两跳relation expansion必须行为等价。明确删除的是任意raw SPARQL、RDF endpoint和Oxigraph-specific arbitrary graph exploration；这些不是当前Agent recall的正确性承诺，也不由新的通用查询层替代。

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

### P3-2：event vocabulary的过度设计是durability混层，不是typed语义本身

**当前机制**

151类event混合产品事实、模型transport布局、projection lifecycle、accounting、debug attribution与UI foundation，并由同一个durable base/serializer/replay policy承载。当前问题是所有类型共享execution-recovery语义，而不是UI、Inspector或hook拥有typed lifecycle。

**原始需求**

完整replay、Inspector解释、确定性重建，以及TUI、eval和未来custom hook对稳定typed vocabulary的真实需求。

**删除后损失**

- 不再跨进程逐segment重放原流式动画；
- 某些精细timing/归因只在OperationalEvent或trace中保留；
- 旧Inspector页面需改为canonical status + selective history + current provider generation/session-control owner epoch；
- 如果把happy-path event压到零，还会错误损失审计、TUI delta、eval与extension hook能力。

**推荐**

把vocabulary预算拆开，而不是用单一总量上限奖励删类型：

- committed core product vocabulary保持窄小并接受架构审查，extension event必须namespaced/versioned；
- normalized且未coalesce的Text/Thinking/Data/ToolCall与ToolResult Start/Delta/End及session Interaction Opened/Replaced/Closed保留在独立LiveAgentEvent base；provider block、tool result与session control使用各自assembler/snapshot/owner；
- TTFT/retry/buffer/backpressure/cache进入OperationalEvent/trace；
- checkpoint attempt、repair attempt、delivery ACK与observer state删除；
- 三个平面都可以有typed schema，但只有CommittedAgentEvent进入selective durable journal，且都不参与foreground execution resume。

---

## 6. 三个候选目标架构

### 6.1 方案一：保守减法——universal durable EventLog core保留

**durable truth**

- 保留PostgreSQL EventLog作为conversation与execution authority；
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

- 从EventLog重建transcript并判断旧execution successor；
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
- committed/live/operational仍无法形成清晰extension contract，hook failure也容易继续绑定publication/confirmation。

### 6.2 方案二：中等 hard cut——Canonical relational conversation kernel with selective domain, effect, and work journals

**durable truth**

PostgreSQL直接保存：

1. sessions，含writer lease、commit-ordered entry sequence high-water与event sequence high-water；selective journal随session lifetime保留，不需要retention lower bound；
2. turns，含client command/submission identity；
3. transcript_entries及原子隶属于assistant message的有序text/tool-call blocks；每条accepted provider-generated assistant entry还保存exact context binding revision与该次pre-dispatch固定的`provider_input_through_sequence`；
4. unique-per-call tool_execution_attempts及exact tool results；
5. accepted interaction_decisions；pending request不落库；
6. durable_jobs + durable_job_attempts；
7. prompt_queue_items；
8. immutable context_snapshots + turn-local context binding revisions；unreferenced可GC，被revision引用后是semantic derived authority；
9. PostgreSQL memory_facts/memory_relations/governance lineage，以及可重建FTS/pgvector read models；
10. subagent_tasks/messages/results；这些是已接受的coordination facts，不是跨Host execution continuation；
11. purpose-neutral blobs与integrity metadata；
12. selective committed `agent_events`，只保存accepted product occurrence及其`event_sequence`、closed typed subject FK、versioned typed/redacted payload；它与对应canonical row同owner、同transaction写入。Gateway以read-time observation projection组合exact subject，数据库event不复制完整message/tool result。

不是每项都必须独立表；原则是每个 row表达一个产品事实，而不是某 reducer是否处理过。

Oxigraph不属于本方案：现有bounded两跳graph recall直接查询PostgreSQL relations；不保留required/optional RDF mirror、raw SPARQL或对应surface worker。provider/tool-result stream同样没有durable segment层；独立`LiveAgentEvent` base/bus承载normalized且未coalesce的Text/Thinking/Data/ToolCall与ToolResult Start/Delta/End，process-local assembler只在完整message或tool result结束时向canonical adapter交付frozen draft。session live-control在同一process-local typed family中另有`SessionLiveControlSnapshot`及Interaction Opened/Replaced/Closed，不与provider block assembler混用。

**crash semantics**

- running model/tool foreground turn在open时变 interrupted；
- 未accepted live event与assembler state丢弃；
- 不恢复旧 coroutine、transport、control disposition或finalization candidate。
- reopen只读canonical rows；committed journal可供audit/query，但不用于证明row或恢复execution。

**writer与authority切换**

- V1 每个 session 同时只允许一个 Host writer；DB generation/lease只fenceHost-owned foreground/session-control mutation；
- background job attempt claim/progress/result只由独立claim generation保护，worker不能直接写transcript；
- session event appender只接受Host writer或exact job-attempt claim guard；二者按统一allocator lock order追加各自有权拥有的occurrence，hook/plugin没有append authority；
- 当前Host以writer generation显式接受job result后，结果才进入conversation；
- observer attachment可以有多个，但 controller命令只通过当前 Host writer提交；
- 第一次production activation一次覆盖全部foreground item、最小rehydrate、context/Inspector、TUI Protocol major与minimal job kernel；所有foreground-reachable background capability已迁移或明确禁用，此前只允许dormant construction；
- 不按模型最终是否选择tool分流，不让同一session出现旧universal EventLog/new canonical rows双authority；selective `agent_events`与canonical row是同transaction的occurrence副产物，不是第二semantic authority。
- Protocol v3 canonical read使用repeatable-read cut；snapshot冻结entry/event cut，Observe消费stored-event + exact-subject形成的bounded projection；client mutation ACK unknown直接按canonical target row和session-wide command id恢复；
- pending interaction只属于当前Host live control；`owner_epoch/live_revision`与atomic snapshot-subscribe连接snapshot及Opened/Replaced/Closed event，同Host reconnect可level-read，Host crash/takeover后新epoch为空并令turn interrupted。

compaction只追加context snapshot/binding revision，不删除、重写或重排transcript；turn可在provider safe point追加revision以支持mid-turn budget recovery，每条accepted assistant message绑定exact revision与该次provider call固定的conversation cut。canonical transcript在session存续期间完整保留，没有prefix-retention state machine。

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
- TUI fresh attach先读canonical snapshot及同一MVCC cut的`event_sequence_cut`，再消费bounded committed observation与当前provider/tool-result/live-control owners；committed GAP触发fresh canonical snapshot，provider/tool-result content-live GAP重置相应partial renderer，control GAP重新atomic snapshot-subscribe。

**close**

3阶段：

1. stop ingress；
2. bounded cancel/join foreground及仍使用session资源的physical operation，写 interrupted/unknown；
3. flush Host-owned transcript/tool/queue/job-control authorization commits并关闭session资源；background worker/claim独立存活。

**migration**

- 冻结complete reset-only hard cut；
- 不做old universal EventLog → new canonical rows双写；selective `agent_events`从第一次activation起只与canonical owner共写；
- 不建 compatibility reducer；
- 允许以多个独立全绿PR构建production-disabled schema/repository、fresh-DB runner、readers与Protocol v3；只有最后一次complete reset + activation release可把新authority接入普通Host；
- 切换时清空全部Pulsara-owned PostgreSQL state、shared blob namespace与derived indexes；不导入旧session、memory、job、event或artifact，不保留cold-archive/compat读取路径。

**优点**

- 保留 Pulsara独有产品能力；
- 明确删除大多数 repair、checkpoint、confirmation和close graph；
- PostgreSQL事务仍提供强 canonical commit；
- side effect语义可理解、可审计。

**缺点**

- 是一次真实 hard cut；
- 第一个 foreground production activation 不能拆成 text/tool/resume/TUI 四次上线；但它们可以在此前用多个dormant construction PR协同建设和测试；
- 放弃已有execution event replay与精确内部transition历史，但保留selective occurrence audit、typed live stream和capability-scoped extension surface。

### 6.3 方案三：激进 transcript-first——append log/file为主

**durable truth**

- 每 session一个 append-only JSONL/SQLite transcript；
- user/assistant/tool/compaction/interrupted少量 record；
- durable jobs另用极小 SQLite/Postgres queue；
- memory/subagent结果作为 transcript或artifact。
- 同进程仍可提供typed live stream，但durable audit只能依附少量record，跨进程查询和多消费者extension能力弱于方案二。

**crash semantics**

与方案二相同，但不承诺数据库级多设备协调。

**tool side effect**

与方案二相同；本地 append前后边界。

**resume**

读文件/SQLite，截断最后损坏record，继续新 turn。

**close**

2阶段：cancel foreground；flush/fdatasync并停资源。

**migration**

complete reset-only；基本删除 PostgreSQL EventLog、projection jobs和大部分 schema。

**优点**

- 最小延迟和最少代码；
- 极易理解；
- 与本地 coding agent工作负载相符。

**缺点**

- 与 Pulsara已有PostgreSQL、多Host/后台能力和治理需求冲突最大；
- durable prompt queue、已接受的subagent协调事实、memory governance仍会迫使再引入数据库；subagent execution与terminal monitor本身分别按决策24、决策23只保留same-Host process-local能力；
- 很可能最终形成“JSONL transcript + PostgreSQL jobs/memory”双 authority；
- 实施与数据迁移风险最高。

### 6.4 评价矩阵

评分中 5 表示该维度最好；“exactly-once承诺强度”不按越强越好评分，而是列实际承诺。数量是目标架构的**预算与审查阈值**，不是 correctness gate，也不是第一阶段即可达到。不能通过把不相干类型塞进巨型 JSON row、合并无关业务类型或把代码搬到生成文件来“满足数字”。

| 维度 | 当前 | 保守减法 | 中等 hard cut | 激进 transcript-first |
|---|---:|---:|---:|---:|
| 正常 reply 延迟 | 2/5 | 3/5 | **5/5** | 5/5 |
| steady-state text turn durable transaction（无新compaction） | 至少15 write scope | 5–7 | **2** | 1–2 |
| steady-state one-tool durable boundary/write（无新compaction） | 至少31 | 10–14 | **5** | 4 |
| durable committed core vocabulary | 151类共用durable registry | 60–80 | **26类exact core；extension另行namespaced/versioned** | ≤12 record type |
| process-local live vocabulary | raw 7类，normalized 13类随后被转durable segment | 未明确分层 | **exact 23类LiveAgentEvent；独立RawProvider/逐delta draft为0** | 可保留typed stream，但无统一跨surface contract |
| TUI committed增量 | Presentation root/cursor + event tap | 继续依赖EventLog projection/reducer | **stored occurrence + exact subject的ephemeral bounded observation projection；无第二authority** | 直接tail transcript；control/audit能力弱 |
| committed append authority | 通用candidate/confirmation writer；session lock不区分domain | 仍以EventLog writer为中心 | **closed Host/job-attempt guard；统一session allocator；hook/plugin无authority** | 单local appender；跨worker协调弱 |
| ordinary hook delivery | callback与publisher语义混合，缺capability/lease/bound | 可能继续绑定publication result | **registration cut后best-effort；overflow GAP/detach；V1 generic可靠action为0** | process callback；future跨进程扩展需逐项具名job |
| steady-state text committed event row / extra transaction | 43 / 11 EventLog tx | 8–16 / 5–7 | **3 / 0额外tx（与2个canonical tx共写）** | 1–2 / 0–1 |
| steady-state one-tool committed event row / extra transaction | 83 / 23 EventLog tx | 16–28 / 10–14 | **7基线、remote identity时8 / 0额外tx（与5–6个canonical mutation tx共写）** | 3–5 / 0–1 |
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

代码删除量是基于当前owner文件和调用面的inventory target，不是未经实施即可保证的精确LOC。产品表与净删`≥22k` LOC是暴露架构回弹、触发审查和衡量方向的预算；committed core则是exact 26类结构gate。两者都不得靠删除live typing或合并无关payload取巧；correctness由单authority、same-transaction canonical+event、fencing、crash、side-effect、resume、observer isolation与reconnect行为gate决定。

### 6.5 推荐选择

只推荐 **方案二：中等 hard cut**。

原因不是它“最完整”，而是：

- 方案一删得不够深，保留universal EventLog会持续诱发新durable transition，并继续混淆typed protocol与execution recovery；
- 方案三对 Pulsara真实的后台 job、已接受的subagent协调事实、memory governance和多进程 PostgreSQL foundation删除过度，后续很可能再造第二 authority；
- 方案二把durability集中到canonical conversation、selective committed occurrence、physical attempt journal、semantic context snapshot和真正后台job，同时保留独立process-local typed stream、ephemeral observation projection与capability-scoped hook，正好覆盖产品价值高、跨进程不可丢或同进程值得扩展的事实；
- 它允许 PostgreSQL继续提供原子 commit、查询、并发和治理，又不要求所有 execution transition都成为 event-sourced transaction。

---

## 7. 推荐方案：中等 hard cut

### 7.1 架构原则

推荐架构的正式名称是 **Canonical relational conversation kernel with selective domain, effect, and work journals**。物理边界不是一个“大EventLog”，而是六个单向平面：

~~~text
canonical relational rows
  conversation / ordered semantic blocks / tool / job / memory / coordination

selective committed agent_events
  accepted occurrence / audit sequence / typed minimal payload / constrained subject FK

tool and job physical attempt journals
  dispatch ambiguity boundary / claim / remote identity / immutable outcome lineage

shared content-addressed blobs
  immutable bytes / canonical FK / orphan GC
  closed transcript content edge / stateless authenticated bounded range read

process-local live AgentEvent stream
  provider delta + Text|Thinking|ToolCall Start|Delta|End
  session live-control Opened|Replaced|Closed / owner epoch + live revision / live GAP

disposable derived planes
  indexes / presentation / telemetry / sampled diagnostic artifacts
~~~

依赖方向只能向下读取：

- Host-owned execution或job-attempt worker提交自己有权拥有的canonical row/physical attempt；同一owner在同一transaction内通过closed `EventAppendGuard`追加对应selective occurrence；
- canonical row拥有当前semantic truth与数据库约束；committed event只拥有“transition在sequence N被接受”的occurrence/audit truth；event不得证明row；
- Gateway/Inspector先读取canonical truth；Gateway把stored event与exact subject在一个bounded read cut中组合成无持久状态的`CommittedObservationProjection`，TUI不直接解释数据库event payload；
- snapshot/history/observation只返回inline content或从exact canonical edge派生的reference；Gateway逐请求鉴权后bounded读取blob，reference不授权、读取不产生durable state，也不在storage传输期间持有canonical read transaction；
- ordinary post-commit hook只从registration cut以后best-effort观察，不拥有journal catch-up；显式audit query/TUI observation使用各自独立contract，V1没有durable extension job或generic tailer；
- operational failure不能反向改变 canonical fact；
- durable event consumer、hook、TUI或Inspector失败不能反向否定canonical commit；
- background job只以自己的claim generation更新job/attempt-owned row或blob，不携带Host writer generation、不直接写session transcript；
- background worker可在同一job/attempt canonical transaction内用`JobAttemptClaimGuard`追加该session的job/memory occurrence；它不能借event append写conversation row，且没有immutable `origin_session_id`的global work不进入session event stream；
- job result进入conversation必须由当前Host以writer generation显式接受；
- 不建立“projection确认 canonical truth”“UI receipt确认 Runtime成功”之类反向边。
- reopen只rehydrate canonical rows；committed journal不参与execution replay，live stream在Host crash后自然消失。

### 7.2 冻结的 25 项决策

#### 决策 1：最小 durable truth

必须持久化：

1. accepted user input及其稳定`command_id`/`client_submission_id`，用于canonical-row级submit幂等；
2. accepted final assistant reply；
3. 已经向模型/用户公开的completed assistant tool-request message：stable message id、可公开text与全部有序calls；单call不是这个边界；
4. physical dispatch前提交的tool execution attempt：stable attempt id、unique call subject、authorization/actor、时间与可用remote/idempotency identity；每logical call最多一attempt；Runtime core不保存高后果tool分级；
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
16. session当前`latest_event_sequence`；它只由同transaction的selective accepted occurrence推进，不保存consumer position，也没有retention lower bound或prune transaction；
17. selective committed `agent_events`的typed minimal payload、closed subject-FK union、sensitivity projection与domain schema version，以及canonical closed payload、binding revision/context snapshot/compiler contract所需的version identity。canonical transcript与selective committed journal都在session存续期append-only保留。

不持久化“某个消费者已观察上述事实”的证明，除非该观察本身是用户承诺的后台工作。

#### 决策 2：明确改回 process-local 的状态

- vendor SDK stream item只存在于adapter调用栈，不形成Runtime protocol；经sanitizer/normalizer后的Text/Thinking/Data/ToolCall与ToolResult Start/Delta/End保留为typed `LiveAgentEvent`；transport batch layout、独立`RawProvider*`/semantic-draft中间层与coalescing segment不保留；
- ModelStart/ModelEnd attempt；
- ReplyStart/ReplyEnd attempt；
- control disposition与execution permit；
- provider-input generation coroutine；
- context compilation中间树、source page、live cursor；
- foreground tool future、suspension/terminal candidate owner；
- pending approval/plan/MCP input request、live interaction payload与其等待future；同Host通过`SessionLiveControlSnapshot`与typed `LiveControlEvent`查询/观察，跨Host不恢复；
- physical operation permit/reservation；
- rollout/token preflight reservation；
- reducer live high-water、post-fold receipt；
- checkpoint candidate/head/retry；
- publication/UI delivery state；
- Host close ordering state；
- temporary recovery session；
- child RuntimeSession teardown generation/retry task；
- process-local fingerprint、executor/coroutine attempt id和waiter identity；这里不包括durable tool/job physical attempt id。

model stream的唯一state owner是process-local bounded `ProviderStreamAssembler`，live observation owner是独立bounded `LiveAgentEventBus`，两者都不是durable segment subsystem。Start是不可变announce，不得被后续Delta原地补写；每个Delta只更新一个由block id定位的active assembler；End从assembler读取并携带最终frozen block、size/hash与ordinal。bus向TUI/Inspector/hook发布各自typed projection；observer overflow若可行则发`LiveGap`后detach，否则直接detach，绝不阻塞provider。assembler/bus不得import EventLog writer、`FrozenFactBase`、canonical event serialization或projection-job contract，不得产生stable segment candidate，也不得参与rehydrate、rematerialize或replay。

这里删除的是transport segmentation的durability，不是typed live lifecycle或completed semantic structure。accepted assistant message仍可包含ordered text block、tool call和产品明确保留的bounded structured-data block；这些block在完整message transaction中一次落库，其identity/ordinal来自completed draft，而不是继承provider delta ordinal、segment seal reason或transport batch布局。raw thinking与Thinking live events不持久化；对经session/workspace鉴权的当前用户，Runtime实际收到的thinking delta使用不摘要、不redact、不做内容截断的first-party live projection，而ordinary hook/plugin仍只得到redacted projection或经独立S2 lease批准的投影。若未来产品决定持久化completed thinking，必须作为单个completed semantic block另行审查，不能恢复delta durability。

#### 决策 3：Model stream crash后的唯一语义

**turn = interrupted；未 accepted delta全部丢弃；旧 model call永不跨进程继续。**

process crash、provider transport failure或Host takeover都会销毁当前`ProviderStreamAssembler`；open只观察running turn并提交coarse interruption，不尝试从partial text、tool argument prefix、operational UI frame或provider item id恢复assembler。只有完整`CompletedAssistantMessageDraft`成功进入assistant canonical transaction后，内容才从live状态跃迁为durable fact。

如果用户已经看过 live partial text，TUI在重连后显示：

> 上一次回复在生成中断；未完成内容没有保存。

可选地把partial content的redacted摘要送到operational crash log，但不得把raw thinking/tool arguments持久化，不得进入下次模型context、不得冒充assistant reply，也不得在reopen时合成历史Start/End。

如果crash发生在pending interaction期间，pending request随旧Host消失，turn同样变interrupted；accepted decision若已commit则保留，但既不恢复request，也不继续旧execution。

live-control owner也随Host销毁：旧`owner_epoch`立即失效，新Host使用新epoch、`live_revision=0`和空`current_interaction`启动。它不从canonical row、committed event或trace合成旧Opened/Closed event；唯一durable后果仍是turn interruption和已经accepted的decision。

#### 决策 4：tool产生 side effect、final reply未提交

对一个已完整commit的assistant tool-request message，逐call区分可证事实：

- 有 durable tool result：rehydrate时 conversation包含 call/attempt/result；turn标 interrupted；新 turn可基于 result继续生成 final reply，不重跑 tool。
- 有 durable call、没有attempt、没有result：证明Runtime没有跨过physical dispatch boundary；显示not_dispatched/interrupted，不得伪称side effect unknown。
- 有 durable attempt、没有result：显示 outcome_unknown；禁止自动重跑；用户或模型必须在新 turn显式选择 inspect/abandon，或以新call表达retry intent。
- closed pre-dispatch terminal result：允许无attempt，但reason只能是invalid_arguments / permission_denied / tool_unavailable / cancelled_before_dispatch，并精确绑定call与相关decision subject。

mixed/multi-call message必须作为完整message保留。已commit results、attempt-without-result与call-without-attempt可以并存；每个call只按自己的attempt/result事实解释，不能因为同batch另一个call成功就推断它已执行或未执行。无法从本地数据库证明外部 effect是否发生时，绝不自动写“failed”或“not executed”。

下一个新turn不得把上述悬空tool call原样发给provider。`ContextRematerializer`必须在provider lowering层使用唯一、确定性、versioned的`ProviderToolResultClosure`：已知result按原call ordinal精确降级；call无attempt时生成`interrupted_before_dispatch`；attempt无result时生成`interrupted_may_have_partially_executed`并绑定attempt id。provider adapter再把这个typed DTO降级成该provider要求的matching synthetic tool result（例如`aborted/interrupted`），保证tool call/result协议闭合。closure只属于本次compiled input，不是canonical `tool_results`、`CommittedAgentEvent`或`LiveAgentEvent`，不追加transcript row，不声称获得了外部返回值，也不授权自动retry。只有在原assistant message的全部calls按ordinal形成合法provider-visible closure后，才能附加新user/continuation item并开始新model call。

#### 决策 5：accepted final reply的唯一 commit point

assistant tool-request message拥有一个独立但非final的commit point：stable`assistant_message_id`、可公开text及全部tool calls/ordinals和`AssistantToolRequestAccepted` committed event在一个transaction中插入，turn保持running或waiting。任何invoke必须等storage adapter确认该完整message已经commit；不存在逐call先执行的入口，也不为commit confirmation建立durable FULL状态。

必须冻结的逻辑约束是：assistant message id在session内唯一；同parent的block ordinal与call ordinal各自唯一且immutable，call ordinals覆盖provider给出的固定顺序；`(assistant_message_id, tool_call_id)`唯一；terminal result以这个pair为外键且最多一个。parent与全部blocks/calls必须all-or-nothing可见。物理schema可以用child rows或严格typed bounded payload，但不能弱化这些约束。

每个实际invoke还拥有一个独立的physical-attempt commit point。attempt row在dispatch前写入并受`attempt_id`、call FK与writer generation约束；storage ACK unknown只能exact query该row。只有attempt commit被确认后Runtime才可调用tool adapter。这个row不是对remote receive的证明，也不需要后续confirmation state；它只标记“从此以后local crash无法证明effect未发生”的保守边界。tool result transaction引用exact attempt，或使用closed pre-dispatch terminal branch。

一个 PostgreSQL transaction：

1. INSERT assistant transcript entry，使用 stable entry_id和turn_id唯一约束；
2. UPDATE turns SET status = completed, final_entry_id = ...；
3. INSERT `AssistantMessageAccepted`及可选同transaction `TurnCompleted` committed events，分配连续`event_sequence`；
4. UPDATE session committed-event high-water；
5. commit。

该 transaction成功即 accepted。TUI notification、projection、checkpoint、RunEnd alias、final-output materialization都不是 commit条件。

如果 connection在 commit后断开、ack unknown，只允许 persistence adapter按 stable entry_id读取已经提交的唯一 canonical winner；这是唯一共享的 storage uncertainty处理，不创建 compatible-winner state或 domain-specific repair owner。

user acceptance使用对称但更简单的边界：client在retry时复用同一`command_id`，canonical command-addressable action/target row保存它，数据库在session范围执行`UNIQUE(session_id, command_id)`，并在同transaction追加`UserMessageAccepted`。turn或prompt queue item直接是该row，或引用同一canonical base row。相同command id和相同typed input返回已有turn/queue target；相同id但text、delivery mode或其他semantic input不同则返回conflict，不写第二个target。query直接读canonical row。`client_submission_id`可同时保留用于客户端本地submission identity，但不得再要求通用`terminal_command_receipts`、receipt revision、query token或confirmation state。

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

#### 决策 7：durable event减法与live/operational分层

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
- ProviderModelStreamErrorEvent；provider/transport失败只更新turn interruption/error summary与operational trace；
- durable Text/Thinking/Data Block Start/Segment/End及其coalescing persistence；对应live语义重塑为Start/Delta/End，不删除typed lifecycle；
- durable ToolCallStart/ArgumentsSegment/ToolCallEnd；对应tool call live lifecycle重塑为Start/Delta/End，partial arguments仍不可dispatch；
- PhysicalOperationReservationCreated/Settled/ChargeApplied；
- LedgerMaterializationAccountGenesis/ConsumerRegistered/HorizonAdvanced；
- per-turn SubagentGraphCheckpointCommitted；
- checkpoint/repair/publication attempt event；
- terminal presentation delivery event。

**合并：**

- RunStart + user transcript acceptance → turn/user transaction；
- RunEnd + ReplyEnd + final-output receipt → assistant/turn completion transaction；
- ToolCall live Start/Delta/End → 一个final frozen tool_call block，再随完整assistant message原子commit；
- physical tool execution admission/dispatch handoff → 一个dispatch前commit的tool_execution_attempt row；
- ToolResultStart + chunks + ToolResultEnd + terminal projection → 一个 tool_result entry，可引用global blob并精确引用attempt；
- rollout account/reservation/settlement → turn/model usage summary；
- terminal process start/progress/notification → Host-scoped process-local handle/monitor；只有已接受的completion/termination summary或conversation item进入canonical row + selective occurrence，不创建durable notification job。

**冻结的selective `CommittedAgentEvent` core vocabulary：26类。** 这26类由2.1.2的151类逐项生命周期审计和下列admission test得出，不再是可浮动的provisional审查带：transition必须已经被canonical owner接受、能exact引用同transaction中的canonical subject、其发生时间对用户/Inspector/audit/eval/post-commit hook有独立价值，并且即使完全禁止execution replay仍有意义。缺任一条件就只留row、进入Live/Operational plane或删除。

| family | final core type | accepted occurrence与closed payload边界 | exact subject | append authority |
|---|---|---|---|---|
| conversation | `UserMessageAccepted` | immutable user entry已接受；只带source/delivery等event-time枚举，不复制正文 | `subject_entry_id` | `HostWriterGuard` |
| conversation | `AssistantMessageAccepted` | 完整final assistant entry及ordered blocks已接受；不复制text/thinking | `subject_entry_id` | `HostWriterGuard` |
| conversation | `AssistantToolRequestAccepted` | 完整assistant tool-request entry及全部call/ordinal已原子接受；不复制arguments | `subject_entry_id` | `HostWriterGuard` |
| conversation | `ToolResultAccepted` | exact call的完整terminal result entry已接受；closed outcome/interaction-terminal reason可保留，正文/private URL只在subject/blob | `subject_entry_id` | `HostWriterGuard` |
| conversation | `TurnCompleted` | turn进入completed及final entry已接受 | `subject_turn_id` | `HostWriterGuard` |
| conversation | `TurnInterrupted` | turn进入interrupted及closed reason已接受；详细exception进Operational | `subject_turn_id` | `HostWriterGuard` |
| conversation | `UserSteerAccepted` | active turn中的steer entry已接受；不复制text | `subject_entry_id` | `HostWriterGuard` |
| policy | `CapabilityDecisionAccepted` | `ToolDispatchAuthorizationPolicy`的Allow/Deny或最终user confirmation已接受；不复制raw context、rules或arguments，policy不得rewrite call | `subject_interaction_decision_id` | `HostWriterGuard` |
| interaction | `InteractionDecisionAccepted` | approval、plan、MCP/external input等user decision已接受；secret只留不可逆commitment | `subject_interaction_decision_id` | `HostWriterGuard` |
| effect | `ToolAttemptAccepted` | dispatch前physical ambiguity boundary已接受；不复制arguments、authorization context或identity | `subject_tool_attempt_id` | `HostWriterGuard` |
| effect | `ToolRemoteIdentityPublished` | first and only remote identity publication已接受；ordinary projection不暴露raw identity | `subject_tool_attempt_id` | `HostWriterGuard` |
| queue | `PromptQueued` | canonical queue item已接受；正文只在queue content/blob | `subject_queue_item_id` | `HostWriterGuard` |
| queue | `PromptConsumed` | queue item被accepted new turn或steer消费；closed delivery kind | `subject_queue_item_id` | `HostWriterGuard` |
| queue | `PromptCancelled` | user/parent显式取消已接受；actor与closed reason保留 | `subject_queue_item_id` | `HostWriterGuard` |
| queue | `PromptRejected` | 系统在admission后拒绝delivery的terminal queue outcome已接受；不得伪装成user cancel | `subject_queue_item_id` | `HostWriterGuard` |
| context | `CompactionAdopted` | immutable snapshot已由新binding revision真正采用；summarizer完成本身不发A | `subject_context_binding_revision_id` | `HostWriterGuard` |
| coordination | `SubagentTaskAccepted` | Host接受task/objective及initial status；同tx的initial active/waiting不另发status event | `subject_subagent_task_id` | `HostWriterGuard` |
| coordination | `SubagentTaskStatusAccepted` | task后续进入closed `active/waiting_dependency/blocked_dependency_failed/completed/failed/cancelled/interrupted`状态；active不证明child process已启动 | `subject_subagent_task_id` | `HostWriterGuard` |
| coordination | `SubagentMessageAccepted` | 一条immutable task message child已接受；不复制正文 | `subject_subagent_message_id` | `HostWriterGuard` |
| coordination | `SubagentResultAccepted` | explicit或inferred immutable result child恰好接受一次；后续task completion不重复发 | `subject_subagent_result_id` | `HostWriterGuard` |
| work | `JobQueued` | 具名跨Host work intent及safety class已接受；同session的chain可由Host或当前job attempt创建 | `subject_job_id` | `HostWriterGuard`或`JobAttemptClaimGuard` |
| work | `JobAttemptAccepted` | exact attempt/claim在external work前已接受 | `subject_job_attempt_id` | `JobAttemptClaimGuard` |
| work | `JobTerminalAccepted` | job aggregate进入closed `completed/failed/cancelled/outcome_unknown`；中间retryable attempt terminal只留attempt row+C | `subject_job_id` | `JobAttemptClaimGuard` |
| memory | `MemoryFactAccepted` | governance接受canonical fact；不复制statement/evidence | `subject_memory_fact_id` | `JobAttemptClaimGuard` |
| memory | `MemoryFactLifecycleChanged` | closed `superseded/stale` transition；V1没有deleted/forgotten | `subject_memory_fact_id` | `JobAttemptClaimGuard` |
| memory | `MemoryRelationAccepted` | normalized relation恰好接受一次，closed kind包含`contradicts`；不为对称方向重复发event | `subject_memory_relation_id` | `JobAttemptClaimGuard` |

`PromptRejected`没有与`PromptCancelled`合并，因为前者是系统拒绝、后者是显式取消，actor、用户补救与hook语义不同。相反，job terminal、subagent task status和memory lifecycle使用closed enum，是因为它们分别属于同一aggregate transition family；这不是自由JSON或用一个`StateChanged`吞掉全域语义。`MemoryCandidateAccepted`不进入core：candidate row与对应`ToolResultAccepted`/`JobQueued`已经闭合durable intake，governance直接claim canonical candidate而不是tail event。中间job-attempt terminal也不发A；attempt journal保存lineage，只有aggregate terminal才是selective extension occurrence。

Text-only happy path固定为3条committed event：`UserMessageAccepted + AssistantMessageAccepted + TurnCompleted`。one-tool happy path固定基线为7条：user、assistant tool request、capability decision、tool attempt、tool result、final assistant、turn completed；若tool确实发布remote identity则为8条。它们都与canonical/attempt mutation同transaction写，event带来的额外PostgreSQL transaction为0。multi-call一轮的基线为`4 + 2N + E + R`：四条conversation边界、N条capability decision、N条result、E条actual attempt、R条remote identity，且`0 <= R <= E <= N`；正确性不能为压预算删event。

`ToolOutcomeUnknown`不进入registry：attempt存在、result缺失且turn interrupted时，`outcome_unknown`是canonical read-time derived observation，不是又一项由owner“接受”的产品事实；TUI bootstrap直接从attempt/result/turn读取它，committed delta只需观察`TurnInterrupted`，Gateway再hydrate exact subjects。它不写event、不推进`latest_event_sequence`，也不成为hook trigger或provider closure authority。

live vocabulary独立冻结为exact 23类：Text/Thinking/Data/ToolCall各Start/Delta/End十二类，`ToolResultStart/Delta/End`三类，`InteractionOpened/Replaced/Closed`三类，`TerminalProcessCompleted`与`TerminalMonitorOpened/Observation/Closed`四类，以及`SubagentProgress`一类。ToolResult Delta是closed text/data variant，End携带assembler冻结的final result view，但只有后续canonical transaction的`ToolResultAccepted`才表示durable接受。独立`RawProvider*`为0类；vendor SDK object不进入Runtime vocabulary，provider failure归typed terminal/usage结果或OperationalEvent。由此正式AgentEvent vocabulary为exact 49类（26 committed + 23 live），OperationalEvent topics另行治理且不进入这个数量。

#### 决策 8：删除哪些 projection/checkpoint

删除：

- transcript projection作为第二 authority；直接查询 transcript_entries；
- tool terminal projection；直接render tool call/result；
- model terminal projection artifact/reference；assistant reply本体就是 authority；
- provider-input generation projection；
- terminal presentation durable foundation；
- Oxigraph RDF materialization、canonical mutation Oxigraph surface delivery/target head/repair/dead-letter及其health projection；
- prompt queue checkpoint；
- subagent graph checkpoint；
- terminal notification/monitor reducer checkpoint；
- authority materialization shadow；
- per-reducer runtime projection checkpoint。

保留：

- immutable completed context snapshot；它不删除、覆盖、重排canonical transcript，未被binding revision引用时是可GC materialization，被引用后是semantic derived authority；
- 必要的PostgreSQL FTS/pgvector index maintenance state，但它只影响候选新鲜度；不存在Oxigraph checkpoint、cursor或mirror freshness contract；
- truly background job的lease/status，不称为 projection checkpoint。

只有rebuildable projection必须满足“删除整表后语义不变”。被binding revision引用的context snapshot不适用：重新生成可能改变summary语义，因此它必须通过revision与blob FK保留。它的创建失败不回滚旧reply；它的缺失也不能由另一个新summary冒充compatible winner。

V1明确禁用“compaction hard rewrite transcript”：completed snapshot记录source sequence range/hash与versioned generation contract，context compiler采用exact snapshot缩短provider input，但`transcript_entries`原序列永久不变。目标完整借鉴Codex的semantic-history原则——accepted canonical history只追加，compaction追加replacement/snapshot而不覆盖旧history，冷存储优化只能是无损表示转换——但不复制其JSONL + SQLite物理实现、rollout replay或exact replacement-history audit。Pulsara继续以PostgreSQL canonical rows为真源，并且没有`transcript_epoch`、`retained_from_sequence`或prefix-retention transaction。

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
- ephemeral stream assembly telemetry id；它不得覆盖content、进入canonical payload或成为restart join；
- checkpoint validation-base fingerprint；
- Host wiring/admission fingerprint，只要它不跨进程参与产品恢复。

#### 决策 10：哪些多状态机不可避免

只接受五类小durable状态族：

1. turn：running / completed / interrupted；
2. durable job aggregate：pending / active / succeeded / failed / cancelled / outcome_unknown；
3. durable job attempt：leased / terminal / outcome_unknown；
4. prompt queue item：pending / active / completed / interrupted / cancelled；
5. subagent task：pending / active / completed / failed / interrupted / cancelled。

subagent task的`pending/active`只表示当前Host已经接受且正在本进程调度/执行的coordination state，不是可被新worker claim的durable execution state。orderly close或下一Host takeover把所有旧Host遗留的nonterminal task置为`interrupted`；它没有attempt、lease、claim generation、retry lineage或`outcome_unknown`分支。child中的真实external effect仍按tool call/attempt/result语义表达，不能把不确定effect折叠进subagent aggregate status。

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
2. foreground model/tool与全部subagent activation/child `RuntimeSession`收到cancel并在共享deadline内退出；
3. 未完成turn与所有未terminal subagent task被Host-owned transaction标interrupted；call-without-attempt保持not_dispatched，attempt-without-result才投影为outcome_unknown；
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
2. 把旧 generation遗留的 running turn幂等置 interrupted，并在同transaction追加`TurnInterrupted` committed event、推进`latest_event_sequence`；
3. 把旧 generation遗留的pending/active subagent task幂等置 interrupted，并为每个用户可观察task在同transaction追加`SubagentTaskStatusAccepted(status=interrupted, reason=host_lost)`；已terminal task、已接受messages/results不变；
4. 保留完整已commit assistant tool-request message、tool execution attempts与已知results；call无attempt解释为not_dispatched，attempt无result才解释为outcome_unknown；
5. 丢弃旧Host的pending interaction request与全部child process-local execution，不恢复approval/plan/MCP continuation或subagent coroutine；
6. 之后所有Host-owned turn、transcript、foreground tool attempt/result、subagent task/message/result、prompt/queue、accepted interaction decision、job enqueue/cancel authorization与session metadata mutation都携带当前 writer_generation条件。

随后：

- 加载turn已exact采用的context snapshot，或为新turn选择最新eligible completed snapshot；绝不重新生成后冒充已采用snapshot；
- 加载其后的 transcript entries；
- 对interrupted turn中的每个未闭合tool call按决策4生成typed、versioned、provider-only `ProviderToolResultClosure`；不写入canonical result，不重新降级或dispatch原tool call；
- 加载 pending prompt queue与durable jobs；
- 加载subagent已接受的task/message/result用于history/Inspector；旧task若已被open transaction置interrupted，只能显式创建新task id重新委派，不能resume或retry原task；
- 不加载pending interaction request；同Host尚存时它只从live endpoint读取，Host takeover后不存在；
- TUI展示 interrupted/unknown；
- 只有新 turn才能调用模型或重试 tool。

observer-only attachment不获取 writer lease，也不改变 turn状态；旧 Host即使仍存活，其 generation上的任何 mutation都会被数据库拒绝。

background worker不属于rehydrate/open fencing domain：它只用job attempt的`claim_generation`提交progress/result/failure/lease settlement。Host takeover不应使合法worker result失败。worker也不得把result直接写进transcript；当前Host若要继续conversation，先读取immutable completed job/attempt result，再以当前writer generation创建显式accepted transcript item或新turn。accepted entry保存`source_job_id`并受session内唯一约束，或使用同等stable acceptance command id，确保accept ACK unknown不会把同一job result导入两次。

#### 决策 13：terminal monitor、subagent、compaction的最小边界

**terminal monitor**

- 服从决策23：`process_id`只是当前Host lease内的opaque process-local handle，不是launch token、OS PID或跨Host capability；
- monitor registration、stdout cursor、progress、spinner与notification scheduling都只在当前Host内存在，不创建`terminal_monitor` durable job/attempt，也不进入execution replay；
- process在Host存活期间完成时，只有已经由canonical owner接受的completion/termination summary或conversation item才跨进程保留；stdout大内容仍只能通过global blob contract随该canonical subject发布；
- orderly detach/close主动终止owned process group并有界drain；Host crash/takeover后不收养仍可能存在的OS process，旧handle失效，未有accepted completion的状态只解释为interrupted/outcome_unknown；
- 不得从durable event、historical process id、PID probe或旧monitor registration推断process仍活着，也不得重新launch原command。

**subagent**

- 服从决策24：V1全部child execution都绑定当前Host，没有foreground/background两种durability模式；
- durable边界只含已接受的task objective/profile、parent-child edge、message、result与terminal status；它们由当前Host writer提交，不进入`durable_jobs`/`durable_job_attempts`；
- child activation task、partial live output、child `RuntimeSession`、capacity reservation与MCP binding都只在当前进程；不保存coroutine、executor handle、claim/attempt、teardown generation、retry_wait或physical lease owner；
- orderly close有界cancel/join后把仍未terminal task标interrupted；Host crash/takeover由新Host open transaction完成同一状态收口，且不恢复或自动重派child；
- completed/failed/cancelled task与已接受message/result继续可查询；如果用户希望再做一次，必须创建新task identity。child tool effect仍由普通tool attempt/result审计，subagent status不承担effect recovery。

**compaction**

- 一次 transaction写immutable context replacement summary或blob reference、source transcript range/hash、snapshot schema、compiler/prompt/model contract；“replacement”只指provider context选择，不指改写canonical transcript；
- 只有 completed snapshot可供turn选择；snapshot source upper bound必须早于该turn user entry；provider dispatch前，turn以immutable binding revision采用exact snapshot/full-history base，并以base + post-source exact delta构造输入，同时冻结该次read cut的`provider_input_through_sequence`；同一revision可跨tool-loop model calls复用，但预算压力可在safe point创建新snapshot/revision并推进turn current pointer；每条accepted assistant message精确引用生成它的revision并原样归因该次cut；
- unreferenced snapshot超过retention可GC；被binding revision引用的snapshot受FK保护，不能删除、覆盖或重新生成替换；
- failed attempt只进operational log；
- memory extraction可作为独立 durable job；
- 以 immutable source range/hash为输入、按唯一snapshot id提交的compaction/memory extraction可声明为retry-safe；
- compaction永不删除、重写或重排source transcript entry；
- accepted canonical transcript在session存续期间全部保留；V1与目标schema都不实现或预埋prefix retention；
- snapshot生成/写入失败不追溯阻止已有reply、turn completion或close；若没有previous snapshot/full transcript能满足provider token budget，则新的provider dispatch以typed target-infeasible停止。

#### 决策 14：PostgreSQL authority与V1 single-writer fencing

PostgreSQL继续是 authority，但从 universal EventLog改为直接 transcript/job schema。

这一决定同时冻结memory physical store：PostgreSQL是唯一canonical memory graph与唯一Agent-facing memory read store。`memory_facts`/`memory_relations`保存accepted node、lifecycle与asserted edge；FTS和pgvector是同一数据库内可重建的candidate read model；`memory_search`、`memory_get`、`memory_explain`及`GraphCandidateService`继续使用现有typed SQL路径和bounded最多两跳扩展。V1不增加recursive traversal、raw SPARQL、通用graph-query DSL或第二个graph service。

Oxigraph从目标架构完整删除。它不是optional derived surface：production composition不得构造`OxigraphGraphStore`，settings不得要求或接受`oxigraph_url`，canonical memory transaction不得计划Oxigraph delivery，worker/Inspector/doctor不得连接Oxigraph，仓库不得保留可重新启用的adapter、RDF endpoint或兼容contract。PostgreSQL中的node/edge关系已经构成canonical graph；删除的是RDF/SPARQL物理引擎，不是memory graph数据模型或现有两跳召回能力。

这也意味着memory write不再需要跨storage publication语义。governance在一个PostgreSQL transaction中提交fact/relation/lifecycle与必要index work intent后即成功；FTS/pgvector维护失败只能使对应candidate channel stale/degraded，不能回滚canonical fact。不存在“PostgreSQL FULL、Oxigraph pending”的产品状态，也不存在mirror revision、rebind、negative-result freshness或全量RDF rebuild owner。

理由：

- Pulsara需要 durable prompt queue、background job、subagent coordination、memory governance和多进程查询；
- 这些需求本来就需要数据库；
- 删除的是“一切必须先成为 AgentEvent”的抽象，不是数据库事务；
- direct schema让唯一性、外键、状态查询和清理策略更直接。
- 当前recall、governance与memory tools已经从PostgreSQL读取；保留Oxigraph只会引入第二物理面与无消费者的freshness gap。

V1 同时冻结以下并发约束：

1. 每个 session 在任一时刻只有一个可写 Host；
2. 可以有多个 observer attachment，但只有 controller命令通过当前 Host进入 mutation path；
3. `sessions` 保存 `writer_generation`、`writer_lease_owner_id`、`writer_lease_expires_at`及entry/event high-water；selective journal随session lifetime全量保留，没有retention lower bound；
4. acquire/takeover在一个 PostgreSQL transaction中校验lease并递增generation；
5. turn、transcript、foreground tool attempt/result、prompt/queue/accepted interaction decision、job enqueue/cancel authorization和可写session metadata的mutation都校验当前generation；
6. lease换代后旧writer在上述Host-owned domain的commit全部失败，不做兼容winner或跨writer reconciliation；
7. lease renewal只是当前Host中的process-local heartbeat；renew失败立即停止新mutation并中断foreground，不创建durable lease-repair owner；
8. close只需停止当前writer、完成有界foreground终止并释放/等待lease过期，不需要证明所有observer已关闭。

`event_sequence`不构成第三个fencing domain。它只排序同transaction已接受的用户可观察occurrence；它不授权mutation、不保护background worker，也不保存consumer observation。canonical row继续拥有current truth，observation budget/schema GAP不能改变row或删除stored event。single writer使session内event sequence分配无需额外candidate/CAS/reconciliation owner。

两个fencing domain必须在schema port和SQL predicate层完全分开：

| domain | generation来源 | 允许保护的mutation | 明确不得保护 |
|---|---|---|---|
| session writer | `sessions.writer_generation` | turn/transcript、foreground tool attempt/result、prompt/accepted interaction decision、job enqueue/cancel request、job result acceptance | worker claim/progress/result/failure/lease settlement |
| background job | `durable_job_attempts.claim_generation` | attempt claim、progress、immutable result/error、failure、lease settlement | session transcript、turn completion、prompt admission、session metadata |

job row可以记录`created_by_writer_generation`作为审计，但worker commit predicate不得检查它。Host cancel只写`cancel_requested`授权事实；实际cancel/failure/settlement由current attempt + claim generation提交。completed job result进入conversation是一个新的Host-owned accept transaction，而不是worker transaction的副作用。

因此V1的可实施fencing边界只有两个：每session单一Host writer拥有conversation/session-control mutation，background worker只拥有job/attempt progress与immutable result；两种generation彼此独立。worker不能直接把结果写入conversation，只有当前Host以current writer generation显式接受后，结果才成为canonical conversation fact。

当前基线的`sessions`表只有id、workspace root、created time与metadata，并没有Host writer lease/generation列：[0002_runtime_truth_baseline.sql](src/pulsara_agent/storage/migrations/sql/0002_runtime_truth_baseline.sql#L77)。因此这里是推荐目标schema中的最小新增fencing事实，不是对现有能力的误读；也不应为它再造独立candidate/receipt表族。

当前 terminal protocol 已有 attachment role与 `controller_generation`，并允许 command携带 expected controller generation：[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L292)、[terminal_client.proto](src/pulsara_agent/terminal_protocol/schema/terminal_client.proto#L1486)。它约束的是连接/应用层 controller takeover，不等同于这里新增的数据库 Host `writer_generation`。目标实现可以在命令入口校验二者，但不能把现有 controller generation误当成 durable writer fencing。

#### 决策 15：接受complete reset-only hard cut

**接受，并推荐。**

- 不做双写；
- 不做旧 EventLog到新 transcript的在线 compatibility reducer；
- 不允许旧/new owner同时拥有一个 turn；
- 开发/当前部署在切换点完全reset：清空Pulsara-owned PostgreSQL schema/data、shared content-addressed blob namespace与全部derived indexes/presentation state，从空库运行新migration并创建新authority；
- 旧session、transcript、memory、queue、job/attempt、event、audit artifact与pending/running状态全部不导入、不冷归档供产品读取，也不提供old-data export/import、offline converter、compat CLI或historical decoder；
- reset不是撤销外部side effect。drop旧authority前必须stop admission并quiesce/fence全部旧Host、worker、terminal monitor与subagent executor；已存在的remote process/effect只按cutover runbook终止或移交operator，绝不由新Runtime导入、查询后自动继续或重新dispatch；
- rollback不承诺恢复cutover前数据；回退旧binary/schema只能再次complete reset，不建立reverse projection或保留旧DB读取通道。

#### 决策 16：防止 external side effect静默重复

1. completed assistant tool-request message的text与全部ordered calls原子提交后，才允许其中任何call invoke；
2. assistant_message_id与call_id表达provider intent；同message保存固定call ordinal，physical retry不得覆盖或改写call；
3. 每次实际invoke前创建stable attempt_id并commit `tool_execution_attempts`；每logical call最多一个foreground attempt，每个result精确引用assistant_message_id + call_id + attempt_id，并有唯一model-visible terminal row；
4. 同一message的全部calls都有success/error/denied/cancelled terminal row后，才进入下一 model call；provider lowering按call ordinal，不按result完成顺序；
5. call无attempt在rehydrate后显示not_dispatched/interrupted；attempt无result才显示outcome_unknown；
6. Runtime永不自动重试 outcome_unknown；
7. 显式 retry必须创建新turn中的新call id及该call的唯一新attempt id，并在新attempt上记录retry_of_attempt_id、actor、reason；不得为旧call创建第二attempt，旧attempt/result lineage不变；
8. Runtime core不建立“高后果/低后果”、read-only或retry-safe的effect-recovery taxonomy；permission、sandbox与并发控制仍可由typed policy/tool descriptor决定，但不能改变attempt/result/crash contract；
9. 支持remote idempotency key/status lookup的具体tool可以在后续显式call中正常使用，但Runtime不因descriptor自动inspection、status lookup或retry；
10. UI统一显示完整assistant message、每个命令、目标、ordinal、时间、已知结果，以及由canonical attempt/result/turn确定性推导的`not_dispatched`或`outcome_unknown`；unknown统一提示“执行被中断，操作可能已经部分完成”，不提供按tool风险等级变化的默认按钮；
11. provider-only synthetic closure只保证下一次model input合法，不能反写canonical result、证明外部操作未发生或触发重试；
12. attempt commit ACK unknown只exact query该attempt row；不创建dispatch receipt、confirmation或reconciliation owner。
13. interrupted attempt的late exact outcome只能在result尚不存在时由current writer追加该call唯一result；旧turn保持interrupted。是否可能参与某条历史assistant的input，只能比较`result.entry_sequence`与该assistant已冻结的`provider_input_through_sequence`，不能使用assistant自身sequence或binding revision推断。若result晚于已有assistant cut，future lowering按result实际sequence生成late-effect observation，不能倒插改写历史provider context。

#### 决策 17：durable、rehydrate与replay的词义

V1只承诺：

- **conversation rehydrate**：恢复全部accepted canonical conversation facts；
- **context rematerialization**：按versioned compiler、conversation facts与exact turn binding revision/context snapshot构造新的provider input；不承诺历史request逐字复现；
- **effect reconciliation**：查询tool/job attempt、remote identity与result，不默认重新执行；
- **audit reproduction**：V1只在显式debug或采样中best-effort、短TTL提供，不承诺逐call合规证据；
- **execution replay**：明确不支持。

canonical relation与closed polymorphic payload必须有domain schema version。兼容演进只允许SQL migration rewrite或有限per-domain upcaster；未知版本fail closed。禁止重新建立universal historical event decoder，也禁止把process-local trace当成rehydrate输入。

#### 决策 18：全局blob publication、canonical read与retention

所有大正文共用一个content-addressed `blobs` owner。publisher计算canonical digest/size/codec并完成immutable write；canonical domain transaction验证blob row并安装普通外键。所有domain reference均`ON DELETE RESTRICT`，不存在queue/tool/job/context/memory专属hold、receipt或confirmation。

同库小内容优先与canonical row同transaction写入。需要预写的大内容允许先形成unreferenced blob；V1在24小时orphan grace后才允许GC。GC只选择当前无FK引用的blob，并以数据库约束作为最终竞态裁决。blob write失败只终止尚未提交的对应canonical mutation，绝不回滚其他已经accepted的conversation fact。

Protocol-facing transcript content使用closed `ObservationContent` union。inline与blob branch都声明相同的canonical logical-byte digest、size、media type与semantic codec；内部压缩/加密只是storage实现，不改变wire bytes。`CanonicalBlobReference`还绑定session、workspace、exact entry、closed content slot/可选block ordinal与immutable blob identity，但它不是授权凭据，也不暴露storage URL。

Gateway只提供bounded、stateless `ReadCanonicalContent`。每次调用在一个短read-only transaction中重新鉴权并重读exact canonical content edge，验证reference descriptor完全相等后结束transaction，再执行有hard cap的storage byte-range read；不得在传输正文时持有MVCC transaction或session lock。FK `RESTRICT`与content edge immutable使后续range call无需同一observation MVCC cut、durable lease或下载cursor。每个chunk携带offset/returned bytes/next offset/EOF、chunk digest和完整content descriptor；客户端按offset组装并校验完整digest。任一请求越权、跨session/workspace或reference stale时不暴露对象是否存在；已授权但bytes missing/digest mismatch时返回明确integrity error并记录operational diagnostic，不自动修复或改写canonical row。

#### 决策 19：V1不承诺逐model-call exact context-input audit

V1不保存、也不向用户、operator或合规consumer承诺“每个physical provider dispatch都有可逐byte复现的历史compiled input”。conversation继续能力只依赖canonical transcript、turn/context binding revisions、被引用的immutable context snapshots、tool/effect facts与versioned compiler contract；`context_binding_revision_id + provider_input_through_sequence`证明semantic base与conversation cut，但不冒充历史provider request副本。detach/reattach、conversation rehydrate与后续新turn的context rematerialization不读取audit artifact。

因此目标删除每model call自动plan/pages/root materialization、read-back confirmation、永久retention、audit expectation event、repair/GC owner与业务完成型close drain。允许的诊断只包括显式doctor/debug session或低比例采样产生的短TTL、best-effort disposable artifact，以及redacted request hash、compiler version、token count等operational metadata；捕获缺失、失败、过期或被采样掉不得阻塞provider dispatch、reply commit、turn完成、rehydrate或Host close，也不得改变canonical truth。

这项决定同时关闭“强保证但不允许pre-dispatch gate”的矛盾：V1没有逐call强保证，所以不存在audit-before-dispatch barrier。未来若法规要求对每个已dispatch call留存exact evidence，必须新建architecture decision，明确canonical encoding、敏感字段、retention/export/delete、pre-dispatch fail-open/fail-closed与storage预算；不得把V1 best-effort采样面静默升级为recovery authority或合规journal。

#### 决策 20：V1用户live可见性与bounded sensitive projection

V1把“人类用户正在看当前session”与“代码extension订阅event”定义为两种不同projection profile，不让plugin借用用户的可见权限：

1. 通过session/workspace鉴权的first-party TUI用户可见Runtime从provider实际收到的raw thinking content/Delta；内容不摘要、不redact、不按长度截断。provider adapter不保证逐token边界，因此协议承诺的是原样delta与顺序，不是伪造“每event等于一token”。
2. tool arguments的first-party用户展示使用closed union：

~~~text
UserToolArgumentsProjection =
    CompleteToolArguments(json_utf8, total_utf8_bytes, digest)
  | TruncatedToolArguments(prefix_utf8, visible_utf8_bytes,
                           total_utf8_bytes, digest)
~~~

在展示阈值内必须完整显示；超限必须在UTF-8边界截断并显式标记遗漏大小，不得把prefix标成完整JSON。stream中可递增bounded prefix，但只有ToolCall End能给出最终`total_utf8_bytes + digest`。这是display-only projection；assembler、completed canonical tool call、schema validation和physical dispatch始终使用完整arguments，绝不使用截断值。
3. 这两种用户可见性由Host authorization service根据当前authenticated attachment、session/workspace scope与交互权限签发server-minted、不可转授的view profile；plugin不能自己声明或继承它。ordinary hook仍只看typed/redacted projection；raw thinking仅可另行授给first-party Inspector/debug registration的短期session lease，未redacted tool arguments使用独立S2 capability且仍受单item/queue byte hard cap。
4. private URL只投影给当前controller的dedicated interaction surface，不进入ordinary hook、recorder或通用history。`McpSecret`、OAuth token、cookie、Authorization header、URL credential/query/fragment等S3 carrier永不构造为user/hook event payload；S3不存在可授予capability。
5. content-live observer queue、shared live ring、provider/tool-result snapshot、control snapshot与control observer queue都必须同时具有event/byte hard cap。overflow若尚能写入一个GAP，则发`LiveGap`/`LiveControlGap`后detach；否则直接detach。provider、tool executor与live-control owner不等空位、不写durable overflow证明。same-Host reconnect只返回当前retained bounded snapshot；晚attach、GAP、detach或Host crash可以丢失早先thinking/tool-result delta，这不改变“已投影内容按契约可见”，也不产生跨进程best-effort replay。
6. close先停止新registration/delivery，丢弃未开始callback，只在有界全局drain budget内等待已开始callback；超时cancel/detach。确切event/byte阈值、tool argument展示阈值、callback deadline与close drain毫秒数允许由dormant implementation和负载探针校准，但Stage 2 activation前必须有具名default、server hard cap与monitor；这些数值不再是架构open question。

#### 决策 21：现有memory proposal先durable candidate、governance异步

V1只重塑当前已经存在的五类`remember_*` proposal，不借架构迁移新增memory feature。代码真值只有Claim、Preference、Observation、ActionBoundary与Decision candidate，以及skip、submit、correct、merge、supersede、contradict治理决策；没有Agent-facing delete/forget tool、delete candidate、delete governance decision或可执行delete lifecycle。`NodeStatus.DELETED`只是没有production writer的dormant vocabulary，不进入目标contract。`supersede`必须有replacement，`mark_stale`只是既有lifecycle primitive；二者都不得被文档、Protocol或UI改称“删除”。[memory.py](src/pulsara_agent/tools/builtins/memory.py#L1)、[memory_candidate.py](src/pulsara_agent/primitives/memory_candidate.py#L44)、[pool.py](src/pulsara_agent/memory/candidates/pool.py#L119)、[lifecycle.py](src/pulsara_agent/memory/canonical/lifecycle.py#L30)、[memory.py](src/pulsara_agent/ontology/memory.py#L92)

memory proposal与canonical memory acceptance是两个明确边界：

1. `remember_*`只接受typed proposal，不直接写`memory_facts`，成功结果只能表达`proposed`，不能表达“已经记住”或“已经可被recall”；
2. 对外承诺`proposed`的durable acceptance point是`memory_candidates` row commit。tool worker可以先写process-local `MemoryProposalSink`作为thread/agent-loop handoff，但sink deposit不是durable acceptance；在接受对应tool result时，Host必须在同一个短PostgreSQL transaction中把candidate row与tool result一起提交，或让整个transaction失败为typed tool error，不能先承诺`proposed`再依赖best-effort drain。当前sink明确只是等待agent-loop drain的process-local staging，目标在这里收紧其ACK语义，不把现状误称为已经原子；[proposal_sink.py](src/pulsara_agent/memory/candidates/proposal_sink.py#L1)
3. automatic extraction同样先由其durable job attempt提交typed candidate row；任何governance worker只能claim已经commit的candidate，不能直接从transcript、live event、operational trace或process-local sink旁路生成canonical memory；
4. governance始终异步。foreground provider loop、assistant reply commit、turn completion与Host close不等待governance model call、canonical memory acceptance、FTS/pgvector freshness或Inspector/hook delivery；这里唯一同步工作是复用现有tool-result transaction完成有界candidate append，不新增一轮foreground transaction；
5. governance由显式durable job + `JobAttemptClaimGuard`拥有。accepted decision在一个PostgreSQL transaction中写closed governance decision、canonical memory fact/relation/lifecycle及对应selective committed occurrence；skip只终结candidate，不生成memory fact；event不证明candidate或memory row存在，也不恢复governance execution；
6. reopen与worker restart按canonical query读取pending candidate/job claim，不replay`MEMORY_CANDIDATE_PROPOSED`或batch progress event。claim、batch-prepared、projection-ready、index-refresh与delivery ACK没有独立产品语义时删除或降为OperationalEvent；真正需要长期解释的proposal source、terminal governance decision、accepted memory与supersede/contradiction lineage留在relational rows；
7. canonical fact transaction成功后，exact fact与direct relation query立即可见；FTS/pgvector允许短暂stale/degraded并异步追平，失败不回滚fact。UI可以把candidate显示为“待治理”，但不得在decision commit前把它混入normal recall或渲染成accepted memory。

Stage 2只冻结上述顺序、transaction owner和failure semantics；candidate/governance SLA、batch size、claim lease与index lag阈值是activation前校准的具名运行参数。V1明确没有`DeleteCandidate`、`ForgetDecision`、`MemoryDeleted` event、pending-delete quarantine或用户删除工具；如果未来需要“忘记”，必须作为独立产品/隐私设计重新定义，而不是从dormant enum或物理SQL删除推导能力。

#### 决策 22：Codex式append-only canonical transcript，不做prefix retention

目标完整借鉴Codex的**semantic transcript retention contract**，而不是照搬其物理存储。Codex公开源码基线6138909d6ec58b2fbe635ef973e02caecad5a5aa把`ThreadStore::append_items`定义为canonical history append API，resume读取完整rollout，compaction追加携带replacement history的`CompactedItem`而不重写旧items，冷rollout只做verified zstd表示转换，archive移动文件且delete是显式操作；raw response、Start与content/reasoning delta仍是transient。[README.md](../codex/codex-rs/thread-store/README.md#L9)、[recorder.rs](../codex/codex-rs/rollout/src/recorder.rs#L933)、[session/mod.rs](../codex/codex-rs/core/src/session/mod.rs#L3030)、[compression.rs](../codex/codex-rs/rollout/src/compression.rs#L600)、[policy.rs](../codex/codex-rs/rollout/src/policy.rs#L117) 官方命令同样把resume/fork/archive与显式永久delete作为不同操作，但没有给local transcript无限期retention的独立SLO。[Developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli)

“借鉴Codex”在Pulsara中冻结为：所有accepted user/assistant/tool-result entries、ordered semantic blocks及其canonical inline/blob content在session存续期间append-only保留；selective committed events采用同一session lifetime。detach/reattach、TUI bounded suffix、context compaction与storage pressure均不得删除、覆盖或重排这些事实。compaction只追加derived context snapshot与binding revision；provider后续看到summary不改变TUI/Inspector可查询完整history。允许在digest/size/codec不变且读取仍exact的前提下对blob或冷row做无损物理压缩/迁移，但这不形成新semantic owner、archive projection或repair state machine。

因此目标删除`transcript_epoch`、`retained_from_sequence`、`retained_from_event_sequence`、history/event-retention GAP及其prune transaction；history cursor只携带`session_id + cut_sequence + entry_sequence`，audit query只携带固定event cut与event sequence。complete reset仍按决策15删除整个数据库，不需要epoch；session identity不得复用。V1不新增archive、整会话delete或合规retention feature；未来若确有整会话删除需求，必须单独冻结用户语义、legal hold、event/blob顺序与审计策略并新增schema migration，不能以dormant列保留半套能力。

这个“semantic transcript无损”不扩张为execution无损：LiveAgentEvent、OperationalEvent和旧coroutine state不持久化；决策19仍不承诺逐model-call exact compiled input。Claude Code也证明两者必须分开：其local JSONL在单个session内按message/tool/result追加，但官方默认30天后按整份session文件清理，compaction还可让model-effective context改用summary；这不能被表述为永久或execution-level无损。[Manage sessions](https://code.claude.com/docs/en/sessions)、[Application data](https://code.claude.com/docs/en/claude-directory)

#### 决策 23：不承诺yielded terminal process跨Host重绑

这是closed subtraction boundary，不是等待terminal supervisor选型。当前代码生成的`process_id`只是`proc_<uuid>`，physical owner是进程内`ProcessRegistry`保存的`subprocess.Popen`、PTY/pipe、reader thread与output journal；product tool path每次lookup都携带`owner_host_session_id`，跨HostSession访问稳定失败。[process.py](src/pulsara_agent/runtime/terminal/process.py#L172)、[process.py](src/pulsara_agent/runtime/terminal/process.py#L591)、[process.py](src/pulsara_agent/runtime/terminal/process.py#L700) Host的workspace supervisor虽然在同一`HostCore`进程内共享manager，但lease authorization principal仍是Host session id；`detach_session()`进入close，lease release调用`release_owner()`杀死该owner的yielded processes并删除其terminal sessions/cwd。[core.py](src/pulsara_agent/host/core.py#L822)、[core.py](src/pulsara_agent/host/core.py#L1164)、[manager.py](src/pulsara_agent/runtime/terminal/manager.py#L286)

V1冻结以下lifetime contract：

1. 同一存活Host owner可以用opaque `process_id`跨tool call执行`poll/wait/log/write/close_stdin/kill`；handle不得被解释为portable identity、capability或canonical process row；
2. orderly detach/close先stop terminal ingress，再向全部owned process group发送termination并在共享close deadline内bounded wait/join、冻结可得terminal outcome/输出、释放pipe/PTY和process-local monitor，最后才释放session资源；close成功后的产品语义是不再有该Host拥有的可控运行process；
3. abrupt Host crash/takeover不承诺Runtime已经杀掉OS child。`start_new_session=True`意味着process可能仍作为orphan存在，但新Host不得按PID、历史`process_id`、output spool或event重新收养、观察、写入或kill它；旧turn/handle按canonical规则成为interrupted/outcome_unknown，外部orphan侦测/处置由部署reaper承担并以OperationalEvent诊断，不能成为conversation correctness gate；
4. terminal monitor、TUI attachment与connection `rebind`都是process-local。restart recovery只能读取已经accepted的canonical completion/termination；没有accepted outcome时明确终止旧monitor语义，不创建durable monitor job、launch-token row、receipt、checkpoint或repair graph；当前代码也显式拒绝仅因PID可达而adopt OS process，并以`interrupted_by_host_restart`关闭无completion的monitor。[monitor.py](src/pulsara_agent/runtime/terminal/monitor.py#L1586)、[monitor.py](src/pulsara_agent/runtime/terminal/monitor.py#L1618)、[attachment.py](src/pulsara_agent/runtime/terminal_application/attachment.py#L1)

未来若产品明确要求terminal跨Host继续，必须作为独立feature引入外部process supervisor、不可伪造的stable launch identity、scope/capability、status-query contract、output retention与kill authority，并重新评审schema和failure matrix；不能把`AgentEvent`、durable job claim、PID probe或普通hook扩张成execution recovery protocol。

#### 决策 24：全部subagent execution随Host结束而interrupted

这是V1统一语义，不再按delegation label区分两种durability模式。当前实现的物理owner本来就是进程内`asyncio.Task`、child `RuntimeSession`、capacity reservation与MCP binding；`register_recovered()`、child teardown generation/retry/reconciliation lineage才是把这种process-local execution扩成恢复图的overdesign。[execution.py](src/pulsara_agent/runtime/subagent/execution.py#L1)、[execution.py](src/pulsara_agent/runtime/subagent/execution.py#L116)、[execution.py](src/pulsara_agent/runtime/subagent/execution.py#L390)、[execution.py](src/pulsara_agent/runtime/subagent/execution.py#L608)

V1冻结以下contract：

1. `subagent_tasks`持久化已接受的task id、objective/profile、parent task/turn、创建它的`execution_writer_generation`、closed status与terminal reason；已接受的message/result作为该task的immutable children保留。它们表达coordination/history，不构成可claim的work queue；
2. task只有`pending | active | completed | failed | interrupted | cancelled`。`pending/active`只对创建它的当前Host generation有效；没有background flag、attempt id、worker lease、claim generation、retry lineage、checkpoint或subagent级`outcome_unknown`；
3. 同一存活Host可以调度、观察和终结child。task/message/result及相应selective committed event都由`HostWriterGuard`在同一PostgreSQL transaction接受；普通hook、worker与child callback没有append authority，subagent execution不会出现在`durable_jobs` catalog；
4. orderly detach/close先停止新child admission，向全部activation task和child `RuntimeSession`发送cancel，并在共享close deadline内bounded join；已经completed/failed/cancelled的task不改写，仍未terminal的task由当前Host置`interrupted(host_closed)`并追加窄occurrence，然后才释放writer/session资源。超deadline的physical owner必须先被撤销全部session-owned port/capability且不能detach成后台任务，close不能在它仍可访问已释放资源时宣称成功；
5. abrupt crash无法执行close transaction时，下一Host acquire/takeover在同一open transaction中把旧`execution_writer_generation`遗留的全部pending/active task幂等置`interrupted(host_lost)`并追加occurrence。child process-local state、partial live output和未接受result直接消失；不合成completed/result，也不从event replay重建child；
6. reattach读取canonical task/message/result。interrupted task不会自动resume、requeue或改造成job；用户或parent明确再次委派时创建新的task id，并可用普通causation字段指向旧task，但不复用旧execution identity；
7. child内部已提交的tool call/attempt/result继续服从统一effect contract。若Host丢失时attempt存在而result缺失，unknown属于exact tool attempt，而不是通过subagent aggregate恢复或重做；
8. future product若真的要求delegation跨Host继续，必须另行定义durable work identity、result acceptance、effect safety、cancel与visibility contract并重新评审Stage 2 schema；不能仅增加`background=true`、复用旧subagent event/checkpoint或把child `RuntimeSession`塞进generic job payload。

这保留了Pulsara的subagent产品能力和完整已接受history，同时删除最昂贵、也最难正确承诺的跨Host child execution recovery。`AgentEvent`只观察`TaskCreated/Completed/Failed/Interrupted/Cancelled`及必要message/result acceptance，不驱动child重新执行。

#### 决策 25：selective committed AgentEvent随session lifetime全量保留

V1把selective `agent_events`与canonical transcript采用同一个session lifetime边界：从session创建开始接受的全部`StoredCommittedEvent`按`event_sequence` append-only保留，直到整个session在未来显式删除或本次complete reset中作为同一数据universe消失。当前V1没有整会话delete/archive/legal-hold产品面，因此正常运行等价于不按age、row count或byte pressure裁剪committed journal。

这一选择冻结以下减法：

1. `sessions`只保存`latest_event_sequence`，不保存`retained_from_event_sequence`；schema没有event retention class、TTL、prune cursor、event-first delete transaction、archive tier、legal-hold flag或journal GC/repair owner；
2. event payload仍由每个closed schema限制single-row byte大小，Protocol observation仍有per-poll event/byte/time hard cap，但这些是写入/响应资源边界，不是storage retention。`(after,H]`超过observation预算时返回budget GAP并要求fresh canonical snapshot；数据库中的旧event不删除，Inspector/audit仍可按固定event cut无状态分页查询；
3. schema incompatibility或client-ahead同样可以让某个presentation consumer返回GAP，但不能裁剪、改写或覆盖stored occurrence，也不能影响canonical row；普通post-commit hook仍无catch-up，这项retention承诺不会把hook变成可靠consumer；
4. exactly-one typed subject FK继续使用`ON DELETE RESTRICT`。V1既不删除canonical subject，也不单独删除event，因此不存在dangling subject、`SET NULL`、tombstone或删除顺序状态机；
5. complete reset按决策15整体清空session、canonical rows、event journal与blob namespace，不属于运行期retention。未来若新增整会话删除、export、archive或legal hold，必须另立ADR/schema migration并定义整个session universe的原子/有序处置，不能重新启用一套预埋的prefix/event prune machinery。

session-lifetime audit并不扩大恢复承诺：reopen仍只读canonical rows，旧event只服务Inspector、审计、bounded observation与typed extension，不用于恢复provider、tool、subagent、terminal或pending interaction execution。

### 7.3 AgentEvent as typed extension protocol

本节是hard-cut的normative contract。核心准则是：**`AgentEvent`是Runtime的typed extension protocol，不再承担Runtime execution recovery state machine。** “统一”指namespace、类型语义、subject与redaction规则可共享；不表示三个event plane共享base class、queue、serializer、retention或failure semantics。

#### 7.3.1 三种hook与一个非hook tool-authorization policy

| extension kind | 输入平面 | 触发点 | 允许用途 | durability/failure语义 |
|---|---|---|---|---|
| live streaming hook | `LiveAgentEvent`的registration-specific projection | Text/Thinking/Data/ToolCall/ToolResult Start/Delta/End或session live-control Opened/Replaced/Closed发布后 | Inspector/debug、recorder、eval、custom renderer | process-local、best-effort；overflow GAP后detach或直接detach；callback失败不影响provider/tool/live-control owner |
| post-commit domain hook | `CommittedAgentEvent`的typed/redacted projection | canonical transaction成功且event已获得sequence后 | audit export、analytics、memory suggestion、automation | 只从registration cut后best-effort投递；失败/overflow不回滚、不改turn；V1没有generic reliable/durable升级开关 |
| operational diagnostics hook | `OperationalEvent` | TTFT/retry/buffer/cache/backpressure/error发生时 | metrics、debug、SRE diagnosis | sampling/丢弃允许；无conversation correctness语义 |
| `ToolDispatchAuthorizationPolicy` | 显式request/decision DTO，不属于普通hook | 完整assistant tool-request已canonical commit、任何attempt/physical dispatch之前 | `Allow | Deny | RequireConfirmation` | machine evaluation默认2秒、hard cap 5秒；timeout/error/schema mismatch转confirmation，无controller时deny；不得rewrite arguments或复用observer bus |

普通hook永远是observer。它不能返回“回滚已提交row”，不能抢占canonical owner，也不能让异常、timeout或queue overflow使run失败。V1唯一policy kind是`ToolDispatchAuthorizationPolicy`；prompt格式校验、canonical constraint与Host capability签发仍是各自application/storage owner的直接职责，不扩展成generic pre-commit policy marketplace。Runtime只调用一个Host-owned typed resolver，不并发合并任意callback列表；ordinary plugin没有policy registration capability，只有显式获批的managed policy principal可以进入resolver的确定性配置。PreToolUse-style授权有真实价值，但不能因为callback API看起来方便就把任意extension变成隐式gate。

policy request引用已经accepted且immutable的assistant tool call、tool descriptor与授权所需的closed argument projection；decision只有：

~~~text
ToolDispatchAuthorizationDecision =
    Allow
  | Deny(reason_code)
  | RequireConfirmation(interaction_kind)
~~~

machine resolver的默认deadline是2秒、server hard cap是5秒；human confirmation等待属于pending interaction lifecycle，不计入machine deadline。resolver缺失时使用Host built-in base policy；已配置resolver的timeout、异常、cancel、schema mismatch或unavailable统一降级为`RequireConfirmation`，若current controller不存在或产品配置禁止询问，则接受`Deny(policy_unavailable)`。这是对physical dispatch的fail-closed、对conversation run的fail-soft：没有`Allow`就不得创建`ToolAttemptAccepted`或physical invoke，但denied/cancelled/tool-unavailable等closed pre-dispatch terminal仍以无attempt canonical tool result闭合该call并可产生`ToolResultAccepted`，后续provider input因此保持tool request/result配对，不把policy故障变成provider transport错误或canonical rollback。

V1的policy可重写字段集合冻结为空。assistant tool-request message及arguments在policy前已经canonical commit；若policy改变dispatch arguments，就必须新增第二个effective-input authority、重新授权并解决TOCTOU与多writer冲突。需要不同参数时，policy拒绝当前call并给出typed reason，由provider在后续turn产生new call。Codex当前并发PreToolUse handler以“最后完成的rewrite获胜”的实现只作为反例，不进入Pulsara contract；Grok-build把classifier unavailable转人工确认的做法支持上述fail-closed-for-dispatch边界。

first-party TUI的authenticated user live projection不是ordinary hook registration。它走同一typed live vocabulary与bounded delivery mechanism，但使用决策20的server-minted user view profile：已投影raw thinking原样可见，tool arguments按closed complete/truncated DTO显示。人类用户在UI中可见不意味着recorder或plugin可以获取同一raw object。

#### 7.3.2 closed event namespace、registration identity、version与lease

V1 formal AgentEvent namespace只有Runtime-owned `pulsara.core`：决策7的exact 26类Committed与23类Live，共49类。extension只能订阅Host生成的typed projection，不能定义、发布、伪装或动态注册新的Committed/Live AgentEvent type；`publisher_id`不是extension API，core producer仍由closed Runtime/domain owner决定。extension自己的debug topic只能进入namespaced Operational diagnostics/logging，不进入49类registry、durable serializer或historical decoder。未来增加formal type必须重开architecture decision并同时修改type count、producer、schema/version、sensitivity、subject/guard与fixtures，不能靠manifest或自由JSON热插拔。

每个registration至少绑定下列字段；缺任何一项不得激活：

| 字段 | 冻结语义 |
|---|---|
| `extension_principal_id` | Host从authenticated installation导出的`(publisher_namespace, package_name)`；extension不能自报另一个publisher |
| `handler_id/manifest_digest` | `handler_id`是manifest中显式、稳定且不随数组重排变化的id；digest绑定当前代码/config/schema，内容变化使旧授权失效 |
| `registration_id` | Host签发的process-local opaque instance id；同scope内唯一，用于replace/revoke/query，不跨Host重用，也不进入event metadata |
| `scope` | 明确的process/session/turn/subagent/plugin scope及可选subject filter；scope结束即停止投递 |
| `protocol_binding` | exact core plane + event type + projection major + negotiated additive capabilities；不能绑定custom AgentEvent namespace |
| `projection_profile` | ordinary-redacted或具名privileged extension projection；不允许callback自行从raw event任意取字段，也不允许extension声称first-party user view profile |
| `capabilities` | 最小集合，例如`live.text.read`、仅first-party Inspector/debug可获批的`live.thinking.read_raw`、独立且仍有byte hard cap的`tool.arguments.read_unredacted`；private URL只由current-controller interaction view获得，S3 secret没有可授予read capability |
| `lease_id/lease_generation/expires_at` | Host签发、可撤销、可过期；每次delivery前level-check，generation或digest变化、revoke、scope close与Host restart都使旧lease失效 |
| `registration_cut/delivery_mode` | 在committed tap registration lock内记录`process_generation + tap_offer_ordinal`；ordinary hook固定为`BEST_EFFORT_AFTER_CUT`，只接收tap在该cut之后接受的offer。它不是数据库`latest_event_sequence`、durable cursor或历史补投承诺 |
| `queue_budget` | max events + max bytes，三类ordinary hook都必须有界；不得配置为unbounded |
| `timeout/drain_budget` | 单callback deadline与close时最多等待的总预算 |

breaking projection change递增major；Runtime至多同时提供current major与immediately previous major，V1首次activation只有current major。additive optional字段通过registration capability negotiation启用，Host为每个binding选择一个exact projection shape，不把“latest”未协商对象直接交给consumer。无共同major时拒绝该event binding；运行中出现不可能的unknown type/version、schema mismatch或client-ahead时只detach该registration并记录bounded Operational diagnostic，不向callback发送`Unknown + raw JSON`，也不使Runtime失败。previous major移除需要显式sunset与fixture删除，但不建立universal historical decoder；stored committed payload的有限per-domain upcaster仍是7.2.5的独立contract。

registration identity、callback对象、recorder实例、queue、task、assembler和live owner都是process identity，**不得写入`EventBase.metadata`、committed payload、canonical row或historical decoder**。committed event metadata只允许versioned domain字段；自由form dict不能作为extension逃生口。manifest digest或capability grant变化时Host推进lease generation并撤销旧instance；revoke/expiry立即停止新callback并丢弃尚未开始的queued projection，已开始callback只能运行到自己的deadline。Host restart后extension必须重新发现、鉴权和注册；不得从数据库恢复active registration、lease或delivery cursor。

#### 7.3.3 ordering、backpressure与close

1. 一个live generation内，bus按publisher接受顺序给event分配单调`live_sequence`；同一block必须满足Start < Delta* < End。这个sequence只在process内有效。
2. Start是frozen announce；observer不得看到后续delta对已交付Start对象的原地mutation。assembler是唯一可变state owner，End携带独立frozen final block。
3. 每个observer/hook有独立bounded queue。slow consumer不能延迟assembler、provider transport、canonical transaction或其他consumer。
4. live/operational queue overflow时，若当前budget仍能容纳一个bounded GAP，实现清空未读suffix、投递包含lost range/current generation的`LiveGap`后detach；否则直接detach并记录bounded `OperationalEvent`。不得await空位，不得把delta转存durable journal。
5. ordinary post-commit hook只接收process-local tap在registration cut之后接受的offer；已实际投递的同session item按`event_sequence`有序，但不承诺覆盖数据库中的完整suffix、多个callback完成顺序或restart catch-up。commit已成功但tap尚未offer时的process crash可以丢callback。overflow时发process-local `HookGap` diagnostic并detach；不得由hook manager查询journal自动回填。需要串行副作用的extension注册一个handler并自行序列化。V1不能把registration改成`reliable=true`或自动升级成durable job；跨重启必达需求必须在未来以独立ADR新增具名job type。
6. callback timeout、cancel、exception和schema mismatch只产生bounded operational diagnostic。它们不能转换成`EventPublicationAfterCommitError`、reconciliation latch、RunError或canonical rollback。
7. revoke/detach立即停止开始新callback，丢弃已排队但未开始的delivery；已开始callback获得自己的deadline。Host close先stop registration admission，再在全局`close_drain_budget`内只等待已开始callback；超时即cancel/detach并关闭bus，不追求business completion。
8. 同进程可保留按event/byte双上限的provider live ring snapshot，fresh observer得到`generation_id + retained_from_live_sequence + snapshot`；每observer queue、shared ring与snapshot各自必须有独立event/byte hard cap。first-party用户snapshot对尚在retained range内的thinking不做内容redaction/truncation，但不承诺已退出range、GAP或跨进程live replay。Host crash后generation失效，observer必须转canonical snapshot。
9. session live-control不靠ring replay：`SessionLiveControlSnapshot`返回`owner_epoch + live_revision + current_interaction`，`snapshot_and_subscribe()`在同一owner lock内冻结snapshot并注册只接收`revision > cut`的observer。Opened/Replaced/Closed每次只推进当前epoch内的revision；notification丢失、queue GAP或TUI reconnect都重新level-read snapshot。owner epoch变化即清空旧interaction/render state。

当前`UiCommittedEventTap`的ring/subscriber/GAP设计可作为机制参考，`RuntimeEventPublisher`的unbounded queue、串行subscriber await和`await_delivery=True` error传播则必须退出Runtime correctness path。

#### 7.3.4 sensitive projection contract

- first-party authenticated user live projection与extension projection必须在类型和构造路径上分开。前者对Runtime实际收到的thinking delta原样投影，对tool arguments返回决策20的`CompleteToolArguments | TruncatedToolArguments`；不持久、不保证late-attach/GAP/crash completeness，且不能被extension registration选择。
- ordinary hook默认只接收typed/redacted projection：文本可按产品visibility投影；thinking只显示“thinking block active/ended”或redacted摘要；tool argument只给tool name、call id、schema-valid/size/hash，不给未redacted JSON；private URL只给origin-less label或commitment；MCP secret完全不可见。
- raw thinking extension projection仅可授予first-party Inspector/debug registration的短期session-scoped lease；未redacted tool arguments使用独立S2 capability，其“未redacted”不取消single-item、queue与snapshot byte hard cap，超限仍显式截断或GAP/detach。private URL不对ordinary hook/recorder授予，只进入current controller的dedicated interaction view。这些profile/capability互不隐含。
- view profile与capability只能由Host authorization service基于authenticated principal、workspace/session policy、current-controller state签发/撤销；plugin、callback或event payload不能自证权限。projection在publish前生成，禁止把raw object交给callback后要求其自觉redact。
- `McpSecret`、OAuth token、cookie、Authorization header、URL credential/query/fragment等S3值不得出现在Committed/Live/Operational payload、GAP、exception message或metadata。若adapter需要secret，只能通过现有sealed borrowing port按purpose临时借用。
- recorder也是hook，不拥有特权。它只能记录registration获准的projection；“调试模式”不能自动升级capability。
- capability revoke后，已排队但未开始的privileged projection必须丢弃；不能依赖callback在收到后检查lease。

#### 7.3.5 committed journal的append authority与hook delivery边界

`CommittedEventAppender`是storage/application内部sealed port，不是extension API。V1只接受closed guard union：

~~~text
EventAppendGuard =
    HostWriterGuard(session_id, writer_generation)
  | JobAttemptClaimGuard(job_id, attempt_id, claim_generation, origin_session_id)
~~~

显式user/conversation/tool/queue/interaction、全部subagent task/message/result acceptance、foreground `remember_*` proposal/tool-result acceptance，以及Host存活期间把terminal completion/termination接受为canonical tool result/entry，都使用`HostWriterGuard`；process-local terminal monitor与child executor本身没有append authority，也没有terminal-specific committed type。proposal transaction只把candidate变成durable work fact，借`ToolResultAccepted`及必要`JobQueued`观察，不新增`MemoryCandidateAccepted`。自动memory extraction/governance等worker-owned canonical mutation使用`JobAttemptClaimGuard`；governance worker只允许写其job/attempt-owned decision、memory output或决策7允许的memory/job occurrence，不能写transcript或subagent coordination rows。job result进入conversation仍需当前Host另做accept transaction。没有immutable `origin_session_id`的global job/memory work不进入session-scoped `agent_events`。V1不增加第三个generic memory/governance guard：无法归入Host或durable job的writer没有committed-event append authority。

所有可发session event的transaction遵守统一SQL lock order：

1. 先锁`sessions`中的event allocator/high-water row；当前PostgreSQL EventLog已有per-session transaction advisory lock先例，但目标使用显式row/conditional lock，不沿用旧receipt/continuity graph；[postgres.py](src/pulsara_agent/event_log/postgres.py#L2090)
2. 在同transaction校验closed guard：Host generation在session row校验；worker再锁exact job attempt并校验claim generation与immutable origin session；
3. 插入/更新guard允许的canonical subject row；
4. 插入0到少量`StoredCommittedEvent`，每行通过closed typed subject-FK union exact引用一个canonical subject，并分配连续sequence；
5. 推进`latest_event_sequence`后commit。rollback不推进high-water，其他owner在前一transaction释放session lock后才能分配下一sequence。

event payload必须能由本transaction已知的accepted transition生成；不得异步读取row后补写“看起来对应”的event。普通hook/plugin、TUI、Inspector、recorder和journal reader没有`EventAppendGuard`，因此永远不能发布`CommittedAgentEvent`。guard只授权canonical+event原子mutation，不写入event metadata、不形成第三个fencing domain，也不保存delivery/recovery state。

appender也不得暴露generic `(event_type, subject_kind, subject_id, payload)`入口。每个closed domain adapter接收该canonical repository在当前transaction返回的typed primary-key/value object，并只能构造7.5映射允许的event type/subject column；它没有stable candidate id、可序列化handle或跨transaction复用。数据库type-slot/composite FK是最终防线，typed adapter是调用面防线；两者都不能被extension绕过。

commit返回后才向process-local committed tap offer `(event_sequence, typed_redacted_projection)`。offer和subscriber执行不在数据库transaction中。若process在commit后、offer前crash，canonical row与stored event都已存在；TUI或显式audit client可以查询journal suffix，**普通post-commit hook不会自动补投，允许错过这次callback**。若任何consumer永远失败，canonical fact仍成立。

V1第三方durable post-commit action数量冻结为0：没有generic durable extension job registration、extension-owned journal tailer、`reliable=true`或`catch_up=true`。compaction precompute与memory extraction/governance是first-party具名job，不是普通hook升级。未来某项产品确实要求跨重启必达时，必须以独立ADR/schema migration新增一个closed job type，并冻结stable `action_definition_id`、payload/handler version、deterministic domain idempotency key、terminal acceptance，且从已冻结的`RETRY_SAFE | REMOTE_QUERYABLE | NON_IDEMPOTENT`中选择safety class，不另造extension retry taxonomy；durable identity不得引用process-local `registration_id`或lease。该future job的cursor/attempt只属于它自身，不能给ordinary hooks恢复generic consumer horizon/receipt graph。

#### 7.3.6 extension protocol architecture guards

- `LiveAgentEventBase`、live bus、assembler不得import`event_log`、historical decoder、PostgreSQL event serializer或authority materialization；
- durable serializer registry不得注册任何provider/tool-result `LiveAgentEvent` class；live schema registry反向不得接受`event_sequence`或durability receipt；
- formal AgentEvent registry必须exact等于`pulsara.core`的26 Committed + 23 Live；plugin manifest、callback或Operational topic不得增加、替换或发布Committed/Live type，也不得提供`CustomAgentEvent(kind, payload)`逃生口；
- production callback signature不得拿到RuntimeSession、canonical repository、live owner、recorder或mutable assembler；只拿frozen projection和cancellation/deadline context；
- 普通hook返回值必须为`None`/diagnostic，不得包含allow/deny/rewrite；唯一`ToolDispatchAuthorizationPolicy`使用不同module/base，只允许managed principal进入Host-owned resolver，decision exact等于`Allow | Deny | RequireConfirmation`且rewritable field count为0；
- `CommittedEventAppender`只接受closed `EventAppendGuard`，其module不得被plugin/hook package import；worker guard不能写transcript，Host guard不能冒充job claim；
- ordinary post-commit hook registration不得持久化cursor、调用journal catch-up、声明reliable delivery或在overflow后继续投递；V1不得注册generic durable extension action/tailer，future具名job必须经独立ADR且不能引用process-local registration identity；
- 任一hook test注入sleep、exception、cancel、overflow或malformed output时，provider继续、canonical commit成功、其他observer继续；
- machine policy的默认2秒/hard-cap 5秒、unavailable→confirmation、无controller→deny路径必须行为测试；未Allow时physical invoke count为0，deny产生无attempt terminal result闭合provider protocol，任何argument rewrite或第二套effective-input row都触发guard；
- registration必须使用authenticated extension principal、manifest-stable handler id、Host-minted instance id与process-local revocable lease；位置派生handler id、跨Host恢复registration/lease、无协商projection major或向callback传`Unknown + raw JSON`都触发guard；
- first-party user live projection的constructor不得被hook/plugin import或选为projection profile；用户thinking投影不得摘要/redact/按内容长度截断，tool arguments必须经closed complete/truncated union且截断值不得进入assembler、canonical call或dispatch adapter；
- live snapshot test只在相同generation返回bounded retained content；GAP后detach并重建observer，restart test断言旧generation不可查询、无历史Start/End被合成；
- content-live observer、shared ring、provider/tool-result/control snapshot与control observer缺任一event/byte hard cap，或close等待未开始callback，应直接触发architecture guard；
- S2 field必须有user-view/ordinary/privileged/revoked四类projection tests，S3必须有construction/serialization/exception-leak tests。

### 7.4 TUI 与 Protocol hard-cut boundary

#### 7.4.1 当前代码事实：TUI 是 Presentation Foundation 的消费者

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

#### 7.4.2 冻结的目标协议语义

第一次 foreground authority cut 同时发布 **Protocol v3**；它是 incompatible major hard cut，不在 server内维护 v2→v3 presentation compatibility shim。

Python侧提供一个窄的conversation query service（逻辑边界名称，不要求沿用此类名）。每个canonical snapshot使用一个短的read-only `REPEATABLE READ` transaction；所有metadata、canonical control state、entry rows与`event_sequence_cut`来自同一PostgreSQL MVCC snapshot，不把多次autocommit query拼成一个response。pending live interaction不属于这组canonical state，见7.4.5。

`sessions.latest_sequence`定义为该session按**commit order**发布的canonical entry high-water，并且在任一MVCC cut内必须等于可见的最大已提交entry sequence。每个canonical entry transaction先锁定session row或执行等价的原子conditional update，在同一transaction内取得`latest_sequence + 1`、插入entry并推进high-water；rollback既不发布entry也不推进high-water。禁止在transaction外预留sequence、使用可能让sequence 101先于100提交的non-transactional `nextval`，或异步追赶head。parallel tool execution仍可并行，只在最终canonical entry commit的窄分配点串行化。bounded snapshot suffix必须以该latest entry结尾，较老部分按容量裁剪并用`has_more_before`表示。

`sessions.latest_event_sequence`是selective committed journal的public observation high-water。决策7列出的26类canonical transition——包括queue admission/consume/cancel/reject、accepted interaction decision、turn terminal、tool attempt/remote identity、message/tool/job/memory/coordination acceptance——都由该canonical owner在同一transaction通过closed `EventAppendGuard`写对应`CommittedAgentEvent`并推进high-water。Host domain使用writer guard；job/automatic memory worker使用exact attempt-claim guard。session detach/close、纯CAS revision、context compiler中间态、pending live interaction、background worker private progress、spinner、transport和UI observation不发committed event。这样不再需要独立`control_revision`或per-section cursor：entry sequence回答conversation ordering，event sequence回答所承诺的可观察transition ordering，canonical row仍回答current truth。

每次provider dispatch也使用一个短read-only `REPEATABLE READ` transaction冻结当前`context_binding_revision_id`与同一MVCC cut中的`latest_sequence = H`，并只把`entry_sequence <= H`的合法conversation delta交给compiler/lowering。immutable blob正文可以在验证引用属于该cut后分页hydrate，但不能把H之后才commit的entry混入本次input。process-local immutable prepared-input handle持有revision与H直至assistant commit；commit只消费该handle，不能重新读取latest sequence或由caller自报cut。accepted provider-generated assistant entry将H写入`provider_input_through_sequence`。这只是每条assistant row上的窄attribution，不是Protocol/TUI cursor，也不引入durable ModelStart、ModelEnd、provider request、candidate、receipt或operation journal。

stored journal row与TUI wire DTO必须物理分开：

~~~text
StoredCommittedEvent
  occurrence + exact typed subject FK + event-time audit fields + minimal payload

CommittedObservationProjection =
    EventOnly(stored_event_projection)
  | ImmutableEntryProjection(stored_event_projection, exact_entry_without_content_duplication,
                             ordered_content: tuple[ObservationContent, ...])
  | CurrentControlProjection(stored_event_projection, current_subject_state, state_through_event_sequence)

ObservationContent =
    InlineContent(subject_entry_id, content_slot, block_ordinal?, canonical_bytes,
                  content_digest, canonical_size_bytes, media_type, content_codec)
  | CanonicalBlobReference(reference_version, session_id, workspace_id,
                           subject_entry_id, content_slot, block_ordinal?, blob_id,
                           content_digest, canonical_size_bytes, media_type, content_codec)
~~~

`CommittedObservationProjection`只是Gateway在read transaction内创建的bounded、typed、capability-redacted DTO；它没有table、owner、checkpoint、root、repair或retention。`EventOnly`服务audit或无需UI mutation的occurrence；`ImmutableEntryProjection`携带渲染新user/assistant/tool-result entry所需的exact canonical fields与ordered semantic content。inline branch立即可渲染；blob branch是可确定性hydrate的canonical locator，不是正文副本。`CurrentControlProjection`携带同一read cut中的queue/turn/session/tool/job current state，并明确它是read-time current truth，不是event-time历史快照。canonical snapshot、history page和committed observation必须复用同一个`ObservationContent` union，不能各自发明artifact DTO。

conversation query service从canonical session/turn/transcript/tool/queue/accepted-interaction-decision facts形成三类响应：

1. **snapshot**：先在同一read transaction中冻结`session_id`、`latest_sequence`与`event_sequence_cut=latest_event_sequence`，再读取`entry_sequence <= latest_sequence`的bounded suffix，以及同一MVCC cut中的turn、tool attempts、queue、session lifecycle与accepted interaction decisions；tool attempt的started/terminal-known/outcome-unknown视图只能由该cut中的attempt/result/turn facts派生；`writer_generation`可以作为后续mutation hint返回，但也必须来自该cut；snapshot明确不返回pending interaction request；
2. **history page**：request携带明确`cut_sequence`，按每session单调递增的`entry_sequence`做`before`/`after`分页，只返回`entry_sequence <= cut_sequence`的稳定entry；page response回显cut，不返回transcript epoch、presentation root、active head、projection contract fingerprint、retention-root lease或continuity receipt；
3. **committed observation**：客户端提交`after_event_sequence`。Gateway在一个短read-only `REPEATABLE READ` transaction中冻结`H=latest_event_sequence`；若client-ahead、schema incompatible，或`(after,H]`全部suffix超过event/byte预算，则直接返回committed GAP并要求fresh snapshot，不分页返回一个可能与current control state错位的半suffix。suffix在预算内时，Gateway读取`after < event_sequence <= H`的全部`StoredCommittedEvent`，按每个closed subject FK读取exact canonical subject，并返回`through_event_sequence=H`的一组`CommittedObservationProjection`。旧event始终保留；Inspector/audit使用独立的固定event-cut无状态分页，而不是让TUI半量追赶current state。immutable entry直接携带exact projection；mutable control携带该MVCC cut的current state和`state_through_event_sequence=H`。LISTEN/NOTIFY只是edge hint，超时或通知后都level-read high-water/observation。spinner、transport retry、token delta与provider live progress走provider live stream；pending interaction走独立session live-control snapshot/event contract，二者都不并入canonical snapshot，也不构成semantic acknowledgment。

Go client只解码`CommittedObservationProjection`：用`ImmutableEntryProjection`追加/替换entry cache，对其中的`CanonicalBlobReference`调用7.4.2.1唯一读取端口后完成exact content，用`CurrentControlProjection`按`state_through_event_sequence`更新current state，对`EventOnly`只更新audit/notification surface。它不得根据stored event的subject id自行猜message内容，也不得把event payload当canonical row副本或拿`blob_id`直读storage。数据库event query仍可供Inspector/audit API使用，但不是Go presentation reducer的wire contract。

每个history page response也使用自己的read-only repeatable-read transaction：先验证session仍存在、request cut不超过当前`latest_sequence`、cursor属于同一session/cut且`entry_sequence <= cut_sequence`，再在同一MVCC cut读rows。accepted entry永不因compaction、takeover或maintenance消失，因此合法旧cursor没有retention失效分支。新commit可以推进全局high-water，但不会混入旧cut的page；客户端需要新snapshot/high-water cycle才能看到它。

cursor可以是opaque carrier，但其语义只能等价于 `(session_id, cut_sequence, entry_sequence)`：

- session identity不可复用；complete reset后旧session直接不存在，不用epoch模拟跨universe连续性；
- compaction只追加context snapshot，绝不删除、覆盖、重排或重编号任何entry；
- Host takeover只改变 `writer_generation`，不得让合法read cursor整体失效；
- `writer_generation`用于mutation fencing，不是history排序，也不是presentation root generation；
- entry ordering由数据库sequence/唯一约束给出，不创建另一套root identity或cursor fingerprint authority。

`event_sequence`不进入conversation history cursor，因为它不替代entry ordering；它只排序selective accepted occurrence。client收到committed GAP或schema incompatibility后获取一个新一致canonical snapshot，而不是event replay execution或让event覆盖row truth。

#### 7.4.2.1 Blob-backed canonical content读取契约

`CanonicalBlobReference`只允许定位**canonical transcript entry的closed content slot**，包括entry body、assistant block body或tool-result body；`content_slot + block_ordinal`必须能由schema唯一定位一条immutable canonical content edge。job payload、memory artifact、context snapshot与debug audit即使也共用`blobs`，也不会因此自动获得TUI读取能力。reference是versioned descriptor，不是bearer capability、presigned/private URL、任意storage key或持久化下载会话。

Protocol v3冻结下列唯一读取形状；`request_context`来自已认证attachment/principal与server-side capability binding，不是client可伪造的reference字段：

~~~text
ReadCanonicalContent(
    reference: CanonicalBlobReference,
    offset_bytes: uint64,
    limit_bytes: uint32,
    request_context: AuthenticatedObservationContext
) -> CanonicalContentChunk

CanonicalContentChunk {
  content_digest,
  canonical_size_bytes,
  media_type,
  content_codec,
  offset_bytes,
  data,
  returned_bytes,
  next_offset_bytes,
  eof,
  chunk_digest
}
~~~

range单位是**canonical logical bytes**：storage decompression/decryption之后、semantic codec decode之前的byte sequence。`content_digest`与`canonical_size_bytes`也针对这组bytes；内部压缩、分片或加密格式不是wire contract。`offset_bytes >= 0`，`1 <= limit_bytes <= server_hard_cap`；offset大于size为typed invalid-range，等于size可返回empty EOF。UTF-8等多byte codec允许chunk切在code point中间，client必须按byte offset组装后用声明的codec增量或最终decode；unknown codec/version fail closed，不能猜测fallback。

每次读取独立执行以下步骤：

1. 验证当前attachment/principal仍有效，并解析当前session/workspace scope与具名content-read capability；
2. 在一个短read-only transaction中按`subject_entry_id + closed content slot/block ordinal`重读exact immutable canonical edge，验证该entry属于reference声明的session/workspace，且当前principal有权读取该subject；
3. 要求edge中的`blob_id/content_digest/canonical_size_bytes/media_type/content_codec`与reference逐字段相等；只提交裸`blob_id`的请求没有合法解析路径；
4. 结束数据库transaction后，执行不超过hard cap的storage range read，验证blob metadata、returned range与`chunk_digest`，并返回完整content descriptor；传输正文期间不得持有数据库transaction、session lock或writer/job guard；
5. Go按`offset_bytes`拼接、逐chunk验`chunk_digest`，在全部bytes到齐后再验完整`content_digest`。可以用安全的streaming decoder渐进展示，但在完整digest通过前不得把内容标记为exact/final。

reference本身永不授权。跨session、跨workspace、capability被撤销、subject/slot不存在或descriptor stale对未授权caller统一返回不构成existence oracle的`NOT_FOUND_OR_FORBIDDEN`；已授权caller遇到missing bytes、range不一致或digest/size mismatch时返回`CONTENT_INTEGRITY_ERROR`并产生redacted `OperationalEvent`。UI显示明确的unavailable/corrupt placeholder；不得把原canonical entry回滚、改成failed，自动寻找另一blob、合成content event或启动repair owner。

content edge与blob是immutable且受FK `ON DELETE RESTRICT`保护，因此后续chunk不需要复用最初snapshot/observation的MVCC cut，也不需要download lease/cursor；每个chunk重新鉴权即可。读取产生的durable row、receipt、lease、cursor、projection、repair、ACK与`ContentDelivered`事件数量全部为0。implementation可以使用bounded process-local/Go cache，但cache key必须包含完整descriptor，capability revoke后不得以cache绕过产品定义的展示撤销规则，也不承诺跨进程live replay。

#### 7.4.3 GAP、reconnect 与 Go client

v3有三种显式stream discontinuity：committed cursor出现client-ahead、schema incompatibility或suffix超budget；provider live observer发生`LiveGap`/generation变化；session live-control发生queue GAP或`owner_epoch`变化。任一情况都不会写回Runtime authority，也不会删除session-lifetime stored event。对committed GAP，Go client必须：

1. 丢弃本地 durable page cache；
2. 请求fresh snapshot；
3. 以snapshot suffix重新建entry-indexed bounded cache，并从snapshot的`event_sequence_cut`开始请求`CommittedObservationProjection`；
4. 如需更老history，再用`before_sequence`分页。

committed cursor超前于server high-water属于stale/corrupt client state，同样fresh snapshot；history/audit page的wrong-session、wrong-cut、malformed或client-ahead cursor返回typed invalid cursor，不伪装成retention GAP。provider `LiveGap`丢弃当前partial renderer并读取同进程bounded live snapshot，若generation已变化则回到canonical snapshot。session live-control GAP只重新调用atomic `snapshot_and_subscribe()`；`owner_epoch`变化清空旧interaction，随后canonical snapshot会显示旧turn的interrupted/accepted-decision状态。raw partial与旧pending request都不由committed journal补造。

Go端应删除root-indexed resident cache、rank-basis join与confirmed-root validity规则，改为以`entry_sequence`索引的bounded transcript cache。attachment observer/controller、heartbeat与secret transport可以保留，但mutation binding必须最终在Host入口携带并校验当前数据库 `writer_generation`。现有wire `controller_generation`仍只管理attachment controller权利，二者不可混用。

#### 7.4.4 Mutation ACK unknown的最小幂等边界

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

#### 7.4.5 Pending interaction的V1 live-control边界

pending approval、plan question/exit与MCP input request不属于canonical snapshot，也不能落入一个未命名的“通知流”。它们是`LiveAgentEventBase`下独立于provider block stream的`session.control` namespace，只有当前Host拥有：

~~~text
SessionLiveControlSnapshot {
  session_id,
  owner_epoch,
  live_revision,
  current_interaction: RedactedLiveInteraction | None
}

LiveControlEvent =
    InteractionOpened(owner_epoch, live_revision, interaction_projection)
  | InteractionReplaced(owner_epoch, live_revision, previous_interaction_id,
                        interaction_projection)
  | InteractionClosed(owner_epoch, live_revision, interaction_id,
                      redacted_disposition)
~~~

`owner_epoch`是每次Host取得session live ownership时新建的opaque process-local token，不等于数据库`writer_generation`，也不写event metadata或数据库；`live_revision`从该epoch内的0开始单调递增。current value的open、replace和close都在单一live owner lock内先更新value/revision，再向bounded bus offer对应typed event。`snapshot_and_subscribe()`在同一把lock内冻结snapshot并登记只接收`revision > snapshot.live_revision`的observer，这才是snapshot与后续事件之间的线性化点。notification只是加速；queue overflow返回`LiveControlGap`并detach，客户端重新调用`snapshot_and_subscribe()`，不从committed journal补造控制事件。

普通observer只得到typed/redacted `RedactedLiveInteraction`。raw thinking与provider ToolCall arguments都不进入该control namespace；它们在provider live namespace中的用户展示遵循决策20。interaction payload里的private URL只进入当前controller的dedicated interaction view，不进入ordinary hook/recorder。MCP secret、secret handle和可恢复sealed response是S3，绝不进入snapshot、event、GAP、exception或metadata。capability revoke后queued privileged projection按7.3规则丢弃。

resolution command携带`expected_writer_generation`、`expected_owner_epoch`、`expected_live_revision`、`live_interaction_id`与`command_id`。Host application service在live owner lock内exact校验这组值，并保持该lock跨越一个短的accepted-decision transaction；该transaction校验writer generation，写`interaction_decisions`、应用command id唯一约束，并以`HostWriterGuard`追加`InteractionDecisionAccepted` committed event、推进`latest_event_sequence`。commit成功后在同一lock内清空current value、推进revision并best-effort offer`InteractionClosed`；rollback则current value不变。commit outcome unknown时先按command id level-read canonical target：winner存在才close，否则保留或fail closed，绝不重放secret或恢复suspended coroutine。这里的live lock只序列化当前进程的一项control value，不是数据库authority、receipt或execution recovery state。

decision不能只引用会随Host消失的live id：approval绑定durable assistant tool call；plan resolution在同一transaction创建canonical user/conversation item并由decision引用；MCP/external secret response只保存closed disposition、durable redacted tool-call/attempt subject与必要keyed commitment，不保存secret plaintext或可恢复sealed response。command-addressable base action是唯一幂等owner，不能为plan item和decision各生成一条command row。

ACK unknown查询只返回accepted/denied/cancelled状态与durable subject，不返回secret。secret handle在当前Host内由revocable process-local owner消费；若Host在decision commit后、使用secret前crash，旧turninterrupted，新Host不恢复值或继续旧operation。一旦Host crash、close或takeover，旧live value消失，旧resolution fail closed；open只把旧running turn置interrupted，不从transcript、trace、audit或suspended owner重建request。

因此Protocol v3 DTO必须把canonical snapshot/observation与session live-control snapshot/event分开。前者可跨Host恢复；后者只保证“当前Host仍活着时可重新读取”。Host close/crash/takeover使旧epoch整体失效；新Host从新epoch、revision 0与empty current value开始，不合成`InteractionClosed`，随后按canonical规则把旧running turn置interrupted。V1 schema明确没有`interaction_requests`、live-control cursor或epoch/revision row。未来产品若改变承诺，需要新的architecture decision和schema hard cut，不能复用operational DTO暗中改变durability。

#### 7.4.6 独立验收

- Python/Go contract tests明确拒绝v2/v3混连；
- snapshot、向前/向后page、空history、budget/schema GAP、client-ahead、reconnect均由跨语言fixture覆盖；
- 并发entry/turn commit发生在snapshot各SQL之间时，response的metadata、canonical control与suffix仍来自同一MVCC cut；不得出现latest=10但rows只到9或反向组合；
- history page严格回显并遵守epoch/cut sequence；翻页期间的新commit不混入旧cut；
- committed observation在任意notification丢失时通过`latest_event_sequence`与journal suffix发现queue/turn/session/accepted-decision/tool-attempt等transition；新assistant/tool-result occurrence必须返回包含exact entry与ordered `ObservationContent`的`ImmutableEntryProjection`，inline立即渲染、blob branch只能经`ReadCanonicalContent`确定性hydrate，不得要求Go只凭subject id猜内容；canonical snapshot仍直接读rows，consumer失败不改变commit；
- 强制把大型assistant text、跨chunk UTF-8与tool result置于blob branch，fresh snapshot/history/committed observation返回同一种`CanonicalBlobReference`；Go以多个bounded byte range组装，逐chunk与完整digest均通过后得到与canonical bytes完全相同的渲染内容；
- raw `blob_id`、篡改slot/digest/size/codec、跨session/workspace与撤销capability的读取稳定拒绝且不形成existence oracle；missing/corrupt bytes返回typed integrity failure与placeholder，不写receipt/lease/cursor/event、不回滚entry、不启动repair；
- TUI/Host重连后同一immutable reference仍可重新鉴权并hydrate；range读取期间数据库transaction已结束，慢client或storage不会占有session writer/allocator lock；
- suffix超过event/byte预算时一次返回committed GAP而不是半suffix；fresh snapshot后从新`event_sequence_cut`继续；`CommittedObservationProjection`全程不落表、不建cursor/checkpoint；
- kill TUI而Host继续产生reply/tool items，重连靠canonical snapshot加bounded observation projection完整恢复；
- notification丢失不影响turn completed；
- live observer overflow若可行只得到一个`LiveGap`后detach，否则直接detach；provider继续；Host crash后旧generation不可查询，TUI不显示合成Start/End；
- 无GAP的authenticated first-party user view逐delta原样显示Runtime收到的thinking content；同一短/超长/跨UTF-8边界tool arguments分别走Complete/Truncated branch，End的total bytes/digest对应完整arguments，render prefix从未进入canonical call、validation或dispatch；ordinary hook对同一fixture仍只见redacted projection；
- user acceptance transaction commit后丢ACK，client用同一command id retry/query只得到原turn/queue item；不同input复用同一id稳定conflict；
- `snapshot_and_subscribe()`与并发`InteractionOpened/Replaced/Closed`逐点故障注入，observer要么在snapshot看到新value，要么收到更高revision event，不能两者都漏；queue GAP/reconnect只重新level-read snapshot；
- TUI在同一Host重连时得到相同`owner_epoch`和current pending interaction；replace后旧revision/interaction resolution稳定失败；kill/takeover Host后新epoch从empty开始、request不在canonical snapshot中，旧resolution被拒绝且turn显示interrupted；
- Host与job worker并发append时event sequence按统一session lock连续提交；stale Host guard、stale job claim、错误subject slot与plugin/hook直接append均被数据库/port拒绝；
- command query只读取canonical target row，删除`terminal_command_receipts`后仍通过；
- Host writer takeover后旧controller即使持有旧socket也无法mutation，但observer仍能用不受takeover影响的history cursor读取；
- Protocol切换不引入dual query、shadow Presentation Foundation或在线root→sequence translator。

### 7.5 目标 schema

目标审查预算为24个产品表；下面是逻辑关系，不要求一项一表。超过预算需要逐项证明产品价值，但表数本身不判定正确性；也禁止用一个无约束巨型 JSON 表隐藏多个彼此独立的authority：

| 逻辑关系 | 作用 | authority |
|---|---|---|
| sessions | `workspace_id`/session metadata、lifecycle、writer generation/lease fencing、commit-ordered latest entry sequence与`latest_event_sequence`；session identity不可复用，且`UNIQUE(id, workspace_id)`供subject composite FK | canonical high-waters；没有transcript/event retention lower bound，event high-water不是row truth |
| turns | user turn、status、final entry、interruption、command/client submission id、current context binding revision pointer | canonical |
| agent_events | `StoredCommittedEvent`：`event_id`、受session约束的`workspace_id`、session-scoped contiguous `event_sequence`、namespace/type、schema version、accepted/occurred time、actor、sensitivity/projection profile、typed bounded payload，以及closed typed nullable subject-FK union；`UNIQUE(session_id,event_sequence)` | selective occurrence/audit journal；不是canonical semantic authority，也不是TUI wire DTO |
| turn_context_binding_revisions | turn-local immutable revision ordinal、FULL_HISTORY/SNAPSHOT base union、exact snapshot/source/compiler binding、safe-point install/advance；旧revision不覆盖 | semantic attribution |
| transcript_entries | user/assistant message/tool result；commit-ordered append-only sequence；assistant tool-request message是原子parent；provider-generated assistant entry绑定exact context revision与`provider_input_through_sequence`；每个正文slot使用受约束的inline-bytes/blob-FK exactly-one union | canonical |
| assistant_message_blocks | 同一assistant message的ordered text/tool calls；message/block/call ordinal与exact call identity；正文slot使用同一inline-bytes/blob-FK union；与parent同transaction插入 | canonical child；不要求独立物理表 |
| tool_execution_attempts | dispatch前commit的attempt id、unique call subject、authorization/actor、pre-generated idempotency key、一次性remote identity、cross-call retry attribution；无可变status | canonical effect journal |
| tool_results | call唯一terminal result；exact call/attempt join；closed pre-dispatch terminal是唯一无attempt分支；late reconciliation保留实际entry sequence | canonical conversation child；可并入entry payload |
| durable_jobs | background compaction precompute/post-compaction memory extraction/governance等first-party具名、确需跨Host继续的intent、safety class、aggregate state与accepted result；V1不包含generic durable extension action，也不包含任何subagent execution、yielded terminal process或terminal monitor | canonical job aggregate |
| durable_job_attempts | attempt ordinal、claim generation/lease、remote identity、terminal/unknown、result/error与retry_of | canonical work journal |
| prompt_queue_items | durable ingress order/status、command/client submission id | canonical |
| interaction_decisions | accepted user interaction或typed capability-policy decision、durable subject、command id、redacted disposition/keyed commitment；不保存pending request或secret | canonical |
| context_snapshots | immutable bounded summary/blob ref、source range/hash、schema/compiler/prompt/model contract；unreferenced可GC，被binding revision引用后受FK保护 | semantic derived authority |
| subagent_tasks | Host接受的task/objective/profile、parent-child/turn、`execution_writer_generation`、closed current status/terminal reason；nonterminal只对该Host generation有效，没有attempt/claim/retry | canonical coordination domain；不是job/execution recovery authority |
| subagent_task_children | task下immutable message/result child、closed child kind、ordinal/content slot与stable child id；message/result必须各自可被exact composite FK引用 | canonical coordination child；可共用一张typed table，不是第二task aggregate |
| memory_candidates | append-only typed proposal、origin/source/evidence与stable candidate identity；foreground `remember_*`随tool result transaction接受，automatic extraction随其job attempt接受 | durable memory work intake；不是accepted memory |
| memory_governance_decisions | closed skip/submit/correct/merge/supersede/contradict decision、exact candidate/job-attempt与accepted fact/relation lineage | durable domain decision；不保存governance execution state machine |
| memory_facts | accepted memory与既有superseded/stale lifecycle | canonical |
| memory_relations | 必要graph relation | canonical |
| blobs | purpose-neutral immutable content；canonical logical-byte digest/size、media type、semantic codec；内部compression/encryption不改变descriptor；所有domain使用FK与ON DELETE RESTRICT | canonical shared storage boundary |
| search_indexes | 可重建全文/vector索引 | derived、non-gating |
| schema_migrations | physical schema version | infrastructure |

V1必须把`subagent_tasks`保留为Host-owned canonical coordination relation，不能把它建模为`durable_jobs` type，也不能同时拥有另一份execution state。message/result child必须拥有数据库可引用的stable identity：可以各自成表，也可以共用带closed child kind的`subagent_task_children`，但不能藏在无法建立exact FK的task JSON payload。无论哪种布局，都不得出现background flag、attempt/claim/lease/checkpoint列。

每个可由Protocol呈现的canonical content slot必须由数据库约束为exactly one of `inline canonical bytes`或`blob_id`，不能同时为空或同时存在。blob branch以普通FK引用`blobs`，slot row保存或可无歧义join出相同的digest/size/media type/codec；entry/block immutable且blob删除受`RESTRICT`保护。`CanonicalBlobReference`只是Gateway从这条canonical edge派生的wire descriptor，**不是新relation、projection或identity owner**。job/memory/context/audit可复用同一blob publication，但只有closed transcript content slot进入7.4.2.1读取端口。

目标`agent_events`不沿用当前schema中的transcript-prefix accumulator、ledger continuity accumulator、materialization account、consumer horizon、candidate fingerprint或confirmation证明列。它只需要能稳定解码已接受occurrence、按session/event sequence执行session-lifetime查询，并在单次observation超budget/schema不兼容时返回GAP。canonical owner在同transaction写subject row和event；event payload不得复制完整canonical message、tool result或secret，只保存该transition的typed/redacted最小event-time字段。Gateway按7.4.2在bounded read transaction中把它与exact canonical subject组合成`CommittedObservationProjection`；该projection不持久化。没有对应产品occurrence的canonical maintenance mutation可以写0条event；一旦某transition承诺通过committed observation对用户可观察，就必须在该transaction写至少1条event，不能事后best-effort补写。

V1物理schema冻结closed subject union，不允许自由`subject_kind`/`subject_id`字符串，也不增加一张`canonical_subjects`间接identity/proof表：

~~~text
EventSubject = exactly one of
    subject_turn_id
  | subject_entry_id
  | subject_tool_attempt_id
  | subject_job_id
  | subject_job_attempt_id
  | subject_queue_item_id
  | subject_interaction_decision_id
  | subject_context_binding_revision_id
  | subject_subagent_task_id
  | subject_subagent_message_id
  | subject_subagent_result_id
  | subject_memory_fact_id
  | subject_memory_relation_id
~~~

V1 core family到slot的方向也冻结，不留给publisher临场选择：

| occurrence family | 唯一subject slot |
|---|---|
| user/assistant/tool-result message accepted、user steer accepted | `subject_entry_id` |
| turn completed/interrupted | `subject_turn_id` |
| tool attempt accepted、remote identity published、tool outcome unknown | `subject_tool_attempt_id` |
| prompt queued/consumed/cancelled/rejected | `subject_queue_item_id` |
| capability/approval/plan/MCP accepted decision | `subject_interaction_decision_id` |
| compaction/context revision adopted | `subject_context_binding_revision_id` |
| job queued或aggregate terminal | `subject_job_id` |
| job attempt accepted | `subject_job_attempt_id` |
| subagent task accepted/status accepted | `subject_subagent_task_id` |
| subagent message accepted | `subject_subagent_message_id` |
| subagent result accepted | `subject_subagent_result_id`；若同一结果另行进入conversation，还产生独立entry occurrence |
| memory fact accepted或closed lifecycle change | `subject_memory_fact_id` |
| normalized memory relation accepted | `subject_memory_relation_id` |

每个variant都是nullable typed column或closed composite column group，row-level `CHECK`要求exactly one variant非空；namespace/type/schema-version到subject variant的closed映射也由migration生成数据库`CHECK`，不能只在Python serializer校验。若message/result物理共用`subagent_task_children`，event还必须保存由event type决定的literal child kind，并以`(session_id, child_id, child_kind)` composite FK引用唯一child；payload中的ordinal/id不算subject integrity。`agent_events(session_id, workspace_id)`以composite FK引用`sessions(id, workspace_id)`，使workspace key不是caller自由输入。session-owned subject使用`(session_id, subject_*_id)` composite FK指向目标表的`(session_id, id)`；job/job-attempt列指向immutable `origin_session_id`组合键，因此没有origin session的global work不能伪装为session event；workspace-owned memory subject使用`(workspace_id, subject_memory_*_id)` composite FK。所有subject FK均`DEFERRABLE INITIALLY DEFERRED ON DELETE RESTRICT`，新subject variant必须显式schema migration、registry version与兼容性测试。错误slot、错误child kind、跨session/workspace引用、已删除subject和不存在subject都必须由数据库拒绝；closed domain adapter不能把这一责任降级为自由字符串约定。V1 core没有session lifecycle event，因此session只是journal owner和FK scope，不是一个多余的`subject_session_id` variant。

FK只保证audit reference不悬空、不串domain；它不把event升级为canonical authority。consumer仍必须读取exact subject row才能回答内容/current state，不能因event存在就推断subject的semantic fields、status或完整正文已经是什么。

V1与目标schema在普通运行中都不删除canonical transcript、memory subject或selective `agent_events`，也没有transcript/event prefix-retention transaction或retention lower bound；canonical subject与其occurrence journal均随session lifetime保留。未来若产品另行引入整会话删除、memory遗忘或审计归档，必须以新ADR/schema migration定义event/blob/legal-hold顺序；不得以`ON DELETE SET NULL`留下无法解释的occurrence，也不得为删除需求反向引入subject projection owner。

`memory_facts`与`memory_relations`就是目标canonical graph的物理存储；`search_indexes`只指PostgreSQL FTS/pgvector及其必要metadata，不包含Oxigraph mirror、RDF triple table、SPARQL cache或per-surface delivery。现有最多两跳的typed relation expansion直接查询这两类canonical row；本次hard cut不以删除Oxigraph为理由扩大查询语言或图遍历深度。

V1没有`interaction_requests`关系。`assistant_message_blocks`是逻辑约束面：实现可以选择有外键/ordinal约束的child rows，或严格有界、typed且整message原子写入的payload；无论物理形状如何，都不能让单个call在parent message commit前可见或可执行，也不能用无约束巨型JSON规避call identity与ordinal唯一性。

`turn_context_binding_revisions`是为保留mid-turn compaction所需的最小semantic attribution，不是ModelCall lifecycle。每个turn的revision ordinal从0单调递增，`UNIQUE(turn_id, revision_ordinal)`；`turns.current_context_binding_revision_id`只能指向本turn且已committed的revision。initial revision必须与user/turn acceptance在同一transaction安装，避免产生“turn存在但首个provider call没有base”的中间态；后续revision只在provider safe point以writer generation新增并原子推进current pointer。base是closed union：`FULL_HISTORY`不引用snapshot并从该turn合法history lower bound开始取exact entries；`SNAPSHOT`必须引用exact `context_snapshot_id`与`source_through_sequence`。两个分支都保存compiler/schema/lowering contract。每条accepted provider-generated assistant entry必须同时引用exact revision并保存prepared-input handle中的`provider_input_through_sequence`；该cut不得早于本turn user entry sequence，且必须严格小于新assistant entry sequence。user、tool result与audit entry不得伪造这两个provider attribution字段。snapshot source upper bound严格早于turn user entry，当前turn始终作为exact delta。旧revision、其contract与所引用snapshot均不可覆盖。

`tool_results`可与transcript entry同row，但physical-attempt reference与无attempt terminal union必须受数据库约束；`tool_execution_attempts.call_id`必须唯一，显式retry的新attempt必须属于新call。若interrupted旧attempt的late exact result在后续assistant entry之后才落盘，result仍只占该call唯一terminal row并保留实际entry sequence；provider lowering逐条比较result sequence与历史assistant的`provider_input_through_sequence`，明确区分“位于当次conversation cut内”与“该assistant不可能看见的late outcome”，并只在未来cut将后者表达为typed late-effect observation。`durable_job_attempts`不得退化成`durable_jobs.attempt_summary JSON`。

### 7.6 保留、删除、合并、process-local、operational-only清单

#### 保留/重塑

- PostgreSQL verified connection、migration runner与transaction；
- sessions、turns、global blobs；
- prompt_queue_items，但去掉独立account/checkpoint ownership；
- working_context_summaries重塑为immutable context_snapshots + turn-local immutable binding revisions；
- tool_execution_records重塑为tool_execution_attempts + exact tool results；
- memory_nodes/relations/governance事实重塑为PostgreSQL `memory_facts`/`memory_relations`；现有FTS、pgvector、direct-edge与bounded两跳recall行为保留，后续schema减法不得改变这些产品能力；
- Host存活期间已接受的terminal completion/termination summary、command/tool attribution与必要输出引用；不保留可重绑launch token、PID owner、monitor cursor或live process row；
- background compaction precompute、post-compaction memory extraction等真正后台 job；当前turn为了下一次provider call执行的safe-point compaction仍是process-local foreground operation，只在成功时提交immutable snapshot/revision；
- durable_jobs重塑为job aggregate，并增加窄durable_job_attempts lineage；
- `agent_events`重塑为带closed typed subject-FK union的selective committed occurrence journal；canonical owner同transaction写入；TUI/Inspector/eval显式读取bounded `CommittedObservationProjection`，ordinary post-commit hook只从registration cut后best-effort观察；reopen不replayevent；
- read-time `CommittedObservationProjection`及其`EventOnly`/`ImmutableEntryProjection`/`CurrentControlProjection` wire union；它没有table、cursor、checkpoint、repair或独立retention；
- snapshot/history/observation共用的`ObservationContent = InlineContent | CanonicalBlobReference`，以及Gateway唯一的bounded、stateless、逐请求鉴权`ReadCanonicalContent` port；它只读closed canonical transcript content edge，不暴露raw blob/private URL，也没有download receipt、lease、cursor、projection、repair或delivery event；
- normalized且未coalesce的Text/Thinking/Data/ToolCall与ToolResult Start/Delta/End及独立bounded `LiveAgentEventBus`；authenticated first-party用户视图对retained raw thinking原样投影、对tool arguments返回closed complete/truncated DTO；ordinary extension projection默认redacted，privileged字段受具名capability/lease控制；
- process-local `SessionLiveControlSnapshot`与`InteractionOpened/Replaced/Closed` typed events；`owner_epoch/live_revision`只服务same-Host snapshot-subscribe线性化，不落durable serializer/schema；
- typed live/post-commit/operational hook registration，以及与普通hook物理分离、V1唯一的`ToolDispatchAuthorizationPolicy` resolver；
- stable primary key、unique constraint、blob hash与foreign key；
- structured operational logs/traces。

#### 目标删除的生产文件/子系统

在其消费者先切走后，目标物理删除：

- [segment.py](src/pulsara_agent/llm/segment.py#L1) 的durable `ModelStreamSegmentAccumulator`、coalescing policy/event candidate与durable Start/Segment/End classes；由不import durable层的process-local assembler和Start/Delta/End live protocol取代；
- [raw_provider.py](src/pulsara_agent/llm/raw_provider.py#L1) 的7类`RawProvider*` union，以及 [drafts.py](src/pulsara_agent/llm/drafts.py#L1) 中逐delta的`ProviderTransportSemanticDraft`/`SanitizedProviderSemanticEnvelope`重复协议；保留并迁入单一adapter→live边界的sanitization、ordering、size与secret校验，以及独立的typed terminal/usage result；
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
- [oxigraph.py](src/pulsara_agent/graph/oxigraph.py#L1)、Oxigraph surface handler/registry/worker、`CanonicalMutationSurface.OXIGRAPH`、`oxigraph_url` settings/wiring、Inspector health、Oxigraph contracts与全部unit/integration/dogfood fixtures；不保留optional或offline adapter；
- replay中只为旧 event grammar服务的assembler/reducer；
- Inspector中展示candidate/receipt/checkpoint owner的旧路径。

context audit的产品决策已在决策19关闭：上述逐call durable owner全部目标删除。显式debug/采样若保留，只能复用短TTL disposable diagnostic artifact与独立process lifecycle，不能保留旧foreground event、materializer/repair/GC authority或close semantic completion gate；迁移期间只要旧physical task仍使用session资源，bounded cancel/join就保留到该owner物理删除。

#### 目标删除的表

reset-only后删除下列旧表；`agent_events`不删除，而是以selective schema重建并只接受`CommittedAgentEvent`：

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
- canonical_mutation_surface_deliveries，以及只为Oxigraph/search/vector per-surface delivery存在的sequence head、target head、repair/decommission rows；
- 与old EventLog write protection专用的runtime_write_protected_relations。

canonical_mutation与memory表不在第一轮一刀切删除：先确认哪些row是用户长期事实，再把它们压入memory_facts；派生的sequence head、surface delivery、migration binding plan/page/receipt随后删除。`canonical_mutations_v2`若在memory fact/relations与PostgreSQL index maintenance切换后不再有独立产品消费者，也随Stage 4/5删除，不能只因曾驱动Oxigraph而保留。

#### 合并

- runs并入turns；session级run统计可query；
- tool_result_artifacts并入global blobs + transcript/tool-result FK；
- terminal completion/termination若被产品接受，合并进其canonical conversation/effect subject与selective occurrence；process-local monitor不拥有独立durable identity；
- durable projection jobs与明确承诺跨Host完成的compaction/background work统一为durable_jobs + durable_job_attempts；当前turn为满足下一provider budget而同步等待的safe-point compaction不创建job row；
- queue content reference可并入prompt_queue_items或global blob FK；
- memory candidate/governance decision在产品允许时压成事实+decision lineage，不保留每步worker transition。

#### process-local

- adapter-local vendor SDK objects；跨transport port只允许normalized且未coalesce的Text/Thinking/Data/ToolCall与ToolResult Start/Delta/End、typed terminal/usage result，随后进入bounded provider/tool-result assembler、独立live bus、token streaming与ephemeral UI coalescing；
- yielded terminal `process_id`、`subprocess.Popen`/PTY/pipe、output journal、monitor registration/cursor、progress notification与TUI attachment；全部绑定当前Host owner lease，close后失效且不跨Host replay/rebind；
- live/post-commit/operational callback、registration lease、registration cut、per-observer bounded queue、bounded live snapshot与`LiveGap`/`HookGap`；ordinary post-commit hook没有durable cursor、自动catch-up或restart replay；callback/recorder/live owner不得进入event metadata；
- foreground tool execution和parallel batch；
- pending approval/plan/MCP request、live interaction id、等待future，以及`SessionLiveControlSnapshot`、`owner_epoch/live_revision`、`InteractionOpened/Replaced/Closed`和atomic snapshot-subscribe owner；
- context compiler工作树与provider request builder；
- capability resolution/exposure与permission evaluation结果；只有最终批准/拒绝有审计需要时写turn metadata；
- resource semaphore、connection lane、physical byte charge；
- live reducer/view cache；
- TUI render tree、spinner、progress、scroll state；
- close cancellation task group；
- 全部subagent activation task、child `RuntimeSession`/capacity reservation/MCP binding、partial live output与executor；只有Host接受的task/message/result/status落canonical rows；
- retry/backoff for transient provider calls within one process generation。

#### operational-only

- ModelStart/End timestamps、TTFT、token deltas、stream assembly cursor与coalescing统计；
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

分类清单的硬边界是：process-local不等于“untyped”。LiveAgentEvent与OperationalEvent仍有独立schema/version和测试，只是不进入PostgreSQL historical registry；反之，能序列化也不表示应该durable。

### 7.7 应删除或改写的测试

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
- interaction recovery测试中跨Host恢复pending request、suspended run continuation、resume link/receipt与reconciliation contract；改写为same-Host owner-epoch/revision atomic snapshot-subscribe、replace/stale resolution拒绝和crash→interrupted test；
- Python terminal protocol中要求Presentation Foundation root/head/retention lease/contract fingerprint的snapshot、page、GAP测试；
- Go `clients/terminal/internal/presentation` 中root-indexed cache、confirmed-root rank spine与v2 GAP rebuild contract测试，改写为v3 sequence/page语义。
- model stream测试中durable Start/Segment/End event数量、seal reason、segment policy/fingerprint、terminal projection replay与segment recovery测试；改写为live Start/Delta/End ordering、immutable Start、single assembler、End final frozen block、completed draft原子commit、partial crash丢弃和observer GAP/detach测试；
- Oxigraph GraphStore、surface materializer、required URL、Inspector health、SPARQL round-trip、delivery retry/dead-letter与PostgreSQL/Oxigraph parity测试；改写为“无Oxigraph配置/进程/网络仍通过全部memory tools”及PostgreSQL FTS/vector/direct-edge/bounded两跳行为测试。

必须保留或重写为行为测试：

- user input commit先于model call；
- user acceptance commit ACK丢失后同command id只返回原canonical target；不同input复用同id为conflict；
- final assistant transaction唯一；
- mixed text + 全部ordered tool calls作为一个assistant message原子commit先于任何effect；
- 每个physical invoke前先committool attempt；call无attempt=not_dispatched，attempt无result=outcome_unknown且不重试；
- parallel tool results精确绑定attempt；全部calls terminal后才follow-up，provider lowering按call ordinal而非完成顺序；
- crash => interrupted；
- provider delta只能在adapter内被解码/清洗为typed LiveAgentEvent并影响单一assembler；只有completed draft进入canonical assistant transaction，仓库中不存在独立`RawProvider*`/semantic-draft协议或durable stream segment carrier/writer；
- authenticated first-party用户在无GAP的live delivery中看到原样thinking delta，tool arguments短则完整、长则显式截断，截断DTO不参与validation/dispatch；ordinary hook只收到typed/redacted projection，first-party debug/unredacted-argument S2 capability/lease与S3 non-serialization negative tests通过；hook sleep/exception/timeout/overflow不阻塞provider、不否定canonical commit；ordinary post-commit hook只从registration cut后best-effort投递，overflow detach/GAP且没有journal自动补投；V1 generic可靠extension action为0，future action只能按独立ADR成为具名deterministic job；
- checkpoint/audit/UI failure不影响reply；
- 不配置或运行Oxigraph时，memory_search/get/explain、governance、direct relations与现有两跳graph recall全部正常；production import/config/schema不含Oxigraph；
- durable job aggregate/attempt restart、retry lineage与remote identity；
- stale Host writer generation mutation被数据库拒绝；
- Host takeover不影响合法background claim result；worker不能直接写transcript，当前Host显式accept job result；
- non-idempotent job lease丢失=>outcome_unknown且不重试；
- context snapshot source/contract一致、source upper bound早于所属turn user entry，且snapshot commit、binding revision install/advance与GC-unreferenced都不改变transcript entry或epoch；rematerialization精确拼接post-source/current-turn delta，被revision引用的snapshot不可GC或重新生成替换；
- global blob publication保证所有canonical FK只指向已验证immutable bytes，24小时orphan GC不删除任何referenced blob；
- Protocol v3 snapshot/page使用一致MVCC read cut并返回`event_sequence_cut`；Gateway把每条stored event与closed-FK exact subject组合为bounded `CommittedObservationProjection`，assistant delta返回ordered `ObservationContent`，blob-backed branch经唯一read port hydrate；suffix超budget/schema GAP、session-lifetime event query、provider generation/GAP与control owner-epoch/GAP测试通过，ACK unknown query只读canonical target；
- pending interaction通过带`owner_epoch/live_revision`的atomic `snapshot_and_subscribe()` same-Host level-read；open/replace/close竞态不漏观察，Host crash/takeover后不出现在canonical snapshot且旧resolution失败；
- `StoredCommittedEvent`的exactly-one typed subject FK、event-type/slot映射、same-session/workspace composite FK与`ON DELETE RESTRICT`由数据库negative tests覆盖；Host/job guard并发append连续，stale claim与hook/plugin append被拒绝；
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
5. 删除obsolete event class/table/test；selective committed与process-local live protocol按新base保留。

physical lifecycle遵循另一条不可跳过的顺序：stop admission → cancel/terminate → bounded join不再访问session-owned resource → release DB pool/artifact store/executor → 删除owner后再删除对应close await。de-gate只说明operation成功与否不影响canonical语义，不授权在physical operation仍存活时释放其依赖。

禁止的过渡方式：

- 同一turn双写旧universal EventLog和canonical schema；canonical row与对应selective committed event的同transaction共写是目标contract，不属于dual authority；
- 新建 compatibility reducer把旧event实时翻译到新表；
- 用新的stable candidate/receipt/fingerprint包住旧owner；
- 为了rollback让两个authority长期共存；
- 先删owner但保留所有依赖，然后再造临时repair owner。

允许的rollback只有：停机、再次complete reset并回退到上一发布版本的binary/schema。代码路径内部不承担双版本兼容，也不承诺恢复cutover前或cutover后的用户数据。

另加一条 **coherent authority cut rule**：凡是第一次把某类生产 canonical fact写入新schema，该release必须同时具备写入、open/resume、context compilation、Inspector、TUI snapshot/page/reconnect、compaction source读取与writer fencing。旧owner的物理文件可以在下一阶段删除，但新数据不能先于所有正确读取/恢复语义进入生产。

这条规则约束production activation，不禁止dormant construction。schema/repository、test-only runner、reader、Protocol/Go consumer可以分多个PR进入tree，只要普通Host composition不可达、没有feature flag按session启用、没有dual-write，并且每个PR独立全绿。

vertical slice只能沿产品模式切，不沿运行后才知道的model outcome切。启动前明确禁用全部tool的`NO_TOOLS`模式可以作为pre-production spike；普通Agent中的“这一次模型碰巧只回text”不能作为authority分流条件。

### 8.2 阶段总览

| 阶段 | vertical slice | 首要删除结果 | 独立 correctness gate |
|---|---|---|---|
| 0 | 产品语义与并发约束冻结 | single Host writer、两类closed append guard、exact 26 committed core/13 subject slots、exact 49 formal AgentEvent subscription-only extension、stored/observation/content DTO、registration/version/lease、V1 generic durable extension action为0、唯一无rewrite tool policy、live control、partial、subagent、terminal、audit、stream、memory physical store与candidate-first async governance决策不再漂移 | 决策可转成行为test；151 inventory及A39/B25/C16/D71逐项处置可重复，26类core mapping、49类formal registry、0 custom publisher/action与1 policy kind可生成结构guard |
| 1 | 低风险真减法 | 逐call durable audit停写；acceleration/presentation failure解除semantic latch | 故障不否决canonical commit；旧owner physical I/O仍在资源释放前bounded退出 |
| 2 | conversation kernel + selective committed journal + minimal job kernel单次production activation | canonical user/assistant/tool/context rows与同transaction committed occurrence一次激活；closed subject/append authority、read-time observation/content projection、canonical content range read、provider/tool-result/live-control bus、subscription-only hook registry与`ToolDispatchAuthorizationPolicy`启用，durable stream停写；PostgreSQL-only memory candidate intake/async governance、first-party background work与TUI v3同步切换 | 一个turn一个semantic authority；event只是occurrence truth；completed draft才落库；extension不能publish type或声明reliable；policy timeout/headless不dispatch且闭合result；memory proposal先candidate后governance且foreground不等待；无old/new job bridge；snapshot+observation+content hydrate+live、attempt-before-effect、crash/reconnect/fencing全通过 |
| 3 | 删除exact execution recovery与derived authority | durable stream segment/recovery、reducer/checkpoint/Presentation Foundation物理删除；close压缩；typed live stream与selective journal继续工作 | 无durable delta/temporary RuntimeSession；hook failure不反向传播；3段close；0 reducer barrier |
| 4 | 后台handler迁移收口与legacy graph删除 | 复用Stage 2已激活的窄job aggregate + attempt journal；迁移剩余被显式禁用的handlers并删除旧projection-job与Oxigraph surface graph | stale claim拒绝；attempt lineage完整；所有产品handler只有新authority；unsafe lease loss=>unknown；Oxigraph代码与配置不可达 |
| 5 | universal EventLog退役 | 151类universal registry、execution replay/proof schema物理删除；selective `agent_events`与LiveAgentEvent package按exact 49 core types定型 | production import graph无旧execution authority；custom AgentEvent publisher为0；committed/live/operational隔离guard全通过 |

### 8.3 阶段 0：冻结产品语义与基线

**目标**

把本报告的架构决策变成不可漂移的architecture gate；特别冻结以下产品约束，不实现新repair owner：

- V1 每session单Host writer，observer可多attach；
- live partial assistant与tool result默认不durable，但经adapter-local解码与sanitizer/normalizer处理的Text/Thinking/Data/ToolCall与ToolResult Start/Delta/End必须是唯一typed process-local provider protocol；
- authenticated first-party用户对Runtime实际收到的thinking delta原样可见，tool arguments按UTF-8 byte展示阈值返回closed complete/truncated DTO；这是bounded live projection而不是durable completeness承诺；
- 全部V1 subagent随Host结束而interrupted，不存在background delegation/job模式，不跨进程复活、claim或自动重新委派executor；
- terminal restart不重新查询、绑定、收养或自动重启历史process/command；只读取已经accepted的canonical completion/termination；
- V1不承诺逐model-call exact context-input audit；旧逐call durable plane目标删除，只有短TTL、best-effort debug/采样诊断可选；
- completed assistant/tool-result semantic content保留；未coalesce的semantic Start/Delta/End保留类型，独立`RawProvider*`/per-delta semantic-draft、transport coalescing segment与stream layout删除；provider/tool-result assembler与per-observer bus均bounded；
- PostgreSQL独占memory graph、FTS、pgvector与现有bounded两跳recall；Oxigraph完整删除，不保留optional/offline adapter，也不新增raw SPARQL或通用graph DSL；
- 现有五类`remember_*`只提交durable candidate，不直接写canonical memory；governance由durable job异步执行，foreground只在已有tool-result transaction内完成有界candidate append，不等待治理或index freshness，也不新增delete/forget语义；
- external side effect unknown默认禁止自动retry；
- Host writer generation与job-attempt claim generation是独立fencing domain；
- Protocol v3 canonical snapshot/page使用repeatable-read sequence cut；mutation ACK unknown由canonical row idempotency处理；
- Protocol v3 fresh attach读取canonical snapshot + `event_sequence_cut`；Gateway只在bounded read transaction中把`StoredCommittedEvent`与exact subject组合成`CommittedObservationProjection`，Go不直接解释stored payload/subject id；projection不持久化，不再新增独立durable `control_revision`；
- snapshot/history/observation使用同一个`ObservationContent = InlineContent | CanonicalBlobReference`；reference不是capability或URL，唯一`ReadCanonicalContent`按canonical byte range逐请求重验subject/session/workspace/capability与digest/size/codec，读取不产生durable state或event；
- `StoredCommittedEvent`使用exactly-one typed nullable subject-FK union与event-type/slot数据库约束；不接受自由`subject_kind/id`，也不增加canonical-subject identity/proof table；
- selective committed registry exact等于决策7的26类、subject union exact等于13种slot；`PromptRejected`不合并为cancel，subagent message/result必须引用exact child，memory candidate、derived tool outcome unknown与retryable job-attempt terminal不发committed event；
- committed append authority只封闭为`HostWriterGuard | JobAttemptClaimGuard`，所有owner按统一session SQL lock order分配event sequence；没有origin session的job不写session journal，普通hook/plugin没有append port；
- V1 pending interaction是带`owner_epoch/live_revision`与atomic snapshot-subscribe的same-Host process-local live control；Host crash/takeover后新epoch为空、request消失，accepted decision才durable；
- completed assistant tool-request message的mixed text与全部ordered calls原子commit；每个physical invoke前commit attempt；parallel results全部terminal后才follow-up；
- V1 compaction不改写transcript、不推进epoch；被binding revision引用的context snapshot exact保留；每次provider dispatch冻结exact revision与commit-ordered conversation sequence cut，accepted assistant原样保存二者；prefix/age pruning关闭，canonical transcript与committed journal按session lifetime全量保留；
- per-session entry sequence只在canonical entry transaction内按commit order分配并推进high-water；禁止预留、乱序commit或异步head；
- job aggregate与attempt lineage分表，global blob publication是所有大内容的唯一写入boundary；canonical transcript另有唯一无状态content read boundary；
- conversation rehydrate/context rematerialization/effect reconciliation/audit reproduction分名，execution replay禁止；
- Stage 1只de-gate业务完成，旧owner physical quiesce保留到owner删除。
- ordinary hook与`ToolDispatchAuthorizationPolicy`物理分离；user view与extension projection类型分离；view/capability只由Host authorization service基于authenticated scope签发/撤销，plugin不可自授权；registration使用authenticated principal、manifest-stable handler id、Host-minted process instance、current/previous projection-major window与revocable process lease，bounded queue/timeout/detach/close drain及S2/S3 projection规则全部冻结；extension只能订阅exact 49类core event，不能定义/publish formal type。ordinary post-commit hook从registration cut后best-effort，overflow GAP/detach且不自动catch-up；V1第三方durable extension action为0，future reliable action必须另立ADR成为具名job。

**删除内容**

无。只建立待删inventory和测量，不把当前复杂机制标成永久contract。

**保留不变量**

- 当前生产行为不变；
- 仓库仍可运行；
- 不触碰用户数据库。

**代码修改面**

后续实现时只允许增加静态/计数测试和metrics；本调研阶段没有修改。

**数据/reset策略**

- 冻结complete-reset manifest：Pulsara-owned PostgreSQL schema/data、shared blob namespace及derived indexes/presentation state全部在cutover清空；只保留无用户数据的test fixture与schema基线；
- 明确最终cutover从empty database/blob namespace运行新migration，不存在旧数据import/cold archive reader；
- 禁止用生产dual-write收集基线。

**独立 gate**

- 两条探针可稳定复现43/83 EventLog event量级，AST inventory稳定得到151类及14-family分布；五片逐项审计可重复得到A39/B25/C16/D71，并生成“旧151类→row/A/B/C/D→exact 26 core”的closed migration manifest；
- table、EventType、owner、close-await计数由CI脚本读取；
- 有一份明确的complete-reset与old-process quiesce runbook；没有old-data export/import、cold archive、converter或compat reader交付物；
- 单writer lease/takeover、Host/job append guard、typed subject integrity、committed observation read cut、canonical content read、live-control snapshot-subscribe、ordinary-hook registration cut、partial text/raw thinking用户可见性、tool-argument complete/truncated展示、Host-scoped subagent、terminal process、audit与unknown UX均有冻结的行为判定，不再留给实现者通过CAS/receipt猜测；
- content-live observer/shared ring/provider/tool-result snapshot/control snapshot/control observer的event+byte hard cap、tool-argument展示阈值、callback deadline和close drain可由dormant probe校准，但Stage 2 activation前必须有具名default、server hard cap与monitor；数值校准不得改变GAP后detach、provider/tool executor不阻塞或用户/extension projection边界；
- `ObservationContent`两分支、closed content locator、byte-range单位、逐请求authorization、chunk/full digest、codec/error与无durable read-state语义已经冻结；Stage 2只允许以dormant probe校准inline threshold、chunk hard cap与并发hydrate数值。
- committed registry/subject/guard与live registry使用独立生成输入：前者只能生成exact 26类、13种slot与两种guard，后者只能生成决策7的exact 23类；独立raw-provider registry/union数量必须为0，vendor SDK wire type不得进入Runtime生成输入。

**回滚边界**

无运行时变更，无需回滚。

### 8.4 阶段 1：先做低风险真减法

**目标**

在不切换canonical authority、不触碰TUI数据源的前提下，先证明所有acceleration、audit与observer failure都不能反向否决foreground语义；同时减少默认I/O和close依赖。

**删除内容**

1. 停止每model call自动offer exact context-input audit；显式doctor/debug或低比例采样只写短TTL、best-effort disposable diagnostic artifact，不提供逐call合规保证；
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

无authority schema迁移。已有audit/checkpoint/presentation数据保留到后续reset；逐call audit停写只停止自动offer，不做background backfill或online cleanup owner。

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
  -> process-local bounded ProviderStreamAssembler
       -> raw typed item -> LiveAgentEvent Start/Delta/End -> bounded TUI/Inspector/hook queues
       -> CompletedAssistantMessageDraft
  -> accepted final assistant(exact revision + frozen cut) + same-tx CommittedAgentEvent
  OR atomic assistant tool-request message(text + ordered calls)
       -> same-tx AssistantToolRequestAccepted
       -> per-call typed policy decision commit
       -> same-tx CapabilityDecisionAccepted
       -> per-call execution attempt commit
       -> same-tx ToolAttemptAccepted
       -> only then physical invoke
       -> optional first remote identity publication
       -> same-tx ToolRemoteIdentityPublished
       -> per-call terminal results(can complete out of order, exact attempt join)
       -> same-tx ToolResultAccepted
       -> all calls terminal
       -> provider safe point may install a newer immutable context binding revision
       -> next provider call freezes its own exact revision + conversation cut
       -> accepted final assistant(exact revision + frozen cut)
  -> completed / interrupted / outcome_unknown
~~~

这里合并旧路线的text slice、tool slice和最小resume slice；模型是否调用tool不再决定storage authority。

Stage 2工程上按以下顺序构建，每一步可独立PR、独立全绿，但前五步都必须保持普通Host composition不可达：

1. fresh conversation schema、canonical content slot的inline/blob exactly-one union、exact 26类selective `agent_events`、13-slot closed typed subject-FK union/event-sequence high-water、PostgreSQL `memory_candidates`/closed governance decisions/`memory_facts`/`memory_relations`与现有FTS/pgvector read models、minimal `durable_jobs`/`durable_job_attempts` schema、claim repository、job-result acceptance port、global blob contract与migration，默认dormant；新schema没有Oxigraph surface/delivery relation；
2. 只供fresh-DB tests使用的conversation runner；
3. context/Inspector/query readers；
4. Protocol v3 Python service、`ObservationContent`/`ReadCanonicalContent`与Go consumer/content hydrator；
5. isolated fresh-DB dogfood；
6. 一次complete reset + production activation，随后才允许普通Host写新authority。

不得用feature flag按session混用authority，不dual-write，不建立online EventLog translator。下面的“同时交付”指第6步activation release，不要求前五步压成一个巨大代码提交。

**activation时同时交付的不可拆工作面**

1. `sessions/turns/turn_context_binding_revisions/transcript_entries/assistant message blocks/agent_events/tool_execution_attempts/tool_results/context_snapshots/blobs/subagent_tasks/subagent_task_children/durable_jobs/durable_job_attempts/memory_candidates/memory_governance_decisions/memory_facts/memory_relations`直接schema、canonical content slot inline/blob exactly-one union、commit-ordered entry/event sequences与session high-waters、exact 26 event registry、13-slot exactly-one typed subject FK及type/slot mapping、assistant exact revision + `provider_input_through_sequence`、`(session_id, command_id)`、assistant-message/call/attempt/result pairing、subagent message/result exact child kind/FK、subagent Host-generation/status约束、job/claim fencing与final winner唯一约束；
2. session Host writer lease/generation、job-attempt claim generation，以及只接受`HostWriterGuard | JobAttemptClaimGuard`的sealed event appender；两类owner按同一session allocator lock order分配event sequence，但只在各自domain校验canonical mutation；
3. Host open时原子 acquire/takeover + 旧running turn和旧generation nonterminal subagent task→interrupted；不构造旧RuntimeSession/child RuntimeSession，不恢复provider/tool/subagent；
4. user、final assistant、完整assistant tool-request message、逐call execution attempt与terminal result、subagent task/message/result/status、turn context binding revisions/current pointer、interrupted/unknown全部走同一authority；
5. Python transcript application service以及context compiler、Inspector、compaction source、prompt/session query等全部production readers；
6. Protocol v3 repeatable-read snapshot/page/`event_sequence_cut`、bounded `CommittedObservationProjection`/budget-or-schema GAP、session-lifetime stored-event audit query、统一`ObservationContent`与stateless `ReadCanonicalContent`逐请求鉴权/range/integrity contract、provider live generation/GAP、first-party user live profile、`CompleteToolArguments | TruncatedToolArguments`、带epoch/revision的atomic session live-control snapshot/event，以及canonical-row command query/idempotency contract；
7. Go client entry-sequence cache、observation-projection reducer、bounded content hydrator/chunk与完整digest校验、能原样显示retained thinking delta且对oversize tool args显式截断的live renderer、same-Host live-control snapshot/event、reconnect/GAP/controller mutation/ACK unknown迁移；Go wire不暴露`StoredCommittedEvent`作为render DTO，也不接受raw blob id/private URL，截断argument DTO不得回流到tool execution；
8. minimal job claim/result-accept service，以及所有从新foreground/Host可以创建或消费的background capability：background compaction precompute与post-compaction memory extraction/governance；job worker只用exact `JobAttemptClaimGuard`追加origin-session occurrence，不能写transcript或subagent coordination rows；V1 job catalog不得出现generic durable extension action/tailer、任何subagent execution或yielded terminal process/monitor；当前turn所需的safe-point context snapshot generation不建job，只在成功时提交snapshot/revision；
9. adapter-local vendor SDK decoding、Runtime-owned sanitizer/normalizer、`ProviderStreamAssembler`、决策7的exact 23类`LiveAgentEvent`、带per-observer/shared-ring/snapshot event+byte hard cap的`LiveAgentEventBus`、`SessionLiveControlSnapshot`/control owner、user/extension projection constructors、Host-owned capability/lease authority、只允许订阅exact 49类core event的hook registry、V1唯一且无argument rewrite的`ToolDispatchAuthorizationPolicy` resolver及completed-draft commit adapter；ToolResult Delta使用closed text/data branch，End只携带frozen live view。live protocol不生成独立raw/draft carrier、durable candidate、segment fingerprint或terminal projection，普通post-commit hook没有durable cursor/catch-up；
10. PostgreSQL-only memory composition：现有五类`remember_*`在对应tool result transaction内先接受durable candidate，automatic extraction也先提交candidate；governance只由minimal job kernel异步claim，accepted decision再写canonical fact/relation与必要committed occurrence。memory query tools和bounded两跳recall全部指向canonical PostgreSQL query ports，settings/wiring/Inspector不再出现Oxigraph；不增加delete/forget入口或状态机；
11. complete-reset/old-owner quiesce/remote-effect non-replay、crash、user acceptance ACK loss、snapshot并发commit、committed observation exact-subject/budget GAP、blob-backed transcript exact hydrate与scope/integrity negative cases、stored subject negative constraints、Host/job并发append、notification loss、provider/tool-result/live-control overflow与epoch change、raw-thinking exact user projection、short/oversize/multibyte tool-argument display与dispatch isolation、hook failure/capability leak/no-catch-up、pending interaction takeover、mixed/multi-tool attempt/result batch、single-writer takeover、TUI reconnect、yielded terminal cross-owner denial/orderly-close kill/crash non-adoption、job claim/takeover、blob GC、stream partial-loss、PostgreSQL-only memory recall与side-effect黑盒tests。

可选的`NO_TOOLS` direct-schema spike只能在启动前明确选择且整个session无tool exposure，用来测SQL/commit路径；它不进入普通Agent生产，不产生与EventLog tool turn混合的session。

**停止产生/删除依赖**

从cutover开始，所有foreground turn停止写durable RunStart、ReplyStart、ModelStart、stream segment、terminal projection、Disposition、ReplyEnd/RunEnd、window/account/reservation、tool start/chunk/end和foreground checkpoint/audit proof event；只写canonical rows及同transaction少量`CommittedAgentEvent`。adapter在本地完成wire解码与sanitization后，provider/tool-result semantic live Start/Delta/End继续通过独立process-local bus；独立raw/draft union同时退出production。production composition同时停止构造Oxigraph、停止计划/claim Oxigraph surface delivery，也不再要求`oxigraph_url`。旧background/adapter模块可以暂时留在tree中，但只能处于production-unreachable/dormant状态；任何可被新Host或foreground创建、查询、等待或接受结果的background work，在activation时必须已使用minimal job kernel。若某capability尚未迁移，必须在同一release中从production catalog/admission明确禁用；禁止读旧job authority的bridge、dual consumer或“先写旧表再导入新conversation”。

**保留不变量**

- provider调用前user input已commit，并已通过prepared-input owner从同一MVCC cut冻结exact binding revision与`provider_input_through_sequence`；
- provider delta在adapter-local wire解码后只进入sanitizer/normalizer构造的LiveAgentEvent、bounded assembler与live bus；completed draft之前没有assistant canonical visibility，partial/crash不会留下raw/draft carrier、segment row、tool-argument prefix或可rehydrate stream identity；
- authenticated first-party user view对已成功投影的thinking delta不做摘要/redaction/内容长度截断，但仍受bounded queue/ring/snapshot与GAP后detach语义限制；tool arguments展示不超阈值时完整，超限时以UTF-8-safe truncated DTO显示，canonical call与dispatch仍使用完整validated arguments；
- 一个turn最多一个accepted final assistant；assistant+completed turn为一个transaction；
- 一个assistant tool-request message的mixed text与全部calls/ordinals原子commit后才允许任何invoke；
- 每个实际invoke前先committool execution attempt；parallel results可分别commit并精确pairing；全部calls拥有terminal result后才进入follow-up provider call，lowering按call ordinal；
- message commit后call无attempt在rehydrate时解释为not_dispatched；attempt无result才解释为outcome_unknown；二者都不自动invoke；message transaction未commit则任何call都不得已执行；
- open只做conversation rehydrate，不做旧execution replay；
- context rematerialization对interrupted multi-tool message按原call ordinal生成provider-only `ProviderToolResultClosure`，不向provider发送悬空call，不伪造canonical result；
- turn可在provider safe point追加immutable context binding revision并推进current pointer；旧revision不可覆盖，每条accepted assistant message绑定exact revision并原样保存本次pre-dispatch cut；assistant commit不得重读latest sequence；所有被revision引用的snapshot不可替换/GC，unreferenced snapshot可按retention删除；
- pending interaction不在canonical snapshot/open中恢复；same-Host通过`owner_epoch/live_revision`的atomic snapshot-subscribe重取，replace/takeover后旧resolution失败；
- subagent task/message/result/status只由当前Host接受；全部physical child state只在进程内，orderly close bounded cancel/join后把nonterminal task置interrupted，takeover完成同一幂等收口；reattach不resume/requeue，重新委派创建new task id；
- 同一session任一时刻只有当前writer_generation可以提交Host-owned mutation；background job worker不在该domain内且不能写transcript。两者只有各自有效`EventAppendGuard`才能在统一session lock下追加committed occurrence；没有origin session的job不写session journal；
- foreground-reachable durable background work只使用Stage 2 minimal job kernel；不存在旧job result到新conversation的bridge，未迁移capability不可出现在production catalog/admission；yielded terminal process/monitor明确不属于durable background work；
- `memory_search`、`memory_get`、`memory_explain`、governance与GraphCandidateService只读PostgreSQL；两跳上限及结果语义保持不变，Oxigraph缺失不是degraded mode而是目标composition；
- `remember_*`成功只表示candidate已durable accepted；candidate commit前不能对外承诺`proposed`，governance只能在candidate commit后异步claim。reply、turn completion与Host close都不等待治理或FTS/pgvector追平，candidate在decision commit前不进入normal recall；
- 同command id重试不创建第二个turn/queue item，query不依赖generic receipt；
- TUI snapshot metadata/control/rows与`event_sequence_cut`来自同一MVCC cut；history page绑定`session_id + cut_sequence + entry_sequence`；committed observation由stored event与exact subject在bounded read cut内组合，suffix超budget或live generation/owner epoch变化即按各自GAP规则refresh；entry content统一为inline或canonical reference，blob-backed bytes只经bounded无状态读取逐次鉴权并验digest，不持有observation transaction；
- canonical row与带exact typed subject FK的committed event同transaction；post-commit/live/operational hook failure、timeout或overflow不改变row、turn或provider；ordinary post-commit hook从registration cut后best-effort，miss/overflow不触发journal自动补读；
- TUI、context、Inspector、compaction看到同一个canonical顺序；compaction不改写该顺序或epoch；所有大内容只通过global blob FK发布，TUI只通过closed canonical content edge读取，不能按任意blob id访问。

**代码修改面**

- storage migration/persistence ports与composition；
- Host submit/open/resume/lease takeover；
- runtime/run_entry.py、runtime/agent.py、runtime/tool_loop.py；删除`llm/segment.py`的durable assembly并新增process-local `llm/stream_assembly.py`；
- settings.py、host/production_composition.py、runtime/wiring.py、graph/canonical memory ports、Inspector与memory query wiring，移除全部Oxigraph配置/构造/读取；
- context compiler、Inspector、context snapshot producer/source readers、binding revision repository、pre-dispatch immutable prepared-input handle与safe-point pointer advance；
- minimal durable job aggregate/attempt repositories、claim lane、result-accept port，以及foreground-reachable background-compaction/memory-extraction/具名extension handlers；subagent task repository与process-local child runner按决策24收口，terminal process/monitor走Host-scoped process-local manager，foreground safe-point compaction走独立process-local调用与terminal snapshot commit；
- purpose-neutral blob repository、canonical content edge/FK publication、bounded range read与orphan GC；
- terminal_protocol gateway/schema/generated carriers、`ObservationContent`/`ReadCanonicalContent`、command query与canonical target lookup；
- `clients/terminal` protocolvalue/presentation/state/cache/app与bounded content hydrator；
- direct-schema test factories和故障注入harness。

**数据/reset策略**

complete-reset production cut；同一turn绝不双写。maintenance window先停止旧ingress，cancel/join或fence全部旧Host/worker/monitor/subagent physical owner，按runbook处理仍在外部运行的remote process/effect；随后清空Pulsara-owned PostgreSQL schema/data、shared blob namespace及derived indexes/presentation state，从empty store运行新migration并一次activation。旧session/transcript/memory/event/job/attempt/context/audit/blob一律不导入，不生成`imported_interrupted`、old→new identity map、offline converter或只读cold archive。新Runtime只看cutover后创建的facts，绝不因reset重新dispatch任何旧effect。

**独立 gate**

- 普通Agent暴露tool后，模型动态选择纯text或tool，两条路径都只写direct schema；
- steady-state（无需新compaction）text目标2个canonical transaction，one-tool physical happy path目标5个、若remote identity需要在result前单独接受则6个；完整turn transaction仍按`2+B+C+E`与remote-publication mutation单列；需要新context snapshot时分别报告snapshot commit与binding revision install/pointer advance。selective event固定text 3条、one-tool 7基线/remote identity时8条，全部与这些canonical mutation共写，额外event transaction为0；
- 第一个新schema running turn在任意delta crash后即可由同版本open原子标interrupted；
- 每种provider delta序列在完成前kill都只留下user/running→interrupted；完成后只产生一个byte-bounded completed draft/canonical message，durable segment event、segment policy与terminal projection计数均为0；
- assistant tool-message/attempt commit/physical invoke/每个result/final的crash windows符合7.2；message或对应attempt未commit时该external invoke count必须为0；
- user/queue acceptance commit后丢ACK，同command id retry/query返回唯一target，不同input冲突且无receipt row；
- 两个Host竞争同session时只有一个generation提交成功，takeover后旧writer所有Host-owned mutation失败；合法background claim不受影响；
- Protocol v3 Python/Go contract、repeatable-read snapshot、cut-bound page、committed observation projection/GAP、canonical content hydrate、provider generation/GAP、session live-control epoch/revision与reconnect通过；entry/event/control edge notification丢失都不影响相应level-read最终可见性；
- 并发commit故障注入不能产生high-water与suffix/control不属于同一read cut的response；
- canonical entry sequence只能在entry transaction内按commit order分配；并发tool result/final commits、rollback与ACK loss不能产生低sequence晚提交、published high-water空洞或cut后entry落入旧cut；
- queue/turn/decision/task/job等决策7承诺的control-only transition不追加entry时，仍在同transaction追加对应typed committed event并推进event high-water；V1 session detach/close不发core event，也不生成独立control cursor/history；
- fresh schema的committed registry逐项exact匹配决策7的26类，数据库type→subject slot/guard矩阵逐项匹配；`PromptRejected`与`PromptCancelled`分别由系统拒绝和显式取消transaction产生，不能共享一个模糊terminal type；
- tool-request message commit同时推进entry/event sequence；tool attempt insert追加`ToolAttemptAccepted`；terminal tool result与turn interrupted分别追加对应committed event；snapshot在同一MVCC cut读取entry/event cut、attempt、result与turn facts；
- 新assistant/tool-result stored event只保存occurrence与exact subject FK；Gateway在同一bounded read transaction形成带ordered `ObservationContent`的`ImmutableEntryProjection`，Go对inline直接渲染、对blob reference经唯一bounded read port hydrate；subagent message/result分别引用exact child slot。错误event subject slot/child kind、跨session/workspace subject、missing/deleted subject全部由数据库拒绝；
- retryable job attempt失败只更新immutable attempt lineage并发C，不产生`JobTerminalAccepted`；只有aggregate进入closed terminal state时由current claim owner原子写job row与该event。memory candidate commit、governance batch准备和skip同样不生成memory A；只有accepted fact/lifecycle/relation使用三类memory core event；
- compaction model完成但binding revision adoption transaction失败时不产生`CompactionAdopted`；subagent physical child启动、phase、suspend、delivery与run terminal不产生committed event，只有accepted task/status/exact message/result child产生四类coordination occurrence；
- 强制blob-backed assistant/tool-result以多chunk和跨UTF-8边界读取后仍exact render；每个request重验canonical content edge与capability，raw blob id、篡改descriptor、跨scope、revoked capability稳定拒绝，missing/corrupt bytes显式报错且canonical commit不变；读取期间数据库transaction不跨storage I/O，durable download state/event数为0；
- Host与多个job worker并发append得到无洞、按commit order的session event sequence；stale Host/job guard、无origin-session job与hook/plugin appender调用在写canonical/event前失败；
- pending interaction在same-Host reconnect由atomic snapshot-subscribe重新读取；opened/replaced/closed竞态要么进snapshot要么成为更高revision event；Host kill/takeover后新epoch为空、canonical snapshot不含request、running turn interrupted且旧resolution fail closed；
- ordinary post-commit hook注册前或commit-tap丢失的event允许不投递，queue overflow只产生process-local GAP并detach；registration不能声明reliable/catch-up，V1没有第三方durable extension fixture；
- ToolResult live Start/Delta/End在canonical result commit前均不能推进entry/event high-water；End后kill只留下既有call/attempt与interrupted/unknown，reopen不得把frozen live view合成为历史result或`ToolResultAccepted`；
- mixed text + 2个以上calls只出现为一个完整assistant message；attempt/results乱序/部分commit时pairing和call ordinal稳定，未全部terminal绝不follow-up；
- restart后新turn的provider input对已知result、call-without-attempt、attempt-without-result按原ordinal形成合法closed message sequence；synthetic closure只在provider lowering中存在，canonical `tool_results`数量不增加；
- initial revision 0与user/turn acceptance原子可见；initial与mid-turn context snapshot/revision commit都不改变append-only transcript或history cut；目标不存在transcript epoch/entry retention lower bound；只有safe point能新增后续revision并推进turn pointer，source upper bound必须早于turn user entry并精确拼接全部current-turn delta；每条accepted assistant绑定当时revision与pre-dispatch `provider_input_through_sequence`，二者都来自同一prepared-input handle；旧revision/被引用snapshot的删除或replacement被数据库拒绝；
- outcome_unknown的旧call只能通过新turn/new call重试；旧remote outcome晚到时只能填充旧call尚不存在的唯一result，旧turn与已accepted assistant attribution不变；通过result sequence与每条assistant cut比较后，future lowering只对明确late的outcome生成typed late-effect observation；
- background compaction precompute或post-compaction memory extraction/governance从新Host创建work后，job/attempt/result只出现在minimal job kernel，Protocol v3/context/Inspector不需读旧projection-job authority；V1 generic durable extension action、任何subagent execution、yielded terminal process/monitor与foreground safe-point compaction都不创建job；
- subagent在Host存活时可产生已接受task/message/result；orderly close或takeover后所有旧generation nonterminal task均为interrupted，accepted children不丢失，job/attempt/claim/recovery row数量为0；显式重新委派只创建new task id；
- 同一subagent task连续接受两条message与一个result时，三条occurrence分别引用各自exact child FK；不得都退化到task aggregate。Host close/takeover写`SubagentTaskStatusAccepted(status=interrupted)`，不得重用cancelled或生成`SUBAGENT_RUN_*`替代type；
- yielded terminal handle只允许同一Host owner操作；跨owner lookup失败，orderly close kill/join全部owned process group并使handle失效，crash后的新Host不adopt或重新launch旧process；
- foreground `remember_*`在tool-result transaction任意insert点kill时，candidate与tool result要么都不可见，要么都可见；governance worker永远看不到未commit candidate，且其slow/failure不延迟reply/turn completion。automatic extraction也必须先提交candidate再由后续governance job处理；
- 环境不提供Oxigraph URL/进程时production Host、全部memory tools、governance、FTS/vector/direct-edge与bounded两跳recall端到端通过；仓库运行期间对Oxigraph网络连接次数为0；
- canonical row永不引用missing/unverified blob；24小时orphan GC与late install竞态由FK/RESTRICT稳定结算；Protocol只能经closed content edge读blob，不能把reference/private URL当授权；
- context、Inspector、compaction读取不需要merge EventLog与transcript。

transaction/row计数是架构预算；单authority、唯一commit、crash语义、fencing与reader一致性才是correctness gate。

**回滚边界**

只能停机、再次complete reset并整体回退binary/schema；不让旧binary读取新schema，不保留v2 server translator，不以dual-write或data snapshot作为rollback机制。

### 8.6 阶段 3：删除 exact execution recovery、derived authority并压缩 close

**目标**

阶段2已让所有foreground writer/reader转到direct schema；本阶段按“先断依赖、再删owner、最后删表/test”的顺序，物理删除旧exact-recovery图和Presentation Foundation，把Host close收缩为对真实process-local execution与canonical writer负责。

**删除内容**

- `llm/segment.py`、segment policy/fingerprint、durable Text/Thinking/Data/Tool Start/Segment/End carriers/serializer entries/reducers及其terminal projection joins；Stage 2的`llm/stream_assembly.py`与`LiveAgentEventBus`保留Text/Thinking/ToolCall和ToolResult Start/Delta/End typed vocabulary，是唯一live stream owner；
- model stream/control disposition recovery、dormant RunOwner与recovered terminal successor；
- pending interaction request的suspended-run recovery、resume link/receipt、MCP continuation replay与reconciliation owner；accepted decision row保留；
- stable RunFinalization candidate/repair-driven retry与temporary RuntimeSession teardown；
- subagent recovered occupancy、graph checkpoint、child coroutine/`RuntimeSession` recovery，以及teardown generation/retry/reconciliation lineage；canonical task/message/result/status保留；
- ToolExecutionStableCandidateOwner、terminal/suspension confirmation、physical handoff与tool terminal projection；
- 9个foreground committed reducer、post-fold receipt、committed reducer repair；
- per-reducer runtime checkpoint maintenance、authority materialization与foreground projection jobs；
- transcript/tool/provider-input/final-output projection作为第二authority；
- terminal Presentation Foundation、root/head/retention owner、Protocol v2 snapshot/page/GAP server、ControlProjectionCursor/per-section source version/fingerprint与Go root/control cache；目标保留canonical snapshot的entry/event cut、read-time `CommittedObservationProjection`、独立provider live generation与session live-control epoch/revision；
- terminal command receipt store与PENDING_CONFIRMATION/RECONCILIATION/compatible-winner query path；v3 query已直接读取canonical target；
- Host close中的reducer、checkpoint、audit、presentation、publication与repair fixed-point drain。

**保留不变量**

- stage2 direct conversation/tool-attempt/result/context-binding facts继续是唯一foreground authority；
- completed semantic message blocks继续可见；删除segment不得改变accepted text/tool-call顺序、tool argument完整性或live UI在同进程中的streaming体验；
- completed turn不可改写，running只一次变interrupted；
- call无attempt始终not_dispatched；attempt无result始终unknown且不自动retry；
- Host crash/takeover后pending interaction request不存在，新Host live-control epoch从empty开始，open不恢复suspended interaction execution；
- Host crash/takeover后旧generation nonterminal subagent task只变interrupted；accepted task/message/result仍可查，open不构造child `RuntimeSession`、不resume/requeue；
- rebuildable checkpoint完全缺失时仍能按transcript分页open；被binding revision引用的context snapshot不属于可删除checkpoint；
- TUI v3、Inspector与context不import旧presentation/reducer；
- unreferenced context snapshot删除不改变transcript或epoch；被binding revision引用的snapshot受FK保护；
- foreground退出遵守bounded cancel/join，owned OS process/MCP连接收到stop。

**代码修改面**

- host/resume.py、host/session.py；
- host/mcp_recovery.py、runtime/run_execution/interaction.py、interaction_transition.py中跨Hostpending-request continuation；
- runtime/session.py composition/close/latches/reducer registration；
- runtime/subagent/execution.py：保留process-local activation与bounded cancel/join，删除recovered occupancy、teardown retry/reconciliation及任何cross-Host reconstruction；
- llm/segment.py及其durable segment vocabulary/serialization/reducer consumers物理删除；live event base、Text/Thinking/ToolCall与ToolResult Start/Delta/End schema及对应assembler通过AST gate保持process-local；
- runtime/model_stream_recovery.py、runtime/model_control_recovery.py、run finalization/repair；
- runtime/terminal_projection.py、llm/terminal_projection.py与tool execution owner；
- runtime/projection_checkpoint_maintenance.py、committed_reducer_repair.py、post_fold、authority_materialization、foreground projection jobs；
- terminal presentation service、v2 Python protocol分支与Go presentation root/cache；
- runtime/terminal_application/command_receipt.py与terminal_command_receipts schema/test；
- old owner contract、repair order、checkpoint、v2 reconnect tests。

**数据/reset策略**

阶段2已经complete reset/cutover；旧EventLog和projection表若因migration链尚未物理drop，只能是**空且production-unreachable**的schema壳，不能供冷审计或dormant migration tooling使用。checkpoint、presentation root、candidate与receipt没有迁移输入；物理drop可与对应import graph清零同commit发布。

**独立 gate**

- production open/resume不构造RuntimeSession、不调用provider/tool，除旧running→interrupted外无repair写；
- production open/resume不materialize pending approval/plan/MCP request；same-Host live-control snapshot-subscribe与accepted decision query不依赖旧recovery owner；
- production open/resume不materialize或schedule旧subagent execution；old-generation pending/active task幂等变interrupted，accepted child facts仍由canonical query返回；
- production foreground import graph不含model/control recovery、stable candidate、committed reducer/checkpoint、Presentation Foundation；
- production import/schema vocabulary不含`ModelStreamSegmentAccumulator`、stream segment policy或Text/Thinking/Data/Tool Start/Segment/End durable carrier；
- checkpoint/presentation数据全空或表不存在时text/tool/resume/TUI仍正确；
- Host close只有3个logical phase、0 committed-reducer barrier；await数≤12是审查预算；
- Stage 1保留的audit/checkpoint/presentation physical cancel/join只有在对应owner/executor已删除且无task可产生后才归零；
- DB pool/artifact store释放后，task inventory与故障注入均证明没有旧owner继续访问session resource；
- physical operation超deadline时产品状态清晰，close不启动第二代repair owner。

**回滚边界**

被删owner不允许通过feature flag重新消费新transcript row。需要回退旧体系时只能停机、再次complete reset并部署旧binary/schema；不恢复pre-stage2用户数据，不做online reverse projection。

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

新schema从complete reset后的empty store开始，没有旧execution candidate、accepted-fact import或`imported_interrupted`分支；只有cutover后新Host创建的canonical facts。

**独立 gate**

- resume不构造RuntimeSession；
- resume本身不调用provider/tool；
- 除一次running→interrupted transaction外无修复写；
- 同session重复resume不产生第二条interruption；
- transcript加载结果与crash前已commit prefix一致。

**回滚边界**

source回退需要再次complete reset；没有用户数据snapshot恢复承诺或online reverse projection。

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
- orderly close中全部Host-owned process group收到terminate并在共享deadline内bounded wait/join；成功close后没有仍可由该Host控制的running process；
- orderly close中全部subagent activation/child `RuntimeSession`收到cancel并bounded join；completed/failed/cancelled不改写，其余task在资源释放前变interrupted；
- canonical pending write flush；
- 未完成turn与nonterminal subagent task明确为interrupted；tool call仍按attempt是否存在区分not-dispatched/outcome-unknown；
- durable job row/lease在Host退出后仍可检查；是否允许新worker执行由Stage 2已激活的handler safety class与attempt retry规则决定，close本身不把lease超时的非幂等work改回pending。

**代码修改面**

- host/session.py aclose；
- runtime/session.py close/teardown；
- runtime/subagent/execution.py；
- MCP supervisor与Host-scoped terminal process manager/process-group的bounded stop接口；
- writer flush接口。

**数据/reset策略**

无特殊迁移；close状态不作为恢复authority。durable job保留`expires_at`供阶段4按safety class处理；close不自行把expired lease回pending。

**独立 gate**

- AST await count目标≤12，超出需解释但不替代行为gate；
- reducer barrier调用=0；
- background job人工阻塞不延长Host close；
- audit/checkpoint/archive故障不阻塞close；
- p95 idle close<1s，hard deadline≤5s；
- deadline后turn与subagent task状态仍清晰；重新attach不会复活physical child。

**回滚边界**

旧close无法安全读取新execution schema；回退只能停机、再次complete reset并部署对应binary/schema，不恢复本阶段用户数据。

### 8.7 阶段 4：完成background handler迁移并删除legacy projection-job graph

**目标**

Stage 2已激活`durable_jobs` aggregate、`durable_job_attempts` journal、claim repository与result-accept port，并迁移了所有foreground-reachable handlers。本阶段不创建第二次job authority cut；它只迁移在Stage 2被明确禁用、因而production-unreachable的剩余handlers，然后物理删除旧projection-job schema/runtime/tests。目标job kernel仍只承载必须跨Host生命周期存在的work；不是把旧projection jobs改名，也不把lease等同于external effect exactly-once。job claim domain与session writer domain完全独立，Host takeover不能使合法background result失效。

**删除内容**

- durable projection activation/cutover/coverage/seed/target head/result receipt/repair表；
- projection-specific lease与confirmation；
- Oxigraph surface delivery、handler、registry、claim/retry/dead-letter、target head、repair action和migration transform；
- `OxigraphGraphStore`、required URL/settings、production composition、Inspector/doctor health、SPARQL/RDF adapter及全部Oxigraph-specific contracts/tests；
- foreground reply/tool/TUI/evidence job；
- child RuntimeSession作为background continuation载体；
- 旧projection-specific target attempt/head、stable candidate、result receipt、repair action等companion graph；它们由一个通用但窄的physical `durable_job_attempts`关系替代，不复制旧proof graph。

**保留不变量**

- PostgreSQL memory facts/relations、FTS、pgvector与现有bounded两跳GraphCandidateService在删除Oxigraph前后返回等价canonical结果；不新增第三个graph store或generic graph-query service；
- terminal monitor没有durable notification/restart承诺；已接受的canonical completion/termination保留，旧process/command绝不因Host或worker restart而收养、查询或重启；
- subagent不是background handler：已接受task/message/result保留，所有nonterminal task已由Host close/open收口为interrupted，不存在下一execution attempt、claim或remote observation；
- compaction/memory extraction最终有completed/failed结果；
- durable prompt queue顺序与claim可恢复；
- job handler safety class显式，默认`NON_IDEMPOTENT`；
- stale attempt claim generation不能commit progress/result；
- non-idempotent lease loss使current attempt与job aggregate变outcome_unknown，不自动pending；
- worker只写job/attempt-owned result/blob、automatic memory output及其允许的occurrence，不写session transcript；有immutable origin session且exact claim有效时才可用`JobAttemptClaimGuard`追加session event；
- 当前Host以writer generation显式接受completed job result后，结果才进入conversation；
- job enqueue/cancel request用writer generation，attempt claim/progress/result/failure/settlement只用attempt claim generation，二者predicate不交叉；
- 旧attempt永不被下一retry覆盖；job aggregate只引用current/accepted terminal attempt。

**代码修改面**

- projection_jobs/contracts/runtime；
- 旧durable terminal monitor coordinator/notification owner；same-Host process-local monitor由terminal manager保留；
- 旧subagent graph/checkpoint/recovered occupancy、background-job handler与teardown retry/reconciliation wiring；保留Stage 2的Host-owned canonical task/message/result repository及process-local child runner；
- compaction memory extraction；
- prompt queue；
- memory governance background executor；
- settings/runtime wiring/graph package/Inspector中Oxigraph imports与branches；
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

复用Stage 2已激活的durable_jobs + durable_job_attempts表，不再执行schema authority reset。Stage 2 complete reset后不存在可迁移的旧product work、lease或attempt；曾禁用的剩余handler只有在切到新implementation并通过safety gate后才能接受**新**job。禁止old-record importer、old/new worker并行、cold archive或migration bridge。

**独立 gate**

- `RETRY_SAFE` worker在attempt claim后crash，lease过期后旧attempt保留且下一attempt引用它；
- `REMOTE_QUERYABLE` worker crash后围绕同一remote identity只query，不重新launch/invoke；
- `NON_IDEMPOTENT` worker crash/lease loss后current attempt与job进入outcome_unknown，自动invoke count不增加；
- stale generation result commit被数据库拒绝；
- stale/错误job claim不能追加committed event；Host与多个worker竞争同一session allocator时sequence无洞且按commit order，worker event subject只能落在claim允许的job/attempt/memory slot；
- job由Host generation N enqueue且attempt被worker claim后，Host takeover到N+1；worker以current attempt claim generation仍可commit result；
- worker result commit后session transcript保持不变；只有N+1 Host显式accept后才新增canonical entry；
- job result acceptance commit后丢ACK，以同一source job/command identity重试不产生第二个entry；
- stale Host generation不能enqueue/cancel/accept result，stale attempt claim generation不能progress/result/settle；两种失败互不传播；
- result commit后不重复执行；
- Host close不等待job完成；
- foreground model/tool execution本身没有job row；只有用户/产品明确承诺跨Host完成的background work由foreground提交job intent；
- Stage 2已迁移的foreground-reachable handler在本阶段前后使用相同job/attempt authority，无二次cutover或语义变化；
- 旧projection-job table/runtime/import与capability registry命中全部为0；所有产品background handler已使用minimal job kernel；
- 仓库production/test-support dependency graph中`oxigraph` package/import、`oxigraph_url`、`CanonicalMutationSurface.OXIGRAPH`、SPARQL endpoint与Oxigraph fixture命中全部为0；启动、recall和close均不探测Oxigraph；
- 固定PostgreSQL fixture对memory_search/get/explain及0/1/2-hop relation recall的golden结果在Oxigraph删除前后相同；超过两跳仍按现有bounded contract不返回，不新增替代查询语言；
- job aggregate只使用pending/active/succeeded/failed/cancelled/outcome_unknown，attempt使用leased/terminal/outcome_unknown closed state；
- compaction job成功/失败/retry attempt都不删除、重写、重排或重编号transcript entries；completed context snapshot被binding revision引用后受外键保护；
- 无target-head/receipt/repair companion row。

**回滚边界**

job payload/version必须与binary同版本；rollback停机并再次complete reset，不做双worker、job record reverse migration或数据恢复。

### 8.8 阶段 5：最后退役 universal EventLog与旧schema，定型selective journal

**目标**

物理删除已经没有产品消费者的通用execution ledger、replay、universal serializer、projection和migration关系；保留并定型窄`CommittedAgentEvent` journal、独立`LiveAgentEvent` protocol与`OperationalEvent` diagnostics。

**删除内容**

- 151类EventType旧universal grammar、schema-v11全量auto-registry与historical execution decoder；`agent_events`表不删除，而是在reset schema中重建为selective committed journal；
- EventLog writer/physical accounting/materialization account；
- replay timeline/message reducer中旧event拼装；
- runs旧projection；
- durable projection表族；
- canonical mutation中纯delivery/head/migration-binding关系；
- 旧Oxigraph世界的migration catalog、protected-relation entries、deployment/env文档与外部service bootstrap；
- old Inspector candidate/checkpoint/receipt页面；
- 所有仅验证已删除owner的测试。

**保留不变量**

- session/conversation/tool-attempt/job-attempt/memory/context-snapshot/blob可直接查询；
- 只查询complete reset后由新authority创建的facts；没有old DB/cold archive产品读取面；
- schema migration仍由verified runner执行；
- canonical closed payload保留有限per-domain upcaster/golden，不以旧EventLog historical decoder替代；
- PostgreSQL仍是唯一线上authority。
- selective `agent_events`只保存accepted occurrence/audit truth，支持历史stored-suffix查询；Gateway另行组合read-time observation projection。任何reader都不得以event replay execution或证明canonical row存在；
- `StoredCommittedEvent`保留closed typed subject-FK union；Gateway用exact subject形成read-time `CommittedObservationProjection`，两者不得合并成复制canonical正文的durable UI event；
- process-local live base/bus保留required Start/Delta/End及session `InteractionOpened/Replaced/Closed`；它不进入durable serializer或migration catalog；
- ordinary post-commit hook不拥有durable cursor/catch-up；V1可靠extension action数量为0，future action必须通过独立ADR新增具名job，不能恢复universal consumer receipt graph。
- subagent窄accepted lifecycle occurrence可以保留：task/status引用canonical task，message/result分别引用exact canonical child并只供audit/observation；没有run/schedule/phase/checkpoint/attempt event驱动child execution。

**代码修改面**

- event/：拆成committed/live/operational base与窄typed vocabularies，删除151类universal union；
- event_log/：删除execution confirmation/materialization/reducer耦合，保留selective PostgreSQL journal writer/query/serializer；
- replay/：删除execution replay与旧grammar拼装，只保留按canonical rows查询及独立audit reproduction所需的窄边界；
- storage migrations；
- Inspector；
- test support/factories；
- production composition。

**数据/reset策略**

最终complete-reset schema。旧DB与旧blob namespace不保留为产品资产，也不挂到production connection pool、Inspector、CLI或audit tooling。

**独立 gate**

- production import graph没有旧universal event writer/reducer/checkpoint/repair或execution replay模块；selective committed journal只能由canonical transaction owner与audit/query adapter引用；
- event appender只接受closed Host/job-attempt guard；数据库拒绝free-form/wrong-slot/cross-session subject；plugin/hook package无法import appender；
- subagent task/message/result occurrence只能由Host guard追加；job catalog与historical decoder均不能创建、claim、resume或retrychild；
- Go/TUI只消费bounded observation projection而不直接deserialize stored event；live-control snapshot-subscribe竞态和ordinary-hook no-catch-up测试通过；
- text/tool指标达标；
- fresh database完成全部行为gate；
- 没有compat shim、dual-write或background backfill owner；
- repository/composition/deployment没有old DB/cold archive/export-import connector，旧binary不能打开新schema；
- committed core vocabulary必须exact等于决策7的26类，live vocabulary必须exact等于同一决策的23类，正式AgentEvent总数exact为49；独立`RawProvider*`/raw-provider registry/逐delta semantic-draft protocol必须为0。新增或删除任一formal type、改变committed type→subject/guard映射都需要architecture review；不得靠巨型JSON、通用receipt graph、第二transcript source或重复transport DTO伪造收敛。

**回滚边界**

cutover或后续阶段若回滚，只能停机并再次complete reset后部署目标binary/schema；不恢复旧用户数据，不允许新旧schema混用，也不生成reverse projection。

### 8.9 进入实施规格的边界

本文到此作为architecture baseline，不把SQL/DTO伪代码继续扩写成隐藏的implementation spec。下一份规格应独立选择一个可交付范围：

- **Stage 0/1 spec**：配置默认、semantic latch移除点、各owner stop-admission/physical cancel/join contract、resource release order与故障注入；或
- **Stage 2 spec**：foreground direct schema和Protocol v3 coherent production activation；允许多个dormant construction PR，但不能拆成text/tool/TUI/rehydrate子上线。

若选择Stage 2，规格在编码前必须逐项冻结：

1. user/turn、assistant/final、完整assistant tool-request message及ordered blocks、逐call tool execution attempt与terminal result、subagent task/message/result/status及close/takeover interruption、context snapshot、binding revision install/current-pointer advance、accepted interaction decision、open/takeover、job enqueue/cancel、job-result acceptance的SQL transaction boundary；provider-generated assistant transaction还必须消费唯一prepared-input handle并保存其exact revision/cut；
2. session-wide command id物理唯一形状、same-input comparison与canonical query SQL；
3. per-session entry sequence与selective event sequence各自在owner transaction内按commit order分配，`latest_sequence`/`latest_event_sequence`、统一session event allocator lock order、rollback/Host-worker并发commit、append-only history/event cut、session-lifetime event retention与committed budget/schema GAP规则；同时把决策7的exact 26 type、13 subject slot、type→slot与type→guard矩阵生成到SQL constraint、serializer registry和cross-language fixture；event sequence只排序accepted occurrence，不替代entry ordering或canonical truth；schema不得出现event retention lower bound或prune transaction；
4. writer lease acquire/renew/takeover predicates、job-attempt claim predicates，以及sealed `EventAppendGuard = HostWriterGuard | JobAttemptClaimGuard`与每个guard允许的canonical subject/occurrence矩阵；不增加第三个generic guard；
5. durable job aggregate/attempt claim/result/retry lineage/cancel request与source-job acceptance唯一约束；
6. Protocol v3 canonical snapshot/page、`StoredCommittedEvent`与exactly-one typed nullable subject-FK union、`CommittedObservationProjection`三分支及bounded hydrate/GAP、snapshot/history/observation共用的`ObservationContent`、provider live stream/snapshot、authenticated first-party user live profile、`CompleteToolArguments | TruncatedToolArguments`、ordinary/privileged extension projection、Host-owned capability issuer/revoke、带`owner_epoch/live_revision`的session live-control `snapshot_and_subscribe()`与Opened/Replaced/Closed DTO、ordinary post-commit registration cut/no-catch-up、read-only repeatable-read transaction和Go cache transition；同时冻结`CommittedAgentEventBase`、`LiveAgentEventBase`、`OperationalEventBase`的物理依赖方向；
7. assistant message/call ordinal/attempt/result pairing约束、pre-dispatch provider conversation cut的prepared-input owner、assistant `context_binding_revision_id + provider_input_through_sequence`字段矩阵、全call terminal follow-up query、按assistant cut判定的versioned provider-only interruption/late-effect lowering contract，以及`2+B+C+E <= 2+B+2C`预算测量；
8. global blob canonical encode/digest/FK/RESTRICT、24小时orphan grace与GC竞态；closed canonical transcript content locator、inline/blob exactly-one约束、`ReadCanonicalContent`的logical-byte range/hard cap、逐请求subject/session/workspace/capability校验、chunk/full digest与codec/error语义，以及“reference不授权、DB transaction不跨blob I/O、durable read state/event为0”；
9. conversation rehydrate、context rematerialization、effect reconciliation、best-effort audit reproduction与明确禁止execution replay的API/命名边界；
10. complete-reset store manifest、old-process/remote-effect quiesce、无import/cold-archive/reverse-projection guard、cross-language fixtures、ACK unknown、concurrent entry/event/exact-subject read cut、observation budget GAP、blob content多range/跨codec边界/scope撤销/corruption、typed subject negative constraints、Host/job append race、lost notification、provider/tool-result/live-control overflow与epoch change、raw-thinking user projection原样性、tool-argument short/oversize/UTF-8截断与dispatch isolation、user/ordinary/privileged/revoked/S3 projection matrix、hook exception/timeout/detach/capability leak/no-catch-up、pending interaction replace/takeover、PromptRejected-vs-cancel、ToolResult live-End-before-commit、job retryable-attempt-vs-aggregate-terminal、memory candidate-no-event、compaction-complete-without-adoption、subagent exact-child与orderly-close/takeover interruption/no-resume/new-task identity、mixed/multi-tool crash、stale generation和physical resource shutdown fault matrix；
11. 只校准、不重新打开架构语义的具名运行参数：committed observation每poll的event/byte/time default、hard cap与suffix GAP阈值，单条committed payload byte hard cap及Inspector/audit page size与query concurrency cap；content-live observer、shared ring、provider/tool-result snapshot、control snapshot/control observer的event/byte default与hard cap，tool-argument display threshold、callback deadline与close drain budget；`ObservationContent` inline/blob threshold、`ReadCanonicalContent`单chunk hard cap、并发hydrate数与client timeout；memory candidate/governance SLA、batch size、claim lease与index lag阈值。上述数值可由production-unreachable dormant implementation、fixture与负载探针校准，但Stage 2 activation前必须形成具名default、server hard cap与monitor；它们不是architecture Open Question，也不能改变已冻结的GAP、retention、failure isolation、capability或durability边界。

该规格不得引入兼容reducer、command receipt graph、control transition log/per-section cursor、durable interaction request、durable observation/content projection或download cursor/lease/receipt、canonical-subject identity table、ordinary-hook receipt/catch-up graph、subagent background flag/job handler/attempt/claim/checkpoint、第三种generic event append guard、read snapshot root、compaction epoch rewrite或跨domain generation binding。

---

## 9. 验收指标

### 9.1 架构预算与观测目标

以下数字用于量化durability amplification是否真正下降。标为“审查预算”的项目不直接判定correctness；偏离时要求解释和architecture review。标为“结构gate”的项目反映本方案明确禁止的依赖，可以直接阻止阶段完成。

主路径量化目标是：在无新compaction的steady state，普通text turn从当前至少15个durable write scope降到2个transaction，one-tool physical happy path从至少31个降到5个transaction；remote identity若在result前独立成为canonical fact则该路径为6个。对应transaction追加3条、7基线/8含remote identity的selective committed occurrence，不形成额外write scope。Host close从45个await、4个reducer barrier压到3个逻辑band、0 barrier，并以≤12个await作为结构审查预算。

| 指标 | 当前实测/静态值 | 推荐目标 | 属性 |
|---|---:|---:|---|
| steady-state text durable transaction/write scope（无新compaction） | ≥15 | 2 | 审查预算 |
| text universal EventLog transaction对照 | 11 | 0；目标2个canonical transaction | 审查预算 |
| text canonical product fact（不含child block rows） | ≥47个旧durable object/fact | ≤4（turn、user、initial binding revision、final assistant） | 审查预算 |
| text selective committed event row | 当前43个universal event | 3；与canonical row同transaction | 结构gate；不得压到0 |
| steady-state one-tool durable transaction/write scope（无新compaction） | ≥31 | 5 | 审查预算 |
| one-tool universal EventLog transaction对照 | 23 | 0；目标5个canonical transaction | 审查预算 |
| one-tool canonical product fact（不含assistant child block rows） | ≥91个旧durable object/fact | ≤7（按现有预算口径） | 审查预算 |
| one-tool selective committed event row | 当前83个universal event | 7基线；发布remote identity时8；与canonical row同transaction | 结构gate；不得压到0 |
| 单个N-call tool round canonical transaction | 当前未单独测量 | N+E+3，E≤N；physical上界2N+3（完整turn上界2+B+2C） | 审查预算；message/attempt correctness优先 |
| 单个N-call one-round logical product item | 当前未单独测量 | N + E + 5，E≤N；全部physical时为2N + 5（turn、user、initial binding revision、tool-request message、E attempts、N results、final） | 审查预算；assistant block child row数单独报告 |
| canonical transition observation authority | v2 per-section version/fingerprint/control cursor | selective `event_sequence` high-water + bounded `CommittedObservationProjection`；0独立control cursor/history | 结构gate |
| committed observation read scope | 当前Foundation/root多阶段读取 | 每次poll最多1个短repeatable-read transaction；suffix超budget直接GAP | 结构gate |
| durable observation projection/cursor row | 当前Presentation Foundation多类row | 0 | 结构gate |
| canonical content hydrate read scope | 当前只有tool-specific artifact char-range读取 | 每个chunk至多1个短authorization/content-edge read transaction + 1次bounded storage byte-range read；transaction不跨storage I/O | 结构gate |
| durable content download receipt/lease/cursor/projection/event | 当前没有统一transcript contract | 0 | 结构gate |
| committed event append guard variant | 当前通用writer candidate/receipt路径 | 2：Host writer、exact job-attempt claim | 结构gate；不得增加generic第三类 |
| free-form committed subject kind/id | 当前event identity/proof字段混杂 | 0；每event exactly-one typed FK且type/slot受DB约束 | 结构gate |
| ordinary post-commit hook durable cursor/receipt | 当前无完整extension contract | 0；registration cut后best-effort | 结构gate |
| durable pending interaction request/恢复owner | 当前存在suspended/recovery graph | 0 | 结构gate |
| durable live-control epoch/revision/event row | 当前pending recovery/transition混合 | 0；epoch/revision/event全部process-local | 结构gate |
| committed event vocabulary | 当前151类universal `EventType` | exact 26个`pulsara.core`类型；type/subject/guard逐项固定，extension-defined/published type为0 | 结构gate |
| formal AgentEvent vocabulary | 当前151类universal registry | exact 49：26 Committed + 23 Live；custom/free-form extension type为0 | 结构gate |
| generic durable extension action/tailer | 当前无closed V1 contract | 0；future每项可靠action单独ADR为具名job | 结构gate |
| policy kind / argument rewrite | 当前hook可影响completed blocks，缺closed gate | 1个`ToolDispatchAuthorizationPolicy`；decision 3种；rewrite field 0；machine default 2秒/hard cap 5秒 | 结构/行为gate |
| required live semantic vocabulary | 当前13类durable model-stream event + 7类private raw item | exact 23类：provider十二类 + ToolResult三类 + Interaction三类 + terminal四类 + SubagentProgress一类；独立RawProvider/逐delta draft为0 | 结构gate |
| live queue/ring/snapshot unbounded surface | `RuntimeEventPublisher`存在unbounded queue；无统一snapshot budget contract | content-live per-observer、shared ring、provider/tool-result snapshot、control snapshot、control observer全部同时具有event/byte named default与server hard cap | 结构gate；确切数值可由dormant probe校准 |
| first-party raw thinking content transform | 当前无独立user-view contract | retained/已投影delta的摘要、redaction、内容长度截断均为0；GAP/crash可丢失delivery | 行为gate；不是durable completeness |
| tool-argument user display | 当前无closed长度contract | threshold内exact；超限UTF-8-safe explicit truncation + total bytes/digest；dispatch参数截断数为0 | 行为/结构gate；threshold数值可校准 |
| 产品SQL tables | 61 | ≤24 | 审查预算 |
| text owner family | ≥14 | ≤3 | 审查预算 |
| one-tool owner family | ≥17 | ≤5 | 审查预算 |
| foreground committed reducers | 9 | 0 | 结构gate |
| mainline hard reconciliation latches | 6 | 0 | 结构gate |
| restart branch family | ≥8 | ≤3 | 审查预算 |
| Host close logical bands | ≥6 | 3 | 行为/结构gate |
| Host close awaits | 45 | ≤12 | 审查预算 |
| committed-reducer barriers | 4 | 0 | 结构gate |
| 自动exact context audit artifact/model call | 4 | 0 | V1结构gate；debug/采样另计 |
| durable model stream segment event/candidate | 当前text/thinking/data/tool delta均可产生 | 0；completed message直接commit；required live typed events非0 | 结构gate |
| Oxigraph production config/worker/surface | required URL + async surface | 0 | 结构gate |
| Agent memory graph traversal | PostgreSQL bounded最多两跳 | 保持现有最多两跳 | 行为gate；不借减法扩权 |
| 预计production LOC | 当前HEAD | 净删≥22k | 审查预算 |

对象/fact百分比使用当前下界，因此实际降幅可能更高。产品表`≤24`、Host close`≤12 awaits`与净删`≥22k` production LOC仍只是architecture review budget和减法信号；committed core exact 26类、type/subject/guard mapping以及required live grammar则已升级为结构gate。两者都不能通过巨型JSON、合并无关类型、生成代码迁移、删除typed可观测性或机械合并coroutine来取巧，也不能让一个行为错误的cut通过。live与operational类型使用独立registry/预算，不能混入committed数量，也不能被“process-local”解释成无需schema/version。

### 9.2 Correctness gates

下列条件是production cut的硬gate：

1. **单authority**：普通Agent暴露tool后，无论模型动态选择text还是tool，同一turn及同一session transcript都只来自direct schema；不存在旧universal EventLog/new transcript merge reader或dual semantic write；canonical row与对应selective committed event同transaction不算双authority，event不得覆盖row；
2. **切换原子性**：允许多PR dormant construction，但第一次写新foreground row的production activation已经具备同版本open/rehydrate、context、Inspector、compaction source、TUI v3 reader与minimal job kernel；所有foreground-reachable background capability已切换或在production明确禁用，不存在old/new job bridge；
3. **single Host writer**：每session只有一个当前writer generation；takeover后旧generation的turn/transcript/foreground tool attempt/result/queue/accepted-interaction-decision/job-control authorization mutation全部被PostgreSQL拒绝；
4. **fencing domain独立**：Host takeover不改变合法job-attempt claim；worker progress/result/failure只校验attempt id + claim generation且不能写transcript；当前Host接受job result时只校验writer generation；
5. **client mutation幂等**：turn/queue/accepted-interaction-decision canonical row持有session-wide command id；user acceptance ACK unknown后同id/same input返回原target，同id/different input稳定conflict；query不依赖receipt row；
6. **final唯一commit**：assistant entry与turn completed同transaction，一个turn最多一个winner；ack unknown只按stable primary key读winner；
7. **crash语义唯一**：model stream、pending interaction或未完成foreground execution跨进程后只变interrupted，不恢复coroutine、cursor、request、foreground execution candidate或provider outcome；已经commit的memory candidate是durable work intake，由新worker按canonical query重新claim，不属于execution replay；
8. **side effect不静默重做**：call无attempt可证明not-dispatched，attempt无result与non-idempotent job-attempt lease loss才是outcome_unknown；foreground每call最多一attempt，显式retry必须是新turn/new call；late exact outcome只能填充旧call尚不存在的唯一result，不能覆盖、倒插或改写旧turn；自动invoke增量为0；
9. **job retry安全与lineage**：stale attempt claim generation不能commit；retry-safe重执行创建新attempt并保留retry_of，remote-queryable只observe旧remote identity，旧attempt永不被覆盖；
10. **canonical read cut与观察一致**：per-session entry sequence只在canonical entry transaction内按commit order分配并与entry high-water原子推进；selective event sequence只由持有closed Host/job-attempt guard的canonical owner在统一session lock order内分配并与event high-water原子推进。Protocol v3 snapshot metadata/control/rows/tool attempts、latest entry sequence与event-sequence cut来自同一repeatable-read MVCC cut；history page绑定`session_id + cut_sequence + entry_sequence`。committed observer在单一bounded read cut中把stored suffix与exact subject组合成projection，超budget直接GAP；tool attempt及public remote-identity publication有typed occurrence，任一edge notification丢失都不造成永久漏读；
11. **semantic context与per-call cut边界**：compaction只追加immutable context snapshot/binding revision，不删除/重写/重排transcript；目标没有transcript epoch或entry retention lower bound。initial revision与user/turn原子安装；revision source upper bound早于turn user entry，rematerialization拼接全部current-turn exact delta；只有provider safe point可新增revision并原子推进turn current pointer。每次provider dispatch从同一pre-dispatch MVCC cut冻结exact revision与`provider_input_through_sequence`，accepted provider-generated assistant只消费该prepared-input handle并原样保存二者；因而mid-turn compaction可用，且late result不能借assistant自身sequence或共享revision伪装成历史input。unreferenced snapshot可GC，被revision引用的snapshot受FK保护且不能重新生成替换；
12. **derived/consumer plane不反向否决**：checkpoint、projection、presentation、audit、durable event consumer、post-commit/live/operational hook、TUI delivery、search index failure不能改变accepted user/assistant/tool fact或回滚canonical transaction；
13. **close有界且physical safe**：最终stop ingress、bounded foreground termination/marking、canonical flush/resource stop三段完成；Stage 1不等待旧owner业务成功，但仍在资源释放前bounded cancel/join其physical task，Stage 3删owner后相应await才归零；
14. **selective occurrence保持最小但非零**：committed registry exact等于决策7的26类；只有具独立用户可观察/audit/hook语义的accepted transition进入`agent_events`。tool attempt、public remote-identity publication、turn interrupted等不靠entry sequence的transition必须有committed occurrence；`PromptRejected`与explicit cancel分型，job只在enqueue、attempt acceptance与aggregate terminal发A，memory candidate/skip/batch progress不发A，compaction只在binding revision真实adopt时发A。`outcome_unknown`只由attempt/result/turn在read time推导，不发独立occurrence。每个stored event以13-slot exactly-one typed nullable FK引用subject，type/slot、type/guard、same-session/workspace和deletion由数据库约束；subagent message/result引用exact child而非task aggregate。纯CAS/context compiler中间态、retryable job-attempt terminal、background private progress、transport与UI observation不进入journal；没有session lifecycle core、独立control revision/history、per-section cursor、receipt、checkpoint或consumer ACK；
15. **pending interaction ownership唯一**：request只存在于当前Host live control；`owner_epoch/live_revision`与atomic `snapshot_and_subscribe()`保证same-Host snapshot/event无缝衔接，replace后stale resolution失败；crash/takeover后新epoch为空且turn interrupted；canonical snapshot/open不恢复request，accepted decision绑定durable subject，secret只保存redacted disposition/keyed commitment；
16. **multi-tool message与attempt原子边界**：mixed text与全部calls/ordinals在一个assistant message transaction中commit；每个physical invoke前exact unique-per-call attempt commit；result精确绑定attempt，全部call terminal后才follow-up，provider lowering不按physical completion排序；rehydrate后的悬空call必须用versioned provider-only `ProviderToolResultClosure`按原ordinal闭合，不伪造canonical result或committed event；若旧result在后续assistant之后晚到，future lowering只能按实际sequence表达typed late-effect observation；
17. **blob publication与canonical read唯一**：所有大内容只引用已验证immutable blob；canonical FK与ON DELETE RESTRICT阻止missing/dangling reference，24小时orphan GC不能删除referenced blob。Protocol-facing transcript slot只有inline/blob exactly-one union；`CanonicalBlobReference`不是capability或URL，raw blob id不可读。每次bounded range read沿exact canonical edge重新校验subject/session/workspace/capability与digest/size/codec，数据库transaction不跨storage I/O，client验证chunk与完整digest；missing/corrupt bytes显式失败但不改变canonical row，durable download receipt/lease/cursor/projection/event为0；
18. **恢复承诺分层**：conversation rehydrate与context rematerialization有versioned contract，且在全部context-input audit artifact缺失时仍必须工作；effect reconciliation默认只query；execution replay不存在；audit reproduction只允许显式debug/采样的短TTL best-effort诊断，不进入正常open，也没有逐model-call completeness承诺；
19. **schema evolution封闭**：canonical closed payload只用SQL migration或有限per-domain upcaster演进；committed journal只解码受支持的namespace/type/schema版本，unknown version对该consumer fail closed并可触发GAP/snapshot，不影响canonical row；production没有universal historical execution decoder。
20. **stream durability归零、typed live非零**：vendor SDK transport item只存在于adapter调用栈；Runtime边界从sanitizer/normalizer直接接收Text/Thinking/Data/ToolCall与ToolResult Start/Delta/End `LiveAgentEvent`，进入独立bounded process-local assembler与bus。没有独立`RawProvider*`或逐delta semantic-draft协议。Start不可变，Delta只更新单一assembler，End携带final frozen block/view。ToolResult live End不是acceptance proof，只有canonical result transaction能产生`ToolResultAccepted`。authenticated first-party user view对retained/已投影thinking delta不摘要、不redact、不按内容长度截断，tool arguments在展示阈值内exact、超限返回UTF-8-safe explicit truncation；两者仍服从GAP/detach/crash语义。session Interaction Opened/Replaced/Closed同属typed live但使用独立control snapshot/owner，不进入block assembler。只有completed assistant draft或accepted tool-result entry能进入canonical message transaction，crash不会恢复stream cursor/partial content/control request，也不会合成历史Start/End/Opened/Closed；
21. **extension failure与sensitivity隔离**：user view与extension projection类型分离且只由Host authorization service签发/撤销；ordinary hook只收到typed/redacted projection，raw thinking extension只给first-party Inspector/debug短期lease，未redacted tool arguments需独立S2 capability且仍有byte hard cap，private URL只给current-controller interaction view，S3 secret不可构造为event。hook exception/timeout/overflow只GAP后detach或直接detach并产生operational diagnostic；ordinary post-commit hook仅从process-local registration cut后best-effort投递，没有durable cursor、自动journal catch-up或restart replay。extension只能订阅exact 49类core event；V1没有custom event publisher、generic durable action或tailer。唯一policy是`ToolDispatchAuthorizationPolicy`，无rewrite；unavailable转confirmation、无controller转deny，只有Allow可physical dispatch；
22. **memory physical store唯一**：PostgreSQL `memory_candidates`、closed governance decisions、`memory_facts`/`memory_relations`、FTS与pgvector支撑全部memory tools及现有bounded两跳recall；production source、settings、composition、schema与tests没有Oxigraph、SPARQL或第二graph store；
23. **memory proposal与governance解耦**：现有`remember_*`的`proposed`只在candidate row与对应tool result原子commit后成立；automatic extraction同样先提交candidate。governance只claim已commit candidate并在durable job中异步完成，foreground provider/reply/turn/close不等待；decision前candidate不进入normal recall。V1没有delete/forget tool、candidate、decision、event或pending-delete状态，supersede/stale不改名为删除；
24. **terminal process lifetime绑定Host**：yielded `process_id`只在当前Host owner lease内可用；orderly detach/close终止owned process group并有界drain，成功后旧handle失效。Host crash/takeover不按PID、historical handle、monitor/event或job claim收养/恢复process，未accepted outcome只显示interrupted/unknown；terminal monitor不创建durable job/launch token/receipt/repair graph，OS orphan cleanup只属operational deployment concern。
25. **subagent execution lifetime绑定Host**：accepted task/objective/parent-child/message/result与terminal status保留，message/result各有stable exact child id与独立subject FK；同一result的explicit/inferred路径只接受一次。activation task、physical run、child `RuntimeSession`、partial live output和capacity/MCP owner只在当前进程。orderly close有界cancel/join并把nonterminal task置`interrupted`且同transaction写`SubagentTaskStatusAccepted`；不得复用`cancelled`或`SUBAGENT_RUN_*`。crash后的takeover按旧`execution_writer_generation`完成同一幂等收口。production job catalog/schema没有subagent handler、attempt/claim/lease/checkpoint；reattach不resume/requeue，显式重新委派使用new task id；child effect unknown只落exact tool attempt。

### 9.3 主路径性能与I/O预算

- model首token前最多1次canonical write：user/turn acceptance；
- final text完成后只有1次canonical write transaction；
- 每个assistant tool-request message 1次原子commit，每个call 1次terminal-result commit，每个实际physical call另有1次attempt commit；通式`2+B+C+E`且`E<=C`，上界`2+B+2C`；
- 上述2/5与transaction通式是复用已有binding revision或无需compaction的steady-state预算；需要新semantic snapshot时，snapshot commit与binding revision install/pointer advance分别单独报告，不得伪装成checkpoint I/O或从correctness计数中删除；
- attempt transaction保存“Runtime已经跨过dispatch ambiguity boundary”这一不可替代的产品事实；即使one-tool目标因此是5而不是4，也不得删除attempt来混淆not-dispatched与outcome-unknown；
- foreground call graph中 checkpoint write = 0；
- foreground call graph中 audit artifact write = 0（opt-in debug除外）；
- foreground call graph中 durable model/tool-result stream segment/event candidate write = 0；Text/Thinking/Data/ToolCall/ToolResult Start/Delta/End只走独立bounded typed live channel；
- pending interaction的durable request/live-control event/epoch/revision write = 0；Opened/Replaced/Closed只走独立bounded process-local control channel；
- foreground call graph中 durable job enqueue = 0，除非该tool明确启动用户承诺的background work；
- subagent task/message/result可写Host-owned canonical rows，但subagent execution产生的`durable_jobs`/`durable_job_attempts`/claim/checkpoint write始终为0；
- `remember_*`不为governance新增foreground transaction：candidate append并入其既有tool-result transaction；governance model call、decision、canonical fact与index maintenance均在background job路径，foreground等待数为0；
- TUI delivery不增加canonical transaction；
- blob-backed transcript hydrate的write/receipt/lease/cursor/projection/event = 0；initial snapshot/observation只受自身byte budget约束，正文按需以server hard cap内的byte range读取；每个range最多一个短鉴权/content-edge read transaction，且在storage I/O开始前结束；
- content hydrate慢、失败或client断开不占用session writer/event allocator，不否定canonical commit，也不进入provider、turn completion或Host close semantic gate；
- p50/p95 text reply数据库等待相对阶段0不回退；目标是顺序写次数下降，而不是只优化单次SQL；
- idle Host close p95 < 1秒；任何close有统一≤5秒hard deadline；
- conversation rehydrate使用direct bounded query；context rematerialization时间随“binding revision source cut之后的exact conversation delta长度”增长，不随全部历史event/reducer数增长。

### 9.4 静态架构 guardrails

CI应直接失败于真正违反目标边界的结构：

- foreground模块import checkpoint repair、projection job或resume recovery owner；
- provider adapter/transport port让SDK/wire object逃出adapter，定义或返回独立`RawProvider*`/逐delta semantic-draft union，直接publish observer bus，或import committed serializer/event writer；Runtime-owned sanitizer/normalizer只能产出`LiveAgentEventBase`或typed terminal/usage result，stream assembler只能依赖这些process-local类型且不得产生stable segment candidate/fingerprint；
- Host close出现 committed-reducer barrier；
- domain模块新定义 FULL/NONE/UNKNOWN/CONFLICT；唯一例外是storage adapter内部；
- 新增 stable candidate + receipt 配对类型；
- UI/publication调用 Runtime reconciliation latch；
- context audit自动从每个ModelStart触发；
- foreground reader从`agent_events`重建/覆盖`transcript_entries`，或canonical row与对应committed occurrence由不同owner/不同transaction补写；同transaction共写、Inspector读取stored journal及Gateway在一个bounded read cut中形成observation projection是必需边界；
- Go/TUI直接deserialize `StoredCommittedEvent`、只凭subject id猜message/control内容，或数据库event payload复制完整message/tool result；Gateway observation projection被落表、赋予cursor/checkpoint/repair/retention owner，suffix超budget仍分页返回与current state错位的半suffix，或snapshot/history/observation对同一entry使用互不兼容的content DTO；
- `agent_events`保留自由`subject_kind/subject_id`、允许0或多个subject slot、event type与slot只在Python校验、subject可跨session/workspace或删除后`SET NULL`，或新增`canonical_subjects`间接identity/proof表；
- committed registry不与决策7的exact 26类逐项相等，出现`CUSTOM`/generic `StateChanged`逃生口、`ToolOutcomeUnknown`、`subject_session_id`或第14种subject slot，type→slot/type→guard没有生成数据库constraint，或serializer/cross-language fixture与SQL registry各自维护而可漂移；
- `PromptRejected`被编码成`PromptCancelled`或模糊queue terminal；`MemoryCandidateAccepted`、governance batch/skip/progress进入committed registry；retryable job-attempt terminal产生`JobTerminalAccepted`，或job aggregate terminal反而只留attempt row；
- subagent message/result event引用task aggregate或payload自由child id、同一explicit result在后续run completion再次产生`SubagentResultAccepted`、Host close/takeover把nonterminal task写cancelled，或任何`SUBAGENT_RUN_*`/delivery/phase/checkpoint type进入committed registry；
- checkpoint failure可到达assistant commit/turn completion拒绝分支；
- operational log/trace被resume读取；
- Host-owned canonical mutation没有writer_generation predicate，或stale Host generation仍可commit；
- background job worker mutation检查writer generation、合法attempt claim在Host takeover后失效，或worker可直接append session transcript；
- subagent task出现background flag、job/attempt/claim/lease/retry/checkpoint列或handler，child physical owner进入job worker/catalog，orderly close在child仍可访问session资源时返回成功，takeover resume/requeue旧task，或重新委派复用旧task id；
- `CommittedEventAppender`接受closed Host/job-attempt guard之外的authority、plugin/hook/TUI/Inspector能import/调用appender、无immutable origin session的job能写session journal、worker guard能写transcript、两类owner不用统一session allocator lock order，或rollback留下event sequence/high-water空洞；
- appender暴露generic `(event_type, subject_kind, subject_id, payload)`、允许caller在transaction外缓存/复用subject handle，或closed domain adapter能选择7.5映射之外的subject slot；
- Host job enqueue/cancel/result-acceptance只检查claim generation或绕过writer generation；
- expired `NON_IDEMPOTENT` job attempt自动回pending/reexecute，或retry覆盖旧attempt row；
- Stage 2 production Host仍能创建/查询/接受旧projection-job authority，存在old-job→new-conversation bridge，或尚未迁移的background capability仍在catalog/admission可达；
- `remember_*`直接写`memory_facts`、只在process-local sink deposit后就持久声称`proposed`、candidate与tool result分transaction产生可观察裂缝、governance读取未commit/process-local proposal、foreground reply/turn/close等待governance或index freshness，或V1新增delete/forget tool、candidate、decision、event、quarantine；
- production source/composition/CLI/Inspector出现old EventLog→canonical importer、old→new identity map、`imported_interrupted`分支、old DB/cold-archive connector、offline converter或reverse projection；deployment在complete reset前未quiesce/fence旧Host/worker/monitor/subagent owner，或新Runtime能查询/继续/重新dispatch旧remote effect；
- production代码、settings、composition、Inspector、CLI、contracts或tests仍import/配置/连接Oxigraph，定义Oxigraph surface delivery，或Agent tool暴露raw SPARQL；
- v3 canonical snapshot/page用多个autocommit read拼接、没有entry cut/event cut，committed observer只比较entry sequence而不level-read bounded observation、exact subject不来自同一MVCC cut，或把live/operational state混入canonical read cut；
- canonical entry sequence在transaction外预留、用允许乱序commit的nextval分配、异步更新session high-water，或rollback后仍发布sequence/high-water；
- 新增独立control transition history/revision、per-section cursor/fingerprint/receipt，或把background worker private progress混入session committed journal；合法的selective typed product occurrence不在此禁令内；
- canonical snapshot/open包含pending interaction request，resume重建approval/plan/MCP request，schema出现`interaction_requests`或durable live-control epoch/revision/event，但没有新的产品决策；
- session live-control没有`owner_epoch/live_revision`、snapshot与subscribe分两次加锁而存在漏窗、Opened/Replaced/Closed不走typed process-local base、overflow阻塞Host/写durable GAP，或takeover合成旧epoch close/open历史；
- interaction resolution不同时校验current writer generation、process-local owner epoch/revision与live interaction id；
- assistant tool call在完整parent message transaction commit前可见/可invoke，或mixed message逐callcommit；
- tool adapter在exact `tool_execution_attempts` row commit前被调用，call无attempt被标unknown，或attempt无result被标not-dispatched；
- 同一foreground logical call可建立多个physical attempts，显式retry不创建新turn/new call，foreground attempt保存可变started/terminal/unknown status，或late outcome覆盖既有result/改写旧turn与历史assistant attribution；
- 任一call尚无terminal result时发起follow-up provider call，result未绑定exact attempt、只能按完成sequence pairing，或丢失original call ordinal；
- restart后把悬空assistant tool call直接发给provider，把interruption closure持久化为canonical tool result，或用它授权自动retry；
- canonical submit target缺少`UNIQUE(session_id, command_id)`，或重新引入generic command receipt/confirmation/reconciliation table；
- compaction mutation删除、覆盖、重排或重编号transcript entry，或以maintenance/storage pressure实现隐式history pruning；
- user/turn acceptance没有原子安装revision 0，current revision pointer跨turn/指向未committed revision，被binding revision引用的context snapshot被删除、原地更新、重新生成替换，turn在非provider safe point推进current revision，provider-generated assistant entry缺少exact revision或`provider_input_through_sequence`、caller在assistant commit时重新读取/自报cut、cut早于turn user entry或不小于assistant sequence、用assistant自身sequence/revision判断late outcome，或snapshot source覆盖所属turn user entry/漏拼current-turn exact delta；
- `durable_jobs.attempt_summary`或同类JSON覆盖多次physical attempt lineage；
- canonical prompt/tool/job/context/memory row引用未验证blob，domain新增专属artifact hold/receipt，或GC能删除仍被FK引用的blob；Protocol返回只有裸blob id/private URL的transcript content、`CanonicalBlobReference`被当作bearer capability、content API接受任意blob id、未按exact subject slot/session/workspace/current capability重读canonical edge，或descriptor未绑定digest/size/media type/codec；
- `ReadCanonicalContent`使用char而非canonical logical-byte offset、允许unbounded get-all/无server hard cap、在storage传输期间持有数据库transaction/session lock/writer guard、跳过chunk或完整digest验证，或Go在完整digest通过前把blob-backed entry标记为exact/final；
- canonical content read新增durable download receipt、lease、cursor、projection、repair、ACK/Delivered event，读取失败回滚/改写canonical row或启动通用repair owner；
- interaction decision只引用process-local live id，plan answer未成为canonical item，或MCP/external secret plaintext进入普通durable row/query response；
- production open调用execution replay、读取committed journal/live/operational trace恢复coroutine，或引入universal historical event decoder；
- durable serializer注册Text/Thinking/Data/ToolCall/ToolResult Start/Delta/End或任何`LiveAgentEventBase` subtype，live registry接受`event_sequence`/durable receipt，ToolResult End可直接触发`ToolResultAccepted`，或Host crash后合成历史Start/End；
- callback/recorder/live owner/assembler进入event metadata或durable payload，ordinary hook获得raw RuntimeSession/repository/mutable assembler，hook返回allow/deny/rewrite，hook异常/超时/overflow到达provider/canonical failure分支，ordinary post-commit hook持久化cursor、声明reliable、自动查询journal catch-up或overflow后继续投递，plugin定义/publish custom AgentEvent，或policy改写已accepted arguments/timeout后仍dispatch；
- first-party user live projection constructor可被plugin/hook import或注册、plugin可自授/继承user view、Host authorization service未按authenticated session/workspace/current-controller scope签发与撤销，或用户thinking在retained delivery内被摘要/redact/按内容长度截断；
- tool argument展示没有closed complete/truncated union、截断破坏UTF-8边界或缺少显式truncated/total-size语义，oversize prefix被标成完整JSON，或任何display DTO进入assembler、canonical call、schema validation或dispatch adapter；
- content-live per-observer queue、shared live ring、provider/tool-result snapshot、control snapshot、control observer任一缺少event/byte named default与server hard cap，overflow阻塞provider/tool executor、写durable proof、GAP后仍继续旧subscription，或close等待未开始callback；
- S2 extension payload未经过具名capability与delivery-time lease check，ordinary hook可见raw thinking/未redacted tool args/private URL，private URL离开current-controller interaction view，S3 secret可进入event/GAP/exception/metadata，或revoke后已排队privileged projection仍被callback接收；
- provider operation从pre-dispatch read到stream结束持有数据库锁或session-wide semantic-write lease，以“模型运行期间没有其他canonical write”为正确性前提，或据此省略assistant的`provider_input_through_sequence`；
- Stage 1在audit/checkpoint/presentation physical task退出前释放其DB/artifact/session资源；
- v3 server依赖Presentation Foundation root/head，或保留online v2→v3 transcript translator。
- `CONTEXT_*COMPACTION_COMPLETED`或summarizer完成在binding revision未于同transaction采用时产生`CompactionAdopted`；terminal monitor/process callback直接追加任何committed event，而不是由Host接受canonical result/entry后走既有domain adapter；
- 旧151类universal union/schema-v11 auto-registry、八个无production constructor的旧type、historical execution decoder或对应architecture guard在Stage 5后仍可由production import；把旧type标deprecated/dormant不算物理删除。

以下静态检查生成review report，不应仅凭数值让CI correctness失败：

- committed/live/operational registry的类型数量与payload byte预算分别报告；exact 26 committed、exact 23 live及独立raw-provider registry为0是前述结构gate，不在这里降级为数值审查带；
- 产品表超过24；
- owner/transaction/await/LOC预算偏离9.1；
- 一个JSON payload聚合多个有独立lifecycle、unique key或retention policy的产品事实；
- 删除行主要被新的generic abstraction、generated carrier或compat code抵消。

review必须逐项检查语义；产品表等审查预算不能因一个数字自动判对或判错，exact 26 committed registry则按closed set直接判定。

### 9.5 行为与故障注入矩阵

| 场景 | 注入点 | 必须观察到 | 明确禁止 |
|---|---|---|---|
| text-only reply | 普通Agent暴露tool，模型动态选择text | 同一direct schema；目标2 transaction；user + assistant + completed turn；TUI可读 | turn开始前预选text authority、Model/Reply lifecycle rows、checkpoint/audit writes |
| one-tool reply | 同一普通Agent，模型动态选择tool后再final | 同一direct schema；目标5 transaction；完整assistant message → attempt-before-dispatch → result-before-follow-up | 回退EventLog tool authority、merge reader、foreground durable job、为守4次删除attempt |
| mixed multi-tool message atomicity | assistant同时返回text + calls A/B/C；在message transaction每个insert点kill | 要么整message不可见且invoke count=0，要么text与A/B/C及ordinals全部可见后才invoke | 只持久化A、逐call先写先执行、半条provider message |
| tool-request message commit ACK丢失 | 完整message transaction commit后、Runtime收到success前断连接 | persistence adapter按stable assistant_message_id读取完整唯一winner；同进程确认后才创建attempt；若进程死亡则rehydrate只见完整message且所有call not_dispatched | 写第二message、按部分call猜winner、增加confirmation owner、无attempt就invoke |
| multi-tool out-of-order/partial results | A/B/C attempts已commit并行；C、A result commit后B side effect窗口kill | parent message完整；C/A精确pair到attempt；B attempt无result按read-time rule得outcome_unknown；turn interrupted；不发`ToolOutcomeUnknown`；无follow-up；新context按A/B/C ordinal使用known results + `ProviderToolResultClosure`形成合法闭合sequence | 按result sequence重排、直接发送悬空call、伪造canonical result/event、自动重跑B、部分result触发follow-up |
| user acceptance ACK丢失 | turn/queue transaction commit后断连接 | 同command id retry/query返回原target；canonical user item只有1个 | 第二turn/queue item、generic durable receipt repair |
| command id conflict | 同command id改text/delivery mode重试 | stable conflict；原target不变；不写conflict row | compatible winner、覆盖原input |
| memory proposal acceptance | `remember_*`执行后，在candidate/tool-result transaction每个insert与commit ACK边界kill | candidate与tool result要么都不可见，要么都可见；`proposed`只对应durable candidate；ACK unknown按stable candidate/tool-result identity读取唯一winner | process-local sink冒充durable acceptance、candidate丢失但tool result声称proposed、直接写canonical memory |
| async memory governance | candidate commit后让governance model、claim、decision、fact或index maintenance慢/失败/worker crash | reply与turn照常完成；pending candidate由新job attempt按canonical query reclaim；accepted decision/fact/relation/occurrence原子；index可stale/degraded | foreground等待治理、从event replay恢复governance、decision前进入normal recall、增加delete/forget或pending-delete路径 |
| model stream crash | 任意第N个delta后kill | 当前generation的semantic live stream消失；user保留；turn interrupted；partial assistant不进context；可显式重试 | 恢复旧stream cursor、把live snapshot当durable replay、合成历史Start/End或伪completed reply |
| model stream completed boundary | text/thinking/data/tool argument delta以任意chunking产生同一completed provider message | 每block严格Start < Delta* < End；Start对象未被原地修改；单assembler输出同一End frozen block/canonical completed draft；一次assistant transaction；durable stream event数0；tool argument只以完整validated call出现 | chunking改变message identity、多个assembler同时改同block、segment ordinal进入canonical、partial argument可执行 |
| canonical/event atomicity | 在canonical row与对应committed event insert/high-water advance各点kill | row、event与event high-water要么全部不可见，要么全部可见；event subject可回查canonical row；event suffix不用于证明row | 异步补event、event先于row可见、event存在而row回滚、consumer ACK参与commit |
| committed subject integrity | 分别写missing subject、两个slot、错误event-type slot、跨session/workspace id，并在event保留时删除subject；尝试绕过domain adapter传任意same-type id | `num_nonnulls`/type-slot/composite FK/RESTRICT在DB内拒绝结构错误；appender无generic入口，domain contract test断言event FK等于该transaction实际接受的row；合法same-transaction subject+event成功 | free-form kind/id、仅Python校验、`SET NULL`、canonical-subject identity/proof表、跨transaction subject handle |
| exact committed registry | 对SQL constraint、serializer、Python/Go fixture分别注入一个缺失type、额外generic type、`ToolOutcomeUnknown`、错误subject slot或错误guard | 三者都只接受决策7的exact 26 types、13 slots与closed guard mapping；任一漂移在build/migration test失败 | 任意浮动数量预算、`CUSTOM`/`StateChanged`、`subject_session_id`、运行时临场路由 |
| closed formal extension vocabulary | plugin manifest尝试注册custom Committed/Live type、伪造publisher或发送`Unknown + raw JSON`；Operational debug topic与core registry同名 | formal registry始终为`pulsara.core` exact 49；extension只能订阅协商后的typed projection；Operational topic不进入serializer/type count | plugin event publisher、`CustomAgentEvent`、free-form metadata或manifest热插拔type |
| Host/worker concurrent event append | 当前Host提交queue/turn，同时两个job attempts提交job/memory terminal occurrence；再让一个claim过期 | 两类valid guard按统一session lock取得连续commit-ordered sequence；stale claim在canonical/event写前失败；worker不能写transcript | 只有Host能分配而worker漏event、第三个generic guard、sequence reserve/repair、plugin publish |
| prompt queue rejection vs cancel | 对同一fixture分别触发系统delivery rejection与用户显式cancel并丢失edge notify | canonical row分别进入rejected/cancelled，原transaction分别写`PromptRejected`/`PromptCancelled`；observation projection给出不同actor/remediation且可level-read | 两者共用cancel、只改row不发event、另建delivery receipt history |
| job retryable attempt failure | retry-safe job的attempt 1失败但aggregate仍pending/retryable，随后attempt 2完成aggregate | attempt 1只保留attempt row+C；不提前发`JobTerminalAccepted`。attempt 2与aggregate terminal同transaction恰好发一次`JobTerminalAccepted` | 每次attempt terminal都发A、覆盖attempt 1、aggregate结束反而无occurrence |
| memory candidate before governance | `remember_*`与tool result transaction提交candidate后立即kill；另测governance skip、fact/lifecycle/relation acceptance | candidate与tool result原子可见且committed memory event为0；重启worker直接query candidate。skip只终结candidate；只有fact/lifecycle/relation acceptance分别发规定core event | `MemoryCandidateAccepted`、batch/progress/skip event、从event tail恢复governance |
| committed assistant observation | assistant entry与ordered content已commit，只把其minimal stored event suffix交给Gateway | 同一bounded MVCC cut返回`ImmutableEntryProjection`；inline立即渲染，blob branch给出exact canonical reference并经唯一read port hydrate；stored payload不复制正文 | Go凭subject id猜内容、要求event复制完整message、第二张durable presentation projection |
| blob-backed transcript exact render | 强制大型assistant text/tool result走blob；用小chunk cap并让UTF-8 code point跨range边界 | snapshot/history/observation返回同一reference；Go按byte offset拼接，chunk/full digest与codec校验后渲染bytes完全一致；慢range不持有DB transaction | 只显示引用、char offset、unbounded get-all、未验完整digest就标exact、为下载写cursor/receipt |
| canonical content authorization | 以raw blob id、篡改entry/slot/digest/size/codec、跨session/workspace reference和已撤销capability分别读取 | 每次request重读exact canonical edge并重新鉴权；非法scope不形成existence oracle；reference本身不能授权 | presigned/private URL绕过、只在首chunk鉴权、任意共享blob可读、cache绕过明确撤销规则 |
| canonical content corruption | authorized reference对应bytes missing、range metadata错误或chunk/full digest mismatch | typed `CONTENT_INTEGRITY_ERROR`、redacted operational diagnostic与UI placeholder；原entry/event/turn保持accepted；durable read-state write为0 | 回滚/覆盖canonical row、静默换blob、合成ContentDelivered/repair event、启动repair owner |
| committed observation budget/cut | 在冻结H后并发commit，再让`(after,H]`超过event/byte预算或subject hydrate中途故障 | response要么完整覆盖到H且每个subject来自同一cut，要么整体GAP并fresh snapshot；新commit留到下一cycle | 半suffix配current state、跨autocommit hydrate、projection checkpoint/repair |
| durable event consumer failure | canonical commit后让显式journal reader、Inspector、TUI/eval consumer抛错或停机 | canonical commit保持成功；这些显式consumer重启按observation suffix或GAP+snapshot继续；不产生通用receipt graph | 把turn改failed、回滚row、阻塞Host close等待consumer追平 |
| ordinary post-commit hook miss/overflow | event在registration cut前commit、commit后tap offer前kill，或慢hook填满bounded queue | 普通hook可不收到这些callback；overflow得到process-local `HookGap`并detach；run/commit不变；V1不创建可靠extension fixture/job | hook manager自动补读journal、持久化cursor/receipt、声明reliable/catch-up、为“可能漏回调”回滚commit |
| extension identity/version/revoke | 重排manifest handler、修改manifest digest、注册不支持的major、伪报publisher、revoke后queue仍有privileged projection、restart后复用旧registration id | stable manifest handler id不因重排变化；Host-derived principal与Host-minted instance不可伪造；digest/grant变化推进lease generation；无共同major拒绝binding；revoke/restart丢弃未开始queue并要求重新注册 | 位置派生identity、self-asserted publisher、跨Host durable lease、universal decoder、schema mismatch影响Runtime |
| policy unavailable/headless | configured resolver分别timeout、throw、cancel、返回错误schema；另测有/无current controller | machine evaluation默认2秒且不超过5秒；有controller进入`RequireConfirmation`，无controller接受`Deny(policy_unavailable)`；physical invoke count始终为0；deny以无attempt terminal result闭合call，run可继续 | timeout后自动Allow/dispatch、把policy异常变RunError/provider protocol error、创建attempt后再询问 |
| policy argument rewrite attempt | managed resolver尝试返回modified arguments，或两个resolver竞争不同rewrite | registration/decoder拒绝非三种decision；rewritable field count为0；原canonical call不变且invoke count为0；需要变更时拒绝并由后续provider生成new call | effective-input row/rewrite event、completion-order winner、用改写参数复用旧authorization或dispatch |
| first-party live content projection | provider产生含多chunk/multibyte thinking及短/超长tool arguments；同一数据分别交user view与ordinary hook | 无GAP的authenticated user view按接收顺序原样显示thinking delta；短args完整；超长args在UTF-8边界显式截断并给出最终total bytes/digest；ordinary hook仍redacted；assembler/canonical call/dispatch参数完整且相同 | 把chunk伪称provider token、thinking摘要/redact/按长度截断、args prefix标完整JSON、展示截断改变validation或dispatch |
| live observer overflow/hook failure | provider慢observer填满event/byte budget；callback sleep/throw/cancel；随后provider继续 | 若budget允许则该observer收到一个`LiveGap`后detach，否则直接detach；其他observer与provider继续；End/assistant commit仍成功；close只bounded等待已开始callback | provider backpressure等待observer、delta落durable journal、GAP后在旧subscription继续、hook异常变RunError |
| ToolResult live End before canonical commit | tool output完整产生Start/Delta/End后，在tool-result canonical transaction前kill Host | live view随进程消失；call/attempt按canonical规则显示interrupted/unknown，没有result entry或`ToolResultAccepted`；reopen不合成历史Start/End | 把End当acceptance proof、持久化tool-result delta、从live snapshot补造result |
| sensitive hook projection | authenticated user、ordinary、first-party Inspector/debug S2、tool-argument S2、current-controller、revoked lease分别读取thinking、tool args、private URL、MCP secret | user view遵循原样thinking/args展示契约；ordinary只见typed redacted projection；具名未过期extension lease只见获准S2字段且仍有byte cap；private URL只给current-controller interaction view；revoke后queued privileged payload丢弃；S3从不构造 | plugin继承user view或自授能力、raw对象先交callback再redact、capability互相蕴含、private URL进ordinary hook、secret进入event/GAP/exception/metadata |
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
| conversation rehydrate | DB残留running turn或旧pending interaction | acquire新writer generation；一次幂等running→interrupted并在同transaction追加`TurnInterrupted`；加载canonical conversation/context binding；无pending request | temporary RuntimeSession、event/execution replay、恢复interaction/provider/tool调用、补造旧live lifecycle |
| reattach without context-input audit | 删除全部旧plan/pages/root artifact、关闭audit store后detach/kill Host，再attach并提交新turn | canonical history完整显示；running turn按规则interrupted；context从binding revision/snapshot + exact delta rematerialize，新model call正常；audit read/write为0 | 要求audit repair/backfill、因missing audit拒绝attach/model admission、从audit恢复旧provider request/coroutine |
| stale Host writer | Host B takeover后让Host A继续commit | A的turn/transcript/tool/queue/job-control authorization全部被DB拒绝；observer read仍可用 | compatible winner、跨writer reconciliation、双final |
| Host takeover during background job | job由Host A enqueue且attempt claimed，Host B takeover后worker完成 | worker按current attempt/claim generation正常commit result；旧attempt保留；transcript不变；B显式accept后才新增entry | result绑定A generation失败、worker直接写transcript |
| orderly Host close with yielded terminal | process仍running时detach/close | stop terminal ingress；向该owner全部process group发terminate并在共享deadline内bounded wait/join；已由canonical owner接受的completion/termination保留，旧`process_id`失效；不创建monitor job | detach process继续使用session资源、把monitor交给worker、close成功后handle仍可写入、等待通知业务成功而无限阻塞 |
| Host crash/takeover with yielded terminal | 返回`process_id`后kill Host，在新Host reattach | 新Host不按PID/handle/spool/event收养或控制旧process；旧turn/handle为interrupted/outcome_unknown；不宣称OS child必然已死，orphan cleanup只走operational reaper且不阻塞conversation reopen | durable terminal monitor/launch token recovery、PID probe后adopt、重新launchcommand、合成exact completion、因cleanup失败拒绝conversation rehydrate |
| Stage 2 background authority cut | fresh Host创建V1 first-party compaction/memory work，并尝试注册generic durable extension action | compaction/memory job、attempt与result只落minimal job kernel；current Host通过result-accept port纳入conversation；generic extension action被catalog拒绝；subagent execution与yielded terminal/monitor仍为process-local且job写入为0 | 写/读旧projection-job authority、online bridge、generic extension tailer、把subagent/terminal monitor换名塞进job、未迁移capability仍在catalog可调用 |
| complete-reset production cut | 旧库含session/memory/job与leased/running worker；remote process仍可能存活，在quiesce、store reset、migration、activation各点故障注入 | 新Host启动前旧ingress/owner均已stop或fence；PostgreSQL、blob namespace和derived state为空后只出现新schema/facts；remote effect不被新Runtime导入或重做；重试cutover仍是empty-store幂等流程 | accepted-fact import、cold archive reader、old/new identity map、`imported_interrupted`、旧worker晚到写新库、reset被误称为撤销external effect |
| initial context binding genesis | user/turn acceptance任意insert点kill | 要么turn、user与revision 0全不可见，要么三者同transaction可见且turn pointer exact指向revision 0 | turn存在但无binding、pointer跨turn、首个provider call临场创造无归因base |
| mid-turn context binding advance | 初始call成功，加入大tool result后预算超限；在compaction/safe-point transaction各阶段kill | 当前turn exact delta始终保留；成功时新snapshot/revision与pointer原子可见，下一assistant绑定新revision；失败时继续旧revision或typed infeasible | summary包含当前turn、非safe-point换版、覆盖旧revision、恢复ModelStart/End lifecycle |
| provider input exact cut late-result race | pre-dispatch在revision R冻结H=100并开始stream；旧tool result随后commit为101；assistant最终commit为102 | assistant保存R与`provider_input_through_sequence=100`；result 101明确不属于该assistant input，历史attribution不改，future lowering按late-effect处理 | 在assistant commit时重读101、以assistant sequence 102或共享revision R判定result已参与input、写ModelStart/End journal |
| commit-ordered sequence allocator | parallel tool result/final entry竞争分配sequence；在lock、insert、commit、rollback边界注入故障 | canonical commit顺序与entry sequence严格一致；rollback不推进session high-water；pre-dispatch cut H之后commit的任一entry都大于H | transaction外reserve、低sequence晚commit、high-water空洞、异步head追平或sequence repair owner |
| illegal context revision advance | model仍active、tool-request未commit、任一call未terminal或stale writer generation时尝试换版 | 数据库/port拒绝；current pointer与旧revision不变 | 先推进pointer再等待physical exit、用repair补齐、让assistant缺失exact revision |
| context snapshot failure | snapshot transaction前故障 | 继续用上一eligible snapshot或完整transcript；已有reply不受阻；若token不可行则新dispatch typed infeasible；transcript/epoch不变 | half snapshot成为authority、删除source entries、生成失败回滚旧reply |
| context snapshot commit ack丢失 | transaction后断连接 | 按snapshot id/source range/contract查询唯一winner；后续safe-point另行安装binding revision；transcript/epoch不变 | duplicate generation/repair owner、把snapshot commit等同pointer advance、推进retention |
| compaction completed but adoption failed | summarizer完成并持有snapshot，但binding revision install/pointer transaction rollback或stale writer失败 | snapshot可保持unreferenced并由GC处理；turn pointer不变，`CompactionAdopted`数量为0；下一dispatch继续旧eligible revision或typed infeasible | completion event伪装adoption、event驱动pointer repair、失败后覆盖旧revision |
| referenced context snapshot GC | binding revision已引用snapshot后运行GC | snapshot与blob保持；删除被FK/RESTRICT拒绝；context rematerialization exact | 删除后重新生成不同summary、静默回退完整transcript |
| orphan blob GC race | prewrite blob接近24小时grace时canonical row尝试安装FK | 要么canonical transaction先锁定/引用成功，要么GC先删除且canonical mutation安全失败 | canonical row引用missing blob、per-domain hold/receipt |
| TUI snapshot concurrent commit | snapshot metadata/control/rows各SQL之间commit entry/event sequence | response只来自一个MVCC cut，并回显一致entry cut与`event_sequence_cut` | event cut已推进但canonical row不可见、entry/event/control来自不同cut |
| canonical non-entry notification丢失 | queue consume/cancel/reject、turn terminal、tool attempt或public remote identity commit但不append entry；丢notify | 决策7对应的same-transaction committed event/high-water已推进；observer level-read suffix发现transition；fresh snapshot在同一cut显示exact queue/attempt/result/turn-derived state。session closing不在core event范围 | 只比较entry sequence后继续等待、永久显示not-dispatched、为session close另建core type或control cursor |
| TUI page concurrent commit | 以epoch E/cut 9翻页时commit 10 | page只含E且sequence≤9；新cycle再见10 | 新row混入旧cut、root/receipt repair |
| TUI reconnect | kill UI、Runtime继续并丢entry/event notification | Protocol v3从一致snapshot/page重建，按`event_sequence_cut`继续committed observation，并只附着current provider/tool-result/live-control owners；无状态反写Runtime | UI ack成为semantic gate、永久漏掉已commit transition、用journal合成丢失live partial |
| live-control snapshot-subscribe race | 在snapshot读value与observer registration边界并发Opened/Replaced/Closed，逐点暂停owner | observer要么在snapshot看到该revision，要么收到更高revision event；不会两者都漏；queue GAP重新原子snapshot-subscribe | 先query后subscribe漏通知、写durable control cursor/receipt、阻塞Host等待observer |
| pending interaction same-Host reconnect/replace | approval/plan/MCP request存活时kill TUI连接，随后replace request并发送旧resolution | reconnect返回同一`owner_epoch`与current revision/value；replace推进revision；旧epoch/revision/id command fail closed；canonical snapshot不含request | 只凭notification知道pending、接受stale resolution、为重连写durable request/receipt |
| pending interaction Host crash/takeover | request显示后kill Host并由新Host acquire writer | turn interrupted；新`owner_epoch`从revision 0/empty开始；旧resolution被拒绝；accepted decision若已commit仍可query | 恢复suspended request/coroutine、合成旧Opened/Closed、旧generation接受resolution |
| TUI committed budget/schema GAP | committed cursor client-ahead、schema不兼容或完整suffix超预算 | 丢弃对应cache，fresh canonical snapshot，从新event cut订阅；旧stored event仍可由Inspector/audit按固定cut分页，history按before_sequence分页；provider live GAP重置partial renderer，control GAP重取live snapshot | 删除旧event、root repair、Runtime latch、返回半committed suffix、把GAP当canonical corruption或恢复execution |
| orderly Host close with active subagent | child active且已有部分accepted messages时detach/close | stop child admission；cancel并在共享deadline内join activation/child RuntimeSession；completed/failed/cancelled保持terminal，其余task由current Host置interrupted；超deadline owner先失去session resource access；accepted messages/results保留；subagent job/attempt为0 | detach child继续访问session资源、close成功后task仍active或仍可访问released resource、把child移交worker或等待child业务完成而无限阻塞 |
| Host crash/takeover with active subagent | child active时kill Host并由新Host reattach | acquire transaction按旧`execution_writer_generation`把pending/active task置interrupted；accepted messages/results保留，partial live output丢失；不resume/requeue；显式重新委派创建new task id；已dispatch tool的不确定性只看exact attempt | 恢复child coroutine/RuntimeSession、自动新claim/retry、复用旧task id、从event合成result/completed、无attempt却显示effect unknown |
| subagent exact child observation | 同一task依次接受两条message与一个explicit result，再完成task；并让result inferred/explicit路径竞争 | 三个immutable child有不同stable id；两条`SubagentMessageAccepted`与唯一`SubagentResultAccepted`分别FK exact child；task completion只发status event，不重复result | 三条event都只引用task、payload ordinal定位、explicit result在run completion重复接受 |
| terminal monitor append isolation | monitor callback与Host result acceptance并发，令monitor持有旧handle或抛错 | monitor只能发process-local typed live/operational event；只有current Host接受canonical tool result/entry后通过domain adapter发既有core occurrence；callback失败不否定commit | monitor持有appender/Host guard、terminal-specific committed type、receipt/notification job恢复 |
| prompt queue restart | item pending/claimed时kill | pending或过期claim可恢复，顺序稳定 | checkpoint缺失阻断queue |
| memory index failure | vector/index write失败 | canonical memory fact仍accepted；index标stale可重建 | memory acceptance回滚或Runtime latch |
| no-Oxigraph production | 不设置URL、不启动service并阻断任何localhost:7878连接 | Host启动；memory_search/get/explain、governance、FTS/vector/direct-edge和现有0/1/2-hop recall通过；网络调用0 | startup/close失败、surface pending、silent fallback到另一个graph store |
| bounded graph parity | 固定canonical nodes/relations覆盖direct、reverse、1-hop、2-hop与3-hop-only target | PostgreSQL返回与当前contract一致的direct/1/2-hop结果；3-hop-only target不返回 | 删除Oxigraph时顺便扩大hop、引入raw SQL/SPARQL或改变scope/status过滤 |

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
- `ModelStreamSegmentAccumulator`、durable stream policy/fingerprint、durable Text/Thinking/Data/Tool Start/Segment/End carriers及其recovery/terminal projection已物理删除；7类`RawProvider*`与逐delta semantic-draft重复协议同样物理删除。vendor SDK object不逃出adapter，sanitizer/normalizer直接产出决策7的exact 23类process-local `LiveAgentEvent`，单assembler与live bus的chunking-invariance、snapshot-subscribe、ToolResult-End-before-commit与partial-crash tests通过；authenticated first-party user在无GAP时原样看到Runtime收到的thinking delta，短tool args完整、超限args显式UTF-8-safe截断，展示DTO不改变canonical call或dispatch；
- crash、reply、tool side effect、conversation rehydrate、close语义与7.2完全一致；
- Pulsara独有能力的最小durable boundary通过restart测试；
- 旧151类universal event grammar、schema-v11 auto-registry、historical execution decoder、八个无production constructor的旧type及execution reducer/checkpoint/repair/close owner物理删除，不只是unused/deprecated/dormant；
- 对应旧proof/recovery表、durable event与contract tests删除；selective `agent_events`、committed/live/operational schema与extension tests保留；
- 没有双写、compat reducer、新stable receipt subsystem；
- 9.2全部correctness gate、9.4全部hard guardrail和9.5行为矩阵通过；
- 9.1预算逐项报告；任何偏离有architecture review结论，不能通过schema/JSON/生成代码取巧；
- complete-reset empty database/blob namespace端到端通过；cutover前旧Host/worker/monitor/subagent均已quiesce/fence，外部effect不会被新Runtime导入、继续或重做；
- production source/schema/composition/CLI/Inspector/deployment没有old-data export/import、cold archive connector、offline converter、old→new identity map、`imported_interrupted`、reverse projection或old DB读取面；rollback只通过再次complete reset；
- Protocol v3 Python/Go client同时cutover，v2 Presentation Foundation不在production import graph；
- Protocol v3 canonical snapshot/page通过repeatable-read并发commit test；entry/event cut与bounded `CommittedObservationProjection`至少通过assistant ordered-block渲染、queue mutation、tool attempt/public remote-identity publication、turn interruption、lost-notification与retention/budget GAP test；fresh snapshot在同一MVCC cut读取call/attempt/result/turn并派生public state；user acceptance ACK unknown通过canonical-row command query/idempotency test；
- Protocol v3 snapshot/history/observation对entry content统一使用`ObservationContent`；强制blob-backed assistant/tool result跨多个bounded byte range与codec边界后可exact render，chunk/full digest、scope/capability revoke、tampered reference、missing/corrupt bytes与reconnect tests通过；raw blob/private URL不可读，数据库transaction不跨storage I/O，schema/import graph没有durable download receipt/lease/cursor/projection/repair/content-delivery event；
- TUI只用canonical conversation snapshot/page、read-time committed observation projection与current provider/tool-result/session-control typed live stream完成fresh attach、history/committed/content-live/control-live四类GAP与reconnect重建；Go不直接解释stored event或subject id，没有presentation root、UI ACK或derived delivery state反向成为Runtime authority，也不从raw canonical row自行猜测实时Start/Delta/End；
- canonical row与对应`StoredCommittedEvent`由持有closed Host或job-attempt guard的同一owner、同一transaction写入；所有owner使用统一session allocator lock order，event subject是DB约束的exactly-one typed FK；stale guard、wrong-slot/cross-session/missing subject和plugin/hook append被拒绝；journal consumer故障不否定commit，reopen不读event恢复execution；event随session lifetime全量保留且schema无retention lower bound/prune owner，presentation budget/schema GAP只要求fresh snapshot；
- committed registry在SQL、Python serializer与Python/Go fixtures中exact等于决策7的26 types、13 subject slots与closed type→slot/guard mapping；不存在generic/custom/session-subject逃生口。`ToolOutcomeUnknown`、job retryable-attempt terminal、memory candidate/batch/skip、compaction completion未adopt、terminal monitor与subagent physical run/progress均不能产生A；`PromptRejected`与cancel继续分型；
- `CommittedObservationProjection`只在bounded Gateway read transaction中存在；production schema/import graph没有durable observation projection/cursor/checkpoint、free-form subject kind/id或canonical-subject identity/proof table；
- live/post-commit/operational hook与`ToolDispatchAuthorizationPolicy`物理分离；user view与extension projection类型分离且plugin不可自授/继承。extension principal、manifest-stable handler id/digest、Host-minted process registration、current/previous projection-major negotiation、capability与revocable lease tests通过；exact 49类core registry不接受custom publisher/type或`Unknown + raw JSON`。per-observer/shared-ring/provider/control snapshot event+byte hard cap、timeout/GAP后detach/bounded close drain、ordinary redaction与user/privileged/revoked/S3 negative tests全部通过；ordinary post-commit hook从registration cut后best-effort、overflow GAP/detach且没有durable cursor/journal catch-up/reliable flag。V1第三方durable extension action/tailer为0；policy decision exact三种、rewrite field为0、machine default 2秒/hard cap 5秒，unavailable→confirmation、无controller→deny且未Allow时physical invoke为0；callback/recorder/live owner不进入metadata；
- pending interaction只通过带`owner_epoch/live_revision`的same-Host atomic live-control snapshot-subscribe恢复可见性；Opened/Replaced/Closed竞态不漏观察，Host crash/takeover后新epoch为空、request消失、turn interrupted、stale resolution拒绝，production schema/import graph没有`interaction_requests`、durable live-control row或request recovery owner；
- subagent已接受task/objective/parent-child/message/result/status可跨attach查询，message/result各有stable exact child identity和subject FK，explicit/inferred result恰好接受一次；全部physical child execution绑定当前Host。orderly close bounded cancel/join并把nonterminal task置interrupted且发唯一status occurrence，takeover按旧generation幂等收口，reattach不resume/requeue且重新委派使用new task id；production schema/registry/job catalog/import graph没有`SUBAGENT_RUN_*`、subagent background flag、job handler、attempt/claim/lease/checkpoint或child recovery owner；
- yielded terminal process在同一Host owner内可跨tool call操作，跨owner lookup稳定拒绝；orderly detach/close终止全部owned process group并bounded join后使handle失效，Host crash/takeover不adopt或重放旧process且无accepted completion时只显示interrupted/unknown；production schema/job catalog/import graph没有terminal monitor durable job、launch-token row、process receipt/checkpoint/repair owner；
- mixed text + 全部calls原子commit；每个physical invoke有先行attempt；parallel result exact attempt pairing、partial crash、all-terminal follow-up barrier与ordinal lowering通过跨进程测试；
- foreground每logical call最多一个physical attempt；显式retry只通过新turn/new call表达；late exact outcome不覆盖既有result、不改写旧turn或历史provider attribution，并通过cross-call lineage测试；
- single Host writer与job-attempt claim两个独立fencing domain、job aggregate/attempt lineage与三类safety规则通过跨进程故障注入；
- Stage 2 activation已包含minimal job schema/claim/result-accept与全部foreground-reachable handlers；未迁移capability在catalog/admission不可达；Stage 4只删除旧graph且不发生第二次job authority cut；
- initial context revision与user/turn原子安装；mid-turn只在provider safe point新增revision并推进pointer；每次provider dispatch从固定MVCC cut建立唯一prepared-input handle，每条accepted provider-generated assistant原样保存exact revision与`provider_input_through_sequence`；100→101→102 late-result race和并发commit-order sequence测试通过；compaction不修改transcript/epoch，unreferenced snapshot可GC，被revision引用的snapshot exact保留并通过current-turn-delta rematerialization；
- global blob publication/FK/RESTRICT/24小时orphan GC通过missing-blob与late-install竞态测试，production没有per-domain hold/receipt owner；canonical transcript只通过exact content edge与无状态逐请求鉴权port读取，reference不充当capability；
- V1自动exact context-input audit artifact/model call为0；旧plan/pages/root expectation/materializer/storage/GC/doctor与close semantic drain已物理删除；清空audit artifact并禁用diagnostic capture后，detach/reattach、conversation rehydrate、context rematerialization和新turn仍端到端通过；debug/采样artifact缺失不构成产品失败；
- conversation rehydrate、context rematerialization、effect reconciliation、best-effort audit reproduction的API与测试语义分离，execution replay明确不存在；
- Stage 1保留的physical quiesce test证明DB pool/artifact store释放后没有旧owner task继续访问；Stage 3删owner后对应close await才归零；
- checkpoint、audit、UI故障不会阻断任何foreground canonical path；
- Oxigraph代码、依赖、配置、surface worker/delivery、Inspector/CLI、contracts、tests与部署说明全部物理删除；PostgreSQL-only memory tools、governance、FTS/vector/direct-edge和现有bounded两跳recall在无Oxigraph环境全绿；
- 文档中标记为target delete的旧owner、表、event与contract test要么已物理删除，要么阶段gate明确未完成，不能宣布架构cut完成；
- 删除不得通过给旧owner换名、覆盖旧attempt、把独立authority塞进巨型JSON或再包一层generic receipt来宣称完成；inventory、import/AST gate与行为测试必须共同证明旧恢复图确实消失。

---

## 最终结论

37e21903证明 Pulsara团队已经意识到“诊断材料不能占据semantic event主路径”：compact commit、bounded carrier、best-effort audit和degraded loader都是正确方向。f752a044代码真值仍让每次model call默认写plan/pages/root、永久保留，并让optional audit参与close；同时又保留non-Host teardown retry lineage。因此它是局部authority减法、全局physical ownership加法。

Pulsara当前事故不是一组互不相关的边角bug，而是同一选择的重复后果：**把foreground execution、derived projection和observer delivery都纳入跨进程exact continuation。**

最合适的目标不是继续完善这套恢复图，也不是删除Pulsara的long-horizon、subagent、same-Host terminal monitor和memory能力，而是采用 **Canonical relational conversation kernel with selective domain, effect, and work journals**，并把边界重新冻结为：

> PostgreSQL保存canonical relational conversation facts、selective committed `agent_events`、tool/job physical attempt journals、revision-referenced semantic context snapshots、global blob references、coarse interruption和真正后台job；canonical row拥有current semantic truth，committed event只拥有sequence N上的accepted occurrence truth。committed core冻结为exact 26 types、13 subject slots与`HostWriterGuard | JobAttemptClaimGuard`，二者由持有对应closed guard的同一owner同transaction写入，event以数据库约束的exact typed FK引用subject；
>
> Gateway在一个bounded read cut中把stored event与exact subject组合成ephemeral `CommittedObservationProjection`供TUI消费，不复制完整message到durable event，也不创建第二套presentation authority；snapshot/history/observation中的正文统一为inline或canonical blob reference，后者只通过无状态、bounded、逐请求鉴权并校验digest的`ReadCanonicalContent` hydrate，不建立下载receipt/lease/cursor/projection或event；
>
> model/tool foreground execution与经adapter-local解码、sanitizer/normalizer直接构造的Text/Thinking/Data/ToolCall/ToolResult Start/Delta/End留在进程内；vendor SDK object不逃出adapter，独立`RawProvider*`/逐delta semantic-draft协议删除；pending interaction使用同一process-local typed语义体系下独立的owner epoch/revision与Opened/Replaced/Closed live-control contract；live event使用独立base、bus和bounded queue，observer failure/overflow不阻塞provider，live End不证明canonical acceptance；
>
> yielded terminal process与monitor同样绑定当前Host owner lease；orderly close终止并bounded join，crash/takeover不按PID、handle、job或event收养，未accepted outcome只显示interrupted/unknown；
>
> subagent已接受的task/message/result与terminal status是canonical coordination facts，但全部child execution同样绑定当前Host；Host结束时nonterminal task变interrupted，reattach不resume/requeue，重新委派创建new task id，subagent不进入durable job/attempt或event replay；
>
> crash就是一次明确interruption；reopen只rehydrate canonical rows，不通过event replay恢复execution，也不合成丢失的live lifecycle；
>
> side effect未知由canonical attempt/result/turn确定性推导，统一显示unknown且不自动重试；provider-only synthetic result只闭合下一次model input，不写canonical result或committed event；
>
> checkpoint、audit、durable event consumer、hook和UI永不成为semantic gate；ordinary post-commit hook只从registration cut后best-effort观察，不承诺catch-up，也不能声明reliable；V1第三方durable extension action为0，future跨进程必达extension必须经独立ADR新增具名job。

同一原则最终也删除两层没有独立产品价值的中间面：provider transport delta不再经过durable segment层，completed semantic message直接进入canonical transaction；memory graph不再异步复制到Oxigraph，PostgreSQL直接承载canonical node/relation与现有typed bounded两跳recall。目标系统既没有stream replay illusion，也没有RDF mirror freshness gap；它保留一个统一但分层的typed AgentEvent语义体系，把durable committed facts、process-local live stream与capability-scoped hooks纳入同一extension protocol，却不把execution recovery建立在event replay之上。
