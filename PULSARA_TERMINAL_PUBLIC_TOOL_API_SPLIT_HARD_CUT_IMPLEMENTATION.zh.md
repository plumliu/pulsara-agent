# Pulsara Terminal Public Tool API 拆分 Hard Cut 实施规格

> 状态：TAPI0-TAPI2已落地；不兼容hard cut，部署时强制durable-store reset
> 日期：2026-07-22
> 范围：重构 terminal public tool facade、唯一input schema owner、ToolResult semantics、权限/Long-Horizon分类和capability registry
> 不变项：managed process、output journal、monitor coordinator、monitor lifecycle event、Host ingress、notification reducer与recovery算法

## 1. 背景

当前代码不是一个一致的十一分支public API，而是已经发生schema drift：

* executable `TerminalProcessTool.parameters`与executor接受十一种action；
* 真正进入ProviderInput的builtin capability descriptor只公开八种process action；
* 因此模型通常看不到monitor action，但测试或内部caller仍可绕过descriptor直接执行；
* descriptor与executor分别手写schema，composition root没有证明两者相等。

漂移出的三个monitor action本身也不属于普通process I/O：

* `monitor`创建Host-owned scheduling authority，未来可能触发付费模型调用；
* `list_monitors`读取monitor lifecycle，而不是process lifecycle；
* `cancel_monitor`终结monitor registration，但不终止process；
* monitor使用`monitor_id`，其余process action主要使用`process_id`；
* monitor拥有独立的permission、wake-chain、notification account和recovery owner。

当前`terminal_process`也没有真正的strict action matrix：executor会在识别branch前解析所有action共用的`max_output_chars`，`bounded_int_arg()`会把越界值clamp，而不是拒绝。继续复用扁平schema会让大量字段只在少数action中有效，也会让模型混淆“取消monitor”和“kill process”。此外还有三个public schema口径问题：

1. `conditions`、`delivery`、`lifetime`只声明为普通object，没有公开nested fields与bounds；
2. heartbeat parser预检查为`1..3600`，typed DTO实际接受`5..1800`；
3. public monitor默认preview为`32000` chars，内部canonical default为`4000`，nested `max_output_chars`也缺少统一hard bound。

本次hard cut收回public API ownership并新增terminal-monitor专属ToolResult semantic schema，但不重做已经落地的monitor coordinator、Host ingress或notification reducer。

## 2. 最终工具边界

| Tool | 唯一职责 | Public actions |
|---|---|---|
| `terminal` | 启动命令；在yield window后返回仍在运行的`process_id` | 无action discriminator |
| `terminal_process` | 查看或控制已有managed process | `list/log/poll/wait/write/submit/close_stdin/kill` |
| `terminal_monitor` | 注册、查看或取消Host-owned monitor | `register/list/cancel` |

硬规则：

* `terminal_process`物理删除`monitor/list_monitors/cancel_monitor`；
* 不保留compat alias，不接受旧action后转发；
* 不保留旧result decoder或历史replay；部署必须执行durable-store reset；
* 不新增`terminal_monitor_register/list/cancel`三个独立工具；
* `wait`继续表示阻塞当前tool call；`terminal_monitor.register`表示持久、重复、非阻塞monitor；
* `terminal_monitor.cancel`只取消monitor，不kill process；
* 历史durable monitor event与monitor ID算法保持不变。

## 3. `terminal` API

`terminal`保持当前职责和参数：

```json
{
  "command": "uv run python train.py",
  "workdir": "/workspace",
  "terminal_session_id": "training",
  "yield_time_ms": 2000,
  "tty": true,
  "max_output_chars": 8000
}
```

若命令在`yield_time_ms`后仍在运行，返回`process_id`。`terminal`不得直接接受monitor policy或自动唤醒参数。

## 4. `terminal_process` API

最终action集合必须精确为：

```text
list
log
poll
wait
write
submit
close_stdin
kill
```

字段按action执行新的typed strict matrix：

