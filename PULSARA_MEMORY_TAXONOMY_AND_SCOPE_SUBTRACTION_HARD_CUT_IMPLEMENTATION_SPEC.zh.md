# Pulsara 记忆 Taxonomy 与 Scope 减法 Hard-cut 实施规范

状态：实施前权威规范

适用仓库：`pulsara_agent`

当前代码基线：`8a2d5fe6`（`fix: hard-cut memory governance terminal semantics`）

本规范是以下已实施规范的后续语义 hard cut：

- `PULSARA_MEMORY_GOVERNANCE_TERMINAL_CLAIM_SOURCE_SEMANTICS_AND_PROMPT_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`

前置规范的 terminal claim、exact source envelope、evidence role、anti-echo、whole-candidate、
relation、public summary、provider-neutral final-wire、ownership 与 deadline 契约继续有效；其中
五类 taxonomy、`USER | WORKSPACE` scope、`ACTION_RULE` structured shape、USER_PROFILE
direct-self-report-only 限制、prompt v2 和相应 clean-v0 schema 被本规范明确取代；前置规范的“单
semantic atom”在 FACT 上由本规范 §9.3 的
“单一内聚管理单元”精确收敛，不再要求形式逻辑上的单命题。

前置规范中 `CHEAP_HINT_REFLECTION` producer、terminal 后 reflection handoff/model call、双 producer
claim/source branch 与 reflection-only schema union 也被本规范 §7.3 明确取代。保留的 cheap matcher
只产生 Main Agent dispatch 前的中性 runtime guidance，不再是 candidate producer。

尚未实施的
`PULSARA_MEMORY_MANAGEMENT_PAGE_AND_CASCADE_DELETION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`
必须先按本规范修订后才能实施。其删除关系代数与用户直接授权原则可以保留，但五类筛选、
scope 投影、`applies_when / do_not_apply_when` 详情、ACTION_RULE 和旧 schema 字段不再是权威真相。

本规范的权威顺序：

1. 当前用户要求；
2. 仓库根目录 `AGENTS.md`；
3. 本规范；
4. 已实施 governance hard cut 中未被本规范明确替代的契约；
5. 当前生产代码；
6. 未实施的管理页规范只作下游设计输入；
7. `archived_docs` 与历史 activation evidence 只作背景。

实施是一次完整 clean-v0 hard cut。Pulsara 当前数据库没有需要兼容的外部部署状态；在精确核验
本地目标确为 disposable development/test PostgreSQL 后，允许直接修改 `0000` baseline 并重置。
不得增加 online migration、旧数据搬运、旧 enum alias、旧/新列双写、compatibility view、feature
flag、repair job 或 v2/v3 双读写。

---

## 0. 执行结论

Pulsara memory 继续是：

> **结构严格、来源可解释、召回可退化，但完整性、新鲜度与最终处理均不受保证的 advisory dataset。**

Universal agentic assistant 不要求 memory taxonomy 覆盖所有可以说出的内容。Taxonomy 的职责不是
构造完整的人类生活 ontology，而是让边界清楚、具有合理复用可能、即使漏召回或陈旧也可安全作为
参考的内容进入长期 memory。

最终 accepted memory kind 封闭为且只为：

~~~text
USER_PROFILE
RESPONSE_PREFERENCE
FACT
DECISION
~~~

完整删除或者不应出现：

~~~text
ACTION_RULE
PROJECT_RULE
CONVENTION
AGREEMENT
GOAL
COMMITMENT
EPISODE
ROUTINE
RELATIONSHIP
~~~

后九项不是兼容 alias，也不能作为 hidden subtype、reason code、prompt-only category 或 UI-only
memory kind 恢复。它们中的有用内容只能忠实落入四类之一、交给明确的其他产品能力、留在 canonical
transcript。

程序性内容的产品边界冻结为：

> **Memory 可以记“采用、倾向或习惯怎样做”；Skill 承载可重复执行所需的具体程序。**

简单做法、工作原则或协作 convention 可以按 USER_PROFILE、FACT 或 DECISION 作为 advisory memory
保存；这只记录“用户/团队采用或倾向什么”，不等于可执行程序。只有需要步骤、工具、模板、验证与
failure branch 等完整操作知识时才进入显式 Skill 产品路径。本规范不自动创建 Skill、不增加
memory-to-Skill job，也不让 memory row 取得程序执行 authority。

原 `USER | WORKSPACE` 不再是 durable scope taxonomy，也不再由 kind 合法性矩阵决定。当前产品只保留：

- `memory_domain_id`：既有用户/账户 ownership 与隔离边界；
- 一个 exact `context_id`：`ctx:global` 或 exact `ctx:workspace/<stable-key>`；
- Host 冻结的 readable context set：transient Host 只读 global，project Host 读 global 加当前 exact
  project context。

数据库删除所有 memory-specific `scope_kind` 列；`scope_id` hard rename 为 `context_id`。不新增
context table、generic context graph、context registry、owner table 或多 context membership relation。

`FACT` 被明确保留为最低消费 authority 的独立声明通道：当用户显式要求“记住 X”，而 X 本身是
来源忠实、大致自足且可安全保存的 attributed declarative claim 时，governance 可以按 FACT 独立
准入。显式 retention intent 证明用户希望保存，不证明 X 属于 FACT；若 X 的实际语义是 USER_PROFILE、
RESPONSE_PREFERENCE 或 DECISION，就应重分类到对应 kind，不能仅因 caller 的 hint 不准或内容来自
轻量归纳而塞进 FACT。FACT 不是“其他”，也不能让 permission、secret、安全策略，或 task/Skill 的
执行 authority 通过改写成陈述句偷渡；但 goal、task、commitment、calendar 和简单做法的背景内容本身
可以按 §2、§3 的宽松语义进入四类之一。

USER_PROFILE 与 RESPONSE_PREFERENCE 不设 direct-self-report gate。Main Agent 从单次/多次行为、工具
选择、计划或对话模式作出的轻量归纳，以及具有对应实质语义的 imperative，均可作为 advisory memory；
governance 只排除 source 明确矛盾、关键事实捏造和产品边界越界，不承担事实证明或价值复审。

Coding 场景中，当前 code/config/schema/lockfile/test/权威项目文档可直接重读的实现事实不复制为
advisory FACT；使用时重读真源。Main Agent 调用 `remember` 前可以把来源中的代词和省略转成自足、
便于 sparse/dense 召回的自然 statement，并以 5W2H 作为软性检查，但不要求七维齐全、不存模板字段、
不推断 source 没有提供的 Why/How。Governance 对 frozen statement 仍只有 accept/skip authority。

本规范通过做减法让系统更轻：

- accepted kind 从五类减为四类；
- 删除 `MemoryScopeKind` 与所有 memory durable `scope_kind`；
- 删除 ACTION_RULE-only 的 `applies_when / do_not_apply_when` candidate/fact 列和 DTO；
- 模型不再处理 kind × USER/WORKSPACE 合法组合；
- `remember` 不再携带 ACTION_RULE structured branch；
- memory read tools 不再让模型试探 USER/WORKSPACE scope filter；
- 删除 terminal/post-reply reflector、第二次 hint-review 模型调用、reflection producer/schema union、
  queue/handoff/config/watchdog；cheap matcher 只保留为前置本地 boolean attention gate；
- task/goal/calendar/commitment 与简单做法不增新 kind，可按四类宽松保存但不取得执行 authority；
- BASED_ON 是唯一主动产品关系并放宽到四类 meaningful rationale；SUPERSEDES/CONTRADICTS 只作为
  被动 lifecycle 结果；
- Main Agent 主动提交且没有明确 hard boundary 时，governance 默认接受；其模型判断重心从“重新证明
  是否值得记”转为 final-kind 分类、existing-memory 对照与被动关系整理；
- 复用既有 `accepted_at` 向所有 memory projection 提供 `recorded_at`，不新增时间列；
- 不增加任何 table、column、event、job、registry、checkpoint 或新 product relation。

---

## 1. 产品定位与准入原则

### 1.1 Universal 不等于什么都记

Universal 表示 memory 语义可以跨工作、项目、旅行、家庭、客户、学习等领域复用，而不是 Pulsara 要保存：

- 用户说过的每句话；
- 每个目标、承诺、日程和任务；
- 每个第三方的完整 profile；
- 每次工具结果或外部状态；
- 每条工作流程、规则和操作习惯；
- 一份压缩后的 autobiographical transcript；
- 一套个人知识图谱或通用事实库。

没有合法 kind 是正常且预期的产品结果。Governance 不得为了提高接受率新增类别、滥用 FACT、
凭空把一次性任务改写成长期画像，或把详细可执行程序包装成“当前流程事实”。这不禁止把来源支持的
目标、计划、承诺、日程背景、简单工作原则或采用的做法忠实分入 USER_PROFILE、FACT 或 DECISION。

### 1.2 默认接受的轻量 admission

Memory 是 advisory dataset，不是需要逐项举证的高精度知识库。Candidate 已由 Main Agent 主动提出，
本身就是“可能值得以后参考”的充分弱信号；governance 的默认姿态应是接受，而不是重新证明它真实、
稳定、长期、高价值或不可从别处获得。

“结构严格”只表示 kind/context/source/relation/record shape 有封闭契约，不表示 admission 要求高置信度、
多次观察或事实证明。来源可解释也只要求以后能看懂这条记忆怎样形成，不把 provenance 变成证据门槛。

只需做三个宽松判断：

1. statement 能否合理读成四类之一；
2. frozen source 是否没有明确反驳它，也没有暴露凭空捏造的实体、事件或因果细节；
3. 它是否没有越过 secret、permission/safety authority、把 memory 当作 task/reminder/Skill 执行器，或
   coding-source precedence 等明确产品边界。

满足以上宽松条件即接受。单次行为、工具选择、计划内容、多轮模式或一句 imperative 都可以成为
USER_PROFILE / RESPONSE_PREFERENCE 的充分 advisory 来源；不要求达到证明标准，也不要求最少观察次数。
用户显式 retention intent 更直接满足复用价值，但普通 candidate 不需要额外证明清楚的未来使用场景、
重复询问成本或未来不存在更权威来源。

不得把以下质量偏好变成额外 hard gate：形式逻辑原子性、完整 5W2H、每条 statement 都显式写入
semantic context/certainty/时间/例外，或保证未来永不陈旧。§1.4 的统一 `recorded_at` metadata 仍是每条
accepted memory 的必有字段；Source 已明确提供的实质限定须忠实保留，source 没有提供的语义维度不
补写。若干相关 clauses 构成一个自然可管理单元时可以整体保存，不要求为了治理方便机械拆句。

真正的 hard rejection 只针对明确越界：source 明确相反或 candidate 凭空添加关键事实；无法落入四类；
secret；permission、安全/隐私/医疗/法律/财务 authority；把 memory 伪装成可靠 task/reminder/履约或
Skill executor；以及下一段规定的可直接重读 coding truth。Goal、task、commitment、calendar 内容或简单
做法本身不再是 rejection reason。除此之外，不得只因内容来自推断、普通、短、暂时、未来用途不够
具体、只表达 What/Who、缺少 Why/How，或将来可能需要再次确认而 `SKIP` / `LOW_VALUE`。

Coding context 中，当前代码、配置、lockfile、schema、测试或同仓库权威文档能够直接、可靠、低成本
重读的事实，作为明确的 coding-source precedence 例外通常不准入。它们的真源就是当前 workspace
material，不应复制成可能陈旧的 advisory FACT 与真源竞争。Memory 可以保存代码中没有表达、且来源
忠实的用户背景、回答偏好、设计决定或非程序性项目 context；不能把“当前代码在哪里/怎样实现/使用
哪个版本”当成长程缓存。

### 1.3 显式“记住”的不同门槛

用户显式说“记住、以后别忘了、请保存这个”等，直接提供 retention intent，因此复用价值不再需要
额外判断：

- governance 不能只因内容普通、难归入前三类或未来用途不够具体而使用 `LOW_VALUE`；
- main Agent 在内容是安全 declarative memory unit 但 kind 不明确时必须仍可调用 `remember`，使用
  `kind_hint=AUTO`，而不是因为没有特定工具分支就不调用；
- governance 可以把它接受为 FACT，并保留来源的 uncertainty、时间和 wording；
- 显式 retention intent 不证明 statement 为真，不提供 global applicability，不授予更强消费语义。

显式“记住”仍不能让 memory 取得以下 authority：

- secret/credential；
- permission、approval、支付/发送/预订 authority；
- 必须可靠遵守的 privacy/safety/medical/legal policy；
- 把 reminder、appointment、deadline、monitor 或 task 当作已建立、会执行或会提醒；
- 把工具顺序、验证流程或 action rule 当作可执行 Skill；
- 无法指认的“记住这个”；
- 明显无关或带来不必要隐私风险的敏感第三方材料；
- 明显无关、无法作为一条自然记忆理解的拼盘，或与 source 明确冲突/凭空添加关键事实的改写。

显式 retention 也不会让 memory 取代当前 coding 真源。若用户要求记住的只是可从当前仓库直接重读的
实现事实，Main Agent 应以代码/配置/测试为准并通常不调用 `remember`；若用户真正希望保留的是代码
未表达的选择原因、业务称呼或跨轮背景，应只保存那个来源支持的 residue，而不是复制整段实现状态。

显式“记住”解决的是 **是否希望 retention**；final kind 决定的是 **以后允许怎样消费**。二者不能混为
一个 bypass。

### 1.4 每条记忆都包含时间

每条 accepted memory 的 provider-visible 完整值必须包含非空 `recorded_at`。它直接投影既有
`memory_facts.accepted_at`，不增加数据库列，也不要求 Main Agent 把机械时间前缀写入 statement。
`updated_at` 继续表示 lifecycle/被动关系导致的最近变化时间。

`recorded_at` 使用一个共享 canonical encoder 输出 timezone-aware RFC 3339 UTC instant，截去 sub-second，
精度到秒即可；不暴露数据库方言，也不为不同消费入口生成不同格式。UI 与自然回复可以按日、月、
年份或相对年龄做
更淡、更粗粒度的展示，但 provider-visible typed value 保持稳定，不动态改写成“3 天前”。

以下入口都必须返回同一个 canonical `recorded_at`：automatic recall、response-preference head、
`memory_search`、`memory_get` / `memory_explain`、governance 的 existing/basis/related projection，以及未来
management catalog/detail。Main Agent 与普通会话 consumer 同时看到当前 `RUNTIME_CLOCK`，因此可以
自行判断一条记忆大约形成于多久以前；自然回复不要求复述到秒，按日、月、年份或“近期/此前”等
合适粒度使用即可。Governance auxiliary call 不因此增加 clock packet field。

若 source 本身含“周五、今年、下个月、目前”等会改变 claim 的语义时间，Main Agent 可以使用当前
`RUNTIME_CLOCK` 把它转成大致但可独立理解的自然时间，并保留必要不确定性。这种 semantic time 属于
statement；`recorded_at` 只表示记忆形成时间，不证明内容在该时刻或现在仍为真。

`recorded_at` / `updated_at` 不进入 statement、fact semantic digest、dedup 或 relation identity。这样同一
语义不会仅因再次提及时钟不同而生成不同 memory，也不需要 timestamp parser、time bucket column 或
新的 durability 机制。

---

## 2. 四类最终 Taxonomy

