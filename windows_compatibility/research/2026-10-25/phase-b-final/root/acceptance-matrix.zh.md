# Phase B 剩余验收工作表（本批开始前的分工快照）

2026-10-25。Windows 差分基线为 `0ea98e63` + alpha.10 版本/Windows mise 路径修正，R0–R5 已通过。下表保留本批开始前的分工和当时待办，不作为最终状态；执行后所选本机验收已完成，最终事实以 [report.zh.md](report.zh.md) 为准。interop、自动挂载与 Hiddify TUN 始终保持。

| 主规范条目 | 已有正式证据 | 本轮剩余工作/状态 |
| --- | --- | --- |
| B1 目标及离线部署 | 原真实 setup 五项失败保数据、正式 GUI 选定 Ubuntu/plumwsl、alpha.6/7/9/10 安装/更新及 alpha.7 卸载重装保数据 | desktop 补真实缺目标/用户/root 拒绝、同版本错误摘要及生产写点 ENOSPC 有限验证；不把有限注入当整盘满 |
| WSL 未安装/需重启 | 无该真实环境 | 用户无备用 Windows/VM，明确不删除日用 Ubuntu，延期；不以 mock 补资格 |
| B2 控制/PG | 原 Runtime 三 pipe/帧校验、实际 TS owner→PG 正常 start/reuse/stop/reset、锁与正常维护拒入 | backend 补生产 helper 在提交前/后、start/stop/initdb 失联的真实 mutator/FD/unknown/readback 边界 |
| B2 interop/原执行 owners | 旧正式 28 cases/38 scopes；本轮 R3 10 cases/12 scopes；共享 R2 71 项 | terminal 补 anchor pin/send/bind 取消、双观察者失联、MCP/hook critical loss、隔离 installed main 活动范围退出 |
| B2/B3 网络与文件退休 | 正式 BackendSession SSE/pending/download、readiness/错误 pin；实际文件选择/拖放/预览/保存 | desktop 补原 BackendSession 自然证书过期/重绑定和原 upload/request owners 组合退休；分清 SDK/owner 集成与实际 GUI 全链 |
| B3 图形工作区 | alpha.6 原用户级 picker 中文目录/会话，实际附件副本/下载 bytes 与权限；alpha.10 新中文空格目录与会话/terminal 成功 | 保留原替换/挂载/路径失败证据；若生产相关逻辑不变，不重复整套图像/附件长程 |
| B4 Windows 壳 | 正式 NSIS、启动、Ctrl+N/K、隐藏/单实例恢复、正常 Quit/冷重开；alpha.7 用户现场 IME/tray/snap；alpha.9 修后快捷键实证 | 同一原窗口 100%→150%→100% 完整往返尚缺；用户现出差仅内屏，双屏延期。真实 AltGr 布局仍未提供，物理右 Alt 不等于资格 |
| B4 OAuth | alpha.7 正式 GUI Notion 授权/目录测试、本机临时连接与 grant 清理通过 | 本轮无对应产品差分，不重新 OAuth 或读取 Notion 内容 |
| 睡眠恢复 | alpha.9 闲置睡眠 108.491 秒、writer 自动恢复、实际新消息及正常 Quit 通过 | 用户确认可现场协助；待隔离故障实验收尾后准备一次任务运行中的真实睡眠，不用普通 wait 冒充 |
| §13.3 provider/长程 | 正式安装版 terminal/工具链/PIPE/PTY/monitor、artifact/memory/history、MCP stdio/HTTP/hooks、capacity/context/compaction 真实调用已归档；alpha.10 随包 context 新实证 | 本轮差分已由确定性 R1 和 installed context 关闭；只有后续生产变更影响相应合同才扩大回归 |
| §13.1 受影响原生平台 | Mac arm64 本轮完整选定矩阵及最终 app/provider 已通过 | 原生 Ubuntu x64/arm64/GUI 用户延期；真实 Mac 硬件/其他架构不能由 WSL 代替 |
| 发布制品及 B5 | workflow 保持手动触发；本地 alpha.10 实际 NSIS 和版本/许可证/来源检查通过 | 四制品统一发布矩阵、签名/分发及仍缺真实环境资格未全部完成；无 tag、公开发布或 B5 激活 |

所有新增故障只作用于本次精确私有进程/home/cluster/临时 selection；不得为测试关闭 interop/TUN、终止共享 Ubuntu、重置日用 PG 或修改保存的模型设置。测试外部精确清理不倒填为产品成功；原失败原样保留。三分工完成后由 root 汇总新事实并复审，不凭本计划判通过。
