# Pulsara 统一身份 hard cut 全仓复查

归档说明（2026-10-14）：身份 hard cut 已实施并提交（`0fea8185`），下文保留各阶段的调查与验证事实；“图片可视化延期”是当时的范围说明，后续已按[统一展示规格](PULSARA_UNIFIED_VISUALIZATION_PRESENTATION_SPEC.zh.md)实施。真实纯文本 provider 的配置缺口及正式 home 切换边界仍按[验证记录](PULSARA_UNIFIED_IDENTITY_HARD_CUT_VALIDATION.zh.md)保留，不因归档改写为已完成。图片读取权限和协议版本以后续统一展示规格为准。

日期：2026-10-14。代码基线：Git `5df79ba9`。

状态：第二轮只读调查，服务于统一业务身份的 hard cut 设计；不是已冻结的实施规格。图片与 HTML 混合展示按用户决定延期。本轮未修改生产代码、数据库或生产配置。

后续设计已整理为[统一身份 hard-cut 实施规格](PULSARA_UNIFIED_IDENTITY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)，并经主代理与 GPT‑6 Astra high 审阅讨论后冻结 v1。该规格明确具体格式、配方、碰撞与生命周期；本文第6节保留调查时的问题清单，不再代表没有设计答复。规格现已实施，结果与验证缺口见[实施记录](PULSARA_UNIFIED_IDENTITY_HARD_CUT_VALIDATION.zh.md)；本文仍保留调查时的基线事实。

## 1 结论与范围修正

第一轮模型可见引用调查不能单独充当全仓身份改造清单。它回答了模型会复制哪些值，但没有完整覆盖主外键、隐式派生身份、fork 重映射、UI 定位、进程局部对象及外部协议身份。

本轮发现了实际遗漏：queue、snapshot、context binding、block、replay fragment、审批 interaction、terminal process/monitor、TODO run、scheduled tick，以及前端 annotation source 和调用模型的 connection ID。它们有些不向模型展示，却会受到 canonical ID 规则改变的影响。

当前设计方向是统一业务身份 hard cut：明确哪些实体采用统一的短 ID 规则，让内部业务接口和模型操作使用同一身份。第一轮建议的“优先保留全部 canonical ID，在模型面作适配”不再是推荐路线。完整内容摘要、真实版本确认、认证值、分页状态和外部 provider 身份仍须分别保留其职责。

不能把“统一”解释成给每张关系表增加一个新 ID。已有复合键、唯一约束、整数序号和派生投影，应按其现有语义继续组织；也不能为了短码增加身份注册表或新的持久化类别。

## 2 覆盖方法与可复核范围

本轮使用四个入口交叉核查：

1. 三个 GPT‑6.1 Sol high 子代理分别从数据库/生命周期、生成点/执行、协议/UI/模型消费端反向追踪。
2. 主代理扫描生产 Python、TypeScript、TSX、JavaScript、SQL 和 protobuf，枚举 UUID、hash、HMAC、随机 token、identity builder 与格式校验。
3. 枚举 clean-v0 baseline 的所有 CREATE TABLE，与 runtime relation manifest 逐项对照，并单独关注表后追加的外键和触发器。
4. 核对真实最后投影，避免把存储字段、GUI 值或普通任务名称误当成模型句柄；对原审计的 Plan 分歧已闭合。

本地静态枚举结果：扫描528份相关生产/前端文件，取得812处候选行、165种 Python f-string 常量前缀及257个 protobuf 候选身份字段。候选包含日志名、async task 名称、URI 和内部校验值，**这些数字不是业务实体数量，也不是逐行审计通过数量**。生成的 protobuf Python 文件不重复计数，以 `.proto` 源为准。

baseline 含31张 `pulsara_v3` 应用关系，已与 `CONVERSATION_KERNEL_RELATIONS` 集合完全对齐；另有1张 schema migration 元数据表。此对照证明没有漏列应用表，不证明所有动态调用路径或 JSON 内嵌引用已完成端到端验证。

公开入口专项还枚举并分类检查了39个 builtin descriptor，反查各类 tool result 与 runtime source；两个 OpenAI payload 路径、Plan 最终投影、scheduled provenance、批注源坐标及 UI/protobuf 消费端已交叉核对。此轮逐类追踪生产接缝，没有声称对528份文件逐行审阅。

