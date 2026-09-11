# Pulsara PR03：精确运行控制、后台命令与任务侧栏 Hard Cut 实施规格

状态：**IMPLEMENTED，REACTIVATION PENDING — 2026-09-11 reviewer 连续指出的 kernel 时序缺口均已按单一路径修正，kernel 全量重验通过；本轮按用户要求不改前端，尚未重跑第 20 节前端、isolated wheel 与真实 provider/browser 门槛。**

规格修订：2026-09-11。实施起始代码核对基线：`f6f52f5c`。本文沿用原文件路径，原地升级为 PR03 的唯一实施规格，不另保留一份竞争性的产品契约。Git 管理代码身份，不记录文件/文档 SHA。

关联索引：[浏览器 dogfood 复盘与修复方案](PULSARA_BROWSER_DOGFOOD_REVIEW_AND_FIX_PLAN_2026-09-08.zh.md)。本 PR 闭合 F06/M01，并包含为用户控制必需的进程终态仲裁、人工反馈、任务页替换与新增后台终端页。F04/M02/M04、能力管理、全会话暂停/恢复不在范围内。

授权与最终范围：

- 允许增加一个来源真实的 committed 控制反馈事件 `UserControlFeedbackAccepted`，以及本文第 18 节列明的最小 typed 类别；不是对任意事件、表、关系、receipt 或 durable queue 的概括授权。
- Demo **只能**参考右侧「任务」（完整替换其生产页，含组图/节点详情）、「后台终端」（新增 tab），以及 `create_agent_tasks` 原始结果区域的视觉样式。其他 Demo 区域一律不参考，含能力页、主对话布局、创建工具半详细摘要/跳转、左栏、设置和输入框。第 16–17 节是精确白名单。
- 保留 production permission、path containment、Hook、effect settlement、cancellation、provider-prefix continuity；现有 cold epoch/合法 compaction successor 以外不建立 rebase 边界。
- 第 14、18 节已将旧稿待决项定案。实施者不得保留兼容 fallback，也不得在未修订本规格时自行增加 durability、权限或总量限制。第 20 节全部满足后才能改为 ACTIVATED。

修订背景：2026-09-10 的讨论确定了 exact-target、monitor-first、活动/idle 分流、反馈三轴、物理终态仲裁；2026-09-11 确认配色和任务/后台终端视觉范围。同日完成 Phase A–D、clean-v0 hard cut、全量验证及真实 provider/browser activation。后续 reviewer 复核先后发现正常 ROOT 结束协调、未知提交确认、异步观察乱序、Host closing 接纳、子任务取消终态、物理终止 disposition，以及 watchdog 全分支截止、completion fence 后接纳窗口、compaction successor 观察、closing 已保存观察合并等 kernel 时序缺口；本文已收紧相应合同，原 activation 证据保留为历史，但在新合同完成全部第 20 节重验前不继续宣称 ACTIVATED。

## 1. 最新结论

用户不必理解 `terminal`、`terminal_process`、`terminal_monitor` 三个模型工具的分工。

主要交互只有三个：

1. **停止本轮运行**：中断用户点击时所见的确切 ROOT turn 的生成和后续执行；已启动操作按各自规则取消或完成收尾，不是只停止文字流。
2. **取消子任务**：取消用户选中的确切 task，由 kernel 传播依赖影响。
3. **终止后台命令**：关闭该进程关联的后续监视，并终止该进程组；一个用户动作，分别保留实际结果。

后台区域按 **process** 展示，不按 monitor 展示。没有 monitor 的后台进程同样可见、可管理；不为了显示而注册监视。

右侧侧栏按第 17 节：能力保持生产实现，任务采用 Demo 的组列表和节点图，新增折叠式后台终端列表。主对话仅统一原始结果的艺术风格，不采用 Demo 的创建工具摘要/跳转；Demo 白名单之外一律不参考。

每个 process 保留 0 或 1 个未关闭 monitor，独立覆盖完成、输出进度和 heartbeat 的组合条件；本次不开放同进程多 monitor。保留记录不承诺永久存在，后台列表只表示当前 Host 的实际保留范围。

用户终止后台命令后的反馈分为两种：

- Host 接纳控制时主模型正在工作：绑定当时的活动 ROOT。若 ROOT 将正常结束，复用其现有 completion 边界协调尚在推进的控制结果，在现有 watchdog 内形成反馈候选、于下一合法 safe point 接纳，并在后续模型请求精确纳入后才正常结束。用户 STOP、异常、owner 丢失或 watchdog 到期保留明确退出路径；命令可来自同一 session 的较早轮次，前提是仍由当前 Host 实际持有。
- Host 接纳控制时主模型空闲：UI-only；不专为本次控制接纳反馈 entry，不额外启动模型轮次，也不自动带入下一次人工轮次。后来出现活动 ROOT 不改变这一选择。

这不阻止其他后台活动的正常通知，也不撤回已经安装的观察或已经启动的轮次。查看、控制、向模型提供事实、启动新轮次是不同边界。

三个用户控制入口都必须区分“请求已发出”“Host 已接纳”和“操作已完成”。Host 接管后及时返回接纳状态，不让请求通道等待耗时收尾；结果已确定时可以直接返回终态。通过原 command 查询本次控制结果，与读取目标当前状态分别表达，详见第 12.1–12.5 节。

人工控制反馈不是操作完成的同义词，也不是一条从“待通知”到“模型已读”的线性状态机。采用第 8.6 节的三条独立维度，按第 8.7 节的精确请求证据显示进度；物理操作成功不掩盖反馈失败，反馈无法确认也不推翻已知物理结果。

## 2. 早期方案哪些仍然成立，哪些已过时

下表同时整理旧建议与后续讨论澄清的理解；并非每项都是早期方案原文曾作出的承诺。

| 早期表述 | 最新判断 | 本文替代合同 |
| --- | --- | --- |
| 后端分别拥有 turn、task、process、monitor 的控制能力 | 保留 | 不建立通用 Activity 执行体系；UI 可以统一组织 |
| ROOT 停止入口称为“停止本轮回复” | 名称容易被理解成只停止文字生成 | 统一为“停止本轮运行”；保留 ROOT cancellation 与已启动效果结算，不新增仅停文字流的路径 |
| 等待 ROOT/物理操作结束后一次返回，或把请求发出当作接纳 | 不足以支撑耗时控制与断线确认 | Host 真正接管后快速返回；保留原 command 的接纳/结果查询，快速完成可直接返回终态 |
| 前端提供四种动作，monitor 取消放进进程详情 | 已被后续简化取代 | 第一版三个主要入口；不向用户单独暴露 monitor 管理 |
| 终止命令由“取消关联 monitor + 终止 process”组成 | 保留并明确 | 人工组合操作，不是假装原子事务；不存在 monitor 时仍能终止进程 |
| 关闭 monitor 就不再告诉模型 | 过于笼统，应替换 | 停止持续监视，但活动模型需要一次真实的用户控制反馈 |
| 用 TerminalObservation/USER_STEER 携带人工控制，或绝对禁止类别增长 | 会歪曲来源 | 复用存储与接纳 machinery；按第 18 节增加已授权的最小 USER_CONTROL_FEEDBACK 类别和唯一 accepted event |
| 以 sequence 上界或 provider install 证明“模型已收到”，将反馈串成单一状态链 | 证据不足，维度混杂 | 独立表达 canonical 接纳、请求纳入、owner 可查询性；请求纳入与 transport 发起分别证明 |
| 终止后不再自动唤醒 | 必须限定 | 只针对此次控制反馈和已关闭监视未来的投递；不是全会话静默 |
| idle 反馈是否自动带入下一人工轮次继续待定 | 第一版已收敛 | UI-only，不专门接纳反馈 entry、不补投；不删除既有历史或阻止主动状态查询 |
| 命令来自旧轮次，就不能影响当前 ROOT 的输入 | 不成立 | 同 session、当前 Host 持有的命令绑定接纳控制时的活动 ROOT；正常结束须经过受限 completion 协调，异常/STOP/owner 丢失或 watchdog 到期可退出，且永不晚到改投 |
| 进度、heartbeat、完成分别需要一个 monitor | 不需要 | 一个 monitor 可组合条件；每个 process 保留 0/1 个未关闭 registration |
| 只展示 monitor | 已纠正 | 展示转入后台的命令，process 是身份，monitor 是可选附属能力 |
| 有 process_id 就表示仍在运行 | 不成立 | ID 是身份；可操作性取决于当前 owner 和实际生命周期 |
| terminal 的等待时间是命令运行期限 | 不成立 | `yield_time_ms` 是首次工具调用等待窗口，不是命令超时 |
| 注册 monitor 后主循环退出等待 | 不成立 | 注册按普通工具返回，模型随后可以继续工作，也可以正常结束当前轮次 |
| 运行中的程序不能接收输入 | 不成立 | 当前支持 stdin；只读界面是第一版产品选择 |
| 前端可以还原一个完整终端屏幕 | 当前数据不支持 | 第一版只读实时日志，不能还原已被过滤的 ANSI/OSC 或光标操作 |
| 子任务、进程日志、权限与实际终态都由现有 owner 支撑 | 保留 | 展示不新增执行权，视图状态不成为 canonical truth |
| 普通停止隐含全会话暂停，直到下一人工输入 | 未采纳 | 不引入全局暂停/恢复；若以后需要，另定 Host admission 合同 |

## 3. 当前代码真源与复用边界

以下路径从仓库根目录起算，行号仅供定位；Git 管理代码身份，不增加源码或文档 SHA。

| 真源 | 已有资产 | 本次设计不能假装已有的部分 |
| --- | --- | --- |
| `src/pulsara_agent/ports/terminal.py` | 三种工具的闭合 schema、参数和描述 | 人工后台命令组合控制 API |
| `conversation_kernel/runner.py:1319/1471/1543`，位于 `src/pulsara_agent/` 下 | 普通工具批次后继续采样；无工具回复按现有规则结算 | monitor 注册并无自动退出循环指令 |
| `src/pulsara_agent/conversation_kernel/host.py:4066` | ROOT cancellation intent、task join 和 settlement | 点击时 exact turn 校验；当前入口仍停止处理时的 active task |
| `src/pulsara_agent/terminal_protocol/v3_gateway.py:638` | controller gate、command envelope | 当前 STOP 仍拒绝 target_turn_id，等待 ROOT 结束后返回，且不把 STOP command_id 交给 Host 保存；无任务取消/进程组合终止入口 |
| `src/pulsara_agent/conversation_kernel/host.py:3841`；`terminal_protocol/v3_gateway.py:972`，后者位于 `src/pulsara_agent/` 下 | 现有 command 查询及结果响应 | 尚无 exact STOP 的接纳/完成查询；不能根据目标已结束反推该次 STOP 已成功 |
| `src/pulsara_agent/web_app/protocol_client.py:135`；`terminal_protocol/v3_gateway.py:194`，后者位于 `src/pulsara_agent/` 下 | 同一 attachment 上的串行请求/响应 | 长时间等待 STOP 会占住 controller 通道；async await 本身不等于阻塞整个事件循环 |
| `src/pulsara_agent/conversation_kernel/subagent.py:2233` | 排队、待依赖、活动任务取消，已终态返回 | 可供用户入口和模型工具共享的显式 typed cancellation 方法 |
| `src/pulsara_agent/conversation_kernel/_repository/subagents.py:2239` | 依赖失败传播与真实状态结算 | 前端不得另算一套执行级联 |
| `src/pulsara_agent/conversation_kernel/host.py:5428`；`web_app/session_controller.py:198` | session-scoped 任务分页及依赖读取 | 具体呈现方式不成为新后端权威 |
| `src/pulsara_agent/terminal_process/manager.py:759/770/805` | process kill/list/log、owner fence、进程组存活判断与物理 join | 用户控制适配；现有 `kill()` 无条件写入 `killed`，不能保持已确认终态，必须先修正共享终止仲裁；不能直接开放任意系统 PID |
| `src/pulsara_agent/terminal_process/models.py:48/112` | process origin、io_mode、physical_state、输出 cursor | 现有 to_payload 未完整暴露所有侧栏需要的字段 |
| `src/pulsara_agent/terminal_process/manager.py:112/280/320/861` | process-local foreground decision、yield 与清理 owner | abort/异常清理也会设置 yield_decision=True，不能直接将其当作 background_adopted；需闭合成功后台交付的唯一真源 |
| `src/pulsara_agent/terminal_process/manager.py:127/988/1011/1431` | finished 保留参数、整条记录 prune、输出容量裁剪与物理/读取约束 | 总输出容量裁剪不等于删除 process；记录彻底 prune 后不能凭空恢复逐项状态 |
| `src/pulsara_agent/terminal_process/monitor.py:43/228/319/488` | Host 容量、同 process 单未关闭 registration、关闭后 in-flight 确认 | 单进程控制只需可选 monitor；0 个未关闭 registration 不证明所有旧观察都已结算 |
| `src/pulsara_agent/terminal_protocol/v3_gateway.py:281/583/985/1071` | 已认证的 session/Host attachment、读取入口与 controller 控制门槛 | 需接入受同一授权边界约束的 retained log 读取；process ID 和 sanitizer 均不代替授权 |
| `src/pulsara_agent/terminal_process/output.py:156/358` | 输出处理、保留范围、cursor、gap、订阅 | 没有未过滤的完整终端屏幕流 |
| `src/pulsara_agent/terminal_process/monitor.py:423/488/794` | cancel、冻结投递的保留边界、关闭 live 状态 | 关闭 live 状态不等于模型收到一次取消反馈 |
| `src/pulsara_agent/conversation_kernel/host.py:3584` | 活动轮次 ExistingTurnInstallation、空闲 NewTurnInstallation | 不能直接把普通 monitor 唤醒策略用于人工取消反馈 |
| `src/pulsara_agent/conversation_kernel/safe_point.py:218` | provider-safe 安装、现有确认/结算边界 | closed monitor 不会凭空生成新的用户控制观察 |
| `src/pulsara_agent/ports/terminal_observation.py:53`；`conversation_kernel/reader.py:2394`，后者位于 `src/pulsara_agent/` 下 | typed terminal observation 与 canonical input origin | observation 强制 monitor/输出覆盖语义；ROOT 的 USER_STEER 被解释为 HUMAN_STEER，均不是一般人工控制反馈 |
| `src/pulsara_agent/conversation_kernel/contracts.py:51`；同目录 `vocabulary.py:32`；`src/pulsara_agent/model_input/contracts.py:77` | 闭合 EntryKind、committed event 与 canonical input origin | 真实人工控制来源须跨这些语义边界保持一致；不能只改 entry 名称却冒用 TerminalObservationAccepted |
| `src/pulsara_agent/conversation_kernel/_repository/conversation.py:488`；`conversation_kernel/reader.py:727`，后者位于 `src/pulsara_agent/` 下 | provider input sequence cut 与按 floor/scope/task 过滤的读取 | through_sequence 只是上界，不能证明某条反馈实际进入最终请求 |
| `src/pulsara_agent/conversation_kernel/provider_dispatch.py:2938`；同目录 `direct_model.py:434/468` | exact dispatch preflight/CAS、之后的 transport open | install 不打开 transport；调用 open_stream 也不证明 provider 服务端收讫、模型已读或已理解 |

后续实现应复用这些资产，不复制进程管理器、依赖调度器、输出缓冲或 canonical writer。

## 4. 三种模型工具保持现有职责

### 4.1 terminal：启动一条命令

- 新建命令执行，按 `yield_time_ms` 先等待一段时间。
- 首次返回仍为 running 时，把 process_id 交给模型；命令继续运行。
- 返回已结束/未启动成功时，模型直接处理结果，无需为了展示或结束而再注册 monitor。
- terminal_session_id 选择工作目录上下文，不是 process_id，也不承诺持续存在的交互 shell。
- max_output_chars 限制本次返回，不限制命令总输出或运行期限。

### 4.2 terminal_process：一次主动查看或控制

保留 list、poll、log、wait、write、submit、close_stdin、kill。

- poll/log 是一次读取；wait 是一次有界等待，返回后不会自动安排下次通知。
- write/submit 支持运行中 stdin；close_stdin 不等于 kill。
- kill 仍由现有 owner 终止受管理进程组，不回滚已发生效果；模型 kill 与人工终止必须共享第 7.1 节修正后的物理终止核心，不能保留一条会改写已确认终态的旧 kill 路径。
- process_id/cursor 只能在其真实 owner/stream 范围使用，不能跨 Host 重建或被拿来操作任意 PID。

### 4.3 terminal_monitor：持续观察并安排后续交付

- register 快速返回，模型可以同时继续其他工作。
- 未指定进度条件时不周期性发送进度；正常关注完成，但仍可能到期或按现有策略关闭。
- 有活动 ROOT 时，普通观察可进入现有轮次；idle 时普通 monitor 观察可按现有规则接纳新 ROOT。
- 当前只允许 ROOT 使用，不是子任务通用定时器，也不是跨 Host 重启的 durable scheduler。
- cancel 停止未来监视，不终止进程；已冻结或已提交的投递继续遵守原结算边界。

**模型工具的 cancel/kill 保留这些既有含义。** 人工“终止后台命令”是显式组合操作，不依据调用者身份偷偷改变同名 kill 的行为。模型自己调用工具时已取得工具结果，不再平白追加一条重复的人工控制反馈。

### 4.4 注册 monitor 不等于等待

正常路径可以是：

```text
terminal 返回 running → register 返回 REGISTERED
→ 模型继续读文件/分析/调用其他工具
→ 观察到达后在合法边界并入现有轮次
```

也可以是：

```text
terminal 返回 running → register 返回 REGISTERED
→ 模型给出一条普通回复，本轮正常结束
→ 之后 monitor 观察触发被允许的新轮次
```

不是 monitor 强行结束 runner，也不是模型在一个空转循环里持续等待。

### 4.5 每个 process 的 monitor 基数固定为 0 或 1

第一版保留 coordinator 的现有约束：同一 process 最多一个未关闭 registration，包含 DORMANT 和 ACTIVE。单进程控制结果使用可选 monitor 及其真实处理结果，不泛化为任意多监视列表；模型工具的 `terminal_monitor.list` 仍可列出不同 process 的监视。现有 Host 总容量 8 及其他已验证预算不随本次产品接入调整。

一个 monitor 已可同时配置输出阈值、静默期、进度间隔和 heartbeat，并在有效期内关注完成。它们按既有优先级及草稿合并规则产生观察，不是三个必须独立投递的通知通道；取消、到期或既有预算关闭后不承诺继续报告。`terminal_process.poll/log/wait` 补充主动查看与等待，无需第二个 monitor，也不替代 monitor 自己的游标。

