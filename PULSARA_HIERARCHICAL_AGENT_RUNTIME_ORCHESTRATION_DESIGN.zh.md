# Pulsara Hierarchical AgentRuntime Graph Orchestration 设计

> 状态：概念设计，尚未冻结为实施规格
> 日期：2026-07-31
> 灵感：KimiX 的外层 Python 编排与 Flow Skill，但不复用其 process-local truth model
> 范围：多个完整 Pulsara AgentRuntime 组成可审计、可恢复的有向任务图

## 1. 核心想法

Pulsara 可以在现有“一个 HostSession 驱动一个 AgentRuntime”的能力之上增加一个更高层的 workflow coordinator：

```text
Workflow Coordinator
    |
    +--> AgentRuntime A: research
    |       `--> its own subagents
    |
    +--> AgentRuntime B: implementation
    |       `--> its own subagents
    |
    +--> AgentRuntime C: verification
    |
    `--> AgentRuntime D: synthesis
```

图上的主要节点不是一段 prompt callback，而是拥有完整运行边界的 Pulsara runtime：

- 自己的 runtime session 与 event ledger；
- 自己的 ResolvedModelTarget / permission snapshot / capability exposure；
- 自己的 context、long-horizon budget、compaction 和 tool lifecycle；
- 必要时可创建受限 subagent；
- 通过 typed result/artifact 与其他节点交换事实。

这里的“进程”首先指独立的逻辑 runtime，不要求 V1 为每个节点启动一个 OS process。是否进程隔离属于部署策略，不应污染 workflow semantic identity。

## 2. 它与普通 subagent 的区别

subagent 适合主 Agent 在一个任务中临时委派局部工作；workflow node 则适合预先声明的长期阶段：

| 维度 | Subagent | Workflow AgentRuntime Node |
|---|---|---|
| 生命周期 | 通常从一个 parent run 派生 | 由 workflow coordinator 独立调度 |
| 事实归属 | parent graph + child ledger | workflow ledger + node runtime ledger |
| 输入 | parent 选择的任务与上下文 | typed edge handoff、node config 与 workflow policy |
| 恢复 | 依赖 parent/child repair | 节点和 workflow 状态可独立恢复 |
| 并发 | 一次 run 内的受限 fan-out | 图级串行、并行、join 与人工 gate |
| 递归 | 由 subagent depth policy 控制 | 节点内部仍可使用既有 subagent runtime |

因此它不是要替换 Pulsara subagent，而是把多个完整 runtime 组合到更高一层。

## 3. 最小图模型

V1 优先支持静态有向无环图：

```text
PENDING -> RUNNABLE -> RUNNING -> SUCCEEDED
                           |       |
                           |       `-> downstream nodes become RUNNABLE
                           |
                           +-> WAITING_USER
                           +-> FAILED
                           +-> CANCELLED
```

基本元素：

- `Workflow`：稳定 identity、owner、全局预算与失败策略；
- `AgentNode`：完整 AgentRuntime 配置、任务、输入 contract、输出 contract；
- `Edge`：从上游 typed output 到下游 typed input 的映射；
- `Join`：等待一组必需前驱并聚合其结果；
- `HumanGate`：明确等待审批、选择或补充信息；
- `Finalizer`：形成 workflow 的最终交付。

循环、动态改图、无限自治和 arbitrary code node 暂不进入 V1。需要迭代时，先使用 bounded、typed iteration node，而不是允许 Python 随意修改已提交的图。

## 4. Python 是 authoring layer，不是事实源

可以提供易读的 Python API：

```python
flow = Workflow("review-and-fix")

review = flow.agent(
    "review",
    task="审阅当前 dirty changes，输出结构化 findings",
)
fix = flow.agent(
    "fix",
    task="根据已确认 findings 修复代码",
    needs=[review],
)
verify = flow.agent(
    "verify",
    task="运行定向验证并复核修复",
    needs=[fix],
)

flow.output_from(verify)
```

但执行前必须把它编译成 immutable、可 fingerprint 的 workflow plan。运行中真正拥有状态的是 Pulsara coordinator 与 durable ledger，而不是仍在内存中的 Python coroutine、闭包或全局变量。

这样 Python、CLI、未来 Mermaid/D2 Flow Skill 都可以成为不同的 authoring frontend：

```text
Python DSL ─┐
CLI spec ───┼──> Typed Workflow Plan ──> Durable Coordinator
Mermaid/D2 ─┘
```

## 5. Typed handoff，不共享整段 mutable context

节点之间默认不继承对方完整 transcript。edge 应传递显式、bounded 的结果：

- result summary；
- artifact IDs；
- findings / decisions / test outcomes 等 typed facts；
- provenance：上游 workflow/node/runtime/run/terminal event；
- downstream 所需的最小 workspace snapshot 或 revision identity。

下游节点根据自己的 context policy 将 handoff materialize 为 provider-visible candidate。这样可以避免：

- 多个 runtime 共同修改一份 LoopState；
- 把上游数十轮 transcript 全量复制给下游；
- 无法分辨结论来自哪个节点；
- 上游被取消或失败后仍交付未确认的中间文本。

只有上游节点 durable terminal success 且 handoff FULL commit 后，下游节点才能把它当作已满足依赖。

