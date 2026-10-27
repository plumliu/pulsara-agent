# Pulsara：task 历史追加与 profile 消息化实施规格

日期：2026-10-28。状态：基础实施规格，待实施。

## 1. 目标与范围

B 从现有数据库读取 A 的有效历史，保持其内容与语义，再追加 B 的新输入，使用 ROOT 也在使用的对话引擎继续执行。A→B→C 的连续体验由 task 历史继承提供；产品实体仍是现有 task。

本次实施包含：删除 task 继承专用的历史改写；将 profile 工作方法放入任务初始 USER 消息；让摘要消费者复用完整内容读取；将内置记忆工具限定为 ROOT；开放活动 task 的 terminal monitor 通知。沿用现有数据库、canonical reader、compiler、provider adapter、权限及工具 owner。

### SUPERSEDES

以下仅替代所列条款；各文档中的其他产品合同不因本规格整体失效。

| 历史文档 | 本规格替代的条款 |
|---|---|
| [worker_history canonical 分支设计](archived_docs/PULSARA_WORKER_HISTORY_CANONICAL_BRANCH_DESIGN.zh.md) | §6.2 将来源 snapshot 的 active request 转为 retained request 的要求；§7.1 第 2 项要求读取“历史化 retained”材料的表述。改为读取原 snapshot 内容。 |
| [Round 10 task 编排规格](archived_docs/ROUND_10_HIERARCHICAL_SUBAGENT_ORCHESTRATION_IMPLEMENTATION_SPEC.zh.md) | §5.1 profile 与通用 task 说明拼入 SYSTEM 的承载方式；§5.3 初始 USER 与裸 objective 相等的约定。改为初始 USER 保存工作方法与 objective 的完整组合值。 |
| [Round 5B 对话压缩规格](archived_docs/ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md) | §10.1.1 中 task profile supplement 进入 SYSTEM，以及相关初始 objective 内容校验的约定；通用 SYSTEM 与共享 cold assembler 合同保持。 |
| [Round 8 记忆子系统规格](archived_docs/ROUND_8_ADVISORY_MEMORY_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md) | §2.3 中 child 可显式调用 `memory_search` 的条款。内置记忆工具统一仅向 ROOT 提供。 |
| [Round 2 终端运行时规格](archived_docs/ROUND_2_TERMINAL_RUNTIME_IMPLEMENTATION_SPEC.zh.md) | §8.3、§8.5 中 monitor 仅允许 ROOT 调用／注册的要求，以及 §10.6 只有 ROOT 投递目标的范围。增加活动 task 的既有 turn 目标；ROOT 空闲时创建新轮等原合同保持。 |

ROOT 会话 fork 的独立产品合同不在本次改动范围。先前调研用于定位代码，本规格明确实施选择。

## 2. 输入组成与追加规则

无压缩来源的输入顺序：

```text
通用 SYSTEM；B 自己的 tools 请求参数

A 的有效任务消息、回答、工具调用／结果、已接纳的 ROOT 消息
→ B 本次指定的结果材料（若有）与当前运行信息
→ B 的 profile 工作方法＋任务正文
→ B 后续执行、工具结果及 ROOT 追加消息
```

已压缩来源将上述 A 部分替换为：

```text
A 已采用 snapshot 的完整内容＋尚未覆盖的有效尾部
```

具体规则：

- 使用已有来源 task、cut、binding 引用及祖先读取顺序。C 继承 B 时，不扩大 B 已冻结的 A 范围；兄弟分支互不混入后缀。
- 使用最近有效 snapshot 和未覆盖的尾部；不恢复压缩前全文，不重复插入被覆盖的祖先摘要、任务或初始材料。
- A 的普通正文、ROOT 消息、snapshot 原内容不因被 B 继承而加说明、移动字段或改变续接模式。B 的新任务本身表达任务切换。
- 工具配对、closure、迟到结果、图像 parts、artifact 引用、预算选择及兼容 native replay 使用现有共同机制。历史调用不再次执行；每段工具可见范围仍由该段 cut 决定。
- B 的身份、权限、模型及工具由 B 的现有 owner 提供。模型需要的当前事实按现有规则追加；工具定义由请求的 `tools` 参数提供。
- 每个新 task 使用现有独立 scope 和首次 cold 安装。已安装 epoch 内 SYSTEM／tools 保持不变，messages 仅追加；后续重建仍只允许既有 cold／显式采用压缩 successor 边界。

