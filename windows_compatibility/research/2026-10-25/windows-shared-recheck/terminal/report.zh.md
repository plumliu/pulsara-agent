# Windows 共享修复 R2/R3 重验

2026-10-25，gpt-6.1-sol high。**本分工 R2/R3 实际通过，无剩余本次测试 owner。** 不单独据此授予整个 R0–R5、B5 激活或发布资格。后续正式深故障按 root 第二批安排，已有 G0 证据不冒作正式产品全矩阵。

## 来源与隔离

Windows/WSL 两 clone 开始时为干净 `0ea98e63d90043773f42bd9bb9bbb56951fca8b4`。实际运行来自 WSL ext4 `/home/plumwsl/workspaces/pulsara_agent` 的项目 `.venv`、Python3.12.15；`find_spec` 精确核为本 clone 的 `src/pulsara_agent/__init__.py`。运行时 dirty 为 root 的 alpha10 版本修改及 desktop 构建生成静态资源，完整原状保存在各 result，不是默认为干净或测旧安装 alpha9。

root 确认 Linux 资源/依赖写入完成后才执行。R3 使用 desktop 刚以当前 Program.cs/csc 重新构建的 helper，mtime UTC `2026-10-25T14:08:25.3644311Z`，`--version` 输出 `pulsara-interop 1`、exit0。本次 child 由仓库原 `tests/fixtures/windows_interop_child.cs` 经系统 csc x64 编译。PE 来源命令、路径和时间保存在 r3-pe-provenance.json，未添加代码/文档 SHA。

两个 runner 各创建独立短路径 ext4 0700 home 与 pytest base，`CI=true`。没有调用用户 PG、saved settings/provider、停止共享 Ubuntu、改变 interop/TUN/default 或写共享依赖。R2 未配置 Windows scope；R3 配置原可信 Ubuntu/plumwsl，生产 scope/Job/anchor/SCM relay 与各原 owner 全部原路。

## 结果

| 门 | 实际结果 | 原始证据 |
| --- | --- | --- |
| R2 五个指定文件 | 71 passed / 0 failed / 0 errors / 0 skipped，exit0，7.67秒 | r2.log、r2.xml、r2-result.json |
| R3 七个指定节点 | 10 passed / 0 failed / 0 errors / 0 skipped，exit0，17.38秒 | r3.log、r3.xml、r3-result.json |

R2 包含3项真实 POSIX zombie/reap/自然 fork-tail、原 hooks 解析/credential guard/permit/deadline、MCP close/repeated cancel、interop unknown inventory 与标准 Unix relay 取消/冷 task writer 结算。WSL 未必产生 Darwin 的 EPERM；本轮不声称实际在 WSL 观察到它，Darwin 实际反例仍由历史报告承担。

R3 包含 no-pipe Windows 尾部正常完成、Linux root 已退后的取消、输入/EOF（各 PIPE/PTY）、同 registry A/B、hook 正常与 timeout、真 SDK 单 call cancel 后继续 echo 再 session close、真 Supervisor 总 close。合计12个 scope，逐项核对：

- `physicalJoin=true`、joined reason `settled`、failure null；helper/diagnostic exit0；
- relay tasks/writers 均0，worker 不再存活，scope 目录已退休；
- MCP 单 call 取消不结束仍可工作的连接，最终原 close 才结算；A 取消不影响 B；
- 正常 no-pipe 与 hooks 依据原 body 的自然尾部/时间/success/no-cancel 断言，未用 TerminateJob 冒充正常成功。

scope trace 在 case 的 finally release_owner 之后输出，最终 cancelled 标记全部为 true；它可能是已经 joined 后的清理意图，不应据该末态标记反推正常路径用了取消。正常完成的证据是原断言在 release_owner 前已通过，原断言未修改。

## 失败保留与清理

第一次 csc 编译使用 slash 相对 source 参数，被旧编译器误解析为仓库根的 windows_interop_child.cs，得到 CS1504。该 preflight 失败和版本事实原样保留；pytest 尚未执行。改用 Resolve-Path 的绝对反斜杠 source 后构建成功，没有修改 fixture、生产代码或测试断言。runner 首次 PE 存在性断言产生的空私有目录 `.wsr-pe-plvzmkpz` 已逐层核实本用户/精确路径且空后删除；随后将前置检查放到创建目录前。

R2 的 `.wsr-term-85fek2q8` 与 R3 的 `.wsr-pe-ih731blg` 在原 pytest/owner 全部返回后才清理。最终 `privateRootRemoved=true`、`remainingExactOwned=[]`；R3 `remainingScopePaths=[]`。没有通过外层强杀或先删 home 制造产品结算成功。测试 child/helper 制品只位于本地 output/构建资源，不随本报告提交二进制。

只读差分审阅见 readonly-review.zh.md：本次三个生产差分未见具体正确性阻断，现 R1/R2/R3 窄范围合理；另列 anchor pin/send/bind、双观察者丢失及 installed 活动 scope 的正常/失联关闭等正式故障补证边界。它们不是本轮新发现的生产失败，也没有在此报告中冒称通过。

本分工没有修改生产代码或正式测试。R0/R1/R4/R5 与最终安装/GUI/provider 由其他 owner 汇总，本次不重跑历史整个 G0/前端/provider 矩阵。
