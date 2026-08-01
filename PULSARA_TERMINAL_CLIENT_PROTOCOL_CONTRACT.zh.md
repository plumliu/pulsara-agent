# Pulsara Terminal Client Protocol Contract

> 状态：PYTHON BOUNDARY IMPLEMENTED（2026-08-01）；Go/Bubble Tea consumer、PTY与默认TTY activation为DEFERRED
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
    generated/terminal_client_pb2.py               # generated Python
    codec.py                                       # explicit domain/wire adapter
    gateway.py                                     # Unix-socket server and framing owner
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

accept后、读取任何application frame前：

- 读取peer credentials并验证UID；
- 验证one-time launch capability或attach nonce；
- 验证frame protocol major/minor；
- 安装connection ID与input/output byte budgets。

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
ClientHello
  protocol version range
  client_instance_id
  client_build identity
  supported terminal/client capabilities
  requested attachment mode
  launch capability

ServerHello
  selected protocol
  server build/runtime identity
  negotiated limits
  attachment challenge
  supported command/view capabilities

AttachRequest
  exact hello transcript fingerprint
  requested runtime session
  requested role: observer | controller

AttachResult
  attachment identity
  controller disposition
  bootstrap requirement
  heartbeat/lease policy
```

Client capability不得决定server domain semantics；它只决定可选择的presentation/transport branch。Server不应向不支持某view branch的client发送无法解析的required message。

## 5. Attachment与controller ownership

### TUI-PROTO-ATTACH-001 Attachment identity

```text
TerminalClientAttachmentIdentity
  client_instance_id
  connection_id
  attachment_id
  runtime_session_id
  attachment_generation
  role: OBSERVER | CONTROLLER
  issued_at / expires_at
  identity_fingerprint
```

Attachment是process-local capability，不是durable EventLog fact。Reconnect必须创建新attachment generation；旧attachment不能复活。

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

Heartbeat只证明attachment liveness，不推进projection cursor。Server冻结interval、grace和maximum missed count。Event loop stall不得自动被解释为user cancel；expiry只revokeclient capabilities并detach observation。V1同一physical connection串行处理request，因此任何`ObserveNext` long-poll上限必须严格小于heartbeat interval，并冻结为不超过interval的一半；hello只能广告该实际上限。合法的单次observation wait不得占满整段attachment lease或挤掉本连接下一次heartbeat。未来若允许更长等待，必须先引入独立并发reader/multiplexing contract，不能只提高数字。

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

Operational plane必须有正式的session-owned bounded store，而不是只返回空cursor：store拥有当前generation、monotonic cursor、bounded activity map/ring与snapshot fingerprint；`OperationalSnapshotRequest`返回当前bounded cells，`ObserveNext`按客户端generation/cursor返回typed ordered delta、no-change或GAP。Coalesce/drop只改变operational generation/cursor和activity bytes，不推进durable projection revision或history root。Store无durable replay承诺；process restart开启新generation，旧cursor收到GAP并重新请求operational snapshot。

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

- session lifecycle；
- exact `PresentationHistoryActiveHeadIdentity`；
- ordered resident `PresentationHistoryRankedEntry`；
- exact `PresentationHistoryLatestRootCursorPair`；
- pending interaction event-safe view；
- queue head/view；
- logical status values；
- bounded notifications；
- cursor join。

它不包含operational activity、raw event、private URL、form response、continuation plaintext、Python class/module identity或database row。Frame的`authority_high_water`必须精确等于active head identity的`through_authority_sequence`；latest cursor pair必须绑定active head中的same confirmed root，root/checkpoint/registry/tail字段不允许由mapper分别填充。Ordered resident entries可包含nested confirmed root的bounded window与active head的bounded uncheckpointed mutations；placement key是stable identity，display rank只绑定snapshot active-head basis。Page cursor仍只绑定confirmed root。Initial attach/reconnect的activity由独立`OperationalSnapshotFrame(operational_generation, operational_cursor, ordered_activity_cells)`交付；它与Projection snapshot没有共同atomic fingerprint或cursor。

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
      interaction replace/clear
      queue replace
      status replace
      notification add/remove
```