这里的原样继承指数据库有效历史的内容与语义。普通读取、共同渲染和协议适配继续存在；不复制上一完整 provider 请求，不增加原始输入保存、长期 worker 或新的历史存储。

## 3. profile 与任务初始消息

在现有 `subagents/contracts.py` 放置一个纯组合函数 `render_task_initial_request(profile_kind, objective)`，统一持有当前五种 profile 的工作方法原句。沿用 `general_worker`、`research_worker`、`review_worker`、`verification_worker`、`synthesizer`，本次不重写其专属工作方法。

在 task 实际启动的 initial turn admission 中生成并保存完整消息；排队或等待依赖的 task 不提前创建 transcript。保持现有任务启动与事务边界。

完整初始消息采用：

```text
Work style: [该 profile 的现有指导原句]

Task:
[ROOT 委派的 objective 原文]
```

例如：

```text
Work style: Investigate the delegated objective carefully, distinguish evidence from inference, and report sources or file locations that matter.

Task:
调查支付失败的原因，并提供代码与日志证据。
```

- `task.objective` 保持 ROOT 的原始委派文本；`profile_kind` 保持现有 task 元数据。完整消息写入已有 initial transcript entry，不新增 prompt 字段或表。
- admission producer、repository writer、确认路径及 cold seed 使用同一组合函数，校验完整初始消息，而非放宽或移除一致性校验。
- reader、UI 对话、字节计量及压缩直接读取该 entry。任务列表／任务说明仍可展示原 `task.objective`，无需从消息正文反向解析。
- 压缩保留当前请求原文时，同时保留其中的工作方法。后续读取 A 不使用 B 的 profile 重新生成 A 的消息。
- 删除 `_profiled_subagent_base_system()` 及仅服务于 SYSTEM 注入的接线。受委派身份宣告、禁止再次委派、自包含结果等重复段不整段搬到 USER；现有通用 SYSTEM、工具权限与结果工具说明继续承担各自职责。

## 4. 完整读取历史与摘要

### 4.1 历史读取

删除 task `historicalize_snapshot()` 及继承调用，保留来源 snapshot 原有 summary、active／recent／retained 内容和 continuation。当前请求由 B 的 `initial_entry_id` 识别，不靠清空 A 的 active 来识别。

删除 reader 中 `_historical_scope` 对 ROOT 消息补入 `recipient_task_id`、`content_semantics`、`handling` 的分支。当前和继承的 ROOT 消息使用同一现行投影，保留发送者与原正文。`_historical_scope` 读取冻结旧 binding 的用途保持。

### 4.2 共享消费者

摘要总结和其中保留的原文共同构成上下文。复用现有 `compaction_snapshot_sections(carrier)`，不让 UI、目的地投影再各自枚举一份不完整字段集。

- UI 展示原 summary 及完整 sections；已有后缀中的正文不再展开一次。
- 目的地历史投影读取完整 sections。旧 A 的 active-exact 正文按历史内容纳入；当前 B 的 active 仍由现有 exact request 机制单独保留，不重复。
- tier 3 若按现有预算选择省略 previous base，原 retained 内容和旧 A 的 active-exact 一起参与既有保留规则；recent 保持既有窗口，不强制永久保留全部 sections。
- `CANONICAL_SUFFIX` 的 active 没有内嵌正文，继续使用后续 entry。按条目身份识别当前请求与重复来源，不按文本相同合并不同请求。
- 完整内容枚举共用，UI 展示和模型投影可保留各自必要的格式；不修改原 carrier。

所有祖先的旧 active 均以其 `entry_id != 当前 canonical.identity.initial_entry_id` 识别，不依赖代际名称或正文内容。

实际可达场景：B 首次使用已知 text-only 模型，而继承历史含图像时，现有能力适配会进入 destination tier 3。删除历史化后，旧代码只读 summary／recent／retained 会漏读 A 的 active-exact。这一修正复用既有适配路径，不新增 task 模型切换功能。

