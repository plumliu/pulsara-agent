# Pulsara 图数据库愿景：证据、经验与能力线索

这份文档记录 Pulsara 对图数据库的长期理解：图数据库首先承载运行证据和语义经验，而不是提前把所有系统资产都建成节点。

核心判断：

> runtime graph 记录发生过什么；memory graph 记录系统从这些经历中沉淀出了什么；capability 不作为图节点存在，只在确有行为事实的位置留下最小能力线索。

这个切法让图数据库的语义更清晰，也避免把系统能力资产和运行证据、语义经验混成同一类图节点。

## 1. 图数据库的两个主语

Pulsara 当前更适合把图数据库收束为两个已经有 runtime writer 的子图：

```text
runtime graph = 发生过什么
memory graph  = 从发生过的事里沉淀出了什么
```

Capability 仍然重要，但它属于 runtime/config 层：

```text
capability = 当前系统可用什么能力
```

它的 source of truth 仍然是文件系统、tool registry、MCP 配置、plugin 配置和 host runtime，而不是 graph database。

## 2. Runtime 子图：证据层

Runtime 子图记录更接近事实发生现场的事件：turn、tool result、run timeline、artifact、evidence、错误、用户指令、模型输出。

当前代码里已经定义的 runtime 类型是：

- `RunTimeline`
- `Turn`
- `ToolResult`
- `Artifact`
- `Evidence`
- `EventSpan`
- `EvalRun`
- `Judgment`

这些类型来自 `src/pulsara_agent/ontology/runtime.py` 和 `src/pulsara_agent/entities/runtime/`。

Runtime 子图的核心语义是“事情如何发生”。它不一定长期保留完整细节，但它是 memory 形成时最重要的证据来源。

## 3. Memory 子图：语义化经验

Memory 子图不是原始日志，而是运行时对话、工具结果、用户纠正、项目决策经过筛选、归纳、压缩后的语义沉淀。

当前代码里已经定义的 durable memory 类型是：

- `Claim`
- `Decision`
- `Preference`
- `ActionBoundary`
- `Observation`

这些类型来自 `src/pulsara_agent/ontology/memory.py` 和 `src/pulsara_agent/entities/memory/`。它们的 IRI 在 `https://pulsara.dev/memory#...` 命名空间下；JSON-LD context 里当前注册的是裸类型名，例如 `ActionBoundary`，不是必须写成 `mem:ActionBoundary`。

Memory 子图的核心语义是“系统学到了什么”，不是“系统拥有什么”。

典型事实包括：

- 用户偏好。
- 项目事实。
- 已经做出的架构决策。
- 对工具、模型、流程的观察。
- 哪些行为以后应该避免或优先采用。

## 4. Capability 不入图

代码里已经有 `Skill`、`Tool`、`Plugin`、`Policy` 等 capability ontology 类型，定义位于 `src/pulsara_agent/ontology/capability.py` 和 `src/pulsara_agent/entities/capability/`。

但这不等于 Pulsara 要构建 capability graph。当前没有 runtime writer 把 capability 实例写入图数据库；这些类型是 type-only ontology，不是已经有实例、有边、有消费路径的图子域。

更重要的是，capability 的天然分区和 runtime/memory 不一样：

- 内置 skill 是安装级或系统级资产。
- 项目 skill 是 workspace 级资产。
- 用户安装 skill 是 user 级资产。
- MCP server 可能来自用户配置、项目配置或临时连接。
- native tool 是 runtime 内置能力。
- plugin 可能有独立的来源、版本和信任边界。

如果把 capability 作为一等图节点，就会立刻遇到一个不漂亮的问题：memory graph 按 user/workspace/domain 分区，runtime graph 按 session/run/workspace 记录证据，而 capability 可能是 global、per-user、per-workspace 混合的。跨 graph_id 的边到底是不是边，会变成一个真正的架构问题。

因此原则是：

> Capability 不作为图节点存在；只有真实发生的工具调用，才通过 `ToolResult.tool_name` 这类运行事实字段留下最小能力线索。

## 5. 能力线索只来自真实工具调用

虽然 capability 不入图，但运行事实里已经有一个足够稳的能力线索：`ToolResult.tool_name`。

