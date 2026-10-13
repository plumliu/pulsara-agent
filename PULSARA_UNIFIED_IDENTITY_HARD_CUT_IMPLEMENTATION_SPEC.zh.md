# Pulsara 统一身份 hard-cut 实施规格

日期：2026-10-14。调查基线：Git `5df79ba9`。

状态：**冻结 v1，代码实施及审阅完成（2026-10-14）**。先由同一 GPT‑6 Astra high 审阅者与主代理复核生产实现，通过后再更新 pytest；最终3289条 pytest、706条前端测试、类型检查和构建通过。真实多模态与工具引用 dogfood 通过；未配置明确纯文本 provider 的验证缺口单独保留。覆盖、证据及启用边界见[实施与验证记录](PULSARA_UNIFIED_IDENTITY_HARD_CUT_VALIDATION.zh.md)。原生产 home 未切换。

依据：[全仓复查](PULSARA_IDENTITY_HARD_CUT_REPOSITORY_AUDIT.zh.md)、[模型可见面调查](PULSARA_MODEL_VISIBLE_REFERENCE_AUDIT_AND_DESIGN.zh.md)、[AGENTS.md](AGENTS.md)。本文实施后取代这些调查中尚未定案的身份设计建议；其调查事实仍以所列代码基线为准。

需保留的已实施行为参见[图片 offload 与历史恢复](archived_docs/PULSARA_IMAGE_CONTEXT_OFFLOAD_AND_HISTORY_RECOVERY_IMPLEMENTATION_SPEC.zh.md)、[文件内容版本编辑](archived_docs/PULSARA_CONTENT_REVISION_LINE_EDIT_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)、[fingerprint subtraction](archived_docs/PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)。这些文档用于定位原行为；其中旧身份外形与前像由本文显式替换，不因此恢复其他历史路径。

## 1 决策与范围

Pulsara 自己拥有的业务对象采用 **`<kind>:<26 位小写 Base32>`**，后缀承载128 bit。数据库、运行时、模型参数、工具结果、历史引用和 UI 使用同一个 canonical ID，不另设模型别名或短码映射层。

“统一”包括生成规则、类型词、精确解析、冲突行为和生命周期；不要求把内容摘要、版本证明、凭据、分页状态、外部协议 ID 也塞进128 bit。复合键与 ordinal 继续存在，不给每个关系新增对象 ID。

本次包含：canonical 主外键及 JSON 中结构化引用、图片 offload 的可操作引用、工具输出 artifact、memory、任务与历史、Plan、调度、terminal/live identity、Pulsara 拥有的配置对象身份，以及模型面不必要摘要字段的移除。没有新旧兼容层、旧前缀别名、模糊查找或过渡双写。

**图片与 HTML 的统一可视化继续延期。** 本文不实现图片围栏、混合图集、缩略图切换或新的 HTML 发布协议。现有 HTML 展示和图片输入/offload 的产品行为继续工作。

本规格授权未来实施重新定义这里列出的本地 canonical ID 前像，替代 fingerprint subtraction 中“为保持旧 ID 而保留旧前像”的要求。该替代只适用于业务身份；不撤销完整内容、权限确认、provider/source/replay 兼容及认证检查。

## 2 编码、长度与生成算法

### 2.1 公共格式

- kind 使用本文表中的固定小写英文词，可以含 `-`；不使用算法名、对象来源或供应商名作为业务类型。
- payload 使用 RFC 4648 Base32 字母表 `abcdefghijklmnopqrstuvwxyz234567`，去掉 padding；不采用 Crockford 字母替换，不接受大小写或字符纠错别名。
- 128 bit 编码为26字符，规范正则为 `[a-z2-7]{25}[aeimquy4]`。最后一位必须满足 padding bits 为零；不能只校验长度。
- 完整 ID 长度为 `len(kind) + 27`。例如 `image:`、`entry:` 为32字符，`task:` 为31字符，`session:` 为34字符。这里指字符数，不承诺相同幅度的 tokenizer 节省。
- 解析必须匹配目标字段的允许 kind 集合、完整后缀及规范编码。禁止 prefix lookup、自动补齐、截短、去掉类型词、把旧 UUID/hex 当作同值或收到错 kind 后换个 owner 搜索。
- 后缀是不可解释的身份值。模型只需复制 runtime 给出的完整 ID，无需知道它来自随机数还是派生算法，也不负责计算 ID。

选择128 bit 而非4～8位短码：它与常见随机对象身份处于相近的空间量级，显著短于64 hex，且不要求短码注册表。对均匀独立身份，生日近似概率是 `n(n-1)/2^129`；同一命名空间累计十亿个候选时约 `1.47×10^-21`。这只是风险量级示例，**不是总对象数上限**。截断摘要的通用碰撞强度约64 bit，不是128 bit；它绝不承担认证或完整性证明。

### 2.2 两种生成方式，只有一种外部格式

`R(kind)`：从操作系统 CSPRNG 取16字节，Base32 编码后加 kind。Python 使用 `secrets.token_bytes(16)` 与 `base64.b32encode`；浏览器需要自行创建 command/request ID 时使用 Web Crypto。不得使用时间戳、进程计数、`Math.random`、普通 PRNG 或先生成更弱短码再补长度。

`D(kind, role, parts)`：对以下 UTF-8 字节计算 SHA-256，取摘要的前16字节，Base32 编码后加 kind：

```text
compact_json(["pulsara-id-v1", kind, role, ...parts])
```

规范化输入是只包含字符串的一维 JSON 数组。使用无多余空白的标准 JSON，保留合法 Unicode 字符，不转义 `/`；拒绝孤立 surrogate。不对普通文本自动 trim、大小写折叠或 Unicode normalization。Python 对应 `json.dumps(values, ensure_ascii=False, separators=(",", ":"))`。

数字以无前导零的十进制字符串传入；缺失值在需要该位置时用空字符串，且相关业务字段不允许把空字符串当作另一个合法实体。时间按原 owner 的精度规范成 UTC；scheduled due time 固定为六位微秒的 `YYYY-MM-DDTHH:mm:ss.ffffffZ`。路径使用现有 workspace/path owner 的 resolved canonical path，不另写路径规范化器。

role 是本文指定的创建原因，属于派生协议，不出现在公开 ID 中。例如 user entry、steer entry、Plan review entry 都是 `entry:`，用 role 区分创建来源。禁止使用任意 DTO 的 `repr`、分隔符拼接、无序集合或未冻结可变字段作为前像。

复用标准 SHA-256、JSON、Base32 和随机源；Pulsara 只拥有这些原语之间的短小编码函数、类型校验和各 owner 的身份配方。不建立通用身份服务、跨实体注册表、自动派生框架或插件化 kind registry。

### 2.3 可复核向量

以下是编码测试向量，零值 owner 只是算法测试输入，不代表存在或获得授权的对象。

| kind / role / parts | 结果 |
| --- | --- |
| `task` / `spawn` / `["attempt:aaaaaaaaaaaaaaaaaaaaaaaaaa", "0"]` | `task:htl27gtxmwe2htij6arwsvscbq` |
| `entry` / `root-user` / `["turn:aaaaaaaaaaaaaaaaaaaaaaaaaa"]` | `entry:bfsgawrz4vz4mqz5utg5ld4zey` |
| `image` / `content` / `["workspace:aaaaaaaaaaaaaaaaaaaaaaaaaa", "image/png", "test-codec", "sha256:0000000000000000000000000000000000000000000000000000000000000000"]` | `image:3rabtsk47f56hr576ld5zfdbja` |

字节 `00` 重复16次编码为 `aaaaaaaaaaaaaaaaaaaaaaaaaa`。实现必须同时覆盖非 ASCII、分隔符、空字符串、非规范最后一位和非法旧格式的向量。

## 3 碰撞、幂等与删除后的限制

### 3.1 分配和接受

| 情况 | 必须行为 |
| --- | --- |
| `R` 尚未发布，现有 owner 的唯一约束发现占用 | 在该 owner 的创建流程中重新生成，重新构造未发布的依赖值后再尝试；受调用取消和已有操作 deadline 约束，不新增全局重试/寿命上限 |
| ID 已返回、进入已安装 provider input、已冻结待批准操作或已提交 | 不再因冲突换号；当前接受明确失败，不能把同一请求悄悄变成另一个对象 |
| `D` 命中已有 ID，且同一语义创建请求、相同不可变 owner 字段 | 返回已有对象或原接受结果；保留现有语义幂等检查，不能只看主键 |
| `D` 命中已有 ID，但来源/owner/内容不一致 | `IDENTITY_CONFLICT`，整个关联写事务回滚；禁止加盐重算、增长后缀、覆盖旧行或只返回第一条 |
| 格式不规范、类型错误 | 按既有参数错误载体返回 `INVALID_ID`；拒绝旧格式而非尝试转换 |
| 合法 ID 但当前 caller 无权读或对象不存在 | 沿用资源的 unavailable/not-found 契约，不通过错误透露另一个 scope 中的存在性 |

