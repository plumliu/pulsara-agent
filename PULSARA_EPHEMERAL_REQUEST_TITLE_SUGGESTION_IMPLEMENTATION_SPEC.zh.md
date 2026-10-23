# Pulsara 临时模型请求、自动标题与会话 Suggestion 实施规格

状态：FROZEN — 主 agent 与 GPT‑6 Astra high critic 共同认可，无未解决阻塞问题。日期：2026-10-23。

冻结审阅：已关闭完成通知/异步请求安装竞争、Host 替换后的迟到响应、用户链来源与计划续接资格、标题首输入与手动改名竞争、建议格式验收、queued/steer 失效、临时 transport 禁止继承正常重试、精确 wire 分支复用接口八类问题。后续必要修订须注明并由双方确认。

2026-10-23 落地补充（主 agent 与 critic 复核）：建议准备与采用时检查当前 continuity cohort 仍持有捕获的同一 wire plan，且没有 compaction fence；成功采用手动 compaction 后清除旧结果。前端也观察既有 compaction-in-progress 状态，避免 canonical 通知先于后台清理时重新显示旧建议。用户级侧栏采用既有可见页面 30 秒元数据刷新节奏，覆盖 queued 首输入在页面切换后才实际接受的情况；该刷新独立于活动会话，不创建模型请求。

本文是本次实现的活动规格，受根目录 AGENTS.md 约束。用户本轮明确授权规格审阅、冻结、并行实现、测试及真实 DeepSeek Chat/Responses dogfood。生产配置只读。历史规格仅用于理解既有边界。

## 1. 产品目标与不变量

提供可随时丢弃的临时 provider 请求，共三种用途：CONNECTION_PROBE、SESSION_TITLE、CONVERSATION_SUGGESTION。复用现有 SDK、wire adapters、配置解析、预算检查和 transport 关闭机制；不创建“临时会话”数据库对象，不走正常 turn 的模型调用记录与 assistant settlement。

- Probe 保持现有未保存连接测试的产品行为及临时凭据边界。
- 自动标题总结首份正式接受的用户输入；成功时填充已有 sessions.title，失败保持“会话 <ID 前缀>”。
- Suggestion 在用户发起的 ROOT 轮次正常结束后生成一句用户可以继续追问的问题，完整响应校验后作为 placeholder。
- 正式回答继续流式显示，绝不等待临时请求。Suggestion 不逐字显示，不禁用编辑器，不显示“正在生成建议”。
- 临时请求不是产品 fork，不安装 epoch，不触发主会话 compaction，不改变主会话运行、完成、usage 或前缀状态。
- 不新增 durable event、live event kind、subject slot、append guard、product relation、durable job、恢复记录或 schema 字段；只允许自动标题采用时更新已有 title。

## 2. 分层与职责

ModelCallPurpose 保留正常 agent、compaction summary、connection probe，新增 SESSION_TITLE 和 CONVERSATION_SUGGESTION；不增加重复的 EPHEMERAL purpose + 第二套 kind 枚举。三种用途共用临时请求执行器及其封闭用途集合。

1. 现有 ModelRuntime / provider-open owner：模型目标、真实凭据、用途许可与 transport 借用。Probe 的未保存候选连接仍由临时 credential owner 提供；标题和建议通过保存配置的普通 owner 解析。
2. 临时请求执行器：消费已准备的 typed call/context，调用现有 transport，聚合公开文本，识别 terminal/tool output，取消并关闭 physical execution，返回仅在内存中的 typed 结果。没有 repository、blob、event、tool dispatcher 或 transcript 写入依赖。
3. 用途调用方：准备输入与预算、决定何时请求、校验及采用结果。标题写入由标题产品 owner 负责；建议由 Host 的进程内状态与前端内存持有。
4. Adapter/SDK 继续负责协议编码、流解析、reasoning 参数和底层连接。不得复制 HTTP/SSE/parser 实现。

从现有 connection_probe.py 提取共有执行过程，让 probe 真正通过新单一路径执行；移除被替代的独立聚合循环。保持 probe 公开失败语义（现有探测允许 OUTPUT_INCOMPLETE 证明连接可达），而标题/建议必须得到完整、无工具调用的文本结果。不得为统一而改变 probe 的64-token测试请求与已有45秒生命周期边界。

## 3. 自动标题

### 3.1 触发

