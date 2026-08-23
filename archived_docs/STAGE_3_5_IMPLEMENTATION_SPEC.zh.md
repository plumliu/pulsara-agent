# Pulsara Stage 3–5 物理删除与终局收口实施规格

状态：**DRAFT FOR REVIEW；BASELINE COMMITTED；NO PRODUCTION RELEASE BETWEEN STAGES**

目标：在不改变 Stage 2 产品语义的前提下，物理删除旧 execution recovery、derived authority、legacy projection/Oxigraph graph 与 universal EventLog，最终只保留 **Canonical relational conversation kernel with selective domain, effect, and work journals**。

上位架构真源：[PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)

已激活规格：[STAGE_2_HARD_CUT_IMPLEMENTATION_SPEC.zh.md](STAGE_2_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)

Stage 2 激活证据：[durability_subtraction_stage2_activation.json](benchmarks/suites/core/v1/durability_subtraction_stage2_activation.json)

实施起点：`9dfc79f2d0b21ea45dd313b4a62d6aa191919154`（`refactor: activate relational conversation kernel hard cut`）

起点文档真值：

- REASSESSMENT SHA-256：`7f4168989f734b3cc11a59f06833a642c0edb4d06adf3dd9a1d9deeef76d2bae`；
- Stage 2 spec SHA-256：`8a30fb3db34bff7c152f3450ce5b18c7b403e3e657fb6f53d9e2e1d87b812b4a`；
- Stage 2 activation evidence SHA-256：`65d598db647beafbf10265f931ad95cd5b8b1be2603528321ff3067d453ede54`。

## 1. Outcome

Stage 3–5是删除阶段，不是第二次authority设计。完成后：

- production只构造`KernelHostCore`、Protocol v3、`pulsara_v3` repositories与当前四类minimal durable job handler；
- canonical relational rows继续拥有current semantic truth；selective `agent_events`只拥有accepted occurrence truth；
- exact 26类Committed、23类Live、13个subject slots与2种append guards保持不变；
- provider/tool-result live stream继续process-local typed；不恢复RawProvider、draft、segment或historical live replay；
- yielded terminal与subagent execution继续绑定Host lifetime；不跨Host恢复；
- PostgreSQL继续独占memory facts/relations、FTS、pgvector、direct-edge与bounded two-hop recall；
- 旧RuntimeSession、reducer/checkpoint/repair、Presentation Foundation、projection-job graph、Oxigraph、151类EventType与execution replay在代码、schema、CLI、tests和production import graph中物理消失；
- final fresh database只建立`pulsara_v3`的24张active product relations及最小migration metadata，不再先建立legacy `public` product schema。

本规格不重新定义Stage 2 SQL、DTO、event vocabulary、hook、policy、memory、job retry或Protocol v3语义。实施中若需要改变这些契约，必须停止并另立ADR，而不是借“删除旧代码”顺手修改。

## 2. 当前代码真值

### 2.1 已成立的边界

- Stage 2 activation状态为`activated`，active product relations为24；
- production默认Host为`pulsara_agent.conversation_kernel.host.KernelHostCore`；
- terminal默认入口为Protocol v3；
- clean process只import新Kernel Host与v3 launcher时，旧universal event/EventLog、RawProvider/draft、v2 Presentation、Oxigraph与RuntimeSession加载数为0；
- provider adapter直接产出正式typed Live payload；新Kernel不经过RawProvider、draft envelope或adoption ACK；
- job catalog exact为四类：`BACKGROUND_COMPACTION`、`POST_COMPACTION_MEMORY_EXTRACTION`、`MEMORY_GOVERNANCE`、`MEMORY_INDEX_REFRESH`，全部为具名finite `RETRY_SAFE` contract；
- Stage 2 target tests、Protocol v3、Go test/vet与静态gate已通过，证据由activation manifest保存。

### 2.2 仍物理存在但production-unreachable的旧面

当前tree仍有：

- `event/`的151类universal vocabulary与`event_log/`的writer/serializer/historical decoder；
- `runtime/session.py`及旧Host/resume/subagent/MCP recovery链；
- `llm/segment.py`、`raw_provider.py`、`drafts.py`、旧sanitizing bridge与model stream/control recovery；
- 16个`runtime/authority_materialization`模块；
- 26个`runtime/projection_jobs`模块及9个顶层`event_log`模块；
- 13个`runtime/terminal_presentation`模块、Protocol v2 schema/generated/gateway与Go v2 presentation/cache；
- 旧projection contracts仍被migration/connection基础设施引用；migration 0006–0009的readiness/preparation仍由migration runner实际调用，新job repository暂时复用名为`PROJECTION_MAINTENANCE`的connection lane；
- v3 launcher暂时复用legacy launcher中的通用process-supervision helper；
- new Kernel terminal tool暂时复用带旧durable completion分支的terminal process manager，但Stage 2配置已关闭该分支；
- provider adapters暂时接受并忽略legacy `event_context` keyword；新provider port不包含它；
- settings、legacy wiring、Inspector、CLI、tests与historical migration chain仍包含Oxigraph、projection、EventLog和Protocol v2入口；
- migration 0001–0013仍先建立legacy `public` relations，再建立`pulsara_v3`。

这些是Stage 3–5的删除输入，不是应继续兼容的产品能力。

## 3. 跨阶段硬边界

### 3.1 始终保留

- `pulsara_v3` 24张canonical/product relations及其数据库约束；
- exact 26/23/13/2 vocabulary oracle；
- canonical row与required committed event同owner、同transaction写入；
- Host writer与job-attempt claim两个独立fencing domain；
- message-before-dispatch、attempt-before-effect、unknown不自动retry；
- immutable context binding revision与`provider_input_through_sequence`；
- Protocol v3 snapshot/page/observation/content/live/control契约；
- typed extension registration、capability/lease、ordinary hook no-catch-up与独立policy port；
- current四类job及其finite attempt lineage；
- candidate-first、异步memory governance与closed index freshness disposition；
- global content-addressed blob publication/read/GC；
- terminal和subagent的Host-scoped process-local lifetime；
- verified PostgreSQL connection、transaction与最小migration runner。

### 3.2 禁止新增

