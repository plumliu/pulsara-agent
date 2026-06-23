# Tool Result Artifact PR1 Implementation Plan

本文把 Terminal P1 中的 PR1 单独拆出来重新规划。目标不是 terminal 专用输出文件迁移，而是直接落地 v2：统一 Tool Result Artifact 协议、`ArtifactStore` 读写扩展、`tool_result_artifacts` join table、executor 通用归档、terminal 全量输出归档、`artifact_read`、以及 memory ledger dedup。

关键产品决策：

- 直接删除 `.pulsara/terminal-output/`。
- 直接删除模型可见的 `full_output_ref`。
- 不做兼容双写。
- 不把 terminal 称为“最重”或特殊长期路径；terminal 只是第一个暴露该问题的工具。
- 未来 search / crawl / browser scrape / grep / test report 等长工具结果共享同一协议。

## 0. 当前代码事实

### 0.1 已存在的 artifact store

当前已有：

- `src/pulsara_agent/memory/foundation/protocols.py`
  - `ArtifactStore.put_text(...)`
  - `ArtifactStore.get_text(...)`
- `src/pulsara_agent/memory/artifacts/archive.py`
  - `InMemoryArchiveStore`
- `src/pulsara_agent/memory/artifacts/postgres_archive.py`
  - `PostgresArtifactStore`
- `src/pulsara_agent/storage/postgres_schema.py`
  - `artifacts` 表，已有 `text_body` / `binary_body` / `media_type` / `digest` / `size_bytes` / `stored_at` / `metadata` / `session_id` / `run_id`。
- `src/pulsara_agent/runtime/wiring.py`
  - durable runtime 已创建 `PostgresArtifactStore`。

这说明“Artifact 存储”不是新概念；PR1 应接入现有三层模型，而不是发明 terminal 私有存储。

### 0.2 terminal 完整输出的真实位置

当前 terminal 的完整输出不是 `ToolExecutionResult.output`。

实际路径：

- `OutputAccumulator` 持有 redacted output。
- `snapshot_process()` 调 `finalize_output(max_chars)`，返回的是 preview。
- `ToolExecutionResult.output` 包含的 terminal JSON 也是 preview。
- 完整输出今天通过 `OutputAccumulator.artifact_path` 写到 `.pulsara/terminal-output/<process_id>.txt`。

因此不能简单在 `ToolExecutor.execute()` 后把 `result.output` 写入 ArtifactStore。对 terminal 来说，那只会归档已经截断的 preview，完整输出会丢失。

### 0.3 ledger 已经在归档 tool output

`ExecutionEvidenceLedger.record_tool_result()` 已经有一条归档路径：

- 阈值：`LARGE_OUTPUT_THRESHOLD = 2_000`
- 若 output 超阈值，调用 `self.archive.put_text(artifact_id, output)`
- artifact id 当前是 `artifact:{uuid4()}`
- 当前没有传 `session_id/run_id`

PR1 如果新增 executor 归档却不收口 ledger，会产生两套 artifact：

- ID 不一致。
- scoping 不一致。
- 字节不一定一致。
- PostgresArtifactStore 的 idempotency 校验可能在确定性 ID 下报冲突。

PR1 必须把 ledger 归档改成复用 runtime tool artifact，而不是并行再写。

### 0.4 executor 现在拿不到 archive

`ToolExecutor` 当前只有：

- `registry`
- `record_event`

`RuntimeSession.create_tool_executor()` 也没有 archive。`archive` 在 `RuntimeWiring` 层。PR1 若要 executor 通用归档，必须把 artifact service 从 wiring 穿到 runtime session / executor，并确保 `_stream_tool_batch_events()` 里临时创建的 executor 也使用同一服务。

### 0.5 事件流才是权威结果

模型可见结果不是从 `ToolExecutionResult` 返回值读取的。实际路径是：

- executor 执行期间发 `ToolResultTextDeltaEvent`。
- executor 最后发 `ToolResultEndEvent`。
- `agent.py` 用 `_tool_result_from_event_slice(batch_events, call.id)` 从事件重建 `ToolResultBlock`。
- `task.result()` 只传播异常，`ToolExecutionResult` 返回值本身会被丢弃。

因此 artifact refs 必须进入事件流。仅把 refs 放进返回值 metadata 对模型和 ledger 都无效。

### 0.6 artifact read 现在无法 owner 校验

