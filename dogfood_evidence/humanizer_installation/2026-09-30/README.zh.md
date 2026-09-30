# humanizer 安装 dogfood（2026-09-30）

结果：Pulsara 自主下载并通过内置 `terminal` 工具调用官方 Skill 安装服务，将 humanizer 3.1.0 安装到 `/Users/plumliu/Desktop/test1/.pulsara/skills/humanizer`。随后实际使用 `$humanizer` 完成文本改写，确认本轮来源为该工作区副本。

- 会话：`session:09114dfab0f3433f8d3b91f91b534776`。
- 配置：用户选择的 `test1`、OpenRouter GPT-6 Luna、xhigh、完全访问。
- 上游：[blader/humanizer v3.1.0](https://github.com/blader/humanizer/tree/v3.1.0)，commit `225a6f39ac85f76ee48dbad772ea4abe4ed6c9d8`。
- 第一轮准确报告未安装：内置 terminal 返回 `pulsara: command not found`，下载已完成，但没有绕过官方安装服务或误报成功。
- 监督方用 uv 补齐官方全局启动器；没有修改生产设置或 runtime 源码。
- 第二轮模型执行 `pulsara skills install --scope workspace --workspace /Users/plumliu/Desktop/test1 /tmp/humanizer-upstream.EHOszQ/repo`，返回 `Installation: INSTALLED`，并运行 list/doctor 检查生效 winner 和旧版遮蔽状态。
- 第三轮使用新版 `$humanizer`；最终稿保留“今天上线”“同时编辑”“实时显示”“所有套餐免费”四项事实。
- 独立核验：工作区能力 API 的 humanizer 为 `enabled=true`、`effective=true`；adoption 无 pending；安装的 SKILL.md 与上游原始字节相同，元数据为 3.1.0；旧用户级源仍为 2.5.1。三轮均以指定模型和 xhigh 完成。
- 本次没有触发 HITL，未覆盖 `manage_capability` Plugin 导入/启用流程；实际覆盖的是内置 terminal + 官方 loose-Skill 安装流程。
- 没有发现 runtime bug，不为环境缺失修改生产代码。未运行代码测试，因为没有生产代码变更。

[完整接受记录与工具参数/结果](trace.json)保留实际输入、输出、错误和最终文本；仅脱敏配置中实际凭据值。未为此次证据新增 runtime 事件、表、指纹或恢复机制。
