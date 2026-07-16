# Pulsara MCP Startup Latency Hard Cut 实施文档

> 状态：M0–M4 已于 2026-07-11 完成 hard cut 并闭环验收；本文保留为 canonical implementation / acceptance record。
>
> 问题证据：`PULSARA_MCP_STARTUP_LATENCY_NOTE.zh.md`。
>
> 上位路线：`PULSARA_NEXT_FIVE_HARD_CUT_STAGES_PLAN.zh.md`。
> Hard cut：不保留同步 `sync_servers()` production path，不保留 live-manager snapshot 兼容读取。

## 0. 目标与最终语义

### 0.1 用户可见语义

#### 只有 optional MCP

```text
pulsara host repl
  -> resolve workspace / storage / runtime
  -> install optional MCP STARTING snapshot（无tool binding）
  -> publish HostSession
  -> print REPL banner
  -> background connect/discovery
  -> next HostSession safe point atomically install READY snapshot/bindings
```

optional MCP 的网络连接、SDK initialize、tools/resources/prompts discovery 不进入 REPL banner 的
blocking critical path。

#### 存在 required MCP

```text
open session
  -> start required + optional workers concurrently
  -> await required servers only, bounded by required startup deadline
  -> all required READY: install required READY + current optional snapshots, publish session
  -> any required FAILED / NEEDS_AUTH / timeout: fail session open and close all MCP work
```

`required=true` 是明确的 session-open availability contract，不再只是 diagnostic severity。

#### safe point

- worker 完成时只提交候选，不修改 active `AgentRuntime`；
- HostSession 在 safe point 验证 epoch并原子安装；
- 同一 run 内 capability descriptor、tool schema、execution binding不变；
- 用户下一次 turn/resume 才看到新 MCP surface；
- config disable/removal在下一个safe point立即撤销，不等待网络。

### 0.2 性能目标

- 没有 required MCP 时，session open 不 await任何 MCP network I/O；
- optional server 35秒 discovery不能把 REPL banner推迟35秒；
- unchanged config + unexpired snapshot不会在每turn重新discover；
- required总等待受一个明确deadline控制；
- discovery可按server并发，server内部只请求声明支持的capabilities。

### 0.3 安全目标

- stale worker不能在disable/reconfigure/close后安装；
- descriptor与binding共同装入同一个frozen execution surface，并以精确`McpBindingIdentity`配对；未变化server的
  binding对象允许跨global installation复用；
- suspended MCP request不静默迁移到另一个binding generation；
- close/shutdown必须cancel、drain并关闭所有pending/installed manager；
- literal headers/token/env secret不进入durable payload或event-safe fingerprint；
- worker异常、取消、超时不会遗留永久STARTING ownership。

## 1. 当前代码真相

### 1.1 session open 被同步 MCP 阻塞

当前 `src/pulsara_agent/host/core.py`：

```python
async def _build_mcp_supervisor(...):
    configs = load_mcp_server_configs(...)
    supervisor = McpServerSupervisor()
    await supervisor.sync_servers(configs)
    return supervisor
```

`_open_session_with_runtime_id()` 在构造 runtime wiring 前等待该函数。实测约35秒中约35秒来自
MCP sync。

### 1.2 每个 turn/resume 重做远端 sync

当前 `HostSession._sync_mcp_servers_for_turn()`：

1. 重新读取 config；
2. `await supervisor.sync_servers(configs)`；
3. build bundle；
4. 替换extra tool bindings；
5. 替换runtime wiring；
6. rebuild capability runtime；
7. 必要时fail active subagents。

该方法在new turn、stream turn、approval resume、plan resume、MCP resume等入口调用，因此远端
refresh处于多个首包critical path。

### 1.3 supervisor在单锁内执行网络I/O

`McpServerSupervisor.sync_servers()` 持有 `_lock`，并在锁内：

- close manager；
- start SDK manager；
- refresh discovery；
- schedule retry。

慢server会阻塞其他server、close和并发safe point。

### 1.4 SDK discovery串行探测optional methods

当前 `_discover_connected()` 顺序：

1. `tools/list`；
2. `resources/list`；
3. `resources/templates/list`；
4. `prompts/list`。

后3项通过失败来探测optional capability。一个不支持prompts的server仍承担完整request timeout并写
diagnostic。

### 1.5 bundle仍能观察live manager漂移

`McpCapabilityBindingBundle` 保存 `manager`；HostSession 的 ready-server判断会再次读取
`bundle.manager.snapshots`。因此bundle descriptors虽然构造时冻结，bundle解释仍可能看到manager后来
更新的snapshot。

### 1.6 close ownership重复

- `HostSession`持有`mcp_supervisor`；
- `RuntimeWiring`又持有`mcp_manager/mcp_bundle`；
- `HostSession.aclose()`从runtime wiring关闭manager；
- HostCore rollback也可能关闭supervisor/manager。

V1必须收口为session-owned supervisor唯一close owner。

## 2. 非目标

- 不改变MCP protocol request/response语义；
- 不改变MCP tool permission gate；
- 不实现跨process或跨session的persistent discovery cache；
- 不共享stdio process或SDK client给多个HostSession；
- 不在background worker中写AgentRuntime、ToolRegistry或CapabilityRuntime；
- 不mid-run热插拔tool；
- 不为旧`startup_timeout_ms`、旧bundle constructor或旧sync API保留production alias；
- 不用thread fire-and-forget；所有work都是event-loop task并由supervisor拥有。

## 3. 新低层 Contracts

落点固定为：

- event-safe facts：`src/pulsara_agent/primitives/mcp.py`；
- process-local config/candidate/lease：`src/pulsara_agent/runtime/mcp/types.py`；
- SDK connection/manager types：`src/pulsara_agent/runtime/mcp/sdk.py`。

`event/events.py`只import `primitives.mcp`；primitives不得importHostSession、SDK client、runtime MCP
manager或capability runtime。

### 3.1 Config hard cut

```python
@dataclass(frozen=True, slots=True)
class McpServerConfig:
    server_id: str
    transport: McpTransportConfig
    enabled: bool = True
    required: bool = False

    connect_timeout_ms: int = 10_000
    discovery_timeout_ms: int = 15_000
    startup_deadline_ms: int = 30_000
    refresh_ttl_ms: int = 300_000
    tool_timeout_ms: int = 30_000

    supports_parallel_tool_calls: bool = False
    enabled_tools: tuple[str, ...] | None = None
    disabled_tools: tuple[str, ...] = ()
    default_approval_mode: str | None = None
```

Hard-cut规则：

- 删除`startup_timeout_ms`；
- timeout/TTL均为正整数；
- `startup_deadline_ms >= connect_timeout_ms`；
- deadline约束connect + initialize + required discovery总wall-clock；
- `tool_timeout_ms`只约束tool/resource/prompt执行，不冒充discovery deadline；
- config loader缺失新字段使用上述产品default；
- YAML出现旧字段直接schema error，不alias。

### 3.2 Config identity

```python
class McpConfigSetFact(BaseModel):
    config_epoch: int
    event_safe_config_set_fingerprint: str
    event_safe_server_config_fingerprints: dict[str, str]
    server_ids: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class McpRuntimeConfigIdentity:
    config_epoch: int
    runtime_config_set_fingerprint: str
    runtime_server_config_fingerprints: Mapping[str, str]
```

同时维护两个不同identity：

1. `runtime_config_fingerprint`：process-local，可包含所有实际影响连接的值hash，用于检测secret
   rotation；不持久化、不进入Inspector；
2. `event_safe_config_fingerprint`：durable，只包含canonical non-secret fields、secret source/key name与
   presence，不包含literal header、env value、bearer token或URL query/userinfo。

字段名禁止缩写为`config_fingerprint`，避免process-local与durable identity误用。

第三个identity是`McpServerSnapshot.snapshot_semantic_fingerprint`。它只覆盖protocol/server identity与
规范化tools/resources/templates/prompts/instructions语义；排除snapshot/installation/attempt随机ID、
timing与diagnostics。Prompt Cache、catalog equality与unchanged-server reuse只消费该fingerprint。

`config_epoch`由supervisor单调递增。任何desired config set语义变化都生成新epoch；TTL refresh、retry、
manual refresh可在相同epoch内生成新的per-server attempt与discovery generation。

### 3.3 Lifecycle timing

```python
class McpServerLifecycleTimingFact(BaseModel):
    queued_at_utc: str
    connect_started_at_utc: str | None
    connect_ended_at_utc: str | None
    discovery_started_at_utc: str | None
    discovery_ended_at_utc: str | None
    completed_at_utc: str | None

    connect_duration_seconds: float | None
    discovery_duration_seconds: float | None
    total_duration_seconds: float | None
```

Invariant：

- UTC ISO timestamp required by phase；
- duration finite且非负；
- `STARTING`可没有end；
- terminal snapshot必须有`completed_at_utc/total_duration_seconds`；
- duration由monotonic clock计算，wall-clock只用于显示；
- Inspector不通过当前时间重算历史duration。

### 3.4 Server snapshot hard cut

