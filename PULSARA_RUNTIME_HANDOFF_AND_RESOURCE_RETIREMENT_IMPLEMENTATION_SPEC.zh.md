# Pulsara：运行时摘要状态筛选与资源退役实施规格

日期：2026-10-30。状态：**实施与本规格要求验收完成。** 实施基线为 `fc0d8d86`。实现、亲审修复、证据层级及验证边界见 [总交付报告](output/runtime-handoff-retirement-20261030/REPORT.zh.md)。

本文明确 Monitor 安全删除及旧通知确认、Process 到期自动尝试回收、普通环境 Cell 索引移除，以及压缩 runtime handoff 的状态筛选。TODO 始终完整保留；3,200 个 V3 启发式 token 上限仅约束 PMCA 状态与相关说明，不约束 TODO。

## 0. 权威、范围与不变量

根 [AGENTS.md](AGENTS.md) 与用户当前决议优先。本文承接 [工具查询与 Cell 身份统一规格](PULSARA_TOOL_QUERY_AND_CELL_ID_UNIFICATION_IMPLEMENTATION_SPEC.zh.md) §15 中与机械 runtime handoff 直接相关的部分；其他行为遵循各自的现行规格。

现行相关合同：

- [Code Mode 执行、资源与生命周期](PULSARA_CODE_MODE_TOOL_SUBSET_DESIGN.zh.md)。
- [Agent／Work 与两段退役](PULSARA_SUBAGENT_WORK_CODE_MODE_DESIGN.zh.md)。
- [子代理与 Cell 侧边栏规格](PULSARA_SUBAGENT_CELL_SIDEBAR_DESIGN_RESEARCH.zh.md)。
- [工具 schema 兼容性实验](output/tool-schema-compatibility-20261026/REPORT.zh.md)：本轮不修改工具参数 schema 或 provider lowering。
- 调查证据：[摘要与查询](output/compaction-query-research-20261030/REPORT.zh.md)、[资源计费与退役](output/compaction-query-research-20261030/RESOURCE_RETIREMENT.zh.md)。调查中的候选方案只在本文明确采用后才构成实施要求。
- 尺寸依据：[状态筛选后摘要的 V3 离线矩阵](output/compaction-query-research-20261030/token-budget/REPORT.zh.md)。本轮上限是单份观察中 PMCA 视图的输入成本边界，不是 TODO、对象数量或执行寿命限制。
- 补充依据：[规格复审与合法 TODO 转义反例](output/compaction-query-research-20261030/spec-review/REVIEW.zh.md)、[普通环境 Cell 索引链路与矩阵](output/runtime-environment-cell-index-20261030/REPORT.zh.md)。旧尺寸实验中的重复中文 TODO 仅为合成尺寸样本，不作为合法 TODO 上界证明。

### 0.1 已明确决议

| 范围 | 实施合同 |
|---|---|
| Monitor | CLOSED 后继续排除摘要与 query；原有注册和通知结算完成后删除内部登记，不保留关闭历史或 tombstone |
| Monitor 缺失 | 删除后取消返回既有 `not_found`；工具结果附通用解释，不追踪或断言每次消失原因 |
| Process | 沿用结束后的保留策略，增加到期自动尝试回收；有使用者或物理收尾未结束时暂缓并再次尝试 |
| Cell | 不新增数量、寿命或终态保留 cap，不按时间丢弃未交付结果；原资源计费、输出确认和回收规则保持 |
| Agent | 沿用同一 idle 起点的 1 小时资源卸载／3 天删除及安全门禁，不增加第二套退役机制 |
| runtime handoff | 只主动列入符合 §6 状态条件的对象身份和必要状态；其他仍可查询的对象通过既有 query 查找 |
| 空 handoff | PMCA 候选全空且没有 TODO 时返回空 source；不为单独发送承接句制造摘要 |
| 普通环境 | 删除模型输入中的 Cell 索引及专用采集／筛选／预算／省略链路；环境保留工作区、时区等事实 |
| 用户 Cell 控制 | 保留用户 UI 查看／停止 Cell 的接口和数据来源；删除模型环境投影不删除用户控制能力 |
| 权限 | `query_agents` 供 ROOT 与 Subagent bypass，按调用方范围查询；`query_cells` 仅供 ROOT bypass |
| 摘要预算 | 仅 PMCA 计量视图最多 3,200 个当前 V3 启发式 tokens，含说明与封装；TODO／todo_counts 完整追加且不占用此额度；完整输入仍受现有 provider 输入预算约束 |

### 0.2 不变量与非目标

1. 不新增数据库表、列、持久事件／任务、subject、append guard、对象注册表、关闭历史表、指纹字段或恢复链；durable oracle 相对实施基线各类别增量为零。
2. 不增加工具、action、参数、旧名兼容层或执行双轨。Monitor 只在既有结果正文中补充提示。
3. 不限制 Process 的运行寿命，不自动停止活跃 Cell／Work，不以摘要容量拒绝逻辑工作。
4. 不为清理制造新的交付确认协议；复用现有 token、in-flight attempt、输出确认、物理 join、锁和 owner。
5. SYSTEM／provider tools 仅在 cold epoch 或明确采用的 compaction successor 安装新定义；已有 epoch 字节不变、messages 只追加，不新增重建边界。
6. runtime handoff 是 runtime 生成的状态观察，不推断启动意图、重要性或下一步行动。模型生成的语义交接摘要继续独立负责其原有内容。
7. 不新增 UI 页面，不重写 TODO／Plan 工作流，不统一所有环境来源。仅删除普通环境的 Cell 索引，保留其余环境信息、用户 UI Cell 控制和压缩 handoff 的 Cell target。

## 1. 对象退役与摘要筛选的边界

三个不同问题分别由现有 owner 处理：

| 问题 | 含义 | 权威 |
|---|---|---|
| 还需执行或结算吗 | 物理执行、未完成调用、注册／通知交付、未确认输出 | ProcessRegistry、TerminalMonitorCoordinator、CodeModeHost、共同 Work 引擎 |
| 记录可以删除了吗 | 原有使用者与结算依赖已释放，符合该对象的保留政策 | 对应唯一 owner；摘要不参与裁决 |
| 本次摘要主动显示吗 | 在采集时符合明确的状态和调用方范围 | owner 提供事实，纯 handoff renderer 投影 |

从摘要中排除不等于删除；能 query 不等于仍在执行；从 query 中消失也不说明业务成功或失败。不得为了让摘要变小而确认输出、关闭 Monitor、缩短 Agent idle 时间或强制删除对象。

