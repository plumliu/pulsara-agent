# MCP Capability Contract

_Created: 2026-07-04_

本文档定义 Pulsara MCP 接入的长期契约。MCP 是 unified capability surface 的 provider + execution binding 扩展，不是独立的第二套工具系统。

相关代码：

- [src/pulsara_agent/runtime/mcp/config.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/mcp/config.py)
- [src/pulsara_agent/runtime/mcp/manager.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/mcp/manager.py)
- [src/pulsara_agent/runtime/mcp/types.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/mcp/types.py)
- [src/pulsara_agent/capability/providers/mcp.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/providers/mcp.py)
- [src/pulsara_agent/tools/adapters/mcp.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/adapters/mcp.py)
- [tests/test_capability_mcp.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_capability_mcp.py)

---

## 1. 核心立场

MCP 接入由同一份 immutable、session-scoped `McpInstalledCapabilitySnapshot` 同时产出：

- `CapabilityDescriptor`；
- `AsyncTool` execution binding；
- diagnostics；
- exact `McpBindingIdentity` execution bindings。

完整 installation 是 descriptor、binding、server snapshot 和 installation attribution 的唯一原子真源。旧
`McpCapabilityBindingBundle`、`build_mcp_bundle()`、RuntimeWiring 的 manager+bundle 双字段均不是受支持生产契约。

不得出现以下状态：

- descriptor 来自一份 snapshot，adapter 来自另一份 snapshot；
- provider 只 advertise descriptor，没有 ToolRegistry binding；
- adapter 能执行，但 capability surface 看不到 descriptor。

生产协议层固定面向官方 Python MCP SDK v2 线构建。稳定版发布前，仓库依赖真源是 `mcp[cli]==2.0.0b1`。`mcp-types==2.0.0b1` 只作为 lockstep guard；升级 PR 必须从 `mcp` package metadata 校验两者完全一致，不得独立推进 `mcp-types`。所有 SDK beta API 必须被 Pulsara-owned manager/facade 包住，不得渗透到 capability、permission、artifact、context compiler 或 host/session 调用点。

当前手搓 stdio/http JSON-RPC manager 只允许作为 legacy spike、deterministic fixture 或迁移辅助，不得作为新生产能力的默认路径继续扩展。不维护 MCP SDK v1 生产兼容分支。

---

## 2. Session-owned supervisor 与 per-server slot

MCP client / server process / remote connection 由 session-owned `McpServerSupervisor` 持有；每个已发现 server
有独立 `McpManagerSlot`，slot 内才持有一个 `McpClientManager` facade。Supervisor 是 manager、worker、candidate、
普通call lease、pending interaction lease、retiring slot 和 close attempt 的唯一 lifecycle owner。Capability execution
handle/tracker不得镜像pending lease计数；MCP slot/manager退休只依赖Supervisor自己的active borrower与pending reservation
状态。

Per-server manager 职责：

- 执行 `call_tool(server_id, tool_name, arguments, timeout_ms=...)`；
- 执行 `read_resource(server_id, uri, ...)` / `get_prompt(server_id, name, arguments, ...)` 的显式 wrapper path（若该能力在产品面开启）；
- `resume_suspended_request(...)` / `resume_input_required(...)`：用 Pulsara-owned resolution DTO 恢复 SDK v2 `InputRequiredResult` 挂起的原始请求；
- `aclose()` 幂等关闭该 server connection；
- `cancel_active()` best-effort 取消 active calls；

MCP adapter 不拥有 client，不启动 server，不负责 lifecycle teardown。

Host/session close 必须通过 supervisor 关闭 workers、pending lease 和所有 slot。close 必须共享同一个
`Future[None]` attempt、幂等且有界；drain 失败必须保留原 HostSession/supervisor/lease ownership 供重试，不能继续
释放 terminal、workspace 或 conversation ownership。

Optional startup failure的exponential backoff由supervisor-owned retry timer task驱动；deadline到达后只能以
`reconcile_trigger="retry"`预留新attempt，safe point只负责安装candidate。backoff属于server+global config epoch；
epoch变化或server删除必须取消旧timer并清零retry state。