- 新canonical relation、committed/live event type、subject slot或append guard；
- 第五类job handler、generic handler、extension job或job schema redesign；
- compatibility reducer、old→new translator、dual-write/dual-reader；
- stable receipt、checkpoint、repair、projection head、delivery ACK或consumer cursor；
- old DB importer、cold reader、archive connector、reverse projection或identity map；
- durable pending interaction、terminal process、subagent execution或live replay；
- Oxigraph替代物、raw SPARQL、第三graph store或超过bounded two-hop的查询能力；
- 为删除方便而改变session-lifetime transcript/event retention。

当前代码没有第五个必须保留的**跨Host必达product handler**。Stage 4默认删除全部legacy projection handlers；如果实施者发现未迁移但仍有明确跨Host completion承诺的handler，必须停止并提交产品证据与ADR，不能自行把它加入job catalog。writer lease renewal、prompt delivery调度、active turn/provider/tool/subagent task、live extension worker、terminal supervision和blob orphan GC属于下文封闭的process-local maintenance，不是job catalog成员。

### 3.3 删除纪律

每个owner按以下顺序处理：

1. 证明Stage 2 product reader/writer不依赖它；
2. 抽离仍被新Kernel使用的纯process-local或基础设施leaf；
3. 删除旧producer、admission和semantic gate；
4. stop/cancel/terminate并bounded join仍可能访问资源的physical task；
5. 删除repair/reconciliation与close await；
6. 删除旧文件、schema、CLI/Inspector surface和owner-specific tests；
7. 运行retained Stage 2 behavior gates与negative import/schema gates。

不得先批量删除tests再判断能力是否丢失。旧contract test只能与其owner在同一slice删除；相应产品行为必须已由Stage 2 test覆盖，或在同一slice补成canonical behavior test。禁止用skip/xfail掩盖删除回归。

## 4. 终局物理边界

~~~text
Kernel Host / exact job worker
  -> pulsara_v3 canonical rows + selective committed journal
  -> tool/job attempt lineage + shared blobs

provider / tool result
  -> typed process-local LiveAgentEvent bus

Gateway / Inspector / context / TUI
  -> canonical reads + ephemeral observation projections

diagnostics
  -> process-local or TTL operational telemetry
~~~

终局中不存在EventLog execution ledger、reducer replay、presentation database、projection delivery graph、RDF mirror或跨Host foreground execution owner。

## 5. 删除归属矩阵

| 旧面 | Stage | 目标处置 | 必须保留的successor |
|---|---:|---|---|
| durable model/tool stream segment、RawProvider/draft、model recovery | 3 | 物理删除 | normalized provider transport、assembler、23类Live |
| RuntimeSession/resume/finalization/interaction recovery | 3 | 物理删除 | canonical rehydrate、running→interrupted |
| reducer/checkpoint/authority materialization/repair | 3 | 物理删除 | direct canonical query与DB constraints |
| terminal Presentation Foundation与Protocol v2 | 3 | Python/Go/schema/generated一起删除 | Protocol v3 + canonical observation/content |
| legacy terminal durable completion branch | 3 | 从process-local manager剥离后删除 | Host-owned process manager、live terminal events |
| legacy subagent recovery/teardown generation | 3 | 删除 | canonical task/child rows + Host close interruption |
| context exact-audit storage/materializer/doctor/GC | 3 | 删除 | optional operational diagnostics only |
| projection connection contracts | 4 | 抽离中性DB leaf后删除 | neutral background-work lane/transaction |
| 0006–0009 projection migration preparation/readiness | 5 | Stage 4抽成sealed migration-only leaf；与旧migration universe同删 | Stage 5 clean baseline不需要preparation |
| legacy projection-job runtime/schema contracts | 4 | 物理删除 | 当前四类minimal jobs |
| Oxigraph/SPARQL/surface delivery/settings/health | 4 | 仓库边界完整删除 | PostgreSQL typed memory reads |
| legacy projection/Oxigraph CLI、Inspector和fixtures | 4 | 删除 | canonical job/memory inspection |
| 151 EventType、EventLog、historical decoder、replay | 5 | 物理删除 | selective committed journal + canonical rehydrate |
| legacy public schema/migration catalog/protected relations | 5 | reset-only clean baseline替换 | pulsara_v3 24 relations + minimal migration metadata |
| lazy compatibility facades与legacy adapter keyword | 3–5 | 随最后消费者删除，不保留shim | direct new imports |
| Stage 0旧151 inventory/oracle tests | 5 | 由final 26/23/13/2 guard替换 | final architecture oracle |

## 6. Stage 3：删除execution recovery与derived authority

### 6.1 Stage 3 outcome

Stage 3结束时，旧foreground execution/recovery、derived reducer/checkpoint和Protocol v2 presentation代码已物理删除；Stage 2 Kernel happy path、Protocol v3与selective/live AgentEvent保持行为不变。

### 6.2 S3-A：建立跨Stage删除manifest与retained gates

在第一个删除diff前生成可重复manifest，至少记录：

- 目标module/file、production/test importers、owner与close await；
- 旧event/schema/table/CLI command；
- 对应Stage 2 successor与retained behavior test；
- `delete | extract-neutral-leaf | retain`三态处置。

manifest是跨Stage、逐checkpoint重算的代码真值，而不是只在S3-A生成一次的静态清单。Stage 3、4、5各自在开始与结束时，都必须以**上一checkpoint实际HEAD**重新扫描并更新：最后一个production/test消费者、实际删除stage、successor test、处置结果与剩余引用数。后续stage不得复制Stage 3开始前的计数；已删除目标不得重新出现，新增最后消费者必须在进入下一slice前分类。

manifest只用于删除审计，不成为运行时registry、compat map或durable state。

### 6.3 S3-B：先抽离新Kernel仍使用的中性leaf

只允许以下机械抽离：

