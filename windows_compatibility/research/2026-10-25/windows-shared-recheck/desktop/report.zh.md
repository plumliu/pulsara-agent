# Windows shared recheck：desktop R0 / R4

本轮基线为两个 clean clone 的 `0ea98e63d90043773f42bd9bb9bbb56951fca8b4`。执行位置为 Windows `C:/python_workspace/pulsara-agent` 与 WSL Ubuntu/plumwsl 的 `/home/plumwsl/workspaces/pulsara_agent`。WSL 资源构建只服务本次 Windows payload，不代表原生 Ubuntu 应用资格。初始状态见 `source-before.json`。

## 来源与构建

按既有授权精确同步七个 canonical 版本位置至 Python `0.1.0a10` / npm `0.1.0-alpha.10`；只改 own package 版本，不全局替换依赖。两 clone 精确复制这些版本文件，原 release check 通过。未提交、tag 或发布。

使用原任务图串行执行 `mise run --jobs 1 desktop:verify` → `desktop:wsl-payload`；verify exit0，105/105 Node、原资源搬移 smoke（PostgreSQL/vector/pg_search/Python/Chromium）及 bundled release check 通过。payload exit0，两份本次制品传入 Windows；没有复用旧 alpha9 payload。R1/R2 的测试 owner 在全部 Linux 资源/venv 写入结束后才放行。Windows helper 重新编译及 `--version` 验证后才放行 R3。此后未重写 Linux 资源。

Windows 实际配方暴露并修正一行：mise 的 inline shell 实际为 `cmd /c`，原 `.venv/Scripts/python.exe ...` 被 cmd 解析为 `.venv` 命令失败。Windows 专属 task 改为 TOML literal `'.venv\Scripts\python.exe desktop\scripts\prepare_windows_resources.py'`，精确同步两 clone；原 `mise desktop:windows-resources` 入口随后 exit0。这不改 Linux task、依赖、guest 三处共享修复或产品协议。见 `mise-windows-shell.*`、`windows-resources-final.*`（修正前）和 `windows-resources-cmd-path-fixed.*`（修正后）。

另一独立失败来自观察编排：首次用待升级的本仓 `.venv` Python 执行日志 recorder，uv bootstrap 从 3.12.13 更新到锁定的 3.12.15 时因父进程占用目录拒绝移除。第二次 `uv python find` 仍默认返回同 venv，也保留失败。明确使用 `uv python find --system 3.12.15` 找到的独立 uv-managed 解释器编排后，原 uv bootstrap 成功。未盲删 venv、未结束外部进程、未绕过锁。原失败见 `windows-resources-first.*`、`windows-resources-retry.*`；该占用原因与 cmd 路径原因分别记录。

## 跨平台 static 审阅

Linux renderer 生成 hash 文件名变化。完整读取原 HEAD 与本次 87 份 sourcemap 的 1336 个源项，CRLF→LF 后完整内容相同；不是记录 source hash，也不把归一后相等冒称生成 JS bytes 相等。新 wheel/payload 保留本轮实际生成物；在 payload 已写完后，只恢复本次造成的 WSL tracked static 并逐项删除本次生成的新文件，其他 dirty 不动。Windows tracked static 未覆盖。见 `static-review-all.json` / `static-cleanup.json`。初次审阅脚本误假定 HTML 使用 `./assets` 而实际 `/assets` 的失败与修正前源码保留。

## R4

| 检查 | 实际结果 |
| --- | --- |
| Windows TypeScript build | exit0 |
| 清单的九个 Node 文件 | 54 passed / 0 failed / 0 skipped |
| main 的 `^Windows ` 选择 | 10 passed / 0 failed / 0 skipped；不代表全部 main suite |
| 真实 Electron StartupCover | exit0，原 visible fade / hidden 直接结算 / IPC gate 断言通过 |

未改测试断言、未增加 timeout 或 skip。原 stdout、实际命令、开始结束时间与退出码见对应 `windows-*.json/.log`。WSL 105 与 Windows 54/10 有重叠，不能相加称独立总数。

`guest-provenance.py` 在 build resources 用目标 Python `-I` 核实际 `pulsara_agent.__file__`、runtime/metadata alpha10，再把 provider_dispatch.py、hooks/executor.py、mcp/sdk_facade.py 与 ext4 clone 对应源文件完整 bytes 比较；三项均通过。安装后可同脚本指向正式 alpha10 root，另写结果，不加载原 settings/PG，也不安装或启动应用。

## 尚待结算

最终 NSIS 编译、实际 unpacked tar/asar 来源核验正在本 owner 内串行结算；结果将追加。正式安装、保数据、installed provider context 和 GUI/Quit/重开由 root 与其他 owner 接续，不能由构建结果代替。

本轮保持 interop/TUN，未操作正式 PG/settings 或共享 Ubuntu 配置。未重做硬件睡眠/DPI/IME或原生平台全矩阵；WSL 未安装/需重启的真实环境因无额外机器/VM且不删除用户 Ubuntu而延期，不记通过。不激活 B5。

## 最终制品结算（22:13 本地）

原 `mise run --jobs 1 desktop:windows-package` 物理 exit0，106.75 秒。安装器 `desktop/release/Pulsara-0.1.0-alpha.10-windows-x64.exe` 为 **536960938 bytes**，blockmap 559749 bytes；旧候选未覆盖。实际 Authenticode 为 `NotSigned`，仅本地候选，不代表签名分发资格。

唯一总来源入口 `artifact-provenance.json`：实际 win-unpacked 内 tar 1595320320 bytes，manifest 为 alpha10 / linux-x86_64，完整内容 SHA256 `b8899807e99b8a44be7cc15a49516b521f155a1efdf2499b1312932058427a6c` 与 manifest 相等（只用于本次不可变 payload 边界）；三个实际 tar member 与 ext4 clone 当前源码完整 bytes 相等，大小依次 298060 / 28885 / 62496。Windows checkout 另只做 CRLF 归一比对，不把该结果冒充原始 bytes 等于 Linux。实际 tar static 等于本轮 build resources；WSL launch manifest 精确为 execution_domain=wsl。随包 helper17920 bytes / --version成功。

实际 `app.asar` 的 alpha10 版本、startup html/js/css 完整 bytes 与当前源相同，友好错误/details 和原 Retry/logs 保留；见 `package-ui-audit.json`。这些检查不执行 installer 或启动 Pulsara。READY 已交 root，后续正常安装及 R5 由 root/对应 owner 记录。上节“尚待结算”是打包前历史阶段，此处已关闭 NSIS/制品核验；正式安装/GUI/provider不由本报告推定通过。

## 正式部署来源核验（22:15 本地）

root 完成 NSIS/setup 并报告正式 HTTPS40121 ready 后，本 owner 运行同一只读 `guest-provenance.py` 指向 `/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10`。实际目标 Python `-I` 加载该目录 site-packages 的 Pulsara，runtime/metadata 都为 `0.1.0a10`；三处修复文件与 ext4 同 HEAD 源码完整 bytes 均相等，原始大小仍为 298060 / 28885 / 62496。命令物理 exit0；见 `installed-provenance.json` 与 `installed-provenance-run.json`。未启动应用、读取 settings/PG 或变更正式数据。该结果只补 R0 的最终安装来源，R5 provider/GUI闭环另由相应 owner 验收。
