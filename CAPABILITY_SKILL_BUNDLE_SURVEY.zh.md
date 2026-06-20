# Capability / Skill Bundle 调研

本文调研 Pulsara 在引入 capability / skill bundle 之前应当学习的现有系统。范围包括本地四个项目：

- `/Users/plumliu/Desktop/python_workspace/codex`
- `/Users/plumliu/Desktop/python_workspace/claude-code`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent`
- `/Users/plumliu/Desktop/python_workspace/openclaw`

同时参考公开官方文档：

- OpenAI Codex Skills: https://developers.openai.com/codex/skills
- OpenAI Codex Plugins: https://developers.openai.com/codex/plugins
- Anthropic Agent Skills overview: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview
- Anthropic Agent Skills best practices: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices

结论先行：成熟实现都把 skill 当作一个可发现、可按需展开的 bundle，而不是一个无条件塞进 prompt 的大文件。最小闭环通常由三层构成：

1. 低成本 catalog：name / description / location 等元数据进入上下文，供模型选择。
2. 按需完整加载：明确触发、匹配任务、或调用读取工具后，才读取完整 `SKILL.md`。
3. 支撑文件和脚本延迟读取/执行：`references/`、`scripts/`、`assets/` 只在 `SKILL.md` 指向且任务需要时使用。

这对 Pulsara 的启发是：capability 的第一轮不是重新发明一个大 capability ontology，而是在 runtime 已存在的三个活 seam 上实现一个窄的 resolve 契约：

- `ToolRegistry.tool_specs()`
- system prompt fragment 组合点
- memory `read_scopes / allowed_write_scopes`

现有 `ontology/capability.py` 和 `entities/capability/skill.py` 可以作为持久化词汇参考，但不能反过来规定第一轮 runtime contract。它们目前没有 runtime 消费者。

## Pulsara 当前状态

已核实的 runtime 事实：

- `src/pulsara_agent/tools/registry.py` 的 `ToolRegistry.tool_specs()` 静态返回当前 registry 中所有工具的 `ToolSpec`。
- `src/pulsara_agent/runtime/context.py` 的 `build_llm_context(...)` 把 `registry.tool_specs()` 放入 `LLMContext.tools`。
- `src/pulsara_agent/runtime/agent.py` 的 `_with_memory_context_prompt(...)` 是当前 system prompt fragment 的活组合点；memory prompt 就在这里拼入。
- `src/pulsara_agent/memory/scope.py` 已经定义 `ctx:user`、`ctx:workspace/<hash>`、`MemoryDomainContext.read_scopes`、`allowed_write_scopes`。
- `src/pulsara_agent/runtime/wiring.py` 已经把 `memory_domain` 穿进 memory query/write 工具和 memory hooks。
- `src/pulsara_agent/ontology/capability.py` 定义 `Skill / Tool / Plugin / Policy` 以及 `PROVIDES_TOOL / REQUIRES / HAS_INPUT_SCHEMA / ALLOWED_IN_SCOPE / BLOCKED_IN_SCOPE` 等谓词。
- `src/pulsara_agent/entities/capability/skill.py` 有 `Skill` JSON-LD entity，但 grep runtime 使用点为空；它只在 ontology / JSON-LD 层存在。

因此 Pulsara 不是“没有 capability 概念”，而是“建模层和 runtime 层互不触碰”。这和之前 MemoryDomainContext、terminal yield、host core 的问题同型：模型已存在，消费者还没接线。第一轮设计必须从消费者倒推 contract。

## OpenAI Codex

### 官方文档事实

OpenAI Codex Skills 官方文档把 skill 定义为一个包含 instructions、resources、optional scripts 的 reusable workflow。Codex 使用 progressive disclosure：初始上下文只包含每个 skill 的 name、description、file path；完整 `SKILL.md` 只有在 Codex 决定使用该 skill 时才加载。

文档还给出几个重要边界：

- 初始 skill 列表有预算：最多 2% context window，未知窗口时 8,000 characters。
- 预算只作用于初始列表；选中 skill 后仍会读取完整 `SKILL.md`。
- skill 是目录：`SKILL.md` 必需，`scripts/`、`references/`、`assets/` 可选。
- 激活方式包括显式调用和隐式调用：CLI/IDE 可以用 `/skills` 或 `$` mention；任务匹配 description 时也可隐式选择。
- repo/user/admin/system 多级 location；repo 下是 `.agents/skills`。
- plugin 是分发单元，可包含 skills、apps、MCP servers；plugin 本身不是 workflow，skills 才是 workflow authoring format。
- `agents/openai.yaml` 可声明 UI metadata、`policy.allow_implicit_invocation`、MCP tool dependencies。

### 本地源码事实

关键文件：

- `codex-rs/core-skills/src/model.rs`
- `codex-rs/core-skills/src/loader.rs`
- `codex-rs/core-skills/src/render.rs`
- `codex-rs/core-skills/src/injection.rs`
- `codex-rs/core/src/context/available_skills_instructions.rs`
- `codex-rs/core/src/mcp_skill_dependencies.rs`
- `codex-rs/core/src/mcp_tool_exposure.rs`

本地源码与官方文档一致：

- `SkillMetadata` 包含 `name`、`description`、`short_description`、`interface`、`dependencies`、`policy`、`path_to_skills_md`、`scope`、`plugin_id`。
- `SkillPolicy.allow_implicit_invocation` 默认允许，false 时不进入隐式触发候选。
- `render_available_skills_body(...)` 生成 `## Skills`、skill roots、available skills、how-to-use 指令。
- how-to-use 指令要求模型在决定使用 skill 后读取 `SKILL.md` 完整内容；相对路径按 skill 目录解析；`references/`、`scripts/`、`assets/` 按需读取或运行。
- `default_skill_metadata_budget(...)` 使用 2% context window 或 8,000 字符默认预算。
- `build_available_skills(...)` 会缩短 description 或省略部分 skills，并产生 warning。
- `build_skill_injections(...)` 读取被显式 mention 的 skills 的完整内容，并封装为 `<skill>` 注入。
- 显式 mention 支持 `$skill` 和 `skill://` / linked mention 的路径匹配。
- MCP dependency install 是显式 prompt / install / login 流程，不是 skill 一出现就把所有 MCP 工具暴露给模型。
- MCP tool exposure 有 direct/deferred 分流：工具过多或 feature 开启时，MCP tools 可以通过 search/lazy mechanism 暴露。

