# 桌面迁移验收记录

日期：2026-10-18。当前机器：macOS ARM64。Linux x86_64/ARM64 已提供原生构建工作流，本机未运行 Linux 桌面，不能视作已经验收。当前构建未签发布证书或公证。

## 交付边界

- 正式 UI 仅 Electron；旧 Web 启动参数、浏览器回退、Next/vinext/Cloudflare 入口和 Python 原生选择器已删除。内部 loopback HTTP 继续服务桌面渲染与现有协议。
- Electron 44.9.0、electron-builder 26.15.3、uv 管理的 Python 3.12、PostgreSQL 17.9、pg_search 0.26.1、pgvector 0.8.1。macOS 资源构建目标为 15.0，Linux 为 Ubuntu 24.04。
- 左栏按实施中用户反馈恢复原深色图标栏及暖色会话列；桌面标题栏、右侧实例标签保留。
- 模型循环、provider 输入安装/追加、工具执行/结算、任务调度、会话上下文及 compaction owner 均未重写。新增进程内任务仅负责宿主控制通道读取；未引入持久执行恢复系统。

## 已执行验证

### 真实打包应用

使用独立临时 Pulsara home，读入用户已保存的模型设置进行真实任务；原设置、原外部数据库和 test8 文件包未改动。应用使用随包 Python/PostgreSQL/扩展/Chromium。

- 从新 home 初始化、进入工作台、创建会话；内置数据库 `db status` 为 up_to_date，`db verify` 为 verified。
- DeepSeek Chat Completions high 完成文件写入/读回、CSV 合计、终端命令、HTML 截图和两项并行核对任务；运行中刷新工作台后继续完成。
- 实际 test8 入口 HTML → 学员 HTML → ACCESS-07.md，在同一文件标签完成；后退/前进可用。显式打开第二份 Markdown 标签，关闭后第一份仍可读取。
- 原生系统文件选择确认真实绝对路径；通过 Chromium 原生 DragData 投递真实磁盘文件与目录，分别得到唯一的原文件/原目录引用。向实际页面注入 PNG ClipboardEvent 后生成 Figure 引用；此项验证页面到附件 owner 的链路，不声称验证了所有系统截图工具的剪贴板格式。
- 同时保留任务组、两个独立任务详情和文件标签。任务节点在组内选择，详情的“在标签中打开任务”打开独立实例。
- 随包 `pulsara app` 返回并聚焦已有实例。明确退出后应用、Python 内核及专用 PostgreSQL 退出；不存在遗留的 postmaster.pid。
- 通过 macOS LaunchServices 再次打开最新应用包，同一 home 的会话、真实任务结果与交付文件仍在；恢复后的左栏已在实际桌面目视检查。随包 `config-check` 返回数据库 ready。
- 用另一个独立临时目录实跑 BundledPostgres：首次启动、停止重开保留数据、明确 reset 后清除数据库表，目录外标记文件保留，最终停止成功。
- 顶栏与复制修订后重新打包并在同一验收 home 启动：窗口控制区、会话栏和右侧分类栏共用 44px 顶栏；具体实例标签位于下一行，文件操作栏在实例标签下方。实际从能力列表打开 `find-skills/SKILL.md`，点击原生复制按钮，再在空输入框粘贴，得到完整原路径；验收文字随后清除，未发送给模型。源码切换、HTML 入口到 Markdown 手册的同标签导航也已实际验证。

### 定向与全量测试

- 前端全量 874 项通过；包括历史分页/定位、引用、分叉、任务组独立成员、原控制引用查询、会话删除清理与文件导航失败边界。左栏恢复后 3 项定向测试及桌面构建通过。
- 本次顶栏与原生剪贴板修订后，前端全量 880 项通过，TypeScript 检查和桌面打包通过，宿主 8 项测试通过。连续 HTML 导航测试等待新 iframe 实际装载对应页面后再发起下一跳，保留原导航范围和结果断言。
- Python 非 PostgreSQL 全量运行：2,997 项通过；一个新增控制通道任务点的架构 oracle 已按有效规范补齐后通过。另两项 identity golden 失败在未修改 HEAD 也复现，未修改断言或 golden 来消除失败。
- 后端/CLI/生命周期定向测试、预览与 bridge 30 项、预览/Markdown 前端 29 项通过。最新 CLI 定向 22 项通过。
- 宿主 8 项测试通过：真实子进程收尾等待、并发启动/停止、异常 IPC、非零退出、AppImage 头less CLI 参数与退出码、数据库等待失败后的实例所有权。Debian CLI 安装/卸载所有权测试通过。
- 已准备资源整体搬移到不同路径后，官方 PostgreSQL 工具可运行；实际 CREATE EXTENSION、BM25 查询命中 1 条、向量距离为 0；随包 Python 导入与 Chromium 启动成功。全部 Mach-O 架构和最低系统版本检查通过。

