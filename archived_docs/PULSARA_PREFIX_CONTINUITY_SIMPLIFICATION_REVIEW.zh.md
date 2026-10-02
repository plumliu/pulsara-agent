# Pulsara 前缀一致性再思考：保留约束，审查证明与状态管理

状态：**第 8.1 节局部已授权并实施，测试及代码 diff 审阅通过；其余仍为讨论与审计草案**。2026-10-02 与 Worker history design critic（GPT-6 Astra high）完成设计核对和代码复审，无阻塞项。

本文整理前缀一致性的目标与可能的减法方向，不是一次已经完成的全链路审计，也不凭类型名称判定某个状态可以删除。行为权威仍是 [AGENTS.md](AGENTS.md)；具体 worker 分支产品设计见 [worker_history canonical 分支设计](PULSARA_WORKER_HISTORY_CANONICAL_BRANCH_DESIGN.zh.md)。

## 1. 判断：约束合理，实现成本需要逐项证明

Pulsara 应严格保证**同一个已安装 epoch 内的前缀不变性**。新 cold epoch 可以从 canonical 有效历史重新构建；跨 epoch 尽量保持相同语义材料的投影稳定，但不用证明整个新请求等同于旧请求。

“围绕约束建立了超过实际需要的证明和状态管理机制”是值得核查的方向。它可能表现为同一份事实被多次封装、重复验证，或为了优化收益引入额外生命周期；但不能据此断言整个 continuity／资源释放链都应删除。

本轮源码核对确认：现有 append 分支严格比较同 epoch 的已安装前缀；cold 与 adopted compaction 使用其他 transition 分支。**未发现要求所有 cold epoch 与前一 epoch 字节相同的统一硬门禁。**因此，不能把讨论写成“已经证实全系统为了跨 epoch 缓存而走偏”。可成立的减法是删除具体的重复载体，而不是整体撤掉 continuity 或结算。

## 2. 三层目标，三种强度

| 层次 | 目标 | 性质 |
| --- | --- | --- |
| 历史语义 | 来源、消息顺序、工具结算和有效压缩基准正确 | 产品正确性要求 |
| 同 epoch 输入 | SYSTEM／tools 不变，messages 与最终 wire 的历史部分仅按后缀扩展 | 严格 runtime 不变量 |
| 跨 epoch 投影 | 相同语义输入尽量投影成相同公共前缀，减少无意义变化 | 工程优化，不构成启动权或失败条件 |

canonical 是语义真源；已安装 cohort 是当前 epoch 输入与目标的进程内真源。两者服务不同边界，没有必要让一份 canonical rows 证明所有供应商缓存事实，也没有必要为了重启保存完整已安装 epoch。

同 epoch 的严格前缀约束能避免运行中意外修改输入，还能改善缓存复用。即使某供应商不命中缓存，仍应守住该合同。缓存命中由供应商决定，冷启动和缓存未命中没有一一对应关系。

## 3. 当前源码能确认什么

本节是源码阅读事实，不是删除清单。

| 入口 | 当前职责／观察 |
| --- | --- |
| [model_input/continuity.py](src/pulsara_agent/model_input/continuity.py) | `FrozenProviderInputEpochView` 持有冻结的 SYSTEM、tools、messages、wire plan 等；`InstalledEpochRuntimeCohort` 将输入与目标 bundle 绑定 |
| [input_continuity.py](src/pulsara_agent/conversation_kernel/input_continuity.py) 的 `register` | 校验 predecessor、epoch revision 与来源；对 `InstalledEpochAppend` 比较 canonical frontier、SYSTEM、tools、native projection、messages 及最终 wire 前缀 |
| [model_input/compiler.py](src/pulsara_agent/model_input/compiler.py) | 普通 append 基于 predecessor messages 追加 suffix，不以每次全历史重编译代替 append |
| 同一 continuity owner 的 `authorize_new_subagent_scope`／`retire_terminal_subagent_scope` | 新 worker 取得自己的空 scope 启动授权；终态 worker 释放进程内 scope，不是可继续运行的旧 epoch |
| [conversation_kernel/cold_epoch.py](src/pulsara_agent/conversation_kernel/cold_epoch.py) | 共用 cold 编译与规划接缝；新输入仍需目标、最终 wire 和资源校验 |

