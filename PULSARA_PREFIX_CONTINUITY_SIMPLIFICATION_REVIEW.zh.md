# Pulsara 前缀一致性再思考：保留约束，审查证明与状态管理

状态：**讨论与审计草案，尚未授权删除生产机制**。记录日期：2026-10-02。

本文整理前缀一致性的目标与可能的减法方向，不是一次已经完成的全链路审计，也不凭类型名称判定某个状态可以删除。行为权威仍是 [AGENTS.md](AGENTS.md)；具体 worker 分支产品设计见 [worker_history canonical 分支设计](PULSARA_WORKER_HISTORY_CANONICAL_BRANCH_DESIGN.zh.md)。

## 1. 判断：约束合理，实现成本需要逐项证明

Pulsara 应严格保证**同一个已安装 epoch 内的前缀不变性**。新 cold epoch 可以从 canonical 有效历史重新构建；跨 epoch 尽量保持相同语义材料的投影稳定，但不用证明整个新请求等同于旧请求。

“围绕约束建立了超过实际需要的证明和状态管理机制”是值得核查的方向。它可能表现为同一份事实被多次封装、重复验证，或为了优化收益引入额外生命周期；但不能据此断言整个 continuity／资源释放链都应删除。

本次阅读已经看到：现有 append 分支严格比较同 epoch 的已安装前缀；cold 与 adopted compaction 使用其他 transition 分支。**未发现要求所有 cold epoch 与前一 epoch 字节相同的统一硬门禁。**因此，不能把讨论写成“已经证实全系统为了跨 epoch 缓存而走偏”。需要分别核查每个机制保护的真实故障。

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

另一方面，`input_continuity.py` 中还有多类 bootstrap lease、install authority、preparation reservation、no-continuation evidence／closure，以及 empty adoption／settlement 状态。它们构成下一步值得逐项追踪的复杂区域。当前阅读尚不足以把某一类型标为确定冗余。

## 4. 必须保留的边界

### 4.1 epoch 安装与并发裁决

同一个 scope 的已安装输入及目标需要唯一 owner。候选编译期间可能发生并发追加、关闭、取消或目标生命周期变化；提交时必须确认候选仍基于正确 predecessor，避免旧 candidate 覆盖新输入。

现有 owner slot、锁、epoch nonce／revision 与 exact predecessor 对象服务这一真实竞争边界。可以研究减少附加 wrapper，但不能把它们全部改成“编译出来就直接发送”，也不能因为内存 view 为 None 就自动授权 cold bootstrap。

### 4.2 两层前缀验证

内部 typed messages 没变，不一定意味着 lowering 后的 provider wire 没变；工具投影或 adapter 可能改变 wire 结构。现有 typed input 与最终 wire 的检查分别保护不同边界，不能仅因两者看起来相似就去掉 wire 验证。

如果未来能由唯一受控的构造路径直接保证某层不变量，可以评估删掉同层重复检查；必须先证明所有实际入口都经过该路径，并保留能发现 adapter 破坏前缀的验证。

### 4.3 资源、取消和副作用结算

最终 wire 报价、provider 输入驻留预算、工具输出／canonical 存储预留、取消与释放结算，有独立的物理和产品意义。它们不是供应商缓存证明。

此前修复的“估算清单能放下但最终 wire 放不下”“JSON 再编码膨胀导致存储预留不足”“可降级展示结果过度预留”均说明：预留与实际表示的边界需要精确。简化 continuity 不授权回退这些修复、减少预算覆盖或把资源释放交给 best-effort 猜测。

### 4.4 canonical、内容与 replay 边界

保留真实内容完整性检查、跨事务／重启的语义确认、稳定 canonical 身份、provider replay compatibility 和必要的 keyed token 认证。历史 tool call 不因被分支继承而获得执行权；历史读取不能跳过归属和有效 compaction 基准。

[Fingerprint 减法规格](archived_docs/PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md) 已明确：完整 frozen DTO 在同进程已随对象携带时，不应再建立 fingerprint registry 证明同一个值；真正跨边界的 digest／MAC 继续保留。新的减法应沿这条区分，而不是统一去掉所有 hash。

## 5. 值得核查的四类过量设计

### 5.1 同一事实经过多层证明对象

优先追踪 `ProcessLocalProviderInputInstallAuthority`、`AuthorizedEmptyBootstrapLease`、`EmptyPreparationReservation` 等对象从谁创建、谁持有、谁消费，以及它们是否表达独立的一次性生命周期。

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
| 产品已批准的模型切换、显式采用 compaction、Host 重载启动 | 各自既有批准边界；不由一般 compatibility mismatch 代替授权 |
| 编译候选过期 | 明确冲突及现有重读／重新准备，不把冲突伪装成新 epoch |

这些只是现有批准边界的归类，不能把“模型切换”理解成 worker 运行中任意热切换，也不能把任意压缩候选理解成已经 adopted 的 successor。

## 7. 跨 epoch 如何尽量稳定而不增加维护负担

优先改进公共投影的确定性：工具和来源排序稳定，历史文本与角色结构稳定，同一内容不重新包成 JSON，不添加无语义时间戳／随机说明，不反复展开已被 snapshot 覆盖的祖先。

当前 SYSTEM、工具表、模型 lowering、合法环境观察确实变化时，新 epoch 接受相应变化。不为了保住旧缓存隐藏权限撤销、继续使用不合法 MCP 工具，或要求不同模型接受旧供应商的私有片段。

需要评估效果时，比较具体公共前缀与实际供应商返回的缓存统计。测量用于判断优化收益；不增加持久缓存命中事件，不设“缓存率低于多少就拒绝运行”的门槛，也不把 provider 全请求内容长期保存为新的恢复真源。

## 8. 下一步审计应交付什么

后续若授权实施减法，应先选一个具体链路，完成下面的闭环，而不是先列一大批类型要求删除：

1. 找到实际不变量、唯一 owner、写入／消费位置和一个会违反该不变量的真实竞争或失败场景。
2. 标明现有结构承担的独立职责：历史语义、epoch 前缀、授权、资源或结算；没有消费者的派生值单独列出。
3. 提出删减后的最小状态／值传递，说明每条取消、过期、释放失败和 Host 关闭路径如何处理。
4. 用产品合同测试证明正确性，评估代码与维护成本；不增加证明机制来证明“已经减少证明机制”。
5. 逐项审阅后，把确切变更写为 hard-cut 实施规范，同步删除旧路径、更新 clean-v0 与测试。未经这一步，本文不授权删除当前 safety／settlement 状态。

验证关注同 epoch 前缀改写被拒绝、合法 suffix 可继续、cold 分支可采用新目标与能力、候选过期不会覆盖、取消与资源释放不双结算，以及长任务／排队／多次 compaction 仍可持续。真实 dogfood 应验证行为与输入；缓存统计作为附加观察。

## 9. 本轮建议

先落实 worker_history 的普通 cold 分支设计，去掉 JSON 历史包装和复制旧 epoch 的潜在需求。随后独立审计 continuity 的 bootstrap／settlement 对象及重复派生字段，按具体消费者做减法。

保留同 epoch 的严格约束；跨 epoch 以 canonical 有效历史重新编译并追求稳定投影。实现复杂度的取舍由真实生命周期与故障决定，不由缓存目标或“证明越多越安全”决定。
