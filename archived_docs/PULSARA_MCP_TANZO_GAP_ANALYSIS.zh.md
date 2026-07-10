# Pulsara MCP 生产化：Tanzo 对照审计与借鉴计划

_Created: 2026-07-05_

本文记录对本地 Tanzo MCP 实现的代码级调研，并对照 Pulsara 当前 MCP 实现，明确：

- Tanzo 在 MCP 生产化上比 Pulsara 强在哪里；
- Pulsara 当前已经具备哪些更硬的 runtime 基础；
- 下一步 MCP 生产化最应该从 Tanzo 借鉴什么。

本文只比较 core / runtime / MCP 管理能力，不比较前端 UI 的视觉完成度。

---

## 1. 调研范围

Tanzo 主要查看：

- `/Users/plumliu/Desktop/python_workspace/Tanzo/docs/architecture/21-mcp.md`
- `/Users/plumliu/Desktop/python_workspace/Tanzo/docs/architecture/12-tools.md`
- `/Users/plumliu/Desktop/python_workspace/Tanzo/src/main/mcp/module.ts`
- `/Users/plumliu/Desktop/python_workspace/Tanzo/src/main/mcp/service.ts`
- `/Users/plumliu/Desktop/python_workspace/Tanzo/src/main/mcp/client.ts`
- `/Users/plumliu/Desktop/python_workspace/Tanzo/src/main/mcp/transport.ts`
- `/Users/plumliu/Desktop/python_workspace/Tanzo/src/main/mcp/env.ts`
- `/Users/plumliu/Desktop/python_workspace/Tanzo/src/main/mcp/store.ts`
- `/Users/plumliu/Desktop/python_workspace/Tanzo/src/main/agent/tools/mcp.ts`
- `/Users/plumliu/Desktop/python_workspace/Tanzo/src/main/agent/tools/registry.ts`
- `/Users/plumliu/Desktop/python_workspace/Tanzo/src/main/agent/plugins/loader.ts`
- `/Users/plumliu/Desktop/python_workspace/Tanzo/tests/unit/main/mcp/*`

Pulsara 当前对照点：

- `contracts/MCP_CAPABILITY_CONTRACT.zh.md`
- `src/pulsara_agent/runtime/mcp/*`
- `src/pulsara_agent/capability/providers/mcp.py`
- `src/pulsara_agent/tools/adapters/mcp.py`
- `tests/test_capability_mcp.py`

---

## 2. 总体判断

Tanzo 的 MCP 比 Pulsara 当前更接近“生产可用”，主要强在产品化外壳与协议完整性：

- 有用户可配置的 MCP server store；
- 有 service / client / transport / IPC / renderer 的完整管理链路；
- 使用成熟 MCP SDK，而不是长期手搓 wire protocol；
- 有 server lifecycle sync、连接状态、远程重连、资源/Prompt API、elicitation UI；
- 有 plugin / built-in server 贡献与合并策略；
- transport 与 env 安全细节更扎实。

Pulsara 的优势在另一个方向：runtime 骨架更硬。

- MCP 已经接进 unified capability surface；
- descriptor 与 execution binding 通过 `McpCapabilityBindingBundle` 同源生成；
- capability exposure / permission gate / tool execution / artifact / event log 能形成更强审计链；
- context compiler 与 artifact preview 对大 MCP tool result 更友好；
- pending interaction 体系天然适合承载 MCP elicitation。

因此差距不是“Pulsara MCP 方向错了”，而是：

> Pulsara 已经有更严谨的 runtime 接线骨架，但还缺 Tanzo 那种让用户真实配置、连接、重连、管理、调试 MCP server 的生产化外层。

本轮版本决策已经收敛：

> Pulsara MCP 生产主路径直接面向官方 Python MCP SDK v2 线构建。稳定版发布前，依赖真源是 `mcp[cli]==2.0.0b1`；仓库同时显式 pin `mcp-types==2.0.0b1` 作为 lockstep guard，升级 PR 必须从 `mcp` metadata 校验两者完全一致，不能独立推进 `mcp-types`。所有 SDK beta API 必须通过 Pulsara 自己的 `McpClientManager` / SDK facade 隔离。不维护 v1 生产兼容分支；当前手搓 stdio/http JSON-RPC manager 只能作为历史 spike 或局部测试参考，不再作为生产协议层目标。

---

## 3. Tanzo 比 Pulsara 强的地方

