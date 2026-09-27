# Pulsara 供应商输出额度实施规格

状态：已实施（2026-09-28）。依据本次用户指令及 `AGENTS.md` 第 3、6、7 条。此规格取代历史文档中的固定 8,192-token 默认输出与 16,384-token 前台输出上限。

## 所有权与预算

- models.dev 目录或自定义连接中用户声明的 `ModelHardLimits` 拥有模型的 context/input/output 边界；继续复用现有 catalog、target resolution、冻结与 adapter，不建立第二套额度配置。
- 正式模型调用采用模型声明的最大输出，推理档位不改变该额度。ROOT、SUBAGENT 与 compaction summary 共用这条解析路径，不按模型名称分支。
- `default_output_tokens` 与冻结预算的 `effective_output_tokens` 表示本次调用允许使用的输出天花板，正式调用等于 provider `max_output_tokens`；它们不再表示预先从所有输入预算中扣除的固定 reservation。
- 输入和输出的声明上限可以重叠（包括 `output == context`）。输入 admission 预算为 `min(max_input_tokens, total_context_tokens - 1) - input_safety_margin_tokens`。减一仅保证实际发送前至少存在一个输出 token 的空间。沿用既有最多 8,192-token 的输入估算余量，并保证预算为正。
- 实际请求输出额度为 `min(输出天花板, provider max_output_tokens, total_context_tokens - final_wire_input_tokens - safety_margin)`。空间足够时完整采用供应商最大输出；空间不足时只由当前输入占用的共享窗口决定。输入本身超限则沿用 typed budget failure，不自动删减上下文或发起不确定重试。
- 编译调用复用已有冻结 `final_wire_estimated_input_tokens`，包括 SYSTEM、tools、replay 与图片估算；无编译计划的 direct/probe 调用使用同一个 v2 final-wire estimator 和 adapter projection。两种协议共用一个无状态计算函数，不添加预算 DTO、缓存或持久事实。
- 移除 runner/dispatch/model preparation 的独立 output cap 参数与校验。冻结物理调用中的 maximum output 来自模型 hard limit，effective output 来自解析预算。
- Chat 的 `max_completion_tokens` 与 Responses 的 `max_output_tokens` 继续显式携带相同额度。compiler、preflight、compaction 采用同一模型输入边界；adapter 以被冻结输入的真实 wire quote 计算输出，不能只使用更小的 semantic estimate。
- 连接测试保留既有 64-token 短请求边界：它只测试连通性，不承担用户任务。现有 per-call purpose override 只用于该 probe 和冻结预算再现，正式推理路径不得用它添加固定输出上限。删除未使用的 target-level output override，避免留下旧策略入口。

## 连续性与失败

本次不改 SYSTEM、tools 或 messages，不赋予预算变化新的 epoch 重建权。已经冻结的目标仍按既有机制验证；采用新预算发生在合法的重新解析边界。供应商真实达到最大输出仍沿用 `OUTPUT_TOKEN_LIMIT`，不能将截断视为完成。

不修改数据库或增加事件、持久 job、累计 token/轮次/工具次数/任务寿命上限。除下文明确删除的 normalized transport 计数外，现有存储、解析、并发和输入资源边界保持原语义。

## 流式片段数量

2026-09-28 的 Luna `max` 实际运行触发了 `transport_source_item_limit_exceeded`。用户明确要求删除这类最大数量限制。

- SDK/供应商拥有流式分片方式；Pulsara 只转换和验证语义事件、保留完整内容并结算终态。分片数量不能决定一次合法回复是否允许完成。
- 删除 normalized transport 的每次调用 16,384 个事件上限、计数状态和常量，不换成更大的数值或配置项。
- 删除 Chat replay 的每次回复 65,536 个片段/条目累计上限、计数状态和常量。该计数原先也会对每个文本 delta 增一，因此只是另一条取决于供应商分片方式的隐式输出上限。保留 replay 内容的精确顺序、终态对账与既有字节内存边界。
- 同步移除 durable Chat replay 的 65,536 个 `reasoning_details` 条目上限；已完成 replay 的解码节点额度由实际 payload 字节数推出（每个 JSON 节点至少占一个输入字节），沿用既有 payload 字节与嵌套深度边界。这保证 adapter 已接收的碎片不会在提交或下一次输入 hydration 时再撞上另一条固定计数上限。
- 不添加重试或截断补救，不改变 SYSTEM/tools/messages、取消、物理流关闭、真实 provider 输出终止原因，以及单个 SDK JSON 和 Responses canonical output 的既有结构边界。
- 验证完整正文与工具参数在超过原 16K 分片边界后仍能完成；Chat 文本和数组 replay 在超过原 65K 边界后仍精确保留，并验证取消、协议错误与字节边界继续生效。随后使用原 Luna `max` 会话继续真实分析与前端可视化验收。

长输出实际验收同时暴露了工具参数预览直接切断 UTF-8 字符的问题：一份包含中文的长 `write_file` 参数在 32 KiB 预览末尾留下了半个字符，前端因此拒绝整个会话内容。预览由 Python 标准库 UTF-8 增量解码器退回完整字符边界；预览仍是有界的派生显示，完整参数、长度、digest 与按需内容读取保持原值，不删减模型正文，也不放宽前端完整性验证。

## 流式字节计数的所有权

同一份 230,000 字节正文，每片一个字符时会触发 normalized transport 的 16 MiB 累计边界，每片 100 字符则仅累计约 0.60 MiB 并成功。该计数重复包含事件标识、字段名、结束时完整结果和 replay，不能作为正文大小或当前内存占用的代理。

