# Pulsara Subagent Runtime / Deno WorkflowScript 初版实施计划

## 0. 摘要

Pulsara 下一阶段可以把主 agent 升级为 **Orchestrator**：它不只是直接解决任务，而是先搭建一个可执行、可审计、可恢复的建议工作流，再由多个受控 subagent 节点并行或串行完成子任务，最终由 Orchestrator 综合结果。

本计划建议采用：

> **Pulsara-owned Subagent Runtime + sandboxed Deno WorkflowScript authoring surface**

也就是说：

- Orchestrator 可以写一段 JS/TS workflow script；
- script 只能调用 Pulsara 提供的极小 workflow SDK，如 `phase()` / `agent()` / `parallel()` / `log()`；
- Deno 只负责执行受控控制流，不拥有真实工具、文件、网络、权限或记忆；
- Pulsara runtime 仍是唯一事实真源：workflow graph、subagent runs、events、artifacts、permission decisions、context compilation、compaction、memory 都由 Pulsara 管。

这条路线吸收 Claude Code 的 workflow-as-code 形态，但不引入 LangGraph / Temporal / Inngest 作为核心 runtime，避免双 runtime、双状态机、双 observability。

## 1. 背景：为什么是 workflow-as-code

Claude Code 本地 workflow 脚本显示出一种非常自然的形态：

```js
phase("Verify")

const [a, b, c] = await parallel([
  () => agent("Review runtime code", { label: "runtime", schema: V }),
  () => agent("Review tests", { label: "tests", schema: V }),
  () => agent("Review contracts", { label: "contracts", schema: V }),
])

phase("Synthesize")

return await agent(`Synthesize:\n${a}\n${b}\n${c}`, { label: "synthesize" })
```

这种形态有几个优势：

- 模型天然熟悉 JS/TS 的变量、数组、模板字符串、`await`、函数；
- 比纯 JSON DAG 表达力更强；
- 比让模型写任意 Python / shell 脚本安全得多；
- 可以直接表达 fan-out / fan-in / synthesis；
- 每个 `agent()` call 可以投影成一个 Pulsara subagent node；
- 每个 `parallel()` 可以投影成一个 fan-out group；
- 每个 `phase()` 可以投影成一个 workflow phase event。

但是，workflow-as-code 不能成为黑盒。Pulsara 必须把脚本执行过程编译/投影为自己的 typed events 与 WorkflowGraph。

## 2. 核心设计判断

### 2.1 不把 LangGraph 放入 core

LangGraph 很适合用户项目里的 agent graph，也值得做可选 adapter / exporter。但它自身有：

- state；
- checkpoint；
- interrupt / human-in-the-loop；
- tool abstraction；
- tracing / observability；
- persistence / recovery。

这些和 Pulsara 当前已经拥有的 runtime ledger、pending interaction、tool registry、capability gate、artifact store、context compiler、memory surface 重叠。把它放进 core 会导致“谁是真相”的问题。

### 2.2 Deno 是 authoring runner，不是 runtime truth

Deno 的职责是：

- 执行 JS/TS 控制流；
- 提供语法层面的熟悉感；
- 通过权限沙箱隔离脚本。

Deno 不负责：

- 读取 workspace；
- 访问网络；
- 运行 shell；
- 调用工具；
- 写 artifacts；
- 读 `.env`；
- 管理 subagent 状态；
- 决定权限；
- 生成 inspect 真相。

所有真实动作必须通过 Pulsara host bridge 发生。

### 2.3 Pulsara owns the graph

最终可审计事实不是 JS 文件本身，而是 Pulsara 的 workflow graph projection：

```text
WorkflowScriptArtifact
  ↓
WorkflowRunStartedEvent
  ↓
WorkflowPhaseStartedEvent
  ↓
WorkflowNodeRequestedEvent / SubagentRunStartedEvent
  ↓
SubagentRunCompletedEvent / Failed / Cancelled
  ↓
WorkflowNodeCompletedEvent
  ↓
WorkflowRunCompletedEvent
```

Inspector 应该能回答：

- Orchestrator 为什么创建这个 workflow？
- workflow script 是哪一版？
- 哪些 phase 被执行？
- 哪些 subagent 节点被启动？
- 节点之间依赖关系是什么？
- 每个 subagent 看到了哪些输入？
- 它产生了哪些 artifacts / summaries？
- 最终合成使用了哪些子结果？
- 哪个节点失败、重试、被取消或被预算阻断？

## 3. V1 目标

### 3.1 用户可感知目标

用户给出复杂任务后，Pulsara 可以：