- v3 launcher所需的process spawn/TTY supervision与exit DTO移到不import v2 gateway/generated的中性模块；随后删除legacy launcher；
- terminal process manager保留的最终API只允许：`exec/yield`、按Host owner列举、bounded process-local output snapshot、stdin write/close、kill与owner-scoped close/join；DTO只允许process/terminal identity、command/cwd、backend/IO mode、running或terminal status、bounded output、exit/timing与Host owner identity。`AgentEvent`、`EventContext`、runtime/run/reply origin、completion candidate/recorded event、semantic settlement、retry timer/count、reservation、receipt、durable monitor identity与callback recorder全部从类型和签名中移除；
- normalized provider transport使用的通用size/circuit-breaker常量移出`authority_materialization`；不得保留compat re-export；
- 新job repository使用的connection lane改为中性`BACKGROUND_WORK`或等价名字；lane只表示pool scheduling，不表示projection authority；
- new Kernel所需的MCP配置文件发现/解析抽成neutral config leaf；enabled MCP继续在建立session前以typed composition-unavailable fail closed。该leaf不得import supervisor、SDK connection、continuation secret、installation、tool execution、interaction recovery或任何MCP I/O owner；
- package facade移除已删除legacy export，不保留动态fallback；
- 旧LLMRuntime最后消费者删除后，OpenAI adapters同时移除被忽略的legacy `event_context`参数。

抽离不得复制旧状态机、fingerprint、receipt或generation。若leaf携带任何resume/repair语义，应删除而不是搬家。

### 6.4 S3-C：删除旧foreground owner

删除范围包括：

- `runtime/session.py`及只服务它的run/finalization/model control/stream recovery；
- `llm/segment.py`、`raw_provider.py`、`drafts.py`与旧sanitizing/adoption bridge；
- committed reducer、post-fold、repair、checkpoint与`runtime/authority_materialization/`；
- 旧transcript/provider/tool terminal projection与foreground projection producer；
- pending interaction跨Host resume/reconciliation；
- child RuntimeSession、subagent recovered occupancy与teardown retry generation；
- `runtime/projection_jobs/compaction_memory_driver.py`与`compaction_memory_settlement.py`这两个直接消费`RuntimeSession`的模块及其owner-specific tests；剩余不依赖RuntimeSession的projection worker/schema graph明确留给Stage 4；
- legacy MCP supervisor、connection/SDK、installation、continuation secret、tool execution、input-required recovery及其旧Host/subagent wiring；终局只保留上节neutral config detection，不保留MCP I/O supervisor；
- 逐call exact context audit materializer/storage/GC/doctor；
- generic command receipt与run finalization repair。

`conversation_kernel`、`ports/live_agent_event.py`、`ports/provider_stream.py`、canonical rows和accepted interaction decisions不得被删除或改写成旧语义。

### 6.5 S3-D：删除Presentation Foundation与Protocol v2

一个slice内同时删除：

- Python terminal presentation service、history root/head/checkpoint/retention/projection/restore/viewport；
- v2 gateway、schema、generated Python、fingerprint fixture与generator分支；
- Go v2 protocol/generated、root-indexed presentation/cache/client/app分支及只验证v2的tests；
- legacy TUI launcher与CLI选择分支；
- presentation/command receipt/operational snapshot owner-specific schema contract。

Protocol v3 schema、generated Python/Go、kernelapp/kernelclient与canonical observation/content hydrator是唯一保留面。不得增加v2→v3 translator。

同一Stage还必须完成CLI与package facade hard cut，而不能把import修复留到Stage 5：

- 保留`host run/repl/tui`名称时，它们只能构造`KernelHostCore`与Protocol v3；删除legacy Host选择分支、resume/repl controller、legacy approval/plan/MCP interaction loop及其eager imports；
- 删除checkpoint doctor/GC、exact-audit doctor及所有依赖本Stage被删owner的命令；
- 删除`LegacyHostCore`、RuntimeSession、authority materialization、v2 launcher、MCP supervisor和旧event类型的顶层CLI imports与Host/package facade exports；
- Stage 4仍负责projection/Oxigraph命令，Stage 5只负责仍存活的EventLog inspection与旧migration surface。不得为保持CLI collection而留下lazy compat import。

### 6.6 S3-E：压缩close与删除旧tests

新Kernel close保持三段语义：

~~~text
stop admission
  -> cancel/terminate + bounded join process-local owners
  -> canonical interruption/lease release + resource close
~~~

结构目标：0 reducer barrier；不等待audit/checkpoint/presentation/job业务完成；总await `<=12`仅为审查预算。真实process与subagent task在依赖释放前必须停止或被剥夺访问能力；“MCP I/O”只指本Stage尚待删除的legacy owner，删除后new Kernel close不再拥有或等待MCP supervisor。

旧recovery/repair测试与owner同删；保留或补强：partial provider crash、tool unknown、pending interaction takeover、subagent interruption、terminal process kill/join、pool关闭后无late I/O。

### 6.7 Stage 3 data策略

Stage 3不迁移产品数据，不增加product relation。legacy `public`表可暂时作为空且不可达的migration壳保留到Stage 5；不得重新授权、查询或用作冷审计。真实production reset仍需operator明确授权，coding agent只可使用ephemeral database/blob namespace。

### 6.8 Stage 3 exit gate

- clean production import graph不含RuntimeSession、segment/raw/draft、model recovery、reducer/checkpoint/repair、authority materialization、Presentation Foundation或v2 gateway；
- CLI与package facade可在上述模块物理消失后独立import；production run/repl/tui只到达new Kernel/v3，checkpoint/audit/legacy Host/MCP supervisor命令与export为0；
- `compaction_memory_driver.py`、`compaction_memory_settlement.py`随RuntimeSession为0；Stage 4-owned projection graph仍可import且不得反向保留RuntimeSession；
- terminal process API/DTO符合S3-B allowlist，对`AgentEvent`、completion candidate/retry/receipt/durable monitor的类型引用为0；new Kernel最多读取neutral MCP config并对enabled配置fail closed，不构造MCP I/O owner；
- durable stream segment candidate/write与historical Start/End synthesis为0；23类Live保持exact；
- Protocol v3 Python/Go、content hydrate、GAP/reconnect全绿；v2 schema/generated/launcher为0；
- open只rehydrate canonical rows，除旧running→interrupted外不执行repair，不调用provider/tool；
- close三段、0 reducer barrier，physical quiescence fault tests通过；
- exact 26/23/13/2、24 relations与四类job catalog未改变；
- Stage 2 retained suite、Go test/vet、ruff、compileall与full collection通过；
- 没有新增skip/xfail、compat shim或durable owner。

## 7. Stage 4：删除legacy projection jobs与Oxigraph

### 7.1 Stage 4 outcome

