# 模型上下文额度实施规格

依据本次用户要求及随后确认的完整三级复用：目录模型的上下文额度可在 256,000 tokens 到 models.dev 声明的 context 上限之间调整，输出额度继续采用既有供应商最大值。

## 产品与所有权

- models.dev 配置新增与修改表单提供上下文额度数字输入，显示目录上限和只读最大输出。未设置时采用目录最大值；空白恢复默认，明确设置的值保持固定。
- `ModelConnectionConfig.context_window_tokens` 保存可选输入策略。catalog 继续拥有物理 context/input/output 上限；不修改 `ModelHardLimits`、`ModelContextLimits` 或输出预算。
- 前后端校验整数范围 `[256000, catalog.total_context_tokens]`。目录变化使明确设置值越界时配置不可执行，不偷偷截断或改写保存值。
- 自定义服务继续使用现有用户声明能力来源；本次不改其声明协议或输出设置。

## 输入预算与输出不变

`resolve_model_target` 在既有 `ResolvedModelContextBudgetFact` 中冻结有效输入预算：`min(provider max_input_tokens, provider total_context_tokens - 1, 选定额度 - 1) - 既有 input_safety_margin_tokens`。未设置额度等于采用目录物理窗口。既有每次调用输入边界仍在其原 owner 处取最小值。

provider 的物理 context/input/output limits、`default_output_tokens`、`effective_output_tokens` 保持原值。Chat/Responses 输出计算函数不改：只有真实物理共享窗口和实际输入占用可以减少输出。用户选择 256K 不得形成新的输出 cap；384K 或 output==physical context 等目录输出值仍须保留。连接测试保留原 64-token probe 边界。

## 完整三级目标配置交接

有效输入预算属于被冻结目标配置，在既有 `ResolvedModelTargetFact` 中保存规范化的 `context_window_tokens` 和由其推导的 context budget，不新增目标 DTO、兼容性 hash 或交接状态。没有实际收窄输入预算的额度规范化为 `None`，使等效设置产生相同目标。target 校验仍要求预算严格等于物理 limits 与冻结额度推导值，不放宽为仅检查小于物理上限。有效预算变化与模型变化一样使 target bundle 不同，进入现有三个层级：

1. Tier 1：按新目标预算做目的端 cold projection 预检，仅选择是否尝试直接交接。运行中最终调用仍由普通 preparation 收集完整来源并测量一次；在交接触发阈值以下且通过原有 wire/resource 准入时，批准、安装与执行绑定同一份最终 candidate。预检不能批准另一份重新收集的输入。最终输入未通过时按已冻结的旧源/新目的端进入原有 Tier 2/3；下一轮 ROOT prospective family 沿用其同一候选测量和采用 owner。
2. Tier 2：使用已安装目标的旧冻结预算和 prefix 生成摘要，按新目标预算验证 successor 并采用。
3. Tier 3：仅在既有允许的 typed outcome 下回退，选取新目标能容纳的最长完整对话后缀，使用新目标和新预算生成摘要；再通过原有 successor 准入后采用。第三级仍可能因不可容纳的必要内容、provider 错误等失败，不承诺任意输入必定成功。

同一 connection/model 的额度调整是本次用户明确选择的目标配置交接产品路径。Tier 1 的显式交接 cold epoch 和 Tier 2/3 的 adopted compaction successor 复用现有获准边界；不得直接改写已安装 prefix。交接前原 epoch 的 SYSTEM/tools 保持逐字节一致，messages 只追加。新 epoch 的 usage anchor 按既有规则清空。若不同设置产生相同有效预算，则目标未变，仍继续原 epoch。

`ModelRuntime.resolve_frozen_target_bundle` 将已安装 bundle 的规范化旧额度传回既有 target resolver，再精确核对完整 target fact、连接、reasoning、projection 和 estimator。保存的新额度不覆盖已冻结调用，也不剥夺 Tier 2 使用旧预算的权限；物理能力或身份漂移继续失败并按原三级规则处理。

运行中的纯上下文额度变更，在工具后续调用的既有安全点进入同一三级流程；判断只允许 context_window_tokens/context_budget 的差异，不将身份、物理能力、reasoning、projection 或 estimator 漂移扩大为中途切换授权。

已接受但等待执行的子代理任务拥有其持久化冻结目标。launch 和第一调用的所有 preparation（包括压缩源）恢复该额度，并精确验证其完整目标与 reasoning；保存设置变更不能令排队任务无故失败。第一调用之后，在普通安全点处理新额度，不新增排队任务状态或执行恢复机制。

普通自动与手动压缩保留已安装目标的冻结额度和普通调用所选 reasoning，摘要继续使用原有摘要 reasoning 规则；dry successor 与最终采用 successor 使用同一普通目标。压缩本身不替代额度交接，后续安全点再进入三级。Tier 2 仍由其原 owner 选择摘要 reasoning，不借普通压缩绕过此边界。

