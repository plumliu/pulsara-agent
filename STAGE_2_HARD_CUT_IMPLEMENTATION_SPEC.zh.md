# Pulsara Stage 2 hard-cut 实施规格

状态：**IMPLEMENTED；S2-G ACTIVATED IN CURRENT DIRTY WORKTREE；STAGE 3 PHYSICAL DELETE NEXT**

目标：在一次 production authority activation 中启用 **Canonical relational conversation kernel with selective domain, effect, and work journals**。

上游架构真源：[PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)

前序实施基线：[STAGE_0_1_IMPLEMENTATION_SPEC.zh.md](STAGE_0_1_IMPLEMENTATION_SPEC.zh.md)

切换运行手册：[DURABILITY_SUBTRACTION_CUTOVER_RUNBOOK.zh.md](DURABILITY_SUBTRACTION_CUTOVER_RUNBOOK.zh.md)

目标 vocabulary oracle：[durability_subtraction_stage0_target_oracle.json](tests/fixtures/durability_subtraction_stage0_target_oracle.json)

激活证据：[durability_subtraction_stage2_activation.json](benchmarks/suites/core/v1/durability_subtraction_stage2_activation.json)

## 1. Outcome

Stage 2不是继续软化旧EventLog，也不是把151类旧event换名后搬进新表。它完成第一次真正的authority cut：

- conversation、tool、job、memory与coordination current truth直接进入PostgreSQL canonical rows；
- exact 26类`CommittedAgentEvent`只记录accepted occurrence；
- exact 23类`LiveAgentEvent`只存在于当前Host进程；
- tool/job physical attempt保存无法从conversation row替代的effect/work lineage；
- Protocol v3、Inspector、context compiler和TUI读取同一canonical truth；
- reopen只做conversation rehydrate，不做execution replay；
- 旧EventLog、segment、projection、receipt与recovery graph在activation时从production composition不可达，物理删除留给Stage 3–5。

本规格冻结契约、事务边界、物理关系、交付切片与gate。它不规定每个Python类名、函数拆分、mock布局或逐文件提交顺序；实现者可以在不改变本文不变量的前提下选择内部结构。

## 2. 当前基线与进入条件

### 2.1 当前事实

起草时Git HEAD为`5b7ad9f7ffc8565bc572180b2bde0c81ab64473a`，Stage 0/1实现仍位于known dirty worktree。原计划是在Stage 2前提交独立Stage 0/1 partial checkpoint；实际实施没有制造事后假checkpoint，而是由`durability_subtraction_stage2_handoff.json`显式记录`stage0_1_checkpoint_head=null`并保留combined WIP。这个偏差降低提交历史的切片可审查性，但没有引入dual authority、兼容写入或运行时恢复语义；后续提交必须如实保留该连续历史，不能伪造前序HEAD。

Stage 0真源冻结：

- current durable `EventType` inventory：151；
- current disposition：39/25/16/71；
- target：26 Committed、23 Live、13 subject slots、2 append guards；
- formal `CustomEvent`、`ToolOutcomeUnknown`与独立`RawProvider*` target均为0。

2026-08-08完整`uv run pytest -q`实测为2848项：`2843 passed, 3 failed, 2 skipped`，collection error为0。其中一个失败是前序规格的intentional SHA drift，已刷新fixture并定向验证通过；剩余两个test属于同一个legacy terminal notification checkpoint account/head join failure family：

- `tests/test_host_core.py::test_host_terminal_monitor_registration_completion_and_autonomous_delivery`
- `tests/test_host_core.py::test_host_terminal_monitor_repeated_progress_without_reregistration`

这两个legacy red不要求在Stage 2前修旧join协议；它们必须在terminal authority-cut slice中与旧durable monitor/checkpoint/reconciliation owner一起删除或由新Host-scoped契约测试替换。

### 2.2 Stage 2入口不是全绿gate

本仓库不发布Stage 0/1中间版本。full pytest在dormant construction期间是迁移观测，失败数量不阻止Stage 2主路径。每个失败只需至少具有初步分类、root owner和以下一种disposition：

~~~text
delete | replace | repair-in-stage2 | baseline-refresh | environment
~~~

不修复不等于忽略：不得用skip/xfail、删除测试或吞异常伪造绿灯。新增失败若属于即将删除的legacy authority，可以继续悬挂；若暴露canonical transaction、physical dispatch fence、数据库完整性或新schema安全问题，则由拥有该不变量的Stage 2 slice修复。它不要求先回到Stage 1完善旧架构。

### 2.3 编码前最小检查点及实际disposition

开始第一个Stage 2 production diff前原应具备：

1. 已提交的Stage 0/1 partial checkpoint HEAD；实际未满足，handoff以`null`和combined-WIP policy显式记录，不允许追溯伪造；
2. exact 151 inventory和26/23/13/2 oracle仍可重复；
3. 当前全部pytest失败node ID及disposition清单；
4. complete-reset/quiesce runbook存在但未被coding agent执行；
5. 当前Host、worker、terminal、subagent physical owner仍有可测试的stop/cancel/join边界。

缺少完整Stage 0/1 DoD不阻止进入；缺少可辨识基线或无法保护用户dirty changes则停止。

handoff由`tests/fixtures/durability_subtraction_stage2_handoff.json`另行保存，冻结实际checkpoint disposition、本规格与上位架构文档的SHA-256、26/23/13/2 oracle SHA、完整pytest red node ID及disposition。该manifest由外部文件保存规格hash，不能在本文件内自引用；当独立checkpoint缺失时必须写`null`和真实worktree policy，不能用当前HEAD冒充Stage 0/1 checkpoint。

## 3. Scope与非目标

### 3.1 Stage 2必须交付

- fresh relational conversation schema与canonical repositories；
- session Host writer fencing和独立job-attempt claim fencing；
- canonical command idempotency；
- entry/event两条独立、commit-ordered、session-scoped sequence；
- assistant ordered blocks、tool call/attempt/result pairing与dispatch-before-effect fence；
- immutable context snapshot和turn-local binding revision；
- exact 26类selective committed journal及closed subject FK union；
- exact 23类process-local live protocol、assembler、bounded bus与live-control owner；
- Protocol v3 Python Gateway、generated contract和Go consumer；
- `CommittedObservationProjection`与canonical blob content读取；
- minimal durable job aggregate/attempt kernel及foreground-reachable first-party handlers；
- PostgreSQL-only memory candidate/governance/fact/relation/FTS/pgvector/two-hop path；
- Host-scoped yielded terminal与subagent execution lifetime；
- fresh-DB dogfood、故障矩阵和单次activation wiring。

### 3.2 Stage 2不交付

- 旧数据import、converter、cold reader、archive或reverse projection；
- old/new dual write、shadow authority、按session feature flag或在线translator；
- coroutine、provider transport、model stream、pending interaction、subagent execution或terminal process recovery；
- universal historical AgentEvent decoder；
- durable model stream segment/coalescing；
- generic command receipt、candidate、checkpoint、repair或consumer ACK graph；
- ordinary hook durable cursor、catch-up、reliable mode或generic extension job；
- 第三种event append guard；
- subagent durable job/attempt/claim/background flag；
- yielded terminal跨Host rebind/adopt；
- memory delete/forget产品语义；
- raw SPARQL、Oxigraph adapter、第二graph store或超过现有bounded two-hop的查询能力；
- transcript/event prefix retention、archive、session delete或legal-hold功能；
- 逐model-call exact context-input audit；
- Stage 3–5要求的全部旧文件和旧表物理删除。

## 4. Delivery与activation模型

Stage 2允许多个可独立审查的dormant construction切片，但普通Host只能发生一次authority activation：

~~~text
dormant schema/repositories
  -> fresh-DB direct runner + live plane
  -> canonical readers/context/Inspector
  -> Protocol v3 Python + Go
  -> minimal jobs/memory/lifetime integration
  -> isolated fresh-DB dogfood and calibrated limits
  -> one reset-only production composition activation
~~~

`dormant`表示普通Host、settings、session metadata和feature flag均不能激活新authority。tests可以直接构造新composition；production composition在最终activation之前仍只使用旧authority。不得让text先切新schema而tool仍写EventLog，也不得让v2 TUI读取新row或v3 TUI读取旧projection。

S2-A的新关系物理上统一位于独立PostgreSQL schema `pulsara_v3`。migration owner创建schema与关系；legacy production runtime role没有`USAGE`或对象权限，dormant repositories只通过显式授权的test/construction role访问。所有SQL、repository与FK都必须显式schema-qualified，禁止依赖`search_path`把`public.sessions`等旧表误解析成新authority。S2-G在quiesce后执行complete reset、从empty universe重跑migration，只向new runtime role授予`pulsara_v3`；`public`中的legacy product tables即使因旧migration序列仍为空壳，也必须保持空且对new composition不可达，物理删除仍由Stage 3–5完成。

每个construction slice必须让自己的targeted gate和retained-safety gate为绿。完整legacy suite允许携带已分类red，但不得增加unclassified failure。最终activation release必须删除或替换所有已切走owner的旧contract test；届时full suite中的剩余失败必须全部属于明确不在本次release运行的environment scope，不能仍由active production authority触发。

Production reset、真实部署、清理数据库/blob或quiesce外部effect需要用户/operator另行明确授权。coding agent只能在ephemeral fresh PostgreSQL和临时blob namespace执行reset/dogfood。

## 5. Target physical planes

依赖方向冻结为：

~~~text
Host writer / exact job-attempt worker
  -> canonical relational transaction
       -> canonical row or physical attempt
       -> same-transaction StoredCommittedEvent when required
  -> post-commit best-effort tap

Gateway / context / Inspector
  -> canonical rows
  -> optional selective occurrence query
  -> disposable read-time projection

