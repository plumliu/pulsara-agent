# Agent Runtime MVP 设计书

## 状态标注

```text
Current
  event/message substrate
  LLM provider adapter boundary
  basic runtime state object

Next
  hand-written main agent loop
  context assembly / projection wiring
  tool execution orchestration

Target
  Claude Code-like inner turn loop + OpenClaw-like lifecycle fully落地
  compaction / recovery / budgets / persistence 全部接入主循环
```

本文档定义 Python 版通用 Agent 的第一版 MVP runtime。它与根目录下的 `MEMORY_SYSTEM_IMPLEMENTATION_DETAILS.zh.md` 配套：记忆系统文档负责 JSON-LD / RDF / SPARQL / 多级记忆对象；本文档负责主 Agent loop、生命周期、工具执行、provider 边界和第一版可运行范围。

本文档的基准判断：

```text
Macro lifecycle 借 OpenClaw：
session_start / context_assembly / model_call / tool_execution / persistence / compaction

Inner turn loop 借 Claude Code：
state object / async event stream / tool concurrency policy / recovery / budget / compaction

Provider boundary 借 Hermes：
memory/model/tool provider 都是 adapter，不拥有 loop
```

核心目标不是做一个功能很多的 Agent 框架，而是做一个能承载 JSON-LD 语义记忆系统的最小 runtime。

## 1. MVP 目标

### 1.1 必须完成

MVP 必须完成以下能力：

1. 有一个手写 main agent loop，不依赖 LangChain / LangGraph 等通用 agent 框架驱动核心流程。
2. 有清晰的 macro lifecycle，每个阶段都能挂 hook。
3. 有 Claude Code 式的 `LoopState`，所有 turn 间状态显式保存。
4. 有 async event stream，UI / CLI / 日志 / 调试器都消费事件，而不是侵入 loop。
5. 有 provider adapter 边界：model、tool、memory provider 都不能拥有 loop。
6. 有 JSON-LD memory runtime 接入点：scope resolve、memory projection、write gate、persistence、compaction。
7. 有工具执行策略：只读工具可并发，写工具串行，危险工具经过 permission gate。
8. 有 token / turn / tool call 预算。
9. 有失败恢复：模型输出不可解析、工具失败、重复失败、预算耗尽、上下文过长。
10. 有 session archive 和最小 compaction。

### 1.2 暂不完成

MVP 不做：

1. 多 agent PA/DA/CA/AA 完整编排。
2. provider marketplace。
3. 自主长期运行 daemon。
4. 复杂 GUI。
5. 分布式任务队列。
6. 多模型动态竞价路由。
7. 复杂 skill graph 自进化。
8. 完整 OpenClaw/Hermes 兼容层。

MVP 要先证明一件事：

```text
主 loop 能稳定运行，并且 JSON-LD memory 是 runtime 的第一等对象，而不是事后外挂。
```

## 2. 总体架构

### 2.1 模块图

```text
AgentRuntime
  owns main loop
  owns lifecycle
  owns LoopState

ModelProvider
  complete()
  stream()

ToolRegistry
  list_tools()
  get_tool()

ToolExecutor
  execute tool calls
  enforce concurrency policy

PermissionGate
  approve / reject / require_user_confirm

MemoryRuntime
  scope_resolve()
  project()
  write_candidate()
  persist_turn()
  compact()
  consolidate()

ContextBuilder
  build model input from system prompt + messages + memory projection + tools

EventBus
  async event stream

SessionStore
  archive messages, tool calls, tool results, events, compaction boundaries
```

### 2.2 Runtime 主权原则

```text
AgentRuntime owns the loop.
Providers supply capabilities.
Hooks observe / modify / veto lifecycle events.
MemoryRuntime owns canonical JSON-LD store.
```

任何外部 provider 都不能直接把内容写入长期记忆。它们只能产生 `MemoryCandidate`，再交给 `MemoryWriteGate`。