上述职责与当前约束一致。worker_history 没有必要为了“像原任务一样继续”复制完整 cohort。它只需承接有效历史，再使用现有的新 scope cold 路径。

另一方面，`input_continuity.py` 中还有多类 bootstrap lease、install authority、preparation reservation、no-continuation evidence／closure，以及 empty adoption／settlement 状态。第 8 节记录本轮减法：已折叠一层 authority carrier，同时确认安装与释放机制有独立职责；其余类型仍需逐项追踪。

## 4. 必须保留的边界

### 4.1 epoch 安装与并发裁决

同一个 scope 的已安装输入及目标需要唯一 owner。候选编译期间可能发生并发追加、关闭、取消或目标生命周期变化；提交时必须确认候选仍基于正确 predecessor，避免旧 candidate 覆盖新输入。

现有 owner slot、锁、epoch nonce／revision 与 exact predecessor 对象服务这一真实竞争边界。可以研究减少附加 wrapper，但不能把它们全部改成“编译出来就直接发送”，也不能因为内存 view 为 None 就自动授权 cold bootstrap。

### 4.2 两层前缀验证

内部 typed messages 没变，不一定意味着 lowering 后的 provider wire 没变；工具投影或 adapter 可能改变 wire 结构。现有 typed input 与最终 wire 的检查分别保护不同边界，不能仅因两者看起来相似就去掉 wire 验证。

这里的 wire 前缀指 `root_policy_value`、`tool_items` 不变，以及 `ordered_input_items` 的已安装历史部分不变；**不是整个 HTTP JSON 请求字节串只能追加**。compiler 的 suffix 构造与安装裁决也不是同一个职责：前者构造合法输入，后者通过 predecessor／slot 检查拒绝过期候选，其中最终 wire 校验拒绝 lowering 改写。

如果未来能由唯一受控的构造路径直接保证某层不变量，可以评估删掉同层重复检查；必须先证明所有实际入口都经过该路径，并保留能发现 adapter 破坏前缀的验证。

### 4.3 资源、取消和副作用结算

最终 wire 报价、provider 输入驻留预算、工具输出／canonical 存储预留、取消与释放结算，有独立的物理和产品意义。它们不是供应商缓存证明。

此前修复的“估算清单能放下但最终 wire 放不下”“JSON 再编码膨胀导致存储预留不足”“可降级展示结果过度预留”均说明：预留与实际表示的边界需要精确。简化 continuity 不授权回退这些修复、减少预算覆盖或把资源释放交给 best-effort 猜测。

### 4.4 canonical、内容与 replay 边界

保留真实内容完整性检查、跨事务／重启的语义确认、稳定 canonical 身份、provider replay compatibility 和必要的 keyed token 认证。历史 tool call 不因被分支继承而获得执行权；历史读取不能跳过归属和有效 compaction 基准。

[Fingerprint 减法规格](archived_docs/PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md) 已明确：完整 frozen DTO 在同进程已随对象携带时，不应再建立 fingerprint registry 证明同一个值；真正跨边界的 digest／MAC 继续保留。新的减法应沿这条区分，而不是统一去掉所有 hash。

## 5. 值得核查的四类过量设计

### 5.1 同一事实经过多层证明对象

追踪 `ProcessLocalProviderInputInstallAuthority`、`AuthorizedEmptyBootstrapLease`、`EmptyPreparationReservation` 等对象从谁创建、谁持有、谁消费，以及它们是否表达独立的一次性生命周期。已核对的对象按第 8 节结论处理，不能因为同属 bootstrap／install 就一起删除。