### 3.1 用户可配置的 MCP server store

Tanzo 有 `mcp_servers` SQLite 表和 `McpStore`，支持：

- create；
- update；
- delete；
- toggle enabled；
- list；
- duplicate name validation；
- transport-specific sanitize。

这让 MCP server 成为用户可管理的产品对象，而不是内部 wiring 参数。

Pulsara 当前有 `McpServerConfig` 和 manager/bundle，但缺少稳定用户入口。换句话说，Pulsara 现在更像“可以被测试注入 MCP manager”，还不是“用户可以配置 MCP server 并长期使用”。

应该借鉴：

- 建立 Pulsara MCP config/store；
- 提供 `pulsara mcp list/add/remove/enable/disable/doctor`；`config-check` 只检查基础环境，不隐式连接 remote MCP；
- 支持 workspace 级与 user 级 MCP 配置；
- 明确 user config 优先级。

---

### 3.2 Desired-state lifecycle sync

Tanzo 的 `McpClient.syncServers(servers)` 是一个很值得抄思路的核心：

- 已删除 server：disconnect；
- disabled server：disconnect，但保留 disconnected state；
- config 未变且已连接：保持连接；
- 新增或变更 server：connect / reconnect；
- 按 server name 串行化操作，避免并发启动/关闭竞态；
- dispose 时清理 reconnect timers 与 active connections。

Pulsara 目前已有 session close -> manager close 的基本链路，但还缺 server-level desired-state reconciliation。

应该借鉴：

- 新增或强化 `McpServerSupervisor` / `McpClientManager.sync_servers()`；
- manager 不只暴露静态 `snapshots`，还应该拥有 server state machine；
- snapshot generation 应随 server config/tool list 更新；
- Host/session close 仍是 owner，但 server reconnect/sync 应属于 MCP manager/supervisor。
- 生产 manager 采用 **一 server 一 SDK `Client`** 的形态，由 Pulsara supervisor 聚合多 server snapshot；不要让 SDK 的 multi-server aggregate 成为命名、权限或 descriptor 真源。
- SDK `ClientSessionGroup` 如未来使用，只能封装在 Pulsara facade 内做实现细节；其合并工具名、资源名、prompt 名的结果不能直接进入模型 tools，也不能覆盖 Pulsara 的 `server_id + original_tool_name -> model tool name` 规则。

---

### 3.3 SDK-backed MCP 协议覆盖

Tanzo 使用 `@ai-sdk/mcp` 的 `createMCPClient`，覆盖了更多真实 MCP surface：

- tools；
- resources；
- resource templates；
- prompts；
- elicitation；
- serverInfo；
- server instructions；
- cursor pagination；
- uncaught connection error；
- request timeout / abort。

Pulsara 当前 stdio/http manager 更像最小 JSON-RPC spike：

- `tools/list`；
- `tools/call`；
- `elicitation/respond`；
- 简单 fake server / fixture 测试。

这能证明 wiring，但不等于能稳定接真实 MCP server。

应该借鉴：

- 使用官方 Python MCP SDK v2 beta 作为生产协议层主路径；
- SDK 依赖必须精确 pin，不使用宽松 prerelease range；
- Pulsara runtime 不直接散落 SDK 类型；所有 SDK 对象必须收敛在 `McpClientManager` / SDK facade 内；
- current hand-rolled stdio/http manager 不继续扩展为生产协议栈，只保留为 legacy fixture / spike reference；
- v2 stable 发布后按专门 PR 升级 pin，并跑真实 MCP dogfood；
- 不要让 Pulsara 的价值陷入“手搓 MCP 协议细节”。Pulsara 的价值应在 runtime、权限、事件、记忆、context 编译，而不是 MCP wire protocol 本身。

---

### 3.4 Transport 与 env 安全细节

Tanzo 的 transport 层有很多生产细节：

- stdio child env 经过 `safeChildEnv` 过滤 ambient secrets；
- config 显式声明的 env 才合并进去；
- remote URL 必须是 `http` / `https`；
- remote redirect 默认 `error`；
- URL / headers 只展开非敏感环境变量；
- Windows `.bat` / `.cmd` shim 通过 `cmd.exe /d /c`；
- Windows batch args 拦截 unsafe metacharacters。

Pulsara 当前 transport/env 层还比较薄，尤其是：

