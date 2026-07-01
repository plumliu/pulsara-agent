# Pulsara 下一阶段三条主线路线图

## 0. 背景

Pulsara 现在已经完成了大部分底层能力:

- durable Postgres event log / artifact store / canonical graph projection。
- Oxigraph mirror 迁移路线已经收口。
- terminal 三层契约、env 契约、kill reason / completion suppression 已经冻结。
- plan workflow 已经有 typed event、pending interaction、approved plan artifact。
- failed / aborted recovery 契约已冻结。
- memory surfaces 契约已冻结。
- sparse + dense + rerank 的语义级 memory recall 已经接入 durable runtime。
- real LLM dogfood 已经覆盖 terminal、plan、permission、memory recall、governance relatedness 等关键路径。

因此下一阶段不应再优先补单点能力，而应优先补三条横向主线:

1. Inspector / Observatory: 让 Pulsara 能解释自己发生了什么。
2. Compaction / Continuity: 让 Pulsara 在长程压缩后仍然保持语义连续。
3. Unified Capability Surface: 让 MCP / CLI / skills / built-in tools 进入同一套能力协议。

这三条主线的共同目标是:在能力继续扩张前，把可观测性、长程连续性、能力边界统一起来。

## 1. 总体排序

推荐顺序:

1. Inspector / Observatory
2. Compaction / Continuity
3. Unified Capability Surface

原因:

- Inspector 是后两者的调试地基。没有 inspector，compaction 错误和 capability gate 错误都会变成靠猜。
- Compaction 是长程 agent 的核心稳定性。Pulsara 已经有后台 terminal、plan、memory governance、artifact、recall、recovery notes，这些都必须在压缩后可恢复。
- Capability Surface 会显著扩大工具数量和外部执行面，应在 inspector 与 compaction 稳定后再扩张。

依赖关系:

| 主线 | 依赖 | 为什么 |
| --- | --- | --- |
| Inspector | 当前 event log / artifact / memory / terminal / plan 契约 | 只读观察面，先做风险最低 |
| Compaction | Inspector | 压缩后“模型为什么看到这些上下文”需要 inspector 验证 |
| Unified Capability Surface | Inspector + permission contract | 工具面扩大后需要统一观测、权限、artifact 行为 |
| MCP adapter | Unified Capability Surface | MCP 不应绕过本地 capability metadata |
| Skills / CLI adapter | Unified Capability Surface | skills / CLI 应与 MCP 共用注册和权限语义 |

## 2. 主线一:Inspector / Observatory

### 2.1 目标

建立一个完整的 Pulsara run inspector，让开发者和高级用户能回答:

- 这轮 run 发生了哪些事件?
- 模型最终看到了哪些 prior messages / system instructions / tool results / recovery notes / memory projections?
- 某条 memory 为什么被召回、为什么被过滤、为什么被写入或拒绝?
- 某个 terminal process 是从哪次 tool call 来的、何时 yielded、何时 completed、log artifact 在哪里?
- plan mode 是谁进入的、何时退出、批准的 plan artifact 是哪个?
- 为什么一次 run failed / aborted 后，下一轮出现了某条 recovery note?
- 是否存在 publisher sequence gap、orphan tool call、late result、unpaired tool call 等异常?

### 2.2 基本原则

Inspector 必须是 read-only。

它不修复状态、不回写 memory、不触发 governance、不重新执行工具。它只读取:

- Postgres event log。
- ArtifactStore。
- canonical graph / projections。
- tool result artifact index。
- terminal process registry 中仍然 retained 的 live/finished process。
- plan workflow state / event reducer 结果。
- recall trace / recall usage。
- memory write outbox。
- Oxigraph mirror 状态。

Inspector 的输出应明确区分:

- canonical facts: event log、artifact、canonical graph、typed plan event。
- derived projections: transcript note、memory projection、recall trace、run timeline。
- runtime retained state: live terminal process、pending interaction、publisher queue。
- diagnostics: sequence gap、missing artifact、outbox failed、mirror lag。

### 2.3 V1 功能面

V1 不需要做 UI，可以先做 CLI + programmatic API。

建议 API:

- inspect_session(session_id) -> SessionInspection
- inspect_run(run_id) -> RunInspection
- inspect_reply(reply_id) -> ReplyInspection
- inspect_memory(memory_id) -> MemoryInspection
- inspect_artifact(artifact_id) -> ArtifactInspection

