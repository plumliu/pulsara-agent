# Pulsara MCP 启动延迟记录

> 状态：历史问题与重构前实测记录；M0–M4 hard cut 已落地，实施契约见
> `PULSARA_MCP_STARTUP_LATENCY_HARD_CUT_IMPLEMENTATION.zh.md`。
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

## 3. 重构前代码根因

本节描述发现问题时的旧实现，不再代表当前production path。

### 3.1 REPL 横幅位于完整 session open 之后

CLI 先等待 `_open_initial_repl_session(...)` 完成，之后才打印 REPL 横幅。因此 session open 中的任何阻塞都会表现为“命令启动后长时间没有界面”。

### 3.2 Session open 同步等待 MCP ready/discovery

`HostCore._open_session_with_runtime_id()` 曾同步调用 `_build_mcp_supervisor()`；后者同步等待已删除的
`McpServerSupervisor.sync_servers()`。即使 MCP server 配置为 `required=false`，它也曾处于 session open 的
blocking critical path。

这里的 `required=false` 仅表示连接或发现失败时可以降级，并不表示后台启动或不阻塞 REPL。

### 3.3 MCP discovery 串行执行多个远端请求

旧 SDK manager 曾依次执行：

1. `tools/list`
2. `resources/list`
3. `resources/templates/list`
4. `prompts/list`

这些远端调用串行累计延迟。当前实现还会尝试调用 server 未必支持的可选方法；`docs-langchain` 的 `prompts/list` 会产生 diagnostic。

### 3.4 `startup_timeout_ms` 不约束完整 discovery

已删除的`startup_timeout_ms`只约束过 MCP SDK client 的连接/初始化阶段，不覆盖后续 capability discovery。

### 3.5 每个 turn 的 safe point 可能再次刷新

旧`HostSession`在新 turn、approval resume、plan resume 等 safe point 会重新执行 MCP sync。该同步路径现已删除。

## 4. 当时的临时处理

目前将：

```yaml
docs-langchain:
  enabled: false
```

作为本地临时规避。需要 LangChain 文档 MCP 时可以再显式启用。

这个配置变化不代表放弃 MCP，也不改变 MCP 的长期产品契约；它只是避免一个非必要远端 server 阻塞日常开发与 dogfood。

## 5. 已落地结果

M0–M4 已完成以下迁移：

1. optional MCP 由 session-owned supervisor 后台连接与发现，不阻塞 REPL banner；
2. required MCP 按每 server absolute deadline bounded await；
3. discovery 只调用 server 声明的 capability，并在同一deadline内并发执行独立方法；
4. worker 只提交 candidate，HostSession safe point 同步原子安装 descriptor/binding surface；
5. config epoch、per-server attempt/generation 阻止 stale worker/candidate 回写；
6. per-call/pending lease、retiring slot与child binding index冻结执行生命周期；
7. RunStart/installation audit、resume audit与Inspector cross-session join成为durable事实；
8. close/shutdown bounded cancel/drain，失败保留同一HostSession/supervisor ownership供重试；
9. `sync_servers()`、`startup_timeout_ms`、manager+bundle双真源和旧elicitation continuation已删除。

以下原建议保留为这次迁移的设计来源，而不是待办：

1. **非 required MCP 后台启动**：基础 HostSession ready 后即可显示 REPL；MCP 状态先显示 `connecting`，完成后原子更新 capability/tool binding。
2. **能力感知 discovery**：依据 server capabilities 只请求实际声明支持的 tools/resources/prompts，避免用失败请求探测可选能力。
3. **Snapshot TTL/版本缓存**：配置 fingerprint 未变化且 snapshot 未过期时，不在每个 turn 重新发现完整能力。
4. **可选 discovery 并发化**：在协议和 SDK 允许时，并发获取 resources、resource templates 与 prompts，避免串行累加网络延迟。
5. **区分连接与发现预算**：分别定义 connect timeout、required tool discovery timeout、optional discovery timeout 和整体 startup deadline。
6. **可观测性**：记录每个 server 的 connect/discovery 分阶段耗时、refresh 原因、cache hit、timeout 和 degraded diagnostic。

## 6. 已执行验收口径

- 无 MCP 时，REPL 横幅应快速出现。
- 只有 `required=false` 的慢 MCP 时，REPL 横幅不应等待远端 discovery 完成。
- `required=true` 的 MCP 是否阻塞启动必须有明确且可测试的产品语义。
- 配置和 generation 未变化时，新 turn 不应无条件执行完整 discovery。
- 不支持 prompts/resources 的 server 不应通过失败请求产生稳定启动延迟。
- MCP 后台 ready 后，模型 exposure 与 ToolRegistry binding 必须在同一个 safe point 原子更新。
- MCP capability 被撤销时，仍需保持现有 fail-closed 与 subagent safety narrowing 语义。

最终 post-hard-cut 实测补充：

- optional 公共 LangChain MCP 未完成 discovery 时，真实 REPL 已先显示
  `latency-docs=starting (no tools)`；
- 下一 turn safe point 后 `:status` 显示 `ready`、`tool_count=3`；
- 公共 server 真实 discovery + tool call dogfood 通过；
- 用户当前 `docs-langchain.enabled=false` 配置下，真实 REPL 启动并关闭约 `2.59s`；
- 单独 real-MCP suite 为 `2 passed in 24.37s`，全量 suite 为
  `1759 passed, 69 skipped in 125.50s`；
- model-visible lifecycle contract复测通过：第一run准确回答`starting/0 tools`且不误报配置失败，第二run
  safe point后准确回答`ready/3 tools`。

## 7. 范围声明

本记录只保存已确认的性能事实与设计来源，不作为实施规格。MCP runtime、session lifecycle、
background ownership、config epoch与safe-point installation的唯一实施契约已经迁移到
`PULSARA_MCP_STARTUP_LATENCY_HARD_CUT_IMPLEMENTATION.zh.md`。