- stdio env 默认传递策略需要冻结；
- remote headers/token 的 redaction 要进入事件/inspector 契约；
- stderr capture 需要 bounded；
- command/args 必须坚持 no-shell 语义；
- Windows 兼容暂时可以后置，但设计上要留位置。

应该借鉴：

- 将 MCP transport 单独做成安全边界；
- 明确 env 传播策略：ambient env 默认过滤，explicit env 可传；
- 任何 token/header 不进入 prompt、event、inspector 明文；
- remote URL/header/env 展开必须避免敏感值误展开；
- stdio server stderr 必须有限制，不能无限堆积。

---

### 3.5 插件与内置 MCP server 贡献模型

Tanzo 的 server 来源有三类：

```text
用户 DB server > plugin server > built-in server
```

插件可以声明 `.mcp.json`，Tanzo loader 读取并转换为 native `McpServerConfig`。内置 server 如 `chrome-devtools` 通过 lazy provider 注入，并可被用户同名覆盖。

这比单一配置文件更接近生态化。

Pulsara 目前已经有 skill / capability / plugin 思路，但 MCP server 贡献源还没有稳定模型。

应该借鉴：

- MCP server source 至少分成 user / workspace / plugin / builtin；
- 合并策略必须 deterministic；
- 用户配置应能 shadow plugin/builtin；
- plugin-contributed MCP server 不应直接绕过用户可见状态；
- builtin MCP server 应有安全边界，例如 Tanzo 对 chrome-devtools 加 `--blockedUrlPattern file://**` 防止 agent 注入应用自身 renderer。

---

### 3.6 Elicitation / InputRequired 用户交互链路

Tanzo 的 elicitation 是真实 MCP client 收到 server request 后，通过主进程 pending map 与 renderer IPC 交互：

```text
MCP server request
  -> client onElicitationRequest
  -> module pendingElicitations[requestId]
  -> renderer event
  -> user accept / decline / cancel
  -> resolve promise
  -> MCP SDK returns result
```

并且有 5 分钟默认超时，无窗口时自动 cancel。

这个模式对旧 back-channel / legacy elicitation 很有参考价值，但不能原样搬到官方 Python MCP SDK v2 的现代路径。

SDK v2 的现代协议把交互式输入表达为 `InputRequiredResult`：

```text
client.session.call_tool(..., allow_input_required=True)
  -> CallToolResult | InputRequiredResult
InputRequiredResult
  -> input_requests + request_state
  -> Pulsara pending interaction
  -> user answer / decline / cancel
  -> retry same request with input_responses + same request_state
```

生产实现必须手动接管这个 loop。也就是说，Pulsara SDK-backed manager 不应直接调用高层 `Client.call_tool()` 并让 SDK 自动驱动 input-required callback；而应调用 `client.session.call_tool(..., allow_input_required=True)`，在收到 `InputRequiredResult` 后把 run 挂起。

pending payload 必须保存足够恢复同一次交互的事实：

- `server_id`；
- wrapper tool call id / wrapper tool name；
- source method：`tools/call`、`resources/read` 或 `prompts/get`；
- complete Pulsara-owned original request DTO；
- for `tools/call`：original tool name 与 original arguments；
- for `resources/read`：resource URI；
- for `prompts/get`：prompt name 与 prompt arguments；
- `protocol_version`；
- `request_state` 字段必须存在，值可为 `None` / `null`；
- `input_requests` 的 Pulsara-owned DTO；
- input-required round count；
- timeout/deadline；

manager/facade 必须把 SDK `InputRequest` 转成 Pulsara-owned、可持久化、可 inspect 的 DTO 后再进入 pending payload；HostSession / CLI / UI 只读取这个 DTO，不接触 SDK 类型。这个 DTO 至少要保留问题文本、字段 schema、默认值、必填性、枚举/选项、validation hints 与 display hints；不得把 SDK pydantic model、SDK enum 或任何 SDK-only object 直接写进 pending payload / event log。

resume 时，HostSession 只产 Pulsara-owned、可持久化的 resolution DTO；SDK `InputResponses` 只能由 SDK-backed manager/facade 在内部构造。manager 用同一个 `request_state` 字段值与同一个原始 request 重试；如果 `request_state` 为 `None` / `null`，manager 必须原样传回或按 SDK API 省略，不得合成、推断或用 Pulsara round id 替代新 state。如果再次返回 `InputRequiredResult`，继续 suspend；超过 round cap、用户 cancel、session close 或 request_state 失效时，返回结构化 cancel/error。