下列“允许 / 不属于本类”首先是分类边界，不是 memory 总准入门。Candidate 不属于一个 kind 时，
governance 必须继续考虑其余 legal kinds；只有明确落在 §1.2 的产品外边界时才 `SKIP`。

### 2.1 USER_PROFILE

产品问题：

> Pulsara 为了理解和正确对待当前用户，应该知道这个用户是谁、通常怎样、喜欢什么或处于什么普通个人状态？

独立消费价值：

- 改善 user-centered 个性化与指代解释；
- ordinary query-driven recall 时作为关于用户的相关背景；
- 形成用户可理解的“关于你”管理投影；
- 与普通 FACT 保持不同的用户画像语义，并按来源及必要限定作 advisory 使用。

允许：

- 称呼、代词、主要语言、常用单位、home locale/timezone；
- 用户明确陈述的身份、长期角色、能力、accessibility need；
- 兴趣、喜好、厌恶、普通价值取舍；
- 描述性的生活/工作习惯；
- 稳定、暂时、推断所得或带情境/时间限定的个人状态；
- 当前用户在某个 exact context 中的角色；
- 理解用户所需的最小直接关系锚点，例如“Mei 是我的伴侣”；
- assistant 根据 canonical 对话、用户选择、工具观察、计划或多轮行为合理归纳的用户画像；
- 实质语义确实表达长期身份、称呼、习惯或个人偏好的 imperative，例如“以后叫我 Plum”。

不属于 USER_PROFILE（不等于自动 `SKIP`）：

- source 明确反驳或凭空捏造的人物画像；
- 用户拥有、知道或提到、但并不描述用户的事实，应考虑 FACT；
- project/client 自身状态，应考虑 FACT 或 DECISION；
- how the Agent should answer，应考虑 RESPONSE_PREFERENCE；
- 仅描述某个 task 的即时执行动作；若它同时表达用户的持续目标、角色、aspiration 或个人处境，仍可
  考虑 USER_PROFILE；具体进度、deadline、appointment 或已选计划通常考虑 FACT / DECISION；
- Agent 执行流程、permission 或 safety rule；
- secret、高风险权威真相或完整第三方档案；
- 把纯 task、procedure、permission 或一次性要求改写成“用户喜欢……”或“用户通常……”。

分类判据：它是对当前用户可理解的描述，而不是要求 Agent 下一步完成某个具体动作。它可以稳定，也可以
是带情境或时间边界的暂时状态；“advisory”已经允许它以后陈旧或需要确认。

Direct human self-report 与 assistant 的 source-aware inference 都是合法来源。Main Agent 可以从 exact
causal context 做轻量画像归纳；一次 observation 也可以形成 advisory memory，不规定最少 observation
次数，也不增加 confidence、inference kind 或 provenance column。若“可能”“通常”“在当前项目中”等
限定会实质改变语义，可以直接保留在 statement 中，但不能把是否写出这些词再设为 admission gate。
Governance 只排除与 source 明确冲突或凭空捏造的内容，不要求原话逐字陈述画像，也不因其属于 assistant
inference 自动拒绝。

USER_PROFILE 不再强制 global。以下都合法：

- global：“用户主要使用 Python”；
- current project：“用户在 Client A 项目负责后端”；
- global-readable、但正文自带时间边界：“用户本月吃纯素”。

`context_id` 只提供 coarse retrieval placement 与 project fence，不穷尽主题、时间或条件相关性；后者
必须留在 statement。属于当前目录 project 的 local profile 不能仅因正文写出了 project 名就放入
global-readable lane；只有 source 明确支持跨 project/对话读取，或语义 context 根本不对应当前目录
project，并且 statement 忠实保留了 named-context 限定时，GLOBAL placement 才成立。

### 2.2 RESPONSE_PREFERENCE

产品问题：

> 用户希望 Agent 的回复、解释、推荐展示或交付文本通常呈现成什么样？

独立消费价值：

- query-independent bounded response-preference head；
- 在新 ROOT human prompt 边界直接影响回复 presentation；
- global 与 current-context response defaults 可以同时以 exact context label 提供。

允许：

- 长短、语言、语气、解释深度；
- 先结论还是先背景；
- 是否给示例、引用、表格或简单栏目；
- 单位、拼写、面向某个客户/课程/领域的简单 output style；
- statement 内的一条简单条件，例如“讨论数学时写完整推导”。

不属于 RESPONSE_PREFERENCE（不等于自动 `SKIP`）：

- 工具选择、分析步骤、验证顺序、循环、重试或状态维护；
- 多步模板工作流或需要附带资产/术语表才能执行的方法；
- 当前一次回复的临时格式要求；
- 用户的一般爱好，应归入 USER_PROFILE；
- permission、安全策略、奉承/依赖、隐瞒重大风险或伪造 authority；
- 将 core behavior/system policy 改写成 soft preference。

分类判据：只改变最终可见回复/产物的呈现即可满足，且偶尔偏离最多降低体验。如果内容改变解决任务
的方法，它就不属于 RESPONSE_PREFERENCE；简单原则、习惯或采用方向可继续考虑 USER_PROFILE、FACT
或 DECISION，只有需要可靠重复执行的详细程序才属于 Skill。该偏好可以由用户直接表达，也可以由 Main
Agent 从当前或既往交互中温和归纳；不因它不是逐字 self-report 而拒绝。

条件与例外直接留在单一 statement 中。本规范不把 ACTION_RULE 的 structured applicability 字段
推广到 RESPONSE_PREFERENCE，也不增加 generic condition AST。

### 2.3 FACT

产品问题：

> 哪条由对话或 observation 带来的 declarative context，可能值得以后在相关情境中作为来源归属明确的背景考虑？

独立消费价值：

- 恢复用户与 Agent 的 shared context；
- 承载用户之外的 context/entity/environment information；
- 为显式 retention 提供最低消费 authority fallback；
- 不迫使模糊声明被错误洗成 profile、preference 或 decision。

FACT 的“fact”只表示 accepted attributed statement，不表示 Pulsara 已验证现实真相。消费者必须按
来源、certainty、时间与当前 evidence 使用。

允许：

- 用户报告或 Main Agent 从 source-aware context 温和归纳的 project/client/course/trip/family context；
- exact cited PRIMARY_OBSERVATION 带来的、且不存在更适合作为当前真源的低成本读取路径的状态；
- 对未来协作可能有帮助的低敏第三方背景；
- 带明确日期/不确定性的历史 residue；
- 用户显式要求保存、且 statement 本身独立满足 FACT 准入的安全 declarative memory unit；
- 来源支持的 goal、task、TODO、deadline、appointment、commitment 或 calendar 背景，例如用户当前要
  完成什么、何时有安排、曾答应什么或计划做什么；不要求先剥掉时间与 lifecycle 词，但记忆本身不
  取得进度机、提醒、履约、调度或主动跟进语义；
- 对简单做法、工作原则或当前惯例的描述；若重点是“已选择以后如此做”，优先归 DECISION，若重点是
  用户的个人习惯，优先归 USER_PROFILE。

不属于 FACT（不等于自动 `SKIP`）：

- raw transcript、raw ToolResult、artifact dump 或完整 episode；
- 与 source 明确冲突或凭空捏造的关键事实，以及只由 memory echo 自证的内容；
- 高度易变且可从权威来源重取的价格、余额、航班、库存、法律现状等；
- 当前 workspace 的代码、配置、lockfile、schema、测试或权威项目文档可以直接重读的实现状态、路径、
  版本和接口形状；
- permission、approval、safety/privacy policy；
- secret、credential、无用途的敏感第三方材料；
- 足以被直接执行的详细 Skill 正文、工具编排、模板、循环与 failure branch；
- 仅通过改成“当前流程是……”来让 memory 冒充 task/reminder/Skill executor。

FACT 是最后一条 **declarative lane**，不是 `OTHER`。它只有 context/reference authority，没有：

- preference ranking authority；
- response shaping authority；
- decision closure authority；
- program execution authority；
- permission 或 truth authority。

即使一个 coding FACT 已因历史数据或显式背景理由存在，召回结果也只可作为定位线索；执行、解释或
修改代码前必须重读当前 workspace 真源。当前 code/config/test evidence 与 recalled FACT 不一致时，
直接以当前真源为准，不能先用 memory 影响判断再把冲突当成同权事实。

FACT 不是 stronger-kind rejection 后的 control-flow fallback：

- assistant/tool 推断的用户属性应直接按 USER_PROFILE 判断，不因它是推断而拒绝，也不改存 FACT；
- 未形成的回答偏好不能因 RESPONSE_PREFERENCE admission 失败而改存 FACT；
- 明确表达倾向、计划或已采用方向的内容优先按 DECISION；若 source 只支持“用户曾考虑/报告 X”这一
  attributed 背景，则可以按 FACT 忠实保存，而不是伪造 decision closure；
- governance 不能为了救一个 candidate 自行添加“用户此前表示……”包装。

只要 candidate 与 source 没有明确冲突、没有凭空添加关键事实，并能作为 attributed/background
proposition 理解，就按 FACT 自身契约宽松判断。例如“记住我希望有一天学日语”可以保存为带 aspiration
语气的 FACT，也可在重点确实是个人长期志向时归 USER_PROFILE；“记住我要买牛奶”可以保存为带
`recorded_at` 的当前意图 FACT。两者都不需要用户额外声明“只当背景”，也都不会因此创建 task、提醒或
跟踪。

### 2.4 DECISION

产品问题：

> 在一个 exact context 中，用户、团队或 Agent 已选择、倾向、计划或采用了什么方向？

独立消费价值：

- 保留已选方向、计划、承诺和采用的简单做法；
- 帮助 Agent 以后接续上下文，而不是把每次对话重新当成空白；
- 可以通过 BASED_ON 表达形成决定的重要依据，并采用明确的共同删除命运。

允许：

- 用户明确选择、同意、倾向或计划的方案；
- 从 canonical 对话、行为、计划或工具结果中可合理看出的已采用方向；
- goal、aspiration、commitment 或 future plan，只要重点是“选择/打算朝什么方向做”；
- appointment、deadline 等带时间的安排，当重点是用户已选定的计划；若重点只是当前状态或日期，归
  FACT；
- 简单工作原则、一次性规则摘要或采用的协作做法，例如“以后实现前先写测试”；
- 在用户授权 Agent 作选择的任务中，assistant 公开宣布的选择；
- global personal context 或 exact current project context 中的上述选择；
- lifecycle 中后来出现的替代或冲突，按既有被动 SUPERSEDES/CONTRADICTS 契约处理。

不属于 DECISION（不等于自动 `SKIP`）：

- source 中完全没有选择、倾向、计划或采用语义的随机建议、候选或草稿；
- permission；
- 足以执行的详细 Skill 程序、工具序列、模板、循环或 failure branch；
- 只有当前状态、没有方向性选择的事实；
- 与 source 明确冲突或凭空添加“已经选定”的改写。

DECISION 是便于未来检索的 advisory 分类，不是不可逆的正式决议。它不要求选项已经永久关闭，也不
保证目标完成、承诺履行、日程触发或做法被每次执行。当前用户输入、task/calendar 真源与现实状态始终
优先；若 source 只表达温和倾向，statement 可以忠实保留“倾向/计划/希望”等 modality。

例：

- “Apollo 已经选定 PostgreSQL”是 DECISION；
- “Apollo 当前使用 PostgreSQL”是 FACT；
- “以后 schema 变更前先备份”可以是采用简单做法的 DECISION；若还需完整备份命令、校验与恢复流程，
  那部分另属 Skill；
- “我决定今年通过 B2”可以是带时间限定的 DECISION；若重点是长期个人志向，也可归 USER_PROFILE。

---

## 3. 明确不属于 Memory 的内容

### 3.1 Skill

当内容需要作为一套可重复执行的程序交付时，它属于 Skill 产品，而不是仅靠 memory 承载。典型信号
包括多个信号组合，而不是任一词一票否决：

- 需要精确先后步骤；
- 需要具体工具、来源或命令；
- 指定验证、验收或 failure branch；
- 指定循环、重试、维护状态或长期工作流；
- 需要模板、示例资产、术语表或领域规则；
- 必须按程序一致执行，只有一句 advisory 摘要不足以正确复用。

简单原则、采用的方法、工作习惯、约定或一两句高层做法可以作为 USER_PROFILE、FACT 或 DECISION
保存；它们只帮助模型回忆“通常/已经打算怎样做”，不提供可执行性保证。相同内容未来可以另有 Skill，
两者也不是双写真源：memory 是 advisory 摘要，Skill 才承载完整执行细节。

对照：

| 表达 | 产品归属 |
| --- | --- |
| “回答先给结论” | RESPONSE_PREFERENCE |
| “我通常先写测试” | USER_PROFILE |
| “当前 CI 会在合并前跑测试” | FACT |
| “项目已经选定 pytest” | DECISION |
| “以后实现前先写测试” | DECISION；若是用户自述习惯也可为 USER_PROFILE，若是现行实践描述也可为 FACT |
| “先查官方来源、核对日期，再做比较矩阵” | 可作为已采用方法的 DECISION 摘要；完整可执行流程才是 Skill |

Governance 不能创建 Skill。Main Agent 也不能因为用户随口表达过一个流程，就静默写 Skill；现有或
未来 Skill creation product 仍按自身契约进入。没有 Skill 不妨碍来源忠实的原则、做法或习惯进入四类
memory，但 memory 消费者不得把摘要当成可执行程序或遵守保证。

### 3.2 Task、goal、calendar、commitment

Goal、task、calendar 与 commitment 不需要独立 memory kind，但其中有复用价值的自然语言背景可以
宽松落入现有四类：

- USER_PROFILE：持续 aspiration、人生/职业方向、用户所处阶段，或与用户身份和长期处境连在一起的
  目标，例如“用户今年在准备转向产品岗位”；
- DECISION：已经选择、倾向或计划去做什么，包括目标、承诺、采用方案和简单下一步，例如“用户计划
  今年通过 B2”；
- FACT：当前任务状态、TODO、deadline、appointment、已表达的义务或日历事实，例如“用户周五要交稿”；
- RESPONSE_PREFERENCE：仍只承载回答呈现偏好，不能用来保存任务内容。

这些内容不要求先删除 deadline、进度、下一步或 commitment wording，也不要求用户额外说“只作背景”。
每条 accepted memory 都携带 `recorded_at`；source 中会改变含义的日期可按 §1.4 用 `RUNTIME_CLOCK`
自然消歧后保留在 statement 中。

边界在 authority，而不在题材：memory 不创建 task、日历事件、recurring trigger、提醒、监控、履约义务
或完成状态机。用户要求可靠提醒/安排/执行时，应调用真正的 task/calendar/automation 能力；同时保存一条
有帮助的 advisory FACT / DECISION 并不构成禁止的双写真源，因为当前产品真源继续独占执行与 lifecycle
authority。消费时以当前用户输入、task/calendar/contract 真源和现实状态为准，陈旧 memory 只作背景。

### 3.3 Permission、安全与外部真源

- 付款、发送、预订、删除、批准、访问与授权额度不来自 memory；
- 隐私、安全、医疗、法律或财务硬约束不能只靠 memory；
- credential、密码、token、恢复码不保存；
- 外部可可靠重取的当前真相在使用时重取；
- coding 场景的当前代码、配置、schema、lockfile、测试与权威项目文档属于 workspace 真源，使用时重读，
  不由 memory shadow；
- 高后果使用中，即使相关 USER_PROFILE/FACT 被召回，也必须按当前 authoritative source 或用户确认。

