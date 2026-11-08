# Pulsara 控制通知统一与 Runtime Observation 精简实施规格

日期：2026-11-08。状态：已实施，已完成 ROOT／Sol 逐项审计及验收。覆盖用户控制通知、重复能力提醒和无生产消费者的 freshness 结构。本文规定唯一的当前实现合同。

## 1. 产品合同

工作区重建、人工停止后台进程的结果，统一通过现有 runtime observation 告知模型。实际操作仍由原 owner 执行；通知是可丢弃的补充事实。

| 已确定的事项 | 实施规则 |
| --- | --- |
| 多次操作 | 同一对象合并为最新结果，不同对象各保留一项，合并成一次 observation |
| 接收范围 | 归属接受用户控制操作的会话，不绑定某个 ROOT turn；原进程来源另行保留 |
| 发送时机 | 随同会话下一次正常业务请求发送；可以跨到下一轮，空闲时也可暂存 |
| 本轮结束 | 不等待操作结果或通知，不阻止 final，不为通知额外调用模型、继续 agent loop 或创建 turn |
| 持久化 | 不创建通知专用 transcript entry、accepted event、数据库记录或恢复任务 |
| 交付追踪 | 删除“已记录／已编入模型请求／请求已发起”等反馈状态及其追踪 |
| 尝试次数 | 下一次正常业务准备取走一次；预算不足或该次准备失败、取消时允许丢弃，不回填或延期重发 |
| 丢失边界 | Host 会话关闭、崩溃或被接管后，未发送通知允许丢弃，不恢复或补发 |

模型结束本轮不代表所有进程都已结束，也不代表模型一定掌握最新状态。通知只说明实际发生的变化，不构成继续旧任务、重跑命令或扩大权限的授权。

## 2. 范围与所有权

控制通知只处理 `workspace_recreated`、`process_control`。同时删除 `CAPABILITY_CHANGE` 和 `TOOL_OBSERVATION_FRESHNESS`，具体边界见第 7、8 节。不新增 `CAPABILITY_CATALOG`，不为 Hook 增设通知路径或扩展 Hook 协议。

停止整轮任务、停止 Cell、停止压缩仍沿各自现有控制语义处理；`PREVIOUS_TURN_OUTCOME`、历史中断事实、权限、时钟、Plan、已激活 Skill 和实际 Hook 输出保持原职责。Cell 正式输出、工具结果及迟到结果不是本方案的可丢弃通知。

- 文件系统 owner 负责验证原路径并真正创建空目录；普通 resume 不自动创建目录。
- 进程 owner 负责终止、物理收尾、退出状态及未知结果；不因通知失败改变操作结果。
- `KernelHostSession` 持有少量待通知事实，Host 生命周期结束即释放。
- 现有 collector、compiler、provider dispatch 负责 runtime observation 的冻结、预算、编码与合法 suffix 安装。
- 界面继续展示实际操作进度和结果，不展示通知是否送达。

沿用现有用户控制请求的身份、权限、Host 校验、重复 command 合并、结果查询和物理关闭规则。通知可以跨轮次，不代表物理控制请求可以改投新 Host、其他进程或其他会话。

## 3. 一个来源、两种事实

增加一个非持久来源 `ContextSourceKind.USER_ACTIONS`，只允许上述两种事实，不建立一般化通知注册表。

| 属性 | 固定规则 |
| --- | --- |
| channel | `RUNTIME_OBSERVATION` |
| trust | `TRUSTED_RUNTIME_FACT` |
| lifecycle | `ONE_SHOT`，沿既有 lifecycle 编码 |
| budget | `OPTIONAL`，只有完整事实版本 `FULL`；装不下可以省略整条来源 |
| provider 格式 | 复用 `encode_runtime_observation`，body 为包含 `actions` 的确定性 JSON |
| 空槽 | 不输出空消息，不发送 `CLEARED` 或 `UNAVAILABLE`；不把清空待发送内存解释成事实已失效 |

来源的内部合同版本沿既有 source registry 管理。模型不再看到 `pulsara_user_control_feedback` 或 `user_control_feedback.v*` 包装，也不看到通知接收轮次、entry/event ID 或入模追踪字段。

### 3.1 最小内容