最终消息顺序以现有 compiler 在 expanded transcript 中定位 B trigger 的逻辑为准，保证 A 的有效历史完整在 B 新内容之前。中间布局的无关清理不纳入本次范围。

## 5. 消息与运行信息

| 来源 | task 收到什么 | 跨 task 处理 |
|---|---|---|
| ROOT 委派、task 实际启动 | 完整的工作方法＋任务初始 USER 消息 | 每个 task 追加自己的新请求 |
| ROOT 对活动 task 补充要求 | 标明 ROOT 发送者的 USER 消息 | 下一普通模型准备前接纳入该 task 历史；继承时保持原内容 |
| 工具 | 匹配调用的结果、错误、图像／引用等 | 沿共同历史读取和工具协议处理 |
| `depends_on` | 成功依赖的结果摘要及实际数据 | 保持现有调度和 `DEPENDENCY_RESULTS` 材料 |
| `material_task_ids` | 指定终态 task 的实际结果或失败材料 | 保持 `TERMINAL_MATERIAL` 路径 |
| runtime observation | 环境、时间、权限、目录、Hook 和压缩交接等既有消息 | 按当前 task 范围提供，复用 ROOT 共有机制 |

维持现有协作语义：

- `worker_history` 与 `last_n` 是互斥 context 模式；均可与结果材料结合。历史继承不额外叠加 ROOT 最近对话摘录。
- 同一 task 同时列入 dependency 与 terminal material 时，保留现有 dependency 优先规则。不新增历史正文与结果材料之间的自动消重。
- task 之间没有直接聊天通道，task 也没有任意向 ROOT 发送中途消息的工具。用户在 ROOT 会话的 steer 不自动广播；ROOT 可发送明确的 task 消息。
- 终态 A 不接新要求；ROOT 创建 B 并指定继承来源。入队回执不等于模型已看到，仍以实际接纳的历史为准。
- 显式 report 的 assistant 工具调用和 accepted 工具结果属于本 task；普通 final 属于本 task。另行投递给 ROOT 的完成报告不反向插回 task。
- A 失败或停止，不自动给 B 增加 ROOT 的完成通知或“上一轮停止”观察；若需要明确原因，由 B 任务正文或指定终态材料提供。
- 除下述记忆工具与 terminal monitor 的调用方调整外，权限、工具可见性、自动记忆、Plan、Hook 及进程／TODO 的现有 scope 规则不变；继承对话不转移 A 的执行身份或活体状态。

### 5.1 内置记忆工具仅供 ROOT 使用

subagent 的工具定义中不包含 `memory_search`、`memory_get`、`remember`、`mark_memory_relation`；ROOT 保留这些工具的现有能力。

- 内置工具 catalog 中记忆读取工具的允许调用方改为 ROOT，与现有记忆写入工具一致；task 工具 surface 的过滤同步移除读取工具。
- 继续使用现有工具调用方和 scope 检查，不新增记忆权限层或 ROOT 记忆策略继承机制。
- 不为 task 新增自动记忆召回或偏好注入。ROOT 的记忆服务、数据、自动召回和既有用户选择规则保持。
- 已进入有效历史的记忆文字及历史工具调用／结果仍按通用历史规则读取；移除工具不改写旧消息，也不把历史调用变成 B 可执行能力。
- 通过现有 cold／adopted successor 边界采用新的工具 surface，不原地修改已安装 epoch 的工具定义。

### 5.2 MCP 资源、prompt 与 META 的边界

当前链路的分工如下；本规格不删除 `read_mcp_resource` 或 `get_mcp_prompt`。

| 入口 | 实际作用 | 是否包含另外两个入口的功能 |
|---|---|---|
| `list_capabilities`／`inspect_capability` | 列出与查看工具、资源、模板、prompt 的元信息；工具检查可返回 DIRECT／META 调用方式 | 不读取资源正文，不渲染远端 prompt |
| `use_new_mcp_tool` | 按检查所得的工具引用执行 MCP tool，最终调用远端 `call_tool` | 不提供资源读取或 prompt 获取操作 |
| `read_mcp_resource` | 根据已列出的 URI 或已填写的模板读取资源，最终调用远端 `read_resource` | 返回资源内容，不执行 MCP tool |
| `get_mcp_prompt` | 按已列出的名称和参数获取渲染后的 prompt messages，最终调用远端 `get_prompt` | 返回提示内容，不执行其中描述的工具调用 |