## 3. Macro Lifecycle

MVP 的宏观生命周期如下：

```text
session_start
  -> intake
  -> context_assembly
  -> turn_loop
       -> before_model_call
       -> model_call
       -> after_model_call
       -> tool_execution, if any
       -> observation
       -> persistence
       -> compaction_check
  -> session_end
  -> consolidation
```

### 3.1 session_start

职责：

```text
创建 Session
创建 SessionScope
初始化 LoopState
准备 Working Context Cache
生成初始 Durable Semantic Memory projection
打开 EventBus
记录 session_start event
```

输入：

```text
user_id
agent_id
initial_user_message
optional workspace
optional artifacts
runtime config
```

输出：

```text
SessionContext
LoopState
```

### 3.2 intake

职责：

```text
规范化用户输入
识别附件 / 文件 / URL / workspace
创建 Turn
创建 Message JSON-LD object
写入 SessionArchive
```

MVP 中 intake 不做复杂 intent router，只做轻量分类：

```text
chat
tool_task
memory_command
system_command
```

### 3.3 context_assembly

这是 JSON-LD memory 系统最关键的接入点。

流程：

```text
ScopeResolver.resolve()
ProjectionPlanner.plan()
MemoryRuntime.project()
ProjectionFilter.apply()
ContextBuilder.build()
ToolRegistry.list_for_context()
BudgetManager.estimate()
```

输出：

```text
ModelRequest
  system_prompt
  messages
  tools
  memory_projection
  budget_config
```

### 3.4 model_call

职责：

```text
调用 ModelProvider
支持 stream 或 non-stream
记录 request/response metadata
解析 assistant message
识别 tool calls
```

MVP 支持 OpenAI-compatible Chat Completions 即可。

### 3.5 tool_execution

职责：

```text
解析 tool calls
按工具安全级别分组
PermissionGate 检查
只读工具并发执行
写工具串行执行
危险工具要求确认或拒绝
工具结果写入 SessionArchive
大结果压缩或存 artifact
```

### 3.6 persistence

每一轮结束后必须持久化：

```text
user message
assistant message
tool calls
tool results
events
token usage
memory candidates
```

同时运行：

```text
MemoryWriteGate.evaluate()
MemoryRuntime.write_candidate()
ContextFencing.mark_recalled_memory()
```

### 3.7 compaction

触发条件：

```text
token budget 超过阈值
messages 数量超过阈值
tool result 过大
用户显式要求压缩
session 即将结束
```

MVP compaction 做两件事：

```text
生成 SessionSummary JSON-LD object
把旧 messages 替换为 compact boundary + summary reference
```

### 3.8 session_end

职责：

```text
finalize Session
flush pending memory candidates
生成 session summary
触发 consolidation
关闭 event stream
```

## 4. Inner Turn Loop

### 4.1 LoopState

参考 Claude Code，MVP 中 loop 状态必须显式建模。

```python
class LoopState:
    session_id: str
    turn_id: str
    messages: list[Message]
    active_scopes: list[str]
    memory_projection: MemoryProjection | None
    tool_context: ToolContext
    pending_tool_calls: list[ToolCall]
    token_usage: TokenUsage
    turn_count: int
    tool_call_count: int
    consecutive_model_failures: int
    consecutive_tool_failures: int
    recovery_mode: bool
    compacted: bool
    status: Literal["running", "waiting_tool", "finished", "failed", "aborted"]
```

原则：

```text
所有跨 turn 状态都进入 LoopState。
不要把 loop 状态散落在局部变量和隐式对象里。
```

### 4.2 Turn Loop 伪代码

