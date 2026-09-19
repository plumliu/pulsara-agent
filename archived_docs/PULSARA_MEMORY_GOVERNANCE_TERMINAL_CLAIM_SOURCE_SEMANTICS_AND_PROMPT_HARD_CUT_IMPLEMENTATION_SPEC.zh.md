# Pulsara 记忆治理 Terminal Claim、来源语义与提示词 Hard-cut 实施规范

> Epoch-boundary hard-cut 覆盖（2026-09-19）：provider-input epoch 与 canonical 重投影的当前唯一权威是 [`PULSARA_PROVIDER_INPUT_EPOCH_BOUNDARIES_AND_CANONICAL_REPROJECTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_PROVIDER_INPUT_EPOCH_BOUNDARIES_AND_CANONICAL_REPROJECTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。canonical transcript row 不再保存 provider、wire API 或 replay disposition；provider replay 是独立可选附件关系。本文中冲突的旧 provider-column 描述均被下文新真值替换，不保留兼容列、双读或 fallback。其余不冲突语义继续有效。

状态：实施前权威规范

适用仓库：pulsara_agent

前置/后续关系：本规范必须先于
`PULSARA_MEMORY_MANAGEMENT_PAGE_AND_CASCADE_DELETION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`
完成实施和验证。后续记忆管理页面与级联删除只能从本规范完成后的 clean-v0
基线继续，不能重新定义 candidate、governance、relation 或来源解释语义。

本规范的权威顺序：

1. 当前用户要求；
2. 仓库根目录 `AGENTS.md`；
3. 本规范；
4. 本规范引用的当前生产代码；
5. `archived_docs/ROUND_8_ADVISORY_MEMORY_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md`
   仅作为已激活 Round 8 产品背景，不能覆盖以上契约；
6. 其他历史文档只作背景。

本规范实施为一次完整 hard cut。不得保留旧 governance packet、旧 claim 时机、旧角色压平、
旧 prompt contract、v1/v2 双读写、compatibility alias 或 feature flag。

---

## 0. 执行结论

Pulsara memory 继续是：

> **结构严格、来源可解释、召回可退化，但完整性、新鲜度与最终处理均不受保证的 advisory dataset。**

本规范不把 memory 升级为严格真源，也不增加治理完成保证。它只修复三类会直接破坏
advisory dataset 产品质量的问题：

1. `MAIN_AGENT_REMEMBER` candidate 只有在其 exact origin turn 已进入 terminal
   状态后才允许从 `PENDING` claim 为 `PROCESSING`；
2. governance 不再只看“调用时恰好已经写入的同轮前缀”，而是看到主模型提出 candidate
   时真正使用的 exact canonical causal cut，加上 candidate 产生后、严格位于 origin turn exact
   terminal occurrence fence 之前的同轮后缀；
3. governance prompt 使用完整业务 taxonomy、来源角色、single-atom、anti-echo、scope、
   basis、replacement、contradiction、taxonomy-correction 与公开摘要契约，而不是枚举名、
   一句笼统 instruction 和全部写成 `FACT` 的伪示例。

目标不是复制主模型的全部 provider wire，也不是让辅助模型复演主模型推理。目标是形成一个
`source-semantically sufficient` 的治理包：

~~~text
主模型产生候选时真正看过的 canonical semantic cut
    + candidate 自身的 exact frozen payload
    + candidate 之后、origin turn terminal occurrence fence 前的 committed suffix
    + exact cited ToolResult / based_on / model-visible-memory provenance
    + same-scope relation target allowlist
    + 完整且稳定的治理业务合同
~~~

治理模型可以比产生 candidate 时的主模型多看一段内容：candidate 之后的用户 steer、撤回、
更正与最终回复。这是本规范有意要求的来源收敛，不是 prefix continuity 例外。

本规范不增加：

- governance job、event、outbox、receipt、checkpoint、lease、generation 或 retry registry；
- durable prompt snapshot、source snapshot、turn-complete marker 或第二套 transcript；
- provider token-count API、厂商 tokenizer、provider 名称倍率或分支；
- confidence、verification、source-authority 自报字段或第二次 semantic judge；
- memory relation、fact lifecycle、candidate 状态或 model-call purpose 的新类别。

数据库只允许增加一个有真实语义必要性的 closed skip reason：
`INSUFFICIENT_SOURCE_SUPPORT`。除此之外，本规范的数据库表、列、FK、index、trigger、grant、
event 与 job delta 必须为零。

---

## 1. 产品语义

### 1.1 Governance 的职责

governance 只回答：

1. frozen candidate 是否是未来可能有复用价值的一条 advisory memory；
2. 它属于哪个与 frozen shape 兼容的 kind；
3. 它是否只是 recalled memory 的无新证据回声；
4. frozen basis refs 是否真的是该 DECISION 的依据；
5. 它与 allowlist 中某条 ACTIVE memory 是否存在明确的 supersede 或 contradict 关系；
6. 应向产品公开怎样的简短形成摘要。

governance 不回答：

- 外部世界的最终真相；
- memory 是否完整、新鲜或必然会被召回；
- 当前操作是否获授权；
- ToolResult 是否应取代业务系统；
- 用户是否永久同意某种行为；
- candidate 是否最终必然完成处理；
- 主模型内部为什么产生某段推理。

### 1.2 来源接近不等于 wire 相同

主模型输入可能包含 BASE_SYSTEM、provider tools、permission、Skills、MCP、runtime context、
provider-native replay 和其他执行材料。governance 不取得这些执行能力，也不复制这些内容。

governance 需要接近的是“判断来源”：

- 用户实际说过什么；
- assistant 在 candidate 周围公开说过什么；
- 主模型当时看到了哪些 canonical conversation/context items；
- candidate 之后用户是否更正、撤回或限定了含义；
- 哪些 ToolResult 是明确引用的 PRIMARY_OBSERVATION；
- 主模型当次看过哪些 memory；
- 哪些现有 memory 是 relation 的 exact 可选目标。

主模型的 hidden reasoning、provider-native replay carrier、SYSTEM、tools 与执行权限不是记忆来源，
不得进入治理包。

### 1.3 终端时点不是完成保证

origin turn terminal status 加上与之匹配的 exact committed terminal occurrence，只是 candidate 可以
开始治理的最早语义边界，不是治理最终完成承诺。

- wake 可以丢失；
- Host close/crash 可以让 `PENDING` 永久未 claim；
- provider call 中 crash 可以让 `PROCESSING` 永久停留；
- provider failure 可以 best-effort `ABANDONED`；
- accepted memory 仍可能遗漏、过期或以后被用户删除。

不得为了“等 terminal”新增 durable wake、retry、repair 或 recovery machinery。

### 1.4 治理阶段与持久化边界

产品时序固定为：

| 阶段 | origin turn | canonical memory 状态 | 产品含义 |
| --- | --- | --- | --- |
| candidate intake | RUNNING 或 terminal 前 | 写入 frozen `PENDING` candidate | 只表示“待考虑”，尚未治理、召回或展示为 memory |
| turn terminal commit | `COMPLETED | INTERRUPTED` 且 exact terminal occurrence 同事务提交 | candidate 仍 PENDING | terminal occurrence sequence 冻结本 candidate 可见的同轮 source fence；可 best-effort wake |
| claim | 已 terminal 且 terminal occurrence 唯一匹配 | exact row `PENDING -> PROCESSING` | 某次局部 attempt 取得处理头和 process-local fence，不代表会成功 |
| source freeze + provider call | 已 terminal | PROCESSING | process-local 读取 exact cut/suffix 并判断；prompt、source envelope、raw model output 不持久化 |
| settlement | 已 terminal | `ACCEPTED | APPLIED_TO_EXISTING | SKIPPED | ABANDONED` | 由 Host 在现有 canonical candidate/fact/relation rows 中原子结算 |
| later recall/management | 与 origin turn 解耦 | 只读 canonical fact/relation/current lifecycle | advisory 使用；不重放 governance prompt |

所以“governance 模型不做持久化”的精确定义是：模型调用及其 prompt/evidence/attempt owner 都是
process-local；持久化的是本来就存在的 candidate terminal decision、accepted fact、relation 和
`decision_public_summary` 产品结果。不能把“不持久化 prompt”误解为“不持久化治理结果”，也不能
为了 UI 反向增加 raw governance log。测试/dogfood 可以在仓库外或 disposable test evidence 中显式
记录一次调用的实际 prompt/response 用于诊断；它不进入生产 canonical schema 或 runtime replay。

---

## 2. 当前生产真源与已确认问题

实施前必须重新完整阅读下列当前代码；符号移动时追踪等价真源：

- `AGENTS.md`
- `src/pulsara_agent/ports/system_prompt.py`
- `src/pulsara_agent/capability/builtin_catalog.py`
- `src/pulsara_agent/model_input/contracts.py`
- `src/pulsara_agent/conversation_kernel/reader.py`
- `src/pulsara_agent/conversation_kernel/auxiliary_model.py`
- `src/pulsara_agent/conversation_kernel/memory/contracts.py`
- `src/pulsara_agent/conversation_kernel/memory/governor.py`
- `src/pulsara_agent/conversation_kernel/memory/reflection.py`
- `src/pulsara_agent/conversation_kernel/memory/dispatch.py`
- `src/pulsara_agent/conversation_kernel/memory_tools.py`
- `src/pulsara_agent/conversation_kernel/tool_execution.py`
- `src/pulsara_agent/conversation_kernel/runner.py`
- `src/pulsara_agent/conversation_kernel/host.py`
- `src/pulsara_agent/conversation_kernel/_repository/memory.py`
- `src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql`
- `tests/test_round8_advisory_memory.py`
- 与 canonical reader、turn terminal settlement、provider input final-wire materialization 直接相关的测试。

### 2.1 Claim 当前过早

当前 `claim_memory_candidate_for_governance` 只按 domain、origin workspace、origin session、
`status='PENDING'` 选择 candidate，没有 join producer entry 所属 turn 的 status。

当前 `remember` ToolResult 与 candidate 同事务接受后，`tool_execution.py` 立即调用
`offer_candidate_wake`。governor 可以在 origin turn 仍为 `RUNNING` 时 claim 并读取 evidence。

因此同一个 candidate 的治理输入会依赖 scheduler：

- provider/tool loop 尚未继续时，可能只看到 candidate 之前的内容；
- 后续 USER_STEER、用户更正、工具观察或 final assistant 尚未 commit；
- 同一语义因 wake 时机不同得到不同 evidence；
- candidate 一旦变为 `PROCESSING`，后续 terminal 内容不会触发同一次重新治理。

这是产品语义错误，不是 weak completion 的合理退化。

### 2.2 Terminal status 当前不是 transcript source fence

`interrupt_turn` 在同一事务把 turn 改为 `INTERRUPTED` 并追加 `TurnInterrupted` occurrence；但当前
`accept_tool_result` 锁 turn 时不要求 `status='RUNNING'`。已在物理执行的工具可以在 interruption
之后继续提交同 turn 的 `TOOL_RESULT + ToolResultAccepted`。

因此只按 `t.status` 后读取“该 turn 当前所有 entries”仍有 scheduler dependence：governor 在 late
ToolResult 前 claim 与之后 claim 会看到不同 suffix。已有 `agent_events.event_sequence` 已为 entry
acceptance 和 `TurnCompleted | TurnInterrupted` 提供同一 session 内全序；本规范直接复用 exact
terminal occurrence sequence 作为 fence，不新增 terminal marker/event/column。

### 2.3 当前 producer turn projection 不等同主模型来源

当前 `_read_memory_governance_turn_projection`：

- 只扫描 source entry 的同一 turn；
- 最多读取前 32 个 transcript entries；
- body aggregate 为 24 KiB，单 entry 最多 8 KiB；
- assistant 只读取 TEXT/DATA blocks，并直接拼接；
- 所有非 assistant/user entry 压成 `TOOL`；
- `PLAN_CONTINUATION` 被错误标为 `USER`；
- 所有 HTTP(S) URL 被 blanket 替换为 `[url redacted]`；
- 完全不读取产生 assistant entry 时已冻结的 exact provider-input causal cut；
- governance total 超界时会把整个 `producer_turn` body 清空。

这产生四个真实语义问题：

1. “前 32 条”可能保留早期工具噪声，却遗漏 turn 末尾的用户更正；
2. `PLAN_CONTINUATION` 冒充 human assertion，可以错误解除 anti-echo 或支持用户偏好；
3. assistant、terminal observation、inter-agent message 与普通 ToolResult 的来源类别丢失；
4. 用户跨 turn 指代“刚才讨论的方案”时，governance 看不到主模型真正使用的历史上下文。

### 2.4 数据库已经拥有 exact causal cut

每个 `ASSISTANT_MESSAGE | ASSISTANT_TOOL_REQUEST` transcript entry 已经保存：

- `context_binding_revision_id`；
- `provider_input_through_sequence`。

这两个 provider-neutral 字段和既有 retained binding-revision/context-snapshot rows 已经足以重建“该 assistant
输出产生前，主模型实际使用的 canonical semantic input cut”。但当前
`CanonicalProviderInputReader.read_frozen_snapshot()` 经 `read_frozen_dispatch()` 仍要求
`turns.current_context_binding_revision_id == cut.context_binding_revision_id`；candidate 之后若同轮已
adopt compaction successor，合法 producer cut 会被现有 current-head guard 判 stale。

Provider/wire-specific replay 只存在于可选的 `provider_assistant_replay_fragments` 附件关系；它不属于
canonical transcript row，也不参与上述 semantic-cut authority。缺失或与目标不兼容的附件只影响
provider replay 可用性，不能改变、补写或重解释 canonical truth。

因此实现不能原样调用该 current-dispatch API。必须从同一 canonical reader 抽取/复用一个窄的
historical semantic-cut read seam：其 authority 是 exact producer assistant entry 已持久化的 cut，
只读取该 revision 当时的 canonical semantic items，并继续复用相同 blob hydration、tool
closure/late-outcome materialization、scope fence 与 bounds。现有 foreground/current-dispatch guard
保持不变；不得用一个宽松 flag 让普通 provider dispatch 读取 stale revision。

本规范禁止为 governance 再增加：

- input-cut fingerprint；
- prompt snapshot；
- source entry array；
- transcript citation table；
- provider replay copy；
- governance evidence registry。

### 2.5 当前 prompt 低于已冻结业务合同

当前 packet 只提供：

- 一句简短 instruction；
- 五个 taxonomy enum 名称；
- candidate 与 bounded evidence；
- output union 的 accept/supersede/contradict 示例，而且所有 `final_kind` 都写成 `FACT`。

它没有向模型完整解释：

- 五类 kind 各自回答的产品问题；
- USER_PROFILE 与 RESPONSE_PREFERENCE、FACT 与 DECISION、FACT 与 ACTION_RULE 的差异；
- source role 与证据用途；
- candidate/assistant text 本身不是 user assertion；
- PLAN_CONTINUATION 不是 human；
- single semantic atom；
- based_on 的真实依赖语义；
- explicit replacement intent；
- contradiction 的不可同时成立条件；
- taxonomy correction 的同一 atom 限制；
- truncation/coverage 下降时不能取得 relation-writing authority；
- `public_summary` 的产品文案合同。

Repository 可以验证 closed branch、kind/shape、scope、target allowlist 和 relation matrix，却不能
替代以上自然语言语义判断。

### 2.6 Auxiliary call 当前只有 USER JSON

`DirectKernelAuxiliaryJsonModel.prepare_json_call` 当前把整个 packet 作为一条 USER message
发送给 `ModelRole.FLASH`，没有 governance SYSTEM message、tools 或 continuity。

动态 transcript/candidate 本身是 untrusted quoted data。稳定规则与不可信正文处于同一 USER
message 层级，会削弱 instruction/data 边界。本规范将治理业务合同提升为独立稳定 SYSTEM
message，动态 evidence 保持为单独 USER JSON message。

---

## 3. 不可妥协的 Hard-cut 边界

1. candidate 在 origin turn 为 `RUNNING` 时不得 claim、不得改为 `PROCESSING`、不得打开
   governance provider。
2. eligibility terminal 定义为 canonical turn status `COMPLETED | INTERRUPTED`，并且必须存在唯一、
   type/status/payload exact 匹配的既有 `TurnCompleted | TurnInterrupted` committed occurrence；该
   occurrence 的 `event_sequence` 是 source closure fence。terminal outcome 只作为 context，不自动
   证明 candidate 正确。
3. claim 必须 exact join candidate、source entry、origin turn、origin session、origin workspace、
   memory domain、frozen scope 与 terminal occurrence fence；provider open 前再次重验。
4. 不新增 durable wake、governance job、lease、retry、receipt、checkpoint、source snapshot、
   prompt row 或 event。
5. 不复制主模型 SYSTEM、tools、permission、environment、hidden reasoning 或 provider-native
   replay 给 governance。
6. 不按 provider 名称维护 source projection、倍率或特殊 prompt。
7. 主模型 causal source 使用 canonical semantic reader；不从 provider wire 反向 parse。
8. candidate statement、scope、conditions、exclusions、basis refs、citations 均 immutable；
   governance 不能 rewrite、split、merge、补字段或只接受一部分。
9. relation target 只能来自 exact allowlist；relatedness 只负责发现，不提供 relation authority。
10. PLAN_CONTINUATION、SUBAGENT_OBJECTIVE、INTER_AGENT_MESSAGE、assistant text、context snapshot
    都不得冒充 human assertion。
11. 只有显式引用且由 Host 分类为 `PRIMARY_OBSERVATION` 的 ToolResult 才能作为外部观察依据；
    generic turn ToolResult 只是 context。
12. `MEMORY_READ_EXPOSURE` 及其 artifact descendant 永远不能作为新证据。
13. 不把处理失败、来源不足或 prompt overflow 变成 conversation/turn failure。
14. 不新增 total turn、task、session、provider-stream 或 memory inventory lifetime cap。
15. governance attempt 保留现有 finite local watchdog；provider transport 使用 connect/write/read-idle
    与 remaining attempt 的物理边界。
16. 不调用 provider token-count API；final-wire bytes exact，本地统一 estimator 只估算 tokens。
17. governance prompt v1 直接删除；只存在 v2 SYSTEM + evidence packet 单一路径。
18. stable BASE_SYSTEM 与 remember descriptor 的改变只进入新 cold epoch；existing epoch 的
    SYSTEM/tools byte-identical，messages 只追加 suffix。
19. 不 blanket redaction 用户、assistant、ToolResult 或 URL 正文。仅 `PULSARA_API_KEY` 的值
    永远不能进入 prompt、日志、证据或报告。same-trust-domain 内容保持 canonical public text；
    cross-provider egress 继续服从显式配置。
20. 不用 confidence、source score、truth score 或第二模型调用伪装语义确定性。

---

## 4. Origin-turn-terminal Claim

### 4.1 Eligible candidate

`claim_memory_candidate_for_governance` 的 SQL eligibility 必须 join：

~~~text
memory_candidates c
  -> exact producer source entry e
       MAIN_AGENT_REMEMBER: c.producer_entry_id
       CHEAP_HINT_REFLECTION: c.trigger_user_entry_id
  -> exact origin turn t by (origin_session_id, e.turn_id)
  -> exact terminal occurrence o by (origin_session_id, t.id, status-matched event_type)
  -> exact session s by (origin_session_id, origin_workspace_id, memory_domain_id)
~~~

候选条件：

- `c.status = PENDING`；
- current Host guard exact owns `c.origin_session_id`；
- `s.workspace_id = c.origin_workspace_id`；
- `s.memory_domain_id = c.memory_domain_id`；
- source entry branch/kind 与 candidate producer_kind exact；
- `t.status IN ('COMPLETED', 'INTERRUPTED')`；
- `COMPLETED` exact 对应一个 `TurnCompleted` occurrence，`INTERRUPTED` exact 对应一个
  `TurnInterrupted` occurrence；其 subject、payload/terminal reason 与 canonical turn winner 一致，
  且匹配 occurrence 数量恰好为 1；
- source entry exact acceptance occurrence 唯一，且其 event sequence 严格小于 terminal occurrence；
- candidate frozen scope 仍是该 origin workspace 可写的 USER 或 exact WORKSPACE scope。

排序仍按 `accepted_at, id` 稳定。`RUNNING` candidate 必须在 SQL eligibility 中排除，不能因为
它更早而阻塞同一 Host 中已经 terminal 的后续 candidate。

claim transaction 只对真正 eligible row 执行：

~~~text
PENDING -> PROCESSING
processing_started_at = now
~~~

claim 返回的 process-local typed head 同时冻结 `terminal_event_id + terminal_event_sequence +
terminal_status/outcome`。它们来自既有 `agent_events` row，不写回 candidate、不新增 event 或 fence
column。若 terminal occurrence 缺失、重复或与 status/payload 不一致，该 row 不 eligible，且不能
阻塞同 Host 中后续健康 candidate。

观察到 RUNNING source turn 时返回 none/继续寻找其他 eligible candidate；不能先 claim 后等待。

### 4.2 Wake 时点

`remember` ToolResult settlement 后不再立即 wake governance。

新的 process-local wake 规则：

- candidate intake 只提交 `PENDING`，不打开 governance lane；
- canonical turn status 与匹配 terminal occurrence 的共同事务 commit 成功后，仍在同一 Host 进程的 terminalization path
  best-effort offer 一次无 payload wake；
- Cheap Hint Reflection 本来只在成功 terminal turn 后插入 candidate，插入完成后可直接 wake；
- Host open 保留一次 bounded pending scan wake；
- turn interruption/takeover/close 路径若仍有活着的原 Host owner，在 status + terminal occurrence
  共同 commit 后同样
  best-effort wake；若进程已结束，允许丢失。

wake 不是 candidate authority，不携带 candidate ID registry，也不证明 turn terminal。claim SQL
每次重新读取 canonical status。

### 4.3 Terminal 后再次重验

`read_memory_governance_evidence` 与 provider open 前必须再次 exact join：

- candidate 仍为该 Host 的 `PROCESSING` row；
- source entry identity/kind 未漂移；
- MAIN_AGENT_REMEMBER 的 `producer_tool_call_id` 仍 exact 属于该 assistant entry，candidate acceptance
  digest、proposal 与 refs 仍与已接受的 frozen call result 一致；CHEAP_HINT_REFLECTION 的 trigger
  entry、candidate ordinal 与 frozen handoff 一致；
- source turn 仍为 terminal；
- exact terminal occurrence 仍唯一且与 claim 时冻结的 event id/sequence/status/outcome 完全相等；
- assistant source entry 的 stored cut 与 binding revision 完整；
- workspace/domain/scope 仍一致。

任一失败都 fail closed，不改绑 source，不回扫另一个 turn，不猜 current Host context。

---

## 5. Frozen Governance Source Envelope

本规范新增的 source envelope 是 exact call process-local typed value，不持久化。

建议职责形状：

~~~text
FrozenMemoryGovernanceSourceEnvelope
  origin_turn
    turn_id
    terminal_status
    terminal_occurrence_fence       # internal exact event id/sequence；provider 只看 product outcome
    producer_kind
    human_source_complete
    post_proposal_human_source_complete
  candidate                       # exact frozen proposal
  producer_call_context           # main model causal semantic cut 的 bounded projection
  producer_public_output          # candidate 所在 assistant entry 的 TEXT/DATA blocks
  post_proposal_turn_suffix       # producer entry 后、terminal occurrence fence 前的 committed suffix
  cited_tool_evidence             # exact refs + evidence_kind
  based_on_items                  # exact prebound facts
  model_visible_memory            # COMPLETE 时全部 exposure IDs 的 scope-safe refetch
  exact_existing_source           # optional exact semantic winner
  relation_targets                # bounded same-scope allowlist
  source_coverage
~~~

完整 typed object 直接交给 packet materializer；不增加 envelope fingerprint 或 registry。

### 5.1 MAIN_AGENT_REMEMBER causal cut

从 `producer_entry_id` 对应 assistant entry 读取：

- `turn_id`；
- `entry_sequence`；
- `context_binding_revision_id`；
- `provider_input_through_sequence`。

使用现有 canonical reader 按这组 exact identity 重建主模型产生该 assistant output 前真正看过的
`CanonicalModelInputSnapshot`。禁止使用：

- governance 执行时的 current context binding；
- session latest sequence；
- fresh compiler output；
- raw full transcript；
- provider replay parse；
- 当前 memory query 重新猜主模型看过什么。

这里的“使用现有 canonical reader”指共享它的唯一 canonical semantic read/lowering core，不是绕过
上节已确认的 current-head guard。historical governance seam 必须先 exact join：

- producer entry 的 session/turn/scope；
- entry 自身 frozen `context_binding_revision_id` 与 `provider_input_through_sequence`；
- revision 的 `(session_id, turn_id)`、base kind、snapshot identity 与 source floor；
- cut 不超过 producer entry sequence，且不超过 exact canonical head available to that call。

它不读取 permission snapshot、BASE_SYSTEM、tools 或 current runtime facts，也不能接受 caller 自报的
任意 old revision。若历史 revision/snapshot/body 已不存在或 exact join 失败，source coverage 不是
“换用 latest”，而是 fail closed 为 source insufficient/attempt failure。

从 canonical snapshot 只选择治理相关的 semantic items：

- HUMAN_MESSAGE / HUMAN_STEER；
- ASSISTANT public TEXT/DATA；
- provider-visible ToolResult/closure/late outcome，标为 context-only；
- PLAN_CONTINUATION，保留独立 kind；
- INTER_AGENT_MESSAGE / SUBAGENT_OBJECTIVE，保留独立 non-human kind；
- CONTEXT_SNAPSHOT，标为 compacted context、非 human evidence；
- terminal observation，保留独立 observation context。

不复制 BASE_SYSTEM、tools、permission、runtime environment、Skills/MCP catalog、hidden reasoning、
native replay body。`MEMORY_RECALL` 与 response-preference exposure 由 candidate 已冻结的
`model_visible_memory` 单独投影，不从 causal context 重复猜测。

### 5.2 Producer public output

candidate 所在 assistant entry 是模型输出，不属于该模型调用的 input cut，但 governance 需要看见
其公开表达，以理解 candidate 如何形成。

投影规则：

- TEXT 与 DATA blocks 按 block ordinal 保持边界，不直接无分隔拼接；
- TOOL_CALL block 不作为自然语言正文；
- exact remember call payload 已由 frozen candidate 单独提供；
- 其他 tool arguments 不注入；
- hidden thinking/reasoning 不读取、不推断；
- assistant output 默认是 `ASSISTANT_CONTEXT`，不是 human assertion 或外部观察。

### 5.3 Post-proposal terminal suffix

在 source assistant entry 的 accepted occurrence 之后，读取同一 origin turn 中 acceptance occurrence
满足以下条件的全部 committed entry metadata，并按 `entry_sequence` 投影 materialized subset：

~~~text
source_entry_acceptance_event_sequence
    < entry_acceptance_event_sequence
    < terminal_event_sequence
~~~

每个 transcript entry 必须 exact join 唯一、kind/subject 匹配的既有 committed acceptance event。
terminal event 使用 `agent_events(session_id, event_sequence)` 的 session 内全序，与 entry acceptance
events 共用同一 allocator；不能用 `terminal_at`、`accepted_at`、claim time 或当时的
`sessions.latest_entry_sequence` 近似该 fence。

这里尤其必须保留：

- USER_STEER；
- 后续 USER_MESSAGE（若 canonical turn 契约允许）；
- 用户撤回、限定、纠正或“只是这一次”的表达；
- later cited/visible ToolResult；
- final assistant public TEXT/DATA；
- terminal outcome。

不能只取 candidate 之前的 prefix，也不能继续使用“前 32 个 entry”选择。entry metadata 通过
keyset/pagination 读取，总量不设固定 item cap；provider materialization 仍受 §9 的物理 byte/token
边界。

当前生产代码允许已在执行的 ToolResult 在 `INTERRUPTED` commit 后才进入 transcript；其
`ToolResultAccepted.event_sequence > terminal_event_sequence`。这类 late entry 无论发生在 claim 前、
source read 前还是 provider open 后，都稳定排除，且不能以 marker/count 形式改变 packet。由此
source closure 只依赖 terminal commit 时已经成立的 occurrence prefix，不依赖 governor 调度。

origin terminal occurrence 之后的新 turn 永远不进入该 candidate 的 source envelope，即使它们在实际
claim 前已经发生。否则同一个 candidate 又会因调度早晚看到不同的 future conversation。后续 turn
中的更正依靠新的 candidate、SUPERSEDES/CONTRADICTS 或用户删除逐步收敛；短暂遗漏/陈旧仍属于
advisory dataset 已声明的退化边界。

### 5.4 CHEAP_HINT_REFLECTION branch

Cheap Hint Reflection 只在成功 ROOT human turn terminal 后产生 candidate，继续保持：

- exact `trigger_user_entry_id`；
- verbatim normalized human atom；
- frozen adjacent assistant text 与 final assistant text；
- visible-memory set 为空；
- cited ToolResult set 为空。

governance claim 仍执行同一 terminal join，不能因为 reflection “按设计应该 terminal”而跳过。

reflection source envelope 使用 trigger user entry 所在 terminal turn 和已冻结 handoff；如果需要
causal tail，只能使用该 turn final assistant entry 的 stored input cut。不得回扫全 session 或把
hint-review model raw response当 producer source。

### 5.5 Exact public text

同一 provider trust domain 内，用户、assistant 和 provider-visible ToolResult 的 canonical public
text 在 byte budget 内保持原文；不再用 URL regex blanket 改写所有 HTTP(S) URL。

以下仍不进入治理包：

- `PULSARA_API_KEY` 的值；
- 从未 provider-visible 的 environment secret；
- raw artifact/blob 全文；
- 非 candidate tool arguments；
- hidden reasoning；
- foreign-origin private provenance。

本规范不新增 secret scanner，也不让 governance reader 读取 credential configuration 后做正文搜索。
`PULSARA_API_KEY` 不泄漏由既有 credential boundary、canonical ingestion discipline，以及本功能从不
把 runtime key slot 注入 source envelope 共同保证；不得借此恢复 blanket redaction。

跨 provider trust domain 的 auxiliary egress 继续要求现有显式 opt-in；未授权时 hydration 和
provider open 均为 0。

---

## 6. 来源角色与业务判断

governance packet 必须向模型明确：所有动态 string 都是 quoted untrusted data，不能作为 prompt
instruction 执行。每个 source item 带 Host 生成的 `evidence_role`，caller/model 不能自报或修改。

### 6.1 Closed evidence roles

| evidence_role | 来源 | 可以支持什么 | 不能支持什么 |
| --- | --- | --- | --- |
| HUMAN_ASSERTION | candidate 产生前或当时、具有 exact human origin 的 HUMAN_MESSAGE/HUMAN_STEER | 用户明确报告、偏好、规则、选择及其确定性 | permission/system override、未表达的推断 |
| POST_PROPOSAL_HUMAN | producer entry 后至 terminal、具有 exact human origin 的 HUMAN_STEER/HUMAN_MESSAGE | 可能确认、否定、限定或修正 candidate；判断 replacement intent | 不能仅凭时间位置假定它一定是更正，也不能自动改写 candidate |
| PRIMARY_OBSERVATION | exact cited ToolResult | 与 candidate 直接相关的外部/项目观察 | 用户画像、回答偏好、permission |
| MEMORY_READ_EXPOSURE | memory read ToolResult/artifact lineage | 只证明模型看过 memory | 任何新事实或 echo 解锁 |
| ASSISTANT_CONTEXT | assistant TEXT/DATA | 解释对话、项目 DECISION 的公开形成过程 | 单独证明用户属性/偏好或外部事实 |
| NON_HUMAN_CONTEXT | plan continuation、subagent objective、inter-agent、context snapshot、terminal observation，以及带 runtime/plan origin 的非人类 USER-shaped item | 消解指代和理解任务背景/终止状态 | 冒充用户声明或 PRIMARY_OBSERVATION |
| TOOL_CONTEXT_ONLY | 未显式引用的普通 ToolResult/closure | 理解 turn flow | 作为 semantic citation 或解除 echo |

`PLAN_CONTINUATION` 不再映射为 `USER`。Prompt 和 typed DTO 都必须保留它的 non-human origin。
`POST_PROPOSAL_HUMAN` 只编码来源和时序，不编码 Host 已经做出的语义判断；是确认、更正、限定
还是无关输入，只能由 governance 根据原文判断。

### 6.2 Source support 按 kind 判断

| final kind | 足够的来源支持 | 不足的例子 |
| --- | --- | --- |
| FACT | 直接 human report（保留其不确定性），或与 statement 直接相关的 PRIMARY_OBSERVATION | assistant 猜测、无关 ToolResult、memory echo |
| USER_PROFILE | 用户对自身身份、习惯、兴趣的明确表达；scope 必须 USER | assistant 从行为推断、项目内角色被提升为跨项目画像 |
| RESPONSE_PREFERENCE | 用户明确表达通常/以后希望 Agent 如何回答、解释或表达 | 仅本次格式要求、用户爱好、assistant 自行推断、要求奉承/隐瞒风险 |
| ACTION_RULE | 用户明确给出的未来条件与行动；或在 exact workspace 任务明确授权 Agent 建立工作约定时，assistant 明确公布的项目规则；`applies_when` 必须 frozen | 一次性动作、permission、安全策略、从单次行为推断长期规则、assistant 未获授权的自定规则 |
| DECISION | 用户明确选择/同意，或在已授权 Agent 作选择的任务中 assistant 明确公布最终选项；可有真实 basis | 仍在比较的备选项、计划草稿、未形成选择的事实 |

对于 assistant 形成的 project DECISION，governance 必须看到：

- 上下文确实要求/允许 assistant 作出选择；
- producer/final assistant 明确陈述已经选择，而不是建议或候选；
- post-proposal human suffix 没有撤回或反对；
- decision 不取得 permission authority。

assistant 形成的 WORKSPACE ACTION_RULE 使用同样的 explicit-authorization gate，并额外要求公开文本
确实形成了 future condition/action convention。USER ACTION_RULE 不能仅靠 assistant source；用户的
沉默也不能把 assistant 建议升级为规则。

source support 必须覆盖 candidate 的全部 semantic payload，而不只 statement 主句：certainty、
USER/WORKSPACE 持久范围、`applies_when`、每个 `do_not_apply_when` 和全部 basis refs 都必须能在来源
中直接解释。candidate 自行把“本项目/本次”扩大为跨项目、补出用户未说过的例外、删除来源中的
关键条件，或把建议写成已决定，都必须 whole-candidate skip；不能以“主句大致正确”为由接受。

### 6.3 同一 origin turn 内的更正

post-proposal human 原文对“这条 candidate 是否仍忠实表达该 turn”具有最高优先级；其中明确的
更正、撤回或范围限定必须覆盖 candidate 产生时的较早表达。

例：

~~~text
用户：以后回答都详细一些。
assistant 调用 remember：回答通常使用详细解释。
用户 steer：等等，我只是说这一次。
~~~

governance 不能接受原 candidate，也不能改写为“本次回答详细”。结果必须
`SKIP(INSUFFICIENT_SOURCE_SUPPORT)`。

这条优先级只判断当前 candidate 的来源忠实度，不把“最新一句”升级为整个 memory dataset 的
永久真相。未来仍可能有其他 candidate、冲突或过期记忆。

post-proposal `ASSISTANT_CONTEXT` 也必须可见，但其权威更窄：assistant 若明确撤回自己刚形成的
project DECISION，原 assistant-authored DECISION candidate 应 skip；assistant 的沉默、换一种说法或
对用户原话的异议，不能反向覆盖 HUMAN_ASSERTION，也不能证明 USER_PROFILE/
RESPONSE_PREFERENCE。治理必须按 final kind 和 source role 判断，不能采用笼统的“最后一条消息赢”。

### 6.4 Source coverage 下降

Host 必须显式投影：

- causal context 是否 complete 或 bounded tail；
- origin-turn human source 是否完整；
- post-proposal human suffix 是否完整；
- 哪些 item/body 被 truncated/omitted；
- relation targets 是否因 budget 被移除。

模型不能把 omitted 当 absent。

如果缺失使 Host 无法向模型提供至少一个直接支持 candidate 的 human assertion、合法 assistant
decision source 或 cited PRIMARY_OBSERVATION，则在 provider open 前或模型决策中使用
`SKIP(INSUFFICIENT_SOURCE_SUPPORT)`。

`post_proposal_human_source_complete=true` 只有在以下条件全部成立时才能签发：

- terminal occurrence fence 前的 suffix metadata 已穷尽扫描；
- 每个 exact human-origin entry 都已完整 materialize；
- 每条 human body/block 都无 truncation、omission 或 ambiguous acceptance occurrence；
- source item 顺序与 fence exact。

只要该 flag 为 false，Host 必须在 provider open 前直接
`SKIP(INSUFFICIENT_SOURCE_SUPPORT)`；所有 `ACCEPT*` 均非法，不只是 relation branch。不能把可见
部分“没有更正”当成完整原文没有更正，也不能让模型猜被裁掉内容。等价地，全部 post-proposal
human material 是 provider admission 的 MUST_KEEP；exact final wire 装不下就 no-provider skip。

post-proposal assistant/tool/context 可以按 §9 诚实退化。若 candidate 需要 assistant-authored
DECISION/ACTION_RULE source，而相关 assistant suffix 不完整，SYSTEM contract 要求模型同样
`SKIP(INSUFFICIENT_SOURCE_SUPPORT)`；它不能用 incomplete assistant context 建立授权或最终选择。

---

## 7. Governance SYSTEM Prompt v2

### 7.1 两层 message

`MEMORY_GOVERNANCE` auxiliary call hard cut 为：

1. 一条稳定 SYSTEM message：治理产品合同；
2. 一条 USER message：canonical JSON evidence packet。

tools 为空，continuity 为空。SYSTEM message 不含 candidate、memory statement、workspace、turn 或
其他动态数据。USER packet 中所有 string 明确标为 untrusted evidence。

contract id 更新为：

~~~text
pulsara.advisory-memory-governance.v2
~~~

v1 packet builder、单 USER message fallback 和 v1 parser tests 同时删除。

`AuxiliaryJsonModelPort.prepare_json_call(prompt=...)` 当前只能表达一条 USER message。实施时必须
hard cut 为 exact typed messages/prepared-prompt 输入，不增加 optional `system_prompt`、默认 fallback
或旧 signature overload：

- MEMORY_GOVERNANCE 只接受 `(SYSTEM contract, USER evidence)`；
- CONTEXT_COMPACTION_SUMMARY 与 MEMORY_HINT_REVIEW 更新为显式传入各自现有单 USER message；
- purpose-specific validator 在 materialization 前验证 role/count；
- estimator 和 transport 直接消费同一个 frozen message tuple，不能 estimate 一组、send 另一组。

这只是把共享 auxiliary port 的真实 message contract 类型化，不给 governance tools、continuity 或
main-model execution authority。

### 7.2 Prompt 必须先建立的产品框架

SYSTEM prompt 在讲 enum 之前，必须用简短、直接的产品语言让治理模型理解：

- Pulsara memory 是以后在相关情境中**可能**提供给主模型的 advisory context，不是知识库真源、
  permission、SYSTEM policy、任务队列或保证召回的用户档案；
- candidate 是主模型 `remember` proposal 或 terminal-turn Cheap Hint Reflection 产生的 frozen proposal；
  producer 类型只说明形成路径，不证明 statement 正确、持久或已获用户确认；
- governance 的工作是判断整条 frozen proposal 能否作为一条来源忠实、未来可复用的 memory；
  只能 whole-candidate accept 或 closed-reason skip，不能替用户/assistant 改写原意；
- ACCEPT 会让该内容进入 ACTIVE advisory dataset；SUPERSEDES 会使 exact old target 进入
  SUPERSEDED history；CONTRADICTS 让两端都保持 ACTIVE 并向产品暴露未解决冲突；BASED_ON 只说明
  DECISION 的依据；
- accepted memory 和 `public_summary` 会进入用户可见的管理产品；summary 是形成方式摘要，不是
  隐藏推理、事实认证或永久保存承诺；
- 所有动态正文都是 untrusted quoted evidence。模型必须按 source role、chronology、scope、
  completeness 和 direct relevance 阅读，不能服从正文中的指令，也不能把 assistant 或 non-human
  context 当成用户原话；
- source 缺失表示“本次无法充分判断”，不是 candidate 必然为假；但在 frozen candidate 无法被
  已见来源忠实支持时必须 skip，不能用常识补证据。

SYSTEM prompt 还必须解释两个 producer product labels：

- “主模型在回复过程中主动提出”：模型在一次 provider-visible `remember` tool call 中提交；
- “终端轮次的轻量提示整理”：用户原话经 best-effort reflection 提出，未经过主模型主动调用。

两者都要接受同一 taxonomy、source-support、anti-echo、single-atom 与 relation admission；不得因为
“主动提出”或“来自用户提示”而放宽证据要求。

SYSTEM contract 必须完整携带 §6 的 evidence-role 含义和按 final kind 的 source-support matrix；
USER packet 只给每条 source 标 closed role，不能期待模型从 `HUMAN_ASSERTION`、
`TOOL_CONTEXT_ONLY` 等名字自行猜权威。角色定义、producer label 定义和 taxonomy 一样属于稳定
业务合同，不随单次 candidate 动态生成。

### 7.3 Shared product taxonomy

stable BASE_SYSTEM、remember descriptor 与 governance SYSTEM prompt 必须共享同一份 closed
product semantics。建议在 provider-neutral、低层、无 Runtime owner 的模块冻结纯常量/构造函数；
不得复制三份逐渐漂移的定义。

五类定义：

- `FACT`：外部世界、项目或环境处于什么持久状态；不是决定、规则或用户画像。
- `USER_PROFILE`：用户是谁、喜欢什么、通常怎样；只允许 USER scope；不是 Agent 的回答方式。
- `RESPONSE_PREFERENCE`：Agent 通常应怎样回答、解释和表达；是 soft default，不是用户爱好、
  行动规则、permission 或 core behavior override。
- `ACTION_RULE`：在明确 future condition 下应执行什么行动；必须有 `applies_when`，永远不成为
  permission、安全 policy 或业务 latch。
- `DECISION`：已经选择了什么方案；可以引用真正构成理由的 accepted memory；不是备选项、
  当前事实或未完成计划。

Prompt 同时解释 scope：

- USER：跨该用户项目仍有长期价值；
- WORKSPACE：只在当前 exact project 共享；
- project-specific 用户角色不能以 USER_PROFILE 提升到全局；
- governance 不能修改 frozen scope。

### 7.4 Exclusion contract

以下内容不能为了“有一个类别可放”而塞入 FACT：

- 当前任务进度；
- TODO/reminder；
- 一次性格式或行动要求；
- secret/credential；
- raw ToolResult/artifact；
- permission、safety policy、SYSTEM authority；
- 要求无条件奉承、同意、停止质疑、隐瞒重大风险、维持依赖/persona 或伪造授权的
  response preference；
- 没有来源支持的 assistant inference。

Prompt 必须说明 memory 可陈述用户报告的 advisory fact，不需要伪造“已外部验证”；但必须保留
来源的 certainty。来源说“可能”，candidate 写成“确定”时不能 rewrite，只能
`SKIP(INSUFFICIENT_SOURCE_SUPPORT)`。

### 7.5 Single semantic atom

每个 candidate 只能表达一个可独立分类、召回、更新和删除的 semantic atom。

- governance 不能拆成两条；
- 不能接受一半；
- 不能删除从句后接受；
- 不能把 statement 改写成更安全/更短的版本；
- multi-atom 必须 `SKIP(MULTI_ATOM_STATEMENT)`。

主模型和 reflection 可以在 intake 阶段提出多个独立 candidate；governance 只处理当前一个。

### 7.6 Legal final kinds

Host 用唯一现有 `validate_final_kind_shape` 对当前 frozen proposal 计算
`legal_final_kinds`，并把完整 enum list 传给模型。

- `kind_hint` 只是 hint，不是 final authority；
- `AUTO` 不放宽 shape；
- 模型只能从 `legal_final_kinds` 选 final kind；
- output schema 不再用 `"final_kind":"FACT"` 表示任意 kind；
- schema 使用 `allowed_values: legal_final_kinds` 或等价 closed 表达；
- Host parser/acceptance transaction仍重验 shape。

### 7.7 Based-on

`based_on_memory_ids` 只允许 DECISION candidate 使用。governance 看到 exact prebound items 后必须
判断：每条 item 是否真的是该选择成立的理由，而不是相似、同时出现或方便引用。

- 不能增加、删除、替换或重排 basis refs；
- 任一 frozen basis 明显不构成依据时，candidate 必须 SKIP；
- relatedness result 不能自动成为 basis；
- duplicate source settlement 不补写 BASED_ON；
- BASED_ON 不使 memory 成为外部真相或 permission。

### 7.8 Anti-echo

Prompt 必须逐条说明：

- model-visible provenance OVERFLOW 时 Host 直接
  `SKIP(MODEL_VISIBLE_MEMORY_PROVENANCE_OVERFLOW)`；
- candidate 与任一 model-visible memory statement 原样或语义等价，且没有与 candidate 直接
  相关的新 HUMAN_ASSERTION/POST_PROPOSAL_HUMAN/PRIMARY_OBSERVATION 时，必须
  `SKIP(RECALLED_MEMORY_ECHO)`；
- assistant 复述 recalled memory 不是新证据；
- PLAN_CONTINUATION 不是新 human assertion；
- `MEMORY_READ_EXPOSURE` 及 artifact descendant 永远不能解除 echo；
- 一个无关 PRIMARY_OBSERVATION 不能解除 echo；
- exact duplicate 仍需进入 governance，因为 producer turn 可能表达 relation intent。

### 7.9 Relation semantics

#### Plain duplicate

candidate 与 exact ACTIVE source 相同，且没有 explicit relation intent 时：

~~~text
SKIP(DUPLICATE)
~~~

不能因为 duplicate 而吞掉明确的 replacement、contradiction 或 taxonomy correction intent。

#### SUPERSEDES

`ACCEPT_AND_SUPERSEDE` 需要明确 replacement intent，且 source/target 是同一 scope 的同一语义
槽位：

- 用户明确说“改成、以后不要 X、用 Y 替代 X、此前说错了”；或
- PRIMARY_OBSERVATION 直接证明同一项目状态已经由旧状态变为新状态，并且 candidate 明确表达
  当前替换关系。

相似、相关、更新、更近、embedding 分数更高、kind 不同或单纯冲突都不够。

ordinary replacement 使用 `SAME_KIND_REPLACEMENT`。若没有明确 replacement intent，最多
contradict、coexist 或 skip。

#### TAXONOMY_CORRECTION

只在以下条件全部成立时使用：

- candidate 与 target 是同一 semantic atom；
- 旧 target 的 kind 明确错误；
- candidate frozen statement/scope/structured fields 不需要改写；
- source context 明确支持“这是分类纠正，而不是内容更新”；
- target 来自 allowlist。

不能因 cross-kind relatedness 自动使用 taxonomy correction。

#### CONTRADICTS

只在 same kind、same scope、相同适用时间/条件下两个 proposition 无法同时成立时使用。

- 条件不同可以 coexist；
- “通常”与“有时”未必冲突；
- 不同 workspace 不冲突；
- 旧状态与新状态若有明确 replacement intent 应 supersede，不是 contradiction；
- 不清楚时 accept coexist 或 skip，不能伪造 winner。

CONTRADICTS 两端都保持 ACTIVE，由产品显示“与对方存在冲突”。治理模型不选胜者。

#### Relation authority hard admission

只有 Host 提供的 target allowlist 和完整 source coverage 才允许模型返回 relation branch。
allowlist 为空时，任何 target output 都 parse fail。acceptance transaction 继续锁定并 exact 重验
target、kind、scope、lifecycle 与 relation matrix。SUPERSEDES 具有 lifecycle-destructive 后果；
CONTRADICTS 不选 winner，但仍写入用户可见关系，因此两者都服从同一 hard admission。

### 7.10 Decision precedence

SYSTEM prompt 要求模型按以下顺序判断：

1. source coverage 与 candidate fidelity；
2. temporary/secret/raw output/permission/unsafe exclusions；
3. single atom；
4. legal kind、scope 与 frozen structured shape；
5. source 对该 kind 是否足够支持；
6. anti-echo；
7. DECISION basis 是否真实；
8. exact duplicate；
9. explicit supersede/taxonomy-correction/contradiction；
10. closed output 与 public summary。

顺序不意味着 memory 取得事实权威，只避免相似度或 relation 诱导模型跳过更基本 admission。

### 7.11 Closed model SKIP reasons

SYSTEM prompt 不能只列 reason enum；必须给出互斥度尽可能高的产品定义。模型可输出的 closed set
只有：

| reason | 使用条件 | 不得替代 |
| --- | --- | --- |
| `INSUFFICIENT_SOURCE_SUPPORT` | frozen semantic payload 的来源不足、certainty/scope/condition 被放大、或本轮后续原文已撤回/限定 | multi-atom、低价值、单纯 kind hint 错误 |
| `TEMPORARY_OR_EPHEMERAL` | 当前进度、一次性请求、近时提醒、只对本轮/当前动作有意义 | 有明确 future condition 的 durable rule |
| `LOW_VALUE` | 来源充分且持久，但没有可辨认的未来复用价值，只会制造噪声 | 来源不确定、模型不喜欢内容、常见但对该用户/项目有用的事实 |
| `MULTI_ATOM_STATEMENT` | whole proposal 含两个或更多需独立分类/更新/删除的 atom | 一个 atom 的条件/例外 |
| `USER_PROFILE_SCOPE_OR_KIND_MISMATCH` | proposal 只有作为 USER_PROFILE 才忠实，但 frozen scope 不是 USER，或它声称描述当前用户却实际不是用户画像 | 可忠实 reclassify 为其他 legal kind 的错误 hint |
| `UNSAFE_RESPONSE_PREFERENCE` | 试图把奉承、依赖、隐瞒风险、permission/system/policy override 等变成回答偏好 | 普通语气/格式偏好 |
| `UNSUPPORTED_STRUCTURE` | whole atom 需要的 frozen structured shape 不成立，且没有任何 legal final kind 能忠实承载，例如行动规则缺失必要 future condition | 来源未支持已存在的 field；后者是 INSUFFICIENT_SOURCE_SUPPORT |
| `RECALLED_MEMORY_ECHO` | 与 model-visible memory 等价且没有 direct relevant new evidence | 有明确 replacement/contradiction intent 的 exact duplicate |
| `DUPLICATE` | exact existing ACTIVE source 已表达同一 semantic payload，且没有 relation intent | semantic echo provenance 检查或 explicit relation |

模型不得输出 capacity、provenance-overflow、settlement drift、provider failure 或 `ABANDONED_*` reason；
这些是 Host-only mechanical/terminal outcomes。若多个 model reason 同时表面适用，按 §7.10 顺序选择
最先成立者，并在不确定时 fail closed；不能返回 reason array。

### 7.12 Public summary

所有 `ACCEPT | ACCEPT_AND_SUPERSEDE | ACCEPT_AND_CONTRADICT` 输出必须携带非空
`public_summary`，1..2048 UTF-8 bytes。SKIP 可以携带 summary。

summary 必须：

- 使用产品语言说明 candidate 的来源形成方式；
- 区分“用户明确表达”“根据可引用观察”“对话中形成的项目决定”；
- 对 relation branch 仍保持 target-independent：不能写 target 正文/ID，不能把“更新了谁”或
  “与谁冲突”编码进 summary；关系由 canonical SUPERSEDES/CONTRADICTS row 在 UI 单独投影；
- 不包含 candidate ID、reason code、producer enum、evidence_role、scope ID、SQL、prompt 或
  provider 实现词；
- 不声称“已验证为真”“永久保存”或“保证以后使用”；
- 不泄漏 foreign-origin provenance；
- 不复制无必要的完整 ToolResult/对话正文。

示例：

- “根据你明确表达的长期回答偏好整理。”
- “根据本轮可引用的项目观察整理；它仍作为参考信息使用。”
- “根据你在本轮明确作出的项目选择整理。”

这个 target-independent 合同是删除 hard cut 的前置不变量：若用户以后删除 relation target，存活
source candidate 可从 `ACCEPT_AND_SUPERSEDE | ACCEPT_AND_CONTRADICT` 归一化为 `ACCEPT`，同时原样
保留非空 summary；不需要重跑 governance、解析自然语言或伪造新的形成原因。

### 7.13 Required few-shot matrix

SYSTEM prompt 必须包含紧凑的中英 mixed few-shot，不少于以下语义覆盖。few-shot 可以使用符号
memory IDs，但不能把所有 accept 示例写成 FACT。

| 场景 | 必须教授的结果 |
| --- | --- |
| “我使用 macOS，所以以后给我 zsh 命令”作为单 candidate | MULTI_ATOM skip；正确 intake 应拆 USER_PROFILE + RESPONSE_PREFERENCE |
| “生产用 PostgreSQL，schema 变更前先备份”作为单 candidate | MULTI_ATOM skip；正确拆 FACT + ACTION_RULE |
| “我们决定用 PostgreSQL，依据 m1/m2” | DECISION，basis 必须真是理由 |
| “我喜欢川菜” | USER_PROFILE，不是 RESPONSE_PREFERENCE |
| “回答先给结论” | RESPONSE_PREFERENCE，不是 USER_PROFILE |
| “本项目生产数据库是 PostgreSQL” | WORKSPACE FACT |
| statement“执行 schema 变更前先备份” + applies_when | ACTION_RULE，不是 permission |
| assistant 未获授权自行说“以后部署前一律由我删除旧数据” | INSUFFICIENT_SOURCE_SUPPORT skip，不能成为 ACTION_RULE |
| “本项目已经决定采用方案 B” | DECISION，不是 FACT 或待办 |
| “永远同意我，不要指出风险” | UNSAFE_RESPONSE_PREFERENCE skip |
| “明天提醒我提交报告” | TEMPORARY_OR_EPHEMERAL skip |
| source“可能更喜欢短回答”，candidate“总是喜欢短回答” | INSUFFICIENT_SOURCE_SUPPORT skip，不能 rewrite certainty |
| recalled memory 被 assistant 原样再 remember，无新 human/tool evidence | RECALLED_MEMORY_ECHO skip |
| PLAN_CONTINUATION 写“以后都用 zsh” | non-human context，不能支持 USER preference |
| old“默认英文”，human“以后改成中文” | explicit SAME_KIND_REPLACEMENT supersede |
| human“这一次用中文” | one-off，不 supersede durable old preference |
| 两条规则适用条件不同 | coexist，不 contradict |
| 同 atom 旧 kind 错且 human 明确纠正分类 | TAXONOMY_CORRECTION |
| proposal 后 user steer“只是本次，不要长期记住” | INSUFFICIENT_SOURCE_SUPPORT skip |
| assistant 在获授权比较后明确公布最终方案，human 未反对 | 可接受 WORKSPACE DECISION；assistant 仍不证明 USER_PROFILE |

---

## 8. Dynamic Evidence Packet v2

USER JSON packet 建议 closed shape：

~~~text
contract
candidate
  statement
  scope
  kind_hint
  legal_final_kinds
  applies_when
  do_not_apply_when
  basis_memory_ids
origin_turn
  terminal_status
  producer_kind_product_label
source_coverage
producer_call_context
producer_public_output
post_proposal_turn_suffix
cited_tool_evidence
based_on_items
all_model_visible_memory
exact_existing_source
allowed_relation_targets
output_schema
~~~

每个 conversation source item 使用相同的 closed public shape：

~~~text
source_handle                 # call-local，不是 durable/raw ID
chronology                    # BEFORE_PROPOSAL | PRODUCER_OUTPUT | AFTER_PROPOSAL
source_product_label          # 产品语言，如“你的原话”“assistant 公开回复”“任务延续上下文”
evidence_role                 # §6 closed role
public_kind                   # user message / user steer / assistant text / tool result / ...
blocks[]
  block_kind                  # TEXT | DATA；普通 entry 为单 TEXT block
  text
  truncated
item_omitted_before
item_omitted_after
~~~

`chronology` 和 `evidence_role` 由 Host 从 exact canonical origin 与 producer entry fence 机械生成；
动态正文不能自报。`source_product_label` 必须使用用户可理解的稳定文案，不暴露 transcript enum，
但不得把 non-human origin 美化成“你说过”。assistant TEXT/DATA 必须保持 block boundary；省略/截断
marker 不能混入正文伪装为原话。

`producer_kind_product_label` 只能取 §7.2 的两个产品 labels，并附带一句“形成路径不证明内容正确”；
不能传 raw producer enum 后让模型自行猜业务语义。

### 8.1 Packet 不暴露的内容

不传：

- memory domain、raw workspace ID、context binding revision ID、entry/event sequence、terminal event ID、
  candidate ID；
- provider target fingerprint、wire API、replay fragment、embedding/rerank score；
- permission snapshot、system prompt、tool schemas；
- governance watchdog、owner、lane、Host、claim 等实现拓扑术语；
- model internal reasoning。

内部 exact IDs 可留在 process-local envelope 做 join；provider-visible source 使用 `context:1`、
`producer:1`、`after:1`、`tool:1` 等 call-local handle。relation/basis 所需 memory ID 例外保留，
因为模型必须选择 exact target。

### 8.2 Output schema 不是示例

`output_schema` 使用真正的 closed union 描述，不用一个具体 object 冒充 schema：

~~~text
SKIP
  decision = SKIP
  reason_code in MODEL_GOVERNANCE_SKIP_REASON_CODES
  public_summary optional

ACCEPT
  decision = ACCEPT
  final_kind in legal_final_kinds
  public_summary required

ACCEPT_AND_SUPERSEDE
  decision = ACCEPT_AND_SUPERSEDE
  final_kind in legal_final_kinds
  target_fact_id in allowed_relation_targets
  supersede_mode in SAME_KIND_REPLACEMENT | TAXONOMY_CORRECTION
  public_summary required

ACCEPT_AND_CONTRADICT
  decision = ACCEPT_AND_CONTRADICT
  final_kind in legal_final_kinds
  target_fact_id in allowed_relation_targets
  public_summary required
~~~

任何 extra semantic field、statement rewrite、source quote field、confidence 或 unknown enum 都 fail
closed。Parser 后继续由 `prepare_memory_governance_acceptance` 做唯一 mechanical validation。

---

## 9. Bounds、Final-wire 与安全退化

### 9.1 保留的物理边界

本规范保留 Round 8 已验证的物理边界：

| 项 | 上界 |
| --- | ---: |
| candidate canonical payload | 32 KiB UTF-8 |
| origin-turn source projection | 32 KiB encoded |
| cited ToolResult aggregate preview | 64 KiB encoded |
| complete model-visible memory | 128 items / 64 KiB encoded |
| relation targets | 8 items |
| total governance user evidence + system contract final wire | 128 KiB exact bytes 且不超过本地 estimator 的 32,768 input tokens |
| governance output | 8 KiB UTF-8 / 8,192 provider output tokens（包含 Responses reasoning 预算；real-provider activation 已证明 4,096 会在闭合 JSON 前耗尽） |

删除当前 `_MAXIMUM_GOVERNANCE_TURN_ITEMS = 32` 这一无独立产品意义的 item-count cutoff。
turn metadata 使用 keyset/streaming 读取；真正限制 provider materialization 的是以上 byte/token
物理边界。

这里的 `origin-turn source projection` 是 `producer_call_context + producer_public_output +
post_proposal_turn_suffix` 三部分 provider-visible encoded material 的 aggregate 上界，不包含
candidate、cited ToolResult、model-visible memory 或 relation targets。`complete` 只表示相对于
producer entry 的 canonical input cut 和 origin turn terminal suffix 是否完整投影；它绝不要求
回扫 compaction 前已不在该 cut 中的 raw history。

### 9.2 Exact final-wire admission

SYSTEM + USER 两条 message 必须一起经过当前统一 provider-neutral materialization 与 local estimator：

- final-wire bytes exact；
- token 数只使用统一 local estimator；
- provider usage 只作事后 telemetry；
- reported/missing/mismatch usage 不改变 governance decision；
- 不调用 provider token-count API；
- 不引入厂商 tokenizer 或 provider 特例。

### 9.3 Deterministic shedding

总量超界时按以下顺序缩减：

1. 移除 `allowed_relation_targets`，同时取消 relation authority；
2. 从 producer causal projection 中最旧的非 anchor item 开始移除，保留 coverage marker；
3. 缩减 context-only assistant/tool/snapshot detail；
4. 缩减 cited ToolResult body，但保留 citation identity、evidence kind、state 与 truncation；
5. 保留 candidate、SYSTEM contract、output schema、完整 model-visible memory、source coverage、
   producer current-turn human anchors 与**全部** post-proposal human material。

禁止当前实现的“把所有 producer_turn body 一次性置空后继续治理”。

如果 MUST_KEEP source anchors 与其他 MUST_KEEP material 仍无法 exact fit：

- 不打开 provider；
- best-effort terminalize 为 `SKIP(INSUFFICIENT_SOURCE_SUPPORT)`；
- 不截断 candidate 后治理；
- 不选择 relation target；
- 不把它变成 foreground failure。

### 9.4 Truncation 语义

每个 truncated/omitted item 都显式标注。SYSTEM prompt 说明：

- truncated 不等于 source 不存在；
- omitted 不等于没有更正；
- source coverage 不完整时不能取得 relation-writing authority；
- 不能从 assistant summary 猜被省略的 human 原话；
- 不能为了得到 ACCEPT 而降低 candidate certainty。

其中任何 post-proposal human truncation/omission 不是交给模型解释的 coverage marker，而是
provider-open 前的 deterministic SKIP。只有 causal history、assistant/tool/context detail 的允许退化
才进入 packet 供模型按 marker 判断。

---

## 10. Ownership、Deadline 与 Provider Neutrality

### 10.1 Linear ownership

- candidate `PENDING/PROCESSING` row 是 canonical processing head；
- terminal wake 只是 process-local lossy signal；
- exact causal cut borrow 只属于一次 evidence capture；
- frozen source envelope 由一次 governance attempt 唯一持有；
- packet materializer 借用 envelope，不复制成 registry；
- auxiliary SYSTEM/USER messages 由一个 prepared call 持有；
- provider execution、transport close 和 physical completion 只关闭一次；
- prepared decision 直接交给 acceptance/confirmation，不重新调用模型。

不得 alias HostWriterGuard、continuity capability、provider execution 或 DB transaction。

### 10.2 Deadline

现有 `MEMORY_GOVERNANCE_ATTEMPT` finite local deadline 从 successful claim 后签发，继续覆盖：

- causal source read；
- terminal suffix/evidence read；
- sparse/dense relatedness；
- packet materialization/final-wire estimation；
- auxiliary provider call；
- acceptance/confirmation settlement。

不在 source read、relatedness 或 provider open 后重新签发完整 attempt。source read/DB pool/connect/write/
read-idle 使用 remaining 与各自物理上限的较小值。

这不会给 provider stream、turn、task、Host 或 long-running session 增加 total lifetime cap；只约束
一次局部治理 attempt。

### 10.3 Provider neutrality

- source projection 只读 canonical semantic snapshot；
- final wire lowering 使用统一 adapter materializer；
- Chat Completions 与 Responses 只能在 adapter 层形成各自 wire；
- prompt/business logic 不读取 provider name 或 wire API；
- main producer 与 auxiliary target 不同 trust domain 时只服从现有显式 egress opt-in；
- 不因 provider usage、model name 或 token telemetry 改分类阈值。

---

## 11. PostgreSQL Clean-v0 Delta 与最小性

### 11.1 不新增 schema shape

禁止新增：

- 表、列、index、UNIQUE、FK、CHECK trigger、function；
- candidate source snapshot/prompt fields；
- origin-turn-terminal marker；
- governance generation/attempt/receipt；
- source item/citation relation；
- runtime grants。

claim/source reader 只使用现有 `memory_candidates -> transcript_entries -> turns -> sessions` joins，
并只读既有 `agent_events` 的 entry-acceptance 与
`TurnCompleted | TurnInterrupted` occurrences；不新增 event kind、row、subject slot、index、fence
column 或 snapshot。若现有 occurrence 不唯一/不匹配即 fail closed，不能为“修复”补写 event。

### 11.2 唯一允许的 CHECK vocabulary 变化

新增 closed reason：

~~~text
INSUFFICIENT_SOURCE_SUPPORT
~~~

同步修改：

- `MemoryDecisionReasonCode`；
- `MODEL_GOVERNANCE_SKIP_REASON_CODES`；
- clean-v0 `memory_candidates.decision_reason_code` CHECK list；
- 由 baseline checksum/catalog verifier 机械派生的资源。

它表示：可见的 human/assistant-decision/PRIMARY_OBSERVATION 来源不足以忠实支持 frozen candidate，
包括 candidate 在 origin turn terminal occurrence fence 前被用户撤回或限定、certainty 被 candidate
放大、或物理 source coverage 无法提供最低支持。

这不是 durability 强化，也不创建新 lifecycle/row；它只是让现有 SKIP transition 的产品原因
诚实可解释。

### 11.3 Schema diff oracle

实施前后 catalog diff 必须证明：

- 表/列/index/FK/trigger/function/grant 数量不变；
- 唯一语义变化是 `decision_reason_code` CHECK 增加一个 value；
- baseline checksum 与确实由该 CHECK 变化引起的 derived catalog identity 更新；
- event、subject、guard、relation、job oracle 全部不增加。

本地数据库可以在核验为 disposable local target 后 reset clean-v0；不提供 0001、online migration、
旧 reason alias 或双读。

---

## 12. 文件级实施范围

### 12.1 必须修改

- `src/pulsara_agent/memory/` 下新增或现有的 provider-neutral product/prompt contract module；
- `src/pulsara_agent/ports/system_prompt.py`；
- `src/pulsara_agent/capability/builtin_catalog.py`；
- `src/pulsara_agent/conversation_kernel/auxiliary_model.py`；
- `src/pulsara_agent/conversation_kernel/reader.py`；
- `src/pulsara_agent/conversation_kernel/memory/contracts.py`；
- `src/pulsara_agent/conversation_kernel/memory/governor.py`；
- `src/pulsara_agent/conversation_kernel/_repository/memory.py`；
- `src/pulsara_agent/conversation_kernel/tool_execution.py`；
- `src/pulsara_agent/conversation_kernel/runner.py` 及所有实际 terminal wake seam；
- `src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql`；
- 对应 baseline checksum/expected catalog 资源；
- `tests/test_round8_advisory_memory.py`；
- `tests/test_stage2_canonical_reader.py`；
- 新增 focused prompt/source semantic tests。

### 12.2 仅在职责 seam 实际位于其中时修改

- `src/pulsara_agent/conversation_kernel/memory/dispatch.py`；
- `src/pulsara_agent/conversation_kernel/host.py`；
- canonical model-input contracts/reader tests。

不能复制一份 governance-only transcript reader 来绕过现有 canonical cut。

### 12.3 不应修改

- memory recall ranking/embedding/rerank 算法；
- memory fact/relation schema；
- management UI；
- deletion algebra；
- provider usage telemetry；
- current epoch installed prefix。

---

## 13. 严格实施顺序

1. 冻结 shared taxonomy、scope、single-atom、source role、relation 与 summary 的纯 product contract；
   先写纯 prompt contract tests。
2. 增加 `INSUFFICIENT_SOURCE_SUPPORT` closed reason，只修改 clean-v0 CHECK vocabulary 与 derived
   catalog evidence；schema diff 证明没有其他数据库变化。
3. 修改 claim SQL：只 claim terminal origin turn，exact join/freeze status-matched terminal occurrence
   fence；增加 running/terminal/missing-or-duplicate occurrence/order/origin PostgreSQL tests。
4. 删除 remember ToolResult immediate wake；在 canonical terminal seams 安装 process-local lossy wake；
   证明 provider 在 turn RUNNING 时 exact 0 call。
5. 从 canonical reader 抽出由 producer assistant entry authority 限定的 historical semantic-cut
   seam，保留 foreground current-head guard；用 existing exact cut 构造 `producer_call_context`。
6. 构造 block-preserving producer output 与按 acceptance-event/terminal-event 全序截取的
   post-proposal terminal suffix；删除 PLAN_CONTINUATION-as-USER 和 first-32-entry path，稳定排除
   terminal fence 后的 late entries。
7. 构造 typed source coverage/evidence role；generic tool/assistant/context 不得取得证据语义。
8. 新增 MEMORY_GOVERNANCE stable SYSTEM + USER evidence two-message call；统一 local final-wire
   estimation。
9. hard cut packet v2、legal_final_kinds、closed output schema、public_summary required acceptance；
   删除 v1 builder/fallback。
10. 实现 deterministic shedding、post-proposal human 全量 MUST_KEEP 与 source-insufficient
    no-provider SKIP。
11. 跑 focused non-PostgreSQL tests；修复 prompt/DTO/source projection。
12. reset 已核验 local disposable PostgreSQL，跑 clean-v0、claim、governance、concurrency tests。
13. 跑 real-provider governance semantic dogfood，检查中英 taxonomy、source correction、anti-echo 与
    relation hard negatives。
14. 完成本规范全部验证后，才开始记忆管理页面与级联删除规范的生产实施。

---

## 14. 必测矩阵

### 14.1 Terminal claim

- MAIN_AGENT candidate origin turn RUNNING：claim 返回 none，row 保持 PENDING，provider call 0；
- 同一 Host 更早 RUNNING candidate 不阻塞更晚 terminal candidate；
- COMPLETED candidate 可 claim；
- INTERRUPTED candidate 可 claim且 packet 明确 terminal outcome；
- COMPLETED/INTERRUPTED status 缺少匹配 terminal occurrence、出现多个 occurrence、event type/payload
  与 turn winner 不一致：candidate 不 eligible，且不阻塞后续健康 candidate；
- status + terminal occurrence 共同 commit 后 process-local wake 才触发 claim；
- candidate intake 与 terminal wake 之间 Host crash：重开 same origin Host 的 open scan 可 best-effort
  claim，但不承诺一定重开；
- foreign session/workspace/domain Host 不能 claim；
- transient Host 仍只能治理自己的 USER candidate；
- reflection candidate 继续只从 terminal turn claim；
- claim 冻结 exact terminal event id/sequence/status/outcome，provider open 前 exact recheck。

### 14.2 Exact causal source

- producer assistant entry 的 `provider_input_through_sequence` 而非 session latest cut 被使用；
- context binding revision exact；
- candidate 后同轮 adopted compaction successor 使 current revision 改变时，仍由 producer entry
  authority 读取旧 exact semantic cut；foreground `read_frozen_snapshot` 的 stale guard 保持；
- candidate 之后的新 turn 不进入 causal input；
- candidate 之前主模型实际看过的前一 turn/context snapshot 可进入 bounded causal tail；
- compaction 后只投影主模型实际看到的 adopted context snapshot/successor，不回扫被压缩 raw history；
- producer assistant TEXT/DATA block boundary/order保持；
- hidden reasoning与普通 tool args 不进入；
- exact candidate tool payload单独进入；
- post-proposal USER_STEER/final assistant 进入 terminal suffix；
- suffix 中每个 entry 通过唯一 acceptance event 严格满足 source event < entry event < terminal event；
- INTERRUPTED 后 late ToolResult 分别在 claim 前、source read 前、provider open 后提交，三种交错的
  source envelope/decision authority exact 相同，late result 全部不进入 packet/marker/count；
- terminal event 之前已提交的 ToolResult 保留，之后提交的同 turn ToolResult 稳定排除；
- URL 正文不再 blanket redacted；
- `PULSARA_API_KEY` 不进入任何 evidence/log。

### 14.3 Source roles

- candidate 前/当时的 human-origin HUMAN_MESSAGE/HUMAN_STEER -> HUMAN_ASSERTION；
- candidate 后的 human-origin HUMAN_STEER/HUMAN_MESSAGE -> POST_PROPOSAL_HUMAN，Host 不预判其是否更正；
- PLAN_CONTINUATION 不得映射 USER/HUMAN；
- SUBAGENT_OBJECTIVE/INTER_AGENT_MESSAGE/CONTEXT_SNAPSHOT/TERMINAL_OBSERVATION 为 NON_HUMAN_CONTEXT；
- entry/public kind 看似 USER 但 canonical origin 是 runtime/plan 时仍为 NON_HUMAN_CONTEXT；
- assistant 为 ASSISTANT_CONTEXT；
- 未引用 ToolResult 为 TOOL_CONTEXT_ONLY；
- cited ordinary ToolResult 为 PRIMARY_OBSERVATION；
- memory read/artifact descendant 为 MEMORY_READ_EXPOSURE；
- assistant/context/tool-only 内容不能解除 echo；
- user correction 可以使 frozen candidate source-insufficient，但不能让 governor rewrite。
- 任一 post-proposal human entry/body/block 截断、省略或 acceptance occurrence ambiguous：provider
  call 0，deterministic INSUFFICIENT_SOURCE_SUPPORT skip；撤回语句位于被裁掉尾部也同样成立；

### 14.4 Taxonomy 与 source support

- FACT、USER_PROFILE、RESPONSE_PREFERENCE、ACTION_RULE、DECISION 中英 golden；
- USER_PROFILE vs RESPONSE_PREFERENCE；
- FACT vs DECISION；
- FACT vs ACTION_RULE；
- project-specific user role不能成为 USER USER_PROFILE；
- response preference one-off vs durable；
- sensitive profile不靠 assistant inference；
- assistant-authorized project decision positive/negative；
- assistant-authored WORKSPACE ACTION_RULE explicit-authorization positive/negative；
- certainty 放大必须 INSUFFICIENT_SOURCE_SUPPORT skip；
- unsupported scope duration、applies_when、do_not_apply_when 或遗漏来源关键限定必须
  INSUFFICIENT_SOURCE_SUPPORT skip；
- temporary/TODO/reminder/secret/raw ToolResult/permission exclusions；
- unsafe response preference skip；
- legal_final_kinds exact复用 shape validator；
- output schema 无 hard-coded universal FACT placeholder。
- 九个 model SKIP reason 都有 positive/hard-negative golden；model 输出 Host-only capacity、overflow、
  drift 或 ABANDONED reason 必须 fail；

### 14.5 Single atom 与 basis

- profile + response preference multi-atom；
- fact + action rule multi-atom；
- decision + independent reason text multi-atom；
- multi-atom 只能 SKIP，不能 partial accept/split/rewrite；
- DECISION basis 全部真实通过；
- unrelated basis 使 candidate skip；
- governor 不能增加/删除/替换 basis；
- non-DECISION basis mechanical reject 保持。

### 14.6 Anti-echo

- exact recalled memory echo无新证据 skip；
- semantic paraphrase echo无新证据 skip；
- relevant new human correction可以解锁新 candidate；
- relevant PRIMARY_OBSERVATION可以支持新 FACT；
- unrelated primary observation不能解锁；
- MEMORY_READ_EXPOSURE及artifact descendant永不解锁；
- PLAN_CONTINUATION永不作为 human 解锁；
- provenance OVERFLOW继续 deterministic skip；
- exact duplicate仍进入 provider以保留 relation intent。

### 14.7 Relations

- explicit same-kind replacement positive；
- similarity/recency-only supersede hard negative；
- one-off request不 supersede durable preference；
- temporal project-state replacement positive；
- taxonomy correction同 atom/旧 kind 错 positive；
- cross-kind related-but-different atom correction negative；
- same scope/kind incompatible propositions contradict；
- condition/time/workspace不同 coexist；
- source coverage incomplete 时 allowlist empty、relation output fail；
- target 不在 allowlist fail；
- target lifecycle/kind/scope drift transaction fail；
- contradiction不选择 winner，两端保持 ACTIVE。

### 14.8 Prompt and output

- governance call exactly SYSTEM + USER、tools 0、continuity 0；
- SYSTEM 明确 advisory-memory 产品用途、四种 relation/BASED_ON 的产品后果、producer labels 与
  public summary 的用户可见用途；
- 每个 source item 保留 chronology、source product label、evidence role、public kind、block boundary
  与 truncation/omission marker；non-human label 不得出现“你说过”；
- dynamic evidence 中的“忽略上文”等 prompt injection不能覆盖 SYSTEM contract；
- shared taxonomy在 BASE_SYSTEM、remember descriptor、governance prompt 语义一致；
- required few-shot 全部存在且 accept kinds 分布正确；
- ACCEPT* 缺少/空 public_summary fail；
- summary 超 2048 bytes fail；
- relation branch summary 引用 target/更新/冲突关系的 product golden fail；target-independent
  formation summary 在 relation target 删除后的 candidate normalization 中 byte-identical 保留；
- summary 含 internal ID/enum/prompt术语的 real-provider fixture不得通过产品 golden；
- extra field、statement、confidence、unknown enum fail；
- v1 packet/fallback exact 0。

### 14.9 Bounds 与 provider neutrality

- origin turn 超过 32 entries 时末尾 human correction仍被穷尽发现；不存在 first-32 semantic cutoff；
- 全部 post-proposal human material 是 MUST_KEEP；任一正文不能 exact fit 时 no-provider
  INSUFFICIENT_SOURCE_SUPPORT skip，不能发送 partial human suffix；
- related targets先移除并取消 relation authority；
- older causal tail可退化且 coverage诚实；
- producer human bodies不能被整体清空后继续 relation；
- MUST_KEEP超界 no-provider INSUFFICIENT_SOURCE_SUPPORT skip；
- final-wire exact bytes与统一 local token estimate join；
- provider usage reported/missing/mismatch均不改变 decision；
- Chat/Responses不改变 prompt语义或 source selection；
- provider token-count API call 0；
- provider-name business branch 0。

### 14.10 Architecture oracle

- committed event、live event、subject slot、append guard、product relation、durable job 数量不增加；
- memory table/column/index/FK/trigger/function/grant 数量不增加；
- 只增加一个 reason CHECK value；
- source envelope/prompt/coverage 全部 process-local；
- no retry/receipt/registry/generation；
- SYSTEM/tools existing epoch byte-identical、messages suffix-only。

---

## 15. 验证命令与 Real-provider Dogfood

实现时优先使用仓库根 `.venv/`。实际新增测试文件名可按仓库惯例调整，但最终必须报告真实命令。

最低 pure/focused：

    .venv/bin/python -m pytest -q \
      tests/test_memory_governance_semantics.py \
      tests/test_round8_advisory_memory.py \
      tests/test_stage2_canonical_reader.py \
      tests/test_stage2_conversation_runner.py

PostgreSQL/clean-v0：

    .venv/bin/python -m pytest -q \
      tests/test_stage5_clean_migration.py \
      tests/test_stage2_conversation_kernel_postgres.py \
      tests/test_round8_advisory_memory.py \
      tests/test_memory_governance_semantics.py

静态与构建按仓库真实命令运行：

    .venv/bin/python -m ruff check src tests
    .venv/bin/python -m compileall -q src tests
    uv lock --check
    git diff --check

不得增加 skip/xfail、弱化断言或改写 evidence 换绿。

### 15.1 Real-provider semantic dogfood

本规范改变模型语义合同，real-provider dogfood 是 activation requirement。应使用 exact production
governance SYSTEM + evidence packet，不用另写简化 prompt。

至少覆盖：

1. 中文/英文五类各一条正确分类；
2. USER_PROFILE / RESPONSE_PREFERENCE hard pair；
3. FACT / DECISION / ACTION_RULE hard pair；
4. multi-atom skip；
5. unsafe response preference skip；
6. source certainty 放大 skip；
7. candidate 后 user correction skip；
8. PLAN_CONTINUATION 不能当 human；
9. recalled-memory echo negative 与 relevant human correction positive；
10. explicit supersede positive、similarity-only negative；
11. taxonomy correction positive/negative；
12. contradiction与条件不同 coexist hard negative；
13. accepted public_summary 不含内部术语且不声称 verified/permanent。

Dogfood 必须记录实际 SYSTEM、evidence packet、raw model JSON、parse/settlement outcome 和 final rows，
以便诊断；只排除 `PULSARA_API_KEY` 的值。若 provider/凭据不可用，完成全部本地验证并精确报告
环境阻塞，不能声称 dogfood 已通过。

---

## 16. 禁止实现清单

以下任一出现都视为 hard-cut 失败：

- turn RUNNING 时 claim 后在 governor 内 sleep/poll 等 terminal；
- 增加 durable terminal wake/job/lease/receipt；
- candidate intake 后立即 provider open；
- 用 session latest context 代替 producer exact cut；
- 回扫完整 raw history冒充主模型当时所知；
- 把 main SYSTEM/tools/native replay复制给 governance；
- PLAN_CONTINUATION 映射为 USER；
- assistant text 默认作为 human/primary evidence；
- 未引用 ToolResult 作为 PRIMARY_OBSERVATION；
- blanket URL redaction；
- first 32 entries semantic cutoff；
- 超界时清空全部 producer turn 后继续建立 relation；
- 所有 output example 的 final_kind 都写 FACT；
- taxonomy只有 enum 名没有产品定义；
- similarity/recency 自动 supersede；
- cross-kind relatedness 自动 taxonomy correction；
- condition不同自动 contradiction；
- governance rewrite/split/merge/partial accept；
- confidence/source score/truth score；
- provider token count API/vendor tokenizer/provider倍率；
- v1/v2 prompt fallback；
- 现有 epoch SYSTEM/tools rewrite；
- 0001 migration/online compatibility；
- `INSUFFICIENT_SOURCE_SUPPORT` 之外的 schema shape/durability 扩张。

---

## 17. 与记忆管理页面/级联删除的接口

后续记忆管理规范依赖本规范提供以下已完成真相：

- accepted fact 来自 terminal-origin、source-aware governance；
- `decision_public_summary` 对所有 accepted branch 非空、只描述 target-independent source
  formation，且可在 relation target 删除后原样保留；
- producer candidate 仍保留 existing turn/entry locator，用于允许时“在对话中查看”；
- source summary 不声称事实 verified、记忆永久或治理必然完成；
- relation 仍只有 BASED_ON、SUPERSEDES、CONTRADICTS；
- contradiction 两端均 ACTIVE、无模型 winner；
- deletion 不需要理解或重放 raw governance prompt；
- deletion 不持久化新的 governance source envelope；
- clean-v0 已包含 `INSUFFICIENT_SOURCE_SUPPORT`，后续 deletion schema diff 从该基线计算。

管理 UI 不展示 raw governance prompt、evidence roles、reason codes 或内部 source coverage。它只使用
公开形成方式、`decision_public_summary`、允许公开的 origin locator 和 relation 产品投影。
UI 也不把“governance 模型”渲染成一位可见裁判，不展示其 confidence、队列/处理中状态或 raw
decision JSON；那会把 advisory 整理过程误导成事实认证。用户真正需要的是 canonical memory 正文、
适用范围、公开形成摘要、可公开来源入口、更新/依据/冲突关系，以及由用户直接授权的删除。

---

## 18. Definition of Done

只有同时满足以下条件，本规范才完成：

1. RUNNING origin turn candidate 无法 claim；只有 terminal status 与唯一 exact terminal occurrence
   同时成立后才能 claim，claim/source/provider 共用同一 process-local occurrence fence。
2. remember candidate intake 不再立即 wake provider governance；terminal seam 提供 lossy wake，
   Host open scan 保留 weak-completion fallback。
3. MAIN_AGENT governance 使用 producer assistant entry 的 exact canonical causal cut，而不是 same-turn
   partial scan 或 latest context。
4. candidate 后、terminal occurrence fence 前的 user steer/correction/final suffix 可见；fence 后
   late entries 无论何时提交都稳定排除。
5. PLAN_CONTINUATION、assistant、generic tool、memory exposure 不再冒充 human/primary evidence。
6. governance prompt 是 stable SYSTEM + dynamic USER packet，完整理解 taxonomy、scope、source、
   single-atom、basis、anti-echo 和 relation 产品语义。
7. legal_final_kinds 来自唯一 shape validator；无 universal FACT placeholder。
8. source correction/不足使用 closed `INSUFFICIENT_SOURCE_SUPPORT`，candidate 不被 rewrite；任何
   post-proposal human source 不完整都在 provider open 前 deterministic skip。
9. ACCEPT* 都产生可公开、无内部术语、无 verified/permanent 暗示、target-independent 的 formation
   summary，relation target 删除后可 byte-identical 保留。
10. relation branch 只有在完整 source coverage 与 exact allowlist 下可签发。
11. current first-32-item、PLAN-as-USER、blanket URL redaction、whole-turn body clearing、v1 prompt
    路径全部删除。
12. schema diff 只有一个 reason CHECK value；无表/列/index/FK/trigger/grant/event/job 增长。
13. provider prefix continuity、final-wire local estimation、provider neutrality 保持。
14. focused、PostgreSQL、clean-v0、static 和 real-provider semantic dogfood 全部通过；环境不可用时
    只允许明确报告阻塞，不能虚报。
15. 本规范完成后，记忆管理页面与级联删除规范才可进入生产实施。