```python
class McpServerSnapshot(BaseModel):
    snapshot_id: str
    server_id: str
    config_epoch: int
    event_safe_config_fingerprint: str
    snapshot_semantic_fingerprint: str
    reconcile_attempt_id: str
    discovery_generation: int
    status: Literal[
        "disabled", "starting", "ready", "degraded",
        "failed", "needs_auth", "closing", "closed"
    ]
    required: bool

    tools: tuple[McpDiscoveredTool, ...]
    resources: tuple[McpDiscoveredResource, ...]
    resource_templates: tuple[McpDiscoveredResourceTemplate, ...]
    prompts: tuple[McpDiscoveredPrompt, ...]

    protocol_version: str | None
    server_info: McpServerInfoFact | None
    instructions: str | None
    timing: McpServerLifecycleTimingFact
    diagnostics: tuple[McpDiagnosticFact, ...]
```

`config: McpServerConfig`不再整体嵌入event-safe snapshot。执行所需secret/config只留在process-local
manager slot；snapshot使用event-safe config fact。

Status invariant：

- `disabled`：所有discovered collections为空；
- `starting`：collections为空，无execution binding；
- `ready`：connect/discovery complete；可以0 tools；
- `degraded/failed/needs_auth`：没有model-visible tool descriptors；
- `closing/closed`：不进入candidate installation；
- discovery generation在同server的supervisor生命周期内单调增加，不因epoch重置；
- snapshot_id随机；snapshot semantic fingerprint按3.2的canonical catalog payload确定；
- timing/diagnostic变化不改变snapshot semantic fingerprint。

### 3.5 Reconcile ticket与candidate

```python
@dataclass(frozen=True, slots=True)
class McpReconcileTicket:
    ticket_id: str
    config_epoch: int
    event_safe_config_set_fingerprint: str
    trigger: Literal["initial", "config_change", "ttl_refresh", "retry", "manual_refresh"]
    required_server_ids: tuple[str, ...]
    optional_server_ids: tuple[str, ...]
    server_attempts: Mapping[str, McpServerAttempt]
    required_wait_deadline_monotonic: float | None

@dataclass(frozen=True, slots=True)
class McpServerAttempt:
    server_id: str
    reconcile_attempt_id: str
    config_epoch: int
    reserved_discovery_generation: int
    runtime_config_fingerprint: str
    deadline_monotonic: float

@dataclass(slots=True)
class McpServerCandidate:
    ticket_id: str
    config_epoch: int
    reconcile_attempt_id: str
    reserved_discovery_generation: int
    server_snapshot: McpServerSnapshot
    runtime_spec: McpServerRuntimeSpec
    manager_slot: McpManagerSlot | None
```

Ticket、attempt与candidate均为process-local ownership DTO，不进入event log；只有bounded snapshot/install
facts可持久化。

Candidate是process-local ownership object：

- READY candidate持有未安装manager slot；
- failed/disabled candidate不持有manager；
- stale/rejected candidate必须close manager；
- worker只能把candidate交给supervisor queue；
- candidate不能直接访问HostSession wiring。
- `runtime_spec`是process-local canonical config，允许构造descriptor/binding，但不得进入durable event。
- candidate只有在`reconcile_attempt_id`仍等于该server的current desired attempt时才能入队；
- 较早attempt即使同epoch且较晚完成，也必须close并丢弃。

### 3.6 Installed capability snapshot

替换当前`mcp_manager + mcp_bundle`双字段：

```python
@dataclass(frozen=True, slots=True)
class McpInstalledCapabilitySnapshot:
    installation_id: str
    config_epoch: int
    event_safe_config_set_fingerprint: str
    installed_at_utc: str
    snapshots: tuple[McpServerSnapshot, ...]
    descriptors: tuple[CapabilityDescriptor, ...]
    tools: tuple[AsyncTool, ...]
    diagnostics: tuple[CapabilityDiagnostic, ...]
    ready_server_ids: frozenset[str]
```

`RuntimeWiring`改为只保存：

```python
mcp_installation: McpInstalledCapabilitySnapshot | None
```

删除：

```python
mcp_manager
mcp_bundle
```

Process-local `McpExecutionBinding`包含：

- supervisor reference；
- `McpBindingIdentity(server_id, slot_id, snapshot_id, discovery_generation)`；
- original tool name；
- timeout；
- descriptor id/model name。

`McpExecutionBinding`不保存`installation_id`、`origin_installation_id`或global config epoch。整体surface attribution
只存在于`CapabilityExecutionSurface.mcp_installation_id`与RunStart durable fact中；binding不是第二个installation
真源。

执行权限只比较`McpBindingIdentity`。调用时slot/snapshot/generation不可用则fail closed
`mcp_binding_generation_unavailable`，不能路由到“当前同名tool”。无关server更新产生新的installation_id时，
未变化server的slot、snapshot和binding对象原样复用，因此不会误杀其pending request。

所有event-safe nested mapping/list在config → snapshot边界递归freeze；Inspector/JSON边界使用统一recursive
thaw helper，不能把mutable SDK/Pydantic payload引用直接留在installation fact中。

### 3.7 Manager slot lease

safe point不代表所有child execution都已退出。为避免替换installation时关闭仍被subagent或suspended
request借用的manager，V1增加显式slot lease：

```python
@dataclass(slots=True)
class McpManagerSlot:
    slot_id: str
    server_id: str
    config_epoch: int
    runtime_config_fingerprint: str
    snapshot_id: str
    discovery_generation: int
    manager: McpClientManager
    lifecycle: Literal["candidate", "installed", "retiring", "closing", "closed"]
    borrower_count: int

class McpManagerLease:
    slot_id: str
    binding_identity: McpBindingIdentity

@dataclass(frozen=True, slots=True)
class McpPendingLeaseReservation:
    reservation_id: str
    interaction_id: str
    binding_identity: McpBindingIdentity
```

- 每次普通tool call执行前从supervisor acquire lease；MCP resume借用pending owner已有lease；
- retiring slot拒绝新acquire；
- 正在执行的lease在finally释放；
- installation swap把old slot标成retiring，不直接close；
- affected child先safety-cancel并drain；
- borrower_count归零后supervisor才close old slot；
- close/shutdown等待所有lease释放，超时是`McpDrainError`；
- lease不是durable DTO，durable事实是binding/installation identity。

普通tool call的lease在terminal result持久化后释放。若调用返回MCP input-required/elicitation suspended，
lease ownership从executor原子转移给pending interaction owner，并一直保持到resume成功、denial、cancel、stop
或session close的terminal路径；不得在suspend返回时释放。

V1必须实现以下process-local supervisor API，不能把Python lease塞进可序列化pending payload：

```python
def promote_lease_to_pending(
    lease: McpManagerLease,
    interaction_id: str,
) -> McpPendingLeaseReservation: ...

def confirm_pending_lease(
    interaction_id: str,
    reservation_id: str,
) -> None: ...

def abort_pending_lease(
    interaction_id: str,
    reservation_id: str,
) -> None: ...

def borrow_pending_lease(
    interaction_id: str,
    binding_identity: McpBindingIdentity,
) -> McpManagerLease: ...

def complete_pending_lease(interaction_id: str) -> None: ...
```

supervisor内部维护`pending_leases_by_interaction_id`，并冻结以下顺序：

1. executor以普通call lease发起MCP请求；
2. 收到suspended结果后，先`promote_lease_to_pending()`取得reservation；
3. runtime提交suspension event与pending state；
4. commit acknowledgement后调用`confirm_pending_lease()`；
5. pending创建、序列化或durable commit失败时调用`abort_pending_lease()`，原lease必须归还；
6. resume通过`borrow_pending_lease()`借用原pending-owned slot，禁止重新对current/retiring slot执行
   `acquire()`；
7. terminal resume/denial/cancel/stop的result fact提交确认后调用`complete_pending_lease()`；
8. session close必须先terminalize pending interaction，再complete lease；若无法提交terminal fact，则close受阻并保留
   ownership供重试。

`borrow_pending_lease()`必须同时校验`interaction_id`与完整`McpBindingIdentity`；不允许按server id或tool name
模糊匹配。reservation/confirm两阶段用于避免“lease已转移、pending fact却未提交”的永久占用。

Child不持有child-lifetime lease。每次真实MCP call仍按调用持lease；如果该child profile引用的server slot发生
更新、撤销或进入retiring，parent必须以`subagent_mcp_binding_generation_changed`取消并drain该child。
无关server更新因复用原slot，不影响child。

## 4. Supervisor Ownership 与状态机

### 4.1 唯一owner

`HostSession.mcp_supervisor`是以下资源唯一owner：

- desired config epoch；
- per-server background tasks；
- candidate queue；
- pending manager slots；
- installed manager slots；
- retry/backoff/TTL state；
- close attempt。

RuntimeWiring、AgentRuntime、McpCapabilityTool只持borrowed binding，不负责close manager。

### 4.2 Supervisor API hard cut

```python
class McpServerSupervisor:
    def prepare(
        self,
        configs: tuple[McpServerConfig, ...],
        *,
        trigger: McpReconcileTrigger,
    ) -> McpReconcileTicket: ...

    async def await_required(
        self,
        ticket: McpReconcileTicket,
    ) -> McpRequiredStartupResult: ...

    def drain_installable_candidates(
        self,
        *,
        expected_epoch: int,
    ) -> McpCandidateBatch: ...

    def current_starting_snapshots(self) -> tuple[McpServerSnapshot, ...]: ...

    async def aclose(self, *, timeout_seconds: float) -> None: ...
```

删除production：

```python
await sync_servers(configs)
```

