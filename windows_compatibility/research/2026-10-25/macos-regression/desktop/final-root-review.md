# 最终root报告、规范与Mac证据审阅

2026-10-25，desktop / gpt-6.1-sol high。

**最终结论：APPROVE。同意冻结本轮实际覆盖的macOS26.2 arm64回归、三处共享生产窄修及相应测试/证据；无剩余阻断。** 前轮条件批准中的最终新签名包GUI/正常Quit、保数据冷重开及CLI恢复条件现已关闭。不把该批准扩大为硬件睡眠/IME、Mac其他架构或分发、Ubuntu延期项、Windows新候选重验或B5激活/发布通过。

## 已完成最终复核

通读 `windows_compatibility/research/2026-10-25/macos-regression/root/report.zh.md`、同目录 `gui-observations.json`、`gui-final-cleanup.json`、`private-pg-cleanup.json`、原生process观察及PG shutdown日志；核对活动spec §25与windows_compatibility/README，结合本专题此前完整前端1372/types/lint、startup原失败及修后三次、root最终startup、包内三生产leaf完整bytes和backend/terminal同freshwheel补证。只读审阅，无新构建/测试或前台操作。

- root报告准确区分所选Mac矩阵、其他平台及历史Windows事实；各suite重叠不相加，初次错误、harness限制和最终来源完整保留。
- 最终包用户确认第二次DesktopFolder系统提示后ready；同私有home原session保留，实际图形创建 `最终包 子目录's`、新会话a7wkn3ta、37byte原生file reference、⌘N/⌘K、⌘W/重新激活保中文draft/reference。正式Mac路径reference未被写成Windows上传副本。
- 第一个最终包main86198/kernel86326/PG86332经普通CmdQ退出；PG日志明确shutdown。相同最终签名再冷开main97594/kernel97605/PG97612，同私有home/database ready、两会话和精确workspace仍在；最终同样普通CmdQ全退，未以强杀替代正常退出。
- `gui-final-cleanup.json`记录本次candidate零残留、PG正常停止、测试home删除、CLI相同inode/完整bytes/mode且guard移除；原production20252/20270/20275仍运行。私有PG数据目录/系统标识已核，stopReturnCode0、UUID库零残留、home删除。报告没有宣称测试更新了日用/Applications应用。
- `PulsaraApp`现用 `useState(() => new PromptDraftStore())`，PromptDraftStore持process-local Map；报告、§25、README准确写“同renderer隐藏恢复保留，完整Quit后cold renderer空草稿”，没有承诺跨重启持久化或为本次验收增加持久owner。
- DesktopFolder提示归于测试签名code requirement变化，由用户处理受保护原生授权；未将等待误报启动超时/产品回归或绕过TCC。中文使用粘贴，文档明确不充当硬件IME候选窗实证。

## 规范/交付范围

spec §25与root最终报告一致：EPERM保live直到原wait/ESRCH；head-only只退休未安装候选、同accepted turn普通prepare、不重accept或重base；前缀连续和最终真实provider、确定性PG反例、实际GUI证据分别归属。README将本轮Mac成功与尚欠平台/故障矩阵明确分开，没有根据旧alpha.9 Windows安装事实放行新共享修复。

最终批准不要求额外重复已通过测试；后续若源码或行为再变，应按受影响范围重新验收。root可将本文件归档并记录本轮四方最终批准。
