# MCP Capability Contract

_Created: 2026-07-04_

本文档定义 Pulsara MCP 接入的长期契约。MCP 是 unified capability surface 的 provider + execution binding 扩展，不是独立的第二套工具系统。

相关代码：

- [src/pulsara_agent/runtime/mcp/config.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/mcp/config.py)
- [src/pulsara_agent/runtime/mcp/manager.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/mcp/manager.py)
- [src/pulsara_agent/runtime/mcp/types.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/mcp/types.py)
- [src/pulsara_agent/capability/providers/mcp.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/providers/mcp.py)
- [src/pulsara_agent/tools/adapters/mcp.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/adapters/mcp.py)
- [tests/test_capability_mcp.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_capability_mcp.py)

---

## 1. 核心立场

MCP 接入由同一份 session-scoped server snapshot 同时产出：

- `CapabilityDescriptor`；
- `AsyncTool` execution binding；
- diagnostics；
- manager reference。

这个原子组合称为 `McpCapabilityBindingBundle`。

不得出现以下状态：

- descriptor 来自一份 snapshot，adapter 来自另一份 snapshot；
- provider 只 advertise descriptor，没有 ToolRegistry binding；
- adapter 能执行，但 capability surface 看不到 descriptor。

生产协议层固定面向官方 Python MCP SDK v2 线构建。稳定版发布前，仓库依赖真源是 `mcp[cli]==2.0.0b1`。`mcp-types==2.0.0b1` 只作为 lockstep guard；升级 PR 必须从 `mcp` package metadata 校验两者完全一致，不得独立推进 `mcp-types`。所有 SDK beta API 必须被 Pulsara-owned manager/facade 包住，不得渗透到 capability、permission、artifact、context compiler 或 host/session 调用点。

当前手搓 stdio/http JSON-RPC manager 只允许作为 legacy spike、deterministic fixture 或迁移辅助，不得作为新生产能力的默认路径继续扩展。不维护 MCP SDK v1 生产兼容分支。

---

## 2. Session-owned manager

MCP client / server process / remote connection 由 session-owned `McpClientManager` 持有。

Manager 职责：

- 暴露当前 `snapshots`；
- 执行 `call_tool(server_id, tool_name, arguments, timeout_ms=...)`；
- 执行 `read_resource(server_id, uri, ...)` / `get_prompt(server_id, name, arguments, ...)` 的显式 wrapper path（若该能力在产品面开启）；
- `resume_suspended_request(...)` / `resume_input_required(...)`：用 Pulsara-owned resolution DTO 恢复 SDK v2 `InputRequiredResult` 挂起的原始请求；
- `aclose()` 幂等关闭；
- `cancel_active()` best-effort 取消 active calls；

MCP adapter 不拥有 client，不启动 server，不负责 lifecycle teardown。

Host/session close 必须最终关闭 session-owned MCP manager；close 必须幂等、有界，不得让单个 MCP call 永久阻塞 session teardown。

SDK-backed manager 必须把 SDK session / transport / subprocess / remote connection 全部收口在 manager 内部，并向上只暴露 Pulsara-owned DTO 与 protocol。SDK 初始化、capability discovery、tool/resource/prompt 调用、timeout、cancellation、connection error 都必须转换为 Pulsara 的 snapshot status、diagnostics、tool result 或 `ToolExecutionSuspended`。

生产 manager 采用一 server 一 SDK `Client`，由 Pulsara supervisor 聚合多 server snapshot。SDK `ClientSessionGroup` 不得成为命名、权限、descriptor 或 exposure 的真源；若未来使用，只能封装在 facade 内作为实现细节。

`respond_elicitation(...)` 只允许存在于 legacy 测试 fixture / spike adapter；不得出现在生产-facing `McpClientManager` protocol、session supervisor 或 SDK-backed manager 的公共职责中。生产恢复语义是“携带 input_responses 与 request_state 重试同一个原始请求”，而不是“按 request_id 向 server 回包”。

SDK-backed manager 对外不得暴露 SDK `InputRequest`、`InputResponses`、`InputRequiredResult` 或 SDK request-state wrapper。所有这类对象必须在 manager/facade 内部完成转换；跨 HostSession、CLI、event log、inspector 边界流动的只能是 Pulsara-owned DTO。

---

