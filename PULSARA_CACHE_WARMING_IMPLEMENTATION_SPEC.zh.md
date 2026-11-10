# Pulsara 实验性缓存保温实施文档

状态：已实施。日期：2026-11-10。

## 1. 目标与取舍

在真实对话完成后的短暂空闲期，使用普通临时模型请求，尽力维持 provider 对会话前缀的缓存。默认关闭，由用户显式开启。

**保温必须是脆弱、可丢弃的附加功能。没有保温、错过保温、保温失败或随时取消，都不影响 Pulsara 的正常使用。** 计时仅在当前 Host 的内存生命周期内有效。不保证缓存命中或发送次数，不提供恢复、补偿或交付承诺。

复用现有临时请求路径，不调用 SDK 的 `prewarm`，不适配特定 provider/model 的保温 API，不维护 TTL 表，也不探测或推断服务商的缓存有效期。固定节奏是一项实验性产品策略，不代表所有 OpenAI 兼容服务都有相同缓存语义。

## 2. 已确定的产品行为

| 项目 | 行为 |
| --- | --- |
| 设置作用域 | 用户级全局开关，默认关闭；与输入框建议相互独立 |
| 设置位置 | 设置页“模型”页签最底部，紧邻输入框建议 |
| 参与会话 | 仅本进程内已载入、实际执行过模型请求且具有有效完成上下文的 ROOT session；包括当前会话和后台已载入的 ROOT session，子代理会话不参与 |
| 空会话 | 刚创建、尚未真正执行对话的会话不参与 |
| 开始计时 | 一次真实 ROOT 对话执行正常完成、结算完成并进入空闲时 |
| 持续时间 | 从上述空闲起点起固定 20 分钟，以时间为准 |
| 请求节奏 | 名义时点为第 4、8、12、16、20 分钟，每个时点至多一次，最多五次机会，实际可以更少或为零 |
| 新消息 | 正式接受新输入时立即取消旧窗口，不等下一次模型请求发出 |
| 切换模型 | 接受新的会话模型选择时立即取消该会话的保温，即使没有发送新消息 |
| 再次开始 | 失效后，只有新的真实对话正常完成并重新进入空闲，才可能创建新窗口 |
| 自我续期 | 保温请求不延长、不重置窗口 |

20 分钟和五次机会仅约束可选的保温功能，不限制正常模型调用、轮次、会话或子代理的生命周期。

## 3. 设置页与配置

### 3.1 布局与文案

完全沿用当前“输入框建议”的单行设置卡片：相同的容器、边框、背景、内边距、图标尺寸、标题/说明排版、右侧 switch、禁用和保存失败行为。不引入新的设置布局或实验功能面板。

“模型”页签中的最终顺序为：

1. 模型配置及其添加/编辑区域。
2. ASR 配置。
3. 记忆检索。
4. 输入框建议。
5. 缓存保温（实验性）。

两个开关各自保持现有卡片样式，连续放在页面最底部。“输入框建议”的标题、说明、默认值和功能行为保持现状，只移动位置。

保温标题为“缓存保温（实验性）”，说明为：

> 会话空闲后，已载入的会话会在接下来 20 分钟内，每 4 分钟尝试保活。会产生额外模型请求，效果取决于模型供应商。

只提供开关，不提供间隔、时长、次数、TTL、provider 或模型覆盖项。不显示保温倒计时、进度、成功状态或失败通知，不增加会话级开关。

### 3.2 保存与传播

在现有 `LocalSettings` / `LocalSettingsStore` 中增加布尔设置 `cache_warming_enabled`，默认 `False`，使用现有用户级本地设置读写及 HTTP/runtime adapter 方式暴露。设置页的读取和保存不依赖当前活动会话。

关闭后立即使所有现存 Host 的保温资格失效，取消计时和保温请求；不影响标题、输入框建议、正常对话及其请求。

开启只允许之后新开始的真实执行在完成时建立窗口，不扫描历史、不立即发送、不追溯此前完成或开启时正在执行的旧轮次。关闭后重新开启也不恢复旧窗口。保存失败保留此前已保存值，沿用输入框建议现有提示方式。

该布尔偏好是唯一新增的持久化配置。运行状态不进入数据库、local-settings、浏览器存储或磁盘缓存。

## 4. 资格、时钟与失效

### 4.1 哪些会话有资格