`PostgresArtifactStore.get_text(blob_id)` 只按 artifact id 读取，没有 owner 参数。 durable runtime 下所有 session 共享同一张 artifacts 表，如果 `artifact_read` 直接调用 `get_text(id)`，模型可以猜测任意 artifact id 读取。

PR1 必须新增 owner 校验路径。建议事实来源为 `tool_result_artifacts` join table，并在 `ArtifactStore.read_text/get_info/get_bytes` 增加 `session_id` 校验参数作为纵深防御。

## 1. 目标

PR1 完成以下能力：

- 所有长 tool result 使用统一 artifact refs。
- 支持一次 tool call 产生多个 artifacts。
- terminal 完整输出进入 ArtifactStore，不再写 `.pulsara/terminal-output/`。
- 通用 tool 大内联输出由 executor 归档。
- terminal 通过 artifact candidates 把完整输出交给 executor，避免只归档 preview。
- executor 是唯一归档者；工具只负责提供 candidates，不直接写 ArtifactStore / join table。
- 新增 `artifact_read` read-only tool。
- artifact read 只允许读取当前 runtime session 拥有的 artifact。
- ledger 复用 runtime artifact，不再重复归档。
- 模型可见 artifact ref 尽量短，避免协议本身膨胀上下文。

## 2. 非目标

本 PR 不做：

- S3 / object storage backend。
- artifact UI viewer。
- stdout/stderr 真分流。
- browser/search/crawl 工具本身。
- 自动把 artifact 内容写入 durable memory。
- 完整 TUI transcript 存储。

## 3. 模型可见协议

### 3.1 精简 ref

模型可见 ref 必须短。不要把 provenance 全塞回上下文。

保留：

- `artifact_id`
- `role`
- `media_type`
- `size_bytes`
- `stored_complete`
- `loss_reason`
- `read_more`

删除到 DB metadata / join table：

- `source.tool_name`
- `tool_call_id`
- `run_id`
- `turn_id`
- `reply_id`
- `digest`
- `encoding`
- per-artifact preview object

模型看到的正文部分是一个 JSON envelope；外层现有 `[tool_result:{name}:{state}]` 前缀保留。

```json
{
  "output_preview": "short redacted preview shown inline",
  "output_truncated": true,
  "artifacts": [
    {
      "artifact_id": "artifact:tool-result:run-abc:call-123:output:0",
      "role": "combined_output",
      "media_type": "text/plain; charset=utf-8",
      "size_bytes": 124532,
      "stored_complete": true,
      "read_more": {"tool": "artifact_read"}
    }
  ]
}
```

下面是 `artifacts[]` 内单个 ref 在 artifact 自身不完整时的形状；它不是第二种顶层 payload：

```json
{
  "artifact_id": "artifact:tool-result:run-abc:call-123:output:0",
  "role": "combined_output",
  "media_type": "text/plain; charset=utf-8",
  "size_bytes": 20971520,
  "stored_complete": false,
  "loss_reason": "artifact_size_limit",
  "read_more": {"tool": "artifact_read"}
}
```

### 3.2 字段语义

- `output_preview`：短文本，已经 redacted，直接用于模型判断下一步。
- `output_truncated`：preview 是否短于完整 tool result。
- `artifacts`：artifact refs 数组。当前 terminal 通常只有一个，未来 crawl/search 可有多个。
- `artifact_id`：读取 artifact 的唯一参数。
- `role`：artifact 在该 tool result 中的角色，例如 `output`、`combined_output`、`json`、`page_html`、`screenshot`。
- `media_type`：真实存储 media type。
- `size_bytes`：存储后字节数。
- `stored_complete`：artifact 是否完整保存了该 role 的内容。它和 `output_truncated` 不同。
- `loss_reason`：仅当 `stored_complete=false` 时出现。
- `read_more`：只提示使用哪个 tool；不重复 `artifact_id`，避免冗余。

无 artifact 时保持旧格式：只渲染 clipped tool result body，不包 envelope。

### 3.3 为什么不显示 digest/source

`digest` 是 provenance 字段，模型一般不会用它决策。

`source` 信息已经存在于 event/run/tool_call 信封和 join table 中。把 run_id/turn_id/tool_call_id 再吐给模型只会膨胀上下文。

如需调试，可在 inspect / host API 展示完整 metadata，不进入普通 tool result context。

## 4. 存储与 schema

### 4.1 ArtifactStore 协议扩展

