# Pulsara Fingerprint 与 Activation Hash 大型减法 Hard-Cut 实施规格

> 状态：**ACTIVATED — 2026-08-22**
>
> 记录日期：2026-08-22
>
> 激活证据：[fingerprint_subtraction_hard_cut_activation.json](benchmarks/suites/core/v1/fingerprint_subtraction_hard_cut_activation.json)
>
> 编码基线：实施开始时的 clean `main`。本文不复制 commit、文件、文档或 evidence 的 SHA；Git tree 是代码版本的唯一内容真源。
>
> 本文优先级：本文一经激活，即覆盖所有早期规格中“逐文件代码 SHA、文档 SHA、activation evidence SHA 必须与当前工作树 exact match”的要求；它不覆盖真正的 runtime content integrity、canonical idempotency、provider replay、permission、MCP policy、cursor/ref authenticity 或 strict-prefix 契约。
>
> 调研输入：三个独立的 `gpt-5.6-luna/max` 只读探针分别审计了 activation evidence SHA、DTO fingerprint 的真实消费者，以及 Round 9/9.1、Round 5B、Round 10、continuity 与 settlement 中的重复 proof topology。

---

## 0. 执行结论

本轮执行一次**无迁移期、无双轨、产品行为严格不变**的 fingerprint 大型 hard-cut。

最终只保留以下四类摘要或认证值：

1. **完整内容不随对象一起携带时的内容完整性 digest**；
2. **跨 PostgreSQL transaction、进程重启或 ACK-unknown confirmation 的 durable semantic digest**；
3. **直接参与 provider prefix、source lineage、replay compatibility 或 canonical stable identity 的 semantic digest**；
4. **带密钥的 HMAC/MAC token**。

其余 fingerprint 一律删除，尤其是：

- 完整 frozen DTO 已经在手，却再 hash 一次用于验证同一个 DTO；
- parent DTO 同时保存 child DTO 与 child fingerprint；
- `fact -> snapshot -> registry -> cut -> view -> selection -> plan` 每层重复 hash 同一份语义；
- 同一进程 owner 已持有 exact candidate，却以公开 fingerprint 字符串回查 candidate；
- fingerprint 只在自己的 constructor / `__post_init__` 中被重算，没有独立 verifier；
- fingerprint 只被另一个 aggregate fingerprint读取，没有独立 correctness consumer；
- 无密钥 SHA 被误当作 owner authenticity、permission或不可伪造 capability；
- 固定 build literal、固定 contract literal 或未被消费的 implementation fingerprint。

本轮同时永久结束 activation evidence 的逐文件 hash 维护：

~~~text
不再记录：
  source_documents[*].sha256
  document_sha256
  post_activation_code_sha256
  post_review_code_sha256
  production_code_sha256
  nested code_sha256
  final_modules[*].sha256
  activation evidence 自身 SHA

继续记录：
  status
  日期与工作树语义
  baseline / activation Git reference（仅当它真实存在）
  exact test commands 与结果
  architecture oracle
  real-provider dogfood 事实
  non-goals 与已知边界
~~~

这不是把 SHA-256 换成另一种 hash，也不是把数百个文件 hash 换成一个普通 `tree_fingerprint`。正常仓库版本由 Git commit/tree 拥有；runtime 数据完整性由各自 canonical owner 拥有。

### 0.1 最终拓扑

~~~text
owner lock / safe point
  -> owner-issued exact immutable snapshot
  -> closed pure composition factory
  -> exact typed route / exposure / compiler input
  -> shared cold/append compiler path
  -> continuity owner slot + epoch nonce/revision + exact object
  -> provider execution
  -> one canonical PostgreSQL transaction + existing constraints/events
~~~

摘要值只允许出现在真正的边界上：

~~~text
bytes unavailable at verifier
  -> content digest

durable row / ACK unknown / cross restart
  -> semantic digest or stable canonical identity

untrusted opaque reference
  -> HMAC/MAC token

same-process exact frozen object already present
  -> object identity/equality, owner slot, nonce/revision
  -> no fingerprint
~~~

不得为本轮新增：

- fingerprint registry；
- `fingerprint -> mutable object` map；
- proof graph；
- receipt、checkpoint、repair、replay owner；
- compatibility adapter或legacy fingerprint fallback；
-第二套 capability、continuity、settlement或cold-epoch state machine；
-新的 durable relation、event、job、subject或append guard；
-新的通用 `Fingerprint` 基类、annotation体系或运行时反射框架。

### 0.2 一次切断，而不是阶段迁移

实现可以按依赖顺序编辑文件，但 activation 只有一个原子终局：

~~~text
old code
  fingerprint-rich DTO/API

one hard-cut
  all producers + consumers + tests changed together

new code
  boundary-only digest/MAC
  exact process-local objects
~~~

禁止出现：

- 新旧字段同时存在；
- `fingerprint=None` 兼容期；
- caller可以传 fingerprint，也可以传 exact object；
- 旧 hash 不匹配时 fallback 到字段比较；
- 新 hash 不匹配时 fallback 到旧 hash；
- runtime feature flag；
-两套 event ID、cursor或confirmation算法并行运行。