### 3.4 Transcript 与 episode

Canonical transcript 继续承担精确对话历史。Memory 可以保存用户明确表达的 residue，也可以保存 Main
Agent 从一次或多次 canonical 行为、选择、工具观察、计划和经历中提出的轻量 advisory 归纳；它不需要
先达到行为统计或事实证明标准。Governance 仍只有 accept/skip authority，不能自行生成另一条归纳、
rewrite 或把 raw episode 整段入库。

---

## 4. Context Hard Cut：删除 USER/WORKSPACE Scope Taxonomy

### 4.1 三个原本混合的概念

旧 `USER` scope 同时混合：

1. 谁拥有这条 memory；
2. statement 是否关于当前用户；
3. memory 是否在所有 project 中可读。

旧 `WORKSPACE` 同时混合来源目录、适用情境、可见性与 relation boundary。这些概念不能由一个
kind × scope matrix 忠实表达。

新契约：

- ownership 继续由 `memory_domain_id` 表达；
- aboutness 由 kind + statement 表达；
- coarse retrieval/readability placement 与 project-local fence 由 exact `context_id` 表达；
- topical、temporal、conditional 与 named-context applicability 继续由来源忠实的 statement 表达；
- origin 继续由 candidate 的 origin workspace/session/turn/entry 表达；
- provenance disposition 继续决定来源 locator 是否公开。

`context_id` 不是完整 applicability ontology。`ctx:global` 表示未被限制在某一个 project 的可读 lane，
不表示正文在所有主题、时间和情境都成立；“东京旅行”“这门西语课”“本月”等边界必须留在 statement。
不新增 owner、subject、applicability、authority 或 temporality 数据库列。以上语义在当前四类、exact
context、statement、source envelope 与现有 provenance 中已经足够；未来独立产品需要更多 context
类型时另立规范，不能在本 hard cut 预建 generic context graph。

### 4.2 当前 context identity

当前 closed identity：

~~~text
ctx:global
ctx:workspace/<stable-project-key>
~~~

- `ctx:global`：当前 `memory_domain_id` 内不受单一 project 限制、可跨不同对话读取的 lane；它不等于
  USER_PROFILE，不表示 statement 关于用户，也不表示 universal semantic applicability；
- workspace context：由 canonical project path 的既有稳定 identity 派生；
- transient/quick Host 没有 workspace memory context；
- project Host readable contexts 固定为 `(ctx:global, exact current workspace context)`；
- transient Host readable contexts 固定为 `(ctx:global,)`。

使用 non-null `ctx:global` sentinel，而不是 nullable `workspace_id`，以保持 composite FK、UNIQUE、
relation endpoint 与 exact equality 简单；这不是新的 scope kind。

### 4.3 Model-facing target

`remember` 不再要求 `scope=USER|WORKSPACE`，改为 required product target：

~~~text
context_target = GLOBAL | CURRENT_PROJECT
~~~

它只从 Host 已提供的 closed destinations 中选择 retrieval/readability placement，不穷尽 statement 的
完整适用条件，也不作为 `USER|WORKSPACE` taxonomy 的换名；该 enum 不进入 durable schema：

- Host 将 GLOBAL materialize 为 `ctx:global`；
- Host 将 CURRENT_PROJECT materialize 为当前 exact project context；
- transient Host 的 CURRENT_PROJECT 非法且必须在 candidate intake 前拒绝；
- candidate 只冻结 materialized `context_id`；governance 不能改写；
- 四类 kind 在两个 context 中都合法，不存在 kind/context legality matrix。

显式 user remember 在已选 project 中，只有 statement 已明确描述当前 project 且没有跨 project 表达
时，才可以把 CURRENT_PROJECT 作为透明的窄默认 placement。它不能覆盖来源中的 global 表达。
自动/主动提取仅凭“当前 session 在 project 中”仍不足以证明 project locality；source 必须支持该
placement。Transient/quick Host 可把正文自带“东京旅行”“这门课程”等语义限定的内容放入 GLOBAL
lane；这不把该正文解释成普遍适用。GLOBAL 与 CURRENT_PROJECT 会实质改变可读位置且 source 无法
消歧时，main Agent 应澄清，不能静默扩大或缩窄 project fence。

`memory_search` 删除 optional `scope=USER|WORKSPACE` filter。Host 搜索全部 readable contexts，结果
携带稳定产品 label；模型可以在 query 中表达需要的 subject/context，但不能试探 raw scope enum。
Management UI 仍可用 server-owned exact context filter；这不是 model taxonomy。

### 4.4 Context 与 kind 的合法组合

以下全部合法：

- global USER_PROFILE；
- current-project USER_PROFILE；
- global RESPONSE_PREFERENCE；
- current-project RESPONSE_PREFERENCE；
- global FACT；
- current-project FACT；
- global DECISION；
- current-project DECISION。

Source support 而非 enum matrix 决定 candidate 是否忠实。Project-local role 不能仅靠补 project 名称就
扩大到 global-readable lane；带忠实名义限定、在其他对话中仍可正确理解且不属于当前目录 project
fence 的生活/课程/旅行内容可以进入 GLOBAL lane，但不得因此解释为适用于所有情境。Global statement
也不能仅因 origin session 位于 project 就缩窄成 project memory，§4.3 的显式窄 default 除外。

### 4.5 Relation context boundary

保留既有物理 relation set，但只把 BASED_ON 作为模型主动表达的产品关系：

~~~text
BASED_ON
SUPERSEDES
CONTRADICTS
~~~

去掉 scope kind 后的语义与 exact boundary：

- `a BASED_ON b` 表示 `a` 的形成、理解或选择有一个值得保留的重要依据、背景、动机或依赖 `b`；普通
  但有意义的 rationale 可以建立关系，不要求证明形式逻辑上的必要条件；
- 四类 source 都可以 BASED_ON 可见的 active memory。Global source 只能引用 global target；
  current-project source 可以引用 global target 或同一 exact project target；
- 多个 basis 表示这些记忆共同参与了 `a` 的形成或解释，不要求每一个单独不可缺少，也不强制解释为
  逻辑合取。只要关系合理、来源没有明确反驳，就不应因依赖“不够强”而 whole-candidate `SKIP`；
- BASED_ON 同时明确采用产品级共同删除命运：删除 `a` 只删除出边，`b` 不受影响；删除任一 `b` 则按
  级联代数递归删除 `a` 与其后继。这个后果是用户管理记忆时可见的产品选择，不反推模型必须把
  BASED_ON 写得像数据库外键证明；
- 不相关、不可见、不在 exact allowlist、来源无法支持或跨上述 context hierarchy 的 target 仍拒绝。
  语义放宽不放宽 ID、owner、context、visibility、source coverage 或事务 join；
- SUPERSEDES 与 CONTRADICTS 是 governance 在 settlement 时被动发现的 lifecycle 关系，不是 Main
  Agent 应主动编排的产品 link。SUPERSEDES 表示 newer memory 令 same-context older memory 不再当前，
  不要求用户说出正式“替代”；CONTRADICTS 表示 same-context active items 不兼容且无法安全选出胜者；
- SUPERSEDES 与 CONTRADICTS 两端必须 exact same `context_id`；CONTRADICTS 保持双向产品文案与无
  winner；不同 context 默认 coexist；
- TAXONOMY_CORRECTION 仍要求 same cohesive semantic unit 与 same context；relatedness 只帮助发现，
  不提供 relation、context 或 deletion authority。

---

## 5. Candidate 与 Structured Shape 减法

### 5.1 FrozenMemoryProposal v3

新的唯一 frozen semantic shape：

~~~text
FrozenMemoryProposal
  statement
  context_id
  kind_hint             # AUTO | four final kinds
  based_on_memory_ids   # optional meaningful basis for any final kind
  cited_tool_result_handles
~~~

完整删除：

~~~text
scope_kind
scope_id
applies_when
do_not_apply_when
~~~

Source 明确给出且会改变实质含义的条件、期限、certainty 和例外应留在单条 canonical statement 中；
不要求每条 memory 主动补齐这些维度。不得新增
`conditions_json`、generic predicate、context AST、delivery mode 或 hidden action fields 作为替代。

### 5.2 Legal final kinds

- 无论是否有 basis refs，四类都是 shape-legal；source semantics 决定 final kind；
- `kind_hint` 永远只是 producer 的非权威提示，不能缩小或扩大 `legal_final_kinds`；
- 无论 hint 是 AUTO 还是 specific，governance 都可从完整 legal set 选择来源忠实的 final kind，或
  `SKIP`；它不能借 reclassification 改写 statement、context、citation 或 basis refs；
- USER_PROFILE 不再有 scope mechanical rejection；
- RESPONSE_PREFERENCE 保留现有 statement byte bound 与 per-context active capacity；
- candidate statement/citation/basis/total canonical byte bounds保持。

删除 `MemoryDecisionReasonCode.USER_PROFILE_SCOPE_OR_KIND_MISMATCH` 与
`MemoryDecisionReasonCode.MULTI_ATOM_STATEMENT`。前者的 context mismatch 由 source fidelity 使用
`INSUFFICIENT_SOURCE_SUPPORT`；后者与本规范的 cohesive multi-clause admission 冲突，不能改名保留。
只有 frozen shape 不能由任何 legal kind 整体消费或是明显无关拼盘时才用既有
`UNSUPPORTED_STRUCTURE`，不得仅因 clause 数量、不同 certainty/时间或辅助语义使用它。

`TEMPORARY_OR_EPHEMERAL` 只用于仅服务 exact current action、没有任何可合理复用 residue 的噪声；出现
deadline、“今天/周五”、task、goal 或 commitment 词本身不是该 reason。`LOW_VALUE` 只用于 automatic/
proactive 的明显无关内容，不能用于 explicit remember 或仅仅普通、短、暂时、价值不确定的内容。
不得保留旧 reason alias。

### 5.3 面向召回的自然转写、轻量归纳与软 5W2H 指南

Main Agent 在调用 `remember`、冻结 candidate 之前，应产出一条大致自足、自然、适合以后检索的
statement。它可以忠实保留原话、解析代词与省略、补回可辨认的实体/context、去掉寒暄，也可以从
exact causal context 做轻量 advisory 归纳。尤其允许从一次或多次行为、工具选择、计划和对话模式归纳
USER_PROFILE / RESPONSE_PREFERENCE，或把“以后叫我 Plum”“回复先给结论”等 imperative 转成对应的
自然陈述。这里不要求逐字复制、直接 self-report、最少观察次数或高置信度。

边界不是“不得推断”，而是不得自由捏造：candidate 不能与 source 明确冲突，不能凭空增加具体实体、
事件、因果理由、权限或决定，也不能把一次性执行动作或详细 Skill 程序凭空改写成稳定画像或偏好。
Goal、task、commitment、calendar 与简单做法可以按 §2、§3 忠实转写，但不能因此伪造执行 authority。需要
“可能”“通常”“在某情境”等词才能避免实质误导时可以保留；措辞是否包含显式 confidence 标签本身
不是 hard gate。

Governance 看到的 `candidate.statement` 因此是 **producer-authored proposal**：它可能是原话、自然
转写，也可能包含上述轻量归纳。Governance SYSTEM 必须明示这一点，并要求模型使用 packet 中的 exact
source evidence 理解形成背景，而不是要求每个词都有逐字引文。普通归纳、概括或非字节相等不得成为
`SKIP` 理由；只有与 source 明确冲突、凭空增加关键实体/事件/因果/authority，或跨越产品边界时才
whole-candidate `SKIP`。现有 producer assistant entry/tool call 已足以定位形成路径；不新增 durable
`was_rewritten` 字段、
转写 fingerprint、中间文本、分数或另一份 source registry。

5W2H 只作为 **soft authoring checklist**，帮助 producer 想到可能影响检索与消歧的维度：

| 维度 | 在 memory statement 中的问题 | 使用边界 |
| --- | --- | --- |
| What | 值得以后知道的 profile、preference、fact 或 decision 到底是什么？ | 通常必须明确核心 claim |
| Who | claim 关于谁、由谁选择、涉及哪个实体？ | 主语不明确时补回 context 可辨认的名字/角色 |
| Where | 属于哪个 project、客户、课程、旅行或其他 named context？ | 只在影响语义/召回时保留 |
| When | 哪个日期、时期、版本或条件下成立？ | 可能陈旧或有时间边界时尤其重要 |
| Why | source 是否给出、且原因对以后理解真的有用？ | 不凭空编造具体因果；有意义的普通理由可以成为 BASED_ON |
| How | 当前状态如何表现，或回复/工作通常怎样进行？ | 简单原则与采用做法可写；详细可执行程序属于 Skill |
| How much | 数量、阈值、预算或程度是否构成 claim 的必要部分？ | 只有来源支持且不会冒充高风险真源时保留 |

不要求覆盖七项，不规定固定顺序，也不把 `What/Why/Who/How` 设为机械必填。对实际召回而言，清楚的
What、Who/subject 和足以消歧的 named context 通常最重要；Where/When 在 project、旅行、课程、版本
或易陈旧内容中常比 Why/How 更关键。具体因果 Why 与执行性 How 只在 source 提供且能提高未来理解时
出现；How 可以概括简单做法，但不得让 memory 冒充详细 Skill、permission 或 task executor。这不禁止
对四类作宽松语义归纳。

最终 statement 必须是普通自然语言，不存储 `What:`、`Why:`、`Who:` 等表单标签，不新增 5W2H JSON、
columns、condition AST 或 completeness score。固定标签与关键词堆砌会给 dense embedding 增加模板噪声；
当前 sparse tokenizer 和 dense embedding 都直接消费 statement，真正有用的是实体、动作、否定、
modality、时间与 context 本身。

转写规则：

- 把“就用它吧”转成 source 足以支持的“当前 Apollo 项目已决定使用 PostgreSQL”，而不是保留无法
  独立检索的代词；
- 把“给他们写短一点”转成 source 足以支持的“为 Client A 撰写邮件时，用户偏好简洁表达”；
- 从一次选择火车出行归纳“用户喜欢或可能偏好火车出行”，可以作为 advisory USER_PROFILE；
- 把“以后叫我 Plum”写成“用户希望被称为 Plum”，不因原句是 imperative 而拒绝；
- 保留“不、可能、曾经、截至某日、仅在某项目”等会改变语义的限定；
- 不为“我们选 PostgreSQL”补写“因为可靠性更高”，除非 source 明确说过；
- 把“以后部署前先跑测试”按语义写成采用该做法的 DECISION；若 source 只描述现行流程可归 FACT，
  若描述用户习惯可归 USER_PROFILE；不凭这条 memory 构造具体执行步骤；
- 不复制可从当前 code/config/test 直接重读的实现事实来增加检索关键词。

Governance 只能判断 producer 已冻结的 statement 是否能从 exact source 得到可理解的形成背景、没有
明确矛盾或产品越界，仍不得再次 rewrite、split、补齐缺失维度或优化关键词。Cheap hint
只在 Main Agent 的原始 provider dispatch 前提醒它自主检查，不另外调用 reflector 模型、不生成
candidate，因此也不取得 rewrite authority。Advisory completeness 不保证，不为自动路径增加第二次
转写模型调用。

### 5.4 Canonical digest

保留真正跨 durable semantic boundary 的现有 digest，但更新唯一 builder：

