# 能力页轻量提示与损坏能力的模型辅助处置实施规格

日期：2026-10-17。状态：已归档；已实施并完成聚焦测试与隔离真实模型验证。实施前已完成 GPT-6 Astra（high）代码对照审阅，并按后续讨论减少强制预检；验收记录见第 10 节。

## 1. 问题、目标与边界

真实会话「HTML · 本地资源冻结实测」的数据库记录显示：`pulsara-demo` 经 `INSTALL_LOOSE_SKILL` 安装成功；用户从能力页删除后询问模型，模型又运行终端及 `list_capabilities`，然后无依据地断言用户用了 `rm -rf`，并猜测存在安装管理记录残留。

当前 GUI 删除和模型管理工具复用 `LocalSkillManagementService.remove_loose_local_skill`，并不存在“能力页删除不如模型工具严格”的区别。缺口是：GUI 操作的实际来源、目标和结果没有作为简短事实交给模型。新 `SKILL_CATALOG` 快照只能说明当前有效集合，不能证明某个目标为什么消失。

历史聊天表不保存每次完整 provider 输入，不能仅凭表中没有操作提示就断言当时没有任何 Runtime observation。现有代码及隔离 compiler 复现已经确认：清单能以 suffix 更新，`SYSTEM/tools` 不变，但更新体不解释变化来源。

目标：用户在能力页完成管理操作后，模型在下一次正常 ROOT 调用中获得一条简短的、来源可靠的结果提示。提示是已完成事实，不是新的用户任务、删除授权或要求模型重复执行的指令。

同时明确外部删除后的处置方向：Runtime 负责真实观察、安全采用、权限和管理结算；模型在用户请求下，利用现有查询、文件工具及管理动作诊断 Plugin、loose Skill、MCP、Hook，并解释删除或恢复的选择。只补通现有 owner 能安全承担的入口缺口，不要求 Runtime 自动识别并修复所有手动删除。

本轮不做：通知中心、完整操作日志、文件监控、后台模型唤醒、自动修复/重装、自动清理残留、历史操作补发，或重新设计 capability 管理协议。损坏能力不新增统一 repair action、修复状态机、孤立资源注册表或安装账本。

## 2. 复用边界与唯一新增项

| 既有 owner | 继续承担的责任 | 本轮最小接入 |
|---|---|---|
| `web_app/session_controller.py` 的 GUI 管理入口 | 接受用户操作、调用真实管理 owner、返回操作结果 | 从已结算结果构造轻量事实；不从请求直接宣布成功 |
| `LocalSkillManagementService`、Plugin management、MCP management、Hook source/trust owner | 删除/发布/配置/凭据清理及其准确结果 | 复用原路径；诊断与删除准备按第 6 节最小补通，不另做删除器或修复框架 |
| `inspect_capability`、`list_capabilities` 与管理 preparation | 公开目标、观察完整性、权限效果及并发校验 | 把“定义损坏”与“管理身份缺失”分开；不因定义不可读就抹掉仍可确认的管理目标 |
| `KernelHostCore.mark_capability_change` | 按 USER/WORKSPACE 范围标记现有 Host | 同时交付已知 GUI 事实，沿用现有刷新 revision |
| `KernelHostSession._adopt_capabilities_if_requested` | 下一次 preparation 前采用变更 | 保留现有安全点及 adoption 失败语义 |
| context collector、compiler、continuity、safe point | 冻结来源、预算、编码、安装 append suffix | 加一个封闭来源，复用现有 observation 编码和安装边界 |
| `SKILL_CATALOG` / `MCP_CATALOG`、调用路由及 permission owner | 当前可见性、路由和调用许可 | 继续是执行真源，提示不替代它们 |

在 observation 链路新增且仅新增一个 context source：`ContextSourceKind.CAPABILITY_CHANGE`，合同 `pulsara.capability-change.v1`。第 6 节的诊断补通使用现有查询/管理入口，不新增公开管理 action。

其独立必要性是承载“已知管理操作的因果事实”：现有 Skill/MCP catalog 是当前状态快照；`HOOK_CONTEXT` 是 Hook 返回的非可信内容；`USER_CONTROL_FEEDBACK` 则是绑定 ROOT turn 的持久进程控制/目录恢复事实。把 GUI 操作混入这些来源会混淆来源、生命周期或不必要地扩大持久化。

新来源使用 `RUNTIME_OBSERVATION`、`TRUSTED_RUNTIME_FACT`、`ONE_SHOT`。事实可信仅指 Runtime 确认操作及结果；目标名称、路径和内容不能变成指令或权限。渲染继续用 `encode_runtime_observation`，不得伪造 `SYSTEM` 消息。

库存变化：context source enum/registry/policy 各增加同一封闭来源；数据库表/列/FK、committed/live event、subject slot、append guard、relation 和 durable job 均不增加。没有新 receipt、指纹字段、generation、lease 或确认协议。不改 clean-v0 schema。

### 2.1 按动作需要读取，不先证明能力整体健康

不把“全量发现 → exact inspect → 健康检查 → reload → 管理动作 → 再全量验证”设为调用协议。模型已有明确 target、scope 与用户意图时，可以直接调用现有管理动作；preparation 由 Runtime 补齐授权所需观察，不要求模型先查询以取得一份许可证明。

| 阶段 | 最小责任 | 不增加的前置条件 |
|---|---|---|
| preparation | 解析目标，读取本动作需要的当前值，投影权限效果，准备必要的用户输入/review | 不扫描无关能力，不要求删除目标先通过安装、连接或执行健康检查 |
| 原 owner 执行/发布 | 在实际修改边界校验当前身份、并发 guard、路径归属，并完成本动作原有的内容/凭据/信任校验 | 不因 preparation 已读过就跳过最终校验，也不在外层复制同一套 validator |
| 结果与采用 | 返回已结算结果及真实 adoption；需要时定向诊断报告的失败 | 正常成功不强制再 list、inspect、reload 或试执行一遍 |

Runtime 不回答“整个能力以后是否一定可用”，只确认当前动作能否按原合同执行。删除 Plugin 读取实例身份；删除 loose Skill 读取目录身份；MCP 沿配置 owner 读取当前 entry；Hook 按动作读取来源/信任。安装、启用或授信所需的完整内容仍由原 owner 验证，不把它变成删除、停用或撤信的通用门槛。

现有 preparation 中为确定 Plugin ID、权限效果或 review 内容而调用的 validator 可以保留；同一 owner 在执行阶段为源变化和并发安全重新检查也可以保留。只删除无独立作用的外围预检，不借本轮把所有阶段重读一概缓存或合并，不为减少读取新增跨阶段验证凭证、健康状态或损坏分类注册表。

## 3. 提示内容必须短

单次删除的 observation body 示例（外层仍为现有 `pulsara_runtime_observation`）：

