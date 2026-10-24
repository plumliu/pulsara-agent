# 对 interop_tasks_r3 真实 owner 组合的只读交叉审查

本审查读取 `output/windows-g0/interop_tasks_r3/round3/report.zh.md`、`guest_probe.py`、`task_probe.py`、`verify_results.py`、最终三份matrix，以及实际生产`terminal_process/manager.py`的spawn/watcher/join落点。未重跑其探针、未改其文件或生产源码。

核心结论有实际证据支持：原baseline在无pipe普通Windows child仍live时发布PHYSICALLY_JOINED/completion并释放容量；这不是仅凭carrier结束推断。patched PIPE/PTY在Linux root退出、reader结束时仍TERMINALIZING、watcher存活、completion=false、liveCount=1，原8槽计算接受7条新任务并阻第8条。normal最终brokerTerminatedJob=false，所持精确HANDLE均signaled后才发布success；cancel对应TerminateJob路径和killed。报告没有把研究清理当正常完成。

共享A/B确实使用同一个Linux manager/registry：最终shared-ab记录两者managerPid782，registryOwner均为`terminal-owner:3knyuvtvazwrqnbxkjl37jyt7q`。A取消后B仍completion=false、TERMINALIZING、liveCount=1；B child精确creation对应HANDLE wait258，随后B正常success且brokerTerminatedJob=false。这个证据比两个独立manager更强，仍只覆盖该受测调度。

必须保留报告当前限制：真实生产owner没有修改，成功的是output脚本对真实实例的内存monkeypatch。它用command匹配/state局部map、output文件RPC/polling和研究ctypes，不是已存在的生产API。每次scope seal/正常join的取消竞态、首次绑定/半请求、MCP/hook与GUI关闭均未因这14项断言自动闭合。

复核了scope安装与线程启动顺序：`_spawn`只构建Popen/Thread并返回state；真正`Thread.start`位于独立`_start_physical_threads`，由随后`_install_spawned_process`调用。研究wrapper在`original_spawn`返回后、将state交回install之前替换reader.join，因此早于watcher启动；不能据此声称存在“watcher先于scope安装”的窗口。初版审查误把相邻方法当作`_spawn`内部，已按实际方法边界撤回此项。launch rollback/Thread.start故障及其他取消交错仍只是未测范围，没有由此得到正证。

在上述范围内未发现推翻“必要接缝已获得有限真实owner正证”的新增事实；原生产baseline反例和整个G0-C/Phase B未通过仍成立。
