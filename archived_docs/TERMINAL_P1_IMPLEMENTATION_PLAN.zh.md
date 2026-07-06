# Terminal P1 Implementation Plan

本文承接 `TERMINAL_CAPABILITY_GAP_ANALYSIS.zh.md` 和 `TERMINAL_P0_IMPLEMENTATION_PLAN.zh.md`，规划 terminal 能力的三个 P1 增量：

1. 统一 Tool Result Artifact 协议。
2. 交互输入增强。
3. approval cache / allowlist。

注意：PR1 已经拆成独立重型实施文档，后续以 `TOOL_RESULT_ARTIFACT_PR1_IMPLEMENTATION_PLAN.zh.md` 为准。本文中的 PR1 章节只保留历史上下文，不作为最终实施依据。

这三个方向都不引入 sandbox，也不改变 Pulsara 对 trusted host / full access 的产品判断。目标是让 host terminal 更可观察、更可恢复、更少重复确认，同时保持权限边界诚实：terminal 仍然是真实 host shell，不是隔离执行环境。

## 0. 当前代码基线

P0 之后，terminal 相关代码已经具备这些前置能力：

- `src/pulsara_agent/tools/builtins/terminal.py`
  - `terminal` 当前支持 `yield_time_ms`、`tty`、`full_output_ref`、streaming output。
  - `terminal_result_payload()` 是 `terminal` 与 `terminal_process` 共享的 payload 入口。
- `src/pulsara_agent/tools/builtins/terminal_process.py`
  - 当前 actions：`list` / `log` / `poll` / `wait` / `kill` / `write` / `submit` / `close_stdin`。
  - `list/log/poll/wait` 已在权限层被视作 read-only process action，不会在 `terminal_access=ask` 或 `approval_policy=on_request` 下反复审批。
  - `write/submit` 会对输入内容做 hardline 检查，阻止通过 stdin 绕过灾难命令底线。
- `src/pulsara_agent/runtime/terminal/output.py`
  - `OutputAccumulator` 会 strip ANSI、redact secrets。
  - 超过阈值后写 `.pulsara/terminal-output/<process_id>.txt`。
  - 目前没有统一 artifact object，也没有 artifact 最大体积 watchdog。
- `src/pulsara_agent/runtime/terminal/models.py`
  - `TerminalResult.full_output_ref`
  - `TerminalProcessInfo.full_output_ref`
  - `TerminalProcessLog.full_output_ref`
  - `TerminalProcessInfo` 内部有 monotonic 时间戳，但 `to_payload()` 不下发它们，只下发 `duration_seconds`。
- `src/pulsara_agent/runtime/permission.py`
  - 三轴权限已经打开：`PermissionProfile` / `ApprovalPolicy` / `TerminalAccess`。
  - hardline deny 不可覆盖。
  - `TERMINAL_PROCESS_READ_ONLY_ACTIONS = {"list", "log", "poll", "wait"}`。
  - 当前没有 approval cache，所有需要确认的 side-effect call 都会进入 `WAIT_FOR_USER`。
- `src/pulsara_agent/runtime/approval.py`
  - `ToolApprovalDecision.rules` 已存在，但当前只是事件/replay 上的 inert payload。
  - `PendingApproval.suggested_rules` 会从 gate 的 `suggested_rules` 聚合而来。
- `src/pulsara_agent/host/session.py`
  - approval resume / deny / suspended stop 已可恢复。
  - `HostSession.summary()` 已包含 pending approval 和 terminal summary。

## 1. 全局约束

这三个 PR 共享以下不变量：

- **Hardline deny 永远优先。** `rm -rf /`、整盘覆写、以及通过 `terminal_process` stdin 注入的 hardline 内容，不得被 approval cache / allowlist 覆盖。
- **权限 cache 只能放行已暴露工具。** `read_only` 下 terminal 工具不在 registry 内，cache 不得重新打开它。
- **模型可见字段不得暴露 monotonic timestamp。** 只允许 `duration_seconds`，若未来需要时间点，新增 wall-clock ISO 字段。
- **secret redaction 必须发生在 artifact、preview、event、tool payload 之前。** 不能为了“完整输出”绕过 `OutputAccumulator`。
- **owner host session isolation 不回退。** `terminal_process list/log/poll/wait` 仍只能访问当前 host session 拥有的 process。
- **full access 是一等路径。** 默认 trusted project 可以继续低摩擦执行；approval cache 是给选择 `ask/on_request` 的用户降噪，不是把 full access 伪装成安全沙箱。
- **PR1 允许 breaking payload change。** 统一 Tool Result Artifact 协议时直接移除 `full_output_ref` 和 `.pulsara/terminal-output/`，不做双字段迁移。