它表示某次工具调用真实发生过。这个字段不把 tool 建成图节点，也不要求跨图 NodeRef；它只是 runtime 证据上的字符串事实。

```text
ToolResult.tool_name = "terminal"
```

这个字段能表达：

- 某次工具调用使用了哪个 tool name。
- 某个 run 中出现过哪些工具调用。
- 某条 memory 的 evidence 链最终来自哪些 tool result。

不记录 active skill。active skill 是 prompt assembly 状态，而不是稳定的行为事实：它只能说明某段 skill instruction 被注入过，不能说明模型实际使用过它，也不能说明相关结论由它支持。

也不在 `Evidence` 上重复记录 `source_tool_name`。Evidence 已经可以通过 `created_from -> ToolResult -> tool_name` 找到来源工具；重复字段会制造同步和漂移问题。

因此当前可以回答的问题包括：

- 某条 memory 来自哪些 tool result？
- 某个 decision 基于哪些 evidence？
- 某个 run 里调用过哪些工具？

不试图用图数据库回答：

- 某个 skill 名称是否在运行中被激活过。
- 某条 memory 是否“来自某个 skill”。
- 某类失败是否经常伴随某个 skill。

但它不承诺：

- `ToolResult -> Tool node`
- `Skill -> Tool node`
- `Memory -> Skill node`
- `MCPServer -> Tool node`

这些不属于 Pulsara 图数据库的职责。

## 6. 当前可承重的图关系

当前更应该依赖已经有明确节点和写入路径的关系。

```text
Claim           --hasEvidence-->      Evidence / ToolResult
Preference      --hasEvidence-->      Evidence / ToolResult
Observation     --hasEvidence-->      Evidence / ToolResult
ActionBoundary  --hasEvidence-->      Evidence / ToolResult
Decision        --hasEvidence-->      Evidence / ToolResult
Decision        --basedOn-->          memory node

Evidence        --rt:sourceEvent-->   runtime event id
Evidence        --rt:sourceRun-->     run id
Evidence        --rt:sourceTurn-->    turn id
Evidence        --rt:sourceReply-->   reply id
ToolResult      --rt:toolName-->      tool name
```

其中 `hasEvidence`、`basedOn` 这类 NodeRef 边是当前图里真正可遍历的关系。`toolName`、`sourceEvent` 这类字段是字符串线索，可以查询和聚合，但不是跨到 capability 节点的图边。

这个区分很重要：Pulsara 可以先把证据链做好，而不是提前把资产图也拖进来。

## 7. 从流程中长出 Skill 不改变图边界

“从流程中长出 Skill”是有吸引力的方向，但它本质上是 capability-as-output：agent 不只是使用能力，还会生产、安装或修改能力。

这需要一个独立于图数据库的 producer：

- 谁决定一段流程值得沉淀为 skill？
- 谁生成 skill bundle？
- 谁审核它？
- 谁安装它？
- 谁决定它属于 user、workspace、project，还是系统级？
- 谁负责版本和废弃？

这些问题属于 capability/runtime/config 层，不属于图数据库的节点建模范围。

即使存在这样的 producer，图数据库也只记录它留下的运行证据和 memory 证据链；它不记录 active skill，也不把生成出的 skill 本身建成图节点。

## 8. 边界与反目标

明确反目标：

- 不把 capability 和 memory 混成同一种节点。
- 不让 type-only ontology 反过来规定 runtime 契约。
- 不把 graph 作为 skill/runtime 的强制 source of truth。
- 不为了“图完整”而写入没有消费者的抽象边。
- 不把 `provides_tools` 变成工具窄化或权限沙箱；它只是建议/常用工具元数据，真正的限制属于 permission/policy。
- 不构建 capability asset graph；能力资产由 runtime/config 层管理，图数据库只记录真实工具调用留下的最小能力线索。

更重要的是：每个进入图的节点和边，都应该有至少一个明确用途，比如 replay、debug、recall、governance、derivation、recommendation 或 lifecycle management。

## 9. 一句话原则

Pulsara 的图数据库先保持“证据与经验”的语义：

> runtime 提供证据，memory 提供经验，capability 只在真实工具调用这类行为事实上留下最小线索；能力资产本身不进入图数据库。
