# Pulsara 用户停止、子任务取消与后台命令控制：最新版产品契约

状态：**产品讨论整理稿，尚未实施；后端接入设计仍需按第 14 节闭合。不是 ACTIVATED，也不是可跳过设计门槛直接实施的规格。**

日期：2026-09-10。代码调查基线：`b9adc8b8`（PR02 已提交）。

本文整合用户选中的早期“四种控制动作”方案，以及随后关于 monitor、后台进程展示、活动模型通知和 idle 唤醒的讨论。只冻结已讨论的产品方向，标明实现缺口与待决细节，不把建议写成已经存在的生产能力。

关联索引：[浏览器 dogfood 复盘与修复方案](PULSARA_BROWSER_DOGFOOD_REVIEW_AND_FIX_PLAN_2026-09-08.zh.md)。本文细化 F06/M01 和相应前端显示合同；不重新开启 PR02，不纳入 F04/M02/M04。

## 1. 最新结论

用户不必理解 `terminal`、`terminal_process`、`terminal_monitor` 三个模型工具的分工。

主要交互只有三个：

1. **停止本轮回复**：停止用户点击时所见的确切 ROOT turn。
2. **取消子任务**：取消图上选中的确切 task，由 kernel 传播依赖影响。
3. **终止后台命令**：关闭该进程关联的后续监视，并终止该进程组；一个用户动作，分别保留实际结果。

后台区域按 **process** 展示，不按 monitor 展示。没有 monitor 的后台进程同样可见、可管理；不为了显示而注册监视。

用户终止后台命令后的反馈分为两种：

- 主模型正在工作：在下一个允许接纳输入的位置，告知实际控制结果，让它继续并调整当前工作；不把这个动作变成停止主轮次。
- 主模型空闲：界面确认结果，不因本次用户终止额外启动模型轮次。

这不阻止其他后台活动的正常通知，也不撤回已经安装的观察或已经启动的轮次。查看、控制、向模型提供事实、启动新轮次是不同边界。

## 2. 早期方案哪些仍然成立，哪些已过时

下表同时整理旧建议与后续讨论澄清的理解；并非每项都是早期方案原文曾作出的承诺。

| 早期表述 | 最新判断 | 本文替代合同 |
| --- | --- | --- |
| 后端分别拥有 turn、task、process、monitor 的控制能力 | 保留 | 不建立通用 Activity 执行体系；UI 可以统一组织 |
| 前端提供四种动作，monitor 取消放进进程详情 | 已被后续简化取代 | 第一版三个主要入口；不向用户单独暴露 monitor 管理 |
| 终止命令由“取消关联 monitor + 终止 process”组成 | 保留并明确 | 人工组合操作，不是假装原子事务；不存在 monitor 时仍能终止进程 |
| 关闭 monitor 就不再告诉模型 | 过于笼统，应替换 | 停止持续监视，但活动模型需要一次真实的用户控制反馈 |
| 终止后不再自动唤醒 | 必须限定 | 只针对此次控制反馈和已关闭监视未来的投递；不是全会话静默 |
| 只展示 monitor | 已纠正 | 展示转入后台的命令，process 是身份，monitor 是可选附属能力 |
| 有 process_id 就表示仍在运行 | 不成立 | ID 是身份；可操作性取决于当前 owner 和实际生命周期 |
| terminal 的等待时间是命令运行期限 | 不成立 | `yield_time_ms` 是首次工具调用等待窗口，不是命令超时 |
| 注册 monitor 后主循环退出等待 | 不成立 | 注册按普通工具返回，模型随后可以继续工作，也可以正常结束当前轮次 |
| 运行中的程序不能接收输入 | 不成立 | 当前支持 stdin；只读界面是第一版产品选择 |
| 前端可以还原一个完整终端屏幕 | 当前数据不支持 | 第一版只读实时日志，不能还原已被过滤的 ANSI/OSC 或光标操作 |
| 子任务图、进程日志、权限与实际终态都由现有 owner 支撑 | 保留 | 图不新增执行权，视图状态不成为 canonical truth |
| 普通停止隐含全会话暂停，直到下一人工输入 | 未采纳 | 不引入全局暂停/恢复；若以后需要，另定 Host admission 合同 |

## 3. 当前代码真源与复用边界

以下路径从仓库根目录起算，行号仅供定位；Git 管理代码身份，不增加源码或文档 SHA。