provider/tool-result adapter
  -> sanitizer + normalizer
  -> process-local assembler + LiveAgentEventBus
  -> completed frozen draft
  -> canonical adapter
~~~

禁止反向依赖：

- canonical repository不得通过event replay证明row；
- live plane不得import durable event serializer、EventLog或projection job；
- hook/TUI/Inspector不得获得canonical mutation port；
- job worker不得写transcript或subagent coordination；
- presentation、index、audit或observer失败不得回滚canonical commit。

## 6. Physical schema contract

### 6.1 Active product relations

Stage 2冻结下列24个active product relations。表中名称是逻辑短名，其物理全名一律为`pulsara_v3.<relation>`；实现中的未限定product relation name属于architecture violation。`public`旧表可以在Stage 2–4暂时以empty、production-unreachable schema壳存在，但不计入active authority；Stage 5负责物理删除。`schema_migrations`属于基础设施，不计入24张产品表。

| # | relation | authority与最小职责 |
|---:|---|---|
| 1 | `sessions` | workspace scope、lifecycle、writer lease/generation、entry/event/queue high-water |
| 2 | `session_commands` | session-wide command id唯一accepted action与typed target；不是receipt graph |
| 3 | `turns` | user turn、ROOT/task conversation scope、每scope RUNNING唯一、completed/interrupted、final entry、current context revision |
| 4 | `turn_context_binding_revisions` | turn-local immutable revision与FULL_HISTORY/SNAPSHOT base union |
| 5 | `context_snapshots` | immutable summary/blob、source cut/hash、compiler/prompt/model contract |
| 6 | `transcript_entries` | append-only user/assistant/tool-result entries；job/subagent result只能经Host接受为唯一ROOT source entry |
| 7 | `assistant_message_blocks` | ordered text/data/tool-call blocks及exact call identity/ordinal |
| 8 | `tool_execution_attempts` | unique-per-call pre-dispatch attempt、authorization、remote identity、retry attribution |
| 9 | `tool_results` | exact call唯一terminal result、attempt join或closed no-attempt branch |
| 10 | `prompt_queue_items` | durable FIFO、closed delivery-target union与PENDING/CONSUMED/CANCELLED/REJECTED CAS |
| 11 | `interaction_decisions` | accepted capability/human decision、durable subject、redacted disposition |
| 12 | `subagent_tasks` | Host-owned accepted task、parent、generation、closed status/reason |
| 13 | `subagent_task_children` | immutable message/result child、stable exact id/kind/ordinal与task-scoped entry FK |
| 14 | `durable_jobs` | first-party named background intent及stable handler-specific intent identity、safety class、finite retry与每attempt provider request上限、aggregate state/result |
| 15 | `durable_job_attempts` | claim generation/lease/deadline、attempt lineage、set-once provider admission、remote identity、terminal/unknown |
| 16 | `memory_candidates` | durable typed proposal intake；decision前不进入normal recall |
| 17 | `memory_governance_decisions` | skip/submit/correct/merge/supersede/contradict及exact lineage |
| 18 | `memory_facts` | accepted memory与既有superseded/stale lifecycle |
| 19 | `memory_relations` | canonical direct relation与bounded two-hop source |
| 20 | `memory_search_index` | disposable PostgreSQL FTS read model |
| 21 | `memory_vector_index` | disposable pgvector read model |
| 22 | `memory_index_state` | 每workspace/channel单行desired/applied generation与handler-contract watermark；不复制refresh job状态，无per-fact receipt/repair owner |
| 23 | `blobs` | purpose-neutral immutable logical bytes metadata与storage identity |
| 24 | `agent_events` | selective `StoredCommittedEvent` occurrence journal |

Stage 2 migration使用当前registry之后的next ordinal，起草时预期为`0013_conversation_kernel_hard_cut.sql`及对应catalog fixture。若实施前registry已前进，只顺延ordinal，不改变schema语义，也不得复用或改写已发布migration。该migration创建`pulsara_v3`及其中全部24个关系，不在`public`原地alter同名legacy authority。

### 6.2 Common identity与scope

- session-owned relation以`(session_id, id)`提供唯一键；需要workspace约束的relation同时保存由session派生并受composite FK约束的`workspace_id`；
- `sessions`提供`UNIQUE(id, workspace_id)`；caller不能自由声明不一致workspace；
- session identity在一个data universe内永不复用；complete reset后旧session直接不存在；
- ID由application预生成并在ACK unknown时复用；数据库唯一约束决定winner；
- user-visible时间使用数据库accepted timestamp；transport timing只进入Operational plane；
- closed enum不得使用free-form fallback；unknown schema/version fail closed。

### 6.3 Session与ordered high-water

`sessions`至少保存：

~~~text
id, workspace_id, lifecycle
writer_generation, writer_lease_owner_id, writer_lease_expires_at
latest_entry_sequence, latest_event_sequence, latest_prompt_queue_sequence
created_at, updated_at
~~~

entry sequence与event sequence是两条不同的session-scoped contiguous public sequence：

- `entry_sequence`只在插入canonical transcript entry的transaction内分配；
- `event_sequence`只在同transaction接受0到少量committed occurrence时分配；
- 两者都先锁exact `sessions` row，按commit order分配并推进high-water；
- rollback不得留下high-water空洞；
- event sequence不替代entry ordering、不授权mutation、不保存consumer cursor；
- schema没有retention lower bound、prune cursor或epoch。

`prompt_queue_sequence`是第三个、只服务durable ingress FIFO的session-local high-water；它不进入history/observation cursor，也不代替entry/event sequence。三者共用session row作为窄分配锁，但只由各自mutation推进。

### 6.4 Session-wide command idempotency

`session_commands`采用：

~~~text
PRIMARY KEY (session_id, command_id)
command_kind, request_schema_version, semantic_digest
target_kind
target_turn_id | target_entry_id | target_queue_item_id
| target_interaction_decision_id | target_job_id
accepted_at
CHECK exactly one target slot matches command_kind
~~~

它保存accepted user action及canonical target，不保存pending/unknown/confirmation/reconciliation状态、query token、delivery receipt或consumer observation。

同一`command_id`重试时：

1. join exact target；
2. digest只作快速比较，最终以versioned typed canonical fields确认semantic equality；
3. same input返回已有target；
4. different input返回typed conflict且不写第二个action；
5. `QueryCommand`为read-only，不要求writer generation。

应用层先查再插不能替代主键。创建target与`session_commands`必须同transaction，FK可`DEFERRABLE INITIALLY DEFERRED`。

### 6.5 Prompt queue

`prompt_queue_items`冻结为一个无durable claim/lease的ordered inbox：

~~~text
id, session_id, queue_sequence, command_id, client_submission_id
delivery_mode = NEW_TURN | STEER_ACTIVE_TURN
target_turn_id?
status = PENDING | CONSUMED | CANCELLED | REJECTED
typed content slot
consumed_entry_id?, terminal_reason?, accepted_at, terminal_at?
UNIQUE(session_id, queue_sequence)
CHECK NEW_TURN iff target_turn_id IS NULL
CHECK STEER_ACTIVE_TURN iff target_turn_id IS NOT NULL
composite FK/DB constraint: target_turn_id is an exact same-session ROOT turn
~~~

- enqueue先锁exact session row，从`latest_prompt_queue_sequence + 1`分配stable order，并在同transaction写command、item与`PromptQueued`；`STEER_ACTIVE_TURN`还必须锁定并验证enqueue时为RUNNING的exact ROOT `target_turn_id`，rollback不推进high-water；
- queue head永远是最小`(queue_sequence, id)`的`PENDING` item，later item不得越过它；head暂不eligible时只能等待、被cancel，或由closed system rule reject；准备、wake hint与reservation只在进程内，不写`CLAIMED`、owner、lease、checkpoint或receipt；
- Host crash前未commit的consume不改变row，item保持`PENDING`，新Host按canonical order继续；
- consume、cancel、reject只能以数据库CAS从`PENDING`竞争，恰有一个terminal winner，失败方读取winner而不补写transition；
- `NEW_TURN` consume在同transaction创建turn、revision 0、user entry，把item置`CONSUMED`并引用该entry，同时追加`PromptConsumed`与`UserMessageAccepted`；
- `STEER_ACTIVE_TURN` consume只能锁定row中已冻结的`target_turn_id`；该turn仍为RUNNING时创建steer entry、置`CONSUMED`并追加`PromptConsumed`与`UserSteerAccepted`，不得解析或转投“当前另一个active turn”；target已COMPLETED/INTERRUPTED时同一Host-owned head transaction确定性转`REJECTED(reason=TARGET_TURN_TERMINAL)`并追加`PromptRejected`，随后FIFO才可前进；
- user cancel使用current Host guard与command id，system rejection使用current Host guard与closed reason；二者分别追加`PromptCancelled`/`PromptRejected`。

因此queue restart恢复的是row level与stable order，不是claim recovery。edge notification丢失时Host仍通过level query发现最早`PENDING` item。

### 6.6 Transcript、assistant blocks与content slots

`turns`额外保存closed conversation scope：

~~~text
conversation_scope_kind = ROOT | SUBAGENT_TASK
scope_subagent_task_id?
CHECK ROOT iff task slot is NULL; SUBAGENT_TASK iff exact task FK is non-NULL
~~~

同一session只能有一个RUNNING root turn；每个subagent task也至多一个RUNNING task-scoped turn，由数据库partial unique/closed transition约束。task-scoped turn属于exact subagent task，仍由相同Host writer与session entry/event allocator保护，不创建第二个session、writer或execution authority。

`transcript_entries`按`UNIQUE(session_id, entry_sequence)`append-only，并从exact turn继承conversation scope。closed `entry_kind`至少包括：

