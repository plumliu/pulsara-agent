# Round 9.2：独立 Hook Subsystem 实施规格

> 状态：**ACTIVATED**
>
> 本文已经逐项吸收[`PULSARA_ROUND_9_2_REVIEWER_FINDINGS.md`](PULSARA_ROUND_9_2_REVIEWER_FINDINGS.md)中的7项P1、6项P2与2项P3。Reviewer文档保留为修订前审阅记录；本文§17是逐项closure索引。2026-08-24复审确认的4项P1与2项P2也已按§15.1单一路径闭合，并由重新生成的真实provider/command轨迹与完整验证取代先前被撤回的activation结论；最终合入前review新增发现的post-adoption error降格与FIFO source阻塞也已闭合，重新运行retained full/PostgreSQL与静态验证，既有真实trace继续证明本次未改变的successful provider/Hook路径。
>
> 修订日期：2026-08-26（Round 9.3 Plugin adapter同步）
>
> 编码基线：lifecycle seam、owner、transaction与process-local carrier以当前production code为第一真源；已ACTIVATED Round 9、Round 9.1、Round 5B与Round 10用于解释仍由当前代码实现的产品不变量。上位文档中的旧拓扑、已删除DTO或被本文显式hard-cut的逐字段等价假设不得迫使实现恢复旧owner、dual path或补偿机制。
>
> Fingerprint约束：[`PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)是强制上位契约。完整frozen Hook values已经传到consumer时不得再添加DTO fingerprint。本轮只允许保留“用户审阅过的exact definition集合”与未来执行之间的trust digest；不得建立Hook registry fingerprint、`digest -> object` map或proof graph。
>
> 上位契约：[Round 3 structured compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 5B compaction](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)、[Round 7 observation](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7.1 ToolResult projection](ROUND_7_1_PROVIDER_VISIBLE_TOOL_RESULT_PROJECTION_IMPLEMENTATION_SPEC.zh.md)、[Round 9 capability](ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md)、[Round 10 subagent](ROUND_10_HIERARCHICAL_SUBAGENT_ORCHESTRATION_IMPLEMENTATION_SPEC.zh.md)、[Gap Index](archived_docs/POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 公开兼容基线：[Codex Hooks](https://learn.chatgpt.com/docs/hooks)。本地Codex checkout当前production实现仍只有不含`SessionEnd`的10项event，但公开契约已经列出11项；本文以公开11项为产品truth，并把本地差异当作上游版本差异，而不是缩窄Pulsara契约。
>
> 已激活下游消费者：[Round 9.3 Agent Plugin Bundle 与 Hook Adapter](ROUND_9_3_AGENT_PLUGIN_BUNDLE_AND_HOOK_ADAPTER_IMPLEMENTATION_SPEC.zh.md)。Round 9.3只成为本文的第三definition source，并复用本文的trust、matcher、dispatcher、executor、output parser、provider context owner与single future-view publication lane。

本文把Hook实现为**独立、source-agnostic、process-local的Runtime subsystem**。USER与exact WORKSPACE `hooks.json`是local paths；Round 9.3把enabled immutable package中的fixed `dev.pulsara/hooks/hooks.json`归一化成同一组definitions，复用全部执行机制。

---

## 0. 最终产品形状

### 0.1 三类当前source，一个运行时

```text
${PULSARA_HOME}/hooks.json             USER source
<workspace>/.pulsara/hooks.json       exact WORKSPACE source
<managed-plugin>/dev.pulsara/hooks/hooks.json  Plugin source
             │
             ├─ complete bounded discovery
             ├─ Codex-compatible JSON normalization
             ├─ exact definition trust
             └─ FrozenHookDefinitionView
                         │
                         ▼
                 KernelHookDispatcher
                   ├─ matcher
                   ├─ command executor
                   ├─ output parser / control aggregation
                   ├─ diagnostic adapter port ──> one process-local diagnostic adapter
                   └─ context port ────────────> one HookContextOwner ──> HOOK_CONTEXT
```

Round 9.3只增加第三个definition source：

```text
Plugin dev.pulsara/hooks/hooks.json
  -> PluginHookContributionAdapter
  -> same FrozenHookSourceProvenance + FrozenHookDefinition values
  -> same trust owner / dispatcher / executor / HOOK_CONTEXT
```

Production不存在Hook subclass、PluginHookDispatcher、managed policy engine或第二套event bus。

### 0.2 Authority

Hook执行外部command，但不拥有它所观察或控制的产品对象：

| 产品对象 | 最终authority | Hook可做什么 |
|---|---|---|
| user prompt ingress | existing queue/turn admission | block exact ingress；提供一次性context |
| Tool call | existing tool authorize/attempt/invoke owner | deny；提供context；不改写arguments |
| permission request | existing permission owner | allow/deny当前exact request；不改变permission mode |
| ToolResult | canonical ToolResult transaction + process-local settlement | 读取public projection；提供下一call feedback；不改写result |
| compaction | Round 5B lane/fence/summary/adoption owner | Pre阶段abort；Post阶段可停止active continuation；不注入PostCompact context |
| subagent | Round 10 task/result/coordinator | start context；stop continuation；不创建/复活task |
| final answer/session | existing ROOT run/Host close | stop continuation；SessionEnd observe-only |

Hook trust授权command以当前Host OS用户身份运行，不等于把Hook提升为SYSTEM、permission policy、canonical authority或sandboxed Tool。

### 0.3 Independent event vocabulary

```text
HookEventType
  SessionStartEvent
  SessionEndEvent
  UserPromptSubmitEvent
  PreToolUseEvent
  PermissionRequestEvent
  PostToolUseEvent
  PreCompactEvent
  PostCompactEvent
  SubagentStartEvent
  SubagentStopEvent
  StopEvent
```

内部名称统一带`Event`后缀；external JSON使用Codex名称。它们不复用`CommittedEventType`或`LiveEventType`：Hook event只是当前Runtime准备调用外部command的lifecycle input，不是durable occurrence或UI stream。

### 0.4 Closed effect algebra

Hook core只向业务owner返回五种closed outcome family；diagnostics是每种family的正交字段，不是第六种authority：

```text
ObserveOutcome
  diagnostics

ContextOutcome
  ordered context entries
  diagnostics

GateOutcome
  PROCEED | BLOCK
  optional reason
  ordered context entries
  diagnostics

PermissionOutcome
  ABSTAIN | ALLOW | DENY
  optional reason
  diagnostics

ContinuationOutcome
  TERMINALIZE | CONTINUE_ONCE
  optional reason
  diagnostics
```

Event到family的映射固定为：

| Family | Events | neutral/default |
|---|---|---|
| `ObserveOutcome` | `SessionEndEvent` | diagnostics only |
| `ContextOutcome` | `PostToolUseEvent`、`SubagentStartEvent` | empty context |
| `GateOutcome` | `SessionStartEvent`、`UserPromptSubmitEvent`、`PreToolUseEvent`、`PreCompactEvent`、`PostCompactEvent` | `PROCEED` |
| `PermissionOutcome` | `PermissionRequestEvent` | `ABSTAIN` |
| `ContinuationOutcome` | `SubagentStopEvent`、`StopEvent` | `TERMINALIZE` |

Permission、gate与continuation分别影响human approval、当前operation admission和既有run的terminalization，不能合并成generic `decision`。Raw exit code、stdout/stderr、external field name与event string永远不能离开Hook core；existing owner只消费typed family，并在重验自己仍拥有authority后应用合法效果。

Event到family的映射不因active/idle/terminal分支改变。某分支没有合法effect或future model consumer时，existing owner仍收到该event固定family，但只保留diagnostics并discard control/context；不得把它动态重包成`ObserveOutcome`或让dispatcher查询业务状态。

### 0.5 Prefix continuity

同一continuity epoch：

```text
SYSTEM[n + 1]   == SYSTEM[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
```

因此：

- `reload_hooks`不改写SYSTEM或provider `tools[]`；
- Hook informational output只形成`HOOK_CONTEXT` user-role suffix；
- Hook config变化只影响future lifecycle event；
- 已dispatch event继续持有旧immutable definition直到settle；
- cold open与Round 5B compaction successor仍是仅有的合法root rebase边界；
- Hook自身不创造第三种rebase。

Compaction active successor的Hook context在**同一个既有successor boundary**内冻结。Current `CompactionCoordinator`在canonical adoption前构造的uninstalled dry dispatch继续只证明**无Hook context的cold base successor**可行；它不签发continuity permit、不安装ToolResult delivery，也不physical open。Canonical adoption FULL后运行`PostCompactEvent`；仅ROOT active successor再运行`SessionStartEvent(source=compact)`。随后existing compiler/continuity owner先重验post-adoption exact canonical read、tool surface与frozen non-Hook source facts，再从这些同源facts构造no-Hook base与可选Hook sibling，选择一个final installable cold-root request。Hook sibling必须保持base sibling的`SYSTEM/tools`逐字节相同，唯一新增semantic source是one-shot `HOOK_CONTEXT`；其user-role observation按normal compiler placement `68`参与这次cold assembly。它不要求pre-write dry messages成为final messages的prefix——dry从未installed，compaction successor本来就是批准的root rebuild boundary。真正的same-epoch prefix合同从final candidate安装后开始。

这是一项以当前代码owner为准的Round 9.2 hard cut：任何上位文字若要求adoption后的actual successor与pre-adoption dry request在全部messages/source heads/wire/candidate上逐项相等，均由“dry证明no-Hook cold base；post-adoption同源facts上final重组；可选Hook是唯一新增source”取代。Hook sibling不fit、invalid或组装失败时必须omit它并选择同一post-adoption facts上的valid no-Hook sibling；advisory context不能独自使已FULL adoption不可继续。任一路径至多一次continuity CAS/install/permit/execution与physical open。不得伪造uninstalled predecessor、先安装base后原地改写/revoke/supersede、执行第二次CAS或把Hook解释成第三种root rebase。

### 0.6 Durability与oracle

本轮唯一跨进程状态是Hook source trust/enablement配置。它是local user configuration，不是conversation recovery。

本轮不新增：

- PostgreSQL relation、column、migration；
- committed/live event、subject、append guard；
- durable job、receipt、checkpoint、reducer、replay、repair graph；
- Hook execution history、exactly-once promise或cross-Host output delivery；
- Hook registry generation或projection authority。

Round 9.2把architecture oracle从六维显式扩展为七维。前六维保持不变；第七维单独记录本轮新增的closed Hook lifecycle vocabulary：

```text
Committed events   29
Live events        24
Subject slots      11
Append guards       1
Product relations  25
Durable jobs        0
Hook event types   11
```

固定顺序简写为`29 / 24 / 11 / 1 / 25 / 0 / 11`，最后一项始终表示`HookEventType`数量。`HookEventType`不是durable occurrence或UI Live stream，因此不得并入前两项；但它是Runtime可观察、Hook作者可配置、dispatcher必须穷尽处理的独立closed vocabulary，因此必须与前六项平级进入oracle，不能作为“免费枚举”隐藏在正文中。

---

## 1. 范围与非目标

### 1.1 本轮实现

1. USER与exact WORKSPACE `hooks.json` discovery；
2. Codex-compatible command Hook JSON shape；
3. 11项`HookEventType`；
4. exact definition inspection/trust/enable/disable；
5. explicit cold/reload discovery；
6. matcher、command process、timeout、output parser与control aggregation；
7. ROOT/tool/permission/compaction/subagent lifecycle seams；
8. source-neutral `HOOK_CONTEXT`；
9. fixed ROOT-only `reload_hooks` Builtin；
10.真实Hook + provider + Tool + compaction + subagent dogfood。

### 1.2 明确不实现

- Plugin package parsing、installation、MCP/Skill authority；这些只属于Round 9.3 package/component owners；
- Codex inline `config.toml [hooks]`；Pulsara本轮只有一个JSON配置truth；
- system、enterprise、MDM、managed requirements或`allow_managed_hooks_only`；
- filesystem watcher、自动热重载；
- `prompt`、`agent`、`http`、`mcp_tool` handler；
- argument rewrite、ToolResult rewrite、output suppression；
- Plugin/用户提供Python模块并dynamic import进Host；
- Hook marketplace、remote download、signed publisher；
- Hook UI；CLI inspection是本轮review/trust入口；
- durable Hook log、receipt、retry queue、cross-restart continuation；
- arbitrary total Hook count、session Hook count或Hook-history cap。

### 1.3 Hard-cut禁止结构

- `PluginHookDispatcher`；
- `PluginHookActivationContextOwner`；
- `PLUGIN_HOOK_CONTEXT`；
- 每source一套executor/parser；
- Hook registry fingerprint与`fingerprint -> definition` map；
- Hook output写入canonical transcript row或event log；
- Hook failure变成conversation failure authority；
- 为Plugin或compaction建立private Hook engine。
- 复用或扩展`conversation_kernel/extensions.py`中的`OperationalHookType`、`KernelExtensionHost`或extension-plane delivery；现有operational observation与本轮用户lifecycle command Hook必须双向隔离。

---

## 2. Source discovery、scope与配置

### 2.1 Current physical sources

```text
USER
  config = ${PULSARA_HOME}/hooks.json
  visible = all ROOT/child scopes in current user domain

WORKSPACE
  config = <canonical project root>/.pulsara/hooks.json
  visible = exact workspace ROOT/child scopes only
```

Workspace identity必须复用现有canonical project-root owner，不从cwd字符串另算第二套identity。Transient session只读取USER source；project session读取USER + exact WORKSPACE。

Config不存在表示`COMPLETE + empty`，不是UNAVAILABLE。Symlink、非regular file、路径escape、invalid UTF-8、overbound或读取identity race使exact source `UNAVAILABLE`；其他source仍可独立成立。Hook是optional subsystem，source UNAVAILABLE只产生diagnostic并让该source future handlers不可运行，不阻断conversation。

### 2.2 JSON shape

一个source只接受一个root object：

```json
{
  "description": "optional",
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|apply_patch",
        "hooks": [
          {
            "type": "command",
            "command": "python3 scripts/check.py",
            "commandWindows": "py -3 scripts\\check.py",
            "timeout": 30,
            "statusMessage": "Checking tool call",
            "additionalContextLimit": 2500,
            "async": false
          }
        ]
      }
    ]
  }
}
```

规则：

Pulsara冻结一个明确的Codex compatibility profile，而不是按“尽量解析”猜测未来字段：

| JSON位置/字段 | disposition | exact recovery unit |
|---|---|---|
| root `description:string` | `SUPPORTED` metadata | source |
| root `hooks:object`；missing表示empty | `SUPPORTED` | source |
| unknown root field | `KNOWN_IGNORED + diagnostic` | source仍可COMPLETE |
| 11个公开event key | `SUPPORTED` | exact event array |
| unknown event key | `UNSUPPORTED` | exact unknown event entry unavailable；sibling events保留 |
| group `matcher:string`、`hooks:array` | `SUPPORTED` | exact group |
| unknown group field、invalid matcher、invalid hooks shape | `UNSUPPORTED` | exact group unavailable |
| handler `type:"command"` | `SUPPORTED` | exact handler |
| `command`、`commandWindows`、`timeout`、`statusMessage`、`additionalContextLimit`、`async` | `SUPPORTED` | exact handler |
| `type:"prompt"|"agent"` | `KNOWN_UNSUPPORTED` | exact handler skipped并诊断 |
| `type:"http"|"mcp_tool"`或其他type | `UNSUPPORTED` | exact handler unavailable |
| unknown handler field、invalid command/timeout/async/context-limit type | `UNSUPPORTED` | exact handler unavailable；不得把`timout`等typo静默变成default |

一个文件合法但没有runnable handlers仍是`COMPLETE + empty runnable set + diagnostics`。一个invalid sibling绝不能废掉其他合法source/event/group/handler；但最窄invalid unit中的任何字段都不得产生partial runnable definition。

`type:"command"`必须有nonempty `command:string`；`commandWindows`只是Windows上的optional nonempty override，不能单独让缺失`command`的handler变合法。Windows有override时选择它，否则选择`command`；其他平台只选择`command`，但两者都纳入normalized definition与trust digest。`timeout`必须是JSON integer seconds；ordinary omitted值为600，合法范围`1..600`。`SessionEnd` omitted值为1，合法范围`1..3`；越界使exact handler unavailable，不clamp出一个用户未review的definition。`async`必须是boolean，omitted为false；SessionEnd即使声明true也以`KNOWN_IGNORED + diagnostic`强制sync，不能越过terminal close lane。

`additionalContextLimit`必须是nonnegative JSON integer，只约束**单个handler的`additionalContext` admission**：

- omitted：默认约2,500 tokens；
- positive integer：使用该approximate-token threshold；若完整context超过threshold，整项context omitted并产生diagnostic，不做head/tail截断；
- `0`：关闭这个per-handler threshold，但仍受1 MiB capture、UTF-8/parser与existing compiler source/aggregate bounds；
- event不接受`additionalContext`时该field为`KNOWN_IGNORED + diagnostic`；
- control字段与context正文分别解析；context over-limit不能抹掉同一handler中其他本来合法的control。

Executor/output parser只保存完整bounded正文及该threshold，不实现第二套tokenizer。是否超过threshold必须在目标provider safe point由existing model-input compiler estimator计算；因此同一正文以目标provider的真实估算为准。超限只省略该context contribution并诊断，不改变已经聚合的合法control。

Codex会把超大Hook output spill到temporary file；Pulsara本轮明确不建立Hook artifact/read path，因此采用上面的whole-entry omission。这是有意、可诊断的payload兼容差异，不得声称支持Codex spill语义。

`statusMessage`是attempt的process-local log/status label，只进入§6.7 diagnostic adapter；不进入SYSTEM、`HOOK_CONTEXT`、canonical row或control authority。

### 2.3 Parser physical bounds

这些是单个不可信配置carrier的parser/RSS边界，不是Hook总量或session寿命上限：

```text
maximum one hooks.json UTF-8 bytes       1 MiB
maximum JSON nodes                      16,384
maximum JSON depth                          64
maximum one scalar UTF-8 bytes            64 KiB
maximum filesystem path UTF-8 bytes        4 KiB
maximum command UTF-8 bytes                8 KiB
maximum matcher UTF-8 bytes                1 KiB
maximum status message UTF-8 bytes          4 KiB
```

Parser禁用duplicate object keys、NaN/Infinity、non-string map keys、aliases或任何非JSON extension。不得静默截断command/matcher后继续执行。

### 2.4 Complete observation cut

每个source的一次scan：

1. resolve exact expected path；
2. 用no-follow/reparse-point-safe的descriptor open，`fstat`并拒绝symlink/reparse/non-regular与escape；不能先`lstat`后用会跟随新symlink的普通open；
3. 从同一descriptor bounded读取exact bytes；
4. 再次`fstat`并对expected path做no-follow identity observation，验证`device/inode/size/mtime_ns/ctime_ns`全部未变且path仍指向该descriptor；同inode、同尺寸的in-place rewrite也必须使本次observation失败；
5. parse/normalize完整definition set；
6. 读取generic trust state并exact join；
7. freeze immutable source value。

读取中出现replace/delete/identity/mtime/ctime变化，本次source为`DISCOVERY_RACED/UNAVAILABLE`，不得无限重试或拼接partial definitions。Final path component必须以no-follow、nonblocking方式打开，再由`fstat`确认regular file；FIFO/socket/device等不得在regular-file检查前阻塞worker。全过程共享当前Host open/reload absolute deadline；deadline在任何stage耗尽都只使exact source `UNAVAILABLE`，不能把optional Hook source升级为Host-open failure。

Cold Host必须先冻结一次Host-open absolute deadline，再把完整source scan放到worker thread，得到一个exact immutable view后注入dispatcher；Host event loop不得同步执行filesystem read/parse/trust join，session constructor也不得第二次discover。Reload复用同一provider但形成future view，仍继承reload owner的existing absolute deadline。

### 2.5 Ordering与merge

Current source order：

```text
USER
WORKSPACE
USER Plugin sources by plugin_id
WORKSPACE Plugin sources by plugin_id
```

两层是append，不是后者覆盖前者。一个exact declaration只属于一个source；即使两层command/matcher文本完全相同也分别运行，CLI必须显示duplicate-looking definitions及provenance，不能做语义dedup。

Definition order为：

```text
source ordinal
event ordinal
group ordinal
handler ordinal
```

每个source内部签发total `source_local_definition_ordinal`；merged view中的deterministic order key是`(source_ordinal, source_local_definition_ordinal)`。它只决定admission、synchronous aggregation与同一safe-point的context rendering；不承诺OS process实际start或finish顺序。Deny/block仍按§7的closed aggregation，不因source顺序被allow覆盖。

`HookSourceKind.PLUGIN`与closed `PluginHookSourceIdentity`已经由Round 9.3 hard cut加入；visibility仍是USER或exact WORKSPACE，identity exact包含plugin id、package install id、fixed config relative path与workspace key（iff WORKSPACE）。Adapter把complete normalized values交给同一个factory；Hook core不扫描Plugin store，也不存在dormant/old enum branch。

---

## 3. Pure contracts与trust

### 3.1 Values

```text
HookSourceKind
  USER_FILE
  WORKSPACE_FILE
  PLUGIN

