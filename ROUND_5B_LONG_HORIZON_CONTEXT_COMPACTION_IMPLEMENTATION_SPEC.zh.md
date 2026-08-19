# Pulsara Round 5B：Long-horizon Context Compaction 与 Successor Capability Rebase 实施规格

> 状态：**DRAFT — NOT ACTIVATED**
>
> 记录日期：2026-08-17
>
> 当前代码基线：ffd0d146f8d7991ff3d1e92dc9ca75e8abf894e8（feat: implement Round 5A.1 output termination）
>
> 上位架构：[PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)
>
> 产品能力索引：[POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 前置实现：[Round 3 compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 5A.1 provider-neutral output termination](ROUND_5A_1_PROVIDER_NEUTRAL_MODEL_OUTPUT_TERMINATION_IMPLEMENTATION_SPEC.zh.md)、[Round 5A.2 durable provider replay/cross-restart continuation](ROUND_5A_2_DURABLE_PROVIDER_REPLAY_AND_CROSS_RESTART_THREAD_CONTINUATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7 model-visible outcome/timing](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7.1 provider-visible ToolResult projection](ROUND_7_1_PROVIDER_VISIBLE_TOOL_RESULT_PROJECTION_IMPLEMENTATION_SPEC.zh.md)、[Round 8 advisory memory](ROUND_8_ADVISORY_MEMORY_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md)、[Round 9 unified capability semantics](ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md)、[Round 9.1 Agent Skills](ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md)、[Lightweight TODO refinement](PULSARA_LIGHTWEIGHT_TODO_TOOL_REFINEMENT_IMPLEMENTATION_SPEC.zh.md)
>
> hard-cut前参考基线：5b7ad9f7ffc8565bc572180b2bde0c81ab64473a
>
> 规范归属修订（2026-08-17）：正常epoch MCP direct/meta、`MCP_CATALOG`、`list_mcp_servers`、`inspect_new_mcp_tool`、`use_new_mcp_tool`与direct unavailable gate全部由Round 9唯一拥有；普通ToolResult的40,000-byte logical FULL、8-KiB COMPACT、artifact与conditional guidance全部由Round 7.1唯一拥有。Round 5B只在合法rebase boundary重新消费Round 9/9.1的current source registrations、complete source snapshots、planning cut与普通compiler结果，并复用Round 7.1既有ToolResult variants；它不重新发现能力、不实现meta gateway、不定义第二套MCP exposure DTO，也不定义compaction专用ToolResult阈值。
>
> Skill收口修订（2026-08-18）：Round 9.1不再定义`read_file` activation intent/lookup。Round 5B只为同一真实user run中已经ordinary COMPLETE + actual FULL + continuity-installed、且current manifest未变化的Skill纯派生bounded `RETAINED_SKILL_CONTEXT`；不建立loaded-state、receipt或跨turn activation history。
>
> Provider replay收口修订（2026-08-19）：Round 5A.2已经ACTIVATED；Round 5B不再假设Host loss以后只能从generic public semantics重建summary prefix。Round 5A.2负责把completed、entry-bound Chat/Responses native carrier与assistant同事务持久化；summary call只消费其现有`FrozenCanonicalProviderDispatchRead`、selected hydration与`FrozenProviderWireInputPlan`接口，可使用当前Host安装或restart后rehydrate的exact old-prefix replay。adoption以后，snapshot floor以前的replay row不再active materialize，但Round 5B不删除row、不把hidden carrier复制进summary，也不建立第二套replay DTO。
>
> 本文现在只实施Round 5B context compaction：可靠且在successor epoch boundary完整就绪的MCP cohort可依Round 9规则重新冻结；compaction adoption因此是已有NEW MCP再次尝试进入下一epoch direct surface的自然边界。本文不实施新的memory extraction、replacement-history replay、provider context-error reactive retry、durable compaction job或hierarchical subagent graph。

---

## 0. 执行结论

Round 5A已经让一个健康turn不再因为固定model/tool-call次数或turn-wide wall-clock deadline提前死亡；Round 3与3.1已经让同一Host、同一scope、同一continuity epoch满足：

~~~text
SYSTEM[n + 1]   == SYSTEM[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
~~~

Round 9已经先行关闭与该不变量相关的正常capability缺口：fixed Builtin与cold DIRECT MCP组成epoch-stable native `tools[]`；late-ready MCP只追加catalog并走inspect/use meta path；direct断连或schema replacement不热改tools。Round 9.1又把dynamic Skill catalog、textual/configured `ACTIVE_SKILL`与ordinary `read_file` progressive disclosure接入同一append-only dispatch planning。本轮不重复实现这些能力。

Round 5B只增加一个合法的**successor boundary**：active compaction已经明确关闭旧epoch并建立新epoch，因此可以用Round 9的current complete registry/planning cut重新选择下一epoch native MCP cohort，并用Round 9.1普通compiler重新安装current Skill catalog/active source。对同一真实user run中已经通过ordinary `read_file`完整FULL交付的Skill，本轮另以低authority `RETAINED_SKILL_CONTEXT`有界重注入；它不是activation lookup或loaded-state。这个promotion/rebase结果仍是Round 9标准`FrozenCapabilityExposurePlan`，不是Round 5B私有MCP generation。

另一个剩余缺口是：当这条append-only epoch本身接近当前模型的active input budget时，Runtime没有合法的换代路径。Round 5B冻结唯一例外：

> compaction在provider safe point把旧epoch的一个完整前缀交给当前主模型生成语义交接；Runtime采用交接后，显式关闭旧epoch并从canonical snapshot、受保护tail与当前Runtime事实冷编译一个新epoch。

本轮最重要的职责分工是：

| 内容 | 唯一owner |
|---|---|
| 早期历史的语义压缩、当前工作与下一步 | 当前主模型的compaction summary |
| 哪段历史被覆盖、哪些tool group继续原样保留 | Runtime planner |
| 最近真实用户原话 | canonical transcript + Runtime selection |
| 当前SYSTEM、tools、permission、Plan、MCP、skill、memory与timing | 当前各authority + Round 3 compiler |
| 当前epoch的direct/new MCP分类与new-tool调用 | epoch direct surface + current MCP supervisor/catalog |
| 仍在运行的Terminal process/monitor、TODO与当前flat subagent | 当前Host process-local owners |
| ToolResult正文、artifact handle与provider投影 | canonical ToolResult + Round 1/3 lowering |
| 当前使用哪一个summary | turn.current_context_binding_revision_id指向的exact snapshot |
| 完整历史 | canonical transcript；永不被compaction删除或重写 |

因此，summarizer只负责语义，不负责重建精确Runtime状态。它不得承担：

- 枚举所有artifact ID、Terminal ID、monitor ID或MCP tool；
- 猜测当前permission、Plan revision、skill、memory head或tool surface；
- 把compaction期间尚未canonical接纳的用户输入写入summary；
- 生成provider replacement history；
- 提交memory candidate；
- 调用任何physical tool。

Round 5B只允许三个入口：

1. 用户显式请求manual compaction；
2. active compiled input达到proactive auto threshold；
3. single-turn agent loop在完整tool batch之后、下一次follow-up model call之前达到mid-turn threshold。

本轮明确不支持：provider在尚未接受输出前返回context-length错误后，再reactive compact并重试。Provider error仍按现有typed provider failure处理；compaction trigger不读取远端错误字符串，也不依赖某一家provider的错误码。

成功路径如下：

~~~text
freeze safe point and exact prospective normal-dispatch view
-> choose protected tail first
-> derive exact summary prefix and source cut
-> append one synthetic summarization user message
-> call the current primary model with the same SYSTEM and tool definitions
-> validate one semantic summary
-> active: freeze current Runtime facts and successor READY_CLEAN MCP direct surface
   idle: validate bounded snapshot/post-cut base only
-> active: dry-compile proposed snapshot + canonical tail + current sources
-> atomically insert snapshot/revision/event and advance exact binding pointer
-> close old continuity epoch
-> exact-read and install the new cold epoch
-> continue the same canonical turn, or return from an idle manual compact
~~~

任何summary、planning、active dry-compile或idle base validation失败都保留旧binding与旧epoch。只有active canonical adoption FULL之后，Runtime才允许successor epoch成为当前model-input authority；idle FULL只使snapshot成为下一turn的canonical cold base。

---

## 1. 起草输入与prior art判断

### 1.1 当前文档hash

~~~text
PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md
cb3e7b0a9f33e5e4c5b17850d47e1af580a3f23f094f868076351bb17a6a6e80

POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md
8f8f8e4606a51b9003090ce260ebfd68815fde7003f5b8db3c0ca08f82ffc851

ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md
1a996f8dda8c767043e4c84bf7d414724129dbd3d890d5cf3bb5463922cae6e6

ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md
9ee6cfca09869a67903a2164c2c2025d7c836998bd26a459336cee90658e34c2

ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md
608ecfdd8e4f20acc62c012fb39569c19c4a34f6bf981d8c96df0aa293f48832

ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md
1dd729efa14cb31261da11b07b4006d954cf0ce52c115aa6cf4bb5ed50554e52

ROUND_8_ADVISORY_MEMORY_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md
e1e64645671105bd409a329f317496d92d57db69177a7c454ba062d1cafc3803
~~~

这些hash只标识起草输入。coding agent必须在第一个production diff前重新记录实际checkpoint HEAD、本文hash与Gap Index hash，并保留并行用户修改。

### 1.2 当前Pulsara已有基础

当前clean-v0 schema已经拥有：

- context_snapshots；
- turn_context_binding_revisions；
- turns.current_context_binding_revision_id；
- CompactionAdopted committed event；
- ConversationKernelRepository.adopt_context_snapshot()；
- reader对SNAPSHOT base的materialization。

这些基础表达了正确的最小canonical关系：

~~~text
turn
  -> current binding revision
  -> one immutable context snapshot
  -> source cut in the complete canonical transcript

provider input
  = snapshot handoff
  + exact-scope canonical entries after source cut
  + current compiler sources
~~~

但当前路径仍是dormant：Host/runner不生成或采用snapshot；adopt_context_snapshot()还错误要求source cut早于当前turn initial entry，因而无法single-turn mid-turn compact；每个新turn又总是创建FULL_HISTORY revision-0，没有继承前一个exact-scope snapshot。

当前还保留最后一套durable job machinery，唯一handler是BACKGROUND_COMPACTION。它与本轮前台主模型compaction语义冲突，且没有production caller。Round 5B必须删除它，而不是把新产品路径接回job executor。

### 1.3 hard-cut前Pulsara：吸收产品语义，不吸收恢复图

hard-cut前值得保留：

- manual、threshold-driven与mid-turn safe-point三个入口；
- 80%附近trigger、55% post target这类active-context分层；
- summary只覆盖完整prefix，current unfinished tail原样保留；
- previous summary参与后续summary；
- private analysis stripping、summary hard bound与source-stale检查；
- tool-call/tool-result pairing不能被拆开；
- artifact-aware ToolResult降级；
- summary失败不得破坏正在执行的run。

禁止恢复：

- ContextCompactionStarted/Failed/Completed事件链；
- EventLog replay、window reducer、projection generation、checkpoint、repair与publication receipt；
- durable rollout account、finalization account与child reservation；
- compaction memory extraction double call；
- background summarizer job；
- 用累计token、cache usage或model-call次数代替active context。

### 1.4 Codex：replacement history是其rollout格式需要，不是Pulsara需要

当前Codex把CompactedItem与replacement_history写进rollout，并在resume时优先按replacement_history恢复provider history。这解决的是Codex JSONL rollout自身的重建问题。

Pulsara不复制该设计。理由是：

1. Pulsara已有完整canonical relational transcript；
2. snapshot + source cut + exact-scope post-cut rows可确定性重建provider-neutral input；
3. 持久化完整provider messages会复制tool grouping、source placement与adapter lowering真值；
4. provider adapter或compiler升级后，旧replacement history会成为第二套历史wire authority；
5. 它会重新引入**完整compiled provider-input/replacement history**与跨Host generation承诺。Round 5A.2新增的entry-bound private native carrier不复制SYSTEM、tools、Runtime observations或完整compiled request，因此不构成本条所拒绝的replacement history。

本轮因此不增加replacement_history、retained_group_manifest列或provider message blob。受保护group identity只存在于process-local `CompactionSettlementResources`，用于采用前验证；采用后，source cut和canonical rows就是唯一truth。

Codex当前另有一个可借鉴但与replacement history正交的普通工具输出边界：`codex-rs/models-manager/models.json`为最新模型冻结`truncation_policy = 10,000 tokens`，由统一tool/context lowering应用，而不是在compaction时另设更小阈值。按其近似换算，这一档约为40 KB model-visible text。

### 1.5 grok-build：吸收summary call与Runtime handoff分层

本地grok-build当前证明了两条很有价值的路径：

- summary call在旧history后追加一个synthetic user message，并保持原SYSTEM与tool definitions；支持时使用tool_choice none，以复用大段prefix cache；
- compacted history由Runtime重新注入project instructions、last real user query、summary与当前active state，而不是要求summary模型记住所有动态事实。

本文吸收这两个原则，但不照搬其所有具体行为：

- 不要求summary列出ALL user messages；Pulsara由Runtime保留最近3条真实用户原话；
- 不把整个recent transcript重新塞回去；只保留最多3个完整tool group及它们之后的canonical suffix；
- 不列出所有artifact handle；只让已保留ToolResult按现有projection暴露必要handle；
- 不依赖裸XML closing marker承载不可信正文；snapshot和runtime handoff使用closed canonical JSON；
- 不把current MCP/skill/permission等复制进summary，它们由各自compiler source重建。

grok-build还提供了固定meta-dispatch这一有价值的窄机制：动态MCP descriptor不必全部进入provider `tools`，因此新MCP发现不必破坏KV prefix。该正常epoch产品语义已经完整抽取到Round 9；Round 5B不再拥有search/inspect/use实现，只在合法rebase boundary把Round 9当前NEW cohort确定性重新规划为successor DIRECT或继续META_ONLY。

grok-build的普通工具边界也支持同一结论：通用`DEFAULT_TOOL_OUTPUT_BYTES = 40,000`；bash/terminal使用`20,000 chars`特例；MCP使用`20,000 bytes`特例。Pulsara对这组prior art的取舍已经由Round 7.1独立冻结：normal logical FULL采用统一40,000-byte量级，但不照搬per-tool lowering特例。Round 5B只消费该已激活contract，不拥有阈值。

---

## 2. 产品语义与non-goals

### 2.1 Compaction不是删除、记忆或事实合并

Compaction只改变provider下一次看到的model-input base。它不改变：

- canonical transcript任何row；
- ToolResult、artifact、Plan、permission、memory、MCP或Terminal的domain truth；
- assistant/tool occurrence；
- user输入的原文；
- old snapshot的immutable content。

summary是derived continuity handoff，不是用户消息，不是accepted memory，也不是业务事实。当前canonical row、当前Runtime fact与compaction之后的新用户输入总是优先于summary。

### 2.2 Compaction允许且只允许一次显式rebase

旧epoch内仍保持strict prefix。summary request本身也必须最大化复用旧prefix：

~~~text
summary.SYSTEM == source_view.materialized_system_prompt()
summary.tools  == source_view.normal_compile_binding.tool_surface.tool_specs
summary.semantic_messages
    == source_view.materialized_messages()[:exact_summary_prefix_count]
       || [synthetic_summary_request]

summary.actual_wire_input
    == Round 5A.2 FrozenProviderWireInputPlan.materialization
       for that exact semantic slice and selected durable native replay
~~~

source_view不是另一个已安装epoch。它是safe point处对“如果现在正常发起下一次model call，模型应看到什么”的只读、不可执行semantic物化，必须覆盖当前exact canonical head，包括尚未进入上一版continuity epoch的最新USER_MESSAGE、USER_STEER与已完成tool group。semantic messages只用于boundary与public projection；adapter实际发送`actual_wire_input`。已有Responses/Chat native carrier不能因为Host重启、idle summary或不存在predecessor epoch而退化为generic semantic history。summary call不得宣称覆盖自己从未看到的canonical suffix。

采用成功后，旧epoch与新epoch不要求prefix关系：

~~~text
old epoch E
  -- CompactionAdopted -->
new epoch E+1

within E+1:
SYSTEM/tools stay equal
messages only append suffix
~~~

以下都不是合法reset reason：物理MCP reconnect且schema相同、provider cache miss、clock变化、memory recall变化、普通turn boundary、model/tool-call数量或用户detach。Round 3.1现有CONTEXT_BINDING_REWRITE仍是本轮唯一合法compiler reset reason。

### 2.3 本轮明确不做

- provider context-length error后的reactive compact/retry；
- remote provider previous_response_id continuity；
- 跨Host恢复正在运行的summary stream；
- summary provider失败的durable retry；
- exact provider request/response audit；
- compaction时自动抽取memory或user preference；
- 删除或归档canonical transcript；
- 通用transcript file/artifact pointer；
- 所有历史artifact的inventory；
- 二次Flash/reranker/special summarizer model；
- hierarchical subagent graph的完整handoff；V1只可投影当前flat task；
- AGENTS.md discovery本身；未来该source存在时按同一rebase规则自然重建；
- 高级Go compaction timeline、history diff或snapshot inspector。
- 把全部MCP tool永久降为meta tool；epoch boundary已经可靠的MCP仍走direct主路径；
- 在普通safe point把late-ready MCP热加入provider `tools`或为此主动reset epoch；
- BM25/dense MCP tool search、descriptor virtual/physical filesystem或通用tool router；
- 让`use_new_mcp_tool`调用builtin、当前direct MCP或任意未inspect的名称；
- 把MCP server instructions、status或catalog提升为SYSTEM authority。

### 2.4 正常Capability语义全部由Round 9/9.1拥有

Round 6的MCP physical semantics与Round 9的统一Capability semantics是本轮硬前置。Round 5B不再supersede Round 6、不再定义正常epoch direct/meta行为；它只消费已经激活的：

- `FrozenCapabilityRegistrySnapshot`；
- `FrozenCapabilityPlanningCut`；
- `CapabilityEpochPredecessor`；
- `FrozenCapabilityExposurePlan`；
- `FrozenMcpRouteProjection`与ordinary `MCP_CATALOG` renderer；
- `FrozenSkillProjectionInput`与compiler最终effective Skill source head；
- normal `ProcessLocalToolSurfaceAccess`、`PreparedToolExecutionBinding`与`PreparedUnavailableDirectMcpGate`。

因此late-ready observation、catalog pagination、inspect/use ref、direct unavailable/schema replacement、Skill discovery与ordinary read语义都不是本轮production修改面。Round 5B只对这些activated contracts做retained regression，并实现自己的compaction-only retained Skill派生。

### 2.5 禁止第二套MCP exposure vocabulary

本文删除并禁止实现`McpEpochExposureKind`、`FrozenEpochMcpExposure`、`McpEpochExposureBorrow`及任何同义compaction-private DTO。DIRECT/NEW/UNAVAILABLE route只能来自Round 9的`FrozenMcpToolExposure`与`FrozenMcpRouteProjection`；native surface只能来自Round 9的`FrozenCapabilityExposurePlan.direct_tool_surface`。

Current MCP supervisor仍是唯一physical/catalog owner；Round 5B的process-local settlement resources只组合一个Round 9 successor exposure plan与一次normal tool-surface physical access，不保存第二份descriptor tuple、catalog generation、pending/latest generation或slot identity。

### 2.6 Epoch boundary的DIRECT选择

Cold/EMPTY cohort selection已经由Round 9定义，本轮不得复制。Active compaction的successor selection是唯一新增规则：

1. 以old installed `CapabilityEpochPredecessor`、current exact-scope owner-issued inventories与complete current registry构造一次新的`FrozenCapabilityPlanningCut`；
2. 保留仍与current READY_CLEAN/config/scope事实exact compatible的旧DIRECT MCP；已removed、dirty、known unavailable或schema-replaced的旧DIRECT不进入successor native candidate；
3. 计算current `NEW_MCP_META_ONLY`候选全集；
4. `fixed Builtin + retained compatible DIRECT + complete NEW cohort`在Round 9 count/schema/provider target bounds内全部fit时，NEW cohort整体提升为DIRECT；
5. 若完整NEW cohort不fit，不提升任何NEW；retained compatible DIRECT继续保留，NEW继续meta，compaction本身仍可成功；
6. 输出标准`FrozenCapabilityExposurePlan`与`FrozenMcpRouteProjection`，不得输出compaction-private exposure type。

这条规则有意不同于Round 9的EMPTY cold all-MCP cohort fallback：active compaction已有一个合法old native surface，不能因为新增cohort过大而无故把仍可靠的旧DIRECT全部降级。它只对“是否新增promotion”all-or-none，不按discovery时序、名字、BM25、dense score或recent use选择partial winner。

### 2.7 `list_mcp_servers`保留总目录语义

完整schema、server/tool page union、cursor identity、DIRECT/NEW/UNAVAILABLE分类与local-snapshot-only行为以Round 9 §6.1为唯一真源。Round 5B只验证successor adoption后该普通工具读取新epoch的current route projection；不修改descriptor、cursor codec或repository。

### 2.8 Late-ready `MCP_CATALOG` observation

Closed、untrusted、complete-row/omitted-count renderer与inspect→use guidance以Round 9 §5.2/§6.4为唯一真源。Summary request只复用old epoch已经可见的exact observation prefix；active successor首次compile按Round 9 normal renderer构造initial head，安装后到达的新变化继续append successor。Round 5B不生成另一种compaction catalog正文。

### 2.9 `inspect_new_mcp_tool` exact contract

Closed success DTO、40K普通ToolResult bound、tool-specific route/policy-bound ref与typed rejection以Round 9 §6.5–§6.6及Round 7.1为唯一真源。Compaction只使old-epoch ref在epoch close后自然stale；它不重新签发、持久化或迁移ref。

### 2.10 `use_new_mcp_tool` exact contract

Schema、ref resolution、dirty/permission/admission/attempt/invoke顺序、one-attempt/result与MCP origin以Round 9 §6.7–§6.8为唯一真源。本轮只验证summary期间工具绝对不可执行、successor epoch中仍为NEW的tool继续使用该普通路径，以及promoted tool只能直接调用。

### 2.11 断连、replacement与dispatch矩阵

普通disconnect、dirty、same-schema reconnect、removal与schema replacement矩阵以Round 9 §6.9/§10为唯一真源。特别冻结一项跨文档冲突的解法：同一`server_id + remote_tool_name`的DIRECT schema replacement在old epoch中只报告pending cold adoption，旧descriptor fenced，新schema不得同时走meta；active compaction successor boundary才可采用new schema。Round 5B不得恢复旧版“old native + replacement meta双版本并存”分支。

### 2.12 Compaction promotion是正常升格边界

summary call必须继续使用old epoch的exact SYSTEM、direct tools与固定meta tools。summary期间完成的MCP discovery不能插入该request，也不能改变其prefix。

对仍将继续provider loop的active compaction，summary成功后、dry compile前，Runtime调用§2.6的compaction-specific selection，但输入输出都使用Round 9普通contract：

~~~text
old InstalledCapabilityEpochPredecessor
+ current owner-issued Builtin/MCP/Skill inventories
+ current complete FrozenCapabilityRegistrySnapshot
-> successor FrozenCapabilityPlanningCut
-> compaction-specific promotion selection
-> successor FrozenCapabilityExposurePlan
~~~

Host在`ActiveCompactionInstallationResources`中只保存一次successor `FrozenCapabilityExposurePlan`、compiler最终采用的initial `MCP_CATALOG`/`SKILL_CATALOG` heads与dry compiled input。随后通过Round 9已有physical exact-join seam取得普通`ProcessLocalToolSurfaceAccess`：

~~~text
successor FrozenCapabilityExposurePlan.direct_tool_surface
-> exact PreparedToolExecutionBinding / PreparedUnavailableDirectMcpGate leaves
-> normal ProcessLocalToolSurfaceAccess
-> existing slot leases for exact DIRECT MCP bindings
~~~

Round 5B不包装、重命名或复制这个access。现有`McpSlotLease`继续唯一拥有slot、connection generation、admitted discovery generation与execution-binding identity；Round 9 `FrozenCapabilityExposurePlan`继续唯一拥有successor semantic surface/route。E1→E2 same-schema reconnect只替换normal physical access，不改变successor semantic plan。

current permission、Plan、skill、memory和live state同样只进入process-local semantic installation resource。任何capability planning/physical字段都不进入`PreparedCompactionCanonicalAdoption`或其fingerprint。access/callback/slot对象绝不序列化。Normal tool-surface access从dry compile持续到FULL后的installation settlement或attempt discard：

- 若只physical reconnect且semantic fingerprints相同，supervisor可在exact policy下替换physical binding；
- 新的semantic discovery/listChanged继续只由既有MCP supervisor拥有，不能原地修改active resources中的successor plan；physical access不得复制第二个“最新generation”owner；
- 因此MCP抖动不会让已经validated的summary或canonical adoption无限重做；FULL后Runtime用Round 9 normal planner按`current registry - frozen successor DIRECT versions`派生NEW suffix；
- canonical write前physical access丢失、owner close或没有compatible binding时discard attempt；canonical FULL后则保留snapshot winner，installation按frozen semantic plan尝试exact rebind，失败时不回滚canonical rows并留给下一次cold read；
- promotion完成前旧epoch仍按旧direct/meta分类结算已经开始的tool batch。

active adoption FULL后：

- current READY_CLEAN且surface fit的NEW工具成为下一epochDIRECT；
- old meta refs全部按old epoch失效；
- Round 9 `list_mcp_servers`按新epoch重新报告DIRECT/NEW；
- Round 9 `use_new_mcp_tool`继续存在，但只接受adoption之后新出现的工具；
- handoff追加一条bounded guidance：已出现在native tool list的MCP必须直接调用，inspect/use只服务本次rebase之后的新发现工具。

若normal physical access冻结后某个promoted descriptor被server移除或semantic replacement，frozen successor plan仍按新epoch保留该DIRECT descriptor但以Round 9 unavailable gate阻止dispatch；supervisor中的replacement等待后续cold adoption，不能在同一epoch以meta绕过。不得为了追逐current generation反复废弃已经合法的summary，也不得让旧descriptor调用新schema executor。

promotion进入provider `tools` channel，不进入BASE_SYSTEM。MCP server instructions、status、failure与尚未promotion的tool names仍属于untrusted runtime observation。本文所称“把动态内容放回新context root”是按其真实authority重新placement，不是把所有内容字面拼到system message。

idle manual compaction不执行本节promotion：它只canonicalize snapshot base，不知道下一条prompt到达时哪些Capability facts仍current，也没有provider call消费continuity permit。下一条same-scope turn以当时owner inventories走Round 9普通EMPTY planning；这同样是合法epoch boundary，但不由idle settlement提前持有tool-surface access或slot lease。

### 2.13 Prefix与bound不变量

同一个installed epoch内必须证明：

~~~text
SYSTEM[n + 1]   == SYSTEM[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
~~~

以下都只按Round 9追加message observation，不改tool fingerprint：late-ready、新tool、schema replacement、disconnect、reconnect status与removed status。只有compaction/cold epoch boundary才重新冻结DIRECT set。

所有normal MCP bounds、catalog/list/inspect/use contracts与fixed descriptor fingerprints以Round 9为准；所有normal ToolResult bounds以Round 7.1为准。本轮只新增compaction quote约束：

- successor `FrozenCapabilityExposurePlan`必须满足Round 9 native tool count/schema/provider target bound；
- successor initial catalog heads必须经过normal compiler VALUE/CLEARED/UNAVAILABLE/no-op决策，不能把raw projection直接塞进new epoch；
- successor `SKILL_CATALOG` / `ACTIVE_SKILL`以及本轮`RETAINED_SKILL_CONTEXT`必须由同一次compile选择并与continuity CAS安装；
- retained ToolResult只复用Round 7.1 normal variants，3-group aggregate quote不得建立更高cap；
- fixed descriptor与lowering contract变化都必须在本轮开始前已由前置round cold-activated，compaction不得兼任它们的migration入口。

---

## 3. Authority、canonical形状与当前summary唯一性

### 3.1 三层authority

| 层 | 内容 | 是否durable |
|---|---|---|
| canonical history | 完整transcript、snapshot、binding revision、CompactionAdopted | 是 |
| process-local attempt | safe-point fence、summary stream、tail plan、active dry compile或idle base validation、adoption settlement | 否 |
| provider projection | 当前snapshot + post-cut rows + current sources/tools | 否，可从前两层重建 |

Runtime不得把provider projection写回成第二套canonical history。

### 3.2 当前模型看到哪一版summary

不存在“模型自由选择summary版本”。唯一选择链是：

~~~text
turn.current_context_binding_revision_id
  -> turn_context_binding_revisions.context_snapshot_id
  -> context_snapshots.content
~~~

同一个turn可有多个历史binding revision，多个turn也可引用不同历史snapshot，但任意一次PreparedProviderInputCut只exact join一个current revision。两次并发compaction都基于同一predecessor revision时，只有一个CAS winner；loser为CONFLICT，不能安装自己的summary。

未采用的raw summary只存在于attempt内并被丢弃。已采用的旧snapshot继续immutable保留，供历史revision解释，但不会与current pointer共同成为active input。

### 3.3 为什么不持久化retained manifest

受保护tail的durable表示已经是：

~~~text
snapshot.source_through_sequence = B
reader selects exact-scope entries where sequence > B
~~~

额外保存retained_group_manifest会产生同一边界的重复真源。`CompactionSettlementResources`可以冻结group IDs、ordered call/result IDs与proof用于write前验证，但它们不进入canonical candidate，也不参与ACK FULL confirmation；事务成功后，snapshot source cut和未被覆盖的canonical rows就是唯一truth。

### 3.4 既有relation足够

Round 5B新增零张relation、零种Committed event、零种Live event、零个subject slot。它复用：

- context_snapshots；
- turn_context_binding_revisions；
- turns.current_context_binding_revision_id；
- CompactionAdopted；
- blobs，用于超出snapshot inline bound的summary carrier。

### 3.5 Capability exposure同样不成为durable authority

MCP supervisor仍是当前Host唯一physical/catalog owner；provider exposure只由Round 9在epoch boundary对complete registry/planning facts作process-local投影：

~~~text
current owner-issued inventories + complete registry
    -> FrozenCapabilityPlanningCut
    -> FrozenCapabilityExposurePlan
    -> provider direct tools + NEW catalog observation/meta refs
~~~

不新增“direct promotion row”“new MCP table”或session-level exposure receipt，也不把Round 9 pure DTO复制进compaction package。Host close/takeover后旧meta ref自然失效；replacement Host重新连接、完整discovery并在cold epoch按当前READY_CLEAN truth冻结direct surface。canonical transcript只保留模型实际发出的`inspect_new_mcp_tool`/`use_new_mcp_tool` request及其普通ToolResult，不保留process-local catalog generation。

---

## 4. Closed policy与active-context测量

### 4.1 ResolvedCompactionPolicy

配置在Host open时验证并冻结为process-local immutable fact：

~~~text
ResolvedCompactionPolicy
    enabled: bool                         default true
    automatic_enabled: bool               default true
    manual_enabled: bool                  default true
    fallback_context_window_tokens: int   default 262144
    auto_trigger_ratio: float              default 0.85
    post_compaction_target_ratio: float    default 0.55
    minimum_reclaim_tokens: int            default 16384
    maximum_retained_tool_groups: int      exact 3
    maximum_retained_tail_utf8_bytes: int  default 2097152
    maximum_recent_human_messages: int     exact 3
    maximum_recent_human_utf8_bytes: int   default 65536
    maximum_summary_utf8_bytes: int        default 65536
    maximum_runtime_handoff_utf8_bytes: int default 32768
    planning_attempt_seconds: float        default 120
    summary_attempt_seconds: float         default 1200
    maximum_consecutive_auto_failures: int exact 3
~~~

`resource headroom OR trigger`与“summary tool-call违规后最多一次repair”是本contract不可关闭的类型行为，不是policy实例字段。sealed policy factory不接受这两个参数；固定literal直接进入对应algorithm domain/version。若未来产品要改变它们，必须升级contract，而不是在同一contract下产生第二种实例取值。

合法范围：

- 131072 <= fallback context <= 2097152；
- 0.70 <= trigger <= 0.95；
- 0.30 <= target <= 0.75；
- target + 0.10 <= trigger；
- recent human count为1..3；
- retained tool group count为1..3；本轮production固定3，测试可注入更小值；
- summary与handoff byte bound均至少8 KiB且至多256 KiB；
- planning至少30秒、summary至少300秒；
- configuration不能改变canonical candidate semantic identity；它只影响新attempt planning。

环境变量或未来config入口可以覆盖context window与ratio，但provider/model明确给出的resolved context window优先。若provider只给input budget，不再叠加另一个独立128K cap；Round 3 target estimator仍拥有最终effective input budget。

### 4.2 Trigger读取active input与可由rebase缓解的local headroom

自动判断分两阶段，避免为了判断是否接近reader hard bound而先越过该bound：

1. one-cut reader先执行bounded、index-backed headroom preflight，只返回current effective materialization floor之后的item count与canonical bytes；continuity owner提供当前epoch logical bytes；
2. 若resource headroom尚未触发，再读取或构造`FrozenCompactionSourceView.provider_projection.final_estimate`，并以`normal_compile_binding.effective_input_budget_tokens`判断token ratio。

最近一次完整FrozenCompiledModelInput只有在canonical frontier、binding、target、tool surface与全部source fingerprints都与本次source view exact相等时才可复用：

这里“复用”仅指复用其already-validated wire fields/estimate来构造或验证`FrozenCompactionProviderProjection`；source view不嵌套`FrozenCompiledModelInput`，因为后者无法表达over-budget view。

~~~text
active_ratio = total_input_tokens / effective_input_budget_tokens

resource_headroom =
    provider item/message count
    post-base exact-scope canonical UTF-8 bytes
    continuity epoch logical UTF-8 bytes
    one maximum legal next-admission quote

should_auto_compact =
    active_ratio >= auto_trigger_ratio
    OR resource_headroom crosses any derived soft boundary
~~~

derived soft boundary不是另一组随意常量。它由现有hard bounds减去“当前exact scope一次最大合法admission”的closed quote得到：

- reader/provider item hard bound减去一次最大合法ordered steer或assistant/tool-result batch item quote；
- reader 16 MiB physical materialization hard bound减去一次最大合法canonical admission byte quote；
- continuity epoch 64 MiB logical hard bound减去一次最大合法dispatch append working-set quote。

中央factory冻结一个process-local fact：

~~~text
CompactionResourceHeadroomQuote
    exact scope / current binding / effective materialization floor / safe head
    post-base canonical item count / UTF-8 bytes
    current continuity epoch logical bytes
    maximum legal next-admission item / canonical-byte / epoch-byte reserve
    resolved_hard_bound_set_fingerprint
    quote fingerprint
~~~

quote algorithm固定为类型常量`pulsara.compaction-resource-headroom.v1`，不作为调用者参数。`resolved_hard_bound_set_fingerprint`仍必须保留：reader、continuity、scope admission与当前resolved target的实际bound可能随合法配置变化，固定contract version不能替代对这些动态resolved values的exact join。中央factory将所引用的全部bound值规范化后只产出这一份集合fingerprint；调用者不得分别传入多枚contract fingerprint。

reserve按当前scope允许的admission kinds取最大closed quote；不能由调用点猜一个平均消息大小，也不能每个子步骤重新计量。该fact只用于提前触发，既不写库也不改变canonical candidate identity。这样正常路径至少保留一次最大合法admission的headroom，compaction planner仍能在第4,097项或16/64 MiB hard stop之前物化source view。

以下不参与trigger：

- session累计input/output；
- cached/uncached provider billing；
- turn已运行时间；
- model/tool-call次数；
- session/genesis以来的canonical transcript总字节；
- 数据库中snapshot数量；
- provider远端错误文本。

post-base canonical bytes参与resource headroom，仅因为它们是当前reader必须物化且能被本次rebase降低的工作集；它不等于按数据库总大小或历史累计usage触发。

否则planner必须使用同一target、tool surface和one-cut facts构造read-only prospective source view；它不得打开provider transport、注册PreparedProviderInputAppendCandidate或推进continuity epoch。这样刚接纳的USER_MESSAGE、USER_STEER或tool result可以在其第一次真实provider call前触发compaction，而不会被上一call的较小estimate遮蔽。

### 4.3 Post target是adoption gate，不是正文截断器

planner必须在summary前预留：

- current SYSTEM与tools estimate；
- current MUST_KEEP source variants；
- runtime handoff maximum；
- selected protected tail；
- maximum summary output；
- provider envelope margin。

summary完成后，active branch再用实际summary执行exact dry compile；idle branch执行§12.2 bounded base validation。automatic/mid-turn adoption要求实际new input <= post target。manual force允许超过soft target，但仍必须：

- 比旧active input至少减少minimum_reclaim_tokens，或旧input已经越过hard provider boundary；
- 严格低于effective input budget；
- 满足Round 3所有physical/working-set bounds。

Runtime不得为了命中target按字符截断summary、用户原话、tool call arguments或provider message。它只能减少protected group数量、使用现有ToolResult render ladder，或让attempt失败。

### 4.4 Failure circuit只process-local

同一turn连续3次automatic attempt在summary或adoption前失败后，剩余turn不再自动compact；manual仍可请求。下一条真实ROOT USER_MESSAGE或新的child turn重置计数。

同一个provider dispatch最多采用一次compaction。new epoch dry compile仍超过target时，不允许在同一dispatch递归summary summary。

---

## 5. Trigger、safe point与用户输入fence

### 5.1 三个入口

| trigger | admission | adoption后行为 |
|---|---|---|
| MANUAL | active ROOT/child的下一个safe point；idle时target latest exact-scope terminal turn | active时继续same turn；idle时返回compact result，下一turn继承snapshot |
| AUTO_ACTIVE_CONTEXT | 每次normal provider dispatch planning开始前 | 继续本次dispatch |
| MID_TURN_FOLLOWUP | 一个完整tool batch接受后、follow-up provider planning前 | 继续same turn follow-up |

manual command可以force低于threshold的compact，但仍必须有可压缩prefix和正reclaim。没有可压缩内容返回typed NOT_NEEDED，不写snapshot/event。

### 5.2 Provider safe point

compaction admission必须同时满足：

1. no active provider handle；
2. require_provider_safe_turn()成功，即全部assistant tool call都有result或typed closure；
3. no open tool/Plan interaction或human confirmation；
4. exact Host writer仍有效；
5. exact scope/turn没有另一compaction attempt；
6. no PreparedProviderInputAppendCandidate处于未settled PREPARED；
7. continuation/steer consumer尚未freeze下一dispatch；
8. current binding revision、canonical head与continuity epoch exact join；
9. source cut不会拆开assistant tool-call batch；
10. Host未开始close。

进入global lane前可以做一次无副作用precheck以避免无意义排队，但它不冻结target。上述十项的authoritative check必须在取得lane后按§5.3重新捕获并一次验证，只有该winner能安装scope fence。

Terminal process、Terminal monitor和flat subagent可以继续physical运行；它们不能在fence期间修改被summary覆盖的exact target canonical prefix。active branch在summary之后由Runtime handoff重新采集其最新current state；idle branch留给下一turn cold compile。

### 5.3 Host-owned CompactionAdmissionFence

Host保留一个Host-wide compaction execution lane，使同一Host最多运行一个昂贵summary transport；该lane只限制compaction physical execution，不等于暂停整个Host。

safe point admission同时在Host lock内为exact target scope安装唯一process-local `CompactionAdmissionFence`。fence持续到attempt失败释放或adoption settlement结束，只覆盖会改变该target source head、binding或runner ownership的producer：

- exact target turn的STEER_ACTIVE_TURN submission/consumption；
- 以exact target binding为predecessor的QUEUE/NEW_TURN admission；
- 目标为该scope/turn的Terminal monitor observation installation；
- 会向该target scope安装observation或创建successor turn的subagent/external result；
- 该target scope的Plan automatic continuation；
- manual/automatic second compaction；
- exact target active task slot replacement。

其他ROOT/child scope的canonical工作和provider loop不得因另一个scope的summary暂停；一个child compact时，ROOT与其他child继续运行。若无关child完成会向正在compact的ROOT安装external result，只延后该exact installation，child自身的physical/canonical terminalization仍可完成。

physical Terminal/subagent工作可继续，memory governor与MCP discovery也可继续；它们不能写入被freeze的exact target transcript cut。MCP discovery只能更新current catalog并形成NEW observation，不能热改summary call使用的old epoch direct surface。adoption transaction仍必须重验canonical head，不能只相信fence。

lane与fence的取得顺序固定为：

1. 不持有Host lock，进入bounded FIFO并await Host-wide summary execution lane；等待期间target scope完全正常运行，尚无fence；
2. 取得lane后重新进入Host lock，从current Host state重新捕获target turn/scope、active/idle branch、binding、writer与safe-point identity；不得复用排队前的stale capture；
3. 验证仍可admit后，在同一Host lock临界区安装exact-scope fence和CompactionSummaryAttempt owner；失败则释放lane并返回typed defer/conflict；
4. 释放Host lock后才允许执行RR read、planning、provider summary、blob publication、canonical write/confirmation或任何await；
5. terminal path先在Host lock内移除exact-scope fence/owner，再在锁外释放global lane。

禁止“先安装scope fence、再等待最长20分钟global lane”，也禁止持Host lock等待lane、PostgreSQL、provider或physical close。

### 5.4 Compaction期间的用户输入

产品语义不是“steer compaction model”。对安装了fence的exact target scope，在compaction期间：

- TUI composer仍允许用户输入；
- Enter/Tab不发送STEER、SUBMIT_PROMPT或QUEUE_PROMPT wire command；
- client将文本和stable command candidate保留在bounded process-local deferred lane；
- 文本不进入summary request、source cut、snapshot或runtime handoff；
- compaction FULL后，client才按当时正常session状态提交；
- compaction失败后，旧turn恢复，client同样按普通routing提交；
- disconnect可丢失尚未发送的local draft，Server不为它创建receipt或durable queue。

同一client最多保留现有composer允许的bounded条目与字节；不同client不得互相看到未发送draft。Protocol应通过live control明确compaction_in_progress；拒绝无视fence的remote command时返回COMPACTION_IN_PROGRESS，不自动改投下一turn。

### 5.5 FrozenCompactionSourceView

fence安装后，planner必须先冻结一个process-local、immutable、不可执行的source view：

~~~text
CompatibleAppendCompactionProjection
    predecessor_epoch_semantic_prefix_fingerprint
    append_only_messages
    final_estimate: TokenEstimate
    logical_utf8_bytes
    projection_fingerprint

ColdRebuildCompactionProjection
    system_prompt
    full_messages
    final_estimate: TokenEstimate
    logical_utf8_bytes
    projection_fingerprint

FrozenCompactionProviderProjection
    = CompatibleAppendCompactionProjection
    | ColdRebuildCompactionProjection

FrozenCompactionSourceView
    compatibility:
        EMPTY_COLD
      | COMPATIBLE_APPEND
      | PENDING_NON_COMPACTION_RESET
    canonical_dispatch_read: FrozenCanonicalProviderDispatchRead
        # Round 5A.2: canonical compile snapshot + metadata-only replay manifest cut
    normal_compile_binding: ModelInputCompileBinding
        # owns the one FrozenModelToolSurface
    predecessor_epoch_view: FrozenProviderInputEpochView | None
    capability_epoch_predecessor: CapabilityEpochPredecessor
    provider_projection: FrozenCompactionProviderProjection
    physical working-set report
    source_view_fingerprint
~~~

这些existing frozen carriers是组合关系，不是把字段重新摊平：

- `FrozenCanonicalProviderDispatchRead`唯一组合`FrozenCanonicalCompileSnapshot`与同一RR cut中的metadata-only durable replay manifest；其`compile_snapshot`拥有canonical provider items、binding/permission/Plan/outcome/freshness one-cut facts与cut identity。source view不得读取或复制private replay body；
- `ModelInputCompileBinding.tool_surface`唯一拥有old provider-visible `FrozenModelToolSurface`，不得同时保存另一份“semantic tool facts + tool specs”；
- `FrozenProviderInputEpochView`仅在已有predecessor epoch时存在，唯一拥有prior epoch nonce/revision、compatibility、prefix、frontier与source heads；
- `CapabilityEpochPredecessor`直接复用Round 9 EMPTY/INSTALLED union；INSTALLED时其唯一`FrozenModelToolSurface`必须与`normal_compile_binding.tool_surface`及`predecessor_epoch_view` exact join，不复制MCP descriptor或route tuple；
- `FrozenCompactionProviderProjection`只拥有无法由上述carrier取得的prospective wire delta/冷投影与其estimate。它不是`FrozenCompiledModelInput`：whole source view允许超过effective input budget，而production `FrozenCompiledModelInput`的closed invariant要求成功输入已经在budget内；

`PreparedProviderInputCut`是reader admission输入，不是source view的第二份identity。factory先验证它与returned `canonical_dispatch_read.compile_snapshot` identity及replay manifest cut exact join，并证明projection由该snapshot、同一one-cut source facts和normal lowering产生，随后不把cut存进view。`exact_safe_canonical_head`也不是独立构造参数，而从`canonical_dispatch_read.compile_snapshot.canonical_input.identity.provider_input_through_sequence`派生；COMPATIBLE_APPEND时factory还证明predecessor epoch frontier是该current cut的prefix，不能错误地把predecessor frontier本身当作current safe head。

projection closed union type与source-view compatibility必须exact join；不存在调用者可填写的第二个branch enum：

- COMPATIBLE_APPEND只允许`CompatibleAppendCompactionProjection`，不复制predecessor SYSTEM/tools/messages；`materialized_system_prompt()`来自predecessor epoch，`materialized_messages()`等于predecessor messages加append-only suffix；
- EMPTY_COLD与PENDING_NON_COMPACTION_RESET只允许`ColdRebuildCompactionProjection`；SYSTEM/messages由该projection拥有，tools仍只来自`normal_compile_binding.tool_surface`；
- `projection.final_estimate`与`logical_utf8_bytes`由central factory对materialized SYSTEM、唯一tool surface与materialized messages计算，不是调用者参数；estimate允许超过effective input budget，summary prefix与adoption后的active dry compile仍各自必须满足其真实budget。

`source_view_fingerprint`只组合compatibility discriminator及上述existing carrier/projection/capability-predecessor的canonical fingerprints，再覆盖physical working-set report；其中replay部分只组合5A.2 manifest-cut fingerprint，不组合尚未hydrate的private body。不得把这些carrier的内部标量重新序列化一遍。`ModelInputCompileBinding`中的estimator object/transport capability不进入fingerprint，只使用其既有binding/target/estimator/tool-surface fingerprints。

它表达“在这个safe point正常继续时，当前authority会投影出的完整**semantic**上下文”，但它不是PreparedProviderInputAppendCandidate，也不能被DirectModel打开；尤其`materialized_messages()`只是tail/boundary规划输入，不是可直接发送给Chat/Responses的physical history。构造规则为：

1. COMPATIBLE_APPEND必须以`predecessor_epoch_view`逐字验证projection只追加exact canonical delta和本次冻结的source observations；
2. EMPTY_COLD从当前one-cut事实冷构造；
3. target、SYSTEM、非MCP tool surface或lowering contract已经要求普通cold reset时，使用PENDING_NON_COMPACTION_RESET冷构造当前view；不得为了缓存伪装成compatible append；epoch内MCP late-ready/semantic replacement不是该分支，只能成为append-only catalog observation；
4. view必须覆盖exact safe canonical head；cut之后才提交的row不进入view；
5. whole view允许报告“超过effective provider input budget”，因为它不会直接open；但仍必须满足reader、单item、JSON、message count和aggregate working-set等physical hard bounds；
6. summary实际切出的prefix加synthetic request必须重新通过真实target estimator并严格低于provider input budget，才允许physical open；
7. central factory必须验证materialized tools恰好等于`normal_compile_binding.tool_surface.tool_specs`、target/estimator exact join，且不得允许调用者另传第二份tools、prior epoch标量或estimate；
8. 构造期间不得注册continuity PREPARED state、消费steer、接受tool attempt或修改任何canonical row。

cache承诺按compatibility分层：

- COMPATIBLE_APPEND：已安装prefix严格不变，summary request最大化复用provider cache；
- EMPTY_COLD：没有历史cache承诺；
- PENDING_NON_COMPACTION_RESET：只保证语义正确，不承诺复用旧remote cache，因为普通dispatch本来也必须reset。

source view使用现有reader、lowering与source policy，不能长出第二套provider semantics。若normal dispatch已经因local budget quote失败，planner仍可从这个bounded non-executable view选择可压缩prefix；这不是provider error reactive retry，provider open仍为0。

---

## 6. Exact source cut与protected tail

### 6.1 Tool group是不可拆原子

一个CompleteToolGroup定义为：

~~~text
one ASSISTANT_TOOL_REQUEST entry
    ordered assistant TEXT/DATA blocks
    ordered TOOL_CALL blocks
all matching result/closure items for every tool_call_id
~~~

并行batch仍是一个group。planner不得：

- 只保留batch中的最后一个pair；
- 把assistant tool request放进summary而把result留在tail；
- 让result没有preceding assistant tool call；
- 按单个ToolResult大小独立移动source boundary；
- 把late result提前到其closure之前。

### 6.2 Protected tail selection

在exact one-cut canonical snapshot中：

1. 枚举safe head之前全部complete tool groups；
2. 从最新group向前扫描；
3. 尝试3、2、1、0个group的longest suffix；
4. boundary位于earliest retained assistant entry之前；
5. boundary之后的全部exact-scope canonical entry自然成为tail，包括夹在group之间的user、assistant text、Terminal observation与Plan continuation；
6. 使用现有compiler与ToolResult variant ladderquote新input；
7. 选择满足count、2 MiB canonical working-set hard bound与post target的最长suffix。

如果latest group即使降级后仍不能与mandatory current sources共同容纳，planner选择0 group，把该group纳入semantic summary。它不得输出破坏pairing的半个group。

retained group及其tool-call anchors只有一个process-local owner：

~~~text
ProtectedTailSelectionFact
    source_view_fingerprint
    ordered retained CompleteToolGroup identities
        assistant_entry_id / assistant_entry_sequence
        ordered tool_call_ids
        ordered result-or-closure item fingerprints
    earliest_retained_assistant_entry_id | None
    source_through_sequence
    protected_tail_message_start_index
    protected_tail_selection_fingerprint
~~~

它由longest-suffix planner一次生成。`ProviderPrefixCutProof`、summary call与settlement resources只能引用这一fact或其fingerprint，不得分别保存retained group IDs、tool-call anchors或tail start。fact不写库；其目的只是让semantic prefix cut、source boundary与post-summary exact tail在异步阶段exact join。真正发送的native wire prefix另由§8.2唯一`FrozenProviderWireInputPlan`证明，不能把该fact或proof的semantic message fingerprint冒充actual wire fingerprint。

### 6.3 Boundary表示

若earliest retained assistant entry sequence为S：

~~~text
source_through_sequence = S - 1
~~~

如果不保留group：

~~~text
source_through_sequence = exact safe canonical head
~~~

entry sequence是session-global单调序列；reader仍按exact scope过滤。`source_digest`是bounded cumulative lineage digest，而不是每次从genesis重扫全部rows：

~~~text
CompactionSourceLineageBase
    FULL_HISTORY_GENESIS
        exact scope identity
        effective_materialization_lineage_floor = 0

    CURRENT_SNAPSHOT
        current context binding revision identity
        current snapshot_id
        prior source_through_sequence
        prior source_digest
        effective_materialization_lineage_floor = prior source_through_sequence

source_digest = H(
    "pulsara.compaction-source-lineage.v1",
    exact scope,
    lineage base closed branch and fields,
    new source_through_sequence,
    FrozenCompactionCanonicalRange.range_fingerprint
)
~~~

这里必须区分两个不同概念：

~~~text
persisted_revision_genesis_marker
    = turn initial_entry_sequence - 1
    # revision-0 insertion/inheritance bookkeeping only

effective_materialization_lineage_floor
    FULL_HISTORY = 0
    SNAPSHOT     = snapshot.source_through_sequence
    # reader/range digest/provider materialization authority
~~~

`persisted_revision_genesis_marker`不属于`CompactionSourceLineageBase`，也绝不进入`source_digest`；它只作为expected predecessor revision row的数据库precondition/confirmation fact。FULL_HISTORY digest的semantic base固定为`(exact scope, FULL_HISTORY_GENESIS, floor=0)`。

因此，一个initial entry sequence为100的第三个普通turn首次compact时，FULL_HISTORY仍读取并digest该exact scope中`0 < sequence <= boundary`的全部既有历史；绝不能从99开始。重复compaction才只扫描current snapshot source floor之后的新range。该digest在逻辑上commit此前已验证的lineage与本次delta，在物理上不重读旧snapshot之前的history。Repository transaction必须锁定expected current binding/base identity并重算同一个bounded range；base之后、boundary之前的row drift会改变digest。不得仅hashprovider text，也不得新增parent_snapshot_id、snapshot chain relation或从EventLog replay。

`canonical semantic row fingerprint`不得由reader与repository各自解释，但也不得为compaction重新声明一套assistant block、ToolResult、timing、closure或late-outcome leaf DTO。Round 3/7已有的provider语义carrier就是唯一leaf authority：

~~~text
FrozenCompactionCanonicalRange
    session_id
    exact scope
    effective_materialization_lineage_floor
    source_through_sequence
    ordered_items: tuple[FrozenProviderInputItem, ...]
    closures: tuple[ProviderToolResultClosure, ...]
    late_outcomes: tuple[LateToolOutcomeObservation, ...]
    canonical_utf8_bytes
    range_fingerprint

canonical_compaction_range_digest(
    CompactionSourceLineageBase,
    FrozenCompactionCanonicalRange
) -> digest
~~~

range envelope只在外层保存一次session、scope、floor与boundary；leaf不得重复session/scope或重新摊平其字段。它直接复用：

- `provider_input_item_fingerprint(FrozenProviderInputItem)`；
- `ProviderToolResultClosure`的现有closed字段编码；
- `LateToolOutcomeObservation`的现有closed字段编码。

range builder使用与normal reader相同的effective base及boundary物化：`ordered_items`只包含该range中真正provider-visible的entry-backed items和由这些request派生的closure items；`CURRENT_SNAPSHOT` base自带的`CONTEXT_SNAPSHOT` item由lineage base identity承诺，不得再次放入delta range。cut-visible late outcome只随其真实result entry落入本range。这样重复compaction不会把prior snapshot正文当作一条新canonical row重新hash。

当前`canonical_model_input_snapshot_fingerprint()`内联了closure与late-outcome framing。实现时将这两段**原样机械抽取**为共享pure leaf helpers，并让现有snapshot fingerprint与compaction range builder共同调用；不得改变现有字段覆盖或同时保留第二份近似实现。未来Round 7 carrier增加provider-visible字段时，只修改唯一carrier/helper即可同时改变normal snapshot与compaction range identity。

range framing固定为domain-separated、length-prefixed canonical encoding，domain为`pulsara.compaction-canonical-range.v1`；不能拼接自由JSON文本，也不能依赖PostgreSQL `jsonb::text`格式。`range_fingerprint`覆盖envelope identity、ordered item fingerprints、closed closure/late-outcome fingerprints与canonical byte count。只有能改变canonical reader/provider语义的字段进入range；writer generation、连接状态、内部diagnostic和非model-visible operational timestamp不得进入。

entry-kind projection必须与Round 3/7 reader一致：USER/STEER/TERMINAL_OBSERVATION等使用其canonical semantic body；assistant正文只来自ordered TEXT/DATA/tool-call blocks，parent manifest作为storage carrier不得进入；ToolResult使用canonical preview、typed dimensions/reasons、artifact semantic handle/digest及timing/origin，不使用blob physical path或storage identity。字段已在existing carrier表达时不得再把整行任意JSON复制进去形成双重、不稳定编码。

reader与repository分别负责在自己的RR/locked transaction中hydrate同一个immutable range envelope，然后调用同一个pure builder。禁止再定义近似的第二套block/tool-result hash。result只有其canonical result entry满足`floor < entry_sequence <= source cut`时才进入；cut之后的late outcome在该range中仍视为absent。builder contract fingerprint进入`compiler_contract`与activation golden。

### 6.4 ProviderPrefixCutProof

summary call不能脱离FrozenCompactionSourceView重新compile一个较短history，因为budget allocator可能为较短context选择更完整source/tool-result variant，从而改变已经冻结的wire语义。planner必须从同一个source view中切出exact wire prefix。

process-local proof冻结：

~~~text
ProviderPrefixCutProof
    source_view_fingerprint
    summary_prefix_message_count
    summary_prefix_messages_fingerprint
    source_through_sequence
    protected_tail_selection_fingerprint
~~~

`source_view_compatibility`、lineage base、prior epoch nonce/revision/prefix均由`source_view_fingerprint`所commit的source view拥有，不在proof中复制。`ProtectedTailSelectionFact`唯一拥有earliest assistant/tool-call anchors；proof只引用其fingerprint并重复保存最终`source_through_sequence`这一必要cut coordinate。factory必须验证该coordinate等于selection fact中的值，调用者不能分别提供两个值。

有retained group时，Runtime以`ProtectedTailSelectionFact`的ordered tool_call_ids在source view的ordered messages中定位唯一preceding assistant tool-call message，summary prefix在该message之前结束。无group时，summary prefix就是source view messages全量。找不到唯一match、canonical attribution漂移、safe head未被覆盖或fingerprint不符均fail closed。

当compatibility=COMPATIBLE_APPEND时，proof先证明summary semantic prefix与prior installed epoch的重叠部分逐项、逐字相等；随后summary wire-plan factory还必须证明对应actual native wire materialization与`predecessor_epoch_view.wire_input_plan`重叠prefix逐项、逐字相等。source boundary只能覆盖summary prefix实际含有的canonical rows；任何已在safe head内但不在summary prefix、也不在post-cut exact tail中的row都会使planning失败。

### 6.5 Summary输入与tail互斥

summary model只看到FrozenCompactionSourceView所选prefix经Round 5A.2 native wire materialization后的actual prefix；retained tail不进入summary request。successor只看到summary snapshot + retained tail。因此同一canonical entry不会同时作为summary source和exact tail source，也不会出现“summary声称覆盖了cut，但实际没看到刚接纳suffix”的窗口。

---

## 7. Runtime保留的最近真实用户原话

### 7.1 选择规则

Runtime从exact scope、sequence <= source boundary的canonical rows中选择最多3条最近真实human input：

- USER_MESSAGE且origin=HUMAN_MESSAGE；
- USER_STEER且origin=HUMAN_STEER；
- chronological order；
- 只保留完整UTF-8正文，不做字符截断；
- 选择满足64 KiB aggregate的longest whole-message suffix；
- 最新一条本身超过64 KiB时，本carrier不保留任何更老quote，由summary承担语义；
- sequence > boundary的真实human input已经在canonical tail中，不复制进snapshot。

明确排除：

- PLAN_CONTINUATION；
- TERMINAL_OBSERVATION；
- JOB_RESULT/SUBAGENT_RESULT；
- summary instruction；
- runtime auto-continue、system reminder与tool result；
- model生成但role恰好为user的provider adapter item。

### 7.2 为什么保留3条而不是ALL user messages

semantic summary已经负责完整长期意图。最近3条原话只用于降低summarizer措辞漂移，尤其帮助模型恢复用户最新约束、纠正和当前问题。ALL user messages会让长session无法真正缩短，也会把已被后续用户修正的早期要求重新放大。

本轮不再发明一个单独的“current run prompt”字段。每条真实human input只落入以下一个位置：位于source boundary之后则作为canonical tail原样保留；位于boundary之前且入选最近3条则进入snapshot recent_user_messages；更早者只由semantic summary承接。不得把同一条原话同时放进snapshot与tail。

### 7.3 Provider carrier不暴露内部identity

snapshot carrier只呈现文本和相对顺序，不呈现entry ID、sequence、digest、turn ID或contract version。process-local `CompactionSettlementResources`仍冻结这些identity以验证正文来自exact cut；canonical candidate只保存最终content/source row drafts。

---

## 8. Summary model call

### 8.1 使用当前主模型，不启用special summarizer

summary request使用当前Host的primary model policy，但target resolution按closed branch取得：

~~~text
CompactionSummaryTargetFact
    ACTIVE
        exact target fact from the current/upcoming normal dispatch planning

    IDLE
        independently resolve once from:
            current Host model config
            exact ROOT | SUBAGENT_TASK role/scope
            current model/provider options
        freeze into PreparedCompactionSummaryCall
~~~

active不得在summary factory里第二次解析出另一个target；idle没有normal dispatch，必须由summary factory显式调用同一个purpose-neutral target resolver。idle resolution只产生model target/transport fact，不创建normal compile candidate、continuity permit、tool-surface executor borrow或runner。target unavailable时summary attempt失败且canonical effect为none。

它不调用DirectKernelJobModel、memory governor model、Flash model或另一个provider配置。

理由不是“主模型永远总结最好”，而是：

- 同一模型最了解自己刚刚执行的task语义；
- 相同SYSTEM/tools/history prefix可以最大化cache reuse；
- 不引入summarizer target resolution、fallback与cross-model contract；
- 不恢复compaction double call或durable job。

### 8.2 SYSTEM、tools与messages

summary request必须使用：

~~~text
semantic SYSTEM = FrozenCompactionSourceView.materialized_system_prompt() exactly
semantic tools  = FrozenCompactionSourceView.normal_compile_binding.tool_surface exactly
semantic messages = exact prefix of FrozenCompactionSourceView.materialized_messages()
                    selected by ProviderPrefixCutProof
                    + one synthetic user-role summary request

actual provider input = Round 5A.2 FrozenProviderWireInputPlan.materialization
    built from the exact semantic slice above
    + source_view.canonical_dispatch_read.replay_manifest_cut
    + current summary replay-target/contract gate
    + selected exact-body hydration
purpose = CONTEXT_COMPACTION_SUMMARY
~~~

`materialized_messages()`只决定semantic boundary、ToolResult variant与canonical attribution，绝不能直接传给adapter。summary factory必须在prefix/tail确定后复用Round 5A.2标准路径：从manifest中选择本prefix实际含有的assistant entries，先做replay-target compatibility，再仅hydrate selected-compatible body，构造最终`FrozenProviderWireInputPlan`。因此Responses `reasoning/message/function_call`与Chat actual closed fields按正常dispatch exact重放；synthetic summary request只作为该actual wire prefix之后的新user-role suffix加入。

exact summary slice已经低于真实budget后，factory才构造一份不安装continuity的`FrozenCompiledModelInput`：prefix中的既有messages复用source view的exact placements；synthetic request取得唯一sealed call-local placement，`origin_entry_id=None`，其item fingerprint由summary prompt contract、exact request text与`ProviderPrefixCutProof` domain-separated生成，ordinal固定为最后一项。它明确不声称synthetic request是canonical transcript row。这样Round 5A.2低层wire planner仍能按existing assistant entry placements做native replacement，又不会要求给summary instruction伪造entry ID、CommittedEvent或source owner。

active且有predecessor epoch时，wire-plan factory必须证明其重叠materialization与`predecessor_epoch_view.wire_input_plan`逐项、逐字相等。idle restart没有predecessor epoch时，从PostgreSQL manifest/body重新构造同一native形状；不得以“没有live epoch”为由退化成generic messages。target不兼容时只允许Round 5A.2定义的显式cold semantic continuation，不允许Chat/Responses互译。

summary target必须调用Round 5A.2同一个closed `ProviderReplayTargetCompatibilityFact` builder。`CONTEXT_COMPACTION_SUMMARY` purpose、synthetic request、`tool_choice=none`、无physical executor以及summary output cap都不进入replay target fingerprint；因此普通`AGENT_MODEL_LOOP`产生的same endpoint/model/semantic transport binding/codec/replay contract carrier可以合法用于summary。API、endpoint、normalized model、semantic transport binding、codec或provider replay contract变化仍必须使其不兼容；与historical replay无关的完整transport-version变化不得单独撤销carrier。不得为了summary另建“宽松target”或把open-world provider request options重新hash进compatibility。

现有`KernelModelPort.prepare_call()`/normal `PreparedKernelModelExecution`不能被含糊复用，因为它们绑定`AGENT_MODEL_LOOP`、continuity install permit和physical tool-surface borrow。本轮冻结一个独立、process-local、one-shot seam：

~~~text
PreparedCompactionSummaryCall
    exact resolved primary-model target/transport binding
    FrozenCompactionSourceView identity
    ProviderPrefixCutProof
    frozen semantic LLMContext:
        exact SYSTEM
        semantic-only frozen tool specs
        exact message prefix + synthetic request
    FrozenProviderWireInputPlan actual_wire_input_plan (private body repr=False)
    final target estimate / request fingerprint
    one-shot open authority
~~~

`purpose = CONTEXT_COMPACTION_SUMMARY`与`wire tool_choice = none`是sealed type/constructor行为，不是调用者可传的实例字段。factory直接将这两个固定literal写入request fingerprint的domain/version；adapter只接收prepared call并按该类型编码`tool_choice=none`。调用者既不能传普通purpose，也不能把suppression改成AUTO/REQUIRED。

factory必须在任何physical open前完成target identity、selected replay hydration、actual wire materialization、request bytes、final estimate及adapter capability preflight。`open_once()`只消费同一个prepared对象，并且只能发送`actual_wire_input_plan.materialization + sealed tool suppression`；它不能从semantic `LLMContext.messages`重新lower另一份history。

存在native replay replacement时，`actual_wire_input_plan.provider_replay_hydration_fingerprint`必须等于本次summary slice的Round 5A.2 hydration fingerprint；不存在replacement时二者均不存在。该proof已绑定source manifest cut、exact scope、summary replay target与selected assistant placements，并由final wire-plan fingerprint覆盖；summary call不得再保存第二份fragments或另建summary-specific hydration identity。

prepared call可以持有provider transport capability，但绝不能持有或取得：

- normal continuity candidate/install permit；
- tool-surface executor borrow；
- tool authorization/invocation callback；
- canonical assistant/tool-result acceptance authority。

Chat Completions与Responses adapter必须依据prepared call的sealed类型，在Round 5A.2 actual materialization之外显式编码各自wire的`tool_choice = none`等价形状，并由constructor/wire golden验证；不能只依赖prompt说“不要调用工具”。未来adapter若无法证明等价wire suppression，必须在closed adapter capability中声明unsupported并拒绝summary open，不能静默退回普通agent loop。

tool specs在这里是纯描述数据，不要求取得live executor borrow，因为physical dispatch被call-purpose gate绝对禁止。COMPATIBLE_APPEND时它们必须与prior epoch完全相等，包括old epoch的DIRECT MCP与三个固定catalog/meta tools；当前catalog中的NEW MCP绝不能插入summary `tools`。PENDING_NON_COMPACTION_RESET时使用普通dispatch本来已经要求的prospective surface，并明确放弃旧cache承诺，但仍不得因MCP late-ready单独进入该状态。

summary request一旦open便不可被中途改写。summary结束后，active branch另行冻结current owner inventories/registry与successor `FrozenCapabilityExposurePlan`、其余Runtime facts并做exact dry compile；idle branch只验证snapshot/post-cut base，不提前建立epoch。successor plan冻结前发生的MCP/Skill变化采用当时最新complete snapshot；冻结后发生的变化继续由各自owner唯一拥有，并在active FULL安装后按Round 9/9.1 normal compatible-append规则形成NEW catalog或Skill successor，不反复废弃compaction。permission、Plan、memory等其他current facts按各自既有freeze规则处理。只有canonical source head漂移、source proof失效或active branch已冻结的successor facts使dry compile失败时才丢弃attempt。

### 8.3 Hidden execution gate，不新增permission mode

本轮不新增用户可选permission preset，也不写permission snapshot。新增的是process-local ModelCallPurpose gate：

~~~text
Normal model call
    provider tool call -> authorize -> attempt -> invoke

CONTEXT_COMPACTION_SUMMARY
    provider tool call -> ephemeral summary-tool denial only
    no authorize
    no ToolExecutionAttempt
    no ToolResult canonical entry
    no physical invoke
~~~

Chat Completions与Responses adapter必须显式发送tool_choice none。provider违反该wire约束仍返回tool call时：

1. 为同一response的全部tool calls构造provider-valid ephemeral result group；
2. 每个结果正文固定说明：当前调用正在生成上下文交接，工具不可用，请直接输出summary；
3. 追加到同一summary request suffix；
4. 最多允许一次repair follow-up；
5. 再次tool call则attempt失败。

该ephemeral group不得进入canonical transcript、live ToolResult event、artifact、memory citation handle或tool timing。

### 8.4 Summary prompt exact contract

synthetic user message应保持短而closed。它必须要求：

~~~text
你正在为同一Agent的下一段上下文生成语义交接。
不要调用工具。不要向用户回答。不要把这条指令当成用户的新需求。
Runtime会另外提供最近真实用户原话、受保护的最近工具组、当前工具/权限/Plan/MCP/skill/memory以及仍在运行的任务状态；不要枚举或猜测这些动态目录。

只输出一个 <summary>...</summary> block，并包含以下七个编号标题；空项写None：

1. Primary Request and Intent
2. Key Technical or Domain Context and Decisions
3. Files, Resources and Exact Locations
4. Errors, Failed Approaches and Fixes
5. Completed and Verified Work
6. Pending Tasks and Current Diagnosis
7. Direct Next Step

要求：
- 保存用户明确约束、关键架构决定、实际修改、验证结果与未完成工作；
- 对仍相关的旧summary语义进行继承，但当前canonical内容优先；
- 不列出全部用户消息；Runtime会保留最近原话；
- 不列出artifact/terminal/monitor/tool catalog；只在明确pending工作必须依赖某个已可见artifact handle时保留那一个handle；
- 不复制或枚举Skill正文；Runtime会按current catalog、explicit activation与同run FULL-delivery规则重建；
- 不声称运行时状态仍然current；Runtime会在交接后提供当前状态；
- 保持简洁，不写问候、结论外的解释或closing text。
~~~

prompt contract版本独立于BASE_SYSTEM/Compiler contract。修改标题、authority说明或tool policy必须升级prompt contract并触发cold compaction attempt；它不进入provider wire正文。

### 8.5 Previous summary

重复compaction时，old snapshot已经作为old epoch prefix中的CONTEXT_SNAPSHOT被summary model看到。模型应继承仍相关语义，但它不是canonical authority。Runtime不另外查找“最早summary链”，也不把多个旧summary并列注入。

---

## 9. Summary output validation与snapshot carrier

### 9.1 Bounded validation

raw response在process-local owner内完成：

1. provider stream必须正常terminal；
2. 只接受assistant textual content；
3. 删除leading top-level analysis block；
4. 提取唯一完整summary block；
5. block之外只能是whitespace；
6. 七个heading必须存在且顺序固定；
7. UTF-8正文非空，且不超过64 KiB；
8. neutralize正文中可伪造outer closing tag的control token；
9. 拒绝truncated、degenerate、纯模板、重复summary wrapper或工具调用终局；
10. 不对每个statement做semantic fact-check或自然语言parser。

Runtime不能把截断输出当成功，也不能用字符裁剪制造合法summary。

validated summary content只取唯一summary block的UTF-8 inner text；`<summary>` wrapper只是本次输出解析边界，不进入context_snapshots.content或后续provider input。

### 9.2 Snapshot provider DTO

context_snapshots.content使用closed canonical JSON，但不把内部contract、fingerprint、UUID或sequence发送给模型：

~~~json
{
  "earlier_context_summary": "...",
  "recent_user_messages": [
    "...",
    "..."
  ]
}
~~~

规则：

- exact keys固定为earlier_context_summary与recent_user_messages；
- recent_user_messages可以为[]，数组顺序就是chronological order；
- 只放完整原文，不增加position、omitted、ID、sequence或digest；
- summary和user text都作为JSON string编码，不能逸出carrier；
- provider lowering可以加稳定的简短说明“这是derived continuity handoff，后续current facts优先”，但不得暴露storage contract；
- snapshot item仍是runtime-generated user-role observation，不升级为SYSTEM authority。

Round 5B activation必须一次性升级稳定BASE_SYSTEM/lowering contract，告诉模型：CONTEXT_SNAPSHOT是derived advisory handoff；其中recent_user_messages是历史原话，不是新的用户请求；post-cut canonical messages与当前Runtime facts优先。该说明在epoch内不变化，后续每次compaction只追加/替换snapshot base，不把动态summary拼入SYSTEM。升级只在cold Host open生效，不允许运行中的旧epoch热切换BASE_SYSTEM。

### 9.3 Canonical snapshot字段语义

既有context_snapshots字段冻结为：

| 字段 | Round 5B语义 |
|---|---|
| source_through_sequence | summary覆盖的exact canonical boundary |
| source_digest | `H(exact scope, current lineage base identity/prior digest, ordered post-base canonical semantic row fingerprints through boundary)`；物理验证只扫描bounded post-base range |
| compiler_contract | snapshot carrier与reader/compiler join contract |
| prompt_contract | 生成summary所用prompt contract |
| model_contract | frozen primary model target semantic contract；不持有transport |
| inline_content/blob_id | closed JSON carrier的唯一body |
| content_digest/size/media/codec | carrier byte identity |

media type固定为application/vnd.pulsara.context-snapshot+json，codec固定utf-8。inline/blob只决定physical storage，不改变provider语义。

### 9.4 不保存raw model output

canonical snapshot只保存validated carrier。private analysis、tool-call repair transcript、provider IDs、usage与raw rejected output最多进入bounded operational diagnostic，不能进入blob或canonical row。

---

## 10. Runtime精确重建与COMPACTION_RUNTIME_HANDOFF

### 10.1 Rebase不等于把所有内容塞进SYSTEM

采用后，new epoch必须从当前authority重新生成：

| 内容 | placement |
|---|---|
| BASE_SYSTEM | SYSTEM channel |
| builtin与successor epoch DIRECT MCP descriptors；固定catalog/meta descriptors | tools |
| project/AGENTS instructions | 其未来的project/system source；本轮不发明 |
| permission、Plan、skill、MCP catalog/direct-new classification、memory、clock、timing/freshness | 各自现有runtime observation source |
| same-run中ordinary read真正完整FULL交付且仍未变化的model-driven Skill | RETAINED_SKILL_CONTEXT |
| compaction summary + recent user quotes | CONTEXT_SNAPSHOT canonical item |
| protected tool groups与post-cut entries | canonical transcript items |
| live Terminal/monitor/TODO/flat subagent | COMPACTION_RUNTIME_HANDOFF source |

不能把permission、MCP catalog/server instructions或memory提升成SYSTEM，也不能把summary当BASE_SYSTEM尾巴。compaction对MCP的“提升”只表示把exact READY_CLEAN descriptor放进下一epoch provider `tools`；它不改变MCP正文的untrusted authority。

### 10.2 新source contract

新增ContextSourceKind.COMPACTION_RUNTIME_HANDOFF：

~~~text
channel       RUNTIME_OBSERVATION
trust         UNTRUSTED_OBSERVATION
lifecycle     SNAPSHOT_ON_CHANGE
budget        MUST_KEEP
variants      FULL | COMPACT
applicable    current context base is SNAPSHOT
~~~

它只表达当前Host可机械证明的live/control **结构、identity、status与currentness**，不表达历史输出。但`command_preview`、`cwd`、TODO text与subagent objective仍可来自用户、工具或模型，不能因为Runtime完成了bounded freeze就被提升为可信指令。当前source只有一个整体trust字段，因此V1将整个`COMPACTION_RUNTIME_HANDOFF`冻结为`UNTRUSTED_OBSERVATION`，不分裂第二个source。

首次cold compile必须安装VALUE或显式CLEARED；后续无变化no-op，状态变化append新snapshot，全部清空append CLEARED。新Host从snapshot resume时，即使所有旧Terminal owner已消失，也必须append CLEARED，终止semantic summary里可能存在的stale running claim。

### 10.3 Closed provider body

FULL body最多包含：

~~~json
{
  "terminal_processes": [
    {
      "process_id": "...",
      "terminal_session_id": "...",
      "status": "running",
      "command_preview": "...",
      "cwd": "..."
    }
  ],
  "terminal_monitors": [
    {
      "monitor_id": "...",
      "process_id": "...",
      "state": "active",
      "pending_observation": false
    }
  ],
  "todos": [
    {"ordinal": 0, "status": "in_progress", "text": "..."}
  ],
  "todo_counts": {
    "pending": 0,
    "in_progress": 1,
    "completed_omitted": 3
  },
  "flat_subagents": [
    {"task_id": "...", "status": "ACTIVE", "objective_preview": "..."}
  ],
  "omitted": {
    "terminal_processes": 0,
    "terminal_monitors": 0,
    "todos": 0,
    "flat_subagents": 0
  }
}
~~~

它不包含：

- Terminal stdout/stderr；
- completed/pruned processes；
- monitor observation正文；
- subagent result正文或dependency graph；
- environment values、secrets或Host owner IDs；
- MCP/skill/permission/Plan/memory正文；
- internal generation/fingerprint。

### 10.4 Bounds与采集顺序

- running Terminal process最多现有8项；
- active/dormant monitor最多现有8项；
- TODO只投影pending/in_progress，最多64项、每项text最多512 UTF-8 bytes；
- flat subagent只投影当前Host ACTIVE，最多当前existing capacity；
- FULL aggregate <=32 KiB；
- COMPACT对Terminal process、monitor与flat subagent保留其actionable public identity、status与exact omitted count；
- TODO没有item ID：其COMPACT必须从FULL ordered actionable items中选择能完整放入的最长前缀，每个保留项仍逐字携带`ordinal + status + text`，并在`omitted.todos`给出未保留的exact数量；不得逐项截断text、只留ordinal/status，或把ordinal描述为稳定ID；
- TODO的`todo_counts`在FULL/COMPACT中都表达原snapshot的exact pending/in_progress与`completed_omitted`总数，不随prefix裁剪而伪造较小current state；
- top-level ordering按固定kind；TODO内部保持owner snapshot order/ordinal，其他kind内部按其public stable ID；
- 路径使用现有public-safe workspace-relative projection；
- 若COMPACT连固定counts/omitted envelope都放不下，或存在actionable TODO但连一个whole item都无法诚实表达，则typed resource boundary、provider open=0；不得发送“有TODO但正文为空”的伪交接。

active branch的live state在summary完成后、dry compile前冻结；adoption FULL后再次读取并要求fingerprint相同，或重新dry compile。idle branch不冻结未来turn的live state。不得在summary开始前抓取一次然后盲用几分钟后的状态。

### 10.5 TODO与Terminal owner修改面

`TodoRunStateOwner`提供exact-run、只读bounded snapshot方法；不得把current items持久化或复制进repository。TODO subshape不携带durable item ID；`ordinal`只是当次projection中的ordered position，completed正文不注入，只进入`completed_omitted`计数。Terminal manager/monitor增加Host-scoped只读snapshot方法，必须在各自lock内freeze，不读raw output。Subagent manager只提供现有flat task的bounded只读view；hierarchical graph后续另行扩展同一source。

### 10.6 `RETAINED_SKILL_CONTEXT`：只保留同run中真正完整交付的Skill

Round 9.1的model-driven progressive disclosure只有普通`read_file`，没有`ACTIVATE_SKILL` intent、catalog lookup、loaded-state或FULL_REQUIRED。Compaction也不能把“执行过read”“ToolResult row已提交”或“path叫SKILL.md”误判成模型已经完整看过Skill。

本轮新增一个**仅在active compaction rebase中纯派生**的source：

~~~text
source        RETAINED_SKILL_CONTEXT
channel       RUNTIME_OBSERVATION
trust         UNTRUSTED_OBSERVATION
lifecycle     SNAPSHOT_ON_CHANGE
budget        MUST_KEEP after deterministic selection
scope         exact ROOT/child run
variants      VALUE_EXACT_FULL | CLEARED_MINIMAL | NOT_APPLICABLE
contract      pulsara.retained-skill-context.v1
~~~

Placement固定在successor current `SKILL_CATALOG` / `ACTIVE_SKILL`之后、`COMPACTION_RUNTIME_HANDOFF`之前；它只存在于messages，不进入SYSTEM或tools。Stable BASE_SYSTEM只说明它是此前已完整看到、为继续同一run而保留的untrusted Skill正文，current user/policy与真实tool authority始终优先。

一个model-driven Skill只有全部满足时才eligible：

1. ordinary `read_file`的exact path在该次read所属scope中对应一个admitted `SKILL.md`，且call从`offset=1`开始；
2. ordinary result明确到达EOF，没有truncation或遗漏pagination；
3. canonical ToolResult preview为`COMPLETE`；
4. 旧epoch实际compiler为该result选择`FULL`，并且包含该FULL result的provider input已经由continuity CAS成功安装；只提交row、HEAD_TAIL、COMPACT、REF_ONLY、OMITTED或取消中的attempt均不算；
5. compaction freeze时current Round 9.1 manifest仍是同name/location的valid winner，current parsed body与当时完整read语义一致；
6. 它属于当前同一真实user run和exact ROOT/child scope，不是更早turn、foreign child或另一Host猜测出的历史read。

V1只接纳**一次ordinary result已经完整覆盖整份文件**的Skill；不把多个partial pages拼成新的proof。超过40K logical FULL而必须分页的valid Skill仍可被模型正常使用，但本轮不会自动retained，compaction后模型按current catalog重新读取。该减法避免page digest、pending assembly与loaded-skill ledger。

Runtime从old `FrozenProviderInputEpochView`的actual installed messages、canonical call/result及current `FrozenSkillProjectionInput`重建上述事实；它不得建立跨call mutable“曾加载Skill”表。资格验证必须复用ordinary `read_file`的closed output parser与pure content renderer：严格解析canonical SUCCESS payload，验证exact path/offset/limit/total_lines/truncated/content；再由current manifest bytes重建同一line-numbered `content`并逐字段相等。`_warning`、dedup telemetry等非content字段不得成为Skill identity。随后另以old epoch actual selected representation证明该exact result为FULL，不能从artifact preview、tool name或人类可读文本启发式推断。当前文件已修改、删除、失效或winner变化时，不重注入旧正文，只重建current `SKILL_CATALOG`，模型需要时重新读取。Host/reopen若已失去证明old actual FULL installation所需的process-local epoch view，则保守视为ineligible；不得从row存在反推FULL。

选择规则固定为：

~~~text
MAX_RETAINED_SKILL_CONTEXT_ITEMS  = 8
MAX_RETAINED_SKILL_CONTEXT_TOKENS = 40_000
candidate order                  = most-recent FULL delivery first
selection                        = take deterministic recent prefix until next item would exceed either bound
render order                     = original FULL-delivery order among selected items
~~~

两项maximum是sealed type constants，不是用户配置或instance field；token quote复用successor primary target estimator。它们是compaction handoff预算，不是Agent Skills格式上限，也不替代Round 7.1单个ordinary ToolResult的40,000 UTF-8-byte logical FULL上限。

重复读取同一unchanged Skill只更新其recentness，不重复正文。Textual/configured `ACTIVE_SKILL`已经由其自身current source重建，不再进入本source；exact ordinary read ToolResult若已在successor protected tail中实际选择FULL，也不再重复注入。若tail只选择COMPACT/REF_ONLY，该Skill仍可按本节规则进入retained source，因为partial preview不等于正文。Planner采用bounded monotonic fixed point：先带retained candidates compile，删除已由tail FULL覆盖的重复项并重compile；每轮至少删除一个、最多8轮，直到没有新FULL覆盖。删除正文只会释放预算，不得使已FULL tail降级，若结果不满足该单调性则implementation conflict。若没有eligible item，首次successor compile使用`CLEARED_MINIMAL`或`NOT_APPLICABLE`的既有stateful-source规则；超出选择bound只在body中给出`omitted_count`，不枚举一串artifact/path ID，因为current `SKILL_CATALOG`已经拥有完整routing metadata。

VALUE body对每项只显示bounded `name + catalog location + exact parsed Markdown body`，不显示frontmatter、digest、read entry ID、tool result ID、epoch nonce或内部proof。它不能：

- 进入BASE_SYSTEM或改变provider `tools[]`；
- 授予file/tool/MCP权限；
-把Skill正文提升成Runtime事实；
- 跨下一条真实ROOT `USER_MESSAGE`继承；
- 在idle compaction中预装到未来run；
- 新增schema、relation、event、receipt、loaded-skill registry或recovery owner。

active rebase后同一run继续时，successor第一次compile把它与current catalog/active/runtime sources一起安装。下一条真实ROOT user message从空retained set重新开始；child集合在exact child run终结时消失。Repeated mid-turn compaction可再次从当前installed epoch纯派生相同或更近的eligible set，不复制上一snapshot的内部identity。

---

## 11. ToolResult、artifact与最近3个tool group

### 11.1 不建立compaction专用ToolResult store

protected group仍由canonical reader读取tool_results与artifact edge。summary snapshot不复制ToolResult正文，不保存artifact inventory。

### 11.2 Protected tail只使用normal rendering policy

compaction只选择保留哪些complete tool groups，不选择另一套ToolResult正文。选中的group必须与普通model call调用同一个`lower_tool_result_variants()`及Round 3 ladder：

~~~text
FULL
-> COMPACT head/tail
-> REF_ONLY
-> OMITTED_BODY
~~~

不得新增`COMPACTION_FULL`、`COMPACTION_COMPACT`、last-result专用cap、parallel-result公平配额或compaction artifact inventory。Planner只按3→2→1→0选择最长完整group suffix，并用normal compiler对每个候选整体quote。等价性必须以Round 7.1定义的`same canonical item + same lowering contract + same call-local augmentation inputs`为前提；不能要求跨epoch opaque citation handle逐字相同。

### 11.3 复用Round 7.1普通ToolResult投影

Round 5B不再修改任何normal ToolResult threshold、canonical preview或artifact contract。Protected tail对每个result只能复用Round 7.1已经冻结的有序variants：

~~~text
FULL -> COMPACT -> REF_ONLY -> OMITTED_BODY
~~~

`MODEL_VISIBLE_TOOL_RESULT_MAX_LOGICAL_UTF8_BYTES`、HEAD_TAIL、COMPACT、REF_ONLY、artifact threshold与`artifact_read`分页的全部精确数值及renderer均以Round 7.1为唯一真源。本文不得再声明同值constant或为last/retained/MCP/Terminal result建立特例。

同一ToolResult在ordinary call与post-compaction retained tail必须调用同一pure builder。只有canonical item、lowering contract与call-local augmentation输入（包括exact scope/epoch citation mapping及当前artifact-read visibility）全部相同时，ordered variants才要求byte-identical。Successor使用自己的citation snapshot；旧epoch `tool:N`不得迁移、保留或伪造成新epochcapability。除这些closed augmentation外，canonical body、artifact/timing/result-state base projection必须一致。

Planner只按3→2→1→0选择最长完整group suffix，并用normal compiler对每个候选整体quote；如果不fit，减少完整group或让normal compiler选择Round 7.1允许的degraded variant。包含`FULL_REQUIRED` result的group只能保持该result为FULL或从protected suffix整体移除，不能拆pair、重做artifact、迁移opaque handle或发明compaction-only representation。

### 11.4 Compaction不诱导模型遍历artifact

- 只在可见retained ToolResult的existing Round 7.1 projection中显示artifact_id；
- guidance复用Round 7.1 conditional语义：“仅当省略正文对当前任务确有必要时，才调用artifact_read”；
- 首尾已经足以判断成功、失败、主要错误或下一步时，模型应继续主线，不读取artifact；
- summary prompt禁止枚举全部handle；
- Runtime handoff不列artifact；
- snapshot carrier只有在summary语义明确依赖一个已在source prefix可见、且Runtime exact验证过的handle时才允许保留该handle文本；
- 不增加read_compacted_history或list_all_artifacts工具；
- artifact_read仍是按具体pending need使用的逃生口，不是compaction后的恢复清单。

---

## 12. Prepared candidate与canonical adoption

### 12.1 Canonical adoption与process-local installation必须物理分层

任何canonical write前，由sealed central factory先接收**独立语义输入**，再派生一个只描述数据库winner的candidate：

~~~text
CompactionCanonicalAdoptionFactoryInput
    stable snapshot_id / binding_revision_id / event_id
    exact target session / workspace / anchor turn / scope
    expected predecessor binding revision fact
    one source materialization identity:
        source_through_sequence / cumulative source_digest
    validated snapshot content bytes-or-blob identity / digest / size / media / codec
    compiler / prompt / model contracts
    event actor / occurred_at
~~~

factory不接受turn pointer winner、event type/subject/payload、next revision ordinal的第二份值，也不允许分别为snapshot与binding传入两个source boundary。

~~~text
PreparedCompactionCanonicalAdoption
    exact predecessor binding revision row mirror

    ContextSnapshotDraft
        stable snapshot_id
        session_id / workspace_id
        source_through_sequence / cumulative source_digest
        compiler / prompt / model contracts
        validated content bytes-or-blob identity / digest / size / media / codec

    TurnContextBindingRevisionDraft
        stable context_binding_revision_id
        exact session_id / anchor turn_id / next revision ordinal
        base_kind = SNAPSHOT
        context_snapshot_id / source_through_sequence

    CompactionAdoptedEventDraft
        stable event_id
        exact event type / subject / actor / occurred_at
        payload = {revision_ordinal}

    canonical_candidate_fingerprint
~~~

`ContextSnapshotDraft`与`TurnContextBindingRevisionDraft`在物理schema中都必须保存`source_through_sequence`，所以prepared candidate仍保留两个**只读row mirror**并在FULL时逐字段确认；但两者只能由factory input中的同一个source materialization identity派生，不能由调用者分别赋值。next revision ordinal由predecessor + 1派生；turn pointer expected winner直接由`TurnContextBindingRevisionDraft.context_binding_revision_id`派生，不再创建`TurnPointerWinner` wrapper；event type、subject和payload revision ordinal同样由binding draft派生，只有event ID、actor与occurred_at是独立输入。

这里的row mirror都能由`context_snapshots`、`turn_context_binding_revisions`、`turns.current_context_binding_revision_id`、predecessor revision和exact committed event逐字段查询。trigger、source view、prefix proof、retained group IDs、recent-user entry IDs、epoch nonce、capability exposure plan、tool-surface access、execution identity、current-source collection与dry-compile fingerprint一律不进入canonical candidate或其fingerprint。

这些process-local事实进入与candidate分开的closed resource union：

~~~text
CompactionSettlementResources
    canonical_candidate_fingerprint
    FrozenCompactionSourceView
    ProviderPrefixCutProof
    ProtectedTailSelectionFact
    ordered recent-user proofs
    CompactionCanonicalWritePreconditions
        exact target scope / expected turn status
        expected safe head / provider-safe closure

    target:
        ActiveCompactionInstallationResources
            old continuity epoch identity
            frozen current source collection
            successor FrozenCapabilityPlanningCut
            successor FrozenCapabilityExposurePlan
            frozen successor effective MCP_CATALOG / SKILL_CATALOG heads
            frozen RETAINED_SKILL_CONTEXT selection/source value
            dry_compiled_input: FrozenCompiledModelInput
            semantic_installation_resource_fingerprint
            physical_attempt:
                normal ProcessLocalToolSurfaceAccess
                continuity installation authority
                replaceable physical-attempt identity

        IdleCompactionBaseSettlementResources
            expected exact terminal target
            validated bounded snapshot/post-cut base quote
            old continuity identity to clear | EMPTY
            no successor planning/exposure/access/retained-skill/dry input/install authority
~~~

`CompactionCanonicalWritePreconditions`只承载在candidate冻结后仍可能变化、且repository必须在write时重新观察的target status、safe head和provider-safe closure。predecessor pointer与lineage base已经由canonical candidate唯一拥有，不得在preconditions再传一份。

`dry_compiled_input.final_estimate`是唯一target estimate；active resources不得另存`target_estimate`字段。`semantic_installation_resource_fingerprint`覆盖`dry_compiled_input.compiled_semantic_fingerprint`、current source collection、successor planning cut/exposure plan、effective catalog heads与`RETAINED_SKILL_CONTEXT` selection fingerprint。Round 9/9.1 semantic values只在active resources出现一次；nested physical attempt只能通过normal tool-surface access exact join direct surface，并可在E1→E2 reconnect时替换compatible physical binding而不改变semantic resource。

该resource fingerprint不是canonical identity，也不参与stateless FULL confirmation。same-schema reconnect从execution identity E1变为E2时，只替换nested physical attempt并重验semantic join；不得改变semantic resource fingerprint或canonical candidate。semantic surface变化则不能按E2重绑：若尚未canonical FULL可放弃当前installation并重新planning；若已经FULL，则frozen旧DIRECT进入stale gate，同identity replacement保持`PENDING_COLD_ADOPTION`且不得走meta，等待下一次合法cold boundary。

HostWriterGuard generation同样不进入canonical candidate。每次write attempt使用可替换guard；ACK unknown先按stable row drafts exact query，NONE时才重新验证process-local write preconditions并绑定current writer重试。

### 12.2 Active dry compile与idle base validation必须发生在write之前

`ACTIVE_INSTALLATION`分支取得§2.12的successor Round 9 exposure plan与normal `ProcessLocalToolSurfaceAccess`，并使用process-local synthetic canonical snapshot构造：

~~~text
[validated snapshot item]
+ exact canonical items after source boundary through safe head
+ current one-cut compiler facts
+ successor FrozenCapabilityExposurePlan.direct_tool_surface
+ compiler最终effective MCP_CATALOG / SKILL_CATALOG heads
+ deterministic RETAINED_SKILL_CONTEXT VALUE/CLEARED/NOT_APPLICABLE
+ current COMPACTION_RUNTIME_HANDOFF
~~~

调用同一个pure compiler和target estimator。Retained Skill selection按§10.6的recent prefix从8逐步收窄到0，直到同时满足其aggregate bound与整体post target；不得截断单个Skill正文。只有dry compile成功、满足post target、`PreparedCompactionCanonicalAdoption`已经冻结且对应`ActiveCompactionInstallationResources`完整时，才允许repository transaction。dry-compile/resource fingerprint不并入canonical candidate。

事务FULL后，reader对真实snapshot/revision和active resources中唯一的effective catalog heads执行一次exact compile，必须与`dry_compiled_input.compiled_semantic_fingerprint`一致；不一致为implementation conflict，provider open=0。Normal tool-surface access只提供与successor native surface相容的execution leaves。该首次exact compile/install完成后，再由Round 9/9.1 normal planning比较current owner snapshots与frozen successor plan，形成下一次compatible append中的NEW MCP或Skill catalog successor。

`IDLE_BASE_ONLY`不冻结successor tool/MCP/current-source surface，因为下一条用户消息到达前这些事实可以合法变化。它只在write前验证：

- snapshot carrier及post-cut canonical base满足reader、单item、message count和physical byte bounds；
- 相对old active view取得规定的minimum reclaim；
- snapshot/post-cut base在当前resolved target下低于hard input boundary；
- 没有伪造对“未来下一条prompt + 未来current sources”的exact dry-compile承诺。

idle branch绝不创建successor Capability planning cut/exposure plan、`RETAINED_SKILL_CONTEXT`、`ProcessLocalToolSurfaceAccess`、PreparedProviderInputAppendCandidate、continuity install candidate或permit。下一条same-scope turn才按当时current capability catalog、permission、Plan、skill与memory执行普通cold compile。

### 12.3 Canonical transaction

一个Host-writer transaction原子执行：

1. lock exact anchor turn与current binding revision；
2. verify turn/scope/status与process-local write preconditions，并验证canonical row drafts自洽；
3. verify canonical exact-scope head/source digest未漂移；
4. require old provider-safe tool closure；
5. verify no newer binding revision；
6. insert context_snapshots；
7. insert next turn_context_binding_revisions；
8. update turns.current_context_binding_revision_id；
9. append exact CompactionAdopted event；
10. commit。

repository API接收`PreparedCompactionCanonicalAdoption`与窄的write-precondition facts，但只把前者作为winner identity。它不读取process-local capability owner、source view、prefix proof、dry compile、retained-Skill proof或tool-surface access。active resources在write前由Host持有Round 9/9.1 semantic plan、retained source selection与normal physical access；FULL后由同一个settlement task按frozen semantic surface exact rebind/install。physical identity冲突时provider open=0，但不能回滚、改写candidate或另写已经FULL的canonical snapshot。idle resources没有physical access或installation side branch。不得分成“先存summary、再更新binding”两个事务；不得产生orphan snapshot或CompactionStarted row。

### 12.4 Active与idle target

active/idle使用以下closed terminal matrix：

| branch | canonical target | process-local FULL settlement |
|---|---|---|
| ACTIVE_INSTALLATION | exact RUNNING turn | 安装冻结的successor epoch并继续same runner |
| IDLE_BASE_ONLY | latest exact-scope terminal turn | 只保留canonical snapshot winner；清除/关闭该scope旧continuity，不签发install permit，不启动runner |

idle manual compaction必须同时满足：

- session没有active same-scope turn；
- target仍是latest same-scope terminal turn；
- candidate source cut覆盖到其safe terminal head；
- target current binding仍是expected predecessor；
- transaction只更新该terminal turn的context binding pointer，不改变turn status/final entry。

idle adoption后没有process-local runner continuation。settlement释放summary owner及任何旧scope资源，并以不产生successor permit的`clear_after_idle_adoption(exact scope, old epoch identity)`终结旧continuity：owner原本为EMPTY时是exact no-op；为idle resources绑定的INSTALLED old epoch时关闭并回到EMPTY；safe-point已禁止PREPARED，其他identity为implementation conflict且不得误关foreign scope。下一条same-scope turn按§13继承snapshot并使用当时current catalog普通cold-open。

### 12.5 FULL | NONE | CONFLICT confirmation

stateless confirmation只读取canonical rows/event：

- FULL：snapshot、revision与event逐字段等于candidate的derived row drafts；turn pointer等于binding draft ID；predecessor revision等于candidate中的expected row mirror；
- NONE：candidate拟新增的snapshot/revision/event均不存在，turn pointer仍等于expected predecessor且可写；
- CONFLICT：部分存在、ID相同语义不同、pointer被另一revision推进或source drift。

source view、prefix proof、old epoch、MCP borrow/execution identity、dry compile及active/idle process branch不参与FULL比较，因为数据库不持有它们。UNKNOWN不允许直接生成新summary、snapshot ID或boundary。FULL再按当前process-local resource branch结算：active最多安装一次new epoch；idle只确认canonical base winner并清除旧continuity。NONE重写same canonical candidate前必须重新验证同一resource/preconditions仍可用；CONFLICT丢弃，不继续provider。

### 12.6 Host-owned settlement task

summary完成并形成`PreparedCompactionCanonicalAdoption + CompactionSettlementResources`后，Host安装唯一process-local settlement task，拥有write、confirmation、binding pointer settlement，以及branch-specific continuity install/clear。request waiter cancellation只detach waiter；Host close必须drain settlement。

FULL后：

- `ActiveCompactionInstallationResources`且current writer/turn仍exact有效：按successor `FrozenCapabilityExposurePlan`把E1 exact rebind为当前compatible normal tool-surface access E1或E2，再消费同一个install authority并install exact new epoch once；
- `ACTIVE_INSTALLATION`在FULL后已变为COMPLETED/INTERRUPTED：保留historical winner，释放successor access/retained-Skill selection，清除旧scope continuity，不签发/遗留permit且不启动runner；
- `IdleCompactionBaseSettlementResources`：释放process-local summary资源，清除旧scope continuity；永远不调用`HostProviderInputContinuityOwner.install()`；
- writer已stale：旧Host不安装provider epoch，新Host从canonical binding冷读；
- identity mismatch：CONFLICT。

不新增durable receipt、repair task或cross-Host summary owner。

---

## 13. New epoch与跨turn继承

### 13.1 Active turn rebase

adoption FULL后：

1. old continuity slot保留到canonical FULL；
2. compiler看到context base semantic identity变化，产生CONTEXT_BINDING_REWRITE；
3. Host从`ActiveCompactionInstallationResources`读取唯一的successor `FrozenCapabilityPlanningCut/FrozenCapabilityExposurePlan`、effective catalog heads与`RETAINED_SKILL_CONTEXT` selection，并以normal `ProcessLocalToolSurfaceAccess` exact join相容physical bindings；尚不发布为current，也不撤销old refs；
4. exact compile snapshot + post-cut rows + current sources + successor direct/meta tool surface，并重新证明retained Skill eligibility、selection与dry compile一致；
5. normal DirectModel为该exact compiled input和tool-surface borrow完成transport-aware preflight，形成one-shot `PreparedKernelModelExecution`，此时尚未打开provider；
6. Host凭该prepared execution注册incompatible successor candidate并执行continuity CAS；同一Host lock/CAS winner安装successor exposure plan与compiled Skill sources、撤销old refs并签发exact permit；
7. 同一个prepared execution的`open_once()`消费同一个permit并打开provider；
8. 由Round 9/9.1 normal planning比较current owner snapshots与frozen successor facts，后续以compatible suffix发布NEW MCP或Skill catalog successor；
9. 后续同epoch恢复strict prefix，普通Capability refresh只能追加observation，不能修改native tools。

preflight失败发生在continuity install前，不得推进epoch或遗留permit；CAS/install后open失败则按Round 3.1/5A existing physical outcome关闭exact prepared execution，canonical snapshot仍是current binding，下一次dispatch从它cold/read-only重备。

summary call本身不推进normal epoch revision，不写canonical assistant entry，不改变source heads。

### 13.2 新ROOT turn必须继承latest ROOT snapshot

当前所有new-turn producer必须走共享ContextBaseInheritanceFact：

| producer | inheritance |
|---|---|
| human SUBMIT_PROMPT | previous exact ROOT turn current SNAPSHOT，否则FULL_HISTORY |
| queued NEW_TURN | 同上 |
| idle Terminal observation | 同上 |
| Plan runtime continuation creating successor turn | 同上 |
| external/subagent result creating ROOT turn | 同上 |

新turn revision-0可以直接为SNAPSHOT并引用同一context_snapshot_id/source floor；不复制snapshot row。它仍必须保存自己的revision ID、turn ID与revision ordinal 0。

previous ROOT turn为FULL_HISTORY时保持现有行为。不得从另一个session、workspace或SUBAGENT_TASK scope继承。

### 13.3 SUBAGENT_TASK scope

一个active child可mid-turn compact并继续same child scope。若未来同一task允许多个successor turn，只能继承exact scope_subagent_task_id的latest snapshot；新child不得继承ROOT snapshot或另一个child snapshot。

本轮flat subagent当前通常只有一个turn，因此不新增child-to-parent context copy或hierarchical graph语义。

### 13.4 Repeated compaction

第二次compaction的summary prefix自然包含当前snapshot handoff与自其source cut之后的history。新snapshot：

- source boundary单调前进；
- summary继承旧summary仍相关语义；
- recent user quotes重新从当前boundary选择；
- protected tail重新选择最新最多3 groups；
- current binding指向新snapshot；
- old snapshot保持immutable。

不保存window number、previous snapshot chain或replacement history。历史binding revisions已经提供充分的审计链。

---

## 14. Concurrency、cancel、close与failure matrix

### 14.1 Summary operation owner

CompactionSummaryAttempt是Host-owned process-local owner：

~~~text
PREPARING
-> STREAMING
-> REPAIRING?       at most once
-> VALIDATED
-> ADOPTION_PREPARED
-> SETTLING
-> ACTIVE_EPOCH_INSTALLED | IDLE_BASE_ADOPTED | DISCARDED
-> CLOSED
~~~

attempt持有Host-wide summary execution-lane token与一个exact-scope admission fence。前者防止同一Host并行占用多个长时provider summary stream，后者只保护target source cut；两者在DISCARDED/INSTALLED/CLOSED路径确定释放，waiter cancellation只能detach。

planning使用一个120秒absolute deadline，贯穿cut read、tail trials、prefix proof、Round 5A.2 selected hydration/decode与final wire quote。selected hydration只能开启新的read-only transaction，不能调用deadline factory或重新获得120秒；其physical deadline不得晚于该compaction attempt已有planning deadline。summary stream使用Round 5A typed connect/write/pool/read-idle policy，另有20分钟reasoning-runaway backstop；持续健康输出仍必须在20分钟内完成本次compaction。canonical write/confirmation各取fresh foreground canonical deadline。

### 14.2 User stop

user stop发生在：

- summary/adoption前：cancel/drain summary attempt，释放fence，旧turn按existing user-stop contract结算；
- adoption settlement已开始：先drain exact settlement；FULL则current binding保留，再按user-stop终结turn；NONE则旧binding保持；
- new epoch provider open后：走普通runner cancellation。

不能因为stop而留下snapshot row但pointer未推进。

### 14.3 Host close/takeover

Host close：

- 停止新compaction admission；
- cancel/drain active summary transport；
- drain adoption settlement；
- 若未FULL，不承诺跨Host恢复summary；
- 若已FULL，canonical snapshot可由replacement Host读取；
- close Terminal/subagent owners按现有顺序执行。

Host takeover不会接管旧Host的process-local summary、prefix proof或deferred user draft。它只读取canonical current binding。

### 14.4 Failure matrix

| failure | canonical effect | runner effect |
|---|---|---|
| below auto threshold | none | normal dispatch |
| manual no compactable prefix | none | typed NOT_NEEDED |
| unsafe/open tool batch | none | defer until safe point |
| prefix proof mismatch | none | discard, old epoch remains |
| summary connect/read-idle/wall timeout | none | old epoch remains; auto failure count+1 |
| summary returns tool call once | none | ephemeral denial + one repair |
| summary returns tool call twice | none | discard |
| malformed/truncated/oversized summary | none | discard |
| MCP late-ready/semantic replacement before summary opens | none | old summary tools不变；catalog observation在exact prefix/tail中，successor另行freeze |
| MCP late-ready/semantic replacement while summary streams、active successor freeze前 | none yet | summary继续old tools；active完成后freeze最新READY_CLEAN exposure并dry compile；idle不freeze |
| 新identity在successor access/dry compile后完成discovery | none yet | supervisor继续拥有current truth；active FULL后按current-minus-frozen差集成为NEW suffix，不改candidate |
| frozen successor DIRECT发生same-identity semantic replacement | none yet | old descriptor按frozen plan安装并stale；replacement报告pending cold adoption，不签发meta ref、不改candidate |
| MCP仅same-schema physical reconnect | none | semantic exposure不变；允许重借current exact slot |
| other current source/non-MCP tool surface changes during summary | none yet | refreeze and dry compile current facts |
| canonical source head drifts despite fence | none | conflict, discard summary |
| dry compile over target | none | try fewer retained groups if summary not yet called; after output, discard |
| snapshot blob publication fails | none | old epoch remains |
| commit ACK unknown | possible FULL | exact-confirm only canonical row drafts/event；不查询prefix/MCP/dry-compile resources |
| canonical FULL后active physical identity E1已被same-schema E2替换 | snapshot remains canonical | 按frozen semantic surface exact rebind E2；canonical candidate不变 |
| canonical FULL后process-local install resources丢失 | snapshot remains canonical | 不伪造FULL resource confirmation；当前/替代Host从binding cold read |
| active FULL, epoch install fails | snapshot remains canonical | provider open=0；释放borrow且不得遗留permit；same Host可重新cold read，replacement Host也可cold read |
| idle FULL | snapshot remains canonical | clear old scope continuity；无successor borrow/permit/runner，下一turn cold read |
| token ratio低但item/post-base bytes/epoch bytes接近hard bound | none before trigger | resource-headroom OR trigger提前compact，不等第4,097项或hard byte rejection |
| active successor完整NEW cohort超过native surface bound | none | compaction继续；compatible old DIRECT保留，NEW全部继续META_ONLY，不按完成顺序partial |
| new Host has no old Terminal state | none | handoff CLEARED invalidates stale summary claim |
| deferred client input exists | none until fence release | submit only after success/failure |
| Round 9 ordinary direct/meta gate变化 | none | 由Round 9 retained contract结算；不得由compaction package重实现 |

Auto compaction failure不应立即interrupt一个仍能完成当前provider call的turn；如果old input已经无法满足hard provider budget且compaction也失败，使用typed CONTEXT_COMPACTION_FAILED_RESOURCE_BOUNDARY终结，不reactive调用provider。

---

## 15. Protocol、Host API与最小TUI语义

### 15.1 Manual command

Protocol新增COMPACT_CONTEXT command，request至少包含：

~~~text
request_id
attachment identity
command_id
force: bool
expected_active_turn_id: optional
~~~

response closed disposition：

~~~text
COMPACTED
NOT_NEEDED
DEFERRED_TO_SAFE_POINT
REJECTED_BUSY
FAILED
~~~

manual command semantic digest覆盖force、target scope与expected turn。duplicate command必须exact-compatible；不能把同一command ID从idle compact复用到active turn。

session_commands可增加COMPACT_CONTEXT，target仍用TURN；stable snapshot/revision/event ID由command ID派生。Auto/mid-turn不写session_commands。

### 15.2 Live control

process-local LiveSnapshotProjection增加：

~~~text
compaction_in_progress
compaction_trigger
compaction_phase
compaction_target_scope
input_admission_deferred
~~~

不新增LiveEventType。字段只用于当前Host UX，disconnect/reconnect后从Host owner重建；不得写PostgreSQL。

### 15.3 Go/TUI最小闭环

Round 5B activation至少要求：

- 能发出manual compact；
- 显示compaction进行中/完成/失败的bounded status；
- composer目标为正在compact的exact scope时可编辑但不发wire；无关scope正常提交；
- fence释放后提交local deferred input；
- 不把deferred input显示成已accepted steer；
- publictext.Transform处理summary/status preview；
- 不实现snapshot diff、artifact inventory或advanced dashboard。

如果Python activation先于Go polished UI，Gap Index必须明确“headless/Protocol manual入口已闭合，完整TUI UX待补”，不能宣传用户已获得无感deferred composer。

---

## 16. 删除最后一套durable job machinery

### 16.1 为什么必须删除

当前唯一durable job handler BACKGROUND_COMPACTION：

- 使用独立bounded job model，而本轮要求当前主模型与old prefix；
- 无法持有当前Host live state、continuity epoch与safe-point fence；
- 会让compaction变成eventual background mutation，而产品要求same-turn continuation；
- 需要JobAttemptClaimGuard、job attempts、job result delivery与三种job events；
- production未调用。

保留它只会制造第二套compaction authority。Round 5B不得“暂时不使用但留着以后”，而应从clean-v0宇宙删除。

### 16.2 删除面

- durable_jobs relation；
- durable_job_attempts relation；
- BACKGROUND_COMPACTION handler catalog；
- JobAttemptClaimGuard；
- JobQueued、JobAttemptAccepted、JobTerminalAccepted；
- subject_job_id、subject_job_attempt_id；
- ACCEPT_JOB_RESULT command/Protocol path；
- JOB_RESULT canonical input origin与reader branch；
- JobControl canonical view；
- job executor、job model wrapper与repository job operations；
- blob GC对job result blob的引用检查；
- Host job close/worker wiring；
- 对应fixtures、grants、catalog与tests。

purpose-neutral AuxiliaryJsonModelPort仍由Round 8 governor使用，不得随job wrapper删除。

这是clean-v0 hard reset，不提供旧job row在线迁移、claim接管或历史Protocol兼容层。实施前必须用现有old-universe rejection证明旧schema会被明确拒绝，并仅对解析、校验过的本地测试数据库执行reset。

### 16.3 最终oracle

从Round 8加已激活Lightweight TODO refinement、再加Round 5A.2唯一provider replay relation的oracle，减去唯一job family：

~~~text
Committed events       31 - 3 = 28
Live events            24
Subject slots          13 - 2 = 11
Append guards           2 - 1 = 1
Product relations      26 - 2 = 24
Durable jobs            1 - 1 = 0
~~~

CompactionAdopted、context_snapshots与turn_context_binding_revisions已经在剩余oracle内，不新增计数。

---

## 17. Repository与reader修改面

### 17.1 adopt_context_snapshot()

现有source_through_sequence < initial_entry_sequence限制必须删除，改为closed target matrix：

- RUNNING active：current_source_cut <= boundary <= exact safe head；
- terminal idle manual：same predicate，且target为latest terminal same-scope turn；
- boundary必须位于complete group边界；
- CompactionSourceLineageBase、bounded post-base source digest、head、scope、current revision、event draft exact join；
- source boundary必须单调不回退；
- new snapshot不能引用另workspace/session blob。

### 17.2 One-cut reader

新增compaction planning read在一个REPEATABLE READ transaction冻结：

- current binding/turn/scope/status；
- current binding及其FULL_HISTORY_GENESIS | CURRENT_SNAPSHOT lineage base；
- exact canonical entries in `(current effective materialization floor, safe head]`，不得在重复compaction时从genesis重扫；
- assistant blocks/tool pairing/result visibility；
- complete tool group identities；
- bounded cumulative source digest；
- recent real human candidates；
- current Plan/permission facts需要的canonical cut。

process-local Terminal/TODO/MCP/memory不假装属于该RR transaction；active branch在summary后另行freeze并进入dry compile fingerprint，idle branch留给下一turn cold compile。

reader必须另有bounded count/byte preflight，使用exact scope sequence index计算post-base item count与canonical UTF-8 bytes，供§4.2 resource-headroom trigger使用。返回值有界不等于允许数据库全历史scan；SQL/EXPLAIN gate必须证明重复compaction按effective floor进行range scan。

### 17.3 Cross-turn context base factory

所有new-turn repository paths调用一个共享pure selection + transaction verification helper。不得让user prompt继承snapshot而Terminal observation/Plan successor偷偷回到FULL_HISTORY。

revision-0 invariant改为closed union：

~~~text
FULL_HISTORY
    snapshot_id = null
    persisted_revision_genesis_marker = initial_entry_sequence - 1
    effective_materialization_lineage_floor = 0

SNAPSHOT
    snapshot_id != null
    effective_materialization_lineage_floor = snapshot.source_through_sequence
    inherited from latest exact-scope predecessor binding
~~~

reader、compiler、compaction range query和source digest只能使用`effective_materialization_lineage_floor`。`persisted_revision_genesis_marker`只维持既有revision-0数据库不变量，绝不能被解释为“此前canonical history已经被summary覆盖”。

### 17.4 Snapshot reader fail closed

snapshot blob丢失、digest/size/media/codec不符或carrier JSON不合法时provider open=0。不得silent fallback full history，因为那可能重新越过context budget，也会改变已accepted binding语义。canonical inspector仍可读完整transcript诊断。

---

## 18. Module与依赖方向

建议新增：

~~~text
src/pulsara_agent/conversation_kernel/compaction/
    contracts.py       pure enums/DTO/fingerprints
    planner.py         one-cut groups/boundary/recent-user selection
    prompt.py          summary request/output validation/provider carrier
    model_call.py      PreparedCompactionSummaryCall preflight/open_once
    runtime_handoff.py Host-local live-state projection
    retained_skills.py pure ordinary-read/FULL/current-manifest eligibility + rendering
    service.py         process-local attempt orchestration
~~~

依赖方向：

~~~text
primitives/model_input
    <- compaction contracts/planner/prompt
    <- conversation_kernel compaction service
    <- Host/runner orchestration

repository/reader
    <- canonical rows only

terminal/todo/subagent
    -> bounded read-only views
    -> runtime_handoff collector

Round 9 capability owners/planner
    -> ordinary FrozenCapabilityPlanningCut / FrozenCapabilityExposurePlan
    -> compaction successor-selection seam
    -> ordinary tool-surface access and compiler

Round 9.1 manifest/read contracts + old installed epoch view
    -> retained_skills pure eligibility/selection
    -> RETAINED_SKILL_CONTEXT compiler source
~~~

禁止：

- pure planner import Host、repository、provider transport或Terminal manager；
- repository调用summarizer/compiler；
- compiler查询PostgreSQL；
- summary model获得ToolRuntime callback；
- summary call复用normal AGENT_MODEL_LOOP continuity/executor owner；
- context snapshot保存Python object、borrow、permit或transport；
- runtime handoff source写canonical row。
- compaction package重新实现MCP meta dispatcher、catalog/list/inspect/use或direct unavailable gate；
- late-ready callback直接修改已安装`FrozenModelToolSurface`；
- compaction package定义第二套capability registry/planning cut/exposure DTO或physical borrow。

---

## 19. 实施切片

Round 5A.2、Round 7.1、Round 9、Round 9.1与Lightweight TODO refinement必须先各自标记ACTIVATED并具备机器证据；它们不是`R5B-*` slice。Round 5B M0只记录五者的activation hash、durable replay/ordinary ToolResult/Capability/Skill/TODO retained node IDs与public DTO manifest，禁止在本轮重新实现或补丁式完成其production路径。

### R5B-1：减掉dormant job universe并重算oracle

- 删除BACKGROUND_COMPACTION及job tables/events/guard/Protocol；
- 保留purpose-neutral auxiliary JSON model；
- 更新clean-v0 manifest、grants、expected catalog与architecture guards；
- oracle冻结28/24/11/1/24/0。

### R5B-A：Pure contracts与one-cut planner

- FrozenCompactionSourceView组合Round 5A.2 semantic + metadata-only manifest cut的只读物化与三态compatibility；
- FULL_HISTORY persisted marker/effective floor分离；
- 复用`FrozenProviderInputItem`/closure/late-outcome carriers的shared `FrozenCompactionCanonicalRange` envelope与bounded lineage digest；
- complete tool group parser；
- 3→2→1→0 longest suffix；
- recent human quote selection；
- bounded lineage source digest与ProviderPrefixCutProof；
- policy validation、token-ratio与resource-headroom OR trigger。

### R5B-B：Summary execution

- `PreparedCompactionSummaryCall` one-shot seam；
- active target exact reuse与idle purpose-neutral one-shot resolution；
- main model exact SYSTEM/semantic-only tools/semantic prefix；
- 对exact summary slice复用Round 5A.2 selected hydration与`FrozenProviderWireInputPlan`，发送native actual prefix；
- native replacement、summary actual wire plan与one-shot open exact join同一个cut/scope/target/placement-bound hydration fingerprint；零replacement不构造empty hydration；
- purpose gate与tool_choice none；
- one repair ephemeral denial；
- prompt/output validator；
- no canonical side effect。

### R5B-C：Snapshot adoption与inheritance

- PreparedCompactionCanonicalAdoption row drafts与CompactionSettlementResources物理分层；
- ACTIVE_INSTALLATION / IDLE_BASE_ONLY canonical transaction与settlement；
- FULL/NONE/CONFLICT exact confirmation；
- new ROOT/context-base inheritance；
- reader snapshot validation。

### R5B-D：Compiler rebase与Runtime handoff

- retained ToolResult复用Round 7.1 ordinary pure builder，并在相同call-local augmentation输入下验证byte-identical；successor epoch citation mapping作为显式输入，不迁移旧opaque handle；
- COMPACTION_RUNTIME_HANDOFF source；
- 整个handoff使用`UNTRUSTED_OBSERVATION`，Runtime只证明结构/identity/status/currentness；
- `RETAINED_SKILL_CONTEXT`从old installed FULL ordinary reads与current manifest纯派生，最多8项；
- Terminal/monitor/TODO/flat subagent snapshots；TODO只使用ordinal/status/text与counts，不伪造item ID；
- dry compile；
- successor Round 9 planning cut/exposure plan refreeze与all-or-none NEW promotion；
- active resources唯一组合successor exposure plan/effective catalog heads/retained Skill selection；physical layer直接复用normal `ProcessLocalToolSurfaceAccess`，期间的新semantic facts仍由既有owners唯一拥有；
- promotion后旧new-tool refs撤销，catalog DIRECT/NEW reclassification继续调用Round 9普通renderer；
- CONTEXT_BINDING_REWRITE epoch swap；
- post-adoption strict-prefix proof。

### R5B-E：Host/runner triggers与input fence

- manual、auto、mid-turn entry；
- safe-point integration；
- no reactive provider retry；
- Host-wide单一summary execution lane、exact-scope admission fence；
- lane-first、recapture、fence-second且await不持Host lock；
- Host-owned settlement/cancel/close；
- deferred input live control。

### R5B-F：Protocol/minimal Go/activation evidence

- COMPACT_CONTEXT；
- live status/deferred composer；
- docs/Gap Index/README；
- real-provider dogfood与machine evidence。

每个slice必须保持clean-v0可安装；不得先合入写了snapshot却没有reader或inheritance的中间production路径。

---

## 20. 测试矩阵

### 20.1 前置Round retained与successor Capability integration

- Round 5A.2、Round 7.1、Round 9、Round 9.1与Lightweight TODO refinement activation evidence hash与public DTO manifest exact匹配；
- Round 5A.2 metadata-only manifest read、selected hydration、replay-target gate与restart suites原样retained；Round 5B不定义第二套replay DTO/reader；
- Round 9 normal cold/direct/meta/catalog/list/inspect/use/unavailable/schema-replacement suites原样retained，Round 5B不新增同义unit suite；
- Round 9.1 normal Skill add/change/remove、textual/configured activation与ordinary read suites原样retained；
- summary request始终使用old installed `CapabilityEpochPredecessor`的exact native tools与old catalog message prefix；
- active successor使用current complete registry形成标准`FrozenCapabilityPlanningCut/FrozenCapabilityExposurePlan`；
- fit的完整NEW cohort整体promotion；overbound时旧compatible DIRECT保留、NEW全部继续meta，不partial ranking且compaction可成功；
- old DIRECT schema replacement只在successor boundary采用，不在old epoch以meta双版本绕过；
- successor compile把effective Skill catalog/active heads与`RETAINED_SKILL_CONTEXT`一起纳入continuity CAS；
- freeze后到达的新MCP/Skill facts不改candidate，安装后走Round 9/9.1 normal compatible suffix；
- no `FrozenEpochMcpExposure`、`McpEpochExposureBorrow`、compaction meta dispatcher或second capability registry exists。

### 20.2 Pure planner golden

- prior epoch之后又接纳USER_MESSAGE/USER_STEER/tool result时，source view覆盖到exact safe head；
- EMPTY_COLD、COMPATIBLE_APPEND与PENDING_NON_COMPACTION_RESET三态；
- source view组合Round 5A.2 `FrozenCanonicalProviderDispatchRead`、`ModelInputCompileBinding`、optional predecessor epoch view与最小`FrozenCompactionProviderProjection`；dispatch read唯一组合canonical compile snapshot与同RR metadata-only replay manifest，source view不hydrate body；compatible projection只存append suffix，cold/reset projection才存完整SYSTEM/messages，且不存在第二份tools/epoch/frontier/source-head/estimate；current safe head从canonical compile cut派生，predecessor frontier只用于prefix proof；
- source view超过effective provider budget时仍能形成合法projection/estimate而不能伪造`FrozenCompiledModelInput`；summary slice必须单独回到budget内；
- `FrozenModelToolSurface`是old provider tool surface唯一owner；source view只组合Round 9 `CapabilityEpochPredecessor`，不复制MCP exposure/spec tuple；
- source view whole-budget可超soft input limit但不越physical working-set，实际summary slice必须可open；
- 0/1/2/3/4+ tool groups；
- parallel 3-call batch保持原子；
- group间夹user/assistant/Terminal entries；
- late result/closure保持provider-valid；
- latest group太大时降到2/1/0，不拆pair；
- boundary与exact scope/global sequence正确；
- 至少三个普通turn后的首次FULL_HISTORY compaction使用effective floor=0，source view/digest覆盖initial marker之前的既有same-scope entries；revision genesis marker仍保持initial sequence - 1但不进入range digest；
- recent 3条human按时间顺序；
- synthetic/Plan/Terminal/subagent input不进入recent human；
- latest human超64 KiB时不保留更老quote；
- first compaction从FULL_HISTORY effective floor=0计算lineage digest；repeated compaction只读取prior snapshot floor之后的bounded delta；
- lineage digest对post-base row drift、prior snapshot/base binding漂移敏感；
- reader与repository hydrate同一`FrozenCompactionCanonicalRange` envelope并复用normal provider-item/closure/late-outcome leaf fingerprints产生逐字相同digest；ordered blocks、artifact/timing/closure及cut内late result任一语义变化都会改变digest，cut后late result不会提前改变；
- retained result以successor epoch自己的memory citation snapshot重建call-local augmentation；同augmentation下variants逐字相等，不同epoch旧`tool:N`不会迁移；
- retained group包含Round 7.1 `FULL_REQUIRED` result时，该result不得降级；候选整体不fit则按3→2→1→0移除整个group而不拆pair；
- `ProviderPrefixCutProof`只保存source-view fingerprint、prefix count/fingerprint、source boundary和protected-tail-selection fingerprint；retained group/tool-call anchors只存在于`ProtectedTailSelectionFact`；
- 大量短entry在4096-item hard stop前由resource headroom触发；16 MiB reader与64 MiB epoch边界各保留一次maximum legal admission quote。

### 20.3 Summary request cache proof

Chat Completions与Responses分别证明：

~~~text
summary SYSTEM == exact source-view materialized_system_prompt()
summary tools == exact source-view normal_compile_binding.tool_surface.tool_specs
summary semantic messages[:-1] == exact source-view materialized_messages() prefix
summary semantic last message == synthetic summary request
summary actual input == exact FrozenProviderWireInputPlan materialization
~~~

- COMPATIBLE_APPEND时，与prior epoch重叠的SYSTEM/tools/messages逐字相等；
- actual input只hydrate semantic prefix中selected + replay-compatible assistant bodies；Responses reasoning/message/function_call与Chat closed fields保持native shape；
- hydration exact绑定session、scope、source manifest cut、summary replay target与selected assistant placements；cross-session/child/cut carrier拒绝；
- ordinary AGENT call与same endpoint/model/semantic transport binding/codec/replay contract的summary call得到相同replay target fingerprint；purpose与`tool_choice=none`不参与compatibility；
- cut read、tail trials、selected hydration与wire quote共享一个compaction-planning absolute deadline，hydration transaction不重置预算；
- summary native replacements iff final wire plan携带本次selected hydration fingerprint；cross-session/scope/cut/tail proof及CAS失败后的hydration不可复用；
- COMPATIBLE_APPEND actual wire overlap与predecessor epoch wire plan逐项、逐字相等；
- idle restart没有predecessor epoch时也从Round 5A.2 durable manifest/body重建native summary prefix，不退化为generic semantic messages；
- target incompatible时按Round 5A.2显式cold semantic continuation，不跨Chat/Responses翻译；
- canonical suffix在prior epoch之后、summary freeze之前提交时，summary prefix或retained tail必须exact覆盖，不得提前推进source cut；
- EMPTY_COLD/PENDING_NON_COMPACTION_RESET语义正确但不伪造remote-cache命中承诺；
- current MCP physical reconnect但semantic schema相同不改变summary prefix；
- current epoch NEW MCP不进入summary tools；其catalog observation若位于exact summary prefix则保持原位置；
- `PreparedCompactionSummaryCall`不含continuity candidate/install permit、executor borrow或tool callback，`open_once()`只能消费同一prepared object；
- `PreparedCompactionSummaryCall` constructor不接受purpose/tool-suppression参数；sealed type恒定编码summary purpose与wire `tool_choice=none`，二者仍进入request fingerprint domain；
- active summary复用upcoming dispatch exact target identity；idle summary独立resolve一次current Host primary target且不创建normal candidate/runner/continuity state；
- summary不取得executor borrow；
- tool_choice none进入真实adapter wire；
- provider tool-call违规只产生ephemeral group，数据库attempt/result/event count不变；
- second tool-call repair失败。

### 20.4 Output/carrier hygiene

- unique summary block；
- analysis stripping；
- missing/duplicate heading；
- truncated closing tag；
- 64 KiB bound；
- malicious closing marker/JSON characters；
- snapshot wire不含contract/fingerprint/generation/UUID/sequence；
- snapshot wire exact keys只有earlier_context_summary与ordered recent_user_messages；
- recent user text保持exact UTF-8。

### 20.5 PostgreSQL adoption

- active same-turn source cut可晚于initial entry；
- idle latest completed turn manual adoption；
- active FULL只消费一次successor install authority；idle FULL不调用continuity install、不产生permit且清除旧scope continuity；
- canonical candidate row drafts不含source view/prefix proof/MCP/epoch/execution/dry-compile identity；ACK FULL在丢弃全部process-local resources后仍可仅凭rows/event确认；
- sealed canonical factory只接收一次source materialization identity，并派生snapshot/binding row mirrors、next ordinal、turn pointer expected value和event type/subject/payload；无法传入互相矛盾的duplicate values；
- `FrozenCompiledModelInput.final_estimate`是active dry compile唯一estimate；不存在并列target estimate；
- same-schema reconnect E1 -> E2不改变canonical candidate fingerprint，active FULL可按frozen semantic surface rebind E2；
- same-schema reconnect只替换normal tool-surface access中的compatible physical binding；successor planning cut/exposure plan与effective catalog heads在active resources中恰好一份；
- snapshot/revision/event/pointer原子；
- ACK unknown FULL/NONE/CONFLICT；
- expected lineage base/prior digest mismatch为CONFLICT，不能用相同delta嫁接到另一snapshot winner；
- source head drift；
- wrong scope/workspace/session；
- incomplete tool group；
- two concurrent candidates one winner；
- blob publication failure无row；
- snapshot corruption reader fail closed；
- no orphan snapshot。

### 20.6 Cross-turn inheritance

- next human ROOT继承snapshot；
- queued NEW_TURN、Terminal observation、Plan successor、external result一致继承；
- FULL_HISTORY predecessor保持full；
- ROOT不继承child；child不继承ROOT/other child；
- repeated compaction source cut单调；
- repeated compaction query/EXPLAIN使用source-floor range，不从genesis重扫；
- reopen从current binding只看到exact one summary。

### 20.7 Runtime handoff

- running Terminal IDs与monitor IDs可见，无output；
- process在summary期间完成，dry compile看到最终状态；
- new Host无physical owner时追加CLEARED；
- TODO只active items；
- flat subagent bounded；
- MCP/skill/permission/Plan/memory不重复进handoff；
- promoted MCP进入successor native tools而非SYSTEM/handoff正文；仍NEW的工具只由MCP_CATALOG说明；
- FULL到COMPACT deterministic；TODO按ordered whole-item prefix降级并保留exact counts/omitted，不存在item ID或text-free TODO表示；
- 32 KiB hard bound。

### 20.8 Retained Skill context

- ordinary `read_file`从offset 1一次到EOF、canonical COMPLETE、actual FULL并已continuity install时，同run active compaction可重注入current exact parsed body；
- physical read完成但row未提交、row已提交但provider未open、HEAD_TAIL、COMPACT、REF_ONLY、OMITTED与partial page均不eligible；
- textual/configured `ACTIVE_SKILL`不在retained source重复；
- protected tail已经为同一read result选择FULL时不重复；tail只保留COMPACT/REF_ONLY时可由retained source补回exact parsed body；
- 相同Skill重复FULL read只保留一份并更新recentness；多个Skill按recent prefix选择、按原delivery顺序渲染；
- 9项时只保留最近8项；40,000-token aggregate与overall post-target任一命中时整项缩减，不截断正文；
- current manifest修改、删除、invalid、same-name winner变化时不重注入旧body，successor catalog仍正常可见；
- next real ROOT user message清空；exact child run与ROOT/other child严格隔离；
- idle compaction不生成retained source；
- repeated mid-turn compaction从current installed epoch重新纯派生，不建立loaded ledger；
- provider body只有name/location/body/omitted_count，不含read/result IDs、digest、epoch或permission；
- BASE_SYSTEM/tools不变，retained source只在new epoch messages中出现。

### 20.9 Trigger/fence

- 84.9%不auto、85%auto；
- token ratio低但item/post-base canonical bytes/epoch logical bytes越derived soft boundary时auto；最大合法下一admission仍可在hard bound内被物化；
- manual force低threshold；
- cumulative million tokens但active低不compact；
- mid-turn完整tool group后compact并继续；
- active provider/open interaction时defer；
- compaction中steer/new-turn/monitor installation不进入source cut；
- child compaction期间ROOT与其他child可继续；只有会改变exact target source head/binding的external result installation被延后；
- scope B在scope A持有global lane时不会提前安装fence；取得lane后必须recapture B target，所有DB/provider await均证明未持Host lock；
- TUI local input不发wire，success/failure后再正常提交；
- no provider context-error reactive retry；
- 3次auto failure circuit；
- one compaction per dispatch。

### 20.10 Round 7.1 ToolResult retained integration

- Round 7.1 boundary、UTF-8、artifact、conditional guidance与all-origin suites原样retained；
- 同一ToolResult在相同lowering contract与call-local augmentation输入下，ordinary call与post-compaction retained tail的ordered variants/provider bytes完全相等；successor epoch自己的citation mapping可使opaque handle合法不同，旧handle不得迁移；
- parallel batch保持provider-valid，不引入compaction专用公平配额；
- 不输出artifact inventory；artifact_read仍只按visible handle工作；
- 3-group aggregate quote不越target；不fit时只减少完整group或选择Round 7.1允许的normal degraded variant；`FULL_REQUIRED` group只能完整保留或整体移除。

### 20.11 Continuity retained regression

- summary call复用old prefix；
- summary source view不注册normal continuity candidate，也不推进epoch；
- adoption前old epoch完全不变；
- adoption后产生CONTEXT_BINDING_REWRITE；
- new epoch第一次call与dry compile一致；
- new epoch后相邻calls继续SYSTEM/tools相等、messages suffix-only；
- Round 7 timing/freshness从canonical rows重新物化；
- Round 8 preference head/recall从current memory重新物化；
- Round 6 same-schema reconnect不单独rebase；
- late-ready/new schema不单独rebase；
- compaction successor将fit的current READY_CLEAN NEW整体promote为DIRECT，旧refs失效；
- successor promotion全量越native bound时不partial选取，NEW集合继续meta且compaction可成功；
- successor exposure plan/normal physical access冻结后完成的新discovery不作废adoption；各source owner仍唯一拥有current truth，安装后按current-minus-frozen差集成为NEW append；
- Plan one-cut与permission snapshot仍exact。

### 20.12 Job subtraction

- schema不存在durable_jobs/durable_job_attempts；
- vocabulary无Job events/subjects/guard；
- Protocol无ACCEPT_JOB_RESULT/JobControl；
- source tree无BACKGROUND_COMPACTION production binding；
- blob GC仍正确保护snapshot/tool-result/memory引用；
- auxiliary governor model保留；
- oracle exact 28/24/11/1/24/0。

---

## 21. Real-provider dogfood

Activation必须在ephemeral clean-v0 PostgreSQL完成至少四条：

### 21.1 Hybrid MCP promotion

Round 9必须已经独立证明cold DIRECT、late NEW、inspect/use、disconnect gate与same-schema reconnect。Round 5B dogfood不重跑整套normal MCP acceptance，只验证rebase集成：

1. 从一个已安装epoch开始，其中至少一个MCP tool为仍compatible的DIRECT，另一个current READY_CLEAN tool为NEW；
2. 在compaction前证明NEW tool仍通过Round 9 meta route可用，并冻结该事实的bounded sentinel；
3. 触发active compaction，summary call继续发送old epoch exact native tools；
4. successor planning保留compatible old DIRECT，并在完整NEW cohort fit时把该cohort整体提升为DIRECT；
5. successor直接调用promoted tool exact一次，old-epoch `NewMcpToolRef` typed stale；
6. 另以over-bound cohort证明compaction仍成功、旧compatible DIRECT不降级、NEW cohort全部继续meta且没有partial winner；
7. 全程不记录MCP private config、headers、requestState、完整schema参数或result正文。

### 21.2 Mid-turn long task

1. 使用真实provider与当前主模型；
2. 构造多个完整tool group，其中至少一个已经由Round 7.1 normal pipeline形成HEAD_TAIL或COMPACT且拥有artifact；
3. active context达到threshold；
4. 发生summary call，tools schema保持相同且physical tool count不增加；
5. adoption FULL；
6. successor看到summary、最近用户原话、最多3组tail与current handoff；
7. successor继续使用已有artifact或Terminal ID完成目标；
8. 最终assistant回答正确。

### 21.3 Repeated/cross-turn

1. 完成第一次compact并结束turn；
2. 新ROOT turn继承snapshot；
3. 继续到第二次compact；
4. current binding只选择第二个snapshot；
5. 当前用户纠正覆盖旧summary；
6. cache metrics仅作operational evidence，不作为correctness gate。

### 21.4 Ordinary Skill retained context

1. provider根据`SKILL_CATALOG`用ordinary `read_file(offset=1, limit=2000)`完整读取一个current Skill；
2. 证明canonical preview为COMPLETE、old epoch actual representation为FULL且continuity已安装；
3. 同一ROOT user run触发active compaction，summary prompt不复制Skill body；
4. successor以`RETAINED_SKILL_CONTEXT`只重注入该parsed body一次，SYSTEM/tools保持目标new epoch的标准形状；
5. provider继续遵循该Skill完成任务，不再为恢复上下文重复读取；
6. 改变manifest后重复场景，旧body不被retained、current catalog仍可用；
7. 下一真实ROOT user message不继承model-driven retained body。

证据只能记录：模型/adapter类型、trigger、source/target estimates、selected group count、summary bytes、snapshot/revision fingerprints、tool call count、test sentinel与cache token aggregate。不得记录API key、DSN、完整prompt、user正文、summary正文、tool output、artifact正文、env或private paths。

---

## 22. Architecture guards

必须机器拒绝：

1. 新增CompactionStarted/Failed/summary event；
2. 新增replacement_history或retained manifest relation/column；
3. summary call创建ToolExecutionAttempt/ToolResult；
4. summary call偏离FrozenCompactionSourceView的SYSTEM/tool schema，或COMPATIBLE_APPEND时改写prior epoch重叠prefix；
5. provider context error触发compact retry；
6. snapshot carrier包含contract/fingerprint/generation/UUID/sequence；
7. runtime handoff包含raw Terminal output或secret；
8. current tool/permission/MCP/memory被写进summary SYSTEM；
9. source boundary拆开parallel tool group；
10. adoption前continuity epoch被reset；
11. snapshot commit与binding pointer分事务；
12. new ROOT producer绕过snapshot inheritance factory；
13. durable_jobs/job attempts/job events/claim guard残留；
14. compaction调用memory extraction、governance或rerank；
15. compaction期间user input成为steer或summary source；
16. current-source drift后仍使用旧dry compile，或Capability owner变化原地改写已冻结successor planning/exposure facts；
17. repeated compaction并列注入多个summary；
18. summary/raw provider output写入operational log；
19. compaction定义任何ToolResult threshold、variant、artifact renderer、artifact inventory或第二个40,000-byte constant，而不是只消费Round 7.1 normal projection；
20. Round 5B重新实现Round 9 normal MCP catalog/list/inspect/use/unavailable/schema-replacement逻辑，或重新实现Round 9.1 Skill discovery/ordinary read；
21. compaction summary使用successor promoted tools而不是old epoch exact tools；
22. successor MCP promotion按ranking、discovery timing或偶然completion order做partial selection，或把server instructions/catalog写入SYSTEM；
23. repository transaction读取process-local Capability owner/physical access，或把source view、prefix proof、dry compile、execution binding、slot lease、retained-Skill proof或catalog callback写入`PreparedCompactionCanonicalAdoption`；
24. compaction定义`McpEpochExposureKind`、`FrozenEpochMcpExposure`、`McpEpochExposureBorrow`或任何同义second registry/exposure/current-generation owner；
25. idle adoption调用continuity install、签发未被provider消费的permit、持有successor planning/access/retained-Skill资源或启动runner；
26. repeated compaction为验证source digest从genesis扫描全部exact-scope rows，而不是验证current lineage base加bounded delta；
27. auto trigger只看token ratio，使reader item/16 MiB或epoch 64 MiB hard bound先于compaction admission命中；
28. active successor NEW cohort越native bound导致compaction失败、无故降级仍compatible的old DIRECT，或产生partial promotion；
29. summary通过normal `AGENT_MODEL_LOOP` execution取得continuity install authority、executor borrow或tool callback；
30. exact child/ROOT compaction fence暂停所有无关scope的canonical/provider工作；
31. canonical candidate fingerprint包含source view、prefix proof、epoch nonce、Capability semantic/physical identity、retained-Skill/tool-tail proof或dry compile等数据库无法逐字段确认的字段；
32. FULL_HISTORY reader/digest把`initial_entry_sequence - 1` revision marker当成effective materialization floor或digest semantic input，而不是只把它作为predecessor-row bookkeeping；
33. reader与repository各自实现canonical range fingerprint，或依赖`jsonb::text`/未framed字符串拼接；
34. idle summary假装复用不存在的normal dispatch target，或为了resolve target创建normal candidate/continuity/tool borrow；
35. exact-scope fence先于global summary lane安装，或任何lane/DB/provider await期间持有Host lock；
36. compaction重新声明assistant block、ToolResult、timing、closure或late-outcome leaf DTO/fingerprint，而不是复用normal provider-input carriers与共享helpers；
37. compatible source view摊平复制`FrozenProviderInputEpochView`的SYSTEM/tools/messages/frontier/source-head，cold/reset source view另存第二份tool surface，projection另存第二份estimate，或者prefix proof再次复制lineage/prior epoch/retained anchors；
38. canonical adoption允许调用者分别传snapshot/binding source boundary、turn pointer winner、next ordinal或event type/subject/payload，或者write preconditions再次传predecessor/lineage proof；
39. normal tool-surface physical access复制完整successor exposure/catalog，或active resources同时保存第二份相同Round 9/9.1 semantic fact；
40. `resource_headroom_trigger_enabled=true`、`maximum_summary_tool_repair_rounds=1`、summary purpose/tool suppression作为可配置实例字段；resolved hard-bound动态值仍必须由唯一`resolved_hard_bound_set_fingerprint`exact join，不能因本guard被删除；
41. retained-equivalence测试要求跨epoch raw citation handle相等、迁移旧`tool:N`，或未把call-local augmentation作为pure builder显式输入；
42. protected tail把Round 7.1 `FULL_REQUIRED` result降级为COMPACT/REF_ONLY/OMITTED，或保留半个不fit的tool group。
43. `RETAINED_SKILL_CONTEXT`因physical read、canonical row存在、HEAD_TAIL/COMPACT/REF_ONLY或partial page而接纳Skill；跨真实ROOT user message继承；重注入已修改/删除manifest；或建立durable loaded-skill ledger。
44. TODO COMPACT只保留ordinal/status、伪造稳定item ID、截断单项text、让counts随prefix缩小，或在一个whole actionable item都无法容纳时仍发送空正文声称current TODO已交接。

---

## 23. 静态与动态验证

至少执行：

~~~bash
uv run pytest -q tests/test_round7_1_provider_visible_tool_result_projection.py
uv run pytest -q tests/test_round9_unified_capability_semantics.py tests/test_round9_1_agent_skills_standard.py
uv run pytest -q tests/test_round6_mcp_production.py
uv run pytest -q tests/test_round5b_long_horizon_context_compaction.py
uv run pytest -q -m postgres tests/test_round5b_long_horizon_context_compaction_postgres.py
uv run pytest -q tests/test_round3_structured_model_input_compiler.py tests/test_round3_1_provider_input_prefix_continuity.py
uv run pytest -q tests/test_round5_long_horizon_execution_envelope.py tests/test_round7_model_visible_failure_and_tool_observation.py
uv run pytest -q tests/test_round8_advisory_memory_subsystem.py
uv run pytest -q
uv run ruff check src tests
uv run python -m compileall -q src tests
uv lock --check
git diff --check
go test ./...
go vet ./...
go mod verify
~~~

实际文件名以coding agent新增node为准；activation evidence必须保存pytest collection前后node-ID集合，不能用“总数相等”替代retained tests。

PostgreSQL验证允许直接使用本机已经安装扩展、专门用于Pulsara开发且可随时reset的真实库：

~~~text
PULSARA_POSTGRES_ADMIN_DSN=postgresql://plumliu@localhost:5432/pulsara
PULSARA_POSTGRES_DSN=postgresql://pulsara:pulsara@localhost:5432/pulsara
~~~

该本地`pulsara`数据库不是需要保留业务数据的环境；coding agent可为Round 5B的clean-v0 fresh install、second migrate、deep verify、PostgreSQL竞态测试和old-universe rejection随时执行完整reset，不必为每次reset再次请求用户确认。但“允许随时reset”只授权上述exact local database：执行任何破坏性操作前仍必须解析最终DSN、证明host为`localhost`或loopback且database name精确等于`pulsara`，并拒绝空database、template database、通配目标、远端host或由未解析变量拼出的目标。不得把该授权泛化到其他DSN、其他数据库或生产环境。

测试日志、activation evidence与dogfood记录不得写入DSN正文、credential或连接环境值；只允许记录经过脱敏的local-resettable database classification及验证结果。

---

## 24. Definition of Done

Round 5B只有在以下全部成立时才能标记ACTIVATED：

- Round 5A.2、Round 7.1、Round 9、Round 9.1与Lightweight TODO refinement已分别ACTIVATED；durable replay、普通ToolResult projection、MCP catalog/list/inspect/use/direct gate、Skill discovery/read与TODO owner/live projection不属于Round 5B production slice；
- summary继续old epoch exact native tools、catalog heads与ordinary Skill ToolResult prefix；summary期间current Capability变化不改变旧request；
- active successor以Round 9 owner inventories、complete registry、planning cut与standard exposure plan重新冻结current capability；compatible old DIRECT保留，完整READY_CLEAN NEW cohort只有在整体fit时才promotion；
- active successor semantic plan、effective MCP/Skill heads与retained Skill selection在`ActiveCompactionInstallationResources`中各出现一次；normal physical access只覆盖相容slot/execution能力，latest MCP generation仍只由supervisor拥有；
- promoted工具在new epoch direct调用，旧meta ref stale；NEW cohort超native bound时compaction仍成功、compatible old DIRECT不降级、NEW全部继续meta且没有partial winner；
- manual、proactive auto与mid-turn safe-point三入口production可达；auto由85% token ratio或可rebase的item/post-base-byte/epoch-byte headroom任一条件触发；
- provider context-error不会reactive compact；
- summary使用当前主模型与exact FrozenCompactionSourceView；COMPATIBLE_APPEND时prior SYSTEM/tools/messages重叠prefix逐字不变；
- source view覆盖safe canonical head且不注册/推进normal continuity candidate；
- summary通过独立`PreparedCompactionSummaryCall`复用Round 5A.2 cut/scope/target/placement-bound selected hydration与`FrozenProviderWireInputPlan`发送旧exact native prefix和semantic tool specs；same endpoint/model/semantic transport binding/codec/replay contract下purpose与wire tool_choice=none不改变replay target fingerprint；summary没有continuity permit、executor borrow或tool callback，`materialized_messages()`不能被直接发送，physical tools绝对不可执行；
- active summary exact复用dispatch target；idle summary由同一purpose-neutral resolver独立冻结current Host primary target，不制造normal dispatch state；
- summary只负责语义，Runtime重建current state；
- protected tail最多3个complete tool groups且pairing-safe；
- 最近最多3条真实human input由Runtime exact保留；
- normal与retained ToolResult逐字复用Round 7.1同一artifact-aware pure builder；byte equality以相同canonical item、lowering contract与call-local augmentation为前提，successor citation mapping不迁移旧opaque handle。Round 5B没有阈值、variant或artifact renderer，也不列inventory；
- `RETAINED_SKILL_CONTEXT`只接纳同run、exact scope、一次从offset 1到EOF、canonical COMPLETE、actual FULL且continuity已安装、current manifest仍相同的ordinary Skill read；最多8项、40,000-token aggregate，超界按recent prefix整项缩减，不截断body；下一真实ROOT user message、idle compaction或manifest漂移不继承；
- active/idle snapshot adoption原子且ACK unknown闭合；active才install successor epoch，idle只清除旧continuity并让下一turn cold-open；
- `PreparedCompactionCanonicalAdoption`只含可由snapshot/revision/pointer/predecessor/event确认的row drafts；所有planning/epoch/MCP/execution/dry-compile事实只存在于process-local resources；
- current binding唯一决定active summary；
- new ROOT producers全部继承latest exact ROOT snapshot；
- current Terminal/monitor/TODO/flat subagent由bounded `UNTRUSTED_OBSERVATION` handoff重建；Runtime只证明其结构与currentness，TODO不携带item ID且completed正文只计数不注入；TODO COMPACT只选择完整的ordered `ordinal/status/text`前缀并保留原snapshot exact counts/omitted；
- old epoch只在adoption FULL后关闭；
- new epoch内恢复strict-prefix；
- FULL_HISTORY persisted revision marker与effective reader floor分离，首次compaction floor恒为0；repeated compaction source proof使用current binding/base + bounded post-base lineage digest，不从genesis重扫；
- reader/repository复用一个canonical range fingerprint builder，覆盖ordered blocks、ToolResult timing/artifact、closure与cut-visible late outcome；
- source view、prefix proof、protected-tail selection、canonical adoption与normal tool-surface physical access均遵守single-owner DTO contract：existing frozen carriers被组合而不重新摊平，数据库重复列只由central factory派生为只读row mirrors；
- Host-wide只串行summary physical execution，先在锁外取得lane、再recapture并安装exact-scope fence；canonical admission fence仅覆盖target scope及其source-head producers；
- 每个compaction planning attempt只有一个120秒absolute deadline，贯穿cut、tail、selected replay hydration与wire quote；hydration的新read-only transaction不得重置预算；
- canonical transcript从未删除或改写；
- 无compiled replacement history、durable compaction job、receipt、checkpoint、repair、event replay/replay worker或memory double call；Round 5A.2唯一entry-bound provider replay relation作为前置能力保留；
- durable job universe被删除，Round 5A.2 replay relation保留，oracle为28/24/11/1/24/0；
- targeted、PostgreSQL、retained、full pytest、static、Go与real-provider dogfood通过；
- Gap Index同步记录Round 7.1 normal ToolResult投影、Round 9/9.1与Lightweight TODO前置、PHC-07B恢复，不扩大Go高级UI、memory extraction或hierarchical subagent范围。

---

## 25. 最终交付口径

Coding agent最终汇报必须分开说明：

1. Round 7.1、Round 9、Round 9.1与Lightweight TODO refinement前置activation evidence如何被retained，而非由Round 5B重复实现；
2. 三种compaction入口及token/resource-headroom OR trigger；
3. `PreparedCompactionSummaryCall`如何取得active/idle target、证明source view exact、禁止execution authority，以及COMPATIBLE_APPEND时old SYSTEM/tools/prefix不变；
4. successor standard capability planning/exposure如何freeze、保留compatible old DIRECT、整体promotion READY_CLEAN NEW cohort并使old refs失效；
5. post-compile effective MCP/Skill heads与`RETAINED_SKILL_CONTEXT`如何随continuity CAS安装；
6. protected tool group、Round 7.1 normal result variants与recent human selection；
7. FULL_HISTORY floor、shared canonical range fingerprint、bounded lineage digest，以及canonical row-draft candidate与process resources分层后的atomic adoption/ACK unknown；
8. current Runtime sources、`RETAINED_SKILL_CONTEXT`与`UNTRUSTED_OBSERVATION` COMPACTION_RUNTIME_HANDOFF的重建，以及TODO无ID actionable subshape；
9. active install与idle base-only settlement、lane/fence顺序、cross-turn及repeated compaction；
10. durable job machinery删除后的最终oracle；
11. exact测试、PostgreSQL、static、Go与四条dogfood证据；
12. 明确normal MCP/Skill/ToolResult contract不是本轮修改面，并列出其余non-goals。

最终用户可感知行为应是：

> 长程任务接近token或local materialization边界时，Agent先在原prefix与原生工具面上生成一份语义交接；Runtime随后以该交接、最近真实用户原话、最多三个完整工具组、同run中真正FULL交付且仍未变化的少量Skill正文、当前精确Runtime状态，以及按Round 9/9.1重新冻结的Capability surface与catalog heads建立新context epoch，并在同一任务中继续。此前NEW且当前可靠、完整cohort可容纳的MCP会在该合法rebase boundary自然升格为DIRECT；over-bound时则继续走既有meta route，不牺牲可靠的旧DIRECT。idle compaction只保存下一turn将冷读的base，不制造无人消费的epoch permit。历史仍完整保存在canonical transcript中；compaction不会调用工具、不会抽取memory、不会重新定义普通ToolResult或MCP/Skill语义、不会因provider错误偷偷重试，也不会让模型面对一份artifact/ID清单自行猜测该恢复什么。
