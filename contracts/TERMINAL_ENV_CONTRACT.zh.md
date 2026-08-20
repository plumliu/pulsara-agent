# 已退役：旧 Terminal Environment Contract

当前`terminal_process` manager是neutral process-local tool leaf，见
[Process-local Execution](PROCESS_LOCAL_EXECUTION_CONTRACT.zh.md)。bundled UI launcher已经物理
删除；未来独立client只能通过[Terminal Protocol v3](TERMINAL_CLIENT_PROTOCOL_V3_CONTRACT.zh.md)
连接，不能成为terminal process或conversation authority。
