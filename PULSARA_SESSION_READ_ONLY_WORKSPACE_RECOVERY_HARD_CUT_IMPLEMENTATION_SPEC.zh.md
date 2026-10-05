# Pulsara 会话统一只读与工作目录恢复前置实施规格

日期：2026-10-05。状态：规格与实现冻结；主代理与 GPT-6 Astra / high critic 代码复审通过，最终验收完成。本文仍是后续修改的当前权威规格。

本规格是定时任务实施之前的前置工作，覆盖普通会话。用户决定优先于旧文档。普通会话实现已在 main 落地；代码审阅与验收记录见 §15。定时任务尚未实现，保存生产配置与生产数据库未修改。

## 1. 产品决定

观察窗口与工作目录丢失共用一套聊天只读行为，原因和退出动作不同。指定目录与快速开始会话遵守相同恢复规则：保存路径丢失后不得自动 mkdir；用户明确选择继续，才在原路径创建空目录。新建快速开始会话仍由既有创建入口准备新目录。

只读时历史照常显示和分页。聊天页不能发送、上传、fork、创建/编辑批注、切模型、改变规划/本轮权限、压缩、重载运行时、解决交互、取消任务或后台进程、修改当前项目 Skills/MCP。复制、滚动、搜索、展开已有消息/批注/结果、查看任务与已保存内容保持可用。

重命名、归档和删除统一留在侧边栏的会话管理，属于明确的产品例外；两种只读原因都不影响这些入口的现有访问范围、确认和 lifecycle/retirement 检查。不能借管理入口发送消息、重载 runtime 或修改项目能力。只读是当前聊天窗口的行为，不暂停其他控制窗口、定时触发或正在执行的工作；工作目录不可用则另外构成所有执行来源共有的物理准入障碍。

本次不包含选择替代目录、迁移工作区、恢复原文件、恢复已丢失的进程执行、任意新安全沙箱或持久修复工作流。

## 2. 实施前源码接缝与复用 owner

以下是冻结规格时的实施前基线，Python 路径以 `src/pulsara_agent/` 为根；实施后的单一路径见 §15。

| 当前接缝 | 事实与本次改变 |
|---|---|
| `workspace_identity.py::resolve_workspace` | project 要求目录存在，transient 对传入路径也 mkdir；保留新建准备，删除恢复时隐式建目录路径 |
| `conversation_kernel/host.py::_open_admitted` | 先 resolve_workspace，再初始化能力并获取 writer；恢复需拆出具体的既存 session writer/目录准备接缝，不启动假 Host |
| `conversation_kernel/_repository/authority.py::acquire_host_writer` | 当前不同 owner 会直接推进 generation、接管并中断旧 generation；恢复意图须增加同事务“不接管有效他者 writer”条件，不能声称原默认已支持 |
| `web_app/session_controller.py::resume_session` | `_by_session` 与 `_ResumeInFlight` 复用 handle/并发打开；目录恢复、普通恢复仍由唯一 controller 获取 owner 驱动 |
| `web_app/browser_bridge.py::connect` | 获取 controller/observer 后依赖 Host resume；历史模式不能伪造 RuntimeConnection 来绕过这个条件 |
| `terminal_protocol/canonical_v3.py::CanonicalProtocolReader` | snapshot/history_page 从 PostgreSQL 读取 canonical entries，已有分页、字节边界、内容投影与 cursor；复用它，不复制历史 serializer |
| `web_app/browser_bridge.py::require_import_controller` | 已有 connection、generation、role、当前 controller 和隔离验证；提取具体共用写准入检查，不另设 authority registry |
| `frontend/components/workbench-view.tsx` | 观察 dock 可复用；fork 只看 message.forkEligible，会话更多菜单和部分检查器写操作未统一 gate |
| `frontend/components/response-annotations.tsx` | 已有 canAnnotate，禁用后仍能选择/复制文本；复用并关闭活跃编辑器/新增菜单，保留草稿 |
| `frontend/app/pulsara-app.tsx::openRuntimeSession` | connect 失败进入 SessionOpeningView；缺目录应进入正常历史工作台，而非卡住全页 |
| `conversation_kernel/_repository/archive.py` / `session_deletion.py` | 冷态访问、session 行锁、writer/lifecycle 与物理结算已有 owner；侧边栏管理不先打开失效目录 |

实施前 `ports/user_control_feedback.py` 仅表示后台进程控制，要求 process 与 RUNNING ROOT，不能把目录恢复伪装为 process 终止。§9 明确最小的 typed 内容扩展及其弱完整性边界。