~~~text
USER_MESSAGE | USER_STEER
ASSISTANT_MESSAGE | ASSISTANT_TOOL_REQUEST
TOOL_RESULT
~~~

provider-generated assistant entry必须保存exact `context_binding_revision_id`与`provider_input_through_sequence`，且后者严格小于新entry sequence。user/tool result不得伪造这两个字段。

ROOT-visible external source使用closed nullable source FK union：ordinary user input不带source FK；job/subagent result acceptance分别携带exact `source_job_id`或`source_subagent_result_id`。数据库constraint必须保证source slot至多一个、non-null source只出现在ROOT `USER_MESSAGE` entry、job source是same-session SUCCEEDED job、subagent source是same-session RESULT child，并保证每个source在同一session至多被接受为一个ROOT entry。source FK不能藏在JSON payload，application precheck不能代替这组composite FK/constraint。

SUCCEEDED durable job result不能直接创造第27种conversation event。当前Host若要把它带回conversation，必须以`source_job_id`唯一约束创建新的command-addressable turn/continuation，并使用`UserMessageAccepted(source=JOB_RESULT)`观察该acceptance；worker自己的`JobTerminalAccepted`不等于conversation已经接受结果。

task-scoped subagent result也不能通过未命名的cross-scope binding直接进入ROOT compiler。当前Host显式接受exact result时，在一个transaction中创建ROOT-scoped entry（指向仍RUNNING的exact ROOT turn，或由显式new-turn command创建新的ROOT turn/revision 0）、安装`source_subagent_result_id` exact result-child FK与session-local unique，并追加唯一`UserMessageAccepted(source=SUBAGENT_RESULT)`。若parent turn已terminal，Runtime不得自动创建新turn或把result注入另一个active turn；result仍作为task child可查询，只有后续显式accept command可创建new ROOT turn。ACK unknown按source FK/command查询winner。

job/subagent external result进入**既有RUNNING ROOT turn**时还必须位于provider safe point：没有active prepared-input handle、没有仍在执行/尚未physical exit的model call，并且上一条assistant tool-request的全部calls已经terminal。Host使用同一个短process-local admission lock线性化“external-source entry transaction”与“冻结下一份prepared-input handle”：

- external acceptance先赢时，entry commit后下一份handle的`provider_input_through_sequence`必须覆盖它；
- prepared-input freeze先赢时，直到该provider operation physical exit并完成assistant transaction disposition前，external acceptance不得commit对应ROOT acceptance command、ROOT entry或`UserMessageAccepted`，只能保留process-local wake/返回typed not-at-safe-point；source job/task result及其自身domain occurrence仍可正常commit并继续作为durable level state，下一safe point重新query即可；
- Host crash可丢失process-local wake，但不能丢失source result；显式accept command可用同一command id重试，未发生canonical acceptance时不得伪造accepted receipt；
- 该lock只跨短read/accept transaction，不跨provider stream持有数据库锁或session-wide semantic-write lease。tool result、queue status、memory与其他非ROOT-input canonical mutation仍可按各自authority并发commit。

accepted entry、ordered blocks及其inline/blob content在session lifetime内完整保留。detach/reattach、compaction、storage pressure和committed observation budget均不得删除、覆盖或重排这些facts。

`assistant_message_blocks`以`UNIQUE(assistant_entry_id, block_ordinal)`排序。closed kind为`TEXT | DATA | TOOL_CALL`；thinking不进入canonical transcript。tool-call block还必须满足：

- `(session_id, assistant_entry_id, tool_call_id)`唯一；
- call ordinal在message内唯一且immutable；
- tool name与完整validated arguments属于canonical block；
- parent及全部blocks在同一transaction all-or-nothing插入；
- parent未commit时任何call均不可dispatch。

所有Protocol可见正文slot使用数据库约束的exactly-one union：

~~~text
InlineContent(canonical_bytes, digest, size, media_type, codec)
  XOR
BlobContent(blob_id FK, digest, size, media_type, codec)
~~~

空正文由zero-length inline branch表达。descriptor必须与blob row一致；blob FK全部`ON DELETE RESTRICT`。

### 6.7 Tool attempt与result

`tool_execution_attempts`：

- 每logical call最多一行，数据库unique call constraint；
- physical invoke前commit；没有confirmed attempt不得调用adapter；
- 保存authorization、actor、可用idempotency key与cross-call `retry_of_attempt_id`；
- explicit retry必须来自新turn/new call；
- remote identity只允许从NULL原子发布一次，且与`ToolRemoteIdentityPublished`同transaction；
- 不保存executor coroutine或execution recovery state machine。

`tool_results`：

- 每call至多一个terminal row并拥有唯一canonical transcript entry；
- normal branch必须引用exact attempt；
- no-attempt branch只能为`INVALID_ARGUMENTS | PERMISSION_DENIED | TOOL_UNAVAILABLE | CANCELLED_BEFORE_DISPATCH`；
- attempt存在但result缺失在read time推导为outcome_unknown；
- call无attempt/result在read time推导为not_dispatched；
- late exact outcome只能填充尚无result的旧call；旧turn和历史assistant不改写。

同一tool-request message全部call拥有terminal result后才能follow-up provider call，lowering按call ordinal而非完成顺序。

### 6.8 Context snapshot与binding revision

- initial revision 0与user entry/turn同transaction安装；
- `UNIQUE(turn_id, revision_ordinal)`，旧revision不可覆盖；
- base为`FULL_HISTORY`或`SNAPSHOT(context_snapshot_id, source_through_sequence)`；
- compiler的FULL_HISTORY/post-snapshot delta只读取该turn的exact conversation scope；ROOT只通过已经成为ROOT entry的`source_subagent_result_id` acceptance看到child result，不存在cross-scope binding table、JSON ID列表或因session-wide entry sequence交错而自动泄漏；
- snapshot source upper bound严格早于本turn user entry；
- 后续revision只能在provider safe point新增，并原子推进turn current pointer；
- provider safe point对ROOT input admission的closed条件是：active prepared-input handle为0、active model operation为0、上一assistant tool-request的全部calls已terminal；同一短process-local lock同时保护下一handle freeze、revision pointer推进与job/subagent external-source entry acceptance的先后次序；
- provider dispatch前的repeatable-read准备阶段返回process-local immutable handle，冻结revision id与`provider_input_through_sequence`；
- assistant commit只能消费该handle，不能重读latest sequence；
- unreferenced snapshot可GC；被revision引用后受FK保护；
- compaction不删除、覆盖、重排transcript；
- V1不保存逐model-call exact compiled request audit。

### 6.9 Subagent coordination

- task status exact为`PENDING | ACTIVE | COMPLETED | FAILED | INTERRUPTED | CANCELLED`；
- `SubagentTaskAccepted`总是接受initial `PENDING`；后续ACTIVE或terminal transition才发`SubagentTaskStatusAccepted`，dependency wait只保留PENDING/ACTIVE并走`SubagentProgress`，dependency failure使用`FAILED(reason=DEPENDENCY_FAILED)`；
- pending/active只对创建它的`execution_writer_generation`有效；
- message/result使用独立stable child id、closed kind和ordinal；
- child内部accepted conversation使用本session中的`SUBAGENT_TASK(task_id)` scoped turns、entries、assistant blocks、tool attempts与tool results；`subagent_task_children`只以exact FK引用需要作为parent-child coordination公开的scoped entry，不复制正文；
- task-scoped普通assistant/tool-request/tool-result继续使用统一canonical relation与相应committed occurrence，因而tool attempt天然引用真实assistant block/call，不需要call-subject union或伪造parent transcript；
- event subject必须引用exact child，不能只指task；
- explicit/inferred result竞争同一identity，数据库只接受一个winner；
- task result被ROOT消费是后续独立Host acceptance；每个result child至多产生一个ROOT source entry，child occurrence本身不证明ROOT已经消费；
- schema没有background、subagent execution attempt/claim/lease/checkpoint/retry或child RuntimeSession字段；conversation scope不是可恢复execution session；
- close/takeover把旧generation nonterminal task幂等置`INTERRUPTED`；
- reattach不resume/requeue；重新委派创建new task id。

### 6.10 Minimal durable jobs

V1 job safety class：

~~~text
RETRY_SAFE | REMOTE_QUERYABLE | NON_IDEMPOTENT
~~~

`durable_jobs`的aggregate status exact为：

~~~text
PENDING | ACTIVE | SUCCEEDED | FAILED | CANCELLED | OUTCOME_UNKNOWN
~~~

job还保存immutable intent/version、origin、safety class、accepted result reference，以及以下不允许caller省略或设为unbounded的retry contract：

~~~text
retry_policy_id, retry_policy_version
maximum_attempts >= 1
attempt_timeout_ms > 0
next_eligible_at
provider_input_token_limit_per_attempt?           # LLM handlers required
provider_output_token_limit_per_attempt?          # LLM handlers required
~~~

attempt表保存ordinal、claim generation/lease、`deadline_at`、remote identity、result/error、retry lineage、terminal/unknown，以及LLM attempt的set-once `provider_call_started_at`与该次bounded request的input/requested-output token数；`attempt_ordinal <= maximum_attempts`由数据库/claim predicate共同保证。V1每个LLM-backed attempt至多一次provider call，因此`maximum_attempts × 每attempt request上限`直接给出整个job的有限provider调用/请求上界，不再建立累计usage account。

- claim/progress/result/failure只校验exact attempt + claim generation；
- Host takeover不得使合法worker commit失败；
- stale claim不得commit；
- retry-safe只有在attempt count与due time仍允许时创建new attempt并保留retry_of；
- remote-queryable先observe已有remote identity；
- non-idempotent lease loss进入outcome_unknown，不自动重跑；
- worker不能写transcript、turn、prompt、interaction或subagent row；
- SUCCEEDED job进入conversation需要当前Host独立accept，并以`source_job_id`唯一；
- 无origin session的global job不写session `agent_events`。

