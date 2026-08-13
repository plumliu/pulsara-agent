# Pulsara Round 5：Long-horizon Execution Envelope 实施规格

> 状态：**ACTIVATED**
>
> 记录日期：2026-08-13
>
> 当前编码基线：`1ade1b00b2b206fe83bfdbcdad385ea67a5e1dd1`（`feat: preserve provider input prefix continuity`）
>
> 激活证据：[`round5_long_horizon_execution_envelope_activation.json`](benchmarks/suites/core/v1/round5_long_horizon_execution_envelope_activation.json)
>
> 本文只规格化 **Round 5A：长程执行包络**。Round 5B 才拥有 context compaction、summary、snapshot adoption与provider-input rebase。本轮不得借“长程任务”之名提前实现compaction。
>
> 上位架构：[`PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md`](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)
>
> 产品能力索引：[`POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md`](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 前置规格：[`ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md`](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[`ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md`](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[`ROUND_4_PLAN_WORKFLOW_AND_RUN_PERMISSION_IMPLEMENTATION_SPEC.zh.md`](ROUND_4_PLAN_WORKFLOW_AND_RUN_PERMISSION_IMPLEMENTATION_SPEC.zh.md)
>
> 历史证据：[`PULSARA_LONG_HORIZON_BUDGET_PRIOR_ART_RESEARCH.zh.md`](archived_docs/PULSARA_LONG_HORIZON_BUDGET_PRIOR_ART_RESEARCH.zh.md)、[`PULSARA_LONG_HORIZON_REAL_REPL_TRAJECTORY_ANALYSIS.zh.md`](archived_docs/PULSARA_LONG_HORIZON_REAL_REPL_TRAJECTORY_ANALYSIS.zh.md)、[`PULSARA_LONG_HORIZON_CONTEXT_WINDOWS_HARD_CUT_IMPLEMENTATION.zh.md`](archived_docs/PULSARA_LONG_HORIZON_CONTEXT_WINDOWS_HARD_CUT_IMPLEMENTATION.zh.md)

---

## 0. 执行结论

当前canonical conversation Kernel已经恢复了完整tool output artifact、Terminal三工具、structured model-input compiler、同Host prefix continuity、busy steer与Plan workflow，却仍保留两项Stage 2 activation保险丝：

- 每个turn最多24次model call；
- 从turn admission开始，canonical preparation与后续settlement复用一个120秒absolute deadline；provider/tool虽各有自己的transport/physical等待，但它们经过的wall time会继续耗掉这份deadline，使下一次canonical操作拿到过期预算。

它们位于错误的架构层。一个健康的长程任务可能合法经历几十或数百次model/tool循环，也可能在每项operation都健康的情况下持续数小时。正常任务不应因为“已经工作很久”或“调用次数很多”而被Runtime强制中断。

Round 5A冻结：

1. **正常交互ROOT turn与SUBAGENT_TASK turn默认没有model-call或tool-call次数上限。**
2. **一个turn没有wall-clock deadline。**每项有明确owner的canonical transaction、dispatch planning、provider transport、tool invocation和close/join使用自己的operation-scoped watchdog；不得把“fresh”机械理解为一次复合planning中的每个子步骤都重新获得完整预算。
3. **watchdog是远端operation backstop，不是工作预算。**production默认值必须显著高于健康路径；只有可取消adapter才能把它同时实现为physical hard bound。UX yield、idle watchdog与physical kill/close不得共用一个数字。
4. **foreground agent provider stream只受connect/write/pool与idle边界约束，不受总生成时长约束。**持续产生合法transport activity的长响应可以一直完成；durable job provider仍服从其30/45秒attempt total owner。
5. **Terminal foreground等待超过UX阈值只yield为Host-scoped process，不终止turn、不杀process。**Terminal process本轮不新增lifetime cap；它由显式kill或Host close终结。
6. **每次独立canonical transaction取得一份新的deadline。**Round 3.1的单次provider-dispatch planning仍共享一份planning-scoped absolute deadline；stable consumption candidate形成后，consume/confirmation才取得新的canonical write deadline。不得把任何一份deadline跨model call、tool call或人类等待复用。
7. **Round 3.1 prefix continuity保持不变。**长程推进只能追加suffix；本轮没有任何合法history rewrite、epoch rebase或compaction reset。
8. **不恢复hard-cut前的durable rollout account、reservation、checkpoint、reducer、repair或execution replay。**call/tool/elapsed/usage计数最多是process-local/operational observation。
9. **本轮不修改128K input、16K output或默认256K model-context配置。**它们属于后续model capability/configuration与Round 5B active-context讨论，不是本轮删除task-level保险丝的前置条件。
10. **不新增relation、migration、Committed/Live event、subject、append guard、durable job或Protocol类型。**activation oracle继续为`34 / 23 / 15 / 2 / 26 / 4`。
11. **logical watchdog不能抹掉已经物理结束的真实tool outcome。**late exact result仍是exact result；无法证明effect outcome的异常保持attempt-without-result并interrupt，不能伪造成已知`SYSTEM_ERROR`，也不能自动重跑。

最终执行模型是：

```text
one canonical turn
    while semantic follow-up is required:
        freeze exact provider safe point
        compile append-only suffix
        execute one bounded provider operation
        atomically accept complete assistant entry
        execute each tool under its own owner/policy
        atomically accept each known result
        continue

    stop only on:
        semantic completion
        explicit user cancellation
        Host close/replacement
        unrecoverable operation failure
        current model-input/resource contract failure
        explicit future opt-in/headless policy
```

本文偶尔使用“无限推进”作为简称，但它不表示无限内存、无限单项输出、无限并发或一定能越过当前4,096-item/16 MiB provider-input guard；它只表示**任务进度不由固定model/tool-call次数或turn总wall-clock budget截断，资源占用在各自owner处有界**。

---

## 1. 基线与证据

### 1.1 起草输入

```text
PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md
cb3e7b0a9f33e5e4c5b17850d47e1af580a3f23f094f868076351bb17a6a6e80

POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md
df45ddb1b2423a3a48a3fe9422d4e4021791d2ea35bff1c2e87a74b8939e4b4d

ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md
1a996f8dda8c767043e4c84bf7d414724129dbd3d890d5cf3bb5463922cae6e6

ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md
9ee6cfca09869a67903a2164c2c2025d7c836998bd26a459336cee90658e34c2

ROUND_4_PLAN_WORKFLOW_AND_RUN_PERMISSION_IMPLEMENTATION_SPEC.zh.md
9209f21692bfc9534d9f95ff583738cd9a86f44a34714bc6d9f38083a72c4c0b
```

这些hash只标识起草输入。coding agent必须在第一个production diff前记录实际checkpoint HEAD、本文hash与Gap Index hash，不得覆盖并行用户修改。

### 1.2 三个代码基线

| 仓库/基线 | Commit | 本轮用途 |
|---|---|---|
| 当前Pulsara | `1ade1b00b2b206fe83bfdbcdad385ea67a5e1dd1` | 唯一production truth；所有修改在canonical Kernel上完成 |
| hard-cut前Pulsara | `5b7ad9f7ffc8565bc572180b2bde0c81ab64473a` | 识别已经验证过的长程产品语义与需要拒绝的durability machinery |
| long-horizon首次完整落地 | `65a71176bb158875d58bf860f1d3dd19da1a1958` | 追溯active/cumulative budget、finalization与compaction设计来源；不得整体移植 |
| Codex | `6138909d6ec58b2fbe635ef973e02caecad5a5aa` | 比较正常interactive loop、mid-turn compaction与operation bounds |
| grok-build | `c68e39f60462f28d9be5e683d9cbe2c57b1a5027` | 比较TUI/headless step policy、tool/output/timeout分层 |

### 1.3 当前代码真值

[代码确认] [`limits.py`](src/pulsara_agent/conversation_kernel/limits.py)当前包含：

```text
model_calls_per_turn_hard              = 24
provider_input_tokens_per_call_hard    = 128_000
provider_output_tokens_per_call_hard   = 16_384
foreground_io_timeout_ms               = 30_000
host_close_hard_ms                     = 5_000
```

[代码确认] [`runner.py`](src/pulsara_agent/conversation_kernel/runner.py)当前：

```text
_run_turn():
    deadline = monotonic() + 120s
    start canonical turn using deadline
    run_accepted_turn(..., same deadline)

run_accepted_turn():
    while model_call_count < 24:
        prepare/provider/tool/result settlement using same deadline

    raise RuntimeError("model-call limit exhausted")
```

Plan QUESTION与少数continuation分支会重置deadline，但普通model/tool循环不会。`operation_timeout_seconds`因此并非per-operation timeout，而是normal turn总寿命。

[代码确认] 当前没有独立tool-call cap；`tool_call_count`只是结果统计。Round 5A不得在删除24-call cap时新增tool cap补位。

[代码确认] [`KernelSessionIO`](src/pulsara_agent/conversation_kernel/io.py)已正确保留一个重要物理边界：logical cancellation/timeout不能让不可取消的worker thread在后台继续修改资源。它会等待exact physical task退出，Host close也会drain已admit task。但deadline分支当前在drain后无条件丢弃task返回值并抛`TimeoutError`；这对需另行exact-confirm的canonical write尚可，对tool physical invocation会抹掉late exact outcome。Round 5A保留physical owner，同时为tool增加窄的outcome-preserving seam。

[代码确认] OpenAI Responses与Chat Completions adapter目前使用scalar `timeout_seconds=60`创建SDK client。OpenAI/httpx通常把该scalar同时应用于connect/read/write/pool，其中read本身是inactivity timeout而非整个response总时长；问题是四个维度被折成同一个过短数字，代码无法表达本轮要求的closed policy。本轮必须拆开，而不是把scalar从60机械提高到另一个数字。

### 1.4 历史真实故障

归档REPL证据至少包含两次决定性轨迹：

| 轨迹 | model calls | tool calls | 单次最大active input | 失败原因 |
|---|---:|---:|---:|---|
| 第一条长搜索 | 46 | 46 | 约25K估算/33K provider报告 | tool projection为36,083 chars，超过36,000固定cap |
| 第二条长搜索 | 50 | 52 | 约23.7K | `max_turns=50`，最后一次仍请求tool，没有final synthesis机会 |

两条轨迹的累计input均超过110万tokens，但这不是单次active context。历史已经证明：

- active-context pressure、累计成本与step count是三种不同事实；
- compaction不应因累计token自动触发；
- 低固定step cap会在context仍健康时截断任务；
- 固定aggregate char cap不应把已知tool outcome变成run-ending failure。

Round 1已经修复完整artifact与bounded preview；Round 5A只修复step/time层，不重复实现tool output产品语义。

### 1.5 hard-cut前值得保留与必须拒绝的部分

`5b7ad9f7`前的最终long-horizon policy包含：

- active-context compaction：80% trigger、55% post target；
- cumulative weighted rollout：8× input budget；
- cached input权重0.1、non-cached input权重1、output权重4；
- 60% warning、80% restricted、100% finalization；
- 预留2次finalization model call、1次compaction及16个tool cost units；
- 200 model calls / 256 tool calls作为emergency stop；
- child reservation与大量window/projection/account/checkpoint事实。

本轮只保留两条经验：

1. task progress、active context、cumulative rollout和physical resource必须分层；
2. 如果未来引入经济/headless预算，必须先保留finalization机会，而不是突然hard stop。

本轮明确不恢复：

- durable rollout balance/account/reservation；
- action-class budget ledger；
- window Started/Completed/repair/reducer事件；
- checkpoint、reconciliation、delivery receipt；
- generic no-progress detector；
- 200/256 emergency cap；
- child rollout quota；
- compaction、summary或snapshot adoption。

### 1.6 Codex代码确认

本地Codex正常interactive agent loop使用无固定次数的`loop`。当模型仍需follow-up且active context达到阈值时，在同一用户turn执行MidTurn compaction并继续；本轮只借鉴其“任务步数不限、context另行治理”的分层，不实现compaction。

Codex确实拥有physical bounds：

- submission channel capacity为512；
- user text最大1 MiB；
- unified exec模型侧默认10,000 tokens；
- raw terminal retained output为1 MiB；
- unified exec process最多64；
- provider支持stream retry与idle-timeout配置；
- bounded thread shutdown。

但本轮扫描没有找到与Pulsara `16 MiB canonical input / 64 MiB compiler working set / 320–640 MiB continuity`完全对应的统一prompt-memory policy。不能用Codex为这些Pulsara特有常量背书。

### 1.7 grok-build代码确认

grok-build默认`max_turns=None`，而`--max-turns`是headless-only；interactive TUI会忽略它。Responses payload的`max_tool_calls=None`。正常agent loop因此同样不使用固定step/tool cap。

grok-build把资源边界放在operation owner：

- auto-compaction默认在resolved context window的85%；
- provider stream idle timeout默认300秒；
- retry默认最多15次；
- 通用tool output为40 KB，bash/terminal为20,000 chars；
- foreground command有UX/operation timeout，可自动转background；
- production terminal foreground ceiling可配置到10小时；
- background process最大runtime为10小时；
- 运行期output file最高5 GiB，完成后retained snapshot为64 MiB；
- completed snapshot、scheduler、notification等各自有局部数量上限。

这些数字不要求Pulsara逐项复制。应借鉴的原则是：**等待、展示、保留、physical lifetime和task progress分别拥有边界，不能由一个120秒deadline统辖。**

---

## 2. 术语与closed分类

### 2.1 Turn不是operation

```text
Turn
    一个canonical user/objective admission开始的产品工作单元
    可以包含任意多次model call、tool batch与safe point

Model call
    一次prepared input到完整provider response的physical operation

Tool invocation
    一个attempt-before-effect后的physical effect operation

Canonical I/O attempt
    一个read transaction、writer transaction或content/blob operation

UX wait/yield threshold
    决定前台是否继续等待；超出不表示operation失败

Idle watchdog
    连续没有transport activity的异常检测；每次activity重置

Physical close/join watchdog
    shutdown阶段等待owner真实退出的远端backstop

Task budget
    对整个turn的次数、成本或时间限制；Round 5A正常交互路径不存在
```

### 2.2 上限分类

所有现有或未来常量必须归入exactly one分类：

| 类别 | 约束对象 | Round 5A处置 |
|---|---|---|
| `TASK_PROGRESS` | 整个turn的model/tool次数或总时间 | 默认不存在 |
| `MODEL_CAPABILITY` | 单call context/output能力 | 本轮不修改；后续按model/profile/config讨论 |
| `PROJECTION` | 模型看到的单项tool/source表示 | 保留既有degrade/artifact语义；不得终止健康turn |
| `PHYSICAL_RESOURCE` | bytes、queue、process、thread、并发、blob | owner-local有界；不充当task budget |
| `OPERATION_WATCHDOG` | 一次I/O/transport/effect/close | 本轮重塑为独立、大值、不可共享 |
| `OPTIONAL_POLICY` | headless/economic/enterprise budget | 本轮不存在；未来显式opt-in |

不得以“architecture guard要求所有数字finite”为由，重新把`TASK_PROGRESS`默认值设成某个大整数。`None`/无字段才是正常交互语义。

### 2.3 核心不变量

```text
healthy operation[i] completes within its own watchdog
healthy operation[i + 1] receives a fresh watchdog

sum(operation durations) may exceed every individual watchdog value
number(model calls) may exceed every historic default step count

there is no turn-wide decrementing clock
there is no model/tool-call admission counter
```

---

## 3. Process-local watchdog contract

### 3.1 单一配置载体

新增一个纯process-local frozen contract，推荐位置：

```text
src/pulsara_agent/conversation_kernel/execution_watchdogs.py
```

最小DTO：

```python
@dataclass(frozen=True, slots=True)
class KernelExecutionWatchdogPolicy:
    provider_dispatch_planning_attempt_seconds: float = 120.0
    foreground_canonical_attempt_seconds: float = 120.0
    writer_renew_attempt_seconds: float = 10.0
    writer_renew_safety_margin_seconds: float = 5.0
    provider_connect_seconds: float = 120.0
    provider_write_seconds: float = 120.0
    provider_pool_seconds: float = 120.0
    provider_stream_idle_seconds: float = 600.0
    nonterminal_tool_attempt_seconds: float = 600.0
    terminal_foreground_decision_seconds: float = 120.0
    host_session_close_join_seconds: float = 120.0
    durable_job_executor_close_seconds: float = 120.0
    blob_gc_close_seconds: float = 120.0
```

它的边界：

- 不序列化；
- 不进PostgreSQL；
- 不进AgentEvent metadata；
- 不进入provider input；
- 不计算semantic fingerprint；
- 不形成lease、receipt、generation、checkpoint或repair；
- production composition使用上述大值；
- tests可显式注入短值与fake clock；
- 不从模型tool arguments读取；
- 本轮不新增环境变量。

这些默认值是初始physical watchdog，不是SLA。健康本地canonical transaction、provider activity或builtin tool通常应远低于它们。

### 3.2 明确为`None`的边界

以下项目没有total timeout：

```text
turn_total_seconds                 = None
model_calls_per_turn               = None
tool_calls_per_turn                = None
foreground_provider_response_total_seconds = None
plan_question_wait_seconds         = None
terminal_process_lifetime_seconds  = None  # bounded by current Host lifecycle
```

不得用极大数模拟`None`。极大数最终仍会成为隐藏产品终止语义，也会污染测试和diagnostic。

### 3.3 fresh deadline factory

建议提供可注入clock、由policy唯一选值的纯factory：

```python
class KernelExecutionDeadlineFactory:
    def new_deadline(self, owner: KernelWatchdogOwner) -> float:
        return self._clock.monotonic() + self._policy.seconds_for(owner)
```

调用方不得传自由`seconds`或用`monotonic() + literal`绕过matrix。`seconds_for()`对closed enum做穷尽匹配，未知owner直接失败。factory只生成一次attempt的deadline；`owner`只用于选择policy与测试调用次数，不得进入semantic candidate、diagnostic自由文本或provider input。deadline不得被保存进turn state、continuity epoch、canonical row或prepared semantic candidate。

一次atomic transaction内的所有SQL共享该attempt deadline是合法的；transaction结束后deadline必须丢弃。ACK-unknown confirmation、retry或下一项operation均取得新deadline。

### 3.4 不要求统一计时实现

不同owner可以使用适合自己的物理机制：

- PostgreSQL driver/connection provider继续使用absolute monotonic deadline；
- async provider transport使用connect/write/pool timeout与read-idle timeout；
- Terminal使用yield decision、process completion event和Host close；
- synchronous builtin tool继续由`KernelSessionIO`追踪physical thread；
- human interaction继续使用process-local waiter，不消耗operation watchdog。

统一的是语义分类，不要求所有路径套一个`asyncio.timeout()`。

### 3.5 Closed owner matrix

不得对当前约94个`KernelSessionIO.run()`调用做“统一替换为120秒”。每个调用必须先属于下列exact owner之一；未分类的新调用fail review，不能默认落入foreground policy：

| Owner | 初始边界 | 覆盖范围 | 禁止行为 |
|---|---:|---|---|
| `PROVIDER_DISPATCH_PLANNING` | 120s absolute | 一次safe-point freeze、canonical/base hydration、target与tool-surface准备、one-cut source freeze、pending-steer hydration、最多128个longest-prefix compiler/estimator trial；只到stable consumption plan形成前 | 给每个trial重置120s；超时后仍消费steer或保留safe-point/borrow |
| `FOREGROUND_CANONICAL` | 120s fresh/transaction or query | Runner与Host面向产品的canonical read、mutation、stable candidate write、ACK-unknown confirmation；包括Plan/external result/Terminal observation等Host command path | 跨两个transaction、model call、tool call或human wait复用 |
| `WRITER_RENEWAL` | 当前10s | `renew_host_writer`一次attempt | 套用120s；deadline不得达到或越过当前30s writer lease expiry |
| `PROVIDER_TRANSPORT` | connect/write/pool 120s；read-idle 600s；total `None` | 一次physical transport attempt | 继承planning/canonical/turn deadline |
| `NONTERMINAL_TOOL_INVOCATION` | 600s logical watchdog | 一个已commit attempt对应的exact physical invocation及drain | 丢弃late exact outcome；把thread logical timeout宣称为physical kill |
| `TERMINAL_FOREGROUND_DECISION` | 120s outer decision；`yield_time_ms <= 30s` inner UX threshold | spawn、owner publication、foreground complete-or-yield decision与exact result/identity交付 | 把120s当process lifetime；在identity未交付时留下unowned process |
| `HUMAN_INTERACTION` | contract-specific finite expiry或`None` | ordinary confirmation、Plan QUESTION/DRAFT_REVIEW | 继承runner/turn deadline |
| `DURABLE_JOB_OR_MAINTENANCE` | 保留当前owner-specific contract | exact-four job executor、blob GC、maintenance scan等 | 因Round 5A改用foreground 120s；借机改变job retry/claim语义 |
| `HOST_SESSION_CLOSE` | 120s absolute/close task | 一个Host session的admission stop、cancel/kill与physical join | 给每个substep重置120s；waiter cancel后取消close task；超时后detach owner |
| `DURABLE_JOB_EXECUTOR_CLOSE` | 独立120s | HostCore shutdown中的job executor cancellation、attempt settlement与physical drain | 复用session-close constant；改变30/45秒job attempt/retry contract |
| `BLOB_GC_CLOSE` | 独立120s | blob-GC task/I/O owner shutdown与physical drain | 复用session-close constant；把该值当单次GC transaction budget |

composition必须断言：

```text
WRITER_RENEW_INTERVAL_SECONDS
  + writer_renew_attempt_seconds
  + writer_renew_safety_margin_seconds
  < WRITER_LEASE_SECONDS
```

当前值为`10 + 10 + 5 < 30`。仅检查`renew_attempt < lease`不充分。`terminal_foreground_decision_seconds`必须严格大于public `yield_time_ms`最大值30秒，使正常yield决策不会与outer watchdog同刻竞速。session close、job executor close与blob-GC close使用三个独立policy field；初始值虽都取120秒，但不能共享常量或owner，后续可以独立校准。job的30/45秒attempt total、maintenance/GC单次operation数值本轮不变；这里只放宽它们的shutdown physical drain。

---

## 4. Runner hard cut

### 4.1 删除model-call admission cap

从production constructor删除：

```text
maximum_model_calls_per_turn
```

从`Stage2RuntimeLimits`删除：

```text
model_calls_per_turn_hard
```

`run_accepted_turn()`改为语义循环：

```python
while True:
    model_call_count += 1       # observation only
    dispatch = await prepare_next_dispatch(...)
    response = await execute_provider_operation(...)
    accepted = await accept_complete_assistant(...)
    if not accepted.tool_calls:
        return completed
    await execute_and_accept_tool_batch(...)
```

必须物理删除：

```text
while model_call_count < maximum_model_calls_per_turn
RuntimeError("model-call limit exhausted")
```

不得替换为：

- `sys.maxsize`；
- 200/256 emergency cap；
- hidden environment default；
- compiler item count推导出的step cap；
- per-tool总次数；
- elapsed-time polling；
- generic no-progress detector。

`model_call_count`与`tool_call_count`继续保留在`KernelRunResult`/diagnostic中，但不能参与authorize、compile、provider open或tool dispatch。

### 4.2 删除turn-wide deadline

从runner constructor删除：

```text
operation_timeout_seconds
self._operation_timeout_seconds
```

从以下调用链删除**turn admission时创建**并向后传递的共享`deadline_monotonic`参数；需要deadline的leaf必须从其closed owner取得：

```text
_run_turn -X-> run_accepted_turn(turn deadline)
run_accepted_turn -> _prepare_provider_dispatch(new planning deadline)
provider loop -> tool batch -> result settlement(each owner-local deadline)
```

`run_accepted_turn(turn_id)`不得接收“从turn admission时开始”的deadline。resume、automatic Plan continuation、busy steer和SUBAGENT_TASK使用相同规则。

### 4.3 dispatch planning与canonical operation deadlines

Round 3.1的`PreparedSteerSuffixAdmissionPlan`不是若干互不相关的I/O。每次model call在任何steer consumption之前必须冻结一份`PROVIDER_DISPATCH_PLANNING` absolute deadline，并贯穿：

```text
freeze exact predecessor/current canonical cut
-> prepare target + scope-filtered tool-surface borrow
-> freeze one-cut dynamic source facts
-> read/hydrate target-lane pending rows
-> quote longest acceptable FIFO prefix
-> run bounded compiler/estimator trials
-> freeze stable consumption plan or no-steer dispatch plan
```

这份planning deadline也约束pure compiler trials；它不是I/O deadline的简单相加。若在stable plan形成前超时，必须consume 0 queue rows、discard planning state、release safe-point handle与borrow，且旧continuity epoch不变。不得让128个trial各自获得120秒。

stable consumption plan形成后，steer consume/confirmation使用新的`FOREGROUND_CANONICAL` deadline。成功后重读canonical cut并证明它只追加exact selected suffix；actual compile必须复用planning时冻结的target/tool/source facts，不得二次采集clock、skill、catalog或surface。下一次model call重新创建planning deadline。

除上述复合planning外，每个独立foreground canonical transaction/query现场生成新的canonical deadline：

```text
start_root_turn                 fresh 120s
start_subagent_turn             fresh 120s
dispatch planning read group    one shared planning-scoped 120s
consume/confirm steer           fresh 120s per attempt
accept assistant candidate      fresh 120s per write/confirmation attempt
accept tool attempt             fresh 120s
accept tool result              fresh 120s per write/confirmation attempt
complete/interrupt turn         fresh 120s
content/blob read or publish    owner-local fresh deadline
```

这不意味着同一prepared semantic candidate可以变化。candidate IDs、digest、occurred-at、event drafts和subject保持冻结；只有physical `WriteAttemptGuard/deadline`可替换。

Host command、Plan resolution、external result acceptance、Terminal observation installation与interaction resolution也属于`FOREGROUND_CANONICAL`，不能继续保留偶然的10秒通用deadline。writer renewal、job/maintenance/GC与close明确不属于该类，按3.5矩阵处理。

### 4.4 ACK unknown

operation timeout不能把已提交canonical mutation误报为失败。已有stable candidate路径继续遵循：

```text
physical write exits/raises
    -> exact confirmation with fresh canonical deadline
        FULL      -> accept winner, continue
        NONE      -> retry same semantic candidate with fresh guard/deadline
        CONFLICT  -> terminalize using existing typed conflict path
```

本轮不新增receipt、repair job或event。没有stable candidate的mutation不得因本轮deadline重构获得“盲重试”许可；应先沿用/补齐当前exact-confirm seam。

turn admission属于该inventory，不能因它位于loop之前而例外：

```text
PreparedTurnAdmissionCandidate =
    PreparedRootTurnAdmission(
        command_id, turn_id, revision_0_id, permission_snapshot_id,
        initial_entry_id, content identity, requested permission,
        occurred_at/actor, complete committed event draft
    )
  | PreparedSubagentTurnAdmission(
        task_id, turn_id, revision_0_id, permission_snapshot_id,
        initial_entry_id, objective content identity,
        occurred_at/actor, complete committed event draft
    )
```

ROOT现有`session_commands` compatible-winner检查可以继续作为canonical anchor，但runner不能在write异常后直接退出；它必须用fresh deadline执行stateless exact confirmation，并验证command、turn、revision-0、permission snapshot、entry与`UserMessageAccepted` occurrence全部exact join。SUBAGENT_TASK没有command row，也必须增加窄的stateless confirmation query，按stable task/turn/revision/entry/event identity验证all-or-none。event ID/occurred-at必须在candidate factory中冻结，不能在repository write内部临时重生。

处置统一为：

```text
write raises/ACK unknown
  -> confirm exact candidate
       FULL      -> run exact accepted turn once
       NONE      -> reissue same candidate under current writer with fresh deadline
       CONFLICT  -> fail closed; never start provider
```

confirmation或reissue完成前，ROOT active slot/subagent runner owner不得释放。不得为此新增session command kind、relation、receipt或repair graph。

### 4.5 human wait

以下等待不消耗turn-wide或provider/tool operation deadline：

- Plan QUESTION；
- Plan DRAFT_REVIEW；
- future明确声明为indefinite的typed human interaction。

用户响应FULL后，下一次canonical I/O、provider或tool operation各自取得新watchdog。不得恢复等待前剩余秒数。

当前ordinary tool confirmation保留自身10分钟expiration policy；它到期后按既有typed deny/expired语义结算。该值是interaction owner的产品策略，不是turn剩余时间，本轮不删除或延长。未来interaction必须由自己的closed contract选择`FINITE_EXPIRY | INDEFINITE`，不能隐式继承runner deadline。

### 4.6 closed termination reasons

正常turn只可由以下条件终结：

| 条件 | Canonical结果 |
|---|---|
| provider完整响应且没有follow-up tool/control | `COMPLETED` |
| Plan lifecycle显式结束/切换 | 按Round 4 contract |
| user cancel | `INTERRUPTED`，既有reason |
| Host close/replacement | `INTERRUPTED`，不恢复coroutine |
| provider retries耗尽 | 既有provider failure/interrupt path |
| current compiled input超过模型/physical contract | 既有typed input failure；等待Round 5B compaction改善 |
| canonical conflict/corruption | fail closed |
| physical effect outcome未知 | attempt保留；由missing result + interrupted turn派生 |

下面两项必须消失：

```text
MODEL_CALL_LIMIT_EXHAUSTED
TURN_WALL_CLOCK_EXHAUSTED
```

如果当前没有对应枚举，也不得在本轮新增。

---

## 5. Provider operation contract

### 5.1 timeout维度

OpenAI-compatible transport不得继续只接受一个scalar timeout。最小transport policy必须分别表达：

```text
connect timeout = 120s
write timeout   = 120s
pool timeout    = 120s
read idle       = 600s
total response  = None
```

推荐通过`httpx.Timeout`或SDK等价typed配置传入。若某adapter无法分别表达，必须在adapter内部实现等价idle watchdog并记录受限capability；不得回退为600秒total response timeout。

timeout policy必须由model owner显式注入transport，adapter不得靠一个新的global default猜调用来源：

```text
OpenAITransportTimeoutPolicy =
    FOREGROUND_AGENT(
        connect=120s, write=120s, pool=120s, read_idle=600s, total=None
    )
  | DURABLE_JOB_ATTEMPT(
        exact_attempt_deadline,
        connect/write/pool/read <= remaining attempt time
    )
```

- `DirectKernelModelPort`只接受`FOREGROUND_AGENT`并把它显式传给Responses与Chat Completions transport；
- `DirectKernelJobModel`只接受`DURABLE_JOB_ATTEMPT`，由`KernelDurableJobExecutor`在每次exact job attempt中绑定当前`deadline_at`；
- job的30/45秒catalog attempt deadline仍是总owner边界，transport field取owner默认与remaining attempt time的较小值，不能因foreground read-idle为600秒而延长job；
- 两个owner可以复用typed `httpx.Timeout`构造helper，但不得共享一份隐式mutable/default policy；
- 本轮不改变exact-four job catalog、retry次数、safety class或provider token cap。

preflight与physical open必须exact join同一transport policy fingerprint；该fingerprint只属于process-local execution binding，不进入provider input或durable event。

### 5.2 activity定义

`provider_stream_idle_seconds`衡量连续没有transport activity的时间，而不是从request open开始的总时间。

以下活动应重置read idle：

- provider data frame；
- SSE heartbeat/comment或SDK可观察的transport keepalive；
- translated raw delta；
- usage/terminal frame。

如果SDK只在完整semantic item时暴露读取，底层read timeout对任何received bytes的重置即可；不得为了统计idle把raw transport body写入AgentEvent。

### 5.3 retry与semantic output

保留当前provider retry contract：

- retry次数与backoff是一次model operation内部的transport policy；
- retry不计为新的agent model-call step；
- 已产生不可安全重放的semantic output后不得自动重开另一transport attempt；
- process-local Live deltas不构成canonical assistant commit；
- 只有完整assistant message才按现有atomic path接受。

Round 5A不提高默认retry次数，也不引入durable provider attempt journal。

### 5.4 cancellation

user cancel/Host close可以取消当前provider transport；transport必须physical close。它不会因为turn本来可以无限推进而获得跨Host恢复承诺。

### 5.5 provider input/output cap明确延期

当前：

```text
provider input hard cap  = 128_000 tokens/call
provider output hard cap = 16_384 tokens/call
model default context    = 256_000 tokens
```

Round 5A不修改这些值，不新增env/config入口，也不以`>128K`作为DoD。后续规格应单独决定：

- model registry/remote metadata与local fallback优先级；
- 默认256K与1M模型；
- user/env override；
- output reserve与safety margin；
- provider reported limit conflict。

本轮唯一要求是不要把这两个per-call capability cap误称为turn progress budget。

---

## 6. Tool operation contract

### 6.1 不新增统一tool-call cap

当前runner没有tool次数gate。Round 5A必须保持：

```text
tool_calls_per_turn = None
```

同一assistant response中的tool batch仍遵守message-before-dispatch、attempt-before-effect和现有permission policy；长程语义不改变effect safety。

### 6.2 UX yield不是physical timeout

Round 2已冻结Terminal默认`yield_time_ms=10_000`。该值只决定：

```text
completed before threshold -> foreground complete result
still running              -> RUNNING/yielded result + process_id
```

不得解释为：

- kill after10秒；
- tool attempt unknown；
- turn timeout；
- tool-call budget消耗完毕。

yielded process继续由当前Host process-local owner管理；Host close按既定产品决策终止并join，不跨Host重绑。

Terminal launch不能只把一个opaque thread future交给outer watchdog，因为process会在线程返回前进入registry，而调用层此时尚未获得`process_id`。本轮新增纯process-local `TerminalForegroundDecisionAttempt`；它在spawn前由Terminal registry安装，handle只携带`attempt_id + owner_host_session_id`，不携带callback、process object或durable identity：

```text
PREPARING
  -> PROCESS_INSTALLED(exact internal process_id, adoption_allowed=true)
  -> RESULT_READY(exact completed | yielded result)
  -> SETTLED

PREPARING | PROCESS_INSTALLED
  -> ABORT_REQUESTED
  -> RESULT_READY(exact interrupted result after required kill/join)
  -> SETTLED
```

worker与watchdog只能在同一Terminal registry lock下推进attempt：

- watchdog在`RESULT_READY`之后到达：exact result胜出，不得改写；
- watchdog在`PREPARING`到达：禁止后续process publication；若spawn已越过physical boundary，worker必须立即进入abort path；
- watchdog在`PROCESS_INSTALLED`到达：将该exact process设为adoption-disabled，kill整个group并join；不得调用会波及同Host其他process的`release_owner()`；
- `SETTLED`只在caller消费exact outcome或abort结果后进入，随后删除attempt；
- Host close可枚举并abort当前Host全部attempt，但普通单次watchdog只能寻址exact attempt。

Terminal调用不再使用与`yield_time_ms`相等的generic outer deadline。`TERMINAL_FOREGROUND_DECISION`的120秒只约束上述attempt中的spawn、owner installation以及“foreground complete或yield exact identity”的交付，不约束交付后的process lifetime。其launch matrix冻结为：

| Physical状态 | Caller可见identity/result | 处置 |
|---|---|---|
| decision边界内形成exact completed/yielded result | 尚未交付 | 正常交付一次；不得因靠近watchdog而丢弃 |
| outer watchdog先触发，但worker drain后已形成exact result | 尚未交付 | exact result胜出并交付；记录operational late，不改写为timeout |
| outer watchdog触发且尚未形成可交付identity/result | 无 | owner将该identity标为不可采用，kill整个process group并join；只有join后的typed interrupted result可提交 |
| kill/join后仍无法证明terminal outcome | 无 | 不提交伪造result；turn interrupted，既有attempt-without-result派生unknown |
| exact yielded result已形成 | exact process ID属于当前Host | generic caller timeout不得触发manager的“invisible process”rollback；必须把原result交给caller |

因此不允许出现“process已注册并继续运行，但任何caller都没有得到process ID”的状态。

该attempt只适用于`terminal` launch。`terminal_process`已经携带exact `process_id`，不得为了复用launch owner而再次包成spawn attempt；它按action使用closed process-local outcome class：

| `terminal_process` action | Outcome class | deadline/exception后的处置 |
|---|---|---|
| `list`、`log`、`poll`、`wait` | `TERMINAL_OBSERVATION` | late exact return仍提交；最终异常可形成bounded typed failure；不得kill被观察process |
| `write`、`submit`、`close_stdin`、`kill` | `TERMINAL_EFFECT` | late exact return仍提交；异常/取消而无法证明结果时不合成known failure result，turn interrupted并由attempt-without-result派生unknown；不自动重试 |

action class由input discriminator与closed catalog contract共同冻结并进入tool binding fingerprint；不能继续把整个`terminal_process`只标为一个name-level `terminal` severity。`kill`的exact success必须以manager已经完成其既有physical join contract为准。

### 6.3 non-Terminal builtin

`read_file/search_files/edit_file/write_file/todo/artifact_read`等同步builtin使用独立`nonterminal_tool_attempt_seconds=600`operation watchdog。该值是悬挂filesystem/driver调用的远端检测边界，不是正常文件规模预算；只有adapter具备cooperative cancellation、driver timeout或subprocess kill时，它才同时构成physical hard bound。

`KernelSessionIO.run()`当前在logical deadline后drain thread、丢弃task返回值并抛`TimeoutError`。该行为适合“canonical write outcome需另行exact-confirm”的generic I/O，却不适合tool physical invocation。本轮必须增加一个**仅process-local、非序列化**的tool invocation outcome carrier；可以是specialized `run_tool_invocation()`，不得改变canonical repository call的ACK-unknown规则：

```text
PhysicalToolInvocationOutcome[T] =
    RETURNED_EXACT(value: T, timing: ON_TIME | LATE_AFTER_WATCHDOG)
  | RAISED(error_class, timing: ON_TIME | LATE_AFTER_WATCHDOG)
  | CANCELLED_WHILE_RUNNING(terminal: RETURNED_EXACT | RAISED)
```

carrier只证明exact physical worker最终如何退出；它不是durable receipt，不进event或tool-result metadata。logical timeout/caller cancellation发生后仍先drain exact thread，再由下列closed matrix决定canonical settlement：

| Physical outcome | Tool effect class | Canonical处置 | 自动重试 |
|---|---|---|---:|
| `RETURNED_EXACT(result)`，无论on-time/late | 任意 | 保留并接受exact result；late只发Operational diagnostic | 0 |
| `RAISED` | `read_only` | 可提交bounded typed timeout/system failure result | 0 |
| `RAISED` | `bounded_write`或`unknown_effect` | 不合成`SYSTEM_ERROR` result；interrupt turn，保留attempt-without-result并在read time派生unknown | 0 |
| caller cancel后最终`RETURNED_EXACT(result)` | 任意 | 若同一Host current writer仍有效，则追加该call唯一known result并保持turn interrupted；writer已stale则不得canonicalize，保留attempt-without-result/derived unknown，最多发bounded Operational diagnostic | 0 |
| caller cancel后最终`RAISED` | 非read-only | attempt-without-result + interrupted | 0 |

effect class必须来自已冻结、与advertised binding exact join的closed tool contract；当前builtin catalog的`read_only | bounded_write | terminal | unknown_effect`可以作为真源，不得在异常现场按tool name自由猜测。Terminal走6.2专用矩阵。

这里没有“旧Host late-result producer”。Repository允许current writer接受late exact result、reader也能lower已经接受的late outcome，不等于process-local result可跨Host交接。Round 5A明确禁止为stale writer新增handoff queue、durable receipt、result recovery job或新Host adoption owner。

必须保持：

- tool descriptor/input schema自己的范围约束；
- workspace/scope/permission校验；
- known tool outcome与artifact publication failure分离；
- timeout/cancellation后等待不可取消physical thread退出，读取其真实terminal outcome，再做canonical settlement；
- physical operation可能已经执行时不得自动重试side effect。

如具体tool拥有更精确、且大于正常健康耗时的timeout contract，可以使用其owner-specific值；不得由runner剩余turn时间压缩它。

需要准确区分logical watchdog与physical cancellation：Python worker thread不能被`asyncio`强制终止。specialized tool runner在600秒后可以标记logical late，但仍必须等待exact thread退出、保留其返回值或异常后才能向caller结算；不得detach。任何可能无限阻塞的新adapter必须先提供cooperative cancellation、driver timeout或subprocess kill/join，不能把“600秒”当成已经获得physical kill能力。现有builtin超过watchdog时保持fail closed，即使这意味着Host close继续等待真实owner退出；“fail closed”不允许覆盖一个已经返回的exact result。

### 6.4 subagent、memory与future MCP

- child turn自身同样没有model/tool step cap；
- `wait`工具的30/300秒只是单次等待请求，不是child lifetime；
- memory仍按用户决定进入后续专项，本轮不改变其job/timeout；
- future MCP adapter必须声明每项physical call timeout/retry与safety class，不能重新依赖runner总deadline。

### 6.5 tool surface borrow

Round 3.1冻结的borrow lifetime保持不变：

```text
preflight
-> provider stream
-> assistant acceptance
-> authorize
-> attempt acceptance
-> physical invoke
-> result settlement
-> close borrow
```

无限loop只会重复获取下一次response的borrow，不允许一个borrow跨不相关response永久持有。

---

## 7. Close、cancel与physical ownership

### 7.1 Host close默认

production `host_session_close_join_seconds`从5秒提升为120秒，并继续使用一个close-scoped absolute deadline。它只覆盖一次Host session close operation，不会从turn开始时计时；durable job executor与blob-GC shutdown分别拥有自己的120秒policy。三个字段数值相同只是初始校准，不构成同一owner。

close不能由CLI/Gateway request waiter本身充当推进owner。`HostCore`必须在自身lock下为每个`host_session_id`原子安装唯一process-local `HostSessionCloseAttempt`：

```text
OPEN
  -> CLOSE_TASK_INSTALLED(
       close_task,
       deadline_frozen_at_creation,
       close_conversation_requested
     )
  -> CLOSED | CLOSE_FAILED_QUARANTINED
```

- `close_conversation_requested`只能从false单调升级为true；后到waiter在join既有task前先合并该请求；
- 第一个caller创建task，所有caller和`HostCore.shutdown()`都`shield`等待同一task；
- waiter cancellation只detach该waiter，不能cancel close task；重试必须join同一attempt，不能创建第二套close sequence；
- close task唯一拥有admission stop、Terminal attempt/process abort、runner/provider cancellation、canonical terminalization、I/O drain与session map清理；
- deadline在task创建时冻结，后到waiter不能延长；逻辑deadline触发force/typed-failure path，但不能让task在仍有可变physical owner时退出或detach；
- `CLOSE_FAILED_QUARANTINED`意味着session对象不可复用或换代，且只有在所有仍可能写资源的owner已经physical terminal后才能成为attempt terminal state。

该attempt不序列化、不跨进程、不进入session row/event，也不是execution recovery owner。

close顺序继续是：

```text
stop new admission
stop monitor/new-turn admission
terminate current Host-scoped Terminal process groups
join process readers/watchers
cancel and join active runner/provider
settle canonical interrupted turn
drain subagent/tool/extension owners
close continuity owner
close repository I/O
release writer
```

不得为了在120秒内返回而：

- detach仍可写数据库的thread；
- 宣称process已退出；
- release/reusetool surface owner；
- synthesize tool result；
- markcanonical turn completed。

真实physical owner未退出时，close返回typed failure并保持对象不可复用；不能换代启动并发owner。

HostCore级shutdown在所有session close attempt terminal后，才分别关闭durable job executor与blob-GC owner。三者即使初始同为120秒，也不能继续共享一个`HOST_CLOSE_SECONDS`常量；配置、注入、deadline creation与测试必须能独立变化，避免未来任一owner校准时扩散到另外两类shutdown。

### 7.2 user cancellation

user cancellation不是deadline：

- 停止下一次provider/tool admission；
- 当前provider transport physical close；
- 已dispatch tool按其owner的cancel/unknown语义；
- canonical turn最终进入`INTERRUPTED`；
- pending steer/future prompt按既有queue contract处理；
- process-local continuity epoch随后可按Host/session lifecycle关闭。

### 7.3 ordinary detach

客户端detach不取消turn。与Round 3.1一致，同一Host继续运行并保留ROOT epoch；重新attach读取canonical snapshot + committed/live delta。Round 5A不新增后台durable execution owner，Host退出仍会interrupt。

---

## 8. 既有physical limits的处置

### 8.1 明确保留的产品边界

本轮不修改：

- Round 1完整tool artifact、head/tail preview、artifact read分页；
- Round 2 Terminal 16 MiB/process与128 MiB/Host retained bytes；
- prompt queue admission bound；
- live observer/ring bound与GAP；
- canonical blob与inline content物理边界；
- job/worker/Host concurrency；
- committed observation分页与wire bounds。

这些边界命中时必须使用各自既有degrade/reject/GAP语义，不能统一升级为“long-horizon turn exhausted”。

### 8.2 暂时保留但不予长期背书的实现guard

以下Round 3/3.1常量本轮保持不变：

```text
canonical provider input      4,096 items / 16 MiB
compiler working set          64 MiB
source candidates             32
tool specs                    64
continuity installed          320 MiB/Host
continuity installed+prepared 640 MiB/Host
```

它们是当前实现guard，不是成熟产品已经验证的long-horizon budget，也没有在Codex/grok-build中找到完全对应物。Round 5A activation不得宣传这些数字为长期架构真理。

特别是：

- `32 sources / 64 tools`可能限制未来MCP与dynamic tool discovery；
- `4,096 items`可能在compaction前成为间接step cap；
- `320/640 MiB`反映当前immutable epoch物化方式，不应进入用户配置或durable contract。

Round 5B或MCP专项必须重新评估它们。当前若命中，provider open必须为0并返回现有typed resource failure；不得静默丢历史、reset epoch或绕过tool surface。

### 8.3 禁止“所有东西都必须有数字”

architecture guard不得要求所有policy字段都是finite positive number。应明确允许：

```text
turn total       absent
model call count absent
tool call count  absent
provider total   absent
Plan question wait absent
```

代码审查应拒绝以单测防无限循环为理由向production constructor重新加入这些字段。测试应通过有限scripted model、explicit cancellation或test harness timeout保证终止。

---

## 9. Prefix continuity与未来compaction seam

### 9.1 Round 5A不能重写prefix

任意两次相邻调用，在同一Host、scope与continuity epoch内继续满足：

```text
system[n + 1]   == system[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
```

model call编号超过24、turn运行超过120秒、provider cache miss、累计token很高或用户detach均不是reset理由。

### 9.2 context不足时的当前行为

本轮没有compaction。如果fixed prefix或new suffix无法装入当前model/physical budget：

- provider transport不得打开；
- 不得回头重写已安装历史；
- 不得伪造summary；
- 使用Round 3.1现有typed compile/resource failure终结或中断当前turn；
- 机器证据必须明确标记“等待Round 5B compaction”，不能把Round 5A宣称为完整PHC-07恢复。

### 9.3 Round 5B唯一合法rebase seam

未来compaction只能在provider safe point：

```text
freeze canonical cut
-> create/accept canonical snapshot or binding revision
-> close old process-local continuity epoch
-> cold compile exact adopted base + protected tail
-> install new epoch
-> continue same canonical user turn
```

Round 5A不得创建placeholder API、dummy summary、`CompactionRequested` event或background job来“为未来预留”。现有dormant schema/repository primitive保持原样即可。

---

## 10. Operational observation

### 10.1 可观察但不gate

runner已经拥有：

- `model_call_count`；
- `tool_call_count`；
- provider usage report；
- per-call index与turn identity。

本轮可以在既有operational diagnostic/hook中增加或保留bounded summary，但不得新增durable event。可选字段：

```text
turn_id
scope
model_call_count
tool_call_count
elapsed_millis_since_turn_start
cumulative_input_tokens_reported
cumulative_cached_input_tokens_reported
cumulative_output_tokens_reported
```

规则：

- usage缺失保持unknown，不估造成canonical fact；
- cached/non-cached只作provider report；
- elapsed使用monotonic，不持久化为恢复依据；
- 计数溢出不可能改变run；
- 不把计数以“剩余预算”形式注入prompt；
- diagnostic丢失不影响canonical commit或physical execution。

### 10.2 本轮禁止的智能治理

不实现：

- query/evidence fingerprint no-progress detector；
- 重复工具自动deny；
- exploration/restricted/finalization phase；
- cost/USD/token hard budget；
- automatic final answer reminder；
- global parent/child rollout account。

这些可能是未来可选策略，但Codex rollout budget当前默认关闭、grok-build interactive默认也无task budget；它们不是Round 5A恢复长程happy path的必要条件。

---

## 11. Failure matrix

| 场景 | Provider open | Canonical/physical处置 | Turn处置 |
|---|---:|---|---|
| 第25、51或201次合法model call | 是 | 正常compile/execute | 继续 |
| turn累计运行超过120秒，但当前operation健康 | 是 | 无特殊动作 | 继续 |
| 单个provider response总时长超过600秒，但每个idle gap小于600秒 | 是 | stream继续 | 继续 |
| provider连续600秒无transport activity | 已打开 | 当前attempt timeout；按既有retry policy | retry成功则继续，否则interrupt |
| provider connect/write超过120秒 | 否/未完成 | 当前attempt失败；安全时retry | 无step/time exhaustion |
| canonical transaction超过120秒 | 无关 | physical drain；exact confirmation/typed failure | 不得盲重试effect |
| ROOT/SUBAGENT turn admission ACK unknown | 0，直到FULL | stateless exact-confirm完整admission candidate；NONE才reissue，CONFLICT fail closed | FULL后exact runner一次；不得留下RUNNING turn无owner |
| 多个canonical operations累计超过120秒，每个均健康 | 按需 | 每项fresh deadline | 继续 |
| dispatch planning超过120秒、尚未形成stable consumption plan | 0 | consume 0；discard plan并释放safe-point/borrow | typed planning failure；旧epoch/queue不变 |
| Terminal运行超过10秒 | 已按正常call | yield process ID；不kill | tool result后继续 |
| Terminal foreground outer watchdog与yield/result竞态 | 无关 | exact result已形成则交付；否则kill group并join，不留隐形process | known result或interrupted/unknown按6.2 |
| `terminal_process` observation action超时/异常 | 无关 | 不kill目标process；late exact return或bounded typed failure | 继续，不重跑 |
| `terminal_process` effect action异常且结果不可证 | 无关 | 不合成known failure；目标process保持其真实状态 | interrupted/derived unknown，不重跑 |
| yielded Terminal运行数小时 | 无关 | 当前Host继续owner；monitor/process可读 | 继续/等待后续观察 |
| non-Terminal builtin超过600秒后exact返回 | 无关 | physical drain并保留late exact result；只记Operational late | 继续，不重跑 |
| non-Terminal read-only builtin最终抛异常 | 无关 | bounded typed timeout/system failure result | 继续/按模型语义，不重跑 |
| bounded-write/unknown-effect builtin最终抛异常 | 无关 | 不合成result；attempt-without-result | interrupted/derived unknown，不重跑 |
| Plan QUESTION等待数小时 | 否 | waiter保持process-local；canonical OPEN | 回答后继续 |
| user cancel | 停止新open | physical owner按类型cancel/join | interrupted |
| close waiter被cancel | 停止新open | waiter detach；HostCore close task继续，后续caller join同一attempt | close继续推进 |
| Host session close | 停止新open | 唯一Host-owned task使用120秒close-scoped watchdog；不伪造退出 | interrupted |
| tool exact result完成时writer已stale | 无关 | 不跨Host投递、不canonicalize；bounded Operational diagnostic | attempt-without-result/derived unknown |
| 第4097个canonical item命中当前guard | 0 | typed compile/resource failure | 当前产品边界终止；等待Round 5B改善，不静默reset |
| continuity 320/640 MiB guard命中 | 0 | candidate不install | typed failure，不rewrite |
| provider input超过当前128K cap | 0 | 现有capability failure | 本轮不修；明确延期 |
| live/operational hook失败 | 不受影响 | detach/degraded | 继续 |
| 模型长期低增益循环 | 是 | 仅可观察 | 默认继续，用户可cancel |

---

## 12. 修改面

### 12.1 production重点

预计至少涉及：

- `src/pulsara_agent/conversation_kernel/limits.py`
  - 删除`model_calls_per_turn_hard`；
  - 不删除128K/16K；
  - 将共享短timeout拆为清晰operation defaults，或由新policy接管。
- `src/pulsara_agent/conversation_kernel/execution_watchdogs.py`
  - 新增纯process-local frozen policy；无serialization。
- `src/pulsara_agent/conversation_kernel/runner.py`
  - `while True` semantic loop；
  - 删除turn deadline；
  - 一次dispatch planning共享其owner deadline；stable plan后的独立canonical transaction各自fresh；
  - ROOT/SUBAGENT turn admission使用完整stable candidate + stateless exact confirmation；
  - preserve counts as observation；
  - preserve Round 3.1 continuity/steer/borrow。
- `src/pulsara_agent/conversation_kernel/io.py`
  - 保留physical drain；
  - API只消费operation-scoped deadline；
  - 为tool physical invocation提供不丢late return/exception的process-local outcome seam；
  - 不持有turn state。
- `src/pulsara_agent/conversation_kernel/tool_runtime.py`
  - Terminal yield与nonterminal hard watchdog分层；
  - 删除30秒通用前台剩余时间语义；
  - `terminal` launch使用exact foreground-decision attempt；`terminal_process`按action区分observation/effect；
  - 按frozen effect class结算late return与exception，不把unknown effect伪装成`SYSTEM_ERROR`。
- `src/pulsara_agent/capability/builtin_catalog.py`
  - 为`terminal_process`冻结action-level outcome class并纳入binding fingerprint；不新增provider-visible tool或durable vocabulary。
- `src/pulsara_agent/conversation_kernel/host.py`
  - HostCore安装并shield唯一session close task；waiter cancel只detach；
  - session close、job executor close与blob-GC close使用独立policy field；
  - writer renewal继续使用小于lease expiry的10秒owner deadline；
  - Host public canonical command使用fresh foreground deadline；
  - active turn不因累计时间清除slot。
- `src/pulsara_agent/llm/adapters/openai/client.py`
  - scalar timeout改为typed connect/write/read/pool配置。
- `src/pulsara_agent/llm/adapters/openai/responses.py`
- `src/pulsara_agent/llm/adapters/openai/chat_completions.py`
  - provider idle而非total response语义；保留safe retry。
- `src/pulsara_agent/conversation_kernel/direct_model.py`
  - 显式注入foreground typed transport timeout policy。
- `src/pulsara_agent/conversation_kernel/job_model.py`
- `src/pulsara_agent/conversation_kernel/jobs.py`
  - job transport绑定exact attempt deadline；保留30/45秒catalog total owner与既有retry。
- `src/pulsara_agent/terminal_process/manager.py`
  - 在spawn前安装exact foreground-decision attempt，freeze identity交付/abort/kill-join竞态；不改变retained output或process lifetime contract。

根据真实调用图可以修改小范围helper与tests，但不得进入schema/migration/event vocabulary。

### 12.2 不应修改

- PostgreSQL clean baseline及migration；
- committed/live AgentEvent vocabulary；
- Protocol v3 schema；
- Go TUI；
- context snapshot schema；
- compaction job/handler；
- memory subsystem；
- Round 1 artifact schema；
- Round 2 Terminal retained-output contract；
- Round 3 compiler allocation算法；
- Round 3.1 continuity epoch结构；
- Round 4 Plan rows与events。

如实现发现必须修改上述面，先回到文档评审，不得自行扩scope。

---

## 13. 实施切片

### R5-0：baseline与architecture guard

1. 记录checkpoint HEAD、本文hash、Gap Index hash。
2. 建立当前oracle与targeted/full test baseline。
3. 新增architecture test，证明当前错误事实存在：24-call field、runner有限loop、turn-wide deadline。
4. 冻结本轮oracle仍为`34 / 23 / 15 / 2 / 26 / 4`。

R5-0不得修改production行为。

### R5-A：semantic loop hard cut

1. 删除production model-call cap字段与constructor参数。
2. 改为`while True`。
3. 删除`model-call limit exhausted`。
4. 保持final response、Plan、tool、steer、cancel的现有closed exit。
5. 用scripted model证明超过24与50次后仍可正常finalize。

### R5-B：operation-scoped deadline

1. 引入process-local watchdog policy。
2. 删除turn admission创建并复用的120秒deadline。
3. 保留Round 3.1单次dispatch planning的共享absolute deadline；stable consumption plan之后的canonical consume/confirmation使用fresh write deadline。
4. deadline factory只接受closed owner，不接受自由seconds；composition验证renew interval + attempt + margin小于lease。
5. 按3.5 closed matrix迁移foreground canonical call；writer renewal、job/maintenance/GC不得机械套用120秒。
6. ROOT/SUBAGENT admission与tool result/assistant ACK-unknown均保留same candidate + fresh confirmation/write attempt。
7. human wait后创建新operation watchdog。

### R5-C：provider/tool/close分层

1. provider scalar timeout拆为connect/write/pool/read-idle；foreground与job owner显式注入不同typed policy，total语义分别由owner决定。
2. Terminal yield不受nonterminal watchdog解释。
3. nonterminal builtin使用600秒logical backstop，并用process-local outcome carrier保留late exact result。
4. `terminal` spawn前安装exact foreground-decision attempt；`terminal_process`按action outcome class结算。
5. HostCore安装唯一shielded session close task；session/job/blob-GC close policy物理解耦。
6. 不改变tool effect safety或retry class，不新增跨Host late-result owner。

### R5-D：activation evidence

1. 跑targeted、full pytest、PostgreSQL、ruff、compileall。
2. Protocol未改也要运行generator check与Go compile/test作为regression gate。
3. 建立machine-readable activation evidence。
4. 更新Gap Index为“PHC-07A restored / PHC-07B compaction open”。
5. 只有全部gate通过后将本文状态改为`ACTIVATED`。

---

## 14. 测试矩阵

### 14.1 必须新增

建议集中在：

```text
tests/test_round5_long_horizon_execution_envelope.py
tests/test_round5_long_horizon_postgres.py
```

至少覆盖：

1. **64次model call**：前63次产生合法tool follow-up，第64次final；证明超过24/50仍完成。
2. **大量tool calls**：同一turn超过历史64次tool call仍能finalize；每次使用无side-effect test tool。
3. **累计时间大于120秒**：fake monotonic或注入watchdog，不sleep；多个健康operation各自成功。
4. **deadline factory admission**：注入clock/factory，断言每个独立foreground canonical operation都重新调用factory并获得完整owner budget；合法的相邻deadline数值可以相等，不以“严格后移”证明freshness；semantic candidate identity不随physical attempt变化。
5. **dispatch planning bound**：safe-point freeze、hydrate与longest-prefix compiler trials只创建一份planning deadline；超时consume 0、旧epoch/queue/borrow不变；stable candidate后的consume/confirmation创建fresh canonical deadline。
6. **closed deadline factory**：API不接受自由seconds；每个owner返回policy唯一预算，未知owner失败；semantic candidate identity不随physical attempt变化。
7. **turn admission ACK unknown**：ROOT与SUBAGENT分别在commit ACK丢失时得到FULL/NONE/CONFLICT；FULL只启动一个runner，NONE只重写同candidate，CONFLICT时provider open=0；逐项验证turn/revision-0/permission/entry/event。
8. **typed OpenAI timeout plumbing**：foreground constructor断言SDK实际收到`httpx.Timeout(connect=120, write=120, pool=120, read=600)`等价四字段，而不是scalar；Chat Completions与Responses均覆盖。
9. **foreground/job transport separation**：同一adapter分别接收foreground policy与30/45秒job-attempt policy；job field不超过attempt remaining，catalog、retry与total attempt deadline不变。
10. **local SSE activity/idle**：bounded本地SSE server对Chat Completions与Responses分别证明持续comment/data bytes可使总时长超过read-idle仍完成；静默gap超过read-idle会超时。无需真实provider。
11. **long provider response**：总时长大于idle watchdog但每次activity间隔更短，必须完成。
12. **provider idle**：超过idle watchdog触发attempt failure；无semantic output时按既有policyretry。
13. **semantic output后transport失败**：不得自动打开duplicate attempt。
14. **late exact tool outcome**：logical watchdog先触发、physical worker后返回exact result；result必须被接受且physical invoke count为1。
15. **effect exception matrix**：read-only异常形成typed failure；bounded-write/unknown-effect异常不写伪`SYSTEM_ERROR` result，turn interrupted并派生unknown；invoke count均为1。
16. **stale writer late result**：exact result在writer stale后到达时canonical write增量为0、无跨Host handoff；attempt/result/turn读取为unknown并只有bounded Operational diagnostic。
17. **long tool then result commit**：tool结束后result transaction获得完整fresh canonical watchdog。
18. **Terminal yield**：超过UX threshold仅yield，不命中600秒nonterminal policy。
19. **Terminal launch attempt races**：逐一覆盖watchdog发生在`PREPARING`、`PROCESS_INSTALLED`、`RESULT_READY`；只终止exact process、不影响同Host sibling，不丢result或留下无caller持有的process。
20. **terminal_process action matrix**：list/log/poll/wait异常不kill process；write/submit/close_stdin/kill outcome不可证时不写known result、不自动重试。
21. **writer renewal**：composition断言`interval + attempt + margin < 30s lease`；长turn持续renew，调用方不能传120秒覆盖policy。
22. **owner classification**：session、job executor与blob-GC close分别从三个policy field取值；测试注入不同值证明没有共享常量。job 30/45秒attempt、maintenance/GC operation contract未被foreground policy改写。
23. **human wait**：QUESTION等待不消耗后续operation watchdog。
24. **user cancel after >24 calls**：可interrupt并physical drain。
25. **Host close waiter cancellation**：第一个waiter cancel只detach；close task继续；第二个caller与HostCore shutdown join同一task；close physical owner count始终为1。
26. **Host close after >120s simulated elapsed**：仍用close-task-scoped120秒，不继承过期turn时间；deadline只在task安装时冻结一次。
27. **ROOT与child**：二者均无call cap，continuity scope隔离不变。
28. **busy steer at call 25+**：safe point吸收steer并只追加suffix。
29. **strict prefix across64 calls**：SYSTEM/tools相同、messages逐call append-only。
30. **compiler resource guard**：仍在provider open前fail，不因本轮删除step cap被绕过。
31. **no schema/event drift**：oracle保持`34/23/15/2/26/4`。

### 14.2 测试保险丝

单测不能通过production `maximum_model_calls_per_turn`保证终止。应使用：

- finite scripted response list；
- test-level `asyncio.timeout`；
- explicit cancellation；
- fake operation watchdog；
- bounded property-based example count。

这些测试机制不得进入production API。

### 14.3 retained regression

至少重跑：

- Round 1 artifact targeted；
- Round 2 Terminal targeted；
- Round 3 compiler targeted；
- Round 3.1 continuity/steer targeted；
- Round 4 Plan/permission targeted；
- canonical runner/repository/Host tests；
- provider adapter/retry/model-resolution tests；
- PostgreSQL clean-v0/deep verify。

### 14.4 real-provider dogfood

真实provider只作integration observation，不用remote cache ratio证明correctness。建议记录：

- 至少一个多tool follow-up turn；
- provider每次call usage；
- local strict-prefix proof；
- Terminal yield后继续；
- 无API key/raw敏感正文；
- 无`model-call limit exhausted`或shared-deadline failure。

不要求真实provider稳定地产生64次调用；该hard gate由deterministic scripted model完成。

---

## 15. Architecture guards

activation时以下生产扫描必须为0：

```text
model_calls_per_turn_hard
maximum_model_calls_per_turn
model-call limit exhausted
turn_deadline
RolloutBudgetAccount
RolloutReservation
finalization_reserved_model_calls
emergency_model_call_limit
emergency_tool_call_limit
```

允许历史文档和archived_docs出现这些文本；当前`capability/tool_action.py`保留的closed `RolloutPhase`分类也不属于本轮task-budget owner，不得被该文本guard误删。guard应证明runner/Host不存在rollout admission account/reservation，而不是要求整个仓库不能出现“rollout”一词。

必须证明：

- runner production loop没有numeric iteration gate；
- `operation_timeout_seconds`不再是runner field；
- deadline factory只接受closed owner，不接受caller-supplied seconds；renew interval + attempt + margin严格小于lease；
- 每次dispatch planning只有一份planning-scoped absolute deadline，最多128个trial不会各自重置预算；
- 所有列入`FOREGROUND_CANONICAL`矩阵的独立operation使用fresh deadline；writer renewal仍严格小于lease expiry，jobs/maintenance/GC保留owner policy；
- ROOT与SUBAGENT turn admission具备完整stable candidate与stateless exact confirmation；
- provider total timeout为空，idle timeout存在；
- OpenAI SDK收到typed connect/write/pool/read timeout而非scalar；foreground/job transport policy可证明分层；
- Terminal yield与nonterminal watchdog不同源；
- `terminal` launch有spawn前exact decision attempt；`terminal_process` action-level outcome class闭合；
- tool logical timeout不会丢弃late exact return，effect-unknown exception不会合成known result；
- stale writer没有跨Host late-result handoff/adoption owner；
- Host session close deadline只在唯一HostCore-owned task创建时冻结；waiter cancellation不取消task；job/blob-GC close policy独立；
- no new relation/event/job/guard/subject；
- no compaction owner becomes reachable；
- no old Runtime/EventLog/provider-input recovery import returns。

---

## 16. 静态与动态验证

至少执行：

```bash
uv run pytest -q tests/test_round5_long_horizon_execution_envelope.py
uv run pytest -q tests/test_round5_long_horizon_postgres.py
uv run pytest -q

uv run ruff check .
uv run python -m compileall -q src tests tools
uv run python tools/generate_terminal_protocol_contract.py --check

(cd clients/terminal && go test ./...)
(cd clients/terminal && go vet ./...)

uv lock --check
git diff --check
```

此外检查：

- pytest collection为0 error；
- 新增skip/xfail为0；
- Markdown fence闭合；
- heading无重复；
- active本地链接存在；
- secret scan不包含`.env`值；
- clean PostgreSQL fresh install、second migrate、deep verify仍通过；
- activation evidence与实际HEAD/hash/test counts一致。

---

## 17. Definition of Done

只有同时满足下列条件，Round 5A才可标记`ACTIVATED`：

1. ROOT与SUBAGENT_TASK production loop没有固定model/tool次数上限。
2. 不存在跨整个turn复用的absolute deadline。
3. watchdog调用严格符合3.5 closed owner matrix：factory不接受自由seconds；dispatch planning共享一次总上界；独立foreground canonical transaction fresh；renew时序满足lease margin；jobs/maintenance不被机械改写。
4. ROOT/SUBAGENT turn admission在ACK unknown后exact-confirm/reissue同一candidate，不留下RUNNING turn无runner。
5. production foreground provider transport表达connect/write/pool/read-idle且没有total response timeout；job transport仍受30/45秒attempt owner约束；constructor与本地SSE测试证明真实wire语义。
6. 健康operation持续产生activity时，turn可超过120秒并继续。
7. deterministic test至少完成64次model call与超过64次tool call后正常finalize。
8. tool watchdog/cancel会drain并保留late exact outcome；read-only与effect-unknown exception按closed matrix结算，physical invoke不自动重跑；writer stale时不跨Host canonicalize。
9. Terminal长命令yield而不是被generic tool/turn timeout杀死；spawn前exact attempt和action-level matrix保证不丢result、不留下无caller持有的process且不误杀sibling。
10. HostCore唯一close task不受waiter cancellation影响；session、job executor与blob-GC close owner分离。
11. user cancel与Host close仍能physical drain并canonical interrupt。
12. Round 3.1 strict prefix在长循环中保持成立。
13. Round 1/2/3/3.1/4 retained tests全绿。
14. oracle保持`34 Committed / 23 Live / 15 subjects / 2 guards / 26 relations / 4 jobs`。
15. 没有新增durable budget、receipt、checkpoint、repair、replay、cross-Host result handoff或compaction owner。
16. 128K/16K与256K/1M问题在证据中明确标记为延期，而不是被静默改动。
17. Gap Index将PHC-07A标为恢复，同时PHC-07B compaction继续open；不得宣传完整PHC-07已恢复。

---

## 18. 明确延期的决策

以下不是Round 5A blocker：

1. 默认context window继续使用256K还是升级1M；
2. environment/model-registry/user override入口；
3. 128K input与16K output per-call cap的最终处置；
4. compaction trigger使用80%、85%、90%或动态阈值；
5. deterministic thinning与LLM summary的顺序；
6. single-turn内部compaction与跨turn compaction；
7. summary model是否使用Flash/double call；
8. memory proposal extraction；
9. optional headless/economic rollout budget；
10. finalization reserve；
11. generic no-progress detection；
12. compiler 4,096/16 MiB、32 sources、64 tools与continuity 320/640 MiB的长期数值。

这些议题必须由后续独立规格基于Round 5A“无固定call次数与turn总wall-clock cap”的语义讨论。尤其Round 5B compaction是唯一可以有意破坏旧provider prefix的产品owner；它不能反过来要求恢复24-call或turn-wide deadline。

---

## 19. 最终交付口径

Round 5A完成后，Pulsara可以准确声明：

> 在当前Host中，一个canonical user turn没有固定model/tool-call次数或turn总wall-clock cap；它持续推进，直到semantic completion、显式cancel/Host close、单次provider-input/resource bound或其他typed product boundary。每项planning、canonical I/O、transport、effect和close仍由自己的owner-scoped watchdog与physical owner约束。

此时仍不能声明：

> 一个turn已经可以跨多个model-visible context windows自动compact并继续。

后一句必须等待Round 5B。两者的区别必须在README、Gap Index、activation evidence与用户文案中保持清晰。