机器枚举保存在本地 `output/identity-audit-20261014/`：`coverage.json`、`builder-candidates.json`、`prefixes.json`、`schema-identity-inventory.json`、`protocol-identity-fields.json`。这些是静态调查辅助结果，不是新的 runtime 注册表、执行收据或文件指纹证据。

## 3 全部应用关系的身份覆盖

以下是31张应用关系的完整覆盖表。生成格式相同不表示生命周期相同；列入表中不等于需要改变该关系的键结构。

| 关系 | 当前身份边界 | hard cut 必须处理的关联 |
| --- | --- | --- |
| `workspaces` | `(memory_domain_id,id)` 复合主键；project 与 transient 来源不同 | 路径归一化、memory context、同 domain 碰撞拒绝及 session 绑定 |
| `sessions` | 会话 ID | ROOT 历史、UI 导航、scope、删除、fork、外部连接 |
| `blobs` | blob ID 与独立 storage identity/完整摘要 | MIME/codec/workspace 边界、去重、七类 owner 与 GC |
| `context_snapshots` | snapshot ID | session 基底、压缩、图片 refs、完整内容读取 |
| `subagent_tasks` | task ID 与 batch/task_key 唯一关系 | 调度、依赖、mailbox、history、模型工具和完成通知 |
| `subagent_task_dependencies` | `(session,task,dependency)` 复合键 | 两端任务身份与依赖 ordinal，保持边关系 |
| `turns` | turn ID | ROOT/worker、commands、entries、active turn 与终态归属 |
| `provider_call_usage` | `resolved_model_call_id` | 本地调用身份与 usage 关联，区分 provider 自带 request/response ID |
| `turn_context_binding_revisions` | binding ID 与 turn/revision ordinal 唯一关系 | 安装基底、turn、epoch 连续性、fork context |
| `session_commands` | `(session,command_id)` 复合键 | 请求幂等、queue、取消与执行结果 |
| `imported_history_groups` | history group ID | fork/import 的 entry owner，不能伪造已执行 turn |
| `transcript_entries` | entry ID、session sequence、origin ID | 历史锚点、批注、tool call/result、fork、源事件关联 |
| `session_context_genesis` | session ID | 会话最初上下文归属，不另设新实体 ID |
| `assistant_message_blocks` | block ID 与 entry/ordinal | TEXT/TOOL/DATA block、工具调用闭合、UI 内容定位 |
| `provider_assistant_replay_fragments` | replay fragment ID | 原 provider payload、所属助手消息、同 provider 回放兼容 |
| `tool_execution_attempts` | attempt ID 与 assistant entry/tool_call 唯一关系 | 执行、审批、结果、运行权与取消结算 |
| `tool_results` | result ID、result entry、artifact ID | 工具闭合、输出 blob、memory 来源、retained-output 读取 |
| `imported_tool_call_closures` | `(session,assistant_entry,tool_call_id)` | 导入历史闭合；provider tool_call_id 不随意改写 |
| `prompt_queue_items` | queue ID 与 session queue sequence | steer、scheduled tick、plan continuation、消费与拒绝 |
| `scheduled_tasks` | task ID 与整数 revision | 到期触发、队列、删除/取消、model/UI 查询和分页 |
| `canonical_image_refs` | owner + ordinal 唯一索引，无独立主键 | queue/entry/snapshot 三选一 owner、blob、图像读取权限 |
| `assistant_visualizations` | `(session,assistant_entry,ordinal)` 复合主键 | 当前 HTML 保存、fork 和 GC；图片扩展仍延期 |
| `interaction_decisions` | decision ID、command 与 interaction 归属 | UI 用户决定、恢复、幂等确认与审批结算 |
| `plan_workflows` | workflow ID | phase、冻结草案、review、turn 与 queue 关联 |
| `plan_interactions` | interaction ID | 问答/review、决定、消息与恢复 |
| `subagent_task_children` | child ID 与 task/child_kind | 只有 RESULT、MESSAGE 两类与终态唯一性；worker turn 在 turns 表，不是 TURN child |
| `memory_facts` | fact ID 与 domain/context | basis、关系、embedding；来源 tool result 被删除后仍可存续 |
| `memory_relations` | relation ID 与两端事实 | 对称关系规范化、supersede 语义和 source result |
| `memory_embeddings` | `(memory_domain_id,fact_id)` 复合键 | 跟随事实身份；不是新的 memory 对象 ID |
| `agent_events` | event ID 与 session sequence | 已接受 user-visible transition、typed payload 中业务身份 |
| `history_search_documents` | 派生 document ID | session/q1/variant/include_tools 的可重建投影，无独立业务身份 |

