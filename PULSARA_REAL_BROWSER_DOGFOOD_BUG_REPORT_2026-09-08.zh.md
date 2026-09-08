# Pulsara 真实浏览器交互测试报告

日期：2026-09-08。第一轮约 12:53–13:05，第二轮约 13:07–13:22（Asia/Shanghai）。
代码基线：`7e2ec332 feat: implement effective-context conversation forks`。
范围：真实浏览器、真实保存的 provider 配置、真实本机数据库；只调查和记录，不修改产品实现。

## 第一轮结论（第二轮深入测试见文末）

完成 13 条用户输入、两次分叉、运行中排队、两次停止、模型切换和页面刷新。OpenRouter Luna 的 8 条输入和 Muse Spark 的 3 条输入得到最终回复；bobapi 的两条输入在等待期间由测试者主动停止，不能记为完成。

确认两类 UI 问题：

1. 文件工具成功结果展示不足，且实时显示与刷新后的显示不一致。
2. 已排队消息刷新后失去可见正文，仅保留数量，直到开始执行才重新出现。

未发现执行器偏离提交的编辑操作、create-only 意外覆盖、分叉带入锚点之后的历史，或停止后无法继续聊天。Muse Spark 曾提交一次含多余行号前缀的内容，随后自行复读纠正；不能将最终正确理解为每次模型编辑都正确。上述结论仅覆盖本次场景，不代表全面回归通过。

## 环境与数据处理

- 启动命令：`PYTHONUNBUFFERED=1 .venv/bin/pulsara app --no-open --port 59051 --workspace /private/tmp/pulsara-browser-dogfood-p5vIk1`。
- 页面：`http://127.0.0.1:59051/`，Codex 内嵌真实浏览器，实际可见视口约 674×734。
- 通过浏览器点击、输入、键盘和刷新操作交互，并多次检查实际截图与无障碍树；不是仅检查 DOM 或运行后端脚本。
- 配置由 `LocalSettingsStore` 读取 `/Users/plumliu/.pulsara/local-settings.yaml`，未修改保存的生产配置，未导出模型密钥。
- 按用户明确要求重置真实本机开发库，没有创建临时数据库。重置前核验目标为 `pulsara`、地址 `::1`、端口 `5432`、管理角色及原数据库 owner 均为 `plumliu`。
- 停止本次启动的旧服务后，删除并重建确切的 `pulsara` 数据库，保留 owner，再用现有 `PostgresMigrationRunner` 初始化 clean-v0；报告为 `status=current`、`applied_versions=(0,)`。原会话等数据被清空，本次没有备份。模型配置和密钥未删除。
- 初次启动的数据库结构不匹配在重置后消失，按用户说明视为开发期尚未重置的环境前提，不计入产品 bug。
- 文件工具仅操作测试目录中的 `budget.txt`，没有让模型修改仓库或配置。
- 使用保存的 `OpenRouter / openai/gpt-5.6-luna`、`OpenRouter / meta/muse-spark-1.3-contributor`（均为 Chat Completions）和 `bobaigpt / gpt-5.5`（bob-api.com，Responses）。按用户后续建议，改用 Muse Spark 完成跨模型与队列测试，不以 bobapi 等待判断 Pulsara 正确性。没有模拟模型返回或故障注入。

会话定位：

| 角色 | 会话 ID |
| --- | --- |
| 原会话 | `session:a9893021ced2423a8fa7264fca97391f` |
| 从第二轮回复分叉 | `session:dc7fe721885f47d2b514bff8d4e2d23c` |
| 从分支后续回复再次分叉 | `session:56c2a9050dcb4a1a87eee5366e2a4df7` |

## BUG-01：文件工具成功卡片丢失具体结果，实时与恢复显示不一致

优先级建议：P2。执行结果正确，但用户无法直接核验修改内容。

### 复现

1. 在原会话要求创建 `budget.txt`，三行为 `项目=青鹭`、`预算=320`、`参与者=小林,阿岚`，保留最终换行。
2. 要求读取文件，用 `edit_file` 将预算行改为 `预算=360`，再读回。
3. 在实时工具链中展开已成功的 `edit_file` 卡片。
4. 后续再次读取该文件；刷新页面，再展开 `read_file` 卡片，比较展示。