process-local DTO 不承担跨重启兼容，因此直接 hard-cut。真正持久化、provider-visible、Protocol-visible或canonical ID 派生算法若必须保留输出，则保留**原算法的即时计算**，但不得继续把其结果作为重复 DTO 字段传递。

---

## 1. 为什么现在需要 hard-cut

### 1.1 文件 SHA 是无消费者的维护负担

当前 `benchmarks/suites/core/v1/` 中已有大量 activation report 保存逐文档、逐代码文件和整份报告的 SHA。只读审计确认：

- 这些字段没有通用 verifier；
- pytest 读取 activation JSON 时主要验证 status、oracle、协议与预算，不重算逐文件 SHA；
- CI 没有用它们恢复或证明工作树；
- 很多 evidence 在 dirty worktree 中生成，`captured_head`与逐文件 hash共同构成一份无法由Git自然复现的临时拼图；
-文档或 evidence 自身每次修订都会导致自引用式重算；
- 早期规格把这种手工维护写成 DoD，导致每次 review fix 后重复更新、重复检查和不必要的全量验证。

文件 SHA 没有提升产品 correctness。Git已经保存每个已提交tree；测试、dogfood和oracle才证明行为。

### 1.2 Runtime fingerprint 的问题不是计算成本

当前主要 helper均以SHA-256计算 namespace + canonical JSON，例如：

- `context_fingerprint(...)`；
- `canonical_digest(...)`；
- `sha256_fingerprint(...)`；
- PostgreSQL schema/catalog fingerprint helper。

SHA-256 CPU时间相对provider、PostgreSQL和工具执行并不重要。真正成本是：

- 每个 DTO 多一组 constructor 参数、validator和builder；
- 同一事实跨多层复制；
- 修改一个字段需要同步多个hash payload和contract version；
- reviewer必须判断每层摘要是否覆盖完全相同的语义；
- coding agent容易把“hash相等”误认为“owner签发”“inventory完整”或“physical binding仍有效”；
- tests被迫验证实现形状，而不是产品行为。

### 1.3 Hash不能证明authority

无密钥SHA只能说明“有人对某些bytes算出了相同结果”。它不能证明：

- snapshot由正确owner在自己的lock/safe point内签发；
- MCP inventory穷尽全部registered server；
- Skill scan包含全部configured roots；
- Builtin descriptor有真实execution binding；
- caller拥有permission；
- capability ref没有被伪造；
- process-local candidate仍是owner当前注册的那个对象。

这些事实必须分别由owner-issued对象、private constructor、scope、epoch/generation、slot lease、permission snapshot、HMAC或数据库约束证明。

### 1.4 完整对象已经存在时，摘要是第二份状态

若producer和consumer同时持有完整immutable DTO，那么下面的结构没有增加correctness：

~~~text
FrozenThing(fields..., fingerprint=H(fields...))
consumer receives FrozenThing
consumer recomputes H(FrozenThing.fields)
~~~

它只证明DTO没有违反自己的constructor。frozen DTO equality已经能证明内容相等；object identity/private owner token能证明它是同一次process-local admission；数据库PK/FK/UNIQUE/CHECK能证明canonical关系。

---

## 2. 术语与closed分类

本轮不再把所有摘要都称为`fingerprint`。

| 名称 | 含义 | 典型边界 | 处理 |
|---|---|---|---|
| `content_digest` | immutable bytes的内容摘要 | blob、artifact、Skill body、provider replay body | 保留 |
| `semantic_digest` | 不携带完整payload时的canonical语义摘要 | durable confirmation、source lineage、prefix | 有独立verifier时保留 |
| `compatibility_digest` | adapter/codec/config中真正改变重放或lowering的closed语义 | provider replay、MCP config/policy | 保留 |
| `stable_identity_digest` | 已进入canonical/opaque ID算法的稳定输入 | event/result/entry派生 | 输出保持不变；可即时计算 |
| `MAC` | 带密钥的不可伪造token | cursor、MCP ref、list_agents cursor | 保留 |
| `process-local nonce` | owner签发的一次性opaque identity | permit、reservation | 保留nonce，不hash完整DTO |
| `decorative fingerprint` | 只做self-check或重复child/parent语义 | frozen DTO链 | 删除 |

字段历史上叫`fingerprint`不构成保留理由；字段历史上叫`digest`也不自动安全。最终以独立consumer和失败路径判断。

### 2.1 保留门槛

一个运行时digest字段只有同时满足以下条件才可保留：

1. producer与verifier是两个独立边界，而不是同一constructor；
2. verifier不能直接取得或不应复制完整canonical payload；
3. digest不相等时存在明确、可测试且产品相关的failure；
4. digest覆盖的closed fields已被冻结；
5. 它不是在冒充owner authenticity、permission或physical liveness；
6. 没有已有PK/FK/UNIQUE/CHECK、epoch revision、object identity或HMAC能够更直接地证明同一事实。

任一条件不满足，删除字段。

### 2.2 “被父fingerprint读取”不是独立消费者

以下引用不计入保留依据：

- 自己的builder；
- 自己的`__post_init__`；
- 同一DTO的validator；
- parent aggregate fingerprint；
- repr/log/telemetry；
- test fixture仅断言字段存在；
- `model_dump()`产生但production从未读取；
- 为了排序而先hash、本可直接按closed identity排序；
- 为了映射而用hash、本可直接使用stable ID或exact object。

