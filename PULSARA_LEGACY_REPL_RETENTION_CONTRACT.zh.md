# Pulsara Frozen Legacy REPL Retention Contract

> 状态：FROZEN LEGACY SURFACE；maintenance-only，无计划删除日期
> Requirement namespace：`TUI-COMPAT-*`
> 唯一owner：现有prompt_toolkit顺序式REPL的保留范围、禁止扩张和隔离规则
> 未来一等Terminal入口：Bubble Tea v2 client（S0 feasibility为PASS、S1只读纵切与S2 live observation/page/reconnect已实施；S3–S6、Go production packaging与默认TTY activation尚未实施）

## 0. 定位

`pulsara host repl`保留作为历史兼容入口，但它不是：

- Bubble Tea的fallback；
- production-equivalent terminal client；
- non-TTY机器协议；
- 新UX能力的承载面；
- Terminal Client Protocol的reference implementation。

它的状态是“显式、冻结、maintenance-only”。保留原因是历史使用、debug/recovery便利和低删除收益，而不是继续投资。

## 1. 当前代码真值

- CLI dispatch位于`src/pulsara_agent/cli.py`的`host repl`分支。
- `PromptSession`、history和redirected stdin fallback位于`src/pulsara_agent/repl.py`。
- 当前REPL直接使用`HostCore`/`HostSession`执行open/resume、turn、approval、plan、MCP、mode、stop与compaction。
- 主循环在`await run_turn()`期间不读取输入，因此不具备真正live stop/steer/follow-up。
- `:mcp-input <json>`进入普通prompt history，不满足新的secret interaction边界。

## 2. Retention policy

### TUI-COMPAT-POLICY-001 显式入口

保留命令：

```text
pulsara host repl
```

Bubble Tea启动、协议或binary失败时不得自动执行该命令。错误必须明确报告，不得让用户误以为能力无损降级。

### TUI-COMPAT-POLICY-002 Maintenance scope

允许修改：

- security fix；
- durable data integrity fix；
- Python/prompt_toolkit compatibility；
- shared Host/Application Service hard cut所需的机械适配；
- test fixture与diagnostic修复；
- 明确能力降级或secret拒绝提示。

禁止修改：

- 新command；
- full-screen layout；
- live transcript/activity；
- durable queue、follow-up或steer；
- typed MCP form/private URL UI；
- sidebar、command palette、mouse或theme；
- Bubble Tea feature parity；
- 独立runtime semantics。

### TUI-COMPAT-POLICY-003 No removal date

本契约不设置删除deadline。未来删除必须单独提案、统计实际使用并提供migration path；当前hard cut不以删除REPL为完成条件。

## 3. Capability manifest

### 3.1 Retained core

以下能力保持当前顺序式语义：

- open initial session；
- list/resume/continue session；
- synchronous single-turn text submission；
- bounded final text/result rendering；
- explicit detach/quit；
- explicit conversation close；
- current status/mode command；
- current manual compaction command；
- existing plan/approval commands，在shared application service仍支持且不要求新secret UI时保留；
- MCP cancel，在不传递secret response时保留。

“保留”不表示得到Bubble Tea的新streaming、queue、projection或reconnect行为。

### 3.2 Unsupported

- active run期间继续编辑或提交；
- real-time stop key routing；
- follow-up queue；
- steer safe point；
- queue item cancel、cancel-confirmed replacement与reconnect projection；
- semantic tool grouping；
- live subagent/MCP/process activity；
- attachment reconnect cursor；
- multiple observer UX；
- controller takeover UX；
- secret MCP form response；
- private URL display/consent；
- futureTerminal protocol extensions。

### 3.3 Existing MCP input command

` :mcp-input <json>`不能继续作为secret-bearing production resolution：

- command line可能进入`FileHistory`；
- 普通Python string/dict会复制secret；
- 没有attachment-bound secret lease；
- 无法满足private URL/form-specific UX。

在Bubble Tea S4 activation时：

- pending interaction若需要form value/private URL/secret response，Legacy REPL typed reject并提示使用`pulsara tui`；
- `:mcp-cancel`可继续使用shared cancellation service；
- 不新增无history secret reader来追求兼容；
- 旧命令代码可保留到一次性隔离重构，但production guard必须阻止secret提交。

## 4. 与新application boundary的关系

### TUI-COMPAT-BOUNDARY-001 Shared services

Legacy不走Protobuf，但必须逐步改为调用与Gateway相同的有限application services：

```text
TerminalSessionLifecycleService
TerminalPromptSubmissionService       # synchronous ordinary prompt only
TerminalRunControlService             # existing supported operations only
TerminalInteractionResolutionService  # non-secret supported subset
TerminalSessionQueryService
```

Legacy不得直接创建新的EventLog candidate、queue mutation、interaction carrier或secret lease。

History capacity fence同样作用于Legacy：`TerminalPromptSubmissionService`使用Foundation签发的growth quote/reservation与capacity decision；Legacy不得根据entry count、tail或terminalization maintenance reserve自行重算。返回`SESSION_HISTORY_ROTATION_REQUIRED | HISTORY_TREE_CAPACITY_EXHAUSTED | HISTORY_GROWTH_QUOTE_EXCEEDED | CAPACITY_POLICY_DRIFT | RESERVATION_AUTHORITY_CONFLICT`后，Legacy只能显示bounded maintenance/reconciliation message并停止向旧session提交ordinary prompt。它可以通过既有session lifecycle入口显式新建session，但不获得自动迁移queue/interaction/secret/controller state或借用terminalization maintenance reserve的能力。

