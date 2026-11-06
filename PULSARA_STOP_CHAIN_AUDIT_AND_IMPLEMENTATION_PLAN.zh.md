# Pulsara 停止链路审计与修改实施方案

状态：实施草案已完成两轮交叉复审，可作为实施基线；生产代码尚未修改。审计日期：2026-11-07。

审计基线：`b2df4babc672e420f506a2d109d55f7a38796098`。本文记录此 HEAD 的静态发现及拟议修复，不回推 0.1.1alpha.2 的历史实现。代码位置以该基线的文件、符号和行号定位；Git 管理内容身份。

## 1. 结论与范围

用户反馈是“只偶尔出现过，点停止无法停止”。截图中工具已显示“操作完成”，随后仍显示“思考中”和停止方块，连续点击产生“没有可停止的运行／当前没有可停止的任务”提示。

最新 HEAD 存在可产生同类组合表现的确定路径：前端长期沿用控制快照中的停止请求截止时间；一小时后，新操作也携带旧期限，Host 在发起取消前拒绝请求，前端将真实的过期原因误报成没有任务。该问题不要求当前轮次本身运行一小时。

另有停止最终结果未跟踪、物理工具超时后取消漏记、模型流消费方异常后未汇合关闭三个独立缺口。后三者分别处理，不将它们拼成未经证实的现场因果链。截图不足以确定旧版本现场根因，也不能证明模型本身已经卡死。

本次授权是记录 findings、起草方案及交叉复审。本文不代表生产修复、测试通过、真实 provider 验收或构建安装已完成。

实施范围是现有停止控制、对应前端适配、工具取消传播及 provider 流退出收尾。依据 [AGENTS.md](AGENTS.md)、[内核生命周期规格](PULSARA_KERNEL_LIFECYCLE_AND_SERVICE_COMPOSITION_IMPLEMENTATION_SPEC.zh.md)、[MCP 首次请求就绪规格](PULSARA_MCP_FIRST_REQUEST_READINESS_IMPLEMENTATION_SPEC.zh.md) 和当前生产代码。归档停止规格仅用于理解既有设计，不据其旧路径或旧状态覆盖当前合同。未跟踪的 Code Mode decide 草案不作为本方案依据。

## 2. 当前停止链路

```text
停止按钮捕获当前 session / Host / exact turn 或 manual command
  → createUserControlCommandRef
  → RuntimeConnection.stopActiveTurn / stopContextCompaction
  → renderer bridge 通用 command
  → v3 gateway
  → Host 校验 owner、command、准入期限、精确目标
  → 安装 USER_REQUEST，标记本轮 Cells，取消 ROOT task
  → 返回 PENDING，由 Host 继续物理停止、结果结算及终态确认
  → query_control_command 提供本次命令最终结果

另一路 observe 长轮询
  → canonical terminal/current_control + live 进展
  → 前端消息、思考标记、运行按钮收敛
```

实际通信是 POST 长轮询，不是 SSE。工具“操作完成”只表明该工具结果已结束，后续 provider 生成仍可运行。停止命令结果与目标当前状态是不同事实：目标已结束不证明这次停止成功；命令 PENDING 也不证明任务已经停止。

后端现有目标处理见 [host.py](src/pulsara_agent/conversation_kernel/host.py) `request_stop_turn`（5415–5537）：

| 目标状态 | 当前行为 |
| --- | --- |
| exact active ROOT task | 安装停止原因并取消原 task，返回 PENDING |
| 正在接纳的 starting turn | 捕获原 admission 的结算 owner，停止同一个目标 |
| Plan successor admission 正在推进 | 停止仍持有 successor 的原执行链 |
| canonical target 已终态 | SUCCEEDED / CONTROL_ALREADY_TERMINAL |
| canonical target 仍 RUNNING，但没有可匹配执行 owner | REJECTED / CONTROL_TARGET_UNAVAILABLE |
| 队列输入 | 使用独立队列取消入口，不隐式改为取消全部队列 |
| 手动上下文整理 | 使用独立 exact manual command owner |

## 3. 已确认 findings

### F1 P1 新停止操作复用过期截止时间，拒绝原因被错误展示

**触发条件：** 距离前端最后一次安装完整 live-control snapshot 已满一小时，期间没有重连，也没有通过 `LIVE_RING_OVERFLOW` 恢复安装完整新 baseline。普通观察、普通 canonical snapshot、会话列表刷新不解除该条件。

**证据链：**

