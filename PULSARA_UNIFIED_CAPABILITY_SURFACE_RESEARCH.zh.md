# Pulsara Unified Capability Surface 调研与设计备忘

本文面向 `PULSARA_NEXT_MAINLINES_ROADMAP.zh.md` 中的「主线三：Unified Capability Surface」。这里的 capability / capacity 指同一类问题：模型可发现、可调用、可授权、可观测的一切能力面，包括内置工具、workflow 工具、memory 工具、本地 skill、未来 CLI adapter、未来 MCP server tool，以及更远期的 plugin / bundle。

本文不是实施 PR 计划。它先回答三件事：

1. 本地几个成熟 agent 产品如何管理 tool / skill / CLI / MCP 等能力。
2. Pulsara 当前已经有哪些可复用基础，还缺什么。
3. 下一步设计应吸收哪些长处、避开哪些坑。

## 0. 结论先行

Pulsara 不应该把 Unified Capability Surface 做成「一个更大的 ToolRegistry」。正确的方向是四层拆开：

```text
发现/注册 Registry
        ↓
本轮暴露 Exposure / Advertisement
        ↓
执行前授权 Gate / Policy
        ↓
执行与观测 Execution / Artifact / Trace
```

成熟产品都在用这四层，只是边界清晰度不同：

- Codex 的强项是 direct / deferred tool exposure、skill prompt progressive disclosure、MCP tool search 分流。
- Claude Code 的强项是 rich tool metadata、permission reason taxonomy、只读并发批处理、`SkillTool` 这种稳定包装器。
- Hermes 的强项是 registry generation、toolset 显式暴露、availability check cache、async handler bridge。
- OpenClaw 的强项是 manifest contract、plugin ownership metadata、静态 capability ownership 与运行时注册 drift 检查。

Pulsara 现在已经有：

- `ToolRegistry` 和 `ToolExecutor`。
- `Tool.is_read_only` / `Tool.is_concurrency_safe` 两个执行层元数据。
- `CapabilityResolver` / `LocalSkillResolver` / skill catalog prompt。
- plan mode、permission mode、artifact service、event log、inspector、resume / compaction 的事实层。

但 Pulsara 还缺：

- capability descriptor 作为本地真值，而不是散落在 Tool 对象、permission name allowlist、prompt 文本、artifact service 中。
- provider adapter：builtin / workflow / memory / local skill / CLI / MCP 各自接入统一 descriptor。
- exposure planner：注册了不等于本轮广告给模型。
- execution-time gate：广告给模型也不等于一定允许执行。
- health / availability / provenance / artifact policy 的统一表面。
- inspector 能解释「这个能力来自哪里、为什么可见、为什么被拒、输出为何落 artifact」。

一句话：下一步不是「把 MCP 接进来」或「让 skill 定义工具」，而是先冻结 capability descriptor 和 runtime 管线。否则每引入一种能力来源，都会复制一套权限、artifact、可观测和降级逻辑。

## 1. Pulsara roadmap §4 的关键约束

`PULSARA_NEXT_MAINLINES_ROADMAP.zh.md` 的 §4 明确把 Unified Capability Surface 定义为统一管理：

- built-in tools
- skills
- CLI adapters
- MCP tools
- workflow tools
- memory / terminal / artifact tools

它给出的 descriptor 草案已经很接近我们需要的控制面：

```text
name
provider: builtin / skill / cli / mcp / workflow
namespace
version
description
input_schema
output_schema
is_read_only
mutability
permission_category
approval_policy_hint
artifact_policy
streaming_policy
timeout_policy
provenance
availability
health_message
```

其中最重要的原则是：

- 模型广告不等于执行权限。
- 执行 gate 要读 descriptor，而不是只读 name allowlist。
- read-only mode 只允许 descriptor 声明为只读的能力。
- terminal / shell 仍是硬边界，不能被普通工具分类冲淡。
- 大输出、网页、搜索、JSON 应统一进入 artifact policy。
- 所有 provider 都应经过统一 registry / metadata / permission / observability。

这和本次调研结论完全一致。

## 2. Pulsara 当前实现基线

### 2.1 ToolRegistry 很薄

当前 `src/pulsara_agent/tools/registry.py` 的 `ToolRegistry` 只做：