`IDENTITY_CONFLICT` 是既有冲突异常/结果载体中的确定错误码，不新增 durable event、告警流水或 repair job。SQL PK/UNIQUE 负责并发排他；接受 owner 负责现有来源字段、scope 和实际完整内容比较。进程对象则在原 owner 的 live collection 中完成同等检查。

冲突检测只使用既有表约束、live collection、slot、冻结对象和原引用计数。若某局部 token 本来仅由 retained object/seal 证明归属，就保持该比较；不为“证明随机值不碰撞”新增 registry、保留期或持久记录。不同 owner 可以使用同一个 kind，字段的 typed owner 仍是解析边界，kind 不承担 owner 认证。

已有行的来源因原生命周期规则置空或资料已不可比较时，不得仅凭相同 ID 宣称这是幂等重试；没有现有接受事实可证明同一请求，就返回冲突。不要为这种罕见冲突新增来源保留表或延长原数据生命周期。

随机创建必须在任何外部可观察动作之前确定身份；副作用发生后遇到冲突，执行原有失败/结算流程，不重跑副作用来换一个 ID。客户端提交 command ID 的重试必须带同一 ID；相同 command 携带不同请求内容仍是幂等冲突。

### 3.2 身份不会被主动回收分配

新发生的随机对象总是重新抽取；禁止空闲 ID 池、根据短前缀唯一性重新解释旧 ID、以路径内容变化覆盖原图片，以及删除后给另一个对象主动复用 ID。

内容派生对象在删除后重新发布**完全相同身份输入**可以恢复同一 ID；它表达同一个内容对象。发生身份输入变化则必须得到新 ID，除非出现真实摘要碰撞。路径身份仍表达位置，同路径目录被重新创建可得到同一 workspace ID，这不是目录物理世代身份。

**有限空间加无永久墓碑，不能证明删除后永远不会随机复现或产生截断摘要碰撞。** 本规格接受128-bit canonical 身份的剩余概率风险，不承诺数学上的零碰撞；当前仍有行/owner 时必须检测冲突，原对象及比较资料全部 GC 后不保证检测历史碰撞。不新增永久身份账本来伪造该保证，也不允许据此省掉正常精确 owner 校验。

如产品以后要求零历史复用或针对主动制造身份碰撞的更强保证，需要另立身份契约。本文不会把“当前唯一的短前缀”作为替代方案。完整字节摘要、MAC 和权限绑定继续提供各自原有强度。

## 4 持久业务对象：闭合的类型与创建规则

下表中的 R、D 均遵守第2节；括号里的数字是完整字符串长度。`session` 等词在 parts 中指完整的新 ID，ordinal 指现有 owner 的顺序位置。类型不因 ROOT/worker、用户/工具或正常/fork 路径改变。

| 对象 / kind（长度） | 新建规则 | 唯一范围、重启与生命周期 |
| --- | --- | --- |
| workspace / `workspace`（36） | project：`D(workspace, project, [canonical_root])`；transient：`D(workspace, transient, [canonical_root])` | 保持 `(memory_domain_id,id)` 主键；相同路径在不同 domain 可同 ID，权限不合并。原 root/kind 冲突检查保留。路径迁移是新 workspace，不追踪 inode |
| 会话 / `session`（34） | `R(session)` | canonical session；冷重启保留，fork 新建，删除后不可恢复为别的会话 |
| 非图片内容 / `blob`（31） | `D(blob, content, [workspace, media_type, codec, full_digest])` | 仍由 blobs 表拥有，全字节精确去重；七类 owner 全失去后方可 GC |
| 图片内容 / `image`（32） | `D(image, content, [workspace, validated_media_type, image_codec, full_digest])` | 图片也是 blobs 行，见第5节；没有第二套 image 实体或引用注册表 |
| command / `command`（34） | 外部 Pulsara caller：`R(command)`；内部稳定命令见下表 | 保持 `(session,command_id)` 幂等域；调用方不能再传任意自定义字符串；网络重试不换 ID；控制命令的 admission deadline 独立进入原 typed request / terminal payload，不再编码在 ID 中，原过期和结果保留规则不变 |
| prompt queue / `queue`（32） | `D(queue, ingress, [session, command])` | queue sequence 和接受/消费关系保持；重复 ingress 返回同一次接受 |
| turn / `turn`（31） | 按下表的固定原因派生 | 归属 ROOT/worker 等仍由 typed owner 决定，不由 ID 前缀赋予执行权 |
| transcript entry / `entry`（32） | 新助手消息和无稳定请求来源的 user-control/workspace-recreated feedback 用 `R(entry)`，首次冻结后复用；接受输入与派生消息见下表 | 所有 entry 用同一 kind；session sequence 独立；fork 新建、保留 origin 关联 |
| compaction snapshot / `snapshot`（35） | 显式请求 `D(snapshot, explicit, [session, command])`；自动压缩 `D(snapshot, compact, [session, turn, trigger_kind, source_digest])`；fork `D(snapshot, fork, [child_session, anchor_entry])` | 同创建原因但内容漂移是冲突；source/content 完整摘要仍比较；fork 保持既有 AWAIT_NEXT_USER 契约 |
| context binding / `binding`（34） | `D(binding, revision, [turn, revision_ordinal])` | 原 turn/revision 唯一约束保留；指向的 snapshot 与 prefix/source 事实必须精确一致 |
| imported history group / `history`（34） | `D(history, fork, [child_session, source_group_kind, source_group_id])` | 只用于导入历史分组，不创建过去的执行 turn |
| assistant block / `block`（32） | `D(block, message, [entry, block_kind, provider_identity])` | 在 Start 冻结并贯穿 live/commit；保留原 adapter 提供的块来源，不由过滤 thinking 后的持久 ordinal 重算。顺序仍由原 Start ordinal 决定 |
| local replay row / `replay`（33） | `D(replay, assistant, [session, assistant_entry, wire_api])` | 本地 row 新身份；远端 payload、call/item ID、加密 reasoning 不重写 |
| tool execution / `attempt`（34） | 首次冻结该调用时 `R(attempt)` | 仍由 `(session,assistant_entry,tool_call_id)` 约束一次执行；审批前预分配，批准/取消不得换号 |
| tool result / `result`（33） | 常规执行、预审批及拒绝结果首次冻结时 `R(result)`，同一冻结操作复用；Plan 的稳定闭合见下表 | 不依赖一定存在 attempt；unavailable、policy/hook/user DENY 可没有 attempt。保留预分配与结算流程，不补造执行记录 |
| retained output / `artifact`（35） | 首次接受 `D(artifact, tool-output, [session, original_result_entry])` | 保持 `(session,artifact_id)` 归属与 AVAILABLE/INCOMPLETE；fork 保留该 ID，不按新 result entry 重算 |
| local provider call / `call`（31） | 每次实际调用 `R(call)` | 对应本地 resolved_model_call_id/usage；重试发起新调用是新 call，远端 request ID 独立 |
| 子代理批次 / `batch`（32） | `D(batch, spawn, [attempt])` | 所属接受调用稳定；不因重试重建一批任务 |
| 子代理任务 / `task`（31） | `D(task, spawn, [attempt, task_ordinal])` | task_key、依赖和队列规则保留；等待/取消/材料读取使用同一 task ID |
| 子代理完成 / `agent-result`（39） | `D(agent-result, completion, [task, terminal_entry])` | 当前 subagent_task_children 的 result 子项；不与 tool result 混淆 |
| worker 助手消息 child / `agent-message`（40） | `D(agent-message, message, [task, assistant_entry])` | 只在原 worker assistant message 接受路径创建现有 MESSAGE child；不是发给 worker 的 incoming mailbox，也不新增投递日志 |
| accepted decision / `decision`（35） | `D(decision, accepted, [session, command])` | 对应原 interaction_decisions；相同命令的交互目标和决定不一致必须拒绝 |
| Plan workflow / `plan`（31） | tool：`D(plan, tool, [session, assistant_entry, external_tool_call_id])`；UI：`D(plan, user, [session, command])` | workflow revision/phase 继续是既有字段；编辑同一计划不换 ID |
| Plan interaction / `plan-interaction`（43） | `D(plan-interaction, tool, [plan, assistant_entry, external_tool_call_id])` | 现有工具创建 Q&A/review interaction；UI 回答/审阅引用已有 interaction，不额外创建一个 UI interaction 或 decision 行。最终 PLAN_CONTROL 投影不增加 ID |
| permission snapshot / `permission`（37） | `D(permission, admission, [turn])` | 同一次 turn admission 冻结；mode/overlay/继承来源等完整事实和 fingerprint 保留，不以短 ID 授权 |
| scheduled task / `schedule`（35） | `R(schedule)` | edit 保留 ID、递增整数 revision；取消/删除不创建同 ID 的新任务 |
| memory fact / `memory`（33） | `D(memory, remember, [session, result])` | statement 编辑不换身份；domain/context/basis/ACTIVE 检查保留；session 删除后可继续存在 |
| memory relation / `relation`（35） | `D(relation, memory, [domain, source_context, target_context, kind, supersede_mode_or_empty, source_memory, target_memory])` | 对称关系沿用 `(context_id, memory_id)` endpoint 的规范排序，同时交换对应 context/fact；有方向的关系不排序。独立 source result 不是关系身份 |
| accepted event / `event`（32） | 有幂等来源：`D(event, accepted, [session, transition_kind, source_kind, source_id, existing_occurrence_key])`；没有稳定接受来源的现有一次性事件：`R(event)` | 不新增事件种类；occurrence_key 使用原 transition 的既有判别值，只有每来源该 kind 确实单次时才为空；禁止用可变 payload 摘要代替来源 |
| 模型连接 / `connection`（37） | `R(connection)` | 配置 owner 创建并保存；编辑连接保留 ID，删除重建分配新 ID；所有 route/default/credential 引用同步，见第10节 |
| 插件安装实例 / `package`（34） | 每次新安装实例 `R(package)` | plugin 名称是自然键，package 是安装 incarnation；原 trust/版本/卸载规则保持，配置准备见第10节 |

