# Run Execution Ownership Contract

_Created: 2026-07-28_

本文档是 committed run、initial/resume activation、pending interaction、terminalization 与
Host/child execution composition 的长期契约真源。`AgentRuntime` loop 的业务规则继续由
`AGENT_RUNTIME_LOOP_CONTRACT.zh.md`定义；本文件只冻结谁拥有这些规则的执行状态与物理资源。

相关代码：

- `src/pulsara_agent/ports/run_execution.py`
- `src/pulsara_agent/ports/run_authority.py`
- `src/pulsara_agent/ports/interaction_transition.py`
- `src/pulsara_agent/ports/run_terminalization.py`
- `src/pulsara_agent/runtime/run_execution/`
- `src/pulsara_agent/runtime/run_execution/factory.py`
- `src/pulsara_agent/runtime/subagent/execution.py`
- `tests/test_run_ownership_architecture.py`
- `tests/test_run_boundary_host_lifecycle.py`
- `tests/test_run_reconciliation.py`
- `tests/test_host_lifecycle_contract.py`

---

## 1. 核心拓扑

一个 committed run 只有一个稳定 `RunOwner`；一次 initial/resume segment 各有一个短生命周期
`RunActivationCoordinator`：

```text
RunActivationComposition
├── AgentRuntime
├── RunExecutionRegistry
└── RunActivationService

RunOwner
├── immutable RunGenesisAuthority
├── replace-on-FULL RunAuthorityHead
├── typed RunProgressState
├── RunResourceSlot
├── RunActivationSlot
├── RunSuspensionSlot
├── stable RunFinalizationOwner
├── observer registry
├── immutable activation completion history
└── run completion
```

`RunExecutionRegistry`是session-scoped、process-local execution owner。它由共同 composition
factory创建，不属于`HostSession`，也不塞入`RuntimeSession`。Host与child必须使用同一个
factory/registry协议；生产代码只有factory可以构造`AgentRuntime`。

`RuntimeSession`仍是唯一commit/confirm/reducer-dispatch/publication gateway，但不成为run task
service locator。各领域reducer的语义owner不因此迁移。

---

## 2. Prepared reservation 与 committed identity

RunStart提交前只能存在：

```text
PreparedRunOwnerReservationKey(
  runtime_session_id,
  run_id,
  run_start_event_id,
)
```

它不含sequence。只有RunStart FULL、并从exact stored envelope取得正sequence后，才能构造
`RunOwnerIdentity`。

Promotion必须同时验证：

- prepared reservation identity与RunStart ID/run ID一致；
- exact raw stored envelope解码后byte/typed-equivalent于该RunStart；
- envelope runtime session与reservation及capability basis owner一致；
- execution handles仍为`boundary_owned`；
- prepared activation仍属于同一boundary/run。

Live handoff在registry临界区内把handles从boundary owner转给RunOwner，并产生：

```text
INITIALIZING
+ AwaitingInitialRevision
+ BoundRunResources
+ stable finalization owner
```

不得出现已transfer handle仍由boundary持有或没有owner的窗口。NONE释放prepared owner；UNKNOWN保留
untrusted reservation并阻止普通dispatch。

---

## 3. Run authority

Authority分三层：

1. `RunGenesisAuthority`：RunStart中不可变的完整typed facts。
2. `RunAuthorityRevision`：initial或continuation的immutable revision。
3. `RunProgressState`：仅保存运行计数、usage与最近context等process state。

Genesis必须保存并exact join：

- exact RunStart raw envelope/reference/payload fingerprint；
- Host ingress或`SubagentRunEntryFact`；
- current user、model target、permission、MCP installation；
- transcript seed semantic与artifact-backed reference；
- long-horizon、subagent graph contract与terminal RunEnd ID；
- child的完整initial capability basis与rollout subaccount。

Child capability basis有四层验证：nested fact验证owner kind、permission与MCP；RunStart验证event/run
attribution；genesis验证ledger runtime session与sequence；initial exposure验证exact basis与execution
surface。Caller提供的runtime-session字符串不能替代stored-envelope authority。