HookSourceIdentity
  source kind
  exact canonical source path
  visibility scope = USER | exact WORKSPACE

HookTrustSubject
  stable logical source locator
  USER_FILE      -> fixed USER subject
  WORKSPACE_FILE -> existing canonical workspace state key

FrozenHookSourceProvenance
  exact HookSourceIdentity
  optional description metadata
  display label
  closed source-stable declaration environment overlay

FrozenHookDefinition
  one immutable FrozenHookSourceProvenance reference
  source-local total definition ordinal
  event type
  matcher fact
  exact command / commandWindows
  timeout
  async disposition
  status message
  additional-context limit

FrozenHookSourceSnapshot
  exact FrozenHookSourceProvenance reference
  COMPLETE | UNAVAILABLE
  ordered complete definitions
  diagnostics

FrozenHookDefinitionView
  ordered source snapshots

HookDispatchScopeRef（private、owner-issued、not serialized）
  exact process-local Host-session owner ref
  exact workspace-state ref | transient-session ref
  ROOT | exact child task ref

HookDispatchEnvelope[T]（private、owner-issued）
  exact FrozenHookDefinitionView object reference（private；not serialized）
  exact HookDispatchScopeRef
  one of eleven typed public input variants T
  one closed non-capability HookDispatchCausalRef variant
  inherited absolute deadline + cancellation signal
```

`FrozenHookDefinitionView`是每个Host/workspace的一份current immutable view，不按ROOT或每个child复制。Dispatcher在event admission时把当时current exact object reference直接装入private envelope，再按definition中的visibility与`HookDispatchScopeRef`筛选；normal event使用该object。`reload_hooks` tool owner必须在其PreToolUse前capture predecessor object ref，并让同一prepared invocation的Pre/Permission/Post都显式复用该ref，即使invoke中已经publish new view；Post admission或tool owner abandon后只需drop该ordinary object reference。Background attempt已经持有immutable definition reference，不再保留whole view。Old view在publication replacement且所有ordinary object/definition refs自然释放后回收。不得为此创建view lease DTO、manual ref-count/release protocol、dispatcher-side registry、generation或fingerprint。

Flattened runnable index与aggregate diagnostics只能是该view的private disposable projection，不能作为第二份authority字段。Scope ref只表达所有event都具备的Host/workspace/ROOT-child union；direct/queued candidate、turn、close、tool、adoption等event-specific identity使用下面closed union，不添加optional run/turn mega field：

```text
HookDispatchCausalRef =
  SessionStartRef(SessionStartAttemptToken, source)
  | SessionEndRef(exact HostCloseAttemptToken)
  | DirectPromptRef(command_id, future turn/entry/revision ids)
  | QueuedPromptRef(command_id, queue_item_id, future turn/entry/revision ids)
  | PreToolRef(turn_id, assistant_entry_id, tool_call_id, exact resolved tool/route identity)
  | PermissionRef(turn_id, tool_call_id, PreparedPermissionRequest nonce)
  | PostToolRef(turn_id, tool_call_id, result entry/id, result state, epoch nonce)
  | PreCompactRef(CompactionAttemptToken, target scope/turn, trigger)
  | PostCompactRef(CompactionAttemptToken, adopted snapshot/binding revision)
  | SubagentStartRef(task-start event id, task id, child turn id)
  | SubagentStopRef(task id, EXPLICIT result-entry | INFERRED assistant-entry)
  | StopRef(ROOT turn id, assistant entry id, epoch nonce/revision)
```

这些refs只能由对应existing owner从当前carrier投影，既不授权operation也不延长capability lifetime。Dispatch可在调用栈内短借完整`PreparedProviderDispatch`、`PreparedAssistantMessageSettlement`、surface borrow或writer guard来构造public input并消费outcome，但envelope、attempt、background buffer与context contribution都不得保存这些owner DTO、borrow/permit capability、canonical content/blocks、private replay或raw result body。

完整值传递到consumer，不增加snapshot/view/definition fingerprints。Private constructor与owner-issued object identity保证当前进程真实性；source scope与path不能由generic caller自报。Provenance先构造，snapshot与definitions都引用它，因此不存在snapshot↔definition frozen object cycle。

Provenance只沿一条owner-issued reference传递：definition引用provenance；attempt引用definition；outcome/context只携带用户可见source label与其真实delivery scope，不再复制第三份path/scope/ordinal作为proof。Source visibility由identity拥有，current dispatch/context delivery scope由同一个`HookDispatchScopeRef`拥有，不能用重复字段或fingerprint互相“证明”。Envelope不是wire mega DTO：serializer只看其中的typed public variant；dispatcher/context owner只看opaque scope、causal carrier、deadline与cancellation signal，不取得Host、repository或ToolRuntime。它不使用digest、registry或可由generic caller自报的strings证明authority。

Current USER/WORKSPACE JSON没有environment字段，因此其`source-stable declaration environment overlay`固定为空。Plugin adapter只提交trust-covered `PLUGIN_ROOT`与`PLUGIN_DATA` exact public overlay；该slot不接收current Host/workspace派生值。`PULSARA_HOOK_SOURCE_DIR`与`PULSARA_PROJECT_DIR`属于§6.1 attempt-time dispatch environment。

### 3.2 Generic trust state

未managed的command Hook只有在用户审阅并trust exact normalized source definition set后才可执行。Trust state由generic Hook subsystem拥有，并按稳定`HookTrustSubject`而不是本次versioned source identity分片：

```text
${PULSARA_HOME}/hooks/trust/user.json
${PULSARA_HOME}/hooks/trust/workspace/<existing-workspace-state-key>.json
${PULSARA_HOME}/hooks/locks/user.lock
${PULSARA_HOME}/hooks/locks/workspace/<existing-workspace-state-key>.lock
```

```text
HookSourceTrustState
  trust subject
  enabled: bool
  trusted_definition_digest: optional digest
  trusted_at
```

`trusted_at`的唯一consumer是`list/inspect/doctor`，它们必须显示用户上次确认时间；它不进入definition digest、不参与execution eligibility、ordering或exact join。

`trusted_definition_digest`使用domain-versioned canonical framing覆盖：

- exact current source identity、scope/path；
- ordered normalized event/group/handler definitions；
- matcher、command/commandWindows、timeout、async、status、context limit；
- closed source-declared environment overlay中的public key/value；
- contract version。

这是用户review与未来跨进程command execution之间的真实boundary，可以持久化。Stable trust subject只用于找到一条状态，不授权任何version；digest才exact覆盖当前source identity与definitions。Digest不保存或索引definition object；runtime每次重新读取current definitions并比较。文件文本只做空白/JSON key order变化、但normalized definition完全相同时digest保持不变；任何执行语义变化形成`MODIFIED/UNTRUSTED`。

### 3.3 CLI与状态

```text
pulsara hooks list [--scope user|workspace]
pulsara hooks inspect --scope ...
pulsara hooks trust --scope ... --expected-definition-digest <digest>
pulsara hooks revoke --scope ...
pulsara hooks enable --scope ...
pulsara hooks disable --scope ...
pulsara hooks doctor [--scope ...]
```

`list/inspect`必须显示source `description`与`trusted_at`；`inspect`还必须完整显示current normalized event、matcher、command、timeout、async/context设置、source path与definition digest。`trust`在写前重新读取current source并比较exact expected digest；禁止`trust latest`或按path盲信。

Initial state为enabled但UNTRUSTED：source可被list/inspect，但command不会运行。Disable只影响future event；已dispatchattempt持有old definition直到settle。Trust/revoke/enable/disable命令使用per-source local lock、read-modify-atomic-replace；不建立全局Hook transaction或durable execution lease。

### 3.4 Project trust边界

WORKSPACE `hooks.json`即便在版本库中存在也不能自动执行。Exact Hook trust是本轮唯一project execution trust gate；它与Tool permission mode是不同边界：

- Hook trust授权配置中的external command；
- Tool permission授权模型发起的Tool operation；
- Hook的`PermissionRequest allow`只批准当前exact request；
- Hook不能改变后续permission snapshot或bypass scope/effect/liveness gate。

这里的“exact”覆盖用户inspection中看到的完整normalized Hook declaration：event、matcher、command/commandWindows、timeout、async、status/context设置与source-declared environment overlay。Host-derived `cwd`、`PULSARA_PROJECT_DIR`、未来`CLAUDE_PROJECT_DIR`等dispatch environment不进入definition digest；否则同一USER source会因workspace变化不断变成MODIFIED。它们由current Host/workspace owner在每次dispatch冻结，CLI必须显示这是稳定command declaration在不同合法workspace下的运行环境。该trust**不是**transitive executable attestation。Command可以引用之后被修改的workspace script、解释器、PATH executable、网络资源或shell expansion；trust意味着用户持续授权该exact command declaration以当前Host OS用户身份运行，直到revoke/definition变化，而不是Runtime承诺其依赖bytes永远不变。

本轮不得递归hash script/import/tree、冻结PATH executable、复制Hook代码进managed store或建立dependency graph来制造虚假的“代码完整性”。需要immutable package code时由Round 9.3 managed Plugin install id提供清晰边界；ordinary custom Hook保持Codex式command trust。

---

## 4. Reload与Host lifecycle

### 4.1 Cold discovery

每个Host cold composition完整读取current USER/exact WORKSPACE sources，形成一个`KernelHookDispatcher` current view。Composition冻结Host-open existing absolute deadline，在worker thread中只discover一次，再把该exact immutable view注入Host/dispatcher；不得在event loop或session constructor中做同步第二次scan。Cold source UNAVAILABLE不阻断Host；对应handlers不执行。

### 4.2 Explicit reload

固定Builtin descriptor：

```text
reload_hooks()
```

它从cold Host起就在ROOT provider `tools[]`中，不能因当前文件有无Hook而热增删。语义：

- ROOT-only；
- 仅`BYPASS_PERMISSIONS`允许invoke；其他mode在local authorize、attempt前typed拒绝；
- 不写配置、不trust、不enable；
- 只重新读取current source/trust state并替换future-event view；
- 返回per-source COMPLETE/UNAVAILABLE/TRUSTED/UNTRUSTED/MODIFIED dispositions；
- 不修改SYSTEM/tools或历史messages。

External host/UI未来可直接调用同一个`KernelHostCore.reload_hooks()`；不存在filesystem watcher。CLI修改文件/trust后必须明确提示running Host需要reload或restart。

### 4.3 Linearization

Reload：

1. 不持Hook publication mutex或Host lock，capture one observed predecessor并按一个absolute deadline读取/normalize本次所需sources与trust joins，build complete candidate；
2. 取得process-local Hook future-view publication mutex；若current view已不是scan所基于的observed predecessor，释放mutex并在同一caller-frozen deadline内重新build，不允许stale overwrite；
3. 在publication mutex内从publication-time current predecessor保留未重建的exact slices、合并本writer完整replacement，并按固定锁序`Hook publication -> Host`短暂取得Host lock，验证Host仍open、target scope current且dispatcher current view仍是exact predecessor object；
4. atomic publishmerged future definition view，然后依次释放Host lock与publication mutex；
5. old attempts继续持有predecessor values并settle；
6. 无old consumer后释放old view。

`reload_hooks`自身的Pre/Permission/Post事件全部使用attempt开始时、PreToolUse前capture并装入prepared invocation的§3.1 predecessor exact view object reference；new view从下一lifecycle event生效，不能在自己的PostToolUse自触发。PostTool dispatch admission或tool owner abandon后drop该ref；它不是“按旧view id重新lookup”的registry。

Missing config是完整empty replacement，会撤销future handlers。Malformed/unreadable/raced source在新view中为UNAVAILABLE并撤销该source future runnable handlers；不得为了“可用性”继续执行current filesystem已经无法证明的old external command。Other complete source继续运行。

这条publication mutex是dispatcher future view的唯一writer。Round 9.3 `reload_plugins`与`reload_hooks`排队进入同一mutex；二者不能假设操作disjoint slices。所有filesystem/package scan与candidate build都在mutex和Host lock之外；每个writer只在commit lane读取publication-time current predecessor，保留未重建的exact slices并完整替换自己负责的source values。Local reload若发现predecessor变化便在同一deadline重建；Plugin writer把already-complete Plugin slice与lock内current local slice合并。Host close/scope replacement导致验证失败时discard unpublished candidate。不得为此引入retry generation、registry seal、view fingerprint或全局Hook transaction。

Host close只有一条顺序，不能把ordinary Hook lane提前fence：

```text
stop external/business admission
+ freeze one Host close absolute deadline
→ cancel/quiesce every already-started ordinary lifecycle producer
  （这些producer持有的owner token仍可进入ordinary Hook lane并settle）
→ fence ordinary Hook admission + freeze current Hook view
→ terminal lane同步运行唯一SessionEndEvent（observe-only）
→ cancel background attempts
→ terminate/kill/drain every Hook process group
→ close dispatcher
→ remaining repository / canonical close / live / io physical close
```

“ordinary producer quiesced”至少包括prompt delivery/admission、ROOT runner、tool/result settlement、permission interaction、compaction、subagent completion与其他能产生11项event的owner；不能只等待provider task。SessionEnd不受ordinary fence误挡，使用fence时冻结的view，且不再生成`HOOK_CONTEXT`。一旦SessionEnd开始，任何late ordinary event均为owner bug并typed拒绝；一旦physical Hook drain开始，不接受terminal或ordinary event。

Close只在Host lock内原子设置closing/admission state与deadline，随后立即释放；等待producer、publication mutex、Hook attempt或process drain时都不得持Host lock。Publisher固定先持publication mutex、最后短暂取得Host lock；close不会以相反锁序同时持两者，因此不存在reload↔close ABBA deadlock。Close取得publication mutex只用于fence/freeze current view，不重新scan或publish generation。

SessionEnd和所有close-triggered process abort都继承同一个Host close deadline。Handler有效deadline与physical grace不得越过remaining close deadline；close/cancel/stale owner状态优先于任何Hook output，fail-open不能复活已关闭operation。

---

## 5. Matcher与Tool identity

### 5.1 Matcher

- missing、empty或exact `*`：match all；
- 其他所有matcher都使用frozen、case-sensitive、RE2-compatible linear-time regex dialect；语义是**search**，因此`Bash`可match`BashOutput`，作者需要exact match时必须写`^Bash$`；`Edit|Write`是普通regex alternation，不存在另一套exact-alternative parser；
- invalid syntax、lookaround、backreference、递归或其他非linear-time construct使exact matcher group unavailable；不得fallback到Python backtracking `re`；
- `UserPromptSubmit`与`Stop`配置中的matcher按Codex profile忽略并产生inspection diagnostic，不能偷偷过滤event。

每次event先冻结一个canonical matcher subject及closed aliases，再让同一matcher实现逐一测试；generic matcher不读取Tool registry、Plugin store或mutable owner state。
一个definition只要任一subject/alias匹配就被select**一次**；同一regex同时命中canonical name与多个aliases不能重复spawn或重复聚合。

Matcher subject是closed contract：

| Event | subject |
|---|---|
| SessionStart | exact `source` |
| SessionEnd | exact `reason` |
| UserPromptSubmit | ignored；等价match-all并产生inspection diagnostic |
| PreToolUse / PermissionRequest / PostToolUse | §5.2 frozen canonical tool identity及closed aliases |
| PreCompact / PostCompact | exact `trigger` |
| SubagentStart / SubagentStop | exact public `agent_type` |
| Stop | ignored；等价match-all并产生inspection diagnostic |

Call site不得另选展示label、outer meta name、tool call id、turn id或free-form reason作为matcher subject。

### 5.2 Tool aliases

```text
terminal                  -> {terminal, Bash}; external tool_name=Bash
terminal_process          -> {terminal_process}; external primary=terminal_process
terminal_monitor          -> {terminal_monitor}; external primary=terminal_monitor
edit_file                 -> {edit_file, apply_patch, Edit}; external primary=apply_patch；pulsara_tool_name=edit_file
write_file                -> {write_file, apply_patch, Write}; external primary=apply_patch；pulsara_tool_name=write_file
spawn_agent               -> {spawn_agent, Agent}; external primary=spawn_agent
create_agent_tasks        -> {create_agent_tasks}; external primary=create_agent_tasks
MCP                       -> exact resolved provider-qualified remote identity
other builtin             -> exact Pulsara descriptor name
```

`apply_patch`不是current Pulsara executable descriptor；上面两行是Codex matcher/input-name compatibility projection，不能保留一条无producer的canonical `apply_patch` row。`^apply_patch$`会匹配两种filesystem mutation，`Edit`只匹配`edit_file`，`Write`只匹配`write_file`。Event stdin按§9.1保留required `pulsara_tool_name`来消歧。`tool_input`仍是exact Pulsara `edit_file`/`write_file` arguments，不能伪造Codex `tool_input.command`；CLI doctor必须显示这项schema差异。Alias只影响matcher与兼容primary name，不改写arguments。公开`Agent` alias只属于`spawn_agent`，不得让batch-only `create_agent_tasks`借它重复匹配。

Codex unified-exec的later `write_stdin`是existing Bash invocation的transport，因此不重新产生Pre，并可在进程结束时交付原Bash Post。Current Pulsara的`terminal_process`与`terminal_monitor`不是这种透明transport：它们是model可见、各自经过permission/effect与canonical ToolResult settlement的独立Builtin。本文以current owner truth为准：只有`terminal`投影为`Bash`；后两者按自己的exact names各自产生Pre/Permission/Post，绝不把later result回挂成早先`terminal` call的delayed Post，也不新增process-id→Hook occurrence registry。CLI doctor必须列出这项deliberate transport difference。

Round 9 meta route：

- ToolRuntime先签发§8.4 closed preparation union。Success `PreparedResolvedToolInvocation`中`use_new_mcp_tool`已经exact resolve opaque ref/route/schema/policy并持有immutable borrow，但authorize/admit/attempt/invoke均为0；
- Hook按underlying resolved provider-qualified MCP identity运行一次；
- outer meta wrapper不再触发第二组Pre/Permission/Post；
- successful route的stdin `tool_input`是exact underlying arguments that the resolved tool would receive，另保留`pulsara_tool_name=use_new_mcp_tool`用于说明outer route；不得把opaque ref/meta envelope谎称为underlying arguments；
- inspect/list等目录Builtin按自身name触发。

若`use_new_mcp_tool`在underlying identity存在前因invalid arguments、unknown/stale opaque ref或route resolution失败而形成canonical no-attempt ToolResult，则PreToolUse=0，PostToolUse以outer exact identity `use_new_mcp_tool`运行一次；此时没有underlying identity可伪造。若route已经exact resolve后才发生underlying schema/semantic rejection，rejection carrier保留underlying identity，Post只用underlying一次。Resolution success仍严格只使用underlying identity的一组Pre/Permission/Post。Close/cancel/revoked owner不形成ToolResult时PostToolUse=0。Dispatcher不得为了补identity重查registry。

Hosted provider tool没有local attempt seam时不伪造Hook覆盖。

---

## 6. Command execution

### 6.1 Attempt

```text
HookCommandAttempt
  exact typed public event input
  exact FrozenHookDefinition
  exact non-capability HookDispatchScopeRef + HookDispatchCausalRef
  event dispatch ordinal + source ordinal + source-local definition ordinal
  command environment
  inherited owner deadline + effective attempt deadline
  process/task handle
  bounded stdout/stderr buffers