旧的 `elicitation/respond` 只能算手搓 spike / legacy fixture，不是 SDK v2 生产主路径。

应该借鉴：

- 真实 MCP input-required 必须从 manager/client 层进入 `ToolExecutionSuspended`；
- pending payload 必须包含 server id、source method、request_state、input_requests、完整原始 request DTO 与 protocol version；
- resource/prompt 如果通过 wrapper tool 触发，payload 必须同时保留 wrapper tool 身份与 underlying MCP request 身份；
- v1 只有 wrapper tool / runtime tool-call 路径支持 input-required suspend；direct CLI / inspect / doctor 路径如果遇到 resource/prompt input-required，先 fail-closed 并产 diagnostic，不创建 pending interaction，也不在 CLI 内临时追问用户；
- CLI/REPL 需要友好展示 accept/decline/cancel；
- detach/resume/compact 时 pending input-required 不应丢失；
- timeout / close / no active user channel 时应返回 cancel。

---

### 3.7 Resources / Prompts 的管理面

Tanzo 不只暴露 tools，还提供：

- `listResources`
- `readResource`
- `listResourceTemplates`
- `listPrompts`
- `getPrompt`

Pulsara 当前主要聚焦 tool。短期可以接受，但 v1 边界必须写清楚，否则 MCP 后续会从 tools 膨胀成混乱能力面。

应该借鉴：

- v1 可先以显式工具或 inspect/CLI 暴露 resources/prompts；
- 不要自动把 MCP prompts 注入系统提示词；
- resource read 应进入 artifact/context 体系；
- 大 resource 内容必须走 adaptive preview / artifact；
- resource/prompt 使用也要有 capability/gate/inspect 事实。

---

### 3.8 工具命名与 allow/disable 体验

Tanzo 将 MCP tool 命名为：

```text
mcp__<server>__<tool>
```

并支持：

- 长名称 hash suffix；
- allowedTools pattern；
- disabled tools settings；
- annotation -> read/edit kind。

Pulsara 已经有类似的 `mangle_mcp_tool_name()` 和 64 字符上限，这一块方向一致。但 Tanzo 的用户禁用/允许工具体验更完整。

应该借鉴：

- MCP tool names 要可逆解释；
- inspector/doctor 显示 original server/tool；
- 支持按 server 禁用、按 tool 禁用；
- 支持 name pattern / allowlist；
- 禁用状态必须进入 exposure plan，不只是 UI 隐藏。

---

## 4. Pulsara 当前更强的地方

### 4.1 Unified capability surface 更严谨

Tanzo 的 MCP tools 最终是 ToolSet + `metadata.tanzo.kind`。简单有效，但声明真值较薄。

Pulsara 当前有：

```text
CapabilityDescriptor
  -> CapabilityExposurePlan
  -> capability_gate_decision
  -> ToolRegistry execution binding
```

这使得 MCP 可以被统一纳入：

- descriptor/binding fail-closed；
- hidden/unavailable/callable 检查；
- permission gate；
- artifact policy；
- inspector；
- event log replay。

这是 Pulsara 未来超过 Tanzo 的关键基础，不能为了快速接 MCP 而绕开。

---

### 4.2 Event ledger 与 inspect 潜力更强

Pulsara 的 typed event log 能把 MCP 的历史事实持久化：

- server snapshot generation；
- capability exposure；
- gate decision；
- tool call/result；
- artifact preview；
- pending elicitation；
- resume/cancel；
- compaction boundary。

Tanzo 有状态和 repo，但 Pulsara 的事件账本更适合回答“历史上为什么可见、为什么被拒、为什么这样恢复”。

下一步 MCP 生产化必须利用这个优势，而不是只做 live manager 查询。

---

### 4.3 Artifact / context compiler 对大 MCP 输出更友好

真实 MCP tools 很容易返回大结果。Pulsara 已经有：

- adaptive preview；
- artifact_read；
- context compiler；
- context compaction；
- model-visible estimate；
- tool result metadata。

这些对于 MCP 大输出非常重要。Tanzo 的 context engine 很成熟，但 Pulsara 的三层存储与 artifact 机制更适合长期可追溯。

---

### 4.4 Pending interaction 体系适合 MCP elicitation

Pulsara 已经有 plan question、approval、MCP elicitation 等 pending interaction 形态。MCP elicitation 可以复用这个控制平面，而不是做一条独立 UI/IPC 链。

