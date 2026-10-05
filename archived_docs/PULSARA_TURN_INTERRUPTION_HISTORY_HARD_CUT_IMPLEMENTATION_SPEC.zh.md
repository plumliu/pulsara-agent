# Pulsara 逐轮中断历史提示前置硬切实施规格

日期：2026-10-05。状态：已在 main 实施并冻结；主代理与 Workspace readonly spec critic（GPT-6 Astra / high）代码复审通过，无剩余必改项。验收与本地数据库激活记录见 §12。

本文在 main 编写并实施，是定时任务实施之前的第二项前置，继已完成的 [会话统一只读与工作目录恢复规格](PULSARA_SESSION_READ_ONLY_WORKSPACE_RECOVERY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md) 之后实施。用户决定优先于历史文档。本次仅落地逐轮中断历史提示，不实施定时任务；保存配置保持只读。

## 1. 已确认的产品合同

每个真实中断的 ROOT turn 都在自己的历史末尾保留一条独立结束提示，继续对话、刷新、分页、冷历史查看后仍存在。前端统一显示斜体 **本轮回复已中断。**，旁边提供圆形 ⓘ，显示具体原因。

这条提示来自后端 runtime 的本轮结束事实。它不是模型生成的普通 final assistant answer，不插入 ASSISTANT_MESSAGE，不合并进思考、工具链或中间结果，不新增一次模型调用，不把 INTERRUPTED 改成 COMPLETED，不填写或替换 turns.final_entry_id。

模型认知链路继续使用现有 compiler：在下一次实际 provider 请求中，随下一轮输入提供上一轮结果通知，明确其 runtime 来源。中断时立即持久化事实并显示历史提示；当时不向模型追加一次请求。下一轮可能来自真人或将来的定时任务，仍走普通队列和 compiler，通知本身不授权重试、续跑或扩大权限。

用户主动停止、provider 失败、本地 runtime 执行失败与原接管路径确认的中断均覆盖。连接断开、普通工具返回失败、尚未接受的请求被拒绝、待运行输入被取消、目录 gate 等待、权限或 Plan 等待，不单独构成 turn 中断。原成功 Plan 流程的关闭/退出标记继续由 Plan owner 展示，不因 terminal_reason 的内部编码被泛化成异常中断。

## 2. 实施前事实与复用 owner

Python 路径以下以 `src/pulsara_agent/` 为根。

| 接缝 | 当前事实与本次责任 |
|---|---|
| `frontend/components/workbench-view.tsx` | 仅在 session.status=interrupted 且没有运行中回复时渲染 conversation-interruption；这是会话尾部瞬时投影，下一轮会消失 |
| `conversation_kernel/_repository/conversation.py::interrupt_turn` | 原 writer 事务修改 turn 状态/原因/时间，追加 TurnInterrupted，结算相关 steer；扩展同一事实的公开详情 |
| `_repository/kernel.py::_interrupt_prior_generation` | 原接管事务确认旧 RUNNING turn 中断；runtime 强杀后由这里结算，不另建恢复服务 |
| `_repository/prompts.py` / `plans.py` / `subagents.py` | 还存在输入资源、Plan 与 child 的具体原子结算；不能只修改 interrupt_turn 漏掉其他终止 owner |
| `conversation_kernel/runner.py` / `turn_admission.py` | 原 cause、provider incomplete、输入边界分类与 shielded terminal settlement；保留真实 winner 和副作用结算 |
| `ports/provider_stream.py` / `primitives/model_call.py` | ProviderModelExecutionFailed 已携带 ProviderSanitizedErrorFact 的 code/message；复用实际结构，不另拷贝 provider 错误解析器 |
| `terminal_protocol/canonical_v3.py` / v3 gateway / HTTP controller | canonical snapshot、分页、committed observation 和冷读 owner；扩展逐轮结束投影，不通过打开 Host 补历史 |
| `conversation_kernel/reader.py::_load_round7_scope_facts` | 从同 scope 的直接前驱 turn 生成上一轮结果与工具执行事实，已经区分未派发和结果未知 |
| `context_sources.py::_render_previous_turn_outcome` / compiler | PREVIOUS_TURN_OUTCOME 是明确来源的 runtime guidance；继续沿此路径提供下一请求通知 |
| `fork_history.py` / `_repository/fork.py` | fork 只复制实际 canonical anchor cut；导入组当前缺少中断原因详情，必须把可见结束事实纳入原复制 owner |
| `conversation_kernel/compaction/*` | 原 summary/source cut、snapshot 与 successor owner；保存必要结束语义，不为提示重建 provider 根 |