不得保留“内部再调用prepare+await所有server”的compatibility implementation。

`prepare()`为每个server在调度时保存绝对monotonic deadline。ticket的
`required_wait_deadline_monotonic`等于所有required server deadline的最大值；`await_required()`不接受
caller自定义timeout。任一required server到达自己的deadline即立即使ticket失败，不等待其他server的
较晚deadline。

### 4.3 Per-server状态机

```text
DISABLED

STARTING_CONNECT
  -> STARTING_DISCOVERY
  -> READY_CANDIDATE
  -> INSTALLED_READY

STARTING_CONNECT / STARTING_DISCOVERY
  -> FAILED | NEEDS_AUTH | DEGRADED
  -> RETRY_WAIT
  -> STARTING_CONNECT

任何非terminal状态
  -> CLOSING
  -> CLOSED
```

`READY_CANDIDATE`与`INSTALLED_READY`必须区分：worker ready不等于模型已看到tool。

### 4.4 Config epoch线性化

`prepare()`在supervisor同步临界区完成：

1. canonicalize config set；
2. 比较process-local runtime fingerprint；
3. changed则epoch += 1；
4. 标记old tasks/candidates stale；
5. disable/remove生成immediate non-network candidate；
6. 对每个new/changed/due server原子预留新的`reconcile_attempt_id`与
   `reserved_discovery_generation`；
7. 将其写入`current_desired_attempt_by_server`后再创建owned task；
8. 返回ticket。

worker completion提交时再次检查：

```text
candidate.config_epoch == supervisor.current_epoch
and supervisor.lifecycle == OPEN
and candidate.reconcile_attempt_id == current_desired_attempt_by_server[server_id]
and candidate.reserved_discovery_generation == reserved generation
and server runtime_config_fingerprint仍匹配
```

不匹配：close candidate manager，记录`mcp_stale_candidate_discarded` process diagnostic，不入install queue。
因此同一epoch内slow TTL refresh/retry/manual refresh也不能覆盖newer attempt。

### 4.5 Lock规则

- supervisor owner event loop上的同步临界区内禁止await network、SDK close或manager discovery；
- 同步临界区内只变更ownership map、epoch、queue、slot lifecycle、borrower count与task references；
- close/cancel在锁内detach ownership，锁外await；
- per-server worker只拥有自己的manager slot；
- `_run_lock`由HostSession safe-point caller在外层await取得；取得后不得再await supervisor `asyncio.Lock`；
- `supervisor.commit_slot_transition(plan)`是owner event loop上的同步、无await方法，并构成slot lifecycle与surface
  pointer的线性化边界；
- `acquire_binding_lease()`、pending lease promotion/borrow/complete及borrower count增减同样是同步线性化操作；
- 若SDK callback或close入口可能来自其他线程，supervisor内部只允许使用独立`threading.RLock`保护这些短状态
  mutation，再把异步drain调度回owner loop；commit block禁止获取`asyncio.Lock`；
- candidate drain是短同步操作。

## 5. SDK Connect / Discovery Hard Cut

### 5.1 拆分API

当前`SdkMcpClientManager.start()`同时connect+discover。改为：

```python
connection = await SdkMcpConnection.connect(
    config,
    timeout_seconds=config.connect_timeout_ms / 1000,
)
snapshot = await discover_mcp_server(
    connection,
    config=config,
    timeout_seconds=config.discovery_timeout_ms / 1000,
)
manager = SdkMcpClientManager.from_connected_server(
    connection=connection,
    snapshot=snapshot,
)
manager_slot = McpManagerSlot(
    slot_id=f"mcp_slot:{uuid4().hex}",
    server_id=config.server_id,
    config_epoch=attempt.config_epoch,
    runtime_config_fingerprint=attempt.runtime_config_fingerprint,
    snapshot_id=snapshot.snapshot_id,
    discovery_generation=attempt.reserved_discovery_generation,
    manager=manager,
    lifecycle="candidate",
    borrower_count=0,
)
```

slot只装入实现`McpClientManager`的per-server SDK facade；不得把raw connection/snapshot以未声明字段塞入
slot。

connect success但discovery failure必须close connection，除非明确生成DEGRADED candidate且不暴露tools；V1选择
close并交由retry，减少半活manager状态。

### 5.2 Capability-aware discovery

initialize response声明的server capabilities是真源：

- tools capability存在才调用`tools/list`；
- resources capability存在才调用`resources/list`和`resources/templates/list`；
- prompts capability存在才调用`prompts/list`；
- 不再通过请求失败探测未声明optional method。

如果server声明capability但method失败：

- required server：startup失败/degraded，不能READY；
- optional server：DEGRADED candidate，无tool binding，按backoff重试；
- diagnostic使用stable code和redacted message。

### 5.3 并发

- 不同server worker并发；
- 同server discovery中的independent list methods可在同一个overall discovery deadline内并发；
- pagination每method内部保持顺序；
- `max_pages/max_items`保留hard cap；
- 任一required declared method失败使server不READY；
- cancellation向所有child list tasks传播并drain。

### 5.4 Deadline

每server使用绝对monotonic deadline：

```text
startup_deadline
  covers connect + initialize + declared discovery + local normalization
```

内部阶段timeout取：

```python
min(configured_phase_timeout, remaining_startup_deadline)
```

不得在每阶段重新获得完整deadline。

## 6. HostCore Session Open 改造

### 6.1 新顺序

`_open_session_with_runtime_id()`冻结为：

```text
resolve workspace
-> reserve HostSession identity
-> attach terminal lease
-> load/canonicalize MCP configs（local filesystem only）
-> create McpServerSupervisor
-> supervisor.prepare(initial configs)
-> construct immediate STARTING/DISABLED installation（no tools）
-> build base runtime wiring with that installation
-> construct unpublished HostSession
-> await required servers only
-> drain/install current required candidates locally
-> upsert manifest
-> publish HostSession under lifecycle lock
-> optional workers continue in background
```

允许base runtime在required wait前构造，因为它不包含remote descriptors/bindings。session只有在required成功后才
publish；required initialization失败时不得写open manifest。manifest只在required installation成功后、registry
publish前upsert。若manifest已写但publish失败，registry先把当前reservation原子转换为manifest-close tombstone，
再幂等`mark_closed()`；finalization失败时tombstone继续阻止list/resume与identity复用，后续explicit close/shutdown
重试。失败路径关闭runtime、supervisor、terminal lease与reservation/tombstone ownership。

### 6.2 Required blocking语义

`required=true` server必须在deadline前得到READY candidate并成功安装。

以下均使open失败：

- connect timeout；
- initialize/protocol error；
- NEEDS_AUTH；
- declared discovery failure；
- descriptor/binding bundle invariant失败；
- required candidate在install前因epoch变化失效；
- HostCore进入CLOSING。

错误：

```python
class McpRequiredStartupError(RuntimeError):
    server_ids: tuple[str, ...]
    reason_code: str
    diagnostics: tuple[McpDiagnosticFact, ...]
```

message不含secret。CLI展示server id、stage、elapsed、stable reason。

### 6.3 Optional open语义

- prepare后立即继续；
- initial installation含STARTING diagnostic但无tool；
- REPL banner可显示`MCP servers: docs=starting`；
- background失败不关闭session；
- background ready不会mid-run突变。

## 7. HostSession Safe-Point 安装

### 7.1 删除旧sync

删除 `_sync_mcp_servers_for_turn()`，替换为两个动作：

```python
def _schedule_mcp_reconcile_at_safe_point(self) -> None: ...
async def _apply_mcp_candidates_at_safe_point(self, state: LoopState | None) -> None: ...
```

第一项只读取local config、compare fingerprint并schedule task。optional attempt从不阻塞；如果返回的ticket
含新的required attempt，host action必须先`await_required(ticket)`，再进入run创建/恢复。第二项只drain
process-local candidate并原子换installation。

### 7.2 Session open后的required语义

`required=true`在session生命周期内始终表示“本次host action开始前必须READY”，不只约束initial open。

safe point发现以下任一情况时创建新的required attempt并bounded await：

- 新增required server；
- optional改为required；
- required server endpoint/transport/auth/config变化；
- required snapshot TTL到期；
- required server进入retry due。

若同runtime identity的required retry attempt已由后台timer启动且仍在运行，safe point不得创建第二个attempt；新ticket
必须引用并join该exact attempt及其原absolute deadline。`await_required()`复用现有worker等待，不能因为本ticket没有
新reserve就立即判required unavailable。

等待期间worker仍由supervisor拥有。结果语义：

- READY：安装candidate，然后创建/恢复run；
- timeout/FAILED/NEEDS_AUTH/DEGRADED：本次host action抛
  `McpRequiredGenerationUnavailable`，不继续model call；
- new turn失败时不得写RunStart；
- suspended resume遇到**无关required server**不可用时，原pending interaction、state与binding lease保持不变，
  host action可重试；
- suspended resume遇到**同配置TTL/retry暂时失败**，但原leased binding仍为有效desired identity时，同样保留
  pending/state/lease，不静默切换generation；
- pending自身binding所属server被disable/remove，或runtime config reconfigure使原binding identity不再desired时，
  不适用上述保留规则：必须提交typed terminal denial/error result，确认提交后清pending并释放lease；
- session进入`required_mcp_blocked`可观测状态，后续host action继续fail closed，直到config修复或required
  generation READY；