## 3. 状态与统一只读规则

后端按物理路径观察得到 `AVAILABLE / MISSING / UNAVAILABLE`；后者携带具体原因，例如路径是文件、权限不足、路径身份变化或文件系统错误。只读原因保留完整集合，不以一个互斥 enum 丢掉“观察窗口同时缺目录”。

前端从实际 role、目录观察、连接和当前操作导出 `chatReadOnly`，不保存另一份权限真相。不改变既有 controller/observer 协议角色；冷历史视图没有 runtime role。offline/starting/quarantine 原有执行禁用继续成立，不能误显示缺目录恢复按钮。

### 3.1 用户可见的会话状态

根据用户追加决定，会话执行徽标只取数据库最近一轮 ROOT turn 的 `RUNNING / COMPLETED / INTERRUPTED`，分别显示 **进行中 / 已完成 / 已中断**；删除前端会话状态 `draft`、`waiting` 及“草稿/等待中”徽标。输入框的草稿、Plan draft 和子任务自身的等待状态不在此删除范围。

顶部现有会话状态位置统一只显示一个面向用户的状态。观察与目录丢失也使用这里，不要求用户理解内部状态分层，不另增加旁观 badge 或目录 badge。显示优先级与底部当前可做的动作一致：**观察中 → 目录丢失 → 最近 ROOT 的进行中/已完成/已中断 → 无徽标**。同时存在两种只读原因时先显示观察中，接管后显示目录丢失，恢复后回到实际执行状态；UNAVAILABLE 同位置显示 **目录不可用**，具体原因在底部提示。目录缺失/不可用只读优先于执行状态，即使其他窗口/已接受调用仍在运行，也不在顶部并列额外执行徽标。

后端会话 summary 与 canonical snapshot 共用同一最近 ROOT 查询口径，以该 ROOT 的 initial_entry 对应 canonical entry_sequence 判定先后，不以进程是否持有 handle、连接是否打开、子任务状态或任意 latest turn 猜测。summary 携带 nullable `latest_root_turn`（turn ID + closed status），前端沿用真实值；没有 ROOT 时返回 null 并隐藏执行徽标，不伪造 COMPLETED 或新增“未开始”枚举。查询失败/未知不能当作 null，按原读取错误/陈旧投影显示。

sidebar、聊天顶部、搜索/概览和冷历史使用相同映射；删除 `_summary_payload` / adapter 中 live→waiting、非 live→completed 的默认路径，以及 publishProjection 的无 ROOT→draft 路径。latest ROOT 的状态也不能仅凭 projection.isRunning 推断，live 更新从 canonical control 的 ROOT 事实取值，按现有 cut/晚响应保护保持一致。

等权限/Plan 确认时，没有更高优先只读原因的会话按数据库显示进行中，具体等待原因在对应交互区域展示；pending 队列保留独立计数/内容，不另造会话 waiting。OPEN/ARCHIVED 生命周期、controller/observer、目录可用性和服务连接在代码中各自保留真实含义，由上面的单一显示规则生成顶部标签；产品界面不解释这些内部分类。冷读看到 RUNNING 只表示数据库记录未结算，未连接时不得由它虚构物理执行、思考流或运行控制能力，不为显示状态自动恢复或改写数据库。

| 行为 | 两种只读状态 |
|---|---|
| 历史分页、复制、查看已有批注/思考/工具结果/任务详情 | 允许；无可读资源时显示准确不可用原因 |
| 查看已有队列、Plan、权限请求、进程日志 | 允许；不因此激活 Hook/MCP 或执行 query 工具 |
| 输入、粘贴/拖入附件、发送、steer、编辑/取消待运行输入 | 禁用 |
| 新建/修改/删除批注、从回复 fork、切模型/推理/权限/规划 | 禁用 |
| 压缩、重载 runtime、解决交互、停止当前执行/子任务/后台进程 | 禁用 |
| 当前项目 Skills/MCP 安装、编辑、开关、删除、连接测试/重连、OAuth 登录/登出 | 禁用；查看已观察状态允许 |
| 侧边栏重命名/归档/删除、切换会话、打开全局设置 | 沿用其独立 owner；全局设置不因某个聊天只读被整体禁用 |
| 退出只读的明确动作 | 按 §4/§7 校验，不能与普通聊天写操作共用“放行”布尔值 |

