# C 第六轮与 root F/N 报告只读终审

已读 tasks/round6 的 owner.cjs、runtime-candidate.diff、report.zh.md，以及 root/round5 的 report.zh.md、文件/HTTP候选。未改对方目录、未重复运行其矩阵。

C 两行候选只删除 child 已清空时的提前 return，把 send 限于仍持 child；原 await this.exit、reachedReady/退出码检查保留。原 main 的 runtime.stop 成功之后才 PG.stop，故三个已测非零退出窗口可阻止后续 PG stop。报告正确区分：22 case/18 observed/4 harness failure；clean EOF 门才是实际应用关闭与锁释放的正证；宿主死亡的 kernel 退出/锁释放/PG仍活只是 unclean UNKNOWN，不能归功于已死 main。没有凭 Node/Electron 无 BrowserWindow 研究壳宣称 GUI Quit、整发行版恢复或睡眠通过。候选对未测试的 carrier0/kernel失联不作保证，措辞准确。

root F/N 报告已明确当前 HTTP sessions/protocol 预置 SimpleNamespace，不是完整 application/session/main 装配。手工 revoke 与旧 Session 新请求拒绝只说明所测边界；13项不含持续连接退休/自然过期/睡眠。PDF精确内置 extension、仅其 frame 的 chrome://resources 例外仍保持 backend exact origin/full cert；原白屏与同步 DownloadItem.cancel 错误保留。文件路径组件修正以最终9项有限调用复验，未声称早期 GUI 全矩阵在最终源码重跑。现场三份导入没有逐一 DOM ID，报告没有编造 chooser/drop 对应。

未发现当前报告实质扩大证据；可按其限定范围归档。R7 网络 fixture 同样使用这些真实 HTTP owners和预置依赖，不把物理 Session/stream 退休扩成生产 main 或完整 controller 退休资格。