```

Attempt只引用definition；source provenance由definition中的single owner-issued source reference取得，不复制一份attempt proof。Generic executor不解析Plugin、Tool registry或repository object。Definition producer只冻结source-declared stable overlay；lifecycle/Host owner在dispatch时另行冻结cwd与Host-derived environment。Current USER/WORKSPACE declaration overlay为空，attempt-time adapter提供：

```text
PULSARA_HOOK_SOURCE_DIR = directory containing hooks.json
PULSARA_PROJECT_DIR     = exact workspace root when available
```

Round 9.3 Plugin Hook adapter通过trust-covered declaration environment提供`PLUGIN_ROOT/PLUGIN_DATA`；generic executor只消费closed overlay。Plugin不增加agent-definition语义。

### 6.2 Process semantics

- stdin是§9 bounded JSON object；
- Unix使用configured shell command语义，Windows使用`commandWindows`或fallback command；
- cwd为event exact current workspace/run cwd；
- 每attempt创建独立POSIX session/process group或Windows等价owner；
- spawn成功后立即并发drain stdout与stderr；不能等process exit后才读pipe；
- 一个attempt owner唯一仲裁exit、capture overflow、timeout、caller cancel与Host close；
- abort顺序固定为terminate whole group → 在§6.4允许的remaining physical grace内等待 → kill whole group → drain EOF → join readers/process；
- exit status按§7 event-specific parser解释；除明确支持的exit 2外，nonzero、spawn failure、timeout或invalid output均fail-open；
- Runtime在一个存活attempt内不主动重复启动同一handler。

### 6.3 Secret boundary

只把exact active `PULSARA_API_KEY`值视为必须排除的secret。Secret处理不能改变用户已经review并trust的command语义：

- 每个attempt持有process-local `HookSecretScrubSet`：dispatch/spawn时的nonempty active value始终保留；stdin serialization、每次output capture以及每个CLI/log/status/context/provider sink前重新snapshot current nonempty value并加入。API key在background attempt期间变化不能让旧attempt只按旧值或只按新值scrub；
- replacement优先使用`[REDACTED_PULSARA_API_KEY]`，但必须先验证它不包含scrub set中的任何value；若碰撞就使用empty replacement。按longest-first替换并做final byte scan。任何外部sink的最终bytes postcondition是：不包含scrub set及sink-time current active value中的任一nonempty exact value；无法满足就整项drop/no-spawn，不能输出“已经脱敏”的假诊断；
- 当前platform实际selected command variant或final definition/runtime-injected environment overlay若包含scrub set中的value，**本attempt**以`API_KEY_VALUE_PRESENT` no-spawn；不得替换command/argv/env后执行另一份未被review的语义，也不得把current definition view永久标成UNAVAILABLE。Non-selected `commandWindows`/`command`与`statusMessage`不参与spawn拒绝，只在外显sink scrub；unset/empty不参与匹配，文本`PULSARA_API_KEY`变量名本身不是secret；
- trust digest仍覆盖原始normalized declaration，但CLI list/inspect/trust/doctor在每次render sink执行上述scrub/final verify，绝不能回显raw match；
- spawn environment显式删除`PULSARA_API_KEY` key，并在其他**ambient inherited** environment values中替换scrub set的exact values；这不修改trusted definition overlay；
- stdin的所有string object keys与string values在serialization前scrub；scrub造成duplicate key、fixed-schema key变化或invalid typed input时exact handler不spawn；
- stdout若是bounded valid JSON，parser只在private raw buffer中解析且绝不把raw写入exception/log，然后对decoded string keys/values scrub再做schema/control解析；若scrub造成duplicate key或改变fixed schema key，exact handler output invalid且不产生control/context。Plain stdout与stderr在parse/log/context前做cross-chunk exact scrub；invalid JSON的diagnostic也只能使用scrubbed、final-verified preview；
- 最终argv只可能来自已验证不含secret的exact selected command；spawn/parse exception、trace、diagnostic、repr、`HOOK_CONTEXT`与provider serialization全部在自己的sink再次scrub/final verify，raw matched bytes不能写artifact或任何sink；
- 其他prompt、Hook input/output、model reply、DSN、path与token-looking文本保持可观察。

每次physical spawn前、紧邻process-creation sink都重新读取current active API-key value并加入attempt scrub set，然后对exact reviewed selected command、最终environment（含key与value）及最终stdin bytes做一次共同postcondition；任一包含current/retained exact secret时`API_KEY_VALUE_PRESENT + no-spawn`，不得改写command后执行。Reload之后API key改变不能依赖旧scan结论。Scrub set随attempt/background contribution retirement销毁，不持久化secret，不改变definition trust digest，也不是新的trust状态。

### 6.4 Timeout与physical concurrency

```text
ordinary command default        600 seconds
ordinary command range          1..600 seconds
SessionEnd default                1 second
SessionEnd range                  1..3 seconds
process-group abort/join          5 seconds