Stage 4不发生第二次job authority cut。当前四类minimal job是唯一承诺跨Host完成的durable product-work authority；所有旧projection handlers与Oxigraph graph默认删除，job catalog仍exact为4。合法process-local maintenance继续存在，但不得获得durable completion承诺。

### 7.2 S4-A：解除storage基础设施对projection contracts的依赖

- 将connection pool lane、transaction capability、migration lock与schema verification所需的通用部分放入中性storage模块；
- 删除`ports.projection_jobs`、`projection_jobs.contracts/migration_state`对runtime composition、verified connection和new job repository的传递依赖；
- 为保证Stage 4 checkpoint仍可从empty database fresh migrate到v13，把migration 0006–0009实际需要的readiness/preparation、transaction capability和progress DTO抽成一个**sealed migration-only legacy leaf**。它只能由old-universe migration runner/CLI migrate与其migration tests import，不得被Host、job worker、repository、Inspector或production composition import，也不得启动worker、持有后台lease或写new Kernel product row；
- sealed leaf必须包含自己所需的closed DTO/SQL preparation，不能通过compat re-export继续依赖`ports.projection_jobs`或`runtime/projection_jobs`。它只是让0000–0013在Stage 4 checkpoint仍可执行的临时代码叶，与旧SQL/registry在Stage 5同一slice删除；
- sealed leaf只支持**empty-world fresh migration**：进入migration 0006–0009 readiness前，在同一数据库identity/advisory-lock domain内证明所有legacy product relation、session、canonical mutation、projection coverage input/job/receipt均为空。发现任一row或coverage source即返回typed non-retryable `MIGRATION_UNIVERSE_RESET_REQUIRED`，不得启动coverage drain、调用旧projection handler、伪造empty receipt或把non-empty old database推进到v13；
- runtime write admission若只保护legacy public relations，随Stage 5 clean baseline删除；Stage 4期间不得让它成为new Kernel mutation authority；
- 不把旧lease/coverage/target-head/receipt字段复制进neutral storage API。

### 7.3 S4-B：删除projection-job graph

删除顶层`projection_jobs/`、Stage 3余下的`runtime/projection_jobs/`及对应service/worker/registry/seeder/surface/coverage/migration/inspection/repair tests和CLI。sealed migration-only leaf及其old-universe fresh-migrate tests是唯一临时例外；不得把projection runtime package改名整体搬入storage。

Stage 2四类handler不迁移schema、不改retry、安全等级或result acceptance。未在四类catalog中的legacy handler直接删除；发现产品仍依赖时停止并另立ADR。

### 7.4 Process-local maintenance边界

以下是终局封闭allowlist，不计入四类durable job：

- Host writer lease renewal与prompt queue delivery wake/scan；
- 当前session的turn、provider、tool、subagent与terminal process task；
- live bus/extension subscription worker及bounded observation delivery；
- Host-level blob orphan scan/GC。

allowlist成员必须同时满足：没有跨Host completion承诺；没有durable cursor/receipt/maintenance-attempt row；丢失唤醒可由level read/周期bounded scan恢复，或该工作可安全丢弃；Host close时可bounded cancel/join且失败不否定canonical commit。allowlist中的foreground tool task仍由Stage 2 canonical tool attempt、dispatch fence与crash→unknown契约约束，process-local不等于可自动retry。任何**新增background maintenance**若要求跨Host必达、会发起不可重复external effect或不满足上述条件，不能私自加入allowlist；它必须映射到现有exact-four具名job，否则停止并另立ADR。

### 7.5 S4-C：完整删除Oxigraph/SPARQL面

同一stage删除：

- `graph/oxigraph.py`、RDF/SPARQL adapter与Oxigraph lowering；
- `CanonicalMutationSurface.OXIGRAPH`、surface delivery/worker/retry/dead-letter/head/health；
- `oxigraph_url` setting、环境变量、CLI、Inspector/doctor、deployment/bootstrap；
- Oxigraph unit/integration/dogfood fixtures与test-support service启动；
- 只为generic raw SPARQL存在的GraphStore methods。

保留PostgreSQL typed fact/relation、FTS、pgvector、direct/reverse edge和既有0/1/2-hop recall。不得以删除SPARQL为理由新增通用SQL/graph DSL。

### 7.6 S4-D：收口CLI、Inspector与docs

- 删除`db projections`、surface retry/decommission/seed repair、legacy binding plan等命令；
- Inspector只展示canonical session/turn/tool/job/memory/event与derived freshness，不展示旧candidate/receipt/checkpoint owner；
- settings不接受或输出Oxigraph配置；
- README和运行文档只在Stage 4真正完成后同步产品真值；历史REASSESSMENT可保留“为何删除”的证据，不参与production grep gate。

### 7.7 Stage 4 data策略

不导入旧job、surface delivery或Oxigraph数据，不建立bridge。legacy public tables继续empty/unreachable，最终由Stage 5 clean reset消失。不得运行old/new worker并行。

### 7.8 Stage 4 exit gate

- job catalog仍exact四类，新增handler为0；所有**要求跨Host生命周期保证完成的durable product work**只使用`durable_jobs`/`durable_job_attempts`；durable job executor自己的poll task与claimed-attempt task由exact-four catalog、claim与finite attempt gate单独证明，所有**非durable-job execution**的process-local task才必须来自7.4 allowlist并满足其准入条件；
- projection-job runtime/ports/contracts/CLI/tests的production与test-support import命中为0；migration runner到sealed migration-only leaf是唯一临时例外，且empty database仍可fresh migrate到v13；
- non-empty legacy fixture在版本5–8稳定返回`MIGRATION_UNIVERSE_RESET_REQUIRED`且projection handler/coverage drain调用数为0；
- clean Kernel/Host/worker import graph无法到达sealed migration-only leaf；leaf不拥有runtime task/worker/lease，Stage 5删除目标已进入当期重算manifest；
- Oxigraph dependency、code、settings、environment、surface、Inspector/CLI、fixtures与网络探测为0；
- 无Oxigraph环境下memory search/get/explain、governance、FTS/vector/direct-edge与0/1/2-hop golden全绿，3-hop-only仍不返回；
- desired/applied lost-wake、same-target exhausted不重建与closed partial disposition保持通过；
- Host close不等待job；stale claim拒绝、attempt lineage与one-provider-call-per-attempt保持通过；
- Stage 2/3 retained gates与full collection通过，无skip/xfail掩盖。

