# Pulsara Context Compaction 时机与 REPL 体验优化设计

## 0. 结论

Pulsara 当前最大的 compaction 体验问题不是 summary 质量，而是 **auto compact 的执行时机错放到了上一轮回复结束后的后台任务**。

这会造成一个很具体的 REPL 破绽:

```text
assistant: abcd
pulsara> context compaction completed: ...
```

用户看到 `pulsara>` 时会自然认为系统已经 idle、可以输入；但后台 auto compact 完成后又把 notice 打进当前输入行，造成 prompt 污染和光标错位。这不是简单 print 美化能彻底解决的问题，而是 runtime 时机问题。

推荐下一步:

1. **取消 run-end background auto compact**。
2. **把 auto compact 收敛到下一轮 user turn 的 preflight**。
3. 如果用户提交新问题时达到阈值，先 compact，再自动继续处理这条用户输入，用户不需要再次回车。
4. 保留 manual `:compact` 的立即执行语义。
5. 后续再做 Codex-like mid-turn inline compact，让长工具循环/多轮采样在最终回复前完成 compact。

这样之后，auto compact 不再是“prompt 出现后的后台幽灵”，而是 turn pipeline 的正式步骤。

## 1. Codex 是怎么做 compact 时机的

调研目录:

- `/Users/plumliu/Desktop/python_workspace/codex`

关键代码:

- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/session/turn.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/tasks/compact.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/compact.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/compact_remote.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core/src/compact_remote_v2.rs`
- `/Users/plumliu/Desktop/python_workspace/codex/codex-rs/app-server/tests/suite/v2/compaction.rs`

Codex 的关键设计点:

1. compact 是 first-class runtime task/item
   - `TaskKind::Compact`
   - `NonSteerableTurnKind::Compact`
   - `TurnItem::ContextCompaction`
   - app-server 会发 `ContextCompaction` item started/completed。

2. auto compact 发生在 turn pipeline 内
   - `run_turn()` 开头先调用 `run_pre_sampling_compact(...)`。
   - 如果 pre-turn token status 已越线，就先 compact，再继续当前 turn。
   - 用户提交的新输入不会丢，也不需要再按一次回车。

3. Codex 支持 mid-turn compact
   - 采样后如果 `token_limit_reached && needs_follow_up`，调用 `run_auto_compact(...)`。
   - compact phase 是 `CompactionPhase::MidTurn`。
   - compact 完成后继续同一个 turn 的工具/模型循环。

4. Codex 不在 idle prompt 后偷跑 UI-visible auto compact
   - compact started/completed 是 turn item，不是后台 listener 随机 print。
   - UI/TUI 可以把它渲染为线程历史中的一项，而不是污染输入 prompt。

5. Codex 有明确 phase
   - manual compact: `StandaloneTurn`
   - auto pre-turn compact: `PreTurn`
   - auto mid-turn compact: `MidTurn`

这比 Pulsara 现在好在: compact 是“当前/下一次 turn 的一部分”，不是“上一轮结束后的后台清理任务”。

## 2. Pulsara 当前做法与最大问题

当前相关代码:

- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/compaction/service.py`
- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/cli.py`

当前 HostSession 大致流程:

```text
run_turn()
  -> _prepare_prior_messages_for_turn(user_input)
     -> preflight compact_if_needed(...)
  -> agent_runtime.run_task(...)
  -> _finish_active_run()
     -> _schedule_auto_compaction_after_run()
        -> background compact_if_needed(...)
        -> listener print "context compaction completed"
