# Terminal 输出三层契约

_Created: 2026-06-24_

这份文档定义 terminal 输出的长期产品契约。它不是单个 PR 的 implementation plan，而是后续 hard cut 的目标形状：让 terminal 输出不再继续长出第四、第五条侧路，同时和统一 Tool Result Artifact 协议、completion event、transcript recovery 保持一致。

核心原则：

- **preview 不是完整事实。**
- **artifact 是完整输出的唯一权威来源。**
- **completion event 只承载生命周期元数据，不承载完整输出。**
- **memory / transcript / recovery 不再把 stdout/stderr preview 当成完整输出。**

相关代码入口：

- [src/pulsara_agent/tools/builtins/terminal.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/terminal.py)
- [src/pulsara_agent/tools/builtins/terminal_process.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/terminal_process.py)
- [src/pulsara_agent/runtime/terminal/process.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/terminal/process.py)
- [src/pulsara_agent/runtime/tool_artifacts.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/tool_artifacts.py)
- [src/pulsara_agent/runtime/context.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/context.py)
- [src/pulsara_agent/host/transcript.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/transcript.py)
- [src/pulsara_agent/memory/canonical/ledger.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/ledger.py)

---

## 1. 为什么需要三层契约

terminal 输出现在天然有三种不同需求：

1. 用户和模型需要看到命令正在输出什么。
2. 系统需要保留完整 stdout/stderr，避免长输出被上下文裁剪后永久丢失。
3. yielded/background process 需要在原始 tool call 结束之后继续报告生命周期事实。

这三件事不能再混成一个 `"output"` 字段。混在一起会导致：

- preview 被误当成完整输出。
- transcript note / memory ledger / recovery classifier 各自解析 stdout。
- 大输出既被 executor 归档，又被 ledger 二次归档。
- background process 完成后只能靠自然语言 note 表达状态。

因此 terminal 输出必须被明确拆成三层。

---

## 2. Layer 1: Streaming Preview

### 2.1 职责

Layer 1 是实时 stdout/stderr preview。

它服务两个读者：

- UI：让用户看到命令正在运行、正在输出。
- 当轮模型：让模型获得 bounded observation，必要时决定继续 poll、wait、log 或给出最终答复。

### 2.2 生命周期

Streaming preview 的生命周期只覆盖当前 tool execution / event replay。

它可以出现在：

- `ToolResultTextDeltaEvent`
- `ToolResultBlock.output`
- provider replay 的 tool result content
- transcript 中的 bounded preview

但它不是完整输出的权威来源。

### 2.3 完整性语义

Layer 1 不保证完整性：

- 可能被 `max_output_chars` 裁剪。
- 可能被 context budget 再裁剪。
- 可能只覆盖 yielded process 在 yield 前已经输出的部分。
- 可能只是 terminal JSON payload 里的 `output` 字段，而不是原始 stdout/stderr 全文。

模型可见提示必须把 preview 当作 preview，而不是 full output。

### 2.4 禁止事项

Layer 1 不允许承担这些职责：

- 不作为 durable memory 的完整证据。
- 不作为 run timeline / recovery 的唯一事实来源。
- 不作为 large output 的唯一保留方式。
- 不直接替代 artifact read。

---

## 3. Layer 2: Tool Result Artifact

### 3.1 职责

Layer 2 是完整输出的唯一权威来源。

对 terminal 来说，artifact 存储的是 stdout/stderr 的完整文本或当前可取得的完整日志切片。对其他未来长输出工具来说，artifact 存储的是该 tool result 的长文本或二进制结果。

这层解决的问题是：上下文 preview 可以裁剪，但完整输出不能因为 preview 裁剪而永久丢失。

### 3.2 归档者

长期契约是：

**`ToolResultArtifactService` 是唯一归档入口。**

这句话比“executor 侧统一归档”更准确，因为 terminal 有 foreground 和 yielded/background 两类路径：

- foreground terminal：tool call 返回时，executor 调用 `ToolResultArtifactService.process_result()`，归档完整输出 candidate。
- yielded/background terminal：进程完成发生在 executor 生命周期之外。当前完整日志应由 `terminal_process log` 产生 artifact；未来如果要在 completion path 自动归档，也必须复用同一个 artifact service，而不是新增侧路。

### 3.3 阈值

默认阈值使用统一 Tool Result Artifact policy：

- `archive_threshold_bytes = 8_000`
- `complete_preview_body_chars = 32_000`
- `large_preview_chars = 8_000`
- `huge_output_chars = 200_000`
- `huge_preview_chars = 4_000`
- `streaming_live_head_cap_chars ≈ 2_466`（默认 huge tier 在扣除 truncation notice 后的可见 head 上限）
- `tool_result_message_context_chars = 36_000`