- `register(tool)`
- `get(name)`
- `names()`
- `all()`
- `tool_specs()`

`tool_specs()` 只把 `name / description / parameters` 投给模型。它不知道 provider、namespace、provenance、health、artifact policy、timeout policy，也不持有权限分类。

这让 V0 很简单，但它无法承载 Unified Capability Surface。继续往 `Tool` 对象上零散加属性，会走向 Claude Code 式 mega object；短期方便，长期难以审计。

### 2.2 Tool 协议已有两个关键元数据

`src/pulsara_agent/tools/base.py` 已经定义：

- `is_read_only`
- `is_concurrency_safe`

`src/pulsara_agent/runtime/tool_loop.py` 也已经用这两个字段决定同批工具调用能否并发。这个方向是对的：执行层需要本地 metadata，而不是 prompt 规则。

但这两个字段还不够。未来至少还需要：

- `is_destructive`
- `is_open_world`
- `requires_user_interaction`
- `permission_category`
- `artifact_policy`
- `timeout_policy`
- `streaming_policy`
- `provider_kind`
- `provenance`
- `availability`

### 2.3 CapabilityResolver 已有「能力提示」骨架

`src/pulsara_agent/capability/types.py` 中已有：

- `CapabilityResolveContext`
- `ResolvedCapabilitySet`
- `CapabilityDiagnostic`
- `LocalSkillManifest`
- `ResolvedSkillCatalogEntry`
- `ActiveSkillInjection`

`src/pulsara_agent/capability/resolver.py` 的 `LocalSkillResolver` 已经能：

- 发现本地 skills。
- 渲染 catalog prompt。
- 根据 `$skill` / `skill:` 或 CLI `--skill` 激活完整 skill prompt。
- 返回 `visible_tool_names`。

这说明 Pulsara 已经有「能力 prompt 输入」层，但还没有「统一 capability execution control plane」。

### 2.4 ToolExecutor 已经是天然执行 choke point

`src/pulsara_agent/tools/executor.py` 是所有工具执行前后事件、artifact 处理、async runtime context 注入的集中点。下一步 capability gate 最自然的位置就在这里附近：

- 执行前读取 resolved descriptor。
- 判定 permission / approval / plan mode / read-only。
- 选择 timeout / streaming / artifact policy。
- 写 trace / diagnostic。

不要让 provider adapter 自己绕过这个 choke point。

### 2.5 Inspector / event log 已经是优势

Pulsara 现在的 event log、inspector、resume、compaction 已经比很多本地 agent 产品更结构化。Unified Capability Surface 应该直接利用这一点：

- capability advertised event / diagnostic 可以落 event log。
- tool denied / degraded / artifacted 可以被 inspector 解释。
- resume 后不需要重新猜某个历史 tool 为什么可见或不可见。

这是 Pulsara 的优势，不要退回「只有 JSONL transcript + 字符串日志」。

## 3. Codex 调研

本地路径：`/Users/plumliu/Desktop/python_workspace/codex`

重点文件：

- `codex-rs/core/src/mcp_tool_exposure.rs`
- `codex-rs/protocol/src/dynamic_tools.rs`
- `codex-rs/core-skills/src/model.rs`
- `codex-rs/core-skills/src/service.rs`
- `codex-rs/core-skills/src/injection.rs`
- `codex-rs/core/src/session/mcp.rs`

### 3.1 MCP tool exposure：direct vs deferred

Codex 的 MCP 暴露不是简单「发现多少工具就全塞给模型」。`mcp_tool_exposure.rs` 有明确的 direct / deferred 分流：

- MCP tools 会先被过滤成 model-visible tools。
- 当 search tool 可用且工具数量超过阈值，或 feature 开启时，工具进入 deferred exposure。
- deferred tools 不直接进入模型 tool schema，而是通过搜索 / deferred loading 机制发现。
- Codex Apps 相关 MCP tools 还会根据 connector、app tool policy、destructive/open_world hints 过滤。

可借鉴点：

- Pulsara 也需要区分「registry 中存在」与「本轮直接广告」。
- MCP/CLI 能力数量一多，直接广告会污染 schema 和上下文；必须准备 deferred discovery。
- direct exposure 阈值应是 exposure planner 的策略，不是 MCP adapter 的私有逻辑。