## 6. Workspace 与副作用

多 runtime 编排最大的实际风险不是图调度，而是共享 workspace。

V1 应显式选择节点 workspace policy：

- `shared_read_only`：多个节点可并行检查，不能写；
- `shared_serial_write`：写节点串行获得 workspace mutation lease；
- `isolated_worktree`：节点在独立 worktree 修改，之后显式 merge；
- `artifact_only`：节点不接触 workspace，只消费和生产 artifacts。

不能默认让多个可写节点并行修改同一目录。merge、冲突解决与最终 publish 都必须是独立、可审计的 workflow action。

## 7. 权限、能力与预算

workflow 只能收窄节点能力，不能替节点预先授予权限：

```text
workflow maximum authority
    ∩ node profile
    ∩ current Host/session policy
    ∩ run-frozen permission/capability resolution
    = node effective authority
```

每个节点独立解析 model target、capability exposure 和 permission snapshot。workflow-level tool list 只是最大允许集合，不是执行 authorization。

预算同样分层：

- workflow 总 rollout / wall-clock / node-count budget；
- 每个 node 的 model/tool/subagent budget；
- 并发上限；
- retry budget；
- final synthesis reserve。

节点内部创建 subagent 时，同时受 node budget 与 workflow 剩余预算约束，不能绕过图级上限。

## 8. Durable ownership 与恢复

workflow coordinator 拥有图级事实：

- workflow created/started/terminal；
- graph plan identity；
- node admission、activation、terminal disposition；
- edge handoff committed；
- join readiness；
- human gate pending/resolved；
- workflow cancel/close/recovery。

每个 AgentRuntime 继续拥有自己的 run、model、tool、compaction、subagent 和 context facts。workflow ledger只保存稳定跨 ledger reference，不复制节点的完整事件。

重启后 coordinator 应从 durable facts恢复：

1. 哪些节点从未启动；
2. 哪些节点已有 RunStart 但缺 terminal，需要走节点自身 recovery；
3. 哪些 handoff 已确认；
4. 哪些 join 已满足；
5. 哪些节点可重新进入 runnable；
6. workflow 是否处于 waiting、failed、cancelled 或 completed。

不得因为内存中的 `asyncio.Task` 丢失，就把可能已经启动的节点当作“尚未运行”重新执行。

## 9. Goal mode 是它的简化特例

常见 Goal Mode 可以表达成一个固定模板：

```text
clarify/plan
      |
      v
execute -----> human gate (optional)
      |
      v
verify
      |
      +-- failed and retry budget remains --> execute
      |
      v
finalize
```

如果产品只开放这个固定模板，它就是 graph orchestration 的简化特例。通用 workflow 能力不应为了 Goal Mode 立即暴露所有图操作；Goal Mode 可以先作为受约束的 built-in plan 编译器。

## 10. 与 KimiX / LangGraph 的关系

KimiX 的 Python 编排很轻：Python 程序在外层顺序调用多个 agent session，容易理解，也方便实验。其 Flow Skill 进一步允许 Mermaid/D2 驱动同一 Agent context 中的 task/decision 节点。

Pulsara 可以借鉴其 authoring 体验，但执行语义更接近：

```text
durable workflow coordinator
    + independently owned AgentRuntime nodes
    + typed cross-ledger handoffs
    + explicit workspace/permission/budget policy
```

它也不同于把所有逻辑塞进 LangGraph-style state dictionary：Pulsara node 是完整 runtime，图状态与节点 working state 分属不同 owner，不共享一个任意 mutable map。

## 11. 推荐演进顺序

这不是当前 runtime hard cut 的顺手扩展，建议独立演进：

1. 先冻结最小 `WorkflowPlan`、node/edge identity 和 durable state machine；
2. 支持串行 AgentRuntime nodes 与 typed handoff；
3. 增加并行只读节点和 deterministic join；
4. 增加 workspace mutation lease / isolated worktree；
5. 增加 human gate、cancel、restart recovery；
6. 提供 Python authoring DSL；
7. 最后再增加 Mermaid/D2 frontend、bounded map node 和 Goal Mode templates。

每一步都应复用现有 AgentRuntime、HostSession、subagent、permission、artifact 和 event writer，而不是建立第二套轻量 runtime。

## 12. 暂不纳入

- arbitrary Python coroutine 直接成为 durable node；
- 多节点共享 mutable LoopState；
- 未经 typed handoff 复制完整 transcript；
- 节点用 ambient authority 继承所有 Host 工具；
- 并行写同一 workspace；
- 通过回滚 transcript 假装回滚已发生的外部副作用；
- 无界循环、无界 fan-out 或自动递归扩图；
- 为了编排重新实现一套 subagent runtime。

## 13. 设计结论

这个方向的价值不只是“把多个 agent 串起来”，而是把完整 Pulsara runtime 变成可组合的可靠计算节点：

```text
AgentRuntime = durable, permissioned, budgeted node
Typed handoff = edge
Workflow coordinator = graph owner
Subagent runtime = node-internal delegation
```

Python 和 Flow Skill 可以让图容易编写；Pulsara ledger、owner 与 recovery contract 则确保它不是一次性脚本。两者结合后，既能覆盖 Goal Mode，也能支持研究、实现、验证、综合等更复杂的长程任务。