- 删除 normalized transport 的累计序列化字节数、16 MiB 常量以及终态的 `累计事件 + replay` 检查，不以更大常量、倍率或配置替代。
- 删除此层 256 KiB 单事件序列化检查。语义事件的 delta 和 end 都来自 adapter；end 按现有协议承载完整结果，不是独立网络小包。分片大小和结束时重述完整内容均不构成输出拒绝理由。
- 本层继续拥有 start/delta/end 一致性、终态顺序、错误分类、取消与物理流关闭；供应商/SDK 拥有网络解码，adapter 继续执行已解码 SDK JSON 的既有 16 MiB 字节与结构检查。
- ROOT、SUBAGENT 和 compaction 继续使用已有 `ProviderStreamAssembler`：按正文、思考、工具参数和 data 的实际新增 UTF-8 内容合计，在保留增量前执行既有 4 MiB 单次消息组装边界。它比 SDK/replay 的边界更紧，单独通过 normalizer 的 9 MiB 测试不代表生产允许 9 MiB 正文。
- Chat replay、Responses 完整输出和 canonical blob 保留各自既有的 16 MiB 内容边界。这些边界作用于实际保留或存储的结果，不累计已消费事件的包装成本。live ring 继续复用既有有界缓存与失效/重新读取机制，不将慢前端变成模型任务失败。
- 连接测试直接消费 transport，没有 kernel assembler。为保留对忽略 64-token 请求的异常端点的保护，仅在 probe 消费循环按 delta 的实际新增 UTF-8 内容复用既有 16 MiB 工作集边界，不累计 start/end 包装，不把探测限制传播到 agent 任务。超限返回明确探测失败，并继续 `finally` 中的关闭和物理完成验证；不引入新常量。
- 无编译计划的 Chat 请求在连续 TOOL_CALL 合并后，将同一份 wire items 同时用于发送与估算，并保留 USER 图像来源。不能逐条估算尚未合并的消息，导致临近窗口上限时误报输入超限；有冻结 plan 的调用继续使用已有 quote。
- 不新增持久中间文件、流式协议、恢复机制或资源额度配置。保留供应商输出 token 上限和真实共享上下文计算；本次并不承诺任意大的单个结果可存储。
- 验证相同正文/推理/工具参数按单字符、小片段和整块传输时精确一致；覆盖超过原 16 MiB 事件累计值和 256 KiB 完整结束事件，验证 replay 不重复计费、持久保存/读取、协议错误、取消和资源边界。

## 验证

1. 两种 wire API、不同 reasoning effort、目录与自定义连接，输出等于各自声明值，覆盖高于旧 8K/16K 的 128K 与 384K。
2. 检查输入 admission、provider hard limit、非正预算和共享窗口边界（包括 output 等于整个 context），不通过删减 SYSTEM/tools/messages 满足新预算。
3. direct model 的 preparation、compiler binding 与冻结预算保存相同输出天花板，实际 payload 符合共享窗口公式；模型切换、compaction、连接 probe 与前缀连续性相关回归保持通过。
4. 使用保存配置只读重建 Luna 生产请求，确认实际输出参数为目录声明的 128,000；不为验证额度继续已经被用户暂停的数据分析任务。

实施验证：相关 584 项测试通过，包括两种 wire API、ROOT/SUBAGENT preflight、共享窗口边界、provider 不完整输出、模型切换、compaction 与前缀连续性。保存的 OpenRouter Luna 配置在 `max` / `high` 下均重建出 `max_completion_tokens: 128000`，未发送推理请求。

流式片段限制移除后的验证：227 项相关回归通过，覆盖超过原片段边界的正文、推理、工具参数、replay 提交与 hydration；工具参数 UTF-8 预览与协议回归另有 30 项通过。用户恢复任务后，原 Luna `max` 会话于 2026-09-28 01:34:02 至 01:51:55 完成数据分析，终态为 `COMPLETED`，产出分析脚本、结果表、报告和四个可视化。独立核对 2,200 位客户的 8,800 项计数/金额结果全部一致，原始 CSV 未变；重启后的前端可加载完整会话并展开四张图，图例隐藏与恢复有效。窄屏部分多子图标题仍拥挤，真实鼠标悬停未取得可靠自动化证据，不将这两项记为通过。

## 最终冻结审阅（2026-09-28）

首轮字节计数删除后 233 项测试通过，再按用户要求新建 GPT-6 Astra / xhigh critic，独立审阅当前全部输出 token、流式事件数量、replay 片段和序列化字节 cap 改动。审阅发现并协商修复：

1. 无 plan 的 Chat 调用按未合并的 TOOL_CALL 消息估算，多计 framing；改为实际发送 items 与估算共用一次转换。近满窗口回归证明合法输入仍能发送正确的剩余输出额度，保留图片来源和已有冻结 plan quote。
2. 连接探测没有 assembler，删除全局累计计数后需保留探测自己的内容资源保护；在 probe 内复用既有 16 MiB 边界，仅统计新增内容。真实 localhost SSE/SDK 验证超过边界时失败并正常关闭。

Critic 独立复现确认修复，并给出“可以冻结”的结论。最终采用上述单一路径，保留 4 MiB 实际消息组装、16 MiB 各自存储/解析边界、live 缓冲和协议语义；不恢复事件计数/重复包装字节计费，不新增全局 cap、持久对象或前缀重建路径。

最终验证：507 项合并回归通过，覆盖两种 API、输出预算、近满窗口、probe 真实 HTTP/SDK、重试、前缀连续性、compaction、长流和前端协议。另有 4 项真实 PostgreSQL 回归通过，使用保存的本地数据库配置创建并清理独立测试库；超过 65K 条 replay details 可精确提交、重读并经生产 decoder hydration。大于 1 MiB 的完整结束事件可使 disposable live ring 失效，但 assembler 仍完成精确结果。ruff 与 `git diff --check` 通过。