要避免：

- 不要把 deferred exposure 设计成 OpenAI/Codex provider 的 API 形状。Pulsara 应保留本地 descriptor truth，再由 provider adapter 转成目标 LLM API。

### 3.2 DynamicToolSpec：函数与 namespace

`dynamic_tools.rs` 中的 `DynamicToolSpec` 支持：

- function spec
- namespace spec
- `defer_loading`

这说明 Codex 已经把「工具集合」从单个 function call schema 推向命名空间和延迟加载。

可借鉴点：

- Pulsara descriptor 应有稳定 id / namespace，不要只靠扁平 name。
- 未来 MCP server 或 plugin 可以作为 namespace 暴露，而不是把每个工具硬塞成全局名字。

要避免：

- namespace 是模型广告形态，不一定等于 Pulsara 内部 ownership。内部仍要能追踪 provider / provenance / permission。

### 3.3 Skills：prompt/context capability，不等于 tool

Codex skill 体系包含：

- `SkillMetadata`
- `SkillPolicy.allow_implicit_invocation`
- `SkillDependencies`
- scope / plugin id / filesystem mapping
- disabled paths / load errors / implicit indexes

`build_skill_injections()` 只在显式 mention 时读取完整 `SKILL.md` 并注入。skills 不是默认生成 callable tool。

可借鉴点：

- skill 是 capability，但不一定是 tool。
- catalog 与完整 body 分离是必须的。
- skill load outcome 要保留 diagnostics：disabled、missing、ambiguous、load error。
- skill dependency 可以声明 MCP/tool 依赖，但不能自动绕过安装和 permission。

要避免：

- 不要让 skill 作者直接注册 arbitrary execution schema，至少 V1 不做。
- 不要把 `SKILL.md` 全量塞进每轮上下文。

### 3.4 MCP elicitation 作为 pending interaction

`session/mcp.rs` 中 MCP server 可以向用户发起 elicitation / approval-like request。Codex 将它接入 session state 和事件。

可借鉴点：

- Pulsara 已经有 plan question / pending interaction；未来 MCP elicitation 可以走同一类交互管线。
- capability provider 不能假定工具调用是纯 request/response；它可能需要用户授权、登录、配置、确认。

## 4. Claude Code 调研

本地路径：`/Users/plumliu/Desktop/python_workspace/claude-code`

重点文件：

- `src/Tool.ts`
- `src/tools.ts`
- `src/services/tools/toolExecution.ts`
- `src/services/tools/toolOrchestration.ts`
- `src/tools/MCPTool/MCPTool.ts`
- `src/tools/SkillTool/SkillTool.ts`
- `src/hooks/useCanUseTool.tsx`
- `src/utils/permissions/permissions.ts`
- `src/services/mcp/client.ts`

### 4.1 Tool object 非常富

Claude Code 的 `Tool` 对象同时承载：

- name / aliases / prompt / schema
- read-only / destructive / concurrency / open-world / user-interaction classification
- permission check
- input validation
- result truncation
- UI rendering
- grouping
- transcript search
- MCP/LSP flags
- deferral / always-load

默认值倾向 fail-closed：

- `isReadOnly` 默认 false。
- `isConcurrencySafe` 默认 false。

可借鉴点：

- Pulsara descriptor 的分类维度应补齐：read-only、concurrency-safe、destructive、open-world、requires-user-interaction、max-result-size。
- 默认必须 fail-closed；新能力如果没有 descriptor，不应该自动读作安全。
- 权限拒绝应有 reason taxonomy，而不是一串 `[TOOL_ERROR] Permission denied`。

要避免：

- 不要照搬 mega Tool object。Claude Code 把执行、UI、权限、渲染、搜索等揉在一个对象里，功能强但耦合重。
- Pulsara 应把核心 descriptor、execution adapter、UI/render helper、inspector presentation 分层。

### 4.2 Tool orchestration：只读并发批处理

Claude Code 会把连续的 concurrency-safe/read-only tool calls 分批并发运行，非只读或不安全工具串行。

Pulsara 已经有相似方向：`is_read_only` + `is_concurrency_safe`。下一步 capability descriptor 应把这两项提升为 registry truth，而不是各工具对象私有属性。

