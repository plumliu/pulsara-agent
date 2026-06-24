# Failed / Aborted Recovery 实施计划（三态精化 + FAILED reflection gate）

_Created: 2026-06-24_

本文是 `FAILED_ABORTED_RECOVERY_SURVEY.zh.md` §7 的可执行实施版。survey 负责"为什么"，本文负责"改哪一行、按什么顺序、怎么测"。两个交付物：

1. **三态精化的 unfinished tool 摘要**：把 failed/aborted note 从静态常量升级为 `state × severity` 参数化摘要。
2. **FAILED reflection gate**（已锁定方案）：给 `DurableMemoryHooks._maybe_reflect()` 补一道与 `ABORTED` 对称的 `FAILED` gate。

两者都不改 canonical event 真值，不写 synthetic tool result，不引入 provider repair。

## 0. 当前代码事实（已逐条核实）

### 0.1 transcript 重建

`src/pulsara_agent/host/transcript.py`：

- `rebuild_prior_messages(event_log)` 是入口。
- `_last_terminal_run_note_target(events)`：找最后一个 `RunEndEvent`，若 status ∈ {`failed`,`aborted`} 返回 note target。
- `_completed_tool_call_ids_by_run(events)`：当前**只扫 `ToolResultEndEvent`**，返回 `dict[run_id, set[tool_call_id]]`。
- `_strip_unfinished_tool_calls(message, completed_tool_call_ids)`：保留有 completed result 的 `ToolCallBlock`，剥离孤儿；若剥离后只剩孤儿 tool call（无 `TextBlock`/`DataBlock`/`ToolResultBlock`）则返回 `None`（整条 assistant message 不 replay）。
- `_note_message(note_target, created_at)`：当前返回**静态** `SystemMsg`，内容是 `FAILURE_NOTE_TEXT` / `INTERRUPTED_NOTE_TEXT` 常量。
- note 注入逻辑：`_should_emit_terminal_note()` 在该 run 的 `RunEndEvent` 处插入 note。

note 常量（transcript.py 顶部）：

- `FAILURE_NOTE_TEXT`
- `INTERRUPTED_NOTE_TEXT`
- `_NOTE_STATUS`: `{"failed": (...), "aborted": (...)}`

### 0.2 事件 schema（`src/pulsara_agent/event/events.py`）

- `ToolCallStartEvent`: `tool_call_id`, `tool_call_name`。
- `ToolResultStartEvent`: `tool_call_id`, `tool_call_name`。
- `ToolResultEndEvent`: `tool_call_id`, `state`, `artifacts`。
- `RequireUserConfirmEvent`: `tool_calls: list[ToolCallBlock]`（每个 block 有 `.id` / `.name`）。
- 所有事件继承 `EventBase`，带 `run_id` / `turn_id` / `reply_id` / `sequence`。

### 0.3 executor 事件顺序（`src/pulsara_agent/tools/executor.py`）

- `ToolExecutor.execute()` 第一件事发 `ToolResultStartEvent`（`executor.py:29-34`），**早于** `registry.get()`（line 36）和真正执行工具。
- → `ToolResultStartEvent` 只证明"进入 executor（execution attempt started）"，**不证明副作用已产生**。

### 0.4 replay 不截断（`src/pulsara_agent/event_log/in_memory.py:52`）

- `replay(reply_id)` reduce 该 reply_id 的**全部**事件，不按 `ReplyEnd` / `RunEnd` 截断。
- 因此 abort 后线程 late 写入的 `ToolResultEndEvent` 仍会进入 replay。

### 0.5 OpenAI event builder 的空 name（`src/pulsara_agent/llm/adapters/openai/events.py:133`）

- 收到 tool_call delta 但还没 start 时，用 `tool_call_name=""` 合成 start。
- → failed mid-stream 时 `ToolCallStartEvent.tool_call_name` 可能为空。

### 0.6 severity 常量已存在（`src/pulsara_agent/runtime/permission.py`）

