# 第四轮：真实 TS PostgreSQL owner 与跨域失联

本轮直接使用当前 `BundledPostgres`，通过现有 `PostgresOperations` 注入 Windows Node→三条 pipe→WSL Linux 双锁 helper，完成真实 start/reuse/stop、原 owner 的 running→stop→delete reset 分支，以及 EOF/失联矩阵。**正常路径接缝可用；原 owner 在未提交和结果不明时仍可能认领其他调用启动的 PG，真实负例已确认。** 输出目录内机械生成的两行 typed guard 候选通过两个针对性对照，原/候选的既有8项测试均通过。

生产源码、接口、依赖、settings 和规范未由本 agent 修改；没有进入 Phase B。`postgresExecution` 只是研究错误合同草案，不是生产已具备的类型。完整 G0-W-C/P 未通过。

## API、边界和精确缺口

受测基线两 clone 均为 `adb3e1dec382b5531eface90b76d4968ec21c0b8`。已读 AGENTS、spec §3.3/19、round3、实际 `desktop/src/postgres.ts`、`postgres-operations.ts`、`postgres-posix.ts` 和 `main.ts` 退出条件。

没有复制 lifecycle 状态机：`owner.cjs` 直接 `require('./compiled/postgres.js')`，该文件由锁定 Windows Node22.23.3 对当前完整 desktop tsconfig 实际编译生成；构造实际 `new BundledPostgres(operations)`，断言其真实 `initialized/startedHere` 字段。研究适配只将现有八种物理接口方法转成一次有限 Linux 调用。PostgreSQL 保留实例与恢复；Linux flock/fd 负责存活准入；TS owner 保留生命周期、调用顺序及唯一 startedHere。

现接口没有跨方法锁 scope、AbortSignal 或 typed unknown。前两项能在研究 leaf 内收敛：每次调用自己取锁、ready 后才收 operation；`removeDataDirectory` 重新检查准确私有data/uid/version、PG生命周期EX及当前status3，持两锁实际删除，不能拿早先stop结果授权。若方法间另有start，删除会busy拒绝。reset 的运行分支继续由原 TS owner 调 `this.stop()`，helper不另造自主reset owner。

**错误类别是实际产品接缝缺口。** 原 `start()` catch 对任意 run error 都调用 status/readInstance；确认准确实例后设 startedHere，再抛原错误。`main.ts` 的 `if (postgres.startedHere || (databaseReady && !startupError)) await postgres.stop()` 因而可能在 startupError 情况下停库。本轮输出的 `mainQuitWouldStop` 是按该条件和受测 startupError 状态计算，不是运行 GUI/Quit 的实证。

## 双锁和三 pipe 的实际适配

Windows Node 使用实际 SystemRoot/System32/wsl.exe 绝对路径、`shell:false`、显式 `-d Ubuntu -u plumwsl --cd ... --exec <Linux .venv python>`、stdin/stdout/stderr 三 pipe。完整回复必须有换行结束的 JSONL result 且 carrier 正常结束；未换行的完整 JSON 对象也不采纳。诊断 progress 帧只记录研究事实，不授权产品操作。

| 调用 | helper 当前持锁 | 实际子命令继承 |
| --- | --- | --- |
| start | kernel EX + PG EX，先取再 ready | 常驻启动链只继承 PG EX |
| initdb | 两锁 | initdb及有限后代继承两锁 |
| status/stop | kernel EX | 真实有限pg_ctl继承kernel |
| 文件观察/布局 | kernel EX | 不产生常驻进程 |
| removeDataDirectory | 两锁、当前再验证status3 | status继承kernel，当前helper持两锁至rmtree结束 |

本轮所有内部 status，包括 start 后的 helper readback，都显式继承 kernel fd，补上第三轮只读readback的限制。PG锁仍在PGDATA外，O_NOFOLLOW、regular/current uid/0600/nlink1检查，不unlink、不LOCK_UN。未知结果不自动重发任何 mutator。

