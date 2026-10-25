# Windows 共享修复重验及 Phase B 收口工作表

启动基线：Windows 与 WSL 两份 codex/windows-g0 均为 0ea98e63，启动时干净。用户授权继续复用三位 agent；保持 Ubuntu/plumwsl、interop、automount、Hiddify TUN 和用户数据。本表为执行计划，不是通过记录。

## 第一批：最小差分重验

| 项目 | 执行 owner | 本轮待取得证据 |
| --- | --- | --- |
| R0 | desktop + root | 新 alpha.10 两仓版本、串行 Linux verify/payload、Windows helper/壳、最终安装来源 |
| R1 | backend | 27 项确定性 ROOT/真实 PG/prefix；私有 cluster 身份和清理 |
| R2 | terminal | 71 项 hooks/MCP/POSIX，实际结算及取消失败原样保留 |
| R3 | terminal | 10 项真实 PE；正常自然完成、取消及双域 join、连接隔离 |
| R4 | desktop | Windows 定向 Node 与真实 startup cover，未执行项不计通过 |
| R5 | backend + root | 新 installed Python context/last_n/mailbox；GUI、保数据升级、隐藏草稿、Quit/重开 |

R1–R4 通过后构建最终 NSIS。R0–R5 完成后记录新 Windows 基线；旧 alpha.9 与 Mac 证据分别保留。新增生产修复须按影响扩大验证，不能削弱原断言。

## 第二批：Phase B 尚缺证据逐项核对

| 主 spec 范围 | 已有证据 | 后续工作/限制 |
| --- | --- | --- |
| B1 接入/部署 | 正常已有 WSL 接入、离线资源及更新/卸载保数据；部分故障注入 | 把生产 setup 与 G0 研究证据逐项区分，补正式错误路径。WSL 未安装/需重启须独立测试环境，不破坏日用 WSL |
| B2 控制/PG | 正式有限 helper、双锁、Runtime 定向测试及真实正常 PG | 核对 G0 深故障与正式生产矩阵，优先遗漏的提交/EOF/异常死亡边界；不用瞬时 status 代替物理锁 |
| B2/B4 睡眠恢复 | alpha.9 闲置睡眠→新消息→正常 Quit | 活动任务/MCP 连接场景另做；手动硬件睡眠需现场协助，不用进程时钟注入冒充 |
| B3 文件/工作区/TLS | 正式中文目录、选择/拖放、HTML/PNG/PDF及下载 | 逐项核对失败结算、旧证书/session 退休的正式集成证据，按缺口补测 |
| B4 输入/显示 | 真实中文 IME、托盘/贴靠；100%→150% | 同一窗口返回100%及真实AltGr布局仍缺。当前只有外屏启用，不能用旧重建窗口代替跨屏返回 |
| B5 共享回归 | 0ea98e63 所选 macOS arm64 矩阵已通过 | 保留 Mac 睡眠/IME、x64/分发边界。原生 Ubuntu 按用户要求延期，不冒称通过 |
| B5 发布/声明 | 本地 Windows NSIS、版本/许可检查 | 精确核验完整制品/构建矩阵和文档；本地验收不自动变成对外发布或 main 合并 |

后续按实际证据消减此表缺口，不把所有历史“尚未”段落机械当作当前缺项，也不以新文档替代执行。

用户现场补充：没有备用 Windows 机器或虚拟机；若删除当前 Ubuntu 不合理，则先不做该项。当前共享 Ubuntu 承载用户配置、会话、数据库、工程及开发环境，且删除发行版不能代替“WSL engine 未安装”资格。因此真实 WSL 未安装／需重启的独立环境项明确延期，不删除、重建或终止日用发行版；不影响其余本机验证。