1. [user_control.py](src/pulsara_agent/conversation_kernel/user_control.py) 第 14 行的准入窗口为 `3_600_000ms`；[host.py](src/pulsara_agent/conversation_kernel/host.py) `control_admission_deadline_ms`（4635–4638）返回当前 Host 单调时间加窗口。
2. [v3_gateway.py](src/pulsara_agent/terminal_protocol/v3_gateway.py) `_live_control_snapshot`（539–545）携带新期限；普通 `_observe` 响应（697–713）不携带该字段。
3. [runtime-adapter.ts](frontend/lib/runtime-adapter.ts) 构造函数第 3216 行保存初始 liveControl。普通 observe（3315–3317）只更新运行目标等字段；current-work 失效后即使读取新 baseline，也只采用 todos/media（3377–3387）。
4. 同文件 `createUserControlCommandRef`（456–480）每次生成新 command ID，却复制 projection 中的旧期限。
5. Host 在目标查找前校验期限（5433–5438），过期直接返回 `CONTROL_REQUEST_EXPIRED`（4803–4807）；尚未调用 `task.cancel()`，也未安装控制 attempt。
6. [pulsara-app.tsx](frontend/app/pulsara-app.tsx) `stopRun`（2081–2087）将所有 rejected 显示成“没有可停止的运行”，忽略 publicCode/publicMessage；该正常返回不进入异常恢复分支。

**可观察结果：** 真实任务仍运行，停止按钮仍显示；重复点击产生新命令，但仍带旧期限，因此持续被拒。空闲连接放置一小时后启动新任务也会触发。重连／完整 gap 恢复可刷新期限，符合偶发而非每轮必现的描述。

**影响面：** ROOT 停止、手动上下文整理停止、后台命令终止共用此期限。后台链还有一个恢复限制：[background-terminal-panel.tsx](frontend/components/background-terminal-panel.tsx) `stop`（279–283）在已有控制记录时只查询旧命令；实施时必须区分未发送、结果未知和已拒绝，不能让已拒绝操作永久阻止新的人工操作。

**已有覆盖与缺口：** [test_desktop_user_controls.py](tests/test_desktop_user_controls.py)（163–174）已验证过期必拒且没有 attempt。前端测试只覆盖初始化期限透传、目标刷新，没有跨一小时健康连接的新控制操作。

### F2 P2 正常受理的停止命令没有最终结果跟踪

**触发条件：** 停止请求正常返回 PENDING，随后物理收尾或终态确认产生 FAILED，或长时间仍未完成。

[host.py](src/pulsara_agent/conversation_kernel/host.py) `_execute_stop_turn_attempt`（5588–5653）会在停止 Cells、汇合 ROOT、结算、读取终态后形成最终结果；`_finish_control_attempt_locked`（4868–4894）更新进程内 attempt，由现有 query 接口读取。

[pulsara-app.tsx](frontend/app/pulsara-app.tsx) `stopRun` 的正常响应分支不保存 reference、不继续 query。异常恢复分支查询到 PENDING 后同样立即返回（2092–2101）。因此最终失败或无法确认没有准确展示。

**结论边界：** FAILED 可能只是确认读取失败，不能据此断言真实任务一定未停止。[test_pr03_user_controls.py](tests/test_pr03_user_controls.py)（88–129）已有“实际中断但确认失败”的后端用例。此 finding 不解释 F1 的拒绝提示，属于独立结果展示缺口。

### F3 P2 物理工具超时后的首次取消没有传播到结算事实

**可达时序：** 物理工具未完成 → `wait_for(shield(task))` 超过现有 watchdog → 进入 drain → 用户首次停止 → drain 捕获并忽略 CancelledError。

[io.py](src/pulsara_agent/conversation_kernel/io.py) `run_tool_invocation` 仅在原始 timed wait 的取消分支设置 `caller_cancelled` 并调用 `on_caller_cancelled`（223–229）；随后调用 `_drain_physical_task`（236–237），其取消分支仅继续等待（311–324）。

因此这个窗口中取消回调不执行，物理结果中的 `caller_cancelled` 仍为 false。工具返回后，[tool_execution.py](src/pulsara_agent/conversation_kernel/tool_execution.py) 第 1879 行不会重抛取消，同一批次尚未派发的工具可能继续执行。下一次 provider 前的 [runner.py](src/pulsara_agent/conversation_kernel/runner.py) 第 1705 行仍检查停止原因，不应扩大描述为全部后续模型调用都会继续。

继续汇合真实工具是必要行为；漏洞是汇合期间丢失首次取消事实。现有取消回调若直接抛错，也可能跳过此处物理 join，修复该局部路径时一并封闭。