若Host safe point到达时同runtime identity的required retry attempt已经在后台运行，`prepare()`返回的ticket必须携带
该existing attempt；`await_required()`复用其worker并等待原absolute deadline。不得重启attempt，也不得因“本ticket
没有新reserve”立即报告required unavailable。

SDK-backed manager 必须把 SDK session / transport / subprocess / remote connection 全部收口在 manager 内部，并向上只暴露 Pulsara-owned DTO 与 protocol。SDK 初始化、capability discovery、tool/resource/prompt 调用、timeout、cancellation、connection error 都必须转换为 Pulsara 的 snapshot status、diagnostics、tool result 或 `ToolExecutionSuspended`。

生产 manager 采用一 server 一 SDK `Client`，由 supervisor 聚合多 server snapshot。SDK `ClientSessionGroup` 不得成为命名、权限、descriptor 或 exposure 的真源；若未来使用，只能封装在 facade 内作为实现细节。

`respond_elicitation(...)`及旧`mcp_elicitation` continuation已从production与active pytest surface删除。生产恢复
语义只有“携带input_responses与request_state重试同一个原始请求”，不是“按request_id向server回包”。

SDK-backed manager 对外不得暴露 SDK `InputRequest`、`InputResponses`、`InputRequiredResult` 或 SDK request-state wrapper。所有这类对象必须在 manager/facade 内部完成转换；跨 HostSession、CLI、event log、inspector 边界流动的只能是 Pulsara-owned DTO。

### 2.1 Background prepare 与 required deadline

`McpServerSupervisor.prepare(configs, trigger)`只能做process-local配置解析、config epoch推进、per-server attempt
reservation和worker调度，不得等待network。每个attempt具有独立`reconcile_attempt_id`、
`discovery_generation`、runtime config fingerprint和prepare时冻结的绝对deadline；candidate只有在仍是该server的
current desired attempt时才可安装，较早启动但较晚完成的same-epoch worker必须作为stale丢弃。

Optional server始终后台连接/发现，不阻塞Host banner或普通host open。Required server在initial open以及任一run/
resume safe point发现新required generation时，必须在创建/恢复run前等待其自己的绝对deadline；任一required server
到达deadline或失败，本次host action失败且不得创建/恢复run。Caller不得传另一套任意deadline覆盖server config。

Worker只提交ready/failed candidate，不直接修改RuntimeWiring或CapabilityRuntime。HostSession只在持有`_run_lock`的
safe point drain candidate并安装。post-linearization architecture fault后V1不提供live repair authority：session latch
fail-closed，只允许inspect与bounded close/reopen。

### 2.2 Model-visible lifecycle contract

只要当前 installation 含 MCP server snapshot，`McpCapabilityProvider` 必须向 context compiler 提交一段
Pulsara-owned、run-frozen 的 lifecycle contract。它不是远端 MCP server prompt，不得包含 server-provided
message、instructions、diagnostic正文或catalog prose。

模型侧规则是强制的：

- 只有当前run实际provider tool schema中的MCP工具可调用；
- `STARTING`表示后台discovery进行中，该server工具在当前run明确不可用；
- 不得从prior messages、历史tool results、memory或compaction summary推断当前MCP可用性；
- 不得仅因`STARTING`宣称配置失败或要求用户修配置；
- 被问及时只能说明后台discovery进行中，成功后**可能**在后续HostSession safe point安装并在后续run可见，
  不得承诺下一run一定成功；
- `FAILED / DEGRADED / NEEDS_AUTH / DISABLED / CLOSING / CLOSED`在当前run不暴露callable tools；
- `READY`只表示snapshot已安装，具体callable name仍以当前run实际tool schema为唯一权威。

该prompt随run capability exposure一起冻结。worker在run中途完成只会产生candidate，不得改写这段prompt；下一safe point
安装新installation后，下一run重新生成对应lifecycle contract。

---

## 3. Installation builder

`build_mcp_installation(supervisor, snapshots, slots, ...)` 是 V1 MCP descriptor/binding 的 composition seam。

规则：