## 证据位置

开发现场截图在 `output/playwright/desktop/`，原生资源 smoke 输出在 `/tmp/pulsara-resource-smoke-final.log`。未修改 HEAD 的 golden 复现输出在 `/tmp/pulsara-desktop-baseline-779ul3bl/result.txt`。这些临时路径不作为产品依赖，也不作为持久执行恢复记录。

## 发布前仍需执行

Ubuntu x86_64/ARM64 的原生包构建和桌面运行，使用 `.github/workflows/desktop-package.yml` 及真实 Ubuntu 桌面完成；发布签名/公证另行配置。不以本机或后端测试替代这些平台声明。

## 紧凑布局与展开动画复验

- 会话正文 13px、二级标题 17px；工具摘要标题 11.5px，双行摘要实测约 41px。右侧文档预览保持自己的阅读字号。顶部左栏和会话之间增加 1px 分隔线。
- 右侧“能力、任务、后台进程”和收起按钮保留在窗口顶栏，实例标签在下方单独排列。分类/实例结构测试 7 项通过。
- GPT-6 Astra high 子代理实现并交叉检查展开逻辑；前端全量 886 项通过。实际 Electron 中逐帧采样：处理过程与工具气泡展开均有连续高度变化，点击按钮最大垂直位移为 0；收起后昂贵内容已卸载。正常跟随、显式回到底部、历史锚点和退场期间不触发隐藏加载由定向测试覆盖。
- 修复 macOS 收起侧栏后拖动区域覆盖按钮：使用实际布局留位，而非仅增加内边距。原生按钮反复展开、收起、再展开通过。
- 右侧调宽与自动收起复验：1440px 窗口、314px 左栏时，最大面板宽度为 766px，右边界始终为 1440px；修复前会伸到 1682px。向右越过收起阈值后面板隐藏，再次打开仍选中 `guide.md`，恢复拖动前 766px 宽度，随后正常调回常用宽度。拖动、捕获释放及保留标签的定向测试通过（该文件共 8 项）。
- 收紧会话间距：正文 13px、实测行高 20.54px（1.58 倍），段尾 6px；末组回复与输入框之间实测约 10px，原为约 74px。右侧文档预览字号保持独立。当前 macOS 桌面包已重新构建并在现有会话验收，TypeScript 检查通过。

## 2026-10-19 间距与侧栏动画调整

- 助手正文保持 13px，行高改为 1.52（实测 19.76px）、段尾 5px；末组回复至输入框的底部留白改为 24px（实测约 23.8px）。用户消息单独使用 14px、1.7 倍行高（实测 23.8px），保留原输入空行，气泡内边距为 12px / 14px。
- 左侧导航和右侧工作区由 Chromium CSS transition 实现 220ms 展开/收起，右侧拖动阶段禁用宽度过渡，达到阈值时回到同一收起路径。右侧保留固定阅读宽度的内容容器，收起的视图通过 inert 禁止交互；左侧也设置 inert，收起不改变运行任务。
- 实际 macOS 应用逐帧采样：左右展开/收起均有超过 20 个不同宽度样本，最终分别回到 314px / 62px 和可用面板宽度 / 0；静止后无残留动画。系统减少动态效果下两侧过渡时长降到全局规定的近零值。
- 工作区定向测试 8 项，文件预览与检查器测试 25 项通过，TypeScript 检查与桌面构建通过。现有桌面应用已更新；拖动捕获释放、阈值收起及标签保留由工作区回归测试覆盖。

## 2026-10-19 选区在版心外松手与字号微调