若child fingerprint只被parent hash读取，parent直接hash child semantic fields；若parent也没有独立consumer，则两者一起删除。

### 2.3 Authority替换规则

process-local authority统一采用：

- owner private constructor；
- exact object identity；
- owner slot中保存的exact frozen value；
-已有nonce、epoch revision、generation或lease；
- typed scope与task/session identity。

不新增一个名为`PreparedCandidateHandle`的通用handle graph。若现有API需要跨一段async调用保留身份，直接携带现有prepared object或现有owner-issued permit object。

---

## 3. 产品与架构不变量

本轮是纯结构减法。以下行为必须byte-for-byte或semantically exact保持：

### 3.1 Conversation / provider

- Chat Completions与Responses的provider request shape不变；
- same epoch的SYSTEM/tools不变，messages只追加suffix；
- provider-native reasoning/replay placement不变；
- cross-process/cross-shutdown thread continuation不变；
- ToolResult FULL/COMPACT/REF_ONLY/OMITTED选择不变；
- 40,000 UTF-8 byte logical FULL边界不变；
- compaction manual/proactive/mid-turn入口不变；
- summary仍由主模型、old exact wire prefix与`tool_choice=none`完成；
- cold epoch assembler与ordinary/compaction/subagent三个consumer的输出不变。

### 3.2 Tool / permission / capability

- attempt-before-effect、ACK unknown与confirmation结果不变；
- permission mode、permission snapshot与local authorize不变；
- Builtin/MCP/Skill registry completeness不变；
- MCP cold DIRECT、late/incompatible META、disconnect gate与same-schema reconnect语义不变；
- inspect/use opaque ref与cursor的HMAC安全不变；
- Skill四root、precedence、complete-or-unavailable、普通`read_file`激活路径不变；
- provider tools顺序、schema、name与description不变。

### 3.3 Memory / TODO / Terminal / Subagent

- advisory memory的candidate/governance/recall/index语义不变；
- TODO snapshot replacement、Live event与compaction handoff不变；
- Terminal process/monitor、artifact与observation完整性不变；
- Round 10 ROOT-only task graph、dependency routing、mailbox、wait/stop与result语义不变；
- subagent real dogfood中的A→B→C、fan-out/fan-in、LAST_N、4 ACTIVE + pending、MCP cold capability保持；
- Protocol v3 wire与未来Web/Desktop client contract不变。

### 3.4 Canonical / durability

- canonical rows与同事务Committed occurrence不变；
- current schema、grant、catalog与deep verification不变；
- event type、subject、guard、relation和job数量不变；
- stable provider-visible、Protocol-visible与canonical opaque IDs不变；
- 任何已有ID若由待删除DTO fingerprint派生，改为在唯一builder中按原payload即时计算，确保输出完全相同；
- 不恢复任何durable execution recovery machinery。

目标oracle保持：

~~~text
Committed events       29
Live events            24
subject slots          11
append guards           1
product relations      25
durable jobs            0
~~~

---

## 4. Activation evidence SHA hard-cut

### 4.1 新 evidence schema

本轮及以后新建或更新的activation report不得包含：

~~~text
source_documents
document_sha256
post_activation_code_sha256
post_review_code_sha256
production_code_sha256
code_sha256
final_modules[*].sha256
activation_report_sha256
evidence_sha256
~~~

不得以这些别名规避：

- `file_digest`；
- `source_fingerprint`；
- `implementation_tree_hash`；
- `report_integrity_hash`；
- 普通Git工作树的aggregate hash。

正常activation report只需要保存：

- evidence schema version；
-状态与激活日期；
-实现开始时的Git状态语义；
- 若存在，真实的baseline/activation commit或tag引用；
- exact test command、node collection和结果；
- PostgreSQL/clean-v0验证；
- architecture oracle；
- real-provider dogfood的provider-neutral事实；
- 未运行项、non-goals与已知限制；
- secret/redaction声明。

不得在dirty worktree中把`captured_head`描述为最终实现快照。可以记录：

~~~text
implementation_base = existing clean commit/tag
worktree_at_evidence = clean | dirty
~~~

最终exact bytes由后续Git commit拥有；evidence不复制Git object database。

### 4.2 历史evidence不回写

已有`benchmarks/suites/core/v1/*.json`视为历史报告：

- 不为本轮批量重算旧hash；
- 不静默删除旧字段；
- 不修改旧报告使其看起来符合新schema；
- README/Gap Index仍可链接历史报告；
- 旧字段只表示当时记录的provenance，不是当前CI gate；
- Stage 3–5 deletion manifest中的historical source digest继续作为历史删除证据，不纳入普通activation规则。

新政策通过新的evidence schema与本文建立，不篡改历史。

### 4.3 旧规格的处理

实施时必须从仍处于active contract的规格中删除或明确废止：

- activation hash必须与当前代码exact match；
-文档hash必须在每次修订后重算；
-跨Round复制整份activation report SHA；
-把逐文件SHA列为DoD。

不必为纯历史、已归档规格重写正文。本文的优先级声明已足够使旧hash gate失效。

### 4.4 测试策略

单纯修改Markdown或activation JSON不要求全量pytest。验证按变化范围执行：