## 3. Bundle builder

`build_mcp_bundle(manager)` 是 V1 MCP descriptor/binding 的 composition seam。

规则：

- 只处理 READY snapshot 的 tools。
- 非 READY snapshot 产生 diagnostics，不产 callable tools。
- 每个 discovered tool 必须通过 `mangle_mcp_tool_name(server_id, tool_name)` 转成模型工具名。
- 同名冲突必须 fail-closed，并产生 `mcp_tool_name_collision` diagnostic。
- descriptor names 与 tool binding names 必须一一对应；缺任一侧都产生 diagnostic。
- bundle generation 是所有 snapshot generation 的最大值。

Bundle 必须在 `AgentRuntime` 构造前或构造期安装到 `RuntimeSession.extra_tool_bindings` 与 `CapabilityRuntime.providers`，避免 snapshot/binding 不同源。

---

## 4. Tool name mangling

MCP model tool name 必须稳定、可逆解释、符合模型工具名限制。

规则：

- 名称必须包含 server id 与原始 tool name 的稳定信息。
- 非法字符必须替换为安全字符。
- 超长名称必须使用稳定 hash suffix，而不是简单截断到可能冲突。
- 发生碰撞时不得随机重命名；必须 fail-closed 并产 diagnostic。
- diagnostics / metadata 中保留原始 `server_id` 与 `original_tool_name`，供 inspector 解释。

---

## 5. Descriptor semantics

MCP descriptor 的 provider kind 必须是 `mcp`。

MCP tool annotations 映射：

- `readOnlyHint=True` -> `is_read_only=True`。
- 缺失 `destructiveHint` 时按 destructive 处理。
- 缺失 `openWorldHint` 时按 open-world 处理。
- `supports_parallel_tool_calls` 决定 `is_concurrency_safe`。
- server default approval mode 只能作为 hint；最终 ALLOW/WAIT/DENY 由 Pulsara permission gate 决定。

V1 MCP descriptor advertise policy 是 DIRECT；未来 deferred MCP discovery 必须先扩展 unified capability surface，而不能绕过现有 exposure/gate。

---

## 6. Execution adapter

`McpCapabilityTool` 是 thin adapter。

职责：

- 按 model tool name 接收 ToolCall；
- 将调用委派给 session-owned manager 的 `call_tool()`；
- 按 manager 返回值格式化 `ToolExecutionResult`；
- 在 metadata 中写入 provider kind、server id、original tool name。

禁止：

- adapter 私自持有独立 client；
- adapter 自行启动/关闭 MCP server；
- adapter 绕过 ToolExecutor / RuntimeSession event recorder；
- adapter 绕过 capability gate 或 permission gate。

---

## 7. InputRequired suspension

SDK v2 modern 交互式输入以 `InputRequiredResult` 表达。生产路径必须手动接管 input-required loop，不得直接调用 SDK 高层 `Client.call_tool()` 并让 SDK 自动消费 input-required callback。

生产 manager 对交互式方法必须使用底层 session API：

- `client.session.call_tool(..., allow_input_required=True)`；
- `client.session.read_resource(..., allow_input_required=True)`；
- `client.session.get_prompt(..., allow_input_required=True)`。

收到 terminal result 时正常返回；收到 `InputRequiredResult` 时，通过通用 tool suspension seam 表达：

```text
AsyncTool.execute_async()
  -> manager calls SDK session method with allow_input_required=True
  -> ToolExecutionSuspended(interaction_kind="mcp_input_required", payload=...)
  -> AgentRuntime state.pending_interaction_kind = "mcp_input_required"
  -> HostSession.pending_interaction = PendingMcpInputRequired
  -> user resolution as Pulsara-owned DTO
  -> AgentRuntime.resume_after_mcp_input_required()
  -> adapter resumes through normal capability/gate-aware tool continuation
  -> manager.resume_suspended_request(...)
```

规则：