`admissionClosed` 只属于本次 pipe/cancel 准入。ready前EOF或ready后取消先关闭准入、关闭stdin，收到晚ready仍再次检查，不写operation；等实际helper no-operation及正常carrier结束后才拒绝Promise。operation写出后EOF不撤销已接纳的物理调用。

## 原 owner 实际矩阵

最终13个物理case分为 normal1、protocol6、death3、未提交他者对照1、typed候选2。各结果JSON包含完整命令、帧、实际 owner 字段、Linux锁/status/process/fd和Windows观察。`verified-results.json` 对这些事实再做离线断言，不重跑PG。

### 正常 start/reuse/reset/stop

`normal.results.json`，home `/home/plumwsl/.g0p4-o47zi96_`，Node22.23.3。

1. 原owner从无PG_VERSION启动，真实initdb/start/query成功；initialized=true、startedHere=true。
2. 新的原owner对象通过同API复用正在运行的同实例，initialized=false、startedHere=false；没有第二份影子状态。
3. 原owner `resetConfirmed()` 真实走 status0→准确 raw instance→`this.stop()`→`removeDataDirectory`。准确运行实例被停止，双锁内实际删除PGDATA，startedHere=false。PG锁inode145860、socket、外部sentinel保留；真实status4证明data目录已不存在。
4. 原owner重建initdb/start，真实query成功，随后真实stop。原实现stop不清startedHere，因此日志保持其真实值，探针没有改写字段使之“好看”。

这补齐了第三轮尚未实测的 **真实TS owner内部 running→stop→delete** 分支；没有另外在helper中复制这段产品流程。

### EOF、取消和结果丢失

`protocol.results.json` 六项无harness失败：

| case | 实际结果 |
| --- | --- |
| before-ready-eof | 在helper取锁/ready前确定性暂停；关闭stdin后恢复。零operation-written，真实no-operation+exit后拒绝，PG保持status3 |
| ready-eof | 已持两锁/ready但未提交操作；零operation-written，helper无操作退出，startedHere=false |
| ready-cancel | 同上，写点已关闭准入；helper实际退出后才返回PG_NOT_EXECUTED，startedHere=false |
| operation-eof | 写出start帧后立即stdinEOF；真实start/readback成功，原owner成功、startedHere=true |
| reply-loss | 真实start已settled、最终result前暂停；Windows关闭实际stdout读端再恢复。helper完成、PG运行，owner抛PG_OUTCOME_UNKNOWN，但原catch再次核当前实例并startedHere=true |
| unterminated-result | 真实helper发完整result JSON但不带末尾换行。adapter记录partial-final-frame且拒采纳；原owner仍在当前status0/readInstance后startedHere=true，错误保留unknown |

前三例离线再次断言：针对该start call，operation-written=0、没有stdin-error、helper no-operation存在、carrier正常close且sent=false，close发生在owner-result之前。不是仅从最终status3猜测“未执行”。初次fixture的late-ready错误另保留，见下文。

后两例的 `mainQuitWouldStop=true`。未把抛error视作owner自动保持无归属，也没有用adapter屏蔽其真实readback。

### helper、carrier、Node死亡

`death.results.json` 三项无harness失败，均在真实 `postgres` exec后、main前由原fault hook暂停；不是只暂停替身shell。

- helper-death：pidfd核当前helper身份后SIGKILL；独立观察见kernel可取、PG EX busy、真实status3/data保留，然后恢复准确PG。真实PG稍后query成功。原owner的catch在恢复后又核到运行实例，因此unknown和startedHere=true同时成立。
- carrier-death：Node按自己持有的ChildProcess终止精确wsl.exe；独立观察同样见kernel可取、PG锁busy、status3，恢复PG后query成功；原owner结果unknown而startedHere=true。
- node-death：外部Windows编排在同一真实PG门上终止自己创建的Native Node进程。死亡前两锁busy，死亡后kernel可取但PG锁仍busy/status3；恢复PG后真实query成功。Node已死亡，没有伪造owner最终字段或操作成功；观测到的只是物理迟启动与锁保护。