- 新epoch被prepare时，旧slot对象在commit前仍保持`installed`物理lifecycle，避免把network wait误当成installation
  transition；但`acquire_binding_lease()`同时校验current desired runtime fingerprint，因此旧generation立即拒绝
  新acquire。已有borrower/pending lease保留到commit后的targeted cancellation/terminalization；
- disable/remove required config表示该server不再desired，按撤销处理，不再等待。

caller不传timeout；使用ticket内每server绝对deadline。

### 7.3 Safe point定义

V1允许安装的点：

1. session publish前的initial required installation；
2. new `run_turn/stream_turn`取得`_run_lock`后、创建run snapshot前；
3. approval/plan/MCP resume取得`_run_lock`后、恢复model loop前；
4. run finalize后、释放`_run_lock`前，可安装但只影响下一run；
5. manual host refresh API取得`_run_lock`且无active run/pending execution时。

worker completioncallback不是safe point。

### 7.4 Installation算法

在`_run_lock`内：

1. local config reload并`prepare()`；
2. `drain_installable_candidates(expected_epoch=current_epoch)`；
3. 从旧installation与candidate batch构造完整new installation；
   - 只有accepted current-attempt candidate可以替换对应server；
   - 未出现在candidate batch中的server原样复用旧slot/snapshot/binding；
   - 只有surface semantic payload与全部slot identity均未变化时，才复用原`installation_id`；任一surface语义
     或任一slot identity发生变化，都必须生成新的`installation_id`；
4. 验证descriptor name唯一；
5. 验证每个descriptor有且仅有一个binding；
6. 对每个binding验证：
   - `McpBindingIdentity`精确对应supervisor当前拥有的slot id、server id、snapshot id与discovery generation；
   - descriptor id/model name/original tool name与该binding一致；
   - binding不携带、也不校验global installation id或global config epoch；
7. 预构造完整immutable `CapabilityExecutionSurface`与stable-id pending audit record；surface同时包含
   RuntimeWiring installation、CapabilityRuntime、ToolExecutor、extra bindings与installation identity；
8. 计算changed/retiring binding identities、revoked server/tool sets及受影响child ids；
9. 完成全部可能失败的validation与allocation后，调用同步
   `supervisor.commit_slot_transition(plan)`进入一个**无await commit block**：
   1. 将affected old slots标记为`retiring`，从此拒绝任何新acquire；这是执行边界的线性化点；
   2. 将accepted candidate slots标记为`installed`并注册进supervisor ownership；
   3. 单指针替换prebuilt `CapabilityExecutionSurface`，从而同时切换RuntimeWiring、CapabilityRuntime、
      ToolExecutor与extra bindings；
   4. 发布process-local installation identity并登记pending audit record；
10. commit block完成后，才允许await safety-cancel/drain受影响child与terminalize受影响pending execution；
11. borrower归零后关闭retiring slots。

`_run_lock`已由外层safe-point caller持有；commit block不再获取第二把`asyncio.Lock`。
`commit_slot_transition(plan)`与lease acquire都在supervisor owner event loop同步执行，并在需要跨线程保护时共享同一
短生命周期`threading.RLock`。锁内禁止network、SDK close、event append或任何await。child不持有parent
`_run_lock`，但其lease acquire必须经过同一同步slot-state线性化边界，因此不可能在“surface已换、old slot尚未
retiring”的窗口取得旧lease。
old与candidate slot对象从candidate drain到close完成始终由同一个supervisor registry拥有；commit block只改变
lifecycle与surface引用，不转移manager close ownership，因此任意中间phase都能由close枚举并收口全部资源。

若步骤3–8失败：

- old installation保持不变；
- candidate manager close；
- 写structured diagnostic；
- required config change导致的安装失败使本次host action fail closed；
- optional failure不破坏已有installation。

commit block的异常语义必须硬切：

- 在第一个slot lifecycle transition之前抛错：没有可见mutation，按上述pre-commit failure处理；
- 一旦第一个old slot进入`retiring`，禁止尝试局部rollback；session原子设置
  `mcp_installation_reconciliation_required=True`，阻止新run、resume和新MCP lease，保留old/candidate slots与
  当前surface pointer及pending audit ownership供inspect与close；
- V1不提供`reconcile_mcp_installation()`或任何live repair authority；该latch在当前HostSession生命周期内不可清除；
- latch后只允许bounded inspect/status与close，不允许继续installation、补写installation audit、恢复old slot为
  installed、完成new surface安装或选择任一侧作为canonical winner；
- HostSession必须走bounded close：停止run/subagent，拒绝新lease，并关闭supervisor实际拥有的全部candidate、
  installed、retiring与closing slots；close不得依赖surface pointer与pending audit彼此一致；
- close成功后，用户/host重新打开session，由当前config构造全新的supervisor、slot registry与installation；不得
  复用faulted session的process-local slot/surface/audit ownership；
- close/drain失败继续遵循第9节的retryable close ownership，不能为了重开而丢弃faulted session；
- production commit block应只含预先分配对象的字段/引用替换，因而中途异常属于architecture fault，必须有
  deterministic fault-injection test。

### 7.5 Subagent safety narrowing

如果ready server/tool被撤销：

- parent下一run exposure不再包含它；
- active child若继承了被撤销MCP snapshot，调用现有safety narrowing cancellation；
- reason code固定`subagent_mcp_binding_generation_changed`；
- reason message只描述binding generation已变化；old/new installation与changed binding集合由parent
  installation audit和ChildExecutionRegistry exact identity index join，不在每个cancel event重复整批payload；
- background ready若只新增server，不影响已运行child；child没有lifetime manager lease；
- child profile引用的existing server slot发生替换/撤销时，明确取消并drain该child；
- capability撤销时先阻止新borrower，再取消/drain受影响child，最后关闭retiring manager。

child spawn时，必须把其冻结profile引用的`frozenset[McpBindingIdentity]`注册到
`ChildExecutionRegistry`。registry维护反向索引：

```python
child_ids_by_mcp_binding_identity: Mapping[McpBindingIdentity, frozenset[str]]
```

installation pre-commit planning用changed/retiring binding identities查询该索引，得到受影响child ids；commit
block先retire slot，随后锁外按该集合cancel/drain。child terminal drain后删除正向与反向索引。禁止根据descriptor
name、tool name或“当前capability surface”反推child是否受影响，因为这些视图在swap后已经代表new generation。

### 7.6 Suspended MCP interaction

pending payload必须记录：

- installation_id（audit attribution only）；
- server_id；
- `mcp_binding_identity`，其中包含slot id、server snapshot id与discovery generation；
- original request/timing seed。

resume时：

- 通过`borrow_pending_lease(interaction_id, binding_identity)`借用pending owner已有lease；禁止重新acquire slot；
- pending owner仍持有同一slot lease，且snapshot/generation仍是desired identity：允许resume；
- unrelated required server不可用：不产生terminal result，保留pending/state/lease并返回retryable host error；
- 同配置TTL/retry失败，但原leased binding仍有效：保留pending/state/lease，不切换generation；
- pending binding所属config disable/remove/reconfigure：fail closed，产生typed denied/error tool result；
- 不允许按server id/tool name转发给新generation；
- 非相关server更新只改变installation_id，不改变slot identity，因此不影响resume；
- terminal resume/denial/cancel/stop必须在result fact提交确认后调用`complete_pending_lease()`；提交失败时保留
  pending lease和可重试terminalization ownership。
- terminal ToolResult已commit但publication observer失败时，从committed event slice/ledger折叠LoopState，完成同一
  pending lease并允许retiring slot关闭，再向上传播publication failure；不得留下无主lease或可重复resume的pending。

## 8. Lifecycle Audit 与 Inspector

### 8.1 Durable carrier

新增typed：

```python
class McpReconcileAttemptSummaryFact(BaseModel):
    server_id: str
    reconcile_attempt_id: str
    reconcile_trigger: Literal["initial", "config_change", "ttl_refresh", "retry", "manual_refresh"]
    attempt_status: Literal["scheduled", "running", "ready", "degraded", "failed", "needs_auth", "disabled"]
    retry_attempt: int
    request_count: int
    page_count: int
    cache_outcome: Literal[
        "not_applicable",
        "miss",
        "ttl_fresh_reuse",
        "config_fingerprint_reuse",
        "sdk_response_cache_hit",
    ]
    stale_candidates_discarded_since_previous_install: int

class McpInstalledServerSnapshotFact(BaseModel):
    server_id: str
    status: McpServerStatus
    required: bool
    changed_in_this_installation: bool
    attempt: McpReconcileAttemptSummaryFact
    snapshot_id: str
    discovery_generation: int
    event_safe_config_fingerprint: str
    snapshot_semantic_fingerprint: str
    protocol_version: str | None
    tool_count: int
    resource_count: int
    resource_template_count: int
    prompt_count: int
    instructions_chars: int
    lifecycle_timing: McpServerLifecycleTimingFact
    diagnostics: tuple[McpDiagnosticFact, ...]
    catalog_artifact_id: None  # V1 fixed; field reserved but archive production disabled

class McpCapabilitySnapshotInstalledEvent(EventBase):
    installation_id: str
    previous_installation_id: str | None
    config_epoch: int
    event_safe_config_set_fingerprint: str
    installation_triggers: tuple[
        Literal["initial", "config_change", "ttl_refresh", "retry", "manual_refresh"],
        ...,
    ]
    coalesced_installation_count: int
    coalesced_attempt_summaries: tuple[McpReconcileAttemptSummaryFact, ...]  # max 64
    coalesced_attempt_summaries_omitted: int
    server_snapshots: tuple[McpInstalledServerSnapshotFact, ...]  # bounded by configured server count cap
    total_installed_tool_count: int
    added_tool_count: int
    revoked_tool_count: int
    changed_tool_names_bounded: tuple[str, ...]  # max 64
    changed_tool_names_omitted: int
    diagnostics: tuple[McpDiagnosticFact, ...]
```