### 实际

- 实时展开 `edit_file`，正文只有“操作已完成。”，底部“操作完成”；没有目标路径、diff、changed windows 或新 revision。
- 实时成功文件卡片主要呈现通用成功信息。
- 最终刷新后，在二次分支展开 `read_file`，变为“已读取 budget.txt · 3 行”，仍不显示实际读取的三行内容。不能将刷新前后都描述成完全相同的通用占位。
- 截图检查确认并非内容在卡片外被裁切：展开区域本身只有上述简短文本。
- `write_file` 的失败卡片可以显示已有路径拒绝覆盖的原因，因此并非所有工具输出都不可见。

### 预期

成功摘要应至少稳定显示目标文件；展开应能检查实际读取内容或编辑 diff/changed windows。实时完成与刷新恢复应采用一致语义，不应依赖模型最终自然语言回复来核验工具修改。

### 证据与定位线索

只读查询 canonical `tool_results` 关联的 `transcript_entries.inline_content`，确认原始成功结果完整存在：

- `read_file` 包含 `path`、`content_revision`、`total_lines=3` 和带行号 `content`。
- `edit_file` 包含 `base_revision`、`content_revision`、`operations_applied=1`、`changed_windows` 和以下 diff：

```diff
--- a/budget.txt
+++ b/budget.txt
@@ -1,3 +1,3 @@
 项目=青鹭
-预算=320
+预算=360
 参与者=小林,阿岚
```

磁盘读回也确认文件预算为 360。因此本次是展示链路问题，不是 builtin 没有产出结果。

相关代码检查：

- `frontend/lib/runtime-adapter.ts:2727` 的 `formatToolResult` 对读取只返回路径/行数摘要；没有编辑 diff/changed windows 的展示分支，未识别结构会返回通用成功文本。
- 同文件 `2455`、`2621` 附近有不同结果接入路径，需要继续核对实时结果覆盖与 transcript 恢复顺序，不能仅凭本次观察断定所有实时差异都由该 formatter 单独造成。
- `frontend/components/workbench-view.tsx:321` 的 `TraceCard` 展示 `command`、处理后的 `output` 和 meta；没有通用展开原始 `resultText` 的入口。

本次没有修改这些代码。

补充复现：13:04–13:05 使用 Muse Spark 编辑 `预算=360` 为 `预算=400`，实时展开 `edit_file` 仍然只有“操作已完成。”和“操作完成”。因此该展示问题不只出现在 Luna 上。

## BUG-02：排队消息刷新后只剩计数，正文暂时消失

优先级建议：P2。不是已确认的数据丢失，但影响用户核对待执行输入。

### 复现

1. 在第一分支选择 bobapi，发送“现在预算追加80元，仍然两人均摊……”并等待其处于“进行中”。
2. 输入“上一轮回答后，再只用一句话说出项目代号和追加后的总预算。”，按 Enter 排队下一轮。
3. 确认页面显示这条消息正文，以及“1 条输入正在等待处理”。
4. 刷新页面，等待会话恢复完成。

### 实际

- 正在执行的消息与历史正常恢复。
- 排队计数仍为 1，但刚刚排队的消息正文从聊天列表消失，没有其他可见入口检查内容。
- 测试者停止当前运行后，队列中的消息被接纳执行，其正文重新出现。因此这不是队列数据实际丢失。
- 刷新前后都检查了实际截图；恢复后的无障碍树也仅包含计数，不包含待处理正文。

### 预期

已接受的排队消息在刷新恢复后仍可查看正文，并明确标记等待状态。可以从现有队列语义投影，不应为此增加新的 durable event 或重复消息权威。

### 定位线索

- `frontend/app/pulsara-app.tsx:161` 使用 process-local React `optimisticMessages`。
- 同文件 `754` 附近在提交成功后将消息正文加入该列表，状态为 `waiting`。
- 同文件 `615` 将 optimistic 消息附加到现有 projection。
- `frontend/lib/runtime-adapter.ts:1923` 恢复 `prompt_queue_total_count` 计数。