可借鉴点：

- batch decision 要读 capability metadata。
- 如果一个 provider adapter 不能证明并发安全，默认 false。

### 4.3 Permission：allow / deny / ask + reason

Claude Code 的 permission 流不只是布尔：

- allow
- deny
- ask
- mode/rule/hook/classifier/subcommand 等 reason

可借鉴点：

- Pulsara capability gate 应返回结构化 `CapabilityGateDecision`：
  - `allow`
  - `deny`
  - `ask`
  - `reason_code`
  - `reason_message`
  - `policy_source`

这和 Pulsara 现有 plan mode / approval / permission mode 可以自然接起来。

### 4.4 SkillTool：稳定包装 skill invocation

Claude Code 没有让每个 skill 都变成独立 tool schema，而是用稳定的 `SkillTool(skill,args)` 包装。skill 被调用后再加载 prompt，甚至可以 fork sub-agent。

可借鉴点：

- 如果 Pulsara 未来想让模型主动调用 skill，不应让每个 skill 注册一把新 tool。可以考虑一个稳定 `skill_use` 或 `skill_view` tool。
- skill invocation 和 tool execution 是两类 capability；前者主要改变上下文，后者执行 side effect。

要避免：

- `allowed-tools` 这类 skill frontmatter 不能直接越权修改 Pulsara permission policy。最多作为建议/diagnostic。

## 5. Hermes Agent 调研

本地路径：`/Users/plumliu/Desktop/python_workspace/hermes-agent`

重点文件：

- `tools/registry.py`
- `model_tools.py`
- `toolsets.py`
- `acp_adapter/tools.py`
- `AGENTS.md`
- `SECURITY.md`

### 5.1 Registry：发现与暴露分离

Hermes 的 registry 会通过模块扫描和 import-time registration 发现 tools。但是否给模型使用，仍由 toolsets 决定。

`ToolEntry` 包含：

- `name`
- `toolset`
- `schema`
- `handler`
- `check_fn`
- `requires_env`
- `is_async`
- `description`
- `emoji`
- `max_result_size_chars`
- `dynamic_schema_overrides`

registry 有：

- generation counter
- RLock
- check_fn TTL cache
- duplicate/shadowing protection
- snapshots for readers

可借鉴点：

- `registry generation` 很有价值：capability set 可以缓存，但要随 registry generation 失效。
- availability / health check 应便宜且有 TTL。
- duplicate name 默认拒绝；跨 provider shadowing 必须显式 override。
- dynamic schema 只能引用当前 available 的能力，避免 schema/prompt 提到不可用工具。

要避免：

- import-time side effects 不适合 Pulsara 作为主架构。它让注册时机、异常、权限边界变得不透明。
- string table 分类容易漂移。Hermes 的 ACP tool kind map/title map 是后置字符串映射，Pulsara 应把分类写进 descriptor。

### 5.2 Persistent async loop lesson

Hermes 为 async tool handler 做了持久 event loop bridge，避免每次 `asyncio.run()` 带来的 loop/client 问题。

这和 Pulsara 记忆召回/async provider 生命周期评审是同一个坑：CLI/MCP/async tool adapter 不能随便在 worker thread 里新建 loop 共享 async client。

可借鉴点：

- capability provider 如果有 async resource，必须有明确 owner 和 shutdown。
- descriptor 里应能表达 provider lifecycle / health，但执行资源所有权不能藏在 handler 闭包里。

### 5.3 SECURITY.md 的现实主义

Hermes 明确说：真正的 containment boundary 是 OS/container/remote host，不是 approval gate、redaction 或 allowlist。

可借鉴点：

- Pulsara 的 capability gate 是误操作防线，不是 sandbox。
- CLI/MCP/plugin adapter 引入后，必须在文档中把 trust boundary 说清楚。不要暗示 descriptor 能提供进程级隔离。

## 6. OpenClaw 调研

本地路径：`/Users/plumliu/Desktop/python_workspace/openclaw`

重点文件：

