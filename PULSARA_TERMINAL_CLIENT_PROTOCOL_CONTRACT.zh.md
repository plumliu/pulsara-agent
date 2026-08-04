# Pulsara Terminal Client Protocol Contract

> 状态：PYTHON BOUNDARY与Go-ready Protocol 2.0 subcut IMPLEMENTED；Bubble Tea S1 consumer IMPLEMENTED；S2–S6消费、正式packaging与默认TTY activation为DEFERRED
> Requirement namespace：`TUI-PROTO-*`
> 唯一owner：Python TerminalClientGateway与外部Terminal client之间的本地wire contract
> Domain authority：`PULSARA_TERMINAL_PRESENTATION_FOUNDATION_IMPLEMENTATION.zh.md`
> 产品行为：`PULSARA_TERMINAL_UI_UX_RESEARCH_AND_DESIGN.zh.md`

## 0. 核心裁决

长期目标中的Bubble Tea是独立Go client，不能import Python DTO；Python runtime也不能import Go client类型。当前INFRA-5只实现renderer-neutral Python server adapter与test-only headless conformance client，并通过同一versioned local protocol证明边界；它不实现或伪装Go renderer。

V1冻结：

- Protocol Buffers是唯一wire schema真源；
- 本地POSIX transport使用Unix domain `SOCK_STREAM`；
- framing使用fixed 4-byte big-endian length加一个Protobuf `TerminalFrame`；
- 不使用stdout/stderr传protocol；Bubble Tea独占TTY；
- 不在V1引入gRPC、HTTP、WebSocket或远程network listener；
- domain DTO与wire DTO是不同类型，必须由显式adapter映射；
- wire message不成为EventLog、queue或interaction authority。

## 1. 非目标

- 远程公网client；
- browser/Desktop协议；
- Windows named pipe；
- arbitrary plugin message；
- raw `AgentEvent`、`RawStoredEventEnvelope`或storage receipt传输；
- generic RPC method name加free-form JSON；
- server端renderer；
- secret frame持久化或重放。

## 2. Source tree与schema ownership

```text
src/pulsara_agent/terminal_protocol/
    schema/terminal_client.proto                   # unique wire definition
    schema/terminal_client_fingerprint_contract.v1.json  # GO-READY-A
    schema/terminal_client_fingerprint_golden.v1.json    # GO-READY-A
    generated/terminal_client_pb2.py               # generated Python
    generated/terminal_client_fingerprint.py       # GO-READY-A generated helper
    codec.py                                       # explicit domain/wire adapter
    gateway.py                                     # Unix-socket server and framing owner
src/pulsara_agent/runtime/terminal_application/
    control_projection.py                          # GO-READY-B session-owned signal
tools/generate_terminal_protocol_contract.py       # GO-READY-A generator
tests/support/terminal_protocol.py                 # test-only headless consumer

# DEFERRED；当前hard cut不得创建：
clients/terminal/                                  # future generated Go + renderer
```

Generated files不得手工修改。Python domain classes不得继承generated message；未来Go presentation model不得直接保存generated message作为长期state。Headless client必须位于`tests/support`并只经正式socket/framing/schema访问server。

Architecture gate：

```text
Python Foundation -> protocol domain adapter -> generated protobuf
Go generated protobuf -> client transport adapter -> Bubble Tea model messages

Foundation -X-> generated protobuf
Headless conformance client -X-> HostSession/RuntimeSession internals
Future Bubble Tea model -X-> Python domain/event vocabulary
```

## 3. Physical transport

### TUI-PROTO-TRANSPORT-001 Runtime directory

Gateway必须在仅当前UID可访问的private runtime directory中创建socket：

- directory mode `0700`；
- socket mode `0600`；
- parent、directory和socket owner UID必须等于current effective UID；
- 拒绝symlink、不安全ancestor或group/world writable replacement；
- socket path必须满足目标平台`sun_path`上限；
- stale socket只能在证明owner process不存在且identity匹配后删除。

Resolver优先使用owned/safe `XDG_RUNTIME_DIR`；否则使用短、UID-bound、随机化的private runtime path。不得在workspace内创建socket或token。

### TUI-PROTO-TRANSPORT-002 Peer validation

accept后先执行physical admission：

- 读取peer credentials并验证UID；
- 安装pre-negotiation hard frame cap、connection ID与input/output byte budgets；
- 只允许读取一个bounded `TerminalTransportAuthPreface`，不得先读取`ClientHello`或其他application frame；
- 验证initial launch credential或reconnect credential后，才进入Hello状态机；
- Hello选择protocol后再安装negotiated frame cap与major/minor decoder。

```text
TerminalTransportAuthPreface
  preface_version = 1
  auth_request_id
  client_instance_id
  handshake_candidate_id
  handshake_candidate_fingerprint
  connection_nonce: bytes[32]
  credential = oneof {
    InitialLaunchCredential
      launch_id
      launch_capability: bytes
    ReconnectCredential
      reconnect_credential_id
      reconnect_capability: bytes
      previous_attachment_id
      previous_attachment_generation
  }
  preface_fingerprint

TerminalTransportAuthResult
  auth_request_id
  auth_attempt_id
  connection_id
  client_instance_id
  credential_id
  disposition: AUTHENTICATED | COMPATIBLE_AUTH_WINNER | ACK_RESULT_RECOVERY | AUTHENTICATION_REJECTED
  authenticated_candidate_fingerprint: optional
  recovered_attach_ack_result: optional AttachAckResult
  recovered_transport_binding: optional RecoveredAttachmentTransportBinding
  public_rejection_code: optional closed enum
  result_fingerprint

RecoveredAttachmentTransportBinding
  previous_transport_binding_fingerprint
  resulting_transport_binding: TerminalClientTransportBindingIdentity
  disposition: REBOUND | COMPATIBLE_ALREADY_REBOUND
  rebind_receipt_fingerprint
```

Preface与Result复用`uint32_be length + protobuf payload`物理framing，但在协商前固定16 KiB hard cap与同一个2秒absolute deadline。`auth_request_id`等于client为本次physical auth effect预装的RequestID；`auth_attempt_id`由server auth owner在接收完整preface后生成并在该connection/result内唯一。Initial branch的`credential_id = launch_id`，reconnect branch等于`reconnect_credential_id`。Client只有读取并验证matching `TerminalTransportAuthResult`后才可进入Hello；socket write成功绝不构成authenticated。

Result matrix：

| disposition | Required | Forbidden |
|---|---|---|
| `AUTHENTICATED` | matching request/client/credential/connection、candidate fingerprint | recovered ACK、rejection code |
| `COMPATIBLE_AUTH_WINNER` | 同上，且existing winner candidate exact相等 | recovered ACK、rejection code |
| `ACK_RESULT_RECOVERY` | matching candidate fingerprint、完整exact `AttachAckResult`、current connection的`RecoveredAttachmentTransportBinding` | ordinary authenticated continuation、rejection code |
| `AUTHENTICATION_REJECTED` | oracle-safe public rejection code | candidate authority、recovered ACK/rebind |

Unknown branch、zero/trailing bytes、credential/client-instance不匹配均typed close。Conflict identity严格以`credential_id + auth_request_id`为scope：同一`auth_request_id`只接受byte-identical preface fingerprint，same request ID + different payload是`CANDIDATE_CONFLICT`；同一credential/candidate的下一次physical retry必须使用新的request ID、fresh nonce与新的preface fingerprint，并被正常接纳。Server按request ID保存bounded attempt receipt，不能把credential ID当成physical-request idempotency key。若同一credential在前一个attachment attempt尚未terminal时提交不同candidate，或candidate generation不满足下一节的successor规则，才是candidate conflict。Authentication rejection只暴露`INVALID_OR_EXPIRED_CREDENTIAL | CANDIDATE_CONFLICT | AUTH_OWNER_UNAVAILABLE`三种oracle-safe public code，随后close。Result fingerprint使用Protocol manifest中`terminal-transport-auth-result:v1` helper覆盖除自身外的所有non-secret fields，以及nested ACK fingerprint与rebind receipt fingerprint；Preface中的secret bytes只允许存在于bounded decoder buffer和transport-auth owner，不能进入Result、`ClientHello`、AppState、ordinary message、log或panic dump。

`TerminalClientTransportBindingIdentity.binding_fingerprint`使用`terminal-client-transport-binding:v1`覆盖除自身外的全部binding fields；`RecoveredAttachmentTransportBinding.rebind_receipt_fingerprint`使用`terminal-attachment-transport-rebind:v1`覆盖previous binding fingerprint、完整resulting binding fingerprint与closed disposition。两个helper都属于`WIRE_RECOMPUTABLE`，不得把arrival time、socket FD、client connection handle或operation/request ID混入binding/rebind identity。

Initial launch credential不是“Hello一发出就不可恢复”的fire-and-forget token。Server process-local auth owner保存稳定状态：

```text
ISSUED
  -> PREFACE_ACCEPTED
  -> HELLO_ACTIVE
  -> ATTACH_RESULT_ISSUED -> ATTACH_ACKNOWLEDGED -> ACK_TOMBSTONE
  |  CANDIDATE_TERMINAL  -> TERMINAL_OUTCOME_TOMBSTONE
  -> RETIRED
```

在`ATTACH_ACKNOWLEDGED | CANDIDATE_TERMINAL`前，同一`client_instance_id + credential + HandshakeRecoveryCandidateIdentity`可在新connection上取回compatible Hello/Attach winner或same candidate terminal receipt，覆盖Hello/Attach/negative-outcome response丢失；physical connection/request/operation generation不参与candidate equality。Attach response丢失必须生成本次connection的`AttachResultReceipt(REBOUND_PRE_ACK | COMPATIBLE_ALREADY_REBOUND_PRE_ACK)`，不能重放旧request ID/binding。Negative Hello response丢失只能返回same terminal receipt + current-request outcome，不得再返回accepted winner。前一代尚未terminal时出现不同candidate是conflict；前一代已Ready或已持有terminal receipt后的successor必须使用exact next `attachment_attempt_generation`，不能误判成compatible old winner。

Attach ACK FULL后live initial credential bytes立即清除，但server保留最多一个`AcknowledgedHandshakeTombstone`：credential keyed commitment、client/candidate identity、Attach winner、exact `AttachAckResult`、current physical transport binding generation与`recovery_expires_at`。Tombstone TTL固定30秒且不得超过attachment/session lifetime；它可以越过原credential的live-use expiry，因为它没有建立新attachment、重新Hello或执行command的权限，只能返回已经FULL的exact ACK并把同一个attachment物理rebind到本次authenticated connection。

Client在未收到AckResult时继续保留initial credential；新connection preface由server以process auth key重算commitment。Matching tombstone在attachment/transport-auth owner的同一process lock内验证旧connection已失效或被本candidate取代，然后CAS递增`transport_binding_generation`，保持attachment ID/generation、role、controller disposition和lease expiry不变，返回`TerminalTransportAuthResult(ACK_RESULT_RECOVERY, recovered_attach_ack_result, recovered_transport_binding)`。同一result write丢失后的再次请求可把same attachment rebind到更新connection并返回新physical binding receipt；旧connection generation立即失效。它不得重新进入Hello/Attach、创建新attachment、延长attachment lease或改变domain authority。Client逐项验证ACK与binding、原子替换`AttachmentState.ConnectionID`/binding generation后才可在该connection请求snapshot，随后清除credential。TTL到期则typed auth rejected，由parent重新launch；tombstone数量每个launch/client最多一个，不随retry增长。

S2启用reconnect rotation后，current reconnect credential遵守同一compatible-winner规则；next credential必须先装入client owner。Old credential在Attach ACK FULL后转相同bounded tombstone，不能在response写出时提前删除；next credential成为current。`AUTHENTICATED | COMPATIBLE_AUTH_WINNER`之后才发送Hello；`ACK_RESULT_RECOVERY`直接产生recovered AttachAcknowledged outcome并结束本次旧handshake，不发送Hello。

仅依赖filesystem mode不够。Peer credential不可用的平台不在POSIX V1 support matrix。

### TUI-PROTO-TRANSPORT-003 Framing

```text
uint32_be payload_length
payload_length bytes of TerminalFrame protobuf
```

规则：

- `payload_length == 0`非法；
- 大于negotiated hard max时立即close；
- frame不得压缩；
- decoder必须在分配payload buffer前验证上限；
- partial read由transport owner聚合，不向application暴露；
- malformed protobuf、unknown required enum或invalid oneof typed close；
- ordinary与secret frame使用同一physical stream但不同closed envelope branch。
- V1没有request multiplexing、wire cancellation或response sequence tag；request fully sent后发生read deadline/partial-frame timeout时，client必须使整个connection/binding invalid并等physical reader退出，不得在同一stream上发successor request。只能在fresh authenticated binding上重建可重试read；迟到旧response必须随旧stream一起丢弃。

## 4. Protocol hello与compatibility

### TUI-PROTO-HELLO-001 Version fact

```text
ProtocolVersion
  major
  minor
  schema_contract_fingerprint
  minimum_compatible_minor
```

Major不同时fail closed。Minor只在server声明区间内协商；不能仅依赖Protobuf unknown-field tolerance。

### TUI-PROTO-HELLO-002 Handshake

```text
HandshakeRecoveryCandidateIdentity
  candidate_version = 1
  candidate_id
  client_instance_id
  attachment_attempt_generation
  host_session_id
  requested_runtime_session_id
  requested_attachment_role
  minimum_protocol_major/minor
  maximum_protocol_major/minor
  client_build_identity
  supported_capabilities: unique sorted enum set
  required_capabilities: unique sorted enum set
  candidate_fingerprint

ClientHello
  request_id
  transport_auth_attempt_id
  transport_auth_result_fingerprint
  handshake_candidate: HandshakeRecoveryCandidateIdentity

HelloNegotiationSemanticWinner
  handshake_candidate_id/fingerprint
  attachment_attempt_generation
  selected protocol
  server build/runtime compatibility identity
  negotiated limits
  server_supported_capabilities: unique sorted enum set
  selected capabilities
  capability contract fingerprint
  negotiation_transcript_fingerprint
  negotiation_winner_fingerprint

ServerHelloReceipt
  request_id
  transport_auth_attempt_id
  handshake_candidate_id/fingerprint
  negotiation_winner_fingerprint
  current_connection_id
  attachment_challenge: bytes[32]
  attachment_challenge_commitment
  hello_receipt_fingerprint

ServerHello
  negotiation_winner: HelloNegotiationSemanticWinner
  receipt: ServerHelloReceipt

HandshakeCandidateTerminalDisposition =
    NEGOTIATION_WINNER_UNAVAILABLE
  | HELLO_REJECTED

HelloCandidateTerminalReason =
    FROZEN_WINNER_BINDING_MISSING
  | FROZEN_WINNER_CONTRACT_UNSUPPORTED
  | FROZEN_WINNER_RUNTIME_INCOMPATIBLE
  | PROTOCOL_RANGE_UNSUPPORTED
  | SCHEMA_CONTRACT_UNSUPPORTED
  | MISSING_REQUIRED_CAPABILITY
  | CANDIDATE_EXPIRED
  | SERVER_NEGOTIATION_POLICY_REJECTED

HelloCandidateClientDisposition =
    PARENT_RELAUNCH_NEW_CANDIDATE
  | FATAL_COMPATIBILITY

HandshakeCandidateTerminalReceipt
  handshake_candidate_id/fingerprint
  attachment_attempt_generation
  terminal_disposition: HandshakeCandidateTerminalDisposition
  terminal_reason: HelloCandidateTerminalReason
  required_client_disposition: HelloCandidateClientDisposition
  prior_negotiation_winner_fingerprint: optional
  candidate_registry_revision
  terminal_receipt_fingerprint

HelloNegotiationWinnerUnavailable
  request_id
  transport_auth_attempt_id
  current_connection_id
  handshake_candidate_id/fingerprint
  candidate_terminal_receipt: HandshakeCandidateTerminalReceipt
  outcome_fingerprint

HelloRejected
  request_id
  transport_auth_attempt_id
  current_connection_id
  handshake_candidate_id/fingerprint
  candidate_terminal_receipt: HandshakeCandidateTerminalReceipt
  outcome_fingerprint

HelloOutcome =
    ServerHello
  | HelloNegotiationWinnerUnavailable
  | HelloRejected

AttachRequest
  request_id
  handshake_candidate_id/fingerprint
  negotiation_winner_fingerprint
  current_hello_receipt_fingerprint
  attachment_challenge: bytes[32]
  attachment_challenge_commitment

AttachSemanticWinner
  handshake_candidate_id/fingerprint
  attachment_attempt_generation
  hello_negotiation_winner_fingerprint
  attachment identity
  controller disposition
  bootstrap requirement
  heartbeat/lease policy
  optional next_reconnect_credential_public_identity: ReconnectCredentialPublicIdentity
  semantic_winner_fingerprint

AttachResultReceipt
  request_id
  transport_auth_attempt_id
  handshake_candidate_id/fingerprint
  attach_semantic_winner: AttachSemanticWinner
  current_transport_binding: TerminalClientTransportBindingIdentity
  previous_transport_binding_fingerprint: optional
  disposition: CREATED | REBOUND_PRE_ACK | COMPATIBLE_ALREADY_REBOUND_PRE_ACK
  optional next_reconnect_credential_carrier: ReconnectCredentialCarrier
  receipt_fingerprint

AttachReceiptAck
  request_id
  attachment identity / semantic winner fingerprint
  current transport binding identity
  attach result receipt fingerprint
  installed reconnect credential commitment（S2+）
  ack fingerprint

AttachAckResult
  request_id
  attachment identity / semantic winner fingerprint
  acknowledged_transport_binding_fingerprint
  disposition: ACKNOWLEDGED | COMPATIBLE_ALREADY_ACKNOWLEDGED
  retired_credential_id
  ack_result_fingerprint
```