观察与“刷新清除 optimistic 正文，而恢复投影只有队列计数”的机制一致；后续修复需核实已有队列读取接口能提供哪些字段。本次未实现修复。

## 实际交互覆盖

| 输入/操作 | 观察结果 |
| --- | --- |
| 1. 记住青鹭、320 元、小林和阿岚 | Luna 两句话准确确认 |
| 2. 按 50%/30%/20% 分配，用表格和 Python assert 输出 | 160/96/64，总额 320；两人各 160；表格、代码块可见 |
| 3. 创建→读→edit→读 budget.txt | 成功；磁盘预算 360；发现 BUG-01 |
| 从第 2 条回复 Fork，原会话保留未发送草稿 | 新分支输入框为空；切回原会话草稿仍在 |
| 4. 分支只根据历史回忆预算和是否改过文件 | 回答 320、两位姓名正确，明确此前没有要求创建/修改文件 |
| 5. 切 bobapi，追加 80 元 | 等待数分钟未见正文，测试者停止；未证明 provider 成功完成 |
| 6. 运行中排队一句话复述，再刷新 | 计数恢复、正文暂失（BUG-02）；上轮停止后开始执行；再次主动停止 |
| 7. 切回 Luna 续聊 | 正确回答青鹭、400 元、两位姓名；中断后可继续 |
| 点击手动压缩 | 按钮由“正在整理上下文”恢复；未证明产生压缩快照，见限制 |
| 8. JSON 汇总及均摊计算 | 400、每人 200；JSON 代码块可读；公式实际是普通文本算式，不算 KaTeX 覆盖 |
| 9. write_file 尝试已有路径，禁止绕过，再读取 | 预期 APPLICATION_ERROR；错误卡片可读；原文件仍为 360 |
| 再次分叉 | 导入历史可见，新输入框为空，锚点回复可继续分叉 |
| 10. 对比对话预算与文件预算 | 回答 400 与 360，能区分对话事实和读文件事实 |
| 最终刷新 | 二次分支历史与最终回复恢复，无重复提交现象 |
| 11. 改用 OpenRouter Muse Spark，按对话预算设计六步活动 | 跨模型保留 400/360 的区别和两位姓名；六项费用合计 400 |
| 12. Muse Spark 运行时排队一句话总结，再刷新 | 自然完成上一轮并执行下一轮，回答每人 200、文件仍 360；不再需要人工停止来推进队列 |
| 13. Muse Spark 读取→按行同步预算为400→复读 | 最终正确；首次误写行号前缀，复读后自行纠正；同样复现 BUG-01 |

视觉观察：Copy/Fork 图标靠左相邻；中文消息、表格、Python/JSON 代码块、思考折叠、工具卡片、排队提示和停止按钮在当前窄视口可用。没有观察到阻止交互的裁切。浏览器警告/错误日志在本次抽查时为空，不代表所有时刻或所有服务端请求都无错误。

## 未确认项与限制

- Muse Spark 排队复测：刷新完成时两轮已经执行完毕，因此验证了自然排队完成和最终恢复，没有在这次更短的等待窗口再次捕获 BUG-02。BUG-02 的直接证据仍来自此前等待期间的刷新。
- 行号前缀易错性：Muse Spark 首次编辑结果为 `2|预算=400`，下一次读取显示 `2|2|预算=400`；它随后提交第二次编辑删除多余前缀，最终三行文件正确。canonical 工具 diff 与磁盘读回相符。建议后续检查 descriptor/examples 是否充分说明 `N|` 仅供定位，不属于 replacement content；不要据此引入自动剥除前缀的 fuzzy 修复，因为文件原文可能合法包含同样字符串。这是模型调用与契约易用性风险，不另列为已确认执行器 bug。
- bobapi：首次请求约从 12:56 等到 12:58，界面持续“正在回复”，随后人工停止；队列下一轮也人工停止。没有拿到完整 provider 终态或 HTTP 错误，不定性为 Pulsara 挂死或 provider 故障。恢复到 Luna 后正常。
- 手动压缩：按钮恢复后续聊正确，但只读查询 `context_snapshots` 为零行。短上下文、策略不执行或其他结果尚未细分，不能声称“压缩已成功且语义通过”。建议后续单独检查压缩结果反馈和实际 successor 激活。
- 没有做长历史压力、多个物理浏览器、真正桌面宽屏、多用户并发、网络断连、OAuth/MCP、子代理、权限弹窗或外部并发写入测试。
- 本次截图已在测试对话中逐次展示和检查，未保存为独立图片文件；持久证据为本报告、保留的本机测试会话、canonical 工具结果和测试文件。
- 没有执行大规模自动化测试，也没有为本次探索新增生产代码、兼容路径、hash 或 durable 数据结构。