关闭只消除该 registration 的未来草稿和订阅，不能丢弃它已冻结的 in-flight。旧 CLOSED registration 的结算与当前可选 monitor 分开处理，不能依据“当前 monitor 为空”断言没有未结算观察；不为简化 public DTO 删除原确认 owner。本次也不新增 monitor update、同进程多订阅合并或新的通知预算体系。

## 5. 三个用户入口

| 入口 | 精确目标 | 主要结果 | 不包含 |
| --- | --- | --- | --- |
| 停止本轮运行 | 点击时所见 ROOT turn | 中断主助手本轮生成和后续执行，保留已启动操作的取消或收尾 | 停止其他 turn、清队列、自动取消子任务或已转入后台的命令、回滚已发生效果 |
| 取消子任务 | 选中 task_id | 取消该任务；由 kernel 传播依赖失败 | 暂停/恢复、自动重试、默认杀关联进程 |
| 终止后台命令 | 选中 owner 下的 process_id | 取消关联监视并终止进程组，报告两项结果 | 回滚、停止主轮次、关闭整个会话、全局禁止自动接续 |

前端不提供独立 monitor 列表或取消按钮；monitor_id 仅用于后端精确关联，必要时可出现在诊断材料中。用户无需理解工具分工。

关闭面板、折叠日志、切换视图只改变展示，不等于上述任何控制动作。

## 6. 后台命令列表：process 是主体，monitor 是附属能力

### 6.1 哪些命令进入列表

第一版主要展示 terminal 首次等待结束后仍运行、已转交后台 owner 的命令。这是“首次工具返回 running + process_id”的产品含义，不是扫描本机所有进程。

- 等待窗口内已完成的短命令留在对话工具记录，不要求再进入后台列表。
- 已转入后台的命令，即使没有 monitor，仍应显示。
- 子任务启动并转入后台的命令也显示，注明实际 task origin。
- 该命令随后完成/被终止，在 owner 仍保留记录期间原条目更新终态；不因不再 running 就由 UI 主动删除。真实 prune 后允许从列表消失，详见第 6.3 节。
- 初连时命令已完成但后台记录仍保留，也可以恢复对应终态条目；不能只依赖页面曾经接收过 running 响应。

后端内部在创建进程时就有 ID，所以“存在 process_id”本身不是后台资格，也不是存活证明。后台资格采用第 6.4 节的只读 `background_adopted` 投影，必须来自成功交付事实，不能将内部 yield_decision 原样改名，更不能新增 durable background job。

### 6.2 不以 monitor inventory 替代 process inventory

monitor 未注册、被取消或到期，都不应使仍在运行的进程从用户视野消失。终止组合中的 monitor 成功/process 失败也必须保留该进程。

展示不注册 monitor，不产生模型调用，不改变 tool permission，不扩大文件 seen observation。

### 6.3 信息与生命周期

展示命令、cwd、来源 ROOT/task、实际状态、退出码/终止原因和可用输出。可附加“结束后将通知模型”等人类可读说明，不要求用户选择 monitor。

进程记录与输出内容的保留分别由既有 owner 管理：

- 当前 ProcessRegistry 默认 finished TTL 为 3600 秒、finished 数量参数为 32；记录 prune 还要求满足物理结算、订阅和在途读取等条件。这是现有清理策略，不承诺准时删除，也不保证任何瞬间都严格不超过 32 条；本稿不改变这些参数或新增总量 cap。
- 总输出容量策略裁剪 retained output，优先从已结束进程开始，必要时也裁剪运行中进程的输出。它不等于删除整条 process 记录，必须继续表达 cursor/gap、可用范围和读取截断。
- retained 期间，读取返回真实进程状态与可用输出；原控制结果另按第 12 节的 command 保留/查询边界取得。整条进程记录被 prune 后，刷新列表允许不再出现它。面板统一提示“仅显示当前 Host 保留范围，较早记录可能已过期”，不要求后台为每个已删除 ID 保留不可用条目。
- 用户已打开的详情在精确读取中确认资源不存在时，可以就地显示“记录已不可用”；普通分页未加载、网络断开或部分查询缺页不证明 prune，不能据此删除已知记录或宣布资源不存在。
- 不新增 durable/process-local 永久 tombstone、全历史索引或恢复记录，不能为了保证条目一直可见而长期保活已无需读取的资源。进程记录的 prune 与第 12.4 节的控制 attempt 退役是不同边界，不能互相当作操作结果证明。
- Host 丢失时把当前控制能力置为不可用，不从旧工具 JSON 复活 process handle。已获得的事实可按现有投影保留，但不承诺跨重启恢复保留日志。
- 已有 canonical tool result/artifact 可继续按原权限读取；其独立保留不随本次 UI 改变，历史输出也不证明进程仍存活。
- 不扩大 scope 到 MCP 服务、任意系统进程或远程任务。

### 6.4 后台只读字段与成功交付真源

受限读取 DTO 可以补齐现有 TerminalProcessInfo 的 origin、stream、io_mode、physical_state、输出 cursor 等事实，并返回只读 `background_adopted`。按实际展示/查询需要暴露字段，不直接泄露可用于跨 owner 操作的执行对象，也不为了面板机械扩增模型工具结果。

`background_adopted` 表达“该命令已经成功交付后台管理”，不是“存在进程”“当前仍运行”或“模型已读到 process_id”。它在 retained 记录自然结束后仍可为 true；false 也不能当作进程物理上不存在的证明。

当前代码尚不能将 `yield_decision` 直接用作此字段：foreground decision abort 与异常清理路径会设置 `yield_decision=True`，随后终止/隔离进程，并不代表成功后台交付。第 18.4 节固定在原 foreground decision owner 的成功交付点设置该事实，覆盖正常返回、取消、异常与结算；不让前端猜测或另建平行注册表。

从首次前台执行、成功交付后台，到结束后保留期间，身份与资格必须保持一致；reload 读取同一 owner 的真源，不解析工具正文、live preview 或历史 running 文案。取消/失败清理中尚未物理结束的资源继续由原 owner 负责收尾，不能为了展示而冒充成功后台交付。

## 7. 终止后台命令的组合合同

### 7.1 执行边界

1. 验证 controller、当前 session/Host owner 和确切 process 身份。
2. 在 Host 接纳本次控制时固定原 command、owner、process 和模型反馈目标，遵守第 8 节及第 12.2 节；真正接管操作后即可返回接纳状态，不等待下列耗时步骤。
3. 从当前 coordinator 查询该 process 的可选未关闭 monitor，包含待工具结算激活的 DORMANT；没有关联项也是正常情况。
4. 关闭该关联监视，阻止它产生新的观察草稿和未来完成唤醒；保留已有冻结投递的原确认与结算，不把 monitor-first 当成撤回事务。
5. 通过修正后的 process owner 终止进程组并取得实际物理结果；不得直接沿用当前会改写已确认终态的 `kill()` 行为。
6. 分别提供实际进程结果、监视结果和反馈交付状态，支持原 command 查询；UI 据此更新，不把第 2 步的接纳响应当作操作完成。

这不是由浏览器先发 cancel 再发 kill 的两个独立调用。组合顺序由 Host 持有，不能因页面关闭就遗失已启动的物理操作。

已结束的进程应返回其现有真实终态，不能只因晚到的点击改写成 killed。若仍有属于它的未投递监视，清理结果单独表达，不伪造进程终止。

当前缺陷不是理论窗口：进程自然 `exit 0` 或 `exit 7`，且 reader/watcher 与 `physical_completion` 均已结算后，再调用现有 `kill()`，完成回调和已经 finalized 的输出 owner 仍分别保留 `success / 0`、`error / 7`，但 kill 返回及后续 list/log 会变成 `killed / 0`、`killed / 7`。因此本功能验收前必须修正物理 owner，不能只在用户控制入口外包一层预检查。

#### 7.1.1 ProcessRegistry 终止仲裁硬约束

`ProcessRegistry` 是终止意图接纳与进程最终状态的唯一仲裁者：

- 本文的“已确认终态”是指唯一最终 `status/exit_code` 已被冻结，并由 output finalize、完成通知及后续 process 读取共同引用；仅有 shell `poll()` 返回、进程组消失或进入 `TERMINALIZING` 都不单独构成已确认终态。
- 已经确认的终态不可被晚到的显式 kill、timeout、foreground-decision abort、启动回滚或 owner cleanup 改写。
- “原子”是指在 Pulsara 进程内线性化终止请求的接纳、终止意图和最终原因的冻结；不是持有 `state.lock` 完成信号发送、进程组等待或 reader/watcher join。
- 存活判断继续以完整进程组为边界。shell leader 已退出但同组子进程仍存活时，终止请求仍可被接纳；不能只检查 `Popen.poll()`。
- watcher 完成发布、显式 kill、timeout、foreground-decision abort、启动回滚和 owner cleanup 必须共享同一套终态仲裁不变量。它们可以保留各自上层语义和物理等待策略，但不能各自无条件改写 `killed/timed_out` 后再计算终态。
- 接纳终止意图后必须释放完成路径需要的锁，再发送信号并等待现有物理边界。Python 锁只能决定 Pulsara 内部哪个事实先胜出，不能证明操作系统进程完全因为该信号退出。
- 不新增平行的进程生命周期状态机；结果继续携带现有 `status`、`exit_code` 和 `physical_state`。底层终止 disposition 最少区分 `ALREADY_TERMINAL`、`TERMINATION_COMPLETED`、`PHYSICAL_SETTLEMENT_INCOMPLETE`。`ALREADY_TERMINAL` 表示本次调用既没有接纳新的终止意图，也没有在仍存活的物理边界上发起信号，且协调后已有真实终态可返回；包含既有 success、error、timeout 或 killed，而不只指自然成功。自然退出、reader/watcher 尚在收尾的窗口，即使本次调用帮助完成 join，也不能据此改称 `TERMINATION_COMPLETED`。
- `TERMINATION_COMPLETED` 证明本次调用在可终止状态接纳了新意图或对当时仍存活的进程组发起了实际终止尝试，且此后 owner 确认了物理结算；并不声称最终原因或退出严格由本次信号独占导致。`PHYSICAL_SETTLEMENT_INCOMPLETE` 可能表示进程组仍存活，也可能表示进程组已消失但输出或 watcher 尚未完成，不能统一写成 `STILL_RUNNING`。
- “本次控制来自用户”由 Host 组合操作保存，不下沉成 `ProcessRegistry` 的用户专用终态。模型 kill、用户终止、timeout 与 owner cleanup 共享物理核心，但其调用来源和上层反馈仍然不同。
- owner 下不存在该资源、旧 Host 已失效或记录已被清理时返回受限的 `OWNER_UNAVAILABLE`/resource unavailable 结果；observer、伪造 attachment 或其他授权失败继续走既有授权边界，不能与资源不可用混为一谈，也不能泄露其他 owner 下的 process 是否存在。

终止仲裁时只在短临界区冻结内部事实，物理等待必须在锁外完成。尤其不得持有 watcher 完成所需的 `state.lock` 等待 watcher 本身，否则会把正确性修复变成死锁。

### 7.2 部分结果

| 实际结果 | 显示与反馈 |
| --- | --- |
| monitor 已关，终止请求完成且进程已物理结算 | 显示终止操作已完成、进程已结束；保留实际输出/退出信息，不承诺信号是退出的唯一原因 |
| 无 monitor，终止请求完成且进程已物理结算 | 显示终止操作已完成；不把“没有监视”当失败 |
| monitor 已关，物理结算仍未完成 | 显示 `PHYSICAL_SETTLEMENT_INCOMPLETE` 及现有 `physical_state`；区分进程组仍存活与仅输出/线程尚未收尾，不丢条目、不偷偷恢复监视 |
| monitor 已关，终止尝试已确定失败且确认进程组仍存活 | 持续显示“进程仍在运行 · 通知已关闭”，保留可用日志；原尝试已结束且目标仍允许控制时提供“重试终止” |
| monitor 已关，进程组已结束但物理收尾未完成 | 显示“进程已结束 · 收尾未完成”，继续查询原收尾状态，不误导用户重复 kill |
| monitor 已关，但进程实际状态无法确认 | 显示“终止结果待确认 · 通知已关闭”，优先查询原操作/目标；不能推定仍在运行或已经结束 |
| 进程此前已进入任一真实终态 | 返回 `ALREADY_TERMINAL`，保留原 status/exit code；分别说明监视是否关闭，不改写为本次用户终止 |
| 有监视投递已经冻结或提交 | 不承诺撤回；继续原确认/结算，并说明仍有既有投递 |
| 网络结果未知 | UI 显示待确认，查询原目标；不猜成功、不自动作用于新目标 |

不得将已提交内容从 provider prefix 或 canonical 历史中删除。也不得为达成“全停”反向撤销已经开始的合法工具效果。

部分失败必须体现在对应命令的持续状态与可用操作上，不得只发一个 toast 后隐藏。通知已关闭仅指该 monitor 的未来通知；已有冻结/提交投递单独说明，不承诺“再也不会出现任何通知”。保留日志遵循第 6.3 节的真实范围，后续资源 prune 不要求新增墓碑。没有已知失败和当前可控制目标时，不用“重试终止”代替结果查询；Host 不可用时禁用物理控制。

### 7.3 原操作查询与人工新尝试

两种情形必须分开：

- **网络 ACK 未知**：沿用原 command_id、原 session/Host owner 与 exact target，只读查询原 attempt；不自动重发控制，即使打算使用相同 command_id 也不能以重发代替确认。保留期内意外重复提交的合并规则仍按第 12.3 节执行。
- **已知失败后的人工“重试终止”**：用户主动发起一项新操作，使用新 command_id。重新核对当前可用 owner 和原确切 process、当前权限及物理状态；新尝试的反馈目标按此次 Host 接纳时重新固定，不沿用上次已结束的 ROOT，也不自动替换 process 或重启命令。

原尝试仍在执行、结果未知或仍由物理 owner 收尾时，默认继续查询，不将其伪装成已知失败来制造重试。已完成的原结果保持可核查，不被新尝试覆盖；不为重试建立 durable 链、父子操作关系或永久去重集合。

对于新发起的控制，确认进程已终态且没有新的控制效果时，只返回原真实终态，不重复发送信号或制造模型反馈；本次反馈维度为 NOT_REQUIRED + NOT_APPLICABLE，并明确无新效果的原因。如果进程早已结束，但本次首次关闭仍有效的 monitor，则这是新的监视控制事实：单独报告实际变化，按第 8 节的活动/idle 规则处理一次反馈，不得再次声称“用户终止了进程”。重复查询或再次无变化的控制不得再生成反馈。

查询/重复提交已有 command 时，仍按第 12.3 节返回原 attempt 的结果与反馈事实；不能因为进程如今已终态，就把原成功控制动态改成 no-op，或把原 ACCEPTED/INCLUDED 改为 NOT_REQUIRED。原记录缺失则如实不可确认，不重建结果。

owner 被替换时返回不可用，不能重新解析旧 ID 去操作新进程。查询不得重新执行控制；原结果查询不到不授权生成新 command 或改换目标自动重试，也不得建立全历史确认注册表或 durable receipt。

## 8. 活动模型与 idle：最新反馈合同

### 8.1 四条信息流不能混在一起

1. **UI 状态更新**：用户随时能看到命令终止/失败、监视关闭。
2. **现有工具调用结算**：poll/wait/kill 等已开始调用仍返回真实结果，不吞掉 tool result。
3. **人工控制反馈**：向已绑定活动 ROOT 的合法输入边界提供用户干预及实际结果，按三轴事实报告是否接纳/纳入，不承诺模型必定收到。
4. **自动创建新 ROOT**：本次人工终止不默认获得这一权力。

因此“不额外唤醒”不是“不告诉模型”，更不是“让模型停止回答”。

### 8.2 状态判定与交付

判定点固定为 Host 接纳本次控制的时刻，不是前端 isRunning 的猜测。反馈绑定与进程 origin 分开：同一 session、由当前 Host 实际持有的后台命令，即使命令来自较早 ROOT 或其子任务，也可向接纳时的活动 ROOT A 提供真实控制反馈。不能仅因 session_id 相同就跨越已替换 Host 的资源 owner，也不复活命令的来源轮次。

| Host 接纳操作时 | 处理方式 |
| --- | --- |
| 有活动 ROOT A，无论命令来自本轮还是同 session 较早轮次 | 绑定 A；结果就绪后，在 A 的下一个合法输入接纳点接纳一次真实反馈；A 的正常结束受下述 completion 协调约束 |
| A 的 provider 请求正在流式生成 | 不修改在途请求、不强制中断；等下一允许接纳位置 |
| A 正在执行工具 | 保留整个工具结算边界；不把反馈插进请求与结果配对之间 |
| A 已冻结 compaction successor | 不重写 successor；遵守现有普通接纳边界 |
| idle，无活动 ROOT | UI-only；本次控制不创建反馈 entry 或 NewTurnInstallation，不自动带入下一人工轮次；结果就绪时即使已有 B 也不改投 |
| 原目标 A 在接纳反馈前已结束/关闭入口 | 不改投 B，不复活 A，不自动新建轮次；按真实接纳结果标注原因，提交仍待确认时不得直接判失败 |
| 反馈已 canonical accepted，但 A 在请求纳入前结束 | 保留该历史 entry；确证未纳入时显示 TARGET_ENDED_BEFORE_INCLUSION，不改投其他轮次 |
| 其他 monitor/人工输入启动了另一个轮次 | 不把它误归因于本次用户终止，也不扩大取消范围 |

绑定后不因 process kill 花了较长时间，就重新选择那时的“当前模型”投递。反馈是否写入历史、是否编入 A 的最终请求、该请求是否发起，分别按第 8.6–8.7 节判断；UI toast、live close event 和 sequence 上界均不能代替这些证据。不能笼统显示“模型已收到”。

活动 ROOT 的**正常结束**采用现有 ROOT completion 边界，不由模型轮询或另建调度器碰运气：