现有 [test_round5_long_horizon_execution_envelope.py](tests/test_round5_long_horizon_execution_envelope.py) 第 330 行用例覆盖迟到精确结果，第 391 行用例覆盖超时前取消，未覆盖超时后才取消。截图阶段与本窗口不直接吻合。

### F4 P2 模型流消费方抛错后没有显式汇合生成器关闭

[runner.py](src/pulsara_agent/conversation_kernel/runner.py) `_collect_model`（2739–2754）直接遍历 `execution.open_once(permit)`；循环体的 `assembler.apply()` 可能抛错，例如 [assembler.py](src/pulsara_agent/conversation_kernel/assembler.py) `_delta`（269–271）拒绝超过现有拼装容量的内容。

此时异步生成器暂停在 [direct_model.py](src/pulsara_agent/conversation_kernel/direct_model.py) `open_once` 的 yield（669）。其 finally（672–706）才负责 provider `aclose()`、物理完成确认和 borrow 收尾。`async for` 的消费方抛错不会自动等待这个关闭；Runner 的异常/finally（2807–2815）仅撤销草稿并清除 active execution，可能先结束而物理关闭仍依赖异步生成器 finalizer。

**修复相关边界：** 仅添加 `contextlib.aclosing` 尚不足以说明重复取消与关闭错误正确。现有 `open_once` 将 GeneratorExit 记为 operation_error，随后 close 错误只在没有 operation_error 时保存；显式 aclose 时必须避免把真实关闭失败隐藏在 GeneratorExit 后面。

该问题是消费方异常入口的资源所有权缺口。截图没有拼装异常证据，不能将其认定为现场根因。已有 provider retry 测试覆盖读取取消及关闭期间取消，没有覆盖消费方 assembler 抛错时等待真实关闭。

## 4. 已检查但不作为本次主要 finding 的情况

| 检查项 | 当前结论 |
| --- | --- |
| 工具完成后进入下一轮思考／自动压缩 | 正常路径保留同一个 ROOT task；未发现正常续行直接丢弃 owner |
| 正常完成后再次停止旧目标 | 返回 CONTROL_ALREADY_TERMINAL，不足以解释截图 rejected |
| 只丢 THINKING_END 或 live settlement | canonical terminal/current_control 到达后，前端过滤未提交 draft 并清 busy；不足以证明永久假忙 |
| observe 分页与 gap | live revision 是本批最后交付位置；其他 gap 在安装 cursor 前触发重连，未证明永久跳过终态 |
| provider 读取／退避的正常取消 | 取消向上传播，关闭先汇合再结束；未发现正常 Stop 触发自动重试新请求 |
| 会话切换 | 有 exact session/Host 与回包归属校验；未证明旧回包误停止新会话 |
| 终端已转入后台、独立子代理工作 | 按各自现有控制合同处理，不把 ROOT Stop 扩大成全会话 kill |

另有两个受限观察保留为后续定位线索，不据此扩大本次实施范围：

- Plan successor admission 已完成但尚未提升 active slot 时，直接以 successor ID 停止可能找不到 owner；正常 UI 优先使用 live origin ID，尚未证明该 UI 路径可达。现有 handoff 测试以 origin 停止。
- COMMITTED settlement 未显式关闭 live reasoning.active，canonical reasoning 加载未完成且缺匹配 END 时可能短暂显示“思考中”；该展示本身不驱动 isRunning，不解释完整截图。

## 5. 必须保持的产品和架构边界

1. 停止对象固定为用户操作时所见的 session、Host 和精确目标。准备／发送／查询之间发生切换，不自动改投新 Host、新 turn 或 successor。
2. 保留现有单次控制准入窗口及结果保留规则；修复新操作使用旧期限的问题，不扩大常量、不续写旧命令、不从浏览器时钟伪造 Host 单调时间。已受理且仍保留的 exact command 先返回原 attempt，即使原 deadline 已过也不重新执行；仅没有原 attempt 的过期提交被拒。查询不要求原 deadline 仍有效。
3. 长连接和长任务没有新增总时限、调用次数或查询次数上限。查询间隔只调度当前观察，不构成执行寿命上限。
4. 不新增数据库表、迁移、持久事件、live event 种类、subject、guard、产品关系、durable job、receipt、lease、重放恢复图或新的权威注册表。复用现有命令查询和进程内所有权。
5. 同一 epoch 的 SYSTEM/tools 字节不变、messages 只追加。停止修复不改变 provider prefix、权限、队列消费、计划 continuation 或副作用结算语义。
6. 已知工具结果先精确结算，再传播取消；不重放副作用，不用取消 waiter 代替物理退出，不把无法确认包装成成功。
7. command 身份继续由共享 ID 层生成；UI 准备阶段不另造 action ID。实现按单一路径 hard cut，删除被替代的旧期限注入和旧结果处理。