### 4.1 固定派生路径

以下条目消除当前 `steer-entry`、`plan-review-turn` 等因来源不同形成的额外身份前缀。表中 parts 的顺序就是前像顺序；不要保留旧公式再包一层短 ID。

| 路径 | 新公式 |
| --- | --- |
| ROOT command 入场 turn | `D(turn, command, [session, command])` |
| queue 转为新 ROOT turn | `D(turn, queue, [session, queue])` |
| worker turn | `D(turn, worker, [session, task])`；当前一次逻辑任务的既有延续继续同 turn，不把每轮模型调用当成新 turn |
| ROOT 首条 user entry | `D(entry, root-user, [turn])` |
| worker 首条 entry | `D(entry, worker, [turn])` |
| queue 作为 steer 插入 | `D(entry, steer, [session, queue])` |
| 普通 tool result entry | `D(entry, tool-result, [result])` |
| Plan 无 attempt 的工具闭合 result | `D(result, plan-control, [session, assistant_entry, external_tool_call_id])`；entry 仍用 `tool-result` 公式 |
| Plan question 接受结果 | `D(result, plan-answer, [session, command])`；entry 用 `D(entry, tool-result, [result])`。同 command 的 interaction/answer 仍按完整语义检查，不新增 accepted decision |
| Plan review turn / user entry | 仅原 APPROVE/REVISE 分支生成 `D(turn, plan-review, [session, command])` / `D(entry, plan-review, [turn])`；拒绝等不创建 turn，decision 仍是既有枚举 |
| 首次工具 enter_plan 的自动 continuation turn / entry | `D(turn, plan-enter, [session, plan, assistant_entry, external_tool_call_id])` / `D(entry, plan-enter, [turn])`；此路径没有 plan_interaction。binding 按该 turn 的 revision ordinal |
| terminal observation 的接受 turn / entry | `D(turn, terminal-observation, [session, observation])` / `D(entry, terminal-observation, [turn])`；permission/binding 复用标准 turn 公式 |
| scheduled tick command | `D(command, scheduled-tick, [schedule, task_revision, due_at_utc])`；queue 复用 ingress 公式，不另造 tick 实体 |
| scheduled cancel command | `D(command, scheduled-cancel, [schedule, task_revision, queue])` |
| 子代理完成与 command | 当前自动投递不创建 command；人工投递复用 caller 提交的 `R(command)`，按原 command 幂等语义绑定接受结果。 |
| 子代理完成引发的 ROOT turn / entry | 已由 queue 承载时用标准 queue turn；显式用户 command 请求新 ROOT 时按 `D(turn, command, [session, command])` / `D(entry, agent-completion, [turn])`。当前自动完成只向活跃 ROOT 投递，不凭空建立 runtime command 或新 ROOT。 |
| 子代理完成投递到已有 ROOT | `D(entry, agent-completion-active, [turn, task])`；同一 turn 可接收多个 task；原每个 task 最多接受一次完成的关系约束保持。任务失败/取消可能没有 agent-result，不制造结果占位 ID。 |
| 子代理 incoming mailbox message entry | `D(entry, agent-message, [sender_attempt, recipient_task])`；首次入 mailbox 冻结，重试复用原 item/ordinal；接受只写原 INTER_AGENT_MESSAGE entry 和事件，不创建 MESSAGE child |
| 子代理 message 接受事件 | `D(event, accepted, [session, "INTER_AGENT_MESSAGE_ACCEPTED", "ENTRY", message_entry, ""])`；原 subject 是该 entry，不新增消息投递 event |
| fork 的复制 entry / block / tool result | `D(entry, fork, [child_session, source_entry])` / `D(block, fork, [child_session, source_block])` / `D(result, fork, [child_session, source_result])` |

已有 source_kind/source_id/occurrence 的 typed 接受事实必须在各 owner 内显式映射，不能开放任意 role 字符串让业务调用者创造新协议。实施时若找到表中不存在的独立语义创建路径，应先把其来源、目标 kind 和配方补入本文；不能归入“其他哈希”后悄悄保留旧格式。

block_kind 固定为 assembler 的 `text`、`thinking`、`data`、`tool`；provider_identity 来自该 Start 的原 block_identity，tool 必须等于外部 tool_call_id，native item identity 保持原样。Pulsara adapter 自己合成的 text/thinking 分段身份统一 `R(segment)`（34字符），仅供当前 stream 组装；它不是远端 provider ID，也不是新的 canonical 表。block ID 在 Start 确定，thinking 仍只在原 live/replay 通道中存在，不因为统一 ID 新增持久 thinking block。现有完整 block 值和调用 tuple 校验继续执行。

没有合法 Plan workflow 的拒绝路径不生成假的 `plan-unavailable-workflow` 对象。原拒绝事实使用现有可空/判别类型表达不可用，不新增 canonical plan、event 或 subject。此处属于内部契约 hard cut，需同步所有调用者，不能靠占位 ID 通过校验。

## 5 图片：复用 canonical blob 身份

### 5.1 一个对象，一个可操作 ID

通过现有图片输入验证、采用 `PROMPT_IMAGE_BLOB_CODEC` 保存的图片 blob，其 `blobs.id` 就是 `image:<26>`；通用非图片 blob 使用 `blob:<26>`。选择 image 类型需要现有可信图片校验结果及图片 codec，不能只凭调用者声称 `media_type=image/*`。

`CanonicalContent.blob_id`、prompt image descriptor、`canonical_image_refs.blob_id`、入场/压缩 carrier、读取 selector 和 fork 都引用这个同一 ID。blob API 的字段允许 `blob` 或 `image` 两个 kind；纯图片读取端只接受 `image`。内部字段名 `blob_id` 可以保留，因为存储 owner 仍是 blobs 表。

当前 `LLMImagePart.content_digest` 只有字节摘要，不能靠截短它得出新图片 ID。入场 publication/preparation 必须使用明确的 workspace、验证后 MIME、既有 image codec 和完整摘要计算同一 `image:`；canonical image descriptor 显式携带该 canonical 引用，hydration、compaction、历史投影把它传到 lowering。原始未绑定 workspace 的图片字节 DTO 仍可用于验证，但不能自行输出可恢复标签。lowering 的图片标签函数接收已绑定的完整引用，删除“从 content_digest 推导公开引用”的旧路径。这里携带的是现有 blob 主键，不是新的 alias 字段；provider 原生图像 payload 仍按原 SDK 协议发送字节，不把内部 ID 当成图片 URL。

绑定必须发生在最终 `FrozenCanonicalPrompt`、canonical body、command 语义及资源 quote 冻结**之前**，不能到 `materialize_*` 写库时才补 ID。新增 descriptor 引用字节计入原 canonical-body、multipart、expanded-byte 等实际报价；hook claim、quota/admission 和 persistence 使用同一个已绑定冻结值。验证阶段无 workspace 的 DTO 只承担原图像解码/物理边界检查，不能把其初步测量当成最终 canonical quote。hydration 必须逐项核对 descriptor image_ref、ref 行 blob_id 和 workspace/完整摘要配方，禁止修补已冻结正文。原发布事务之外不增加新 owner 或提交步骤。

提前计算候选 ID 不授予读取资格：直到原 owner 事务接受后，才存在可 exact-join 的权限来源。必须分别验证纯文本 offload、实际视觉输入、view_image 返回图和 HTML 截图的绑定路径，不能仅更换正则。

不新增 `image_id -> full_sha` 表、nullable alias 列、图片主表、双主键或双读协议。`storage_identity` 继续从 canonical ID 构造；完整 `logical_digest`、size、media_type、codec 和原字节继续由原 blob owner 保存并校验。

这项决定明确改变原来的引用语义：图片公开引用不再是全局字节 SHA。相同 workspace、相同验证后 media type/codec、相同原字节，得到同 ID；换 workspace、重新编码、裁剪或截图产生不同身份输入。相同内容同时出现在多个有权限的 session 中可以共用 blob，但它们的读取资格仍各自建立。

### 5.2 发布与读取

发布沿用当前 blob 事务：算完整摘要和新 ID，INSERT/冲突后精确比较 workspace、media_type、codec、摘要、长度及字节。不匹配则拒绝该次发布，不能复用已有行。只改变 ID 不增加提交步骤、receipt 或 generation。

