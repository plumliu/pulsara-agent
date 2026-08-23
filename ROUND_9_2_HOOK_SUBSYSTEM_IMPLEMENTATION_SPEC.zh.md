# Round 9.2：独立 Hook Subsystem 实施规格

> 状态：**DEFERRED — 暂不实施，NOT ACTIVATED**
>
> 本规格当前仅保留为后续讨论材料，不是可交给coding agent的实施authority。现有设计仍有未闭合问题，详见[`PULSARA_ROUND_9_2_REVIEWER_FINDINGS.md`](PULSARA_ROUND_9_2_REVIEWER_FINDINGS.md)；在这些findings经过重新讨论并形成新的明确修订前，不得据本文开始编码、预埋Hook seam或增加Hook vocabulary。
>
> 修订日期：2026-08-22
>
> 编码基线：以当前已ACTIVATED的Round 9、Round 9.1、Round 5B与Round 10 clean checkpoint为准。
>
> Fingerprint约束：[`PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)是强制上位契约。完整frozen Hook values已经传到consumer时不得再添加DTO fingerprint。本轮只允许保留“用户审阅过的exact definition集合”与未来执行之间的trust digest；不得建立Hook registry fingerprint、`digest -> object` map或proof graph。
>
> 上位契约：[Round 3 structured compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 5B compaction](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)、[Round 7 observation](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7.1 ToolResult projection](ROUND_7_1_PROVIDER_VISIBLE_TOOL_RESULT_PROJECTION_IMPLEMENTATION_SPEC.zh.md)、[Round 9 capability](ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md)、[Round 10 subagent](ROUND_10_HIERARCHICAL_SUBAGENT_ORCHESTRATION_IMPLEMENTATION_SPEC.zh.md)、[Gap Index](archived_docs/POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 公开兼容基线：[Codex Hooks](https://learn.chatgpt.com/docs/hooks)。本地Codex checkout当前production实现仍只有不含`SessionEnd`的10项event，但公开契约已经列出11项；本文以公开11项为产品truth，并把本地差异当作上游版本差异，而不是缩窄Pulsara契约。
>
> 下游消费者：[Round 9.3 Agent Plugin Bundle 与 Hook Adapter](ROUND_9_3_AGENT_PLUGIN_BUNDLE_AND_HOOK_ADAPTER_IMPLEMENTATION_SPEC.zh.md)。Round 9.3只能成为本文的definition producer，不得复制trust、matcher、dispatcher、executor、output parser或provider context owner。

本文先把Hook实现为**独立、source-agnostic、process-local的Runtime subsystem**。本轮用USER与exact WORKSPACE `hooks.json`作为首个真实产品路径；未来Plugin只把包内`hooks/hooks.json`归一化成同一组definitions，复用本轮所有执行机制。

---

## 0. 最终产品形状

### 0.1 两类当前source，一个运行时

```text
${PULSARA_HOME}/hooks.json             USER source
<workspace>/.pulsara/hooks.json       exact WORKSPACE source
             │
             ├─ complete bounded discovery
             ├─ Codex-compatible JSON normalization
             ├─ exact definition trust
             └─ FrozenHookDefinitionSet
                         │
                         ▼
                 KernelHookDispatcher
                   ├─ matcher
                   ├─ command executor
                   ├─ control aggregation
                   ├─ diagnostic/background buffer
                   └─ HOOK_CONTEXT
```

Round 9.3加入后，只增加第三个producer：

```text
Plugin hooks/hooks.json
  -> PluginHookContributionAdapter
  -> same FrozenHookDefinition values
  -> same trust owner / dispatcher / executor / HOOK_CONTEXT
```

本轮不存在Hook subclass、PluginHookDispatcher、managed policy engine或第二套event bus。

### 0.2 Authority

Hook执行外部command，但不拥有它所观察或控制的产品对象：

| 产品对象 | 最终authority | Hook可做什么 |
|---|---|---|
| user prompt ingress | existing queue/turn admission | block exact ingress；提供一次性context |
| Tool call | existing tool authorize/attempt/invoke owner | deny；提供context；不改写arguments |
| permission request | existing permission owner | allow/deny当前exact request；不改变permission mode |
| ToolResult | canonical ToolResult transaction + process-local settlement | 读取public projection；提供下一call feedback；不改写result |
| compaction | Round 5B lane/fence/summary/adoption owner | Pre阶段abort；Post阶段提供context或停止active continuation |
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

### 0.4 Prefix continuity

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

### 0.5 Durability与oracle

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

- Plugin package parsing、installation、MCP/Skill/preset；这些属于Round 9.3；
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

Config不存在表示`COMPLETE + empty`，不是UNAVAILABLE。Symlink、非regular file、路径escape、invalid UTF-8、overbound或读取identity race使exact source `UNAVAILABLE`；其他source仍可独立成立。Hook是optional subsystem，source UNAVAILABLE只产生diagnostic并让该sourcefuture handlers不可运行，不阻断conversation。

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

- unknown top-level/event/group/handler field按Codex兼容诊断处理，绝不猜成Pulsara authority；
- unknown event使exact event entry unavailable，不废掉其他event；
- 非`command`handler逐handler typed unsupported；
- invalid matcher只使exact group unavailable；
- invalid command/timeout只使exact handler unavailable；
-一个文件合法但没有runnable handlers仍是`COMPLETE + empty runnable set + diagnostics`。

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
2. lstat并拒绝symlink/non-regular；
3. bounded读取exact bytes；
4. 再次验证identity/size未变；
5. parse/normalize完整definition set；
6. 读取generic trust state并exact join；
7. freeze immutable source value。

读取中出现replace/delete/identity变化，本次source为`DISCOVERY_RACED/UNAVAILABLE`，不得无限重试或拼接partial definitions。全过程共享当前Host open/reload absolute deadline。

### 2.5 Ordering与merge

Current source order：

```text
USER
WORKSPACE
```

两层是append，不是后者覆盖前者。一个exact declaration只属于一个source；即使两层command/matcher文本完全相同也分别运行，CLI必须显示duplicate-looking definitions及provenance，不能做语义dedup。

Definition order为：

```text
source ordinal
event ordinal
group ordinal
handler ordinal
```

这一顺序只决定start/settlement/context rendering；deny/block仍按§7的closed aggregation，不因source顺序被allow覆盖。

Round 9.3启用时可在本文后追加`HookSourceKind.PLUGIN`及其deterministic package/component order。Visibility仍是USER或exact WORKSPACE。它必须把complete normalized values交给同一个factory；不得让Hook core扫描Plugin store或提前在本轮加入dormant Plugin enum/branch。

---

## 3. Pure contracts与trust

### 3.1 Values

```text
HookSourceKind
  USER_FILE
  WORKSPACE_FILE

HookSourceIdentity
  source kind
  exact canonical source path
  visibility scope = USER | exact WORKSPACE

HookTrustSubject
  stable logical source locator
  USER_FILE      -> fixed USER subject
  WORKSPACE_FILE -> existing canonical workspace state key

FrozenHookDefinition
  source identity
  event type
  matcher fact
  handler ordinal
  exact command / commandWindows
  timeout
  async disposition
  status message
  additional-context limit
  closed environment overlay

FrozenHookSourceSnapshot
  source identity
  COMPLETE | UNAVAILABLE
  ordered complete definitions
  diagnostics

FrozenHookDefinitionSet
  exact ROOT/child visibility scope
  ordered source snapshots
  ordered runnable definitions
  diagnostics
```

完整值传递到consumer，不增加snapshot/set/definition fingerprints。Private constructor与owner-issued object identity保证当前进程真实性；source scope与path不能由generic caller自报。

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

`trusted_definition_digest`使用domain-versioned canonical framing覆盖：

- exact current source identity、scope/path；
- ordered normalized event/group/handler definitions；
- matcher、command/commandWindows、timeout、async、status、context limit；
- closed environment overlay中的public key/value；
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

`inspect`必须完整显示current normalized event、matcher、command、timeout、async/context设置、source path与definition digest。`trust`在写前重新读取current source并比较exact expected digest；禁止`trust latest`或按path盲信。

Initial state为enabled但UNTRUSTED：source可被list/inspect，但command不会运行。Disable只影响future event；已dispatchattempt持有old definition直到settle。Trust/revoke/enable/disable命令使用per-source local lock、read-modify-atomic-replace；不建立全局Hook transaction或durable execution lease。

### 3.4 Project trust边界

WORKSPACE `hooks.json`即便在版本库中存在也不能自动执行。Exact Hook trust是本轮唯一project execution trust gate；它与Tool permission mode是不同边界：

- Hook trust授权配置中的external command；
- Tool permission授权模型发起的Tool operation；
- Hook的`PermissionRequest allow`只批准当前exact request；
- Hook不能改变后续permission snapshot或bypass scope/effect/liveness gate。

这里的“exact”只指用户inspection中看到的normalized Hook declaration：event、matcher、command line、timeout与environment overlay。它**不是**transitive executable attestation。Command可以引用之后被修改的workspace script、解释器、PATH executable、网络资源或shell expansion；trust意味着用户持续授权该exact command declaration以当前Host OS用户身份运行，直到revoke/definition变化，而不是Runtime承诺其依赖bytes永远不变。CLI必须醒目标注这一点。

本轮不得递归hash script/import/tree、冻结PATH executable、复制Hook代码进managed store或建立dependency graph来制造虚假的“代码完整性”。需要immutable package code时由Round 9.3 managed Plugin install id提供清晰边界；ordinary custom Hook保持Codex式command trust。

---

## 4. Reload与Host lifecycle

### 4.1 Cold discovery

每个Host cold composition完整读取current USER/exact WORKSPACE sources，形成一个`KernelHookDispatcher` current view。Cold source UNAVAILABLE不阻断Host；对应handlers不执行。

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

1. 取得process-local Hook reload lane；
2. 在锁外按一个absolute deadline读取/normalize sources；
3. 重新进入Host lock验证Host仍open与target scope current；
4. atomic replace `KernelHookDispatcher` future definition set；
5. old attempts继续持有predecessor values并settle；
6. 无old consumer后释放old view。

`reload_hooks`自身的Pre/Permission/Post事件全部使用attempt开始时的predecessor Hook view；new view从下一lifecycle event生效，不能在自己的PostToolUse自触发。

Missing config是完整empty replacement，会撤销future handlers。Malformed/unreadable/raced source在新view中为UNAVAILABLE并撤销该sourcefuture runnable handlers；不得为了“可用性”继续执行current filesystem已经无法证明的old external command。Other complete source继续运行。

Host close先停止ordinary lifecycle Hook admission并冻结current view，再通过唯一terminal lane运行§8 `SessionEnd`；随后取消未完成background attempts、bounded drain/kill全部process groups并关闭dispatcher。SessionEnd不受ordinary-admission fence误挡，且自身使用close开始时冻结的view；一旦开始physical Hook drain便不得再接受任何event。

---

## 5. Matcher与Tool identity

### 5.1 Matcher

- missing、empty或exact `*`：match all；
- only alphanumeric/underscore/pipe：exact alternatives；
- 其他：linear-time regex；
- unsupported lookaround/backreference：exact matcher group unavailable；
- `UserPromptSubmit`与`Stop`忽略matcher。

### 5.2 Tool aliases

```text
terminal                  -> {terminal, Bash}; external tool_name=Bash
apply_patch               -> {apply_patch, Edit, Write}; external primary=apply_patch
spawn_agent/create_agent_tasks -> exact Pulsara name + Agent alias
MCP                       -> exact resolved provider-qualified remote identity
other builtin             -> exact Pulsara descriptor name
```

Event stdin同时保留`pulsara_tool_name`。Alias只影响matcher与兼容primary name，不伪造参数schema。

Round 9 meta route：

- `use_new_mcp_tool`先exact resolve opaque ref/route/policy；
- Hook在remote attempt前按underlying resolved MCP identity运行一次；
- outer meta wrapper不再触发第二组Pre/Permission/Post；
- stdin保留`pulsara_tool_name=use_new_mcp_tool`用于诊断；
- inspect/list等目录Builtin按自身name触发。

Hosted provider tool没有local attempt seam时不伪造Hook覆盖。

---

## 6. Command execution

### 6.1 Attempt

```text
HookCommandAttempt
  exact event input
  exact FrozenHookDefinition
  source provenance
  command environment
  absolute deadline
  process/task handle
  bounded stdout/stderr buffers
```

Generic executor不解析Plugin、Tool registry或repository object。Source adapter已经冻结cwd与environment overlay。Current USER/WORKSPACE adapter提供：

```text
PULSARA_HOOK_SOURCE_DIR = directory containing hooks.json
PULSARA_PROJECT_DIR     = exact workspace root when available
```

Round 9.3 Plugin adapter未来可额外提供`PLUGIN_ROOT/PLUGIN_DATA`与Claude aliases；generic executor只消费closed overlay。

### 6.2 Process semantics

- stdin是§9 bounded JSON object；
- Unix使用configured shell command语义，Windows使用`commandWindows`或fallback command；
- cwd为event exact current workspace/run cwd；
- 每attempt创建独立POSIX session/process group或Windows等价owner；
- timeout/cancel/Host close先terminate whole group，复用existing 5-second abort/join physical bound后kill并drain；
- nonzero、spawn failure、timeout或invalid output按§7 fail-open；
- Runtime在一个存活attempt内不主动重复启动同一handler。

### 6.3 Secret boundary

只把exact active `PULSARA_API_KEY`值视为必须排除的secret：

- spawn environment显式删除该key；
- stdin所有string leaf在serialization前用固定`[REDACTED_PULSARA_API_KEY]`替换exact value；
- stdout/stderr在parse/log/context前跨chunk替换exact value；
- raw matched bytes不能写artifact/trace/exception repr；
- 其他prompt、Hook input/output、model reply、DSN、path与token-looking文本保持可观察。

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

Slots是physical scheduling bounds，额外matching handlers排队而不被拒绝。每个attempt deadline从event dispatch计时，不能通过排队获得新timeout。没有selected-handlers、session Hook count或Hook lifetime cap。

### 6.5 Output bounds

```text
stdout capture  1 MiB
stderr capture  1 MiB
stdin JSON      existing MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES
```

Capture越界终止exact process。Parser复用§2.3 node/depth/scalar bounds；`additionalContext`可使用capture中完整UTF-8 string，再进入existing single-source/aggregate compiler bounds。不得为Hook建立artifact或专用大正文通道。

### 6.6 Sync/background

`async=false`为synchronous：同一event matching handlers尽快并发start，event owner等待settle后按definition order聚合。

`async=true`为background：

- 不能产生allow/deny/block/rewrite/stop/continue；
- 只有diagnostic与informational context；
- output进入exact-scope process-local pending buffer；
- 下一次合法provider safe point作为`HOOK_CONTEXT`消费；
- Host close、scope terminal或无未来safe point允许丢失。

---

## 7. Output与control contract

### 7.1 Common output

```json
{
  "continue": true,
  "stopReason": "optional",
  "suppressOutput": false,
  "systemMessage": "optional",
  "hookSpecificOutput": {}
}
```

只支持本文closed字段。`systemMessage`只进入process-local diagnostic，不进入SYSTEM。`suppressOutput=true`、argument/result rewrite字段或event不支持的control使exact handler output invalid并fail-open。

### 7.2 Event controls

| Event | accepted control |
|---|---|
| SessionStart | `continue:false`停止当前ROOT continuation；additional context |
| SessionEnd | observe-only |
| UserPromptSubmit | block exact ingress；additional context |
| PreToolUse | deny；additional context；`updatedInput`触发unsupported local rejection |
| PermissionRequest | allow/deny current exact request；no-decision进入原human flow |
| PostToolUse | feedback/additional context；不改写ToolResult |
| PreCompact | abort exact compaction |
| PostCompact | stop active continuation；context |
| SubagentStart | context only；不能取消canonical task |
| SubagentStop | 最多一次same-child continuation；context |
| Stop | 最多一次same-ROOT continuation；context |

Aggregation：deny/block优先；`continue:false`按event closed解释；context按definition order；一个handler返回互斥control则该handler invalid；physical failure不伪造decision。

### 7.3 Plain stdout

Plain non-JSON stdout只有`SessionStart`、`UserPromptSubmit`、`SubagentStart`可作为additional context。其他event忽略plain stdout或要求JSON。以`{`/`[`开头但invalid JSON是handler failure，不回退为plain text。

### 7.4 Failure policy

Trusted Hook command本身可能有任意filesystem/network/process side effect，不在Tool sandbox内。CLI inspection必须明确显示这一点。

但Hook engine failure默认fail-open：timeout、spawn failure、nonzero、invalid JSON、unsupported output只产生typed diagnostic，normal product owner继续。只有成功解析的explicit control影响当前合法operation。

Hook不承诺cross-crash exactly-once。Host可能在command产生外部side effect后、上游transition前崩溃，未来发生duplicate或loss；作者应设计幂等handler。已有canonical FULL/stateless confirmation证明transition已成立时不得重跑对应ingress Hook，但不为此新增receipt/history/replay。

---

## 8. Exact lifecycle seams

### 8.1 Matrix

| Internal | External | Producer seam | Matcher |
|---|---|---|---|
| `SessionStartEvent` | SessionStart | ROOT首次cold open或compaction successor下一次actual open前 | startup/resume/compact |
| `SessionEndEvent` | SessionEnd | ROOT close停止业务admission并drain后、Hook engine close前 | other |
| `UserPromptSubmitEvent` | UserPromptSubmit | NEW_TURN local validation/compatible ingress后、queue insert前 | ignored |
| `PreToolUseEvent` | PreToolUse | exact invocation parse/resolve后、authorize/attempt前 | tool aliases |
| `PermissionRequestEvent` | PermissionRequest | existing gate确定ASK后、interaction/attempt前 | tool aliases |
| `PostToolUseEvent` | PostToolUse | canonical ToolResult FULL及process-local settlement后、next compile前 | tool aliases |
| `PreCompactEvent` | PreCompact | Round 5B lane+scope fence后、source read/summary前 | manual/auto |
| `PostCompactEvent` | PostCompact | adoption FULL；active还需successor install成功 | manual/auto |
| `SubagentStartEvent` | SubagentStart | task FULL且scheduler选中、child first compile前 | agent_type |
| `SubagentStopEvent` | SubagentStop | EXPLICIT/INFERRED completion candidate后、SubagentResult commit前 | agent_type |
| `StopEvent` | Stop | ROOT natural final assistant accepted、run terminal前 | ignored |

### 8.2 Session

SessionStart/End只属于ROOT。Child first-open运行SubagentStart；child compaction运行Pre/PostCompact但不伪造ROOT SessionStart(compact)。Idle ROOT compaction直到next actual cold open才运行SessionStart(compact)。SessionEnd同步1..3秒，失败不能阻止physical close。

### 8.3 Prompt ingress

```text
local validation
-> compatible-ingress confirmation
-> already FULL: Hook = 0
-> UserPromptSubmit hooks
-> block: queue row=0, USER_MESSAGE=0, provider open=0
-> continue: ordinary enqueue
-> future queue-head FULL consumption
-> first compile consumes matching pending Hook context
```

Informational output以existing stable`queue_item_id`绑定在process-local `PendingPromptHookContext`中；Host loss允许丢context但不能丢durable queue item。Steer、automatic continuation、Hook continuation与subagent objective不触发UserPromptSubmit。

### 8.4 Tool与permission

PreToolUse消费exact model-call invocation carrier，不从mutable registry按name重找。Deny发生在attempt前。

PermissionRequest只在原owner本来需要human decision时运行。Any deny wins；否则allow只批准current exact request；无decision进入原interaction。Hook不能越过scope/effect/dirty/liveness guard。

PostToolUse读取Round 7.1 pure provider-neutral variant builder产生的event-time public representation：FULL eligible则FULL，否则first best-available，并标明mode。它不读取artifact raw/private replay，不承诺未来compiler会选择同一variant。Feedback进入下一call `HOOK_CONTEXT`，canonical ToolResult不变。

### 8.5 Compaction

PreCompact在global lane与exact-scope fence已安装后运行；abort不写snapshot、不调用summary。PostCompact只在canonical adoption FULL后运行，active还需successor installation成功。Round 5B只调用本轮dispatcher，不建立compaction-private engine/context owner。

### 8.6 Subagent

SubagentStart发生在task canonical FULL后，Hook不能撤销task；context进入child cold seed。

SubagentStop统一EXPLICIT与INFERRED result。每个successful completion最多一次Hook continuation，使用process-local `stop_hook_active`阻止循环。INFERRED continuation不提交result；EXPLICIT continuation只提交ordinary“本次result未接纳”的ToolResult，SubagentResult仍为0，task保持ACTIVE。Second natural stop必须terminalize。

### 8.7 ROOT Stop

ROOT natural stop最多一次Hook continuation。User cancel、Host close、provider failure或resource interruption不运行可continuation Stop Hook。Continuation context不是human prompt，不进入prompt queue。

---

## 9. Hook input与provider context

### 9.1 Common stdin

所有command至少收到bounded public JSON：

```text
session_id
transcript_path = null
cwd
permission_mode
hook_event_name
```

按event增加prompt、tool name/input/response、compact trigger、agent id/type、stop reason等closed public字段。不得携带hidden reasoning、private replay body、raw artifact、PULSARA_API_KEY或完整internal DTO repr。

### 9.2 Source-neutral provider source

```text
ContextSourceKind.HOOK_CONTEXT
contract    pulsara.hook-context.v1
channel     RUNTIME_OBSERVATION
trust       UNTRUSTED_OBSERVATION
budget      IMPORTANT
placement   68
lifecycle   ONE_SHOT
variants    FULL | COMPACT
```

```text
FrozenHookContextBatch
  exact ROOT/child scope
  ordered complete entries
  omitted older entry count
```

完整value直接传递，不添加batch fingerprint。每次compile最多一个该kind candidate。FULL含source/event/status/context；COMPACT删除诊断定位但不截断单项正文。若不fit，compiler可省略整个IMPORTANT source；advisory Hook context不得形成conversation resource boundary。

Body明确说明它是untrusted external command output，不能授予permission、改变Tool/MCP availability、覆盖current user request/canonical ToolResult或修改SYSTEM。

### 9.3 Installation与background buffer

Hook context与同一continuity candidate一起CAS安装。CAS失败可保守丢弃call-local batch，不重跑settled Hook。

Background pending buffer是exact-scope process-local值，复用existing maximum single-source variant bytes，以完整entry保留最新可容纳suffix并累计omitted count；不增加entry-count cap。Scope terminal/Host crash允许丢失。

只有event后仍存在同scope合法provider continuation时才交付context。Block ingress、abort compaction、terminal Stop/SubagentStop、idle PostCompact与SessionEnd上的context只做diagnostic。

---

## 10. Owners与依赖

### 10.1 Owners

| Owner | 拥有 | 不拥有 |
|---|---|---|
| `LocalHookSourceProvider` | USER/WORKSPACE exact source discovery/normalization | process execution、Plugin package |
| `HookTrustStore` | source enablement与exact trust digest | definition object registry、execution history |
| `KernelHookDispatcher` | current definition set、matcher、attempt scheduling、control aggregation、pending context | canonical rows、Plugin parsing、Tool/permission authority |
| `HookCommandExecutor` | subprocess/process-group、stdin/stdout/stderr、deadline | event semantics、trust persistence |
| existing compiler/continuity | `HOOK_CONTEXT` selection/CAS | Hook process/trust |
| existing lifecycle owners | exact event seam与最终operation settlement | Hook process internals |

### 10.2 Dependency direction

```text
hooks/contracts.py       imports no plugins/repository/provider adapter
hooks/source.py          may import local filesystem/config primitives
hooks/dispatcher.py      imports contracts/executor only
plugins/hook_adapter.py  (Round 9.3) may import hooks contracts/parser
hooks/*                  must never import plugins/*
compiler/continuity      receives pure HOOK_CONTEXT source values only
```

Generic Hook subsystem不能扫描Plugin package root，也不能知道MCP supervisor、Skill provider或subagent manifest。

---

## 11. Implementation slices

### R9.2-0：Contracts与local sources

- 11-event closed vocabulary；
- JSON parser/bounds；
- USER/WORKSPACE complete discovery；
- deterministic append ordering；
- no fingerprints on carried values。

### R9.2-A：Trust与reload

- generic source trust state/digest；
- list/inspect/trust/revoke/enable/disable/doctor CLI；
- fixed `reload_hooks` Builtin与Host method；
- old-attempt drain/future-view replacement。

### R9.2-B：Executor与output

- matcher/aliases；
- sync/background scheduler；
- process groups/deadlines/bounds；
- PULSARA_API_KEY exact scrub；
- JSON/plain output parser与control aggregation。

### R9.2-C：Lifecycle integration

- prompt/tool/permission/result；
- compaction；
- subagent；
- ROOT start/stop/end；
- one `HOOK_CONTEXT` source。

### R9.2-D：Activation

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
  parser.py
  source.py
  trust.py
  matcher.py
  executor.py
  dispatcher.py
  context.py
```

### 12.2 Existing integration

- `conversation_kernel/host.py`：cold discovery、reload、close；
- `conversation_kernel/runner.py`：prompt/tool/permission/result/stop seams；
- `conversation_kernel/compaction/*`：Pre/PostCompact与ROOT SessionStart(compact)；
- `conversation_kernel/subagent.py`：SubagentStart/Stop；
- `conversation_kernel/context_sources.py`、`model_input/contracts.py`：`HOOK_CONTEXT`；
- `capability/builtin_catalog.py`、`conversation_kernel/tool_runtime.py`：fixed ROOT `reload_hooks`；
- CLI：Hook review/trust commands。

不得新增migration、Protocol event kind或repository relation。

---

## 13. Failure matrix

| Failure | Hook outcome | Product owner outcome |
|---|---|---|
| config absent | COMPLETE empty | normal continuation |
| malformed/overbound/raced source | source UNAVAILABLE；future handlers none | normal continuation |
| untrusted/modified/disabled | visible diagnostic；not runnable | normal continuation |
| reload CAS/Host close race | new view discarded | predecessor/current close rules |
| matcher invalid | exact group unavailable | other handlers continue |
| spawn/timeout/nonzero/invalid output | typed diagnostic, fail-open | normal operation continues |
| stdout/stderr overflow | kill exact process, fail-open | normal operation continues |
| successful explicit deny/block | closed control | original owner executes typed denial path |
| updatedInput | HOOK_UPDATED_INPUT_UNSUPPORTED | physical Tool invoke=0；ordinary rejection ToolResult |
| background control output | ignored/diagnostic | no control effect |
| context compiler omission | context lost | provider call may continue |
| Host crash after Hook side effect | duplicate/loss allowed | no receipt/replay |
| SessionEnd failure | diagnostic only | Host closes |

---

## 14. Test plan

### 14.1 Discovery/trust

- USER only、WORKSPACE only、combined scope、transient USER only；
- absent vs malformed/UNAVAILABLE；
- symlink/nonregular/race/UTF-8/bounds；
- append-not-override order与duplicate-looking handlers；
- normalized-equivalent JSON保持trust；semantic change变MODIFIED；
- trust expected digest stale拒绝；
- no digest->object map；
- project file未trust不执行。
- command引用的script bytes变化不伪造definition MODIFIED；inspection明确说明trust不证明transitive code integrity；

### 14.2 Reload/prefix

- fixed descriptor在所有epoch存在；non-BYPASS attempt=0；
- new view只从下一event生效；reload自己的Post使用old view；
- deleted config形成empty replacement；malformed replacement撤销future handlers；
- running old attempt可settle且view随后释放；
- Chat/Responses均证明same epoch SYSTEM/tools exact、messages suffix-only。

### 14.3 Executor

- sync parallel start、deterministic settle；
- background queue与safe-point context；
- deadline从dispatch开始；extra handlers queue不reject；
- process-group terminate/kill/drain；
- stdin/capture/node/depth/scalar bounds；
- exact PULSARA_API_KEY排除且其他真实内容可见；
- no total handler/session/history cap。

### 14.4 Controls

- each of 11 events happy path；
- deny/block/allow precedence；
- invalid conflicting control fail-open；
- plain stdout only accepted events；
- updatedInput invoke=0；
- background control impossible；
- canonical ToolResult never rewritten；
- SessionEnd observe-only。

### 14.5 Lifecycle

- direct/queued prompt Hook exactly once；ACK-confirmed existing row Hook=0；
- PreTool before attempt；Permission only ASK；Post after FULL settlement；
- meta MCP underlying identity exactly once；
- PreCompact before summary、Post after adoption/install；
- ROOT-only SessionStart/End；
- EXPLICIT/INFERRED SubagentStop unified；
- Stop/SubagentStop one continuation then terminal；
- cancellation/failure paths do not inventHooks。

### 14.6 Context

- one candidate per compile；
- source-neutral renderer不写Plugin假设；
- FULL/COMPACT complete-entry behavior；
- aggregate omission不阻断provider；
- CAS conflict discards batch；
- scope isolation/background loss；
- trust=`UNTRUSTED_OBSERVATION`且不进入SYSTEM。

### 14.7 Architecture/oracle

- `hooks/*`不导入`plugins/*`；
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
2. WORKSPACE PreToolUse真实deny一次Tool并让模型恢复；
3. PermissionRequest真实allow/deny exact request；
4. PostToolUse读取真实Round 7.1 public projection并feedback；
5. background output在下一safe point进入真实prompt；
6. mid-turn compaction触发Pre/PostCompact与SessionStart(compact)；
7. Round 10 worker触发SubagentStart/Stop；
8. 修改definition后old trust变MODIFIED，reload后不再执行；
9.真实记录Hook stdin/stdout/stderr、provider prompt与model reply，只排除`PULSARA_API_KEY`。

---

## 15. Definition of Done

1. USER/WORKSPACE `hooks.json`是可用的真实产品路径；
2. 11项event均接到exact owner seam；
3. Hook engine完全独立于Plugin；
4. trust/reload/modified状态闭合；
5. one dispatcher/executor/output parser/context source；
6. same-epoch strict prefix成立；
7. Hook output不成为SYSTEM、canonical row或permission snapshot；
8. no durable Hook execution machinery；
9. no arbitrary total/lifetime cap；
10. no redundant fingerprints/maps；
11. no schema/event/job/guard/relation增长；
12. targeted/full/PostgreSQL/architecture与real dogfood全部通过；
13. Round 9.3只需增加Plugin producer，不修改本轮runtime semantics。

---

## 16. 最终冻结

Round 9.2激活后的产品语义：

> Pulsara用户可以在`${PULSARA_HOME}/hooks.json`或exact workspace `.pulsara/hooks.json`中声明Codex-compatible command Hooks。用户审阅并trust exact normalized definitions后，唯一process-local `KernelHookDispatcher`在11项lifecycle seam执行它们；successful control只影响当前合法operation，informational output只作为append-only、source-neutral、untrusted `HOOK_CONTEXT`进入模型。Hook不拥有canonical conversation、Tool、permission、compaction或subagent authority，不增加durable execution recovery，也不改变同epoch SYSTEM/tools。未来Plugin只向这套subsystem贡献definitions与source environment，不复制任何Hook engine。
