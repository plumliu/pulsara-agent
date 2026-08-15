# Pulsara 长期记忆：三种先例与未来方向讨论草案

> 状态：**DISCUSSION DRAFT / NON-NORMATIVE — 不授权编码**
>
> 记录日期：2026-08-14
>
> Pulsara读取基线：`327bf86061a04e628dc8e700d7030f4237fbbe5d`
>
> 当前实施主线：[Round 8 Advisory Memory Subsystem](ROUND_8_ADVISORY_MEMORY_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md)
>
> 被取代的初步设计：[PULSARA_MEMORY_RELATIONAL_SUBTRACTION_PRELIMINARY_DESIGN.zh.md](PULSARA_MEMORY_RELATIONAL_SUBTRACTION_PRELIMINARY_DESIGN.zh.md)

本文记录Evolver/EvoMap、Codex与grok-build三种不同长期学习/记忆方案中值得Pulsara继续讨论的部分。它讨论的时间尺度长于Round 8，覆盖未来的显式记忆、自动提取、召回、compaction衔接、使用反馈以及程序性Skill演化。

本文不是Round 8的补丁，也不是数据库、event vocabulary或后台job实施规格。除非后续规范明确引用并冻结，本文中的阶段名、载体和方向都不构成编码要求。当前主线仍是先收口Round 8 reviewer findings，再决定后续能力。

---

## 0. 读取范围与证据口径

本轮读取了以下本地代码基线：

| 项目 | 读取提交 | 主要证据 |
|---|---|---|
| Codex | `6138909d6ec58b2fbe635ef973e02caecad5a5aa` | `codex-rs/memories/README.md`、read/write prompt、memory extension、usage记录路径 |
| grok-build | `c68e39f60462f28d9be5e683d9cbe2c57b1a5027` | `xai-grok-memory`、memory tools、session hooks、flush、dream、prompt cache路径 |
| Evolver/EvoMap | `d9df8fb6cad2b17b86a5ec1675d16448efd9d8be` | `README.md`、Gene/Capsule schema、memory graph与solidify相关测试 |

Evolver若干production JavaScript文件是构建后混淆形态，因此本文对它的判断优先依赖公开README、未混淆schema、seed assets与行为测试，不把无法直接审计的内部实现细节写成确定事实。

本文只比较长期记忆和长期学习边界，不评价三者的整体agent架构，也不把某个项目的命名直接当作Pulsara的目标词汇。

---

## 1. 核心判断：三者其实在解决三类不同问题

“长期记忆”至少包含三种经常被混为一谈的产品：

1. **Advisory semantic memory**
   - 用户偏好、workspace事实、行为建议、历史决定；
   - 未来模型可搜索、读取、解释；
   - 可能陈旧或不完整，不是业务事实和权限真源。

2. **Episodic/evidence memory**
   - 某次session、rollout、工具证据、失败与验证过程的摘要或索引；
   - 主要用于追溯“当时发生了什么、为什么得出这个结论”；
   - 不应自动等同于可长期执行的规则。

3. **Procedural/behavioral memory**
   - 可复用工作流、failure shield、验证清单、Skill、Gene或策略；
   - 它改变agent未来“怎样做”，其风险高于展示一条事实；
   - 应经过独立的验证、适用范围与退役机制。

三者的关系不是“一张更大的memory表”：

~~~text
canonical conversation / ToolResult evidence
              |
              +--> candidate --> advisory semantic memory       # Round 8主线
              |
              +--> bounded episodic summary / evidence pointer  # 未来自动提取/compaction
              |
              +--> proposed procedure --> validated Skill       # 更晚的程序性学习

search index / summary / ranking
    = 上述数据的可丢弃访问层
    != 第四种事实真源
~~~

Codex横跨三层；grok-build把第二层和第一层强耦合，并与compaction相连；Evolver/EvoMap主要集中在第三层。Round 8则有意只实现第一层的克制关系模型。

因此，三者对Pulsara最大的共同启示不是“选一个照搬”，而是：

> **必须先分清保存的是一条参考数据、一段可回查经历，还是一种可执行行为；不同层不能共享同一条无门槛晋升路径。**

---

## 2. Codex：证据分层、渐进披露与使用反馈

### 2.1 当前形态

Codex的memory功能是stable但默认关闭的feature。它在符合条件的root session启动时异步运行两阶段pipeline：