```json
{"source":"CAPABILITY_UI","scope":"USER","action":"REMOVE_LOOSE_SKILL","target":{"kind":"SKILL","skill_path":"/Users/example/.pulsara/skills/demo/SKILL.md"},"result":"REMOVED"}
```

只保留以下信息：

- `source`：固定为服务端确认的 `CAPABILITY_UI`；不接受前端提交任意来源标签。
- `scope`、`action`、`target`：复用现有管理动作及查询目标词汇，使用实际落地的目标。Skill 用安装副本的 `skill_path`，Plugin 用 scope/plugin_id，MCP 用 scope/server_id，Plugin 内 MCP 保留 plugin_id，Hook 用既有 source target。唯一没有现成管理 action 的 GUI Hook 原文保存使用 observation 专用的封闭标签 `SAVE_HOOK_CONFIG`；它不增加 `manage_capability` 的公开 action 或另建配置写入路径。MCP 启停复用 `UPDATE_LOCAL_MCP`，并携带实际 `enabled`。
- `result`：真实 owner 的结算结果，如 `INSTALLED`、`REMOVED`、`DISABLED`、`CLEANUP_ATTENTION`。GUI 返回只包含上层成功码时，从同一次结算的 typed outcome 投影；不靠再扫描倒推。
- 仅有额外事实时追加最小字段：启停的 `enabled`；采用未完整时的 `adoption:"PARTIAL"` 和已有简短诊断码；连续操作合并时的 `changes` 数量。

正常结果不附带 adoption 全量对象，不重复技能清单、描述、配置、review、Hook 定义、目录树、diff、安装来源文件或使用教程。成功移除只说明这一目标；不得推导“同名 Skill 的所有副本都不可用”。

提示不得含 API key、MCP/OAuth secret/token、带认证信息的连接 URL、原始异常、配置文本或任意用户输入正文。目标采用已有安全公开 identity。凭据与目的地语义仍由原 owner 管理。

公开字段名不代表值可直接公开。GUI 结算任务用现有 `ProcessCredentialScrubSet` 净化 identity 值；`LocalSettings.credential_scrub_set()` 与 dogfood 共享实际保存凭据的提取路径，另合入现有进程凭据边界的观察。scope 仅属于当前操作和同一 settings owner：保留操作前的值，原 settings 写锁内将已读到的 current/updated 值合入，通知前再合入当前值。这样等待原生 mutation lane 期间新增、随后被本次操作移除的凭据也不会漏出；不重复获取原生 lane；结算切点不增加设置读取、秘密历史或持久状态。无法观察安全清理集合时保留正常刷新、省略该 advisory 事实，不以提示安全检查阻止原 owner 可以完成的修改。

提示不声称所有 Host 已刷新、MCP 已连接、旧工具调用已取消、远端令牌已吊销，或共享数据已删除。`result` 是管理操作结果，当前执行能力仍查本次 catalog/route。

## 4. 操作范围与结果发布

### 4.1 覆盖哪些 GUI 操作

覆盖能力页及会话项目能力面板中已经结算的同类管理动作：

| 操作 | 使用的既有结果 |
|---|---|
| Skill 安装、启用/停用、移除 | 安装/启停/删除 owner 的实际目标和 disposition |
| Plugin 安装/替换、启停、移除、连接配置 | 已确认实例与 package 对应管理结果；不输出包内容或凭据 |
| 独立 MCP 新增/导入、修改、启停、移除 | 配置变更结果；批量导入按实际已结算目标合并 |
| Hook 配置保存、来源启停、信任/撤销 | 既有保存/信任 owner 结果；信任和启用必须分开 |

预览、列表、刷新、打开目录、连接测试和 OAuth 状态轮询不算管理变更，不制造提示。OAuth 登录/取消/退出仍沿原专门流程；本轮不把授权过程转成能力变更通知。

未写入的验证失败、冲突、拒绝、用户取消继续只在原 UI 返回，不广播成模型已生效事实。`NOT_FOUND` 不伪装成 `REMOVED`。若 owner 明确报告部分发布或清理未完成，只发送该部分事实和 attention 结果，不将其降成“什么也没发生”或升级成完全成功。

### 4.2 来源认证和复用

GUI controller 在真正结算后设置来源；模型不得通过 `manage_capability` 参数伪造 `CAPABILITY_UI`。模型发起的操作继续依赖自己的 tool result 和现有 adoption，不额外给原调用者发同一条提示。

扩充既有 capability 刷新通知的 typed 参数，把可选 GUI 事实与 scope 一起交给 Core/Host。复用已有锁、shield 结算和刷新点；不得为了提示再调用一次删除、再重载一遍，或通过前端发送一条假用户消息。

事实在 owner 已结算后、同一既有服务端结算任务返回前发布；客户端断开不能将已发布操作改报失败。后续 UI 清单读取失败不撤销已结算事实。提示传播失败可以采用现有“写入已完成、刷新需注意”的返回语义，不回滚真实能力变更，也不建立持久重试任务。

USER 事实分发给结算时已经注册的所有 live Host；WORKSPACE 仅同一实际工作目录。`active_session_id` 不能扩大范围，也不能授权操作其他目录。

本轮仅向这些 Host 的 ROOT provider scope 提供因果提示；子代理继续使用原有当前 capability 视图，不为每个子代理复制 GUI 操作历史。

## 5. 轻量进程内状态，不建设通知队列

每个 live Host 只保留一个尚未提供给 ROOT 的合并摘要：**最近一次实际操作的完整简短事实 + 合并操作数量**。不保存每项操作明细，不为“通知完整性”创建队列、日志、ID、时间线或重放协议。

这是明确的产品取舍：单次操作能准确归因；连续多个 GUI 操作以 `changes:N` 和最后一项结果概括，完整当前状态仍看 catalog。模型不能把最后一项目标说成其余 N−1 项的目标，也不能声称掌握完整操作历史。相同目标的启停/修改可自然合并成最近结果，不保留中间事件供模型重演。

数量字段是聚合计数，不是容量上限；不限制用户可操作目标数、Host 生命周期或会话时长。没有固定“最多 N 条提醒”或溢出后拒绝能力操作的路径。

摘要与既有 `_capability_refresh_requested_revision` 的当前值一起冻结。revision 仅作本地 preparation/去重边界，沿用现有值，不写入提示 body，不增加 DTO fingerprint。若两次已分开发送的操作文本相同，也必须能区分其不同已有 revision，不能被 ONE_SHOT 的内容去重误吞。

### 5.1 冻结、安装与清理