现有 ROOT reason 对一般 provider 失败可能只记录 FOREGROUND_EXECUTION_INTERRUPTED，详细 sanitized 错误存在调用诊断但未作为逐轮公开详情贯穿。本规格补齐分类与可读详情，不承诺从进程崩溃中恢复已丢失的异常堆栈。

## 3. 数据事实与 durability 边界

### 3.1 执行 turn 的结束事实

继续由 turns.status、terminal_reason、terminal_at 拥有当前结果，TurnInterrupted 拥有已接受的中断 occurrence。最小字段增量为 turns 上 nullable `terminal_public_detail text`，保存原 owner 已知的公开原因详情，不保存另一份 assistant 正文、前端标签或最终答案。

细节由一个闭合的 typed 结束值交给原结算 owner：reason 使用实际原因，public_detail 为已知公开说明或 null。时间来自原 terminal settlement，不另建 notification 时间。终止 occurrence 在同一原事务内携带 reason/public_detail；这不是另一个事件、通知 receipt 或投递确认。

公开 DTO 从该事实生成：owner kind、turn/group ID、reason、public_detail、terminal_at_utc，以及供显示的 `display_after_entry_sequence`。原执行 turn 的位置固定为该中断 occurrence 之前已接受的最后一个本 turn ROOT entry 序列，在同一读取事务按原 entry occurrence 的 event_sequence 确定；无 assistant 时至少包含本轮 initial_entry。不按当前 max entry 重算，不把同事务之后追加或后来迟到的结果计入位置，不额外保存 display checkpoint。由 existing ID 构造可重复的 UI key，不增加指纹。

公开详情只用于说明已确认的失败。Provider 详情复用既有 sanitized code/message；不复制原始请求头、凭据或整份错误对象。保留实际错误内容，排除实际 credential 值，沿现有 provider sanitization 和 repository 物理字节边界处理，不新建任意提示字数上限或扩大现有 cap。工具未派发/结果未知等状态仍由下一 provider cut 的 reader 判断，不在中断行中冻结一份会过时的工具统计。

### 3.2 原中断 winner 与分类

- USER_STOPPED：用户主动停止；保留“不擅自续跑”的 guidance。
- SESSION_CLOSED / HOST_TAKEOVER：沿原 owner 说明会话运行时关闭或替换，不能推断 OS 崩溃、原文件丢失或调用未执行。
- provider incomplete：沿已有输出上限、上下文上限、过滤及 incomplete 原因；输出 token 配置和 provider cap 计算完全保持原合同。
- ProviderModelExecutionFailed：增加明确 `PROVIDER_REQUEST_FAILED` reason，详情使用已有 typed sanitized code/message；不把 provider_overloaded、rate_limited、timeout 等事实全丢成一般 runtime 异常。
- 输入资源/连续性、Hook、Plan continuation 等边界：保留现有具体 reason，并给出与实际观察一致的公开说明。
- 未分类执行错误：沿 FOREGROUND_EXECUTION_INTERRUPTED 或已确认的未知原因，准确显示未能确定原因；不根据异常字符串猜“服务端故障”。

实现须闭合 reader 的 outcome 映射，使新增 provider reason 进入 EXECUTION_FAILED 等已有真实类别；不得因增加原因让它误落 UNKNOWN_INTERRUPTION。这里细化失败说明，不新建 turn 状态。

同一 turn 只有原 terminal winner。失败、取消、接管与正常完成竞态时，丢失者不追加另一条提示，不覆盖 winner 的 cause/detail，不修改已接受消息或假定副作用没有发生。COMMIT 回执丢失继续使用原 terminal outcome 确认，详情属于同一个已提交 winner；不新设重试/修复协议。

### 3.3 导入历史的必要增量

