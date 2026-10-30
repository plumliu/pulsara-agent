# Pulsara：压缩配置沿用、idle 手动压缩与共同输入安全点实施规格

日期：2026-10-30。状态：**实施与本规格要求验收完成（方案 1）。** 实际执行、证据层级与验证边界见 [最终验收报告](PULSARA_COMPACTION_REQUEST_START_ACCEPTANCE_REPORT.zh.md)。

本文规定三类压缩的模型配置、输入接纳、执行顺序和停止语义，并给出复用现有机制的实施边界。生产实现、协议接线、测试与必要基线调整遵守本文的同一 hard cut。

## 1. 产品合同与范围

压缩是一次特殊的模型调用：模型根据既有历史生成对话摘要，运行时校验并采用新的上下文。它不重新执行原任务，不重启后台对象，不修改已经接受的历史。

须区分两种内容：

- **模型摘要**：通过模型调用归纳对话、任务进展、结论与后续需要的信息。本次模型与推理配置沿用规则作用于这次调用及其合法修复调用。
- **运行时交接**：PMCA 状态与完整 TODO 由现有运行时收集和装配，继续遵守各自合同，不把它们当成已经执行过模型摘要的证明。

三类操作的共同语义如下：

| 场景 | 模型配置来源 | 执行归属 | 完成后的动作 |
|---|---|---|---|
| idle 手动压缩 | 本次手动操作接纳时的 session 选择 | 独立上下文操作，不创建普通 turn | 安装 idle base；之后到来的普通请求按队列执行 |
| turn 内自动压缩 | 当前 turn 已捕获的配置 | 当前 turn 的执行过程 | 经过共同输入 safe point，继续当前请求 |
| 新请求首次调用前压缩 | 本次新请求已捕获的配置 | 已取得执行位置的新请求的准备过程 | 原始输入在前，补充在后，再进行第一次普通模型调用 |

ROOT 与 Subagent 共享后两种语义。Subagent 的配置来自该 Work 接纳时冻结的配置，不读取 ROOT 后来修改的界面选择。手动压缩只开放给 idle ROOT，不增加 Subagent 手动压缩入口。

### 1.1 必须保持的边界

- 根 [AGENTS.md](AGENTS.md) 及本轮用户决定优先。本文覆盖相关旧合同中与本次配置来源、首次调用前活动边界和压缩后输入接纳冲突的部分。
- 同 epoch 的 SYSTEM、provider tools 字节不变，messages 只追加。仅沿既有 cold epoch 或明确采用的 compaction successor 重建根输入。
- 压缩前后，当前请求的原文、开启 entry、权限与绑定身份保持；后续补充不替换原请求，不与原请求合成一个 entry。
- 不增加压缩表、消息表、Work 状态表、持久 job、事件种类、subject、append guard、registry 或新指纹。当前请求、turn、queue、命令与摘要采用事实仍由原有关系保存。
- 不新增持久执行恢复、回执账本、重放调度、租约或新的请求调度框架。不增加总历史、turn、调用次数、重试次数或执行寿命上限；现有真实资源边界保留。
- 不改前端布局、文案和交互流程，不新增“直接发送 steer”入口或压缩专属页面。必要的协议接线与既有状态投影适配不构成新增产品表面；idle 门禁必须在后端成立。
- 所有行为变更一次 hard cut；删除被替代的执行分支，不保留兼容执行双轨。

继续复用 [Agent／Work 设计](PULSARA_SUBAGENT_WORK_CODE_MODE_DESIGN.zh.md)、[Code Mode 合同](PULSARA_CODE_MODE_TOOL_SUBSET_DESIGN.zh.md)、[runtime handoff 与资源退役规格](PULSARA_RUNTIME_HANDOFF_AND_RESOURCE_RETIREMENT_IMPLEMENTATION_SPEC.zh.md) 和 [工具查询与身份规格](PULSARA_TOOL_QUERY_AND_CELL_ID_UNIFICATION_IMPLEMENTATION_SPEC.zh.md)。PMCA 的 3,200 V3 token 局部预算、完整 TODO、后台资源生命周期、权限与查询范围不因本次变更扩大或缩减。

## 2. 模型与推理配置

### 2.1 一次选择，沿原请求传递

配置包含连接、已解析目标及完整 `ReasoningSelection`，包括现有 effort、budget、toggle 等合法形式。不能只复制一个 effort 字符串。

- 手动操作接纳时捕获当前选择。摘要和 idle base 使用这个目标；旧 terminal turn 的配置只属于历史，不负责本次选模。
- 自动压缩使用所属请求的 `ModelCallBinding` 与其合法准备得到的目标。首次调用前压缩即使用旧历史作为摘要来源，也使用新请求的配置。
- 普通输入排队后，使用其 queue 行已经捕获的 binding；消费队列时不读取最新界面选择替换它。
- 排队输入被用户转为 steer 后，不再拥有独立未来调用的配置选择权；它继承目标 turn 的配置。
- 摘要、摘要修复、实际 provider open 和压缩后的普通调用不得悄悄改用默认 reasoning、旧模型或最新界面配置。

例：R1 使用 A 运行，用户把输入框改成 B。R1 的普通调用和自动摘要仍使用 A；之后接纳的 R2 可以使用 B。idle 下先选 B 再点击手动压缩，摘要使用 B。手动操作接纳后再改选择，不影响该操作，也不额外触发调用。仅修改选择本身不触发压缩。

### 2.2 复用已有配置载荷

沿 `ModelCallBinding`、`PreparedKernelModelTarget`、`FrozenModelResolutionSnapshot` 和现有 resolution owner 传递配置，不增加“压缩配置 owner”或持久配置副本。epoch target bundle 不包含所选 reasoning，不能仅从 bundle 恢复完整选择。

