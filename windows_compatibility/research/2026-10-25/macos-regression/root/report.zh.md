# Windows 共享修改后的原生 macOS 回归

2026-10-25，`codex/windows-g0`，受测基线 `a57ce3b103214d3a0865e0460c0f7dd0d0aa97a4` 加本轮窄修，先完成验证和四方冻结，再按用户授权随本报告提交；完整候选以包含本报告的Git提交为准。root 与三位 `gpt-6.1-sol / high` subagents 并行执行。**已执行的 macOS arm64 矩阵通过；交叉审阅收口见末尾。** 这不是所有平台、所有睡眠场景或 Windows 激活资格。

## 环境与证据来源

原生 macOS 26.2 / 25C56、Darwin 25.2.0、arm64；Node 22.23.3、Electron 44.9.0；Python 使用仓库 uv `.venv`，随包使用独立 Python 3.12。PG17.9、pg_search0.26.1、vector0.8.1。root 独占 mise/uv/npm 依赖、资源、wheel 和应用构建；subagents 不并行改写构建目录。

原始材料来自 `output/macos-regression-20261025/{root,backend,terminal,desktop}`。本目录对应四方文本材料及小探针归档；报告内各文件名相对于各方目录。`package/`、`package-fixed/`、`installed/`、venv、node_modules、数据库和 tsbuildinfo 不归档。临时 PID/路径只表示当时事实，不能在另一机器照抄操作。

## 结果

| 范围 | 最终事实 | 材料 |
| --- | --- | --- |
| desktop 统一验证 | `mise run --jobs 1 desktop:verify` 初次通过；三条生产修复后的最终重建再次通过，Node105/105、0skip，资源搬移/版本/实际检索验证通过 | root `verify.log`、`verify-fixed.log` |
| 前端 | 完整83文件1372测试通过、0skip；类型及完整frontend lint通过；新增10个Mac Meta/IME/picker取消合同case | desktop `report.md`、`results.json`、`vitest-full.log` |
| 后端 | 最终受影响11模块244通过、0skip；包含原206项、真实PG counterexample、head-only负例/清理失败；21条既有aiohttp deprecation保留 | backend `report.md`、`pytest-final.log` |
| terminal/hooks/MCP | 原14模块167通过，私有PG host/persistence4通过；窄修后最终定向68通过、0skip；不是与前项相加得一个不重叠总数 | terminal `suites-results.json`、`implementation-review.md`、`zombie-final-focused.log` |
| Electron cover | 原首次真实运行失败，原因是fixture未确保可见却等待fade；补真实可见前提及独立hidden快路径，原2秒和所有原断言保留。agent完整连续3次通过，root原npm命令最终再通过 | desktop `startup-analysis.md`；root `startup.log`、`startup-fixed-final.log` |
| Electron browser | 13/13真实Electron场景通过；本轮后续只改Python与测试fixture，未改browser产品代码 | root `browser.log`、`browser/results.json` |
| 最终包 | `package:dir`成功，alpha.9 / Mac arm64；包中三条生产Python文件与当前源码完整bytes相等 | root `package-fixed.log`、`package-fixed-provenance.json` |
| 包外CLI/文件/终端资源 | 从`/private/tmp`使用最终app内Python `-I`；site-packages来源、static、中文/空格/单引号可执行路径的原生产CLI生成器`--version/--help`、实际文件搜索和terminal私有rg通过 | root `final-package-smoke.py`、`final-package-smoke.json` |
| 最终wheel真实provider | context8次、capacity15次调用通过；四ACTIVE/一PENDING、MCP READY、压缩、五worker完成；context steer/mailbox精确marker通过 | backend `context-fixed/`、`capacity-fixed/` |
| 最终wheel真实terminal provider | 四场景19次provider、14次tool通过；workdir隔离、PIPE输入、PTY输入、39.29秒同process自主唤醒，退出0；未人工follow-up | terminal `provider-final-results.json`、`provider-final-validation.json` |
| provider前缀 | 上述最终真实场景逐次SYSTEM/tools相同、messages按suffix追加；随机调度没触发post-accept新分支，所以另用同wheel+真实PG确定性head6→7触发一次，严格验原single accept/原installed view和prefix | backend `final-wheel-counterexample.json`、terminal最终validation |
| writer恢复 | 最终wheel+真实独立PG：过期writer封旧准入、旧Core单次物理close、并发resume共享新handle、generation1→2，原session可用 | backend `writer-recovery-final/` |
| native GUI | 最终独立app通过冷启动、原生创建中文目录/会话、文件引用、⌘N/⌘K、隐藏/恢复草稿、正常Quit、再次冷重开保留两会话/工作区，最后正常Quit | 下节、root `gui-final-*.json/txt/log` |
| 静态检查 | 最终七个受影响Python文件Ruff通过；diff whitespace检查通过。unused test import已删，无行为变更不重复跑测试 | terminal `ruff-final.log`、root最终验证记录 |

完整provider请求、回复和工具结果保留，仅实际saved credential值被原scrub owner处理；不将“看不到secret”替代行为证据。未从shell环境/.env拼装模型配置，未写回生产settings。

## 两个实际产品缺陷与修复边界