依据：[baseline](../src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql)、[manifest](../src/pulsara_agent/storage/migrations/manifest.py)。`public.pulsara_schema_migrations.version` 是 schema 版本整数，不是待统一的业务短 ID。

## 4 第二轮新增或修正的事实

### 4.1 已有短身份与 scope

项目 workspace/context 已使用 `ctx:workspace/` 加 canonical root 的 SHA-256 前16 hex；临时 workspace 则使用 `transient:` 加完整64 hex。两者来自相同的路径身份问题，却采用不同编码。[路径解析](../src/pulsara_agent/workspace_identity.py)、[context builder](../src/pulsara_agent/memory/scope.py:136)。

workspace 的真正唯一范围包含 memory domain。不能把全部现有 `id` 字段都假定为跨所有 domain 全局唯一，再据此设计统一生成器。路径可变性、符号链接规范化和目录恢复语义也不能被哈希格式替换覆盖。

blob ID 包含 workspace、MIME、codec 与内容摘要的身份输入，与图像内容摘要不是同一个概念。[blob builder](../src/pulsara_agent/conversation_kernel/blob.py:29)。统一命名时必须明确“同字节在不同容器中是否同一对象”，不能靠前缀推断。

### 4.2 fork 同时存在保留身份和新建身份

fork 为复制的 entry、block、tool-result 和 imported history group 生成新身份，但保留原始来源关联、provider tool_call_id、retained artifact_id 等。复制后的 artifact ID 不再等于拿新 result entry 重算的值。

因此不能把全仓现有 `_stable_id` 的前像公式当作任何时刻都可从当前行重算的规则。每类身份需要明确“创建时派生”与“复制时保留”是否不同。[fork repository](../src/pulsara_agent/conversation_kernel/_repository/fork.py)、[历史复制](../src/pulsara_agent/conversation_kernel/fork_history.py)。

### 4.3 删除不等于所有相关身份消失

删除 session 时，memory facts/relations 可以继续存在，来源 tool-result 外键置空；workspace 清理还要考虑 project memory。blob GC 必须确认七类 canonical owner 都不存在：snapshot、entry、assistant block、queue、image ref、visualization、tool output。[GC](../src/pulsara_agent/conversation_kernel/blob.py:310)。

这些是短身份的生命周期输入，而不是迁移细节。清空开发库能省去旧数据兼容，不能省略新系统里正确的 fork、删除和回收行为。

### 4.4 隐藏在共享生成器后的身份家族

`_id(prefix)`、`_stable_id(prefix,*parts)` 分散在 runner、steer、tool execution、compaction、provider dispatch、host 和 plan runtime。只搜索 `sha256:` 或显式 `task_id` 会漏掉调用点传入的前缀。

新增家族包括 plan question/review 的 result/entry/turn、terminal observation 的 turn/entry/context revision、scheduled tick 和取消 queue 的 command。统一规则需要同时核对 helper 与调用参数，不能仅替换 helper 的输出长度。

还有为保留旧 canonical ID 前像而存在的 `_stable_input_identity.py` 与 steer 私有 legacy identity helpers。它们不是模型能力，也不是额外权限；hard cut 要决定是否重新定义这部分创建身份，不能无意保留复杂的旧前像，也不能误删仍参与 provider/source 兼容的真实检查。[稳定输入身份](../src/pulsara_agent/model_input/_stable_input_identity.py)、[steer identity](../src/pulsara_agent/conversation_kernel/steer.py)。

### 4.5 模型可见面的额外遗漏