Stage 2 production catalog只允许具名first-party handlers：background compaction precompute、post-compaction memory extraction/governance，以及FTS/pgvector所需的coalesced memory index refresh。index refresh是workspace/global derived work，不伪造origin session，也不向session journal追加job occurrence。generic extension action、subagent execution、yielded terminal/monitor与foreground safe-point compaction不得注册。

production safety class不留给registration caller选择：

| handler | V1 safety class | 自动再次调用provider/effect的边界 |
|---|---|---|
| background compaction precompute | `RETRY_SAFE` | 允许；输入由immutable source cut/hash固定，attempt output在terminal winner前不得成为canonical snapshot |
| post-compaction memory extraction | `RETRY_SAFE` | 允许；只有winning terminal attempt可原子安装proposal bundle，旧attempt不可直接发布candidate |
| memory governance | `RETRY_SAFE` | 允许；只能读取exact candidate，只有winning attempt可安装唯一decision/fact/relation |
| coalesced FTS/pgvector index refresh | `RETRY_SAFE` | 不调用provider；只按target generation重建derived rows |
| `REMOTE_QUERYABLE` handler | **0** | future handler必须另行schema/catalog review |

这些LLM-backed handler之所以允许自动重新调用provider，是因为V1 contract禁止它们调用tool或产生canonical外部effect，且只允许winning attempt的单一PostgreSQL acceptance；重复provider计费/计算是该显式safety选择的一部分。任何handler一旦需要tool、remote mutation或无法把attempt output隔离到winner commit，就不得继续注册为上述type，必须停用或通过新ADR改变catalog。

bounded retry执行规则：

1. enqueue时由closed handler catalog写immutable retry policy/version、finite maximum attempts/timeout与LLM每attempt input/requested-output token caps；request payload和runtime config不能把它们改成`None`、0-as-unlimited或更宽值；
2. retryable failure只在`attempt_ordinal < maximum_attempts`时把aggregate回到`PENDING`，并按versioned deterministic policy从数据库accepted time与ordinal计算`next_eligible_at`；可使用由job id确定的jitter，但不能每次scan重新随机；未到期不得claim；
3. 每次LLM provider dispatch前，exact claim transaction以attempt row的NULL→set CAS安装唯一provider-call admission，并保存versioned tokenizer得到的input token数与requested maximum output tokens；二者必须分别不超过job冻结的per-attempt cap。确认该transaction后才可调用provider，ACK unknown按exact attempt row查询；同attempt的marker已存在时physical provider call为0；
4. attempt deadline到期先terminalize当前attempt；RETRY_SAFE且仍有attempt时按第2条排下一attempt。maximum attempts耗尽、request超过per-attempt cap，或没有合法retry的timeout发生时，同一claim-owned transaction把aggregate稳定置`FAILED`并保存closed terminal reason `ATTEMPT_TIMEOUT | RETRY_EXHAUSTED | PROVIDER_REQUEST_LIMIT_EXCEEDED`，有origin session时恰好追加一次`JobTerminalAccepted`；中间attempt failure不发该event；
5. 有限attempt数、每attempt最多一次provider call与每次request上限共同形成静态hard bound；不保存跨attempt累计call/token counter，不新增budget account、reservation/settlement table、receipt或reducer。

### 6.11 Memory与index freshness

- 只保留现有五类proposal；
- foreground `remember_*`的candidate row与对应tool result同transaction提交，成功只表示proposed；
- automatic extraction也先提交candidate；
- governance只claim committed candidate并异步完成；foreground reply、turn completion和close不等待；
- accepted decision、fact/relation/lifecycle及对应occurrence同一job-attempt-owned transaction提交；
- skip只terminalize candidate，不产生memory fact/event；
- candidate在decision前不进入normal recall；
- exact fact/direct relation在canonical commit后立即可见；
- `memory_index_state`的freshness target只是一对watermark：`desired=(generation, handler_contract_id/version)`与`applied=(generation, handler_contract_id/version)`；它没有job id/status/reason；
- fact/relation/lifecycle canonical transaction对每个受影响的`FTS | VECTOR` channel锁定该row，在当前desired handler contract下推进`desired_generation`；generation推进与canonical memory mutation同commit，不依赖enqueue/notification成功。真实handler contract变更由catalog/migration owner推进desired contract watermark，不伪装成memory mutation；
- notification只作edge hint；每个worker启动时及周期性扫描`desired != applied`。automatic refresh intent稳定绑定exact `(workspace_id, channel, target_generation, handler_contract_id, handler_contract_version)`，数据库保证同一key至多一个automatic job aggregate；scanner只能查询、claim或等待该winner，不能因为它`FAILED`而为同一key创建successor；
- refresh attempt冻结上述target tuple，重建对应index rows，并在同一PostgreSQL transaction安装rows、无回退地推进applied watermark；若desired并发继续前进，下一轮scanner可以为更高generation或新handler contract创建新key；
- 同一key的attempt耗尽时只把该唯一job aggregate置`FAILED`；`memory_index_state`不复制`EXHAUSTED`、reason或job id。scanner以stable key bounded join该job，发现terminal failure后保持它，不得把`desired != applied`解释成新automatic job授权；
- Stage 2不交付same-key operator retry command/API。只有更高desired generation或真实的新handler contract形成新automatic key；未来若需要人工重试，必须以独立maintenance契约/ADR保存actor与failed-job causation，不能由scanner、notification或通用repair owner伪造；
- `memory_index_state`每workspace/channel只保存desired/applied generation及其handler-contract watermark；job active/exhausted truth只存在于`durable_jobs`，不保存per-fact receipt或通用repair owner；
- memory query disposition exact为`COMPLETE | PARTIAL_STALE(channels, desired, applied) | PARTIAL_UNAVAILABLE(channels, reason_code)`；stable key尚无terminal failure时，stale返回当前可用indexed candidates并显式标partial；exact unique automatic job已经exhausted时，query通过bounded join返回`PARTIAL_UNAVAILABLE(..., INDEX_REFRESH_EXHAUSTED)`并省略该channel。两者仍合并可由exact canonical fact/direct-edge/bounded-two-hop query得到的结果，但不得做unbounded全表scan fallback，也不得把partial标成complete；
- direct relation expansion保持现有bounded最多两跳；
- Oxigraph配置、composition、surface、worker、Inspector health与network call均为0；
- 没有delete/forget入口或状态机。

## 7. Fencing、append authority与transaction

### 7.1 两种且仅两种guard

~~~text
EventAppendGuard =
    HostWriterGuard(session_id, writer_generation)
  | JobAttemptClaimGuard(job_id, attempt_id, claim_generation, origin_session_id)
~~~

`CommittedEventAppender`是storage/application内部sealed port。普通hook、plugin、TUI、Inspector、recorder、terminal monitor和subagent callback不能获得它。

### 7.2 Writer lease

- 新session从generation 1开始；
- acquire/takeover锁session row并原子推进generation；
- takeover在同transaction把旧generation running turn和nonterminal subagent task置interrupted并追加events；
- renew只允许exact owner + generation；失败立即停止新mutation并cancel foreground；
- observer attachment不获取writer lease；
- Protocol controller generation不能代替数据库writer generation。

### 7.3 Uniform lock order

任何需要session event的transaction严格遵守：

1. 锁exact `sessions` allocator row；
2. 校验Host guard，或锁exact job attempt后校验claim guard与origin session；
3. 锁/插入本domain canonical subject；
4. 分配entry sequence（若有）；
5. 插入0到少量typed events并分配连续event sequence；
6. 推进session high-water；
7. commit；
8. commit后才向process-local tap best-effort offer。

不得先锁job/memory row再回头锁session。global无origin-session work跳过session event append。

首次job claim不是由尚不存在的`JobAttemptClaimGuard`授权。storage/application内部只暴露sealed `claim_attempt()` transaction：

1. 无锁定位candidate id，不向worker返回执行capability；
2. 有`origin_session_id`时先锁`pulsara_v3.sessions` allocator row；global job跳过该步且不会追加session event；
3. 锁定并重新校验exact job仍eligible、未terminal、cancel规则与catalog safety class；
4. 创建新attempt及lease/claim generation，或按safety contract换代可reclaim attempt；
5. 只在该transaction内部签发`JobAttemptClaimGuard`，插入`JobAttemptAccepted`并推进origin session event high-water；
6. commit确认后才向worker返回包含exact attempt与generation的execution capability。

因此bootstrap仍只有两种guard：guard是sealed claim transaction的结果，不是caller预先持有的第三种authority。claim commit ACK unknown时按job/current attempt identity查询winner；不得在未确认时启动handler。

### 7.4 Transaction matrix