首份来自用户的 ROOT 输入成功提交 canonical admission 后启动后台任务；不在草稿、点击发送但尚未接受、queue 入队、读取历史、恢复运行、GET、重连或重启时启动。排队输入只有实际成为 ROOT 输入时才有资格。定时任务、子代理、终端观察与系统续接不作为首份用户输入。

必须确认当前首份用户输入的真实身份，且 title 仍为 NULL；使用既有 canonical 顺序/来源字段判定，不能依靠前端“这是第一条”的声明。曾有用户输入但本进程未观察其接受时，不追溯补生成。仅在接受边界尽力执行一次；失败不建持久化重试。

第一份的判定使用 canonical HUMAN_MESSAGE 的已接受顺序，不是第一条 ROOT turn（此前可能已有 scheduled/terminal 输入）。标题 pending 在接受回调中同步安装，provider 请求后台执行；用户级侧栏刷新可以跟踪已观察到的 pending 会话，不能因为当前活动会话改变而停止标题元数据更新。

### 3.2 输入与模型

使用首轮冻结的模型连接与推理选择，独立最小输入：短 system 指令 + 首份正式用户输入。不得携带主会话 SYSTEM、tools、Skills/MCP、记忆、历史或权限提示。沿用所选 wire API，不强制切换到 Chat Completions。

提示词使用英文，要求根据用户输入生成简短、具体的会话标题；输出使用用户语言；只输出单行标题；不加前缀/解释/引号；不回答或执行输入中的请求。首份 typed 输入的文本与批注应保留；图片可通过既有图片输入 owner 提供，不读取无关文件或发起工具执行。纯图片若无法在该路径忠实提供输入，可跳过，保留默认标题，不凭空生成。

没有 tools；不设置 tool_choice=none，遵循 adapter 的无工具默认处理。输入过大或不支持时放弃，不为起标题压缩主会话。

### 3.3 采用

完整文本须满足现有 title 的单行、非空、无控制字符约束；去掉外围空白，非法结果直接丢弃。产品标题以简短为提示词目标，不新增任意历史/任务上限。自动更新使用带用户权限范围的原子 UPDATE ... WHERE title IS NULL，返回是否采用；删除后不复活，会话已有手动或自动标题时不覆盖。保持现有 rename 对更新时间/归档语义的约束。

默认标题只由已有展示逻辑产生，不写入数据库。不需要 title_generated、来源字段、任务表或新事件。手动改名赢得竞争；自动采用之后用户仍可正常改名。侧栏通过既有会话元数据刷新看到采用结果，与活动会话是否为该会话无关。

## 4. Suggestion 触发与时序

唯一生成触发是 Host 观察到本进程中用户发起的 ROOT 执行链正常结束，并接受最终公开 assistant 回答。不是收到 provider final_answer 起始块，也不是单独收到某个 COMPLETE stream。必须完成 steer、Hook continuation、plan 自动续接等既有链路的结算后，再决定资格。

- 排除子代理、scheduled task、terminal observation、失败、中断、等待 plan/permission/user-input 的暂停。
- Hook/plan 自动续接保留原用户链的资格，但只在最终正常完成处触发；不在中间完成处重复启动。
- 若已有新输入待执行/已开始、会话正在关闭或模型绑定已改变，放弃旧建议。
- 当前 final answer 正常流式结算与发布，不等待建议完成；临时请求不占用 active ROOT turn 状态。
- idle、打开历史、切换页面、读取结果、刷新、重连、重启均不触发 provider 请求，不补偿错过的建议。
- 切换页面本身不产生请求，也不清除已缓存建议；进程内请求可继续完成。关闭 Host 或删除会话则取消并丢弃。

在 ROOT 激活时建立进程内 awaiting_completion slot，先于任何最终完成通知；slot 冻结链起点的 HUMAN_MESSAGE 资格，PLAN_CONTINUATION 不能单凭返回值变成人类请求。自动续接继承同一个 slot；人工 plan-review 后另起的恢复链不属于本次自动续接，除非它接受了新的 HUMAN_MESSAGE，否则不生成建议。正常 chain 完成后再填 final entry、实际模型和分支源，转 pending；失败、暂停或取消必须转 absent。queued/steer 接受和任何新的 ROOT（包括 scheduled/terminal）均使已有建议失效。

## 5. Suggestion 的临时前缀分支