1. Orchestrator 生成一个 workflow script；
2. 可选展示/解释这个 workflow；
3. 用户确认后执行；
4. 并行启动多个 subagent；
5. 等待结果；
6. 让 synthesis agent 综合；
7. 返回最终答案；
8. inspect 能复盘整个 workflow。

### 3.2 技术目标

V1 需要实现：

- Deno sandbox runner；
- Pulsara workflow SDK；
- JSON-RPC bridge；
- `phase()` / `log()` / `agent()` / `parallel()`；
- subagent run primitive；
- workflow events；
- workflow graph projection；
- step label / fingerprint / memoization；
- bounded execution budget；
- cancellation / timeout；
- failure event；
- inspect projection。

### 3.3 非目标

V1 不做：

- npm imports；
- script 访问 workspace 文件；
- script 网络请求；
- script 运行 shell；
- long-lived Deno worker pool；
- arbitrary user JS host callback；
- recursive subagent nesting；
- durable instruction-level JS continuation；
- LangGraph runtime integration；
- Temporal/Inngest/Trigger.dev integration；
- UI graph editor。

## 4. 架构概览

```text
Main Agent / Orchestrator
        │ writes
        ▼
WorkflowScript Artifact (.ts)
        │ executed by
        ▼
DenoWorkflowRunner
        │ JSON-RPC over stdio
        ▼
Pulsara WorkflowHostBridge
        │ emits events / starts runs
        ▼
Subagent Runtime
        │ uses existing
        ▼
Context Compiler · Capability Runtime · Tool Registry · Artifacts · Memory
```

### 4.1 新组件

#### `WorkflowScriptArtifact`

保存 Orchestrator 生成的 workflow script：

- `workflow_script_id`
- `script_text`
- `script_hash`
- `created_by_run_id`
- `created_by_reply_id`
- `language="typescript"`
- `sdk_version`
- `metadata`

脚本本身应作为 artifact 落盘，便于 inspect / rerun / audit。

#### `DenoWorkflowRunner`

负责：

- 创建临时目录；
- 写入 `workflow.ts`；
- 写入 `pulsara_workflow_sdk.ts`；
- 启动 Deno subprocess；
- 设置权限；
- 处理 stdout/stderr；
- 处理 timeout / cancellation；
- 清理临时目录。

#### `WorkflowHostBridge`

负责 JSON-RPC：

- 接收 Deno SDK 发出的 `phase` / `log` / `agent` / `parallel_group` 等请求；
- 校验请求 schema；
- 生成 workflow events；
- 启动 subagent runs；
- 返回结果给 Deno。

#### `WorkflowExecutionState`

runtime 内部状态：

- `workflow_run_id`
- `workflow_script_id`
- `current_phase`
- `nodes`
- `edges`
- `step_cache`
- `running_subagents`
- `budget`
- `status`

#### `SubagentRuntime`

不是第二套 agent runtime，而是对现有 `AgentRuntime` 的受限 child-run 封装：

- 每个 subagent node 是一个 child run；
- 每个 child run 有自己的 context compile request；
- child run 可以有独立 tool/capability scope；
- child run output 应产生 structured result + artifact summary；
- child run 的 events 要能按 parent workflow/node 聚合。

## 5. Workflow SDK API

V1 SDK 只暴露极小集合。

### 5.1 `phase(name: string): void`

声明当前 workflow phase。

效果：

- Deno SDK 发 RPC；
- Pulsara 写 `WorkflowPhaseStartedEvent`；
- 后续 `agent()` 默认归属该 phase。

约束：

- `name` 必须是短字符串；
- 不能无限创建 phase；
- phase 只影响 workflow metadata，不赋予权限。

### 5.2 `log(message: string): void`

写 workflow log。

效果：

- 不允许直接 `console.log` 污染 stdout；
- `log()` 通过 RPC 写 `WorkflowLogEvent`；
- message 有长度上限。

### 5.3 `agent(prompt: string, options?: AgentOptions): Promise<AgentResult>`

启动一个 subagent node。

建议 V1 options：

```ts
type AgentOptions = {
  label: string
  phase?: string
  schema?: object
  effort?: "low" | "medium" | "high"
  tools?: string[]
  context?: object
  maxTurns?: number
  timeoutMs?: number
}
```

关键约束：

- `label` V1 强烈建议必填；未来可以变成必填；
- `(workflow_run_id, label, prompt_hash, options_hash)` 构成 step fingerprint；
- 相同 fingerprint 可 memoize；
- `tools` 是请求范围，不是授权结果；最终仍由 capability / permission gate 决定；
- `schema` 只是 subagent output format 约束，不是 runtime truth。