```

这里有两个路径:

1. preflight compact
   - 发生在用户提交下一轮输入之后、模型调用之前。
   - 这是正确方向。

2. run-end background compact
   - 发生在 assistant 回复完成后。
   - REPL 已经重新显示 `pulsara>`。
   - compact 完成 notice 可能插进用户正在输入的行。
   - 这是当前最大体验问题。

因此最大问题不是:

- compact summary 不够好；
- token 估算不够准；
- completed notice 文案不够漂亮。

最大问题是:

> auto compact 在用户感知的 idle prompt 阶段异步发生，并通过普通 stdout/stderr 打印到交互输入区。

这违反 REPL 的基本体验边界: prompt 出现意味着系统已经准备接受用户输入，不能再有后台输出把这行打乱。

## 3. 推荐调整方案

### 3.1 V1: 取消 run-end background auto compact

直接停止在 `_finish_active_run()` 后调度 auto compact。

目标行为:

```text
assistant: abcd
pulsara>
```

到这里就是真 idle，不会再冒出:

```text
context compaction completed: ...
```

这一步应该移除或禁用:

- `_finish_active_run() -> _schedule_auto_compaction_after_run()`
- run-end 的 `_auto_compaction_task` 自动创建路径。

可以保留 `_auto_compaction_task` 字段用于兼容/manual cleanup，也可以在后续清理，但 V1 重点是禁用 run-end auto schedule。

### 3.2 V1: 保留并强化 next-turn preflight compact

当用户输入下一轮:

```text
pulsara> 讲讲梅西世界杯表现怎么样？
```

流程应为:

```text
run_turn(user_input)
  -> rebuild prior messages
  -> estimate model-visible prior + current_user_input
  -> if threshold reached:
       compact(trigger=auto, reason=preflight_context_threshold)
       publish compaction events
       print completed/failed notice while no prompt is active
       rebuild prior messages
  -> agent_runtime.run_task(user_input, prior_messages=...)
```

用户不需要再次输入同一个问题。compact 后继续处理原始 `user_input`。

这条路径目前已经基本存在；上一轮已经修成 model-visible estimate。下一步主要是让它成为唯一 auto compact 热路径。

### 3.3 V1: manual compact 保持独立

手动 `:compact` 是用户显式要求，继续允许在 idle prompt 下立即执行:

```text
pulsara> :compact
context compaction completed: ...
pulsara>
```

这是合理的，因为用户知道当前命令会产生输出。

### 3.4 V1: REPL notice 只允许在非输入编辑期出现

auto compaction notice 的合法时机:

- preflight compact 中，用户已经提交输入，模型尚未开始输出；
- manual compact 命令执行期间；
- future mid-turn compact item 展示期间。

不合法时机:

- `pulsara>` 已显示、用户可能正在编辑输入时；
- prompt_toolkit 正在等待 `read_line()` 时由后台 listener 直接 `print()`。

V1 可以通过取消 run-end background auto compact 解决大部分问题，不必先引入复杂 terminal redraw。

### 3.5 V2: AgentRuntime loop 内 mid-turn inline compact

Codex 更强的一点是 mid-turn compact:

```text
model -> tool call
tool result -> model needs follow-up
token threshold reached
inline compact
model continues
final answer
```

Pulsara 目前 compaction 在 HostSession 外层，不在 AgentRuntime loop 里。要做 mid-turn，需要新的 runtime seam:

- AgentRuntime 在每次 model call 前/工具结果后检查 token budget。
- 如果需要 compact:
  - 暂停当前 loop；
  - 调用 ContextCompactionService；
  - publish compaction events；
  - rebuild compacted prior / loop context；
  - 继续当前 run。

这会比 V1 深很多，应该作为后续 PR，不阻塞当前 REPL 体验修复。

## 4. 代码落点

### 4.1 HostSession

主要文件:

- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/session.py`

建议改动:

1. `_finish_active_run()`
   - 删除 `_schedule_auto_compaction_after_run()` 调用。

2. `_schedule_auto_compaction_after_run()`
   - V1 可以删除；
   - 或先保留但不再调用，避免一次性大重构。

3. `_prepare_prior_messages_for_turn(user_input)`
   - 保持当前 preflight compact 流程:
     - 先 `_prior_messages()`；
     - 传 `model_visible_messages` 和 `current_user_input` 给 service；
     - 如果 compacted，重新 `_prior_messages()`。

4. `_compact_if_needed_and_notify(...)`
   - 保留，因为 preflight/manual 仍需要 publish directly-written events，并通知 CLI listener。
   - 但它不应再由 idle background task 调用。

5. `_drain_auto_compaction()`
   - 如果删除 run-end background task，可简化；
   - V1 可先保留 no-op 安全逻辑，减少风险。

### 4.2 CLI

主要文件:

- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/cli.py`

V1 理想状态下 CLI 不需要复杂改动。因为取消 run-end background auto compact 后，listener 不会在 `prompt_async()` 等待期间触发。

仍应加测试保护:

- run-end 后不触发 compaction notice；
- preflight compact notice 在用户提交后、模型输出前出现。

如果未来重新引入后台任务，则必须让 CLI listener 使用 prompt_toolkit 的 safe redraw/print API，而不是直接 `print()`。

### 4.3 ContextCompactionService

主要文件:

- `/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/compaction/service.py`

上一轮已完成关键触发口径修正:

- auto compact 以 model-visible context 估算触发；
- compaction input estimate 与 trigger estimate 分离；
- compact input 从 raw event line 改为 coalesced observations。

下一步无需大改 service，主要是 HostSession 调度时机调整。

## 5. 测试矩阵

### 5.1 run-end 不再后台 compact

构造 fake compaction service:

- `compact_if_needed()` 记录调用次数；
- `run_turn()` 完成后 drain 一次；
- 断言没有 `reason="run_end_context_threshold"` 调用。

验收:

- assistant reply 完成后不会 schedule background compact；
- compaction listener 不会在 idle prompt 后收到 completed/failed event。

### 5.2 next-turn preflight 继续 compact 并消费原 user input

构造:

- prior messages 估算超过阈值；
- 当前 user input 是 `"讲讲梅西世界杯表现"`；
- fake service 返回 compacted=True；
- agent runtime transport 记录收到的 user input。

验收:

- service 收到:
  - `reason="preflight_context_threshold"`
  - `current_user_input` 为原用户输入；
  - `model_visible_messages` 为 rebuild 后 prior。
- compact 后 HostSession 重新 rebuild prior；
- 同一次 run 继续把原 user input 发给模型；
- 用户不需要二次输入。

### 5.3 REPL prompt 污染回归

CLI 层 fake session:

- 第一轮 run_turn 返回 final answer；
- run-end 不产生 listener notice；
- 下一次输入前 stdout 中不应出现 `context compaction completed:`。

验收:

```text
assistant final text
pulsara>
```

中间没有异步 compaction line。

### 5.4 manual compact 不回归

保持现有测试:

- `:compact` 调用 `session.compact_now()`；
- 成功时打印 completed；
- 失败时打印 failed；
- skipped 时打印 skipped。

### 5.5 compaction event publish 不回归

保留上一轮修复:

- compaction service 直接写入 event log；
- HostSession 读取新写入的 compaction events；
- 调用 `runtime_session.publish_stored_events(...)`；
- 避免 publisher sequence gap 导致下一轮卡住。

### 5.6 real trajectory

复现:

1. 打开 REPL；
2. 做几轮新闻搜索/长回复；
3. 等 assistant 完整回复后看到 `pulsara>`；
4. 等待数秒，不应出现 background `context compaction completed`；
5. 下一轮输入触发阈值时，preflight compact 可先打印 completed；
6. compact 完成后立刻继续回答该输入。

## 6. 推荐 PR 顺序

### PR C1: 禁用 run-end background auto compact

内容:

- 移除 `_finish_active_run()` 后的 auto compact schedule。
- 更新/删除 run-end safe point 相关测试。
- 新增 run-end 不触发后台 compact 测试。

收益:

- 立即修复 REPL prompt 污染。
- 风险很低，因为 preflight compact 仍保留。

### PR C2: 固化 preflight compact UX

内容:

- 增加测试证明 compact 后继续消费原用户输入。
- CLI 测试保证 completed notice 出现在 turn 执行期，而不是 idle prompt 后。
- Inspector/事件测试确认 reason 为 `preflight_context_threshold`。

收益:

- 将 next-turn compact 变成明确产品语义，而不是实现偶然。

### PR C3: 清理旧 background task 残留

内容:

- 如果 `_auto_compaction_task` 已无实际用途，删除字段和 drain 逻辑；
- 或改名为 future inline/maintenance task，避免误导。

收益:

- HostSession ownership 更清晰。

### PR C4: 设计并实现 mid-turn inline compact

内容:

- 在 AgentRuntime loop 内添加 compact safe-point；
- 当 tool result / follow-up model call 可能越线时 compact；
- compact 后继续当前 run；
- 事件中标记 phase=`mid_turn`。

收益:

- 向 Codex 的完整体验靠拢；
- 长工具链任务不会等到下一轮用户输入才压缩。

## 7. 决策冻结

V1 冻结以下决策:

1. auto compact 不在 idle prompt 后后台执行。
2. auto compact 的主路径是下一轮 user turn preflight。
3. preflight compact 后必须自动继续处理原 user input。
4. manual compact 仍可在 idle 状态立即执行。
5. mid-turn compact 是后续增强，不阻塞 V1。
6. REPL 不依赖后台 listener 在 prompt_toolkit 输入期直接打印 compaction notice。

这组决策的核心体验原则是:

> `pulsara>` 一旦显示，就表示系统不会再主动往这一行写异步 auto-compaction 输出。