| mutation | guard | 同transaction rows | committed occurrence |
|---|---|---|---|
| user submit | Host | command、turn、revision 0、user entry | `UserMessageAccepted` |
| user steer | Host | command、steer entry | `UserSteerAccepted` |
| final assistant | Host | assistant entry/blocks、turn completed | `AssistantMessageAccepted` + `TurnCompleted` |
| assistant tool request | Host | complete entry + all blocks/calls | `AssistantToolRequestAccepted` |
| machine policy allow | Host | capability decision + tool attempt | `CapabilityDecisionAccepted` + `ToolAttemptAccepted` |
| machine policy deny | Host | decision + no-attempt result/entry | `CapabilityDecisionAccepted` + `ToolResultAccepted` |
| require confirmation | Host | capability decision；pending request仅live | `CapabilityDecisionAccepted` |
| human interaction allow | Host | command + decision + attempt | `InteractionDecisionAccepted` + `ToolAttemptAccepted` |
| human interaction deny | Host | command + decision + no-attempt result | `InteractionDecisionAccepted` + `ToolResultAccepted` |
| remote identity publication | Host | attempt set-once identity | `ToolRemoteIdentityPublished` |
| normal/late tool result | Host | result + entry | `ToolResultAccepted` |
| turn interruption | Host | turn status/reason | `TurnInterrupted` |
| prompt enqueue | Host | command + ordered `PENDING` queue row + closed target-turn union | `PromptQueued` |
| prompt consume | Host | queue `PENDING→CONSUMED` + exact new-turn或frozen-target steer entry/turn/revision | `PromptConsumed` + exact user-entry occurrence |
| prompt cancel/reject | Host | command as applicable + queue `PENDING→terminal` CAS | exact `PromptCancelled | PromptRejected` |
| compaction adoption | Host | new revision + current pointer | `CompactionAdopted` |
| subagent create/status | Host | task | exact task occurrence |
| subagent message/result | Host | exact child + referenced task-scoped entry | exact child occurrence及entry kind要求的occurrence |
| subagent result进入ROOT | Host | new ROOT turn，或provider-safe existing ROOT + command as applicable + entry + `source_subagent_result_id`唯一 | `UserMessageAccepted(source=SUBAGENT_RESULT)` |
| job enqueue | Host或valid parent claim | job | `JobQueued` |
| first/retry job attempt acceptance | sealed claim transaction | attempt/lease；transaction内mint Job claim | `JobAttemptAccepted` |
| aggregate job terminal | Job claim | attempt + aggregate terminal | `JobTerminalAccepted` |
| SUCCEEDED job result进入conversation | Host | command、new ROOT turn，或provider-safe existing ROOT + `source_job_id`唯一 | `UserMessageAccepted(source=JOB_RESULT)` |
| memory acceptance | Job claim | decision + fact/relation/lifecycle | exact memory occurrence |
| candidate intake、skip、private retry failure、snapshot precompute | owning guard | work/canonical row only | none |

一次transaction可产生多条event，但不得为满足计数拆transaction。event只能由该transaction已知的accepted transition构造，不能事后补写。

## 8. AgentEvent target contract

### 8.1 三个物理base

~~~text
CommittedAgentEventBase -> PostgreSQL serializer + selective journal
LiveAgentEventBase      -> process-local registry + bounded bus
OperationalEventBase   -> sampled diagnostics/trace
~~~

三者可以复用纯schema定义，但不得共享queue、serializer、retention、receipt或failure semantics。

### 8.2 Exact 26 Committed core

namespace固定为`pulsara.core`。production registry、type→subject数据库CHECK、type→guard sealed appender entrypoint/SQL predicate和fixture必须由一个静态descriptor生成并exact匹配oracle。guard不保存为event metadata或row字段。

| type | subject | guard |
|---|---|---|
| `UserMessageAccepted` | entry | Host |
| `AssistantMessageAccepted` | entry | Host |
| `AssistantToolRequestAccepted` | entry | Host |
| `ToolResultAccepted` | entry | Host |
| `TurnCompleted` | turn | Host |
| `TurnInterrupted` | turn | Host |
| `UserSteerAccepted` | entry | Host |
| `CapabilityDecisionAccepted` | interaction decision | Host |
| `InteractionDecisionAccepted` | interaction decision | Host |
| `ToolAttemptAccepted` | tool attempt | Host |
| `ToolRemoteIdentityPublished` | tool attempt | Host |
| `PromptQueued` | queue item | Host |
| `PromptConsumed` | queue item | Host |
| `PromptCancelled` | queue item | Host |
| `PromptRejected` | queue item | Host |
| `CompactionAdopted` | binding revision | Host |
| `SubagentTaskAccepted` | subagent task | Host |
| `SubagentTaskStatusAccepted` | subagent task | Host |
| `SubagentMessageAccepted` | exact message child | Host |
| `SubagentResultAccepted` | exact result child | Host |
| `JobQueued` | job | Host或Job claim |
| `JobAttemptAccepted` | job attempt | Job claim |
| `JobTerminalAccepted` | job | Job claim |
| `MemoryFactAccepted` | memory fact | Job claim |
| `MemoryFactLifecycleChanged` | memory fact | Job claim |
| `MemoryRelationAccepted` | memory relation | Job claim |

payload只允许event-time closed enum、reason、actor、visibility、ordinal或bounded usage summary；不得复制完整message、arguments/result、memory正文、private URL、secret、callback identity或canonical mutable state。

跨SQL CHECK、event payload与generated fixtures的closed enum必须共用同一descriptor：`CapabilityDecisionAccepted.decision = ALLOW | DENY | REQUIRE_CONFIRMATION`且只表示machine policy；最终human allow/deny只由`InteractionDecisionAccepted`表示。`SubagentTaskStatusAccepted.status = ACTIVE | COMPLETED | FAILED | INTERRUPTED | CANCELLED`（initial PENDING只在`SubagentTaskAccepted`）；`JobTerminalAccepted.status = SUCCEEDED | FAILED | CANCELLED | OUTCOME_UNKNOWN`，不得出现`COMPLETED` alias或unknown string fallback。

### 8.3 Stored event与subject integrity

`agent_events`至少包含：

~~~text
event_id
workspace_id, session_id, event_sequence
namespace, event_type, schema_major, schema_minor
accepted_at, occurred_at
actor_kind, actor_id
sensitivity_class, projection_profile
typed bounded payload
13 nullable typed subject columns
~~~

13个物理slot exact为：

~~~text
subject_turn_id
subject_entry_id
subject_tool_attempt_id
subject_job_id
subject_job_attempt_id
subject_queue_item_id
subject_interaction_decision_id
subject_context_binding_revision_id
subject_subagent_task_id
subject_subagent_message_id
subject_subagent_result_id
subject_memory_fact_id
subject_memory_relation_id
~~~

数据库必须保证：

- `UNIQUE(session_id, event_sequence)`；
- exactly-one subject非NULL；
- type→subject closed CHECK；type→guard由当前transaction的sealed entrypoint、SQL predicate和role privilege校验，不新增guard metadata列；
- session/workspace/origin-session composite FK；
- subagent child literal kind；
- subject FK均`DEFERRABLE INITIALLY DEFERRED ON DELETE RESTRICT`；
- payload hard cap为64 KiB canonical JSON bytes；
- unknown type/version无raw JSON fallback。

event随session lifetime全量保留。没有TTL、prune、archive、consumer cursor或repair owner。

### 8.4 Exact 23 Live core

~~~text
TextStart TextDelta TextEnd
ThinkingStart ThinkingDelta ThinkingEnd
DataStart DataDelta DataEnd
ToolCallStart ToolCallDelta ToolCallEnd
ToolResultStart ToolResultDelta ToolResultEnd
InteractionOpened InteractionReplaced InteractionClosed
TerminalProcessCompleted
TerminalMonitorOpened TerminalMonitorObservation TerminalMonitorClosed
SubagentProgress
~~~

- vendor SDK item只在adapter stack；独立`RawProvider*`/逐delta draft为0；
- Start是frozen announce，Delta只更新单一assembler；
- End携带final frozen block/view、ordinal、size与digest；
- ToolResult End不是canonical acceptance proof；
- bus sequence只在process generation内有效；
- 同进程提供按event与byte双上限的bounded snapshot，携带generation id、retained-from与through sequence；不承诺跨进程live replay；
- ToolResult Delta使用closed text/data projection，End只携带frozen live view；
- overflow只GAP/detach，不等待queue；
- Host crash后不合成历史Start/End；
- live registry不得接受event sequence、durable serializer或receipt。

### 8.5 Operational与hooks

TTFT、transport retry、provider error detail、buffer/backpressure、cache、index lag、hook failure、blob integrity和orphan process只进OperationalEvent/trace。

- live hook：process-local、best-effort、bounded；
- post-commit hook：只从registration cut后接收tap offer，无catch-up；
- operational hook：允许采样；
- callback异常/timeout/overflow不影响run或commit；
- registration、callback、recorder、assembler、live owner和lease不得进入event metadata；
- ordinary hook默认typed/redacted；
- authenticated first-party user可原样看到已投影raw thinking；
- raw thinking extension projection只可授予first-party Inspector/debug的短期session lease；未redacted tool arguments需要独立capability且仍受single-item/queue byte hard cap；
- tool args阈值内完整，超限UTF-8-safe截断并携带total bytes/digest；dispatch始终使用完整arguments；
- private URL只给current-controller interaction view；S3 secret永不构造为event；
- 唯一policy是独立`ToolDispatchAuthorizationPolicy`，decision为`Allow | Deny | RequireConfirmation`，rewrite fields为0；machine default 2秒、hard cap 5秒；unavailable转confirmation，无controller时deny。

V1 formal custom event publisher与third-party durable action均为0。

每个hook registration最少绑定：Host认证得到的`extension_principal_id`、stable `handler_id`与manifest digest、process-local `registration_id`、process/session/turn等closed scope、exact plane/type/projection major、projection profile、capability set、revocable lease generation/expiry、registration cut、event+byte queue budget、callback deadline与close drain budget。registration、lease、cut和queue均不落库，Host restart后必须重新注册。

同一observer按bus/tap接受顺序投递；callback完成顺序不构成产品顺序。revoke/expiry立即停止新callback并丢弃未开始delivery；已开始callback只运行到自己的deadline。`LiveGap`、`LiveControlGap`与`HookGap`是bounded delivery-control frame，不属于23类formal LiveAgentEvent，也不能进入durable registry。

Host close先停止registration与新delivery，丢弃未开始callback，只在全局close drain budget内等待已开始callback；超时cancel/detach。callback business completion不进入Host close correctness。

