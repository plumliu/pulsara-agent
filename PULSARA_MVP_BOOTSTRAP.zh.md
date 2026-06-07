# Pulsara MVP 项目骨架说明

## 状态标注

```text
Current
  jsonld/ ontology/ event/ message/ llm adapter/ settings/ ExecutionEvidenceLedger 已有代码实现

Next
  main agent loop
  ProjectionEngine MVP
  summary provider / flash 异步摘要

Target
  完整多层记忆 runtime
  更强的 graph recall / SPARQL / Oxigraph backend
  后台 curator / consolidation pipeline
```

Pulsara 是一个以后端为核心的 Python agent runtime。当前阶段不搭前端，不先接 FastAPI，不先做完整 provider 生态，也不先实现宏大的 Task Graph。MVP 的目标有两个：先证明工具执行结果可以稳定地进入 JSON-LD 记忆系统，并形成可追溯的证据链；同时钉住 AgentScope-like 的 `AgentEvent` / message reducer 地基，让后续 main loop、LLM streaming、tool execution、MemoryWriteGate、ProjectionEngine 都站在同一条可回放事件流上。

## 设计取向

Pulsara 不直接复刻 Claude Code、Hermes 或 Gliding Horse，而是吸收它们最适合本项目的部分。

Claude Code 值得借鉴的是手写 main loop、明确的 runtime state、工具执行边界、side query / compaction / budget 思想。它的工程形态不是“把 agent 框架套上去”，而是自己控制 query、state、tools、memory、entrypoints 这些边界。Pulsara 也应该保持这种可控性。

Hermes 值得借鉴的是 provider / registry 边界：memory provider、model provider、tool provider 都应该是 adapter，不拥有主循环。但 Hermes 的插件生态不应该在 MVP 阶段照搬，否则 Pulsara 会过早变成“记忆功能博物馆”。LLM 层也是同理：我们吸收 provider boundary，而不是复制一个完整 provider marketplace。

Gliding Horse 值得借鉴的是 JSON-LD + 多级记忆的愿景，但 Pulsara 不能只做 JSON-LD 套壳。进入 GraphStore 的对象必须保留可查询的语义关系，而不是把一段 summary 包在 `@context` 里。

## MVP 物理边界

MVP 只有四个物理对象。

`LoopState` 是 Working Context Cache。它保存当前 loop 的临时状态、预算、当前 scope、当前 projection。它不是事实源，也不长期保存。

`GraphStore` 是 JSON-LD/RDF fact layer。当前实现是 in-memory JSON-LD document store，后续 Oxigraph / SPARQL 必须藏在这个边界之后，调用方不直接依赖具体数据库。

`ArchiveStore` 保存大文本、大工具输出、原文 blob。GraphStore 只保存它的 `@id`、hash、summary 和 metadata。

`ProjectionEngine` 以后负责把 GraphStore / ArchiveStore 的查询结果裁剪成 prompt view。它是 view，不是新的数据库。当前 MVP 还没有实现它。

## Event / Message 边界

Pulsara 当前采用统一 `AgentEvent`，不再暴露单独的公共 `LLMEvent` 协议。事件层是 runtime 的 append-only 操作日志，消息、UI、工具结果入库、记忆维护都应该从事件派生。

事件对象使用 Pydantic，因为它们是跨 runtime、测试、日志、未来 API/UI 的边界对象，需要稳定序列化和校验。Python 代码里仍然使用 typed enum，JSON 边界再序列化成字符串。

当前事件分为五组：

```text
reply/model/content/tool lifecycle
human-in-the-loop / external execution
memory write / maintenance
projection requested / ready / failed
custom extension
```

`InMemoryEventLog` 负责给事件分配 sequence，并支持按 `run_id` / `turn_id` / `reply_id` 过滤。`MessageReducer` 负责把一个 `reply_id` 的事件回放成 `Msg`：text/thinking/tool call/tool result 都是 message block。

重要边界：不是每个 event 都进入 GraphStore。事件流是 runtime log；GraphStore 只接收经过 gate 或 ledger 提升后的语义事实。Projection 是 view/event，不是 canonical memory。

## LLM 适配层边界

Pulsara 的 LLM 层采用两个模型槽位，而不是让主循环到处感知具体模型名。

`pro` 是主推理模型，负责复杂规划、主回合生成、关键判断。

`flash` 是轻量模型，后续用于 projection、compaction、摘要、候选记忆提取等低风险任务。这个命名是 Pulsara 自己的用户界面，不直接绑定 OpenAI / Anthropic / Google 任意一家厂商的产品线。

用户侧只需要提供四个值：

```text
PULSARA_API_KEY
PULSARA_BASE_URL
PULSARA_PRO_MODEL
PULSARA_FLASH_MODEL
```

当前第一版只实现 OpenAI Responses-compatible adapter。它负责把 Pulsara 内部的 `LLMContext`、`LLMMessage`、`ToolSpec`、`ModelRole` 转成 Responses payload，再把 provider response / event 翻译成 Pulsara 自己的 `AgentEvent`：

```text
MODEL_CALL_START / MODEL_CALL_END
TEXT_BLOCK_START / TEXT_BLOCK_DELTA / TEXT_BLOCK_END
THINKING_BLOCK_START / THINKING_BLOCK_DELTA / THINKING_BLOCK_END
TOOL_CALL_START / TOOL_CALL_DELTA / TOOL_CALL_END
RUN_ERROR
```

