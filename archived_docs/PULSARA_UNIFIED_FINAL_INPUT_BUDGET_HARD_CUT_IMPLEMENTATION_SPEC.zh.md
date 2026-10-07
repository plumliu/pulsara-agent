# Pulsara 统一最终输入预算改造实施规格

- 日期：2026-10-07
- 状态：已实施并激活。后端完整确定性回归、审阅修复回归、同一 Unified budget critic 最终复核及 DeepSeek 双 API 真实验收均完成；前端当时遗留的 lint/typecheck 问题已按后续授权修复，见 §13.6。
- 适用基线：当前工作树，包括已完成的内置工具默认值与 null 契约修订。Git 管理代码身份，不维护文件、文档或证据 SHA。
- 本轮授权：用户已明确授权按本文完成代码落地；顺序为静态/确定性测试、同一 Unified budget critic 代码审阅、最后使用 DeepSeek 完成 Chat Completions 与 Responses 真实 dogfood。

## 1 目标和范围

上下文占用、发送准入、输出空间、压缩触发与候选选择，统一消费实际 provider 输入对应的有效预算 `budget_input_tokens`。复用现有最终 wire 投影、启发式估算、usage anchor 和输入连续性 owner，删除 canonical 整份上下文 token 账及其重复校验。

当前主调用路径和 UI 已使用最终 wire 预算。本轮主要清除 compiler 的重复记账及残留的混合口径，修正记忆失效预留等消费者。不得据此重写工具 schema、模型路由、provider 协议、记忆检索策略或压缩产品语义。

本规格采用一次 hard cut：所有受影响的生产者、消费者、测试、诊断与 dogfood 在同一改造中切换，不保留新旧字段双轨、旧名 alias、feature flag 或 canonical 预算 fallback。实施步骤仅表示编辑依赖顺序，不是分阶段激活方案。

### 1.1 保留的产品与架构边界

1. canonical schema 继续拥有工具参数的精确执行契约；provider 投影可以是协议要求下的确定性超集。
2. 只有新 cold epoch 与显式采用的 compaction successor 可以重建 provider roots。现有 epoch 内 SYSTEM/tools 不变，ordered input 仅追加；更换估算字段不是新重建边界。
3. 继续使用既有输入预算、输出保留、安全余量、物理字节、图片、item、并发和单操作 deadline 边界。没有新总历史、总轮次、总调用次数或任务寿命上限。
4. 新增 durable relation、event kind、live event kind、subject、append guard、job 均为零。已有 provider usage 行保持原义；anchor 仍为 process-local 观测，不增加恢复机制。
5. 不新增 estimator 框架、tokenizer、provider usage 查询服务、fingerprint registry 或候选 proof graph。

### 1.2 文档关系

本规格实施后，替代旧规格中“保留 raw semantic 整份上下文 token 账及其恒等式”的要求；保留最终 wire、usage anchor、epoch、replay、图片和物理资源的产品契约。以下归档资料仅帮助定位设计背景，不能用历史实现覆盖本文或 AGENTS.md：

- [最终 wire 估算](PULSARA_COMPACTION_FINAL_WIRE_ESTIMATION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)
- [provider usage 锚定预算](PULSARA_PROVIDER_USAGE_ANCHORED_INPUT_BUDGET_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)
- [epoch 与 canonical 重投影](PULSARA_PROVIDER_INPUT_EPOCH_BOUNDARIES_AND_CANONICAL_REPROJECTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)
- [fingerprint 减法](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)

## 2 当前实现与需要改动的位置

下表描述改造前代码，不代表目标状态。定位以符号名为准，行号随改造变化。

| 位置 | 当前行为 | 目标 |
|---|---|---|
| `llm/estimator.py` 的 `TokenEstimate`、`estimate_context`、`estimate_frozen_input` 等 | 用 canonical ToolSpec 和 semantic message 生成整份 token 分项与总量 | 删除整份 semantic 记账 API 与 DTO，保留最终 wire 和有独立用途的文本/JSON 原语 |
| `model_input/compiler.py` | cold、append、projection 均计算 semantic estimate，增量维护每消息 token，并重算验证 | 只生成结构、内容、来源、placement 和决策事实，不拥有上下文 token 预算 |
| `model_input/contracts.py`、`continuity.py` | compiled/projection DTO 携带 estimate、budget report，多个序列化及等值约束依赖它们 | 删除 token 账；保留来源、顺序、权限、prefix、物理结构等必要事实 |
| `conversation_kernel/direct_model.py` | 最终 wire 测量前又校验 semantic estimate；最终 quote 同时携带两套总量 | 删除 semantic 依赖，保持 wire materialization、replay 替换和有效预算为唯一上下文计量 |
| `llm/validation.py` | 有 plan 时做 semantic estimate 一致性检查；无 plan 时还用 semantic 总量拒绝超预算 | shape/身份验证与最终 wire 准入各归其 owner，删除 semantic token 拒绝路径 |
| `conversation_kernel/memory/dispatch.py` | 用 semantic 增量计算失效预留及完整快照成本 | 预留由当前目标的实际 wire 追加成本产生，见第 5 节 |
| `provider_dispatch.py` | 主准入使用 wire；预留加数和部分失败原因归类仍依赖 semantic 估算 | 预算相加及预算原因诊断也不得混入口径 |
| `compaction/`、`steer.py`、`input_continuity.py` | 多处 DTO join、诊断序列化携带 semantic estimate；主压缩判断已经使用 wire | 保持现有选择与 adopt 流程，删除重复 token 依赖 |
| `host.py`、`web_app/session_controller.py`、前端 context indicator | 显示有效 wire 预算，可用有效 anchor 校准 | 保持此行为，不把估算计算搬进 UI/host |

