# 0ea98e63：共享差分与正式 interop 剩余边界只读核对

2026-10-25。本页未修改生产代码、测试或构建资源。R2 在 root 单独通知资源依赖写入结束后执行，其事实另见 r2-result.json；R3 等新 helper 才开跑。

## 三条生产差分

没有发现本次差分的具体正确性阻断，当前 R1/R2/R3 最小重验范围合理。

1. provider_dispatch：新分支仅在 actual binding 完全相等、head 严格前进时尝试归一化。现四层 DTO 的 dataclass equality 包含完整值（相关字段没有 compare=False）；replace 经原 constructor 重新验证，实际 payload/identity/context/permission/plan/closure/late outcome/replay 的变化仍无法相等。归一化的 fingerprint 是原 DTO 的既有派生值，不新增登记或 authority。返回 None 前只退休未安装资源，runner 清空 pending prospective 后 continue，仍在同一个已 accepted intent/turn 的原普通 prepare 循环；不再 accept、不提前增加 model_call_count。
2. retire_uninstalled：每项先撤销本地所有权再调用清理，nested finally 确保其余三项仍结算；外层 except 再进入时不会重复释放。15 个新 unit 覆盖 head-only、非前进 head、binding/完整内容漂移及四处独立清理失败；真实 PG 反例在 ROOT accept 后提交 child，要求 accept 仅一次、同 turn、safe-point 不留 handle、SYSTEM/tools 不变和 messages suffix。没有把随机 provider 通过当作确定性触发。
3. hooks/MCP：EPERM 只表示可能仍活；signal 拒绝不替代原执行/reader 结果。原 root wait、真实 group ESRCH 与 stream/worker/scope join 保留，未新增寿命 cap 或 PID 猜测。Windows scope 仍在原 credential guard/attempt/transport 内，MCP call cancel 不扩大为 connection close。新真实 zombie/reap、自然 fork-tail 与既有重复取消/close 路径覆盖主要受改边界。

一个非阻断的精确覆盖说明：R2 的 WSL 原生 zombie 测试不强行要求 EPERM（Darwin 已有实际 EPERM 证据）；因此不能写成“本轮 WSL 实际产生了 EPERM”。此外，新增 `_stderr` 的 signal PermissionError suppression 没有独立的“reader 原错误 + TERM EPERM”定向测试；源码分支保原 `_offer_failure`，并非发现它有实际错误。若需要逐 leaf 新分支证据，可补一个窄注入单元用例，无须重做 PE 矩阵。

## G0 历史证据不能直接升级为正式完整故障资格

正式 Phase B 已有 28 case/38 scopes：真实 terminal PIPE/PTY、8槽/AB、reader/watcher.start 回滚、helper owner/monitor/control EOF/anchor 单故障、预约内3取消门、relay3取消门、MCP SDK/Supervisor close、hooks、environment、HANDLE 常驻回收。此次 R3 的10项是对受改 hooks/MCP 及基本 terminal 的窄回归，不替代这份历史正式矩阵，也不表示所有故障本次重跑。

在今后要宣称完整 §5/§9 跨域故障已闭合之前，最小必要的补证按独立责任边界分三组；它们不是当前三个共享差分的新失败，不应据此把本次 R0–R5 无限制扩大：

| 最小补证 | 为何既有证据还不足 | 必须观察的结果 |
| --- | --- | --- |
| 正式 anchor 的 pin/send/bind 交接取消：pin 后尚未 SCM send，以及 receiver 已拿 FD 尚未发布 relay 两门 | G0 R4 的四门是真实标准 API 研究 fixture；正式 test_cancel_inside_original_launch_reservation 只明确 before-bootstrap/descriptor-receive/before-command，不能把 recv 等待等同 pin 两侧全部窗口 | 同一原 launch reservation 内取消；不发布实际任务、精确双域 join、无预约/FD/worker 漏失；socket 仅在原 caller 已结算后退休 |
| 同一真实 scope 的 helper 和 monitor 都失联，一项即可 | 现正式 critical-loss 四项各有另一精确观察者；unit UnjoinedScope 证明 inventory 合同但不是实际两观察者丢失 | 无完整 joined 帧时保 unknown/unjoined，原 owner/close failure 重入不变成功、不释放为已知 clean。测试外部预持 exact HANDLE 仅用于清理，不把外部清理倒填产品成功 |
| 原 installed Application/main 的活动 scope 关闭：正常 Quit 与宿主强退/控制 EOF 两项；另以 MCP pending call、hook 执行各选一个 critical-loss 原 owner 场景 | 正式底层 scope/terminal critical loss 与 G0 kernel/main 的 EOF/PG 顺序分别已有证据；真实 writer-loss 候选有 Core + Windows 任务，但不是完整 installed main Quit。MCP/hooks 正式真 PE 正常/timeout close 也不等于其 critical loss 的原 transport/permit 路径 | 活动 Windows/Linux task、MCP/hook owner 先物理结算，再 kernel clean stopped/PG stop；宿主死则只判断 Linux 原收尾与 unknown、不要求死宿主执行 main。单次 MCP 工具不自动重放，原连接失败/关闭与 remote unknown 分开，真实 close failure 必须阻止假 clean |

完整 distro 终止与 PG 自行恢复已有独立可丢弃 distro G0 证据；研究 fixture 未包含正式任务 scope 时，不能把它写成正式 scope 的连续 HANDLE/pidfd 资格。无需为了这次共享差分重建该环境；只有今后明确验证“整个执行域丢失时活动正式 scope”的产品合同才再用唯一临时 distro，不能终止共享 Ubuntu。活动任务睡眠、任意主动逃逸进程、所有 owner×全部 gate 的笛卡尔积不属于这里建议的最小补证。

R2 已实际 71 passed、0 skip、exit0；R3 尚待新 helper 的就绪通知。本页没有将待测补证写成通过或已发现生产 bug。