建议 CLI:

- uv run pulsara inspect session SESSION_ID
- uv run pulsara inspect run RUN_ID
- uv run pulsara inspect memory MEMORY_ID
- uv run pulsara inspect artifact ARTIFACT_ID
- uv run pulsara inspect health

### 2.4 Run Inspection 输出

inspect run 至少包含:

- run metadata:
  - session_id
  - run_id
  - turn ids
  - status
  - start/end sequence
  - abort / error / stop reason
- ordered event timeline:
  - sequence
  - type
  - run / turn / reply
  - short payload summary
- model transcript reconstruction:
  - prior messages
  - current user message
  - runtime instruction messages
  - memory projections
  - recovery notes
  - completion notes
  - tool result blocks
- tool execution summary:
  - tool_call_id
  - tool_name
  - start/end sequence
  - status
  - artifact refs
  - permission / approval / workflow gate if relevant
- terminal summary:
  - process_id
  - command
  - yielded / completed / killed
  - completion_reason
  - retained log availability
- plan summary:
  - active before run?
  - entered / asked / answered / exit requested / resolved / exited
  - accepted_plan_artifact_id
- memory summary:
  - recalled memory ids
  - recall trigger
  - vector / sparse / rerank channels
  - memory candidates proposed
  - governance decisions
  - memory write events
- diagnostics:
  - sequence gaps
  - event order anomalies
  - orphan tool calls
  - late tool results
  - missing artifact refs
  - outbox failed / pending
  - Oxigraph mirror lag

### 2.5 Health Inspection

inspect health 应用于排查运行环境，不绑定单次 run。

至少检查:

- Postgres connectivity。
- Oxigraph connectivity。
- Postgres schema presence。
- pgvector extension。
- memory_write_outbox failed / stale pending。
- vector worker lag:
  - pending vector_index surface count
  - failed vector_index count
- Oxigraph mirror lag:
  - pending / failed oxigraph surface count
- artifact orphan count。
- recall trace volume。
- last event sequence per session。
- publisher retained diagnostics if attached to live host。

### 2.6 为什么它优先

最近的 publisher sequence deadlock 就是典型例子。

如果 Inspector 已经存在，故障路径应该一眼能看到:

- last published sequence: 7768
- event log next observed: 7769 MEMORY_CANDIDATE_PROPOSED
- publisher pending: 7771 RUN_START
- diagnostic: sequence gap caused by direct event_log.extend outside publisher handoff

这类能力会让后续 compaction、MCP、capability gate 的调试成本显著下降。

### 2.7 V1 测试

普通测试:

- 构造 event log，断言 inspector 能显示 ordered timeline。
- 构造 tool call + artifact，断言 inspector 能解析 artifact refs。
- 构造 terminal completed event，断言 inspector 显示 lifecycle summary 但不假装 retained log 一定存在。
- 构造 plan typed events，断言 reducer 输出 entered/question/exit/accepted artifact。
- 构造 memory recall trace，断言 sparse/dense/rerank metadata 被展示。
- 构造 sequence gap，断言 health diagnostics 报警。

real LLM / dogfood:

- 对一次 real memory recall dogfood 输出 inspect run。
- 对一次 plan dogfood 输出 inspect run。
- 对一次 terminal yielded process 输出 inspect run。
- 确认 inspector 输出能解释模型看到的 memory projection 与 terminal completion note。

## 3. 主线二:Compaction / Continuity

### 3.1 目标

把 compaction 从“摘要文本”提升为“可验证的长程连续性协议”。

Pulsara 的 compaction 不能只压缩对话。它必须保留或可重建:

- event-sourced truth。
- active plan workflow。
- pending approval / pending plan interaction。
- terminal process lifecycle。
- tool artifacts。
- accepted plan artifact。
- memory recall projection 边界。
- failed / aborted recovery context。
- working context summary。
- run timeline。
- do_not_write_back / projection echo guard。

### 3.2 核心问题

压缩前模型可见上下文包含很多 derived surface:

- prior transcript。
- memory recall projection。
- terminal completion note。
- failed / aborted recovery note。
- plan runtime instructions。
- tool result envelope。
- artifact refs。
- working context summary。

压缩后不能让这些 derived surface 变成新的 canonical facts。

例如:

- memory projection 不能被 reflection 抽成“用户又说了一遍”。
- recovery note 不能被抽成长期偏好。
- terminal preview 不能被误当成完整输出。
- plan instruction 不能被压成用户长期要求。
- failed / aborted unfinished tool summary 不能变成“工具从未运行”的事实。

### 3.3 Compaction Contract

建议冻结一份 contracts/CONTEXT_COMPACTION_CONTRACT.zh.md。

核心规则:

1. Event log remains canonical
   - compaction 不生成新的 canonical event truth。
   - compaction summary 是 derived artifact / working context，不是事实源。

2. Projection carries do_not_write_back
   - memory projections、recovery notes、completion notes、plan instructions 都必须被标记为不可回写或可由 reflection 识别为 projection。

3. Artifacts survive compaction
   - tool result artifact refs、accepted plan artifact refs、terminal log refs 必须保留。
   - 不内联长文本作为“压缩后的事实”。

4. Pending state is structural
   - pending approval / pending plan interaction 不靠自然语言摘要恢复。
   - 它必须来自 runtime state 或 typed event reducer。

5. Terminal state is lifecycle-based
   - 压缩不保存 stdout 全量。
   - retained process/log 通过 terminal runtime / artifact / completion event 读取。

6. Recovery state is replay-based
   - failed / aborted note 由事件窗口重建，不由 summary 生成。
   - late tool result 仍能消掉 unfinished summary。

7. Memory recall is re-queryable
   - cheap auto recall 可以重新跑。
   - explicit memory_search 的历史结果作为 tool result/artifact 保留。
   - recall trace 可用于解释，不应替代 canonical memory。

### 3.4 实现方向

建议拆成三层。

#### Layer A: Compaction Artifact

把被压缩的历史上下文写入 artifact，而不是丢失。

建议字段:

- kind: context_compaction
- session_id
- through_sequence
- summary
- included_run_ids
- included_artifact_ids
- excluded_projection_ids
- created_at

#### Layer B: Rehydration Planner

根据 event log + current runtime state 决定下一轮 prior context:

- compacted summary artifact。
- active plan state。
- pending interaction state。
- recent non-compacted events。
- memory recall projection。
- recovery/completion notes。
- retained terminal summaries。
- tool artifact refs。

#### Layer C: Verification Inspector

Compaction 后用 Inspector 验证:

- 模型实际看到的 context。
- 哪些内容来自 summary。
- 哪些内容来自 event replay。
- 哪些内容来自 recall。
- 哪些内容来自 terminal lifecycle。
- 哪些内容被禁止回写。

### 3.5 关键测试

普通测试:

- memory projection 压缩后不进入 reflection candidate。
- failed note 压缩后不进入 memory candidate。
- terminal completion note 压缩后不暗示完整 log。
- accepted plan artifact id 压缩后仍可见。
- pending plan question 压缩后仍能 resume。
- pending approval 压缩后仍能 resolve。
- late tool result 在压缩边界后仍能消掉 unfinished summary。
- artifact refs 不被 summary 文字替代。

real LLM dogfood:

- 设计一个会触发 auto-compact 的长程 run:
  1. 用户进入 plan。
  2. agent 读代码、提问、exit plan。
  3. 用户 approve。
  4. agent 写代码、跑测试。
  5. agent 启动 yielded terminal process。
  6. 中途触发 compaction。
  7. terminal later completion event 出现。
  8. 用户问“刚才做到哪了?”
  9. agent 需要正确使用 retained state / artifact / memory，而不是凭 summary 幻觉。
- 最终断言:
  - 没有重复执行已完成写操作。
  - 没有把 failed/aborted/recovery note 写入 memory。
  - terminal completion note 不重复、不丢。
  - accepted plan artifact id 仍可追踪。
  - final answer 包含 sentinel。

### 3.6 与 Inspector 的关系

Compaction 的每个测试都应该能输出 Inspector report。

没有 inspector 的 compaction 测试很容易只测“最终文本看起来对”，却漏掉:

- projection 被错误回写。
- artifact ref 丢失。
- pending state 由自然语言假恢复。
- recovery note 重复。
- vector recall trace 指向错误 graph。
- terminal retained state 和 summary 冲突。

## 4. 主线三:Unified Capability Surface

### 4.1 目标

把 built-in tools、skills、CLI adapters、MCP tools 统一成一套 capability protocol。

现在 Pulsara 的能力来源会越来越多:

- built-in core tools。
- bundled/local skills。
- future CLI-backed tools。
- future MCP server tools。
- workflow tools:
  - enter_plan
  - ask_plan_question
  - exit_plan
- memory tools。
- terminal tools。
- artifact tools。

如果每一类能力各自处理权限、schema、artifact、read-only、approval、visibility，系统会重新长出一堆平行边界。

Unified Capability Surface 的目标是:

- 一个 registry。
- 一套 metadata。
- 一套 permission / approval gate。
- 一套 artifact policy。
- 一套 observability。
- 多种 provider adapter。

### 4.2 Capability Descriptor

建议每个 capability 统一描述为:

- name
- provider: builtin / skill / cli / mcp / workflow
- namespace
- version
- description
- input_schema
- output_schema
- is_read_only
- mutability: read / write / execute / workflow / external
- permission_category
- approval_policy_hint
- artifact_policy: none / auto_text / auto_binary / producer_supplied
- streaming_policy: none / text_delta / structured_delta
- timeout_policy
- provenance
- availability: available / disabled / unhealthy
- health_message

### 4.3 Gate 原则

工具是否展示给 provider 和工具是否允许执行要分开。

推荐:

- 工具广告保持稳定，尽量不因 mode 变化造成 prompt/tool cache churn。
- 执行期统一经过 capability gate。
- gate 的输入必须包含 descriptor，而不是只看 name allowlist。
- name allowlist 可以作为 hardline fallback，但不应是最终真源。

### 4.4 Permission / Approval 对齐

Capability Surface 应复用现有 permission contract:

- read-only profile:
  - 只允许 is_read_only=True 的 capability。
  - workflow tool 走专门 workflow gate。
- terminal hardline:
  - 当前只覆盖 terminal / terminal_process。
  - 未来如 file write hardline，应作为新的 capability category 决策，不偷偷塞进 terminal 规则。
- approval:
  - mutating tools 根据 policy 进入 approval。
  - read-only observe action 可以豁免。
- plan mode:
  - workflow subsystem 切 read-only。
  - plan workflow tool 的执行由 plan-active state gate，而不是靠 permission enum。

### 4.5 Artifact 对齐

所有 capability 输出都必须使用统一 Tool Result Artifact 协议。

规则:

- 长文本不直接塞满 context。
- provider-supplied artifact candidates 可由 executor 统一归档。
- streaming 工具可以先流 preview，再在 ToolResultEndEvent 附 artifact refs。
- CLI / MCP 返回大 JSON、网页内容、搜索结果时必须进入 artifact policy。
- Inspector 可以从 capability descriptor 上解释 artifact 行为。

### 4.6 Skills / CLI / MCP 的 adapter 形状

#### Built-in Tools

已有 tool registry 可逐步改造为 descriptor registry。

#### Skills

Skill adapter 应提供:

- skill metadata。
- callable commands/tools。
- read/write/execute 分类。
- required files / workspace assumptions。
- prompt instructions 是否需要注入。
- artifact behavior。

#### CLI

CLI adapter 应避免“随便 shell out”。

每个 CLI-backed capability 应声明:

- command template。
- allowed args schema。
- cwd policy。
- env policy。
- timeout。
- output artifact policy。
- mutability。

#### MCP

MCP adapter 应声明:

- server identity。
- tool name namespace。
- remote schema。
- trust level。
- read/write/execute classification。
- result artifact policy。
- health check。
- connection lifecycle。

MCP 工具不应绕过本地 permission / approval / artifact / inspector 体系。

### 4.7 测试

普通测试:

- registry 能同时注册 builtin / skill / cli / mcp descriptor。
- read-only gate 按 descriptor 拦截 mutable capability。
- plan mode 下 workflow tools 由 workflow gate 处理。
- artifact policy 对 CLI/MCP 大输出生效。
- capability health degraded 时工具不可执行但可观测。
- tool advertisement 在 mode 切换中保持稳定。
- inspector 能显示 capability provenance。

real dogfood:

- 用一个 mock MCP server 暴露 read-only search 和 mutating write 两个工具。
- read-only mode 下 search 可用、write 被 gate。
- bypass 下 write 走 approval 或执行，按 policy。
- 大结果进入 artifact。
- inspector 能说明这次 tool 来自 MCP server、用的是哪个 descriptor、为什么被允许/拒绝。

## 5. 三条主线如何互相咬合

### 5.1 Inspector 解释 Capability