完整执行盘点必须覆盖生产代码、协议类型、测试 fake、benchmark、dogfood、tools 和公共诊断字段。仅删除 `estimate_tool_spec`，或把它改成 function JSON 计数，不满足本规格：消息包装、replay、图片和固定 context 仍会产生差异。

## 3 唯一预算定义

### 3.1 估算对象

`W` 是既有 adapter materialize 的最终 context-bearing projection，包含 SYSTEM/instructions、实际安装的工具、tool choice、影响上下文的 route defaults/extra body、最终 ordered input 及真实 replay items。按既有 adapter 边界识别上下文，不对整个 HTTP payload、凭据、输出控制参数或网络字节盲目计 token。

`H(W)` 使用 `PulsaraHeuristicTokenEstimatorV3` 的现有最终 wire 规则：固定 projection 的 JSON 成本与 request framing，加 ordered item 的 JSON/framing 成本，再按既有图片规则计算视觉成本。使用 compact canonical JSON 的规则、CJK 规则、逐组件取整和已验证常量保持不变。

图片必须继续携带与 ordered item 对应的 typed source：按既有规则剔除 base64 数据载荷的文本成本并计视觉 token，不得直接对图像 data URL 全量字符串计数。replay 替换成本必须取真实替换后的 provider items；禁止以可见 assistant 文本或 `output_tokens` 代替回灌输入。

### 3.2 有效预算和来源

```text
无兼容 anchor：B(W) = H(W)
有兼容 anchor A：B(W) = reported_input_tokens(A) + H(W) - H(A)
```

第二式只在固定 projection 一致、A 的 ordered input 为 W 的精确前缀且现有可加估算契约成立时使用。差值必须非负；禁止用 `max(0, delta)` 掩盖 prefix 漂移。

沿用以下字段，不另造第三套计数：

| 字段 | 语义 |
|---|---|
| `raw_final_wire_estimated_input_tokens` | H(W)，完整最终输入的启发式值 |
| `budget_input_tokens` | B(W)，唯一输入预算判断值 |
| `budget_source` | `heuristic` 或 `reported_input_anchor` |
| `anchor_model_call_id`、`anchor_reported_input_tokens`、`estimated_suffix_tokens` | 锚定时的来源与分解，未锚定时为空 |
| `final_wire_visual_image_tokens` | raw heuristic 的视觉分项，不是校准预算下限 |
| `final_wire_utf8_bytes` | 独立的物理资源事实，不受 token 校准折减 |

H 和 B 是同一预算链的原始值与校准值，并非两套并行准入 authority。允许 B 小于或大于 H；不得使用 `max(H, B)`，也不得加上 `B >= raw_visual_tokens` 的隐性下限。物理资源超限仍独立阻止准入。

### 3.3 锚点和冻结时机

复用现有 scope/epoch owner 的 anchor 接纳条件、目标和模型身份判断、prefix 检查与迟到报告处理。当前 purpose、model alias、usage 缺失、零 input、失败终态、取消和跨 epoch 报告的策略不改。

同一次候选选择使用同一个不可变 anchor snapshot。新 usage 不能改写已冻结的 quote、已安装的 roots 或已发请求。post-response follow-up 的新报价可使用刚收到的合格 input；缺失新 usage 时继续使用仍兼容的旧 anchor。

cold、新采用的 compaction successor、重启或不兼容目标不复用旧 anchor。没有兼容 anchor 时全量启发式计算是正常测量路径；已安装 epoch 的 prefix 违规必须进入既有错误路径，不能靠丢弃 anchor、重算总量来掩盖。

## 4 Owner 分工和调用顺序

### 4.1 复用现有边界

- semantic compiler 拥有内容结构、source/placement、合法 render variants、完整 tool group、FULL_REQUIRED、图片引用和物理 working-set 校验。它不调用 dispatch，不打开 provider，不计算整份上下文 token 账。
- Chat/Responses adapter 拥有协议投影、function schema lowering、消息/replay 形状与 context-bearing defaults。
- `direct_model.py` 现有 wire measurement 拥有投影后的启发式计数与 anchor 校准，输出现有 frozen quote/materialization。
- dispatch、runner、compaction 与 continuity 各自继续拥有选择、资源准入、安装、执行和副作用结算。quote 只描述成本，不授予执行 authority。
- host 和 UI 消费同一有效预算，不维护第二套计算或缓存 authority。

SDK 仍负责已有 transport/协议调用；本轮不新增依赖，不复制 SDK 机制。Pulsara 只拥有其输入投影和产品预算。

### 4.2 主调用和候选退化

1. 在已有 deadline 与 owner cut 下生成结构合法的 semantic 候选。首次候选保留现有物理约束允许的完整内容，不按旧 semantic token 总量提前删减。
2. 使用已冻结 target、native projection 与 replay cut，materialize 实际 provider 输入并计算 B(W)。测量不得打开 provider。
3. 同时检查 B(W) 和既有物理资源边界；超限时沿现有 source/tool-result sacrifice 顺序推进 render floor，再投影、再测量。
4. 只修改候选允许改变的内容；已安装的 prefix 不重排、不删除、不重新投影 roots。FULL_REQUIRED 仍不可降级。
5. 合格 quote 进入现有 frozen plan 与 install/open 路径。发送使用该 plan 的实际 projection；不得在预算检查之后改写 context-bearing payload。

继续以合法 variant 集、严格前进的 floor 和现有 deadline 保证单次操作终止，不新增重试次数或生命周期 cap。删除 semantic variant token 单调性断言后，仍保留 variant 顺序和完整性验证；wire 成本不保证每次递减，不能据此跳过尚未测量的合法候选。