## 6. 实施设计

### 6.1 F1 使用既有快照准备新的精确控制命令

由 `RuntimeConnection` 提供一个小型异步准备方法，供 ROOT、manual compaction、background process 三个现有 UI 动作共用。输入是已冻结的 operation、session、expected Host、target kind 和 target ID；输出是不可变的现有 `UserControlCommandRef`。

准备顺序：

1. UI 在 await 前捕获连接和 exact target，并同步标记该目标正在准备，阻止同目标双击创建两个准备过程。
2. adapter 在现有 `ordered` 观察序列中请求既有 `live-control-snapshot`。该 endpoint 会重建服务端 live-control subscriber，不能与 observe 无序并发。
3. 校验响应仍属于捕获的 Host，读取本次新产生的 deadline。此次准备只消费 owner／deadline，不安装新的 interaction、todos、media 或 cursor；保留客户端原 cursor 以便下次 observe 继续回放。
4. 校验准备仍归属原连接、controller 和页面动作，然后通过共享 ID 层分配一个 command ID，构造冻结引用。原目标即使已终态，也不改为另一个目标；由 Host 给出已终态结果。snapshot 是观察读取，不证明提交时仍有控制权，最终接纳继续由现有 gateway controller gate 裁决。
5. UI 再次确认连接归属，先保存冻结引用，再发送一次。未发送前准备失败，清除 preparing 状态并允许新的人工点击；发送可能发生后保留原引用，只查询该命令，不自动重发。

准备方法不必串行化耗时控制执行，也不在全局锁内等待停止；它只加入现有观察读取顺序。无需修改 protobuf、添加 heartbeat 或定时续期服务。

此处保留原 cursor 有当前实现依据：[live_control.py](src/pulsara_agent/conversation_kernel/live_control.py) 的 subscriber 只记录 owner epoch（71–72），snapshot／detach 不裁剪共享 ring（103–114、208–210）；observe 按请求中的 after_revision 回放（122–163）。[v3_gateway.py](src/pulsara_agent/terminal_protocol/v3_gateway.py) 第 653–656 行使用客户端 cursor，而非 snapshot 写入的 state.live_control_revision 作为下限。同 owner 且 ring 尚保留时可回放订阅重建前后的过渡事件；ring 已溢出时走既有 LIVE_CONTROL_GAP 重连／完整 baseline 恢复，绝不推进未交付 cursor。A3 对两条路径都做集成验证。

删除 `RuntimeProjection.controlAdmissionDeadlineMs` 对新命令创建的依赖、同步 `createUserControlCommandRef` 公共入口，以及 App → InspectorPanel → BackgroundTerminalPanel 的旧 deadline props。snapshot wire 字段与后端校验保留，由新准备方法消费。现有 hostSessionId 展示和精确 owner 判断继续使用。

Code Mode cell 停止已有后端协议，但当前没有对应的前端停止调用；保持协议与权限回归，不为此增加新 UI。

### 6.2 F1/F2 结果展示和命令跟踪

ROOT／manual Stop 在现有 App 会话控制状态中保留必要的原 reference 和最近结果；后台进程复用 `DesktopWorkspace.backgroundControls`，按 session＋Host 分区，面板只消费该状态。无 workspace 的独立面板保留现有局部 owner，不另建 App 后台控制 map。仅扩展当前操作及尚未结束的旧结果／反馈观察所需的状态，不增加持久恢复、执行 owner 或通用控制服务。

