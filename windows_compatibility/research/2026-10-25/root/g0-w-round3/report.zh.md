# G0-W 第三轮：PG准入替代与Windows组合回收

2026-10-25。用户授权继续由三位 `gpt-6.1-sol high` subagents 并行研究，沿用 Ubuntu/plumwsl、保持 interop，先不进入 Phase B。受测基线为 Windows/Linux 两份 clone 的 `671f8fd21da5ac4bdc4190988d2c294395faa036`，包含 `3ac7fc2b`。本轮只归档研究证据、修订活动规范；生产源码、依赖和用户配置未改。

**找到了有真实正负闭环支持的PG维护准入替代候选；Windows首次绑定、组合取消和真实terminal集成也取得局部正证。完整G0-W-C/P仍未通过，Phase B未进入。** Phase A的Mac证据保持，原生Ubuntu x64/arm64及GUI资格仍须独立验收。

## 本轮结果

| 专题 | 实际结果 | 可得结论 |
| --- | --- | --- |
| PG维护准入 | 独立PG EX生命周期锁由真实start链继承；原kernel锁仍可用于正常kernel；helper死后status3但PG锁busy，新start/reset拒绝 | 替代上一轮已否定的单helper持原锁候选；有机制可行性，未完成生产协议合成 |
| 有限PG命令 | 真实stop的exec后执行前/成功exit前停点、initdb及postgres bootstrap停点中杀helper，有限后代仍持准入锁 | 不能只跟踪PG寿命，还须保护可能晚执行的有限mutator |
| 实际reset | 同一guard拒删inflight数据；准确stop后重取两锁，实际删除PGDATA并保留锁inode/socket/外部sentinel，真实initdb/start/query成功 | 删除准入来自本次持锁和当前事实，不能来自过去wait-owner/status/stop结果 |
| interop首次绑定 | anchor自开O_PATH，经SCM_RIGHTS传fd；取消在pin前、pin后send前、send后bind前及live正控四门 | 标准fd传递和pidfd能避免按陈旧PID路径重开；完整宿主可信绑定尚未合成 |
| 请求阶段取消 | 转发前、1字节部分转发、完整请求已转发三门；seal→Windows Job join→Linux PGID取消/join→路径退休 | 组合顺序有正证；只回收Windows Job仍可能留下Linux调用者，原失败保留 |
| 真实terminal | baseline复现Windows无pipe后代仍活但任务已完成/槽位已释放；内存组合14项通过 | 原owner需要跨域物理scope接缝，正确路由本身不够；生产路径仍未修复 |

## PG候选的边界