当前协议太窄：

```python
def put_text(...)
def get_text(blob_id: str) -> str
```

PR1 增加：

```python
@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: str
    media_type: str
    digest: str
    size_bytes: int
    stored_at: str
    created_at: str | None
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class ArtifactTextSlice:
    artifact: ArtifactRecord
    text: str
    offset_chars: int
    returned_chars: int
    total_chars: int | None
    has_more: bool


class ArtifactStore(Protocol):
    def put_text(..., session_id: str | None = None, run_id: str | None = None, metadata: dict[str, Any] | None = None) -> ArtifactWriteResult: ...
    def put_bytes(..., session_id: str | None = None, run_id: str | None = None, media_type: str, metadata: dict[str, Any] | None = None) -> ArtifactWriteResult: ...
    def get_info(self, blob_id: str, *, session_id: str | None = None) -> ArtifactRecord: ...
    def read_text(self, blob_id: str, *, session_id: str | None = None, offset_chars: int = 0, max_chars: int = 20000) -> ArtifactTextSlice: ...
    def get_text(self, blob_id: str, *, session_id: str | None = None) -> str: ...
    def get_bytes(self, blob_id: str, *, session_id: str | None = None) -> bytes: ...
```

Read-side `session_id` 是必须的。`None` 只允许内部 trusted code 使用；`artifact_read` 必须传当前 runtime session id。

### 4.2 Postgres artifacts 表

不删除现有字段。

新增建议：

```sql
CREATE INDEX IF NOT EXISTS idx_artifacts_session_id ON artifacts(session_id);
```

暂不把 `kind/role/tool_call_id` 提升为 `artifacts` 表列。它们属于 tool result 关系，应由 join table 表达。

### 4.3 Join table

直接实现 v2 join table：

```sql
CREATE TABLE IF NOT EXISTS tool_result_artifacts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    turn_id TEXT NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
    reply_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    role TEXT NOT NULL DEFAULT 'output',
    ordinal INTEGER NOT NULL DEFAULT 0,
    media_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    stored_complete BOOLEAN NOT NULL DEFAULT TRUE,
    loss_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (run_id, tool_call_id, role, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_tool_result_artifacts_session_id
    ON tool_result_artifacts(session_id);

CREATE INDEX IF NOT EXISTS idx_tool_result_artifacts_artifact_id
    ON tool_result_artifacts(artifact_id);
```

这张表承担：

- artifact ownership for read。
- tool result 到多 artifact 的映射。
- role / ordinal 排序。
- ledger dedup 查找。

`tool_execution_records.artifact_id` 暂不删除，但不再作为新协议的主路径。可继续填 primary artifact 以兼容现有 graph/query，但 join table 是权威多 artifact 索引。

ordinal 规则：

- `ordinal` 等于 `artifact_candidates` 元组中的索引。
- 若未来一个 candidate 展开成多个 artifact，则使用 role 内序号，但必须保持 deterministic。
- `ToolResultBlock.artifacts` 按 `ordinal` 升序排列。
- ledger 取 `artifacts[0]` 作为 primary artifact 时才有稳定含义。

## 5. Runtime 数据模型

### 5.1 Artifact ref

```python
class ToolResultArtifactRef(BaseModel):
    artifact_id: str
    role: str
    media_type: str
    size_bytes: int
    stored_complete: bool = True
    loss_reason: str | None = None

    def to_model_payload(self) -> dict[str, object]: ...
```

`ToolResultArtifactRef` 必须是 Pydantic `BaseModel`，因为它会嵌进 `ToolResultEndEvent` 和 `ToolResultBlock`，需要随 event log dump/load round-trip。

`to_model_payload()` 只输出精简字段和 `read_more`。

### 5.2 Artifact candidate

为了同时支持 terminal 全量输出和通用 executor fallback，扩展 `ToolExecutionResult`：

```python
@dataclass(frozen=True, slots=True)
class ToolResultArtifactCandidate:
    role: str
    media_type: str
    text: str | None = None
    data: bytes | None = None
    redacted: bool = True
    stored_complete: bool = True
    loss_reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    call_id: str
    tool_name: str
    status: ToolResultState
    output: str
    metadata: dict[str, Any] = field(default_factory=dict)
    artifact_candidates: tuple[ToolResultArtifactCandidate, ...] = ()
```

规则：