## 交接

服务保留在 `http://127.0.0.1:59051/`，最后一条输入已完成，没有故意保留运行中的模型请求。三个测试会话和测试文件保留，方便继续复现。

本次仓库仅新增本报告。开始测试前已存在未跟踪目录 `output/playwright/cli-session/`，未修改或清理它；没有 stage 或 commit。

## 第二轮：深入功能测试

用户要求扩大覆盖后，在同一真实数据库和 `session:56c2a9050dcb4a1a87eee5366e2a4df7` 上继续。全部模型交互使用 OpenRouter Muse Spark；没有再使用 bobapi，也没有重置数据库。

这一轮不是重复问答：覆盖了规划提问、服务重启、方案修订、批准实施、取消、两个真实并行子任务、权限确认的允许/拒绝、只读模式、终端异步输出与完成、运行中 steer、真实压缩、MCP、技能选择、能力/记忆导航、主题切换和无效目录错误。

新增 10 条普通任务输入和 1 条运行中引导，另有规划问题回答、修订、批准、取消等界面操作。主任务的自动续轮与子任务模型调用不冒充人工输入次数。

### D01：规划提问 → 刷新 → 审批 → 服务重启 → 修订 → 批准实施

步骤与结果：

1. 勾选“先规划”，要求审批后才创建 `itinerary.md`、`checklist.md`，并要求先问一个规划问题。
2. 模型实际调用 `ask_plan_question`；页面出现三种活动风格选项和自由输入。
3. 刷新页面后问题仍可回答。选择“混合：户外+聚餐”，模型调用 `exit_plan`，出现可审批的完整方案。
4. 审批前磁盘只有 `budget.txt`，没有提前创建两个计划文件。
5. 对本次应用进程发送 SIGTERM，随后用原启动命令、同端口、同库重新启动；不终止其他服务、不重置数据。
6. 刷新后待审批正文、取消/修改/批准按钮恢复。此处验证的是 canonical draft review 跨进程恢复，不是恢复旧模型 coroutine。
7. 提出修改：取消文化小馆，将70元留作应急备用金；加入雨天改室内散步且不增加费用；重新审批。
8. 模型提交修订版，准确包含 `60+40+0+150+50+30+70=400` 和雨天约束。
9. 点击“批准并继续”，才实际创建两文件并复读验证。执行期间切换到能力/记忆页，回到会话后结果正常。

磁盘证据：

- `itinerary.md` 为1095字节，包含总额400、两人均摊200、备用金70、取消文化小馆及雨天零新增费用。
- `checklist.md` 为657字节，包含雨天条款、备用金与两人分工。
- `budget.txt` 仍为49字节、预算400，修改时间仍是13:04，没有被规划实施改写。

canonical 只读核验：workflow 1 为 `APPROVED`、revision 7；三个 interaction 依次为 `QUESTION/ANSWERED`、`DRAFT_REVIEW/REVISION_REQUESTED`、`DRAFT_REVIEW/APPROVED`。完整审批主路径通过。

### D02：真实子任务及结果自动交回

明确要求创建两个独立子任务，而不是模型口头模拟：

- A 只读 `itinerary.md` 核算总额、备用金、每人均摊。
- B 只读 `checklist.md` 检查雨天条款与分工。

实际看到 `create_agent_tasks`、两个子任务各自的 `read_file` 和 `report_agent_result`、主任务 `wait_agent`。状态由1/2完成变为2/2完成，主任务收到“子任务进展”并自动汇总，不需要人工点击接纳结果。