| 真源 | 已有资产 | 本次设计不能假装已有的部分 |
| --- | --- | --- |
| `src/pulsara_agent/ports/terminal.py` | 三种工具的闭合 schema、参数和描述 | 人工后台命令组合控制 API |
| `conversation_kernel/runner.py:1319/1471/1543`，位于 `src/pulsara_agent/` 下 | 普通工具批次后继续采样；无工具回复按现有规则结算 | monitor 注册并无自动退出循环指令 |
| `src/pulsara_agent/conversation_kernel/host.py:4066` | ROOT cancellation intent、task join 和 settlement | 点击时 exact turn 校验；当前入口仍停止处理时的 active task |
| `src/pulsara_agent/terminal_protocol/v3_gateway.py:638` | controller gate、command envelope | 当前 STOP 仍拒绝 target_turn_id；无任务取消/进程组合终止入口 |
| `src/pulsara_agent/conversation_kernel/subagent.py:2233` | 排队、待依赖、活动任务取消，已终态返回 | 可供用户入口和模型工具共享的显式 typed cancellation 方法 |
| `src/pulsara_agent/conversation_kernel/_repository/subagents.py:2239` | 依赖失败传播与真实状态结算 | 前端不得另算一套执行级联 |
| `src/pulsara_agent/conversation_kernel/host.py:5428`；`web_app/session_controller.py:198` | session-scoped 任务分页及依赖读取 | 图布局不是新后端权威 |
| `src/pulsara_agent/terminal_process/manager.py:759/770/805` | process kill/list/log、owner fence、物理 join | 用户控制适配；不能直接开放任意系统 PID |
| `src/pulsara_agent/terminal_process/models.py:48/112` | process origin、io_mode、physical_state、输出 cursor | 现有 to_payload 未完整暴露所有侧栏需要的字段 |
| `src/pulsara_agent/terminal_process/manager.py:112/861` | process-local yield_decision | 需要明确“转入后台”读取投影，不能靠历史文本猜 |
| `src/pulsara_agent/terminal_process/output.py:156/358` | 输出处理、保留范围、cursor、gap、订阅 | 没有未过滤的完整终端屏幕流 |
| `src/pulsara_agent/terminal_process/monitor.py:423/488/794` | cancel、冻结投递的保留边界、关闭 live 状态 | 关闭 live 状态不等于模型收到一次取消反馈 |
| `src/pulsara_agent/conversation_kernel/host.py:3584` | 活动轮次 ExistingTurnInstallation、空闲 NewTurnInstallation | 不能直接把普通 monitor 唤醒策略用于人工取消反馈 |
| `src/pulsara_agent/conversation_kernel/safe_point.py:218` | provider-safe 安装、现有确认/结算边界 | closed monitor 不会凭空生成新的用户控制观察 |

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
- kill 使用现有 owner 终止受管理进程组，不回滚已发生效果。
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

## 5. 三个用户入口

| 入口 | 精确目标 | 主要结果 | 不包含 |
| --- | --- | --- | --- |
| 停止本轮回复 | 点击时所见 ROOT turn | 中断该轮，保留已开始效果的结算 | 停止其他 turn、清队列、取消所有子任务、杀所有进程 |
| 取消子任务 | 选中 task_id | 取消该任务；由 kernel 传播依赖失败 | 暂停/恢复、自动重试、默认杀关联进程 |
| 终止后台命令 | 选中 owner 下的 process_id | 取消关联监视并终止进程组，报告两项结果 | 回滚、停止主轮次、关闭整个会话、全局禁止自动接续 |

前端不提供独立 monitor 列表或取消按钮；monitor_id 仅用于后端精确关联，必要时可出现在诊断材料中。用户无需理解工具分工。

关闭侧栏、折叠日志、切换图视图只改变展示，不等于上述任何控制动作。

## 6. 后台命令列表：process 是主体，monitor 是附属能力

### 6.1 哪些命令进入列表

第一版主要展示 terminal 首次等待结束后仍运行、已转交后台 owner 的命令。这是“首次工具返回 running + process_id”的产品含义，不是扫描本机所有进程。

- 等待窗口内已完成的短命令留在对话工具记录，不要求再进入后台列表。
- 已转入后台的命令，即使没有 monitor，仍应显示。
- 子任务启动并转入后台的命令也显示，注明实际 task origin。
- 该命令随后完成/被终止，原条目更新终态，不立即因不再 running 而消失。
- 初连时命令已完成但后台记录仍保留，也可以恢复对应终态条目；不能只依赖页面曾经接收过 running 响应。