## 2. Cell：保留现有资源和交付合同

### 2.1 资源边界

默认单 Cell 16 MiB／Host 64 MiB 是 sidecar 和 worker 共用的协议逻辑数据计费；每 Cell 的 control、catalog、启动数据、输出、调用及结果交付按既有 reserve／release 规则计费。它不等于 Python／Node 的总 RSS。

Python 对象、asyncio task、闭包、快照引用、反序列化副本、Node Worker／WASM、IPC 瞬时复制以及其他工具／artifact owner 的存储，不由这个计数器按实际内存完整覆盖。QuickJS heap／stack、VM 并发、单值／单帧及模型结果页各自沿原限制。

终态未领取的 Cell 仍保留 control／catalog 和未读输出等 charge，可能令新接纳返回既有 `data_boundary`。执行期间的 reserve 失败及晚到结果继续走既有 typed 资源失败与效果结算；不能把交付失败写成外部业务没有发生。

本轮不新增 Cell 数量 cap、TTL、自动归档、显式清理 action、内存追踪系统或新保留账本，也不借摘要投影提前释放仍被执行或结算引用的数据。

### 2.2 正常回收条件保持

只有以下条件同时满足，才沿原路径从 `_cells` 删除并发送 `forget`：

- Cell 已终态；
- 终态观察已经成功确认，`terminal_delivered` 为真；
- 整个内部 `pending` 字典为空；
- 输出队列为空；
- 没有持有中的输出 observation；
- VM 已释放。

`pending_calls == 0` 不等价于内部 pending 为空：prepare 等请求不一定计入公开调用数。源码 query 不消费输出、不设置 terminal_delivered；stop 不代替输出交付，也不取消已接纳的独立 Work／外部调用。Host 关闭及 sidecar 失败继续使用原有清理和失败路径。

本轮对 Cell 的生产修改限于 §6 所需的 owner 状态观察、普通环境索引删除及投影接线；用户 UI 查看／停止路径保留，不改变以上执行、收费或交付规则。

## 3. Monitor：关闭后的安全删除

### 3.1 关闭与删除

`TerminalMonitorCoordinator` 是内部登记删除的唯一 owner。`CLOSED` 立即退出 `query_monitors` 列表／详情和 runtime handoff，这个行为保持。

物理删除必须在既有 coordinator 锁内检查，且同时满足：

1. 映射仍指向本次检查的同一个登记，状态为 CLOSED。
2. 原关闭动作已经完成：mutable draft／successor 已清理，订阅已解除，原有关闭事件发送路径已执行。
3. 现有 `_tokens` 中没有尚待结算、指向该登记的注册 token。
4. `in_flight is None`。

关闭事件沿现有 best-effort live bus 语义，不增加“模型已看见关闭通知”的确认要求。不可仅按关闭时长强行越过注册 token 或 in-flight 的结算依赖。

### 3.2 触发位置与顺序

复用一个 coordinator 内部删除检查，在三个现有完成点调用：

- 原关闭处理完成后；
- `settle_registration` 验证并弹出注册 token 后；
- `settle_installation` 明确结算并清空 in-flight、完成该次状态更新后。

注册先关闭后结算时，仍须先验证 token 身份并退役 token，再尝试删除；不能因登记不是 DORMANT 就跳过清理检查。通知先冻结后取消时，关闭只取消 mutable 部分，原不可变 installation 保留到既有 FULL／NONE／conflict 确认结束。UNKNOWN 保持原对象，不新增超时丢弃或伪造确认。

同一内部 helper 只检查当前事实并删除，不创建待清理队列、第二份 closed 集合或按 ID 保存的历史缓存。

### 3.3 Host 先确认旧通知，再判断是否允许新通知

Work 已结束不代表已冻结通知的写入结果已经确定。Host 的 Monitor 投递循环必须先取得现有 immutable installation attempt；如果存在，先复用 safe-point 的原精确确认路径，再判断当前 Work 是否仍允许创建新通知。

- 已确认 FULL：按原成功路径结算；已确认 NONE／conflict：按原对应路径结算。清空 in-flight 后再检查 CLOSED 登记是否可以删除。
- 仍是 UNKNOWN：保留原 attempt 和登记，结束本次对该对象的尝试，沿既有唤醒／重驱路径等待后续确认。不得在同一轮不停选中同一 ID、重复 close／continue 或阻塞其他可处理 Monitor。
- Work 不再活动且没有待确认 attempt：可以关闭登记并安全清理；不为旧 Work 生成、冻结或安装新草稿。
- 已冻结 attempt 始终归原 Work／原安装目标，不转投下一份 Work，不改写通知归属，不把 UNKNOWN 伪造为 NONE。
- 确认读取与写入沿现有 owner、权限和终态约束；本轮不增加新确认协议、持久重试队列或新的执行恢复路径。

必须修改 Host 投递循环中“先检查 Work 是否活动、否则 close_work 后 continue”的原顺序，不能仅给 coordinator 增加删除 helper。该顺序要求覆盖 Work 结束／取消、注册结算、Host 关闭及 immutable attempt 已存在时的交错。

### 3.4 并发与关闭

- `process_completed`、`close_work` 等遍历登记并关闭对象的路径使用安全快照或收集后删除，禁止迭代字典时直接改变其大小。
- 已复制的输出 callback／worker 可以晚到；按 ID 重新取登记，不存在或 CLOSED 就结束，不能借旧引用重新创建登记或通知。
- Host 在按 ID 取 scope、取 installation attempt 与结算之间的竞争，仍以现有冻结 attempt 和结算 owner 为准。删除检查不得令一个已接纳且尚需结算的 attempt 失去 owner。
- 重复／冲突结算沿现有精确身份校验处理；不将所有 stale／identity conflict 改为无条件成功。
- Host close 和 Work close 都走同一关闭／删除条件。存在 UNKNOWN 等未决依赖时可暂留 CLOSED 记录；在依赖真正结束后再次尝试，不给这些依赖新增持久恢复机制。
- 清理失败不得妨碍已经生效的关闭事实；错误走现有诊断路径，不吞掉真实结算异常。

## 4. Monitor 工具结果：缺失解释与可见性

### 4.1 既有结果分类保持