| 阶段／结果 | UI 与后续操作 |
| --- | --- |
| preparing，尚未发送 | 同目标重复点击加入／忽略本次准备；失败可重新准备 |
| PENDING | 显示正在停止；按原引用继续查询，不能显示已停止 |
| SUCCEEDED / ROOT_STOPPED 等 | 显示对应已确认结果，结束此命令的执行跟踪 |
| SUCCEEDED / CONTROL_ALREADY_TERMINAL | 显示目标已结束，不声称这次停止造成结束 |
| REJECTED / CONTROL_REQUEST_EXPIRED | 显示本次请求已过期；不影响真实运行投影，下一次明确人工操作重新准备 |
| REJECTED / OWNER_UNAVAILABLE | 显示原会话运行实例不可控制，不改投新 owner |
| REJECTED / CONTROL_TARGET_UNAVAILABLE 或 NOT_CURRENT | 显示原目标当前无法停止／已不在当前执行中，不断言会话没有任务 |
| FAILED / CONTROL_FAILED | 显示停止失败或结果未能确认，保留已知事实与诊断；同一目标仍可操作时允许新的明确人工操作 |
| 响应丢失或 query 暂时失败 | 保留原引用，显示待确认；只读查询，不自动创建新命令 |
| query OWNER_UNAVAILABLE | 原 Host 不再可查询，结束其查询并保留不可确认事实，不推断物理工作已经结束 |
| query RESULT_UNAVAILABLE | 仅表示此刻查不到该 command；保存原 reference 和 UNKNOWN，在原 Host 可用时继续只读查询，不推断未发送、未受理或已停止 |

ROOT／manual 的查询由一个 App UI effect 调度，沿用后台面板已有的一秒查询节奏，仅在当前控制视图存在未决记录时运行。observe／人工刷新可以唤醒或合并需求，但不会按每个流事件立即发起查询；同命令两次查询启动至少间隔一秒，且至多一个在途查询，不阻塞 observe。间隔是 UI 读负载控制，不是总查询次数或执行时限。

后台保留 `refresh()` 作为唯一查询调度来源，删除 `stop()` 内的递归 poll。定时刷新、人工刷新和面板重新打开都进入同一个调度路径；按 command 保存在已有 workspace 控制记录中的在途标记与最近启动时间去重，避免重挂载／多个视图重复查询。查询遍历仍需观察的控制记录，不仅遍历当前进程列表；进程暂未出现在列表中不丢弃旧命令结果或未完反馈。旧请求 finally 只清理自身的在途状态，不能清掉新请求状态。物理执行已终态后仍按现有 `controlNeedsPolling` 的反馈维度决定是否继续观察。

preparing／PENDING 的普通重复点击不产生新命令；UNKNOWN 的默认动作是查询原结果。同一 exact target 仍被当前事实证明可操作时，错误区域可以提供明确区分的“再次请求停止”人工动作：该动作创建新 reference，保留上次结果未知的事实，不复用或修改旧 ID／deadline，也不把两次操作描述成一次自动重试。已终态的 FAILED／REJECTED 同样允许新的明确操作。每个目标只保存当前操作及仍有未决结果／反馈的旧引用；旧回包按 command 归入旧记录，不覆盖新操作。后来独立出现的新目标不受旧 pending 全局禁用。

断线时保留原引用；同 Host 重连后按原身份恢复查询，connection generation 只用于传输／视图归属，不替代命令 owner。Host 已变化时展示原结果不可确认，不修改 reference。切换会话／离开控制视图时暂停该视图的新查询和通知，保留原记录，返回同 session／Host 后恢复。即使已经切走，只要原记录仍存在且完整 reference 匹配，在途确定回包仍写入原记录；是否通知当前页面另按当前视图归属判断。明确关闭会话、用户明确结束该结果观察、Host 失效或 App 销毁会结束相应 UI 跟踪；已释放记录不能被迟到回包复活。这些行为不取消后端已受理工作，不承诺跨应用重启恢复。

ROOT／manual 的完成记录在最终结果通知被当前视图消费后释放；切走时收到的未展示最终结果保留到返回展示、用户明确清除或会话关闭。后台已完成操作且反馈观察结束后保留当前面板所需结果，下一次明确操作或对应 workspace 释放时删除完成旧记录，不积累所有历史命令。未决记录没有任意条数／寿命上限；用户可以明确结束结果观察，结束观察不代表执行成功。

Host 在途 attempt 不因原 deadline 到期而退役；已结束结果保留到 `max(原 deadline, finished + 1h)`，反馈未结清时继续保留。UI 不把一小时当成查询超时。RESULT_UNAVAILABLE 可以先出现、稍后变为 FOUND，因此不会单凭一次缺失永久结束同 Host 确认。

后台命令的已有反馈维度（物理结果、canonical 反馈、模型纳入）保留各自含义，不能用 ROOT Stop 的单一终态提前终止原面板的反馈观察。是否允许再次人工终止，取决于上次执行阶段与目标当前可操作性；是否继续查询旧反馈，取决于旧反馈自身状态，二者不能混为一个 `controls.has(processId)` 判断。

### 6.3 F3 在物理汇合期间保留取消

