# 第三轮 G0 审阅边界

日期2026-10-25；本轮起点671f8fd21da5ac4bdc4190988d2c294395faa036。继续使用已有Ubuntu/plumwsl并保持interop，禁止Phase B。所有执行原型仅在output中；生产owner不改，活动规范后续只按实证更新。

## PG候选的最小必要边界

上一轮已经证明：helper独占kernel.lock且CLOEXEC，不能阻止helper死亡后的旧PG晚启动；把同一锁传给PG又阻断正常kernel。

新候选把物理生命周期分开：原kernel.lock维持内核/维护串行，另一个PGDATA外的空锁文件以OS flock拥有启动链/PG寿命，不存receipt或第二startedHere。初始SH只是研究比较，优先全寿命EX避免不必要的降级/再升级。

| 动作 | 待证的锁/调用规则 |
| --- | --- |
| start | 先原kernel锁，再PG生命周期EX非阻塞；只有取得后才接纳start；启动链继承PG锁、不能继承kernel锁 |
| 正常kernel | 仍只取原kernel锁；常驻PG持独立锁不能挡住正常kernel |
| PG锁busy | 不因status3认为无inflight；只能核实当前已运行实例的复用/停止，其他情况busy/unknown，不能排队未来start/reset |
| stop/status/initdb等有限子命令 | helper死亡后真实子命令也必须继续占住维护准入；可继承原kernel锁，直到其有限后代全部结束，不创建第二PG owner |
| reset/delete | 先kernel锁，必要时验证/停止准确PG；再实际取得PG生命周期EX，保持锁至删除结束；stop历史结果和wait-owner不作授权 |
| 锁文件 | PGDATA外的稳定inode，正常reset不unlink；O_NOFOLLOW/fstat/uid/mode等边界实际检查；存在与否、文件内容不作状态authority |

注意`pg_ctl stop`在读取pidfile前死亡/暂停的窗口：旧stop可能在新start之后才读取新pidfile。仅固定PG寿命锁不足以覆盖这个有限操作，需要真实停点证明其仍阻止新维护。PostgreSQL自己的数据锁、WAL和PID仍各自原owner，不把新增flock当任意外部程序遵守的强制访问控制。

## Windows候选

上轮证明了无breakaway Job与普通后代继承、私有Unix路径保留避免ENOENT fallback；仍需证明首次pin取得的是预期anchor、取消在创建/请求转发各阶段的真实结果和旧调用者未结算前不能退休路径。

- 若在私有socket中转发纯bytes，不能假定该通道必然有reply；WSL的stdin/stdout/stderr/control另走vsock。对没有实际收到完成的创建保留unknown，不重复发送。
- 首次对象绑定优先从anchor自己的生命周期内打开socket，用标准Linux SCM_RIGHTS转交已打开fd，而不是在其他进程反复按旧PID路径重新打开。任何额外机制先给出真实API与最小缺口，不复制WSL parser。
- 真实terminal baseline与内存prototype分开记录：外层wrapper等Job不能被报告为生产owner正确。原watcher的唯一完成发布、输出finalize、槽位释放必须等组合物理范围；取消必须封新准入再真实收回Windows/Linux范围。
- output-only monkeypatch是机制实验而非生产接口；它可以揭示未来最小接缝，但不能跳过真实权限、启动reservation、reader失败、MCP/hooks、超时/失联及8槽等原合同。

所有有限计时仅测试调度/观察边界，不引入产品总寿命cap。只清理本次精确实例/目录；不改变用户WSL配置或碰保存凭据。新证据归档2026-10-25，旧两轮保持历史原貌。
