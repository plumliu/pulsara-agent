# Pulsara Bubble Tea v2 Terminal Client Hard-Cut 实施规格

> 状态：S0 IN PROGRESS（2026-08-01：隔离自动化smoke、20次CPU/RSS/render-jitter基线与真实远程SSH host通过，真实IME/attached tmux/非native clean runner仍待人工证据）；S1-S6、production接线与默认TTY activation继续`DEFERRED`。
> Requirement namespace：`TUI-BT-*`
> 唯一owner：Go Terminal client的TTY、Model/Update/View、layout、composer与distribution
> Wire contract：`PULSARA_TERMINAL_CLIENT_PROTOCOL_CONTRACT.zh.md`
> 产品行为：`PULSARA_TERMINAL_UI_UX_RESEARCH_AND_DESIGN.zh.md`

## 0. 技术选择

Pulsara把Terminal UI作为长期一等产品入口。V1选择：

- `charm.land/bubbletea/v2`，S0基线pin `v2.0.6`；
- `charm.land/bubbles/v2`，S0基线pin `v2.1.0`；
- `charm.land/lipgloss/v2`，按同一S0 lockfile冻结reviewed stable版本；
- Protobuf generated Go client；
- POSIX Unix domain socket；
- darwin/linux amd64/arm64。

Bubble Tea v2提供declarative `tea.View`、terminal capability/key enhancements、paste messages和独立renderer；Bubbles v2提供textarea、viewport、spinner等基础组件。官方来源：

- https://github.com/charmbracelet/bubbletea/releases
- https://github.com/charmbracelet/bubbles/releases
- https://github.com/charmbracelet/bubbletea/blob/main/UPGRADE_GUIDE_V2.md

选型依据是process/ownership和长期维护，不是star数量。S0失败时重新评估，但不建设prompt_toolkit/Textual过渡full-screen。

## 1. 唯一ownership

### 1.1 Go client拥有

- real TTY stdin/stdout；
- Bubble Tea program lifecycle；
- composer draft与local edit history；
- viewport scroll/follow-tail/unseen count；
- selection/copy；
- cell expand/collapse preference；
- terminal width/height、wrap cache和layout；
- colors、styles、borders、spacing；
- key routing、mouse和clipboard；
- current attachment-local ephemeral secret input/display state；
- connection presentation与reconnect UX。

### 1.2 Go client不拥有

- RuntimeSession、run、tool、queue或interaction authority；
- EventLog sequence interpretation；
- semantic cell kind/group/severity；
- command success；
- durable transcript；
- MCP continuation、secret validation或expiry；
- session close/drain；
- projection revision生成。

Go不得接收、import或镜像Python `AgentEvent`、`RawStoredEventEnvelope`、EventLog schema、PostgreSQL row或Host manager。

## 2. Go module与目录

```text
clients/terminal/
  go.mod
  go.sum
  cmd/pulsara-tui/main.go
  internal/app/
    model.go
    update.go
    view.go
    messages.go
    commands.go
    keymap.go
  internal/client/
    connection.go
    bridge.go
    supervision.go
  internal/protocol/                # generated, no manual edits
  internal/presentation/
    snapshot.go
    delta.go
    cells.go
    activity.go
  internal/components/
    transcript/
    composer/
    interaction/
    status/
    sidebar/
    notification/
  internal/secret/
    state.go
    buffer.go
  internal/testkit/
  testdata/
```

`cmd/pulsara-tui`只做args、transport/bootstrap和exit code。所有state transition在`internal/app`，所有wire mapping在`internal/client`，所有render mechanics在components。

## 3. S0 feasibility gate

### TUI-BT-S0-001 Spike边界

S0是可删除spike，不连接production Runtime/EventLog，不复用真实secret。它使用fake Protobuf snapshot/delta stream和Python parent probe验证framework/process可行性。

### TUI-BT-S0-002 Required matrix

