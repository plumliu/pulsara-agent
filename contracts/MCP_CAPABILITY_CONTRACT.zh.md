# Pulsara MCP Capability Contract

_状态：MCP 2026-07-28 / Python SDK v2 production contract_

本文冻结 Pulsara 对 MCP 的长期产品、协议和 ownership 契约。实现细节以代码和 migration registry 为准；任何修改若改变下列 authority、提交顺序、secret 边界或恢复语义，必须同步更新本文和对应合同测试。

## 1. 核心立场

1. 官方 Python SDK 唯一版本为 `mcp[cli]==2.0.0`。
2. 只有 `runtime/mcp/sdk.py` 可以 import 官方 `mcp` SDK 与 `httpx2`。
3. Pulsara 不把 SDK client/session/cache 当作 durable authority。
4. exact protocol revision 与 behavior era 分开建模：
   - `2026-07-28` 为 `STATELESS_PER_REQUEST`；
   - SDK 支持的 2024/2025 revisions 为 `HANDSHAKE_SESSIONFUL`；
   - unknown revision fail closed。
5. 协议无状态不等于 Pulsara 无状态。server slot、snapshot、binding、pending lease、RuntimeSession ledger 与 continuation carrier 仍由 Pulsara 拥有。
6. Apps、Tasks、Roots、Sampling、Logging 不属于本轮 production capability。不得从 SDK 可导入类型推断产品支持。

## 2. Authority 拓扑

```text
HostSession
  -> McpServerSupervisor
       -> per-server slot generation
            -> SDK client generation
            -> exact protocol binding + wire receipt
            -> endpoint/auth attribution
            -> installed snapshot
            -> operation/subscription owners

RuntimeSession
  -> event commit/confirm/reducer/publication gateway
  -> MCP lifecycle reducer
  -> secure continuation transaction companion

McpToolExecutionPort
  -> ordinary call lease
  -> pending continuation handle
  -> stable suspension/resolution/dispatch/terminal candidate joins
```

每个 READY snapshot 必须携带 registered `McpServerSnapshotAuthorityFact`，并可精确证明：

- protocol semantic；
- final negotiation wire receipt；
- client capability policy；
- endpoint/auth attribution；
- SDK client generation；
- discovery page receipts；
- tool rejection与provider projection；
- snapshot semantic identity。

Snapshot semantic 只覆盖 server/tool/resource/prompt 的逻辑 surface。config epoch、TTL、transport generation、auth generation、page receipt 和 wire operation ID 只属于 attribution。

## 3. Stable SDK facade

### 3.1 Negotiation

Stateless HTTP 必须执行两阶段 negotiation：

```text
probe
  -> resolve exact supported revision
final client adopts probe result
  -> send_discover(exact_revision) with real wire I/O
  -> validate final result
  -> adopt final result
  -> install McpFinalDiscoverWireReceiptFact
```

调用 cached `discover()`、复用 probe receipt 或设置 caller boolean marker 均不能进入 READY。final capability/catalog authority 取 final discover result，不要求与 probe byte-equal。

Handshake revisions 必须从 exact initialize result 生成 `McpLegacyInitializeWireReceiptFact`。两种 receipt branch 不能互换。

### 3.2 SDK conformance boundary

- listing 使用 SDK conformed tool set，因此 `x-mcp-header` validation/filtering 由 stable SDK 首先执行；
- tool/resource/prompt execution 使用 public `ClientSession.send_request()` raw-result seam；
- 三路result只接受exact `complete | input_required`；SDK保留的任何未知`resultType`必须typed fail-closed，不能terminalize为成功；
- 不使用高层 `call_tool()` 的 pre-return output validation作为 Pulsara authority；
- `structuredContent` 的 absent、explicit null、value 由独立 presence carrier 表达；raw base result物理删除该 field与alias；
- output mismatch只产生一次 physical call，Pulsara保存 typed mismatch，不重放 side effect；
- stdio owner只使用 public close/cancel contract，不读取 SDK private transport task。

## 4. Protocol与transport ownership

`McpTransportOwner` 是 closed union：stateless HTTP、handshake HTTP、stdio。Discovery listing与ordinary operation共用唯一concurrency resolver：