1. 一条无工具回复准备正常结束 A 时，Host 必须在与后台控制接纳相同的锁边界 seal A 的反馈入口，再读取绑定 attempts。若控制接纳先赢，它已绑定 A 并进入本次协调；若 completion seal 先赢，随后到达的终止操作仍执行，但不得再把反馈绑定给正在结算的 A，反馈为 NOT_REQUIRED + NOT_APPLICABLE(TARGET_CLOSED)。assistant settlement 未完成 A 时释放 seal，继续允许后续控制绑定；确认完成时 seal 保留到 Host 清除或切换该 exact ROOT slot。不得用一次 pending 快照代替该线性化边界。
2. A 仍绑定 canonical PENDING/ACCEPTED + inclusion PENDING 时，使用该 attempt 首次取得的现有 `FOREGROUND_CANONICAL` watchdog。deadline 检查覆盖所有协调分支，包括 candidate 已形成的 PENDING 和已经 ACCEPTED + inclusion PENDING；到期即标记本 attempt 的正常结束协调 exhausted，不得因状态已经 ACCEPTED 而无限制造后续请求，也不得另加调用次数上限。
3. candidate 已形成时，将当前助手回复按现有结算规则提交但保持 A 打开；在下一次 provider preparation 前，由同一 Host owner 等待该 candidate 在合法 safe point 得到 ACCEPTED/确定失败/UNKNOWN。若进入 completion fence 时已经是 ACCEPTED + inclusion PENDING，必须立即保持 A 打开并创建后续请求，不能等待一个尚无请求生产者的 inclusion 事件直至 watchdog。正常成功路径必须先接纳反馈，随后编译一个包含该 exact entry 的后续请求。
4. 后续请求的最终 install observation 必须按第 8.7/18.3 节证明 INCLUDED，ROOT 才可沿正常无工具回复结束。观察点覆盖 runner 普通安装和 compaction coordinator 已预装 `provider_open` 的合法 successor；不能因为 successor 不是由 runner 当场安装就跳过观察。
5. provider-input 与 transport 观察回调先于 Host 的 canonical ACK 或 inclusion 状态更新到达时，按 exact candidate/request identity 暂存在该 process-local attempt 中并随后合并，不能静默丢弃。Host closing 后对未知提交 exact-confirm 成功时也必须先合并已经保存的精确观察；owner 不可用只把确实未知的维度标为 UNKNOWN，不得抹掉已确认的 ACCEPTED、INCLUDED 或 transport invocation。
6. 同一 watchdog 到期、用户主动 STOP、ROOT 异常、Host closing/owner 丢失是明确退出路径。尚未开始写入且已无入口时为 FAILED(TARGET_CLOSED)；已经发起但提交结果未知的 immutable candidate 必须继续只读 exact-confirm，证实 winner 则保留 ACCEPTED，无法确认才为 UNKNOWN。它们都不改投 B、不唤醒 idle、不产生跨重启补投。

这条协调只保证**正常结束边界内、watchdog 允许期间**的后续请求纳入；不把物理操作变成无限 turn lifetime，也不声称 provider 服务端已读。idle 仍保持 UI-only，不因本规则增加 candidate、entry 或下一人工轮次补投。

用户应能区分命令来源 ROOT/task 与本次反馈目标 ROOT/idle；结果返回 Host 实际冻结的目标，不能把 origin 当作接收者。本版不增加反馈目标确认弹窗、预期接收轮次参数或轮次选择器：点击前的活动 ROOT 只是预览，UI 不承诺向该预览轮次投递；接纳后展示 Host 实际冻结的反馈目标，具体接入见第 14、18 节。这里的反馈绑定不同于 STOP 的点击时 exact target，不能互相替代。

### 8.3 反馈内容

至少表达控制来源为用户、确切 process、命令来源 ROOT/task、本次绑定的反馈目标、实际进程结果、可选监视处理结果，以及“其他工作未被此操作停止”。使用 typed 来源和实际值，不伪造用户键入了一段话或模型自己调用了某工具。旧轮次命令的结果只是当前执行环境变化的事实，不是恢复旧任务或要求当前模型接手旧任务的指令。

人类可读投影示例；确定的 typed 内容及 wire 结构见第 14、18 节：

```text
用户请求终止后台命令 P；该命令来自较早轮次 O，本次反馈绑定当前轮次 A。
进程：终止尝试已完成，进程已结束。
关联监视：已取消，不会再产生新的监视通知。
本轮及其他工作未被停止。请根据该事实继续当前任务。
```

失败示例必须按事实改为“监视已取消，但进程仍在运行”“进程已结束，收尾未完成”或“无法确认终止结果”，不能为了简化文案说成功。进程原本已终态、只首次关闭 monitor 时，只陈述该监视变化，不伪造新的进程终止结果。

反馈不要求模型专门输出一句确认，也不强迫模型重启命令、重建 monitor 或重试任务。模型应根据当前用户目标决定后续工作，不能把终止当成重跑授权。

`terminal_process` / `terminal_monitor` 的模型可见说明必须同步冻结以下使用规则：后台命令可能被用户在模型工具调用之外终止；monitor 列表为空只说明当前没有活动 monitor，不能证明 process 已结束；用户询问当前状态或下一步依赖该进程时，应以已知 exact process_id 主动查询 `terminal_process`。收到用户控制反馈后可继续其他独立工作，但不得把它当成重启命令的授权。注册 monitor 后应继续独立工作；确无其他工作时可以正常结束本轮，由 monitor 按原合同在以后恢复对话，而不是为了等待反复 poll。

### 8.4 idle UI-only 与已接纳历史的保留边界

反馈一旦 canonical accepted，就是已提交的历史事实。ROOT 随后结束只改变“是否纳入绑定轮次的请求”，不能删除 entry、撤销 accepted occurrence、偷偷改投其他轮次或自动唤醒新 ROOT。已确认的 ACCEPTED/INCLUDED 也不能因为 owner 后来丢失而倒退。

后续普通历史读取或合法 compaction 可以按既有规则处理这条已接纳 entry；这不等于将本次控制的反馈目标从 A 改成 B，也不证明 A 当时收到过反馈。摘要提到了类似事实，不足以证明原 entry 曾精确纳入 A 的请求。

第一版明确选择 **idle UI-only，不自动带入下一次人工轮次**。Host 接纳控制时没有活动 ROOT，就不专为此次控制创建 USER_CONTROL_FEEDBACK entry、accepted occurrence、待投递记录或新的 ROOT/provider call；仅通过 UI/现有 owner 更新和查询实际控制、进程与监视结果。结果就绪后新出现的人工轮次不能成为补投目标，未来的 provider 编译也不能扫描 UI 控制记录偷偷注入这条反馈。

这一选择不删除既有工具结果、不撤回已冻结 monitor 观察、不屏蔽后续模型主动通过 terminal_process 查询进程状态；它禁止的是本次人工反馈的额外接纳、自动补投与唤醒，不是抹去已发生事实。

对绑定过活动 ROOT、但因入口关闭而确定未接纳的反馈，同样不增加另一条持久待投递路径。已 accepted 的 entry 则继续按本节开头的历史规则保留，两者不能混淆。本稿不建立跨轮次或跨重启的 durable delivery 队列。若以后需要自动带入或可靠补投，必须另行修订产品边界；不是本版尚待选择的实现选项。

### 8.5 真实来源与最小语义扩展

monitor 的 `_close_locked` 会清理 mutable draft/successor 并发出 TERMINAL_MONITOR_CLOSED；这是监视状态变化，不是 provider-visible 人工控制反馈。现有来源不能无条件复用：

- `TerminalObservationContentV1` 强制要求 monitor_id、process_id、observation kind 和输出覆盖字段；kind 只有 PROGRESS、HEARTBEAT、COMPLETION、EXPIRY。人工控制可能没有 monitor，也可能终止失败，不能伪造 monitor/coverage，或冒充 COMPLETION/EXPIRY。不得让 closed monitor 再注册一次来“发最后一句”。
- `USER_STEER` 在 ROOT reader 中明确映射为 `HUMAN_STEER`，不是通用控制反馈。不能把控制结果伪装成用户键入的新指令，也不能伪造模型 tool call/tool result。
- `TerminalObservationAccepted` 是终端观察的 committed 来源。即使新增了正确的 entry 名称，也不能借用这个事件来提交人工控制事实。

复用现有 transcript、safe point、compiler、资源接纳、provider-input 与事件事务。本 PR **确定新增**最小的 `USER_CONTROL_FEEDBACK` EntryKind、同名 canonical input origin 和唯一的 `UserControlFeedbackAccepted` committed event，范围限于人工后台命令控制的真实结果；具体字段、唯一 writer、事务、未知提交确认与 oracle 增量固定在第 18.1–18.3 节。

不新增控制反馈表、关系、subject、append guard、durable delivery queue 或进度事件。内部 provider item 复用现有 USER 与真实的 input origin，provider wire 仍为 user role；不是用户手输指令，不触发 HUMAN_STEER 的专属路径。

接纳只面向已绑定活动 ROOT；不采用 NewTurnInstallation fallback。仅追加允许的消息 suffix，不改变现有 SYSTEM/tools、在途请求或已冻结 compaction successor。无法通过既有边界实施时必须报告冲突，不能先加例外再补文档。

### 8.6 三条独立反馈维度

以下是产品层的闭合状态词汇，供后续 typed DTO 投影；不是新增数据库状态机，也不是六个阶段首尾相接的投递流水线。它们描述**同一次人工控制、同一条反馈和同一原目标**，与第 12 节的控制请求接纳及物理结果分开。

#### 8.6.1 canonical 接纳

| 状态 | 精确含义 |
| --- | --- |
| `NOT_REQUIRED` | 按政策无需模型反馈，例如接纳控制时 idle，或已终态且没有新的控制效果；附真实原因，不代表控制未执行，也不能代替错误 |
| `PENDING` | 原 owner 正在接纳或确认该条反馈；不保证数据库尚未提交 |
| `ACCEPTED` | 已确认 canonical 事务提交，并可关联确切 entry_id；不是 UI toast 或 live event |
| `FAILED` | 已确定未接纳，且本次接纳不再推进；附 typed reason，例如确认入口已关闭且没有提交，不能仅凭异常推定 |
| `UNKNOWN` | 无法确定是否提交，且目前不能完成精确确认；不把可能已经写入的事实判成 FAILED |

数据库响应丢失、超时或连接异常本身不证明回滚。原 owner 仍在负责确认时可保持 PENDING；确认能力已丢失而结论不明时为 UNKNOWN。FAILED 表达接纳的确定结果，不一定是产品故障：因原轮次结束而拒绝接纳应显示具体原因，不统一渲染为“系统错误”。

#### 8.6.2 请求纳入

| 状态 | 精确含义 |
| --- | --- |
| `NOT_APPLICABLE` | 不要求反馈，或 canonical 已确定未接纳、没有可纳入的 entry；不是编译失败的兜底 |
| `PENDING` | 已绑定原目标，仍由 owner 推进 canonical 接纳、合法输入边界或最终请求选择 |
| `INCLUDED` | 精确反馈已进入绑定轮次最终选定的 compiled request，并关联既有 request/execution 身份；不代表 transport 已发起 |
| `TARGET_ENDED_BEFORE_INCLUSION` | 反馈已接纳，但有证据确认原目标在请求纳入前结束；不是“模型未读”的推测 |
| `FAILED` | 反馈已接纳，但本次请求纳入已确定失败且不再推进，附编译、资源接纳等真实原因；不能无限保留 PENDING |
| `UNKNOWN` | 证据不足，无法确定反馈是否已纳入原目标请求；不能仅凭 owner 丢失或目标结束判为未纳入 |

INCLUDED 只认最终选定请求。测量、预编译、被放弃的候选、被替代的 cut 或摘要中的相似内容均不足以证明。候选编译暂时失败但既有 owner 仍在推进合法替代候选时，不能提前把本次纳入判为 FAILED；也不为这一状态增加新的无限重试策略。

请求已经 INCLUDED 后，transport 发起失败、流式失败或 ROOT 结束都不反向改成纳入 FAILED/未纳入；这些属于对应请求的执行结果。

#### 8.6.3 原 owner 可查询性

| 状态 | 精确含义 |
| --- | --- |
| `AVAILABLE` | 原 Host owner 仍可按现有授权路径提供查询；不保证某条已退役操作记录仍保留 |
| `UNAVAILABLE` | 已确认原 owner 丢失、关闭或被替换，不能再依赖其 process-local attempt 查询 |

这是原 owner 的查询能力，不是网络连接状态，也不是前两轴是否为真的判定。暂时断线时通过既有连接状态显示“结果待确认”；若保留最后确认的 AVAILABLE，必须标明其为最后已知状态，不能装作刚完成存活确认。不得单凭网络错误置为 UNAVAILABLE；也不为这条轴新增 owner registry、generation 或独立存活协议。

原 owner 可用但操作记录已退役时，按第 12.3 节报告原结果不可确认，不伪装 owner 丢失。相反，owner 已不可用而 canonical ACCEPTED/请求 INCLUDED 已被证实时，保留这些已知事实，不能一律覆盖成 UNKNOWN。

#### 8.6.4 组合约束与未知结果

三轴独立不等于任意组合都合法：

- canonical 为 NOT_REQUIRED/FAILED 时，请求纳入为 NOT_APPLICABLE；canonical PENDING 时，请求纳入可以 PENDING，但不能提前 INCLUDED。
- INCLUDED 必须关联已确认 ACCEPTED 的精确 entry。确认过程若获得了足以证明该 entry 已纳入的证据，也须同步反映已证实的 canonical 接纳，不能保留“canonical UNKNOWN、请求 INCLUDED”的矛盾投影。
- TARGET_ENDED_BEFORE_INCLUSION 必须有已接纳 entry 和原目标未纳入的证据。目标结束发生在提交之前且确定没有提交时，为 FAILED（入口关闭原因）+ NOT_APPLICABLE；提交未知时先处理确认，不硬套这两种终态。
- FAILED 是有依据的终局失败，UNKNOWN 是知识不足。失败原因按所处边界归属：canonical 拒绝不冒充请求编译失败；已接纳后的纳入失败不抹去历史。
- 已知 ACCEPTED/INCLUDED 不因后续 owner 不可用、窗口退役或请求失败而降级；仍未确认的部分可以 UNKNOWN。PENDING 必须有明确 owner 正在推进或确认，不能在已失去执行/确认能力后无限显示“处理中”。暂时断线可展示最后已知 PENDING，但须同时标注待确认。
- 三轴变化不单独追加 durable event，不创建投递 receipt、确认表或反馈重放任务。复用第 12 节的局部 attempt 与既有 canonical/request 身份；重复查询只读取，不能重复接纳或触发新的 provider call。

“不降级已知事实”不承诺无限保留 process-local 证据。已有投影仍掌握确定结果时，不因另一次查询不可用而抹去它；页面重载、owner 丢失或记录退役后，新查询若没有该证据，可以如实 UNKNOWN。能够从既有 canonical 读取确认的接纳事实仍可确认，但不能借此推断原请求纳入，更不能为恢复这条轴新增持久证明。

### 8.7 请求纳入证据与 transport 边界

当前代码的证据边界明确分开：

1. `prepare_provider_input_cut()` 取得当时的 sequence 上界；reader 随后按 history floor、conversation scope、subagent identity 等筛选。`entry.sequence <= provider_input_through_sequence` 不能单独证明该 entry 进入输入。
2. 编译/选择阶段必须精确关联原 session、ROOT、适用的 context binding/合法 compaction lineage、反馈 entry 和最终选定的请求。复用既有 typed identity、provenance 和实际选定内容；不能按相似正文、当前 turn 或 sequence 上界猜纳入，也不新增内容 hash 或全历史 membership registry。
3. `install_provider_open()` 只完成 exact dispatch 的 preflight/CAS，其合同明确为 “without opening transport”。仅有 install 成功不能显示请求已经发起。
4. 实际 transport-open 边界在更后的 `open_once()` 中调用 `transport.open_stream()`。进度投影必须来自对应执行 owner 的真实调用事实；即使跨过该边界，也没有 provider 服务端收讫、模型已读或已理解的证明。

UI 可使用的准确表述：

| 已确认事实 | 允许表述 | 不能推导 |
| --- | --- | --- |
| canonical 事务接纳反馈 | 已记录控制反馈 | 已编入请求、已发起请求 |
| 最终选定请求精确包含反馈 | 已编入本轮请求 | transport 已发起、provider 已收讫 |
| 对应请求跨过 transport-open 边界 | 本轮请求已发起 | 模型已收到、已读、已理解 |
| 已纳入，但目标在 transport open 前结束 | 已编入本轮请求；请求未发起 | 改成 TARGET_ENDED_BEFORE_INCLUSION 或删除历史 |
| 只知已纳入，无法确认是否发起 | 已编入本轮请求；请求发起状态待确认 | 凭 install、超时或缺少回复猜发起结果 |

“请求已发起”只表示本地 transport 调用边界，不承诺字节已到达 provider。若该调用失败，仍须展示对应请求的实际失败/未知结果，不以曾发起掩盖失败。

请求发起进度投影已有 execution 生命周期，不再建立第四套持久 delivery 状态机，也不新增投递 ack/event。按第 18.3 节在 execution owner 的真实边界补充最小 process-local 观察，读取与丢失语义使用该节固定合同，不能让前端推测。

### 8.8 典型反馈组合

下表中的 owner 状态指查询时已确认的原 owner 能力；普通网络断线另按第 8.6.3 节表达。

| 场景 | canonical 接纳 | 请求纳入 | owner | 用户应理解的结果 |
| --- | --- | --- | --- | --- |
| idle 时终止完成，原 owner 可用 | NOT_REQUIRED | NOT_APPLICABLE | AVAILABLE | UI-only，不创建本次反馈 entry，不额外唤醒或自动带入下一人工轮次 |
| 绑定 A，接纳仍由 owner 推进 | PENDING | PENDING | AVAILABLE | 控制实际结果另报；反馈尚在接纳/确认 |
| A 入口关闭，确认反馈从未提交 | FAILED | NOT_APPLICABLE | AVAILABLE | 原轮次已结束，反馈未记录；不改投 B |
| 已记录，A 在纳入前结束且证据明确 | ACCEPTED | TARGET_ENDED_BEFORE_INCLUSION | AVAILABLE | 历史保留；未编入原轮次请求 |
| 已记录，但纳入发生确定的终局编译失败 | ACCEPTED | FAILED | AVAILABLE | 历史保留；说明纳入失败原因，物理结果不受影响 |
| 已编入最终请求，A 在 transport open 前结束 | ACCEPTED | INCLUDED | AVAILABLE | 已编入但请求未发起，不显示模型已读 |
| 提交结果无法确认，原 owner 已丢失且无纳入证据 | UNKNOWN | UNKNOWN | UNAVAILABLE | 控制/历史中已有事实照常保留；未知部分不猜、不补投 |
| 接纳已证实，但纳入证据随原 owner 丢失 | ACCEPTED | UNKNOWN | UNAVAILABLE | 已记录历史；无法确定是否编入原轮次请求 |
| 接纳与纳入均已证实，之后原 owner 丢失 | ACCEPTED | INCLUDED | UNAVAILABLE | 保留已知事实；无法查询的后续执行进度明确为未知 |