```python
async def run_task(input: UserInput) -> TaskResult:
    state = await session_start(input)

    while not state.done:
        await emit("turn_start", state)

        model_request = await context_assembly(state)

        try:
            assistant = await model_provider.complete(model_request)
        except ModelError as error:
            state = await recover_model_error(state, error)
            continue

        await persist_assistant_message(state, assistant)

        if assistant.tool_calls:
            tool_results = await tool_executor.run(
                assistant.tool_calls,
                state.tool_context,
            )
            await persist_tool_results(state, tool_results)
            await memory_runtime.observe_tool_results(state, tool_results)
            state = state.next_turn()
            continue

        if assistant.is_final:
            state.status = "finished"
            break

        state = state.next_turn()

        if await should_compact(state):
            state = await compact(state)

    return await session_end(state)
```

### 4.3 Loop Transition

MVP 中 turn transition 必须显式：

```text
continue_after_tool
continue_after_recovery
continue_after_compaction
finish
abort
fail
wait_for_user
```

这样测试时可以直接断言 loop 为什么继续。

## 5. Event Stream

MVP 使用 async event stream，避免 UI、日志、调试器侵入 runtime。

### 5.1 Event 类型

```text
session_started
turn_started
context_assembled
memory_projected
model_request_started
model_delta
model_completed
tool_call_started
tool_call_completed
tool_call_failed
permission_requested
permission_denied
memory_candidate_created
memory_written
compaction_started
compaction_completed
recovery_started
session_completed
session_failed
```

### 5.2 Event 数据要求

每个 event 至少包含：

```text
event_id
session_id
turn_id
timestamp
type
payload
trace_ids
```

不要在 event 中塞大段 tool result。大结果放 SessionArchive / ArtifactStore，event 里放 `@id` 或 handle。

## 6. Tool System

### 6.1 ToolProvider 接口

```python
class ToolProvider(Protocol):
    async def list_tools(self, context: ToolContext) -> list[ToolSpec]: ...
    async def execute(self, call: ToolCall, context: ToolContext) -> ToolResult: ...
```

### 6.2 ToolSpec

```python
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    safety_level: Literal["readonly", "workspace_write", "external_side_effect", "dangerous"]
    concurrency_safe: bool
    allowed_scopes: list[str]
```

### 6.3 Concurrency Policy

借鉴 Claude Code：

```text
连续 readonly + concurrency_safe 工具可以并发。
写工具必须串行。
危险工具必须先过 PermissionGate。
工具执行后如果修改 ToolContext，修改必须按确定顺序应用。
```

### 6.4 MVP 内置工具

第一版只需要：

```text
file_read
file_list
grep_search
file_write
shell_readonly
shell_command, gated
memory_search
memory_get
memory_write_candidate
session_search
```

MVP 不需要浏览器、MCP、复杂代码编辑工具。

## 7. Permission Gate

### 7.1 权限等级

```text
readonly
  不修改外部世界。

workspace_write
  修改当前 workspace 文件。

external_side_effect
  网络请求、发消息、创建 issue、调用外部服务。

dangerous
  删除、移动、大范围写入、执行任意 shell。
```

### 7.2 Gate 结果

```text
allow
deny
ask_user
rewrite_required
```

### 7.3 Action Boundary 记忆

PermissionGate 必须读取 `mem:ActionBoundary`。

例如：

```text
用户曾说不要自动提交代码。
用户曾说这个项目只分析设计，不改代码。
用户只允许 readonly 调研。
```

这些边界来自 JSON-LD memory projection，而不是硬编码在 prompt 里。

## 8. MemoryRuntime 接入

### 8.1 MemoryRuntime 接口

```python
class MemoryRuntime:
    async def start_session(self, session: SessionContext) -> None: ...
    async def resolve_scopes(self, state: LoopState, user_input: str) -> ScopeResolution: ...
    async def project(self, state: LoopState, budget: int) -> MemoryProjection: ...
    async def search(self, intent: RetrievalIntent) -> list[MemorySearchResult]: ...
    async def get(self, memory_id: str) -> JsonLdMemoryDocument: ...
    async def expand(self, memory_id: str, relation: str, hops: int) -> MemorySubgraph: ...
    async def observe_turn(self, state: LoopState) -> list[MemoryCandidate]: ...
    async def write_candidate(self, candidate: MemoryCandidate) -> WriteDecision: ...
    async def compact(self, state: LoopState) -> CompactionResult: ...
    async def end_session(self, state: LoopState) -> None: ...
```