| 能力 | 必须验证 |
|---|---|
| CJK/IME | 中文输入、候选提交、中文标点、mixed CJK/ASCII、emoji/wide rune |
| Cursor | insert/delete、home/end、跨行移动、wide rune前后定位 |
| Textarea | 多行、动态高度、Shift+Enter capability/fallback、undo边界 |
| Paste | bracketed paste、小/大payload、换行、取消、render不冻结 |
| Concurrent stream | 20Hz/100Hz fake delta期间持续输入，无draft丢失 |
| Resize | 连续resize、80/120/160 columns、极窄/极矮窗口 |
| Multiplexer | tmux主流版本，alternate screen、mouse/paste capability |
| SSH | locale、TERM、latency、disconnect和reconnect |
| Crash | normal quit、panic、SIGTERM、parent kill child、SIGKILL后parent recovery |
| Packaging | darwin/linux amd64/arm64 binary、checksum和launch |
| Signal | Ctrl-C/Esc不误杀Python；child exit不取消probe operation |

Bubble Tea `v2.0.6`包含wide-character修复，因此S0必须锁定该版本或更新版本，不得回退到早期v2而省略回归。

### TUI-BT-S0-003 PASS标准

- 无CJK cursor corruption或输入丢失；
- 1 MiB paste不会阻塞event loop超过冻结阈值，且会转入large-paste UX而非直接常驻textarea；
- 100Hz server输入下keypress p95 latency满足标定阈值；
- client panic/SIGTERM后terminal mode恢复；SIGKILL由Python parent检测并执行best-effort emergency restore；
- tmux/SSH至少各一组真实记录通过；
- 四个target binary可在clean runner启动；
- signal ownership探针证明Go UI action不会直接cancel Python run owner。

任一阻断项失败即停止S1+ production接线，并在产品基线记录结论。

### TUI-BT-S0-004 当前实施与证据

S0 disposable module位于`clients/terminal/spikes/s0/`。它拥有独立`go.mod`和fake Protobuf schema，只通过继承FD连接Python parent probe，不import或连接production Runtime、EventLog、Terminal client protocol或secret owner。该目录不得成为S1 production module的compatibility facade；S0完成后可以整体删除。

2026-08-01自动化smoke结果：

| 能力 | 当前结论 | 证据边界 |
|---|---|---|
| dependency pin | PASS | Bubble Tea `v2.0.6`、Bubbles `v2.1.0`、Lip Gloss `v2.0.5`进入`go.mod`/`go.sum`和runtime build info |
| CJK/wide rune | PARTIAL | 已组合UTF-8、emoji、wide-rune cursor/edit通过；真实macOS IME pre-edit/candidate提交仍须人工 |
| textarea/cursor | PASS | multiline、dynamic height、Home/End/跨行与wide-rune边界、Shift+Enter typed path及Ctrl+J fallback通过 |
| undo | FEASIBLE WITH CLIENT OWNER | Bubbles textarea无native undo stack；spike的bounded client-owned wrapper通过，S3不得把undo authority假设给framework |
| paste | AUTOMATED PASS / MANUAL PARTIAL | small、cancel boundary、1 MiB bracketed paste和非resident large-paste path通过；真实terminal手工取消仍待记录 |
| concurrent stream | PASS | fake Protobuf 20Hz/100Hz各20次、合计40条轨迹均保持完整draft与exact delta count；per-run keypress p95的跨run p95分别为20.227ms/20.418ms |
| CPU/RSS/render cadence | PASS | 每档20次；1s warm-up、3s active window、10Hz process sampling、每轮20个keypress probe；20Hz/100Hz CPU average跨run p95为4.500%/10.039%，RSS全局peak为15.578/16.828MiB，physical renderer write interval p99的跨run p95为87.701ms/45.035ms |
| resize | PASS | unit与tmux覆盖80/120/160列、12x4极端窗口和连续resize |
| tmux | AUTOMATED PASS / ATTACHED PENDING | tmux 3.6a detached PTY验证alternate screen、bracketed paste和restore；真实attached client/IME仍待记录 |
| SSH | REAL REMOTE PASS | macOS OpenSSH → Windows OpenSSH/ConPTY → WSL2 Linux x86_64真实主机链路通过；UTF-8、`TERM=xterm-256color`、CJK、alternate-screen、20次keypress p95/p99 98.972/100.636ms、abrupt disconnect后remote process退出、parent emergency restore和reconnect均已验证 |
| crash/signal | PASS | normal、panic、SIGTERM、SIGINT均由child恢复；SIGKILL由Python parent emergency restore；parent operation持续存活 |
| packaging | PARTIAL | darwin/linux amd64/arm64交叉构建、checksum、dependency inspection和native darwin/arm64 launch通过；其余target clean runner launch待补 |