- 只处理 READY snapshot 的 tools。
- 非 READY snapshot 产生 diagnostics，不产 callable tools。
- 每个 discovered tool 必须通过 `mangle_mcp_tool_name(server_id, tool_name)` 转成模型工具名。
- 同名冲突必须 fail-closed，并产生 `mcp_tool_name_collision` diagnostic。
- descriptor names 与 tool binding names 必须一一对应；缺任一侧都产生 diagnostic。
- 每个 binding 保存精确的 `server_id + slot_id + snapshot_id + discovery_generation`；不保存 global installation id
  或 global config epoch；
- 只有 surface semantic payload 与全部 slot identity 均未变化时才复用 installation id；任一变化都生成新 id。

Installation 必须在 safe point 作为完整 execution surface 安装到 RuntimeWiring、
`RuntimeSession.extra_tool_bindings` 与 `CapabilityRuntime.providers`，避免 snapshot/binding 不同源。未变化 server 的
slot/binding对象可以跨 installation 原样复用。

---

## 4. Tool name mangling

MCP model tool name 必须稳定、可逆解释、符合模型工具名限制。

规则：

- 名称必须包含 server id 与原始 tool name 的稳定信息。
- 非法字符必须替换为安全字符。
- 超长名称必须使用稳定 hash suffix，而不是简单截断到可能冲突。
- 发生碰撞时不得随机重命名；必须 fail-closed 并产 diagnostic。
- diagnostics / metadata 中保留原始 `server_id` 与 `original_tool_name`，供 inspector 解释。

---

## 5. Descriptor semantics

MCP descriptor 的 provider kind 必须是 `mcp`。

MCP tool annotations 映射：

- `readOnlyHint=True` -> `is_read_only=True`。
- 缺失 `destructiveHint` 时按 destructive 处理。
- 缺失 `openWorldHint` 时按 open-world 处理。
- `supports_parallel_tool_calls` 决定 `is_concurrency_safe`。
- server default approval mode 只能作为 hint；最终 ALLOW/WAIT/DENY 由 Pulsara permission gate 决定。

V1 MCP descriptor advertise policy 是 DIRECT；未来 deferred MCP discovery 必须先扩展 unified capability surface，而不能绕过现有 exposure/gate。

---

## 6. Execution adapter

`McpCapabilityTool` 是 thin adapter。

职责：

- 按 model tool name 接收 ToolCall；
- 用 frozen `McpBindingIdentity` 向 supervisor 获取 exact slot lease；
- acquire除exact slot/snapshot/generation外还必须校验slot runtime fingerprint仍等于current desired config；config
  reconfigure后旧slot可在installation commit前保持物理`installed`，但不得取得新lease；
- 将调用委派给该 lease 所属 per-server manager 的 `call_tool()`；
- 按 manager 返回值格式化 `ToolExecutionResult`；
- 在 metadata 中写入 provider kind、server id、original tool name。

禁止：

- adapter 私自持有独立 client；
- adapter 自行启动/关闭 MCP server；
- adapter 绕过 ToolExecutor / RuntimeSession event recorder；
- adapter 绕过 capability gate 或 permission gate。

普通调用在 terminal result 后释放 per-call lease。`ToolExecutionSuspended` 必须通过
`promote_lease_to_pending(lease, interaction_id)`把同一 lease 转移给 process-local pending owner；resume 使用
`borrow_pending_lease()`借用原 lease，不得重新 acquire 当前同名 binding。terminal resume/cancel 的durable result
提交后才调用`complete_pending_lease()`。binding disable/remove/reconfigure时 terminal deny 并释放 lease；无关
required server 暂时失败或同配置 refresh 失败时保留 pending state 与原 lease。

terminal fact在commit前失败时保留pending owner供重试；fact已commit但ordered publication失败时，先按committed slice
折叠state并完成/abort exact Supervisor lease，再传播publication failure；commit outcome unknown时保留lease并阻止
destructive close。execution handle退休与这套pending ledger完全独立。

Suspension event与terminal resume batch都必须预生成stable event ID，并在任意`BaseException`（包括
`CancelledError`）后调用`confirm_event_batch()`裁决：`NONE`时suspension abort reservation、terminal resume恢复原pending
state；`FULL`时suspension confirm reservation、terminal resume fold result并complete lease；`PARTIAL/UNKNOWN`时latch
RuntimeSession、保留Supervisor owner并禁止破坏性teardown。不得用Python异常类型推断commit outcome。