这些组合不是要求记录所有中间步骤，也不要求 UI 同时显示三排枚举。默认显示实际操作结果，反馈详情再解释记录、请求纳入及可查询性，避免把用户控制面板变成调度器控制台。

## 9. 子任务依赖读取与取消语义

### 9.1 保留任务身份、真实依赖与分页边界

- 任务身份为 task_id，dependency edge 表达“依赖谁的结果”。
- ROOT/task origin、batch grouping、创建顺序与 dependency edge 分开，不能解释成同一种关系。
- 复用任务分页与 dependency rows。当前 control 窗口不等于全部历史，不得静默遗漏页外任务或依赖并称完整。
- 未加载的依赖明确表示尚未加载，可按现有分页/后续只读查询补齐。
- 本次只提供受限读取和精确取消，不授予前端改写依赖、调度状态或自行执行取消级联的能力；组列表、依赖图与节点对话的呈现按第 17 节，读取接入仍需在实施规格闭合。

### 9.2 取消选中的任务

共享 KernelSubagentManager 的现有取消和结算流程：排队/待依赖任务进入 CANCELLED；活动任务中断并完成原效果结算；已终态任务返回实际状态。

前置失败导致等待中的下游进入 BLOCKED_DEPENDENCY_FAILED，由原 repository frontier 处理。UI 展示原因，不自行遍历后端并发送一串取消请求。

取消不是暂停。初版没有恢复旧任务、自动重试或重新接纳依赖按钮；后续若加重试，应明确它是新的任务还是原任务新执行，不能只改状态标签。

### 9.3 关联进程不默认级联终止

origin 能证明谁启动命令，不能证明只有该任务使用命令。取消子任务不默认杀其已转入后台的命令，不因依赖边传播而杀其他任务进程。

任务详情可列关联后台命令，并让用户分别终止。暂不增加“取消子任务及全部关联进程”的一键动作；如以后增加，需要另定影响预览和组合结果，不能复用普通取消名称偷偷扩大范围。

有实际下游影响或关联进程时，确认界面提示真实事实；没有额外影响时不要求每次弹窗。预览是读取结果，不是锁住世界的承诺，执行后仍以 kernel 实际状态为准。

### 9.4 完成反馈沿用原资产

现有子任务异步完成合同已包含取消、失败、依赖失败等 terminal outcome。复用现有 ROOT completion inbox 和接纳路径，不为用户取消另造重复结果。

子任务的 queue-only completion 与 terminal monitor 的 idle 自动唤醒是不同产品机制，不能因 UI 都在“运行面板”就统一改调度。

参考：[子代理异步完成与 Late-Join 契约](PULSARA_SUBAGENT_ASYNC_COMPLETION_HARD_CUT_RESEARCH_AND_IMPLEMENTATION_SPEC.zh.md)。

## 10. 停止本轮运行：文案澄清与独立的 F06 hard cut

### 10.1 P1 只澄清名称与取消范围

ROOT 停止入口的按钮名称、无障碍标签和相关帮助统一使用 **“停止本轮运行”**。不使用“停止本轮回复”或“停止生成”暗示只停止文字流，也不使用“停止全部”暗示会话内所有活动都会结束。“本轮运行”指主助手的这一轮执行，不是某个子任务，也不是整个会话的所有工作。

推荐 tooltip：

> 停止主助手本轮生成和后续执行。已启动的操作会按各自规则取消或完成收尾，不回滚已发生的修改。子任务、排队输入和已转入后台的命令不会因此自动取消。

现有 `HostSession.stop_current_turn()` 取消 ROOT 执行任务并等待收尾，符合本文保留的执行范围；原先的歧义来自“回复”一词，不应据此把 kernel 改成只关闭文本流或只取消一次 provider 请求。前端当前 `workbench-view.tsx:1845` 的无障碍标签已是“停止当前运行”，后续统一文案时改为上述名称，不表示本次已经修改前端。

本条 P1 不新增停止 API、取消模式或并行执行路径，不改变 provider prefix、permission、Hook 与效果结算合同。若以后确实需要“文字停止但模型继续执行”，应另行定义产品需求，不能作为这次文案修正的隐含行为。

### 10.2 已启动操作的收尾不能被文案抹去

复用原 root task cancellation、started effect settlement 和物理 owner。停止 ROOT 不自动清空人工输入队列、不取消独立子任务、不关闭已有 monitor，也不批量终止已转入后台的命令。

“已启动操作按各自规则取消或收尾”有以下具体含义：

- 主助手收到取消后，不继续本轮正常采样和后续工作；但这不是所有物理操作立刻停止的保证。
- 已经开始的文件写入等操作可能完成，真实工具结果仍须保存。停止期间出现收尾结果，不代表恢复了模型循环，也不能为了表现“停得快”丢弃结果或回滚修改。
- `terminal` 尚在首次前台等待/交付决策中时，取消可触发该次 foreground decision abort 并终止其进程。这不等于批量终止此前已经交付后台 owner 的命令。
- `terminal_process.wait` 等调用被取消，不等于其目标进程收到 kill；当前已启动的物理等待也不保证立即返回，仍遵守原调用和结算边界。

代码依据：`src/pulsara_agent/conversation_kernel/io.py:155` 保留被取消调用的物理结果；`tool_runtime.py:3026` 为首次 terminal 调用接入 foreground-decision abort；`tool_execution.py:1556` 在已知结果结算后继续传播取消。后两者同样位于 `src/pulsara_agent/conversation_kernel/`。不能把这些不同边界概括成“立即中断所有正在等待的工具”。

“停止 ROOT 后永久安静，直到用户恢复”不在本稿范围。其他已有输入或有效后台源仍可按各自合同继续；UI 应说明还有其他工作，而不是显示“全部已停止”。

### 10.3 F06 精确目标 hard cut

用户点击“停止本轮运行”时冻结所见 turn_id；没有目标时不发送无目标 STOP。

协议入口要求明确 target_turn_id，Host 验证 exact active owner 后才安装 cancellation cause。A 已结束、B 已开始时，旧请求不能取消 B；不保留无目标外部 fallback。

这是对原 STOP 路径的目标绑定修正，继续复用原取消和结算机制，不是新增一种“只停文字”的控制能力。

第 12.1–12.5 节的快速接纳、精确结果查询与 owner 校验已和 F06 同批落实；它没有更换 ROOT cancellation/settlement 机制。

## 11. 只读实时日志与一致的显示语义

### 11.1 日志而非完整终端模拟

当前输出 owner 保留经处理的 UTF-8，过滤 ANSI/OSC，并规范化回车等字符。第一版不能重建已丢失的光标、屏幕刷新或全屏应用状态。

可以用终端风格展示命令输出，但不把它称为完整 PTY 屏幕。也不为此引入 raw 逃逸序列/密钥泄露路径。文件正文的 exact-byte 校验和终端既有 sanitizer 是不同内容合同，不能混同。

用户不提供 stdin，不是因为进程不能输入，而是为保持这版“查看与终止”的产品范围。模型工具的 write/submit/close_stdin 不受影响。

### 11.2 读取行为

- 只读 API 复用 process owner 的 log/cursor/subscription，不能经模型重新执行 terminal_process 或注册 monitor 来刷新页面。
- 跟随最新输出时可自动滚动；用户翻看旧内容时不强制拉回。
- 复制对象明确为当前可用输出/选定范围；不把截断片段标成完整。
- 保留 gap、invalid cursor、owner lost、unavailable 与进程 exit 状态的区别。
- 页面切换/折叠使旧读取回调失效，但不停止后端工作。
- 复用既有单次读取边界和保留策略；不新增任务/历史/运行寿命总量 cap 来迁就前端展示。

### 11.3 控制反馈

按所见对象分别使用“正在停止”“正在取消”“正在终止”；后端确认后显示真实终态。已自然结束不改成已取消；未知不当成功；不因操作失败而删除对象。资源之后被 owner 真正 prune 时按第 6.3 节处理，不把持续反馈要求变成永久条目保留。

“停止本轮运行”的反馈文案遵循以下边界：点击并发出请求后显示“正在停止”；有后端事实表明仍在收尾时显示“正在停止，等待操作收尾”；后端确认本轮因用户停止而结束后显示“本轮已停止”。目标抢先自然结束时保留原完成结果；网络等原因导致结果未知时显示“停止结果待确认”。不把请求已发出当成已停止，也不在收到已完成结算的结果后仍提示“正在停止”。这些是后续 UX 实现的文案要求，不授权为提示语另建 durable 状态或新增事件。

点击后的即时进行中提示只是本地请求状态，不能据此标注“Host 已接纳”。接纳响应也不能触发完成 toast、删除目标或释放正在收尾的后端资源。断线后的查询与不可用提示按第 12.3 节处理；原先已经确认的目标终态和部分结果不因 owner 后来丢失而被抹去。

每个异步结果绑定原 session、connection owner 和 exact target；success/error/finally 均检查，不能让 A 的迟到操作重连或更新 B。

物理操作、反馈记录、请求纳入与 UI 状态分开表达。例如，有确切证据时可显示“进程已结束；反馈已记录，但原轮次在请求纳入前结束”，不算进程终止失败。没有纳入证据时显示“反馈请求纳入状态待确认”，不能笼统断言“未交付”。

反馈详情遵循第 8.6–8.7 节：已记录、已编入本轮请求和本轮请求已发起是不同事实；禁止“模型已收到／已读／已理解”的无依据提示。owner 丢失只关闭当前查询/控制能力，不擦除已确认的操作结果、canonical 接纳或请求纳入事实。

第 7.2 节的部分失败文案须持续可见，并保留当前可用日志和合法后续操作；toast 只能补充，不能代替状态。查询原 command 与人工新 command 的“重试终止”按第 7.3 节区分，不能因用户关闭提示或页面重连而自动重试。

### 11.4 observer 的 retained log 读取权限

第一版明确授予：**已通过现有 session 读取授权的 observer，可以读取该 session 当前 Host 实际持有进程的 retained log，而不只限于 transcript preview。** 这是终端日志读取范围的明确产品选择，不代表扩大其他受限内容入口的权限。

- 复用既有认证 attachment、session/Host 绑定和角色校验；每次读取验证当前 attachment、目标归属及 cursor/stream owner，不能由裸 process_id、terminal_session_id、系统 PID、日志路径或旧 Host 身份绕过授权。
- observer 不获得停止 ROOT、取消子任务、终止进程、修改 monitor 或写入 stdin 的能力；所有控制继续要求当前有效 controller。读取不得伪造模型工具调用或借读取升级 controller。
- 只提供现有 output owner 经过 sanitizer 的保留输出，遵守单次读取/分页限制、retention、cursor/gap、响应截断和可用性语义；不开放 raw PTY，不从其他文件或历史工具内容偷偷恢复已被裁剪的日志。
- sanitizer 处理终端控制序列，不等于内容脱敏，也不能证明日志对任意读者安全。日志可能比 preview 包含更多正文，必须由上述读取授权决定能否查看，不能以“已经 sanitizer”代替权限检查。
- Host 替换、attachment 失效、资源 prune、cursor 失效与权限拒绝分别表达；不能因权限错误泄露其他 session/owner 下的进程是否存在。重连后的读取重新绑定并授权，不复活旧 handle。
- 如采用输出订阅，复用已有 owner，并在取消读取、页面关闭、连接/Host 失效时释放订阅和在途读取持有关系；不为了前端条目长期存在而制造永久保活，也不注册模型 monitor 来实现实时日志。

第 19 节 T11/T12 为 observer/controller 的合法读取、越权控制、跨 session/Host、失效 attachment/cursor、输出缺口与资源 prune 分别提供测试。授权的同一 retained 输出范围应按相同内容规则读取，不另建 observer 专属输出缓存或第二套权限 registry。

## 12. 后端改动范围与不变量

建议复用边界：

- **HostSession**：controller 操作接纳、exact target、进程/监视组合、反馈目标固定和生命周期；持有必要的 typed process-local 控制 attempt 及查询结果，不成为 turn/task/process 状态的第二真源。
- **KernelSubagentManager**：共享 typed task cancel，既有 terminal settlement 与 dependency frontier。
- **DirectKernelToolPort / TerminalSessionManager / ProcessRegistry**：有限查询/控制接口、成功后台交付真源及 background_adopted 只读投影，继续独占进程物理管理与保留策略；在这里统一修正终止意图与最终状态仲裁，用户入口和模型 kill 不得分叉成两套物理终止实现。
- **TerminalMonitorCoordinator**：保留每 process 的 0/1 未关闭 registration，查询可选监视、取消与现有 in-flight 结算；不选择模型 turn。
- **既有 canonical writer / repository**：按第 8.5 节的真实 typed 来源，在既有事务中接纳 entry 与相应 occurrence；确认已提交历史，不负责物理控制或保证未来投递。
- **既有 provider-input / safe-point / execution owner**：控制反馈的编译、资源、append-only 接纳，以及最终选定请求的精确纳入和 transport 发起事实；不让 UI 另写历史或推测执行阶段。
- **protocol/gateway/bridge**：最小闭合 DTO 和受限读取/控制路由；按第 11.4 节校验 observer 日志读取与 controller 控制权限。
- **frontend**：任务及依赖、后台命令和真实反馈的投影，不产生执行真源；呈现范围固定在第 16–17 节，读取接入固定在第 18 节。

权限产品范围按第 11.4 节冻结，接入点按第 14.1、18.4 节固定：人工控制由有效 controller 与 session/Host 目标授权，observer 只在现有 session 读取授权下读取受限 retained log；不能拿模型工具的展示状态或 sanitizer 代替授权，也不能直接调用私有方法绕开 owner。模型调用仍走原 permission/Hook/settlement，不为人工操作伪造模型工具请求或工具 Hook。

保留 SYSTEM/tools byte-identical、messages append-only；只有已批准的 cold epoch/compaction successor 才能重建根。读取面板、控制按钮、重连都不是新 rebase 边界。

不新增通用 Activity 实体、图数据库、scheduler、fingerprint registry、receipt/checkpoint、durable live-process/monitor job 或全历史确认集合。必要的新 DTO 不等于新的 durable authority。第 18.1 节列明唯一获准的真实人工控制类别及 accepted occurrence；不由此授权新增表、关系、投递队列，或为操作/反馈进度另增事件。

控制开始后页面断开不应遗失物理操作 owner；复用现有 cancellation/physical settlement 纪律。不承诺跨 Host crash 的进程恢复或控制反馈自动补投。

### 12.1 当前控制生命周期缺口

目前 STOP 在 gateway 中直接等待 `stop_current_turn()`，返回时只有一个即时构造的 command outcome；该方法不接收 STOP 的 command_id，现有 `query_command()` 也没有可恢复的 exact STOP 结果。ROOT 的 canonical 终态可以读取，但它不是这次停止操作的接纳凭证。

浏览器 controller 使用顺序往返的 `TerminalProtocolClient`，gateway 同样逐帧等待 dispatch 再响应。耗时 STOP 占用的是该 attachment 的请求通道，后续同通道查询/控制会等待；不能把它描述为 `await task` 阻塞整个事件循环。当前 `stop_current_turn()` 已在等待 ROOT 前释放 Host lock，这一点应保留。

因此，本条 P1 要补的是控制请求的接纳、持有和结果确认，不是新调度器，也不是靠 UI 提前显示成功掩盖等待。

### 12.2 快速接纳与完成边界

“停止本轮运行”“取消子任务”“终止后台命令”遵循相同的接纳纪律，但保留各自真实执行 owner：

1. 前端在发送前保存本次 command_id、操作种类、原 session/Host owner 和 exact target；不能等收到响应后才获得查询所需的身份。
2. Host 校验当前 controller 权限、原 owner 与精确目标，并检查同一 command 是否已有相同请求。完整 typed 请求值直接比较；同一 command 携带不同操作、目标或参数必须拒绝冲突，不引入请求 fingerprint。
3. 对需要执行的控制，Host 先安装明确的 process-local 操作持有关系，保证请求 waiter 离开不使已接纳工作消失；进程组合同时固定第 8 节的反馈目标。不能先回复已接纳，再尝试创建可能失败的执行任务。
4. 接管成功后及时返回。操作仍在进行时复用现有 `CommandStatus.PENDING` 并以明确的 public code 表达已接纳；不为这一步额外增加 `ACCEPTED` 枚举，不等候 ROOT 收尾或进程组 join 才回复。

目标已终态、请求被拒绝或结果已确定时，允许直接返回对应结果，不强制经过 PENDING，不人为增加第二次响应或新的短等待阈值。协议本来就有 `PENDING`；使用它不代表已有 STOP 的接纳/查询能力已经实现。

接纳只表示 Host 已接管这次控制，不等于取消成功、物理操作完成、监视全部关闭、反馈 canonical accepted 或反馈请求已发起。“控制已接纳”与第 8.6.1 节的“反馈已接纳”必须在 DTO/UX 中区分对象。最终判断继续由原 owner 提供：

- ROOT 停止以确切轮次和已启动效果的真实结算结果为准，不把调用 `task.cancel()` 或 waiter 返回当作成功证明。
- 子任务取消以原 kernel/repository 的任务终态与依赖传播结果为准，不由控制 attempt 自行改写 task 状态。
- 进程终止以第 7 节的物理结果为准，monitor 和模型反馈状态分别报告。物理操作完成不能被尚未交付的模型反馈掩盖，部分失败也不能压成统一成功。

操作已有明确失败/部分结果时，不应无期限伪装成 PENDING；`PHYSICAL_SETTLEMENT_INCOMPLETE` 可以是本次终止尝试的实际结果，但不授权丢弃仍在收尾的物理 owner。各对象结果到 wire status/public code 的精确映射固定在第 14.2 节。

### 12.3 原 command 查询、重连与不可用

优先扩展现有 command 查询接入，使其返回这次控制的接纳状态、固定目标及可用结果。目标当前状态可通过既有读取路径获取，但不得代替操作结果：进程已经结束，不证明本次 kill 被接纳；ROOT 已中断，也不证明它由这一 command 停止。

查询必须按原 `command_id + session/Host owner + exact target` 校验；这些是既有身份的绑定要求，不是新增 hash/epoch/generation 的理由。重连后可通过重新授权的新 attachment 查询仍存活的原 Host，不要求沿用已经断开的旧 attachment，也不能把当前新 Host 自动当成原 owner。