关键是下一步要接真实 server elicitation，而不是只在 adapter 测试参数里模拟。

---

## 5. Pulsara 应该从 Tanzo 借鉴的下一步

### M1. MCP config/store/doctor

优先级最高。

建议新增：

- user-level MCP config；
- workspace-level MCP config；
- merge priority；
- `pulsara mcp list`;
- `pulsara mcp add`;
- `pulsara mcp enable/disable`;
- `pulsara mcp reconnect`;
- `pulsara mcp doctor`;
- `pulsara config-check` 不做 MCP 网络连接；MCP health 统一由 `pulsara mcp doctor` 输出，避免配置检查命令产生远端副作用或慢启动。

验收：

- 用户无需写 Python 测试代码即可配置真实 MCP server；
- REPL 启动能加载并显示 MCP diagnostics；
- missing auth / command missing / URL invalid 有清晰错误。

---

### M2. Desired-state MCP supervisor

实现类似 Tanzo `syncServers()` 的状态对齐：

- server config diff；
- connect/disconnect/reconnect；
- per-server operation serialization；
- remote reconnect backoff：v1 采用“下一次 supervisor sync / manual reconnect 时按 backoff 重试”的轻量策略，不启动后台 reconnect loop；
- stdio 是否自动重连要明确，建议 v1 不自动重连；
- snapshot generation 更新；
- session close 有界关闭全部 server。

验收：

- enabled -> disabled 断开连接但保留状态；
- config 变更触发 reconnect；
- remote / startup failure 不会因相同 fingerprint 永久黏住，retry 到期后下一次 sync 会 reconnect；
- close 不泄漏 process/client。

---

### M3. 官方 Python MCP SDK v2 manager

M3 不再做“SDK-backed 或自研完整 client”的二选一。生产主路径固定为官方 Python MCP SDK v2 线：

- 当前依赖真源：`mcp[cli]==2.0.0b1`；
- 当前额外 lockstep guard：`mcp-types==2.0.0b1`；
- `mcp-types` 不能独立升级；每次升级 PR 必须从 `mcp` package metadata 校验它声明的 `mcp-types` 版本与直接 pin 完全一致；
- 这个 guard 是为了让 `uv` 在默认 prerelease 策略下稳定解析，同时避免开启项目级 “allow all prerelease”；
- 不维护 MCP SDK v1 生产兼容口；
- SDK beta API 只能出现在 `src/pulsara_agent/runtime/mcp/sdk_*` 或等价 facade 内；
- 上层仍只依赖 Pulsara-owned DTO：`McpServerConfig`、`McpServerSnapshot`、`McpDiscoveredTool`、`McpToolResult`、`McpClientManager`；
- stdio 与 streamable HTTP 都通过一 server 一 SDK `Client` 建立；
- SDK session 初始化后生成同一份 snapshot，再由 `McpCapabilityBindingBundle` 同时产出 descriptor 与 `AsyncTool` binding；
- SDK errors、timeouts、cancellation、connection close 都必须归一成 Pulsara manager status / diagnostics / tool result / suspension。

需要覆盖的 SDK surface：

- initialize / server capabilities；
- tools/list 与 tools/call；
- resources/list/read；
- prompts/list/get；
- content variants；
- standard MCP errors；
- cancellation / timeout；
- streamable HTTP session semantics；
- notifications/logging/progress 的 v1 产品边界。

#### M3.1 input-required loop

生产 manager 必须手动接管 SDK v2 modern input-required：

- 对 `tools/call` 使用 `client.session.call_tool(..., allow_input_required=True)`；
- 对 `resources/read` 使用 `client.session.read_resource(..., allow_input_required=True)`；
- 对 `prompts/get` 使用 `client.session.get_prompt(..., allow_input_required=True)`；
- 收到 terminal `CallToolResult` / `ReadResourceResult` / `GetPromptResult` 时正常返回；
- 收到 `InputRequiredResult` 时抛出/返回 Pulsara 的 suspension 信号；
- suspension payload 持久化 `request_state`、`input_requests`、原始 request、server/tool/prompt/resource id、protocol_version 与 round count；
- resume 时 manager/facade 根据 Pulsara-owned resolution DTO 构造 SDK `input_responses`，并用 `input_responses + request_state` 重试同一 request；
- 不使用 SDK 高层自动 driver 作为生产主路径，因为它无法把 pending interaction 纳入 Pulsara event log / resume / inspect。

