# Terminal P0 Implementation Plan

本文把 [TERMINAL_CAPABILITY_GAP_ANALYSIS.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/TERMINAL_CAPABILITY_GAP_ANALYSIS.zh.md) 中前两个 P0 落成切合当前代码的实施计划：

1. `terminal_process` inventory/list/log/poll 产品化。
2. yielded terminal process completion event 与下一轮唤醒/提示。

这两个 P0 都基于现有 terminal runtime，不引入 sandbox，不改变 trusted host/full access 作为一等路径的产品判断。

## 0. 当前代码基线

当前 terminal 相关代码集中在：

- `src/pulsara_agent/runtime/terminal/process.py`
  - `TerminalProcessState`
  - `ProcessRegistry`
  - `spawn_local_process()`
  - `wait_for_process()`
  - `snapshot_process()`
  - reader thread `_reader_loop()`
- `src/pulsara_agent/runtime/terminal/manager.py`
  - `TerminalSessionManager`
  - 已有 `list_owned()` / live count / finished count。
- `src/pulsara_agent/runtime/terminal/models.py`
  - `TerminalStatus`
  - `TerminalRequest`
  - `TerminalResult`
- `src/pulsara_agent/tools/builtins/terminal.py`
  - `TerminalTool`
  - `terminal_result_payload()`
- `src/pulsara_agent/tools/builtins/terminal_process.py`
  - `TerminalProcessTool`
  - 当前 actions：`poll` / `wait` / `kill` / `write` / `submit` / `close_stdin`
- `src/pulsara_agent/runtime/session.py`
  - `RuntimeSession.emit_from_thread()`
  - `RuntimeSession.make_thread_recorder()`
- `src/pulsara_agent/tools/executor.py`
  - `ToolExecutor.execute()`
  - 当前 tool 执行时只有 executor 持有 `EventContext`，tool 本身拿不到。
- `src/pulsara_agent/host/transcript.py`
  - failed / aborted note 注入。
- `src/pulsara_agent/host/session.py`
  - `HostSession.summary()`
  - stop / approval lifecycle。
- `src/pulsara_agent/host/supervisor.py`
  - `WorkspaceTerminalSupervisor.summary()`。

已有能力：

- yielded process 会登记进 `ProcessRegistry._processes`。
- registry 已经有 owner scoped `list_owned()`。
- registry 已经做 finished TTL 和 finished max count。
- output 由 `OutputAccumulator` 保留内存文本，并在超过阈值时写 `.pulsara/terminal-output/<process_id>.txt`。
- host/core 已有 session/supervisor summary，但只暴露 counts，不暴露 process list。

关键缺口：

- `terminal_process` 不能列出 task。
- 没有 log/tail action。
- `poll/wait` payload 只有底层结果，缺少 duration、task summary 等产品字段。
- yielded process 完成时只更新内存状态，不发 canonical event。
- 下一轮模型不会被告知“后台 terminal task 已完成”。

## 1. P0-A：Terminal Task Inventory

### 1.1 目标

让 terminal process 从“裸 process_id”升级成可浏览 task inventory：

- 模型可调用 `terminal_process(action="list")` 查看本 host session 拥有的 terminal tasks。
- 模型可调用 `terminal_process(action="log")` 查看某个 task 的输出 tail。
- `poll/wait/kill/write/submit/close_stdin` 返回更产品化的 payload。
- host session / workspace supervisor summary 能展示 terminal task count 和轻量列表。

### 1.2 非目标

本 PR 不做：

- durable process registry。
- app 重启后恢复 process handle。
- completion notification。
- watch patterns。
- persistent PTY panel。
- stdout/stderr 分流。
- cursor/delta 协议的完整实现。

原因：当前 `OutputAccumulator` 没有 output cursor API，先做 list/log/tail 能覆盖大部分 dogfood 痛点。cursor/delta 可在 P1 output protocol 再做。

### 1.3 新增/调整数据结构

建议在 `src/pulsara_agent/runtime/terminal/models.py` 增加两个 dataclass：