- `FILE_WRITE_TOOL_NAMES = frozenset({"edit_file", "write_file"})`（line 84）
- `TERMINAL_TOOL_NAMES = frozenset({"terminal", "terminal_process"})`（line 85）
- `TERMINAL_PROCESS_READ_ONLY_ACTIONS = frozenset({"list", "log", "poll", "wait"})`（line 86）

### 0.7 durable memory gate（`src/pulsara_agent/memory/hooks/durable.py`）

- `_maybe_reflect(state, safe_point)`（line 251）：`if state.status is LoopStatus.ABORTED: return []`（line 257）。**FAILED 未拦**。
- `_trigger_reasons()`（line 273）：仅当 `safe_point == "on_session_end"` 且 run 有 cheap hints 且无 memory attempt 时追加 `cheap_memory_hint`（line 281）。
- 调用点：`on_session_end()`（line 234）→ `_maybe_reflect(state, safe_point="on_session_end")`。
- `LoopStatus`（`runtime/state.py:23`）：`RUNNING` / `FINISHED` / `FAILED` / `ABORTED`。

## 1. 两个交付物的独立性

PR 可拆成两个独立提交，互不依赖：

- **Commit A：FAILED reflection gate**。极小，1 行逻辑 + 测试。低风险，建议先合。
- **Commit B：三态精化摘要**。中等，transcript.py 重构 + 新分类模块 + 测试。

下面先写 A（简单），再写 B（主体）。

---

## 2. Commit A：FAILED reflection gate

### 2.1 目标

`aborted` 与 `failed` 的 assistant text 都"may be partial or empty"，都不应喂 durable reflection。当前只 gate 了 `ABORTED`，`FAILED` 漏网。锁定方案：补对称 gate。

### 2.2 改动点

`src/pulsara_agent/memory/hooks/durable.py:257`

```python
# before
if state.status is LoopStatus.ABORTED:
    return []

# after
if state.status in {LoopStatus.ABORTED, LoopStatus.FAILED}:
    return []
```

`LoopStatus` 已在该文件导入（`_maybe_reflect` 已引用 `LoopStatus.ABORTED`），无需新增 import。

### 2.3 为什么够

- `_maybe_reflect` 是 durable reflection 的唯一收口：`on_session_end()` 是唯一调用点（durable.py:240）。在此 early-return 即可阻断 failed run 的 reflection。
- 不影响 `FINISHED`：正常完成的 run 仍照常 reflect。
- 不影响 execution evidence ledger：ledger 走的是 `after_tool_results` / persistence hook，与 reflection 是两条路径，本改动不碰它（ledger 仍记录真实 `ToolResultBlock`，这是工具结果事实，合理）。

### 2.4 测试

`tests/test_durable_memory_hooks.py`（或现有 durable hooks 测试文件，按仓库实际命名）：

1. `test_maybe_reflect_skips_failed_run`：构造 `state.status = LoopStatus.FAILED` + 满足 cheap_memory_hint 条件（`safe_point="on_session_end"`、有 cheap hints、无 memory attempt）→ 断言 `on_session_end()` 返回 `[]`，reflection 未触发。
2. `test_maybe_reflect_still_skips_aborted_run`：回归，确认 ABORTED 仍 gate。
3. `test_maybe_reflect_allows_finished_run`：回归，确认 FINISHED 仍能 reflect（防止误伤）。

验证：

```bash
uv run pytest tests/test_durable_memory_hooks.py -q
```

---

## 3. Commit B：三态精化摘要

### 3.1 两轴模型

note 摘要措辞 = `state × severity` 交叉。两轴正交：

- **state**：由事件判定，与工具类型无关。
- **severity**：由 tool name 判定。

### 3.2 新增模块：`src/pulsara_agent/host/unfinished_tools.py`

把分类逻辑抽成独立纯函数模块，便于单测、不污染 transcript.py。

#### 3.2.1 数据类型

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class UnfinishedState(StrEnum):
    PENDING_APPROVAL = "pending_approval_not_executed"
    STARTED = "started_no_completed_result"
    AMBIGUOUS = "ambiguous_failed_generation"


