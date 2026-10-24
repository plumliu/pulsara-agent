# 第二轮 G0-W 审阅边界

用户授权：继续 gpt-6.1-sol / high 并行研究和实验，优先 Windows 子进程取消/回收，补 PG helper 异常死亡；保持 interop，禁止进入 Phase B。受测 Git：fb2c57da12d24ddda32da1320db1d7c645b5ebb6。

## 需要区分的事实

- Linux `terminal_process/manager.py::_join_physical` 当前依赖 process.wait、reader/watcher/timer join 与 PGID 消失。不能从这些事实推导任意 interop Windows 后代消失。将 Windows 任务登记到明确范围并得到该范围结算，才可能补足这一个边界。
- Windows Job 可否杀死已被正确纳入的普通后代，与 WSL 是否会在子进程开始工作前将其纳入该 Job，是两个问题。启动后扫描 PID/PPID、只看当前 Job ActiveProcesses 或给 cooperative fixture 自行加 Job，都不能证明未受控的普通 WSL interop 已被覆盖。
- 每个应用一个 Job 不能单独满足某个 terminal 的取消；某个任务取消不得回收另一任务或用户工作。显式系统打开继续其原 OS 交接语义，不能误扩为所有外部浏览器/编辑器都归应用所有。
- 宿主/载体死亡、Linux proxy 结束、Windows PE 根先退、孙代继续且不持 pipe、孙代继续并持 pipe，应分别观察。普通子代反例与显式 breakaway/setsid/其他服务代为启动的外部范围要区分。
- PG helper 复用 kernel.lock 的目的，是让物理操作 settle/readback 之前另一维护操作无法进入；只保证 helper 自身存活时持锁不够。status3 是一个时点的事实，不证明旧 pg_ctl 稍后不会 start。
- 将锁 fd 继承给常驻 postmaster 可能挡住新维护，也可能永远挡住正常 kernel；把锁放进另一个有限 supervisor 也必须检查 supervisor 自己死亡。不能为一个测试绿而引入第二生命周期 owner、持久 receipt 或重放。
- 故障 hook 只应提供可复现调度窗口；需记录注入位置、是否改变真实子命令输出/信号/继承，以及没有该注入的正控。未执行的场景保持待验，不把 probe 自检通过等同产品机制通过。

## 交付要求

保留命令、退出码、真实 PID/创建身份、进程与锁/pipe 观察、原始失败、仅自有资源清理。活动 spec 只吸收实测结论与待验候选，不提前将未闭合候选冻结为产品实现。生产代码、用户配置与共享发行版生命周期均不改变。