## 9. Runtime contract

### 9.1 Submit、stream与assistant commit

1. current writer接受command、turn、user entry与revision 0；
2. context preparation在read-only repeatable-read cut中冻结revision与`provider_input_through_sequence`；
3. adapter完成decode/sanitization后直接构造LiveAgentEvent并交给单一assembler/bus；
4. completed draft包含frozen ordered blocks、完整validated arguments与prepared-input handle；它不是durable candidate；
5. final reply在一个transaction提交assistant/turn/events；
6. tool request在一个transaction提交完整mixed message与全部calls，之后才运行policy；
7. 只有Allow decision与attempt commit确认后才能physical invoke；
8. results可以按完成顺序分别commit，但follow-up按call ordinal且等待全部terminal。

steady-state text目标2个canonical transaction；one-tool physical happy path目标5个，remote identity若需单独公开则6个。新增context snapshot/revision单列，不以checkpoint隐藏。

### 9.2 Crash、open与rehydrate

open/takeover transaction获取writer generation，把旧running turn和旧generation nonterminal subagent task置interrupted，并追加相应events。它保留tool request、attempt与已有result，但不恢复pending interaction、provider assembler、tool future、child RuntimeSession或terminal monitor。

provider lowering必须以正在构造的exact `provider_input_through_sequence = H`为判断cut，而不是用“现在是否已有result”：

- `result.entry_sequence <= H`：在该cut使用exact matching result；
- target historical assistant的cut早于result：原call在该历史位置继续使用当时的versioned、provider-only `ProviderToolResultClosure`；
- call无attempt：closure为`interrupted_before_dispatch`；
- attempt无result，或result晚于该历史cut：closure为`interrupted_may_have_partially_executed`；
- 实际result只从其真实`entry_sequence`起进入未来cut，并降低为typed provider-only `LateToolOutcomeObservation`，不能倒插替换早先closure或改写任何accepted assistant attribution。

closure与late-effect observation都不写canonical result/event、不授权retry。每次lowering逐条比较result sequence与相关assistant冻结的cut；“reopen时result已经存在”不等于历史provider曾看见它。只有新turn可以再次调用模型或产生新call。

### 9.3 Terminal与subagent lifetime

- yielded terminal handle仅在当前Host owner内有效；
- orderly close终止owned process group并bounded join；
- crash后的新Host不按PID、旧process id、event或job adopt/relaunch；
- terminal monitor/progress只走LiveAgentEvent，不建durable job/checkpoint/receipt；
- subagent physical execution、partial output、capacity和MCP owner只在当前Host；
- close/takeover将nonterminal task置interrupted；accepted children保留；
- reattach不resume/requeue，重新委派使用new task id。

这组实现替换当前两个terminal legacy red；不得为了旧test变绿而恢复durable monitor account/head join。

### 9.4 Host close

close只有三个逻辑阶段：

1. stop ingress、new hook registration、provider/tool/subagent/terminal admission；
2. 在共享hard deadline内cancel/join仍使用session资源的foreground、terminal、subagent及尚未物理删除的Stage 1 derived owner，并只把turn/task的canonical status收口为completed/interrupted；
3. flush Host-owned canonical writer，撤销process-local capability/live owner，然后释放pool、blob client、executor与workspace资源。

close不更新foreground tool attempt“状态”：attempt row本身immutable；`outcome_unknown`只能在read time由attempt存在、result缺失且所属turn已interrupted确定性派生。close不等待background job、memory governance/index追平、hook业务完成、TUI delivery或derived presentation成功。background job claim独立存活。尚未physical exit且仍可访问session resource的task不得detach后伪造close成功；总hard deadline为5秒。V1没有session-close core event。

## 10. Protocol v3与read contracts

### 10.1 Hard cut与snapshot

Protocol major固定为v3。Python server、generated fixtures和Go client同release切换；不提供v2/v3 translator。

fresh attach在一个read-only repeatable-read transaction返回：

~~~text
CanonicalSessionSnapshot {
  session/workspace identity
  lifecycle and current canonical control
  entry_sequence_cut
  event_sequence_cut
  bounded newest entries
  older_history_cursor?
}
~~~

`current canonical control`是closed sections，不允许Gateway临场聚合任意表：session lifecycle恰一项；active root turn为0或1；task-scoped nonterminal turn/task不超过subagent admission hard cap；pending prompt queue返回最早N项、total count与stable queue cursor；active/unknown tool视图只覆盖snapshot entry suffix及上述active scopes；nonterminal jobs返回bounded page与cursor；memory freshness固定返回`FTS`、`VECTOR`两个channel。每个N都有S2-F冻结的finite default/hard cap；超cap使用该section的typed continuation或整个snapshot typed resource-exhausted，不能静默截断后声称complete。pending interaction不在canonical section中。

history cursor exact语义为`(session_id, cut_sequence, entry_sequence)`。每page绑定原cut；新commit不混入。没有retention GAP、root generation、rank basis或presentation checkpoint。

### 10.2 Committed observation

wire DTO不是StoredCommittedEvent：

~~~text
CommittedObservationProjection =
    EventOnly
  | ImmutableEntryProjection
  | CurrentControlProjection
~~~

poll在一个repeatable-read cut读取`H = latest_event_sequence`、完整`(after,H]`suffix及exact subjects。预算内完整返回`through_event_sequence=H`；超event/byte/time预算或schema不兼容只返回GAP，不返回半suffix。notification只是edge hint。

`ImmutableEntryProjection`携带ordered `ObservationContent`；Go不得凭subject id猜正文。read-time projection不落表、不建cursor/checkpoint/repair。

26个core type到projection branch的映射closed如下，generated Python/Go fixtures必须exact覆盖，不能由client按payload猜测：

| projection branch | exact committed types |
|---|---|
| `ImmutableEntryProjection` | `UserMessageAccepted`、`AssistantMessageAccepted`、`AssistantToolRequestAccepted`、`ToolResultAccepted`、`UserSteerAccepted` |
| `CurrentControlProjection` | `TurnCompleted`、`TurnInterrupted`、`CapabilityDecisionAccepted`、`InteractionDecisionAccepted`、`ToolAttemptAccepted`、`ToolRemoteIdentityPublished`、`PromptQueued`、`PromptConsumed`、`PromptCancelled`、`PromptRejected`、`CompactionAdopted`、`SubagentTaskAccepted`、`SubagentTaskStatusAccepted`、`JobQueued`、`JobTerminalAccepted`、`MemoryFactAccepted`、`MemoryFactLifecycleChanged` |
| `EventOnly` | `SubagentMessageAccepted`、`SubagentResultAccepted`、`JobAttemptAccepted`、`MemoryRelationAccepted` |

`ImmutableEntryProjection`必须包含`conversation_scope = ROOT | SUBAGENT_TASK(task_id)`。subagent child occurrence只提供coordination audit/notification；task transcript由同transaction的普通entry occurrence产生唯一immutable projection，因此一个entry不会因child event重复投影。`CurrentControlProjection`仍是该observation read cut的canonical current state，不是event-time row副本。

### 10.3 Live到committed交接

provider与tool-result live generation使用exact process-local identity：

~~~text
LiveGenerationKey {
  owner_epoch, session_id, turn_id, conversation_scope
  channel = MODEL_OUTPUT | TOOL_RESULT(tool_call_id, attempt_id)
  generation_id, proposed_entry_id
}

LiveBlockKey { LiveGenerationKey, block_id, block_ordinal, block_kind }

LiveGenerationSettlement =
    Committed(entry_id)
  | Aborted(reason_code)
~~~

`proposed_entry_id`与block id在stream开始前由当前Host预生成，successful canonical commit复用这些identity；未commit时它们只是process-local values，不是durable candidate或authority。Start/Delta/End均携带generation/block identity；并行tool results还必须携带exact call/attempt。terminal/subagent live extension使用各自owner-scoped channel identity，但没有canonical replacement承诺。

`LiveGenerationSettlement`是可丢失的v3 process-local delivery-control hint，不是第24类`LiveAgentEvent`、committed event或journal row。live bus sequence与committed `event_sequence`各自独立；Protocol不再创造跨两平面的`attachment_sequence`、ACK或第三种ordering authority。两平面的唯一join key是successful commit复用的canonical entry/block identity。

交接规则冻结为：

1. Live Start/Delta/End只创建provisional renderer；End表示frozen live block，不表示canonical acceptance；
2. commit确认后Host可以best-effort发布`Committed` hint并触发committed level-read；hint只要求client立即查询，不把draft升级为canonical exact content；
3. 若committed projection因并发poll先到，它作为authority立即按`proposed_entry_id`、turn/scope及call identity替换并retire匹配generation；之后迟到的Delta/End/settlement一律丢弃并记Operational diagnostic；
4. settlement或End先到而projection未到时只触发bounded level-read；超budget、GAP或identity conflict转fresh canonical snapshot。client以已有canonical entry cache拒绝同identity的迟到live frame，不需要跨平面reorder buffer或retired-delivery cursor；
5. `Aborted`或LiveGap只丢弃对应generation draft；Host crash使attachment与全部settlement state消失，重连直接读canonical snapshot，不合成历史Start/End。

committed GAP的重建范围是该session的post-snapshot entry tail与全部current-control cache；已验证且早于旧snapshot suffix floor的immutable history page可以保留。provider LiveGap只清对应generation draft，live-control GAP只重读current interaction。三种GAP不得互相冒充，也不得通过event replay恢复execution。

### 10.4 Canonical content

~~~text
ObservationContent = InlineContent | CanonicalBlobReference

ReadCanonicalContent(reference, offset_bytes, limit_bytes,
                     authenticated_context)
  -> CanonicalContentChunk