原响应是否具备 fork anchor 是历史事实，与当前窗口是否允许 fork 分开。禁用 fork 不改 canonical fork eligibility；renderer、快捷键与 callback 用相同 gate，后端提交也检查。

进入只读关闭新增批注菜单和正在编辑的弹层，保留原草稿/批注内容，历史里已发送批注仍可展开。关闭模型菜单、项目能力编辑器等尚未提交写界面；已提交动作仍按原 owner 结算，不能把结果改成未发生。

## 4. 界面

两种原因复用同一个输入区只读 dock 的布局、键盘焦点与移动端行为，不覆盖上方历史，也不拿会话打开错误页面代替工作台。

顶部状态标签沿 §3.1 单一位置呈现；底部 dock 只解释当前阻塞和提供继续动作，不再显示另一组状态徽标或介绍数据库/runtime 内部分类。

观察原因文案：**此窗口仅供查看**；按钮 **在此窗口继续**。

目录缺失文案：**工作目录已丢失**；说明 **历史对话仍可查看。继续时会在原路径创建空目录，原文件不会恢复。**；显示原绝对路径；按钮 **创建空目录并继续**。不再添加解释段落或二次通用确认弹窗，这个明确按钮就是创建授权。

同时有观察原因和缺目录时先显示 **在此窗口继续**，接管成功后仍受目录 gate，才显示创建按钮；接管不得顺便 mkdir。只有 MISSING 提供创建，UNAVAILABLE 显示具体错误与“重新检查”，不尝试覆盖文件或权限错误。

这条接管路径只适用于仍有真实 live Host 的情况：`resume_session` 的 live fast-path 与真实 attachment/takeover 在缺目录时仍可用，目录 gate 阻止执行而不阻止连接。若 live Host 已消失，普通连接返回 scoped 冷历史、role=None，清掉过期 observer 原因，直接显示缺目录创建入口；不能重复要求一次无法完成的冷 resume 接管，也不建立假的 controller attachment。

目录由用户在外部恢复后，“重新检查”可进入普通连接流程，无须清空它。默认不自动接管其他窗口；如果另一个窗口已经成为 controller，按当前实际 role 留在只读。

创建期间按钮显示进度、防重复点击；失败在 dock 显示具体原因，历史与草稿保持。切会话后旧请求返回不能覆盖新会话状态、显示成功通知或抢焦点。当前存活 runtime 若正在执行，历史保持原 live 显示；冷历史读取只展示已持久化事实，不虚构思考流/运行连接。

## 5. 历史读取独立于运行时

增加具体的 session 历史读取 HTTP 接缝，共用 CanonicalProtocolReader 与现有 protobuf→JSON→前端 message 投影。建议 `GET /api/sessions/{id}/history-snapshot` 与分页 action，最终路由遵循既有规范。先通过当前 memory_domain 的普通会话查询验证访问，再读 snapshot/history；不得只凭 guessed session ID 或 cursor 读数据库。

冷历史不分配 Host、writer、runtime connection、browser controller、Hook/MCP、provider call 或 fork 子会话；不伪造 host_session_id / connection generation。页面 session metadata 与 runtime attachment 是两个实际值。只读 snapshot 使用 reader 原有 cut 和 paging，不复制为新 durable history cache，不给长历史加总数上限。

消息正文、思考、工具结果、已保存图像/visualization/附件经原 canonical content/blob owner 的 scoped 只读接缝读取；全部关联到已授权 session/entry，不开放通用“任意 blob ID”端点。原文件路径预览若实体已丢失则准确不可用，不承诺所有本地文件链接仍有效。

已有 subagent task/page/activity 冷读继续复用。Plan/interaction/能力详情仅读取已有 canonical 或实际 live observation；没有 reader 的字段显示不可用，不为补齐 UI 自动 resume/discover/connect。live 观察窗口继续走现有实时协议，冷模式不新增后台执行恢复/轮询事件流。

切回 live 时替换 disposable projection，通过原 message/entry ID 与 cut 处理分页，不用两个历史 authority 双写/双 reducer。晚响应用现有 attempt/owner 引用取消或丢弃；删除会话后 read 返回 NOT_FOUND，不重新创建它。

## 6. 目录检查与身份

目录状态是实际文件系统观察，不是 sessions 新字段。`WorkspaceAvailability` 为携原路径与 typed outcome 的短期值，不带 fingerprint、不当作永久授权。