能力目录中的资源和 prompt 项明确标记为“已发现元数据；尚未读取或渲染”。因此目录检查、META 工具执行、资源读取、prompt 获取在当前代码中不互相替代。具体服务若只提供 tools，就不会用到该服务的资源与 prompt 入口；是否进一步合并产品入口另行讨论。

代码依据：`capability_query.py` 与 `mcp/directory.py` 生成元信息；`mcp/supervisor.py` 的工具执行及 `_invoke_standard_remote_result()` 分别调用上述三个远端操作。

### 5.3 terminal monitor：向活动 task 追加通知

subagent 可使用现有 `terminal_monitor register/list/cancel`，接收所监控进程的进展／结束观察，投递限于注册 task 仍活动期间。复用现有 coordinator、进程输出、观察内容和对话引擎，不新增工具 action。

**task 的最终回答或成功报告仍表示任务结束。** 注册 monitor 不使 task 挂起等待，不阻止显式／隐式完成；需要阻塞等待时继续使用 `terminal_process.wait`。ROOT 原有“一轮结束后由通知开启新轮”的行为保持，仅适用于 ROOT。

| 位置 | 实施合同 |
|---|---|
| 工具入口 | catalog、task surface 和执行入口共同允许 task 使用 monitor，沿用当前 task 的权限快照与现有操作审批规则 |
| 通知投递 | 根据注册者归属投给其活动 task；不使用当前活动 ROOT 作为 task 的投递目标 |
| 查看与取消 | ROOT 保持同 Host 全部 monitor 的查看／取消能力；task 只查看／取消自己注册的 monitor |
| 压缩交接 | task 获得自己注册的 monitor 状态，复用现有 handoff；ROOT 保持全局视图 |
| 活动 UI 事件 | opened／observation／closed 均携带注册者的真实 scope／task 归属，不默认投到 ROOT |

#### 归属与范围

- 注册者归属来自当前 invocation，而非模型提供的目标 task 参数。process origin 与 monitor owner 分开：注册仍使用当前工具可访问的同 Host 进程，不另加“只能监控自己启动的进程”限制。
- 注册和冻结的投递目标须与数据库中真实的 session／turn／task 归属一致。task 通知只能投给注册 task 的原有活动 turn，不能创建新 task 或新 turn；ROOT 的新轮路径保持 ROOT 专属。
- 当前每个进程最多一个有效 monitor、Host 总容量、输出／频率／生命周期约束保持。ROOT 与 task 的重复注册沿用现有冲突结果，不新增 task 配额或并行订阅体系。
- 取消 monitor 只停止后续监控，不结束被监控进程。task 不能因持有另一个注册者的 monitor ID 而取消对方订阅；ROOT 的全局管理能力保持。

#### 接纳与任务结束