以真实后端 Host 和已接受的最终回答上下文为准，不以 UI 标签或前端消息数量作为执行依据。

保温仅适用于 ROOT session。使用既有 Host 的顶层会话身份（`root_session_id is None`）判定；子代理 Host 不取得保温资格，不创建计时任务，也不准备或发送保温请求。保温不读取或管理子代理的运行状态。

当前 `getSessionPresence()` 优先标记“当前会话”，之后才看 `session.live`，因此“当前会话”标签本身不能证明后端仍载入。不得为了保温而创建、恢复或重新载入 Host。

“压缩上下文”的 `hasCompactionContext` 综合历史分页、当前消息、运行状态及 snapshot/compaction 判断是否已有上下文；保温复用其“不对空白会话做事”的产品含义，但不直接复用此布尔条件。历史、fork snapshot 或压缩结果并不证明本进程刚完成过可保温的真实模型请求。

保温使用本进程刚接受的 `AcceptedProviderFinalSource`。没有这个来源，或者来源不再属于当前有效 ROOT 前缀，就不保温。加载旧会话、打开历史、有草稿、创建 fork、读取上下文用量、点击压缩按钮，都不会单独获得保温资格。

资格不继承 suggestion 的 HUMAN_MESSAGE 专有限制：ROOT session 由定时任务等正常来源驱动时，只要真实 ROOT 执行正常完成、具有有效 final source 并进入空闲，也可参与。不为保温检查或等待子代理结束。

### 4.2 时钟

使用事件循环的单调时钟。`t0` 是 Host 正式执行结算完成并进入空闲的时刻，`deadline = t0 + 20 分钟`。

保温时点固定从 `t0` 推导，不从上一次保温响应完成时间推导，避免网络延迟不断拉长窗口。每个会话同一时间最多有一个保温请求，不并行追赶错过的时点。

- 到点时先检查资格，再准备请求；provider open 前必须再次确认资格。
- 进程挂起、事件循环延迟、请求耗时或资源不可用导致的错过，不补发，不堆积任务。唤醒时只考虑仍有效的当前机会，不遍历发送此前错过的机会。
- 第 20 分钟是最后一个名义机会；只有未超过截止时间且可以立即启动才允许发送。实际唤醒已超过截止时间就放弃，不为凑满五次增加宽限期。
- 截止时间限制新请求的启动；已经合法发出的最后一次请求沿用现有 transport timeout 和关闭机制完成。其他失效事件仍可立即取消它。
- suggestion、标题、连接测试、保温及 UI 活动均不改变 `t0` 或 deadline。尤其不为 suggestion 增加“刷新下一次保温时间”的共享时钟。

例如：12:00 正常回答结算完成，窗口截止 12:20，名义机会为 12:04、12:08、12:12、12:16、12:20。12:01 的 suggestion 不改变这些时间。12:06 接受新消息则旧窗口立即结束；新一轮在 12:09 正常完成，才可能创建截止 12:29 的新窗口。

### 4.3 失效即结束

以下任一情况出现，当前窗口直接结束，清除其来源引用并取消计时/请求，不进入暂停或等待恢复状态：

| 事件或观察 | 处理 |
| --- | --- |
| 接受新输入，包括 queued/steer；新的 ROOT 执行开始；存在待执行输入 | 取消旧窗口，由后续真实执行完成决定新窗口 |
| 会话模型绑定改变，包括连接或推理选择变化 | 立即取消；新选择不自动保温，切回也不复活旧窗口 |
| 压缩开始、压缩 fence 生效或当前 wire plan 不再是捕获的对象 | 取消；不保温旧前缀，不为新压缩前缀做主动预填充 |
| 等待计划、权限或用户输入，失败、停止、中断，或没有有效 final source | 不建立窗口；已有窗口失效 |
| Host 卸载、替换、关闭，会话删除，或应用退出 | 取消，不恢复 |
| 全局开关关闭、偏好不可读 | 取消，保温关闭不使正常会话失败 |
| 目标配置无法忠实复用、缺少 replay、预算不满足、provider/协议/网络失败、输出不完整或包含工具调用 | 放弃本次请求并结束当前窗口；不重试、不修复、不切换目标 |
| 当前资格无法确认或所需资源不能立即取得 | 放弃当前窗口，不建立等待队列 |
| 超过 deadline | 结束，不续期、不补发 |