| 调用及事实 | 保持的外层／正文 | 本轮调整 |
|---|---|---|
| `manage_monitor.cancel` 找到未关闭对象并关闭 | 外层 SUCCESS；`status:CANCELLED`；`cancellation_outcome:cancelled` | 安全条件满足后内部登记可删除 |
| 登记已关闭但仍因结算保留 | 既有 `already_terminal` 结果 | 不为立即消除该结果而越过安全条件 |
| cancel 不存在或当前范围不可见 | 外层 SUCCESS；`status:REJECTED`；`cancellation_outcome:not_found`；原 target | 添加通用 `message` |
| `query_monitors` 精确 target 不存在、已关闭或不可见 | 外层 ERROR；既有 `status:REJECTED`、`reason:MONITOR_UNAVAILABLE` | 添加相同含义的通用 `message` |

模型可见提示采用：

> The monitor is unavailable in the current scope. It may have been cancelled, expired, or reclaimed. Use query_monitors without a target to list currently visible monitors.

提示说明的是可能原因，不断言对象确实存在过，也不承诺列举能找回已删除对象。不得为了精确说明 `not_found` 的来源读取其他 Work／Host 的登记、建立 tombstone，或区分“外部对象存在但无权看见”。

实际提示放在现有 tool runtime 结果构造处，复用统一正文渲染；成功关闭不冒充 Process 已停止。既有 renderer 对附加 message 正常展示，无新 UI。

### 4.2 参数与 public 行为

输入 schema、target、action、权限、ROOT／Subagent 范围不变。再次取消已物理删除的 ID 返回 `not_found` 是明确接受的产品行为，不要求无限期返回 `already_terminal`。原始格式不合法的 ID 仍在参数校验层拒绝，不能统一伪装成已过期。

## 5. Process：结束后到期自动尝试回收

### 5.1 复用范围

继续由 `ProcessRegistry` 管理 `_states`、单调时钟结束时间、finished TTL、已有数量压力策略和 `_mark_prunable`／物理收尾。

- 默认 finished TTL 保持 3600 秒，从现有 ended_at 起算。
- 默认 32 条结束记录的压力策略保持，包括现有最老候选选择与受阻时跳过的行为；不借本轮重写该策略。
- 运行进程、子进程组或 interop 仍存活的对象不因保留 TTL 被终止。
- 到期意味着可以尝试清理，不意味着无论使用者是否结束都必须删除。
- 本轮不调用执行寿命 `_expire` 来实现记录回收，不新增运行时长上限。

### 5.2 一个 owner 内的自动触发

在现有 Registry 中增加一个进程本地 condition 等待工作线程，复用该 Registry 的状态和同步边界。新增机制仅负责到期唤醒，不是通用调度器，不创建每 Process 一个 timer、持久 job、到期事件表或对象副本。

1. 有需要管理的状态时启动，整个 Registry 同时最多一个回收线程。
2. 按真实已结束记录的最早 `ended_at + finished_ttl_seconds` 计算下次唤醒。运行对象不是 TTL 候选。
3. 进程结束、状态增加／移除、owner release／shutdown 时唤醒并重新计算；使用既有单调时钟。
4. 到期后调用同一安全回收判断；启动新 Process 时原有机会式回收仍复用同一实现，不形成另一套删除规则。
5. 到期但安全条件受阻时，通过同一个线程等待后重查。采用 1 秒重查间隔，防止已过期时间导致零等待空转，并让占用释放后无需再启动新命令即可清理；这是单次维护频率，不是记录寿命、重试次数或等待总时长上限。
6. 没有到期候选时按最近到期点等待；没有待管理记录或在途接纳时允许线程退出。后续合法接纳可由同一个 owner 再启动，创建／退出竞争不得产生两个线程。

已有 output owner 的 condition 通知不直接唤醒 Registry，因此不能仅声明“复用现有通知”而漏掉重试。本文选择单线程有限间隔重查，不要求增加跨 owner 通知订阅。完成与关闭路径仍需唤醒，以便及时重新计算和退出。

### 5.3 原子读取保护与安全删除

当前“Registry 取到 state → 释放锁 → output 开始计 inflight”的窗口必须与主动回收一起收口。

同一 Process 的读取／观察／订阅接纳和删除应满足：

- 如果读取先取得资格，回收必须看见它并暂缓；如果删除先完成，后续请求按原不可用结果结束。
- 取得仍属于当前 Registry／owner 的精确 state 与登记本次读取保护，应处于一致的锁／现有使用计数协调边界内。
- 物理 join、安全计数检查、标记 PRUNABLE 和最终移除之间须再次核对对象身份，防止旧引用绕过已关闭的读取接纳。
- 复用现有 observation lease／inflight read 及物理状态，不新增第二套租约表、generation 或恢复记录。
- 核对 `_owned`、元数据 query／列表、poll／wait、Monitor 订阅、snapshot 与 Host release 的实际使用点；不只修一个调用入口。
- 保持明确锁顺序，不在会阻止读取释放的锁内等待长时间 join；超时或失败沿现有实际结果处理。

回收过程中不得读取并消费业务输出，不创建 artifact 副本。已经回收的 Process 不能由 query 自动重建或扫描操作系统找回。

### 5.4 生命周期

正常生产释放经过 `release_owner` 和超时 force 路径 `release_owner_and_join`，不能只在 `Registry.shutdown` 停线程。

- 最后一个 owner 的状态与在途接纳清理完后，唤醒并收束维护线程；在锁外 join。
- 共享 Registry 仍有其他 owner 时，只移除被释放 owner 的状态，不能关闭其他 owner 所需维护。
- Subagent 释放借用的工具 port 不停止共享终端 owner。
- 明确 shutdown 后拒绝重启；普通 owner 释放后若 Registry 仍合法复用，重新接纳沿既有规则，而非把它误标为永久关闭。
- join 使用既有关闭 deadline／force join 生命周期，不能遗留失去 owner 的后台线程，也不能持 Registry 锁等待该线程退出。

## 6. Runtime handoff：按状态主动展示身份

### 6.1 筛选矩阵

以下决定进入本次逻辑候选集合，与 §8 的 token 装箱分开。筛选只使用现有真实状态，不调用会消费输出的操作。