`installation_triggers`是本批`changed_in_this_installation=True`的`server_snapshots[*].attempt.reconcile_trigger`
与`coalesced_attempt_summaries[*].reconcile_trigger`的去重、稳定排序集合；一次safe point可以同时安装
config change、retry与TTL refresh candidate，不允许用单个顶层trigger抹平来源。所有count必须非负，
`retry_attempt/request_count/page_count/stale_candidates_discarded_since_previous_install`使用bounded integer；
达到cap时保存cap与omitted/overflow diagnostic，而不是继续增长event payload。stale candidate本身不会进入
installation，但supervisor从上一条installed audit之后累计的bounded discard summary必须归入对应server fact。
`installation_triggers`不得为空，且必须与本次changed server facts及coalesced summaries的trigger union完全一致。
原样复用的
server fact保留其原始attempt provenance，但设置`changed_in_this_installation=False`，不污染本批trigger集合。
零MCP配置使用schema内建的canonical empty installation sentinel，不写installed event，因此不存在“空server facts
但强造trigger”的例外。

`cache_outcome`只描述attempt实际采用的缓存/复用路径：`not_applicable`表示本attempt不经过cache判定，`miss`
表示检查后未命中，其余三个值分别表示TTL freshness、相同config fingerprint与SDK response cache命中。禁止用
一个boolean把“未检查”与“检查但miss”混为一谈。
`ttl_fresh_reuse/config_fingerprint_reuse/sdk_response_cache_hit`均要求network `request_count=0`且`page_count=0`；
`page_count <= request_count`。若未来SDK能在本地cache hit前仍发validation request，必须新增枚举值，不能悄悄改变
现有值的含义。
`McpInstalledServerSnapshotFact.attempt.server_id`必须等于外层`server_id`；coalesced summaries因没有外层server carrier而
自行携带server id。

`total_installed_tool_count`是new installation的当前总量；`added_tool_count`与`revoked_tool_count`是相对
`previous_installation_id`的本次delta，三者不得互相替代。

Audit event不得重复嵌入完整tools/resources/prompts/instructions catalog。V1不实现catalog archive，完整catalog
只存在于process-local installation，`catalog_artifact_id`字段required但固定为`None`，validator拒绝非空值。
未来若实现archive，必须在producer PR中先完成bounded artifact写入，再提交携带artifact id的event；Inspector PR
不能事后补写历史artifact。diagnostic数量、message长度和changed tool names均有hard cap，超出只记录
count/omitted。

Event只在有AgentEvent context的safe point写入：

- new run：HostSession把audit放入state pending facts；AgentRuntime必须把`RunStartEvent`与本run首次引用的全部
  pending installed events通过一次`RuntimeSession.emit_many()`原子批写；
- resume：复用原run context，把pending audit作为独立原子batch提交，在继续model call前确认；
- initial required installation若尚无run，保留bounded pending audit，首个run写入；
- required startup导致session open失败：通过typed `McpRequiredStartupError`返回，不伪造run event。

`RunStartEvent`增加两个required non-null字段：

```python
mcp_installation_id: str
mcp_installation_owner_runtime_session_id: str
```

parent run的owner runtime session是自身；subagent child run使用独立RuntimeSession/EventLog，但MCP installation
仍由parent HostSession拥有，因此child必须写parent runtime session id。child ledger不复制parent installed event，
Inspector通过现有`EventLogLocator`以
`(mcp_installation_owner_runtime_session_id, mcp_installation_id)`跨session join。找不到owner ledger或对应audit时
报告stable dangling-installation diagnostic，不从child当前surface或live supervisor猜测。

每个run的`mcp_installation_id`即使没有MCP也引用canonical empty installation；canonical empty sentinel由event
schema/Inspector内建，不要求installed event。对于其他尚未durable audit的installation，**owner runtime session的
parent run**唯一合法写入顺序是：

```python
stored = await runtime_session.emit_many(
    (
        RunStartEvent(
            ...,
            mcp_installation_id=current_installation_id,
            mcp_installation_owner_runtime_session_id=runtime_session_id,
        ),
        *pending_mcp_installation_audit_events,
    ),
    state=state,
)
```

parent必须先完成该原子batch的commit acknowledgement，才能spawn继承该installation的child；child随后只写
自己的RunStart attribution，不重复提交audit。

EventLog必须把整个batch作为连续sequence的原子commit：任一validation/append失败时整批不可见，不得留下引用
缺失audit的半启动run。只有整批commit acknowledgement后，runtime才可清pending audits、发布RunStart、把run
标记active或进入context compile/model call。若current installation已被历史audit覆盖，batch只含RunStart。

commit acknowledgement以`EventWriteResult.committed_events`为准，而不是以observer publication成功为准。
`emit_many()`若因observer失败抛`EventPublicationAfterCommitError`，RunStart与resume路径必须从异常的committed slice
acknowledge已提交audit；observer failure仍向上传播，但不得把canonical commit误判成pre-commit失败或把stable
audit event id留给下一run重复写入。

resume已有原RunStart，不能重写它；resume safe point安装的pending audits作为独立`emit_many()` batch提交，
acknowledgement前不得continuation。失败时保持原pending run/state/lease并遵循RuntimeSession reconciliation语义。

pending audit规则：

- audit预生成stable event id；
- 只在包含该audit的`RuntimeSession.emit_many()`收到commit acknowledgement后从pending queue删除；
- commit失败或结果不确定时不进入model call，按RuntimeSession reconciliation语义处理；
- run finalize safe point安装的新surface不写入已结束run，留给下一run；
- 多个从未被任何run使用、也未durable commit的intermediate installations可以coalesce；被RunStart引用过或已经
  durable的installation不得coalesce/delete；
- coalesce不是“只保留latest event对象”：必须重新构造一个新的stable-id pending audit，使
  `previous_installation_id`直接指向最近一条已durable installation（没有则为`None`），并设置
  `coalesced_installation_count`；
- rebuilt audit使用latest完整server snapshot作为current state，同时把被折叠installation的changed attempt
  provenance并入bounded `coalesced_attempt_summaries`，重算`installation_triggers`、added/revoked delta与changed
  tool summary；超出cap写`coalesced_attempt_summaries_omitted`；
- 被coalesce的旧pending event ids与intermediate installation ids不得再作为任何pending event的
  `previous_installation_id`；因此historical chain永远从latest pending直接连到last durable；
- pending queue有明确上界；到达上界时停止安装新optional candidate，而不是丢audit。

event中的snapshot是bounded event-safe DTO，不含manager/config secret或完整server-controlled catalog。

### 8.2 Live Inspector shape

```json
{
  "mcp": {
    "installation_id": "mcp_installation:...",
    "config_epoch": 4,
    "event_safe_config_set_fingerprint": "sha256:...",
    "background_tasks": {
      "running": 1,
      "closing": 0
    },
    "servers": [
      {
        "server_id": "docs-langchain",
        "status": "starting",
        "required": false,
        "changed_in_this_installation": true,
        "attempt": {
          "server_id": "docs-langchain",
          "reconcile_attempt_id": "mcp_attempt:...",
          "reconcile_trigger": "ttl_refresh",
          "attempt_status": "running",
          "retry_attempt": 0,
          "request_count": 0,
          "page_count": 0,
          "cache_outcome": "miss",
          "stale_candidates_discarded_since_previous_install": 1
        },
        "snapshot_id": "mcp_snapshot:...",
        "snapshot_semantic_fingerprint": "sha256:...",
        "discovery_generation": 0,
        "installed": false,
        "timing": {
          "connect_duration_seconds": null,
          "discovery_duration_seconds": null
        },
        "diagnostics": []
      }
    ]
  }
}
```

Historical Inspector从installed events读取，不查询当前manager、不重新discover、不重算elapsed。

### 8.3 CLI

REPL banner/status至少区分：

```text
MCP servers: docs=starting, required-db=ready (3 tools)
```

background transition不异步打印干扰用户输入；`:status`或下一turn可见。未来可增加非侵入notification，V1
不做。

## 9. Close / Shutdown / Rollback

### 9.1 Supervisor close状态机

```text
OPEN
  -> CLOSING(attempt_id)
  -> CLOSED

CLOSING timeout/error
  -> OPEN_WITH_CLOSE_PENDING（保留retry ownership）
```

共享close attempt使用Future承载结果；并发waiter观察同一success/error，不能只等Event。

```python
@dataclass(slots=True)
class McpCloseAttempt:
    attempt_id: str
    result: asyncio.Future[None]
    supervisor_identity: str
```