| Action | Required | Relevant optional |
|---|---|---|
| `list` | 无 | `include_running`, `include_finished` |
| `log` | `process_id` | `max_output_chars` |
| `poll` | `process_id` | `max_output_chars` |
| `wait` | `process_id` | `timeout_seconds`, `max_output_chars` |
| `write` | `process_id`, `data` | 无 |
| `submit` | `process_id`, `data` | 无 |
| `close_stdin` | `process_id` | 无 |
| `kill` | `process_id` | 无 |

`terminal_process` schema与parser不得再出现`monitor_id`、`conditions`、`delivery`或`lifetime`。

实现必须定义完整discriminated union，而不是继续解析一个共享dict：

```python
TerminalProcessInput = Annotated[
    TerminalProcessListInput
    | TerminalProcessLogInput
    | TerminalProcessPollInput
    | TerminalProcessWaitInput
    | TerminalProcessWriteInput
    | TerminalProcessSubmitInput
    | TerminalProcessCloseStdinInput
    | TerminalProcessKillInput,
    Field(discriminator="action"),
]
```

每个branch使用`extra="forbid"`和strict scalar；`wait.timeout_seconds`、各类`max_output_chars`等越界输入返回typed malformed-arguments，不再clamp、回退default或在branch识别前提前解析。

## 5. `terminal_monitor` API

### 5.1 Register

```json
{
  "action": "register",
  "process_id": "process:abc123",
  "conditions": {
    "output": {
      "min_new_output_chars": 1000,
      "quiet_period_ms": 2000
    },
    "heartbeat_interval_seconds": 300
  },
  "delivery": {
    "max_output_chars": 4000,
    "minimum_progress_observation_interval_seconds": 30
  },
  "lifetime": {
    "maximum_duration_seconds": 36000
  }
}
```

Public bounds与defaults唯一冻结为：

| Field | Bound/default |
|---|---|
| `conditions.output.min_new_output_chars` | `1..65536`, default `200` |
| `conditions.output.quiet_period_ms` | `0..10000`, default `500` |
| `conditions.heartbeat_interval_seconds` | nullable；非null时`5..1800` |
| `delivery.max_output_chars` | `512..32000`, default `4000` |
| `delivery.minimum_progress_observation_interval_seconds` | `5..1800`, default `5` |
| `lifetime.maximum_duration_seconds` | `1..36000`, default `36000` |

`conditions.output`和heartbeat都可以省略；completion始终启用，因此空conditions表示completion/expiry-only monitor。

`lifetime.kind`不进入public schema。当前coordinator只消费maximum duration，`bounded`与`process_lifetime`没有不同算法；public factory固定构造durable `kind="process_lifetime"`。在两种kind拥有真实不同语义前，不允许暴露一个无效选择。

以下参数不属于public caller choice，由versioned resolved policy唯一提供：

```text
maximum_pending_progress_observations = 1
maximum_committed_progress_observations = 119
progress_observation_rate_window_seconds = 600
maximum_progress_observations_per_rate_window = 60
reserved_terminal_observations = 1
maximum_automatic_deliveries_per_chain = 12
```

三个policy owner必须保持分离：

* `TerminalProcessMonitorPolicyFact`只拥有conditions、delivery与lifetime；
* `ResolvedTerminalAutonomyChainPolicyFact`只拥有automatic delivery budget与interval；
* notification account只拥有active slot和terminal reserve。

Public factory只把caller input与固定progress limiter参数组合为`TerminalProcessMonitorPolicyFact`，不得把autonomy或account字段复制进去。现有durable DTO继续允许hydrate历史superset；新的strict bounds属于public input DTO和production factory，不通过收窄历史DTO validator改变既有schema fingerprint。

最终production policy明确采用`600s/60` sliding window。它与当前executor-only入口的`60s/12`默认、内部helper的`3600s/119`默认都不等价，这是本次hard cut有意消除的漂移，不得再声称所有legacy input都与新入口等价。

成功结果保持现有语义：

```json
{
  "status": "running",
  "terminal_monitor_action": "register",
  "process_id": "process:abc123",
  "monitor_id": "monitor:def456",
  "monitor_status": "registered",
  "expires_at_utc": "...",
  "output": "..."
}
```

