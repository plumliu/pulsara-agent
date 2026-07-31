# Pulsara MCP Catalog Announcement 与 List Fallback 设计

> 状态：概念设计，尚未冻结为实施规格
> 日期：2026-07-31
> 范围：MCP server 可见性、连接状态提示，以及长对话中的 catalog 记忆恢复
> 不包含：通用 `search_tool/use_tool`、LLM 自动总结 MCP schema、MCP lifecycle 的实现重构

## 1. 背景

Pulsara 的 MCP server 可能在 HostSession 打开时仍处于连接和发现阶段，也可能在会话中被启用、禁用或重新配置。模型因此会遇到三类问题：

1. 首次 provider call 时，某个 MCP 仍在连接，工具还不能进入 capability exposure；
2. MCP 在会话中途变为 ready，不能为了更新一段 system prompt 就任意改写已经稳定的 provider prefix；
3. 即使模型曾经看过 server announcement，长对话中的 lost-in-the-middle 仍可能令它忘记有哪些 MCP 已连接。

这里不采用两个通用的工具搜索/执行 meta-tool。它们会让模型先猜“应该搜索什么”，再经过动态 schema 选择，增加 action space、归因和授权复杂度。MCP 工具仍按 Pulsara 既有 capability contract 暴露；本设计只增加一个很小的 catalog 记忆恢复入口。

## 2. 核心设计

设计由三部分组成：

```text
authoritative MCP lifecycle snapshot
        |
        +--> initial bounded server announcement
        |
        +--> safe-point state-change announcement
        |
        `--> stable list_mcp_servers fallback
```

### 2.1 Initial server announcement

在首次 provider-visible input 冻结前，Pulsara 根据自己的 MCP config、installation snapshot 和 discovery result，确定性生成一个 bounded announcement，例如：

```text
MCP servers:
- docs-langchain — connecting
- linear — connected (18 tools)
- internal-search — failed (retryable)
```

announcement 只表达 Pulsara 已确认的事实：

- 稳定的 server display name / server ID；
- `connecting | connected | failed | disabled` 等 bounded 状态；
- connected server 的工具数量；
- server 提供且经过 Pulsara 清洗、裁剪的 instructions 摘要；
- 必要时给出稳定的失败类别，但不泄漏 secret 或原始 transport exception。

它不是由模型生成的。Pulsara 不应在连接 MCP 时额外调用模型总结 server 或 tool schema，因为这会增加延迟、成本和不可复现的第二语义真源。server instructions 与 tool descriptions 仍由 MCP server 提供，Pulsara 只做确定性的验证、清洗和 bounded rendering。

### 2.2 Safe-point state-change announcement

如果 MCP 在首次 user input 前完成连接，可以在第一次 provider input 冻结时直接使用 ready snapshot，不产生额外中途消息。

如果状态在会话进行中变化，则不回写旧 system prompt，也不在任意时刻打断 model/tool operation。Host 只在合法 safe point 处理状态变化，例如：

- `PRE_RUN`；
- `PRE_RESUME`；
- 当前 turn 已结束、下一次 model step 尚未开始的边界。

状态变化可形成一次 bounded runtime-owned announcement：

```text
MCP update: docs-langchain is now connected and exposes 3 tools.
```

同一 generation 的重复 completion 不重复提示；旧 generation 的迟到 completion 不得重新进入模型上下文。announcement 是 capability 状态提示，不授予额外权限，也不能替代本次 run 的 capability exposure resolution。

### 2.3 `list_mcp_servers` fallback

Pulsara 保留一个名称明确、schema 稳定的小型 meta-tool：

```text
list_mcp_servers()
```

它用于在长对话中重新枚举当前 MCP catalog，而不是执行 MCP 工具，也不是自由文本搜索器。返回内容应包含：

- 当前已知的 MCP servers；
- 每个 server 的当前 authoritative 状态；
- connected server 的工具数量；
- bounded instructions / purpose；
- 必要时返回 bounded tool-name 概览或“该 server 的工具已直接暴露”提示；
- snapshot/generation attribution，供 runtime 验证结果没有跨代漂移。

模型不需要在每次使用 MCP 前强制调用它。正常情况下，直接可见的 MCP tool schema 仍是最强的行为先验；`list_mcp_servers` 只是以下场景的恢复手段：

- 用户问“现在有哪些 MCP？”；
- 模型记得某类能力但忘记来自哪个 server；
- 长程 compaction 后需要重新确认 catalog；
- server 曾处于 connecting，模型需要确认它后来是否 ready；
- capability gate 告知目标工具当前不可用，模型需要重新枚举可用入口。

因此 system instruction 应写成“必要时重新枚举”，而不是“调用任何 MCP 前必须 list”。后者会给所有 MCP 调用增加无意义 tool round。

## 3. 与直接工具暴露的关系

V1 不改变现有事实：

- MCP tool descriptor 仍经 capability surface 进入 provider-visible tool schema；
- permission/capability gate 仍在真实执行前生效；
- `list_mcp_servers` 返回 server catalog，不返回可直接执行的 opaque handle；
- list 结果不能令一个未暴露、未授权或已撤销的工具变得可调用；
- server status 与本次 run 的 frozen capability exposure 不同，前者 ready 不代表后者一定授权。

如果未来单个 MCP 的 schema 数量大到无法全部常驻 provider payload，可以另行设计“bounded catalog retrieval”。那将涉及 tool schema materialization、reranker/Flash 选择、cache identity 和 execution rebind，不应偷偷塞进本设计的 `list` fallback。

## 4. Prefix cache 与长上下文

本设计的目标不是让动态状态永远写进 system prefix，而是区分：

```text
首次输入前已知的稳定事实
    -> 可进入 initial prefix

会话中途变化的事实
    -> safe-point announcement

后来被上下文稀释或压缩的事实
    -> list_mcp_servers 按当前 snapshot 重取
```

这样无需因为每次 MCP retry/refresh 都重建 stable prefix，也不要求模型永久记住一条位于上下文中部的状态提示。

## 5. 失败与安全边界

- MCP catalog 来自 Pulsara authority，不信任模型回忆；
- server/tool instructions 必须执行既有 sanitizer 与长度上限；
- connecting/failed server 不伪造工具可用性；
- stale generation completion 不得出现在 list 或 announcement；
- list failure只表示 catalog observation 失败，不能自动降级为“没有 MCP”；
- list 是只读观察能力，不产生连接、重试、启用或权限变更副作用；
- Host close 后不得再注入迟到 announcement。

## 6. 暂不纳入 V1

- 通用 `search_tool` / `use_tool`；
- 用 LLM 为 MCP server 或 schema 生成 authority summary；
- 根据自然语言问题自动挂载、卸载工具；
- 将 catalog list 作为 permission authority；
- 为了 catalog 更新任意 rebase 已冻结的 run；
- 跨会话恢复旧的 process-local MCP manager 对象。

## 7. 对 Pulsara 的价值

这是一个小而明确的产品能力：

1. initial announcement 告诉模型“已配置和正在连接什么”；
2. safe-point announcement 告诉模型“中途发生了什么变化”；
3. `list_mcp_servers` 解决长上下文遗忘；
4. 真实 tool schema 与 capability gate 继续负责“如何调用”和“是否允许”。

它避免了一个过度通用的动态工具搜索系统，同时保留 prompt-cache 稳定性和 MCP late-ready 的可用性。