### 4.3 无 wire plan 的受支持调用

当前 Chat/Responses payload builder 已能在无 plan 时投影 context 并进行 wire 估算。本轮保留这些真实调用入口，复用其投影和 `resolve_wire_output_tokens`，删除 `validation.py` 的 semantic 提前拒绝。

无 plan 不是绕过预算的许可：在实际网络 open 前必须完成最终 wire 预算与目标/能力/输入形状验证；超限不得调用 SDK。不得给无 plan 调用伪造 anchor 或 semantic 数值占位。测试应覆盖直接 adapter 调用以及正常 kernel 调用，明确证明两种入口均无法绕过预算。

## 5 记忆失效预留和其他增量

### 5.1 从实际追加成本计算

`MemorySourceInvalidationReservation` 是当前已经存在的 process-local 预留。本轮不增加预留 registry 或独立调度层。

memory owner 继续生成既有 `CLEARED`、`UNAVAILABLE` 以及适用的完整 `SNAPSHOT` observation 内容。wire owner 使用此次冻结目标，把这些真实消息经现有 `chat_semantic_wire_group` / `responses_semantic_wire_group` 降为 ordered items，并用 `estimate_ordered_wire_json_components` 计算追加成本。memory owner 不另写 provider JSON 构造器。

对仅追加、fixed context 不变的候选 P 与可能的失效消息 m：

```text
delta(m) = H(P + m) - H(P) = H(实际追加的 ordered items)
reserve = max(delta(CLEARED), delta(UNAVAILABLE))
拟议准入 = B(P) + reserve
```

reserve 本身不能再次拿 provider 报告校准或加一次 request/SYSTEM/tools 固定成本。采用同一 anchor 时，B(P+m)-B(P) 等于该 wire 增量；不得跨目标或跨 anchor snapshot 做差。不要为了测量 m 重算整个历史。

每个被预留的 source 确保最多追加一个既有产品约定的失效 observation。多个 source 的预留按可加 item 成本求和；只有真实可同时发生的事件才同时保留容量。消费预留时，用实际追加后的总 quote 替换“当前 quote 加预留”，不能重复计入；消息已入 prefix 后也不能再次支付同一失效。

若 adapter 一条消息映射为多个 items，按实际 items 计算；不得把 semantic message 数当 wire item 数。固定输入或 prefix 不满足追加条件时，调用既有完整候选测量与连续性错误路径，不使用上述差值捷径。

### 5.2 字段处理

`invalidation_input_token_ceiling` 改为上述 wire reserve，并在其唯一构造点记录单位与来源。当前全仓生产引用已确认 `full_input_token_cost` 没有选择或资源消费者，只有字段校验与旧 identity 编码：删除运行 DTO 字段和 memory planning 中完整快照 token 的计算。历史稳定身份编码的处理单列于第 6.4 节，不允许复用该编码值作为运行时成本。

其他预留字段也按真实消费者收口：

| 字段 | 当前真实用途 | 本轮处理 |
|---|---|---|
| `invalidation_epoch_bytes_ceiling` | steer quote 与 prospective epoch logical bytes 预留 | 保留并验证单位，消费后不重复累计 |
| `invalidation_provider_item_ceiling` | DTO 固定值校验和旧 identity 输入，没有独立准入消费者 | 删除运行字段，不将它宣称为已执行的 item 保护 |
| `invalidation_encoded_utf8_bytes_ceiling`、`full_encoded_utf8_bytes` | DTO 校验和旧 identity 输入，没有独立准入消费者 | 删除运行字段和只服务它们的计算 |

实际 provider item 数来自投影结果；canonical/logical 字节限制仍由原 owner 负责，最终 wire 字节仍由 measurement 和 dispatch 负责。删除未消费的字段不得删除这些有效检查，也不能把 logical bytes 当 wire bytes。历史 identity 所需的原编码按第 6.4 节局部处理，不重新创建资源 DTO 或预算消费者。

### 5.3 其他 prospective suffix

tool result、actual assistant/replay、steer、hook、capability observation、permission notification 与 follow-up 继续复用现有 suffix quote。检查每个预留的生产、相加、消费和释放点，防止引入 semantic 增量、重复固定成本或遗漏 assistant 回灌。

有副作用工具的准入与结算保持现有顺序：不能因预算字段清理而先执行、后发现无法交付必须返回的结果；取消、失败、输出截断和 FULL_REQUIRED settlement 的原有保证不变。

## 6 Compiler 和 DTO 的减法

### 6.1 一次性移除的整份上下文记账

| 当前对象或入口 | 处理 |
|---|---|
| `TokenEstimate` 及 `estimate_context`、`estimate_frozen_input`、cooperative 变体、`estimate_frozen_tool_spec`、`estimate_tool_spec`、`estimate_message` | 在全部上下文消费者切换后删除公开协议、DTO 与方法；不得保留包装器或 deprecated alias。稳定 ID 的私有历史编码例外见第 6.4 节 |
| `estimate_model_context_for_call`、`ModelContextValidationResult.estimate` | 删除 token 职责；保留 shape/绑定/能力验证，修改返回契约与调用方 |
| `LLMContext.compiler_estimated_input_tokens` | 删除，不改成 wire 值填旧字段 |
| compiled、semantic projection、append layout、compaction semantic carrier 的 `final_estimate` | 删除字段、构造、序列化与 `estimate == estimate` 的重复检查 |
| `ContextCompileBudgetReport` 的 system/message/tool/envelope/total、protected transcript/source token 分项 | 删除这些 token 字段，剩余有用的决策/物理事实归入具名 compile report；移除 Budget 命名与没有独立消费者的限额副本 |
| source/tool-result decision 的 `estimated_tokens`、每消息 token 数组、SYSTEM 归属 token 差值 | 删除重复账；保持 selected mode、来源、必要性、reason、placement 和 physical bytes 等独立事实 |
| `FrozenProviderWireInputQuote.semantic_estimated_input_tokens`、`semantic_visual_image_tokens` | 删除字段及 cross-object 等值断言；视觉成本仅在最终 wire 计量链保留 |
| `_wire_budget_failure_kind` 的 semantic 预算归类 | 失败先由实际 wire quote 确认；原因若需要比较 protected/required 子集，则使用对应合法 wire 诊断投影，无法准确归因时使用现有通用预算原因，不造精确的假原因 |