```python
@dataclass(frozen=True, slots=True)
class TerminalProcessInfo:
    process_id: str
    terminal_session_id: str
    command: str
    cwd: str
    backend_type: str
    io_mode: str
    status: str
    exit_code: int | None
    timed_out: bool
    stdin_closed: bool
    # Internal monotonic timestamps only. Do not expose these directly to the model.
    started_at_monotonic: float
    ended_at_monotonic: float | None
    duration_seconds: float
    full_output_ref: str | None
    owner_host_session_id: str | None = None
    owner_conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class TerminalProcessLog:
    process: TerminalProcessInfo
    output: str
    truncated: bool
    full_output_ref: str | None
```

注意：

- `started_at_monotonic` / `ended_at_monotonic` 当前来自 `time.monotonic()`，只能用于排序和计算 `duration_seconds`。
- 模型可见 payload 不得下发 monotonic timestamp。它不是 Unix time，对模型没有语义，容易误导。
- 若未来需要模型可见时间，应新增 wall-clock ISO 字段，例如 `started_at_iso` / `ended_at_iso`。

### 1.4 ProcessRegistry 增量

在 `src/pulsara_agent/runtime/terminal/process.py` 添加：

- `process_info(state: TerminalProcessState) -> TerminalProcessInfo`
- `process_log(state: TerminalProcessState, *, max_output_chars: int) -> TerminalProcessLog`
- `ProcessRegistry.list(...) -> list[TerminalProcessInfo]`
- `ProcessRegistry.log(process_id, ...) -> TerminalProcessLog`

建议签名：

```python
def list(
    self,
    *,
    owner_host_session_id: str | None = None,
    include_finished: bool = True,
    include_running: bool = True,
) -> list[TerminalProcessInfo]: ...

def log(
    self,
    process_id: str,
    *,
    max_output_chars: int | None = None,
    owner_host_session_id: str | None = None,
) -> TerminalProcessLog: ...
```

排序：

- running 在前，按内部 `started_at_monotonic` 倒序。
- finished 在后，按内部 `ended_at_monotonic or started_at_monotonic` 倒序。

`process_log()` 第一版复用 `state.output.snapshot(max_chars=...)`，不做 cursor。

### 1.5 TerminalSessionManager 增量

在 `src/pulsara_agent/runtime/terminal/manager.py` 添加：

- `list_processes(owner_host_session_id: str | None = None, include_finished: bool = True, include_running: bool = True)`
- `log_process(process_id: str, *, max_output_chars: int | None = None, owner_host_session_id: str | None = None)`

保留现有 `list_owned()` 以减少破坏，但内部可改为调用 `list_processes()` 或保留兼容。

### 1.6 TerminalProcessTool schema

当前 `_SUPPORTED_ACTIONS` 是：

```python
{"poll", "wait", "kill", "write", "submit", "close_stdin"}
```

改为：

```python
{"list", "log", "poll", "wait", "kill", "write", "submit", "close_stdin"}
```

当前 schema `required=["action", "process_id"]` 会阻止 `list`。建议改成 `required=["action"]`，在 `execute()` 中按 action 校验：

- `list`：不需要 `process_id`。
- `log` / `poll` / `wait` / `kill` / `write` / `submit` / `close_stdin`：需要 `process_id`。
- `write` / `submit`：仍需要 `data`。

参数建议：

- `include_finished`: boolean，默认 true，仅 `list`。
- `include_running`: boolean，默认 true，仅 `list`。
- `max_output_chars`: 继续用于 `log/poll/wait/...`。

第一版不加 `tail_lines`，避免 `OutputAccumulator` line index 复杂化。若需要，可以在 P1 用 artifact/read_file 支持更强分页。

### 1.7 Tool payload

新增 `terminal_process(action="list")` payload 形状：

```json
{
  "status": "success",
  "terminal_process_action": "list",
  "processes": [
    {
      "process_id": "proc_...",
      "terminal_session_id": "default",
      "command": "pytest -q",
      "cwd": "/path/to/workspace",
      "status": "running",
      "exit_code": null,
      "timed_out": false,
      "io_mode": "pipe",
      "backend_type": "local",
      "stdin_closed": false,
      "duration_seconds": 12.3,
      "full_output_ref": ".pulsara/terminal-output/proc_....txt"
    }
  ],
  "live_process_count": 1,
  "finished_process_count": 0
}
```

新增 `terminal_process(action="log")` payload：

```json
{
  "status": "success",
  "terminal_process_action": "log",
  "process_id": "proc_...",
  "process": { "...": "same process info" },
  "output": "tail or head/tail snapshot",
  "truncated": false,
  "full_output_ref": null
}
```