fork 子会话不能依赖父会话继续存在才能读到提示。在已有 imported_history_groups 增加 nullable `interruption_outcome jsonb`，仅保存原 fork cut 确实包含的结束事实。闭合内容为 reason、public_detail、display_after_entry_sequence；时间复用该 imported group 的 terminal_at。按原 sequence_map / local_cut 重定位位置。字段不保存用户界面正文、parent 查询指针、DTO hash 或重复事件序列。

null 表示该复制 cut 不包含结束提示；即使该组因现有导入合同有 INTERRUPTED status，也不能仅凭 status 补造一个后来才发生的中断。字段存在时必须有 terminal_at，reason 非空，位置属于当前子会话复制前缀。解析拒绝未知字段和身份不一致。

独立必要性是：子会话拥有其复制历史且不读取父会话 authority；公开结束事实必须正确裁剪、重定位并自包含。除此以外不新增表、关系、event kind、live event、subject、append guard、durable job、lease/generation 或 projection authority。

## 4. 历史协议、分页与实时投影

原 canonical reader 提供 closed `TurnInterruptionNotice` 投影；snapshot/history_page 与对应 TurnInterrupted committed observation 都承载同一种 typed 值。它是历史展示 DTO，不是 CanonicalEntry，不占 transcript entry_sequence，不假造内容/blob/assistant block/provider replay。

snapshot/page 按实际 ROOT entries 的窗口返回相关结束提示：提示挂在上述固定 entry 之后、下一轮 user entry 之前；没有任何完整 assistant entry 时放在本轮已接受输入之后。后来接受的工具结果仍沿原 canonical 接受顺序/结果归属投影，不移动提示，也不把提示当作所有物理作用已经结束的证明。只显示 ROOT，child 中断仍沿原任务详情展示，不能把一个 child 的失败变成主会话中断。

先根据原 event cut 确认结束事实，再定位已有历史；不能只查询 latest_root_turn，也不能先拉出当前所有终止 turn 再把未来结果塞进较旧读取 cut。位置与时间用途分开：时间用于详情，canonical 序列/occurrence 用于顺序，不按浏览器到达时间排序。

当前 HistoryCursor 只有 cut_sequence / entry_sequence，缺少中断事实的读取边界。本次硬切在原 cursor 增加 `event_sequence_cut`，由 snapshot 的 existing event_sequence_cut 初始化，后续 page 原样继承，冷/热 HTTP、协议 client 与 adapter 贯穿同一值。页读取在同一 PostgreSQL read transaction 内验证两个 cut 未超前，并仅选择中断 event_sequence 不大于该 cursor cut 的 notice。一次 page 不能改用当时最新事件头。live observation 可以另带较新的已接受 notice，前端按同一 key 合并；旧 page 的缺省值不是删除较新 notice 的命令。不加独立通知 cursor、durable watermark 或第二套 replay。

前端按 owner kind + turn/group ID 合并去重。每个历史提示在其位置所属页面出现一次；分页重叠、重复 observation、重连和迟到 snapshot 不生成重复或把它移动到会话当前最末尾。跨页 turn 不要求一次加载全部消息；末尾所在页可定位提示，历史滚动保持原 cursor 与锚点，不改成按通知数量分页。

页所属关系确定为 display_after_entry_sequence 对应的 entry 是否入选。本次把该 entry 与挂载于它的全部 notice 作为一个返回组合，按完整 Snapshot/HistoryPageResponse 的实际 serialized bytes 试装，包括 control、cursor 和 notice；禁止先按 entry 截断，再在返回前附加提示。组合不适合剩余预算时连同该 entry 留在下一页，不推进 cursor 越过它。单组合在该操作物理预算内不可容纳时返回原 typed CanonicalProtocolResourceExhausted，不返回空 has_more 循环、不静默省略 notice，也不临时放宽字节预算。observation 同样将 notice bytes 纳入原 maximum_bytes/gap 处理。没有新增总历史 cap，不一次返回全会话 notice 数组。

committed observation 可即时显示已确认的 notice；snapshot/page 为同一 canonical cut 的读取投影，不能用 live delta 或前端 catch 写入终止事实。冷历史、observer、缺目录和普通已连接窗口都能读，不因此激活 Host/MCP/Hook。

