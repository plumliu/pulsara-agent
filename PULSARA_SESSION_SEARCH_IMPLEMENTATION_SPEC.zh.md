# 会话搜索与命令面板移除

状态：已实施并验证，2026-09-30。

## 边界

搜索同 memory domain 的全部项目会话：显示标题、ROOT 用户消息/steer 的文本（包含用户批注 comment）、ROOT assistant 的 TEXT blocks，覆盖最终回复和中间 commentary（包括携带工具请求的消息）。代码块属于文本；不读 DATA/思考/provider replay、工具参数/结果、附件正文、批注引用来源或子任务内部记录。只搜索已持久接受的内容，正在流式生成但尚未提交的文本不在结果内。fork 已导入的用户/助手文本按子会话自身 canonical ownership 搜索。

以空白分隔关键词，大小写不敏感字面匹配，所有词须在同一标题或文本片段中出现，不解析正则或通配符。每会话一个结果；标题完全匹配、标题包含、正文匹配依次排序，同类按活动时间降序、session id确定顺序；正文取最近命中的文本块。空查询先按所选生命周期筛选，再按活动时间取最近5个会话（用户指定的预览数量），不提供加载更多，不扫描正文；关键词搜索仍完整分页，不限制历史总量。

弹窗采用紧凑布局：筛选与搜索输入同一栏，复用输入框模型/推理选择器的mode-chip与menu-popover样式，使用无边框文字和小箭头、选中项勾选的紧凑菜单（选项行高24px、上下内边距3px）；支持菜单方向键、Escape关闭菜单与外部点击收起。不显示快捷键页脚。空查询显示小字“最近会话”，复用能力页说明文字样式；初始及关键词/筛选切换后都不默认选中结果，方向键或鼠标聚焦才选中。结果标题、项目、活动时间和归档标记在同一行；“已归档”位于标题前，使用无填充的紧凑红色圆角描边标签，与标题间距4px，复用输入框“完全访问”的红色变量，不增加结果行高；正文命中另显示一行原文片段，不展示用户/助手等来源标签，标题命中不重复展示标题片段。前端将摘要定位到首个关键词，保留前12个Unicode字符及其后文，高亮命中词，避免单行省略把命中词藏到右侧；此处仅限制展示上下文，不限制搜索正文。片段240字符是展示预算，不是搜索截断。首版支持全部/活跃会话/已归档筛选（活跃会话对应OPEN，即所有未归档会话，不仅是正在生成的会话），默认全部项目全部会话。后端每页20，最大50是单次列表展示/响应预算，不设会话/历史总量上限。显式加载更多，不预取所有正文。游标绑定query与lifecycle且重新校验domain；是分页位置，不是权限凭据。跨请求为实时列表，新增内容可能改变排序，刷新重新开始。

移除左侧命令按钮、旧CommandPalette、旧样式和快捷命令项。侧栏入口显示“搜索会话”，⌘K/Ctrl+K全局打开/关闭新搜索弹窗；保留独立⌘N等已有功能。结果支持键盘上下选择、Enter、Escape、焦点回收、输入法合成、防过期异步响应。输入去抖200ms仅减少请求，不限制任务。无结果、失败、加载中分别显示；失败不伪装为空结果。

点击普通结果沿现有会话打开路径；已归档结果先展示明确“取消归档并打开”操作，必须用户点击才调用现有unarchive接口，不因搜索/选择隐式恢复。恢复失败保留弹窗。打开前重新读取会话，已删除/再次归档给出具体错误，不复活。

## 所有权与实现

复用canonical关系与现有prompt codec、PostgreSQL server cursor：单次查询流式读取已授权session的候选内容，Python按现有内容类型解码、匹配，按会话聚合并仅保留一页候选。PG持有一致读事务与查询期限；应用循环复用Host已有canonical deadline，超时返回可见错误，不返回不完整结果。没有本地持久索引、事件、任务、影子正文、模型调用或新schema；不改provider prefix。首版读取复杂度随历史文本增长，明确接受局部应用初始规模的直接查询成本；后续若实测需要索引再单独定义可丢弃搜索投影，不能用隐形历史截断替代。

API：POST /api/sessions/search，严格JSON {query,lifecycle?,cursor?,limit?}；返回items与next_cursor。Host提供冷读取，不启动模型/会话/MCP。Web沿现有Origin/Host与memory domain保护；前端RuntimeAdapter传AbortSignal。旧请求中断仅取消客户端等待，后端物理读取受现有deadline约束。

## 验证

真实PostgreSQL验证标题、用户、final/commentary、工具请求中的TEXT、排除项、blob正文、Unicode/字面符号、多关键词、重复聚合、分页、域隔离、归档/删除、fork与空查询；HTTP严格字段和错误；前端入口替换、快捷键、过期响应、键盘/IME、归档显式恢复及打开链路。TypeScript、ESLint、相关Python检查与前端构建。使用当前保存数据库作只读搜索检查，禁止为验证修改用户历史。

### 本次验证结果

- `pytest -q tests/test_session_search.py tests/test_session_title.py tests/test_session_archive.py tests/test_local_web_http_surface.py`：42 passed；包含真实 PostgreSQL 与真实 Runner 的工具结果、子任务排除检查。
- Vitest：`components/session-search-dialog.test.tsx app/pulsara-app.test.tsx lib/runtime-adapter.test.ts`，217 passed；包含从初始侧栏尚未载入的会话结果打开、⌘K、IME、过期请求、分页重试、归档显式恢复。
- `tsc --noEmit`、ESLint（0 errors，5条现有warnings）、相关 Python Ruff、`build:local` 通过；构建保留已有chunk体积提示。
- 经 LocalSettingsStore / require_pulsara_home 读取当前配置，使用已验证数据库连接作只读查询。当前1个会话：空查询1条、`OpenAI`命中assistant 1条、无匹配词0条，三次分别10.2/11.7/11.0 ms。此小样本不代表大历史规模性能。
- Playwright 在实际服务上验证关键词高亮、原文片段、空结果、背景inert与原命令入口移除；没有提交消息、修改标题、归档或删除用户会话。