- `VISION.md`
- `docs/tools/plugin.md`
- `docs/plugins/manifest.md`
- `src/plugins/manifest-types.ts`
- `src/plugins/tools.ts`
- `src/agents/tools/common.ts`
- `src/agents/tool-policy.ts`
- `src/plugins/manifest-tool-availability.ts`
- `extensions/voice-call/openclaw.plugin.json`
- `extensions/memory-core/openclaw.plugin.json`

### 6.1 Core lean，能力走 plugin/bundle

OpenClaw 的 VISION 明确：

- core 保持 lean。
- optional capability 通常应做成 plugin。
- 有 code plugin 和 bundle-style plugin 两类。
- memory 是特殊 exclusive plugin slot。
- MCP 是集成面，但不应该重复 tool/plugin 路径。

可借鉴点：

- Pulsara 可以先不做完整 plugin manager，但要从第一天给 provider/provenance/owner 留字段。
- memory / terminal / context-engine 这类核心能力可以有 exclusive slot 概念：同一 slot 只能有一个 owner。

### 6.2 Manifest 是 cheap control plane

OpenClaw plugin manifest 是无需执行插件代码即可读取的元数据：

- identity
- config schema
- UI hints
- auth/setup
- activation metadata
- static contracts
- `contracts.tools`
- `toolMetadata`

它还会检查 runtime 注册工具是否超出 manifest 声明，发现 contract drift。

可借鉴点：

- Pulsara 的未来 CLI/MCP/skill bundle provider 应优先读静态 descriptor / manifest，再决定是否加载代码或连接 server。
- 静态 ownership contract 和运行时注册之间要有 drift diagnostic。

要避免：

- OpenClaw manifest 很强，但也很大。Pulsara V1 不应复制完整 plugin/package manager。
- 先做 descriptor + adapter + inspector；安装市场、版本解析、包管理可以后置。

### 6.3 Plugin tool metadata 与 policy

OpenClaw 为 plugin tool 绑定：

- plugin id
- optional / trustedLocalMedia / mcp meta
- allow/deny group expansion
- optional plugin tools 显式 allow
- slow factory warning
- malformed tool detection

可借鉴点：

- Pulsara descriptor 必须能回答「这个 tool_owner 是谁」。
- observability labels 应有 provider/source/owner。
- group policy 可以存在，但执行时仍要展开到 descriptor 后判断。

## 7. 四个项目横向对比

| 维度 | Codex | Claude Code | Hermes | OpenClaw | Pulsara 应取 |
| --- | --- | --- | --- | --- | --- |
| 注册 | skills snapshot、MCP registry | central tool list + MCP/skills | registry generation + import discovery | manifest + runtime registration | provider adapter → descriptor registry |
| 暴露 | direct/deferred MCP，skill catalog | tool list + SkillTool | toolsets 显式选择 | manifest/agent filter/allowlist | exposure planner，注册≠广告 |
| skill | prompt/context capability | SkillTool 包装 | skills_list/skill_view | available_skills + read tool | skill 首先是 prompt capability，不默认是 executable tool |
| MCP | direct/deferred + elicitation | MCPTool stub + client | MCP registry toolset | MCP as integration/plugin surface | MCP adapter 产 descriptor，支持 deferred |
| CLI | 非主要调研对象 | shell/tool 分类丰富 | tools 可封装 CLI | plugin/provider 可暴露 | CLI adapter 必须有 cwd/env/timeout/artifact policy |
| 权限 | app/MCP policy + approval | allow/deny/ask reason | toolsets + host containment | allow/deny group/plugin/tool | descriptor-driven gate |
| artifact | tool result / large output 有处理 | max result + persistence | max_result_size | diagnostics/metrics | artifact_policy 一等字段 |
| health | load errors / disabled paths | isEnabled / MCP status | check_fn TTL | manifest availability | availability + health_message |
| 风险 | API-shape 耦合 | Tool mega object | import side effects/string maps | manifest 过大 | 小 descriptor + 分层 |

## 8. Pulsara 应吸收的长处

### 8.1 从 Codex 吸收：direct / deferred exposure

Pulsara 未来 MCP/CLI provider 一定会遇到工具数量膨胀。应该提前设计：

- `advertise_policy = direct | deferred | hidden`
- `defer_reason`
- `search_hint`
- `namespace`

模型每轮看到的直接 tool schema 应稳定、可解释、可预算。大量工具应通过 `tool_search` / capability search / namespace discovery 进入，而不是全量塞进 LLM API。