`resolve_compaction_summary_call` 直接沿用已选 target 与 binding，只指定摘要调用用途。去掉清空 reasoning 后调用 `default_reasoning_selection` 的逻辑。普通初始化、设置更新和普通发送原有的 reconcile/default 机制继续使用；摘要不新增一轮默认选择。

手动准备读取同一份解析观察并验证选择；若需要从 snapshot 构造目标，在现有 resolution owner 内抽取共用构造逻辑。不要先冻结一份合同，又从最新 catalog 重建同名目标。准备与命令接纳必须确认同一选择和历史源；若期间发生冲突，明确结束本次操作，不暗中换配置或换历史。

配置不可用就明确失败。普通请求与 Work 捕获完整 binding，不永久冻结 catalog、allowance 或凭据；实际准备沿现有合法 resolver 观察，已安装 epoch 的 target bundle 继续保持冻结。provider open 继续执行原合同校验，不保证物理连接始终可用。会话设置和保存配置不因压缩被修复或改写。

## 3. 手动压缩：只接纳 idle，必须调用模型

### 3.1 idle 的含义

ROOT 没有当前请求准备、执行或结算，没有待启动的普通新请求，没有其他占用中的手动操作或冲突接纳，才允许新的手动压缩。尚在进行的 prompt admission、Hook 准备、外部新轮接纳、successor 安装与关闭等真实 owner 同样属于忙，不能只看有没有流式输出。

- 手动压缩不进入普通请求队列，不抢在已有排队输入前面，不登记为稍后执行。
- 忙时立即返回忙。被拒绝的操作在 turn 结束后不能复活。
- 手动操作先接纳后，用户仍可继续提交普通输入；这些输入正常保存，但等待该操作结束后才启动。
- 后台 Process、Cell 和其他 Agent 的独立工作不使 ROOT 自动变忙，也不因手动压缩被停止。
- 空会话或没有可压缩内容时，不接纳摘要执行，不调用模型；可沿现有结果报告 `NOT_NEEDED`，不能报告生成了摘要。

复用现有 Host 排他边界与 manual request/future 表达占用，不增加第二份 busy 权威。命令接纳必须与普通输入接纳、请求启动及设置更新正确串行化：既有排队输入先成立则 manual 忙；manual 先成立则后来输入入队等待。不得通过长期持锁或拒绝后来入队实现排他。

### 3.2 必须执行摘要

有可压缩内容且操作被接纳后，**必须进行模型摘要**，不受自动触发阈值限制。摘要准备、模型调用或采用失败时如实失败，不以“上下文还装得下”或“模型切换成功”代替摘要完成。

手动操作直接复用目标模型历史投影、预算规划和摘要生成机制，包含同模型与跨模型场景。它不先走无摘要直接切换，也不调用旧模型生成中间摘要。投影规划的必要预算检查继续保留；绕过自动触发阈值不等于绕过实际资源边界。

摘要完成只安装 idle base，不创建普通用户 turn，不生成普通 assistant 回复。运行时交接仍按既有方式装配。原本仅用于让手动操作“不实际摘要”的可选分支应删除；没有独立用途的手动 force 参数随调用链一起移除，内部自动必要性判断不因此删除。

### 3.3 相同命令与结果

继续复用 `session_commands`、manual future、owned task、原精确确认和本地结果缓存：

- 同一命令重试先识别原操作；匹配时 join 或返回原结果，不重新选模，也不重新执行一次摘要。
- 相同 ID 的不同命令语义明确冲突；新的手动命令不能 join 另一个操作。
- 原 Host 已不存在、只剩历史接纳事实时，如实返回当前结果不可得，不从旧 turn 已完成推断压缩成功，也不自动重放。
- 提交／采用结果未知时，沿原候选确认；明确拒绝、取消或配置错误不能混入未知提交重试。
- 准备失败、完成、停止或关闭均由原 owner 结算并释放占用，唤醒共同队列。调用方断开或取消等待，不自动取消已接纳的操作。

不为本功能新增持久结果保证。删除手动 active target、运行中手动消费及 active→idle 转交分支；公开结果准确区分进行中、成功、失败、忙与结果不可得，不再承诺“等待安全点执行”。

## 4. 输入排队与首次调用前的活动边界

### 4.1 一个队列，两种用途

复用 `prompt_queue_items`，不建立“压缩期间收件箱”。压缩保护约束历史截面、输入安装及采用，不应阻止合法输入进入待处理队列。入队仍执行原权限、Hook、内容资源和目标校验；仅保存待输入不得提前修改当前 transcript 或 provider 前缀，也不能仅因 queue 序号推进就使正在执行的摘要截面失效。

| 输入 | 接纳与处理规则 |
|---|---|
| 用户普通发送 | 始终按既有前端流程提交 `NEW_TURN`；压缩期间正常入队，之后独立启动 |
| 用户把某条排队输入改为引导 | 通过现有队列转换操作变为目标 turn 的 `STEER_ACTIVE_TURN`；等待该 turn 的输入 safe point |
| Agent 消息／已有完成内容投递 | 通过共同补充输入链路绑定接收者当前执行目标；不创建未来 Work，不改变来源归因 |
| 新 Subagent Work | 按现有请求接纳、依赖、容量与同 Agent 串行规则排队，不中断当前 Work |

用户普通发送不自动变为 steer，也不新增用户直接发送 steer 的 UI。转换必须原子地终止原 NEW_TURN 的未来执行用途并接纳补充；源条目已经消费或目标已结束时，沿原明确结果处理，不能重复执行或偷偷换目标。Work 不新增“转为 steer”功能。