`AttachSemanticWinner.controller_disposition`只能使用Protocol behavioral vocabulary中的三个exact values；`CONTROLLER_UNAVAILABLE_OBSERVER_ATTACHED`已经完整表达“请求controller但只获得observer”。`controller held by other`、reconciliation banner或client action enablement若需要展示，只能来自后续atomic control projection或client-local rendering state，不得成为第四/第五个Attach disposition，也不得进入semantic winner fingerprint。`bootstrap_requirement`在V1只有`PROJECTION_AND_OPERATIONAL_SNAPSHOT_REQUIRED`一个合法值；不存在none、durable-only或client推导分支。

`AttachAckResult`必须作为完整validated carrier交付到client application boundary，不能在wire adapter中降成disposition + result fingerprint。Ordinary ACK保留request、attachment、semantic winner、`acknowledged_transport_binding_fingerprint`、retired credential与完整result fingerprint；`ACK_RESULT_RECOVERY`保留同一个完整nested ACK，并额外携带当前connection的typed rebind receipt。Nested ACK确认的是原FULL ACK所绑定的transport binding；recovery branch的resulting binding是本次physical rebind，两者不得折叠或相互覆盖。

`HelloNegotiationSemanticWinner.negotiated_limits`使用closed carrier，至少包含`maximum_frame_bytes`、`maximum_history_page_cells`、`maximum_history_page_decoded_bytes`、`maximum_observation_wait_ms`、`maximum_active_queue_items`、`maximum_server_control_notifications`、`maximum_operational_activity_cells`、三项per-plane observation bytes、`maximum_observation_batch_bytes`与`secret_frame_maximum_bytes`。Protocol 2.0固定：active queue 64、server notifications 16、operational activity 256、durable/operational/control observation分别4 MiB/1 MiB/256 KiB、完整batch 6 MiB，且batch hard max严格小于8 MiB frame max。Zero、larger/smaller drift或与对应projection/observation contract fingerprint冻结bound不一致使Hello拒绝。这些limit是server contract proof，不是client请求配额，也不能被client用来截断snapshot或省略pending plane。

`HelloNegotiationSemanticWinner`是同一handshake candidate在所有pre-Ready physical retry间唯一稳定的协商结果。Server negotiation owner第一次成功协商时CAS安装winner；之后即使process-local capability/config发生变化，同candidate也只能返回byte-identical winner，不能用current config重算另一个selected protocol/capability/limit集合。Ordinary Ready reconnect使用next candidate，才允许产生新的negotiation winner。

若旧winner已无法被当前server build安全兑现，negotiation owner必须在同一candidate-registry CAS中把当前candidate从ACTIVE收口为`HandshakeCandidateTerminalReceipt(NEGOTIATION_WINNER_UNAVAILABLE, exact closed reason, PARENT_RELAUNCH_NEW_CANDIDATE, prior winner fingerprint)`，然后返回`HelloNegotiationWinnerUnavailable`。有效ClientHello的其他闭集协商拒绝必须先CAS成`HELLO_REJECTED`再返回`HelloRejected`；malformed/unknown required wire在decoder boundary直接typed close，不伪造outcome。两个negative outcome的nested terminal receipt跨physical retry稳定，outer outcome只换current request/auth attempt/connection attribution。Same candidate retry只能取回compatible identical terminal receipt，same generation different terminal reason/disposition为conflict。

Terminal matrix唯一为：`NEGOTIATION_WINNER_UNAVAILABLE`只允许前3个`FROZEN_WINNER_*`原因，必须携`prior_negotiation_winner_fingerprint`且client disposition固定`PARENT_RELAUNCH_NEW_CANDIDATE`；`HELLO_REJECTED + CANDIDATE_EXPIRED`不携prior winner，client disposition也是`PARENT_RELAUNCH_NEW_CANDIDATE`；其余`PROTOCOL_RANGE_UNSUPPORTED | SCHEMA_CONTRACT_UNSUPPORTED | MISSING_REQUIRED_CAPABILITY | SERVER_NEGOTIATION_POLICY_REJECTED`只能配`HELLO_REJECTED + FATAL_COMPATIBILITY`且禁止prior winner。Incoming same-generation/different-fingerprint `CANDIDATE_CONFLICT`不属于Hello terminal reason；它在transport-auth/candidate-registry admission中typed close，不得修改已安装candidate的ACTIVE/winner/terminal state。

Terminal receipt FULL后该candidate不再是“前一代尚未terminal”：server successor registry只允许parent以新launch/reconnect credential提交exact `attachment_attempt_generation + 1`的new candidate，或新`client_instance_id`的generation 1。旧credential只能在bounded 30-second terminal-outcome tombstone中查询same receipt，不能重新建立attachment。Go只消费`required_client_disposition`：`PARENT_RELAUNCH_NEW_CANDIDATE`再按reason映射`ParentRelaunchNegotiationWinnerUnavailable | ParentRelaunchHelloRejected`，`FATAL_COMPATIBILITY`直接进入fatal teardown且禁止auto relaunch；不得读`stable_error_code`决定control flow。

`ServerHelloReceipt`只拥有本次request/auth attempt/current connection与fresh attachment challenge，并只以fingerprint引用outer winner，不复制winner semantic fields。Challenge必须是CSPRNG产生的exact 32 bytes。Wire decoder验证commitment/receipt后只能把bytes安装为operation-bound `PREPARED` challenge record，绑定current Hello request/operation、connection、candidate、winner、receipt、commitment与absolute deadline；PREPARED record不能被Attach borrow。Application完成candidate/request/AppState exact join后，typed local promotion只能把它变为`ACTIVE_PENDING_APPLICATION_ACCEPTANCE`并返回promotion receipt；matching result被application接纳、再通过独立confirmation operation后才可成为ACTIVE。Promotion/confirmation result stale、drop、fatal或无法交付时，client runtime owner必须执行typed active-revoke；constructor/send failure、operation successor、new Hello receipt、connection close、deadline或teardown也必须idempotent revoke对应PREPARED/ACTIVE_PENDING/ACTIVE record。Ordinary AppState/message只保留opaque identity/receipt与commitment，不保存bytes。Hello response丢失后的new physical attempt复用same negotiation winner，但必须返回matching current request/connection receipt和fresh challenge；旧receipt与任意phase challenge须先收口revoke。`AttachRequest`同时exact引用stable negotiation winner、current receipt、从application-confirmed ACTIVE handle一次性borrow的challenge bytes与commitment，因此不能把未接纳的prepared/active-pending bytes或旧challenge装到new connection，也不能让physical request identity污染协商semantic winner。

Fingerprints固定为：

- `negotiation_transcript_fingerprint = H("terminal-hello-negotiation-transcript:v1", complete handshake candidate fingerprint、selected protocol、server compatibility identity、negotiated limits、server-supported capability set、selected capability set与capability contract fingerprint)`；
- `negotiation_winner_fingerprint = H("terminal-hello-negotiation-winner:v1", candidate ID/fingerprint、attempt generation、negotiation transcript fingerprint与上述完整semantic fields)`；
- `attachment_challenge_commitment = SHA-256(UTF8("terminal-attachment-challenge:v1") || 0x00 || canonical_json_utf8({"auth_attempt_id": ..., "candidate_fingerprint": ..., "candidate_id": ..., "connection_id": ..., "negotiation_winner_fingerprint": ..., "request_id": ...}) || 0x00 || uint32_be(32) || exact challenge bytes)`，object只含这6个required UTF-8 string fields且按TUI-PROTO-SCHEMA-003 canonical key order编码；wire形式为`sha256:` + 64位lowercase hex；
- `hello_receipt_fingerprint = H("terminal-server-hello-receipt:v1", request ID、transport-auth attempt、candidate ID/fingerprint、referenced negotiation winner fingerprint、current connection ID、attachment challenge commitment)`；
- `terminal_receipt_fingerprint = H("terminal-handshake-candidate-terminal:v1", candidate ID/fingerprint、attempt generation、terminal disposition/reason、required client disposition、optional prior winner fingerprint、candidate registry revision)`；negative `outcome_fingerprint`使用各自`terminal-hello-negotiation-unavailable:v1 | terminal-hello-rejected:v1`覆盖current request/auth attempt/connection、candidate identity与nested terminal receipt fingerprint。

前两项禁止request/auth attempt、connection、challenge、arrival time；receipt禁止challenge plaintext直接进入diagnostic，但generated Python/Go helper必须从exact bytes重算purpose-bound commitment，再重算receipt fingerprint。Challenge bytes codec只是上述固定32-byte binary framing，不得base64/string-normalize后再hash。Negotiation/winner/commitment/receipt/terminal outcome各有独立golden，所有列出的wire carrier fingerprint均属于`WIRE_RECOMPUTABLE`；`HandshakeCandidateTerminalReceipt`的安装/CAS authority仍只在server registry，client重算fingerprint不使它成为候选终结owner。

`HandshakeRecoveryCandidateIdentity`是“一次semantic attachment attempt”的compatible-winner candidate，不是整个client/session生命周期的身份。它使用`WIRE_RECOMPUTABLE` namespace `terminal-handshake-recovery-candidate:v1`；fingerprint覆盖除`candidate_id`与自身fingerprint外的全部列出字段，包含`attachment_attempt_generation`，`candidate_id = "handshake:" + fingerprint digest`，不存在自引用。Generation由client runtime owner从1开始单调递增：同一代覆盖pre-Ready auth/Hello/Attach/ACK的所有physical retries；client收到matching `AttachAckResult`并进入Ready，或收到matching negative `HelloOutcome`并安装candidate terminal receipt后，该代terminal。Ready后的ordinary reconnect或negative-Hello parent relaunch必须建立`generation + 1`的新candidate（新client instance则从1开始）；不得复用旧candidate命中旧winner。只有ACK result丢失的bounded tombstone recovery仍使用已经FULL的旧generation，且只能返回同一attachment的typed physical rebind。

Candidate在每代首次connect前由generated factory构造，完整carrier安装进client runtime owner；AppState只保存immutable identity/join fields与opaque handle。Requested session/role、client build、protocol range或capability set变化也必须创建下一代candidate，不能原地修改。它严格排除connection ID/generation、operation/request ID、nonce、credential ID、deadline、physical attempt count和timestamp。每次physical preface/Hello/Attach使用新的request/operation token，但同一pre-Ready retry必须引用同一candidate ID/fingerprint；server winner key为`client_instance_id + attachment_attempt_generation + candidate_fingerprint`并另行exact验证current/retiring credential authority。Generation回退、跳过已安装next generation或same generation different fingerprint均fail closed。

Server attachment registry是successor generation的唯一线性化owner。Initial candidate只允许generation 1；ordinary Ready reconnect的credential record冻结`previous_candidate_fingerprint`、`previous_attachment identity`与`expected_next_attachment_attempt_generation = previous + 1`；negative-Hello relaunch credential改为冻结`previous_candidate_terminal_receipt_fingerprint + expected next generation`。新semantic winner形成时必须在同一registry CAS中验证predecessor已是ACK FULL或candidate terminal FULL，并安装successor；Ready predecessor还需把previous attachment标记为`SUPERSEDED_BY_RECONNECT`，使旧socket/binding/heartbeat从该点起fail closed。CAS NONE复用same candidate retry，compatible winner返回same semantic winner/terminal receipt + current physical outcome，conflict或generation跳跃不得创建第二attachment。Pre-ACK/ACK-result recovery不执行successor CAS，只更新同一winner的physical binding。

`AttachSemanticWinner`是该candidate唯一稳定的semantic attachment结果；它必须exact引用已经安装的`HelloNegotiationSemanticWinner.negotiation_winner_fingerprint`，Attach owner不得重新读取current capability/config或只信任physical hello receipt。它不包含request ID、connection/binding、arrival time或physical receipt identity。`AttachResultReceipt`只是一次connection-bound delivery receipt。第一次成功attach使用`CREATED`；若receipt在ACK前丢失，client以same candidate做新的physical auth/Hello/Attach，server保持Hello/Attach两个semantic winner不变，在attachment owner内CAS递增transport binding generation、使旧binding失效，并返回`REBOUND_PRE_ACK`的新receipt；同一new binding的exact duplicate返回`COMPATIBLE_ALREADY_REBOUND_PRE_ACK`。Receipt request ID必须等于本次`AttachRequest`，binding必须指向current connection，previous binding只在两个rebind branch出现。Client先验证Hello winner、Attach winner与current receipt三层exact join，再安装本次binding和可重复交付的same reconnect credential carrier，不能接受旧request ID、旧challenge或旧binding。

两层fingerprint均由Protocol manifest生成且无交叉污染：

- `semantic_winner_fingerprint = H("terminal-attach-semantic-winner:v1", candidate ID/fingerprint、attachment attempt generation、hello negotiation winner fingerprint、完整attachment semantic identity、controller disposition、bootstrap requirement、heartbeat/lease policy、可选next reconnect credential的non-secret identity/commitment)`；禁止request ID、auth attempt、connection/binding、delivery disposition、carrier plaintext与arrival time；
- `receipt_fingerprint = H("terminal-attach-result-receipt:v1", current request ID、transport-auth attempt、candidate ID/fingerprint、nested semantic winner fingerprint、current transport binding fingerprint、optional previous binding fingerprint、closed disposition、可选reconnect carrier fingerprint)`；禁止把socket FD、local connection handle或arrival time混入；
- `ReconnectCredentialCarrier.carrier_fingerprint = H("terminal-reconnect-credential-carrier:v1", credential ID、client/attachment identity、issued/expiry、credential commitment)`；它不覆盖capability plaintext，但commitment已purpose-bind secret bytes。

三者都是`WIRE_RECOMPUTABLE`且各有独立golden。相同semantic winner配不同current receipt是合法pre-ACK retry；相同receipt identity配不同nested winner/binding/carrier fingerprint是conflict。任何一层都不得以另一层fingerprint替代。

`next reconnect credential public identity/carrier`的closed shape是：

```text
ReconnectCredentialPublicIdentity
  reconnect_credential_id
  client_instance_id
  attachment_id
  attachment_generation
  issued_at_utc
  expires_at_utc
  credential_commitment
  public_identity_fingerprint

ReconnectCredentialCarrier
  public_identity: ReconnectCredentialPublicIdentity
  reconnect_capability: bytes[32..64]
  carrier_fingerprint
```

Capability使用CSPRNG，TTL最多30分钟且不得超过attachment/session lifetime；commitment固定为`HMAC-SHA256(process auth key, "terminal-reconnect-credential:v1" || 0x00 || canonical credential ID/client/attachment fields || capability bytes)`的`hmac-sha256:`lowercase hex，不能以plaintext hash替代；process auth key不出server owner。Public identity fingerprint覆盖除自身外的全部non-secret fields。Carrier只在selected `RECONNECT_AUTH_ROTATION_V1`时出现，且其nested public identity必须byte-identical于semantic winner中的optional public identity。Generated decoder验证non-secret fields后必须立即把mutable bytes安装进client runtime owner并清除carrier副本；AppState/message只保留semantic winner中的public identity、opaque handle与carrier fingerprint。Server auth owner保存current + at most one retiring credential，Attach ACK FULL或grace/expiry后删除retiring value；数量不能随reconnect增长。

Transport credential不进入Hello transcript或capability semantic fingerprint；Hello只引用已验证transport-auth attempt ID。`AttachSemanticWinner`与`AttachResultReceipt`都是process-local protocol authority，不是durable EventLog fact；前者跨同代physical retry稳定，后者只绑定本次request/connection。Client必须先把S2+ reconnect credential安装进`ClientRuntimeOwner`，再发送同时绑定winner + receipt + current binding的`AttachReceiptAck`；ACK发送失败不得清除old credential。Client capability不得决定server domain semantics；它只决定可选择的presentation/transport branch。Server不应向不支持某view branch的client发送无法解析的required message。

### TUI-PROTO-HELLO-003 Capability negotiation result

当前`repeated string supported_capabilities`只记录client自报集合，不能作为Go client的准入证明。S1开始前必须完成一次Protocol-owned schema hard cut：

```text
TerminalClientCapability = closed protobuf enum

HandshakeRecoveryCandidateIdentity
  supported_capabilities: unique sorted enum set
  required_capabilities: unique sorted enum set

HelloOutcome.ServerHello
  negotiation_winner:
    server_supported_capabilities: unique sorted enum set
    selected_capabilities: unique sorted enum set
    capability_contract_fingerprint
  receipt: ServerHelloReceipt
```

冻结规则：

