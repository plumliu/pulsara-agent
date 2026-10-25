# alpha.10 真实活动进程睡眠、自动收尾与新消息

2026-10-25，本机正式安装版 `0.1.0-alpha.10`，Ubuntu/plumwsl，interop 与 Hiddify TUN 保持。**本报告所选真实 GUI 活动进程睡眠闭环通过。** 这是一个 PIPE/Windows echo 等待输入任务，不扩成活动 provider 请求、全部 MCP/hooks 或所有硬件的睡眠矩阵。

## 实际准备与睡眠

原正式 exe 经真实 GUI 快速开始入口创建 `session:qha3722ihbndoc7c7jhzwxaa3e`。保存模型连接只在界面选择，未改 settings。首条消息明确只启动一次等待 stdin 的测试 PE，原 terminal 返回 `process:sz57qhtih4bhpcmwx3rggesfee`/running；首轮 COMPLETED，数据库仅一次 terminal 调用及唯一 QUEUE_PROMPT/CONSUMED。首次 yield0 返回的 output 为空；随后 GUI 后台面板显示运行中，并在“命令输出”区域实际显示独立的 `PULSARA_ACTIVE_SLEEP_READY`，来源为 gui-draft-before.txt，不把命令字符串或模型自述当作已捕获输出。

独立 Windows 观察器在睡前持有准确 PID14916/creation134374126291452207 的 HANDLE，并核 image 为本目录独占测试 PE；持续等待该 HANDLE 直到退出，不发任何信号。该只读观察器的独立有限进程自检通过。测试 PE 只有等待 EOF 的 echo 行为，没有业务写入或寿命 timeout。

用户当时在等现场睡眠，因此三分工先完成当前有限实验并明确给出 safe-for-sleep，再记录正式睡前快照。用户使用笔记本内屏，外接屏不可用；双屏往返继续延期。系统实际记录三组 506→507：1.076 秒、7.430 秒及最后 **132.121 秒**（22:42:43.0974559→22:44:55.2181384）。保留全部原事件，不用用户目标约 60 秒或观察快照间隔冒充实际睡眠时长。

## 唤醒后的事实

16 项前后比较通过：Windows main34108、Linux kernel472/start9469、PG499/start10106、原 namespace/boot、PG systemid7700523439330484737、锁 inode、control socket、HTTPS37313 listener/完整证书保持；Windows 精确证书只读 health 检查成功。TUN tun0/ifIndex30 仍 Up。

writer generation 1→2，owner 改变，新 lease 在独立数据库观察时有效。原 Windows child 的同一已持 HANDLE 在 22:45:03.821677 被观察为退出，exit1；观察器 signalSent=false。用户未点重试、终止或发送，后台进程列表自动清空、中文未发送草稿原样保留。这与原 Host 写入资格失效后物理关闭活动 scope、随后 cold 恢复的合同一致；不是旧进程继续运行或从历史恢复。

本轮未在睡前另持 Linux task 的独立 pidfd，所以不声称独立逐一证明该 scope 的全部旧 Linux PID/FD 身份。真实 Windows HANDLE、原产品恢复后后台库存、相同 kernel/PG 和随后正常完整退出共同构成本项证据；具体跨域双域 owner 的失联/物理 join 由 terminal 专项另验。不能凭这个有限观察宣称所有系统进程都为空。

## 新消息与正常退出

root 观察草稿后只点击一次发送，实际模型回复“活动任务睡眠后连接正常”。独立只读 SQL 核两个 ROOT 均 COMPLETED、两个不同 client_submission_id 的 QUEUE_PROMPT 各精确消费到对应 initial_entry。全部 tool_calls 与睡前完整值相等，仍只有原一次 terminal；没有重新启动命令、工具重放或新增工具调用。原用户两个会话/5 turns/32 entries 的完整语义投影仍保持。

Ctrl+Q 打开原确认框，首次 normal-quit.json 仍有进程是等待确认阶段，不是产品退出失败。Computer Use 无法绑定该原生按钮，用户明确报告亲自点击退出。22:47:50 应用进程0、测试child0、原kernel472不存在、正式pg_ctl status3；22:47:52 原已装 exe 维护检查 exit0。未外部kill正式应用/kernel/PG，不重置数据或清理 writer lease。

独立 `review_results.py` 通过并生成 verified-results.json；原观察和工具消息未重写。正式应用保持关闭，新验收会话/目录留作复核；所有本项观察进程已自然退出。两批故障实验只在用户睡眠闭环结束后继续。普通关闭失败、自动恢复失败的既有边界并未因本次通过删除；本项不激活 B5，也不替代延期的其他真实环境。