class ToolSeverity(StrEnum):
    READ_ONLY = "read_only"
    BOUNDED_WRITE = "bounded_write"
    TERMINAL = "terminal"
    UNKNOWN_EFFECT = "unknown_effect"


@dataclass(frozen=True, slots=True)
class UnfinishedToolCall:
    tool_call_id: str
    tool_name: str          # 已解析（可能是 "" → UNKNOWN_EFFECT）
    state: UnfinishedState
    severity: ToolSeverity
```

#### 3.2.2 事件聚合（按 run）

输入是该 run 的事件列表（调用方已按 run_id 过滤，见 §3.3）。

```python
def classify_unfinished_tool_calls(events: list[AgentEvent]) -> list[UnfinishedToolCall]:
    proposed: dict[str, str] = {}          # id -> name from ToolCallStartEvent
    completed: set[str] = set()            # all-events 语义，见 §3.2.4
    attempted: set[str] = set()
    pending: dict[str, str] = {}           # id -> name from RequireUserConfirmEvent
    result_start_names: dict[str, str] = {}

    for event in events:
        if isinstance(event, ToolCallStartEvent):
            # 不覆盖已有非空 name（防止 delta-before-start 的 "" 覆盖真实 name）
            if event.tool_call_id not in proposed or not proposed[event.tool_call_id]:
                proposed[event.tool_call_id] = event.tool_call_name
        elif isinstance(event, ToolResultStartEvent):
            attempted.add(event.tool_call_id)
            if event.tool_call_name:
                result_start_names[event.tool_call_id] = event.tool_call_name
        elif isinstance(event, ToolResultEndEvent):
            completed.add(event.tool_call_id)
        elif isinstance(event, RequireUserConfirmEvent):
            for block in event.tool_calls:
                pending[block.id] = block.name

    unfinished_ids = set(proposed) - completed
    result: list[UnfinishedToolCall] = []
    for tool_call_id in unfinished_ids:
        name = _resolve_name(
            tool_call_id,
            proposed=proposed,
            pending=pending,
            result_start_names=result_start_names,
        )
        state = _classify_state(tool_call_id, attempted=attempted, pending=pending)
        severity = _classify_severity(name)
        result.append(UnfinishedToolCall(tool_call_id, name, state, severity))
    return result
```

#### 3.2.3 state 判定（优先级：attempted > pending > ambiguous）

```python
def _classify_state(tool_call_id, *, attempted, pending) -> UnfinishedState:
    if tool_call_id in attempted:
        return UnfinishedState.STARTED          # 进入 executor，may have partially run
    if tool_call_id in pending:
        return UnfinishedState.PENDING_APPROVAL  # 仅审批挂起，did not execute
    return UnfinishedState.AMBIGUOUS             # 仅 proposed（含残片）
```

`attempted` 必须先于 `pending` 判：approve 后进入 executor 再被 abort 的 call，既在 pending 又在 attempted，必须落 STARTED（state 2），否则会误报"did not execute"。

#### 3.2.4 completed 的序列契约（all-events 语义）

`completed` = 该 run 事件流里**所有** `ToolResultEndEvent`，**不按 RunEnd 截断**。

原因（survey §7.2.1 已论证，代码已核实）：

- `stop_current_turn()` cancel run task，但 `asyncio.to_thread` 工具线程不可强停，可能在 `RunEnd(aborted)` 之后通过 thread recorder 写入 late `ToolResultEndEvent`。
- `event_log.replay(reply_id)`（`in_memory.py:52`）不截断，late result 会进入 replay。

→ 采 all-events 语义：late result 进 `completed`，被 strip 逻辑保留、被 replay 如实展示，并自动移出 unfinished 摘要（自愈）。若 rebuild 早于 late result 落 log，该轮暂列 unfinished，下一轮自然消失——措辞本就是"may have partially run / may still be running"，与之不冲突。

实现上无需特殊代码：`classify_unfinished_tool_calls` 遍历传入的全部 run 事件、`completed` 收集所有 `ToolResultEndEvent`，天然就是 all-events 语义。**关键是调用方传入的事件不能预先按 RunEnd 截断**（见 §3.3 注意事项）。

#### 3.2.5 name fallback 链

`ToolCallStartEvent.tool_call_name` 可能为空（§0.5）。fallback 链：

```python
def _resolve_name(tool_call_id, *, proposed, pending, result_start_names) -> str:
    for candidate in (
        proposed.get(tool_call_id, ""),
        pending.get(tool_call_id, ""),
        result_start_names.get(tool_call_id, ""),
    ):
        if candidate:
            return candidate
    return ""   # 交给 severity 归 UNKNOWN_EFFECT