机器证据与复现入口：

- `clients/terminal/spikes/s0/evidence/darwin-arm64-pty.json`
- `clients/terminal/spikes/s0/evidence/darwin-arm64-tmux.json`
- `clients/terminal/spikes/s0/evidence/docker-ssh-arm64.json`
- `clients/terminal/spikes/s0/evidence/real-ssh-plumliuwin-wsl2-amd64.json`
- `clients/terminal/spikes/s0/evidence/cross-build.txt`
- `clients/terminal/spikes/s0/evidence/darwin-arm64-performance.json`
- `clients/terminal/spikes/s0/evidence/darwin-arm64-performance.md`
- `clients/terminal/spikes/s0/scripts/run_automated.sh`
- `clients/terminal/spikes/s0/scripts/run_performance.sh`
- `clients/terminal/spikes/s0/scripts/real_ssh_smoke.sh`

性能基线固定使用nearest-rank percentile；renderer cadence量的是Bubble Tea最终PTY output writer的non-empty physical write，不把Python PTY read分包或terminal display refresh冒充framework frame。Feasibility gate冻结为：keypress p95/p99不超过50/100ms、stream delivery p95不超过10ms、renderer write interval p99不超过100ms、jitter(`p95 - p50`)不超过50ms、CPU average跨run p95在20Hz/100Hz下不超过25%/50%单核、CPU interval peak不超过150%、RSS peak不超过128MiB、每轮首尾quartile median growth绝对值不超过16MiB；跨run`p95 - p05`允许方差分别为keypress p95不超过25ms、CPU average不超过20 percentage points、RSS steady p95不超过16MiB、renderer interval p95不超过25ms。40条轨迹的correctness invariant与统计gate全部通过，raw per-run summary和全部check保存在JSON证据中；这些是S0 feasibility guard，不是production SLO。

真实SSH fixture上传现有`linux/amd64` artifact并核对SHA-256，不把Windows加入production packaging target；远端运行面明确位于WSL2。远程latency gate独立冻结为first frame不超过3s、20个probe的keypress p95/p99不超过150/250ms；不错误复用本机50/100ms gate。测试结束后对remote process、WSL `/tmp` binary与Windows staging file逐项验证不存在。该自动化关闭真实host/network/PTY/reconnect项，但不冒充macOS真实IME候选窗口或terminal emulator视觉检查。

当前总判定仍为`PARTIAL`，不得据此启动S1 production接线。只有真实IME、attached tmux与四target clean-runner launch补齐后，才能把S0改为`PASS`。

## 4. Bubble Tea lifecycle

### TUI-BT-APP-001 Model

```text
AppModel
  connection: ConnectionModel
  attachment: AttachmentModel
  session: SessionProjectionModel
  history: PresentationHistoryModel
  activity: OperationalActivityModel
  composer: ComposerModel
  interaction: InteractionModel
  queue: QueueViewModel
  status: StatusModel
  sidebar: SidebarModel
  notifications: NotificationModel
  modal: ModalModel
  terminal: TerminalCapabilityModel
  preferences: ClientPreferenceModel
```

`PresentationHistoryModel`只保存server已排序的bounded `PresentationHistoryRankedEntry`、active head identity、一个`latestRootCursorPair`以及按root fingerprint隔离的bounded `pinnedRootPageStates`。Latest pair服务follow-tail/current-root读取；pinned state只服务已打开的旧immutable root页面，绝不能覆盖latest pair。Stable entry identity使用placement key + entry ID；display rank只在其root/active-head basis内有效。Client installation必须exact resolve root声明的placement-key contract ID/version/fingerprint，并验证V1固定75-byte framing；未知historical binding不能按opaque bytes继续排序。`OperationalActivityModel`只保存operational generation/cursor与closed activity oneof；它与history不共享entry map、placement key、revision或cache key。Model不保存generated Protobuf message、socket、goroutine、Python identity或secret beyond current secret component。