~~~

reference只定位closed transcript content edge，不是bearer capability、private URL或raw blob id。每个chunk先在短read transaction重新校验attachment、scope、capability、subject、slot和descriptor，然后结束transaction再做bounded storage read。client验证chunk与完整digest。

missing/corrupt显示typed placeholder并产生redacted diagnostic，不回滚entry、不找替代blob、不启动repair。durable receipt、lease、cursor、projection和event均为0。

### 10.5 Live control

~~~text
SessionLiveControlSnapshot {
  session_id, owner_epoch, live_revision, current_interaction?
}

LiveControlEvent = InteractionOpened | InteractionReplaced | InteractionClosed
~~~

`snapshot_and_subscribe()`在一个process-local owner lock内冻结snapshot并注册更高revision observer。epoch不等于writer generation且不落库。GAP/reconnect重新level-read；Host takeover使用new epoch、revision 0和empty value。

resolution携带expected writer generation、epoch、revision、interaction id与command id。live lock跨越短accepted-decision transaction；commit成功后清空current并best-effort offerClosed，rollback保持current。secret不进入snapshot/event/canonical plaintext。

### 10.6 Go client

- cache按`conversation_scope + entry_sequence`，不按presentation root/rank；root transcript默认只呈现`ROOT`，task view显式选择exact task scope；
- committed GAP按10.3替换canonical tail/current control并fresh snapshot；
- provider LiveGap清除exact generation renderer；generation变化或settlement冲突回canonical snapshot；
- canonical committed projection永远胜过provisional live frame，retired generation的迟到frame不得复活draft；
- control GAP重新`snapshot_and_subscribe()`；
- inline直接渲染，blob只经canonical content port；
- digest验证前不得标记exact/final；
- raw thinking按first-party view原样显示；
- tool argument truncated DTO不得回流dispatch；
- ACK unknown用同command id querycanonical target。

## 11. Blob publication与GC

- digest针对storage解压/解密后、semantic codec decode前的logical bytes；
- 小内容inline，大内容先写immutable blob并验证，再由canonical transaction安装FK；
- blob write失败只终止尚未commit的对应mutation；
- unreferenced blob采用24小时orphan grace；
- GC只选无FK引用blob，最终竞态由FK/RESTRICT裁决；
- referenced blob不可由TTL、index cleanup或observer删除；
- 允许无损压缩/加密/物理迁移，但logical descriptor不变。

初始inline threshold固定64 KiB canonical bytes；content read单chunk server hard cap固定1 MiB。activation前可据probe向下收紧；向上调整必须重跑memory/latency gate。

## 12. Dormant construction slices

### S2-A：Schema、descriptor与repositories

交付创建`pulsara_v3`的next migration、24 active relations、FK/CHECK/unique/privilege、26/23/13/2 descriptor与fixture生成、writer/allocator/appender、sealed first-claim transaction和repository transaction。legacy runtime role无schema权限，production Host不得构造。

Gate：fresh DB deep verify、`public`/`pulsara_v3`同名表并存且无误路由、search-path negative、subject negative matrix、queue target union、ROOT/task RUNNING唯一、job/subagent source unique、finite job policy CHECK、index-refresh automatic intent key unique、Host/job append race、first-claim bootstrap、sequence rollback/commit order、stale generation/claim rejection。

### S2-B：Fresh conversation runner、effect与live plane

交付test-only submit/open/rehydrate/direct runner、无claim prompt queue CAS、prepared-input handle、sanitizer/assembler/bus、completed draft、tool message/attempt/result/policy path、Host-scoped terminal及same-session task-scoped subagent adapter。

Gate：text/tool/multi-tool crash windows、queue order/consume-cancel-reject race、target turn terminal rejection与restart pending、child internal tool subject、job/subagent result single ROOT acceptance、external-source acceptance vs prepared-input freeze的两种100→101→102调度、每task单RUNNING turn、Start immutability、Delta single assembler、overflow nonblocking、no durable segment/raw carrier、message-before-dispatch与attempt-before-invoke。

### S2-C：Canonical readers、context与Inspector

交付canonical query ports、context rematerializer、ProviderToolResultClosure、late-effect lowering、snapshot/binding adoption、Inspector canonical/selective views。

Gate：rehydrate不replay、missing audit仍可继续、mid-turn attribution、late result按每条assistant cut不倒插、append-only scoped history、Inspector不以event证明row。

### S2-D：Protocol v3与Go

交付proto v3、repeatable-read Gateway、26-type projection mapping、identity-based live/committed handoff、canonical content reader、generated artifacts、Go scoped sequence cache、observation reducer、content hydrator和live renderer。

Gate：cross-language fixtures、26-type projection exact且child/entry无重复immutable projection、MVCC cut、live-before/after-committed乱序、retired-generation late delta、lost notification、三类GAP/reconnect、snapshot section caps、blob scope/integrity、thinking、tool argument complete/truncated、ACK unknown、v2/v3拒绝。

### S2-E：Minimal jobs、memory与capability closure

交付job kernel、named handlers、candidate/tool-result atomic intake、PostgreSQL-only memory query/freshness、Oxigraph-free composition，并证明subagent/terminal不进job catalog。

Gate：first/retry claim fencing、catalog safety exact、finite attempts/timeout/deterministic due、one-call-per-attempt与per-attempt request cap、exhaustion唯一terminal event、Host takeover independence、unique winning result acceptance、governance async、desired/applied同tx与lost-wake scanner、index-refresh stable intent key、同target exhausted后automatic successor为0、Stage 2 operator-retry surface为0、三种query disposition、two-hop等价、Oxigraph network count 0。

### S2-F：Dogfood、limits与activation report

交付ephemeral fresh-DB Host/TUI dogfood、named limits、transaction/event/owner/close测量、activation manifest与full pytest disposition刷新。

Gate：所有Stage 2 slice/retained-safety tests为绿；full suite没有unclassified red；production composition仍未激活。

### S2-G：Single authority activation

同一release完成：

- 普通Host切new kernel；
- Protocol v3成为唯一terminal protocol；
- foreground-reachable background capability使用minimal job kernel或被禁用；
- 旧EventLog writer/reducer、segment、Presentation Foundation、terminal durable monitor、subagent recovery、Oxigraph和旧job authority对production不可达；
- 经operator授权后按runbook complete reset、从empty universe重跑migration；new runtime role只获`pulsara_v3`权限，public legacy tables保持空且不可达，只启动new universe。

不得临时加入translator、dual reader、compat reducer或旧job bridge。尚未迁移capability必须禁用。

## 13. Named runtime limits

除已冻结的policy timeout、5秒Host close hard deadline、24小时blob orphan grace、64 KiB inline threshold和1 MiB content chunk hard cap外，其余数值在S2-A到S2-E只要求有限且可配置，S2-F根据fixture/负载probe冻结activation default与server hard cap。

closed配置对象必须具名提供：

- committed observation default/hard events、bytes、time；
- committed payload hard bytes；
- audit page与query concurrency default/hard；
- canonical snapshot queue/task/job/tool-control section item default/hard与page limits；
- live observer、shared ring、snapshot、control observer的event/byte default/hard；
- live/committed level-read debounce与diagnostic sampling；
- tool argument display bytes；
- hook callback timeout与close drain budget；
- content read chunk、hydrate concurrency与timeout；
- memory governance SLA、batch size、job claim lease；
- per-handler maximum attempts、attempt timeout、versioned deterministic backoff与LLM per-attempt input/output token caps；
- memory index lag warning/error。

禁止`None`、负数或“0表示无限”。参数调优不得改变GAP、nonblocking overflow、hook无catch-up、thinking可见性、tool arg显式截断、reference不授权、governance异步、session-lifetime retention或physical safety fence。

## 14. Test与failure contract

### 14.1 三层gate

1. retained-safety gate：PostgreSQL transaction/confirmation、physical dispatch、resource stop/cancel/join、secret、test collection；
2. slice gate：当前Stage 2新契约与architecture guards；
3. full suite observation：完整数字和全部red disposition；construction期间不要求legacy全绿。

最终activation要求active production surface的retained-safety与Stage 2 gates全绿。旧test应在删除旧owner的同一slice替换，不提前批量删除。

### 14.2 最小故障矩阵

| 场景 | 必须结果 |
|---|---|
| commit ACK unknown | exact canonical winner query；无receipt/repair |
| concurrent entry/event append | commit-order无洞；rollback不推进high-water |
| stale Host/job guard | 写前被DB拒绝；Host takeover不影响合法job claim |
| first job claim crash/ACK unknown | commit前无worker capability；commit后exact attempt/guard/event可查询；第三种guard 0 |
| retry/provider request bound | due前claim 0；attempt hard cap不超；每attempt provider call至多1且input/output cap不超；aggregate→FAILED；`JobTerminalAccepted`恰好1 |
| provider kill at任意delta | partial丢失、turn interrupted、无durable stream event |
| tool message/attempt未commit | invoke count 0 |
| attempt无result crash | read-time outcome_unknown；自动retry 0 |
| mixed multi-tool partial result | pairing/ordinal稳定；全部terminal前无follow-up |
| late exact result | 只填空缺result；逐assistant cut保留旧closure；只在未来cut按真实sequence出现late observation |
| external source vs provider cut | acceptance先赢则下一handle的H覆盖ROOT entry；freeze先赢则assistant commit前ROOT source entry/`UserMessageAccepted`为0，source job/task fact仍可提交；不存在H=100、ROOT external entry=101、assistant=102/cut100 |
| committed notification loss | level-read发现；超budget完整GAP |
| committed/live跨平面乱序 | canonical projection胜出；draft恰好retire一次；迟到delta不复活 |
| live/hook overflow或异常 | GAP/detach/diagnostic；owner与commit继续 |
| pending interaction replace/takeover | stale resolution拒绝；new epoch empty |
| blob篡改/跨scope/revoke | NOT_FOUND_OR_FORBIDDEN；无existence oracle |
| blob missing/corrupt | integrity placeholder；row不变；无repair |
| terminal close/crash | close kill/join；takeover不adopt/relaunch |
| subagent close/takeover | nonterminal→interrupted；children保留；job rows 0 |
| child internal tool call | task-scoped canonical block/attempt/result完整；parent transcript伪造0；child session row 0 |
| subagent result进入ROOT | exact result最多一个ROOT source entry；terminal parent不自动注入/新建turn；cross-scope binding row 0 |
| prompt consume/cancel/reject race | 单一`PENDING` CAS winner；顺序稳定；claim/receipt row 0 |
| queued steer target terminal/replaced | frozen target只转REJECTED；不投递到新active turn；FIFO继续 |
| candidate/tool-result insert kill | 二者同可见或同不可见 |
| governance/index failure/notification loss | foreground不等待；desired保持领先并被scanner重发现；query返回exact partial disposition |
| index refresh exhaust + repeated scan | 同target/contract automatic job总数1；scanner不生成无限successor；query为`PARTIAL_UNAVAILABLE(INDEX_REFRESH_EXHAUSTED)`；更高desired或新contract才可自动创建新key |
| Oxigraph absent | memory与two-hop继续；network call 0 |
| reset interruption | 停止activation；从empty universe重试；不import effect |