| 对象 | 主动进入条件 | 主动排除 | 保留的最小公开事实 |
|---|---|---|---|
| Process | 现有物理存活判断为真，包括仍存活的进程组／interop | 已结束且仅保留历史记录的对象 | PROCESS target、`status:running` |
| Monitor | 当前调用方可见，状态 ACTIVE 或 DORMANT | CLOSED，无论内部是否尚待结算删除 | MONITOR target、PROCESS target、state、既有 pending_observation |
| Cell | queued／running；或终态但尚未确认终态；或 output 非空；或内部 pending 字典非空 | 终态已确认、无输出、无 pending，仅剩 observation／VM 释放等内部收尾的对象 | CELL target、state、has_unread_output、既有 pending_calls |
| Agent | 属于调用方可查询范围，`current_work_id IS NOT NULL OR queued_count > 0` | 尚保留但没有当前或排队 Work 的闲置 Agent | AGENT target、current_work 的 WORK target 或 null、queued_count |
| TODO | 沿当前 ROOT run 的完整冻结快照 | 原 owner 判定没有当前清单时为空 | 原顺序、status、text 和原 counts |

Process 的命令预览／cwd、Agent 名称预览不再作为对象候选行的必需内容，完整信息由原 query 提供。Monitor 与当前 Work／Process 的直接关联、Agent 当前 Work 以及 Cell 的未读／pending 状态仍保留，不能只输出难以区分的一串裸字符串。

Cell pending_calls 保持公开字段原有含义，不把 prepare 改计入它以凑摘要。进入条件由 CodeModeHost 根据完整内部 pending 与 terminal_delivered 判断；不向模型暴露 source attempt、回调、执行快照或 Promise 内容。列出的终态 Cell 仍可能需要一次既有 wait 来完成终态／输出观察，即使公开未读数量为空；此规则由现有 Cell 工具／Skill 使用指导承担，不额外增加每份摘要的固定说明。

所有对象身份使用既有完整 target。当前 `code_cells` 中的裸 cell_id 改为 CELL target；不添加旧字段双读或别名。输出 key 可沿用 `terminal_processes`、`terminal_monitors`、`agents`、`code_cells`、`todos`、`todo_counts`，但每项字段与上表一致。

### 6.2 调用方范围与 Monitor 采集修复

- ROOT 的 Process 使用既有 Host owner 范围；Subagent handoff 继续按真实 origin session 限定。不得为了与 Process query 的共享 owner 范围完全一致而扩大 Subagent 摘要。
- Monitor ROOT 沿全 Host 可见范围；Subagent 必须同时传真实 `agent_id=session_id` 和本次真实 `work_turn_id` 给 coordinator 现有过滤入口。
- 当前 Subagent handoff 无参冻结全部 Monitor 后读取不存在的 `session_id` 字段，这条错误路径在本轮替换。不得通过 `.get()` 跳过所有记录掩盖问题，也不能退化成仅 Agent、不限 Work 的过滤。
- 当前 freezer 接口不足以携带真实 Work turn 时，从已有 compaction scope／调用上下文显式传入；不借“最近一次调用”缓存猜 turn，不新增当前 Work 映射。
- ROOT 的 Agent 范围为其当前成员；Subagent 的 Agent 范围为自身及已知成员，不获得 ROOT 全库存或 Cell 库存。Cell 只来自当前 CodeModeHost；已知集合以 [已知对象与交接规格](PULSARA_SUBAGENT_KNOWN_AGENTS_QUERY_AND_HANDOFF_IMPLEMENTATION_SPEC.zh.md) 为准。
- 这次观察的 scope 不授权之后的工具调用。query 继续检查执行时的权限、范围和存活性。

### 6.3 Agent 不能只过滤前 50 行

现有 handoff 读取所有 Agent 的首页 50 行，再制造特殊 `{omitted_agents,next_cursor}` 条目，不能继续用于新的活跃状态筛选。

在现有 repository owner 内复用 `_RUNTIME_SELECT` 的 current_work／queued_count 推导：先按调用方查询范围和活跃条件筛选，再按 Agent ID 排序并沿现有读取边界分页采集。不得先 LIMIT 全库存首页，再在 Python 过滤，否则前 50 个闲置 Agent 会遮住后面的活动 Agent。

handoff 复用该 owner 的只读范围和活跃筛选，不为 handoff 新增表、索引、Agent 状态投影权威或模型工具过滤参数。复用真实 request／turn 状态，不能将加载在内存与否当成 Agent 活跃。

50 只作为既有单页读取大小，不作为候选对象总量上限。预算装箱前不混入分页标记伪装 Agent；按 §8 在预算允许范围继续读取活跃候选，确认下一项装不下时停止，不为计算遗漏总数扫描全部库存。读取失败沿既有 typed 错误路径处理，不能伪装成预算省略或空库存。

### 6.4 Cell 由 owner 生成状态观察

现有 `snapshot_cell_index()` 和 `capture_cell()` 都不足以独立判断 terminal_delivered 与全部 pending。由 CodeModeHost 在原事件循环同步观察中生成本次 handoff 事实，直接执行 §6.1 谓词；复用既有 target／公开字段构造，不在 tool runtime 用不完整公开 index 做二次猜测。

候选集按 canonical Cell ID 排序；不得调用 observe／acknowledge、占用输出 reservation、阻止 forget 或以源码存在为由恢复已回收 Cell。捕获后 Cell 可正常回收，快照不保证后续 wait/query 一定成功。

普通 environment source 的 Cell 索引按 §6.6 删除，不再维护另一套筛选谓词。压缩 handoff 仍由本节 owner 事实提供 CELL target；不能因为普通环境不再展示索引而删除这条压缩交接路径。

### 6.5 查询指引与模式边界

每份 runtime handoff 均用 §8.2 的第一句说明承接压缩前的工作，并指示使用已有 target 继续。只有本次符合状态条件的对象因 token 预算未完整展示时，才添加 §8.2 的第二句查询指引；未超限时不附加查询说明。摘要本身按状态筛选，未列出不证明对象不存在，但不额外反复强调状态不可靠或要求先全面重新核查。

ROOT bypass 是本方案主要使用场景。非 bypass／Plan 仍可能不提供 Agent／Cell query；不宣称查询始终可用，不要求模型为取库存自动改变权限或绕过 Plan 批准／退出规则。模式切换不取消对象原执行快照，也不迫使摘要把所有闲置对象补回来。

查询不会找回已删除 Monitor、已回收 Cell、已 prune Process 或已删除 Agent。详情和状态是查询当时的事实，不能保证重现摘要冻结时的库存。TODO 没有只读 query，完整清单继续按原 handoff 合同保留。

### 6.6 删除普通模型环境 Cell 索引，保留用户控制