Registration仍遵守既有原子边界：dormant prepare后，将monitor registration、notification reservation、ToolResult terminal projection、ToolResultEnd与settlement同批FULL；FULL后才activate并立即recheck。

### 5.2 List

```json
{
  "action": "list"
}
```

返回bounded current monitor inventory：

```json
{
  "status": "success",
  "terminal_monitor_action": "list",
  "monitors": [
    {
      "monitor_id": "monitor:def456",
      "process_id": "process:abc123",
      "lifecycle_state": "active_ready",
      "observation_ordinal": 2,
      "has_pending_observation": false
    }
  ]
}
```

`list`不接受`process_id`、`monitor_id`、conditions、delivery或lifetime。历史详情继续由Inspector拥有。

### 5.3 Cancel

```json
{
  "action": "cancel",
  "monitor_id": "monitor:def456"
}
```

返回：

```json
{
  "status": "success",
  "terminal_monitor_action": "cancel",
  "monitor_id": "monitor:def456",
  "monitor_status": "cancelled"
}
```

`monitor_status`只允许`cancelled | already_terminal`。Cancellation继续复用既有session-owned atomic owner；若存在FIRING candidate，不得以新cancel candidate覆盖它。

## 6. 唯一input schema owner与branch matrix

`TerminalProcessInput`与`TerminalMonitorInput`是两个public schema的唯一语义owner。Monitor parser生成互斥typed union：

```python
TerminalMonitorInput = Annotated[
    TerminalMonitorRegisterInput
    | TerminalMonitorListInput
    | TerminalMonitorCancelInput,
    Field(discriminator="action"),
]
```

唯一矩阵：

| Branch | Required | Forbidden |
|---|---|---|
| `register` | `process_id` | `monitor_id` |
| `list` | 无 | `process_id`, `monitor_id`, `conditions`, `delivery`, `lifetime` |
| `cancel` | `monitor_id` | `process_id`, `conditions`, `delivery`, `lifetime` |

所有nested object都必须`additionalProperties=false`。未知action、未知field、bool冒充integer、越界数字和不合法null都在tool adapter边界返回typed malformed-arguments result，不得把Pydantic异常泄漏为runtime failure。

新增中央binding，例如：

```python
class BuiltinToolInputContractBinding:
    tool_name: str
    input_adapter: TypeAdapter
    input_schema: FrozenJsonObjectFact
    input_schema_fingerprint: str
```

唯一factory从typed union生成、规范化并冻结schema。以下消费者必须读取同一个binding，不得复制dict：

* `BUILTIN_CAPABILITY_DESCRIPTORS[name].input_schema`；
* executable tool的`parameters`；
* tool executor的strict parser；
* capability/tool composition doctor；
* schema snapshot tests。

Composition root必须验证canonical `descriptor.input_schema == tool.parameters == binding.input_schema`且fingerprint相等，任一漂移阻止Host启动。

Public schema使用按`action`区分的`oneOf`，每个branch都是完整object schema并带`const action`、branch-specific required与`additionalProperties=false`。Schema exporter可以内联`$defs`以满足provider限制，但不得将oneOf重新降级为共享flat object。

必须对以下三条实际adapter路径做schema conformance测试：

* OpenAI Chat Completions tools；
* OpenAI Responses tools；
* DeepSeek OpenAI-compatible Chat tools。

测试至少证明schema被provider request serializer原样保留、三个action branch可表达、非法cross-branch fields无法通过本地strict adapter。Public schema必须完整声明nested properties、description、required、default、minimum和maximum；不能再使用裸`{"type":"object"}`代替领域schema。

## 7. Permission与ownership

权限与Long-Horizon必须同时按`tool_name + action`分类：

| Call | Permission | Long-Horizon action |
|---|---|---|
| `terminal_monitor.register` | scheduling mutation | `PROCESS_CONTROL` |
| `terminal_monitor.list` | read-only observation | `EVIDENCE_HYDRATION` |
| `terminal_monitor.cancel` | scheduling mutation | `PROCESS_CONTROL` |

