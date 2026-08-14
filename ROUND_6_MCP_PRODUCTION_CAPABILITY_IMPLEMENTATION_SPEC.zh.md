# Pulsara Round 6：MCP Production Capability Restoration 实施规格

> 状态：**ACTIVATED — 2026-08-14（post-activation boundary review closed）**
>
> 记录日期：2026-08-13
>
> 当前编码基线：44ec551f7ae6ff4c98f1b4cdeb222d68ac94f28c（feat: activate long-horizon execution envelope）
>
> 本文是设计意图与实施落地的共同真源。Post-activation review 提出的 URI-template 线性匹配、`json.loads`前结构扫描、bounded remote identity 与从 `client.open()` 开始的 Host discovery reservation 已全部闭合；机器可读证据已按最终工作树刷新。PHC-08 后续 non-goal 不随核心 activation 隐式关闭。
>
> 上位架构：[PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md](PULSARA_DURABILITY_SUBTRACTION_REASSESSMENT.zh.md)
>
> 产品能力索引：[POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md](POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> Catalog 先行设计：[PULSARA_MCP_CATALOG_AND_LIST_FALLBACK_DESIGN.zh.md](PULSARA_MCP_CATALOG_AND_LIST_FALLBACK_DESIGN.zh.md)
>
> 前置规格：[Round 3 compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5 long-horizon envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)

---

## 0. 执行结论

Round 6 恢复的是 MCP 产品能力，不是 hard-cut 前的 MCP execution recovery state machine。

目标终局由四组既有优势组成：

1. **旧 Pulsara 的 per-server generation、完整 discovery candidate 与 Host safe-point 安装。** 模型看到的 descriptor 与实际 executor 必须来自同一代；旧代只为已经借出的 operation 排空，新调用不能重新借用旧代。
2. **Codex 的 request-scoped runtime snapshot。** 一次 model request 及其返回的整个 tool batch 持有同一个不可替换的 runtime/tool-surface borrow；manager 刷新只能影响后续 request。
3. **grok-build 的稳定 catalog 体验。** 首次 bounded announcement、late-ready/state-change reminder 与长上下文中的 catalog 恢复都不重写旧 prefix；其 compaction 后重注入只作为未来 Round 5B 的兼容契约，不是 Round 6 activation gate。
4. **根目录设计文档的克制 V1。** MCP 工具仍作为真正的 typed provider tools 直接暴露；list_mcp_servers 只提供当前 catalog/status fallback，不成为权限 authority，也不引入通用 search_tool/use_tool 转发层。

Round 6 冻结以下核心准则：

~~~text
MCP connection/catalog lifecycle
    = Host-scoped process-local authority

canonical tool request / attempt / result
    = existing PostgreSQL conversation authority

provider-visible tool semantics
    != process-local executor binding identity

physical reconnect with identical semantics
    -> install a new execution generation
    -> preserve the current provider-input semantic epoch and exact prefix

schema/name/description/exposure change
    -> install at a Host safe point
    -> open a new process-local provider-input epoch
    -> never mutate an already frozen request

Host crash
    -> process-local MCP manager/request disappears
    -> an accepted attempt without result remains outcome-unknown/interrupted
    -> reconnect afresh on the next Host
    -> never rebind the old physical request

MCP permission
    = READ_ONLY | EXTERNAL_EFFECT two-class fact
    -> provider tool exposure never varies with a run permission preset
    -> the existing local authorize seam is the only permission decision point
    -> default BYPASS_PERMISSIONS experience remains approval-free
    -> no separate MCP risk classifier, policy DSL or durable approval graph
~~~

本轮不得恢复：

- durable MCP connection/session/generation rows；
- durable pending request、continuation secret、resume receipt 或 delivery ACK graph；
- MCP reducer、checkpoint、projection、repair 或 event replay；
- 把旧 tool call 按 server name 转投给最新 client；
- 对 outcome 不确定的 tools/call 自动重试；
- 通用 search_tool/use_tool；
- 跨 Host 恢复旧 form、private URL 或 input-required 请求；
- MCP Apps、Tasks 或跨进程 MCP manager restore。

核心恢复不需要新增或修改 PostgreSQL product relation、CommittedAgentEvent、subject slot、append guard 或 durable job。现有 assistant tool request、tool_execution_attempts、tool_results、TOOL_CALL permission decision、ToolAttemptAccepted 与 ToolResultAccepted 已经能够承载 V1 canonical 产品事实。`interaction_decisions(subject_kind=MCP_INPUT)`保留为尚未激活的未来schema branch；Round 6不写入它，也不扩展其columns/constraints。

Round 6 activation 后的数量 oracle 原则上保持：

~~~text
Committed AgentEvent     34
Live AgentEvent          23
subject slots            15
append guards             2
product relations        26
durable job kinds         4
durable MCP relations     0
durable MCP jobs          0
~~~

如果编码期间发现必须改变这些数字，必须先证明出现了新的独立产品事实；连接 ready、retry、dirty、candidate、borrow、generation、drain 和 subscription ACK 都不是增加 durable vocabulary 的理由。

---

## 1. 基线、范围与设计输入

### 1.1 起草输入

~~~text
current Pulsara HEAD
44ec551f7ae6ff4c98f1b4cdeb222d68ac94f28c

PULSARA_MCP_CATALOG_AND_LIST_FALLBACK_DESIGN.zh.md
a52299169381aa722617084a8a805969a6b5349cf711441c11c8dd59f4bb7e69

POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md
a3b49a60b21a762baa27fe5b14ec8fc22b645745d775569822bffc87b1243ab6

ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md
1a996f8dda8c767043e4c84bf7d414724129dbd3d890d5cf3bb5463922cae6e6

ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md
9ee6cfca09869a67903a2164c2c2025d7c836998bd26a459336cee90658e34c2

ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md
608ecfdd8e4f20acc62c012fb39569c19c4a34f6bf981d8c96df0aa293f48832
~~~

这些 hash 只标识本文起草输入。coding agent 必须在第一个 production diff 前重新记录实际 checkpoint HEAD 与本文 SHA-256，不得假设文档审阅期间工作树没有变化。

### 1.2 四个 prior-art 代码基线

| 来源 | 基线 | 本文采用的能力 | 本文拒绝的部分 |
|---|---|---|---|
| hard-cut 前 Pulsara | 5b7ad9f7ffc8565bc572180b2bde0c81ab64473a | per-server generation、candidate、safe-point installation、schema conformance、exact binding；interaction secret boundary只作为future elicitation约束 | durable continuation/recovery、event/reducer/checkpoint/receipt graph；Round 6 V1不恢复form/URL wire |
| Codex | 6138909d6ec58b2fbe635ef973e02caecad5a5aa，本地调研日期 2026-08-13 | request-scoped McpRuntimeSnapshot/StepContext、旧 runtime 由 borrow 自然存活、deferred catalog 作为未来证据 | 不能把仅记录 list-changed 日志当作完整 reconcile；本轮不直接引入其完整 Apps/OAuth 产品面 |
| grok-build | c68e39f60462f28d9be5e683d9cbe2c57b1a5027，本地调研日期 2026-08-13 | initial catalog、late-ready reminder、safe inference boundary、compaction 后重新公告、sanitized instructions | 按 server name 取当前 client 的弱 generation binding、可能重复 ambiguous side effect 的 transport retry、V1 通用 use_tool |
| Pulsara 根目录 catalog 设计 | SHA 见 1.1 | authoritative snapshot、initial announcement、safe-point update、list_mcp_servers fallback、direct typed tools | 文档明确排除的 generic search/use、LLM summary、catalog-as-permission、cross-session manager restore |

### 1.3 当前代码真值

[代码确认] 当前 [mcp_config.py](src/pulsara_agent/mcp_config.py) 只识别 server_id 与 enabled，不构造 transport、SDK client、supervisor 或 tool executor。

[代码确认] 当前 [host.py](src/pulsara_agent/conversation_kernel/host.py) 发现任何 enabled MCP config 时直接抛出 KernelCompositionUnavailable；这使 MCP config 成为 session-open blocker，而不是渐进能力。

[代码确认] 当前 [tool_runtime.py](src/pulsara_agent/conversation_kernel/tool_runtime.py) 已有 process-local tool-surface borrow 和 authorize/invoke exact join，但 FrozenToolSpec 仍携带 executor_binding_fingerprint，并且该字段进入 provider-visible surface fingerprint。

[代码确认] 当前 [compiler.py](src/pulsara_agent/model_input/compiler.py) 在 tool surface fingerprint 变化时选择 TOOL_SURFACE_CHANGED，建立新的 process-local provider-input epoch。这对 schema 变化正确，对仅 physical connection generation 变化错误。

[代码确认] 当前 clean-v0 已经保留：

- interaction_decisions.subject_kind IN (TOOL_CALL, MCP_INPUT)；
- session_commands.RESOLVE_INTERACTION；
- process-local InteractionOpened/InteractionReplaced/InteractionClosed；
- generic tool attempt/result canonical contract；
- exact permission snapshot 与 message-before-dispatch。

现有 MCP_INPUT decision 只通过 subject_turn_id 指向整个turn，不能证明一次form/input decision属于哪个assistant tool call；当前Protocol/Go controller也不能提交typed form/private-URL secret。Round 6因此不把这条残缺branch伪装成已恢复能力：V1 elicitation固定`DISABLED`，不写MCP_INPUT decision，也不修改clean-v0 schema。未来恢复typed elicitation时，必须以exact assistant tool-call为subject并另写Protocol/secret规格；不能复用当前turn-level branch或普通字符串JSON。

### 1.4 V1 产品范围

Round 6 activation 必须覆盖：

- user/workspace MCP config 解析与 closed precedence；
- stdio 与 Streamable HTTP transport；
- initialize、协商与 bounded tools/list 完整发现；
- optional/required server startup 语义；
- retry、manual reconnect、config reload、list-changed reconcile 与 Host-policy periodic refresh；
- exact schema normalization 与 direct provider tool exposure；
- tools/call、known result、typed server error、unknown outcome；
- resources/list、resources/templates/list、resources/read、prompts/list 与 prompts/get 的typed只读产品面；
- server instructions/catalog announcement；
- stable list_mcp_servers；
- closed input-required result validation、bounded keyed state-only continuation与unsupported-method rejection；
- permission、scope、secret 与 redaction 边界；
- ROOT 与 SUBAGENT_TASK scoped exposure；
- Host close、generation drain 与 stale candidate rejection；
- Python Host/Kernel production composition；
- config list/add/remove/enable/disable/doctor/reconnect 的非 Legacy CLI 入口；
- local stdio、local HTTP 与 real-provider MCP dogfood。

Round 6 不要求高级 Go MCP dashboard。Protocol/TUI 继续使用现有generic tool confirmation与tool stream；MCP form/private-URL elicitation不在V1广告或activation范围内。typed form schema、field-value oneof、controller-bound secret submission、private URL launch/consent port与高级资源浏览器留给后续UI轮次。

### 1.5 明确 non-goals

- OAuth 登录流程；V1 支持 none、static_headers、bearer_env，遇到 oauth 配置必须 typed fail closed，不能降级为匿名连接。
- MCP form/private-URL elicitation与human-bearing input-required；V1 initialize固定广告DISABLED，server违规发送时typed拒绝，不能降级为普通文本/JSON。
- legacy SSE transport。
- MCP Apps、Tasks、Roots、Sampling 或 server logging 的完整产品面。
- generic deferred tool search。若 direct exposure 超过现有 64-tool hard bound，必须要求显式 exposure allowlist 或 typed 拒绝，不得随机截断。
- durable MCP audit transcript。MCP 工具的用户可观察事实由普通 assistant tool request、attempt、result 和 interaction decision 表达。
- skill manifest 对late-ready MCP tool的动态依赖解析；Round 6只恢复scope-filtered direct tool surface、MCP_CATALOG与list_mcp_servers，不修改skill activation/catalog authority。

### 1.6 SDK与协议contract

Round 6冻结hard-cut前已经验证过的official Python SDK public-v2 seam：

~~~text
mcp[cli]==2.0.0
mcp-types==2.0.0  # 由official SDK依赖闭合，uv.lock必须exact确认
Pulsara semantic contract: pulsara.mcp-sdk-v2.2026-07-28.v1
~~~

SDK自身可以按public negotiation API与兼容server协商，但Pulsara只接受本文closed vocabulary中的core tools/resources/prompts、exact `"complete" | "input_required"`与elicitation mode；未知experimental result type、capability或extension必须typed fail closed。production不得复制SDK的private header map、exit stack或frame state，也不得因为未来SDK出现新字段就把开放JSON直接投影成成功ToolResult。

---

## 2. 四方取舍的最终判断

### 2.1 旧 Pulsara：恢复 generation 安全，不恢复 recovery machinery

[历史代码确认] 5b7ad9f7:src/pulsara_agent/runtime/mcp/supervisor.py 把 config change、TTL refresh、retry 与 manual refresh 变成新的 per-server attempt/generation。新 client 完成完整 discovery 后才产生 installable candidate；candidate 必须 exact join 当前 config epoch、attempt 与 discovery generation。

[历史代码确认] 5b7ad9f7:src/pulsara_agent/host/session.py::_apply_mcp_safe_point 在 run/resume safe point 把 descriptor 与 executor 从同一 slot/snapshot 原子安装，旧 slot 进入 retiring。

[设计采用] 恢复：

- config epoch；
- server attempt generation；
- immutable discovery snapshot；
- installable candidate；
- stale candidate drop；
- semantic snapshot 与 execution attribution 分离；
- old generation drain；
- safe-point install。

[设计拒绝] 不恢复：

- durable WAITING_USER continuation；
- stateless cross-Host rebind；
- continuation encryption rows；
- MCP-specific reservation/account/checkpoint；
- projection-ready/receipt/reducer repair 事件。

### 2.2 Codex：一次 request 持有一个 runtime 世界

[代码确认] Codex 的 codex-rs/core/src/session/mcp_runtime.rs 与 step_context.rs 把 MCP manager 冻结到 request-scoped runtime snapshot；refresh 发布新 manager，但旧 StepContext 继续持有旧 manager。

[设计采用] Pulsara 一次 provider request 取得 ProcessLocalToolSurfaceBorrow 后，该 borrow 必须覆盖：

~~~text
preflight
-> provider open/read/close
-> complete assistant acceptance
-> response 内全部 tool authorize
-> attempt acceptance
-> physical invocation
-> known result settlement / unknown interruption
~~~

refresh 不能让上述链路中途换代。新 generation 可以发布给后续 request，但旧 borrow 必须仍能使用其 exact retiring slot，直到该 batch 完成。

### 2.3 grok-build：采用 catalog/reminder，不采用弱 execution lookup

[代码确认] grok-build 用固定 catalog 入口、initial server context 和 safe-point reminder 解决 late-ready 与长上下文遗忘；其已经实现的 compaction 路径还会重新注入 MCP server context。

[代码确认] grok-build 的部分 McpTool 调用路径按 server name 从共享 state 取当前 client，不能证明当时搜索到的 schema 与执行 client 属于同 generation。

[设计采用] Pulsara 采用：

- initial bounded announcement；
- safe-point delta/snapshot announcement；
- dirty coalescing；
- sanitized instructions；
- 为未来 Round 5B 预留“rebase 后从当前 snapshot 重新生成 announcement”的纯编译契约；Round 6 不实现 compaction，也不把该项列为 activation gate；
- catalog retrieval 不调用 LLM summary。

[设计拒绝] Pulsara 不允许：

~~~text
old advertised schema
    + lookup latest client by server name
    -> physical call
~~~

每次 physical call 必须持有旧 provider request 借出的 exact generation binding。

### 2.4 根目录设计：V1 继续直接暴露真正工具

list_mcp_servers 是 catalog observation，不是 execution indirection。它可以帮助模型回答“有哪些 server”、恢复 lost-in-the-middle 或确认 late-ready，但不能：

- 让未进入当前 tool surface 的工具变得可调用；
- 绕过 permission；
- 返回可任意重放的 opaque execution handle；
- 主动 connect/retry/enable server；
- 取代 provider-visible tool schema。

因此 Round 6 保持：

~~~text
small/bounded catalog
    -> direct typed MCP tools

catalog memory/status recovery
    -> list_mcp_servers

huge schema universe
    -> future deferred-search design
~~~

---

## 3. Rebase 必须拆成四种语义

### 3.1 Transport reconnect

物理 stdio/HTTP 连接失效后建立新连接。它只能产生新的 connection generation，不能原地修改旧 client 对象的身份。

### 3.2 Execution generation replacement

新 client 完成 initialize 和完整 discovery 后替换当前可借用的 executor generation。旧 generation 可以由已有 borrow 继续使用，但不能发放新 borrow。

### 3.3 Provider-visible semantic surface replacement

工具 name、description、schema、scope exposure 或 semantic descriptor version 变化时，provider tools 数组发生变化。只能在 safe point 安装，并触发 Round 3.1 TOOL_SURFACE_CHANGED，建立新的 process-local provider-input epoch。

这会合理地牺牲一次 prefix cache，因为 provider 实际可调用 action space 已经改变。

### 3.4 Cross-Host request recovery

Host 崩溃后在新 Host 恢复旧 SDK request、requestState、form response 或 connection generation。Round 6 明确不支持。

“不恢复 3.4”不等于禁止 1–3。正确不变量是：

> 允许同 Host 在 safe point 重连、换代和更新 catalog；禁止修改 frozen request、禁止 in-flight 调用漂移、禁止跨 Host 恢复 old physical request。

---

## 4. 目标物理架构

~~~text
KernelHostSession
  |
  +-- McpHostSupervisor                         process-local owner
  |     |
  |     +-- config epoch
  |     +-- per-server attempt state
  |     +-- current + retiring connection slots
  |     +-- discovery candidates
  |     +-- retry / Host-policy refresh / subscription dirty tasks
  |
  +-- McpSafePointInstaller                     only installation owner
  |     |
  |     +-- current McpCatalogSnapshot
  |     +-- current semantic tool installation
  |     +-- current execution-generation bindings
  |
  +-- MultiGenerationToolSurfaceOwner
  |     |
  |     +-- current surface generation
  |     +-- retiring generations with borrow counts
  |     +-- ROOT / SUBAGENT_TASK scoped snapshots
  |
  +-- KernelContextSourceCollector
  |     +-- MCP_CATALOG typed runtime observation
  |
  +-- DirectKernelToolPort
  |     +-- stable list_mcp_servers
  |     +-- exact MCP tool adapters
  |
  +-- KernelInteractionArbiter
        +-- one Host-wide visible TOOL_CONFIRMATION slot
        +-- bounded FIFO of dormant ordinary/MCP confirmation candidates
        +-- existing TOOL_CALL decision + attempt transaction
~~~

PostgreSQL 只继续保存：

~~~text
assistant tool request
tool execution attempt
optional remote identity
known tool result
human/machine interaction decision
turn completed/interrupted
~~~

PostgreSQL 不保存：

~~~text
MCP client/session
connection generation
discovery candidate
subscription cursor
retry timer
borrow/refcount
pending SDK request
human-bearing MCP inputRequests
requestState beyond the current bounded state-only attempt
announcement delivery receipt
~~~

---

## 5. R6-0：Provider-visible tool identity 与 executor identity 分层

这是 Round 6 第一个 production slice。MCP supervisor 开始编码前必须先完成本节；它就是“仅物理连接重连不破坏 prefix”的小修复规格。

### 5.1 当前问题

当前 FrozenToolSpec 同时包含：

~~~text
name
description
parameters
descriptor_fingerprint
executor_binding_fingerprint
~~~

model_tool_surface_fingerprint() 又把五项全部 hash。因此 semantic 完全相同的 MCP server 重连，只要 executor binding 变了，就会触发：

~~~text
TOOL_SURFACE_CHANGED
-> process-local epoch reset
-> system/tools/history 重新发送
-> provider prefix cache 无意义失效
~~~

只从 model_tool_surface_fingerprint() 漏掉一个字段仍不闭合。compatible append 会继续携带 predecessor 中的旧 FrozenToolSpec.executor_binding_fingerprint，DirectModel 又要求 compiled tools 与当前 prepared surface 完全相等，最终不是错误 reset，就是把旧 binding 带入新 execution。

### 5.2 冻结后的 closed DTO

~~~text
FrozenProviderToolSpec
  name
  description
  parameters: FrozenJsonObjectFact
  descriptor_fingerprint

FrozenProviderToolSurface
  conversation_scope_kind
  tool_specs: tuple[FrozenProviderToolSpec, ...]
  semantic_surface_fingerprint

PreparedToolExecutionBinding
  tool_name
  descriptor_fingerprint
  executor_binding_fingerprint
  execution_policy:
    BuiltinExecutionPolicyRef
    | McpToolExecutionPolicyFact

PreparedKernelToolSurface
  model_surface: FrozenProviderToolSurface
  execution_bindings: tuple[PreparedToolExecutionBinding, ...]
  execution_surface_fingerprint
  access: ProcessLocalToolSurfaceAccess
~~~

实现可以保留 FrozenToolSpec 类名以减少机械修改，但该类在 R6-0 后不得再包含 executor identity。语义必须以字段而非旧类名为准。

`BuiltinExecutionPolicyRef` 只是对现有 closed builtin catalog entry fingerprint 的引用，不建立新的 builtin policy vocabulary；`McpToolExecutionPolicyFact` 才是 dynamic MCP adapter 提供的最小两类policy。二者由closed union分派，不能先按tool name查询builtin catalog、失败后再fallback。

### 5.3 两类 fingerprint 覆盖范围

~~~text
semantic_surface_fingerprint = H(
    scope,
    ordered(name, description, parameters, descriptor_fingerprint),
)

execution_surface_fingerprint = H(
    host owner epoch,
    tool-surface generation,
    semantic_surface_fingerprint,
    ordered(
      tool_name,
      descriptor_fingerprint,
      executor_binding_fingerprint,
      execution_policy fingerprint,
    ),
)
~~~

semantic_surface_fingerprint 可以进入：

- ModelInputCompileBinding；
- ContextCompileBudgetReport；
- ProviderInputEpochCompatibility；
- provider-input semantic prefix fingerprint；
- semantic compiler golden vectors。

execution_surface_fingerprint 只能进入：

- PreparedKernelModelCall 的 process-local preparation identity；
- PreparedKernelModelExecution.execution_fingerprint；
- one-shot install permit exact join；
- ProcessLocalToolSurfaceBorrow；
- authorize/attempt/invoke 前的 exact validation；
- bounded operational diagnostics。

它不得进入 provider wire input、canonical rows、CommittedAgentEvent、context-source semantic identity 或 provider-prefix fingerprint。

### 5.4 Multi-generation borrow

当前“只要存在任何 borrow 就禁止 surface 变化”的 single-generation owner 必须升级成 copy-on-write generation registry：

~~~text
ToolSurfaceGenerationRegistry
  current_generation
  generations[generation_id]
    semantic surface
    execution bindings
    exact slot leases: tuple[McpSlotLease, ...]
    opaque authority
    accepting_new_borrows
    active_borrow_count
    retiring
~~~

安装新 generation 时：

1. 新 generation 成为 current 并允许新 borrow；
2. 旧 generation 原子切为 retiring 且禁止新 borrow；
3. 已经签发的旧 borrow 继续 exact join 旧 generation；
4. 新 generation 发布前，必须对其引用的每个 exact slot 去重并取得 `McpSlotLease`；generation 不直接拥有 client，也不具有 close slot 的能力；
5. 旧 borrow 归零后，registry 删除该 process-local generation并释放其 exact slot lease tuple；
6. `McpHostSupervisor` 是唯一 physical close owner。只有 slot 已停止接纳新 lease、所有 generation lease 已释放且所有 admitted operation 已 join，supervisor 才能关闭该 exact slot；
7. Host close 拒绝新 borrow/lease，等待所有 generation borrow 与 slot operation drain，再由 supervisor close。

这同时吸收旧 Pulsara generation lease 和 Codex Arc-held old runtime 的优势。ROOT 的 refresh 不能因为一个长运行 child 仍持有旧 borrow 而无限阻塞；child 继续完成其旧 batch，ROOT 下一次 request 可以借新 generation。若 `G1={A1,B1}`、A 重连后 `G2={A2,B1}`，G1 退休只释放 A1/B1 lease，绝不能关闭仍被 G2 lease 的 B1。generation registry 管 provider/request 世界，supervisor slot 管 physical lifetime；两者不重复表达 close authority。

### 5.5 DirectModel exact join

preflight 必须验证：

~~~text
compiled semantic tools
    == prepared model_surface semantic tools

current execution borrow
    exact-joins prepared execution_surface_fingerprint

for each compiled tool name
    execution binding descriptor fingerprint
    == semantic tool descriptor fingerprint
~~~

provider transport 只收到 semantic tools thaw 后的新 ToolSpec。physical executor identity 永远不序列化给 provider。

### 5.6 Prefix 行为矩阵

| 变化 | semantic fingerprint | execution fingerprint | Round 3.1 行为 |
|---|---|---|---|
| HTTP/stdio reconnect，schema 完全相同 | 不变 | 变化 | 同 epoch，保持 exact prefix |
| auth token 轮换，server 语义不变 | 不变 | 变化 | 同 epoch，保持 exact prefix |
| client object 重建，listing 语义相同 | 不变 | 变化 | 同 epoch，保持 exact prefix |
| tool description 变化 | 变化 | 变化 | safe-point 新 epoch |
| input schema 变化 | 变化 | 变化 | safe-point 新 epoch |
| tool 增加/删除/rename | 变化 | 变化 | safe-point 新 epoch |
| scope exposure 变化 | 对相应 scope 变化 | 变化 | 只重建受影响 scope epoch |
| server status/instructions 变化，但 direct tools 不变 | tool fingerprint 不变 | 可变或不变 | schema prefix 不重建；追加 MCP_CATALOG observation |

### 5.7 R6-0 architecture guards

- model_input 不得出现名为 executor_binding_fingerprint 的 provider semantic DTO 字段；
- model_tool_surface_fingerprint 的 payload 不得包含 execution identity；
- FrozenCompiledModelInput.tools 必须是 provider semantic DTO；
- tool authorize/invoke 仍必须要求 opaque Host authority 与 execution generation；
- foreign Host、foreign generation、same-shape 伪造对象在 physical open 前失败；
- semantic-identical physical rebind 不得产生 TOOL_SURFACE_CHANGED；
- semantic change 必须产生 TOOL_SURFACE_CHANGED；
- compiler contract/domain separator 按需升级为新版本并提供 fixed golden vector。

---

## 6. MCP 配置与安全输入

### 6.1 Closed config

当前 neutral detector 扩展为 typed config parser，但配置对象不得直接携带可打印 secret 值：

~~~text
McpServerConfig
  server_id
  display_name
  enabled
  required
  transport:
    StdioTransportConfig
    | StreamableHttpTransportConfig
  auth:
    NoAuth
    | StaticHeaderEnvironmentRefs
    | BearerEnvironmentRef
    | UnsupportedOAuth
  startup_policy
  exposure_policy
  scope_policy
  effect_policy
  supports_parallel_tool_calls
  stateless_http_max_in_flight
  catalog_refresh_interval_ms
  default_tool_timeout_ms
  per_tool_timeout_ms
  semantic_config_fingerprint
  runtime_config_fingerprint
  resolved_config_identity
~~~

semantic_config_fingerprint 只覆盖会改变 server/capability 语义的非 secret 事实；runtime_config_fingerprint 绑定transport endpoint、command、secret-generation commitment、refresh/concurrency/timeout等physical policy。raw secret 不得进入任一普通 fingerprint、repr、diagnostic 或 event。

~~~text
resolved_config_identity = H(
  "pulsara:mcp-resolved-config:v1",
  server_id,
  semantic_config_fingerprint,
  runtime_config_fingerprint,
)
~~~

candidate必须同时携带并exact compare两个fingerprint以及resolved identity；不得用含义不明的单一`expected_config_fingerprint`让实现者临场决定比较哪一层。

数值config在parse时闭合：

| 字段 | 默认 | 合法范围 | 组合规则 |
|---|---:|---:|---|
| `default_tool_timeout_ms` | 600000 | 1000..600000 | `effective_timeout=min(configured, Round5A NONTERMINAL_TOOL_INVOCATION hard bound)` |
| `per_tool_timeout_ms[name]` | 继承server默认 | 1000..600000 | exact tool override；最多512项且key必须属于完整listing |
| `catalog_refresh_interval_ms` | 300000 | `DISABLED` 或 30000..86400000 | 只驱动Host periodic refresh；不覆盖listChanged fence |
| `stateless_http_max_in_flight` | 4 | 1..16 | 仅proved-stateless HTTP；同时服从Host aggregate limit |

Round 5A的600秒是foreground logical watchdog上界，不证明线程/remote request被物理kill。MCP adapter还必须提供transport cancel/close/join；late exact result与unknown outcome继续遵守Round 5A。零、负数、NaN、范围外值与未知per-tool key在candidate发布前typed拒绝，不能静默clamp。effective timeout进入runtime/policy fingerprint，不进入provider-visible descriptor。

### 6.2 Merge precedence

~~~text
user config
    < explicitly trusted workspace config
    < explicit Host open overrides
~~~

同 server_id 按 whole-entry replacement，不做字段级深合并，避免 user auth 与 workspace endpoint 被意外拼成新 authority。解析后按 server_id 排序。workspace 文件属于 repository-owned input，普通 Host open 默认不信任：它不能覆盖 user entry，workspace-only entry会保留为可诊断但强制`enabled=false`的配置。只有当前 Host open 显式传入`--trust-workspace-mcp`，或用户进入显式 workspace MCP 管理命令时，workspace entry 才能成为active physical配置；仅checkout一个仓库永远不能自动启动command或解封header/bearer secret reference。

配置物理真源继续使用当前 neutral detector 已冻结的两个路径：

~~~text
user config       ~/.pulsara/mcp.yaml
workspace config  <workspace-root>/.pulsara/mcp.yaml
Host open override  process-local only
~~~

`pulsara mcp add` 默认写 user config；只有显式 `--workspace` 才写 workspace config。`mcp list/doctor/add/enable/disable --workspace`本身属于显式管理行为，但随后普通`host run/repl/tui`仍需`--trust-workspace-mcp`才激活repository-owned配置。Host open override 不回写文件。配置文件只保存用户明确填写的 server/tool effect override，不保存 discovery 后自动推导出的每个 tool classification，也不保存 connection generation、candidate、slot 或 approval decision。

连接 server 时不要求用户逐个给 tool 选择 effect kind。默认 classification 在每次完整 discovery candidate 中确定；用户只有在 server annotation 不准确或希望采用更严格分类时，才需要编辑 config 或使用后续 CLI override。

### 6.3 最小 MCP effect policy

Round 6 不建立 Codex 式完整 MCP approval subsystem，也不恢复旧 Pulsara 的多轴 risk vocabulary。MCP physical execution 只冻结两类：

~~~text
McpEffectKind
  READ_ONLY
  EXTERNAL_EFFECT

McpEffectPolicyConfig
  default_effect: AUTO | READ_ONLY | EXTERNAL_EFFECT
  tool_effect_overrides: map[remote_tool_name, READ_ONLY | EXTERNAL_EFFECT]

McpToolExecutionPolicyFact
  server_id
  remote_tool_name
  tool_semantic_fingerprint
  effect_kind
  timeout_ms
  parallel_safe
  classification_source: TOOL_OVERRIDE | SERVER_OVERRIDE | SERVER_ANNOTATIONS
  policy_fingerprint
~~~

effective `McpEffectKind` 的唯一 owner 是 discovery candidate factory，优先级为：

~~~text
exact tool override
-> server default override
-> conformed server annotations
~~~

annotation 自动推导规则只有一条：

~~~text
readOnlyHint == true
and destructiveHint != true
and openWorldHint != true
    -> READ_ONLY

otherwise
    -> EXTERNAL_EFFECT
~~~

用户主动配置一个 MCP server 即表示信任该 server 提供其公开 descriptor/annotation；Round 6 不做零信任语义审计或 LLM risk classification。矛盾或缺失 annotation 统一落入 `EXTERNAL_EFFECT`。`destructiveHint/openWorldHint` 只把 classification 提升到 `EXTERNAL_EFFECT`，不再形成额外 effect class；`idempotentHint`在V1不参与effect classification，也绝不授予自动retry。

自动推导结果不写 PostgreSQL，也不展开写回 YAML。它与 exact tool semantic fact 一起冻结在当前 process-local execution generation 中，并进入 `policy_fingerprint`。显式 override 才持久化在上述 user/workspace YAML 中。physical reconnect 后相同 tool semantic fact 与相同 policy 必须得到相同 `policy_fingerprint`；配置或 annotation 真正变化才安装新 generation。

最小持久化形状示例：

~~~yaml
servers:
  github:
    enabled: true
    # transport/auth/exposure fields omitted
    effect_policy:
      default_effect: auto
      tool_effect_overrides:
        get_issue: read_only
        delete_repository: external_effect
~~~

省略 `effect_policy` 等价于 `default_effect: auto`。这里的 key 是 remote tool name，不是 provider mangling 后的名字；candidate factory 在同一 discovery snapshot 内解析并拒绝未知 override key，避免拼写错误静默失效。

`parallel_safe` 不从 read-only 自动推导：只有 server config 显式 `supports_parallel_tool_calls=true` 才为真。timeout 来自 per-tool override 或 server default。effect classification 不提供自动 retry 能力。

V1 不增加：per-argument risk scoring、LLM permission classifier、Guardian reviewer、durable approval receipt、persistent “always allow” database、MCP policy rule language或新的 permission event。

### 6.4 Transport

V1 支持：

- stdio：argument vector，不经 shell；cwd 必须在 workspace policy 内；child environment 使用 default-deny allowlist，只把显式配置的非 secret 值和 sealed secret refs 注入 fresh process；
- Streamable HTTP：HTTPS 默认；localhost 可以显式允许 HTTP；redirect、proxy 和 private network 规则必须由 typed network policy 决定；`PUBLIC_ONLY`必须把一次验证通过的exact IP pin到实际connection，同时保留原始Host header与TLS SNI，禁止validation与connect分别解析造成DNS rebinding窗口；
- static header 与 bearer 值只在 fresh wire request builder 处解封；
- server log、stderr 与 transport exception 经过 redaction 和长度上限。

V1 不支持 legacy SSE 与隐式全环境继承。

official SDK 的默认 stdio/HTTP helper 在完成 JSON parse 前不能证明内存有界，因此“SDK facade bounded”不能只限制 parse 后的 page/item 数。Round 6 必须在 SDK model/session 层之前安装 Pulsara-owned public framing seam；不得 monkeypatch SDK private field：

~~~text
McpWireBounds
  maximum_stdio_frame_bytes                 16 MiB
  maximum_http_json_body_bytes              16 MiB
  maximum_sse_event_data_bytes              16 MiB
  maximum_buffered_transport_bytes_per_slot 32 MiB
  maximum_wire_json_nodes                    65536
  maximum_wire_json_depth                      128
  maximum_schema_utf8_bytes                256 KiB
  maximum_schema_nodes                        4096
  maximum_schema_depth                          64
  maximum_discovery_candidate_bytes_per_server 32 MiB
  maximum_discovery_candidate_bytes_per_host  128 MiB
~~~

- stdio adapter 必须以 bounded byte buffer 找到一条完整 frame，再把该 frame交给 public SDK message/session API；超限时立即终止 exact slot并 join process group；
- HTTP adapter 必须在 `aread()`/JSON decode 前流式计数 body；SSE decoder 必须逐块计数当前 event-data，不能先无限累积；并发request/listener必须共享一个slot-owned 32 MiB byte reservation，不能把16 MiB上限按并发数重复获得；
- stdio EOF必须形成exact slot transport failure并推进future reconnect，不能把reader正常返回误报为READY；stderr被立即丢弃，其bound计算resident chunk而非生命周期累计吞吐；
- bounded JSON decoder必须用能识别quoted string、escape、number、object key和container depth的线性grammar scanner，在`json.loads`及SDK/Pydantic object graph分配前执行wire-level node/depth budget；schema subobject再执行更窄的byte/node/depth bound，且都发生在conformance/normalization前；
- Host aggregate discovery reservation必须在`client.open()`之前取得，因为open已经执行initialize/discover；reservation一直持有到listing normalization、candidate physical quote和pending installation完成；
- 任一 frame/body/event/schema/aggregate bound 超限，都丢弃整个 candidate或终止 exact operation，不能安装 partial snapshot，也不能把截断 JSON lower 为 known success；
- 若 pinned SDK 没有能注入上述 bound 的 public transport seam，implementation 必须提供小型 Pulsara-owned transport/framing adapter并继续复用 official SDK 的 public session与typed model；不得以“SDK内部最终会parse”为由豁免 pre-parse bound。

### 6.5 Exposure policy

~~~text
McpExposurePolicy
  include_tool_names: tuple[str, ...] | ALL
  exclude_tool_names: tuple[str, ...]
  invalid_tool_policy: FAIL_SERVER | OMIT_INVALID

McpScopePolicy
  ROOT_ONLY
  ROOT_AND_SUBAGENTS
~~~

默认 ROOT_ONLY。把 MCP capability 暴露给 child 必须显式配置，且 child 仍继承 parent run permission snapshot。

provider surface 继续受当前 maximum_tool_specs = 64 约束。若 builtins 加显式选中的 MCP tools 超过 64：

- 不得按发现顺序随机截断；
- 不得让 list_mcp_servers 返回不可执行 handle 绕过限制；
- provider open 必须 typed fail closed，说明需要配置 allowlist；
- future deferred-search 设计才能改变该结论。

---

## 7. Process-local MCP supervisor

### 7.1 Server 状态

~~~text
DISABLED
CONNECTING
DISCOVERING
READY
FAILED_RETRYABLE
FAILED_TERMINAL
RETIRING
CLOSED
~~~

状态是 process-local operational truth。它可以进入当前 catalog snapshot 和 redacted diagnostics，但不进入 durable journal。

### 7.2 Connection slot

~~~text
McpConnectionSlot
  slot_id
  server_id
  supervisor_epoch
  connection_generation
  runtime_config_fingerprint
  protocol_binding
  client/session owner
  discovered_snapshot
  execution_binding_fingerprint
  physical_concurrency_mode
  dispatch_fence_state
  accepting_new_leases
  active_slot_lease_count
  active_operation_count
  close_state
~~~

slot_id、generation 和 client 对象只存在于当前 Host。`McpSlotLease` 是 sealed、opaque、process-local capability，只能由 supervisor 签发和释放；它不能被 generation 复制成同形 DTO，也不能进入 provider input、canonical row、event、hook metadata或activation evidence。

~~~text
McpSlotLease
  slot_id
  connection_generation
  admitted_discovery_generation
  admitted_execution_binding_fingerprint
  opaque lease authority
~~~

每个product operation permit必须exact join这四项与slot当前admission revision。listChanged会永久fence该slot上旧discovery generation的**新**operation；reconcile即使复用同一physical HTTP session，也要为新discovery generation签发新lease，不能重新激活旧lease。fence前已取得的permit仍可drain。相反，仅其他server换代时，未变化server的exact lease仍可被新旧runtime generation共享。

supervisor 是 slot/client/process 的唯一 physical owner。Tool-surface generation 只能持有 exact `McpSlotLease`，不能调用 client close。slot 只有在停止接纳新 lease、`active_slot_lease_count == 0`、`active_operation_count == 0` 且 reader/listener 已 join 后才能进入 CLOSED。

### 7.3 唯一 physical concurrency mode

~~~text
McpPhysicalConcurrencyMode
  SERIAL_SESSION
  | BOUNDED_STATELESS_HTTP(max_in_flight)
~~~

选择规则是 closed 的：

- stdio 永远使用 `SERIAL_SESSION`；
- 协商后持有 session identity/state 的 Streamable HTTP 使用 `SERIAL_SESSION`；
- 只有能够证明每次 request 不共享 protocol session state 的 stateless HTTP 才能使用 `BOUNDED_STATELESS_HTTP`，并受 `stateless_http_max_in_flight` 与 Host aggregate bound 双重约束；
- 无法证明 stateless 时一律降为 `SERIAL_SESSION`，不能从 tool 的 `readOnlyHint` 或 server 自称 parallel 推导 physical mode；
- tool policy 的 `parallel_safe` 只是 tool-level admission 条件；实际并发必须同时满足 tool policy 与 slot physical mode。

同一个 slot 的 outbound initialize/discovery/list、tools/call、resources/read、prompts/get、subscription request 与 input-required continuation send 共享该 slot-owned operation lane。`SERIAL_SESSION` 在任意时刻最多一个 `McpPhysicalOperationAttempt`；bounded stateless mode使用同一个 semaphore，禁止不同 adapter各建一把锁。input-required response是原attempt持有同一permit的continuation leg，不能递归获取第二个lane permit，否则sessionful server会自锁；round完成/取消后才释放原permit。

被动 transport receive loop/notification parser不取得 outbound operation lane，否则 sessionful request等待 response时会与 notification自锁。它只能把 bounded typed notification投入slot owner；notification handler不得在receive callback内递归发起 discovery或product operation。

### 7.4 Discovery snapshot

~~~text
McpDiscoverySnapshot
  server semantic identity
  negotiated protocol/capabilities
  sanitized bounded instructions
  ordered conformed tool semantic facts
  ordered resource, resource-template and prompt semantic facts
  tool_surface_semantic_fingerprint
  catalog_semantic_fingerprint
  presentation_fingerprint
  sdk_conformance_contract_fingerprint
~~~

tool semantic fact 至少覆盖：

~~~text
server_id
remote_tool_name
provider_tool_name
description
frozen input schema
optional output schema
schema dialect
descriptor fingerprint
scope policy
~~~

同一 candidate factory 必须为每个 tool 同时构造第6.3节的 `McpToolExecutionPolicyFact`。semantic fact 决定模型看见的 name/description/schema；execution policy fact 决定当前 run 是否允许 physical dispatch。二者 exact join 同一个 tool semantic fingerprint，但 policy 不得伪装成 provider tool schema，也不得把 connection identity带入provider semantic fingerprint。

schema 必须来自 official SDK 完成 conformance 后的 listing。Pulsara 不得把缺失 type: object 的 schema 自动修补成合法工具，不得追随 external $ref，不得读取 SDK private frame/attribute 拼装 authority。

resource、resource-template与prompt semantic fact只保存公开的URI/name/description、MIME/argument schema与semantic fingerprint；resource body与prompt/get result不进入discovery snapshot。它们是每次operation的普通tool result，继续使用现有artifact与known/unknown outcome边界。

三个派生 fingerprint 不得混用：

- `tool_surface_semantic_fingerprint` 只覆盖 scope-filtered direct tool name/description/schema/descriptor facts；它决定 provider tools 与 Round 3.1 semantic epoch；
- `catalog_semantic_fingerprint` 覆盖模型在 MCP_CATALOG/list_mcp_servers 中看见的 bounded server status、instructions、counts、resource/prompt overview；它决定 append-only catalog observation；
- `presentation_fingerprint` 只覆盖 Inspector/TUI presentation，不得触发 provider tool rebase。

status/instructions/catalog-only change不得改变tool-surface fingerprint；tool schema change可以同时改变tool与catalog fingerprint。

### 7.5 Candidate

网络 I/O、initialize 与 pagination 全部在 supervisor lock 外完成。完成后构造 immutable candidate：

~~~text
McpInstallationCandidate
  candidate_id
  server_id
  expected_supervisor_epoch
  expected_semantic_config_fingerprint
  expected_runtime_config_fingerprint
  expected_resolved_config_identity
  attempt_generation
  exact slot lease
  discovery_snapshot
  ordered_tool_execution_policies
  candidate_fingerprint
~~~

candidate fingerprint覆盖expected owner、两个config fingerprints、resolved-config identity、attempt identity、exact slot-lease identity、完整discovery snapshot的tool/catalog/conformance fingerprints与ordered execution-policy fingerprints；不覆盖client对象、server cache hint或presentation-only fields。presentation fingerprint作为同一candidate内独立字段校验，不能改变provider semantic identity。

candidate 入队前必须重验：

- Host 仍 open；
- server 仍 enabled；
- semantic/runtime config fingerprints与resolved identity均未变化；
- attempt 仍是当前 generation；
- slot lease 与 snapshot exact join；
- candidate 尚未被替代。

失败 candidate 只释放自己的exact slot lease；它没有close能力。supervisor按统一lease/operation/close-state规则决定该slot是否可以关闭，不能撤销仍被current或其他generation引用的slot。

failure settlement必须按object identity同时检查`pending`、`installed`与current slot carrier；同一server的新pending slot失败时不得弹出健康旧installed generation。exact slot完成physical close后从active slot registry删除，不保留client/process重对象作为历史。

### 7.6 Startup 语义

- optional server：Host 最多提供 3 秒 initial fast-start window；未 ready 则以 CONNECTING 进入首次 catalog，连接在后台继续；
- required server：首次 provider dispatch 前等待最多一个 closed required-startup attempt，production 默认 120 秒；失败或超时不打开 provider；
- optional failure 不阻止 Host open；required failure typed fail closed；
- 每次 connect/discovery attempt 有独立 watchdog，不是 session 或 turn 总 deadline。

### 7.7 Retry 与 refresh

允许自动 retry：

- initialize/connect 尚未发出 tool side effect；
- tools/list/catalog discovery；
- subscription listener 恢复；
- idle connection health check。

retry 使用有界指数 backoff 并加 jitter；pending timer 可在 Host close 时丢弃。它不承诺跨 Host 完成，因此不是 durable job。

不允许自动 retry：

- 已经提交 attempt 且可能到达 server 的 tools/call；
- input-required continuation 的 client-input leg；
- outcome 未知的 effectful operation。

### 7.8 list-changed dispatch fence 与 Host refresh

`notifications/tools/list_changed`、resource/prompt equivalent notification 只能在 slot state lock 内执行：

~~~text
advance dirty_generation
change dispatch_fence_state to DIRTY_FENCED
coalesce wake
schedule full relist
~~~

它不能直接修改 installed snapshot，也不能在 notification callback 内发起 relist。dirty linearization point前已经签发的`McpDispatchAdmissionPermit`（包括正在等待human confirmation者）和已经取得lane permit的physical operation都属于已admit集合，可以drain；dirty之后不得签发新的dispatch admission。旧 provider response若尚未取得admission permit，随后返回的tool call必须得到typed `MCP_SNAPSHOT_STALE` no-attempt result，不能按旧descriptor调用已经变化的server。只有full relist完成、candidate在safe point安装且slot与新snapshot exact join后，才为**新discovery-generation lease**打开product dispatch admission；旧lease只允许消费其dirty前permits，不再签发新permit，直到runtime generation释放。Round 6不允许未admit的“listChanged后再stale-use一次”。

dirty期间唯一允许进入operation lane的是reconcile/discovery与close/cancel控制；它们仍服从第7.3节physical mode。list-changed storm只推进generation并coalesce为一个当前reconcile，过期relist结果按candidate stale规则丢弃。

semantic-unchanged refresh：

- 可以只更新 freshness/operational facts；
- 不更换 provider semantic surface；
- 如果 physical client 换代，则只更换 execution generation；
- replacement attempt处于CONNECTING/DISCOVERING或失败时，只要旧installed generation仍健康，catalog继续呈现该installed generation的READY语义；replacement operational state不得制造虚假CONNECTING→READY source变化；
- 不触发 prefix reset。

semantic-changed refresh 必须走新 candidate 和 safe-point semantic rebase。

V1明确忽略server提供的`ttl_ms`、`cache_scope`及SDK为缺失字段填入的默认值：它们不进入candidate、fingerprint、freshness或dispatch authority，也不做跨页merge。periodic refresh只由Host的`catalog_refresh_interval_ms` policy驱动；explicit list-changed始终走上述立即fence。未来若采用server cache hint，必须另行冻结wire presence与跨页一致性，不能悄悄改变本契约。

---

## 8. Safe-point installation

### 8.1 唯一 owner

只有 McpSafePointInstaller 可以把 candidate 变为 current tool/catalog surface。supervisor worker、subscription callback、CLI watcher、hook 和 tool executor 都不能自行发布 surface。

### 8.2 Safe point 定义

合法安装边界：

- first provider dispatch 准备前；
- 每次 _prepare_provider_dispatch() 冻结 tool surface 与 context sources 之前；
- 上一个 provider response 产生的全部 tool batch 已经 settle，旧 borrow 可以继续存在于其他并发 scope，但本 scope 不持有 active dispatch borrow；
- 未来 Round 5B compaction adoption 后第一次 provider dispatch 前（dormant兼容边界，不是Round 6实现项）。

非法安装边界：

- provider stream 进行中；
- assistant message 尚未 canonical FULL；
- 当前 response 的 tool batch 执行中；
- interaction resolution transaction 中；
- Host close admission 关闭后。

### 8.3 Atomic process-local installation

一次 safe-point install 必须在一个 process-local 临界区内同时决定：

~~~text
current server slots
current scope-filtered provider semantic tools
current execution bindings
exact slot-lease tuple for the new runtime generation
current catalog snapshot
current MCP_CATALOG source fact
retiring tool-surface generations
~~~

不得出现：

- announcement 说 server READY，但 tool surface 仍是旧代；
- provider 看到新 schema，但 executor 仍指旧 client；
- list 返回新 generation，而 authorize 只能借旧 generation；
- candidate 安装一半后抛错留下混合 surface。

### 8.4 Lock order

~~~text
Host safe-point/admission lock
  -> MCP installer lock
  -> MCP supervisor state lock
  -> tool-surface registry lock
  -> continuity planning freeze
~~~

任何网络、SDK、PostgreSQL 或 process join I/O 都不得持有上述组合锁。candidate 的昂贵工作必须在进入 safe point 前完成。

### 8.5 Config 变化矩阵

| 变化 | 当前 slot | 新 candidate | 新 borrow | 旧 borrow |
|---|---|---|---|---|
| same config reconnect | 保留至 candidate ready | semantic 相同/physical 新代 | candidate 安装后借新代 | 排空旧代 |
| config endpoint/auth 变化 | 旧代 retiring | 按新 runtime fingerprint 发现 | 只借新代 | 已 admit batch 可排空 |
| effect override/annotation 变化 | 旧代保留至safe point | 重算exact policy fact | 新request使用新policy generation | 已admit batch沿用冻结旧policy |
| disable/remove | 立即停止发放新 borrow | 不创建 | 不允许 | ordinary batch bounded drain；pending human interaction 取消 |
| list semantic 变化 | 旧代 retiring | semantic 新代 | safe point 后借新代 | 已 admit batch 排空 |
| Host periodic refresh 失败但旧 slot 健康且未收到listChanged | 继续 current 并标记 degraded | 丢弃失败 candidate | 仍可借旧 current | 不受影响 |
| explicit listChanged 后 reconcile 失败 | exact slot保持DIRTY_FENCED | 丢弃失败 candidate并有界重试 | 不发放新product operation | fence前已admit operation排空 |
| current connection 确认死亡 | 停止新 borrow | retry 中 | 暂无该 server 工具 | 已发出调用按 known/unknown 收口 |

---

## 9. Direct tool exposure 与执行

### 9.1 Provider tool name

MCP provider-facing name 必须由 central deterministic mangler 生成，例如：

~~~text
mcp__<server-slug>__<tool-slug>
~~~

规则必须冻结：UTF-8 输入、ASCII 输出、长度上限、reserved builtin 冲突、normalization collision 与 hash suffix。相同 semantic server/tool 跨 reconnect 保持同名；collision 不能因发现顺序改变名称。

mapping 属于 discovery snapshot，并且 executor 只能从 exact snapshot lookup remote name。不得在调用时重新按字符串解析并查询全局 latest server。

### 9.2 Descriptor 安装

每个 MCP descriptor 由同一 snapshot 产生：

~~~text
FrozenProviderToolSpec
    <- McpToolSemanticFact

PreparedToolExecutionBinding
    <- same McpToolSemanticFact
     + exact McpToolExecutionPolicyFact
     + exact McpSlotLease from the runtime generation
~~~

invalid schema、duplicate name、unsupported dialect 或不合法 annotation 必须在 candidate 阶段处理，不得等模型调用后才发现。

### 9.3 Authorization 顺序

~~~text
model returns tool call
-> exact advertised semantic descriptor lookup
-> exact generation-bound McpToolExecutionPolicyFact lookup
-> local authorize once using the frozen run permission snapshot
-> if confirmation is required, enter the Host-wide arbiter as a dormant candidate
-> immediately (no-confirmation) or when candidate becomes FIFO head (confirmation),
   acquire generation-bound McpDispatchAdmissionPermit
   (reject if dirty/revoked/closed; does not occupy outbound lane)
-> confirmation path publishes the one visible prompt only after permit acquisition
-> DENY/cancel/timeout: commit/settle existing decision path and release admission permit
-> ALLOW: existing human decision + ToolAttemptAccepted atomic transaction
-> upgrade the same admission permit into an exact slot operation permit
   (may wait for the outbound lane; does not re-authorize or rebind generation)
-> one physical MCP call
-> prepare ordinary Round 1 tool output artifact/preview
-> atomically commit ToolResultAccepted
~~~

`McpDispatchAdmissionPermit`必须在**公开human confirmation之前**取得；它冻结session/scope/tool-call、descriptor/policy、runtime generation、slot lease与dirty generation，但不占用SERIAL_SESSION lane，也不产生physical effect。这样用户等待期间到达的listChanged只fence后续新admission，不会在ALLOW原子事务之后才制造一个“确定未调用却只有attempt”的假unknown。该permit计入slot lease/admitted-operation lifetime，直到DENY/cancel/timeout释放，或ALLOW后被同一owner一次性升级为physical operation permit。

~~~text
McpDispatchAdmissionPermit
  permit_id
  exact session/scope/turn/tool-call attribution
  descriptor_fingerprint
  policy_fingerprint
  runtime_generation_id
  exact slot_lease_identity
  admitted_dirty_generation
  opaque Host authority
  state:
    ADMITTED
    | ATTEMPT_ACCEPTED
    | LANE_ACQUIRED
    | RELEASED
~~~

只有slot owner能把`ADMITTED -> ATTEMPT_ACCEPTED -> LANE_ACQUIRED -> RELEASED`推进；DENY/cancel/timeout允许`ADMITTED -> RELEASED`。same-shape copy、不同tool call或不同generation不能消费permit。ALLOW canonical ACK unknown必须先按既有stable decision/attempt candidate exact-confirm；FULL才推进ATTEMPT_ACCEPTED，NONE才重写同一candidate，CONFLICT释放/terminalize且绝不physical call。

任何 MCP side effect 都不能发生在 ToolAttemptAccepted FULL 之前。permission、scope、generation、dirty fence、slot lease 或 binding 失败时不得创建attempt或physical dispatch。permit取得后到listChanged到达之间已经admit的operation可以在attempt FULL后drain；permit不能转移、复制、重新bind到新generation或用于第二次call。config disable/Host close取消尚未FULL的confirmation并释放permit；若decision+attempt已经FULL，则按普通已admit operation的known/unknown close矩阵收口。

### 9.4 Host-wide confirmation arbiter

当前LiveControl与interaction coordinator只有一个可见slot。Round 6不把并发MCP calls或ROOT/child confirmations直接并行公开，而是安装唯一process-local：

~~~text
KernelInteractionArbiter
  maximum_dormant_candidates = 64
  one current visible interaction
  FIFO admission_sequence
  candidates:
    ordinary tool confirmation
    | MCP tool confirmation with exact McpDispatchAdmissionPermit
~~~

- 普通builtin与MCP confirmation共用同一arbiter，不能各自维护`_pending`；
- candidate先以dormant形态入队；MCP candidate只有在成为FIFO head且成功取得dispatch admission permit后才发布LiveControl OPEN；
- 一次只公开一个current interaction；resolution/cancel/detach/timeout后CLOSE并推进下一项；
- queue满时在公开前typed capacity failure；MCP不得创建attempt，普通tool沿既有no-attempt失败；
- arbiter lock内不做PostgreSQL、network或controller I/O；stable candidate/permit settlement在lock外执行并以revision exact join结果；
- 每次canonical resolution retry安装fresh unsettled event；上一次失败用于唤醒detach/close的settlement edge不得沿用，否则close会零等待忙循环并饿死真正的retry I/O；
- Host close停止admission、取消dormant/current candidates、释放全部MCP permits并join waiter；不建立durable queue或跨Host恢复。

多个tool confirmation的UI次序只由Host-wideFIFO决定；physical stateless并发不意味着human prompt并发展示。

permission decision 只使用两类 effect fact 与现有四种 run preset：

| frozen run permission preset | `READ_ONLY` | `EXTERNAL_EFFECT` |
|---|---:|---:|
| `READ_ONLY` | allow | deny |
| `ASK_PERMISSIONS` | allow | require confirmation |
| `ACCEPT_EDITS` | allow | require confirmation |
| `BYPASS_PERMISSIONS` | allow | allow |

因此 Pulsara 默认 `BYPASS_PERMISSIONS` 下，启用 MCP 不增加确认弹窗。`ACCEPT_EDITS` 仍确认 external effect，因为接受本地 workspace edit 不等于授权邮件、工单、云资源等远程副作用。用户若希望所有 MCP 调用无提示，应显式使用既有 `BYPASS_PERMISSIONS`，而不是新增 MCP 专属 YOLO mode。

无论 effect kind 是什么，MCP 调用都继续执行 attempt-before-effect，且 ambiguous transport outcome 不自动重试。`READ_ONLY` 只影响 permission decision；它不是 idempotency、retry safety 或 physical completion proof。

### 9.5 Result lowering

MCP known result 统一 lower 为现有 KernelToolResult：

- server 成功内容 -> SUCCESS；
- server 明确 application error -> APPLICATION_ERROR；
- invalid arguments 由本地 schema/adapter 确定 -> INVALID_ARGUMENTS 且无 physical attempt，或 server 已处理时作为 known application error；
- permission 拒绝 -> PERMISSION_DENIED 且无 physical attempt；
- transport 失败且可证明 request 未写出 -> typed system failure 可以提交 known result；
- request 可能已到达 server 但无 exact response -> 不提交伪 SYSTEM_ERROR result，保留 attempt-without-result 并 interrupt。

large textual/structured result 继续使用 Round 1 artifact，MCP 不得另建 artifact relation。

SDK facade必须在任何success/application-result lowering之前执行negotiated-era result-type validation：

~~~text
McpClosedResultType
  COMPLETE = "complete"
  INPUT_REQUIRED = "input_required"

validated methods
  tools/call
  resources/read
  prompts/get
~~~

- V1只与能够协商本文SDK contract、并为上述响应显式携带该era所要求resultType字段的peer工作；
- bounded wire adapter必须保留top-level字段presence fact，再交给typed model；不能把SDK对缺失字段填入的默认值当成wire presence；
- exact `complete` 才能进入ordinary known-result lowering；exact `input_required` 只能进入第11章round owner；
- missing、unknown字符串（例如future result type）或resultType与payload形状矛盾都形成typed protocol-conformance failure，canonical success/result lowering count为0；
- validation同样适用于resources/read与prompts/get，不能只包住tools/call；
- 该seam只保存本次process-local presence/typed fact，不持久化raw envelope或开放resultType。

initialize/discover还必须冻结negotiated capability set：只在`tools`、`resources`、`prompts`对应capability存在时调用其listing方法；未广告的surface投影为空。合法tools-only、resources-only或prompts-only server不能因为另一方法返回`METHOD_NOT_FOUND`而进入FAILED。

### 9.6 Progress 与 logging

bounded server progress 可以投影为现有 ToolResultDelta 或 Operational diagnostic。它不进入 canonical result 前的 durable journal，也不能阻塞 physical call。日志必须 redact header、token、form 值、private URL query 和 raw exception body。

### 9.7 Physical retry 规则

一旦 tools/call 可能写到 server：

- reconnect 只服务未来调用；
- 不自动重新调用；
- 即使 tool 被声明 read-only，V1 也不从描述文本推导 retry safety；
- 未来若要自动 retry，必须有独立 typed idempotency/retry-safe contract，不能作为 MCP transport 默认行为。

### 9.8 Standard resource与prompt product surface

Round 6恢复一组稳定的标准typed入口。代码证据必须按能力分别归因：旧Pulsara证明了resources与prompts产品面；当前调研的Codex基线只证明resource list/template/read入口，不证明prompt list/get。prompt入口是旧Pulsara能力与Round 6显式产品选择，不得宣传为Codex等价实现。所有builtin descriptor在有无MCP配置时都保持稳定：

~~~text
list_mcp_resources(server_id?, cursor?, limit?)
list_mcp_resource_templates(server_id?, cursor?, limit?)
read_mcp_resource(server_id, uri)
list_mcp_prompts(server_id?, cursor?, limit?)
get_mcp_prompt(server_id, prompt_name, frozen_arguments)
~~~

这些不是generic search_tool/use_tool：

- server、resource URI、prompt name与argument schema来自exact discovery snapshot；resource URI可以exact命中静态descriptor，或被同一snapshot中的bounded RFC6570 template保守匹配；matcher必须线性消费literal/expression，拒绝不能唯一切分的adjacent expression，并对query/matrix expansion exact校验advertised variable name；malformed/unsupported template只能作为不可执行metadata，不能授权open-ended URI；
- invocation持有同一execution-generation borrow，不能按server name查询latest client；
- resource body与prompt content一律作为UNTRUSTED_OBSERVATION tool result进入模型，不能提升为system/root instruction；
- resource read是一次bounded远端读取；MCP wire不提供稳定offset/limit，因此不同调用间不承诺远端byte坐标或snapshot identity；
- 完整接受的large body复用Round 1 artifact，后续只通过artifact_read在canonical blob上稳定分页；不得把artifact offset重新发送为第二次远端resources/read；
- list/template/prompt listing的pagination cursor有单次operation上限，不成为durable cursor；
- resource/prompt list-changed只推进`DIRTY_FENCED`并触发full reconcile，不直接改写snapshot；
- 即使协议方法名看起来read-only，transport outcome未知时V1仍不自动重放；
- input-required只进入第11章bounded keyed state-only/unsupported owner；不打开human interaction。

本轮不恢复server-originated sampling、Roots、Apps、Tasks或把prompt模板自动安装成Pulsara skill。

---

## 10. MCP catalog、announcement 与 list fallback

### 10.1 Authoritative catalog snapshot

~~~text
McpCatalogSnapshot
  owner_epoch
  catalog_revision
  servers: tuple[McpServerCatalogEntry, ...]
  semantic_fingerprint
  presentation_fingerprint

McpServerCatalogEntry
  server_id
  display_name
  status
  required
  exposed_tool_count
  discovered_tool_count
  resource_count
  resource_template_count
  prompt_count
  bounded_tool_name_overview
  sanitized_instructions
  stable_failure_category
  tool_surface_semantic_fingerprint | None
  catalog_semantic_fingerprint
~~~

owner_epoch 和 revision 用于 process-local exact join，不进入 provider semantic fingerprint。catalog semantic fingerprint 覆盖模型实际看到的 bounded 内容。

### 10.2 新 context source

增加 first-party source：

~~~text
ContextSourceKind.MCP_CATALOG
channel          RUNTIME_OBSERVATION
trust            UNTRUSTED_OBSERVATION
budget           IMPORTANT
lifecycle        SNAPSHOT_ON_CHANGE
variants         FULL | COMPACT | REF_ONLY
~~~

MCP server instructions 来自外部 server，不得标为 root instruction。runtime 负责外层 typed envelope、字段清洗和权限说明；server 正文仍是 untrusted capability metadata。

首次 cold Host：

- 有配置 server 时生成当前 snapshot；
- optional 未 ready 可显示 CONNECTING；
- 没有 server 时使用 NOT_APPLICABLE，不产生空噪声。

同 Host 变化：

- Round 3.1 把新 snapshot 作为 stateful replacement observation 追加；
- 不改写旧 announcement；
- 同 semantic fingerprint 不重复；
- stale candidate 永不公告。

Host replacement：

- 从当前 authoritative snapshot 重新生成一条当前 announcement；
- 不尝试恢复旧 announcement delivery receipt。

未来 Round 5B compaction rebase采用同一纯snapshot renderer重新生成当前announcement；该兼容断言是dormant future contract，Round 6不实现compaction，也不以compaction测试阻塞activation。

### 10.3 Bounds

V1 默认硬边界：

~~~text
configured servers                           64
discovered tools per server                 512
discovery/list pages per method              20
discovery items across one server          2000
remote resource/prompt body per operation 16 MiB
direct provider tools total                  64  # existing compiler bound
sanitized instructions per server         8 KiB UTF-8
full MCP catalog source total             32 KiB UTF-8
compact catalog source total               8 KiB UTF-8
ref-only catalog source total              2 KiB UTF-8
tool-name overview per server                32
catalog/list result total                 64 KiB UTF-8 before normal artifact handling
~~~

这些是parse后产品边界，不替代第6.4节pre-parse wire/schema/aggregate bounds，也不是 session lifetime budget。超过 discovery bound 必须 typed 标记 catalog incomplete 并阻止该 server direct exposure，不能把截断 listing 伪装成 complete snapshot。remote resource/prompt body只有在一次operation内完整落入bound后才能交给Round 1 artifact；不能先截断远端响应再声称完整。

### 10.4 Stable list_mcp_servers

list_mcp_servers 作为 builtin descriptor 始终存在，哪怕当前没有 MCP config，从而避免启用第一个 server 时仅因 fallback 工具本身出现而改变 provider tool schema。

~~~text
list_mcp_servers()
  -> exact current scope-visible catalog snapshot projection
~~~

它必须：

- 只读当前 snapshot；
- 不 connect、refresh、retry 或修改 config；
- 不返回 raw secret、endpoint credential 或 private URL；
- 不返回可绕过 surface 的 execution handle；
- 按 ROOT/child scope 过滤；
- 结果成为普通 known tool result，因此可被 canonical transcript 保留；
- 调用过程中 catalog 换代时，要么返回 borrow 固定的旧 snapshot，要么在 physical read 前 exact join 当前 snapshot 并 typed conflict，不能混合两代字段。

### 10.5 与 direct schema 的关系

announcement/list 说明“现在有什么”；provider tools 说明“本次 request 可以调用什么”；permission snapshot 说明“物理执行是否被允许”。三者不得合并为一个 authority。

---

## 11. Input-required V1 边界

### 11.1 Elicitation advertisement固定DISABLED

Round 6 V1没有typed form/private-URL controller wire，因此initialize唯一合法值是：

~~~text
McpElicitationAdvertisement.DISABLED
~~~

它由Host composition稳定冻结，不读取transient controller。production不得安装generic JSON/string callback、不得广告form/URL、不得写`MCP_INPUT` interaction decision。server在DISABLED后仍发送human-bearing elicitation时，facade返回协议允许的typed decline/unsupported；若协议无法对exact keyed round完整表达拒绝，则终止该operation并形成protocol-conformance failure。它不能打开LiveControl interaction，也不能让模型或用户把secret塞进普通文本。

未来恢复elicitation至少需要独立规格同时冻结typed form schema、field-value oneof、controller-bound secret submission、private URL consent/launch port、Host-wide interaction arbitration、exact tool-call subject和Protocol/Go rendering；不得只打开SDK callback。

### 11.2 Bounded keyed state-only owner

official SDK 的`inputRequests`仍是keyed request set。即使V1不支持human input，也必须由唯一process-local owner完整验证和原子回复，不能忽略未知key：

~~~text
McpInputRequiredRoundOwner
  exact tool-call / attempt / slot operation identity
  exact connection generation
  round_ordinal
  ordered_request_keys: tuple[SealedRequestKey, ...]
  exact_key_set_fingerprint
  items: tuple[McpInputRequestItem, ...]
  round_state: COLLECTING | READY | SUBMITTING | CLOSED

McpInputRequestItem.method_kind
  STATE_ONLY_CONTINUATION
  | ELICITATION_UNSUPPORTED
  | SAMPLING_UNSUPPORTED
  | ROOTS_UNSUPPORTED
  | UNKNOWN_UNSUPPORTED
~~~

request key先验证唯一与bound，再按UTF-8 byte order确定canonical ordinal；raw key与requestState保持sealed且无generic mapping/repr，不进入digest、event、hook或diagnostic。exact key set中每项都必须得到terminal response disposition，SDK response key set必须严格相等且一次发送；不发送partial mapping。无法表达某个unsupported item时取消整个exact round，不能只继续其他key。

sampling、Roots、elicitation和unknown method均deterministic unsupported，不能调用LLM、读取workspace roots或等待controller。只有不含human data、仅携带opaque requestState的closed state-only item可以在同一physical attempt/operation permit内继续；continuation send复用原lane permit而不递归获取。若unsupported request出现时`tools/call`已经越过physical boundary，只有READ_ONLY observation可以形成known typed failure；EXTERNAL_EFFECT在未取得terminal decline/final response时必须保留attempt-without-result并interrupt，不能伪造`SYSTEM_ERROR`。

### 11.3 Bounds、cancel 与 crash

~~~text
maximum input request items per SDK round      16
maximum state-only rounds per tool call         16
maximum total input-required rounds             24
maximum request-key UTF-8 bytes                 256
maximum public request bytes per item         64 KiB
maximum opaque requestState bytes per item     1 MiB
maximum aggregate round working set            4 MiB
~~~

- cap耗尽取消exact physical attempt并按known/unknown矩阵收口，不能通过新owner重置计数；
- requestState只给fresh MCP wire builder的capability-scoped accessor，不跨connection generation；
- config disable/remove或Host close取消owner并physical join；
- Host crash/writer replacement后owner、raw keys与requestState消失，新Host不恢复、补齐或重放；
- 若无法证明server operation未发生，不提交伪known result。

---

## 12. Permission、scope 与 subagent

### 12.1 Catalog visibility 不是 permission

server READY 或工具出现在 announcement 中，不表示本次 run 已经授权。真实 dispatch 仍按：

~~~text
config/exposure/scope builds a stable provider tool surface
-> model returns tool call under an exact tool-surface borrow
-> exact generation-bound READ_ONLY | EXTERNAL_EFFECT fact
-> frozen run permission snapshot is evaluated once by local authorize
-> optional Host-wide serialized human confirmation
-> exact MCP dispatch admission permit
-> attempt-before-effect
~~~

Round 6 的 permission closure 是一条窄 dynamic-tool adapter seam，不是第二套 permission engine：

- builtin tools 继续使用 builtin catalog/classifier；
- MCP tools 不查询 `builtin_tool_catalog_entry`；
- dynamic adapter 只能返回第6.3节的 closed effect fact；
- ordinary unknown dynamic tool 不得沿当前 fallback 路径静默 ALLOW，也不得在 authorize或effect classification时抛 `KeyError`；
- 最终 allow/deny/confirm 仍由同一个 frozen run permission snapshot决定；
- permission preset只在local authorize生效一次，不得参与provider exposure、descriptor filtering、semantic tool fingerprint或Round 3.1 epoch compatibility。

不得因为 server READY、catalog可见、annotation声称read-only或用户曾在另一generation批准过同名tool，就跳过当前 generation/scope exact join。Round 6 不提供跨run、跨Host或durable “remember approval”。

### 12.2 ROOT 与 child surface

每个 scope 冻结自己的 semantic surface 和 execution bindings；这里的scope只由config/exposure policy与ROOT/SUBAGENT_TASK attribution决定，不读取run permission preset：

- ROOT 只看到 ROOT 允许的 MCP tools；
- child 只看到 ROOT_AND_SUBAGENTS 的 MCP tools；调用时再由继承的parent permission snapshot authorize；
- child 的 list_mcp_servers 只返回 child-visible catalog；
- parent human prompt 中的 textual skill/MCP mention 不自动授予 child；
- child 不能按 server id 查全局 latest client。

`READ_ONLY` run 只允许effective effect kind为`READ_ONLY`的MCP tool；`EXTERNAL_EFFECT`唯一在local authorize确定性拒绝且不得取得dispatch permit。provider仍可看到同一scope-stable descriptor并得到typed no-attempt denial result，因此permission切换不会增删tools、改写prefix或重建epoch。child沿用parent冻结的run permission preset，不重新推断更宽权限。

### 12.3 Borrow attribution

每次 MCP authorize/invoke 必须 exact join：

~~~text
session_id
scope kind
subagent_task_id | None
turn_id
assistant entry/tool call
permission snapshot fingerprint
semantic descriptor fingerprint
execution surface generation
opaque Host authority identity
~~~

foreign Host、foreign child、expired generation 或 same-shape copied DTO 必须在 attempt/physical dispatch 前失败。

---

## 13. Failure、retry、crash 与 close 矩阵

| 场景 | canonical 结果 | process-local 处置 | 自动重试 |
|---|---|---|---|
| optional server startup 慢 | 无 | catalog 显示 CONNECTING，后台继续 | 允许 connect/discovery retry |
| required server startup 失败 | 不开启 provider call | typed open/dispatch failure 并 close attempt | 允许有界 connect retry |
| discovery page 失败 | 无 complete snapshot | candidate 丢弃，旧健康 slot 可保留 | 允许完整 relist |
| frame/body/event在parse前超限 | 无success/candidate | 终止exact operation或slot并physical join | discovery可fresh retry；tool call不重放 |
| stale candidate 迟到 | 无 | 释放candidate lease；由supervisor按统一条件决定slot close | 否 |
| semantic-identical reconnect | 无 | 新 execution generation safe-point 安装 | 连接层允许 |
| schema 变化 | 无 | safe-point 安装并触发 new provider-input epoch | discovery 允许，tool call 不重试 |
| list-changed storm | 无 | 单 server dirty bit + bounded wake | coalesced full relist |
| listChanged后旧response返回tool call | typed no-attempt `MCP_SNAPSHOT_STALE` result | 不取得operation permit；等待reconcile | 当前call不重放 |
| missing/unknown resultType | typed protocol-conformance failure，不接受success | exact operation收口；slot按compatibility policy降级/关闭 | 当前call不重放 |
| tool 已明确返回 success/error | 接受 exact ToolResult | 正常 release borrow | 否 |
| tools/call 确定未写出 | known typed transport failure 可接受 | reconnect 供未来调用 | 当前调用不重跑 |
| tools/call 可能写出但响应丢失 | attempt 无 result，turn interrupted | reconnect 供未来调用 | 绝不自动重跑 |
| MCP tool confirmation被user拒绝 | existing TOOL_CALL interaction decision；no-attempt denial result | 释放dispatch admission permit，不进入outbound lane | 否 |
| keyed input batch有unsupported item | READ_ONLY可形成known typed observation failure；EXTERNAL_EFFECT且无terminal decline/final response时attempt无result并interrupt | 不发送partial response；不重跑physical tool | 否 |
| Host crash | 无新的 MCP recovery row | manager/request 全部消失 | 新 Host 仅 fresh connect |
| config disable/remove | 已有 canonical facts 不改写 | 停止新 borrow；取消 pending human request；drain/close | 否 |
| ordinary hook 抛错 | canonical/tool outcome 不受影响 | detach/quarantine hook | 否 |
| Host close waiter 取消 | 无 | waiter detach；唯一 close task 继续 drain | 不适用 |

### 13.1 Close 顺序

~~~text
close MCP admission
-> stop config refresh/retry timers
-> stop subscription listeners
-> cancel dormant/current tool confirmations and state-only input-required owners
-> prevent new tool-surface borrows
-> wait/cancel active exact physical requests according to outcome policy
-> drain retiring generations
-> close current clients/stdio process groups
-> join supervisor tasks
-> release generic tool/repository/provider owners
~~~

close 超时不能把仍运行的 stdio child 或 HTTP task 变成无 owner 后台工作。与 Round 5A 相同，logical waiter timeout 只 detach waiter；Host-owned close task 继续 physical join。

---

## 14. Durability 与 AgentEvent 边界

### 14.1 不新增 MCP canonical relation

连接、catalog 与 generation 是“当前 Host 现在能做什么”，不是跨 Host conversation truth。它们不应进入 PostgreSQL。

现有 canonical rows 承载：

- 模型接受的 MCP tool request；
- attempt-before-effect；
- known result 或 missing-result unknown；
- human decision；
- turn terminal state。

### 14.2 不新增 MCP-specific CommittedAgentEvent

| 用户可观察事实 | 已有 event |
|---|---|
| assistant 请求 MCP tool | AssistantToolRequestAccepted |
| physical attempt 被接受 | ToolAttemptAccepted |
| remote identity 发布（若适用） | ToolRemoteIdentityPublished |
| ordinary MCP tool permission confirmation | 既有 TOOL_CALL InteractionDecisionAccepted |
| known MCP result | ToolResultAccepted |
| unknown 导致 turn 结束 | TurnInterrupted |

McpServerConnected、McpCandidateReady、McpRefreshStarted、McpBorrowReleased 不是 durable 产品 occurrence。

### 14.3 Live 与 Operational

- tool streaming 继续用现有 ToolResult Start/Delta/End；
- interaction 继续用现有 Interaction Opened/Replaced/Closed；
- MCP lifecycle/status、retry、pagination、dirty 和 latency 属于 OperationalEvent/hook；
- MCP_CATALOG 是 provider-input typed context source，不是 AgentEvent；
- overflow 只能丢弃/coalesce operational observer，不得阻塞 MCP transport 或 canonical commit。

### 14.4 Hook capability

普通 hook 只看 typed/redacted server id、status、stable error category 和计时。以下内容需要显式 capability 并默认拒绝：

- auth headers/token；
- private URL query/fragment；
- form values；
- raw server instructions；
- raw tool arguments/result；
- stdio environment/stderr；
- transport exception body。

---

## 15. Implementation slices

### 15.1 R6-0：Identity split 与 multi-generation tool surface

修改：

- model_input/contracts.py；
- model_input/continuity.py；
- model_input/compiler.py；
- conversation_kernel/tool_surface.py；
- conversation_kernel/tool_runtime.py；
- conversation_kernel/direct_model.py；
- conversation_kernel/runner.py；
- Round 3/3.1 retained tests 与 golden vectors。

Exit gate：

- semantic-identical execution rebind 不重建 epoch；
- semantic change 仍重建 epoch；
- old borrow/new borrow 可以分别 exact join retiring/current generation；
- provider wire tools 不包含 execution identity；
- foreign/same-shape 伪造 binding 仍 fail closed。

### 15.2 R6-A：Typed config 与 SDK leaf

新增建议 package：

~~~text
src/pulsara_agent/conversation_kernel/mcp/
  contracts.py
  config.py
  transport.py
  sdk_client.py
  result_validation.py
  schema.py
  naming.py
~~~

可以复用 historical 算法与测试语义，但不得恢复 runtime/mcp 旧 authority 图。SDK 只能通过一个 facade package 导入。

Exit gate：

- stdio/Streamable HTTP config；
- none/static/bearer auth；
- `~/.pulsara/mcp.yaml`与显式trusted workspace YAML采用whole-entry merge；普通Host open默认把workspace-only entries强制disabled且禁止其覆盖user entry；
- CLI add默认写user config，`--workspace`才写workspace config，Host override不落盘；
- `AUTO | READ_ONLY | EXTERNAL_EFFECT` server default与exact tool override；
- 自动annotation classification不写回YAML或PostgreSQL；
- secret-safe repr/diagnostic；
- pre-parse bounded stdio frame、HTTP body、SSE event与aggregate discovery working set；
- bounded initialize/listing，且listing严格服从negotiated capability set；
- schema conformance 与 deterministic naming；
- 任意合法remote tool name在exposure前都必须证明最终remote identity可进入canonical 4 KiB contract；V1使用覆盖server、connection generation和完整remote name的domain-separated bounded digest，不把原始长name带到result settlement；
- negotiated-era `complete | input_required` resultType wire-presence validation；
- server cache hint在V1被明确忽略，refresh只由Host policy驱动；
- no SDK private API。

### 15.3 R6-B：Supervisor、candidate 与 safe-point installer

新增：

~~~text
supervisor.py
installation.py
catalog.py
~~~

接入 KernelHostSession 唯一 owner 与 Host close task。

Exit gate：

- optional/required 启动；
- retry/config reload/manual reconnect；
- stale candidate drop；
- semantic unchanged/changed 矩阵；
- stdio/sessionful HTTP serialize，只有proved-stateless HTTP bounded parallel；
- notification receive loop不取得outbound lane且不能递归operation；
- runtime generation持有exact slot lease tuple，只有supervisor关闭slot；
- old generation drain与shared-slot lease交叠；
- pending/installed failure settlement exact join slot identity，successful retired slot从active registry移除；stdio EOF推进typed failure/reconnect；
- listChanged立即fence新dispatch，reconcile safe-point安装后恢复；
- no network I/O under installer lock；
- enabled config 不再触发 Stage 2 fail-closed composition gate。

### 15.4 R6-C：Direct tool execution

新增：

~~~text
tool_adapter.py
tool_execution.py
~~~

接入 DirectKernelToolPort 的 dynamic installation 与 existing runner attempt/result path。

Exit gate：

- descriptor/executor 同 snapshot；
- dynamic MCP authorization不查询builtin catalog；
- generation-bound `READ_ONLY | EXTERNAL_EFFECT` policy exact join；
- 四种run permission preset矩阵闭合，默认BYPASS无确认；
- attempt-before-effect；
- exact scope/permission/generation；
- exact slot operation permit在attempt前取得，dirty/revoked slot产生no-attempt stale result；
- known/unknown 结果矩阵；
- missing/unknown resultType在success lowering前fail closed；
- Round 1 artifact；
- no ambiguous retry；
- resources/templates/prompts的stable builtin descriptors、exact snapshot binding与untrusted lowering；
- remote resource read没有offset/limit，完整bounded body只经Round 1 artifact/artifact_read分页。

### 15.5 R6-D：Catalog source 与 list_mcp_servers

修改：

- model_input/contracts.py 增加 MCP_CATALOG；
- conversation_kernel/context_sources.py 增加 binding 与 render；
- builtin catalog 永久加入 list_mcp_servers；
- tool runtime 绑定只读 snapshot port。

Exit gate：

- initial connecting/ready/failed announcement；
- late-ready safe-point append；
- duplicate generation 不重复；
- Host cold bootstrap 从 current snapshot 重建；
- list 只读且 scope-filtered；
- server instructions 有 sanitizer/bounds；
- list 不授予 execution 能力。

### 15.6 R6-E：State-only input-required 与 confirmation arbitration

新增：

~~~text
  input_required.py
  interaction_arbiter.py
~~~

不扩展Protocol，不修改clean-v0 `MCP_INPUT` branch。ordinary MCP permission confirmation继续复用既有TOOL_CALL decision+attempt原子事务；Host-wide arbiter只串行公开现有tool confirmation。input-required只实现process-local keyed state-only/unsupported owner。

Exit gate：

- initialize固定广告elicitation DISABLED；
- human-bearing form/URL不会公开interaction或写MCP_INPUT row；
- ordinary/MCP tool confirmation共用Host-wide bounded FIFO与单一visible slot；
- MCP confirmation在公开前取得dispatch admission permit，ALLOW复用decision+attempt原子事务；
- keyed inputRequests按bounded canonical key order建立单一round owner；
- 全部exact keys terminal后才原子发送完整response mapping；
- form/URL/Sampling/Roots/unknown method deterministic unsupported，state-only/total round cap闭合；
- raw request key/requestState不进日志/event/metadata；
- same-Host exact operation permit continuation；
- cancel/timeout/Host close physical join；
- cross-Host 明确不恢复。

### 15.7 R6-F：CLI、activation 与旧 gate 删除

恢复新的非 Legacy 命令：

~~~text
pulsara mcp list
pulsara mcp add
pulsara mcp remove
pulsara mcp enable
pulsara mcp disable
pulsara mcp doctor
pulsara mcp reconnect
~~~

其中 config edit 与 active Host reconnect 必须是不同 typed 路径；一个独立 CLI 进程不能假装控制另一个 Host 的 process-local supervisor。

最后删除 Host 中“发现 enabled MCP 即拒绝 open”的临时 composition gate，更新 README、Gap Index 与 activation evidence。

---

## 16. Transaction 与 linearization 矩阵

| 操作 | linearization point | PostgreSQL transaction | physical action 时机 |
|---|---|---|---|
| MCP candidate ready | supervisor lock 接受 candidate | 无 | connect/list 已经完成 |
| safe-point install | installer 原子发布 current generation | 无 | 无新 side effect |
| listChanged | slot state lock推进dirty generation并关闭product admission | 无 | fence前operation排空；fence后只允许reconcile/close |
| list_mcp_servers | exact catalog snapshot borrow | 普通 tool attempt/result 沿现有事务 | snapshot read 发生在 attempt FULL 后 |
| MCP tool authorize deny | policy result | no-attempt ToolResult transaction | 无 physical call |
| MCP confirmation publish | dispatch admission permit在arbiter FIFO head取得 | 无 | 不占outbound lane；dirty/revoked时不OPEN |
| MCP confirmation ALLOW | stable existing decision candidate FULL | existing TOOL_CALL decision + ToolAttemptAccepted同一transaction | FULL后同一permit进入outbound lane |
| MCP confirmation DENY/cancel | stable existing decision/denial result | existing TOOL_CALL decision/no-attempt result path | 释放permit，无physical call |
| MCP tool execute | ToolAttemptAccepted FULL | existing attempt transaction | FULL 后 exact 一次 |
| MCP known result | result candidate FULL | existing result/entry/event transaction | result 已取得，随后 commit |
| MCP unknown | physical outcome 无法证明 | turn interrupt transaction；attempt 无 result | 不重试 |
| input-required batch resume | exact key set全部terminal且slot operation仍有效 | 无新增transaction | 一次发送完整keyed response mapping |
| config disable | Host config/supervisor admission fence | 无 | 停止新 borrow 并 close/cancel |

canonical commit ACK unknown 继续使用现有 stable candidate 与 stateless exact confirmation。MCP 不得为此增加 receipt。

---

## 17. Test matrix

### 17.1 Prefix 与 identity

1. 两个 FrozenProviderToolSpec 语义相同、execution binding 不同，semantic surface fingerprint 必须相同。
2. physical reconnect 后 system/tools/messages 保持相等或 append-only suffix，reset reason 为 None。
3. schema/description/name 变化产生 TOOL_SURFACE_CHANGED。
4. old borrow 完成旧 tool batch，新 request 借新 generation。
5. old generation 不再发放新 borrow。
6. foreign Host 与 same-shape copied access 在 provider open/authorize/invoke 前失败。
7. compiled semantic fingerprint 不含 client、slot、generation 或 binding identity。
8. prepared execution fingerprint 必须包含 current execution surface identity。
9. `G1={A1,B1}`、`G2={A2,B1}`时释放G1不会关闭B1；只有最后slot lease与operation归零后supervisor可关闭。

### 17.2 Startup 与 discovery

- optional slow server 不阻止 first call，announcement 显示 CONNECTING；
- config numeric defaults/ranges与`resolved_config_identity` fixed vectors；范围外值不clamp且candidate count为0；
- effective MCP timeout取configured与Round5A 600秒owner bound较小值，late exact result/unknown矩阵保持；
- late ready 在下一 safe point 同时安装 tool 和 announcement；
- required server 在 bounded wait 内 ready 后才 open provider；
- required timeout provider open count 为 0；
- pagination complete 且 deterministic；
- partial/oversized listing 不能安装；
- oversized stdio frame、HTTP JSON body、SSE event在JSON parse前终止，内存不超过sealed transport bound；
- wire JSON node/depth在`json.loads`前拒绝、schema 256 KiB/4096 nodes/depth 64与从`client.open()`开始的per-server/Host aggregate candidate reservation均有边界测试；
- URI-template adjacent expression、错误query/matrix variable name与linear-time回归闭合；合法长remote name的exact physical result可完成remote-identity publication与ToolResult canonical acceptance；
- invalid schema 按 closed policy 处理；
- stale config/attempt candidate 被丢弃；
- list-changed storm 只触发 bounded reconcile；
- listChanged线性化后old provider response得到no-attempt stale result，reconcile安装后才恢复；
- stdio与sessionful HTTP的discovery/call/read严格串行，stateless HTTP只在closed semaphore内并发；
- request等待response时notification可被接收且不与outbound lane死锁；
- server `ttl_ms/cache_scope`有无、分页是否矛盾都不改变V1 Host refresh decision或fingerprint。

### 17.3 Tool happy path

~~~text
provider sees direct MCP schema
-> assistant tool request canonical FULL
-> attempt canonical FULL
-> exact MCP physical call
-> known result
-> Round 1 artifact/preview if needed
-> result canonical FULL
-> next provider call sees closed tool pair
~~~

覆盖 text、structured content、server application error、large result、多 tool batch 与 child scope。

permission/effect至少覆盖：

- annotation明确且无矛盾的read-only -> `READ_ONLY`；
- annotation缺失、矛盾、destructive或open-world -> `EXTERNAL_EFFECT`；
- exact tool override > server override > annotation；
- `READ_ONLY / ASK_PERMISSIONS / ACCEPT_EDITS / BYPASS_PERMISSIONS`完整矩阵；
- 四种preset使用完全相同scope-stable provider tool schema/fingerprint，permission变化不触发TOOL_SURFACE_CHANGED；
- EXTERNAL_EFFECT在READ_ONLY中只由local authorize拒绝并产生typed no-attempt result；
- 默认BYPASS不会弹MCP confirmation；
- ACCEPT_EDITS不会把远程external effect当成本地edit自动放行；
- dynamic MCP authorize/effect lookup不触发builtin catalog `KeyError`；
- 相同semantic/policy physical reconnect保持相同policy fingerprint；
- policy变化只影响safe-point后新borrow，已admit batch仍使用旧冻结fact；
- read-only annotation本身不授予parallel execution；
- permission allow不授予ambiguous retry。

标准read surface还要覆盖resource list/template/read、prompt list/get、listing pagination循环/越界、large artifact、hostile prompt content保持untrusted，以及semantic-identical reconnect不改变这些builtin descriptor。两次read_mcp_resource不得伪装为同一远端snapshot的offset分页；一次完整接受后只能由artifact_read提供stable坐标。

result-type至少覆盖tools/call、resources/read、prompts/get各自的explicit `complete`、explicit `input_required`、missing字段、unknown future字符串与payload矛盾；只有前两种closed值进入相应lowering，其他success count为0。

### 17.4 Unknown 与 retry

- connect/list transient failure 可以 retry；
- physical tool request 确认未写出时 known failure；
- response ACK 丢失时 attempt 无 result 并 interrupt；
- reconnect 后不自动重复旧 call；
- old call 不能被 redirect 到 new client；
- stale writer 不能 canonicalize late result。

### 17.5 Catalog

- no config -> list_mcp_servers 返回空，tool descriptor 本身保持稳定；
- connecting/ready/failed/disabled render；
- instructions 中的 prompt injection、ESC/OSC/C1、invalid UTF-8 安全处置；
- FULL/COMPACT/REF_ONLY bounds；
- same snapshot 不重复 announcement；
- status-only change append observation 但不改变 tool semantic fingerprint；
- tool_surface/catalog/presentation三个fingerprint的变化矩阵闭合；
- cold Host 使用 current snapshot 重建；
- future Round 5B compaction rebase只保留dormant contract test，不计入Round 6 activation；
- list 结果不能调用未暴露 tool。

### 17.6 Confirmation 与 input-required

- initialize wire明确不广告elicitation；
- human-bearing form/URL不会产生LiveControl、MCP_INPUT row或普通文本fallback；
- builtin/MCP tool confirmations按Host-wide FIFO逐个公开，第二项不会触发`_pending`异常；
- MCP confirmation只在取得dispatch admission permit后OPEN；DENY/detach/timeout释放permit；
- confirmation等待期间listChanged后ALLOW仍用同一pre-admitted generation，decision+attempt FULL后exact call一次；
- listChanged先于permit则不公开confirmation、不创建attempt并返回stale；
- keyed batch canonical order、exact response key set与no partial response；
- mixed human-unsupported/state-only/Sampling/Roots batch及state-only/total round cap；
- unknown method与unknown resultType fail closed；
- cancel、timeout、Host closephysical join且requestState/raw key不进event、diagnostic、hook、repr或activation evidence；
- Host replacement不恢复旧request；clean-v0 catalog/fingerprint不因Round 6改变。

### 17.7 Physical lifecycle

- stdio process group close/join；
- HTTP task close/join；
- retry timer/subscription listener 不泄漏；
- waiter cancellation 只 detach；
- config disable取消dormant/current confirmation并释放MCP admission permits；
- ROOT 与 child 并发 borrows 不阻塞新 generation 发布；
- retiring generation只释放slot lease，不调用close；
- exact slot在last generation lease和last operation release后由supervisor关闭。

### 17.8 Real dogfood

至少执行：

1. local stdio MCP fixture：discover -> direct tool -> result；
2. local Streamable HTTP fixture：late-ready -> announcement -> direct tool；
3. same-schema reconnect：证明 prefix 未 reset 且 new client 执行；
4. schema-change reconnect：证明 safe-point new epoch；
5. input-required fixture：state-only exact-key response以及human-bearing form typed rejection；
6. real provider：模型根据 initial catalog 主动调用 MCP tool，并在长对话中使用 list_mcp_servers 恢复 catalog。

dogfood 证据不得保存 API key、header、raw inputRequests/requestState、完整 prompt 或大段 tool output。

---

## 18. Architecture guards

静态 guard 至少包含：

- production 只有一个 MCP SDK facade 可以 import official SDK；
- conversation_kernel/mcp 不得 import 旧 runtime/event_log/projection；
- MCP package 不得定义 PostgreSQL repository 或 migration；
- no class/function identifier 含 receipt/checkpoint/reducer/repair/reconciliation，除非只在 negative guard/test 说明中；
- no durable MCP relation/event/job；
- no provider semantic DTO 持有 executor/client/slot/generation/transport；
- no executor lookup by only server_id + tool_name；
- no automatic retry around physical tools/call；
- no tool-surface generation/client adapter can close a slot；only supervisor owns physical close；
- every runtime generation references MCP physical clients only through exact `McpSlotLease` tuple；
- stdio/sessionful HTTP cannot select bounded-parallel mode；
- passive notification callbacks cannot acquire outbound operation permit or start relist inline；
- dirty slot cannot admit new product operation；
- no success lowering before closed resultType wire-presence validation；
- no stock unbounded SDK stdio/HTTP/SSE framing helper on the production path；
- no network I/O while installer/tool-surface locks held；
- no raw secret in dataclass repr、JSON、diagnostic、hook；
- no MCP dynamic tool permission/effect lookup through builtin catalog；
- no run permission preset in MCP provider exposure/filtering or semantic tool fingerprint；
- no MCP effect vocabulary beyond `READ_ONLY | EXTERNAL_EFFECT` in Round 6 production contract；
- no persisted derived MCP classification或durable MCP approval receipt；
- no server cache hint in V1 fingerprint、freshness或dispatch authority；
- no remote offset/limit parameters on read_mcp_resource；
- no raw request key/requestState in ordinary digest、event、hook或diagnostic；
- no MCP elicitation advertisement或MCP_INPUT write in Round 6 production；
- one Host-wide confirmation arbiter is shared by builtin and MCP tools；
- direct exposure 总数服从 64-tool hard bound；
- list_mcp_servers 总是只读；
- existing message-before-dispatch 与 attempt-before-effect tests 继续保留；
- oracle 保持 34 / 23 / 15 / 2 / 26 / 4。

---

## 19. Verification gates

实现时至少运行：

~~~bash
uv run pytest -q tests/test_round6_mcp_production.py

uv run pytest -q \
  tests/test_round3_structured_model_input_compiler.py \
  tests/test_round3_1_provider_input_prefix_continuity.py \
  tests/test_round5_long_horizon_execution_envelope.py \
  tests/test_stage2_direct_model.py

uv run pytest -q

uv run pytest -q -m postgresql

uv run ruff check .
uv run python -m compileall -q src tests tools
uv run python tools/generate_terminal_protocol_contract.py --check

(cd clients/terminal && go test ./...)
(cd clients/terminal && go vet ./...)

uv lock --check
git diff --check
~~~

还必须执行：

- Markdown fence 闭合；
- 重复 heading 检查；
- active 文档本地链接检查；
- secret scan；
- MCP package import graph；
- event/relation/job oracle；
- clean-v0 fresh install、second migrate、deep verify，证明本轮没有数据库catalog变化且relation/event/job oracle不变；
- local stdio/HTTP physical task leak 检查；
- real-provider dogfood。

---

## 20. Definition of Done

Round 6 只有同时满足以下条件才能标记 ACTIVATED：

- enabled MCP config 不再阻止 Kernel open；
- workspace MCP只有在当前Host显式trust后才能启动或解封secret reference；
- stdio 和 Streamable HTTP 至少各有一条 production happy path；
- optional/required startup 语义闭合；
- descriptor 与 executor exact same-generation；
- runtime generation只持有slot lease，physical slot只由supervisor在lease/operation归零后关闭；
- stdio/sessionful HTTP串行且stateless HTTP bounded parallel；
- pre-parse frame/body/event、`json.loads`前structural scan、schema结构、per-slot concurrent byte reservation与从`client.open()`开始的aggregate discovery working set全部有界；PUBLIC_ONLY实际connection使用验证后pinned address与原始Host/SNI；
- old request/tool batch 不会因 refresh 漂移到 new client；
- listChanged立即fence新physical dispatch，reconcile safe-point install后才恢复；
- same-semantic physical reconnect 不改变 provider semantic prefix；
- semantic schema 变化只在 safe point 建立 new process-local epoch；
- direct MCP tool 遵守 permission、message-before-dispatch 与 attempt-before-effect；
- MCP tool使用generation-bound两类effect fact，四种现有permission preset矩阵闭合且默认BYPASS无确认；
- 自动effect classification仅存于process-local generation，只有显式override写入既有user/workspace YAML；
- resources/templates/prompts标准read surface使用exact generation与untrusted result lowering；advertised resource-template实例只经linear conservative matcher执行并exact校验named query/matrix变量，list类bounded pagination，resource body一次bounded读取后只经artifact_read稳定分页；
- remote tool name无论长度都不能在exact result返回后阻断canonical settlement；provider display name与bounded remote identity均由完整原名的domain-separated digest闭合；
- tools/call、resources/read、prompts/get在lowering前验证explicit closed resultType；
- ambiguous side effect 不自动 retry；
- initial/state-change announcement 与 list_mcp_servers 生效；
- server instructions 和 catalog 有 bounded sanitizer；
- ROOT/child scope 隔离；
- elicitation固定DISABLED且不写MCP_INPUT；input-required keyed state-only batch、round caps与human/Sampling/Roots/unknown rejection矩阵闭合，EXTERNAL_EFFECT unsupported input不伪造known result，requestState不泄漏且不跨Host恢复；
- ordinary/MCP confirmation共用Host-wide FIFO单槽；MCP在公开确认前取得generation-bound admission permit，decision+attempt仍同事务；
- V1完全忽略server cache hint，periodic refresh只有Host policy一个owner；
- Host close physical drain 完成；
- no MCP durable relation/event/job/recovery owner；
- full Python tests、PostgreSQL tests、未修改Protocol/Go的retained gates、static checks 与 dogfood 通过；
- activation evidence 记录实际 HEAD、文档 hash、代码 hash、测试结果、oracle 和 non-goals；
- Gap Index 只把实际恢复的 PHC-08 范围标记为 closed：resources/prompts只声明本文标准read surface；form/private-URL、MCP-backed skill activation、OAuth、server-initiated Sampling、Apps/Tasks与advanced Go UI继续列为non-goal/缺口。

最终产品语义应当可以用一句话描述：

> Pulsara 把 MCP 连接与目录作为 Host-scoped、可 safe-point 换代的 process-local capability authority；把模型看到的 typed tool schema 与实际执行 generation 精确绑定；用 bounded announcement 和 list_mcp_servers 保持 catalog 可见；把真正的 tool request、attempt、result 与 human decision 提交到现有 canonical conversation kernel，但绝不通过 event replay 恢复旧 MCP execution。