- Phase 1从有界的历史rollout集合提取`raw_memory`、`rollout_summary`和slug；
- Phase 2把候选rollout汇总成`MEMORY.md`、`memory_summary.md`、skills与rollout summaries；
- read path先看小型summary，再搜索handbook，最后只打开少量rollout evidence或Skill；
- 使用memory时输出可解析citation，Runtime再把引用到的rollout记为“被使用”；
- 用户明确要求记住、删除或更新时，Agent只能写一条append-only ad-hoc note，而不是直接改最终memory文件。

其写入prompt还明确区分证据强度：

~~~text
user request / correction / interruption
    > tool output / validation evidence
    > assistant narrative
~~~

并明确允许no-op：如果未来agent不会因此表现更好，就不应生成memory。

### 2.2 最值得借鉴的部分

#### 2.2.1 渐进披露，而不是把完整记忆常驻prompt

Codex的read path形成三级访问：

~~~text
memory_summary
    -> MEMORY registry/handbook
        -> 1-2 exact rollout summaries or skills
~~~

这比“每轮把所有记忆拼入system prompt”稳健。对Pulsara而言，可转化为：

- 默认召回只返回少量typed result与短statement；
- 需要完整来源、关系或citation时再调用`memory_get`/`memory_explain`；
- 未来的episodic summary或Skill只通过显式handle展开；
- token预算应约束每一层，而不是只限制最终provider payload。

#### 2.2.2 记忆必须携带来源，并允许模型声明“我实际使用了哪条”

Codex要求最终回答携带memory citation，并把引用到的rollout记录为usage。这提供了一个低成本反馈信号：不是“搜到了”，而是“模型在答案中真正使用了”。

Pulsara未来可以借鉴该区别：

~~~text
retrieved
    != materialized into model input
    != model cited/relied on
    != user confirmed useful
~~~

这四个层级不能压成单个`usage_count`。即使未来只实现后两种中的一种，也应保持语义诚实。

#### 2.2.3 漂移意识是召回产品语义，而不只是排序参数

Codex要求：易变化且便宜验证的事实应现场验证；未验证的旧memory应明确说明可能陈旧。这与Round 8“memory是advisory data”高度一致。

值得保留的产品行为是：

- recall结果公开scope、来源和时间；
- 模型能区分“memory-derived”与“current verified”；
- 当前用户消息、当前ToolResult和真实系统read覆盖memory；
- stale不是一条隐藏的ranking penalty，而是必要时可展示的信任提示。

#### 2.2.4 高信号不等于“出现次数多”

Codex重点抽取：

- 用户反复纠正或打断后形成的稳定偏好；
- 经过验证的快捷路径和failure shield；
- 能减少未来用户重复输入的行为默认值；
- 真实成功、失败、partial与uncertain outcome。

这对未来automatic extractor很重要：普通session summary不应自动成为长期memory。用户纠正、工具验证和明确采用，比assistant自述“完成了”更强。

#### 2.2.5 外部上下文污染需要显式边界

Codex可在web/tool-search等外部上下文进入thread后标记memory mode polluted。它提醒Pulsara：未来自动提取不能把第三方网页、MCP正文或工具输出中的指令当作用户偏好或agent规则。

Pulsara不必复制Codex的thread标志，但应保留原则：

- third-party content只能成为数据或显式citation；
- 它不能仅因出现在rollout中就提升为ACTION_RULE或Skill；
- 自动提取的trust必须来自canonical source kind，而不是模型自行声称。

### 2.3 不应直接复制的部分

Codex当前pipeline带有较强的completion machinery：claim、lease、retry backoff、global Phase 2 lock、watermark、Git baseline与内部consolidation agent。它适合其全局文件工作区，但不符合Round 8的弱完成目标。

另外：

- free-form `MEMORY.md`同时容纳偏好、repo事实、流程和Skills，边界易模糊；
- `applies_to`主要是文本约定，弱于Round 8的typed USER/WORKSPACE isolation；
- consolidation agent可以语义重写与合并，可能让citation不再支持最终陈述；
- startup自动抽取容易把噪声和prompt injection带入候选；
- 常驻`memory_summary`若每轮重排，会破坏provider prefix；即使Codex自身有其thread边界，Pulsara也不能照搬成动态system prefix。

Pulsara应吸收它的read UX、evidence hierarchy与usage feedback，不吸收其durable pipeline拓扑。

---

## 3. grok-build：显式scope、可读存储、检索退化与prefix纪律

### 3.1 当前形态

grok-build的experimental memory采用人类可读Markdown：