普通 `RUNTIME_ENVIRONMENT` source 不再自动附带 Cell 库存。完整移除该投影的 `cell_index`／`omitted_cell_index_entries` JSON 字段、collector 的 `cell_index_provider` 注入及保存、专用筛选／排序／32 KiB 装箱／省略计数和对应说明。旧名称 `code_cells`／`omitted_code_cells` 不作为兼容字段恢复。

删除该采集回调也移除了已复现的跨线程索引遍历入口；保留的 UI 与压缩采集继续在 Host 所属事件循环内读取，无需新增并发机制。

环境 source 继续提供原工作区类型、根目录、时区、UTC offset 等其余事实，其自身采样与后缀追加合同保持。删除索引专用字节预算和共享常量引用；不把该预算另迁回环境 owner，不新建替代索引 source。与索引无关的既有输入、传输和资源边界不在删除范围。

模型的 Cell 状态来源为：既有 `exec_cell`／`manage_cell` 结果、按需 `query_cells`，以及压缩后按本规格筛选和装箱的 handoff。不能承诺模型天然知道实时状态，也不为删除索引扩大非 bypass 查询权限。

`CodeModeHost.snapshot_cell_index()` 仍可供用户层面的查看／精确停止路径及必要内部消费者使用。保留 Host 的 `code_mode_cells()`、gateway／proto 的 Cell 状态响应、桌面 bridge／现有 UI 查看和停止入口，名称和外部协议不因本次模型投影删除而改变。只删除普通环境 consumer，不机械删除所有索引方法／类型／调用。

压缩 handoff 中独立的 `code_cells` 数组保留并使用 CELL target；不将其与已删除的普通环境字段混为一谈。不得为保留 UI 功能把 UI 的 Cell 列表重新自动注入模型环境。

## 7. Agent 退役及其他保留政策

Agent 无 pending Work／running turn 后进入原 idle 状态；同一 idle 起点满 1 小时尝试卸载资源但保留 session／历史，满 3 天尝试删除。两步都保留既有安全关闭检查，新 Work 清除 idle 起点；卸载可按需重新加载，不恢复旧执行。

本轮不新增 Agent 年龄、Work 总量、调用次数或历史长度 cap，不把物理卸载与从 handoff 排除绑定成同一个状态。Agent 是否进入 handoff 只由 §6.1 的当前／排队 Work 决定；闲置但未删除的 Agent 仍可按既有权限查询。

## 8. PMCA 预算与完整 TODO 解耦

### 8.1 唯一局部上限与计量口径

**3,200 tokens（十进制 3.2k）仅限制 PMCA，不限制完整 handoff。** PMCA 是 Process、Monitor、Cell、Agent 的状态条目；`todos` 和 `todo_counts` 完整保留，不占用该局部额度，不因内容含引号、Unicode 或转义而被裁剪。

使用当前 `PulsaraHeuristicTokenEstimatorV3`，定义一个无 I/O 的 PMCA 计量视图：从最终 handoff 正文移除 `todos` 与 `todo_counts` 两个字段，保留四类对象数组及本次实际输出的承接／超限说明，然后沿原 source encoder 和 adapter 编码成 ordered input item。这个视图的 V3 估值须 ≤3,200，包含 JSON、说明、runtime observation 字符串封装和原有 message framing。不得只计算未封装正文或逐行估值相加。

这个视图仅用于预算计算，不单独发送、不持久化、不新增 source 或对象 owner。实际发送的仍是一份含完整 TODO 的 handoff。最终完整消息的 V3 估值另外交原输入编译链处理，可以高于 3,200；不把 TODO 的 4,024-token 已知反例当成 TODO 最大值，也不另设 6,400 或 7,224 的隐含总上限。

复用现有 source encoder、adapter 的纯文本消息 lowering 与 `estimate_wire_json_component`；将计量能力通过既有 owner 传入纯投影器或复用公共无 I/O helper，不复制一套 token 公式，也不使纯 renderer 依赖具体 provider 客户端。当前两协议编码结果相同；验收仍分别经过 Chat／Responses serializer。若两协议表示成本不同，PMCA 视图使用两者较大值；完整消息分别按实际协议计入总输入预算。

取消 runtime handoff 专属的 32,768-byte 检查、字节预算配置及其参数／调用链；不新增同义 token 配置旋钮、KiB 兜底或旧字段 fallback。其他来源、传输与模型的现有物理边界不在本轮删除范围。

该局部上限不替代现有最终请求输入预算。完整 TODO、SYSTEM、tools、其他 source、历史与输出预留继续由原编译链处理。不能因 TODO 令完整 handoff 超过 3,200 就报局部超限；若整个请求超过真实输入预算，沿原明确资源失败／压缩规划处理，不静默截断 TODO。局部上限也不作为对象接纳／运行限制。

### 8.2 两段模型可见文字

**每份摘要始终附带以下承接说明：**

> This snapshot carries forward the work in progress before compaction. Continue that work using the existing targets.

含义是“这份快照承接压缩前正在进行的工作，请使用已有 target 继续推进”。重点是连续执行，不附加“状态可能已变化”的警告、不要求模型因压缩先重新核查全部对象，也不把这些 target 当成启动新副本的指令。

**仅当符合 §6 状态条件的对象因本节预算没有完整展示时，再附带以下一句：**

> Additional active or pending objects are omitted; when available, use query_processes, query_monitors, query_cells, or query_agents without a target to discover currently retained objects.

Subagent 使用同一说明但不列 `query_cells`，该实际文案同样参与预算测量。

PMCA 未超限时完全不输出这句，也不输出零遗漏提示、遗漏计数或额外查询说明。两段文字均计入 PMCA 的 3,200 上限。TODO 大小本身不触发超限句，也不挤占 PMCA 名额。

正文使用六个既有数组／计数字段，先渲染 canonical JSON，再以换行追加承接说明；有预算省略时再以换行追加超限说明，整体作为一个 source 的 text body。不是三个独立 source，也不新增摘要持久字段。

如果状态筛选后的 PMCA 候选全空，而且 TODO 没有任何条目（包括无快照或空清单），沿原路径返回 `None`／空 source，由 coordinator 使用既有 `EXPLICIT_EMPTY`，不单独追加承接句。ROOT／Subagent 均按该条件处理。

有 TODO 而 PMCA 全空时，仍生成含完整 TODO、空对象数组和承接句的 handoff；有 PMCA 而无 TODO 时同样生成。判断是否有 PMCA 以裁剪前的合格候选为准，不能因为最终裁剪为空而误报没有交接内容。承接句只随实际存在的 handoff 提供，具体状态仍由原 owner 与执行时检查负责，不借文字授予 liveness 或执行权限。