| 查询处境 | 可表达的结果与限制 |
| --- | --- |
| 原 Host 可用，原操作仍在进行 | 返回该 command 的接纳/进行中事实和已知部分结果；查询不触发重复取消、kill 或模型反馈 |
| 原 Host 可用，原操作已完成且结果仍保留 | 返回原操作的实际结果，与原目标身份一致 |
| 网络不可达，尚不能确认原 Host 是否丢失 | 显示“结果待确认”；不猜成功，也不直接断言 OWNER_UNAVAILABLE |
| 已确认原 Host 不再可用或已被替换 | 返回 `OWNER_UNAVAILABLE`；不从旧 ID 重建执行权，不伪造原操作结果 |
| 原 Host 可用，但原操作记录不存在或已不再可查 | 明确“无法确认原操作结果”；缺失本身不证明请求从未接纳，也不能证明执行失败 |
| 新 attachment 无查询权限，或请求身份不匹配 | 按授权/身份冲突拒绝，不泄露其他 owner 的对象或操作记录 |

保留窗口内，相同 typed 请求的重复提交必须连接到原 attempt/结果，不能重复执行；不同参数必须拒绝。网络未知后的自动动作是只读查询，不是重发控制。第 14.3 节以提交身份内的原截止时间拒绝已退役旧请求；查询仍允许返回不可确认，不承诺历史级幂等。

查询不到原结果时，不自动生成新 command、不改投下一轮或新进程、不触发重放。用户之后主动发起的新操作是独立授权，必须重新确定所见目标，不能被实现成原请求的隐式重试。已经获得的真实终态或部分结果可以继续展示，但不能补造尚未确认的部分。

第 7.3 节的“重试终止”只适用于用户已知原尝试失败、目标仍允许再次终止的场景，使用新 command_id；它不覆盖原结果，也不继承原反馈目标。未知查询不能自动转入这一分支。

### 12.4 最小 process-local attempt 与保留纪律

允许 Host 为这些用户控制持有最小 typed process-local attempt：冻结的 command/操作身份、原 owner 和目标、实际执行 task/future 引用、必要的分项结果，以及进程组合的反馈绑定。反馈只关联必要的确切 entry/request/execution 身份与第 8.6 节的已知事实，不生成第二套内容真源。其产品必要性仅是耗时控制接管、同一请求合并和断线后结果查询；不复制进程输出、任务及依赖、canonical 历史或完整 provider 输入。

- 执行仍由现有 ROOT task、KernelSubagentManager、ProcessRegistry 等 owner 完成；attempt 只关联已有工作，必要的 Host 组合 coroutine 不成为另一套执行引擎。
- 已接纳操作不能由浏览器/HTTP/协议请求 waiter 独占。请求取消、页面关闭或连接替换只终止等待/展示，Host 保留强引用并观察结果/异常，不能留下无人持有的 fire-and-forget 工作。
- Host 正常关闭沿用现有 owner 关闭与物理结算纪律；Host crash 不承诺重放、恢复或跨重启补投。取消权限在接纳时校验，查询仍按当前 attachment 授权。
- 在途 attempt 不能因为页面断开、前端不再查询或普通结果清理而丢失。最终结果不能在完成回调中立即删除，否则响应丢失后的查询没有意义。
- 已完成结果按第 14.3 节的明确时间规则保留/退役；不与 process prune 捆绑，不保留整个 Host 的无限操作历史，也不增加操作总数或任务寿命上限。
- 不为保持“结果曾存在”的证明再加永久 tombstone、receipt、确认 registry 或 durable job。缺失/退役明确降级为不可确认，不能为补齐查询而扩大 durability。

这不是通用 Operation/Activity 框架。操作 attempt 的进度与查询本身不新增事件、表、恢复调度器或 fingerprint；第 18.1 节唯一获准的 accepted occurrence 记录真实 canonical 来源，不是这套局部 attempt 的 receipt，也不记录逐步交付状态。现有 ingress/compaction 等局部 attempt 可作为持有与 shield 模式的参考，但它们的即刻退役或其他专用确认策略不能未经核对直接套到 STOP 查询。

### 12.5 物理等待与锁边界

Host 只能在短临界区冻结 exact owner、目标和反馈绑定，随后通过现有 I/O/线程边界调用阻塞式进程终止；不能持有 Host async lock 或 process `state.lock` 等待进程组、reader、watcher 或 timer join。即使已经释放锁，也不能在事件循环线程直接执行阻塞式 kill/join。

ROOT/子任务的 async 取消与等待保留原机制；后台 kill 复用实际支持线程工作的 I/O owner，不复制线程池或另建物理 executor。接纳响应与后台持有关系分开，并不放松原工具/effect settlement，也不要求为此重写串行协议客户端或新增推送事件。

## 13. 典型场景验收

| 场景 | 预期 |
| --- | --- |
| 短命令在首次等待内完成 | 只需对话工具结果，无后台条目要求 |
| 长命令转后台，没有 monitor | 可见、可读、可终止；不自动注册监视 |
| 子任务命令转后台 | 可见来源 task；没有 ROOT-only monitor 不影响管理 |
| 正常后台交付后自然结束，再 reload | retained 期间 background_adopted 仍为 true，并从原 owner 读取真实终态；不依赖页面曾看过 running |
| foreground decision abort/异常清理设置 yield_decision=True | 不误判成功后台交付；仍存活的物理效果由原 owner 收尾，不能从该布尔值伪造后台资格 |
| 同一 process 再注册一个未关闭 monitor | 保留 DUPLICATE_PROCESS_MONITOR 拒绝；DORMANT 也占用单进程名额，不增加多 monitor 分支 |
| 单个 monitor 同时配置输出进度与 heartbeat | 按既有优先级、合并与完成/期限规则工作，不要求三个独立 monitor 或每条件独立投递 |
| 取消 DORMANT monitor，原注册工具随后完成结算 | 不重新激活已关闭监视；继续原工具结算 |
| 当前没有未关闭 monitor，但旧观察已冻结 | 单进程 DTO 可为空；既有 in-flight owner 仍结算，不能据此声称没有在途投递 |
| 注册 monitor 后模型继续读代码 | 主循环继续，监视不强制退出 |
| 注册 monitor 后模型正常结束回复 | 后续普通观察仍按原 monitor 合同可唤醒 |
| 活动 ROOT 期间用户终止后台命令 | 关闭该监视的未来通知、终止进程；向接纳时绑定的 ROOT 提供真实反馈。ROOT 不被中断；若将正常结束，须通过受 watchdog 约束的 completion fence 接纳并精确纳入后续请求后再结束 |
| 旧轮次 O 的命令在同 session 的活动 ROOT A 期间被终止 | 显示命令来源 O 与反馈目标 A，绑定接纳时的 A；不复活 O，不把结果当成新指令或旧任务移交 |
| 点击前预览 ROOT A，Host 接纳进程控制时已切到 B | 预览不构成交付承诺；本版绑定接纳时的 B 并如实返回。接纳后 B 再结束不得改投；STOP A 则始终只针对 A |
| idle 时用户终止 | UI-only；不创建本次反馈 entry/accepted occurrence、新 turn/provider call 或待投递记录 |
| idle 接纳控制后出现人工 ROOT B，或稍后发起下一人工轮次 | 不向 B 或下一人工轮次自动补投该控制反馈；已有历史/冻结观察及主动状态查询照常遵守各自合同 |
| 操作期间原 ROOT 结束并出现新 ROOT | 不改投新 ROOT，不复活旧轮次 |
| 无 monitor 的进程被人工终止，或终止失败 | 活动目标下需要反馈时使用真实 USER_CONTROL_FEEDBACK 与实际结果；idle 仍为 UI-only，不伪造 monitor/coverage、USER_STEER、tool call 或 TerminalObservationAccepted |
| 反馈 canonical 已接纳，原 ROOT 随后结束 | entry 和 accepted occurrence 保留；只更新原请求纳入结果，不删除、不改投、不自动唤醒 |
| canonical 提交响应丢失 | 有 owner 正在确认时 PENDING；不能完成确认且是否提交不明时 UNKNOWN，不仅凭异常判 FAILED，也不补写重复 entry |
| canonical 可能已提交、ACK/后续确认失败，此时原 ROOT 结束 | 继续对原 immutable candidate 做只读 exact-confirm；确认 winner 则 ACCEPTED 并按真实请求证据结算 inclusion，确认无 winner 才可 FAILED(TARGET_CLOSED)，确认能力丢失则 UNKNOWN |
| canonical 确定拒绝，或已接纳后的纳入确定失败 | 分别为 FAILED + NOT_APPLICABLE、ACCEPTED + FAILED，携带对应边界的真实原因 |
| sequence 在 cut 上界内，但被 floor/scope/task 筛选排除 | 不显示 INCLUDED；以最终选定请求的确切反馈来源为准 |
| 候选曾含反馈，但候选被放弃或仅用于测量 | 不能以候选 membership 宣称已编入本轮请求 |
| 请求已编入反馈，但 install 后尚未 transport open | INCLUDED 不变，不能显示“本轮请求已发起”；原轮次结束也不改成未纳入 |
| 请求纳入或 transport 回调先于 Host 本地 ACCEPTED/INCLUDED 更新 | 以 exact candidate 与 `{session, turn, context revision, model_call_index}` 保留 process-local 观察，后续原子合并三轴；不得因回调先到而丢弃 |
| transport 已调用，但请求失败或服务端收讫无证明 | 如实显示请求发起及真实执行结果；不宣称模型已收到、已读或已理解 |
| 原 owner 丢失时接纳/纳入已知或未知 | 保留已知 ACCEPTED/INCLUDED；仅未知部分为 UNKNOWN。网络中断本身不等于 owner UNAVAILABLE |
| 重复查询、后续普通历史读取或 compaction | 查询不重复反馈/调用模型；历史处理不改原目标，不以摘要相似内容证明原请求纳入 |
| monitor 已有冻结/提交中的观察 | 原结算不被破坏；不声称全撤回 |
| 取消监视成功、终止尝试失败且进程组确认存活 | 持续显示“进程仍在运行 · 通知已关闭”，保留可用日志；原尝试已结束且允许控制时提供人工重试，不恢复监视或自动重试 |
| 取消监视成功，进程组已结束但仍在收尾，或状态未知 | 分别显示收尾未完成、终止结果待确认；优先查询，不错误显示仍运行或诱导重复 kill |
| 模型自身 kill/cancel | 保留原工具语义与结果；不追加重复人工反馈 |
| 取消有依赖的子任务 | 下游由 kernel 进入真实依赖失败状态，无关任务继续 |
| 子任务在取消 preflight 后自然完成 | cancel owner 按最终 canonical task 状态返回 ALREADY_TERMINAL/COMPLETED 与真实原因，不得返回 CANCELLED/USER_CANCELLED |
| 取消子任务，但它启动的服务器仍运行 | 显示关联服务器，不默认杀它 |
| 点击主助手停止入口 | 名称为“停止本轮运行”，帮助说明 ROOT 执行范围、收尾与独立后台活动；不承诺仅停文字流或所有工具立即停止 |
| 停止期间已启动工具完成收尾 | 保留真实工具结果，不继续本轮正常执行；未确认终态前不显示“本轮已停止” |
| ROOT 收尾或后台 kill 耗时 | Host 真正接管后及时返回 PENDING，不等待物理收尾占住 controller 请求通道；后续查询可取得原操作状态 |
| 目标已结束或控制结果已立即确定 | 可直接返回实际结果，不强制制造 PENDING 阶段 |
| 校验或操作持有关系安装失败 | 不返回已接纳；保留真实拒绝/错误，不遗留无人持有的控制工作 |
| Host 已进入 closing，或异步 preflight 返回时才进入 closing | 不接纳新的控制 attempt，返回 OWNER_UNAVAILABLE；原已接纳操作的查询、确认和结算继续，不能让新任务错过关闭快照 |
| 接纳响应丢失后重连到原 Host | 新授权 attachment 按原 command/owner/target 查询；不重发控制、不重复制造反馈 |
| 已知失败后用户主动点击“重试终止” | 新 command_id，重新授权和核对原 process；按此次接纳时绑定反馈目标，保留原尝试结果，不复用旧 ROOT 或偷偷重启命令 |
| 相同 command 重复请求或携带不同目标 | 保留期内相同 typed 请求连接原结果，不同请求拒绝冲突；查询永不执行控制 |
| 网络不可达、Host 替换、原结果已退役 | 分别表达待确认、OWNER_UNAVAILABLE、原结果不可确认；不混成成功或从未执行 |
| 原目标已终态但原控制结果不可查 | 可显示目标状态，不能推断这次 command 已接纳或造成该终态 |
| 页面关闭时已接纳操作仍在执行 | Host 继续持有并观察结算；正常关闭按原 owner 纪律处理，crash 不自动重放 |
| 控制结果完成、目标仍有输出/线程收尾 | 分别显示操作结果与实际 physical_state；不遗失物理 owner，不用 PENDING 掩盖已知部分失败 |
| STOP A 迟到，B 已开始 | B 不受影响；返回 A 的真实目标结果 |
| 自然成功进程在完成通知和物理 join 后收到终止 | 返回 `ALREADY_TERMINAL` 与原 `success / exit 0`；poll、log、list、输出终态和完成通知一致 |
| 自然失败进程在完成通知和物理 join 后收到终止 | 返回 `ALREADY_TERMINAL` 与原 `error / exit code`；不得改写为 killed |
| 自然结束与终止请求交错 | 由 ProcessRegistry 的单一仲裁点决定胜者；所有读取渠道和完成发布得到同一终态，不要求声称信号的严格因果 |
| 自然结束、尚在 reader/watcher 收尾，本次未接纳终止意图也未发送信号 | 收尾后返回 `ALREADY_TERMINAL` 与原真实终态；不能仅因本次调用完成了 join 就声称 `TERMINATION_COMPLETED` |
| shell leader 已退出但同组子进程仍存活 | 仍按 live 进程组接受终止并完成物理 join，不能误报 `ALREADY_TERMINAL` |
| 进程组已消失但 reader/watcher 尚未 join | 返回物理结算未完成及实际 `physical_state`，不能误报仍在运行或已经全部完成 |
| 新 command 终止已结束进程且无新控制效果 | 返回原真实终态、不重复发送信号、不重复反馈；本次 NOT_REQUIRED + NOT_APPLICABLE 携带无新效果原因 |
| 原 command 查询/重复提交时进程已终态 | 返回原操作及反馈事实，不将原成功结果改成 no-op 或 NOT_REQUIRED；记录缺失按不可确认处理 |
| 进程已结束，本次首次关闭仍有效 monitor | 单独报告新监视变化；按活动/idle 规则处理反馈，不宣称再次终止进程；后续无变化查询不重复接纳 |
| 模型 kill 与用户终止同一类进程 | 共享修正后的终止仲裁核心；模型工具不因此取消 monitor 或追加人工反馈 |
| owner 替换或保留窗口失效 | 禁止控制旧 ID；说明日志/状态不可用，不伪装仍运行 |
| finished 记录 retained、整条 prune、仅输出被裁剪 | 分别为更新终态、刷新列表允许消失、记录仍在但有日志缺口；统一说明当前 Host 保留范围，不新增 tombstone |
| 已打开详情精确读取确认 prune，或只是分页未加载/网络断开 | 前者可显示记录不可用，后者不能据此推断资源已删除；进程缺失也不证明某 command 从未执行 |
| 合法 observer 读取超过 transcript preview 的 retained log | 在现有 session read 授权、当前 attachment 和 exact owner/stream 下允许；同样遵守 sanitizer、读取上限、retention 和 gap |
| observer 尝试控制、伪造 session/PID/cursor | 拒绝越权；普通查看不升级 controller 权限 |
| 其他 session/Host 的 process ID、失效 attachment 或旧 cursor 被用于日志读取 | 不越界取日志、不复活旧 owner、不因错误泄露其他 owner 的资源；sanitizer 不代替授权 |
| 输出订阅读取结束或连接/页面关闭 | 释放订阅和在途读取持有关系，不注册 model monitor，不为保留界面条目永久阻止资源清理 |
| 切会话后旧请求完成 | 不更新/重连新会话，不串页、不串 toast |

实现后应通过真实 owner/adapter 测试和可控时序验证，不靠模型恰好慢来覆盖。真实 provider/browser 测试需按 AGENTS 只读使用保存配置，控制明确的探针命令；新功能证据不能由 PR02 的旧场景替代。

这些是 PR03 必测场景，不是已经取得的验证结果；第 19 节指定测试落点，第 20 节定义 activation 门槛。

## 14. 已定案的控制接口与操作生命周期

本节的新增名称是 PR03 要实施的目标接口，不声称当前生产已存在。所有模型工具沿用原 tool dispatch/permission/Hook/settlement；人工入口不伪造 tool call。

### 14.1 唯一接入链与请求

`pulsara-app → runtime-adapter → browser_bridge → v3_gateway → HostSession → 原对象 owner`。继续使用现有 command/query_command 通道，不新建通用 operation service、REST kill 捷径或另一套协议客户端。

在现有 CommandRequest 上复用 `command_id`、`target_turn_id`、`subagent_task_id`，仅补 `expected_session_id`、`expected_host_session_id` 和 `target_process_id`。这三个控制必须携带完整 owner/target；其余 command 沿用现行合同，不能为“统一”重新设计 prompt/plan/compaction 请求。

| command_kind | 唯一目标字段 | Host / 物理 owner 目标方法 |
| --- | --- | --- |
| STOP_ACTIVE_TURN（保留枚举值） | target_turn_id | Host.request_stop_turn → 现有 ROOT cancellation intent / task / settlement |
| CANCEL_SUBAGENT_TASK（新增） | subagent_task_id | Host.request_cancel_subagent → KernelSubagentManager.cancel_task |
| TERMINATE_BACKGROUND_PROCESS（新增） | target_process_id | Host.request_terminate_background_process → coordinator 取消 + ProcessRegistry.terminate_if_running |

同一控制只允许一个目标；text、permission override、plan 参数、force 和其他目标必须为空。session 与 host ID 取自已认证 attachment/当前投影，必须同当前控制连接 exact-join；它们是身份，不是权限 token。gateway 与 Host 在接纳时都校验 controller，非 controller 仍走原拒绝路径。

Host 的新控制校验必须在持有同一 Host lock 时检查 `_closing/_closed`；异步 subagent preflight 返回后、安装 attempt 前再检查一次。closing 只禁止新的控制接纳，不删除或拒绝查询既有 command，也不阻止已接纳 attempt 的 exact confirmation 与 settlement。

浏览器在点击时冻结 owner、确切目标、command_id；STOP 缺少可见 activeTurnId 时不发送。只读更新不能把已冻结 A 改成 B。将 `stopActiveTurn()` hard-cut 为带冻结 command/owner/turn 的 typed 调用；旧无参数接口、gateway 的“有 target 就拒绝”规则同次删除。