Codex 的核心模式是：catalog 进入 prompt，完整 skill 是按需注入；MCP/app/plugin 作为外部能力有独立安装、权限和 lazy exposure 逻辑。

## Claude Code

### 本地源码事实

关键文件：

- `src/skills/loadSkillsDir.ts`
- `src/tools/SkillTool/SkillTool.ts`
- `src/tools/SkillTool/prompt.ts`
- `src/commands.ts`
- `src/skills/bundledSkills.ts`
- `src/skills/mcpSkillBuilders.ts`

Claude Code 的 skill 在实现上是 prompt command：

- `/skills` 目录只支持 `skill-name/SKILL.md`。
- legacy `/commands` 也能被转换为 skill-like prompt command。
- project/user/managed/additional/bundled/plugin/MCP skills 都会汇总成 `Command`。
- frontmatter 支持丰富字段：`description`、`when_to_use`、`allowed-tools`、`argument-hint`、`arguments`、`model`、`disable-model-invocation`、`user-invocable`、`hooks`、`context: fork`、`agent`、`effort`、`shell`、`paths`。
- `estimateSkillFrontmatterTokens(...)` 和 `SkillTool/prompt.ts` 的 `formatCommandsWithinBudget(...)` 只把 name / description / whenToUse 等元数据放进 skill listing。
- `SkillTool` 是模型可调用工具，schema 只有 `skill` 和 optional `args`。
- `SkillTool` 调用后才加载 skill prompt；inline skill 把完整内容注入主对话，`context: fork` 可以交给子 agent 执行。
- `allowed-tools` 不是新工具 schema，而是修改 permission / allowed tool context。
- `disable-model-invocation` 会阻止模型通过 SkillTool 使用该 skill。
- MCP skills 只有 `loadedFrom === 'mcp'` 且 `type === 'prompt'` 时才进入 SkillTool；普通 MCP prompts 即使存在也不会被 SkillTool 猜名调用。
- `paths` frontmatter 可做条件激活：匹配读写文件路径后，把原本未暴露的 conditional skills 加进动态 skills map。
- nested `.claude/skills` 可在文件操作路径上动态发现，但会检查 gitignored 路径。

