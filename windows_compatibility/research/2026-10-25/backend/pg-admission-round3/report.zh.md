# 第三轮 PG 维护准入研究：独立生命周期锁候选

本轮找到并在真实 PG17.9 上验证了一个最小替代候选：**原 `kernel.lock` 继续保护 kernel 与有限维护；新增 PGDATA 外的 `desktop/pg-lifetime.lock`，由 start 链以 EX flock 继承给常驻 PG；不会产生常驻 PG 的有限命令继承原 kernel 锁。** 真实旧 start 在 helper 死亡后晚启动仍存在，但此时 PG 生命周期锁 busy，不能准入新 start 或删除。运行 PG 不再阻断原 kernel 锁。

同一个研究 guard 已实际验证“status3 + PG 锁 busy 时保留数据”与“准确停止后双锁内删除 PGDATA、保留锁 inode/socket/外部 sentinel、真实重建启动查询”的正负闭环。本结论仅是所测 Linux/WSL 机制候选的正证；**完整 G0-W-P、G0-W、原生 Ubuntu/GUI 资格与 Phase B 均未获批准**。生产源码与规范未由本 agent 修改；规范采纳由 root 决定。

## 依据与所有权

已读取根 AGENTS、现行 Windows spec §3.3/18、G0 checklist §7.4、第二轮 pg-death-round2 报告和源码、实际 `desktop/src/postgres{,-posix,-operations}.ts`、`src/pulsara_agent/desktop_runtime.py::acquire_kernel_lock`。

实际依赖源码来自 Linux clone 的 `desktop/build/postgres/postgresql-17.9`：

- `src/bin/pg_ctl/pg_ctl.c::start_postmaster`：fork → child setsid → shell exec postgres；常驻 PG 会继承显式传入且未 CLOEXEC 的额外 fd。第二轮已经实证了此点，本轮沿用并改为独立锁。
- `pg_ctl.c::do_stop`：先 `get_pgpid(false)`，再对已读 PID 发信号，再等停止。故停库 helper 死于 pg_ctl 尚未读 PID 文件时，仅观察旧 PG 生命周期结束不足以约束仍可能晚执行的 stop。
- `src/bin/initdb/initdb.c::popen_check` 和 bootstrap 入口：initdb 用实际 popen 启动真实 `postgres --boot`，PGDATA 通过环境传给后端。本轮暂停了这个真实后端，核其继承锁。

Linux `flock` 与 fd 继承负责物理存活准入；PostgreSQL 保留恢复、PID 文件、实例启动和停止；TS `BundledPostgres` 保留产品生命周期、既有 `startedHere`、用户确认和错误分类。探针不记录第二份 startedHere，不读写 settings，不添加 registry/receipt/replay/持久事件，不以 lock 文件存在或内容为 authority。

## 最终候选的锁与 action 表

以下为待规范采纳的单一路径建议，不是本报告擅自批准的生产合同。

| action | 原 kernel.lock | 独立 pg-lifetime.lock | 实际子进程继承 | 必要结果 |
| --- | --- | --- | --- | --- |
| 原 kernel 正常运行 | EX，沿原 owner | 不取 | 沿原合同 | 运行 PG 不阻 kernel |
| 新 start | helper 从 ready 到本次结算持 EX | fork 前非阻塞取 EX | pg_ctl → shell → postgres 只继承 PG 锁 | PG 锁 busy 不提交新 start；status3 时为 inflight/unknown，不排队/重发 |
| 复用已运行实例 | 当前调用取 kernel EX | 允许观察 busy，不强取/解锁 | 只读有限命令继承 kernel | 当前 status/精确 data/socket/readiness 事实决定复用；busy 本身不等于实例正确 |
| stop/status 等有限命令 | 当前调用取 EX | stop 不要求取得运行 PG 已持的 EX | 真实有限 pg_ctl 继承 kernel | helper 死后 pg_ctl 未结算仍阻新 kernel/start/reset |
| initdb | 当前调用取 EX | 当前调用先取 EX | initdb 及其有限 postgres 后代继承两锁 | helper 死后有限 mutator 仍有锁，不出现晚初始化空窗 |
| reset | 当前调用取 EX | 准确实例必要时 stop 后，非阻塞取 EX；一直持至删除结束 | stop/status 继承 kernel；删除在当前 helper 内完成 | 每次新调用重新取锁/核当前事实；只有当前 PG 锁 EX + 当前 status3 才能删 |

