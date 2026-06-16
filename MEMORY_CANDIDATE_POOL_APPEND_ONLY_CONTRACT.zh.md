# Pulsara 记忆候选池 Append-Only 契约

本文档沉淀 GPT/Codex 与 Claude 多轮设计审查后的最终收敛结果。它不是第一阶段 reflection hook 的继续扩写，而是记忆写入路径的硬切目标：主 agent、cheap hint reflection、未来治理 agent 都只能提出候选；只有 governance 能把候选变成 canonical `mem:*` 节点。

本文档描述本轮重构必须遵守的目标契约。实现完成后，旧的“producer 直接调用 `MemoryWriteService.submit()` 写入 GraphStore”路径必须消失。

## 1. 核心不变量

最重要的不变量只有一条：

```text
所有 producer 只能向 durable candidate pool 追加候选；
把候选变成 canonical mem:* 节点的，只有 governance 一条路；
governance 写入永远走 MemoryWriteService -> MemoryWriteGate -> ExecutionEvidenceLedger。
```

由此推出几条硬边界：

- `remember_*` 工具是主 agent 的候选提出 fast path，不是长期记忆落盘入口。
- cheap hint reflection 是补漏 producer，不是最终写入裁判。
- candidate pool 是 durable inbox，不是 semantic memory。
- GraphStore 只保存治理后进入长期语义层的 `mem:*` 节点。
- 去重、projection echo 拒绝、修正、合并，都属于 governance 的职责。
- `MemoryWriteService.submit()` 仍然是唯一 canonical write API，写事件与 graph 写入审计链不分叉。

## 2. 两层模型

候选模型分两层，不要把 provenance、lifecycle、workflow 字段塞进 typed candidate。

### 2.1 Typed MemoryCandidate

现有 `MemoryCandidate` 判别联合继续保持纯净：

```text
ClaimCandidate
PreferenceCandidate
ObservationCandidate
ActionBoundaryCandidate
DecisionCandidate
```

它只表达“如果要写一条 memory，它的类型化内容是什么”。类型专属约束继续留在 schema 边界：

- `ActionBoundaryCandidate` 必须有 `applies_when` 和 `do_not_apply_when`。
- `DecisionCandidate` 可以携带 `based_on_ids`。
- `evidence_ids` 是候选声明的 provenance 引用，但候选本身不保存来源 run、tool call、治理状态。

### 2.2 Candidate Envelope

候选池保存的是 envelope。Envelope 承载来源、上下文和 invalid attempt，不改变内层 typed candidate 的纯度。

概念模型：

```python
class ValidCandidate:
    payload_kind = "valid"
    candidate: MemoryCandidate

class InvalidAttempt:
    payload_kind = "invalid"
    attempted_tool_name: str
    attempted_kind: str | None
    raw_arguments: dict
    validation_error: str

CandidatePayload = ValidCandidate | InvalidAttempt
```

`InvalidAttempt` 很重要。主 agent 调用 `remember_action_boundary` 时如果缺少 `do_not_apply_when`，这个失败 attempt 不应被丢掉；治理 agent 后续可以读取用户原话、tool result 错误和 raw arguments，决定是否生成 corrected candidate。

但工具返回语义不变：

```text
schema-valid remember_*:
  deposit ValidCandidate envelope
  tool result = SUCCESS, status="proposed"

schema-invalid remember_*:
  deposit InvalidAttempt envelope
  tool result = ERROR
```

不能把 invalid attempt 伪装成成功，否则主模型会失去本轮自我修正机会。

## 3. Candidate Pool 表

第一版候选池只需要 append-only `memory_candidates`。候选行不可变，不维护 mutable `status`。

概念列：

```text
memory_candidates
  entry_id                  pool:<uuid>，候选池主键
  payload_json              CandidatePayload JSON
  origin                    main_agent_tool | reflection | governance
  source_session_id          候选来源 runtime session
  source_run_id              候选来源 run
  source_turn_id             候选来源 turn
  source_reply_id            候选来源 reply
  source_tool_call_id        nullable
  user_quote                 nullable
  created_at
```