```

注意：survey 提到第 4 级 fallback 是 replay 出的 `ToolCallBlock.name`。但 replay 的 block 同样源于 `ToolCallStartEvent`，name 为空时 block.name 也为空，不提供额外信息。因此实现上三级事件级 fallback 已足够；保留"仍为空 → UNKNOWN_EFFECT"作为终点。

#### 3.2.6 severity 分桶

复用 permission.py 常量，避免重复定义。V1 把整个 `terminal_process` 归 `terminal`（不按 action 细分）。

```python
from pulsara_agent.runtime.permission import FILE_WRITE_TOOL_NAMES, TERMINAL_TOOL_NAMES

_READ_ONLY_TOOL_NAMES = frozenset({"read_file", "search_files", "artifact_read"})

def _classify_severity(tool_name: str) -> ToolSeverity:
    if not tool_name:
        return ToolSeverity.UNKNOWN_EFFECT
    if tool_name in TERMINAL_TOOL_NAMES:        # terminal + terminal_process（整体）
        return ToolSeverity.TERMINAL
    if tool_name in FILE_WRITE_TOOL_NAMES:
        return ToolSeverity.BOUNDED_WRITE
    if tool_name in _READ_ONLY_TOOL_NAMES:
        return ToolSeverity.READ_ONLY
    return ToolSeverity.UNKNOWN_EFFECT          # 未知工具保守处理
```

注意：`terminal_process` 的只读 action（list/log/poll/wait）在 V1 也归 TERMINAL。这与 permission 层的 approval 豁免（permission.py:216）是两个维度——approval 豁免决定"是否问用户"，severity 决定"中断后如何描述风险"。只读 action 极少成为 unfinished 停点，保守归 terminal 的过度警告无害。

#### 3.2.7 摘要渲染

```python
_MAX_LISTED_TOOLS = 3

def render_unfinished_summary(
    unfinished: list[UnfinishedToolCall],
    *,
    run_status: str,   # "failed" | "aborted"
) -> str:
    if not unfinished:
        return ""
    names = [u.tool_name or "unknown" for u in unfinished]
    listed = names[:_MAX_LISTED_TOOLS]
    suffix = f" +{len(names) - _MAX_LISTED_TOOLS} more" if len(names) > _MAX_LISTED_TOOLS else ""
    count_phrase = (
        f"The turn had {len(unfinished)} unfinished tool call"
        f"{'s' if len(unfinished) != 1 else ''}: {', '.join(listed)}{suffix}."
    )
    wording = _wording_for(unfinished, run_status=run_status)
    return f" {count_phrase} {wording}"
```

`_wording_for` 按措辞矩阵选句（见 §3.5）。摘要规则：只列名 + 数量、不列 arguments、最多前三 + `+N more`。

### 3.3 transcript.py 改动

`src/pulsara_agent/host/transcript.py`

#### 3.3.1 `_note_message` 参数化

当前 `_note_message(note_target, created_at)` 返回静态常量 `SystemMsg`。改为接收该 run 的 unfinished 摘要并拼接：

```python
def _note_message(
    note_target: _TerminalRunNoteTarget,
    *,
    created_at: str | None,
    unfinished_summary: str,    # 新增
) -> SystemMsg:
    return SystemMsg(
        name="pulsara",
        content=note_target.text + unfinished_summary,   # 基础框架 + 摘要
        id=f"{note_target.id_prefix}:{note_target.run_id}",
        created_at=created_at,
        metadata={"run_id": note_target.run_id, "kind": note_target.kind},
    )