历史查询失败沿原读取错误呈现，不静默隐藏已知 notice，也不从错误生成一个新中断。summary 的最近 ROOT 状态与顶部单一标签合同保持原样；顶部是当前状态，历史末尾提示是每轮事实，彼此不能充当数据来源。

## 5. 前端展示与交互

删除依赖当前 session.status 的 conversation-interruption 尾部条件分支。新增具体历史结束组件，沿原消息列表的顺序/分页显示。不要作为 AssistantMessage 的普通正文或 tool trace 渲染，也不要放进“中间结果”折叠区域。

固定主文案：**本轮回复已中断。**，沿现有 muted 斜体样式。旁边圆形 ⓘ 是可聚焦按钮，aria-label 为“查看中断详情”；hover/focus 可读，点击可固定展开，触屏可用，Escape 关闭。复用已有浮层定位与样式 owner，不自建通用 tooltip 框架。

详情呈现具体原因、原公开说明与结束时间。没有详情时显示已知类别；未知时明确“未能确定具体原因”，不显示伪造错误码或“运行正常”。默认不展示内部 opaque ID、原始协议 JSON、凭据或整段 traceback。

提示不拥有复制 assistant 答案、批注源 anchor、fork、重新运行或自动重试按钮，不计入模型回复 token/usage，不更改最后真实 assistant 文本。已有消息仍可查看/复制；本规格不把部分未提交流输出补写成 canonical assistant entry。

继续对话后旧提示留在旧轮末尾，新轮正常显示；多轮中断各保留一条。新轮 RUNNING 不移除旧提示，后来 COMPLETED 不清除历史。只读模式不禁用详情查看，也不借详情提供写控制。进入或离开只读保持原统一 gate。

## 6. Compiler 与模型认知

保留 PREVIOUS_TURN_OUTCOME 的现有 runtime source、trust、budget 与 lowering owner，直接前驱仍按原同 scope initial_entry_sequence 判断。通知从后端结束事实及 reader 当前 cut 的必要工具状态生成，明确“runtime 说明上一轮如何结束”，不使用 assistant role 伪装模型自述。

这里的直接前驱明确仅为真实 EXECUTED_TURN；当前 reader 不把 imported_history_groups 当执行 turn。本规格保留这个范围。导入 notice 是子会话自包含的历史展示事实，不为它创建 executed turn、伪造 predecessor ID，或声称冷 compiler 会自动重放全部导入结束通知。原 fork 的 snapshot/summary 与导入工具 closure 沿原语义保留上下文和副作用边界。历史可见性与每次请求的直接前驱通知不混为同一库存，也不增加历史通知重放源。

主路径仍是：中断结算 → 后端历史可读 → 下次正常 turn admission → 原 canonical read/compiler → 原 provider dispatch。没有下一请求时只有历史展示；定时、队列或人工随后触发时才随其请求供模型阅读。通知不构成一次新的 turn，也不提前把未来通知安装进已经冻结的 prospective 请求。

公开详情纳入现有 _render_previous_turn_outcome 的同一通知，FULL/COMPACT 沿原 compiler budget。必保留的是原因类别与必要行为指导；COMPACT 可省略长 provider 诊断而保留其原因，不能因诊断挤占预算丢掉未知工具结果等指导。不另叠加一条“人工 final”通知后继续重复旧原因提示。UI 文案不作为 prompt 内容；保持用户主动停止后不得擅自恢复、已保存输出可能只是未完成轨迹等现有必要指导。Provider 返回的 message 即使由已认证 runtime 传递，也只是被引用的诊断文字，不能把其中指令当作 runtime 授权或用户要求。

保留 provider tool-result closure、未派发与结果未知的区分和禁止盲目重复执行的指导。中断说明不能代替协议要求的工具闭合，也不能声称取消撤销了副作用。已有迟到结果仍按原 accepted boundary 投影，不覆盖已安装 closure 或重写历史。

保持现有 source placement；“随下一次用户消息发送”表示同一实际请求，不要求把消息插回旧 turn 的 provider 位置。在同 epoch 内 SYSTEM/tools 字节不变，messages 仅 suffix 追加。只在原允许的 cold epoch / adopted compaction successor 重建，不因 notice、重连或详情读取新增 rebase。