检查接缝分开两个当前产品意图：新建会话可准备新目录；EXISTING/resume 只能验证已有目录，快速开始也不能隐式创建。Core、CLI/其他 harness、fork、定时入口不能绕过这一区别。未显式通过恢复 owner 的 mkdir 一律不属于本路径。

路径只从已授权 session 的 immutable workspace row 取得。客户端不提交替代路径、workspace_kind 或 new workspace ID。创建后仍保留原绑定、记忆 domain/key 和会话 ID；不迁移模型配置、历史或权限。重新 resolve 必须与保存的 canonical root/identity 一致，符号链接重定向或路径是文件视为 UNAVAILABLE，不悄悄绑定另一处目录。

仅检查根目录存在，不扫描全树，不认为目录存在就证明旧文件仍在。新建根目录可按原路径补齐缺失父目录；创建前后核对路径与身份，使用标准库 mkdir 的排他创建/已存在分支，不删除、不 chmod 现存目录、不复制旧文件。外部文件系统变化不能与数据库原子提交，错误必须按真实结果返回。

冷打开、fork 接受前、新输入接受/queued input 消费、provider 下一调用及新工具/子代理 admission 使用同一检查 owner。UI 以当前已有连接观察和服务级定期复核更新；首版单次等待最多 30 秒，目的是发现运行中外部删目录与外部恢复，不是任务寿命/调用总数上限。实际执行仍在对应 admission 重查，不把 UI 30 秒缓存当准入权威。

## 7. 明确恢复操作与并发

一个具体 `restore_missing_workspace` owner 放在 LocalSessionController 的既有 per-session operations/retirement 路径中，复用 browser session gate、当前 controller/ generation 验证和 Core writer 获取；无第二套 handle cache、恢复 job、receipt、lease 或通用 repair framework。

操作步骤：

1. 验证访问范围、OPEN lifecycle、draining/quarantine/retirement 与当前窗口身份。已有 live browser controller 时恢复者必须是当前 controller；observer 先完成单独的普通接管动作。无 runtime 时，冷查看不预留 controller，明确恢复操作进入既有 session acquisition owner。
2. live Host 复用其 writer；冷态在创建目录前通过原 acquire owner 的具体恢复意图取得原 session 执行权，复用保存 workspace 值。该意图在 session FOR UPDATE 的同一事务中先验证 domain/lifecycle 与“不存在其他 owner 的有效 writer”，冲突返回 typed writer conflict，验证通过才执行原 generation/旧执行结算；不能 check-then-acquire，也不能调用原默认无条件 takeover。普通既有显式接管语义不在本规格扩大或替换。为此拆分 Core 的具体 existing-session 打开准备，使 workspace 检查不强迫先启动 Hook/MCP；没有新 lease 协议或伪造 guard。
3. 在 process-local 操作中 recheck。AVAILABLE 返回“目录已可用”，继续普通连接；MISSING 才创建。目录创建时不持 PostgreSQL 行锁跨文件系统 I/O。UNAVAILABLE 返回具体错误，保持历史视图。
4. 记录本操作实际是否创建了根目录；创建前后的路径、writer/lifecycle 再验证。若根已被另一方恢复，使用已有目录且不声称“刚创建空目录”。创建中若失去权限、发生目录冲突或身份改变，准确失败，不递归删除回滚已创建目录。
5. 校验仍是同一当前窗口/operation，复用 live Host 或完成普通 resume/attachment。目录恢复本身不自动发送 prompt、fork、批准权限或执行模型；既有已接受 pending 按 §8 普通队列边界处理。接管/连接结果以原 bridge 返回的实际角色为准。

同进程双击/并发恢复合并到同一 in-flight 操作；其他进程使用原 writer exclusivity。请求结果丢失后重试重新检查真实目录，存在则正常继续；不为证明 mkdir exactly-once 增加持久 command/确认记录。进程在 mkdir 后 crash 可留下目录或部分父目录，允许用户再次检查，不能承诺 crash 后“目录从未创建”。

archive/delete/reopen 与恢复复用同一 operation 互斥：先完成的已接受动作保留，其后按真实 lifecycle 处理；目录可能已创建但会话随后被删除，不为清理它新增补偿事务。原工作目录不因删除会话而被本恢复机制递归删除。