- terminal 把完整 combined output 放进 `artifact_candidates`，`output` 只放 preview JSON。
- 普通工具不需要知道 artifact；若 `output` 超阈值且没有 candidates，executor fallback 将 `output` 作为 role=`output` 归档。
- 未来 search/crawl 可主动返回多个 candidates。

### 5.3 Artifact service

新增：

- `src/pulsara_agent/runtime/tool_artifacts.py`

职责：

- `archive_candidates(...)`
- `archive_fallback_output_if_needed(...)`
- 写 `ArtifactStore`。
- 写 `tool_result_artifacts` join table。
- 生成 model-visible refs。
- 返回 refs 供 executor 写入 `ToolResultEndEvent.artifacts`；context renderer 负责把 refs 渲染给模型。

建议接口：

```python
class ToolResultArtifactService:
    def process_result(
        self,
        result: ToolExecutionResult,
        *,
        event_context: EventContext,
        tool_call: ToolCall,
    ) -> tuple[ToolExecutionResult, tuple[ToolResultArtifactRef, ...]]: ...
```

返回：

- 可能被压缩/替换成 preview 的 `ToolExecutionResult`。
- 将挂到 `ToolResultEndEvent.artifacts` 的结构化 refs。

注意：refs 的权威通道是事件，不是 `ToolExecutionResult.metadata`。metadata 可用于调试，但 ledger 不应依赖它。

## 6. Wiring

### 6.1 RuntimeSession 持有 archive

当前 `RuntimeWiring.archive` 在 `RuntimeSession` 外层。PR1 需要：

- `RuntimeSession.archive: ArtifactStore`
- `RuntimeSession.tool_result_artifacts: ToolResultArtifactIndex`
- `RuntimeSession.create_tool_executor()` 把 artifact service 传给 `ToolExecutor`。

durable wiring 中，`archive = PostgresArtifactStore(...)` 应在构造 `RuntimeSession` 前准备好，或构造后注入。

in-memory runtime 中，默认使用 `InMemoryArchiveStore` + in-memory artifact index。每个 host session 应有独立 wiring / runtime session / in-memory store；这是结构性隔离。真正验证“共享 backend 下 session A 不能读 session B artifact”的测试必须使用 Postgres，或显式构造 shared in-memory store/index 单测来压 artifact_read 的校验谓词。

### 6.2 ToolExecutor 使用 artifact service

`ToolExecutor` 增加字段：

```python
artifact_service: ToolResultArtifactService | None = None
```

执行流程：

1. 工具执行得到 raw `ToolExecutionResult`。
2. 若 `artifact_service` 存在，调用 `process_result(...)`，得到 `(processed_result, artifact_refs)`。
3. 对非流式工具：在 emit delta 前使用 `processed_result.output`。
4. 对流式工具：若 `streamed_output_complete=True`，不再补发 text delta；已经发出的 bounded preview 保持原样。
5. emit `ToolResultEndEvent(..., artifacts=artifact_refs)`。

注意 `_stream_tool_batch_events()` 里会临时创建 executor：

```python
executor = ToolExecutor(
    registry=self.tool_executor.registry,
    record_event=self.runtime_session.make_thread_recorder(state=state),
)
```

这里必须把同一个 `artifact_service` 带过去，否则批量工具路径会绕过归档。

### 6.3 Event schema 与 context 渲染

新增结构化事件字段：

```python
class ToolResultEndEvent(EventBase):
    ...
    artifacts: list[ToolResultArtifactRef] = Field(default_factory=list)


class ToolResultBlock(BaseModel):
    ...
    artifacts: list[ToolResultArtifactRef] = Field(default_factory=list)
```

Assembler 修改：

- `assembler.py` 处理 `ToolResultEndEvent` 时，除了设置 `block.state`，还要设置 `block.artifacts = event.artifacts`。
- `completed_tool_result_from_events()` 自然随事件重建 artifacts。
- 加 event-log dump/load round-trip 测试，确保 persisted old events 没有 artifacts 时默认 `[]`。

Context 渲染要求：