### 8.2 必须插入的生命周期点

```text
session_start:
  create mem:Session

intake:
  create mem:Turn / mem:Message

context_assembly:
  ScopeResolver + ProjectionPlanner + fixed query templates + ProjectionFilter

after_model_call:
  archive assistant message

after_tool_execution:
  create mem:ToolResult
  create mem:Artifact when output is large or must be replayable
  create mem:Evidence candidate when result can support or contradict claims

persistence:
  write Execution Evidence Ledger nodes to GraphStore
  write raw outputs / blobs to ArchiveStore
  run MemoryWriteGate for Claim / Decision candidates

compaction:
  create mem:SessionSummary

session_end:
  consolidation
```

### 8.3 Context Fencing

每个 memory projection item 必须带 source marker：

```text
source = recalled_memory
source_id = mem:...
```

Memory extractor 只允许从本轮新信息中抽取候选记忆，不能把 recalled memory 原样写回。

## 9. Model Provider

### 9.1 接口

```python
class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
```

### 9.2 MVP 支持

第一版只支持：

```text
OpenAI-compatible chat completions
tool calls
stream optional
basic retry
model timeout
```

不做：

```text
多 provider fallback
复杂 reasoning token 管理
模型自动选择
```

## 10. Provider Boundary

借鉴 Hermes，但不照搬 Hermes。

### 10.1 Provider 分类

```text
ModelProvider
ToolProvider
MemoryProvider
ContextProvider
PermissionProvider
```

### 10.2 Provider 不拥有 loop

规则：

```text
Provider 不能调用 AgentRuntime.run_task()
Provider 不能自己推进 turn
Provider 不能直接修改 LoopState
Provider 不能直接写 canonical JSON-LD store
```

它只能：

```text
返回能力
返回候选
返回建议
返回 veto/approval
```

### 10.3 MemoryProvider Adapter

未来接 Mem0 / Honcho / Supermemory / Hindsight 时，接口是：

```python
class ExternalMemoryProvider(Protocol):
    async def prefetch(self, query: str, scopes: list[str]) -> list[ExternalMemoryItem]: ...
    async def sync_turn(self, turn: TurnArchive) -> list[ExternalMemoryCandidate]: ...
```

返回值必须进入：

```text
ExternalMemoryCandidate -> normalize -> MemoryCandidate -> MemoryWriteGate -> JSON-LD
```

## 11. Context Builder

### 11.1 输入

```text
system prompt
developer/runtime policy
current user message
recent messages after compact boundary
memory projection
tool specs
budget information
```

### 11.2 输出

```text
ModelRequest
```

### 11.3 Prompt 结构

建议顺序：

```text
1. System identity and operating rules
2. Runtime safety policy
3. Memory projection
4. Current task/session state
5. Available tools
6. Recent conversation
7. Current user input
```

Memory projection 中必须保留 `@id`，便于精确读取。

## 12. Budget Manager

MVP 至少管理：

```text
max_turns
max_tool_calls
max_context_tokens
max_output_tokens
max_session_duration
max_consecutive_failures
```

Budget 触发动作：

```text
compact
reduce tool result
enter recovery mode
ask user
finish with partial result
abort
```

## 13. Recovery

### 13.1 Model Recovery

触发：

```text
JSON parse failed
tool call schema invalid
model timeout
provider error
context too long
max output reached
```

策略：

```text
retry with repair prompt
reduce context
compact
switch to no-tool response
ask user
fail gracefully
```

### 13.2 Tool Recovery

触发：

```text
tool not found
schema validation failed
permission denied
tool timeout
same tool failed repeatedly
```

策略：