Initial exposure FULL后安装`InitialRunAuthorityRevision`。Continuation只有resume boundary与exposure
均FULL并exact join predecessor后，才以CAS替换revision。旧revision永不原地修改。

`RunProgressState`禁止复制pending interaction、active activation generation或terminal summary；公开
snapshot在registry锁内由progress加slots组合生成。

---

## 4. Lifecycle 与 slots

Run lifecycle是：

```text
INITIALIZING | OPEN | SUSPENDED | TERMINALIZING | TERMINAL
| RECONCILIATION_REQUIRED
```

约束：

- `INITIALIZING`表示durable authority已存在但activation尚不可dispatch；
- `OPEN`必须同时拥有installed authority、bound resources与唯一active activation；
- `SUSPENDED`必须拥有typed suspension authority与live resources；
- `TERMINALIZING`由stable finalization owner拥有；
- `TERMINAL`表示matching RunEnd已经FULL；finalization slot可继续处于`run_end_full_pending_output`，此时run completion尚未完成；
- `RECONCILIATION_REQUIRED`禁止ordinary model/tool/interaction admission。

Resource slot是`unbound | bound | retiring | closed_bound | closed_never_bound`。Unbound只允许reopen
initial/continuation rebind pending或terminal-only recovery。Live RunStart promotion不得创建Unbound。

Continuation FULL但activation安装前crash时，reopen必须构造：

```text
INITIALIZING
+ InstalledRunAuthorityRevision(continuation)
+ Unbound(reopen_continuation_rebind_pending)
```

它不能伪装成OPEN，也不能退回仍接受旧interaction的SUSPENDED。

---

## 5. Activation ownership

`RunActivationIdentity`只能投影既有durable `RunExecutionActivationFact`的三类source：

- `host_run_boundary`
- `host_resume_boundary`
- `subagent_run_start`

`reopen_rebind`只是process-local installation reason，不是第四种durable attribution。

每个activation generation拥有独立coordinator、driver task、state carrier token、execution borrows、
model-control owner与可选stream observer。Activation phase为：

```text
SAFE_POINT -> MODEL_STEP | TOOL_BATCH -> SUSPENDING | COMPLETED
```

具体durable write attempt继续使用自己的`PREPARED/COMMITTING/FULL/NONE/UNKNOWN/RETIRED`
状态，不把所有维度压进一个大enum。

Activation completion与run completion是两个不同future：

- activation completion可返回`RunSuspendedOutcome`；
- run completion只有matching RunEnd FULL并生成terminal receipt后才完成。

旧activation completion immutable。Resume只新增`InteractionResumeLinkReceipt`并让新activation引用
predecessor generation；不得回写“resumed”到旧receipt。

Service-owned driver的每一个物理出口都必须完成且只能完成以下一种ownership transition：

- matching RunEnd FULL并转入stable finalization；
- typed suspension FULL并把state carrier移交给`RunSuspensionSlot`；
- 把stable candidate、segment identity和physical failure移交给reconciliation/finalization owner；
- 在尚无durable candidate时，由run terminalization port冻结并提交唯一error terminal candidate。

Driver task结束后不得仍有active segment、state carrier或execution-handle borrow由该task占有。Task done
backstop必须在registry临界区exact检查segment generation；发现孤立active segment时，立即安装
reconciliation owner并撤销该segment的ordinary dispatch authority。Public waiter是否仍resident不能参与该判定。

---

## 6. Host opaque contract 与 observers

Host只消费opaque `RunHandle`、`HostRunControlView`和closed outcome：

```text
RunSuspendedOutcome
| RunTerminalOutcome
| RunTerminalizationPending
| RunTerminalOutputPending
| RunReconciliationRequired
```

`RunSuspendedOutcome`直接携带`PendingInteractionAuthority`。Host不得读取或保存activation working
state，不得推断pending interaction，也不得保存run driver task。

