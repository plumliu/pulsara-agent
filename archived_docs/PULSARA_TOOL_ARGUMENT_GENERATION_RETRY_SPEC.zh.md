# 工具参数生成失败的有限重试

本规格描述当前实现。它只扩大现有单次 provider 请求的重试条件，不改变会话、工具执行、provider 输入 epoch 或持久化结构。

## 边界与所有权

- SDK 继续拥有 HTTP/SSE 解码和响应关闭。SDK 的自动重试在 Pulsara 重试启用时保持关闭；它不能处理应用层的工具参数 JSON 校验失败。
- Chat Completions adapter 继续拥有一份冻结 payload 和一个 `LLMRetryConfig` 请求预算。网络失败和参数生成失败共同消耗该预算（默认总共 3 次物理请求），不会各自嵌套重试。该预算只约束当前 provider 请求，不限制任务的模型调用总量或寿命。
- 只有 `AGENT_MODEL_LOOP` 调用的 `transport_tool_arguments_invalid` 可以在已流出内容后重新生成：包括 JSON 解析失败，以及 JSON 合法但不是对象。身份关联、回放校验、其他协议错误、输出长度截断、过滤、工具执行失败不因此重试。其他调用目的保持原有行为。
- 重试前必须成功关闭失败的 SDK stream。关闭失败或取消不重试。等待仍使用现有可取消退避；用户停止会中断等待及后续生成。

## 回复接受与副作用

runner 必须完整收集并校验一份回复，随后才接受 assistant entry/replay，最后才派发其中的工具。失败回复中即使已有完整工具块，也不得接受或执行。

adapter 在关闭失败流后，用纯进程内 `ProviderGenerationRetry` 控制值通知消费者丢弃未接受的输出；它不是新增 live event、数据库记录或 durable receipt。normalizer 清除该次回复的块、身份与 usage 状态。runner 使用现有 live `ABORTED` settlement 结束旧草稿，创建新的 proposed entry / generation / assembler，并通过现有 presentation notice 显示“工具参数格式异常，正在重新生成”。最终接受、回放与 committed settlement 全部绑定最终成功草稿的 ID。

每次重生成使用完全相同的 provider payload。安装 permit 只消费一次；不重新编译、安装或重建 prefix，不附加纠错消息，不更改 SYSTEM/tools/messages，不重新执行此前已完成的工具。不会修补、补全或猜测非法参数，也不会伪造工具结果。

## 诊断与失败

失败参数的原始拼接字符串、终结 chunk、JSON 错误位置及尝试编号写入现有诊断日志；通过保存设置与当前 credential boundary 清除真实凭据值。日志不是执行权威，不新增持久化机制。

usage 保持现有单次逻辑调用语义：仅最后一次响应的 provider usage 可作为该调用结果；失败尝试不会被伪装成成功 usage，无法取得的计费数据不估算或累计为精确值。持续失败耗尽预算后，沿用现有失败终态及可继续的会话路径，保留重试摘要。

## 验证要求

- 实际 adapter + normalizer：同一 payload、工具调用 ID 复用、旧块清除；网络失败与参数失败共用预算；耗尽、其他 purpose/协议错误、关闭失败、取消均不越界。
- Host 集成：失败代含完整工具和非法工具，成功代复用 ID；只有成功代产生 canonical assistant/tool attempt/replay，副作用恰好一次。验证输入根不变且仅安装一次。
- 实际会话：继续使用保存的生产模型配置和正常任务，检查页面恢复、旧草稿消失与工具执行次数。确定性故障注入必须明确标记为注入，不能冒称 provider 自然故障；原始 provider 响应与注入内容分别保留。