Capability Surface 接入后，Inspector 应能显示:

- tool_call: mcp.github.search_issues
- provider: mcp
- server: github
- is_read_only: true
- permission_decision: allowed
- artifact_policy: auto_text
- result_artifact_id: artifact:...

### 5.2 Inspector 解释 Compaction

Compaction 后，Inspector 应能显示:

- compacted_summary artifact
- recent events sequence range
- memory projection ids
- terminal completion notes
- plan active instruction
- recovery note presence
- artifact refs

### 5.3 Compaction 保护 Capability

MCP / CLI / skills 会让工具结果更长、更杂。

Compaction 必须保证:

- capability descriptor 不被摘要成事实。
- tool outputs 通过 artifact ref survive。
- approvals / denied / failed 不变成长期偏好。
- remote MCP result 的 provenance 不丢。

## 6. 推荐 PR 切分

### PR A: Inspector Read Model

- 新增 inspection model。
- 支持 event timeline。
- 支持 run/tool/artifact/memory/plan basic inspection。
- 无 CLI 或只有内部 API。
- Tests: synthetic event log + artifact + memory trace。

### PR B: Inspector CLI

- pulsara inspect run/session/memory/artifact/health。
- 输出 JSON + human readable 两种模式。
- Tests: CLI smoke + durable DB smoke。

### PR C: Compaction Contract

- 新增 contracts/CONTEXT_COMPACTION_CONTRACT.zh.md。
- 定义 projection / artifact / pending / terminal / recovery / memory recall 边界。
- 不改业务代码或只加 guard tests。

### PR D: Compaction Runtime V1

- 实现 compaction artifact。
- 实现 rehydration planner。
- 与 transcript builder / memory hooks / plan state 对齐。
- Tests: projection no-write-back、pending resume、artifact survival。

### PR E: Long Compaction Dogfood

- real LLM long dogfood。
- 强制跨 compaction 边界。
- 覆盖 plan + terminal + memory + artifact + recovery。

### PR F: Capability Descriptor V1

- 为 built-in tools 生成 descriptor。
- Gate 从 name-set 逐步改为 descriptor-first。
- 保留 hardline name fallback 仅限当前 terminal hardline。
- Tests: permission/read-only/plan gate。

### PR G: Skill / CLI Adapter

- skills 和 CLI-backed capability 进入 descriptor registry。
- CLI output artifact policy。
- Inspector 显示 provenance。

### PR H: MCP Adapter V1

- MCP server lifecycle。
- MCP tool descriptor。
- Permission / approval / artifact / inspector 集成。
- mock MCP dogfood。

## 7. 不在本阶段做的事

本阶段暂不做:

- EverMemOS-like MemScene / subject clustering。
- 新一轮 memory ontology 扩张。
- 新的 permission enum。
- 大 UI。
- 把 MCP 当成绕过本地 tool executor 的旁路。
- 把 compaction summary 当作 durable memory。
- 对旧兼容路径做长期 shim。

## 8. 成功标准

这三条主线完成后，Pulsara 应达到:

1. 可解释
   - 任意一次重要 run 都能被 inspector 解释。
   - 能回答“模型为什么看到这些上下文”。

2. 可长程运行
   - auto-compaction 后 plan / terminal / artifact / memory / recovery 都连续。
   - real LLM 长 dogfood 可以跨压缩稳定完成。

3. 可扩展能力
   - built-in / skill / CLI / MCP 都进入统一 capability registry。
   - permission / approval / artifact / observability 不再分裂。

4. 可调试
   - sequence gap、outbox lag、artifact missing、recall degraded、capability denied 都能被结构化看到。

## 9. 当前最建议的第一步

先做 PR A: Inspector Read Model。

具体理由:

- 它只读，风险最低。
- 它会立刻改善开发体验。
- 它能复用现有 event log / artifact / recall trace / plan event。
- 它是 compaction 和 MCP 之前最有杠杆的地基。
- 最近遇到的 publisher deadlock、Oxigraph 502、real LLM dogfood stale harness，都说明 Pulsara 已经需要正式 observability，而不是临时 psql / grep / 推理。

PR A 不需要一次做漂亮 UI。先让这条命令成立就足够有价值:

- uv run pulsara inspect run RUN_ID --json

它输出的第一版即使粗糙，也会成为后续所有长程 dogfood 的黑匣子记录仪。