两个 estimator Protocol 同步减法：`llm/estimator.py::TokenEstimator` 与 `model_input/contracts.py::ModelInputTokenEstimator`。不得因测试 fake 仍实现旧协议而让生产保留旧方法。

结构校验必须继续直接验证现有 typed content、source、target、tool exposure、placement、canonical frontier、replay 与 owner-issued plan 的对应关系。token 数值相等不能承担身份或完整性证明，删除数值比较也不等于删除这些对应关系。

### 6.2 保留的局部估算

`estimate_text`、`estimate_json`、视觉估算和最终 wire 组件方法保留。现有 embedding 输入限额、memory 查询/片段预算、hook 本地阈值、retained Skill 内容预算有独立产品语义，继续由原 owner 使用，不能随整份上下文账一起删除。

这些局部量不是上下文余额，不得加到 B(W) 上，也不能替代最终 wire 准入。本轮不改变其已验证边界或为了统一命名而改动检索、保留内容的产品选择。

### 6.3 Estimator 身份与计数稳定性

本轮保持 H(W) 的既有数值算法和 `pulsara_heuristic:v3` 口径。删除 API 不是修改 token 公式的理由。现有 estimator fact/fingerprint 属于真实输入兼容边界，禁止无依据重建或另加版本协商。

旧 estimator identity payload 中与 semantic 账相关的历史常量，若需要保留以维持当前 wire estimator 身份，只能在唯一 identity 构造点作为历史契约编码即时存在，不能保留相应运行时算法（第 6.4 节具名身份编码例外除外）、重复 DTO 字段或兼容读写。增加固定样本 H(W) 前后相等、当前 estimator 身份不漂移的检查。

### 6.4 稳定身份编码的最小例外

本轮不授权改变已有 stable external/canonical ID 派生。删除 token 预算字段不自动授权删掉 identity 编码中的同名历史成员；按 AGENTS.md 第 4 节，在真实稳定身份边界保留原输出。

当前已确认的依赖链如下，实施必须同时核对所有入边，不可只删 DTO 最外层字段：

| 链路 | 独立的稳定边界 | 处理 |
|---|---|---|
| `frozen_compiled_model_input_fingerprint` → `parent_context_call_subject_identity_digest` → `subagent_task_batch_identity_digest` → repository accepted event ID | 子代理接纳的 canonical occurrence，见 `subagents/contracts.py` 与 `_repository/subagents.py` | 对同一冻结语义输入保留历史 digest 与最终 event ID，不能让工具接纳的确认身份漂移 |
| compiled/旧 quote/reservation 编码 → `prepared_steer_suffix_plan_identity_fingerprint` → `build_steer_plan_conflict_interruption` → `TURN_INTERRUPTED.event_id` | repository 持久化与确认的 steer conflict occurrence，见 `steer_consumption.py` 和 `steer.py` | 保留原 stable ID 派生；同一 plan 的重复结算保持同一事件身份 |
| steer plan identity → `model-context-final-steer` | 当前内部 context identity，并可能继续进入上述 compiled 链 | 对受保护链保持原派生，不能仅因字段看似内部就孤立更改 |
| compaction projection/source/summary/request 的 estimate 编码 | 须按实际消费者区分；summary 的 `compaction-summary:...` 目前仅用于 `_NullLiveBus` assembler，返回的是文本/tool calls | 不能把这个临时 proposed ID 误称为 canonical entry ID；无稳定边界依赖的进程内编码可以移除 token 成员，真实 replay/prefix 和 canonical 内容身份继续保留 |

这构成删除旧算法目标的一个具名例外：在已有唯一 identity builder 内，将保护上述稳定输出所需的原数值编码局部派生、直接折入原 digest。输出只有现有 identity，不返回或保存 `TokenEstimate`、每消息 token 数组、旧 budget report 或 reservation 资源副本。不得重新暴露 canonical 估算服务、增加 identity registry，或把私有编码值用于准入、预留、降级、压缩、UI 和诊断 token 展示。

必须复现完整旧 preimage，包括旧决策 token、report 数值、decision digest 及其所有间接入边，不能只保住 total 一个数。尤其是新 `invalidation_input_token_ceiling` 已改成 wire 数值，旧 identity 若曾包含 semantic ceiling，不能把新值直接填入旧编码。`full_input_token_cost` 等已删字段也只能在确实受保护的历史 identity 编码内部即时推导，不重新携带在运行 DTO 上。