synchronous command slots        16
background command slots           8
```

Slots是Host-owned physical process/FD/RSS scheduling bounds：每个running command至少占用一个process group、stdout/stderr两条pipe及对应reader/task。`16`个sync slots限制前台瞬时process/pipe压力，独立的`8`个background slots防止diagnostic work占满前台control容量；两池不能互借成绕过bound的第三池。额外matching handlers只按同一absolute deadline排队而不被逻辑拒绝。修改这些sealed defaults必须以process/FD/RSS资源验证为依据；不得借此添加logical handler-count gate。每个attempt deadline从event dispatch计时，不能通过排队获得新timeout。没有selected-handlers、session Hook count或Hook lifetime cap。

每个event producer必须传入existing owner absolute deadline（若该owner有deadline）：

```text
effective_attempt_deadline = min(
  event_dispatch_monotonic + configured_timeout,
  inherited_owner_deadline,
)
```

没有parent deadline时才只使用configured timeout。Semaphore queue wait始终消耗这份deadline，acquire后不得刷新。Timeout后最多5秒physical grace，但其终点仍取`min(effective_attempt_deadline + 5s, inherited_owner_deadline)`；若remaining grace为0则立即kill。Dependent product effect不得在Hook process仍可能返回control时开始；若parent cancel/close/deadline先到，原owner走existing cancellation/deadline结果，而不是把“fail-open”解释成继续一个已经失效的operation。

### 6.5 Output bounds

```text
stdout capture  1 MiB
stderr capture  1 MiB
stdin JSON      existing MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES
```

Capture越界终止exact process。Output parser复用§2.3 node/depth与ordinary scalar bounds；`additionalContext`是唯一明确的scalar exception，可使用capture中最多1 MiB的完整UTF-8 string，再进入per-handler threshold与existing single-source/aggregate compiler bounds。Stdin保留各event upstream owner已经验证的field bounds，整体才受`MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES`；generic Hook serializer不得另施加64 KiB scalar cap。若完整serialization超aggregate bound，exact handler以`HOOK_STDIN_BOUND_EXCEEDED` no-spawn/no-control fail-open；不得截断prompt/tool arguments后执行一份语义不同的command。不得为Hook建立artifact或专用大正文通道。

### 6.6 Sync/background

`async=false`为synchronous：selection时按definition order签发admission ordinal，matching attempts并发等待physical slots；event owner在自己的absolute deadline内等待settle，并按definition order聚合。Runtime只承诺deterministic admission与aggregation，不承诺OS process实际start/finish顺序。

`async=true`为background：

- SessionEnd无条件强制sync；其他event的background handler不能产生allow/deny/block/rewrite/stop/continue。Async profile有意更严格：output只要出现control-vocabulary key（包括neutral `continue:true`、SubagentStart中sync时known-ignored的`continue`、任何decision/permissionDecision/rewrite/suppress key），exact handler output就整体invalid，不能部分保留context；`systemMessage`不是control key，可继续作为diagnostic；
- 只有valid diagnostic与event本来允许的informational context可进入exact-scope process-local pending buffer；
- 每项携带`(event_dispatch_ordinal, source_ordinal, source_local_definition_ordinal)`；safe point只freeze**当时已经完成**的entries，按该tuple排序，不等待任何较早in-flight attempt；
- 较早event晚完成时可在后续safe point出现，因此不承诺跨safe-point的global completion order；
- 下一次同scope合法provider safe point可把completed entries作为`HOOK_CONTEXT`消费；Host close、scope terminal或无future safe point允许丢失；
- scope terminal/close之后的late completion直接丢弃，不能写入新ROOT、child、workspace或run scope。

SessionStart/SubagentStart的“first request/first compile context”只约束在event owner等待期间已经settled的**synchronous** aggregate。Async contribution若在first safe-point freeze前完成可一并进入；若在first open/compile后才完成，则按同一个`HookDispatchScopeRef`进入ROOT/child background slot，并只可在下一次合法provider safe point one-shot交付。Launch/final-install失败、scope terminal或没有下一safe point时直接丢弃。不得为了等待async start context延迟first open，也不得把late start context投到parent或replacement child。

### 6.7 Diagnostic/status sink

整个Hook subsystem只有一个process-local diagnostic adapter，直接使用现有application logging/status port；不增加Committed/Live event kind、canonical row、Hook history或Plugin sink。它消费：

- config/source/trust/reload disposition；
- configured `statusMessage` attempt label；
- spawn/timeout/overflow/exit/parse/unsupported-control diagnostics；
- terminal/idle event上没有provider consumer的informational output；
- API-key exact scrub后的stderr与exception detail。

同一sync event或同一completed safe-point batch内，attempt diagnostics按`(event_dispatch_ordinal, source_ordinal, source_local_definition_ordinal, diagnostic_ordinal)`稳定render；跨safe point或实时日志到达不承诺全局重排，只携带这些ordinals供观察。Discovery/trust diagnostics按source/definition order稳定render。日志sink不可反向控制dispatcher，也不能把`statusMessage`升级为SYSTEM或`HOOK_CONTEXT`。

---

## 7. Output与control contract

### 7.1 One parser transaction

每个settled command只经过一个output parser transaction并形成`ValidHandlerContribution | InvalidHandlerOutput | HandlerFailure`。Parser先在private bounded raw buffer中判断JSON/plain并按§6.3执行不改变wire parse的exact API-key scrub：JSON先做duplicate-safe raw decode、再scrub decoded string keys/values后验证schema；plain/stderr先完成cross-chunk scrub再解释或诊断。Raw secret bytes绝不进入exception/log/context。Parser contract固定为：

1. `exit 0 + empty stdout`：neutral success；
2. `exit 0 + JSON`：stdout必须是一个duplicate-key-free root object，沿用§2.3 node/depth/ordinary scalar bounds；`additionalContext`正文按§6.5的1 MiB exception；root array invalid；
3. 存在`hookSpecificOutput`时它必须是object，且`hookEventName`必须是当前external event exact literal；mismatch使exact handler invalid；
4. unknown output field、错误type、event不支持的control或同一handler中的互斥control使**exact handler完整output** invalid；不得保留其中“看起来还能用”的context或control；
5. 以`{`或`[`开头但不是合法JSON时是handler failure，不fallback为plain text；
6. plain stdout只有`SessionStart`、`UserPromptSubmit`、`SubagentStart`形成additional context；`Stop/SubagentStop`的plain stdout invalid；其余event的plain stdout只进入diagnostic，control为neutral；
7. `SessionEnd`始终observe-only：bounded stdout/stderr可诊断，但不进入model或control；
8. background output包含任何control字段时exact output invalid；不能部分保留其context。

Raw JSON、field strings与exit status只存在于Hook core。Lifecycle owner永远只收到§0.4 typed outcome。

### 7.2 Exit status

Exit 2是Codex公开协议中少数event的合法control，不得被generic nonzero分支吞掉：

| Event | exit 2 mapping | Pulsara effect |
|---|---|---|
| `PreToolUse` | deny，reason取bounded scrubbed stderr | `GateOutcome.BLOCK`；authorize/admit/attempt/invoke为0 |
| `UserPromptSubmit` | block，reason取stderr | `GateOutcome.BLOCK`；canonical ingress为0 |
| `SubagentStop` | request continuation，reason取stderr | `ContinuationOutcome.CONTINUE_ONCE`，仍受once guard |
| `Stop` | request continuation，reason取stderr | `ContinuationOutcome.CONTINUE_ONCE`，仍受once guard |
| `PostToolUse` | Codex feedback replacement | 本轮`UNSUPPORTED_CONTROL`；handler invalid、原ToolResult不变、owner fail-open |
| other events | unsupported nonzero | handler failure、neutral outcome |

Exit 2本身就是表中supported events的explicit control，不再解析stdout。Scrubbed stderr trim后非空则作为reason；为空仍保留BLOCK/CONTINUE_ONCE并令optional reason为null，同时产生diagnostic。Stop/SubagentStop在需要provider continuation正文而reason为null时使用固定低权威Runtime文案“Lifecycle Hook requested one additional pass.”，不能把空值提升成SYSTEM。其他nonzero、signal termination、spawn/timeout/capture failure全部是handler failure。

### 7.3 Supported JSON forms

External “common” fields仍按event-scoped profile解释，不存在所有11项event共享的control DTO：

- `systemMessage:string`在下表列出的所有event都只进入§6.7 diagnostic/status；永不进入SYSTEM或`HOOK_CONTEXT`；
- `SessionStart/PreCompact/PostCompact/UserPromptSubmit/SubagentStop/Stop`接受common control family：`continue:true` neutral，`continue:false`产生表中control，`stopReason`只能与`continue:false`配对，`suppressOutput:false`为known no-op，`suppressOutput:true` unsupported；
- `PreToolUse/PermissionRequest`只接受`systemMessage`，任何`continue/stopReason/suppressOutput`字段都使exact handler invalid；
- `PostToolUse`接受`systemMessage`、`continue:true` neutral与hook-specific context；`continue:false/stopReason/decision:block/exit 2`因会替换或抑制原ToolResult而是本轮deliberate unsupported；任意`suppressOutput`也unsupported；
- `SubagentStart`接受`systemMessage`、context与boolean `continue`，但`continue`只做known-ignored diagnostic、绝不撤销task；`stopReason/suppressOutput` unsupported；
- `SessionEnd`只有observe diagnostic；control/context字段unsupported；
- unknown root/common field使exact handler invalid。

Event-specific parser-to-outcome映射固定为：

| Event | accepted external output | typed contribution |
|---|---|---|
| SessionStart | plain stdout或`hookSpecificOutput.{hookEventName,additionalContext}`；common control family | context；或`continue:false`→gate `BLOCK` |
| SessionEnd | no control/context | observe diagnostic only |
| UserPromptSubmit | plain或`hookSpecificOutput.{hookEventName,additionalContext}`；top-level legacy `decision:"block", reason:nonempty string`；common control family；exit 2 | context；legacy/`continue:false`/exit 2→gate `BLOCK` |
| PreToolUse | `hookSpecificOutput.{hookEventName,permissionDecision:"deny",permissionDecisionReason?:string,additionalContext?:string}`；top-level legacy `decision:"block",reason:nonempty string`；exit 2；`systemMessage` | gate/context；`permissionDecision:"allow"`与任何rewrite均unsupported，原owner fail-open |
| PermissionRequest | `hookSpecificOutput.{hookEventName,decision:{behavior:"allow"|"deny",message?:string}}`；decision omitted；`systemMessage` | `ALLOW | DENY | ABSTAIN`；exit 2是ordinary failure→ABSTAIN，不是DENY |
| PostToolUse | `hookSpecificOutput.{hookEventName,additionalContext}`、`systemMessage`、`continue:true` | context only；`decision:"block"`、`continue:false/stopReason`、exit 2均unsupported |
| PreCompact | `continue:false + stopReason?` | gate `BLOCK`=abort exact compaction |
| PostCompact | `continue:false + stopReason?` | gate `BLOCK`=stop active continuation；idle降为diagnostic |
| SubagentStart | plain或`hookSpecificOutput.{hookEventName,additionalContext}`、`systemMessage`、boolean `continue` | context only；`continue` known-ignored，不能撤销task |
| SubagentStop | top-level legacy `decision:"block",reason:nonempty string`或exit 2；common control family | continue-once或explicit `continue:false` terminalize |
| Stop | top-level legacy `decision:"block",reason:nonempty string`或exit 2；common control family | continue-once或explicit `continue:false` terminalize |

`hookSpecificOutput.additionalContext`只在SessionStart、UserPromptSubmit、PreToolUse、PostToolUse、SubagentStart合法。Stop/SubagentStop的continuation reason由`reason`/stderr形成continuation context；Pre/PostCompact不接受额外model context。一个handler同时返回continuation request与`continue:false`属于in-handler conflict并整项invalid；不同handlers之间的合法`continue:false`按§7.4优先。

明确unsupported且不得partial mutation：

- `updatedInput`、`updatedMCPToolOutput`、ToolResult replacement、result suppression；
- PreToolUse `permissionDecision:"ask"`、legacy `decision:"approve"`；
- PermissionRequest `updatedInput`、`updatedPermissions`、`interrupt`；
- event不支持的`continue/stopReason/decision/permissionDecision`；
- 任意generic state patch或unknown nested output。

Unsupported rewrite只使exact handler invalid。若existing owner仍live，原invocation使用**原arguments至多一次**继续原authorize path；不得把unsupported output制造成synthetic rejection ToolResult，也不得产生partial argument mutation。

### 7.4 Ordered aggregation

Parser先逐handler验证，再按definition order聚合；一个invalid handler不污染valid siblings：

- `GateOutcome`：任意valid `BLOCK`获胜；reason取definition order中的第一个blocking reason；任何proceed都不能覆盖block；
- `PermissionOutcome`：`DENY > ALLOW > ABSTAIN`；engine failure/no-decision保持ABSTAIN。Winning rank为DENY或ALLOW时，optional reason取该rank中definition order第一个nonempty `decision.message`，没有则null；不得从losing rank借reason；
- `ContinuationOutcome`：只有explicit `continue:false`贡献terminalize veto；empty/neutral handler不是veto。任意valid explicit terminalize优先，此时reason取这些veto中definition order第一个nonempty `stopReason/reason`；否则guard未使用且存在continuation request时为`CONTINUE_ONCE`，reason取requesting handlers中definition order第一个nonempty reason/stderr；都没有则null。若once guard已经使用且没有explicit veto，continuation request只诊断、最终TERMINALIZE，reason可取first request但绝不形成model context；
- `ContextOutcome`与`GateOutcome`附带context：只收集valid、admitted entries，按definition orderrender。Continuation没有独立ordered-context DTO；只有最终`CONTINUE_ONCE`的selected reason由owner按§9.2形成至多一个continuation contribution；
- `ObserveOutcome`：只有diagnostics。

Physical completion order不影响control winner或sync context order。Background不参与control aggregation。

Selection结束后，同一event的全部matching synchronous handlers都取得admission ordinal；较早完成的deny/block不会取消尚未开始或尚未完成的sibling。Event owner只在其absolute deadline到达时取消未settled siblings，然后按已有valid contributions聚合。

### 7.5 Fail-open、liveness与external side effect

Trusted Hook command以Host OS用户身份运行，可能产生任意filesystem/network/process side effect；它不是sandboxed Tool。CLI inspection必须明确显示exact command trust不递归证明script、PATH、imports或依赖内容。

“Fail-open”只表示：engine/transport/parser/unsupported-output没有签发control，**原owner若仍live**则按自己的原路径继续。它不表示：

- 回滚trusted command已经产生的external side effect；
- cancel/close/deadline/stale后复活operation；
- 绕过scope/effect/dirty/liveness revalidation；
- 把PermissionRequest failure当成ALLOW；
- 修改canonical ToolResult或provider prefix。

Hook不承诺cross-crash exactly-once。Host可能在command产生external side effect后、上游transition前崩溃，未来发生duplicate或loss；作者应设计幂等handler。已有canonical FULL/stateless confirmation证明transition已成立时不得重跑对应ingress Hook，但不为此新增receipt/history/replay。

---

## 8. Exact lifecycle seams

### 8.1 Matrix

| Internal / external | 唯一producer与linearization point | 合法consumer与terminal outcome |
|---|---|---|
| `SessionStartEvent` / SessionStart | ROOT provider-loop owner；所有headroom/semantic/dry trial完成、exact first-open attempt/admission选定后，但final installable compile/continuity install/open前；compact由ROOT runner提供的call-local §8.5 port在coordinator fence内、adoption FULL与PostCompact proceed后调用 | ROOT owner/port消费`GateOutcome`；BLOCK使本次ROOT continuation interrupted且provider open=0；settled sync context交给first installable request，late async按§6.6进入same-scope future safe point |
| `SessionEndEvent` / SessionEnd | Host §4.3 terminal lane；ordinary producers quiesce/fence后、dispatcher/process/repository close前exact once | Host只消费`ObserveOutcome`；无model/control consumer，失败后仍physical close |
| `UserPromptSubmitEvent` / UserPromptSubmit | Host-owned `NewTurnHookGate`；validation/compatible preflight后，任何queue row、USER_MESSAGE、active ROOT task或provider open前 | Host消费`GateOutcome`；BLOCK产生typed ingress rejection且durable writes=0，PROCEED才进入direct/queued admission |
| `PreToolUseEvent` / PreToolUse | ordinary `ToolBatchExecutor`在ToolRuntime签发exact prepared resolution后；Plan-control batch则由`PlanToolBatchCoordinator`在valid selected control与whole-batch barrier冻结后；两者都早于authorize/canonical control admission/attempt/invoke | exact tool/Plan owner消费`GateOutcome`；BLOCK沿各自existing no-attempt ToolResult transaction，PROCEED才authorize/apply prepared invocation |
| `PermissionRequestEvent` / PermissionRequest | existing permission owner确定ASK并签发exact pending request后、human interaction/admit/attempt前 | permission owner消费`PermissionOutcome`；ABSTAIN回原human flow，ALLOW/DENY只settle exact request |
| `PostToolUseEvent` / PostToolUse | canonical ToolResult FULL及process-local effect settlement完成后、next compile前exact once；ordinary owner是`ToolBatchExecutor`，Plan immediate/delayed result由`PlanToolBatchCoordinator`/interaction waiter owner投影settlement carrier | 只消费`ContextOutcome`；canonical result永不改变，无future call时降为diagnostic |
| `PreCompactEvent` / PreCompact | `CompactionCoordinator`取得global lane与exact-scope fence、完成trigger-only frozen source projection并确认`should_trigger=true`后，但snapshot candidate/write、summary/provider call前 | coordinator消费`GateOutcome`；BLOCK时snapshot candidate/write、summary request/call、adoption、successor install/open均为0 |
| `PostCompactEvent` / PostCompact | `CompactionCoordinator`确认canonical adoption FULL后、任何final successor install/open前；active/idle/historical winner都以adoption事实为唯一producer条件 | coordinator始终收到`GateOutcome`；active BLOCK不打开successor；idle/historical无continuation consumer，只保留diagnostics并discard control/context |
| `SubagentStartEvent` / SubagentStart | subagent scheduler owner在task FULL并取得§8.6 `PreparedSubagentLaunch`后、child runtime install与first compile前 | 只消费`ContextOutcome`；task不能撤销；settled sync context只给child first compile，late async按§6.6给same child future safe point；omission/launch failure走existing outcome |
| `SubagentStopEvent` / SubagentStop | subagent completion owner冻结EXPLICIT/INFERRED candidate并取得completion permit后、任何SubagentResult/composite commit前 | owner消费`ContinuationOutcome`；首次可continue，otherwise terminalize；每次都有明确assistant/tool settlement |
| `StopEvent` / Stop | ROOT runner冻结natural final public assistant candidate、确认无tool calls后，但`PreparedAssistantMessageSettlement`提交前 | ROOT owner消费`ContinuationOutcome`；首次可settle为nonterminal并继续，otherwise settle terminal |

11项producer都由existing lifecycle owner直接调用一个dispatcher；没有generic event bus或第二个lifecycle registry。若实现无法在上表位置构造typed input并只消费指定family，必须停止编码并修订本文，不能把dispatch移动到“附近”或让Hook core取得owner object。

### 8.2 Session

`SessionStart`与`SessionEnd`只属于ROOT session；child first-open使用`SubagentStart`，child compaction可运行Pre/PostCompact，但不伪造ROOT SessionStart/End。

SessionStart source只有：

- `startup`：新canonical conversation的Host首次实际ROOT provider attempt；
- `resume`：已有canonical conversation由新Host恢复后的首次实际ROOT provider attempt；
- `compact`：canonical compaction adoption后该successor epoch的首次ROOT provider attempt。

Codex的`clear`在本轮是known-no-producer，inspection显示该差异，Runtime不得伪造。ROOT runner/Host共同拥有**一个**窄process-local pending cold-boundary slot，而不是runner-lifetime consumed boolean：Host launch先arm `startup|resume`；每次ROOT compaction adoption FULL以exact `CompactionAttemptToken + snapshot + binding revision` arm `compact`并覆盖尚未消费的launch boundary。Active ROOT由coordinator fence内的call-local port exact-consume该compact boundary；idle或已经成为historical的adoption保留pending compact，直到下一次actual ROOT provider attempt。这样compact-before-first-open必然supersede resume/startup，ordinary start已消费后的idle compact也会为successor重新arm；child永远没有此slot。Slot只持一个pending immutable value，不是registry、generation、lease DTO或token map。

Dispatch admission在exact owner处原子consume pending boundary并签发owner-issued immutable `SessionStartAttemptToken`：startup/resume绑定Host launch boundary，compact绑定exact compaction adoption/snapshot identity。同一token上的dry recompile/CAS replan不得重跑；没有pending boundary时不得补发late SessionStart。Installed continuity本身已表明后续是ordinary append，不需要把token绑定epoch nonce。Host crash后resume可签发新launch boundary并再次运行，符合weak exactly-once且不增加receipt。

Host cold composition必须向provider-loop传入immutable launch kind，不能让startup/resume在共同`_open`路径中靠current state重猜。SessionStart允许existing headroom estimate、semantic preflight与uninstalled dry trial在前，但必须早于final installable compile、continuity install与transport open；initial path继承当前planning cut已经冻结的absolute deadline，active compact facts继承同一`successor_deadline`，两者都不得重新调用deadline factory刷新owner期限。Compact顺序见§8.5。Hook等待期间不得持Host lock；BLOCK或owner liveness失效时context reservation立即retire，并通过owner-typed `SessionStartBlocked`让ROOT runner走existing turn interruption，provider open=0。Engine/parser failure是PROCEED，但不能覆盖close/cancel。

Idle ROOT compaction仍运行固定`GateOutcome`的PostCompact，但owner只保留diagnostics并discard control/context；pending `SessionStart(source=compact)`延迟到下一次actual ROOT provider attempt。没有future attempt就没有幽灵context。SessionEnd在§4.3 terminal lane同步执行，timeout固定受`1..3s`配置和Host close remaining deadline共同约束；无论handler成功与否都继续physical close。

### 8.3 Prompt ingress

```text
local validation
→ under Host lock reserve/join exact in-flight command attempt，或判different candidate CONFLICT
→ release Host lock
→ reservation owner执行stateless compatible-ingress preflight
→ canonical FULL or CONFLICT: Hook attempts = 0；settle/release reservation
  （direct保留already-accepted/query-outcome behavior；queued返回existing command outcome）
→ NONE: freeze exact ingress candidate并运行UserPromptSubmit
→ reacquire Host lock and revalidate close/fence/plan/reservation
→ BLOCK: active task/queue row/USER_MESSAGE/provider open = 0
→ PROCEED: enter existing direct or queued admission path；capacity仍由ordinary repository transaction裁决
```

Host是唯一`NewTurnHookGate` owner；direct `run_turn`与queued `submit_prompt`只共享这一套gate实现、parser/outcome/context owner，不各自复制机制。Current direct future turn identity由`(session, command)`稳定构造，queued future turn identity由`(session, queue_item)`稳定构造，因此两种API的candidate不是同一个对象，也不能为了Hook统一而改写已有observable IDs。Host在full candidate/Plan projection尚未读取前，先以`IngressHookReservationKey(API variant, command id, exact prompt bytes, requested permission mode, queued queue_item_id when applicable)`签发唯一process-local reservation；Host-session/workspace由owner slot隐含，不复制进key。同一exact key的并发retry coalesce到一个in-flight attempt/future；**仅在这份reservation仍in-flight时**，同command但不同key（包括另一个API）才在Hook前走typed CONFLICT，不能各自跑Hook。Reservation owner随后完成stateless preflight并冻结full candidate/effective permission cut；不得声称最初lock已经比较了一个当时尚不存在的full candidate。Canonical row已经存在时继续由stateless confirmation裁决。BLOCK、write failure或owner abandon会释放reservation；未来caller可按weak duplicate语义形成新attempt，不能为防重保留session-lifetime tombstone、receipt或无界command history。Reservation不是durable lease，也不能在等待Hook期间占Host lock。

Reservation必须先于stateless repository preflight线性化，否则两个caller都可先读到NONE，winner完成后loser再取得reservation并重复Hook。Host lock只负责claim/join/conflict，不在锁内await repository或Hook；唯一reservation owner释放锁后确认FULL/CONFLICT/NONE，joiner等待同一个future。

Gate前构造private `PreparedNewTurnHookCandidate`，只冻结能在canonical write前诚实取得的values：command/request/queue identity、完整prompt、按current各自stable-ID framing计算的future exact turn/entry/context-revision IDs，以及existing permission/Plan owner给出的`PreparedIngressPermissionProjection(requested, effective, exact Plan/precondition cut)`。Current direct与queued的entry/revision公式并不相同，不能用一个无variant helper或logical-ID字符串前缀暗推。只从current builders factor两个thin pure helpers：

```text
build_direct_root_turn_identity(session_id, command_id)
  turn     = stable("turn", session_id, command_id)
  entry    = stable("entry", turn, "user")
  revision = stable("context-revision", turn, "0")
  permission_snapshot = stable("permission-snapshot", turn)

build_queued_root_turn_identity(session_id, queue_item_id)
  turn     = stable("turn", session_id, queue_item_id)
  entry    = stable("entry", session_id, queue_item_id)
  revision = stable("context-revision", session_id, queue_item_id)
  permission_snapshot = stable("permission-snapshot", session_id, queue_item_id)
```

Queued `queue_item_id`本身继续使用current `stable("queue-item", session_id, command_id)`；helper不另发明identity。两个helper只计算IDs，不需要queue sequence/content/effective permission/occurred_at，不复制full admission DTO。Later direct/queued full admission builder必须接收或重算并exact-join各自四个IDs与permission precondition；observable canonical IDs保持current code不变。

- Direct candidate用command id调用direct helper冻结future IDs；BLOCK返回typed `PromptBlockedByHook(reason)`，不得先安装active ROOT task。
- Queued candidate在不insert row的前提下按current公式计算stable `queue_item_id`，用queued helper冻结future turn/entry/revision/permission-snapshot IDs；后续取得queue sequence后才构造full `PreparedQueuedRootTurnAdmission`并exact-join，不能在Hook前调用需要queue sequence的full builder、把queue id本身冒充turn id或在Hook后另算。BLOCK返回existing `KernelCommandOutcome(status=REJECTED, code=HOOK_BLOCKED)`。
- PROCEED context分别绑定direct exact candidate identity或queued `queue_item_id`，随后才materialize content与执行ordinary admission/enqueue。Direct FULL可把exact contribution交给该唯一live turn；queued enqueue FULL/ACK-confirmed FULL后只能把contribution留在`queue_item_id`-bound process-local slot，绝不能放入通用ROOT bucket。只有`consume_prepared_prompt_head`确认exact queue candidate FULL、且Host在同一local critical section成功安装B的唯一live ROOT task后，才把该slot转交ROOT bucket；close、conflict或task-install failure retire exact slot。因而较早的active A无论tool-followup compile还是compaction都无法freeze B的context。
- Queue head以后只消费已运行过Hook的row；canonical FULL/compatible retry、queue delivery与provider replan均不得再次触发UserPromptSubmit。
- Direct canonical FULL/CONFLICT保持current `already accepted; query outcome`语义；其返回型不能从command row伪造一个`KernelRunResult`。只有同进程同candidate的in-flight retry可共享原future。Queued FULL/CONFLICT才返回existing `KernelCommandOutcome`。
- Admission write ACK-unknown只做existing exact stateless confirmation：原exact direct/queued admission owner确认FULL后继续/返回且不重跑Hook；确认CONFLICT走各API existing behavior。Later unrelated direct caller不承诺取得运行中`KernelRunResult`；只有无法证明任何row成立且调用方发起新的logical ingress时才可能再次产生external side effect。
- Queue capacity由existing repository enqueue transaction原子重验，Host lock中的advisory observation不能取代它。Content materialization、capacity、enqueue、direct admission或close revalidation失败时只清理exact pending context/reservation，不影响sibling ingress。
- UserPromptSubmit wire `permission_mode`来自candidate的effective projection。PROCEED write transaction必须exact-join其Plan/precondition cut及effective mode；Hook等待期间Plan/permission事实漂移时write=0、retire context并返回typed `INGRESS_PRECONDITION_CHANGED`。Runtime不得用requested mode冒充effective，也不得静默重跑Hook；caller发起新的logical ingress时可形成new candidate，weak duplicate语义仍适用。

Steer、automatic continuation、Hook continuation、Plan continuation与subagent objective不是新的user prompt，不触发该event。Crash发生在Hook external side effect后、canonical ingress前时可能在未来logical retry重复执行；不为此增加receipt。

### 8.4 Tool与permission

ToolRuntime新增private-construction、owner-issued、immutable `PreparedResolvedToolInvocation`：

```text
exact original model call carrier + tool_call_id
exact resolved invocation arguments
canonical underlying tool identity + compatibility aliases
outer Pulsara/meta name
validated schema/surface facts
immutable route/binding borrow
scope/effect/dirty/liveness facts needed for later revalidation
```

Preparation API返回closed union，而不是用exception丢失Post identity：

```text
PreparedToolInvocation
  = PreparedResolvedToolInvocation
  | PreparedToolPreparationRejection

PreparedToolPreparationRejection
  exact original model tool-call carrier
  typed semantic rejection reason
  exact post_tool_identity
    direct requested identity
    | outer use_new_mcp_tool before underlying resolution
    | underlying identity after exact route resolution
  optional immutable borrow to release
```

这个rejection只表示owner仍live且必须形成canonical no-attempt ToolResult的parse/schema/surface/resolution failure；close/cancel/stale/revoked-owner仍走existing interruption，不伪造成该union branch。它不复制ToolResult body、projection或dispatcher DTO。

Preparation只做parse、schema validation、exact direct/meta resolution与immutable borrow；authorize、permission admit、tool attempt、invoke、external effect均为0。Success branch才运行PreToolUse。Rejection branch按existing no-attempt ToolResult完成canonical settlement并在FULL/effect settlement后用carrier中的`post_tool_identity`产生一次PostToolUse：direct使用requested identity；meta pre-resolution失败使用outer `use_new_mcp_tool`；route已resolve后才invalid则使用underlying。Close/cancel/stale/revoked-owner failure走existing interruption，ToolResult/PostTool均为0，不能用“exhaustive origin”复活已失效call。

固定顺序：

```text
ToolRuntime.prepare_resolved_invocation
→ PreToolUse on underlying canonical identity exactly once
→ BLOCK: release borrow；authorize/admit/attempt/invoke=0；existing no-attempt ToolResult
→ PROCEED/fail-open: authorize_prepared
→ revalidate scope/effect/dirty/liveness
→ existing permission/admission/attempt/invoke path
```

Dispatcher不能按name重查registry，也不能获得ToolRuntime/executor/repository。Round 9 `use_new_mcp_tool`成功resolution后只触发underlying MCP的一组Pre/Permission/Post；outer wrapper只通过`pulsara_tool_name`诊断，不产生第二组events。只有pre-resolution failure没有underlying identity时，canonical no-attempt ToolResult的Post使用outer identity，不能同时再造underlying event。

Current ROOT runner把任何含`enter_plan | ask_plan_question | exit_plan`的完整tool-call batch交给`PlanToolBatchCoordinator`，不会经过ordinary `ToolBatchExecutor`；Hook coverage不能假装这一owner不存在，也不能把dispatcher下沉进repository transaction。Plan owner把现有`accept_batch()`机械拆成prepare与settle两段：

```text
freeze exact whole batch + selected Plan ordinal + surface borrow + canonical permission
→ validate selected descriptor/schema/arguments/workflow availability
→ INVALID/UNAVAILABLE selected:
     PreToolUse = 0；build existing rejected Plan batch
→ READY selected:
     PreparedPlanControlInvocation（canonical selected identity/args，admit/effect = 0）
     → PreToolUse exactly once
     → BLOCK: build rejected Plan batch with closed HOOK_BLOCKED disposition
     → PROCEED/fail-open: build APPLY Plan batch
→ existing accept_plan_tool_batch / automatic Plan continuation transaction
```

`HOOK_BLOCKED`只是一项process-local prepared Plan rejection disposition/public error detail；repository不得把这个新字符串直接写入`tool_results.result_state`。Selected result复用现有canonical `result_state=PERMISSION_DENIED`、`result_origin_kind=POLICY_NO_ATTEMPT`，model-facing result body可用`error_kind=HOOK_BLOCKED`说明原因；clean-v0 CHECK、event validator与oracle vocabulary均不增长。它不是Hook event/outcome、durable job或第二套permission。Pre BLOCK时Plan workflow、interaction、question waiter、continuation turn与external/tool attempt均为0；batch中未选siblings继续由existing barrier形成`CANCELLED_BEFORE_DISPATCH`。这些siblings及invalid/unavailable selected本来就没有legal prepared invocation，因此Pre=0，但它们的canonical no-attempt ToolResult FULL后各自Post=1。Current Plan control没有ASK permission producer，故不伪造PermissionRequest；未来若permission owner真正决定ASK，只能复用本节同一个`PreparedPermissionRequest`路径。

“Plan batch不经过ordinary `ToolBatchExecutor`”只是**execution/transaction owner分离**，不是Plan逃离统一ToolResult语义。Plan apply/reject/question-resolution transaction继续原子写入同一`TOOL_RESULT` transcript entry、`pulsara_v3.tool_results`与`ToolResultAccepted`；canonical reader仍以同一`ProviderInputItem.TOOL_RESULT`读取，compiler仍以Round 7.1同一provider-neutral outer projection降低。不得为形式统一把Plan控制塞回physical attempt/authorize/invoke pipeline，也不得把question结果拆出human-resolution canonical transaction。

但PostTool的统一边界必须是一个真实的shared settlement contract，不能让Plan owner凭IDs重建body/状态或让Hook再查repository。本轮hard-cut一个process-local、non-capability、无fingerprint的`AcceptedCanonicalToolResultSettlement`，由ordinary Tool、Plan immediate/delayed与composite subagent ToolResult owner共用：

```text
AcceptedCanonicalToolResultSettlement
  exact scope/turn + assistant entry
  canonical call ordinal + exact tool identity + frozen public arguments
  result id + result entry id + accepted entry sequence
  result state + result origin kind
  FrozenToolResultPublicProjectionInput
    canonical inline storage body
    observation/timing + display/coverage/artifact/memory public metadata
```

该carrier只能在canonical ToolResult与同一owner所有必需effect settlement都已FULL后构造；Plan的workflow/interaction effect已在同一transaction内FULL，因此不伪造process-local effect。`FrozenToolResultPublicProjectionInput`不持有artifact raw body、repository handle、writer guard、surface borrow或replay；它只携带现有public projection真正需要的bounded values。Current `model_input/lowering.py::_project_plan_tool_result_if_owned`必须下沉为Round 7.1 provider-neutral pure projection的一部分，compiler与Hook均调用它；Hook不能直接看Plan storage-only workflow/interaction fields。

本轮不为此新建generic ToolResult transaction/writer owner：ordinary、Plan与subagent composite仍保留各自的atomic owner，统一点只在FULL后的shared settlement与public projection。`AcceptedPlanToolBatch`必须按canonical call order返回本次transaction已FULL的settlement tuple（包括barrier siblings）；`AcceptedPlanResolution`在question result FULL时返回exact one settlement。ACK-unknown confirmation返回同一exact values与accepted entry sequence；owner只对首次确认FULL的settlement发Post，不重跑Plan effect、Pre或Post。

Plan PostToolUse仍由process-local owner在repository返回accepted carrier后运行，不由`_repository/plans.py`直接调用dispatcher：

- non-question batch：对returned settlement tuple按canonical call order用Round 7.1 pure projection各dispatch一次；是否有context consumer只看accepted batch carrier的`origin_turn_completed`，不按tool name猜测。Rejected与idempotent existing ENTER当前保持origin turn open，context可进入该turn下一compile；new ENTER/DRAFT使origin terminal时，origin-scope context立即retire为diagnostic，绝不跨到Plan continuation turn；
- valid question：initial transaction返回的non-selected barrier settlements先Post，然后publish/wait human interaction；selected question result直到Host resolution transaction FULL才存在；existing `KernelPlanInteractionCoordinator` waiter把含exact one settlement的`AcceptedPlanResolution`交回Plan owner，owner消费后Post一次，再允许runner next compile；
- ACK-unknown只confirm同一个prepared Plan/result candidate；确认FULL后不重跑Pre、Plan effect或Post。

Existing permission owner仅在policy已经决定ASK后签发immutable `PreparedPermissionRequest`，其中绑定current exact invocation、pending admission/permit与owner nonce；Hook stdin只投影public字段。消费路径闭合为：

- `ABSTAIN`，包括无decision、engine/parser failure：此时才提交existing `REQUIRE_CONFIRMATION` canonical decision并调用原`request_confirmation()`，保留human interaction flow；
- `ALLOW`：不写`REQUIRE_CONFIRMATION`，exact-bind current request，重验scope/effect/dirty/liveness并复用existing ALLOW admission/attempt transaction；
- `DENY`：不写`REQUIRE_CONFIRMATION`，discard exact pending admission/permit，复用existing DENY no-attempt ToolResult settlement。

PermissionRequest dispatch之前canonical `REQUIRE_CONFIRMATION`/interaction/decision row必须为0，否则insert-only authority已经无法合法改写。Hook不创建permission snapshot、不修改mode/effect、不直接publish或settleinteraction row。Hook等待期间发生close、cancel、request replacement、route dirty或owner stale时，late ALLOW/DENY只诊断并丢弃；绝不能复活request。

PostToolUse在每个supported local model tool call的canonical ToolResult FULL且process-local effect settlement完成后exact once，覆盖parse/schema/policy/PreTool deny/Permission deny、direct builtin/MCP、meta MCP underlying、Plan immediate/delayed result、late exact result与`report_agent_result`；任何no-attempt origin也不能漏掉。Hosted provider-native tool若没有local ToolResult seam则不伪造event。

Round 7.1的唯一public provider-neutral pure helper必须同时供compiler与Hook调用，并在event time返回`render_mode + public value`：FULL eligible则FULL，否则first best-available。Hook不得读取artifact raw/private replay，也不得声称future compiler一定选择同一variant。Context只进入同scope下一次合法compile；canonical ToolResult、result visibility与external effect完全不变。

Explicit `report_agent_result`先经过SubagentStop：terminalize时提交SubagentResult/composite ToolResult FULL，再产生PostToolUse，但child scope已经terminal，因此其informational output只进diagnostic、不得buffer到parent；continue时SubagentResult/composite均为0，只提交ordinary“result未接纳，请继续工作”ToolResult，随后正常产生PostToolUse并可供下一child compile消费。

### 8.5 Compaction

Compaction只有一条owner顺序：

```text
CompactionCoordinator acquires existing global lane + exact-scope fence
→ build trigger-only frozen source projection/read
→ decide manual/auto should_trigger
→ below trigger: Hook attempts = 0，ordinary NOT_NEEDED
→ PreCompact（不得持Host lock等待）
→ BLOCK: return PRE_COMPACTION_BLOCKED；snapshot candidate/write/summary request/provider summary/adoption/install/open = 0
→ PROCEED/fail-open: existing snapshot + summary call + canonical adoption
→ require adoption FULL
→ PostCompact
→ idle/historical winner: discard Gate control/context，保留diagnostics并finish adoption
→ active BLOCK: return ACTIVE_CONTINUATION_BLOCKED，successor install/open = 0
→ active ROOT PROCEED: invoke call-local SessionStartCompactPort inside fence
  → compact SessionStart BLOCK: retire exact reservation，return ACTIVE_CONTINUATION_BLOCKED；final sibling/CAS/install/open = 0
  → compact SessionStart PROCEED: continue
→ active child PROCEED: port absent，SessionStart attempts = 0
→ freeze exact Hook context + compaction handoff + current active request
→ from one post-adoption exact fact set build no-Hook/Hook cold siblings
→ select one final installable dispatch
  （PostCompact自身不产context；ROOT可含compact-start/既有eligible context；
    child不产SessionStart context，但可消费更早same-child eligible background/tool context）
→ at most one continuity CAS / permit / execution install
→ physical open_once at most
```

Auto compaction必须读取exact frozen source projection、token estimate与physical working set才能诚实决定trigger；这项trigger-only read可发生在PreCompact前，但不得形成snapshot candidate/write、summary request或provider payload。Coordinator为一次logical compaction签发process-local `CompactionAttemptToken`，贯穿precompile→execute与existing smaller-tail retry/递归；同一token确认`should_trigger=true`后PreCompact至多一次，dry retry不得重跑Hook。BLOCK不删除已存在canonical history，只中止exact compaction attempt。

`PRE_COMPACTION_BLOCKED(reason)`也是coordinator-owned process-local typed result，不扩充durable/Hook/compaction enum。Manual command row在current Host路径中早于physical compaction attempt canonical FULL，因此BLOCK只把其existing process-local waiter settle为`CompactionOutcome(FAILED, HOOK_PRE_COMPACT_BLOCKED)`；不能声称manual command row为0，也不能重跑同一command Hook。Auto/mid-turn owner不记录automatic engine failure/backoff：它在同一planning cycle保留或重新构造ordinary no-compaction dispatch，并对该exact `CompactionAttemptToken`跳过再次trigger；若ordinary compile本身不fit，仍由existing model-input resource boundary诚实失败。Future provider cycle可产生新的logical compaction attempt，不能把一次BLOCK变成session-lifetime disable。

同一skip-once rule也适用于§8.9确认的late-threshold `NOT_NEEDED`：它不是engine failure，不增加existing automatic-failure counter；caller必须消费同一token的已判定事实并让ordinary dispatch在本轮继续到install/open，不能无条件回loop再次触发。Precompile与late-threshold路径都必须做到“同一planning cut至多一次automatic compaction decision，随后若无successor便继续ordinary provider path”；一次provider call/canonical advance后的future cycle仍可签发new token。这不是retry-count或session cap。

`PostCompact`的产品事实只有canonical compaction adoption已经FULL；不再要求successor install或active structural preflight成功。Existing uninstalled dry/structural dispatch可在adoption前构造，但只能证明§0.5 no-context base并且不能包含continuity permit、ToolResult delivery installation或provider execution。Adoption FULL后owner先arm exact compact cold boundary，再settle PostCompact，随后在原`successor_deadline`内对exact turn status做一次cancellation-safe canonical read；不得把任何异常由generic catch降格成pre-adoption `FAILED`、刷新deadline或无限重试。Cancellation、stale owner与永久repository failure必须传播并由existing `finally`释放lane/fence。Coordinator可用一个private、process-local `PostAdoptionCompactionFailure(COMPACTED winner, exact cause)`穿过fence与manual settlement seam：manual waiter先得到exact snapshot/revision的`COMPACTED`，active caller随后重新抛出原cause并进入existing interruption/failure path，绝不返回可供runner ordinary fallback的`FAILED` result；idle caller只保留canonical `COMPACTED` truth。该carrier不持repository/continuity capability，不扩充durable vocabulary。只有status仍为RUNNING时才消费PostCompact BLOCK为active control；若已不再RUNNING，则把BLOCK/context与其他control降为historical diagnostic，SessionStart attempts=0且不得借Hook复活continuation，pending compact boundary留给未来actual ROOT attempt。这样即使final install后来因close/CAS/resource失败，Hook可能已产生external side effect也符合weak execution语义，不需要base-successor revoke/supersede机制。

Current coordinator在`HostCompactionRuntimeOwner.run_fenced()` operation内从adoption一直执行到final install，runner在`execute_active()`返回后再dispatch已经太晚。ROOT runner因此为每次active compaction调用传入private、call-local、root-only async `SessionStartCompactPort`；它闭包只持该exact ROOT run的cold-open once slot、`HookDispatchEnvelope` factory与context reservation port，不暴露runner/Host/repository back-reference。其调用签名闭合为：

```text
SessionStartCompactPort(
  PreparedCompactSessionStartFacts(
    exact CompactionAttemptToken,
    adopted snapshot/binding revision,
    target ROOT scope + turn,
    selected model identity,
    effective permission fact + exact precondition,
    inherited successor absolute deadline,
  )
) -> PreparedCompactSessionStart(PROCEED + reservation | BLOCK + reason)
```

这些facts只能由Coordinator在canonical adoption FULL、PostCompact settle、post-adoption status仍RUNNING后，从其exact adopted winner与`PreparedProviderDispatch.prepared_call/canonical_facts`签发；预先创建的runner closure不能猜adoption、model、permission或另取deadline。一个额外的narrow boundary port只允许Coordinator在adoption FULL时arm同一runner-owned cold-boundary slot；它不暴露runner back-reference。Call-local SessionStart port据此exact-consume绑定该adoption的pending boundary并签发`SessionStartAttemptToken`，构造`source=compact` public input、继承facts中的`successor_deadline`并消费`GateOutcome`。Final no-Hook/Hook sibling及continuity install必须exact-join同一model、permission precondition、adopted binding与attempt token；不join时retire reservation并走existing conflict/interruption，不重跑Hook。Child为两个port都传`None`。Coordinator不得在释放fence后补发event，也不得把port/facts保存到registry、resource或future run。

`ACTIVE_CONTINUATION_BLOCKED(reason)`统一承载active PostCompact BLOCK与active ROOT compact-SessionStart BLOCK；它由CompactionCoordinator在`CompactionOutcome(COMPACTED, ..., reason=COMPACTED_CONTINUATION_BLOCKED)`旁返回、ROOT/child runner唯一消费，不扩充`CompactionDisposition`、Hook outcome或durable event。Canonical adoption已经FULL，因此manual waiter必须看到`COMPACTED`而不是把成功快照谎报为FAILED；active run另行被停止。ROOT runner必须在generic provider/run failure trap之前专门消费该signal，调用现有`TurnAdmissionCoordinator.interrupt_turn(exact turn, HOOK_COMPACTION_BLOCKED)`并沿既有ACK-unknown/read-terminal确认。Child路径以专用`ChildCompactionContinuationBlocked`越过runner generic interruption与manager generic FAILED catch，由`_run_child`的前置catch调用existing joint settlement，使exact child turn与task都成为`INTERRUPTED / HOOK_COMPACTION_BLOCKED`，不能落成task `FAILED`。Auto/mid-turn caller看到existing interrupted run outcome。任何调用方都不能把signal降成`NOT_NEEDED`、`FAILED`或`successor_dispatch=None`后继续普通compile/reprepare。Startup/resume的`SessionStartBlocked`复用对应ROOT special interruption path，provider open=0。

ROOT SessionStart context只作为§0.5 final cold assembly中的optional `HOOK_CONTEXT` source。Final reassembly用同一post-adoption canonical read/tool surface/frozen non-Hook facts分别验证no-Hook与Hook sibling；Hook observation按normal placement参与root rebuild，不要求uninstalled dry message prefix。Compiler omission、Hook sibling invalid或不fit时使用valid no-Hook sibling。Known CAS conflict或owner replan只retire exact frozen reservation，不重跑PostCompact/SessionStart。任一路径至多调用一次existing continuity install/open；“one final”不限制existing uninstalled dry compile/structural trial次数，也不新增总attempt cap。Idle adoption不建立successor或context buffer；下一次actual ROOT attempt才运行SessionStart(compact)。Round 5B只调用本轮dispatcher与context owner，不建立compaction-private engine、parser、CAS或trust state。

### 8.6 Subagent

Current `_TaskStartMaterial`在task FULL时尚无child provider target或permission projection；这些字段不能由Hook adapter猜测。Host composition向Subagent manager注入一个narrow `SubagentLaunchPreparationPort`，而不是Host/runner back-reference或service locator。Scheduler在选择exact spawning permit并确认task start FULL后把task/start material交给该port；model owner投影current configured child model identity，repository permission owner读取canonical parent turn并冻结effective mode、snapshot/precondition，port返回immutable `PreparedSubagentLaunch`：

```text
exact task/start material + spawning permit
stable child turn id
configured child model identity
parent effective permission mode + exact snapshot/precondition
```

Current product没有per-child model override，child runner复用同一个Host model adapter；该configured identity是pre-open lifecycle projection。LAUNCHING admission owner接收同一launch carrier并让child turn admission exact-join其parent permission precondition；admitted child `run_accepted_turn`继续携带carrier/context，first target preparation exact-join configured model。不能到Hook后另读一个漂移值。Future若增加profile model override，必须先进入同一launch carrier。Carrier不是canonical row、permission snapshot副本或generic Hook DTO；port不暴露Host、repository或model adapter对象。

SubagentStart发生在task canonical FULL、scheduler已经选择且取得`PreparedSubagentLaunch`之后，但在child runtime install/first cold compile之前。它是`ContextOutcome`而不是gate：Hook不能撤销canonical task或释放别人的permit。Wire `model`/`permission_mode`只从launch carrier投影。Settled sync context先绑定`child-launch call-local` slot，只能由同一carrier的first child cold planning freeze；late async context遵循§6.6 same-child background规则。

Hook settle后、`_install_live_task`前采用两级仲裁，不能声称process-local manager lock能原子读取canonical database truth：

1. manager lock内只把同一exact launch permit从existing `SPAWNING` ownership原子claim为`LAUNCHING`，重验manager open、material unchanged与local stop/close intent；
2. 释放manager lock后，由turn-admission owner在**尚未注册live child task**时构造exact child turn candidate并调用`accept_subagent`，repository transaction exact-join canonical task仍ACTIVE/start FULL与parent permission precondition；
3. admission FULL后，manager才在同一permit下注册live child coroutine；它从`run_accepted_turn(exact child turn)`开始，携带launch context，并在first `prepare_target` exact-join configured model；canonical terminal/conflict winner则retire Hook context/permit，child runtime为0。

`stop_agent`/close必须竞争同一个permit：若它先claim，scheduler不得写child turn；若`LAUNCHING`先claim，stop不能并发走`require_absent_turn` dormant terminal path，而是标记cancel并等待/驱动该launch settlement，admission FULL后走existing live-child joint cancellation。这样不会出现task已USER_CANCELLED却随后emit ACTIVE，也不需要补偿事务。其他launch failure沿existing task failure settlement。Scheduler等待Hook时不得持Subagent manager lock；该attempt必须登记到dispatcher close cancellation。`SubagentManager.aclose()`把scheduler cancel/join纳入同一个Host absolute remaining deadline，不能在应用timeout前无界等待scheduler；task已经FULL且仍由stop/close先赢得permit时才走existing dormant terminal fallback，不因Hook悬挂阻止SessionEnd/Host close。

这要求把current `run_subagent_turn`中“child turn admission + provider loop”的组合入口hard-cut拆成`prepare/admit_subagent_turn`与existing `run_accepted_turn`两段；不能先`create_task(run_subagent_turn)`、再声称admission FULL前没有live child。拆分只移动existing turn-admission owner，不新增canonical state或第二套runner。Admitted child coroutine的single wrapper必须保留current组合入口`finally`拥有的exact child continuity scope与memory-context scope cleanup；无论terminal、cancel、launch error或Hook BLOCK都discard一次，不能因拆入口新造process-local scope leak。

Subagent completion owner为每个task持有唯一process-local `continuation_already_used` boolean，并在每次EXPLICIT/INFERRED completion candidate冻结后、commit前dispatch：首次input `stop_hook_active=false`；continuation后下一次natural stop仍dispatch，但`stop_hook_active=true`。

- INFERRED `CONTINUE_ONCE`：该assistant candidate按existing canonical path settle为`complete_turn=false`，释放completion permit，不提交SubagentResult，task保持ACTIVE，context进入下一child compile。
- EXPLICIT `CONTINUE_ONCE`：不提交SubagentResult/composite；settle ordinary“result未接纳，请继续工作”ToolResult，释放completion permit，task保持ACTIVE，然后运行PostToolUse。
- `TERMINALIZE`：按existing inferred/explicit terminal path提交exact assistant/result/composite。
- once guard已使用时，所有continuation request只诊断并强制TERMINALIZE；event仍运行以保持观察语义。不同handlers任意合法terminalize优先。

`continuation_already_used`不是在Hook返回时翻转：INFERRED只在同一assistant以`complete_turn=false` canonical FULL且owner仍live后原子标记；EXPLICIT只在ordinary rejection ToolResult FULL且owner仍live后原子标记。Hook仅提出请求、settlement failure/ACK-unknown未确认、close/cancel或stale均不消耗guard，也不得复活task；ACK-unknown只确认同一candidate，不能重跑Hook。Continuation不会增加child turn/tool/model-call总cap；child可继续任意正常工作。Crash可造成Hook duplicate/loss，不增加completion receipt或replay。

### 8.7 ROOT Stop

Stop只在ROOT provider返回natural final public assistant candidate、该candidate已经frozen且确认无tool calls、但`PreparedAssistantMessageSettlement`尚未canonical commit时产生。Accepted/`complete_turn=true`之后再运行Hook是非法late producer。

每次`run_accepted_turn(exact ROOT turn)`建立并拥有自己的process-local `continuation_already_used` boolean；它不是Host-session singleton，也不能跨turn继承。Exact run terminal/interruption时retire该guard；Host crash后允许丢失。该turn内：

- first `TERMINALIZE`：只令同一assistant candidate请求`complete_turn=true`；canonical assistant settlement仍是唯一terminal authority；
- first `CONTINUE_ONCE`：同一assistant candidate settle为`complete_turn=false`，reason/context进入exact ROOT continuation reservation，run-loop不经过human prompt queue直接开始下一provider cycle；
- second natural stop仍dispatch并令`stop_hook_active=true`，但任何continuation request只诊断并强制terminalize。

Hook等待期间可能到达新的steer。Repository在assistant settlement transaction中继续重验existing pending steer；即使Hook请求TERMINALIZE，若accepted assistant为FULL但`turn_completed=false`，同一assistant仍合法归属于旧cut，runner按ordinary steer continuation继续。该race不消耗Hook once guard、不安装Stop continuation context，也不能吞掉steer。

CONTINUE_ONCE只有在同一assistant以`complete_turn=false` canonical FULL且owner仍live后才原子标记`continuation_already_used`并安装exact context reservation。Hook仅提出请求、settlement failure、close/cancel或ACK-unknown未确认均不消费guard。Assistant settlement ACK-unknown只走existing exact confirmation；确认同一candidate FULL后不得重跑Stop。User cancel、Host close、provider/transport failure、resource interruption与tool-driven interruption都不是natural stop，不dispatch该event。Close/cancel在Hook等待期间获胜；fail-open不能把已取消run继续起来。

### 8.8 SessionEnd close proof

Host close按§4.3停止business admission后，先让所有已开始producer完成或被existing owner取消；它们的PostTool/SubagentStop等ordinary events仍可进入lane。只有这些producer quiesce后才fence ordinary lane并运行SessionEnd。SessionEnd output绝不进入pending context；其handler即使exit 2、`continue:false`或输出additional context也只形成invalid/diagnostic，不能延长或阻止physical close。

### 8.9 本轮必须一并收口的current-code缺陷

下面三项不是旧上位文档推导出的抽象担忧，而是本文窄探针已经在current production code确认的真实竞态/owner缺口。Hook等待、新增BLOCK效果与既有`NOT_NEEDED`分支会扩大它们的窗口；Round 9.2 implementation diff必须按本节与§8.5–§8.6一起hard-cut，不能把它们留作后续cleanup：

1. `KernelSubagentManager._start_available_tasks_worker()`从claim `_spawning`、task-start CAS到canonical FULL后install live child的整个窗口，目前没有与`stop_agent`/close共享一个launch permit；FULL后也只重验`_closed`便调用`_install_live_task()`。并发`stop_agent`可在`_tasks`尚无live item时走`require_absent_turn=True`并把同一canonical task置为`CANCELLED`，随后scheduler仍可能安装child并emit `ACTIVE`；current `_install_live_task()`即使观察到closed也仍可能store `_tasks`并emit ACTIVE。§8.6的single permit `SPAWNING → LAUNCHING`仲裁、repository exact join与stop/close等待规则必须消除这个“terminal winner后仍launch”的现有bug；closed/terminal winner取消新task后绝不能store live item或emit ACTIVE。
2. Current `KernelSubagentManager.aclose()`在应用`timeout_seconds`前会无界shield等待batch admissions、mailbox consumptions和scheduler。SessionStart/SubagentStart等长Hook等待会使Host close的frozen absolute deadline直到这些wait结束后才开始生效。Close必须从第一项join起使用Host传入的absolute remaining deadline，先传播cancel到dispatcher/Hook process与scheduler，并按§4.3在deadline选择logical timeout/terminal outcome。Deadline后对仍拥有的process/provider/tool/child physical resource继续terminate/kill/drain、绝不detach，是existing no-detach contract，不是本轮要删除的bug；错误仅在于不能等到无界pre-timeout wait之后才开始cancel或选择logical outcome。
3. Current runner late-threshold branch在`dispatch_crosses_threshold=true`后先关闭ordinary dispatch，再调用`execute_active()`，随后不检查outcome便把`successor_dispatch`（可能为null）赋回并无条件`continue`。Coordinator对`BELOW_TRIGGER`或`NO_COMPACTABLE_PREFIX`合法返回`NOT_NEEDED + successor=None`，且正确地不记录automatic failure；因此同一canonical cut可无限重复“compile → late threshold → NOT_NEEDED”，provider open始终为0。Caller必须按closed compaction outcome分支：有successor的`COMPACTED`才handoff；`NOT_NEEDED`在同一planning cycle标记exact `CompactionAttemptToken`已判定、重建/保留ordinary no-compaction dispatch并继续provider open；`FAILED`沿existing automatic-failure disposition。不得用新增retry-count cap掩盖该liveness bug，future provider cycle仍可签发new token重新评估。

这些修复不新增relation、event、receipt、generation或lifetime cap；它们只让现有canonical task/turn authority与process-local launch/close/compaction owner在竞态下保持一致。Current `run_subagent_turn()`的admission+provider-loop组合入口本身在无Hook产品下没有独立失败；§8.6要求拆成`prepare/admit_subagent_turn → run_accepted_turn`是SubagentStart新seam所需的mandatory topology hard-cut，不应伪称第四项current bug，也不得因此保留old/new双入口。

---

## 9. Hook input与provider context

### 9.1 Eleven typed stdin variants

不存在一个把所有optional字段塞在一起的mega wire DTO。Lifecycle owner把11个private-construction typed public input variant之一放入§3.1 `HookDispatchEnvelope[T]`；command stdin serializer只能看`T`，绝不序列化internal scope、causal carrier、deadline或cancellation signal。11个public variant都包含：

```text
session_id: string
transcript_path: null
cwd: string
hook_event_name: exact external literal
model: string
```

`transcript_path=null`是Pulsara不暴露canonical/private transcript file的有意差异。`model`优先由event owner投影已经selected/frozen的exact provider target；tool/stop等使用current attempt。SubagentStart尚未prepare provider target，使用§8.6 `PreparedSubagentLaunch`中Host configured child model identity并要求later target exact-join。只有确实没有selected target/carrier的ROOT lifecycle/ingress event才fallback到Host cold composition冻结的ROOT configured model identity。不得因为transport尚未open就把已有child/compaction carrier改成另一模型。

Public stdin字段先保留其上游产品自己的validated bound：例如UserPromptSubmit的`prompt`仍可达到current ingress允许的大小，tool input/response仍受其既有tool/result carrier bound；不能把§2.3针对untrusted config/output structure的64 KiB scalar上限反向施加到合法lifecycle input。Serialized stdin整体仍受§6.5 `MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES`。Output JSON的node/depth/ordinary scalar受§2.3约束，但`additionalContext`正文是明确的最多1 MiB capture exception，之后再应用per-handler token threshold与compiler source/aggregate bound。任何event-specific上游bound变化都由其owner冻结，generic Hook adapter不另加更小的隐藏cap。

`permission_mode`仅出现在`SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PermissionRequest`、`PostToolUse`、`SubagentStart`、`SubagentStop`、`Stop`；`SessionEnd`、`PreCompact`、`PostCompact`必须omit。Closed projection为：

| Pulsara effective permission | external value |
|---|---|
| `ASK_PERMISSIONS` | `default` |
| `ACCEPT_EDITS` | `acceptEdits` |
| `READ_ONLY`且绑定active Plan workflow | `plan` |
| 其他`READ_ONLY` | `dontAsk` |
| `BYPASS_PERMISSIONS` | `bypassPermissions` |

Event-specific required fields：

| Event | required fields beyond common |
|---|---|
| SessionStart | `source: startup|resume|compact` |
| SessionEnd | `reason: other` |
| UserPromptSubmit | `turn_id:string`, `prompt:string`, `permission_mode` |
| PreToolUse | `turn_id`, `tool_name`, `tool_use_id`, `tool_input:any JSON`, `permission_mode`; conditional Pulsara extension `pulsara_tool_name` |
| PermissionRequest | `turn_id`, `tool_name`, `tool_input:any JSON`, `permission_mode`; conditional `pulsara_tool_name` |
| PostToolUse | `turn_id`, `tool_name`, `tool_use_id`, `tool_input`, `tool_response:public JSON`, `permission_mode`; conditional `pulsara_tool_name` |
| PreCompact | `turn_id`, `trigger:manual|auto` |
| PostCompact | `turn_id`, `trigger:manual|auto` |
| SubagentStart | `turn_id`, `agent_id`, `agent_type`, `permission_mode` |
| SubagentStop | `turn_id`, `agent_id`, `agent_type`, `agent_transcript_path:null`, `stop_hook_active:bool`, `last_assistant_message:string|null`, `permission_mode` |
| Stop | `turn_id`, `stop_hook_active:bool`, `last_assistant_message:string|null`, `permission_mode` |

`tool_name`是§5.2 compatibility primary；matcher仍使用canonical subject/aliases。`pulsara_tool_name`只在Pulsara exposed/outer name与compatibility primary不同（尤其meta MCP）时出现，值是exact outer name；相同时必须omit，不能由call site任选。Successful meta route的`tool_input`是resolved underlying arguments；pre-resolution failure的Post以outer identity和original meta input投影，resolved-then-invalid failure以underlying identity和rejected underlying input投影。Inputs不得携带hidden reasoning、private replay、raw artifact、PULSARA_API_KEY、package install/config path或完整internal DTO repr。Plugin provenance只留在trust/inspection/diagnostic，不能通过generic stdin extension map暴露。

PreCompact/PostCompact的`turn_id`始终来自current compaction owner已经冻结的exact `target_turn_id`，包括idle/manual target；不得改用“当前active turn”、latest arbitrary turn或空值。`last_assistant_message`在owner没有合法public assistant正文时为null，不能读取private replay或伪造empty assistant。

UserPromptSubmit的`turn_id`来自§8.3 direct/queued各自的pure identity helper及exact future admission candidate；queued path不能在queue sequence存在前调用full `build_queued_root_turn_admission`。Its `permission_mode`来自`PreparedIngressPermissionProjection.effective`，later write必须exact-join。SubagentStart/SubagentStop的`turn_id`是existing stable child turn id，`agent_id`是canonical task id；不得投影parent ROOT turn id、batch id或scheduler attempt id。

Subagent matcher/stdin的`agent_type`固定投影为canonical task的`SubagentProfileKind.value`：`general_worker | research_worker | review_worker | verification_worker | synthesizer`。它绝不能取display-only `display_role`、用户label或scheduler说明文字；SubagentStart与同一task后续SubagentStop必须使用同一个profile value。

本文所谓Codex-compatible是一个明确subset/profile：11个event名、command config shape、matcher、上述stdin与支持的exit/JSON controls按公开协议对齐。Pulsara的deliberate differences必须在CLI doctor显示：无`clear` producer；`transcript_path`固定null；不支持prompt/agent/http/mcp_tool handler、output spill、argument/ToolResult rewrite或suppression；filesystem `edit_file/write_file`以`tool_name=apply_patch`和aliases兼容matcher，但`tool_input`保持各自真实Pulsara argument schema而不是伪造`command`；只有`terminal`映射`Bash`，current independent `terminal_process/terminal_monitor`不伪装成Codex `write_stdin` transport或delayed Bash Post；PreTool allow/ask不支持，allow只在PermissionRequest消费；PostTool exit 2不替换result；PermissionRequest的exit 2、malformed reserved output或unsupported control按本文统一fail-open为`ABSTAIN`并回到原human flow，不采用其他实现的implicit fail-closed deny；async output出现任何control-vocabulary key时整项invalid；context进入untrusted user-role suffix而非developer/SYSTEM；`pulsara_tool_name`是唯一tool input extension。不得把这些差异描述成已兼容能力。

### 9.2 Source-neutral provider source

```text
ContextSourceKind.HOOK_CONTEXT
contract    pulsara.hook-context.v1
channel     RUNTIME_OBSERVATION
trust       UNTRUSTED_OBSERVATION
budget      IMPORTANT
placement   68
degradation 60
lifecycle   ONE_SHOT
variants    FULL | COMPACT
```

`degradation=60`是closed compiler policy：在current `IMPORTANT` sources中它使advisory Hook context先于Skill/MCP/Memory等更低priority值被牺牲，不能由coding agent临时猜测。每次source collection都必须为`HOOK_CONTEXT`提供exact candidate或typed absent fact；没有eligible reservation的no-Hook sibling使用existing `EXPLICIT_EMPTY` absence kind，它只满足all-source contract validation，不是semantic contribution，也不制造model message。

```text
FrozenHookContextContribution
  exact FrozenHookDefinition reference
  exact non-capability HookContextOccurrenceRef
  source ordinal（source-local ordinal来自definition）
  complete context text

FrozenHookContextBatch
  exact ROOT/child scope
  ordered FrozenHookContextContributions
```

Contribution保存source builder真正需要的definition与settled occurrence reference，不要求renderer从展示正文反推identity；definition已经唯一拥有event type、source identity、display label、source-local ordinal与`additionalContextLimit`，contribution不重复这些字段。`HookContextOccurrenceRef`只能在owner仍live且output settlement成功后，从§3.1 `HookDispatchCausalRef`投影为`HookDispatchScopeRef + event_dispatch_ordinal + existing stable IDs/tokens/nonces/revisions`；它不保留whole definition view、owner DTO、borrow/permit、writer guard、repository handle、raw arguments/result、assistant content/blocks或replay。没有合法model consumer的event不构造该ref。每个entry对模型只render用户可理解的source label、event label与context正文；`statusMessage`、process status、stderr、trust/path proof、occurrence ref和internal ordinals不进入model body。

`HookContextOwner`先在target safe point用existing compiler estimator应用closed threshold policy，再以剩余完整contributions构造atomic FULL/COMPACT variants。`hookSpecificOutput.additionalContext`/plain informational contribution使用definition的`additionalContextLimit`；Stop/SubagentStop continuation reason不受该field影响，固定使用Codex-compatible sealed default 2,500 tokens并whole-entry omit。Gate/permission/terminal diagnostic reason不是model contribution，不进入这套threshold。Contribution kind可由event与accepted output branch机械推导，不重复存一个enum/limit字段。

FULL保留source/event label与正文；COMPACT仅压缩label/包装，绝不head/tail截断或临时删除batch中的单项。Compiler只能选择一个完整variant或omit整个`HOOK_CONTEXT` source；不能在selection中再次摘除entry。完整value直接传递，不添加batch/entry fingerprint。若不fit则omit整个source并记录process-local diagnostic，advisory Hook context不得形成conversation resource boundary。Physical buffer丢弃只进入diagnostic；不把omitted count render给模型或保存在batch DTO，因此也不会制造额外provider-visible semantic/occurrence字段。

Compiler把`HOOK_CONTEXT`投影成**user-role message in the append-only suffix**。它按placement `68`与该次新suffix中的其他runtime observations/delta确定性排序，不承诺自己是absolute last message；但绝不插入或改写已经installed的predecessor messages。Cold/compaction root rebuild时没有installed dry predecessor，按§0.5参与唯一final root assembly。Body明确说明它是untrusted external command output，不能授予permission、改变Tool/MCP availability、覆盖current active user request/canonical ToolResult或修改SYSTEM。它是one-shot、same-scope、advisory/omittable；不成为developer/system message。

### 9.3 Causal identity、reservation与CAS

不为`FrozenHookContextBatch`或contribution增加fingerprint字段。Settled `HookContextOccurrenceRef`只保存上节允许的non-capability projection；Source builder再从immutable definition取得exact source identity与`source_local_definition_ordinal`，并从contribution取得`source_ordinal`。这些值各自只有一个owner slot，不在occurrence或正文中重复复制。已有installed epoch内发生的tool/background/continuation occurrence包含epoch nonce与command/turn、ToolResult、subagent completion candidate或ROOT assistant candidate的stable IDs。Pre-epoch prompt使用direct candidate或queue item IDs；startup/resume/compact SessionStart使用§8.2 `SessionStartAttemptToken`；Post-adoption compaction使用exact adoption/attempt token。正文相同但由这些owner values组合出的causal occurrence不同，必须形成不同suffix。

在真实provider-source边界，existing source builder以domain-versioned framing从该完整causal tuple就地计算ONE_SHOT occurrence identity；它不把digest回写DTO、不建立digest→object map，也不把正文hash当owner proof。

唯一process-local Hook context component持有分离typed slots：

```text
prompt-bound direct/queued
Host-session + workspace + ROOT background
exact child-task background
SessionStart / continuation call-local
SubagentStart child-launch call-local
tool-feedback call-local
```

Pre-child-epoch SubagentStart occurrence由exact `PreparedSubagentLaunch + task-start`投影non-capability IDs，不要求尚不存在的child epoch nonce。其sync reservation只能由LAUNCHING settlement携至admitted `run_accepted_turn`并由first child cold planning freeze；launch/turn admission/target exact-join失败即retire。Event owner返回后才完成的async contribution转入same-child background slot；它不能回填first compile或投到parent。

一次provider planning只freeze当时eligible entries形成exact reservation object；freeze之后到达的background entries留给future planning。Hook context与同一provider continuity candidate一起CAS安装。CAS success、compiler omission、known CAS conflict或owner-abandoned planning都只retire该frozen reservation；不得put-back、replay或重跑已经settled的Hook。Owner若需要replan，只使用尚未freeze的新entries或无Hook context的current canonical truth。

Call-local/run-bound slots在exact run terminal或Host close时清空。Child background在exact task terminal/Host close时清空并拒绝late completion。ROOT background的delivery scope是同一Host-session + exact workspace + ROOT，因此origin turn terminal后仍可在下一accepted user turn的合法provider safe point交付；只有Host close/workspace replacement才清空。Prompt BLOCK/admission failure、PreCompact BLOCK、terminal Stop/SubagentStop、idle PostCompact、SessionEnd及其他明确没有本scope future consumer的**synchronous/call-local** context立即retire；不能误删仍符合ROOT background delivery contract的late async observation，也不能制造ghost child buffer。

### 9.4 Bounds、scope与continuity

Background pending buffer把**origin identity**与**delivery scope**分开：每项的`HookContextOccurrenceRef`保留origin turn/tool/task IDs；ROOT delivery只绑定Host-session + exact workspace + ROOT，可跨ROOT turn到下一accepted user turn，但不跨Host/workspace或进入child；child delivery绑定exact task且不跨task terminal。它不从public strings重建scope。Buffer复用existing maximum single-source variant bytes，只保留能完整容纳的最新entries并对drop产生process-local diagnostic；不增加entry-count cap或rendered omitted counter。Candidate-bound prompt slot只在ordinary admission FULL时转成exact run-bound slot。Host crash允许丢失。

同epoch安装始终保持SYSTEM/tools byte-identical、messages suffix-only；compact SessionStart只进入既有successor root build。Reload、trust change和Hook执行都不创建rebase边界。Compiler预算不足可安全省略context而继续provider call；Reload前开始的old immutable attempt可在scope仍live时settle并贡献future context，但不改变它capture的view，也不让reload自己的PostToolUse使用new view。

---

## 10. Owners与依赖

### 10.1 Owners

| Owner | 拥有 | 不拥有 |
|---|---|---|
| `LocalHookSourceProvider` + single config parser | USER/WORKSPACE exact source discovery/normalization/provenance | process execution、Plugin package |
| `PluginHookContributionAdapter` | fixed `dev.pulsara/hooks/hooks.json` location、Plugin source/trust identity、declaration environment、generic lifetime anchor | trust、matcher、executor、context |
| `HookTrustStore` | source enablement与exact trust digest | definition object registry、execution history |
| `KernelHookDispatcher` | current immutable view、matcher、attempt scheduling、single output parser、typed aggregation | canonical rows、Plugin parsing、Tool/permission authority |
| `HookCommandExecutor` | subprocess/process-group、stdin/stdout/stderr、deadline | event semantics、trust persistence |
| one `HookContextOwner` | exact-scope buffers/reservations、one `HOOK_CONTEXT` builder | canonical truth、provider continuity CAS |
| existing compiler/continuity | model budget selection、user-role suffix、provider candidate CAS | Hook process/trust/replay |
| existing lifecycle owners | typed input construction、owner liveness revalidation、final operation settlement | parser/executor/trust internals |

Event-specific production code只能做两件事：从owner-held exact carrier构造对应typed input；消费§0.4的一种narrow outcome。不得为11个event各建parser、executor、trust store、context source、decision DTO或状态机；不得让generic Hook取得Host/repository/ToolRuntime/permission/subagent coordinator。

### 10.2 Dependency direction

```text
hooks/contracts.py       imports no plugins/repository/provider adapter
hooks/source.py          may import local filesystem/config primitives
hooks/dispatcher.py      may import contracts/matcher/executor/output_parser/context ports
hooks/*                  imports no plugins/extensions/repository/ToolRuntime/lifecycle owner
conversation_kernel/extensions.py imports no HookEventType/dispatcher
plugins/hook_adapter.py  (Round 9.3) may import Hook definition factory/contracts
hooks/*                  must never import plugins/*
compiler/continuity      receives pure HOOK_CONTEXT source values only
```

Generic Hook subsystem不能扫描Plugin package root，也不能知道MCP supervisor、Skill provider或subagent manifest。它不得复用`OperationalHookType`、`KernelExtensionHost`或extension delivery；extension plane也不得反向发送本轮11项lifecycle event。

Round 9.3只把package declaration/provenance/environment overlay规范化成本文既有`FrozenHookSourceProvenance + FrozenHookDefinition`，并通过§4.3唯一publication lane替换Plugin slice。它没有建立第二套trust、matcher、dispatcher、executor、output parser、context owner/source或Hook event enum；USER/WORKSPACE路径继续独立成立。

Round 9.3已经ACTIVATED；其single-path adapter遵守本文收口：无head/tail context preview、无`hooks/parser.py`旧路径、无Plugin provenance stdin arm、无`statusMessage` model context、无dispatcher私有pending buffer，也无第二publication lane。历史冲突条款不得复活为compatibility path。

---

## 11. Implementation slices

### R9.2-0：Contracts与local sources

- 11-event closed vocabulary、11个typed input与5-family outcome algebra；
- single config parser、complete scan/bounds与Codex compatibility profile；
- USER/WORKSPACE complete discovery；
- deterministic append ordering；
- source provenance无object cycle，carried values无fingerprint。

### R9.2-A：Trust与reload

- generic source trust state/digest；
- list/inspect/trust/revoke/enable/disable/doctor CLI；
- fixed `reload_hooks` Builtin与Host method；
- one future-view publication lane、source-slice merge与old-attempt drain；
- malformed current source撤销future runnable commands。

### R9.2-B：Executor与output

- matcher/aliases；
- sync/background scheduler；
- process groups/inherited deadlines/immediate pipe drain/bounds；
- PULSARA_API_KEY exact scrub；
- single JSON/plain output parser、exit-2 matrix与typed aggregation。

### R9.2-C：Context与owner carriers

- one exact-scope `HookContextOwner`与one `HOOK_CONTEXT` source；
- exact predecessor `FrozenHookDefinitionView` object-ref plumbing、closed 12-arm `HookDispatchCausalRef`与settled non-capability `HookContextOccurrenceRef`；11个event中`UserPromptSubmitEvent`因为current direct/queued producer carrier不同而拆成两个causal arms；
- causal occurrence identity、reservation settlement、budget omission；
- direct/queued ingress identities、`PreparedResolvedToolInvocation`、`PreparedPermissionRequest`、`PreparedPlanControlInvocation`、`PreparedCompactSessionStartFacts`与`PreparedSubagentLaunch`；
- one process-local `AcceptedCanonicalToolResultSettlement`供ordinary、Plan immediate/delayed与subagent composite owner共用；
- Round 7.1 pure public ToolResult projection helper，包括从compiler移出的Plan storage-to-public projection。

### R9.2-D：Lifecycle integration

- Host direct/queued `NewTurnHookGate`与SessionEnd terminal lane；
- runner ROOT SessionStart/Stop；
- tool/permission/PostTool；
- compaction one-final-successor path；
- subagent start/stop exact settlements；
- §8.9三项current-code launch/stop、close-deadline与late-threshold compaction liveness缺陷在同一hard cut收口，不留compatibility path。

同一implementation diff必须同步hard-cut Round 5B active spec与其architecture tests中“FULL后actual successor的全部messages/source heads/wire/candidate必须逐项等于pre-write dry”的旧断言，改为§0.5规定的新invariants：dry只证明no-Hook cold base；post-adoption no-Hook/Hook siblings来自同一exact current read、tool surface与frozen non-Hook facts；两者`SYSTEM/tools` exact equal，Hook是唯一新增source并重新过bounds；final continuity install/open至多一次。不得保留old/new双断言、伪造ephemeral installed predecessor或compatibility flag。

### R9.2-E：Activation

- targeted/full/PostgreSQL/architecture tests；
- real provider + real Hook dogfood；
- specs/Gap Index/README/evidence；
- no per-file/document/evidence SHA gates。

---

## 12. Production modification map

### 12.1 New package

```text
src/pulsara_agent/hooks/
  contracts.py
  config_parser.py
  source.py
  trust.py
  matcher.py
  executor.py
  output_parser.py
  dispatcher.py
  context.py
```

### 12.2 Existing integration

- `conversation_kernel/host.py`：cold discovery、single future-view publication lane、exact predecessor-view object-ref plumbing、direct/queued `NewTurnHookGate`、SessionEnd close order；
- `conversation_kernel/runner.py`：ROOT SessionStart/Stop run-loop integration、call-local `SessionStartCompactPort` producer、Plan batch/child launch carrier plumbing与compaction BLOCK special branches；
- `conversation_kernel/tool_contracts.py`：定义provider/repository-neutral的`AcceptedCanonicalToolResultSettlement`，不引用Hook或repository authority；
- `conversation_kernel/tool_execution.py`：ordinary PreTool/Permission/PostTool orchestration；在canonical/effect FULL后产生shared accepted ToolResult settlement；
- `conversation_kernel/tool_runtime.py`：prepared resolution、prepared permission handoff、fixed ROOT `reload_hooks` invoke path；
- `conversation_kernel/plan_runtime.py`、existing Plan interaction owner：Plan-control selected-call PreTool gate；消费ordered immediate/delayed shared ToolResult settlements后Post；
- `conversation_kernel/_repository/plans.py`：保留canonical Plan/result atomic transaction；`AcceptedPlanToolBatch`/`AcceptedPlanResolution`返回ordered `AcceptedCanonicalToolResultSettlement`而不只返回result-entry ID，绝不直接dispatch Hook；
- `conversation_kernel/compaction/coordinator.py`：Pre/PostCompact、active/idle adoption及fence内消费ROOT-only compact-start port；
- `conversation_kernel/provider_dispatch.py`、existing input continuity owner：uninstalled base/final assembly与至多一次final continuity install/open；
- `conversation_kernel/subagent.py`：`SubagentLaunchPreparationPort`消费、SubagentStart/Stop、launch liveness revalidation与once guard settlements；
- `conversation_kernel/direct_model.py`及现有model-port protocol：只向launch/compact preparation port投影configured/selected model identity，不暴露adapter；
- `conversation_kernel/_repository/subagents.py`及turn-admission plumbing：parent permission projection与child exact-join；
- `primitives/tool_result_projection.py`、`model_input/lowering.py`：one provider-neutral ToolResult projection，Plan storage-only body的public变换不再compiler-private；
- `conversation_kernel/context_sources.py`、`model_input/`：one `HOOK_CONTEXT` builder、estimator与user-role suffix；
- `capability/builtin_catalog.py`：fixed ROOT `reload_hooks` descriptor；
- CLI：Hook review/trust commands。

不得新增migration、Protocol event kind、repository relation、Mixin、runner back-reference、service locator、legacy alias或Plugin-private Hook path。`conversation_kernel/extensions.py`不参与本轮。

---

## 13. Failure matrix

| Failure | Hook outcome | Product owner outcome |
|---|---|---|
| config absent | COMPLETE empty | normal continuation |
| malformed/overbound/raced source | source UNAVAILABLE；future handlers none | normal continuation |
| untrusted/modified/disabled | visible diagnostic；not runnable | normal continuation |
| actual command variant contains active API-key value | exact handler unavailable；no spawn | sibling handlers/owner按normal path |
| reload Host close/scope invalidation | publication mutex内不产生writer predecessor conflict；unpublished scan discarded | no lost update；current close rules |
| matcher invalid | exact group unavailable | other handlers continue |
| serialized Hook stdin exceeds aggregate bound | exact handler `HOOK_STDIN_BOUND_EXCEEDED`；no spawn/no control；never truncate | owner若仍live按原路径；否则existing stale/cancel outcome |
| slot queue或attempt超过effective owner deadline | cancel/terminate exact attempt | owner若仍live才fail-open；否则existing deadline/cancel outcome |
| spawn/timeout/ordinary nonzero/invalid output | typed diagnostic，no control | owner若仍live按原路径；不能revive stale operation |
| stdout/stderr overflow | kill/drain exact process，no control | same liveness rule |
| exit 2 PreTool/UserPrompt | exact BLOCK | no tool attempt / no ingress writes |
| exit 2 PermissionRequest | ordinary handler failure；PermissionOutcome保持ABSTAIN | original human confirmation flow；不是ALLOW或DENY |
| exit 2 Stop/SubagentStop | request CONTINUE_ONCE | once guard决定continue或terminalize |
| exit 2 PostToolUse/other | unsupported/failure | original ToolResult/owner path不变 |
| successful explicit deny/block | closed control | original owner executes typed denial path |
| `updatedInput`/result rewrite/suppression | exact handler invalid；no partial contribution | original arguments至多执行一次；不制造synthetic rejection |
| background control output | exact handler output invalid | no context/control partial contribution |
| Permission ALLOW after cancel/stale | late diagnostic/drop | request不能复活 |
| PreCompact BLOCK | exact compaction abort | trigger-only admission/projection/read及已FULL manual command row可存在；snapshot candidate/write、summary request/call、adoption/install/open=0；auto回同轮ordinary compile且不计engine failure |
| PostCompact BLOCK | adoption保持FULL；exact active continuation interrupted | final successor install/open=0 |
| SessionStart(compact) BLOCK | adoption保持FULL；exact ROOT continuation interrupted | final sibling/CAS/install/open=0 |
| SessionStart(startup/resume) BLOCK | no adoption exists；exact ROOT open attempt interrupted | provider compile/install/open=0 |
| context threshold/compiler omission | exact entries或batch retired | provider call继续，不形成resource boundary |
| context continuity CAS conflict/abandoned plan | frozen reservation retired | no put-back/replay/Hook rerun |
| branch with no legal delivery consumer（SessionEnd、idle/historical PostCompact、terminal call-local Stop/SubagentStop） | diagnostic/drop | no ghost buffer；不影响合法ROOT async background的next-turn delivery |
| Host crash after Hook side effect | duplicate/loss allowed | no receipt/replay |
| SessionEnd failure/timeout | diagnostic only；bounded drain | Host按remaining close deadline关闭 |

---

## 14. Test plan

### 14.1 Discovery/trust

- USER only、WORKSPACE only、combined scope、transient USER only；
- absent vs malformed/UNAVAILABLE；
- symlink/nonregular/race/UTF-8/bounds；FIFO在无writer时也必须通过nonblocking final open立即成为`UNAVAILABLE`；same-inode/same-size in-place rewrite必须因`mtime_ns/ctime_ns`变化成为`DISCOVERY_RACED`；expired absolute deadline只使exact source UNAVAILABLE；
- cold Host scan在worker thread继承exact Host-open deadline且只discover一次，dispatcher只接收该frozen view；
- append-not-override order与duplicate-looking handlers；
- root/event/group/handler unknown-field recovery unit与known unsupported handler type；
- `description`/`trusted_at`确有list/inspect/doctor consumer；
- normalized-equivalent JSON保持trust；semantic change变MODIFIED；
- trust expected digest stale拒绝；
- no digest->object map；
- project file未trust不执行。
- command引用的script bytes变化不伪造definition MODIFIED；inspection明确说明trust不证明transitive code integrity；

### 14.2 Reload/prefix

- fixed descriptor在所有epoch存在；non-BYPASS attempt=0；
- new view只从下一event生效；reload自己的Post使用old view；
- reload Pre前capture的exact predecessor view object贯穿同一prepared invocation的Pre/Permission/Post，Post admission后drop ordinary ref；无lease DTO/ref-count protocol/view-id registry/generation，new view不能自触发reload Post；
- deleted config形成empty replacement；malformed replacement撤销future handlers；
- running old attempt可settle且view随后释放；
- concurrent local reload与模拟Plugin publication通过同一mutex串行；每个writer从取得mutex后的current predecessor重建affected joins且不会lost-update；
- reload scan与Host close race证明无Host-lock/publication-mutex ABBA；close只discard unpublished scan；
- no generation/view fingerprint/registry seal；
- Chat/Responses均证明same epoch SYSTEM/tools exact、messages suffix-only。

### 14.3 Executor

- sync全量admission、concurrent slot queue与definition-order aggregation；不assert OS start/finish order；
- background safe point不等in-flight，completed entries按event/source/source-local-definition ordinal排序；
- deadline从dispatch开始并取parent minimum；extra handlers queue不reject或刷新timeout；
- stdout/stderr immediate concurrent drain、overflow/exit/timeout/cancel单owner仲裁；
- process-group terminate/grace/kill/EOF drain/join；
- stdin/capture/node/depth/scalar bounds；
- exact PULSARA_API_KEY覆盖command拒绝、environment/stdin/cross-chunk output/exception且其他真实内容可见；default marker与secret碰撞时final bytes仍无secret；attempt期间key rotation、late background/context/provider sink同时scrub所有captured/current values；environment build后、spawn前的确定性rotation必须由最终command/environment/stdin postcondition拒绝且physical process=0；unset/empty不误匹配；
- no total handler/session/history cap。

### 14.4 Controls

- each of 11 events happy path；
- 11个typed stdin逐字段presence/type、permission mapping、`clear` no-producer；
- internal `HookDispatchScopeRef`/causal carrier/deadline绝不出stdin；`IngressHookReservationKey`只比较preflight前已有facts；direct/queued turn/entry/revision/permission-snapshot四个pure IDs与later full admission exact equal；prompt effective permission/Plan cut exact-join且stale write=0；child model/turn/task projection不误用ROOT parent；
- `HookDispatchCausalRef`的12个arms逐项可从对应current carrier构造；11个event与11个public stdin variants保持不变，只有`UserPromptSubmitEvent`内部按direct/queued拆成两个causal arms；PreTool exact使用original model-call carrier中的`assistant_entry_id + tool_call_id + resolved route identity`，不得发明尚不存在的attempt id；attempt/context/background从不保留surface borrow、writer guard、full PreparedProviderDispatch/AssistantSettlement、raw result/replay或whole view object；
- matcher match-all、regex search、invalid linear-time syntax三分支；证明不存在exact-alternative旁路；
- current `terminal`按Bash匹配；`terminal_process/terminal_monitor`按exact name各自产生一组events且不回挂delayed Bash Post；`edit_file/write_file`分别按`apply_patch + Edit/Write` aliases匹配且stdin带exact `pulsara_tool_name`/真实args；`spawn_agent`才匹配Agent，`create_agent_tasks`不匹配；
- exit 0 empty/JSON/plain与exit 2逐event完整矩阵；
- deny/block/allow/abstain/terminalize precedence；所有sync sibling均admitted；
- Permission/Continuation multiple winners的reason按winning rank + definition order确定；null fallback与once-used diagnostic不产生错误model context；
- invalid conflicting control fail-open；
- `hookSpecificOutput.hookEventName` mismatch、unknown field与partial-output rejection；
- plain stdout only accepted events；PostTool exit 2 unsupported；
- updatedInput/result rewrite/suppression invalid，original arguments至多invoke once；
- background control使whole handler output invalid；
- canonical ToolResult never rewritten；
- SessionEnd observe-only。

### 14.5 Lifecycle

- SessionStart startup/resume与active ROOT compact各once；一个runner/Host-owned pending cold-boundary slot覆盖launch、active compact与idle/historical compact，compact-before-first-open supersede launch source，idle ROOT adoption当下attempts=0且下一次actual ROOT attempt才以compact once；child compact attempts=0；initial与compact分别继承原planning/successor absolute deadline；所有真实SessionStart均在final installable compile/install/open前，允许headroom/semantic/uninstalled dry trial在前；
- SessionEnd证明ordinary producers先quiesce、ordinary fence后进入terminal lane，再physical drain/close；
- direct/queued复用同一gate实现但保持各自current stable identity；同一API exact retry coalesce，cross-API/different-variant同command并发在Hook前CONFLICT；BLOCK三项durable effect为0；FULL/CONFLICT/ACK-confirmed Hook=0；queued B sync context在queue-head FULL与live-task install前只存在exact `queue_item_id` slot，真实active A tool-followup/compaction不可消费，转交后B first request可见；
- PreTool prepared resolution后且authorize/admit/attempt前；early direct failure Pre=0/Post=1；pre-resolution meta failure的Post只使用outer identity；
- Plan enter/question/exit分别覆盖READY selected Pre、invalid/unavailable selected Pre=0、unselected barrier sibling Pre=0；Pre BLOCK证明workflow/interaction/waiter/continuation=0且selected canonical state仍是existing `PERMISSION_DENIED/POLICY_NO_ATTEMPT`、public error才是`HOOK_BLOCKED`，所有closed no-attempt results各Post once；rejected/idempotent ENTER的context留同一open origin，new ENTER/DRAFT terminal origin的context不跨continuation；question selected Post只在human resolution result FULL后、next compile前运行；
- ordinary、Plan immediate/delayed与subagent composite均产生同一`AcceptedCanonicalToolResultSettlement`；Plan batch tuple按call order覆盖selected与barrier siblings，question resolution exact one；ACK-unknown confirmation返回同一accepted entry sequence且Post不重复；Plan Hook `tool_response`与compiler对同一result的Round 7.1 public projection逐字段相等，不泄露storage-only workflow/interaction fields；
- Permission只在ASK，ABSTAIN回human，ALLOW/DENY exact request，late allow不能revive；
- PostTool覆盖每种canonical ToolResult origin并在effect settlement后exact once；
- meta MCP underlying identity exactly once；
- PreCompact BLOCK证明trigger-only projection/read及manual command row可存在，但snapshot candidate/write、summary request/call、adoption/install/open均0；manual waiter typed failure，auto同轮ordinary compile且不记automatic failure；PostCompact after adoption且before唯一final install/open；PostCompact settle后只在原successor deadline内读取一次turn status，cancellation/permanent failure通过post-adoption carrier传播且fence释放，manual waiter仍见exact `COMPACTED` winner、active runner ordinary fallback=0，historical winner忽略BLOCK active control；
- precompile与late-threshold的`NOT_NEEDED + successor=None`在同一planning cut只产生一个automatic attempt并继续ordinary provider open；不增加automatic-failure计数、不无限recompile、不用retry cap；
- active compaction允许多个uninstalled dry/structural trial，但final continuity CAS/install/open至多一次；ROOT final no-Hook/Hook siblings共享post-adoption exact facts与SYSTEM/tools，Hook按placement 68成为唯一新增source而不要求dry message prefix；child不伪造SessionStart；idle不造ghost context；
- active ROOT证明`SessionStartCompactPort`在coordinator fence内且final assemble前exact once；child port absent；释放fence后attempts=0；
- compact-start facts的adoption/binding/model/permission/attempt与final sibling/install exact-join；compact SessionStart BLOCK和PostCompact BLOCK都进入same special interruption carrier；manual/precompile/late-threshold三个caller在ROOT/child均不得继续ordinary compile，child task必须INTERRUPTED而非FAILED；
- SubagentStart task不可撤销；`PreparedSubagentLaunch`的configured model、effective permission/precondition、stable child turn与canonical task/profile全部投影准确，并与later target/admission exact-join；五种profile的matcher/stdin都使用`SubagentProfileKind.value`而非display role；sync context只进child first compile；
- SubagentStart等待不持manager lock，close按same absolute deadline cancel/join scheduler；已FULL未launch走dormant terminal fallback；
- `stop_agent` during SubagentStart覆盖SPAWNING/LAUNCHING两侧winner：stop先赢则child turn/runtime=0，launch先赢则stop不得走absent-turn path而由live-child joint cancellation settle；无ACTIVE-after-terminal；
- current `SubagentManager.aclose()`从第一项admission/scheduler join起受Host absolute deadline约束；Hook/process cancel先传播，deadline选择logical timeout，physical owner仍按existing no-detach contract完成terminate/kill/drain；不得保留pre-timeout无界shield wait，也不得把deadline后的physical drain误删；
- current combined `run_subagent_turn`入口已经hard-cut为pre-live child admission与admitted `run_accepted_turn`；architecture test禁止旧组合入口继续承担canonical admission，并证明continuity/memory scopes在所有terminal/cancel/error分支exact cleanup；
- async SessionStart/SubagentStart在first freeze前完成可进入first request；late completion只进exact ROOT/child下一safe point，launch/install失败或terminal则drop；
- EXPLICIT/INFERRED SubagentStop逐分支证明assistant/ToolResult/SubagentResult/permit settlement，且guard只在对应nonterminal settlement FULL后翻转；
- Stop在assistant settlement前；guard按exact ROOT turn/run隔离并在terminal/interruption retire；first continuation settle nonterminal，second仍observe但强制terminal；Hook等待期间steer可使TERMINALIZE candidate FULL但turn保持open且不消费once guard；
- cancellation/provider/resource failure paths不invent Stop/SessionStart/其他Hooks。

### 14.6 Context

- one candidate per compile；user-role append-only suffix；
- source-neutral renderer不写Plugin假设；
- FULL/COMPACT complete-entry behavior，`statusMessage`/stderr不进入body；
- existing compiler estimator执行additionalContext threshold，aggregate omission不阻断provider；
- additionalContext使用definition limit；Stop/SubagentStop continuation reason固定2,500且不受该field影响；buffer drop没有rendered omitted-count field；
- identical text with distinct causal occurrence产生distinct suffix；
- planning freeze后新arrival留给future，CAS success/conflict/omission/abandon都retire exact reservation；
- no put-back/replay/settled Hook rerun；
- ROOT/child/workspace/run/Host scope isolation、terminal late-drop与background loss；
- ROOT async background可从origin turn跨到同Host/exact workspace下一accepted ROOT turn，origin identity仍保留；不得进child。Child async永不跨task terminal；call-local/prompt slots仍按exact run/candidate retire；
- trust=`UNTRUSTED_OBSERVATION`且不进入SYSTEM。

### 14.7 Architecture/oracle

- `hooks/*`不导入`plugins/*`；
- `hooks/*`与`OperationalHookType/KernelExtensionHost`双向import prohibition；
- dispatcher dependency map允许pure matcher/executor/output/context port但禁止owner/repository；
- no database/repository/event/job additions；
- 七维oracle精确为`29 / 24 / 11 / 1 / 25 / 0 / 11`，最后一维是本轮11项`HookEventType`；
- `CommittedEventType`、`LiveEventType`与`HookEventType`三套closed vocabulary必须分别穷尽测试，不得互相复用或漏计；
- no redundant DTO fingerprints；
- no new arbitrary lifetime caps；
- no skip/xfail；
- old `PluginHookDispatcher`/`PLUGIN_HOOK_CONTEXT` absent。

### 14.8 Real dogfood

至少用一个真实OpenAI-compatible provider：

1. USER Hook在UserPromptSubmit提供真实context；
2. active ROOT A期间enqueue B，A的tool-followup requests均看不到B context，exact queue-head FULL后的B request可见；
3. WORKSPACE PreToolUse真实deny一次Tool并让模型恢复；
4. PermissionRequest真实allow/deny exact request；
5. PostToolUse读取真实Round 7.1 public projection并feedback；
6. background output在下一safe point进入真实prompt；
7. mid-turn compaction触发Pre/PostCompact与SessionStart(compact)，trace证明唯一final successor open；
8. idle compaction adoption后下一actual ROOT provider open才出现exact one SessionStart(compact)；
9. Round 10 worker触发SubagentStart/Stop；
10. 修改definition后old trust变MODIFIED，reload后不再执行；
11. 真实记录Hook stdin/stdout/stderr、provider prompt与model reply，只排除`PULSARA_API_KEY`。

---

## 15. Definition of Done

1. USER/WORKSPACE `hooks.json`是可用的真实产品路径；
2. 11项event各有唯一producer、指定typed outcome consumer与明确terminal outcome；
3. 11 typed input、5 closed outcome family与single output parser穷尽，raw field不出core；
4. Hook engine完全独立于Plugin与OperationalHook extension plane；
5. trust/reload/modified状态、single publication lane与concurrent slice merge闭合；
6. one config parser、trust store、matcher、dispatcher、executor、output parser、background buffer/context source；
7. `description`、`statusMessage`、`trusted_at`各有且仅有本文规定的inspection/diagnostic consumer；
8. same-epoch SYSTEM/tools byte-identical、messages suffix-only；compaction可做uninstalled dry trial，但只CAS/install/open一个final successor；
9. `HOOK_CONTEXT`保持user-role、one-shot、same-scope、untrusted、advisory/omittable；
10. Hook output不成为SYSTEM、canonical row、ToolResult rewrite或permission snapshot；
11. owner/cancel/deadline revalidation、process group drain与PULSARA_API_KEY exact boundary闭合；
12. no durable Hook execution machinery、no arbitrary total/lifetime cap、no redundant fingerprints/maps；
13. no schema/Committed/Live event/job/guard/relation增长；oracle保持`29 / 24 / 11 / 1 / 25 / 0 / 11`；
14. targeted/full/PostgreSQL/architecture与real dogfood全部通过，无skip/xfail或assertion weakening；
15. Round 5B active spec/tests中被§0.5取代的full-dry-equality断言已在同一hard cut更新，未保留dual contract；
16. §8.9确认的Subagent launch-vs-stop、close deadline与late-threshold compaction liveness缺陷已经在同一diff修复；§8.6 combined child admission/loop入口完成single-path hard cut，均无dual path、补偿事务或scope leak；
17. ordinary、Plan immediate/delayed与subagent composite ToolResult保留各自atomic owner，但在FULL后收敛为one process-local accepted settlement与one Round 7.1 public projection；Plan不伪造physical attempt，Hook不查库或重建body；
18. Round 9.3只增加definition producer/provenance/environment overlay并复用single publication lane，没有修改本轮runtime semantics。

### 15.1 Activation（2026-08-24，ACTIVATED）

复审确认的6项缺口已在当前working-tree单一路径闭合：queued UserPromptSubmit context保持exact `queue_item_id` binding直至queue-head FULL与live-task install；一个runner/Host-owned pending cold-boundary slot统一startup/resume、active compact与idle/historical compact；adoption FULL后的PostCompact control只在原`successor_deadline`内单次status revalidation确认RUNNING后生效，post-adoption failure保留canonical `COMPACTED` winner并禁止ordinary fallback；spawn sink紧邻process creation重新snapshot exact API key并共同验证command/final environment/stdin；initial/compact SessionStart分别继承现有planning/successor deadline；cold source discovery在worker thread接收Host-open deadline，以nonblocking final open拒绝FIFO/non-regular source，完整比较`dev/inode/size/mtime_ns/ctime_ns`并把race/timeout降为exact source UNAVAILABLE。没有为这些closure增加generation、lease DTO、durable recovery、retry cap或兼容路径。

重新运行的retained full pytest为`922 passed in 159.52s`；PostgreSQL全量为`223 passed, 699 deselected in 119.23s`；fresh-v0/reset/repeat-migrate/deep-verification沿用本diff未触及schema后的最近结果`12 passed in 3.27s`；Ruff、compileall与`git diff --check`在最终diff重新通过，terminal protocol generator与`uv lock --check`沿用本diff未触及协议/依赖后的activation结果。七维oracle保持`29 / 24 / 11 / 1 / 25 / 0 / 11`，没有新增skip/xfail、schema、Committed/Live event、guard、relation或durable job。

真实dogfood继续使用生产`DirectKernelModelPort`与真实command Hook，结果为`passed`：70次Hook command invocation、24次provider open、4次compaction summary request、12个canonical ToolResult，11项lifecycle全覆盖；queued B context对active A的两个provider requests均不可见、对B request可见，active与idle compaction各有exact one compact SessionStart final open，permission allow/deny、Plan no-physical-attempt、Subagent/ROOT continuation、reload predecessor view与SessionEnd terminal lane全部成立。完整prompt、Hook stdin/stdout/stderr、provider-visible messages、model replies与ToolResults保存在[`round9_2_hook_subsystem_trace.json`](benchmarks/suites/core/v1/round9_2_hook_subsystem_trace.json)，只排除exact nonempty `PULSARA_API_KEY`；machine-readable结论见[`round9_2_hook_subsystem_activation.json`](benchmarks/suites/core/v1/round9_2_hook_subsystem_activation.json)。当前没有外部availability blocker。

### 15.2 Round 9.3 adapter synchronization（2026-08-26）

Round 9.3真实provider dogfood使用one installed package运行trusted Plugin `UserPromptSubmit`与`PreToolUse`，并在compaction lifecycle运行`PreCompact`、`PostCompact`、`SessionStart(compact)`。Trace保留exact Hook stdin/stdout/stderr、provider-visible context/control与model replies，只排除exact nonempty API key。Replace形成new disabled package；重新enable后原Hook trust为`MODIFIED/UNTRUSTED`，必须单独trust。`reload_hooks`的Pre/Permission/Post继续使用invoke-time predecessor，commit merge使用publication-time current predecessor；concurrent Plugin/local publication共享one lane且固定`Hook publication -> Host`锁序。Old attempt通过generic lifetime anchor持旧package root直到settle。

---

## 16. 最终冻结

Round 9.2激活后的产品语义：

> Pulsara用户可以在`${PULSARA_HOME}/hooks.json`或exact workspace `.pulsara/hooks.json`中声明Codex-compatible command Hooks；enabled local Agent Plugin也可从fixed `dev.pulsara/hooks/hooks.json`贡献definitions。用户分别审阅并trust exact normalized definitions后，唯一process-local `KernelHookDispatcher`在11项lifecycle seam执行它们；successful control只影响当前合法operation，informational output只作为append-only、source-neutral、untrusted `HOOK_CONTEXT`进入模型。Hook不拥有canonical conversation、Tool、permission、compaction或subagent authority，不增加durable execution recovery，也不改变同epoch SYSTEM/tools。Plugin只贡献definitions、trust subject与trust-covered declaration environment，不复制任何Hook engine。

---

## 17. Reviewer finding closure index

本表把[`PULSARA_ROUND_9_2_REVIEWER_FINDINGS.md`](PULSARA_ROUND_9_2_REVIEWER_FINDINGS.md)逐项绑定到本文新的coding authority。所有closure都**不需要新增durable machinery**。

| Finding | authoritative closure | durable addition |
|---|---|---|
| P1-1 output/outcome矛盾 | §0.4、§7、§13：5-family union、exit-2 matrix、unsupported rewrite no-partial/no-synthetic rejection | none |
| P1-2 prompt logical producer | §8.1/§8.3：Host one `NewTurnHookGate`覆盖direct/queued/FULL/ACK-unknown | none |
| P1-3 underlying identity seam | §5.2、§8.4：owner-issued `PreparedResolvedToolInvocation`，authorize/admit/attempt=0 | none |
| P1-4 permission handoff | §8.4：owner-issued `PreparedPermissionRequest`与ABSTAIN/ALLOW/DENY exact consumption | none |
| P1-5 compaction dependency cycle | §0.5、§8.5：dry只证明no-Hook base；fence内Post + ROOT call-local start port；同源facts上final cold assembly；至多一次final CAS/install/open；删除installed-base augmentation | none |
| P1-6 late ROOT Stop | §8.1/§8.7：assistant candidate frozen后、canonical settlement前dispatch | none |
| P1-7 SessionEnd ordering | §4.3、§8.8：producer quiesce→ordinary fence→terminal lane→process drain→physical close | none |
| P2-1 compatibility profile | §2.2、§5.1、§7、§9.1：config recovery、matcher、output、11 typed stdin及permission mapping | none |
| P2-2 deadline/drain/secret | §6：parent-min deadline、immediate drains、group abort与exact API-key reject/scrub | none |
| P2-3 context occurrence/settlement | §9.2–§9.4：causal identity、typed slots、reservation retirement与scope loss | none |
| P2-4 PostTool projection/coverage | §8.4：Round 7.1 single pure helper与all canonical ToolResult origins exact once | none |
| P2-5 SubagentStop settlement | §8.6：EXPLICIT/INFERRED assistant、ToolResult、SubagentResult、permit branches | none |
| P2-6 background/diagnostics | §6.6–§6.7、§9.3：admission ordinals、nonwaiting safe point、one process-local sink | none |
| P3-1 decorative fields/provenance | §3.1–§3.3、§6.1、§9.2：acyclic single provenance chain、real consumers、no DTO proof hash | none |
| P3-2 OperationalHook/maps | §1.3、§10、§12、§14.7：extension isolation与decomposed production/dependency/test map | none |

Closure成立的机械检查是：全文不得再出现“`updatedInput`使invoke=0”、PostCompact要求successor已installed、Stop在assistant accepted之后、all-event `permission_mode`、background control partial effect或base-successor augmentation。若未来修订重新引入其中任一语义，状态必须退回DEFERRED并重新审阅。
