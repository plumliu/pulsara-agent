# MCP 调用失败后的对话续行

## 边界

MCP SDK 负责协议、响应流和传输重连；Pulsara 负责把调用失败作为工具观察交给模型，
并保留副作用的不确定性。一次工具断连不应直接终止 ROOT 或 worker 的 agent loop。
此规范取代历史实现中“这类 MCP 调用结果未知就中断整轮”的行为。

## 实现

- 对已发出的调用，SDK `MCPError`、传输异常和工具超时通过现有 `SYSTEM_ERROR`
  工具结果提交。正文保留具体错误，并明确远端结果未知，不能推断未执行或成功。
- 只读调用允许模型选择其他来源或重新尝试；可能有副作用的调用，重复前必须验证
  结果或幂等性，否则与用户确认。Pulsara 不自动重放 `tools/call`。
- 未发出的调用仍返回既有 `MCP_TRANSPORT_UNWRITTEN`。已收到的应用错误和载荷校验错误
  保留原有结果。远端调用范围内的普通异常与不支持的额外交互也交给模型处理，遵守
  [工具续行规范](PULSARA_TOOL_FAILURE_CONTINUATION_SPEC.zh.md)。所有权错误、用户停止、
  Host 关闭和 canonical 写入失败继续使用原边界。
- 直接工具、`use_new_mcp_tool` 和资源/提示词读取共用此语义。故障连接仍由现有
  supervisor 隔离和重连；后续工具可以得到正常结果或明确的不可用结果，模型继续收尾。
- 错误文本复用 SDK 适配层已观察到的凭据和 process credential boundary 脱敏，
  只删除实际凭据值。结果未知通过已有工具正文表达，不新增 schema、事件、恢复任务、
  重试机制或 hash。SYSTEM/tools 前缀保持不变，工具结果仅追加到当前历史。

## 验证

复现 `SSE stream ended and reconnection attempts were exhausted`，覆盖只读及有副作用
策略、直接及动态工具入口、超时与发送前/后断连。验证错误只提交一次、不重放远端调用、
同批其他工具与下一次模型调用继续、具体错误可见且凭据不泄露、输入前缀不变；同时验证
取消、内核所有权失效和持久化故障仍遵守各自的停止边界。