`poll/wait/kill/write/submit/close_stdin` 的现有 payload 继续兼容，但追加模型有意义的字段：

- `command`
- `duration_seconds`
- `stdin_closed`
- `owner_host_session_id` 不下发给模型，除非 debug/inspect；tool payload 应省略，metadata 可保留。
- 不下发 `started_at_monotonic` / `ended_at_monotonic`。

### 1.8 TerminalProcessTool 只读 action 的审批语义

本 PR 不应新增一个在 `terminal_access=ask` 下更烦的高频路径。

PR4 之后，当前策略会 gate 所有 `terminal_process` action。新增 `list/log` 后，如果 `list/log/poll/wait` 仍然每次审批，就会与 completion event 的目标相冲突：系统主动告诉模型任务完成，但模型查看结果还要反复等待用户确认。

因此本 PR 同时收窄 `terminal_access=ask` 的 terminal_process 语义：

- `list`：只读，allow。
- `log`：只读，allow。
- `poll`：只读，allow。
- `wait`：只读，allow。它可能阻塞一小段时间，但不改变进程状态。
- `write` / `submit`：会写 stdin，继续 ask，并继续 hardline 检查输入内容。
- `close_stdin`：会改变进程输入状态，继续 ask。
- `kill`：会终止进程，继续 ask。

实现切入点：

- `src/pulsara_agent/runtime/permission.py`
  - `PolicyPermissionGate._evaluate_call()` 或 `_evaluate_terminal_call()` 中识别 `terminal_process` action。
  - 在 `terminal_access=ask` 分支前，对只读 action 直接 allow。
  - hardline stdin 检查仍然优先于 allow。
- `tests/test_permission_policy.py`
  - 新增 `terminal_process list/log/poll/wait` 在 `terminal_access=ask` 下 allow。
  - 保留 `write/submit/close_stdin/kill` 在 `terminal_access=ask` 下 wait for user。

这属于 PR1 的一部分，不 defer。否则 P0 inventory 一上线就是已知高摩擦 UX。

### 1.9 Host/session summary

在 `HostSession.summary()` 中把：

```python
"has_live_processes": self.has_live_processes
```

扩展为：

```python
"terminal": {
    "has_live_processes": bool,
    "live_process_count": int,
    "finished_process_count": int,
    "processes": [lightweight info without output],
}
```

在 `WorkspaceTerminalSupervisor.summary()` 中加入：

- `processes`: workspace-level lightweight info。
- 或先只加入 `live_processes` / `finished_processes` 的 top N summary，避免 inspect 太吵。

注意：

- `cli host inspect` 现在创建临时 `RuntimeSession`，返回 `sessions=[]`、`workspace_supervisors=[]`，它不是运行态 inspect。P0 不必强行让 CLI inspect 看到 live tasks。
- 如果要展示 live tasks，应在长期运行的 `HostCore` API / desktop server inspect 上做，而不是当前一次性 CLI inspect。

### 1.10 P0-A 测试计划

`tests/test_terminal_runtime.py`：

- yielded running process 出现在 `list_processes()`。
- in-window completed command 不进入 list。
- finished yielded process 出现在 list，且 status/exit_code/duration 正确。
- `include_finished=False` 只返回 running。
- owner scoped list 只返回该 host session 的 process。
- `log_process()` 返回当前 output 和 `full_output_ref`。
- finished TTL cleanup 后 list 不返回 expired process。

`tests/test_tools.py`：

- `terminal_process(action="list")` 不要求 `process_id`。
- `terminal_process(action="list")` 返回 running process。
- `terminal_process(action="log")` 返回 output。
- `terminal_process(action="poll")` 兼容旧字段并包含新 summary 字段。
- `terminal_process` 在 `TerminalAccess.OFF` 下 list/log 也 fail closed。
- `log/poll/wait` 对其他 host session 的 process 返回 not_found。
- `terminal_access=ask` 下 `list/log/poll/wait` 不产生 approval。
- `terminal_access=ask` 下 `write/submit/close_stdin/kill` 继续产生 approval。

`tests/test_host_core.py`：

- session summary 包含 terminal live/finished count。
- workspace supervisor summary 包含 process counts/top-level summaries。
- close session 仍 kill owned process。