CLI `:stop` 先只读取得当前 Host control snapshot，冻结其中的 active ROOT 和提交身份，调用同一个 Host.request_stop_turn，并按原 command 查询结果；无 active ROOT 就报告无目标。CLI 不保留 public 无目标 stop_current_turn，也不将关闭 Host 的内部 cleanup 改成用户 STOP。原 method 删除，内部 task cancellation/settlement helper 保留并以确切 task 引用调用。

### 14.2 结果 DTO、wire status 与查询

CommandOutcome 保留既有字段，增加可选 typed `user_control`，仅用于上述三个操作。它包含：

- `operation`、`session_id`、`host_session_id`、判别联合 `target = ROOT_TURN | SUBAGENT_TASK | BACKGROUND_PROCESS`。
- `accepted: bool`：是否真正安装操作持有关系；`execution = NOT_STARTED | RUNNING | FINISHED`。
- 操作分项结果：ROOT 的真实 turn status/reason；子任务的真实 task status/reason；进程的 disposition、status、exit_code、physical_state、`group_alive: bool | null`。未知不是 false。
- 进程组合的 `monitor: null | {monitor_id, outcome, in_flight_observation_ids}`，outcome 为 CANCELLED / ALREADY_CLOSED / FAILED / UNKNOWN；没有关联 monitor 是 null，不算错误。
- 进程组合的 `feedback`：第 8.6 节三轴、typed reason、绑定 ROOT 或 idle、可用的 entry_id 与请求身份；请求发起投影按第 18.3 节。ROOT/task 操作不额外制造人工模型反馈。
- 实际 public error code/detail；只携带现有 owner 的诊断，不将进程日志或整个 provider request 复制进 attempt。

增加 `CommandStatus.FAILED`，仅表示操作已接管但本次尝试确定失败/部分失败。它是协议结果枚举，不是新 durable 状态或事件；不增加 ACCEPTED 枚举。所有 Python/TS 映射及闭合集合测试同次适配；其他 command 的含义不变。

| 情况 | status / public_code | 额外要求 |
| --- | --- | --- |
| 已接管、仍执行 | PENDING / CONTROL_ACCEPTED | accepted=true，execution=RUNNING，不显示已停止 |
| ROOT 已按用户原因收尾 | SUCCEEDED / ROOT_STOPPED | 完整原结算已确认 |
| 子任务已取消并结算 | SUCCEEDED / SUBAGENT_CANCELLED | 保留真实依赖传播结果 |
| 目标在控制前已经终态且无新效果 | SUCCEEDED / CONTROL_ALREADY_TERMINAL | accepted=false，保留原终态，不伪造用户取消 |
| 进程/可选监视的请求均完成 | SUCCEEDED / BACKGROUND_CONTROL_COMPLETED | 以分项真实结果显示；反馈纳入状态独立，不等同模型收到 |
| 已接管后一个或多个控制分项失败/物理收尾不完整 | FAILED / CONTROL_PARTIAL_FAILURE 或 CONTROL_FAILED | accepted=true，已知成功分项不丢；物理 owner 继续持有未收尾工作 |
| 目标不是当前绑定 owner/turn，或字段不合法 | REJECTED / CONTROL_TARGET_NOT_CURRENT 或 CONTROL_REQUEST_INVALID | 不作用于新目标，不安装执行 |
| 目标不存在/已被 prune | REJECTED / CONTROL_TARGET_UNAVAILABLE | 在授权检查后返回，不泄露其他 session 的资源 |
| 确认原 Host 失效 | REJECTED / OWNER_UNAVAILABLE | 不向新 Host 发出替代控制 |
| 同一 command 的完整 typed 值不同 | REJECTED / CONTROL_COMMAND_CONFLICT | 不更新旧 attempt |
| 首次提交已经过期 | REJECTED / CONTROL_REQUEST_EXPIRED | 无操作；只能人工新授权新 command |

反馈失败不把物理成功改成 CONTROL_FAILED；两者分别展示。自然终态后来改变展示为 killed 属于错误，不能用上述结果码掩盖。

QueryCommandRequest 增加可选 `expected_control`，携带原 operation/session/host/判别目标；对控制格式 command_id 必填。Host.query_command 先处理本地 control attempts，再走既有普通 command 查询，不能把控制写成普通 durable command row。查询时重新校验 attachment 与 owner；允许原 Host 上重新授权的新 controller attachment，不要求死去的 connection ID。

控制查询返回 typed `control_query_status = FOUND | RESULT_UNAVAILABLE | OWNER_UNAVAILABLE`；现有 found/outcome 继续供普通 command 使用。FOUND 才携带原 outcome；RESULT_UNAVAILABLE 不证明未接纳、失败或回滚。身份/授权错误走独立拒绝，不混入 UNKNOWN。observer 可以读取目标状态/受限日志，不能查询 controller 的控制 attempt。

通过身份、权限和参数校验后的确定 no-op 也保留最小的原 command 结果，适用第 14.3 节退役规则；accepted=false 只表示没有安装物理执行，不代表结果不可查。授权失败不保存攻击者载荷。查询不随目标后来变化重算原结果；物理/反馈/请求发起三部分分别读其原已知事实。

前端未知时保存原引用、重连后查询；不自动重发。切会话后旧 success/error/finally 不更新新会话，也不自动打开原页面。查询已结束且有可重试的物理失败时，“重试终止”是重新授权的全新 command，不修改原记录或反馈目标。

### 14.3 提交期限与结果退役：不用永久墓碑

仅给已完成 attempt 设置 TTL、却允许任意未知 ID 当新请求执行，会让清理后的原请求重新执行；只说“process-local”不能解决这个问题。本版采用**同 Host 的有期限提交身份 + 有限结果保留**，不引入 lease、server reservation、签名或永久去重集：

1. 原 Host 的 live control snapshot 增加只读 `control_admission_deadline_ms`：使用该 Host 的单调时钟毫秒，值为读取时刻 + 3,600,000 ms。它不是授权令牌，不保存单独 registry。
2. 三类控制使用 `command:control:<deadline_ms>:<uuid4>` 作为 command_id；deadline 是身份的一部分，修改期限必须产生新 ID。CLI/浏览器用 snapshot 给定期限，不比较本机墙上时间，不新增 hash/MAC/generation。
3. Host 先 exact-join 已存在 attempt；存在且值相同则返回原结果，即使原提交期限已过。不存在时，要求 `now < deadline <= now + 3,600,000`，否则拒绝 CONTROL_REQUEST_EXPIRED / CONTROL_REQUEST_INVALID，不执行。Host ID 必须仍相同。不存在且未过期的 ID 只可能是未接纳的新命令，因为旧 attempt 在该期限内不能退役。
4. 在途控制、实际取消 join，以及仍由 owner 推进/确认的反馈，均不得按时间清理。反馈原目标结束时完成已有确认；失去确认能力时如实 UNKNOWN，不继续无人负责的 PENDING。进程操作已有部分失败并结束，不等于物理 owner 已可释放。
5. attempt 所有本地推进工作已终局后，保留到 `max(原 deadline_ms, finished_monotonic_ms + 3,600,000)`。到期后在控制提交、查询或既有 Host 维护机会下清理；不新增永久 job。清理不等待 process prune，也不删除 canonical entry。
6. 清理后原 ID 的期限必已过：原 query 返回 RESULT_UNAVAILABLE，原 submit 拒绝 CONTROL_REQUEST_EXPIRED；不重新执行、不重建原结果。已保留前端事实可展示为最后已知。改变 UUID/期限属于新操作，客户端只有明确人工点击才生成。
7. Host 正常关闭结束/确认既有持有工作后释放 attempt；crash 后不恢复。用旧 Host 身份查询返回 OWNER_UNAVAILABLE，不用重启后不同的单调时钟重新解释旧 ID。

这两处 1 小时是**控制提交新鲜度和断线查询保留窗口**：给用户断线、关面板再回来的确认时间，并与现有本地 finished-process 的小时级保留体验对齐；不是 turn、进程、工具或 worker 运行期限，也不是整库历史限制。任务可运行数天，已接纳的操作不会因到期被取消。没有总操作数/任务数上限，不能为实现该策略截断历史或新增记录数量 cap。测试使用可控单调时钟，不实际等待一小时。

### 14.4 物理控制的确定接法

ProcessRegistry.terminate_if_running 为显式 kill 与人工终止的共享物理入口；timeout/abort/rollback/cleanup 共享同一短锁内终态仲裁 helper。最终状态冻结一次，_status、output.finalize、完成回调、list/log/poll 都引用同一事实。模型 kill 的 API/结果结构不因此增加人工反馈或关闭 monitor。

Host 通过原 KernelIO.run 执行同步 kill/join；不把线程池、信号处理或输出缓冲复制到 Host。monitor-first 在 coordinator 锁内获取并关闭当时的可选未关闭 registration，锁外 kill；monitor 关闭失败仍报告该分项并尝试终止原进程，不伪造通知已关闭。已经冻结的旧观察由原 owner 结算；后续新授权的 monitor 注册不是本次取消的隐藏级联目标。

KernelSubagentManager.cancel_task 接收确切 task_id 并返回 typed cancel disposition 与真实任务状态；原 _stop 工具只负责参数转换/结果编码并调用它，移除旧内嵌取消分支。等待依赖/排队任务可以取消，活动任务必须保留 started-effect settlement，终态任务不被改写。preflight 后必须由子任务 owner 根据最终 canonical status 再裁定 disposition：自然完成/失败/中断返回 ALREADY_TERMINAL 与实际原因，只有最终确认为 CANCELLED 才返回 CANCELLED/USER_CANCELLED。

## 15. Phase A–D 实施顺序

所有阶段属于同一个 hard cut；阶段完成不单独标记 ACTIVATED，也不引入运行时 feature flag。

| 阶段 | 实施内容 | 阶段通过条件 |
| --- | --- | --- |
| A：真实控制 owner | 终态仲裁、background_adopted、typed 子任务取消、exact ROOT cancellation helper | P0 自然终态与完整进程组测试转绿；原工具/settlement 不退化 |
| B：Host 与协议 | 三类控制、查询/退役、CLI、后台读取、唯一反馈事件及 provider 投影 | 第 14/18 节各失败路径有确定性测试；schema/oracle 与生成协议同步 |
| C：生产前端 | 完整替换任务 tab、新增后台终端、原始结果风格；保留生产能力/主对话结构 | 第 17 节交互、原文、分页、权限、跨会话 owner 测试转绿 |
| D：删除与 activation | 第 19 节清理、完整回归、continuity、隔离安装与新场景真实 provider/browser dogfood | 第 20 节逐项提供实际证据，无豁免/未解释失败 |

从生产组件和原 owner 直接实现，不通过静态 Demo 承担执行。先写能暴露当前缺陷的断言，再修正；不让最终文件/截图“看起来正确”替代精确身份和副作用结算测试。

## 16. 已确认的视觉决定：工具原始结果配色

状态：**已在生产实现并通过浅色、深色、桌面与窄屏验证。** 设计参考为 `design/sidebar-demos/group-modal.html` 中子任务工具展开后的「原始结果」区域。

### 16.1 用现有暖色纸面层次替换黑底

用户认可的是截图中比外层纸面更深的暖灰棕结果底色，不是另选一种高饱和深棕色。以 Demo 的实际 CSS 为准，复用生产已有主题变量：

| 区域 | 主题变量 | 当前浅色主题值 |
| --- | --- | --- |
| 展开区外层 | `--paper-raised` | `#faf8f2` |
| 原始文本 / JSON 内容底色 | `--paper-deep` | `#e9e5db` |
| 结果正文 | `--ink-soft` | `#32343d` |
| 次级标签与边界 | `--muted` / `--line` | 沿用现有主题值 |

生产当前 `frontend/app/styles/workbench.css` 的 `.terminal-output` 固定使用 `#1c1e23` 背景和 `#cecac1` 正文，浅色界面中仍呈黑底浅字。后续工具结果展示改用上述层次，不再让普通原始结果呈现为独立的黑色终端块。

### 16.2 应用范围与保留行为

- 仅借鉴 `create_agent_tasks` Demo 的原始结果区域：暖灰棕底、深色正文、等宽字体、内边距、轻圆角与滚动容器。推广到生产其他工具的同类原始文本/JSON和分页完整输出。
- 保留生产 TraceCard 的标题、折叠层级、结果/完整输出入口、复制、diff 和 artifact 页控制。**不采用 Demo 的创建摘要、任务名称双列、整块点击跳转或额外原始结果折叠层级。** 艺术风格调整不是交互与正文协议改版。
- 原文及复制内容保持不变，不翻译、清洗、重排 JSON 或重写正文；保留完整输出分页、截断/缺口、错误和不可用提示，不将当前页称为完整结果。diff 仍显示真实 unified diff，不恢复独立“复制差异”按钮。
- 替换背景时同步检查为黑底配置的正文、按钮、边框和状态色，避免浅底浅字；差异增删、错误等区分不能丢失。
- 使用 `frontend/app/styles/base.css` 中已有主题变量，深色模式使用其深色取值，不复制颜色常量或把浅色值强制用于深色模式。
- 不更改主对话正文 Markdown 代码块、公式及其他与原始工具结果无关的样式。

视觉参考真源为 `design/sidebar-demos/subagent-tool-summary.css` 的结果区域规则，生产实现在 `frontend/app/styles/workbench.css` 等原组件样式落点完成。不移植 Demo iframe、构建期 JSX 替换、模拟数据或 UI bridge。

## 17. 前端精确范围：任务完全替换、后台终端新增

### 17.1 Demo 采用白名单

| Demo 部分 | PR03 决定 |
| --- | --- |
| 侧栏任务页：组卡片、组图弹窗、节点对话 | 采用，完整替换生产任务 tab 的旧概览/展开 TaskCard 布局 |
| 侧栏后台终端页：折叠命令行与输出 | 采用，作为新增 tab；真实异常反馈遵守本规格，不照搬简化夹具 |
| create_agent_tasks 展开后的原始结果区域 | 只参考第 16 节艺术风格，推广到其他工具原始结果 |
| 能力页、主对话/头像/输入框、创建工具半详细摘要及跳转、左栏、设置、页面壳、其他 Demo 内容 | 一律不参考，保留生产实现 |

顶部顺序「能力／任务／后台终端」，初始仍选中任务；共享导航只作新增第三页所需调整。保留生产能力页所有 Skills/MCP 内容、来源、权限、导入、配置、连接、加载与错误反馈，不能用 Demo 静态卡片替换。

主对话保留现有消息、SubagentGroup、工具 trace、完成事件、TODO dock 和输入框结构；不因 Demo 没有某部分就删除生产功能。图内节点详情可复用现有真实 activity/Markdown/tool result renderer，但不把 Demo 的仿制对话搬到 root。既有 root 的 task locate 仍可指向其原位置，PR03 不另加“工具摘要跳侧栏”的入口。

### 17.2 任务组、依赖图与节点详情

- 任务 tab 只排列组级卡片，显示真实组名投影、状态计数与紧凑进度；组内节点隐藏。生产旧概览、逐个展开卡片的任务页布局同次删除，不提供新旧切换。
- 点击组打开图形弹窗；多节点组初始不选节点、不显示右侧占位。选节点显示右侧只读对话；空白点击/收起清除选择、隐藏详情。
- 单个 spawn_agent 使用同一真实 batch/task 语义，正常大小的单节点居中，打开时选中；不伪造 ROOT 或依赖边。批次与 node 名称/聚合规则由第 18.5 节给定，不让模型为 UI 生成新标题。
- 保留缩放、适应、Esc 关闭、焦点返回、键盘可达和窄屏可用性；图不是编辑器，不允许拖线改依赖。选中节点的直接入/出边沿依赖方向流动；首次打开右详情时滑入，切换节点不重复入场。进行中图标为灰色进度环，尊重减少动态效果设置。
- 取消按钮作用于选中的确切 task。ACTIVE、PENDING、等待依赖的非终态任务在 controller 下可以取消；只有完整结算后才显示已取消。终态/observer/失效 owner 不可取消。等待依赖的节点不伪造模型对话。
- 节点对话使用真实 objective、activities、工具记录及结果，不开放用户给子任务的输入框。加载、部分内容、读取失败分别显示，不能仅呈现 preview 并称完整。
- 有取消影响时按第 9.3 节展示真实下游影响和关联命令，不自动遍历并向下游或进程发取消。正常态不堆叠“点击节点”“箭头方向”等说明。

现有“用这份结果继续／让 Pulsara 处理这个问题”移动到**组弹窗的节点详情底部结果操作区**，不是子任务输入框：仅 controller、主助手 idle、该任务已终态且 completion 未接纳时可用。复用原 accept_subagent_completion、权限提示、command 与 completion inbox；它启动 ROOT 处理结果，不重跑子任务。原入口位置删除，能力和历史完成事件保留。终态节点没有取消按钮，因此两个动作不混同。

### 17.3 后台终端

- 一条成功后台交付的 process 一行，包括 0 monitor 的进程。实际命令为标题，默认折叠，点击 toggle 展开/收起，输出只读，无输入框；行头放状态与方形终止按钮。
- 正常态删除重复命令、内部 ID、组号、“只读／保留输出／不影响主助手本轮运行”等常驻说明。命令来源、cwd、退出信息与反馈目标放在行头的信息按钮打开的紧凑详情内；不强制增加确认反馈目标弹窗。
- 部分失败、结果未知、输出 gap、owner 不可用必须在对应行持续可见，不只藏 hover/toast；原操作可查询时优先查询，已知失败且目标可控时才提供新 command 的“重试终止”。
- 终止按第 7、14 节执行，关闭 monitor 与杀进程由 Host 组合；不停止 ROOT，不回滚，不默认终止关联子任务。
- retained 期间保持终态行和输出；prune 后允许消失并统一提示保留范围。关闭视图、折叠、切 tab 只停止读取，不控制后端对象。
- observer 在原 session read entitlement 下查看相同受限日志，无控制按钮。跨会话、旧 Host、失效 cursor 不能恢复旧资源。

### 17.4 前端实现边界

实际图布局使用 production React 组件和已安装组件/图标；布局库的选用遵守 AGENTS 第 7 条，不复制 Demo 固定坐标或另建图执行状态。工程选型不能改变本节交互、分页、身份或取消合同；不以窄屏/性能为由加入历史/任务总量 cap。

原始结果只改第 16 节风格；除工具复制/分页既有行为回归外，Demo 不为 root 消息或控制提供新的实现真源。保留 `frontend/components/inspector-panel.tsx` 的能力组件，拆出的 task/terminal 子组件由真实 adapter 数据驱动。所有异步读取/控制结果都绑定原 session/Host/attachment/目标，组件卸载后停止显示与轮询，不取消已接纳的物理操作。