Claude Code 比 Codex 更强调“skill 作为一个工具调用”。它没有让每个 skill 生成独立 LLM tool schema，而是用一个稳定的 `SkillTool` 包装 skill invocation。这很重要：它避免了每新增一个 skill 就扩大 provider tool schema 面，降低 optional 参数 materialization 风险。

## Hermes

### 本地源码事实

关键文件：

- `tools/skills_tool.py`
- `agent/skill_commands.py`
- `agent/skill_bundles.py`
- `agent/skill_preprocessing.py`
- `agent/skill_utils.py`
- `model_tools.py`

Hermes 的结构最接近传统 progressive disclosure tool：

- skills 位于 `~/.hermes/skills/`，每个 skill 是含 `SKILL.md` 的目录，可有 `references/`、`templates/`、`scripts/`、`assets/`。
- `tools/skills_tool.py` 明确写了 progressive disclosure：metadata 在 `skills_list`，完整内容在 `skill_view`，linked files 也通过 `skill_view(name, file_path)` 读取。
- frontmatter 遵循 agentskills.io 语义：`name`、`description`、`version`、`license`、`platforms`、`prerequisites`、`compatibility`、`metadata` 等。
- `skills_list` 返回 name + description，提示使用 `skill_view(name)` 查看完整内容。
- `skill_view` 可以读取主 `SKILL.md` 或 supporting file。
- `/skill-name` slash command 通过 `agent/skill_commands.py` 加载完整 skill 内容，注入一条 user message，并附上 skill directory、supporting files、setup notes 等。
- `agent/skill_bundles.py` 支持一个 bundle slash command 同时加载多个 skills。
- platform/environment filters 是 offer-time relevance gate；显式加载可以绕过。
- `model_tools.py` 中 `skills_tools` 只是全局工具集的一部分，skill 本身不动态生成新 tool schema。

Hermes 的启发是：第一轮即使不做动态工具过滤，也可以用 `skills_list/skill_view` 形成清晰的 progressive disclosure。但 Pulsara 当前更适合先把 catalog/prompt fragment 接进现有 `build_llm_context`，再决定是否需要单独的 skill view tool。

## OpenClaw

### 本地源码事实

关键文件：

- `src/skills/loading/skill-contract.ts`
- `src/skills/loading/workspace.ts`
- `src/skills/types.ts`
- `src/skills/discovery/agent-filter.ts`
- `src/skills/runtime/session-snapshot.ts`
- `src/skills/discovery/skill-index.ts`
- `src/plugins/contracts/inventory/bundled-capability-metadata.ts`

OpenClaw 的实现对 Pulsara 有两个特别有价值的提醒：

第一，skill prompt formatter 很窄：

- `formatSkillsForPrompt(skills)` 输出 `<available_skills>`。
- 每个 entry 只有 `<name>`、`<description>`、`<location>`。
- prompt 明确要求模型用 read tool 加载匹配 skill 的 file。

第二，它把 exposure 拆成多个正交维度：

- `includeInRuntimeRegistry`
- `includeInAvailableSkillsPrompt`
- `userInvocable`
- `disableModelInvocation`