- MCP input-required 不得被格式化成普通 tool error。
- pending payload 必须包含 interaction id、wrapper tool call id、wrapper tool name、server id、source_method、protocol version、request_state、input_requests、round count、deadline。
- `input_requests` 必须是 Pulsara-owned、可持久化、可 inspect 的 DTO；SDK-backed manager/facade 必须把 SDK `InputRequest` 转成该 DTO 后再进入 pending payload。该 DTO 至少要保留问题文本、字段 schema、默认值、必填性、枚举/选项、validation hints 与 display hints；不得把 SDK 对象、SDK pydantic model 或 SDK-specific enum 直接塞进 pending payload / event log。HostSession / CLI / UI 只读取 Pulsara DTO，不接触 SDK 类型。resume 时再由 manager/facade 映射回 SDK 类型。
- `request_state` 字段必须保留，但值可为 `None` / `null`。resume 时 manager/facade 必须原样传回或按 SDK API 省略该参数；不得合成、推断、序列化压缩或用 round id 替代新的 request_state。
- pending payload 必须包含完整 Pulsara-owned original request DTO：
  - `source_method="tools/call"`：original tool name 与 original arguments；
  - `source_method="resources/read"`：resource URI；
  - `source_method="prompts/get"`：prompt name 与 prompt arguments。
- v1 只有 wrapper tool / runtime tool-call 路径支持 input-required suspend。direct CLI / inspect / doctor 路径如果遇到 resource/prompt input-required，必须 fail-closed 并产 diagnostic，不创建 pending interaction，也不尝试在 CLI 内临时提问。
- 如果 v1 通过显式 wrapper tool 暴露 resource/prompt input-required，payload 必须同时保留 wrapper tool 身份与 underlying MCP request 身份；两者不得混用。
- HostSession 只产 Pulsara-owned、可持久化的 resolution DTO，不构造或持有 SDK `InputResponses`。
- SDK `InputResponses` 只能由 SDK-backed manager/facade 在内部构造，并用同一个 request_state 与同一个原始 request 重试。
- 再次收到 `InputRequiredResult` 时继续 suspend；超过 round cap、用户 cancel、session close 或 request_state 失效时返回结构化 cancel/error。
- pending input-required 状态下不得自动 context compact；resolution 后进入 follow-up safe point 时才可 mid-turn compact。
- legacy `elicitation/respond` 只允许作为 hand-rolled manager spike / fixture，不是 SDK v2 生产主路径。

---

## 8. Tool result mapping

SDK v2 `CallToolResult` 必须完整映射，不得只 join text：

- `TextContent` 进入 model-facing output/preview；
- `ImageContent`、`AudioContent`、blob-like resource 进入 artifact，model-facing output 只放摘要与 artifact ref；
- `EmbeddedResource` 按 resource 内容类型归档或预览；
- `ResourceLink` 保留 uri/name/mime/description metadata，必要时提示可用 MCP resource read；
- `structured_content` 保存为 JSON artifact 或 metadata；小型 JSON 可进入 preview，大型 JSON 必须走 artifact；
- `is_error=True` 是 tool-visible result，应作为模型可见 tool result 返回，不得伪装成协议异常；
- top-level `MCPError`、transport error、validation error 是执行/协议错误，进入 diagnostics、tool error event 与 manager status。

---

## 9. Pagination / cache / notifications

Snapshot sync 与 doctor 必须 drain SDK pagination：

- `list_tools`、`list_resources`、`list_resource_templates`、`list_prompts` 都必须支持 cursor loop；
- 每类 list 必须有 max pages / max items；
- repeated cursor 必须 fail-closed；
- snapshot sync / doctor 必须明确绕过 SDK cache：可使用高层 `Client` verbs 并传 `cache_mode="refresh"`，或使用 `ClientSession.list_*` / `Client(cache=False)` 等不读取 SDK response cache 的路径；
- 任何非 refresh cache 使用必须可 inspect：至少记录 server id、method、cache mode、hit/miss、cache key scope；
- 除非使用 Pulsara-owned/custom cache store 或 facade-level instrumentation 能可靠记录 hit/miss，否则生产路径必须 `cache=False` 或强制 `cache_mode="refresh"`。SDK 默认 cache 如果无法被 Pulsara instrumentation 观测，视为不可用，不得在生产路径静默开启。
- SDK cache 不得在 event/inspector 中静默生效；
- v1 不承诺 notification-driven live invalidation；server list/resource/prompt 变化只通过 manual refresh、reconnect、TTL 或下一轮 sync 生效。

---

## 10. HTTP transport security

Remote streamable HTTP 生产路径禁止直接使用 SDK `Client(url)` URL shorthand，因为 SDK 默认 HTTP client 会 follow redirects。