- candidate semantic payload 只含 statement、context_id、kind_hint；
- fact semantic digest 只含 final kind 与 normalized statement；
- active uniqueness 使用 `(memory_domain_id, context_id, fact_semantic_digest)`；
- relation identity 使用 source/target context_id；
- 为新 frozen shape 更新 digest domain/version；
- 不新增 fingerprint 字段、registry 或旧/新 digest dual compare。

本地 clean-v0 reset 后不存在需要接受的旧 digest。Git 与测试证明实现内容，不增加文档/代码 SHA。

---

## 6. Shared Product Contract 与 Prompt v3

### 6.1 单一共享语义

以下三个消费者继续导入同一份 provider-neutral product contract：

- stable foreground BASE_SYSTEM；
- `remember` capability descriptor；
- MEMORY_GOVERNANCE stable SYSTEM prompt。

contract id hard cut 为：

~~~text
pulsara.advisory-memory-governance.v3
~~~

v2 taxonomy/scope/action-rule prompt 路径直接删除，不提供 fallback。

### 6.2 Prompt 必须教授

SYSTEM prompt 必须明确：

1. taxonomy 刻意保持小而清楚；不属于某一 kind 时先考虑其他 legal kinds，对来源可理解且未明确越界的
   candidate 默认接受，只有清楚落在产品外边界时才 SKIP；
2. 四类的独立消费 authority；
3. USER_PROFILE 可以 global 或 current-context，也可以来自 direct self-report 或 Main Agent 对行为、工具、
   计划及多轮上下文的轻量归纳；单次 observation、imperative 语气或缺少 confidence 标签都不是拒绝理由；
4. RESPONSE_PREFERENCE 只改变 output presentation；简单工作原则、做法或习惯可以分入其他三类，只有
   需要可靠重复执行的详细程序才属于 Skill；
5. FACT 是 independently admitted attributed declarative lane 与 explicit-retention fallback，不是
   stronger-kind failure fallback、truth 或 OTHER；它可以保存 task/goal/calendar/commitment 的背景和
   简单做法，但不取得对应执行或 lifecycle authority；
6. 当前 code/config/schema/lockfile/test/权威项目文档可直接低成本重读的 coding truth 通常不进 FACT；
   recalled coding memory 永远不能覆盖当前 workspace source；
7. DECISION 宽松表示来源支持的选择、倾向、计划、goal、commitment 或采用的简单做法，不要求永久
   closure；它不执行任务、兑现承诺或承载详细 Skill；
8. explicit user retention intent 直接满足 reuse-value admission，不能再因用途不具体、内容普通、context
   字段不齐或缺少 5W2H 维度而拒绝；它仍不绕过 source 明确矛盾/关键事实捏造、secret、authority、
   把 memory 当作 procedure/Skill/task executor、coding-truth precedence 等 hard product boundary；
9. 简单做法以及 task/goal/calendar/commitment 背景怎样分入四类，并明确真正的执行、提醒、tracking、
   permission、safety、secret 与 volatile external state authority 仍在其他产品/真源；
10. `kind_hint` 非权威，mis-hint 应 reclassify 而不是锁死或 FACT-launder；
11. Main Agent 应在 freeze 前按 §5.3 做 retrieval-oriented 自然转写或轻量归纳；5W2H 不是必填 schema；
    governance 必须知道 candidate 可能不是原话，不要求字节相等或逐词证明，只排除 exact source 明确
    反驳、关键事实凭空新增及产品越界，且自己不得补写缺失维度或优化 frozen statement；
12. pre-dispatch cheap hint 只是 fallible attention cue，不是 human source、保存命令或记忆准入；命中可以
    零次调用，未命中也不禁止 Main Agent 自主调用 `remember`；
13. context placement 本身不穷尽 applicability，GLOBAL 不表示 universal semantics；显式 remember 的
    current-project narrow default 是独立 product rule；
14. BASED_ON 可表达任何四类 memory 的重要依据、背景、动机或依赖，不要求逻辑必要性；普通但有意义
    的 rationale 可以引用，同时必须让模型知道删除任一 basis 会级联删除 source；
15. SUPERSEDES / CONTRADICTS 是 settlement 被动发现的 lifecycle 关系，不作为 Main Agent 主动 link；
16. 每条 provider-visible memory 都携带 canonical `recorded_at`；Main Agent 在 freeze 前使用普通输入中的
    `RUNTIME_CLOCK` 解析必要相对时间，普通 consumer 也可用当前时钟判断记忆年龄；
17. 所有既有 evidence role、source completeness、anti-echo、exact identity/context、relation settlement
    与 public summary 契约。

#### Governance 工作重心 hard cut

新的 prompt 不能再把 governance 描述成对 Main Agent retention judgment 的第二次价值审批。Main Agent
已经基于当前完整 provider context 主动提交 candidate；只要 exact terminal source 没有明确撤回/反驳，
candidate 没有凭空添加关键语义，也没有越过 §1.2 的少数 hard boundary，governance 就应以
`ACCEPT` 为默认结果。

Governance 模型按以下轻量顺序工作：

1. 先检查 terminal source correction、anti-echo 与明确 hard boundary；
2. 若没有 hard rejection，从四类中选择最自然的 `final_kind`；
3. 对 packet 已提供的 existing/related items，判断 candidate 是否只是 duplicate，或是否被动形成
   SUPERSEDES / CONTRADICTS；显式 basis 只按放宽后的 BASED_ON 契约核验；
4. 找不到关系、related retrieval 不完整、多个关系解释都不够清楚，或只是普通 coexist 时，返回 plain
   `ACCEPT`，不得因“关系尚未整理好”而 `SKIP`；
5. 生成来源可解释但不声称 verified/permanent 的 `public_summary`。

因此，大部分来源忠实的 Main Agent candidates 应被接受。Governance 的高价值判断主要是 final-kind 与
关系整理；source fidelity 和 hard boundary 仍是必要的薄护栏，而不是重新施加稳定性、长期价值、形式
原子性、完整 5W2H 或“必须有关系”的证明责任。

这项工作重心调整 **只修改共享 product contract / governance SYSTEM prompt 的措辞、few-shot/golden 与
对应语义测试**。保持现有一次 SYSTEM + USER governance call、packet planning、related retrieval、
allowed-target freeze、closed output union、parser、事务 settlement、deadline/watchdog 与 failure
degradation；不增加 deterministic auto-accept shortcut、第二次 relation call、agent loop、工具调用、新
字段或新持久化机制。本规范其他 taxonomy/context/schema hard cut 仍按各自章节实施，不能把本段误读为
它们也无需改代码。

### 6.3 删除 prompt 语义

完整删除：

- ACTION_RULE definition/source-support/few-shot；
- `applies_when`、`do_not_apply_when` instructions；
- `USER_PROFILE is always USER`；
- USER/WORKSPACE duration matrix；
- project action-rule assistant authorization branch；
- FACT-vs-ACTION_RULE hard pair；
- action-rule condition coexist examples；
- single-atom gate、`MULTI_ATOM_STATEMENT` reason，以及把 near-term reminder/task/goal 一概解释成
  `TEMPORARY_OR_EPHEMERAL` 的示例；
- “先重新证明 candidate 足够稳定/长期/高价值才允许 ACCEPT”的 reviewer posture，以及关系不确定就
  SKIP 的暗示；
- v2 output packet 的 scope kind/raw scope fields；
- `producer_kind_product_label`、双 producer labels 与 reflection-specific source instructions。

### 6.4 Required semantic golden matrix

| Source | Frozen target | 必须结果 |
| --- | --- | --- |
| “我主要用 Python” | global AUTO | USER_PROFILE |
| “我在 Client A 负责后端” | CURRENT_PROJECT（当前项目为 Client A）AUTO | USER_PROFILE，不得 globalize |
| “回答先给结论” | global AUTO | RESPONSE_PREFERENCE |
| “讨论数学时写完整推导” | global AUTO | RESPONSE_PREFERENCE，条件在 statement |
| “给 Client A 写邮件时正式一些” | CURRENT_PROJECT（当前项目为 Client A）AUTO | RESPONSE_PREFERENCE |
| “Client A 当前使用 Azure” | CURRENT_PROJECT（当前项目为 Client A）AUTO | FACT |
| “Apollo 已经选定 PostgreSQL” | CURRENT_PROJECT（当前项目为 Apollo）AUTO | DECISION |
| “Apollo 当前使用 PostgreSQL” | CURRENT_PROJECT（当前项目为 Apollo）AUTO | FACT |
| 原话为“就用它吧”，Main Agent 依据同一 exact source context 写成“Apollo 项目已决定使用 PostgreSQL” | CURRENT_PROJECT AUTO | 按语义忠实性准入 DECISION；不因非逐字复制而 skip |
| 原话仅为“就用它吧”，candidate 自行补出“因可靠性更高而使用 PostgreSQL” | CURRENT_PROJECT AUTO | INSUFFICIENT_SOURCE_SUPPORT；转写不得发明 why |
| 当前代码直接显示服务端口为 8080 | CURRENT_PROJECT AUTO | SKIP memory；使用时重读 code/config 真源 |
| “记住，处理器定义在 src/runtime/worker.py”且文件可直接查到 | CURRENT_PROJECT AUTO | 仍 SKIP FACT；不建立代码影子缓存 |
| 代码未表达：“北星是旧结算系统的内部称呼” | CURRENT_PROJECT AUTO | FACT 可准入；这是业务 context，不是可重读实现状态 |
| quick：“这次东京旅行住在上野” | GLOBAL AUTO | FACT；正文保留“这次东京旅行”，GLOBAL 不等于普遍适用 |
| quick：“我在这门西语课里是初学者” | GLOBAL AUTO | USER_PROFILE；正文保留课程限定 |
| quick：“给妈妈解释医疗报告时用简单中文” | GLOBAL AUTO | RESPONSE_PREFERENCE；正文保留条件限定 |
| “部署前先跑测试” | 任意 | DECISION；记录采用的简单做法，不取得执行 authority |
| “先查官网、核日期、再做矩阵” | 任意 | DECISION；可保存高层方法摘要，具体工具/分支仍属 Skill |
| frozen RUNTIME_CLOCK 为 2026-09-02 Asia/Shanghai；“我决定今年通过 B2” | GLOBAL AUTO，statement 为“用户计划在 2026 年通过 B2” | DECISION；保留计划语气，不保证完成 |
| 同一 frozen clock；“周五提醒我交稿” | GLOBAL AUTO，statement 为“用户在 2026-09-04 要交稿” | FACT 可准入；另由真正 reminder 产品处理提醒，memory 不触发提醒 |
| “500 美元以下可直接订票” | 任意 | permission，SKIP |
| “记住，北星是旧结算系统的内部称呼” | AUTO | FACT declarative lane；不得 LOW_VALUE |
| “记住，我希望有一天学日语” | AUTO | USER_PROFILE；是用户 aspiration，不得要求追加“只当背景”才准入 |
| “记住我要买牛奶” | AUTO | 带 `recorded_at` 的 FACT；保留当前意图，不建立 TODO/提醒 |
| “记住，每次部署前跑测试” | AUTO | DECISION；若 source 重点是用户习惯可归 USER_PROFILE，不得仅因程序语气 SKIP |
| “记住我的密码” | AUTO | SKIP secret |
| assistant 从一次行为推断“用户喜欢火车” | AUTO | USER_PROFILE 可准入；单次观察足以形成 advisory inference |
| assistant 推断“用户喜欢火车”且 hint=FACT | global FACT hint | 重分类为 USER_PROFILE；不得因是 inference 或 mis-hint 而 SKIP |
| “我主要用 Python”且 hint=FACT | global FACT hint | USER_PROFILE；specific hint 不锁死 final kind |
| “回答先给结论”且 hint=USER_PROFILE | global USER_PROFILE hint | RESPONSE_PREFERENCE；不得 hint-lock |
| “Apollo 当前使用 PostgreSQL”且 hint=USER_PROFILE | CURRENT_PROJECT USER_PROFILE hint | FACT；不得 hint-lock |
| current project session 中无 project 语义的普通句子 | project target | source-insufficient，除显式 narrow remember rule |
| global“回答简洁” + project“本项目给完整推导” | 各自 context | rows coexist；当前项目不兼容处 project 局部优先 |
| “用户最近开始学习法语”，related item 只是“用户喜欢欧洲旅行” | GLOBAL USER_PROFILE | plain ACCEPT；普通相关性不等于关系，也不得因无关系 SKIP |
| “北星是旧结算系统的内部称呼”，relation target allowlist 为空 | CURRENT_PROJECT FACT | plain ACCEPT；target 缺失不阻塞健康 candidate |
| existing“Apollo 部署在美国区”，new source 明确表明“Apollo 现已迁移到欧盟区” | CURRENT_PROJECT FACT | ACCEPT_AND_SUPERSEDE；newer 使 older 不再 current |
| existing“用户目前吃纯素”，new source 独立支持“用户目前仍会吃肉”，且无法安全判断时间或胜者 | GLOBAL USER_PROFILE | ACCEPT_AND_CONTRADICT；两端 ACTIVE、无 winner |
| “合同仅允许 EU 数据驻留，因此本项目部署决定仅限 EU 区域” | DECISION + exact basis | BASED_ON；重要约束/理由足够，不要求证明逻辑不可分割 |
| “团队熟悉 Python，因此选择 FastAPI” | DECISION + proposed basis | BASED_ON 可准入；普通但有意义的 rationale 不是 skip reason |
| “用户最近更偏向坐火车，因为此前乘机容易不适” | USER_PROFILE + exact basis FACT | USER_PROFILE 可 BASED_ON FACT；basis 不限 DECISION source |
| candidate 引用一条内容无关、不可见或其他 project 的 memory | 任意 kind + proposed basis | relation admission 拒绝；语义放宽不放宽 exact allowlist/context/source join |
| Apollo 对话先明确 PostgreSQL，随后用户说“就用它吧” | CURRENT_PROJECT AUTO | producer 转写为“Apollo 项目已决定使用 PostgreSQL”；不得保留孤立代词 |
| source 只说“我们选 PostgreSQL” | CURRENT_PROJECT AUTO | statement 不得补写“因为可靠性更高”或其他 Why |
| “以后叫我 Plum” | GLOBAL AUTO | USER_PROFILE；What/Who 足够，不因缺少其余 5W2H 维度而 SKIP |

所有 accept examples 必须分布到四类，不能恢复 universal FACT placeholder。表中及其他所有 accepted
memory 的 provider-visible projection 都必须带同一非空 canonical `recorded_at`；表内相对日期用测试冻结的
`RUNTIME_CLOCK` 做自然消歧，不能靠把每次执行时钟写入 digest 过关。

### 6.5 Dynamic packet v3

Provider-visible candidate 使用：

~~~text
candidate
  statement
  context_product_label
  kind_hint
  legal_final_kinds
  basis_memory_ids
~~~

raw `memory_domain_id`、context_id、workspace ID、candidate ID 继续留在 process-local exact join，不暴露
给 provider。`context_product_label` 使用“可跨不同对话召回（仍按正文限定使用）”或“仅在当前项目/
情境中召回”等产品语言；不能声称 GLOBAL 在所有情境都适用，也不能让动态正文自报 raw context。

Candidate 的 exact producer 恒为 Main Agent `remember` tool call，因此 USER packet 不再传
`producer_kind_product_label`或 raw producer enum。Stable governance SYSTEM 直接说明 statement 由主模型在回复中
提案、可能在 freeze 前做过来源忠实转写，而形成路径不证明内容正确。Internal exact
join 只保留 non-null producer assistant entry/tool call，不增加替代 label 字段。