说明：

- `entry_id` 是池主键；内层 valid candidate 仍然有自己的 `candidate_id`。
- `origin=reflection` 表示 cheap hint reflection 或其他旁路 producer 产出的候选。
- `origin=governance` 只用于治理自己生成的 corrected/merged candidate envelope，避免把治理产物与主 agent 原始 attempt 混淆。
- `source_session_id` 必须保留。Postgres EventLog 的 `run_id` 是全局主键，并且 run 归属于某个 runtime session；跨 session 治理时不能只靠 run/turn/reply。
- `source_event_ids` 第一版不作为候选池核心列。需要精确证据定位时，复用已有 `RuntimeEventRef` / `RuntimeEventSpan` 形状，或从 source run 的 EventLog 回放得到。
- `content_key` / fingerprint 可以作为派生索引或生成列，但不是 source of truth。去重裁判在 governance 写入口。

候选池的 pending 状态不存字段，按决策表派生。

## 4. Governance Decision 表

第一版治理结果只需要 append-only `memory_governance_decisions`。它记录治理对一个或多个候选做了什么，以及 canonical write 的结果是什么。

概念列：

```text
memory_governance_decisions
  decision_id                decision:<uuid>
  governance_batch_id         governance:<uuid>
  decision_json              GovernanceDecision JSON
  write_outcome_json          GovernanceWriteOutcome JSON
  created_at
```

其中 `GovernanceDecision` 是判别联合，不要摊成一张宽 nullable 表：

```python
class SkipDecision:
    kind = "skip"
    target_entry_ids: tuple[str, ...]
    reason: str
    skip_reason: str | None

class SubmitAsIsDecision:
    kind = "submit_as_is"
    target_entry_id: str
    reason: str

class CorrectAndSubmitDecision:
    kind = "correct_and_submit"
    target_entry_id: str
    candidate: MemoryCandidate
    reason: str

class MergeAndSubmitDecision:
    kind = "merge_and_submit"
    target_entry_ids: tuple[str, ...]
    candidate: MemoryCandidate
    reason: str
```

第一版只保留这四种。`supersede`、`contradict`、`escalate` 暂不进入候选池主路径：

- `supersede` / `contradict` 会改写既有 canonical memory 的生命周期，属于慢速 maintenance。
- `escalate` 第一版没有人工 review consumer，容易变成死状态；可先表达为 `skip` 加 reason。

`GovernanceWriteOutcome` 也应是判别联合：

```python
class NoWriteOutcome:
    kind = "no_write"

class WriteSucceededOutcome:
    kind = "write_succeeded"
    memory_id: str
    memory_type: str
    node_status: str        # ACTIVE | NEEDS_REVIEW | REJECTED
    confidence_level: str
    verification_status: str
    gate_reason: str
    write_event_ids: tuple[str, ...]

class WriteFailedOutcome:
    kind = "write_failed"
    error_type: str
    message: str
    write_event_ids: tuple[str, ...]
```

注意：如果 `MemoryWriteGate` 判定 `REJECTED`，但 ledger 仍然落了一个带 `memory_id` 的节点，这属于 `WriteSucceededOutcome`，不是 store failure。`WriteFailedOutcome` 只表示写入链路异常，例如缺失引用、GraphStore 错误、Postgres 错误。

## 5. Pending 状态派生规则

不要在 `memory_candidates` 上维护 `status`。候选是否 pending 由 anti-join 派生：

```text
pending(candidate)
  = 不存在 terminal governance decision 覆盖该 candidate
  且 candidate.origin != governance
```

`origin=governance` 的候选是 corrected / merged 候选的 append-only 审计行。它们已经由同一条 governance decision 写入或尝试写入 canonical memory，不应再次进入普通 pending 扫描；否则 corrected / merged 审计行会永久悬空为 pending。