`view_image.image_ref`、图片标签、纯文本历史占位和恢复结果直接携带 `image:<26>`。读取使用**完整 ID 等值查询**，再 exact-join 当前 session/workspace 的合法 `USER_MESSAGE`、`USER_STEER`、`TOOL_RESULT` 或 snapshot owner。queue 仍不是可读取权限来源；跨会话搜索得到 ID 不自动给当前会话授权。

同一图片可以有多个已接受 owner；按现有成本/顺序选择一个 owner 读取是合法的，因为这些行指向同一个 blob 主键，不是从多个前缀匹配对象中挑第一个。完整内容与 descriptor/owner 引用都必须通过校验。

### 5.3 压缩与中立占位

只将新身份串接入已冻结的 offload 行为，不改 recent 原图保留窗口、不建立永久图片清单、不强迫摘要保留全部图片引用，也不让 runtime 每轮重发所有历史图片。

图片退出视觉上下文后，已有占位结构保留图片身份及原有客观元数据，例如 `image_ref=image:3rabtsk47f56hr576ld5zfdbja`。它说明“这里曾有一张可定位图片”，不使用“必须重新读取”“请先查看原图”等任务指令，不声称已理解图片内容。

引用后来被自由文本摘要遗忘时，显式历史 search/read 可以从保存的结构化消息中恢复当前规范 ID；不增加必须搜索历史的 system 指令。纯文本模型能读引用和文字信息，但 `view_image` 不因此获得视觉能力，也不自动启用其他模型代读。

压缩与 fork carrier 中的结构化 identity 由原 serializer 重建并重新计算完整内容摘要；不通过全文正则替换用户正文、历史解释或 provider 原生 replay payload。子代理只获得现有上下文传递契约允许的材料。

## 6 易失身份与投影定位

以下对象也使用128-bit规则，但不因此写库。冷重启、owner 关闭或既有失效条件到达后，返回 unavailable/stale；没有 durable recovery 新承诺。

| owner / 新 kind（长度） | 生成与生命周期 |
| --- | --- |
| terminal process / `process`（34） | `R(process)`；仍按 host/session 检查实际进程；结束记录、淘汰、kill/release 遵守原 owner |
| terminal stream / `stream`（33） | `R(stream)`；同一次输出流，cursor 绑定它和原 owner epoch；裁剪导致 GAP，不生成新身份掩盖丢失 |
| terminal monitor / `monitor`（34） | `R(monitor)`；原 DORMANT→提交后 ACTIVE、取消与关闭规则保持 |
| monitor settlement / `monitor-settlement`（45） | `R(monitor-settlement)`；仅用于原 prepared object，仍比较 `retained is prepared` 与 owner |
| output subscription / `subscription`（39） | `R(subscription)`；订阅结束失效，原 callback/drain/lease 语义保持 |
| terminal local decision / `terminal-decision`（44） | 无 canonical 决定时 `R(terminal-decision)`；若已有 attempt/decision 身份，继续复用其 typed ID，不再重新编码成另一个对象 |
| terminal observation / `observation`（38） | `R(observation)`；同一冻结观察重试复用，接受产生第4节 turn/entry，原 ACK exact-join 保持 |
| ROOT TODO run / `todo-run`（35） | `D(todo-run, root, [session, turn])`；进程 owner/epoch 另校验。worker run 复用 `task:`，不另造 run |
| TODO settlement / `todo-settlement`（42） | `R(todo-settlement)`；仍需冻结 DTO、run 和 owner 精确一致 |
| tool surface borrow / `borrow`（33） | `R(borrow)`；仍需真实 borrow 对象、authority object、generation 及当前 surface；不是 bearer capability |
| pending UI interaction / `interaction`（38） | `R(interaction)`；现有 pending/dormant、取消和超时规则保持；与持久 `plan-interaction`、accepted `decision` 分开 |
| host instance / `host`（31） | `R(host)`；用于实际 host incarnation；持久 session ID 不随之改变 |
| terminal protocol launch / `launch`（33） | `R(launch)`；launch capability 仍独立使用原32随机字节 |
| protocol attachment / `attachment`（37） | `R(attachment)`；重连新建，旧附件按原协议失效 |
| 浏览器传输连接 / `transport`（36） | `R(transport)`；不与模型 `connection:` 混淆；仅该连接存在时有效 |
| 本地协议请求 / `request`（34） | `R(request)`；包括本地 detach request。只有 transport request 相关性，不代替持久 command 幂等 |
| 局部操作 nonce / `operation`（36） | `R(operation)`；resume/reopen/close 等保留原 owner 的生命周期与比较方式 |

下列容易被共享 helper/nonce 名称掩盖的进程身份也属于本次切换。它们不是新增持久对象，kind 的长度同样按 `len(kind)+27` 计算：

| 当前身份家族 | 新 kind 与生成 | 保持的边界 |
| --- | --- | --- |
| compaction-attempt | `R(compact-attempt)`，42字符 | 原 CompactionAttemptToken 的 scope、trigger、seal；同一冻结请求复用，不凭字符串重建 token |
| compaction-request | `R(request)`，34字符 | 原 manual request owner；command 幂等另外校验 |
| compaction-summary assembler proposed entry | `D(entry, compaction-summary, [full_request_fingerprint])`，32字符 | 只用作原汇总调用的流式定位，不新增 transcript entry；request 完整兼容检查保持 |
| provider-input-epoch | `R(epoch)`，32字符 | 只在原 cold/adopted-successor 边界生成；append 沿用原 epoch |
| provider-input-permit / planning | `R(permit)` / `R(planning)`，33/35字符 | 原 issued permit、planning 对象及 scope/revision 精确比较 |
| empty-bootstrap-lease / empty-preparation | `R(bootstrap)` / `R(preparation)`，36/38字符 | 原已有局部 lease/reservation；不新建持久租约或恢复语义 |
| assistant-replay-reservation | `R(replay-reservation)`，45字符 | 原 slot、fragment、object seal、scope/epoch/revision |
| extension-registration | `R(extension)`，36字符 | 原 lease generation、过期和撤销规则 |
| live-observer / live-control-subscriber | `R(observer)` / `R(subscription)`，35/39字符 | 原 ring、subscriber、owner epoch 与退出 |
| live-generation 派生字符串 | 普通 draft 为 `D(generation, draft, [draft_identity])`；task operational channel 为 `D(generation, task, [task])`；其余既有 channel 为 `D(generation, channel, [channel_kind, source_id])`，37字符 | 原 draft/settlement 精确对应；字段本来是整数 generation 的不改类型；使用现有 typed channel kind 与来源字段，不从显示文本解析 |
| terminal-owner | `R(terminal-owner)`，41字符 | 原 process manager incarnation，cursor/monitor 不跨 owner 重认 |
| postgres-borrower | `R(borrow)`，33字符 | 原连接 provider 的 release/引用计数；不转成数据库授权令牌 |
| mcp-candidate | `D(mcp-candidate, installation, [full_candidate_compatibility_digest])`，40字符 | 原 SDK discovery/config/slot lease/policy 完整比较保持；短 ID 不代替候选事实或 owner |
| Pulsara MCP connection slot / dispatch permit | `R(slot)` / `R(permit)`，31/33字符 | 原 supervisor epoch、server、connection generation、slot lease、retained permit 与 authority object 全部保留；这不是 SDK 自有 ID |
| Pulsara MCP ref settlement | `R(settlement)`，37字符 | 原新 tool_ref 的冻结 result entry、scope/epoch 与单次结算；tool_ref 本身仍是完整 HMAC |
| no-continuation fence | `R(fence)`，32字符 | 原 installed predecessor、candidate/cut/destination、scope 与 seal；不新增阻止续轮的产品分支 |
| model-context / final-steer / prospective-root / compaction-variant / hook-sibling，以及 compaction summary / destination summary / repair 的实际 context | 每个新的 compile/summary candidate `R(context)`，34字符；持有该 candidate 的各阶段复用 | 覆盖 provider_dispatch 和 compaction/model_call 的创建路径。完整 source/prefix/compiled compatibility 检查另行保持；不以 context ID 判断内容相等或保留旧 DTO 前像来求同号 |
| 非执行 structural projection 的 wire-plan context | `D(context, structural, [canonical_identity_fingerprint, placements_fingerprint, compile_binding_fingerprint])`，34字符 | 三项沿用原 `_wire_plan_semantic_identity` 已有完整边界值。只适用于现有 `FrozenModelInputSemanticProjection` 闭合类型的不可执行测量；不新增字段/实体或赋予执行权 |

`context-source:<slot>`、`descriptor:<name>`、`credential:<name>`、`mcp-direct:<server>`、`route-wire:<route>` 等由实际名称/槽位构成的标签按逻辑名称处理，保留原含义；不为它们各建随机实体。MCP SDK 自有 transport/request/session 身份由 SDK 原样拥有；Pulsara 自己的候选、slot、permit 和 settlement 按上表处理。