- artifact refs 不能放进 `_tool_result_text()` 内部末尾，也不能用字符串拼接追加在 terminal JSON 后面。
- 当前 `_tool_result_messages()` / `_textual_parts()` 会先调用 `_tool_result_text(block)`，再按 `tool_result_context_chars` 裁剪。
- 如果 refs 跟正文一起裁剪，长 preview 会把 `artifact_id` 裁掉，模型就无法读取 artifact。
- 正确做法：正文先 clip；若没有 artifacts，保持旧 body；若有 artifacts，把 clipped body 作为 `output_preview` 字符串放进 JSON envelope，并把 refs 放在 `artifacts` sibling key。refs 不参与正文预算。
- 现有 `[tool_result:{name}:{state}]` 前缀保留在 envelope 外层，由 `_tool_result_messages()` / `_textual_parts()` 继续添加。
- `output_truncated` 不能因为 artifacts 存在就写死为 `true`。当归档阈值低于 context 预算时，可能出现“已归档但正文未被 clip”的区间；此时 inline preview 已完整展示，`output_truncated` 应为 `false`。
- terminal 的正文当前本身是 JSON；作为 `output_preview` 字符串放进 envelope 后会产生一层 JSON 转义。这是“不 parse tool JSON、不为 terminal 写特殊 merge 逻辑”的明确取舍，可以接受。

建议抽 helper：

```python
def _render_tool_result_body(block: ToolResultBlock, remaining_tool_chars: int) -> tuple[str, int]:
    body = _tool_result_text(block)
    clipped, remaining_tool_chars = _clip_with_remaining(body, remaining_tool_chars)
    if not block.artifacts:
        return clipped, remaining_tool_chars
    primary = block.artifacts[0]
    output_truncated = len(clipped.encode("utf-8")) < primary.size_bytes
    envelope = {
        "output_preview": clipped,
        "output_truncated": output_truncated,
        "artifacts": [artifact.to_model_payload() for artifact in block.artifacts],
    }
    return json.dumps(envelope, ensure_ascii=False), remaining_tool_chars
```

`_tool_result_messages()` 和 `_textual_parts()` 都用同一个 helper，避免双实现漂移。helper 返回的是 body；调用方继续负责加 `[tool_result:{name}:{state}]` 前缀。

## 7. Terminal 生产者

### 7.1 删除本地文件路径

删除：

- `artifact_root`
- `artifact_path`
- `full_output_ref`
- `.pulsara/terminal-output/<process_id>.txt`

涉及：

- `TerminalSession.execute()`
- `spawn_local_process()`
- `TerminalProcessState.full_output_ref`
- `TerminalResult.full_output_ref`
- `TerminalProcessInfo.full_output_ref`
- `TerminalProcessLog.full_output_ref`
- `terminal_result_payload()`
- tool descriptions / tests / prompts。

### 7.2 OutputAccumulator 提供 full redacted text

`OutputAccumulator` 当前已经积累 redacted text。新增：

```python
def full_text(self) -> str: ...
def full_size_bytes(self) -> int: ...
```

`snapshot(max_chars=...)` 继续用于 preview。

第一版可以继续全量驻留内存，因为当前实现本来也如此。后续若要解决超长常驻进程内存，另开 streaming artifact writer PR。

### 7.3 TerminalTool 生成 candidate

terminal 的 `ToolExecutionResult.output` 必须继续是短 preview JSON，但 `artifact_candidates` 必须包含完整 combined output。

策略：

- 命令完成时：
  - `output_preview` = `snapshot(max_output_chars)`
  - artifact candidate = `OutputAccumulator.full_text()`
  - role = `combined_output`
  - media_type = `text/plain; charset=utf-8`
  - redacted = true
- 命令 yielded/running 时：
  - 初始 result 只提供当前 snapshot candidate；是否归档由 executor artifact service 根据阈值决定。
  - 后续完整或更多输出通过 `terminal_process log/poll/wait` 返回 candidate，再由 executor 统一归档。
  - 此时 artifact 语义是“截至该 tool call 时刻的输出快照”。它可以 `stored_complete=true`，因为 artifact 完整保存了这个快照；但它不代表 process 的最终全量输出。
  - 最终或更新后的输出仍需要后续 `terminal_process log/wait` 或 completion note 驱动模型再次读取。
- background completion event 不主动生成 artifact；它只提示模型用 `terminal_process log`，该 tool result 再走统一归档。

### 7.4 Streaming 不关闭

不要关闭 terminal streaming。

原因：

- `_StreamingTerminalJsonBuilder` 已经把 streaming preview 限制在 `max_output_chars`。
- 完整输出在 accumulator。
- 最终 result 通过 candidate 把 full redacted text 交给 executor 归档。