`run_turn`与`stream_turn`共享同一个service-owned driver。Streaming只是bounded observer
subscription：observer detach、backpressure或caller cancellation只detach observer/waiter，不取消run。

生产Host import中禁止`RunActivationWorkingState`、`RunOwner`和concrete registry。

---

## 7. Pending interaction 与 resume

Pending authority是closed union：

- approval exact引用`RequireUserConfirmEvent`；
- plan question exact引用`PlanQuestionAskedEvent`；
- plan exit exact引用`PlanExitRequestedEvent`；
- MCP只嵌`McpInputRequiredSuspensionFact`及其suspension event reference，不复制binding/request/deadline。

每个branch的source reference必须在对应durable source event FULL时冻结。Suspension builder只允许
`get_by_id(exact_reference.event_id)`并重新绑定event type、run ID、runtime session、sequence及payload
fingerprint；禁止扫描整条run、按tool-call/question/interaction ID选择“最后一个匹配事件”，也禁止
identity与branch各自保存一份可漂移的source authority。

进入SUSPENDED时，activation completion与RunOwner slot transition在registry内原子完成。Public
resolution唯一通过`InteractionTransitionPort`消费slot。Resolution FULL后进入INITIALIZING；新
authority revision、incoming handles和新activation原子安装后才进入OPEN。

同一interaction只允许一个winner。NONE保留exact stable candidate供新physical generation重试；
UNKNOWN/CONFLICT进入reconciliation。Waiter cancellation只detach，不取消resident owner。

---

## 8. Stable finalization 与 final output

Finalization挂在稳定RunOwner上，不依赖active activation继续存活。它拥有：

- terminal request与stable RunEnd candidate；
- attempt generation/deadline；
- FULL/NONE/UNKNOWN confirmation；
- terminal maintenance/reconciliation；
- final-output materialization owner。

RunEnd FULL后仍可能处于`run_end_full_pending_output`。唯一
`RunFinalOutputMaterializer`从matching RunEnd、accepted model terminal projection、canonical
transcript与usage settlement重建bounded output；不得读取activation state。Finalization owner在output
receipt FULL前必须保留immutable confirmed RunEnd reference，snapshot不得把该阶段投影为empty。

Final text的唯一authority是canonical transcript projection store中不晚于matching RunEnd的最后一个
accepted、non-tool assistant terminal projection。该store必须从canonical checkpoint加bounded semantic
delta恢复；materializer不得回放raw reply或同步读取完整run ledger。Usage只允许在session-owned bounded
I/O operation中按sequence/byte cap分页fold为标量accumulator。Artifact-backed final text必须exact验证
media type、byte count与SHA-256。Live完成与owner退休后rebuild必须byte-identical。

Run completion只在`TerminalRunReceipt`安装后完成。Finalization retry与Host close不能依赖segment
task仍resident。

---

## 9. Reconciliation 与 reopen

Reconciliation snapshot必须覆盖完整`RunOwnerStateIdentity`：lifecycle、authority head、resource、
activation、suspension、finalization、termination revision及stable candidate。

Confirmation分类为FULL/NONE/CONFLICT或UNRESOLVED：

- NONE保留stable candidate并继续阻止ordinary admission；
- FULL仅按snapshot与exact receipt计算目标state；
- reopen没有resident driver时，FULL也最多回到INITIALIZING；
- caller不能任意选择OPEN/SUSPENDED/TERMINAL；
- conflict/unresolved保持reconciliation。

Process reopen不恢复旧task、observer、borrow或state carrier。所有process-local resources获得新
generation。Terminal-only recovery可以不重绑capability/MCP execution surface。