失效不可通过“当前绑定又与旧来源相等”来撤销。特别是 A → B → A、关闭再开启、压缩失败，都不恢复旧窗口。执行期间切换模型还应使该旧执行的保温完成资格失效，不能等旧执行完成后再次开启。下一次新的真实执行才可重新取得资格。

单纯在页面间或已载入会话间切换，不改变窗口。正常草稿编辑和读取结果不改变窗口。后台已载入会话按同样规则运行，不要求始终停留在对话页。

自动压缩发生在正常执行链中时，不保留旧来源；后续真实执行正常完成，可用最终新前缀创建新窗口。手动压缩不会自行开启新窗口，压缩失败也不恢复旧窗口。

## 5. 请求内容与前缀边界

新增独立用途 `ModelCallPurpose.CONVERSATION_CACHE_WARMING`，加入既有临时请求用途许可、ephemeral wire validation、saved provider-open permit 和 retry-disabled transport 闭集。不要冒充 connection probe 或 suggestion，也不增加另一层用途分类。

复用 `direct_model.py` 中 suggestion 的 exact-prefix 请求准备方式与 `_materialize_provider_followup()`，仅对两种会话临时用途所需部分做最小共享，不新增通用调度或插件框架。

输入必须满足：

1. 使用刚完成执行的冻结目标及其普通保存凭据 owner；配置不兼容就放弃，不转用另一个连接。
2. `SYSTEM` 和 provider `tools` 与来源 byte-identical。
3. 保留完整已安装 wire prefix、已有 opaque replay；在尾部追加尚未包含的正式 final answer 及其 native replay，恰好一次，再追加固定保温 user 指令。
4. 保持现有 suggestion 的 `tool_choice="auto"` 方式及完整工具声明，依靠执行器绝不调度工具；不为保温替换 SYSTEM、清空 tools 或重建输入根。
5. 每次都从同一个正式 final source 建立独立临时分支，不把上一轮保温的请求、回答或 replay 接到下一轮保温中。

固定英文指令：

> This is a cache keep-alive request. Do not perform any work or call tools. Reply with exactly: OK.

不加入动态时间、随机文本或额外上下文发现。不调用正常 turn 的 planner/dispatch/settlement，不刷新 Skills、MCP、memory 或其他动态根。不能保持前缀就结束，不建立 cold epoch，不触发压缩，不裁剪历史。

沿用现有最终 wire 预算检查、模型输出预算、transport timeout 和响应聚合 bytes 边界。不因只期望 `OK` 就设置未经验证的极小 reasoning/output 上限。

复用 `execute_ephemeral_request()` 的单次执行、工具输出识别和物理关闭。完整响应直接丢弃，不要求新增 OK 文本解析器；工具调用永远不执行，不发送 tool result，不 repair。保温不接管或修改正常请求的重试策略。

不读取缓存命中统计来决定续期，不为本功能扩展 usage 结果类型、建立命中率指标或 provider 分支。无法判断效果是实验功能允许的结果。

临时 prompt、响应、reasoning、replay、usage 和错误详情不写入正式 transcript、provider usage、blob、memory 或事件。保温的成本可能由 provider 实际收取，但不伪造为正式对话 usage。正常后续请求仍从正式前缀推进，完全不包含临时保温内容。

## 6. 最小实现与所有权

### 6.1 复用边界

| 既有 owner / 机制 | 继续负责 |
| --- | --- |
| `LocalSettingsStore`、用户级 settings HTTP/runtime adapter | 保存开关、读取偏好、向现存 Host 传播关闭 |
| `SessionEphemeralProducts` / Host 生命周期 | 当前会话的保温资格、单个后台任务及随时失效 |
| `AcceptedProviderFinalSource`、现有 exact-prefix materializer | 冻结来源、忠实构造临时分支和预算检查 |
| `ModelRuntime` / provider-open owner | 保存凭据、目标解析、用途许可、transport 借用 |
| `execute_ephemeral_request()` | 单次调用、丢弃工具输出、取消、物理关闭与释放借用 |
| 现有 OpenAI Chat / Responses adapters 和 SDK | 协议编码、流解析、reasoning 参数、底层网络 |
| Python `asyncio` | 本地等待、任务取消和收拢 |