验证命令：

```bash
uv run pytest tests/test_terminal_runtime.py -q
uv run pytest tests/test_tools.py -q
uv run pytest tests/test_host_core.py -q
```

## 2. P0-B：Terminal Completion Event / Wake

### 2.1 目标

当 yielded terminal process 在后台完成时：

- 写入 canonical event log。
- live subscriber 能收到事件。
- 下一轮 prior context 能看到轻量 note。
- note 只在下一轮出现一次，不无限重复。
- 不要求模型主动 poll 才知道完成。

### 2.2 非目标

本 PR 不做：

- durable process recovery。
- app-level notification UI。
- heartbeat automation。
- watch patterns。
- per-process `notify_on_complete` 参数。
- completion event 写 memory。

当前 `TerminalTool` 明确 hard-cut `notify_on_complete` 参数。P0-B 建议仍不恢复这个模型参数，而是默认对所有 yielded process 发 completion event。等产品需要时再加配置。

### 2.3 新事件

在 `src/pulsara_agent/event/events.py`：

新增 `EventType`：

```python
TERMINAL_PROCESS_COMPLETED = "TERMINAL_PROCESS_COMPLETED"
```

新增事件类：

```python
class TerminalProcessCompletedEvent(EventBase):
    type: Literal[EventType.TERMINAL_PROCESS_COMPLETED] = EventType.TERMINAL_PROCESS_COMPLETED
    process_id: str
    terminal_session_id: str
    command: str
    status: str
    exit_code: int
    cwd: str
    timed_out: bool = False
    duration_seconds: float
    output_preview: str = ""
    output_truncated: bool = False
    full_output_ref: str | None = None
    backend_type: str = "local"
    io_mode: str = "pipe"
    tool_call_id: str | None = None
```

并加入 `AgentEvent` union。

字段原则：

- 不包含 raw env。
- 不包含 owner host session id，避免泄漏内部 routing；必要时放 metadata。
- `output_preview` 必须经过现有 `OutputAccumulator` redaction。
- preview 要短，例如 2000 chars。
- `duration_seconds` 是模型可见时间字段；不要下发 monotonic `started_at` / `ended_at`。

### 2.4 Origin context 是 PR2 的承重点

`TerminalProcessCompletedEvent` 继承 `EventBase`，必须有：

- `run_id`
- `turn_id`
- `reply_id`

后台进程完成时 run 通常已经结束，因此 reader thread 必须保存 origin context。

当前问题：

- `ToolExecutor.execute()` 有 `event_context`。
- `TerminalTool.execute()` 拿不到 `event_context`。
- `TerminalRequest.metadata` 目前只传 `output_callback`。
- `TerminalProcessState` 没有 `run_id/turn_id/reply_id/tool_call_id`。

建议最小改法：

1. 在 `ToolExecutor.execute()` 中支持 context-aware tool。

   伪代码：

   ```python
   if hasattr(tool, "execute_with_context"):
       result = tool.execute_with_context(
           call,
           event_context=event_context,
           record_event=self.record_event,
           emit_delta=self._tool_delta_emitter(event_context, call.id),
       )
   elif hasattr(tool, "execute_streaming"):
       ...
   else:
       ...
   ```

2. `TerminalTool` 实现 `execute_with_context()`，内部复用 `_execute()`。

3. `TerminalTool` 把下面 metadata 放进 `TerminalRequest.metadata`：

   - `origin_event_context`: `event_context`
   - `tool_call_id`: `call.id`
   - `record_event`: `self.record_event` 或经 executor 传入的 callable

4. `TerminalSession.execute()` 把这些 metadata 传给 `ProcessRegistry.exec_with_yield()`。

5. `ProcessRegistry.exec_with_yield()` / `spawn_local_process()` 把 origin context 和 recorder 存进 `TerminalProcessState`。

这比在 `TerminalTool` 里直接 emit completion 更稳，因为 completion 发生在 reader thread。

### 2.5 TerminalProcessState 增量

在 `TerminalProcessState` 增加：

```python
origin_run_id: str | None = None
origin_turn_id: str | None = None
origin_reply_id: str | None = None
origin_tool_call_id: str | None = None
completion_event_recorded: bool = False
completion_suppressed: bool = False
completion_reason: str | None = None
record_event: Callable[[AgentEvent], AgentEvent] | None = field(default=None, repr=False)
```