1. `required_capabilities`必须是`supported_capabilities`子集；
2. server计算`selected = client_supported ∩ server_supported`，不得照抄client tuple；
3. `client_required - selected`非空时先将candidate CAS为`HandshakeCandidateTerminalReceipt(HELLO_REJECTED, MISSING_REQUIRED_CAPABILITY)`，再返回matching `HelloOutcome.HelloRejected`，不得返回可attach的Hello或用opaque `stable_error_code`替代；
4. `HelloOutcome.ServerHello.negotiation_winner.selected_capabilities`及其contract fingerprint进入stable hello negotiation transcript/winner；current receipt不得改写它们；
5. unknown enum number、重复项、非canonical顺序或同一capability ID的contract fingerprint漂移均fail closed；
6. attach后只能发送selected集合允许的required branch；optional capability缺失只能关闭对应client-local UX，不能改变Python domain semantics。

各vertical slice使用累计矩阵：

| Slice | Required capabilities | Optional capabilities |
|---|---|---|
| S1 | `PRESENTATION_SNAPSHOT_V1`、`OPERATIONAL_SNAPSHOT_V1`、`BOOTSTRAP_CARRIER_V1`、`LAUNCH_AUTH_PREFACE_V1`、`ATTACH_ACK_V1` | `CLIPBOARD_PUBLIC_TEXT_V1` |
| S2 | S1 + `HISTORY_PAGE_V1`、`OBSERVATION_STREAM_V1`、`ROOT_ADVANCE_V1`、`GAP_REBUILD_V1`、`CONTROL_PROJECTION_OBSERVATION_V1`、`RECONNECT_AUTH_ROTATION_V1` | `FOCUS_REPORT_V1`、`MOUSE_CELL_MOTION_V1` |
| S3 | S2 + `CONTROLLER_COMMAND_V1`、`COMMAND_QUERY_V1` | `LARGE_PASTE_ARTIFACT_V1` |
| S4 | S3 + `TYPED_INTERACTION_ACTIONS_V1`、`SECRET_FORM_V1`、`SECRET_PRIVATE_URL_V1`、`SECRET_REVOKE_V1` | 无 |
| S5 | S4 + `PROMPT_QUEUE_MUTATION_V1`、`SESSION_SUCCESSOR_V1` | 无 |
| S6 | S5 + `SERVER_CLOSING_V1` | `MOUSE_ALL_MOTION_V1` |

S1–S6 client build只声明已实现slice及更早slice的capabilities；不得预先广告尚未实现的branch。Python Gateway必须对`required_capabilities`执行真实admission，不能仅将tuple写入hello transcript。

`PRESENTATION_SNAPSHOT_V1`的必选baseline包含完整control projection cursor、pending-interaction public view与bounded active queue public view；它只证明snapshot安装在哪个control revision并允许read-only render，不表示client能消费live control observation或发送interaction/queue mutation。`CONTROL_PROJECTION_OBSERVATION_V1`是S2才可选择的增量branch，同时覆盖`ObserveNextRequest.after_control_cursor`、Changed/GAP frame与bounded transition proof；S4/S5 capability分别只激活interaction actions与queue mutations。Baseline cursor/view与live/action behavior不再拆成可互相矛盾的projection capabilities。

### TUI-PROTO-HELLO-004 TerminalClientBootstrapCarrier

Bootstrap是Protocol-owned、versioned Protobuf carrier，但不是`ClientFrame`分支，也不复用socket framing：

```text
TerminalClientBootstrapCarrier
  carrier_version = 1
  launch_id
  client_instance_id
  host_session_id
  unix_socket_path
  launch_capability: bytes
  requested_attachment_role: AttachmentRole
  parent_pid
  issued_at_utc
  expires_at_utc
  carrier_nonce: bytes[32]
  bootstrap_fingerprint
```

Physical framing固定为：

```text
uint32_be payload_length
payload_length bytes of TerminalClientBootstrapCarrier
EOF
```

约束：

- inherited read-only FD number可经`PULSARA_TUI_BOOTSTRAP_FD`传递；该值只允许十进制FD number，不含secret；
- payload hard cap为16 KiB，`payload_length == 0`、超限、partial payload、缺少EOF或任意trailing byte均拒绝；
- child在Program构造前以2秒absolute deadline完成exact one-shot read，随后关闭FD；同一FD不得重读或seek；
- `launch_capability`、nonce和完整carrier不得进入argv、其他environment value、process title、log、panic dump或AppState；
- client校验version、expiry、parent PID、safe socket path及bootstrap fingerprint；随后把launch capability安装进`ClientRuntimeOwner`的revocable mutable credential cell并best-effort覆写carrier/decoder副本；credential cell只能在matching Attach ACK FULL、expiry或teardown后清除，不能在Hello发送时提前销毁；
- fingerprint使用TUI-PROTO-SCHEMA-003的`WIRE_RECOMPUTABLE` contract，并覆盖除自身以外全部字段；
- parent只写一次并关闭write end；spawn失败、child early exit或read deadline到期时parent清除buffer并撤销launch authority。

Bootstrap schema、Python encoder、Go decoder、golden vector及trailing-byte/oversize tests必须在S1之前一起落地；Bubble Tea文档不得另定义同名carrier。

## 5. Attachment与controller ownership

### TUI-PROTO-ATTACH-001 Attachment identity

```text
TerminalClientAttachmentIdentity
  client_instance_id
  attachment_id
  runtime_session_id
  attachment_generation
  role: OBSERVER | CONTROLLER
  issued_at / expires_at
  identity_fingerprint

TerminalClientTransportBindingIdentity
  attachment_id
  attachment_generation
  connection_id
  transport_binding_generation
  bound_at_utc
  binding_fingerprint
```

Attachment是process-local capability，不是durable EventLog fact。Attachment identity fingerprint覆盖除自身外的semantic capability fields，严格排除connection/binding；transport binding fingerprint覆盖除自身外的全部binding fields。`AttachSemanticWinner`拥有attachment identity，`AttachResultReceipt`拥有current physical binding，二者必须exact join attachment ID/generation。Ordinary Ready-state reconnect必须使用next handshake candidate generation并创建新attachment generation；旧attachment不能复活。两种pre-Ready恢复不创建新semantic attachment：ACK前response丢失由new `AttachResultReceipt`对同一semantic winner执行typed rebind；ACK已FULL但result丢失由TUI-PROTO-TRANSPORT-002 tombstone返回exact ACK并执行typed rebind。任何command/heartbeat/snapshot都必须由Gateway把当前socket connection与current binding exact join；旧binding立即fail closed。

### TUI-PROTO-ATTACH-002 Multi-client policy

V1允许：

- 多个read-only observer；
- 每个runtime session同一时刻最多一个interactive controller；
- observer不得发送mutation或secret frame；
- secret lease只签发给current controller attachment。

### TUI-PROTO-ATTACH-003 Controller lease

Controller lease由Python Gateway唯一拥有：

```text
AVAILABLE
  -> HELD(attachment, generation)
      -> RELEASED
      -> EXPIRED
      -> REVOKED_BY_DETACH
      -> TRANSFERRED(new attachment, generation+1)
```

Takeover必须是显式command，验证same UID、target session、expected controller generation和policy。Transition生成typed command receipt与bounded structured audit record；V1不为普通attach/takeover新增EventLog schema。

同一connection断开、heartbeat expiry、client process exit或Gateway close都会revoke controller。Lease revoke不取消active run或删除durable queue intent。

### TUI-PROTO-ATTACH-004 Heartbeat

Heartbeat只证明attachment liveness，不推进projection cursor。Protocol 2.0的request/result不是行为约定或“下一份客户端状态”，而是下列closed wire carrier：

```text
HeartbeatRequest
  request_id
  runtime_session_id
  attachment_id
  attachment_generation
  attachment_identity_fingerprint
  attach_semantic_winner_fingerprint
  current_transport_binding: TerminalClientTransportBindingIdentity
  heartbeat_generation >= 1
  previous_accepted_heartbeat_generation >= 0
  heartbeat_candidate_fingerprint
  request_fingerprint

HeartbeatLivenessDisposition =
    ATTACHMENT_ACTIVE_LEASE_RENEWED
  | SESSION_CLOSING_LEASE_NOT_RENEWED

HeartbeatAcceptedReceipt
  request_id
  runtime_session_id
  attachment_id
  attachment_generation
  attachment_identity_fingerprint
  attach_semantic_winner_fingerprint
  acknowledged_transport_binding_fingerprint
  heartbeat_generation
  previous_accepted_heartbeat_generation
  heartbeat_candidate_fingerprint
  resulting_attachment_lease_expires_at
  liveness_disposition: HeartbeatLivenessDisposition
  heartbeat_semantic_result_fingerprint
  receipt_fingerprint

HeartbeatRejectedReason =
    STALE_ATTACHMENT
  | STALE_TRANSPORT_BINDING
  | ATTACHMENT_REVOKED
  | ATTACHMENT_EXPIRED
  | SESSION_CLOSED

HeartbeatRejectedReceipt
  request_id
  runtime_session_id
  attachment_id
  attachment_generation
  attachment_identity_fingerprint
  attach_semantic_winner_fingerprint
  submitted_transport_binding_fingerprint
  heartbeat_generation
  previous_accepted_heartbeat_generation
  heartbeat_candidate_fingerprint
  rejection_reason: HeartbeatRejectedReason
  heartbeat_semantic_result_fingerprint
  receipt_fingerprint

HeartbeatResult =
    HeartbeatAcceptedReceipt
  | HeartbeatRejectedReceipt
```

`heartbeat_candidate_fingerprint = H("terminal-heartbeat-candidate:v1", runtime session、attachment identity fingerprint、semantic winner fingerprint、heartbeat generation、previous accepted generation)`，故意不覆盖physical request/connection/binding；它使fully-sent read失败后的fresh-binding retry仍引用same semantic liveness attempt。Accepted semantic result使用`terminal-heartbeat-accepted-semantic-result:v1`覆盖candidate、liveness disposition与resulting expiry；rejected semantic result使用`terminal-heartbeat-rejected-semantic-result:v1`覆盖candidate与closed reason。`HeartbeatRequest.request_fingerprint`使用`terminal-heartbeat-request:v1`覆盖除自身外的全部字段，并覆盖完整current binding fingerprint。两个outer receipt分别使用`terminal-heartbeat-accepted-receipt:v1`与`terminal-heartbeat-rejected-receipt:v1`覆盖除自身外的全部字段；全部属于`WIRE_RECOMPUTABLE`。Request ID、attachment、winner、binding和generation任一漂移都不能被decoder修补。`heartbeat_generation`由client attachment-local scheduler从1开始严格递增；只有matching accepted receipt才能推进`previous_accepted_heartbeat_generation`。

Server按`attachment identity + heartbeat candidate fingerprint`冻结一次semantic result：first accepted可以更新lease一次；same candidate在fresh request/binding上的compatible retry必须返回相同liveness disposition与resulting expiry，只替换current physical request/binding attribution，不得再次延长lease。Same request只在完整receipt fingerprint相同才duplicate compatible；same candidate不同semantic result/expiry或same request different receipt是protocol conflict。Rejected candidate同样冻结closed reason；若server已经为candidate安装accepted winner，fresh binding retry必须取回accepted result，不能重新计算成stale-binding rejection。

Gateway必须验证承载request的socket正是`current_transport_binding.connection_id`，binding fingerprint与attachment live owner exact join，并按closed branch返回结果。`ATTACHMENT_ACTIVE_LEASE_RENEWED`要求expiry严格晚于本次server acceptance instant；`SESSION_CLOSING_LEASE_NOT_RENEWED`不得延长lease，client立即停止新heartbeat/observe并进入typed closing。Rejected receipt不修改attachment、controller或projection authority；stale/revoked/expired/closed均不可在同binding重试，必须按attachment/teardown状态机处理。Wire decoder只能产出validated receipt，不能把server fields与client-local `next_heartbeat_at`、missed count或backoff合成为`AttachmentState`。

Server冻结interval、grace和maximum missed count。Client以validated receipt到达时的process-local monotonic time加frozen interval计算下一次调度；wire timestamp只作lease authority join，不能当作本地timer。Event loop stall不得自动被解释为user cancel；expiry只revoke client capabilities并detach observation。V1同一physical connection串行处理request，因此任何`ObserveNext` long-poll上限必须严格小于heartbeat interval，并冻结为不超过interval的一半；hello只能广告该实际上限。合法的单次observation wait不得占满整段attachment lease或挤掉本连接下一次heartbeat。未来若允许更长等待，必须先引入独立并发reader/multiplexing contract，不能只提高数字。

## 6. Cursor模型

### TUI-PROTO-CURSOR-001 Authority high-water

`authority_high_water`是Python Foundation已经连续验证完整physical stored sequence、且canonical transcript reducer与registered durable-audit purpose policy/extractors均处理到的最高durable EventLog sequence。Presentation kernel不自行解释transcript acceptance/pairing。High-water可以推进而没有可见delta，例如accounting/noop event。

### TUI-PROTO-CURSOR-002 Projection revision

`projection_revision`是client-visible presentation stream的monotonic revision。一个durable event可产生零个或多个changes，多个events也可coalesce为一个delta。

### TUI-PROTO-CURSOR-003 Operational cursor

Operational state使用：

```text
operational_generation
operational_cursor
```

它不与durable sequence比较。Terminal operational owner replacement推进generation；同generation内cursor monotonic。

Operational plane必须有正式的session-owned bounded store，而不是只返回空cursor：store拥有当前generation、monotonic cursor、最多256项的bounded activity map/ring、最多1 MiB的encoded activity payload与opaque state fingerprint。Snapshot request/result冻结为：

```text
OperationalSnapshotRequest
  request_id
  runtime_session_id
  attachment_id
  attachment_generation
  attachment_identity_fingerprint
  current_transport_binding: TerminalClientTransportBindingIdentity
  requested_after_operational_generation >= 0
  requested_after_operational_cursor >= 0
  request_fingerprint

OperationalSnapshotFrame
  request_id
  runtime_session_id
  attachment_id
  attachment_generation
  attachment_identity_fingerprint
  acknowledged_transport_binding_fingerprint
  operational_generation >= 1
  operational_cursor >= 0
  ordered_activity_cells: tuple[OperationalActivityCell, ...] <= 256
  activity_count
  encoded_activity_bytes <= 1 MiB
  activity_fingerprint_accumulator
  operational_state_fingerprint
  snapshot_contract_fingerprint
  snapshot_frame_fingerprint
```

Request helper使用`terminal-operational-snapshot-request:v1`；frame helper使用`terminal-operational-snapshot-frame:v1`覆盖request/attachment/binding、generation/cursor、ordered cell identity+fingerprint vector、count/bytes/accumulator、opaque state fingerprint与contract fingerprint。两个outer fingerprint属于`WIRE_RECOMPUTABLE`；nested cell/state/contract fingerprints仍属于`OPAQUE_DOMAIN_AUTHORITY`。`activity_count == len(ordered_activity_cells)`，ordered vector按store canonical key顺序输出，identity key唯一；duplicate identity、count/bytes/accumulator不一致、unknown required branch或超 negotiated bound拒绝整帧，client不得截断。

`encoded_activity_bytes`不是Protobuf实现相关的`Size()`：它等于TUI-PROTO-SCHEMA-003 canonical JSON codec对完整ordered cell wire-value vector编码后的UTF-8 byte length，manifest禁止unknown fields、map与float。Accumulator genesis固定为`H("terminal-operational-activity-accumulator-genesis:v1", [])`；第`i`项使用`H("terminal-operational-activity-accumulator-step:v1", previous accumulator, zero-based index, owner kind/ID/generation, coalesce key, activity cell fingerprint)`递推。两个helper均由Protocol manifest生成；它们只证明ordered physical view完整性，不把opaque cell fingerprint变成Go可重算的domain语义。

`OperationalSnapshotRequest`返回当前bounded cells，`ObserveNext`按客户端generation/cursor返回typed ordered delta、no-change或GAP。Requested-after fields只用于diagnostic与compatible duplicate detection，不允许server返回“局部merge snapshot”：success frame始终是完整replace snapshot。Coalesce/drop只改变operational generation/cursor和activity bytes，不推进durable projection revision或history root。Store无durable replay承诺；process restart开启新generation，旧cursor收到GAP并重新请求operational snapshot。

### TUI-PROTO-CURSOR-004 Cursor join

每个snapshot/delta至少携带：

```text
runtime_session_id
authority_high_water
projection_revision
projection_contract_fingerprint
presentation_history_active_head_identity
```

Client只在`base_projection_revision == local revision`时应用delta。Authority high-water可跳跃；projection revision不可跳跃。Active head identity把Python Foundation已安装的immutable root proof与bounded uncheckpointed tail identity原子组合，Go不解释其registry或artifact内容。Operational frame按独立generation/cursor应用，不改变projection revision、history active head或root。

## 7. Observation plane

### TUI-PROTO-OBS-000 Closed presentation unions

Wire必须保持Foundation的lifecycle split：

