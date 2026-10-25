# alpha.10 正式 GUI 活动任务睡眠见证

本目录尚待实际睡眠结果，不能因准备就绪判通过。

经正式安装 exe 和 GUI 新建快速开始会话 `session:qha3722ihbndoc7c7jhzwxaa3e`，使用用户已保存连接，发送一次明确消息启动原 terminal PIPE 任务：printf 标记后执行本机测试 PE 的 echo 模式。该模式阻塞读取 stdin 直至 EOF；没有任务寿命超时、业务写入、monitor 或旧任务重放。真实首轮已 COMPLETED，精确 process 为 `process:sz57qhtih4bhpcmwx3rggesfee`，Windows child PID14916/creation134374126291452207，由独立原生 HANDLE 只读观察者持有至物理退出。观察者永不发信号，其自身 3 秒有限子进程自检已通过。

本次公开入口与 terminal agent 的测试注入 Application/main 关闭实验分开：这里由真实 GUI→保存模型→原 terminal 启动；没有改生产或注入 owner。

用户当前出差，仅笔记本内屏，可现场睡眠。先等三个故障实验到资源安全边界；再次核任务实际存活、当前有效 writer、同 kernel/PG、HTTPS 精确证书与 TUN 后，再明确通知用户手动睡眠约 60 秒并唤醒解锁。不会自动睡眠或关闭 TUN/interop，不将仅等待 60 秒当睡眠。

若睡眠使 writer 过期，预期原 Host 封准入并沿原 close 回收其活动进程，旧 Core 完整关闭后 cold 恢复新 writer；原 process-local 任务不复活。观察 exact HANDLE 的信号、scope/relay/anchor 退休、GUI 草稿及数据库唯一消费，不以健康检查 200 单独判通过。若 writer 仍有效且原任务继续，则只据实际结果记录，不要求为了迎合预期强杀旧进程。

随后通过 GUI 仅发送已留的新草稿，要求回复固定标记且不调用工具；检查第二个 COMPLETED turn、两次唯一消费且未新启动任务，再正常 Quit/维护检查。发现失败先保留现场，不外部清理后伪称正常通过。

observe.py/guest_observe.py 是原 alpha.9 已验只读快照的路径/版本适配；native_observe.py 和 https_observe.cjs 保留原只读底层。Windows 父观察进程使用独立 uv 管理 Python，不锁仓库 .venv；未调用 uv 更新依赖。