子任务侧栏的展开详情显示独立上下文、创建/完成时间、结果和“已用于对话”。核验数据库 `check_checklist`、`check_itinerary` 均为 `COMPLETED`。没有把工具卡片显示“完成”当作子任务实际完成的唯一证据。

### D03：权限拒绝、允许与只读

| 场景 | 实际结果 | 证据边界 |
| --- | --- | --- |
| 每次询问：创建 `permission-denied.txt`，点击拒绝 | 卡片显示已拒绝，模型停止且不绕过，文件不存在 | 数据库有一条 `PERMISSION_DENIED`，覆盖真实拒绝路径 |
| 新一轮每次询问：创建 `permission-allowed.txt`，点击允许 | 单次写入成功，复读为 `allowed-once` | 磁盘12字节；这是一项新的明确授权，不是沿用上次选择 |
| 只读：读预算后尝试创建 `readonly-blocked.txt` | 读到400；模型依据只读权限不发起写调用，不提权、不换终端 | 文件不存在；仅证明权限上下文与模型遵守，未覆盖这轮后端实际拒绝写调用 |

下一轮权限选择恢复默认“完全访问”与界面“规划与权限只作用于本轮”相符；没有把这个产品行为误报为本轮权限被提升。

### D04：终端观察、完成通知和运行中引导

通过 Pulsara 终端工具运行：

```sh
python3 -u -c 'import time; print("START_PROBE", flush=True); time.sleep(25); print("END_PROBE", flush=True)'
```

命令不写文件、不联网。实际工具对 shell 引号做了正确转义。页面显示初始 `START_PROBE`、`terminal_monitor` 观察及后续“命令进展”；最终输出两行齐全，退出码0。

在进程观察期间向 composer 输入追加要求，并按 ⌘ Enter：最终报告加入 `STEER_OK_0918`，不重跑命令。界面显示“你·引导”，数据库记录为 `USER_STEER`（entry sequence 91），不是仅在前端伪装成引导的普通排队消息。终端完成后的自动报告也包含标记。

此处验证了正常异步结束与引导；没有测试强制杀进程、终端断连恢复或 stop 对后台进程的全部策略。

### D05：真正发生的压缩与后续记忆

在上述更长历史后点击手动压缩，页面出现“上下文已压缩”分界线。

数据库有真实 `context_snapshots`：`source_through_sequence=94`，`content_size=2709`，创建时间13:15:26。与第一轮零快照的短上下文探测不同，本次可以确认发生了压缩快照。

后续不使用工具，要求核对预算、备用金、雨天安排、分工、两个子任务、允许/拒绝文件、终端退出码和引导标记。模型全部给出正确要点；对于雨天条款的逐字原文，明确说明摘要中没有保留，不编造原文。

这支持本次样例的压缩可用性，不等于长历史压力或 provider 字节级 prefix continuity 专项验收。

### D06：MCP、技能、页面与视觉检查

- MCP：通过已有 firecrawl 连接实际调用 `mcp__firecrawl__firecrawl_scrape`，只请求公开 `https://example.com`。返回标题 Example Domain 和首句。没有让模型将本地文件传到 MCP、安装服务或修改连接。MCP 工具卡片同样复现 BUG-01：展开仅有通用成功文本，用户不能在卡片中核对抓取正文。
- 技能：从本轮选项的技能选择器点击 `humanizer-zh`，输入框插入 `$humanizer-zh`。提交测试文案后，实际出现 `read_file / 正在使用 humanizer-zh Skill`，模型完成改写，未写文件或联网。测试者没有启停用户技能。
- 能力：用户技能清单19项，搜索 `humanizer` 得到相应两项；切换 MCP 后同一过滤词呈现无匹配，清空后看到已有 firecrawl 已连接。没有新增配置、调用导入或切换实际开关。
- 记忆：跨对话空态、项目选择器和所选测试目录的项目空态可用。只测读取/筛选界面，不声称验证记忆写入、检索质量或治理流程。
- 命令面板：按钮打开、⌘K打开、关键词搜索、Escape关闭可用；深色主题截图检查中文正文与输入区域可读，随后恢复原浅色主题。
- 无效目录：新建会话选择指定目录，提交测试目录下不存在的 `no-such-directory`，返回“这个工作目录不存在或无法使用”，表单保留，未创建目录。关闭表单后原会话仍在。
- 多次实际截图检查审批区、选项、侧栏覆盖层、工具结果与主题；窄视口下审批正文使用内部滚动，底部决策按钮可见。没有将无障碍树可定位等同于所有尺寸下都通过视觉测试。