准备态 writer 由这个 existing-session 打开 operation 唯一持有，同一 host ID/KernelSessionIO 在恢复完成后直接交给 KernelHostSession；Core 不二次 acquire、生成另一个 host ID 或无条件抢回失效 writer。准备期间复用原 renew_host_writer 与 operation deadline，在实际准备阶段边界确认 guard；失效则终止而非重新 takeover。构造前失败时原 release_host_writer 按完整 guard 释放并关闭 IO；构造后按原 session.aclose/物理关闭路径结算。释放失败只报告真实不确定性并等原 lease 到期，不新增恢复 worker/清理日志。

## 8. 运行中丢失、排队与物理结算

观察原因只限制该窗口，其他 controller 和既有执行不受影响。目录 MISSING/UNAVAILABLE 则阻止新的工作区执行，不靠是否打开前端决定。

已接受 NEW_TURN PENDING 与子任务 PENDING_START/WAITING_DEPENDENCY 保持，暂停消费/启动，不伪造任务失败；未接受的真人/定时输入返回目录不可用的 typed outcome，不装作入队成功。子任务 scheduler 必须在 `accept_subagent_task_start` 之前检查；当前代码 start COMMIT 在 prepare_launch 前，仅在 prepare_launch 加 gate 已太晚。已提交 start 的任务若后续准备失败，沿原 typed settlement，不退回假 pending。

消失时正在进行的模型响应和已经开始的工具/终端/MCP 操作仍按原 owner 结算，不因 UI 变只读声称已经停止。模型结果按原 prefix 连续性入账；已接受模型请求产生但尚未启动的工具操作按原 typed rejected/failed settlement 处理。ROOT 与所有 running child 在下一 provider/tool admission 同样阻塞，包含已 prepared 但尚未发送的 dispatch；不能绕过等待启动新调用，也不能因等待丢弃 frozen handoff。

安全点等待只使用 session 当前 process-local 目录阻塞观察与普通唤醒；不新增持久 WORKSPACE_BLOCKED turn status、Plan interaction、event kind、恢复 checkpoint 或执行 replay。只读聊天禁用停止按钮；用户仍能通过侧边栏删除会话走原 retirement/物理取消。不能将归档扩大为取消运行中的任务，仍遵守 idle-only。

目录被明确创建或外部恢复，经重新检查后可唤醒原存活 Host 的安全点和原队列；不是重执行已结算工具。失去 Host/进程后只走普通 resume 的旧 generation 中断/NEW_TURN pending 处理，不恢复崩溃时 RUNNING 的调用。本产品不把目录恢复升级为执行恢复。

目录检查不是防止所有外部 rename/unlink 竞态的文件系统沙箱。检查后再丢失仍由工具/provider settlement 的原实际错误处理；不能把已执行副作用改写为拒绝前未执行。

## 9. 模型知情与 prefix

用户明确创建目录后，保存一份 advisory 提醒：“宿主在原路径重建了工作目录，原文件没有恢复；历史文件内容不代表当前磁盘仍有这些文件”。在最早可用的既有 ROOT feedback 安全点追加，不自动发起一轮回复，不保证每个模型的紧接着下一次调用都含该提醒。

复用 USER_CONTROL_FEEDBACK entry/event/origin 和现有 active ROOT append admission，但将当前仅限 process-control 的内容协议硬切为闭合的两种事实：`process_control` 保留现有语义；`workspace_recreated` 保存 session、实际原路径、创建时间与明确 UI 动作来源。workspace 分支不捏造 process ID/终止结果。codec/parser/renderer/compaction/fork 同时更新，不保留 v1 双读或一般化反馈注册表。

目录创建的 typed observation 仅存于当前 Host/恢复操作内。具体安装顺序：有存活 RUNNING ROOT 且进入普通 `_root_control_preparation_barrier` 时，按原 writer 与 active ROOT provider input admission 追加；没有 ROOT 时先保存观察，允许原 queued/new ROOT 正常 canonical admission，不要求先追加再创建 ROOT。

`runner.py` 在持有 prospective ROOT 首调用或 compaction successor 时会跳过该 barrier。本规格保留这条真实连续性边界：目录等待解除后原 frozen dispatch 原样发送与结算，提醒保留到之后第一次普通 ROOT barrier，作为消息 suffix 追加。若该 ROOT 在此前完成，则提醒保留到后续 ROOT 的合法 barrier；没有合法 barrier 或 Host 关闭前仍未安装时可以未送达。不得修改/丢弃 handoff、插入第一请求字节、创造额外模型调用、虚构 turn 或另设 prospective feedback 提交路径来兑现 stronger completeness。

