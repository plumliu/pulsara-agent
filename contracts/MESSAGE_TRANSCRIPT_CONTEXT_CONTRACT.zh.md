# Message / Transcript / Context Contract

_Created: 2026-07-04_

本文档定义 Pulsara 内部 message block、event replay、prior transcript reconstruction 与 model context budgeting 的长期契约。

相关代码：

- [src/pulsara_agent/message/blocks.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/message/blocks.py)
- [src/pulsara_agent/message/assembler.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/message/assembler.py)
- [src/pulsara_agent/message/reducer.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/message/reducer.py)
- [src/pulsara_agent/runtime/transcript.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/transcript.py)
- [src/pulsara_agent/runtime/context_input](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/context_input)
- [tests/test_event_message_system.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_event_message_system.py)
- [tests/test_host_core.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_host_core.py)

---

## 1. 核心立场

Pulsara 有四层不同对象：

- `AgentEvent`：durable truth。
- `Msg` / content blocks：runtime replay/projection。
- `ContextFactSnapshotFact`、`TranscriptCompileInput`、`ToolResultRenderUnit`：immutable compiler input。
- `LLMContext` / `LLMMessage`：provider-neutral model request。

四者不得混用。Event log 是事实；message 是UI/runtime投影；normalized input是可重放的compiler authority；LLM context是当前模型调用视图。

---

## 2. Content blocks

Runtime message content 使用 typed blocks：

- `TextBlock`
- `ThinkingBlock`
- `DataBlock`
- `HintBlock`
- `ToolCallBlock`
- `ToolResultBlock`

Tool result artifact refs 必须使用 `ToolResultArtifactRef`。如果 artifact 有 durable preview metadata，必须放在 `ToolResultArtifactRef.preview`，而不是只放 transient renderer metadata。

旧 event log 中没有 `preview` 字段的 artifact ref 必须仍能 replay。

---

## 3. Block assembly

`BlockAssembler`将durable model stream singleton/segment events增量折叠为completed content blocks。Provider delta不是token边界，也不是durable semantic边界；text/thinking/data/tool-call arguments分别从对应`*_SEGMENT`正文lossless拼接。

规则：

- start/segment/end正常成对组装；相邻segment可以跨transaction，但source span与durable index必须连续。
- orphan segment/end是recoverable stream problem，diagnostic assembler可以忽略并报告；terminal projection producer必须fail closed。
- segment layout、seal reason与source receipt只属于provenance/fact identity。Canonical transcript、checkpoint semantic identity与control result只消费hydrated terminal projection，不按当前policy重新切segment，也不把segment schedule写入provider semantic fingerprint。
- `ToolResultEndEvent` 负责写入 final state 与 artifact refs。
- `ExternalExecutionResultEvent` 可以作为已完成 tool result block 输入。

严格业务入口若依赖完整 block，必须另行用 diagnostics 检查 orphan/unfinished 状态。

---

## 4. Message reducer

`MessageReducer` 只重建单个 reply message。

职责：

- 追加 completed blocks；
- 聚合 `MODEL_CALL_END` usage；
- 根据 `TOOL_RESULT_END` 把对应 `ToolCallBlock` 标为 finished；
- 根据 approval events 更新 tool call state；
- 设置 `ReplyEndEvent.finished_at`。

它不负责：

- prior transcript 排序；
- recovery notes；
- compaction boundary；
- terminal completion note；
- user message reconstruction。

这些由 `runtime/transcript.py` 负责。

---

## 5. Prior transcript reconstruction

`rebuild_prior_messages()` 只用于Host prior-view、recovery与compaction服务，不是context compiler输入入口。

规则：

- 从 event log 读取 events；
- 若存在最新有效 completed compaction boundary，先注入 summary system message，再只 replay boundary 后 events；
- 每个 `RunStartEvent.current_user_message` 生成 user message；该 required typed fact 的 text/hash/observed_at
  是唯一 durable truth，`metadata.user_input` 不再是 supported fallback；
- 每个 completed reply 通过 `event_log.replay(reply_id)` 重建 assistant message；
- `completed`只描述provider stream闭合；main reply必须另有精确join的durable
  `ModelCallControlDispositionResolvedEvent(disposition=accepted)`才是canonical transcript。termination/recovery suppressed reply只可
  用于audit/UI，不能被prior reconstruction、Context Compiler或compaction summary重新引入；
- failed/aborted recoverable last run 注入 recovery system note；
- terminal process completion after last run start 注入 lifecycle-only note；
- 对 aborted/failed terminal runs，必须 strip unfinished tool calls，避免 provider tool-call ordering 违法。