若 ID 构造时缺少派生旧编码的原始语义，应沿已有准备流程将计算放到完整 desired source、compiled 内容和决策事实同时可用的既有 owner cut，复用已有 identity 字段；只凭 fingerprint 或字节长度不能恢复旧 preimage，同字节长度也可能有不同 CJK 成本。已生成的最终 identity 在同一次操作内复用；不增加历史 TokenEstimate carrier、跨请求 cache 或旧/新双 identity。实现前先以改造前代码生成固定语义样例及原 stable ID，覆盖子代理 batch 接纳、steer 成功/冲突、带记忆预留和图片/tool-result 的输入；修改后对同一样例逐值相等，并验证重复接纳、ACK-unknown 确认和冲突结算仍使用同一 canonical 身份。

这里允许的是维持稳定身份所需的私有历史算术，不是第二套上下文预算，也不承诺清除所有旧公式的字面代码。已有 durable 行不重写；重启沿现有 canonical 读取和 cold 路径继续工作，不引入历史执行恢复或运行中 epoch 重建。其他无稳定消费者的 aggregate hash 不因本例外自动获得保留理由。

## 7 压缩 预览与资源保护

自动、强制、handover/model switch、continuation compaction 均保留原有 source、summary call 和 successor 的目标归属。每个候选按它实际发送的目标/API/内容单独测量；不能把 source 的校准总量当 destination 的预算，也不能让仅用于预览的测量拥有 adoption 权限。

保留 protected recent window、完整 tool group、历史请求保留、图片跨目标投影与 retained Skill 局部预算。summary 输入、可能的 repair、successor dry-run/adopt 各用对应 wire quote。reclaim、post-compaction target 和自动触发继续使用既有产品公式及对应有效 quote，不使用已删除 semantic 总量。

UI idle preview 继续只读，通过现有投影路径计算；busy preview 继续读取安装的 plan 和合格锚点；模型切换、compacting、无运行 handle 的状态语义不变。未发送草稿与尚未收集的新 turn memory/hooks 不计入现有 preview，不能把预览宣称成下一次调用的精确实测。

逻辑字节、canonical expanded bytes、wire bytes、图片数量/尺寸/引用、item 数和 deadline 均独立保留。final-wire visual token 是 heuristic 分项，不称为物理字节或服务端实际视觉 token。

## 8 诊断 持久化与历史证据

当前 compile observation 的 `total_input_tokens`、`tool_tokens` 等来自 canonical 账。生产诊断切换后删除这些含混字段，不给它们灌入 wire 或 calibrated 值来维持旧接口。已有观察事件可以输出 compile 的结构决策；最终预算诊断沿用 wire quote 的明确字段和 provenance，不新增 event kind。

上下文预算只展示 B 及其来源，需要解释时附 H、anchor 报告与 suffix。不能声称 heuristic 工具/消息分项加总等于校准后的 B，也不能把 usage anchor 的整次 reported input 分摊为“provider 实测工具 token”。

逐工具优化分析采用投影后的 function JSON，需明确是否包含 API wrapper；这类独立分析不反向参与运行时预算。上一轮 41 个工具的 function JSON 合计 22257 是当时目录的启发式分析值，不是任意会话实际工具面或完整请求预算。

`tools/measure_builtin_tool_tokens.py` 在生产 API 删除后改为只复测 function JSON；过去保存的 canonical 前后快照和研究表保留为历史测量，不新增一份生产 canonical estimator 来兼容脚本。benchmark/dogfood 中当前诊断字段同步切换；历史失败记录和旧验收结果不得改写成新实现的证据。

持久化仍使用现有 provider usage 表与数值语义。本轮预期不改数据库 schema；实施时若发现必须改变的 canonical payload/基线结构，应列明字段并更新 clean-v0，同步维持 oracle delta 为零，不因内部开发状态添加在线迁移或双读协议。

## 9 实施顺序

1. **冻结消费者清单。** 全仓盘点整份 semantic estimate、局部 estimate、wire quote 和 reservation 的生产/消费关系，逐项归入删除、迁移或保留。覆盖 root/subagent/summary/direct adapter、UI、诊断、工具脚本与测试。
2. **统一预算入口和预留。** 复用现有 wire 组件测量，修正 memory/其他混用预留，删除无 plan 的 semantic token gate，证明所有真实网络 open 前都有最终预算准入。
3. **减去 compiler/DTO 重复账。** 同步改 cold、append、compaction projection、continuity 和 serialization；保持 typed 对应关系。移除未被独立使用的 token 字段和辅助算法。
4. **清理 estimator 协议与诊断。** 删除旧方法和 fake 实现，保留局部产品预算与最终 wire 规则；修改脚本和文档，清除旧公共字段消费者。
5. **完成针对性回归和真实验证。** 通过第 10 节验收后一次性激活。不得保留“先 wire、失败再 semantic”的运行时分支。

测量性能继续复用已有 frozen projection、同次准备的 projection set、组件增量和 deadline。不得为预留每个变体反复复制完整长历史，或为减少计算引入新的 durable cache、全局 fingerprint map。需要 cooperative 检查时在既有测量过程插入检查点，取消后不发布半成品 plan。

## 10 验收矩阵

### 10.1 确定性测试