- 修正批注菜单的 pointerup 判断：松手落点在正文外时也重新验证浏览器选区，不因落点在空白区强制隐藏；继续由既有选区映射拒绝无效范围。
- 实际桌面页面复现旧逻辑：有效文字仍被选中，向 `.thread-scroll` 空白容器派发 pointerup 后菜单由 1 个变为 0 个。更新后同一事件路径菜单继续可见；清除选区后菜单正常移除。此验收通过 DOM 事件验证，未新增或发送用户消息。
- 批注 UI 11 项和选区映射 24 项测试通过。助手字号微调至 13.5px，行高实测 20.52px；输入框上方留白为 30px，当前页面实测约 30.5px。用户消息仍为 14px、1.7 倍行高。当前桌面包已更新。

### 2026-10-19 正文字号、最后标签收起与能力页布局

- 用户消息从 14px 调至 16px，助手正文从 13.5px 调至 15.5px，正文标题相应增加 2px；变更限定在消息正文，工具摘要维持 11.5px。既有行高比例、段距与输入框上方 30px 留白保持。
- 关闭当前会话最后一个实例标签会触发现有工作区收起动画；关闭其他标签不收起，也不改变任务、后台进程与能力运行态。测试覆盖另一会话标签仍保留、读取资源只释放一次。
- 能力页原本在三列工具栏内直接放置四个控件，使“重新加载会话能力”挤进 38px 列。现在将显式重新加载与显示刷新放入同一动作组；窄布局让分类另占一行。两个按钮的调用边界保持独立。
- 工作区与能力页 16 个相关测试通过，前端 TypeScript 检查、桌面构建和打包通过。
- 同一临时 home 的实际 macOS Electron 应用验证：助手正文 15.5px / 23.56px 行高，用户消息 16px / 27.2px 行高，工具摘要 11.5px，消息区底部 padding 30px。插件、MCP、技能、钩子四个分类的工具栏均保持正常横向布局，工具栏高 52px，无竖排按钮或刷新掉行。截图：`output/playwright/desktop/capability-toolbar-fixed.png`。
- 实际工作区从四个标签逐个关闭：剩余 3、2、1 个时保持打开，最后一个关闭后 is-open 移除，动画完成宽度为 0。未发送模型消息或执行任务控制。

### 2026-10-19 自动 CLI 入口与通用设置整理

- 安装产物首次启动自动注册用户级 CLI，后续启动更新自己创建的启动器；deb 系统入口继续由包管理器管理。移除手动安装 IPC、设置按钮及菜单项。Node 文件接口负责创建/替换入口；旧符号链接不会被写穿，不运行发现的命令或 shell 配置。冲突只在用户明确点击替换后处理。
- 通用页本地服务卡片合并 PostgreSQL、Pulsara 家目录、命令行入口三项；移除本地服务导航分类及页面连接行。PostgreSQL 只显示检查数据、重置数据，检查结果使用通知，保留既有重置确认与失败处理。CLI 压缩为状态、路径与按钮，仅异常时补充必要提示。
- 桌面 11 个测试通过，涵盖入口自动创建、应用移动后更新、执行权限修复、旧 uv 符号链接/悬空链接保留及显式替换、deb 所有权、PATH 遮蔽以及实际 AppImage 启动器参数与退出码。前端设置/CLI 15 个、记忆页 35 个和设置导航相关 3 个测试通过；TypeScript 检查、构建和打包通过。
- 实际 macOS 应用启动后识别 `/Users/plumliu/.local/bin/pulsara` 指向已有 uv 安装，状态为 conflict，未覆盖该入口。设置导航只有通用、模型、已归档会话；本地服务包含三项且 CLI 无独立大卡片。实际检查数据库返回成功通知，重置确认弹窗取消后未执行重置。截图：`output/playwright/desktop/general-local-service-compact.png`。
- 当前 Linux 分发行为由上述启动器/文件系统测试覆盖，未在本轮执行 Linux 原生桌面安装验收。
- 实际点击会话能力页“技能覆盖规则 → 用户级 Pulsara 目录”，进入通用页，家目录获得焦点与高亮，展示同一有效 Pulsara home；未保留旧 service 导航分支。

### 2026-10-19 命令行冲突详情交互