- `list_agent_models.connection_id` 是模型选择连接时会传回的 UUID handle，不能因没有 SHA 字样排除。
- scheduled task 的公开结果会包含关联的 queue、turn、entry ID；其 next_cursor 是最后一条 task 主键，按主键排序，不能归入“所有 cursor 都是 base64 JSON”。模型 action 与 UI-only `run_now` 要分开。
- `list_agents.model_target` 的 estimator fingerprint 仍是确认的无必要内部暴露；material_refs 仅有 task/status/result，不是图片或 artifact 身份集合。
- Plan workflow/interaction ID 在存储和 UI 中存在，但最终 PLAN_CONTROL 模型投影过滤掉；这不妨碍它们属于 canonical hard-cut 调查范围。
- 第一轮“Hook 摘要只在 GUI”的结论不成立：`reload_capabilities` 的 `plugin_hook_sources` 经共享 `hook_snapshot_public` 暴露 `definition_digest`、`trusted_definition_digest`（裸64 hex，可空）及 `pkg_` + UUID32 的 package_install_id。list/inspect 的过滤不能证明 reload 同样过滤。主代理已复核 [host](../src/pulsara_agent/conversation_kernel/host.py:1444)、[展示函数](../src/pulsara_agent/hooks/presentation.py:14)、[模型工具结果](../src/pulsara_agent/conversation_kernel/tool_runtime.py:3096)。
- terminal monitor 的注册结果还返回 `baseline_cursor`，与 output/retained cursor 属于同一套协议；第一轮漏列该出口。[注册结果](../src/pulsara_agent/conversation_kernel/tool_runtime.py:3826)。

### 4.6 进程局部身份

terminal 有 process、stream、monitor、settlement、subscription、decision 与 observation 身份。TODO 有 `todo-root-run:` 派生身份和 settlement token，worker TODO run 复用子任务 ID。tool surface borrow、host owner、provider epoch 等也有局部身份。

它们需要列入规则分类，但不能因“统一身份”变成 durable 对象。object identity、现有 epoch/nonce、prepared DTO 相等以及真实 owner 验证仍负责执行权。不能让一个短字符串替代物理存活与 owner 校验。[terminal](../src/pulsara_agent/terminal_process)、[TODO runtime](../src/pulsara_agent/conversation_kernel/todo_runtime.py)、[tool runtime](../src/pulsara_agent/conversation_kernel/tool_runtime.py)。

### 4.7 前端与协议依赖

前端依 session/task/batch 等身份组织路由、请求、缓存和选中项。批注 `source.entry_id` 与 UTF-16 范围绑定已保存助手正文；虽然 provider 只看到 quote/comment，主键改造仍需保持 UI 选择到 canonical admission 的一致性。[批注来源校验](../src/pulsara_agent/conversation_kernel/annotations.py)、[前端适配](../frontend/lib/runtime-adapter.ts)。

protobuf 中的 bytes、digest、ID 与 revision 字段必须逐类判断，不能对所有 string ID 作机械替换。图片前端仍有完整摘要校验，这些校验值未必等同于模型图片句柄；同样名字不能替代具体字段职责。[协议](../src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto)、[prompt content](../frontend/lib/prompt-content.ts)。

UI reasoning block 还使用 `${entry_id}:provider-reasoning:${ordinal}` 派生定位，不能只改数据库 entry ID 而遗漏拼接/解析。UI 任务/分组/活动 cursor 与模型 `list_agents` HMAC cursor 是不同协议，需分别核对。

