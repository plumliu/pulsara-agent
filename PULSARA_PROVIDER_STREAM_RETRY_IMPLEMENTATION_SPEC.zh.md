# Pulsara：模型流式中断安全重试实施规格

日期：2026-10-30。状态：**实施与激活完成，2026-10-31 经 ROOT 最终语义验收。** 证据见[激活报告](output/unified-input-retry-20261031/activation-report.zh.md)、[逐项语义矩阵](output/unified-input-retry-20261031/verification-plan.zh.md)与[原始证据索引](output/unified-input-retry-20261031/activation-index.json)。保存生产配置只读。

## 0. 目标、依据与范围

Pulsara 的 ROOT 与 Subagent 在网络短暂故障后，应能自动重新请求当前尚未接纳的模型响应。用户看见准确的重试状态，可以立即停止；已经执行的工具和已经接纳的消息不重复。首次请求之外最多重试五次，通过较长退避容忍数秒至几十秒的网络波动。

根 AGENTS.md、当前人类决议优先。本文明确修改普通 Agent 模型循环的重试合同；其他现行的前缀连续性、安全点输入、显式停止、Work、Cell、provider replay 与资源边界合同继续生效。

### 0.1 现场证据与结论边界

2026-10-30 的“能否看到MCP及Firecrawl”会话：风险研究员在 22:14:24、ROOT 在 22:15:10 分别以 `unknown_provider_error: Connection error.` 中断。定价与能力研究员完成；Cell 在 22:15:03 完成并交付结果后正常回收。Clash 服务日志确认当时 DeepSeek 连接 `using GLOBAL`；22:12–22:16 无对应 warning/error，22:17:59 同一日本节点另有访问 ChatGPT 的连接超时。日志摘录见 [现场证据](output/clash-investigation-20261030/incident-excerpts.log)。

这些证据支持“代理链路是可疑因素”，不能证明这两次失败的具体根因、是否已有流式输出、或重试耗尽。本文的验收依靠受控故障注入，不把这次事故推断当作已验证根因。

已确认的代码缺口：adapter 将连接失败输出为 `provider_transport_error`，现有 sanitizer 没有识别此 hint，因而显示 `unknown_provider_error`；最终失败路径又仅保留 `code: message`，未记录结构化重试决定及底层异常链。错误分类与诊断修复属于本次同一实施范围，分别由 §3 与 §6 规定，不能只调整次数和退避而漏掉它们。

### 0.2 外部实践及采用边界

