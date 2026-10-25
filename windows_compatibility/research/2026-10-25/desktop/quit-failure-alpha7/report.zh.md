# alpha7 正常退出失败与原GUI Retry只读核验

root正常Ctrl+Q后在原cover观察到StaleHostWriter: host writer generation is stale。不是干净Quit：main8324仍活，PG保留；alpha8尚未安装。本分支仅取只读证据，没有操作GUI、重启/停止任何app、PG、WSL或安装器，没有重建alpha8，没有覆盖root后补的sleep限定。Host lease根因/修复由其他owner负责。

18:49:50+08原kernel468已经不存在，formalHome下精确desktop_runtime argv候选0、diagnostics.sock无，kernel锁inode145305空闲；原PG498/start23109仍活、PG锁171582 busy。原Windows main8324的HANDLE creation134373958392475087与睡前一致。CIM直属子进程清单未见wsl.exe。Windows宿主原kernel.log只有876bytes旧HOOK_SOURCE_STATE，mtime09:57:35UTC，没有StaleHostWriter堆栈；failed控制帧只由stdout交给TS，不在此stderr日志持久化。本次无法从现有日志直接恢复数字carrier exit code；已安装Python代码在aclose异常后emit failed、最后emit stopped并return1是源码判据，不冒充实际wait数值。

直接读取已安装app.asar，确认alpha7实际installed-main.js/runtime.js。stop等待同attempt的close结果，即使child已清空仍保留failure，重复Quit会继续抛同错误而不进入PGstop/app.exit0，这是原安全屏障。原Retry→boot允许已退出child的新runtime.start，先waitForPreviousOwner，再由原BundledPostgres检查并复用同PG，成功后换新TLS Session/窗口；不能通过删旧结果或直接停PG来称退出恢复。

root在只读调查期间通过原GUI Retry恢复。18:52:57+08新kernel53665/start340742，原PG498/start23109、systemid7700523439330484737、postmaster start17:57:33.694648+08与data inode171484不变，双锁busy。新监听https://127.0.0.1:42805的socket已核属新kernel，Windows用其公证书作CA/fullDER exact校验GET /healthz一次200/ready。原session仍3个COMPLETED turn/latestentry28。此证据证明原Retry恢复工作台，不证明原退出为clean，也不证明lease缺陷已修或下一次Quit一定成功。

inventory在18:51:51已捕获53665的-c入口。最初通信把inline Python当作其他agent诊断是不充分推断，现更正：该PID正是Retry新kernel，由真实listener fd归属确认；host的有限env/argv wrapper可使用-c。原始两份inventory保留，不更改事实或用-c推断owner身份。

当前原生显示快照只剩DISPLAY6 1920×1080/96DPI/100%，新窗口thread36108记录DPI96但IsIconic=true、rect[-32000,...]，故不能把此最小化坐标称为屏上实际位置。root说明Retry已重建窗口；这不是同一window从150%跨屏回返的正证。原同窗口96→144内屏证据与本次重建恢复分层。

18:52:07可观察Windows pulsara-interop.exe数0；Linuxalpha7/home inventory无interop模块标记。本分支没有完整普通PE任务的旧HANDLE成员全集，不能据helper0泛称所有Windows进程已清。root随后确认正式GUI无活动任务/MCP已清、其余provider隔离owner已早先清理，明确停止扩大旧scope审计。只读查询进程、fd、HANDLE及HTTP均有限退出/关闭，没有新增持久观察服务。原失败、原PG及alpha8制品保持，后续Host修复后新候选与关闭资格由root协调。