### 5.4 `parallel(tasks, options?): Promise<T[]>`

并行执行一组 async task。

```ts
const [a, b] = await parallel([
  () => agent("Check runtime", { label: "runtime" }),
  () => agent("Check tests", { label: "tests" }),
], { label: "verify", maxConcurrency: 2 })
```

效果：

- Pulsara 写 fan-out group；
- 每个 nested `agent()` 仍是独立 node；
- runner 控制最大并发；
- 任一 task 失败时 V1 默认 fail-fast，后续可以支持 `settleAll`。

### 5.5 `return`

script 的最终返回值成为 workflow result：

- string：作为 final summary；
- object：作为 structured result；
- 大对象需要 artifact 化；
- final result 写 `WorkflowRunCompletedEvent`。

## 6. Deno sandbox 策略

### 6.1 临时目录布局

```text
/tmp/pulsara-workflow-<id>/
  workflow.ts
  pulsara_workflow_sdk.ts
```

### 6.2 启动命令

V1 推荐：

```bash
deno run \
  --no-prompt \
  --allow-read=/tmp/pulsara-workflow-<id> \
  --deny-write \
  --deny-net \
  --deny-env \
  --deny-run \
  /tmp/pulsara-workflow-<id>/workflow.ts
```

说明：

- `--allow-read` 只允许读取临时目录，以便 import SDK；
- 不允许写；
- 不允许网络；
- 不允许环境变量；
- 不允许运行 subprocess；
- 不允许交互式 permission prompt；
- stdout 只用于 JSON-RPC framing；
- stderr 收集为 diagnostic。

### 6.3 禁止能力

script 不得：

- `Deno.readTextFile` 读取 repo；
- `fetch`；
- `Deno.Command`；
- 读取 `.env`；
- npm import；
- 动态 import 外部 URL；
- 直接写 stdout 任意文本。

### 6.4 console 策略

V1 可以在 SDK 中覆盖：

```ts
console.log = (...args) => log(args.join(" "))
```

但 stdout framing 仍必须严格：

- JSON-RPC channel 只接受 SDK 输出；
- 非法 stdout 行触发 workflow failure diagnostic；
- stderr 不参与 RPC，只进入 diagnostic artifact。

更稳的未来方案：

- RPC 走 fd 3/4；
- stdout/stderr 完全作为 display logs。

## 7. JSON-RPC Bridge

### 7.1 请求形态

SDK 向 Pulsara host 发送 JSON lines：

```json
{"id":"1","method":"agent","params":{"prompt":"...","options":{"label":"runtime"}}}
```

Pulsara 返回：

```json
{"id":"1","result":{"text":"...","artifacts":[],"metadata":{}}}
```

错误：

```json
{"id":"1","error":{"code":"subagent_failed","message":"...","metadata":{}}}
```

### 7.2 Host methods

V1 methods：

- `phase`
- `log`
- `agent`
- `parallel_group_started`
- `parallel_group_completed`
- `workflow_return`

注意：`parallel()` 可以主要由 JS 层实现，但 host 仍需要观测 fan-out group。

## 8. Workflow events 初稿

### 8.1 `WorkflowRunStartedEvent`

字段：

- `workflow_run_id`
- `workflow_script_id`
- `parent_run_id`
- `parent_turn_id`
- `parent_reply_id`
- `script_hash`
- `sdk_version`
- `budget`

### 8.2 `WorkflowPhaseStartedEvent`

字段：

- `workflow_run_id`
- `phase_id`
- `name`
- `sequence_in_workflow`

### 8.3 `WorkflowNodeRequestedEvent`

字段：

- `workflow_run_id`
- `node_id`
- `label`
- `phase_id`
- `kind="subagent"`
- `prompt_hash`
- `options_hash`
- `schema_hash`
- `requested_tools`
- `dependencies`
- `memoization_key`

### 8.4 `SubagentRunStartedEvent`

字段：

- `workflow_run_id`
- `node_id`
- `subagent_run_id`
- `runtime_session_id`
- `context_id`
- `capability_exposure_generation`

### 8.5 `SubagentRunCompletedEvent`

字段：

- `workflow_run_id`
- `node_id`
- `subagent_run_id`
- `status`
- `summary`
- `artifact_ids`
- `token_estimate`
- `duration_ms`

### 8.6 `WorkflowNodeCompletedEvent`

字段：

- `workflow_run_id`
- `node_id`
- `result_preview`
- `result_artifact_id`
- `memoized`

### 8.7 `WorkflowRunCompletedEvent`

字段：