后端内部在创建进程时就有 ID，所以“存在 process_id”本身不是后台资格，也不是存活证明。应从已确认的后台交付事实导出读取字段，复用 yield/adoption machinery；不能新增 durable background job。

### 6.2 不以 monitor inventory 替代 process inventory

monitor 未注册、被取消或到期，都不应使仍在运行的进程从用户视野消失。终止组合中的 monitor 成功/process 失败也必须保留该进程。

展示不注册 monitor，不产生模型调用，不改变 tool permission，不扩大文件 seen observation。

### 6.3 信息与生命周期

展示命令、cwd、来源 ROOT/task、实际状态、退出码/终止原因和可用输出。可附加“结束后将通知模型”等人类可读说明，不要求用户选择 monitor。

进程/输出保留沿用当前 owner 的既有资源策略。“保留条目与输出”不承诺无限日志或跨重启恢复：

- 保留窗口耗尽时明确提示不可用/缺口，不伪造完整历史。
- Host 丢失时把当前控制能力置为不可用，不从旧工具 JSON 复活 process handle。
- 已有 canonical tool result/artifact 可继续按原权限读取，但历史输出不证明进程仍存活。
- 不扩大 scope 到 MCP 服务、任意系统进程或远程任务。

## 7. 终止后台命令的组合合同

### 7.1 执行边界

1. 验证 controller、当前 session/Host owner 和确切 process 身份。
2. 在 Host 接纳本次控制时确定模型反馈目标，遵守第 8 节。
3. 从当前 coordinator 查询该 process 的关联 monitor；没有关联项也是正常情况。
4. 关闭关联监视，阻止新的观察草稿和未来完成唤醒。
5. 通过现有 process owner 终止进程组并取得实际物理结果。
6. 分别返回进程结果、监视结果和反馈交付状态；UI 据此更新。

这不是由浏览器先发 cancel 再发 kill 的两个独立调用。组合顺序由 Host 持有，不能因页面关闭就遗失已启动的物理操作。

已结束的进程应返回其现有真实终态，不能只因晚到的点击改写成 killed。若仍有属于它的未投递监视，清理结果单独表达，不伪造进程终止。

### 7.2 部分结果

| 实际结果 | 显示与反馈 |
| --- | --- |
| monitor 已关，进程已终止 | 用户已终止；保留输出/退出信息 |
| 无 monitor，进程已终止 | 用户已终止；不把“没有监视”当失败 |
| monitor 已关，进程仍未终止 | 明确仍在运行/终止失败；不丢条目、不偷偷恢复监视 |
| 进程已自然结束 | 保留自然结束状态；分别说明监视是否关闭 |
| 有监视投递已经冻结或提交 | 不承诺撤回；继续原确认/结算，并说明仍有既有投递 |
| 网络结果未知 | UI 显示待确认，查询原目标；不猜成功、不自动作用于新目标 |

不得将已提交内容从 provider prefix 或 canonical 历史中删除。也不得为达成“全停”反向撤销已经开始的合法工具效果。

### 7.3 重复操作

重复请求仍指向同一 owner 下的同一 process。确认目标已终态后返回实际状态，不重复制造“用户终止”反馈。owner 被替换时返回不可用，不能重新解析旧 ID 去操作新进程。

具体请求确认与去重接入应优先使用现有 command identity/操作生命周期；不得为此建立全历史确认注册表或 durable receipt。

## 8. 活动模型与 idle：最新反馈合同

### 8.1 四条信息流不能混在一起

1. **UI 状态更新**：用户随时能看到命令终止/失败、监视关闭。
2. **现有工具调用结算**：poll/wait/kill 等已开始调用仍返回真实结果，不吞掉 tool result。
3. **人工控制反馈**：活动模型在合法输入边界得知用户干预及实际结果。
4. **自动创建新 ROOT**：本次人工终止不默认获得这一权力。

因此“不额外唤醒”不是“不告诉模型”，更不是“让模型停止回答”。

### 8.2 状态判定与交付

以下为本次讨论的落地建议边界；实现规格须把判定点写成精确的 Host 接纳合同，而不是前端 isRunning 的猜测。