#### M3.2 tool result DTO 映射

真实 MCP tool result 不能继续“只 join text”。SDK v2 `CallToolResult` 至少要映射：

- `content: list[ContentBlock]`
  - `TextContent`：进入 model-facing output/preview；
  - `ImageContent` / `AudioContent` / blob-like resource：归档为 artifact，model-facing output 只放摘要与 artifact ref；
  - `EmbeddedResource`：按 resource 内容类型归档或预览；
  - `ResourceLink`：保留 uri/name/mime/description 等 metadata，必要时提示可用 MCP resource read 路径；
- `structured_content`：保存为 JSON artifact 或 metadata；小型 JSON 可以进入 preview，大型 JSON 必须走 artifact；
- `is_error=True`：这是 tool-visible error result，应作为成功返回的可见 tool result 交给模型自我修正，不等同于 MCP protocol exception；
- top-level `MCPError` / transport error / validation error：这是执行/协议错误，进入 diagnostics、tool error event 与 manager status，不伪装成 `is_error=True` 的正常 result。

#### M3.3 pagination / cache / notification 边界

SDK list APIs 有 cursor，snapshot sync 与 doctor 必须 drain pagination：

- `list_tools`、`list_resources`、`list_resource_templates`、`list_prompts` 都必须支持 cursor loop；
- 每类 list 都要有 max pages / max items；
- repeated cursor 必须 fail-closed，避免无限循环；
- snapshot sync / doctor 默认使用 `cache_mode="refresh"`，避免 stale SDK response cache 掩盖 server 实际状态；
- 普通 read/list 可按后续产品策略使用 SDK cache，但 cache 命中必须进入 diagnostics；
- 除非使用 Pulsara-owned/custom cache store 或 facade-level instrumentation 能可靠记录 hit/miss，否则生产路径必须 `cache=False` 或强制 `cache_mode="refresh"`；SDK 默认 cache 如果不能被 Pulsara 事件/diagnostics 观测，视为不可用；
- v1 不承诺 notification-driven live invalidation；server list/resource/prompt 变化只通过 manual refresh、reconnect、TTL 或下一轮 sync 生效；
- Python SDK v2 当前 listen/subscription 相关能力不作为 Pulsara v1 生产验收项。

验收：

- 接真实 `@modelcontextprotocol/server-filesystem`；
- 接真实 remote/http MCP server；
- 接 reference/everything MCP server；
- 所有 server 能完成 list tools 与一次 tool call。
- SDK import smoke test 固定在当前 pin；
- 手搓 manager 不参与 production wiring；
- v2 stable 替换 beta pin 时，不能改变 Pulsara 上层 manager / bundle / adapter 接口。
- input-required tool 能 suspend/resume/cancel；
- `is_error=True` result 被模型看见，protocol error 进入 tool error；
- pagination max pages/items 与 repeated cursor guard 有测试。

---

### M4. Transport/env/security hardening

借鉴 Tanzo transport 层：

- stdio command/args 不走 shell；
- child env 默认过滤 ambient secrets；
- explicit env 才传敏感值；
- remote URL 只允许 http/https；
- headers/token redaction；
- stderr bounded；
- startup/request timeout；
- remote streamable HTTP 禁止直接使用 `Client(url)` URL shorthand，因为 SDK 默认 HTTP client 会 `follow_redirects=True`；
- 生产路径必须构造自己的 `httpx.AsyncClient`，统一设置 redirect policy、headers/auth、timeout、proxy、event hook/redaction，再传给 `streamable_http_client(url, http_client=...)`；
- redirect 默认应 fail-closed；如未来允许 redirect，必须有 explicit policy、目标 host 校验与 redacted diagnostics；
- Windows shim 策略可后置，但接口要留好。

验收：

- env secret 不进入 event/prompt/inspector；
- remote bearer token 缺失显示 needs_auth；
- invalid URL fail-fast；
- stdio stderr 不会撑爆内存或 prompt。

---

### M5. MCP permissions 与 per-server/tool policy

在 Pulsara 现有 gate 上扩展：

- MCP server-level allow/deny；
- MCP tool-level enable/disable；
- annotation 缺失默认保守；
- readOnlyHint 只能降风险，不能绕过 policy；
- destructive/open-world 默认 ask；
- approval payload 展示 server/tool/original args。