## 8. Stage 5：退役universal EventLog并建立clean baseline

### 8.1 Stage 5 outcome

Stage 5删除151类universal grammar、EventLog execution ledger、historical decoder/replay和legacy migration universe。selective committed journal、process-local live protocol与operational diagnostics成为唯一AgentEvent分层。

### 8.2 S5-A：定型AgentEvent物理分层

保留现有Stage 2 vocabulary和行为，可机械整理物理package，但不得改变数量或wire/schema：

- Committed：exact 26；只由Host writer或exact job-attempt claim在canonical transaction中append；
- Live：exact 23；独立process-local base/bus/queue/snapshot，不进入durable serializer；
- Operational：TTFT/retry/buffer/cache/diagnostic，默认只进telemetry；
- extension只能订阅typed/redacted projection，不能发布formal event或获得appender。

删除151类`EventType`、`AgentEvent` universal union、`CustomEvent`、schema-v11 auto-registry与historical execution upcaster。不得让Live/Operational注册进Committed serializer来“复用代码”。

### 8.3 S5-B：删除EventLog与execution replay

删除：

- `event_log/` writer、in-memory/postgres execution log、accounting/materialization、prompt-queue companion与historical decoder；
- `replay/` timeline/message reducer/provenance/tool-result receipt；
- 旧Host/CLI/Inspector/test factory中所有EventLog与RuntimeSession入口；
- 旧151 inventory generator、manifest与只验证obsolete grammar的tests；
- 旧event callback/publisher/serializer facade和lazy compatibility exports。

selective committed journal不保留第二个`event_log` package owner：其唯一physical owner是`conversation_kernel`中对`agent_events`的closed repository append与canonical query/observation read。appender仍只能作为canonical transaction内部方法接受两类guard，不能成为通用publisher；query不能证明canonical row存在。保留canonical rehydrate、effect reconciliation、best-effort audit reproduction的分名API；任何API不得命名或实现execution replay。

### 8.4 S5-C：reset-only clean migration baseline

本阶段冻结为**migration universe reset**，不是给旧数据库增加`DROP` forward migration。唯一clean universe contract如下：

~~~text
universe_id          = "pulsara.conversation-kernel.v1"
universe_generation  = 1
baseline_version     = 0
baseline_resource    = "0000_conversation_kernel_baseline.sql"
catalog_resource     = "0000_conversation_kernel_expected_catalog_v1.json"
grant_resource       = "0000_conversation_kernel_runtime_grants_v1.json"
version_domain       = contiguous integers from 0 within this universe
ledger               = public.pulsara_schema_migrations  # infrastructure only
~~~

#### 8.4.1 Verified PostgreSQL binding v2与admission graph同切

Stage 5不能只从baseline删SQL对象；必须在同一不可发布slice完成connection contract hard cut。`VerifiedPostgresSchemaBinding v2`使用domain `pulsara:verified-postgres-schema-binding:v2`，closed字段只包括：database target/name/OID、exact `public` search path、runtime role、server version、`public.vector` version、migration universe ID/generation/fingerprint、migration head与registry prefix、verified catalog fingerprint、runtime grant-policy fingerprint、verification-contract fingerprint及binding fingerprint。这里的verified catalog fingerprint固定为现有`pulsara:postgres-observed-catalog:v1`对匹配expected fast+deep catalog所得的combined fingerprint；grant-policy fingerprint对应clean grant artifact，只有全部requirements satisfied才签发binding。

binding v2不含runtime-write epoch、guard secret、maintenance mode或admission-lock identity。verifier签发binding前验证上述database/universe/catalog/grant/extension事实；physical connection checkout只重验database/role/search-path identity与ledger中的universe/head/prefix等于binding，不读取epoch、不调用admission function，也不安装transaction-local generic guard。

同一slice必须删除：`runtime_write_admission.py`及其projection fact依赖、epoch/secret/protected-relation rows、normal/maintenance guard functions、canonical relation triggers、schema binding v1字段、verifier/provider/session-bootstrap/memory-UOW/runner中的全部epoch/guard调用和旧transaction identity字段。Stage 4若已删除某调用面，Stage 5 guard仍要求production/test-support引用总数为0；不得用constant epoch或no-op SQL function保留兼容。

这不改变产品mutation authority：Host writer generation与job-attempt claim generation仍由`pulsara_v3` canonical row/CAS、closed application guard和repository transaction验证；migration仍由admin role + advisory lock隔离。binding v2只证明“连接到哪一个已验证schema”，不成为第三种domain mutation guard。

#### 8.4.2 Migration identity的唯一无环编码

本阶段复用现有`postgres_schema_fingerprint`编码，且禁止另写JSON/hash helper：

~~~text
canonical_json_bytes(value)
  = UTF-8 JSON；dict key按Unicode code point排序；tuple保持顺序并编码为array；
    只接受null/bool/int/string/tuple/closed string-key dict；
    ensure_ascii=false，separators=(",", ":")；拒绝float/bytes/list/set/enum。

FP(domain, payload)
  = "sha256:" + lowercase_hex(
        SHA256(UTF8(domain) || 0x00 || canonical_json_bytes(payload))
    )
~~~

version-0 baseline contract严格为：

~~~text
baseline_contract = FP(
  "pulsara:postgres-migration-baseline-contract:v1",
  {
    "schema_version": "postgres_migration_baseline_contract.v1",
    "version": 0,
    "name": "conversation_kernel_baseline",
    "resource_name": "0000_conversation_kernel_baseline.sql",
    "resource_sha256": <64 lowercase hex>,
    "transaction_mode": "atomic",
    "catalog_resource_name": "0000_conversation_kernel_expected_catalog_v1.json",
    "catalog_sha256": <64 lowercase hex>,
    "grant_resource_name": "0000_conversation_kernel_runtime_grants_v1.json",
    "grant_sha256": <64 lowercase hex>
  }
)
~~~

它不包含`universe_fingerprint`或registry prefix。随后唯一允许的universe与genesis公式为：