| 消费端接缝 | 需要同步的内容 | 证据 |
| --- | --- | --- |
| 已保存 UI 会话与草稿 | `pulsara-active-session` localStorage、session 到 draft 的 Map；旧开发 ID 明确失效，不做旧 ID fallback | [app](../frontend/app/pulsara-app.tsx:128)、[draft](../frontend/lib/prompt-draft.ts:81) |
| 消息批注 | source entry ID、UTF-16 范围、fork 重映射；provider 原有 quote/comment 投影保持 | [选择来源](../frontend/lib/response-selection.ts:85)、[admission](../src/pulsara_agent/conversation_kernel/annotations.py:35) |
| reasoning 内容定位 | entry 派生 block ID 的生成与解析、前端缓存 | [生成](../src/pulsara_agent/terminal_protocol/canonical_v3.py:946)、[解析](../src/pulsara_agent/terminal_protocol/canonical_v3.py:785) |
| UI task/group/activity 翻页 | 独立 base64 JSON 中的 session/task/batch 与排序锚点 | [UI controller](../src/pulsara_agent/web_app/session_controller.py:3514) |
| canonical 内容读取 | selector 随主键改变，完整 digest/size 校验保持；含新 ID 的正文由原 owner 重建摘要 | [前端读取](../frontend/lib/runtime-adapter.ts:2158)、[字节校验](../frontend/lib/runtime-adapter.ts:5318) |
| provider native replay | 本地 replay row ID 可纳入改造；远端 call/item ID 与 encrypted reasoning payload 保留原协议值 | [Chat call ID](../src/pulsara_agent/llm/adapters/openai/chat_completions.py:1168)、[Responses replay](../src/pulsara_agent/llm/adapters/openai/responses.py:558) |
| 模型连接选择 | connection UUID 可纳入业务范围；model_id/route_id/wire_api 是既有配置或协议值，另行分类 | [公开模型选择](../src/pulsara_agent/conversation_kernel/subagents/launch.py:82) |
| 配置自然键 | plugin/server/tool/skill_path/URI 等不应机械改为 hash；plugin server 名称还包含长度编码 | [capability target](../src/pulsara_agent/capability/source_query.py:49)、[plugin server](../src/pulsara_agent/plugins/mcp_adapter.py:189) |
| UI 文件预览 | read_token 是权限相关的随机 token；相对资源路径与访问 owner 继续保持 | [preview owner](../src/pulsara_agent/web_app/file_preview.py:192) |

scheduled 输入的 `task_id/task_revision` 还进入可信请求 provenance，并随保留请求进入压缩后继；不能只修改管理工具输出。[请求投影](../src/pulsara_agent/model_input/lowering.py:286)。

### 4.8 数据库之外的持久资料与安全边界

workspace MCP trust 文件名由 resolved workspace path 的完整摘要导出，文件内按 server ID 保存 approval identity；它属于用户保存的权限资料，不能假定清空 clean-v0 数据库就已迁移或清除。配置变更后的 trust 失效逻辑仍需保持。[workspace trust](../src/pulsara_agent/capability/workspace_mcp_trust.py)、[MCP 配置承诺](../src/pulsara_agent/mcp_config.py)。

MCP secret generation commitment 是进程密钥参与的 HMAC，terminal launch capability 是32字节随机凭据，协议 schema digest 用于握手兼容检查。这些不能混入普通业务短 ID。修改 terminal 身份与 protobuf 时，必须同步生成脚本和握手版本规则，同时保留 capability 的常量时间比较和关闭失效行为。[terminal gateway](../src/pulsara_agent/terminal_protocol/v3_gateway.py)。

### 4.9 新增身份家族与生成证据

`H64` 表示64位小写十六进制摘要，`U32` 表示 UUID 的32位十六进制编码。以下格式记录已找到的生产创建路径，不宣称所有 SQL text 列只接受这一种格式。