需要确保流式 preview 不再声称 `full_output_ref`，而是最终 `ToolResultBlock` 通过 `ToolResultEndEvent.artifacts` 持有 refs，并由 context renderer 渲染给模型。

## 8. Generic executor producer

当工具没有主动提供 `artifact_candidates`，但 `result.output` 超过阈值时：

- executor fallback 归档整个 `result.output`。
- role = `output`
- media_type = `text/plain; charset=utf-8`，除非 result.metadata 指定。
- `processed_result.output` 可替换为短 preview。artifact refs 不塞进 output 字符串，而是挂在 `ToolResultEndEvent.artifacts`；context renderer 最终呈现为：

```json
{
  "output_preview": "...",
  "output_truncated": true,
  "artifacts": [
    {
      "artifact_id": "...",
      "role": "output",
      "media_type": "text/plain; charset=utf-8",
      "size_bytes": 123456,
      "stored_complete": true,
      "read_more": {"tool": "artifact_read"}
    }
  ]
}
```

不要 parse 工具输出 JSON 来注入 artifacts。第一版统一依赖结构化 `ToolResultEndEvent.artifacts`，由 context renderer 生成上面的模型可见 envelope。

## 9. artifact_read

新增 read-only builtin：

- `src/pulsara_agent/tools/builtins/artifact.py`

Schema：

```json
{
  "artifact_id": {"type": "string"},
  "mode": {"type": "string", "enum": ["text", "info"], "default": "text"},
  "offset_chars": {"type": "integer", "default": 0},
  "max_chars": {"type": "integer", "default": 20000}
}
```

权限：

- read-only。
- `read_only` profile 下允许。
- 不触发 terminal approval。
- 必须按当前 runtime session id 查询 `tool_result_artifacts`，找不到则返回 not_found。
- 读取 ArtifactStore 时也传 `session_id` 做二次校验。
- artifact 不存在与 artifact 属于其他 session 必须返回同一种 `not_found`，不要用错误文案泄漏 artifact 是否存在。

Payload：

```json
{
  "status": "success",
  "artifact_id": "artifact:tool-result:run-abc:call-123:output:0",
  "media_type": "text/plain; charset=utf-8",
  "size_bytes": 124532,
  "offset_chars": 0,
  "returned_chars": 20000,
  "has_more": true,
  "text": "..."
}
```

`mode="info"` 不返回正文，只返回 metadata。

## 10. Ledger dedup

### 10.1 当前问题

`ExecutionEvidenceLedger.record_tool_result()` 现在会再次归档 >2000 字符 output。

PR1 后，event log 中的 tool result 可能已经是 preview + `artifacts` refs。如果 ledger 继续 `put_text()`：

- 会归档 preview，而不是 full artifact。
- 或和 runtime artifact 重复。
- 或在确定性 id 下冲突。

### 10.2 修改方案

`ExecutionEvidencePersistenceHook.after_tool_results()` 或 ledger 层需要识别 tool result 中的 artifact refs。

建议：

- `ToolResultEndEvent.artifacts` 是权威载体。
- `assembler` 把 artifacts 写进 `ToolResultBlock.artifacts`。
- `ExecutionEvidencePersistenceHook` 从 `ToolResultBlock.artifacts` 结构化读取 refs。
- 不 parse tool output JSON。
- 不依赖 `ToolExecutionResult.metadata`，因为返回值不会进入 state。

Ledger 行为：

- 若已有 artifact refs：
  - 不再 `archive.put_text()`。
  - 仍然创建 `Artifact` graph node，节点 id 指向已存在的 runtime artifact id，并填入 `stored_at/digest/summary/scope/event_span`。
  - 写 `ToolResult` graph node，`stored_as` 指向 primary artifact。
  - 若 graph 模型暂时只支持单 artifact，primary 选 `ordinal=0`。
- 若没有 artifact refs 且 output 超过旧阈值：
  - 可保留旧 fallback，但必须传 `session_id/run_id`。

### 10.3 阈值统一

当前 ledger 阈值 2000，tool artifact 阈值应统一配置。

建议新增：

- `ToolResultArtifactOptions.inline_preview_chars`
- `ToolResultArtifactOptions.archive_threshold_chars`
- 默认 archive threshold 与 runtime tool context budget 协调，不再在 ledger 里硬编码 2000。

必须写死的序不变量：

```text
archive_threshold_chars <= tool_result_context_chars
```