| 事实 | 内容 |
| --- | --- |
| `workspace_recreated` | 原工作区路径、实际创建时间、`original_files_restored: false`；简短说明只重建了空目录，使用历史文件内容前应检查磁盘 |
| `process_control` | 现有 PROCESS Target、原进程来源 ROOT 或 AGENT、结果观测时间、实际 disposition/status/exit_code/physical_state/group_alive，以及必要的公开失败详情 |

直接复用现有 `ProcessControlResult`、进程来源等 typed 值，只冻结通知必要的数据。命令全文、终端日志及内部确认字段不复制进通知；需要更多信息时由模型正常查询进程或读取输出。

通知将“用户请求停止”“已经终止”“终止未完成／结果未知”按真实结果区分，描述结果观测时的操作事实，不冒充发送时的实时状态。需要当前状态时正常查询进程，不为通知另加轮询、过期时间或实时同步。

接纳阶段被拒绝的请求、`ALREADY_TERMINAL` 等没有新控制效果的重复操作不生成提醒；真正执行后的失败或不确定结果可以通知，不得写成成功。进程自然退出继续走既有 UI／查询路径，不额外生成 USER_ACTIONS。

路径和错误详情沿现有 public projection 与凭据边界输出，作为数据表达，不将其中的文本当作指令。不复制原始配置或另建脱敏机制。通知不触发人类 prompt/steer Hook、不改变权限、不执行工具。

### 3.2 内存合并

每个接收会话只保留：一个待通知工作区重建值，以及按现有 `process_id` 索引的最新进程控制结果。两者共同冻结为一个 batch。

- 同一对象的新结果替换尚未消费的旧结果；不同进程互不覆盖。
- 工作区事实排在前面，进程按现有 ID 稳定排序，保证同一冻结值反复准备时内容一致。
- 不分配 notification/batch/delivery ID，不增加 revision、租约、持久队列或投递计数。
- 复用已有会话、进程和 AGENT 身份，不新增通知身份类别。
- 不设置额外条数、存活时长或任务总量上限。待通知值可以合并、正常消费或随 Host 会话丢弃。

## 4. 接入普通请求

```text
用户执行操作
→ 原 owner 得到真实结果
→ 在接收会话内存中合并事实
→ 下一次普通 ROOT 业务准备原子取走一批
→ 现有 compiler 和 wire 测量尽力包含它
→ 装不下或准备失败时丢弃；不回填、不延期重发
```

### 4.1 取走一次

1. 只在普通 ROOT 业务来源收集入口取走通知；预览、独立 dry 试算、摘要、repair、suggestion、连接测试和子代理都不能取走或读取新 pending。
2. 沿 Host 现有锁一次取得不可变值并清空本批 pending，持锁步骤不做外部 I/O。取走后新到达的操作进入下一批，不受当前批编译、取消或安装结果影响。动态路径／错误详情在锁外复用第 3.1 节的只读凭据投影；投影失败或取消即丢弃该批，不回填，不影响原操作。不得为此保留凭据变更历史、另建缓存或增加重试。
3. 同一准备对象内部的预算试算可复用已经取得的值，不重复取走 pending。准备对象被放弃、取消或失败时直接释放，不保留通知专用恢复状态。
4. 删除安装后 compare-and-consume、revision 匹配、确认回调和延期重发。即使模型没有收到也不补发；实际操作、结果查询和 UI 状态不受影响。
5. 既有 SDK 重试可以重用原请求；已安装观察随历史前缀继续出现属于正常上下文，不是通知补发。不等待 transport 成功或模型理解，也不追踪送达。

### 4.2 预算与压缩

- 完整计入现有 source bytes、tokens、实际 wire 和工作集预算。OPTIONAL 来源放不下就整批丢弃，不做部分 JSON、通知分页、优先队列或单独截断协议。
- 在 compiler 的硬物理校验前处理超大的可选 batch；来源或聚合容量不足时移除本来源，让原本合法的业务继续。不新增任意大小阈值，也不为了提醒增加压缩或模型调用。
- 新通知不得进入辅助摘要。已安装的旧观察可以进入合法历史摘要；通知丢失不触发重建。压缩已冻结的 successor 不为通知重开或改写，不再额外携带一份跨准备恢复缓存。
- 同一 epoch 的 SYSTEM/tools 保持不变，messages 只在既有安全点追加；安装后不因通知内存已清空删除或修改旧消息。空槽不生成 CLEARED/UNAVAILABLE。
- 本来源只发给接收 ROOT 会话，不广播给子代理；原进程属于 AGENT 时保留真实来源。不得从通知推导新的执行授权。