| Host 接纳操作时 | 处理方式 |
| --- | --- |
| 有活动 ROOT A | 绑定 A；结果就绪后，向 A 的下一个合法输入接纳点提供一次真实控制反馈 |
| A 的 provider 请求正在流式生成 | 不修改在途请求、不强制中断；等下一允许接纳位置 |
| A 正在执行工具 | 保留整个工具结算边界；不把反馈插进请求与结果配对之间 |
| A 已冻结 compaction successor | 不重写 successor；遵守现有普通接纳边界 |
| idle，无活动 ROOT | 更新 UI；本次反馈不创建 NewTurnInstallation |
| 原目标 A 在接纳反馈前已结束/关闭入口 | 不改投 B，不复活 A，不自动新建轮次；如实标注未向原轮次交付 |
| 其他 monitor/人工输入启动了另一个轮次 | 不把它误归因于本次用户终止，也不扩大取消范围 |

绑定后不因 process kill 花了较长时间，就重新选择那时的“当前模型”投递。只有进入实际 provider 输入之后，才能声称反馈已经可供模型读取；UI toast 或 live close event 不是这一证明。

### 8.3 反馈内容

至少表达控制来源为用户、确切 process、实际终止结果、监视处理结果，以及“其他工作未被此操作停止”。使用 typed 来源和实际值，不伪造用户键入了一段话或模型自己调用了某工具。

示意语义，不是最终 wire schema：

```text
用户终止了后台命令 P。
进程：已终止。
关联监视：已取消，不会再产生新的监视通知。
本轮及其他工作未被停止。请根据该事实继续当前任务。
```

失败示例必须改为“监视已取消，但进程仍在运行/无法确认已终止”，不能为了简化文案说成功。

反馈不要求模型专门输出一句确认，也不强迫模型重启命令、重建 monitor 或重试任务。模型应根据当前用户目标决定后续工作，不能把终止当成重跑授权。

### 8.4 idle 与晚到反馈的保留深度

已确认的要求是 **不因此额外唤醒**。尚未决定“idle 时的取消事实是否要自动携带到下一次人工发起的轮次”。

本稿不暗中增加跨轮次或跨重启的 durable delivery 队列，也不承诺自动补发。最低要求是 UI/现有 owner 可核查实际状态。若希望下一次人工轮次一定看到该事实，须在第 14 节明确来源、现有读取/接纳方式、保留寿命和丢失语义后再实施。

### 8.5 当前实现的实际缺口

monitor 的 `_close_locked` 会清理 mutable draft/successor 并发出 TERMINAL_MONITOR_CLOSED；这不是 provider-visible 人工控制反馈。

现有 TerminalObservationKind 只有 PROGRESS、HEARTBEAT、COMPLETION、EXPIRY。不能把用户取消塞成 COMPLETION/EXPIRY，也不能让 closed monitor 再注册一次来“发最后一句”。

需要定义 typed 人工控制结果和 existing-turn-only 的接纳方式。复用已有 compiler、资源接纳、provider safe point、输入追加和结算 owner；并不意味着可以不作适配就调用普通 monitor 的自动唤醒循环。

具体载体尚未冻结。若必须增加 durable occurrence、事件枚举或数据库关系，必须先证明独立产品必要性并修订实施规格；不能借“用户通知”随意扩张类别。

## 9. 子任务图与取消语义

### 9.1 图只表达真实依赖

- node identity 为 task_id，箭头表达“依赖谁的结果”。
- ROOT/task origin、batch grouping、创建顺序与 dependency edge 分开；不能把它们画成同一种关系。
- 复用任务分页与 dependency rows。当前 control 窗口不是全历史图，不得静默遗漏页外节点并称完整。
- 未加载的依赖明确表示尚未加载，可按现有分页/后续只读查询补齐。
- 布局、缩放、折叠与选中属于 UI；初版不允许拖线修改后端依赖。

### 9.2 取消选中的任务

共享 KernelSubagentManager 的现有取消和结算流程：排队/待依赖任务进入 CANCELLED；活动任务中断并完成原效果结算；已终态任务返回实际状态。

前置失败导致等待中的下游进入 BLOCKED_DEPENDENCY_FAILED，由原 repository frontier 处理。UI 展示原因，不自行遍历后端并发送一串取消请求。

取消不是暂停。初版没有恢复旧任务、自动重试或重新接纳依赖按钮；后续若加重试，应明确它是新的任务还是原任务新执行，不能只改状态标签。

### 9.3 关联进程不默认级联终止

origin 能证明谁启动命令，不能证明只有该任务使用命令。取消子任务不默认杀其已转入后台的命令，不因依赖边传播而杀其他任务进程。