此提醒为 advisory，跨 crash 未安装观察可丢失；不保存恢复标志/receipt 来保证提醒 exactly-once，不从“路径存在”推断发生过重建。只有本操作真实创建根目录才能生成 workspace_recreated。已接受确认沿现有 feedback owner 的真实边界处理，源自当前可验证观察；process_control 的原完整性与结算合同不因新 advisory 分支削弱。成功追加后模型历史按原 canonical 保留。工具仍必须检查实际文件，历史内容与恢复提醒都不证明文件现在存在。

仍只使用现有允许的 cold epoch 与 adopted compaction successor。恢复文件夹不能重写已安装 SYSTEM/tools、删历史、重排消息或默默刷新 provider tool inventory；模型知情以消息后缀追加。能力文件丢失/重新出现沿既有 capability refresh 及 prefix 边界处理，不为目录恢复发明新的 rebase。

## 10. 后端与 UI 的写边界

统一聊天写准入验证真实 connection ID、generation、session、当前 controller、operation/lifecycle，以及即时目录可用性；HTTP 当前绕过 browser role 的 fork、model binding、runtime reopen、项目能力写接口须接入同一具体 gate。现有协议 controller 检查不放宽，不允许只发 `readonly=false`、browser_instance_id 或 frontend canControl 来取得写权。

保护作用于新的动作接受边界，不回滚已经接受的动作。跨项目变更需验证实际目标 workspace 与当前 connection 的绑定，避免在只读窗口通过任意 workspace 参数修改项目。controller 丢失、窗口切换或目录状态变动时待提交编辑不自动重试写。

read 接口不因 observer 被拒绝，也不为了读取自动连 MCP、测试连接、登录或执行模型；普通已连接项目的静态配置查询可按现有 owner 读取。缺目录时 reader 返回准确不可用字段。全局设置的独立产品授权继续存在，不能用聊天 readonly 掩盖它们属于独立操作。

前端用一个明确的聊天写策略传给 workbench、messages/fork、ResponseAnnotations、composer、interaction、队列与 inspector。UI 状态是投影，后端 scope/role/lifecycle 检查拥有动作准入。侧边栏重命名/归档/删除继续使用原独立管理 owner；从聊天更多菜单移除这些入口，不在观察 dock 藏一个可调用的重载 action。

## 11. 与定时任务规格的关系

先完成本规格，再落地 `PULSARA_SCHEDULED_TASKS_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`。定时/CLI/Core 普通 resume 都不得静默 mkdir。定时准备若目录缺失按其规格的同 cut 最终失败 PAUSED；已接受队列暂停消费，既有任务配置与历史不因目录缺失删除。后台触发不能替用户点击恢复按钮。

定时任务管理页/工具的独立授权不从聊天 readonly 推导；观察窗口不能借当前聊天的模型工具调用进行管理。当前会话的归档/删除仍按用户最新规则删除绑定任务，归档拒绝无变更，取消归档不重建。目录恢复不自动恢复 PAUSED 定时任务，用户仍需显式恢复它。

## 12. 硬切、durability 与实施顺序

不新增表、session/workspace 状态列、durable event kind、subject slot、append guard、relation、durable job、恢复 lease/generation、proof fingerprint。目录观察与操作等待为 typed process-local facts；历史仍是原 canonical 关系，模型提醒沿原 entry/event。

明确内部增量为：目录状态 DTO、具体冷历史/恢复 HTTP 接缝、controller 既有 operation union 的具体恢复分支、既有写 gate、既有 feedback 内容闭合分支。这些都拥有上文的独立产品理由；实际 oracle 以仓库工具为准，如需增加 durable category 必须先修订规格。

一次硬切删除：transient resume 自动 mkdir、缺目录卡全页的打开分支、观察模式未受约束的写入口。不能靠仅隐藏按钮、保留后端旧默认或 fake live adapter 宣称完成。

实施顺序：

1. 创建/恢复意图区分与目录 DTO；明确 Core writer 获取接缝及唯一 controller operation。
2. scoped 冷历史/content reader 与 disposable 前端投影；证明完全不启动 Host。
3. 统一聊天写 gate 和所有入口；观察/目录两种 readonly dock，侧边栏独立管理。
4. 明确恢复及运行中安全点、普通 pending 唤醒、模型反馈内容硬切；保持 prefix。
5. backend/frontend 受影响回归、必要全量与真实 provider dogfood、代码 critic；完成后修订定时规格的依赖接缝。

## 13. 必须验收