`begin_close()`在同一supervisor临界区内创建或加入attempt；owner与waiter都await同一个Future。owner必须以
`BaseException`收口，在success、ordinary exception与cancellation三条路径上解析Future并按attempt identity条件
释放ownership。失败attempt完成后可创建new retry attempt；旧owner/finally不得删除或覆盖new attempt。HostSession/
HostCore的shared close attempt必须传播同一个`McpDrainError`，不能让并发waiter把owner failure误判为成功。

### 9.2 Close算法

1. 在supervisor同步状态临界区内：标记CLOSING、epoch递增使future completion stale、detach
   task/candidate/manager ownership；不得await `asyncio.Lock`；
2. 锁外cancel worker tasks；
3. bounded await tasks；
4. close pending candidates；
5. installed/retiring slots拒绝新lease acquisition；
6. 等待所有per-call与pending-owner leases释放；
7. close installed/retiring manager slots；
8. drain SDK owner tasks、HTTP clients、stdio processes；
9. 全部成功才CLOSED；
10. timeout/error保留retryable closing ownership并抛`McpDrainError`。

步骤2–8任一deadline耗尽时，supervisor不得进入`CLOSED`；仍在运行的worker/SDK close可以继续后台收口，但其
ownership必须继续属于原supervisor，retry close加入/创建受控attempt并检查实际资源状态。不得通过清空registry、
替换supervisor或释放HostSession来“解决”超时。

不得吞掉`CancelledError`后假装closed；SDK内部cancel可转换，但必须确认owner task和transport已退出。

### 9.3 HostSession / HostCore

- HostSession.aclose先停止run/subagent，再close MCP supervisor，再runtime local close；
- MCP drain失败是destructive teardown blocker；
- HostCore不得释放terminal lease、删除session/workspace；
- retry close继续同一supervisor ownership；
- rollback open失败也必须drain supervisor；
- RuntimeWiring不再单独close MCP manager。

## 10. Retry / Refresh / TTL

### 10.1 Initial retry

- required startup在单次open deadline内不做无界retry；
- 可做至多一次立即retry，仅限明确transient connect错误且remaining deadline足够；
- optional按exponential backoff后台retry；
- retry deadline由supervisor-owned bounded timer task等待；到期后以`trigger="retry"`预留新attempt，worker只产
  candidate，HostSession safe point仍是唯一installation边界；
- backoff state属于server+global config epoch；任一config-set epoch变化取消旧epoch timer并清零计数，server删除
  同样清理。未变化且已READY的slot仍可复用，不因global epoch变化强制rediscovery。

### 10.2 TTL refresh

- READY snapshot记录`discovery_ended_at`与`refresh_due_monotonic`；
- safe point仅在due时schedule refresh；
- unchanged/not due不发network request；
- refresh worker使用新candidate manager/connection，不原地修改installed snapshot；
- install成功后swap；失败则按policy生成DEGRADED candidate。

V1选择fail-closed projection：DEGRADED candidate安装后撤销该server descriptors。原manager在swap后关闭。

### 10.3 Config change

- disable/remove：safe point立即安装DISABLED/removal，不等待worker；
- endpoint/transport/auth source/tool allowlist变化：new epoch，oldworker stale；
- optional server发生runtime config变化且replacement尚未READY时，safe point立即安装无tool的STARTING projection并
  retire旧slot；本次run不得继续暴露一个已不再desired、执行时只会fail的旧descriptor；同配置TTL refresh则继续复用
  旧READY slot直到candidate可安装；
- permission hint/tool filtering变化同样new epoch；
- literal secret rotation通过process-local fingerprint触发，但durable fingerprint不泄密。

## 11. PR 实施顺序

M0–M4必须是可独立运行、独立全绿的纵向PR，不是把schema producer与consumer拆开的半迁移。具体冻结
如下。

### M0：Contract characterization 与新 primitives（additive）

本PR不删除旧production字段，不改变启动行为；只建立下一PR需要的低层类型、故障fixture和architecture
tests，因此可以独立合并。

修改：

- `primitives/mcp.py`；
- `runtime/mcp/types.py`；
- config/schema characterization tests；
- fake slow/hanging/capability-aware MCP servers；
- contract migration checklist。

内容：

- event-safe identity/timing/snapshot fact；
- process-local attempt/candidate/slot/lease types；
- secret fingerprint fixtures；
- current sync behavior baseline test；
- old contract consumers清单。

M0中event-safe primitives落在`primitives/mcp.py`；attempt/candidate/slot/lease等process-local ownership DTO落在
`runtime/mcp/types.py`，以additive类型存在且不替换旧production path。M1完成纵向迁移后，这些DTO成为唯一生产
类型并删除旧别名/constructor。

验收：

- 新DTO validators与recursive freeze/thaw tests；
- same-epoch attempt race fixture；
- required/optional deadline fake clock；
- M0后production仍全绿，且尚未声称旧contract已删除。

### M1：Config / snapshot / installation / SDK 纵向 schema hard cut

本PR一次迁移所有production consumer，不能只改`types.py`。

修改：

- `runtime/mcp/types.py`、`store.py`、`sdk.py`、`stdio.py`、`manager.py`；
- `runtime/mcp/supervisor.py`现有同步路径；
- `capability/providers/mcp.py`、`tools/adapters/mcp.py`；
- `runtime/wiring.py`、`host/session.py` close owner；
- direct CLI doctor/reconnect及其tests；
- 本PR拥有的长期contracts。

内容：

- 删除`startup_timeout_ms`并迁移所有读取者；
- 删除`McpServerSnapshot.config`并迁移provider/CLI/Inspector读取者；
- connect/discovery拆分与per-server `McpClientManager` facade；
- capability-aware discovery、absolute deadline；
- 新`McpInstalledCapabilitySnapshot`替换RuntimeWiring的manager+bundle双字段；
- 当前Host open/turn可暂时保持同步行为，但只能消费新installation contract；
- direct CLI不再调用`SdkMcpClientManager.start()`。

本PR同步更新并以测试约束：

- `contracts/MCP_CAPABILITY_CONTRACT.zh.md`；
- `contracts/CAPABILITY_SURFACE_CONTRACT.zh.md`；
- `contracts/APP_SETTINGS_CLI_ENTRY_CONTRACT.zh.md`的direct CLI config语义。

验收：

- 不支持prompts不调用prompts/list；
- direct CLI使用同一SDK facade与deadline；
- descriptor/binding在同一frozen execution surface内按slot identity配对；未变化server允许复用旧binding对象；
- 所有旧config/snapshot constructor编译或schema失败；
- M1结束时没有producer/consumer跨schema版本。

### M1–M3 长期contract所有权矩阵

长期contract只能与其约束的producer、consumer和durable schema在同一个可独立全绿的PR中迁移，禁止提前写入
尚未落地的future contract：

| PR | 本PR拥有并必须同步更新的长期contract | 所有权理由 |
|---|---|---|
| M1 | `MCP_CAPABILITY_CONTRACT`、`CAPABILITY_SURFACE_CONTRACT`、direct CLI对应的`APP_SETTINGS_CLI_ENTRY_CONTRACT` | M1已经完成config、snapshot、installation、SDK facade与descriptor/binding schema纵向hard cut |
| M2 | `EVENT_LOG_STORAGE_CONTRACT`、`HOST_RESUME_CONTRACT`、`AGENT_RUNTIME_LOOP_CONTRACT` | RunStart installation owner、RunStart/audit `emit_many()`、pending lease与run safe-point行为都在M2首次成为production truth |
| M3 | `INSPECTOR_PROJECTION_CONTRACT` | M3只消费M2已经durable commit的facts并定义normalized projection，不反向生产或修复M2历史事实 |

任一PR不得为了“文档先行”提前修改后一行所属contract；否则该PR独立合入后会主动违反长期contract。

### M2：Background supervisor + HostCore/HostSession 生命周期原子迁移

这是核心行为cut，supervisor、HostCore、HostSession、lease、required语义在同一个PR落地，避免出现“worker
已后台运行但HostSession仍同步refresh”或“installation已替换但lease不存在”的中间版本。

修改：

- `runtime/mcp/supervisor.py`；
- `host/core.py`、`host/session.py`；
- `runtime/wiring.py`、`runtime/agent.py`；
- `event/events.py`、`event/__init__.py`、`event_log/serialization.py`；
- 本PR拥有的runtime/event/host长期contracts；
- MCP tool/resume adapters；
- subagent safety narrowing。

内容：

- `prepare/await_required/drain_candidates`；
- per-server attempt reservation与same-epoch stale rejection；
- optional background、required bounded await；
- non-blocking HostCore open；
- session-open后的required generation blocking；
- safe-point installation；
- unchanged server slot reuse；
- per-call/pending lease与retiring slot；
- suspended resume与child cancellation；
- shared close `Future[None]`、close attempt identity与并发waiter相同success/error结果；
- worker/lease bounded drain；drain失败阻止HostCore释放session、terminal lease、workspace与conversation
  ownership；
- close retry继续使用同一supervisor/slot/pending ownership，不构造空的新supervisor绕过旧资源；
- timeout/cancellation/error不得把supervisor或HostSession误报为`CLOSED`；
- bounded `McpInstalledServerSnapshotFact`与`McpCapabilitySnapshotInstalledEvent`生产接线；
- V1 event producer固定写`catalog_artifact_id=None`，不创建catalog artifact；
- `RunStartEvent.mcp_installation_id`与`mcp_installation_owner_runtime_session_id` required；parent owner指向
  自身，child指向parent runtime session；