| 场景 | 必须证明 |
|---|---|
| canonical 较大而 wire 可容纳 | 不再因旧 semantic 估值拒绝、提前裁剪或触发压缩；以构造输入与 wire 结果表达断言，不恢复旧生产 estimator |
| canonical 较小而 wire 超预算 | 特别覆盖 JSON 引号/反斜线、action union 投影、Chat/Responses 包装；按实际 quote 降级或拒绝 |
| 相同内容不同 route/defaults/tool_choice | 上下文相关固定 projection 都被计入，非上下文凭据不参与；额外字段冲突按原协议拒绝 |
| 无 plan 直接调用 | Chat/Responses 均在 SDK open 前阻止超预算请求，合法请求保留可达性 |
| 输出空间 | 对 Chat/Responses、plan/no-plan、anchor 高低偏差，断言实际 payload 的 `max_completion_tokens` / `max_output_tokens` 等于 `min(effective_output_tokens, max_output_tokens, total_context_tokens - B - safety_margin)`；无输出空间时不 open |
| memory 预留 | cleared/unavailable/full snapshot、两个 source 同时失效、无 anchor/有 anchor；预留覆盖实际 wire 后缀，消费不重复计费，不重收固定成本 |
| 预留与 actual assistant/replay | tool result、hook、steer、capability 等后缀组合仍受同一预算约束，副作用结算不退化 |
| anchor 的两种偏差 | reported 高于与低于 H 均影响准入；保留 anchor + suffix 恒等式，不取 raw 下限 |
| anchor 生命周期 | missing/partial/zero/failed usage、合法 alias、迟到报告、scope 隔离、目标切换、重启与 successor；旧 quote 不被后来的报告修改 |
| native replay | generic 替换为真实 Chat/Responses replay 后计量正确，encrypted/opaque items 不被正文替代 |
| 图片 | source 与 item 一一对应、base64 文本剔除、视觉 heuristic、尺寸/字节独立保护，校准值可低于 raw 视觉分项 |
| 压缩 | 自动/强制/切模/continuation、summary/repair/successor 的目标预算、protected tail/tool groups/图片投影保持；候选 floor 推进无需 token 单调 |
| preview | idle/busy/switch/compacting/unavailable，使用与对应 candidate/installed plan 一致的预算和来源，GET 不激活会话或调用 provider |
| 长历史和取消 | 无新总 cap；沿用增量估算和现有 deadline，超时/取消不安装 partial plan，不复制完整历史给每个预留 |
| 身份与物理约束 | 删 token 等值检查后，内容/placement/target/replay/exposure/prefix 漂移仍被拒绝；raw 校准变小不能绕过物理边界 |
| 稳定 ID 历史编码 | 第 6.4 节保护链同一冻结语义的 digest/event ID 与改造前相等；子代理重复接纳、steer 冲突、确认及重启读取无身份漂移；私有历史算术不流入预算 |
| 局部产品预算 | embedding、memory 查询、hook 阈值、retained Skill 原有局部行为与上限保持 |

优先扩展现有测试，包括 `test_provider_usage_anchored_budget.py`、`test_context_usage_preview.py`、`test_context_usage_controller.py`、`test_model_context_allowance_postgres.py`、`test_provider_usage_postgres.py`、`test_catalog_final_wire_resources.py`、`test_ordinary_tool_result_resources.py`、`test_round5b_long_horizon_context_compaction.py`、`test_stage2_conversation_runner.py`，及相关图片/steer/memory/adapter 测试。保持真实错误断言，不改成 skip/xfail。

直接 adapter 准入测试注入 SDK client stub，断言超预算时网络创建调用为零、合法请求为一次，并检查实际 payload。不能使用 `_mock_chunks` 证明此边界：现有 adapter 的该分支在 payload 构造前返回，会绕过要验证的准入与输出额度逻辑。

删除符号检查覆盖源代码和当前执行脚本；历史文档、历史 JSON 与解释旧行为的测试注释可保留旧词。验收重点是调用行为，不能只有 grep 零命中或 snapshot 重录。

### 10.2 实际调用和长程 dogfood

读取 `LocalSettingsStore` 与 `require_pulsara_home()` 下保存的设置；设置只读，使用既有注入与已验证本机 disposable PostgreSQL、临时 home/workspace 隔离。不导出凭据、不 dump 原始配置，仅脱敏实际 secret。

至少覆盖一个已配置 Chat 和一个 Responses 连接，验证正常工具调用、实际 provider input/usage、下一次 suffix 校准与 UI preview 对照；另覆盖子代理独立 scope、真实 summary/successor 和多 turn/tool/steer/compaction 的既有长程场景。usage 不可用时必须明确记录 heuristic 路径，不能伪造 provider 报告证明 anchor。

保存 actual provider projection、B/H/provenance、usage、工具结果及关键终态；子代理必须检查实际完成和返回结果，不能只看 spawn/wait 工具成功。检查每个 epoch 的 SYSTEM/tools 字节一致与 messages/input 追加。记录测试命令、结果、失败和限制，不维护代码/文档/证据 SHA。

所有有用失败记录保留。确定性验收必须通过；真实模型错填参数与预算实现缺陷分开记录。某个真实 API、anchor 或长程必需场景无法验证时，明确标记该项未完成，不把规格标记 ACTIVATED。

## 11 完成条件

- 所有上下文预算消费者使用最终 wire 的 B；无 semantic gate、canonical token 预留或总量双账。
- 相同 final projection 的 H(W) 与改造前一致；raw 与 calibrated 来源清楚，图片/replay 与局部产品预算保留。
- 整份 semantic token API、DTO 字段、预算序列化和仅为其存在的证明检查一并删除；没有兼容层、替身预算 estimator 或零值占位。第 6.4 节私有稳定身份编码例外有清晰调用边界和逐值回归。
- 内容身份、作用域、prefix、权限、tool result settlement、物理边界与长程可用性通过回归。
- 新增 durable/event/subject/guard/job 为零，无新增 fingerprint、总量 cap 或 epoch 重建边界。
- 相关测试、真实 Chat/Responses 与规定长程场景完成；文档记录实际结果后才将状态改为已实施和已激活。

## 12 独立审阅记录

审阅者：用户指定的 GPT-6 Astra，reasoning effort 为 high。审阅基于初稿及当前生产代码，只读检查；不把文档审阅当作测试或真实 provider 验收。