如果不想让 process.py import `AgentEvent`，可把 `record_event` 类型写成 `Callable[[object], object] | None`，但更推荐明确 import。

### 2.6 Kill reason 与 teardown suppression

completion event 不是所有 final state 都应该通知。

必须区分：

- 自然退出：emit completion event。
- 用户显式 `terminal_process kill`：emit completion event，status 为 `killed`。
- session close / workspace close / runtime teardown：suppress completion event。
- runtime-only lifetime watchdog：默认 suppress，除非未来产品明确需要报告这种后台清理。

原因：

- session/workspace 正在关闭时发一串 `killed` completion event 是噪声。
- 这些事件会落到即将销毁或关闭的 runtime session，用户也不需要下一轮继续它们。

建议实现：

- `kill_process(state, *, reason: TerminalKillReason = TerminalKillReason.USER)`。
- `ProcessRegistry.kill()` 调用 reason=`user_tool_kill`。
- `ProcessRegistry.kill_owned()`、`shutdown()` 调用 reason=`teardown`。
- lifetime watchdog 调用 reason=`lifetime_watchdog`。
- `TerminalProcessState` 记录 `completion_suppressed=True` 或 `completion_reason`。
- `_maybe_record_completion_event()` 仅在 non-suppressed final state 下 emit。

### 2.7 Exactly-once、race 与锁

当前 `_reader_loop()` 结束时：

- append final output。
- wait process。
- 如果 status still running，则设置 success/error。
- 清理 fd/cwd file。

新逻辑：

- 在状态进入 terminal final state 后调用 `_maybe_record_completion_event(state)`。
- 只有 `state.yielded is True` 时记录 completion event。
- 只有 `completion_event_recorded is False` 时记录。
- 第一次设置 `completion_event_recorded=True` 必须在 state lock 内完成。
- 锁内只做 guard 翻转和 snapshot 数据复制。
- `record_event(...)` / `emit_from_thread(...)` 必须在锁外调用，避免持 `state.lock` 做 event log append / publish。

race 必须处理：

1. `wait_for_process(..., kill_on_timeout=False)` 返回 False。
2. 进程可能刚好结束。
3. `exec_with_yield()` 还没来得及 `state.yielded = True`。
4. reader thread 先结束，看到 `yielded=False`，不记录 event。
5. `exec_with_yield()` 再设 `yielded=True`，就会漏 event。

修法：

- `_reader_loop()` final path 调 `_maybe_record_completion_event(state)`。
- `exec_with_yield()` 在设置 `state.yielded = True` 后也调一次 `_maybe_record_completion_event(state)`。
- exactly-once guard 保证两边不会双发。
- 两个调用点都必须遵守“锁内抓数据，锁外 emit”。

### 2.8 Thread publish

事件从 reader thread 产生，必须走现有 thread-safe path：

- `RuntimeSession.make_thread_recorder()` 生成 `RuntimeThreadRecorder`。
- recorder 调 `RuntimeSession.emit_from_thread()`。
- `emit_from_thread()` 会 append event log，再 `publish_from_thread()`。

已知行为：

- 如果 publisher 已绑定且 loop 仍活，live subscribers 会收到。
- 如果 loop 已关闭，`publish_from_thread()` 返回 false；当前实现会 discard unpublished，但 event log append 已发生。

文档契约：

- canonical event log append 是核心保证。
- live publish 是 best-effort，在 desktop/server 常驻 loop 下应成功。
- CLI one-shot loop 结束后，后台 completion 可能只能落 event log，不能 live publish；这是当前 RuntimeEventPublisher 架构限制，P0 不解决。

### 2.9 Completion note 注入

在 `src/pulsara_agent/host/transcript.py` 增加 completion note 逻辑。

文案建议：

```text
Pulsara note: a terminal task from previous work completed in the background. Process {process_id} finished with status {status} and exit code {exit_code}. If the process is still retained, inspect it with terminal_process log or continue from this result.
```

不要包含完整 output；最多包含短 preview，且 preview 已 redacted。

关键寿命策略：

- completion note 只为“上一次 RunStart 之后发生、但还没有被下一轮 user input 覆盖”的 completion events 注入。

这个策略依赖一个必须锁死的 host 编排契约：