### 8.3 确定性装箱

1. 按 §6 的真实状态和 scope 筛选，冻结完整 TODO；先按 §8.2 判定是否需要生成 handoff。PMCA 计量视图只预留四类数组、原消息封装和承接说明，不预扣 TODO 预算。
2. 各对象类型按完整 canonical ID 稳定排序，以 Process、Monitor、Cell、Agent 为固定轮转次序，每轮从每个非空类型取一项；某类耗尽后跳过。该顺序是确定性的展示规则，不推断任务重要性，不先把全部 Cell 装满再考虑 Agent。
3. 每次试加一个完整条目，对整份编码后的 PMCA 计量视图重新计量。全部符合条件的候选均能装入时，保留它们，只输出承接说明。
4. 确认下一条符合条件的对象装不下时，标记本次存在预算省略，停止继续枚举，不为遗漏计数扫描后续全部 Agent 页。
5. 加入超限说明，按装入的逆序移除末尾完整对象条目，直到**含两段说明、但不含 TODO 字段**的 PMCA 视图不超过 3,200。不得把 target、状态行或 JSON 字符串从中间截断。
6. 将完整 `todos`／`todo_counts` 加入实际发送正文，原顺序和文字全部保留。FULL／COMPACT 使用相同完整正文；分别验证 PMCA 视图局部合规和完整实际消息的总输入成本，不再以 3,200 拒绝完整消息。

Agent 读取保留单页 50 的既有读取边界，按需取得下一页；第一页前的活跃筛选不能省略。一次采集开始后只操作已冻结／读取的事实，不为回退条目重新读取对象状态。采集期间对象可以按原生命周期变化，不新增库存快照或延寿机制。

单纯 PMCA 对象过多触发裁剪与一句查询指引。合法 TODO 即使独立超过 3,200，也必须完整保留且不触发 PMCA 裁剪或局部失败；移除“所有合法 TODO 都能装入 3,200”的保证。没有 TODO 的 PMCA envelope 和两段说明应能装入局部预算，最终整个请求仍须满足真实输入边界。

### 8.4 省略语义与权限

- 完整删除 runtime handoff 的 `omitted` 字段，包括其中 `agents`／`code_cells` 等所有分类 count；不输出空块、零值或改名后的替代字段，也不扩展 Process／Monitor 的省略计数。同步删除专为该块维护的计数初始化、递增／递减、序列化、说明和仅冻结旧形状的断言。
- 删除 Agent 数组中的 `{omitted_agents,next_cursor}` 特殊条目。摘要不发送遗漏数、候选总数或剩余页数；实际 query 工具自己的分页／结果合同继续独立工作。
- 摘要只在确有预算裁剪时输出 §8.2 的一句查询提示，不为生成精确 count 扫描全部库存，不维护新遗漏统计或关闭历史。
- 按状态主动排除的闲置对象不触发超限说明。必须有实际符合条件、但装不下的下一条对象，才能将裁剪称为预算省略。
- 来源读取错误不能伪装成超限、完整或空集合；沿原 typed 错误处理。尚未读完的其他页在已确认预算省略后无需继续读取。
- query 说明只承诺查询当前仍保留且可见的对象，不保证恢复冻结时的完整库存。`when available` 保留原权限边界，不要求自动退出 Plan 或提升权限。
- 本轮不在摘要附加第三段查询／Cell 使用指南；必要工具细节由既有工具说明与 Skill 提供。
- 这是单份上下文观察的有界投影，不限制执行、对象历史、Agent／Work 数量或 Cell 寿命。

## 9. Source、前缀与文档联动

handoff 仍是同 Host 的 untrusted runtime observation，复用现有 collector、coordinator 的冻结／候选编译／显式采用流程；不会因为状态变化或回收线程运行而重建已安装输入。

启用 §8 的 wire 形状时，同步修改 `context_sources.py` 与 compiler 的 source metadata 镜像，以及纯 renderer 的既有 projection 标识。沿现有 provider source 兼容边界使用一版新合同；不新建 fingerprint 字段、注册表，不保留 old/new 双输出或旧 payload fallback。历史已接受内容不重写。

保留 MUST_KEEP 和现有 FULL／COMPACT source 接口，两种 mode 使用同一份完整正文，其中仅 PMCA 视图受 3,200 上限约束。预算处理按 §8 联动，移除本 source 旧字节参数与检查；普通环境 Cell 索引及其专用字节装箱按 §6.6 删除，不再保留另一条自动 Cell 状态投影。编译成功、successor 被采用且实际发送后，才算新 source 对模型可见；仅 freezer 返回成功不是交付证明。

普通环境 source 的模型可见字段减少时，同步现有 collector／compiler 的合同元数据和说明，按现有 snapshot-on-change 后缀观察／合法 successor 路径提供新值，不回写既有历史、不修改已安装 SYSTEM／tools。移除所有专为该普通索引配置的回调和预算引用，同时保留用户控制所需索引获取方法。

SYSTEM 中删除关于 omitted task counts 的旧说法，改为准确的状态承接说明；本 source 的预算省略只以 §8.2 条件句表示，不再指示模型读取省略计数。只同步相关句子，不顺带重写 TODO 生命周期、Plan 流程或模型语义摘要提示。

## 10. 实施落点与分工矩阵