## 5. 两个 producer 的行为

### 5.1 工作区重建

所有显式恢复入口在真正创建空目录后，调用同一个会话事实合并方法；保留原路径验证、writer 检查、工作区执行 gate、排队唤醒与取消收尾。目录原本已经存在、恢复失败或只读观察时不制造“已重建”事实。

替换 `_workspace_recreated_at`／`_workspace_feedback_candidate` 到专用反馈安装的分支。不再创建 canonical 候选，也不为提醒占用 compaction writer、等待 ROOT 运行或新开 turn。

### 5.2 停止后台进程

控制接纳不再计算 `feedback_target_turn_id`，也不因 idle 或 ROOT completion seal 决定是否保存通知。实际执行过的非重复控制尝试在结果返回后合并到同一接收会话，包括明确失败或结果未知；当前轮结束、下一轮开始不影响归属。

物理操作完成前不要求模型等候；真实结果稍后就绪时进入待通知内存。会话已关闭则丢弃通知，实际终止、资源回收、失败或未知结论仍由原控制 owner 收尾。

保留 `_UserControlAttempt` 中对请求去重、实际执行 task、结果查询及既有结果退役必要的字段。删除只为模型反馈等待或延长 attempt 生命周期的条件，通知缓存不借 attempt 存续充当交付队列。

## 6. 删除专用反馈机制

这是完整 hard cut，不是修改 JSON 标签。删除下列旧合同及全部仅服务它们的分支：

- `ports/user_control_feedback.py` 的持久内容、media type、版本 codec、installation attempt 与 provider renderer；必要的纯事实字段移入新的小型进程内表示，不保留旧类或别名。
- `EntryKind.USER_CONTROL_FEEDBACK`、`CanonicalInputOriginKind.USER_CONTROL_FEEDBACK`、`UserControlFeedbackAccepted` event 和对应 vocabulary/oracle 绑定。
- repository 的 prepare/accept/exact-confirm 反馈 entry 方法，safe point 的专用 install/confirm，runner 的反馈安装入口；共同的普通 safe point、预算和事务 owner 保留。
- `FeedbackCanonicalStatus`、`FeedbackInclusionStatus`、`FeedbackOwnerAvailability`、`FeedbackTransportResult`、`UserControlFeedbackState`，以及 `UserControlOutcome.feedback`。
- Host 的反馈目标、canonical candidate、ready/settled event、commit-unknown 确认循环、provider-input/transport 观察集合、记录结果匹配与反馈专用退役逻辑。
- 为反馈设置的 ROOT preparation barrier、completion fence/seal/settlement 和 runner 回调；正常 final 不再因为控制通知返回 continue。
- `provider_input_installed_observer`、`provider_input_failure_observer`、`transport_invocation_observer` 如仅用于旧反馈则连同注入接缝删除。provider usage、retry、物理关闭及其他有独立消费者的功能继续保留。

逐个审计 completion seal 的调用点：删除反馈专用的阻塞和拒绝条件，但保留 assistant 事务收尾、steer／Plan 接纳、stop、close、compaction writer 及真实物理控制的正确性。

纯 assistant final 与 steer 的竞争由现有 canonical 事务仲裁：`enqueue_prompt` 与 `commit_assistant_message` 锁定同一目标 turn；先接纳的 pending steer 使 assistant 结算保留本轮，runner 下一次普通 dispatch 再处理它；若完成先提交，迟到 steer 沿既有 stale-target 语义明确拒绝，不偷偷改投其他轮次。

删除 `runner.py` 当前 completion-fence 内仅为提前预测 steer 存在而做的读取及其动态 follow-up 报价。纯 final 不为尚未发生或尚未组装的 steer 预留下一请求的 wire/token 上界，不增加新 seal 或重复确认；其实际 assistant 内容与 replay 仍通过原结算 owner 接纳。真实后续请求由现有 dispatch 对实际 steer、机械来源及 wire 准入，沿既有压缩和资源失败规则处理，不静默丢失输入，也不承诺所有已入队 steer 都能入模。工具调用的结果／图片页预算、实际 Stop Hook 续接及各自已规定的报价继续保留。Plan、stop、close、compaction writer 与 assistant settlement 的独立正确性不随通知机制删除。