| 家族 | 当前生成形式 | 主要生成与消费证据 |
| --- | --- | --- |
| turn / ROOT user entry | `turn:H64` / `entry:H64`，普通助手 entry 另用 U32 | [steer admission](../src/pulsara_agent/conversation_kernel/steer.py:375) |
| snapshot | `context-snapshot:H64`，fork 用 U32 | [compaction](../src/pulsara_agent/conversation_kernel/compaction/coordinator.py)、[fork](../src/pulsara_agent/conversation_kernel/_repository/fork.py) |
| context binding | `context-revision:H64` 或 `context-binding:H64` | [steer](../src/pulsara_agent/conversation_kernel/steer.py)、[compaction](../src/pulsara_agent/conversation_kernel/compaction/coordinator.py) |
| queue / command | `queue-item:H64`；command 有 UUID、派生 hash 及调用方提供值 | [host ingress](../src/pulsara_agent/conversation_kernel/host.py:2767)、[scheduled cancel](../src/pulsara_agent/conversation_kernel/_repository/scheduling.py:543) |
| history group | `history-group:U32` | [fork owner](../src/pulsara_agent/conversation_kernel/_repository/fork.py:117) |
| assistant block | `block:H64`；fork 用 U32 | [assembler](../src/pulsara_agent/conversation_kernel/assembler.py:405) |
| tool attempt / result | `tool-attempt:U32` / `tool-result:U32`，部分 plan 路径确定性派生 | [tool execution](../src/pulsara_agent/conversation_kernel/tool_execution.py:875)、[plan runtime](../src/pulsara_agent/conversation_kernel/plan_runtime.py:705) |
| replay fragment | `provider-replay:H64` | [本地 row builder](../src/pulsara_agent/llm/provider_replay.py:402) |
| local model call | `model_call:U32` | [resolution](../src/pulsara_agent/llm/resolution.py:171) |
| event | `event:U32` 或 `event:H64` | [通用接受](../src/pulsara_agent/conversation_kernel/_repository/kernel.py:865)、[typed builder](../src/pulsara_agent/conversation_kernel/_repository/contracts.py:1091) |
| subagent message | `subagent-message:H64`；与 result 前像分隔规则也不同 | [message builder](../src/pulsara_agent/conversation_kernel/_repository/contracts.py:1053)、[result builder](../src/pulsara_agent/conversation_kernel/subagent.py:3477) |
| process / stream / monitor | `proc_U32` / `terminal-stream:U32` / `terminal-monitor:U32` | [process](../src/pulsara_agent/terminal_process/manager.py:475)、[stream](../src/pulsara_agent/terminal_process/output.py:379)、[monitor](../src/pulsara_agent/terminal_process/monitor.py:250) |
| terminal 局部操作 | `terminal-monitor-settlement:`、`terminal-output-subscription:`、`terminal-decision:`、`terminal-observation:` 后接 U32 | [monitor](../src/pulsara_agent/terminal_process/monitor.py)、[manager](../src/pulsara_agent/terminal_process/manager.py)、[output](../src/pulsara_agent/terminal_process/output.py) |
| TODO run / settlement | `todo-root-run:H64` / `todo-settlement:U32`，worker run 复用 task ID | [TODO owner](../src/pulsara_agent/conversation_kernel/todo_runtime.py:479) |
| tool surface borrow | `tool-surface-borrow:U32`，同时校验 owner 对象身份 | [borrow](../src/pulsara_agent/conversation_kernel/tool_runtime.py:1543) |
| live interaction / accepted decision | `interaction:U32` / `interaction-decision:H64` | [交互 owner](../src/pulsara_agent/conversation_kernel/interaction.py:271)、[decision](../src/pulsara_agent/conversation_kernel/interaction.py:394) |
| scheduled task / tick | `scheduled:U32` / `scheduled-tick:H64`，另有整数 revision | [service](../src/pulsara_agent/scheduling/service.py:141)、[tick](../src/pulsara_agent/scheduling/service.py:360) |
| plan 派生身份 | `plan-workflow:`、`plan-interaction:`、question-result、review-turn/entry、continuation-turn/entry 等 H64 | [agent plan](../src/pulsara_agent/conversation_kernel/plan_runtime.py:510)、[host plan](../src/pulsara_agent/conversation_kernel/host.py:3278) |
| permission snapshot | `permission-snapshot:H64` 与独立 `sha256:H64` fingerprint | [身份与完整权限事实](../src/pulsara_agent/primitives/run_permission.py)、[plan 接受](../src/pulsara_agent/conversation_kernel/_repository/plans.py:254) |
| terminal launch / attachment | `terminal-v3-launch:U32` / `terminal-v3-attachment:U32`，另有随机 capability | [gateway](../src/pulsara_agent/terminal_protocol/v3_gateway.py:175) |

permission snapshot 的 ID 与 fingerprint 承担不同职责：完整权限事实包含业务身份、mode、admission、overlay 和继承来源。改业务 ID 后相关完整摘要会重新计算，但不能把摘要本身改成短 ID 后省略权限比较。

`plan-unavailable-workflow` 等拒绝候选身份、async task 名称、文件 staging UUID、前端 draft/toast/mermaid DOM ID 同样被扫描到；它们不应未经分类就升级为 canonical 业务对象。

## 5 hard cut 的分类边界