顺序由请求用途决定，不是把所有输入混为全局 FIFO。例如 R1 正在压缩，用户依次发送 Q1、Q2，二者原本都等待 R1 结束。用户明确将 Q2 转为 R1 的 steer 后，Q2 在 R1 的压缩后 safe point 处理，Q1 仍等待 R1 结束；Q2 不再开启独立 turn。同一目标的待处理 steer 按补充实际接纳时的 queue 顺序处理；排队转 steer 使用转换事务分配的新序号，不沿用原 NEW_TURN 的位置。目标先结束时转换失败，原普通输入保留；源先被消费时不能再转换。

### 4.2 共同启动事务：turn 与首次输入准备解耦

turn 表达请求已经开始执行，不表达首次 provider 输入已经准备成功。ROOT direct、ROOT queued 与 Subagent Work 共用以下边界。Plan 自动 successor、外部 review continuation 和 NewTurn terminal observation 在各自原事务内完成真实启动，再由原 owner 承接同一首次准备与终态边界；原子事务职责见 §4.3：

1. 启动前检查真实执行资格：有效 writer／workspace、同 session 串行、队首及未取消事实、依赖就绪、可用物理容量、冻结模型选择合法、权限与 Plan 前置条件、适用的 UserPromptSubmit Hook 未阻止。等待依赖、前一 Work 或容量时仍是未开始请求，不创建 turn；无可信配置观察时沿原等待修复规则处理。
2. 在原 Host 取得执行资格，由同一个 owned task 与 `ActiveTurnCancellationIntent` 承接启动写入及其确认、首次准备、普通执行和终态。此时内部 owner 已存在，但未成立的 turn 不作为可接收消息的公开目标。取消／关闭可以作用于该 owner，并由其确认实际启动结果。
3. 原 writer 事务重验可变接纳条件，一次性创建真实 RUNNING turn、冻结 binding／permission、完整独立初始 entries、初始 context binding revision；有 queue 的请求同时改为 CONSUMED，并将 `consumed_entry_id` 指向 instruction。直接请求沿原命令身份接纳，不为统一形式制造队列项。复用现有 committed occurrences。
4. 同一 owner 确认启动结果并完成 TODO 等既有启动 finalization，再执行首次 provider 输入准备。长时间摘要只能发生在真实开始之后；接纳成功后的 finalization／准备异常必须进入原 turn 的终态结算范围。
5. 只有成功开始的真实目标才能公开为活动 turn、接收 steer／Agent 消息并用于 Work wait／stop。DB 成立到活动投影更新之间仍由上述 owner 负责；不得留下可用 Host 内无执行或结算 owner 的 RUNNING turn。

启动事务在事务内分配真实 entry sequence 并继承当时有效的 canonical 历史基底，不再要求完整 `PreparedRootProviderInputAdmission`、semantic input 或 wire quote。删除仅为对接预编译输入而存在的预期历史头／base 匹配及对应 stale-head 重试；命令身份、模型选择、权限、Plan handoff、队首、依赖和内容引用校验仍保留。耗时历史投影、摘要、最终能力与 wire 预算检查、物理 provider open 发生在启动之后。

启动返回原 accepted 身份、按实际 sequence 排序的初始 entries 身份、组首前驱截止及初始 revision 的实际基底，供当前执行 owner 使用；可以扩展现有 typed carrier，不建立持久 receipt、组边界字段或注册表。启动确认与随后执行是否仍被允许分开，详见 §7.3。

初始 revision 表达继承的 FULL_HISTORY／SNAPSHOT 历史基底，**不是 provider-ready 标志**。它与 turn、entries 同事务成立；首次摘要采用产生该 turn 的后继 revision。保留现有单 RUNNING、initial entry 归属、queue 消费关系和 pending steer→RUNNING 约束，不以清空 current binding 或放宽外键实现解耦。当前方案不需要新增表列；若实施发现真实基线合同需要修改，可按 clean-v0 hard cut 调整，不能为维持旧 schema 引入平行状态，也不为“允许改 schema”增加无产品用途的字段。

### 4.3 活动目标、独立 entries 与共同副作用

请求成功开始后，即使还在首次调用前压缩，也接受针对它的补充并允许停止。当前 Work W1 活动、W2 排队时，消息针对 W1，不改投 W2。接收方无活动目标但有等待 Work 时返回 `not_started`；无工作或空闲时返回 `inactive`。未加载且只有等待 Work 也按未开始处理；这些失败结果均不接纳正文、不自动加载或启动工作。idle 手动压缩不创建普通 turn，不单独成为 Agent 消息目标。

原始 instruction 是 `initial_entry_id` 对应的开启 entry，但不必是该 turn 第一条 entry。Subagent profile、initial context、instruction、依赖结果／其他材料按现有顺序各自保存；每条后续补充也保持独立 entry。不能把这些内容拼成 instruction，也不能合并多个普通新请求。Agent 来源归因保持，不能因复用共同队列而获得人类 Plan 授权或人类专属控制副作用。

- UserPromptSubmit 等启动前 gate 不因重试重复运行；Hook 未许可时不创建活动 turn。Hook context 绑定原请求，不污染前驱摘要。
- TODO activation 跟随真实启动且 exact-join 原始 instruction，失败／终态沿原 finalization 收束；不等到 provider open 再激活。
- SessionStart、PreCompact、PostCompact 各自按既有合法 epoch／压缩采用边界运行一次；创建 RUNNING 本身不额外触发它们。启动后 Hook 阻断结算同一 turn。
- memory policy、recall 与 hint 由真实输入及来源触发，不能仅因创建 turn 重跑；原 HUMAN_MESSAGE 与后续 steer 的处理顺序保持。
- Plan continuation 的 workflow 更新、权限、origin 结算及 successor 创建在原事务内原子成立，不要求预编译 provider admission；确认后读取实际初始组，由原 Host task／handoff 与 cancellation owner 进入共同首次准备，不能拆成两次独立提交。NewTurn terminal observation 同样先在原发布事务建立真实 turn 与 observation，再确认并进入共同准备；ExistingTurn terminal observation 与 Plan question 的 active suffix 保留发布前精确准备。