### 6.1 数据库、协议与历史

- clean-v0 移除旧 entry/event 值和相关 SQL 允许集，按现有工具从真实数据库更新 expected catalog／当前合同断言。不增加迁移链；开发数据库可按既有授权重置。
- 删除 reader、session content、fork、compaction retained-origin 校验等旧反馈分支。特别移除将旧反馈 entry 转为普通 `USER` item 的投影，以及压缩历史、模型切换对话投影和未消费请求选择中仅服务该 origin 的处理。新的 USER_ACTIONS 是机械来源，不成为用户指令、human/steer 激活输入、canonical 请求或新的未消费任务。旧会话丢弃，不转换为新 observation，不保留旧正文读取或 fallback。
- Protocol v3 删除旧反馈状态 message、`UserControlOutcome.feedback` 和旧 entry/event 枚举名，通过 `tools/generate_terminal_protocol_contract.py` 及现有流程更新 bindings、协议身份和 fixtures；不手改生成文件。原号码按 Protobuf 规则 reserved，不重排其他字段，不支持旧消息双读。
- 新的待通知内存不随 fork、重启或 Host takeover 复制。既有普通 compaction／fork 按其当前合同处理已经存在的摘要，不为通知新增历史存储或重建渠道。
- 实际 turn 中断、进程控制结果和其他现有独立真值不删除；不新增表、event、subject、guard、relation、job 或回执。

### 6.2 前端

删除 `ProcessFeedback`、`background-process__feedback` 的专用样式、反馈目标/入模状态展示，以及仅因反馈 PENDING 进行的查询。清理 `runtime-adapter.ts`、`pulsara-types.ts` 与 DTO 中对应字段。

后台进程面板继续按真实控制 execution/结果查询需要轮询，并展示运行、退出、失败、未知等状态。保留来源、目标、权限和失败详情；不增加“稍后送达模型”等新状态、灰色说明或替代回执。

## 7. 能力信息去重

模型侧只保留各自有独立用途的 `SKILL_CATALOG` 和 `MCP_CATALOG`，沿现有 SNAPSHOT_ON_CHANGE 规则追加。每一类能力只有一个目录来源，不新增联合 `CAPABILITY_CATALOG`。

- Skill 继续提供完整名称、描述、路径，以及明确不可用状态；实际激活和压缩后保留的 Skill 正文沿独立来源处理。
- MCP 继续提供实际状态、DIRECT／META 路由及既有分级展示；保留 ROOT／子代理的作用域过滤。epoch 后接入的新工具仍走 META，不改变已安装 tools。
- MCP 单独改变时不重发 Skill 目录；Skill 单独改变时不重发 MCP。插件同时影响两者时，各自按实际变化更新。保留独立预算、source head、失败和空目录语义。
- 删除 `CAPABILITY_CHANGE` 的 source kind、注册、compiler／absence 策略、专用通知 pending/revision、freeze/consume 回调，以及仅服务它的 `CapabilityUiChange`、`FrozenCapabilityChange`、渲染和预览缓存。清理管理页面仅为构造通知而传递的 action/target/result 字段及无用适配；共享凭据清理和实际管理校验不能误删。
- 保留实际刷新 requested/applied revision、能力采用 owner、跨会话刷新传播、页面 pending／attention 和真实管理结果。这些负责能力采用，不是模型通知回执。
- 模型不再自动收到插件／Hook 清单及 Host 部分采用提示。不为 Hook-only 修改、PARTIAL 诊断另建 observation、USER_ACTIONS 类型或通知通道；沿当前简单 Hook 支持处理实际事件和输出，不扩展协议。
- 按需能力查询沿其已有语义工作，不声称配置来源查询能精确还原某 Host 已采用的 cut 或 PARTIAL 状态。实际 Hook 执行由 owner 决定，输出继续沿 HOOK_CONTEXT；不依赖模型预先阅读 Hook 清单。