~~~text
universe_fingerprint = FP(
  "pulsara:postgres-migration-universe:v1",
  {
    "schema_version": "postgres_migration_universe.v1",
    "universe_id": "pulsara.conversation-kernel.v1",
    "universe_generation": 1,
    "baseline_version": 0,
    "baseline_resource_name": "0000_conversation_kernel_baseline.sql",
    "baseline_resource_sha256": <same SQL SHA-256>,
    "catalog_resource_name": "0000_conversation_kernel_expected_catalog_v1.json",
    "catalog_sha256": <same catalog SHA-256>,
    "grant_resource_name": "0000_conversation_kernel_runtime_grants_v1.json",
    "grant_sha256": <same grant SHA-256>,
    "baseline_migration_contract_fingerprint": baseline_contract
  }
)

genesis_registry_prefix = FP(
  "pulsara:postgres-migration-registry-prefix:v2",
  {
    "universe_fingerprint": universe_fingerprint,
    "migration_contract_fingerprint": baseline_contract
  }
)
~~~

Python golden vector固定为：SQL/catalog/grant checksum分别取`"11" * 32`、`"22" * 32`、`"33" * 32`时，`baseline_contract = sha256:8390ab92c98ed167b03a3fd73943750bd23b148538c4eb5f75714b5398cbd240`，`universe_fingerprint = sha256:9f3b3cc41831e3dd7ddff91ff9b0c4f35d421745c25a3d346331c95a2073ca19`，`genesis_registry_prefix = sha256:62c84b5c8e9dec93c3c76f1ba4da1892983dd431bc1be51d6d3d9cb12d7cdcc4`。跨binary、ledger、verifier与ACK confirmation必须通过同一golden；任何字段增删、domain或encoding变化都是new universe contract。

new ledger每行都必须包含`universe_id`、`universe_generation`与`universe_fingerprint`，并继续保存version、name、SQL checksum、migration-contract fingerprint、registry-prefix fingerprint、applied time与application version。数据库CHECK把ID/generation限制到该universe，`version`仍为主键且name唯一；version 0是ledger genesis。clean baseline及未来同universe migration仍使用“tuple index等于contiguous version”的closed registry规则。

#### 8.4.3 Database-scoped extension边界

clean baseline的exact **required extension set**只有`vector`：schema必须为`public`，版本必须`>= 0.5.0`，且catalog verifier必须确认canonical SQL使用的`public.vector` type/operator shape。`pgcrypto`来自legacy projection/write-admission链；clean v3 SQL和Kernel没有使用证据，因此Stage 5彻底移除其requirement、安装、grant和catalog fingerprint。数据库里由其他owner预先安装的`pgcrypto`可以继续存在，并作为unrelated extension被clean verifier忽略。

extension是database-scoped retained capability，不属于默认Pulsara reset scope。compatible pre-existing `public.vector`直接采用；缺失时baseline可在admin授权范围内`CREATE EXTENSION ... WITH SCHEMA public`；存在于错误schema、版本过旧或required type/operator shape不兼容时fail closed，不自动drop/relocate/upgrade。只有dedicated database且operator对exact extension另行明确授权时才能删除extension；共享数据库reset永远不删除它。expected catalog绑定required set与observed required extension identity，但不要求数据库的unrelated extension集合精确相等。

#### 8.4.4 Ledger识别、reset与confirmation

`universe_fingerprint`绑定上述静态identity、baseline contract及三个resource checksum；expected catalog只描述最小migration metadata、exact required extension和`pulsara_v3` exact 24 product relations，grant artifact只描述admin/migration/runtime角色所需closed grants。binary、ledger genesis、binding v2、deep verifier与activation evidence必须报告同一fingerprint，不增加另一张universe state表。

runner在取得migration advisory lock后先做closed识别：

- 无ledger且Pulsara-owned world为空：允许原子安装version 0；compatible retained extension不使world变为non-empty；
- ledger的列形状、universe ID/generation/fingerprint与registry genesis均与new contract一致：按new registry验证/前进；
- 发现old v13 ledger、old registry prefix、legacy Pulsara relation或任何不同universe：返回typed non-retryable `MIGRATION_UNIVERSE_RESET_REQUIRED`（wire/CLI value固定为`schema_migration_universe_reset_required`），不得误报普通behind/ahead/history conflict，不得自动drop、upgrade或import；
- baseline commit ACK unknown：重新连接后只用ledger genesis + exact catalog + grants确认`FULL | NONE | CONFLICT`；`FULL`接受，`NONE`可在仍为空世界时重试，`CONFLICT`保持服务停止并请求operator处置。

执行顺序由独立[Stage 5 clean-baseline runbook](STAGE_5_CLEAN_BASELINE_RUNBOOK.zh.md)约束：

1. 停止所有Pulsara进程并确认无外部effect会被继续或重做；
2. 对真实环境reset取得operator针对exact endpoint/database/schema/blob boundary的明确授权；测试只使用ephemeral store；
3. 清空Pulsara-owned database schema/data、blob namespace与derived state，但默认保留database-scoped extensions；reset中断时保持服务停止，重新做read-only empty-world inventory后才可继续，不得在partial old/new world启动Host；
4. 从binary删除legacy migration SQL、sealed preparation leaf、expected catalogs、protected-relation registry与runtime-write admission graph；
5. 用version-0 clean baseline原子建立new ledger genesis、采用或安装exact required `public.vector` capability，并建立`pulsara_v3` 24张product relations、constraints/functions/grants；
6. fresh migrate + universe/catalog/grant deep verify + Kernel dogfood，并保存不含secret的baseline confirmation evidence；
7. production role只能访问`pulsara_v3`所需对象，不能解析legacy public product relation。

不得保留0001–0013“先建旧表再删”的链，不提供旧catalog upgrade、import、cold reader或reverse migration。verified runner只保留transaction/advisory lock、universe recognition、commit confirmation与catalog/grant verification；不得保留execution write-guard secret、projection preparation或legacy protected-relation semantics。当前Stage 2 runbook不适用于本次migration universe reset。

### 8.5 S5-D：删除最后compat surface

