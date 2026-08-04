# Pulsara Bubble Tea v2 Terminal Client Hard-Cut 实施规格

> 状态：S0 PASS；S1 IMPLEMENTED；S2–S6 NOT STARTED
> Requirement namespace：TUI-BT-*
> 唯一 owner：Go Terminal client 的状态、渲染、交互、进程内 I/O 调度与发布
> Python authority：PULSARA_TERMINAL_PRESENTATION_FOUNDATION_IMPLEMENTATION.zh.md
> Wire DTO 与兼容性：PULSARA_TERMINAL_CLIENT_PROTOCOL_CONTRACT.zh.md
> 产品与 UX 原则：PULSARA_TERMINAL_UI_UX_RESEARCH_AND_DESIGN.zh.md
> Legacy 边界：PULSARA_LEGACY_REPL_RETENTION_CONTRACT.zh.md

## S1 实施回执（2026-08-04）

S1 已按本规格完成只读纵切：

- Protocol 已原子切换至 major 2 / package `pulsara.terminal.v2`，Python 与 Go binding、canonical fingerprint helper、golden vector及behavioral vocabulary由同一schema生成；
- Go client 已完成transport auth、Hello、observer Attach、Attach ACK、Heartbeat、durable snapshot、operational snapshot和atomic five-section control baseline；
- Bubble Tea `Model/Update/View`只消费closed application message/effect，Update/View无I/O；framework signal handler已禁用，signal、parent supervision和single teardown由client owner接管；
- production View现在是renderer-owned full-height shell：默认进入alternate screen，按真实WindowSize精确输出一屏visual rows，固定header、bounded transcript viewport和单行read-only footer；高度1/2有closed compact降级，不再使用隐藏的`240×100` clamp，也不绘制伪composer；
- `transcript.Model`已成为wrap cache、visual-row scroll、follow-tail、unseen、resize anchor的唯一owner；`presentation.State`只保存validated durable snapshot。Up/Down为1行、wheel为3行、PageUp/PageDown为`viewportRows-1`、End恢复tail，width变化才重建immutable wrap cache；
- production View固定启用cell-motion mouse reporting，滚轮只改变client-owned transcript viewport，不泄漏到terminal native scrollback；pending interaction与active queue只读安装，S1不生成mutation、secret、composer、page或observation effect；
- `pulsara host tui`为显式opt-in入口；`pulsara host`默认行为和`pulsara host repl`保持不变，缺失/不兼容binary时typed fail closed且不fallback；
- Terminal emulator的native scrollbar属于窗口chrome，不产生Bubble Tea mouse event；标准alternate screen无法阻止Terminal.app回看primary-buffer scrollback。Python launcher提供显式`--clear-scrollback`作为不可逆private-screen选择，默认关闭且必须在帮助文本中明确其删除语义；Go client不得自行清空或伪造可恢复的terminal history；
- Go S1 capability集合明确排除`HISTORY_PAGE_V1`、`OBSERVATION_STREAM_V1`、`CONTROL_PROJECTION_OBSERVATION_V1`和`RECONNECT_AUTH_ROTATION_V1`。Protocol已冻结control cursor/change/GAP和three-plane batch的wire prerequisite，但Go消费与server activation仍归S2；
- 四平台production packaging、checksum release、wheel carrier和默认TTY activation仍归S6，未计入本回执。

机器证据由`clients/terminal`的Go tests/race/vet、Python Protocol/Gateway/launcher/cross-language PTY tests、schema generator clean check和全量pytest共同提供；最终测试数字记录在本次实施交付报告中。

## 0. 本文裁决

本文是 production Go Terminal client 的唯一实施规格。它只拥有：

- Bubble Tea Model、Update、View；
- Go 进程内的 closed state、message、effect；
- Protobuf frame 到 client-local immutable view state 的消费算法；
- viewport、composer、interaction、secret、queue 的客户端状态机；
- Go child 的 transport scheduler、backpressure、signal 和 teardown；
- Python parent 与 Go child 之间的启动、监督和退出行为；
- Go 构建、四平台制品、checksum、wheel carrier 和默认入口切换；
- S1–S6 的逐文件实施顺序、测试矩阵与独立 DoD。

本文不重新定义：

- AgentEvent、EventLog、canonical transcript acceptance、suppression、pairing；
- presentation history root、placement key、checkpoint、capacity 的服务端语义；
- queue admission、dispatch、cancel、delivery authority；
- command domain outcome、MCP continuation、secret validation、lease expiry；
- Protobuf field number、wire fingerprint 或版本兼容规则；
- 产品为什么选择某种布局、文案、交互层级。

这些内容分别由 Foundation、Protocol 和 Research 文档拥有。本文只引用 Protobuf message、field 和 server disposition，并定义 Go 客户端收到它们之后的行为。

### TUI-BT-OWN-001 规范优先级

冲突时按下表处理：

| 问题 | 唯一真源 | Go 文档允许做什么 |
|---|---|---|
| Python durable/projection authority | Foundation | 引用输出，不复制 reducer |
| Wire 字段、closed union、fingerprint、兼容性 | Protocol | 调用 generated binding 和 validator |
| 产品布局、文案、交互原则 | Research | 实现，不另造产品原则 |
| Legacy 能力边界 | Legacy contract | 保持显式旧入口，不扩权 |
| Go state、MUV、渲染、交互、发布 | 本文 | 唯一规范 |

若 Go 实现需要一个 Protobuf 中不存在的 authority field 或 disposition，必须先修改 Protocol 文档、schema、Python mapper 和 cross-language golden。禁止在 Go 中增加自由 dict、开放字符串、反解 public text 或临时 compatibility shim。

### TUI-BT-OWN-002 Go/Python physical boundary

Python 进程拥有 Runtime、Host、Gateway、Unix socket、attachment/controller authority 和 durable command outcome。Go 进程拥有 real TTY、alternate screen、local render state 和 attachment-local UX。

Go 永远不得：

- import、生成或镜像 Python AgentEvent；
-读取 RawStoredEventEnvelope、StoredEventBatchCommitReceipt 或 PostgreSQL row；
-从 cell public text 推断 tool、run、queue 或 interaction 状态；
-从 socket write 成功推断 command 成功；
-本地计算 queue winner、history capacity 或 secret validity；
-将 server string disposition 扩展成未注册 fallback；
-把 generated Protobuf object 长期放进 Bubble Tea Model。

所有 generated message 只在 protocol/client 边界短暂存在。进入 AppModel 前必须经过 Protocol-owned structural validation，并复制为 client-local immutable value。

## 1. S0 结论与证据索引

### TUI-BT-S0-001 结论

S0 结论冻结为 PASS。Bubble Tea v2 路线进入 production S1–S6，不建设 Python full-screen TUI。

S0 是可删除的隔离 spike。clients/terminal/spikes/s0 不得被 production module import，不得成为 compatibility facade，也不得向 production 复制 fake Protobuf 或 fake secret。

真实 IME 候选窗口、attached tmux 视觉检查和非本机 clean-runner 启动已由产品 owner 明确移出 S1 admission gate，保留为后续兼容性与 release regression，不改变 S0 PASS。

### TUI-BT-S0-002 证据索引

| 证据 | 路径 |
|---|---|
| S0 汇总 | clients/terminal/spikes/s0/S0_REPORT.zh.md |
| 本机 PTY | clients/terminal/spikes/s0/evidence/darwin-arm64-pty.json |
| tmux 自动化 | clients/terminal/spikes/s0/evidence/darwin-arm64-tmux.json |
| Docker SSH | clients/terminal/spikes/s0/evidence/docker-ssh-arm64.json |
| 真实远程 SSH | clients/terminal/spikes/s0/evidence/real-ssh-plumliuwin-wsl2-amd64.json |
| 四目标 cross-build | clients/terminal/spikes/s0/evidence/cross-build.txt |
| 40 轮性能原始数据 | clients/terminal/spikes/s0/evidence/darwin-arm64-performance.json |
| 性能摘要 | clients/terminal/spikes/s0/evidence/darwin-arm64-performance.md |
| 自动化入口 | clients/terminal/spikes/s0/scripts/run_automated.sh |
| 性能入口 | clients/terminal/spikes/s0/scripts/run_performance.sh |
| 真实 SSH 入口 | clients/terminal/spikes/s0/scripts/real_ssh_smoke.sh |

S0 dependency baseline：

- Go toolchain 1.26.5；
- module language baseline 1.25.0；
- Bubble Tea v2.0.6；
- Bubbles v2.1.0；
- Lip Gloss v2.0.5；
- Ultraviolet v0.0.0-20260416155717-489999b90468；
- Protobuf Go runtime v1.36.11。

Ultraviolet 虽为Go module graph中的间接依赖，但它是Bubble Tea v2 renderer的物理实现，属于renderer-critical compatibility pin。不得让`go get`或`go mod tidy`将它静默抬升到Bubble Tea v2.0.6未声明的pseudo-version。升级任一项必须重跑production PTY、Apple Terminal resize、wide-rune、paste、signal和render-jitter gates；S0 spike不需要成为长期product test dependency。

## 2. Production module 与逐文件目录

### TUI-BT-PKG-001 Module identity

Production module 固定为：

~~~text
module github.com/plumliu/pulsara-agent/clients/terminal
go 1.25.0
toolchain go1.26.5
~~~

Production module 位于 clients/terminal/。spikes/s0 保持 nested disposable module，不进入 production go list ./...。

### TUI-BT-PKG-002 最终目录

S6 完成后的目录必须精确收敛为：

~~~text
clients/terminal/
  go.mod
  go.sum
  README.md

  cmd/pulsara-tui/
    main.go

  internal/bootstrap/
    bootstrap.go
    options.go

  internal/buildinfo/
    buildinfo.go

  internal/config/
    config.go

  internal/protocol/
    terminal_client.pb.go        # generated；禁止手改
    fingerprint_contract_gen.go  # 从Protocol manifest生成；禁止手改

  internal/protocolvalue/
    vocabulary_gen.go            # 从Protocol enum机械生成的non-Protobuf immutable value；generated
    carriers_gen.go              # validated、deep-copied、non-Protobuf carriers；generated

  internal/wire/
    framing.go
    compatibility.go
    decode.go
    encode.go
    validate.go

  internal/presentation/
    state.go
    immutable.go
    snapshot.go
    projection_delta.go
    root_advance.go
    operational.go
    gap.go
    page.go
    cell.go
    cursor.go
    cache.go

  internal/commandstate/
    state.go
    transition.go

  internal/interaction/
    state.go
    transition.go

  internal/queue/
    state.go
    transition.go

  internal/secret/
    state.go
    buffer.go
    handle.go
    runtime.go
    transition.go

  internal/app/
    model.go
    state.go
    message.go
    effect.go
    input.go
    update.go
    view.go
    keymap.go
    layout.go

  internal/client/
    runtime.go
    service.go
    connection.go
    auth.go
    heartbeat.go
    operation_registry.go
    scheduler.go
    bridge.go
    snapshot.go
    observe.go
    history.go
    mutation.go
    secret.go

  internal/components/transcript/
    model.go
    update.go
    view.go
    wrap_cache.go

  internal/components/composer/
    model.go
    update.go
    view.go
    history.go
    paste.go

  internal/components/interaction/
    view.go

  internal/components/queue/
    view.go

  internal/components/status/
    view.go

  internal/components/sidebar/
    view.go

  internal/components/notification/
    model.go
    view.go

  internal/supervision/
    child.go
    signal_unix.go
    teardown.go

  internal/release/
    manifest.go
    selftest.go

  internal/testkit/
    fake_executor.go
    fixture.go
    pty.go

  testdata/protocol/
  testdata/view/
  testdata/pty/

  scripts/
    generate_protocol.sh
    build.sh
    package.sh
    verify_dist.sh
~~~

Python launch/release integration 固定使用：

~~~text
src/pulsara_agent/terminal_client/
  __init__.py
  binary.py
  launcher.py
  supervision.py

tests/
  test_terminal_tui_launcher.py
  test_terminal_tui_cross_language.py
  test_terminal_tui_release.py

.github/workflows/
  terminal-client-release.yml
~~~

不得增加 parallel app2、legacy_client、compat、domain、event、queue_authority 或 secret_validator package。

### TUI-BT-PKG-003 Package DAG

箭头表示左侧 package 可以 import 右侧 package：

~~~text
cmd/pulsara-tui
  -> bootstrap

bootstrap
  -> app
  -> client
  -> supervision
  -> buildinfo
  -> config

supervision
  -> app
  -> client

client
  -> app
  -> wire
  -> protocol
  -> protocolvalue
  -> presentation
  -> commandstate
  -> secret

app
  -> protocolvalue
  -> presentation
  -> commandstate
  -> interaction
  -> queue
  -> secret
  -> components/*
  -> bubbletea

components/*
  -> presentation | commandstate | interaction | queue | secret
  -> bubbles
  -> lipgloss

presentation
  -> protocolvalue
  -> wire

commandstate | interaction | queue | secret
  -> no app/client/component package

wire
  -> protocol
  -> protocolvalue

protocolvalue
  -> protocol

protocol | buildinfo | config
  -> no project-internal higher layer
~~~

强制规则：

1. app 不 import client、wire 或 generated Protobuf；它只可import由Protocol generator生成的immutable `protocolvalue` value/carrier。
2. components 不 import app、client、wire、generated Protobuf或`protocolvalue`；renderer只能消费presentation/app已经验证的client value。
3. client 可以构造 app 中已定义的 concrete messages，但不能实现新的 message branch。
4. presentation 是 wire-to-view conversion owner，不持有 socket 或 tea.Cmd。
5. protocol与protocolvalue只有generated code；manual helper必须进入wire。`protocolvalue`不得增加手写enum、field mapping、validator或domain inference。
6. Python src 不 import任何 Go source、spike 或 tests/support。
7. Go production 不读取 repository root 下的 Python module。
8. architecture test 必须按 AST/go list observation 验证 DAG，而不是维护手写 import 猜测。

## 3. Closed Bubble Tea application contract

### TUI-BT-APP-001 AppState

Bubble Tea Model 中的 semantic state 只有一个 AppState：

~~~go
type AppPhase uint8

const (
    PhaseBooting AppPhase = iota + 1
    PhaseConnecting
    PhaseNegotiating
    PhaseAttaching
    PhaseLoadingSnapshot
    PhaseReady
    PhaseReconnecting
    PhaseReadOnly
    PhaseFatal
    PhaseDetaching
    PhaseExited
)

type AppState struct {
    Phase        AppPhase
    Connection   ConnectionState
    Attachment   AttachmentState
    SnapshotLoading SnapshotLoadingState
    Durable      presentation.State
    Operational  presentation.OperationalState
    Control      presentation.ControlProjectionState
    Viewport     TranscriptViewportState
    Composer     ComposerState
    Commands     commandstate.Registry
    Interaction  interaction.State
    Queue         queue.State
    Secret        secret.State
    Layout        LayoutState
    LocalNotifications  LocalNotificationState
    Teardown      TeardownState
}
~~~

`PhaseLoadingSnapshot`不再是一个同时表示三种进度的模糊标签。S1冻结下列closed substate；它在`PhaseReady | PhaseReadOnly`继续保留`SnapshotBaselinesInstalled`证明，而不是进入Ready后清零：

~~~go
type SnapshotLoadingPhase uint8
const (
    SnapshotLoadingUninitialized SnapshotLoadingPhase = iota + 1
    SnapshotAwaitingDurableSnapshot
    SnapshotAwaitingOperationalSnapshot
    SnapshotBaselinesInstalled
)

type SnapshotLoadingState struct {
    Phase                         SnapshotLoadingPhase
    AttachmentID                  string
    AttachmentGeneration          uint64
    TransportBindingFingerprint   string
    DurableOperationID            string
    DurableOperationGeneration    uint64
    DurableSnapshotFingerprint    string
    DurableControlCursorFingerprint string
    OperationalOperationID        string
    OperationalOperationGeneration uint64
    OperationalSnapshotFingerprint string
    OperationalGeneration         uint64
    OperationalCursor             uint64
}
~~~

Presence矩阵唯一为：

| phase | durable operation/result | operational operation/result | 合法AppPhase |
|---|---|---|---|
| `Uninitialized` | 全空 | 全空 | Booting至AttachAckPending |
| `AwaitingDurableSnapshot` | exact current durable operation；result空 | 全空 | LoadingSnapshot |
| `AwaitingOperationalSnapshot` | durable result全部存在且已安装 | exact current operational operation；result空 | LoadingSnapshot |
| `BaselinesInstalled` | durable result全部存在 | operational result全部存在 | Ready或ReadOnly |

唯一前向转换是`Uninitialized -> AwaitingDurable -> AwaitingOperational -> BaselinesInstalled`。Matching duplicate response只能证明same fingerprint并no-op；same operation/result identity different fingerprint是fatal compatibility。Durable retry不得清空或替换已确认durable baseline，operational retry不得改写durable fields；重连/GAP建立新的loading generation前，必须把旧operation结算为stale并保留最后confirmed screen为read-only，不能把两个snapshot当作一次原子server snapshot。

上述名字不是留给实施者自由设计的placeholder。S1必须按以下closed value types落地；所有字段均为value、immutable copy或opaque process-local handle ID，不得保存physical object：

~~~go
type ConnectionPhase uint8
const (
    ConnectionDisconnected ConnectionPhase = iota + 1
    ConnectionDialing
    ConnectionAuthPending
    ConnectionHelloPending
    ConnectionAttachPending
    ConnectionAttachAckPending
    ConnectionAttached
    ConnectionBackoff
    ConnectionClosing
)

type AttachmentRole uint8
const (
    AttachmentObserver AttachmentRole = iota + 1
    AttachmentController
)

type OperationKind uint8
const (
    OpConnect OperationKind = iota + 1
    OpTransportAuth
    OpHello
    OpChallengePromote
    OpChallengePromotionConfirm
    OpChallengeRevokePrepared
    OpChallengeRevokeActive
    OpAttach
    OpAttachAck
    OpHeartbeat
    OpProjectionSnapshot
    OpOperationalSnapshot
    OpObserve
    OpHistoryPage
    OpMutation
    OpCommandQuery
    OpSecretReveal
    OpSecretEdit
    OpSecretSubmit
    OpClipboard
    OpOpenURL
    OpTick
    OpReconnect
    OpTeardown
)

// OperationToken只用于会产生Protocol request_id的wire operation。
type OperationToken struct {
    Kind                 OperationKind
    OperationID          string
    OperationGeneration  uint64
    RequestID            string
    ConnectionGeneration uint64
    AttachmentID         string
    AttachmentGeneration uint64
    TransportBindingGeneration uint64
    TransportBindingFingerprint string
    ControllerGeneration uint64
    Deadline             time.Time // process-local monotonic component必须保留
}

// LocalOperationToken用于connect、challenge lifecycle、timer、clipboard、browser和teardown；它没有RequestID。
type LocalOperationToken struct {
    Kind                OperationKind
    OperationID         string
    OperationGeneration uint64
    AppGeneration       uint64
    Deadline            time.Time
}

// Token/kind矩阵是closed contract：OperationToken只接受Protocol wire kinds；
// LocalOperationToken只接受connect、challenge lifecycle、secret edit、UI helper、tick、
// reconnect与teardown kinds。完整穷尽集合由紧随代码围栏后的正文冻结。

type OutstandingOperationKind uint8
const (
    OutstandingWire OutstandingOperationKind = iota + 1
    OutstandingLocal
)

type OutstandingOperation struct {
    Carrier OutstandingOperationKind
    Wire    OperationToken
    Local   LocalOperationToken
}

type PublicFailureCode uint16
const (
    FailureConnect PublicFailureCode = iota + 1
    FailurePeerIdentity
    FailureBootstrap
    FailureTransportAuthentication
    FailureTransportIO
    FailureProtocolVersion
    FailureProtocolSchema
    FailureRequiredCapability
    FailureAttach
    FailureHeartbeat
    FailureReadTimeout
    FailureCommandOutcomeTimeout
    FailureCancelled
    FailureProjectionSnapshot
    FailureOperationalSnapshot
    FailureHistoryPage
    FailureCommandPreDispatch
    FailureCommandDeliveryUnknown
    FailureSecretTransport
    FailureSecretSubmitDeliveryUnknown
    FailureClipboard
    FailureOpenURL
    FailureTeardown
    FailureTeardownDeadline
    FailureClientInvariant
)

type FailureDisposition uint8
const (
    FailureFatal FailureDisposition = iota + 1
    FailureRetryWithBackoff
    FailureReconnect
    FailureRetryRead
    FailureQueryCommand
    FailureRebuildDurableSnapshot
    FailureRebuildOperationalSnapshot
    FailureRevokeSecret
    FailureRevokeSecretAndQuery
    FailureContinueTeardown
    FailureNoRetry
)

type FailureDeliveryPhase uint8
const (
    DeliveryNotStarted FailureDeliveryPhase = iota + 1
    DeliveryWriteStarted
    DeliveryRequestFullySent
    DeliveryResponseReadStarted
    DeliveryResponseFullyValidated
    DeliveryLocalOperationStarted
)

type FailureConnectionState uint8
const (
    FailureConnectionNotEstablished FailureConnectionState = iota + 1
    FailureConnectionUsable
    FailureConnectionInvalidated
    FailureConnectionClosing
)

type PhysicalFailureCause uint8
const (
    CauseDialFailed PhysicalFailureCause = iota + 1
    CausePeerRejected
    CauseBootstrapRejected
    CauseAuthenticationRejected
    CauseProtocolVersionRejected
    CauseProtocolSchemaRejected
    CauseRequiredCapabilityMissing
    CauseAttachRejected
    CauseDeadlineExpired
    CauseCallerCancelled
    CauseEOF
    CauseReadFailed
    CauseWriteFailed
    CauseMalformedResponse
    CauseProjectionValidationFailed
    CauseLocalIntegrationFailed
    CauseClientInvariant
)

type FailureProductionFact struct {
    operationKind              OperationKind
    operationID                string
    requestID                  string
    hasRequestID               bool
    deliveryPhase              FailureDeliveryPhase
    connectionState            FailureConnectionState
    physicalCause              PhysicalFailureCause
    connectionTerminalReceiptFingerprint string
    hasConnectionTerminalReceipt bool
    physicalReceiptFingerprint string
    evidenceFingerprint        string
}

// 只有PhysicalOperationRegistry可以构造；caller不能自报delivery phase。
type PhysicalOperationFailureReceipt struct {
    operation          OutstandingOperation
    deliveryPhase      FailureDeliveryPhase
    connectionState    FailureConnectionState
    physicalCause      PhysicalFailureCause
    connectionTerminalReceipt PhysicalConnectionTerminalReceipt
    hasConnectionTerminalReceipt bool
    physicalReceiptFingerprint string
}

type PhysicalOperationStage uint8
const (
    PhysicalInstalled PhysicalOperationStage = iota + 1
    PhysicalWriteStarted
    PhysicalRequestFullySent
    PhysicalResponseReadStarted
    PhysicalResponseFullyValidated
    PhysicalTerminalizing
    PhysicalTerminal
)

// 两种capability由registry借出，字段不对其他package暴露。
type OperationProgressCapability struct {
    operationID string
    generation  uint64
    nonce       string
}
type OperationSettlementCapability struct {
    operationID string
    generation  uint64
    nonce       string
}

// beginConnectionTerminalization消费initial settlement capability后签发。
type PostJoinOperationSettlementCapability struct {
    operationID                          string
    operationGeneration                  uint64
    preparedTerminalizationFingerprint   string
    terminalizationCapabilityFingerprint string
    frozenPhysicalCause                  PhysicalFailureCause
    frozenFailureSignalFingerprint       string
    nonce                                string
}
type PhysicalFailureSignal struct {
    cause            PhysicalFailureCause
    causeFingerprint string
}

type PhysicalConnectionTerminalReason uint8
const (
    ConnectionTerminalReadDeadline PhysicalConnectionTerminalReason = iota + 1
    ConnectionTerminalEOF
    ConnectionTerminalReadFailure
    ConnectionTerminalWriteFailure
    ConnectionTerminalMalformedFrame
    ConnectionTerminalServerClosing
    ConnectionTerminalCallerTeardown
)

type PhysicalIOExitDisposition uint8
const (
    PhysicalIOJoined PhysicalIOExitDisposition = iota + 1
    PhysicalIONotStarted
)

// 只有physical connection supervisor在matching drain RUNNING、socket close且
// reader/writer join后可构造，并在同一owner lock内安装为TERMINAL receipt。
type PhysicalConnectionTerminalReceipt struct {
    connectionID               string
    connectionGeneration       uint64
    transportBindingGeneration uint64
    transportBindingFingerprint string
    terminalizationCapabilityFingerprint string
    physicalDrainIdentityFingerprint string
    terminalReason             PhysicalConnectionTerminalReason
    socketCloseFingerprint     string
    readerOperationID          string
    readerOperationGeneration  uint64
    readerExit                 PhysicalIOExitDisposition
    readerExitFingerprint      string
    writerOperationID          string
    writerOperationGeneration  uint64
    writerExit                 PhysicalIOExitDisposition
    writerExitFingerprint      string
    terminalSequence           uint64
    terminalReceiptFingerprint string
}

type ConnectionTerminalizationCapability struct {
    operationID                 string
    operationGeneration         uint64
    connectionID                string
    connectionGeneration        uint64
    transportBindingGeneration  uint64
    transportBindingFingerprint string
    requiredReason              PhysicalConnectionTerminalReason
    capabilityFingerprint       string
    nonce                       string
}

// begin内部创建并只安装进registry record；绝不作为caller返回值。
// 两个nested capability只能由各自owner消费一次。
type PreparedConnectionTerminalization struct {
    physicalTerminalizationCapability ConnectionTerminalizationCapability
    postJoinSettlementCapability       PostJoinOperationSettlementCapability
    frozenFailureSignalFingerprint     string
    preparedTerminalizationFingerprint string
}

type ConnectionTerminalizationAttemptState uint8
const (
    TerminalizationAttemptInstalled ConnectionTerminalizationAttemptState = iota + 1
    TerminalizationAttemptInvalidating
    TerminalizationAttemptPhysicalDraining
    TerminalizationAttemptReceiptReady
    TerminalizationAttemptSettling
    TerminalizationAttemptTerminal
)

// Attempt identity在创建后永不变化；waiter/re-drive只绑定这一层。
type ConnectionTerminalizationAttemptIdentity struct {
    attemptID                         string
    attemptGeneration                 uint64
    operationID                       string
    operationGeneration               uint64
    connectionID                      string
    connectionGeneration              uint64
    transportBindingGeneration        uint64
    transportBindingFingerprint       string
    frozenFailureSignalFingerprint    string
    preparedTerminalizationFingerprint string
    attemptIdentityFingerprint        string
}

type PhysicalConnectionDrainStartDisposition uint8
const (
    PhysicalDrainCreated PhysicalConnectionDrainStartDisposition = iota + 1
    PhysicalDrainCompatibleAlreadyCreated
    PhysicalDrainConflict
)

type PhysicalConnectionDrainRecordState uint8
const (
    PhysicalDrainReserved PhysicalConnectionDrainRecordState = iota + 1
    PhysicalDrainStarting
    PhysicalDrainRunning
    PhysicalDrainTerminal
)

// 由physical connection owner在启动cancel/close task之前安装；可exact rebind。
type PhysicalConnectionDrainHandle struct {
    drainID                              string
    drainGeneration                      uint64
    attemptIdentityFingerprint           string
    connectionID                         string
    connectionGeneration                 uint64
    transportBindingGeneration           uint64
    transportBindingFingerprint          string
    terminalizationCapabilityFingerprint string
    readerOperationID                    string
    readerOperationGeneration            uint64
    writerOperationID                    string
    writerOperationGeneration            uint64
    drainIdentityFingerprint             string
}

// Stable launch record；只由常驻connection drain supervisor消费。
type PhysicalConnectionDrainLaunchPermit struct {
    drainIdentityFingerprint   string
    connectionID               string
    connectionGeneration       uint64
    launchGeneration           uint64
    launchPermitFingerprint    string
}

// 同一时刻最多一张active runner lease；panic/re-drive产生successor lease。
type PhysicalConnectionDrainRunnerLease struct {
    drainIdentityFingerprint   string
    launchPermitFingerprint    string
    runnerID                   string
    runnerGeneration           uint64
    predecessorRunnerLeaseFingerprint string
    runnerLeaseFingerprint     string
}

type PhysicalConnectionDrainRecordSnapshot struct {
    drainHandle                PhysicalConnectionDrainHandle
    state                      PhysicalConnectionDrainRecordState
    stateRevision              uint64
    launchPermit               PhysicalConnectionDrainLaunchPermit
    supervisorGeneration       uint64
    driveQueued                bool
    activeRunnerLease          PhysicalConnectionDrainRunnerLease
    hasActiveRunnerLease       bool
    terminalReceipt            PhysicalConnectionTerminalReceipt
    hasTerminalReceipt         bool
    recordStateFingerprint     string
}

type PhysicalConnectionDrainStartResult struct {
    disposition      PhysicalConnectionDrainStartDisposition
    drainHandle      PhysicalConnectionDrainHandle
    hasDrainHandle   bool
    observedRecordState PhysicalConnectionDrainRecordState
    resultFingerprint string
}

// Mutable snapshot每次transition重算state fingerprint；它不是handle identity。
type ConnectionTerminalizationAttemptSnapshot struct {
    identity                          ConnectionTerminalizationAttemptIdentity
    state                             ConnectionTerminalizationAttemptState
    stateRevision                     uint64
    physicalDrainHandle               PhysicalConnectionDrainHandle
    hasPhysicalDrainHandle            bool
    connectionTerminalReceipt         PhysicalConnectionTerminalReceipt
    hasConnectionTerminalReceipt      bool
    operationFailureReceipt           PhysicalOperationFailureReceipt
    hasOperationFailureReceipt        bool
    completionFingerprint             string
    attemptStateFingerprint           string
}

// Registry-owned record；任何capability/physical handle均不暴露给caller。
type ConnectionTerminalizationAttempt struct {
    identity ConnectionTerminalizationAttemptIdentity
    prepared PreparedConnectionTerminalization
    snapshot ConnectionTerminalizationAttemptSnapshot
}

type ConnectionTerminalizationAttemptHandle struct {
    attemptID                   string
    attemptGeneration           uint64
    operationID                 string
    operationGeneration         uint64
    attemptIdentityFingerprint  string
}

type ConnectionTerminalizationWaitDisposition uint8
const (
    TerminalizationWaitCompleted ConnectionTerminalizationWaitDisposition = iota + 1
    TerminalizationWaiterCancelled
    TerminalizationWaiterDeadline
)

type ConnectionTerminalizationWaitResult struct {
    disposition          ConnectionTerminalizationWaitDisposition
    attemptHandle        ConnectionTerminalizationAttemptHandle
    completionFingerprint string
    failureReceipt       PhysicalOperationFailureReceipt
    hasFailureReceipt    bool
}

type PhysicalOperationRegistry struct { /* private bounded records + lock */ }

