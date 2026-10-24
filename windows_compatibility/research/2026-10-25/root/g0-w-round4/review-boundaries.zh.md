# 第四轮 G0 组合验证的审阅边界

基线adb3e1de，两clone一致；继续既有三位gpt-6.1-sol high agent，不新建。用户明确保持interop、先不进入Phase B。所有运行适配只在output；生产owner通过实际注入点或解释器内装配参与，不能复制其状态机冒充集成。

## Terminal

- SCM_RIGHTS绑定、seal、request pump和真实manager必须在同一次运行链路中。scope准备若在manager之前完成，不能称为真实launch reservation期间取消。
- 原manager先持foreground decision/launch reservation，_spawn仅创建state；_install_spawned_process随后启动reader/watcher。scope装配必须覆盖真实启动rollback，不只替换reader.join：reader尚未start时原_join_physical不会调用它。
- 每次执行的临时scope及研究文件RPC不构成新的产品registry/receipt。原watcher仍唯一正常完成发布者；8槽/输入/PTY/输出finalize不改语义。
- cancel须封准入，结算已有pending connect/forward和Windows Job、Linux group/reader/watcher；old caller仍可能使用环境时保留socket，最后才能退休。标准asyncio任务取消不是WSL操作未发生的证明。
- 半/全请求失联保持unknown，不重发。正常退出不调用TerminateJob。必须有真实PE及其精确HANDLE事实，不把Job空或LinuxPGID消失各自当完整join。
- 不扩大成MCP/hooks/GUI宿主全部已验；少量Thread.start/launch rollback门应直接调用原owner失败路径，不重写结果。

## PostgreSQL

- import真实BundledPostgres，通过实际PostgresOperations注入Windows Node→WSL三pipe有限helper。原owner保留startedHere；不copy一套owner，不在adapter私藏第二份生命周期状态。
- helper各次当前取kernel锁；start/初始化取独立PGEX；永久PG仅继承独立PG，有限mutator继承所需准入锁。removeDataDirectory必须本次重验且持双锁，不能依据前一method停库/等待结果删除。
- ready前EOF、ready后operation前EOF、发operation后EOF分别保留真实childexit/锁/副作用事实。Node取消在写操作帧之前必须真正结束helper；写出后只报当前证据，不自动重发。
- 真实carrier/helper/Node死亡不是互相替代；若只测一层须明确。锁释放不意味着操作成功，不能status3即删。
- postgres.ts start的catch会status/readInstance并可能startedHere=true，再抛原error；main.ts Quit按该字段决定stop。失联后真实行为要记录并与当前规范逐句核对，不能预设该字段正确或错误、不能用adapter伪造status压住读回。
- resetConfirmed真实running→stop→remove分支需要正控；data/socket/外部sentinel/锁inode仍核对。研究home在ext4，只删确切本次PGDATA，用户settings不读写。

root负责集成边界交叉审阅、选择证据归档、活动规范消除过时结论及最终Windows/Linux Git同步。原失败保留；不因实验超时加产品寿命cap，不改interop、不结束共享WSL。当前主机原生Ubuntu资格仍独立待验。