1. **Darwin僵尸进程组EPERM。** 原fork-tail自然完成偶发被hooks当成失败，清理signal也抛EPERM。新增真实未reap子进程反例在改前2失败/1通过；修后68项通过、原自然tail连续30次通过。hooks与MCP两leaf对齐现有terminal/environment合同：存在性EPERM保可能存活，信号EPERM不覆盖原结果，仍等原wait和最终ESRCH。没有把EPERM当空组、增总超时或引入ps权威。生产只改`hooks/executor.py`与`conversation_kernel/mcp/sdk_facade.py`。
2. **ROOT accept后全局head推进。** 原真实context两次失败，另有真实PG改前失败。严格later head、model binding相同、经原constructor验证的完整DTO归一值相同才退休未安装handle/borrow/hook/planning，回到同一个已accepted turn的普通prepare。其余漂移仍失败；不再accept、不造新command/turn、不改installed epoch/SYSTEM/tools/messages；四资源逐个故障仍各清理一次并传播原失败。生产只改`provider_dispatch.py`。最终真实provider通过与同wheel确定性分支触发分别记录，不混为一次运行。

无新增schema、durable event/job/receipt、registry、fingerprint类别、生命周期总上限或平台框架。规范§24记录窄修合同。两类测试fixture修正分别为真实PageVisibility前提和裸Core已有`_history_maintenance=None`字段，不修改生产关闭合同或弱化旧断言。所有原失败、harness import/scrub/CLI help大小写等试验自身错误保留，不计成功、不删证据。

## 实际 macOS GUI 与用户状态隔离

先运行改前alpha.9，再用三条窄修后的最终alpha.9重建覆盖**已退出的测试副本**，保持同一私有home。测试bundle仅改bundle ID为`dev.pulsara.desktop.macos-regression`、注入私有PULSARA_HOME、移除URL注册；保留原basename/Helper名。没有覆盖`/Applications/Pulsara.app`、用户数据库或保存的模型配置。最终GUI模型配置为0组；真实模型由前述saved settings只读内存注入的wheel场景验证，不冒称在GUI发送了模型消息。

改前实测创建`Mac 中文 工作区's`、会话`jbtudjjg`，选择37byte测试文件`附件 中文's.txt`，插入原生reference及中文草稿，⌘K搜索、关闭恢复、正常⌘Q退出。改前main31867/kernel32078/PG32106全部退出。之后新签名副本因macOS旧签名要求失配，再触发DesktopFolder确认；用户实际点允许后最终包ready，原会话/目录仍在。这是系统权限等待，不是增加产品timeout的理由。

最终包main86198/kernel86326/PG86332：原生目录面板在原工作区内图形新建`最终包 子目录's`，返回完整POSIX路径并创建会话`a7wkn3ta`。原生文件面板选定37byte文件后composer出现文件reference；原Mac路径直引用合同保持，不宣称发生Windows式上传副本。⌘K列出两会话；中文文本与file reference在⌘W/重新激活同窗口后完整保留。正常⌘Q后main/kernel/PG全退，PG日志shutdown完整。相同最终签名再冷开，无需第三次授权，main97594/kernel97605/PG97612 ready，总览两会话、本地数据正常，进入新会话后工作区路径仍准确；最后正常⌘Q，三个进程全退。未用强杀制造正常退出。

**草稿寿命按现有合同记录：** 隐藏/恢复同renderer保留；完全Quit后的cold renderer是空草稿。改前和改后相同，`PulsaraApp useState(new PromptDraftStore())`及其内存Map没有持久化owner。本轮没有添加跨重启草稿持久化。中文文字由自动化粘贴，不作为真实硬件IME候选窗证据。

GUI测试曾短暂用同目录symlink保护用户CLI不被candidate自动重注册覆盖；原文件原inode保存在同父目录并仍可执行。所有candidate退出后恢复**原inode/完整bytes/mode**，保护symlink及备份均消失。最终核对原生产main20252/kernel20270/PG20275仍原样运行。两个私有home均已按身份核验停止并删除，UUID数据库零残留，测试包保留在忽略的output中。详见`gui-final-cleanup.json`和`private-pg-cleanup.json`。

## 未覆盖与后续边界

- 用户明确无法做的真实Mac OS sleep/wake仍待验收；本次writer到期/冷恢复是机制实证，不能替代硬件睡眠。
- 真实硬件IME、Mac x64/最低OS、签名公证/DMG分发更新、所有故障排列未覆盖。当前为arm64隔离应用副本，未安装更新用户日用app。
- 本轮没有Notion重新授权；Windows已有OAuth证据不算Mac新包OAuth重验。
- 原生Ubuntu x64/arm64及GUI仍按用户要求延期；WSL、Mac都不替代。
- 本次共享修复后的Windows新候选需在Windows重跑受影响回归，不沿用旧alpha.9安装证据。未自动升版本、tag或发布，也未把所选Mac矩阵通过写成B5激活。

## 交叉审阅

backend、terminal、desktop已分别交叉审查其他分支源码/失败/最终wheel证据，并完成根报告、规范§25及README的全文审阅，三方最终均APPROVE。桌面原条件批准所列最终新包GUI、退出和CLI还原已按上节闭合。root也批准本轮所选Mac arm64矩阵，四方冻结结论见[freeze-review.zh.md](freeze-review.zh.md)；不把未覆盖项改写成通过。