复用现有 Skill/MCP renderer、fitter、路由与目录身份规则；保留 `retained_skill.py` 对实际 Skill 目录的读取。删除旧提醒时，不把两个目录的失效或降级捆绑在一起。

## 8. 完整删除 Freshness 空概念

删除 `TOOL_OBSERVATION_FRESHNESS` 来源及其所有专属结构：注册／policy／absence、`FrozenToolObservationFreshnessCompileFact`、builder、专用 fingerprint、快照字段、传递、候选相等比较和只冻结这些结构的测试。没有实际时效分类消费者，不以“还有引用”作为保留理由，不增加替代 DTO 或同义字段。

保留以下真实行为：

- reader 在 canonical 读取事务中查询前驱，用来生成 `PREVIOUS_TURN_OUTCOME`；将原来同时返回 freshness 的方法收敛为实际的前轮结果读取。
- 前轮结果对当前 session、workspace、turn、scope 的独立合法性校验，以及 canonical cut、工具 closure／late outcome、权限、Plan 等原有一致性检查。
- 工具结果自身的观测时间、耗时、来源身份与实际结果状态；fork 的历史工具来源和迟到结果闭合不改变。

不再构造第二份 freshness 值来确认前轮查询自身；不删前轮中断查询，也不把整个复合一致性条件一并删除。

## 9. 实施落点

| 现有位置 | 工作 |
| --- | --- |
| `conversation_kernel/host.py`、`user_control.py` | 保留真实控制 owner，删除反馈状态机／结束协调，加入会话内合并与普通业务准备时一次取走 |
| `conversation_kernel/context_sources.py`、`provider_dispatch.py` | 使用普通来源准备接缝；辅助调用不读取通知，超限整批丢弃，不加安装后清理回调 |
| `model_input/contracts.py`、`compiler.py`、`continuity.py` | 接入具体 ONE_SHOT source 与既有预算／prefix；删除旧 canonical origin、重复能力提醒和 freshness |
| `runner.py`、`direct_model.py`、`safe_point.py`、`_repository/conversation.py` | 删除专用回调、写入与确认；保留共享模型、事务、资源和普通输入链路 |
| reader／fork／session content／clean-v0／vocabulary | 完整移除旧持久类别，更新真实 catalog 与减法 oracle |
| Protocol v3／gateway／前端 adapter／进程面板 | 删除反馈 DTO、传输字段、显示和反馈轮询，保留实际控制状态 |
| `capability/change_observation.py`／Host／页面刷新调用 | 删除模型通知专用 DTO 和链路；保留实际刷新、采用、诊断、跨会话传播和两类独立目录 |
| reader／快照／dispatch 与 compaction 验证 | 完整删除 freshness；保留前轮中断查询与其他真实一致性检查 |
| tests、dogfood、当前文档 | 删除纯冻结旧回执／等待／entry 形状的测试；迁移有意义的控制、取消、prefix、预算和物理收尾断言 |

采用现有适配点，不复制 encoder、source fitter、输入安装流程或另建 scheduler。新增 helper 只服务上述两种事实，不演变为通用事件总线。

## 10. 必须通过的验收