| 发现 | 处理 |
|---|---|
| P1：token 字段进入 identity 派生，直接删除可能改变稳定 ID | 增加第 6.4 节，明确保护 steer conflict event 和 subagent accepted event 两条 canonical 链及其完整入边；仅在唯一现有 ID owner 保留私有历史编码，保持原输出 |
| P2：`full_input_token_cost` 及部分物理字段没有实际资源消费者 | 第 5.2 节改为明确删除运行字段，区分有效的 token/epoch logical bytes 预留与未消费字段，不保留假资源保护 |
| P2：输出空间和无 plan 测试入口不够明确 | 第 10.1 节补齐输出额度公式与实际 payload 断言；用 SDK client stub 验证 gate，禁止用提前返回的 `_mock_chunks` 代替 |

身份专项复核纠正了首轮审阅的一处判断：summary assembler 的 proposed ID 未被证明为 canonical entry ID，不能按字段名冻结整条链；已确认的真实 canonical 边界包括 steer interruption 和子代理接纳事件。审阅者撤回“默认允许改变 canonical ID”的初步建议，认可第 6.4 节的稳定身份编码例外，并确认当前没有可完全免除必要旧数值推导的现成复用途径。

同一 critic 已完成修订稿最终复核，结论为：“未发现仍然阻塞实施的矛盾或遗漏，可作为实施规格。”最终建议的非阻塞措辞修正也已采纳：第 6.3 节显式引用第 6.4 节例外，避免将稳定身份编码误删。

此结论仅确认规格具备实施条件，不代表生产代码已实现、测试通过或真实 provider/长程 dogfood 已完成。本轮没有执行该改造的生产代码变更。


## 13 实施记录（2026-10-07，已验收）

### 13.1 已落地的边界

- `llm/estimator.py` 只保留文本/JSON primitives 和最终 wire 估算。两个 estimator Protocol 同步删掉 semantic API；当前 V3 identity 和 H(W) 不变。
- compiler 生成内容、placement、source/tool-result 选择与 `ContextCompileReport`，不生成 token 账。compiled、continuity、compaction、steer、LLMContext、wire quote、diagnostic projection 的旧字段一起删除。
- preflight 保留 shape/target/capability 验证，并用 registered append candidate 的完整 compiled 值做 exact join。Chat/Responses payload builder 对无 plan 调用也在 SDK open 前检查 shape 和最终 wire/output 预算。
- memory owner 生成真实 observation；wire owner 按选定 API 的 ordered items 计算 suffix 成本。候选 source head 已含 CLEARED/UNAVAILABLE 时不重复预留。
- `_stable_input_identity.py` 只返回原有身份 digest。compiled、steer quote、子代理 subject 以及冷启动/installed append 的固定样本已与改造前源码逐值比对。memory reservation 的原有 digest 提前到完整 desired source 可见的 owner cut 计算，`stable_identity` 只传递这一个既有 digest；不保存旧资源账，不用于预算或 owner 验证，也未新增 hash domain/registry。
- `tools/measure_builtin_tool_tokens.py` 只输出投影后的 function JSON 估算。既有 canonical 测量文件作为历史材料保留。
- durable schema、event/subject/guard/job、epoch 重建边界及物理上限均未增加。

### 13.2 静态和确定性证据

证据目录：`output/unified-final-input-budget-20261007/`。临时测试启动器从有效 Pulsara home 只读加载保存设置，并创建已验证的本机隔离数据库；多进程入口加 main guard，数据库按测试模块隔离，clean-v0 测试保留真正的空库。没有跳过、放宽或删除损坏检测断言。

- 第一轮完整隔离回归：2,965 passed、8 failed。一个断言按本规格改为通用 wire 超限原因；七个失败由启动器提前迁移“空库”引起，已修复启动器。
- 修复和新增测试回归：71 passed，包含上述全部失败项、SDK no-plan/plan/anchor 和历史身份固定样本。
- Python `ruff check src tests tools`、`compileall`、`git diff --check` 通过。
- 前端：Node 25 关闭实验性 webstorage 后，40 个文件、633 项测试全部通过。第一次 localStorage 环境冲突日志保留。
- 前端存量静态问题明确未通过：ESLint 18 errors / 8 warnings；TypeScript 一个 `UserPluginCapability.hookCount` 缺失。前端工作树无本轮改动，不把这些结果记为通过。
- 修正启动器后的完整复跑：2,975 passed（`all-static-final.txt`）。critic 修复后，memory/预算/catalog/runner 回归 232 passed（`critic-fixes-regression.txt`）；新增测试覆盖两种 API、anchor/no-anchor 的完整 memory 替换恰好达到预算边界，以及冷 epoch 排除旧预留。


### 13.3 同一 critic 的代码审阅与修复

Unified budget critic 在静态测试后、真实 provider 调用前发现两项 P2，初次修复后复核又补充了同值 recall 边界，现均已修复。最终复核结论：“当前没有剩余阻塞项，可以进入 DeepSeek Chat/Responses 双 API 真实 dogfood。”critic 独立重跑 113 项全部通过：

