# Process-local Execution Contract

## 1. Provider stream

Normalized provider events、Text/Thinking/Data/ToolCall/ToolResult
Start/Delta/End与assembler draft只存在于当前进程。它们不写数据库、不跨 Host恢复，也不拥有
canonical success。Final assistant blocks按 Start ordinal冻结后一次提交。

每个 provider attempt最多一次 physical provider call，并在 dispatch前执行 bounded target
token admission。取消必须等待实际 transport owner退出或由 Host close持有明确 blocker。

## 2. Terminal

Terminal process manager只拥有：exec/yield、owner-scoped list、bounded output snapshot、stdin
write/close、kill、owner-scoped close/join。DTO不携带 AgentEvent、run/reply origin、completion
candidate、semantic settlement、retry receipt或 durable monitor identity。

Yielded process绑定当前 Host。Host close会 stop admission、kill/terminate并 bounded join；不会
把 process恢复给下一个 Host。

## 3. Subagent

Subagent task coordination与task-scoped conversation是 canonical rows；实际 asyncio task只属于
当前 Host。Host关闭映射为 `INTERRUPTED`，用户显式 stop才映射为 `CANCELLED`。不保存 execution
lease、teardown generation、checkpoint或resume carrier。

## 4. Live event

Live bus是 bounded non-blocking ring。Consumer慢、detach、overflow或失败只能导致 process-local
GAP/丢帧，不能阻塞 model stream、tool effect、canonical commit或 close。

## 5. Close

统一顺序：

```text
stop admission
-> cancel/terminate process-local owners
-> bounded physical join
-> commit canonical interruption / release Host writer
-> release verified resources
```

Close不等待 reducer、projection、presentation、audit或notification semantic success，因为这些
owner不存在或不属于 durable completion boundary。
