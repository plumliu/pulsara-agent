# Phase B：PG finite helper 与后端接入

2026-10-25。本报告记录正式实现和定向验证，不表示 Windows 激活或 B1–B5 全部完成。生产源码由 Git 工作树持有，本目录只保存小型验证脚本及实际结果；未提交 Git，未改保存 settings，未调用真实 provider，未终止共享 Ubuntu。

后续真实NSIS首启暴露的SQL换行/catalog drift缺陷及修复，见[首启修复报告](schema-first-start-fix.zh.md)。该报告也保留同一ready实例Windows localhost转发延迟的新实证；不能用前面的聚焦通过覆盖后续集成发现。

再后续用户已在真实GUI配置模型，terminal/PIPE-PTY关闭输入/Linux工具链/Windows PE的真实provider有限补证见[执行报告](provider-acceptance/report.zh.md)。下文“用户尚无保存连接/尚未跑provider”描述本报告初始阶段，不是当前补证状态；其它长程矩阵仍以各分工结果为准。

## 实现与所有权

- `desktop/src/postgres-wsl.ts` 只实现原 `PostgresOperations`，由唯一 `BundledPostgres` 决定 start/reuse/stop/reset/startedHere。复用 host 的固定 WSL target/argv 三 pipe，不创建第二 PG owner。未提交、已提交但结果未知、已确认非零分开；仅完整合法 result 加真实 carrier close0 才采纳。取消封闭写入，晚 ready 不能重开；不重放。fatal UTF-8、完整 LF、恰一 ready/result、各 pipe 4 MiB 上界均在有限协议边界落实。
- `desktop_guest_ops.py` 在 ready 前取得当前原 kernel 锁及适用的 PG lifetime EX。原 kernel 锁不由常驻 PG 继承；PG start 链只继承独立 lifetime 锁，有限 status/stop 继承 kernel，initdb 两者继承。锁文件在 PGDATA 外、O_NOFOLLOW/UID/mode/nlink 验证，普通 reset 不 unlink/替换。remove 必须当前双锁和 fresh pg_ctl status3，实际删除仍在锁内；固定命令/实例 data/socket 核验，不接受任意 argv。恰一完整 operation 加 stdin EOF 才执行；ready 前已有输入拒绝。
- 维护检查复用同一有限 client/helper，只有当前双锁和停止事实才准入；empty-layout 只读核查，不 start/stop/remove。允许创建空协调锁文件，不引入持久权限凭证。检查是当下事实，安装器仍须自己的应用运行/覆盖准入。
- `desktop_runtime.py` 只从可信 start.host 配置 interop，TLS 创建本进程证书，ready 返回完整公证书；正常 stopped 在原 application.aclose 和 kernel 锁退休后发送。OAuth 仅把原 SDK 的 open_browser 请求转交 Windows，opened 只说明浏览器打开，不说明授权成功。PKCE/state/token/grant 仍由原 SDK/管理 owner 负责。
- `tls_identity.py` 复用 cryptography 和 aiohttp SSLContext；私钥仅 guest 临时私有文件，load 后删除。cryptography50 成为显式直接依赖，未换版本。
- 用户目录 service 的唯一根是 Linux 实际用户 home/workspaces，GET 不建根，显式 initialize 才建。原 HTTP 安全校验继续使用。目录验证拒 symlink/越界/替换/不适用存储；分页不设总目录上限；mkdir 结果未知不重做或回滚。HTTP create-session 再验证，原 KernelHostCore 在原 resolver 后、资源接纳前做同一验证，关闭 picker 与 canonical 接纳间的已知链接替换窗口；quick/transient 不改。

## 已执行