### 4.4 首次输入组边界与新 turn 采用

首次普通模型调用之前，压缩只处理本次完整初始输入组之前的历史。定义三个不同截面：

| 截面 | 含义 |
|---|---|
| 前驱历史截止 | 本次完整初始组最早 entry sequence 之前；限定模型摘要可替换的范围 |
| 当前 canonical safe head | 本次冻结读取的真实历史末端，包含已经落库的完整初始组；用于采用时精确核对 |
| 待输入截面 | safe point 读取的目标 steer queue 前缀；这些内容尚未消费时不属于 transcript |

不能用 instruction 的 sequence 减一作为前驱截止，否则它之前的 profile／initial context 会被误当成旧历史。也不能使用初始 revision 的 `source_through_sequence` 代替请求起点；继承 SNAPSHOT 时该值指旧摘要截止。边界优先来自启动确认的实际有序输入组，也可由新 turn 的 executed entries 最小 sequence 推导，不另存持久边界。

**摘要 source 来自前驱历史，采用 target 始终是当前新 RUNNING turn。** 沿 active adoption 更新该 turn 的 snapshot／binding revision；采用的 safe head 包含当前完整输入组，摘要 source cut 则不超过前驱截止。不能继续对旧 terminal turn 执行 IDLE_BASE_ONLY 再期待新 turn 自动继承变化，也不能删除 canonical-head 检查来放过冲突。

复用 `build_synthetic_compaction_dispatch_read` 的精确 suffix 保留机制，将摘要边界之后的 canonical entries 原样接在新 base 后；不重接纳请求、不重复插入初始 entries、不改写旧 turn。首次准备中的同目标 planner、目标投影、图片能力判断和近期窗口选择都必须识别完整初始组，不能只排除 `initial_entry_id`。当前组的图片不能按历史图片降级，原文不能被模型摘要替代。

上述完整初始组保护持续到该请求第一次普通 provider 调用；后续 turn 内压缩继续遵守既有当前请求精确保留合同，不把初始组永久冻结成不可压缩历史。没有可压缩前驱时，合法请求正常继续；若实际输入无法容纳，则明确资源失败，不生成空摘要或概括当前请求来绕过限制。

## 5. 压缩后的共同输入 safe point

压缩期间到达的合法补充不取消摘要、不重启摘要、不修改已经发给摘要模型的输入。摘要采用完成后、下一次执行任务的普通模型调用之前，必须经过共同输入 safe point。

```text
turn 内自动压缩：
已采用的上下文 → 当前 turn 的待处理补充 → 继续执行任务的模型调用

新请求首次调用前压缩：
已采用的前驱历史 → 本次原始输入组 → 本次待处理补充 → 第一次普通模型调用

idle 手动压缩：
已采用的 idle base → 等待中的普通请求按顺序启动，各自走共同输入链路
```

### 5.1 复用输入消费与预算机制

1. 由原输入 owner 读取当前目标一个确定的待输入截面，按已有单次资源界限选取有序前缀。
2. 沿 `SteerConsumptionCoordinator`、现有 suffix 规划和事务确认，将选中的补充各自保存为独立 entry，并加入本次完整调用输入。
3. 使用实际最终 wire 重新校验能力、预算、权限及输入连续性，才允许 provider open。
4. 截面之后到达的消息以及因已有单次资源界限未选中的后缀继续等待下一次边界；不持续追逐新消息使模型永远无法启动，也不新增消息总数上限。
5. 补充导致输入无法容纳时，沿现有共同输入预算恢复／明确资源失败处理，不静默删掉它、不绕过预算。仅有消息到达不触发摘要重启。

压缩后 safe point 已选中且能合法容纳的补充，应进入随后的第一次普通调用；不能为了先消费旧 prepared dispatch 而固定多等一轮回复。正常完成仍由共同完成事务与待输入裁决；停止时则按 §7 结算，不能混用正常完成语义。

### 5.2 采用、共同 suffix 准备与最终绑定

压缩 owner 负责摘要 dry compile、canonical adopt／confirm、既有压缩 Hook 与采用后精确读取核对；它转交的是原 owner 持有的已采用 base 及尚未最终绑定的调用准备资源。共同 provider-input owner 在这个 base 上执行 §5.1，再形成可执行调用。

**`bind_prepared_executable_wire_input` 与 `install_provider_open` 都必须在 safe point 最终选定输入之后。** 前者已经请求 continuity transition，并可能消费 adopted seed 的一次性 authority；只推迟 install 或网络发送不够。摘要前后的预算预检可以继续 dry-run，但不得提前消费供后续普通调用使用的唯一采用权限。

continuation 权限按真实前驱分型：已有 installed epoch 时，转交真实 `AdoptedCompactionContinuationSeed`；尚无 installed epoch 时，保留原 Empty adoption 的 FULL 确认、准备资源释放及 adopted-base bootstrap lease 重封，再以采用后精确 canonical read 建立合法 `CanonicalColdContinuationSeed`。两种权限不能互换；fresh Host 的长历史首次准备和无 installed epoch 的手动压缩均须支持。这仍使用既有 adopted-successor／cold 边界，不新增重建边界或租约机制。

复用现有 `PreparedCompactionCandidateFamily` 的 suffix 变体、上述真实 seed／bootstrap 权限、handle／surface borrow 与共同 steer 消费机制。在现有 ProviderDispatch owner 内抽出共享步骤：