### TUI-COMPAT-BOUNDARY-002 Controller lease

Legacy interactive session必须取得统一Python `InteractiveControllerLease`：

- 只有controller available时可attach；
- 已有Bubble Tea controller时fail closed，不隐式takeover；
- Legacy detach/exit释放lease；
- lease expiry/revoke后后续mutation fail closed；
- Legacy不需要Protobuf attachment，但其internal identity必须加入command audit。

这项机械适配是防止legacy成为mutation后门，不是为其增加新功能。

### TUI-COMPAT-BOUNDARY-003 Projection

Legacy可以继续读取已有bounded final result和显式status query，不要求消费Terminal projection snapshot/delta。若底层Host旧query被删除，只能适配到`TerminalSessionQueryService`，不得在Legacy模块中重建event fold或SQL scan。

## 5. 一次性隔离改造

为了让“No growth”可机器验证，Bubble Tea production activation前执行一次行为保持迁移：

```text
src/pulsara_agent/host/legacy_repl.py
  _host_repl
  command parsing/help
  legacy rendering helpers

src/pulsara_agent/repl.py
  ReplPrompt
  BasicReplPrompt
  InteractiveReplPrompt
  history construction
```

`cli.py`只保留argument dispatch。迁移不得新增command或改变现有non-secret semantics。

## 6. Import与growth gate

### TUI-COMPAT-GATE-001 prompt_toolkit allowlist

Production `prompt_toolkit` import只允许：

- `src/pulsara_agent/repl.py`
- `src/pulsara_agent/host/legacy_repl.py`（仅在确有类型/exception需要时）

Foundation、Protocol、Gateway、Runtime和Bubble Tea build不得依赖它。

### TUI-COMPAT-GATE-002 Command vocabulary freeze

冻结activation commit时的legacy command set和help fingerprint。AST gate拒绝新增command literal/branch。删除或security-disable既有command需要更新本契约和compatibility test，但不要求替代功能。

### TUI-COMPAT-GATE-003 Concrete dependency freeze

隔离后记录Legacy模块的allowed application-service imports。禁止新增：

- RuntimeSession/internal manager；
- EventLog/PostgreSQL；
- queue repository/companion；
- MCP secret store；
- Go/protocol generated types；
- renderer-neutral reducer internals。

## 7. User-facing policy

启动banner明确：

```text
Pulsara Legacy REPL (maintenance-only)
For the first-class Terminal UI, use: pulsara tui
```

措辞不宣称即将删除，也不在每一轮重复警告。遇到unsupported能力时返回具体提示，不使用generic“upgrade required”。

Bubble Tea不可用时：

- `pulsara tui`返回typed installation/version error；
- 文档可告知用户显式运行Legacy REPL进行有限操作；
- launcher不得自动切换或复用原command input。

## 8. non-TTY与automation

Redirected stdin继续保持当前best-effort顺序式行为，但不冻结为stable machine protocol：

- 不新增JSONL；
- 不保证完整typed interaction；
- 不输出alternate-screen/cursor control；
- secret interaction拒绝；
- future automation protocol必须另立契约和入口。

## 9. Tests

### TUI-COMPAT-TEST-001 Smoke

- TTY ordinary single turn；
- redirected stdin ordinary turn；
- list/resume/detach/close；
- current non-secret approval/plan path；
- explicit banner/help；
- no automatic launch from`tui` failure。

### TUI-COMPAT-TEST-002 Boundary

- Bubble Tea controller active时Legacy attach rejected；
- Legacy lease revoke后mutation rejected；
- MCP secret form/private URL rejected withouthistory write；
- no queue/steer command；
- no prompt_toolkit import outsideallowlist；
- no direct EventLog/RuntimeSession/secret repository mutation。
- history growth decision/rotation/hard exhaustion后ordinary prompt fail closed，显式新建session不迁移旧session state；Legacy不重算quote或reserve；

### TUI-COMPAT-TEST-003 No-growth

- command AST observation set不增长；
- application-service capability set不增长；
- help fingerprint只按explicit contract change更新；
- legacy tests不被复制为Bubble Tea parity tests。

## 10. Definition of Done

1. Legacy REPL被所有文档称为Frozen Legacy/maintenance-only，不再称fallback。
2. `pulsara host repl`保持显式可用且无自动降级路径。
3. prompt_toolkit不被升级为full-screen Application。
4. Legacy无queue、steer、semantic transcript和secret interaction新能力。
5. Secret-bearing MCP input不能进入FileHistory或普通JSON command。
6. Legacy mutation服从shared controller lease与有限application services。
7. Legacy模块与CLI完成一次性隔离，command/import growth有AST gate。
8. Existing retained non-secret behavior有smoke tests。
9. Foundation/Protocol/Bubble Tea均不依赖Legacy模块。
10. 保留该入口不阻塞Bubble Tea成为默认一等TTY客户端。
11. Legacy服从shared history growth quote/reservation与capacity fence；rotation/hard exhaustion后不能继续向旧session提交ordinary prompt、重算decision或借用terminalization maintenance reserve。