~~~text
~/.grok/memory/
    MEMORY.md                         # global
    <workspace-slug-hash>/
        MEMORY.md                     # workspace
        sessions/*.md                 # episodic logs
        index.sqlite                  # retrieval index
~~~

它提供只读`memory_search`与`memory_get`，支持lexical search、可选vector、source weighting、temporal decay及相关性排序。Session可在结束时写轻量metadata summary，也可以在compaction前让模型执行memory flush；多个session log又可被dream consolidation合并回workspace `MEMORY.md`。

其`/remember`体验允许用户输入原文，模型给出“enhanced”版本，用户在modal里选择保存原文或增强文本。

### 3.2 最值得借鉴的部分

#### 3.2.1 USER/global与exact workspace应是首等产品概念

grok-build把global和workspace在物理目录上清楚分开，临时workspace不写workspace memory。这与Pulsara Round 8已经冻结的`memory_domain_id + USER | WORKSPACE`方向一致。

值得长期保留的不是目录形态，而是用户可理解的可见性：

- USER memory跨同一用户domain下的workspace可见；
- WORKSPACE memory只对exact canonical workspace可见；
- transient session只能看到USER，不得猜测workspace identity；
- Agent只选择产品scope，不接触真实domain/workspace ID。

#### 3.2.2 显式记忆应有清晰的review体验

grok-build的`/remember`让用户看到raw与enhanced版本，这个产品想法很好：用户知道系统准备保存什么，而不是在后台悄悄改写。

Pulsara不能照搬“选中后直接写最终memory”，但未来UI可借鉴为：

~~~text
user asks to remember
    -> show normalized candidate preview
    -> choose scope / optional correction
    -> submit candidate
    -> governance may later accept/skip/supersede/contradict
~~~

用户确认的是candidate内容与scope，不是绕过governance的canonical publish权限。

#### 3.2.3 读取工具比常驻大摘要更可控

`memory_search`返回小片段、source、score、path与行范围；`memory_get`再做精确有界读取。这个形状与Round 8的`memory_search/get/explain`相容：

- search负责发现；
- get负责读取exact accepted item；
- explain负责来源、scope、lifecycle与relations；
- embedding失败时仍可lexical recall；
- index是可重建的访问层，不是memory authority。

#### 3.2.4 Prefix continuity必须覆盖memory recall

grok-build有一条非常直接的cache纪律：一旦memory-context block进入leading system message，后续复用原块，不重新search/re-score，因为改写会破坏整个KV-cache prefix。

Pulsara长期应采用更适合Round 3.1的表达：

- 一次dispatch冻结的memory recall observation不可原地重排或替换；
- 同一epoch只追加新的typed observation；
- 新召回结果只能进入后续suffix；
- compaction/rebase发生时才建立新的cold epoch；
- 不把“最新top K”反复写回旧prefix。

我们不必把memory塞入system message，但必须保留grok-build识别出的缓存不变量。

#### 3.2.5 Automatic extraction需要minimum-signal gate与no-op

grok-build在session end排除synthetic prompt和auto-continue，并对真实用户消息数、正文量设置gate；flush prompt也允许`NO_REPLY`。这说明自动记忆最重要的不是“保证每次都有产物”，而是有能力什么都不写。

未来Pulsara extractor至少应区分：

- synthetic runtime text与真实用户输入；
- routine execution与真正新知识；
-当前状态与长期信息；
- user preference与workspace technical fact；
- current evidence与assistant speculation。

### 3.3 不应直接复制的部分

grok-build把session logs、long-term memory、compaction与dream紧密耦合，带来几个明显风险：

- session end自动summary容易积累噪声；
- flush直接写session memory，绕过candidate/governance；
- dream要求模型“解决矛盾并只保留当前真相”，会删除历史证据和lifecycle；
- `/remember`最终直接append到global `MEMORY.md`，不符合“必须记住仍只能进入candidate”的Round 8边界；
- global/workspace内容被视为evergreen并不可靠，workspace事实同样可能漂移；
- Markdown file path是可读来源，但不等价于typed domain isolation；
- SQLite/FTS/vector/watcher是其文件体系的合理配套，不代表Round 8需要复制双存储。

Pulsara应吸收其scope UX、渐进读取、退化检索、prefix freeze与人类review体验，不吸收dream覆盖写与direct-final-write语义。

---

## 4. Evolver/EvoMap：从经历到经过验证的程序性资产

### 4.1 当前形态

Evolver不是普通的用户/项目记忆库。它扫描日志和signals，选择已有Gene或Capsule，生成protocol-bound evolution prompt，并记录EvolutionEvent。它强调自己是prompt generator，而不是自动code patcher。

其主要资产可粗略理解为：

- **Gene**：适用signals、strategy、validation、constraints、preconditions、anti-patterns与learning history；
- **Capsule**：某次具体策略执行后的summary、outcome、confidence、blast radius、execution trace、环境指纹与成本；
- **Memory graph/outcome history**：记录某个signal下某个Gene的实际结果，影响后续偏好或禁用；
- **candidate/promote边界**：外部资产先进入隔离candidate zone，经显式validated promotion后才进入本地store；
- **EvoMap网络**：允许共享、验证和复用这些资产，但不是离线核心能力的前置条件。

### 4.2 最值得借鉴的部分

#### 4.2.1 程序性记忆必须与普通事实分开

“用户喜欢简短回答”与“遇到某类失败时执行这套修复流程”不是同一种memory。后者会指导未来行动，甚至携带validation commands和tool policy，风险更高。

Pulsara长期应明确：

~~~text
Round 8 MemoryFact / Preference / ActionRule / Decision
    = advisory data

Future Skill / Procedure Asset
    = executable or action-shaping capability
~~~

ACTION_RULE可以表达参考性约束，但不能自动升级为Skill、permission或tool policy。任何程序性晋升都必须经过独立proposal与validation。

#### 4.2.2 选择已有资产优先于不断生成新资产

Evolver的selector先按signal寻找已有Gene/Capsule，只有没有合适候选或旧候选被证明无效时才探索新方案。这比每次session都“总结一个新Skill”更节制。

未来Pulsara若做自动Skill提取，应优先：

1. 找到已有Skill/procedure；
2. 判断本次经历是支持、反驳还是揭示新的适用边界；
3. 更新usage/evidence或提出修订candidate；
4. 只有确有新颖性时才提出新Skill。

#### 4.2.3 真实outcome比模型自评更重要

Evolver把success、failed、inert区分开。其回归测试特别证明：没有产生实际工作的`stable_no_error`不能被当作成功，否则一个无效Gene会因为虚假正反馈长期垄断选择。

这个教训可泛化到长期记忆：

- “模型引用了这条memory”不是“这条memory正确”；
- “没有报错”不是“该procedure有效”；
- usage频率不是质量；
- 用户确认、测试、ToolResult或真实环境验证应具有更高权重；
- repeated failure/inert outcome应降低程序性资产的选择概率或触发退役。

#### 4.2.4 失败知识也应结构化，但不能扩大触发范围

Evolver在成功时可把结构化signal加入Gene matching；失败时记录anti-pattern，而不直接扩大matching。这是一条很好的保守原则：

> **失败告诉系统“何时不要这样做”，并不自动证明另一个更宽泛规则。**

未来Pulsara可把failure shield、stop rule、verification checklist作为Skill evidence，但失败记录不能直接修改USER/WORKSPACE memory statement或自动生成更强的ACTION_RULE。

#### 4.2.5 外部可复用资产必须先隔离、验证、再promote

Evolver对外部Gene/Capsule使用candidate zone，并要求显式validated promotion；验证命令还受安全检查。若未来Pulsara支持Skill marketplace、跨用户memory export或EvoMap式共享，必须借鉴这个信任边界：

- remote asset默认是untrusted data；
- download/import不等于启用；
- validation与promotion分离；
- 外部资产不能覆盖同ID本地资产；
- 可执行字段必须单独授权和审计。

### 4.3 不应直接复制的部分

Evolver的目标是agent evolution，而不是用户可解释的事实库。以下机制不应进入Round 8：

- Gene/Capsule、Mutation、Personality或EvolutionEvent表；
- 为普通memory构建fitness、reputation、leaderboard或网络共识；
- 自动修改prompt/tool policy；
- 把一次成功Capsule直接当作跨workspace通用策略；
- 把Hub购买、广播、worker pool和validator变成memory写入前置；
- 仅凭signal匹配和outcome score决定事实真伪。

Evolver最适合启发未来“程序性学习与Skill演化”阶段，而不是扩张Round 8 schema。

---

## 5. 三者的可借鉴点对照

| 主题 | Codex | grok-build | Evolver/EvoMap | Pulsara建议方向 |
|---|---|---|---|---|
| 显式记忆入口 | append-only ad-hoc note，最终memory由后续pipeline处理 | raw/enhanced review后直接append | 不以用户事实记忆为主 | 任何“必须记住”都只提交candidate |
| Scope | 主要靠cwd/`applies_to`文本 | 物理global/workspace目录 | workspace-local assets + optional network | typed `memory_domain_id + USER \| WORKSPACE` |
| 来源 | rollout、summary、citation | file/line/session source | signal、event、execution trace | producer provenance + exact typed citations |
| Recall | summary→handbook→evidence/skill | search→get，首次自动注入 | signal selector→Gene/Capsule | search→get/explain；默认小结果 |
| Freshness | 明确要求验证与披露stale | temporal decay/staleness note | outcome history影响选择 | advisory标记；当前事实永远覆盖memory |
| No-op | 高信号gate，明确偏好不写 | short session gate、NO_REPLY | 无匹配/旧资产失效时才探索 | governance可SKIP；未来extractor默认允许空结果 |
| Feedback | memory citation回写usage | source weighting/decay | success/fail/inert、ban与learning history | 分开retrieved/materialized/cited/confirmed |
| Consolidation | 两阶段模型重写全局文件 | dream覆盖workspace memory | solidify验证程序性资产 | semantic memory不被模型重写；未来合并只能提candidate |
| Prefix | summary注入由thread配置管理 | 已注入memory block复用，不重排 | 非核心关注点 | Round 3.1 epoch内append-only；rebase才冷启动 |
| 程序性学习 | 可生成skills | memory中包含workflow但边界较松 | Gene/Capsule是核心 | 独立future Skill proposal/validation plane |
| 外部内容 | third-party当数据，external-context fence | session/tool内容进入flush，风险较高 | external assets candidate + promote | untrusted input不直接晋升；remote capability另行验证 |

---

## 6. Pulsara未来长期记忆的建议分层

本节只是讨论框架，不冻结物理表。

### 6.1 Layer A：Canonical evidence

已有conversation rows、ToolResult、artifact、Plan和external-source acceptance继续拥有“实际发生过什么”。Memory不得复制或替代这些authority。

Memory citation应指向它们；若正文过大，应保留ID/handle与bounded preview，而不是复制全文。

### 6.2 Layer B：Advisory semantic memory

Round 8负责此层：

- FACT、PREFERENCE、ACTION_RULE、DECISION；
- USER与WORKSPACE scope；
- candidate-first；
- Host-local best-effort governance；
- statement不被governance语义改写；
- BASED_ON只引用既有canonical memory；
- SUPERSEDES/CONTRADICTS保留lifecycle与冲突；
- FTS同步可查，vector可缺失；
- 不保证完整、新鲜或最终处理。

该层的长期职责是“向未来模型展示可能有用的数据”，不是训练agent，也不是完成compaction。

### 6.3 Layer C：Episodic evidence products

未来automatic extraction或Round 5B可能需要episodic summary，但它不应默认成为MemoryFact。可能的产品用途包括：

- 解释一条memory来自哪段session；
- compaction后恢复尚未固化的工作上下文；
- 为procedure proposal提供failure/success evidence；
- 为用户review提供“当时发生了什么”。

关键边界：

- raw transcript仍是canonical evidence；
- summary是可丢弃派生物或candidate输入；
- 模型生成summary不得直接发布FACT/PREFERENCE；
- deletion/retention由conversation与privacy policy决定，不能由memory subsystem暗自复制无限期保留；
-是否需要独立episodic store，必须等compaction与privacy设计共同确定。

### 6.4 Layer D：Procedural assets / Skills

未来可从重复的成功、失败与用户纠正中提出Skill candidate，但必须独立于Round 8治理：

~~~text
evidence set
    -> procedure proposal
    -> scope/precondition/tool requirement review
    -> validation / shadow use
    -> promoted Skill
    -> usage + outcome feedback
    -> revise / disable / retire
~~~

程序性资产至少需要：

- exact scope与preconditions；
- 被允许调用的tools/capabilities；
- validation或可观察成功标准；
- failure/anti-pattern；
- lineage到支撑它的evidence；
- 与permission/Plan的动态交集；
- 不成功、无效果或过期时可disable。

它不应通过给`memory_facts`增加更多kind来实现。

### 6.5 Retrieval与presentation只做访问层

未来可采用FTS、vector、hybrid ranking、summary index、temporal decay与usage feedback，但都必须满足：

- 删除index不丢semantic memory；
- index未追平只降低召回，不改变事实；
- ranking不生成SUPERSEDES/BASED_ON等关系；
- provider看到的结果必须标明scope、trust与staleness；
- 大正文通过handle渐进读取；
- 同一provider epoch内不重写已安装的memory prefix。

---

## 7. 建议的长期生命周期

### 7.1 Capture：所有入口汇入candidate，而不是多套publish路径

未来可能有四种入口：

~~~text
explicit remember tool
automatic post-turn/session extractor
compaction memory extraction
human import/edit proposal
~~~

它们可以使用不同source metadata，但都只能生成同一类candidate。不能出现：

- 显式记忆可直接publish；
- compaction summary绕过governance；
- automatic extractor拥有独立memory表；
-人工编辑文件后静默覆盖canonical statement。

如果用户直接说“现在必须记住”，产品可确认candidate已经接受，但不能承诺已成为最终memory。

### 7.2 Governance：分类与生命周期，而不是再创作

Round 8已经选择的克制方向应成为长期默认：

- deterministic normalization可以改变编码，不改变含义；
- governance可SKIP、ACCEPT、SUPERSEDE或CONTRADICT；
- governance不得把弱陈述改成更强陈述；
- merge/correct应生成新的human/agent-visible candidate，而不是后台悄悄改写；
- citation必须仍能支撑最终statement；
-处理失败可以永远不重试，不能阻塞foreground。

### 7.3 Recall：发现、展开、解释分开

推荐长期保持三个读取动作：

~~~text
memory_search   # 找候选，返回短结果
memory_get      # 读exact accepted item
memory_explain  # 读producer、citation、scope、lifecycle、relations
~~~

未来episodic或Skill读取可以有自己的tool，不应让`memory_get`变成通用文件系统或图查询语言。

### 7.4 Use feedback：记录“被使用”，但不把它误当正确性

可以讨论的feedback层级：

1. search返回；
2. compiler选择并物化；
3. 模型显式引用；
4. 当前ToolResult/用户反馈验证；
5. 后续事实反驳；
6. procedure产生真实成功、失败或inert结果。

前两层只适合优化ranking；第三层表示相关性；第四、五层才接近语义质量；第六层只适用于程序性资产。

不应建立一个跨所有memory kind的通用confidence分数。

### 7.5 Reconsideration：纠正与遗忘需要显式产品语义

当前Round 8未设计forget/delete。长期仍需单独讨论：

- 用户纠正错误memory；
- 用户要求忘记或删除个人信息；
- workspace被删除或identity变化；
- citation随conversation retention消失；
- import/export和跨domain移动；
- SUPERSEDED数据保留多久；
- privacy deletion是否必须级联到artifact/index/backup。

这些不能由temporal decay、低usage或dream consolidation代替。

---

## 8. 与Prompt Compiler和prefix continuity的长期契约

Memory一旦进入provider input，就必须服从Round 3/3.1，而不是拥有例外。

建议继续冻结：

1. memory是独立typed advisory source，不拼入BASE_SYSTEM；
2. 每次dispatch在one-cut中冻结exact recall fact；
3. 已安装observation不可重排、替换或因rerank而删除；
4. 新memory、supersede、contradiction或staleness correction只形成suffix；
5. 同一epoch内SYSTEM与tool surface不因memory变化而改变；
6. compaction/rebase建立新的cold epoch，允许用新的memory snapshot重建；
7. 召回失败或vector不可用应变成ABSENT/lexical fallback，而不是改变历史prefix；
8. source body不暴露内部contract、fingerprint、generation、database ID或ranking debug字段；
9. opaque statement/citation正文使用closed carrier，不能逃逸runtime observation envelope。

grok-build的“已经注入就原样复用”是值得保留的cache直觉；Pulsara通过process-local epoch和append-only observation把它表达得更严格。

---

## 9. 暂定阶段地图

这不是承诺顺序，只用于避免把未来功能塞回Round 8。

### 9.1 Round 8：advisory semantic memory

只实现：

- explicit `remember` candidate；
- best-effort governance；
- typed USER/WORKSPACE accepted items；
- lexical recall、optional vector；
- get/explain与citation；
- weak completion与advisory trust。

本草案不要求Round 8新增usage telemetry、episodic summary、automatic extraction、Skill或compaction integration。

### 9.2 Round 8.x候选：recall体验与使用反馈

后续可单独讨论：

- 渐进披露；
- staleness-aware provider presentation；
- retrieved/materialized/cited/confirmed分层；
- 用户可见的candidate状态与scope review；
- 搜索结果如何保持prefix continuity。

### 9.3 Round 5B/未来compaction：只提出candidate

Compaction可从即将丢失的context中提出memory candidate，但：

- compaction成功不依赖candidate处理；
- candidate失败不否定compaction；
- summary不直接publish；
- memory抽取与context rebase使用不同的成功标准；
- 未来模型偏好提取仍必须经过source、scope和governance。

### 9.4 Future automatic extraction：先做quality gate，再做调度

优先研究：

- 真实用户纠正与ToolResult证据如何绑定；
- no-op、partial、uncertain、failure如何表达；
- third-party/MCP/web内容如何隔离；
- 如何避免重复candidate；
- 如何让automatic extraction保持best effort。

不要先恢复durable extraction job、claim、lease、retry和watermark。

### 9.5 Future procedural learning：独立Skill proposal plane

当Plan、subagent、memory与compaction边界都稳定后，再讨论：

- 从重复成功和失败中提Skill proposal；
- shadow/validation；
- outcome feedback与inert检测；
- scope/precondition；
-用户review与promotion；
- 外部Skill import/marketplace信任边界。

这才是Evolver/EvoMap经验真正适用的阶段。

---

## 10. 明确拒绝的合并方案

后续设计中应默认拒绝以下捷径：

1. 用一张`memory_nodes`表同时装事实、session summary、Skill和外部asset。
2. 用户说“必须记住”时直接写accepted memory。
3. 让governance或dream模型重写statement并保留旧citation。
4. 把assistant生成的session summary当作已验证FACT。
5. 用vector相似度建立BASED_ON、SUPERSEDES或CONTRADICTS。
6. 用usage count或“没有报错”证明memory/procedure正确。
7. 把memory中的ACTION_RULE接入permission、Plan或tool authorization。
8. 每轮rerank后回写system prompt中的memory block。
9. 为了最终处理所有candidate恢复durable retry/recovery graph。
10. 把EvoMap式网络validator、reputation和asset marketplace设成local memory前置。
11. 让compaction对memory成功负责，或让memory失败阻断compaction。
12. 用自动consolidation代替用户可执行的纠正、forget与privacy deletion。

---

## 11. 后续讨论问题

在Round 8收口后，可以按以下问题逐项讨论，而不是一次设计完整“记忆平台”：

### 11.1 Recall

- 默认是否只由模型显式调用`memory_search`，还是compiler可自动召回？
- 自动召回的trigger、预算、source lifecycle和prefix行为是什么？
- 是否需要把“memory-derived但未现场验证”显式呈现给用户？

### 11.2 Usage与quality

- 哪些usage信号值得保存，哪些只做process-local telemetry？
- 用户纠正是否应自动提出contradiction/supersede candidate？
- 如何区分“相关但错误”与“不相关”？

### 11.3 Human control

- candidate preview是否允许用户修改statement与scope？
- accepted memory如何纠正、supersede、forget或导出？
- 人工编辑是否生成candidate，而不是直接改canonical row？

### 11.4 Automatic extraction

- 触发点是turn end、session end、compaction前，还是只接受显式请求？
- 哪种evidence足以提出PREFERENCE、ACTION_RULE或DECISION？
- 如何避免assistant proposal被误当user intent？

### 11.5 Episodic memory

- Round 5B是否真的需要独立session summary store？
- canonical transcript + artifact handle是否已经足够？
- retention与privacy deletion如何覆盖summary副本？

### 11.6 Procedural learning

- 什么时候一条ACTION_RULE值得升级为Skill proposal？
- validation由真实task、shadow call、fixture还是用户批准承担？
- Skill的USER/WORKSPACE scope与permission snapshot如何交叉？
- 如何检测inert Skill并阻止其因高usage垄断选择？

### 11.7 Sharing

- USER memory是否允许显式export/import？
- 外部memory/Skill进入本地后采用何种candidate与trust等级？
- network reputation是否只能作为筛选信号，而不能替代本地验证？

---

## 12. 对当前Round 8的影响口径

本文对当前主线只有三条约束性提醒，但不直接修改Round 8：

1. Round 8只拥有advisory semantic memory，不为episodic summary或Skill预留通用node vocabulary。
2. 所有未来写入来源都应复用candidate边界；因此当前显式`remember`不能形成direct-final-write特例。
3. Round 8的accepted item、citation和scope需要足够诚实，使未来recall、compaction和procedure proposal可以引用它们，但不必提前实现这些future consumers。

reviewer对Round 8提出的finding应在Round 8规格中独立收口。不得为了“与本草案一致”在当前轮次新增relation、event、job、guard、usage ledger、episodic store或Skill registry。

---

## 13. 暂定结论

三者各自最值得Pulsara长期吸收的一句话是：

- **Codex**：把memory做成有证据、可渐进展开、可观察实际使用情况的辅助知识，而不是一段不可追溯的全局prompt。
- **grok-build**：让global/workspace scope、search/get、staleness与prefix freeze成为真实产品行为，同时保留用户可理解的review体验。
- **Evolver/EvoMap**：当经验要改变agent行为时，必须升级为另一类有precondition、validation、outcome和退役机制的程序性资产。

Pulsara的差异化方向应是：

> **以Round 8的typed、scoped、candidate-first advisory memory作为语义底座；以canonical conversation作为证据底座；未来再让episodic extraction和程序性Skill从这两者上生长，但永远不把三者压回一套万能memory authority。**

---

## 14. 本地证据索引

以下路径均相对于第0节记录的对应仓库提交。

### 14.1 Codex

- `codex-rs/memories/README.md:29-157`：两阶段pipeline、claim/lease、selection、consolidation与workspace diff。
- `codex-rs/memories/write/templates/memories/stage_one_system.md:15-125`：evidence hierarchy、no-op gate、偏好与程序性知识抽取原则。
- `codex-rs/memories/write/templates/memories/consolidation.md:17-230`：progressive-disclosure文件结构、Skill与handbook consolidation。
- `codex-rs/ext/memories/templates/memories/read_path.md:19-123`：summary→registry→evidence/Skill读取、staleness披露、citation与ad-hoc note边界。
- `codex-rs/core/src/stream_events_utils.rs:162-216`：external-context污染标记与memory citation usage记录。
- `codex-rs/ext/memories/src/tools/ad_hoc_note.rs:22-79`：用户明确要求后只能创建append-only note。
- `codex-rs/features/src/lib.rs:925-930`：feature状态与默认开关。

### 14.2 grok-build

- `crates/codegen/xai-grok-memory/src/lib.rs:1-108`：global/workspace/session布局与optional embedding退化。
- `crates/codegen/xai-grok-memory/src/storage.rs:11-35,119-240`：scope、ephemeral workspace、human-readable files与写入入口。
- `crates/codegen/xai-grok-shell/src/session/helpers/memory_context.rs:1-64`：memory recall展示与已安装prefix原样复用。
- `crates/codegen/xai-grok-shell/src/session/memory/hooks.rs:1-146`：session-end best-effort、minimum-signal gate与无LLM metadata summary。
- `crates/codegen/xai-grok-shell/src/session/helpers/memory_flush.rs:1-195`：pre-compaction flush、NO_REPLY与质量gate。
- `crates/codegen/xai-grok-memory/src/dream.rs:32-180`：dream gates、merge/resolve语义与bounded input。
- `crates/codegen/xai-grok-tools/src/implementations/memory/search_tool.rs:22-108`：只读search与staleness presentation。
- `crates/codegen/xai-grok-tools/src/implementations/memory/get_tool.rs:33-105`：渐进exact读取。
- `crates/codegen/xai-grok-pager/src/app/dispatch/notes.rs`及相邻effects：`/remember` raw/enhanced review体验。

### 14.3 Evolver/EvoMap

- `README.md:198-250`：Evolver的prompt-generator边界、signal scan、asset selection与EvolutionEvent。
- `README.md:390-414`：Gene/Capsule/Event本地asset store与恢复边界。
- `README.md:484-508`：validation command安全与external asset candidate/promotion。
- `src/gep/schemas/gene.js`：signal、strategy、validation、constraints、learning history、anti-pattern与可选tool policy。
- `src/gep/schemas/capsule.js`：outcome、confidence、blast radius、execution trace、environment fingerprint与derivation cost。
- `test/solidifyLearning.test.js`：成功扩展matching signal、失败只记录anti-pattern的行为。
- `test/memoryGraph.test.js`：outcome history、失败ban与drift不能绕过ban。
- `test/issue562InertGeneBan.test.js`：inert outcome不能积累虚假confidence。
- `test/memoryFiltering.test.js`：对recent successful outcomes的有界筛选。
