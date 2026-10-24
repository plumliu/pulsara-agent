# WSL-only 根规格全文交叉审阅：terminal round 1

审阅日期：2026-10-24。审阅对象：根 `PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md`，本轮 387 行候选，文首状态仍为待全文交叉审阅。本轮只读检查并写个人审阅记录，未改根规格/生产，未跑 G0、测试、provider、构建或数据库。

## 结论

**可冻结；未发现必须阻止设计冻结的 P0/P1 或跨章节合同矛盾。** G0-W 尚未执行、Windows 支持尚未激活，不能把本结论解释为 pipe/锁/TLS/发行版集成已经可用。

已全文通读 §1–15，并交叉核对 backend/desktop/terminal 的 `wsl-first/proposal.md`。根规格明确选择的唯一 Linux-backed workspace picker、专用 Pulsara/nonroot/interop off、TS PG lifecycle owner、首版不做 Windows 全局 CLI、Linux `pulsara app` 提示 Windows 图标，均是明确产品收敛；不因与早期提案不同判为缺陷。

## 重点核对

1. **路线和 A 收窄闭合。** §1/§3/§11/§13 移除 native ConPTY/Job/PS7/Win32 files/Windows PG 的必选路径，A 只抽 Node locator/真实 stream/finite execution/PG 物理操作，Linux terminal/file/Unix transport 不重构；A 可先独立交付，B 仍须 A 和 G0-W 全过。每片 hard cut 范围明确，无空 Windows stub 或双路线 fallback。
2. **Linux terminal 语义没有被载体替换。** §2.2/§5/§13 保留独立命令、running process、多轮输入、PTY/PIPE、root/group/drain/worker join、8 槽与输出保留；不把 PGID 提升为任意逃逸后代/整个发行版保证。MCP/hooks/screenshot 继续各自 policy 与 guard，不合并 terminal 槽/完成权。
3. **Linux-only 支持域一致。** §1/§5/§8 固定 distro/user/Ubuntu/ext4 和 `interop.enabled=false`、`appendWindowsPath=false`、automount 关闭；清 WSL_INTEROP/PATH 不被当作禁止 interop 的证明；不存在接用户默认 distro 的路径，也未把 nonroot/专用 distro 包装为恶意本机用户沙箱。
4. **PG owner 与 leaf 分工成立。** §3.3 保留 BundledPostgres initialized/startedHere/时序，Python helper 只做有限 Linux 操作，不拥有第二 start/stop/reset 决策；Linux uid/mode/realpath/postmaster.pid 检查不由 Windows UNC stat 替代。DDL/角色/schema 仍 Python 原 owner。helper 的 busy/基础设施失败/真实 pg_ctl 非零分别处理。
5. **ready-before-operation 能堵 late helper 空窗。** §3.3:104–108 的核心序列为：先 NB 取得原锁 → ready → Node 在取消/close 准入点检查后发送唯一操作。帧未发送时，晚调度旧 helper 没有预载 mutation，只能等待 EOF 退出；帧已发送时锁已持有，后续失联不得放弃真实 child/readback。旧 wait-owner 成功不是下一次操作授权，各 helper 仍自行取得锁。没有需要新增 durable ack/receipt/generation/registry 的理由。
6. **helper/child 非正常死亡的缺口被诚实设为 gate。** §3.3:106 明确如果 helper 先死、pg_ctl 存活且锁释放，则机制尚未通过，而非用瞬时 status=3 放行 reset。CLOEXEC 防止常驻 PG 继承 flock，与 finite helper 持到真实 child settle 的责任不冲突。没有把目前未证明的 WSL EOF 行为宣称为事实。
7. **正常关闭与载体失联有足够区分。** §9 正常 close 需要无收尾 failed、Linux 正常 exit0、完整本次控制/pipe 结果；stopped 和 flock 释放均不能独立证明工具成功或物理回收。§9.2:241 正面修复 child 已清空时 stop 误报成功的当前源码陷阱。PG stop/reset 仍在 kernel 关闭之后，失联则保留 unknown/数据、禁止重发。
8. **故障及重开语义保持小。** EOF 走原 application.aclose；sleep/network 中断不成为用户取消或任务寿命上限；外部 terminate/shutdown 是 crash，不是正常 settlement。重开原锁排他与当前 PG 事实观察，不增加执行恢复日志，不凭旧 process_id 重送输入。provider 前缀边界也未因重连/睡醒改变。
9. **TLS 是标准机制接缝而非新认证框架。** §10 将完整短命 public cert 经原可信 pipe 提供给每 launch 新 Session，exact origin 与证书校验各有责任，明确替换实际 Window/WebContents/retire 旧连接，而非热换 pin/clearAuthCache。原 browser 分区隔离、preview/null-Origin/SSE/下载/OAuth 路径仍在真实 G0-N/资格矩阵内；TLS 不冒充 HTTP client authorization，也没有把 V3 capability 送 renderer。官方 Electron session API确实说明 certificate verify proc 的 hostname/certificate 请求形状与 network-service cache；此处依赖约束正确。
10. **GUI/路径/维护条款可实施且与权限 owner 不冲突。** §6 以用户级服务提供受限根内 list/create/admission，最终 session 入场重验，不使用 active session 或路径清单作 authority；GUI 新建无需 terminal。UNC 只负责引用来源/OS handoff，Windows File 通过真实 bytes 导入，model export 与 Windows save 区分。Linux settings、PG、workspace、root-owned runtime 与 Windows UI home 分离。部署不依赖已禁用的 /mnt/c/interop，更新先正常关闭且不覆盖 live payload，普通卸载保数据；无全局强杀/自动 unregister。
11. **预算/验收没有沿用旧总数。** §12 50–95 合计与工作包算术相符，25–35% 后 63–129 是规划余量；A/B/三专题共用验收不重复相加，长期基础包维护与资格成本明确。§13 区分 native POSIX、真实 Windows WSL、已安装 GUI、provider/长程，不用历史通过数或 mock 冒充激活。冻结与完成状态分离一致。