1. 既有安全点采用来源后，在当前 dispatch preparation 冻结 pending 摘要及已有 applied/requested revision，作为纯 typed value 传入 context source；collector/compiler 不自行读取 GUI、文件系统或可变 controller 状态。若最新摘要对应的 requested revision 晚于本次已处理的 applied cut，说明操作发生在采用之后，整份摘要留给下一次正常 preparation；不临时重载或改写已冻结的请求。采用失败但已完成该 cut 的尝试时，可以携带原事实和 `adoption:"PARTIAL"`，不能宣称已全部采用。
2. `CAPABILITY_CHANGE` 作为普通 append observation 加入本次输入。source placement 放在本次 Skill/MCP 快照之前，不改变其他既有顺序或根前缀。
3. 只有 `install_provider_input_candidate` 成功安装，且该冻结摘要以 `FULL / VALUE` 实际装入 append suffix 后，才从 pending 摘要扣除此次冻结的操作数。`UNAVAILABLE_MINIMAL` 即使作为 unavailable observation 被选中并安装也不能消费；preflight、预算估算、dry projection、编译失败或安装前取消同样不能消费。
4. 若冻结后发生新操作，在同一 Host 锁下保留其最近事实及剩余数量，不清掉较新的变更。不以“网络发送成功/模型已读”另建 ACK；安装后的消息已属于同一 epoch 的 append-only 前缀，失败重试沿原前缀继续。
5. 后续没有新操作就不再发同一提示，也不追加空的 `CLEARED` 通知。只扩充现有 ONE_SHOT 缺省处理的封闭来源集合，不能顺手改变 PLAN_HANDOFF 等其他来源语义。

`CAPABILITY_CHANGE` 使用现有 IMPORTANT source 预算及 `FULL / UNAVAILABLE_MINIMAL` 模式。FULL 就是上述短字段，没有额外长文版本。若完整目标连同必需上下文不能装入当前模型物理输入预算，使用原 unavailable/未选中处理，继续正常请求并保留 pending；不得截断目标后仍把提示消费，不得让无上限通知工作饿死用户请求。无需为提示发起额外模型调用。

### 5.2 生命周期保证

切换前端页面不丢 live Host 的 pending。空闲时不唤醒模型，下一次正常 ROOT dispatch 才提供。已经准备并安装的旧请求不被重写；之后的调用处理新变化。

Host 关闭、进程重启、丢失 process-local 状态后允许提示丢失；重新打开会话读取当前真源，不编造旧 GUI 操作。没有 live Host 时不为了通知创建 Host，也不保存供将来会话消费的用户级日志。

compaction/cold epoch 继续使用已批准边界；不得为了能力提示重建已有 `SYSTEM/tools`。已经消费的短提示不变成新的 durable handoff 项。若模型未看到因果信息，只能说明当前状态及信息边界。

## 6. 外部改动与模型辅助处置

### 6.1 分工、实时性与用户意图

外部文件操作没有通过管理 owner，不能获得 `CAPABILITY_UI` 来源，也不能因为“少了一个条目”推定发生了正式卸载。反过来，用户口中的“手动删除”也可能指手动点击能力页；模型不能自动解释成 `rm -rf`。

| 实际外部改动 | 下一次既有观察/重载时能确认的事实 | 不得推断或自动做的事 |
|---|---|---|
| 删除 loose Skill 安装目录或 SKILL.md | loose 扫描发现候选消失/无效；重新解析优先级，可能露出同名较低优先级副本 | 不标 GUI 删除，不保证同名技能全消失，不臆造安装账本；不自动 REMOVE 或重装 |
| 仅删除创作/下载源目录 | 已安装副本可以保持可用 | 不把源目录消失当卸载，不删安装副本 |
| 删除启用 Plugin 的受管理 package/manifest | Plugin owner 可报告 `UNAVAILABLE`；当前 enabled view 不能再作为完整有效清单 | 不报正式 `REMOVE_PLUGIN`；不声称实例状态、凭据、数据或旧消费者已经清理 |
| 仅删 Plugin data 或安装来源目录 | 可能是资源缺失，也可能不改变包声明 | 不把数据损坏/来源消失等同 Plugin 卸载 |
| 从独立 MCP 配置移除 entry，或删整个配置文件 | 由现有配置 loader 区分有效空配置、entry 缺失和不可读；重载采用新状态 | 不声称专用凭据/本地 OAuth grant 已清除，更不声称远端令牌吊销 |
| 删除 MCP stdio 可执行程序，或远端服务停止 | 配置可能仍存在，但连接/调用不可用 | 不把连接失败说成用户删除了配置；不重放有副作用的旧调用 |
| 改坏 MCP YAML、Plugin 状态/manifest，或出现权限错误 | 解析失败/来源不可用，而非完整空清单 | 不拿旧清单证明可用，不自动重写配置/注册表或扩大权限 |
| 删除/修改 Hook 定义文件 | 既有发现、信任及 reload 决定 EMPTY/UNAVAILABLE/需重审 | 不自动恢复信任或执行新定义，不归因为 GUI 操作 |

实时性沿现有 owner：loose Skill 在 provider preparation 的源冻结阶段重新扫描；Plugin/MCP/Hook 的已采用视图可能需要显式 `reload_capabilities` 或原有重载入口才反映外部文件变化。GUI 列表刷新/inspect 只是读取，不能宣称它必然刷新了所有 live 执行消费者。

本轮不加文件 watcher、轮询或“每个调用先全量 reload”。外部删除到重载之间，已打开的连接或包引用可能仍存在；正在进行的调用按既有结算合同收尾。重载后继续使用原有 source-unavailable、route fencing 和同 epoch meta/native 规则，旧 provider schema 留在前缀里不等于仍有执行权限。

没有已知 GUI 事实时不新增一条伪造的删除提示；使用现有 current catalog/diagnostic。需要追查时优先 `inspect_capability` 查询已知 target；只有目标未知、完整性不足或确需发现时才 list。Plugin/MCP 的 out-of-band 变更需要刷新已采用视图时，使用既有 reload。来源不可读/清单不完整时不能以“没列出来”证明删除成功。

若正式管理 owner 或现有配置确实显示专用凭据、孤立规则或清理 attention，可准确报告其具体事实；否则不能泛称“手动删就会留下安装记录”。任何补清理都必须是用户实际请求下的现有管理操作，本提醒本身不授予该权限。

模型按以下顺序处理，不将全部步骤变成每次必跑的检查单：