- package `__init__`不再暴露legacy Host、LLMRuntime、EventLog、v2 terminal、projection或Oxigraph symbol；
- CLI只提供new Kernel、Protocol v3、canonical DB/job/memory inspection与必要migration命令；
- 删除Stage 3/4后仍存活的EventLog inspection、historical decoder与old-universe migration命令/option；`db migrate/verify`只理解new universe，遇old v13只返回typed reset-required与runbook引用；
- test support默认只构造new Kernel repositories；
- Stage 0的151 inventory证据可留在Git历史和REASSESSMENT，不再是active CI fixture；active oracle只验证26/23/13/2；
- deployment/env/docs不再要求旧service、old DB或Oxigraph。

### 8.6 Stage 5 exit gate

- production/test-support import graph中`event_log`、old `event.events.EventType`、execution `replay`、RuntimeSession、v2 protocol、projection jobs与Oxigraph命中为0；
- committed registry exact 26、live registry exact 23、formal total exact 49、subject slots 13、append guards 2；
- custom/free-form event、RawProvider/draft/segment、generic receipt/cursor/repair为0；
- product relations exact 24且只在`pulsara_v3`；legacy public product relation为0；
- `VerifiedPostgresSchemaBinding v2`在clean baseline上可签发并完成repository checkout；runtime-write epoch/secret/function/trigger、binding v1 admission字段与production/test-support调用命中为0；stale Host writer与job-attempt claim仍被各自canonical guard拒绝；
- required extension exact为`public.vector >= 0.5.0`；compatible pre-existing vector可采用，wrong-schema/too-old/incompatible shape fail closed；`pgcrypto` requirement为0且unrelated pre-existing pgcrypto不造成catalog drift；reset默认不删除extension；
- fresh empty database只运行version-0 clean baseline并通过universe/catalog/grant deep verification；old v13 fixture稳定返回`MIGRATION_UNIVERSE_RESET_REQUIRED`且不发生DDL；
- baseline/universe/genesis prefix三项Python golden vector与8.4.2 exact相等，binary、ledger、verifier、binding与ACK confirmation无第二编码实现；
- baseline ACK `FULL/NONE/CONFLICT`确认与reset中断保持quiesced的fault tests通过；activation evidence绑定universe、baseline SQL、catalog和grant fingerprints；
- text/tool/multi-tool、rehydrate、Protocol v3、hooks/policy、jobs、memory、terminal/subagent Host lifetime、blob与crash matrix全绿；
- production source没有old DB/import/archive/reverse projection；
- full pytest collection error为0，除明确environment scope外全部通过；Go test/vet、ruff、compileall、protocol generation与`git diff --check`通过；
- 没有compat shim、dual authority或新的durable owner。

## 9. Test与删除策略

### 9.1 Retained oracle

每个stage始终运行：

- 全部`tests/test_stage2_*`或其后续同语义重命名；
- exact 26/23/13/2 architecture gates；
- provider/model adapter与legacy参数删除边界测试；
- Protocol v3 Python/Go cross-language与content hydrate；
- canonical PostgreSQL runner/dogfood；
- message-before-dispatch、attempt-before-effect和unknown no-retry；
- Host takeover/job claim fencing；
- binding v2 checkout与无runtime-write-admission条件下的Host/job independent fencing；
- memory PostgreSQL-only与bounded two-hop；
- terminal/subagent Host-close interruption；
- hook failure/capability/no-catch-up与policy timeout/headless no-dispatch。

### 9.2 旧test的合法删除条件

一个test只有同时满足以下条件才可删除：

1. 它只构造待删owner或只断言待删event/table/receipt/checkpoint；
2. 对应生产owner在同一diff删除；
3. 产品行为已由retained canonical test覆盖，或同diff补充；
4. test support中不存在继续暴露旧factory的默认路径。

不得只因full suite变红就批量删除目录；也不得把旧tests全部保留并为其重建compat实现。

### 9.3 Full suite口径

Stage 3/4 construction允许旧owner测试在其删除slice前暂时存在；每个slice合入时必须无未分类新增失败。Stage 5完成时不再接受“legacy red”，因为legacy owner和test都应已经物理删除。环境型skip必须与架构无关且有明确原因。

### 9.4 Manifest checkpoint gate

每个Stage的开始/结束evidence必须附当期重新生成的删除manifest摘要：扫描HEAD、目标数、最后消费者数、`delete/extract/retain`分布、未分类数与相对上一checkpoint的变化。未分类必须为0；Stage 5必须从Stage 4完成HEAD重算，不能使用Stage 3初始manifest证明最终零引用。

## 10. Architecture guards

最终CI至少包含：

- clean-process production import probe；
- forbidden module/path/import probe；
- committed/live/subject/guard generated oracle；
- product relation/catalog/grant exact probe；
- job catalog exact-four probe；
- no-v2/no-Oxigraph/no-EventLog/no-RuntimeSession source probe；
- canonical repository不import execution replay、live bus不import durable serializer、hook不import appender；
- Go只编译Protocol v3，generated v2 package为0；
- migration tree只包含new-universe version-0 clean baseline及其clean catalog/grant artifacts；ledger genesis与runner报告`pulsara.conversation-kernel.v1` generation 1；old v13稳定返回typed reset-required；
- migration fingerprint只能调用8.4.2的canonical encoder并通过固定golden；required extension manifest exact为`public.vector >= 0.5.0`，`pgcrypto`不在required set；
- verified connection binding只允许v2 closed fields；runtime-write admission module/SQL object/trigger/callsite为0，Host/job domain guards仍有negative tests；
- no old DB/import/archive/reverse-projection command；
- Host close reducer barrier为0；
- package facade全部public export可解析且不含legacy symbol。

历史架构文档允许提及被删除名词；source probe应限定production、active tests/config/deployment，不能为了得到字符串0而篡改REASSESSMENT证据。

## 11. Failure matrix