## 2. PR1：统一 Tool Result Artifact 协议

### 2.1 目标

把“长 tool result 怎么存、怎么在上下文中引用、模型怎么继续读取”提升为 runtime 级协议。terminal output 只是当前第一个接入场景；未来 web search、crawl、browser scrape、large grep、test report、JSON dump 等长结果都走同一协议。

- 直接删除 terminal 私有的 `.pulsara/terminal-output/` 文件路径。
- 直接删除模型可见 payload 中的 `full_output_ref`。
- 长 tool result 进入 `ArtifactStore`，默认实现是 Postgres `artifacts` 表。
- Tool result 上下文只保留 preview + artifact reference。
- 模型通过统一 read 工具/API 继续读取 artifact，而不是通过 `read_file` 读取本地路径。
- terminal、search、crawl 等工具共享同一 artifact reference shape。

### 2.2 非目标

本 PR 不做：

- 新的 memory 语义图实体设计。
- 把 artifact 内容自动写入 durable memory。
- stdout/stderr 真正分流。
- UI rich viewer。
- 大型对象存储/S3 backend。

原因：仓库里已经有 `ArtifactStore` / `PostgresArtifactStore` 和 `artifacts` 表。PR1 应先把 runtime tool result 接到这条主干上。语义图只记录 artifact provenance，不承载大正文。

### 2.3 模型可见协议形状

建议模型可见字段命名为 `artifacts`，值为 artifact refs 数组。即使当前只有一个长输出，也保持数组，避免 web crawl / browser scrape 未来一次 tool call 产生多个 artifact 时再破坏协议。

```json
{
  "output_preview": "short redacted preview shown inline",
  "output_truncated": true,
  "artifacts": [
    {
      "kind": "tool_result_artifact",
      "artifact_id": "artifact:tool-result:run-abc:call-123:output",
      "role": "output",
      "media_type": "text/plain; charset=utf-8",
      "encoding": "utf-8",
      "size_bytes": 124532,
      "digest": "sha256:...",
      "redacted": true,
      "stored_complete": true,
      "loss_reason": null,
      "preview": {
        "text": "same preview, or a smaller artifact-local preview",
        "chars": 4000,
        "truncated": true,
        "strategy": "head_tail"
      },
      "source": {
        "tool_name": "terminal",
        "tool_call_id": "call-123",
        "run_id": "run-abc",
        "turn_id": "turn-abc",
        "reply_id": "reply-abc",
        "metadata": {
          "terminal_process_id": "terminal-process-abc",
          "stream": "combined"
        }
      },
      "read_more": {
        "tool": "artifact_read",
        "artifact_id": "artifact:tool-result:run-abc:call-123:output",
        "mode": "text",
        "default_max_chars": 20000
      }
    }
  ]
}
```

字段约束：

- `artifact_id` 是唯一读取入口。模型协议里不出现 filesystem path。
- `role` 表示该 artifact 在 tool result 中扮演的角色。第一版常用 `output`；terminal 可用 `combined_output`，未来可扩展 `stdout` / `stderr` / `page_html` / `screenshot` / `json`。
- `media_type` 必须来自真实存储对象，不能由 tool description 随便写。
- `encoding` 只对 text artifact 有意义；binary artifact 可为 `null` 或省略。
- `size_bytes` 是存储后 payload 的字节数。
- `digest` 是存储后 payload 的 digest。若 terminal output 已 redacted，digest 对应 redacted 后内容，不是 raw subprocess bytes。
- `redacted` 表示存储内容是否经过 redaction。terminal 应为 `true`。
- `stored_complete=false` 表示 artifact 本身已经丢失了 raw 内容的一部分，例如超过 artifact 最大体积。不要把它和 `preview.truncated` 混为一谈。
- `loss_reason` 只在 `stored_complete=false` 时出现，例如 `artifact_size_limit`。
- `preview.truncated=true` 只说明 inline preview 比 artifact 短。
- `source.metadata` 放工具特有字段；核心字段不要塞进 metadata。
- `read_more.tool` 第一版建议新增 read-only builtin：`artifact_read`。

### 2.4 Runtime 协议代码落点

建议新增：