- 新 memory snapshot 已进入本次 wire 时仍加一次失效预留；冷 epoch 又继承旧 epoch 预留。现在仅当旧 head 仍未被替换时保留预算，新 VALUE / CLEARED / UNAVAILABLE 已由 H(W) 支付时不重复收取。recall 的 planning placeholder 与检索结果不同，判断依赖本次结果 head 是否已替换 prior，或已精确匹配本次实际 resolved VALUE；成功检索内容未变时不会追加消息，也不再重复预留。不能仅比较 placeholder，亦不能仅凭 head 与 prior 相同就认定 stale。新 epoch 在 reservation 生产处直接返回空，连同 steer phase A 的物理字节预留一起排除旧义务。
- 聚合 wire 超限曾因存在 FULL_REQUIRED / 图片而推断该分项是原因。已删除推断函数，统一返回 REQUIRED_CONTEXT_EXCEEDS_BUDGET；独立物理边界与明确的 FULL_REQUIRED 产品失败保留。真实 artifact 失败测试仍验证没有 provider open、没有新 turn/command，已结算结果保持 SUCCESS。

新增测试没有改写历史 golden；同一冻结语义的历史身份编码继续不变。冷 epoch 不继承旧 memory 预留属于本轮修正后的实际计划内容，不是引入新的 ID 算法。


### 13.4 DeepSeek 真实验收

在静态测试和同一 critic 最终复核完成之后执行，使用有效 Pulsara home 保存的 `deepseek-flash` / `deepseek` 两个 API 连接。配置只读；每次运行独立临时 home/workspace 和经校验的本机 disposable PostgreSQL，结束时关闭 runtime 并删除测试数据库。没有替换 provider usage 或使用 mock。

执行入口：`tests/dogfood/run_unified_final_input_budget_dogfood.py`；最终报告 `output/unified-final-input-budget-20261007/deepseek-dogfood.json`，状态 PASSED。

| 实际 API | 实际模型调用 | 有效 usage 报告 | 使用 usage anchor 的调用 | 前缀连续性相邻检查 | scope / epoch 组 | 实际压缩 |
|---|---:|---:|---:|---:|---:|---|
| Chat Completions | 19 | 19 | 15 | 15 | 3 | COMPACTED，含真实 summary 与 successor |
| Responses | 17 | 17 | 13 | 13 | 3 | COMPACTED，含真实 summary 与 successor |

36 次调用均正常 COMPLETED。每条 API 均完成文件工具、多轮带转义符/中文的历史、被实际接纳并体现在回复中的 steer、独立 scope 的子代理完成、真实压缩、后继 epoch 文件工具调用。每条 API 的 4 次 idle preview 没有 provider open，input_tokens / budget_source 与同次最终 wire quote 相等。

每个 anchored call 验证 `B = anchor.reported_input_tokens + estimated_suffix_tokens` 以及 `estimated_suffix_tokens = H(current) - H(anchor)`，同时核对 anchor 的 session / scope / epoch。实际例子：Chat 一次 follow-up 的 H 为 30,538，B 为 27,453 + 695 = 28,148；Responses 一次 follow-up 的 H 为 30,635，B 为 27,446 + 922 = 28,368。校准值低于 raw H 时没有取 raw 下限。

前缀检查对同一 scope / epoch 的实际 provider input 比较：SYSTEM/tools 等固定部分相等，messages/input 保持 suffix append。两个 API 的 root 前后 epoch 和 worker scope 独立，未复用前驱 epoch 校准。

前三次 fixture 失败的 JSON/log 原样保留为 `deepseek-dogfood-attempt1/2/3.*`：第一次模型额外读取不存在的 AGENTS.md；第二次 steer 等待任意 active turn；第三次虽已匹配当前 command，仍早于 canonical turn 接纳，返回 STEER_TARGET_STALE。修复仅补测试工作区 AGENTS.md，并等待本 command 的实际 provider open 后注入 steer；保留所有成功/接纳/结果断言，没有改变生产代码。第四次完整运行通过。

### 13.5 最终范围

本轮按 hard cut 删除重复 semantic 预算及公共数值载荷，保留必要的局部产品预算和私有历史身份编码。最终准入、降级、输出额度与预览共用 wire quote 及其合法 usage 校准；memory 的物理预留与实际 wire 后缀消费不重复。

审阅最后一次修复后回归 113 passed，critic 独立复跑同样 113 passed；之前完整后端 2,975 项及前端 633 项通过。Ruff、compileall 与 diff whitespace 检查通过。前端 lint/typecheck 的存量失败仍如 §13.2 所列，本轮未修改前端文件，也未把它们纳入“已通过”声明。


### 13.6 后续前端静态问题修复（用户另行授权）

§13.2 / §13.5 记录的是 budget 验收当时的前端结果。用户随后授权修复全部前端 lint 和类型问题，本次已完成：

- 插件连接编辑器只要求实际消费的 name / enabled / packageInstallId，移除为满足完整插件 DTO 而构造的占位字段；完整 UserPluginCapability 的 hookCount 契约保留。
- 中断详情组件解构 hover hook 返回值，消除 refs 混合对象推断引起的 16 条报错。
- 控制权变化时，使用有条件的本组件状态调整关闭编辑器和菜单，保持失去控制权后关闭、恢复控制权后不自动重开的语义。
- 清除 6 个过时的 callback 依赖项；定时任务清理通过稳定回调使当前请求序号失效，继续覆盖手动 reload 和预览的过期响应。
- 没有新增 lint 规则禁用或降低检查等级。

结果：ESLint 0 errors / 0 warnings；TypeScript noEmit 通过；前端 40 个测试文件、635 项测试通过，包含新增两项控制权丢失及恢复回归。测试继续使用当前 Node 环境所需的 NODE_OPTIONS=--no-experimental-webstorage；原有 633 项保留。git diff --check 通过。

证据：`output/unified-final-input-budget-20261007/frontend-tests-lint-fixes.txt` 与 `frontend-lint-fixes-verification.json`。本次仅修复前端问题，不改变已经通过真实 provider 验收的预算生产代码。