| 变化 | 最低验证 |
|---|---|
| 文档措辞/链接 | fence、heading、link、`git diff --check` |
| evidence内容 | JSON parse/schema、引用命令与oracle一致性 |
| process-local DTO | 对应targeted tests + retained architecture tests |
| repository/schema/provider wire | 对应PostgreSQL/adapter/continuity tests |
| 本轮最终activation | 全量pytest +真实dogfood |

这不降低测试标准；它删除的是由hash字段自我变化引起的无意义重复运行。

---

## 5. 必须保留的runtime摘要

### 5.1 Content integrity

保留并独立验证：

- `InlineContent`、shared blob与artifact正文digest；
- canonical ToolResult/artifact page的content identity；
- Agent Skills exact raw document/body digest；
- Terminal observation/process output digest；
- provider replay private payload/body digest；
- vector/index payload若依赖exact source body的内容digest。

writer在写入时计算，reader在取得bytes后独立重算。这里不能改成DTO equality，因为reader可能只取得row metadata，body可能来自blob/filesystem/private replay relation。

### 5.2 Durable confirmation与idempotency

保留：

- permission snapshot semantic identity；
- provider replay manifest/cut/body digest；
- memory active semantic digest与candidate acceptance digest中真正用于数据库unique/duplicate settlement的部分；
- canonical subagent public result identity；
- inter-agent mailbox canonical message digest；
- repository ACK-unknown confirmation中无法通过stable IDs与逐字段row比较替代的semantic digest；
- schema/catalog/grant fingerprint中被migration/deep verification独立重算的值。

若repository已经通过stable ID定位row并逐字段比较完整prepared candidate，则外层aggregate candidate fingerprint删除。保留row自身真正的semantic/content digest。

### 5.3 Provider prefix、source lineage与replay

保留：

- installed provider semantic prefix digest；
- canonical frontier的有序item semantic identity；
- context source head identity；
- provider replay fragment/payload/placement digest；
- replay-target compatibility digest；
- durable compaction snapshot的source/lineage/summary/manifest digest；
- final provider wire plan中由adapter独立重算的wire prefix/content identity。

但同一个prefix/source/route digest只在拥有它的边界保存一次。不得复制到predecessor、cut、view、selection、plan、permit等每一层。

### 5.4 HMAC/MAC

以下不是普通fingerprint，必须保留：

- MCP inspect/use opaque ref；
- MCP directory cursor；
- subagent list cursor；
- 任何permission/config secret-generation commitment；
-未来真正需要不可伪造的Web/Desktop cursor。

token payload、key generation、scope、expiry和closed fields保持现有契约。不得用普通SHA替换HMAC。

### 5.5 Stable identity derivation

若当前event ID、result ID、candidate ID或opaque canonical ID包含SHA派生结果，本轮保持生成结果不变。

允许的减法是：

~~~text
before:
  dto.candidate_fingerprint = H(dto fields)
  event_id = H(dto.candidate_fingerprint, task_id, event_type)

after:
  event_id = existing_stable_event_id_builder(
      H_same_payload(dto fields),
      task_id,
      event_type,
  )
~~~

即时局部计算不是第二份状态。不得为了删除字段而改变已接受row、event或Protocol所使用的opaque ID。

---

## 6. Capability（Round 9 / 9.1）减法

### 6.1 保留的真实事实

保留：

- sealed Builtin execution-backed inventory；
- MCP per-server COMPLETE/UNAVAILABLE snapshot及physical generation/slot/lease；
- 聚合LOCAL_SKILL_CATALOG snapshot与四root provenance；
- canonical capability identity；
- Tool semantic version/schema/policy identity；
- Skill catalog、activation body与winning-root provenance语义；
- exact native canonical→wire projection；
- installed DIRECT cohort；
- MCP tool-specific route与policy-bound ref；
- final adapter wire bytes proof。

### 6.2 删除的重复层

必须删除或改为即时derived value：

- `ToolActionClassifierBinding.implementation_build_fingerprint`；
- `FrozenToolCapabilityExposureSelection.selection_fingerprint`；
- `McpInstallationCandidate.candidate_fingerprint`字段；
- `PreparedLocalSkillCatalogSourceSnapshot.root_policy_fingerprint`副本；
- inspection input中同时携带完整source/fact和它们fingerprint的重复字段；
- registration set、source snapshot、registry、dispatch sibling view、selection和final plan之间重复复制的aggregate fingerprint；
-同一native projection tuple在predecessor/cut/plan中的多份摘要；
-只用于“证明调用方没改过自己刚构造DTO”的semantic fingerprint。

### 6.3 最终API形状

~~~text
SealedBuiltinCapabilitySnapshot ┐
McpCapabilitySnapshotSet        ├─> FrozenCapabilityRegistrySnapshot
LocalSkillCatalogSnapshot       ┘            │
                                              ▼
                                   FrozenCapabilityDispatchCut
                                      ├─ exact tool view
                                      └─ exact skill view
                                              │
                               planner/composer receive exact views
                                              │
                                   exact exposure + wire projection
~~~

parent cut、tool view和skill view通过exact frozen object组合，不通过caller传入三个可任意拼接的fingerprint字符串。

owner authenticity继续由private constructor/issuer证明；inventory completeness继续由owner lock下的snapshot证明；physical MCP调用继续由generation、slot、lease和policy exact join证明。

