# Phase B 最后补证：setup / TLS Session / ENOSPC / recipe

2026-10-25。当前 alpha.10，源码基线 `0ea98e63d90043773f42bd9bb9bbb56951fca8b4` 加先前七处本包版本同步与 Windows mise 路径修正。**本批生产 owner / native helper / 资源依赖 / 版本无新增改动。** `production-scope.json` 核上述实际 production/native diff exit0；`tested-js-provenance.json` 核实际已装 app.asar 的 WslSetup/host-wsl/BackendSession 与测试所用编译 JS 完整 bytes 相同。Linux 直接使用已装 alpha10 Python `-I`，实际 import 路径保留在 raw。没有从 src 覆盖产品 import；只将现 tests/helpers 及开发测试依赖追加到已装路径之后。

本批 **8 项 setup、3 项网络组合和 1 项有限 ENOSPC 注入通过**；原 recipe 独立 fixture **7/7**。分组独立陈述，不与此前 Node/PG/provider 数量相加宣称完整覆盖。所有失败原样保留。上述自动owner故障探针没有启动正式 Pulsara/PG、provider 或额外 kernel；探针只观察正式 settings 的 inode/size/mtime/mode/uid，不读取或复制凭据内容。后续单独的原矩阵 Explorer GUI 补证按授权正常启动/退出正式应用，由原应用加载配置，未发模型消息；见末节。

## 1. 真实生产 WslSetup（8 项）

`setup-results.json` / `setup.json` 为原结果与物理 exit0。真实发现已有 Ubuntu（WSL2）/plumwsl，UID1000；缺失 distro、未知用户、root 用户明确拒绝，不创建、注销、换默认目标或以 root 启动应用。损坏私有 saved 文件读取不改原文；明确 select 后保存/重开固定目标。selection 文件只位于本次 output，随后删除。

真实 alpha10 既有资源沿原 `prepare()` 再验证成功；同版本传入不同摘要在既有 marker 比较处拒绝，**没有读/覆写该版本的 tar 或 marker**。这不是一次实际新版本安装，也不等于构造整块损坏已装资源后的恢复测试。

实际 stdin 部分输入取消时观测到本次 `.install-bsyZtjQM/payload.tar` **30056448 bytes**；随后 abort 原 source，等待原有限 installer close，再核本次 stage 全清、没有新 UUID version 发布、flock 可非阻塞取得。已送到 Linux 的部分 bytes 不能改称未提交。before/after 所有正式版本 marker 相同，settings 与 PGDATA 的具体元数据相同；不冒称已读取整个数据库逐字节比较。

## 2. 生产 BackendSession 的真实 Electron 网络组合（3 项）

`network-results.json` / `network-retry.json`：host33612、唯一 Linux guest436/start4523，carrier0、Electron0。实际 `LocalHttpServer` / `RendererBridge` / `LocalImport` 来自已装 alpha10；Session/pin/origin/readiness/seal/retire 来自与 installed asar 相同的 production BackendSession。用于观察证书 callback 的 facade只转交原 production proc，不复制准入规则；BrowserWindow/net.request 都使用同一个真实 native Session。

**资格范围明确：** HTTP 的 sessions/Core、controller connection 是测试供给的 process-local 依赖；SSE/slow-download/pending 为小型控制路由。本批不是正式 GUI/kernel/main 整体在途请求关闭实测。没有以 SimpleNamespace 或手动 abort 列表冒充完整 main 生命周期。