- `HostSession.run_turn()` / `stream_turn()` 必须先调用 `_prior_messages()` / `rebuild_prior_messages()`。
- 然后才创建 state 并调用 `AgentRuntime.run_task()` / `stream_task()`。
- `RunStartEvent` 在 `AgentRuntime._stream_task()` 内部追加，因此当前轮 `RunStartEvent` 在 context rebuild 时尚未落 log。

当前代码满足这个顺序：`host/session.py` 中 `prior_messages = self._prior_messages()` 出现在 `new_state()` 和 `agent_runtime.run_task()` / `stream_task()` 之前；`runtime/agent.py` 中 `RunStartEvent` 在 `_stream_task()` 内 emit。

实施时必须保留这个顺序并加测试。否则如果当前轮 `RunStartEvent` 先落 log，`completion.sequence > last RunStart.sequence` 会被当前轮 RunStart 吞掉，completion note 永远不出现。

具体规则：

1. 扫 event log，找到最后一个 `RunStartEvent` 的 sequence。
2. 收集 `TerminalProcessCompletedEvent.sequence > last_run_start.sequence` 的 events。
3. 在 `rebuild_prior_messages()` 结尾追加一个 `SystemMsg`，汇总最多 N 条 completion notes。
4. 一旦下一轮开始，新的 `RunStartEvent` sequence 会大于这些 completion events；它们自然不再注入。

为什么不用“最后一个 RunEndEvent”：

- completion event 发生在 previous `RunEndEvent` 之后。
- 如果 completion 在 turn N+1 运行期间发生，那么它的 sequence 会小于 turn N+1 的 RunEnd。
- 用 last RunEnd 作为 key，会导致这类 completion 在 turn N+2 被跳过，note 永远不出现。
- 用 last RunStart 可以覆盖“completion 落在下一轮运行期间”的场景，并自然在 turn N+2 注入一次。
- 代价是必须锁死上面的 rebuild-before-RunStart 编排契约。

与 failed/aborted note 的关系：

- failed/aborted note 仍按最后 terminal RunEnd status 注入。
- completion note 可与 failed/aborted note 共存，但应放在 failed/aborted note 之后。
- 如果同一时刻有多条 completion，只汇总最多 3 条，并提示可用 `terminal_process list` 查看更多。

必须补的测试：

- completion event 落在 turn N 的 RunEnd 之后、turn N+1 开始前，turn N+1 context 出现一次。
- completion event 落在 turn N+1 运行期间，turn N+2 context 出现一次。
- turn N+2 完成后，turn N+3 不再重复旧 completion note。

### 2.10 HostCore / replay events

`HostCore.replay_events()` 已经能按 `after_sequence` 读取 event。新增 event 后，desktop/server 可以通过现有接口看到 completion event。

P0 不新增专门 notification transport，但建议：

- `HostSession.summary()` 的 terminal process list 显示 completed tasks。
- `WorkspaceTerminalSupervisor.summary()` 显示 completed count。
- completion event 作为 UI notification 的未来输入。

### 2.11 Memory 不污染

Completion note 是 `SystemMsg`，不是 user/assistant 原话。P0 不把 completion event 写 memory。

需要确认：

- memory reflection 不应把 completion note 当成用户偏好。
- completion event 不应直接进入 memory candidate pipeline。

如果现有 memory hooks 会从 all events 挖 tool results，completion event 应只作为 runtime event，不作为 memory evidence 自动抽取。

### 2.12 P0-B 测试计划

`tests/test_terminal_runtime.py`：

- yielded process 完成后调用 completion recorder 一次。
- race 测试：命令很快完成，`yield_time_ms=0`，确保 completion event 不漏。
- kill process 后 completion event status 为 `killed`。
- session close / workspace shutdown / teardown kill 不产生 completion event。
- in-window completion 不产生 completion event。
- completion event output preview redacted。
- `_maybe_record_completion_event()` 不在持有 `state.lock` 时调用 recorder。

可通过 fake recorder 收集 events，不必启动 AgentRuntime。

`tests/test_tools.py`：

- `TerminalTool` 通过 `execute_with_context()` 把 origin context 注入 yielded process。
- yielded process 完成后 recorder 收到 `TerminalProcessCompletedEvent`，run_id/turn_id/reply_id/tool_call_id 正确。
- 旧的 `execute()` 直接调用仍可用，只是不发 completion event。