### 6.4 不得改变的行为

- catalog successor不触发same-epoch rebase；
- native tools只在cold epoch冻结；
- late MCP走META；
- installed DIRECT disconnect返回typed unavailable，不偷偷走META；
- runtime-only reconnect不发布模型可见catalog变化；
- Skill没有executor或permission authority；
- Skill catalog/body仍是UNTRUSTED_OBSERVATION。

---

## 7. Model input / continuity / provider execution减法

### 7.1 删除public candidate fingerprint路由

当前continuity owner通过`candidate_fingerprint`查找prepared slot，并在permit中重复保存candidate/execution fingerprint。hard-cut后：

1. `register(...)`把exact `PreparedProviderInputAppendCandidate`保存到exact scope slot；
2. caller继续携带同一个prepared candidate对象；
3. `install(...)`接收exact candidate和exact prepared execution对象；
4. owner要求slot中的对象identity与caller对象一致，并重验scope、epoch revision、compatibility和full frozen value；
5. install permit只保存现有scope、epoch nonce/revision与owner-issued permit nonce；
6. provider open消费exact permit对象，不再比较candidate/execution fingerprint；
7. `discard`、`require_registered_plan`等API同样接收exact candidate或exact scope slot，不以hash全Host搜索；
8. owner close清理slot/permit，行为保持现状。

不得新建global candidate handle registry。slot本身已经是唯一owner。

### 7.2 继续保留的continuity证明

以下仍然是独立语义，不因candidate字段删除而消失：

- scope；
- epoch nonce/revision；
- compatibility与reset reason；
- system/tools/messages exact equality；
- direct native projection exact equality；
- semantic message prefix；
- actual provider wire prefix；
- canonical frontier prefix；
- source heads；
- logical/physical byte bounds；
- assistant replay reservation与fragment integrity。

compatible append仍必须逐项比较旧、新system/tools/messages/wire prefix。hash从来不能替代这些现有exact checks。

### 7.3 Wire与replay

`FrozenProviderWireInputPlan`若完整materialization与adapter proof已被同一次dispatch携带，删除中间DTO对其fingerprint的重复引用；adapter独立重算并验证的wire prefix/content identity保留一次。

Round 5A.2 durable replay继续保持：

- manifest先读；
- selected hydration后读body；
- exact session/scope/cut/placement/target compatibility；
- body/content digest独立重算；
- aggregate bounds；
- Chat/Responses closed item contract。

不得因删除process-local request fingerprint而削弱durable replay完整性。

---

## 8. Repository / settlement减法

### 8.1 Assistant settlement

`PreparedAssistantMessageSettlement`不再保存把自身全部字段重hash一次的candidate fingerprint。

owner以：

- canonical `entry_id`；
- exact prepared candidate对象；
- exact writer/turn/scope guard；
- repository FULL/NONE/CONFLICT逐字段confirmation；

完成settlement。

若同值不同对象必须被区分，使用owner已有attempt slot/nonce，不新增hash。caller cancellation仍只detach，shielded settlement行为不变。

### 8.2 Tool/TODO/Terminal process-local settlement

普通SHA token fingerprint不是安全token。对只存在于同一Host的settlement：

- 携带exact prepared token/candidate对象；
- owner按slot、attempt ID、result entry ID和object identity匹配；
- missing/conflict继续是invariant failure；
- canonical result FULL之后的INSTALLED/DISCARDED语义保持；
- 不改变attempt-before-effect；
- 不改变Terminal monitor/process lifecycle。

保留真正的canonical content digest、permission snapshot digest和remote/config identity。

### 8.3 ACK unknown

不得简单删除ACK-unknown需要的digest。逐个repository API应用下列规则：

~~~text
stable canonical IDs + complete prepared fields + stateless row comparison available
  -> remove outer candidate fingerprint

verifier only has digest because body is private/blob-backed/not hydrated
  -> retain durable semantic/content digest
~~~

confirmation结果继续严格为`FULL | NONE | CONFLICT`或该domain现有closed vocabulary，不新增“hash unavailable”状态。

---

## 9. Cold epoch / compaction减法

### 9.1 保留

- durable snapshot source/summary/manifest/lineage digest；
- protected-tail selection中真正跨summary/adoption异步边界的proof；
- resolved hard-bound contract identity；
- old exact wire prefix与selected provider replay hydration；
- active/idle adoption的canonical fields；
- retained Skill exact body digest；
- `KernelColdEpochInputAssembler`唯一实现。

### 9.2 删除

- `SubagentInitialSeed.objective_item_fingerprint`；
- `SubagentInitialSeed.seed_fingerprint`；
- `SelectedDurableReplayHydrationRequest.source_dispatch_read_fingerprint`；
- `SelectedDurableReplayHydrationRequest.request_fingerprint`；
- `RecentHumanMessageProof.item_fingerprint`；
- `CompatibleAppendCompactionProjection.predecessor_epoch_semantic_prefix_fingerprint`重复副本；
- retained-skill/dependency item仅作为父hash输入的child fingerprint；
- cold assembly result上的任何新增aggregate fingerprint。

`SubagentInitialSeed`继续通过private constructor和exact equality验证objective、parent selection、dependency context与dispatch scope。

### 9.3 Compaction行为不变

