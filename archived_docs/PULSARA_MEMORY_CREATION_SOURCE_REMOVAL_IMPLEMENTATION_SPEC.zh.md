# Pulsara 记忆创建来源关联删除实施规格

状态：ARCHIVED / ACTIVATED，2026-10-23。实施、交叉审查及验证完成，按用户要求归档；包含后续确认采用的记忆页动效。

## 1. 产品决定与边界

记忆及记忆关系的生命周期归属于 memory domain 和 global/project context。永久删除创建它们的会话不影响记忆正文、关系和使用状态。删除记忆/关系指向创建 ToolResult 的持久关联，不保存用户原话、turn 请求副本或其他替代来源。

本规格覆盖 clean-v0 schema、写入结算、会话删除、记忆管理读取、前端和验证。完成审查并冻结后一次 hard cut；不增加迁移链、兼容字段、来源 tombstone、receipt、事件、任务或修复机制。历史规格的创建来源要求由本文替代，其他记忆语义沿用当前生产合同。

不修改 provider SYSTEM、tools 定义或已有输入前缀，不改模型工具参数/结果形状、不改变记忆 ID 构造，不修改存储的旧会话内容。工具结果正文中自然包含的 memory/relation ID 仍是普通历史记录，不构成记忆行到会话的反向生命周期依赖。

## 2. 当前实现与删除范围

- `memory_facts.created_by_tool_result_id`、`memory_relations.created_by_tool_result_id`，对应 FK、创建者唯一索引及关系来源索引。
- `enforce_direct_memory_lineage` 中依赖创建 ToolResult/session/tool arguments 的跨表回查及两个 lineage constraint trigger。
- fact seal 中创建来源置空例外；relation seal 中唯一允许的 FK 置空 UPDATE 例外。
- `_settle_remember`/`_settle_memory_relation` 的来源落盘；重复关系的 `require_existing_owner` 回查。
- 记忆管理详情的来源 JOIN、availability/locator、relation owner/write_tool/source、仅解释创建来源的 formation 等返回字段和辅助函数。
- 会话删除 `_DeletionSources`/`_sources` 中的记忆来源扫描、fact/relation 锁、来源集合重查与 FK 置空特例。
- 前端 `MemorySource`/`MemorySourceProjection`、`onOpenSource`、来源跳转状态和只为此存在的导航连线；不得删除批注回原文、任务定位、历史分页等独立功能。

## 3. 必须保留的写入一致性

复用现有 canonical ToolResult 接受事务与 `_settle_direct_memory_mutation`：

1. 当前写入必须来自合法 root 工具调用、成功的工具尝试及相同 memory domain/workspace。
2. host 准备的 mutation 与本次 canonical tool call 的名称、参数、正文规范化、kind、context、依据 ID 精确一致。
3. 记忆/关系变更和工具结果一起提交或回滚。已提交结果的 exact confirmation 沿用现有 ToolResult 机制，不能因后来记忆被用户修改或删除而再次执行 mutation。
4. `SAVED`/`ALREADY_PRESENT`、并发语义去重、关系幂等、矛盾无向去重、替代 lifecycle、response preference 容量、依据读取与范围检查保留。
5. ID 继续使用现有稳定构造；创建时借用已有调用身份推导 opaque ID 不要求保存或查询来源外键。

移除持久来源校验不代表新增任意直接写入 API；运行角色权限和唯一现有写入入口保持。

显式收敛保证边界：移除来源 FK 后，数据库不再独立证明一条任意 SQL INSERT 一定对应某次 ToolResult。生产写入的合法调用、精确参数和原子性由已有 repository 接受事务保证。不得新增 receipt、GUC 授权令牌、跨表证明或新持久事务记录以重建被删除的来源链；仍以真实事务失败/回滚、重复确认和既有权限测试证明生产边界。

## 4. 独立关系校验与 schema 减法

保留事实的 domain/context、kind、正文、编辑时间/语义摘要和生命周期约束；用户编辑正文仍只沿现有明确路径。

