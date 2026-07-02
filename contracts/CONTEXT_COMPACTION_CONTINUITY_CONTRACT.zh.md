# Context Compaction / Continuity Contract

本文档定义 Pulsara context compaction 的生产契约。根目录设计文档可以作为实施说明；本文件是长期代码契约。

## 1. 触发语义

Context compaction 不是“run 结束就 compact”。

允许的触发:

- 自动 compact: 当估算的 model-visible context 达到或接近 compact 阈值时触发。
- 手动 compact: 用户显式执行 `:compact`，可在达到阈值前提前压缩。

V1 固定阈值:

- context window: `256_000` tokens。
- auto compact threshold: `200_000` tokens。

阈值判断必须使用保守估算。Event-log / JSON-ish / tool-result-shaped 内容不得按普通自然语言 `chars/4` 乐观估算；V1 至少按 event-log `chars/2`、普通文本 `chars/4`，并保留约 20%-30% 安全余量。

Run-end / run-start 只是安全执行点:

- run-end: 作为后台 safe-point，为下一轮准备。
- run-start preflight: 在下一次模型调用前兜底，防止 resume、超长输入、上轮 compact 失败导致本轮直接超窗。
- V1 不做 mid-turn compact，不在 tool call、approval、plan interaction、pending user interaction 中途替换 `LoopState.messages`。

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

当存在可用 completed boundary 时，模型可见 prior context 形态为:

```text
context compaction summary system message
+ events after keep_after_sequence replayed by normal transcript reducer
+ normal runtime projections / plan instructions / recovery notes
```

Compaction summary 必须带 no-write-back fence，并明确区分:

- user message;
- tool result;
- artifact reference;
- memory recall projection;
- working context projection;
- recovery/abort diagnostic;
- plan/runtime state。

## 6. Safety boundaries

V1 必须满足:

- 当前用户输入不得被 preflight compact 吞进 summary。
- pending approval / pending plan interaction / active run 不得自动 compact。
- 手动 `:compact` 只允许 idle session。
- Missing summary artifact 必须 fail-open 到 full event replay。
- Repeated auto compact failure 必须有 circuit breaker，避免每轮重复烧模型。
- Compact model 不得获得工具 schema；compact prompt 必须强制 text-only/no-tools。
- Compact model stream 中出现 `RUN_ERROR` 必须使本次 compaction 失败，即便此前已经产生部分文本。
- Malformed compact output（例如未闭合的 `<analysis>` 或 `<summary>`）不得写入 summary artifact。

## 7. Inspector

Inspector 必须能解释:

- session compact windows;
- run 看到的 compaction boundary;
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
- auto threshold 不无条件 run-end compact;
- single huge completed run 可触发 auto compact;
- inspector windows/diagnostics;
- real LLM dogfood 覆盖 long-session compact/resume。