// 只能由internal/client/connection.go中的exact connection owner实现。
type PhysicalConnectionDrainPort interface {
    startInvalidateClose(
        capability ConnectionTerminalizationCapability,
        identity ConnectionTerminalizationAttemptIdentity,
    ) (PhysicalConnectionDrainStartResult, error)
    rebindPhysicalDrain(
        identity ConnectionTerminalizationAttemptIdentity,
        drainIdentityFingerprint string,
    ) (PhysicalConnectionDrainHandle, error)
    waitPhysicalDrain(
        handle PhysicalConnectionDrainHandle,
    ) (PhysicalConnectionTerminalReceipt, error)
}

type PublicFailure struct {
    code        PublicFailureCode
    message     string // <=512 characters且<=2048 UTF-8 bytes的sanitized public text
    disposition FailureDisposition
    production  FailureProductionFact
}

func (r *PhysicalOperationRegistry) beginConnectionTerminalization(
    capability OperationSettlementCapability,
    signal PhysicalFailureSignal,
) (ConnectionTerminalizationAttemptHandle, error)

func (r *PhysicalOperationRegistry) waitConnectionTerminalization(
    handle ConnectionTerminalizationAttemptHandle,
    waiterCancellation <-chan struct{},
    waiterDeadline time.Time,
) (ConnectionTerminalizationWaitResult, error)

func (r *PhysicalOperationRegistry) drainConnectionTerminalizations(
    closeDeadline time.Time,
) error

func (h ConnectionTerminalizationAttemptHandle) AttemptID() string
func (h ConnectionTerminalizationAttemptHandle) AttemptGeneration() uint64
func (h ConnectionTerminalizationAttemptHandle) OperationID() string
func (h ConnectionTerminalizationAttemptHandle) OperationGeneration() uint64
func (h ConnectionTerminalizationAttemptHandle) IdentityFingerprint() string
func (r ConnectionTerminalizationWaitResult) Disposition() ConnectionTerminalizationWaitDisposition
func (r ConnectionTerminalizationWaitResult) AttemptHandle() ConnectionTerminalizationAttemptHandle
func (r ConnectionTerminalizationWaitResult) CompletionFingerprint() string
func (r ConnectionTerminalizationWaitResult) FailureReceipt() (PhysicalOperationFailureReceipt, bool)

func (r *PhysicalOperationRegistry) settleLocalFailure(
    capability OperationSettlementCapability,
    signal PhysicalFailureSignal,
) (PhysicalOperationFailureReceipt, error)

func (r *PhysicalOperationRegistry) settleConnectionFailure(
    capability PostJoinOperationSettlementCapability,
    connectionTerminalReceipt PhysicalConnectionTerminalReceipt,
) (PhysicalOperationFailureReceipt, error)

func ClassifyPublicFailure(
    receipt PhysicalOperationFailureReceipt,
    sanitizedMessage string,
) (PublicFailure, error)

func (f PublicFailure) Code() PublicFailureCode
func (f PublicFailure) Message() string
func (f PublicFailure) Disposition() FailureDisposition
func (f PublicFailure) Production() FailureProductionFact
func (f FailureProductionFact) OperationKind() OperationKind
func (f FailureProductionFact) OperationID() string
func (f FailureProductionFact) RequestID() (string, bool)
func (f FailureProductionFact) DeliveryPhase() FailureDeliveryPhase
func (f FailureProductionFact) ConnectionState() FailureConnectionState
func (f FailureProductionFact) PhysicalCause() PhysicalFailureCause
func (f FailureProductionFact) ConnectionTerminalReceiptFingerprint() (string, bool)
func (f FailureProductionFact) PhysicalReceiptFingerprint() string
func (f FailureProductionFact) EvidenceFingerprint() string

type BuildIdentity struct {
    ProductVersion            string
    SourceRevision            string
    GoVersion                 string
    TargetOS                  string
    TargetArch                string
    ProtocolSchemaFingerprint string
    BuildFingerprint          string
}

type ClientBuildIdentity = BuildIdentity

type HandshakeRecoveryCandidateIdentity struct {
    CandidateID                   string
    CandidateFingerprint          string
    CandidateHandleID             string // ClientRuntimeOwner中的immutable full carrier
    ClientInstanceID              string
    AttachmentAttemptGeneration   uint64
    HostSessionID                 string
    RequestedRuntimeSessionID     string
    RequestedRole                 AttachmentRole
    ClientBuildFingerprint        string
    ProtocolRangeFingerprint      string
    SupportedCapabilitySetFingerprint string
    RequiredCapabilitySetFingerprint  string
}

type HandshakeCandidateTerminalDisposition uint8
const (
    CandidateTerminalNegotiationWinnerUnavailable HandshakeCandidateTerminalDisposition = iota + 1
    CandidateTerminalHelloRejected
)

type HelloCandidateTerminalReason uint8
const (
    HelloTerminalFrozenWinnerBindingMissing HelloCandidateTerminalReason = iota + 1
    HelloTerminalFrozenWinnerContractUnsupported
    HelloTerminalFrozenWinnerRuntimeIncompatible
    HelloTerminalProtocolRangeUnsupported
    HelloTerminalSchemaContractUnsupported
    HelloTerminalMissingRequiredCapability
    HelloTerminalCandidateExpired
    HelloTerminalServerNegotiationPolicyRejected
)

type HelloCandidateClientDisposition uint8
const (
    HelloClientParentRelaunchNewCandidate HelloCandidateClientDisposition = iota + 1
    HelloClientFatalCompatibility
)

type HandshakeCandidateTerminalReceipt struct {
    CandidateID                       string
    CandidateFingerprint              string
    AttachmentAttemptGeneration       uint64
    Disposition                       HandshakeCandidateTerminalDisposition
    Reason                            HelloCandidateTerminalReason
    RequiredClientDisposition         HelloCandidateClientDisposition
    PriorNegotiationWinnerFingerprint string
    HasPriorNegotiationWinner         bool
    CandidateRegistryRevision         uint64
    TerminalReceiptFingerprint        string
}

type ParentRelaunchCause uint8
const (
    ParentRelaunchNegotiationWinnerUnavailable ParentRelaunchCause = iota + 1
    ParentRelaunchHelloRejected
)

type ValidatedPeerIdentity struct {
    EffectiveUID               uint64
    PeerUID                    uint64
    PeerPID                    uint64
    HasPeerPID                 bool
    SocketOwnerUID             uint64
    RuntimePathFingerprint     string
    CredentialContractID       string
    CredentialContractVersion string
    ValidationFingerprint      string
}

// Token stage matrix由constructor强制：
// OpTransportAuth/OpHello/OpAttach: AttachmentID="", attachment/controller generation=0。
// OpAttachAck及observer read: exact attachment + current transport binding；controller generation可为0。
// Mutation/command/secret: exact attachment + current transport binding + non-zero controller generation。

type SelectedProtocol struct {
    Major                     uint32
    Minor                     uint32
    SchemaContractFingerprint string
    ServerBuild               BuildIdentity
    ServerRuntimeIdentity     string
}

type NegotiatedFeatureSet struct {
    PresentationSnapshot       bool
    OperationalSnapshot        bool
    BootstrapCarrier           bool
    LaunchAuthPreface          bool
    AttachAck                   bool
    HistoryPage                bool
    ObservationStream          bool
    RootAdvance                bool
    GapRebuild                 bool
    ControlProjectionObservation bool
    ReconnectAuthRotation      bool
    ControllerCommand          bool
    CommandQuery               bool
    TypedInteractionActions    bool
    SecretForm                 bool
    SecretPrivateURL           bool
    SecretRevoke               bool
    PromptQueueMutation        bool
    SessionSuccessor           bool
    ServerClosing              bool
    PublicClipboard            bool
    FocusReport                bool
    MouseCellMotion            bool
    MouseAllMotion             bool
    ContractFingerprint        string
}

type NegotiatedLimits struct {
    MaximumFrameBytes              uint32
    MaximumHistoryPageCells        uint32
    MaximumHistoryPageDecodedBytes uint32
    MaximumObservationWait         time.Duration
    MaximumActiveQueueItems        uint32
    MaximumServerControlNotifications uint32
    MaximumDurableObservationBytes uint32
    MaximumOperationalObservationBytes uint32
    MaximumControlObservationBytes uint32
    MaximumObservationBatchBytes   uint32
    SecretFrameMaximumBytes        uint32
}

type CapabilitySet struct {
    Values      [32]TerminalClientCapability
    Count       uint8
    Fingerprint string
}

type HelloNegotiationSemanticWinner struct {
    CandidateID                    string
    CandidateFingerprint           string
    AttachmentAttemptGeneration    uint64
    Protocol                       SelectedProtocol
    ServerSupportedCapabilities    CapabilitySet
    SelectedCapabilities           CapabilitySet
    Features                       NegotiatedFeatureSet
    Limits                         NegotiatedLimits
    CapabilityContractFingerprint  string
    NegotiationTranscriptFingerprint string
    NegotiationWinnerFingerprint   string
}

type AttachmentChallengeHandlePhase uint8
const (
    AttachmentChallengeNone AttachmentChallengeHandlePhase = iota + 1
    AttachmentChallengePreparedPromotionPending
    AttachmentChallengeActiveAcceptancePending
    AttachmentChallengeActive
)

type AttachmentChallengeRevocationTarget uint8
const (
    RevokePreparedAttachmentChallenge AttachmentChallengeRevocationTarget = iota + 1
    RevokeActivePendingAttachmentChallenge
    RevokeActiveAttachmentChallenge
)

type AttachmentChallengeRevocationReason uint8
const (
    ChallengeRevokeApplicationNonApply AttachmentChallengeRevocationReason = iota + 1
    ChallengeRevokeMessageUndelivered
    ChallengeRevokeOperationSuccessor
    ChallengeRevokeNewHelloReceipt
    ChallengeRevokeConnectionClosed
    ChallengeRevokeDeadline
    ChallengeRevokeTeardown
    ChallengeRevokePromotionFailure
)

type AttachmentChallengePromotionReceipt struct {
    PreparedHandleFingerprint string
    ActiveHandleID string
    ActiveHandleGeneration uint64
    ActiveHandleFingerprint string
    PromotionOperationID string
    PromotionOperationGeneration uint64
    CandidateFingerprint string
    HelloReceiptFingerprint string
    ConnectionID string
    PromotionReceiptFingerprint string
}

type AttachmentChallengeAcceptanceReceipt struct {
    PromotionReceiptFingerprint string
    ActiveHandleID string
    ActiveHandleGeneration uint64
    ActiveHandleFingerprint string
    ConfirmationOperationID string
    ConfirmationOperationGeneration uint64
    AppGeneration uint64
    AcceptanceReceiptFingerprint string
}

type AttachmentChallengeRevocationReceipt struct {
    Target AttachmentChallengeRevocationTarget
    Reason AttachmentChallengeRevocationReason
    HandleID string
    HandleGeneration uint64
    HandleFingerprint string
    RevocationOperationID string
    RevocationOperationGeneration uint64
    RevocationReceiptFingerprint string
}

// Process-local identity only; challenge bytes remain inside ClientRuntimeOwner.
type PreparedAttachmentChallengeHandleIdentity struct {
    HandleID                    string
    HandleGeneration            uint64
    OperationID                 string
    OperationGeneration         uint64
    RequestID                   string
    ConnectionID                string
    CandidateFingerprint        string
    NegotiationWinnerFingerprint string
    ChallengeCommitment         string
    HelloReceiptFingerprint     string
    ExpiresAt                   time.Time
    PreparedHandleFingerprint   string
}

type AttachmentChallengeState struct {
    Phase                       AttachmentChallengeHandlePhase
    Prepared                    PreparedAttachmentChallengeHandleIdentity
    ActiveHandleID              string
    ActiveHandleGeneration      uint64
    ActiveHandleFingerprint     string
    PromotionReceiptFingerprint string
    AcceptanceReceiptFingerprint string
}

type ValidatedServerHelloReceipt struct {
    RequestID                       string
    TransportAuthAttemptID          string
    CandidateID                     string
    CandidateFingerprint            string
    NegotiationWinnerFingerprint    string
    CurrentConnectionID             string
    PreparedAttachmentChallenge     PreparedAttachmentChallengeHandleIdentity
    AttachmentChallengeCommitment   string
    HelloReceiptFingerprint         string
}

type ConnectionState struct {
    Phase                  ConnectionPhase
    ClientInstanceID       string
    Generation             uint64
    HandleID               string // ClientRuntimeOwner中的opaque handle
    ServerConnectionID     string
    DialAttempt            uint32
    ReconnectAttempt       uint32
    ReconnectDueAt         time.Time
    NextOperationGeneration uint64
    Outstanding            OutstandingOperation
    HasOutstanding         bool
    HelloWinner            HelloNegotiationSemanticWinner
    HelloReceipt           ValidatedServerHelloReceipt
    TransportCredentialID string
    TransportCredentialHandleID string
    TransportCredentialCommitment string
    TransportCredentialExpiresAt time.Time
    HandshakeCandidate     HandshakeRecoveryCandidateIdentity
    HandshakeCandidateTerminalReceipt HandshakeCandidateTerminalReceipt
    HasHandshakeCandidateTerminalReceipt bool
    AttachmentChallenge     AttachmentChallengeState
    TransportAuthAttemptID string
    TransportAuthResultFingerprint string
    AttachSemanticWinnerFingerprint string
    AttachResultReceiptFingerprint string
    HeartbeatSchedule       HeartbeatScheduleState
    LastFailure            PublicFailure
    HasLastFailure         bool
}

type HeartbeatPolicy struct {
    Interval           time.Duration
    Grace              time.Duration
    MaximumMissedCount uint32
}

// Pure client-local scheduling state. It never enters AttachSemanticWinner,
// Heartbeat wire receipt, or any Protocol fingerprint.
type HeartbeatScheduleState struct {
    NextHeartbeatGeneration         uint64
    LastAcceptedHeartbeatGeneration uint64
    LastLivenessDisposition         protocolvalue.HeartbeatLivenessDisposition
    NextHeartbeatAt                 time.Time
    MissedHeartbeats                uint32
    LastAcceptedReceiptFingerprint  string
}

`HeartbeatScheduleState`存在矩阵：未ACK attachment时全部zero；ordinary/recovered ACK安装同一attachment后初始化`next=1,last=0`（ACK tombstone recovery若AppState已有same attachment schedule则保留其strictly newer generation，不倒退）；matching accepted receipt推进`last=current,next=current+1`；rejected/connection loss不伪造accepted generation。Ready后的ordinary reconnect建立new attachment attempt/attachment generation时重置schedule，same-attachment physical rebind则保留。任何`last >= next`、nonzero receipt配zero last、next-at早于matching accepted `IOMessageHeader.ReceivedAt`或schedule disposition与receipt不一致均constructor fail。

type AttachmentState struct {
    Valid                 bool
    ClientInstanceID      string
    AttachmentAttemptGeneration uint64
    ConnectionID          string
    TransportBindingGeneration uint64
    TransportBindingFingerprint string
    AttachmentID          string
    RuntimeSessionID      string
    AttachmentGeneration  uint64
    Role                  AttachmentRole
    ControllerGeneration  uint64
    IssuedAt               time.Time
    ExpiresAt              time.Time
    IdentityFingerprint   string
    SemanticWinnerFingerprint string
    CurrentReceiptFingerprint string
    ControllerDisposition protocolvalue.AttachmentControllerDisposition
    BootstrapRequirement  protocolvalue.BootstrapRequirement
    Heartbeat              HeartbeatPolicy
}

type TransportBindingIdentity struct {
    AttachmentID               string
    AttachmentGeneration       uint64
    ConnectionID               string
    TransportBindingGeneration uint64
    BoundAt                    time.Time
    BindingFingerprint         string
}

type AttachSemanticWinner struct {
    CandidateID                  string
    CandidateFingerprint         string
    AttachmentAttemptGeneration  uint64
    HelloNegotiationWinnerFingerprint string
    AttachmentID                 string
    RuntimeSessionID             string
    AttachmentGeneration         uint64
    Role                         AttachmentRole
    ControllerDisposition        protocolvalue.AttachmentControllerDisposition
    ControllerGeneration         uint64
    BootstrapRequirement         protocolvalue.BootstrapRequirement
    Heartbeat                    HeartbeatPolicy
    IssuedAt                     time.Time
    ExpiresAt                    time.Time
    NextReconnectCredential     ReconnectCredentialPublicIdentity
    HasNextReconnectCredential  bool
    SemanticWinnerFingerprint    string
}

type ReconnectCredentialPublicIdentity struct {
    CredentialID              string
    ClientInstanceID          string
    AttachmentID              string
    AttachmentGeneration      uint64
    IssuedAt                  time.Time
    ExpiresAt                 time.Time
    CredentialCommitment      string
    PublicIdentityFingerprint string
}

type AttachResultReceipt struct {
    RequestID                            string
    TransportAuthAttemptID              string
    CandidateID                         string
    CandidateFingerprint                string
    SemanticWinnerFingerprint           string
    CurrentTransportBinding             TransportBindingIdentity
    PreviousTransportBindingFingerprint string
    HasPreviousTransportBinding         bool
    Disposition                         protocolvalue.AttachResultDisposition
    ReconnectCredentialCarrierFingerprint string
    HasReconnectCredentialCarrier       bool
    ReceiptFingerprint                  string
}

type RecoveredTransportBinding struct {
    AttachmentID              string
    AttachmentGeneration      uint64
    PreviousTransportBindingFingerprint string
    ResultingConnectionID     string
    TransportBindingGeneration uint64
    Disposition               protocolvalue.TransportBindingDisposition
    ResultingTransportBindingFingerprint string
    RebindReceiptFingerprint  string
}

type TranscriptViewportState struct {
    Width                    int
    Height                   int
    FollowTail               bool
    UnseenDurableCount       uint32
    TopHistoryEntryID        string
    TopWrappedRowOffset      uint32
    SelectedHistoryEntryID   string
    SelectedPublicBlockIndex uint32
    LatestRootFingerprint    string
    PinnedRootFingerprint    string
    PendingPage              OperationToken
    HasPendingPage           bool
}

type ComposerMode uint8
const (
    ComposerDisabled ComposerMode = iota + 1
    ComposerOrdinary
    ComposerPasteReview
)

type ComposerState struct {
    Mode                    ComposerMode
    DraftUTF8               string
    GraphemeCursor          uint32
    PreferredVisualColumn   int
    DraftRevision           uint64
    PasteByteCount          uint64
    PasteReviewFingerprint  string
    SubmitAvailability      SubmitAvailability
    undo                    [64]ComposerRevision
    undoCount               uint8
    redoCount               uint8
}

type LayoutState struct {
    Width              int
    Height             int
    Density            DensityMode
    Focus              FocusTarget
    SidebarVisible     bool
    ReportFocusEnabled bool
    MouseMode          ClientMouseMode
}

type LocalNotificationState struct {
    items        [16]LocalNotification
    itemCount    uint8
    droppedCount uint64
}

type ControlInvalidationKind uint8
const (
    ControlChangedSnapshotRequired ControlInvalidationKind = iota + 1
    ControlGapGenerationChanged
    ControlGapCursorTooOld
    ControlGapTransitionNotContiguous
    ControlGapContractChanged
)

type ControlProjectionCursor struct {
    Generation                    uint64
    Revision                      uint64
    ProjectionFingerprint         string
    TransitionPrefixAccumulator   string
    TransitionRegistryFingerprint string
    CursorFingerprint             string
}

type ValidatedControlProjectionView struct {
    RuntimeSessionID      string
    SessionLifecycle      TerminalSessionLifecycleControlView
    RunControl            TerminalRunControlView
    PendingInteraction    TerminalPendingInteractionControlView
    PromptQueue           TerminalPromptQueueControlView
    ServerNotifications   TerminalServerNotificationProjection
    ViewFingerprint       string
}

type ControlProjectionPhase uint8
const (
    ControlProjectionUninitialized ControlProjectionPhase = iota + 1
    ControlProjectionFresh
    ControlProjectionSnapshotRequired
)

type FreshControlProjectionState struct {
    View                ValidatedControlProjectionView
    ConfirmedCursor     ControlProjectionCursor
    SnapshotFingerprint string
}

type SnapshotRequiredControlProjectionState struct {
    StaleView            ValidatedControlProjectionView
    ConfirmedCursor      ControlProjectionCursor
    ConfirmedSnapshotFingerprint string
    ObservedLatestCursor ControlProjectionCursor
    InvalidationKind     ControlInvalidationKind
    InvalidationFingerprint string
}

type ControlProjectionState struct {
    phase            ControlProjectionPhase
    fresh            FreshControlProjectionState
    snapshotRequired SnapshotRequiredControlProjectionState
}

type TeardownState struct {
    Phase                    TeardownPhase
    Reason                   TeardownReason
    Generation               uint64
    Deadline                 time.Time
    DetachCommandID          string
    StopAcceptingEffects     bool
    PhysicalOperationCount   uint32
    SecretRuntimeRevoked     bool
    SchedulerDrained         bool
    BridgeDrained            bool
    TerminalRestoreCompleted bool
}
~~~

Token/kind矩阵是closed contract：`OperationToken`只接受`OpTransportAuth | OpHello | OpAttach | OpAttachAck | OpHeartbeat | OpProjectionSnapshot | OpOperationalSnapshot | OpObserve | OpHistoryPage | OpMutation | OpCommandQuery | OpSecretReveal | OpSecretSubmit`；`LocalOperationToken`只接受`OpConnect | OpChallengePromote | OpChallengePromotionConfirm | OpChallengeRevokePrepared | OpChallengeRevokeActive | OpSecretEdit | OpClipboard | OpOpenURL | OpTick | OpReconnect | OpTeardown`。任一kind出现在错误token类型、zero operation generation、非matching AppGeneration或challenge effect借用普通`OpReconnect/OpTeardown` token均constructor fail closed。

S1中央input/result carriers也在本阶段一次冻结，不能只留下类型名：

~~~go
type KeyAction uint16
const (
    KeyText KeyAction = iota + 1
    KeyEnter
    KeyBackspace
    KeyDelete
    KeyLeft
    KeyRight
    KeyUp
    KeyDown
    KeyHome
    KeyEnd
    KeyPageUp
    KeyPageDown
    KeyTab
    KeyBackTab
    KeyEscape
    KeyInterrupt
    KeyEOF
)

type KeyModifiers uint8
const (
    KeyModShift KeyModifiers = 1 << iota
    KeyModAlt
    KeyModCtrl
)

type NormalizedKey struct {
    Action    KeyAction
    Modifiers KeyModifiers
    TextUTF8  string // 仅KeyText非空；<=256 UTF-8 bytes；不得用于secret-mode累计
    Repeat    bool
}

type MouseWheelDirection uint8
const (
    MouseWheelScrollUp MouseWheelDirection = iota + 1
    MouseWheelScrollDown
)

type MouseWheelInputMsg struct {
    Header     LocalMessageHeader
    Direction  MouseWheelDirection
    VisualRows uint8 // V1固定为3；caller不得自报任意幅度
}

type TickKind uint8
const (
    TickHeartbeat TickKind = iota + 1
    TickReconnect
    TickCursorBlink
    TickNotificationExpiry
    TickTeardownDeadline
)

type PasteBoundary uint8
const (
    PasteStarted PasteBoundary = iota + 1
    PasteCompleted
    PasteCancelled
)

type ParentShutdownReason uint8
const (
    ParentRequestedShutdown ParentShutdownReason = iota + 1
    ParentPipeClosed
    ParentProcessExited
    ParentProtocolRevoked
)

type PublicTeardownDisposition uint8
const (
    TeardownCompleted PublicTeardownDisposition = iota + 1
    TeardownDeadlineExceeded
    TeardownEmergencyRestoreRequired
)

type PublicTeardownSummary struct {
    TeardownGeneration       uint64
    Disposition              PublicTeardownDisposition
    CancelledOperationCount  uint32
    DrainedOperationCount    uint32
    RevokedSecretHandleCount uint32
    DetachAttempted          bool
    DetachConfirmed          bool
    TerminalRestoreCompleted bool
    Failure                  PublicFailure
    HasFailure               bool
}

type LocalNotificationKind uint8
const (
    LocalNotificationInfo LocalNotificationKind = iota + 1
    LocalNotificationWarning
    LocalNotificationError
    LocalNotificationCommandOutcome
)

type LocalNotification struct {
    NotificationID   string
    Kind             LocalNotificationKind
    PublicText       string // <=512 characters且<=2048 UTF-8 bytes
    SourceFingerprint string
    CreatedAt        time.Time
    ExpiresAt        time.Time
    Sticky           bool
}

type ComposerRevision struct {
    Revision        uint64
    DraftUTF8       string // <=32 KiB
    GraphemeCursor  uint32
    PreferredColumn int
    DraftFingerprint string
}
~~~

其他closed numeric enum的exact vocabulary冻结为：

| Type | Values |
|---|---|
| `protocolvalue.AttachmentControllerDisposition` | Protocol exact `OBSERVER_ATTACHED`、`CONTROLLER_GRANTED`、`CONTROLLER_UNAVAILABLE_OBSERVER_ATTACHED`；generated value，Go不得新增值 |
| `protocolvalue.TransportAuthDisposition` | Protocol exact `AUTHENTICATED`、`COMPATIBLE_AUTH_WINNER`、`ACK_RESULT_RECOVERY`、`AUTHENTICATION_REJECTED` |
| `protocolvalue.TransportBindingDisposition` | Protocol exact `REBOUND`、`COMPATIBLE_ALREADY_REBOUND` |
| `protocolvalue.AttachResultDisposition` | Protocol exact `CREATED`、`REBOUND_PRE_ACK`、`COMPATIBLE_ALREADY_REBOUND_PRE_ACK` |
| `protocolvalue.ServerClosingReason` | Protocol exact `HOST_CLOSE`、`RUNTIME_SHUTDOWN`、`PROTOCOL_UPGRADE_REQUIRED` |
| `protocolvalue.AttachAckDisposition` | Protocol exact `ACKNOWLEDGED`、`COMPATIBLE_ALREADY_ACKNOWLEDGED` |
| `protocolvalue.BootstrapRequirement` | Protocol V1唯一`PROJECTION_AND_OPERATIONAL_SNAPSHOT_REQUIRED`；generated value，Go不得新增none/durable-only值 |
| `protocolvalue.HeartbeatLivenessDisposition` | Protocol exact `ATTACHMENT_ACTIVE_LEASE_RENEWED`、`SESSION_CLOSING_LEASE_NOT_RENEWED` |
| `protocolvalue.HeartbeatRejectedReason` | Protocol exact `STALE_ATTACHMENT`、`STALE_TRANSPORT_BINDING`、`ATTACHMENT_REVOKED`、`ATTACHMENT_EXPIRED`、`SESSION_CLOSED` |
| `SubmitAvailability` | `SubmitDisabled`、`SubmitAvailable`、`SubmitBlockedControlStale`、`SubmitBlockedReadOnly`、`SubmitBlockedCapacity`、`SubmitBlockedInteraction` |
| `DensityMode` | `DensityCompact`、`DensityStandard`、`DensityWide`、`DensityConstrained` |
| `FocusTarget` | `FocusTranscript`、`FocusComposer`、`FocusInteraction`、`FocusQueue` |
| `ClientMouseMode` | `MouseDisabled`、`MouseCellMotion`、`MouseAllMotion` |
| `TeardownPhase` | `TeardownIdle`、`TeardownStoppingEffects`、`TeardownDetaching`、`TeardownDraining`、`TeardownRestoringTerminal`、`TeardownTerminal` |
| `TeardownReason` | `TeardownUserQuit`、`TeardownParentShutdown`、`TeardownServerClosing`、`TeardownSignal`、`TeardownFatalCompatibility`、`TeardownClientInvariant` |

`ControllerHeldByOther`或`ControllerReconciliation`若需要用于禁用按钮/显示banner，只属于`presentation.ControlProjectionState`与client-local rendering availability；它们不能进入`AttachSemanticWinner`、`AttachmentState.ControllerDisposition`、Attach compatible-winner equality或wire fingerprint。Protocol enum的numeric value、unknown/UNSPECIFIED disposition与Go validation switch均由`protocolvalue/vocabulary_gen.go`生成，手写mirror enum是architecture failure。