归档阈值按 UTF-8 bytes 计算；preview、`artifact_read.offset_chars` 和 head/tail 边界按 Python 字符数计算。terminal 不应单独定义 1KB、2KB、12KB 这类特殊阈值。若未来要调整阈值，应作为全局 artifact policy 调整，而不是 terminal-only 特例。

terminal 的 `max_output_chars` 可以把本次调用的 preview budget 调小；它不能提高全局上限。默认值是 32k，表示中等输出可被完整/近似完整展示，不代表巨大 streaming 输出可以先实时写入 32k head。该参数在 terminal 执行层和 artifact service 中必须按同一套 schema 解释：过小正整数 clamp 到 `MIN_TERMINAL_OUTPUT_CHARS`，超过默认上限 clamp 到 `DEFAULT_MAX_OUTPUT_CHARS`。

### 3.4 artifact ref 语义

模型可见结果不直接内联完整 artifact，而是提供 `ToolResultArtifactRef`：

```json
{
  "artifact_id": "artifact:tool-result:run:call:combined_output:0",
  "role": "combined_output",
  "media_type": "text/plain; charset=utf-8",
  "size_bytes": 124532,
  "stored_complete": true,
  "preview": {
    "preview_policy": "head_tail",
    "preview_chars": 8192,
    "original_chars": 128734,
    "original_bytes": 180245,
    "omitted_middle_chars": 120542,
    "visible_head_chars": 5324,
    "visible_tail_chars": 2868,
    "read_more": {
      "tool": "artifact_read",
      "artifact_id": "artifact:tool-result:run:call:combined_output:0",
      "suggested_offset_chars": 5324,
      "suggested_max_chars": 20000
    }
  },
  "read_more": {"tool": "artifact_read"}
}
```

`ToolResultBlock.artifacts` 是 list。一个 tool call 可以产生多个 artifact，例如：

- `combined_output`
- `stdout`
- `stderr`
- `diagnostics`
- `report`
- `screenshot`

只有作为 preview source 的 primary text artifact ref 挂 `preview`；其他 artifact ref 默认 `preview=null`，除非它们各自有独立 preview。旧 event log 中没有 `preview` 字段的 artifact refs 仍必须能 replay / inspect / compact。

ledger / timeline / transcript 不能把它降级成“只有第一个 artifact”。可以保留 primary artifact 作为兼容入口，但完整集合必须保留。

### 3.5 read-side 规则

读侧必须遵守：

- transcript 可以展示 artifact ref，但默认不主动读取完整 artifact。
- memory ledger 只保存 artifact ref / graph node，不再 `put_text()` 二次归档。
- recovery classifier 不能把 preview 当作完整输出；需要全文时显式读取 artifact。
- `artifact_read` 是模型读取完整内容的显式工具入口。

换句话说：**artifact 是权威，但不是默认塞进上下文的全文。**

---

## 4. Layer 3: Terminal Completion Event

### 4.1 职责

Layer 3 是 terminal process 的结构化生命周期事实。

它回答的问题不是“完整输出是什么”，而是：

- 进程是否完成？
- 怎么完成？
- exit code 是多少？
- 是否超时？
- 是否被用户 kill？
- 是否来自 teardown / watchdog？
- 这个完成事件是否应该投影给模型？

### 4.2 事件

核心事件是 `TerminalProcessCompletedEvent`。

建议字段范围：

- `process_id`
- `terminal_session_id`
- `command`
- `status`
- `exit_code`
- `cwd`
- `timed_out`
- `duration_seconds`
- `backend_type`
- `io_mode`
- `tool_call_id`
- `kill_reason` / `completion_reason`
- bounded `output_preview`
- `output_truncated`

`output_preview` 可以存在，但它仍属于 preview 语义，不是完整输出。

### 4.3 不应包含的内容

Completion event 不应承载：

- 完整 stdout/stderr。
- 完整 env snapshot。
- 大段 shell metadata。
- 大 artifact body。

env 信息很容易又大又敏感。可保留 bounded/debug metadata，但不应成为 completion event 的核心契约字段。

### 4.4 抑制规则

completion event / completion note 必须区分 kill reason：

- 用户显式 kill：可以发 completion event，因为这是用户可感知、可恢复的生命周期事实。
- teardown kill：不应投影成模型可见 completion note。
- lifetime watchdog kill：默认不应投影成普通用户可恢复 completion note，除非未来产品明确要暴露 watchdog 状态。