捕获刚完成回答所用的实际 frozen target、installed epoch view 与 exact wire prefix。使用既有只读上下文投影/adapter materialization/replay 机制构造分支；禁止调用会 install epoch、accept dispatch、启动 compaction 或写 usage 的普通 dispatch 路径。

具体复用 direct_model.quote_provider_followup_wire_resources 的 exact-wire successor seam：从最后一次已接受回答捕获其 request、公开 assistant 与 native replay，对纯 materialization/quote 部分做最小提取，让普通 followup 估算与临时分支共用。不得调用会追加当前 runtime observations 的 compaction-source planner 来冒充原前缀，也不得伪造 compiled semantic fingerprint、message placements 或 durable replay hydration manifest。若现有 LLMContext 只接受 kernel wire plan，可增加完整 typed materialization + quote 的临时输入 carrier，由相同 adapter 消费并检查用途；不增加摘要 hash 或全新协议实现。捕获须在正式 settlement 之后、可变 Host 前进之前完成，不借用已关闭的 tool surface 执行能力。

分支保持 SYSTEM 与 provider tools byte-identical，消息在 installed prefix 后仅追加尚未进入该前缀的正式 final answer（恰好一次，包含对应已接受 replay 所需的 provider-native 内容），再追加 suggestion 指令。不能仅用 public text 替换前缀中已有 opaque replay，也不能遗漏 final answer 或重复追加。

语义前缀和最终 wire 前缀都要验证：root policy 与 tools exact equality，已安装 ordered input items 是临时 ordered input items 的精确前缀。不以指纹替代现成 typed value 的比较；不创建新 registry、epoch nonce 或 proof graph。无法保持精确前缀/重用既有 replay 时直接放弃，不另建 cold epoch。

tool_choice 必须为 auto，保留完整原 tools。提示词只能请求模型不调用工具，不能保证模型不输出 tool call。执行器必须识别任何工具调用，禁止调度，整次结果丢弃（即使同时有文本），不追加 tool-result、不请求 repair、不继续工具循环。

临时 prompt、响应、provider continuation/replay、usage anchor 均不回写主 continuity owner。下一正常调用仍从原前缀加正式输入前进，不能包含临时指令/建议输出。

建议提示词使用精简的英文，输出仍使用用户语言。审视整个有效上下文，优先寻找用户反复强调的约束之间尚未解决的张力，提出这些约束如何兼容、如何决定取舍的未明说问题。推断必须有上下文依据；尊重用户明确作出的决策，不虚构问题，不只是复述已有需求或泛泛询问“具体怎么实现”。对已经解决或缺乏这类线索的对话返回空文本，避免为了深度而挑刺。明确以用户为发问者、助手为被询问者，写成用户可直接发送的简短问题，只输出单行，不加解释或引号；不继续执行原任务，不得调用工具。

2026-10-23 用户追加修订（主 agent 与 GPT‑6 Astra high critic 共同复核并冻结）：两个提示词改为英文，保留用户语言输出；suggestion 明确允许有上下文依据的隐含问题推断，替换此前过宽的“不要假定未表达心理或动机”限制。此次只修改提示词，不改变触发、前缀、工具、存储及缓存边界；前述真实 provider 证据对应修订前措辞，不作为新措辞效果的实测结论。

结果去掉外围空白后，只有非空、单行、无控制字符的完整文本可采用；空文本表示没有建议，多行/控制字符/工具混合输出直接丢弃，不修复，不新增任意字符长度硬上限。

## 6. 预算、失败与资源

使用现有模型 context limits、最终 wire 估算/物理 bytes 边界，计入 final answer、临时指令、tools、replay 与 reasoning/输出空间。建议与标题沿用所选模型的输出预算机制，不把一行可见文本误当作只有几十个 reasoning tokens。不得为了该功能改变主会话 limits。

若最终请求不满足已有输入/输出窗口规则，在 provider open 前返回丢弃；若 provider 仍返回 context exceeded，直接丢弃。禁止为临时请求启动 compaction、重建 prefix、裁剪主会话历史、发摘要调用或进行修复重试。三种临时请求均为单次尽力操作（无 provider 自动重试）；这不是主任务 retry/model-call 总量限制。

ModelRuntime 为临时用途借用 retry-disabled transport，不能直接继承主 runtime 的 retry 配置，也不能修改共享 runtime 导致正常模型请求失去 retry。