### BUG-03：批准后无法回看方案正文，历史卡片仍提示等待确认

优先级建议：P2，审批可追溯性与展示问题。没有证据表明后台丢失批准内容。

复现：按D01完成修订和批准，之后向上滚动，展开13:09的历史 `exit_plan` 卡片。

实际：

- 原来完整的可审批方案正文已从主对话中消失。
- 展开该历史卡片，仅显示“方案已提交，等待你的确认。”以及“操作完成”。
- 紧接着下一条用户记录是“方案已批准，请按方案继续。”
- 当前界面没有找到从该历史卡片打开已批准全文或区分两个 draft 正文的入口。不能依赖最后生成的文件反推出究竟批准过哪一版。

预期：批准/修订/取消后，历史中仍有只读的对应方案正文和明确决策状态，避免继续使用等待确认提示作为唯一详情。保留原始工具结果可以理解，但必须有可读的历史审批视图补足，不应把历史结果改写成新的权威。

定位线索：`frontend/lib/runtime-adapter.ts:2732` 附近把 `DRAFT_SUBMITTED_FOR_REVIEW` 格式化为固定等待提示；`frontend/components/workbench-view.tsx` 的 `TraceCard` 只显示处理后的 output，完整 `plan-draft` 正文则属于当前 interaction 编辑器。需沿已有 canonical plan interaction 读取路径投影历史，不新增重复审批事件/记录。

### BUG-04：取消规划的成功提示错误地承诺继续处理

优先级建议：P3，反馈文案问题。

复现：新建仅拟创建 `cancelled-plan.txt` 的规划，等 `exit_plan` 审批卡出现，点击“取消规划”。

实际 toast 标题为“规划已取消”，正文却是“Pulsara 将继续处理。”；实际上没有启动实施、文件没有创建。数据库 workflow 2 为 `CANCELLED`、revision 3，draft interaction 也是 `CANCELLED`，不存在运行中的 turn。

预期：明确说明计划已取消、未执行，可发送新任务，不承诺自动继续。

定位：`frontend/app/pulsara-app.tsx:894–896` 按决策设置标题，但 approve/revise/cancel 共用“Pulsara 将继续处理。”正文。本次没有修改代码。

### 第二轮最终核验和交接

- canonical：一个 APPROVED workflow、一个 CANCELLED workflow；两个 COMPLETED 子任务；一份真实压缩快照；存在 USER_STEER；查询当前会话 RUNNING turns 为空。
- 当前会话 EXECUTED tool results 汇总为27个 SUCCESS、1个 PERMISSION_DENIED（该统计包含此会话本轮之前的真实执行，不是第二轮独有调用数）。
- 测试目录最终仅有四个文件：`budget.txt`、`itinerary.md`、`checklist.md`、`permission-allowed.txt`。`permission-denied.txt`、`readonly-blocked.txt`、`cancelled-plan.txt` 不存在。
- 本轮浏览器 warn/error 日志末次抽查为空；不将这等同于端到端所有请求都无错误。
- 服务重启后仍运行在原URL，测试对话保留，主题恢复浅色。没有活跃模型请求或待处理审批故意留给用户。
- 没有修改生产代码、规格状态、配置或密钥，没有重置数据库第二次，没有 stage/commit。Git仅见本报告及测试开始前已有的未跟踪目录。

累计确认4项问题（BUG-01现已扩展到MCP结果展示，BUG-02仍按第一轮直接证据记录，新增BUG-03/04）。其余通过项逐项记录，未运行完整自动化回归，也未覆盖所有权限/取消/长期上下文分支。

## 第三轮：并发控制、失败边界与取消（2026-09-08 13:26起）

继续使用原服务、真实数据库和 OpenRouter；没有再次重置数据库。以下是实际浏览器对话和工具调用，不是模拟响应。除根目录本报告外，只在原测试目录创建、修改专用测试文件，不修改产品代码。