在 `run_tool_invocation` 内使用一个局部、幂等的取消标记函数：先设置 `caller_cancelled=True`，再调用现有 `on_caller_cancelled`，至多调用一次。原 timed wait 取消分支和 drain 取消分支都通过它处理。

必须区分等待者被取消和物理操作自身抛出 CancelledError：结合当前等待 task 的取消请求事实与物理 task 终态判定，不能仅凭捕获的异常类型设置 caller_cancelled，也不能仅凭 physical task.cancelled() 忽略同时发生的等待者取消。可在首次物理等待前记录一次现有 task.cancelling() 计数基线并检测新增请求，贯穿 timed wait／drain，进入 drain 或重复等待时不重置，已记录的取消事实不回退；不调用 uncancel，不引入新的取消 owner。无外部取消时，物理 CancelledError 作为原 RAISED outcome 返回且 caller_cancelled=false；两者并发时保留原物理异常和 caller_cancelled=true。

`_drain_physical_task` 只需一个可选同步取消通知参数；不传参数的 `run()`、`aclose()` 保持原合同。继续 shield 并 join 同一个物理 task，不取消线程 task，不提前释放 semaphore。

同步取消通知是 best-effort，回调抛出的 BaseException（包括 CancelledError）记录到既有诊断并继续 join；不能替换 physical.error、丢掉 exact result，或使 waiter 在物理完成前退出。回调错误不能清除已经记录的 caller_cancelled，也不触发重复通知。物理 value/error、观测时长及既有 timing 分类保留，现有工具结算在确认结果后重抛取消，阻止同批后续调用。

### 6.4 F4 显式拥有并汇合模型流关闭

依赖边界保持：SDK／transport 拥有网络流关闭，`PreparedKernelModelExecution.open_once` 拥有物理完成检查和 borrow 释放，Runner 拥有其消费迭代器的完整生命周期。

Runner 显式保存 `open_once(permit)` 返回的同一个迭代器，在正常完成、消费方异常、外部取消的统一 finally 中关闭它，不创建第二次 open_once。复用当前代码中的 task＋shield＋drain 汇合惯用法，等待同一个局部 aclose task；不跨层调用线程专用的 `io._drain_physical_task`。关闭等待期间首次或重复收到的取消都记录下来，不能取消真实关闭 task，也不能因完成 join 就吞掉取消。`_active_execution` 的释放必须在关闭汇合之后；不添加新的 provider owner 注册表、重试或总关闭期限。

`open_once` 收尾将 GeneratorExit 仅视为消费方关闭控制信号，不参与真实异常优先级；不能让它遮蔽 close／物理完成错误。异常传播规则固定如下：

| 条件 | 关闭汇合后的结果 |
| --- | --- |
| 已有真实消费／读取异常或原始 CancelledError | 保留原异常对象和类型；物理收尾失败通过异常链或既有诊断保留 |
| 没有原异常，关闭汇合期间首次收到等待者取消 | 同一次关闭完整汇合后重抛取消；同时发生的物理收尾失败保留在异常链或诊断中 |
| 没有上述两项，仅物理收尾失败 | 向调用者抛出该失败 |
| 上述情况均不存在 | 正常返回 |

已安装的 USER_REQUEST／HOST_CLOSE intent 继续决定 ROOT 终态；保留原生成异常不改变该终态优先级。usage observation 维持现有 best-effort：普通异常只记录已有 warning，不属于反转主结果的物理关闭失败；观察至多一次，失败／取消生成不得建立成功 usage anchor。Runner 等待 DirectModel 原 finally，不重复执行 transport close、borrow release 或 usage。

物理完成只由 `wait_physical_completion()==COMPLETED` 证明；`aclose()` 正常返回但 completion 为 BLOCKED 同样是未确认退出。`PHYSICALLY_CLOSED` 必须在确认后设置，不能先设置再检查。borrow 继续由原 DirectModel 唯一收尾路径处理一次；无法确认退出时保留真实失败，遵循现有依赖释放／隔离规则，不提前宣称成功或自动重放。不得仅增加 `aclosing` 包装就宣告覆盖这些分支。

## 7. 修改落点与删除项