复用既有 transport timeout 和 MAX_COMPLETED_PROVIDER_RESPONSE_AGGREGATE_BYTES 内存边界；不可因一个 advisory 请求无限持有网络/内存。采用既有 timeout policy，不另加未经说明的任务生命周期上限。物理关闭在成功、provider failure、工具输出、取消与格式错误时均须完成。取消不能被异常转换吞掉。

Host 同一用途只保留当前的一个 task/result slot；它表示当前建议/首标题的 owner，不是累计次数上限。新 ROOT/模型改变使旧 suggestion 无效，取消旧请求并阻止迟到结果采用。正常任务不等待旧建议的网络请求完成；Host shutdown 必须收拢并关闭临时 transport。

## 7. 不入库与可采用结果

临时执行的请求、响应、reasoning、tool calls、usage、错误详情和 replay 不写数据库、blob 或磁盘缓存，不走 conversation entries、resolved call records、provider usage、memory、hooks 或 event settlement。不把临时 usage 用作主 epoch 的预算 anchor。执行器无写入 owner。

唯一结果采用例外是 SESSION_TITLE：校验后的最终标题字符串由标题 owner 更新已有 sessions.title。SUGGESTION 始终只在内存中；只有用户自己把内容作为普通输入正式发送后，才成为正常持久化用户消息。Probe 配置测试本身不保存候选配置或凭据。

测试/dogfood 可以按用户明确要求输出独立诊断证据（包括实际输入输出），但生产自动请求不因此获得日志落盘权限。dogfood 必须按实际配置秘密值精确脱敏，不过度隐藏提示词、回答、错误体或可检查的 PostgreSQL DSN。

## 8. 结果读取与前端缓存

新增只读会话级 HTTP 查询（建议 GET /api/sessions/{session_id}/ephemeral-results），通过现有用户/会话访问检查，只读取已存在 Host 的进程内状态。没有 Host 时返回不可用/空，不激活会话、不生成请求、不读取/恢复旧结果。结果包含当前 suggestion 的 session/turn/final entry/model binding 以及 pending/ready/absent 状态；title 只需暴露进行中与采用完成的可丢弃状态，便于普通 metadata refresh。

结果同时携带现有 host_session_id，不发明新 generation/token。awaiting_completion 是尚未启动 provider 的状态：前端在完成通知之后读到它时继续读取至 pending/ready/absent，避免“先收到完成通知、后建立请求”导致永远错过结果。Host 关闭/替换或旧 Host HTTP 响应迟到时，以既有 Host/connection 身份拒绝，不只比较 session ID。

不新增 live event kind 或修改 durable snapshot。前端可以在已观察用户轮次结束后查询，并只在 pending 时按现有短刷新节奏继续读取；页面切换/卸载中止该读取循环。读取无论多少次都不能触发 provider。重新打开可以读取尚存的结果，但不重新生成；cold restart 没有结果是正常情况。

前端缓存由 App/会话级内存 owner 持有，不能仅放在随页面卸载的 composer 局部 state；按 session 保存当前的 suggestion 和现有 turn/final entry/model binding。不得使用 localStorage、IndexedDB、磁盘或数据库。

- 输入框已有文字时仍接收并缓存结果，只隐藏 placeholder；删除所有草稿内容后重新显示。
- 切换页面/会话后回来，仍有效的缓存继续使用，不补发请求。
- 正式提交下一轮输入、任何更新的执行使上下文过期、模型切换、删除或 Host 替换时清除/失效旧结果，禁止迟到响应复活。
- 同一运行中 ROOT 接收 steer 后，失效的是先前结果与 HTTP 读取 owner，不永久禁止该链最终回答的新建议。使用既有 isRunning 的 running → settled 转换且无 queued 输入时允许当前 ROOT 的新结果；已完成 ROOT 的旧 ready 不能仅因草稿或 queue 状态变化复活，不新增 generation。
- 草稿编辑、清空、取消尚未提交的输入不清除缓存。
- 建议显示为普通文本，一次性更新空编辑器的 placeholder；文字换行并参与高度布局，沿用编辑器已有 250px 可滚动高度边界，不以省略号截断。输入框聚焦且草稿为空时，普通 Tab 将当前建议填入可编辑草稿，不发送；Shift/其他修饰键、输入法组合、禁用或已有草稿时保留原键盘行为，现有技能/路径补全优先。清空或撤销填入后继续显示有效缓存。
- 刷新/重启允许丢失。无需 loading UI、持久化“已生成”标记或后台补偿。