### TUI-BT-APP-002 Update purity

`Update(msg)`：

- 只处理typed Go messages；
- 更新model并返回`tea.Cmd`；
- 不做socket read/write、file I/O、blocking wait或domain validation；
- 不从display text推断stable identity；
- unknown required message进入protocol error state；
- stale revision/attachment message不修改model。

### TUI-BT-APP-003 View purity

`View()`只从model构造`tea.View`。禁止I/O、timer allocation、protocol call、history page read和mutable global state。AltScreen、cursor、mouse、bracketed paste和keyboard enhancements通过v2 declarative View设置。

### TUI-BT-APP-004 Transport bridge

一个client-owned bridge读取protocol connection，把wire message映射为typed `tea.Msg`再送入Program。Bridge：

- 不解释domain events；
- 有bounded local queue；
- program done后停止send；
- local overflow触发client projection invalid/GAP handling；
- command write由`tea.Cmd`请求client service，不在Update内执行；
- close时先停止new send，再等待bridge physical exit。

## 5. Connection与startup UX

### TUI-BT-CONN-001 First frame

Bubble Tea shell必须在Host dependencies fully ready前显示，状态依次为：

```text
starting client
connecting gateway
attaching session
loading snapshot
ready | recoverable error | fatal incompatibility
```

不显示无限spinner。每个阶段有bounded elapsed和typed retry/quit action。

### TUI-BT-CONN-002 Reconnect

Connection loss不清空last confirmed snapshot，UI进入stale/read-only state。Reconnect：

1. 创建新connection/attachment generation；
2. 旧controller/secret state立即revoke/clear；
3. negotiate protocol；
4. request snapshot或合法delta catch-up；
5. exact install后恢复interaction；
6. composer ordinary draft可保留，secret draft不得保留。

### TUI-BT-CONN-003 Version failure

Major mismatch、missing required capability或client binary incompatibility显示明确版本信息并退出，不自动启动Legacy REPL。

## 6. Transcript viewport

### TUI-BT-VIEW-001 Resident model

Go保存当前bounded unified history ranked views和derived wrapped-line cache。它必须按server返回的placement-key ordered vector显示，不按root-local display rank、cell kind、source sequence或arrival time重新排序transcript/audit。Display rank只用于当前basis的行号/定位，不能成为stable map key。Cache key至少覆盖：

```text
cell_id
cell_semantic_revision
history_entry_id
placement_key_fingerprint
root_local_display_rank
rank_basis_fingerprint
presentation_history_active_head_fingerprint
available_width
display_density
theme typography contract
expanded preference
```

Resize只invalidate受影响wrap cache，不请求server重新解释semantic cell。

`DurableHistoryCell`与`OperationalActivityCell`使用两个不相交generated oneof mapper。`AuditCell.audit_kind=RUN_LIFECYCLE`进入ordinary durable history renderer；客户端不定义`RunLifecycleCell`、unknown-cell fallback或从RunStart/RunEnd自行推导lifecycle。Unknown required branch进入protocol incompatible state。

### TUI-BT-VIEW-002 Scroll

