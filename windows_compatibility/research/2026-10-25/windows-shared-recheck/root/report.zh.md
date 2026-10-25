# 0ea98e63 → alpha.10：Windows 共享修复最小重验基线

2026-10-25，Windows Electron + Ubuntu WSL2/plumwsl，interop、automount 和 Hiddify TUN 保持开启。

**R0–R5 全部通过。** 这是本轮共享修复的 Windows 最小回归基线，不是完整 Phase B/B5 激活。Windows 与 WSL 均从干净的 `0ea98e63d90043773f42bd9bb9bbb56951fca8b4` 开始；本轮源码差分为七个原版本文件同步到 alpha.10，以及 `mise.toml` 的 Windows 配方相对路径修正。没有改写三个共享修复的产品逻辑。

## 分项事实

| 项目 | 实测与证据 |
| --- | --- |
| R0 | WSL 原 desktop:verify、payload、Windows resources/build/package 全部退出 0；实际安装 alpha.10。随包 Python -I 和三个产品文件完整 bytes 与 WSL 源码一致。详见 ../desktop/report.zh.md。 |
| R1 | 27 passed、0 skip；真实私有 PG 确定性触发 accept 后 head-only 前进，完整漂移负例与前缀连续保留。详见 ../backend/report.zh.md。 |
| R2 | 71 passed、0 skip；WSL POSIX/hooks/MCP/interop 定向回归。未声称 WSL 实际产生 Darwin EPERM。详见 ../terminal/report.zh.md。 |
| R3 | 10 passed、0 skip，12 scopes 物理 join、精确退休；实际 PE、PIPE/PTY、隔离、hooks/MCP close。finally release 的 cancelled 标记不冒充正常路径主动取消。 |
| R4 | Windows 54 项定向 Node、10 项 Windows main 及实际 Electron startup 全部通过；WSL desktop:verify 的 105 项另列，不相加宣称独立覆盖。 |
| R5 context | 新安装 Python -I，8 次实际 provider 调用；两个 PENDING、唯一 mailbox、worker ROOT/MIDFLIGHT 与原场景/prefix 断言通过。随机调度未证明新 post-accept race 命中，该窗口由 R1 证明。 |
| R5 保数据 | alpha.9 → alpha.10 实际 NSIS /S 更新 exit 0；原模型设置、两个会话/5 turns/32 entries、工作区及四个文件严格保持，正式 PG system_identifier 7700523439330484737 保持。 |
| R5 GUI | 正式界面创建中文空格目录和独立会话；真实 terminal 返回 marker/exit 0；Ctrl+N 新会话、Ctrl+K 搜索、中文草稿隐藏后恢复、正常 Quit、冷重开和最终 Quit 通过。 |

## GUI 的确切范围

新目录 `/home/plumwsl/workspaces/Windows 共享修复 alpha10 验收`，会话 `session:uctdxsxom55pycdl5l3dvg5owu`。用户消息只发送一次，唯一 QUEUE_PROMPT 被消费并最终 COMPLETED。模型额外做了一次只读 `find_files(AGENTS.md)`，随后 terminal 执行 printf marker 加退出码回显；不是“总共只有一个工具调用”。后台首次观察器额外要求总 tool 数恰为 1 的失败已保留，独立事实核对见 backend/gui-verified.json。

窗口 19664142 留下未发送中文草稿，点击关闭后窗口列表无 Pulsara、原进程仍在；再次启动原 exe 恢复同一窗口和草稿。这证明隐藏/单实例恢复，不冒充本轮实际托盘菜单点击。Ctrl+K 显示三个实际会话，Escape 返回后草稿仍在。

首次 Ctrl+Q 后 22:24:54 应用进程为空、正式 pg_ctl status 3；维护检查 22:25:04 exit 0。冷启动窗口 39915048、HTTPS 41377 显示三个会话及一组模型；打开新会话后工作区、完整回复、模型仍在，输入框为空（草稿仅驻内存的合同）。再次 Ctrl+Q 后初次快照尚在收尾，保留于 alpha10-cold-quit.json；22:26:17 的 settled 快照确认应用进程为空、正式 PG status 3。全程未强杀应用或人工停止正式 PG。

GUI 自动化遇到启动页窗口被主窗口替换、无输入几何、检测到用户输入，均重新观察后继续；未把自动化错误称产品缺陷。选择目录/会话结果均由后续真实状态核对。没有使用合成中文输入证明硬件输入法资格。

## 构建失败与 uv 处理

Windows 资源准备最初因构建编排本身运行于仓库 .venv，而 uv 同时替换该解释器产生占用；换成独立的 `uv python find --system` Python 后解除。另一次真实 mise/cmd 配方把斜杠相对 exe 解析为 `.venv`，仅将该 Windows 命令改为反斜杠路径后原配方通过。原失败均归档。不把文件占用称安全软件拦截，不关闭系统防护或删除用户环境；依赖/资源更新串行结束后才放行测试。

## 清理与剩余资格

本轮私有 PG、UUID 测试库、临时 provider home、精确 owned PE scopes 均由各原 owner 结算清理。正式模型、原会话及新 GUI 验收会话保留供复核；新 GUI 目录未写业务文件。正式应用最终关闭，PG 停止。没有新增临时 OAuth 连接。

用户无备用 Windows/VM，并明确不为验收删除当前 Ubuntu；真实“WSL 未安装／需重启”延期，不能用 mock 代替。其余 Phase B 故障边界按下一批 owner 实验继续，活动任务睡眠、实际 AltGr/其他硬件与签名分发等仍按完整矩阵列资格。原生 Ubuntu 验收按用户要求延期；不能由 WSL 结果替代。本地候选 Authenticode 为 NotSigned，没有打 tag 或公开发布。