## 9. 验证与激活

冻结前 critic 与主 agent 必须共同认可规格无阻塞问题；冻结记录写在本文。实现后发现与实际 owner 冲突时，应显式修订本文并再次让 critic 确认，不暗中放宽前缀/存储边界。

必须完成：

1. 真实本地 SDK/HTTP probe 回归，成功、payload bound、provider error、incomplete 与取消均正确释放；候选凭据不保存。
2. 临时执行器三用途测试，purpose 权限、完整/空/工具混合/截断/错误响应、单次调用与物理关闭；标题和建议均无工具调度。
3. 标题 admission 资格、queued/scheduled/恢复排除、并发手动 rename 优先、删除/已命名不写、默认标题与失败路径；PostgreSQL 验证仅 title 更新。
4. Suggestion ROOT completion/自动续接/steer 排除重复、非用户输入/失败/暂停/idle/GET 不触发、新输入和模型变化取消及迟到丢弃。
5. Chat 与 Responses 的 exact SYSTEM/tools/wire prefix 和 final answer 恰好一次；后续正常请求不含临时 suffix；真实 opaque replay 保留。
6. 边界输入追加后超限 provider open=0、compaction=0；provider 实际超限也不重试；无新数据库行/事件/usage/blob，主 continuity 与 usage anchor 不变。
7. 前端生成时已有草稿仍缓存、清空重现、切换回来重现、下一次提交/模型变化/删除/晚到结果失效、不流式或阻塞回答。
8. 运行相关 focused tests、适当 backend/frontend 集成与构建，并检查既有 event/subject/guard/relation/job oracle 不增加。
9. 使用保存的 DeepSeek deepseek-flash high 配置执行两种 wire API 的真实 provider dogfood：probe、首输入标题、正式回答后 suggestion、第二轮 prefix 连续性、不入库检查及失败/容量边界。配置通过 require_pulsara_home 与 LocalSettingsStore 读取；缺少 Responses 时由 Chat 配置派生仅在内存的 Responses 连接并保留原凭据 owner，不写保存设置、不导出 secrets 到环境。若 endpoint 不支持 Responses，保留真实错误，清楚报告未验证项，不能声称通过。

落地验收方法补充：容量边界使用真实已接受的 final/native replay 和原始前缀，在测试中缩窄合法输入预算，验证追加分支在 provider open 前被拒绝、continuity/usage/数据库不变；不发送巨大上下文消耗线上资源。线上失败使用未保存 probe，仅在测试的实际 outgoing payload seam 注入不存在的 model，沿用原 endpoint/凭据 owner，保留真实 4xx 与单次 HTTP 证据。provider context-exceeded 的具体分类由本地 SDK/HTTP 测试覆盖，不称为线上窗口耗尽实测。

本机 PostgreSQL 由桌面 BundledPostgres owner 注入，保存设置未含 postgres 项。dogfood 使用该 owner 的既有 socket/角色路径，查询 data_directory 与本地 socket 身份后创建全新临时数据库，以只读 settings injection 提供给生产 owner，结束后删除该临时库。现有主会话异步 history_search_documents 投影与 writer lease 心跳可并行推进；隔离检查分别验证索引只引用正式 transcript、无临时内容，并严格比较其余语义表行数与 session 字段，不能将正常投影追赶误报为临时请求入库。

## 10. 实施分工与交付

冻结后按文件边界并行：临时执行器/probe/runtime 由一方负责；Host 自动标题、suggestion 输入与生命周期、HTTP 和前端集成由另一方负责；约定 typed interface 后互相审查，避免双写同一文件。测试与 dogfood可按两条路径分工，最终由主 agent 集成并跑完整必要验证。不得只提交规格就结束任务。

交付包括冻结规格、单一路径实现、测试、真实 provider 证据与限制说明。不要求创建 PR、发布或改动生产配置。

## 11. 落地验收记录（2026-10-23）

