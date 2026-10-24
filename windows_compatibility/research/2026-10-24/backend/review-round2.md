# 根实施规格交叉审阅：Backend 第二轮

审阅对象：根 `PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md`，2026-10-24修订稿，当前388行。已重新通读全文，覆盖终端、文件/数据库/非终端进程、IPC、Electron/CLI/安装、WSL、成本和激活矩阵。

**结论：可冻结。没有剩余阻断。**

这里批准冻结的是“Windows11 25H2 x64、本地NTFS、原生条件实施规格”，并不表示G0已通过，也不表示Windows已实现/已支持。G0未执行、Windows平台与provider证据缺失在正文中已被明确保留；全面实施和产品激活仍受其闸门约束。

## 第一轮阻断的关闭核对

| 首轮项 | 修订位置 | 结论 |
|---|---|---|
| R1-B1 PG auth初始化/失败/reset与owner | §7.2，191–205行 | 已固定private postgres-auth.json的必要内容与绑定、文件先发布后initdb、临时pwfile、已有cluster材料缺失/损坏/不匹配拒绝、不自动regen/rotate/ALTER PASSWORD/trust/reset、角色中断与schema重试边界、确认reset的data先删除/auth后删除和unknown失败；BundledPostgres/prepare_database/migration职责已纠正。客户端require_auth=scram-sha-256及typed parser一起hard cut可避免把server hba配置误当客户端认证要求。关闭。 |
| R1-B2 Win源可执行属性与launch admission | G0-F第85行；§8，235–239行 | Windows frozen execution属性明确NA，POSIX仍保留原执行位；native exe/com、cmd/bat wrapper、显式解释器脚本分开，ACL sealing不是可执行授权；真实argv、拒绝与属性变化测试列入G0，不让实现者随意翻译脚本或全True/False。关闭。 |
| R1-B3 非终端必要env/凭据边界 | G0-R第86行；§8第229行 | MCP stdio有自己baseline/lookup_path/secret_env/最后credential_guard，hooks保留自身overlay/scrub策略；仅复用key规范化机制，禁止全量process.env透传；SystemRoot/Unicode/cmd argv/credential mutation进入G0。关闭。 |

## 本轮跨专题复核

- **内部IPC闭合**：§7.3，211–215行冻结Windows Protocol-v3 loopback+原launch capability/frame/attachment，独占listener lifetime与close/connect admission线性化；capability不进入renderer/URL/日志/磁盘。diagnostics独立local-only SID DACL named pipe，明确home隔离、first-instance、server owner与抢占失败，不暴露未鉴权诊断TCP，也不另造RPC framework。
- **真实依赖复用**：§6.2明确锁定pywin32 b312的FileRenameInfo/RootDirectory与LockFileEx现有扩展点，G0仍要证明祖先绑定/reparse/sharing/flush。MCP已承认SDK能力及spawn后Assign/fail-open边界，不以SDK名义删除有界wire与credential保护。
- **终端结算与退出码**：Job创建前纳管、root/managed-set/output/thread各自结算、IOCP只是唤醒、ConPTY附属host不得永久占用集合、脚本通道不占stdin、标准PowerShell7 -Command退出码和一致bootstrap、TTY EOF未证明则G0不通过，均明确。没有悄悄缩小输入合同或加总任务寿命cap。
- **宿主管道**：UTF-8无BOM字节wire、chunk split、真实Node匿名pipe与overlapped限制、reader线程join、pre-ready/EOF/broken-pipe路径已列G0。没有把Proactor支持subprocess扩大成“Node传入stdin必然可直接异步读”。
- **桌面与安装**：per-user NSIS/CLI Unicode/stdio/exit、原生titleBarOverlay/tray、用户页不依赖session、OAuth URI只focus及预览隔离都保留；electron-builder默认Stop-Process/kill被明确要求从现有customCheckAppRunning扩展点替换，关闭/占用失败不能强行覆盖。
- **WSL与成本**：WSL是另行决策的完整Linux执行域，interop、发行版、home、跨FS/网络/生命周期的成本清楚；未授权实现且无自动fallback。估算单独排除G0失败/长期上游fork，并保留维护、签名、实机与日历成本。
- **不变量与激活**：prefix连续性、最小durability、无新fingerprint、三平台回归、真实provider+long-horizon、配置只读与精确secret scrub都清楚。macOS49/24通过未被冒充Windows证据，根spec正文足以独立实施，不依赖ignored output才知道决策。

G0仍需实证具体Windows binding/API/PG扩展构建/文件持久性保证，这是已冻结方案的前置工程任务，而非本次文档缺失或默认放宽授权。如G0证明外部合同无法满足，须按正文修订规格，不能靠本次“可冻结”跳过。

本轮仅写本审阅文件，未改根spec、生产代码、保存设置或数据库。
