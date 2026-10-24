# 第六轮补充：独立发行版、SIGHUP 与实际窗口控制链

2026-10-25。root在审阅 `disposable-distro-plan.zh.md` 后明确授权执行。该计划和此前22例主矩阵保持历史事实，新增结果在单份 `supplement-matrix.json`。生产/spec/共享PG helper未改，无PhaseB。

## 独立发行版正常/整机域失联

唯一 `PulsaraG0-kernel6-20261025-d88d705e`，注册ID `{2abf609d-eb8a-4980-9f49-a5cf79b45220}`，BasePath严格位于本次 `round6/disposable-distro/同名/instance`。创建前、terminate前、unregister前均核name/BasePath/Version2；原默认ID和共享Ubuntu注册项不变。见 `distro-lifecycle.jsonl/distro-state.json`。

从 [Canonical官方26.04.1目录](https://releases.ubuntu.com/26.04.1/) 下载实际 **418495746 bytes** WSL rootfs；内容SHA256匹配官方SHA256SUMS，且gpgv以既有Ubuntu archive keyring核官方SHA256SUMS.gpg通过（Ubuntu CD Image签名key）。清单/签名保留，399M镜像已在结束时删除。只通过tar pipe转移随包Python477121651bytes和PG356507014bytes；未export日常Ubuntu、复制home/settings/凭据/PGDATA、安装dev环境或改变interop/TUN。新用户UID1000，ext4 home0700；实际bundle module来自新实例资源site-packages，未用系统Python替换。

PG P5协议/helper只机械复制到本分支output并改目标distro/解释器/探针目录，实际framing和guard/lock算法不变，共享P5原文件只读。原Runtime/PG/main Quit编译产物仍同一份，`distro_build.py`的替换次数有断言。没有第二owner或重发/恢复器。

- `normal-3cf88beb`：新发行版内真实bundle kernel+PG ready；实际application.close、carrier0/lockfree后原main才PGstop/main exit0。说明资源与新rootfs真实可用，没有借共享Ubuntu跑正控。
- `distro-loss-ae39b6d0`：ready后kernel/PG锁均busy、实际PG live。仅该唯一distro被terminate；故障前持续持有的carrier HANDLE creation精确绑定，terminate后wait0。原载体close1；output两行候选保留quitError/PGstop0/appExit未请求，已接受start不重发。
- 显式恢复查询会重新启动该独立发行版：PID namespace由 `pid:[4026532221]` 变为 `pid:[4026532223]`，同WSL kernel boot_id不作为旧进程连续性的证据；双锁变free，无本次PG，PGDATA inode64036、kernel/PG lock inode64033/64035保留，postmaster.pid仍是旧内容。它证明新namespace内恢复事实，**不是故障前pidfd的连续观测，也不证明此前应用clean关闭或已接受操作没有执行**。没有application-close-returned；整distro故障归unclean/UNKNOWN。

恢复清理先在新namespace核两锁free/无本次PG，核postmaster.pid记载的精确PGDATA，再仅unlink本次旧pidfile供研究cleanup；避免对新namespace复用PID调用pg_ctl stop。未启动第二kernel/重启PG/重放start来取得绿结果。两home均精确清理，随后独立注册项和VHD注销、只删本次精确镜像/空目录；原Ubuntu仍Running且default未变。`distro-cleanup.json`保留前后注册与空间事实。

准备时第一次tar源stdout在交给目标后已close，随后communicate又启动reader，产生非致命 `ValueError: read of closed file` readerthread提示；source/target实际exit均0、资源体积和真实normal链通过。修正只将已关闭的source.stdout字段置None，不重传/不把该提示隐藏成产品故障。新rootfs首次登录还记录systemd user-session提示；没有改配置去“修”它，实际bundle正控制成功。

## §9.3 宿主死后的实际Linux收尾合同

此前“host死→kernel已退/锁free”只能分类unclean，不能代替原EOF/aclose合同。新增真实对照只改启动时SIGHUP处置，不新造daemon、parser或生命周期owner；原kernel仍只安装它原有SIGTERM/SIGINT策略，stdinEOF由原commands读取，application.aclose由原finally调用。

- `host-loss-candidate-node-b772f4da`：精确HANDLE终止宿主后，guest实际记录 **SIGHUP=1**；观察handler先记signal再恢复SIG_DFL并self raise保持默认终止语义，未进入aclose。信号真实发生；日志没有siginfo发送者，故不声称已证明内核信号sender PID。
- `host-loss-candidate-node-fb2f58fa` 与实际Electron `host-loss-candidate-electron-61abedf7`：启动接缝的SIGHUP handler只记录后返回（忽略该信号的终止效果），其余原控制不变。宿主死亡后仍实际收到SIGHUP，原stdinEOF进入原application.aclose门；门内原kernel仍live、kernel.lock仍busy。外部只放本次研究门，随后原aclose返回，预先独立持有的Linux pidfd确认精确kernel退出、锁释放。PG仍live直到明确fixture cleanup，宿主已死不可能执行main PGstop。

这两例支持最小启动signal策略使Linux原EOF/aclose收尾合同可行；未修生产，也没有把原hostloss无aclose反例抹去。已死宿主拿不到结果仍不能把远程call结果从UNKNOWN提升为成功/未执行。整distro terminate不受忽略SIGHUP策略保护，仍保持前述UNKNOWN。尚不宣称所有跨域启动/宿主崩溃组合已覆盖。

本次HUP策略实测发生在kernel已ready且策略已安装之后，不能由此宣称Python尚未安装启动策略时的全部launch交错都已覆盖；若生产要在更早exec边界设置继承处置，应在对应接缝另验。SIGHUP日志只证明宿主强退后guest实际收到了信号，不证明sender PID/内部WSL实现。

## 同一真实owner的可见窗口close

`browser_build.cjs` AST选取当前main唯一 `window.on('close',…)` 原callback，连同已机械抽取的完整原requestQuit执行。真实Windows Electron BrowserWindow（sandbox/contextIsolation开启）show后isVisible=true，以实际window.close触发该callback；原dialog只用退出选择stub，状态/PG/kernel控制未复制。研究窗口显示自己的说明页，不当成正式产品前端/安装包。

首例 `window-close-original-electron-200378a3` 已实际走到原close/PGstop，但研究壳缺少原main已有window-all-closed no-op政策，Electron默认提前退出，未写最终host-result；Python最后KeyError保留为fixture failure，不能因底层run_case的observed字段为true就计为合格。`browser-probe-200378a3-failure.json`准确标注。

只补回原main既有no-op政策后单项 `window-close-original-electron-1f068d6c`：原窗口close callback→原requestQuit→实际kernel.application.close/carrier0/锁free→PGstop→原main appExit0请求。随后研究壳destroy窗口只用于退出stub后的fixture结算，不能把它当提前触发产品成功的动作。

这是G0真实owner控制机制的窄正证。它已含实际可见窗口/关闭事件，不把尚未进入PhaseB的正式GUI实现/installer反过来设成必须先做B才可完成G0的循环门；也不声称正式产品壳或安装分发已经验收。

## 人工睡眠实测与原owner正常收尾

`sleep-076aee0b` 启动真实bundle kernel/专用PG静置：Windows host37428，kernel1062/start42308，PG1100/start42462，namespace4026532222，双锁busy，实际psql读取postmaster start/current_database成功。root统一请求用户实际睡眠并解锁确认。等待期间未自动sleep、发故障或清理。

用户明确回复实际睡眠约1–2分钟且已唤醒，root统一通知后才打开inspect门。实际核验同host持续持有的HANDLE creation、同kernel/PG PID+starttime/namespace、同kernel/PG锁inode且busy、相同postmaster启动时间与数据库名的真实SQL结果；没有重启或重发。然后放研究resume门，原requestQuit/application.close/carrier0后PGstop1/main appExit0，精确home/PG清理，无forced升级。这里只对该次实际人工睡眠给出真实kernel/PG连续性与正常收尾正证；Electron powerMonitor/HTTP session辅证由races/root独立保存，不把本探针没有监听的系统电源事件写成自己测过。

独立系统电源/网络传感器证据见 `output/windows-g0/interop_races_r3/round7/manual-resume-observation.json`、同目录 `report.zh.md` 与 `n7-47fc4a8b-8034-46b3-afc6-27a27259df1e-results.json`。races已只读交叉审本补充，确认上述HUP/namespace/窗口/Sleep窄边界未实质扩大；本分支未运行另一网络/电源实验。

## 补充矩阵与最终清理

`supplement-matrix.json` 收8个实际case：2独立distro、3真实SIGHUP、2窗口（其中首条保留fixture failure）、1人工睡眠。7项合格、1条原窗口fixture失败；此前 `final-matrix.json` 的22例保持原选择，未重跑整套或修改旧失败。全部新增source、必要官方清单和raw汇总均在archive-files，舍弃镜像/VHD和重复stdout。

`supplement-cleanup.json`：8个Windows host精确creation、6个共享Ubuntu kernel/home均已退出/移除，剩余精确host/kernel/home及当前不可观测PID全空。另2例属于已经注销的独立PID namespace，不能拿它们数字PID去共享Ubuntu假装当前身份审计；它们的原正常/精确载体join、新namespace双锁/无本次PG、精确home删除和注册注销证据分别保留。每例PG两锁free/无本次进程后才移除home，未强制清理升级。

G0实际owner机制可行性新增上述有限正证；仍保留原生产缺口和实测范围。正式产品GUI/installer为PhaseB工作，不以尚未做B本身循环阻塞G0；没有因此激活Windows或进入B。