独立锁位于 PGDATA 外，普通 reset 不 unlink/替换它。最终探针用 O_NOFOLLOW，fstat 检查 regular、当前 UID、0600、nlink1；home/desktop 沿现 owner 的真实路径和0700检查。所有锁按固定顺序 kernel → PG 取得。**继承的是同一 open file description，helper 仅 close 自己的副本，不调用 `LOCK_UN`**；否则会连带释放后代仍应持有的 flock。

不采用 SH 的原因是简化新启动准入：SH 允许其他 SH 并行，仍需另设 EX admission/降锁步骤。先行 SH 实验验证了分离效果，最终 EX 全寿命方案直接拒绝第二条启动链，省掉此转换。锁只在进程/OS 内存中有效；空文件不表达操作完成、实例身份或重放权限。

表中有限status继承kernel是待采纳的统一目标。实际探针中 `guarded-reset.py::command` 和 `finite.py` 的有限命令确实继承kernel；`helper.py` 在start后的只读status readback没有传kernel fd，`control.py`的独立只读status也没有统一继承。此差异不产生迟到mutator，但不能声称本探针所有status子进程都已实现表中目标。

## 环境与 fault 方法

- Windows 仓库 `C:\python_workspace\pulsara-agent`；Linux clone `/home/plumwsl/workspaces/pulsara_agent`，本轮受测 HEAD `671f8fd21da5ac4bdc4190988d2c294395faa036`。
- 所有 guest 调用显式 `wsl -d Ubuntu -u plumwsl --cd /home/plumwsl/workspaces/pulsara_agent --exec ...`，UID1000；Linux `.venv/bin/python` 3.12.15，Windows `.venv/Scripts/python.exe` 编排。
- PG 固定为 clone 中 `desktop/resources/postgres/bin/{initdb,pg_ctl,postgres,psql}`，PG17.9，真实 preload `pg_search`；未读生产 settings/credentials，未运行 provider。
- 内核 `6.18.33.2-microsoft-standard-WSL2`，`/home/plumwsl` 为 `/dev/sdd` ext4，device2096。全部25个 home 为 `/home/plumwsl/.g0p3-*`，真实0700专用目录。socket地址逐次断言 `<104` 字节，没有用 tmpfs 冒充 ext4。
- fault.c 沿第二轮真实 hook：只在指定程序/阶段写私有 gate、SIGSTOP，由 Windows 观察者杀精确 helper 后才 SIGCONT。真实 fork/setsid/exit 转发给 libc；不替换 PG 结果、不改 PID 文件、不伪造 readiness。initdb bootstrap 使用 PGDATA + `--boot` 双条件匹配，避免误拦版本检查。
- 正控 `plain` 不注入 LD_PRELOAD、不加载 fault module；其余未暂停正控加载同一 fault module。fault 只用于确定调度顺序，不证明自然竞态频率。

`provenance.log` 保留实际版本、存储与只读 wsl.conf。interop/Windows PATH/automount 保持；未提权、未终止或更改共享发行版。

## 实际结果

原始逐命令/结果/时间及 `/proc` exe/cmdline/status/fd/stat 保存在各 results JSON 的 records 中；`archive-files.txt` 选择每轮一份原始结果，避免重复复制同内容 log。

### 启动链

先行 SH7项与随后 EX7项均无 harness failure。EX 主矩阵见 `ex-matrix.results.json`：plain、unpaused、pg_ctl fork 前、fork child 已 setsid、真实 postgres exec 后、postmaster readiness 前、pg_ctl 已 ready 但 exit 前。