| 故障 | 正确结果 | 禁止结果 |
|---|---|---|
| 删除segment后provider中途crash | canonical turn interrupted；partial live消失 | 合成历史End或恢复provider cursor |
| 删除RuntimeSession后reattach | 读取canonical rows继续新turn | replay旧coroutine/tool/subagent |
| terminal/subagent close超时 | bounded stop后状态清晰并报告diagnostic | detach仍可访问已释放resource的task |
| v3 launcher helper抽离失败 | Stage 3停止，保留原文件直到修复 | 为v3保留整个v2 gateway graph |
| Stage 3删除RuntimeSession | 两个直接依赖它的compaction projection模块同Stage删除；剩余graph留Stage 4 | 留下无法import的半包或compat RuntimeSession |
| Stage 4 fresh migrate旧链 | 只由sealed migration-only leaf执行0006–0009 preparation | 从Host/runtime导入leaf或提前删掉使checkpoint失绿 |
| Stage 4 leaf发现legacy row/coverage input | typed reset-required，handler/drain调用数0 | 伪造ready/receipt或迁移non-empty old DB |
| projection handler无successor | 删除；capability继续disabled | 临场添加generic job handler |
| Oxigraph absent | PostgreSQL memory完整工作 | silent fallback、health degraded gate |
| job lease丢失 | 维持Stage 2 finite retry/safety语义 | 恢复projection receipt/repair graph |
| committed journal consumer失败 | canonical commit仍成立 | rollback row或安装global latch |
| old v13 ledger由new runner打开 | typed `MIGRATION_UNIVERSE_RESET_REQUIRED`，不执行DDL | 当作behind/ahead并尝试upgrade |
| clean schema无runtime-write epoch | binding v2签发且repository正常checkout；Host/job各自fencing仍生效 | 调用缺失函数、constant epoch或移除domain guard |
| shared DB已有compatible vector | 保留并采用，不进入reset scope | 因Pulsara reset而drop/reinstall |
| vector wrong schema/too old/incompatible | baseline/verifier fail closed | 自动relocate/upgrade或静默接受 |
| final reset中断 | 保持服务停止；read-only重验边界与empty world后按runbook继续 | 部分旧/新schema上线 |
| clean baseline ACK unknown | 以new genesis + exact catalog/grants确认FULL/NONE/CONFLICT | 盲目重跑drop或增加receipt graph |
| clean baseline migration失败 | 不激活Host | 回读旧DB或运行converter |
| legacy test失败 | 判断owner是否同slice删除或补canonical test | skip/xfail或重建compat owner |

## 12. 数量与减法预算

| 指标 | Stage 2 checkpoint | Stage 5 gate |
|---|---:|---:|
| active product relations | 24 | exact 24 |
| committed/live formal events | 26/23 | exact 26/23 |
| subject slots / append guards | 13/2 | exact 13/2 |
| production job handlers | 4 | exact 4 |
| universal EventType | 151 physical legacy | 0 |
| durable stream segment carrier | legacy files仍在 | 0 |
| foreground reducer barrier | legacy files仍在 | 0 |
| Protocol major in production | v3 default、v2 physical | v3 only |
| Oxigraph production/config/test-support | legacy physical | 0 |
| legacy public product relation | empty schema壳 | 0 |
| product LOC | checkpoint baseline | 净删`>=22k`为审查预算 |

净删LOC、await数和文件数是减法信号，不得通过删除typed events、合并无关canonical rows或生成巨型文件取巧。exact vocabulary、authority、effect fence和behavior gates优先。

## 13. 提交与实施顺序

推荐形成三个可审查checkpoint：

1. `Stage 3: remove execution recovery and presentation authority`；
2. `Stage 4: remove projection jobs and Oxigraph`；
3. `Stage 5: retire universal EventLog and install clean baseline`。

项目不会在三者之间发布production版本，但每个checkpoint必须编译、collection无错误、retained gates全绿，便于定位删除回归。不要把Stage 5 migration reset提前混入Stage 3，也不要在Stage 4重构Stage 2 job schema。

每个checkpoint更新一份简短activation/deletion evidence：baseline HEAD、由该HEAD重新生成的删除manifest摘要、retained tests、full-suite disposition、relation/vocabulary/import counts。它是审计artifact，不是运行时authority。

## 14. Definition of Done

Stage 3–5全部完成需同时满足：

1. Stage 2全部canonical、effect、job、memory、live、hook、Protocol v3语义保持；
2. old execution recovery、RuntimeSession、segment/draft/raw-provider、reducer/checkpoint/repair和Presentation Foundation物理删除；
3. close只有真实process-local physical quiescence与canonical interruption，没有derived success drain；
4. projection-job runtime graph与Oxigraph代码/config/worker/CLI/Inspector/tests及active schema contract物理删除；Stage 4 sealed migration-only leaf只为旧链checkpoint存在，并与legacy public table壳、old migrations在Stage 5 clean baseline同一slice消失；
5. job catalog仍exact四类，没有generic或extension handler；
6. universal EventLog、151 grammar、historical execution decoder/replay与legacy factory物理删除；
7. AgentEvent终局exact为26 Committed + 23 Live，Operational独立且不durable；
8. final migration universe固定为`pulsara.conversation-kernel.v1` generation 1、contiguous version 0起步；identity使用唯一无环公式/golden；clean baseline只建立24张`pulsara_v3` product relations和最小migration metadata，采用database-scoped `public.vector >= 0.5.0`且不要求`pgcrypto`，old v13只能得到typed reset-required；
9. verified connection使用binding v2且不依赖runtime-write admission graph；Host writer与job-attempt claim两个domain guard保持独立有效；
10. no import/cold archive/converter/reverse projection/dual authority；
11. clean import、schema、Go/Python、fault matrix与full-suite gates通过；
12. production净删除达到预期方向，未以新receipt/repair/JSON authority换名回归；
13. 下一步可以在不触碰durability架构的情况下进行普通功能开发。

## 15. 停止与升级条件

出现以下任一情况应停止当前stage并请求架构决策：

- 删除需要改变26/23/13/2或24 relations；
- 某legacy handler被声称仍是产品必需，但不在当前四类job catalog；
- 新Kernel仍真实依赖旧owner的semantic state，而不只是中性leaf；
- physical task无法bounded stop/join且仍会访问将释放的resource；
- 需要读取或迁移Stage 2前数据；
- clean baseline无法在empty store重建Stage 2产品能力；
- Stage 4无法在不恢复runtime projection owner的情况下封装old-universe migration-only preparation leaf；
- memory parity要求恢复Oxigraph/SPARQL或扩大two-hop；
- 删除旧test后没有对应canonical behavior coverage；
- 实现者认为需要compat reducer、receipt、checkpoint、repair或第五种job/第三种guard。

这些不是临场实现细节，而是超出本次hard-cut授权的架构变化。