## 7. Fork 与 compaction

fork eligibility 继续由 read_fork_anchor 判断真实 assistant anchor；notice 永远不是可分叉的 final_entry。原 final_entry_id、接受输出、replay、权限与模型选择含义保持不变。

复制结束提示须独立核对 cut：对于源执行历史，使用 anchor 自己的 canonical 接受 occurrence 的 event_sequence，不能用 fork 时 session 最新事件头替代；中断 occurrence 必须在该 anchor occurrence 之前且所属历史在复制范围内，才复制结束事实。anchor 自身之后才发生的中断不得带入；不能仅凭父 turn 当前 INTERRUPTED status 把后来事实倒灌到更早的 anchor。导入 notice 随原导入创建事务成为封闭历史，后续按固定位置裁剪；它挂在 entry 之后，故只有 display_after_entry_sequence 严格小于 anchor entry_sequence 才落在复制 cut 内，相等也排除。被排除的事实不能在子会话首次 connect/compiler 时从父会话补回来。

fork owner 同事务复制到 imported_history_groups 的闭合字段，按既有 local_cut 重定位；后续 fork 再次复制保持同样 cut 合同。父会话删除后子会话仍能读到其已包含 notice。不新建跨会话 relation，不给导入 notice 伪造本地执行 event、ROOT turn 或 provider usage。

现有 fork 对 snapshot 只复制 earlier_context_summary 并重建 carrier，不调用 summary 模型，也不重新综合新 notice。本规格保留这个行为：已有 summary 中的结束语义照常保留；未被旧 summary 包含的导入 notice 不在 fork 时自动送模型，等后续实际 compaction 选中其 source 范围时按下一段读取，不增加 fork summary 请求。

compaction 需要覆盖“被摘要的当前 source turn 已中断”，原 PREVIOUS 只表示它的前驱，不能替代这份事实。在 `FrozenCompactionCanonicalRead` 增加可空 typed 当前 source 结束材料，以及本次 safe_head_range 实际选中的导入结束材料 tuple。原 reader/source proof 读取事务同时取得原中断 occurrence 的 event cut、reason、public_detail 与结束时间并冻结；当前 source 事实必须与 source scope/turn exact-join。导入材料从同一 source 范围实际涉及的 imported groups 读取，位置在 safe head 内且未被 lineage snapshot 覆盖，沿已封闭位置和 owner identity 校验；不能扫描范围外全部 UI notice。不存在对应事实时为 null/空 tuple，不根据 status 猜测缺失原因；读取出错不能当作空。完整 frozen 值直接交给原消费者，不新建材料指纹或 durable 证明。

冻结 tuple 只是本次 source 的候选材料，不是每个 summary 候选都可以使用的全量附件。installed-prefix 在原 coordinator 逐候选选定 prefix 时，只保留固定挂载 entry 实际进入该 prefix 摘要范围、且不超过 prefix.source_through_sequence 的导入事实；retained tail 的事实不混入摘要。destination-projection 逐候选仅保留 projection.units 的 source_entry_sequences 实际包含相应挂载 entry/group 范围的事实，不能仅用最宽 safe_head cut 代替选中范围。筛选是对已冻结完整值的纯函数，不重读、不创建新 hash/registry。

当前 source turn 的结束材料单列为明确标注的 source 生命周期说明，不表示该 turn 全部消息已经被选中摘要。`compaction/model_call.py::prepare_compaction_summary_semantic` 与 `prepare_destination_projection_summary_semantic` 使用同一冻结库存、上述各自精确筛选后的候选值，通过现有 summary_request / ephemeral summary placement 追加带来源的说明；原 source messages 不动。每个候选的实际材料与构造请求一起纳入原 input quote、token/byte admission 和 existing source proof 的真实兼容性边界，不能先按全量 tuple 或空 tuple 计量再替换正文。最终 winner 的筛选值随原 prepared 请求冻结，repair 重用完全同值，不在 dispatch/repair 时重读数据库或重新选择范围。已有前驱通知与工具 closure 保留。要求 summarizer 把中断视为 runtime 状态，不总结成模型答案，也不把被停止任务列成获准继续事项；snapshot 保留必要续跑语义，普通 canonical 历史不因压缩删除提示。