### 14.3 Known terminal red接管

两个terminal legacy test保留可见，直到S2-B或S2-G删除旧durable monitor owner并增加：

- same-Host handle操作；
- cross-Host lookup拒绝；
- orderly close kill/join；
- takeover不adopt/relaunch；
- live observer失败不安装global latch；
- 已accepted result在reattach后仍可见。

## 15. Validation commands

每slice使用repo root的uv环境：

~~~bash
uv run ruff check .
uv run pytest -q tests/test_durability_subtraction_stage0_architecture.py
uv run pytest -q <current-stage2-targeted-tests>
uv run python tools/generate_terminal_protocol_contract.py --check
(cd clients/terminal && go test ./...)
(cd clients/terminal && go vet ./...)
git diff --check
~~~

S2-F与S2-G候选还必须运行：

~~~bash
uv run pytest -q
uv run pytest -q -m postgres
~~~

本地缺PostgreSQL可标environment，但activation evidence必须包含fresh migration/deep verify及全部postgres gate。不得用多次运行结果并集冒充一次全绿运行。

## 16. Architecture guards

至少静态证明：

- committed/live registry exact为26/23；
- SQL/serializer/fixture的type→subject→guard矩阵一致；
- custom、`ToolOutcomeUnknown`、独立`RawProvider*` target为0；
- live modules不import EventLog、durable serializer、authority materialization或projection jobs；
- hook不能importappender/canonical mutation port；
- worker不能写transcript/subagent，Host不能伪造job claim；
- production只构造一种conversation authority与一种Protocol major；
- clean Python process只import new Kernel Host与Protocol v3 launcher时，不得因package facade或共享helper急切初始化旧151-type universal AgentEvent/EventLog、RawProvider/draft/adoption、v2 Presentation Foundation、Oxigraph或execution replay模块；显式legacy module路径可留待Stage 3物理删除，但不能成为new production import的传递副作用；
- new product repository只访问`pulsara_v3` fully-qualified relation；legacy runtime role无`USAGE`；
- activation后旧EventLog、segment、Presentation Foundation、Oxigraph、old jobs、terminal durable monitor和subagent recovery不可达；
- `agent_events`无free-form subject、cursor、receipt、retention lower bound或repair列；
- schema无durable pending interaction、subagent attempt/claim、terminal owner或generic extension action；
- prompt queue只有四种status、closed target-turn union且无claim/lease/checkpoint；turn scope只有ROOT/task两种且task scope必须FK、每scope RUNNING唯一；
- ROOT接受job/subagent result只能使用exact source FK + session-local unique；cross-scope binding relation/JSON列表为0；
- existing RUNNING ROOT的job/subagent result acceptance与prepared-input freeze不经同一短safe-point admission linearization，active handle/model call期间仍能插入external source，或为等待safe point新增durable pending/receipt owner；
- 26种committed type到三个observation branch exact且exhaustive；live settlement/control frame不进入23 registry；
- child coordination events固定`EventOnly`，同entry只产生一个immutable projection；
- policy/subagent/job status enum在SQL/event/generated fixture exact一致；
- first job claim只能经sealed bootstrap；production handler type→safety class exact，无caller override；job retry/provider limits全部finite且provider admission先于调用；
- canonical memory mutation与desired generation同transaction，query disposition不能省略stale/unavailable；
- index scanner可在同一`workspace/channel/target_generation/handler_contract` automatic job exhausted后继续创建job，stable intent key没有数据库唯一性，或Stage 2出现same-key operator-retry API/repair owner；
- user/extension/S3 projections类型分离；
- v3 wire不暴露StoredCommittedEvent、raw blob id/private URL或v2 root；
- content DB transaction不跨storage I/O；
- memory composition无Oxigraph/SPARQL target；
- reset guard拒绝import/converter/cold reader/reverse projection。

## 17. 停止与升级条件

出现以下情况停止当前slice并报告：

- 需要dual-write、compat reducer、translator或old/new merge；
- 需要增加26/23/13/2；
- canonical constraint只能靠event replay或free-form JSON证明；
- tool必须在完整message或attempt commit前dispatch；
- ordinary hook需要跨重启必达或pre-commit veto；
- background capability既不能迁移也不能在activation禁用；
- subagent/terminal被要求跨Host继续；
- pending interaction需要持久化secret/request；
- blob读取需要download receipt/lease/repair；
- memory要求delete/forget或扩大图查询；
- physical owner无法在resource release前stop/cancel/join；
- reset目标不明确或需要操作真实用户数据；
- dirty worktree与当前slice重叠且无法确认归属。

pytest新增失败本身不是停止条件。先分类；legacy failure留给拥有authority cut的slice，新/retained safety failure在相应slice解决。不得为减少红项修复即将删除的recovery graph。

## 18. Activation gate

S2-G要求：

- fresh schema、privilege、catalog deep verify通过；
- `pulsara_v3`与empty public legacy壳隔离，new role/repositories不能解析legacy表；
- direct text/tool/multi-tool/open/rehydrate不写旧authority；
- Protocol v3 Python/Go及content hydrate通过；
- exact 26/23/13/2通过；
- Host writer/job claim fencing通过；
- prompt queue stable order、frozen target、四态CAS与crash-pending通过；
- existing ROOT external-source acceptance与prepared-input freeze的safe-point race通过，active handle/model期间ROOT source entry/`UserMessageAccepted`写入为0，source domain fact不被阻塞；
- message-before-dispatch、attempt-before-invoke、unknown不自动retry通过；
- thinking/tool arguments与extension/S3 capability matrix通过；
- snapshot/observation/live-control的MVCC/GAP/reconnect通过；
- live/committed identity handoff、跨平面乱序与26-type projection mapping通过；
- foreground-reachable jobs迁移或禁用，finite retry、one-call-per-attempt、per-attempt request cap与terminal exhaustion通过；
- terminal/subagent Host lifetime替代测试通过；
- PostgreSQL memory、FTS、pgvector、direct-edge、bounded two-hop等价且Oxigraph连接0；
- memory desired/applied lost-wake追平与closed partial disposition通过；
- index refresh automatic intent按target/handler contract唯一，exhausted同key不会被scanner重建；
- quiesce/reset manifest精确；
- full suite没有unclassified failure；
- named limits有finite default、hard cap和monitor；
- code review确认无compat authority、generic receipt/repair或新durable extension owner。

## 19. Definition of Done

Stage 2完成意味着：

1. 普通Host、TUI、Inspector、context和foreground-reachable job只读写new kernel；
2. canonical row拥有semantic truth，event只拥有accepted occurrence；
3. canonical row与required committed event由同一owner同transaction提交；
4. reopen不通过event replay恢复execution；
5. assistant message、ordered blocks、tool attempt/result和context attribution由数据库约束；
6. 26 Committed、23 Live、13 subject slots、2 guards exact成立；
7. live observer/hook failure不阻塞provider、run或commit；
8. Protocol v3从canonical snapshot引导，以committed observation和live stream增量更新；
9. blob-backed transcript可以exact、bounded、重新鉴权地渲染；
10. prompt queue以stable sequence与四态CAS恢复pending ingress，不恢复claim；
11. job/subagent result只经Host接受的唯一ROOT source entry进入parent context，task scope不被compiler隐式穿透；进入existing RUNNING ROOT必须与prepared-input freeze在provider safe point线性化，active handle/model期间不产生ROOT source entry/`UserMessageAccepted`，但source job/task fact可独立接受；
12. minimal jobs只承载真正跨Host必达的first-party work，首次claim、每个handler safety class、finite retry与每attempt provider request bound闭合；
13. memory proposal先入candidate，governance异步，index lost wake可重发现且query不隐瞒partial；同target/handler contract的automatic refresh exhausted后scanner不能绕过finite retry创建无限job链；
14. yielded terminal和subagent execution随Host结束，不跨Host恢复；child history以同session task scope保留；
15. old EventLog execution graph、durable segment、Presentation Foundation、Oxigraph、old jobs、terminal/subagent recovery在production不可达；
16. complete reset是唯一activation/rollback数据策略；
17. Stage 3可以在不改变上述产品语义的前提下物理删除旧execution recovery与derived authority。

达到DoD后，后续主线是删除旧代码与旧schema，而不是再次设计conversation authority。