`NewNormalizedKey`验证closed action/modifier、UTF-8与bounds；非`KeyText`必须`TextUTF8 == ""`。Public failure不能由caller选择code：`PhysicalOperationRegistry`是唯一physical stage与terminalization-attempt owner，只允许scheduler/writer/reader通过borrower-scoped progress capability执行`Installed -> WriteStarted -> RequestFullySent -> ResponseReadStarted -> ResponseFullyValidated | Terminalizing -> Terminal`。Settlement caller只提交closed cause，不再提交任何connection-invalidation或reader-exited boolean。无需废弃connection的branch调用package-private `settleLocalFailure(initialSettlementCapability, signal)`；需要废弃connection的branch只调用`beginConnectionTerminalization()`并得到opaque attempt handle，不能取得successor capabilities或physical task。Registry-owned worker独立执行`invalidate/close -> physical drain -> receipt -> settleConnectionFailure`。两个settle入口都从registry中已安装的exact operation与stage派生delivery phase，并由package-private `newPhysicalOperationFailureReceipt()`形成receipt。Caller不得传入或覆盖`DeliveryNotStarted`、connection state、join disposition或post-join cause。唯一`ClassifyPublicFailure(receipt, sanitizedMessage)`生成`FailureProductionFact -> PublicFailureCode -> FailureDisposition`。Terminalization attempt/handle、prepared carrier、两张receipt、`FailureProductionFact`和`PublicFailure`全部使用unexported fields；外部只能调用read-only getter，不能用Go struct literal伪造code/disposition/evidence。

`PhysicalOperationRegistry`在client runtime启动时创建一个service-owned bounded terminalization worker；它不是发起read/write的caller task，也不会因caller context取消而退出。Registry记录在effect发出前安装，exact token完成或connection terminal后退役。每张capability与operation ID/generation/registry nonce exact join且严格一次性；stale capability、阶段倒退、跨operation settlement和same stage conflicting cause全部fail closed。需要废弃connection时，caller把closed `PhysicalFailureSignal`与initial `OperationSettlementCapability`交给`beginConnectionTerminalization()`；begin在同一registry lock内消费initial capability、把operation从当前ordinary stage CAS为`PhysicalTerminalizing`、冻结signal/cause、创建唯一`ConnectionTerminalizationAttemptIdentity`和初始snapshot，并把两张successor capability仅安装进private record。`attemptIdentityFingerprint = H("connection-terminalization-attempt-identity:v1", identity fields except itself)`创建后永不变化；`attemptStateFingerprint = H("connection-terminalization-attempt-state:v1", attempt identity fingerprint, state revision, state, optional handle/receipt/completion fields)`在每次state transition递增revision后重算。只有attempt成功进入worker ready queue后begin才返回opaque `ConnectionTerminalizationAttemptHandle`；handle只覆盖stable identity，不携带mutable snapshot fingerprint，caller永远看不到`PreparedConnectionTerminalization`、drain handle或successor capability。Worker admission在registry内部是non-awaiting bounded handoff；若worker已closing或capacity contract冲突，begin必须在消费initial capability前fail closed。

Attempt worker消费内部physical terminalization capability并调用exact connection owner的`startInvalidateClose(capability, attempt identity)`。该入口不把“winner已安装”等同于“physical task已启动”：connection owner在同一把owner lock内按`(connection identity, attempt identity fingerprint)`安装immutable handle、stable launch permit与`PhysicalDrainReserved` record，CAS使binding不可新borrow，并把record ID加入常驻connection drain supervisor的bounded ready set；ready-set insertion与record install属于同一个owner-lock linearization，随后只做non-awaiting wake。Caller或attempt worker自身绝不直接创建cancel/close goroutine。

结果矩阵唯一为：首次matching install返回`PhysicalDrainCreated + exact handle + observed RESERVED`；同一attempt/capability的任何重驱返回`PhysicalDrainCompatibleAlreadyCreated + 同一handle + 当前closed record state`，不再次消费capability，并再次幂等wake supervisor；同connection不同attempt/capability或same identity different payload返回`PhysicalDrainConflict`且handle absent。Created/compatible两个branch必须`hasDrainHandle=true`且observed state non-zero；conflict必须`hasDrainHandle=false`、zero handle、zero observed state并使client invariant latch。Compatible branch遇到`RESERVED | STARTING | RUNNING`都不能假称task已经存在或已经完成；其唯一保证是stable launch record仍由supervisor拥有。

Drain handle identity覆盖drain ID/generation、stable attempt identity、connection/binding、terminalization capability及开始terminalization时已经安装的reader/writer owner identity；不覆盖未来record state、runner lease、exit receipt或attempt mutable state。未创建某个I/O owner时其ID必须为空且generation为0，已经创建则ID non-empty、generation > 0并与connection registry exact join。Launch permit fingerprint覆盖drain identity、connection identity与launch generation，record创建后不换代；runner lease fingerprint覆盖permit、runner identity/generation与predecessor lease，首张lease的predecessor为空，panic重驱必须形成exact successor chain。`resultFingerprint = H("physical-connection-drain-start-result:v1", disposition, nested drain identity fingerprint, observed record state)`；compatible retry必须逐byte返回同一handle，允许observed state与result attribution随真实record推进，不能只返回同一drain ID却更换stable owner字段。Connection owner按每connection最多一个nonterminal drain winner保留record到matching attempt terminal或whole process teardown。

常驻connection drain supervisor是launch record的唯一consumer，状态机固定为：

```text
RESERVED -> STARTING -> RUNNING -> TERMINAL
```

Supervisor每次启动/重启都先扫描owner内全部nonterminal records，ready wake只降低延迟、不是唯一恢复来源。其`driveRecord()`在取得任何runner lease前已经安装supervisor-owned `defer/recover`，随后从bounded ready set取record并在owner lock内裁决：`RESERVED`安装第一张active runner lease并推进`STARTING`；`STARTING | RUNNING`且存在active lease时只等待/观察，不启动第二个runner；`STARTING | RUNNING`但active lease已由panic recovery确认退出时，安装同一launch permit下的successor runner lease并继续同一drain；`TERMINAL`只返回已安装receipt。Runner lease必须在执行任何socket side effect前安装。V1只允许`driveRecord()`在同一supervised execution stack内inline执行physical steps，禁止为drain再启动未受管child goroutine，也禁止把裸`go` statement当作launch proof。Panic/ordinary runner failure由已安装的defer在owner lock内release active lease、保留state并重新加入ready set。即使panic发生在record install后、第一张runner lease前，record仍为`RESERVED`且会被全表scan发现；即使panic发生在`STARTING/RUNNING`任意physical step，successor lease也只重驱幂等的cancel、socket close与exact reader/writer join。同一时刻最多一张active runner lease，因此不会并发启动第二个physical drain。

Drain record字段矩阵固定为：`RESERVED`必须有stable handle/launch permit、`driveQueued=true`、无runner/receipt；`STARTING | RUNNING`必须无receipt，且恰有`active runner lease`或`driveQueued=true`二者之一，不能两者都无或同时为真；panic recovery在同一owner lock内完成`active lease clear + driveQueued=true + stateRevision advance`，不存在可观察的unowned state；`TERMINAL`必须`driveQueued=false`、无active lease并有exact drain-bound terminal receipt。`supervisorGeneration`只能在constant supervisor重启时单调增加，不能改变drain/launch identity。`recordStateFingerprint = H("physical-connection-drain-record-state:v1", drain identity, state revision, state, supervisor generation, drive queued, launch permit, optional runner lease, optional terminal receipt)`；same revision different fingerprint、state倒退或违反存在矩阵都fail closed。

Registry在收到created/compatible result后于registry lock内把typed handle exact写入attempt snapshot，再由worker通过`waitPhysicalDrain(handle)`等待connection-owned record进入TERMINAL。若worker在connection owner安装winner后、registry snapshot安装前panic，重驱再次调用`startInvalidateClose()`取得compatible winner并幂等wake supervisor；若snapshot已经保存handle，则先调用`rebindPhysicalDrain(attempt identity, drain identity fingerprint)`并要求逐字段返回同一个handle，再继续wait。`rebindPhysicalDrain()`只借出同一launch record的opaque handle，不创建runner、不重发cause、不消费capability；`waitPhysicalDrain()`只观察该handle对应的record completion。Registry worker/task取消只能detach这次wait，不能取消supervisor、active runner lease或drain record。`INSTALLED | INVALIDATING | PHYSICAL_DRAINING`的所有重驱都必须走这些exact seam，禁止依赖自由字符串、process-global map猜测或重新生成drain ID。Ordinary request/read deadline和`waitConnectionTerminalization()` waiter deadline都不能取消physical owner、丢失post-join capability或删除attempt。

Physical exit后，connection supervisor只允许从matching `PhysicalDrainRunning` record构造terminal receipt；receipt的`physicalDrainIdentityFingerprint`必须等于record nested handle的`drainIdentityFingerprint`，然后在同一owner lock内安装receipt并推进`PhysicalDrainTerminal`。`waitPhysicalDrain(handle)`只在record TERMINAL且receipt drain fingerprint与input handle exact相等时返回；same connection/capability但different drain identity、receipt先于RUNNING、TERMINAL缺receipt或receipt被替换都fail closed。Attempt worker随后使用record内post-join capability调用`settleConnectionFailure()`，安装completion并唤醒所有waiters。

Attempt snapshot存在矩阵固定为：`INSTALLED`无drain handle/receipt/completion；`INVALIDATING`只允许handle absent或matching handle present，后者表示connection winner已取得但registry尚未推进下一state；`PHYSICAL_DRAINING`必须有handle且无terminal/failure receipt；`RECEIPT_READY`必须有handle和connection terminal receipt、无failure receipt，receipt physical drain fingerprint必须等于handle identity；`SETTLING`保持前述两者且无completion；`TERMINAL`必须同时有handle、matching drain-bound connection receipt、failure receipt与non-empty completion fingerprint。任何state倒退、同state revision不同state fingerprint、handle被替换、receipt来自foreign drain或optional字段违反矩阵都fail closed。

`waitConnectionTerminalization()`的竞速只在registry lock内裁决。每次首次检查与wake后复查都按固定顺序执行：matching attempt已安装immutable completion时一律返回`TerminalizationWaitCompleted`，即使cancel channel同时ready或deadline同时到达；尚无completion时，已观察到caller cancellation返回`TerminalizationWaiterCancelled`，否则absolute deadline到达返回`TerminalizationWaiterDeadline`，否则安装waiter。Cancel与deadline同一裁决点同时成立时cancel优先。后二者只detach该waiter，不改变attempt、worker、drain handle或其他waiter。

Wait result字段矩阵冻结为：`COMPLETED`要求`hasFailureReceipt=true`、receipt等于attempt immutable completion、`completionFingerprint` non-empty且与receipt/attempt snapshot exact join；`WAITER_CANCELLED | WAITER_DEADLINE`要求`hasFailureReceipt=false`、zero-value receipt、`completionFingerprint == ""`，handle仍只引用stable attempt identity。任何detached result携receipt/completion、completed result缺receipt、或waiter handle引用state fingerprint都属于client invariant。Wait返回后若completion才安装，caller可凭同一stable handle再次wait/query；registry不得改写先前detached result。

Receipt fingerprint覆盖connection/binding identity、terminalization capability fingerprint、exact physical drain identity fingerprint、closed reason、socket-close proof、reader/writer exact operation ID/generation、exit dispositions/fingerprints与terminal sequence。Constructor必须先验证receipt drain fingerprint等于current RUNNING record handle，再验证connection/capability/I/O owner；不能从其他winner复制相同connection fields生成compatible receipt。`readerExit=PhysicalIOJoined`要求non-empty reader operation ID且generation > 0，并与connection registry exact reader owner相等；`readerExit=PhysicalIONotStarted`要求ID为空、generation=0且使用fixed not-started exit fingerprint。Writer矩阵更严格：只有terminalization从`PhysicalInstalled`且已证明zero bytes/尚未创建writer operation时才允许`PhysicalIONotStarted`、空ID与generation 0；从`PhysicalWriteStarted | PhysicalRequestFullySent | PhysicalResponseReadStarted | PhysicalResponseFullyValidated`进入terminalization时，writer必须`PhysicalIOJoined`、携non-empty exact ID/generation并与connection writer owner相等。尤其`PhysicalRequestFullySent`强制reader与writer均JOINED，不能用writer NOT_STARTED receipt。

`settleConnectionFailure(postJoinCapability, receipt)`只由attempt worker调用。它验证post-join capability的prepared/signal/terminalization fingerprints与registry attempt record逐项相等，验证attempt snapshot handle fingerprint等于receipt `physicalDrainIdentityFingerprint`，再验证connection owner中matching drain record已经TERMINAL且保存exact same receipt，最后验证receipt的connection/binding、terminalization capability、reason以及reader/writer owner/exit proof；成功后一次性消费post-join capability、把operation与attempt都CAS到Terminal并安装immutable completion。Receipt缺失、drain identity缺失/漂移、非terminal record、record receipt不同、post-join capability重复使用、把initial capability交给post-join settle、重新提交cause或writer/reader identity不匹配均为`FailureClientInvariant`。

`drainConnectionTerminalizations(closeDeadline)`由single teardown owner调用，等待所有installed/invalidating/draining/receipt-ready/settling attempts以及其matching RESERVED/STARTING/RUNNING drain records真实terminal，但不取消registry worker或connection supervisor。Close deadline到期只使client close保持blocked并把exact stable attempt/drain handles交给parent emergency restore/child-reap owner；registry、drain record/launch permit/runner lease、successor capabilities、receipt slot和completion仍保留到process termination或真实terminal。新的connection/reconnect、operation registry close、connection supervisor destruction和runtime owner destruction在matching attempt与drain record均terminal前禁止。Attempt count固定不超过current connection count且每connection最多一项，不存在无界retry task。

Registry worker panic/ordinary task cancellation不能删除attempt：runtime supervisor记录closed operational failure、读取最新`ConnectionTerminalizationAttemptSnapshot`，按同一stable identity重新驱动`INSTALLED | INVALIDATING | PHYSICAL_DRAINING | RECEIPT_READY | SETTLING`。`INVALIDATING`缺handle时只能通过idempotent start取得compatible winner；已有handle时必须exact rebind。Connection drain supervisor独立负责`RESERVED | STARTING | RUNNING` record的runner lease recovery；registry worker不得自行launch physical task。两层supervisor都不得重新生成cause/capability/drain identity。只有whole client process退出才结束未terminal attempt；parent在恢复终端后可以按supervision policy终止child process，但不得让Go client先报告graceful teardown成功。

Cause/reason pair唯一为：`CauseDeadlineExpired -> ConnectionTerminalReadDeadline`、`CauseEOF -> ConnectionTerminalEOF`、`CauseReadFailed -> ConnectionTerminalReadFailure`、`CauseWriteFailed -> ConnectionTerminalWriteFailure`、`CauseMalformedResponse -> ConnectionTerminalMalformedFrame`；server closing和caller teardown只由对应push/teardown owner使用后两个terminal reason，不能被ordinary failure caller选择。

Architecture gate要求attempt identity/snapshot/record、prepared/successor capability constructors、`beginConnectionTerminalization`、registry worker loop、wait linearization、`settleLocalFailure`、`settleConnectionFailure`与`newPhysicalOperationFailureReceipt`只能在`internal/client/operation_registry.go`定义；drain record/state/launch permit/runner lease/handle constructors、constant supervisor loop、`startInvalidateClose` compatible-winner registry、`rebindPhysicalDrain`、`waitPhysicalDrain`与`newPhysicalConnectionTerminalReceipt`只能在`internal/client/connection.go`定义/调用，physical drain三入口只能由registry worker调用，runner只能由connection supervisor驱动。Production其他package对attempt internals、prepared carrier、drain record/permit/lease/handle、两个receipt与successor capabilities的struct literal/borrow或替代入口的AST observation必须为0。`settleConnectionFailure`唯一call-site是registry worker；caller task只能持opaque stable attempt handle。Receipt必须与attempt中capability/prepared/signal、connection/binding、exact drain handle fingerprint、terminal drain record、reader/writer operation ID/generation exact join；pre-write local failure只能走`settleLocalFailure`，winner无launch owner、STARTING/RUNNING无recoverable runner lease、并发runner、fully-sent writer NOT_STARTED、timeout/EOF/read failure/partial frame缺少receipt、receipt drain fingerprint缺失/漂移、携未joined receipt、start compatible winner漂移、rebind/wait漂移或capability/cause/reason不匹配均为`FailureClientInvariant`。第一层classifier固定为：

| Operation/cause | Delivery/connection condition | 唯一 code |
|---|---|---|
| connect | dial failed、尚未建立connection | `FailureConnect` |
| transport auth / peer / bootstrap / Hello / Attach | matching closed rejection或validation cause | 对应`FailurePeerIdentity | FailureBootstrap | FailureTransportAuthentication | FailureProtocolVersion | FailureProtocolSchema | FailureRequiredCapability | FailureAttach` |
| heartbeat/snapshot/operational/observe/page/query | request fully sent后read deadline | connection supervisor由exact drain record关闭stream、join reader/writer并签发drain-bound terminal receipt；registry验证TERMINAL record并消费后才产生`FailureReadTimeout`；page contract validation另为`FailureHistoryPage` |
| any ordinary read | EOF/read/write failure或connection invalidated | `FailureTransportIO`；heartbeat owner可收窄为`FailureHeartbeat` |
| mutation | `DeliveryNotStarted`且有writer证明zero bytes | `FailureCommandPreDispatch` |
| mutation | `DeliveryWriteStarted | DeliveryRequestFullySent | DeliveryResponseReadStarted`，或connection在这些phase失效 | `FailureCommandDeliveryUnknown` |
| mutation | response wait deadline after full send | `FailureCommandOutcomeTimeout` |
| secret reveal | 任意transport/timeout/cancel | `FailureSecretTransport` |
| secret submit | zero-byte predispatch proof | `FailureSecretTransport`；可撤销handle但不形成query |
| secret submit | write started及以后、outcome未知 | `FailureSecretSubmitDeliveryUnknown` |
| projection/operational payload | fully read但typed validation失败 | `FailureProjectionSnapshot | FailureOperationalSnapshot` |
| local clipboard/open URL | local integration failure | `FailureClipboard | FailureOpenURL` |
| teardown | ordinary failure/deadline | `FailureTeardown | FailureTeardownDeadline` |
| any operation | caller cancellation before wire write | `FailureCancelled`；mutation/secret submit在write started后不得归此code |
| any | impossible token/phase/cause combination | `FailureClientInvariant` |

同一physical receipt只能分类一次；operation kind、request ID、delivery phase或connection generation与message header不一致时constructor拒绝。第二层code/disposition矩阵固定为：

| PublicFailureCode | 唯一 FailureDisposition | Update语义 |
|---|---|---|
| `FailureConnect` | `FailureRetryWithBackoff` | bounded redial |
| `FailurePeerIdentity`、`FailureBootstrap`、`FailureTransportAuthentication`、`FailureProtocolVersion`、`FailureProtocolSchema`、`FailureRequiredCapability`、`FailureAttach`、`FailureClientInvariant` | `FailureFatal` | 进入fatal/parent relaunch，不本地重试 |
| `FailureTransportIO`、`FailureHeartbeat` | `FailureReconnect` | settlement outstanding后按capability reconnect |
| `FailureReadTimeout` | `FailureReconnect` | 使旧connection与physical reader终止，在fresh binding上重建stable read request |
| `FailureHistoryPage` | `FailureRetryRead` | 仅当旧response已完整读取且typed page validation终结后，同authority建立新read token |
| `FailureCommandPreDispatch` | `FailureRetryWithBackoff` | 只重发same stable candidate；必须保留zero-byte proof |
| `FailureCommandOutcomeTimeout`、`FailureCommandDeliveryUnknown` | `FailureQueryCommand` | 只query stable command identity，不重发mutation |
| `FailureProjectionSnapshot` | `FailureRebuildDurableSnapshot` | coalesce一次fresh durable snapshot |
| `FailureOperationalSnapshot` | `FailureRebuildOperationalSnapshot` | 清空operational view并resnapshot |
| `FailureSecretTransport` | `FailureRevokeSecret` | revoke plaintext/handle，禁止自动重放 |
| `FailureSecretSubmitDeliveryUnknown` | `FailureRevokeSecretAndQuery` | 先revoke plaintext，再以无plaintext stable identity query/reconcile |
| `FailureTeardown`、`FailureTeardownDeadline` | `FailureContinueTeardown` | 继续restore/drain，必要时parent emergency restore |
| `FailureCancelled`、`FailureClipboard`、`FailureOpenURL` | `FailureNoRetry` | terminal local outcome |

未知code、code/disposition不一致、`FailureReadTimeout`绑定mutation或usable connection/retry-read、delivery-unknown mutation绑定retry-read，或production fact与message operation/header不一致，全部由message validator拒绝并进入`FailureClientInvariant`；不能降级成retryable。`NewValidatedPeerIdentity`验证effective/peer/socket UID相等及validation fingerprint；不支持peer PID的平台只能显式`HasPeerPID=false`。`NewBuildIdentity`验证semver、GOOS/GOARCH closed support matrix及全部fingerprint。`NewPublicTeardownSummary`验证count、disposition与failure矩阵。上述carrier都提供`Validate() error`，禁止public struct literal。

`NewLocalNotification`强制ID/fingerprint、UTF-8 bounds与`ExpiresAt >= CreatedAt`；`NewComposerRevision`重算draft fingerprint并验证cursor boundary。Array未使用slot必须保持zero，不参与View。Server notifications只能存在于`ControlProjectionFresh.View`或`ControlProjectionSnapshotRequired.StaleView`，只能由validated atomic control snapshot安装，最多16项且按stable ordinal/ID canonical order，不能由local constructor追加；`TickNotificationExpiry`只清理`LocalNotificationState`。Server notification即使已过显示期限也只在View中派生hidden状态，不能从server vector删除、改fingerprint或推进local伪cursor；正式删除必须来自下一份server control snapshot。

其他package slice同样不是opaque占位：

| State owner | 必备字段 |
|---|---|
| `presentation.State` | valid/stale、runtime session、authority high-water、projection revision、projection contract、active head、ordered immutable resident vector、latest root cursor pair、bounded pinned-root states、snapshot fingerprint；不保存control section |
| `presentation.OperationalState` | valid、generation、cursor、immutable coalesce-key map、snapshot fingerprint、dropped diagnostic count |
| `presentation.ControlProjectionState` | closed `UNINITIALIZED | FRESH(view, confirmed_cursor, snapshot_fingerprint) | SNAPSHOT_REQUIRED(stale_view, confirmed_cursor, confirmed_snapshot_fingerprint, observed_latest_cursor, invalidation)` union；五section唯一server-projected owner，observed cursor永不冒充view authority |
| `commandstate.Registry` | bounded ordered command records；每项含kind、command ID、request semantic fingerprint、target/generations、phase、query token、last exact outcome fingerprint |
| `interaction.State` | exact projected-view fingerprint reference、local selection metadata、enabled actions；server interaction value只存在`ControlProjectionState`，不得复制authority |
| `queue.State` | exact projected-view fingerprint reference、local pending command metadata、stale flag；server QueueItem vector只存在`ControlProjectionState`，不得保存local inferred queue item |
| `secret.State` | active opaque handle ID、lease fingerprint/generation/expiry、request key、masked length/validation metadata、local phase；不含URL/form plaintext |

为保证S1可以编译完整`AppState`且后续没有第二套type identity，S1必须创建四个最终owner的dormant closed state：

| Final S1 owner | S1 constructor与不可变结构 | S1合法状态 | 后续激活 |
|---|---|---|---|
| `commandstate.Registry` | `NewDormantRegistry(maxRecords=64)`；固定数组、count、registry generation、enabled flag | `enabled=false,count=0`；任何candidate/outcome transition拒绝为`FeatureNotActivated` | S3只修改transition/constructor policy，不重建type |
| `interaction.State` | `NewDormantState()`；phase、control cursor/view fingerprint reference、target reference、selection、enabled-actions fixed fields | snapshot前为`InteractionDormant`；S1 snapshot后允许`InteractionReadOnlyProjected`，只引用Control中exact server view且`enabled_actions=0` | S4只激活actions/transition，不重建view identity |
| `queue.State` | `NewDormantState(maxItems=64)`；phase、control cursor/view fingerprint reference、pending command metadata、stale flag | snapshot前为`QueueDormant`；S1 snapshot后允许`QueueReadOnlyProjected`，只引用Control中0..64项projection且无mutation | S5只激活mutation/transition，不重建item identity |
| `secret.State` | `NewDormantState()`；phase、opaque handle/lease/request/masked metadata fixed fields | `SecretDormant`且无handle/lease | S4只修改transition并接入runtime owner |

`Dormant`不是placeholder或兼容stub：这些`state.go`文件、type layout、constructors、validators与package ownership在S1即为最终版本。`Dormant`只表示尚未安装server control snapshot，不能用来拒绝合法的pending interaction或non-empty queue。S1必须提供final constructors：

```go
func NewReadOnlyProjectedInteraction(
    cursorFingerprint string,
    projectedViewFingerprint string,
    targetID string,
    targetGeneration uint64,
) (interaction.State, error)

func NewReadOnlyProjectedQueue(
    cursorFingerprint string,
    projectedViewFingerprint string,
    negotiatedMaximum uint32,
) (queue.State, error)
```

App snapshot installer先验证完整`presentation.ControlProjectionState`，再从其nested section提取上述immutable references。Interaction constructor验证view fingerprint、target/generation及cursor exact join，然后强制清空selection并设置`enabled_actions=0`；它不保存public text/options。Queue constructor要求`negotiatedMaximum == 64`并引用已经验证过0..64 bound、canonical order、unique IDs、active-state/head/account/count/accumulator的Control section；它不复制item vector、不截断、不分页、不从历史row过滤。`RECONCILIATION_REQUIRED` item可由Control显示但永远没有client action。S1 Update不得产生interaction/queue effect；S3/S4/S5文件清单必须把这些state owners列为“修改”，不得再次“新增”或复制DTO。

每个state type必须提供`New...()`和`Validate() error`。除本package constructor/transition外禁止struct literal；slice/map均private并在constructor中deep-copy。`NewInitialAppState()`必须生成`PhaseBooting + disconnected connection + invalid attachment + SnapshotLoadingUninitialized + invalid presentation/operational/control + disabled composer + four dormant final owners + teardown idle`，其validator还必须执行：

- `PhaseLoadingSnapshot`要求`SnapshotLoadingState`处于AwaitingDurable或AwaitingOperational且严格满足presence矩阵；`PhaseReady`要求valid attachment、`SnapshotBaselinesInstalled`、matching durable/operational fingerprints与`ControlProjectionFresh`；`PhaseReadOnly`同样要求BaselinesInstalled，Control只能为Fresh，或者为携完整stale view + matching confirmed cursor + same-generation strictly newer observed cursor / typed generation-rebase target的`ControlProjectionSnapshotRequired`；
- serial connection lane至多一个outstanding operation；page、command、secret与teardown各自在对应state slice安装bounded token。Wire token必须含exact non-empty RequestID，local token必须物理不存在RequestID；post-attach wire token还必须逐项绑定current attachment与transport-binding generation/fingerprint，pre-attach token则强制这些字段为zero；
- attachment role为observer时controller generation仍可展示但mutation disabled；
- viewport root只能是latest或retention-bound pinned root；
- control projection为`SnapshotRequired`时interaction/queue/run mutation全部disabled，且interaction/queue仍只能引用confirmed cursor，不得引用observed latest cursor；
- valid durable snapshot要求interaction/queue cursor都等于snapshot baseline confirmed control cursor；`Dormant`仅在snapshot前合法，snapshot后的read-only/projected state必须完整接纳server view，不能要求pending/queue为空；
- `AttachmentChallengePreparedPromotionPending`只允许处于Negotiating、绑定current `OpHello`与matching winner/receipt，且不得存在active handle或`OpAttach`；`AttachmentChallengeActiveAcceptancePending`要求matching promotion receipt与`OpChallengePromotionConfirm | OpChallengeRevokeActive` local token，仍禁止`OpAttach`；`AttachmentChallengeActive`只允许matching application-acceptance receipt已安装、prepared identity已消费且下一步为Attaching/`OpAttach`。同一connection最多一个prepared、active-pending或active challenge；new Hello/connection generation必须先取得matching revoke receipt；
- `PhaseReady` + `ControlProjectionSnapshotRequired | Uninitialized`、`PhaseReadOnly` + Uninitialized、Ready/ReadOnly但loading state非BaselinesInstalled、AwaitingOperational却缺durable result或仍存在durable outstanding、Fresh cursor与view fingerprint不一致、SnapshotRequired中same-generation observed cursor不比confirmed cursor新，或generation-change缺typed rebase reason，都是constructor-level invariant failure；
- `PhaseExited`要求teardown terminal且无outstanding operation、prepared/active challenge或secret handle。

Physical objects由`internal/client/runtime.go`中的单一`ClientRuntimeOwner`持有：connection handles、transport-auth credentials、`PREPARED -> ACTIVE_PENDING_APPLICATION_ACCEPTANCE -> ACTIVE -> CONSUMED | REVOKED` hello challenge records、operation cancellation/drain records，以及S4后注入的`SecretRuntimeOwner`。Authority-bearing AppState/application Message/Effect只保存opaque identity；唯一例外是framework input进入boundary normalizer前的bounded transient bytes，必须按TUI-BT-APP-003与secret ingress规则立即转成one-shot edit handle。Runtime owner按closed local operation token执行prepare/promote/confirm/borrow/revoke；prepared和active-pending challenge必须绑定delivery guard与absolute deadline，close必须在single teardown deadline内revoke/drain全部challenge records。它不是新的semantic context，也不得读取View state或推导server outcome。

