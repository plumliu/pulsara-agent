# Pulsara Chat / Responses 流形状兼容：增量实施规格

状态：已实施（2026-09-20）。本文件独立描述这次增量变更；`archived_docs/` 中的旧规格不是本任务的实现依据。实现时仍以根目录 `AGENTS.md` 和当前生产代码为约束。

## 1. 目标与边界

让现有 OpenAI-compatible Chat Completions 和 Responses adapter 接受两类已知的合法流形状，而不引入 StepFun 专用 adapter、供应商名称判断或自动切换 API：

1. Chat 流可在每个 chunk 携带**累计** usage，而非只在流尾给一份；空字符串 `delta.role` / `finish_reason` 可表示该 chunk 未声明角色 / 尚未结束。
2. Responses 流可逐块展示 `reasoning_text`，但最终 reasoning output item 的 `content` 为 `null`、`summary` 为空。该文本只是一条当前调用的 live observation，不是假装存在于最终回放中的历史内容。

两个 API 仍由用户声明的 `wire_api` 和 Base URL 决定。StepFun 文档给出的 Chat Step Plan 地址是 `/step_plan/v1/chat/completions`，Responses 地址是 `/v1/responses`；不能从前者推导后者也在 Step Plan 路径，不能在失败后偷偷改 URL 或 API。2026-09-20 用用户指定连接实测 `/step_plan/v1/responses` 确实返回 `response.completed`，但这只是该连接的观察，不是文档化的端点保证。[StepFun Chat 文档](https://platform.stepfun.com/docs/zh/api-reference/chat/chat-completion-create)、[StepFun Responses 文档](https://platform.stepfun.com/docs/zh/api-reference/responses/responses-create)。

本变更只扩展**通用流形状的解释**。不修改 canonical row、数据库 schema、provider input epoch 边界、SYSTEM/tools 前缀、已有消息前缀、连接配置格式，也不增加 durable event、回放 reducer、用量快照表、供应商注册表或迁移期双路径。模型切换仍由 canonical row 在合法边界重新投影；流输出适配不产生新的前缀重建权。

## 2. 保留什么，丢弃什么

边界原则：只把影响 Pulsara 产品语义、结算或下一次 provider 输入的材料带过 adapter；不要求对供应商的每一个字段建立观察与证明。

| 类别 | 处理规则 |
| --- | --- |
| 正文、工具调用 ID / 名称 / 参数原文、完成与截断状态、所需的最终 replay item | 保留并沿用现有精确校验；不能为了“兼容”静默丢弃。工具不得因解析放宽而提前执行。 |
| Chat 中途的累计 usage | 直接丢弃，不求和、不逐块发 `TransportUsageReport`，不入 canonical / durable history。 |
| Chat 终止 chunk 或其后的纯 usage chunk | 仅保留**最后一份完整且可结算的** usage 候选；模型调用在语义终止且物理流结束后才把它作为唯一 normalized usage 发出。 |
| Responses `reasoning_text` 的流式片段 | 可供当前调用的 live thinking 展示，保持现有 delta / done 一致性验证；若最终 item 不携带这段文本，调用结束后丢弃，不写入 canonical reasoning，也不补造 replay 字段。 |
| `reasoning_part.added/done`、`sequence_number`、未知 usage 扩展 / breakdown、message 上的空 `summary`、function_call 上的 `namespace:null`、与产品无关的顶层遥测 | 不建完整镜像、不建立新的持久校验状态；只有已证实为空且不影响 replay 的字段可丢弃，非空未知语义仍失败。 |
| 未知或冲突的正文 / 工具调用 / 最终 replay 语义 | 仍按现有 closed contract 失败；“可丢弃”不适用于可能影响下一轮输入或副作用的材料。 |

usage 的最小可用值是输入与输出 token 的非负整数，以及现有 `ModelTokenUsageFact` 已支持且实际给出的缓存 / 推理 token 数。`total_tokens` 继续按现有规范化规则由输入、输出相加，供应商 total 不一致仍可产生现有诊断。未知细分字段直接忽略，不因它们新建 DTO 或字段目录。若终止 chunk 和随后纯 usage chunk 都没有完整用量，结果为现有 `usage_status=missing`，**不能**把较早的中途累计数冒充最终结算。流中断、取消或协议失败也不能用中途快照冒充已结算用量。

## 3. Chat Completions 的单一路径

当前 `ChatCompletionAccumulator.apply` 在第二份 usage 到来时抛出 `transport_usage_report_duplicate`，正好拒绝了上述累计形状；其 `role` 和 `finish_reason` 判断也不接受文档示例里的空字符串。实施时替换这些判断，而不是另起一条 StepFun 路径。

1. `usage` 处理不再依赖“整个 stream 只能出现一次”。非终止 chunk 的 usage 只当作可丢弃的临时统计。`finish_reason` 为有效终止值的 chunk 上的完整 usage，以及其后 `choices=[]` / 无 choice 的纯 usage chunk，才是最终候选；后来的完整候选覆盖先前候选。仍只向 normalized transport 交一份报告。纯 usage chunk 可以出现在语义终止之后、物理 EOF 之前；其他语义输出不得在终止后继续出现。
2. `delta.role` 为 `""` 时，仅视为“本 chunk 未重复声明 role”；只有 `"assistant"` 能明确声明角色，其他非空角色继续失败。`finish_reason` 为 `""` 时，仅视为“尚未结束”，与 `null` 一样不能发 terminal；`stop`、`tool_calls` 和现有不完整结束值的语义不变。工具调用的后续参数 chunk 若将 `type` 写成 `""`，也只视作未重复声明类型；其他非空且非 `function` 的类型继续失败。只对这些已确认的空值作窄归一化。
3. 文档可同时提供内容相同的 `reasoning` 与 `reasoning_content`。两者在 provider replay 的受支持字段中仍按实际完成值保留，以便下一轮采用当前 replay contract；live thinking 只跟随先出现的一个顶层文本字段，不在流中猜测另一个字段是否镜像。最终从 replay 派生的用户可见 reasoning 对**逐字相同的别名**只展示一次，不同文本仍各自保留；不以供应商名称判断，不把整段推理存成第二份 canonical 文本。
4. `finish_reason`、文本和工具调用继续沿用当前的一选择、精确参数、终止和物理完成检查。EOF 本身不能证明模型成功；不增加隐式 retry / fallback。

StepFun 的 Chat 文档逐块给出累计 usage，且示例以 `role:""`、`finish_reason:""` 表示非终止块；OpenAI 官方文档则规定 `include_usage` 的用量通常在最后一个空 choice chunk 提供。这是**两种 Chat 流形状**，不是两种 provider adapter。[StepFun Chat 流示例](https://platform.stepfun.com/docs/zh/api-reference/chat/chat-completion-create)、[OpenAI Chat `include_usage` 语义](https://developers.openai.com/api/reference/resources/chat)。

## 4. Responses 的单一路径

当前 `ResponsesCompletionAccumulator` 已能接收 `response.reasoning_text.delta/done`，但 `_project_completed_response` 要求最终 reasoning item 的 `content` 或 `summary` 含有逐字相同的流式文本。StepFun 文档示例并非如此：流式推理文本出现过，最终 item 却是 `summary:[]`、`content:null`。应允许这一**精确的 stream-only thinking 形状**，而不是伪造最终 reasoning 内容。[StepFun Responses 流示例](https://platform.stepfun.com/docs/zh/api-reference/responses/responses-create)。

规则如下：

1. 若最终 reasoning item 的 `content` 无文本且 `summary` 无文本，同 index 的 `reasoning_text` **必须收到 `done` 且与累计 delta 完全一致**，方可作为 process-local live thinking 结束；它不参加最终 item 的逐字比较，也不写入 replay 或 canonical completed content。该 index 仍须对应一个合法的最终 reasoning item；不得容忍孤立或未完成的 reasoning stream。
2. 若最终 item 确实携带 reasoning 文本，继续使用现有逐字比较 / 精确 summary alias 规则；若流与最终非空文本矛盾，仍失败。`response.output_text`、`function_call`、`output_item.done`、`response.completed` 的精确验证完全不放宽。
3. 最终 provider replay 只取真实 `response.completed.output` 的受支持 item。下一次请求不能将已经被供应商从最终 item 中省略的推理文本“补回去”；也不能用 `previous_response_id` 或远端状态弥补缺口。当前模型调用可以显示 live thinking，但历史重载及跨模型 canonical 投影不承诺恢复它。
4. `response.reasoning_part.added/done` 只是可忽略的容器提示，不需要为其造新的配对状态、事件或数据库字段；已有 `reasoning_text.done` 与最终 output item 足以界定这次增量的产品语义。
5. 同一响应可以用 `reasoning_part.*` 表示推理项，用 `content_part.*` 表示正文项。`content_part` 的完整序列只在实际发出这种事件的 item 内验证；不能要求未走该事件形状的 reasoning item 也发出 `content_part.done`。若某 item 发了 `content_part.added` 却未 `done`，继续失败。完成的 message 上 `summary:null` / `summary:[]` 以及 function_call 上的 `namespace:null` 是空的非语义字段，可在 replay 归一化时丢弃；相应非空字段仍失败。这些 replay 归一化规则必须进入现有跨重启 replay contract 指纹，不额外创建新指纹。

## 5. 请求边界与实施位置

继续使用现有 OpenAI Python SDK 发请求、处理 HTTP/SSE 生命周期；Pulsara 只负责自己的 provider-input materialization、stream 语义和 replay。已安装 SDK 的 Chat create 接受 `max_completion_tokens` / `stream_options`，Responses create 接受 `max_output_tokens` / `store`；不能因第三方文档未列某字段就臆测 SDK 或服务器会拒绝，也不能由此发明 provider-specific request dialect。

预期代码改动集中在 `src/pulsara_agent/llm/adapters/openai/chat_completions.py`、`src/pulsara_agent/llm/adapters/openai/responses.py`，以及为“相同 Chat reasoning 别名只展示一次”确有需要的 `src/pulsara_agent/llm/provider_replay.py`。`src/pulsara_agent/llm/adapters/openai/events.py` 的 usage 正规化可复用；如需修补非负整数校验，应对两类 wire API 统一生效。`src/pulsara_agent/llm/normalized_transport.py` 的“一次 normalized usage”合同不改。

当前 Chat 请求仍使用 `max_completion_tokens`，Responses 请求仍使用 `store:false` 和 `max_output_tokens`。2026-09-20 使用指定 key / Base URL 的真实 Chat 与 Responses 短请求均已被服务端接受，因此本次不改变这些请求字段。StepFun 文档未列其中某字段不等于服务端拒绝；若以后观察到明确的请求字段不兼容，应单独修订这份规格，定义**用户可声明的请求形状**及其与 provider-input 前缀的关系，再实施；不得在运行时按供应商名重试另一组参数，或让失败后自动切换 Chat / Responses。

## 6. 验收与失败边界

实现时至少覆盖以下契约测试，复用现有测试文件与构造器，不创建一套平行 adapter 测试框架：

- Chat：OpenAI 式尾部单次 usage、StepFun 式每块累计 usage、无最终可用 usage、`role:""` / `finish_reason:""`、终止后纯 usage、终止后多出的正文 / 工具数据、流中断、无有效终止、重复的 reasoning 别名、工具调用跨轮 replay。断言只发一次 normalized usage，统计取最后的**终止合格**值，且不会把累计值相加。
- Responses：流式 `reasoning_text` + 最终 `content:null` / `summary:[]` 能成功；live thinking 可见但最终 replay 仍为真实空 item；最终非空文本相同 / 不同的正反例、孤立 reasoning index、正文或工具调用与最终 item 不一致、`response.incomplete` / `response.failed`、物理流未正常结束都维持现有失败语义。
- 连接 probe：仍用用户选择的普通 Chat 或 Responses adapter、原样 Base URL 和短请求；失败信息保留具体 HTTP / 协议原因，不伪装成认证或模型问题。用已保存的生产连接或用户显式提供的临时连接做真实模型 dogfood，至少各跑一次 Chat / Responses 文本响应及可行的工具调用继续执行；保存配置只读，实际密钥不写入规格、fixture、日志或提交物。若 Responses 连接实际未配置为可用端点或凭证不具备权限，准确报告该外部阻碍，不替用户改配置。
- 前缀回归：验证本改动不重建已安装 SYSTEM/tools，不修改现有 epoch 的历史 message 前缀；下一轮只附加合法 suffix。最终 replay / canonical 投影仍以真实终止输出为准。

激活标准：上述通用形状在测试中通过，真实 Chat 连接不再因重复累计 usage 失败；真实 Responses 连接若具备文档所示端点与权限，其 stream-only thinking 形状不再触发最终 reasoning mismatch；其他协议错误仍可诊断。规格本身不授权删改用户保存的连接或创建新数据库状态。

2026-09-20 实测记录：用户显式提供的临时 key 和 `https://api.stepfun.com/step_plan/v1` 只在进程内使用。Chat 与 Responses 均通过原请求形状的短文本连接 probe；两种 API 均完成真实工具调用与下一轮继续请求，Responses 的继续请求直接携带 adapter 归一化后的真实最终 replay items。Responses 在该 Base URL 上可用是实测事实，但仍非官方文档承诺。未写入用户保存配置或数据库，未持久化密钥。
