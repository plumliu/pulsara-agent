# 原生macOS回归最终四方审阅

2026-10-25。受测基线a57ce3b1加本轮三条Python生产leaf窄修及对应测试/规范增量。本文件记录提交前完成的冻结审阅；用户随后授权提交、推送，完整候选以包含本文件的Git提交为准，不以旧HEAD冒称包含本轮修改。

| 审阅方 | 最终结论 | 记录 |
| --- | --- | --- |
| root | APPROVE | 已读生产差分、严格负例、改前失败、最终wheel/provider及实际GUI；统一构建、包来源、正常退出、用户CLI恢复与私有PG清理闭合 |
| backend，gpt-6.1-sol/high | APPROVE | [最终审阅](../backend/final-root-review.md) |
| terminal，gpt-6.1-sol/high | APPROVE | [最终审阅](../terminal/final-root-review.md) |
| desktop，gpt-6.1-sol/high | APPROVE | [最终审阅](../desktop/final-root-review.md) |

此前desktop条件批准所列最终签名包ready/保数据/正常Quit和CLI还原已由真实CUA、进程/PG日志及实际文件恢复证据关闭；不把过程中的旧包通过替代最终包。后端随机provider未触发post-accept新分支，已用同wheel真实PG确定性原测试补齐，两个证据用途保持分开。

**冻结本轮已执行的原生Mac arm64回归结果、三条相关生产窄修与规范§24–25对应合同。** 无剩余代码或本机证据阻断。正常业务保持原owner与provider前缀合同，没有新增durability/registry/总上限。source静态检查、最终相关测试、真实provider与独立应用包GUI均通过。

冻结范围不包括用户明确不能做的真实Mac睡眠、硬件IME、Mac x64/最低OS/签名分发、原生Ubuntu和新共享修复后的Windows资格。Windows需重跑受影响用例，不沿用旧alpha.9安装记录；Windows支持/B5未激活。没有tag、对外发布或覆盖用户日用app。

冻结审阅完成时，源码与新增测试仍在工作树，尚未stage/commit/push；用户随后明确授权将代码、测试和报告提交并推送到codex/windows-g0，不合并main。测试资源/应用包留在忽略的output；可读报告、原始失败/成功记录和小型探针归档在实施目录，实际saved credentials扫描零命中。清理验收时原生产main/kernel/PG仍运行，用户CLI原inode/bytes/mode已还原。