1. 保持与真实前驱匹配的 seed／bootstrap 权限、snapshot／base、权限／Plan 和冻结 capability cut；普通 base 与 adopted base 进入同一有序补充选择与消费逻辑。
2. 根据选中 suffix 的真实来源，复用现有 trigger 分类、memory policy、recall／hint 与 invalidation 预算计算。非 trigger 根事实及已经冻结的 SYSTEM／tools 不借机重新解析或替换；受允许的 suffix 变化按现有合法追加规则处理。
3. 对候选完整输入做 dry compile 和最终 wire 测量；确认 queue 消费后 rotate／重读实际 canonical cut，精确核对选中候选。
4. 保留一次性 Hook context reservation 的真实持有者与消费语义，不通过 close/replan 丢弃已选 context。最终候选确定后才绑定 transition／permit、安装 provider open。
5. 无 suffix 且输入未变时可复用仍有效的准备结果；停止或失败时由唯一持有者退役未发送资源，采用事实保持。

已有 installed 前驱时，不能直接调用现有普通 `prepare` 并让它以 `CanonicalColdContinuationSeed` 覆盖 adopted seed；合法 Empty cold seed 则按上述分型处理。也不能只把文字拼到冻结的 compaction family 上而绕过 memory 等触发语义。这两个现有路径应在原 owner 内补齐共用接口，不建立另一套 compiler、steer 消费器、permit 注册表或回收框架。

不得改写已冻结 request、沿用与新 suffix 不匹配的 quote／permit、重复采用摘要或重建已安装 epoch 的根输入。转交一个实际 continuation owner，不同时返回多个可独立执行的 successor。ROOT 首次准备、turn 内压缩和 Subagent 使用同一边界。

## 6. 摘要路径与模型切换

| 情况 | 执行路径 |
|---|---|
| idle 手动压缩，有可压缩内容 | 直接按本次目标模型规划历史投影，并调用该模型摘要 |
| 自动压缩，目标与现有 epoch 相同 | 复用现有同目标 source／摘要机制，显式沿用所属请求配置 |
| 新请求切换 A→B，完整输入可以合法直接交给 B，且无需摘要 | 保留既有直接切换与最终 wire 校验 |
| A→B 确实需要摘要 | 复用目标 B 的历史投影和摘要机制，由 B 生成摘要 |
| 目标模型或合法投影不可执行 | 明确失败；不换回 A、不改默认推理配置 |

目标模型投影复用 `prepare_compaction_source`、`_select_destination_projection_summary`、`prepare_destination_projection_summary_semantic` 及原候选预算规划，使手动同模型压缩也能直接调用这套机制，而不伪造“发生了模型切换”。

保留历史材料、prior handoff、近期请求、工具结果证据及图片的现有规划规则。B 已知不支持图片时，历史材料可沿已有引用投影；本次精确请求包含 B 不支持的图片时明确失败，不把新图片静默降级。未知能力不能当成已知 text-only。材料省略沿现有通知机制如实说明，不承诺缩减投影与完整历史具有相同信息覆盖。

删除用 A 先生成摘要再交 B 的中间路径，以及只为这条路线存在的 numeric tier 和 fallback 调度。普通无需摘要的直接切换、同目标摘要和目标投影摘要各有真实用途，按语义分支组织，不保留常量 tier 包裹死代码。

所有摘要继续使用原摘要校验、合法 repair、promotion、采用确认和实际输入预算检查。summary repair 只按原合同追加纠正内容，不重选配置。临时投影不等于已安装 epoch；只有明确采用后才能改变有效上下文。

## 7. 失败、停止与运行状态

### 7.1 自动失败能否继续

| 情况 | 行为 |
|---|---|
| 提前达到自动触发阈值，摘要确定失败，但未采用新上下文且原完整请求仍合法 | 报告失败，允许原任务继续；必须实际检查完整请求的能力与预算 |
| 没有摘要就无法合法调用，包括硬预算不足或目标能力不匹配 | 明确结束当前请求，保留历史；独立队列请求仍可按调度执行 |
| 用户明确停止当前请求 | 停止该请求及其压缩，不因旧上下文还能调用就继续原任务 |
| 摘要已采用，后续调用准备失败或被停止 | 保留已经成立的采用结果，结算当前请求，不回滚成旧上下文 |
| 采用结果未知 | 先由原 owner 精确确认，不宣称未采用并重新执行另一次压缩 |

复用现有 automatic failure 处理与物理约束，不在同一未变化输入上立即循环触发刚失败的软阈值摘要。允许继续时仍走共同输入 safe point，对加入补充后的完整请求验证，而非只检查摘要失败前的旧估算。自动失败的可继续分支不适用于手动压缩，也不能吞掉停止、Host 关闭或 writer 失效。

### 7.2 同一 owner 承接停止、失败和关闭

同一个 Host task／cancellation intent 从启动资格、写入确认、TODO finalization、首次准备、普通执行一直负责到终态；首次准备必须位于这个终态异常处理范围内。仅提前 INSERT、仍让准备异常从外层抛出不构成完成。清除 Host 活动槽不等于已经中断数据库 turn。

- 停止 idle 手动压缩只结束该操作，不制造假 turn。复用现有停止按钮及控制 attempt／query：`STOP_CONTEXT_COMPACTION` 精确指向 `MANUAL_COMPACTION` 的原命令 ID，和 `STOP_ACTIVE_TURN` 的 ROOT turn 目标严格区分；不新增按钮、布局或文案。当前手动命令 ID 由真实操作 owner 派生到既有 live snapshot／observation，结束后清除；旧 ID 不得停止后来的操作。
- 停止自动压缩结束所属请求，包含首次调用前阶段。保留首个有效 cancellation cause，不在摘要后新建 intent 或把 USER_STOP 改成普通 provider 错误。
- started turn 的准备失败、Hook 阻断、最终预算／能力失败及停止，均由原结算 owner 结束同一 turn；原 queue 保持 CONSUMED，原始 entries 保留，不退回 PENDING。
- turn 终态与其全部未消费 steer 沿共同事务结算，不转投下一轮。消息与停止竞争时，以 writer 事务先后决定接纳或拒绝；不能只清一批就遗留指向 terminal turn 的 PENDING steer。
- 独立排队请求及其他后台 Agent／Work 不连带取消；显式依赖成功的 Work 继续服从原依赖规则。R1 停止后 R2 可以启动，并用 R2 的配置再次触发压缩。
- 物理容量在安装 §4.2 的执行 owner 前由原调度启动过程持有；安装成功即交给该同一 owner，覆盖启动写入及确认、finalization、首次准备与执行，最终在其 finally 中释放。安装前失败由原调度过程释放；安装后不再交给另一执行 task。终态／资源结算完成后才清槽并唤醒后项，不提前或重复释放。