- 默认follow-tail；
- 用户向上滚动立即退出follow-tail；
- 新delta增加unseen count但不移动viewport；
- 显式jump/end恢复follow-tail并清零unseen；
- 接近page boundary时发bounded history request；
- latest history cursor pair只绑定active head中的confirmed immutable root/generation与anchor，不保存tail或direction；
- old-root page cursor按root fingerprint留在bounded pinned state；它可以继续浏览same retained root，但不能用于follow-tail、jump-to-end、current-root retry或近期cache eviction后的rehydration；
- history cursor与request都不保存feed kind；client没有transcript/audit双cursor、双cache或merge queue；
- 每次scroll/page action显式构造`HistoryPageRequest.direction = before | after`，该request字段是方向的唯一真源；
- client不从cursor、anchor、before/after cursor slot或本地method名推导方向，也不在cursor cache中附加direction；
- page loading不冻结composer；
- `HistoryPageData`只有在cursor confirmed-root identity、anchor placement key、validated direction与outstanding request exact join后才可按server返回顺序扩展history并更新before/after cursor；display rank必须绑定同一root且不得跨root缓存；
- `HistoryCursorStale`不得显示为“没有更多历史”：有same-anchor replacement proof时用新cursor重试，否则进入rebase；
- `PresentationHistoryRootAdvancedFrame`只有在base projection revision、previous active-head fingerprint、resulting active head、latest cursor pair、root relation、consumed segment prefix、retained concurrent segment suffix与resident transition全部exact join时才原子安装；previous latest pair转为pinned，new pair成为唯一latest。Resulting active head可以有non-empty tail，其source/segment/mutation identity必须与retained suffix相等。`RESIDENT_ENTRIES_UNCHANGED`要求before/after vector fingerprint相同，并把整条resident vector原子rebind到resulting active-head rank basis；`BOUNDED_ORDERED_RESIDENT_CHANGES`按ordered upsert/remove一次性应用并重算after accumulator/rank basis；`RESIDENT_HISTORY_REBASE_REQUIRED`清除受影响resident cache并使用attachment-bound token。任一字段、count、bytes、previous fingerprint或resulting accumulator不匹配均fail closed；
- checkpoint I/O期间收到的live append/noop继续留在model；root-advanced只消费server证明已覆盖的segment prefix并保留frame声明的segment suffix。Noop-only suffix的mutation count可以为0，但positive segment count与advanced source accumulator仍必须安装；client不得看到new root后自行清空tail。Rewrite/retirement post-cut conflict只能按server rebase branch处理；
- `SESSION_HISTORY_ROTATION_REQUIRED`或command携带的request-specific capacity decision禁止对应submit/follow-up/steer并显示显式“新建会话”action；client只显示server quote/decision，不自行把terminalization maintenance reserve加进ordinary projected count，也不缩小growth quote后重试。Active run/pending interaction的terminalization仍可继续。`HISTORY_TREE_CAPACITY_EXHAUSTED | HISTORY_GROWTH_QUOTE_EXCEEDED | CAPACITY_POLICY_DRIFT | RESERVATION_AUTHORITY_CONFLICT`进入typed read-only/reconciliation state，不做本地history eviction或隐藏旧entry；
- `AuthorityAdvanceFrame`若改变confirmed root fingerprint必须fail closed；client不得从普通delta或authority frame猜测new latest cursor；
- latest root advanced到达时，已打开的旧root history page/cursor cache仍可继续向上滚动；old-root empty-after-page只结束该pinned snapshot，不能把new latest root标成history end；
- root-advanced frame丢失或revision跳跃时进入GAP/snapshot rebuild，不以old pair继续读取current history；
- `HistoryRebaseRequired`丢弃受影响的paged cache并使用server token请求bounded snapshot/root，不能按旧cell ID猜位置；
- `HistoryReconciliationRequired`保留已确认resident cells并显示typed unavailable/retry state；
- page gap/revision conflict触发snapshot rebuild。

### TUI-BT-VIEW-003 Selection/copy

Selection是client-local，不进入server snapshot。Copy默认只包含event-safe rendered public content；secret view、private URL和redacted blocks不进入copy-all。Explicit private URL copy需要当前secret lease和独立confirmation UX。

## 7. Composer

### TUI-BT-COMP-001 Ordinary draft

使用Bubbles v2 textarea，支持：

- multiline；
- dynamic bounded height；
- history navigation；
- command completion；
- paste classification；
- draft persistence across ordinary reconnect；
- active run期间继续编辑。

Draft是client-local，不自动提交、不进入server projection。

### TUI-BT-COMP-002 Submit

Enter语义由explicit mode/key capability决定，不能依赖无法区分的terminal key。Submit前：

1. freeze exact UTF-8 content；
2. create stable command ID；
3. choose ordinary prompt/follow-up/steer intent only throughavailable server capability；
4. send command；
5. retain frozen pending submission untilreceipt；
6. SUCCEEDED后clear matching draft；
7. reject/conflict保留可编辑内容；
8. disconnect用same command ID query，不生成新submission。

### TUI-BT-COMP-003 Large paste