| 类别 | 当前调查结论 | 设计动作 |
| --- | --- | --- |
| durable 业务身份 | 需要统一，并覆盖间接派生和复制规则 | 确定类型词、长度/编码、创建算法、唯一范围、冲突及删除后身份规则 |
| 复合关系与 ordinal | 不一定需要独立 ID | 保持 owner + key/ordinal，禁止为统一外形增加实体 |
| disposable 投影身份 | 可由 canonical 行重建 | 同步重建与查询一致性，不升级为权威主键 |
| 进程局部身份 | 有 owner/epoch/物理生命周期 | 可统一编码约定，但保持易失性与真实 owner 校验 |
| 内容摘要与版本事实 | 支撑完整性和 exact read-before-write | 保留完整校验；是否以业务版本身份供模型操作另行明确 |
| MAC、认证与 capability token | 具有实际认证或绑定用途 | 不按普通短 ID 截断，保留协议强度与失效规则 |
| cursor | 含状态或借助过程局部 owner，生命周期各异 | 按协议处理，不因统一编码丢失 cut/GAP/过期语义 |
| provider/外部服务身份 | Pulsara 不是生成权威 | 原样维护协议身份；如需内部对象 ID，要明确两者分别属于哪个边界 |
| 配置键、逻辑名称、URI 和 task name | 不一定是对象身份 | 不纳入机械短化；检查调用语义后决定 |

## 6 复查时待闭合的实施问题

完成这一轮调查不等于已经可以把所有生成器改成短哈希。冻结实施规格前还需交付：

1. **实体决策表**：对每一类 canonical/live 身份给出改或不改的决定，明确是否保留复合键。不能用“所有 id 一律统一”替代分类。
2. **生成与冲突规则**：确定性、随机、新建、幂等重算、复制保留分别如何使用统一格式；不能凭默认不会撞码跳过错误行为，也不能增加总对象数上限。
3. **调用点与消费者清单**：把共享 builder 的 prefix 参数、SQL 精确查询、JSON 内嵌引用、正则/固定长度验证、协议/UI 路由和缓存、权限 owner 全部对应起来。
4. **生命周期实例**：每类至少覆盖创建、重复请求、冷重启、fork、删除/GC 与碰撞/未知 ID；局部对象还需覆盖 owner 关闭、失效和迟到结果。
5. **外部身份清单**：逐项标出 provider tool-call/response/replay、MCP、OAuth 与连接配置等实际协议值，防止 hard cut 误改外部身份。
6. **clean-v0 与激活方案**：应用表、约束、触发器、manifest/grants/catalog oracle、protobuf 及生成产物、测试 fixture 和说明共同切换；明确重建哪些已验证的本地 disposable 数据。
7. **provider prefix 策略**：新格式和工具定义仅在已有允许的 cold epoch 或明确采用的 compaction successor 安装；不能重写已安装消息。开发库重建不授权修改外部历史或远程配置。

不通过一次宽泛正则替换完成以上工作，也不建立线上迁移兼容链来延续旧开发状态。先冻结具体规则，再做一次范围明确、可测试的 hard cut。

## 7 证据的实际限度

写实施规格时再次核对 baseline，修正了本表原先把 worker turn 混入 `subagent_task_children` 的描述；这张表的 CHECK 只允许 MESSAGE/RESULT。另补充了 compaction/input-continuity reservation、live observer/generation、extension registration 等局部身份的具体切换规则，见实施规格第6节。

独立审阅进一步区分了两个消息方向：incoming mailbox 接受只写 INTER_AGENT_MESSAGE entry 和 ENTRY event；`subagent_task_children.MESSAGE` 是 worker assistant 输出的 child。Plan answer/review 走 resolution command，不应为了编号增加 interaction_decisions 行。最终配方、配置切换及碰撞边界以冻结规格为准。

已完成静态入口、应用表目录和多领域生产链路复查，确实扩展了第一轮清单。尚未通过运行时采样证明所有工具结果、错误分支、Hook/MCP 自由文本和历史内容的实际出现频率；外部服务返回值无法由本仓静态搜索穷尽。

测试和 dogfood 当前只作为未来验证入口，不充当本次审计的通过记录。无数据库重置、无 provider 调用、无生产修改。冻结时需要把上述未闭合项变成具体决定，而不是继续以“全仓已扫过”代替证据。

相关文档：[第一轮模型可见引用调查](PULSARA_MODEL_VISIBLE_REFERENCE_AUDIT_AND_DESIGN.zh.md)、[延期的统一可视化设计](PULSARA_UNIFIED_VISUALIZATION_PRESENTATION_SPEC.zh.md)、[仓库约束](../AGENTS.md)。