保留关系的两个 fact 外键、作用域约束、kind/替代模式/ordinal CHECK、自关联禁止、已有唯一约束及并发锁。原 lineage trigger 中验证端点真实 context/kind 的部分具有独立价值，必须保留为小型关系端点校验，复用同一 trigger 机制，不新造登记表或执行框架。

关系行保持不可修改；删除来源后原 UPDATE 置空通道不再合法。审查时确认复用现有 seal 函数收敛为拒绝 UPDATE，或由等价现有不可变规则覆盖；不能扩大 runtime 的 UPDATE 权限。不要顺手增加历史上没有保证的整张关系图重判或 fact-kind 修改能力。

函数/trigger 总量不增加；关系表、事件、subject、guard、job 分类保持不变。被移除的来源库存必须真实减少，不以别名保留。

## 5. 会话归档、删除与项目保留

归档原本不改记忆行；继续保持。删除会话后，所有记忆、关系、向量和时间字段应字节值相同，不再有来源置空写入。

删除会话仍需锁 session admission、session 行、验证 writer 身份和生命周期；保留现有物理关闭、条件删除及 deadlock/serialization 按操作 deadline 重试。

只保留被删会话本身 workspace 的必要锁与空 workspace 清理。空 workspace 清理继续同时检查剩余 sessions 和 project memory facts；最后一个会话删除后，只要还有项目记忆，workspace 和项目列表必须保留。不同会话并发删除、删除与记忆写入/删除竞争沿现有 workspace 锁序验证。

删除来源扫描专用的 plan/recheck，不为测试保留死辅助函数。相应竞争测试改在保留的真实锁边界同步。

## 6. API 与前端

管理详情不再返回 creation source、source availability/locator、relation owner 或来源说明。不返回固定 null/空对象代替旧字段。

用户截图冻结的界面变化：

- 删除记忆详情的“在对话中查看”按钮。
- 删除“关系”可见标题，保留关系图 region 的可访问名称、当前记忆、关系箭头、关联记忆卡片及点击卡片定位到另一条记忆的能力。
- 删除每条关系下整行“保存时建立/后续标定”“查看建立处”以及来源不可用说明；关系的 recorded_at 等独立语义保留。
- 编辑、删除、分页、关系删除/恢复预览与确认保留。
- 编辑后的提示仅说明记忆正文被用户修改，不再提来源会话；保留模型工具已有 user_edit 提示。
- 删除仅为上述 UI 存在的 CSS，并适当缩小图上方和底部留白，不遗留空行/分隔线。
- 用户追加的列表要求：每条记忆内部更紧凑；类别图标、时间和时间右侧小箭头统一对齐正文首行，窄屏也保留；回答偏好“通常随新一轮提供”用蓝色并前置表示详情的感叹号图标。冲突/已被替代等独立状态继续保持其语义。

会话页若仍有批注原文定位，应保留其内部消息定位、按条加载和过程展开能力。外部 memory 跳转 props/state/callback 与未被任何实际调用者使用的 scheduled entry 参数一起删除，不能留来源跳转空壳，也不能把通用 runtime 定位 API 当作记忆专用错误删除。

## 7. 审查结论与实施分工

GPT-6 Astra（high）核验后无冻结阻塞：移除持久来源不增加数据库证明机制；同 workspace 的删除/创建/记忆操作复用既有 workspace 锁序；exact confirmation 不依赖记忆仍存在；fork 不复制或重新归属记忆；`MemorySourceInvalidationReservation` 属于 provider 输入来源快照机制，与创建来源无关，完全保留。真实 direct-memory dogfood 收敛为明确激活必跑项。

冻结前只审查文档和代码。冻结后主代理负责 schema、repository、后端相关测试和 dogfood；审查代理可并行负责前端及对应测试，文件边界互不覆盖。两方交叉检查另一侧最终差异，发现边界先修正再激活，不为快速变绿删掉仍必要的断言。

## 8. 验证与激活

### 数据库与后端