### 8.2 从 Claude Code 吸收：安全分类与权限 reason

Descriptor 至少应覆盖：

- `is_read_only`
- `is_concurrency_safe`
- `is_destructive`
- `is_open_world`
- `requires_user_interaction`
- `permission_category`
- `approval_policy_hint`
- `max_result_size`

Gate 结果应覆盖：

- allow / deny / ask
- reason code
- policy source
- optional user-facing explanation

### 8.3 从 Hermes 吸收：registry generation 与 health cache

Capability registry 应有：

- generation counter
- deterministic snapshot
- duplicate/shadowing rejection
- cheap availability check
- health TTL

这能让 CLI/MCP provider 不必每轮重新连接/探测，也能让 inspector 解释 stale/degraded 状态。

### 8.4 从 OpenClaw 吸收：manifest/contract 与 ownership

未来 provider 不应只运行代码后动态注册。更安全的顺序是：

1. 读取静态 descriptor/manifest。
2. 做 availability / config / permission 初筛。
3. 必要时加载 runtime adapter。
4. 检查 runtime 注册是否与静态 contract 一致。

Descriptor 里必须有：

- provider kind
- provider id
- owner / provenance
- source path / server / command
- version

## 9. Pulsara 应避免的短处

### 9.1 避免 name-only permission

工具名 allowlist 是最后 fallback，不应是主机制。否则 MCP/CLI/plugin 一进来，重名、别名、shadowing、namespace 都会让 permission 失真。

### 9.2 避免把 descriptor 做成 UI mega object

Claude Code 的 Tool object 很强，但 Pulsara 不应把 UI rendering、permission、execution、artifact、search、diagnostics 都塞进一个 Python Protocol。更好的结构是：

- `CapabilityDescriptor`：控制面真值。
- `CapabilityExecutor` / provider adapter：执行。
- `CapabilityRenderer`：CLI / app 展示。
- `CapabilityInspector`：诊断解释。

### 9.3 避免 import-time registration 成为主路径

Hermes 的 import discovery 对本地快速开发很舒服，但生产能力管理更需要：

- 静态声明。
- 加载前检查。
- 加载失败可诊断。
- 禁止隐式 side effects。

### 9.4 避免 skill 直接越权扩展工具

skill 可以声明 `provides_tools` / `requires_tools` / `suggested_tools`，但不能因为 frontmatter 写了某工具，就让 runtime 打开权限。

尤其不能支持：

```yaml
allowed-tools: "*"
permission: bypass
```

这类字段即使兼容读取，也只能作为 diagnostic 或被忽略。

### 9.5 避免 CLI adapter 忽略 artifact 与 timeout

CLI-backed tools 最容易产生：

- 大 stdout/stderr。
- 长运行。
- 交互式挂起。
- cwd/env 泄漏。
- exit code 语义不清。

因此 CLI capability 必须声明：

- cwd policy
- env policy
- timeout
- streaming
- max output
- artifact policy
- interactive policy

没有这些就不应该注册为生产 capability。

### 9.6 避免 async resource owner 不清

MCP client、browser session、embedding provider、HTTP client 都可能是 async resource。不要让它们藏在 tool function closure 里，也不要跨线程/跨 event loop 共享。

Provider adapter 必须声明 lifecycle owner，并接入 HostSession / RuntimeSession 的关闭链。

## 10. 粗略 v1 设计方向

这里不是完整实施计划，只定义下一步设计形状。

### 10.1 CapabilityDescriptor

建议先在 `src/pulsara_agent/capability/descriptor.py` 或同级文件定义一个本地真值 DTO：

```python
@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    id: str
    name: str
    namespace: str | None
    provider_kind: Literal["builtin", "workflow", "memory", "skill", "cli", "mcp"]
    provider_id: str
    version: str | None
    description: str
    input_schema: dict[str, object] | None
    output_schema: dict[str, object] | None
    is_model_callable: bool
    is_read_only: bool
    is_concurrency_safe: bool
    is_destructive: bool
    is_open_world: bool
    requires_user_interaction: bool
    permission_category: str
    approval_policy_hint: str | None
    artifact_policy: str
    streaming_policy: str
    timeout_policy: str
    advertise_policy: Literal["direct", "deferred", "hidden"]
    provenance: CapabilityProvenance
    availability: Literal["available", "degraded", "unavailable"]
    health_message: str | None
```