这意味着“可以被用户命令看到”“可以被模型 catalog 看到”“可以被 runtime registry 使用”不是一回事。Pulsara V1 不一定要复制这些字段，但应该避免把 exposure 设计成一个 bool。

其他重要机制：

- `resolveEffectiveAgentSkillFilter(...)`：agent 显式 skills 列表优先，否则使用 defaults；未知 agent fallback defaults，不扩大访问。
- `buildWorkspaceSkillSnapshot(...)`：构建 prompt 和 resolved skills snapshot。
- `resolveReusableWorkspaceSkillSnapshot(...)`：通过 version、skillFilter、agentId、eligibility、config fingerprint 缓存/刷新。
- `bundled-capability-metadata.ts` 明确写着 build/test inventory only；runtime 应优先使用 manifest/runtime registry。这一点直接支持 Pulsara 不应让 dead ontology 规定 live runtime contract。

## Anthropic Agent Skills 官方文档

Anthropic 官方文档将 Skill 定义为 filesystem-based resources，包含 workflows、context、best practices。关键机制：

- Skills load on-demand，不是 conversation-level 一次性 prompt。
- filesystem architecture 让 progressive disclosure 成立。
- 三层加载：
  - Level 1 metadata：`name` 和 `description` always loaded。
  - Level 2 instructions：task 匹配后读取 `SKILL.md` body。
  - Level 3 resources/code：额外 markdown、reference、scripts 按需读取或执行。
- `SKILL.md` YAML frontmatter 必需 `name` 和 `description`。
- `description` 必须同时说明 skill 做什么、何时使用。
- best-practices 强调 `SKILL.md` 作为 overview / table of contents，长内容拆到单层 reference 文件；脚本应执行而不是全文塞上下文。
- security considerations 明确把 skill 当作安装软件级风险：恶意 skill 可引导工具误用、数据外泄、执行危险代码。

这说明 capability bundle 的本体不是 prompt text，而是“可由 agent 在文件系统中导航的一组材料”。Pulsara V1 即使只支持 local filesystem skill provider，也应保留目录、supporting files、relative path 的语义，而不是只读取一个 markdown 字符串。

## 对比矩阵

| 系统 | 初始上下文 | 完整 skill 加载 | 支撑文件 | 工具暴露 | scope/filter | permission |
| --- | --- | --- | --- | --- | --- | --- |
| OpenAI Codex | name/description/path，2% 或 8k budget | 显式 `$skill` / 隐式匹配后读完整 `SKILL.md` | `references/`、`scripts/`、`assets/` 按需 | 不由 skill 任意生成工具；MCP deps 单独安装，MCP tools 可 deferred | repo/user/admin/system source scope；enable/disable config | plugin/app/MCP 走安装、认证、approval |
| Claude Code | SkillTool prompt 中列出可用 skills，budgeted | 调用 `SkillTool(skill,args)` 后加载 | skill root、supporting files、inline/fork context | 稳定 `SkillTool` 包装；`allowed-tools` 调权限上下文 | project/user/managed/plugin/bundled/MCP；paths 条件激活 | unknown / unsafe properties 可触发 permission；deny/allow rules |
| Hermes | `skills_list` 只列 metadata；slash command 可列技能 | `skill_view` 或 `/skill` 加载完整内容 | `skill_view(name,file_path)`；scripts/templates/assets | `skills_list/skill_view` 是全局工具；skill 不新增 schema | platform/environment offer-time filters | setup/secret collection 有单独逻辑 |
| OpenClaw | `<available_skills>` name/description/location | 模型用 read tool 读取 location | 相对路径按 skill dir | exposure 拆成 registry/prompt/userInvocable | agent skills filter、snapshot、eligibility | runtime registry/manifest，不让 inventory 决定 runtime |
| Anthropic 官方 | metadata always loaded | triggered 后读 `SKILL.md` | resources/code on-demand | Skills 在 code execution env 中用 bash/read/run | surface-dependent sharing scope | 强调 trusted source 和 malicious skill 风险 |