1. 用户要求诊断时优先 exact inspect，区分声明、管理身份、实际文件和当前采用状态；只有查询不能解释问题时，才检查 owner 给出的具体路径、相关文件或连接诊断，目标未知才 list。用户已明确要求管理操作且 target/scope 足够时，可以直接管理，不要求先 inspect；管理结果已指出具体问题时也不重走全量发现。
2. 报告可确认的损坏，例如“实例仍在，manifest 缺失”或“配置仍在，可执行文件缺失”。目录不存在不能证明谁删除了它；未知状态不能降成空清单。
3. “帮我排查”允许只读诊断，不自动扩展成卸载、覆盖配置、重新安装或恢复信任。需要改变用户状态时说明具体选择；用户已经明确要求删除或重装的，沿现有权限/确认路径执行，不额外重复索取同一授权。
4. 修复采用原 owner 的动作。重装前用已有公开只读路径核对新来源/配置可获得且与目标匹配，说明哪些数据、启用状态和信任需要重新处理；不能假设被删的源文件仍可找回。完整有效性仍由正式安装/配置动作的原 validator 判断，不增加模型必须先调用的预检步骤。
5. 先读管理结果中的结算、清理与 live adoption，分别报告其含义；正常成功不强制附加查询或试运行。只有结果不完整、与观察矛盾，或用户明确要求验证可用性时，才补充定向检查。只重试明确未结算的步骤，不因采用失败重放已完成的删除，也不重放有副作用的旧工具调用来“验证恢复”。

模型自身操作由 tool result 反馈；外部文件修改不伪装成 `CAPABILITY_UI`。模型可以提供恢复选择，但不承诺任意损坏都能完整清理。边界在身份与原 owner 能否准确结算，而不在模型是否能直接访问目录。

### 6.2 当前实现核对与本轮范围

| 类别 | 已有可复用路径 | 当前真实缺口 | 本轮处理 |
|---|---|---|---|
| Plugin | 实例 store、原生 `remove_local_plugin`、凭据 owner、package 安装/GC | exact query 与模型 preparation 要求完整 package；manifest/package 缺失会挡住删除；替换还会读取旧 package summary | 补通只读实例诊断及按实例准备删除；核对新来源后 REMOVE → INSTALL |
| loose Skill | 按安装目录身份准备/删除、原生安装验证与独占发布 | `SKILL.md` 缺失时查询可能看不到仍存在的安装目录；整目录已消失时模型删除 preparation 会失败 | exact inspect 补充允许根内的目录级诊断；删除复用原 owner，已不存在不伪造成功 |
| 独立 MCP | 配置 parser、exact target、mutation guard、连接/凭据/OAuth owner | 配置有效但程序离线仍可删除；坏 entry、坏 YAML 会挡住管理准备；entry 已消失则不能 REMOVE，但可普通 ADD | 有效配置沿 UPDATE/REMOVE，缺失配置沿 ADD 重建；不可解析配置先诊断止步，不新建孤立清理协议 |
| Hook | source provider、原文保存、source/trust 管理、reload | 没有 `REMOVE_HOOK`/`INSTALL_HOOK` 动作；定义、信任和脚本是不同对象 | 使用现有撤信/停用和本地定义编辑路径；不新增 Hook 安装器，不把停用说成删除 |

简化后的共同判断只有三步：先确认具体 owner 与目标；对象仍在时用原管理动作，完整确认缺失且用户要恢复时用普通安装/新增；身份或配置无法确认时报告具体限制。Hook 的恢复对应定义/脚本编辑而非虚构安装动作。该判断只写入现有技能指导，不实现成跨类别状态机，也不要求模型对每个目标逐项运行所有检查。

隔离复现已确认：Plugin manifest/package 缺失时模型 REMOVE/replace preparation 失败，但原生删除可以完成，之后重新安装能准备；Skill 文档损坏或缺失但目录仍在时 REMOVE 可以准备；MCP 程序缺失但配置有效时 REMOVE 可以准备，坏 entry/YAML 或 entry 缺失时不行，文件/entry 缺失后普通 ADD 可完整执行；本地 Hook 定义缺失、坏 JSON 或 trust 状态损坏时，现有撤信/停用可完整执行。准备成功不等于执行、清理或 adoption 已成功，实施测试必须覆盖后续结算。

实现落点保持集中：模型入口在 `conversation_kernel/capability_query.py`、`capability_management.py`、`capability_management_execution.py`；Plugin 身份/结算复用 `plugins/inspection.py`、`package_store.py`、`connection_management.py`；Skill 目录与删除复用 `capability/local_skill_management.py`、`local_skill_removal.py`；MCP 复用 `capability/mcp_management.py` 与 `mcp_config.py`；Hook 复用 `hooks/source.py`、`trust.py`。以上均相对 `src/pulsara_agent/`，不建立跨类别 repair service。

### 6.3 Plugin：实例身份与包内容分离

Plugin 诊断分成“实例状态能否读取”和“package 能否验证”两个既有 owner 事实，不新增第二套实例真源：

- 从实例 store 的既有锁与路径解析读取 exact scope/plugin_id、当前 package_install_id、enabled 和可公开路径；package 检查继续由现有 inspection/validator 承担。包缺失或 manifest 损坏时，query 保留可确认的实例信息及 unavailable 诊断，不构造一个空的有效 manifest，也不声称 components 完整。
- 使用现有 query 的 target 和观察完整性/diagnostic 字段承载；确需添加公开字段时，只投影上述仍存在的 typed 值，不复制私有 overlay、settings 或凭据。组件内容未知时不提供依赖内容的配置/启用准备。
- `REMOVE_PLUGIN` preparation 不再依赖完整 package summary，改从同一实例 owner 冻结当前实例值及已有 package_install_id guard；复用 `PreparedPluginInstanceObservation`，同时调整 execution 的原观察适配，不伪造健康 `plugin.summary` 来满足旧字段。执行仍走原 `remove_local_plugin`。执行切点继续检查实例及 guard，目标已变更则冲突，不删除另一个新安装实例。
- 包不可读不能推断没有进程效果。有完整现成组件观察时复用原 `_has_process` 效果投影；内容未知且实例 enabled 时直接保守声明 `process_control:true`，不为此扫描 live consumers 或增加观察 owner。禁用实例沿既有语义；执行仍走原删除与 adoption，不绕过权限和副作用结算。
- 正式删除负责移除实例、清理该 scope/plugin_id 的已知专用凭据与本地授权，并报告 cleanup attention；包引用释放/回收沿原生命周期。数据目录、原始来源、其他 scope 实例和共享包不由模型 `rm -rf`。不声称撤销了远端令牌。

重装先用公开只读路径核对新来源可获得、格式与目标身份匹配，随后正式 REMOVE，再普通 INSTALL，最后沿既有启用/连接/Hook review 路径采用。该流程不是原子 replace：删除完成而安装失败时，准确报告“已卸载，安装未完成”，保留来源与数据，不自动回滚或循环重装。旧 package summary 已不可读时，不使用依赖它的现有 replace 路径；健康实例仍沿现有替换路径。

不要求模型先调用内部 validator、导入仓库私有服务，或运行带外 CLI；也不新增 validate action。已有公开预览/验证只在可用且确有帮助时使用。公开来源核对不锁定未来内容，最终 INSTALL 仍由原 validator 完整验证和独占发布，不增加 source receipt、逐文件 hash 或预留任务来保证两步原子性。不得为了预检而先执行一次会修改旧实例的安装。