1. **自然过期与新握手。** 仍调用生产 `create_desktop_tls_identity`；仅在本次 guest 内临时注入 X509 builder 的短日期，保留原 key/SSLContext/0600 临时文件 owner。生产默认日期仍为1970–9999，未修改产品或系统 clock。证书实际 validTo 为 UTC14:34:40；真实时间跨过后，同 guest PID/完整 cert、SSE计数5→292，原 Session fetch成功；全新 native Session 新握手也成功，原完整证书相同且 callback code0。不是模拟发 expired 事件或全局忽略证书验证；private key不输出，factory临时 key 文件已删除。
2. **在途退休。** 一个真实 LocalImport 部分 stage589824 bytes、0600/uid1000；一条 SSE、三条未发 headers 的 pending、一条已收720896 bytes 的 native DownloadItem 同时存在。调用原 `primary.retire()`：先原 seal，并立即安排 native 回调外的 deferred DownloadItem cancel；retire 等调用方已持有的四个 ClientRequest（upload+3pending）和真实 WebContents destroyed，然后 closeAllConnections、等待 download done 并 clearStorage。四个 request close，DownloadItem最终cancelled；其最终bytes0不能抹除先前已收bytes。Linux五条控制handler都 transportClosing 并退出，实际 import stage与published文件皆0，旧Session新fetch拒绝。上传只出现一次原请求，未重放或宣称提交成功。控制stream取消后的观察status500不是业务成功。
3. **真实同端口换证书。** 关闭原 server 后，本次同guest进程的新 LocalHttpServer/SSLContext实际绑定同一35757端口，使用不同完整cert；另一个仍未seal的旧pin Session关闭原连接后新请求被原验证回调-2拒绝，原始错误net::ERR_FAILED。新pin Session成功，已seal旧primary继续拒新请求。这里是同guest内两个实际server对象的端口重绑定，不冒称另一个OS进程或用户陌生服务接管了端口。

原 first `network-first-*`：探针误请求不存在的 `/api/healthz`，真实404按断言失败，未进入在途阶段；当前正确生产路径为 `/healthz`。旧guest正常close0/home清，原失败不改写成产品问题。修正只涉及output探针；没有放宽HTTP状态断言。

## 3. 生产安装路径 ENOSPC：明确的有限注入

`enospc-results.json` / `enospc-open-final.json` 为最终结果，物理 exit0。测试 VM加载同一原生产 WslSetup JS，仅在本次有限 installer 的 execFile argv增加 `env LD_PRELOAD=<本次私有.so>`。不改脚本、WSL协议或产品；没有挂载全局磁盘、设置系统quota、填满共享ext4或给常驻进程注入。原 shell/Node streaming、trap、错误与close仍各由原owner执行。

最终 shim 只在本 child 的 `runtime/.install-*/payload.tar` **open分配点** 返回 errno ENOSPC。实际 stderr 同时保留注入marker与 `cannot create ...: No space left on device`；dash原exit **2** 经生产 `prepare()` 原样传播。本次输入可能已进 pipe；不能因为 output open失败就把整个操作改报未执行。stage已创建并由原trap清理；UUID版本未发布，flock可再取，正式 marker/settings/PGDATA元数据 before/after 相同。

这证明生产有限安装owner处理该**注入的分配失败**；不是实际物理磁盘耗尽、解压中ENOSPC、或所有写点都已实测。

保留四次原fixture失败：

- `enospc-first-*`：write hook未实际命中，操作只因坏摘要失败，不计ENOSPC。
- `enospc-second-*`：测试C使用本机头文件没有的loff_t，编译失败，尚未提交安装。
- `enospc-third-*`：off64_t修正后write/splice仍未命中本机uutils cat的数据通道，仍只见坏摘要，未推定具体底层syscall。
- `enospc-open-first-*`：open注入确实命中，但新探针错误期待cat的exit1；实际是dash重定向的exit2。保留原失败，最终专门断言2及原ENOSPC stderr，不改产品或接受任意非零。每次自有lib目录均已删除。

一次私有user/mount namespace可用性观察中，`unshare -Urmp --mount-proc /usr/bin/id -u` 因 mount /proc Operation not permitted退出1；未绕过或提权、未改变共享mount，故采用上述明确注入层次，而不假称受限文件系统实测。

## 4. 原构建缓存fixture与交叉审阅