观察者在捕获锁/status事实后即恢复PG，再等待调用结束，没有拿pg_ctl默认readiness超时冒充故障结算。所有最终case中Windows观察者预持的carrier HANDLE最后wait=0；这些HANDLE来自当时观察到的自有spawn PID，不将其夸大为跨权限全系统进程证明。

## 未提交后认领他者：基线负例

`foreign-baseline.results.json`，home `.g0p4-k3h1337n`。本次start已ready但取消，**本次operation-written=0**，helper真实no-operation并正常close。随后在原catch的status读回前，让独立、同一双锁协议的有限helper按owner已经生成的准确start argv启动同一专用PG。独立helper自身真实start/readback完成，才继续原status/readInstance。

原owner没有提交启动，却真实得到：

```json
{"failure":"PG_NOT_EXECUTED","initialized":true,"startedHere":true,"mainQuitWouldStop":true}
```

因此缺口不仅是“自己已经启动但丢回复时Quit是否允许停库”的解释分歧：它会认领另一调用在合法间隙启动的PG。data/socket匹配证明当前实例是准确私有实例，不证明这次调用执行过start。

## 两行 typed guard 研究候选

`generate_candidate.py` 机械读取实际 `desktop/src/postgres.ts`，只在start catch新增 `candidate.diff` 的两行：

```ts
const execution = (error as {postgresExecution?: unknown} | null)?.postgresExecution;
if (execution === 'not-submitted' || execution === 'unknown') throw error;
```

其余owner源码、调用顺序和字段保留。研究adapter根据本次物理协议分类，不根据scenario名字分类：

- 未写operation、完整busy拒绝或早期spawn失败：not-submitted；保原Error/code/cause。
- 已写operation，但没有完整有效result/正常carrier结算：unknown；不恢复操作结果、不重发。
- 完整有效回复携真实PG非零且carrier正常：保原numeric code/输出，不加上述标记，继续原owner的已结算失败readback。

这是错误传递合同草案，不是另一份startedHere、registry或receipt。guard只阻止本次错误触发新的归属更新，不清空调用前已经存在的startedHere。无生产API变更，本轮仅在output生成/编译研究变体。

`candidate.results.json` 只重跑两项：

| case | 原owner基线 | 研究candidate |
| --- | --- | --- |
| 明确未提交、独立helper已启动同PG | PG_NOT_EXECUTED、startedHere=true、Quit条件true | not-submitted错误原样抛，PG仍真实运行query成功，startedHere=false、Quit条件false |
| 真实start已settled后丢result | unknown、startedHere=true、Quit条件true | unknown错误原样抛，PG仍真实运行query成功，startedHere=false、Quit条件false |

候选对照的独立helper在transport拒绝交给owner之前启动，所以即使guard不再调用status，另一个PG也确实已运行；没有因为跳过baseline readback而取消该对照的他者启动。

`transport-spawn-error.json` 另在锁定Node22实际spawn一个不存在的研究路径，不启动WSL/PG。其原ENOENT Error、code、cause和identity均保留，只添加not-submitted。此项补了child.once('error')早期分支；完整前帧busy分类有源码支持，未额外跑busy矩阵。

`tests-original.log`、`tests-candidate.log`：Linux锁定Node22分别运行原 `postgres.test` 和机械候选模块下同一份原测试，均8 passed、无skip/xfail。测试继续覆盖已结算readiness失败会核实PG并保留所有权/原错误。**本轮未另做真实PG非零/延迟readiness专门场景**；该保留语义由源码分支和原回归支持，不能冒称新的真实非零PG实验。

## 原失败、环境与清理