节点详情可列关联后台命令，并让用户分别终止。暂不增加“取消子任务及全部关联进程”的一键动作；如以后增加，需要另定影响预览和组合结果，不能复用普通取消名称偷偷扩大范围。

有实际下游影响或关联进程时，确认界面提示真实事实；没有额外影响时不要求每次弹窗。预览是读取结果，不是锁住世界的承诺，执行后仍以 kernel 实际状态为准。

### 9.4 完成反馈沿用原资产

现有子任务异步完成合同已包含取消、失败、依赖失败等 terminal outcome。复用现有 ROOT completion inbox 和接纳路径，不为用户取消另造重复结果。

子任务的 queue-only completion 与 terminal monitor 的 idle 自动唤醒是不同产品机制，不能因 UI 都在“运行面板”就统一改调度。

参考：[子代理异步完成与 Late-Join 契约](PULSARA_SUBAGENT_ASYNC_COMPLETION_HARD_CUT_RESEARCH_AND_IMPLEMENTATION_SPEC.zh.md)。

## 10. ROOT 停止仍然是独立的 F06 hard cut

用户点击“停止本轮回复”时冻结所见 turn_id；没有目标时不发送无目标 STOP。

协议已有 target_turn_id，应让该入口要求明确目标，Host 验证 exact active owner 后才安装 cancellation cause。A 已结束、B 已开始时，旧请求不能取消 B；不保留无目标外部 fallback。

复用原 root task cancellation、started effect settlement 和物理 owner。停止 ROOT 不自动清空人工输入队列、不取消所有 leaf task、不关闭所有 monitor、不杀所有进程。

“停止 ROOT 后永久安静，直到用户恢复”不在本稿范围。其他已有输入或有效后台源仍可按各自合同继续；UI 应说明还有其他工作，而不是显示“全部已停止”。

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
- 复用既有单次读取边界和保留策略；不新增任务/历史/运行寿命总量 cap 来迁就图或侧栏。

### 11.3 控制反馈

请求在途显示“正在取消/终止”；后端确认后显示真实终态。已自然结束不改成已取消；未知不当成功；失败不删除对象。

每个异步结果绑定原 session、connection owner 和 exact target；success/error/finally 均检查，不能让 A 的迟到操作重连或更新 B。

物理操作、模型反馈、UI 状态分开表达。“进程已终止，但原轮次已结束，未向它交付反馈”是合法组合，不算进程终止失败。

## 12. 后端改动范围与不变量

建议复用边界：

- **HostSession**：controller 操作接纳、exact target、进程/监视组合、反馈目标固定和生命周期。
- **KernelSubagentManager**：共享 typed task cancel，既有 terminal settlement 与 dependency frontier。
- **DirectKernelToolPort / TerminalSessionManager / ProcessRegistry**：有限查询/控制接口，继续独占进程物理管理。
- **TerminalMonitorCoordinator**：按 process 查关联监视、取消与现有 in-flight 结算；不选择模型 turn。
- **既有 provider-input / safe-point owner**：控制反馈的编译、资源与 append-only 接纳；不让 UI 另写历史。
- **protocol/gateway/bridge**：最小闭合 DTO 和受限读取/控制路由。
- **frontend**：任务图、后台命令和真实反馈投影，不产生执行真源。

权限边界应在实施规格明确：人工控制由有效 controller 与 session/Host 目标授权；不能拿模型工具的展示状态代替授权，也不能直接调用私有方法绕开 owner。模型调用仍走原 permission/Hook/settlement，不为人工操作伪造模型工具请求或工具 Hook。

保留 SYSTEM/tools byte-identical、messages append-only；只有已批准的 cold epoch/compaction successor 才能重建根。读取面板、控制按钮、重连都不是新 rebase 边界。

不新增通用 Activity 实体、图数据库、scheduler、fingerprint registry、receipt/checkpoint、durable live-process/monitor job 或全历史确认集合。必要的新 DTO 不等于新的 durable authority。新增事件/表/列不是本稿默认授权。

控制开始后页面断开不应遗失物理操作 owner；复用现有 cancellation/physical settlement 纪律。不承诺跨 Host crash 的进程恢复或控制反馈自动补投。

## 13. 典型场景验收