- parent首个installation audit与RunStart使用同一原子`RuntimeSession.emit_many()` batch；
- pending audit只在commit acknowledgement后删除，audit失败不创建半启动run、不进入model call；
- post-commit publication failure依据`committed_events`删除已提交audit并继续向上传播，不把audit留在pending queue；
- required initialization成功后才写open manifest；manifest写入后、registry publish前失败使用bounded close tombstone
  finalization，required失败不得留下resumable manifest；
- optional retry由supervisor-owned timer后台启动并写`trigger="retry"` provenance；config epoch变化清空旧backoff；
- 删除production`sync_servers()`与`_sync_mcp_servers_for_turn()`；
- supervisor唯一manager close owner。

本PR在对应producer与runtime行为同批落地时更新：

- `contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md`：RunStart installation owner字段与RunStart/audit原子batch；
- `contracts/HOST_RESUME_CONTRACT.zh.md`：pending lease promotion、borrow、terminal release与required resume分支；
- `contracts/AGENT_RUNTIME_LOOP_CONTRACT.zh.md`：run-frozen capability surface、safe point与child installation owner。

验收：

- slow optional不阻塞banner；
- initial与mid-session required语义一致；
- slow old attempt输给newer same-epoch attempt；
- unrelated server更新不破坏pending request；
- related slot更新取消child并drain；
- install失败保留old optional installation；
- post-linearization architecture fault不可在线repair，只能保留ownership并close/reopen；
- required失败不创建/恢复run；
- RunStart与首次installation audit不存在partial commit；
- child RunStart可通过owner runtime session跨ledger定位parent installation audit；
- shared close waiter不会丢失owner failure；
- MCP drain timeout保留HostSession、terminal lease、workspace和同一supervisor供retry；
- close/open/config races无leak。

### M3：Durable audit投影 + Inspector / CLI

修改：

- `inspector/service.py`；
- `contracts/INSPECTOR_PROJECTION_CONTRACT.zh.md`；
- REPL banner/`:status`。

内容：

- 投影M2已经落地的bounded installed facts与per-server attempt summaries；
- 通过`EventLogLocator`按RunStart owner runtime session跨ledger join child installation attribution；
- lifecycle timing/status；
- live/historical normalized projection；
- `catalog_artifact_id=None`按V1固定值展示，不读取或补写catalog archive；
- changed tool/diagnostic bounds。

验收：

- Inspector稳定展示M2已冻结的audit commit、coalesce与finalize归属事实；
- mixed-trigger installation可按server还原attempt/trigger/retry/request/page/cache/stale summary；
- historical Inspector不触发network或读取live manager；
- event payload不重复完整catalog且不泄密。

### M4：Close压力验证、deletion gates、real dogfood

修改：

- supervisor/HostSession/HostCore close fault fixtures与可观测性；
- all MCP tests/docs/contracts final pass；
- grep/import architecture gates。

内容：

- 对M2已落地的shared close Future/attempt identity做高并发与取消压力测试；
- blocked worker/SDK owner/borrower drain的罕见fault injection与recovery验证；
- 验证close retry始终保留M2的原ownership；
- 删除M0/M1阶段残留的unused adapters/helpers；
- real MCP与真实REPL latency dogfood。

验收：

- close during connect/discovery；
- close时pending lease可terminalize/drain；
- blocked SDK close保留HostSession可重试；
- no task/client/process/manager-slot leak；
- slow optional docs server不阻塞banner，ready后下一safe point可调用；
- required deadline、direct CLI、resume与subagent dogfood；
- full Ruff/pytest/real MCP；
- §13 grep与architecture gates全部为零。

## 12. 测试矩阵

### 12.1 Config与schema

- `test_mcp_config_uses_new_timeout_defaults`
- `test_mcp_config_rejects_legacy_startup_timeout_field`
- `test_mcp_event_safe_fingerprint_redacts_headers_and_tokens`
- `test_mcp_runtime_fingerprint_changes_on_secret_rotation`
- `test_snapshot_semantic_fingerprint_ignores_timing_diagnostics_and_random_ids`
- `test_snapshot_semantic_fingerprint_changes_with_catalog_semantics`
- `test_mcp_snapshot_status_timing_invariants`
- `test_mcp_snapshot_fact_rejects_non_null_catalog_artifact_id_in_v1`

### 12.2 Discovery

- `test_discovery_calls_only_declared_capabilities`
- `test_discovery_methods_share_one_absolute_deadline`
- `test_discovery_cancels_and_drains_sibling_requests`
- `test_declared_optional_method_failure_is_not_ready`
- `test_server_workers_run_concurrently`

### 12.3 Epoch与candidate

- `test_disable_wins_over_late_ready_candidate`
- `test_reconfigure_wins_over_old_epoch_completion`
- `test_slow_ttl_refresh_loses_to_newer_same_epoch_refresh`
- `test_retry_candidate_requires_current_desired_attempt_id`
- `test_required_failed_candidate_retries_in_background_with_retry_trigger`
- `test_runtime_config_change_and_removal_reset_retry_backoff`
- `test_real_attempt_failure_owns_background_retry_timer_until_close`
- `test_close_wins_over_worker_completion`
- `test_stale_candidate_manager_is_closed_once`
- `test_worker_exception_returns_task_ownership`
- `test_unchanged_config_before_ttl_does_not_schedule_refresh`

### 12.4 Session open

- `test_optional_mcp_does_not_block_host_session_open`
- `test_required_mcp_blocks_until_ready`
- `test_required_mcp_timeout_rolls_back_session`
- `test_mixed_servers_wait_only_for_required`
- `test_open_cancel_drains_background_mcp`
- `test_direct_cli_uses_same_sdk_facade_and_deadline_contract`
- `test_required_mcp_failure_does_not_write_open_manifest`
- `test_manifest_written_before_publish_failure_is_closed_or_tombstoned`

### 12.5 Safe point

- `test_background_ready_does_not_mutate_active_run`
- `test_next_safe_point_installs_descriptor_and_binding_atomically`
- `test_installation_id_reused_only_when_surface_and_all_slot_identities_are_unchanged`
- `test_any_surface_or_slot_identity_change_generates_new_installation_id`
- `test_reused_binding_has_no_global_installation_or_epoch_identity`
- `test_reused_binding_validates_against_exact_supervisor_slot_identity`
- `test_installation_commit_marks_old_slot_retiring_before_child_can_acquire`
- `test_commit_slot_transition_and_lease_acquire_share_sync_linearization_boundary`
- `test_installation_commit_block_never_awaits_asyncio_lock`
- `test_installation_commit_post_linearization_fault_latches_session_until_close`
- `test_faulted_installation_allows_only_inspect_status_and_close`
- `test_faulted_installation_has_no_live_reconcile_authority`
- `test_faulted_installation_close_drains_all_slot_lifecycle_variants`
- `test_reopen_after_fault_builds_fresh_supervisor_and_installation`
- `test_installation_identity_matches_tool_binding_identity`
- `test_unrelated_server_installation_change_reuses_pending_binding_slot`
- `test_retired_manager_waits_for_child_borrower_release`
- `test_retiring_manager_rejects_new_tool_acquisition`
- `test_suspended_request_transfers_lease_to_pending_owner`
- `test_pending_creation_failure_aborts_promoted_lease_reservation`
- `test_pending_resume_borrows_existing_lease_without_slot_reacquire`
- `test_pending_resume_terminal_path_releases_slot_lease`
- `test_install_failure_keeps_previous_snapshot`
- `test_config_disable_revokes_subagent_capability`
- `test_related_server_slot_change_cancels_child_without_lifetime_lease`
- `test_child_registry_indexes_exact_mcp_binding_identities`
- `test_mcp_resume_rejects_changed_binding_generation`
- `test_mid_session_required_config_change_blocks_before_run_start`
- `test_safe_point_joins_running_required_background_retry_attempt`
- `test_unrelated_required_resume_failure_preserves_pending_state_and_lease`
- `test_same_config_refresh_failure_preserves_valid_pending_binding`
- `test_reconfigured_pending_binding_terminalizes_and_releases_lease`

### 12.6 Audit与Inspector

- `test_installed_event_uses_bounded_snapshot_facts_not_full_catalog`
- `test_mixed_candidate_batch_records_per_server_attempt_triggers`
- `test_audit_distinguishes_total_added_and_revoked_tool_counts`
- `test_audit_cache_outcome_distinguishes_miss_from_not_applicable`
- `test_coalesced_pending_audit_points_previous_id_to_last_durable_installation`
- `test_coalesced_pending_audit_merges_bounded_attempt_summaries_and_triggers`
- `test_run_start_and_first_installation_audits_commit_as_one_atomic_batch`
- `test_run_start_and_audit_batch_failure_leaves_no_half_started_run`
- `test_pending_installation_audit_pops_only_after_commit_acknowledgement`
- `test_pending_audit_failure_prevents_model_call`
- `test_run_start_post_commit_publication_failure_acknowledges_mcp_audit`
- `test_resume_audit_post_commit_publication_failure_acknowledges_pending`
- `test_finalize_installation_is_audited_by_next_run`
- `test_run_start_references_non_null_mcp_installation_id`
- `test_parent_run_start_installation_owner_points_to_self`
- `test_child_run_start_installation_owner_points_to_parent_runtime_session`
- `test_inspector_joins_child_installation_audit_through_event_log_locator`
- `test_historical_inspector_does_not_read_live_manager`