- `src/pulsara_agent/runtime/artifacts.py`
- `src/pulsara_agent/runtime/tool_artifacts.py`

核心 dataclass：

```python
@dataclass(frozen=True, slots=True)
class ToolResultArtifactRef:
    kind: Literal["tool_result_artifact"]
    artifact_id: str
    role: str
    media_type: str
    encoding: str | None
    size_bytes: int
    digest: str
    redacted: bool
    stored_complete: bool
    loss_reason: str | None
    preview: dict[str, object]
    source: dict[str, object]
    read_more: dict[str, object]

    def to_payload(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class ToolResultArtifactPayload:
    output_preview: str
    output_truncated: bool
    artifacts: tuple[ToolResultArtifactRef, ...]

    def to_payload(self) -> dict[str, object]: ...
```

`ToolResultArtifactService` 职责：

- 根据 tool output 长度决定是否 archive。
- 生成 artifact id。
- 调用 `ArtifactStore`。
- 生成模型可见 `ToolResultArtifactPayload`。
- 不理解 terminal/search/crawl 的业务语义，只处理 tool result 输出归档。

### 2.5 ArtifactStore 是否需要修改

当前 `ArtifactStore` 协议太窄：

```python
def put_text(blob_id, content, *, session_id=None, run_id=None, media_type="text/plain", metadata=None) -> ArtifactWriteResult
def get_text(blob_id) -> str
```

当前 Postgres schema 反而已经更宽：

- `text_body`
- `binary_body`
- `media_type`
- `digest`
- `size_bytes`
- `stored_at`
- `metadata`
- `session_id`
- `run_id`

建议修改协议，不删除现有 schema 字段：

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
    def put_text(..., metadata: dict[str, Any] | None = None) -> ArtifactWriteResult: ...
    def put_bytes(..., media_type: str, metadata: dict[str, Any] | None = None) -> ArtifactWriteResult: ...
    def get_info(self, blob_id: str) -> ArtifactRecord: ...
    def get_text(self, blob_id: str) -> str: ...
    def read_text(self, blob_id: str, *, offset_chars: int = 0, max_chars: int = 20000) -> ArtifactTextSlice: ...
    def get_bytes(self, blob_id: str) -> bytes: ...
```

Postgres 表建议：

- 不删除现有字段。
- `binary_body` 已存在，应补 `put_bytes/get_bytes` 协议和实现。
- `metadata` 先承载 `artifact_kind`、`role`、`tool_name`、`tool_call_id`、`turn_id`、`reply_id`、`redacted`、`stored_complete`、`loss_reason`。
- 新增索引 `idx_artifacts_session_id`，因为 read 权限会按 runtime session 校验。
- 如果未来需要高频查询 `kind/role`，再把它们提升为实体列；PR1 不必急着迁移。
- `tool_execution_records.artifact_id` 当前是单 artifact 设计。统一协议不应继续强化这个单值字段，建议新增 join table：

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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (run_id, tool_call_id, role, ordinal)
);
```

理由：

- web crawl / browser scrape 未来可能一次 tool call 产出多个 artifact。
- terminal 今天可以只有一个 `combined_output`，但协议不应把单值写死。
- join table 比往 `artifacts` 表硬塞 tool 字段更干净，因为 artifact store 仍然是通用 blob store。

### 2.6 ToolExecutor 接入点

统一归档应尽量放在 tool 执行边界，而不是每个工具各写一套：

- `ToolExecutor.execute()` 得到 `ToolExecutionResult` 后调用 `ToolResultArtifactService.archive_if_needed(...)`。
- 若 output 未超过阈值，原样发 `ToolResultTextDeltaEvent`。
- 若 output 超过阈值：
  - 完整 output 写入 `ArtifactStore`。
  - event log 中只写 preview + `artifacts` refs。
  - `ToolResultEndEvent` 仍保持原状态。

这会覆盖所有同步工具。

terminal 的特殊点：

- `terminal.execute_streaming_with_context()` 当前会提前发 streaming deltas。
- PR1 第一版应避免让 streaming deltas 绕过归档策略；建议对会产生 artifact 的 terminal call 关闭完整 streaming，只发送 bounded preview。
- yielded/background process 完成后不主动生成本地文件；下一轮模型如需查看输出，调用 `terminal_process log`，该 tool result 若超阈值再进入统一 artifact store。
- `OutputAccumulator` 第一版可以继续做内存 redacted accumulator，但必须删除 `artifact_path` / `artifact_root` / `.pulsara/terminal-output` 写文件逻辑。