- task 在自己的下一次普通输入准备安全点消费待投递观察，使用该 task 的 safe point、continuity、compaction 协调和取消意图。Host 的 ROOT runner 不能代替 child runner 写入通知。
- ROOT Host 投递分支只选择 ROOT 注册者的待投递项；task 项由其 task 的普通安全点处理，不得因排在全局 pending 队首而误投或阻塞 ROOT。
- 复用现有活动 turn 的输入准备、预算校验、发布和确认机制，使 terminal observation 的既有 turn 路径按真实 scope 工作。repository 写入／确认不能继续硬编码 ROOT；ROOT 真人输入、Plan 和其他 ROOT 专属入口不因此开放给 task。
- 模型调用期间、已冻结输入以及已采用的 compaction successor 不临时插入通知；后到内容留到下一普通准备。ROOT 的新轮选择、用户输入优先规则及 ROOT compaction fence 不作为 task 通知的全局调度条件。
- 通知草稿不阻止 task 的 final／report。成功、失败、取消、中断等真正终态均关闭该 task 的 monitor；Stop Hook 要求继续时 task 仍活动，不提前关闭。完成尚未提交或被撤回时，不把 task 当作终态。
- 终态时丢弃尚未冻结的草稿，停止未来订阅。已冻结 attempt 不更换目标或内容：先按现有确认机制处理；确认已写入则保留历史并结算，确认未写入且目标 task 已终态则结束该 attempt，不继续对终态 turn 重试。
- 已冻结 attempt 由现有 Host／coordinator 继续持有并负责确认，不随 child runner 退出释放。目标 task 终态后仅确认旧 attempt，已写入／未写入结算后释放；Host 关闭或进程丢失仍沿既有弱语义，不增加恢复 job。
- 清理失败不改写已提交的任务结果。终态后的确认只处理既有 attempt，不追加新通知、不唤醒 ROOT、不复活 A、不转投继承者 B。
- A→B 继承只带入已进入有效历史的通知及工具记录，不转移 A 的活跃 monitor。B 可独立注册新的 monitor。

示例：

```text
A 启动后台命令 p，注册 monitor(p)
A 继续检查其他文件
下一普通输入准备：向 A 追加 p 的进展观察
A 继续处理；若发生压缩，交接 A 自己的 monitor 状态
A final／成功 report：A 结束，关闭其 monitor；命令 p 不因取消监控而被杀死
B 继承 A：可见已接纳的历史通知，不接管 A 的活体订阅
```

monitor 仍为同 Host 的进程内状态；重启不恢复订阅，未接纳草稿仍为 best-effort。复用既有 observation entry、committed occurrence 与确认逻辑，不增加持久表、事件种类、job 或独立任务等待循环。

### 5.4 A→B→C 示例

```text
A USER：研究方法＋“调查支付失败的原因”
A ASSISTANT／TOOL：调查
A ASSISTANT：调查结论

B 继承 A 的有效历史
B USER：审查方法＋“审查 A 的结论”
B ASSISTANT／TOOL：审查
B USER〔ROOT〕：“补查回调超时路径”
B ASSISTANT／TOOL：继续检查
B ASSISTANT tool call：report_agent_result(summary=审查结论)
B TOOL：报告已接纳

C 继承 A→B 的有效历史
C USER：执行方法＋“按审查结果修复并验证”
C ASSISTANT／TOOL：实施
```

示例省略与 ROOT 共用的运行观察。ROOT 的完成通知各归 ROOT，不出现在上述继承段；材料未指定时不添加。

## 6. 最小实施落点

| 文件／owner | 修改 |
|---|---|
| `subagents/contracts.py` | 单一初始请求组合函数与现有 profile 指导；不新增业务字段 |
| `runner.py`、`_repository/subagents.py`、`cold_epoch.py` | 初始消息生成、写入／确认、cold seed 校验统一使用完整消息 |
| `context_sources.py`、`provider_dispatch.py`、subagent runtime 接口 | 删除 profile SYSTEM 注入及其专用参数／查询接线，保留其他任务材料和能力采集 |
| `subagents/history.py`、`reader.py` | 删除继承专用摘要与 ROOT 消息改写，保持固定来源读取与共同 compiler |
| `_repository/subagents.py` 的任务活动 UI | 复用完整摘要 sections，展示原内容 |
| `compaction/planner.py`、`compaction/coordinator.py` | 目的地投影与 previous-base 选择完整读取旧历史请求，当前请求不重复 |
| `src/pulsara_agent/capability/builtin_catalog.py`、`tool_runtime.py` | 记忆工具仅允许 ROOT；task surface 不含任何内置记忆工具，ROOT 能力保持 |
| `ports/terminal.py`、`capability/builtin_catalog.py`、`conversation_kernel/tool_runtime.py` | monitor 工具说明区分 ROOT 与活动 task，调整调用方、scope 过滤及当前权限下的执行接线 |
| `terminal_process/monitor.py`、`ports/terminal_observation.py` | 注册者归属、按 owner 筛选、冻结目标校验、终态取消与 in-flight 结算；活动 UI 事件带真实 scope |
| `conversation_kernel/host.py`、`runner.py`、`subagent.py`、`safe_point.py`、`_repository/conversation.py` | task 自身普通准备安全点投递；共享 existing-turn 观察接纳／确认；完整终态清理，ROOT 新轮行为保持 |
| `conversation_kernel/tool_runtime.py` 的 compaction handoff | monitor 按注册者而非进程 origin 为 task 选择，ROOT 视图保持 |
| 相关测试、bundled subagent Skill 与有效说明 | 与本规格一致；仅更新实际受影响内容，不把内部实验或改动过程写进模型提示词 |