如果某 wrapper 只是携带已有 owner 持有的 exact frozen 值，没有新授权或竞争裁决，可考虑直接传递该值。如果它防止释放后再次安装、跨 scope 使用或重复消费，就需要保留该语义，或者交回已有唯一 owner；不能直接删除检查。

目标是减少重复事实，不是给所有类型换一个更短名字，也不是建立新的通用 proof 框架。

### 5.2 settlement 状态是否重复表示资源阶段

核查 empty adoption、full／conflict settlement、no-continuation closure 及相关 evidence，区分“没有未来模型续请求”与“旧资源已彻底释放”。这两个事实未必同时发生。

若多套状态都在追踪同一资源阶段，可收敛到现有 owner 的少量明确状态及精确资源句柄；如果某状态承载 canonical 结果已提交但释放未完成的窗口，它不能被折叠成普通 EMPTY。失去 writer、取消、provider open 失败与释放失败的路径都要追到真实消费者后再决定。

不为此引入 durable lease、恢复 job、checkpoint、跨模块 proof graph 或第二套 settlement 状态机。

### 5.3 当前输入已完整携带，却重复保存派生身份

对 source、planning、compiled input、wire plan 的附加 fingerprint／兼容字段，按实际独立消费者分类。只有 aggregate hash 再读取、constructor 自校验或同进程 exact value 对比的字段，是优先减法候选；真实 replay／跨重启语义边界不是。

本次不宣称源码中每个现存 fingerprint 都属于前一类。若外部或 canonical ID 仍依赖其算法，应在唯一 ID builder 即时计算同样值，不保留冗余 DTO 字段或改变公开身份。

### 5.4 为跨 epoch 缓存优化新增生命周期

worker 分支不需要继承旧 epoch、保存旧冻结工具集、维护 warm／cold 路由选择或校验缓存可复用证明。它从 canonical 有效历史开始即可。

如果其他子系统确有类似机制，需先确认它是否还承担 provider 协议、模型切换、结果确认或输入预算职责。不能因为它也改善缓存，就把它归为“只为缓存而存在”。本次阅读没有确认这些候选已经构成一套可删除的跨 epoch 管理路径。

## 6. 推荐的简单结构

```text
canonical 有效历史 + 当前合法输入来源
             ↓
        已批准的边界？
       /              \
新 cold / adopted       同 epoch
按当前目标重新编译       exact installed cohort + suffix
       \              /
      公共 lowering / 最终 wire 报价
             ↓
     唯一 continuity owner 裁决与安装
             ↓
        provider 执行与既有结算
```

这个结构保留当前共用的 compiler、wire planner、continuity 和资源 owner。优化发生在确定性构造和已有 owner 内，不引入另一个“前缀管理器”。

| 变化 | 应走的路径 |
| --- | --- |
| 新 worker／历史分支第一次启动 | 新 scope 的普通 cold 路径 |
| 同 epoch 新消息、工具结果、协作材料 | suffix 追加 |
| 运行中 MCP／Skill／memory／权限／UI 变化 | 当前执行权限检查及既有追加观察，不隐式 rebase |
| 产品已批准并接纳的模型切换、Host 重载启动 | 各自既有 new cold epoch 路径；不由一般 compatibility mismatch 代替授权 |
| 显式采用 compaction | adopted successor 边界；仅生成候选不获得 rebase 权 |
| 编译候选过期 | 明确冲突及现有重读／重新准备，不把冲突伪装成新 epoch |

这些只是现有批准边界的归类。显式模型切换归入经接纳的新 cold epoch，不增加第三种任意 rebase 例外；也不能把“模型切换”理解成 worker 运行中任意热切换。

## 7. 跨 epoch 如何尽量稳定而不增加维护负担

