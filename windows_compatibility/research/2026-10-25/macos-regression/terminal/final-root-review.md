# 最终 root 汇总与规范交叉审阅

2026-10-25，terminal / gpt-6.1-sol high。

**APPROVE：同意冻结本次共享窄修与已执行原生 macOS arm64 资格，无剩余源码或本机证据阻断。** 此批准只覆盖报告列明范围，不批准 Windows 新候选激活、原生 Ubuntu 或真实 OS sleep/wake 等待验收项；不授权 tag 或对外发布。本轮为只读审阅，没有运行测试/构建或操作用户应用。

通读对象：`windows_compatibility/research/2026-10-25/macos-regression/root/report.zh.md`、活动规范 §24–25、`windows_compatibility/README.md` 本轮diff；交叉核对四方报告及最终 wheel/provider、head-race、应用包来源、GUI/CLI/PG 清理材料。

1. 本专题事实与 root/§25/README 一致：原167+私有PG4、最终定向68分别记录，不将重叠 suite 相加。最终 provider19次/14工具、原38 tools、PIPE/PTY退出0、39.29秒同process自主唤醒与精确SYSTEM/tools/messages suffix来自同一fresh wheel。报告保留改前EPERM失败、输出harness失败和未修wheel首轮正证，未混同最终候选。
2. backend 最终context8/capacity15报告、same-wheel real-PG head6→7确定性反例及writer-recovery-final已闭合先前实际剩余前置。随机provider未命中新分支如实单列，不将随机成功伪称确定性触发。head-only仍仅退休未安装资源，原single accept/installed view/prefix保持；EPERM两leaf仍以ESRCH证明组消失，未增加durability/timeout/registry。
3. root 包来源JSON证明最终app三条Python文件与本次源码完整bytes一致；final-package-smoke来自独立app内Python -I、/private/tmp cwd，CLI生成器与中文/空格/单引号路径、文件搜索/private rg成功。不是把仓库可导入性冒作已打包证据。测试bundle ID/home/URL注册隔离与日用应用保持分开。
4. 已读gui-observations、first-quit、cold-reopen进程记录、gui-final-cleanup、PG日志/cleanup。最终图形原生中文目录/会话/37byte文件reference、Meta快捷键、隐藏恢复草稿，与同一最终签名冷开后两会话/工作区保留的叙述一致。两个最终运行均正常Quit，候选PID清空、正常PG stop；原CLI same inode/bytes/mode恢复、保护symlink移除，原生产main/kernel/PG仍为原PID。私有PG身份与空UUID DB后正常stop、home删除有独立记录。未将强杀或另包冷开冒作正常寿命。
5. 实际 `PulsaraApp` 每renderer useState创建 `PromptDraftStore`，其sessions为内存Map；同renderer隐藏恢复保留、完整Quit后草稿为空符合原合同。报告明确中文采用粘贴，并未把它当硬件IME证据，也未引入跨重启草稿durability。
6. startup smoke修正建立真实visible前提且独立验证hidden无动画/IPC与销毁；原2秒和旧断言保留，无生产动画修改。root最终原命令通过与desktop连续三次通过均有日志。未将fixture问题混同产品超时或删除原失败。
7. §25与README保留OS sleep/wake、硬件IME、Mac x64/最低OS/签名分发、Ubuntu延期与共享修复后Windows重验等边界；历史§16等当时状态未被静默当现状。当前支持未激活、不自动tag/发布的表述一致。

archive-scan记录具体saved credential零命中并含JSON转义扫描，完整provider输入/回复仍可审阅；未过度redact或导出credential。最终冻结表仅再收集四方本候选明确批准，不应把前轮条件批准或沉默当最终批准。除此元数据收口外，无需为了本审阅另改实施语义。