| 场景 | helper 死后 kernel | PG 锁 EX | PG 状态与后续事实 |
| --- | --- | --- | --- |
| plain/unpaused 正常结束 | 可取 | busy | 真实查询成功；正常 stop 后 PG EX可取 |
| pg_ctl fork 前 | 可取 | busy | status3；恢复后该 pg_ctl 受 pending SIGHUP 结束，没有晚启动；结束后 PG EX可取 |
| child 已 setsid / postgres exec 后 | 可取 | busy | 暂停时status3；恢复后真实晚启动/query成功；status3不能删 |
| readiness 前 / ready但pg_ctl未退出 | 可取 | busy | status0可能尚不可查询；恢复后真实query成功；stop后PG EX可取 |

增加 O_NOFOLLOW/owner检查与明确竞争 start 拒绝后，轻量复验 plain、setsid、postgres-exec 三项，见最终 `results.json`。后两例中，新 helper 取得 kernel 后因 PG EX busy 返回74、`lifetime-busy`，没有 ready/operation admission、没有 spawn 第二条 PG 链。维护检查同时记录 `kernel_acquired=true, lifetime_acquired=false, status.exit=3, delete_admitted=false, data_preserved=true`。

最终 setsid 例：home `.g0p3-mj6rh0cv`，真实 child3201；postgres-exec 例：home `.g0p3-x1_7spn3`，真实 postgres3315。两例同 case 的 PG lock inode146673 从busy到stop后可取保持不变；不同case inode复用不作为身份 authority。

### stop 与 initdb 的有限后代

`stop-results.json` 两项无失败：

1. `pgctl-stop-exec`：真实 pg_ctl 已 exec、main/get_pgpid 前暂停。杀 helper 后，pg_ctl 的 kernel fd 仍在，kernel EX busy。恢复后此 pg_ctl 受 pending SIGHUP 退出，原 PG 保持运行；随后经精确实例核对清理。没有声称本例 stop 已生效。
2. `pgctl-after-ready`：真实 stop 已完成但 exit 前暂停。杀 helper 后 **status3、PG EX已可取，但 kernel EX仍busy**；恢复真实 pg_ctl 结束后 kernel EX才可取。这关闭了“PG已停但旧有限 stop 尚未结束”的准入空窗。

`initdb-results.json` 两项无失败：真实 initdb exec 后、真实 `postgres --boot` exec 后分别暂停。最终 finite.py 将 kernel+PG EX同时传给 initdb；杀 helper 后两把锁都busy，真实后代退出后才释放。bootstrap argv不带home，观察者按本次私有 G0_HOOK_HOME/PGDATA 关联，不靠无关PID猜测。此前kernel-only两项保留作历史补充，最终报告的双锁事实来自最终两项。

### 同一 guarded reset 的拒绝、实际删除与重建

`reset-results.json` 无失败，home `/home/plumwsl/.g0p3-epv1h0oc`，真实 postgres625在 exec 后暂停。

1. 杀启动 helper 后，同一 `guarded-reset.py` 当前取 kernel，真实status3；PG EX不可取，返回74 `busy-inflight`，`deleted=false`。随后重新读取 data sentinel=`data-sentinel`、外部sentinel=`outside-sentinel`，socket保留。
2. 恢复旧 postgres，真实 psql 返回准确 data/socket 和启动时间 `2026-10-25 00:17:28.209207+08`。按准确data/socket核对后真实stop成功。
3. 新一次同guard重新取kernel和PG EX，重新status3，双锁持续期间实际 `shutil.rmtree(PGDATA)`。只删PGDATA；`kernel_held/lifetime_held/deleted/same_lock_inode/socket_preserved/sentinel_preserved`均true，lock inode146677。
4. 删除后真实status4（目录不存在，区别于status3）。真实initdb继承两锁重建；新start仍只继承PG锁，真实query成功，新启动时间 `2026-10-25 00:17:29.826172+08`。外部sentinel/socket及PG锁inode146677仍在，原data sentinel已消失。
5. 最后精确stop、检查status3与无本次进程后清理本次home。

本正控明确先在独立调用中停库，再由guard重取当前事实完成删除。guard内“发现running→核准确实例→stop子继承kernel→PG EX→删”的分支已有源码，尚未单独做此分支的删除正控；不能把它描述成已覆盖所有reset分支。

## 原失败、范围与剩余工作