生产路径必须：

- 构造 Pulsara-owned `httpx.AsyncClient`；
- 显式设置 redirect policy，默认 fail-closed；
- 统一注入 headers/auth/timeout/proxy/event hooks；
- 对 token/header/url diagnostics 做 redaction；
- 将 client 传给 `streamable_http_client(url, http_client=...)`。

---

## 11. Permission / capability gate

MCP tool 与内置 tool 共享同一套 gate：

1. capability exposure access；
2. permission policy；
3. ToolExecutor execution。

MCP 不拥有独立 approval 系统。MCP server 的 annotation / default approval mode 只能影响 descriptor hint 或 diagnostics，不得绕过 Pulsara 的 `PolicyPermissionGate`。

MCP input-required resume 是恢复已挂起 MCP 请求的边界，不得静默绕过 gate。resume 前必须重新执行 capability exposure / permission recheck：

- `DENY`：写入 `CapabilityGateDecisionEvent(decision="deny")` 与 denied/error tool result；
- `WAIT_FOR_USER`：V1 不创建二次 approval pending，必须 fail-closed，写入 `CapabilityGateDecisionEvent(decision="deny", reason_code="mcp_resume_permission_approval_unsupported")` 与 denied tool result；
- `ALLOW`：才可以调用 adapter / manager resume。

---

## 12. Observability

MCP 必须能通过现有 capability 与 tool events 被解释：

- `capability_exposure_resolved` 显示 MCP descriptor、hidden/deferred/callable 状态与 diagnostics；
- `CapabilityGateDecisionEvent` 显示 MCP tool 的最终 gate decision；
- tool call/result events 显示 model tool name；
- result metadata 保留 server id 与 original tool name；
- input-required suspension 写入 `tool_execution_suspended`。

Inspector 不应通过查询 live MCP manager 来解释历史；历史解释必须来自 event log 与 stored metadata。

---

## 13. 禁止事项

- 不允许 MCP provider 与 adapter 分开从不同 snapshot 构造。
- 不允许非 READY server 的 tools 进入 direct tools。
- 不允许名称冲突时自动随机改名继续执行。
- 不允许 adapter 拥有 MCP client lifecycle。
- 不允许 MCP approval/elicitation 绕过 Pulsara pending interaction seam。
- 不允许 MCP server annotation 直接覆盖 permission policy。
- 不允许 SDK `ClientSessionGroup` 的合并命名结果成为模型工具名或权限真源。
- 不允许 remote MCP 生产路径使用 `Client(url)` shorthand。

---

## 14. 测试守护

最低测试门槛：

- `mcp[cli]==2.0.0b1` / `mcp-types==2.0.0b1` import smoke test 通过，且生产 wiring 使用 SDK-backed manager；
- READY snapshot 产出 descriptor + AsyncTool binding。
- FAILED / NEEDS_AUTH / DISABLED snapshot 产 diagnostic，不产 callable tool。
- name mangling 稳定，碰撞 fail-closed。
- descriptor/binding 缺任一侧产生 diagnostic。
- MCP tool 调用经 capability gate 与 permission gate 后执行。
- approval resume / crafted pending call 在 descriptor 缺失时 fail-closed。
- modern `InputRequiredResult` 可 suspend，HostSession 捕获 pending，resolution 以 Pulsara-owned DTO 路由回 manager；SDK `InputResponses` 只在 manager/facade 内部构造。
- pending `input_requests` 使用 Pulsara-owned DTO，不泄漏 SDK `InputRequest`；nullable `request_state` 字段被保留且不被合成。
- tools/call、resources/read、prompts/get 的 input-required payload 都包含 source_method 与完整 original request DTO。
- direct CLI / inspect / doctor resource/prompt 遇到 input-required 时 fail-closed + diagnostic，不创建 pending interaction。
- legacy `elicitation/respond` 不参与 production wiring。
- `is_error=True` result 是模型可见 tool result；protocol/transport error 是 tool error。
- pagination 有 max pages/items 与 repeated cursor guard；非 refresh cache hit/miss 可 inspect。
- remote HTTP 使用自建 `httpx.AsyncClient`，redirect 默认 fail-closed。
- manager close 幂等并取消 active calls。
- inspector 能看到 exposure/gate/tool metadata。