注意 `id` 和 `name` 不应混为一谈：

- `name` 是模型可能看到的调用名。
- `id` 是内部稳定 capability identity，可包含 provider/namespace/version。

### 10.2 Provider adapter

每个 provider 输出 descriptor，而不是直接注册 tool schema：

```python
class CapabilityProvider(Protocol):
    provider_id: str
    provider_kind: str

    def snapshot(self, context: CapabilityProviderContext) -> CapabilityProviderSnapshot:
        ...
```

Provider 类型：

- builtin tool provider：包装现有 `ToolRegistry`。
- workflow provider：plan tools、compact tools、memory governance 之类。
- local skill provider：已有 `LocalSkillResolver` 可拆出 descriptor/catalog。
- CLI provider：读取静态 config，产生命令 descriptor。
- MCP provider：连接 server 或读取 cached manifest，产生命名空间 descriptor。

### 10.3 Registry

`CapabilityRegistry` 负责：

- 聚合 provider snapshot。
- 检查 duplicate / shadowing。
- 生成 generation。
- 保存 diagnostics。
- 提供 deterministic snapshot。

它不负责本轮暴露，也不直接执行。

### 10.4 Exposure planner

`CapabilityExposurePlanner` 根据：

- 当前 permission mode。
- plan mode。
- workspace kind。
- active skills。
- user input。
- token/tool schema budget。
- provider health。

决定：

- 哪些 direct advertise。
- 哪些 deferred advertise。
- 哪些 hidden。
- 本轮 `ToolSpec`。
- capability prompt fragment。
- diagnostics。

这层对应 Codex 的 direct/deferred MCP exposure 和 Pulsara 当前 `CapabilityResolver`。

### 10.5 Execution gate

`CapabilityGate` 在 tool call 到达后重新判断：

- capability 是否仍存在。
- descriptor 是否允许当前 permission mode。
- plan mode 是否阻断。
- 是否需要 approval。
- 是否 health degraded。
- 是否超出 workspace / terminal / memory domain。

这一步必须存在，因为模型看到工具 schema 与真实执行之间可能隔了很久；resume / plan revise / provider health 变化都可能改变可执行性。

### 10.6 Artifact policy

`ToolResultArtifactService` 应从 capability descriptor 读取策略：

- inline limit。
- artifact media type。
- redaction 默认。
- truncation behavior。
- structured JSON preservation。
- whether large stdout must artifact.

不要让每个 tool 自己私下决定大输出怎么塞回模型。

### 10.7 Inspector

`pulsara inspect workspace capability` 已经有静态能力检查入口。它应扩展为能回答：

- registry 里有哪些 provider。
- 某 capability 为什么可见/不可见。
- 某 tool call 为什么被拒。
- 某输出为什么被 artifact。
- 某 MCP/CLI capability 为什么 degraded。
- name 是否被 shadowed。

## 11. CLI / MCP / Skill 的边界建议

### 11.1 Skill

V1 中 skill 仍应主要是 prompt/context capability：

- catalog 可见。
- full `SKILL.md` 按需注入或读取。
- 可声明 suggested/required tools，但不能新增 tool schema。
- 可影响 prompt，不直接影响 permission。

未来如果要模型主动加载 skill，优先考虑稳定包装器：

- `skill_view(name, path?)`
- `skill_use(name, args?)`

而不是每个 skill 变一个 tool。

### 11.2 CLI adapter

CLI capability 必须静态声明：

- command / argv template
- allowed cwd policy
- env allowlist
- stdin policy
- stdout/stderr max bytes
- timeout
- interactive false by default
- mutability/read-only classification
- artifact policy

CLI adapter 不应默认继承 terminal 工具权限。否则它会成为绕过 terminal hardline 的后门。

### 11.3 MCP adapter

MCP provider 应输出 descriptor snapshot：

- server id
- tool namespace
- tool name
- schema
- server health
- auth/login state
- elicitation support
- destructive/open-world hints 如果可得

大量 MCP tools 默认 deferred，不应全 direct advertise。MCP elicitation 应复用 pending interaction / approval 管线。

