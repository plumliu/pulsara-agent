# 活动spec/README/checklist未提交diff只读审阅

已读当前三文档diff、实际runtime.stop/main.requestQuit、hooks executor，以及tasks补充distro/signal/GUI/sleep报告；未编辑活动文档。

G0机制可行性与B正式NSIS/setup/生产GUI/激活的新增边界准确，避免先实现B才准验G0的循环。研究接缝仍逐项保留，MCP call/session与hooks/environment不同owner未被terminal槽位语义替代；生产未改与Windows未激活的声明准确。Session退休组合与实际选择源码一致，也明确不能把研究对象列表变通用registry。原生Ubuntu延期而非WSL替代，正确。

最小需精确化的两处已发root：

1. §9.2“WSL向kernel发送SIGHUP”超出信号sender证据。tasks实际记录kernel收到signal1，报告明确无siginfo sender PID；可改为“真实宿主强退后kernel实际收到SIGHUP”，保留最小启动handler使原EOF/aclose进入门的两条正证，不推定所有signal或故障。
2. §10.2“确认closeAllConnections不能单独结算全部尚未收headers的请求”宜限定为本轮壳的三条pending未在观察截止结算，不能据该Promise当全部owner已join。从这一已测反例推出必须显式结算自己持有的request/download合理；不从一次窗口推成Electron API普遍失效断言。

§21/README/checklist的最新distro/SIGHUP/GUI/sleep/N补充尚未汇入是当前进度，不重复列作新产品缺口。最终门表应分别记自然有效期新上下文正测、用户睡眠88.493秒系统事件、SSE一次只读重连、两个独立HTTP与真实kernel/PG owner而非fullmain组合。不得将manual宿主的app.exit0代码分支当未捕获的Windows OS历史exitcode。