Governance packet 中所有 existing、basis、related public fact item 都携带 canonical `recorded_at`，并继续
携带区分 lifecycle 所需的 `updated_at`（若该 projection 原本公开）。Candidate 尚未 accepted，不伪造
`recorded_at`。Main Agent 已在普通 provider input 中看到 `RUNTIME_CLOCK`，应在 freeze 前解析会改变语义的
“周五/今年/目前”等相对时间；governance 继续只依据 frozen statement 与 exact source，不为此新增 clock
packet field。Settlement 后才从数据库 `accepted_at` 产生唯一形成时间。

SYSTEM + USER two-message、tools=0、continuity=0、final-wire bytes/local estimator、watchdog 与 provider
neutrality 完全沿用已实施 hard cut。

---

## 7. Main Agent Tool Contract

### 7.1 remember v3

唯一参数：

~~~text
statement                     required
context_target                required: GLOBAL | CURRENT_PROJECT
kind_hint                     optional: AUTO | USER_PROFILE | RESPONSE_PREFERENCE | FACT | DECISION
based_on_memory_ids           optional; any final kind
cited_tool_result_handles     optional
~~~

删除：

~~~text
scope
applies_when
do_not_apply_when
ACTION_RULE kind branch
~~~

Descriptor 必须告诉主模型：

- taxonomy 无法忠实覆盖时通常不要调用；
- 但用户显式要求保存安全 declarative memory 时，即使 kind 模糊也应调用，使用 AUTO；
- 可以根据当前可见对话、行为、工具选择或计划作轻量 advisory 归纳；不要求 direct self-report、多次观察
  或高置信度，imperative 也可按其实质语义写成 USER_PROFILE / RESPONSE_PREFERENCE；
- cheap hint 只要求重新检查原始 human input，不要求一定调用；明确越界时零次调用是正确结果，未命中时
  仍可对可能有复用价值的信息自主调用；
- 若决定提交，在最终用户可见回复前完成 `remember` tool call；不在 final reply 后生成隐藏续转；
- FACT declarative lane 必须独立满足准入；它可保存 task/goal/calendar/commitment 背景或简单做法，但
  不得承载相应 executor、permission、policy、secret，也不得接住其他 kind 的 authority failure；
- 可从当前 code/config/schema/lockfile/test/权威项目文档直接重读的实现事实通常不调用 remember；
  使用 recalled coding FACT 前必须重读当前 workspace source；
- kind_hint 非权威，不能被 caller 当作 final-kind lock；
- 调用前按 §5.3 把 source-faithful residue 写成自足自然 statement：优先明确 What、Who/subject 与必要
  context，再按来源需要保留 Where、When、Why、How、How much；不要求七项齐全，不输出 5W2H 标签，
  不推断来源没有的原因/方法；
- context_target 只选择 retrieval placement，不穷尽正文适用条件，也不能靠改 kind 逃避 source fence；
- BASED_ON 可表示四类 memory 的重要依据、背景、动机或依赖；不要求逻辑必要性，普通但有意义的
  rationale 可以引用。Descriptor 必须同时提醒：删除任一 basis 会级联删除该 source；
- current task/goal/calendar/commitment 的有用背景可以变成长程 advisory memory，但不会创建或更新
  对应 executor/lifecycle；
- 简单工作原则/采用做法可以记忆；详细 procedure 仍进入 Skill product，remember 不创建 Skill。

### 7.2 Read tools

`memory_search`：

- 删除 `scope=USER|WORKSPACE` optional filter；
- optional kind 只含四类；
- search 全部 Host-readable contexts；
- result 对每条 item 给出稳定 context product label、是否 current-context 与 canonical `recorded_at`；
- existing kind relaxation 与 bounded retrieval 可以保留，但不得试探已删除 ACTION_RULE/scope enum。

`memory_get` / `memory_explain`：

- exact ID 与 Host-readable context fence 保持；
- 产品输出不再暴露 scope kind；
- statement、kind、context label、canonical `recorded_at`、lifecycle、source/relation 保持；
- 删除 applies/exclusions 输出；
- FACT 文案不得声称 verified truth。

### 7.3 Pre-dispatch Cheap Hint Guidance

Cheap hint 保留，但从 terminal 后的二次模型提取器 hard cut 为 **Main Agent 首次处理新人类
input 时的轻量提醒**。唯一流程：

~~~text
exact human message/steer accepted
  -> sealed local lexical matcher
  -> no match: no hint
  -> match: add one fixed user-role runtime-guidance message to the next dispatch
  -> Main Agent independently decides zero or more remember calls
  -> ordinary terminal governance of those frozen candidates
~~~

这不意味每轮或每次命中都调用 `remember`：

- cheap matcher 只决定是否展示提醒，不决定记忆意图、kind、context、粒度、准入或调用次数；
- 命中后 Main Agent 仍从原始 human input 独立判断；内容明确是 secret、只有详细可执行程序且没有可
  独立保存的高层做法、无任何可复用语义或 opt-out 时可以零次调用，不能只因临时、来自推断或价值
  不确定而放弃；
- 未命中不是 remember prohibition；Main Agent 看到可能有复用价值的内容时仍可自主调用；
- 用户明确说“请记住”且内容符合边界时，prompt 应强烈引导 Main Agent 提交，但记忆完整性与
  最终治理仍不保证。

#### Provider-visible shape

提醒是 system-authored runtime guidance，不是 SYSTEM prompt，也不是 human message/steer。它使用现有
runtime-observation 协议物理降低为一条 provider `role=user` 的 canonical JSON message：

~~~text
source       = MEMORY_WRITE_HINT
channel      = RUNTIME_OBSERVATION
trust        = AUTHORIZED_RUNTIME_GUIDANCE
lifecycle    = CALL_APPEND
presence     = VALUE
budget       = OPTIONAL
placement    = 95, in the existing pre-anchor observation order
render mode  = FULL only
~~~

provider role 为 `user` 不等于 human origin。Envelope 必须使 Main Agent 与调试工具能机械识别它为
Pulsara runtime guidance；不写 `transcript_entries`，不产生 `HUMAN_STEER`、event、subject slot 或 UI 消息，
也不进入 governance 的 human source evidence。

唯一 FULL body 使用固定、中性、不携带命中文本或 signal code 的产品语义：

~~~text
The following human input may contain durable, reusable information. Apply the
memory contract independently. If appropriate, use remember before the final
reply; otherwise continue normally. This hint does not itself require storage.
~~~

文案可做不改变语义的编码调整，但不能包含“必须记住”、“用户已要求保存”、推荐 kind/context
或复制原文。Hint 自身不是 source evidence、permission 或 user retention intent；只能回到跟随其后的
human input 判断。明确 opt-out、memory write disabled、read-only permission 或 `remember` 未在 exact tool
surface 时必须跳过提醒。

#### Existing placement and continuity

Matcher 只在新 accepted human input 已冻结、provider open 前运行，只读该 dispatch 的 exact human
activation text。不为它新建 post-anchor 特例：它像其他 runtime observation 一样，按现有 compiler
语义放在 exact new trigger/steer anchor 之前。`placement=95` 使其在当前已有 runtime observations 之后，
尽量贴近接下来的真实 user message，但不改写、包裹或拼接该 human message。

若一次 dispatch 接受了多条 human suffix，只根据该 dispatch 的 exact activation text 产生一条提醒。
同一 accepted dispatch 只计算一次 matcher、只 materialize 一次；它与普通 messages 一起进入唯一
compiler、wire lowering、replay selection、materialization 与本地 estimator。不得在 compiler 后手工修改
messages。

`CALL_APPEND` 只对这一次 provider dispatch 提供语义指导。后续 tool loop 不因旧 hint 重复追加；
新 human steer 形成新 dispatch anchor 时可按其 exact text 独立匹配。已发送的 message 作为 provider
prefix 的一部分仍保持 append-only，直到新 cold epoch 或 adopted compaction successor；其 CALL lifecycle 防止
模型把旧提醒误用于后续 dispatch。

#### Closed blast radius

本节不授权重构或“顺手简化”其他子系统。除了删除旧 reflector 直接占用的代码/配置/
schema/tests，以及按已有 source registry/compiler 模式增加一个 `MEMORY_WRITE_HINT` binding，
anything else 保持不变。

特别冻结：

- `ModelRole.PRO | FLASH`、两个 model slot 与现有 purpose routing 保持；删除 orphan
  `MEMORY_HINT_REVIEW` 不授权改变 governance/compaction 的 FLASH 路由；
- MEMORY_GOVERNANCE 的 auxiliary lane、deadline、SYSTEM/USER call shape、terminal claim timing、decision parser 和
  settlement 保持；只删除已不可达的 reflection producer branch，并按 §5.3 告知其 Main Agent
  statement 可能已转写；
- compaction、Hooks、Skills、MCP、subagent、plan、permission、tool execution、provider adapter/transport、
  usage telemetry、embedding/rerank 和 management UI 保持；
- 所有既有 context source 的 channel/trust/lifecycle/placement/degradation/rendering/continuity 保持；新 hint
  完全使用已有 pre-anchor runtime-observation 规则，不新增 placement 分支、framework 或 compiler 特例；
- final-wire estimator、wire materialization、replay selection 与 provider prefix adoption 算法保持；新 hint
  只是多一个经过这条唯一路径的普通短 message；
- memory recall、response-preference head、candidate governance semantics、relation/deletion algebra 不因删除
  reflector 而改变；本规范其他章节已独立授权的 taxonomy/context 减法不得被反向扩大。

对 no-hint 的任意相同 input，provider-visible SYSTEM、tools、messages、final-wire bytes/tokens 与 prefix
compatibility 必须与修改前 exact 相同；只允许 closed source inventory 多一个不发送的
`MEMORY_WRITE_HINT=NOT_APPLICABLE` fact。实施者不得为它增加通用 placement abstraction。

#### Removed reflection path

删除 terminal 后 `MEMORY_HINT_REVIEW` provider call、dormant/active reflection queue、handoff/token、最多四条
reflection candidate parser、cross-provider hint-review 分支与 feature flag。Cheap matcher 收缩为一个纯本地
boolean gate，不生成 excerpt、signal digest/code、candidate 或 durable provenance。

所有 candidate 从此只能由 Main Agent 的 `remember` tool call 产生。因此同步删除
`MemoryProducerKind`、`CHEAP_HINT_REFLECTION`、reflection-only source locator 与 governance branch；不保留
producer compatibility enum/column。Governance 仍等 origin turn terminal，并只依据 exact producer tool call、human/
tool source 与 post-proposal suffix 判断。Cheap hint 本身不解锁 anti-echo、source support 或 relation authority。

---

## 8. Recall 与消费语义

### 8.1 Read binding

`FrozenMemoryReadScopeBinding` hard rename/收敛为 context binding；唯一 readable sequence：

- transient：global；
- project：global，current project。

删除 `MemoryScopeKind`、`FrozenMemoryScope`、`CTX_USER`、`WORKSPACE_SCOPE_PREFIX` public API。使用
`CTX_GLOBAL`、`WORKSPACE_CONTEXT_PREFIX`、`workspace_context_id` 和 exact context tuple。不得保留旧
helper alias。

Existing binding digest 若仍是 provider/cross-operation exact boundary，只更新其 canonical payload 为
ordered context IDs；不新增 fingerprint 或 registry。

所有 binding 后进入 provider-visible memory item 的路径都必须携带由同一 `accepted_at` 投影得到的
`recorded_at`。该字段是 item 完整值的一部分，但不参与 read-binding identity 或 semantic digest。

### 8.2 Response preference head

- 每条 preference item 带 canonical `recorded_at`，使模型可结合 `RUNTIME_CLOCK` 判断偏好的形成时间；
- global 与 current-project ACTIVE RESPONSE_PREFERENCE 都可进入 current project 的 bounded head；
- transient 只读取 global；
- capacity/count/canonical-byte admission 从 per-scope 改名并 exact hard cut 为 per-context；
- canonical-byte admission 测量包含 `recorded_at` 的真实 item payload，不另造不含时间的估算 shape；
- global 与 current-project preference 条件不同可 coexist；
- current-project item 不通过 durable precedence、relation 或 lifecycle 修改 global item；两者继续
  coexist；
- 在当前项目的消费阶段，适用且更具体的 CURRENT_PROJECT preference 只在不兼容维度局部优先于
  GLOBAL default；兼容维度可共同应用，GLOBAL 在其他 context 继续有效；这是 prompt-level specificity，
  不是 cross-context SUPERSEDES/CONTRADICTS 或新的持久化 authority；
- contradiction 仍只在 same exact context 建立；冲突端点继续从 effective head 排除；
- source identity 变化只在下一 ROOT prompt 通过既有 append-only invalidation 生效。

### 8.3 Ordinary recall

- 每条 recall item 带 canonical `recorded_at`；sparse/dense/rerank 不把时间重复拼入 statement；
- USER_PROFILE、FACT、DECISION 继续 query-driven；
- RESPONSE_PREFERENCE 继续从 ordinary automatic recall 排除，避免与 bounded head 重复；
- sparse/dense/RRF/rerank/failure degradation 不变；
- SQL visible predicate 只使用 exact context IDs，不再 zip scope_kind/scope_id arrays；
- workspace provenance fence 由 context prefix + exact origin join 判断，不由 scope enum；
- 不新增 cross-context propagation、generic graph traversal 或 context similarity。

### 8.4 当前真相优先

四类全部是 advisory。当前用户指令、当前 ToolResult、文件、日历、业务系统、permission 与安全策略
永远覆盖 recalled memory。FACT declarative lane 不提高 recall priority，也不获得 always-inject behavior。
Coding 场景中，当前 workspace 的 code/config/schema/lockfile/test/权威项目文档始终是实现状态真源；
memory 只能帮助发现可能相关的实体或历史背景，不能避免读取，也不能因语义相似而与当前真源投票。

---

## 9. Relation 语义保持与减法

### 9.1 保留

- `remember` 唯一直接接受的 relation 输入仍是 `based_on_memory_ids`；不新增 supersedes/contradicts
  参数。后两者只能由 governance 对 newer 与 existing items 的比较在 settlement 中被动产生；
- BASED_ON 允许四类 source，表达 §4.5 的重要依据、背景、动机或依赖；普通但有意义的 rationale 可以
  建 relation，不要求逻辑必要性或 DECISION-only；
- 多个 basis 不必逐个证明不可缺少或逻辑合取；删除任一 basis 仍按已选择的下游删除代数级联 source；
- exact duplicate 即使携带不同 basis set，也不得合并、补写或修改既有 fact/relation；
- SUPERSEDES 是 governance 被动发现“newer 使 older 不再当前”的 settlement 结果，不要求正式
  replacement wording；
- TAXONOMY_CORRECTION 需要 same cohesive semantic unit 与 same context，不要求用户使用内部分类术语；
- CONTRADICTS 两端 ACTIVE、same kind/same context、无 winner；
- exact allowlist 与 source-completeness hard admission；
- relatedness 只发现，不取得 relation authority；
- target-independent public_summary。

### 9.2 删除后的 relation matrix

- ACTION_RULE 不存在，因此相关同类 replacement/contradiction 与 prompt/tests 全部删除；
- USER_PROFILE 可在 current project context 与同 context USER_PROFILE 建 relation；
- RESPONSE_PREFERENCE、FACT、DECISION 同理；
- cross-context supersede/contradict 永远非法；
- BASED_ON visibility 只按 §4.5 context hierarchy；
- relationship/routine/goal/commitment 不增加 relation kind。