### 11.4 Builtin / memory / workflow

内置能力不应该享受 metadata 例外。它们也要有 descriptor：

- memory_search / memory_get
- terminal
- plan workflow tools
- compact tools
- file tools
- inspector tools

这样 read-only、plan mode、artifact policy 才能统一。

## 12. 编码时最可能遇到的坑

### 12.1 ToolSpec 生成位置会诱惑你绕过 descriptor

当前 `build_llm_context(...)` 从 `registry.tool_specs()` 取工具。如果只是把 `ToolRegistry.tool_specs()` 改复杂，容易把 exposure planner 和 registry 混在一起。

建议先新增一层：

```text
CapabilityRegistry snapshot
        ↓
ResolvedCapabilitySet / ExposurePlan
        ↓
LLMContext.tools
```

ToolRegistry 仍可短期保留，但它不是 Unified Surface 的最终入口。

### 12.2 Permission name allowlist 还会存在一段时间

短期兼容需要 name allowlist，但新能力必须 descriptor-driven。可以先让 name allowlist 作为 legacy fallback，并在 inspector 里标记。

### 12.3 `is_read_only` 语义必须统一

Pulsara 当前注释已经说「mutating agent-local ephemeral state still counts as read-only」。这个语义要写进 capability descriptor，否则各 provider 会各自解释。

CLI/MCP 的 read-only 默认 false，除非显式证明。

### 12.4 Artifact policy 不能等到工具输出后再猜

如果输出已经是巨大 stdout 或二进制，再决定 artifact 往往太晚。Descriptor 应提前告诉 executor：

- 是否 streaming。
- 是否强制 artifact。
- inline 上限。
- truncation 规则。

### 12.5 MCP / async provider 生命周期

MCP client 很可能是 async resource。不要复制曾经记忆 provider 的 loop/thread 风险：

- provider owner 明确。
- close 幂等。
- drain 有界。
- 不跨 event loop 共享 async client。

### 12.6 Skill 与 tool 的边界容易再次混淆

`provides_tools` 听起来像 skill 可以提供工具，但 V1 应只表示「这个 skill 会指导模型使用这些已有工具」。如果未来支持 skill-defined tools，应换一个明确字段，例如 `declares_tools`，并要求静态 descriptor + permission review。

### 12.7 Provider health 不能阻塞热路径

Health check 要有 TTL / cached snapshot。不要每次用户输入都探测所有 MCP server / CLI binaries / network。

### 12.8 Namespace 与模型 tool name 的映射

LLM tool API 通常要求扁平 name，但内部 capability 需要 namespace。必须设计稳定映射，例如：

```text
internal id: mcp:github/issues.create
model name: github__issues_create
```

并保证 inspector 能反查。

## 13. 建议的阶段性设计判断

先做：

1. `CapabilityDescriptor` 和 provider snapshot。
2. 用 descriptor 包装现有 built-in tools。
3. Exposure planner 生成本轮 `ToolSpec`。
4. Execution gate 读取 descriptor。
5. Inspector 显示 descriptor / exposure / gate reason。
6. Local skill 继续作为 prompt capability，接入同一 registry 的 non-callable capability。

后做：

1. CLI adapter。
2. MCP adapter。
3. Deferred capability search。
4. Static manifest / bundle contract。
5. Plugin manager / package install。

明确暂不做：

- skill arbitrary tool schema。
- MCP 全量 direct advertise。
- plugin marketplace。
- 让 capability ontology / JSON-LD 反向规定 runtime DTO。

## 14. 判断标准

Unified Capability Surface 的第一轮是否成功，不看它接了多少 provider，而看它是否让以下问题都有结构化答案：

- 这个能力从哪里来？
- 为什么本轮模型看得到它？
- 为什么执行时允许 / 拒绝 / 要求确认？
- 它是否只读、是否可并发、是否 destructive、是否 open-world？
- 它的输出为什么 inline / artifact / truncate？
- 它当前 health 为什么 degraded？
- resume / inspect 能否解释历史上的选择？

如果这些问题仍需要翻 prompt 文本、name allowlist 或工具私有代码，那就还没有完成 Unified Capability Surface，只是新增了一批工具。