```text
append tool error observation
suggest alternative tool
enter recovery mode after N failures
stop repeating same tool
ask user if permission is required
```

### 13.3 Recovery Mode

进入 recovery mode 后，下一次 model request 应包含：

```text
recent failure summary
forbidden repeated action
available alternatives
budget remaining
```

## 14. Persistence

### 14.1 SessionArchive

必须保存：

```text
session metadata
turns
messages
model requests metadata
model responses
tool calls
tool results
events
compaction boundaries
memory candidates
memory write decisions
```

### 14.2 Canonical JSON-LD Store / GraphStore

GraphStore 保存两类 JSON-LD/RDF 对象：

```text
Execution Evidence Ledger
  execution provenance + claim lifecycle。
  例如 Turn / ToolResult / Artifact / Evidence / Claim / Decision。

Durable Semantic Memory
  会影响未来行动和召回的长期语义记忆。
  例如 Claim / Decision / UserPreference / ActionBoundary / Observation。
```

`Turn / ToolResult / Artifact / Evidence` 属于 runtime provenance，可以由 runtime 按固定 schema 直接追加写入，但必须是 append-only、带 scope、带 createdFrom，且不能直接进入 Prompt Projection 作为行动结论。

`Claim / Decision` 是结论节点。它们可以参与 Execution Evidence Ledger 的 supports / contradicts / supersedes 链路，但必须由 `MemoryWriteGate` 审核后才能成为 active claim 或 durable decision。

写入后必须形成两层表示：

```text
DocumentStore
  保存 canonical JSON-LD document。

GraphStore
  保存由 JSON-LD expansion 得到的 RDF quads，用于 SPARQL 查询。
```

MVP 中 GraphStore 的执行层只要求落地 `Execution Evidence Ledger`：

```text
mem:Turn
mem:ToolResult
mem:Artifact
mem:Evidence
mem:Claim
mem:Decision
```

暂不要求维护完整 task / plan / role handoff graph。

SessionArchive 保存历史，不等于长期记忆。

### 14.3 ArtifactStore

大文件、大工具结果、大网页内容不进 prompt。

保存为：

```text
artifact @id
summary
mime/type
source
storage path
hash
```

## 15. Compaction MVP

### 15.1 触发条件

```text
context token > 70%
tool result total > threshold
messages > threshold
before session end
before provider context overflow
```

### 15.2 输出

```text
mem:SessionSummary
compact_boundary event
updated messages list
candidate durable memories
```

### 15.3 SessionSummary 内容

```text
current task
completed steps
pending steps
important decisions
tool results by @id
errors and corrections
action boundaries
open questions
```

## 16. Hook System

MVP hooks 使用 Hollywood Principle。

### 16.1 Hook 点

```text
on_session_start
on_turn_start
before_context_assembly
after_context_assembly
before_model_call
after_model_call
before_tool_execution
after_tool_execution
before_persistence
after_persistence
before_compaction
after_compaction
on_session_end
on_error
```

### 16.2 Hook 返回

```text
continue
modify
veto
abort
request_user
```

### 16.3 Hook 限制

Hook 不能：

```text
直接推进 loop
直接写 canonical memory
执行危险副作用
无限阻塞
吞掉错误
```

## 17. MVP 文件结构

建议 Python 目录：

```text
agent_runtime/
  runtime.py
  loop_state.py
  lifecycle.py
  events.py
  context_builder.py
  budget.py
  recovery.py
  session_store.py
  artifact_store.py
  hooks.py

  models/
    base.py
    openai_compatible.py

  tools/
    base.py
    registry.py
    executor.py
    permissions.py
    builtin_file.py
    builtin_shell.py

  memory/
    runtime.py
    scope_resolver.py
    projection.py
    write_gate.py
    jsonld_store.py
    graph_store.py
    compaction.py

  providers/
    base.py
    external_memory.py
```

## 18. MVP 验收标准

### 18.1 Runtime