源 cut 之后发生的中断不回灌已冻结 summary 或 successor。旧 frozen 请求沿原所有权/取消/结算合同处理；结束事实在后续新 turn 的合法准备读取中由原 PREVIOUS 路径看到，没有同 turn 的通用 barrier 补通知保证。本规格不要求废弃已冻结首请求来强制安装它。首调用/三级额度交接与冻结 successor 保持原字节/handoff；不复制 UI 展示数组为第二套 authority，也不创造“提示已摘要”的 durable receipt。

## 8. 结算、崩溃和准入边界

只在原终止 writer 事务的 RUNNING → INTERRUPTED winner 上保存公开详情与 occurrence；helper 可以复用具体 typed 分类/格式化，不得另起异步提示写事务。输入资源、prepared steer conflict、原子 child/task、接管等其他终止路径逐一检查；不要为了复用函数拆开其原有原子边界。

已接受工具/进程副作用与未提交模型流沿原 owner 正确结算。用户停止、Host 关闭和 provider 失败同时出现时，原 cancellation cause 优先级与结算赢家不变，不能因 richer error detail 抢占原 USER_STOPPED 原因。非终止重试的某次 provider 错误不生成历史提示；只有整轮最终中断才显示。

本地服务强杀或机器掉电时无法保证立即写事实。仍由原下次合法 writer 获取/接管路径确认旧 RUNNING 的终止；没有确认前前端显示连接错误/旧数据库状态，不虚构结束提示。没有恢复 owner 时不新增 watcher/job 去补它，也不承诺自动恢复原执行。

请求在 turn 接受之前失败、队列取消、permission deny、单个工具失败后模型继续、观察窗口掉线、目录等待都不生成 notice。一个 ROOT 的真实终止也不取消会话的其他计划配置；用户停止本轮不等同于暂停或删除未来定时任务。

## 9. 硬切、schema 与实施顺序

数据库增量严格为 turns.terminal_public_detail 与 imported_history_groups.interruption_outcome，既有 TurnInterrupted payload 增加公开详情。依据是原执行结果详情与自包含 fork 历史，不引入额外 product relation。oracle 仍为 committed events 30、live events 24、subject slots 11、append guard 仅 HostWriterGuard、relations 29，无新增 durable job。

clean-v0 baseline、原 repository contracts、protobuf schema/生成代码、冷/热历史 DTO、fork material 与 compiler 同时硬切。删除旧会话尾部临时提示；不保留旧/新两套公开详情读写、event payload alias、legacy message 或人工 assistant fallback。开发数据库按既有验证本地目标的规则重建，不为未发布内部合同新增在线迁移链。

实施顺序：

1. typed 原因/详情与所有原终止事务；关闭/取消/重试/接管真实赢家测试。
2. 原 canonical snapshot/page/observation 的 notice 投影及冷读；fork cut 与导入字段复制。
3. 前端独立结束提示/详情浮层、分页去重、只读和晚响应；删除瞬时分支。
4. 现有 compiler 通知统一详情、未完成工具 guidance、summary/fork/replay 与 prefix 回归。
5. focused → 受影响后端/前端 → 必要全量、类型/构建、真实 provider dogfood、代码 critic；激活后更新本节证据，再开始定时任务实施。

仅完成 UI 或某一个 interrupt_turn 路径不能宣称激活。此前输出额度、三级交接、统一只读与目录恢复合同继续成立，不夹带其他清理或新执行机制。

## 10. 必须通过的验收