Large paste不直接常驻普通textarea或projection。Client显示bounded placeholder与bytes/lines，准备content command由server/Artifact foundation拥有。Threshold由S0 latency、protocol cap和artifact cost标定。

### TUI-BT-COMP-004 History

Ordinary prompt history只保存用户明确提交且policy允许的event-safe text。Secret interaction、private URL、MCP form response和protocol token永不进入history、autosuggest或completion。

## 8. Key routing

### TUI-BT-KEY-001 Hierarchy

```text
active secret interaction
  -> active typed interaction
  -> modal/command palette
  -> transcript selection/search
  -> composer
  -> global app
```

Esc/Ctrl-C必须按层处理，禁止任何组件各自直接`tea.Quit`。

### TUI-BT-KEY-002 Stop

Active run时Esc第一次显示stop intent/confirmation或直接按冻结policy发送stable Stop command；重复按键查询/展示same command，不重复send physical stop。Terminalization pending时UI显示真实state，不假装已经停止。

### TUI-BT-KEY-003 Quit/detach

Quit、detach、close conversation是不同command。普通窗口关闭或Ctrl-D默认detach client，不隐式close durable conversation。Active run继续由Python owner持有。

## 9. Typed interactions

### TUI-BT-INT-001 View stack

Interaction view优先于ordinary composer，但必须保存ordinary draft。支持closed branches：

- tool approval；
- plan question；
- plan exit；
- MCP form；
- MCP private URL consent。

Client按server-provided stable IDs/actions渲染，不从文本猜resolution。

### TUI-BT-INT-002 Stale protection

每个resolution command绑定interaction ID/generation和controller generation。收到interaction replace/clear后，旧modal所有action disabled；late receipt只更新matching pending command。

## 10. Secret client state

### TUI-BT-SECRET-001 Separate component

Secret state不得复用ordinary textarea/history/model field：

```text
SecretInteractionState
  lease identity
  request key
  mutable input buffer
  display/reveal state
  local expiry
  submission command identity
```

它拥有constant redacted `String/GoString`，不得被generic debug dump、snapshot serializer或analytics读取。

### TUI-BT-SECRET-002 Buffer policy

- no history；
- no autosuggest；
- no completion；
- no undo beyond current ephemeral operation；
- no copy-all；
- no ordinary reconnect retention；
- release/detach/takeover时best-effort overwrite mutable buffers并drop references。

Go strings和terminal display无法绝对零化；不得做更强声明。

### TUI-BT-SECRET-003 Private URL

URL只通过one-shot secret reveal进入current controller。UI显示完整URL及server-provided safety context，禁止prefetch。Open-browser与copy都是显式action；离开interaction立即droplocal reference，底层continuation仅由Python terminal resolution/cancel/expiry删除。

## 11. Queue UX

### TUI-BT-QUEUE-001 Server authority

Client queue model只保存server projection和pending command receipt。Queued状态只在server SUCCEEDED/FULL domain outcome后显示；local draft或socket send成功不等于queued。

### TUI-BT-QUEUE-002 Intent

- ordinary idle submit；
- follow-up next run；
- explicit steer exact safe point；
- cancel existing item；
- “编辑”或“改投递模式”只是client UX：先确认旧item cancel成功，再以新的submission/command ID提交replacement。

Steer不可用或错过boundary时显示typed rejection；client不得静默改成follow-up。

Client不得呈现server-side atomic edit/reclassify。Cancel与replacement分别显示receipt：若cancel成功而replacement失败，旧item保持cancelled，draft保留供用户修正/重试；reconnect分别query两个原command ID，不得自动复活旧item或重复submit。

### TUI-BT-QUEUE-003 Reconnect

Reconnect snapshot替换local queue projection。Pending command按same ID query；client不把offline draft伪装成accepted item。

## 12. Semantic transcript与status

### TUI-BT-SEM-001 Display mechanics

Client消费Python semantic cell/group，不重新分类tool。它可以：

- compact/verbose；
- expand/collapse；
- width-dependent inline/block layout；
- stable elapsed animation；
- error emphasis；
- source ID navigation。

不可隐藏server标记`must_show`的error/terminal cell。

### TUI-BT-SEM-002 Status/sidebar