- summary只负责语义交接；
- Runtime精确重建Terminal/TODO/subagent/capability/memory/permission状态；
- retained three tool groups继续复用normal ToolResult projection；
- retained Skill只来自已FULL安装的ordinary `read_file`；
- successor重新走Round 9/9.1 standard EMPTY cold path；
- 无provider-error reactive compaction；
- 无replacement-history graph或durable compaction job。

---

## 10. Round 10 Subagent减法

### 10.1 必须保留

- `task_id`、`batch_id`、dependency rows和stable event/entry IDs；
- canonical `FrozenSubagentResultPublicFact.result_fingerprint`或等价durable result semantic digest；
- canonical inter-agent message digest；
- ROOT/child exact scope；
- actual parent model call的exact subject；
- objective、NONE/LAST_N parent context与direct dependency result；
- repository row-by-rowFULL/NONE/CONFLICT confirmation；
- list cursor HMAC。

### 10.2 删除

至少删除以下process-local重复字段：

- `eligible_context_units_fingerprint`；
- parent call subject aggregate fingerprint；
- parent context selection fingerprint；
- parent context source fingerprint副本；
- dependency result item fingerprint；
- dependency context aggregate fingerprint；
- task draft candidate fingerprint；
- batch candidate fingerprint字段；
- explicit-result settlement candidate fingerprint；
- mailbox item fingerprint；
- mailbox batch candidate fingerprint。

若这些值参与现有stable event ID生成，唯一event ID builder继续按原canonical semantic payload即时计算相同摘要，DTO不保存该值。

### 10.3 最终数据流

~~~text
exact parent model-call subject
  -> exact immutable task batch
  -> canonical task/dependency/event transaction
  -> exact SubagentInitialSeed
  -> child cold epoch
  -> canonical SubagentResult
  -> direct downstream dependency projection
~~~

每层传完整typed value或stable canonical ID，不传一串由上一层自己生成的public fingerprint。

以下产品行为保持：

- ROOT-only flat topology；
- worker不可递归spawn；
- NONE默认、LAST_N最多三个ROOT conversational groups；
- dependency result只给direct downstream；
- explicit/inferred result统一；
- mailbox只用于ROOT到active worker临时指导；
- compaction task-board handoff；
- child启动时重新构造当前capability cut。

---

## 11. TODO、Memory与其他domain减法

### 11.1 TODO

删除`FrozenTodoItem.item_fingerprint`。TODO item完整携带`text/status`，aggregate snapshot从ordered semantic fields直接构造。

若TODO snapshot digest被Live projection、settlement或client resync独立消费，则保留snapshot级digest一次。process-local admission/settlement使用exact candidate/token对象与run/revision，不hash完整对象。

### 11.2 Memory

Memory不是本轮功能改造。必须保留：

- active semantic duplicate digest；
- candidate acceptance/settlement中数据库真正消费的digest；
- vector embedding输入body identity；
- provider ToolResult citation的canonical identity或MAC；
- canonical fact/relation/result stable IDs。

必须删除：

- governance/reflection batch只做self-check的aggregate fingerprint；
- `PreparedCheapHintReflectionCandidateBatch.batch_fingerprint`；
-完整candidate/fact/relation DTO已随调用携带时的重复wrapper fingerprint；
-同进程governor/recall projection之间只用于再次确认同一个frozen value的hash。

Memory仍是advisory data；本轮不得改变recall分数、governance决策、taxonomy、scope、preference head或weak-completeness语义。

### 11.3 Terminal / artifact / blob

保留：

- content/blob/artifact digest；
- canonical observation/result identity；
- process/monitor canonical IDs；
- artifact page的UTF-8 offset与body digest。

process-local token fingerprint若只用于owner内部匹配，改成exact token/nonce。不得降低terminal process、monitor或artifact的物理bounds。

### 11.4 Model failure / diagnostics

component fingerprint若只进入父级failure/summary fingerprint，父级直接hashclosed semantic fields；删除child字段。

真正进入canonical event identity、dedup或provider retry classification的最终failure identity保持输出不变。不得改变retryable/non-retryable判定、terminal reason或用户可见错误。

---

## 12. Mandatory deletion inventory

下列是三个审计共同确认的**最低删除集合**，不是允许实现者止步的完整清单：

| 文件/区域 | 必须删除或内联的字段 |
|---|---|
| `capability/tool_action.py` | `implementation_build_fingerprint` |
| `capability/contracts.py` | exposure selection self-only fingerprint |
| `conversation_kernel/mcp/contracts.py` | installation candidate stored fingerprint |
| `conversation_kernel/capability_composition.py` | duplicate root-policy/source/fact fingerprints when exact objects coexist |
| `conversation_kernel/cold_epoch.py` | subagent seed objective/aggregate fingerprints；selected hydration request self fingerprints |
| `conversation_kernel/compaction/contracts.py` | duplicate predecessor prefix；recent-human item fingerprint |
| `tools/builtins/todo.py` | TODO item fingerprint |
| `conversation_kernel/memory/reflection.py` | Cheap Hint candidate batch fingerprint |
| `conversation_kernel/compaction/runtime_handoff.py` | subagent task-board handoff fact self fingerprint |
| `conversation_kernel/subagents/contracts.py` | subject/selection/dependency/mailbox/task-batch/result-settlement process-local aggregate fingerprints |
| `conversation_kernel/assistant_settlement.py` | aggregate assistant settlement candidate fingerprint |
| `conversation_kernel/input_continuity.py` | public candidate/execution fingerprint lookup与permit复制 |