- 用户 abort、provider sanitized failure/incomplete、runtime 执行错误、Host 关闭、原接管确认分别保留准确原因；重试最终成功不生成提示，未知 crash 不伪造诊断。
- 同 turn 正常完成/停止/失败/接管竞态仅一个真实 winner，一条 notice；回执丢失、重复 observation、重连/分页不会复制；原 cancellation cause 与工具副作用结算断言不削弱。
- 没有任何完整 assistant 输出时也显示；已保存中间消息保持原状，不补写半截流、不制造 ASSISTANT_MESSAGE、final_entry/provider replay/usage；多个中断 turn 分别独立显示。
- 下一轮开始/完成、刷新、分页、跨页长 turn、冷历史、observer/缺目录仍保留旧提示；只读只允许查看详情；hover/focus/点击/触屏/Escape 可用。
- 双 cut 分页在取 snapshot 后才中断的情况下不把未来 event 带入旧页；较新 observation 已合并的 notice 不被旧页缺省值清除。固定挂载 entry 在跨页、迟到结果、重连时不变。
- entry+notice 按完整响应字节组合试装，cursor 不越过未返回组合；单组合不适合物理预算有明确 typed outcome，无空 has_more 循环、无遗漏提示。没有无界全量拉取或新增总历史限制；不一致 cut 与晚窗口响应不会污染新会话。
- fork 从中断前 anchor 不带未来结束事实，较晚 anchor 包含早已发生的中断；提示不可 fork；重复 fork、父会话删除后的子历史自包含；导入裁剪不凭当前 status 推断未来事实。
- 下次真实请求仅一份带来源的 PREVIOUS_TURN_OUTCOME，用户主动停止 guidance 与工具未派发/未知结果闭合保留；无新请求时模型不被唤醒；通知不增加权限或自动重试。
- 当前 epoch 请求满足 SYSTEM/tools 相同和 messages suffix，冷/热切换与冻结首调用/successor 不回插 notice；模型切换与缩小上下文的三级策略无回归，输出 token 计算不改。
- idle 中断后压缩必须读取当前 source 的原因而非只读其前驱；installed-prefix 逐 prefix cut、destination-projection 逐选中 units 筛选已冻结导入事实，retained tail/未选 group 的事实不进入摘要；source 生命周期说明与选中正文范围分开，逐候选 quote、winner/repair 使用同一精确值。必要状态保留为 runtime 事实，源 cut 后事实不进入旧 summary，压缩后历史仍显示原提示。fork 不增加 summary 调用；重复 fork、父删除后后续压缩仍能读取导入事实。fork/replay/既有导出保留实际历史来源/cut，不把 imported group 伪造成执行前驱或承诺通知全量重放。
- 尚未接受的请求、取消队列、前端断线、工具失败后继续、权限/Plan 等待和目录 gate 等待没有误报；成功 Plan 转移不重复生成中断提示；child 不覆盖 ROOT 历史状态。
- oracle 保持 30/24/11/1/29，schema 仅明确字段增量，旧 UI 分支和重复通知路径删除，类型/构建及适当测试通过。

使用实际 Core/controller/bridge、隔离 PostgreSQL 和可控 provider 流/取消 seams 证明事务与历史，不靠全 fake projection 或 wall-clock 长 sleep。真实 provider dogfood 只读使用保存配置，在隔离数据库中至少证明主动停止后的历史保留、续聊时模型知道 runtime 原因、工具闭合和 prefix；provider 错误细类用 typed 可控错误测试，不要求真实服务故障。保存实际输入输出并排除实际 credential 值。

## 11. 与定时任务的依赖与文档状态

实施顺序明确为：已完成的统一只读/目录恢复 → 本规格 → 定时任务规格。定时触发沿普通队列/turn/compiler，自然继承逐轮结束历史，不另写 scheduled interruption 消息、模型通知或结果表。

定时任务配置状态与本轮执行结果继续独立：本轮 USER_STOPPED/provider 失败不自动改 ACTIVE/PAUSED；定时准备阶段尚未接受输入的失败仍按定时规格处理，不伪造一轮历史回复。

冻结记录（2026-10-05）：主代理与原 Workspace readonly spec critic（GPT-6 Astra / high）进行初审、修订和复审，已闭合以下具体问题：