V1继续fail closed：

* 只有committed Host main run可以register；
* process origin runtime必须等于Host main RuntimeSession；
* child caller、child-origin process和cross-ledger process拒绝；
* registration-time permission不代表未来automatic delivery仍获准；delivery每次重验当前permission revision；
* terminal access关闭时三个action均不可绕过现有policy。

必须新增`terminal_monitor_tool_action_policy()`与独立classifier contract，并注册到`default_tool_action_classifier_registry()`；否则新tool会落入默认`EXTERNAL_ACTION`并改变restricted/finalization行为。

所有tool-name taxonomy同时迁移：

* shared `TERMINAL_TOOL_NAMES`加入`terminal_monitor`，保证terminal-off、origin与recovery不漏拦；
* subagent capability集合明确排除`terminal_monitor`，V1 child不能通过verification/read-only profile取得`list`或绕到`register`；
* unfinished-call recovery在能hydrate exact action时将`list`归为`READ_ONLY`、`register/cancel`归为`TERMINAL`；action缺失、malformed或无法hydrate时保守归为`TERMINAL`；
* terminal artifact preview、trusted timing/freshness、tool origin与result renderer中所有`{"terminal", "terminal_process"}`集合按实际语义决定是否加入`terminal_monitor`，禁止依赖遗漏后的generic fallback；
* capability classifier、permission policy、tool descriptor与result semantic variant全部改绑新tool，不再通过旧`terminal_process` action识别monitor mutation。

## 8. `terminal_monitor`专属Result semantics

本阶段选择新增专属result contract，不把旧action保留为内部稳定operation code。需要新增或升级以下typed semantics：

```text
TerminalMonitorRegistrationDomainSubmissionFact
TerminalMonitorInventoryDomainSubmissionFact  # schema v2
TerminalMonitorCancellationDomainSubmissionFact  # schema v2
TerminalMonitorErrorDomainSubmissionFact

TerminalMonitorRegistrationEssentialFact
TerminalMonitorInventoryEssentialFact  # schema v2
TerminalMonitorCancellationEssentialFact  # schema v2
TerminalMonitorErrorEssentialFact
```

公开action literal只允许`register/list/cancel`。Registration不再借用`TerminalProcessObservationEssentialFact(action="monitor")`；其essential fact至少冻结：

```text
action = register
process_id
monitor_id
monitor_status = registered
expires_at_utc
current process status/exit code
output_truncated
terminal session/backend identity
```

Inventory action固定为`list`，cancellation action固定为`cancel`。Error fact保存requested action及适用的process/monitor identity，并冻结三branch nullability matrix。

新增`terminal_monitor_result_render_contract()`，拥有registration、inventory、cancellation、malformed/permission/adapter error variants；`result_render_contract_for_tool("terminal_monitor")`必须返回该contract，不能落入generic contract。原`terminal_process_result_render_contract()`删除monitor variants。

Essential-envelope renderer按owner输出：

```text
terminal_process fact -> terminal_process_action
terminal_monitor fact -> terminal_monitor_action
```

Registration正文被预算降级时仍必须渲染`terminal_monitor_action="register"`，不得重新出现`terminal_process_action="monitor"`。Full result、display payload、metadata、essential envelope和hydrated terminal projection的action必须逐项相等。

这会修改ToolResult semantic/terminal projection的durable schema与contract fingerprint，但不修改新世界中的monitor coordinator event vocabulary。旧v1 result facts、旧action literal及其decoder全部物理删除；不存在historical allowlist、dual decoder或兼容读取路径。

## 9. Durable与ProviderInput影响

本次不新增monitor lifecycle event，但会新增terminal-monitor ToolResult DTO/contract：