`tests/test_host_core.py`：

- real HostSession 中启动 yielded process，turn 结束。
- 等进程完成。
- `session.replay_events(after_sequence=...)` 能看到 `TerminalProcessCompletedEvent`。
- 下一轮 `prior_messages` 包含 completion note。
- 再下一轮 completion note 不重复出现。
- completion event 在 turn N+1 运行中发生，turn N+2 prior context 出现一次。
- workspace supervisor 共享 registry 时，session A 的 process completion 只进入 A 的 event log/context；session B 不看到 A 的 completion note。

`tests/test_host_transcript.py` 或现有 host core transcript 测试：

- 手工 seed `RunStart -> RunEnd -> TerminalProcessCompletedEvent`。
- `rebuild_prior_messages()` 包含 completion note。
- 再 seed later `RunStart/RunEnd(finished)` 后不再包含旧 note。
- 多条 completion 汇总且有上限。
- 手工 seed `RunStart(N) -> RunEnd(N) -> RunStart(N+1) -> Completion(from N) -> RunEnd(N+1)`，验证下一次 rebuild 会注入 note。

`tests/test_event_log_serialization.py` 如存在，或新加到 event serialization tests：

- `TerminalProcessCompletedEvent` dump/load round-trip。

验证命令：

```bash
uv run pytest tests/test_terminal_runtime.py -q
uv run pytest tests/test_tools.py -q
uv run pytest tests/test_host_core.py -q
uv run pytest tests/test_agent_runtime_loop.py -q
uv run pytest -q
```

## 3. 推荐 PR 切分

### PR1：Inventory / List / Log

文件：

- `src/pulsara_agent/runtime/terminal/models.py`
- `src/pulsara_agent/runtime/terminal/process.py`
- `src/pulsara_agent/runtime/terminal/manager.py`
- `src/pulsara_agent/tools/builtins/terminal_process.py`
- `src/pulsara_agent/runtime/permission.py`
- `src/pulsara_agent/host/session.py`
- `src/pulsara_agent/host/supervisor.py`
- tests。

验收：

- `terminal_process list` 可用。
- `terminal_process log` 可用。
- `poll/wait` 兼容旧字段。
- `terminal_process list/log/poll/wait` 在 `terminal_access=ask` 下不要求 approval。
- host summary/supervisor summary 看到 task inventory。

### PR2：Completion Event / Transcript Note

文件：

- `src/pulsara_agent/event/events.py`
- `src/pulsara_agent/runtime/terminal/models.py`
- `src/pulsara_agent/runtime/terminal/process.py`
- `src/pulsara_agent/runtime/terminal/session.py`
- `src/pulsara_agent/tools/executor.py`
- `src/pulsara_agent/tools/builtins/terminal.py`
- `src/pulsara_agent/host/transcript.py`
- tests。

验收：

- yielded process 完成后写 `TerminalProcessCompletedEvent`。
- live subscriber 在常驻 loop 下能收到。
- 下一轮 prior context 有轻量 note。
- completion 落在下一轮运行期间也会在再下一轮出现一次。
- note 不重复。
- race 不漏、kill 不漏、不双发。
- teardown kill 不产生 completion event。

## 4. 设计取舍

### 4.1 为什么先 list/log，再 completion event

completion note 最终要告诉模型“用 `terminal_process log/list` 看更多”。如果没有 list/log，completion event 只能把 output preview 塞进 transcript，容易污染上下文。

所以顺序应是：

1. 先让 task 可枚举、可查看。
2. 再让 completion 主动提醒。

同时，PR1 必须包含只读 `terminal_process` action 的 approval 豁免。否则 PR2 的 completion note 会把模型引到一个仍需反复审批的 `log/poll` 路径，体验自相矛盾。

### 4.2 为什么不恢复 `notify_on_complete`

当前 `TerminalTool` hard-cut `notify_on_complete` 是合理的：yielded process 默认就应该是 managed task。P0-B 应默认记录 completion event，不需要让模型选择。

未来若用户嫌通知太多，再加 runtime/host policy：

- notify all。
- notify errors only。
- notify never。
- per command override。

### 4.3 为什么不直接做 durable registry

当前 `ProcessRegistry` 是内存态，且内部时间使用 monotonic。durable registry 会牵涉：