这条规则的目的不是隐藏事实，而是避免 session close / cleanup 产生一堆误导模型的“任务完成”提示。

### 4.5 read-side 规则

Completion event 的主要消费者：

- transcript completion note
- run timeline
- recovery classifier
- UI process history

这些消费者应该读结构化字段，而不是从 stdout preview 里猜状态。
transcript completion note 只是一条生命周期提示：它不自动读取 artifact，不摘要完整日志，也不能把 bounded preview 说成 full output。需要完整日志时，模型必须显式调用 `terminal_process log` 或 `artifact_read`。

### 4.6 Completion Note 投影契约

`TerminalProcessCompletedEvent` 是后台进程完成事实的 canonical source。transcript completion note 只是从 event log 派生出来的轻量投影，目的是让下一轮模型知道“有后台 terminal process 后来结束了”。

completion note 允许包含：

- `process_id`
- `status`
- `exit_code`
- retained log 的读取入口提示，例如 `terminal_process log`
- 有限 overflow count，例如还有多少个 completed process 未逐条列出

completion note 不允许包含：

- 完整 stdout/stderr
- bounded `output_preview`
- command 全文
- cwd / env / shell metadata
- artifact body
- recovery 指令或“可以直接相信结果已完成”的强语义

completion note 必须满足：

- 明确自己是 `lifecycle-only`，不是 full output。
- 引导模型显式调用 `terminal_process log` 或 `artifact_read` 读取 retained output。
- 不进入 durable memory fact。
- 不替代 run timeline 或 terminal process view 的权威事实。
- 不为 teardown / watchdog suppressed completion 生成模型可见 note。
- 多个 completion 只列出最多 `_MAX_COMPLETION_NOTES` 个，超出部分只用 count summary。

### 4.7 Kill Reason / Completion Suppression 契约

terminal kill reason 是 terminal lifecycle 的结构化状态，不是 transcript / host 自己推断的文案规则。

允许的 kill reason：

- `user_tool_kill`：用户或模型通过 `terminal_process kill` 显式停止进程。
- `teardown`：session、workspace、owner cleanup 或 live-process-limit cleanup。
- `lifetime_watchdog`：runtime lifetime watchdog 自动清理。

completion suppression 只在 terminal lifecycle 层决定：

- `user_tool_kill` 不 suppress。yielded process 被用户显式 kill 后，可以发 `TerminalProcessCompletedEvent(status="killed", completion_reason="user_tool_kill")`，并可在下一轮投影轻量 completion note。
- `teardown` suppress。cleanup 不应生成模型可见 completion event / note。
- `lifetime_watchdog` suppress。watchdog cleanup 不应伪装成普通用户可恢复 completion note。

`TerminalProcessCompletedEvent.completion_reason` 是唯一模型可见的结构化 reason 字段。新代码不应把 reason 塞进 event `metadata`，也不应让 transcript / host 从 `metadata` 或进程退出码里反推 kill 类型。

---

## 5. 三层关系

| Layer | 是否实时 | 是否完整 | 是否持久 | 主要消费者 | 权威问题 |
| --- | --- | --- | --- | --- | --- |
| Streaming Preview | 是 | 否 | 否 | UI / 当轮模型 / transcript preview | “现在看到了什么？” |
| Tool Result Artifact | 否 | 是 | 是 | artifact_read / memory / audit / recovery 按需读取 | “完整输出是什么？” |
| Completion Event | 否 | 不适用 | 是 | transcript note / timeline / recovery / UI history | “进程后来怎么结束？” |

重要边界：

- preview 可以引用 artifact，但不能替代 artifact。
- completion event 可以带 bounded preview，但不能承载完整输出。
- artifact 可以保存完整输出，但不应该默认全部注入上下文。

---

## 6. Foreground terminal 生命周期

foreground terminal 的理想路径：

1. terminal tool 启动进程。
2. 进程运行期间发 streaming preview。
3. 进程结束。
4. terminal tool 返回 `ToolExecutionResult`，其中包含 bounded output payload 和 full-output artifact candidate。
5. executor 调用 `ToolResultArtifactService.process_result()`。
6. executor 发 `ToolResultEndEvent(artifacts=[...])`。
7. transcript/context 渲染 bounded preview + artifact ref envelope。
8. memory ledger 从 `ToolResultBlock.artifacts` 建 graph ref，不再自行归档全文。

这条路径里，完整输出只归档一次。

---

## 7. Yielded/background terminal 生命周期

yielded/background terminal 的路径不同：