- Streamable HTTP + stateless era经 bounded semaphore并发；
- Streamable HTTP + handshake era串行；
- stdio在任意era都串行，现代revision不改变其physical ownership；
- 同一 MRTR continuation保持 single-flight；
- caller cancellation只detach waiter，physical owner由manager继续drain；
- reconnect安装新 generation，不得偷换旧 operation/pending binding；
- Host close先停止新operation与subscription admission，再drain physical task，最后释放SDK client、slot与supervisor。

final discover/initialize完成后先形成`McpSdkNegotiatedProtocolBinding`；该provisional binding只允许listing，不得进入manager installation或operation admission。完整listing必须构造新的final `McpSdkProtocolBinding`与唯一`McpSdkConformedClientGeneration`，其required non-empty listing accumulator必须相等，并绑定exact snapshot ID、snapshot semantic、完整authority fingerprint、final wire receipt与SDK client object。Manager installation和每个dispatch borrow都必须消费并重验final owner；不存在nullable accumulator或原地promote。Binding lease必须 exact join server、slot、snapshot、discovery generation、protocol semantic、endpoint、auth与tool contract。配置变化后旧 generation可以drain已准入operation，但不能取得新borrow。

## 5. Discovery、schema 与 provider projection

### 5.1 Schema authority

- `inputSchema` wire container必须是 JSON object，根 `type` 必须 exact 为 `object`；
- 缺根 type、non-object/boolean container均使单个tool不可暴露；不得自动补写；
- `outputSchema` container必须是object，但可以描述scalar、array或null根；
- 仅在缺失 `$schema` 时使用 JSON Schema 2020-12；显式 dialect原样保留；
- external `$ref`不得发起network fetch；
- schema depth/nodes/bytes受closed bounds限制；
- SDK-conformed schema递归冻结后成为tool semantic authority；
- provider projection是唯一model-visible schema owner，无法lossless投影的tool不暴露。

非法单个tool产生 typed rejection，不拖垮同server其他合法工具。Snapshot identity按排序后的semantic facts计算，不受server listing顺序影响。

### 5.2 Structured result

`structuredContent`可以是任意 JSON value。声明output schema时，字段缺失与explicit null必须分开验证。Text/Image/Audio/EmbeddedResource/ResourceLink均按typed result mapping进入preview或artifact；`isError=true`是合法application result，不是transport异常。

## 6. Cache 与 subscription

SDK opaque cache永久关闭，client构造必须 `cache=None`。

Pulsara只持有cache attribution，不把cache hint升级为semantic authority：

- `server/discover`、list/read cacheable methods使用closed enum；
- 每页保存request params fingerprint、cursor、ordinal、received time、TTL、scope与result fingerprint；
- wire hint presence必须由raw/model fields-set证明，SDK的`ttl=0`、`scope=private`默认值不能冒充server已发送字段；
- 同一paginated method的全部page必须使用exact相同cache scope，混合scope在factory与fact validator两层拒绝；
- 每页独立计算freshness；不声称跨页一致snapshot；
- 需要完整snapshot时从`cursor=None`重新拉取；
- restart/reconnect后monotonic freshness全部失效。
- TTL revalidation semantic不变时不得替换installed snapshot或client generation；它只生成绑定exact dispatch operation的`McpFreshnessRevalidationReceipt`；
- dispatch borrow必须引用installed full-authority fingerprint及matching freshness receipt；revalidation发现semantic变化时必须走safe-point安装new generation。

Subscription owner只将notification转成dirty reason并唤醒coalesced reconcile。它不能直接修改snapshot。

- dirty barrier之前已经准入的operation可以在exact旧generation上drain；
- listChanged、auth/config generation变化与reconnect禁止新的dispatch borrow，必须同步reconcile；
- TTL只有在显式policy允许时才可stale-once；默认同步reconcile；
- tool-not-found/invalid-params只触发future reconcile，不自动重放side-effecting call；
- reconnect先完成full reconcile，之后才重新信任subscription；
- notification丢失由next-use freshness检查或explicit reconcile修复。

## 7. Secure MRTR

### 7.1 Leg vocabulary

InputRequired result必须lower成closed union：

- `McpStateOnlyRetryLeg`：只有opaque string `requestState`，按50/100/200/250ms bounded schedule自动重试，不写event、不建secret row、不进入WAITING_USER；
- `McpClientInputRequiredLeg`：至少一个typed input request，进入secure suspension。

只支持 `elicitation/create`。Sampling/Roots或unknown method typed reject。SDK elicitation callback只有在form与URL Host ports、continuation codec及secret repository同时READY时安装；因SDK v2 capability builder会同时广告两种mode，不能只广告form。

