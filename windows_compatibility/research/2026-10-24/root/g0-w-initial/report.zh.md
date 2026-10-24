# Windows 开发环境与首轮 G0-W 实测

日期：2026-10-24。本轮已准备现有 `Ubuntu` / `plumwsl` 开发环境，执行真实 Windows→WSL 实验，**未进入 Phase B，完整 G0-W 未通过**。

测试源码为本地分支 `codex/windows-g0` 的 `da397a2bb701c6371ed19155f66328334684bf38`，包含用户此前授权的 Ubuntu 版本准入取消及共享 WSL/interop 规范修订。该提交仅本地保存，未推送。后续报告提交不改变受测生产代码。

root 负责环境、源码同步、资源构建与回归；三位用户明确授权的 `gpt-6.1-sol / high` agents 分别负责 terminal、backend、desktop。探针位于 `output/windows-g0/`，不被生产 import，不构成 WSL 产品适配、安装器或新的完成 authority。

## 环境与开发入口

| 项目 | 实际值 |
| --- | --- |
| Windows | Windows 11 家庭版 25H2 x64，build 26200.9711，普通用户 |
| WSL | 2.7.11.0；kernel 6.18.33.2-microsoft-standard-WSL2 |
| 已有发行版/用户 | Ubuntu 26.04.1 LTS x86_64；plumwsl，UID/GID1000，home `/home/plumwsl` |
| Linux 仓库 | `/home/plumwsl/workspaces/pulsara_agent`，ext4 `/dev/sdd` |
| Windows 仓库 | `C:\python_workspace\pulsara-agent`，独立 clone、依赖与输出 |
| Windows 工具 | mise 2026.10.12，Node 22.23.3 win32/x64，npm 10.9.9，Electron 44.9.0 |
| Linux 工具 | mise 2026.10.15，Node 22.23.3，uv 0.13.4，protoc 34.0，项目/随包 Python 3.12.15 |
| 资源 | PG17.9、vector0.8.1、pg_search0.26.1、rg15.2.0、锁定 Playwright1.63.0/Chromium |
| 互操作 | interop、Windows PATH、automount、mountFsTab 保留开启；Windows PE 正测通过 |

Windows mise 由 WinGet 用户级安装，未替换已有全局 Node24；新终端可识别 mise，当前进程也可直接使用 WinGet 安装目录中的 `mise.exe`。Linux mise 位于 `~/.local/bin/mise`；本轮只在命令进程中设置 PATH，没有改 `.bashrc` 或 WSL 配置。

从 Windows 进入开发环境：

```cmd
wsl -d Ubuntu -u plumwsl --cd /home/plumwsl/workspaces/pulsara_agent --exec /bin/bash -l
```

在该 Linux shell 运行：

```bash
export PATH="$HOME/.local/bin:$PATH"
mise exec -- node --version
mise exec -- uv --version
.venv/bin/python --version
mise run --jobs 1 desktop:verify
```

Linux clone 经 Git 从 Windows 本地仓库克隆，不复制 venv/node_modules/资源；`origin` 为原 GitHub URL，`windows-source` 指向 `/mnt/c/python_workspace/pulsara-agent`，供未推送的本地提交同步。两边通过实际 Git HEAD 核对，不以分支名或版本号代替源码一致性。

## 已完成验证

| 验证 | 实际结果 | 范围 |
| --- | --- | --- |
| Windows npm ci、tsc、Electron版本 | 成功 | 仅开发宿主，不运行生产 main/package |
| Windows 指定纯测试 | 5/5 | browser-policy / oauth-return / ui-preferences |
| Linux `mise run --jobs 1 desktop:verify` | 退出0，约190秒 | 新资源、fresh renderer/wheel、Node46/46、真实资源搬移/版本检查 |
| Linux 聚焦 Python/构建缓存 | 39通过、Ruff通过 | desktop runtime/launch/release、Ubuntu准入和真实mise/uv缓存测试 |
| Linux terminal/MCP | 94通过 | environment/output、process termination、Host lifetime、cognitive subtraction、MCP transport/SDK probe |
| Linux hooks | 45通过 | 当前hook subsystem测试 |
| Windows Node→WSL pipe | 锁定Node/Python下通过 | literal argv、中文/逐字节UTF8、JSONL、1MiB输入/2MiB stderr、exit0/1/7、EOF |
| 随包 runtime start前EOF | 通过 | module来源为随包site-packages，未触DB/settings，退出0、wait-owner/锁释放、自有home清理 |
| 真实 PG/检索 | 已覆盖子项通过 | clean-v0、中文/英文/代码BM25+snippet/vector、错实例/版本拒绝保数据、复用/重开/精确reset |
| 隔离 TLS/Session | 12/12 | fetch/SSE、完整cert pin、精确origin、旧cert/rebind拒绝、新Session+新WebContents、sandbox、下载字节、过期cert精确pin |
| 实际 BrowserOwner | 11/11行为断言 | Windows普通browser隔离；测试profile清理另有失败，见下文 |
| Explorer UNC | 图形新建/打开成功 | 中文空格目录在guest观察为同一canonical ext4目标，截图保留 |