```text
DurableHistoryCell = oneof {
  user_prompt
  assistant_message
  tool_terminal
  error
  interaction
  compaction_boundary
  recovery
  audit
  system_notice
}

OperationalActivityCell = oneof {
  model_activity
  tool_activity
  terminal_process_activity
  subagent_activity
}

PresentationHistoryEntry
  history_entry_id
  placement_key: PresentationHistoryPlacementKey
  source_reference_view
  durable_history_cell: DurableHistoryCell
  entry_fingerprint

PresentationHistoryPlacementKey
  placement_key_contract_id
  placement_key_contract_version
  placement_key_contract_fingerprint
  canonical_comparable_key_bytes
  placement_key_fingerprint

PresentationHistoryRankedEntry
  entry: PresentationHistoryEntry
  root_local_display_rank
  rank_basis_kind: CONFIRMED_ROOT | ACTIVE_HEAD
  rank_basis_fingerprint
  ranked_view_fingerprint

PresentationHistoryRootIdentity
  history_projection_contract_fingerprint
  materialization_policy_fingerprint
  tree_contract_fingerprint
  placement_key_contract_id
  placement_key_contract_version
  placement_key_contract_fingerprint
  checkpoint_generation
  checkpoint_fingerprint
  projection_generation
  projection_root_fingerprint
  through_authority_sequence
  presentation_source_segment_count
  presentation_source_prefix_accumulator
  presentation_policy_registry_contract_fingerprint
  audit_extractor_registry_contract_fingerprint
  root_identity_fingerprint

PresentationHistoryCursor
  root_identity: PresentationHistoryRootIdentity
  anchor_history_entry_id
  anchor_placement_key: PresentationHistoryPlacementKey
  cursor_fingerprint

PresentationHistoryActiveHeadIdentity
  confirmed_root_identity: PresentationHistoryRootIdentity
  tail_from_sequence_exclusive
  through_authority_sequence
  tail_source_range_accumulator
  tail_segment_count
  ordered_tail_segment_accumulator
  tail_mutation_count
  ordered_tail_mutation_accumulator
  resulting_resident_entry_count
  resulting_resident_entry_accumulator
  capacity_state: PresentationHistoryCapacityState
  active_head_fingerprint

PresentationHistoryCapacityState = oneof {
  available
  session_rotation_required
  tree_capacity_exhausted
  capacity_reconciliation_required
}

PresentationHistoryCapacityAdmissionDecision
  runtime_session_id
  source_active_head_fingerprint
  requested_growth_quote_fingerprint
  confirmed_entry_count
  current_tail_worst_case_entry_count
  active_growth_reservation_remaining_entry_count
  requested_admission_growth_quote_entry_count
  projected_ordinary_entries
  soft_rotation_threshold_entries
  terminalization_maintenance_reserve_entries
  maximum_representable_entries
  disposition: AVAILABLE | SESSION_ROTATION_REQUIRED
  decision_fingerprint

AvailableHistoryCapacity
  confirmed_entry_count
  current_tail_worst_case_entry_count
  active_growth_reservation_remaining_entry_count
  projected_ordinary_entries_before_request
  soft_rotation_threshold_entries
  terminalization_maintenance_reserve_entries
  remaining_ordinary_admission_entries
  capacity_state_fingerprint

HistorySessionRotationRequired
  confirmed_entry_count
  current_tail_worst_case_entry_count
  active_growth_reservation_remaining_entry_count
  projected_ordinary_entries_before_request
  soft_rotation_threshold_entries
  terminalization_maintenance_reserve_entries
  stable_reason = SESSION_HISTORY_ROTATION_REQUIRED
  capacity_state_fingerprint

HistoryTreeCapacityExhausted
  observed_entry_count
  maximum_representable_entries
  stable_reason = HISTORY_TREE_CAPACITY_EXHAUSTED
  reconciliation_identity
  capacity_state_fingerprint

HistoryCapacityReconciliationRequired
  source_active_head_fingerprint
  offending_growth_quote_fingerprint
  offending_growth_reservation_fingerprint
  observed_positive_growth_entries
  remaining_unmaterialized_entry_count
  stable_reason: HISTORY_GROWTH_QUOTE_EXCEEDED | CAPACITY_POLICY_DRIFT | RESERVATION_AUTHORITY_CONFLICT
  reconciliation_identity
  capacity_state_fingerprint

PresentationHistoryLatestRootCursorPair
  root_identity: PresentationHistoryRootIdentity
  before_cursor: PresentationHistoryCursor | None
  after_cursor: PresentationHistoryCursor | None
  cursor_pair_fingerprint

PresentationHistoryRootCursorRelation
  previous_root_identity: PresentationHistoryRootIdentity
  resulting_root_identity: PresentationHistoryRootIdentity
  relation_kind: STRICT_PREFIX_EXTENDED | REWRITTEN_GENERATION
  previous_cursor_disposition: RETAINED_PINNED
  shared_prefix_entry_count
  shared_prefix_accumulator
  relation_fingerprint
```

`run_lifecycle` 只是`AuditCell.audit_kind`的closed enum value，不存在`RunLifecycleCell` oneof branch。旧的扁平`TerminalSemanticCell`不得出现在`.proto`或generated wrapper。Durable/operational两个oneof没有共享branch；unknown branch必须fail closed并要求protocol upgrade。

`PresentationHistoryPlacementKey`不是任意bytes。V1只接受Foundation registered `presentation-history-placement-key-fixed:v1` exact ID/version/fingerprint和恰好75-byte `PHK1` framing；Python mapper与Go decoder都必须按同一fixed-width unsigned big-endian contract验证kind rank、sentinel、source sequence、local ordinal及32-byte tiebreaker。未知historical binding、长度不符或typed fields与bytes不一致返回typed rebase/protocol error，不能按locale/string或protobuf field order比较。

Capacity mapper必须验证唯一公式：`projected_ordinary_entries = confirmed_entry_count + current_tail_worst_case_entry_count + active_growth_reservation_remaining_entry_count + requested_admission_growth_quote_entry_count`。`terminalization_maintenance_reserve_entries`只验证`soft_rotation_threshold + reserve <= maximum_representable_entries`，不得加入projected count。Active-head capacity state不包含本次request；request-specific accept/reject只由`PresentationHistoryCapacityAdmissionDecision`表达。Python mapper与Go client不得从baseline state补猜quote、reservation或decision。

### TUI-PROTO-OBS-001 Snapshot

`ProjectionSnapshotFrame`包含renderer-neutral、bounded view：

- exact `PresentationHistoryActiveHeadIdentity`；
- ordered resident `PresentationHistoryRankedEntry`；
- exact `PresentationHistoryLatestRootCursorPair`；
- exact `TerminalControlProjectionSnapshot`；
- durable/control cursor join。

Control snapshot是Foundation `TerminalControlProjectionSnapshotFact`的唯一wire projection，不允许Gateway分项读取后拼装：

```text
TerminalControlProjectionView
  runtime_session_id
  session_lifecycle: TerminalSessionLifecycleControlView
  run_control: TerminalRunControlView
  pending_interaction: TerminalPendingInteractionControlView
  prompt_queue: TerminalPromptQueueControlView
  server_notifications: TerminalServerNotificationProjection
  control_view_fingerprint

TerminalControlProjectionSnapshot
  view: TerminalControlProjectionView
  cursor: ControlProjectionCursor
  snapshot_fingerprint

TerminalControlSectionSourceVersion
  section_kind: SESSION_LIFECYCLE | RUN_CONTROL | PENDING_INTERACTION | PROMPT_QUEUE | NOTIFICATIONS
  source_owner_id
  source_owner_generation
  source_owner_revision
  source_view_fingerprint
  source_version_fingerprint

TerminalPromptQueueControlView
  source_version: TerminalControlSectionSourceVersion
  projection: PromptQueueClientProjection
  view_fingerprint

ProjectionSnapshotRequest
  request_id
  runtime_session_id
  minimum_observed_control_cursor: ControlProjectionCursor | None
  request_fingerprint

ProjectionSnapshotControlJoin              # ProjectionSnapshotFrame的required nested fields
  validated_minimum_observed_control_cursor_fingerprint: optional
  control_projection_snapshot: TerminalControlProjectionSnapshot

ProjectionSnapshotControlRebaseRequired
  request_id
  requested_minimum_control_cursor_fingerprint
  latest_control_cursor: ControlProjectionCursor
  stable_reason = CONTROL_GENERATION_REBASED
  response_fingerprint

ProjectionSnapshotResponse =
    ProjectionSnapshotFrame
  | ProjectionSnapshotControlRebaseRequired
```

`ProjectionSnapshotControlJoin`不是第二个wire top-level message，只是上述`ProjectionSnapshotFrame`中两个required fields的文档化join contract；`.proto`只能定义一次`ProjectionSnapshotFrame`。Wire mapper必须从一次Foundation `TerminalControlProjectionStore.snapshot()`结果构造该nested control snapshot与echo；cursor `control_projection_fingerprint`必须等于view fingerprint，generation/revision与snapshot fingerprint逐项join。五个section都携带Foundation冻结的exact source owner generation/revision/fingerprint；queue通过wrapper保留原domain projection，Gateway不得为它伪造source version。Session lifecycle、三个run IDs、interaction、queue或notification任何字段不得从Host/Gateway即时状态覆盖。五个section fingerprint、source versions、完整view fingerprint、cursor与snapshot fingerprint均属于opaque Python authority；Go只做closed branch、bounds和exact joins。

`minimum_observed_control_cursor` 只在client已接受control Changed/GAP invalidation时出现。Server必须返回同generation且revision不低于minimum的atomic control snapshot，并回显已验证minimum cursor fingerprint；若generation已更换，则返回typed `ProjectionSnapshotControlRebaseRequired`，不得返回比已观察head更旧的snapshot。Client保留stale view/confirmed cursor，将response latest cursor替换为observed target，再发一次minimum-bound request；每次request仅允许一次generation rebase，连续换代受同一startup/reconnect absolute deadline和最多4轮bound限制，超界进入typed reconnect/fatal compatibility，不无界自旋。Request没有minimum时该optional字段和echo同时不存在。

Queue section不得把PostgreSQL中所有历史row序列化进snapshot。V1的wire carrier冻结为：

```text
PromptQueueClientProjection
  projection_contract_id = terminal-active-prompt-queue-projection
  projection_contract_version = 1
  projection_contract_fingerprint
  queue_head: PromptQueueProjectionHead
  queue_account_revision
  ordered_active_items: repeated PromptQueueItemPublicView
  active_item_count
  active_item_accumulator
  projection_fingerprint

PromptQueueProjectionHead =
    EmptyPromptQueueGenesisHead
  | CommittedPromptQueueHead

EmptyPromptQueueGenesisHead
  checkpoint_generation = 0
  checkpoint_through_sequence = 0
  checkpoint_fingerprint
  checkpoint_transition_count = 0
  bounded_tail_count = 0
  head_receipt_fingerprint
  empty_head_fingerprint

CommittedPromptQueueHead
  checkpoint_generation >= 0
  checkpoint_through_sequence >= 0
  checkpoint_fingerprint
  checkpoint_transition_count >= 0
  checkpoint_transition_accumulator
  bounded_tail_first_sequence_or_zero
  bounded_tail_last_sequence_or_zero
  bounded_tail_count >= 0
  bounded_tail_accumulator
  head_event_id
  head_event_sequence >= 1
  head_event_payload_fingerprint
  head_receipt_fingerprint
  committed_head_fingerprint
```

Wire branch必须原样映射Foundation公式`checkpoint_transition_count + bounded_tail_count`：总数为0才允许`EmptyPromptQueueGenesisHead`，且必须是canonical generation-0/through-0 checkpoint、zero tail、account revision 0、active count 0和canonical empty accumulators；总数大于0必须使用`CommittedPromptQueueHead`，其checkpoint generation可以为0。第一次Accepted至首次checkpoint之间因此表现为generation-0 checkpoint + non-empty bounded tail，而不是非法空branch。Committed branch要求checkpoint identity、tail range/count/accumulator、head receipt fingerprint及ID/sequence/payload全部存在并逐项匹配Foundation head receipt；zero tail仅在checkpoint transition count大于0时合法。Unknown branch、empty branch带transition/item、committed branch缺字段、client按generation自行换branch均拒绝整份snapshot。

`ordered_active_items`只允许Foundation定义的active client set，以accepted ordinal + queue item ID的canonical order输出，最多64项且content retention必须为ACTIVE；已committed、cancelled、delivery-rejected的terminal row只属于EventLog/Inspector，不进入client snapshot。`RECONCILIATION_REQUIRED`仍是active owner state，保留在projection但无client mutation action。Server queue admission在同一account/CAS boundary保证active set不超过64；mapper不截断，若count、accumulator、order或bound不符则整个snapshot fail closed并latch queue reconciliation。Server notifications最多16项、单项与aggregate bytes必须匹配Foundation policy，每项必须携带stable positive `notification_ordinal`并按`(ordinal, notification_id)`排序；client clock只改变local render visibility，不能从server vector删除item。Hello `NegotiatedLimits.maximum_active_queue_items`在V1必须等于64；客户端只用它验证服务端投影，不用它本地截断。

它不包含operational activity、raw event、private URL、form response、continuation plaintext、Python class/module identity或database row。Frame的`authority_high_water`必须精确等于active head identity的`through_authority_sequence`；latest cursor pair必须绑定active head中的same confirmed root，root/checkpoint/registry/tail字段不允许由mapper分别填充。Ordered resident entries可包含nested confirmed root的bounded window与active head的bounded uncheckpointed mutations；placement key是stable identity，display rank只绑定snapshot active-head basis。Page cursor仍只绑定confirmed root。Initial attach/reconnect的activity由TUI-PROTO-CURSOR-003完整定义的独立`OperationalSnapshotRequest -> OperationalSnapshotFrame`交付；它与Projection snapshot没有共同atomic fingerprint或cursor，但两者各自必须绑定same current attachment与transport binding。Operational frame不能只携generation/cursor空壳或由Gateway临时读取多个owner拼接。

### TUI-PROTO-OBS-002 Delta

Incremental durable observation使用closed frame union：

```text
ProjectionObservationFrame =
    ProjectionDeltaFrame
  | AuthorityAdvanceFrame
  | PresentationHistoryRootAdvancedFrame
```

```text
ProjectionDeltaFrame
  base_projection_revision
  resulting_projection_revision
  resulting_authority_high_water
  resulting_presentation_history_active_head_identity
  ordered changes:
      history entry upsert/remove
```

`ProjectionDeltaFrame` V1只拥有durable history resident changes；pending interaction、prompt queue、run/session status和notification control view一律不在该frame中出现。Changes使用stable history entry ID；client不得按display text推断identity。`resulting_authority_high_water`必须等于resulting active head identity的through sequence。Empty delta非法，same-root authority-only advance通过lightweight `AuthorityAdvanceFrame(base_active_head_fingerprint, resulting_active_head_identity)`表达或留待下一个snapshot；它不推进projection revision，但client必须原子替换current active head identity。任何confirmed-root变化都禁止走该branch。Schema hard cut必须删除旧control change fields并永久reserve其field numbers/names，不能保留未使用的第二语义入口。

`AuthorityAdvanceFrame`只允许base/resulting active head引用同一个confirmed root fingerprint；它可以推进authority high-water和bounded tail identity，但不能安装new root或latest cursor pair。Checkpoint FULL、compatible-winner adoption或rewrite安装new root必须使用独立closed branch：

```text
PresentationHistoryRootAdvancedFrame
  base_projection_revision
  resulting_projection_revision
  previous_active_head_fingerprint
  resulting_presentation_history_active_head_identity
  latest_root_cursor_pair: PresentationHistoryLatestRootCursorPair
  previous_root_relation: PresentationHistoryRootCursorRelation
  resident_transition: PresentationHistoryRootResidentTransition
  consumed_checkpoint_candidate_cut_fingerprint
  consumed_tail_prefix_through_sequence
  consumed_tail_prefix_source_range_accumulator
  consumed_tail_prefix_segment_count
  consumed_tail_prefix_segment_accumulator
  consumed_tail_prefix_mutation_count
  consumed_tail_prefix_mutation_accumulator
  retained_tail_suffix_from_sequence_exclusive
  retained_tail_suffix_through_sequence
  retained_tail_suffix_source_range_accumulator
  retained_tail_suffix_segment_count
  retained_tail_suffix_segment_accumulator
  retained_tail_suffix_mutation_count
  retained_tail_suffix_mutation_accumulator
  checkpoint_full_confirmation_fingerprint
  frame_fingerprint

PresentationHistoryRootResidentTransition = oneof {
  resident_entries_unchanged
  bounded_ordered_resident_changes
  resident_history_rebase_required
}

ResidentEntriesUnchanged
  before_resident_vector_fingerprint
  after_resident_vector_fingerprint
  exact_equivalence_proof_fingerprint
  transition_fingerprint

PresentationHistoryResidentChange = oneof {
  upsert
  remove
}

PresentationHistoryResidentUpsert
  history_entry_id
  placement_key: PresentationHistoryPlacementKey
  expected_previous_entry_fingerprint: optional
  resulting_ranked_entry: PresentationHistoryRankedEntry
  change_fingerprint

PresentationHistoryResidentRemove
  history_entry_id
  placement_key: PresentationHistoryPlacementKey
  expected_previous_entry_fingerprint
  change_fingerprint

BoundedOrderedResidentChanges
  before_resident_vector_fingerprint
  after_resident_vector_fingerprint
  ordered_changes: repeated PresentationHistoryResidentChange
  change_count
  encoded_change_bytes
  transition_limits_policy_fingerprint
  ordered_change_accumulator
  transition_fingerprint

ResidentHistoryRebaseRequired
  before_resident_vector_fingerprint
  target_root_identity: PresentationHistoryRootIdentity
  target_active_head_fingerprint
  stable_reason: RESIDENT_CHANGE_COUNT_EXCEEDED | RESIDENT_CHANGE_BYTES_EXCEEDED | REWRITE_REQUIRES_SNAPSHOT | PINNED_WINDOW_NOT_PROVABLE | SESSION_HISTORY_ROTATION_REQUIRED | HISTORY_TREE_CAPACITY_EXHAUSTED
  bounded_rebase_or_snapshot_token
  token_generation
  expires_at_utc
  transition_fingerprint
```