优先改进公共投影的确定性：工具和来源排序稳定，历史文本与角色结构稳定，不把原生对话重新包成 JSON 历史材料，不添加无语义时间戳／随机说明，不反复展开已被 snapshot 覆盖的祖先。初始协作材料仍使用其既有 typed 不可信数据包装；这不是删除所有 JSON 编码或信任边界包装的原则。

当前 SYSTEM、工具表、模型 lowering、合法环境观察确实变化时，新 epoch 接受相应变化。不为了保住旧缓存隐藏权限撤销、继续使用不合法 MCP 工具，或要求不同模型接受旧供应商的私有片段。

需要评估效果时，比较具体公共前缀与实际供应商返回的缓存统计。测量用于判断优化收益；不增加持久缓存命中事件，不设“缓存率低于多少就拒绝运行”的门槛，也不把 provider 全请求内容长期保存为新的恢复真源。

## 8. 本轮减法：变更前审计依据与实施结果

下表记录**变更前的消费者审计依据**，按创建、消费和失败窗口追踪对象；旧 authority 已按 8.1 删除。结论只覆盖这些路径，不是整个 continuity 子系统都已完成减法审计。

| 对象 | 实际职责与消费者 | 本轮结论 |
| --- | --- | --- |
| `PreparedEmptyScopeBootstrapAuthority` | `issue_transition()` 创建，`EmptyScopeColdStart` 携带；candidate 构造校验 destination 的 exact identity，`register()` 核对 scope 和 exact preparation basis；没有独立消费状态或释放流程 | 内容可以并入已有 transition；单独的 `authority_nonce` 仅生成、保存和非空校验，没有身份消费者 |
| `EmptyPreparationReservation` | 把 `AUTHORIZED_EMPTY` 占用为 `PREPARING_EMPTY`；abort／discard 恢复原授权，adoption 可接管 | 保留占用与 exact reservation 身份，不能因为 authority carrier 冗余而连带删除 |
| `ProcessLocalProviderInputInstallAuthority` | `direct_model.preflight_execution()` 验证已注册的 exact plan；`open_once()` 消费绑定 exact candidate／execution 的一次性 permit | 保护实际 provider 执行边界，首切片保留 |
| no-continuation／empty-adoption settlement | 先撤销旧 preparation、关闭句柄及 borrow，确认物理释放后才发布 EMPTY；释放失败留在 settling／quarantine | 有真实失败窗口，首切片保留 |

源码锚点：[bootstrap 创建](src/pulsara_agent/conversation_kernel/input_continuity.py)、[authority 与 transition](src/pulsara_agent/model_input/continuity.py)、[provider preflight／open](src/pulsara_agent/conversation_kernel/direct_model.py)、[FULL 到 EMPTY 的释放顺序](src/pulsara_agent/conversation_kernel/safe_point.py)、[compaction borrow 检查](src/pulsara_agent/conversation_kernel/compaction/coordinator.py)。

一个不能丢失的故障窗口是：压缩结果已提交，但旧 tool／provider borrow 关闭失败。若此时直接恢复 EMPTY，新 cold preparation 会与未释放的旧资源重叠。“不再续请求”不能替代“已经释放”。

**本轮实施的首切片：把 bootstrap authority 的必要内容并入已有 `EmptyScopeColdStart`。**删除 `PreparedEmptyScopeBootstrapAuthority`、独立的 authority issuer 包装和无消费者的 `authority_nonce`；transition 直接携带 scope、destination、exact preparation basis 与原有 seed，仍由现有 owner 发放，保留发放 seal 的约束。没有新增另一种授权对象、registry 或状态机。

必须保留 `BOUND_EMPTY`、exact reservation／basis、revision 为 0、无 predecessor、scope 一致性及 destination 对象身份（`is`）检查，以及既有 safe-point register／install 裁决。尤其不能把“没有 installed view”当成允许 bootstrap，也不能让公开构造 transition 绕过 owner 授权。

### 8.1 本轮已授权的 hard-cut 实施合同