| 文件／模块 | 预计修改 |
| --- | --- |
| `frontend/lib/runtime-adapter.ts` | 异步准备精确命令；复用 ordered snapshot；移除旧 projection deadline builder 路径 |
| `frontend/app/pulsara-app.tsx` | ROOT/manual preparing 与原命令结果跟踪；按 publicCode 展示；重连／切会话归属保护 |
| `frontend/components/inspector-panel.tsx`、`background-terminal-panel.tsx`、`desktop-workspace.tsx` | 替换旧 deadline props；扩展已有控制记录；删除 stop 内递归 poll、统一 refresh 查询；保留反馈维度与终态／UNKNOWN 下的明确新人工操作 |
| `frontend/components/workbench-view.tsx` | 仅在必要时呈现同目标正在停止／待确认；不改整体布局 |
| `conversation_kernel/io.py` | drain 取消通知、局部幂等标记和回调异常时的物理 join |
| `conversation_kernel/runner.py`、`direct_model.py` | 显式迭代器关闭汇合，异常优先级与 GeneratorExit 收尾 |
| 对应 frontend／Python／gateway 测试 | 补真实行为回归；迁移旧 builder fixtures，不保留兼容接口 |

Host 控制准入、精确目标、命令查询和 protobuf 继续使用现有接口。clean-v0 schema 和 durable 分类不变；不产生数据库迁移。测试 fake adapter 必须随接口一起 hard cut，不提供旧 builder 回退。

## 8. 验收矩阵

使用仓库根 `.venv` 管理的 Python；前端使用仓库现有 Vitest 工具。用可控时钟、事件屏障和现有 fake／本地 transport，不靠固定 sleep 猜测竞态，不新增 skip/xfail 或放宽断言。

| 编号 | 必须证明的行为 | 首选覆盖位置 |
| --- | --- | --- |
| A1 | 同一健康连接超过准入窗口后，新 ROOT、manual、background 操作取得新期限并可受理；当前轮次可以刚开始 | `runtime-adapter.test.ts`、`test_desktop_user_controls.py` |
| A2 | 无保留 attempt 的过期提交拒绝且零执行；已受理并保留的 exact command 在期限后仍返回原结果、不重新执行；新命令不续旧身份 | 同上 |
| A3 | 准备 snapshot 与 observe 排序正确；ring 内旧 cursor 可回放订阅重建前后的 interaction open/close；ring 溢出走既有 GAP 恢复、不跳 cursor | adapter + 真实 gateway/live-control integration |
| A4 | 准备期间切会话、换 Host、角色失去controller、连接关闭、目标自然完成／出现successor，不改投；snapshot成功不能绕过command权限gate | adapter/App |
| A5 | preparing 双击只发送一次；确定未发送失败可重试；FAILED／REJECTED 后同目标仍可操作时，新人工点击可新建命令；旧回包不覆盖新命令 | App/background panel |
| B1 | 正常 PENDING→SUCCEEDED、ALREADY_TERMINAL、FAILED 均展示真实结果；确认失败不抹掉已知中断事实 | App + `test_pr03_user_controls.py` |
| B2 | 初始回包丢失、query失败、同Host重连只查原命令；原deadline过后在途／保留结果仍可查；RESULT_UNAVAILABLE随后FOUND可恢复；实际退役／换Host不伪造成功 | App/adapter + Host query |
| B3 | 切走后收到终态仍保存原记录、不给新页面通知；切回展示原结果；释放后迟到回包不复活；新目标不被旧pending禁用 | App |
| B4 | 后台物理结果与反馈独立收敛，进程暂时不在列表时仍查询其未决记录；查询阻塞时定时／人工刷新和重挂面板不产生第二个同command查询；旧poll不再启动；高频observe不突破既有一秒UI查询节奏 | background panel/App |
| B5 | UNKNOWN默认查询原引用；用户明确再次请求停止才新建引用，上次未知事实与未决反馈不丢；最终通知消费／明确结束观察后释放相应记录 | App/background panel |
| C1 | 超时无取消仍保留 exact value/error，caller_cancelled=false | `test_round5_long_horizon_execution_envelope.py` |
| C2 | 确认进入真实 drain 后首次 cancel；callback 恰一次，线程未释放时 waiter 不结束，最终 flag=true | 同上 |
| C3 | timed wait 首次取消＋drain 重复取消、drain 多次取消均不重复 callback；迟到物理异常仍是原异常 | 同上 |
| C4 | callback普通异常／CancelledError均继续join，保留exact result和取消事实；通知恰一次；`run()`、`aclose()`默认语义不变 | 同上 |
| C5 | 双工具批次第一项超时后 Stop：第一项精确结算一次，第二项零派发，turn 最终 INTERRUPTED，无下一 provider | 工具／runner integration |
| C6 | 物理操作自身抛CancelledError、没有外部取消时保留原RAISED且caller_cancelled=false；与外部取消并发时原物理异常和true均保留 | IO integration |
| D1 | 真正 PreparedKernelModelExecution 流在 assembler 内抛错，close 卡在屏障时 Runner 不结束、不提前释放相关资源 | runner/direct-model integration |
| D2 | 消费中首次取消、显式close等待中首次取消及重复取消均完整汇合且最终传播；SDK close与usage分别设屏障，active引用不提前清除，borrow/usage至多一次，无新请求 | 同上、provider retry tests |
| D3 | GeneratorExit＋close失败、主异常＋close失败、仅close失败、仅cleanup阶段取消均按异常矩阵处理；验证原异常对象／类型及cause或诊断；aclose返回而completion=BLOCKED不得标PHYSICALLY_CLOSED | 同上 |
| D4 | 普通完成、read/backoff 取消、terminal 后 close failure 的既有行为不回退 | `test_provider_stream_retry.py` |
| E1 | 完成／中断 current_control 到达后 busy 清除；starting turn、Plan handoff、Cells/MCP 结算仍按原 exact owner | 现有 frontend 与 Host lifecycle/control tests |