Production bounded prior reconstruction必须先选择latest readable durable compaction checkpoint，再读取checkpoint之后的indexed
control facts，并用一次multi-reply snapshot批量读取全部目标reply block events。该读取冻结统一high-water并具有aggregate events/bytes cap，
禁止per-reply N+1。该checkpoint identity必须进入下一RunStart的transcript snapshot，且与“本次
preflight是否执行”分离；Context snapshot collector不得因本次没有preflight terminal而从sequence 1读取无界authority slice。

同run window compaction打开generation>1 window后，transcript authority必须拆为post-compaction contiguous primary delta与exact/sparse named facts。
Retained messages、tool-call/result pairing和tool-result render units从window source document中的versioned normalized baseline恢复；summarized旧semantic
chunks不再为compile/replay常驻内存。Exact replay必须验证同一primary/named range、baseline fingerprint与source document identity，不得信任process cache。
Soft exact-recurrence只使用当前window仍有完整typed gate/result evidence的terminal calls；累计rollout counters/phase跨window延续，但不得为soft hint
重新引入旧window无界text/data delta。

`rebuild_prior_messages_before_sequence()` 是 mid-turn inline compaction 的 prefix-only replay helper；它必须严格 replay `sequence < before_sequence`。Compiler本身不得消费该`Msg`结果。

---

## 6. Model context assembly

生产编译入口只接受：

- `ContextFactSnapshot`（event-safe fact加resolved call/tool-schema invocation binding）；
- `TranscriptCompileInput`；
- ordered `ToolResultRenderUnit`；
- `PreparedContextCandidateSet`。

live与replay分别收集同形状的`ContextSnapshotBuildInput`，再调用同一个pure builder。Compiler不得读取
`LoopState`、`scratchpad`、live MCP supervisor、session defaults、`Msg`或EventLog。

`TranscriptCompileInput`必须保留message order、assistant tool-call原始arguments JSON、tool-call/result pairing、artifact与
segment attribution。Malformed/non-object arguments作为typed status保留，不能通过重新序列化“修复”。Thinking与structured
tool calls不得混入natural-language user/system text。

Tool-result renderer只返回按`unit_id`索引的rendered fragments与canonical decisions，不得构造`LLMMessage`或完整message
sequence。最终assistant tool call/result pairing、message order与provider-neutral lowering只由compiler根据
`TranscriptCompileInput`完成；compiler不得接受预先lowering的transcript。

每个tool result在DTO validation、render preparation与compiler lowering三层都必须执行四方identity join：
`TranscriptToolResultRefFact`、`ToolInteractionPairFact`、`ToolResultRenderUnit`、`RenderedToolResultFragment`的call/unit ID、
call/result message ID、block index、global position与segment完全一致。跨call替换或只匹配unit ID均fail closed。

每个non-transcript candidate必须精确匹配`ContextFactSnapshotFact.candidate_authorities`中同source的正文hash/chars、event/artifact
refs、priority/required/stability、channel/lowering与dependency fingerprint。schema与compiler共同执行固定
source/channel/lowering矩阵；合法artifact ID不能为伪造inline正文提供authority。

---

## 7. Tool result context budget

每次compile先根据同一次`ResolvedModelCall`的effective input budget、当前projection/window policy解析完整
`ResolvedToolResultRenderPolicyFact`；renderer只消费最终派生policy，不回读`LoopBudget`。不存在固定36K
`tool_result_context_chars`第二真源：tool-result aggregate是model-call hard boundary之下的动态soft projection target；
body/envelope/prior/current-tail/per-tool/per-message/latest reserve等分池由同一个resolved policy统一约束，最终
provider payload仍必须通过整次call的token hard-bound validation。

规则：

- `ToolResultEndEvent`必须持久化actual render profile、typed essential result与timing；renderer不得从tool name或output JSON反推。
- 普通 tool result text 按剩余额度裁剪。
- 含 artifact 的 tool result 必须渲染 parseable JSON envelope：
  - `output_preview`
  - `output_truncated`
  - `artifacts`
- 若 aggregate budget 耗尽，必须保留 bounded compact envelope，而不是无限塞入所有 artifact refs。
- compact envelope 的primary必须优先选择带preview的text-like artifact；没有preview时退回第一个text-like ref；完全没有
  text-like artifact时primary保持为空。
- primary只能从text/JSON/XML/YAML artifact选择；binary/image不得成为`primary_artifact_id`。decision、compact/minimal payload与
  fallback必须使用同一selected primary；compact把primary置前，非primary refs不携带`read_more`，minimal只保留primary。
- primary compact artifact payload 必须保留 `artifact_id`、role、size、read_more；不得丢失可读取完整输出的入口。
- render cache只允许作为immutable hint输入；fresh render必须重新验证完全相等，hint不能成为语义真源。
- generic body被clip时仍必须保留universal observation timing：优先使用timed header，空间不足则使用含
  `pulsara_tool_observation`的compact envelope，不得无条件降成无timing basic header。