详见[PG报告](../../backend/pg-admission-round3/report.zh.md)及其逐命令结果。最终候选使用两个不同生命周期的OS锁：原 `kernel.lock` 串行kernel/维护；PGDATA外 `desktop/pg-lifetime.lock` 的EX由start链一直传给PG。PG锁不写内容，不成为receipt、registry、实例身份或第二份startedHere。helper只close自己的副本，不对继承的同一open file description执行LOCK_UN。这个语义来自[Linux flock定义](https://man7.org/linux/man-pages/man2/flock.2.html)；实际PG继承链与有限stop读取PID的顺序已对照[PG17.9 pg_ctl源码](https://raw.githubusercontent.com/postgres/postgres/REL_17_9/src/bin/pg_ctl/pg_ctl.c)并在本机注入真实停点验证。

最终EX主矩阵7项，增加路径/owner检查后的start复验3项，stop2项，initdb2项以及真实reset闭环均取得证据。历史SH7项只作为比较，不保留为并行候选。stop有限child继承原kernel锁，initdb有限链继承两锁；因此即使PG已停且PG锁空，尚未退出的旧stop仍阻止新维护。reset必须在自己的调用内重新取得当前两锁、重新确认已停，锁保持至删除结束，普通删除不能替换锁inode。

两个具体限制保留：删除正控先在独立调用中准确stop，再由guard重取锁删除；guard内部running→stop→delete分支尚未单独做删除正控。start探针最后的只读status readback未继承kernel fd，不能把目标表中所有status继承规则都说成已覆盖。它不具备晚写入能力，不推翻本轮mutator准入结果。完整ready/operation/EOF/取消/失联、真实TS owner和全部PG功能矩阵仍待验；不把外部手动绕过协议的PG自动算成受锁保护。

## Windows回收与真实任务

详见[竞态报告](../../terminal/interop-races-round3/report.zh.md)和[真实任务报告](../../terminal/interop-tasks-round3/report.zh.md)。沿用上一轮无breakaway Job、CREATE_SUSPENDED→Assign→Resume、私有0700 ext4 socket及透明字节转发。WSL继续拥有原PE创建与stdio/control；探针没有复制WSL协议parser。

首次绑定候选让anchor在自己的生命周期内打开上游fd，以标准SCM_RIGHTS转交；receiver核对SO_PEERCRED、pidfd及本次可信anchor记录，bind时检查存活。实际Unix通道只观察到请求字节，没有可据以判断操作完成的回复；不能虚构reply gate。部分请求和完整转发后的失联均保持unknown，不重发。

`jobcancel-02dfb85d`证明仅seal和Windows Job全成员HANDLE signaled/ActiveProcesses0仍不足：已转发请求的Linux调用者在20秒诊断观察内未退出。`jobcancel-5d292f5f`补测为每次调用建立独立Linux PGID，封新准入且保留socket，Windows精确join后执行本次Linux group取消并等待成员pidfd/组空，然后join调用者和退休路径；三门通过。20秒仅是实验观察边界，不是产品任务寿命cap。全请求已执行PE仍记unknown，物理回收不恢复丢失的操作结果。

真实 `TerminalManager/ProcessRegistry` 的baseline与内存候选分开记录。baseline在无pipe Windows child存活时已PHYSICALLY_JOINED/liveCount0，还允许8个新Linux任务。内存候选把scope正常join和取消接入原reader/watcher/kill_process，保持原唯一完成发布和容量计算：Windows尾部存活时TERMINALIZING/liveCount1，最多再准入7个；自然完成不调用TerminateJob。PIPE/PTY、Unicode输入、PIPE EOF/PTY EOT和同一registry同一owner的A取消/B继续共14项断言通过。原terminal相关47项测试通过。

这不是生产实现：command过滤、局部state映射、文件RPC/polling、Python ctypes仅为试验装配。真实任务探针仍使用旧的上游固定路径方法，未合入本轮SCM_RIGHTS首次绑定；relay的sealed检查与await open仍有未合成竞态。launch rollback、Thread.start故障、真实MCP/hooks、环境probe、GUI Quit/宿主崩溃全链和休眠恢复仍未由这组实验覆盖。交叉审阅一度误读_spawn与相邻安装方法，已按真实方法边界撤回“watcher先于scope安装”指控；正确的未测故障范围保留。

## 证据、审阅与清理

- [PG探针和结果](../../backend/pg-admission-round3/)：真实PG17.9/pg_search，25个ext4实验home；最终审计无本次home/可观察当前用户进程残留。研究signal检查不是生产原子身份API，报告明确其限制。
- [竞态探针和结果](../../terminal/interop-races-round3/)：首次绑定、错误路由负控、旧socket退休回退、请求阶段取消、精确Windows HANDLE/Linux pidfd及清理事实。
- [真实任务探针和结果](../../terminal/interop-tasks-round3/)：最终三组matrix、关键manager/PE原记录、原失败、自检、清理；222个Windows和62个Linux历史身份审计无残留/unknown，私有socket目录为空。
- [PG交叉审阅](../../terminal/interop-tasks-round3/pg-cross-review.zh.md)与[terminal交叉审阅](../../backend/pg-admission-round3/cross-review-interop.zh.md)：支持上述有限结论，不升级整门资格。

首次socket过长、ActiveProcesses0早于个别HANDLE signaled、PG恢复后过早query、仅Job取消留下Linuxcaller、错误假设reply gate等失败均保留，不弱化断言或覆盖原证据。归档只选报告、小型源码、结构化结果及必要原始日志，不收数据库、二进制、venv、第三方源码或settings。各目录archive-files.txt是选择清单，不是产品registry。历史中间重复留在ignored output；归档不保证每条历史命令原地可重跑，复现需遵循报告路径并提供前轮小探针依赖。

root终审按实际源码/记录核对，PG与Windows专题agent只读终审了活动规范和本汇总；修正了旧句把“任意锁释放”误当新方案失败的歧义。归档144个所选文件约2.3MB，其中27份Python仅格式化/必要lint注释并核对规范化语法树一致，其余文件逐字节一致，见[归档检查](archive-check.json)。root独立检查最终PG计数/失败、reset正负记录、绑定/组合取消、terminal14项、清理空集及43个文档链接，见[记录复核](verification.json)；仓库Ruff与Git空白检查通过。这些是归档/记录核对，不增加运行实验数量或提升G0资格。

后续G0优先顺序：将首次绑定/封准入/组合取消合进同一真实terminal研究探针，补启动失败与取消交错；将PG两锁接入真实TS owner的独立研究调用并补EOF/失联及guard内停止删除分支；然后补MCP/hooks和剩余各门。生产Phase B继续保持未开始。
