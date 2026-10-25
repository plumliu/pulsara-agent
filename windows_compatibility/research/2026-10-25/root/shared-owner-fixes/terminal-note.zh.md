# Terminal：跨域接入与共享缺陷的边界

2026-10-25，只读审阅当前9e1150b7、AGENTS/spec和真实manager/调用测试，未改生产。独立Python内存探针及结果见probe.py/observed.json；探针没有实际child，唯一泄漏的PTY master由探针精确关闭，没有终止共享Ubuntu或改interop/TUN。

**原Linux PGID不足以证明Windows普通后代存活、取消或join的原问题，不能在“完整跨域修复且完全不做B接入”两者同时成立下解决。** 当前_ProcessState只持Popen/PGID、reader/watcher、PIPE/PTY；Windows无pipe后代可以在这些全部结束后继续运行，manager没有Windows对象身份或范围可等。R4研究的live/terminate/join/rollback接缝依赖真实Job、SCM绑定/relay/准入/退休，缺了物理adapter只改_status/延迟completion没有可等待的事实。加未使用scope Protocol或空抽象不构成修复；把所有PTY/reader活着都当Windows后代也不能覆盖无pipe后代。

G0证明可行，生产装配Job/interop scope及实际owner接缝正是spec §5/§11.2的B部分。可把这一个生命周期slice单独授权实施，而不顺带做完整Windows GUI/installer；仍须准确称“B的跨域生命周期部分”，不能用命名绕过范围冲突。当前等待用户选择，不实施。

另有两项可独立修复的共享POSIX问题，与原Windows问题分开：

1. **foreground watchdog Timer.start预约泄漏**：exec_with_yield先增加launch count、发布decision，再在既有spawn try之前启动Timer；实际Thread.start失败后没有进入原rollback。探针结果launching1/decision1/states0，容量1时下一次启动被拒。最小补丁仅在该Timer创建/start异常时调用现有_release_launch_locked/_settle_foreground_decision_locked（或将其纳入同一已有try/except），清本次精确预约/decision，无新接口。现测试只覆盖_spawn失败和post-state lifetime timer失败，没有覆盖这个更早的foreground timer。
2. **PTY Popen失败的master fd泄漏**：openpty成功后Popen以实际不存在executable抛FileNotFoundError，finally只关slave；master仍open。最小补丁在这个Popen异常分支关闭刚取得的master，保留成功路径供原reader/rollback拥有。现实触发包括shell executable或cwd在spawn时不可用；不是Windows专属。后续fdopen/Thread构造的静态异常范围未在本次实验扩展，不虚报已验证全部early-spawn问题。

现有post-state reader/watcher/timer/subscription rollback、launch close/decision abort、group/drain/join和8槽并非一概缺失，test_round2_terminal_output.py现28passed4.62s。不能为补这两项共享缺陷再造调度/完成owner，也不能把它们的修复写成Windows后代跨域回收已正式完成。root明确目前不授权这两个新发现的生产改动，保留小型复现供单独决定。