* `TerminalProcessMonitorRegisteredEvent`等现有event不变；
* `monitor_id`、observation ordinal、cursor和wake-chain算法不变；
* Host ingress、safe point、notification delivery与Inspector reducer不变；
* 所有`terminal_process(action=monitor/list_monitors/cancel_monitor)`历史ToolCall、旧result artifact与旧projection均不再可读；
* capability catalog/tool schema fingerprint会变化；reset后只允许以新catalog创建全新的ProviderInput generation；
* generation-scoped execution binding不能在同一HostSession中从旧descriptor热替换为新descriptor；
* active monitor、pending notification与后台process不跨cutover恢复。

本次hard cut强制重置旧durable world，不提供migration。至少重置PostgreSQL/EventLog、tool-result/terminal-projection artifact metadata、provider-input generation state和monitor/account state；任何引用旧runtime ledger的派生store也必须同步清空或按仓库统一reset流程重建。旧artifact blob可以物理删除或在reset后作为unreachable content执行GC，但不得被新runtime hydrate。

### 9.1 唯一cutover算法

V1只支持restart cutover，不支持live hot-swap：

1. 关闭新Host run、model dispatch和tool dispatch admission；
2. drain或typed terminalize全部active model streams、suspended runs、已提交但未执行的ToolCall、正在执行的旧tool call与prepared terminal/monitor owners；
3. 已按旧generation dispatch的call必须由旧binding完成或明确terminalize，不能交给新executor返回unsupported；
4. close旧HostSession，终结全部monitor并关闭/kill其owned process；cutover后不保留可恢复的live owner；
5. 只有确认不存在引用旧binding的physical owner后，停止旧进程并执行强制durable-store reset；
6. 新进程只安装新descriptor、schema binding、result contract、decoder registry与taxonomy；
7. 以新capability catalog和空durable state创建全新HostSession/ProviderInput generation。

禁止live cutover、双binding生命周期和旧数据lazy migration。需要保留的用户workspace文件不属于runtime DB reset范围，但必须与清空的run/session authority重新建立新世界关系。

## 10. 实施顺序

### TAPI0：Additive facade

* 新增`TerminalProcessInput`、`TerminalMonitorInput`与唯一schema binding/factory；
* 新增`TerminalMonitorTool`及专属result DTO、contract和renderer；
* 建立中央public bounds/defaults与fixed resolved progress policy；
* 复用现有`TerminalMonitorCoordinator`、monitor lifecycle candidates和atomic owner；
* 新tool只在测试composition root可见；该阶段不可部署到production。

Gate：两个typed union的branch/property tests全绿；descriptor/tool/binding schema逐字节相等；Chat Completions、Responses与DeepSeek schema conformance通过；new factory必须直接生成最终`600s/60` policy。该gate不与legacy helper比较，也不承诺legacy input equivalence。

### TAPI1：Atomic production switch

同一变更中：

* production registry加入`terminal_monitor`；
* `terminal_process`删除三个monitor action及字段；
* 按§9.1执行restart cutover；
* 在同一次cutover change中物理删除旧input/result DTO、decoder、factory、renderer branch、positive fixture与文档调用；
* capability classifier、permission、Long-Horizon policy、taxonomy、unfinished recovery、result semantics、artifact/timing helpers、tool descriptions和fixtures切到新tool；
* 同步`BUILTIN_TOOLS`、`PERMISSION_POLICY`、`CAPABILITY_SURFACE`、`RECOVERY`与Long-Horizon长期contract，以及monitor实施规格中的public调用示例；
* reset后以新capability catalog创建首个全新ProviderInput generation，不读取或rollover旧generation。

Gate：cutover前不存在旧physical owner；reset后旧ToolCall/ToolResult/monitor state不可hydrate；production tool catalog只出现三个最终工具边界；Long-Horizon三action分类矩阵与terminal-off/subagent/recovery矩阵通过；四类terminal-monitor result在full与essential降级下都保留新action；register/list/cancel Host dogfood全部通过；对旧action的negative call稳定返回typed malformed-arguments且绝不转发。

### TAPI2：Audit与architecture guard

* 审计`TerminalProcessTool`不存在monitor parser、result helper和imports；
* architecture guard扫描整个production source，禁止旧action构造、旧action literal与compat alias；
* Inspector与docs只展示`terminal_monitor` public owner。