`PresentationHistoryRootAdvancedFrame`必须满足`resulting_projection_revision = base + 1`，并原子exact-join resulting active head、new latest cursor pair、root relation、consumed segment prefix、retained concurrent segment suffix与resident transition。Resulting active head允许non-empty tail，但其source/segment/mutation count与accumulators必须等于frame retained suffix；noop-only suffix因此表现为positive segment count、advanced source accumulator和zero mutation count，不能被mapper丢弃。`STRICT_PREFIX_EXTENDED`要求old root placement-key ordered entry vector是new root exact prefix；只有root swap与suffix retention前后的resident vector逐项相同才可使用unchanged。Resident vector fingerprint只覆盖ordered entry/placement key/display rank，不覆盖rank-basis attribution；UNCHANGED安装时client必须把整条vector原子rebind到resulting active-head basis。Compatible successor带来额外covered source-prefix lineage或post-cut live suffix时，必须按最终resident vector选择exact unchanged、bounded changes或rebase。`REWRITTEN_GENERATION`禁止client推断prefix或anchor映射。Unchanged要求before/after fingerprint相等；changes必须在wire/policy count与bytes上限内且ordered apply后得到resulting active head vector；rebase target必须等于frame resulting root/head。两种root relation都保留old-root cursors为retention-bound pinned cursors，但old pair不再是latest。Frame丢失表现为projection revision GAP并强制snapshot rebuild，不能继续靠old cursor观察new checkpointed tail。

### TUI-PROTO-OBS-002A Control projection change

History/presentation revision不是queue、pending interaction、run-control或session lifecycle变化的代理。V1不要求立即传输完整control delta，而冻结一个带独立cursor与bounded opaque transition proof的coalesced snapshot invalidation branch：

```text
ControlProjectionTransitionRegistryBinding
  registry_id = terminal-control-transition-registry
  registry_version = 1
  ordered_section_vocabulary_fingerprint
  transition_record_contract_fingerprint
  maximum_resident_records = 256
  maximum_resident_canonical_bytes = 1048576
  registry_contract_fingerprint

ControlProjectionTransitionRecord
  control_generation
  transition_ordinal
  base_control_projection_revision
  base_control_projection_fingerprint
  resulting_control_projection_revision
  resulting_control_projection_fingerprint
  changed_sections: non-empty unique sorted closed enum set
  transition_semantic_fingerprint
  previous_transition_prefix_accumulator
  resulting_transition_prefix_accumulator
  record_fingerprint

ControlProjectionCursor
  control_generation
  control_revision
  control_projection_fingerprint
  transition_prefix_accumulator
  transition_registry_contract_fingerprint
  cursor_fingerprint

ProjectionSnapshotFrame.control_projection_snapshot.cursor:
  ControlProjectionCursor

ObserveNextRequest
  request_id
  after_authority_high_water
  after_projection_revision
  after_operational_generation
  after_operational_cursor
  after_control_cursor: ControlProjectionCursor
  maximum_wait_ms

ControlProjectionChangedFrame
  request_id
  validated_after_control_cursor_fingerprint
  control_generation
  base_control_projection_revision
  base_control_projection_fingerprint
  resulting_control_projection_revision
  resulting_control_projection_fingerprint
  changed_sections: unique sorted enum set {
    SESSION_LIFECYCLE
    RUN_CONTROL
    PENDING_INTERACTION
    PROMPT_QUEUE
    NOTIFICATIONS
  }
  consumed_transition_count
  consumed_transition_range_accumulator
  resulting_control_cursor: ControlProjectionCursor
  disposition = SNAPSHOT_REQUIRED
  frame_fingerprint

ControlProjectionGapFrame
  request_id
  requested_control_cursor_fingerprint
  latest_control_cursor: ControlProjectionCursor
  stable_reason: GENERATION_CHANGED | CURSOR_TOO_OLD | TRANSITION_NOT_CONTIGUOUS | CONTRACT_CHANGED
  disposition = SNAPSHOT_REQUIRED
  frame_fingerprint

ObservationNoChangeFrame
  request_id
  echoed_authority_high_water
  echoed_projection_revision
  echoed_operational_generation
  echoed_operational_cursor
  echoed_control_cursor_fingerprint
  frame_fingerprint

DurableObservationBranch =
    ProjectionDeltaFrame
  | AuthorityAdvanceFrame
  | PresentationHistoryRootAdvancedFrame
  | DurableObservationGapFrame

OperationalObservationBranch =
    OperationalDeltaFrame
  | OperationalObservationGapFrame

ControlObservationBranch =
    ControlProjectionChangedFrame
  | ControlProjectionGapFrame

ObservationBatchFrame
  request_id
  durable: optional DurableObservationBranch
  operational: optional OperationalObservationBranch
  control: optional ControlObservationBranch
  included_plane_count: 1..3
  batch_fingerprint

ObserveNextResponse =
    ObservationBatchFrame
  | ObservationNoChangeFrame
```

`ControlProjectionChangedFrame`与`ControlProjectionGapFrame`进入`ObservationBatchFrame.control` closed union，共同受单一`CONTROL_PROJECTION_OBSERVATION_V1` capability约束。Snapshot中的baseline cursor属于`PRESENTATION_SNAPSHOT_V1`，不需要该live capability。规则：

- server端control projection owner在任一listed section的semantic view变化时推进revision；queue-only transition必须产生该branch，即使history root、authority high-water和presentation revision都不变；
- 每次advance形成一条上述immutable record；`transition_ordinal = resulting_revision`，resulting revision必须恰好等于base + 1，base/result projection identity必须与同一把store advance前后的typed view exact join；record只能append，不能就地合并或改写；
- 每个process-local generation从revision 0开始且没有record；genesis固定为`H("terminal-control-transition-genesis:v1", generation, 0, initial projection fingerprint, registry contract fingerprint)`。`transition_semantic_fingerprint`只覆盖generation/ordinal、base/result revision+projection fingerprint、changed sections与registry contract，不覆盖两个prefix accumulator或record fingerprint；resulting prefix固定为`H("terminal-control-transition-step:v1", previous prefix accumulator, transition semantic fingerprint)`。最后`record_fingerprint = H("terminal-control-transition-record:v1", transition semantic fingerprint, previous prefix, resulting prefix)`。Range accumulator以`H("terminal-control-transition-range-genesis:v1", after cursor fingerprint)`开始，按ordered record fingerprint使用`terminal-control-transition-range-step:v1`折叠；三者无自引用。
- server按request携带的exact `after_control_cursor`读取连续区间`(after_revision, current_revision]`，`changed_sections`取这些records的canonical union，count/range accumulator/resulting cursor证明同一段区间；不能从current view反推历史section union；
- 多次变化可在wire上coalesce成一个frame，但frame base必须等于request cursor，result必须等于当前cursor；
- `ControlProjectionCursor.cursor_fingerprint = H("terminal-control-cursor:v1", generation, revision, projection fingerprint, transition prefix accumulator, registry contract fingerprint)`；frame fingerprint覆盖除自身外的全部frame fields及nested resulting cursor fingerprint；
- client只在`validated_after_control_cursor_fingerprint == confirmed cursor fingerprint`且base generation/revision/fingerprint/prefix accumulator/registry全部相等时接受；resulting cursor revision与count delta、resulting projection fields与frame逐项exact join；duplicate resulting cursor + exact frame fingerprint为no-op；same cursor different fingerprint是compatibility failure；
- requested cursor落后于bounded ring、generation/registry变化或transition不连续时，server返回`ControlProjectionGapFrame`，不得伪造一个base等于current的Changed frame；
- Changed/GAP只证明已观察到的latest control head，不携带resulting view。接受后client必须保留旧view与与它exact join的`confirmed_cursor`，另存frame的`observed_latest_cursor`，进入`SNAPSHOT_REQUIRED`；禁止用observed cursor覆盖confirmed cursor或假称它已对应未收到的view；
- client调度一次携`minimum_observed_control_cursor = observed_latest_cursor`的`ProjectionSnapshotRequest`；收到matching且不早于minimum的snapshot前禁止依赖旧queue/interaction/run state发mutation；
- durable history与operational plane仍可继续显示；control invalidation不伪造history GAP，也不清除ordinary composer draft；
- snapshot安装必须消费Foundation一次原子返回的`TerminalControlProjectionSnapshot`，替换全部control sections和完整control cursor，不能只patch queue或从Gateway当前字段补值；
- Go不得以观察到任意history delta、command receipt或socket activity推断control变化。

Python唯一owner是session-owned `TerminalControlProjectionStore`。它保存完整immutable `TerminalControlProjectionViewFact`、五个section的exact source owner generation/revision、monotonic process-local control generation/revision/prefix accumulator、matching cursor、最多256 records或1 MiB（先到者为准）的transition ring与non-blocking wake signal；limits与section vocabulary由exact `ControlProjectionTransitionRegistryBinding`冻结。Host lifecycle/run-control/pending-interaction、PromptQueue canonical reducer和server-notification owner在各自confirmed transition fold后调用同一typed `replace_section(candidate)` port。

`replace_section` 不接受caller提供的global expected cursor；Store在同一锁内将different-section candidate rebase到latest view，同section则按source owner generation/revision/fingerprint决议`APPLIED | EXACT_DUPLICATE | STALE_SOURCE | SOURCE_CONFLICT | SOURCE_GAP | STORE_NOT_READY`，然后构造完整resulting view、record与cursor并原子安装。Store不拥有底层queue/interaction authority，也不扫描EventLog。Ring eviction只影响可增量证明的oldest cursor，不改变current semantic identity。`snapshot()`在同一锁内返回view + cursor，Gateway不得分项读取。

Process restart/bootstrap在attachment admission前必须执行Foundation冻结的capture-then-snapshot协议：先安装五source transition capture，再读取携source version的五个immutable snapshot，然后要求五个source owner在各自confirmed-publication sequencer中flush callback并签发matching `TerminalControlSourceCaptureFenceReceipt`，最后按global capture ordinal回放bounded transition并追至同一barrier。仅把callback排入async queue不构成fence；READY要求五receipt、snapshot与accepted-buffer recurrence exact join，随后每个capture registration必须经`promote_capture_to_live()`返回store-owned `TerminalControlLiveSubscriptionLease`，并以同一synchronously-acknowledged sink继续交付。Generation replacement由Foundation唯一store-owned attempt持有稳定identity与可变resource snapshot：保持旧lease live，新generation完成capture/promotion后在store锁内原子切换active view/cursor/sink generation，随后锁外凭`RELEASED_ON_GENERATION_REPLACEMENT` receipts drain旧lease；release超时保留retiring owner并禁止第三代replacement。所有source-port调用禁止持有store lock。READY后无replacement/close就释放、无live lease、换callback或close不drain active/retiring lease均非法；partial promotion必须完整rollback且不得开放attachment。Store close与replacement在同一锁内线性化：先禁止new capture/promotion，再锁外release new CAPTURING registrations、drain new partially-promoted leases及active/retiring leases，全部typed receipt回锁验证后才释放sink/source dependency。Close deadline到期必须保留attempt/resources并报告blocked。Live/capture release receipt只是Foundation process-local owner proof，不进入Protocol wire或control fingerprint。一代最多512 candidates/8 MiB，最多4代，共享attachment startup deadline且上限10秒。Overflow/gap/conflict/fence timeout必须restart/fail closed，不得用分项当前值建立baseline。旧generation必得typed control GAP，不能复用revision。Notification section必须以stable ordinal + ID排序；pending interaction与notification owner都必须提供monotonic source revision，不得让迟到candidate覆盖新view。

V1明确选择“opaque Python authority”：`ControlProjectionTransitionRecord`的semantic/record fingerprints、prefix/range accumulators、cursor和frame fingerprint全部归`OPAQUE_DOMAIN_AUTHORITY`。Wire不传输record payload；Go不重算changed-section union或accumulator，只验证registry identity、base/result cursor、revision/count、closed set与nested exact join。Python recurrence以golden vectors验证，Go以cross-language fixtures验证opaque value与结构join。若未来要Go重算，必须先将ordered record fingerprint vector加入wire并升级capability/schema，不得在client私自发明算法。

Gateway `ObserveNext`必须在wake/timeout后对request中的三套after-cursor各做一次bounded evaluation，然后把每个pending plane至多一个branch装入同一`ObservationBatchFrame`。某plane存在delta/root/change或GAP时不得因另一个plane更繁忙而省略；因此持续history delta不能饿死control，持续control也不能饿死operational。三个branch各自证明自己的base/result cursor，不声明跨plane atomic semantic snapshot；batch fingerprint只证明本次完整delivery set和canonical plane order `control -> durable -> operational`。Negotiated per-plane encoded-byte上限与batch envelope reserve之和必须小于`maximum_frame_bytes`；单plane超界返回该plane GAP/rebuild branch，不能通过丢弃其他pending plane腾空间。

Client将完整batch映射为一个closed application message，并按`control -> durable -> operational`顺序在一次Update中apply：control change/GAP先标记control stale，随后durable/operational仍可安全显示。任何branch失败使整批不安装，并按受影响plane触发typed rebuild；bridge不得拆成三个可重排的`Program.Send`。只有三个plane都没有推进且没有GAP时才返回`ObservationNoChangeFrame`，它必须exact echo request中的durable、operational和control三个cursor fingerprints。Durable/control/operational分别使用自己的typed GAP branch，不得用一个ambiguous generic gap误清空其他plane。

未来若改为完整typed `ControlProjectionDelta`，必须先修改Protocol schema和Python application projection owner；不得在Go中自行从command text或history cell重建。

### TUI-PROTO-OBS-003 Operational frame

Operational frame可被replace/coalesce/drop，携带owner identity、generation、cursor、expiry和`OperationalActivityCell` upsert/remove。它不得携带`DurableHistoryCell`、history placement key/root/cursor，也不得改变durable history、projection revision或command outcome。

### TUI-PROTO-OBS-004 History page

History request携带stable semantic cursor、direction、page limits和expected projection contract。Client visual wrap/height不进入server page identity。Wire response必须是与Foundation disposition一一对应的closed union：

```text
HistoryPageRequest
  request_id
  runtime_session_id
  cursor: PresentationHistoryCursor
  direction: BEFORE | AFTER
  maximum_cells
  maximum_decoded_bytes
  expected_projection_contract_fingerprint

HistoryPageResponse =
    HistoryPageData
  | HistoryCursorStale
  | HistoryRebaseRequired
  | HistoryReconciliationRequired

HistoryPageData
  request_id
  validated_input_cursor_fingerprint
  validated_request_direction
  validated_presentation_history_root_identity
  ordered_history_entries: repeated PresentationHistoryRankedEntry
  ordered_history_entry_accumulator
  continuity_proof
  before_cursor
  after_cursor
  has_more_before
  has_more_after
  response_fingerprint

HistoryCursorStale
  request_id
  requested_cursor_fingerprint
  latest_root_identity: PresentationHistoryRootIdentity
  replacement_cursor: optional
  replacement_cursor_anchor_proof: optional
  response_fingerprint

HistoryRebaseRequired
  request_id
  requested_cursor_fingerprint
  latest_root_identity: PresentationHistoryRootIdentity
  bounded_snapshot_or_rebase_token
  response_fingerprint

HistoryReconciliationRequired
  request_id
  requested_cursor_fingerprint
  fault_code
  reconciliation_owner_identity
  retry_after_ms: optional
  trusted_latest_root_identity_hint: PresentationHistoryRootIdentity | None
  response_fingerprint
```

Wire `PresentationHistoryCursor`不得包含direction或feed kind；它只绑定unified `PresentationHistoryRootIdentity`与anchor。`HistoryPageRequest.direction`是本次读取方向的唯一真源，Python adapter必须原样调用`PresentationHistoryPagePort.read_page(cursor, direction, limits, deadline)`。禁止生成transcript/audit两套RPC、`read_before/read_after`两个method，也禁止从cursor、anchor位置或stale/rebase response反推direction。`HistoryCursorStale | HistoryRebaseRequired | HistoryReconciliationRequired`不得携带recommended/next direction；重建后的下一次direction仍由client新建request选择。

