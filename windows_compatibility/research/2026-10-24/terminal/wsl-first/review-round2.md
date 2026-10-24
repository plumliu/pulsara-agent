# WSL-only 根规格最终语义候选复核：terminal round 2

日期：2026-10-24。对象：`PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md` 当前 394 行候选（文首仍为待交叉审阅）。承接本目录 `review-round1.md` 的 387 行全文审阅，本轮重读完整候选并逐项核对新增段与原合同。

**最终结论：可冻结，无新增阻断；同意 root 对这份 394 行候选冻结。** 此结论批准实施设计，仍未实施 A/B、执行 G0-W 或激活 Windows 支持。

本轮新增/精确化事项均与全文相容：

1. §3.3:107 明确取消结束 stdin 并 join helper，真实 operation 发送点再检查准入，发帧后仍持锁结算/readback；§9.1:235 将 wait-owner 定为原入口或独立模式，排除先持 helper 锁再嵌套等待的自锁。没有引入持久 ack、操作 registry 或新生命周期 owner。实现时 operation 帧发出前的任意取消阶段都应收回本次管道/等待 helper，不能仅关闭 ready 前一个窗口。
2. §10.2:277 将短命 certificate 定为 process/session lifetime，而非 wall-clock 到期强制任务终止、kernel 重启或 epoch 更换；精确 pin/时间变化/长期连接均留在真实 G0-N 验证。它与 §2.2 的长期可用性及 provider prefix 连续性一致，没有为 TLS 暗加任务总时长。
3. §8.3:221 区分 native Ubuntu x64 guest 制品与 Windows Electron/NSIS 目标，Windows 构建用自己的 uv Python，不冒充 Windows backend。固定发布制品名、版本完整性、mise cache 输入与原手动发布权限一起更新；publish never 与本轮不发布/不 bump/tag 的授权边界一致。未挤占现有 Mac/Linux 单一路径。
4. §3.2:92、§10.3:285–289 将 OAuth 系统浏览器缺口落到真实已有 async `open_browser` 注入点，贯穿 DesktopApplication → KernelHostCore.production/ctor → LocalMcpManagementService。已重读源码：`application.py:91` 创建 production core，`host.py:7877/7967` 当前尚未传该 callback，`mcp_management.py:329–367` 已有 callback 并传入原 McpOAuthManager，因此不是捏造 SDK/现生产能力。
5. OAuth 使用原可信 JSONL 的有限请求/结果，当前 live launch 与 requestId 相关，Node 校验 HTTP(S)/无 userinfo 后调用受限 shell.openExternal；listener 先安装，取消/retire/管道丢失收回 pending/listener，无自动重开。已发 host 动作不声称可回滚；PKCE/state/issuer/token 仍归原 SDK/OAuth owner。它不是任意反向执行 RPC，也不开放 interop、传 code/token/client secret 或改变内部 V3 transport。
6. 新 OAuth 与构建条款都处于 B，A 的 Node/资源/PG 接缝范围未扩成原全库抽取。§4、§11、§13 的 G0/真实安装/provider/长程/受影响 POSIX 回归和激活前置仍保留；预算为当前统一 50–95、含余量 63–129 人日，未叠回旧 native 预算。

未发现正文其他条款与这些修订冲突。原 round 1 的 scope、PG unknown、carrier close/stopped/exit0/锁区别、TLS fresh Session/实际 WebContents retire、Linux-backed workspace、维护保数据与禁止 fallback 结论保持。

本轮仅个人审阅记录写入；未改根规格/生产，未运行测试、G0、provider、构建或数据库。下一步可只更新文首与 §15 的冻结状态/审阅事实；若再增加执行语义，应通知相应专题复核。

## 同日最终文字确认

已复核仍为 394 行的最终候选两处精确化：§3.3 明确 operation 帧写出前任何取消（含已 ready 未发帧）均结束 stdin 并等 helper 退出；§10.3 只承诺 Node 在打开 sink 拒绝本地已知 close/retire 的 late 请求，Linux 已发请求后取消不冒称可撤销另一 event loop 尚未知的排队/打开动作，原 flow/pending 收回并拒晚结果，不新增 cancel 协议或 registry。两处均符合真实不可逆点和跨域取消边界，无新矛盾。

**最终确认：这份 394 行候选可冻结，无阻断。** 此后仅更新冻结元数据/审阅事实即可；未重做研究或运行测试/G0。