`protocol-first.results.json` 与 `owner.first.cjs` 保留首个before-readyEOF fixture失败。最初adapter关闭stdin后收到晚ready，仍尝试写operation并把sent标true；实际helper无操作退出/PG保持status3，但错误分类变unknown，原断言要求not-executed而失败。这暴露了spec要求“写点再次检查”的必要性。修复为本次process-local admissionClosed，晚ready只继续EOF，随后六项通过。没有弱化断言、覆盖原失败或根据status伪造未提交。

最初一次编译命令用了PATH默认Node24.18.0，mise命令也不在该shell PATH。随后找到并显式使用 `C:\Users\plumlocal\AppData\Local\mise\installs\node\22.23.3\node.exe` 重编当前desktop到round4/compiled。所有实际PG场景从首个normal起就由harness的上述锁定Node22路径运行，逐case node-start可核；没有把Node24编译尝试当版本资格。

Linux命令全部显式Ubuntu/plumwsl；`.venv/bin/python`3.12.15、真实PG17.9/pg_search、UID1000。全部14个home为 `/home/plumwsl/.g0p4-*`，0700、ext4/device2096，Unix socket短路径，不开TCP、不读settings/provider、不sudo、不终止distro。interop/Windows PATH/automount保留。

每case仅清理本次准确home，按准确data/socket用真实pg_ctl停库，等待本次有限命令及锁退出后再移除home。Linux信号使用先持pidfd、再核start_ticks或私有home关联后发送；诊断gate恢复仍只对本次目标。最终 `cleanup-audit.json`：14个home全部不存在，当前用户可观察范围无本次tagged进程。PermissionError仍会跳过，不能声称跨权限全系统无残留。

## 复现与未覆盖

Windows在仓库cwd用锁定Node执行：

```powershell
& 'C:/Users/plumlocal/AppData/Local/mise/installs/node/22.23.3/node.exe' desktop/node_modules/typescript/bin/tsc --project desktop/tsconfig.json --outDir output/windows-g0/pg_admission_r3/round4/compiled
.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/round4/generate_candidate.py
```

候选使用同typescript入口，以ES2022/CommonJS/strict/esModuleInterop/skipLibCheck、node类型和 `desktop/node_modules/@types` 编译 candidate-source下postgres.ts/postgres-posix.ts/postgres.test.ts到candidate-compiled。Linux需先将helper.py/control.py/fault.c同步到本clone同名round4目录，使用原round3相同PG头文件和cc -shared -fPIC -ldl构建fault.so；不复制venv/node_modules/二进制。

```powershell
.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/round4/harness.py normal
.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/round4/harness.py before-ready-eof ready-eof ready-cancel operation-eof reply-loss unterminated-result
.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/round4/harness.py helper-death carrier-death node-death
.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/round4/harness.py foreign-before-readback
$env:PG4_IMPLEMENTATION='candidate'
.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/round4/harness.py foreign-before-readback reply-loss
Remove-Item Env:PG4_IMPLEMENTATION
.venv/Scripts/python.exe output/windows-g0/pg_admission_r3/round4/verify_results.py
```

harness.py每次写results.json，归档各阶段前应另存对应标签；最终原始阶段结果及关键失败在archive-files.txt中，每组只选结构化结果，不复制同内容console log。`owner.baseline.cjs`保存typed guard前的真实基线fixture，`owner.first.cjs`保存late-ready原失败；最终owner.cjs还包含后续spawn错误分类补充，不能把当前脚本冒称历史逐字源码。编译产物可由受测Git基线生成，不归档整份编译owner或依赖。

诊断调用的15/25/35秒等待只是有限实验观察边界，不是产品任务寿命cap。本轮未覆盖完整frame/schema/byte资源边界、所有取消和启动故障交错、真实Electron GUI Quit、共享kernel运行中的全部可用性、外部手动绕锁PG、多版本PG、长 horizon、clean-v0/BM25/vector及完整其他G0门。研究helper/fault控制和typed草案不得直接import生产；原owner的归属负例在生产仍存在。