Terminal decision 只有：

- `skip`
- `submit_as_is` 且 `write_outcome.kind == "write_succeeded"`
- `correct_and_submit` 且 `write_outcome.kind == "write_succeeded"`
- `merge_and_submit` 且 `write_outcome.kind == "write_succeeded"`，并且该 candidate 是 merge member

非 terminal：

- `write_failed`
- governance run 崩溃
- schema-invalid governance decision
- store 暂时不可用

这保证一次瞬时 store error 不会永久丢掉候选。下一次 governance 仍能拾起该 pending candidate，结合失败决策记录决定重试、修正或 skip。

## 6. Synthetic Governance Context

`MemoryWriteService.submit()` 会产生 `MemoryCandidateProposedEvent` 与 `MemoryWriteResultEvent` / `MemoryWriteFailedEvent`。这些事件必须有 `EventContext(run_id, turn_id, reply_id)`。

候选池 governance 的写事件一律使用 synthetic governance context，不挂回用户 run。

```text
governance_batch_id = governance:<uuid>
run_id              = run:governance/<governance_batch_id>
turn_id             = turn:governance/<governance_batch_id>
reply_id            = reply:governance/<governance_batch_id>
```

`governance_batch_id` 是唯一真相；run/turn/reply 由纯函数派生，不另存三份可漂移字段。

三套坐标严禁混淆：

```text
candidate.source_*:
  候选从哪个真实对话 run 来

governance_batch_id:
  哪个治理批次处理了它

write event context:
  canonical memory 写事件写在哪个 synthetic governance run 下
```

`memory_governance_decisions` 是三者的连接点：它通过 `target_entry_ids` 指向候选来源，通过 `governance_batch_id` 指向治理批次，通过 `write_outcome` 指向最终写入结果。

### 6.1 为什么不能挂回 source run

把 governance 写事件挂回 source run 是机制性错误：

- PostgresEventLog 的 `runtime_session_id` 绑定在 EventLog 实例上。已有 run 归属于某个 session；另一个 session 不能复用这个 run。
- 即使同 session，把新 sequence 的 memory write event 追加到已经 `RunEnd` 的 source run 后面，也会污染 `iter(run_id=...)` 与 timeline/replay 语义。
- 候选池治理可能处理多个 run 的候选；把这些写事件挂到触发治理的当前用户 run，也是在坐标上撒谎。

所以规则必须无分支：

```text
reflection producer events:
  live user run context
  只表示“当前 run 产生了候选或补漏失败”

governance write events:
  synthetic governance context
  恒定，不借用用户 run
```

### 6.2 v1 与 offline 迁移

第一版可以把 synthetic governance run 挂在当前 `runtime_session_id` 下，因为治理仍由当前 runtime wiring 触发和写入。

未来离线化时有两种选择：

```text
per-source-session 子批次:
  按 source_session_id 分组，每个源 session 内开 synthetic governance run

governance 独占 session:
  所有治理写事件进入 runtime:governance，跨 session 链接全靠 decision 表
```

这两个选择与 DB lease、`memory_governance_runs` 表、多 worker 调度是同一条边界上的事情。第一版不要提前承诺。

## 7. Dedupe 与 Claim 是两件事

去重和并发认领不能混为一谈。

```text
dedupe:
  保证 canonical graph 正确性
  重跑治理不会产生重复 mem:* 节点

claim / lease:
  保证治理任务不重复认领同一批 pending candidates
  避免重复 decision 行和重复工作
```

第一版分三档：

```text
v1 inline-only:
  governance executor 串行触发
  不需要 DB lease
  仍必须在写入口做 dedupe

v1 + 同进程 scheduled task:
  需要 asyncio 级 mutex 保护 candidate claim/selection
  不一定需要 DB lease

独立进程 / 多 worker:
  需要 DB lease 或 claim table
  同时引入 memory_governance_runs / worker metadata
```