1. **统一形态**：两类事实都通过标准 runtime observation 编码；正确来源、信任和 ONE_SHOT，无旧包装、旧 entry/event 或兼容解析器。
2. **合并**：停止 P1/P2 后同一 batch 含两项；同一对象重复结果合并；工作区事实可同时存在，顺序稳定。
3. **取走竞争**：取走 P1 后更新 P1/P2，新值仍在下一批；当前批失败、取消或被省略都不能删掉后来者。独立 dry／预览／辅助调用不取走，不增加安装回执或回填。
4. **预算与丢弃**：在 source bytes、token、wire 与工作集边界下，放不下的可选 batch 整批丢弃，合法业务仍可执行；后续正常请求不重试旧批，不增加保送通知的调用、分页或任意总量限制。
5. **跨轮与 idle**：正常预算和成功准备下，A 结束后结果就绪，B 的正常请求可以看到；idle 操作不启动模型，后续正常请求可以带入；其他会话与子代理不收到。
6. **不拦 final 与独立并发**：让物理控制结果晚于 final 返回，ROOT 正常完成，无额外 provider open、继续 turn 或等待反馈的 seal；实际控制仍正确收尾。分别控制 final 前后新 steer 的事务胜负，以及 Plan、stop、close、压缩 writer 的竞争，验证输入不静默丢失、结算不重复、真实后续请求按实际内容准入；这些断言不依赖任何反馈 callback 存在。覆盖 final 本身合法但假想 follow-up 超限的情况，证明无 steer 时仍能正常完成；覆盖真实 steer 的正常处理和明确资源拒绝，不以放宽预算获得通过。
7. **真实结果**：成功、already terminal、部分终止、退出状态未知分别正确；来源为 AGENT 时不误标 ROOT。目录失败、已存在、只读打开不生成假恢复通知。
8. **工作区功能**：真实删除目录并显式重建，文件仍为空；正常预算和成功准备下，下一普通请求收到准确事实；工作区 gate、pending 输入、writer 及取消处理保持。
9. **生命周期**：Host 关闭／崩溃／接管后 pending 丢弃；不补发、不恢复，不影响真实进程关闭和 turn 中断记录。
10. **prefix 与压缩**：同 epoch SYSTEM/tools 不变、messages 只追加；来源清空不删除旧消息、不发假 CLEARED。覆盖两条 compiler 路径；辅助摘要不读取新 pending，压缩不恢复已丢通知，也不为通知改写已冻结后继。
11. **失败边界**：正式安装后 transport 失败不新增反馈重试或交付追踪；413、SDK retry、stop、权限变化及新 steer 仍走原有 owner，不重复终止进程。
12. **UI 与减法**：进程状态和实际控制查询正常；无反馈回执文字或反馈轮询。schema、协议、reader、fork、compaction 和 source 静态合同均无旧专用类别残留；新 observation 不被计作用户对话请求、未消费请求或 Skill 激活输入。Cell 正式输出、工具及迟到结果、Plan 续接和真实中断定位保持各自语义。
13. **能力去重**：MCP-only 修改只更新实际变化的 MCP 目录，Skill-only 修改只更新 Skill 目录，插件可同时影响两者；连接失败、空目录、META 新工具、scope 过滤及独立降级保持。无 CAPABILITY_CHANGE、联合 CAPABILITY_CATALOG 或替代 Hook 通知；UI 刷新与部分采用诊断正常。
14. **freshness 减法**：生产代码和当前合同测试无专属 source／fact／builder／fingerprint 残留，无同义替代结构；完成、用户停止、崩溃接管和未知工具结果的前轮观察正确，工具 timing、fork、闭合及其他一致性检查保持。

使用仓库 `.venv` 执行聚焦测试、真实隔离 PostgreSQL、两种 API 的官方 SDK HTTP 集成，以及相关后端、前端、桌面和协议回归。真实配置通过 `LocalSettingsStore` 只读加载，不导出密钥。

激活前至少用保存的真实 DeepSeek 配置验证：显式工作区重建后下一正常请求能理解“目录为空、原文件未恢复”；idle 停止后台进程后下一正常请求能看到真实结果。捕获实际 provider 输入和结果，验证没有通知专用额外调用、canonical 行或事件。完整证据齐备后再标记实施完成。

## 11. 验收记录

2026-11-08 完成第 10 节的 14 项审计。记录位于 `output/runtime-observation-implementation/AUDIT.zh.md`，原始请求、响应与日志保留在同目录。

- 后端全量 6,099 项通过，20 项既有 WSL 专属跳过；随后新增的两 API 真实进程／final 并发场景也已补测通过。当前 6,121 项采集对应 6,101 项通过和 20 项跳过，聚焦重跑不重复计数。
- 前端 1,668 项、桌面 149 项通过，前端类型检查、协议生成器检查、Ruff F 与差异格式检查通过。
- 两 API 官方 SDK HTTP 覆盖 8 个场景；真实 PostgreSQL 覆盖 final／steer 双胜者与合法 final 的预算反例。
- 保存配置的真实 DeepSeek Chat／Responses 四场景通过；工作区与进程事实被正确理解，空闲控制无额外调用或通知持久行，已安装前缀连续。原始模型响应及格式试验失败均保留，凭据扫描通过。

本次完成源码及当前合同，尚未构建安装或提交。