冻结目标契约硬切到 `resolved-model-target:v9`，clean-v0 的 subagent target JSON 约束和预期 schema catalog 同步更新，不加入 v8 双读、兼容分支或在线迁移。

删除独立“摘要放宽输入预算”的分支，不修改 compaction summary 的 compile binding 或 proof 契约，不新增普通压缩 fallback。ROOT、SUBAGENT、运行中后续调用和下一轮发送共用既有交接 owners。

## 界面与状态

配置保存不提前发请求或压缩。下一次调用在现有 preparation 边界解析新预算。只读圆环按新目标目的端投影重新测量，达到阈值标红；运行中尚未交接时显示待更新。沿用模型配置刷新、取消和迟到响应防护，无新增轮询。

不新增数据库表、durable/live event、subject、guard、relation、job、fingerprint 或总量上限；复用设置存储、HTTP、冻结、交接、压缩与 divider。

## 验证

- 配置 codec、API 保存/读取、上下界、bool/小数/越界拒绝、目录缩小后的不可执行状态，以及目录输出覆盖拒绝。
- 两种 wire API，覆盖输出大于用户所选额度的 384K、旧 cap 以上的 128K 和 output==physical context；新旧冻结预算再现而 provider 输出不变。
- 真实 Host 同模型调整额度：Tier 1 直接交接、Tier 2 旧预算摘要、Tier 3 typed fallback 后按新预算投影摘要；确认旧源不可改写、新目标预算正确、输出原值、失败不提前采用后继。
- 运行中工具后续调用覆盖 Tier 1/2/3，最终 Tier 1 输入越过阈值后回到原 Tier 2/3；手动压缩确实采用后继时保留旧额度与非默认普通 reasoning，再正常交接新额度；排队子代理第一调用执行已接受额度、下一安全点再交接。冻结恢复仍拒绝物理与认证漂移，异常释放原有 headroom handle。
- 前端默认最大值、编辑、上下界、只读输出与保存失败；相关 Python、前端全量、静态检查及生产构建。

## 落地记录（2026-10-04）

已按上述单一路径实现。真实 Host + 隔离 PostgreSQL 的三级测试覆盖直接承接、旧 512K 预算摘要和新 256K 预算的 Tier 3；两种 wire API 验证输出 128K、384K 及等于物理 context 的边界。等效额度产生相同冻结目标，变小后旧冻结目标仍可精确再现。

后端全量首次运行 2743 项通过，唯一差异是 v9 冻结目标改变 compile binding 的 golden 指纹；诊断恢复原 v8 序列化可重现旧 golden，且 source、预算决策与最终 token 估算全部一致。更新该明确变更的预期值后，compiler、额度、三级交接及 clean-v0 的 119 项回归全部通过。前端全量 607 项、TypeScript、ESLint、Ruff、diff 检查及生产静态构建通过。

已核实保存配置指向本机 `localhost:5432/pulsara` 且无活动连接，按仓库对可重置本地数据库的授权重建 clean-v0 并完成深度验证；本地会话数据清空，保存配置与密钥未改写。未新增表、事件或交接 owner，未改动 wire 输出计算函数。


## 本轮代码复审（2026-10-04）

critic 按完整三级复用审查并指出两个 P1 缺口：排队子代理 launch/首次调用仍读保存的新额度；运行中工具后续调用尚未进入额度交接。两处已分别通过已接受任务的冻结目标与普通安全点的同一三级 owner 修复。普通自动/手动压缩的 dry/adopted successor 同时固定旧额度及普通调用 reasoning，摘要 reasoning 继续由原 owner 选择。

端到端验证另外闭合了运行中 Tier 1 的双次来源收集问题：目的端预检只决定尝试，最终完整候选由其自身 wire 测量签发准入，安装仍验证同一语义和 wire。最终输入未通过时先释放候选，再转回原 Tier 2/3；删除跨两份输入的交接重绑定方法。没有放松 prefix、物理能力、reasoning 或 provider-open 校验。

测试覆盖实际 Host 的运行中三级交接、最终 wire 扩展导致 Tier 1 转 Tier 2、排队子代理旧额度首次调用、确实 COMPACTED 的手动压缩后交接，以及 headroom 决策异常后同会话继续执行。测试夹具统一其实际 dispatch 与冻结恢复使用的 runtime，保留小窗口压缩、漂移拒绝、取消与采用后清理断言。

最终后端全量 `pytest -q -n 4`：2752 passed；critic 独立额度专项 22 passed，加上 headroom 异常专项 1 passed。Ruff 与 diff 检查通过。本轮覆盖使用实际 Host + 隔离 PostgreSQL，provider transport 使用脚本夹具；未新增真实供应商 dogfood 或改写生产保存设置。critic 认可生产修复与测试修订，全量验证闭合后本设计和代码可以冻结。