### 2.7 artifact_read 工具

新增 read-only builtin：

- `src/pulsara_agent/tools/builtins/artifact.py`

Schema：

```json
{
  "artifact_id": { "type": "string" },
  "mode": { "type": "string", "enum": ["text", "info"], "default": "text" },
  "offset_chars": { "type": "integer", "default": 0 },
  "max_chars": { "type": "integer", "default": 20000 }
}
```

权限：

- read-only。
- 只允许读取当前 runtime session 拥有的 artifact。
- `read_only` profile 下可以开放，因为它不执行 host shell、不读 workspace filesystem。
- 不允许按 arbitrary Postgres id 绕过 session ownership。

Payload：

```json
{
  "status": "success",
  "artifact_id": "artifact:tool-result:run-abc:call-123:output",
  "media_type": "text/plain; charset=utf-8",
  "size_bytes": 124532,
  "offset_chars": 0,
  "returned_chars": 20000,
  "has_more": true,
  "text": "..."
}
```

### 2.8 Payload 硬切策略

PR1 是 breaking payload change：

- 不再生成 `.pulsara/terminal-output/<process_id>.txt`。
- 不再输出 `full_output_ref`。
- 不再让模型通过 `read_file` 读取 terminal output。
- prompt / tool description / tests 全部改为 `artifacts[].read_more.tool == "artifact_read"`。

Terminal payload 示例：

```json
{
  "status": "success",
  "output_preview": "pytest output preview...",
  "output_truncated": true,
  "artifacts": [
    {
      "kind": "tool_result_artifact",
      "artifact_id": "artifact:tool-result:run-abc:call-terminal:combined-output",
      "role": "combined_output",
      "media_type": "application/json",
      "size_bytes": 88210,
      "read_more": {
        "tool": "artifact_read",
        "artifact_id": "artifact:tool-result:run-abc:call-terminal:combined-output",
        "mode": "text"
      }
    }
  ],
  "exit_code": 1,
  "cwd": "/Users/plumliu/Desktop/python_workspace/pulsara_agent"
}
```

注意：如果归档的是整个 terminal result JSON，`media_type` 可以是 `application/json`；如果只归档 terminal combined output，则 `media_type` 是 `text/plain; charset=utf-8`。PR1 需要二选一并保持一致。建议第一版归档整个 tool output JSON，因为 ToolExecutor 级归档才能覆盖所有工具。

### 2.9 测试

新增/更新：

- `tests/test_terminal_runtime.py`
  - 不再创建 `.pulsara/terminal-output/`。
  - `OutputAccumulator` 不再写本地 artifact。
  - payload 中不再出现 `full_output_ref`。
  - terminal 长输出通过统一 artifact refs 暴露。
  - secret redaction 同时作用于 preview 和 ArtifactStore 内容。
  - `duration_seconds` 存在，monotonic timestamp 不出现在 payload。
- `tests/test_artifact_store_contract.py`
  - `put_bytes/get_bytes`。
  - `get_info` 返回 media_type、digest、size、metadata。
  - `read_text(offset_chars, max_chars)` 支持分页。
  - Postgres owner 校验继续有效。
- `tests/test_tools.py`
  - 任意长 tool result 被 `ToolResultArtifactService` 归档。
  - 短 tool result 不归档。
  - `artifact_read` 只能读当前 session artifact。
  - binary artifact 用 `info` 可见，但 `mode=text` 返回明确错误。
- `tests/test_permission_policy.py`
  - `artifact_read` 是 read-only，不触发 terminal approval。
- `tests/test_real_llm_integration.py`
  - real smoke：让模型运行一个超过 tool preview 的命令，再使用 `artifact_read` 读取 artifact，最终输出 sentinel。

建议 real smoke：

```text
运行一个会产生大量输出的命令，使 tool result 返回 artifacts[]。
然后根据 artifacts[0].read_more.artifact_id 调用 artifact_read，
最后只回答 PULSARA_TOOL_ARTIFACT_OK。
```

## 3. PR2：交互输入增强

### 3.1 目标

让 Pulsara 更自然地处理 REPL、installer、简单 TUI、认证提示等交互场景：

- 增加 `terminal_process(action="send_keys")`。
- 增加 `terminal_process(action="paste")`。
- `poll/wait/log` 返回 `waiting_for_input` hint。
- PTY 下支持常用控制键、方向键、Tab、Enter、Ctrl-C。
- `paste` 在 PTY 下默认 bracketed paste。