单个调用者断开或取消等待不撤销已接纳请求。显式 stop、Host close 和 writer replacement 沿原控制入口作用于真实 owner。已经开始的 canonical 结算由同一个 shielded settlement drain 到确定结果，重复 stop／close 不能取消它后直接清槽。等待 settlement 完成还不够：其返回的 adopted base、未发送 dispatch、borrow 和 Hook reservation 必须由真实持有者显式收束，不能丢弃携带资源的结果。

采用 FULL 后停止，保留已成立 snapshot／binding 并退役未发送调用；取消不能触发另一配置的摘要或让被丢弃的 dispatch 继续发送。采用是否成立未知时先按 §7.3 确认。

崩溃后沿现有 writer takeover 规则处理：启动未提交则请求仍未开始；启动已提交则 takeover 中断该 RUNNING turn 并结算其 steer，不重放原执行。崩溃到接管之间数据库暂存 RUNNING 是既有事实，不为消除这段窗口增加恢复 job 或租约。没有显式成功依赖的后续请求继续按原调度运行。

### 7.3 确认接纳事实与当前执行资格分开

启动确认验证原命令／queue 身份、冻结配置和权限、独立初始 entries、初始 revision 及消费关系等原事实。不能要求 turn 仍 RUNNING 或 current binding 仍指初始 revision；停止、接管或合法摘要采用不能把已成功启动改判为 CONFLICT。初始 revision 的确认核对其原内容，不从后来变化的前驱状态重推一次“现在应该是什么”。

启动确认 FULL 后另读原目标当前状态，并检查 cancellation／writer 资格，决定继续准备还是只完成结算。确认前不得启动摘要或普通调用；重试也不能再次激活 TODO、消费 queue 或插入初始 entries。

| 写入／确认结果 | 处理 |
|---|---|
| FULL | 原事实已成立；接管同一结果，按当前状态继续或结算 |
| CONFLICT | 明确结束本次候选，不重新生成身份或输入 |
| NONE，原错误是确定业务拒绝 | 返回原拒绝，不循环重发无效候选 |
| NONE，已经请求取消／关闭 | 不重发；释放未启动资格，按实际目标状态结算 |
| NONE，仍有执行资格且确属可重试 I/O 失败 | 沿原候选重试，事务重新检查真实接纳条件 |
| 已确定 writer 失效或被替换 | 不再重发、不启动摘要或普通调用；join 已开始的物理写入并收束进程内资源，退出原执行资格；canonical turn 由既有 takeover 规则结算，不新增恢复机制 |
| 结果仍未知，且没有已确定的 writer 失效 | 保留原候选与 settlement owner 继续原精确确认，不当成 NONE；停止／关闭禁止重发和继续执行，但不能跳过已开始写入的确认与资源收束 |

摘要采用沿原 FULL／NONE／CONFLICT 确认合同同样区分确定失败与未知结果。不得用覆盖所有 `BaseException` 的 catch 将取消、权限／Plan 拒绝、无效配置等无限重试；不以新增总重试上限掩盖错误分类。

### 7.4 状态与外部观察

不新增 session／turn 的持久 PREPARING／COMPACTING 枚举。活动请求准备、自动压缩和普通调用均属于原 RUNNING turn；idle 手动压缩仅有操作 owner。压缩进度复用现有进程内 phase、manual request 与 disposable projection。

query_command、Work 状态、Agent wait、stop 和既有 UI active target 从同一真实接纳事实与 live owner 派生。首次压缩期间应能指向原 turn／Work，无须前端重设计。session 的 runtime 已加载状态不改成请求运行状态；手动压缩结果也不能从旧 turn 终态推导。

没有当前 owner 的历史压缩结果不可得，不代表正在压缩，不占用执行资格，也不使界面永久 busy。

## 8. 共享接口、依赖与删除范围

下表规定职责，不要求新建同名类或服务；在现有 owner／typed carrier 内完成。

| 共同边界 | 输入与输出 | 现有落点 |
|---|---|---|
| 请求开始 | 输入确定请求身份、初始输入组、binding／permission 和真实启动条件；输出 accepted turn／entries／初始基底及实际组边界，不携带预编译 wire；Plan／terminal 的新 turn 保持各自原子事务 | `turn_admission.py`、Host、`_repository/conversation.py`／`prompts.py`／`plans.py`、原 terminal publication |
| 首次输入准备 | 输入已开始的 turn、初始组保护边界及同一 cancellation intent；输出合法普通输入，必要时由该 turn 采用摘要 | `runner.py`、`provider_dispatch.py`、`compaction/coordinator.py` |
| 压缩采用与 continuation | 输出一个持有与 Installed／Empty 前驱匹配的真实 continuation 权限和未最终绑定资源的 owner，由共同 safe point 接手 | 原 coordinator、candidate family、continuity |
| 共同输入 safe point | 输入 ordinary／adopted base 和一次待输入截面；输出精确消费并最终测量、绑定、安装的调用 | ProviderDispatch、`steer_consumption.py`、原 writer |
| 终态结算 | 接收原 cancellation／失败原因与实际接纳／采用结果，收束原请求和资源，再释放执行资格 | Host task、原 settlement、turn interrupt、Agent runtime |
| 模型选择 | 传递原 binding／完整 target，摘要只改变调用用途，不重新默认选模 | `llm/resolution.py`、`llm/runtime.py`、`direct_model.py` |
| 手动操作 | idle 接纳、目标投影摘要、idle base 和原 future 结算；既有队列优先，后来输入正常保存 | Host、`compaction/runtime.py`、原命令 writer／query |