实施者必须再执行一次AST/reference审计，将所有字段分成：

~~~text
KEEP_CONTENT_INTEGRITY
KEEP_DURABLE_SEMANTIC
KEEP_PREFIX_OR_REPLAY
KEEP_MAC
KEEP_STABLE_ID_DERIVATION_LOCAL_ONLY
DELETE_SELF_ONLY
DELETE_DUPLICATE_CHILD_PARENT
DELETE_PROCESS_LOCAL_AUTHORITY_HASH
~~~

这份inventory是一次性实施输入，不作为新的permanent registry、运行时annotation或activation hash清单提交。

---

## 13. 机械实施规则

### 13.1 删除顺序只是编辑顺序

建议在一个working tree中按以下顺序修改，但不形成可运行的中间兼容版本：

1. 删除activation hash政策与新evidence字段；
2. 删除self-only leaf fingerprint；
3. 让parent直接消费semantic fields，再删除child fingerprint；
4. capability cut/view/plan改为exact frozen object；
5. continuity与settlement改为owner slot + exact object/permit；
6. 清理subagent、cold epoch、TODO、memory、MCP wrapper；
7. 保持stable ID算法并删除DTO中的中间存储；
8. 一次性更新全部tests/docs/evidence；
9. 最终全量验证。

任何步骤完成后都不提交“兼容态”。最终commit只能包含new topology。

### 13.2 Exact object的使用

同进程API优先直接接受closed frozen DTO：

~~~python
install(candidate: PreparedProviderInputAppendCandidate, execution: PreparedExecution)
settle(candidate: PreparedAssistantMessageSettlement)
plan(view: FrozenToolCapabilityDispatchView)
compose(view: FrozenSkillCapabilityDispatchView)
~~~

owner必须验证：

- exact scope；
- exact slot/epoch/revision；
- private issuer或object identity；
- closed DTO equality；
- bounds与lifecycle。

不得把“可构造一个字段相同的DTO”误认为“拥有owner authority”。authority来自owner slot/private object，equality只证明content。

### 13.3 Derived property不是默认逃生口

不能把所有待删除字段简单改成`@property fingerprint`后宣称完成hard-cut。

只在以下情况允许即时derived helper：

- 必须保持既有stable external/canonical ID字节；
- verifier不保存该property，只在唯一builder中使用；
- helper直接接受semantic fields；
- 不重新出现在DTO schema、repr、serialization或跨层API。

其它self-only fingerprint连计算一起删除。

### 13.4 Contract/version处理

- process-local DTO shape可直接bump内部contract ID；
- provider message/tool/wire bytes不变，因此不得伪造provider contract变化或强制新epoch；
- Protocol v3不变；
- PostgreSQL schema不变，除非审计发现一个纯冗余、无任何durable consumer的列；默认不得为追求数字删除durable列；
- retained digest算法与namespace保持，避免stable IDs或历史row不兼容；
- 不新增migration compatibility链；clean-v0不得因本轮被无故reset。

---

## 14. Failure matrix

| 场景 | 正确结果 |
|---|---|
| 完整DTO已在手，caller传入结构相同但不是owner当前slot对象 | typed conflict；不得因字段相等获得authority |
| continuity candidate在install前被新candidate替换 | epoch/slot/object identity冲突；provider open为0 |
| ACK unknown后canonical row已提交 | 通过stable ID与逐字段confirmation返回FULL，不依赖外层self fingerprint |
| blob/replay/artifact body被篡改 | content digest校验失败；hard fail |
| MCP ref/cursor被修改 | HMAC失败；不得降级为普通hash比较 |
| capability owner snapshot遗漏source | owner completeness/factory失败；不得靠registry hash自证 |
| stable event ID原先依赖candidate fingerprint | 按原semantic payload即时计算，ID保持byte-identical |
| historical activation JSON含旧hash | 正常保留；不作为current gate |
| 新activation report包含逐文件hash | activation失败 |
| 单纯文档修改 | 不触发全量pytest；只跑文档/evidence验证 |
| 删除digest导致provider wire变化 | activation失败 |
| 删除digest导致Protocol/canonical ID变化 | activation失败 |
| 实现新增fingerprint registry/handle map | architecture failure |

---

## 15. 测试与验收

### 15.1 Static audit

实施前后执行一次性AST/引用探针，覆盖：

- dataclass/Pydantic字段名以`fingerprint/digest/hash`结尾；
- SHA/HMAC helper调用；
- SQL columns/parameters；
- event/entry/result ID builders；
- `model_dump()`/wire serialization；
- mapping/set key；
- repr/log/telemetry；
- constructor-only、self-validator-only和parent-hash-only引用。

最终不能以“`rg`数量下降”作为correctness证明。数量只用于发现遗漏。

### 15.2 Targeted tests

至少覆盖：