唯一 harness 失败在 `reset-first.reset-results.json`，对应原 `reset-first.reset-harness.py`。负控已正确拒删，但恢复旧PG后马上query，服务器尚未ready，真实psql返回socket不存在；断言失败。失败后的精确清理成功，原结果保留。修正为**有限诊断调用中只读观察readiness**，不重发start/reset/initdb/stop；最终闭环通过。其15秒是诊断观察超时，不是产品寿命上限。

SH与EX主矩阵、最终start3、stop2、initdb2均无harness失败。没有通过修改PG错误、弱化断言、跳过分支或重写旧证据取得结果。Python源仅作常规格式化，C hook保留第二轮风格以对应实际fault机制。

此机制只覆盖遵守同一锁协议的 Pulsara 启动/维护路径。外部用户手动 `pg_ctl/postgres` 绕过锁、不合作的同UID进程替换锁文件、未来PG显式关闭继承fd/exec不同后端路径，不在本轮保证内。生产采用时必须让所有相应入口硬切同一协议；PG升级需复验真实继承。不应偷偷把未持锁的外部运行实例标成已经受生命周期锁保护。

独立PG锁busy不提供实例身份、readiness或成功语义；原错误/截断/EOF/失联仍为unknown，当前锁重新可取不重建过去的操作结果。不新增后台轮询、自动重放或队列。普通EOF/取消、Windows carrier失联、完整JSONL边界、真实TS owner合成和全部G0-W-P功能矩阵仍须原规范要求的验证；本探针不替代它们。

交叉审查另指出两项研究工具限制：`control.py` 的signal/resume先核cmdline/私有环境再os.kill，没有pidfd或创建时间的原子身份保护；清理扫描会跳过PermissionError，只证明本次可观察的当前用户进程范围，不证明跨权限全系统无残留。暂停门维持了所测目标的实际调度，但不能把这套研究清理逻辑宣称为生产原子进程身份机制。guard初始status0分支可能先stop再遇PG锁busy，因此`busy-inflight`只保证没有删除，不能概括为整个调用没有副作用。

## 运行命令与清理

编译（显式Ubuntu/plumwsl、Linux clone cwd）：

```powershell
wsl -d Ubuntu -u plumwsl --cd /home/plumwsl/workspaces/pulsara_agent --exec cc -shared -fPIC -I desktop/resources/postgres/include/server -I desktop/resources/postgres/include -o output/windows-g0/pg_admission_r3/round3/fault.so output/windows-g0/pg_admission_r3/round3/fault.c -ldl
```

Windows编排入口：

```powershell
.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/round3/harness.py --final
.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/round3/stop-harness.py
.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/round3/initdb-harness.py
.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/round3/reset-harness.py
wsl -d Ubuntu -u plumwsl --cd /home/plumwsl/workspaces/pulsara_agent --exec /home/plumwsl/workspaces/pulsara_agent/.venv/bin/python /home/plumwsl/workspaces/pulsara_agent/output/windows-g0/pg_admission_r3/round3/audit.py
```

源码须先从本目录传入Linux clone同名目录；未跨系统复制venv/deps/二进制。初始SH、EX全矩阵及原失败均有独立原始结果；不要用最终helper重跑历史SH而冒称原行为。

归档中的`sh-first.*`、`ex-matrix.*`和`reset-first.*`源快照对应各自实验阶段；`fault.executed.c`保存加入stop/initdb门后的实际紧凑版本。最终普通格式Python文件包含后续补充，不能直接声称可逐字重放全部历史阶段；每个历史结果都保留实际命令/输出，重复log不进入清单。早期kernel-only initdb仅保留历史结果，最终双锁资格以最终源码与`initdb-results.json`为准。

每case只恢复本次gate对应且已核身份的进程，真实stop准确data/socket实例。最终 `audit-input.json` 汇总全部25个精确home；`cleanup-audit.json` 为 `passed=true, remaining_homes=[], owned_processes=[]`。审计同时检查 argv 与私有G0_HOOK_HOME，覆盖无data argv的bootstrap后代。未终止共享distro，未对无关PG发信号，未读取或修改saved settings。