Gate：terminal、terminal_process、terminal_monitor定向测试、provider schema conformance、clean-reset startup、全量offline pytest、Ruff与`git diff --check`全绿。

## 11. Architecture guards

至少冻结：

* `TerminalProcessTool._SUPPORTED_ACTIONS`不包含`monitor/list_monitors/cancel_monitor`；
* production不存在把旧action改写为`terminal_monitor`的shim；
* production不存在`terminal_monitor_register/list/cancel`独立tool alias；
* descriptor、executable tool与central binding的input schema/fingerprint必须相等；
* 两个public parser只能接受typed union，不得调用clamping `bounded_int_arg()`；
* `TerminalMonitorTool`不能直接写EventLog，只能产生现有prepared owner/candidates；
* register/cancel不能被分类为read-only；
* list不能创建或终结monitor；
* `builtin_tool_action_policy("terminal_monitor")`不能返回default `EXTERNAL_ACTION`；
* shared terminal taxonomy必须包含`terminal_monitor`，subagent production exposure必须排除它；
* `result_render_contract_for_tool("terminal_monitor")`不能返回generic contract；
* production source与decoder registry不得包含旧action literal或`terminal_process_action="monitor"`；仅architecture guard和明确验证拒绝行为的negative tests允许保存forbidden marker；
* public JSON Schema不得包含无properties的`conditions/delivery/lifetime`；
* heartbeat schema、parser与DTO都只接受`5..1800`；
* monitor output preview schema、parser与DTO都只接受`512..32000`，default为`4000`；
* caller不能设置内部progress cap、rate-window、terminal reserve或wake-chain budget。

## 12. Definition of Done

只有以下条件全部成立，本次public API hard cut才完成：

* 三个tool的职责与action集合等于§2；
* `terminal_process`不再暴露或接受monitor action；
* `terminal_monitor`拥有完整、自描述、bounded nested schema；
* terminal-process八branch与terminal-monitor三branch matrix分别由typed union唯一执行；
* capability descriptor与executable tool不存在第二份schema；
* public schema、parser、DTO与resolved policy不存在范围/default漂移；
* dedicated terminal-monitor result contract在full/essential/artifact路径只使用新action；
* register/cancel继续复用原子durable owner，既有monitor lifecycle算法不变；
* permission、Long-Horizon、taxonomy、subagent exclusion、unfinished recovery、Inspector和ProviderInput capability attribution全部改绑新tool；
* restart cutover证明没有跨generation旧binding owner；reset后以新tool catalog建立全新generation；
* 旧调用、旧result和旧monitor durable state均不可replay或恢复；
* architecture guard和TAPI0-TAPI2 gate全部通过。

最终产品语言应当简单：`terminal`启动任务，`terminal_process`操作任务，`terminal_monitor`管理任务的持续通知。工具名本身即表达ownership，descriptor、executor与result renderer也不再各自描述不同的public surface。

## 13. 实施验收记录

2026-07-22完成TAPI0-TAPI2代码落地：

* `TerminalProcessInput`八分支与`TerminalMonitorInput`三分支成为唯一public schema/parser owner；descriptor、tool与binding由composition guard逐字节校验；
* production registry只公开`terminal`、`terminal_process`与`terminal_monitor`三个最终边界，旧monitor action、result branch、renderer和兼容入口均已物理删除；
* monitor registration/inventory/cancellation/error使用专属full与essential result contract；permission、Long-Horizon、terminal taxonomy、subagent exclusion和recovery分类已改绑新tool；
* architecture guard覆盖旧action literal、schema drift、strict bounds、provider serializer与typed malformed negative calls；
* 全量offline验证结果为`2381 passed, 2 skipped`，Ruff与`git diff --check`通过。

该验收记录证明代码与clean-world测试闭合，不代替部署动作。任何仍保存旧ToolCall、旧ToolResult projection、旧provider-input generation或旧monitor/account state的环境，启动新版本前仍必须执行第9.1节规定的restart cutover与durable-store reset。