---

## 7. InputRequired suspension

SDK v2 modern 交互式输入以 `InputRequiredResult` 表达。生产路径必须手动接管 input-required loop，不得直接调用 SDK 高层 `Client.call_tool()` 并让 SDK 自动消费 input-required callback。

生产 manager 对交互式方法必须使用底层 session API：

- `client.session.call_tool(..., allow_input_required=True)`；
- `client.session.read_resource(..., allow_input_required=True)`；
- `client.session.get_prompt(..., allow_input_required=True)`。

收到 terminal result 时正常返回；收到 `InputRequiredResult` 时，通过通用 tool suspension seam 表达：

```text
AsyncTool.execute_async()
  -> manager calls SDK session method with allow_input_required=True
  -> freeze McpInputRequiredSuspensionFact
  -> ToolExecutionSuspended(interaction_kind="mcp_input_required",
                            prepared_mcp_input_required=...)
  -> AgentRuntime state.pending_interaction_kind = "mcp_input_required"
  -> HostSession.pending_interaction = PendingMcpInputRequired
  -> freeze PreparedMcpInputRequiredResolution before first await
  -> Host boundary + McpInputRequiredResolutionSubmittedEvent atomic FULL
  -> AgentRuntime.resume_after_mcp_input_required()
  -> adapter resumes through normal capability/gate-aware tool continuation
  -> manager.resume_suspended_request(...)
```

规则：

- MCP input-required 不得被格式化成普通 tool error。
- durable suspension source必须冻结 interaction id、wrapper tool call、server/source method、
  protocol/round、deadline、exact binding、pending lease reservation与 predecessor resolution。
  它只保存 typed user-visible `input_requests`、original request envelope fingerprint和 opaque
  request-state fingerprint；raw arguments/request_state不得进入 event或 artifact。
- `input_requests` 必须是 Pulsara-owned、bounded、可 inspect 的 DTO；SDK-backed
  manager/facade 必须先转换 SDK `InputRequest`。Host/UI只读取该 DTO，不接触 SDK 类型。
- Original request与nullable `request_state` 的真实对象只由 process-local Supervisor
  pending lease owner持有。Live resume时 manager/facade将 frozen response与同一 opaque
  owner重新结合；SESSION_REOPEN不得从 durable fingerprint重建 request_state或 acquire
  同名 binding。
- v1 只有 wrapper tool / runtime tool-call 路径支持 input-required suspend。direct CLI / inspect / doctor 路径如果遇到 resource/prompt input-required，必须 fail-closed 并产 diagnostic，不创建 pending interaction，也不尝试在 CLI 内临时提问。
- 如果 v1 通过显式 wrapper tool 暴露 resource/prompt input-required，typed source必须同时
  证明 wrapper tool 身份与 underlying request envelope fingerprint；两者不得混用。
- HostSession 只产 Pulsara-owned、可持久化的 resolution DTO，不构造或持有 SDK `InputResponses`。
- SDK `InputResponses` 只能由 SDK-backed manager/facade 在内部构造，并用同一个 request_state 与同一个原始 request 重试。
- 再次收到 `InputRequiredResult` 时继续 suspend；超过 round cap、用户 cancel、session close 或 request_state 失效时返回结构化 cancel/error。
- pending input-required 状态下不得自动 context compact；resolution 后进入 follow-up safe point 时才可 mid-turn compact。
- legacy `elicitation/respond` 不属于受支持runtime或test compatibility contract。

Lifecycle由 `McpInputRequiredLifecycleStore` 唯一归约。每次 resolution-submitted携带
attempt ordinal及前一 resolution/resume-failed exact refs；normal ToolResult terminal、
expired/binding-changed disposition、下一轮 suspension和 interaction closure都必须 exact
join本 attempt。Critical publication unavailable安装 RuntimeSession publication latch，并
按 active/terminal interaction matrix完成 closure或 RunEnd；不得假装 publisher已catch up。

---

## 8. Tool result mapping

SDK v2 `CallToolResult` 必须完整映射，不得只 join text：

