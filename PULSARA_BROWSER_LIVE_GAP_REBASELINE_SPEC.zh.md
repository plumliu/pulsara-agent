# 浏览器临时进展缺口的同连接续接

状态：本次普通长会话发现的产品修正。本文定义当前 LIVE_RING_OVERFLOW 分支；历史 protocol 文档仅提供背景。

## 真实触发

2026-10-18，1018test2 在团队看板任务运行时打开浏览器，持续显示正在重新连接，输入框不可编辑。独立 public bridge observer 重现：connect 13.54 秒，随后正常读取两页历史（677 entries）和三个正文，首次 observe 返回 LIVE_GAP / LIVE_RING_OVERFLOW，live revision 从 186141 变为 187883。没有人工等待、注入或新增任务负载。证据位于 output/runtime-million-20261018/case2/reconnect-investigation/。

BrowserRuntimeConnection.open 先建立 observer HELLO（此时取得 live cut），再读取 canonical snapshot 和 live-control snapshot；frontend initialize 又读取较早历史及正文后才开始 observe。繁忙生成期间可以在这些正常读取之间溢出。原页面收到任意 GAP 就关闭整连接并重新载入全部历史，重复同一耗时窗口。

## 所有权与单路径

- canonical committed suffix、已验证正文缓存、连接/controller generation 保持原所有权。live 缺口不代表 canonical 丢失，也不撤销已受理执行。
- 仅精确 LIVE_GAP / LIVE_RING_OVERFLOW 在同连接处理。gateway 保持现有 live_bus.subscribe 路径重新建立 process-local observer，返回 fresh live epoch/revision 和已有连续 committed batch；本缺口响应没有 live 后缀。没有新的协议字段、event、durable row、回放恢复或历史上限。
- 丢失的临时过程无法重建。客户端丢弃旧 drafts、临时 tool results 和 task progress，应用本响应连续 committed；提示“部分临时进展已丢失，已从当前进展继续；已保存的会话记录不受影响。”后续新 live 与最终 canonical entry 继续呈现进展，不声称已恢复缺失内容。
- 随后单次调用既有 live-control-snapshot，采用它的 current TODO、interaction 和 cursor。它发生在本响应应用之后，因此较新的当前状态最终权威。
- 客户端不再次 connect、不重新读取全部 history、不重置 canonical 正文缓存。下次 observe 使用本响应的 committed cut 和 fresh live cut；缺失的执行/输出不会被重新运行。
- COMMITTED_GAP、LIVE_CONTROL_GAP 和其它 LIVE_GAP 原规则不变，不在此分支假设连续性。

## 范围与资源边界

生产 gateway、protocol frame 和 canonical observation 的资源行为不变。此修复仅改变浏览器对已有精确 gap 的处理，不新增 retained suffix、frame clamp 或可选后缀降级路径。重新订阅的新 live cut 解除初始化读历史期间产生的缺口；客户端不再重复整个耗时初始化窗口，而是从该 cut 接受未来观察。若未来又发生缺口，同一规则丢弃临时态并继续，不声称恢复执行。

## 验证

- 真实旧 gateway 的 public bridge 观测已记录 fresh cut 与连续 committed；新前端部署后，同一运行中的 ROOT 自然遇到 LIVE_RING_OVERFLOW 提示，仍在线可编辑并继续接收进展。
- 前端测试针对实际空 live 后缀响应，保留 controller identity、已 hydration 的正文及新增 committed entry，清理旧 draft/progress；较新 live-control baseline 最终权威；下一 observe 使用新 cursor，不产生第二次 connect/history/content read。
- 其它 gap 保留原处理；既有 stage2 protocol 测试继续验证未改动的 owner。
- 浏览器缺口的续接沿用正在运行的 Python 服务，Host 与后台任务保持原所有权。

## TODO 当前值与临时通知的边界

TODO 的当前权威仍是 Host 的 `TodoRunStateOwner.current_snapshots()`。普通 live 流与 live-control snapshot 的读取切点不同；取得较新 baseline 后，下一页普通 live 仍可能带较旧 TODO。不同 run 的 revision 也不能互相比大小，且 CLOSED 是 best-effort 投影，不能把必达关闭事件当作下一 run 的准入条件。

- `TODO_SNAPSHOT_UPDATED` 只使当前 TODO 失效，不再用其 payload 覆盖 current_todos。连接初始化保留返回的 owner baseline，retained live TODO 不改写它。
- 每个包含 TODO 通知的 observe 批次，应用普通 suffix 后至多调用一次既有 live-control-snapshot，读取当前 owner TODO；多条通知合并为一次读取。与 LIVE_RING_OVERFLOW 重合时共用该次读取，不增加第二次请求。
- 普通 TODO 刷新只替换 current_todos；interaction 与对应 control cursor 仍由原 control suffix 连续消费。仅精确 gap 分支采用完整 baseline 及其 cursor。既有 snapshot endpoint 虽重建 control subscriber，但 control owner 按请求 epoch/revision 与保留 ring 验证连续性，不要求跳到新订阅切点。
- 不增加 run registry、跨流 watermark、关闭墓碑、协议字段或 durable 记录。额外成本仅为每个有 TODO 通知的批次一次既有内存 owner 快照读取，不随文本 delta 逐条请求。
- 回归覆盖同 run 版本回退、CLEARED 后旧 ACTIVE、新 ROOT 后旧 run、已关闭 run 缺席、初始化 retained 通知，以及单批多通知合并和 interaction cursor 不跳过。