实施依赖顺序：先拆开共同启动与 provider 准备，覆盖 started turn 的完整 owner／确认／异常范围；再接入新 turn 的受限前驱摘要和完整初始 suffix；最后贯通 adopted base 的共同 safe point 与最终绑定。配置载荷和手动目标投影在原 owner 内接入同一摘要能力。Hook、Plan、TODO 和 memory 的边界随上述 owner 一起验收，不另加协调框架。

以下被替代路径必须删除，不能保留恒定分支、参数别名或 wrapper：

- 新 turn 创建对 `PreparedRootProviderInputAdmission` 的依赖，包括 Plan continuation 和 NewTurn terminal observation；删除只为预编译输入存在的 prospective head/base 预期与重试。
- 普通新请求尚未发布时借旧 terminal turn 压缩的 `recover_pending_root_input`／`execute_pending_root_model_handover` 及专用重接纳流程。
- `pending_root_turn_id` fence 特例、terminal retarget、prospective-root 临时输入／重接纳及其 candidate／admission／dispatch／activation；所有新 turn 入口接入真实启动后的共同准备，删除 runner `pending_prospective_root_dispatch` 接力。
- 压缩后先绑定安装、绕过共同输入 safe point 发送首个普通调用的路径。
- 运行中手动压缩及结束后转交 idle 的分支、无用途的手动 active-target／force 协议接线。
- 摘要重选默认 reasoning、A 先摘要再转 B 的中间路线及专属 numeric tier fallback。

Plan review continuation、自动 Plan successor 和 NewTurn terminal observation 均由原事务先建立真实 turn；确认后沿现有 `read_started_turn_input_group` 读取开启 entry 与初始 revision 的事实，再进入 §5 的共同准备与 safe point，不保留 root prospective carrier。Plan 的 workflow／权限／origin／successor 原子性、已有 pending handoff 的取消收束及 terminal observation 的精确 ACK 确认保持。Plan question、ExistingTurn terminal observation 等未发布 active suffix，及 Empty adoption settlement／bootstrap 重封、普通 source 重借用、摘要 repair、权限校验与采用确认具有独立用途，保留其实际 caller 所需职责，不因名称包含 pending／prospective／frozen 而机械删除。

纯 queue 接纳和转换不登记成会修改当前 transcript 的 compaction write reservation；它们仍受原 writer、目标与权限检查。实际 transcript 修改、final 裁决、输入安装和采用继续保留各自保护。手动 idle 的接纳判断需同时覆盖已有队列和正在进行的接纳，不能因开放入队而失去互斥。

不持 Host 锁等待 DB I/O、Hook、摘要、summary lane 或物理关闭。生产 caller、Protocol、有效测试及必要基线合同在同一 hard cut 更新。schema 不是禁止修改的边界，但本方案没有新增持久结构的需求；维持单一 canonical truth，不为内部未发布设计增加兼容迁移链。

## 9. 验收与完成条件

下表要求行为证据，不要求再造证明用事件、状态或持久记录。使用真实 owner、可控 transport、事件屏障和 PostgreSQL 事务验证竞争；不只检查方法被调用。