- renderer必须从structured payload builder取得observation/terminal-timing inclusion flags；不得扫描工具正文中的
  `observed_at=`、`pulsara_tool_observation`或`timing`字符串来决定是否省略真实timing。

Candidate priority数值越小越优先。`required=True`是唯一must-keep标志；system source不自动成为required。collection、allocation
和lowering使用同一stable priority order，degrade/omit从最低优先级optional candidate开始。

Render cache归RuntimeSession bounded owner。prepare阶段只读immutable hints；只有matching
`ContextCompiledEvent(status="compiled")`取得durable FULL acknowledgement后才提交write candidates。pressure、failed、
NONE/UNKNOWN/PARTIAL confirmation均不写cache。

Render cache只接纳`full_visible + full_envelope + within_budget + payload_preserved`结果；任何clip、omit、artifact preview、
compact/minimal envelope或budget exhaustion都不得写入。read/write异常只记录process-local operational diagnostic，不能改变
provider payload或阻止model call。Candidate lifecycle cache必须是entry count与aggregate chars双上界LRU，eviction只影响命中率。

Candidate authority required只持有唯一model-visible text、source timing与归因；selected/omitted由独立snapshot selection fact
拥有。Collector只消费selection/authority，
不再接收并行source字符串。Memory先join最新request的唯一terminal：Ready才从event重建，Failed表示本次没有memory candidate，
不得回退旧Ready；subagent正文/timing从`SubagentRunCompletedEvent`及其frozen created-at/sequence重建。Plan revision必须引用并
渲染latest durable revise event。Runtime context必须由environment/timing facts确定性渲染；memory hook prompt必须引用versioned
static instruction artifact/hash。Candidate cache read failure的canonical lifecycle仍等同普通miss；异常不得进入candidate-set或
manifest fingerprint。Oversized lifecycle entry在mutation前skip，不得清空已有LRU。

Pending subagent result选择先执行`max_results <= 0 -> empty`，snapshot schema再验证selected count不超过frozen candidate policy。
无pending与cap=0全省略必须产生不同selection fact；后者即使没有projection/authority，也必须在prepared set和manifest保存
`selected=(), omitted=N, reason=policy_limit`。禁止用空正文authority表达selection audit。
Selection只能从同一canonical parent event slice的pure subagent reducer派生；其source from/through必须等于被审计range。
Exact replay必须从ledger重建selection、projection、authority与prepared candidate/decision facts，不能直接信任manifest payload。

---

## 8. Data / binary blocks

Model context 中不得直接内联任意 binary/data body。

`DataBlock` 必须渲染为 placeholder，包含：

- id；
- optional name；
- media type；
- source kind。

真正的数据读取必须通过 artifact 或专用工具路径。

---

## 9. 禁止事项

- 不允许把 `Msg` 当 durable truth。
- 不允许把`Msg`、`LoopState.messages`或scratchpad作为compiler输入。
- 不允许从tool-result JSON推断terminal variant、essential envelope或terminal timing。
- 不允许 context renderer 无界内联大 tool result。
- 不允许 compact envelope 只保留第一个 artifact 而丢掉 primary preview artifact。
- 不允许 unfinished assistant tool call 在 transcript replay 中喂给 provider。
- 不允许 compaction summary 替代系统提示词/skill active injection。
- 不允许 message reducer承担 recovery/compaction/terminal completion note 的职责。
- 不允许恢复已删除的`build_llm_context`、`msg_to_llm_messages`或state-based compile facade。

---

## 10. 测试守护

最低测试门槛：

- singleton/segment event stream folds into text/thinking/data/tool call/tool result blocks。
- usage from multiple model calls aggregates on reply message。
- missing start event does not crash assembler。
- prior transcript injects user messages from required
  `RunStartEvent.current_user_message`；缺失、hash 不一致或 attribution 不一致均为 contract error，不能回退到
  legacy metadata。
- failed/aborted last run injects recovery note。
- unfinished tool call is stripped from aborted/failed replay。
- terminal completion note is lifecycle-only and capped。
- compaction boundary summary is used and tail replayed。
- mid-turn prefix replay excludes current run tail。
- aggregate tool result context budget applies across multiple results。
- compact artifact envelope keeps primary preview artifact.
- live/replay生成相同snapshot/transcript/unit/manifest fingerprint。
- malformed arguments、parallel pairing、typed terminal deny/execute/process variants均可exact replay。
- 修改display JSON不能改变typed execution semantics；缺少typed terminal semantics必须fail closed。