```

基础框架（`FAILURE_NOTE_TEXT` / `INTERRUPTED_NOTE_TEXT`）**保持不变**，摘要追加在后。

#### 3.3.2 在 `rebuild_prior_messages` 内计算 unfinished

`rebuild_prior_messages(event_log)` 已经 `events = event_log.iter()` 拿到全量事件。note_target 已知其 `run_id`。新增：

```python
# 按 note_target.run_id 过滤事件，传给分类器
note_run_events = [e for e in events if e.run_id == note_target.run_id]
unfinished = classify_unfinished_tool_calls(note_run_events)
unfinished_summary = render_unfinished_summary(unfinished, run_status=last_run_end.status)
```

**注意事项（与 all-events 语义的耦合）**：`note_run_events` 必须是该 run 的全部事件，包括 `RunEndEvent` 之后的 late events。`event_log.iter()` 返回的是完整 log，按 `run_id` 过滤不会截断 RunEnd 之后的 late `ToolResultEndEvent`（它们带同一个 run_id）。这正是 all-events 语义生效的前提，不要在此处加 RunEnd 截断。

两个注入点都要传 `unfinished_summary`（当前代码有两处 `_note_message` 调用：`_should_emit_terminal_note` 命中处，和末尾 `note_target.run_id not in noted_runs` 兜底处）。

#### 3.3.3 与 `_strip_unfinished_tool_calls` 的关系

`_strip_unfinished_tool_calls` 用的 `completed_tool_call_ids_by_run`（只扫 `ToolResultEndEvent`）已经是 all-events 语义（遍历全量 events），与新分类器一致。**不需要改 strip 逻辑**——它和摘要分类器是互补的两个消费者，共享同一套 completed 定义。

### 3.4 与 terminal completion note 的协调

survey §7.2.2 已论证不相交，实现上自动成立：

- yielded terminal 的初始 tool call 有 `ToolResultEndEvent`（yield 即返回结果）→ 进 `completed` → 不进 unfinished 摘要。
- 其后续完成由 `TerminalProcessCompletedEvent` → `_completion_note_after_last_run_start` 单独投影。

因此摘要里的 terminal 项与 completion note 覆盖的集合不相交，无需额外协调代码。但要有一个测试守住这个不变量（§3.6 测试 #10）。

补充限定：foreground terminal（没到 yield 就被 abort / fail）只有在最终仍没有 late `ToolResultEndEvent` 时，才会进入 unfinished 摘要。若工具线程稍后写入真实 result，all-events `completed` 集合会自动把它移出摘要。

### 3.5 措辞矩阵

| | read_only | bounded_write | terminal |
| --- | --- | --- | --- |
| **PENDING_APPROVAL** | 省略 | pending approval, did not execute | pending approval, did not execute |
| **STARTED** | 一句带过 | may have partially run; re-read to verify | may have partially run and may still be running in the background; verify before continuing |
| **AMBIGUOUS** | 省略 | proposed but uncertain; re-evaluate | proposed; uncertain whether it ran; verify |

`UNKNOWN_EFFECT`（任意 state）：单一保守措辞 `proposed a tool call whose effect is unknown; verify before continuing`。

`failed` 与 `aborted` 的差异在**基础框架**（`FAILURE_NOTE_TEXT` vs `INTERRUPTED_NOTE_TEXT`，已存在），摘要措辞共用矩阵。failed 的 ambiguous 比例通常更高（failure 可能发生在 generation 边界），矩阵已用 "uncertain / proposed" 覆盖。

`_wording_for` 的实现建议：当一个 run 同时有多种 (state, severity)，取**最保守**的一条作为整体 wording（terminal STARTED > bounded_write STARTED > pending > ambiguous），避免拼接多句使 note 过长。具体优先级可在实现时定，测试覆盖"混合时取最保守"。

### 3.6 测试

`tests/test_host_core.py`（沿用现有 `test_rebuild_prior_messages_*` 命名），或新建 `tests/test_unfinished_tools.py` 测纯函数分类器。

纯函数（`test_unfinished_tools.py`，不依赖 HostSession）：

1. pending approval（有 `RequireUserConfirmEvent`、无 `ToolResultStartEvent`）→ state PENDING_APPROVAL。
2. started（有 `ToolResultStartEvent`、无 `ToolResultEndEvent`）→ state STARTED。
3. approve 后 abort（既 pending 又 attempted）→ state STARTED（不误判 pending）。
4. 仅 `ToolCallStartEvent` → state AMBIGUOUS。
5. 空 `tool_call_name` 且无 fallback 来源 → severity UNKNOWN_EFFECT。
6. name fallback：`ToolCallStartEvent` name 空但 `RequireUserConfirmEvent.tool_calls[].name` 有 → 解析成功，不归 UNKNOWN_EFFECT。
7. severity 分桶：`terminal` / `terminal_process` → TERMINAL；`write_file`/`edit_file` → BOUNDED_WRITE；`read_file` → READ_ONLY。
8. terminal STARTED 措辞含 "and may still be running in the background"。
9. bounded_write STARTED 措辞过去式、不含 still running。
10. 工具数 > 3 → `+N more`。
11. late ToolResultEnd（completed 跨 RunEnd）：事件序列含 RunEnd 之后的 `ToolResultEndEvent` → 该 id 进 completed、不进 unfinished。

集成（`test_host_core.py`）：

12. aborted run 有 pending terminal approval → 下一轮 note 含基础框架 + "1 unfinished tool call: terminal" + did not execute。
13. failed run 两个 proposed-only → note 列两名 + ambiguous wording。
14. yielded terminal（已有 `ToolResultEndEvent`）→ 不进 unfinished 摘要；completion note 可单独出现（不相交）。
15. 回归：现有 `_strip_unfinished_tool_calls` 行为不变（孤儿仍剥离、只剩孤儿仍整条省略）。
16. note 不含 tool arguments。

验证：

```bash
uv run pytest tests/test_unfinished_tools.py -q
uv run pytest tests/test_host_core.py -q
uv run pytest -q
```

## 4. 实施顺序

1. **Commit A**（FAILED gate）：durable.py 1 行 + 3 个 durable 测试。先合，低风险。
2. **Commit B Step 1**：新建 `host/unfinished_tools.py`（数据类型 + 分类器 + 渲染），配 `test_unfinished_tools.py`。纯函数，无副作用，独立可测。
3. **Commit B Step 2**：改 `transcript.py`（`_note_message` 参数化 + `rebuild_prior_messages` 计算并传入摘要），配 `test_host_core.py` 集成测试。
4. 全量回归 `uv run pytest -q` + `uv run ruff check`。

## 5. 不做（明确排除）

- 不写 synthetic tool result 进 canonical event log（survey §7.3）。
- 不做 provider-only repair（survey §7.4，留作未来逃生口）。
- 不解析 terminal_process action 细分 severity（V1 整体归 terminal）。
- 不改 `_strip_unfinished_tool_calls` 的 strip 行为（仅复用其 completed 定义）。
- 不动 execution evidence ledger 的真实 `ToolResultBlock` 记录路径。

## 6. 风险

- **note 过长**：`_wording_for` 取最保守单句、摘要列名 ≤ 3，控制长度。已有 `tool_result_context_chars` 预算对 note 无直接裁剪（note 是 SystemMsg，不是 tool result），但仍应保持简短。
- **late result 时序自愈的可观察性**：测试 #11 守住"completed 跨 RunEnd"。若未来换 event log 后端，需确认其 `iter()` / `replay()` 同样不按 RunEnd 截断（Postgres 后端 `iter(run_id=...)` 返回该 run 全部事件，符合）。
- **UNKNOWN_EFFECT 误判**：name 解析失败时保守归 UNKNOWN_EFFECT 而非 read_only，宁可过度警告，不可漏报副作用。