实例状态本身不存在、不可解析、身份无法校验或路径不符合 owner 约束时，不能仅凭目录名拼出实例、凭据归属或 expected guard。报告可定位的诊断，必要时与用户讨论恢复可信状态/来源；本轮不绕过 store 手工删注册文件或批量清凭据，不声称完成卸载。

### 6.4 Skill：文档、安装目录与来源副本

loose Skill 删除对象是允许根下的具体安装目录，而不是某个成功解析的 Skill 文档。现有 `inspect_loose_skill_removal` 已按目录身份准备删除，因此 `SKILL.md` 格式错误或已消失、目录仍在时，可以复用 `REMOVE_LOOSE_SKILL`，不先补造文档。

exact inspect 对已知安装路径补充目录级诊断和既有 removal eligibility：明确文档缺失/无效、目录是否仍在、原 scope 是否允许删除。只调用已有根策略及 no-follow 目录观察，不扫描任意父目录，不把残留目录注册成可用 Skill。查询无法确认的权限/IO 问题仍为 unavailable。该诊断是按需读取；已知目标的 REMOVE 直接复用原目录 preparation，不要求先经过这次 query，也不要求先完成全局同名副本选择。

删除继续使用目录 device/inode 校验、原有隔离改名和配置 override 清理；symlink、路径越界、目标被替换时沿原拒绝/冲突语义。USER 允许的 Pulsara/.agents 根、WORKSPACE Pulsara 根以当前策略为准；WORKSPACE `.agents`、bundled Skill 和 Plugin 内 Skill 不因此获得 loose 删除资格。

若安装目录已全部消失，观察事实就是目录不存在；没有可冻结的删除身份，不报一次新的 `REMOVED`。可能存在的启用 override 只在 owner 实际观察到时报告；不创造安装账本，也不为了“完整清理”清除其他同名副本的规则。原生删除已结算但 override/目录清理失败时，保留其 attention 结果。

重装先核对新来源及相同 scope/destination，目录仍在则正式删除后安装，已不存在则直接走普通安装的验证和独占发布。旧启用 override 可能仍影响新副本，必须读取真实结果，不能保证重装自动启用。同名低优先级 Skill 可以重新显现；“删除这个副本”不等于“删除全部同名 Skill”。来源目录被删但安装副本仍在时，不需要重装。Plugin 内 Skill 损坏按 Plugin 处理，不单独修改受管理 package。

### 6.5 MCP：配置、进程与凭据归属

配置有效、但 stdio executable 缺失或远端服务不可达时，exact inspect 和 `REMOVE_LOCAL_MCP` 仍走原配置 owner。模型可以根据用户请求恢复命令/安装依赖、修改 endpoint，或者正式移除连接；删除连接不负责删除可执行程序，不等于停止远端服务。凭据通过原私有输入与绑定流程恢复，不从聊天记录补造 token。

重建连接先核对新的公开配置候选和目标，由正式 mutation 完成原验证；相同 entry 仍有效时优先 `UPDATE_LOCAL_MCP`，无需为了重连而卸载。用户要求卸载后重装时，依次 REMOVE → ADD，保留现有 destination 确认、credential owner、OAuth、工作区授权和 guard 校验。私有凭据/OAuth 未完成时只报告配置已恢复，不宣布连接可用。

若文件或 entry 已消失、loader 能完整确认目标不存在，用户明确要求恢复时直接普通 `ADD_LOCAL_MCP`，不必先带外恢复旧 entry，也不必先做一次必然冲突的 REMOVE。沿原 ADD 的配置验证、私有输入、同 owner 凭据处理和采用结果结算；不能推导旧 grant 已清除或可直接复用，连接认证仍按实际结果验证。若用户只要求删除，则准确报告配置已不存在，不凭这一事实宣布其他清理完成。

以下情况明确保留限制，不假定现有 REMOVE 能处理：

- YAML 可读但目标 entry 不符合语义 parser：目前 inspect/remove 都可能失败。模型可诊断具体错误并提出最小配置恢复方案；本轮不在 preparation 中把无效 entry 强行解释为有效连接，也不绕过 effects/credential 校验直接 pop raw entry。
- 整文件缺失或 entry 已被外部移除：当前 loader 可能得到空集合，但这不能证明私有凭据和本地 grant 已清理。现有 REMOVE 对不存在的 entry 会冲突；不能反复调用并声称清理成功，也不能从公开 inventory 缺席推导 secret 不存在。
- 整体 YAML 损坏、不可读、owner 绑定冲突或身份无法确认：不能覆盖为空配置或清掉其他 entry。恢复配置只有在用户授权、可信原文/来源可用且能保留其他内容时才考虑；恢复后回到 parser 和管理 owner。无法确认则说明需要用户提供来源或处理配置，停止该修改步骤。

模型可沿已有文件工具与权限边界修复用户源文件、命令或依赖，或生成供检查的公开配置候选；不直接改受管 MCP YAML 来绕过 mutation preparation，不通过文件编辑替代受管理凭据、OAuth 或连接删除 owner。管理 owner 已无法解析仍存在的旧配置时，本轮只能诊断、给出候选并与用户讨论配置恢复，不能假称完整模型自助恢复；用户在带外恢复有效配置后，再回到原管理路径并按需 reload。这一限制不适用于上文可完整确认缺失的普通 ADD 重建。不得把私有 settings 原文或密钥带进提示。若后续确实要求“entry 已消失仍清除孤立凭据”或“坏 entry 直接正式删除”，必须另行明确原 MCP owner 的输入、归属、权限效果及并发结算合同；本轮不承诺该能力，不新增后台清理器。

Plugin 内 MCP 由 Plugin 实例与 package 声明拥有。连接故障使用既有 Plugin MCP 配置/授权入口；包声明损坏走 Plugin 流程，不能把组件偷偷注册成独立 MCP 来掩盖故障。

### 6.6 Hook：定义、信任与脚本分别处理

本地 Hook 来源路径由 source owner 解析。定义文件缺失可表示完整空来源；JSON 损坏/读取失败表示 unavailable。两者都不能证明以前的 trust 已删除，也不能自动恢复执行。

已有 `REVOKE_HOOK_TRUST` 和 `SET_HOOK_SOURCE_ENABLED(enabled:false)` 可在本地定义损坏/缺失但来源身份仍可确认时准备，执行继续重查同一来源和既有 trust owner。该过程可能改变信任/启用状态，不删除配置或脚本。trust 状态损坏时沿现有显式 mutation 的处理语义，不另写恢复器；信任状态不可确认不能被解释成已授权。