| 场景 | 预期 |
| --- | --- |
| 短命令在首次等待内完成 | 只需对话工具结果，无后台条目要求 |
| 长命令转后台，没有 monitor | 可见、可读、可终止；不自动注册监视 |
| 子任务命令转后台 | 可见来源 task；没有 ROOT-only monitor 不影响管理 |
| 注册 monitor 后模型继续读代码 | 主循环继续，监视不强制退出 |
| 注册 monitor 后模型正常结束回复 | 后续普通观察仍按原 monitor 合同可唤醒 |
| 活动 ROOT 期间用户终止后台命令 | 关闭未来监视、终止进程；在合法边界提供真实反馈，ROOT 不被中断 |
| idle 时用户终止 | UI 更新；本次控制不创建新 turn/provider call |
| 操作期间原 ROOT 结束并出现新 ROOT | 不改投新 ROOT，不复活旧轮次 |
| monitor 已有冻结/提交中的观察 | 原结算不被破坏；不声称全撤回 |
| 取消监视成功、杀进程失败 | 进程仍可见；报告失败，不恢复监视或自动重试 |
| 模型自身 kill/cancel | 保留原工具语义与结果；不追加重复人工反馈 |
| 取消有依赖的子任务 | 下游由 kernel 进入真实依赖失败状态，无关任务继续 |
| 取消子任务，但它启动的服务器仍运行 | 显示关联服务器，不默认杀它 |
| STOP A 迟到，B 已开始 | B 不受影响；返回 A 的真实目标结果 |
| 已结束进程收到重复终止 | 不改写自然终态、不重复反馈 |
| owner 替换或保留窗口失效 | 禁止控制旧 ID；说明日志/状态不可用，不伪装仍运行 |
| observer 尝试控制、伪造 session/PID/cursor | 拒绝越权；普通查看不升级 controller 权限 |
| 切会话后旧请求完成 | 不更新/重连新会话，不串页、不串 toast |

实现后应通过真实 owner/adapter 测试和可控时序验证，不靠模型恰好慢来覆盖。真实 provider/browser 测试需按 AGENTS 只读使用保存配置，控制明确的探针命令；新功能证据不能由 PR02 的旧场景替代。

本轮仅编写文档，没有执行上述验收，也没有因此激活任何功能。

## 14. 实施规格还需闭合的细节

这些不是让前端自行决定的事项，也不是泛化并发审计；每项都有直接产品结果：

1. **人工控制反馈载体**：由哪个既有输入 owner 接纳，怎样表达用户来源而不冒充键入 prompt/模型工具结果；是否需要最小 schema 扩展，如何保持事件/存储类别不增长。
2. **exact active target 与 answer boundary**：Host 何时固定目标；原轮次结束时返回怎样的未交付结果；不能以普通 monitor 的 NewTurn fallback 代替。
3. **idle/未交付反馈的保留深度**：只支持状态核查，还是在下一人工轮次自动提供事实；不能未经定义加入持久待投递队列。
4. **后台资格读取**：从现有 yield/adoption 事实导出哪个只读字段，如何在 reload 后恢复条目而不依据文本或仅有 ID 判断。
5. **组合控制 outcome 和确认**：进程结果、monitor 部分结果、反馈状态如何组成 typed response；查询未知结果时如何复用现有身份与 owner，避免新增 receipt/registry。
6. **用户控制授权与 Hook 边界**：controller 对当前 owner 资源的操作权限、与模型权限快照的区别、人工操作的实际来源和已有效果结算怎样保留。
7. **侧栏输出接入**：复用查询还是已有订阅扩展，怎样遵守 cursor/gap 与连接失效；不为视觉实时性注册 model monitor。

先把这些写成可测试的最小实施合同，再进入生产代码 hard cut。若发现确需扩大 durability 或重建 provider 根，停止局部实现，先修订产品边界。

## 15. 推荐推进顺序

1. 闭合第 14 节，形成下一 PR 的实施规格和失败测试矩阵。
2. 完成 ROOT exact stop、共享 task cancel 和受限 process 读取，保持已有 owner 唯一性。
3. 完成人工进程终止组合与 existing-turn-only 控制反馈，证明活动/idle 分流。
4. 前端据后端合同重构子任务图和后台命令面板，不要求沿用当前侧栏布局。
5. focused、协议/完整回归、continuity、发布产物与新功能真实浏览器验收；同步规格与索引，再根据实际结果决定 activation。

当前只有这份新版讨论文档，未创建新 PR、未修改生产代码、未 stage/commit。早期方案中与本文冲突的用户展示和通知建议，以本文为准；未修改的生产功能仍遵守其现行 active specification。