- “重新检查”增加与数据检查一致的刷新图标；“已有同名命令”变为红色警告按钮，带展开箭头、键盘焦点和 aria-expanded/aria-controls。
- 复用 AnimatedDisclosure 向下展开详情：一句冲突原因、命令入口/当前指向、替换影响。收起时详情隐藏且不可交互；尊重减少动态效果设置。展开不调用 CLI 检查或替换接口。
- 2 个定向组件测试、TypeScript、前端构建和桌面打包通过。实际 macOS 应用点击确认红色为 rgb(182,95,86)，刷新图标存在，展开高度连续变化，收起后 aria-expanded=false 且详情不可见。截图：`output/playwright/desktop/cli-conflict-details.png`。未替换用户已有 uv 命令。
- 后续布局微调：警告移动到命令行入口标题右侧；路径使用与家目录相同的整行禁用输入框。实际应用测得两输入框宽 670px、高 36px、左边界相同，背景及 8px 圆角一致；警告与标题垂直中心一致。TypeScript 与桌面构建通过。截图：`output/playwright/desktop/cli-path-input.png`。

### 2026-10-19 用户授权实际重置 CLI 入口

- 同名冲突按钮文案缩短为“重置”，继续保留命令入口作用域及展开说明，数据库仍使用独立“重置数据”按钮。
- 用户明确授权后在实际应用点击命令行入口“重置”：状态从 conflict 转为 ready，红色冲突详情消失，路径显示 `/Users/plumliu/.local/bin/pulsara`。该位置从指向 uv 工具的符号链接改为可执行启动脚本，调用实际 `.app/Contents/Resources/python/bin/python3 -m pulsara_agent`；旧 uv 安装文件仍保留。
- 终端 `command -v pulsara` 命中上述入口，`pulsara --version` 返回 0.1.0，`pulsara --help` 返回桌面版命令集，均正常退出。以本轮临时 home 执行 `pulsara app` 返回成功，原应用调试端口的主进程 PID 前后相同，未替换当前实例。未操作数据库重置。
- 2 个 CLI UI 定向测试及构建/打包通过。截图：`output/playwright/desktop/cli-reset-ready.png`。


### 2026-10-19 真实 home、DeepSeek Responses 与可视化重启验收

- 运行实际 macOS 桌面安装产物，使用 `/Users/plumliu/.pulsara` 及其桌面专用 PostgreSQL；沿用保存的模型连接，未改凭据、未重置数据库。旧 uv `pulsara-agent` 已卸载，桌面 CLI 启动器保留，`pulsara app` 可正常启动。
- 会话 `session:hk7ud7dvpyxqz4csc3z3hqksuq`，标题“DeepSeek Responses 真实环境验收”，选择 DeepSeek V4.1 Flash 1017 / Responses / high。任务创建订单与退款 CSV、Decimal 对账脚本、5 个 unittest、JSON/CSV 结果、Markdown 报告和离线 HTML。净额 619.99，异常 2 条；模型实际运行测试，外部独立复跑 5/5 通过。
- 通过正常退出和 `pulsara app` 重新启动，打开原会话并展开历史终端输出，原始消息、最终回复及实际测试输出仍可读；随后在同一会话继续生成 PNG、SVG、Mermaid 与交互 HTML，验证重启后的模型续写和工具执行。
- 四类显示均通过实际页面检查：本地 Markdown PNG 按现有合同显示引用按钮，点击在文件标签预览；通过 `pulsara-visualization` 发布 PNG 后可在聊天内嵌显示。SVG 与 Mermaid fenced block 正常渲染，Mermaid 放大查看正常。HTML 内嵌与当前文件预览均可切换净额/毛额，总计 619.99 ↔ 750.39，地区数据和柱高同步变化。
- 发现并修复迁移新增的图片“在标签中打开”按钮漏样式，复用 HTML 可视化右上角图标按钮样式；仅改展示，不改发布、模型上下文或执行语义。20 个 inline-visualization 测试及前端构建、桌面打包通过。
- 再次正常退出、更新产物并通过桌面 CLI 启动。54 条记录恢复，PNG/SVG/Mermaid/HTML 重新显示，HTML 切换再次成功。前后只读数据库核对一致：27 次模型调用全部为 `openai_responses` / `deepseek-flash` / `COMPLETED`；24 条工具结果中 23 SUCCESS、1 APPLICATION_ERROR，后者是依赖探测命令的 `which rsvg-convert qlmanage sips` 返回非零，并未阻断后续执行。两个冻结可视化（dashboard.html、net_by_region.png）均为 READY。
- 交付目录：`/Users/plumliu/.pulsara/workspaces/quick-20261019-011557-dbb0d744`。只读核对记录位于 `output/playwright/desktop/deepseek-real-before-final-restart.json` 与 `deepseek-real-after-final-restart.json`。本轮没有验证 Linux 桌面或其他未覆盖的图表类型。