1. Windows 锁定 Node22.23.3 构建 TS 成功；新 postgres-wsl 七项原生三 pipe 测试通过，覆盖分段 UTF-8、完整帧/重复/坏帧、非零与 busy 分类、写入前后取消、维护及独立输出上界。它们是运输边界测试，不能当真实 WSL/PG 故障矩阵。
2. `verify_real.py` → `prepare_guest.py` → Windows `real_owner.cjs` → 生产 BundledPostgres/生产 WSL operations/生产 host launcher → 独立 ext4 guest 源码快照及真实 PG17.9。`real-run.json` exit0；`real-result.json` 保存 SQL 版本、data/socket/无 TCP、首次初始化、reuse 不认领、运行中 remove 拒绝、原 reset 的 stop→fresh guard→实际删除、锁 inode/外部 sentinel/socket 保留、重建 start/stop，以及 empty/running/stopped 维护事实。
3. 私有 ext4 Python 快照（Linux主 clone 源码不改）：`backend-full-progress.json` 64/64，通过新 helper12、后端8、原 runtime12、HTTP20、OAuth12。之后只增加 canonical 接纳链接替换的确定性测试，`backend-quick-result.json` 后端9/9；未把两次运行谎称一次65项全跑。新增用例通过原公开 open_session 证实 resolver 看见根外目录后被拒，_ensure_resources 未调用，无接纳残留。
4. helper 用例真实子进程证明 FD 继承、非零/信号/输出超界、spawn 失败、双锁、固定实例与真实 rmtree；测试存储类型 fixture 替换不代表该临时 pytest 路径本身是 ext4。真实 PG 纵切使用实际 ext4 检查。
5. 本片聚焦 Ruff 检查通过。root 另跑整体 desktop Node 回归，计数以 root 日志为准。

## 失败与 fixture 修正

- `first.backend-quick-result.json`：初次 fixture 的无效 DSN/过长 Unix socket，三项失败。改用原 desktop_home fixture 的有效 DSN/短 socket，未弱化断言。
- `backend-test-result.json`、`first.backend-full-progress.json`：初次组合运行未完成，后者45项后 OAuth 等待不结束，原样保留。`oauth-diagnose.json` 将原因定位为隔离源码快照缺静态 logo，真实 callback 页面500，fixture 浏览器任务断言失败而原 OAuth 等待；修正快照 static 链接后原 OAuth12全部通过。诊断 timeout 仅用于定位，不是产品生命周期上限。
- 卡住的两个自有 pytest 仅在核准确切 UID/cmdline 后通过 pidfd 结算；未杀共享发行版。私有源码目录由 harness finally 精确删除。
- 原独立 PG 正控 `first.real-result.json` 保留；加入维护消费者后重新跑当前纵切，最终以 `real-result.json` 为准。

## 共享修正与只读交叉审查

root 的 main/runtime 当前使用 attempt AbortSignal、PG initialized getter；start/reset 清初始化事实，只有本次确认 initdb 成功才初始化角色/库。Runtime 已提交后的失败/非零/stopped、fatal UTF-8/完整 LF 由 root 修正并记录原测试。未把常驻 PG 与 kernel 锁混同，也未把 child.close 当 mutator 未发生。

向 tasks 报告并已只读复核三项真实接缝修正：environment 未 join 的失败 attempt 留在清理库存；MCP stdio 的唯一 close task 保留物理关闭错误，取消后等结算；scope/diagnostic 拒绝无 LF 的 EOF 残帧。terminal 最外层不得丢 bootstrap unknown。tasks 的126项结果由其独立快照产生，本人未复跑或冒领。未发现新的具体阻断；不以 Linux PGID 证明 Windows 后代已结束。

## 清理及剩余资格

最终专用 PG home `/home/plumwsl/.g0bpg-zilcj8zz` 清理前真实 status3、两锁可得，`real-run.json` cleanup exit0/目录已删。较早 `.g0bpg-5x5bwhgf` 已在 `cleanup-result.json` 精确清理。所有本片私有 `.g0backend-*` 快照已由 harness 退休；未动保存设置、他人 PG、interop/TUN 或共享 WSL 生命周期。

当前代码的正常真实 PG 链及定向协议/后端边界有证据；G0 深故障矩阵仍是历史研究证据，不假称新生产 helper 已重跑全部排列。Windows 安装后 Electron/TLS/session、真实目录 UI/文件往返、生产 OAuth 贯穿、完整 Windows 后代生命周期由集成验收补齐。真实 Notion R6 是研究链，不能代替当前正式 runtime 重验。真实 provider/长程尚未跑：用户尚无保存模型连接。原生 macOS/Ubuntu 未在本片实测，WSL 不能替代它们。

## 重现入口

在仓库根使用项目 Windows Python 执行 `output/windows-g0/pg_admission_r3/phase-b-pg/verify_real.py`（实际新建并精确清理专用 PG）；后端为 `test_backend_snapshot.py --full-progress` 或 `--quick`。脚本中的目标固定当前专用 Ubuntu/plumwsl 与项目环境，换机器应先核准目标/资源。不要重用已删除 config.home，不要把这些测试脚本导入生产。
