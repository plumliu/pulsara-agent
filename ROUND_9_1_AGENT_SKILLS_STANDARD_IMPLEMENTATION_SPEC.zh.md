# Round 9.1：Agent Skills Standard 与 Append-only Skill Capability 实施规格

> 状态：**DRAFT — NOT ACTIVATED**
>
> 记录日期：2026-08-17
>
> 当前代码基线：`ffd0d146f8d7991ff3d1e92dc9ca75e8abf894e8`
>
> hard-cut 前参考基线：`5b7ad9f7ffc8565bc572180b2bde0c81ab64473a`
>
> Codex 本地参考基线：`6138909d6ec58b2fbe635ef973e02caecad5a5aa`
>
> grok-build 本地参考基线：`c68e39f60462f28d9be5e683d9cbe2c57b1a5027`
>
> 上位契约：[Round 3 structured compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 provider-input prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 6 MCP](ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md)、[Round 7.1 provider-visible ToolResult projection](ROUND_7_1_PROVIDER_VISIBLE_TOOL_RESULT_PROJECTION_IMPLEMENTATION_SPEC.zh.md)、[Round 9 unified capability semantics](ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md)、[Gap Index](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 下游设计：[Round 5B compaction](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)；Round 9.2 Plugin capability bundle 尚未起草

本文只实施 Pulsara 在 hard-cut 后的 **Agent Skills 标准**、dynamic skill与append-only skill exposure。它必须在 Round 7.1 的normal ToolResult projection以及 Round 9 的统一 source registration、frozen capability registry、semantic fact、MCP direct/meta route与纯 exposure planner均激活后实施；它也是未来 Round 5B 的编码前置，但**不实施 capability ontology、MCP meta gateway、Plugin、compaction summary、adoption、rebase、successor epoch installation或任何 compaction transaction**。

本文把 Anthropic 发布并迁移到 [Agent Skills specification](https://agentskills.io/specification) 的开放契约视为 Pulsara skill 的**唯一规范格式**，而不是额外兼容格式：

- `SKILL.md` frontmatter、name/directory约束、可选字段与progressive disclosure以该标准为准；
- Anthropic Claude Code、Kimi Code或其他宿主的额外字段不得冒充开放标准字段，也不在Pulsara V1获得行为语义；
- Pulsara不得继续把自有字段放入frontmatter，也不得在标准`metadata`中建立`pulsara.*`依赖语言；
- clean-v0允许直接移除旧Pulsara私有顶层语义，不建立双读、自动迁移或长期compatibility parser。

本文不新增 `load_skill` / `skill_view` / `skill_use` provider tool，不把每个 skill 注册成 tool，不恢复 hard-cut 前的 durable capability exposure snapshot、event chain、receipt、checkpoint、repair graph或跨 Host skill generation。

---

## 0. 执行结论

以下ontology、source-registration/registry contract与direct/meta route均是Round 9的上位输入，不是本轮重新定义的第二套capability contract。Skill 是 **prompt/context capability**，不是 executable tool。MCP tool 是带 schema、permission、effect 与 physical executor 的执行能力；`SKILL.md` 是本地、可变、低 authority 的指导数据。Built-in、MCP与Skill已经统一：

1. source registration、refresh policy与complete source snapshot；
2. common leaf admission与immutable registry snapshot；
3. model exposure 与 local authorization/execution 分层；
4. continuity epoch 内 append-only，并为未来cold boundary提供可冻结的semantic input。

二者不统一物理调用方式：

~~~text
MCP tool
  -> DIRECT native provider tool
  -> 或 inspect_new_mcp_tool + use_new_mcp_tool meta route
  -> Runtime exact authorize / attempt / invoke

Skill
  -> SKILL_CATALOG routing entry
  -> read_file(exact listed SKILL.md, intent=ACTIVATE_SKILL)
  -> read scripts/references/assets on demand
  -> 模型按正文使用既有 builtin/MCP/terminal tools
  -> skill 本身不产生 execution authority
~~~

同一 Host、同一 exact scope、同一 provider-input continuity epoch 必须继续满足：

~~~text
SYSTEM[n + 1]   == SYSTEM[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
~~~

因此：

- Host/continuity epoch 开始时冻结完整当前 skill semantic catalog；每个admitted Skill的`name + description + location`必须以唯一、完整、byte-exact表示进入catalog，compiler不得缩短routing metadata；
- epoch 中新增、修改、删除或变为不可用的 skill，只能通过新的 `SKILL_CATALOG` SNAPSHOT observation 追加表达；
- `$skill`、`skill:name` 或 Host configured skill 形成 `ACTIVE_SKILL` activation snapshot；同一 activation 内不因文件变化静默改写；
- 模型因任务匹配选择skill时，必须显式使用`read_file(intent=ACTIVATE_SKILL)`；该调用只能命中本次provider实际生效的`SKILL_CATALOG` head，ordinary read不会建立隐藏的“loaded skill”状态；
- 下一条真实 activation boundary 才从当前文件系统重新解析 active body；
- BASE_SYSTEM 不拼接动态 skill 内容；provider `tools` 不因 skill 安装、修改或删除而变化。

本文完成后，Round 5B可以把同一份frozen catalog/active source输入作为其下游输入；如何在history reset之后消费它们，完全不属于本文实现或activation gate。

### 0.1 最终产品路径

~~~text
Host/cold epoch
  -> compose six default SAFE_POINT_REFRESHABLE Skill root registrations
  -> exact join each registration to one process-local root binding
  -> bounded scan registered Agent Skills roots
  -> validate standard frontmatter and directory name without pre-reading resources
  -> freeze complete per-root source snapshots and enter Round 9 registry
  -> freeze exact manifest bytes and portable standard fields
  -> compile SKILL_CATALOG snapshot

new/changed/removed skill inside epoch
  -> next complete tool-batch/provider safe point re-scan
  -> build successor Skill source snapshots and one successor capability registry
  -> same semantic snapshot: no-op
  -> changed semantic snapshot: append successor SKILL_CATALOG
  -> never rewrite SYSTEM/tools/history

model selects an implicit skill
  -> read_file(catalog location, intent=ACTIVATE_SKILL), reading the complete SKILL.md
  -> complete ToolResult itself carries the exact skill once
  -> optionally search_files for referenced material
  -> execute existing tools under their ordinary permission/effect contracts

explicit/configured activation
  -> append ACTIVE_SKILL VALUE with exact frozen body
  -> same tool loop/automatic continuation inherits that head
  -> next true activation boundary replaces or clears it
~~~

### 0.2 为什么最终不增加 `load_skill`

当前 `read_file` 已支持：

- workspace-relative path；
- absolute path；
- `~` 下的 host-local ordinary UTF-8 file；
- line pagination、100,000-character result bound、binary/device rejection与canonical ToolResult。

本轮只给现有`read_file`增加closed intent：`ORDINARY | ACTIVATE_SKILL`。当前catalog会明确告诉模型，采用skill时使用`ACTIVATE_SKILL`完整读取；只是检查文件时使用`ORDINARY`。新增独立`load_skill`仍只会产生：

- 两条读取同一正文的模型路径；
- 永久增加的 provider tool schema；
- `read_file` 与 `load_skill` 两套截断、artifact、permission与错误语义；
- “读过”是否等于“激活”的隐藏状态机；
- 后续任何history reset是否要追踪所有历史load的额外问题。

因此 normative 分工是：

| 需求 | 唯一路径 |
|---|---|
| 发现可用 skill | `SKILL_CATALOG` |
| 采用并完整读取 `SKILL.md` | `read_file(intent=ACTIVATE_SKILL)` |
| 普通检查/比较 `SKILL.md` | `read_file(intent=ORDINARY)` |
| 读取/搜索 `scripts/`、`references/`、`assets/` | `read_file` / `search_files` |
| 执行命令 | `terminal` / `terminal_process` |
| 调用 direct MCP | native MCP tool |
| 调用 epoch 中新发现的 MCP | `inspect_new_mcp_tool` + `use_new_mcp_tool` |
| 显式激活完整正文 | Runtime `ACTIVE_SKILL` source |

`terminal` + `rg` 可以作为复杂内容搜索的普通工具，但不得成为加载 `SKILL.md` 的标准路径：搜索片段不能证明正文已完整阅读，并会不必要地扩大 shell 动作空间。

---

## 1. 范围、非目标与 authority

### 1.1 本轮恢复

- Agent Skills standard `SKILL.md` parser与directory/resource contract；
- workspace skill roots：`.pulsara/skills`、`.agents/skills`、`.claude/skills`；
- user skill roots：`${PULSARA_HOME}/skills`（默认 `~/.pulsara/skills`）、`~/.agents/skills`、`~/.claude/skills`；
- catalog 在同 epoch 中响应新增、修改、删除与invalid/unavailable变化；
- configured 与显式 textual skill activation；
- ROOT initial prompt、ordered steer batch、tool loop、Plan automatic continuation和child scope的 closed activation matrix；
- bounded discovery、render、compiler、prefix 与跨 scope tests；
- inspector/diagnostic 能解释 skill 来源、standard validity与不可用原因。

### 1.2 明确非目标

- 不把 skill 变成 callable tool；
- 不增加 `load_skill`、`skill_view`、`skill_use`；
- 不让 skill frontmatter修改 permission preset、tool schema、MCP effect或sandbox；
- 不实现任何Skill声明驱动的tool authorization或permission mutation；
- 不从Skill frontmatter或正文构造Tool/MCP/CLI dependency graph；
- 不扫描正文猜测binary、MCP server、tool name、network或auth需求；
- 不把Claude Code host extensions误称为Agent Skills core；
- 不执行Claude Code动态`!command`预处理，不允许skill load本身产生shell effect；
- 不扫描Codex/Claude/plugin manager的私有cache目录；插件若要供Pulsara使用，必须把skill安装到一个声明root；
- 不建立 session-pinned implicit skill 集；
- 不持久化“模型曾经读过哪些 skill”；
- 不自动把普通 `read_file(intent=ORDINARY)`解释为Runtime activation；
- 不做 embedding/BM25 skill search；
- 不为 skill 建 durable generation、relation、event、job、receipt或repair owner；
- 不实现compaction summary、snapshot adoption、rebase、successor dry compile或epoch installation；
- 不新增任何compaction专用skill DTO、borrow、fence或test owner；
- 不承诺同一 turn 内外部文件修改立即抢占正在进行的 provider stream。

### 1.3 Authority 梯度

~~~text
BASE_SYSTEM / developer policy
    > current explicit user request
    > Runtime permission / Plan / tool authorization
    > ACTIVE_SKILL and SKILL_CATALOG guidance
    > arbitrary tool/web/MCP result data
~~~

这里的 `>` 表示冲突优先级，不表示 skill 是可信业务事实。Skill 可以指导工作流，但：

- 不能授予工具；
- 不能把 unavailable tool变成 callable；
- 不能绕过 approval；
- 不能覆盖当前用户明确要求；
- 不能把描述中的命令当成已经执行的事实；
- 不能把 MCP/server annotation提升为本地 policy。

---

## 2. 三套 prior art 的批判性吸收

### 2.1 hard-cut 前 Pulsara

旧实现的正确核心不是 durable exposure graph，而是四层分离：

~~~text
Registry / Discovery
    -> Exposure / Advertisement
    -> Gate / Policy
    -> Execution / Artifact / Trace
~~~

`CapabilityExposurePlan` 同时描述 direct/deferred/hidden/callable tool、skill catalog与active injection。它正确证明：

- skill catalog 与 active injection可以和 tool exposure来自同一个 semantic planning cut；
- descriptor不等于executor；
- skill引用工具不能创建schema或权限；
- continuation只能exact reuse或monotonic narrowing，不能静默widen；
- full `SKILL.md` 可以由active injection或普通file tool承载。

本文保留这些语义，但删除旧实现的以下形状：

- durable capability exposure fact/event/artifact；
- run working set中的original/effective exposure lineage；
- continuation exposure receipt与recovery；
-跨 Host registry generation；
-为了证明prompt内容而持久化完整projection artifact。

当前 Round 3/3.1 的 compiler source head、continuity epoch与tool-surface borrow已经是更小的替代品。

### 2.2 Codex

Codex 的 `HostSkillsSnapshot` 在创建 `TurnContext` 时冻结，并在该 turn 的显式 skill mention与skill body injection中复用。值得吸收：

- 一个执行上下文使用一份immutable skill snapshot；
- explicit mention读取同一snapshot，避免一次turn内重复扫描得到不同正文；
- skill body作为context item进入history，而不是每个skill变成tool；
- cache invalidation只决定下一snapshot，不改写已经运行的turn。

Codex当前把OpenAI自己的interface、dependency与policy放在独立`agents/openai.yaml` sidecar，而不是扩张portable `SKILL.md`。这证明宿主或发行方确有额外组合需求时，应使用**可选、带明确owner的外部bundle/sidecar**；它不能成为第三方Skill被Pulsara发现或激活的前置条件。Round 9.1不实现该sidecar，future Plugin可在bundle manifest中分别声明Skill roots与MCP/安装配置。

不照搬：

- Codex可在新turn重建developer/system context；Pulsara同epoch必须保持SYSTEM完全相等；
- Codex的rollout replacement history不是Pulsara canonical relational transcript的需要；
- Codex的plugin/app/MCP安装模型与Pulsara current MCP supervisor、permission和effect contract不同；
- 不以 Codex 的 host snapshot 为理由建立 durable skill snapshot。

### 2.3 grok-build

grok-build 的 `SkillManager` 保存startup与dynamic discovered skills。file tool之后发现新的 `SKILL.md` 时，它把skill listing作为user-role system reminder追加；compaction时使用同一个renderer重新列出startup + discovered skills。

值得吸收：

- dynamic discovery只在完整tool result之后形成提醒，不插入并行tool batch中间；
-同一skill按canonical path/name去重；
- catalog正文有独立预算；
- 在history reset后使用同一个renderer重建完整current semantic catalog，而不是依赖旧delta；这只是值得保留的下游经验，不是本文实施项；
- conditional/path-related discovery可以作为未来优化，但不能改变基础 correctness。

不照搬：

- 不增加专用 `Skill` tool；Pulsara已有更完整的 `read_file`；
- 不持久化announced-name set来恢复prompt；Pulsara由source head与canonical snapshot重建；
- 不把“system-reminder”伪装成真实system role；Pulsara使用typed runtime observation；
- 不依赖仅path去重而忽略body/metadata变化；
- 不让全局manager成为tool permission或execution owner。

Kimi Code提供了同样重要的反例：其parser能接受`whenToUse`、`disableModelInvocation`、skill type等宿主扩展，但仓库中的实际Skill几乎全部只使用`name + description`；官方datasource Skill把MCP工具使用方法写在description/body中，MCP server则由plugin manifest独立安装。本文吸收这种“Skill负责指导，bundle负责组成”的边界，不把Kimi的宿主字段升级成Pulsara contract。

### 2.4 Anthropic Agent Skills 标准

Anthropic 的skills仓库已经明确把格式规范迁移到Agent Skills开放标准；Claude Code与Agent SDK文档又证明：**portable skill contract**与**host-specific execution policy**必须分开。

本文完整吸收的标准部分：

- 每个skill是`<skill-name>/SKILL.md`目录；
- frontmatter必填`name`与`description`；
- 可选`license`、`compatibility`与`metadata`；
- `name`为1–64字符、小写字母/数字/单连字符，不得首尾或连续连字符，并且必须与parent directory name完全相等；
- `description`为1–1024字符，承担“做什么 + 何时使用”的routing语义；
- `compatibility`最多500字符；`metadata`必须是string-to-string mapping；
- 可选`./scripts/`、`./references/`、`./assets/`和其他相对资源按需读取；
- progressive disclosure固定为：catalog只暴露bounded metadata，activation时读取完整`SKILL.md`，引用资源仅在需要时读取。

本文不把以下宿主能力伪装成portable standard：`when_to_use`、`disable-model-invocation`、`user-invocable`、`argument-hint`、`allowed-tools`、`model`选择、subagent `context`、hooks、动态`!command`注入和任意host command expansion。它们可以存在于原始YAML中，但不得产生Pulsara Runtime行为、依赖边、permission变化或provider catalog字段。

Agent Skills规范中的实验性host permission字段不属于Pulsara V1的行为契约；完整边界见§4.3。

### 2.5 最终取舍表

| 问题 | 旧 Pulsara | Codex | grok-build | Pulsara 最终方案 |
|---|---|---|---|---|
| turn/run一致性 | exposure reuse/narrow | turn snapshot | session manager | one-call frozen cut + epoch source head |
| dynamic install |新run才widen |下一turn snapshot |追加reminder |同epoch追加catalog SNAPSHOT |
| skill body | active injection/read | explicit injection |专用Skill tool | textual/configured activation或`read_file(intent=ACTIVATE_SKILL)` |
| Skill引用外部能力 |统一descriptor surface | optional sidecar/bundle |正文指导 + plugin manifest |正文只提供指导；真实能力由Builtin/MCP/Terminal owner拥有 |
| manifest contract | Pulsara私有frontmatter | Agent Skills + optional sidecar |实际Skill主要是name/description | Agent Skills portable core是唯一文件契约；无Pulsara扩展 |
| progressive resources |普通file tool | scripts/references/assets按需加载 |专用Skill tool | `read_file`/`search_files`按需读取标准目录资源 |
| future history reset兼容性 |旧体系复杂 |沿用turn context |完整semantic catalog reinjection |只留下可复用的frozen semantic input；具体reset由下游规格决定 |
| durability |过重 |rollout-owned |session state |无新增durable authority |

---

## 3. 当前代码真值、Round 9 前置与必须修复的缺口

### 3.1 已经正确的部分

当前基线已经拥有旧命名的 `FrozenKernelCapabilityProjectionInput`、`KernelCapabilityComposer` 与 `CAPABILITY_CATALOG`；它们实际只处理Skill。Round 9必须在本轮编码开始前把这三者分别收窄为`FrozenSkillProjectionInput`、`KernelSkillProjectionComposer`与`SKILL_CATALOG`，并提供统一的source registration、`FrozenCapabilityRegistrySnapshot`与`FrozenCapabilityPlanningCut`。本轮不得自己兼容两套名称，也不得在Round 9未激活时临时复制其DTO。

完成该前置后，本轮可以直接复用：

- `LocalSkillProvider` 的四个legacy root、UTF-8读取、64 KiB/file bound、symlink containment与deterministic root/name precedence；
- `FrozenSkillProjectionInput`，覆盖skill discovery metadata/content digest并引用registry-owned Skill source snapshots；
- `SKILL_CATALOG`：`IMPORTANT + SNAPSHOT_ON_CHANGE`；
- `ACTIVE_SKILL`：`MUST_KEEP + ACTIVATION_SNAPSHOT`；
- same-turn tool loop用`NOT_APPLICABLE`保持active head；
- catalog唯一的完整`name + description + location`表示，以及`CLEARED | UNAVAILABLE`状态；
- `$name` / `skill:name` textual activation；
- configured active skill；
- `read_file` 对workspace/absolute/`~`普通文本的读取；
- Round 3.1 compatible append与stateful source invalidation；
- continuity owner已经拥有可被未来下游读取的frozen source head。

### 3.2 当前真实缺口

1. 旧 `LocalSkillProvider` 解析并传播`provides_tools`、`suggested_tools`、binary/service/auth等Pulsara私有字段；这些字段没有portable author，必须整体删除而不是迁移到新的namespace。
2. Round 9只把旧Skill输出适配为最小`FrozenSkillCapabilityFact`，不会替本文实现Agent Skills parser或dynamic activation；这些仍是本轮职责。
3. ordinary provider planning尚未把“不重新解释filesystem”的exact frozen skill projection seam冻结成明确公共契约。
4. 没有标准的model-invoked activation seam；普通`read_file`若被隐式解释会产生隐藏状态。
5. 没有删除/修改/invalid skill的完整append-only transition matrix。
6. 当前parser不接受标准`license`、`compatibility`与`metadata`，name规则也没有证明`name == parent directory basename`。
7. 当前unknown-frontmatter warning会把合法但Pulsara不解释的宿主字段误报成整个Skill无效。
8. 当前catalog没有把`scripts/references/assets`作为受containment约束的progressive resources表达。

---

## 4. 最小 closed contracts

本节是 normative DTO 规格。应复用现有类型，避免建立第二套近似skill model。

### 4.1 Agent Skills canonical manifest

`LocalSkillManifest`继续是唯一filesystem-backed leaf，但它必须直接表达Agent Skills标准，而不是保留一套Pulsara私有frontmatter schema。

~~~python
@dataclass(frozen=True, slots=True)
class LocalSkillManifest:
    name: str
    description: str
    license: str | None
    compatibility: str | None
    metadata: tuple[tuple[str, str], ...]  # key-sorted immutable standard mapping
    path: Path                 # process-local exact path
    base_dir: Path
    location: str             # model-visible stable display path
    body: str                 # exact Markdown body after frontmatter
    raw_document_digest: str  # digest of exact bounded UTF-8 SKILL.md bytes
    source: SkillSource
    authoring_diagnostic_codes: tuple[SkillAuthoringDiagnosticCode, ...]
~~~

标准validation必须exact执行：

1. `SKILL.md`以YAML frontmatter开头；frontmatter是mapping；
2. `name`与`description`存在且为string；
3. `name`满足`^[a-z0-9]+(?:-[a-z0-9]+)*$`、UTF-8长度1–64，并与parent directory basename byte-equal；
4. `description`trim后长度1–1024；它是唯一portable routing description，不再读取顶层`when_to_use`；
5. `license`若存在必须是non-empty string，canonical UTF-8最多1,024 bytes；它仍只是license name或bundled license file reference；
6. `compatibility`若存在必须为1–500字符；
7. `metadata`若存在必须为mapping，最多64项；所有key必须是1–128 UTF-8 bytes的string，value必须是0–1,024 UTF-8 bytes的string，key-sorted canonical aggregate最多16 KiB；不得把任意nested YAML object偷渡为metadata value；
8. 整个UTF-8 `SKILL.md`最多64 KiB，YAML frontmatter最多32 KiB；parse前的event/node scan最多512 nodes、最大depth 16，duplicate key、anchor、alias、custom tag与multi-document YAML全部拒绝；
9. body与标准resources继续受§5.4 physical bounds约束；supporting resources不在discovery时读取；
10. unsupported top-level extension只进入bounded internal diagnostic，不产生Pulsara语义；`license`、`compatibility`与`metadata`绝不能再被标为unknown。

旧顶层`provides_tools`、`suggested_tools`、`required_binaries`、`optional_binaries`、`external_services`、`network_required`、`auth_required`、`cli_usage_kind`、`when_to_use`、`disable_model_invocation`与`user_invocable`全部失去canonical语义。clean-v0不得悄悄恢复alias、双读或迁移逻辑，也不得把这些字段搬进`metadata.pulsara.*`继续解释。

`SkillAuthoringDiagnosticCode`至少包含`BODY_OVER_500_LINES`与`BODY_ESTIMATE_OVER_5000_TOKENS`。它们只表达Agent Skills authoring recommendation：前者由exact line count产生，后者可在存在target estimator时产生；二者都不能使manifest invalid、隐藏Skill或改变fingerprint。详细内容应由作者拆到`references/`等resources，但Runtime不得为了满足recommendation而截断body。

`local_skill_manifest_semantic_fingerprint()`必须使用domain-separated、length-prefixed canonical encoding，覆盖标准parsed字段与body；它不覆盖raw document digest、mtime、inode、scan duration、absolute home path的字符串展示、supporting resource当前内容、unsupported/ignored extension值、authoring diagnostic或free-text internal diagnostic。`raw_document_digest`独立证明exact file bytes，供read race/join使用，不能替代semantic fingerprint。Provider-visible catalog fingerprint只覆盖`name + description + location`；active/activation fingerprint只额外覆盖exact body。因而license、compatibility或opaque metadata变化可以更新local manifest identity，但在routing/body均未变化时不得追加冗余provider snapshot。Runtime activation的ToolResult只发送exact parsed Markdown body及closed provider wrapper，不把raw YAML frontmatter重复暴露给模型；ordinary `read_file`仍返回文件真实内容。

contract identity冻结为：

~~~text
pulsara.agent-skills-core.v1
~~~

这些是Pulsara parser/renderer的versioned implementation identities，不会写入provider正文。Agent Skills标准本身仍是外部canonical contract；不得把上述ID理解成另一种文件格式。

root discovery是Host policy，不属于Agent Skills文件格式。Pulsara deterministic precedence冻结为：

1. workspace `.pulsara/skills`；
2. workspace `.agents/skills`；
3. workspace `.claude/skills`；
4. user `${PULSARA_HOME}/skills` / `~/.pulsara/skills`；
5. user `~/.agents/skills`；
6. user `~/.claude/skills`。

同名时first valid winner生效，后续项只产生internal diagnostic。不得按最近mtime或目录遍历偶然顺序选winner。

六个条目不是scanner内部的hidden常量。Host composition必须先通过Round 9 central adapter为它们构造六个`SAFE_POINT_REFRESHABLE + LOCAL_SKILL_ROOT` source registrations，再分别exact join一个process-local physical root binding：

~~~python
@dataclass(frozen=True, slots=True)
class PreparedSkillRootSourceBinding:
    registration_fingerprint: str
    path: Path
    containment_root: Path
    location_prefix: str
    precedence_ordinal: int
    source_scope: SkillSource
    binding_fingerprint: str
~~~

约束如下：

- generic registration保存logical root identity、scope/precedence contract fingerprint与refresh mode，不保存absolute path；
- binding保存scan所需path/containment，但不进入provider、generic registry fingerprint或durable state；
- `LocalSkillProvider`必须消费ordered registrations + exact bindings，不得自行重新发明默认roots；
- Round 9.1的default Host composition精确产生上述六项；测试可显式传较小集合，但必须仍经过同一registration adapter；
- filesystem新增、修改、删除Skill只更新registered root的source snapshot，不重新注册root；
- 本轮不允许运行时新增第七root；future Plugin只能在Round 9.2通过同一个source-registration入口贡献root，不能向scanner注入private cache path。

Codex/Claude/plugin cache不是skill root。插件installer必须把skill物化到上述一个声明root；Runtime不得扫描`~/.codex/plugins/cache`、Claude plugin internals或任意包管理器cache来猜安装状态。

### 4.2 Standard resources 与 opaque metadata

Discovery只读取`SKILL.md`，**不得预枚举、预读或fingerprint supporting resources**。这是Agent Skills progressive disclosure的标准语义，也是避免资源目录变化制造无意义catalog successor的必要条件。Catalog/active wrapper告诉模型以skill root解释相对引用；模型将解析后的路径交给现有`read_file`/`search_files`，并继续服从这些工具自己的path/permission contract。本文不在file tool之外再建一套skill-resource sandbox。Script只有在模型显式调用普通`terminal`后才执行；skill discovery、activation或read绝不能自动执行`scripts/`。Supporting resource的真实current bytes由该次ordinary tool attempt拥有，不建立skill-private snapshot或generation。

标准`metadata`是可选、bounded、string-to-string的opaque author metadata。Pulsara可以在local inspector中原样显示其已解析值，但V1不得：

- 解释`pulsara.capabilities`或任何其他namespaced dependency语言；
- 从metadata生成Builtin/MCP/CLI引用、health check、route、permission或tool surface；
- 根据metadata改变model discovery、textual activation、configured activation或provider ordering；
- 要求installer、用户或第三方Skill作者为Pulsara补写metadata；
- 从description/body反向推断并回填metadata。

Skill若依赖CLI、MCP或其他tool，应在portable `description`或Markdown body中把使用方法说清楚。模型随后只能使用当前真实暴露的Builtin、MCP direct/meta与`terminal`能力；availability、authorization、effect、dirty fence和physical execution仍由这些既有owner在真实调用时判断。缺失能力产生ordinary typed unavailable/failure，而不是使Skill manifest无效。

### 4.3 明确不支持的宿主字段

`when_to_use`、`when-to-use`、`disable-model-invocation`、`user-invocable`、`argument-hint`、`arguments`、`allowed-tools`、`model`、`effort`、`context`、`agent`、`hooks`、`shell`、`paths`、动态`!command`及其他Claude/Kimi/Codex宿主字段，在Pulsara V1全部是**行为上inert**的unsupported extension。

Loader在完整YAML成功解析后可以记录一个bounded、closed diagnostic code，但不得解析这些字段的内部语法，也不得把它们保存进canonical manifest DTO。它们不参与manifest semantic fingerprint、catalog、activation、provider wire、tool surface、authorization、effect policy或execution binding；字段变化只会改变证明原始文件bytes的`raw_document_digest`。无论value是否为空、未知或使用其他宿主语法，都不能授予权限、隐藏Skill、禁止用户激活、触发subagent/hook/shell，或让其他部分有效的standard Skill失效。

用户在`$skill`后的文字继续作为ordinary user text保留；Pulsara不执行宿主参数模板展开。模型若通过`read_file(intent=ORDINARY)`读取原始文件，仍可能在opaque文件内容中看到这些frontmatter字段；这只是普通文件内容，不产生Runtime语义。

### 4.4 Skill 不拥有 dependency graph

Round 9.1不定义`SkillCapabilityDeclaration`、`FrozenResolvedSkillDependency`、CLI health fact或Skill-specific route enum。Skill body可以提到任何命令、工具或服务，但这些文字只是`UNTRUSTED_OBSERVATION`级指导，不是mechanical dependency proof。

若当前epoch已有对应能力，模型按普通路径使用：Builtin/direct MCP直接调用，late MCP经`inspect_new_mcp_tool`与`use_new_mcp_tool`，CLI经`terminal`。若能力不存在，既有owner返回typed unavailable/failure。Pulsara不得因Skill正文提到某个能力而安装MCP、扩大tool surface、改变permission、执行health probe或影响未来MCP promotion。

Future Plugin若需要保证“安装这个Skill时也安装某个MCP/CLI package”，该关系属于Plugin bundle manifest与installer，不属于`SKILL.md`，也不反向写入Skill manifest。

### 4.5 Catalog entry

`ResolvedSkillCatalogEntry`由一个central factory直接从manifest构造：

~~~python
@dataclass(frozen=True, slots=True)
class ResolvedSkillCatalogEntry:
    name: str
    description: str
    location: str
    source: SkillSource
~~~

Catalog严格遵守standard progressive disclosure：只展示任务选择所需的`name + description + location`；不展示license、compatibility、metadata、unsupported frontmatter、supporting resource清单或resource全文。Supporting resources由完整`SKILL.md`自身导航。任何宿主字段都不能过滤、扩写或重新排序catalog entry。

### 4.6 Frozen projection input

不新增 `SkillCatalogGenerationOwner`或第二个render/dependency input。直接复用Round 9的`FrozenSkillProjectionInput`作为filesystem/manifest的唯一process-local cut：

~~~python
@dataclass(frozen=True, slots=True)
class FrozenSkillProjectionInput:
    discovery: LocalSkillDiscovery
    source_snapshot_fingerprints: tuple[str, ...]
    snapshot_fingerprint: str
~~~

Skill facts只存在于Round 9 `FrozenCapabilityRegistrySnapshot`引用的Skill source snapshots；`FrozenSkillProjectionInput`不得保存第二份caller-supplied fact tuple。`source_snapshot_fingerprints`按registered-root precedence排列，并必须全部resolve到同一次planning cut的`LOCAL_SKILL_ROOT` snapshots。`LocalSkillDiscovery`是standard manifest renderer/activation需要的source-specific carrier；central factory必须证明winning manifests、source snapshot facts与registry flattened Skill view exact join。

`FrozenSkillProjectionInput.snapshot_fingerprint`覆盖ordered source snapshot refs、`(manifest semantic fingerprint, raw_document_digest, location)`与closed parse-result codes，但它**不得直接作为**`SKILL_CATALOG` source semantic fingerprint。后者由ordered `ResolvedSkillCatalogEntry`及exact rendered body独立计算，只覆盖provider-visible`name/description/location`。因而license、compatibility、opaque/ignored metadata或supporting-resource变化可更新local discovery/下一activation，却不会在provider-visible catalog与body完全相同时追加冗余snapshot。Internal diagnostic的path/free-text不进入provider semantic fingerprint；public closed status若实际进入catalog body才进入。

### 4.7 Active skill activation snapshot

`ActiveSkillInjection`继续是唯一active leaf；增加/明确：

- manifest semantic fingerprint；
- exact body digest与raw document digest；
- activation reason；
- source/location。

它不持有file handle、mtime或read capability。`ACTIVE_SKILL` body由这份frozen value一次渲染，后续不重新打开文件。

`manifest semantic fingerprint`只证明该activation来自哪份exact parsed manifest，不直接进入`ACTIVE_SKILL` provider semantic fingerprint。后者只覆盖provider-visible skill identity/location、activation reason与exact body；仅license、compatibility、opaque metadata或unsupported host字段变化时不得追加内容完全相同的active successor。

### 4.8 `read_file` activation intent

~~~python
class FileReadIntent(StrEnum):
    ORDINARY = "ORDINARY"
    ACTIVATE_SKILL = "ACTIVATE_SKILL"


@dataclass(frozen=True, slots=True)
class FrozenSkillActivationCatalogEntry:
    skill_name: str
    catalog_location: str
    source_registration_fingerprint: str
    source_snapshot_fingerprint: str
    expected_manifest_semantic_fingerprint: str
    expected_raw_document_digest: str
    root_binding_fingerprint: str
    activation_path: Path = field(repr=False, compare=False)
    entry_fingerprint: str


@dataclass(frozen=True, slots=True)
class FrozenSkillActivationCatalogLookup:
    exact_scope: ExactConversationScope
    effective_presence: SourceObservationPresence | None
    skill_catalog_source_fingerprint: str | None
    skill_catalog_observation_fingerprint: str | None
    entries: tuple[FrozenSkillActivationCatalogEntry, ...]
    lookup_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedSkillActivationRead:
    exact_scope: ExactConversationScope
    advertised_entry: FrozenSkillActivationCatalogEntry
    catalog_lookup_fingerprint: str
~~~

`FrozenSkillProjectionInput`只是**候选catalog事实**，不能直接签发`FrozenSkillActivationCatalogLookup`。Lookup必须在compiler返回最终`FrozenProviderInputAppendCompileResult`之后，由central post-compile factory基于以下四项一次构造：

1. 最终`append_result.source_heads`中的effective `SKILL_CATALOG` head；
2. 本次compiler实际选择的exact catalog representation及其provider-visible body；
3. 同一次planning的`FrozenSkillProjectionInput`与六个root bindings；
4. predecessor epoch随其安装的exact activation lookup（若有）。

`skill_catalog_source_fingerprint`必须exact equal effective `ProcessLocalSourceHead.semantic_fingerprint`；`skill_catalog_observation_fingerprint`必须exact equal其`installed_observation_fingerprint`。`effective_presence=None`只表达该epoch尚无installed catalog head，此时两个fingerprint都为`None`且`entries=()`。`VALUE`必须拥有两个fingerprint和至少一个entry；`CLEARED | UNAVAILABLE`必须拥有两个fingerprint且`entries=()`。`lookup_fingerprint`覆盖scope、effective presence、两个head fingerprint与ordered entry fingerprints；`activation_path`仍只作为已验证root binding下的physical leaf保存，不进入semantic/provider fingerprint。

Post-compile factory的closed矩阵是：

~~~text
compiler实际追加 VALUE_EXACT_FULL
    -> 从该exact VALUE body逐项反解/核验advertised entries
    -> exact join current projection与root bindings
    -> 构造非空lookup

resulting head仍为VALUE，但本次没有追加observation
    -> 若current projection渲染出的VALUE与effective head/body逐项相等，
       可以用current physical bindings重建同语义lookup
    -> 否则只可继承predecessor lookup，且其两个head fingerprint必须exact equal
    -> predecessor lookup缺失或不匹配则在candidate register前typed internal conflict

resulting head为CLEARED或UNAVAILABLE
    -> 构造绑定该effective head的空lookup

resulting head不存在
    -> 构造ABSENT空lookup
~~~

`NOT_APPLICABLE`与semantic no-op都不是新的effective presence。它们只表示“本次没有发新observation”，必须解析到`append_result`最终保留的installed head：仍有效的VALUE只能按上表重建或继承，CLEARED/UNAVAILABLE/ABSENT只能为空。尤其当raw projection是catalog B、但budget把本次provider-visible状态降为`UNAVAILABLE`时，lookup必须为空；旧VALUE虽仍存在于immutable prefix，已被最新observation失效，不能授权B或旧entry。反过来，same-turn `NOT_APPLICABLE`期间filesystem已经变成B，也不得用raw B替换仍生效的predecessor A lookup。

Lookup是model-call scoped、process-local dispatch attachment，不进入provider或canonical row。为避免generic continuity层反向import Skill DTO，`PreparedProviderInputAppendCandidate`只增加opaque、process-local `dispatch_attachment_fingerprint`，其值由central runner factory domain-separate覆盖该lookup fingerprint；candidate fingerprint、register/install CAS与install permit都exact join该字段，但它不进入provider semantic-prefix、wire fingerprint或epoch compatibility。

Fingerprint join不能替代physical object join，因为`activation_path`有意不进入semantic fingerprint。Continuity owner的既有PREPARED slot必须同时登记candidate与一个opaque attachment object；它只读取该对象的frozen fingerprint，不解释Skill字段。Install必须消费**登记的同一个lookup对象**，same-shape copy、`dataclasses.replace()`副本、foreign lookup或只提交相同fingerprint都在provider open前拒绝。成功install返回的既有opaque permit同时绑定candidate与attachment object identity；`_PreparedProviderDispatch`只有凭该permit才能把登记的exact lookup随assistant/tool batch贯穿到`KernelToolInvocationContext`。这只是给现有process-local PREPARED/INSTALLED CAS增加一个opaque attachment槽，不是新的continuity owner、lookup registry或durable generation。

CAS失败、preflight失败、cancel或discard必须同时丢弃candidate与lookup，并从新的predecessor重新compile/build；禁止把失败attempt的lookup复用到下一call，禁止只携带fingerprint后查询latest discovery，也禁止建立`fingerprint -> snapshot` mutable map。

`ACTIVATE_SKILL`只接受**产生该tool call的model call** frozen lookup中唯一entry的exact location；central factory从lookup选择entry并构造`PreparedSkillActivationRead`，调用方不能自由提交expected digest。不得把后续safe-point catalog、新winner或任意Markdown path冒充成已advertised Skill。Runtime一次读取完整bounded `SKILL.md`，重新执行standard parser，并冻结**实际返回bytes**对应的standard manifest与body：

- 与expected manifest一致：ToolResult body与activation validation使用同一frozen manifest；
- 文件发生变化但仍是同name/location的valid standard skill：ToolResult返回current exact parsed body，next safe point同时发布catalog successor；
- missing/invalid/name mismatch/over-bound：ordinary typed read/activation failure，不安装active head；
- pagination/partial read不允许使用`ACTIVATE_SKILL`；
- activation body必须先通过普通`PreparedToolOutputProjection`规则；只有ordinary display kind为`COMPLETE`时才可形成successful activation result。若普通ToolResult会变成`HEAD_TAIL`，则改为小型typed `SKILL_ACTIVATION_UNAVAILABLE/BODY_NOT_INLINEABLE`结果，提示作者将详细内容拆入resources；不得建立Skill专用大正文通道或把HEAD_TAIL称为完整activation；
- canonical successful ToolResult保存exact parsed Markdown body这个真实activation read outcome，并标记Round 7.1 `FULL_REQUIRED/SKILL_ACTIVATION`；其普通FULL provider projection是model-driven activation body的唯一副本，不再追加raw frontmatter或第二份`ACTIVE_SKILL`正文；
- 只有下一provider compile为该exact result选择FULL且continuity candidate成功安装时，才能声称模型完整激活了该skill。单条logical result没有FULL时必须在successful result形成前改为上述小型typed unavailable；successful COMPLETE已经canonical accepted、但aggregate provider input装不下时则由Round 7.1返回typed resource boundary、provider open=0并保持activation未生效，不能重写canonical result或静默以COMPACT/REF_ONLY正文代替完整body；
- activation settlement发生在完整tool batch结算之后、provider open之前，不能影响同一个parallel tool batch中的其他call。

Activation attachment与frozen catalog lookup同属既有dispatch attachment：FULL安装前为dormant，caller cancellation/CAS conflict不能使其生效或把requirement改成BEST_AVAILABLE；fresh attempt必须从exact canonical result、request intent与同一versioned classifier重建。Host/epoch close可释放未安装attachment，不新增durable loaded state或recovery owner。

`ORDINARY`保持现有raw-file semantics，不安装active head或loaded-skill marker。`ACTIVATE_SKILL`读取必须绕过ordinary read dedup并且不得读取、递增或写入ordinary dedup record；因此“先ORDINARY检查、后ACTIVATE采用”与重复显式activation都仍会重新取得完整current body。只有`ORDINARY`保留现有unchanged/third-read-blocked行为。这不是session loaded-state：每个activation仍是独立ordinary tool attempt/result。

`PreparedSkillActivationRead`是one-call process-local binding，不持有file descriptor、不持久化，也不创建skill receipt。其tool arguments/result仍按普通canonical attempt/result保存；Host重启后successful canonical ToolResult会在下一input cut重现exact parsed body。不得把“row已提交”误称为模型已经看到。

### 4.9 无 durable generation

本文中的“snapshot/generation”只表示process-local immutable value与semantic fingerprint。不得新增：

- skill_catalog_generations表；
- skill_activations表；
- SkillCatalogChanged event；
- announced-skill receipt；
- filesystem checkpoint；
- cross-Host generation restore；
- background reindex/reconcile job。

filesystem是当前skill内容来源；provider input continuity owner是当前模型前缀来源；canonical transcript保存模型真实进行过的`read_file`及其ToolResult。三者已经足够。

未来Round 5B若需要skill输入，只能组合这里已经定义的frozen projection与installed source head；本文不得为它提前创建`ActiveCompactionInstallationResources`或其他compaction owner。

---

## 5. Discovery、freeze 与 safe-point 顺序

### 5.1 每次 prospective provider dispatch

顺序固定为：

~~~text
freeze canonical provider-input cut
-> compose exact current Builtin/MCP/Skill source registrations
-> exact join six Skill root registrations to physical root bindings
-> freeze current Builtin/MCP source snapshots
-> bounded scan and parse registered Skill roots
-> apply cross-root precedence and split winners into complete per-root source snapshots
-> freeze one Round 9 capability registry snapshot
-> freeze current epoch native tool surface / MCP NEW-META exposure from that registry
-> freeze FrozenSkillProjectionInput referencing exact registry source snapshots
-> resolve trigger-specific ACTIVE_SKILL from the same frozen discovery
-> collect all other one-cut sources
-> compile
-> resolve final effective SKILL_CATALOG head from append result
-> build/rebuild/inherit an exact FrozenSkillActivationCatalogLookup for that head
-> construct and PREPARED-register continuity candidate + exact lookup attachment
-> provider preflight
-> continuity candidate + exact attachment install CAS
-> provider open with the same installed lookup
~~~

同一次planning中，registry Skill facts、catalog与active body必须来自同一`LocalSkillDiscovery`和同一组六个source snapshots。不得先注册facts或渲染catalog、随后重新读文件渲染active body。

“compile后构造lookup”不允许重新scan filesystem，也不允许改变compiler决策。它只把本次frozen projection、predecessor installed lookup与final append result做exact join。若这个join失败，整个dispatch attempt在provider open前失败并重建；不得用raw projection猜测模型看到了什么。

Round 3.1 dispatch-planning absolute deadline继续覆盖skill scan/parse/render；每个文件I/O仍使用相应physical bound。不得因每个skill重新签发完整planning deadline。

### 5.2 为什么保持safe-point rescan

当前实现每次provider planning都bounded scan skill roots。本轮把roots改成显式registration/binding输入，但继续保留每次planning对这些registered roots的complete scan。这一行为直接覆盖：

- Agent通过filesystem tool安装/修改skill；
- 用户或外部installer在进程外修改skill；
- bundled/plugin sync替换文件；
- skill删除或失效。

V1继续保留这一正确性路径。未来可以增加Codex式watcher/cache，但只能是性能优化：

- dirty signal可以清cache；
- cache hit必须返回与重新scan相同的immutable snapshot；
- missed watcher event不得成为skill永远不可见的原因；
- correctness tests必须能关闭cache并得到相同projection。

Watcher/cache即使存在，也只能帮助某个`SAFE_POINT_REFRESHABLE` registration更快产生相同source snapshot。它不得直接向generic registry调用`register()`、删除leaf或改变root precedence。

### 5.3 Tool batch边界

如果skill文件由某个tool batch创建或修改：

- 等整个batch的attempt/result/closure canonical settlement完成；
- 下一provider planning才scan；
- `SKILL_CATALOG` observation位于完整tool group之后；
- 绝不能插在某个tool call与其result之间；
- parallel batch只追加一个catalog successor，不按每个result重复扫描/通知。

### 5.4 扫描物理上界

沿用64 KiB单个`SKILL.md`上界，并补齐aggregate工作集：

~~~text
default registered skill roots                   = exactly 6
maximum registered skill roots in Round 9.1      = 6
maximum direct child directories inspected       = 1,024 total
maximum admitted valid manifests per cut         = 64
aggregate SKILL.md bytes read per discovery cut  = 16 MiB
maximum provider catalog location UTF-8 bytes     = 1,024 per entry
exact full catalog renderer UTF-8 bytes           = 384 KiB
maximum active skills per activation snapshot    = 16
aggregate active body UTF-8 bytes                 = 512 KiB
~~~

这些是异常/恶意filesystem边界，不是业务配额。超过64个valid winner或完整`name + description + location` catalog超过384 KiB时，整个catalog产生closed `UNAVAILABLE/CATALOG_OVERBOUND`；不得选择前64项、缩短description或发布partial Skill source facts。其他discovery aggregate超界同样不得发布partial truth。Active selection超界时不得部分激活，产生`ACTIVE_SKILL UNAVAILABLE`。普通conversation仍可继续；只有Round 3.1最小invalidation observation本身也无法容纳时才形成typed resource boundary。

Agent Skills关于`SKILL.md`少于500行、activation instructions少于约5,000 tokens是authoring recommendation，不是parser validity条件。Pulsara用closed authoring diagnostic提醒作者拆分resources，但不因此拒绝标准文件。Supporting resources不进入discovery aggregate；它们在ordinary `read_file`/`search_files`/`terminal`调用时分别受既有tool bound约束。

64 KiB只限制parser/filesystem working set，不承诺任意valid body都能在一次activation中进入provider。Model-driven activation完全服从Round 7.1 canonical COMPLETE、canonical preview hard bound与provider-neutral logical FULL quote；单条正文只能产生HEAD_TAIL或没有FULL时，在successful activation result形成前以小型typed unavailable投影结算并提示拆分resources。已经形成合法successful COMPLETE/FULL result但与siblings/history合计不fit时，`FULL_REQUIRED/SKILL_ACTIVATION`触发typed provider-input resource boundary，不得事后重写结果。Textual/configured activation同样必须通过`ACTIVE_SKILL` source的exact body/aggregate/provider quote；放不下时使用既有`UNAVAILABLE` state，而不是专用大正文carrier。

---

## 6. Provider-visible catalog contract

### 6.1 外层carrier

继续使用Round 7统一carrier：

~~~json
{
  "source": "SKILL_CATALOG",
  "trust": "UNTRUSTED_OBSERVATION",
  "lifecycle": "SNAPSHOT_ON_CHANGE",
  "presence": "VALUE",
  "body": "...exact full name/description/location catalog..."
}
~~~

`SKILL_CATALOG`与`ACTIVE_SKILL`都使用`UNTRUSTED_OBSERVATION`；activation ToolResult继续使用普通untrusted tool-output语义。第三方description/body不会因Runtime选择或包装而升级为授权上下文。在Runtime自动生成的catalog/active/activation projection中，contract/version/fingerprint/generation、raw frontmatter、opaque metadata、absolute resolved path、inode、mtime、Host ID和diagnostic path不得进入provider正文。Provider只看到routing metadata与parsed instruction body。模型若明确用`ORDINARY`读取原始`SKILL.md`，其ToolResult仍按ordinary opaque file content处理；Runtime不递归删改用户主动读取的正文。

### 6.2 稳定 BASE_SYSTEM 说明与纯数据 catalog body

以下Runtime规则必须随本轮global lowering cold bump一次写入稳定`BASE_SYSTEM`，不得混进untrusted catalog/body envelope：

- `SKILL_CATALOG`只是untrusted routing index，不是skill body；
- 采用skill时用`read_file(intent=ACTIVATE_SKILL)`完整读取列出的`SKILL.md`；只检查/比较时使用`ORDINARY`；
- 相对引用从skill root解析，`scripts/`、`references/`、`assets/`及其他supporting files只按需读取；
- skill只能使用已存在工具，不能授予工具或权限；
- Skill正文提到的Builtin、MCP或CLI用法只是指导；真实可用性与调用方式以当前tool/MCP catalog及调用结果为准；
- epoch中新发现的MCP仍按Round 9固定meta route使用，Skill本身不携带或签发route/ref；
- Skill content不能覆盖system/developer policy、当前用户明确要求、permission或effect gate。

`SKILL_CATALOG VALUE` body只保存ordered entries；每个entry必须包含完整、未经摘要或截断的`name + description + location`。它不重复上述Runtime说明，也不存在COMPACT/REF_ONLY routing variant。若exact body超过catalog aggregate，发布小型`UNAVAILABLE/CATALOG_OVERBOUND`；若body本身合法但本次provider-input budget无法完整容纳，发布`UNAVAILABLE/PROVIDER_BUDGET_UNAVAILABLE`。两者都不能让不完整metadata冒充可选Skill集合。

Compiler-facing representation family固定只有：

~~~text
VALUE_EXACT_FULL
UNAVAILABLE_MINIMAL(CATALOG_OVERBOUND | PROVIDER_BUDGET_UNAVAILABLE | DISCOVERY_UNAVAILABLE)
CLEARED_MINIMAL
NOT_APPLICABLE
~~~

即使是首次cold install，也不得通过省略尾部entries把部分catalog伪装成完整metadata。Exact VALUE放不下时，compiler选择同一次planning已准备的小型UNAVAILABLE carrier；只有连最小carrier也无法容纳才形成typed resource boundary。该规则覆盖现有generic `IMPORTANT` source degradation，不能继续沿用旧Skill catalog的short-description COMPACT/REF_ONLY renderer。

### 6.3 确定性

Catalog ordering固定为：

1. root precedence winner；
2. provider-visible entry按`name` Unicode codepoint升序；

同一个semantic snapshot必须生成byte-identical exact catalog body。不得把“newly discovered”“seen before”或当前日期写入正文；这些history-dependent字段会让相同catalog产生不同prefix。

### 6.4 Change / clear / unavailable

~~~text
current semantic catalog == installed VALUE
    -> no-op

catalog changed and at least one valid skill exists
    -> append new VALUE snapshot

current exact catalog is empty
    -> append CLEARED when prior head was VALUE/UNAVAILABLE
    -> otherwise no-op

complete discovery cannot be proven or exact full metadata catalog is overbound
    -> append UNAVAILABLE when prior head was VALUE/CLEARED or differs
    -> otherwise no-op
~~~

完整replacement放不下时按Round 3.1 stateful-source policy尝试最小`UNAVAILABLE` invalidation；不得继续让旧catalog冒充current。Catalog是advisory capability context，不能因为完整新catalog太大而永久阻断后续所有对话。

该transition matrix同时决定model-call activation eligibility，而不是只决定provider文字。最终head为`VALUE`时，lookup必须与该VALUE exact join；最终head为`CLEARED | UNAVAILABLE`或尚无head时，lookup必须为空。Compiler选择最小UNAVAILABLE后，即使旧VALUE bytes仍位于append-only prefix中，Runtime也不得接受针对旧entry的`ACTIVATE_SKILL` attempt。

---

## 7. Skill body 的两条路径

### 7.1 Explicit/configured activation

以下情形由Runtime直接注入完整body：

- ROOT真实human input显式包含`$name`或`skill:name`；
- Host配置的active skill name；
- ordered accepted steer batch中的activation anchor显式点名skill；
- child scope的configured skill。

BODY作为`ACTIVE_SKILL VALUE`进入provider runtime observation。它是本次activation的exact snapshot；同一tool loop不重新读文件。

### 7.2 Model-driven progressive disclosure

如果用户没有显式点名、模型仅根据catalog判断task匹配：

~~~text
model -> read_file({path: catalog.location, intent: "ACTIVATE_SKILL"})
Runtime -> exact catalog join + full bounded read + ordinary authorization/attempt/result
Runtime -> FULL result supplies exact body once
model -> read referenced files as needed
~~~

这条路径不产生第二份`ACTIVE_SKILL` successor，也不建立session-wide `loaded_skill_names`。明确区分：

- 模型只是比较skill时必须用`ORDINARY`，不代表采用；
- 只有provider显式给出`ACTIVATE_SKILL` intent且下一compile选择FULL，才能声称该skill已被完整激活；body由该ToolResult自身承载；
- canonical ToolResult已经记录模型实际看到了什么；
- 后续history reset如何保留ongoing workflow属于Round 5B，而不是本轮建立loaded-state的理由。

本文不维护session内“读过的skill”清单。未来Round 5B也不得仅因为一次普通read就自动枚举或重新注入skill；这是一条下游设计约束，不是本轮实现项。

### 7.3 Read race 的语义

Catalog描述的是其freeze时刻的bounded manifest；`read_file`读取的是tool attempt时文件的current bytes。二者之间文件变化时：

- `ORDINARY`成功：canonical ToolResult中的bytes就是模型实际检查的内容，不activation；
- `ACTIVATE_SKILL`成功且current file仍是valid same-name standard skill：ToolResult FULL projection使用current exact parsed Markdown body；
- file不存在/不可读：返回普通typed read failure；
- 下一safe point重新scan并追加catalog successor；
- 不因为skill是advisory guidance而建立catalog-generation read permit。

这不是TOCTOU authority漏洞：skill始终只是advisory instruction，不能授予tool schema、permission或physical capability。Textual/configured `ACTIVE_SKILL`使用planning时已经读取的body；model-driven activation使用exact ToolResult bytes；两条路径都不做第三次file read，也不重复正文。

---

## 8. Activation lifecycle matrix

### 8.1 Closed activation subject

| scope/admission | textual activation | configured activation | 结果 |
|---|---:|---:|---|
| ROOT initial human message | yes | yes | aggregate exact snapshot |
| ROOT accepted USER_STEER anchor | yes | yes | replace/clear active head |
| model `ACTIVATE_SKILL` ToolResult | explicit tool intent | inherit configured | FULL result carries body once |
| same-turn ordinary tool/result loop | no | inherit | `NOT_APPLICABLE`, head不变 |
| Plan automatic continuation | no | yes | configured-only replacement或clear |
| ROOT next real human message | yes | yes | new activation epoch |
| SUBAGENT_TASK objective | no | yes | configured-only；objective不能textual activate |
| child tool loop | no | inherit | head不变 |

Ordered steer batch继续使用Round 3.1 exact anchor：最后一个accepted ROOT human steer决定textual activation，前序steer仍是canonical delta，但不得各自产生一个active snapshot。

Skill activation不创建任何derived authorization state。旧`ACTIVE_SKILL`或activation ToolResult正文可以留在immutable prefix，但它们只表达模型曾经收到的advisory instruction，不能成为后续tool authorization的输入。

### 8.2 Active file发生变化

同一activation期间，即便active skill文件被修改、删除或被同名winner替换：

- 当前`ACTIVE_SKILL` exact body不变；
- model-driven activation的canonical ToolResult body同样不变；
- catalog可追加新snapshot反映current filesystem；
- same-turn ordinary tool loop不重新activate；
- 下一真实activation boundary才读取current winner并replace/clear/unavailable。

这一点吸收Codex turn snapshot与旧Pulsara same-run exact reuse语义，避免长任务在文件保存瞬间静默更换工作规程。

### 8.3 Active presence transitions

~~~text
no prior / CLEARED / UNAVAILABLE -> selected valid set
    append VALUE

VALUE(A) -> same semantic A
    no-op

VALUE(A) -> different selected set/body B at true activation boundary
    append VALUE(B)

VALUE(A) -> no selected skill at true activation boundary
    append CLEARED

VALUE(A) -> selected name missing/invalid/over-bound
    append UNAVAILABLE

same-turn non-activation call
    NOT_APPLICABLE; never clear or refresh
~~~

连续CLEARED按presence去重，不把新turn ID写进clear semantic fingerprint。

---

## 9. Skill 与可执行能力的边界

### 9.1 Body guidance 不是 dependency declaration

Skill作者可以在`description`或body中写明“使用`gh`”“调用Firecrawl MCP”“先运行某个脚本”。Pulsara把这些文字当作advisory instruction，不解析成identity、route、health或permission fact，也不从当前registry反向验证整份Skill是否“依赖满足”。

这不是能力退化。模型采用Skill以后仍然可以：

- 调用当前native `tools[]`中的Builtin/direct MCP；
- 根据Round 9的`MCP_CATALOG`与runtime observation，用`inspect_new_mcp_tool`/`use_new_mcp_tool`访问late MCP；
- 通过普通`terminal`执行CLI；
- 在typed unavailable/failure后向用户解释缺失前提或选择其他合法路径。

真实Tool/MCP/Terminal owner始终拥有schema、availability、authorization、effect与physical execution truth。Skill body不能让不存在的能力变为callable，也不能让同名或相似工具自动替代。

### 9.2 Dynamic MCP 与 Skill catalog彼此独立

MCP READY、DIRTY、removed、same-schema reconnect或direct/meta route变化，只更新MCP source/catalog；如果Skill文件自身没有变化，`SKILL_CATALOG`不得因为这些变化追加successor。反方向上，安装或修改Skill也不能改变MCP cohort、promotion、tool ordering或provider `tools[]`。

模型若在Skill正文中看到一个late MCP名称，应以当前MCP runtime observation为准获取exact schema/ref；Skill不预先持有`NewMcpToolRef`、server generation或slot lease。

### 9.3 CLI 与普通 terminal

Pulsara不为Skill扫描PATH、运行`--version`、检查登录、网络或credential，也不在catalog标记`TERMINAL_CLI | UNAVAILABLE`。CLI是否存在只在模型真实调用`terminal`时由操作系统与既有ToolResult契约回答。GitHub `gh`、Hugging Face `hf`、Firecrawl CLI及任何第三方命令都使用同一路径，不各建Skill-private capability owner。

### 9.4 Future Plugin bundle

如果发行者需要保证Skill与MCP server/CLI package共同安装，应由future Round 9.2 Plugin manifest分别贡献Skill root与MCP/install配置。Loose Agent Skill始终可以原样安装；缺少Plugin或host sidecar不能让它失去发现、激活或正文指导能力。Pulsara不要求用户补metadata，也不使用规则从正文生成bundle manifest。

---

## 10. Round 9 与 Round 5B 边界（非重复实施、非compaction验收）

本节只是防止本文实现把未来Round 5B堵死，不是本轮production change、test gate或activation evidence。本文不得导入compaction package，也不得新增summary/adoption/installation DTO。

### 10.1 本轮必须留下的普通接口

Round 5B未来只能消费本文普通provider planning已经拥有的两类值：

1. `FrozenSkillProjectionInput`：完整current catalog的immutable semantic input；
2. continuity owner中已经安装的`SKILL_CATALOG` / `ACTIVE_SKILL` source heads。

这些值首先服务正常dispatch。不得为未来compaction复制成`CompactionSkillSnapshot`、`ActiveCompactionInstallationResources`或另一套fingerprint。

### 10.2 下游必须遵守但由Round 5B实现的规则

以下全部留在Round 5B：

- summary call是否以及如何看到旧catalog/active body；
- successor catalog取哪个safe point；
- same-run active body是否继承；
- active/idle adoption分支；
- successor dry compile与continuity install；
- freeze后filesystem变化如何settle；
- repeated compaction如何去重；
- compaction-specific tests、failure matrix与activation evidence。

本文只提出兼容性约束：Round 5B不得要求修改BASE_SYSTEM、不得把skill变成tool、不得依赖durable loaded-skill history，也不得重新解释raw filesystem时绕开本文的central manifest/projection builders。

active compaction adoption后的第一次普通compile仍必须执行本文已经冻结的**post-compile effective-head lookup**顺序：先由compiler决定successor `SKILL_CATALOG`的VALUE/CLEARED/UNAVAILABLE/no-op继承结果，再从该次真正provider-visible的effective head构造`FrozenSkillActivationCatalogLookup`，最后把lookup作为opaque exact-object dispatch attachment与successor continuity candidate一起CAS安装。Round 5B不得从compaction前raw filesystem projection、summary正文或pre-compile successor candidate预先构造lookup；idle adoption不构造lookup，下一条真实turn cold compile时再走同一普通路径。

这只是Round 5B复用本文普通接口的下游义务，不扩大本文production修改面，也不建立`CompactionSkillLookup`、loaded-skill ledger或durable activation history。

### 10.3 实施顺序

整体顺序冻结为：

~~~text
Round 7.1 global provider-visible ToolResult projection
-> Round 9 unified capability semantics + MCP direct/meta
-> 本文 Agent Skills standard + append-only skill capability
-> Round 5B compaction implementation
~~~

因此本文只复用Round 9提供的`CapabilityIdentity`、`FrozenToolCapabilityFact`、`FrozenSkillCapabilityFact`、`NewMcpToolRef`、fixed meta tools与纯exposure planner，不重新定义任何这些类型，也不调用任何compaction service。若Round 9尚未激活，不得以临时string ref、第二套meta tool或skill-private capability基类绕过。

---

## 11. Prefix continuity证明

### 11.1 新增skill

~~~text
wire[n]
  SYSTEM=S
  tools=T
  messages=M

install skill X on filesystem

wire[n+1]
  SYSTEM=S
  tools=T
  messages=M || SKILL_CATALOG(VALUE, catalog+X)
~~~

### 11.2 修改/删除skill

~~~text
modify X name/description/body
  -> append successor only when provider-visible catalog or active body changes

modify only license/compatibility/opaque metadata/unsupported host fields
  -> local manifest/raw digest may change
  -> provider catalog/active no-op

remove X
  -> append full successor without X

catalog unreadable/over-bound
  -> append UNAVAILABLE invalidation
~~~

没有任何路径修改旧catalog bytes。

### 11.3 Explicit activation

~~~text
USER_MESSAGE("use $X")
  -> append user message
  -> append ACTIVE_SKILL(VALUE, exact X body) at defined placement

tool loop
  -> active source NOT_APPLICABLE
  -> old active head remains part of immutable prefix
~~~

### 11.4 Tools array

Skill安装、修改、删除、activation与read都不改变tools。本文任何路径都不能重建native tools；skill reference本身永远不能触发tool-surface mutation。未来Round 5B的new-epoch MCP cohort selection属于下游独立契约。

---

## 12. Failure matrix

| failure | provider open | canonical mutation | skill/source处理 |
|---|---:|---:|---|
| root缺失 | allowed | none | absent root正常，不诊断 |
| root symlink escape | allowed | none | catalog UNAVAILABLE only if complete scan不可证明 |
| one skill invalid YAML/frontmatter | allowed | none | omit exact invalid skill + bounded public diagnostic |
| unsupported host extension | allowed | none | ignore behavior + internal diagnostic；standard skill remains |
| opaque metadata changes | allowed | none | local manifest可更新；name/description/body不变则provider source no-op |
| one document >64 KiB | allowed | none | omit invalid/over-bound skill + diagnostic |
| valid body exceeds ordinary COMPLETE ToolResult or provider FULL quote | allowed | ordinary typed result | catalog仍完整列出；activation明确UNAVAILABLE并提示拆分resources |
| successful activation FULL单条合法但aggregate input不fit | no | successful canonical ToolResult保持 | `FULL_REQUIRED/SKILL_ACTIVATION` resource boundary；activation保持dormant，不降级/重写/重读文件 |
| more than 64 valid winners or exact catalog >384 KiB | allowed if invalidation fits | none | no partial/shortened metadata；catalog UNAVAILABLE/CATALOG_OVERBOUND |
| aggregate discovery bound exceeded | allowed if invalidation fits | none | no partial snapshot；UNAVAILABLE |
| duplicate name | allowed | none | deterministic first winner；later duplicate omitted |
| body mentions missing CLI/tool/MCP | allowed | only on real call | Skill remains valid；existing execution owner returns typed unavailable/failure |
| unrelated MCP route changes | allowed | none | MCP catalog may change；Skill catalog no-op |
| direct MCP disconnect | allowed | attempt only on call | native schema不变；call typed unavailable |
| read_file target changed | allowed | ordinary tool result | model使用真实returned bytes；next scan updates catalog |
| read_file target removed | allowed | ordinary failed tool result | next scan removes catalog entry |
| active skill modified mid-run | allowed | none | active head保持；catalog可变 |
| catalog replacement over input budget | allowed if invalidation fits | none | append UNAVAILABLE；本次activation lookup为空，旧prefix entry不可调用 |
| post-compile effective-head / lookup join conflict | no | none | candidate注册前丢弃整次dispatch并从fresh predecessor重建 |
| continuity CAS conflict after lookup freeze | no for stale attempt | none | candidate与lookup一起discard；不得向下一attempt复用 |
| caller cancel before activation FULL install | no for cancelled waiter | successful canonical ToolResult保持 | waiter detach；activation不生效，Host-owned exact settlement/next attempt仍重建同一requirement |
| active exact body cannot fit but UNAVAILABLE fits | allowed | none | append/安装UNAVAILABLE；不回退旧body |
| even minimum active invalidation cannot fit | no | none | typed capability resource boundary |

“one skill invalid”与“complete scan不可证明”必须区分。确定性地跳过一个invalid manifest仍能构造完整valid catalog；目录枚举/aggregate reserve失败则不能声称剩余项就是完整catalog。

---

## 13. 实施修改面

### 13.1 `capability/local_skills.py`

- 增加六个default logical-root registration factory与`PreparedSkillRootSourceBinding`；
- `LocalSkillProvider`只消费ordered registrations/bindings，不在scanner内部自行选择roots；
- root registration exact join path/containment/scope/precedence binding，generic registration不保存absolute path；
- 以Agent Skills core parser替换Pulsara私有顶层schema；
- exact验证name、directory、description、license、compatibility与string metadata，包括license/metadata/aggregate closed bounds；
- 在YAML对象materialize前执行frontmatter bytes、node、depth、duplicate key、anchor/alias、custom tag与multi-document scan；
- 500 lines/约5,000 tokens只生成closed authoring diagnostic，不改变validity、semantic fingerprint或provider admission；
- 所有host extension与`metadata.pulsara.*`均不产生Runtime语义；
- 删除`available_tool_names`参数及旧Pulsara tool/binary/service/auth字段解析；不以新alias或namespace恢复；
- 任意valid standard skill无需Pulsara补丁即可正常发现；
- discovery不枚举或预读supporting resources；
- 增加aggregate scan reservation与complete/incomplete result；
- 增加`.claude/skills`两root并保持deterministic precedence、symlink containment、UTF-8与64 KiB bound；
- complete scan按winning logical root发布Round 9 `FrozenCapabilitySourceSnapshot`，然后只通过pure registry factory注册Skill facts；
- central manifest semantic fingerprint与raw document digest；authoring diagnostics不进入semantic fingerprint。

### 13.2 `capability/types.py`

- 删除旧Skill tool/binary/service/auth字段及相关enum/DTO；
- 新增最小standard manifest DTO；不增加Anthropic host profile、Pulsara extension或dependency route DTO；
- `ResolvedSkillCatalogEntry`只保存name/description/location/source；
- `ActiveSkillInjection`绑定manifest/body fingerprint、reason与source/location；
- 所有 provider DTO 保持frozen、无mutable dict。

### 13.3 `capability/resolver.py`

- catalog与active projection使用同一frozen discovery；
- renderer只投影portable name/description/location及exact active body；
- 不读取tool/MCP registry做Skill dependency resolution，也不执行CLI health check；
- 不实现implicit matcher或loaded state。

`capability/skill_health.py`若只服务旧`required_binaries`语义则整体删除；不得保留一个无authoritative input的Skill health subsystem。

### 13.4 `conversation_kernel/capability.py`

- 删除Host-startup `_available_tool_names`作为动态semantic交集；
- 保留static product allowlist只用于Host未配置某builtin的硬排除；
- Host composition构造default Skill root registrations与physical bindings，并与Round 9 Builtin/MCP registrations共同进入planning；
- `freeze_projection_input()`只消费exact Skill discovery与registry source refs，不需要native/MCP exposure参数；
- 复用Round 9的`FrozenSkillProjectionInput`，只保存registry-owned Skill source snapshot refs，并把skill-specific fingerprint domain升级为`pulsara:frozen-skill-projection-input:v2`；
- freeze后不再读取filesystem/MCP supervisor。

### 13.5 `conversation_kernel/context_sources.py`

- `SKILL_CATALOG` source contract升级为`pulsara.skill-catalog.v2`；
- collector contract升级为`pulsara.skill-catalog-collector.v2`；
- `ACTIVE_SKILL` source contract升级为`pulsara.active-skill.v2`，因为canonical manifest与parsed-body contract已经改变；
- `read_file` descriptor/schema contract升级，新增closed intent；只能在Host cold tool-surface construction安装，禁止同epoch热改；
- complete scan failure映射UNAVAILABLE；empty映射CLEARED；
- `SKILL_CATALOG`与`ACTIVE_SKILL`使用`UNTRUSTED_OBSERVATION`；stable usage/authority规则只进入BASE_SYSTEM；
- catalog renderer只有exact full `name + description + location`表示，不再生成缩短description的COMPACT/REF_ONLY variant；
- same tool loop继续NOT_APPLICABLE；
- 保持placement：catalog在active之前，不能用degradation priority替代placement ordinal。

### 13.6 `model_input/continuity.py`、`input_continuity.py` 与 Runner safe-point wiring

- provider planning在同一safe-point freeze complete Skill source snapshots；
- raw projection只产生catalog candidate，不能直接产生activation lookup；
- compiler完成后，从final append result的effective `SKILL_CATALOG` head、同一projection/root bindings与predecessor installed lookup构造`FrozenSkillActivationCatalogLookup`；
- `VALUE_EXACT_FULL`新head逐项核验rendered catalog后构造lookup；no-op/`NOT_APPLICABLE`按effective head重建或继承；`CLEARED | UNAVAILABLE | ABSENT`构造空lookup；
- `PreparedProviderInputAppendCandidate`增加`dispatch_attachment_fingerprint`，其process-local candidate fingerprint domain从`pulsara:prepared-provider-input-append:v2-wire-proof`升级为`pulsara:prepared-provider-input-append:v3-dispatch-attachment`；该字段不进入prefix/wire/compatibility fingerprint；
- `PreparedProviderInputAppendCandidate.dispatch_attachment_fingerprint` exact join lookup fingerprint；continuity PREPARED slot opaque登记同一lookup对象，register/install CAS与permit同时校验fingerprint和object identity，provider open后由`_PreparedProviderDispatch`把这个exact对象随assistant batch贯穿到每个`KernelToolInvocationContext`；
- CAS/preflight/cancel/discard失败同时释放lookup，下一attempt必须重新compile与构造；
- 完整tool batch之后再允许catalog update；
- ordinary initial、steer与tool-loop dispatch都使用同一个frozen projection factory；
- 只把普通frozen projection/source head接口暴露给未来consumer；
- 不导入或创建任何compaction resource；
- no new provider tool name；仅对既有`read_file`做cold schema revision，同epoch不得热改。

### 13.7 `read_file` activation wiring

- 扩展既有`read_file` schema加入optional closed `intent`，默认`ORDINARY`；不新增provider tool name；
- `ACTIVATE_SKILL`禁止partial pagination并exact join当前model-call `FrozenSkillActivationCatalogLookup`；
- empty lookup必须在local authorize/attempt之前返回typed `SKILL_NOT_ADVERTISED_FOR_MODEL_CALL`；不能因旧VALUE仍在prefix、调用参数猜中路径或latest projection存在entry而放行；
- full read、standard parse与ToolResult共享一份immutable returned-byte carrier；
- activation body必须先通过ordinary ToolResult `COMPLETE`与logical FULL-eligibility边界；HEAD_TAIL/无FULL候选在successful result形成前改为小型typed unavailable；successful result附加Round 7.1 `FULL_REQUIRED/SKILL_ACTIVATION`，aggregate compiler不得降级；
- activation settlement只在完整tool batch与exact FULL continuity install之后生效；aggregate不fit或cancel保持dormant并走typed resource boundary，不改写canonical result；
- `_PreparedProviderDispatch`、assistant/tool batch与`KernelToolInvocationContext`贯穿同一个frozen lookup对象；invoke前从该lookup构造selected read，不能用latest scan或单独fingerprint替代；
- `ACTIVATE_SKILL`绕过且不更新ordinary read dedup；`ORDINARY`继续维持原行为；
- failure/cancel/non-FULL projection不安装active state，也不把requirement降为BEST_AVAILABLE；
- provider tool schema在Host cold construction时冻结，同一continuity epoch内仍byte-equal。

### 13.8 Tool authorization保持不变

- Skill activation、instruction body与frontmatter都不进入permission resolution；
- 所有真实tool call继续执行ordinary authorize/effect/attempt/invoke契约；
- Skill不得修改provider tool surface、ToolSpec或execution binding。

### 13.9 Inspector / diagnostics

只读inspect可显示：

- winning root/source/location；
- manifest semantic digest（local only）；
- Agent Skills core conformance与unsupported host-extension codes；
- duplicate/invalid/over-bound原因；
- active reason与body digest；
- exact full catalog byte cost、admitted count与closed unavailable reason。

公开provider diagnostic只使用closed code，不暴露absolute path或free-text exception。

---

## 14. 测试规格

### 14.1 Parser/discovery golden

- default Host composition精确产生六个SAFE_POINT_REFRESHABLE root registrations；scanner不含hidden default-root lookup；
- registration与physical root binding fingerprint exact join，foreign/path-swapped binding在scan前拒绝；
- 同registration重复composition idempotent，同logical root不同registration冲突；
- 六root precedence exact，并覆盖`.claude/skills`；
-同root按child name排序；
- symlink escape、invalid UTF-8、missing frontmatter、whole-file/frontmatter oversize；
- YAML 512-node、depth-16、duplicate-key、anchor/alias、custom-tag与multi-document边界；
- `name`首尾/连续连字符、uppercase、directory mismatch全部拒绝；
- standard `license`、`compatibility`与string metadata不产生unknown warning；license 1,024-byte边界通过；
- metadata 64-entry、key/value/16-KiB aggregate边界与non-string value拒绝；
- legacy Pulsara顶层字段不再获得旧语义；
- `metadata.pulsara.capabilities`与其他namespaced值保持opaque，不产生declaration、route或health fact；
- supporting resource修改不改变manifest/catalog fingerprint，读取时返回当前exact bytes；
- mtime变化但bytes/metadata相同，semantic fingerprint相等；
- name/description/body变化时相应manifest/catalog/activation fingerprint按字段覆盖规则变化；
- license/compatibility/opaque metadata单独变化不会追加provider catalog或active successor；
- duplicate name first winner稳定；
- winning manifests按root拆成complete source snapshots并与Round 9 registry Skill view exact join；
- empty root发布COMPLETE空snapshot；global scan无法证明完整时不发布任何partial Skill facts；
- 1,025 dirs、65 valid winners、16 MiB+1 aggregate或384 KiB+1 exact catalog产生complete-scan/catalog unavailable，不发布partial catalog或缩短description；
- 500 lines/5,000-token recommendation只产生closed authoring diagnostic，manifest仍valid且fingerprint不变。

### 14.2 Official contract fixtures

- 使用`skills-ref validate`可接受的minimal与full Agent Skills fixtures；
- fixture的`name == parent directory`、description 1/1024边界与compatibility 1/500边界；
- 带实验性host字段的official fixture仍可发现；改变该字段不改变manifest semantic fingerprint、catalog、activation或authorization；
- `when_to_use`、`argument-hint`、`disable-model-invocation`、`user-invocable`、`allowed-tools`、`model/context/hooks/!command`均不执行且不改变Pulsara行为；
- `~/.codex/plugins/cache`与其他private plugin cache不被scan。

### 14.3 Capability independence

- body提到available builtin/direct MCP/CLI时，真实调用仍走既有owner；
- body提到late MCP时，模型按Round 9 observation/meta tool使用，不由Skill签发ref；
- missing/ambiguous tool或CLI不让Skill失效，真实调用返回typed unavailable/failure；
- MCP READY/disconnect/same-schema reconnect/schema replacement不改变Skill semantic fingerprint；
- permission mode切换不改变Skill catalog或active body；
- Skill add/change/remove不改变MCP route或provider tools。

### 14.4 Read semantics

- workspace `.pulsara/skills/X/SKILL.md`可由catalog location通过`read_file(intent=ACTIVATE_SKILL)`完整读取，FULL ToolResult只展示正文一次；
- workspace `.agents`/`.claude`、user `.pulsara`/`.agents`/`.claude`同样覆盖；
- `search_files`可查reference，`terminal rg`不是必需路径；
- supporting resources不在discovery时读取；relative reference按skill root展示并由ordinary file-tool path policy执行；script不因load自动执行；
- `ORDINARY` read不创建activation；`ACTIVATE_SKILL`只在FULL result、batch与continuity settlement后算作完整激活，不另建body head或permission state；
- read之后文件变化，ToolResult保留old exact output、next catalog显示new state；
- repeated unchanged `ORDINARY` read继续遵守现有dedup contract；`ORDINARY -> ACTIVATE_SKILL`与repeated `ACTIVATE_SKILL`均绕过且不污染ordinary dedup，并在ordinary COMPLETE边界内返回完整current body；
- 超出ordinary COMPLETE边界的valid Skill返回typed activation unavailable并提示拆分resources，不返回HEAD_TAIL成功或建立Skill专用大正文通道；
- successful COMPLETE activation result带`FULL_REQUIRED/SKILL_ACTIVATION`；与一个或多个parallel sibling/长history合计超budget时provider open=0、siblings不丢失、canonical result不重写且activation保持dormant；
- FULL安装前cancel只detach waiter，不安装active state；fresh attempt从exact request/result重建requirement，绝不重新读取文件或重跑tool；
- model call看到catalog A后filesystem刷新为B，其tool call仍只可用随该call贯穿的lookup A解析；不能查询latest B补齐未advertised entry，A的exact path已删除时返回typed failure；
- raw catalog B因provider budget被编译为UNAVAILABLE时，本call lookup为空；即使模型从旧prefix猜出A/B location，`ACTIVATE_SKILL`也在attempt前typed拒绝；
- catalog CLEARED/UNAVAILABLE后lookup为空；same semantic no-op可从current bindings重建同一VALUE lookup，`NOT_APPLICABLE`且current projection不同则必须继承predecessor VALUE lookup；
- post-compile lookup与effective source head fingerprint不一致时provider open为0；continuity CAS失败后旧lookup不可被下一attempt复用；
- `_PreparedProviderDispatch -> assistant/tool batch -> KernelToolInvocationContext`传递同一个lookup object；只传fingerprint、foreign-scope lookup或mutable fingerprint map均由architecture test拒绝；
- repository/tool result没有skill-private receipt。

### 14.5 Activation

- ROOT `$skill`注入full body；
- unsupported宿主invocation字段不改变catalog或textual/configured activation；
- configured skill在ROOT/child生效；
- child objective中的`$skill`不textual activate；
- ordered multi-steer只以last accepted anchor计算；
- tool loop保持active head；
- Plan nonhuman successor投影configured-only或clear；
- active文件mid-run修改不改变active head；
-下一真实activation boundary读取new body；
- missing/over-bound active产生UNAVAILABLE，不沿用旧body；
- failure -> clear -> clear只追加一次clear。

### 14.6 Dynamic catalog

- model call N后安装skill，N+1只追加catalog snapshot；
- add/change/remove先形成registered-root source snapshot successor与registry successor，旧registry仍immutable；
- watcher/cache不能旁路source snapshot直接注册leaf；
- parallel tool batch安装多个skill，只在完整batch后追加一个snapshot；
- modify/remove/disable分别产生确定性successor；
-无semantic变化不追加；
- complete replacement over budget使用UNAVAILABLE invalidation，不永久阻断对话；
- 每个VALUE entry保留完整description；64个admitted skills的exact catalog deterministic，任何description缩短、partial prefix admission或第65项静默丢弃均拒绝；
-外部filesystem修改无需watcher也能在next planning被发现。

### 14.7 Prefix tests

Chat Completions与Responses都必须对以下路径逐call断言：

~~~text
SYSTEM exactly equal
tools exactly equal
old messages byte-for-byte prefix of new messages
~~~

路径至少包括：add、modify、remove、explicit activation、tool loop、steer activation、unrelated MCP route change、catalog unavailable。

另对model-driven activation断言：旧messages保持prefix；新增完整tool group中`SKILL.md`正文只出现一次；不得同时在ToolResult与`ACTIVE_SKILL`复制正文；不得创建或注入skill-derived permission state。

Provider-wire hygiene还必须断言：catalog/active carrier的trust为`UNTRUSTED_OBSERVATION`，正文不含BASE_SYSTEM usage rules、raw frontmatter、license、metadata、Pulsara contract/fingerprint或absolute path；模型明确发起的`ORDINARY` raw file read不做递归清洗。

### 14.8 Round 5B readiness（非compaction测试）

- normal provider planning能返回完整frozen projection input；
- installed catalog/active source head可由continuity owner只读取得，不创建新owner；
- capability package不import compaction package；
- 测试不得构造summary、snapshot adoption、successor epoch或compaction transaction；
- 一个consumer在相同输入上复用central builders会得到byte-identical catalog/active rendering。

### 14.9 Architecture/oracle

- provider tool catalog中不存在`load_skill`、`skill_view`、`skill_use`；
- Skill source通过Round 9 pure registry factory注册，不存在Skill-private mutable registry/generation；
- `LocalSkillProvider`不自行构造root set，只消费explicit registration/binding tuple；
- model-call Skill activation lookup只在compiler final append result之后构造，由continuity PREPARED slot作为opaque exact-object attachment与candidate一起CAS安装，再由dispatch exact对象随tool batch传递；same-shape副本、raw-projection shortcut、fingerprint lookup cache、latest-catalog fallback或durable activation receipt均不存在；
- catalog没有short-description、partial admission或Skill专用大正文provider channel；
-无skill durable table/relation/event/job/guard；
-无absolute skill path进入provider diagnostics、event或telemetry；
- no reverse import from capability package into conversation repository；
-现有architecture oracle数量保持不变；
- Round 3/3.1/6/7/7.1/8 retained tests通过；Round 5B仍为draft，不作为本轮production test dependency。

---

## 15. 分片实施顺序

### Slice S0：机器基线

- 验证Round 7.1与Round 9均已ACTIVATED，冻结其public contract manifest、activation hash与retained node IDs；
- 记录实际HEAD、本文/上位文档hash；
- 记录pytest node IDs、architecture oracle与provider tool names；
- 锁定当前catalog/active golden；
- 不修改production。

### Slice S1：Agent Skills standard parser

- 以core standard替换Pulsara私有顶层schema；
- 增加六个root registrations/physical bindings、`.claude/skills` roots与official validation；
- scanner输出complete per-root source snapshots并进入Round 9 registry；
- 删除所有旧Skill dependency/health字段与parser，不增加namespaced替代；
- DTO/fingerprint与`read_file` cold schema升级；
- YAML/frontmatter/metadata安全bounds与authoring diagnostics；
- 保持当前provider output暂不改变；
- parser与Agent Skills conformance tests通过。

### Slice S2：portable projection与activation read

- catalog以唯一representation完整渲染每个admitted Skill的name/description/location；
- active/body projection只渲染exact parsed Markdown；
- 接入post-compile effective-head activation lookup、continuity dispatch-attachment CAS、dedup bypass与`read_file(intent=ACTIVATE_SKILL)`普通COMPLETE/FULL settlement；
- Round 6 direct/meta保持独立且retained tests通过。

### Slice S3：dynamic append-only skill

- bounded complete scan；
- safe tool-batch placement；
- change/clear/unavailable matrix；
- Chat/Responses strict-prefix tests通过。

这是Round 5B compaction实现前的硬前置。

### Slice S4：证据与文档

- 更新Gap Index、README与本文activation evidence；
- full pytest/PostgreSQL/Go/architecture/secret/link checks；
- gated real-provider smoke验证模型看到catalog后使用`read_file(intent=ACTIVATE_SKILL)`；另用正文指导验证现有direct/meta/terminal路径可正常调用，但Skill没有生成dependency route。

Round 5B compaction在本文ACTIVATED以后另行实施、另行review、另行生成activation evidence。

---

## 16. Definition of Done

只有全部满足才可标记ACTIVATED：

1. 同epoch所有skill add/change/remove/activation路径保持SYSTEM/tools不变、messages只追加suffix。
2. 六个default roots由Host显式注册并exact joinphysical bindings；`LocalSkillProvider`没有hidden root-registration authority。
3. 每次ordinary provider planning都能从complete per-root source snapshots与Round 9 registry冻结latest semantic catalog的唯一exact full-metadata representation；任何admitted Skill的description不得截断或摘要，overbound时整个catalog明确UNAVAILABLE。
4. Same-run active body在普通tool loop/automatic continuation中精确保持，不受文件热修改影响。
5. `read_file(intent=ORDINARY)`不会创建隐藏activation；`ACTIVATE_SKILL`只exact join该call经compiler最终effective catalog head与continuity CAS安装的frozen lookup、绕过ordinary dedup，并只在普通ToolResult COMPLETE、`FULL_REQUIRED/SKILL_ACTIVATION`选择与exact FULL continuity installation均成功时承载完整正文；aggregate不fit或cancel不降级、不改写canonical result且不激活。CLEARED/UNAVAILABLE/ABSENT head对应空lookup，它不创建permission state、durable loaded state或Skill专用大正文通道。
6. 不存在`load_skill`/`skill_view`/`skill_use` provider tool。
7. Agent Skills core fields、name/directory约束与progressive resource semantics通过official fixtures；旧Pulsara私有顶层字段不再拥有canonical语义。
8. 所有unsupported top-level host extension行为inert且不进入manifest semantic fingerprint；standard `metadata`（包括`pulsara.*`键）只可作为opaque local manifest数据进入manifest fingerprint，不进入catalog、activation、provider wire、dependency、health或authorization语义。
9. 不存在Skill-authored Tool/MCP/CLI dependency graph、health probe或route renderer；第三方standard Skill无需Pulsara改写即可使用。
10. `SKILL_CATALOG`与`ACTIVE_SKILL`均以`UNTRUSTED_OBSERVATION`进入messages；稳定BASE_SYSTEM单独说明使用规则与authority边界。Skill正文指导不受permission preset改写，真实调用仍走ordinary authorize/effect/attempt/invoke。
11. Catalog complete-scan failure不发布partial source/registry truth；旧snapshot被最小UNAVAILABLE终止。
12. Catalog与active来自一个frozen Skill registry/planning cut；MCP route继续由独立Round 9 input拥有；provider open前activation lookup exact joincompiler final effective source head，并作为opaque exact-object dispatch attachment与continuity candidate一起CAS安装。Raw projection或same-shape lookup副本不能越过CLEARED/UNAVAILABLE、NOT_APPLICABLE继承语义或physical root binding签发activation entry。
13. 本轮没有compaction import、summary、adoption、rebase、successor installation或compaction test owner。
14. 不新增schema、relation、event、job、guard、receipt、checkpoint、repair或cross-Host generation。
15. Architecture oracle与provider tool name baseline不变；`read_file` schema只在cold construction采用新版本，同epoch tools byte-equal。
16. Round 7.1与Round 9均已ACTIVATED，public contract manifests、activation hashes与retained node IDs exact匹配；本轮没有临时复制normal ToolResult或MCP meta contract。

---

## 17. 最终产品语义示例

### 17.1 运行中安装普通skill

~~~text
Agent使用write_file安装 .agents/skills/review/SKILL.md
-> write ToolResult完成
-> next safe point发现review
-> append latest SKILL_CATALOG snapshot
-> 模型看到review的用途与路径
-> 模型需要时read_file(intent=ACTIVATE_SKILL)完整正文
~~~

运行中安装没有SYSTEM rewrite或tool schema change，也没有load_skill；`read_file`的intent字段属于本轮部署后的cold baseline，不能在epoch中热改。

### 17.2 运行中安装提到新MCP的skill

~~~text
SKILL.md正文指导模型使用docs/search MCP tool
docs MCP在current epoch内READY
-> current native tools不变
-> SKILL_CATALOG只追加skill name/description/location
-> model完整读取Skill后，根据独立MCP observation调用inspect_new_mcp_tool
-> model再用use_new_mcp_tool(ref, arguments)
~~~

Skill没有声明、解析或持有MCP route；未来META_ONLY到DIRECT的cold promotion由Round 5B实现，不属于本例或本文activation gate。

### 17.3 Active skill执行中被修改

~~~text
user: "$review 检查当前改动"
-> ACTIVE_SKILL freezes review@A

Agent/外部编辑SKILL.md为review@B
-> catalog更新为B
-> current activation仍遵循A

next real user message再次$review
-> ACTIVE_SKILL replaces with B
~~~

### 17.4 隐式选择skill

~~~text
model sees catalog -> read_file(review/SKILL.md, intent=ACTIVATE_SKILL) -> follows it
-> exact current-run activation
-> ordinary canonical ToolResult records the exact parsed body seen
-> no derived authorization state is created
~~~

这条路径刻意不维护session中所有曾读skill，避免token浪费、动作空间膨胀和第二套activation authority。

### 17.5 GitHub CLI、Hugging Face CLI 与 Firecrawl MCP

标准Skill不需要也不允许Pulsara dependency metadata。CLI/MCP使用方法直接写进portable description与body：

~~~yaml
---
name: hugging-face-workflow
description: Use the Hugging Face Hub CLI for model, dataset, Space, cache, and job workflows. Use when the user asks to operate on Hugging Face resources.
compatibility: Requires the hf CLI and network access to huggingface.co
---

# Hugging Face workflow

Use the existing `terminal` tool to run `hf ...`. If the command is unavailable or authentication is missing, report the exact ToolResult and ask the user to configure it.
~~~

GitHub CLI同理在正文中说明通过固定`terminal`运行`gh ...`。Pulsara不在discovery时检查binary；缺失、auth或network问题由真实terminal attempt诚实返回。

Firecrawl若通过MCP提供能力：

~~~yaml
---
name: firecrawl-research
description: Search and extract web content with Firecrawl. Use for web research, scraping, mapping, and crawling tasks.
---

# Firecrawl research

Use the Firecrawl MCP tools currently exposed by the host. If they were discovered after the current epoch began, follow the Runtime observation and use the new-MCP inspect/use meta route.
~~~

该server在cold epoch已可靠连接时，模型会看到DIRECT tool；epoch中后到时则由独立MCP observation与`inspect_new_mcp_tool`/`use_new_mcp_tool`提供路径；未连接时真实调用不可用。Skill只提供guidance，MCP supervisor、permission、effect、dirty fence和executor拥有全部调用真值。

现有第三方Agent Skill因此可以原样被发现并使用。若某个发行包必须同时安装Skill与MCP/CLI，其作者应提供future Plugin bundle manifest；Pulsara既不要求用户改写`SKILL.md`，也不通过规则匹配生成依赖。

---

## 18. 最终判断

最终方案不是恢复旧Pulsara那套统一durable capability graph，也不是照搬grok-build的专用Skill tool，更不是把Codex的turn prompt重建直接套入Pulsara。

它保留三者真正有价值的部分：

- 旧Pulsara：discovery / exposure / gate / execution职责分层；
- Codex：执行上下文内immutable skill snapshot与explicit injection；
- grok-build：dynamic discovery append reminder与统一catalog renderer；
- Anthropic Agent Skills：canonical filesystem format、description routing与progressive disclosure；
- Pulsara：Round 3.1 strict prefix、Round 6 MCP authority、Round 9 direct/meta与canonical relational transcript。

其最小本质是：

~~~text
skill filesystem changes
    -> append-only catalog data

explicit activation
    -> exact active instruction snapshot

implicit use
    -> explicit read_file ACTIVATE_SKILL intent

permission/effect/execution
    -> existing tool/MCP authorities only
~~~

这同时满足产品可用性、prefix cache continuity与hard-cut后的减法原则，并为Round 5B留下可复用的frozen semantic input；本文不消费该接口进行compaction。

---

## 19. 审阅证据锚点

### 19.1 当前 Pulsara

- `src/pulsara_agent/capability/local_skills.py`：当前四legacy roots、deterministic precedence、bounded UTF-8 parse、私有frontmatter与unknown-tool过滤缺口；
- `src/pulsara_agent/capability/render.py`：catalog已经要求模型使用existing read tool完整读取`SKILL.md`；
- `src/pulsara_agent/capability/resolver.py`：catalog与active injection projection、显式`$skill`/`skill:name`；
- `src/pulsara_agent/conversation_kernel/capability.py`：`FrozenSkillProjectionInput`与startup allowlist交集；
- `src/pulsara_agent/conversation_kernel/context_sources.py`：`SKILL_CATALOG SNAPSHOT_ON_CHANGE`与`ACTIVE_SKILL ACTIVATION_SNAPSHOT`；
- `src/pulsara_agent/capability/builtin_catalog.py`、`src/pulsara_agent/tools/builtins/filesystem.py`：`read_file`支持workspace、absolute与`~` text reads，并具有分页/字符/device/binary bounds。

### 19.2 hard-cut 前 Pulsara

- `5b7ad9f7:src/pulsara_agent/capability/exposure.py`：`CapabilityExposurePlan`统一direct/deferred/hidden/callable、catalog与active injection；
- `5b7ad9f7:src/pulsara_agent/capability/runtime.py`：continuation exact reuse / monotonic narrowing；
- `5b7ad9f7:src/pulsara_agent/runtime/run_entry.py`：旧durable/process-local exposure working set；
- `archived_docs/CAPABILITY_SKILL_RUNTIME_V1_IMPLEMENTATION.zh.md`：普通file tool progressive disclosure是V1，`skill_view`仅为未来可选项；
- `archived_docs/PULSARA_UNIFIED_CAPABILITY_SURFACE_RESEARCH.zh.md`：skill首先是prompt capability，MCP是typed execution capability。

### 19.3 Codex

- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/session/turn_context.rs`：`TurnSkillsContext`持有`HostSkillsSnapshot`；
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/session/turn.rs`：显式skill mention与body injection；
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core-skills/src/loader.rs`与sample `agents/openai.yaml`：OpenAI-specific interface/dependency/policy位于可选sidecar，不扩张portable `SKILL.md`；
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core-skills/src/service.rs`：snapshot cache与clear/reload；
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/compact.rs`：同一turn context上的inline compaction。

### 19.4 grok-build

- `/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-tools/src/reminders/skill_discovery.rs`：file-tool之后的dynamic discovery；
- `/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-tools/src/types/skill_discovery_tracker/mod.rs`：startup/discovered set、pending reconciliation与compaction preservation；
- `/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-tools/src/types/skill_discovery_tracker/listing.rs`：startup/dynamic/compaction共用listing renderer；
- `/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-shell/src/session/helpers/compaction_context.rs`：compaction context重注入available skills；
- `/Users/plumliu/Desktop/python_workspace/grok-build/crates/codegen/xai-grok-tools/src/implementations/opencode/skill/mod.rs`：其专用Skill tool是本文明确不照搬的部分。

### 19.5 Anthropic / Agent Skills normative sources

- [Agent Skills specification](https://agentskills.io/specification)：canonical directory、frontmatter、name/directory equality、standard fields、supporting resources与progressive disclosure；
- [Anthropic skills repository](https://github.com/anthropics/skills)：Anthropic公开skills与规范迁移入口；
- [Claude Code skills documentation](https://code.claude.com/docs/en/slash-commands)：Claude-specific invocation control、supporting files与dynamic features；其中实验性host permission字段不进入Pulsara V1语义；
- [Claude Agent SDK skills documentation](https://code.claude.com/docs/en/agent-sdk/skills)：filesystem discovery与host execution-policy边界。

本地Claude Code窄探针进一步证明其`allowed-tools/hooks/context/model/effort/shell/paths`均依赖Claude自己的permission、hook、subagent与UI owner；本地Kimi Code窄探针则证明实际Skill几乎都只使用`name + description`，官方MCP Skill把使用方法写在正文、把server安装放在plugin manifest。两者都不是让Pulsara复制宿主frontmatter的依据。

以上来源的规范优先级固定为：Agent Skills core定义portable file contract；Claude/Kimi宿主字段只描述各自产品。Pulsara V1不增加host profile或standard metadata namespace语义。不得用当前Pulsara parser、某个宿主loader或第三方Skill的非标准写法反向修改core contract。