Dangling Host run repair必须先exact-read stored RunStart envelope，fold完整authority revision chain，
并注册`INITIALIZING + Unbound` dormant owner。Window/account close与RunEnd由该owner的stable
finalization slot冻结、经RuntimeSession writer提交并逐candidate exact-confirm；随后同一个
`RunFinalOutputMaterializer`从ledger生成terminal receipt。Generic resume repair不得在common owner之外
直接写RunEnd。Continuation exposure在同一batch中先于resume boundary，reducer按effective exposure
identity与exact相邻sequence配对。

---

## 10. Child parity

Child ownership拆成两部分：

1. `ChildAdmissionSessionOwner`只拥有capacity、parent graph slot、child RuntimeSession composition
   lease与parent/child attribution。
2. common `RunOwner`唯一拥有child activation task、execution handles、finalization和run completion。

Child admission owner不得提供`attach_coroutine()`或`attach_execution_handles()`。Restart后capacity
slot只能是`live_reservation | recovered_occupancy | released`；recovered occupancy必须由parent graph
proof/high-water恢复，先建立capacity barrier，再repair child RunOwner。

Terminal close顺序固定为：child RunOwner terminal FULL → parent graph terminal settlement FULL → child
session drain/close → capacity release。任何UNKNOWN或仍在途physical operation都阻止提前释放。

Child activation timeout不是普通waiter cancellation。Timeout owner必须先经common child activation port
请求stop/terminalization并等待exact child RunEnd FULL，再用该terminal reference提交parent graph failure；
之后才允许child session drain和capacity release。不得先把parent标记failed而让shielded child driver继续
执行model/tool operation。

---

## 11. Capability-scoped ports

Coordinator只能按attempt取得窄port：context preparation、model execution、tool batch、interaction
transition、authority read/commit和terminalization。Generic event writer与arbitrary transaction companion
只存在于RuntimeSession implementation/closed adapter内部。

`AgentRuntime`不得保存完整`RuntimeSession`，也不得在构造时向session写回service。LLMRuntime继续
拥有provider/model physical lifecycle；ModelStep只消费committed result handle。

---

## 12. Durable schema subcuts

D6包含两项显式不兼容subcut：

- event schema registry version 7：child RunStart持久化完整initial capability basis；旧world按项目
  reset/migration policy处理，不提供dual decoder；
- `tool_delta_burst_contract.v2`：增加bounded successor-suspension tail与minimum terminal tail，支持同一
  MCP tool call的多轮InputRequired，同时保持最终ToolResult terminal capacity。

Tool burst v2规定successor suspension只消费专用tail，不能吞掉minimum terminal tail；physical
suspension identity使用exact `ToolExecutionSuspendedEvent.id`，不能用跨轮共享interaction ID。

---

## 13. 禁止事项

- 禁止production scratchpad、metadata/extras/dict改名fallback。
- 禁止`AgentRunResult.state`或Host保存active/suspended/preparing state。
- 禁止Host/child各自创建run registry、AgentRuntime或driver。
- 禁止progress复制slot/finalization真值。
- 禁止RunStart envelope、runtime session或sequence由caller字符串自证。
- 禁止continuation在durable FULL前安装。
- 禁止observer cancellation传播到run driver。
- 禁止finalization依赖active segment。
- 禁止run-execution coordinator取得generic EventLog writer。

---

## 14. 测试守护

最低门槛：

- prepared→committed promotion与handle owner-gap；
- child basis四层exact join；
- initial/continuation authority与crash-before-activation；
- 同一run至少两个resume generation；
- observer detach/backpressure不取消run；
- activation driver异常或settlement异常后不存在active orphan segment/carrier/borrow；
- RunEnd FULL前run completion不完成；
- final output live/reopen byte identity，且不做raw reply或unbounded run-ledger read；
- pending interaction exact source type/run/reference rebound；
- FULL/NONE/UNKNOWN/CONFLICT、stop、cancel、close与restart；
- reconciliation无driver不得恢复OPEN；
- child recovered occupancy、timeout child-first terminalization与terminal release ordering；
- AST gate证明scratchpad、legacy Host driver、child execution attach API和generic writer escape hatch为零；
- global package graph无跨package SCC。