## 18. 已定案的反馈、读取与数据投影

### 18.1 唯一事件增量与 typed 内容

| 类别 | PR03 增量 | 唯一职责 |
| --- | --- | --- |
| canonical EntryKind | +1 USER_CONTROL_FEEDBACK | 保存人工后台命令控制事实 |
| committed event | +1 UserControlFeedbackAccepted | 该反馈 entry 已在同一事务接纳 |
| CanonicalInputOriginKind | +1 USER_CONTROL_FEEDBACK | 与 HUMAN_MESSAGE/HUMAN_STEER 区分来源 |
| ProviderInputItemKind / FrozenProviderInputItemKind | +0 | 复用 USER + 新 origin，不增加 wire item kind |
| provider API role | +0 | 仍映射为 user |
| 表、产品关系、subject、append guard、durable job、live event kind | 全部 +0 | 复用 entries、内容存储、ENTRY subject、HOST_WRITER 及现有投影 |
| 控制结果协议枚举 | 第 14 节列明的两个 CommandKind 与一个 FAILED status | 命令请求/结果表示，不是数据库执行状态 |

禁止新增 ControlRequested/ProcessKilled/FeedbackIncluded 等 committed/live 事件、投递队列、receipt、ack 表或恢复 scheduler。protocol committed enum 与 canonical event 映射同步增加同一个事件，不算第二种语义事件。若实施需要其他类别增量，必须先修订此表并说明独立理由，不能自动扩张。

在 `ports/user_control_feedback.py` 定义封闭 `UserControlFeedbackContentV1`，字段：

- `schema_version = user_control_feedback.v1`、`source = USER_CONTROL`；
- 原 `command_id`、`session_id`、`host_session_id`、`process_id`、实际命令与 cwd；
- `origin_turn_id`、可选 `origin_subagent_task_id`、`target_root_turn_id`；
- 第 14.2 节实际 process/monitor 分项结果，`new_control_attempt = true` 与真实 public detail；
- `other_work_stopped = false`。

`new_control_attempt` 只表示本次确实新推进过终止或关闭监视的尝试，包括尝试失败；不证明 OS 副作用成功。已终态且没有新的监视关闭/终止尝试才属于 no-op，不生成内容；不能因为一次真正尝试失败、未改变进程状态，就把它当成无需反馈。

它不携带监视输出覆盖、假 monitor ID、完整日志、反馈三轴的可变进度、重复内容摘要/指纹或新的权限凭证。不接受浏览器提供的反馈正文；内容仅由 Host 用 owner 结果构造。媒体类型固定为 `application/vnd.pulsara.user-control-feedback+json`、UTF-8。资源大小使用既有 canonical admission/content storage 边界，不能截断后伪装完整 typed 内容或另加随意文本上限。

Entry id 在 Host 原 control attempt 中生成一次 UUID 并冻结；accepted event 使用既有 ENTRY 事件 ID builder，不另算 request fingerprint。现有 InlineContent/BlobContent 在真实内容存储边界的 digest 按原规则保留与复用，不新增平行 content_digest 字段或 membership SHA。command_id 用作事件/内容的来源字段，不插入普通 durable command 记录充当控制 receipt。

### 18.2 事务、safe point 与未知提交

1. Host 接纳进程控制时，在原锁下固定活动 ROOT A 或 idle。idle 为 NOT_REQUIRED + NOT_APPLICABLE，不创建反馈 entry、event、候选或新 ROOT。反馈目标不采用前端猜测。
2. 得到本次终止尝试的最终分项结果后才冻结一条内容；无新尝试/监视变化的 no-op 不新增反馈。全部或部分失败都是真实控制结果，不因失败而伪造成功或省略反馈。此后物理 owner 继续收尾不追写/重写已接纳反馈；模型可主动查询。
3. Host 在现有 safe point 调用新增的窄方法 `install_user_control_feedback(attempt)`；复用同一 safe-point lock、compaction write reservation 和确认纪律，不调用 monitor.freeze、不新增轮询 runner。活动 ROOT 正常结束时复用 runner 已有 completion settlement 形状：Host 锁内先 seal 反馈接纳并读取 attempts，assistant settlement 再按 exact `turn_completed` 结算该 seal；一次 fence 保持 turn 打开，并在下一次 provider preparation 前等待同一 candidate 的 canonical 结论。seal 后到达的控制不能绑定即将结束的 A，settlement 未完成 A 才重新开放。
4. repository `accept_user_control_feedback` 复用 `_writer_transaction`、`_require_provider_safe_turn_in_transaction`、entry/content writer 与 `_append_events`。同一事务校验 writer、session、ROOT A 仍 RUNNING 且可安全接纳，再插入 entry 和唯一 accepted event；不创建 turn、queue、关系或 delivery row。
5. 主循环流式/工具执行期间只能等待原合法边界，不能修改在途 request 或 tool call/result 配对；已冻结 compaction successor 不改写。正常结束 fence 只等待到该 attempt 首次取得的既有 foreground-canonical deadline，且每次判断任一 PENDING/ACCEPTED 协调分支前都执行该时间截止；物理结果在期限内尚未形成、用户 STOP、异常或 owner closing 时允许退出，不能无限延长 ROOT，也不能增加调用次数上限。Host 关闭/ROOT 停止仍须结清已开始提交候选的精确确认。
6. 候选保存精确 entry/event ID 与完整 typed 值。新增 `confirm_user_control_feedback_winner` 复用原 exact confirmation 路径：检查同一 entry、event、target 与实际内容；数据库异常不证明回滚。没有 winner 且仍合法时只重试同一候选；winner 冲突拒绝，不补写第二条；owner 失去确认能力时 UNKNOWN。提交调用已发起而 ACK/确认异常后，即使 A 已结束也先对原候选做只读 exact-confirm；只有确认无 winner 才可由 TARGET_CLOSED 得出 FAILED。
7. 最终 provider-input install 与 transport invocation 是可能先于 Host 本地 ACCEPTED/INCLUDED 更新到达的独立观察。Host 在原 process-local attempt 中按 exact candidate/entry 与 `{session_id, turn_id, context_binding_revision_id, model_call_index}` 暂存和协调；canonical ACK 后一次性合并，不能用单向状态前置条件丢弃真实观察，也不新增 durable receipt。Host closing 时 exact-confirm 得到 winner，仍先以 ACCEPTED + PENDING 合并这些已保存观察，再仅将缺少证据的 inclusion/transport 维度按 owner 丢失边界表达为 UNKNOWN；不能先写 UNKNOWN 使 reconciliation 不可达。
8. A 在任何写入调用前结束：确定未写为 FAILED(TARGET_CLOSED) + NOT_APPLICABLE。已发起写入但结果未知时按第 6 点确认；已写后结束则保留历史，并按真实请求证据纳入/未纳入/UNKNOWN。不能改投 B、fallback NewTurnInstallation 或扫描 UI 控制记录为下一人工轮次补投。
9. 该 entry 进入现有 canonical transcript/event 投影，标注“用户控制反馈”，不是用户手输消息。fork、reload、history 与合法 compaction 复用现有来源归属规则；复制的历史 host/process/command 身份只表示原事实，绝不授予子会话控制原对象的权限。

typed reason 按边界使用固定集合：IDLE_AT_ADMISSION、NO_NEW_CONTROL_EFFECT、TARGET_CLOSED、CANONICAL_REJECTED、CANONICAL_COMMIT_UNKNOWN、INPUT_COMPILE_FAILED、INPUT_ADMISSION_FAILED、OWNER_LOST、RESULT_RETIRED。原 underlying code 可附加为 detail，不能用未知原因兜底推定物理失败。

### 18.3 reader/lowering 与请求纳入证据

reader 的内容加载名单、历史读、fork 来源处理和 typed 校验显式支持新 EntryKind；分支在普通 HUMAN_MESSAGE/USER_STEER 处理之前，用完整 typed 内容生成确定的事实文本，`ProviderInputItemKind.USER` + `CanonicalInputOriginKind.USER_CONTROL_FEEDBACK`。FrozenProviderInputItem 继续 USER，并保留准确 source_entry_id/sequence/turn。不触发仅人类 prompt/steer 拥有的 Hook、权限变更、能力激活或新轮次 admission。

事实文本表达“用户控制了哪个命令、实际结果如何、其他工作未停止”，不是重启命令或接手旧任务的新授权。编译器按原 budget、floor/scope 和合法 compaction 规则处理；不能为了保证读到该条而重建 provider 根、强制抢占或无限重试。

canonical storage 中的 `schema_version` 只用于闭合解码和版本校验，不属于模型理解控制事实所需的内容，provider 投影必须将其删除。同一减法规则适用于 Pulsara 自有的 terminal observation、子任务完成、tool-result closure 与 late-outcome 载体；它不允许改写 MCP/未知工具返回的原始正文，外部工具自己返回的 `schema_version` 仍按原文保真。该 provider-message lowering 变化必须提升现有 lowering/compiler contract，使运行中的旧 epoch 通过既有 `PROVIDER_LOWERING_CHANGED` 冷边界切换，不能在同一 epoch 内重写已安装前缀，也不新增兼容双投影或 feature flag。

确定的观察点：

- runner 取得当前最终 `InstalledProviderOpen` 后，将确切 compiled_input 的 `message_placements.origin_entry_id`、并列 messages 与该反馈的确定文本/来源作完整比较；匹配且属于绑定 ROOT A，才记录 INCLUDED。观察覆盖 runner 当场执行 `provider_dispatch.install_provider_open` 的普通路径，以及 compaction coordinator 已安装并随 successor dispatch 交给 runner 的路径；同一最终 open 只观察一次。不读取/新增 placement fingerprint 来证明此事实。
- 只看最终安装的选择，不看试算候选、sequence 上界、摘要相似文本或被丢弃的 cut。当前候选没有完整来源映射时不标 INCLUDED；按原 owner 后续推进，目标结束/确定失败时给出对应结论。
- 记录既有身份 `{session_id, turn_id, context_binding_revision_id, model_call_index}`、原 execution 引用及反馈 entry_id；只检查当前尚待纳入的 control attempts，不建立所有历史 entry 的 membership registry。
- `direct_model.PreparedKernelModelExecution.open_once` 在实际调用 `transport.open_stream` 的准确位置报告本地 invocation（调用同步抛错也报告已尝试并保留失败）。它不能等同 CAS 或函数进入，也不声称 provider 服务端收讫。
- 请求发起只读投影为 NOT_APPLICABLE / NOT_STARTED / INVOKED / UNKNOWN；execution 已知失败另携其原失败信息。它复用原 execution 生命周期，不新增持久状态机。纳入前无需展示该字段，已 INCLUDED 后只有真实边界才能从 NOT_STARTED 变为 INVOKED。
- 观察信息通过原 execution/Host process-local 回调传递，不新增 live event；控制 query 读取事实。回调与 canonical ACK/Host state update 不保证单向顺序：先到的 exact install/transport 观察按第 18.2 节保留并在条件齐备后合并。已知 ACCEPTED/INCLUDED 不因后续流失败降级；新查询缺少证据则如实 UNKNOWN，不从 canonical entry 或 through_sequence 反推原请求。

UNKNOWN 本身不触发额外 provider call；第 8.2 节为正常结束已明确 seal 的一次后续请求不属于 UNKNOWN 补投。三轴、请求发起与控制物理结果依第 8、14 节分别展示，不能统一为“模型已收到”。

### 18.4 成功后台交付与日志读取

`_ProcessState.background_adopted` 默认 false，唯一写入 owner 是 ProcessRegistry 的原 foreground decision。改造正常 `_mark_foreground_decision_result_ready` 路径：在同一原 decision 临界区确认尚未 ABORT_REQUESTED、adoption_allowed、正常 yield 决策为 true，发布 RESULT_READY 时同时设置 true。abort 先赢则走原 abort/物理清理，不被迟到正常结果反向发布为成功交付。

- 短命令正常完成、abort、异常回滚/隔离均不置 true；不能在创建 process ID、观察 yield_decision=True 或工具 JSON 出现 running 时设置。
- 成功发布之后自然结束、终止、工具结果 waiter 被取消不撤销曾经交付的事实；物理 owner 继续负责，canonical 工具结果仍按原结算。
- TerminalProcessInfo 补只读该字段和既有 origin、stream_id、io_mode、physical_state。只在受限读取 DTO 中按需公开，不机械扩增模型工具 schema 或新建数据库列。

在现有 authenticated observer attachment 上增加两类只读协议请求，经 browser_bridge → gateway → Host → TerminalSessionManager/ProcessRegistry：

| 请求 | 参数 | 返回 |
| --- | --- | --- |
| list_background_processes | expected_session_id、expected_host_session_id、cursor、maximum_items | 后台 retained processes、各项原身份/状态/origin、next_cursor |
| read_background_process_log | 同一 owner、process_id、原 output cursor、max_output_chars | TerminalProcessLog 原字段、stream/physical 状态、gap/retained_from/next cursor |

列表只选 background_adopted=true 的 retained 进程，包括已结束；不扫描系统、不按 monitor inventory 建表。按 started_at_monotonic/process_id 稳定排序，使用这一排序值和 exact Host 的无状态分页 cursor；沿用现有 task page 每页 50 的 UI/API 资源尺度，不加总进程/历史数上限。进程列表在读之间可变化，不声称跨页物理事务快照。

日志直接调用原 log/output owner，沿用 `TerminalProcessArgs.max_output_chars` 的校验边界、sanitizer、cursor/gap 和截断语义；不从文件/工具正文恢复被裁剪输出。首次展开取当前保留尾部，之后沿原 cursor 增量读取；不创建模型 monitor。

首版固定使用**可见时串行拉取**，不增加 server subscription：后台 tab 可见时每次请求完成后间隔 1 秒刷新列表；展开的日志每次读取完成后间隔 1 秒增量读取。control attempt 的进行中查询使用相同节奏。每个目标最多一个在途读取，1 秒仅是 UI 刷新节奏，不是执行/重试/历史上限；网络断线交给既有重连 owner，恢复后 exact-join 原目标，不重发控制。

control 查询是否继续不能只看 CommandStatus：物理操作已完成，但 canonical/请求纳入仍 PENDING 或对应 execution 尚在推进 transport 发起时，仍查询原 attempt。三个控制的进度在实际展示它的视图可见时刷新；仅查看历史终局结果不保持轮询。读取暂停不终止 Host 的反馈接纳/确认工作，重新可见后先查询原身份。

折叠、隐藏页面、切 tab/session、卸载即停止后续拉取并取消 waiter；现有 I/O 结果仍正常释放，不取消 process 或已接纳 control attempt。原 output 临时读取持有关系在 finally 释放，不新增长期订阅，不让视图阻止 prune。轮询响应携带查询时事实；从列表某一页缺失不能推断已 prune，详情须按精确 ID 确认。

### 18.5 任务分组、完整节点活动与 completion 接纳

当前 `LocalWebSessionController.list_session_tasks` 和 repository 的 task/dependency rows 是真源，不能只用 canonical control 的短窗口或 root message.subagentRuns 来构造全部组。

新增受同一 session read entitlement 保护的 `GET /api/sessions/{session_id}/task-groups`；它是对原 task/batch rows 的分页 GROUP BY 投影，不建表。返回 group_id（原 batch_id）、parent_turn_id、first_accepted_at、task_count、各真实状态计数、single_task_label、next_cursor。按 first_accepted_at/batch_id 排序；分页复用现有 cursor 编解码和每页 50 边界，group/task/activities cursor 各自明确 kind，不互换。只读历史不隐式创建 Host。

生产单/批量创建都有真实 batch 身份；正常读路径必须使用 batch_id，不按 parent_turn_id、label 或正文合并不同批次。batch_id 缺失应返回明确的 TASK_BATCH_DATA_INCOMPLETE 读取错误并保留错误目标信息，不按 parent/label 自动补组，也不恢复旧 UI fallback；开发数据按 clean-v0 处理，不能静默隐藏缺失项。

组标题确定为：单任务使用 `label || task_key || 子任务`；多任务显示“子任务组”与其创建时间。不存在独立 canonical 组名称，不把 Demo 的“控制入口与运行面板”当作后端字段或请求模型补命名。进度逐状态聚合；包含失败/依赖失败、取消、活动和等待的混合态如实呈现，不压成全部成功。

扩展现有 `GET /api/sessions/{session_id}/tasks` 支持精确 batch_id 过滤；返回该组 task/dependency 页，next_cursor 绑定同一 filter。组内加载后续页直至 next_cursor=null 才可称完整；用户可停止读取，图显示“已加载 N/M”而非虚构全部。跨批次依赖只读展示为真实外部引用，可定位其所属组，不虚构为本组节点。

新增 `GET /api/sessions/{session_id}/tasks/{task_id}/activities`：从现有 task/turn/entries/blocks/inter-agent rows 按 exact session + SUBAGENT_TASK + task_id 读规范活动页，按 entry_sequence/block ordinal 排序；返回 objective、规范 activities 元数据、原 entry/block/result 身份与 next_cursor。正文复用 read_content/read_tool_artifact 的分页与授权，不另拷贝 transcript 表；读取不得为“完整”绕过现有最大响应/资源接纳边界。

adapter 将该任务的 canonical activities 与原 live attempt/channel 身份 join，复用 PR02 的精确结果关联，不能使用“最后一个 trace”、正文去重或按组名匹配。reload、页外任务、完成后和 Host 不可用时，按实际 canonical/retained 能力显示完整、部分或不可用；没有观察证据时不伪造运行中状态。

完成接纳的位置和门槛按第 17.2 节固定：节点详情结果区调用原 ACCEPT_SUBAGENT_COMPLETION；保留失败/取消结果的可处理性、原权限、新 ROOT admission、queue-only completion 与已接纳历史。它不是恢复子任务、后台 monitor 唤醒或新的 UI 控制反馈事件。

### 18.6 clean-v0、oracle 与 continuity

同步修改 `storage/migrations/sql/0000_conversation_kernel_baseline.sql` 中 EntryKind/check/事件登记的闭合集合，沿用 existing entries/内容/agent_events，无新增表、关系、product column 或在线迁移链。保留原事务的 writer guard、subject 和原 schema oracle；仅按第 18.1 节调整精确许可计数/名单，不放宽为“任意事件都通过”。

Protocol proto、generated Python、gateway schema identity、wire fixture、canonical_v3、TS unions/normalizers、reader/compiler/fork/compaction 与 UI entry projection 同次更新。继续用现有协议生成机制；不新建协议兼容协商、旧枚举 fallback 或每文件 SHA。