使用仓库 `.venv` 和现有 disposable test database fixture，确认实际 PostgreSQL 用例运行，不接受以新增 skip/xfail 隐藏失败。测试库有独立随机名称，不重置未核验目标；保存配置只读。

- clean-v0 无两个创建来源列/FK/索引/旧 lineage 回查；剩余表/事件等 oracle 库存不增加。
- root/域/参数不一致、失败尝试、越权作用域仍被拒绝；ToolResult 与 memory mutation 的回滚和 exact confirmation 保持。
- 真实 PG 上关系端点 context/kind 错误拒绝，范围/自关联/ordinal/替代模式/重复矛盾等既有约束保持，关系 UPDATE 被拒绝。
- 并发相同 remember、重复 relation、SAVED/ALREADY_PRESENT，以及先后用户编辑/删除后的已提交工具确认。
- 删除 OPEN/ARCHIVED 会话后 facts/relations/embeddings 不变；global、同项目多会话、最后一个项目会话、fork 和有/无项目记忆的 workspace 清理。
- 会话删除、取消归档、writer takeover、两会话删除及记忆操作竞争保持原有结果，无新总量/寿命限制。
- 管理详情接口不包含来源字段；读取、关系分页、用户编辑、级联删除/恢复预览及记忆查询回归。

### 前端

MemoryView、PulsaraApp、WorkbenchView/批注、定时任务相关测试，TypeScript 和变更文件 ESLint。真实浏览器核验用户截图要求、空关系状态、多个关系、编辑/删除确认与窄屏；不触发真实记忆变更。

### Dogfood

修订现有 direct-memory dogfood 中依赖来源 JOIN 的统计，改用本次 canonical 工具结果已有 memory/relation ID 与独立记忆查询核对，不能新增持久映射。激活前必须运行现有 `tests/dogfood/run_direct_memory_dogfood.py` 全部现有必要场景，使用已保存且已配置的模型连接。沿 LocalSettingsStore / require_pulsara_home 读取保存配置并用 disposable 数据库注入，不导出凭据。缺少配置或 real-provider 验证未完成时不得标记激活，需报告实际阻塞。验证结果记录实际命令、通过/失败及范围，不记录逐文件内容 hash。

完成两方最终审查后将状态改为 ACTIVATED。本轮不包含 stage/commit、重新打包安装或发布，除非用户之后明确要求。

## 9. 实施结果与激活证据

2026-10-23：主代理审查前端删除路径、保留的批注定位和真实浏览器截图；GPT-6 Astra（high）反向审查 schema、repository、会话删除、测试边界和 dogfood，最终双方均无阻塞意见。没有增加表、事件、subject、guard、job 或替代来源证明机制。

已完成验证（Python 均使用根目录 `.venv`，PostgreSQL 用例设置 `CI=1`，不允许配置缺失时静默跳过）：

- `pytest tests/test_direct_advisory_memory.py tests/test_session_permanent_deletion.py tests/test_conversation_fork.py tests/test_session_archive.py tests/test_stage5_clean_migration.py -q -x`：113 项通过。覆盖 clean-v0/catalog/grants、无来源列、独立关系约束、回滚、域隔离、归档/删除/并发、最后一个项目会话及 fork 父会话删除后的共享记忆读取。日志 `/tmp/pulsara-memory-source-final-db.log`。
- 追加真实 worker root-only 拒绝用例后，完整 `test_direct_advisory_memory.py` 再跑：39 项通过。失败/缺失 attempt 在既有冻结候选类型入口拒绝；域/workspace/canonical call/kind/context 不一致由现有接受入口拒绝；已提交 ToolResult 在先编辑、再删除记忆后仍可 exact confirmation。日志 `/tmp/pulsara-memory-source-direct-final.log`。
- `test_memory_tool_result_rejection.py`、`test_memory_management_contracts.py`、`test_desktop_memory_management.py`、`test_direct_memory_preference_refresh.py`、`test_direct_memory_prompt_contract.py`、`test_direct_memory_related_fallback.py`、`test_memory_tool_surface.py` 和 `test_session_permanent_deletion.py`：51 项通过。最后一次删除测试包含 3 条 embedding 的完整前后比较。日志 `/tmp/pulsara-memory-source-contracts.log`。
- `test_stage2_conversation_runner.py` 中 bad citation、saved remember basis、root memory policy、provider wire hint 和 accepted steer 的 5 项相关用例通过。日志 `/tmp/pulsara-memory-source-runner-final.log`。
- 前端 PulsaraApp、MemoryView、WorkbenchView、response annotations、scheduled tasks、memory API 六套测试共 421 项通过。普通能力页导航恢复原有默认插件 tab，技能/MCP 的显式跳转仍保留目标，相关既有断言全部保留。日志 `/tmp/pulsara-memory-source-final-frontend.log`。
- TypeScript、变更生产文件 ESLint、Python Ruff、`git diff --check` 通过。真实浏览器核验桌面和 390px 窄屏、关系图及关联记忆跳转、编辑/删除取消、空关系状态；顶部图标/标题/时间/箭头对齐，未出现水平溢出。截图在 `output/playwright/memory-source-removal/`，只读模拟预览端口 5199；未操作用户真实记忆。