- [Codex 官方配置说明](https://learn.chatgpt.com/docs/config-file/config-reference)：区分 HTTP 请求重试与 SSE 流式中断重试，后者默认五次。本文借鉴流式中断应可恢复及其次数量级；不照搬其协议实现，也不把 HTTP 与流重试相乘。
- [AWS：Timeouts, retries, and backoff with jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)：采用有限自动重试、指数退避、随机抖动及单一重试 owner；重放有副作用操作需要独立安全依据。本文的具体参数是 Pulsara 的产品决议，不宣称来自 AWS 的推荐默认值。
- 已检查现有 SDK 与 Pulsara adapter：SDK 负责 HTTP/SSE 解析和传输；Pulsara 已有 adapter 重试循环、错误分类、退避 helper、generation replacement 与最终接纳边界。沿这些 owner 修改，不复制 SDK parser、DNS、代理或连接管理。

### 0.3 本轮非目标

不调整 Clash、系统代理、模型 endpoint、模型选择、网络出口或凭据；不自动切换直连、节点、供应商、Chat/Responses 协议。材料 Markdown、技能标签误识别、过程分组及完成后 Cell/工作流历史阅读另行处理。无跨进程恢复、后台重试 job、持久等待网络模式或任务级无限重试。

## 1. 不变量与唯一 ownership

| 边界 | 唯一 owner 与合同 |
|---|---|
| HTTP/SSE/TCP/TLS 与物理关闭 | 现有 OpenAI SDK / HTTP 客户端；Pulsara 使用支持的 close 接口并确认本地流退出 |
| 分类、次数、退避、下一次请求 | 现有 Chat/Responses adapter 重试循环；共享纯 helper，禁止第二套循环 |
| 已安装 provider 输入 | 现有 prepared execution / prefix owner；所有重试复用同一冻结输入 |
| 草稿替换、完整响应接纳、工具派发 | Runner 与现有 assembler/settlement/dispatch owner |
| 用户停止及安全点输入 | 现有 cancellation intent 与 safe-point owner，优先于下一次重试 |
| UI 重试观察 | 正在运行的 prepared execution 持有一个进程本地 typed slot，Host/desktop 只投影当前值 |
| 诊断 | 现有应用 logger；终态沿现有 turn public detail |

1. 同一逻辑请求每次重发的 SYSTEM、provider tools、messages、参数、模型 target、输出预算、工具绑定、请求时钟与输入快照相同。不重编译、不重新采样环境，不重新安装 append candidate，不消耗第二份 one-shot provider-input authority。
2. 同一逻辑请求首次通过权限 gate 后，其全部重试沿用该次放行与冻结的 connection/credential。按用户决议，不在重试间重新检查权限、重读保存设置或比较凭据，不增加新的授权回调、撤销跟踪或 settings 锁接线。用户停止、任务取消和 Host close 继续按原执行控制生效，见 §4.5。
3. 无新增 durable event、表、列、subject、append guard、产品关系、job 或 live event kind；无新哈希/指纹注册表。复用现有身份、typed values 与 observation/settlement。
4. 五次只约束**单个逻辑模型请求**的自动恢复成本，不是 turn、任务、session、Subagent 或应用寿命上限。后续正常逻辑请求各有独立额度。
5. 不更改现有 per-operation/物理并发/单帧/资源保护及流读取超时，不新增任务总时长限制。

## 2. 五次重试的计数与策略

### 2.1 统一额度

普通 `AGENT_MODEL_LOOP`：`max_attempts = 6`，即 initial attempt 1 加 retry 1–5。代码现有 `attempts` 字段继续表示总尝试次数，不改成语义不明的“重试次数”。用户文字统一显示重试 `k/5`；日志同时包含 attempt `n/6`。

请求建立失败、流中断、以及已有的工具参数格式异常重新生成，共享这一逻辑请求的五次恢复额度。收到任何 delta、重新生成、成功建立 HTTP 或切换故障类型均不重置计数。SDK 的隐式 HTTP retry 为零；外层 Runner/Host 不再重试同一次逻辑请求。请求未发出但已进入本次传输尝试的失败也计为一次 attempt，字段不冒充实际网络发送次数。

连接探测、标题、建议、压缩等其他 purpose 沿原政策，不因本轮默认值改变而获得六次请求。实现必须核对所有 `LLMRetryConfig()` 使用点，只对本轮 scope 明确采用新默认。

### 2.2 退避公式

失败后第 k 次重试（k=1..5）基础等待：

`base(k) = min(2 * 2**(k-1), 30)` 秒，即 **2、4、8、16、30** 秒。

无有效 Retry-After 时：`delay = base(k) * U(0.8, 1.2)`。复用现有可注入随机源与 retry sleep。总退避基础值 60 秒，抖动后 48–72 秒；每次模型请求和关闭耗时另算，不宣称任务最多一分钟。五次覆盖暂时网络波动并限制重复推理成本；30 秒是基础退避上限及 Retry-After 接纳阈值，含抖动实际单次可达 36 秒，不是任务寿命限制。

有效非负且不超过现有 30 秒边界的 Retry-After：`floor = max(base(k), Retry-After)`，`delay = floor * U(1.0, 1.2)`，保证不早于服务端要求。HTTP 日期按收到响应时解析成等待时长，实际等待用单调时钟。负值、非法值、NaN/Infinity 忽略；零有效。超过 30 秒沿既有 `retry_after_exceeded` 明确结束本次自动恢复，不截短到 30 秒后擅自请求。UI 告知服务端建议等待更久；不新建持久定时任务。保留原可解析值于 scrub 后日志；summary 沿现有 600,000 毫秒字段边界，无法精确表达的值置 null 并保留 `retry_after_exceeded`，不截成 600 秒冒充原值。现有 `_seconds_to_millis` 的静默 min 截断须改为这项明确规则。

所有等待均异步、可取消，不能占住事件循环或用阻塞 sleep。并行 ROOT/Subagent 独立取样，复用现有物理并发接纳，不新增全局熔断器、持久协调器或另一套调度。

## 3. 可重试分类

分类基于 SDK 异常类型、结构化 HTTP 状态/错误码及异常链；保留已有必要的兼容解释，但不因任意文本出现 `ssl`/`timeout` 就掩盖确定性配置错误。

| 情况 | 决策 |
|---|---|
| 临时连接建立失败、连接 reset/aborted、传输读写失败、意外网络 EOF、可恢复读空闲超时 | 可重试，仍需通过 §4 安全门 |
| HTTP 408/409/429/500/502/503/504/529 | 沿现有允许集合可重试；明确 quota/billing/确定性错误优先拒绝；Retry-After 另按 §2 |
| API error/SSE failure 有可核实的临时错误类别 | 按同一分类与安全门；SSE 错误不是成功 terminal |
| 401/403、invalid request/schema、模型不存在、上下文超限、quota/billing、不可用配置/凭据 | 立即失败，不消耗等待去重复无效请求 |
| 明确证书校验/主机名不匹配、不支持的 URL/代理配置等确定性本地错误 | 立即失败，保留具体错误；不关闭证书校验或降级直连 |
| 用户停止、Host close、任务取消、执行 owner 已结束 | 走原停止/收尾，不重试，不显示网络故障代替取消 |
| 协议不一致、内容/单值资源边界、输入连续性错误、未知编程异常 | 沿现有 typed failure，不泛化为网络抖动 |
| 工具参数 JSON 格式异常 | 保留已有明确允许的 regeneration 条件，共享额度；不扩大到其他协议损坏 |

把 `provider_transport_error` 显式映射为新增稳定公开码 `provider_connection_error`。该码表达连接类失败，不声称已定位 Clash、DNS 或上游。`provider_timeout` 继续映射为 timeout，协议错误保持独立。更新既有公开枚举、sanitization contract 的映射定义、消费者和测试；不新建错误注册表或兼容别名。真实未知错误仍为 unknown。

## 4. 流式中断重试的安全门

### 4.1 允许替换的对象

重试恢复的是**当前未接纳的一次模型生成**，不是整个 turn 或 Work。可以已经展示思考、正文或部分工具参数；这些 delta 只是可撤销 live 草稿。之前其他逻辑请求已接纳的 assistant/tool 记录、工具结果和 Work 不受影响。

必须同时满足：

1. purpose 是本轮普通 Agent 循环，当前 response 尚未交给 canonical assistant commit，也尚未派发这份 response 中的任何工具。
2. 尚未观察到不可替换的成功/不完整业务终态；没有待确认的 assistant commit 或工具派发结果。
3. 以冻结后的最终 SDK 有效请求体（包括 `request_defaults`、`request_extra_body` 的覆盖效果）及现有 route/wire contract 证明：模型只生成内容和 Pulsara 完整响应后才派发的 function tool 描述，没有托管工具、后台执行或其他供应商执行外部动作的能力。不仅检查 `context.tools`；未知扩展无法证明安全时返回 `unsafe_to_replay`。当前闭合生产 profile 可直接由已有完整 typed 值判断，不增加能力 registry 或 fingerprint。该安全门也适用于 opening/零 delta 的失败，因为未收到输出不证明远端未执行。
4. 失败可重试且额度未耗尽；原流已本地关闭并完成物理收尾，原 SDK iterator 不再产生有效输出。
5. 使用原 prepared execution、installed input、target 与首次权限放行；执行尚未结束且没有优先停止原因，不重新跑权限 gate。

安全门不能只检查 `has_semantic_output`；它表示有输出，不表示已有不可逆副作用。完整工具参数已收到但未 commit/dispatch，也仍须满足其他条件；部分 JSON 绝不派发。

### 4.2 terminal 与 EOF

- SDK 明确抛出的连接中断/网络 EOF 可进入重试分类。SDK iterator 正常结束但缺少规范要求的 terminal，仍归 `transport_protocol_error`，不凭缺终态这一事实推断网络故障。字段冲突、缺 replay、结构损坏、源项限制同样保留原协议错误。两种 API 都必须把 typed 网络异常与干净 EOF 的测试分开。
- 已有完整、验证通过的成功结果，随后关闭失败：保持当前“物理失败优先”的合同，终止本次逻辑请求，不 canonical 接纳、不派发工具，也不重新生成。UI/日志明确“已收到结果，但连接收尾失败”，保留原错误和 close 错误；不得把它记成普通重试耗尽或静默成功。
- `max_output_tokens`、content filter、明确业务 incomplete 等保持原 typed 结果，不当作网络断流无限重新生成。
- 已观察失败 terminal 时，adapter 必须先决定是否内部恢复，不能先向 normalized consumer 发出不可撤销终态，再请求替换。
- 本地关闭成功不证明服务端未计费或未继续计算。允许重复推理/计费风险；不承诺 exactly-once 模型执行。

### 4.3 重试顺序

`接收失败 → 关闭/确认本地旧流 → 核对重放安全门与停止 → 记录失败决定 → ABORT 旧草稿并清空累积器 → 发布等待状态 → 可取消退避 → 检查停止 → 沿用首次权限放行发起下一次完全相同的请求`。

无输出时也发布等待状态；已有输出时复用 `ProviderGenerationRetry` 及既有 live settlement，泛化其 reason 为 typed 原因（网络恢复/参数重新生成），不能依旧显示“工具参数格式异常”。更换 disposable draft identity；旧草稿与新草稿不得拼接、去重续写或同时变成最终正文。

清空 Chat/Responses accumulator、open blocks、seen item IDs、usage、model identity、replay fragments 与 Runner assembler；失败 attempt 的 tool call ID 和 replay 不能进入成功 attempt。重复文本完全相同也不能据此认定是同一次响应。不得添加 provider previous_response_id、续传 offset 或新兼容协议来续接旧流。

Chat 与 Responses 必须使用同样的 SDK close/join 保证。当前 Responses 异常路径没有与 Chat 对等的每 attempt 显式 close 收口，实施时必须补齐，不能只放宽重试布尔条件。关闭无法确认或资源收尾失败，禁止新请求并沿原收尾失败路径结算。

### 4.4 停止与输入竞争

退避仍属于原 Work 的执行期。Steer/排队用户输入继续由既有 safe-point 处理，不在 adapter 重试间隙修改冻结 messages；不得为吸收新输入重编译重试请求。显式停止在关闭、等待、admission 与输出竞争的任一阶段优先生效。等待被取消立即结束，下一次请求不得开始；新请求已经开始时使用原取消机制关闭。测试使用同步屏障验证先后，不以“取消大概及时”代替断言。

ROOT 与 Subagent 使用同一恢复合同。等待期间 Work 保持原 running 语义，不提前发布 stopped/final 或触发失败依赖；其他独立 Work 可以正常完成。只有最终耗尽/拒绝恢复才走现有失败结算。父级失败、独立 Work、Cell 回收仍由各自原合同负责。

### 4.5 沿用首次权限放行与原取消机制

同一 `AGENT_MODEL_LOOP` 逻辑请求在首次通过权限 gate、完成 provider-input 安装并借得 transport 后，最多五次重试全部沿用这一已接纳执行。重试不重新借 target、不重新消耗 one-shot permit，也不重新检查已借工具绑定的权限状态。后续真正的新逻辑请求仍按其既有准入合同执行。

冻结 connection/credential 在这次执行及其重试中继续使用；退避期间保存设置发生编辑，不触发本次请求重新授权、更新 endpoint/key 或主动终止。若远端返回 401/403 等确定性失败，按 §3 停止。保留现有 `admit_provider_request` 的 payload credential containment 与原 transport 生命周期；本轮不向 `LocalSettingsStore` 增加逐次授权入口，不增加 per-attempt authorization callback、mutation 锁、凭据比较或轮询。

取消仍通过原 turn cancellation intent、异步任务取消及 SDK close 路径生效。等待中停止则取消 sleep，不进入下一次请求；请求已经开始则关闭原流，之后不得再发。已有生命周期机制若已结束该执行，重试不能将其复活。这里检查的是执行是否被停止，不是第二道权限 gate；不另建 lease、generation、registry 或取消协议。

## 5. UI 与进程本地观察

### 5.1 显示位置和文字

ROOT 及 Subagent 会话的现有运行状态区显示当前重试提示，不作为 assistant 消息、不进入 provider 输入或任务材料。Subagent 详情页独立显示本代理状态；不把子代理等待错误画成 ROOT 请求错误。

| 阶段 | 示例 |
|---|---|
| 等待重试 | 连接中断，8 秒后重试（第 3/5 次） |
| 正在尝试 | 正在重新连接（第 3/5 次） |
| 已开始输出后重试 | 本次回复中断，正在重新生成（第 3/5 次） |
| 非连接暂时故障 | 模型服务暂时不可用，8 秒后重试（第 3/5 次） |
| 恢复成功 | 清除运行区重试提示；可沿原提示通道短暂说明“连接已恢复” |
| 最终耗尽 | 模型连接失败，已重试 5 次。可检查网络后继续。 |
| 无法安全重放 | 本次请求中断，无法安全自动重试；保留明确原因与错误详情 |
| Retry-After 超界 | 模型服务要求等待更久，本次自动重试已停止。 |

倒计时为实际剩余等待秒数向上取整。它不冒充请求剩余时间；重连阶段不显示已过期倒计时。ROOT 沿用现有会话停止按钮，只停止其原合同规定的目标。Subagent 详情目前是 observer 只读视图，没有单代理停止按钮，本轮不虚构此按钮或扩展其控制权限；Subagent 经既有授权 Work/agent stop 路径接收取消时，等待必须立即可取消。ROOT 停止不被解释为强制取消所有独立 Work。不能以停止 Cell 代替停止已接纳 Work。不新增立即重试/跳过等待按钮，避免绕过服务端等待要求。短暂恢复提示可复用现有 presentation notices，不依赖其一次性队列维持当前等待状态。

### 5.2 最小数据合同

在现有 prepared execution 上增加一个 typed、只读观察 slot。当前 Host 没有现成的 active-execution 观察入口，因此必须由 Runner `_collect_model` 在 open 前登记这一精确 execution 对象，在 finally compare-and-clear；每个正在执行的 session 只持有其当前执行对象引用，不建以 call ID 为键的第二套历史 map。adapter 仅通过该 execution 的回调更新 slot，Host/desktop 从 Runner 的当前引用投影 session observation 的 `provider_retry` 字段。需要字段：原 session/turn/resolved_model_call 身份，当前 disposable draft 身份、attempt、retry_number、max_retries、phase（waiting/reconnecting）、reason、实际剩余等待时长。剩余时长由 backend 单调截止点计算，前端以收到值后的本地单调时间递减；跨刷新重新取事实，不传不可比较的后台 monotonic 原值。

每次 observe 响应必须包含当前值或显式 `provider_retry:null`，不能把省略字段解释为保留旧提示。复用现有观察唤醒条件：slot 值变化使本次 observe 可以返回；仅剩余秒数变化不每秒发后台事件，由前端递减并在下一次普通读取校正。不得新增 live event kind；observe 返回循环每次核对 slot 的阶段/身份变化。controller 与 observer 都可读取当前值，Subagent 详情使用 observer 连接也必须显示；不复用只能向 controller 消费的一次性 notice 来运送这个 slot。

等待/重连更新同一 slot；进入成功响应输出、最终成功、终态失败、取消、Host close 时按精确执行身份清除。旧 callback 不能清除新逻辑请求的提示。UI 刷新/切页重新读取同一当前事实；附件销毁不取消执行。进程重启丢失观察可接受，不恢复执行。无需新 live event kind，使用已有 observation 刷新链路；若现有入口不足，仅扩展同一 session observation DTO 与读取接线。

失败最终沿当前 turn 的 `terminal_public_detail` 存准确错误及简短重试结果，不增加列或事件类型。用户“继续”沿原新输入/新请求流程，不承诺重新构造旧进程状态或专用恢复按钮。

## 6. 日志、错误与 usage

### 6.1 必需诊断

复用现有 retry logger，并补齐最终决定及恢复成功日志。每次失败都可定位到现有 session、turn、resolved_model_call_id、model_call_index、wire_api、target model、endpoint host、attempt/max_attempts、retry_count/max_retries。不得为日志新生成 durable attempt 身份。

需记录：发生阶段（opening/streaming/closing）、异常类型及 cause/context 链、SDK/HTTP status、provider code/request ID（存在时）、是否已有语义输出、是否已观察 terminal、原流关闭状态、classification、decision、skipped_reason、Retry-After、base/实际 delay、失败/等待时间戳；最终成功记录实际尝试数，最终失败记录是否 exhausted。字段含义明确：只有真正发起重试后 retry_count 才增加，等待中取消不计为已发出的重试。旧 trace 可记录计划等待，但不得把 trace 长度误认为实际已发生重试次数。

cause/context 遍历用对象身份去环，不递归无限循环；本轮不设任意异常链深度 cap。对大错误正文沿既有诊断尺寸边界输出并标示截断。首次准备请求时，经原设置借用流程及本次 credential owner 收集实际秘密值，构造并沿用本次执行的 scrub set；不在每次失败或重试时重新读取 settings。scrub 覆盖本次借用的凭据值、代理认证、OAuth、请求 URL 中真实 token、URL percent-encoded 及 JSON 转义形式。endpoint host 必须用解析后的 hostname，不用可能含 userinfo 的 netloc；现有 `_safe_host` 须一并修正。序列化后的最终日志文本和 `terminal_public_detail` 再做精确值 scrub，不能只 scrub 中间异常。不能输出原始 settings、Authorization 或连接密钥；不泛化删掉提示词、正常响应、错误正文和非秘密上下文。PostgreSQL DSN 沿仓库允许范围可检查。

日志要区分 `exhausted`、`non_retryable`、`retry_after_exceeded`、`unsafe_to_replay`、`physical_close_unconfirmed`、`cancelled` 等实际原因。不能在一次尝试后显示“已重试五次”，不能把 close 错误覆盖原始故障；两者都保留。

### 6.2 现有 summary 与计量

`ProviderRetrySummaryFact` 继续承载最终错误的既有结构化摘要；按实际已发出的 retry 修正其 traces/count 口径，并更新原 validator/serializer 测试，不添加 dual 字段兼容。计划等待在失败时单独记日志；trace 只有等待完成、进入下一次传输尝试时才计入已发生 retries。保留 trace 内“导致该次重试的前一次失败”的 attempt 编号，因此其现有连续性校验仍成立；等待取消不把未发生重试纳入 summary。成功恢复也必须在现有日志可观察。对参数异常与网络失败记录不同原因，但保持同一额度。

失败 attempt 的 usage/replay 不合入后一次成功输出，不把两个 provider 响应的 token 累加成一个响应。现有 provider_call_usage 保持逻辑请求观察、非账单审计的合同；无可靠 usage 时继续 missing/partial，不伪造零消耗。不新增每次 HTTP 重试的 durable usage 表或计费账本。

## 7. 实施文件与 hard cut

| 位置 | 必须工作 |
|---|---|
| `llm/retry.py`、`adapters/openai/retrying.py` | scope 内六次总尝试、统一退避/Retry-After、分类诊断和实际次数口径 |
| `adapters/openai/errors.py` | 确定性错误优先，精确识别可恢复连接失败与可恢复 EOF |
| `adapters/openai/chat_completions.py`、`responses.py` | 单 owner 重试，partial stream 安全替换，对等 per-attempt 物理关闭 |
| `ports/provider_stream.py`、`llm/normalized_transport.py` | 泛化现有 generation replacement，重置临时累积与 typed 原因；无新增 live kind |
| `conversation_kernel/direct_model.py`、`runner.py`、`host.py` | 一次安装、不可逆边界、草稿 ABORT、观察 slot、停止竞争、最终提示与日志 |
| `primitives/model_call.py`、`llm/provider_sanitization.py` | connection 稳定错误码、既有 summary/错误映射消费者一致更新 |
| desktop observation / frontend runtime types、adapter、会话运行区 | ROOT/Subagent scoped 当前重试状态、倒计时、停止、重载与清理 |
| 活跃规格、开发说明与测试 | 删去 superseded 的“一有 delta 就禁止恢复”等描述；只有本轮采用的单路径 |

生产代码中删除旧条件及仅针对参数异常的重复替换分支，整合到现有单路径。保留参数异常自身成立的校验条件。数据库 clean-v0 无 schema 改动，不执行数据库重置；无迁移、兼容 wrapper、旧/新策略双轨。

## 8. 验收矩阵

全部验证必须通过真实 SDK/adapter/Runner 边界，不能只 mock `can_retry=True`。网络注入使用本地可控 HTTP/SSE 服务，配合原 SDK；时钟、sleep、随机源可注入加速，但至少一项验证真实可取消等待。

| 类别 | 必测场景与断言 |
|---|---|
| 计数/退避 | 六次均失败精确六次发送、五次等待；第一/中间/最后重试成功；2/4/8/16/30 与抖动边界；等待取消无额外发送；计数不跨逻辑请求累计 |
| 单 owner | SDK implicit retry=0，HTTP 失败+流中断+参数异常共享额度；没有 Runner 二次循环或乘法次数 |
| 错误分类 | 连接拒绝/reset/超时/EOF/临时 SSE 错误可恢复；401/403/400/quota/证书失败/无效代理/未知异常不重试；connection 不再 unknown |
| Retry-After | 数字/日期/零/负数/非法/NaN/Infinity/大于30/大于600；不早于服务端值；超界明确终止；summary无法表达原值时null且不伪造600秒；等待中停止 |
| Chat 与 Responses | 思考、正文、工具名/参数任意阶段断流；正确替换草稿/累积器/replay，旧响应不混入成功结果；无 terminal EOF 与明确业务 incomplete 区分 |
| 不可逆边界 | 工具完整但未派发可重新生成；已 commit/dispatch 或结果未知禁止重放；旧已执行工具保持一次；extra_body/defaults覆盖tools/托管执行能力的opening与partial失败均不重放 |
| 物理关闭 | 每次重试前旧 iterator/response 已退出；close 失败无新请求；成功 terminal 后断链不再生成；无双流、连接泄漏或悬挂 task |
| 前缀/首次放行 | 每次 wire input 完全一致、只安装一次、无新 epoch/rebase；steer 不混入重试；重试不再调用权限 gate、不重读设置或替换 credential；退避中编辑权限/连接配置不触发新增授权流程；显式停止仍阻止后续请求 |
| 取消竞争 | closing、waiting、admitting、streaming 各屏障取消；取消原因优先；Host close/owner 更换后无晚到重试或提示复活 |
| ROOT/Subagent | 一支重试时其他分支能完成，父 wait 不收到假 stopped；第5次恢复后正常综合；最终耗尽沿原依赖失败语义，不丢已完成材料 |
| UI | 正确会话显示与切页恢复、倒计时、重新连接、草稿替换、成功/取消清除；旧 callback 不清新 slot；重试不产生 canonical 消息/历史通知洪泛 |
| 日志/秘密 | 三阶段错误链、最终 skipped/exhausted/success、实际计数可查；注入credential、URL userinfo、percent-encoded token与JSON转义值，确认最终日志及terminal public detail无泄露且保留正常上下文；循环异常链可结束 |
| usage/durability | 失败/成功 usage 不拼接，replay 仅来自最终响应；durable oracle 各类别增量零；无新 live kind/registry/指纹 |
| 长程 | 超过六次成功逻辑请求，每个请求仍有五次额度；多次 turn/Cell/Subagent 后持续可用，无任务总次数/寿命 cap |

### 8.1 激活所需证据

1. 聚焦 pytest/前端测试及 repo 要求静态检查通过；失败不加 skip/xfail 或弱化断言。
2. 本地真实 SDK 故障注入：Chat 与 Responses 至少各跑一次“部分输出 → 断流 → 重试成功”，以及一次“所有尝试失败”。保存 request 输入对比、物理退出、工具执行次数及用户可见结果；不记录代码/文档哈希。
3. 使用用户保存生产设置，经 LocalSettingsStore 与 require_pulsara_home 只读加载，做真实 provider 的 ROOT＋并行 Subagent 长程 dogfood。生产 endpoint/代理/凭据保持原选择；不主动断开用户全局网络。可通过测试注入在 SDK 消费流的受控位置模拟断连，但必须标明为本地注入，不能冒充远端真实故障。
4. 真 provider 只对实际配置支持的 wire_api 给实测结论；未配置另一协议时以本地 SDK 验证覆盖并明确证据边界，不擅造生产连接。真实故障不出现也不能替代受控断流验证。
5. 激活报告说明已验证的安全边界、恢复成本和仍存在的重复推理计费风险。规格完成不等于代码或 dogfood 已完成。

## 9. 完成标准

普通 ROOT/Subagent 的临时连接失败及安全的部分输出断流，按同一六次总尝试和退避合同恢复；不能恢复时准确结算。无重复本地工具、无错误草稿拼接、无前缀重建、无停止失效；UI 与最终日志可以解释每次恢复及停止理由。所有验收通过且证据可核查后，才把本文状态改为实施完成。

## 10. 规格审查结论

实现已完成执行安全、产品观察和跨模块独立审阅，并通过 ROOT 对生产代码及原始激活证据的最终语义验收。§8 的每个类别对应实现 owner、聚焦断言、真实 SDK/Provider 场景和证据位置，见[逐项语义矩阵](output/unified-input-retry-20261031/verification-plan.zh.md)。

本规格的权限合同以用户明确决议为准：首次 gate 放行覆盖同一逻辑请求的全部重试，具体合同见 §1、§4.5。安全审查范围包括 typed 网络 EOF 与协议 EOF 区分、成功 terminal 后物理失败结果，以及最终有效 wire 请求的副作用证明。产品审查范围包括 current execution 观察入口、observer 可读及显式 null 清除、30/36 秒口径、超范围 Retry-After、真实重试计数、URL 凭据与停止目标。均沿现有 owner 实现，不增加持久机制。

本地真实 SDK 的 Chat/Responses 八场景、真实 DeepSeek 十一场景及必要回归均已验收。证据保留本地故障注入、usage/计费和源码进程边界，不能将单次本地工具作用解释为单次远端推理或计费；具体见[激活报告](output/unified-input-retry-20261031/activation-report.zh.md)。