- `workflow_run_id`
- `status`
- `result_preview`
- `result_artifact_id`
- `node_count`
- `failed_node_count`
- `duration_ms`

### 8.8 `WorkflowRunFailedEvent`

字段：

- `workflow_run_id`
- `error_code`
- `message`
- `phase_id`
- `node_id`
- `stderr_artifact_id`
- `diagnostics`

## 9. Graph projection

Pulsara 应从事件投影出 workflow graph：

```json
{
  "workflow_run_id": "workflow:...",
  "nodes": [
    {
      "id": "node:verify-runtime",
      "label": "verify-runtime",
      "phase": "Verify",
      "kind": "subagent",
      "status": "completed",
      "subagent_run_id": "run:..."
    }
  ],
  "edges": [
    {
      "from": "node:verify-runtime",
      "to": "node:synthesize",
      "reason": "data_dependency"
    }
  ]
}
```

V1 可先用执行顺序和 `parallel()` group 推导基础 edges：

- 同一 `parallel()` 内 nodes 彼此无依赖；
- `parallel()` 后使用其结果的后续 `agent()` 依赖这些 nodes；
- 如果静态 JS 数据依赖难以准确解析，V1 可以让 SDK 在 promise result 中携带 provenance，host 根据 provenance 建边。

## 10. Step memoization / replay

V1 不恢复 JS call stack，而是借鉴 durable step model。

### 10.1 Step key

```text
workflow_run_id
label
method
prompt_hash
options_hash
sdk_version
```

### 10.2 行为

当 script rerun 时：

- 如果 step key 命中 completed result，直接返回 cached result；
- 不重复启动 subagent；
- 写 `WorkflowStepReusedEvent` 或在 node completed event 标记 `memoized=true`。

### 10.3 限制

- label 不稳定会导致无法 memoize；
- prompt/options 变化会生成新 step；
- V1 可以只支持同一 workflow run 内 memoization；
- durable resume 可从 event log 投影 step cache，但不必第一版就做完整 replay。

## 11. Permission / capability 边界

Workflow script 本身没有工具权限。

能力只在 subagent run 内发生：

```text
workflow.ts
  agent("Use terminal to run tests", { tools: ["terminal"] })
       ↓
Pulsara starts subagent
       ↓
Subagent context compiler + capability exposure
       ↓
Permission gate
       ↓
Tool execution
```

`tools` option 只表示 Orchestrator 期望，不是授权。

每个 subagent run 都必须：

- resolve capability exposure；
- 写 gate events；
- 受 permission policy 约束；
- 产出 artifacts；
- 可被 inspect。

## 12. Context compiler 集成

每个 subagent node 需要独立 `ContextCompileRequest`：

- parent user goal；
- workflow phase；
- node objective；
- dependencies outputs；
- selected artifacts；
- allowed memory projection；
- current workflow graph summary；
- node-specific tool/capability scope。

不要把整个主 agent trajectory 直接塞给 subagent。

理想形态：

```text
Subagent sees:
- user goal summary
- node objective
- relevant prior artifacts
- dependency outputs
- local instructions

Subagent does NOT see:
- unrelated previous turns
- all tool outputs
- unrelated sibling node internals
```

这正好是 context compiler 的强项。

## 13. Artifact 策略

以下内容应 artifact 化：

- workflow script；
- Deno stderr；
- large subagent outputs；
- per-node structured result；
- final synthesis bundle；
- graph projection snapshot。

模型可见内容只保留 preview + artifact refs。

## 14. Failure semantics

### 14.1 Deno compile/runtime failure

例如 TS syntax error、illegal import、permission denied：

- workflow fail；
- 写 `WorkflowRunFailedEvent`；
- stderr / diagnostic artifact 化；
- 不启动 subagent。

### 14.2 Subagent failure

V1 默认：

- `agent()` reject；
- `parallel()` fail-fast；
- workflow fail；
- 后续可支持 `settleAll`。

### 14.3 Timeout

需要至少三层：

- workflow total timeout；
- Deno subprocess timeout；
- subagent node timeout。

### 14.4 Cancellation

用户 stop：

- cancel Deno process；
- cancel running subagents；
- write cancellation events；
- preserve completed node results。

## 15. Security model

主要攻击面：

1. Orchestrator 生成恶意 JS；
2. script 试图访问文件/网络/env；
3. script 试图污染 stdout RPC；
4. script 无限循环；
5. script 大量创建 subagents；
6. prompt injection 诱导 subagent 越权。

V1 防线：

- Deno deny permissions；
- subprocess timeout；
- max agent calls；
- max parallel width；
- max phases/log size；
- strict JSON-RPC schema；
- no npm / remote imports；
- no env；
- no shell；
- subagent capability gate；
- workflow event audit。