Status只读取last snapshot/delta logical values。Sidebar是responsive optional view，关闭后所有关键动作仍可由main surface完成。Renderer callback不得发I/O。

## 13. Terminal lifecycle与recovery

### TUI-BT-LIFE-001 Declarative terminal state

`View()`声明AltScreen、cursor、mouse、paste和keyboard enhancements。Client不在多个component中imperative toggle terminal modes。

### TUI-BT-LIFE-002 Exit restore

Normal return、error、panic和SIGTERM路径必须经single teardown owner恢复terminal。Python parent检测unexpected child exit后执行S0验证过的best-effort emergency restore并报告typed client failure。SIGKILL后不承诺child defer执行。

### TUI-BT-LIFE-003 Bounded exit summary

退出alternate screen后最多输出bounded session ID/status/reconnect hint，不回灌完整transcript。

## 14. Distribution

### TUI-BT-DIST-001 Binary

Binary名：`pulsara-tui`。Build metadata包含semantic version、commit、protocol major/minor、Go version和dependency lock fingerprint。

### TUI-BT-DIST-002 Targets

V1 required：

- `darwin/arm64`
- `darwin/amd64`
- `linux/amd64`
- `linux/arm64`

Windows不在V1，不能发布未测试binary。

### TUI-BT-DIST-003 Packaging decision gate

S0同时验证两种carrier，S1前冻结一个：

1. platform-specific Python wheels内置matching binary；
2. separately signed release asset加显式installer。

禁止production首次启动时静默从网络下载。无compatible binary时`pulsara tui`typed fail，不自动进入Legacy REPL。Development可通过`PULSARA_TUI_BIN`显式覆盖。

### TUI-BT-DIST-004 Supply chain

- reproducible build inputs；
- checksums/signatures；
- SBOM；
- license inventory；
- protocol compatibility gate；
- release asset smoke test on clean runner。

## 15. Vertical slices

| Slice | Go client交付 |
|---|---|
| S0 | disposable TTY/process/package spike |
| S1 | connection、unified history-root snapshot、real transcript viewport |
| S2 | delta、scroll、history、GAP/reconnect |
| S3 | composer、stable submit、stop、receipt/history |
| S4 | approval/plan/MCP views与secret state |
| S5 | follow-up/steer/cancel，以及cancel-confirmed后new-submit replacement UX |
| S6 | semantic grouping、status/sidebar、distribution/default activation |

每个slice必须与matching Python/protocol实现和cross-language integration test同PR或同一不可分割release train完成。

## 16. Testing

### TUI-BT-GATE-001 Pure model

- typed message -> model transition；
- stale revision/generation；
- key routing hierarchy；
- follow-tail/unseen；
- command pending/receipt/query；
- interaction replacement；
- reconnect ordinary draft retention与secret draft deletion。

### TUI-BT-GATE-002 View golden

- 80/120/160 columns；
- compact/verbose；
- CJK/emoji/wide rune；
- long unbroken text；
- errors/must-show；
- narrow/short terminal；
- reduced color/capability。

### TUI-BT-GATE-003 PTY

- paste、resize、signals、alternate screen restore；
- tmux/SSH recorded matrix；
- client panic/kill；
- Python parent surviveschild crash；
- terminal bytes contain noprotocol/secret diagnostics。

### TUI-BT-GATE-004 Cross-language