显式 mutation 可能按 trust owner 原语义用默认值重建不可读记录。因此操作后采用视图中的启用值只能证明当时状态，不能反推操作前状态，也不能证明外部曾重新启用。bundled Plugin installer 与既有 Hook 参考文档应简短说明这一点；不为解释默认值增加旧状态账本或新的结果协议。

`manage_capability` 的既有说明明确 `current/adoption` 是操作后事实，不描述操作前状态。这是同一结果语义的澄清，避免模型将重建后的默认值当作历史证据；不增加结果字段或 Runtime 预检。

真实模型验收与实现复审暴露并修正了一个展示歧义：定义不可读、trust 不可读或 trust 状态无效时，模型查询与本地/Plugin reload/adoption 返回的 `enabled` 为 `null`，而非把内部保守默认值 `true` 说成确定事实；当前操作的结算结果分别明确给出 `enabled:false` 或 `trust_revoked:true`。这些模型出口共用现有 presentation 层的小型投影函数，不改变 GUI review、trust owner、执行判定或增加状态读取/修复路径。

本地“删除 Hook”是删除指定定义/规则或用户明确要求的整个本地来源，通过已有本地文件编辑路径完成；GUI USER 原文保存继续使用 `read_user_config`/`save_user_config` 的完整旧值比较与原子发布。保留其他规则及未修改内容，不为了删除一条规则写成空文件。无可解析旧定义时先诊断/讨论恢复或删除整个已确认来源，不能猜测哪些规则可丢弃。路径替换、不可读或并发修改按原机制失败，不绕开 no-follow 与 owner 约束。

Hook 没有统一安装包语义；“重装”应具体说明恢复定义、恢复命令所用脚本/依赖，还是重装所属 Plugin。恢复本地定义后由既有 parser/reload 验证；需要信任时仍走完整当前 GUI review，即使 full access 也不能自动授信。启用、信任、Plugin 启用与 live adoption 分开验证。

现有信任依据覆盖规范化定义，不包含被调用脚本的逐文件内容摘要。仅脚本丢失/变化可能导致执行失败而不改变定义 trust；模型应据实诊断，不能声称 Runtime 必然发现并撤信。本轮不增加脚本 hash、自动扫描或新 trust 合同。模型恢复脚本仍遵守用户任务授权，不能将一次排查扩展成执行任意 Hook。

Plugin Hook 定义属于不可变 package，不用文件工具局部修补。完整 Plugin 组件观察可用时，可独立使用既有 source 停用/撤信；包损坏使完整 inventory 不可用时，即使实例身份仍可读取，也不保证组件级 Hook 操作可用，恢复走可校验的实例级 Plugin 流程。本轮不额外补一条绕过 package 观察的 Plugin Hook 修复路径。

## 7. bundled Skills 与基础规则

实施时修改下列现有技能，不新增能力管理技能或额外 activation：

- `src/pulsara_agent/bundled_skills/pulsara-skill-installer/SKILL.md`
- `src/pulsara_agent/bundled_skills/pulsara-plugin-installer/SKILL.md`
- `src/pulsara_agent/bundled_skills/pulsara-mcp-installer/SKILL.md`
- `src/pulsara_agent/bundled_skills/pulsara-skill-creator/SKILL.md`

前三份在既有 Verify/diagnose 段各增加同一短段，creator 在安装/验证附近引用相同规则；不复制本规格的操作矩阵或实施细节。建议正文：

> Capability-page actions may produce a brief Runtime result notice. Use its reported origin and outcome; do not repeat the operation. Catalog changes alone do not identify who changed a capability. Inspect or reload only when needed, and never invent leftover installation records.

损坏诊断再补一小段共同预期，不将复杂修复指导塞入每次 observation：

> Missing files do not prove uninstallation. For diagnosis, inspect the exact target; known-target management can proceed directly through its normal action. State ownership or cleanup limits. Diagnosis alone does not authorize uninstalling, reinstalling or restoring Hook trust.

各技能只保留本领域必要边界；详细例子放已有 references：

| 现有 Skill / reference | 本轮需要补充的具体预期 |
|---|---|
| Skill installer 的既有替换/诊断段 | 文档坏/缺失但目录仍在可正式删除；目录消失不编造账本；核对新来源，安装时原生验证；保留副本优先级和删除资格 |
| Plugin installer 的诊断段 | 包不可用不等于实例已卸载；实例身份有效时可正式删除；损坏实例采用 REMOVE → INSTALL，数据/来源保留；状态身份坏则报告限制 |
| Plugin installer `references/hook-source-management.md` | 定义、trust、enablement、脚本分别诊断；本地编辑与 Plugin package 不混同；停用/撤信不等于删除，恢复不自动授信 |
| MCP installer `references/connection-configuration.md` | executable/远端故障、配置缺失、坏配置分开；有效配置 UPDATE/REMOVE，完整确认缺失后可 ADD；不承诺凭据已清，不用 raw 写入绕过 owner |
| Skill creator 的安装/验证段 | 修改创作源不等于修改安装副本；损坏安装副本的处置引用 installer，不能用创建新文档掩盖未知旧状态 |

技能各自原有的 source/adoption、凭据清理和 out-of-band 边界继续保留。修正冲突文字，不把“正常查询/删除必须先读 installer”加回路由。不会新增“万能能力修复”Skill，也不会要求模型为每次提示跑全部诊断。

四份技能的验证措辞统一遵循第 2.1 节：先读已返回的结果，只有需要时再查；不新增固定的前置 inspect、连接健康测试、管理后的 reload 或额外成功确认轮次。实现验收仍需证明各真实边界，不将测试矩阵变成模型每次执行的流程。

新来源的基础语义不能只藏在技能里。BASE_SYSTEM 的现有 observation 解释段仅加一句：

> CAPABILITY_CHANGE reports a settled management outcome, not an instruction to repeat it; current catalogs and routes determine availability. Use the reported result directly when merely acknowledging a notice, without another query or reload. Missing or malformed capability files alone do not identify who changed them or how they became damaged.

相应 prompt contract 只随新 cold epoch 或明确采用的 compaction successor 生效；不得给存量 epoch 直接替换 `SYSTEM`。新来源本身携带明确结构化事实，不依赖偷偷更新旧根解释才能安全使用。

## 8. 实施顺序