- 原 HistoryCursor 缺少事件边界：改为双 cut；entry 与归属 notice 按完整响应字节组合试装；旧页不删除新 observation 已接受的提示，挂载位置固定。
- 原摘要仅有 turn_status：在原 source 读取事务冻结必要结束材料，两条 summary 路径纳入相同归属/计量合同，repair 不重读；不承诺原同 turn barrier 存在补送机制。
- 原前驱读取仅包含实际执行 turn：导入 notice 不伪造 PREVIOUS 通知；fork 只保留旧 summary、不加模型调用，实际后续 compaction 按选中范围读取自包含导入事实。
- safe_head 不是每个候选的摘要范围：installed-prefix / destination-projection 分别按实际 prefix / units 纯筛选，逐候选计量，winner/repair 复用精确值，范围外和 retained tail 的事实不混入。

上述记录确认实施前的规格冻结；实现和代码复审的结论单列于 §12，不以此前其他功能的审阅或测试替代。

## 12. 实现冻结与激活证据

2026-10-05，main 上完成本规格的单路径硬切。原终止事务持久化公开详情；snapshot、双 cut 分页和 observation 投影逐轮提示；前端删除会话尾部瞬时提示，改为独立历史行与可访问的详情浮层。Compiler 继续使用原 PREVIOUS_TURN_OUTCOME；fork 复制自包含结束事实，两条实际 compaction 路径按选中材料范围追加 runtime 说明。没有新增 assistant entry、final_entry、模型唤醒或定时任务实现。

代码 critic 复审已闭合以下问题：

- 按 entry 的已接受 occurrence 检查 entry cut / event cut；检测到混合 cut 时拒绝整次分页请求并返回 CanonicalProtocolGap，避免将未来历史带入旧页。
- 已安装的 ROOT 停止/关闭原因在 provider 抛出非 CancelledError 时仍拥有原优先级，不被 provider 详情覆盖；已完成 winner 不新增中断事实。
- 仅有隐藏反馈 entry 的窗口也能显示提示；显示锚点是可丢弃投影，加载历史后可重新定位，旧 snapshot 不清除较新的已接受提示。
- HistoryCursor 的 event_sequence_cut 使用显式 presence，fork 冷历史的合法值 0 可经 protobuf/JSON 往返；缺少该字段的旧入口直接拒绝。
- 实际 installed-prefix 与 destination-projection / tier3 摘要验收覆盖候选筛选、输入计量与 repair 请求复用；未选中的导入中断不进入摘要，迟到工具结果不移动提示。

验证结果：

- `.venv/bin/python -m pytest -q`：**2775 passed**，432.36 秒；含 PostgreSQL、取消/结算、fork、分页、compaction、prefix 与原 durability oracle 回归。
- 前端全量：**39 个文件、618 项测试通过**；TypeScript `--noEmit` 与 `npm run build:local` 通过，发布静态 bundle 已重建。
- Ruff、`tools/generate_terminal_protocol_contract.py --check` 与 `git diff --check` 通过。
- [逐轮中断专项测试](../tests/test_turn_interruption_history.py) 验证真实 repository/runner、双 cut、完整响应组合预算、嵌套 fork 与父会话删除、终止 winner、两条摘要路径及 repair；[前端专项测试](../tests/frontend/turn-interruption-history.test.tsx) 验证独立历史行和详情交互。
- [真实 provider dogfood](../tests/dogfood/run_turn_interruption_history_dogfood.py) 只读使用保存的 deepseek-flash Chat 配置，在隔离 PostgreSQL/工作目录中完成实际工具调用后主动停止、续聊及输入 prefix 检查；模型下一轮知道 runtime 停止原因与未知工具结果，历史提示在续聊后保持同值。实际请求与响应证据保存在 [dogfood.json](../output/turn-interruption-history-20261005/dogfood.json)，排除实际 credential 值。
- 原 Workspace readonly spec critic（GPT-6 Astra / high）完成代码初审与复审，确认上述修订闭合、无剩余必改项；最终全量回归通过，主代理确认实现可冻结。

本地激活使用既有 PostgresMigrationRunner.reset 与 runtime deep verifier：再次确认目标为 **localhost:5432/pulsara**、实际地址为 loopback、会话数 0 且无其他 client 后，原子重建 clean-v0。baseline COMMIT 确认为 FULL，schema 深验为 verified；保存设置未修改。schema 增量仅为 §9 的两个字段，oracle 维持 **30/24/11/1/29**。本次未创建在线迁移兼容路径，未实施定时任务。