dedupe 不能替代 claim。dedupe 只能防止重复 canonical memory；它不能防止两个 worker 同时治理同一批候选并写入重复 decision 行。

## 8. 事件与审计链

第一版不新增 governance started/decision/completed/failed 事件。`memory_governance_decisions` 表本身就是治理过程的 source of truth。

仍然保留 canonical write 审计链：

```text
MemoryWriteService.submit()
  -> MemoryCandidateProposedEvent
  -> MemoryWriteResultEvent | MemoryWriteFailedEvent
  -> GraphStore canonical memory write
```

这些写事件使用 synthetic governance context。每条 `memory_governance_decisions.write_outcome` 应记录对应 `write_event_ids`，使 decision row、EventLog、GraphStore 可互相追踪。

不需要第一版 `MemoryCandidateQueuedEvent`。候选入池不改变 GraphStore，也不属于 canonical memory 写入审计链；candidate pool 表就是入池真相。

## 9. Producer 路径

### 9.1 主 Agent Fast Path

`remember_*` 工具只 deposit envelope：

```text
valid args:
  MemoryProposalSink.deposit(ValidCandidate envelope)
  tool result SUCCESS

invalid args:
  MemoryProposalSink.deposit(InvalidAttempt envelope)
  tool result ERROR
```

随后 loop safe point 的 memory hook 将 sink 中的 envelope append 到 durable candidate pool。它不调用 `MemoryWriteService.submit()`。

### 9.2 Cheap Hint Reflection

cheap hint 仍然只做便宜唤醒：

```text
每个 run 结束时检查用户输入。
如果命中强记忆词，且本轮没有主 agent memory attempt，才唤醒 Flash reflection。
Flash 必须批判性判断，允许 should_reflect=false。
```

Flash reflection 输出候选时，只 append candidate pool。它不做 canonical write，不做最终 dedupe，不直接落 GraphStore。

### 9.3 Governance Producer

治理生成 corrected/merged candidate 时，也 append `origin=governance` 的 candidate envelope，随后同一治理决策通过 `MemoryWriteService.submit()` 写 canonical memory，并记录 decision row。

这样 corrected/merged candidate 也有自己的 `entry_id`，审计链不会断。

## 10. Governance 执行流程

概念流程：

```text
1. 读取 pending candidates。
2. 按 source_session/source_run 回查 EventLog、timeline、tool evidence。
3. 搜索已有 canonical memory。
4. 将候选按 content_key / 语义关系聚类。
5. Governance agent 输出 GovernanceDecision。
6. 宿主校验 decision schema。
7. 对 submit/correct/merge 决策，在单一写入口做 dedupe。
8. 调用 MemoryWriteService.submit(candidate, synthetic_event_context)。
9. append memory_governance_decisions，携带 write outcome。
```

去重逻辑从当前 reflection 层迁移到 governance 写入口：

```text
content_key =
  kind
  normalized statement
  scope
  type-specific fields
```

`content_key` 是聚类和精确 dedupe 线索，不是最终语义裁判。最终 skip、correct、merge 或 submit 由 governance 在读取证据、已有 memory、projection ids 后决定。

## 11. 与旧实现的硬切差异

旧的 producer-direct-write implementation 中有几处需要硬切。完成态应满足：

- `DurableMemoryHooks._drain()` append durable candidate pool，不调用 `MemoryWriteService.submit()`。
- `MemoryReflectionEngine.reflect()` 只产候选入池，不直接 submit，不做最终 dedupe。
- `remember_*` 工具的 invalid args 也 deposit invalid attempt envelope，但工具仍返回 `ERROR`。
- `MemoryProposalSink` 保存 candidate envelope，不保存裸 `MemoryCandidate`。
- `build_agent_runtime_wiring()` 注入 `CandidatePool` 与 governance executor；`MemoryWriteService` 只给 governance 使用。