`HistoryPageData`中的empty `ordered_history_entries`加matching `has_more_* = false`才表示该方向没有更多历史。Entries已由Python unified root按stable placement key完成transcript/audit全局排序，Go不得按display rank、cell kind、source sequence或arrival time再merge/reorder。每个display rank只绑定response中的confirmed root，不能缓存为跨root identity。`HistoryCursorStale`和`HistoryRebaseRequired`永不表示end-of-history：前者只有在server能证明same anchor placement key/entry ID在latest root中的exact映射时才可返回replacement cursor，`replacement_cursor`与`replacement_cursor_anchor_proof`必须同时存在或同时为空；无法证明时必须升级为rebase。后者要求client丢弃受影响的history cache并以snapshot/rebase token重建。`HistoryReconciliationRequired`不得伪造可信cursor/root；只有authority仍被证明的hint才可出现。

Server每次向attachment发布snapshot/page cursor或接受旧cursor page request时，必须在读取root前获取attachment-bound process-local root-retention lease；该lease不进入wire DTO或cursor fingerprint。Detach/expiry释放lease；unleased immutable root只在Foundation冻结的generation-window与TTL双重horizon内可重新borrow。只有root已被合法retire/GC时才返回cursor stale，latest root推进本身不得让仍retained的旧cursor失效。

Client必须区分`latest_root_cursor_pair`和零个或多个`pinned_root_page_cursor`：follow-tail、jump-to-end、近期cache eviction后的rehydration及从current root发起的新page request只能使用latest pair；已经打开的old-root page可以继续用其pinned cursor读取同一immutable root。Root-advanced frame不得覆写或重标pinned cursor，也不得把old empty-after-page解释为new root的history end。

Protocol adapter必须把Foundation的`PAGE | CURSOR_STALE | REBASE_REQUIRED | RECONCILIATION_REQUIRED`逐branch无损映射；禁止把后三种降级为空page、generic transport error或`has_more=false`。

## 8. Backpressure与GAP

### TUI-PROTO-BP-001 Queue isolation

每个attachment至少有三个独立bounded delivery classes：

- observation snapshot/delta；
- control/command receipt；
- secret one-shot frame。

UI feed不得进入Runtime writer/publisher await chain。Gateway从Foundation tap异步消费并`put_nowait`到attachment buffer。

### TUI-PROTO-BP-002 Overflow

Observation overflow：

```text
drop pending deltas
-> enqueue GAP(latest authority high-water, latest projection revision, reason)
-> mark attachment projection invalid
-> reject further delta application
-> require snapshot rebuild
```

若连GAP都不能入队，detach attachment。绝不阻塞Runtime。

Command receipt delivery overflow不改变command结果；client可按command ID查询。Secret frame不进入replay queue，delivery failure使lease remain/revoke according to exact state，不把plaintext放入retry buffer。

### TUI-PROTO-BP-003 Ingestion matrix

- exact next revision：apply；
- duplicate revision+fingerprint：no-op；
- overlapping page/snapshot with exact stable IDs：dedupe；
- gap、base mismatch或same revision different fingerprint：invalidate并snapshot rebuild。

## 9. Command plane

### TUI-PROTO-CMD-001 Identity

每个mutation command必须携带：

```text
client_instance_id
attachment_id
attachment_generation
command_id
runtime_session_id
expected_target_id
expected_target_generation
expected_controller_generation
request_semantic_fingerprint
```

`command_id`由client在第一次admission前生成，retry/reconnect保持不变。相同ID不同semantic fingerprint是authority conflict。

### TUI-PROTO-CMD-002 Closed request union

```text
TerminalMutationCommand =
    SubmitPromptCommand
  | StopRunCommand
  | ResolveApprovalCommand
  | ResolvePlanQuestionCommand
  | ResolvePlanExitCommand
  | ResolveMcpInteractionCommand
  | CancelMcpInteractionCommand
  | QueueCancelCommand
  | StartSuccessorSessionCommand
  | DetachSessionCommand
  | CloseSessionCommand
  | ControllerTakeoverCommand
```

`StartSuccessorSessionCommand`只在current active head携带exact `HistorySessionRotationRequired`或operator显式新建session时合法；它引用source runtime session与capacity-state fingerprint，创建新的runtime session，但不自动搬运pending queue、interaction、secret或controller lease。相同command ID只确认同一successor session winner。`HistoryTreeCapacityExhausted`只能由该command或privileged repair退出当前session，不能继续向旧session提交ordinary growth。

Read-onlyquery使用独立union，不要求controller，但仍受attachment/capacity/deadline约束。禁止method string加arbitrary JSON。

V1不存在`QueueEditCommand`、`QueueReclassifyCommand`或server-side atomic replace。修改queue content、delivery mode或target必须由client顺序提交两个独立stable command：

```text
QueueCancelCommand(old_item, cancel_command_id)
  -> SUCCEEDED
SubmitPromptCommand(new_submission, replacement_command_id)
```

两个command分别拥有独立candidate、receipt与reconnect query。只有cancel已确认`SUCCEEDED`后client才能提交replacement；replacement失败、超时或进入reconciliation时，旧item保持cancelled，不得复活或隐式恢复。Reconnect必须分别查询原cancel command ID与replacement command ID，不得用新ID重放任一已提交mutation。

### TUI-PROTO-CMD-003 Idempotency

Gateway不以process-local receipt cache作为成功authority：

- stable domain candidate/event/row ID必须从runtime session、command ID、target identity和contract推导；
- FULL后query按exact durable authority重建same outcome；
- NONE允许same stable command candidate重试；
- UNKNOWN保留owner并exact confirm；
- conflict/reconciliation返回closed outcome；
- reconnect不得以新command ID自动重放submit、stop、approval或queue mutation。

### TUI-PROTO-CMD-004 Outcome

```text
CommandOutcome =
    SUCCEEDED
  | REJECTED
  | PENDING_CONFIRMATION
  | RECONCILIATION_REQUIRED
  | SUPERSEDED_BY_COMPATIBLE_WINNER
```

Outcome携带command ID、target identity/generation、bounded public result、exact durable references或query token。任何因history growth被拒绝的prompt/run/queue/interaction command必须在bounded public result中携带exact `PresentationHistoryCapacityAdmissionDecision`；client不得从active-head baseline自行重算request quote。Physical `FULL/NONE/UNKNOWN`不直接暴露为domain outcome。

### TUI-PROTO-CMD-005 Query

`QueryCommandOutcome`可在新attachment上按runtime session + original client instance + command ID查询。Secret response plaintext永不返回；MCP resolution只返回status与durable refs。

## 10. Secret plane

### TUI-PROTO-SECRET-001 三层authority

```text
encrypted continuation store       # durable storage-only authority
Python Host secret service         # decrypt/hydration/validation/expiry owner
Go controller attachment           # ephemeral input/display state
```

Protobuf secret message只是transient carrier，不是上述任何authority。

### TUI-PROTO-SECRET-002 Lease

```text
TerminalSecretLeaseIdentity
  attachment_id/generation
  controller_generation
  interaction_id
  request_key
  secret_kind: PRIVATE_URL | FORM_RESPONSE
  owner_epoch
  lease_generation
  expires_at
  identity_fingerprint
```

只有current controller可请求。Detach、controller takeover、interaction terminal、expiry或Host close revoke lease。Reconnect必须exact hydrate并签发新lease。

`FORM_RESPONSE`并非仅按`interaction_id`密封。Opaque sealed handle在Python Host中还必须exact绑定当前elicitation batch owner ID/generation、round ordinal、request-set fingerprint、该request的fingerprint与wire `request_key`，以及attachment/controller generation、owner epoch、lease generation和UTC+monotonic TTL。Gateway不得丢弃`SecretFormSubmit.request_key`。Submit时先验证完整response key set与当前pending form batch一致，再密封整份atomic response map；consume时重新验证同一个exact owner仍为current。Batch/round替换、ABA owner变化、controller takeover、detach、interaction terminal、expiry或Host close都使未来consume fail closed，并best-effort overwrite/release mutable plaintext buffer。Sealed handle是process-local owner，不把这些内部attribution复制进ordinary wire或durable event。

### TUI-PROTO-SECRET-003 Frames

```text
SecretRevealRequest
SecretRevealResult             # one-shot private URL bytes
SecretFormSubmit               # response bytes + request key
SecretSubmitReceipt            # no plaintext
SecretLeaseRevoked
  push_header: ServerPushHeader
  lease_identity_fingerprint
  reason: DETACH | CONTROLLER_TAKEOVER | INTERACTION_TERMINAL | EXPIRED | HOST_CLOSE
  frame_fingerprint
```

Secret frames：

- 不进入ordinary observation/command replay buffer；
- 不进入snapshot、delta、diagnostic、trace或structured log；
- 不启用compression；
- 使用独立strict byte cap和short deadline；
- repr/log interceptor只能输出constant redacted marker；
- decode后立即交给secret service或client secret state；
- 失败后不得把plaintext缓存为retry payload。

`SecretLeaseRevoked`是S4起的unsolicited push，使用与TUI-PROTO-LIFE-003相同的connection-scoped push generation/sequence和独立`terminal-secret-lease-revoked-frame:v1` wire fingerprint；它不匹配或完成当前secret request。若revoke与reveal/submit response竞速，wire顺序决定先应用哪一项，但revoke安装后任何future borrow必失败，late response只能清除transient bytes而不能重新激活handle。

### TUI-PROTO-SECRET-004 Memory承诺边界

Python/Go/terminal emulator不能保证已复制字符串的物理零化。实现只承诺：

- 不主动持久化或加入history/undo/snapshot；
- owner epoch变化后旧lease未来访问fail closed；
- mutable byte/rune buffers退出时best-effort overwrite；
- 已显示、复制、terminal scrollback或截图中的plaintext不可撤销。

## 11. Process与signal contract

### TUI-PROTO-LIFE-001 Ownership

Python launcher/Host process是runtime、Gateway和socket owner；Go child是TTY owner。Go exit不会直接关闭RuntimeSession。Python负责：

- child spawn identity；
- socket/bootstrap capability；
- unexpected exit classification；
- optional client restart；
- runtime close/detach decision。

### TUI-PROTO-LIFE-002 Signals

- terminal-generatedSIGINT首先由Go key routing解释，不能无条件传播为Python cancellation；
- explicit stop通过typed command；
- Go process SIGTERM退出前best-effort restore terminal并detach；
- GO-READY-C/S6且client selected `SERVER_CLOSING_V1`后，Python shutdown显式发送server closing frame，再revoke leases和关闭socket；此前只承诺lease revoke/attachment invalidation与EOF；
- child crash/kill不得遗留terminal mode；该性质由S0和PTY integration gate验证。

### TUI-PROTO-LIFE-003 ServerClosingFrame阶段归属

通用graceful close frame属于S6，不是S4 secret safety前置。S4依赖request-scoped`SecretLeaseRevoked`以及attachment/connection invalidation时client local revoke；即使没有graceful frame也不得继续使用secret handle。

S6前新增closed branch：

```text
ServerPushHeader
  connection_id
  connection_generation
  attachment_id
  attachment_generation
  transport_binding_generation
  transport_binding_fingerprint
  push_generation
  push_sequence
  header_fingerprint

ServerClosingFrame
  push_header: ServerPushHeader
  server_runtime_identity
  host_session_id
  reason: HOST_CLOSE | RUNTIME_SHUTDOWN | PROTOCOL_UPGRADE_REQUIRED
  remaining_grace_ms
  detach_allowed
  frame_fingerprint
```

它使用`SERVER_CLOSING_V1` capability并进入`ServerFrame`独立unsolicited-push oneof；不是任何request的response，不能借用当前request ID或伪造operation completion。Header fingerprint使用`terminal-server-push-header:v1`覆盖除自身外的全部header fields；frame fingerprint使用`terminal-server-closing-frame:v1`覆盖nested header fingerprint和除自身外的全部frame fields。Server在单个connection上为push header维护monotonic generation/sequence；client按exact connection/attachment/transport binding接纳，duplicate sequence只在frame fingerprint相同才no-op，回退、跳号或same sequence different fingerprint进入typed lifecycle GAP/compatibility teardown。Decoder可以在等待heartbeat、snapshot、page、observe、command、query或secret response时先交付该push；原outstanding operation仍由下表settle，而不是被push“匹配完成”。

收到frame后Go原子安装single teardown permit、停止创建新effect，并按operation kind使用唯一settlement matrix：

| In-flight kind | ServerClosing settlement |
|---|---|
| heartbeat、snapshot、operational snapshot、page、observe | 取消physical read，terminalize为`INTERRUPTED_BY_SERVER_CLOSING`；不安装partial payload、不重试 |
| command mutation/command query | 保留stable command identity并转`QUERY_REQUIRED_AFTER_REATTACH`；不得按断连推断成功或失败，也不得用新command ID重发 |
| secret reveal | 撤销owner中任何已安装handle，丢弃transient bytes；不得自动reveal |
| secret submit | 撤销plaintext handle；若request可能已send则只保留无plaintext command/query identity并进入query/reconciliation，不重放body |
| attach/heartbeat前的auth、Hello、Attach、Attach ACK | 该push在未建立matching attachment时非法；只允许transport close/auth result表达失败 |
| client-local clipboard、open URL、tick | 取消或完成stale local operation，不产生server disposition |
| teardown/detach | 并入当前single teardown generation，不启动第二个owner |

随后在remaining grace内best-effort detach；不得把它解释为run cancel或conversation close。Unknown reason/invalid attachment按protocol incompatible teardown。S4的Host close测试通过lease revoke或EOF证明secret future borrow失败；S6另测graceful文案、in-flight逐类settlement与bounded detach。

## 12. Schema evolution

### TUI-PROTO-SCHEMA-001 Compatibility

- 禁止复用field number；
- removed field永久reserved；
- required semantic branch通过oneof + application validator表达；
- enum zero值必须`UNSPECIFIED`并在required位置拒绝；
- new optional field需要minor bump；
- new required behavior或changed meaning需要major bump；
- breaking check比较committed schema baseline。

GO-READY-A/B是一次原子的Protocol `2.0` hard cut，物理版本矩阵固定为：

| Surface | Current baseline | GO-READY-A result |
|---|---|---|
| Python constants | `PROTOCOL_MAJOR = 1`, `PROTOCOL_MINOR = 0` | `PROTOCOL_MAJOR = 2`, `PROTOCOL_MINOR = 0`, `minimum_compatible_minor = 0` |
| Protobuf package | `pulsara.terminal.v1` | `pulsara.terminal.v2` |
| Go package option | absent | `option go_package = "github.com/plumliu/pulsara-agent/clients/terminal/internal/protocol;protocol"` |
| schema fingerprint | current v1 value | newly generated Protocol-2 fingerprint |
| bootstrap supported range | major 1 | exact major 2、minor 0..0 |
| Python generated module path | `terminal_client_pb2.py` | path unchanged、descriptor full names become `pulsara.terminal.v2.*` |
| Go generated owner | absent | `clients/terminal/internal/protocol/terminal_client.pb.go` |

Behavioral string迁enum会改变wire type，因此旧field number/name必须`reserved`，new enum使用fresh field number；不得在同一message保留string+enum dual truth或复用wire number。`terminal_client.proto`的package、`codec.py` constants、bootstrap carrier/range、Gateway admission、Python/headless generated binding、fingerprint manifest/golden和future Go binding在同一GO-READY-A PR切换。Transport auth preface自身仍为`preface_version = 1`，但其handshake candidate protocol range必须只允许major 2；它不是application Protocol major的替代品。

Cutover后Gateway对major 1 `ClientHello`固定返回typed `PROTOCOL_MAJOR_UNSUPPORTED`并close，不协商、不attach、不回退v1 decoder；major 1 package descriptors、golden和headless fixtures从production/test path删除。当前没有production external Go client，不提供Protocol 1.x compatibility shim；旧Python headless fixture机械重写。之后S4/S6新增optional branch可从2.0按普通minor evolution处理，但仍受capability gate。

GO-READY-A的物理修改面至少包含且不得拆分：

```text
src/pulsara_agent/terminal_protocol/schema/terminal_client.proto
src/pulsara_agent/terminal_protocol/generated/terminal_client_pb2.py
src/pulsara_agent/terminal_protocol/codec.py
src/pulsara_agent/terminal_protocol/gateway.py
tests/support/terminal_protocol.py
tests/test_terminal_protocol.py
tests/test_terminal_infrastructure_architecture.py
```

以上是当前代码真值中读取`PROTOCOL_MAJOR`、protobuf full name或generated carrier的完整已有修改面；S1同PR新增的launcher/bootstrap/Go binding文件以Bubble Tea实施规格清单为准。Schema/generator输出属于同一commit；旧v1 descriptor、fixture和decoder引用计数必须为零。这里的carrier内部`candidate_version = 1`或fingerprint namespace后缀`:v1`是各自contract version，不是application Protocol major，不能被机械改成2。

### TUI-PROTO-SCHEMA-002 Domain mapping

每个domain-to-wire mapper有golden vectors，验证：

- stable IDs/cursors无损；
- closed union branch一致；
- secret fields不存在于ordinary messages；
- unknown domain branchfail closed；
- wire round-trip不被用作domain semantic fingerprint算法。