- `TextContent` 进入 model-facing output/preview；
- `ImageContent`、`AudioContent`、blob-like resource 进入 artifact，model-facing output 只放摘要与 artifact ref；
- `EmbeddedResource` 按 resource 内容类型归档或预览；
- `ResourceLink` 保留 uri/name/mime/description metadata，必要时提示可用 MCP resource read；
- `structured_content` 保存为 JSON artifact 或 metadata；小型 JSON 可进入 preview，大型 JSON 必须走 artifact；
- `is_error=True` 是 tool-visible result，应作为模型可见 tool result 返回，不得伪装成协议异常；
- top-level `MCPError`、transport error、validation error 是执行/协议错误，进入 diagnostics、tool error event 与 manager status。

---

## 9. Pagination / cache / notifications

Snapshot sync 与 doctor 必须 drain SDK pagination：

- `list_tools`、`list_resources`、`list_resource_templates`、`list_prompts` 都必须支持 cursor loop；
- 每类 list 必须有 max pages / max items；
- repeated cursor 必须 fail-closed；
- snapshot sync / doctor 必须明确绕过 SDK cache：可使用高层 `Client` verbs 并传 `cache_mode="refresh"`，或使用 `ClientSession.list_*` / `Client(cache=False)` 等不读取 SDK response cache 的路径；
- 任何非 refresh cache 使用必须可 inspect：至少记录 server id、method、cache mode、hit/miss、cache key scope；
- 除非使用 Pulsara-owned/custom cache store 或 facade-level instrumentation 能可靠记录 hit/miss，否则生产路径必须 `cache=False` 或强制 `cache_mode="refresh"`。SDK 默认 cache 如果无法被 Pulsara instrumentation 观测，视为不可用，不得在生产路径静默开启。
- SDK cache 不得在 event/inspector 中静默生效；
- v1 不承诺 notification-driven live invalidation；server list/resource/prompt 变化只通过 manual refresh、reconnect、TTL 或下一轮 sync 生效。

---

## 10. HTTP transport security

Remote streamable HTTP 生产路径禁止直接使用 SDK `Client(url)` URL shorthand，因为 SDK 默认 HTTP client 会 follow redirects。

生产路径必须：

- 构造 Pulsara-owned `httpx.AsyncClient`；
- 显式设置 redirect policy，默认 fail-closed；
- 统一注入 headers/auth/timeout/proxy/event hooks；
- 对 token/header/url diagnostics 做 redaction；
- 将 client 传给 `streamable_http_client(url, http_client=...)`。

---

## 11. Permission / capability gate

MCP tool 与内置 tool 共享同一套 gate：

1. capability exposure access；
2. permission policy；
3. ToolExecutor execution。

MCP 不拥有独立 approval 系统。MCP server 的 annotation / default approval mode 只能影响 descriptor hint 或 diagnostics，不得绕过 Pulsara 的 `PolicyPermissionGate`。

MCP input-required resume 是恢复已挂起 MCP 请求的边界，不得静默绕过 gate。resume 前必须重新执行 capability exposure / permission recheck：

- `DENY`：写入 `CapabilityGateDecisionEvent(decision="deny")` 与 denied/error tool result；
- `WAIT_FOR_USER`：V1 不创建二次 approval pending，必须 fail-closed，写入 `CapabilityGateDecisionEvent(decision="deny", reason_code="mcp_resume_permission_approval_unsupported")` 与 denied tool result；
- `ALLOW`：才可以调用 adapter / manager resume。

---

## 12. Observability

MCP 必须能通过现有 capability 与 tool events 被解释：

- `capability_exposure_resolved` 显示 MCP descriptor、hidden/deferred/callable 状态与 diagnostics；
- `CapabilityGateDecisionEvent` 显示 MCP tool 的最终 gate decision；
- tool call/result events 显示 model tool name；
- result metadata 保留 server id 与 original tool name；
- input-required suspension 写入 `tool_execution_suspended`。

Inspector 不应通过查询 live MCP manager 来解释历史；历史解释必须来自 event log 与 stored metadata。

---

## 13. 禁止事项