Changes使用stable domain IDs；client不得按display text推断identity。`resulting_authority_high_water`必须等于resulting active head identity的through sequence。Empty delta非法，same-root authority-only advance通过lightweight `AuthorityAdvanceFrame(base_active_head_fingerprint, resulting_active_head_identity)`表达或留待下一个snapshot；它不推进projection revision，但client必须原子替换current active head identity。任何confirmed-root变化都禁止走该branch。

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
```

Secret frames：

- 不进入ordinary observation/command replay buffer；
- 不进入snapshot、delta、diagnostic、trace或structured log；
- 不启用compression；
- 使用独立strict byte cap和short deadline；
- repr/log interceptor只能输出constant redacted marker；
- decode后立即交给secret service或client secret state；
- 失败后不得把plaintext缓存为retry payload。

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
- Python shutdown显式发送server closing frame，再revoke leases和关闭socket；
- child crash/kill不得遗留terminal mode；该性质由S0和PTY integration gate验证。

## 12. Schema evolution

### TUI-PROTO-SCHEMA-001 Compatibility

- 禁止复用field number；
- removed field永久reserved；
- required semantic branch通过oneof + application validator表达；
- enum zero值必须`UNSPECIFIED`并在required位置拒绝；
- new optional field需要minor bump；
- new required behavior或changed meaning需要major bump；
- breaking check比较committed schema baseline。

### TUI-PROTO-SCHEMA-002 Domain mapping

每个domain-to-wire mapper有golden vectors，验证：

- stable IDs/cursors无损；
- closed union branch一致；
- secret fields不存在于ordinary messages；
- unknown domain branchfail closed；
- wire round-trip不被用作domain semantic fingerprint算法。

## 13. 实施slice

| Slice | Protocol交付 | 当前状态 |
|---|---|---|
| INFRA-5A | schema、framing、hello/attach/heartbeat、snapshot/page | IMPLEMENTED |
| INFRA-5B | projection/operational delta、GAP、bounded reconnect | IMPLEMENTED |
| INFRA-5C | controller、closed mutation、durable receipt/query | IMPLEMENTED |
| INFRA-5D | interaction、secret lease/reveal/submit/revoke | IMPLEMENTED |
| INFRA-5E | test-only Python headless attach/snapshot/delta/page/GAP/command/detach conformance | IMPLEMENTED |
| TUI-BT-S0 | 隔离Go/Python process、TTY、framework与cross-build feasibility spike | IN PROGRESS；不连接本协议的production adapter |
| TUI-BT-S1-S6 | production Go process supervision、TTY renderer与cross-language client packaging | DEFERRED |

## 14. Tests与gate

### TUI-PROTO-GATE-001 Schema

- proto lint/breaking check；
- generated tree clean；
- no hand-written duplicate wire DTO；
- no raw AgentEvent/storage envelope fields；
- ordinary message secret denylist。
- `DurableHistoryCell`与`OperationalActivityCell`是不相交oneof，旧`TerminalSemanticCell`、`RunLifecycleCell`与unknown fallback branch为零；
- `ProjectionSnapshotFrame`只含durable history entries/root，operational activity只存在独立operational snapshot/frame；

### TUI-PROTO-GATE-002 Transport

- unsafe directory/socket/peer UID拒绝；
- partial/oversize/malformed frame；
- concurrent observer/controller；
- heartbeat expiry/takeover；
- 最大合法observation long-poll期间连接仍能在advertised heartbeat期限前返回；advertised wait严格小于heartbeat interval；
- output queue overflow/GAP；
- socket close与stale cleanup。

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
2. Protocol major/minor、capability与hard limits在hello中exact协商。
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