### 9.3 单一内聚管理单元与 record atomicity

保持 whole-candidate accept/skip，但不再把 FACT 的正确粒度等同于形式逻辑上的单命题。Candidate 应是
大致自足、读起来像一条自然记忆的管理单元，而不是原始 transcript dump 或互不相关内容的拼盘；这是
轻量质量判断，不是要求模型证明每个 clause 具有完全相同的 certainty、时间边界或未来管理命运。

FACT 可以包含一个命题，也可以包含若干彼此相关、合起来更容易理解和召回的 declarative clauses。
只要完整 statement 仍由 source 支持、整体有一个自然的主要分类，且没有夹带 permission、安全
authority、secret、可直接重读的 coding truth、独立易变外部状态或足以执行的详细 Skill，就可以保存为
一行。相关的 task/goal/calendar/commitment 背景、简单做法或另一类辅助 clause 不自动使整条无效；按
未来主要消费方式选择 final kind。不要仅因它不是最小原子、可能以后局部变化，或存在另一种合理拆分
方法而拒绝。

明显无关、无法用一个主要 final kind 自然消费或具有不同管理命运的 bundle，适合由 producer 在 freeze
前拆开；这是一条 authoring 建议，不是“出现两类词就 SKIP”的门禁。Governance 仍不能 split、删 clause、
补连接语或 rewrite。Frozen candidate 的粒度不完美但整体仍满足 hard boundary 时应接受；只有 whole
record 明确越界时才 `SKIP`。不设置 clause 数量上限，也不引入 clause-level schema。

Record-level 后果保持明确：

- search/recall 可由任一相关 clause 命中，但始终返回完整 statement；
- digest/dedup 使用完整 canonical statement，不新增 clause digest、subset/superset merge 或 partial
  duplicate；
- SUPERSEDES 只有在 newer record 令 older whole record 不再适合作为 current memory 时才建立；局部
  修正不支持 partial supersede，必要时两条 coexist/contradict，或由 producer 形成更合适的完整 record；
- CONTRADICTS 标记两条完整 record；若冲突 clause 本可独立管理、而其余内容仍应正常存活，说明原
  bundle 过宽，不建立 clause-level relation；
- 当 BASED_ON target 是 cohesive bundle 时，只能指向该完整 record 并沿用既定删除级联；不能指向内部
  clause；这不新增 source/target-kind 限制；
- 删除与管理 UI 始终展示、确认并删除完整 statement，不提供隐式 partial delete/update。

对照：

- “东京行程住上野，入住时间是 10 月 3 日，同行人是 Mei”在相同来源、certainty、时间边界且用户
  会整体管理时，可以是一个 cohesive FACT；
- “我使用 macOS，所以代码示例用 zsh”可以作为以输出方式为主要消费语义的 RESPONSE_PREFERENCE；
  producer 也可以把 USER_PROFILE 单独保存并建立 BASED_ON，但不是 admission 必需；
- “项目已采用 PostgreSQL，部署前先备份”可以是一个 cohesive DECISION；具体备份命令、校验与恢复
  分支仍属于 Skill；
- condition 是同一 RESPONSE_PREFERENCE 的限定时可以留在 statement；
- governance 不能删掉任何 frozen clause 再接受；若 statement 含详细 Skill/permission 等越界内容只能
  whole skip，task/goal/calendar/commitment 背景本身不再是需要删掉的 clause。

---

## 10. PostgreSQL Clean-v0 Hard Cut

### 10.1 允许直接重置

本仓库处于开发期，当前 local PostgreSQL 为可丢弃开发/测试数据库。实施者必须先解析 DSN、确认 host
为本地且 target 明确是 disposable，然后：

1. 记录 before catalog；
2. 直接修改 `0000_conversation_kernel_baseline.sql`；
3. reset verified local database；
4. 从空 v0 构建；
5. 记录 after catalog 与 closed diff。

不创建 `0001`，不迁移旧 ACTION_RULE rows，不转换 ctx:user，不复制 applies fields，不保留旧 CHECK。

### 10.2 memory_candidates

hard cut：

- 删除 `producer_kind`、`trigger_user_entry_id`、`producer_candidate_ordinal` 与 reflection union CHECK/FK；
- `producer_entry_id` 与 `producer_tool_call_id` 改为 NOT NULL，每个 candidate 只能 exact join Main Agent
  的 `remember` tool call；
- 删除 `scope_kind`；
- `scope_id` rename 为 `context_id`；
- 删除 `applies_when`；
- 删除 `do_not_apply_when`；
- `kind_hint` CHECK 只含 AUTO + 四类；
- `final_kind` CHECK 只含四类；
- decision reason CHECK 删除 `USER_PROFILE_SCOPE_OR_KIND_MISMATCH` 与 `MULTI_ATOM_STATEMENT`；
- unique/FK identity 改为 `(id, memory_domain_id, context_id)`；
- context CHECK：`ctx:global`，或 exact project origin workspace context；transient origin 不能写 project
  context；
- accepted/candidate lifecycle、terminal claim、source locator、public summary、visible-memory provenance 与
  status union 保持。

### 10.3 memory_facts

- 删除 `scope_kind`；
- `scope_id` rename 为 `context_id`；
- fact_kind 只含四类；
- 删除 `applies_when`；
- 删除 `do_not_apply_when`；
- 删除 USER_PROFILE scope CHECK；
- 保留 RESPONSE_PREFERENCE statement bound；
- active semantic UNIQUE 改为 `(memory_domain_id, context_id, fact_semantic_digest)`；
- accepted candidate pair、search terms/document、lifecycle 与 timestamps 保持；
- 既有 non-null `accepted_at` 是 canonical 形成时间，在 provider/UI projection 中命名为 `recorded_at`；
  `updated_at` 保持 lifecycle 最近变化时间。不新增时间列，也不把时间加入 semantic digest。

### 10.4 Basis refs

`memory_candidate_basis_refs`：

- 删除 `source_scope_kind`、`target_scope_kind`；
- `source_scope_id` rename `source_context_id`；
- `target_scope_id` rename `target_context_id`；
- candidate FK 使用 exact source context；
- target fact FK 使用 exact target context；
- 删除 DECISION-only source-kind 限制；四类 source 均合法；
- CHECK 使用 §4.5：global source 只能 global target；workspace source 可 global 或 same workspace。

### 10.5 Relations

`memory_relations`：

- 删除 `source_scope_kind`、`target_scope_kind`；
- rename source/target context IDs；
- source/target fact kind CHECK 只含四类；
- endpoint FK/UNIQUE/index 使用 context_id；
- BASED_ON CHECK 删除 `source_fact_kind = DECISION`，四类 source 均合法；其余
  BASED_ON/SUPERSEDES/CONTRADICTS CHECK 使用 §4.5；
- unordered contradiction UNIQUE 使用 same source_context_id；
- relation set、lifecycle 与 supersede mode 不增加。

### 10.6 Citation visibility

原 `USER_SAFE | WORKSPACE_BOUND` hard rename 为：

~~~text
GLOBAL_SAFE
CURRENT_CONTEXT_BOUND
~~~

GLOBAL candidate 只能引用 GLOBAL_SAFE ToolResult；current project candidate 可引用两者。旧字符串不保留。

### 10.7 Closed schema delta

从 `8a2d5fe6` clean-v0 baseline 计算，允许：

- 删除 13 个冗余/失效列：六个 scope-kind endpoint 列，candidate/fact 各两个 ACTION_RULE-only
  列，以及 candidate 的 `producer_kind`、`trigger_user_entry_id`、`producer_candidate_ordinal`；
- scope-id 列原位 rename 为 context-id；
- candidate producer locator 收紧为唯一 non-null assistant entry/tool call FK，删除 reflection provenance union；
- closed enum/CHECK vocabulary 与相应 FK/index 列集合更新；
- citation visibility vocabulary rename；
- canonical digest/identity version 与派生 catalog/checksum resource 更新。

禁止：

- 新 table、column、index、FK、trigger、function、event、job、grant；
- context table、owner table、subject table；
- soft-delete、migration mapping 或 compatibility view；
- JSON applicability replacement；
- schema strengthening 与本次减法无关的 relation invariant；
- management deletion spec 尚未实施的 DELETE grant/FK action 提前混入本 hard cut。

Schema oracle 必须证明 after catalog 的表数、event/job/relation kind 数不增加，memory column 总数减少，
所有额外差异都能归入上面的 closed set。

---

## 11. 当前生产真源与文件范围

实施前必须完整读取并追踪等价真源：

- `AGENTS.md`
- 本规范及已实施 governance hard cut
- `src/pulsara_agent/memory/scope.py`
- `src/pulsara_agent/memory/product_contract.py`
- `src/pulsara_agent/memory/__init__.py`
- `src/pulsara_agent/workspace_identity.py`
- `src/pulsara_agent/ports/system_prompt.py`
- `src/pulsara_agent/capability/builtin_catalog.py`
- `src/pulsara_agent/conversation_kernel/memory/contracts.py`
- `src/pulsara_agent/conversation_kernel/memory/governor.py`
- `src/pulsara_agent/conversation_kernel/memory/reflection.py`
- `src/pulsara_agent/conversation_kernel/memory/recall.py`
- `src/pulsara_agent/conversation_kernel/memory/dispatch.py`
- `src/pulsara_agent/conversation_kernel/memory_tools.py`
- `src/pulsara_agent/conversation_kernel/context_sources.py`
- `src/pulsara_agent/conversation_kernel/provider_dispatch.py`
- `src/pulsara_agent/conversation_kernel/runner.py`
- `src/pulsara_agent/conversation_kernel/host.py`
- `src/pulsara_agent/conversation_kernel/execution_watchdogs.py`
- `src/pulsara_agent/conversation_kernel/_repository/memory.py`
- `src/pulsara_agent/model_input/contracts.py`
- `src/pulsara_agent/model_input/compiler.py`
- `src/pulsara_agent/model_input/continuity.py`
- `src/pulsara_agent/primitives/model_call.py`
- `src/pulsara_agent/retrieval/config.py`
- `src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql`
- migration manifest/catalog/grant resources
- `tests/test_memory_governance_semantics.py`
- `tests/test_round8_advisory_memory.py`
- `tests/test_stage2_conversation_kernel_postgres.py`
- `tests/test_stage5_clean_migration.py`
- 与 memory scope、workspace identity、tool schema、provider input source 直接相关的 tests。

### 11.1 必须修改

- shared memory product contract/prompt；
- memory context identity/read binding；
- workspace identity 的 memory context helper；
- foreground BASE_SYSTEM 与 remember/search descriptors；
- candidate/fact/relation DTO、validators 与 identity builders；
- remember intake、basis/citation validation；
- 新 `MEMORY_WRITE_HINT` standard pre-anchor runtime source 与 sealed boolean matcher；
- 删除 cheap hint reflector 的 handoff/parser/queue/activation/provider call/config/watchdog 全路径；
- 删除 `MemoryProducerKind` 和 reflection-only candidate/source/schema branch；
- governance packet/parser/settlement；
- repository candidate/fact/relation/claim/source SQL；
- recall/search/get/explain/response preference head；
- clean-v0 SQL 与 derived catalog/checksum resources；
- focused tests 与 active non-historical product contract fixtures。

其中 §6.2 的 governance 工作重心转变只要求修改 shared prompt/product-contract 文案和语义 fixtures；不得
借此改动 `_govern` 控制流、拆分模型调用、增加 relation pass 或添加自动接受分支。上列 packet/parser/
settlement 修改仅服务于本规范另行要求的 taxonomy、context、shape、time projection 与 BASED_ON hard cut。

### 11.2 不应修改

- terminal claim timing、terminal occurrence fence 与 source envelope算法；
- canonical transcript 行/event、compiler placement 算法与既有 source 的 provider replay 语义；新 hint
  必须复用已有 pre-anchor runtime-source placement；
- PRO/FLASH 模型分工、LLM config/CLI 契约、governance/compaction 的现有 model target；
- 非 `MEMORY_HINT_REVIEW` 的 watchdog owner/policy，以及非 reflector 的 auxiliary model purpose/call；
- 任何既有 context source 的顺序、生命周期、降级、source head 或 compatibility 行为；
- embedding/vector/RRF/reranker算法；
- relation set与 deletion algebra；
- provider usage telemetry；
- permission/Skill/task/goal/calendar实现；
- management UI 生产代码；
- 仅为“多数 Main Agent candidate 默认接受、重点整理关系”而修改 governance 调度、调用次数、packet
  planner、output union、parser、settlement 或失败降级；
- archived Round 8 activation evidence；
- 当前已安装 epoch 的 SYSTEM/tools/messages prefix。

若 static web assets 只是旧 frontend build 产物且本任务没有管理 UI 生产变更，不得手改 minified bundle；由
未来真实 frontend build 按仓库流程生成。

---

## 12. 严格实施顺序

1. 冻结四类 product definitions、FACT 独立声明通道、coding-source precedence、cohesive management
   unit、§5.3 retrieval-oriented authoring、Skill boundary、context guide 与 semantic goldens；先写 pure
   contract tests。
2. Hard cut `MemoryFactKind/MemoryKindHint` 与 reason vocabulary；删除 ACTION_RULE、structured
   applicability 与 USER_PROFILE scope validator。
3. Hard cut memory context types/helpers：`ctx:global` + exact workspace context；删除 scope aliases。
4. 修改 remember/search tool schema 与 runtime intake；证明 explicit ambiguous remember 可以产生 AUTO
   candidate，Main Agent 可做自然转写与 source-aware 轻量归纳，kind_hint 非权威，directly-readable coding truth、
   permission/secret/详细 Skill execution authority 与 stronger-kind authority failure 不能 FACT-launder，
   同时 task/goal/calendar/commitment 背景和简单做法可忠实进入四类。
5. 激进删除 terminal cheap-hint reflector 全路径及 reflection producer/schema union；把 sealed matcher 收缩为
   pre-dispatch boolean gate，用已有 pre-anchor source registry/compiler 路径实现唯一
   `MEMORY_WRITE_HINT` user-role runtime guidance；证明它无模型调用、无 candidate authority，且不因
   FACT declarative lane 放宽
   explicit-intent/reuse-value gate。
6. 修改 governance SYSTEM/packet 为 v3，保留 exact source/terminal/evidence mechanics，收紧为唯一 Main Agent
   producer；删除 v2 与 reflection fallback。工作重心转变只落在 prompt/golden，不为它单独修改调用或
   settlement 控制流。
7. 修改 candidate/fact/relation DTO、digest、read binding、repository SQL 与 recall projection；把既有
   `accepted_at` 统一投影为 provider-visible `recorded_at`，不新增数据库列。
8. 修改 clean-v0 baseline 与 catalog resources；reset verified disposable PostgreSQL。
9. 跑 pure/focused tests，修复 taxonomy/context/tool regressions。
10. 跑 PostgreSQL/clean-v0/relation/recall/response-preference tests。
11. 跑 real-provider semantic dogfood，检查四类、FACT declarative lane/cohesive bundle、coding-source
    precedence、retrieval-oriented authoring、task/goal/calendar/commitment 宽松归类、简单做法与详细
    Skill 的 authority 边界。
12. 更新仍属于 active truth 的文档；先修订管理页规范再进入其生产实施。

不得先保留 old scope/kind compatibility 再“以后清理”。

---