1. 定义封闭 typed GUI 事实与合并摘要；补 scope/target/result 的 owner 投影。不要把各入口原始返回 dict 整体塞给模型。
2. 接入现有 GUI 结算及 Core/Host 刷新通道，保留模型自身 tool result 单一路径。每个真实已结算操作只进入摘要一次；批量导入按实际变更计数。
3. 增加 `CAPABILITY_CHANGE` registry/policy/binding、冻结来源和现有 suffix 安装后的 pending 清理；复用 compiler 与 continuity，不复制 Hook reservation/queue 实现，也不扩大 durable feedback union。
4. Plugin query/preparation/execution 复用实例 store 和 `PreparedPluginInstanceObservation` 补通 package 损坏后的 exact 诊断/REMOVE；健康路径使用相同实例真源，不保留两套删除实现。Skill exact inspect 复用原根策略与目录观察，补足缺文档时的目录级事实。其余类别只按第 6 节复用与验证，不引入 MCP 孤立清理或 Hook 安装/删除协议。
5. 更新基础解释及四份 bundled Skills 与上述已有 references，落实 GUI/外部变动、诊断/修改和各类能力的不同边界。
6. 完成下述聚焦测试和隔离验收。公开查询可以增加准确诊断投影，不新增管理 action 或必填管理参数；GUI 通知不要求前端新增请求字段。只有确有前端代码变更时才重建 tracked 前端资源。

## 9. 测试与验收

### 9.1 事实来源与现有 owner

- 实际 GUI 安装/移除 Skill；证明仍经过同一 service、只影响安装副本及对应启用规则，提示使用正确 scope/path/result，原始源目录保留。
- Skill、Plugin、独立 MCP、Plugin MCP、Hook 各至少覆盖一个实际 GUI 结算入口。取消/冲突不报生效，部分发布/清理 attention 不报完全成功。
- GUI provenance 只能由服务端确定；工具参数、Skill 文本、用户聊天或外部文件不能伪造该来源。工具自身操作不重复发给原调用者。
- 密钥、OAuth token、秘密 URL、配置正文和 exception body 均不进入提示；公开 target 保持可准确定位。

### 9.2 preparation、prefix 与生命周期

- 抓取真实发送对象：旧 Skill 清单 → GUI 删除 → 下一次正常 ROOT 请求含一个短 `CAPABILITY_CHANGE` 及当前清单。`SYSTEM/tools` 字节不变，messages 仅追加；不需要模型再 list/terminal 才知道来源。
- 后续无新操作时不重复发，也不生成 CLEARED。分开两次内容相同的真实操作可借既有 revision 各被提供一次。
- 多次 GUI 操作合并为最近事实与准确数量；不伪称模型看到其余目标明细。大数量不拒绝操作或制造无界逐条提示体。
- freeze 后新操作、重载失败、preflight 失败、安装前取消、安装后网络失败分别验证 pending 保留/消费边界；新变更不能被旧 candidate 清空。
- pending 完全放不入预算时正常用户调用仍可继续，目标不截断，pending 不误消费；空闲不启动模型，不为发完提示增加调用。
- USER/WORKSPACE 与 ROOT/subagent 分发范围正确；导航不清 pending，Host 关闭/重启不伪造补发，compaction 不复活旧操作。

### 9.3 外部改动与行为验证

- Plugin：分别删除 manifest 和整个 package；exact inspect 保留可信实例身份并标记内容不可用；模型 `REMOVE_PLUGIN` 真正经过原 owner 结算，专用凭据及本地授权清理准确、data/来源及其他 scope 不变，既有消费者引用正常释放。启用且组件未知时保留 process-control 权限，不新增消费者扫描；prepare 后实例被替换必须冲突。删除前核对新来源、最终安装执行原验证，不要求额外 validator 工具；移除后安装失败保留“已移除、未装好”的事实；实例状态损坏/缺失不能假造 guard 或扩大清理。
- Skill：文档坏、文档缺失但目录仍在时 exact inspect 可诊断，随后原 REMOVE 执行；整目录已不存在不假报 REMOVED。覆盖残留 override、清理 attention、同名 fallback、源副本分离、WORKSPACE `.agents` 不可删、symlink/越界/目录身份更换和安装独占发布。
- MCP：配置有效而 executable 缺失/服务不可达时，inspect 和正式 REMOVE/UPDATE 仍可使用；移除后的配置、专用凭据/本地 grant 以及其他 entry 分别核对。文件/entry 缺失且 loader 完整时，用户要求恢复可直接 ADD，验证同 owner 私有输入与认证结果，不先 REMOVE。坏 entry、坏 YAML 保留真实错误；不自动清空文件、不通过 raw 写入绕过 owner、不宣称孤立凭据已清。Plugin MCP 不自动转成独立连接。
- Hook：本地定义缺失/坏 JSON 时分别观察 EMPTY/UNAVAILABLE，并验证撤信/停用的实际执行和 adoption，不能只测 preparation。覆盖来源并发变化、trust 状态损坏的既有显式处理、删除一条规则保留其他规则；恢复后按原 trust 规则判断，需重新授信时必须经过正常 GUI review，不能自动授信。脚本内容变化不伪称一定使定义 trust 失效；Plugin 包定义不允许 loose 编辑，包不可用时组件级限制明确。
- 上述观察保留真实完整性与现有 fencing；不能出现 `CAPABILITY_UI`、正式卸载或凭据已清除的虚假声明。GUI inspect 与 live adoption 不混同。权限/IO 错误不降为空集合，用户只要求排查时不得自动修复；已有明确删除/恢复授权时不重复索取。
- 四份 bundled Skill 的短文与基础解释相互一致，不要求无条件全量 list、reload、安装或清理。直接改受管理 Plugin package/MCP 配置以绕过 owner、伪造实例/凭据身份和自动恢复 Hook 信任必须被明确排除；授权的本地 Hook 定义编辑仍走第 6.6 节。
- 对已知且已授权的目标，直接调用现有管理入口即可准备/执行，不需要前置 list/inspect 或整体健康证明；正常成功结果不触发额外自动查询/reload/模型调用。该减法不能削弱发布切点的身份、权限效果、并发、路径及本动作原有校验。

最后用保存的生产配置作为只读输入，在临时 Pulsara home、经验证的 disposable 本地数据库和独立 demo 目标中做真实 provider dogfood：安装 → 能力页删除 → 用户询问是否可见；确认模型正确归因为能力页操作，不运行无必要的全量清单/终端，也不猜测管理记录残留。

再用隔离的损坏 Plugin 做一轮对话：用户先只要求排查，模型准确定位 package/manifest 与实例状态，提出删除或重装选择而不先执行；随后明确授权重装，验证模型核对新来源后沿原管理入口删除并普通安装，完整验证由 INSTALL 负责。Skill/MCP/Hook 各做针对其边界的诊断场景，MCP 缺失配置可普通 ADD、坏配置必须准确承认限制，Hook 不自动恢复信任。能力变化实际触发后，验证后续同 epoch 请求的 `SYSTEM/tools` 字节不变、messages 仅追加；管理可见性恢复不靠重建 provider 根。

保留实际请求 suffix/回复与测试结果，按现有 secret scrubber 只去除实际凭据；不触碰用户真实能力配置。当前隔离复现材料位于 `output/diagnostics/capability-skill-deletion/`，属于本地诊断证据而非新增持久化产品机制；实施验收需重新证明最终代码行为，不能用这些准备阶段结果代替。