验收：

- hidden/unavailable/not-callable MCP tool call-local deny；
- read-only profile 下 MCP edit tool 拒绝；
- trusted/on-request 下 unknown/destructive MCP tool 进入 ask；
- approved pending MCP call resume 仍重新跑 capability gate。

---

### M6. Resources / prompts 以显式能力进入

不要一开始把 resources/prompts 混入普通 tools。

建议：

- `mcp_list_resources`;
- `mcp_read_resource`;
- `mcp_list_prompts`;
- `mcp_get_prompt`;
- 或先只在 `pulsara mcp doctor` / 后续 `pulsara mcp inspect` 暴露。

验收：

- 大 resource 内容走 artifact；
- direct inspect / doctor 如果遇到 `InputRequiredResult`，返回 fail-closed diagnostic，不进入 pending interaction，也不启动临时 CLI 问答；
- 只有显式 wrapper tool 路径支持 input-required suspend/resume；
- prompt 获取不自动改写系统提示词；
- resource/prompt 使用写入事件与 inspector。

---

### M7. 真实 MCP dogfood

V1 首个无 key dogfood 目标固定为 LangChain Docs MCP：

- endpoint: `https://docs.langchain.com/mcp`；
- 覆盖 streamable HTTP、remote public MCP、tool discovery、optional resource/prompt degrade、tool call；
- 通过 `pulsara mcp doctor` 验证 server snapshot；
- 通过 opt-in 测试 `PULSARA_RUN_REAL_MCP=1` 调用 `search_docs_by_lang_chain`。

至少覆盖：

- filesystem MCP；
- chrome-devtools MCP；
- firecrawl 或其他 remote MCP；
- auth-required MCP；
- large-result MCP；
- slow/timeout MCP；
- elicitation MCP；
- resources/prompts MCP。

目标不是“测试通过一次”，而是能连续使用一个下午不漏水。

---

## 6. 不应该照搬 Tanzo 的地方

### 6.1 不要把 MCP 只当 ToolSet metadata

Tanzo 的 `metadata.tanzo.kind` 轻便，但 Pulsara 已经有更强的 descriptor/exposure/gate 架构。MCP 生产化不能绕过 `CapabilityDescriptor`。

### 6.2 不要让 live manager 成为历史解释真源

Inspector 必须基于 event log 和 stored metadata 解释历史，不能依赖当前 MCP server 是否还活着。

### 6.3 不要让 plugin/builtin MCP server 静默进入模型

plugin/builtin server 必须进入可见配置、diagnostics 和 exposure event。安装插件不等于所有 MCP tool 永久无提示可用。

### 6.4 不要把 MCP sampling 过早打开

MCP sampling 即 server 请求 host 调模型，风险面很大。v1 应默认禁用或显式 policy 开启。

---

## 7. 最小生产可用定义

Pulsara MCP 可以称为生产可用，至少要满足：

- 用户能通过配置文件或 CLI 管理 MCP server；
- stdio/http MCP server 能通过官方 Python MCP SDK v2 真实启动、连接、列工具、调用工具；
- server 状态可查看、可重连、可禁用；
- descriptor 与 execution binding 同源，不漂移；
- MCP tool 完整经过 capability gate 与 permission gate；
- secrets 不进入 prompt/event/inspector；
- tool result 大输出走 artifact；
- modern `InputRequiredResult` 能 suspend/resume/cancel，legacy `elicitation/respond` 不作为生产主路径；
- list/read/prompt pagination 有 max pages/items、repeated cursor guard 与 cache refresh 策略；
- session close 能有界关闭 MCP server；
- inspect 能解释历史 MCP exposure/gate/tool call；
- 有真实 MCP dogfood 覆盖。

---

## 8. 一句话结论

Tanzo 最值得 Pulsara 借鉴的是 MCP 的产品化外壳：配置、store、lifecycle sync、SDK-backed client、reconnect、resources/prompts、elicitation UI、plugin/builtin server 合并。

Pulsara 不应照搬 Tanzo 的轻量 ToolSet 模型，而应把这些产品化能力接进自己的更强 runtime 骨架：unified capability surface、typed event ledger、permission gate、artifact store、context compiler 与 inspect。

如果下一轮按这个方向推进，Pulsara 的 MCP 会从“架构正确的 spike”升级成“可长期运行、可审计、可恢复的本地 MCP runtime”。