### TUI-PROTO-SCHEMA-003 Fingerprint classes与跨语言canonical helper

Go-ready Protocol不得把所有`sha256:*`字段都称为“可重算fingerprint”。每个fingerprint field必须在Protocol-owned manifest中归入且仅归入一类：

| Class | Go行为 | V1 examples |
|---|---|---|
| `WIRE_RECOMPUTABLE` | 调用Protocol-generated helper重算并比较 | bootstrap/auth preface/auth result；handshake recovery candidate；Hello negotiation winner、challenge commitment、current receipt、candidate terminal receipt三分支outcome；attach semantic winner；per-connection attach-result/transport-binding/rebind/ACK receipt；heartbeat request/accepted/rejected receipt；operational snapshot request与outer frame；server push header/closing frame；client operation/request/local-operation IDs；12个client mutation request semantic fingerprints；command outcome wire view |
| `OPAQUE_DOMAIN_AUTHORITY` | 只做格式、nested exact join与duplicate/conflict比较；禁止重算 | durable cell/root/active-head/cursor/capacity/snapshot/delta/interaction/queue/secret-lease；operational cell/state/contract fingerprint；control transition record/prefix/range/cursor/frame fingerprints |
| `PHYSICAL_ATTRIBUTION` | 只用于request/attachment/operation correlation；不进入semantic equality | request ID、connection ID、build identity、operation generation；server push generation/sequence |

唯一contract source新增：

```text
src/pulsara_agent/terminal_protocol/schema/
  terminal_client.proto
  terminal_client_fingerprint_contract.v1.json
  terminal_client_fingerprint_golden.v1.json
```

`terminal_client_fingerprint_contract.v1.json`逐entry冻结：field path、class、namespace、ordered covered field paths、absent/null/empty处理和output format。Generator从同一manifest产生Python与Go helper；不允许在`codec.py`、Go `wire/validate.go`或测试fixture手写第二份field list。

Client operation identity必须有三个独立manifest entries：`terminal-client-operation-id:v1`覆盖client instance、app/connection/operation generation与operation kind；`terminal-client-request-id:v1`覆盖operation ID与kind；`terminal-client-local-operation-id:v1`覆盖client instance、app/operation generation与local kind。Generated helper负责加closed output prefix。Wire effect在I/O前必须已携带exact operation ID与request ID；executor不得临时生成。Local operation没有request-ID entry或nullable request field。

`WIRE_RECOMPUTABLE` V1 canonical算法与现有Python `context_fingerprint()`保持一致：

```text
sha256(
  UTF8(namespace)
  || 0x00
  || canonical_json_utf8(covered_value)
)
-> lowercase "sha256:" + 64 hex
```

`canonical_json_utf8`冻结为：object key按Unicode code point升序、无多余空白、array保持顺序、UTF-8非ASCII字符不转义、JSON control/quote/backslash按唯一规则转义、integer使用无前导零十进制、boolean/null使用小写token；禁止float、NaN、Infinity、map iteration order、HTML escaping、Unicode normalization和隐式bytes-to-string。Optional field必须由manifest明确为`absent`、`null`或empty，三者不得互换。Generated helper先构造typed canonical value再编码，不对Protobuf deterministic bytes或`fmt.Sprint`直接hash。

Proto enum进入现有domain request/outcome fingerprint时，manifest必须逐值给出canonical semantic token（例如`SUCCEEDED -> "succeeded"`、`FOLLOW_UP -> "follow_up"`）；不得hash enum integer、generated constant name或locale display label。Bytes只有在entry明确声明hex/base64 codec时才允许，且codec是field contract的一部分。

V1明确裁决：`ProjectionSnapshotFrame.snapshot_fingerprint`及所有Foundation派生fingerprint属于`OPAQUE_DOMAIN_AUTHORITY`。Go验证字段间exact join、closed branch、ordered vector和duplicate/conflict，但不声称用本地算法重算Python domain fact。Bubble Tea实施规格中的“Protocol validator”只能调用manifest中明确生成的helper。

Schema/generator gate：

- Python helper结果必须与当前domain builder逐vector相等；
- Go/Python各至少100个Unicode、optional、uint64 boundary和branch golden逐字节相等；
- manifest漏列新增fingerprint field、同field重复分类或覆盖自身fingerprint字段时generation失败；
- `OPAQUE_DOMAIN_AUTHORITY`出现在Go canonical helper switch中是architecture failure；
-任何会改变covered fields、namespace或canonical encoding的修改至少minor bump；改变既有required comparison语义时major bump。

### TUI-PROTO-SCHEMA-004 Behavioral vocabulary与unknown disposition

S1生成Go binding前，所有会改变client control flow的string field必须迁为closed protobuf enum或oneof；只允许下列三种string类别继续存在：

1. identity/fingerprint/timestamp/media type；按字段validator处理；
2. bounded public content；只渲染，不作为branch；
3. bounded opaque stable code；可显示generic diagnostic，但不得决定authority transition。

Behavioral vocabulary矩阵：

| Wire field family | Closed values | Unknown/UNSPECIFIED disposition |
|---|---|---|
| attachment controller disposition | `OBSERVER_ATTACHED`、`CONTROLLER_GRANTED`、`CONTROLLER_UNAVAILABLE_OBSERVER_ATTACHED` | Hello/Attach incompatible，close connection |
| Hello outcome/candidate terminal | outcome=`SERVER_HELLO|NEGOTIATION_WINNER_UNAVAILABLE|HELLO_REJECTED`；terminal=`NEGOTIATION_WINNER_UNAVAILABLE|HELLO_REJECTED`；reason使用TUI-PROTO-HELLO-002的8个closed values；client disposition=`PARENT_RELAUNCH_NEW_CANDIDATE|FATAL_COMPATIBILITY` | unknown branch/reason/disposition fatal compatibility；禁止读opaque error code |
| attach ACK disposition | `ACKNOWLEDGED`、`COMPATIBLE_ALREADY_ACKNOWLEDGED` | transport auth incompatible，close connection |
| bootstrap requirement | `PROJECTION_AND_OPERATIONAL_SNAPSHOT_REQUIRED` | attach rejected |
| heartbeat accepted liveness | `ATTACHMENT_ACTIVE_LEASE_RENEWED`、`SESSION_CLOSING_LEASE_NOT_RENEWED` | heartbeat result incompatible，close connection |
| heartbeat rejection reason | `STALE_ATTACHMENT`、`STALE_TRANSPORT_BINDING`、`ATTACHMENT_REVOKED`、`ATTACHMENT_EXPIRED`、`SESSION_CLOSED` | heartbeat result incompatible，close connection |
| session lifecycle | `OPEN`、`CLOSING`、`CLOSED` | snapshot rejected/fatal compatibility |
| text semantic role | `PRIMARY`、`SECONDARY`、`DIAGNOSTIC`、`CODE`、`TOOL_ARGUMENTS`、`TOOL_RESULT` | containing cell rejected |
| visibility | `ALWAYS`、`NORMAL`、`DIAGNOSTIC_ONLY` | containing cell rejected |
| tool result state | `SUCCESS`、`ERROR`、`DENIED`、`INTERRUPTED` | containing cell rejected |
| interaction kind/state | kind=`APPROVAL|PLAN|MCP_INPUT|EXTERNAL_INPUT`；state=`PENDING|RESOLVED|CANCELLED|FAILED` | containing cell rejected |
| audit kind/severity | exact Foundation audit enum；`INFO|WARNING|ERROR` | containing cell rejected |
| placement/rank | six registered placement kinds；`CONFIRMED_ROOT|ACTIVE_HEAD` | rebase/protocol incompatible |
| capacity/root transition | existing capacity oneof；relation=`STRICT_PREFIX_EXTENDED|REWRITTEN_GENERATION`；cursor=`RETAINED_PINNED` | snapshot/GAP rebuild；unknown required oneof fatal |
| operational replacement | `REPLACE_SAME_KEY|EXPIRE_AT_TERMINAL` | operational generation invalid + operational resnapshot |
| queue states | delivery=`ACCEPTED_PENDING|STEER_RESERVED|FOLLOW_UP_RESERVED|COMMITTED_TO_ACTIVE_RUN|COMMITTED_TO_NEW_RUN|CANCELLED|DELIVERY_REJECTED|RECONCILIATION_REQUIRED`；retention=`ACTIVE|RETIRED`；requested=`AUTO|STEER|FOLLOW_UP`；resolved=`PENDING|STEER|FOLLOW_UP` | control snapshot rejected；mutation disabled until compatible snapshot |
| MCP public request mode | `FORM|URL` | pending interaction rejected；secret action disabled |
| plan-exit decision | `APPROVE|REVISE|CANCEL` | client不得发送；server rejects |
| command outcome status | `SUCCEEDED|REJECTED|PENDING_CONFIRMATION|RECONCILIATION_REQUIRED|SUPERSEDED_BY_COMPATIBLE_WINNER` | command enters QueryRequired；connection marked incompatible if repeated |
| secret kind | `PRIVATE_URL|FORM_RESPONSE` | lease rejected and local handle revoked |
| control changed section/disposition | five TUI-PROTO-OBS-002A sections；`SNAPSHOT_REQUIRED` | control snapshot rebuild/fatal on repeated incompatible frame |

`recovery_kind`、`notice_kind`、`stable_error_code`、`public_result_code`、`fault_code`、`removal_reason`和`stable_reason`若未升级为enum，均属于bounded opaque stable code：Go只能选择generic icon/text并保留原值用于diagnostic，不能据此决定command、cursor、queue、secret或teardown语义。`owner_kind`只作attribution；实际operational render branch由`OperationalActivityCell` oneof决定。`event_type`只作event reference display/debug attribution；Go不维护AgentEvent vocabulary。

Protocol必须维护machine-readable field classification并让schema gate枚举所有string fields。任何新增string若既不在identity/content/opaque-code allowlist，也未迁为enum，generation失败。这样closed vocabulary由Protocol拥有，Go validator只消费generated enum与classification，不生成第二套字符串常量。

## 13. 实施slice

| Slice | Protocol交付 | 当前状态 |
|---|---|---|
| INFRA-5A | schema、framing、hello/attach/heartbeat、snapshot/page | IMPLEMENTED |
| INFRA-5B | projection/operational delta、GAP、bounded reconnect | IMPLEMENTED |
| INFRA-5C | controller、closed mutation、durable receipt/query | IMPLEMENTED |
| INFRA-5D | interaction、secret lease/reveal/submit/revoke | IMPLEMENTED |
| INFRA-5E | test-only Python headless attach/snapshot/delta/page/GAP/command/detach conformance | IMPLEMENTED |
| TUI-BT-S0 | 隔离Go/Python process、TTY、framework与cross-build feasibility spike | PASS；不连接本协议的production adapter |
| GO-READY-A | Protocol 2.0/v2 package原子切换、capability negotiation、bootstrap carrier、transport-auth preface/result、attachment-attempt candidate、stable Hello negotiation winner/current receipt、Attach semantic winner/current receipt、完整Attach ACK/tombstone、closed Heartbeat request/result、完整OperationalSnapshot request/frame、typed queue zero/first-tail head与active bound、operation identity、fingerprint helper与behavioral enum migration | IMPLEMENTED；S1 gate通过 |
| GO-READY-B | atomic five-section control view+source versions+cursor、snapshot control baseline、opaque transition ring及control changed/GAP + three-plane batch wire prerequisite | S1 baseline/schema IMPLEMENTED；live source subscription、observation activation与Go消费为S2 gate |
| GO-READY-C | ServerClosingFrame | SPEC FROZEN；NOT STARTED；S6 blocker |
| TUI-BT-S1 | production Go supervision、只读TTY viewport与cross-language client | IMPLEMENTED |
| TUI-BT-S2-S6 | live observation、mutation/interaction/queue/secret、production packaging与默认入口切换 | NOT STARTED |

## 14. Tests与gate

### TUI-PROTO-GATE-001 Schema

- proto lint/breaking check；
- generated tree clean；
- GO-READY-A一次性证明`PROTOCOL_MAJOR=2`、minor 0、Protobuf full name前缀`pulsara.terminal.v2`、bootstrap exact range 2.0..2.0；major 1只能得到`PROTOCOL_MAJOR_UNSUPPORTED`并close，production/generated/test tree中不存在v1 decoder或dual package；
- no hand-written duplicate wire DTO；
- no raw AgentEvent/storage envelope fields；
- ordinary message secret denylist。
- `DurableHistoryCell`与`OperationalActivityCell`是不相交oneof，旧`TerminalSemanticCell`、`RunLifecycleCell`与unknown fallback branch为零；
- `ProjectionSnapshotFrame`只含durable history entries/root，operational activity只存在独立operational snapshot/frame；
- `OperationalSnapshotRequest/Frame`完整绑定request、runtime session、attachment、current/acknowledged transport binding、generation/cursor与0..256 cells；frame count/bytes/accumulator、1 MiB hard bound和outer fingerprint均由schema/golden验证；
- `HeartbeatRequest/AcceptedReceipt/RejectedReceipt`完整绑定request、attachment、semantic winner、binding与heartbeat generation；accepted liveness和五个rejection reasons穷尽，wire无client-local next-at/missed-count字段；
- every behavioral field使用closed enum/oneof；machine-readable classification覆盖全部remaining string fields；unknown required enum逐branch fail closed；
- fingerprint manifest覆盖全部fingerprint fields且恰好一种class；Go/Python generated helper与Unicode/uint64/optional golden一致；opaque-domain field不得进入canonical helper；
- Hello required/selected capability使用closed enum、canonical set并进入stable `HelloNegotiationSemanticWinner`；Gateway真实拒绝missing required capability；同candidate的pre-Ready重试必须得到byte-identical winner + fresh current-connection receipt，config drift不得重算winner；旧winner不可兑现时必须先CAS candidate terminal receipt再返回closed unavailable outcome，Attach winner必须exact引用Hello winner fingerprint；
- `TerminalTransportAuthResult`、带`attachment_attempt_generation`的`HandshakeRecoveryCandidateIdentity`、`HelloNegotiationSemanticWinner`、`ServerHelloReceipt`、`HandshakeCandidateTerminalReceipt`、三分支`HelloOutcome`、`AttachSemanticWinner`、`AttachResultReceipt`、`ServerPushHeader`、`TerminalControlProjectionSnapshot`和typed `PromptQueueProjectionHead`均为registered closed wire carrier；write success、旧connection receipt、physical request ID、opaque stable code或client-side queue truncation不能替代server receipt/authority；
- attachment controller disposition只有Protocol三个exact值，bootstrap requirement只有`PROJECTION_AND_OPERATIONAL_SNAPSHOT_REQUIRED`；Go generated non-Protobuf value与Python enum golden等集，任何手写mirror/extra value使gate失败；
- `AttachAckResult`完整保留acknowledged binding；ordinary ACK与auth tombstone recovery的nested ACK/rebind proof不得bool-lowering。Heartbeat request/accepted/rejected与OperationalSnapshot request/frame均有完整schema、closed branch、field-presence matrix、fingerprint manifest entry与Python/Go golden；
- control snapshot的五个section/source versions、view fingerprint和cursor只能来自Foundation同锁snapshot；queue只在generation-0 checkpoint的zero-transition/zero-tail状态使用empty-genesis branch，首transition后即使checkpoint仍为generation 0也必须使用携exact tail receipt的committed-head branch，optional half-pair与Gateway分项拼接均无法编码；
- `ProjectionDeltaFrame`只含history changes；旧interaction/queue/status/notification change field numbers永久reserved且无第二semantic path；
- operation/request/local-operation identity由manifest helper生成；wire effect在I/O前已携带exact token，local operation物理无request ID；

### TUI-PROTO-GATE-002 Transport

- unsafe directory/socket/peer UID拒绝；
- partial/oversize/malformed frame；
- concurrent observer/controller；
- heartbeat expiry/takeover；
- 最大合法observation long-poll期间连接仍能在advertised heartbeat期限前返回；advertised wait严格小于heartbeat interval；
- output queue overflow/GAP；
- socket close与stale cleanup。
- bootstrap carrier 16 KiB、single-read、EOF/trailing-byte、expiry/PID/path、buffer wipe与launch authority revoke矩阵；
- auth preface是Hello前唯一可读request，matching typed auth result是唯一成功receipt；同一`credential_id + auth_request_id`的different payload conflict，而same credential/candidate的新physical attempt必须使用fresh request ID/nonce/preface fingerprint并成功进入admission；Hello accepted/unavailable/rejected三分支、candidate terminal CAS/response-loss tombstone、exact-next generation predecessor和parent relaunch cause全部有矩阵测试；
- attachment challenge必须exact 32 bytes；Python/Go重算purpose-bound `terminal-attachment-challenge:v1` commitment、Hello receipt和Attach echo，bytes/commitment/request/connection任一漂移都拒绝。Decoder只能建立operation-bound PREPARED record；promotion先到ACTIVE_PENDING_APPLICATION_ACCEPTANCE，application接纳promotion receipt并完成独立confirmation后才到ACTIVE。Promotion/acceptance result stale/drop/undelivered、constructor/send failure、operation successor、new receipt、connection close、deadline和teardown均必须typed revoke，plaintext不进log/AppState/message；
- candidate generation只覆盖一次semantic attachment attempt：pre-Ready retry稳定，Ready ordinary reconnect严格使用next generation并创建new attachment；Attach response在ACK前丢失时保持same `AttachSemanticWinner`、返回current-request/current-binding `AttachResultReceipt(REBOUND_PRE_ACK | COMPATIBLE_ALREADY_REBOUND_PRE_ACK)`，ACK后丢失只走bounded tombstone recovery；两类rebind都必须使old binding失效；
- initial/reconnect credential的rotation grace、ACK result loss、repeat recovery superseding old physical binding、expiry与secret-free diagnostics矩阵全绿；
- idle EOF/read/write failure不需要outstanding request；unsolicited ServerClosing使用push header，可在各request wait期间被decoder接纳且按逐operation matrix结算；