Pulsara 新增的只有产品偏好、保温用途，以及 Host 内的短期计时策略。现有单调时钟和 `asyncio` 已足够，不使用定时任务产品、数据库 scheduler 或额外调度依赖。

### 6.2 内存与并发

每个 ROOT session Host 至多持有一个当前保温来源和一个计时/请求任务，时间值由该任务持有即可。子代理 Host 不创建保温状态或任务。失效直接撤销当前引用并 cancel；使用已有运行/slot 对象身份拒绝迟到完成，不新增 generation、fingerprint、注册表、检查点或恢复状态机。

保温与 suggestion 使用独立资格和开关。`completed()` 不能因为 suggestion 关闭、没有 HUMAN_MESSAGE origin 或 suggestion admission 读取失败，就连带阻止合法保温；也不能为了保温改变既有标题/suggestion 行为。

新输入和模型选择先撤销保温资格，不等待保温网络请求结束才能继续正常工作。取消后的物理关闭继续由既有临时执行器完成，并由 Host 的既有 task 集合在关闭时收拢；不能取消后直接丢下 transport。旧保温尚未物理关闭时，新窗口宁可放弃，不建立保温排队或重叠请求。

每个可能让旧来源失效的异步检查之后、真正 provider open 之前，都需确认当前 owner 仍有效。无效的迟到请求不能发出；已发出的响应也不能重启时钟或恢复资格。正常执行和 Host shutdown 的错误语义不由保温改写。

不保证跨会话公平性或所有会话均收到五次请求，不新增全局保温并发控制框架；沿用现有 transport 资源规则。不要为可有可无的请求增加可持续等待的逻辑工作。

### 6.3 代码落点

- `src/pulsara_agent/settings.py`：布尔设置、校验、编解码、修改与既有 settings 重建路径的字段保留。
- `src/pulsara_agent/desktop_app/http_server.py`、`session_controller.py`：用户级设置读取/保存；关闭时只遍历现有 Host 撤销保温，不激活会话。
- `frontend/lib/runtime-adapter.ts`：设置 DTO 和保存接口。
- `frontend/components/settings-view.tsx`：两个现有样式的卡片置底，新增独立保存状态。
- `src/pulsara_agent/primitives/model_call.py`、`llm/ephemeral.py`、`llm/validation.py`、`llm/provider_open.py`、`llm/adapters/openai/retrying.py`：新增用途并接通现有单次临时执行路径。
- `src/pulsara_agent/conversation_kernel/direct_model.py`：共享最小的精确前缀请求准备过程。
- `src/pulsara_agent/conversation_kernel/ephemeral.py`、`host_lifecycle.py`、`host.py`、`host_prompts.py`：完成时资格、输入/模型/压缩失效和关闭；更新仅描述当前有效职责的 docstring。

运行状态不增加数据库表/列、durable event、live event kind、subject slot、append guard、product relation、durable job，也不增加保温身份对象。现有请求/context 如需分配 ID，继续通过共享 identity 层产生。

## 7. 实施顺序

1. 接通默认关闭的用户级布尔设置和两个卡片的底部布局，保留 suggestion 原行为。
2. 增加独立保温用途，复用单次临时执行器及原前缀准备，覆盖 Chat 与 Responses。
3. 在现有 Host 临时产品 owner 中接入完成、计时、失效和关闭；不复用 suggestion 的人类来源资格。
4. 验证生命周期、精确前缀、无持久化副作用和 UI 布局；同步修正受影响的当前契约与架构闭集断言，不能放宽断言来掩盖新增机制。

不修改或迁移旧会话，不添加兼容路径。不顺带改 MCP 连接池、正常调度、suggestion 提示词或正式请求预算。

## 8. 验收

以下是实现的验收契约；实际验证范围见 8.3。

### 8.1 聚焦测试

用可控单调时钟验证调度，不在单元测试中真实等待 20 分钟：