## 13. 必测矩阵

### 13.1 Closed taxonomy

- `MemoryFactKind` exact 为四类；
- `MemoryKindHint` exact 为 AUTO + 四类；
- production code/tool schema/prompt/SQL 无 ACTION_RULE/PROJECT_RULE/CONVENTION；
- 无 GOAL/COMMITMENT/ROUTINE/EPISODE memory kind；
- output schema/few-shot 不使用 universal FACT placeholder；
- kind_hint 不缩小/扩大 legal_final_kinds；specific mis-hint 可被 governance 忠实纠正；
- FACT-hinted USER_PROFILE、USER_PROFILE-hinted FACT、USER_PROFILE-hinted RESPONSE_PREFERENCE hard pairs；
- `MULTI_ATOM_STATEMENT` 与 `USER_PROFILE_SCOPE_OR_KIND_MISMATCH` reason exact 0；
- `TEMPORARY_OR_EPHEMERAL` 不因日期/task/goal/commitment 触发，`LOW_VALUE` 不拒绝 explicit remember；
- old v2 contract id/path exact 0。

### 13.2 USER_PROFILE

- global user identity positive；
- current-project user role positive；
- user hobby/habit positive；
- context-bound temporary personal state with explicit time positive；
- assistant 从单次行为、工具选择或计划作轻量画像归纳 positive；
- assistant 从多轮模式归纳 profile positive，不要求 observation count/confidence threshold；
- imperative “以后叫我 Plum”转成 USER_PROFILE positive；
- source 明确反驳或凭空捏造人物属性 negative；
- project/entity fact不能 profile-launder；
- aspiration、持续 goal、个人阶段或 recurring commitment 可按语义进入 USER_PROFILE；
- 只有即时执行动作不能凭空 profile-launder，permission 仍不能 profile-launder；
- contextual profile不得 globalize。

### 13.3 RESPONSE_PREFERENCE、简单做法与 Skill

- global concise/answer-first positive；
- contextual math/legal/client style positive；
- simple output columns positive；
- 用户一般爱好不属于 RESPONSE_PREFERENCE，但必须重分类为 USER_PROFILE 而不是 SKIP；
- imperative “回答先给结论”与从既往回复交互归纳出的简洁偏好 positive；
- one-turn format negative；
- multi-step workflow 与 tool/validation order 不属于 RESPONSE_PREFERENCE；其高层采用方向可重分类为
  DECISION，足以执行的细节仍属 Skill；
- one-step future action rule 可按重点归 DECISION、FACT 或 USER_PROFILE；
- unsafe/dependency/risk-concealment negative；
- 详细 procedure 即使以“偏好/决定/记住”开头也不能让 memory 取得 Skill execution authority；但来源
  支持的高层做法摘要不得因此一并 SKIP。

### 13.4 FACT declarative lane 与 explicit-retention fallback

- ordinary source-supported durable context FACT positive；
- exact cited PRIMARY_OBSERVATION 只有在不复制可低成本重读的当前真源时 positive；
- 当前 code/config/schema/lockfile/test/权威项目文档可直接重读的事实 negative；
- “记住某处理器当前文件路径”即使 explicit 仍 negative；代码未表达的业务别名/设计背景 positive；
- 历史遗留 coding FACT 被召回时，当前 workspace truth 绝对优先并触发重读；
- explicit safe ambiguous remember -> FACT，不得 LOW_VALUE；
- explicit uncertain statement保留 uncertainty；
- explicit time-bounded background保留日期；
- 没有明确未来使用场景、但具有合理复用可能的 source-supported ordinary candidate positive；
- 只有 What/Who、缺 Why/How 或未携带并不存在的 context 字段不得成为 LOW_VALUE/SKIP 理由；
- goal/task/TODO/deadline/appointment/commitment 的来源忠实背景 FACT positive，不要求先删掉 lifecycle
  wording 或要求用户追加“只作背景”；
- “希望学日语”按语义重点可为 USER_PROFILE 或 FACT；“记住我要买牛奶”作为带形成时间的当前意图
  FACT positive，但两者都不创建提醒/跟踪/task；
- stronger-kind authority 不得降级 FACT；若 source 只支持“用户曾考虑/报告 X”而不支持 preference、
  profile 或已选方向，按该 attributed background 接受 FACT 是独立分类，不是 authority laundering；
- 只有纯瞬时噪声、明显无关且没有任何可复用语义的 automatic/proactive statement才可 LOW_VALUE；
- `TEMPORARY_OR_EPHEMERAL` 只覆盖 exact current-action-only 且无 reusable residue 的内容；周五/date/task
  wording 本身不能触发；
- raw episode、ToolResult dump、与 source 明确冲突/凭空捏造的 assistant claim、memory echo self-proof
  negative；
- secret、permission、安全 authority 即使显式“记住”也 negative；详细 workflow 不能取得 Skill authority，
  但简单做法摘要可按其他 kind 接受。

### 13.5 DECISION

- user explicit choice、倾向、计划、goal、commitment positive；
- authorized assistant final choice positive；
- 从行为/计划可合理看出的 adopted direction positive；
- 没有任何选择/倾向/采用语义的随机 suggestion/draft negative；比较中的温和倾向可保留 modality 后
  positive；
- current fact vs chosen option hard pair；
- “我决定今年通过 B2”与简单“以后实现前先写测试” positive；
- detailed procedure with “决定”仍不能取得 Skill authority，但高层 adopted method 可为 DECISION；
- DECISION advisory、不保证 closure/完成/履约/执行；当前真源覆盖；
- basis 允许四类 source；普通但有意义 rationale positive，不相关 basis negative；任一 basis 删除仍级联。

### 13.6 Context

- database/DTO 无 memory scope_kind；
- global exact `ctx:global`；
- project exact current workspace context；
- transient CURRENT_PROJECT intake rejected；
- all four kinds legal global/current project；
- project Host reads exactly global + current；
- transient reads exactly global；
- transient bounded travel FACT、course USER_PROFILE、conditional RESPONSE_PREFERENCE 进入 GLOBAL lane，
  statement 保留语义限定；
- GLOBAL label/packet 不得解释为 universal semantic applicability；
- context candidate source support不足时 skip；
- explicit project remember narrow default positive；
- automatic candidate不能仅靠 origin session 证明 project context；
- model read tools 无 USER/WORKSPACE filter；
- client/provider不见 raw context ID；
- cross-context supersede/contradict rejected；
- BASED_ON context hierarchy exact。

### 13.7 Retrieval-oriented statement authoring

- Main Agent 可在 tool call 前把代词/省略句转成 source-supported、自足自然 statement；
- What 与 Who/subject 清楚、必要 named context 保留的 positive；
- source 支持时保留否定、modality、Where、When、Why、How、How much；
- 只覆盖 What/Who 的简短 USER_PROFILE 仍 positive，不要求七项齐全；
- 缺少 Why/How 不构成 skip reason，Where/When 在语义无关时可省略；
- 不存储 `What:` 等模板标签，不新增 5W2H schema/completeness score；
- 不补写 source 无法合理支持的具体 why/how/identity/certainty/time/authority；source-aware profile/
  preference 归纳本身 positive；
- “就用它吧”结合 exact producer source 可转写成 named-project DECISION；孤立代词不能入库；
- governance packet 告知模型 Main Agent statement 可能已经做过来源忠实转写，且仍附 exact
  source evidence；无 `was_rewritten` 字段或中间文本；
- paraphrase 或轻量归纳不因非字节相等、非直接 self-report 而 skip；与 source 明确冲突或凭空补写的
  Why/How/确定性/时间/身份/context/authority 必须 whole skip；
- sparse query 与 dense paraphrase 分别能用实体、动作、context 命中 source-faithful statement；
- governance 对 frozen statement 仍只能 whole accept/skip，不能重写或补齐；
- cheap hint 只提醒 Main Agent 自己在 freeze 前转写，无第二次 normalization/model call。

### 13.8 Pre-dispatch Cheap Hint hard cut

- exact new human message 命中 sealed matcher 时，provider final wire 在该 dispatch anchor 前只出现一条
  `MEMORY_WRITE_HINT` runtime observation；其 role 为 USER，origin/trust 不是 human；
- no-match 时无 hint；但 Main Agent 仍能主动 `remember`；
- match 但内容是 secret、permission/safety authority、只有可直接重读 coding truth，或只有详细 Skill 且
  没有可独立保存的高层 residue 时零 `remember` call；temporary/task/goal 不再自动为零；
- explicit admissible “请记住”引导 Main Agent 在 final reply 前调用，但 hint 本身不作为意图或 source
  evidence；
- fixed hint body 不复制 human text、signal/code、kind/context 建议，不宣称必须保存；
- write opt-out、memory disabled、read-only 和 absent `remember` tool surface 都不产生 hint；
- hint 走同一 compiler、wire lowering/materialization/estimator，不在 compiler 后拼接；
- 对 no-hint 和所有非 `MEMORY_WRITE_HINT` source，修改前后 provider-visible SYSTEM/tools/messages/
  final-wire bytes/tokens/prefix compatibility exact 相等；内部 closed inventory 只可多一个 NOT_APPLICABLE fact；
- hint-positive 时只比 no-hint sibling 多 exact 一条 pre-anchor user-role runtime message，它按已有
  placement order 位于当前 runtime observations 最后，其余顺序与物理载荷
  exact 相等；Chat Completions 与 Responses 两条 wire 都必须证明；
- compiler 无 post-anchor/generic placement 新分支；现有 source 的 policy tuple 和 ordering test exact 不变；
- same dispatch hydrate/materialize once，tool-loop redispatch 不重复提示；新 accepted human steer 可独立触发；
- provider prefix 在已发送后 append-only，CALL lifecycle 不让旧 hint 继续指导后续 call；
- canonical transcript/UI 无伪 user entry/steer，governance human source envelope 不包含 hint；
- production 中 `MEMORY_HINT_REVIEW`、`CHEAP_HINT_REFLECTION`、`MemoryProducerKind`、reflection queue/
  handoff/token/parser/model call/watchdog/config exact 0；
- clean-v0 中 `producer_kind`、`trigger_user_entry_id`、`producer_candidate_ordinal` exact 0，producer entry/tool
  call 为唯一 NOT NULL locator；
- 旧 feature flag 开/关、v1/v2 producer 及 database rows 无双读写或 compatibility test。

### 13.9 Structured subtraction

- production candidate/fact DTO、SQL、tool schema无 applies_when/do_not_apply_when；
- conditional response preference在单 statement 中 round-trip；
- 单一 RESPONSE_PREFERENCE condition仍是 cohesive management unit；
- cohesive multi-clause FACT positive，任一 clause 可检索但返回完整 statement；
- 明显无关、跨 permission/safety/secret/coding-truth authority，或无法用一个主要 final kind 自然消费的
  bundle negative；仅有不同 certainty/time 或另一类辅助 clause 不自动 skip；
- FACT + task/goal/calendar/commitment、FACT + 简单做法或其他 cohesive cross-kind wording 可以按主要
  消费语义 whole accept；详细 Skill 执行内容仍不能 memory-launder；
- FACT bundle SUPERSEDES/CONTRADICTS/BASED_ON/digest/dedup/delete 全部 record-atomic，无 clause identity
  或 partial mutation；
- semantic digest v3只使用 final kind + statement；
- active uniqueness per exact context。

### 13.10 Recall

- response-preference head、ordinary recall、search/get/explain 与 governance existing/basis/related projection
  每条 item 都有同一 canonical `recorded_at`；
- `recorded_at` 是由同一 `accepted_at` 经共享 encoder 得到的 timezone-aware RFC 3339 UTC 秒级值；各入口
  byte-identical，UI 可粗粒度展示但不得回写相对时间；
- Main Agent 与普通 memory consumer 可结合各自 dispatch 的 `RUNTIME_CLOCK` 判断年龄；`recorded_at` 不
  重复写入 statement/digest，governance auxiliary packet 不新增 clock field；
- response preference head global/current context projection；
- per-context count/byte capacity exact；
- response-preference capacity 与 final-wire fixture 都按含 `recorded_at` 的真实 canonical payload 计量；
- compatible global/current-project preference 同时应用；
- incompatible CURRENT_PROJECT preference 仅在当前项目消费时按 specificity 局部优先，GLOBAL row 不被
  supersede/contradict 或改变 lifecycle；
- same-context、same-condition incompatibility 才建立 CONTRADICTS；
- contradiction endpoint排除保持；
- ordinary recall只读 readable contexts；
- response preference不与 ordinary recall重复；
- sparse/dense failure degradation不变；
- self-contained statement 的 dense/sparse recall 不依赖 5W2H label 或关键词堆砌；
- provider prefix只在下一 ROOT prompt观察新 source。

### 13.11 Relations 与 lifecycle

- 四类 same-context replacement/contradiction；
- taxonomy correction只在四类之间；
- different context coexist；
- 四类 source BASED_ON global/same context positive与非法 cross-context negative；
- meaningful ordinary-rationale positive，不要求 strong dependency 或 multiple-basis conjunction；
- 删除 source 只去除其出边且 targets 保持；删除任一 target 按既定代数级联 source；
- exact duplicate 携带不同 basis set 不补写/合并 relation；
- target allowlist/source coverage hard gate保持；
- SUPERSEDES 作为被动 newer-replaces-older lifecycle，不要求显式 replacement intent；CONTRADICTS
  被动发现、双向展示且无 winner；
- relation ID/UNIQUE 使用 context endpoint。

### 13.12 PostgreSQL oracle

- empty clean-v0 build成功；
- old ACTION_RULE CHECK value不存在；
- old scope_kind与 applies columns不存在；
- old `MULTI_ATOM_STATEMENT` 与 `USER_PROFILE_SCOPE_OR_KIND_MISMATCH` reason CHECK values不存在；
- context FK/UNIQUE/CHECK exact；
- old `ctx:user` row不能写入；
- candidate/fact accepted pair与lineage trigger保持；
- `accepted_at` / `updated_at` 既有 non-null columns 保持，无新时间列；所有 provider/UI memory DTO 用
  `accepted_at -> recorded_at` exact projection；
- response-preference capacity保持；
- event/live-event/subject/guard/relation/job数量不增加；
- memory columns净减少；
- runtime grants无本任务外变化。

### 13.13 Provider neutrality 与 source semantics

- governance仍 exactly SYSTEM + USER、tools 0、continuity 0；
- terminal前不可 claim；
- exact producer cut与post-proposal human suffix保持；
- PLAN/assistant/generic tool不冒充 human；
- final-wire exact bytes/local estimator保持；
- provider usage不改变分类；
- provider token-count API/vendor tokenizer/provider-name branch 0；
- existing epoch prefix不变，新 cold epoch使用新 contract/tools。

### 13.14 Governance 工作重心

- source-supported、无 hard boundary、无清楚 relation 的 ordinary Main Agent candidate -> plain ACCEPT；
- candidate 普通、短、暂时、价值不确定、只有 What/Who 或没有 relation 都不能触发 SKIP；
- clear same-context replacement -> ACCEPT_AND_SUPERSEDE；clear unresolved incompatibility ->
  ACCEPT_AND_CONTRADICT；ordinary coexist -> ACCEPT；
- related retrieval/allowlist 不含合适 target 时 plain ACCEPT，不因无法整理关系丢弃 candidate；
- terminal correction、source 明确反驳、关键语义捏造、anti-echo 与 hard product boundary 仍可 SKIP；
- shared prompt 明确 relation-oriented posture；不存在第二次 relation call、agent loop、tool call、
  deterministic auto-accept branch 或新的 packet/output/settlement shape；