表中仅写文件名或 `subagents/`、`compaction/` 等内部目录的源码路径，以 `src/pulsara_agent/conversation_kernel/` 为基准；明确带 `ports/`、`capability/`、`terminal_process/`、`conversation_kernel/` 的路径以 `src/pulsara_agent/` 为基准。组合与读取复用现有 owner，不增加事件种类、job、快照种类、DTO fingerprint、worker 生命周期或历史深度上限。

## 7. 实施与验收

作为一次完整 hard cut 实施，不保留旧／新双路、兼容猜测或回退历史化。现有运行中 epoch 不原地重写；通过既有新 cold 或已采用 successor 边界生效。开发数据沿仓库 clean-v0 规则处理，本规格不要求新增数据库迁移或历史回写机制。

行为验收覆盖：

1. 无压缩与已压缩 A→B→C；A→B、A→C 兄弟隔离；固定 cut 之外的后到内容不混入已接纳分支。
2. A snapshot 内容不因继承而改写；ROOT 消息当前／历史投影一致；B 新任务仅出现一次，A profile 不被 B profile 替换。
3. 初始完整消息在 producer、writer、确认、cold seed、UI、字节预算和 active exact 压缩中一致；再次压缩仍保留 B 当前请求的工作方法。
4. 工具调用／结果、closure／late、图片及 artifact 继续合法继承，不重执行，不因原文重复而按内容消重。
5. 现有 text-only＋历史图像的 tier 3 路径：旧 A active-exact 不漏读，当前 B 不重复，previous base 省略时保持既有选择语义。
6. ROOT 中途消息在接纳后进入正确 task；终态 task 不接消息；显式 report 调用／回执可继承，ROOT 完成通知不反灌。
7. 未指定的材料不新增；已指定的 dependency／terminal material 与 `last_n`、`worker_history` 现有关系保持。
8. 已安装前缀连续性、权限及工具 scope 的现有行为回归保持。
9. task 的首次输入及压缩后工具 surface 均不暴露四个内置记忆工具；调用方规则不允许 child 使用，ROOT 的记忆读取和写入能力保持。
10. task monitor 的 register／list／cancel、进程与注册者不同归属、单进程重复订阅、ROOT 全局管理、正确通知／活动 UI 归属及 task 压缩 handoff；现有权限规则保持。
11. monitor 到达模型调用或冻结 successor 期间时不改既有输入；final／report、Stop Hook 继续、失败／取消／中断及终态清理竞态；已写入／未写入两种晚确认不重复、不永久重试、不覆盖终态结果。关闭订阅不 kill 进程、不自动转给 B。
12. ROOT monitor 的活动轮投递、空闲新轮、用户输入优先、Host close 和现有资源边界行为保持；不引入 task 的结束后自动续轮。

使用仓库 `.venv` 运行聚焦的行为和 PostgreSQL 集成测试；不冻结提示词全文、固定 token 值或 golden hash。实施收口通过保存的生产配置进行一次真实模型的 A→B→C 输入检查，记录实际输入顺序、继承内容与结果归属，不以缓存命中率为条件。

## 8. 代码调研依据

- [Astra：append 方案 critic](output/runtime-observation-revision-20261027/worker_history_astra_review/append-critic/REVIEW.zh.md)
- [Astra：多任务消息矩阵](output/runtime-observation-revision-20261027/worker_history_astra_review/multi-task-messages/REVIEW.zh.md)
- [ROOT：运行信息核对](output/task-message-flow-20261027/root/RUNTIME_REVIEW.zh.md)

上述材料提供代码定位；实施选择以本规格和后续用户指令为准。