Challenge runtime port固定为：

~~~go
func (r *ClientRuntimeOwner) PrepareAttachmentChallenge(
    helloOperation OperationToken,
    challengeBytes [32]byte,
    validatedReceiptFingerprint string,
    candidateFingerprint string,
    connectionID string,
) (PreparedAttachmentChallengeHandleIdentity, error)

func (r *ClientRuntimeOwner) PromotePreparedAttachmentChallenge(
    operation LocalOperationToken, // kind == OpChallengePromote
    prepared PreparedAttachmentChallengeHandleIdentity,
) (AttachmentChallengePromotionReceipt, error)

func (r *ClientRuntimeOwner) ConfirmAttachmentChallengePromotion(
    operation LocalOperationToken, // kind == OpChallengePromotionConfirm
    promotion AttachmentChallengePromotionReceipt,
) (AttachmentChallengeAcceptanceReceipt, error)

func (r *ClientRuntimeOwner) RevokeAttachmentChallenge(
    operation LocalOperationToken, // kind == OpChallengeRevokePrepared | OpChallengeRevokeActive
    target AttachmentChallengeRevocationTarget,
    handleFingerprint string,
    reason AttachmentChallengeRevocationReason,
) (AttachmentChallengeRevocationReceipt, error)
~~~

Prepare只从exact validated Hello operation/receipt创建`PREPARED`；Promote只接受PREPARED，Confirm只接受`ACTIVE_PENDING_APPLICATION_ACCEPTANCE`。Attach borrow只接受matching `AttachmentChallengeAcceptanceReceipt`关联的ACTIVE record。Runtime owner还持有每次message delivery guard：bridge send失败或result没有被Update接纳时，由guard调用同一pure revoke core并保存receipt；guard入口不接受caller自报target/reason，而是从record phase与closed delivery disposition派生。所有receipt fingerprint覆盖除自身外完整字段，不能进入wire fingerprint或server authority。

AppState 不保存：

- net.Conn、os.File、context.CancelFunc、goroutine、channel；
- generated Protobuf pointer；
- mutable shared map/slice；
- Python class/module identity；
- plaintext secret string；
- wall clock callback。

I/O executor 是 Model 的 process-local dependency，不属于 AppState，不参与 View、snapshot、debug dump 或 equality。所有时间变化通过 typed TickMsg 进入 Update。

### TUI-BT-APP-002 Closed phases

合法 phase transition：

~~~text
Booting
  -> Connecting

Connecting
  -> Negotiating
  -> Reconnecting
  -> Fatal

Negotiating
  -> Attaching
  -> Reconnecting
  -> Fatal

Attaching
  -> LoadingSnapshot
  -> Reconnecting
  -> Fatal

LoadingSnapshot
  -> Ready
  -> ReadOnly
  -> Reconnecting
  -> Fatal

Ready
  -> ReadOnly
  -> Reconnecting
  -> Detaching
  -> Fatal

ReadOnly
  -> Ready
  -> Reconnecting
  -> Detaching
  -> Fatal

Reconnecting
  -> Negotiating
  -> Fatal
  -> Detaching

Detaching
  -> Exited
  -> Fatal
~~~

没有 generic string phase。非法 transition 是 client invariant failure，进入 bounded fatal view；不得 panic 后继续使用旧 attachment。

### TUI-BT-APP-003 Closed messages

Application messages 必须由 app package 定义 concrete type，并实现 package-private marker。client package只能实例化这些类型。

消息集合冻结为：

| 类别 | Concrete messages |
|---|---|
| lifecycle | AppStartedMsg、ParentShutdownMsg、ReconnectDueMsg、ServerClosingMsg、TeardownCompletedMsg |
| framework input | KeyInputMsg、MouseWheelInputMsg、PasteInputMsg、PasteBoundaryMsg、ResizeMsg、FocusChangedMsg、TickMsg |
| connection | ConnectSucceededMsg、ConnectFailedMsg、TransportAuthenticatedMsg、TransportAuthenticationFailedMsg、HelloAcceptedMsg、AttachmentChallengePromotedMsg、AttachmentChallengePromotionAcceptedMsg、AttachmentChallengePromotionFailedMsg、AttachmentChallengeRevokedMsg、HelloNegotiationUnavailableMsg、HelloRejectedMsg、AttachAcceptedMsg、AttachRejectedMsg、AttachAcknowledgedMsg、AttachAckFailedMsg、HeartbeatAcceptedMsg、HeartbeatRejectedMsg、ConnectionLostMsg |
| bootstrap | SnapshotAcceptedMsg、SnapshotRejectedMsg、OperationalSnapshotAcceptedMsg、OperationalSnapshotRejectedMsg |
| observation | ObservationBatchMsg、ObservationNoChangeMsg、LocalObservationOverflowMsg |
| history | PageDataMsg、PageStaleMsg、PageRebaseMsg、PageReconciliationMsg、PageFailedMsg |
| command | CommandOutcomeMsg、CommandQueryFoundMsg、CommandQueryMissingMsg、CommandTransportFailedMsg |
| secret | SecretEditReadyMsg、SecretHandleInstalledMsg、SecretBufferChangedMsg、SecretSubmittedMsg、SecretRevokedMsg、SecretTransportFailedMsg |
| local effect result | ClipboardResultMsg、OpenURLResultMsg、ReleaseCheckResultMsg |

Bubble Tea framework 原始消息只在top-level `FrameworkIngressNormalizer`中出现。v2.0.6 allowlist至少包括KeyPressMsg、MouseWheelMsg、MouseClickMsg、MouseReleaseMsg、MouseMotionMsg、PasteStartMsg、PasteMsg、PasteEndMsg、WindowSizeMsg、KeyboardEnhancementsMsg。Vertical wheel唯一转换为固定3 visual rows的`MouseWheelInputMsg`；horizontal wheel及非wheel pointer event只形成bounded operational advisory，不改变application semantic state，也不得触发fatal。Ordinary composer mode立刻转为`KeyInputMsg/PasteInputMsg`；active secret form mode不得构造这两个含text的message，而是把bounded normalized edit安装进`SecretRuntimeOwner`的one-shot edit cell，再只产生`SecretEditReadyMsg(target handle, edit handle)`。Component不直接switch framework message。

未知 application message 是编程错误并进入 fatal diagnostic。未知 Bubble Tea 非必需 capability message可被 operationally ignore，但必须有计数，不得改变 authority-bearing state。

所有request result、local result、connection lifecycle与server push message都必须使用下列互斥的closed header，并归一到同一个stale verdict；它们不共享或伪造RequestID：

~~~go
type MessageDisposition uint8
const (
    MessageApply MessageDisposition = iota + 1
    MessageCompleteStaleOperation
    MessageDropStaleAuthority
    MessageTriggerGap
    MessageFatalCompatibility
)

type IOMessageHeader struct {
    Operation            OperationToken
    PayloadFingerprint   string
    ReceivedAt           time.Time
}

type LocalMessageHeader struct {
    AppGeneration uint64
    Sequence      uint64
    ProducedAt    time.Time
}

type LocalResultHeader struct {
    Operation          LocalOperationToken
    PayloadFingerprint string
    ReceivedAt         time.Time
}

type ConnectionLossCause uint8
const (
    ConnectionEOF ConnectionLossCause = iota + 1
    ConnectionReadFailure
    ConnectionWriteFailure
    ConnectionProtocolClose
    ConnectionParentRevoked
)

// ConnectionLifecycleHeader is local physical attribution, not a request result.
type ConnectionLifecycleHeader struct {
    ConnectionHandleID         string
    ConnectionGeneration       uint64
    AttachmentID               string
    AttachmentGeneration       uint64
    TransportBindingGeneration uint64
    TransportBindingFingerprint string
    LifecycleSequence          uint64
    Cause                      ConnectionLossCause
    RelatedOperationID         string
    RelatedOperationGeneration uint64
    HasRelatedOperation        bool
    ObservedAt                 time.Time
}

// ServerPushHeader is the validated immutable form of Protocol ServerPushHeader.
type ServerPushHeader struct {
    ConnectionHandleID   string
    ConnectionGeneration uint64
    AttachmentID         string
    AttachmentGeneration uint64
    TransportBindingGeneration uint64
    TransportBindingFingerprint string
    PushGeneration       uint64
    PushSequence         uint64
    HeaderFingerprint    string
    ReceivedAt           time.Time
}
~~~

`NewConnectionLifecycleHeader`是connection owner的唯一constructor。Lifecycle sequence在每个connection generation内严格递增；idle loss要求related fields全零，in-flight loss要求它们exact current outstanding；该header只作process-local physical attribution，不拥有semantic fingerprint。`NewServerPushHeader`只接受Protocol validator已经验证的wire header并逐项绑定current connection/attachment/transport binding；它不得从本地arrival order重建push identity。

跨语言closed vocabulary与validated carrier的唯一Go owner是generated `internal/protocolvalue`。它不保存Protobuf message/pointer，decoder验证后deep-copy成private-field value并清除generated input。S1必须生成且只生成下列read-only carrier（字段集合与Protocol DTO逐项等集）：

| Carrier | 必须保留的proof |
|---|---|
| `ValidatedAttachAckResult` | request、attachment identity/generation/fingerprint、semantic winner fingerprint、acknowledged binding fingerprint、exact ACK disposition、retired credential optional、ACK result fingerprint |
| `ValidatedRecoveredAttachAck` | 完整nested `ValidatedAttachAckResult`、auth result request/attempt/connection/candidate/result fingerprint、完整resulting transport binding与rebind receipt |
| `PreparedHeartbeatRequest` | Protocol `HeartbeatRequest`全部字段及重算通过的request fingerprint |
| `ValidatedHeartbeatAcceptedReceipt` | Protocol accepted receipt全部字段及重算通过的receipt fingerprint |
| `ValidatedHeartbeatRejectedReceipt` | Protocol rejected receipt全部字段及重算通过的receipt fingerprint |
| `PreparedOperationalSnapshotRequest` | Protocol request全部字段及重算通过的request fingerprint |
| `ValidatedOperationalSnapshotFrame` | Protocol frame全部字段、deep-copied ordered cells、count/bytes/accumulator、opaque state/contract fingerprint及重算通过的outer frame fingerprint |

这些carrier只提供getters与closed branch accessor；不存在接收自由enum/string/fingerprint的public constructor。Generated `FromProto` factory只接受exact generated message + raw validated frame metadata，并由AST call-site gate限制在`internal/wire/decode.go`；其他package即使能import也不得调用。`app`只能exact join，`presentation`只能从validated operational frame构造immutable operational state。手写mirror DTO、把validated result拆成几个string再入队、或让decoder直接构造`AttachmentState`均为architecture failure。

S1使用的concrete messages字段冻结如下：

~~~go
type AppStartedMsg struct {
    Header LocalMessageHeader
    BootstrapHandleID, TransportCredentialHandleID string
    HandshakeCandidate HandshakeRecoveryCandidateIdentity
}
type ParentShutdownMsg struct { Header LocalMessageHeader; Reason ParentShutdownReason }
type ReconnectDueMsg struct { Header LocalMessageHeader; ReconnectGeneration uint64 }
type TeardownCompletedMsg struct { Header LocalResultHeader; Summary PublicTeardownSummary }

type KeyInputMsg struct { Header LocalMessageHeader; Key NormalizedKey }
type MouseWheelInputMsg struct { Header LocalMessageHeader; Direction MouseWheelDirection; VisualRows uint8 }
type PasteInputMsg struct { Header LocalMessageHeader; ChunkUTF8 string; ByteCount uint32 }
type PasteBoundaryMsg struct { Header LocalMessageHeader; Boundary PasteBoundary }
type ResizeMsg struct { Header LocalMessageHeader; Width int; Height int }
type FocusChangedMsg struct { Header LocalMessageHeader; Focused bool }
type TickMsg struct { Header LocalMessageHeader; Kind TickKind; TickGeneration uint64 }

type ConnectSucceededMsg struct {
    Header             LocalResultHeader
    ConnectionHandleID string
    Peer               ValidatedPeerIdentity
}
type ConnectFailedMsg struct { Header LocalResultHeader; Failure PublicFailure }
type TransportAuthenticatedMsg struct {
    Header                 IOMessageHeader
    ConnectionHandleID     string
    ServerConnectionID     string
    TransportAuthAttemptID string
    HandshakeCandidateFingerprint string
    Disposition protocolvalue.TransportAuthDisposition
}
type TransportAuthenticationFailedMsg struct {
    Header IOMessageHeader
    AuthAttemptID, ServerConnectionID string
    Failure PublicFailure
}
type HelloAcceptedMsg struct {
    Header  IOMessageHeader
    Winner  HelloNegotiationSemanticWinner
    Receipt ValidatedServerHelloReceipt
}
type AttachmentChallengePromotedMsg struct {
    Header LocalResultHeader
    Receipt AttachmentChallengePromotionReceipt
}
type AttachmentChallengePromotionAcceptedMsg struct {
    Header LocalResultHeader
    Receipt AttachmentChallengeAcceptanceReceipt
}
type AttachmentChallengePromotionFailedMsg struct {
    Header LocalResultHeader
    PreparedHandleFingerprint string
    Failure PublicFailure
}
type AttachmentChallengeRevokedMsg struct {
    Header LocalResultHeader
    Receipt AttachmentChallengeRevocationReceipt
}
type HelloNegotiationUnavailableMsg struct {
    Header IOMessageHeader
    CandidateTerminalReceipt HandshakeCandidateTerminalReceipt
    OutcomeFingerprint string
}
type HelloRejectedMsg struct {
    Header IOMessageHeader
    CandidateTerminalReceipt HandshakeCandidateTerminalReceipt
    OutcomeFingerprint string
}
type AttachAcceptedMsg struct {
    Header                               IOMessageHeader
    SemanticWinner                       AttachSemanticWinner
    Receipt                              AttachResultReceipt
    ReconnectCredentialHandleID          string
    ReconnectCredentialCarrierFingerprint string
    HasReconnectCredentialHandle         bool
}
type AttachRejectedMsg struct { Header IOMessageHeader; Failure PublicFailure }
type AttachAcknowledgementProofKind uint8
const (
    AttachAcknowledgementOrdinary AttachAcknowledgementProofKind = iota + 1
    AttachAcknowledgementRecoveredViaTransportAuth
)
type OrdinaryAttachAcknowledgementProof struct {
    AckResult protocolvalue.ValidatedAttachAckResult
    AttachResultReceiptFingerprint string
}
type RecoveredAttachAcknowledgementProof struct {
    Recovery protocolvalue.ValidatedRecoveredAttachAck
}
type AttachAcknowledgementProof struct {
    kind      AttachAcknowledgementProofKind
    ordinary OrdinaryAttachAcknowledgementProof
    recovered RecoveredAttachAcknowledgementProof
}
type AttachAcknowledgedMsg struct {
    Header IOMessageHeader
    Proof  AttachAcknowledgementProof
}
type AttachAckFailedMsg struct { Header IOMessageHeader; Failure PublicFailure }
type HeartbeatAcceptedMsg struct {
    Header  IOMessageHeader
    Receipt protocolvalue.ValidatedHeartbeatAcceptedReceipt
}
type HeartbeatRejectedMsg struct {
    Header  IOMessageHeader
    Receipt protocolvalue.ValidatedHeartbeatRejectedReceipt
}
type ConnectionLostMsg struct { Header ConnectionLifecycleHeader; Failure PublicFailure }
type ServerClosingMsg struct {
    Header           ServerPushHeader
    Reason           protocolvalue.ServerClosingReason
    RemainingGrace   time.Duration
    DetachAllowed    bool
    FrameFingerprint string
}

type SnapshotAcceptedMsg struct { Header IOMessageHeader; Snapshot presentation.ValidatedSnapshot }
type SnapshotControlRebaseRequiredMsg struct {
    Header IOMessageHeader
    RequestedMinimumControlCursorFingerprint string
    LatestControlCursor ControlProjectionCursor
    ResponseFingerprint string
}
type SnapshotRejectedMsg struct { Header IOMessageHeader; Failure PublicFailure }
type OperationalSnapshotAcceptedMsg struct {
    Header IOMessageHeader
    Snapshot protocolvalue.ValidatedOperationalSnapshotFrame
}
type OperationalSnapshotRejectedMsg struct { Header IOMessageHeader; Failure PublicFailure }

type ClipboardResultMsg struct { Header LocalResultHeader; Succeeded bool; Failure PublicFailure }
type OpenURLResultMsg struct { Header LocalResultHeader; Succeeded bool; Failure PublicFailure }
type ReleaseCheckResultMsg struct { Header LocalResultHeader; Succeeded bool; Failure PublicFailure }
~~~

`HelloAcceptedMsg`只携带完整stable `HelloNegotiationSemanticWinner`和current-connection `ValidatedServerHelloReceipt`；后者是Go application carrier，不是第二个wire DTO。Generated decoder先按Protocol helper从exact 32 bytes重算challenge commitment/receipt fingerprint；通过后只能调用`ClientRuntimeOwner.PrepareAttachmentChallenge()`，把bytes安装为`PREPARED` record，并取得不含plaintext的`PreparedAttachmentChallengeHandleIdentity`。Prepared identity必须绑定current `OpHello` operation/request、connection、candidate、winner、receipt、commitment与该operation absolute deadline；它不能被`AttachEffect` borrow。Generated wire bytes随后清除。

`HelloAcceptedMsg` constructor先验证wire semantic fields；`ValidateFor(AppState)`再验证candidate/generation、selected protocol/capabilities/limits、capability contract、negotiation transcript、current `OpHello` request、connection和prepared identity逐项exact join。`MessageApply`只把winner/receipt与prepared identity装入`AttachmentChallengePreparedPromotionPending`，并以`OpChallengePromote` token产生`PromotePreparedAttachmentChallengeEffect`；此时禁止进入Attaching或产生`AttachEffect`。Runtime owner只在effect携带的expected candidate/receipt/connection仍等于prepared record时原子执行`PREPARED -> ACTIVE_PENDING_APPLICATION_ACCEPTANCE`，消费prepared generation并返回带promotion receipt的`AttachmentChallengePromotedMsg`；该handle此时仍不能被Attach borrow。

`AttachmentChallengePromotedMsg`只有`MessageApply`时才把AppState切到`AttachmentChallengeActiveAcceptancePending`并以`OpChallengePromotionConfirm`生成`ConfirmAttachmentChallengePromotionEffect`。任何stale/drop/fatal disposition都必须以`OpChallengeRevokeActive`产生`RevokeActiveAttachmentChallengeEffect(target=RevokeActivePendingAttachmentChallenge)`；bridge无法交付promoted message时，runtime owner delivery guard执行同一revoke。Confirm effect携exact promotion receipt、candidate/receipt/connection与active handle identity，runtime owner验证后执行`ACTIVE_PENDING_APPLICATION_ACCEPTANCE -> ACTIVE`并返回带one-shot acceptance receipt的`AttachmentChallengePromotionAcceptedMsg`。只有matching accepted message获得`MessageApply`，AppState才进入`AttachmentChallengeActive`并允许构造携exact acceptance receipt fingerprint的`AttachEffect`；Attach encoder borrow必须原子消费该receipt，不能只凭active handle ID/fingerprint取bytes。

若`AttachmentChallengePromotionAcceptedMsg`最终为stale/drop/fatal、无法交付，或其local operation token被successor/teardown取代，runtime owner acceptance-delivery guard必须以typed `RevokeActiveAttachmentChallenge`路径撤销已经ACTIVE的handle与未消费acceptance receipt；Update收到非apply result时也产生同一closed revoke effect。Runtime owner在acceptance receipt被Attach borrow消费或revoke receipt形成前始终是active handle的物理owner，因此不存在“物理promotion/confirmation成功但application未接纳”的无owner窗口。Promotion failure必须先确认prepared/active-pending record已撤销，再由`AttachmentChallengePromotionFailedMsg`进入reconnect/fatal closed path。

`AttachEffect` preparation失败、effect被scheduler取消或executor在borrow前拒绝时，也必须用acceptance receipt exact定位并撤销ACTIVE record；只有Attach encoder成功原子borrow challenge bytes时才消费acceptance receipt并执行`ACTIVE -> CONSUMED`。Borrow之后的transport失败按Attach physical operation规则处理，不能重新借用旧challenge或把CONSUMED退回ACTIVE。

`HelloAcceptedMsg`本身出现`MessageCompleteStaleOperation | MessageDropStaleAuthority | MessageFatalCompatibility`时不得promote prepared identity，Update以`OpChallengeRevokePrepared`产生matching `RevokePreparedAttachmentChallengeEffect`。Constructor failure、bridge在`Program.Send`前/中取消、message无法交付、operation successor、new Hello receipt、connection close、prepared deadline与teardown也都由ClientRuntimeOwner delivery guard自动执行idempotent revoke。所有promote/confirm/revoke effect与result必须使用上述四个closed local operation kind；用`OpTick | OpReconnect | OpTeardown`代替或未安装exact local token均为client invariant。Challenge record在CONSUMED/REVOKED前由runtime owner计入bounded drain；AppState删除identity、程序退出或effect取消不能让它无owner滞留。Pre-Ready物理重试可prepare fresh receipt/challenge，但同一candidate的winner fingerprint必须不变，且旧connection的任意phase challenge先取得revoke receipt；同candidate不同winner是fatal compatibility failure。

`HelloNegotiationUnavailableMsg | HelloRejectedMsg`只能由matching `HelloOutcome` negative branch构造：Header必须匹配current `OpHello`，nested candidate ID/fingerprint/generation等于AppState current candidate，terminal receipt fingerprint与outer outcome fingerprint都由Protocol-generated helper重算成功，且disposition/reason/required-client-disposition组合必须属于Protocol closed matrix。Update先把current candidate原子安装为terminal，对任何matching prepared/active challenge产生revoke并等待runtime owner确认，不得只清AppState handle，然后使old connection进入close；随后`HelloClientParentRelaunchNewCandidate`才产生`RequestParentRelaunchEffect`，unavailable映射`ParentRelaunchNegotiationWinnerUnavailable`，只有`HelloTerminalCandidateExpired`的rejected branch映射`ParentRelaunchHelloRejected`；`HelloClientFatalCompatibility`直接进入`PhaseFatal + BeginTeardownEffect`且禁止auto relaunch。不允许通过`PublicFailure.Message`、opaque stable code或current server config反推该transition；outer response丢失后的same-candidate retry必须安装same terminal receipt。Incoming same-generation/different-fingerprint candidate conflict在auth/registry admission被拒绝，不能生成针对已安装candidate的terminal message。

Reconnect capability decoder必须先验证carrier public identity等于`SemanticWinner.NextReconnectCredential`、安装mutable credential bytes到`ClientRuntimeOwner`并清除wire副本，再生成只含opaque handle与carrier fingerprint的`AttachAcceptedMsg`。`HasReconnectCredentialHandle`必须与winner optional identity、receipt optional carrier、selected reconnect capability三方同时存在或同时缺失；handle借出的carrier fingerprint必须等于receipt字段。该message还必须分别验证stable `SemanticWinner`与current-connection `Receipt`：`SemanticWinner.HelloNegotiationWinnerFingerprint`必须等于已安装Hello winner，candidate/generation、attachment identity与winner fingerprint exact相等，receipt request ID匹配current `OpAttach`，binding connection等于current connection；`CREATED`禁止previous binding，两个pre-ACK rebind branch要求previous binding等于已安装old receipt且generation严格增加。Update由二者构造`AttachmentState`，不能让connection ID进入semantic winner fingerprint。

`AttachAcknowledgedMsg`矩阵强制：ordinary branch要求`Header.Operation.Kind == OpAttachAck`、payload fingerprint等于完整`ValidatedAttachAckResult` fingerprint，nested request、attachment、semantic winner、acknowledged binding与本次`AttachResultReceipt.CurrentTransportBinding`逐项exact join；auth tombstone recovery要求`Header.Operation.Kind == OpTransportAuth`、payload fingerprint等于完整`TerminalTransportAuthResult` fingerprint、closed proof只存在`ValidatedRecoveredAttachAck`，其nested ACK逐项匹配stable winner，ACK acknowledged binding等于原FULL ACK binding，rebind receipt previous fingerprint等于installed binding，resulting connection等于current且binding generation严格增加。两个branch互斥，inactive branch必须zero；不再使用`RecoveredViaTransportAuth`、`HasRecoveredBinding`或散装fingerprint字段。Recovery不得伪造一个从未dispatch的`OpAttachAck` token，也不得把resulting rebind fingerprint覆盖nested ACK实际确认的binding。`ValidatedSnapshot`与`ValidatedOperationalSnapshotFrame`已经deep-copy并丢弃generated Protobuf；各自validator覆盖TUI-BT-CONSUME-002/006全部join。

`HeartbeatAcceptedMsg`只能由完整accepted receipt构造。Validator要求Header current `OpHeartbeat`、request ID、attachment/winner、submitted current binding、heartbeat candidate/generation与schedule中的`NextHeartbeatGeneration` exact join；`previous_accepted`必须等于local last accepted。Fresh-binding retry可以使用new operation/request/binding，但必须复用same generated candidate fingerprint；accepted receipt中的semantic disposition/expiry必须与candidate先前winner一致。`MessageApply`只允许从receipt更新existing `AttachmentState.ExpiresAt`，不得覆盖attachment/winner/controller/bootstrap/binding；liveness disposition只安装进client-local heartbeat schedule。随后以`Header.ReceivedAt + Attachment.Heartbeat.Interval`更新next-at、清零missed count并递增next generation。I/O decoder不得传入`NextHeartbeatAt`或新的`AttachmentState`。`SESSION_CLOSING_LEASE_NOT_RENEWED`不得更新expiry，安装receipt后立即停止heartbeat/observe并进入typed closing。Rejected message执行Protocol closed reason矩阵，不能把rejected receipt降成普通`PublicFailure`或在same stale binding无条件retry。

每个concrete message必须有同名constructor与validator：

~~~go
func NewConnectSucceededMsg(
    h LocalResultHeader,
    connectionHandleID string,
    peer ValidatedPeerIdentity,
) (ConnectSucceededMsg, error)

func (m ConnectSucceededMsg) ValidateFor(s AppState) MessageDisposition
~~~

其余类型机械使用`New<Type>(all fields) (<Type>, error)`与`ValidateFor(AppState) MessageDisposition`；禁止public struct literal。Constructor负责non-empty、bounds、closed enum、fingerprint syntax、deep copy和payload fingerprint覆盖；`ValidateFor`按header kind执行join：ordinary response匹配request/operation，connection lifecycle匹配connection generation，unsolicited push匹配connection/attachment/push generation。三类header不可互换：

| 条件 | Verdict |
|---|---|
| exact current operation + exact current authority | `MessageApply` |
| request/operation generation已被同kind successor替代 | `MessageCompleteStaleOperation`；只能退休old outstanding，不改current authority |
| attachment/controller generation过期 | `MessageDropStaleAuthority`并记bounded diagnostic |
| projection/control/operational base与current不连续 | `MessageTriggerGap` |
| same identity different fingerprint、unknown required enum/oneof、impossible branch | `MessageFatalCompatibility` |

S2–S6新增message必须复用上述header/constructor/validator，不得仅向type switch添加裸payload。字段覆盖矩阵冻结为：

| Message family | Required payload identity |
|---|---|
| snapshot | request + attachment/current transport binding；optional minimum observed control cursor；accepted atomic durable snapshot/echo或typed control-generation rebase result；operational snapshot必须携完整validated Protocol frame |
| heartbeat | request + attachment semantic identity/current transport binding；heartbeat generation/previous accepted generation；完整accepted/rejected receipt |
| observation batch | request + attachment/current transport binding；每个present plane的base/result cursor、branch fingerprint与完整validated immutable payload；1..3 plane canonical set |
| page | request + attachment/current transport binding；requested cursor fingerprint；direction；response branch/fingerprint |
| command/query | request + attachment/current transport binding/controller；command ID；request semantic fingerprint；target generation；outcome/query token/fingerprint |
| interaction/control change（batch nested） | attachment；control base/result revision；完整view/cursor fingerprint；snapshot-required disposition |
| secret handle | request + attachment/current transport binding/controller；lease fingerprint/generation/expiry；opaque handle ID；不含plaintext |
| connection lifecycle | connection handle/generation；optional related operation只作attribution；不要求RequestID |
| unsolicited server push | connection/attachment generation；push generation/sequence；frame fingerprint；不得完成outstanding request |
| teardown | teardown generation；reason；deadline；physical drain summary |

S2–S6每个message的exact non-header payload如下；`Header`默认为`IOMessageHeader`，明确标记local、lifecycle或push者除外：

