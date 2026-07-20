# Context Compaction / Continuity Contract

本文档定义 Pulsara context compaction 的生产契约。根目录设计文档可以作为实施说明；本文件是长期代码契约。

## 1. 触发语义

Context compaction 不是“run 结束就 compact”。

允许的触发:

- 自动 compact: 当估算的 model-visible context 达到或接近 compact 阈值时触发。
- 手动 compact: 用户显式执行 `:compact`，可在达到阈值前提前压缩。

生产路径不存在固定`256_000 / 200_000`阈值。每次主模型调用使用同一个`ResolvedModelCall`中的model limits、input safety
margin与window policy派生admission/compaction target；summarizer使用独立resolved target。估算必须使用该resolved call冻结的
versioned token estimator，compiler、compaction planner与provider pre-send validation不得各自维护字符比率或窗口常量。

Auto compact 的 UI 可见执行点有两个:

- run-start preflight: 用户提交新的普通 user turn 后、下一次模型调用前触发；若 compact 成功，HostSession 必须继续消费同一个 user input，不得要求用户再次输入。
- mid-turn inline: active run 内工具结果已完成、runtime 准备发起 follow-up model call 前触发。旧inline路径只允许处理current run之前的历史prefix；Stage 4 current-run window compaction则通过typed transcript units、pairing-safe projection与durable window facts处理同run历史，不能直接truncate mutable `LoopState.messages`。current user、protected tail与tool-call/result pairing必须保留。
- run-end: 不得调度后台 auto compact；`pulsara>` 提示符显示后，不得再由 auto compaction 向 REPL 输入区写入 completed / failed notice。
- suspended-run resume 路径不得触发 HostSession preflight auto compact；但 approval / plan interaction / MCP elicitation resolution 若已经完成 pending payload、状态回到 `RUNNING`、并准备进入 follow-up model call，可走同一个 mid-turn inline safe point。abort / stop / host teardown recovery 不得触发 compact。

## 2. Canonical truth

Compaction 不得删除、重写或替换 `agent_events`。

Canonical truth 仍是:

- Postgres `agent_events`;
- artifacts;
- typed reducers/projections;
- runtime/session metadata。

Compaction summary 是 derived continuity artifact，不是新的事实源。

## 3. Typed events

生产 compaction 必须使用 typed events:

- `CONTEXT_COMPACTION_STARTED`
- `CONTEXT_COMPACTION_COMPLETED`
- `CONTEXT_COMPACTION_FAILED`

不得使用 `CUSTOM` 事件作为正式 compaction boundary。

`CONTEXT_COMPACTION_COMPLETED` 是唯一可信的 compaction boundary，并且必须引用存在的 summary artifact。若 artifact 缺失，rehydration 必须忽略该 boundary，回退到 canonical event replay。

连续 compaction 时，新的 compact input 必须包含上一条可用 completed boundary 的 summary artifact 正文。由于 rehydration 只采用最新 completed boundary，新的 summary 必须 carry forward 旧 summary 中仍然有效的上下文；不得只总结上一条 boundary 之后的 raw events。

## 4. Summary artifact

Compaction summary 必须写入 artifact store。

Artifact metadata 必须包含:

- `kind = "context_compaction_summary"`
- `do_not_write_back = true`
- `compaction_id`
- `trigger`
- `reason`
- `window_id`
- `through_sequence`
- `keep_after_sequence`

Summary artifact 不得进入 durable memory reflection，不得作为用户偏好/事实写回。

## 5. Rehydration

`rebuild_prior_messages()` 是 compaction-aware transcript rehydration 的唯一入口。

Production PRE_RUN必须使用其bounded变体：latest readable completed checkpoint + indexed sparse control delta + single multi-reply bounded
snapshot。该snapshot冻结统一high-water并施加aggregate events/bytes cap，不允许per-reply N+1。旧`ContextCompactionService`的source planning与Started/terminal recovery同样只能消费bounded checkpoint/delta或indexed
lifecycle facts；`should_auto_compact()`与`compact()`不得各自重新扫描整本ledger。当前run的RunStart必须独立冻结所采用的
checkpoint basis，即使本次preflight没有产生新的compaction terminal，后续ContextFactSnapshot仍从该basis恢复。

当存在可用 completed boundary 时，模型可见 prior context 形态为:

```text
context compaction summary system message
+ events after keep_after_sequence replayed by normal transcript reducer
+ normal runtime projections / plan instructions / recovery notes
```

Mid-turn inline compaction 的 completed boundary event 可以出现在 current run events 之后，但其 `keep_after_sequence` 必须指向 current run `RUN_START` 之前。运行中重写必须使用 prefix-only rehydration:

```text
context compaction summary system message
+ replayed events where keep_after_sequence < sequence < current_run_start_sequence
+ current run tail copied from LoopState.messages
```

不得全量 rebuild 后再 append current tail，避免重复 current user/tool messages。

Compaction summary 必须带 no-write-back fence，并明确区分:

- user message;
- tool result;
- artifact reference;
- memory recall projection;
- working context projection;
- recovery/abort diagnostic;
- plan/runtime state。

## 6. Safety boundaries

必须满足:

- 当前用户输入不得被 preflight compact 吞进 summary。
- `ModelCallEnd(completed)`/`ReplyEnd(completed)`本身不构成canonical assistant history。只有同一call已持久化
  `ModelCallControlDispositionResolvedEvent(disposition=accepted)`时，其text/tool-call blocks才能进入prior transcript或compaction
  summary；`suppressed_by_termination`与`suppressed_by_recovery`只用于audit/UI。Transcript projection与compaction必须复用同一个
  accepted-disposition reducer，禁止保留第二套“看到ReplyEnd就纳入”的推断。