- 不允许 MCP provider 与 adapter 分开从不同 snapshot 构造。
- 不允许非 READY server 的 tools 进入 direct tools。
- 不允许名称冲突时自动随机改名继续执行。
- 不允许 adapter 拥有 MCP client lifecycle。
- 不允许 MCP approval/input-required 绕过 Pulsara pending interaction seam。
- 不允许 MCP server annotation 直接覆盖 permission policy。
- 不允许 SDK `ClientSessionGroup` 的合并命名结果成为模型工具名或权限真源。
- 不允许 remote MCP 生产路径使用 `Client(url)` shorthand。

---

## 14. 测试守护

最低测试门槛：

- `mcp[cli]==2.0.0b1` / `mcp-types==2.0.0b1` import smoke test 通过，且生产 wiring 使用 SDK-backed manager；
- READY snapshot 产出 descriptor + AsyncTool binding。
- FAILED / NEEDS_AUTH / DISABLED snapshot 产 diagnostic，不产 callable tool。
- name mangling 稳定，碰撞 fail-closed。
- descriptor/binding 缺任一侧产生 diagnostic。
- MCP tool 调用经 capability gate 与 permission gate 后执行。
- approval resume / crafted pending call 在 descriptor 缺失时 fail-closed。
- modern `InputRequiredResult` 可通过 typed suspension source suspend，HostSession 捕获
  pending，prepared resolution byte-exact路由回 manager；SDK `InputResponses` 只在
  manager/facade 内部构造。
- durable `input_requests` 使用 Pulsara-owned DTO，不泄漏 SDK `InputRequest`；
  nullable raw `request_state`只由 process lease owner保留，event只保存 opaque fingerprint。
- tools/call、resources/read、prompts/get 的 typed suspension都包含 source method、
  wrapper identity、exact binding、request-envelope fingerprint与 pending reservation。
- resolution attempt chain、normal terminal source ref、expired/binding-changed/closure atomic
  matrix、publication failure及 SESSION_REOPEN lost-lease closure均有回归。
- live `McpInputRequiredLifecycleStore`只常驻active interaction、active run所需的最小exact
  refs；terminal interaction与RunEnd必须prune resident state，无MCP事件batch走O(1)
  fast path。Inspector可用同一reducer在一次有界历史query内临时捕获terminal snapshots，
  但不得把该inspection sink安装进RuntimeSession。
- durable failure diagnostic只允许使用closed redaction profile registry。默认消息来自
  profile常量；任何允许的显式MCP diagnostic也必须经过中央secret scrubber，禁止回退
  `str(error)`。
- direct CLI / inspect / doctor resource/prompt 遇到 input-required 时 fail-closed + diagnostic，不创建 pending interaction。
- legacy `elicitation/respond`、`McpElicitationResolution`与`PendingMcpElicitation`不得重新进入production wiring。
- `is_error=True` result 是模型可见 tool result；protocol/transport error 是 tool error。
- pagination 有 max pages/items 与 repeated cursor guard；非 refresh cache hit/miss 可 inspect。
- remote HTTP 使用自建 `httpx.AsyncClient`，redirect 默认 fail-closed。
- manager close 幂等并取消 active calls。
- inspector 能看到 exposure/gate/tool metadata。

---

## 15. MCP 与 long-horizon rollout

MCP descriptor bundle 必须像 built-in descriptor 一样冻结 long-horizon action contract、classifier contract fingerprint 与 tool cost units。
MCP annotation 不得直接决定 phase policy；无法可靠分类的调用在 finalization 中 fail closed。

MCP tool admission 必须原子提交 capability/permission/phase gate 与原 tool rollout reservation。`InputRequiredResult` suspend 后保留同一
reservation，不创建第二份 reservation；pending lease 仍完全归 `McpServerSupervisor`。resume success、exposure deny、permission deny、
unsupported WAIT、cancel、timeout 与 protocol failure 都必须在 terminal tool-result batch 中精确 settle 原 reservation。terminal commit
NONE 保留 pending carrier与lease；PARTIAL/UNKNOWN latch并阻止破坏性 teardown。

MCP manager slot lease 与 rollout reservation 是不同 ownership，不得合并计数。前者保护 exact manager/request state，后者只核算 run
工作量；任一分支遗漏其中一侧都必须由 fault-injection test 捕获。