| 模块 | 主要文件／owner | 必须完成的修改 | 不得越界 |
|---|---|---|---|
| Monitor 回收 | `terminal_process/monitor.py` | CLOSED 删除条件、三处结算触发、安全迭代／晚到回调／关闭 | 不新增历史、确认协议或计时强删 |
| Monitor 旧通知确认 | `conversation_kernel/host.py` 的 Monitor 投递循环、既有 `safe_point.py` 确认路径 | 在活动 Work 门禁前确认已有 immutable attempt；UNKNOWN 结束本次尝试并保留，避免 close／continue 空转 | 不向新 Work 转发旧通知，不伪造 NONE，不增加确认协议 |
| Monitor 结果提示 | `conversation_kernel/tool_runtime.py`，既有 renderer | cancel not_found／query unavailable 的通用 message | 保持原外层分类、reason/outcome、权限 |
| Process 到期维护 | `terminal_process/manager.py` | 唯一维护线程、到期与受阻重查、release／force／shutdown | 不用执行超时杀活进程、不重写保留策略 |
| Process 读取协调 | 同上及 `terminal_process/output.py` 必要最小接线 | 精确 state 获取、读取保护与 prune 删除原子协调 | 复用现有计数，不建并行租约系统 |
| Cell handoff 事实 | `code_mode/host.py`，必要的现有 contracts | owner 根据完整 pending／terminal_delivered 筛选 | 不改 VM、记账、ack、stop、forget 合同 |
| Agent 活跃读取 | `_repository/subagents.py`、`subagents/collaboration_api.py` | ROOT 内先筛状态再分页；移除伪 Agent 分页条目 | 不改公共 query schema、两段退役或表结构 |
| handoff 组装 | `tool_runtime.py`、`compaction/runtime_handoff.py` | scope、目标形状、稳定轮转、PMCA 3,200-token 装箱；TODO 无损追加、空事实返回空 source | 不新增对象数量限制或遗漏计数，不以局部 cap 裁剪 TODO |
| Subagent compaction 接线 | 既有 compaction scope／coordinator 调用链 | 真实 session+Work turn 传 Monitor owner | 不从不存在的输出字段过滤，不扩大范围 |
| 输入 source 与说明 | `context_sources.py`、`model_input/compiler.py`、`ports/system_prompt.py` | 合同镜像及准确使用指导 | 原前缀边界不变，不扩权限 |
| 普通环境索引删除 | `context_sources.py`、Host collector 注入、相关说明与测试 | 删除模型环境 `cell_index`／省略字段及专用采集／筛选／预算链；保留其余环境信息 | 不删除压缩 `code_cells`，不删除用户查看／停止 Cell 的数据接口 |
| 用户控制保留 | `CodeModeHost.snapshot_cell_index()`、Host／gateway／proto／desktop bridge 现有路径 | 查看及精确停止继续使用原对象和索引数据 | 不改 UI 外部协议，不把 UI 列表反向注入模型 |
| 预算接线 | 现有 compaction contracts／policy、coordinator、source encoder 与 V3 estimator | 删除 handoff 专属字节参数／检查，传递并复用实际消息计量 | 不删除其他来源的物理边界，不复制估算器 |
| 测试／现行设计 | 相关 owner／Host／compaction 测试及本文引用设计 | 更新当前事实与行为证据 | 不靠删行为断言获得通过，不重写旧实验 |

## 11. 验收矩阵

优先用可控时钟、同步屏障与真实 owner 验证，避免真实等待一小时／三天或用短 sleep 猜竞态。只删除纯旧结构冻结测试；有语义的测试迁移到新 owner/API。

| 组 | 必须证明 |
|---|---|
| Monitor 普通关闭 | cancel／expiry／completion／Work close 后立即退出 query 和摘要；无剩余依赖时内部记录删除 |
| Monitor 注册竞争 | 注册结果结算前关闭，token 仍能 committed／discarded 正确退役；两种顺序最终无残留；冲突身份仍拒绝 |
| Monitor 安装竞争 | freeze 后取消、UNKNOWN 后 FULL／NONE／conflict；未决 attempt 不丢，明确结算后删除；晚到回调无复活 |
| Monitor Work 结束 | UNKNOWN 后 Work 结束／关闭：旧 attempt 仍可确认并最终结算删除；同一次投递尝试不反复选择同一 ID、不阻塞其他登记；不发布旧草稿到下一 Work |
| Monitor 并发关闭 | 同时关闭多登记、不修改正在遍历的字典；Host 取 scope／attempt 与清理竞争不丢既有结算 |
| Monitor 缺失提示 | 删除后 cancel 的 not_found 和 query unavailable 具有 message；错误 ID 仍拒绝；foreign Work/Host 不泄露存在性；不错误声称当前调用取消了进程 |
| Process 到期 | 没有新 exec_process 时，已结束记录也能到期移除；到期前可查询，仍活进程／子进程不被误杀 |
| Process 暂缓 | 观察 lease、inflight read、物理 join 未完成时保留；解除后无需新启动即可重试并删除；受阻期间无空转 |
| Process 原子性 | 读取先接纳则读取可完成且回收暂缓；回收先赢则新读不可用；metadata／poll／wait／订阅／release 同样受保护 |
| Process 线程关闭 | 普通 release、force、shutdown 都收束维护线程；共享 Subagent 关闭不误关；并发接纳与线程退出不重建多个 worker |
| Process 保留语义 | 原 TTL／32 条压力策略及输出 coverage 保持，没有新增执行寿命或逻辑总量限制 |
| Cell 状态筛选 | queued、running、终态未确认、未读输出、prepare-only pending 均选入；仅剩内部收尾可排除；筛选不 ack／不占 observation |
| Cell 原合同 | 终态无人领取仍保留；资源错误不撤销已发生效果；query/source/stop/输出失败重读及原正常回收继续成立 |
| Agent 状态筛选 | 前 50 个闲置、后续活动的 Agent 不漏；当前／排队 Work 与等待依赖均按真实派生值；闲置对象仍可 query |
| Agent 退役 | 原 1 小时卸载／3 天删除及 idle 安全门禁不变；不按摘要选择改变 idle 起点 |
| Scope | ROOT／Subagent Process 范围、Monitor 当前 Agent+Work 隔离；有其他 Work 的 Monitor 时 Subagent 压缩不 KeyError、不泄露 |
| TODO／Plan／权限 | 完整 TODO 顺序和 counts 保持；合法大量引号反例由真实 parser 接受且无损发送，完整消息可 >3,200；相同 PMCA 在有／无 TODO 时选择结果一致；Plan source 与写入拒绝保持；非 bypass 不因此获得 Agent／Cell query |
| 摘要／预算 | 两协议的无 TODO PMCA 计量视图均 ≤3,200 V3 tokens，含承接句／可选超限句和封装；实际完整正文另计总预算；边界恰为3,200、试加一项超限、因补句而回退多项、不同类型轮转均正确 |
| 摘要说明 | 每份实际 handoff 都有承接句；PMCA 全空且无 TODO 时为 None／EXPLICIT_EMPTY、不附承接句；仅 TODO 或仅 PMCA 时正常生成；能全装时无查询提示，TODO 大小不误报 PMCA 超限；所有情况下均无 omitted 字段、分类遗漏 count、替代统计字段或伪 Agent 分页条目 |
| 预算 hard cut | handoff 旧 KiB 配置、参数和重复检查全部删除，无别名／fallback；FULL／COMPACT 同文且同口径；整个请求预算检查仍生效 |
| 普通环境删除 | 普通输入和 successor 的环境 source 均无 Cell 列表／省略字段或 provider 回调；工作区／时区事实仍可见；普通 Cell 状态变化不再单独改变环境 source；压缩 handoff 仍提供筛选后的 CELL target |
| 用户 Cell 控制 | 用户 UI 仍能列出、查看和精确停止同一 Cell；gateway／proto／bridge 原协议不变；删除模型索引不删共享数据路径 |
| Prefix | 对实际采用的 successor 检查新状态 source；已有 epoch SYSTEM/tools 不变且消息只追加；对象执行没有因压缩重复启动 |
| Subtraction | 无新增表列、事件类别、registry、恢复链、指纹字段或对象总量cap；新维护线程有唯一物理 owner |