`build-cache-final.*`：正确process PATH下，原 `desktop/scripts/test_build_cache.py` **7 passed**，没有资源构建/共享依赖改动。首次虽用mise exec，但未把mise自身目录放PATH，`shutil.which('mise')`为None，原结果4passed/3setup errors保留于`build-cache.*`。修正仅进程PATH。该7项fixture验证原mise freshness与真实uv/wheel隔离边界，不测cmd语法；cmd反斜杠语法由上一批实际Windows `mise desktop:windows-resources`成功证明。

只读审root R0–R5新基线报告，未发现来源/计数实质夸大：WSL105/Windows54+10不相加，R5随机context与R1确定性race分开，额外find_files/首观察器计数错误保留，隐藏恢复不冒称tray，安装保数据与最终正常Quit有独立证据。该结论不将此前alpha9资格自动继承为alpha10或宣称B5。

## 5. 精确收尾与剩余范围

`cleanup-linux.json`核两个guest PID/start不再相同存活、两home及各次ENOSPC私有lib/UUIDversion都不存在、安装stage空；`cleanup-windows.json`核两个本次Electron PID都不存在，实际launcher/carrier exit0另有原记录。没有以泛杀或终止共享Ubuntu制造通过；本次没有scope/Job常驻helper。仅各 output Electron idle profile作为无活进程缓存保留，未入归档，不冒称已删。

首次cleanup脚本因Windows默认GBK读取UTF8 compiler错误失败，随后串行PowerShell生成了无意义的null-PID JSON；`cleanup-first-*`保留，此份不作为liveness证据。改明确UTF8并先核Linux成功后，才生成当前两个真实PID结果。22:40:28清理核验后已向root报告 safe-for-sleep=true，此后不再启动case。root后续现场睡眠证据归其独立报告，不混为本fixture证明。

具体尚未由本批闭合的项：真实WSL未安装/需重启已按用户缺备用机且不删Ubuntu而延期；整盘满/解压中ENOSPC不是本次open注入；正式main/frontend在途upload/SSE/pending/DownloadItem共同关闭仍须以原真实owner装配证据判定，不能只由本fixture推导；真实其他输入法/硬件/分发签名及原生Ubuntu等仍按主矩阵。不为补缺证更改系统clock、VPN、interop、用户settings或正式PG，不自行激活Windows/B5。



## 6. 原矩阵 Explorer 新建→产品刷新/选中（补齐）

root核对确无旧raw后明确授权只补此项。按computer-use技能使用真实Windows Explorer在`\\wsl.localhost\Ubuntu\home\plumwsl\workspaces`以Ctrl+Shift+N及可见名称控件创建唯一空目录`Explorer 中文 alpha10 fb3136ea`，没有用mkdir/API代替创建。正式alpha10 HTTPS41131的picker在创建前已打开；Explorer创建后其旧列表仍未出现新名，点击产品“刷新”后列出，进入并“选择此目录”后新会话框Value为`/home/plumwsl/workspaces/Explorer 中文 alpha10 fb3136ea`。随后Escape取消，无需创建session；总览仍4会话，没有发送消息或模型调用。

只读已装Python核同canonical路径、inode265523、uid1000、ext4、0755且空；目录保留供复核。原Ctrl+Q正常退出后23:04:34正式app进程0、PGstatus3、kernel.lock可取、diagnostics.sock不存在；未kill/手工停PG。仅关闭本次打开的Explorer窗口。原CU几何不可用/异步UIA滞后均重新观察恢复，保留在`explorer-results.json`的limitations中，不当作产品错误或重做额外GUI矩阵。实际before/stale/after/selected文本、目录身份及正常Quit见`explorer-*`原证据。

独立active-sleep与§11–13逐项审阅见`spec-11-13-review.zh.md`。明确不把“同一个main run所有类型网络流同时在途”或“填满用户物理盘”增加为验收门；ENOSPC保持有限分配点注入的准确层次，用户已延期项不标passed。