完成标准：单次已知 GUI 操作能被下一次正常调用准确、简短地解释；连续操作有明确轻量降级；外部损坏能由模型按真实身份诊断，明确授权后复用原路径处置，无法确认的清理与恢复准确止步；现有 capability 执行与 prefix、durability 合同保持。


## 10. 实施与验收记录（2026-10-17）

- GUI 结算后的事实接入原 `mark_capability_change`；复用原 mutation lane 与 `_settle_capability_io`，明确哪些入口自己拥有 lane、哪些由原生 owner 获取，避免重复获取同一锁。事实仍只在 live Host 内合并，完整候选安装后消费；未增加数据库、持久事件或管理 action。
- Plugin 删除复用 `PreparedPluginInstanceObservation` 与原生移除 owner；Skill 缺文档诊断复用允许根和目录身份；字段沿用 `remove_eligible`。MCP 原 YAML parser 的解析异常在查询适配处作为不可观察诊断返回，不改解析器或新增修复路径。Hook 仅澄清上述公开未知值和结算结果。
- 24 份相关测试文件共 **610 项通过**；覆盖 compiler/prefix、GUI scope 与取消、管理 preparation/execution、损坏 Plugin/Skill、MCP、Hook，以及原生 Plugin 生命周期。随后加强 Hook 结算/未知视图断言的 **17 项聚焦测试通过**；另对压缩、子代理、规划及调度续跑执行 **91 项单元测试通过**（34 项 PostgreSQL 标记用例未纳入该单元命令，实际 PostgreSQL 路径由隔离真实模型验收覆盖）。相关测试合计 **701 项通过**。Ruff 与 `git diff --check` 通过。
- 使用保存的 `deepseek-flash` Chat 配置作为只读输入，临时 Pulsara home、独立 demo 与经验证的 disposable 本地 PostgreSQL 数据库进行真实调用；数据库已清理，用户生产配置未写入。两次完整场景验证 GUI 通知、四类能力管理结算及同 epoch 的 SYSTEM/tools 不变、messages 仅追加；另将 Plugin 先经原生 GUI 启用、再外部删除 manifest，验证新 Host 仍可诊断并正式移除该已启用的损坏实例，普通重装后保持禁用。
- 人工复核保留了初轮语言偏差：模型曾做多余查询、猜测 MCP 文件损坏方式，并被 Hook 的默认启用值误导。已收紧轻量基础解释和现有技能指导，并修正未知状态投影；对 UI/MCP/Hook 定向重跑通过。最后一轮 GUI 删除与 Hook 保存的确认均无需工具查询；按现有 MCP/Plugin installer 指导诊断时，模型准确解释坏配置限制，授权后 MCP 普通 ADD、Hook 撤信/停用均通过原路径完成。
- 原始输入、工具调用、回复及结果保留在 `output/diagnostics/capability-change-observation/real-provider-initial.json`、`real-provider.json`、`real-provider-plugin.json` 和 `real-provider-hook-mcp-ui.json`；汇总与实际测试命令见同目录 `verification.json`。这些是隔离验收材料，不是产品日志或新的恢复机制。

可重跑：`.venv/bin/python -m tests.dogfood.run_capability_change_observation_dogfood ui plugin skill mcp hook`；定向复核：`.venv/bin/python -m tests.dogfood.run_capability_change_observation_dogfood ui mcp hook`。诊断指导的定向验收显式调用既有 bundled Skill，未向模型提供预设诊断结论；自然语言 UI 确认不要求读 Skill 或附加查询。

### 10.1 实现复审后的修复与补测

同一个 GPT-6 Astra（high）reviewer 对最终实现复审，指出 GUI identity 值未按实际凭据清理、Hook trust 损坏时的未知启用状态投影遗漏，以及 dispatch 异常边界的测试缺口。修复后的再次复审又复现了“等待原生 lane 期间新增、随后由 REMOVE 清除凭据”的窗口；最终将捕获接入原 `LocalSettingsStore._mutate` 的写锁内，使用已有 current/updated 值关闭该窗口。同一 reviewer 最后确认全部 P2 关闭、无新的阻断问题，独立重跑 47 项聚焦测试通过，并验证了同 store 并发隔离、不同 store 隔离和取消后的 scope 释放。

本轮最终回归为 28 份相关文件 **665 项通过**，压缩、子代理、规划和调度续跑 **91 项通过**，合计 **756 项通过**；后者仍有 34 项 PostgreSQL 标记用例未纳入该单元命令。新 `test_capability_change_dispatch.py` 自行读取保存的配置，复用既有隔离 PostgreSQL owner，实际经过 runner、dispatch、compiler 和 continuity 验证 preflight 失败、安装前取消、安装后网络失败、冻结后新变更和预算真正降级时的 pending/已安装 suffix。未添加新的消费模拟器或持久机制。最终 Ruff、`git diff --check` 通过。

新增 GUI MCP 测试覆盖创建后新增凭据、删除前已存在凭据及排队期间新增凭据，并确认原生删除清理后通知仍不含秘密。Hook 用例先检查损坏状态，再执行显式 mutation，覆盖坏定义、坏 trust、trust 不可读，以及正常未授信/已停用的区分。提示安全观察不可用时，Hook 保存仍可完成，只省略不安全的 advisory 事实。

真实模型新增“定义有效、trust 文件损坏”的只读诊断及正式撤信/停用场景，保留实际输入、工具结果和回复。首轮 Hook GUI 确认仍做了一次多余 list，失败记录保留在 `real-provider-review-before-ack-guidance.json`；随后只收紧 BASE 中一句确认指导，要求按 source/action/result 回答、不额外查询或提议，再按原断言重跑。没有放宽测试断言。

本轮一次新增测试夹具最初遗漏临时 `PULSARA_HOME`，曾向默认 MCP 配置写入名为 `demo-fixture-managed-notice-secret` 的测试连接；已通过原生管理入口只移除该项并确认不存在，随后修正夹具。测试专用凭据保存在临时 settings，未写入用户私有设置。真实 provider 验收继续使用只读保存配置、临时 home 与隔离数据库。不能将整个过程表述为“用户配置从未写入”。最终记录见 `output/diagnostics/capability-change-observation/verification.json` 中 `review_fix`。

最终真实模型复核：UI/Hook 结构断言、原生结算与同 epoch prefix 验证通过；Skill 内说明后仍出现一次将操作后默认值当作先前事实的误判，记录保留于 `real-provider-review-before-adoption-guidance.json`。在既有工具说明明确 post-action 语义后，最后 `real-provider-hook.json` 的回复准确区分损坏时的 `enabled:null`、撤信重建后的中间默认值与最终 `enabled:false`，不再据此声称外部重新启用。未改定义、未授信、未执行脚本。同一 reviewer 对最后文字澄清复审通过，无新增 finding。