| Message | Exact payload fields（Header以外） |
|---|---|
| `SnapshotControlRebaseRequiredMsg` | requested minimum control cursor fingerprint、latest control cursor、stable reason、response fingerprint |
| `ObservationBatchMsg` | request identity；optional validated durable branch、operational branch、control branch；included-plane count、batch fingerprint；至少一项且每plane最多一项 |
| `LocalObservationOverflowMsg`（local only） | local overflow cause、latest known plane cursors、affected plane bitset、gap generation |
| `ObservationNoChangeMsg` | exact echoed durable/operational/control cursors |
| `PageDataMsg` | requested cursor/direction、validated root、immutable ordered entries、before/after cursors、has-more flags、response fingerprint |
| `PageStaleMsg` | requested cursor fingerprint、latest root、optional replacement cursor+proof、response fingerprint |
| `PageRebaseMsg` | requested cursor fingerprint、latest root、opaque bounded token、response fingerprint |
| `PageReconciliationMsg` | requested cursor fingerprint、fault code、owner identity、optional retry delay/trusted root hint、response fingerprint |
| `PageFailedMsg` | failure、requested cursor fingerprint、direction |
| `CommandOutcomeMsg` | command kind/ID、request semantic fingerprint、target/generation、closed status、public result、durable refs、query token、outcome fingerprint |
| `CommandQueryFoundMsg` | command ID、request semantic fingerprint、exact `CommandOutcomeMsg` value |
| `CommandQueryMissingMsg` | command ID、request semantic fingerprint |
| `CommandTransportFailedMsg` | command ID、request semantic fingerprint、failure、delivery phase enum |
| `SecretEditReadyMsg`（local） | target secret handle ID、one-shot edit handle ID、secret edit generation；无edit plaintext |
| `SecretHandleInstalledMsg` | interaction/request key、lease identity metadata、opaque handle ID、masked metadata；no plaintext |
| `SecretBufferChangedMsg` | opaque handle ID、edit generation、masked length、closed validation summary |
| `SecretSubmittedMsg` | opaque source handle ID、sealed response handle ID、receipt attribution |
| `SecretRevokedMsg`（push） | `ServerPushHeader`；opaque handle ID、lease fingerprint、closed revoke reason |
| `SecretTransportFailedMsg` | opaque handle ID（optional before install）、request key、failure；no plaintext |
| `ServerClosingMsg`（push） | `ServerPushHeader`；closed reason、remaining grace、detach allowed、frame fingerprint |

这些payload types全部为app或lower package的value struct；generated pointer、`[]byte` secret、`any`和map不得出现。新增字段必须先更新本表与constructor tests，不允许只在client decoder中私下携带。

`ConnectionLostMsg`可以在没有任何outstanding request时产生；其`ConnectionLifecycleHeader`的related-operation fields此时必须为空。若断连时存在in-flight operation，header只可附带该operation ID/generation作为结算归因，绝不能伪造request response。每个authenticated connection在建立时都安装一个process-local connection-lifecycle terminalization owner；idle EOF、ServerClosing与caller teardown没有ordinary outstanding时，以该owner构造内部attempt/drain identity，但不能把它伪装进related-operation fields。Physical connection supervisor只能从matching RUNNING drain record在socket close且reader/writer join后签发带exact `physicalDrainIdentityFingerprint`的`PhysicalConnectionTerminalReceipt`，再经operation registry产生唯一`ConnectionLostMsg`；drain record未TERMINAL时message不得提前进队。对一次断连不得再为same operation发送SnapshotRejected/PageFailed/CommandTransportFailed等第二个terminal message。Update由该单一message按矩阵结算outstanding；旧connection generation的late response一律stale drop。`ServerClosingMsg`可与任意ordinary response竞速；bridge先交付push并安装single teardown permit，随后按以下矩阵terminalize existing operation：

| Current operation | 唯一 disposition |
|---|---|
| heartbeat/snapshot/operational snapshot/page/observe | `InterruptedByServerClosing`，丢弃partial并停止retry |
| command mutation/query | stable candidate转`QueryRequiredAfterReattach`，禁止重发 |
| secret reveal | revoke handle/transient bytes，禁止auto reveal |
| secret submit | revoke plaintext；可能已send时只保留无plaintext query/reconciliation identity |
| auth/Hello/Attach/ACK before matching attachment | push非法，按protocol incompatibility close |
| local clipboard/open/tick | cancel或complete stale，不产生server outcome |
| teardown/detach | merge到same teardown generation，不创建第二owner |

EOF/read/write loss使用同一operation settlement矩阵，但connection owner只能提交sealed `PhysicalOperationFailureReceipt`，由中央classifier决定code/disposition。`CommandTransportFailedMsg.delivery_phase`必须等于`PublicFailure.Production().DeliveryPhase()`，二者还必须绑定同一operation/request；mutation已进入write-started后任何`FailureReadTimeout | FailureCancelled | FailureCommandPreDispatch`都由validator拒绝。Observer disconnect、push或close都不能把command/secret不确定性折叠成“失败”。

### TUI-BT-APP-004 Closed effects

Update 只能返回 closed effects：

| Effect | 唯一用途 |
|---|---|
| ConnectEffect | 建立 Unix socket |
| AuthenticateTransportEffect | 发送initial/reconnect auth preface；credential只由runtime owner borrow |
| NegotiateHelloEffect | 发送 HelloRequest |
| PromotePreparedAttachmentChallengeEffect | application candidate/request/connection join通过后，把operation-bound prepared challenge提升为`ACTIVE_PENDING_APPLICATION_ACCEPTANCE` |
| ConfirmAttachmentChallengePromotionEffect | matching promoted message被Update接纳后，确认active handle可供Attach一次性borrow |
| RevokePreparedAttachmentChallengeEffect | stale/conflict/rejected/teardown路径撤销尚未promote的prepared challenge |
| RevokeActiveAttachmentChallengeEffect | promoted/accepted result未被application接纳或authority退出时撤销active-pending/active handle |
| AttachEffect | 发送 AttachRequest |
| AcknowledgeAttachEffect | 确认exact semantic winner + current AttachResultReceipt并触发credential retirement/rotation |
| HeartbeatEffect | 发送 HeartbeatRequest |
| RequestSnapshotEffect | 发送 ProjectionSnapshotRequest |
| RequestOperationalSnapshotEffect | 发送 OperationalSnapshotRequest |
| ObserveNextEffect | 发送 ObserveNextRequest |
| ReadHistoryPageEffect | 发送 HistoryPageRequest |
| SendMutationEffect | 发送 MutationCommand |
| QueryCommandEffect | 发送 QueryCommandRequest |
| RevealSecretEffect | 发送 SecretRevealRequest |
| SubmitSecretEffect | 发送 SecretFormSubmit |
| ApplySecretEditEffect | 将一个normalized local edit交给SecretRuntimeOwner |
| ScheduleTickEffect | 安排 monotonic tick |
| CopyPublicTextEffect | 写系统 clipboard |
| OpenPrivateURLEffect | 显式打开当前 leased URL |
| CopyPrivateSecretEffect | 显式复制当前leased secret；只携带opaque handle |
| BeginReconnectEffect | 关闭旧连接并启动 bounded backoff |
| RequestParentRelaunchEffect | 交付exact terminalized handshake candidate与closed relaunch cause，不在child内自行创建new launch credential |
| BeginTeardownEffect | 执行唯一 teardown |
| QuitProgramEffect | 返回 tea.Quit |

Effect 是 immutable request description，不持有执行中的 socket operation。client.Service 是唯一 executor；每个 effect 最终产生一个 closed message或在 teardown context 中被明确取消。

禁止 generic FuncEffect、AnyMsg、map[string]any payload、method string + JSON。

Effect字段不是自由设计。Wire effect必须直接携带Update已经安装进state的exact `OperationToken`；executor不得生成或替换OperationID/RequestID。Local effect使用物理上不含RequestID的独立header：

~~~go
type WireEffectHeader struct {
    EffectID  string
    Operation OperationToken
}

type LocalEffectHeader struct {
    EffectID  string
    Operation LocalOperationToken
}

type ConnectEffect struct { Header LocalEffectHeader; BootstrapHandleID string }
type AuthenticateTransportEffect struct {
    Header WireEffectHeader; ConnectionHandleID, CredentialHandleID string
    Candidate HandshakeRecoveryCandidateIdentity
}
type NegotiateHelloEffect struct {
    Header WireEffectHeader; ConnectionHandleID string
    TransportAuthAttemptID, TransportAuthResultFingerprint string
    Candidate HandshakeRecoveryCandidateIdentity
}
type PromotePreparedAttachmentChallengeEffect struct {
    Header LocalEffectHeader
    Prepared PreparedAttachmentChallengeHandleIdentity
    ExpectedCandidateFingerprint string
    ExpectedHelloReceiptFingerprint string
    ExpectedConnectionID string
}
type ConfirmAttachmentChallengePromotionEffect struct {
    Header LocalEffectHeader
    ActiveHandleID string
    ActiveHandleGeneration uint64
    ActiveHandleFingerprint string
    PromotionReceiptFingerprint string
    ExpectedCandidateFingerprint string
    ExpectedHelloReceiptFingerprint string
    ExpectedConnectionID string
}
type RevokePreparedAttachmentChallengeEffect struct {
    Header LocalEffectHeader
    PreparedHandleID string
    PreparedHandleGeneration uint64
    PreparedHandleFingerprint string
    Reason AttachmentChallengeRevocationReason
}
type RevokeActiveAttachmentChallengeEffect struct {
    Header LocalEffectHeader
    ActiveHandleID string
    ActiveHandleGeneration uint64
    ActiveHandleFingerprint string
    PromotionReceiptFingerprint string
    Target AttachmentChallengeRevocationTarget
    Reason AttachmentChallengeRevocationReason
}
type AttachEffect struct {
    Header WireEffectHeader; ConnectionHandleID string
    Candidate HandshakeRecoveryCandidateIdentity
    HelloNegotiationWinnerFingerprint string
    ServerHelloReceiptFingerprint string
    ActiveAttachmentChallengeHandleID string
    ActiveAttachmentChallengeHandleGeneration uint64
    ActiveAttachmentChallengeHandleFingerprint string
    AttachmentChallengeAcceptanceReceiptFingerprint string
    AttachmentChallengeCommitment string
}
type AcknowledgeAttachEffect struct {
    Header WireEffectHeader; ConnectionHandleID string
    SemanticWinnerFingerprint, AttachResultReceiptFingerprint string
    InstalledReconnectCredentialCommitment string
    HasInstalledReconnectCredential bool
}
type HeartbeatEffect struct {
    Header WireEffectHeader
    ConnectionHandleID string
    Request protocolvalue.PreparedHeartbeatRequest
}
type RequestParentRelaunchEffect struct {
    Header LocalEffectHeader
    Cause ParentRelaunchCause
    CandidateTerminalReceipt HandshakeCandidateTerminalReceipt
}
type RequestSnapshotEffect struct {
    Header WireEffectHeader
    ConnectionHandleID string
    MinimumObservedControlCursor ControlProjectionCursor
    HasMinimumObservedControlCursor bool
}
type RequestOperationalSnapshotEffect struct {
    Header WireEffectHeader
    ConnectionHandleID string
    Request protocolvalue.PreparedOperationalSnapshotRequest
}
type ScheduleTickEffect struct { Header LocalEffectHeader; TickKind TickKind; TickGeneration uint64; DueAt time.Time }
type CopyPublicTextEffect struct { Header LocalEffectHeader; PublicUTF8 string }
type BeginReconnectEffect struct { Header LocalEffectHeader; PreviousConnectionHandleID string; Backoff time.Duration }
type BeginTeardownEffect struct { Header LocalEffectHeader; Reason TeardownReason }
type QuitProgramEffect struct { Header LocalEffectHeader }
~~~

`PrepareWireOperation(current owner slice, kind, deadline)`是Update唯一wire operation constructor。它先递增该slice的next operation generation，再按TUI-PROTO-SCHEMA-003生成：

```text
operation_id = context_fingerprint("terminal-client-operation-id:v1", client instance + app/connection/operation generation + kind)
request_id   = context_fingerprint("terminal-client-request-id:v1", operation_id + kind)
```

两者加closed prefix后成为wire strings，并与全部generation/deadline一起原子安装进其state owner；connection/Hello/Attach/heartbeat/snapshot/observe走serial `ConnectionState.Outstanding`，page、command、secret分别走viewport/registry/secret owner。随后构造effect，effect header必须逐字段等于该token。Heartbeat与operational snapshot还必须在同一个pure Update内用Protocol-generated factory构造完整`PreparedHeartbeatRequest`/`PreparedOperationalSnapshotRequest`并装入effect；executor只能逐字节encode该validated request，不能补attachment、binding、generation或fingerprint。`PrepareLocalOperation`使用独立`terminal-client-local-operation-id:v1`且不生成RequestID；local token安装在对应teardown/tick/clipboard/secret-edit state或bounded runtime operation ledger。三个namespace及字段覆盖必须进入Protocol fingerprint manifest/golden；constructor失败不得返回effect。

Physical operation/request ID故意随connection generation变化；它们只做一次I/O归因。Hello/Attach compatible winner只比较同一`attachment_attempt_generation`的`HandshakeRecoveryCandidateIdentity`。Pre-Ready physical retry重新生成operation token但复用candidate ID/fingerprint/handle；Ready后的ordinary reconnect必须递增attempt generation并生成新candidate，复用old candidate或old physical token都非法。Candidate full carrier由Protocol-generated factory构造、安装到`ClientRuntimeOwner`且immutable，AppState/effect只持有validated identity与opaque handle；executor borrow后必须验证full carrier fingerprint等于effect identity。

S2–S6 effect payload matrix：

| Effect family | Required fields |
|---|---|
| observe/page | wire header、connection handle、exact durable/operational/control after cursors或page cursor+direction+limits |
| mutation/query | header、connection handle、frozen command candidate或exact command query identity；candidate含Protocol-generated request fingerprint |
| secret reveal | header、connection handle、interaction ID、request key；不含plaintext |
| secret edit | local header、secret handle、one-shot edit handle；不携带closed edit bytes |
| secret submit/open/private-copy | header、opaque secret handle；submit另带interaction/request-key identity但不带buffer bytes |
| reconnect/teardown | header、owner generation、single absolute deadline与待drain physical handle IDs |

Non-S1 concrete effect payloads冻结为：

~~~go
type ObserveNextEffect struct {
    Header WireEffectHeader; ConnectionHandleID string
    AfterAuthorityHighWater, AfterProjectionRevision uint64
    AfterOperationalGeneration, AfterOperationalCursor uint64
    AfterControlCursor presentation.ControlProjectionCursor
    MaximumWait time.Duration
}
type ReadHistoryPageEffect struct {
    Header WireEffectHeader; ConnectionHandleID string
    Cursor presentation.PageCursor; Direction presentation.PageDirection
    MaximumCells, MaximumDecodedBytes uint32
}
type SendMutationEffect struct {
    Header WireEffectHeader; ConnectionHandleID string
    Candidate commandstate.FrozenCandidate
}
type QueryCommandEffect struct {
    Header WireEffectHeader; ConnectionHandleID string
    RuntimeSessionID, OriginalClientInstanceID, CommandID string
    RequestSemanticFingerprint string
}
type RevealSecretEffect struct {
    Header WireEffectHeader; ConnectionHandleID string
    InteractionID, RequestKey string
}
type ApplySecretEditEffect struct {
    Header LocalEffectHeader; Handle secret.HandleID; EditHandle secret.EditHandleID
}
type SubmitSecretEffect struct {
    Header WireEffectHeader; ConnectionHandleID string
    InteractionID, RequestKey string; Handle secret.HandleID
}
type OpenPrivateURLEffect struct { Header LocalEffectHeader; Handle secret.HandleID }
type CopyPrivateSecretEffect struct { Header LocalEffectHeader; Handle secret.HandleID }
~~~

`client.Service`在执行`SubmitSecretEffect`时从owner取得purpose-bound borrow并构造transient wire bytes；effect本身不拥有或缓存响应body。所有physical result仍回到上一节列出的closed message。

每个effect同样只有`New<Type>(...) (<Type>, error)`可构造，并实现package-private `effectKind()`；constructor校验header/authority/deadline及payload bound。`client.Service.Execute(effect)`使用exhaustive type switch，未知effect立即返回fatal typed message；不得提供generic callback effect。

### TUI-BT-APP-005 Update

Top-level Model与pure application reducer固定执行：

~~~text
FrameworkIngressNormalizer.normalize framework message
  ordinary input -> bounded Key/Paste message
  secret input   -> SecretRuntimeOwner.InstallEdit -> opaque SecretEditReadyMsg
-> validate message belongs to current attachment/request/generation
-> pure state transition
-> derive zero or more closed effects
-> wrap effects as tea.Cmd
~~~

规则：

- pure reducer不执行socket、file、clipboard、browser、timer I/O或secret mutation；normalizer唯一允许的external mutation是把单个bounded secret edit安装进one-shot process-local cell，失败只产生closed masked failure；
- state transition 先于 effect dispatch；
- stale request ID只完成对应 outstanding operation，不修改 current state；
- stale attachment/controller generation直接丢弃并记 operational diagnostic；
- exact duplicate message + fingerprint 是 no-op；
- same identity + different fingerprint 进入 invalid/GAP 或 fatal compatibility；
- ordinary reconnect保留 composer draft和confirmed durable view；
- reconnect、GAP、takeover、interaction replacement立即revoke secret value与pending edit handles；
-任何 mutation effect 都先在 command registry 安装 frozen candidate。

### TUI-BT-APP-006 View

View 只读取 AppState 并构造 tea.View：

- 不读取 clock；elapsed由 TickMsg提前写入state；
- 不发起page、snapshot或command；
- 不修改follow-tail、selection或cache；
- 不访问secret之外的private bytes；
- declaratively设置AltScreen、cursor、mouse、paste和keyboard enhancements；
- S1固定`MouseModeCellMotion`，捕获click/release/wheel但不请求无按键pointer motion；wheel up/down只滚动resident transcript，退出后framework必须关闭mouse reporting并恢复primary screen；
-每次render输出不包含protocol frame、fingerprint、launch capability或secret diagnostic；
- narrow/short terminal仍提供quit、stop、interaction action和capacity action。

Component不得自行recover或恢复terminal mode。Production保留Bubble Tea默认panic catcher：panic时framework负责立即恢复terminal并使`Program.Run()`返回panic error；此异常路径不假装执行可信的application teardown。Python parent随后按unexpected child exit执行attachment revocation与emergency restore。`tea.WithoutCatchPanics()`在production禁止，详见TUI-BT-LIFE-002。

## 4. Connection、transport 与 bridge

### TUI-BT-CONN-001 Startup

Go 首帧必须在任何网络 I/O 完成前出现：

~~~text
starting client
-> connecting gateway
-> authenticating transport
-> negotiating protocol
-> attaching session
-> acknowledging attach result
-> loading durable snapshot
-> loading operational snapshot
-> ready
~~~

Matching Attach ACK（ordinary或auth tombstone recovery）只能把`SnapshotLoadingState`从Uninitialized切到`AwaitingDurableSnapshot`并产生一个exact `RequestSnapshotEffect`。Durable response获得`MessageApply`后，Update先原子安装durable history + complete control baseline及其fingerprint，再切到`AwaitingOperationalSnapshot`并产生exact `RequestOperationalSnapshotEffect`；不得在同一个effect、decoder callback或View中跳过中间态。Operational response获得`MessageApply`后，Update从完整validated Protocol frame构造operational state，验证attachment/binding与loading state，再切到`SnapshotBaselinesInstalled`和Ready/ReadOnly。任一阶段的timeout/retry只重建当前阶段operation；已经确认的前一阶段baseline及fingerprint保持不变。Ready validator以`SnapshotBaselinesInstalled`作为mandatory proof，不允许仅凭`presentation.State.Valid`猜测bootstrap已完成。

每个阶段都有monotonic deadline、retry和quit。Host optional dependency状态只通过server view显示，不延迟Bubble Tea program start。

S1/S2 attachment role 固定为 OBSERVER。S3开始才请求 CONTROLLER。Controller拒绝不阻止只读 attach，但所有 mutation UI disabled。

Startup capability算法：client build嵌入`implemented_slice`及由Protocol enum生成的supported/required sets；Hello发送两组canonical set，server返回immutable negotiation winner。`NewHelloAcceptedMsg`只在`required ⊆ winner.selected ⊆ client supported`、`winner.selected ⊆ winner.server_supported`、capability contract fingerprint、selected protocol、negotiation transcript fingerprint、winner fingerprint、current receipt与operation-bound prepared challenge identity join全部通过generated validator时成功；成功仅允许进入promotion-pending，不等于challenge已可用于Attach。Feature bools只能由generated `SelectedCapabilitiesToFeatureSet(winner.SelectedCapabilities)`构造，禁止Update对raw enum/string自行switch。S1不得广告S2–S6尚未实现的capability；observer/controller角色不改变build实际实现集合。

`AuthenticateTransportEffect`必须读到matching `TerminalTransportAuthResult`才terminal：`AUTHENTICATED | COMPATIBLE_AUTH_WINNER`生成`TransportAuthenticatedMsg`，其中`Header.PayloadFingerprint`就是唯一auth result fingerprint。Update先把server connection ID、auth attempt ID/header fingerprint原子安装进`ConnectionState`，再用同一值构造`NegotiateHelloEffect`；`ACK_RESULT_RECOVERY`必须同时验证完整nested `ValidatedAttachAckResult`与`RecoveredAttachmentTransportBinding`，生成closed recovery proof的`AttachAcknowledgedMsg`。Update先证明nested ACK确认的old binding，再原子把existing attachment rebind到本次server connection/resulting binding generation，随后才完成旧handshake并进入`SnapshotAwaitingDurableSnapshot`；缺失binding、binding指向其他connection、ACK acknowledged binding丢失或attachment identity漂移全部fatal。`AUTHENTICATION_REJECTED`生成typed failure并关闭connection。本地preface write成功、socket仍open或peer UID正确都不能生成`TransportAuthenticatedMsg`。Hello encoder必须逐字段验证candidate与已安装auth receipt，不得从socket state重新合成attempt identity。

### TUI-BT-CONN-002 Serial scheduler

V1同一connection只允许一个in-flight request。client.scheduler 使用四个bounded class：

1. secret/control response；
2. heartbeat；
3. snapshot/page/command/query；
4. observation long-poll。

调度规则：

- heartbeat deadline优先于新observe；
- ObserveNext.maximum_wait_ms取negotiated maximum与下次heartbeat安全余量的较小值；
- 合法observe wait完成后若heartbeat进入guard window，先heartbeat再发下一observe；
- command/secret不会与另一个request并发写同一framing stream；
- write deadline、read deadline和overall operation deadline共用同一absolute deadline；
- request尚未开始写入时的deadline可以在保持connection可用的情况下退役operation；一旦`RequestFullySent`，read deadline就不是“换一个read token继续用同一stream”的许可；
- fully-sent request超时时，registry消费initial settlement capability、冻结cause并安装service-owned terminalization attempt；stable attempt identity、mutable snapshot、两张successor capability与completion由attempt owner持有，drain record/launch permit/runner lease/handle由常驻connection supervisor持有。Caller只拿stable opaque handle，取消/deadline仅detach；supervisor按`RESERVED -> STARTING -> RUNNING -> TERMINAL`关闭socket并等待exact reader/writer JOINED，receipt携exact drain identity后才供attempt worker结算。Registry worker panic通过same-attempt compatible start或handle rebind恢复；connection supervisor panic通过nonterminal record scan和successor runner lease恢复；这之前禁止在旧stream安装新operation、写入新frame或把迟到response归给新request；
- 只有matching `PhysicalConnectionTerminalReceipt(reader=JOINED, socket=closed, physicalDrainIdentityFingerprint=exact handle)`已经由connection supervisor安装到TERMINAL drain record并被operation registry消费后，旧physical operation才terminal并允许进入reconnect；fresh authenticated transport binding上可以对heartbeat/snapshot/observe/page/query重建same semantic read，mutation仍遵守query/reconciliation矩阵；
- `FailureRetryRead`只适用于connection未被物理超时破坏、旧response已完整读取并完成typed终结的application-level validation failure，不得用于EOF、read deadline或partial frame。
- partial frame、oversize、malformed oneof或unexpected response branch使connection失效；
- request_id是response join唯一键，response branch还必须与outstanding effect kind匹配。

client不得另开第二条“heartbeat connection”绕过attachment identity。

### TUI-BT-CONN-003 Bridge backpressure

client bridge 到 Bubble Tea program 使用三类队列：

| class | 默认上限 | overflow |
|---|---:|---|
| durable observation | 64 frames | 丢弃pending delta，合成`LocalObservationOverflowMsg` |
| operational | 128 coalesce keys | 按coalesce key替换；仍超限则丢operational并请求operational snapshot |
| command/control | 32 results | 标记对应command query-required；不得推断失败 |