- generated Protobuf golden vectors；
- handshake/version mismatch；
- snapshot/delta/GAP；
- duplicate command query；
- controller takeover；
- secret lease revoke；
- output backpressure；
- history PAGE/STALE/REBASE/RECONCILIATION四branch；
- history cursor的wire/golden shape不含direction；`HistoryPageRequest.direction`逐项映射到server唯一`read_page()`调用，PAGE必须回显相同validated direction；
- history cursor/request不含feed kind，混合transcript/audit fixture只按server placement-key ordered ranked views渲染，不得按display rank之外的本地规则merge/reorder；
- durable history与operational activity oneof无共享branch；activity coalesce/drop不修改history root/revision；
- run lifecycle fixture只通过`AuditCell(RUN_LIFECYCLE)`，unknown/removed `RunLifecycleCell`必须fail closed；
- canonical leaf replacement/retirement fixture消费server stable placement key与anchor tombstone，未受影响suffix cache key不变，不按replacement sequence或arrival time移到尾部；
- root-advanced frame原子更新active head/latest pair并保留old pinned state；checkpoint期间并发append/noop只消费proved segment prefix并保留segment suffix；noop-only suffix必须推进source/segment identity而不伪造history mutation；old-root empty page不覆盖latest pair；
- resident transition三个wire branch覆盖exact unchanged、bounded ordered upsert/remove与typed rebase；malformed count/bytes/accumulator、wrong expected previous或wrong target root均fail closed；
- capacity golden逐项验证`confirmed + tail + active remaining reservations + requested quote`，terminalization maintenance reserve只出现在soft/hard policy invariant而不进入ordinary projected count；soft capacity fixture禁止对应ordinary submit并提供start-new-session action，已准入terminalization仍完成；hard exhausted与quote/policy/reservation reconciliation fixtures进入read-only state且不本地截断history；
- placement-key golden覆盖六个kind、left/right sentinel、uint bounds、exact 75-byte `PHK1` framing、unsigned lexicographic ordering与unknown historical binding fail closed；
- malformed root relation、same-revision rollover、AuthorityAdvance root change与丢失root-advanced frame分别fail closed/GAP rebuild；
- 同一directionless cursor分别发出before/after request时，server返回各自正确page，client不得因cursor cache污染而串向；
- queue cancel与replacement两个独立command receipt/query。

## 17. Definition of Done

1. S0完整通过并在产品基线记录证据。
2. Go client只依赖versioned protocol，不含Python domain vocabulary。
3. `Update/View`不执行I/O，transport bridge有明确close owner。
4. CJK/IME、wide rune、paste、resize、tmux与SSH达到冻结标准。
5. Streaming期间composer保持可编辑且不丢draft。
6. Client crash/detach不取消Python run。
7. Snapshot/delta/GAP/reconnect不重复或遗漏visible projection。
8. Mutation command稳定幂等，receipt丢失后可query。
9. Secret state不进入history/snapshot/log/replay，revoke后旧lease不可再用。
10. Queue状态只来自server authority，不由local send成功推断。
11. 80/120/160 columns及窄屏均无重叠或不可达关键动作。
12. darwin/linux四个binary和protocol compatibility release gate通过。
13. Bubble Tea成为默认TTY入口时，不存在prompt_toolkit full-screen实现或silent fallback。
14. Cursor stale/rebase从不显示为history end；client按typed branch重试或重建。
15. Queue编辑不依赖edit/reclassify wire command，只执行cancel成功后new submit。
16. History direction只存在于单次`HistoryPageRequest`；Go cursor/model cache不保存第二份direction，PAGE的validated direction必须与outstanding request exact join。
17. Go只消费unified history root；每个root只有一对directionless cursor，model只有一个latest pair及bounded pinned old-root states，不保存feed kind、transcript/audit双cache或cross-feed merge逻辑。
18. `DurableHistoryCell`与`OperationalActivityCell`是不相交model/renderer path；activity不改变history root、placement key或projection revision。
19. Run lifecycle只渲染`AuditCell(RUN_LIFECYCLE)`，Go不包含`RunLifecycleCell`或unknown-cell fallback。
20. Go明确区分一个latest-root cursor pair与bounded pinned-root page states；follow-tail和current-root rehydrate永远不使用old pinned cursor。
21. Confirmed-root rollover只由`PresentationHistoryRootAdvancedFrame`推进projection revision并原子安装，frame gap必然snapshot rebuild。
22. Stable placement key + entry ID是history cache/cursor identity；root-local display rank不进入跨root identity。
23. Root-advanced完整验证consumed segment prefix、retained segment suffix与closed resident transition；checkpoint期间并发live append/noop tail不会被清空、丢失或重复应用。
24. Session rotation/hard capacity状态及request-specific growth decision具有typed UX；Go不重算quote/reserve，也不通过eviction、truncation或继续submit绕过server capacity fence。
25. Go只接受registered placement-key contract的exact fixed framing；未知binding或typed/byte mismatch fail closed，不使用本地替代排序。