## 共同设计规律

1. Catalog 和 full body 必须分离。
   初始 prompt 只放 name / description / location；完整 `SKILL.md` 只有被选中后才进入上下文。

2. Tool schema 不应随 skill 任意增长。
   Claude Code 用一个 `SkillTool` 包装 skill invocation；Hermes 用 `skill_view`；Codex 对 MCP dependencies 有独立安装和 deferred tool exposure。没有一个成熟实现鼓励本地 skill 任意注册新的 provider tool schema。

3. Description 是激活主索引。
   官方文档和源码都依赖 description 做隐式匹配、prompt catalog 和选择。description 必须包含“做什么”和“什么时候用”。

4. Scope/filter 是 host/runtime 的事，不是 skill 自己说了算。
   Codex 有 source scope，OpenClaw 有 agent filter，Claude Code 有 project/user/managed source 和 policy，Hermes 有 platform/environment offer filter。skill frontmatter 可声明意图，但 runtime 必须最终裁决。

5. Permission 和 external capability 要单独处理。
   MCP、browser、plugin、app 都可能联网或有副作用。成熟系统不会因为 skill 提到它们就无条件开放。Pulsara V1 应只做本地 trusted provider，MCP/browser permission 后置。

6. Progressive disclosure 是上下文经济，也是一种安全边界。
   不相关 skill 的完整正文不进入上下文，就减少 prompt injection 面；支撑文件按需读取，也减少无关材料污染。

7. Snapshot/cache 是优化，不是契约源头。
   OpenClaw snapshot 是 session/runtime 复用机制；其 bundled capability inventory 明确不是 runtime truth。Pulsara 应先定义 live resolve contract，再考虑缓存。

## 对 Pulsara 的直接建议

第一轮只做 input capability，且只接 local skill provider：

- 发现 `<workspace>/.pulsara/skills/<skill-name>/SKILL.md`、`<workspace>/.agents/skills/<skill-name>/SKILL.md`、`${PULSARA_HOME}/skills/<skill-name>/SKILL.md`、`~/.agents/skills/<skill-name>/SKILL.md`。
- 不发现 `~/.codex/skills`、`~/.claude/skills`、`~/.hermes/skills`、`~/.openclaw/skills` 或其他 agent 的 product home skills。
- 解析 `name`、`description`、可选 `when_to_use`、`provides_tools`、`disable_model_invocation`、`user_invocable`；V1 不暴露作者可写 `allowed_scopes/blocked_scopes`。
- 生成低成本 available skill catalog prompt fragment，location 使用非绝对 display path，并对 XML-ish wrapper 中的字段做 escaping。
- 支持显式 `$skill` / `skill:<name>` / slash-like host command 激活完整 skill。
- full body 保持 raw Markdown，用 collision-checked sentinel fence 作为 prompt fragment 注入当前用户消息对应的 agent run，而不是永久改 system prompt。
- supporting files 第一轮只在 prompt 中告诉模型如何通过现有 file tools 读取；不新增专用 `skill_view`，除非后续发现 file path 暴露体验差。
- `provides_tools` 只能引用已有工具名，不能声明新 schema。
- resolver 内部复用 `ctx:user` 和 `ctx:workspace/<hash>`，不能发明 parallel skill scope；workspace-local skill 的归属由文件位置表达。
- capability resolve 在一次用户消息边界只执行一次；该用户消息内多次模型调用共享同一个 resolved set 和工具列表。

推迟项：

- MCP/browser/plugin adapters。
- permission_hook。
- implicit activation 的复杂检索模型。
- agent 产出/安装 skill，即 capability output。
- JSON-LD ontology persistence。
- per-skill arbitrary tool schema。

本轮最重要的负约束：不要让已经 runtime-dead 的 `Skill` ontology 反向规定 runtime contract。它可以帮助命名字段，但必须等第一轮 live consumer 跑通后再映射。
