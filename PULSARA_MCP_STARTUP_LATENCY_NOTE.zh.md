# Pulsara MCP 启动延迟记录

> 状态：已确认，暂缓实施，不阻塞当前 code review 与 hard-cut 修复。
>
> 记录日期：2026-07-11

## 1. 现象

运行：

```bash
uv run pulsara host repl --env-file .env --workspace ~/Desktop/little_snake
```

在打印以下 REPL 横幅前会等待较长时间：

```text
Pulsara REPL · :help 查看命令 · Ctrl-D detach · :close 关闭对话
```

当全局 MCP 配置中的 `docs-langchain.enabled` 从 `true` 改为 `false` 后，REPL 启动明显变快，验证了主要延迟来自 MCP 启动与能力发现路径，而不是 `uv run`、PostgreSQL、Oxigraph、retrieval 或 runtime wiring。

当前配置来源：

```text
~/.pulsara/mcp.yaml
```

## 2. 实测结果

一次完整 session open 的分阶段耗时：

| 阶段 | 耗时 |
|---|---:|
| `HostCore.open_session()` 总计 | 约 35.21 秒 |
| MCP `sync_servers()` | 约 35.08 秒 |
| settings/env 加载 | 约 0.001 秒 |
| terminal supervisor attach | 接近 0 秒 |
| retrieval resources 初始化 | 约 0.038 秒 |
| agent runtime wiring | 约 0.078 秒 |

对 `docs-langchain` 的一次独立能力发现探针显示：

| MCP 方法 | 耗时 | 结果 |
|---|---:|---|
| `tools/list` | 约 11.39 秒 | 成功，返回 3 个工具 |
| `resources/list` | 约 5.74 秒 | 成功 |
| `resources/templates/list` | 约 5.89 秒 | 成功 |
| `prompts/list` | 约 5.79 秒 | `MCPError`，形成 1 条 diagnostic |

具体耗时受网络状况影响，但 MCP 在 session open 中占据绝大多数 wall-clock latency 的结论稳定成立。

## 3. 当前代码根因

### 3.1 REPL 横幅位于完整 session open 之后

CLI 先等待 `_open_initial_repl_session(...)` 完成，之后才打印 REPL 横幅。因此 session open 中的任何阻塞都会表现为“命令启动后长时间没有界面”。

### 3.2 Session open 同步等待 MCP ready/discovery

`HostCore._open_session_with_runtime_id()` 会同步调用 `_build_mcp_supervisor()`；后者同步等待 `McpServerSupervisor.sync_servers()`。即使 MCP server 配置为 `required=false`，它当前仍处于 session open 的 blocking critical path。

这里的 `required=false` 仅表示连接或发现失败时可以降级，并不表示后台启动或不阻塞 REPL。

### 3.3 MCP discovery 串行执行多个远端请求

SDK manager 当前依次执行：

1. `tools/list`
2. `resources/list`
3. `resources/templates/list`
4. `prompts/list`

这些远端调用串行累计延迟。当前实现还会尝试调用 server 未必支持的可选方法；`docs-langchain` 的 `prompts/list` 会产生 diagnostic。

### 3.4 `startup_timeout_ms` 不约束完整 discovery

`startup_timeout_ms` 只约束 MCP SDK client 的连接/初始化阶段，不覆盖后续 capability discovery。后续方法使用各自的读取超时，因此完整启动时间可能明显超过 `startup_timeout_ms`。

### 3.5 每个 turn 的 safe point 可能再次刷新

`HostSession` 在新 turn、approval resume、plan resume 等 safe point 会重新执行 MCP sync。对于已经 ready 的 manager，当前 supervisor 仍会 refresh snapshot，可能再次执行完整能力发现。因此该问题不只影响 REPL 初次启动，也可能增加每个 turn 的首包延迟。

## 4. 当前临时处理

目前将：

```yaml
docs-langchain:
  enabled: false
```

作为本地临时规避。需要 LangChain 文档 MCP 时可以再显式启用。

这个配置变化不代表放弃 MCP，也不改变 MCP 的长期产品契约；它只是避免一个非必要远端 server 阻塞日常开发与 dogfood。

## 5. 后续建议

建议后续单独安排 MCP startup/refresh latency PR，不与当前 ResolvedModelCall hard-cut 混做。

优先级建议如下：

1. **非 required MCP 后台启动**：基础 HostSession ready 后即可显示 REPL；MCP 状态先显示 `connecting`，完成后原子更新 capability/tool binding。
2. **能力感知 discovery**：依据 server capabilities 只请求实际声明支持的 tools/resources/prompts，避免用失败请求探测可选能力。
3. **Snapshot TTL/版本缓存**：配置 fingerprint 未变化且 snapshot 未过期时，不在每个 turn 重新发现完整能力。
4. **可选 discovery 并发化**：在协议和 SDK 允许时，并发获取 resources、resource templates 与 prompts，避免串行累加网络延迟。
5. **区分连接与发现预算**：分别定义 connect timeout、required tool discovery timeout、optional discovery timeout 和整体 startup deadline。
6. **可观测性**：记录每个 server 的 connect/discovery 分阶段耗时、refresh 原因、cache hit、timeout 和 degraded diagnostic。

## 6. 后续验收建议

- 无 MCP 时，REPL 横幅应快速出现。
- 只有 `required=false` 的慢 MCP 时，REPL 横幅不应等待远端 discovery 完成。
- `required=true` 的 MCP 是否阻塞启动必须有明确且可测试的产品语义。
- 配置和 generation 未变化时，新 turn 不应无条件执行完整 discovery。
- 不支持 prompts/resources 的 server 不应通过失败请求产生稳定启动延迟。
- MCP 后台 ready 后，模型 exposure 与 ToolRegistry binding 必须在同一个 safe point 原子更新。
- MCP capability 被撤销时，仍需保持现有 fail-closed 与 subagent safety narrowing 语义。

## 7. 范围声明

本记录只保存已确认的性能事实与后续设计方向。当前不修改 MCP runtime，不改变 session lifecycle，也不阻塞正在进行的 ResolvedModelCall hard-cut review、修复与验证。