这不是兼容性改造，而是硬切。旧路径不应继续保留一个“直接写 canonical memory”的旁路。

## 12. 实现顺序建议

建议按以下顺序落地：

1. 定义 `CandidatePayload`、`GovernanceDecision`、`GovernanceWriteOutcome` 的 Pydantic 判别联合。
2. 新增 `CandidatePool` protocol、`InMemoryCandidatePool`，只做 append/list_pending/append_decision/derive_pending。
3. 修改 `MemoryProposalSink`，让它保存 envelope。
4. 修改 `remember_*`，valid 与 invalid attempt 都 deposit envelope。
5. 修改 `DurableMemoryHooks._drain()`，从“submit canonical memory”改为“append candidate pool”。
6. 修改 reflection，让它只 append candidates，不直接 submit，不直接 dedupe。
7. 新增最小 governance executor：读取 pending，输出显式决策，使用 synthetic context 调用 `MemoryWriteService.submit()`，append decision。
8. 把 `_candidate_fingerprint` / `_already_exists` 抽到 `memory/governance/dedupe.py`，只在 governance 写入口使用。
9. 再实现 `PostgresCandidatePool`。
10. 最后考虑 scheduled governance、mutex、lease、`memory_governance_runs`。

## 13. 需要验证的不变量

实现时需要测试以下不变量：

- schema-valid `remember_preference` 只入池，不写 GraphStore。
- schema-invalid `remember_action_boundary` 入池为 `InvalidAttempt`，工具返回 `ERROR`。
- cheap hint reflection 命中时，若主 agent 已有 memory attempt，不额外唤醒 Flash。
- reflection 产出的 candidate 只入池，不产生 `MemoryWriteResultEvent`。
- governance 使用 synthetic run/turn/reply 产生 write events。
- source run 的 EventLog 不会因为后续 governance 多出 `RunEnd` 之后的 memory write events。
- `write_failed` decision 不终结 candidate，候选仍可被下一轮治理拾起。
- `skip` 或成功写入 decision 会让候选从 pending 派生集中消失。
- 重跑 governance 命中已有 active/needs-review memory 时，产生 duplicate skip，不写重复节点。
- InMemoryCandidatePool 与 PostgresCandidatePool 的 pending 派生语义一致。

## 14. 一个实现注意点

Synthetic governance reply 只承载 memory write events，不是 assistant reply。它不应被当作可 replay 的对话内容。

实现时需要确认：

- `BlockAssembler` / reducer 对只有 memory events、没有 block events 的 synthetic reply 不会残留未释放状态。
- `replay(reply:governance/...)` 即使返回空 `AssistantMsg` 也不应进入用户可见对话流。
- timeline projection 不应把 governance synthetic reply 混成主 agent 回复。

若发现 assembler/reducer 对非 block events 有副作用，应在 runtime/timeline 或 replay 调用方显式排除 `run:governance/*`。

## 15. 最终收口

最终形状：

```text
memory_candidates:
  append-only durable inbox
  保存 valid candidates 与 invalid attempts

memory_governance_decisions:
  append-only decisions
  保存治理决策、write outcome、governance_batch_id

governance context:
  synthetic run/turn/reply
  v1 挂当前 runtime session
  offline 化时再引入 governance session 或 per-source-session 子批次

canonical write:
  只有 governance 调 MemoryWriteService.submit()
  只有这里做 dedupe
  只有这里进入 GraphStore
```

这让 Pulsara 的记忆系统保持三层分工：

```text
EventLog / ArtifactStore:
  runtime truth 和证据

CandidatePool:
  durable pending memory inbox 和治理审计

GraphStore:
  经过治理的 semantic memory
```

主 agent 负责提出，Flash/reflection 负责补漏，governance 负责判断，gate 负责写入裁决。长期记忆不依赖主模型“刚好会调用工具”，也不被每轮旁路 reflection 过度写入。