- 默认关闭；两个开关独立；开关保存/读取及保存失败保持旧值。
- 两张卡片都在 ASR、记忆检索之后，suggestion 在前、保温在后；沿用现有样式与 switch 可访问名称。
- 空白/只加载历史/只有 snapshot/创建 fork 不触发；多个已载入 ROOT 会话独立，切换页面不重置；子代理即使正常完成且有 final source，也不取得资格、不创建计时任务、不准备或发送保温请求；合法 scheduled ROOT 完成可以参与。
- 正常完成才建立窗口；第 4、8、12、16、20 分钟的名义机会、截止相等/超过、延迟/挂起错过、不补发、不重叠及有限窗口；保温与 suggestion 都不续期。
- 新输入 queued/steer、模型切换且无新消息、A → B → A、执行中切换、关闭再开启、压缩开始/失败/成功、Host 关闭/替换，均不能让旧窗口或迟到完成复活。
- 首次 provider 错误、超限、工具输出或不完整结果结束当前窗口；不重试、不调工具、不启动压缩，正常对话仍可继续。
- 取消在准备、provider open、读取流和关闭期间均保持资格失效、物理关闭及借用释放；普通对话不等待保温网络完成。
- Chat/Responses 精确 SYSTEM/tools/wire prefix、opaque replay、正式 final answer 恰好一次；重复保温不累计临时 suffix，下一次正常请求不受污染。
- 无新增正式调用/usage/transcript/blob/事件写入，主 continuity、预算 anchor 和结构性 oracle 不变。正式标题采用等既有独立行为不能被误判为保温写入。

优先扩展现有 `tests/test_session_ephemeral.py`、`test_ephemeral_model_requests.py`、`test_desktop_http_surface.py`、`test_provider_usage_postgres.py` 和 `tests/frontend/settings-view.test.tsx`，按实际改动运行聚焦检查及相关前后端构建。Python 使用仓库 uv 管理的 `.venv`。

### 8.2 真实集成与视觉验收

实现后通过 `require_pulsara_home()` / `LocalSettingsStore` 读取已保存生产连接，使用只读配置注入和隔离临时数据库/设置覆盖，不改写用户保存配置、不导出凭据到 shell。

使用现有保存配置验证实际可用的 Chat 和 Responses 路径：正式回答后保温、保温后正常发言、模型切换取消、失败放弃、精确前缀与不入库。至少用一个真实配置验证正常的 4 分钟空闲等待后可发出保温；其余时间边界由可控时钟验证，无须为凑足五次做 20 分钟重试实验。协议不可用或配置缺失时如实记录未验证项，不能声称支持或缓存效果已验证。

Dogfood 证据保留实际请求/响应及必要错误，只按真实凭据值脱敏。缓存命中信息若 provider 本来返回，可以观察，但不作为功能成功的通用判据。

设置页按用户提供截图中的输入框建议样式验收，完成必要构建后供用户视觉检查。不为卡片移动另开 Playwright 验证流程。

最终通过标准是：开启时有机会保温；任何失效都可安全丢弃；关闭或完全没有保温时，正常 Pulsara 行为不受影响。

### 8.3 验证结果

- 后端保温、临时请求、设置、HTTP 与正常请求重试相关检查：252 passed；随后补充的完成时资格及迟到取消检查通过。
- 使用已验证的桌面本地 PostgreSQL，以独立临时数据库运行会话临时产品、usage 与上下文预算集成：30 passed。
- 相关架构与闭集检查：33 passed；未增加数据库结构或事件/subject/guard/relation/job 类别。
- 前端设置页、suggestion 与用户级状态相关测试：62 passed；TypeScript 检查、前端构建及修改文件 Ruff 检查通过。
- 真实保存的 DeepSeek Chat / Responses 连接均在未缩短生产间隔的情况下自动保活成功，从正式结果及标题结算后观察到完整保活响应分别约 241.4 秒。精确前缀、重复临时分支、保活后正常发言、模型切换取消及每会话仅两条正式 usage 验证通过。

真实验收的无副作用检查按保活请求发出前后取完整快照，保留字段逐项比较。等待期间现有写租约心跳会正常更新 `writer_lease_expires_at` 和 `updated_at`；等待开始时的快照另行保留，不能把整段空闲期的心跳写入归因于保活，也不能笼统忽略 `updated_at` 来通过检查。

真实验收通过 `tests/dogfood/run_cache_warming_dogfood.py` 重现，证据位于 `output/cache-warming-20261110/dogfood.json`。首次采用整段等待期比较而失败的证据保留在同目录 `dogfood-first-attempt.json`。保存配置保持只读，临时数据库已删除。provider 错误、工具输出、不完整响应及取消的单次请求语义由聚焦测试和本地真实 SDK/HTTP 集成覆盖；不据此宣称所有服务商的缓存效果已验证。
