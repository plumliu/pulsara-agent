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

---

## 2. Session-owned manager

MCP client / server process / remote connection 由 session-owned `McpClientManager` 持有。

Manager 职责：

- 暴露当前 `snapshots`；
- 执行 `call_tool(server_id, tool_name, arguments, timeout_ms=...)`；
- `aclose()` 幂等关闭；
- `cancel_active()` best-effort 取消 active calls；
- `respond_elicitation(...)` 路由用户输入回 MCP server。

MCP adapter 不拥有 client，不启动 server，不负责 lifecycle teardown。

Host/session close 必须最终关闭 session-owned MCP manager；close 必须幂等、有界，不得让单个 MCP call 永久阻塞 session teardown。

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

## 7. Elicitation / tool suspension

MCP elicitation 通过通用 tool suspension seam 表达：

```text
AsyncTool.execute_async()
  -> ToolExecutionSuspended(interaction_kind="mcp_elicitation", payload=...)
  -> AgentRuntime state.pending_interaction_kind = "mcp_elicitation"
  -> HostSession.pending_interaction = PendingMcpElicitation
  -> user resolution
  -> AgentRuntime.resume_after_mcp_elicitation()
  -> adapter.resume_elicitation()
```

规则：

- MCP elicitation 不得被格式化成普通 tool error。
- pending payload 必须包含 interaction id、tool call id、tool name、server id、request id、prompt、schema。
- resume 时必须把用户 answer 路由回同一个 manager/server/request id。
- pending elicitation 状态下不得自动 context compact；resolution 后进入 follow-up safe point 时才可 mid-turn compact。

---

## 8. Permission / capability gate

MCP tool 与内置 tool 共享同一套 gate：

1. capability exposure access；
2. permission policy；
3. ToolExecutor execution。

MCP 不拥有独立 approval 系统。MCP server 的 annotation / default approval mode 只能影响 descriptor hint 或 diagnostics，不得绕过 Pulsara 的 `PolicyPermissionGate`。

---

## 9. Observability

MCP 必须能通过现有 capability 与 tool events 被解释：

- `capability_exposure_resolved` 显示 MCP descriptor、hidden/deferred/callable 状态与 diagnostics；
- `capability_gate_decision` 显示 MCP tool 的最终 gate decision；
- tool call/result events 显示 model tool name；
- result metadata 保留 server id 与 original tool name；
- elicitation suspension 写入 `tool_execution_suspended`。

Inspector 不应通过查询 live MCP manager 来解释历史；历史解释必须来自 event log 与 stored metadata。

---

## 10. 禁止事项

- 不允许 MCP provider 与 adapter 分开从不同 snapshot 构造。
- 不允许非 READY server 的 tools 进入 direct tools。
- 不允许名称冲突时自动随机改名继续执行。
- 不允许 adapter 拥有 MCP client lifecycle。
- 不允许 MCP approval/elicitation 绕过 Pulsara pending interaction seam。
- 不允许 MCP server annotation 直接覆盖 permission policy。

---

## 11. 测试守护

最低测试门槛：

- READY snapshot 产出 descriptor + AsyncTool binding。
- FAILED / NEEDS_AUTH / DISABLED snapshot 产 diagnostic，不产 callable tool。
- name mangling 稳定，碰撞 fail-closed。
- descriptor/binding 缺任一侧产生 diagnostic。
- MCP tool 调用经 capability gate 与 permission gate 后执行。
- approval resume / crafted pending call 在 descriptor 缺失时 fail-closed。
- elicitation 可 suspend，HostSession 捕获 pending，resolution 路由回 manager。
- manager close 幂等并取消 active calls。
- inspector 能看到 exposure/gate/tool metadata。