原因：context renderer 会按动态预算裁剪 tool result body；artifact service 则在执行期按绝对大小决定是否归档。若 `archive_threshold_chars > tool_result_context_chars`，会出现 `tool_result_context_chars < len(body) <= archive_threshold_chars` 的中间区间：正文会被 context 裁掉，但不会生成 artifact，模型看到的是不可恢复的静默截断。

这个不变量只关闭新协议引入的中间区间。因为 `tool_result_context_chars` 是一轮内多个 tool result 共享的动态剩余预算，即便某个 body 小于 archive 阈值，也仍可能在预算快耗尽时被裁剪；这是 PR1 前已有行为，不由本 PR 一次性解决。

## 11. ID 策略

使用确定性 ID：

```text
artifact:tool-result:{run_id}:{tool_call_id}:{role}:{ordinal}
```

需要 sanitize：

- `run_id`
- `tool_call_id`
- `role`

或者 hash：

```text
artifact:tool-result:{sha256(run_id + tool_call_id + role + ordinal)}
```

建议第一版用可读 ID，并在 tests 中覆盖特殊字符。

PostgresArtifactStore 仍保持 idempotent：

- 同 ID 同内容 OK。
- 同 ID 不同内容 error。

这能抓住重复写入时的真实 bug。

## 12. 实施切分

虽然这是一个 PR1，但内部建议按以下提交顺序实现。

### Step 1：ArtifactStore read API 与 schema

- 增加 `ArtifactRecord` / `ArtifactTextSlice`。
- 增加 `put_bytes/get_bytes/get_info/read_text`。
- `PostgresArtifactStore.get_text/read_text/get_info/get_bytes` 支持 `session_id` owner 校验。
- `InMemoryArchiveStore` 补同样接口。
- `InMemoryArchiveStore.ArchiveBlob` 从 `id/content/digest/stored_at` 扩展出 `media_type/size_bytes/metadata/created_at`，保证 `get_info()` 能返回完整 `ArtifactRecord`。
- 添加 `idx_artifacts_session_id`。
- 添加 `tool_result_artifacts` 表。

### Step 2：ToolResultArtifactIndex / service

- 新增 in-memory + Postgres index。
- 写 join table。
- 生成精简 model refs。
- 支持多 artifact。

### Step 3：ToolExecutionResult artifact candidates

- 扩展 `ToolExecutionResult`。
- 扩展 `ToolExecutor`，执行后调用 artifact service。
- 扩展 `ToolResultEndEvent.artifacts` 和 `ToolResultBlock.artifacts`。
- 更新 assembler，使 artifacts 随 END 事件进入 block。
- 更新 context renderer，正文裁剪后包进 envelope，artifact refs 放进 `artifacts` sibling key，refs 不参与正文预算。
- 更新 `RuntimeSession.create_tool_executor()`。
- 更新 `_stream_tool_batch_events()` 临时 executor。

### Step 4：Terminal hard cut

- 删除 `.pulsara/terminal-output` 写文件。
- 删除 `full_output_ref` 字段和 payload。
- `OutputAccumulator.full_text()`。
- `terminal` / `terminal_process log/poll/wait` 输出 preview + candidates。
- streaming preview 保持 bounded，不关闭 streaming。

### Step 5：artifact_read tool

- 新增 builtin。
- 注册到 core registry。
- read-only policy。
- session owner 校验。

### Step 6：Ledger dedup

- persistence hook 接收 artifact refs。
- ledger 复用 refs，不重复归档。
- fallback 归档传 `session_id/run_id`。

### Step 7：Prompt / docs / tests clean up

- 删除所有 `full_output_ref` 文案。
- 删除所有 `read_file` 读取 terminal output 的指引。
- real smoke 改用 `artifact_read`。

## 13. 测试计划

### 13.1 ArtifactStore contract

- `put_text/get_text/read_text/get_info`。
- `put_bytes/get_bytes/get_info`。
- `read_text` offset/max_chars/has_more。
- Postgres owner 校验：session A 不能读 session B artifact。
- InMemory store 行为一致。

### 13.2 Schema / index

- 写入 `tool_result_artifacts`。
- 同一 run/call/role/ordinal 幂等。
- 多 artifact 同一 call 可按 ordinal 返回。
- `ordinal` 等于 candidate 顺序，`ToolResultBlock.artifacts` 按 ordinal 稳定排序。
- artifact_id 查询必须 scoped by session。