- wall-clock timestamps。
- process pid persistence。
- app restart orphan detection。
- stale/live reconciliation。
- permission ownership restoration。

这是 P2，不应阻塞 P0 的 list/log/completion event。

### 4.4 为什么 completion note 用 SystemMsg

它不是用户原话，也不是 assistant 输出。沿用 failed/aborted note 的 SystemMsg 模式可以避免 memory reflection 把它当成用户偏好。

注意：

- 之前已经发现中段 SystemMsg 对部分 provider 不一定兼容。
- 当前 host transcript note 已采用 SystemMsg；若未来统一改成 top-level system prompt augmentation，completion note 应跟随同一投递机制。

P0-B 只负责 note derivation，不重新打开 message-level system vs top-level prompt 的设计争论。

## 5. 风险清单

### 5.1 Completion event 乱序 / note 时序

completion note 的 one-shot 投影依赖 `HostSession` 当前的 rebuild-before-RunStart 顺序。该顺序是实施契约，需要测试守住。

reader thread completion event 可能与后续 run events 并发。`RuntimeSession.emit_from_thread()` 由 event log 分配 sequence，publisher 会按 sequence drain。测试要覆盖 completion event sequence 在 RunEnd 后、下一轮 RunStart 前的常见路径。

还必须覆盖 completion event 在下一轮运行期间发生的路径；这是选择 last RunStart key 而不是 last RunEnd key 的主要理由。

### 5.2 Publisher 未绑定或 loop 关闭

CLI one-shot 结束后后台 process 完成，live publish 可能失败。P0 不解决，但 event log append 仍是核心。

若未来要可靠桌面 notification，需要 HostCore 常驻 loop 或独立 terminal supervisor event sink。

### 5.3 Memory 污染

不要把 completion note 做成 UserMsg。不要把 output preview 放太长。不要把 completion event 当 durable memory fact。

### 5.4 Context 膨胀

Completion note 最多汇总 3 条；每条最多一行。详细 output 让模型用 `terminal_process log` 查。

### 5.5 Owner isolation

`list/log` 必须沿用 `owner_host_session_id` 过滤。Workspace supervisor 共享 registry，但 host session 只能看到自己 owner 的 process。

completion event 也必须沿用 origin recorder：session A 启动的 process 完成后，只进入 session A 的 runtime event log。session B 即使共享 workspace supervisor，也不应看到 A 的 completion note。

### 5.6 Approval 摩擦

本计划不 defer 只读豁免。`terminal_process list/log/poll/wait` 在 `terminal_access=ask` 下应 allow，`write/submit/close_stdin/kill` 继续 ask。

### 5.7 RuntimeSession 强引用

`RuntimeThreadRecorder` 持有 `RuntimeSession` 强引用。yielded process 未完成前，如果把 recorder 存在 `TerminalProcessState`，就会 keep alive runtime session、event log 和 publisher。

这不是 P0 blocker，但要在实现中有意识地接受：

- 长驻 desktop/server host 下，这是 completion event 的代价。
- session close / workspace shutdown 应 kill owned process 并 suppress completion event，释放 recorder。
- 未来 durable registry 不能直接持久化 recorder。

### 5.8 Note 可检查性有限

finished process 会受 TTL 和 `max_finished_processes` 淘汰。因此 completion note 不能承诺一定能 inspect/log，只能说 “if still retained/available”。

## 6. 最小可交付定义

PR1 done：

- `terminal_process list` 返回当前 host session 的 running/finished task summary。
- `terminal_process log` 返回某 task output snapshot。
- `terminal_process list/log/poll/wait` 在 `terminal_access=ask` 下不要求 approval。
- host/supervisor summaries 含 task counts。
- 旧 terminal tests 全过。

PR2 done：

- yielded process final state 产生 exactly-once `TerminalProcessCompletedEvent`。
- event round-trip serialization 可用。
- host replay 能看到 event。
- 下一轮 transcript 有 one-shot completion note。
- completion 落在下一轮运行期间时，再下一轮 transcript 仍有 one-shot completion note。
- in-window command 不产生 completion event。
- teardown kill 不产生 completion event。

做到这两步后，Pulsara terminal 就从“能管理进程”跨到“能观察任务生命周期”的阶段，后续 output artifact protocol、interactive input、approval cache 都会更好接上。