structural projection 的三项分别取 `canonical_input_identity.identity_fingerprint`、`compiled_message_placements_fingerprint(message_placements)` 和现有 `compile_binding_fingerprint`。wire measurement 明确按上述 typed 非执行投影分支处理；有 context 的 compiled/summary/repair 必须携带其冻结 `context:`。删除 `getattr` 缺字段后通用猜测的身份路径，不能把错误 DTO 降级为结构投影，也不能把正常非执行测量误称为旧协议兼容。原完整 structural semantic 比较保持，短 context ID 不代替它。

保留 Python object nonce、现有整数 revision/generation 及状态枚举的原类型；不用字符串 ID 替换本来无需序列化的对象身份。内部 async task 名称、临时目录随机尾缀、文件 staging 名、DOM/toast/Mermaid ID 继续由原库或局部 owner 生成，不是业务对象，不纳入这张类型表。特别是 `interaction-settlement:<interaction_id>` 只是现有 asyncio task 名，不能据它新增 settlement token 或分配记录。

UI reasoning locator 继续是 `entry_id + ":provider-reasoning:" + decimal_ordinal`，属于明确的结构化投影定位，不是第二个 block 主键；生成和解析一起调整，不能把任意冒号拆分当作 entry ID。history search document 仍由 `(session,q1,variant,include_tools)` 重建；不创造 `search-document:` canonical 实体。

`source_turn_ref` 原样使用该条 canonical entry 的所属分组 ID：EXECUTED_TURN 为 `turn:`，IMPORTED_HISTORY 为子会话 `history:`。`current_turn_ref` 与 `immediate_predecessor_turn_ref` 仅来自本会话真实 EXECUTED_TURN，仍为 `turn:`。history 引用只表达导入归组，不创建执行 turn、不指向源会话的执行权限，也不成为当前或直接前驱。没有前驱时保持原 absent 语义；当前来源是否新鲜仍由实际会话、turn 顺序与原 freshness owner 判断，不靠 ID 自身证明。旧源 SHA 派生器删除，不保留“短引用→源哈希”映射。

## 7 完整摘要、版本、token 与 cursor

### 7.1 内容与文件版本

内部 immutable bytes、source/prefix/replay、权限及 Plan 用户确认等真实边界继续使用完整 SHA-256 或原 MAC。现有 `sha256:<64hex>`、需要裸 hex 的协议字段可以继续存在于这些**明确的内部字段**；不把全部文件里的 sha256 视为待删除对象身份。

文件工具的 `content_revision`、`base_revision`、`current_revision` 统一为 **`revision:<52 位小写 Base32>`**，编码的是完整32字节 SHA-256，完整长度61字符；正则后缀为 `[a-z2-7]{51}[aq]`。这是内容版本证明的无损表示，不是128-bit业务 ID，也不是新建版本对象或映射表。空文件的向量为：

```text
revision:4oymiquy7qobjgx36tejs35zeqt24qpemsnzgtfeswmrw6csxbkq
```

工具参数解析可直接解码回完整32字节，与当前文件完整摘要比较。read-before-write 的路径 observation、已见行/缺口、replace_file 的既有覆盖要求、staging 逐字节比较、原子发布和写后验证全部保留。冷重启后即使 revision 仍指向相同字节，丢失的读取 observation 也不能由该字符串恢复；保持原重新读取要求。文件 A→B→A 可以恢复相同 revision，因为它表示内容而非写入次数。

这意味着不是所有模型可复制值都只有26位；文件版本这一类不引入新持久状态，也不降低既有确认强度。原始文件、用户正文、外部工具结果中的 SHA 值原样保留。

### 7.2 认证和外部身份的明确例外

| 值 | 格式/强度与决定 | 生命周期/owner |
| --- | --- | --- |
| late MCP tool_ref | 原 `mcpref_` +43字符 Base64url，完整 HMAC-SHA256，不截短 | 原 scope/epoch/capability/policy/route 绑定，不能跨 owner 猜测重建 |
| MCP secret-generation commitment | 原 `hmac-sha256:` +64 hex，不改为对象 ID | 原进程密钥和 secret-generation owner |
| terminal launch capability | 原32随机字节及现有 wire 编码 | 常量时间比较；launch 结束失效；不是 `launch:` ID |
| UI file preview read_token | 原32随机字节 Base64url43字符 | 原权限和预览 owner；关闭/失效规则保持 |
| provider call/item/response/replay ID、OAuth state、SDK request state | 外部原协议，长度由原 owner 决定，不增加 Pulsara 前缀 | 原协议负责；Pulsara 不在 native payload 内改写 |
| plugin/server/tool/model/route/skill_path/URI/domain 自然键 | 原输入规则，包括 plugin server 长度编码 | 名称与路由 owner；domain 仍是现有配置值，不新增 domain 表 |
| global memory context sentinel | 保留 `ctx:global` 常量；project context 直接使用 `workspace:` | sentinel 是固定语义，不是第二套随机身份；transient 仍不自动拥有 project memory |
| trust 文件名、approval identity、hook definition digest | 保留完整摘要及原 trust 比较 | 用户保存的 trust 与定义/配置承诺，不缩短、不重新赋予权限 |

`connection:` 与 `package:` 是 Pulsara 生成的配置实体，不属于上一表的自然键例外。它们必须按第4、10节切换，不能因为存储在 DB 外就遗漏。

### 7.3 分页不变成短码注册表

cursor 原有 envelope/编码保留，只更新其中 Pulsara 自有 ID 字段；不统一添加新的 `cursor:` 类型，也不缩短 checksum/MAC 或把 JSON 状态替换成几位内存键。各 decoder 只接受新契约，不探测旧身份格式。若原 cursor 有显式版本，发布时升到其新单一版本；没有版本的也不能新增旧分支。

| 分页种类 | 保持的状态与失效语义 |
| --- | --- |
| terminal output/retained/baseline | Base64url JSON；owner epoch、process、stream、offset、完整 checksum；GAP/retained cut 保持。checksum 不冒充认证 |
| search_sessions | 原 caller/query/lifecycle/range 与 keyset；仍是 live discovery，不新增全局快照 |
| read_session_content | 原 cut、entry/event/offset/direction/include_tools，逐页重新授权；数据还在时可跨重启重建 |
| search_session_content | 原 BM25 consumed-page 链；丢失进程链明确 CURSOR_EXPIRED，不持久化。该页内局部身份若是序列化自有 UUID，改 `page:<26>`、`R(page)`，长度31 |
| list_agents | 原32字节 HMAC、session/sort/page 参数、既有512字节输入边界；owner secret 重建后失效 |
| scheduled_tasks | 直接使用最后一条完整 `schedule:` ID；字节序 keyset，不从随机 ID 推断创建时间 |
| UI task/group/activity | 保留各自 Base64url JSON 与过滤/排序锚点；typed ID 字段同步改，不能误复用模型 HMAC cursor |

## 8 模型面与 UI 的切换

模型只需知道“类型标识 + 原样传回”。工具参数名、作用域、数据内容及既有错误处理尽量保持；参数值、schema 示例、说明、错误回显、FULL/COMPACT/REF_ONLY、runtime source、history projection 和 dogfood 输入一起更新。没有让模型记住旧 SHA 与新 ID 对应关系的步骤。

删除模型 `list_agents.model_target` 中不供其操作的 estimator fingerprint；按显式业务字段投影。`reload_capabilities.plugin_hook_sources` 的 `definition_digest`、`trusted_definition_digest`、无操作用途的 package_install_id 不再进入模型结果。GUI 的 trust 查看与底层比较可继续保留完整字段；不能通过删底层事实来减少模型输出。不要再以序列化整个内部 DTO 作为模型公开格式。

Plan 最终模型投影继续过滤 workflow/interaction ID；不能因为统一完成就把更多内部 ID 暴露给模型。普通摘要、terminal stdout、MCP/Hook 自由文本及用户输入不经过“哈希清洗”正则。

UI 路由、session/task/batch 缓存、annotation `source.entry_id` 与 UTF-16 坐标、stream draft→accepted entry 的对应、content selector、reasoning locator、文件导出和所有 REST/protobuf 值同步切换。不得把响应文本里碰巧长得像 ID 的字符串全部重写。

provider epoch 内 SYSTEM/tools 仍须逐字节不变，messages 只追加 suffix。此 hard cut 的正式启用以旧 host 停止、新版冷启动为边界；不在运行中的旧 epoch 热换格式。新版后续压缩继续只在明确采用 compaction successor 时重建输入根；不新增“身份升级 epoch”例外。

## 9 31张应用关系的 schema 决策

clean-v0 直接更新，没有在线迁移链。保留现有31张应用关系、事件种类、subject slots、append guards、durable jobs 的数量与职责；添加/调整当前表的 CHECK、索引或字段类型校验不构成新增身份 owner，但每项必须服务下面的现有关系。