### 12.7 Close

- `test_session_close_cancels_and_drains_mcp_workers`
- `test_m2_close_waiters_share_future_success_and_failure`
- `test_mcp_close_timeout_preserves_host_session_for_retry`
- `test_concurrent_close_waiters_share_failure`
- `test_open_rollback_closes_candidate_and_installed_managers`
- `test_runtime_wiring_does_not_double_close_mcp_manager`
- `test_mcp_terminal_post_commit_failure_folds_state_and_releases_lease`

### 12.8 Real dogfood

1. user config启用一个人为延迟20–30秒的optional MCP；
2. 启动REPL并记录banner时间；
3. banner应先出现并显示starting；
4. 等worker完成；
5. 下一turn询问/调用MCP tool；
6. event/Inspector显示相同installation id；
7. `:close`后确认没有SDK owner task/http client/stdio process；
8. required版本验证deadline与startup error。

## 13. Grep 与 Architecture Gates

最终必须为零：

```bash
rg "await .*sync_servers\(" src/pulsara_agent/host src/pulsara_agent/runtime
rg "_sync_mcp_servers_for_turn" src tests
rg "runtime_wiring\.mcp_manager|runtime_wiring\.mcp_bundle" src
rg "McpCapabilityBindingBundle" src/pulsara_agent
rg "bundle\.manager\.snapshots" src
rg "startup_timeout_ms" src/pulsara_agent
```

允许：

- test fixture中明确标记legacy schema rejection的字符串；
- archived docs中的历史描述；
- 新supervisor内部的`installed manager slots`，但不暴露给RuntimeWiring。

增加import/layering guard：

- MCP contracts不importHostSession/AgentRuntime；
- SDK adapter不importcapability provider；
- capability provider只消费installed snapshot；
- worker module不importHostSession mutation API。

## 14. 故障语义表

| 场景 | required | optional |
|---|---|---|
| connect timeout | session open失败 | FAILED/重试，session继续 |
| NEEDS_AUTH | session open失败 | NEEDS_AUTH，无tool |
| declared discovery失败 | session open失败 | DEGRADED，无tool/重试 |
| undeclared capability | 不调用该method | 不调用该method |
| config disable | safe point立即撤销 | safe point立即撤销 |
| stale completion | close candidate，忽略 | close candidate，忽略 |
| install invariant失败 | host action fail closed | 保留旧installation并diagnostic |
| installation post-linearization architecture fault | latch session；只允许inspect/close/reopen | 同左 |
| close timeout | HostSession teardown阻塞并可重试 | 同左 |
| resume binding改变 | typed denied/error result | 同左 |

## 15. 可观测性指标

每server至少记录：

- config epoch；
- ticket/trigger；
- queued→connect、connect、discovery、total duration；
- requested methods与pagination counts；
- ready/degraded/failure reason；
- retry attempt/next retry；
- candidate ready time与installed time差；
- stale candidate discard count；
- cache outcome（not-applicable/miss/TTL/config-fingerprint/SDK-response）；
- close cancel/drain duration。

不记录：

- request/response完整payload；
- auth header/token；
- URL query/userinfo；
- stdio secret env value；
- MCP tool arguments/results。

## 16. 实施完成后的代码形态

```text
HostCore.open_session
  -> supervisor.prepare(configs)
  -> await required only
  -> HostSession published

McpServerSupervisor
  -> owns workers/managers/candidates/epoch/retry/close
  -> never mutates HostSession

HostSession safe point
  -> schedule due work (local, non-blocking)
  -> drain candidates
  -> build McpInstalledCapabilitySnapshot
  -> atomic wiring/capability/tool swap

AgentRuntime model loop
  -> sees one frozen installation for the run

HostSession.close
  -> stop run/subagents
  -> cancel/drain supervisor
  -> close runtime
```

最终产品语义不是“异步启动后随时热插拔”，而是：

> **远端工作在后台发生，能力变化只在HostSession控制的安全边界生效；optional服务不阻塞交互，required服务仍有明确的启动合约。**

## 17. M0–M4 实施闭环（2026-07-11）

### 17.1 实际落地结果

| 阶段 | 状态 | 已删除的旧真源 / 已建立的唯一真源 |
|---|---|---|
| M0 | 完成 | event-safe facts 落在 `primitives/mcp.py`；attempt/candidate/slot/lease 落在 `runtime/mcp/types.py`，并冻结 recursive JSON、timing、fingerprint 与 bounded schema |
| M1 | 完成 | 删除 `startup_timeout_ms`、`McpServerSnapshot.config`、旧 SDK `start()`、`client.py` / `stdio.py` compatibility path；`McpInstalledCapabilitySnapshot` 成为 descriptor/binding surface 唯一真源 |
| M2 | 完成 | 删除 production `sync_servers()` 与 turn/resume 同步 refresh；session-owned supervisor、per-server attempt/generation、required wait、safe-point installation、pending lease、child binding index、RunStart/audit atomic batch 与 close retry 全部接线 |
| M3 | 完成 | bounded installation event、per-server attempt summary、RunStart owner session attribution、cross-session Inspector join、REPL/status 与 direct CLI 使用同一事实链 |
| M4 | 完成 | close/connect/discovery/candidate/slot/pending lease 故障注入、高并发 close waiter、architecture grep、真实 MCP 与真实 REPL latency dogfood 闭环 |

V1 的 `catalog_artifact_id` 是 required schema 字段但固定为 `None`；完整 server-controlled catalog 只存在于
process-local installation，不进入每次 durable audit。post-linearization architecture fault 不提供 live repair：
session 被永久 latch 为 inspect/status/close-only，关闭并重新打开后从 config 重建。

### 17.2 最终自动验证

在停止代码修改后的最终快照上执行：

```text
uv run pytest -q
1759 passed, 69 skipped, 150 warnings in 125.50s

PULSARA_RUN_REAL_MCP=1 uv run pytest tests/test_real_mcp_dogfood.py -q -s
2 passed in 24.37s
```

real-MCP dogfood 覆盖：

1. 公共 LangChain MCP 的真实 SDK connect/initialize/discovery；
2. `search_docs_by_lang_chain` 真实 tool call；
3. supervisor close 后无 task/slot ownership；
4. required localhost 不可达 endpoint 的真实 SDK failure，按 bounded deadline 产出
   `McpRequiredStartupError(reason_code="mcp_required_generation_unavailable")`。

最终 `ruff check src tests`、`git diff --check` 与 §13 的 7 个 grep / live-reconcile gate 全部为零错误。

### 17.3 真实 REPL 轨迹

使用一个 optional 公共 LangChain MCP 的隔离 workspace 运行真实：

```text
uv run pulsara host repl --env-file .env --workspace <dogfood-workspace>
```

观察到：

1. 远端 discovery 尚未完成时先打印 banner：
   `MCP servers: latency-docs=starting (no tools)`；
2. 等待后台 discovery 后发起下一 turn，safe point 安装新 surface；
3. `:status` 显示同一 server 为 `ready`、`tool_count=3`；
4. `:close` 在约 `0.39s` 内完成；
5. 用户当前 `docs-langchain.enabled=false` 的真实配置 smoke 启动并关闭总耗时约 `2.59s`，显示
   `docs-langchain=disabled (no tools)`，没有网络等待。

因此 M4 的结论不是“测试 helper 看起来非阻塞”，而是生产 CLI、真实 PostgreSQL/Oxigraph wiring、真实 SDK
和真实远端 MCP 的完整交互链均符合本契约。

### 17.4 最终 deletion / architecture gates

以下 production 搜索结果均为零：

```text
await .*sync_servers\(
_sync_mcp_servers_for_turn
runtime_wiring.mcp_manager / runtime_wiring.mcp_bundle
McpCapabilityBindingBundle
bundle.manager.snapshots
startup_timeout_ms
reconcile_mcp_installation
```

`tests/test_mcp_architecture.py` 同时约束低层 MCP contract 不反向 import HostSession/AgentRuntime、SDK/worker
不 import capability/host mutation layer，以及被删除 production symbols 不得重新出现。

### 17.5 Model-visible STARTING 语义补强

真实 REPL 验证暴露了一个UX边界：第一run冻结在`STARTING`时，模型虽没有MCP tool schema，却可能把历史MCP
tool result误当成当前能力，或把正常后台启动误报为配置失败。最终实现因此让`McpCapabilityProvider`为所有非空
installation生成Pulsara-owned lifecycle contract，并强制声明：

- 当前run只有实际tool schema中的MCP工具可调用；
- `STARTING`在当前run明确不可用，但不是配置失败证据；
- 历史message/tool result/memory/compaction summary不是当前availability事实；
- 成功后只承诺“可能在后续safe point安装并在后续run可见”，不承诺下一run成功；
- lifecycle prompt本身run-frozen，background worker不得mid-run更新。

该prompt不包含任何remote server-provided prose，避免把MCP lifecycle projection变成新的prompt-injection入口。

真实REPL复测中，第一run的模型明确报告`docs-langchain=starting`、`installed_tool_count=0`、当前无可调用
MCP工具，并正确说明这不是配置故障；第二run在safe point安装后报告`ready`与3个实际tool schema中的工具。