Secret plaintext不进入任何普通bridge或Bubble Tea message队列。Bubble Tea v2.0.6 [`Program.Send`](https://github.com/charmbracelet/bubbletea/blob/v2.0.6/tea.go)使用内部message channel且可能阻塞，因此secret decoder不得发送含plaintext的`SecretRevealedMsg`：

```text
decode SecretRevealResult
-> validate request/attachment/controller/lease identity
-> SecretRuntimeOwner.InstallPrivateURL(mutable bytes)
-> best-effort overwrite generated protobuf bytes + decode buffer
-> Program.Send(SecretHandleInstalledMsg{opaque handle, lease metadata})
```

若install后handle message未能交付、bridge被取消或Program已结束，bridge只调用`SecretRuntimeOwner.Revoke(handle)`并发送不了任何retry。普通队列只看见opaque handle ID。Generated Protobuf/client decoder在安装前不可避免地短暂持有wire bytes；architecture gate允许这条bounded transient path，但禁止retain、string conversion、log、debug dump和replay，不得作“secret package外从未出现private_url bytes”的不真实承诺。

Program done后bridge停止send。bridge dispatcher阻塞于Program.Send时，teardown cancellation必须能使其退出。Go local overflow不能反压Python Runtime writer。

### TUI-BT-CONN-004 Reconnect

Connection loss：

1. current attachment标记invalid；
2. durable view保留但显示stale/read-only；
3. operational view清空；
4. secret state清除；
5.所有outstanding page/observe/secret operation取消；
6. pending commands转QueryRequired；
7. ordinary composer draft保留；
8. 若尚未进入Ready且正在恢复Hello/Attach/Ack response loss，可borrow仍在TTL内的credential与同一attachment-attempt candidate执行bounded handshake-recovery redial；若已经Ready，则只能在S2 selected reconnect capability下borrow current reconnect credential，并由runtime owner原子创建`attachment_attempt_generation + 1`的新candidate；否则进入typed reconnect-unavailable，不发送unauthenticated Hello；
9. bounded exponential backoff建立新connection；pre-Ready retry发送相同candidate generation，Ready后的ordinary reconnect发送new generation。两者都生成fresh auth request ID/nonce/preface fingerprint；同credential的新physical attempt不是conflict；
10.重新Hello/Attach。Server复用同代`AttachSemanticWinner`，但每个connection返回matching `AttachResultReceipt`；ACK前旧receipt丢失时new receipt必须为typed pre-ACK rebind且旧binding立即失效。Next credential carrier先安装到runtime owner，AppState只保存handle/commitment；
11.发送matching `AttachReceiptAck`，同时绑定semantic winner、本次receipt和current binding；收到AckResult前client保留old credential。ACK FULL后server清除live old bytes并安装30秒bounded tombstone；AckResult丢失时新preface返回`ACK_RESULT_RECOVERY`、exact AckResult及当前connection的typed post-ACK rebind receipt。Client原子安装rebind后才能继续snapshot；S2再promote next，S1清除initial credential；
12.请求durable和operational snapshot；
13. snapshot exact install后query原command IDs；
14.重新启动observe并携带snapshot给出的control cursor。

Initial launch同样使用transport auth preface；其mutable credential cell必须保留到初次Attach ACK FULL，不能在Hello发送后清除。禁止使用旧attachment generation继续send，也禁止以新command ID重放mutation。Reconnect capability不进入AppState/message/log；borrow只在framing encoder作用域内存在并best-effort清除。

### TUI-BT-CONN-005 Version incompatibility

Hello ProtocolError、major mismatch、schema fingerprint mismatch、missing required capability或unsupported required oneof进入FatalVersionState。UI显示：

- client build；
- server build；
- client支持range；
- server selected version或stable error；
-升级建议。

不得自动启动Legacy REPL。

## 5. Wire consumption algorithms

本章只描述Go如何消费当前Protobuf。Server字段含义与fingerprint算法仍由Protocol文档拥有。

### TUI-BT-CONSUME-001 Immutable conversion

presentation constructors 必须：

-校验required oneof非空；
-校验Protocol规定的nested identity join；
-复制所有bytes、slice和map；
-把map字段设为private并只暴露copy/read-only iterator；
-丢弃generated Protobuf object；
-返回ValidatedSnapshot、ValidatedDelta等closed value；
-失败时返回stable client protocol error，不返回半成品。

Fingerprint严格消费Protocol manifest的三类classification：`WIRE_RECOMPUTABLE`只调用`internal/protocol/fingerprint_contract_gen.go`生成helper；`OPAQUE_DOMAIN_AUTHORITY`只执行格式、nested exact join、duplicate/conflict和cross-language golden；`PHYSICAL_ATTRIBUTION`只做operation correlation。Go `wire/validate.go`不得手写namespace、canonical JSON或covered field list。Manifest漏项使protocol generation/S1 build失败。

### TUI-BT-CONSUME-002 ProjectionSnapshotFrame

收到snapshot后按以下顺序：

1. request_id必须匹配唯一outstanding snapshot。
2. runtime_session_id必须等于attachment.runtime_session_id。
3. snapshot_fingerprint按`OPAQUE_DOMAIN_AUTHORITY`校验格式与nested exact join；不得声称从wire payload重算Python domain snapshot fingerprint。
4. authority_high_water必须与active_head.through_authority_sequence exact join。
5. latest_root_cursor_pair.root_identity必须与active_head.confirmed_root_identity exact join。
6. ordered_resident_entries按server顺序复制；Go不得按rank、cell kind、source sequence或arrival time重排。
7. history_entry_id必须唯一；相同ID不同entry_fingerprint拒绝。
8. 完整`TerminalControlProjectionSnapshot(view + cursor)`属于`PRESENTATION_SNAPSHOT_V1`，无论S1是否启用live control observation都必须一次原子安装成`presentation.ControlProjectionFresh`。View中的lifecycle/run-control/interaction/queue/server-notification五个section必须各自携带validated source owner generation/revision/fingerprint，并共享同一view fingerprint/control revision；Pending interaction通过`NewReadOnlyProjectedInteraction`安装local action reference，active queue通过`NewReadOnlyProjectedQueue`安装local mutation reference，server notification vector最多16项、按stable ordinal + ID排序且只存在Control state。S1把interaction/queue置为read-only且enabled actions为空，不能因feature未激活而要求snapshot内容为空；S4/S5只在既有projected reference上激活action。Gateway分项拼接、source version倒退、notification乱序或client用local notification补写server vector使整份snapshot拒绝。若request携minimum observed control cursor，frame还必须回显其fingerprint并证明result cursor不早于minimum，否则不得替换SnapshotRequired state。
9. active head capacity branch映射为client SubmitAvailability，不在Go计算quote。
10.安装新DurableState时保留ordinary draft、local view preference和仍可exact join的selection；旧root page state只在retention-bound pinned集合中保留。
11. snapshot安装完成前不发mutation。Durable snapshot只允许在`SnapshotAwaitingDurableSnapshot`安装；FULL安装后必须保留其operation/result/control cursor fingerprint，原子切到`SnapshotAwaitingOperationalSnapshot`并且只产生一个prepared operational request。
12. durable与operational snapshot独立安装；两者不伪造共同atomic fingerprint。Operational未完成时durable/control baseline已经confirmed但AppPhase仍为LoadingSnapshot，不能提前Ready；durable retry也不能覆盖已经confirmed的baseline。

Snapshot不是merge。除明确保留的client-local state外，它替换全部server-projected durable/control state。Queue超界、notification超界、truncated marker、unknown active state或server projection/count mismatch使整份snapshot拒绝；Go绝不通过截断到64/16来“修复”server contract。`LocalNotificationState`不属于snapshot，可按本地policy保留；它不能影响server control fingerprint。

Queue head decoder必须调用Protocol-generated closed-union validator，唯一branch公式为`checkpoint_transition_count + bounded_tail_count`：和为0时只允许canonical generation-0 empty genesis；和大于0时只允许committed head。因此首条queue transition到首checkpoint前必须接受`generation 0 checkpoint + non-empty exact tail receipt`，不得按checkpoint generation选择empty branch。Committed zero-tail只在checkpoint transition count大于0时合法；checkpoint/tail/head receipt任一fingerprint/sequence/count不一致拒绝整份snapshot。

### TUI-BT-CONSUME-002B ObservationBatchFrame

每个Protocol response只转换成一个`ObservationBatchMsg`，bridge不得按plane拆成多个`Program.Send`。Constructor验证request/header、batch fingerprint、`included_plane_count == present branches`、每plane至多一个branch，以及每个nested branch仍精确绑定Observe effect冻结的对应after-cursor。Empty batch非法；真正no-change使用独立`ObservationNoChangeMsg`。

Pure Update在一个state clone上按固定顺序执行：

```text
control branch
  -> Changed/Gap: preserve stale view + confirmed cursor,
                  install observed latest cursor,
                  coalesce one snapshot effect with that minimum cursor
durable branch
  -> delta / authority advance / root advance / durable GAP
operational branch
  -> delta / operational GAP
verify every nested resulting cursor and whole batch
-> atomically install all successful plane results
```

任一nested branch structural validation失败时整批不部分安装；Update按该plane返回typed GAP/rebuild，其他branch将在下一次Observe按old cursor重新交付。Control Changed/GAP不覆盖confirmed cursor；Update原子保留stale view + confirmed cursor、安装same-generation strictly newer cursor或typed generation-rebase target为observed latest、切到`PhaseReadOnly`，然后构造携minimum observed cursor的唯一snapshot effect。在该effect terminal前不再发Observe，避免从同一confirmed cursor无界重收invalidation。Matching snapshot必须回显minimum fingerprint并安装same generation/revision不低于observed latest的Fresh state；generation rebase走closed rebase response与bounded fresh snapshot request。Control stale不阻止durable/operational显示，但在同一Update结束前先关闭所有依赖control的mutation。持续durable流、持续control变更或operational 100Hz更新都不得省略同batch中另一个已经pending的plane。Local bridge overflow仍可生成`LocalObservationOverflowMsg`，但它不是server observation branch。

### TUI-BT-CONSUME-003 ProjectionDeltaFrame

合法delta必须满足：

- base_projection_revision等于current；
- resulting_projection_revision等于base + 1；
- resulting_authority_high_water等于resulting_active_head.through_authority_sequence；
- resulting confirmed root等于current confirmed root；
-每个upsert/remove expected previous fingerprint与current map exact join；
- ordered changes应用后entry count/accumulator与resulting active head一致；
- duplicate revision只在frame fingerprint相同才no-op。

该branch只表达Protocol schema中实际存在的presentation/history changes。Queue、pending interaction、run-control、session lifecycle和notification的变化不得因“恰好伴随某个history delta”被Go顺带推断。

### TUI-BT-CONSUME-003A Control observation branch

1. request/attachment必须exact current。
2. `validated_after_control_cursor_fingerprint`必须等于Observe effect冻结值，并等于local `Fresh.ConfirmedCursor` fingerprint；`SnapshotRequired.ObservedLatestCursor`不能用作新Observe base。
3. generation、base revision、base projection fingerprint、transition prefix accumulator与registry contract必须逐字段等于local confirmed cursor；result revision必须严格前进。
4. changed section必须是Protocol closed enum且至少一项；transition count必须等于`result-base`，resulting cursor的revision/projection identity必须等于frame result。Record、prefix/range accumulator属于opaque Python authority：Go只验证fingerprint syntax、non-empty、nested exact join和cross-language fixture，不重算changed-section union或hash recurrence；disposition必须为`SNAPSHOT_REQUIRED`。
5. exact duplicate resulting cursor + frame fingerprint为no-op；same cursor different fingerprint进入fatal compatibility。
6.一次性构造`ControlProjectionSnapshotRequired{StaleView, ConfirmedCursor, ObservedLatestCursor=resulting cursor}`，并disable所有依赖queue/interaction/run state的mutation；禁止修改stale view或confirmed cursor。
7. coalesce已有pending snapshot request；不得每个changed frame再创建一个request。
8.发携`MinimumObservedControlCursor=resulting cursor`的`RequestSnapshotEffect`；durable history与operational viewport继续显示，ordinary draft保留；该effect terminal前不发新Observe。
9. matching snapshot必须回显minimum cursor fingerprint，且snapshot control cursor与minimum同generation、revision不小于minimum；原子替换全部control view/cursor并构造Fresh后才能重新enable相应action。`SnapshotControlRebaseRequiredMsg`必须匹配request/minimum且latest cursor为new generation；Update保留stale view/confirmed cursor、替换observed target并在同一absolute deadline/最多4轮bound内发fresh minimum-bound snapshot。不降级接受旧generation snapshot，不无界自旋。

Nested `ControlProjectionGapFrame`不尝试从latest cursor重建view，但会把它作为`ObservedLatestCursor`安装到同一SnapshotRequired union，保留旧view + confirmed cursor并记录closed reason，coalesce一次minimum-bound snapshot request；generation change、cursor too old、transition discontinuity和registry contract change都走该branch。若latest cursor与confirmed cursor不在同generation，minimum request必须使用typed generation-rebase branch而不做ordinal比较。Control GAP不清除history/operational screen、不伪造history GAP，也不从latest cursor重建queue。

Queue-only transition必须通过该frame唤醒client。Go不得把history delta/high-water、command receipt或timer当作control变化信号。

应用算法：

~~~text
clone current immutable resident vector index
-> ordered apply each HistoryEntryChange
-> verify resulting vector
-> atomically replace revision, high-water, active-head, vector and indexes
-> preserve follow-tail or increment unseen
~~~

`ProjectionDeltaFrame`不携带top-level pending interaction、queue、lifecycle或run-ID replacement；这些变化只由TUI-BT-CONSUME-003A的explicit signal驱动snapshot。禁止保留旧的“accepted history delta后顺便snapshot”路径。

### TUI-BT-CONSUME-004 AuthorityAdvanceFrame

AuthorityAdvance只在以下条件安装：

- frame.projection_revision等于current revision；
- base_active_head_fingerprint等于current active head；
- resulting active head仍绑定same confirmed root；
- authority high-water不回退；
- frame fingerprint合法。

它只替换active head和high-water，不改变resident entries、latest cursor pair或control projection。若confirmed root变化，立即进入`LocalObservationOverflowMsg`所驱动的durable rebuild，不尝试猜root rollover。

### TUI-BT-CONSUME-005 PresentationHistoryRootAdvancedFrame

Root advance是一个不可拆分transition：

1. base_projection_revision等于current。
2. resulting_projection_revision等于base + 1。
3. previous_active_head_fingerprint等于current。
4. latest_root_cursor_pair绑定resulting_active_head.confirmed_root_identity。
5. previous_root_relation.previous_root_identity绑定current root。
6. relation.resulting_root_identity绑定resulting root。
7. consumed prefix、retained suffix、checkpoint confirmation和frame fingerprint通过Protocol validator。
8. current latest pair降为root-fingerprint隔离的pinned state。
9.根据resident_transition closed branch执行：

| branch | client action |
|---|---|
| unchanged | 保留stable entry/content vector，切换client-local root/rank basis；不得伪造新的wire ranked fingerprint |
| bounded_changes | ordered applyupsert/remove并验证before/after vector fingerprint、count、bytes和accumulator |
| rebase_required | 保留最后confirmed screen为stale，清除受影响resident/page cache，发RequestSnapshotEffect |

10. resulting latest pair成为唯一latest；pinned old pair不能参与follow-tail。
11. resulting active head保留frame声明的concurrent suffix；不能看到new root后清空tail。
12.整个transition一次性commit到AppState；任一join失败均转该plane的typed rebuild；若错误来自client bridge连续性，则生成`LocalObservationOverflowMsg`。

Go不解释checkpoint、tree或prefix为何成立，只校验Protocol要求的wire joins。

### TUI-BT-CONSUME-006 OperationalSnapshotFrame

Operational snapshot：

- `RequestOperationalSnapshotEffect.Request`必须是Update已经冻结的完整Protocol request；request ID、runtime session、attachment identity、current binding和requested-after generation/cursor逐项匹配`SnapshotAwaitingOperationalSnapshot`，executor不得补字段；
- frame request ID、runtime session、attachment identity与acknowledged binding匹配，并与loading state中已经确认的durable attachment/binding相等；
- generation/cursor、count、encoded bytes、accumulator、opaque state/contract fingerprint与recomputed outer frame fingerprint全部合法；
- ordered_activity_cells最多256项且encoded payload不超过negotiated 1 MiB，按server canonical order deep-copy；Go不得截断、局部merge或按arrival time重排；
- identity key固定为owner_kind、owner_id、owner_generation、coalesce_key；
- snapshot内identity key必须唯一；相同key重复（即使fingerprint相同）也拒绝整帧，不能“只保留最终值”掩盖server contract violation；
- matching frame原子替换全部operational state，并把loading substate从AwaitingOperational切到BaselinesInstalled；
-不得改变durable revision、history root、queue或command。

Operational retry只更换exact operation/request identity，requested-after cursor仍来自当前last confirmed operational baseline；它不能清空已经安装的durable/control baseline。Duplicate frame只有request与frame fingerprint都identical才完成stale operation no-op；same request different frame fatal。Frame被拒绝时保持AwaitingOperational和durable baseline，按typed operational rebuild重试；不得退回AwaitingDurable，除非另有durable GAP/attachment generation change使整个bootstrap generation失效。

### TUI-BT-CONSUME-007 OperationalDeltaFrame

Delta只在same operational_generation且第一项cursor是current + 1时应用；ordered changes必须连续，最后一项cursor等于frame.operational_cursor。

Upsert按identity key替换。Remove必须exact匹配expected_activity_fingerprint。Generation变化、cursor gap或identity conflict只使operational state invalid并请求OperationalSnapshot；不使durable history失效。

Operational overflow、coalesce或drop不增加durable unseen count。

### TUI-BT-CONSUME-008 ObservationGap

Server GAP或local bridge GAP执行同一算法：

1.停止发ObserveNext。
2. durable state标记stale/read-only，但保留最后confirmed screen。
3. operational state清空并标记invalid。
4.清除secret plaintext和lease-local state。
5.取消所有page请求并隔离pinned cache。
6. pending command转QueryRequired。
7.发RequestSnapshotEffect与RequestOperationalSnapshotEffect。
8. durable与operational snapshot各自安装完成，且Projection snapshot已安装完整control cursor后，使用新durable/operational/control cursor重新ObserveNext。

Durable/operational GAP中的latest hints只用于diagnostic，不直接推进local confirmed cursor。Control GAP的latest cursor例外只能进入`SnapshotRequired.ObservedLatestCursor`并成为snapshot minimum，仍不是confirmed cursor或view authority。

### TUI-BT-CONSUME-009 ObservationNoChange

NoChange只在request ID及echoed durable/operational/control cursors逐项等于matching Observe effect时完成operation，不修改任何state。任一echo漂移是protocol conflict，不能当作NoChange。Scheduler随后按heartbeat priority决定发送Heartbeat或下一Observe。

### TUI-BT-CONSUME-010 HistoryPageResponse

每个page request在OutstandingPage中冻结：

~~~text
request_id
root_identity_fingerprint
input_cursor_fingerprint
direction
limits
viewport_intent_generation
~~~

四branch算法：

#### PAGE

- validated_input_cursor_fingerprint等于outstanding input；
- validated_request_direction等于outstanding direction；
- validated_root_identity等于outstanding root；
- entries按server顺序使用，不重排；
- overlap entry只有exact same fingerprint可dedupe；
- before/after cursor只写入该root的pinned page state；
- empty page只有matching has_more=false才表示该方向结束；
- response到达时viewport intent已变，只填cache，不强制移动viewport。

#### CURSOR_STALE

- replacement_cursor和replacement proof必须同时有或同时无；
-两者都有：以same user direction建立新request；
-两者都无：清除该root cache并请求snapshot；
-绝不把stale显示为history end。

#### REBASE_REQUIRED

-保留当前visible confirmed cells并标记stale；
-清除受影响root cache；
-发正式ProjectionSnapshotRequest；
- bounded_snapshot_or_rebase_token在当前schema没有request echo carrier，Go不得把它塞进自由metadata；只作为non-logged opaque response attribution随该attempt销毁。若未来要求echo，必须先由Protocol增加typed request field。

#### RECONCILIATION_REQUIRED

-保留confirmed screen；
- page state进入ReadOnlyReconciliation；
-只按retry_after_ms安排bounded retry或等待新snapshot；
- trusted root hint不能直接替换current root；
-不显示“没有更多历史”。

## 6. Client state machines

### TUI-BT-STATE-001 Transcript viewport

Viewport state：

~~~text
FollowTail
  -> Scrolled
  -> Selecting
  -> Searching

Scrolled | Selecting | Searching
  -> FollowTail       # explicit End/jump
~~~

规则：

-新entry在FollowTail时保持tail anchor；
-非FollowTail时只增加unseen count；
-jump-to-end使用latest root pair，不使用pinned old root；
- page接近boundary才发ReadHistoryPageEffect；
- resize只invalidatewrap cache；
- stable selection key是history_entry_id + placement_key_fingerprint + cell semantic revision；
-root-local display rank只用于当前view定位；
-old-root page可以继续浏览，但new latest root到达后不会覆盖它；
-copy只使用PresentationContentBlock public content；
-secret、private URL、redacted marker不进入copy-all。

### TUI-BT-STATE-002 Composer

Composer closed state：

~~~text
Empty
  -> Editing

Editing
  -> PasteReview
  -> SubmitFrozen
  -> Empty

PasteReview
  -> Editing
  -> SubmitFrozen

SubmitFrozen
  -> AwaitingReceipt
  -> Editing             # local user starts next draft

AwaitingReceipt
  -> Accepted
  -> RejectedEditable
  -> QueryRequired
  -> Reconciliation
~~~

Submit冻结：

- exact UTF-8 bytes；
- local draft revision；
- stable client_submission_id；
- stable command_id；
- requested delivery mode；
- current attachment/controller binding；
- request semantic fingerprint由Protocol helper生成。

收到accepted terminal outcome时，只在current draft revision仍等于frozen revision时清空；用户已继续编辑则不得擦除新draft。Rejected保留frozen内容供编辑。Disconnect不生成新ID。

小paste进入textarea。大paste进入PasteReview，只保存bounded preview、byte/line count和local ephemeral payload owner。当前MutationCommand没有artifact preparation branch；超过negotiated frame cap的paste必须禁用提交并显示typed unsupported，直到Protocol owner增加artifact preparation command。Go不得绕过frame cap或写临时server artifact。

### TUI-BT-STATE-003 Command receipt

每个command ID只有一个state：

~~~text
Frozen
  -> Sending
  -> AwaitingOutcome

Sending | AwaitingOutcome
  -> QueryRequired       # disconnect/response loss
  -> Accepted
  -> Rejected
  -> PendingConfirmation
  -> Reconciliation
  -> CompatibleWinner

PendingConfirmation
  -> Querying

QueryRequired
  -> Querying

Querying
  -> Accepted
  -> Rejected
  -> PendingConfirmation
  -> Reconciliation
  -> CompatibleWinner
  -> ResendSameCandidate # found=false
~~~

Go只识别Protocol规定的CommandOutcome.status。socket success、HTTP-like write success或snapshot变化都不是receipt。

- SUCCEEDED -> Accepted；
- REJECTED -> Rejected；
- PENDING_CONFIRMATION -> PendingConfirmation并query；
- RECONCILIATION_REQUIRED -> Reconciliation；
- SUPERSEDED_BY_COMPATIBLE_WINNER -> CompatibleWinner。

found=false只允许重发同command ID、same frozen payload和same fingerprint；不得创建new candidate。Terminal state仍保留bounded public result和durable references用于显示，不据此重建domain fact。

### TUI-BT-STATE-004 Interaction

Interaction closed branches只来自PendingInteraction oneof：

- ApprovalInteraction；
- PlanQuestionInteraction；
- PlanExitInteraction；
- McpInteraction。

Client identity使用interaction_id + view_fingerprint。Snapshot replacement或clear使旧view所有action立即disabled。每次resolution先冻结matching view fingerprint和current controller binding；late receipt只更新matching pending command。

Approval只提交server列出的tool_call_id。Plan option只提交server列出的option或allow_free_text允许的text。Go不解析prompt来发明action。

当前McpInputPublicRequest只给出request_key、mode、public_prompt和schema_or_url_present；它没有form field schema。S4的typed MCP form renderer必须等待Protocol owner增加event-safe closed form schema carrier。S4不得以自由JSON textarea冒充typed form。

### TUI-BT-STATE-005 Secret

Secret state与ordinary composer物理分离。Plaintext唯一长期process-local owner是AppState外的`SecretRuntimeOwner`，而不是`secret.State`或Bubble Tea message：

~~~text
NoSecret
  -> URLRevealPending
  -> FormEditing

URLRevealPending
  -> URLRevealed
  -> Revoked

URLRevealed
  -> OpenPending
  -> Revoked

FormEditing
  -> FormSealPending
  -> Revoked

FormSealPending
  -> SealedHandleReady
  -> Revoked

SealedHandleReady
  -> ResolutionPending
  -> Revoked

any
  -> Revoked
  -> NoSecret
~~~

`secret.State`只保存opaque handle、lease identity/generation/expiry、request key、masked length、validation summary和上述phase。`SecretRuntimeOwner`保存两种revocable mutable cell：server-returned private URL bytes与locally typed form response buffer。它由`client.Service`持有并参加single teardown drain，不参与Model equality、View dump或snapshot。

Secret buffer：

-使用mutable byte/rune buffer；
-禁止String/GoString输出plaintext；
-禁止history、undo、autosuggest、completion、snapshot；
-禁止ordinary queue、notification和diagnostic；
-detach、takeover、GAP、interaction replacement、expiry、Host close时best-effort overwrite；
-不能承诺撤销已显示、copy或terminal scrollback中的plaintext。

Private URL只在SecretRevealResult exact join current interaction/request/attachment/controller后安装为opaque handle并进入URLRevealed。Open/copy effect只携带handle；executor临时borrow bytes、完成physical operation后release borrow，不prefetch。SecretLeaseRevoked按lease fingerprint先使owner cell future borrow fail closed，再向Update发送不含plaintext的`SecretRevokedMsg`。

Form keystroke/paste作为Bubble Tea framework input不可避免地短暂存在于framework message和normalizer stack，但不得进入application message/effect或累计进AppState。Active secret form下，normalizer将一个最大256 UTF-8 bytes的normalized edit安装为one-shot `EditHandleID`，best-effort清除临时buffer，并发出`SecretEditReadyMsg`。Pure Update只产生`ApplySecretEditEffect(target handle, edit handle)`；executor原子consume edit handle并修改form buffer，随后返回`SecretBufferChangedMsg(handle, masked length, validation summary)`。Edit handle有5秒TTL、只能消费一次，duplicate/expired/revoked均fail closed且不含原字符。Submit/open/private-copy effect同样只携带handle，不能携带assembled plaintext。SecretRuntimeOwner API冻结为：

~~~go
type RuntimeOwner interface {
    InstallPrivateURL(lease LeaseIdentity, plaintext []byte) (HandleID, error)
    CreateFormBuffer(lease LeaseIdentity, schema FormSchema) (HandleID, error)
    InstallEdit(target HandleID, edit EditOperation) (EditHandleID, error)
    ApplyEdit(target HandleID, edit EditHandleID) (MaskedBufferState, error)
    Borrow(handle HandleID, purpose BorrowPurpose) (RevocableBorrow, error)
    Revoke(handle HandleID, reason RevokeReason)
    RevokeAll(reason RevokeReason)
    Drain(deadline time.Time) error
}
~~~

所有value/edit handle的one-shot/lease validation在owner内执行；App Update只检查handle/lease attribution，不实现secret validity。Architecture gate允许secret plaintext仅在framework raw carrier、normalizer stack、`InstallEdit`参数与owner mutable cells中bounded transient存在；禁止`KeyInputMsg`、`PasteInputMsg`、AppState、application effect、debug dump、history、notification和log携带secret text。这取代不可执行的“secret package外绝不出现任何plaintext byte”绝对表述。

Secret package返回的error/panic boundary只能使用closed constant code，禁止把plaintext、schema value、URL、form response或`[]byte`格式化进error。Production依赖Bubble Tea default panic logger，因此tests必须在安装known canary secret后注入non-secret panic并证明stderr/exit summary不含canary；secret owner自身不得以plaintext构造panic value。

Form response先通过SecretFormSubmit获得sealed_response_handle_id，再用ResolveMcpInteractionCommand提交handle；Go不持久化plaintext或handle到ordinary history。

### TUI-BT-STATE-006 Queue

Queue projection完全由snapshot安装；`ControlProjectionChangedFrame(PROMPT_QUEUE)`只使现有queue view stale并触发coalesced snapshot，不携带或推导queue item。Snapshot只允许Foundation reducer派生的0..64项active client set；terminal historical rows不进入Go state，`RECONCILIATION_REQUIRED` item只读显示。`NegotiatedLimits.MaximumActiveQueueItems`必须等于64，count/accumulator/order/fingerprint任一不符则拒绝整份snapshot，Go不得截断。S1已经安装同一`QueueReadOnlyProjected` value；S5只开启mutation state machine。Local state：

~~~text
ServerItems
  + PendingSubmitCommands
  + PendingCancelCommands
  + ReplacementWorkflow
~~~

Queue item不可由draft或send success创建。

Replacement workflow：

~~~text
Idle
  -> CancelFrozen
  -> CancelAwaitingReceipt
  -> CancelAccepted
  -> ReplacementSubmitFrozen
  -> ReplacementAwaitingReceipt
  -> Done | ReplacementFailed
~~~

只有cancel terminal accepted后才能产生new submission ID和new command ID。Replacement失败时旧item保持cancelled，draft保留。Reconnect分别query两个command，不能复活旧item。

Explicit STEER被server拒绝时不得自动变FOLLOW_UP。AUTO、STEER、FOLLOW_UP只来自SubmitPromptCommand enum。

V1固定使用Protocol-owned `ControlProjectionChangedFrame + coalesced snapshot`，不再把“任意history projection advance后也许请求snapshot”作为策略。性能gate必须证明queue-only burst能coalesce且不会形成unbounded snapshot loop。未来完整typed queue delta是独立Protocol evolution；Go不得从history cell或command public text重建QueueItemView。

### TUI-BT-STATE-007 Capacity

Go只把PresentationHistoryCapacityState映射到：

| server branch | client state |
|---|---|
| available | submit enabled |
| session_rotation_required | ordinary submit disabled；显示StartSuccessorSession action |
| tree_capacity_exhausted | read-only；显示repair/new-session action |
| reconciliation_required | read-only；显示stable fault/retry |

Client不调整requested quote、不丢history entry、不通过缩短text重试同command。StartSuccessorSessionCommand必须引用收到的source capacity fingerprint。

## 7. Key routing 与 render mechanics

### TUI-BT-KEY-001 Routing hierarchy

按顺序只有第一个active layer消费按键：

~~~text
secret
-> typed interaction
-> modal/command palette
-> selection/search
-> composer
-> global app
~~~

Esc/Ctrl-C不由component直接tea.Quit。

- secret层：取消local edit或发送typed cancel；
- interaction层：执行server允许的cancel；
- modal层：关闭modal；
- selection/search：退出模式；
- composer：清空当前操作或保留draft；
- global active run：冻结StopRun command；
- global idle：显示detach/quit intent。

### TUI-BT-KEY-002 Input mechanics

- Enter和newline行为只使用Bubble Tea capability message与明确fallback；
- wide rune/grapheme移动沿用S0 verified seam；
- Bubbles无native undo时使用bounded client-owned undo，仅ordinary composer启用；
- paste boundary是closed client message；
- active run期间composer保持editable；
- selection/search不改变server projection。

### TUI-BT-RENDER-001 Cell render

Renderer只消费DurableHistoryCell/OperationalActivityCell generated branches的validated client value。Unknown required oneof进入protocol incompatible；不提供generic “unknown event” renderer。

允许的client-local mechanics：

- width-dependent wrap；
- compact/verbose；
- expand/collapse；
- color/style/border；
- public text selection；
- stable group collapse preference；
- operational animation。

不得改变server返回顺序、stable IDs、visibility policy或must-show内容。Run lifecycle只按AuditCell渲染；Go不定义RunLifecycleCell。

### TUI-BT-RENDER-002 Cache

Wrap cache key至少包含：

~~~text
history_entry_id
entry_fingerprint
placement_key_fingerprint
cell semantic revision
rank basis fingerprint
active head fingerprint
available width
density
theme typography contract
expanded preference
~~~

Resize只清除width相关cache。Root advance只清除受transition影响的cache。Operational cache和durable cache物理分离。

## 8. Parent/child supervision、signal、socket 与 teardown

### TUI-BT-LIFE-001 Parent/child ownership

Python parent：

-构造Host、Gateway和安全runtime directory；
-创建Unix socket与launch capability；
-选择并校验matching Go binary；
-创建bootstrap pipe；
-spawn、wait、classify child；
-unexpected exit时best-effort emergency terminal restore；
-决定Host继续、detach或close。

Go child：

-读取一次bootstrap carrier，把launch credential安装进`ClientRuntimeOwner`的revocable cell，随后清除carrier/decode副本；
-关闭bootstrap FD；matching Attach ACK FULL前不得清除owner中的launch credential；
-连接Unix socket；
-拥有TTY和Bubble Tea program；
-通过typed command请求stop/detach/close；
-退出前运行single teardown。

Launch capability不得出现在argv、environment value、process title、log或crash report。Environment只可携带非秘密十进制FD number。Child严格按TUI-PROTO-HELLO-004读取`TerminalClientBootstrapCarrier`：16 KiB cap、2秒deadline、single payload + immediate EOF、trailing byte拒绝；读取/校验后立即关闭FD。Go只实现generated decoder和process-local bootstrap handle，不复制carrier字段/指纹语义。

### TUI-BT-LIFE-002 Foreground process与signals

POSIX启动：

1. Python创建child process group。
2. parent将foreground TTY process group转给Go child。
3. terminal SIGINT只到foreground child group。
4. Go把Ctrl-C/Esc解释为key routing，不直接signal Python。
5. Python SIGTERM/shutdown通过supervision path通知child并给bounded grace。
6. child退出后parent恢复foreground process group和termios。

Go收到：

- SIGTERM/SIGHUP：进入BeginTeardownEffect；
- SIGINT：只有非raw/无法解释场景才转teardown，ordinary Ctrl-C由key routing；
- SIGWINCH：交给Bubble Tea resize；
- SIGKILL：无defer承诺，由parent emergency restore。

Go不得向Python发送SIGINT来实现StopRun。

Bubble Tea v2.0.6默认会注册SIGINT/SIGTERM handler并在application `Update`前消费成`InterruptMsg/QuitMsg`；官方[`WithoutSignalHandler`](https://github.com/charmbracelet/bubbletea/blob/v2.0.6/options.go)正是外部接管开关。Production必须显式关闭这位framework signal owner。唯一Program构造冻结为：

~~~go
func NewProductionProgram(
    model tea.Model,
    programContext context.Context,
    sanitizedEnvironment []string,
) *tea.Program {
    return tea.NewProgram(
        model,
        tea.WithContext(programContext),
        tea.WithInput(os.Stdin),
        tea.WithOutput(os.Stdout),
        tea.WithEnvironment(sanitizedEnvironment),
        tea.WithoutSignalHandler(),
        tea.WithFPS(60),
    )
}
~~~

不得增加`tea.WithoutSignals()`、`tea.WithFilter()`、`tea.WithoutRenderer()`或`tea.WithoutCatchPanics()`到production；tests可在fixture中使用WithInput/Output/WindowSize替代值，但仍必须有一组exact production-options contract test。

`internal/supervision/signal_unix.go`是SIGTERM/SIGHUP/SIGINT的唯一OS signal owner：

- 使用buffered signal channel + `signal.NotifyContext`或等价bounded owner；
- SIGTERM/SIGHUP生成closed `ParentShutdownMsg`，Update安装teardown state后才产生`BeginTeardownEffect`；
- raw TTY下Ctrl-C走KeyPress normalizer；仅无法成为key的process SIGINT进入同一teardown消息；
- SIGWINCH仍由Bubble Tea自身resize path拥有，不在supervision重复注册；
- teardown完成后stop signal notification并drain owner，禁止late signal重启effect。

Panic ownership与ordinary signal teardown不同：保留framework默认panic recovery，使其恢复raw mode/alternate screen并让Run返回error；application不承诺panic后发送Detach。Parent观察abnormal exit后执行emergency foreground/termios restore、关闭connection并让server attachment失效。禁止component-level recover、二次panic logger或把panic value写入protocol/exit summary。

### TUI-BT-LIFE-003 Socket

-只连接Protocol提供的POSIX Unix socket；
-拒绝symlink、非socket、foreign owner和unsafe parent directory；
-不搜索TCP fallback；
-不通过stdout/stderr传Protobuf；
-socket read/write只有client.Service scheduler拥有；
-framing cap使用Hello negotiated limit与client compiled hard cap的较小值；
-EOF使attachment invalid并进入reconnect/teardown；
-teardown后禁止goroutine重新dial。

### TUI-BT-LIFE-004 Teardown

唯一teardown顺序：

~~~text
stop accepting new local effects
-> cancel observe/page/timer operations
-> clear secret state
-> if legal, send stable DetachSessionCommand within remaining deadline
-> close socket
-> wait scheduler/bridge physical exit
-> stop child-owned background goroutines
-> return Bubble Tea program
-> restore terminal/foreground process group
-> emit bounded exit summary
~~~

Caller cancellation不能跳过physical drain。Deadline耗尽时child退出并由parent继续emergency restore；不得卡住Python Host close。

Bounded exit summary只含public session ID、last connection status和reconnect hint，不回灌transcript、protocol diagnostic或secret。

### TUI-BT-LIFE-005 Server closing dependency

当前ServerFrame没有typed server-closing branch。S6的graceful Python shutdown UX必须先由Protocol owner增加closed frame；在此之前EOF只能表现为ConnectionLost，Go不得从EOF原因猜Host close。

## 9. Build、packaging 与 activation

### TUI-BT-DIST-001 Dependency pin

Production go.mod固定direct dependencies：

- charm.land/bubbletea/v2 v2.0.6；
- charm.land/bubbles/v2 v2.1.0；
- charm.land/lipgloss/v2 v2.0.5；
- google.golang.org/protobuf v1.36.11。

Renderer-critical transitive dependency额外固定为：

- github.com/charmbracelet/ultraviolet v0.0.0-20260416155717-489999b90468。

该版本与Bubble Tea v2.0.6官方module graph及S0验证一致。禁止使用未随当前Bubble Tea正式版发布的更高Ultraviolet pseudo-version；已验证`20260703-f5a850f9c2b7`在macOS Terminal.app的CJK transcript上经过窄→宽resize后会造成cursor/line diff错位。go.sum必须完整提交。禁止replace到local path或unpinned pseudo fork。升级依赖需要独立PR和S0/PTY/view golden复跑。

Root mise配置在S1固定Go 1.26.5和protoc 34.0。CI不得隐式下载不同Go toolchain。

### TUI-BT-DIST-002 Protocol generation

唯一source：

src/pulsara_agent/terminal_protocol/schema/terminal_client.proto

唯一Go outputs：

```text
clients/terminal/internal/protocol/terminal_client.pb.go
clients/terminal/internal/protocol/fingerprint_contract_gen.go
clients/terminal/internal/protocolvalue/vocabulary_gen.go
clients/terminal/internal/protocolvalue/carriers_gen.go
```

scripts/generate_protocol.sh固定：

- protoc 34.0；
- protoc-gen-go v1.36.11；
- exact Go package mapping；
- deterministic generation；
- 从同一`.proto`与fingerprint/field manifest生成non-Protobuf immutable protocolvalue vocabulary/carrier；
- gofmt；
-生成后git diff clean gate。

禁止手写第二套wire DTO、复制.proto到Go目录或编辑generated file。

### TUI-BT-DIST-003 Reproducible build

build.sh使用：

~~~text
CGO_ENABLED=0
go build -trimpath
-buildvcs=true
-ldflags:
  -s -w
  -X buildinfo.Version
  -X buildinfo.Commit
  -X buildinfo.ProtocolMajor
  -X buildinfo.ProtocolMinor
  -X buildinfo.SchemaFingerprint
  -X buildinfo.DependencyLockFingerprint
~~~

SOURCE_DATE_EPOCH来自tag commit timestamp。每个target在clean runner构建两次，SHA-256必须一致；若Go build ID导致差异，必须先冻结可复现参数，不能忽略。

Binary提供：

- --version；
- --version-json；
- --self-test；
- --print-protocol-range。

这些命令不连接socket、不进入alternate screen。

### TUI-BT-DIST-004 Four-platform matrix

V1只有：

| GOOS | GOARCH | archive |
|---|---|---|
| darwin | arm64 | pulsara-tui_VERSION_darwin_arm64.tar.gz |
| darwin | amd64 | pulsara-tui_VERSION_darwin_amd64.tar.gz |
| linux | amd64 | pulsara-tui_VERSION_linux_amd64.tar.gz |
| linux | arm64 | pulsara-tui_VERSION_linux_arm64.tar.gz |

每个archive只含：

- pulsara-tui；
- LICENSES.txt；
- build-manifest.json。

Windows不发布。Cross-build成功不等于release；四个平台都必须在native或official emulated clean runner执行--self-test和version compatibility smoke。

### TUI-BT-DIST-005 Checksums与manifest

Release生成：

- SHA256SUMS；
- SHA256SUMS.sigstore.json；
-每archive内build-manifest.json；
-SBOM SPDX JSON；
-Go module license inventory。

签名方案固定为GitHub Actions OIDC上的Sigstore keyless cosign blob bundle。Release gate执行cosign verify-blob --bundle SHA256SUMS.sigstore.json SHA256SUMS；不维护repository长期私钥，也不把未验证的裸.sig文件称为签名证据。

Manifest至少包含version、commit、dirty=false、Go version、GOOS、GOARCH、protocol range、schema fingerprint、go.sum fingerprint、binary SHA-256、build timestamp/source epoch。

Python launcher验证embedded manifest与binary SHA，且不得只依赖filename。实际协议兼容仍以Hello为最终裁决。

### TUI-BT-DIST-006 Python wheel carrier

V1 canonical carrier冻结为matching platform-specific Python wheel，不在首次运行联网下载。

每个wheel只包含一个matching binary。Binary使用wheel标准scripts carrier，避免把普通package data的可执行位当成跨installer保证：

~~~text
DIST-VERSION.data/scripts/pulsara-tui
pulsara_agent/_terminal/build-manifest.json
~~~

Wheel targets：

- macosx_11_0_arm64；
- macosx_11_0_x86_64；
- manylinux_2_28_x86_64；
- manylinux_2_28_aarch64。

Universal py3-none-any wheel不得宣称包含TUI。Platform wheel必须标记Root-Is-Purelib=false并使用py3-none-PLATFORM tag。Release pipeline先构建并验证Go artifact，再由Hatch build hook注入matching binary。Wheel integration test安装到clean venv，核对scripts目录中的exact binary、executable mode、manifest、SHA、--self-test和Hello compatibility。

Python resolver使用当前interpreter的sysconfig scripts path解析exact sibling binary，不搜索PATH；manifest是package resource，必须声明同一个artifact SHA和platform。Scripts path中不存在或SHA不匹配时typed fail closed。

Development override只允许显式PULSARA_TUI_BIN。Resolver不搜索PATH；override文件必须regular、owned、executable且非group/world writable，并仍通过version-json与Hello。

### TUI-BT-DIST-007 Compatibility

三层检查：

1. launcher检查binary manifest可读、平台匹配、SHA正确；
2. client/server通过Hello协商Protocol range、schema fingerprint和required capabilities；
3. attachment后只消费selected minor允许的fields/branches。

Python package version与Go version在正式wheel中必须相等，但版本相等不替代protocol handshake。兼容的不同build可连接；major/schema不兼容fail closed。

### TUI-BT-DIST-008 Default entry switch

S1–S5：

-显式入口 pulsara host tui；
- pulsara host repl保持Legacy；
- pulsara host无subcommand保持当前行为；
-缺binary或不兼容时typed error，不fallback。

S6 gate通过后：

-交互TTY下 pulsara host 默认进入TUI；
- pulsara host tui仍为显式等价入口；
- pulsara host repl仍为显式Frozen Legacy REPL；
-非TTY不自动启动full-screen，提示host run/repl；
-任何binary/protocol失败都不silent fallback。

默认切换必须是单独可回滚release flag；回滚只改变入口选择，不改变Foundation、Protocol或durable authority。

## 10. S1：Connection、snapshot 与真实只读 viewport

### TUI-BT-S1-000 不可跳过的Protocol Go-ready gate

S1开工前先以独立全绿Protocol 2.0 subcut完成（old behavioral string field number永久reserved，不保留dual field）：

1. `codec.py PROTOCOL_MAJOR: 1 -> 2`、minor固定0、Protobuf package `pulsara.terminal.v1 -> v2`、Go package option、new schema fingerprint及major-1 typed rejection；
2. TUI-PROTO-HELLO-003 capability enum、client required set、server selected set与Gateway admission；
3. TUI-PROTO-HELLO-004 `TerminalClientBootstrapCarrier`、16 KiB one-shot framing与Python launcher encoder；
4. TUI-PROTO-TRANSPORT-002 auth preface/result、attachment-attempt generation、stable semantic winner/per-connection receipt、pre/post-ACK typed rebind、initial launch compatible-winner owner与bounded ACK tombstone；
5. TUI-PROTO-SCHEMA-003 fingerprint manifest/generator/goldens（含operation/request ID namespace）；
6. TUI-PROTO-SCHEMA-004全部behavioral string迁enum及unknown disposition；
7. TUI-PROTO-OBS-002A atomic control view/cursor、multi-plane batch、control change/GAP与bounded opaque transition store owner；
8. Foundation/Protocol active queue projection hard max 64、typed empty genesis head、admission bound与snapshot mapper；
9. Protocol exact controller/bootstrap enum、完整Attach ACK lowering carrier、Heartbeat request/result与OperationalSnapshot request/frame生成；
10. S1 required capability matrix的Python/headless conformance；`HISTORY_PAGE_V1`、control observation与reconnect rotation不得被S1广告。

该subcut修改Protocol schema/Python boundary但不启用Go入口；必须独立部署、全量Python测试全绿后，S1才可生成Go binding。禁止先在Go手写临时capability strings、bootstrap struct或canonical helper。

### TUI-BT-S1-001 目标

交付一个只读、真实连接Python Gateway的Go client：

-首帧；
-transport auth preface、Hello/Attach/Attach ACK observer；
-typed Heartbeat request/receipt与client-local schedule；
-ProjectionSnapshot和OperationalSnapshot；
-占满真实Terminal WindowSize的alternate-screen shell，固定header、resident transcript viewport和单行read-only footer；
-resize、scroll、copy；
-local quit和parent supervision；
-显式 pulsara host tui。

不消费live delta，不发送domain mutation。

### TUI-BT-S1-001A Full-height layout 与 viewport hard cut

`clients/terminal/internal/app/layout.go`是S1几何的唯一owner。`NewLayoutPlan(width, height)`必须先验证positive dimensions和`width × height <= 256 Ki visual cells`，再产生不可变`LayoutPlan`：

| Terminal height | Header | Transcript | Footer | closed mode |
|---:|---:|---:|---:|---|
| `1` | 1 | 0 | 0 | `phase · q`单行降级 |
| `2` | 1 | 0 | 1 | compact status/failure + footer |
| `>=3` | 1 | `height-2` | 1 | full-height read-only shell |

每次`View`必须恰好产生`height`个visual rows。每行经ANSI-aware display-width truncation后补齐到exact width；header/footer不得自动wrap。Footer只按width执行closed降级：宽屏显示`observer · wheel/↑/↓ scroll · y copy · q quit`，中等宽度显示read-only紧凑提示，极窄显示`↑↓·y·q`。异常或超界WindowSize进入保留最后一个validated plan的bounded fatal view，禁止用隐藏的`240×100`或其他fallback尺寸继续渲染。

`presentation.State`只拥有validated `DurableSnapshot`，不得保存width、height、scroll offset、follow-tail或wrap结果。`components/transcript.Model`唯一拥有：

~~~go
type Model struct {
    cache               WrapCache
    width               int
    height              int
    scrollOffset        int
    followTail          bool
    unseenTerminalCells uint32
    installed           bool
    anchor              viewportAnchor
    hasAnchor           bool
}
~~~

`WrapCache`按`snapshot fingerprint + body width`构造，每行保存stable cell ID、label/body discriminator和正文UTF-8 source offset；height-only resize不重建。Scrolled resize以当前top visual row的cell/source offset重定位，follow-tail resize保持tail。S1尚无live delta，因此`unseenTerminalCells`固定为0，但字段、validator和owner本阶段即冻结，S2只能推进既有owner。

输入算法固定为：Up/Down移动1 visual row，vertical wheel移动3 visual rows，PageUp/PageDown移动`max(viewportRows-1, 1)`，End清零offset并恢复follow-tail。offset归零必须等价于follow-tail；View只读取Update已经准备好的rows，不执行wrap state transition、I/O或snapshot reconstruction。

Production `tea.View`始终声明`AltScreen=true`和`MouseModeCellMotion`，统一teardown负责退出alternate screen并关闭mouse reporting。普通启动绝不发送`CSI 3J`，所以Terminal.app等emulator的native scrollbar仍可能访问启动前primary scrollback；这不属于Go renderer可逆控制。只有Python launcher拥有`--clear-scrollback`：默认关闭，显式开启后在首次child之前精确写一次`CSI H + CSI 2J + CSI 3J`，parent relaunch不重复，非TTY、partial write或flush失败typed fail closed。Protocol、Foundation与Go client不得持有或推导该策略。

本subcut不提前激活composer、live delta、history page、command、interaction或queue mutation。S2拥有delta/root/GAP/page/reconnect以及unseen的live推进；S3只在既有full-height几何中激活composer。S6可增加semantic styling、sidebar和主题，但不得重新定义基础viewport几何、wrap、scroll或terminal mode ownership。

### TUI-BT-S1-002 文件清单

新增：

~~~text
clients/terminal/go.mod
clients/terminal/go.sum
clients/terminal/README.md
clients/terminal/cmd/pulsara-tui/main.go
clients/terminal/internal/bootstrap/bootstrap.go
clients/terminal/internal/bootstrap/options.go
clients/terminal/internal/buildinfo/buildinfo.go
clients/terminal/internal/config/config.go
clients/terminal/internal/protocol/terminal_client.pb.go
clients/terminal/internal/protocol/fingerprint_contract_gen.go
clients/terminal/internal/protocolvalue/vocabulary_gen.go
clients/terminal/internal/protocolvalue/carriers_gen.go
clients/terminal/internal/wire/framing.go
clients/terminal/internal/wire/compatibility.go
clients/terminal/internal/wire/decode.go
clients/terminal/internal/wire/encode.go
clients/terminal/internal/wire/validate.go
clients/terminal/internal/presentation/state.go
clients/terminal/internal/presentation/immutable.go
clients/terminal/internal/presentation/snapshot.go
clients/terminal/internal/presentation/operational.go
clients/terminal/internal/presentation/cell.go
clients/terminal/internal/presentation/cursor.go
clients/terminal/internal/commandstate/state.go
clients/terminal/internal/commandstate/candidate.go
clients/terminal/internal/interaction/state.go
clients/terminal/internal/queue/state.go
clients/terminal/internal/secret/state.go
clients/terminal/internal/secret/handle.go
clients/terminal/internal/app/model.go
clients/terminal/internal/app/state.go
clients/terminal/internal/app/message.go
clients/terminal/internal/app/effect.go
clients/terminal/internal/app/input.go
clients/terminal/internal/app/update.go
clients/terminal/internal/app/view.go
clients/terminal/internal/app/layout.go
clients/terminal/internal/client/service.go
clients/terminal/internal/client/runtime.go
clients/terminal/internal/client/connection.go
clients/terminal/internal/client/auth.go
clients/terminal/internal/client/operation_registry.go
clients/terminal/internal/client/scheduler.go
clients/terminal/internal/client/snapshot.go
clients/terminal/internal/client/heartbeat.go
clients/terminal/internal/components/transcript/model.go
clients/terminal/internal/components/transcript/update.go
clients/terminal/internal/components/transcript/view.go
clients/terminal/internal/components/transcript/wrap_cache.go
clients/terminal/internal/app/layout_test.go
clients/terminal/internal/app/view_test.go
clients/terminal/internal/components/transcript/view_test.go
clients/terminal/internal/supervision/child.go
clients/terminal/internal/supervision/signal_unix.go
clients/terminal/internal/supervision/teardown.go
clients/terminal/internal/testkit/fake_executor.go
clients/terminal/internal/testkit/fixture.go
clients/terminal/scripts/generate_protocol.sh
src/pulsara_agent/terminal_protocol/schema/terminal_client_fingerprint_contract.v1.json
src/pulsara_agent/terminal_protocol/schema/terminal_client_fingerprint_golden.v1.json
src/pulsara_agent/terminal_protocol/generated/terminal_client_fingerprint.py
src/pulsara_agent/terminal_protocol/transport_auth.py
tools/generate_terminal_protocol_contract.py
src/pulsara_agent/runtime/terminal_application/control_projection.py
src/pulsara_agent/storage/migrations/sql/0012_terminal_active_queue_projection.sql
src/pulsara_agent/storage/migrations/expected_catalog_v12.json
src/pulsara_agent/storage/migrations/resources/0012_runtime_write_protected_relations_v1.json
src/pulsara_agent/terminal_client/__init__.py
src/pulsara_agent/terminal_client/binary.py
src/pulsara_agent/terminal_client/launcher.py
src/pulsara_agent/terminal_client/supervision.py
tests/test_terminal_tui_launcher.py
tests/test_terminal_tui_cross_language.py
tests/test_terminal_application_services.py
~~~

修改：

~~~text
src/pulsara_agent/terminal_protocol/schema/terminal_client.proto
src/pulsara_agent/terminal_protocol/generated/terminal_client_pb2.py
src/pulsara_agent/terminal_protocol/codec.py
src/pulsara_agent/terminal_protocol/gateway.py
src/pulsara_agent/ports/terminal_application.py
src/pulsara_agent/runtime/terminal_application/services.py
src/pulsara_agent/runtime/terminal_application/prompt_queue.py
src/pulsara_agent/runtime/terminal_application/prompt_queue_checkpoint.py
src/pulsara_agent/storage/prompt_queue_bootstrap.py
src/pulsara_agent/event_log/postgres_prompt_queue.py
src/pulsara_agent/runtime/session.py
src/pulsara_agent/host/session.py
src/pulsara_agent/cli.py
src/pulsara_agent/storage/migrations/manifest.py
src/pulsara_agent/storage/migrations/registry.py
src/pulsara_agent/storage/migrations/grants.py
src/pulsara_agent/storage/migrations/verifier.py
tests/test_terminal_protocol.py
tests/test_postgres_runtime_schema.py
tests/test_schema_verification_service.py
.gitignore
~~~

新增root toolchain pin：

~~~text
mise.toml
~~~

禁止修改Foundation reducer语义和AgentEvent。

### TUI-BT-S1-003 测试矩阵

| 类别 | 必测 |
|---|---|
| pure model | phase graph、`AwaitingDurable -> AwaitingOperational -> BaselinesInstalled`字段存在矩阵、各阶段retry不覆盖confirmed baseline、stale response；snapshot前四个dormant owner；S1合法pending interaction/non-empty active queue安装为read-only projected且actions为空；server/local notifications物理分离 |
| carriers | NormalizedKey/Tick/peer/build/sealed operation + service-owned terminalization stable identity/mutable snapshot/opaque handle、connection-owned drain state/launch permit/runner lease/compatible winner、drain-bound terminal receipts/classifier/teardown/lifecycle/push constructors、bounds、unknown enum与public struct-literal AST gate；operation registry从stage派生delivery phase，伪造`DeliveryNotStarted`、caller invalidation bool、caller cancellation偷走attempt、mutable state fingerprint冒充handle、自由字符串drain handle、winner存在但无launch owner、并发runner、重复physical close、receipt缺少/漂移drain identity、重复initial/post-join capability、重提cause、fully-sent writer NOT_STARTED、unjoined或writer/reader identity不匹配的receipt、stale capability与非唯一constructor call-site均拒绝 |
| protocol | Protocol 2.0/v2 package generated golden、major-1 rejection、Hello mismatch、exact three-value controller + single-value bootstrap vocabulary、full ordinary/recovered Attach ACK proof、Heartbeat request/accepted/rejected、OperationalSnapshot request/frame bounds、unknown oneof |
| capability | required/selected exact set、missing required server reject、slice不得过度广告 |
| bootstrap | one-shot、16 KiB cap、EOF/trailing byte、expiry/PID/path、buffer清除 |
| transport auth | preface-before-Hello、typed auth result、same request conflict/new request retry、candidate attempt generation、stable Hello negotiation winner + per-connection receipt、accepted/unavailable/rejected outcome、candidate terminal receipt + parent relaunch mapping、exact 32-byte challenge commitment、四个closed challenge local operation kind、PREPARED→ACTIVE_PENDING_ACCEPTANCE→confirmed ACTIVE、promoted/accepted result stale/drop/undelivered typed active revoke、stable attach semantic winner、pre-ACK receipt rebind、ACK tombstone rebind、Ready reconnect next candidate/attachment generation、retirement/expiry、secret-free logs |
| serial framing | fully-sent heartbeat/snapshot/observe/page/query read timeout必须安装service-owned terminalization attempt、invalidate原connection并等待reader/writer均JOINED；connection owner原子安装RESERVED launch record并由常驻supervisor的唯一runner lease驱动，registry worker可按stable attempt identity取得compatible start或exact rebind。只有携exact drain identity且来自TERMINAL record的receipt可被attempt-internal post-join capability消费，随后才允许fresh binding重建read。Caller cancellation/deadline只detach且completion在线性化点已安装时COMPLETED优先；close drain超时保留attempt/drain blocker，迟到旧response不得被successor request消费 |
| heartbeat | request exact绑定attachment/winner/binding/generation及connection-neutral candidate；fresh-binding retry复用same candidate且不二次延长lease；accepted receipt以ReceivedAt推进local schedule；closing branch不续lease；五个rejected reason、duplicate/conflict、decoder不得构造AttachmentState |
| operational snapshot | 0/1/256 cells、1 MiB边界、count/bytes/accumulator/outer fingerprint、duplicate identity、超限拒绝；success只推进AwaitingOperational且不改durable/control |
| queue/control snapshot | typed empty genesis、generation-0 checkpoint + first-transition tail committed head、0/1/64 active items、16 server notifications可安装；第65项、17th notification、truncation、count/accumulator/order mismatch拒绝；五section + source versions + cursor来自一个atomic Foundation snapshot；Ready拒绝uninitialized/stale control |
| fingerprint | generated Go/Python canonical helper、opaque-domain denylist、Unicode/uint64/optional golden |
| layout/view golden | `1×1`、height 2/3、`80×24`、120/160列、极窄、CJK、emoji、长不可断字符串；exact visual row count/display width、fixed footer、header/footer no-wrap、无隐藏尺寸clamp |
| viewport | exact wrapped visual rows；Up/Down 1行、wheel 3行、Page为`rows-1`、End、上下界、follow-tail；scrolled resize保留cell/source anchor，tail resize保持tail；height-only cache hit、width-change single rebuild |
| cross-language | Python Gateway -> Go hello/attach/snapshot |
| PTY | first frame、真实`SIGWINCH` resize、alternate-screen `1049h/l`、cell-motion mouse `1002h/l + 1006h/l`、copy-safe content、normal quit恢复；default physical output不得含`CSI 3J` |
| launcher | `--clear-scrollback`默认false、help明确不可逆；显式路径exact single erase、parent relaunch不重复、non-TTY/partial write fail closed；Go/Protocol/Foundation无erase owner |
| supervision | child exit、parent survives、terminal restore |
| architecture | package DAG、no AgentEvent/raw storage import；`app`不importgenerated Protobuf，`protocolvalue` outputs生成后diff-clean且目录内无manual Go file/mirror enum |

### TUI-BT-S1-004 独立 DoD

1. pulsara host tui可显式启动。
2. client只请求observer attachment。
3. snapshot按TUI-BT-CONSUME-002安装。
4.真实history entry按server order显示。
5. Update/View无I/O。
6. Go crash不关闭RuntimeSession。
7. 无delta/command/secret production behavior或自由fallback；S1已存在的final state/carrier owner可以安装read-only interaction/queue snapshot，但不能发对应effect。
8.现有Legacy REPL行为不变。
9. go test -race ./...、go vet、Python cross-language和PTY全绿。
10. production `tea.NewProgram` options exact包含`WithoutSignalHandler()`且不包含`WithoutCatchPanics()`。
11. selected capabilities不含`HISTORY_PAGE_V1`、`CONTROL_PROJECTION_OBSERVATION_V1`、任何observation stream或reconnect rotation；`PRESENTATION_SNAPSHOT_V1`仍必须安装完整control baseline cursor。Viewport边界只展示snapshot resident window且不伪造“历史结束”。
12. Hello negative outcome先安装matching candidate terminal receipt再交付closed parent-relaunch effect；challenge bytes只在generated decoder、runtime owner PREPARED/ACTIVE_PENDING_ACCEPTANCE/ACTIVE record与Attach encoder一次性borrow路径中存活。Promotion result必须被application接纳并经独立confirmation才可供Attach；promoted/accepted result stale/drop/undelivered以及constructor/send failure/operation successor/deadline/teardown均有typed owner-confirmed prepared/active revoke；commitment与receipt有Python/Go golden。
13. Attach winner只消费Protocol exact controller enum与唯一bootstrap value；ordinary/recovered ACK均保留完整validated result及acknowledged binding proof，Go无mirror enum或bool-union lowering。
14. Heartbeat使用完整prepared request与accepted/rejected receipt；client-local schedule与semantic attachment分离，decoder不能返回下一份AttachmentState。
15. Snapshot bootstrap严格经过AwaitingDurable、AwaitingOperational、BaselinesInstalled；Operational request/frame字段、256项/1 MiB bounds与outer fingerprint均有跨语言golden，Ready不能绕过substate validator。
16. 正常terminal尺寸下View exact填满真实WindowSize：1行header、`height-2`行transcript、1行read-only footer；height 1/2按closed compact matrix降级且无伪composer。
17. `transcript.Model`是scroll/follow/anchor/wrap cache唯一owner；`presentation.State`和`AppState`不存在第二套viewport state，View零I/O且不重算state transition。
18. 默认alternate-screen启动不发送`CSI 3J`；显式`--clear-scrollback`只由Python launcher在首次child前执行一次，并对non-TTY/partial write fail closed。
19. PTY证据证明alternate-screen与mouse mode正常enter/exit、真实resize生效、quit恢复primary terminal；basic full-height geometry不得推迟到S6。

## 11. S2：Delta、root、history、GAP 与 reconnect

### TUI-BT-S2-000 Protocol prerequisite

Protocol必须先落地TUI-PROTO-OBS-002A：snapshot完整control cursor、`ObserveNextRequest.after_control_cursor`、Changed/GAP branches、`CONTROL_PROJECTION_OBSERVATION_V1`和Python bounded transition owner。Queue-only、interaction-only、run-control-only transition的headless tests必须证明即使没有history delta也能收到signal；Python ring内coalescing必须从ordered records重建exact section union/range accumulator，Go只验证opaque proof及base/result join；ring外必须typed GAP。S2还必须激活`HISTORY_PAGE_V1`与`RECONNECT_AUTH_ROTATION_V1`，并证明client在三条路径可用前不广告它们。该subcut未全绿时S2不得启动observe production loop。

### TUI-BT-S2-001 目标

增加：

- ObserveNext serial scheduler；
- ProjectionDelta、AuthorityAdvance、RootAdvanced；
- OperationalDelta/GAP（OperationalSnapshot已由S1正式实现）；
- history page四branch；
-follow-tail/unseen/pinned root；
-server/local GAP rebuild；
-disconnect/reconnect。

仍为observer，不发mutation。

### TUI-BT-S2-002 文件清单

新增：

~~~text
clients/terminal/internal/presentation/projection_delta.go
clients/terminal/internal/presentation/root_advance.go
clients/terminal/internal/presentation/operational.go
clients/terminal/internal/presentation/gap.go
clients/terminal/internal/presentation/control_change.go
clients/terminal/internal/presentation/page.go
clients/terminal/internal/presentation/cache.go
clients/terminal/internal/client/bridge.go
clients/terminal/internal/client/observe.go
clients/terminal/internal/client/history.go
clients/terminal/internal/components/status/view.go
clients/terminal/testdata/protocol/
clients/terminal/testdata/view/
~~~

修改：

~~~text
clients/terminal/internal/app/message.go
clients/terminal/internal/app/effect.go
clients/terminal/internal/app/update.go
clients/terminal/internal/app/view.go
clients/terminal/internal/presentation/state.go
clients/terminal/internal/presentation/snapshot.go
clients/terminal/internal/presentation/cursor.go
clients/terminal/internal/client/service.go
clients/terminal/internal/client/scheduler.go
clients/terminal/internal/client/auth.go
clients/terminal/internal/components/transcript/*
src/pulsara_agent/terminal_protocol/transport_auth.py
src/pulsara_agent/terminal_protocol/gateway.py
src/pulsara_agent/runtime/terminal_application/control_projection.py
tests/test_terminal_protocol.py
tests/test_terminal_presentation_foundation.py
tests/test_terminal_infrastructure_postgres.py
tests/test_terminal_infrastructure_architecture.py
tests/test_terminal_tui_cross_language.py
~~~

### TUI-BT-S2-003 测试矩阵

| 类别 | 必测 |
|---|---|
| projection | next、duplicate、overlap、same revision conflict；multi-plane batch原子消费 |
| root | unchanged、bounded changes、rebase、retained noop suffix |
| operational | coalesce、remove、generation gap、resnapshot |
| control | queue/interaction/run/lifecycle/notification-only signal、atomic view+source versions+cursor、Python ring内union/range golden、Go opaque proof structural join、ring外/重启typed GAP；Fresh -> SnapshotRequired保留stale view/confirmed cursor且单独记observed latest，minimum-bound snapshot才恢复Fresh；generation-rebase response最多4轮且共享deadline；Ready对stale/uninitialized fail closed |
| GAP | server gap、local overflow、dual snapshot rebuild |
| page | PAGE/STALE/REBASE/RECONCILIATION、before/after同cursor |
| viewport | follow-tail、unseen、old pinned root、new latest pair |
| reconnect | pre-Ready same candidate generation、Ready next generation、typed auth result、credential rotation、semantic winner/per-connection receipt、pre/post-ACK recovery、old/new grace、ordinary draft保留、secret为空、command无重放 |
| fairness/performance | control+durable+operational同时pending全部进入一批；持续100Hz任一plane不饿死其他plane、不丢keypress、bounded memory |

### TUI-BT-S2-004 独立 DoD

1. ObservationBatch每plane最多一个closed branch，ProjectionDelta、AuthorityAdvance、RootAdvanced、OperationalDelta、ControlChanged、ControlGap、history/operational GAP与NoChange全部closed处理且无饥饿。
2. root advance一次性安装，不丢concurrent suffix。
3. page stale/rebase不显示history end。
4. durable和operational失效可独立恢复。
5.任何overflow不阻塞Python writer。
6. reconnect只靠正式snapshot/delta。
7. old pinned cursor不覆盖latest pair。
8.仍无mutation/secret production path。
9. `HISTORY_PAGE_V1`、`CONTROL_PROJECTION_OBSERVATION_V1`与`RECONNECT_AUTH_ROTATION_V1`只在page四分支/cache、control Changed/GAP消费和credential rotation gates全绿的build中进入required/selected set。
10. Control invalidation不破坏cursor→view join；observed latest只是snapshot lower bound，matching snapshot前App仅能ReadOnly且不发新Observe。

## 12. S3：Composer、submit、stop 与 command receipt

### TUI-BT-S3-001 前置

Protocol必须明确关闭：

- S2 `ObservationBatchFrame.control`中的`ControlProjectionChangedFrame`已是唯一control projection invalidation，不允许退回history-delta proxy或单独第二response；
- large paste超过frame cap时的typed artifact preparation carrier，或明确V1拒绝；
- command target identity/generation的client source matrix。

这些规则必须先进入Protocol schema/contract/golden；Go不得自行发明。

### TUI-BT-S3-002 目标

- controller attach；
- ordinary composer；
-bounded undo/history/paste review；
- stable SubmitPrompt；
- StopRun；
- command outcome/query；
- key routing；
- detach/close distinction；
- active run时继续编辑。

### TUI-BT-S3-003 文件清单

新增：

~~~text
clients/terminal/internal/commandstate/transition.go
clients/terminal/internal/components/composer/model.go
clients/terminal/internal/components/composer/update.go
clients/terminal/internal/components/composer/view.go
clients/terminal/internal/components/composer/history.go
clients/terminal/internal/components/composer/paste.go
clients/terminal/internal/components/notification/model.go
clients/terminal/internal/components/notification/view.go
clients/terminal/internal/client/mutation.go
clients/terminal/internal/app/keymap.go
~~~

修改：

~~~text
clients/terminal/internal/commandstate/state.go
clients/terminal/internal/commandstate/candidate.go
clients/terminal/internal/app/state.go
clients/terminal/internal/app/message.go
clients/terminal/internal/app/effect.go
clients/terminal/internal/app/update.go
clients/terminal/internal/app/view.go
clients/terminal/internal/client/service.go
clients/terminal/internal/client/scheduler.go
src/pulsara_agent/terminal_protocol/schema/terminal_client.proto   # only if prereq requires
src/pulsara_agent/terminal_protocol/codec.py
src/pulsara_agent/terminal_protocol/generated/terminal_client_pb2.py
tests/test_terminal_protocol.py
tests/test_terminal_tui_cross_language.py
~~~

Wire修改的语义必须写回Protocol文档，不由本文拥有。

### TUI-BT-S3-004 测试矩阵

| 类别 | 必测 |
|---|---|
| composer | multiline、wide rune、undo bound、draft revision |
| paste | small、large review、frame cap、cancel |
| command | SUCCEEDED/REJECTED/PENDING/RECONCILIATION/COMPATIBLE |
| idempotency | receipt lost、query found/missing、same-ID resend |
| controller | observer reject、takeover、stale generation |
| stop | repeated key same command、disconnect query |
| signals | Ctrl-C不杀Python、SIGTERM teardown |
| PTY | input during 100Hz stream、no draft loss |

### TUI-BT-S3-005 独立 DoD

1.所有mutation由command registry frozen candidate发出。
2. socket send永不显示accepted。
3. receipt丢失可same-ID query。
4. active run期间composer可编辑。
5. StopRun不传播signal给Python。
6.大paste不绕过frame/secret/authority boundary。
7. detach不等于close conversation。
8. S1/S2 observation gates保持全绿。

## 13. S4：Typed interaction 与 secret

### TUI-BT-S4-001 前置

Protocol必须先提供：

- event-safe MCP form field/schema closed carrier；
- form与URL mode的closed branch；
- interaction target generation/source matrix；
- request-scoped `SecretLeaseRevoked` typed frame，并冻结attachment invalidation/EOF时的local `SecretRuntimeOwner.RevokeAll`。

S4不依赖通用`ServerClosingFrame`：Host close若能先发送matching revoke则消费它，否则connection invalidation仍必须本地撤销全部handle。通用graceful closing UX保留到S6，不能作为S4 secret safety的前置。

没有form schema时S4不能用JSON textarea替代。

### TUI-BT-S4-002 目标

- approval；
- plan question；
- plan exit；
- MCP private URL reveal；
- MCP form；
- secret mutable buffer；
- sealed handle -> resolve；
- stale/replacement/takeover/revoke。

### TUI-BT-S4-003 文件清单

新增：

~~~text
clients/terminal/internal/interaction/transition.go
clients/terminal/internal/secret/buffer.go
clients/terminal/internal/secret/runtime.go
clients/terminal/internal/secret/transition.go
clients/terminal/internal/client/secret.go
clients/terminal/internal/components/interaction/view.go
~~~

修改：

~~~text
clients/terminal/internal/interaction/state.go
clients/terminal/internal/secret/state.go
clients/terminal/internal/secret/handle.go
clients/terminal/internal/app/state.go
clients/terminal/internal/app/message.go
clients/terminal/internal/app/effect.go
clients/terminal/internal/app/update.go
clients/terminal/internal/app/view.go
clients/terminal/internal/app/keymap.go
clients/terminal/internal/client/service.go
clients/terminal/internal/client/runtime.go
clients/terminal/internal/client/mutation.go
src/pulsara_agent/terminal_protocol/schema/terminal_client.proto
src/pulsara_agent/terminal_protocol/codec.py
src/pulsara_agent/terminal_protocol/generated/terminal_client_pb2.py
tests/test_terminal_protocol.py
tests/test_mcp_host_lifecycle.py
tests/test_terminal_tui_cross_language.py
~~~

### TUI-BT-S4-004 测试矩阵

| 类别 | 必测 |
|---|---|
| interaction |四oneof、replacement、clear、late receipt |
| approval | exact tool IDs、partial decisions、stale reject |
| plan | option/free text、exit decisions |
| URL | reveal、explicit open/copy、no prefetch、revoke |
| form | closed schema、validation、atomic seal、resolve |
| secret | no history/log/snapshot/debug、best-effort overwrite、edit handle one-shot/5s expiry/duplicate/revoke |
| bridge | generated private bytes安装后清除、secret key/paste normalizer只生成edit handle、Program.Send只见handle、blocked send cancellation revoke |
| ownership | observer reject、takeover、detach、expiry |
| reconnect | ordinary draft保留、secret draft删除、新lease |

### TUI-BT-S4-005 独立 DoD

1.没有手写arbitrary JSON主路径。
2.旧interaction action即时disabled。
3. server reveal与assembled form plaintext只存在AppState外的SecretRuntimeOwner mutable cell；secret-mode raw key/paste仅在framework ingress瞬时存在并立刻安装为one-shot edit handle；Program.Send、AppState和application message/effect只见opaque handle。
4. private URL不进入copy-all、history、notification。
5. form先seal再resolve。
6. detach/takeover/revoke后旧lease不可继续使用。
7. Go不校验continuation authority，只消费server disposition。

## 14. S5：Durable queue UX

### TUI-BT-S5-001 前置

Protocol的`ControlProjectionChangedFrame(PROMPT_QUEUE, SNAPSHOT_REQUIRED)`、control cursor/opaque transition proof与typed control GAP必须已在S2生产使用并通过queue-only headless gate。Foundation active projection max-64 admission/snapshot contract必须已在S1接线。S5固定消费该coalesced snapshot disposition，不在本阶段另选queue delta，也不能从history或command text重建QueueItemView。

### TUI-BT-S5-002 目标

- queue list；
- AUTO/FOLLOW_UP/STEER；
- queue cancel；
- cancel-confirmed后new-submit replacement；
- reconnect双command query；
- capacity/rotation action。

### TUI-BT-S5-003 文件清单

新增：

~~~text
clients/terminal/internal/queue/transition.go
clients/terminal/internal/components/queue/view.go
~~~

修改：

~~~text
clients/terminal/internal/queue/state.go
clients/terminal/internal/app/state.go
clients/terminal/internal/app/message.go
clients/terminal/internal/app/effect.go
clients/terminal/internal/app/update.go
clients/terminal/internal/app/view.go
clients/terminal/internal/client/mutation.go
clients/terminal/internal/presentation/snapshot.go
clients/terminal/internal/presentation/control_change.go
tests/test_terminal_tui_cross_language.py
~~~

### TUI-BT-S5-004 测试矩阵

| 类别 | 必测 |
|---|---|
| projection | snapshot replace、incremental replace、reconnect |
| bounds | 0/1/64 active items accepted；65/truncated/order/count/accumulator mismatch拒绝；terminal rows never projected |
| submit | AUTO/FOLLOW_UP/STEER server outcomes |
| cancel | accepted/rejected/pending/reconciliation |
| replacement | cancel success then submit；submit failure不复活旧item |
| reconnect |分别query cancel与replacement ID |
| race | server item更新与late command receipt |
| capacity | available/rotation/exhausted/reconciliation |
| negative |无QueueEdit/Reclassify、无silent steer downgrade |

### TUI-BT-S5-005 独立 DoD

1. queue只显示server-projected items。
2. local draft不冒充queued。
3. replacement严格两command。
4. reconnect不重复submission。
5. explicit steer不静默降级。
6. capacity branch只显示/禁用，不重算。
7. queue update不依赖full EventLog或Python internals。
8. Go不截断queue；最多64项由Python admission/projection contract保证，reconciliation item只读显示。

## 15. S6：Semantic rendering、distribution 与默认激活

### TUI-BT-S6-001 前置

- ServerClosing typed frame由Protocol owner补齐；
-四平台clean runner存在；
-platform wheel build hook完成；
-S1–S5 cross-language、PTY和real dogfood全绿；
-默认切换有release rollback flag。

### TUI-BT-S6-002 目标

- compact/verbose semantic cell mechanics；
-tool grouping display；
- operational status；
-responsive sidebar；
-bounded notification；
-完整teardown；
-four-platform archive和wheel；
-checksum/SBOM/license；
-default TTY activation。

### TUI-BT-S6-003 文件清单

新增：

~~~text
clients/terminal/internal/components/sidebar/view.go
clients/terminal/internal/release/manifest.go
clients/terminal/internal/release/selftest.go
clients/terminal/internal/testkit/pty.go
clients/terminal/testdata/pty/
clients/terminal/scripts/build.sh
clients/terminal/scripts/package.sh
clients/terminal/scripts/verify_dist.sh
tests/test_terminal_tui_release.py
.github/workflows/terminal-client-release.yml
hatch_build.py
~~~

修改：

~~~text
clients/terminal/internal/app/view.go
clients/terminal/internal/app/layout.go
clients/terminal/internal/components/status/view.go
clients/terminal/internal/components/transcript/view.go
clients/terminal/internal/components/transcript/wrap_cache.go
clients/terminal/internal/components/notification/*
clients/terminal/internal/supervision/*
clients/terminal/README.md
src/pulsara_agent/terminal_client/binary.py
src/pulsara_agent/terminal_client/launcher.py
src/pulsara_agent/terminal_client/supervision.py
src/pulsara_agent/cli.py
pyproject.toml
uv.lock
PULSARA_TERMINAL_UI_UX_RESEARCH_AND_DESIGN.zh.md
PULSARA_LEGACY_REPL_RETENTION_CONTRACT.zh.md
~~~

### TUI-BT-S6-004 测试矩阵

| 类别 | 必测 |
|---|---|
| view golden | 80/120/160、极窄/极矮、CJK、long unbroken |
| semantic |全部DurableHistoryCell与OperationalActivityCell branch |
| must-show | error/interaction/capacity不可隐藏 |
| PTY | paste、resize、signals、panic、SIGTERM、SIGKILL parent recovery |
| tmux/SSH | recorded regression matrix |
| packaging |四archive、四wheel、SHA、manifest、SBOM |
| clean install |四platform wheel install + --self-test |
| compatibility |major/minor/schema/capabilities、missing binary |
| activation |host默认TUI、host tui、host repl、non-TTY |
| rollback |只切entry flag，不变更durable state |

### TUI-BT-S6-005 独立 DoD

1.所有required cell/activity branch有renderer。
2.未知required branchfail closed。
3. render/status callback零I/O。
4. layout在冻结尺寸可用。
5.四平台制品与checksum通过。
6. Python wheel只带matching binary。
7.默认入口切换无silent fallback。
8. Legacy显式入口行为保持，且没有新authority旁路。
9. release rollback不触碰Foundation/Protocol schema。

## 16. Global architecture guards

必须新增机器gate：

1. clients/terminal不出现AgentEvent、RawStoredEventEnvelope、StoredEventBatchCommitReceipt、PostgreSQL table name。
2. generated Protobuf只有一个definition owner。
3. app/components不importclient/wire/protocol。
4. Update/View中禁止net、os、io、time.Now、database、exec调用。
5. server reveal只允许generated/wire decoder到`SecretRuntimeOwner.Install*`的bounded transient `[]byte`；local form edit只允许framework raw carrier、normalizer stack与`InstallEdit`参数的bounded transient bytes。两条路径之外禁止private URL/form plaintext field、string conversion、retain、log、replay；AppState/application message/effect不得含plaintext，只能含opaque value/edit handle。
6.不存在map[string]any、json.RawMessage command/interaction fallback。
7.不存在QueueEdit、QueueReclassify。
8.不存在RunLifecycleCell。
9. history model不保存direction或feed kind。
10. display rank不作为map/cache stable key的唯一组成。
11. Go不重算history capacity或queue state；fingerprint只按Protocol manifest分类，opaque domain fingerprint不进入Go canonical helper。
12. production module不importspikes/s0。
13. Python src不importtests/support或Go spike。
14. client package只有一个socket scheduler和一个teardown owner。
15. release binary不包含dirty build或local replacement module。
16. production Program options exact包含`WithoutSignalHandler()`并保留default panic catcher；signal_unix.go是唯一SIGTERM/SIGHUP/SIGINT owner。
17. `Program.Send`可达message type中不存在secret plaintext carrier。
18. secret error/panic path只使用closed code；panic/teardown stderr canary scan不含installed secret。
19. S1已定义command/interaction/queue/secret final state与必要carrier；interaction/queue可安装read-only projected snapshot但不能发mutation；S3–S5不得重新新增同名owner或用build tag隐藏缺失type。
20. wire effect header必须嵌入exact preinstalled `OperationToken`；executor内生成RequestID、local header出现RequestID或effect token与state token不等均为architecture failure。
21. `ProjectionDeltaFrame` decoder/model只接受history changes；control changes只能到达ControlChanged/ControlGap message。
22. `ConnectionLostMsg`不得使用`IOMessageHeader`，`ServerClosingMsg`/`SecretRevokedMsg`不得使用request token；push/lifecycle header不能完成outstanding request。
23. `PublicFailure`禁止public literal、caller-supplied code/disposition；只有registry-sealed operation receipt以及在connection-invalidating branch中matching owner-signed connection-terminal receipt经central classifier可生产。Initial settlement capability在begin时消费；两张successor capability只存在service-owned attempt中，post-join只能使用冻结cause。Caller cancellation/deadline只detach handle；classifier必须穷尽operation/delivery/connection/cause/terminal-reason，message validator拒绝不兼容code。
24. Queue snapshot mapper/client不存在`[:64]`、`maxItems`截断或历史row过滤；只有validated active projection factory可进入state。
25. Control transition fingerprint/accumulator不进入Go canonical helper；只有Protocol manifest标记的opaque structural join合法。
26. Server-projected notification只存在`presentation.ControlProjectionState`，local notification只存在`LocalNotificationState`；Tick不得修改server vector/fingerprint。
27. Protocol observation response只进入一个`ObservationBatchMsg`；bridge禁止按plane拆分send，Gateway/headless fixture必须证明pending control/durable/operational无饥饿。
28. Handshake candidate generation只覆盖一次semantic attach attempt；Ready后的ordinary reconnect必须next generation，pre-ACK receipt recovery与post-ACK tombstone recovery各有typed physical rebind；Hello unavailable/rejected先安装candidate terminal receipt，只能生成closed parent-relaunch cause。
29. Terminalization attempt identity/snapshot/record、prepared/successor capability constructors、registry worker、wait linearization与`settleConnectionFailure`只能由`internal/client/operation_registry.go`拥有；drain state/launch permit/runner lease/handle constructors、constant supervisor、`startInvalidateClose` compatible-winner registry、`rebindPhysicalDrain`、`waitPhysicalDrain`与drain-bound terminal receipt只能在`internal/client/connection.go`拥有。Physical drain三入口只能由registry worker调用，runner只能由connection supervisor inline驱动。其他production package不得构造/借出attempt或drain internals；caller只能持stable opaque attempt handle，wait cancellation/deadline不取消physical owner，已安装completion在线性化竞速中优先，teardown必须drain registry attempts与connection drain records。
30. Fully-sent read deadline必须invalidate/close原connection并等physical reader与writer均JOINED；receipt中的reader/writer operation ID/generation必须与connection registry exact join，writer NOT_STARTED只能用于PhysicalInstalled且zero-byte的pre-write branch。Matching receipt被attempt-internal post-join capability消费前不得结算operation、reconnect或在旧stream安装successor request/token。
31. Control state只能是`UNINITIALIZED | FRESH | SNAPSHOT_REQUIRED`；Ready必须Fresh，observed latest cursor不得覆盖与stale view绑定的confirmed cursor。
32. Attachment challenge commitment只由Protocol-generated helper从exact 32 bytes与current request/auth/candidate/winner/connection重算；challenge lifecycle只允许四个closed local operation kind。Decoder只建立PREPARED；promotion只到ACTIVE_PENDING_APPLICATION_ACCEPTANCE，matching promoted message经application接纳及confirmation后才到ACTIVE。Promoted/accepted result非apply或undelivered，以及所有authority/deadline/teardown出口均typed revoke prepared/active record。Go AppState/message/effect不持有challenge plaintext，Attach effect只持confirmed ACTIVE identity + commitment并由executor一次性borrow。

## 17. Global test gates

每个slice都必须运行：

~~~text
go test ./...
go test -race ./...
go vet ./...
go mod verify
go fmt / gofmt clean
generated protocol diff clean
uv run pytest matching Python/protocol/launcher nodes
uv run ruff check .
uv run ruff format --check .
git diff --check
~~~

跨语言golden至少覆盖：

- Hello/Attach/完整ordinary与tombstone-recovery ACK proof、Protocol exact controller/bootstrap vocabulary；
- Heartbeat prepared request、accepted/rejected receipt与client-local schedule transition；
- Durable Snapshot及`AwaitingDurable -> AwaitingOperational -> BaselinesInstalled`；
- ProjectionDelta/AuthorityAdvance/RootAdvanced；
- OperationalSnapshot request/full frame/Delta；
- GAP/NoChange；
-ControlProjectionChanged/ControlProjectionGap、confirmed/observed dual-cursor state、minimum-bound snapshot、control source-version/bootstrap proof、opaque ring proof与queue-only/interaction-only触发；
- PAGE/STALE/REBASE/RECONCILIATION；
-所有MutationCommand和CommandOutcome；
-Query found/missing；
-Secret reveal/submit/revoke；
-capability selected/missing-required、bootstrap carrier、typed transport-auth result、connection-neutral handshake candidate、stable Hello negotiation winner/current receipt、accepted/unavailable/rejected outcome与candidate terminal receipt/parent relaunch、exact challenge commitment、四种local operation token、promotion acceptance confirmation、promoted/accepted result stale/drop/undelivered active revoke、Attach semantic winner/current receipt、Attach ACK tombstone/credential rotation、operation identity与fingerprint contract golden；
- fully-sent timeout的service-owned stable attempt identity/mutable snapshot、physical start `CREATED | COMPATIBLE_ALREADY_CREATED | CONFLICT`、drain record `RESERVED -> STARTING -> RUNNING -> TERMINAL`、winner安装后/runner lease前panic、STARTING/RUNNING panic与successor lease、supervisor restart全表scan、无未受管child goroutine、typed handle exact rebind、第二个/并发physical close拒绝、caller cancellation/deadline detach、completion-vs-cancel/deadline锁内竞速、三分支wait字段存在矩阵、close drain、terminal receipt exact drain fingerprint、different drain winner receipt拒绝、connection invalidate/close/reader+writer JOINED receipt、physical-drain blocked、receipt/operation/binding/writer-owner mismatch、writer NOT_STARTED拒绝、initial capability二次使用、post-join重新提交cause与caller bool伪造对抗fixture；
-queue zero genesis、generation-0 checkpoint + first-transition tail、post-checkpoint zero-tail committed head三类branch golden；
-idle connection loss、unsolicited ServerClosing push与in-flight settlement matrix；
-unknown/removed branch fail closed。

性能gate：

-100Hz operational stream下draft不丢；
-keypress latency不回退超过S0 guard；
-resident history/page/cache保持bounded；
-local overflow产生GAP而不阻塞server；
-View不分配与total session history成比例的对象；
-root/page处理与wire payload size线性，不与EventLog size相关。

## 18. Final Definition of Done

只有以下全部成立，Go Terminal client hard cut才完成：

1. S0只有PASS结论与可追溯证据，不再承载production设计。
2. Foundation、Protocol、Research、Go四份文档ownership无重叠。
3. production Go module与spike物理隔离。
4. package DAG由machine gate证明无cycle/forbidden import。
5. AppState、messages、effects均closed。
5a. 七个central state、S1全部message/effect均有concrete fields、constructor、validator与stale verdict；后续slice遵循同一header matrix。
5b. command/interaction/queue/secret final state与其carrier在S1可编译；snapshot前dormant，interaction/queue snapshot后read-only projected；后续slice只激活transition，不新增第二type owner。
5c. `PhaseReady`必须安装Fresh control baseline；Changed/GAP只进入ReadOnly `SnapshotRequired(stale view, confirmed cursor, observed latest cursor)`，不早于minimum observed cursor的atomic snapshot才能恢复Ready。
5d. Protocol-generated `protocolvalue`是controller/bootstrap non-Protobuf value与validated ACK/heartbeat/operational carrier的唯一Go owner；AppState不得保存generated Protobuf或手写mirror vocabulary。Snapshot bootstrap必须以closed substate证明durable与operational baseline依次安装，Heartbeat decoder不得构造客户端调度状态。
6. generated Protobuf不进入长期Model。
7. Update/View零I/O。
8. snapshot、delta、root advance、operational、GAP、page算法逐branch测试。
9. durable与operational state物理分离。
10. reconnect使用runtime-owned rotating credential；handshake candidate按semantic attachment attempt分代，pre-Ready physical retry复用same generation，Ready后的ordinary reconnect或negative-Hello relaunch使用next generation。Hello negotiation semantic winner与current-connection receipt分离，unavailable/rejected有stable candidate terminal receipt与closed relaunch cause；challenge使用PREPARED→ACTIVE_PENDING_APPLICATION_ACCEPTANCE→confirmed ACTIVE，四个closed local operation kind与delivery guard保证所有非接纳路径revoke；Attach semantic winner又exact引用Hello winner并与per-connection receipt分离；pre-ACK/ACK-result丢失分别由typed rebind/bounded tombstone恢复，不丢ordinary draft、不保留无owner secret/challenge plaintext、不重放new command ID。
11. command成功只来自server outcome/query。
12. interaction action绑定exact current view。
13. secret不进入history、snapshot、log、notification或ordinary queue。
13a. server secret reveal先安装进AppState外SecretRuntimeOwner；secret key/paste在normalizer安装成one-shot edit handle；Bubble Tea application message/effect只接收opaque handle，revocation使future borrow fail closed。
14. queue只来自server projection，replacement严格cancel后new submit。
15. parent/child signal不把Ctrl-C误传成Python cancellation。
15a. Bubble Tea default signal handler已显式禁用；default panic recovery保留并与parent emergency restore分层。
16. bridge/scheduler/physical operation在teardown内bounded退出；fully-sent read timeout必须由registry安装service-owned stable terminalization identity与mutable snapshot、消费initial capability，由connection owner原子安装RESERVED drain record/launch permit，并由常驻supervisor以唯一active runner lease推进STARTING/RUNNING、幂等关闭原stream并等reader/writer均JOINED。Registry worker重驱只能取得compatible winner或exact rebind；只有携matching drain identity、来自TERMINAL record的receipt可由内部post-join capability消费并开放reconnect。Caller取消和ordinary deadline只detach，若completion已在registry锁内安装则COMPLETED优先；teardown drain超时保持close blocked、attempt/drain record ownership，不得重用capability、重提cause、让winner无launch owner、并发/重复启动physical close、接纳foreign-drain receipt、用writer NOT_STARTED/caller bool或在旧framing stream上重试。
17. client crash/detach不取消Python run。
18.四平台binary和wheel均clean-runner验证。
19. checksum、signature、manifest、SBOM和license inventory齐全。
20. protocol incompatibilitytyped fail closed。
21.默认TTY入口切换后无silent Legacy fallback。
22. pulsara host repl仍为显式Frozen Legacy入口。
23. S1–S6每个slice独立DoD和前序回归全绿。
24.所有Protocol前置缺口都在Protocol owner关闭，Go中没有临时推断。
24a. lifecycle/run/interaction/queue/notification由Foundation atomic control view + cursor唯一证明；queue-only transition有独立opaque bounded proof及Changed/GAP/snapshot-required branch，server notification与local notification物理分离。
24b. S1不广告history page/control observation；S2只有在page四分支/cache、control observation与reconnect rotation均完成后才广告对应capabilities；snapshot control baseline无条件存在。
24c. Wire operation effect在dispatch前已携带state中exact operation/request ID；local effect使用独立无RequestID carrier。
24d. Sealed physical failure receipt经唯一classifier产生code与closed disposition，operation/delivery/connection/cause矩阵穷尽且不可由caller覆盖；idle connection loss与server push不伪造request token。
24e. Queue snapshot只接纳server reducer派生的typed empty-genesis head或committed head及最多64项active projection；S1不截断、不要求queue为空。
24f. Observe response每plane最多一个branch、pending plane全量纳入同一batch；100Hz任一plane不能饿死control/durable/operational其他plane。
24g. Protocol physical version已原子切到2.0与`pulsara.terminal.v2`，major-1固定拒绝且无dual decoder/package。
25.工作区无未解释generated diff、binary、cache或dirty release artifact。