## 16. CLI / UX 初稿

可能命令：

```bash
pulsara workflow doctor
pulsara workflow run <script.ts> --workspace .
pulsara workflow inspect <workflow_run_id>
```

REPL 体验：

```text
pulsara> 请你用 subagents 审阅这组改动
Orchestrator proposed workflow:
  Phase Verify: 3 subagents
  Phase Review: 2 subagents
  Phase Synthesize: 1 subagent
Proceed? [y/N]
```

V1 可以先不做自动确认 UI，把 workflow execution 作为内部 capability，后续再接 Plan Mode。

## 17. PR 拆分建议

### PR0：设计与契约

- 本文档；
- `SUBAGENT_RUNTIME_CONTRACT.zh.md`；
- `WORKFLOW_SCRIPT_CONTRACT.zh.md`；
- 确认 Deno 作为 V1 runner。

### PR1：Deno runner spike

- detect `deno`；
- write tempdir script + SDK；
- run script with deny permissions；
- JSON-RPC echo method；
- timeout / stderr capture；
- tests cover permission denied / syntax error / illegal stdout。

### PR2：Workflow events + graph projection

- typed events；
- event log roundtrip；
- inspector projection；
- graph reconstruction。

### PR3：`phase` / `log` / `agent`

- SDK methods；
- host bridge；
- one subagent node execution；
- child run event linkage；
- result artifact / preview。

### PR4：`parallel`

- fan-out group；
- max concurrency；
- fail-fast；
- cancellation。

### PR5：memoization / rerun

- stable step key；
- event-projected step cache；
- repeated script run reuses completed nodes。

### PR6：Orchestrator integration

- main agent can propose workflow script；
- script stored as artifact；
- user confirmation boundary；
- execute workflow；
- final synthesis。

### PR7：dogfood workflows

- code review workflow；
- research workflow；
- test/audit workflow；
- long-running terminal workflow with subagent handoff.

## 18. Test plan

### Unit tests

- Deno missing → diagnostic；
- Deno syntax error → `WorkflowRunFailedEvent`；
- denied fs/net/env/run access → failure diagnostic；
- stdout non-JSON → failure diagnostic；
- `phase()` emits event；
- `log()` emits event；
- `agent()` creates node event；
- subagent result returns to script；
- `parallel()` starts multiple nodes；
- max concurrency respected；
- max agent calls enforced；
- timeout kills subprocess；
- cancellation kills subprocess and child runs。

### Integration tests

- simple workflow:

```ts
phase("One")
const a = await agent("Say hello", { label: "hello" })
return a
```

- verify/review/synthesize workflow；
- failed subagent propagates failure；
- memoized rerun does not duplicate subagent；
- inspector graph projection stable。

### Real LLM dogfood

- Orchestrator writes workflow to review dirty changes；
- subagents inspect disjoint files；
- synthesis combines results；
- inspect can explain all nodes and artifacts。

## 19. Open questions

1. V1 是否强制 `agent(..., { label })` 必填？
   - 建议：是。没有 label 不利于 memoization 与 inspect。

2. Deno 是否作为必须安装依赖？
   - 建议：V1 optional，`pulsara workflow doctor` 提示安装。

3. 是否允许 script import 用户自定义 helper？
   - 建议：V1 不允许。只允许 Pulsara 生成的 SDK。

4. 是否先做 Plan Mode confirmation？
   - 建议：workflow execution 属于高阶自动化，默认需要用户确认。

5. 是否支持 nested subagent？
   - 建议：V1 禁止；未来可以允许 subagent 申请 workflow。

6. 是否支持 durable resume？
   - 建议：V1 先支持同 run memoization；跨进程 durable resume 放 V2。

## 20. 结论

Pulsara 的 subagent runtime 不应直接依附 LangGraph，也不应让模型随意写 shell/Python。更自然的路线是：

> **用 Deno 执行受控 JS/TS WorkflowScript；用 Pulsara runtime 执行、审计、恢复真实 workflow graph。**

这让模型获得熟悉、灵活的 authoring surface，同时让 Pulsara 保持自己的核心优势：

- event ledger；
- capability gate；
- permission facts；
- context compiler；
- artifacts；
- memory；
- inspector；
- local-first durable runtime。

V1 最小闭环可以很小：`phase` / `agent` / `parallel` / `log`。一旦跑通，Pulsara 就能拥有非常强的 Orchestrator → subagents 工作流能力，而且这套能力会天然继承现有 runtime 的可审计性与长期工作流优势。