- 修改前后 `_govern` 调用次数、SYSTEM + USER shape、packet shedding、parser union、transaction settlement、
  deadline/watchdog 与 failure degradation exact 保持，除其他明确 hard-cut 字段变化外。

---

## 14. 验证命令与 Real-provider Dogfood

实现时使用仓库根 `.venv` 与 `uv`。

最低 pure/focused：

    .venv/bin/python -m pytest -q \
      tests/test_memory_governance_semantics.py \
      tests/test_round8_advisory_memory.py \
      tests/test_stage2_canonical_reader.py \
      tests/test_stage2_conversation_runner.py \
      tests/test_host_identity.py

PostgreSQL/clean-v0：

    .venv/bin/python -m pytest -q \
      tests/test_stage5_clean_migration.py \
      tests/test_stage2_conversation_kernel_postgres.py \
      tests/test_round8_advisory_memory.py \
      tests/test_memory_governance_semantics.py

静态与构建：

    .venv/bin/python -m ruff check src tests
    .venv/bin/python -m compileall -q src tests
    uv lock --check
    git diff --check

最终必须运行完整 Python suite；若本次 tool schema 改变影响 frontend/runtime contract fixture，也运行真实
frontend test/build。不得增加 skip/xfail、弱化断言或修改 evidence 换绿。

### 14.1 Real-provider semantic dogfood

这是 prompt 与工具语义 hard cut，real-provider dogfood 是 activation requirement。使用 production
SYSTEM + evidence packet 与当前配置的 Responses-compatible provider；不得输出 `PULSARA_API_KEY`。

至少覆盖：

1. 中英文四类各一条；
2. USER_PROFILE / RESPONSE_PREFERENCE hard pair；
3. FACT / DECISION hard pair；
4. global/contextual response preference；
5. current-project USER_PROFILE；
6. 一般爱好从 RESPONSE_PREFERENCE 重分类为 USER_PROFILE，不得 SKIP；
7. 单次行为/工具选择/计划归纳 USER_PROFILE 与 imperative 归纳 RESPONSE_PREFERENCE；
8. quick/transient bounded travel/course/family statement -> GLOBAL placement，仍按正文限定；
9. explicit ambiguous remember -> independently admitted FACT；
10. aspiration、task/TODO、deadline/appointment 与 commitment 按 USER_PROFILE/FACT/DECISION 宽松归类，
    且不取得 executor/lifecycle authority；
11. specific kind mis-hint 被忠实纠正，stronger-kind failure不能 FACT-launder；
12. cohesive FACT/cross-kind bundle 按主要消费语义 positive；明显无关或跨 hard authority 的 bundle negative；
13. directly-readable coding truth 与 explicit code-path remember 均 SKIP FACT；
14. 代码未表达的业务背景可记，召回后仍重读当前 workspace truth；
15. source 中代词/省略经 exact context 转成自足 statement，dense paraphrase 与 sparse entity query 可召回；
16. 不要求 5W2H 七项齐全，不存模板标签，不补 source 未表达的具体 Why/How；
17. explicit low-value ordinary statement不被 LOW_VALUE；
18. detailed executable procedure 不能让 memory 取得 Skill authority，但 source-faithful high-level method
    summary 可按 DECISION/FACT/USER_PROFILE 接受；
19. one-step action rule -> DECISION/FACT/USER_PROFILE，不得仅因 imperative SKIP；
20. goal、commitment、task/calendar 背景 positive 且 non-executing；permission、secret、安全 authority hard
    negatives；
21. source 明确 uncertainty/negation 被反转时 skip；普通 inference 不要求 confidence 标签；
22. post-proposal correction skip；
23. recalled-memory echo；
24. same-context supersede/contradict；
25. 四类 BASED_ON 与 ordinary meaningful rationale positive、unrelated basis negative，并验证删除代数；
26. cross-context relation hard negative；
27. project preference specificity 不改变 global row lifecycle；
28. public_summary 不含 internal scope/context/provider术语且不声称 verified/permanent；
29. 所有 accepted/read/governance projections 携带 canonical `recorded_at`，可与 `RUNTIME_CLOCK` 联合解释，
    不改变 digest/dedup；
30. 一组没有 relation 的普通 Main Agent candidates 大部分 plain ACCEPT，不因 governance 二次价值复审
    被拒绝；
31. clear supersede、clear unresolved contradiction 与 ordinary coexist 三联对照，关系不清/target 缺失时
    plain ACCEPT。

记录实际 SYSTEM、packet、raw JSON、parse/settlement 和 final rows；只排除 API key 值。若凭据/provider
不可用，完成全部本地验证并精确报告环境阻塞，不能声称 dogfood 通过。

---

## 15. 对管理页面与级联删除规范的强制修订

管理页规范进入实施前必须：

1. 五类 filter 改为四类，删除“行动规则”；
2. 不再把“关于你”硬编码为 USER scope；它只能是 USER_PROFILE 产品视图，不能等于 GLOBAL lane；
   GLOBAL 还可包含 FACT/DECISION/RESPONSE_PREFERENCE，产品文案不得暗示其中全是“关于用户”；
3. project view 允许四类，包括 project-context USER_PROFILE；
4. 所有 catalog/detail/relations/deletion plan 从 scope_kind/scope_id 改为 exact context_id；
5. 删除 applies_when/do_not_apply_when 展示与 confirmation expectation；
6. per-scope preference admission 改为 per-context；
7. BASED_ON 跨 global/current-project closure按 §4.5；详情可自然显示“依据/形成背景”，不能伪装成形式
   逻辑证明。删除 preview 明列删除 basis 将级联删除的全部 dependent；
8. schema diff before snapshot 使用本规范完成后的 clean-v0；
9. 管理读写、搜索、SUPERSEDES、CONTRADICTS、BASED_ON 与删除始终把 cohesive FACT bundle 当完整
   record，不提供 clause-level partial edit/delete/relation；
10. 每行只展示最终自然 statement；不渲染 5W2H 字段/标签，也不把转写结果伪装成用户逐字原话；
    source contract 允许时仍通过“在对话中查看”展示真实来源；
11. catalog/detail/relations 均显示 canonical `recorded_at`，可另显示 `updated_at`；不新增数据库时间列；
12. 不重写本规范的简单做法/Skill authority、task/goal/calendar/commitment 宽松归类与 coding-source
    precedence；
13. 保留 CONTRADICTS 双向产品文案、物理删除、恢复 admission、用户 explicit resolution 与不删除
    transcript 的契约；删除 preview 必须明列因删除 superseder 将恢复的旧项，并明确不会自动选择
    CONTRADICTS winner。

管理页 UI 可在修订该规范时为 `ctx:global` 选择“跨对话记忆”等不承诺普遍适用、也不暗示 subject
必为用户的产品文案；“关于你”保留给 USER_PROFILE 视图。Kernel 不通过恢复 USER scope 提前替 UI
做决定，也不能让 UI 把 GLOBAL placement 重新解释成旧 USER scope。

---

## 16. 禁止实现清单

以下任一出现都视为 hard-cut 失败：

- 保留 ACTION_RULE enum/row/prompt/tool alias；
- 把 ACTION_RULE rename 为 PROJECT_RULE/CONVENTION 后继续留在 memory；
- 从普通对话自动创建 Skill；
- 为 goal/commitment/routine/episode 增加 memory kind；
- `OTHER`、`NOTE`、generic CLAIM 兜底类别；
- FACT 或 DECISION 把 task/calendar/commitment 背景伪装成已创建的 executor、提醒、履约或完成状态；
- memory 承载 permission、安全 policy，或把详细 procedure 摘要冒充可执行 Skill；
- 把可从当前 code/config/schema/lockfile/test/权威项目文档直接重读的实现状态复制成 FACT shadow；
- recalled coding FACT 覆盖、免除或与当前 workspace 真源投票；
- stronger-kind authority/formation failure 自动降级 FACT；
- 保留或重命名 `MULTI_ATOM_STATEMENT`，或仅因 statement 多 clause、带时间/任务词就使用
  `UNSUPPORTED_STRUCTURE` / `TEMPORARY_OR_EPHEMERAL`；
- 为 cohesive FACT bundle 增加 clause ID、clause digest、partial mutation、subset merge 或任意 clause cap；
- 用户显式 remember 因 kind 模糊而完全没有可调用 memory path；
- explicit remember 绕过 source 明确矛盾/关键事实捏造或 hard product boundary，或反转 source 已提供的
  实质限定；
- 把 direct self-report、稳定性、多次 observation、confidence、明确未来场景或高价值证明设为 admission
  必要条件；
- 因 candidate 不属于最初尝试的 kind 就直接 SKIP，而不遍历其余 legal kinds；
- 把一般爱好从 RESPONSE_PREFERENCE 排除后直接丢弃，而不重分类为 USER_PROFILE；
- 把 kind_hint 当 final-kind lock；
- 5W2H 必填矩阵、模板标签、JSON/column、completeness score、关键词堆砌或缺维度 skip reason；
- governance/第二模型在 freeze 后补写 Why/How、改写 statement 或创建 normalization durable path；
- 要求 producer-authored statement 与用户原话字节相等，或把转写后的 statement 伪装成用户直接引语；
- 保留 terminal/post-reply reflector、`MEMORY_HINT_REVIEW`、`CHEAP_HINT_REFLECTION`、
  `MemoryProducerKind`、reflection handoff/queue/token/parser/config/watchdog 或任何旧路径 alias；
- 让 cheap matcher 直接生成 candidate、statement、kind/context、source evidence 或 governance decision；
- cheap hint 命中就强制 `remember`，或未命中就禁止 Main Agent 自主提交；
- 把 `MEMORY_WRITE_HINT` 存为 transcript/user-steer/event，伪装成 human source，或放入 SYSTEM root；
- 在 compiler/final-wire materialization 后手工拼接 hint，导致 measurement/replay 不包含它；
- 为 hint 引入 post-anchor/generic placement framework，或改变任何既有 context source 的顺序/生命周期/
  continuity；
- 借删除 reflector 改动 PRO/FLASH、compaction、Hooks、Skills、MCP、subagent、plan、
  permission、provider adapter/transport、非 hint watchdog 或其他子系统；
- `USER_PROFILE -> global` 或其他 kind/context 固定映射；
- 把 GLOBAL 解释成“所有情境适用”或旧 USER scope；
- 要求 BASED_ON 必须是逻辑必要条件、只允许 DECISION source，或仅因 rationale 普通而 whole-candidate
  SKIP；
- 为 basis 建 clause-level relation，或放宽 exact owner/context/visibility/source join；
- provider/model 生成 raw context ID；
- durable `scope_kind`、`MemoryScopeKind` compatibility；
- `ctx:user` 与 `ctx:global` 双读写；
- nullable workspace 与 context_id 两套 representation；
- applies_when/do_not_apply_when legacy columns或 generic JSON替代；
- 0001 migration、online data conversion、compatibility view；
- context/owner/subject registry；
- provider tokenizer/name分支；
- 重写 existing epoch prefix；
- 为本任务新增 event/job/receipt/checkpoint/lease/generation；
- 改写 historical activation evidence 假装新 contract 曾在旧基线激活。

---

## 17. Definition of Done

只有同时满足以下条件，本规范实施才完成：

1. accepted memory kind exact 为 USER_PROFILE、RESPONSE_PREFERENCE、FACT、DECISION。
2. ACTION_RULE 与所有程序性 alias 从 production code、prompt、tool schema、SQL 和 active tests 删除。
3. 简单原则、采用做法、习惯或 convention 可按 USER_PROFILE、FACT、DECISION 保存；详细可执行程序只
   进入 Skill product，memory 不取得 execution authority，也不自动创建 Skill。
4. 显式 user remember 的安全 declarative ambiguity 有 AUTO -> independently admitted FACT path，且不
   取得更强 authority；stronger-kind failure 不得降级 FACT。
5. USER_PROFILE 保留并支持 global/current context；direct human self-report、单次/多次行为、工具选择、
   计划与 imperative 的 source-aware 轻量归纳均可接受，不设 observation/confidence 门槛。
6. RESPONSE_PREFERENCE 与 Skill 的 presentation/process 边界有 positive/hard-negative proof。
7. FACT 是 attributed context/最低 authority declarative lane，不是 truth 或 OTHER；cohesive multi-clause
   FACT 以完整 record 为唯一管理、relation、digest、召回与删除原子；task/goal/calendar/commitment
   背景可保存而不取得 lifecycle authority；`MULTI_ATOM_STATEMENT` reason 已删除。
8. 可直接重读的 coding truth 不形成 FACT shadow；任何 recalled coding background 使用前都以当前
   workspace source 为准。
9. Main Agent 在 freeze 前按 soft 5W2H 做自足自然的 retrieval-oriented 转写或 source-aware 轻量归纳，
   但不要求七项齐全、不存模板、不凭空补写关键事实；governance 明知 candidate 可能不是原话，使用
   exact source 只排除明确矛盾/捏造而不要求字节相等或逐词证明；governance 不取得 rewrite authority，
   cheap hint 不生成或转写 candidate。
10. DECISION 宽松接收选择、倾向、计划、goal、commitment 与简单 adopted method，不要求永久 closure；
    仍不执行任务、保证履约或承载详细 Skill。
11. Durable USER/WORKSPACE scope taxonomy 消失；只剩 memory_domain owner + exact context_id；GLOBAL 只
   表达跨 project readability placement，不承诺 universal semantic applicability。
12. clean-v0 删除 scope-kind、ACTION_RULE-only 和 reflection-only columns，candidate 只保留 non-null Main
    Agent entry/tool locator，无新增 schema/durability机制；verified local DB 从空 v0 重建通过。
13. terminal reflector 全路径 exact 0；sealed cheap matcher 只通过已有 pre-anchor runtime-source 路径生成
    optional `MEMORY_WRITE_HINT`，
    hit/no-hit 都不决定 `remember`，无模型调用或 durable provenance。
14. remember/search/hint/governance/recall/relation 全部使用单一 v3 contract，无 compatibility path；
    kind_hint 非权威，四类均可 BASED_ON meaningful rationale，删除任一 basis 仍级联 source；project
    response preference specificity 只发生在消费阶段。
15. terminal claim、source envelope、anti-echo、relation/public summary、provider-neutral final-wire 与 prefix
    continuity 未回退。
16. 对 no-hint 和非 hint source，provider-input compiler/output 修改前后 exact 不变；PRO/FLASH、
    compaction 与其他非 reflector 子系统无语义或架构扩张。
17. focused、PostgreSQL、clean-v0、完整 suite、static/build 和 real-provider semantic dogfood 全部通过；外部
    环境不可用时只精确报告阻塞。
18. 每条 provider-visible memory 使用既有 `accepted_at -> recorded_at` canonical projection，结合
    `RUNTIME_CLOCK` 可解释年龄；不新增时间列且不改变 digest/dedup。
19. 管理页规范在生产实施前已按 §15 修订，不能从旧五类/scope schema 直接开工。
20. Governance prompt 明确从 retention value reviewer 转为“薄 hard-boundary/fidelity 护栏 + final-kind +
    relation 整理”；多数健康 Main Agent candidate 默认接受，关系不确定时 plain ACCEPT；该重心变化没有
    新调用、agent loop、tool、auto-accept branch、packet/output/settlement shape 或 durability。