- mid-turn compact 不得把 current run 的 `RUN_START`、current user input、assistant tool call、tool result、pending approval、pending plan interaction 或 pending MCP elicitation payload 写入 summary。
- pending approval / pending plan interaction / pending MCP elicitation 状态下不得自动 compact；必须等 resolution 后回到 tool-follow-up safe point。
- 手动 `:compact` 只允许 idle session。
- 手动 `:compact` 直接写入的 compaction events 必须 publish 到 `RuntimeSession.publisher`，避免后续 runtime event 出现 sequence gap；但不得同时触发 REPL compaction listener 双输出。
- mid-turn compact 直接写入的 started/completed/failed events 也必须 publish 到 `RuntimeSession.publisher`，并作为 active run event stream 的一部分可观察；不得通过 HostSession idle listener 在 REPL prompt 后后台打印。
- Missing summary artifact 必须 fail-open 到 full event replay。
- Repeated auto compact failure 必须有 circuit breaker，避免每轮重复烧模型。
- Compact model 不得获得工具 schema；compact prompt 必须强制 text-only/no-tools。
- Compact model stream 中出现 `RUN_ERROR` 必须使本次 compaction 失败，即便此前已经产生部分文本。
- Malformed compact output（例如未闭合的 `<analysis>` 或 `<summary>`）不得写入 summary artifact。
- Started durable后必须由service-owned bounded terminalization owner持有到Completed/Failed确认提交；普通函数栈不是owner。
- cancellation不得无界等待writer。stable candidate confirmation为full时收口；none保留candidate；partial/conflict/unknown fail closed。
- Host close必须bounded drain pending compaction terminal owners；失败时停止session/lease/workspace teardown。
- caller取消时若Started write仍在运行且当前confirmation为missing，write ownership必须转交service-owned pending commit；
  迟到full commit必须使用Started冻结的terminal ID补terminal fact，确认none才可删除provisional owner。
- confirmation读取自身抛错时同样必须转移pending owner；不得把unknown confirmation降格为raw cancellation并遗忘后台write。
- session/recovery发现orphan Started时，使用Started冻结的terminal ID和模型/预算事实写
  `recovery_terminalization/recovered_interrupted` Failed event，不重新resolve模型或重新规划window。

## 7. Inspector

Inspector 必须能解释:

- session compact windows;
- run 看到的 compaction boundary;
- compaction phase (`preflight` / `mid_turn` / legacy `run_end`)、safe point、current run id、max compactable sequence、tail message count;
- summary artifact metadata/payload;
- dangling started-without-completed/failed;
- completed boundary referencing missing artifact。

## 8. Tests

最低测试门槛:

- typed events roundtrip;
- summary analysis stripping;
- summary artifact metadata and no-write-back;
- rehydration uses boundary and replays tail;
- missing artifact fallback;
- manual `:compact` / HostSession API;
- run-end 不得调度后台 auto compact;
- single huge completed run 可在下一轮 preflight 触发 auto compact;
- preflight compact 后继续消费原始 current user input;
- manual `:compact` publishes direct-written compaction events without duplicate listener notice;
- approval / plan / MCP suspended-run resume 不触发 auto compact;
- mid-turn compact 只压 current run 前的历史 prefix;
- current run assistant tool call 和 tool result 保留在 rewritten `LoopState.messages` tail;
- mid-turn compact failed event publish 后，后续 runtime event 不得因 sequence gap 卡住;
- inspector windows 显示 mid-turn phase/safe-point metadata;
- inspector windows/diagnostics;
- real LLM dogfood 覆盖 long-session compact/resume。

## 9. Host boundary pairing

preflight compaction Started/Completed/Failed 必须携带同一个 `host_boundary_id` 与 `host_boundary_kind=pre_run`；manual与
mid-turn两字段均为空。Started预生成稳定 terminal event ID；所有 terminal fact必须引用唯一 Started。Inspector只按该
boundary identity join到随后RunStart，不按“最近event”或run attribution猜测。Started后取消也必须写稳定 cancelled Failed；
terminal commit outcome未知时阻止破坏性close，不能留下静默dangling Started。

---

## 10. Runtime-observation projection continuity

Context compaction/Long-Horizon rewrite是压力驱动重建provider projection的唯一正常authority；`auxiliary_frame_rebase`已删除。Confirmed rewrite必须同时规划canonical transcript rewrite、
runtime-observation stable state和current tail，并通过同一ordered-provider-projection validator确定因果顺序。它保护current run/window、latest clock、每个replacement source effective head、
未闭合lifecycle/active skill/plan/rollout及pending control/continuation依赖。

Observation rewrite只收缩active provider projection，EventLog中的原facts保持不变。Rewrite event嵌套Long-Horizon authority、bounded paged partition proof与transitive coverage；replacement
unit使用首次commit时冻结的generation-neutral causal placement，不得从旧vector ordinal或当前compiler重猜位置。System/tool compatibility变化可以合法rollover并顺带物化effective heads，
但source absence、cache miss或旧observation数量本身不能触发rebuild。

Generation store必须从durable append增量维护唯一observation lifecycle reducer state，至少包含
ordered observation identities、effective replacement heads、latest clock、closed run/workflow/
child scopes与pending dependency identities。Plan terminal、subagent result delivery以及registered
run/workflow terminal推进closure；current run、latest clock、effective heads与pending dependency继续
protected。Long-Horizon planner只能消费该snapshot，不得以几个局部`if`重新分类。Rollover fold
必须再次重算partition、coverage和resulting effective heads。