### 13.3 ToolExecutor generic archive

- 短 output 不归档。
- 长 output 归档为 artifact。
- event log 只包含 preview + refs。
- envelope 中的 refs JSON 足够短，不包含 source/digest。
- `_stream_tool_batch_events()` 路径也归档。
- `ToolResultEndEvent.artifacts` dump/load round-trip。
- `ToolResultBlock.artifacts` 可从事件重建。
- 长 preview 触发 context clip 时，artifact refs 仍保留在 envelope 的 `artifacts[]` 中，模型可见 `artifact_id`。

### 13.4 Terminal full output

- 大 terminal output 不创建 `.pulsara/terminal-output`。
- payload 不含 `full_output_ref`。
- artifact_read 读到完整 redacted output，不是 preview。
- secret redaction 在 preview 和 artifact 中都生效。
- streaming terminal preview 仍工作，最终 result 有 artifacts。
- yielded process 完成后，`terminal_process log` 产生 artifact。

### 13.5 artifact_read

- 当前 session 可读。
- 其他 session 不可读。
- artifact 不存在和其他 session artifact 都返回同一种 not_found。
- `mode=info` 不返回正文。
- binary artifact 用 `mode=text` 返回明确错误。
- read-only profile 下可用。
- in-memory 默认 per-session store 测功能路径。
- Postgres 测真实共享 backend 跨 session隔离。
- shared in-memory store/index 单测直接验证 owner predicate。

### 13.6 Ledger dedup

- tool result 已有 artifacts 时 ledger 不再调用 `archive.put_text()`。
- ledger 仍创建 `Artifact` graph node，且 `stored_at` 指向已有 runtime artifact。
- graph `ToolResult.stored_as` 指向 primary artifact。
- fallback path 仍能归档没有 refs 的旧式大输出。
- ledger artifact 写入带 `session_id/run_id`。

### 13.7 Real LLM smoke

新增：

```bash
PULSARA_RUN_REAL_LLM=1 uv run pytest tests/test_real_llm_integration.py::test_real_host_core_tool_result_artifact_protocol -q -s
```

剧本：

1. 让模型运行产生大量输出的 terminal 命令。
2. 断言 tool result 中出现 `artifacts[]`，没有 `full_output_ref`。
3. 模型调用 `artifact_read` 读取 artifact。
4. final text 输出 `PULSARA_TOOL_ARTIFACT_OK`。

## 14. 验证命令

```bash
uv run pytest tests/test_artifact_store_contract.py -q
uv run pytest tests/test_terminal_runtime.py -q
uv run pytest tests/test_tools.py -q
uv run pytest tests/test_permission_policy.py -q
uv run pytest tests/test_host_core.py -q
uv run pytest tests/test_cli_host.py -q
uv run pytest -q
```

Real smoke：

```bash
PULSARA_RUN_REAL_LLM=1 uv run pytest tests/test_real_llm_integration.py::test_real_host_core_tool_result_artifact_protocol -q -s
```

## 15. Open Questions

### 15.1 Artifact max size

需要配置：

- `PULSARA_TOOL_ARTIFACT_MAX_BYTES`
- 默认建议 20 MiB。

超过后：

- 不 kill tool / terminal process。
- artifact 截断并标 `stored_complete=false`。
- `loss_reason="artifact_size_limit"`。

### 15.2 Context rendering envelope

第一版建议 context renderer 总是用统一 envelope 把 clipped body 和 structured artifacts 渲染给模型：

```json
{
  "output_preview": "...",
  "output_truncated": true,
  "artifacts": [...]
}
```

不要尝试智能 merge 进所有工具的原 JSON，也不要 parse tool output JSON 来注入 artifacts，避免每个工具一套特殊逻辑。

terminal 这种有结构化状态的工具，可以在 preview 中保留 `status/exit_code/cwd/process_id` 等小字段。

注意：terminal preview 作为字符串嵌入 `output_preview` 时会出现双层 JSON 转义，形如 `{"output_preview":"{\"status\":...}"}`。这会多耗少量 token，但换来 renderer 不解析、不改写各工具原始输出格式的统一实现。

### 15.3 Graph 多 artifact 表达

当前 runtime `ToolResult` entity 似乎只有 `stored_as` 单值。PR1 可先把 primary artifact 写进去，多 artifact 的完整关系以 Postgres join table 为准。后续再升级语义图模型。