### 3.2 非目标

本 PR 不做：

- full TUI screen model。
- expect-style pattern automation。
- terminal resize / alternate screen diff。
- 密码输入安全 UI。
- shell prompt semantic parser。

第一版只做输入能力和低置信度 hint。

### 3.3 Tool schema

`terminal_process` 新增 actions：

```python
_SUPPORTED_ACTIONS = {
    "list", "log", "poll", "wait", "kill",
    "write", "submit", "send_keys", "paste", "close_stdin",
}
```

新增参数：

```json
{
  "keys": {
    "type": "array",
    "items": { "type": "string" },
    "description": "Key tokens for send_keys, e.g. ENTER, CTRL_C, TAB, UP, DOWN."
  },
  "bracketed_paste": {
    "type": "boolean",
    "default": true
  }
}
```

`paste` 复用 `data`。

`send_keys` 支持 token：

- `ENTER`
- `TAB`
- `ESC`
- `BACKSPACE`
- `DELETE`
- `UP`
- `DOWN`
- `LEFT`
- `RIGHT`
- `HOME`
- `END`
- `CTRL_A`
- `CTRL_C`
- `CTRL_D`
- `CTRL_E`
- `CTRL_L`
- `CTRL_U`

第一版不支持任意 `CTRL_<letter>` 泛化，避免模型误造 token。

### 3.4 Runtime 实现

`process.py` 新增：

```python
def send_process_keys(state: TerminalProcessState, keys: list[str]) -> None: ...
def paste_process_input(state: TerminalProcessState, data: str, *, bracketed_paste: bool) -> None: ...
```

PTY 编码：

- `ENTER` -> `\r`
- `TAB` -> `\t`
- `CTRL_C` -> `\x03`
- `CTRL_D` -> `\x04`
- `ESC` -> `\x1b`
- arrows/home/end/delete/backspace 使用常见 ANSI sequence。
- `paste` 默认 `\x1b[200~{data}\x1b[201~`。

PIPE 编码：

- `paste` 等价 raw `write`。
- `ENTER` 写 `\n`。
- `TAB` 写 `\t`。
- `CTRL_D` 等价 `close_stdin`。
- `CTRL_C` 对 process group 发 SIGINT，而不是写 `\x03` 到 pipe。
- 方向键等 PTY-only token 在 PIPE 下返回 `blocked`，提示用户需要 `terminal(..., tty=true)`。

注意：`CTRL_C` 是 side-effect action，不等价 `kill`。它允许进程自行处理 SIGINT；若仍不退出，模型再用 `kill`。

### 3.5 waiting_for_input hint

新增轻量 dataclass：

```python
@dataclass(frozen=True, slots=True)
class TerminalWaitingForInputHint:
    possible: bool
    confidence: Literal["low", "medium", "high"]
    reason: str | None
    idle_seconds: float
    prompt_excerpt: str | None
```

`TerminalProcessState` 增加：

- `last_output_at: float`
- `last_input_at: float | None`

更新点：

- reader loop 每次 append output 后更新 `last_output_at`。
- `write/submit/send_keys/paste/close_stdin` 更新 `last_input_at`。

启发式：

- process running。
- stdin 未关闭。
- 最近输出 idle 超过 1 秒。
- tail 命中 prompt pattern：
  - `password:`
  - `passphrase`
  - `continue?`
  - `press enter`
  - `>>> `
  - `In [1]:`
  - `$ ` / `# ` / `> `
  - `Enter .*:`

返回位置：

- `TerminalResult.metadata["waiting_for_input"]`
- `TerminalProcessInfo.to_payload()`
- `TerminalProcessLog.to_payload()`

置信度保持保守：无法确定时返回 `possible=false`，不要把 hint 当阻塞状态。

### 3.6 权限语义

`send_keys` / `paste` 是 side-effect action：

- 不加入 `TERMINAL_PROCESS_READ_ONLY_ACTIONS`。
- `terminal_access=ask` 下要审批。
- `approval_policy=on_request` 下要审批。
- `paste` 的 `data` 必须走 hardline 检查。
- `send_keys` 不接受 raw shell string，因此不做 hardline 字符串匹配；若未来支持 raw text token，必须补 hardline。
- `CTRL_C` / `CTRL_D` 仍要审批，因为会改变进程状态。

### 3.7 测试

新增/更新：