这个设计的关键不是“现在支持很多厂商”，而是先固定内部协议。main loop 以后只消费 Pulsara `AgentEvent` stream，不直接消费 `response.output_text.delta` 或 Anthropic / Google 的事件名。OpenAI、Anthropic、Google 等多种厂商格式必须分别放到独立 adapter 目录中，例如：

```text
llm/adapters/openai/responses.py
llm/adapters/anthropic/...
llm/adapters/google/...
```

这样后续新增 provider 时，只新增“翻译层”，不重写主循环、记忆系统或工具执行边界。

## JSON-LD 与 Ontology 边界

JSON-LD 是 Pulsara 的一等公民，不属于 memory 子模块。当前实体层分成三部分：

`jsonld/` 提供通用对象语言：`IRI`、`Namespace`、`Term`、`NodeRef`、`JsonLdEntity` 和 JSON-LD 序列化工具。这里不应该知道 `Claim`、`Skill` 或 `Plugin`。

`ontology/` 保存 Pulsara 官方 ontology。当前只有 `ontology.memory`，未来可以扩展 `ontology.skill`、`ontology.plugin`。节点类型、关系谓词、状态枚举、来源权威、验证状态都从这里引用。

`memory/entities/` 保存会真正进入图的 typed entity。每个实体自己声明 `TYPE`，自己负责 `to_jsonld()` 字段形状。`ledger.py` 只编排流程，不再手写万能 JSON-LD dict。

这条约束比代码整洁更重要。Pulsara 的 JSON-LD 层要避免语义漂移，就必须保证每个内置语义概念只有一个官方来源。否则后续 GraphStore / Oxigraph 中会出现多个拼写不同但语义相同的谓词或状态，SPARQL 查询也会随之失效。

## 第一条最小闭环

MVP 先实现 Execution Evidence Ledger：

```text
Turn -> ToolResult -> Artifact
ToolResult -> Evidence
Evidence -> Claim / Decision
```

`Turn`、`ToolResult`、`Artifact`、`Evidence` 是 runtime provenance。它们记录“发生过什么”，可以由 runtime 追加写入。

`Claim` 和 `Decision` 是 conclusion node。它们会影响未来召回和行为，因此必须经过 `MemoryWriteGate`。MVP 里 gate 只做保守规则：空 statement 拒绝；没有 evidence 且不是用户显式指令的 claim 进入 review；最终 `confidenceLevel` 由 source authority 和 verification status 计算，而不是让 LLM 自报浮点置信度。

`ExecutionEvidenceLedger` 仍然负责把 `ToolResult -> Artifact -> Evidence -> Claim` 写入 JSON-LD GraphStore，但现在也支持从 `EventLog.replay(reply_id)` 还原 `ToolResultBlock` 后再入库。这样工具执行、消息回放、记忆入库共享同一条事件事实源。

## 当前目录结构

```text
src/pulsara_agent/
  jsonld/
    entity.py        # JsonLdEntity base class
    iri.py           # IRI value object
    namespace.py     # Namespace helper
    node_ref.py      # JSON-LD node references
    term.py          # compact JSON-LD term
    value.py         # JSON-LD serialization helpers
  ontology/
    memory.py        # Pulsara built-in memory ontology
  runtime/
    state.py          # LoopState，短期 loop 状态
  event/
    events.py         # AgentEvent / EventContext / memory/projection events
    log.py            # InMemoryEventLog，append-only runtime event log
  message/
    blocks.py         # Text/Thinking/Data/ToolCall/ToolResult blocks
    message.py        # Msg / Usage / message constructors
    reducer.py        # MessageReducer，event -> Msg replay
  llm/
    config.py         # LLMConfig，API key / base_url / pro_model / flash_model
    factory.py        # 默认 runtime 构造
    input.py          # provider-neutral model input / tool spec
    models.py         # ModelRole.PRO / ModelRole.FLASH
    registry.py       # LLMTransportRegistry
    request.py        # LLMContext / LLMOptions
    runtime.py        # role -> model -> transport
    transport.py      # LLMTransport protocol
    usage.py          # provider token usage
    adapters/
      mock.py
      openai/
        responses.py  # OpenAI Responses-compatible transport
  tools/
    base.py           # ToolCall / ToolExecutionResult / Tool protocol
    registry.py       # 收敛版 ToolRegistry
  memory/
    archive.py        # ArchiveStore
    graph.py          # GraphStore boundary
    ledger.py         # ExecutionEvidenceLedger
    entities/
      artifact.py     # Artifact JSON-LD entity
      claim.py        # Claim JSON-LD entity
      evidence.py     # Evidence JSON-LD entity
      tool_result.py  # ToolResult JSON-LD entity
      turn.py         # Turn JSON-LD entity
    records.py        # runtime return records
    write_gate.py     # MemoryWriteGate
  cli.py              # pulsara CLI
tests/
  test_event_message_system.py
  test_execution_evidence_ledger.py
  test_llm_runtime.py
  test_real_llm_integration.py
  test_settings.py
```

## 暂时不做什么

MVP 阶段暂时不做完整 Task Graph、后台 curator、真实 Oxigraph、SPARQL planner、FastAPI 服务、前端、插件市场、长期定时维护任务、多厂商 provider marketplace。

LLM 层当前只做 OpenAI Responses-compatible 的最小真实 HTTP 调用和 mock 测试流；还不做完整 SSE streaming、自动重试、速率限制、provider 特性协商、复杂 tool result 回填协议。

这些都可以做，但必须等第一条证据闭环稳定之后再加。否则我们会失去最重要的判断标准：Pulsara 的记忆系统到底是在保留语义，还是只是在给普通 JSON 包一层 JSON-LD 外壳。