### 7.2 Elicitation batch

每个round由唯一 `McpElicitationBatchOwner` 拥有：

- request map按canonical key排序并冻结request-set fingerprint；
- form与URL使用discriminated request/response；
- 每个key有独立process-local状态；
- 只有exact full response key set可冻结唯一resolution；
- missing/unknown/duplicate/mode mismatch全部拒绝；
- partial progress与URL consent不持久化；caller cancellation只detach；
- reopen从durable request重建全部items，并重新取得URL consent。

URL mode必须显示exact full URL和domain，取得显式one-shot同意后才调用system browser。Consent前禁止HEAD/GET/DNS/favicon/prefetch；browser owner不返回page content、redirect target或用户输入，URL response的content必须absent。

### 7.3 Secret boundary

Form response、round response set、retry plaintext与解密carrier统一继承 `SealedMcpContinuationSecretBase`：

- constant redacted repr；
- pickle、`dataclasses.asdict`、`model_dump`和generic sink fail closed；
- 不进入EventLog、ordinary ArtifactStore、Inspector、log、diagnostic或model input；
- durable event只保存domain-separated keyed commitment与不覆盖raw hash的safe attribution；
- 不保存raw/entry/set ordinary SHA fingerprint。

Event-safe authority继承registered `FrozenFactBase`。Encrypted envelope/control继承独立registered `FrozenStorageFactBase`，只能进入typed secret repository；EventLog writer在类型层拒绝storage fact。

### 7.4 Retry carrier

Retry plaintext只保存sealed method-specific base params：tool call、resource read或prompt get。它明确排除JSON-RPC ID、`_meta`、protocol/trace/progress stamps、旧responses、旧requestState与transport metadata。每次wire retry由当前SDK generation生成fresh envelope，并只附加current-round responses与latest requestState。

所有carrier使用统一 `McpContinuationBoundsFact`，在event/row write admission前限制requestState、base params、input requests、responses、private URL、plaintext、ciphertext、stored envelope、round与TTL。

## 8. Durable continuation transaction

### 8.1 Suspension

Pre-commit先准备ciphertext并reserve process-local pending lease。随后唯一RuntimeSession transaction原子提交：

1. ordered suspension event batch；
2. materialization-account suspension charge/CAS；
3. encrypted awaiting carrier row。

process-local lease不进入PostgreSQL transaction。FULL confirm pending owner；NONE保留byte-identical candidate/owner重试；UNKNOWN latch；CONFLICT不暴露WAITING_USER。

### 8.2 Resolution与dispatch

Resolution transaction以distinct replay carrier替换awaiting carrier。Dispatch前必须提交：

1. `McpContinuationDispatchReservedEvent`；
2. exact physical reservation attribution；
3. secret control row `REPLAY_READY -> DISPATCH_RESERVED` CAS；
4. materialization-account transition。

只有FULL后才能physical send。Durable dispatch fact不包含会变化的account revision/fingerprint；process-local commit guard在writer lock内读取latest account并验证active reservation。NONE只刷新guard/deadline，stable event、companion batch与carrier不变。UNKNOWN阻止mutation；FULL后crash不得自动replay，reopen必须保守terminalize。

Companion identity绑定exact ordered event IDs、historical schema identities、sequence-null candidate fingerprints与batch accumulator。EventLog分配sequence后用exact historical schema将stored event normalize回`sequence=null`并生成typed rebind receipt；reorder、subset、payload substitution、schema drift、非sequence mutation和sequence gap均在secret mutation前拒绝。

### 8.3 Expiry与terminal delete

Initial awaiting、resolution、dispatch、successor与storage envelope共享唯一operation expiry。Successor round、reconnect、rewrite或key rotation不得续期。Terminal settlement经独立companion原子删除carrier；expiry/mismatch/key unavailable只能fail closed，不得产生plaintext fallback。

## 9. Restart recovery

只有以下状态可以在new process重绑：

- exact stateless `suspended` awaiting carrier；
- exact stateless `resolution_submitted` replay-ready carrier，且尚未dispatch reserve。

恢复顺序：

```text
fold exact lifecycle
-> real final discovery installs current generation
-> exact-read source suspension/resolution + encrypted row
-> decrypt and recompute full commitment
-> join protocol semantic, endpoint, auth, snapshot/tool contract
-> recover pending lease and batch owner
-> install RunOwner suspension or new resume activation
```