- capability owner completeness、scope、DIRECT/META routing与stale ref；
- continuity register/install/consume/discard、stale candidate和prefix rewrite拒绝；
- Chat/Responses exact wire plan；
- durable provider replay metadata/body hydration与corruption；
- assistant/tool/TODO/Terminal ACK unknown/cancellation settlement；
- memory duplicate/governance/citation/index；
- compaction active/idle/repeated/mid-turn/retained Skill；
- subagent batch FULL/NONE/CONFLICT、DAG、mailbox、dependency result与cursor；
- blob/artifact/ToolResult content integrity；
- permission snapshot与attempt-before-effect。

### 15.3 Byte/row identity tests

对hard-cut前后同一fixture证明：

- provider SYSTEM/tools/messages/materialized wire相等；
- ToolResult projection相等；
- canonical row字段相等；
- event/entry/result/attempt opaque IDs相等；
- Protocol v3 payload相等；
- MCP/Skill catalog renderer相等；
- compaction summary input与successor input相等；
- subagent initial seed的provider-visible内容相等。

process-local DTO repr、constructor signature和内部fingerprint值不属于兼容面。

### 15.4 Full validation

最终activation必须运行：

- full pytest；
-全部PostgreSQL marker tests；
- clean-v0 fresh/repeat/deep verify；
- Ruff；
- compileall；
- Protocol generator/check；
- dependency lock check；
- architecture/doc/link/fence/heading/secret/diff checks。

### 15.5 Real dogfood

至少运行：

1. ordinary Chat model/tool loop；
2. ordinary Responses model/tool loop；
3. cross-restart provider replay continuation；
4. memory remember→governance→recall；
5. MCP DIRECT与late META inspect/use；
6. Skill catalog→ordinary `read_file`→supporting resource；
7. mid-turn compaction后继续完成；
8. `tools/run_round10_subagent_dogfood.py`完整长程测试。

dogfood不得记录API key、DSN、prompt正文、private reasoning、memory正文、vector或远端response payload。

---

## 16. Activation evidence

本轮activation report自身必须成为新政策的第一个实例。它不得保存任何文件、文档或report SHA。

建议形状：

~~~json
{
  "schema_version": "fingerprint-subtraction-activation-v1",
  "status": "ACTIVATED",
  "implementation_base": "clean-git-reference-if-available",
  "worktree_at_evidence": "clean-or-dirty",
  "product_behavior": "UNCHANGED",
  "removed_runtime_fingerprint_classes": [
    "self_only",
    "duplicate_child_parent",
    "process_local_authority_hash"
  ],
  "retained_digest_classes": [
    "content_integrity",
    "durable_semantic",
    "prefix_or_replay",
    "mac",
    "stable_id_local_derivation"
  ],
  "validation": {},
  "oracle": {
    "committed_events": 29,
    "live_events": 24,
    "subject_slots": 11,
    "append_guards": 1,
    "product_relations": 25,
    "durable_jobs": 0
  },
  "non_goals": []
}
~~~

`implementation_base`是Git引用，不是SHA-256文件清单。若evidence生成时最终activation commit尚不存在，应诚实记录baseline与dirty状态；合入后Git历史自然拥有最终tree。

---

## 17. Definition of Done

只有同时满足以下条件，本轮才可标记`ACTIVATED`：

1. Mandatory deletion inventory全部完成；
2. 全仓库fingerprint/digest/hash AST审计完成，所有剩余字段都通过§2.1保留门槛；
3. 没有self-validator-only、parent-hash-only或完整对象旁的重复fingerprint字段；
4. capability registry/cut/view/plan使用exact frozen object，不靠public hash拼接authority；
5. continuity/settlement不再以candidate/execution SHA在同进程回查对象；
6. content、replay、permission、durable confirmation、prefix/source lineage与HMAC边界没有削弱；
7. provider wire、Protocol、canonical rows与stable IDs保持不变；
8. Round 8 memory、Round 9/9.1 capability、Round 5B compaction与Round 10 subagent产品happy path全部保持；
9. oracle仍为`29 / 24 / 11 / 1 / 25 / 0`；
10. 没有新增relation、event、job、subject、guard、receipt、checkpoint、repair、replay owner或fingerprint registry；
11. 新activation evidence不含逐文件/文档/report SHA；
12. 历史evidence未被回写；
13. full pytest、PostgreSQL、clean-v0、static checks与real dogfood全部通过；
14. `uv run python tools/run_round10_subagent_dogfood.py`通过；
15. 没有新增skip/xfail或以放宽断言维持绿色。

---

## 18. 明确non-goals

本轮不：

- 更换SHA-256算法；
- 设计通用Merkle tree；
- 对Git tree再造仓库内hash系统；
- 修改provider cache策略；
- 修改canonical schema或历史data migration；
- 改写memory taxonomy/recall/governance；
- 改写MCP direct/meta产品策略；
- 改写Skill标准或root policy；
- 改写compaction输入、summary prompt或retained tail；
- 改写subagent task graph、permission或mailbox语义；
- 引入plugin/hook、Web/Desktop UI或新Protocol；
- 清理与fingerprint无关的普通DTO；
- 为历史activation evidence补commit或重算hash；
- 把“代码行数减少”作为成功标准。

本轮唯一目标是：

> 在产品功能、provider wire、canonical truth、Protocol与durability边界严格不变的前提下，删除所有不能跨真实边界提供独立证明的fingerprint和activation文件hash维护。
