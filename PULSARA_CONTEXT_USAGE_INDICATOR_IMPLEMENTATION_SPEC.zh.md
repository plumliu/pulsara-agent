# 上下文占用圆环实施规格

本规格落实用户新增的桌面界面需求及随后删除解释文案的修订，扩展 provider usage 实施规格原先排除的 GUI 展示范围。计量、模型切换、压缩和 provider prefix 的执行契约继续由现有 owner 负责。

## 用户行为

- 在会话输入框的模型选择器左侧显示小圆环，采用 Pulsara 的主题色和悬浮卡片样式。
- 鼠标悬停、键盘聚焦或触屏点击可查看“上下文占用”、约占百分比、用量及额度。卡片不展示压缩策略、计量来源或其他解释文案。Escape、失焦或移开鼠标关闭。
- 数字始终标为“约”。同一 epoch 的兼容供应商 input usage 可以校准历史前缀；新增内容仍是估算。切换模型后不能沿用旧模型的分词结果。
- 分母是所选模型的有效输入额度，已经扣除回复预留、安全余量，并遵循现有单次输入限制；不是模型宣传的完整共享窗口。
- 超过现有 `auto_trigger_ratio` 阈值时标红，继续展示比例与额度。压缩只由实际发送时的 runtime 决定。
- 未达到阈值时更新普通圆环。未知、首轮尚未发送或运行中无法测量新模型时显示灰色状态，不伪造 0%。

## 测量范围与边界

`KernelHostSession.read_context_usage()` 提供 ROOT 会话的只读、可丢弃预览。HTTP GET `/api/sessions/{session_id}/context-usage` 经 LocalSessionController 仅读取已有 live handle；会话尚未打开、正在关闭或生命周期操作进行中时返回未知，不主动恢复会话。正常打开会话的现有 owner 独自负责恢复、hooks、MCP 和 Host writer。

空闲时复用 ProviderDispatchCoordinator 的非执行语义投影和实际 adapter wire quote，包含最新完成的 assistant 内容及 replay。相同目标复用已安装 prefix 的追加投影及兼容 usage anchor；目标变化时用目标模型重新生成脱离执行权限的预览，回到该模型的启发式估算。

预览不发送 provider 请求、不借用物理工具、不安装待生效 MCP 工具面、不运行 hook、不触发新 memory recall 或 preference refresh、不安装新 epoch、不更新现有根与 anchor，也不生成 usage 记录。原有压缩 source projection 默认行为不变，只有显式只读预览跳过工具 safe point 更新和这些新一轮 memory 激活。

运行中同一模型只读已安装请求，可使用兼容的最新 usage anchor；当前尚未落盘的回复不计入。运行中改选模型时，当前调用仍使用原模型，新模型预览显示待更新，本轮结束后重新测量。压缩过程中显示待更新，避免把旧 epoch 当作压缩后的结果。

未发送草稿、未来新消息、未来 memory 与 hook 注入、执行前的资源和能力变化不在预览范围内。因此发送时仍运行完整的现有准入、逐级压缩与模型交接流程；圆环不是执行许可，也不提前触发压缩。能力不兼容或查询失败显示未知，不从错误推断一定能自动压缩。

## 前端与刷新

RuntimeAdapter 提供可取消的 HTTP 查询。ContextUsageIndicator 按会话及完整 ModelCallBinding 区分结果；切换时立即停止展示旧模型数字，取消旧请求，并拒绝迟到结果。

会话、Host 实例、模型绑定、模型配置、canonical event sequence、运行状态、压缩开始与结束变化时重新读取。悬停、聚焦或点击打开卡片时再次读取，以覆盖 visible reply 后才完成的 usage 结算。无需新增轮询定时器或持久化 UI 缓存。

悬浮卡片复用项目已有 `@floating-ui/dom` 的 fixed、flip、shift、autoUpdate，portal 到 document body，防止输入框裁剪及窄屏越界。使用现有 CSS 主题变量，同时支持键盘、触屏和 reduced motion。

## 不增加的状态

不新增数据库表、durable/live event、subject slot、append guard、product relation、job、fingerprint、恢复链路或生命周期上限。HTTP 返回与组件内观察值均为可丢弃投影。模型切换仍只在下一轮实际发送时执行现有获准边界。

## 验证

- 真实 Host + PostgreSQL + 确定性 provider 流：首轮未知、最新 assistant 后缀、同模型 anchor、切到另一模型的启发式估算、较小模型压缩阈值预测、禁用压缩、运行中切模待更新、实际压缩后重新估算；查询不额外调用 provider、不安装 prefix、不替换 anchor、不写 usage。
- HTTP、Controller 与 RuntimeAdapter：选中会话路由、返回内容、URL 编码与 AbortSignal；未知会话不恢复、生命周期操作不读取 Host、读取期间退休的结果丢弃。
- React：比例、精简数值卡片、红色状态、超过 100% 的圆环绘制截断、未知状态及清除旧数字、输入框保持焦点时悬停卡片的 Escape 关闭、切模迟到响应与完成后的刷新。
- 前端全量测试、TypeScript、ESLint、Python 相关压缩及 usage 回归、Ruff、生产静态包构建。
- 真实浏览器使用隔离数据库和确定性 provider 流检查普通、切模和红色悬浮状态，以及窄屏布局；不修改用户保存的生产配置。

2026-10-04 critic 复审认可冻结。审查发现并修复了预览安装 MCP 工具面、悬停时 Escape 关闭失效、指标 GET 主动恢复会话三项问题。最终前端全量 606 项、后端相关 221 项通过，TypeScript、ESLint、Ruff、diff 检查及生产静态包构建通过。