### TUI-PROTO-GATE-003 Command

- same ID same request exact winner；
- same ID different request conflict；
- disconnect before/after FULL；
- receipt lost then query；
- controller generation stale；
- mutation fromobserver rejected；
- command delivery overflow不重复physical action；
- V1 proto/closed union不存在queue edit或reclassify branch；
- queue cancel FULL后replacement失败时旧item保持cancelled，两个command可独立query。

### TUI-PROTO-GATE-004 History

- 四个history response oneof branch与Foundation disposition逐项映射；
- empty page与cursor stale/rebase不可混淆；
- stale replacement cursor必须携带same-anchor proof；
- rebase使Go client丢弃受影响cache并请求bounded snapshot/root；
- reconciliation不得返回未经证明的root/cursor；
- cursor wire message没有direction字段，request direction逐项映射到唯一`read_page()`调用并在PAGE response回显；
- cursor与request均没有`durable_feed_kind`，PAGE只返回unified root中已全局排序的history entries；
- root identity携带独立presentation-policy与audit-extractor registry fingerprints，它们不被event-domain registry fingerprint替代；
- root identity携带materialization-policy与tree-contract fingerprints；wire page adapter不暴露flat manifest或全量page-reference vector；
- placement key携带contract ID/version/fingerprint并严格验证75-byte fixed framing；six-kind/sentinel/uint/tiebreaker cross-language golden与unknown historical binding fail closed；
- latest root推进不会让旧cursor立即stale；attachment root lease、detach/reconnect重新borrow、retention TTL与GC后stale矩阵全绿；
- root advanced frame原子安装new active head、new latest cursor pair与old pinned-root relation，并严格推进一个projection revision；frame loss触发GAP/snapshot rebuild；
- root advanced frame逐字段映射consumed checkpoint segment cut、covered source-prefix lineage、retained concurrent segment suffix和三个closed resident-transition branch；resulting head可有non-empty tail；
- resident unchanged验证equal vector proof，bounded changes验证ordered upsert/remove count/bytes/accumulator，rebase验证target root/head与bounded token；任一malformed branch fail closed；
- entry/cursor identity使用stable placement key + entry ID；root-local display rank只存在ranked response view，不进入cursor或跨root identity；
- checkpoint I/O期间并发append/noop在FULL frame后作为retained segment suffix继续存在；noop-only suffix有positive segment count/source lineage与zero mutation count；rewrite/retirement post-cut必须rebase/rebuild，不能伪装append；
- available/session-rotation-required/tree-capacity-exhausted/capacity-reconciliation-required四个capacity branch、request-specific admission decision与start-successor-session command均有cross-language golden；ordinary projected count不含terminalization maintenance reserve；
- old-root empty-after-page不能覆盖new latest cursor pair，follow-tail/eviction rehydrate只使用latest pair；pinned cursor只读取自身root；
- run lifecycle golden vector是`AuditCell(run_lifecycle)`，protocol schema不含`RunLifecycleCell`；
- operational activity coalesce/drop不改变projection revision、history root或history page bytes；
- control snapshot中lifecycle/run/interaction/queue/server-notification五个section及source versions与cursor来自同一immutable view owner；逐项Gateway read、local notification覆写server notification、client Tick推进server cursor均被拒绝；server notification 0/16/17、stable ordinal/order及bounded text矩阵全绿；
- control store的different-section concurrent replace在latest global view上rebase；same-section older/same-conflict/gap按source owner generation/revision/fingerprint返回closed disposition。Bootstrap必须先capture、再读五snapshot、再由五source publication sequencer flush/ack并签发fence receipts，最后按global ordinal回放至barrier；registration必须promote为五张store-owned live lease。Read/subscribe race、async callback尚未ack、fence后transition、promotion suffix、READY后错误release、partial-promotion rollback、generation replacement stable identity/mutable snapshot、双lease原子sink切换、锁外old-lease drain、retiring timeout/third-generation fence、source-port锁反转、close分别发生在0/1/4/5项new promotion与swap前后、CAPTURING/partial-live/active/retiring全集typed drain、close deadline保留blocker、512/8 MiB/deadline overflow与迟到interaction/notification candidate全部有对抗测试；
- queue/interaction/run/lifecycle-only change不依赖history delta，必须推进control cursor；Python ring内Changed frame从immutable transition records重建exact section union与opaque range accumulator，ring外/重启/contract change返回ControlProjectionGap；client保留stale view + confirmed cursor、单独安装observed latest cursor并请求不早于它的snapshot，不得用hint覆盖confirmed authority；
- 同一次Observe中control、durable、operational任意组合pending时，`ObservationBatchFrame`包含每个pending plane恰好一个branch；20Hz/100Hz单plane持续推进不能饿死其他plane，per-plane超限必须形成该plane GAP而不能静默省略；
- queue snapshot只投影Foundation active client set；0/1/64项、terminal-row exclusion、reconciliation read-only与第65项admission rejection均有Python/headless golden，任何mapper/client truncation为零；
- headless happy-path只在目标assistant terminal cell真实出现在正式snapshot后继续；任意high-water advance不构成完成证明。目标cell出现后仍允许RunEnd/audit合法推进，client必须通过snapshot/observation追随successor root，直到从最新cursor得到真实`no_change`；
- cross-language golden vectors覆盖每个branch、latest generation/root hints与unknown branch fail-closed。

### TUI-PROTO-GATE-005 Secret

- secret只接受current controller；
- detach/takeover/expiry revoke；
- no snapshot/replay/log/history；
- failed delivery不缓存plaintext；
- reconnect签发新lease；
- bounded bytes/deadline。
- FORM_RESPONSE wire request key必须exact匹配current form slot；stale batch/round owner、controller/attachment generation、owner epoch与TTL均拒绝，expired/ABA handle不能被后续resolve消费；

## 15. Definition of Done

1. `.proto`是唯一wire schema owner，当前Python generated code来自同一commit；Go generation属于deferred `TUI-BT-*`。
2. GO-READY-A物理切换到Protocol 2.0：Python constants为major 2/minor 0，Protobuf package为`pulsara.terminal.v2`，bootstrap只声明2.0..2.0，major 1 typed拒绝且不存在dual decoder/package；capability与hard limits在Hello中exact协商。
3. Runtime directory、socket和peer UID均fail closed。
4. `authority_high_water`、`projection_revision`和operational cursor物理分离。
5. 多observer、单controller及takeover state machine有完整测试。
6. 所有mutation command稳定幂等并可在reconnect后查询winner。
7. UI backpressure永不进入Runtime writer等待链。
8. GAP后只能snapshot rebuild，不猜测缺失delta。
9. 任意client永远不接收或解释`AgentEvent`/raw storage authority；当前由headless conformance client证明。
10. Secret frame无普通replay、snapshot、diagnostic或log路径。
11. Client crash/detach不取消run；server close不遗留attachment/lease。
12. Python Protocol mapper、schema compatibility、wire golden与headless socket conformance全绿；cross-language golden在Go client落地前保持deferred。
13. History wire contract穷尽表达page、stale、rebase与reconciliation；后三者绝不伪装为history end。
14. V1 mutation union不存在queue edit/reclassify；编辑只由cancel成功后新submit表达。
15. History direction只由每次request拥有；cursor与server port method不保存第二份direction。
16. Protocol只暴露一种unified presentation history root和一个page RPC；每个root只有一对directionless cursor，每个attachment只有一个latest pair及bounded pinned old-root cursors。Request/cursor均不含feed kind，client不做transcript/audit merge。
17. `PresentationHistoryRootIdentity`显式绑定presentation-policy与audit-extractor registry fingerprints；cursor通过root fingerprint间接绑定它们。
18. Durable/operational cell oneof物理分离；`RunLifecycleCell`不存在，run lifecycle只是`AuditCell.audit_kind=RUN_LIFECYCLE`。
19. Checkpoint root rollover只通过`PresentationHistoryRootAdvancedFrame`交付；`AuthorityAdvanceFrame`无法改变confirmed root，new latest cursors与active head永不分离。
20. Client wire state区分一个latest-root cursor pair与retention-bound pinned-root cursors；旧root可浏览但不能遮蔽new checkpointed history。
21. History entry/tree/cursor不携带连续ordinal；stable placement key + entry ID是唯一durable anchor，display rank只绑定单个root/active-head view。
22. Root-advanced wire branch完整携带consumed segment prefix、retained segment suffix与closed resident transition；noop-only suffix不会因zero mutations丢失，Python server/headless consumer无标签-only或自由JSON fallback。
23. Capacity state、request-specific growth admission decision与`StartSuccessorSessionCommand`是typed contract；客户端不重算quote/reserve，不能通过继续submit或本地截断绕过session rotation/hard exhaustion。
24. `PresentationHistoryPlacementKey`携带exact registered contract identity与fixed 75-byte framing；Python boundary对unknown binding或typed/bytes mismatch统一fail closed，未来Go adapter必须复用同一golden。
25. 同连接串行V1的observation wait hard max严格小于heartbeat interval；合法long-poll不会使attachment因无法发送heartbeat而自我过期。
26. Operational snapshot/delta消费正式bounded session store并拥有generation/cursor/GAP语义，不是空壳cursor，也不进入durable history identity。
27. FORM_RESPONSE sealed handle exact绑定wire request key、current elicitation batch/round/request-set、controller/attachment generation、owner epoch和TTL；Gateway不丢字段，stale/expired handle未来访问fail closed。
28. Headless conformance happy path按目标assistant terminal cell而非任意high-water推进判定交付完成；后续RunEnd/audit root通过正式observation/snapshot追随至真实`no_change`，不假定两个RPC之间projection静止。
29. Capability negotiation区分client supported/required、server supported/selected；missing required在Hello阶段由Gateway拒绝，不只写transcript。
30. `TerminalClientBootstrapCarrier`有唯一schema、16 KiB one-shot framing、EOF/trailing-byte规则、expiry/PID/path校验和跨语言golden。
31. 每个fingerprint field由manifest唯一分类为wire-recomputable、opaque-domain或physical attribution；Go/Python只从同一manifest生成canonical helper。
32. 所有behavioral vocabulary使用enum/oneof；remaining string field有machine-readable identity/content/opaque-code classification及unknown disposition。
33. Queue/interaction/run/lifecycle-only变化推进独立control cursor；`ObserveNextRequest`携带client cursor，ring内返回带opaque Python-authority连续proof的`ControlProjectionChangedFrame`，ring外返回typed `ControlProjectionGapFrame`；Go不重算record recurrence或section union，也不依赖history delta代理。
34. S4 secret safety只依赖exact revoke/attachment invalidation；S6 `ServerClosingFrame`只增加graceful teardown UX，不成为secret future-borrow safety条件。
35. `ProjectionDeltaFrame`只有durable history changes；interaction、queue、status、notification control fields已物理删除并reserve，不存在第二control delta真源。
36. S1不广告`HISTORY_PAGE_V1`或`CONTROL_PROJECTION_OBSERVATION_V1`，但`PRESENTATION_SNAPSHOT_V1`无条件携带control baseline cursor；S2只有在page、control observation和reconnect rotation真实接线后才广告对应capabilities。
37. Transport auth preface在Hello前验证且matching `TerminalTransportAuthResult`是唯一成功receipt；同request ID different payload conflict，同credential/candidate的fresh physical request被接纳；candidate generation仅覆盖一次semantic attachment attempt，Ready ordinary reconnect必须使用next generation。`HelloOutcome` accepted/unavailable/rejected穷尽；unavailable/rejected先安装stable candidate terminal receipt，response loss只取回same receipt，successor generation exact引用ACK或terminal predecessor。Accepted challenge必须经过operation-bound PREPARED→ACTIVE_PENDING_APPLICATION_ACCEPTANCE→application-confirmed ACTIVE，promotion/acceptance result所有非apply、undelivered或expired出口有owner-signed revoke。`AttachSemanticWinner`跨同代pre-ACK retry稳定，`AttachResultReceipt`永远绑定current request/connection；ACK前response loss和ACK后result loss分别使用typed pre-ACK rebind与bounded tombstone rebind，credential/challenge plaintext不进入AppState/log。
38. Wire operation token在effect构造前已安装并包含exact operation/request ID；local operation使用无RequestID的closed carrier。
39. Projection snapshot的queue section只含Foundation reducer派生的最多64项active projection；server admission保证上限，Gateway/Go不扫描、截断或分页历史queue rows。
40. Idle connection loss使用无RequestID的connection-lifecycle header；ServerClosing使用monotonic push header并可与ordinary response竞速，所有in-flight operation按closed matrix结算。
41. `TerminalControlProjectionSnapshot`由Foundation在同一锁内返回完整五section immutable view + source versions + cursor；Gateway不存在逐项读取或拼接路径，view/cursor fingerprint能证明同一revision。
42. Control Changed/GAP后client物理分离confirmed view/cursor与observed latest cursor，只有不早于minimum observed cursor的atomic snapshot能恢复Fresh/Ready；server generation rebase有typed branch。
43. Control store按source owner generation/revision决议section candidate，different-section并发不伪冲突；bootstrap由capture-then-snapshot + 五张source-owner fence receipts关闭read/subscribe/callback-queue race，READY前验证snapshot→capture-through recurrence、512 candidates/8 MiB/4 attempts/10-second upper bound。五个capture registration必须经同一sink提升为store-owned live leases；generation replacement须store-owned stable attempt identity + mutable resource snapshot、old-live/new-prepare、锁内原子active-sink swap、锁外typed drain，且所有source-port调用不持store lock。Close必须在线性化为CLOSING后取消preparing replacement并按new CAPTURING → new partial live → active/retiring顺序取得完整release receipts；deadline只能保持blocked及资源owner。无理由READY release、partial promotion遗留、retiring timeout后第三代或close漏drain任一代均不可通过。Notification以stable ordinal canonical ordering。
44. Server-projected notification与client-local transient notification物理分离；server vector固定最多16项且只由control snapshot/change推进，client Tick不得改变其内容或fingerprint。
45. `ObserveNextResponse`为最多三plane、每plane最多一branch的`ObservationBatchFrame`或`ObservationNoChangeFrame`；Gateway必须包含所有pending plane，持续高频plane不能饿死control/durable/operational中的其他plane。
46. `PublicFailureCode`由Go唯一operation-registry settlement receipt classifier按registry-derived operation kind、delivery phase、connection state与physical cause生成，再唯一映射`FailureDisposition`；message validator拒绝caller自报或与operation/phase不兼容的code。
47. Prompt queue head使用`EmptyPromptQueueGenesisHead | CommittedPromptQueueHead` closed union；只有zero checkpoint transitions + zero bounded tail使用empty branch，generation-0 checkpoint在首transition后必须被committed branch与exact tail receipt引用，committed branch禁止partial optional pair。GO-READY-AQ修改bootstrap、checkpoint、PostgreSQL repository、migration、mapper和tests完整真源面。
48. Attachment challenge的exact 32-byte codec、purpose-bound commitment、Hello receipt与Attach echo由Protocol manifest唯一定义，Python/Go重算golden一致；request/auth/candidate/winner/connection/challenge任一漂移均fail closed。Decoder installation只能产生有deadline与delivery guard的PREPARED record；promotion result须application接纳并经独立confirmation才成为ACTIVE。Promoted/accepted result stale/drop/undelivered、constructor/send failure、successor、close、deadline与teardown路径全部确认typed prepared/active revoke，无未归属handle。
49. Attach winner的controller/bootstrap vocabulary分别固定为Protocol三个exact值与一个exact值；client-local controller availability/reconciliation不得进入winner或跨语言fingerprint。
50. Heartbeat使用完整typed request与accepted/rejected receipt，exact绑定request、attachment、winner、current binding与monotonic generation；connection-neutral candidate使fresh-binding retry复用same semantic result且不二次延长lease，wire result不携带或构造client-local next-at/missed-count state。
51. Attach ACK的ordinary与auth-tombstone recovery都保留完整validated `AttachAckResult`及acknowledged binding；recovery resulting binding与nested ACK binding物理分层且逐项exact join。
52. Operational snapshot拥有正式request/full-replace frame、256-cell/1-MiB bound、count/bytes/accumulator与outer wire fingerprint；durable与operational bootstrap顺序由client closed loading substate证明，不能用空cursor或单个Loading标签提前Ready。