主 agent 与 GPT‑6 Astra high critic 已复核实现与冻结修订；临时执行器、Host、只读 HTTP、前端内存缓存已接通。未新增数据库表/字段或 durable event、subject、guard、relation、job。闭集架构测试保留精确相等断言，补入本次明确授权的两个进程内任务 owner，并校正已有 history/catalog owner 的清单遗漏。

- 后端综合相关回归 427 passed；新增 provider context-exceeded 覆盖后的临时请求/probe/四组架构闭集回归 71 passed。plan/steer/scheduled/usage 的 PostgreSQL Host 回归 52 项通过（与其他集合存在重叠，不相加作为唯一测试数）。
- 追加 Hook/long-horizon PostgreSQL 回归 58 项通过，上下文限额/三层 handover 回归 10 项通过。usage 与 allowance 的 provider 脚本已按用途分离临时响应，避免临时请求消耗正式调用序号；原有调用数、前缀、预算、工具执行及 usage 断言全部保留。
- 前端全量 81 个文件、1322 项通过；此后补齐卸载后的迟到标题回执和同 ROOT steer 边界，最终功能集成 22 项、完整 App 192 项通过。TypeScript、ESLint、desktop build 通过。全量期间分别遇到原 toast 与 PR05 定时测试的一次超时，隔离及对应完整回归重跑均通过，未放宽原断言。
- 使用已保存的两条 DeepSeek deepseek-flash 连接，两种 API 的 probe、标题、两轮正式回答及 suggestion 均以 high 验证。两个正常调用各自仅留下两条正式 usage；临时 prompt/response/replay 没有入库，第二轮正常前缀没有混入临时 suffix。
- 两种 API 的真实失败各得到一次 HTTP 400，没有重试；真实上下文的测试输入预算分别收窄为 27091/27509 tokens 时，临时分支均在 provider open 前拒绝，主 cohort、usage anchor 和语义数据库不变。未进行线上窗口耗尽请求。
- 完整实际请求/响应、隔离比较及失败体位于 `output/ephemeral-products-20261023/dogfood.json`，通过 `tests/dogfood/run_ephemeral_products_dogfood.py` 重现；早期诊断记录同时保留。所有证据按保存凭据的实际秘密值检查脱敏。保存设置保持只读，临时数据库已删除。
- 精简英文提示词追加效果抽样：`tests/dogfood/run_suggestion_prompt_eval.py` 使用三组相同的人工整理对话、最小中性 SYSTEM 与一个声明工具，分别执行 DeepSeek Chat/Responses high，始终 auto，不创建 Host 或数据库。四条非空问题均以用户向助手发问；自主决策场景两者均抓住未明说的决策授权边界，临时请求场景 Chat 部分综合已有约束、Responses 仍为泛化实现追问。已结束问题对照中 Responses 正常返回空文本，Chat 输出工具调用并由执行器丢弃，未执行工具。证据与人工评估在 `output/ephemeral-products-20261023/suggestion-prompt-eval.json`。这是每场景/API 一次的措辞效果抽样，不是完整生产上下文回归，也不据此推断 API 优劣或稳定命中率。

- 后续提示词 v2/v3 使用同一组案例重跑，证据分别保存在 `output/ephemeral-products-20261023/suggestion-prompt-eval-v2.json` 和 `suggestion-prompt-eval-v3.json`。v2 出现重新质疑既定决策、对已解决算术题强行发问的问题；最终采用 v3，强调尊重明确决策、已解决时返回空文本。v3 两种 API 的算术对照均为空，自主决策案例均提出确认标准问题；临时请求案例仍有复述既有边界的倾向，不宣称稳定发现未明说核心问题。
- 完整桌面服务与打包 React 前端在 Chromium 中计时，使用真实 DeepSeek high、生产 SYSTEM/tools 和两轮正式会话：Chat 为 5.515/5.557 秒，Responses 为 4.767/5.107 秒（final provider TextEnd 至完整 placeholder 的双 requestAnimationFrame 绘制近似时点）。后端 ready 至显示为 0.083–0.466 秒；已命名会话排除自动标题竞争。证据在 `output/playwright/suggestion-latency/summary.json`，服务脚本为 `tests/dogfood/run_suggestion_latency_server.py`；Electron 窗口/偏好接口使用浏览器桩，主请求、结果查询和渲染走真实实现。用户要求暂停后已停止测试并删除临时数据库。
- 用户确认保留回答完整结束后再生成 suggestion；不实施回答流式输出期间的并行预生成。