验证新事件只追加 suffix，旧 epoch 的 SYSTEM/tools 与已安装 messages prefix 不变；单条新反馈进入现有合法 source 读取，不能逐次触发冷 epoch 作为投递捷径。第 18.3 节一次性的 provider-schema subtraction 属于明确的 lowering 合同变化，必须由现有 `PROVIDER_LOWERING_CHANGED` 边界开启新冷 epoch；切换后仍只允许 suffix append。fork/compaction 保留真实来源，不给复制的历史控制新的执行权；未知确认不能用新 ROOT、重播或重新写 entry 修复。

## 19. 删除清单与验证执行

### 19.1 同次删除/替换

| 删除的旧路径 | 保留/替代的唯一生产路径 |
| --- | --- |
| 浏览器无参数 stopActiveTurn、gateway 拒绝 target 的 STOP 规则、CLI public 无目标 stop_current_turn | 第 14 节冻结身份的 exact 控制；内部取消/结算 helper 不删除 |
| gateway 等待 ROOT 完成后临时拼装一个不可查询 STOP 结果 | Host 持有的 attempt、快速接纳与原 command 查询 |
| STOP 未知网络结果统称失败并提示盲重试 | 原身份查询、未知/退役/owner 失效明确区分 |
| ProcessRegistry 各处无条件写 killed/timed_out、各渠道重新计算并改写已确认终态 | 单一短锁仲裁与冻结终态；原信号/join/output owners |
| _stop 内嵌子任务取消流程 | 模型/人工调用共享 typed cancel_task |
| 生产任务 tab 的旧概览、逐项展开 TaskCard 和其旧位置的结果继续入口 | 组列表、节点图/详情及新的结果操作位置；能力组件、root SubagentGroup、completion语义保留 |
| 原始结果及其配套标签/按钮的固定黑底浅字样式 | 现有主题 token 的第 16 节风格 |
| 新代码中由工具 JSON/running 文案或 monitor inventory 猜后台资格的路径 | 唯一 background_adopted 与受限 process reads |

不得新增 feature flag、旧协议兼容、无目标 fallback、双事件写入、model-tool 伪装的用户控制、trace 文本扫描、DTO fingerprint、控制历史永久墓碑或新调度器。Demo 构建适配只留在 design 参考目录，不进入 production。PR02 的结果原文、精确 live join、队列和分页路径不能退回旧实现。

### 19.2 确定性测试矩阵

下列 PR03 专用测试/脚本已在本 hard cut 中创建：四个 tests/test_pr03_*.py、tools/run_pr03_user_control_dogfood.py，以及 frontend/components 下的 task-workspace、background-terminal-panel、inspector-panel、workbench-view 四个 .test.tsx 文件；其余落点复用既有测试。每行对应第 13 节场景；实现过程先以失败测试证明旧实现不满足断言，再修成绿。

| 编号 | 覆盖重点 | 测试落点 |
| --- | --- | --- |
| T01 | STOP A 在途，A 完成/B 从 prompt queue、plan successor、monitor 分别启动；B 均不被取消；缺 target 拒绝；Host closing 及 preflight→closing 交错不接纳新控制 | tests/test_pr03_user_controls.py、test_stage2_protocol_v3.py、test_round4_plan_host.py、test_round2_terminal_host.py |
| T02 | 重复相同 command、不同参数冲突、两个新请求同目标、CLI exact stop；auth/controller/old Host | test_pr03_user_controls.py、test_local_web_browser_bridge.py、test_local_web_http_surface.py |
| T03 | ROOT/子任务启动效果与取消交错、preflight 后自然完成的最终 disposition、前台 terminal abort vs 已交付后台、wait 取消不等于 kill | test_pr03_user_controls.py、test_stage2_kernel_io.py、test_stage2_conversation_runner.py、test_stage2_subagent_close.py |
| T04 | 自然 exit 0/nonzero 后晚 kill、timeout/cleanup/rollback 交错、shell 已退但子进程活、reader/watcher 未 join；无新 intent/无信号时不得报 TERMINATION_COMPLETED | tests/test_pr03_process_termination.py、test_round2_terminal_environment.py |
| T05 | list/log/poll/output finalize/完成回调冻结同一终态；signal/join 不持 Host/state 锁，不阻塞 event loop | test_pr03_process_termination.py、test_stage2_terminal_host_lifetime.py |
| T06 | 0/1 monitor、DORMANT 取消后工具结算、旧 in-flight、取消失败、部分 kill 失败、全终态 no-op/仅首次关闭监视 | test_pr03_user_controls.py、test_round2_terminal_monitor.py |
| T07 | 原 command ACK 丢失/重连、期限内重复、提交过期、进行中超过窗口、完成保留期、退役后 query/submit、新主动重试 | test_pr03_user_controls.py（可控单调时钟，断言控制次数/反馈条数） |
| T08 | 活动 ROOT 的一次反馈、正常结束 completion seal/assistant settlement/下一请求 barrier、PENDING 与 ACCEPTED 全分支 watchdog 退出、seal 后控制不再绑定 A、旧轮次命令、idle UI-only、A→B 切换、provider streaming/tool/compaction safe point | tests/test_pr03_control_feedback.py、test_pr03_user_controls.py、test_stage2_conversation_runner.py、test_round2_terminal_safe_point.py、test_round2_terminal_host.py |
| T09 | entry+唯一 event 同事务、unknown commit 在 ROOT 结束后仍精确确认不重复、拒绝/UNKNOWN、owner close；历史/重载/fork/compaction | test_pr03_control_feedback.py、test_stage2_canonical_reader.py、test_stage2_conversation_kernel_postgres.py |
| T10 | 候选/sequence/floor 不能证明 INCLUDED；最终 placements、完整内容、CAS vs transport-open、普通与预装 compaction successor 均观察、观察先于 Host state/closing exact-confirm 的合并、失败或 owner 丢失后已知事实不降级 | test_pr03_control_feedback.py、test_stage2_conversation_runner.py、test_stage2_direct_model.py、test_round3_1_provider_input_prefix_continuity.py |
| T11 | background_adopted 正常交付/abort/rollback/RESULT_READY 交错，retained/prune、分页与全部状态 | tests/test_pr03_background_reads.py、test_round2_terminal_output.py |
| T12 | observer 相同授权日志范围、越权控制/读取、旧 cursor/Host、读取取消/轮询关闭不杀进程 | test_pr03_background_reads.py、test_local_web_browser_bridge.py、test_local_web_http_surface.py |
| T13 | task组分页、页外/跨组依赖、真实标题/计数、单节点、节点完整活动/live exact binding、结果继续不重跑子任务 | test_pr03_background_reads.py、test_round10_hierarchical_subagent_orchestration.py、frontend/components/task-workspace.test.tsx |
| T14 | click-time target、未知不自动重发、跨 session success/error/finally、部分失败持续显示、灰色图标/键盘与无输入框 | frontend/app/pulsara-app.test.tsx、frontend/lib/runtime-adapter.test.ts、frontend/components/background-terminal-panel.test.tsx |
| T15 | 生产能力页不变、root消息/公式/输入框结构不被Demo替换、原始结果逐字复制/diff/artifact分页/两主题 | frontend/components/inspector-panel.test.tsx、frontend/components/workbench-view.test.tsx、现有 Markdown/公式测试 |
| T16 | 仅许可类别增量、无新表/关系/subject/guard/job/额外live事件或fingerprint、协议生成一致 | test_stage2_architecture.py、test_stage3_5_architecture.py、test_stage5_clean_migration.py、test_fingerprint_subtraction_architecture.py |

新测试可与同一 owner 的既有测试合并，但上述编号及断言必须在证据中逐项可定位，不能仅靠大批 tests passed 覆盖矩阵。不允许以 mocked UI state 代替物理进程或 canonical 事务测试；也不把可控注入伪称自然浏览器复现。

### 19.3 必跑命令

在仓库根目录，使用仓库 uv/.venv。新目标文件创建后执行：

~~~sh
.venv/bin/pytest tests/test_pr03_user_controls.py tests/test_pr03_process_termination.py tests/test_pr03_control_feedback.py tests/test_pr03_background_reads.py -q
.venv/bin/pytest tests/test_stage2_protocol_v3.py tests/test_local_web_browser_bridge.py tests/test_local_web_http_surface.py tests/test_round2_terminal_host.py tests/test_round2_terminal_monitor.py tests/test_round2_terminal_safe_point.py tests/test_round4_plan_host.py tests/test_round10_hierarchical_subagent_orchestration.py -q
.venv/bin/pytest tests/test_stage2_kernel_io.py tests/test_stage2_conversation_runner.py tests/test_stage2_subagent_close.py tests/test_stage2_terminal_host_lifetime.py tests/test_stage2_canonical_reader.py tests/test_stage2_direct_model.py tests/test_round3_1_provider_input_prefix_continuity.py tests/test_fingerprint_subtraction_architecture.py -q
.venv/bin/python tools/generate_terminal_protocol_contract.py --check
uv run ruff check .
.venv/bin/pytest -q
git diff --check
~~~

在 frontend 目录：

~~~sh
npm test -- lib/runtime-adapter.test.ts app/pulsara-app.test.tsx components/task-workspace.test.tsx components/background-terminal-panel.test.tsx components/inspector-panel.test.tsx components/workbench-view.test.tsx
npm test
npm run lint
./node_modules/.bin/tsc --noEmit --incremental false
npm run build:local
~~~

协议改动使用现有 generator/protoc 同步 generated binding、schema identity、wire fixture，不手工造生成结果。Ruff 使用现有项目配置，不安装第二套 Python 环境。需数据库的 fixture 先读 tests/support/postgres.py；clean-v0 reset 只作用于确认过的本地可丢弃目标，不误操作远程数据库或改保存设置。

小型既有 fixture/typecheck 失配可按当前生产合同修缮，说明为何与本 PR 无关；较大无关失败可以停止扩修并如实报告，但不得 skip/xfail、弱化断言或在全回归未通过时 ACTIVATED。旧报告中的已知失败不会自动成为豁免。

### 19.4 isolated wheel / launcher

1. 完成最终 build:local，记录实际打包的静态入口。用 mktemp 创建并解析真实临时路径，执行 `uv build --wheel --out-dir <实际 wheel 输出目录>`。
2. 用 `uv venv <实际隔离环境目录>` 和 `uv pip install --python <该环境 Python> <实际 wheel>` 安装；参考已有 installer dogfood 的隔离安装步骤，不运行其无关插件场景或使用旧 env-file 凭据入口。
3. 从非源码 cwd 运行安装版 `pulsara --version`、核对 `pulsara_agent.__file__` 来自该 site-packages，按保存生产配置启动 `pulsara app --port <空闲本机端口> --no-open`；不借 PYTHONPATH 或 editable install 偷用源码。
4. 安装版真实浏览器验证任务/后台控制与工具原始结果，核对实际静态资源来自 wheel；只验证版本号不算 launcher 验收。
5. 避免源码与 wheel 同时争用同一 Host。保存设置只读；临时环境不覆盖全局 launcher。最后改动若影响 bundle/协议/反馈，重建 wheel 并复验对应行为。

尖括号参数必须替换为执行时已核实的真实路径/端口，不能把本节当作已执行日志。

### 19.5 新功能 real-provider dogfood

本 PR 新建 `tools/run_pr03_user_control_dogfood.py`，复用当前 Host/保存设置和已有 dogfood owner；接口固定为 `--connection-id <保存的连接 ID> --output-dir <实际证据目录>`。不得内置 provider 密钥、强制 .env、默认改生产配置或重用 PR02 证据冒充 PR03 执行。

保存配置经 LocalSettingsStore + require_pulsara_home 只读加载，优先现有可用 OpenRouter；缺项如实报告。探针仅在明确目录内运行可控本地命令，真实模型不能被引导杀任意进程。脚本与浏览器共同覆盖：

- ROOT A 收尾、B 接纳后迟到 STOP A；有可控延迟注入，另保留真实 provider 交互。明确断言 B 的身份与继续执行，不能靠第三方中转慢来碰时序。
- 多子任务依赖组和单 spawn_agent：取消活动/等待任务、查看其真实对话；无关任务继续，结果接纳入口启动 ROOT 而非重跑子任务。
- 真实后台进程无 monitor/有 monitor；展开日志，用户终止；活动 ROOT 得到一次真实反馈并继续，idle 不新增 feedback entry/新 ROOT，也不自动带入下一人工轮次。
- 自然结束后的晚到终止、已关闭 monitor 的部分失败、原 command 查询与新主动重试；故障注入与自然成功证据分别标注。
- reload、断线重连、第二窗口 observer、旧 Host 不可控、prune 和日志 gap；视图关闭不会杀命令。
- 比较 typed feedback、canonical entry/event、最终 request placements/实际 provider-visible 文本及 transport 本地调用事实；不以模型回复一句“收到”代替证据。
- 实际生产能力页的核心查看/连接入口不退化；root 对话布局仍是生产，普通文件工具、终端工具和已有可用 MCP 的原始结果使用新风格，复制与 canonical 保真。

必须在真实浏览器实际点击并结合截图与 DOM/clipboard/协议/canonical 断言；桌面及窄屏、浅/深主题都核对可达性/可读性。不以静态 Demo 或 Playwright mocked screenshot 替代 production dogfood。MCP/未知工具的原始结果样式与保真必须有自动化 fixture 覆盖；保存配置已有可用 MCP 时另做真实检查，未配置时记录该条件分支不适用，不为配色验收私自安装服务。真实 provider、后台进程、任务与控制链均不是可选分支，必需门槛未满足仍不能激活。

证据放在 `output/playwright/pr03-dogfood/`，记录实际命令、测试结果、session/turn/task/process/command/entry 身份、关键输入输出与截图路径。实际凭据值必须排除，其余正文不泛化脱敏。不记录证据 SHA、文件 SHA 或工作树聚合 hash。

## 20. Activation acceptance criteria

以下全部通过才允许把本文改为 ACTIVATED，并同步索引中的 PR03/F06/M01 状态；任一未满足保持未激活，不能按阶段或“主要功能已好”提前激活。

- [x] F06 全链要求 exact target；STOP A 不误停 prompt/plan/monitor 来源的 B，重复/未知/CLI 路径均不回退无目标。
- [x] M01 三类操作与自动续轮边界在代码/文案一致；无全会话暂停、无未授权级联、无虚假“全部停止”。
- [x] ROOT/子任务/物理进程保持原 cancellation、permission、Hook 和 started-effect settlement；自然终态不可被任一晚到原因改写；preflight 后自然完成与无新终止 intent 的收尾竞态已按新合同重验。
- [x] 三类控制快速接管、同身份查询、过期/退役、新主动重试、原 owner 失效均有精确测试；Host closing 前后不接纳新操作，既有 attempt 继续查询/确认/结算。
- [x] 活动/idle 反馈按冻结 ROOT 分流，唯一 entry+UserControlFeedbackAccepted 同事务；正常结束 completion seal 与 assistant settlement 对新控制接纳线性化，PENDING/ACCEPTED 各分支执行同一 watchdog，unknown commit 在 ROOT 结束或 Host closing 后的 exact confirmation 均通过。
- [x] 反馈三轴及最终请求/transport 本地证据准确；普通安装与预装 compaction successor 均观察最终 request，乱序观察可在 canonical ACK 或 closing exact-confirm 后按 exact request identity 合并，不以 through_sequence/CAS/模型文字确认宣称服务端收讫。
- [x] category/oracle 增量严格等于第 18.1 节；无新表、关系、subject/guard、durable job、额外 live/committed 事件、receipt 或 fingerprint。
- [x] background_adopted 唯一成功交付事实准确；列表/日志分页、retention、gap、授权和读取释放均闭合。
- [x] 任务 tab 完整替换为组/图/节点详情，单/批次统一，页外任务与活动可读；原结果继续入口已承接，无完成语义回归。
- [x] 后台终端新增 tab，正常态简洁，部分失败持续可见，控制只作用于确切 process。
- [x] Demo 使用严格止于第 17.1 节白名单；生产能力页、root消息/输入框/公式/左栏未被 Demo 替换，主对话只实施获准的原始结果风格。
- [x] 原文、复制、diff、artifact 完整读取和主题可读性通过；PR01/PR02 的原文/精确 live join/queue 合同无回退。
- [x] 第 19 节删除清单完成，无兼容 fallback/feature flag；所有新场景先红后绿且未弱化断言。
- [x] 本次 kernel 修订后的 focused、Python 全回归、ruff、protocol/clean-v0/oracle/continuity 均通过；Pulsara 自有 provider 载体不暴露内部 schema/version，外部工具正文仍保真。
- [ ] 前端不在本轮修改范围，仍需确认既有前端回归不受 wire 影响。
- [ ] 本次修订后的最终 isolated wheel/launcher 与 real-provider/browser 正常结束反馈链已重验；旧 activation 证据不能替代新增时序。
- [ ] 本次修订后的源码/产物受影响改动已重验；剩余物理/retention/process-local 边界如实报告，Git 状态与未提交文件范围明确。

历史 Activation 记录：2026-09-11 原合同曾完成 Phase A–D。后续 PostgreSQL 超远 deadline 边界加固计入回归后，Python 全量 1719 tests、前端全量 192 tests、focused、lint、typecheck、协议生成、ruff、clean-v0/oracle、continuity 与 `git diff --check` 全部通过；isolated wheel 从非源码目录启动并完成真实 provider、控制、任务、后台终端、observer、重启、浅/深主题、窄屏、内置文件工具、artifact 与保存 Firecrawl MCP 的真实浏览器验收。完整旧证据见 [`output/playwright/pr03-dogfood/activation-evidence.md`](output/playwright/pr03-dogfood/activation-evidence.md)。本轮新增 kernel 时序合同后，该记录只作为历史，不足以将上方新增门槛重新勾选。

2026-09-11 第一轮 kernel 重验：reviewer 六项时序缺口及复核发现的 accepted-feedback completion fence、Host-closing ambiguous commit confirmation、durable/no-live 子任务取消竞态均先红后绿；受影响 focused `251 passed`，Python 全量 `1732 passed, 19 warnings`，`ruff check .`、协议 generator `--check` 和 `git diff --check` 通过。

2026-09-11 第二轮 kernel 重验：新增四个 reviewer 交错均先由回归复现再修正，包括 ACCEPTED feedback 超过既有 deadline、completion fence 后新控制绑定、预装 compaction successor 漏观察、closing exact-confirm 丢弃早到 install/transport 事实。直接复现 `4 passed`，相邻 kernel/continuity focused `215 passed`，Python 全量 `1734 passed, 19 warnings`；本轮未修改前端，也未重跑前端、isolated wheel、真实 provider/browser，因此状态仍为 REACTIVATION PENDING。