- project/quick 新建正常；删除保存目录后 resume 不 mkdir、历史可分页并复制、附件按实际已保存来源读取；路径变文件/不可访问/身份重定向不会创建或覆盖。
- observer 与缺目录的全部表内禁用操作一致；键盘、拖放、菜单、callback 与直接 HTTP 请求没有绕过；fork 被拒绝前没有 child session 行。
- 顶部同一位置只显示一个状态：观察中、目录丢失/不可用优先于最近 ROOT 三种执行状态；观察+缺目录接管后标签切换、恢复后回到执行状态；正常空会话无徽标，无重复旁观/目录 badge。live/冷读/搜索/概览映射一致，子任务不覆盖 ROOT；无 ROOT、读取失败和 runtime 未连接不混同，草稿/等待默认路径移除，真实交互等待与队列独立展示。
- 已有批注可读，草稿保留，进入 readonly 关闭写弹层；切回可写恢复草稿不自动提交。
- 相同 readonly UI 组件，两种退出动作不同；真实 live 观察+缺目录先接管，再明确创建；live 已消失清掉role进入冷历史，不陷入resume接管循环；无自动接管/自动重建。
- 明确创建后原 session/workspace 绑定不变、旧文件不恢复；外部恢复不清空内容；双击/多个窗口/多个实例、同事务有效他者 writer conflict、准备 lease 转交/失效/失败释放、创建后连接失败、ack 丢失重试都按真实结果处理。
- 恢复 vs archive/delete/reopen/controller 变更；失去作用域的晚返回不改变新窗口；mkdir 后 crash 不承诺文件系统回滚，不加恢复日志。
- 根目录运行中删除：ROOT/running child 下一准入不启动新执行，已经启动的副作用正确结算，当前 live projection 与历史仍可读；NEW_TURN/PENDING_START/WAITING_DEPENDENCY 保留，子任务start前检查，已commit start用原失败结算；恢复后不重执行 CONSUMED/工具结果。
- 冷态管理在目录缺失时可重命名/归档/删除；idle-only 与 retirement 保持原限制，不申请假 runtime 只为管理。
- 模型反馈来源准确、普通进程控制反馈无回归；workspace 反馈在既有普通 ROOT barrier 追加，prospective/compaction handoff 原样使用后再追加 suffix；无 ROOT 时先正常 admission、不制造安装循环；advisory 未安装观察或无合法barrier未送达可接受；SYSTEM/tools 不变，messages append-only。
- memory_domain / entry/blob / cursor scoped 读取、分页与现有物理字节边界、只读资源读取不激活 Hook/MCP；不新增任意总 history/task/turn cap。
- 与未来定时链路一致：后台不 mkdir，已接受 pending 阻塞，任务管理与目录恢复互不隐式授权。

使用实际 controller/bridge/Core 与隔离 PostgreSQL，临时目录 rename/remove、可控时间/安全点验证；不靠长 sleep。真实 provider dogfood 只读注入保存生产设置，证明历史查看、明确恢复、模型知情与继续对话；实际 secret 值不进入证据。schema/category oracle 无新增 durable category，前端类型/构建与受影响测试通过后才激活。

## 14. 审阅与冻结

critic 重点审查：统一只读的产品例外、冷历史 scope、controller 与目录恢复的权威、writer 获取/文件系统非原子边界、安全点结算、反馈复用是否真实、prefix 与小 durability。

冻结结论（2026-10-05）：GPT-6 Astra / high critic 初审指出的四项阻断均已修订并复审通过：恢复专用 writer 同事务非抢占及 lease 转交/释放；真实 live 与冷历史的可达接管路径；已接受待启动子任务与 running child 的目录准入；目录提醒的 advisory 合同与冻结 handoff 连续性。主代理与 critic 均确认无剩余必改项。

用户随后追加的执行徽标决定记录在 §3.1 与验收：前端仅显示最近 ROOT 的三种数据库执行状态，无 ROOT 不显示徽标；这是对原草稿/等待界面投影的硬切，不增加数据库状态。

用户进一步决定观察与目录丢失也在同一顶部状态位置显示。§3.1/§4 已补齐单一标签优先级及底部恢复动作，界面不向用户解释内部状态分层；这不将只读原因写成新的数据库执行状态。

以上为规格审阅记录；实现与代码审阅另记录于 §15，不能用文档冻结代替代码验收。

## 15. 实施与代码验收记录

实施日期：2026-10-05；分支：main。实施前已提交冻结文档，提交为 `94e17da1`。本轮只实现普通会话前置，不增加定时任务表、派发器、页面或工具。