- `tests/test_terminal_runtime.py`
  - PTY `send_keys(["ENTER"])` 能驱动 REPL。
  - PTY `send_keys(["CTRL_C"])` 能中断可中断进程。
  - PIPE `CTRL_C` 发送 SIGINT。
  - PTY `paste(..., bracketed_paste=True)` 写入 bracketed paste sequence。
  - PIPE 下 PTY-only key 返回 blocked。
  - `waiting_for_input` 在简单 prompt 场景返回 possible/high 或 medium。
- `tests/test_tools.py`
  - `terminal_process send_keys/paste` schema 和 payload。
  - `paste` hardline 输入 blocked。
- `tests/test_permission_policy.py`
  - `send_keys/paste` 在 ask/on_request 下 WAIT_FOR_USER。
  - `paste` hardline 在 never/allow 下仍 DENY。
- `tests/test_real_llm_integration.py`
  - real smoke：启动 `python -q` 或一个小型 prompt script，模型用 `paste/send_keys` 输入表达式，最终输出 `PULSARA_INTERACTIVE_INPUT_OK`。

Real smoke 需要充足 `max_output_tokens`，避免思考模型把预算烧完导致 final text 空。

## 4. PR3：Approval Cache / Allowlist

### 4.1 目标

approval resume 解决了“问完能不能继续”的问题；本 PR 解决“同类动作不要一直问”的问题：

- 支持用户在 approve 时选择 scope：
  - once：只批准当前 pending call。
  - session：当前 host session 内匹配规则自动放行。
  - project：当前 workspace 内持久放行。
  - always：用户级持久放行。
- 支持 exact rule 和 terminal prefix rule。
- approval prompt 展示 command、cwd、tool、risk reason、建议规则。
- host summary / inspect 可查看 active approval grants。
- hardline deny 不可被 cache / allowlist 覆盖。

### 4.2 非目标

第一版不做：

- 模型自行创建 allowlist。
- 复杂 shell AST。
- 按 env value 级别匹配 secret。
- 团队级 policy。
- remote policy server。

所有持久 allowlist 都必须来自用户显式 approval resolution，不能由 assistant tool call 写入。

### 4.3 数据模型

建议新增：

- `src/pulsara_agent/runtime/approval_rules.py`

```python
class ApprovalGrantScope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    PROJECT = "project"
    ALWAYS = "always"


class ApprovalRuleKind(StrEnum):
    EXACT_TOOL_CALL = "exact_tool_call"
    TERMINAL_COMMAND_PREFIX = "terminal_command_prefix"
    FILE_PATH_PREFIX = "file_path_prefix"


@dataclass(frozen=True, slots=True)
class ApprovalRule:
    rule_id: str
    scope: ApprovalGrantScope
    kind: ApprovalRuleKind
    tool_name: str
    pattern: dict[str, object]
    workspace_root: str | None
    created_at: float
    source_approval_id: str
    source_tool_call_id: str
    reason: str | None = None
```

匹配对象：

```python
@dataclass(frozen=True, slots=True)
class ApprovalRuleMatch:
    rule_id: str
    scope: ApprovalGrantScope
    reason: str
```

Store：

```python
class ApprovalRuleStore:
    def add(self, rule: ApprovalRule) -> None: ...
    def match(self, call: ToolCall, *, workspace_root: Path) -> ApprovalRuleMatch | None: ...
    def list(self) -> list[ApprovalRule]: ...
    def clear(self, rule_id: str | None = None) -> int: ...
```

第一版实现：

- session rules：内存。
- project rules：`.pulsara/approvals.json`。
- always rules：`$PULSARA_HOME/approvals.json`。

若担心持久 scope 一次做太大，可以切成 PR3-A session、PR3-B project/always。但接口一次定好，避免以后改 resolution schema。

### 4.4 Rule 语义

`EXACT_TOOL_CALL`：

- 匹配 tool name。
- 匹配 canonical JSON args hash。
- 适合 `write_file`、`edit_file`、单条 terminal command。

`TERMINAL_COMMAND_PREFIX`：

- 只用于 `terminal`。
- 用 `shlex.split(command)` 得到 argv。
- pattern 存 argv prefix，例如：

```json
{
  "argv_prefix": ["uv", "run", "pytest"],
  "cwd": "/Users/plumliu/Desktop/python_workspace/pulsara_agent"
}
```

- 匹配时要求 command argv 以该 prefix 开头。
- `cwd` 默认绑定当前 workspace cwd；用户显式选择 broader scope 前不跨 cwd。