独立回收测试与相关终端／Code Mode／Agent／Plan／compaction 回归通过后，再验证模型可见行为。仅检查字符串或测试通过不能替代对实际权限、结算和关闭顺序的代码审阅。

## 12. 实际集成、实验与实施顺序

### 12.1 顺序

1. 完成 Monitor 旧通知优先确认、安全删除与通用提示，验证 Work 结束／UNKNOWN、注册／安装竞争及范围。
2. 完成 Process 到期维护及原子读取保护，验证无新启动时回收和全部关闭路径。
3. 准备 Agent／Cell／Monitor 的 handoff owner 事实与状态／scope 测试；删除普通模型环境的 Cell 索引，验证用户 UI 控制及其余环境事实保留。
4. 接入 §8 的 PMCA 3,200-token 装箱、独立完整 TODO、空 source 条件与两段说明，更新 source metadata 和 V3 计量，删除旧字节预算链路。
5. 完成真实 Host 压缩集成、受影响回归、代码审阅及代表性真实 provider 验证，再标记实施完成。

### 12.2 真实 Host 场景

至少构造同一 ROOT 下的运行／结束 Process、未关闭／待结算关闭 Monitor、活动／未领取终态／仅收尾 Cell、活动／闲置 Agent 和完整 TODO。触发 mid-turn compaction 并采用 successor，检查**实际发给模型**的 source 与 §6 状态及 §8 最终预算合同一致。

另外验证 Subagent 当前 Work 压缩，包含 ROOT 和其他 Work 的 Monitor，证明范围正确且不会因错误字段读取失败。读取源码／元数据不消费输出；既有 wait 可继续取得原结果；原对象不因压缩重新执行。Plan 权限及上下文依原专门 source 保持。

验证普通输入中只有环境事实、没有 Cell 索引，而压缩 handoff 仍能提供 CELL target；用户 UI 仍可查看／停止该对象。另覆盖 PMCA／TODO 全空、有 TODO 无 PMCA，以及合法 TODO 令完整 source 超过 3,200 的情况。后者不得裁剪 TODO、挤掉相同 PMCA 或触发局部 token 错误。

### 12.3 代表性 provider 验证

沿用户既有实验偏好，用保存配置中的 DeepSeek `deepseek-flash`，主模型 reasoning effort `max`，通过两种 OpenAI API 协议各完成一个聚焦场景：读取压缩后的活动对象状态，使用既有 query 查当前仍保留的对象，并正确理解 Monitor 的 unavailable/not_found 提示。允许模型正常查询、纠错，不设零误调用门槛，也不重跑全部旧工具组合。

独立 `context_compaction_summary` 沿用所属请求的完整 binding／reasoning 与合法准备得到的目标；本场景与主模型共同使用所选 `max`，并分别记录实际调用。手动 idle 压缩使用操作接纳时的 session 选择，配置与输入安全点遵守 [压缩配置实施规格](PULSARA_COMPACTION_MODEL_CONFIGURATION_AND_MANUAL_IDLE_ONLY_IMPLEMENTATION_SPEC.zh.md)。

模型连接、凭据与 PostgreSQL 按 `LocalSettingsStore`／`require_pulsara_home()` 及现行生产注入 owner 读取；隔离时复用只读设置注入和已核实的本地可丢弃数据库，不修改保存设置，不导出秘密。真实提示、响应、参数、业务结果、原失败与修正轮保留，仅精确移除实际凭据。

报告区分接口接受、模型参数、实际业务效果与 source 交付；没有采用 successor 不算验证压缩后输入。记录实际 usage 与前缀，不由摘要行数下降推断 token 改善。若必要配置或资源不足，明确未验证范围，不能标为全部通过。

## 13. 完成条件与验收结果

Monitor／Process 可按各自合同独立完成和验收。Cell 不加新 cap、Agent 两段退役保留、状态筛选谓词与权限范围均已明确。

PMCA 预算为 3,200 个 V3 启发式 tokens，含 PMCA 视图的封装和说明；TODO 与 counts 始终完整保留且不受该局部 cap 限制。PMCA 全空且无 TODO 时为空 source；实际存在的 handoff 承接压缩前工作，仅 PMCA 预算省略时补一句查询提示。

Monitor 在 Work 结束后仍先确认旧 immutable attempt 再安全删除，UNKNOWN 不空转；普通模型环境 Cell 索引删除，压缩 CELL target 和用户 UI 查看／停止接口保留。上述合同连同权限、原前缀边界、Process 回收及完整输入预算均已完成要求验收，证据与范围见 [总交付报告](output/runtime-handoff-retirement-20261030/REPORT.zh.md)。

验收结果：

- [真实 Host 集成](output/runtime-handoff-retirement-20261030/INTEGRATION.zh.md)：ROOT 混合对象／合法大 TODO、仅 TODO、全空、Plan及 Subagent 当前真实 Work，双协议共10项通过；实际采用 successor并发送serializer source，完整输入／PMCA分别计量，原对象与前缀连续。
- [真实 provider](output/runtime-handoff-retirement-20261030/provider/REPORT.zh.md)：两协议33次HTTP200、27对同epoch精确前缀，PMCA各283；实际查询与连续执行通过，实际模型参数及原harness和transport失败保留在报告中。
- Monitor／Process／Agent／Cell owner、普通环境和受影响回归分别见总报告链接；ROOT亲审发现的读取／关闭竞态、callback误杀和本地新轮启动失败reservation泄漏均已在原owner边界修复并验证。没有新增schema表列或durable oracle类别。
- 独占本地测试／实验数据库与fixture资源已清理，未知／remote PostgreSQL在创建前拒绝；统一33个修改Python文件ruff及全工作树diff检查通过。

这些是本规格定义的完成证据，不扩张为全仓、所有provider或所有配置均已验证。