实施后的唯一 owner 与硬切结果：

- `resolve_workspace` 区分 NEW、EXISTING、RESTORE、READ。NEW 准备新目录，EXISTING 校验保存路径，RESTORE 仅允许明确恢复 owner 创建原路径，READ 无副作用读取身份。最近会话查询也走 READ；没有保留 transient resume 自动 mkdir。
- `LocalSessionController` 使用原 scoped canonical reader 读取冷历史、内容、附件和任务；冷视图不分配 Host、writer、runtime role 或 connection。目录恢复复用原 in-flight operation 与 retirement owner；原 repository acquire 事务拥有 RESTORE 的非抢占检查，准备后的同一 lease/IO/Host ID 转交普通 Host 构造。
- 聊天 HTTP 写准入复用真实 controller/generation 校验，并在异步目录检查之后再次核对 controller。目录观察不授权接管；明确 reopen 由原服务端 owner 先完成 detach，前端抑制这段期间的旧观察重连，避免接管竞态。
- ROOT、待消费输入、子任务启动和下一 provider 请求共用 process-local 目录 gate。已启动调用沿原 owner 结算；关闭先安装原关闭原因，再唤醒目录等待，避免 ROOT/child 原因被改写，也避免 prepared queue 的 shielded 等待阻止关闭。尚未消费输入不因关闭被虚构为已消费。
- 原 USER_CONTROL_FEEDBACK 内容硬切为 v2 闭合的 `process_control` / `workspace_recreated`。后者仅来自实际创建，按原普通 ROOT barrier 追加；冻结首调用与 compaction successor 保持原样。提醒保留原 advisory 边界，不增加投递记录或强完整性承诺。
- 前端共用冷/热历史读取 owner，统一只读 dock 与所有聊天写入口。顶部单一状态优先级沿 §3.1；删除草稿/等待默认映射和重复旁观提示。冷读 pending 内容在读取期间变为已消费时，沿原 typed 不可读结果展示，不因此让整页历史打开失败。

验证使用实际 Core/controller/bridge、隔离 PostgreSQL、临时工作目录以及保存配置中的真实模型。生产配置为只读输入，未重置生产数据库。

真实 provider dogfood 使用 `deepseek-flash` Chat，证据为 `output/session-workspace-recovery-20261005/dogfood.json`，复现入口为 `tests/dogfood/run_workspace_recovery_dogfood.py`。实际记录证明：冷读没有分配 Host；明确恢复后 session/workspace 绑定不变且原文件不存在；模型继续执行终端检查，并明确识别空目录及未恢复原文件。恢复后同 epoch 的三个真实 provider 请求保持 tools 与其他输入根不变、messages 为 suffix 追加；恢复前后另起合法 cold epoch。

durability oracle 保持 committed events 30、live events 24、subject slots 11、append guard 仅 HostWriterGuard、relations 29；未修改 schema 或增加 durable job。

最终验收全部通过：

- 仓库根 `.venv/bin/python -m pytest -q --maxfail=5`：2764 passed；未增加 skip/xfail 或削弱断言。56 个现有 aiohttp shutdown_timeout 弃用提示未造成失败。
- 目录恢复专项：12 passed；协议专项：25 passed。使用实际隔离 PostgreSQL 验证冷读、writer 排他、恢复、关闭与 controller 竞态。
- frontend 下 `NODE_OPTIONS=--no-experimental-webstorage npm test`：38 个文件、613 passed。该启动选项处理本机 Node 25 默认 WebStorage 与测试环境的冲突，不改变产品逻辑或测试断言。
- frontend 下 `npx tsc --noEmit` 与 `npm run build:local`：通过；已刷新仓库跟踪的静态产物。修改过的 Python 文件 `ruff check` 与 `git diff --check`：通过。
- 真实 provider dogfood：4 次模型请求全部 COMPLETED，冷读不分配 Host，明确重建没有恢复原文件，同 epoch 三次恢复后请求满足输入前缀连续性。

GPT-6 Astra / high critic 已进行多轮代码复审，修正关闭唤醒、异步准入后 controller 变更、明确 reopen 重连竞态后，确认无剩余必改项；收尾代码与文档修改也已复审通过。最终回归、类型检查、构建与真实 provider dogfood 均通过，主代理确认本轮实现可冻结。实现提交以 Git 历史为准，保存生产配置和生产数据库保持原样。