工具测试应通过包装真实 helper 的入口通知来建立 drain-entered 屏障，包装不得替代实际 join。provider 测试必须让真实生产消费／关闭路径接受可控 transport，不能只验证一个独立 helper。

## 9. 实施顺序与完成门槛

1. 先补 A1/A2、B1、C2/C5、D1/D3 的失败回归，确认真实缺口，不只测试重写后的结构。
2. 完成 F1/F2 前端单一路径及背景入口 hard cut，同时验证 snapshot 订阅顺序和 UI 归属。
3. 完成 F3 取消记录及工具结算回归，再完成 F4 显式关闭与异常优先级。两个改动可独立开发，但合并后跑停止全链路回归。
4. 运行第 8 节对应的 focused 测试及直接受影响的前端类型／构建检查、Python 静态检查。协议定义不变时不人为修改生成产物。
5. 使用已验证的独立本地 PostgreSQL 测试库跑 Host／gateway／工具结算集成。无需为了文档或普通单测重置用户数据库。
6. 激活前使用保存的生产配置，经 `LocalSettingsStore` 与 `require_pulsara_home()` 只读加载并注入隔离环境，完成一次真实 provider 思考中停止、一次真实工具执行后停止；核对最终命令结果、canonical 中断及相关物理收尾。跨一小时场景用确定性时钟测试证明，不要求人为等待一小时。不能注入新环境凭据或打印真实凭据；证据保留具体请求、响应和失败详情。
7. 确认无未结算测试 owner、无副作用重放、无新耐久分类及 provider 前缀变化后，才把文档状态更新为已实施／已验收。真实验收缺失时保持相应未完成状态。

构建安装不是本次文档任务的一部分。后续实施是否安装按届时任务要求执行，不由本文推定。

## 10. 交叉复审记录

2026-11-07，五位 GPT-6 Astra 子代理对根目录草案进行了两轮只读复审。各自核对生产代码，并交叉检查其主要审计范围以外的设计。下表记录已合入正文的最终结论，不保留已被替代的方案作为并行选项。

| 复审范围 | 闭合的主要问题 | 第二轮结论 |
| --- | --- | --- |
| 前端控制、后台进程、交互生命周期 | UNKNOWN 与 OWNER_UNAVAILABLE 分离；终态后明确人工新操作；旧回包保存与当前视图通知分离；workspace 记录、查询节奏及释放 | 无剩余方案阻塞 |
| Host、协议、订阅和查询保留 | 已受理命令过期后 exact-join；metadata snapshot 与旧 cursor 回放证据；controller gate；在途及结束结果保留语义 | 无剩余方案阻塞 |
| 工具取消，并交叉检查 provider 关闭 | 回调 BaseException 不脱离 join；等待者与物理 CancelledError 分开；首次关闭等待取消；BLOCKED 不误标完成 | 无剩余方案阻塞 |
| Provider 关闭，并交叉检查工具取消 | GeneratorExit、主异常、收尾异常和取消的优先级；usage best-effort；关闭／usage 屏障与异常身份断言 | 无剩余方案阻塞 |
| 独立架构与范围反证 | 后台唯一 refresh 查询并删除递归 poll；记录释放；无新增耐久类别、总寿命限制、兼容路径或 provider 前缀例外 | 可作为实施基线 |

复审通过的是 findings 的静态依据、修改设计和验收要求。第 8–9 节的生产修复、测试及真实 provider 验收仍待实施；没有将文档复审记作行为验证通过。