| 关系 | 新身份 / 结构决定 |
| --- | --- |
| workspaces | `(memory_domain_id, workspace:)`；root/kind/label 原冲突检查，project context 同 ID |
| sessions | `session:` 主键；所有 workspace/domain/parent 关联同步 |
| blobs | `blob:` 或 `image:` 主键；完整摘要/bytes/codec/storage_identity 保留，无 alias 列 |
| context_snapshots | `snapshot:`；session、blob/image descriptor 与 source/content 摘要同步 |
| subagent_tasks | `task:`；`batch:`、parent、task_key 与状态保持 |
| subagent_task_dependencies | 原 session/task/dependency 复合键、ordinal；不加 ID |
| turns | `turn:`；command、task、权限与状态关联保持 |
| provider_call_usage | 本地 `call:` 主键；外部调用 ID 不改 |
| turn_context_binding_revisions | `binding:`；原 turn/revision ordinal UNIQUE |
| session_commands | 原 `(session, command:)`；请求语义幂等比较保持 |
| imported_history_groups | `history:`；保持导入分组的非执行语义 |
| transcript_entries | `entry:`；origin、关联 turn/group、sequence 与结构化 body 引用同步 |
| session_context_genesis | 原 session 主键，不增 ID |
| assistant_message_blocks | `block:`；entry/ordinal 唯一，外部 tool_call_id 原样 |
| provider_assistant_replay_fragments | `replay:`；原 native payload 不改 |
| tool_execution_attempts | `attempt:`；工具调用 tuple UNIQUE 保持 |
| tool_results | `result:`，独立 `artifact:`；result entry、output blob 等关系保持 |
| imported_tool_call_closures | 原复合键，外部 tool_call_id 不改 |
| prompt_queue_items | `queue:`；session sequence、command、target turn、consumed entry 保持 |
| scheduled_tasks | `schedule:`；revision 整数，不加 tick 表 |
| canonical_image_refs | 原三选一 owner + ordinal；blob_id 必须是合法 `image:`；不加 image 实体键 |
| assistant_visualizations | 原 `(session,assistant_entry,ordinal)`；现有 HTML 展示内容与摘要不改成图片协议 |
| interaction_decisions | `decision:`；原 TOOL_CALL/MCP_INPUT 的 accepted decision，Plan 不新增该行 |
| plan_workflows | `plan:`；完整计划内容与 revision/confirmation 保持 |
| plan_interactions | `plan-interaction:`；绑定原 workflow、resolution command 和 continuation entry，不引入 decision 实体 |
| subagent_task_children | 只有 RESULT / MESSAGE 两类，分别使用 `agent-result:` / `agent-message:`；不加一层 child ID。worker turn 位于 turns 表，不新增 TURN child_kind |
| memory_facts | `memory:`；domain/context、语义去重与 provenance 保持 |
| memory_relations | `relation:`；两端 memory/context、方向与 supersede 规则保持 |
| memory_embeddings | 原 `(memory_domain_id,fact_id)`，不增 ID |
| agent_events | `event:`；typed payload、session sequence、已接受转换种类保持 |
| history_search_documents | 原可重建复合定位；重建投影和索引，不作为身份权威 |

`public.pulsara_schema_migrations.version` 保持原 schema 管理职责。manifest、FK/触发器、grants、catalog oracle 与 baseline 一起校验；不能只改 Python 生成器而留下 SQL 的旧格式假设。

## 10 生命周期、配置与一次性启用

### 10.1 重启、fork、删除

持久行与保存配置中的 ID 冷重启保持。host/process/slot 等 incarnation 型 R 身份重建分配新 ID；TODO run、task operational generation 等逻辑 D 身份或复用 task 的值，重启后可能得到相同字符串，但原 live owner 已失效。能否重新建立执行由原 owner/epoch 和现有恢复规则决定；相同字符串不恢复丢失的执行能力、分页链或文件读取 observation，不向所有 D 配方追加 epoch 来制造新身份。

fork 沿用源 workspace，复制 entry/block/result/history group/replay row 和必要 snapshot 时使用子会话新身份；annotation 的结构化 source 精确重映射，origin_entry 保留来源，native provider call/item ID 与 replay bytes 保留。图片 blob 与 artifact handle 保留，并通过子会话自己的复制 owner 获得合法读取。artifact 不按新的 result entry 重算。fork 不复制一组仍在运行的任务、进程、monitor 或权限 approval。

自由文本里出现的旧 session/entry/task 引用不做正则 remap；它仍是对源对象的文字提及，使用时需原 scope 授权。已保存 body 的结构化字段由 fork owner 决定复制/重映射，不能因其中含字符串就一概当作正文。现有 fork 不提供跨 workspace 迁移，本文不新增该能力。

删除 session 仍允许 memory facts/relations 存续，来源 tool-result FK 按原规则置空；statement/语义编辑不改 memory ID。workspace 清理仍考虑 project memory。blob GC 必须同时检查 snapshot、entry、assistant block、queue、image ref、visualization、tool output 七类 owner；不因公开引用已从摘要消失就回收仍被历史拥有的图片。

### 10.2 DB 外的配置必须纳入 cutover

模型连接与插件安装实例不能在新 runtime 中双读旧格式。新版配置 schema 只接受 `connection:` / `package:`；改连接 ID 时，其模型选择、默认路由和按连接索引的凭据引用必须在同一个新配置快照中一致，不能把秘密值放进 ID 前像。

**现有生产 home 不因实现或 dogfood 被原地改写。** 启用采用新 home 的重新配置/安装：在旧版本仍可正常读取时，通过其 `LocalSettingsStore` 和 `require_pulsara_home()` 读取保存设置，在内存中取得实际连接参数、路由、凭据和数据库设置；在新 home 中由新版配置 owner 新建连接身份并一致装配。用户自然键不改；新安装实例由新版 package owner 生成。旧 home 可保留为用户资料，不能作为新 runtime 的 fallback。

这是部署前一次性准备新的有效配置，不是交付一个常驻旧配置 decoder、ID 翻译表或在线迁移器。新版启动遇到旧 schema 要明确报配置不匹配并说明需准备新配置，不得静默生成新连接、丢失凭据或覆盖原文件。正常运行只读写新版配置。

dogfood 使用在旧版本读取后传入的无旧身份业务配置值，通过现有 read-only settings injection 在内存中装配新 ID，并绑定验证过的 disposable local DB；不在新版测试中留旧 schema 解析分支，不 source `.env`，不输出秘密。用于跨进程交付的秘密不得写入审计报告、fixture、trace 或 shell 环境；若无法安全使用现有设置注入，就报告具体准备阻塞，不能假称真实 provider 已验证。

workspace MCP trust、Hook trust 和其他用户保存权限资料不随数据库重置自动迁移。原路径摘要/approval identity 算法没有改动的资料可以按原有效性规则读取；涉及重建 package incarnation 或改动实际绑定值的 trust 按原规则失效，走既有用户确认路径，不能根据旧短 ID 自动再授权。实现不新增 trust 迁移机制。

`LocalSettingsStore` 不是整个 home 的导出器。新 home 必须按下表逐 owner 准备，不能只重建连接就宣称完整承接生产配置；下面的复制/重新配置是一次性部署准备，新 runtime 不保留旧 home 查找分支。

| 资料 owner | 新 home 的准备规则 |
| --- | --- |
| LocalSettingsStore | 模型连接、默认绑定/路由、DB、memory retrieval、MCP credentials/OAuth 等真实配置值按原 owner 装配。仅 Pulsara 连接身份重新分配，所有关联键一次性一致；秘密不进入日志、报告、测试 fixture 或环境变量 |
| `mcp.yaml` / MCP 配置 owner | 显式复制格式未受此改造影响的定义，或由现有配置入口重新配置；server 自然键及 secret 引用同步到新 home 的相应 credential owner，不自动执行/授权新配置 |
| `hooks.json` / Hook 配置 owner | 显式复制有效定义或重新配置；引用的文件/路径必须实际可用，definition/trust 完整检查按现有规则执行 |
| user skill 配置及 Skills 文件 owner | 显式复制/重新设置启用项，并确认指向实际可读的 skill 路径；不从旧 ID 猜测路径，不无条件扩大启用范围 |
| plugin package store / 用户插件资料 | 由新版 package owner 新安装产生 `package:`，使用既有配置/用户数据入口恢复所需资料；不把旧安装 manifest 当作新实例，不自动删除旧 home 的用户文件 |
| workspace MCP trust / Hook trust / package trust | 需要保留的资料显式准备到新 home，随后根据实际 canonical path、定义、server 配置及新 package incarnation 核验。只有原有效性规则仍成立才可继续有效；变更的绑定按原交互重新确认，不能批量重签或自动再授权 |

dogfood 的内存 settings injection 只覆盖该接口拥有的资料。测试场景若依赖外置 MCP/Hook/Skill/plugin 配置，必须另外按其 owner 准备隔离资料；未准备的能力应明确不在该场景中，不能把默认空目录当作已验证的生产等价配置。首次正式启用前逐项说明是已准备还是用户明确选择重新配置，不通过扩大日志暴露秘密来证明配置完整。

### 10.3 发布顺序