```text
test_single_turn_answer
  无工具普通回答。

test_tool_call_loop
  模型调用 file_read，工具结果进入下一轮。

test_finish_after_tool
  工具执行后模型能完成任务。

test_max_turns
  超过 max_turns 优雅结束。
```

### 18.2 Tool

```text
test_readonly_tools_parallel
  多个 readonly 工具并发执行。

test_write_tools_serial
  写工具串行执行。

test_dangerous_tool_requires_permission
  dangerous tool 触发 PermissionGate。

test_tool_failure_recovery
  同一工具连续失败后停止重复。
```

### 18.3 Memory

```text
test_scope_resolved_before_prompt
  prompt 构建前已完成 scope resolve。

test_memory_projection_in_prompt
  memory projection 进入 prompt 且保留 @id。

test_recalled_memory_not_rewritten
  context fencing 防止 memory echo。

test_memory_candidate_gate
  候选记忆必须经过 write gate。
```

### 18.4 Compaction

```text
test_compaction_boundary
  压缩后 messages 保留 compact boundary。

test_session_summary_jsonld
  compaction 生成 mem:SessionSummary。

test_tool_result_artifact
  大工具结果转 artifact，不直接塞 prompt。
```

### 18.5 Event Stream

```text
test_events_order
  session_started -> turn_started -> model_completed -> session_completed。

test_tool_events
  tool_call_started/tool_call_completed 成对出现。

test_error_event
  失败时发出 on_error/session_failed。
```

## 19. 第一版开发顺序

### Phase 1: Skeleton

```text
LoopState
EventBus
SessionStore
ModelProvider mock
AgentRuntime.run_task()
```

目标：无工具单轮对话能跑通。

### Phase 2: Tool Loop

```text
ToolSpec
ToolRegistry
ToolExecutor
PermissionGate
file_read / grep_search / file_write
```

目标：模型能调用工具，工具结果能进入下一轮。

### Phase 3: Memory Integration

```text
ScopeResolver
ExecutionEvidenceLedger
ToolResult / Artifact / Evidence / Claim / Decision
MemoryProjection
MemoryWriteGate
SessionArchive -> JSON-LD message
memory_search / memory_get
```

目标：prompt 中出现 JSON-LD memory projection，且不会 memory echo。

### Phase 4: Compaction

```text
BudgetManager
CompactionManager
SessionSummary
ArtifactStore
```

目标：长会话能压缩，工具大结果能被 artifact 化。

### Phase 5: Provider Adapters

```text
OpenAI-compatible ModelProvider
ExternalMemoryProvider base
optional Mem0/Honcho mock adapter
```

目标：provider 能接入，但不能拥有 loop。

## 20. 设计取舍

### 20.1 为什么不用通用 agent 框架

因为本项目的核心不是“让 LLM 调工具”，而是：

```text
JSON-LD memory object
RDF quads
SPARQL recall
scope-aware projection
memory governance
compaction/consolidation
```

通用 agent 框架会把主 loop 的关键节点藏起来，迫使我们把 JSON-LD 记忆系统做成外挂。这正是需要避免的。

### 20.2 为什么要手写 loop

手写 loop 可以保证：

```text
每个生命周期点可控
MemoryRuntime 是一等公民
工具权限和并发策略可控
压缩和归档可控
事件流可控
provider 只是 adapter
```

### 20.3 为什么仍然保留 provider 接口

不使用 agent 框架，不等于什么都自己写。

可以使用：

```text
OpenAI / Anthropic SDK
MCP SDK
rdflib / pyld
SQLite / Postgres
embedding provider
external memory provider
```

但这些都是能力插件，不是 runtime 主人。

## 21. 一句话总结

MVP 的架构原则：

```text
OpenClaw 给生命周期。
Claude Code 给内层循环工程。
Hermes 给 provider 边界。
JSON-LD memory 给系统灵魂。

主 loop 必须手写，因为记忆不是附属能力，而是 Agent Runtime 的内核。
```
