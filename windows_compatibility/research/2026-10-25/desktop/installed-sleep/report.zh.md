# 正式安装版 alpha7 睡眠/唤醒观察

后续限定（2026-10-25 18:47）：准备更新时正常Quit报告`StaleHostWriter`，PG仍运行。原会话lease实际在本次睡眠期间到期；根因及修复见[root后续记录](../../root/phase-b/report.zh.md)。下列10项只读快照原结果保留，但不证明会话writer恢复、新turn准入或正常Quit，完整睡眠恢复资格重新开放。

用户手动睡眠并回复“已唤醒了”后，原安装版的10项前后比较全部通过；没有重启app、PG或WSL，没有改变interop、TUN或电源设置。观测只含只读SQL、已有锁/进程/socket元数据、公证书和一次Windows HTTPS GET /healthz，无settings/token/authURL，无业务操作重发。

before采样2026-10-25 18:19:40.410625+08；after采样18:23:40.331261+08。System Microsoft-Windows-Kernel-Power记录506/1448为18:21:37.0856655+08，507/1450为18:22:57.0034213+08，两事件间隔**79.9177558秒**。两个观察快照相隔239.920636秒，不能当作睡眠时长；用户本次仅报告已唤醒，没有另行提供精确时长。

原Windows main PID8324、HANDLE creation134373958392475087不变；Linux alpha7 kernel468/start22382、PG498/start23109、PID namespace4026532223与bootid不变。PG systemid7700523439330484737、postmaster start17:57:33.694648+08、PGDATA inode171484、Unixsocket/noTCP身份不变。kernel锁inode145305与PG lifetime锁171582均保持busy；diagnostics socket inode192175、HTTPS listener inode31687不变。原session:r3d6okwuq2trl3ribxzct6rbuy仍3个COMPLETED turn、latestentry28，未重置或修改原会话。

Linux先将监听inode绑定到同kernel的真实socket fd，再观察公证书；Windows after沿用before完整PEM作CA，并比较完整DER，https://127.0.0.1:40363/healthz一次返回200/status ready；没有重试或换证书、换origin。只读初始公证书观察不替代生产BackendSession的Session准入。

连接发生变化：同一Windows网络owner26656/creation134373958403754432，睡前4条Established本地端口49377/50062/62023/64405，睡后6条53145/53255/53894/57212/57929/59373，旧端口无一保留。这证明同原实例唤醒后恢复可用，不能声称原TCP始终不断、HTTP请求完全无中断、SSE保持或所有UI在途操作均完成。本次使用正式health/long-poll应用，无另造SSE fixture。

before/after脚本与Windows/guest有限命令均exit0，未出现断言失败。Linux pidfd、锁观察fd及Windows HANDLE在finally关闭，SQL连接上下文退出，HTTPS观察进程退出；正式app/PG有意保持运行，没有需清理的持久观察服务。原始before.json、after.json及两份运行日志保留，verified-results.json仅作这些事实的汇总。Tray、真实IME/AltGr、多DPI/snap的人工范围仍见README，不由本次睡眠观察替代。