| 场景 | 必须验证 |
|---|---|
| 手动 idle、历史很短、同模型 | 仍实际调用所选模型摘要；不受自动阈值拦截；无普通 turn／assistant 回复 |
| 手动先选 B、旧历史来自 A | 直接 B 投影摘要，无 A 摘要，无仅切换即成功 |
| 空历史、已有队列、活动任务、Hook 准备或其他手动操作 | 无摘要／明确忙；被拒绝的操作之后不执行 |
| 手动接纳后用户发送 | 正常入队，操作结束后启动；失败／停止也不遗留 busy |
| 手动配置准备与设置更新／普通接纳竞争 | 由真实接纳顺序确定结果；不混用两份配置，不换历史源，接纳后仍保持原选择 |
| 三类入口选择非默认 reasoning | 实际 summary wire 与各自 binding 一致，repair 不重选默认；支持的其他选择形式不丢失 |
| 活动 turn 后修改界面配置、加入 steer | 自动摘要与继续调用保持原 turn 配置；steer 不带入独立未来配置 |
| 多条新请求分别捕获不同配置 | 各自开启 entry、各自配置；首调用前压缩不使用旧 turn 或队列后项配置 |
| ROOT／Subagent 首调用前压缩期间收到补充 | 当前目标真实 RUNNING；补充可入队；原始输入组在前、补充独立 entries 在后；无重复接纳 |
| 首次摘要 transport 屏障挂起 | DB 中唯一 RUNNING、完整初始 entries、revision0 与 CONSUMED 已成立；query／Work／live target 一致；普通 provider open 为零 |
| Subagent profile 在 instruction 前，材料／依赖结果在其后 | 摘要 cut 在整组之前；新 turn 采用后整组原文与图片保持；旧 terminal binding 不被当成采用目标 |
| 尚未取得执行位置的 Work | 无当前目标时消息未接纳；等待依赖不启动空 turn；W1 活动而 W2 排队时消息只到 W1 |
| 压缩期间普通发送及队列转 steer | 两者均能合法接纳；转换原子、原 NEW_TURN 不再执行；无新增直接 steer UI |
| R1 压缩、Q1/Q2 排队、将 Q2 转 steer | Q2 在 R1 safe point 处理，Q1 等 R1 结束；顺序、配置和 entry 身份正确 |
| 摘要调用被屏障挂起时加入 steer | 摘要不重启；采用后第一次普通 provider 请求包含截面内合法选中的 steer |
| Installed／Empty 两类前驱的采用后追加，包括 fresh Host 长历史首次准备与 idle 手动 | Installed 保持 adopted seed；Empty 沿 FULL、释放与 bootstrap 重封使用合法 cold seed；不互换权限、不伪造新 epoch 边界 |
| 采用后追加带 memory 指令的人类 steer | 保留所属分型的真实 continuation 权限；原 trigger／recall／hint／预算规则生效；最终 suffix 选定前未消费普通调用 transition 权限 |
| safe point 截面之后继续来消息 | 后来消息保留到下一边界，不倒序、不丢失，不无限追逐新输入 |
| 新 suffix 超预算或含不支持内容 | 真实最终 wire 检查生效；沿共同恢复／失败语义处理，不发送超预算请求 |
| 三种补充来源混排与正常 final 竞争 | 人类转换 steer、Agent message／completion 按实际接纳序号交付、归因不变；消息先提交则当前 turn 继续，final 先提交则不接纳消息、不投向后继 |
| 请求停止与消息接纳／准备完成竞争 | 原目标有单一结算；未消费 steer 不转投；原请求不重新回队首启动 |
| 启动前拒绝与启动后准备失败 | 前者无 turn；后者保留原始 entries、CONSUMED 和同一 INTERRUPTED turn；不能沿用“首次编译失败必为零 turn”的旧断言 |
| 启动 commit ACK 丢失，随后停止或 binding 推进 | 确认仍识别原 FULL 接纳；当前资格另判，不重消费、不重复 TODO activation |
| 确定拒绝＋确认 NONE；取消＋确认 NONE | 明确退出，不重发无效或已取消候选，不靠重试次数上限终止 |
| ACK 未知后 writer 确定被替换 | 旧 owner join 物理写入并退出资格，不无限持旧 guard 确认；takeover 结算原 turn，无重复执行 |
| stop／close 重复取消，发生在启动确认、TODO finalization 或 adoption ACK 未知处 | 同一 settlement drain；实际采用结果保留，返回资源明确退役；无额外普通 provider open |
| R1 压缩中停止、R2 排队 | R1 结束；R2 正常启动且可再次压缩；依赖成功的 Work 仍按原规则处理 |
| 容量仅容纳一份 Work，W1 首次准备中停止；或准备中进程崩溃后接管 | W1 不重放，目标 steer 全部结算；只在既有容量／终态规则允许后启动 W2，恰一次 |
| 自动软触发摘要失败／硬边界失败 | 前者仅在完整原请求仍合法时继续；后者明确结束；取消不被吞掉 |
| 摘要失败、采用失败、采用 ACK 未知 | 采用前旧上下文有效；未知走原确认；采用成立后不伪造回滚 |
| 命令重试、关闭、Host 替换与结果查询 | 不重复摘要、不冒充成功、不自动恢复；资源与等待者正确结算 |
| 后台 Cell、Process、Monitor、Work 及 PMCA／TODO | 不重启或连带停止对象；交接预算、完整 TODO、权限及查询范围保持 |
| Hook／Plan／TODO／memory 及 terminal observation 穿过首次准备与采用 | 启动 gate、force-exit、原子 continuation、一次性 Hook context、TODO 开始／结束及来源相关 memory 语义均保持；Plan／NewTurn terminal 均先真实启动再进入共同准备，不绕过普通调用前 safe point |
| 前缀与调用所有权 | SYSTEM/tools 不变、messages 只追加；新 suffix 对应新测量；无重复采用、过期 permit 或资源泄漏 |

优先扩展现有真实 Host／PostgreSQL 的 request execution、Agent session、队列转换、Hook、Plan、TODO 和 interruption 测试；将屏障放在真实启动写入／ACK、摘要、采用与首次 provider open，而非仅挂起已经开始的普通流式回复。现有 direct 和 queued 的“最终输入预算失败必须发生在 turn／消费之前”断言应改为上述启动前／后分界，不能以 skip 或弱化检查保留冲突合同。

配置、输入顺序和竞争测试通过后，使用保存生产配置做真实 provider 验证：idle 手动、首次调用前、turn 内自动各覆盖一次非默认 reasoning；另覆盖 A→B 目标投影摘要。对现有支持的 Chat Completions 与 Responses 分别检查实际 wire，保留摘要输入、输出、后续普通调用和 usage。尤其要在摘要执行期间注入补充，证明它进入压缩后的第一次合法普通调用，不能仅以 queue 行存在作为交付证据。复用现有最终输入预算 dogfood 的配置与 wire 捕获；旧 source-summary tier 或旧 Agent task API 的实验须先按当前合同更新，不能直接作为通过证据。

真实实验沿 `LocalSettingsStore` 与 `require_pulsara_home()` 读取保存配置；只读使用，不导出凭据、不使用环境密钥 fallback。需要隔离数据库时先核实本地可丢弃目标，再通过既有注入机制使用；不得重置未核实或远程数据库。诊断只精确清除实际凭据，保留有用的输入输出与错误证据。历史报告不改写为本功能已通过。

相关当前规格中的配置来源、首次调用前活动边界和 safe point 描述已同步；退役接口与分支已删除，未增加持久类别或逻辑总量 cap。真实接纳、实际 provider 输入、停止／失败与资源释放已按本规格要求验证，具体回归、真实 provider 证据及验证边界见 [最终验收报告](PULSARA_COMPACTION_REQUEST_START_ACCEPTANCE_REPORT.zh.md)。