`FILE_PATH_PREFIX`：

- 用于 `write_file` / `edit_file`。
- path 仍必须通过 workspace containment。
- 默认只允许具体文件；目录 prefix 需要用户显式选择。

### 4.5 Gate 决策顺序

`PolicyPermissionGate._evaluate_call()` 调整为：

1. hardline terminal command deny。
2. hardline terminal_process input deny。
3. `is_tool_allowed_by_policy()`。
4. `terminal_process` read-only action allow。
5. approval rule store match；命中则 allow。
6. `terminal_access=ask`。
7. `approval_policy=on_request`。
8. `approval_policy=risky_only`。
9. inner gate。

这保证：

- allowlist 不会打开被 profile/terminal off 隐藏的工具。
- allowlist 不会覆盖 hardline。
- allowlist 可以降低 `ask/on_request/risky_only` 的重复审批。

### 4.6 Resolution schema

当前：

```python
@dataclass(frozen=True, slots=True)
class ToolApprovalDecision:
    tool_call_id: str
    confirmed: bool
    rules: tuple[dict, ...] = ()
```

可以复用 `rules`，但需要把它从 inert payload 变成 host 消费字段。

约定：

```json
{
  "scope": "session",
  "kind": "terminal_command_prefix",
  "argv_prefix": ["uv", "run", "pytest"]
}
```

Host 侧在 `resolve_approval()` / `stream_approval_resolution()` 中：

- 校验 `approval_id`。
- 对 `confirmed=True` 的 decision 读取 `rules`。
- 只接受当前 pending tool call 可派生出的 rule。
- 写入 `ApprovalRuleStore`。
- 再调用 `AgentRuntime.resume_after_approval()`。

要在 resume 前写入 store，这样同一 run 后续模型再次发出匹配 call 时可直接放行。

### 4.7 suggested_rules

Gate 需要提供更有用的 `suggested_rules`：

Terminal example：

```json
{
  "tool": "terminal",
  "reason": "terminal_on_request",
  "command": "uv run pytest tests/test_terminal_runtime.py -q",
  "cwd": "/Users/plumliu/Desktop/python_workspace/pulsara_agent",
  "suggested_approval_rules": [
    {
      "scope": "session",
      "kind": "exact_tool_call"
    },
    {
      "scope": "session",
      "kind": "terminal_command_prefix",
      "argv_prefix": ["uv", "run", "pytest"]
    }
  ]
}
```

File write example：

```json
{
  "tool": "write_file",
  "reason": "write_tool_on_request",
  "path": "docs/example.md",
  "suggested_approval_rules": [
    {
      "scope": "session",
      "kind": "exact_tool_call"
    },
    {
      "scope": "project",
      "kind": "file_path_prefix",
      "path_prefix": "docs/"
    }
  ]
}
```

### 4.8 CLI / Host API

Host API：

- `HostCore.list_approval_rules(host_session_id)`
- `HostCore.clear_approval_rule(host_session_id, rule_id=None)`
- `HostSession.summary()` 增加 `approval_rules` 简表。

CLI REPL：

- `:approval` 显示 pending approval、risk reason、suggested rules。
- `:approve` 保持 once。
- `:approve session` 对每个 confirmed call 添加 exact session rule。
- `:approve prefix` 对 terminal command 添加 suggested prefix session rule。
- `:approvals` 列出 active rules。
- `:approval-clear [rule_id]` 清除规则。

持久 scope：

- `:approve project` / `:approve always` 第一版可以要求二次确认文案。
- 输出必须明确这是 trusted host allowlist，不是 sandbox。

### 4.9 Audit

新增事件可选但建议做：

```python
@dataclass(frozen=True, slots=True)
class ApprovalRuleAddedEvent(AgentEvent):
    rule_id: str
    scope: str
    kind: str
    tool_name: str
    source_approval_id: str
    source_tool_call_id: str
```

也可以先不加 canonical event，只放 host summary。但如果做 project/always 持久化，最好有 event/audit，方便用户追溯“为什么这次没问”。

### 4.10 测试

新增/更新：

- `tests/test_permission_policy.py`
  - cache 命中后 `terminal_access=ask` 放行。
  - cache 命中后 `approval_policy=on_request` 放行。
  - hardline deny 优先于 cache。
  - `read_only` 仍不能被 cache 打开 terminal。
  - prefix rule 只匹配 argv prefix，不匹配字符串前缀陷阱，例如 `pytestx` 不匹配 `pytest`。