1. 完成单一新路径的代码、schema、配置 schema、工具 schema、协议、前端及 fixtures。删除旧 helper、旧 regex、旧 ID 前像保留层和旧解析分支，不保留旧 ID 生成器做内部过渡。
2. 在旧版本仍可读取时完成上一节的只读配置准备；停止旧 host/provider epoch 和 terminal gateway，等待原有取消/结算完成。不要边运行旧 epoch 边替换 IDs。
3. 核实要重建的 PostgreSQL 确为可丢弃本地目标，再重建 clean-v0；不触碰远程数据库，不原地改写未知生产数据。生成的 protobuf bindings 随 `.proto` 同步；根据当前 terminal 握手版本 owner 升级 breaking contract，只接受新版本，不做 old/new 协商。schema digest 仍为完整校验。
4. 重建 history search 等 disposable 投影。旧 localStorage active-session 失效后清除选择，旧路由返回不存在；不尝试从标题或 ID 前缀猜新 session。UI 草稿正文属于用户内容，不能因 cache cutover 自动删除；在停旧 UI 前保留/导出，或通过已有 UI 提供另存新会话，不能携旧 session owner 自动提交。
5. 用新 home/隔离注入和新 DB 冷启动，运行第12节验收。正式更换用户使用的 home 是单独可见的部署动作，本轮写文档不执行该动作。

没有“先只改模型显示、以后再改主键”的阶段；提交可拆分开发，但启用前必须完成全部连接面。若未完成配置准备、协议或某个 canonical owner，不能把该部分留在旧格式而宣布 hard cut 完成。

## 11 实施接缝与删除清单

| 接缝 | 必须共同修改的 owner / 消费者 |
| --- | --- |
| 编码与解析 | 少量纯函数与 typed 字段校验；Python/浏览器向量一致；禁止把新函数做成全局对象查找入口 |
| 全部持久键 | [baseline](src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql)、[repository](src/pulsara_agent/conversation_kernel/_repository)、[contracts](src/pulsara_agent/conversation_kernel/contracts.py)、[manifest](src/pulsara_agent/storage/migrations/manifest.py) |
| 入场与执行 | [steer](src/pulsara_agent/conversation_kernel/steer.py)、[host](src/pulsara_agent/conversation_kernel/host.py)、[assembler](src/pulsara_agent/conversation_kernel/assembler.py)、[tool execution](src/pulsara_agent/conversation_kernel/tool_execution.py)、[interaction](src/pulsara_agent/conversation_kernel/interaction.py) |
| 压缩/历史/fork | [compaction](src/pulsara_agent/conversation_kernel/compaction)、[reader](src/pulsara_agent/conversation_kernel/reader.py)、[session content](src/pulsara_agent/conversation_kernel/session_content.py)、[fork](src/pulsara_agent/conversation_kernel/_repository/fork.py)、[annotations](src/pulsara_agent/conversation_kernel/annotations.py) |
| 图片/内容/artifact | [blob](src/pulsara_agent/conversation_kernel/blob.py)、[prompt storage](src/pulsara_agent/conversation_kernel/prompt_storage.py)、[prompt content](src/pulsara_agent/conversation_kernel/prompt_content.py)、[tool artifacts](src/pulsara_agent/conversation_kernel/tool_artifacts.py)、[lowering](src/pulsara_agent/model_input/lowering.py) |
| 任务/计划/调度/memory | [subagent](src/pulsara_agent/conversation_kernel/subagent.py)、[Plan](src/pulsara_agent/conversation_kernel/plan_runtime.py)、[scheduling](src/pulsara_agent/scheduling)、[memory owner](src/pulsara_agent/conversation_kernel/memory)、[workspace identity](src/pulsara_agent/workspace_identity.py) |
| 运行时与公开工具面 | [terminal](src/pulsara_agent/terminal_process)、[TODO](src/pulsara_agent/conversation_kernel/todo_runtime.py)、[tool runtime](src/pulsara_agent/conversation_kernel/tool_runtime.py)、[tool observation](src/pulsara_agent/primitives/tool_observation.py)、builtin schemas/文件工具/历史投影 |
| 配置/协议/前端 | [model connections](src/pulsara_agent/llm/model_connections.py)、[package store](src/pulsara_agent/plugins/package_store.py)、[terminal protocol](src/pulsara_agent/terminal_protocol)、[session controller](src/pulsara_agent/web_app/session_controller.py)、[runtime adapter](frontend/lib/runtime-adapter.ts)、[UI app](frontend/app/pulsara-app.tsx) |

删除只用于旧 context/event 编号的 steer legacy identity 分支，context 编号改用第6节冻结候选的 `R(context)`。不要按文件名整文件删除 `_stable_input_identity.py`：其中 `compiled_input_identity` 当前还生成实际 `compiled_semantic_fingerprint`，`memory_reservation_identity` 的原消费者是 steer legacy quote 编号链；随该链退出，其 `stable_identity` 字段与专属计算一并删除，保留 reservation 的实际 typed 值、资源预算与完整 source/estimator 摘要。只移除失去全部消费者的旧编号链，具有 compiled/source/prefix/replay 或既有冻结确认职责的完整摘要继续由原 owner 计算，不能换成短 context ID。本文不顺带再开展一次 fingerprint subtraction。

删除散落的旧 `prefix + UUID.hex`/64-hex 业务生成器、`sha256:` 图片 reader、旧模型 schema 示例、旧 connection/package validator。清除无用途的模型 DTO 摘要泄漏，保留 GUI 与安全 owner 所需字段。

再运行全仓候选扫描，把每个剩余 hash/UUID/随机 token/共享 builder 的 prefix 调用归入本文类型、完整摘要、外部协议、cursor、自然键或纯局部实现名称。扫描产物只是开发辅助，不建立运行时身份目录、不保存逐文件 SHA。发现新的独立实体时补规格，而不是给扫描结果强行全打“通过”。

## 12 验收与激活门槛

本节定义实施验收要求；实际结果和明确缺口见实施与验证记录。测试不能靠跳过碰撞、放宽 owner 检查或新旧双读得到绿色。

| 验证组 | 必须证明 |
| --- | --- |
| 编码/生成 | 第2节向量、128/256-bit 无损编码、canonical padding、kind 白名单；同源确定性、角色分离；非法格式/旧格式/多余字符拒绝 |
| 主外键/并发 | 31表与 manifest 对齐；代表性双并发入场、同 command 重试、不同内容冲突；随机分配碰撞重试在发布前完成；已发布身份不得换号 |
| 构造碰撞 | 注入派生器的测试 seam 使不同内容/owner 产生同 ID，真实接受 owner 必须拒绝并回滚；读回原对象不变。不能只在编码单测里比较字符串 |
| 图片 | 上传/工具图/HTML 截图、新 image blob 与现有 refs 精确联结；在最终 body/command/hook/quote 冻结前绑定，验证新增 descriptor 字节的原资源边界；hydration 不补写ID；同 workspace 去重、不同 workspace 隔离；错 kind、只有 queue、跨 session 无 owner、篡改 bytes 全部拒绝 |
| 压缩与恢复 | recent 图片仍按原规则送视觉内容；offload 后中立占位；摘要丢引用后显式历史搜索/读取找回；冷重启可读；纯文本模型无虚假视觉成功或强制历史搜索 |
| fork/删除/GC | 新 entry/block/result、保留 image/artifact/native provider ID、annotation remap；删除源 session 后子 owner 可读；memory 留存与来源置空；七类 blob owner 最后移除才 GC |
| 文件编辑 | 完整 revision 往返、stale、已见行与 gap、replace_file、staging 并发变化及写后核验；同短化输入不得降低内容比较强度 |
| 执行/审批/Plan | 预分配身份批准后复用，无 attempt 的 unavailable/policy/hook/user DENY 仍闭合结果；取消与副作用结算；Plan answer/review 的 command 幂等、无 interaction 的首次 ENTER continuation、无 workflow 拒绝；不新增 decision/attempt；权限完整比较和 revision 不能被 ID 替代 |
| 任务/调度/terminal | 批次/task/material/mailbox-entry/message-child/result 全链，mailbox 冻结重试与原 ordinal；scheduled revision+due 幂等入队及取消；monitor 提交后激活、旧 owner/迟到结果拒绝、stream GAP 与物理关闭；相同逻辑 TODO/task ID 在新 owner 中不恢复旧能力 |
| cursor/token | 各分页至少往返一条真实 typed 新 ID；保留每页授权、cut、HMAC、过期、512字节已有边界；stateless 与 process-local 重启行为分别测试 |
| 模型/UI/protocol | 39个 builtin descriptor 的相关字段及各预算投影；已知 DTO 泄漏消失；source/current/predecessor 一致；UI route/annotation/content preview/reasoning/连接选择；thinking 与 text/tool 交错 Start/End 时 live→canonical block ID 不变；新旧 terminal handshake 明确拒绝 |
| 负向保真 | 用户文本、Git SHA、文件校验和、外部 MCP payload、provider tool/item IDs、native replay/encrypted reasoning 没被批量改写 |
| 配置与存储 | 新 home 连接与凭据引用一致，外置 MCP/Hook/Skill/plugin 资料按 owner 准备并说明覆盖；旧 schema 明确失败；保存生产配置只读；trust 不自动授权；无新增表/event/job/身份 registry，async task 名未被升级成实体 |
| prefix 连续性 | 多轮、任务结果、terminal observation、reload、权限变化等同 epoch SYSTEM/tools 字节一致且 messages suffix-only；只有原有两种合法重建边界 |
| 编译候选与结构测量 | normal/steer/summary/destination/repair 的 context 首次冻结后跨语义编译与 wire quote 复用；typed 非执行 structural projection 确定派生、完整事实比较、不能执行；未知/缺字段DTO拒绝，不误走降级分支 |