Node/Python 测试没有增加 skip/xfail 或削弱产品断言。PG 重置后的 status4 是目录不存在，与现存停库的 status3 区分；原错误测试预期和修正记录均保留。以上结果只覆盖对应 WSL/Windows子场景，**不替代原生 Ubuntu x64/arm64 或 Ubuntu GUI资格**。

## 关键未通过项：普通 Windows 后代的取消

terminal 用 Windows PowerShell 的 .NET `ProcessStartInfo` 启动普通非 detached Node PE 子进程，排除了 Node父进程自身 libuv Job 自动杀子代的混淆。锁定 Windows Node22.23.3、Linux Python3.12.15 下，至少两次独立观察到：

1. 对本次 Linux 进程组发 SIGTERM，Linux `Popen.wait()` 返回 -15、Linux root 退出，Windows carrier 随后 close0。
2. 一秒后的 Windows CIM 仍找到普通 Windows子进程，PID、CreationDate及唯一实验命令标记对应；重复实验还确认 Windows父进程已不存在。
3. 该子进程继续完成探针工作后自行退出，随后 CIM 不再找到它；自有 Linux根也已不存在。

因此，**当前 Linux PGID/root/pipe 观察不能单独证明普通互操作 Windows后代已取消/结算**。这不是仅刻意 detached/setsid 逃逸的反例。载体丢失也有后代继续运行的观察。G0-W-C 的该要求未通过，不能通过关闭interop、杀共享发行版、自动重发或宣称整个Windows树已回收来放行。下一步需要在G0范围研究可复用的Windows所有权/结算机制并明确实际支持范围，尚未实现生产修补。

## 其他闸门状态

| 门 | 首轮结论 | 仍缺 |
| --- | --- | --- |
| D 接入/环境 | 基础环境、同提交双clone、非root/ext4/互操作及资源准备已有证据 | 正式目标接纳/默认值变化、私有payload安装的离线/磁盘满/失败重开等完整矩阵 |
| C 控制/生命周期 | 基础pipe/EOF通过；普通Windows后代取消存在已复现缺口 | Windows过程所有权/关闭结算、真实kernel运行中故障、睡眠/恢复等 |
| P PG/资源 | 常规PG/检索/reset、helper EOF/busy/丢reply子项通过 | helper自身死亡在真实pg_ctl各阶段的物理结算/维护准入仍未资格化 |
| F 图形/文件 | Explorer子项通过 | 无session的Electron有限目录GUI、挂载/权限/替换/取消/丢回复、实际File导入与保存失败/unknown |
| N 网络/身份 | 标准TLS/Session扩展点12子项通过 | 生产路由/preview/上传下载retire闭包、长期连接/时间变化、VPN/firewall/休眠、真实OAuth |

P门另保留两个准确边界：`pg_ctl -t0`故障输入返回code1/startedHere=false，随后status已running，说明本次结果不能当作“未启动”；该窗口不代表正常等待参数下的发生频率。helper SIGKILL + 物理child在exec前SIGSTOP实验只观察到锁释放、child带待处理SIGHUP、继续后退出且没有晚启动；它不证明真实pg_ctl其他fork/start阶段安全，`helperDeathMechanismPassed`保持null。

## 操作、证据与清理

root通过明确 `wsl -d Ubuntu -u root` 仅安装本次缺失OS构建依赖及锁定Playwright要求的OS库，普通用户执行所有uv/npm/build/initdb。APT和Playwright维护日志如实记录新增包及相关依赖更新；没有替换系统Python/Node/PG、改sudoers、保存密码、读取生产settings或调用provider。用户原bash/其他工作保持，不terminate/shutdown/unregister共享发行版。

root原始日志在 `output/windows-g0/root/20261024-environment/`；三个专题的原始证据分别在 `output/windows-g0/{terminal/20261024-interop,backend/20261024-2323,desktop/20261024-host}/`。本目录及各专题 `g0-w-initial/` 归档精选小型文本、结果和探针；原始output及大资源仍忽略，不作为运行时依赖，不生成代码/文档/证据hash。

归档的PG helper仅补充了“先设置隔离home再import”的局部E402说明，不改变执行语义；原始执行文件仍在output。root验证日志为明确标注的末35行摘录，完整编译/安装日志继续原样留在output，不把摘录冒充完整原始日志。

PG/backend的五个确切临时home和对应PG均已核实清理。TLS fixture/carrier正常退出，临时private key加载后删除，没有把私钥交host或日志。Explorer创建的空目录 `/home/plumwsl/workspaces/中文 工程 G0-20261024`作为明确实验产物保留，其本次窗口已关闭。

BrowserOwner原smoke在Electron仍占用profile时遇EPERM；行为11/11不能算干净收尾通过。退出后的PowerShell删除操作又被自动审批拒绝，返回理由仅 `blocked by policy`，未执行、未换工具绕过。保留路径为 `C:\Users\plumlocal\AppData\Local\Temp\pulsara-owner-test-p7z6Dx`，用户可在确认测试已退出后手动清理该精确目录。此残留不掩盖为全部cleanup通过。

**本轮交付为开发环境就绪与首轮G0证据/缺口。Windows支持、完整G0和Phase B均未批准或实施。**