唯一发放 owner 仍是 `HostProviderInputContinuityOwner.issue_transition()`：在原有锁内确认 exact reservation 与 `PREPARING_EMPTY` 后，由私有工厂直接构造 sealed `EmptyScopeColdStart`，再进入既有 `BOUND_EMPTY`。不再先创建 authority 再创建 transition，也不保存只供构造校验的 seal 字段。candidate 继续检查 destination 的 `is` 身份，`register()` 直接读取 transition 的 scope 和 exact basis。

abort／discard 仍通过现有 reservation 恢复授权；旧 reservation 不能注册，Host 关闭和跨 scope 仍拒绝，install permit 仍一次性绑定 exact candidate／execution。结算、预算、provider 输入与数据库 schema 均无变更，不增加事件、slot、registry、持久关系或状态；删除旧类型、issuer 和导出，不保留别名或兼容分支。

本轮收口要求是下述测试及同一 critic 的代码 diff 审阅。其他 bootstrap 来源 wrapper、settlement carrier 和派生 fingerprint 尚未完成足以授权删除的审计。`semantic_prefix_fingerprint` 当前参与 compiler、steer 和 compaction 的派生绑定，不能当作仅 constructor 自校验的无消费者字段顺手删除；这些消费者是否能改为 exact value 或在真实边界即时计算，需要另轮追踪。

首切片的验证范围：ROOT／新 worker／worker_history cold 启动、abort 后重试、旧 preparation 被替代、跨 scope 候选、重复安装、Host 关闭、合法 suffix 及前缀改写拒绝；补跑相关 compaction NONE／FULL／CONFLICT 与释放失败回归。验证授权和资源行为，缓存统计只作附加观察。

### 8.2 实施收口

代码只改两个 continuity 模块；旧类型、issuer、nonce、导出及嵌套访问均已删除，未留下兼容路径。新增 8 个边界用例，包含值相等但不是同一对象的 target／basis／reservation，验证身份检查没有被替换为值比较。

254 项测试通过：223 项 compiler／prefix continuity／direct model／compaction 测试，30 项使用临时 PostgreSQL 的 worker_history 与 runner 集成测试，1 项 Host 关闭失败隔离回归；身份用例完善后另重跑 8 个新增用例。Ruff 和 `git diff --check` 通过。同一 Astra high critic 审阅代码 diff 后未发现 P1／P2 或实现阻塞。本轮未运行真实供应商 dogfood，也未改 schema。

## 9. 后续实施应交付什么

后续若授权实施减法，应先选一个具体链路，完成下面的闭环，而不是先列一大批类型要求删除：

1. 找到实际不变量、唯一 owner、写入／消费位置和一个会违反该不变量的真实竞争或失败场景。
2. 标明现有结构承担的独立职责：历史语义、epoch 前缀、授权、资源或结算；没有消费者的派生值单独列出。
3. 提出删减后的最小状态／值传递，说明每条取消、过期、释放失败和 Host 关闭路径如何处理。
4. 用产品合同测试证明正确性，评估代码与维护成本；不增加证明机制来证明“已经减少证明机制”。
5. 逐项审阅后，把确切变更写为 hard-cut 实施规范，同步删除旧路径、更新测试；如涉及 schema，再更新 clean-v0。第 8 节是本轮已授权的局部实施合同，其余讨论不授权删除当前 safety／settlement 状态。

第 8 节列出首切片的具体验证范围；后续扩大减法时，还需确认长任务、排队和多次 compaction 仍可持续。真实 dogfood 验证行为与输入，缓存统计作为附加观察。

## 10. 本轮建议

worker_history 的普通 cold 分支和原生 canonical 继承已实施，并完成测试、critic 代码审阅及真实 dogfood。本轮不再把它列为前置待办，也不重开 warm／cold 分支方案。

本轮仅实施第 8 节的局部合同；其余 wrapper 与派生字段分别审计。继续保留同 epoch 的严格约束，跨 epoch 以 canonical 有效历史重新编译并追求稳定投影。