1. terminal tool 启动进程。
2. 到达 yield 条件时，tool call 返回 running result。
3. 当轮模型看到 process id、当前 preview 和必要的 artifact ref。
4. 进程继续在后台运行。
5. 进程完成后，runtime 发 `TerminalProcessCompletedEvent`。
6. transcript 可在下一轮投影 bounded completion note。
7. 如需完整日志，模型调用 `terminal_process log`。
8. `terminal_process log` 通过 `ToolResultArtifactService` 产出完整/当前日志 artifact。

当前契约的关键诚实点：

- yield 时的 artifact 如果存在，只代表 yield 当时可取得的输出，不一定代表最终完整输出。
- 最终完整日志应通过 `terminal_process log` 读取。
- completion note 不是 log summary；它只告诉模型后台进程后来怎么结束。
- 如果未来要在 process completion 时自动归档最终完整输出，也必须复用 `ToolResultArtifactService`，不能让 process reader 直接 `put_text()`。

---

## 8. 废弃路径

以下路径应被视为 legacy / prohibited：

1. **Memory ledger 直接归档 terminal 输出**

   `ExecutionEvidenceLedger` 不应再对长输出调用 `archive.put_text()`。它只能消费 executor / artifact service 已经产生的 artifact refs。

2. **transcript 直接把 stdout/stderr 当完整输出**

   transcript 可以展示 preview，但不能把 preview 解释成完整 terminal result。需要完整输出时必须通过 artifact ref。

3. **completion event 承载完整 output body**

   completion event 是生命周期事实，不是 output storage。

4. **新增 terminal output 侧路**

   不再新增诸如 `.pulsara/terminal-output/`、独立 log file、memory-only blob、transcript-only full output 之类的第四路径。

---

## 9. 当前实现对齐状态

已经接近契约的部分：

- `ToolResultArtifactService` 已经是 executor 路径的 artifact 归档入口。
- `ToolResultEndEvent` 已经可以携带 `artifacts`。
- `ToolResultBlock.artifacts` 已经是 list。
- `artifact_read` 已经提供显式读侧工具。
- ledger 已开始从 artifact refs 建 graph node，而不是自行归档大输出。
- threshold 已统一到 8KB。

仍需继续收口的部分：

- yielded/background process completion path 不应自己归档；如果要自动归档最终日志，需要接入 `ToolResultArtifactService`。
- transcript completion note 仍是自然语言投影，未来应尽量消费结构化 recovery / completion state。
- terminal preview / artifact / completion event 的 wording 需要在 tool descriptions 和 prompt 中统一。
- external execution / 非 executor 产生的大 `ToolResultBlock` 必须带 artifact refs；否则 persistence hook 应拒绝而不是重开归档路径。

---

## 10. 测试要求

这份契约应由以下测试守住：

1. foreground terminal 大输出产生 artifact ref，且 `artifact_read` 能读回完整输出。
2. `ToolResultEndEvent.artifacts` 能 round-trip 到 `ToolResultBlock.artifacts`。
3. context renderer 使用全 context aggregate tool-result budget；先保护 preview body，再包 artifact envelope，artifact refs 不被裁掉；预算耗尽后的 artifact tool result 只能输出有界极简 ref / read-more envelope，不能重复 bypass aggregate budget。
4. ledger 消费 artifact refs，不对长 output 自行 `put_text()`。
5. ledger 对 multi-artifact refs 建所有 `Artifact` 图节点。
6. persistence hook 拒绝大输出但无 artifact refs 的 external / non-executor `ToolResultBlock`。
7. yielded process completion event 不承载完整输出。
8. `terminal_process log` 可以为 yielded process 产出 artifact ref。
9. teardown / watchdog kill 不产生误导性的模型可见 completion note。
10. streaming terminal live head 不超过固定保守 cap；中等输出在 finish 补齐，巨大输出只补 marker + tail。
11. 多字节文本按 bytes 触发归档，但 preview/read offset 按 chars 正确工作。
12. preview metadata 在 `ToolResultArtifactRef.preview` 与 tool-result artifact index metadata 中一致。

---

## 11. 推荐下一步

下一步不应该直接新增 terminal feature，而应先做一次 terminal contract hardening：

1. 审核 terminal / terminal_process tool description，把 preview / artifact / completion event 的边界写清楚。
2. 审核 transcript note，避免把 preview 说成 full output。
3. 审核 yielded process 的最终日志路径，确认 `terminal_process log` 是唯一完整日志读取入口。
4. 如需 completion 自动归档最终日志，先写 implementation plan，明确如何把 process reader 接到 `ToolResultArtifactService`，并禁止直接 `archive.put_text()`。

这份三层契约的目标不是减少 terminal 能力，而是让每一层只做自己的事。