## 非阻断实施提示

这些是实现契约的落地提醒；现候选的结果/失败合同已足以约束它们，不要求为此再扩架构。

- §3.3:106：帧发送前取消时，应关闭本次 stdin 并等 helper 退出，不能只 reject Node Promise 留一个持锁、等待 operation 的 helper；帧发送后取消只封新操作，继续持锁物理 join，保留可能已写/unknown。
- §9.1:232：“通过同一 Linux helper 执行原 --wait-owner”应实现为该包有限模块的独立 wait-owner 模式或复用原入口；不要先取得 helper 的 flock，再在内部调用另一个 `acquire_kernel_lock(wait=True)`，以免同进程不同 fd 自锁。等待模式与 NB mutation 模式是两种确切调用需求，不需要新 registry。
- §3.3/§9.2：锁 FD 的 CLOEXEC、helper SIGPIPE/BrokenPipe 处理、carrier termination 与 Linux helper/child 的真实生命周期要在 G0-C/P 同一故障用例观察。不要把“Node pipe 已 close”或“helper reply 读完”当作持锁 child settle 的替代证据。
- §10.2：workbench 的新 Session 与原 BrowserOwner 的独立普通网页 partitions 不应混为一个网络 allowlist；现 `browser-owner.ts:41–68` 已有独立 partitions，继续复用。StartupCover/IPC sender 的退休也必须实际覆盖，而非只更换证书字段。

## 证据边界

本轮读了当前 `desktop/src/{main,runtime,postgres,browser-owner,browser-policy,startup-cover}.ts`、`desktop_runtime.py`、terminal manager/environment、MCP/hooks/screenshot 相关生产入口与当前测试结构；重查 [Electron Session 官方说明](https://www.electronjs.org/docs/latest/api/session#sessetcertificateverifyprocproc)。WSL pipe/interop/生命周期依据仍为此前本轮已读的 [WSL boot/relay](https://wsl.dev/technical-documentation/boot-process/)、[interop](https://wsl.dev/technical-documentation/interop/) 与 [配置文档](https://learn.microsoft.com/en-us/windows/wsl/wsl-config)。这些证明 API/架构事实，不证明 Pulsara WSL 集成通过。

等待 root 闭合其他专题意见后的同一最终候选复核；本条“可冻结”针对 387 行候选实施语义，不预先批准后续新增语义。