- `tests/test_approval_resume.py` 或 host core 相关测试
  - approve with session rule 后，同一 run 后续匹配 call 不再 suspend。
  - denied decision 不写入 rule。
  - session rule 不跨 host session。
  - project/always rule 若实现，重建 session 后仍可加载。
- `tests/test_cli_host.py`
  - `:approve` once 行为不变。
  - `:approve session` 写 rule。
  - `:approvals` / `:approval-clear` 输出正确。
- `tests/test_real_llm_integration.py`
  - real smoke：`trusted_host + on_request + ask` 下，第一次 terminal call pending；测试 harness 用 session/prefix approve；第二次相同 prefix terminal call 不再 pending；最终 sentinel。

## 5. PR 顺序

建议顺序：

1. **PR1 Tool Result Artifact protocol**
   - 独立、低权限风险。
   - 会改善所有长 tool result 的上下文占用和可读性，terminal/log/completion note 是第一批受益场景。
2. **PR2 interactive input**
   - 依赖 PR1 的 payload 统一，但不依赖 approval cache。
   - 需要重点测 PTY 和 PIPE 差异。
3. **PR3 approval cache / allowlist**
   - 依赖 PR2 后 side-effect action 面更完整。
   - 是权限语义变更，测试面最大。

也可以先做 PR3-A session-only cache，推迟 project/always persistence。若 dogfood 目标是“常态 `trusted_host + on_request + ask`”，session cache 已经能显著降噪。

## 6. 风险与需要拍板的问题

### 6.1 Artifact size 上限

建议默认 20 MiB。超过后：

- 不中断 tool / terminal process。
- 不继续扩大 artifact。
- payload 标 `stored_complete=false` 和 `loss_reason="artifact_size_limit"`。
- preview 仍保留 head/tail。

需要确认是否允许配置，例如 `PULSARA_TOOL_ARTIFACT_MAX_BYTES`。

### 6.2 归档整个 tool output 还是只归档大字段

建议第一版归档整个 `ToolExecutionResult.output` 字符串：

- 优点：ToolExecutor 层可以覆盖所有工具，不要求每个工具重写 payload。
- 缺点：terminal 的 `exit_code` / `cwd` 等结构化字段也在 artifact JSON 里，inline preview 需要保留足够摘要，避免模型为了看 exit code 还要读 artifact。

如果只归档 terminal payload 中的 `output` 字段，会更精细，但每个工具都要自己接入，无法自然覆盖未来 search/crawl。

结论：PR1 先归档整个 tool output，并要求工具在短 preview 中保留关键状态摘要。

### 6.3 Project / always allowlist 是否进入首版

session cache 风险低；project/always 是持久 trust 决策，产品上有价值，但要有：

- inspect 可见。
- CLI 可清除。
- 二次确认。
- 文件格式版本。

建议实现接口时预留 scope，但首个 merge 可以只打开 session scope。

### 6.4 Prefix rule 的粒度

`terminal_command_prefix` 应按 argv prefix 匹配，不按 raw string prefix 匹配。否则 `rm` / `rmdir`、`pytest` / `pytestx` 这类边界会出问题。

### 6.5 waiting_for_input 误判

hint 只能是提示，不应驱动自动审批或自动输入。模型看见它可以选择问用户、send_keys 或 paste，但 runtime 不应因为 hint 自行动作。

## 7. 验证命令

常规：

```bash
uv run pytest tests/test_terminal_runtime.py -q
uv run pytest tests/test_tools.py -q
uv run pytest tests/test_permission_policy.py -q
uv run pytest tests/test_host_core.py -q
uv run pytest tests/test_cli_host.py -q
uv run pytest -q
```

Real LLM smoke：

```bash
PULSARA_RUN_REAL_LLM=1 uv run pytest tests/test_real_llm_integration.py::test_real_host_core_tool_result_artifact_protocol -q -s
PULSARA_RUN_REAL_LLM=1 uv run pytest tests/test_real_llm_integration.py::test_real_host_core_terminal_interactive_input -q -s
PULSARA_RUN_REAL_LLM=1 uv run pytest tests/test_real_llm_integration.py::test_real_host_core_approval_cache_prefix_rule -q -s
```

Real smoke 需要：

- 使用足够 `max_output_tokens`，建议不少于 512。
- sentinel 必须出现在 final text。
- 若 provider 返回 finish_reason，断言为 `stop`，避免把 token 预算耗尽误判为功能失败。