真实模型验证：原 `run_direct_memory_dogfood.py` 的全部场景通过，使用已保存的 `qwen3.8-flash / openai_chat_completions` 连接。9 次 final、8 条事实，验证 `SUPERSEDES`、`BASED_ON`、Q4 返回 3 条相关记忆加自身、Host 重启后读取 B 楼、删除 replacement 和项目依据级联删除。结果 `output/memory-source-removal/direct-memory-dogfood.json` 为 `passed: true`。

隔离配置说明：原保存设置缺 PostgreSQL admin/runtime DSN，直接运行先得到明确配置缺失。随后仍先由 `LocalSettingsStore` 读取保存模型配置，再将现有测试 harness 的已核验本地 DSN 通过 `replace(saved, postgres=LocalPostgresConfig(...))` 与现有 `_ReadOnlySettingsStore` 作内存注入；复用 dogfood 自带随机数据库创建/清理。没有修改保存配置、重置用户现有数据库或把模型凭据导出到环境。临时执行器 `/tmp/pulsara-memory-isolated-dogfood.py` 仅用于本次隔离运行，不是新的生产 fallback。

此激活记录证明新 clean-v0 路径；未给旧数据库增加在线迁移或兼容路径。后续打包/安装时仍须按仓库既有 clean-v0 数据库规则处理旧本地 schema。

## 10. 追加：记忆页轻量动效

按用户授权设计并实施，沿用项目已有的浏览器 View Transition 和 CSS 动效机制，不增加动画依赖、持久状态或计时卸载机制：

- 首次打开和关闭详情使用局部过渡：列表位置/宽度平顺衔接，桌面详情从右侧淡入/淡出，窄屏改为小幅垂直过渡。页面其他区域保持静止，过渡 180–240ms。
- 筛选后的新列表、空态、不同记忆的详情正文、编辑区和操作反馈使用 180–200ms、4px 的短淡入；分页追加不重播整张列表。
- 行箭头悬停前移 2px，关系卡片按下移动 1px，均不改变布局。删除确认复用已有的遮罩淡入和对话框入场样式。
- 不依赖系统减少动态效果设置。动画不延迟启动详情请求；沿用现有 request identity 丢弃过期结果，快速成功/失败均须等浏览器完成 pending 面板更新后再结算，避免迟到的 loading。关闭仍立即撤销旧请求的 UI 所有权，退出快照由浏览器管理，不保留可交互旧副本。

验证：MemoryView 37 项通过（含新增的浏览器延后更新与快速成功/失败两项时序回归），PulsaraApp 189 项通过，TypeScript、变更生产 TSX ESLint、diff 检查通过。真实 Chromium 确认桌面 `memory-detail-in/out` 和列表局部过渡实际执行；390px 展开/收起完成后无水平溢出，截图在 `output/playwright/memory-source-removal/motion-*.png`。本次只涉及前端，不重复无变化的数据库/真实模型测试。