### E01：停止当前运行不等于终止后台进程

启动打印 `STOP_PROBE_3`、等待45秒、再打印 `NATURAL_END` 的 Python 命令；在等待期间点击“停止当前运行”。页面显示已中断，原始 turn 的 canonical 状态确为 `INTERRUPTED`。进程检查显示命令仍存活，随后自然结束，没有重启命令。

终端完成通知又触发了一个新的模型轮次，模型报告收到 `NATURAL_END / exit 0`。数据库区分得很清楚：原轮次 initial entry 为 `USER_MESSAGE`，新轮次为 `TERMINAL_OBSERVATION`，后者 `COMPLETED`。不能把它描述成原中断轮次被复活。

这项记录为**产品语义/交互风险，尚不列为确定执行缺陷**：停止当前推理没有同时杀死后台命令，也没有禁止未来终端完成通知启动新轮次。用户可能将停止理解为整个任务不再自动回复。需要明确停止按钮和后台进程的边界；本次不擅自修改取消契约。测试进程最终已自然退出。

### E02：权限等待期间切换会话与历史隔离

在“每次询问”模式请求创建 `switch-pending.txt`，实际出现 write_file 权限卡后，切换到源会话。源会话没有继承该权限卡；要求只凭历史回答预算及是否请求过该文件，Luna 正确回答360且没有该文件请求，没有读取共享磁盘。

返回测试会话后，该写入显示已拒绝。canonical tool result 为 `PERMISSION_DENIED`，正文为 `tool confirmation ended because the controller detached`，文件不存在。当前 `conversation_kernel/interaction.py` 存在此显式拒绝路径。

结论：安全上 fail-closed，无越权写入；普通权限等待会随 controller detach 终止，不能与上一轮已验证的可持久化 plan draft 混为一谈。导航前没有明显警告此待确认操作将结束，属于需要评估的交互缺口，而不是审批数据丢失的证明。

### E03：content_revision 的真实拒绝和恢复路径

测试者用 apply_patch 在测试目录建立六行 `line-guard-probe.txt`，初始内容依次为 alpha、beta、gamma、delta、epsilon、zeta。外部变更步骤也使用 apply_patch，只改此测试夹具。

| 路径 | 实际调用和结果 | 磁盘结果 |
| --- | --- | --- |
| 未见行 | read_file 只读1–2行，使用返回 revision 编辑第5行 | `APPLICATION_ERROR / UNSEEN_LINE_RANGE`；未写入 |
| 旧版本 | 外部将第1行改为 external-alpha；模型不重读，仍用旧 revision 修改第1行 | `CONTENT_REVISION_MISMATCH`；外部变更保留 |
| 正常恢复 | 重读完整六行，以新 revision 一次替换第1、5行 | SUCCESS，`operations_applied=2`，返回 diff、新 revision、changed_windows |
| 批次整体拒绝 | 同次提交合法的第2行替换与非法的第99行替换 | `INVALID_LINE_RANGE`；第2行仍为 beta，无部分写入 |

错误码与成功返回均从数据库 EXECUTED tool result 二次核对，不只采用模型总结。最终六行是 ALPHA、beta、gamma、delta、EPSILON、zeta。未让模型自动补读/重试负例，以免成功恢复掩盖拒绝行为。本次不声称覆盖跨进程 CAS、磁盘故障或所有换行/编码组合。

### E04：双窗口接管、旁观同步与控制权恢复

新开同一应用第二个真实浏览器标签页，自动连接当前会话，显示“旁观中”，没有可提交的 composer。点击“在此窗口继续”后，新窗口取得控制权，原窗口短暂重连随后进入旁观状态。

新窗口发送只回复 `WINDOW_TWO_OK` 的请求，两窗口都显示同一回复。再从原窗口取回控制权，新窗口确实转为旁观；最后关闭额外测试标签页，保留原窗口可输入。

实际截图检查了原674×734窗口与新标签默认1280×720桌面布局，桌面侧栏、对话、任务区域均可读。没有通过调整视口掩盖布局问题；也未把本次接管成功当作底层 provider prefix 字节级专项验证。