实现时先运行受影响 owner 的聚焦测试，再执行仓库要求的 kernel/schema/oracle、前端类型/测试及相关全链路检查。只更新期望的新契约，不弱化原行为断言。

真实 provider dogfood 至少包含：(1) 多模态模型收到图片、压缩后根据真实任务需要恢复图片；(2) 纯文本模型拿到中立引用、显式历史找回但不会得到伪装视觉；(3) artifact/memory/task/connection 等实际存在的工具引用被模型正确传回；(4) 多轮和 compaction successor 下 prefix 契约。允许使用多个短场景组合，不为拼成一次会话增加产品机制。

记录实际 schema、provider 输入、模型回复、工具参数/结果及失败详情，只遮蔽真实凭据。用代表性旧/新说明和输出比较真实 tokenizer 长度及复制成功情况；不把26字符当作 token 收益证明，不以模型偶然没调用工具算往返通过。未配置多模态或纯文本 provider 时明确该验证缺口，不能用 mock 冒充真实 dogfood。

完成标准：所有本文纳入的实体使用单一新规则；旧兼容路径不存在；保留的完整摘要/token/外部身份都有明确 owner；所有受影响验收通过，真实 provider 证据可检查。图片混合可视化不属于完成标准。

## 13 冻结审阅结论

本轮由 GPT‑6 Astra high 独立审阅，主代理核查调用者并修订，再由同一审阅者复核修改结果。冻结的是以上最终规则，不是对尚未发生的实现作通过确认。

| 审阅问题 | 最终决定 |
| --- | --- |
| Plan 配方依赖不存在的 interaction/decision | answer/review 使用真实 command 幂等来源；首次 ENTER continuation 不依赖 interaction；不新增 decision 行 |
| 拒绝或 unavailable 结果没有 attempt | 常规 result 保留首次冻结的随机身份；接受/批准/取消/拒绝共享原预分配与结算生命周期 |
| inter-agent 消息路径遗漏、方向混淆 | incoming mailbox 只创建原 entry/event；worker assistant 输出才创建 MESSAGE child；各自配方与重试规则明确 |
| 图片 ID 绑定可能晚于冻结报价 | workspace 绑定先于最终 canonical body/command/hook/quote 冻结；新增引用字节计入原报价，hydration 精确核对 |
| 局部身份漏项和过度设计 | 补齐 Pulsara-owned MCP/continuity/context；async task 名不升级为实体；block 不因 thinking 过滤而换号；非执行结构投影不增加字段或执行权 |
| 配置切换只考虑 LocalSettingsStore | 明确 MCP/Hook/Skill/plugin/trust 的外置资料准备 owner；原生产配置只读，新 home 不自动继承或授权 |
| 重启后所有 live ID 都必须不同的错误概括 | 区分随机 incarnation 与确定性逻辑身份；同串不恢复旧 owner/epoch/能力 |
| 碰撞、GC 与幂等 | 保留128-bit选择、存活 owner 的冲突拒绝及完整事实检查；明确删除后剩余概率；不新增墓碑、registry、总量上限或兼容别名 |

文档检查覆盖：与 clean-v0 的31张应用表逐项对照、规范长度和 Base32 padding、派生/完整 revision 向量、仓库链接与文档格式。没有声称全仓动态分支已执行；第12节定义实施验收，后续实现结果另记于实施与验证记录。本轮不启用延期的图片可视化。

## 14 实施期间补充的既有 owner 接缝（原审阅者已复核）

- 控制 command 的 admission deadline 原本嵌入 ID，改由控制请求与查询的显式 `admission_deadline_ms` 字段承载；deadline、结果保留期和语义比较保持。terminal 握手升到 major 4，旧 major 不读取；protobuf 包/文件名作为代码命名不提供旧 wire 兼容。
- 浏览器窗口 incarnation 与 runtime transport 都使用 `R(transport)`，分别由原浏览器 sessionStorage 和连接 owner 管理；窗口刷新复用当前窗口 ID，新的窗口重新分配。它不是授权令牌。旧 active-session cache 仅清理选择，不删除用户草稿正文。
- Plan continuation 的内部并发去重 key 保留 `(role, 原 command 或完整 candidate 承诺)` 的复合键，不伪装成新实体 ID。没有 continuation turn 时用 `None`，不生成 question/no-continuation 假 turn。
- 空 assistant 的既有空文本 block 在首次冻结时用合成 `segment:` 派生；正常 provider block 仍在 Start 冻结。旧 message.blocks 中无生产消费者的 UUID DTO 删除，保留实际使用的 ToolResultState。
- 剩余 UUID 用途限于临时文件/目录名、前端 DOM/编辑草稿局部 key，以及 SDK 所需整数 epoch；这些不成为模型可操作的 canonical 身份。
- 定时任务随机身份由既有事务创建 owner 分配，事务内碰撞重新分配；切换计划的暂停与新建仍原子提交。

### 14.1 审阅补充的持久 machine decision 与事件来源

`interaction_decisions` 同时存用户确认与机器 policy decision。用户确认仍用 `D(decision, accepted, [session, command])`；机器策略没有 command，使用 `D(decision, tool-policy, [session, assistant_entry, external_tool_call_id])`，首次策略判断时冻结。不能使用 process-local `terminal-decision` 写该表。原完整 tool tuple、permission snapshot 与决策效果检查保持。

原通用 `_event` 的当前调用都有稳定已接受 subject，统一显式传 session 与 occurrence key 后使用 D。entry、queue、attempt、decision、terminal turn 的单次转换 occurrence 为空；依赖前沿、Host takeover、推断完成这些 task status 接受用 `status:<目标状态>` 区分已有状态转换。专门的 worker start/terminal owner 仍保留已有 writer generation occurrence，Plan owner保留原 command/assistant occurrence；不新增事件种类或来源记录。

### 14.2 生产实现交叉复核 gate

2026-10-14，同一 GPT‑6 Astra high 审阅者与主代理完成生产实现交叉复核，确认本节修订及发现的问题闭合后，双方同意进入 pytest 夹具和断言更新阶段。进入此阶段前测试文件没有修改；这不是 pytest、dogfood 或正式激活通过声明。

### 14.3 测试阶段生产问题与最终审阅

测试阶段发现并由同一审阅者复核闭合：fork 导入工具来源允许 `history:`，仍禁止其成为当前/前序执行 turn；explicit subagent result 事件从真实 tool-result owner 取得 session；无 workflow 的 Plan 调用先完整校验原始参数再返回 unavailable，不为此分配假 plan/interaction；terminal PREPARING 期间已接受的 abort 保持原有 spawn 后 kill/join 结算语义，新增随机身份碰撞处理不改变该边界。

删除 legacy steer/reservation 编号后剩余的无消费者消息计算及注释，不删除真实 reservation 预算、来源或 estimator 事实。最终同一审阅者确认没有未解决的生产阻塞；实际全量测试和 provider 覆盖以实施与验证记录为准。

### 14.4 原哈希审计复查后的边界闭合

以下落实 §3.1 的既有规则，不增加身份注册表、兼容路径或新的保留期：

- terminal manager 只在 registry 成功返回本次 process 后结算本次 foreground decision；拒绝入场不能清理同名既有 owner。尚未发布的局部随机 decision 候选碰撞可以重抽，受原操作 deadline 约束；显式已冻结 attempt 碰撞直接拒绝。启动失败与物理 join 的结算仍由 registry 负责。
- gateway 现有 connection 集合保留连接对象及其 attachment，直到连接清理结束；controller 的逻辑撤销不提前释放该连接的身份。分配新 attachment 时检查仍保留的连接，防止旧能力因同号重新生效及旧清理误撤销新 controller。
- incoming mailbox 在原锁内、递增 ordinal 和返回 queued 之前，检查所有仍保留的 recipient mailbox 的 entry/event 身份。异源碰撞拒绝；同 attempt 的完整事实幂等检查保持。正在提交的批次仍由原 mailbox 保留，成功接受或确认后才退役。
- scheduled HTTP 的读取及管理操作使用精确 kind 校验；run-once 的 command/session/schedule 在首次确认查询之前校验。旧格式、错误 kind 和不规范编码使用原参数错误载体返回 `INVALID_ID`，不作为未知对象查询。
- terminal monitor cancel 在工具参数解析和现有 owner 入口校验 `monitor:`；工具授权使用原 `INVALID_ARGUMENTS` 载体保留 `INVALID_ID` 原因。合法但不存在的 monitor 继续返回原 not-found 结果。

本轮测试和原三位审阅者的复核结论记入实施与验证记录；按用户要求不重跑 provider dogfood。