Physical slot/generation可以变化，但semantic target必须精确相同，并生成 `McpStatelessRecoveryRebindReceipt`。Handshake-era、disabled store、dispatch-reserved、expired、wrong key或authority mismatch不得replay，必须由reopen terminalizer写closure、error ToolResult、settlement与RunEnd，并在同事务删除carrier。Main与child reopen都必须经持有exact continuation repository的MCP execution/recovery port构造`TERMINAL_DELETE` companion；production terminalizer不存在repository缺省或`None`成功分支。

## 10. Auth、HTTP 与 trace

### 10.1 Static auth

- static headers、env headers与bearer env是V1唯一production auth；
- header values不进入event-safe config、snapshot、diagnostic或普通hash；runtime identity使用process-owned HMAC；
- continuation启用时auth attribution使用同一stable keyed commitment，支持restart exact join；
- protocol-managed、`Mcp-*`、trace、Host/Content headers不能由config覆盖；
- bearer owner与manual Authorization不能并存；
- redirect默认并实际禁止，避免credential跨origin泄漏。

OAuth未启用。未来只有在issuer exact validation、issuer-partitioned credential store、scope/refresh owner、browser authorization与Host WAITING_USER边界全部具备后才能新增typed profile。不得启用SDK隐式OAuth store。

### 10.2 Trace

HTTP通过process-local W3C hook注入`traceparent`，可选`tracestate`，baggage只允许closed、bounded、redacted key。Trace context不进入tool/snapshot/binding semantic fingerprint、continuation carrier或replay authority。Telemetry hook/export failure必须被隔离，不能改变ToolResult、retry或owner结算。

## 11. Capability、permission 与 rollout

MCP provider与adapter必须从同一个installed snapshot构造。非READY server不产callable tool。Tool name mangling稳定且碰撞fail closed。

执行顺序固定为capability exposure、permission、long-horizon phase/rollout reservation、ToolExecutor、MCP port。Server annotation只能提供descriptor hint，不能绕过permission。Resume前重新执行capability与permission检查；DENY与unsupported WAIT写typed denied terminal，不调用provider。

Manager slot lease与rollout reservation是两个独立owner。Suspension保留两者的exact attribution；resume success、deny、cancel、timeout、protocol failure和terminal commit outcome均必须分别settle。

## 12. Observability与product boundary

CLI doctor/Inspector只能读取secret-safe snapshot/event authority，显示exact revision、behavior era、wire receipt、cache page摘要与typed diagnostics。历史解释不得query live manager。

模型只看到typed input request、bounded resolution status和最终ToolResult；不看到requestState、form values、private URL response、ciphertext或carrier identity。

Meta-tool surface不因SDK升级改变。Apps/Tasks不进入registry；Roots/Sampling/Logging不广告。

## 13. Architecture gates

必须持续满足：

- production SDK/httpx2 import仅存在于`runtime/mcp/sdk.py`；
- lock与source无beta/RC compatibility branch；
- durable behavior era无年份命名；
- event serializer拒绝storage-only fact；secret repository拒绝event fact；
- no process-local owner进入authority serializer；
- no legacy elicitation DTO、secret-bearing fingerprint或SDK private transport access；
- RuntimeSession仍是唯一event commit/confirm/reducer/publication gateway；
- Apps/Tasks/Roots/Sampling/Logging无新增production exposure。

## 14. Required verification

每次改变本契约至少运行：

```text
tests/test_mcp_architecture.py
tests/test_mcp_v2_contracts.py
tests/test_mcp_v2_sdk.py
tests/test_mcp_sdk_discovery.py
tests/test_mcp_tool_execution_port.py
tests/test_mcp_input_required_lifecycle.py
tests/test_mcp_host_lifecycle.py
tests/test_mcp_restart_recovery.py
tests/test_capability_mcp.py
tests/test_schema_migrations.py
tests/test_real_mcp_dogfood.py   # explicit real-bench gate
```

Hard cut完成的最低机器证据包括：stable dependency/lock、real wire final discover、strict schema corpus、cache/subscription dirty matrix、secure MRTR FULL/NONE/UNKNOWN/CONFLICT、sequence-null rebind、restart exact/mismatch paths、key/expiry/delete repair、Host close drain、secret canary sink audit、architecture guards、全量pytest与real stable MCP bench。