### E05：三级子任务依赖链取消与后续调度

主模型通过真实 `create_agent_tasks` 一次创建 A→B→C：A 做20秒终端等待，B、C 分别依赖前一项且只允许输出固定标记。创建后主模型立即调用 `stop_agent` 停止 A。

最终页面与数据库一致：

| task_key | canonical 状态 | terminal_reason | 实际子任务 turn 数 |
| --- | --- | --- | --- |
| cancel_probe_a | CANCELLED | USER_CANCELLED | 1 |
| cancel_probe_b | BLOCKED_DEPENDENCY_FAILED | DEPENDENCY_FAILED | 0 |
| cancel_probe_c | BLOCKED_DEPENDENCY_FAILED | DEPENDENCY_FAILED | 0 |
| verify-budget-after-cancel | COMPLETED | 无 | 1 |

B、C 不仅显示“依赖未完成”，实际也没有启动子任务 turn。随后主模型另行创建独立只读验证子任务，成功读取 budget.txt、报告预算400并完成，证明本次失败链没有堵死后续任务接纳。未重建被取消的三项任务。

页面任务概览显示6项、0进行中、2需留意，与旧的两个已完成子任务、新的一个已取消、两个依赖失败、一个验证完成相符；依赖失败详情明确说前置任务未完成、工作没有开始。

### E06：空文件、中文含空格路径与 create-only

通过真实浏览器发送串行工具任务，仅操作 `空白 测试.txt`：

1. write_file 空字符串创建成功，`bytes_written=0`。
2. read_file 返回 `total_lines=0`、`file_size=0` 与真实空文件 revision。
3. edit_file 以该 revision 执行 replace_file，写入 `第一行\n第二行`，不添加最终换行。
4. read_file 返回两行、19字节。
5. 再以 write_file 写入 BAD，被 `APPLICATION_ERROR / FILE_ALREADY_EXISTS` 拒绝。
6. 最后 read_file 和测试者原始字节检查均确认原两行保留、最终没有换行。

上述六次工具结果均从 canonical EXECUTED records 核对。没有改用终端绕过 edit/write 语义。

### 第三轮收尾核验（13:40）

- 本轮新增10次普通用户输入（其中源会话一次、第二窗口一次），覆盖停止、切换、四组行编辑、双窗口、依赖链及文件边界；不是十次同类闲聊。使用 Playwright skill 的快照/定位和截图检查方法，实际操作通过真实浏览器完成。
- 双窗口探针的 canonical USER_MESSAGE 仅1条，没有因同步/接管重复提交。额外窗口已关闭，原窗口恢复控制权。
- 全库 `turns WHERE status='RUNNING'` 查询为空；六个子任务都已终结。进程检查未发现本轮 STOP_PROBE_3 或 sleep 20 测试进程残留。
- 测试目录最终六个文件：budget.txt、checklist.md、itinerary.md、permission-allowed.txt、line-guard-probe.txt、空白 测试.txt。四个拒绝/取消目标仍不存在：permission-denied.txt、readonly-blocked.txt、cancelled-plan.txt、switch-pending.txt。
- 浏览器 warn/error 日志末次抽查为空。截图已在测试对话内检查，未另存独立 PNG 附件。
- 数据库核验均使用根目录 `.venv/bin/python`、LocalSettingsStore 读取既有配置、psycopg 只读事务；磁盘核验读取测试夹具原始字节，未计算或登记额外文件哈希。补充使用 `ps` 检查测试进程、`git status --short` 检查仓库改动。
- 未运行自动化全回归；本轮是实际 provider/browser 交互测试。没有覆盖断电、跨进程原子竞争、长历史压力、所有模型或所有平台。
- 没有新增确定性缺陷编号：累计确认问题仍为 BUG-01～04；E01、E02 的停止/导航反馈语义单独列为待评估风险，避免把安全拒绝或新通知轮次误报为数据损坏。
- 只更新本报告；生产代码、生产设置和数据库结构未改，数据库未再 reset，未 stage/commit。Git 仍仅显示本报告及测试前已有 `output/playwright/cli-session/` 为未跟踪。服务和测试历史保留，便于继续复现。
