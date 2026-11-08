# Pulsara 统一上下文压缩与 HTTP 413 恢复实施规格

日期：2026-11-08。状态：实现、逐项审计与验收完成。本文规定 idle 手动、entry 自动、turn 内自动、模型切换以及业务 HTTP 413 恢复的统一行为。

## 1. 产品合同

### 1.1 先区分四种材料

| 符号 | 含义 | 压缩后怎么处理 |
| --- | --- | --- |
| H | 准备用摘要替代的较早历史 | 用摘要 S 承担这部分历史 |
| R | 选中继续原样保留的近期交互 | 从原 canonical rows 读取，按原角色和顺序重新投影为消息序列 |
| N | 本次需要继续原样交给业务模型的新输入，例如新 turn 的整个初始输入组、尚未消费的强制图文页 | 不进入历史摘要，沿自己的原 canonical 记录投影为独立消息序列 |
| S | 摘要模型生成的历史交接文字 | 与 R、N 一起组成后续上下文 |

H、R、N 表示内容归属，不要求它们在物理消息中正好是三个连续区段。当前活动用户请求等必须保留的原文，仍由 runtime 按既有规则保存，不能只剩摘要。

### 1.2 所有压缩共用的五步

1. **固定本次要处理的内容和配置。** runtime 冻结当前有效历史、输入截止位置，以及本次摘要使用的模型和完整推理配置。已有摘要也属于当前有效历史，不重新展开它已经替代的全部旧记录。
2. **决定哪些摘要、哪些原样保留。** runtime 根据既有完整性和保留规则选定 H、R、N。这个决定在调用摘要模型前完成，不让模型自行选择丢弃哪些消息。
3. **让模型读 H+R，生成历史摘要 S。** H 是总结对象，R 提供最近状态和更正，帮助模型正确理解 H。例如 H 还在排查播放故障，而 R 已确认修复成功，S 就不能仍把它写成未解决。这里不是禁止 S 提及 R 中的重要更正，而是不依靠 S 替代 R 的原文，也不要求再抄一份 R 的完整日志。N 不作为历史放进这次摘要。
4. **由 runtime 重新投影原记录。** 先形成一条包含“解释1+S”的摘要消息，再从已选 canonical rows 投影独立的 R、N 消息序列，得到 `解释1+S+R+N`。R/N 保留原角色、正文、顺序、工具调用及图文关联，不拼进摘要 entry，也不重复写入 transcript。权限、Plan、TODO、运行中资源等机械上下文继续由既有 owner 按当前事实提供；idle 的 N 为空。
5. **检查完整结果，再正式采用。** 测量上述完整输入，检查实际预算、内容能力、图文和工具关联，以及对应的压缩收益。通过后才采用新上下文；不把刚生成的摘要直接当成已经采用。成功后是保存 idle base，还是立即继续当前 turn，由下面三条路径决定。

### 1.3 idle 手动：先整理历史，等待下一条请求

```text
用户在空闲时点击压缩
→ 从现有历史选出 H 和 R；此时没有 N
→ 摘要模型读取 H+R，返回 S
→ 保存 S 与保留项引用；检查并采用解释1+S+原 R 消息序列+机械上下文
→ 操作结束；等下一条用户请求到来，再使用这份上下文
```

摘要使用手动操作接纳时选择的模型和推理配置。有可压缩历史时，不必等到 85% 才摘要。成功只采用 idle base，不创建普通 turn，也不立即让业务模型继续回答。操作期间后来提交的普通请求按现有队列等待，不混进这次摘要。

### 1.4 new turn entry：先整理旧历史，再处理新请求

```text
新请求取得执行位置，turn 和整个初始输入组已建立
→ 首次业务调用准备时发现需要压缩
→ 旧历史分为 H 和 R；本轮整个初始输入组作为 N 保留
→ 摘要模型只读取旧历史 H+R，返回 S
→ 从原 canonical rows 重投影 R 和整个初始输入组 N
→ runtime 检查并采用解释1+S+R+N+机械上下文
→ 业务模型收到新上下文，开始处理本轮请求
```

摘要使用本次新请求捕获的模型和推理配置。新请求虽已保存、turn 虽已启动，仍不等于业务模型已经处理过它；不能把整个初始输入组吞进旧历史摘要。上一轮结束时没有另开一个后台自动摘要任务，这条路径是在新一轮的首次业务调用准备阶段执行。

### 1.5 turn 内：整理已积累的历史，继续同一任务

```text
当前 turn 已经执行了一些模型调用和工具操作
→ 在工具后续等合法安全点发现需要压缩
→ 累积历史分为 H 和 R，按原 canonical 记录确定强制保留的新材料 N
→ 摘要模型读取 H+R，返回 S
→ 从原 canonical rows 重投影 R/N，保持整批工具调用、结果和图片关联
→ runtime 检查并采用解释1+S+R+N+机械上下文
→ 经过共同输入安全点接入合法补充，重测实际输入
→ 业务模型继续当前 turn，处理待消费结果或剩余任务
```

摘要使用当前请求的模型和推理配置，不因用户修改输入框中的模型选择而临时换模型。当前用户请求原文按已有 entry 引用精确读回，尚未消费的强制图文证据及完整工具关系沿原记录保留；已完成的工具不重新执行，也不新建 turn。工具批次尚未完成时，只能按既有 pending 批次流程先准备，等真实批次完成后再 finalize/adopt，不能提前采用。

### 1.6 共同边界

完整摘要输入因本地硬预算或历史内容能力无法执行时，所有入口统一允许复用现有历史投影：`P(H)+原样 R+摘要指令`。优先完整来源；不能因达到 85% 就提前省略材料。投影只用于摘要准备，成功采用摘要后才能改变业务输入根。

本文明确允许普通自动压缩使用这一有损来源救援；投影可能省略旧对话或工具正文，必须沿现有来源提示和用户通知如实表达，不承诺与完整原文摘要等价。canonical 原记录保留。

保留内容、模型配置与输入归属由 runtime 决定，不由摘要文字决定。摘要不执行任务或工具，不停止或重建后台对象，不创建用户普通 turn，不改变原任务权限。

### 1.7 ROOT／Subagent system 文案与实际组装

`ports/system_prompt.py` 的 `DEFAULT_SYSTEM_PROMPT` 和 `DEFAULT_SUBAGENT_SYSTEM_PROMPT` 均加入同一段简短说明。中文语义为：

> 上下文可能包含较早历史的摘要及原样保留的近期交互，两者通常只有少量内容重叠。

沿现有英文 system 模板使用：

> Context may include a summary of earlier history and recent interactions in their original message form, with usually only limited overlap between them.

采用后，摘要消息只有解释1与 S；解释1为：

> 以下是较早历史的摘要。

实际英文模板为：

> The following is a summary of earlier history.

```text
SYSTEM：现有 ROOT 或 Subagent system，包括上述共同短说明
现有机械上下文：仍按当前 compiler 的来源规则提供
USER 摘要消息：解释1 + S
R：原 user / assistant / tool 消息序列及既有图片附件
N：本次新输入的原消息序列
```

`解释1+S+R+N` 表示摘要与对话记录的相对顺序，不要求调整机械上下文原有合法位置。R/N 不放入摘要 USER，不加解释2、不添加 R/N 分区标签，不用 JSON 引号或新说明包裹普通历史消息；原消息已有的工具结果、计划、定时来源等协议包装继续保留。

这里的无分区要求作用于压缩后业务输入；临时摘要请求仍需按第 5.3 节向摘要模型明确 H 的总结范围与 R 的参考范围。

短文案只描述来源和预期的少量重叠，不规定摘要或原文谁更可信，不引入内容优先等级，不暗示新的执行、权限或任务状态。少量重叠不作为需要量化检测的新阈值。内部 continuation 由 runtime 管理，不再额外向模型发送旧的长 HANDOFF／active quote 说明。

SYSTEM 修改在新开发合同的 cold root／正式 successor 中生效，不向已安装 epoch 动态追加或改写 SYSTEM。

## 2. 入口、配置与出口

| 入口 | 进入条件 | 摘要配置 | 成功出口 |
| --- | --- | --- | --- |
| idle 手动 | ROOT 空闲、操作被接纳且有旧历史可摘要 | 操作接纳时选择的目标及完整推理配置 | 只采用 idle base，下次业务才建立 epoch |
| 新 turn entry | 首次业务输入达到自动触发线，或遇到可恢复的本地硬边界 | 新请求捕获的配置 | 当前整个初始输入组原样保留，继续本次请求 |
| turn 内 | 工具后续等合法安全点达到触发线，或需要本地硬边界恢复 | 当前请求配置；合法额度交接按既有安全点处理 | 继续同一个 turn |
| 模型切换 | 完整 B 输入无需压缩且可执行则直接切换；否则进入统一压缩 | 本次请求选择的 B | 采用 B 后继后继续请求 |
| 业务 HTTP 413 | 满足第 8 节全部恢复条件 | 原失败业务请求的同一目标及 binding | 采用后继后重试该业务请求一次 |

普通自动压缩和 413 恢复遵守现有 `enabled`、`automatic_enabled` 及自动失败熔断；idle 手动遵守 `enabled`、`manual_enabled` 和 idle 排他接纳。模型切换保留既有启用与能力门控，不因普通自动／手动开关关闭而擅自改为旧模型执行。

模型切换直接路径仍按 B 的完整最终输入判断：低于自动触发线且通过硬准入才直接切换；达到触发线则压缩。初步预检不是最终派发许可，收集全部实际来源后必须再次检查。

不恢复 A 先摘要再交 B 的路径。摘要、合法修复、候选测量与实际执行沿所属请求捕获的完整模型／推理配置；不得读取界面最新选择重新选模，不以默认 reasoning 替代。

没有旧历史可摘要时不发送空摘要，也不把 N 改成摘要来绕过边界。空会话手动沿既有 NOT_NEEDED；必须恢复但没有可用历史时明确失败。

## 3. 触发阈值与实际准入

### 3.1 85% 只负责自动启动

自动压缩在 `budget_input_tokens >= floor(effective_input_budget_tokens × auto_trigger_ratio)` 时启动，默认 ratio 为 0.85。手动及有明确原因的硬边界恢复不受软触发线限制。

摘要输入和压缩后输入都不再要求低于 85%。删除 `minimum_reclaim_tokens`、`post_compaction_target_ratio` 及其校验、分支、测试夹具和文档规则，不新增替代最小收益或落点比例。`maximum_retained_tail_utf8_bytes` 不存在，不恢复 2 MiB 尾部选材限制。

保留最近完整工具批次的数量偏好、最近用户原文规则、输出完整性、模型预算、请求字节、内容能力、已有物理资源准入及准备／传输期限。不借本次统一新增总历史、总轮次、总调用、总任务时长限制。

按用户决定，不为原地重复压缩增加冷却、去重状态或额外调度机制。已有 successor 交接与正常业务循环继续使用。

### 3.2 摘要与后继分别按真实请求准入

摘要测量包含实际 SYSTEM、tools、完整 H+R 或 P(H)+R、指令和合法修复所加内容；后继测量包含解释1、S、从 canonical rows 展开的全部 R/N、派生图片附件、机械 runtime 上下文以及该阶段实际接纳的补充输入。不能只按 snapshot 中短引用的 JSON 长度计量，要计入被引用原文、原图及兼容 replay 的真实开销。

使用现有 compiler、SDK serializer、wire quote、能力检查与资源 owners；不能以内部 JSON 长度、字节／token 固定换算或普通工具结果的页预算代替完整模型请求测量。业务请求装不下，不代表去掉 N 的摘要请求也装不下。

## 4. H、R、N 与 canonical 重投影合同

### 4.1 内容真值与来源边界

canonical transcript rows 及其既有正文／图片／工具结果 owners 是原交互的唯一内容来源；snapshot 保存 S、选中原文的引用和必要 continuation，不保存另一份 R/N 正文或 provider messages。

当前有效历史由已有 S、snapshot 明确保留的原记录及截止点之后的正常 canonical suffix 构成。不重新展开全部 H，不把“引用原行”解释为恢复整段已经摘要的旧历史。

H 是新摘要承担的材料，R 是继续原样提供的已选历史材料；N 按本次真实初始输入组、已接纳补充及强制待消费材料的既有事实确定。摘要读取范围与 canonical replacement cut 分开；H/R/N 不要求物理连续。

当前活动请求或近期用户原文即使位于 replacement cut 之前，也用原 entry 引用精确选读。不能把 cut 退到最早活动请求之前，迫使整段 turn 历史无法摘要；也不能为了保原文把它复制进 S 的消息。

### 4.2 R 的共同选择规则

所有入口优先保留最近最多 3 个完整、已闭合的历史工具批次。一个批次包含同一 assistant request 的全部并行调用、结果／closure 及图片关联；不拆成单个工具选择。R 工具尾部继续使用真实连续 canonical suffix。

近期用户原文沿现有最多 3 条／64 KiB 规则选择；当前活动请求和强制新输入不受这些可选偏好限制。位于 cut 之前的选中用户原文通过同 session 的 entry 引用读回。此次不顺带删除其他现有局部边界。

同一个 entry 因多个保留理由被选中时仅出现一次；不同 entry 即使正文相同也分别保留，不按内容去重。R 内部按真实 source 顺序排列；与正常 suffix 合并时使用 entry 身份去重，绝不把新输入误归为旧历史。

“原样”指保留 canonical 正文、角色、相对顺序、tool call IDs、结果状态和图文来源。新 successor 仍通过目标协议正常序列化，不承诺整个旧 HTTP 请求字节相同，也不保存另一份 raw wire 档案。兼容的 accepted assistant native replay 沿现有 manifest 复用；不兼容的 opaque／签名／加密载荷不得改签、猜测或跨 target 强行重放。

目标无法读取的可选历史图片可以在摘要调用前降低原样保留优先级，移入 H 后按已允许的投影处理。最终 R 不得被静默改写；目标无法支持 N 或强制图片时明确失败。

### 4.3 Snapshot 的最小持久内容

沿用现有 context snapshot 与 binding，不新增表、引用实体或 provider message 存储。carrier body 固定表达以下内容：

| 内容 | 合同 |
| --- | --- |
| `earlier_context_summary` | S 的文本 |
| `retained_entry_ids` | 当前 session 内、sequence 不大于 snapshot cut 的已选保留原 entry ID 有序列表；不包含正常 suffix 中已有的项 |
| `continuation.mode` | 既有 idle 等待下一请求或恢复当前 turn 的机械语义 |
| `continuation.active_request_entry_id` | 恢复当前 turn 时指向其真实活动请求；idle 为 null |

session、workspace、source cut 和合同版本继续使用现有外层 snapshot／binding 字段，不在 body 复制一套。引用使用共享 ID 层已有 entry ID，不为引用项新建 ID。

活动请求的位置由其真实原 row 与 cut 的关系确定：若在 cut 之内必须包含在 `retained_entry_ids`，若在 cut 之后则直接位于 suffix。类型、sequence、正文、scheduled provenance 等从该原 row 冻结读取。删除以 `SNAPSHOT_EXACT` 复制正文的旧持久表示及其分支，不保留位置别名、旧 quoted body 或长 `handoff_instruction`。

prefix 引用的合法类型沿现有活动／历史请求的 kind、origin 联合约束，包括允许保留的用户请求、Plan continuation 和 terminal observation；不是任意 assistant 或单条工具结果引用。工具历史仍按 cut 之后的完整批次保留，不能用稀疏 entry 引用绕过调用／结果闭合规则。

引用是上下文选材事实，不是执行授权、任务队列或恢复 checkpoint。原文不存在、越界或来源不匹配时明确失败，不使用保存的旧正文、S、相同文本搜索或父会话作为 fallback。

### 4.4 Reader、dry builder 与普通 lowering

真实 reader 在同一个冻结读 cut 中取得：S-only snapshot、明确选中的 prefix 原 rows，以及 `sequence > cut` 的正常 suffix。引用行必须属于当前 session／workspace／scope、满足原文保留用途与冻结上界；正文、metadata、图片和工具关系统一走既有 row→typed item 路径。

当前 view 形态为：

```text
CONTEXT_SNAPSHOT(S 及内部选择／生命周期信息)
selected-prefix-originalItems
original-suffixItems
```

S 始终在前；保留 rows 按真实顺序排列。snapshot 自身的 sequence 可以高于其后显式保留的旧 row，不能把整个 view 按 sequence 重排而挪动 S。原 row 仍使用原 entry ID／sequence／kind，不伪造新来源，不 INSERT 新转录行。

`build_synthetic_compaction_dispatch_read` 必须构造相同 view，不能继续只用 `sequence > new_cut` 而漏掉明确选中的 prefix R。dry 与采用后真实 reader 的 items、引用成员、closure、late outcomes、replay、图片及资源计量必须一致。

`read_frozen_compaction_cut` 的有效 Q、后续 H/R 重选及历史投影都要包含显式保留的 prefix rows。原有 floor 不变，但 `floor < sequence` 不能成为丢失这些有效材料的理由；引用成员与旧 lineage 沿现有 typed cut 合同校验，不新增覆盖注册表或伪造更早 floor。

compiler 对 snapshot 只生成一条“解释1+S”的 USER 消息；R/N 的真实 items 继续普通 lowering，保留真实 entry placements。工具结果中的图片仍按完整 assistant 批次派生 USER 图片附件，保留原 tool-call 关联；这些附件不是新 human entry。不增加 snapshot 多叶展开、特殊子消息身份或另一套 replay 机制。

显式保留 rows 的正文和图片同样计入既有 headroom、展开内存、token 和 wire 准入；引用很短不能降低真实开销。canonical immutable 图片由现有 blob／prompt refs 读取，不在重发时重新读取视频／文件来替换内容。

### 4.5 Entry、mid-turn 与强制材料

entry 使用已启动 turn 的真实 predecessor ceiling；整个初始输入组都留在替换截止之后，不只保护 `initial_entry_id`。直到首次业务调用成功接受前保持该上界，重规划和 413 不清空。N 沿自身原 rows 到达一次，不重新接纳、不写入另一份初始输入。

mid-turn 保持同一个活动请求。位于旧前缀内的活动原文按 `active_request_entry_id` 从本 session 原 rows 精确读回；新工具结果和已接纳补充按真实记录投影。已执行工具不重执行；只有后续新接受的 assistant 输出才能进入工具 dispatcher。

尚未被业务模型消费的强制图文证据整批原样保留，包括索引、图片、全部结果和必要调用关系，不受可选 R 数量限制，不混入可选历史 R 名额。沿已有 assistant 输入 cut 和 pending image 判断确定是否消费，不能用工具已执行替代模型已阅读。

未发布 steer、Plan answer 等继续沿既有候选与共同输入 safe point 接纳，不提前混入历史摘要，不新增收件箱。pending 工具批次仍先对上一闭合 cut 准备摘要／页预算，等待真实批次完成后才 finalize/adopt，不提前采用或重复执行工具。

### 4.6 连续压缩中的保留引用

此前 snapshot 的 S 及其保留 rows 都属于下一次有效历史。完整读取 prior base 意味着实际读取其 S 和所引用的原记录，不是只读 entry IDs。凡本次决定继续原样保留的旧项都重用本地原 rows，引用并入同一个 `retained_entry_ids`，不复制正文。

只有旧引用对应材料被分入本次 H 并实际进入摘要来源，才能按新选择释放该引用；单纯作为 R 参考读过不表示已由 S 承担。投影省略 prior handoff 时，未交接的既有保留引用继续带入，不得仅凭新摘要已生成就清空。

因 prior-base 省略而继续保留的引用必须在本地来源规划时并入本次最终 R，其原文参与同一次 `P(H)+R` 摘要准入；不能在摘要返回或采用后临时补入。沿既有整块 prior-base 包含／省略判断，不为逐条语义覆盖新建登记表；必保引用不受可选近期数量偏好裁剪。

## 5. 统一来源规划与摘要调用

### 5.1 完整来源优先

先冻结合法 H/R/N 及实际目标，使用完整 H+R 加摘要指令测量。满足硬预算与能力就直接选完整来源，即使摘要请求本身位于 85% 至硬边界之间也不先投影。

只有本地明确的容量／历史能力不满足，才复用现有 destination dialogue projection：保留可容纳的历史对话 backbone，在剩余空间补入工具证据；仅对 H 投影，R 作为精确参考计入同一次实际测量。投影允许的省略沿既有 source notice 和 UI 提示说明。未知 input modalities 不等于已知 text-only，不能据此自动省略图片；能力判断继续使用既有已知事实。

投影来源搜索只做本地规划，不调用旧模型，不先让模型用临时投影正常执行业务。provider 的 401、429、超时、断流、413 或关闭失败不是重新投影并再发摘要的理由；遵守正常 transport 重试合同，摘要 413 结束本次摘要。软触发后的原业务能否继续，另按第 9 节判断；413 恢复中的摘要 413 则结束整个恢复。

完整 H+R 不 fit 时，仅把 R 移入 H 不会缩小完整读取集；不能把减少 R 当成完整来源的容量救援。对于 P(H)+R，移入 H 会扩大可投影范围，允许在发送前按既有可选保留偏好本地选择合法 R。

### 5.2 两种传输表示，共同来源合同

在现有摘要 builder／source proof 中表达同一冻结来源与独立的读取／替换边界：

| 表示 | 使用条件 | 前缀约束 |
| --- | --- | --- |
| 已安装前缀追加摘要指令 | 完整摘要读取集能够覆盖既有已安装消息，且真实 wire 可保持原前缀 | 原 SYSTEM/tools 和已安装消息逐字不变，只追加合法指令 |
| 独立辅助摘要输入 | 需排除已经安装的 N，或目标／投影来源不能作为原 epoch append 表示 | 明确的临时摘要 scope，保留真实 predecessor 与 canonical 来源；不安装为业务 epoch |

这是本规格对所有压缩准备调用的明确授权，不是只为 413 建特殊压缩器。独立模式不伪造 `predecessor=None`、EMPTY_COLD 或发生了模型切换；安装前缀长度只约束 append 表示的读取完整性，不再强迫 canonical replacement cut 越过 R。

普通来源可以继续利用原前缀缓存，不为统一而强制重写全部辅助输入。SYSTEM/tools 从现有冻结 owner 提供，不以最新 MCP、Skill 或设置静默重建原业务根。

### 5.3 摘要与保留调整

摘要指令必须让 provider 实际看见 H 的总结边界与 R 的参考范围，不能只在内部 proof／message placements 中记录切点。append 表示在末尾追加的短指令中标明真实消息范围／工具批次边界，不向已安装前缀中插入标签或重排消息；独立表示可用现有内容 renderer 输出明确的 H 与 R 参考区段。边界由冻结 placements 推导，不新分配材料 ID，不让模型自行猜测最近几条是什么。

摘要根据 R 的最新更正理解 H，不复活已取消或被替代的旧任务；R 原文由 runtime 接回，不要求摘要复制 R。模型不继续任务、不执行工具，不以摘要操作推断任务已排队／暂停。

摘要中的中断、停止等生命周期事实沿本次冻结来源读取。完整来源按实际选中的 H/R 消息确定；投影来源可保留 H 的真实生命周期信息而省略其正文，并保留原样 R 对应的事实。不能仅按 replacement cut 漏掉 R 的状态，也不能混入 N 的生命周期事实。

正常流程固定 R 后生成一份逻辑摘要，沿现有完整 terminal、输出校验、无工具执行和一次 tool-call repair 合同。repair 复用同一来源和配置，实际测量其追加内容；二次违规失败。

仅当真实后继因可减少的原样历史材料无法通过硬准入时，沿已有较小可选保留候选救援。H 发生扩大就必须重新摘要，不能删掉未被旧摘要承担的 R 后复用旧 S。仅当减少的是已由原冻结 H 承担的重复保留引用、H 本身未扩大时，才能沿既有来源事实复用摘要；不能从摘要文案猜测覆盖，不新增覆盖登记框架。

不为追求 55%、85% 落点、16k 收益或无改善结果反复生成摘要。取消、来源／目标失效、provider 失败与关闭不确定不进入减保留分支。重新捕获真实来源的 drift 仍沿既有重规划，不伪装成保留预算失败。

## 6. 采用前收益与采用后准入

所有候选都必须先通过现有目标、canonical cut、来源归属、权限、工具完整性与硬资源准入；在此基础上，采用前的改善判据如下。

| 场景 | PRE_FULL 判据 |
| --- | --- |
| 同一冻结目标下普通自动或手动压缩 | 两端同 estimator／wire 合同，完整原输入的 raw token 估算严格大于完整后继估算；不设最小差值，不要求字节也下降 |
| 跨目标或合法额度交接 | B 后继真实可执行，不比较 A/B token 差，不要求低于 B 的 85% |
| 明确本地 wire 硬边界恢复 | 后继通过真实硬准入，按同目标 wire 字节改善；不能伪造 HTTP 413 |
| HTTP 413 恢复 | 后继真实 wire 字节严格小于原失败业务请求，同时满足模型硬预算；token 可以不减少 |

普通 token 收益使用 `raw_final_wire_estimated_input_tokens`：同一冻结 estimator 对完整 wire（包括 replay 替换）的估算。不能用带 provider usage anchor 的旧 `budget_input_tokens` 减去没有 anchor 的 cold successor 估算，不能把 provider 多次请求累计 usage 当作上下文大小。软触发和硬预算准入仍使用原有 `budget_input_tokens` 及其合法来源，不删除校准机制。

需要硬恢复、但旧输入因真实物理边界无法产生可比较完整 quote 时，不伪造收益基线；保留该明确失败原因，只有真实可执行后继通过对应硬准入才能继续。不能把此例外用于缺少证据的软触发压缩。

无改善的普通摘要不采用、不报告压缩成功。软自动操作若仍可安全执行原请求，按第 9 节继续；手动如实报告未缩减。手动有可压缩历史必须真实调用摘要，不等于无条件采用任何输出。

POST_FULL 重新验证真实 adopted cut、目标、内容与硬准入，不再次对新增 runtime 观察或新 steer 套用旧普通 token 收益判据。业务 final safe point 接纳补充后再次测量完整输入；413 的最终字节条件是必须继续检查的例外。

## 7. 采用、生命周期与输入连续性

复用现有 Host compaction lane、attempt、write fence、canonical adoption 确认、snapshot carrier、revision 与 successor owner。idle 与 active 共用内容规划及采用前校验，分别以采用 idle base、交出唯一 active continuation 结束；不把 idle 伪装成业务 turn。

FULL 前失败不安装摘要或临时投影。确认未知时只确认原候选，不因不知道结果就重新生成摘要／重放采用。FULL 后摘要已成为有效历史；同一次操作不得回滚旧 epoch，或改走另一层摘要覆盖 winner。continuation 失败／停止沿既有退役结算，未来独立操作仍可合法压缩。

采用后的 PostCompact／SessionStart 与 optional Hook no-Hook fallback 保留现有职责；它们不是模型切换层级。共同安全点接纳 steer／Agent 消息时保留原顺序、权限与接受事实，实际发送前重测，不能使用已过期的 dry quote。

取消或关闭时 drain 已受理的采用任务，回收可能已生成的 successor；不能仅取消 await 后遗失唯一 owner。prepare 的期限、transport 连接／读空闲 watchdog 沿现有 owners，不新增持续有进展的摘要流总时长限制。

业务 epoch 内 SYSTEM/tools byte-identical、messages 仅追加。独立辅助摘要不授予业务 rebase 权限；业务根重建仍仅发生于合法 cold epoch 或 explicitly adopted compaction successor。

### 7.1 Fork：只复制选中 R 与有效 suffix

fork 仍由现有事务创建自包含历史，不引入父会话原文的运行时依赖。复制集合是源 snapshot 明确引用的 prefix R 原 rows 与原有效 suffix 的并集，按 entry 身份去重、按源 sequence 排序；不复制整段 H。

全部选中 rows 统一经过现有 imported-history 复制、entry_map、group_map、sequence_map、图片 refs 和 annotation remap。child 的 `retained_entry_ids` 必须重映射到 child 本地 entry IDs；不能保留指向 parent 的引用。历史请求属于 settled imported history，不因采用原角色消息就变成活动请求或执行权限；child 的 idle genesis 没有 active request。

沿用 `local_cut = bisect_right(original_sequences, source_cut)` 计算 child floor。复制的 prefix R 位于 child floor 内，由显式引用读回；原 suffix 位于 floor 后，按正常规则读取，避免重复。latest sequence、anchor、assistant input cuts、closure cuts 与 snapshot cut 都使用同一映射，保持现有 dense imported prefix 和 FK／owner 约束，不为只保留一条 USER 而复制整个旧 turn。

父会话删除后，child 仍可从自身 canonical rows、blob／prompt refs 读取全部 R。源本身也是 fork 时仍按同一规则复制其本地选中 rows；不增加跨 session lease、保活图或 typed 正文 fallback。普通同 session 压缩／重发不复制 rows，新增 child rows 仅发生在原有 fork 创建边界。

## 8. HTTP 413：同一管线的一次业务恢复

### 8.1 资格与关闭

只在 ROOT／SUBAGENT 业务模型请求满足以下全部条件时恢复：真实 SDK／HTTP 状态为 413；没有业务语义输出、没有接受 assistant 结果或执行该请求产生的工具；当前 turn 仍运行且没有停止／关闭意图；自动压缩 owner 和策略允许；本失败业务调用尚未使用本次恢复资格；物理执行与相关 borrows 已确认完整结算。

复用 `retry_summary.final_status_code`、`has_semantic_output`、既有关闭原因及 `PreparedKernelModelExecution.state`。不解析正文中的“413”，不将 400、token 超限、429、超时或断连推测为 413。摘要、连接测试、ASR 等辅助调用不适用。

修正 `PHYSICALLY_CLOSED` 的发布时间：只有流物理退出和 borrowed 资源释放均成功后才发布。借用释放失败不能保留成功 state、仅挂异常 cause/note。SDK client 的 `physical_close_unconfirmed` 等既有关闭失败事实必须排除恢复，不另建矛盾的结算布尔权威。

runner 在 `_collect_model` 原收尾之后、通用 `interrupt_turn` 之前处理资格：先 `finish_model_operation()` 释放旧 safe-point handle，再关闭 surface borrow，全部成功后才调用统一压缩。transport 仍不对 413 原样重试。

### 8.2 来源、资格和重试

413 使用与其他入口相同的 H+R 读取、canonical R/N 保留及完整／投影来源规则，不改为只读 H，不固定清空 R。摘要本身只需通过已知硬准入，不要求比原失败业务请求小；repair 同样如此。

失败请求已经安装了 R/N，不代表它们已经获得本次失败请求的 assistant 回应。恢复沿同一 session 的原 rows 重新投影：S-only snapshot 后接原角色 R/N 消息，保留原 tool call IDs、结果和图片。先正式采用 successor，再发送这份新 view；不能在原 epoch 直接删 H／插 S。首次调用失败继续保护整个 initial group；turn 内失败继续保护待消费工具／图文材料。历史 assistant tool calls 只作为上下文，不再次执行。

如果 H+R 摘要也被未知服务端上限拒绝，明确失败，不再减少来源、分块总结或探测阈值。即使 H 单独可发送，也不切换成未经本合同批准的 H-only 摘要路径。

runner 在现有调用作用域保存原失败请求的完整 wire quote 与恢复已使用事实，进入恢复前即占用资格；不新增 durable 记录、实体或 ID。该事实穿过摘要、自然候选调整、采用、safe point 和业务重开，不能因 loop continue、model_call_index 调整、重报价或 model target 表示变化而重置。

正式业务 install/open 前要求实际重试请求 `final_wire_utf8_bytes < 原失败请求.final_wire_utf8_bytes`，并通过全部硬准入。期间新增 steer 若使条件不成立，结束，不丢补充、不发原大小请求、不再压缩。

业务重试成功接受 assistant 结果后回到正常 agent loop，后续独立业务调用重新具有资格。重试仍 413 则明确结束；重试其他错误按正常错误策略处理，不能因此重新发放本次 413 恢复资格。不以任何形式重执行已经完成的工具副作用。

## 9. 失败与用户反馈

| 结果 | 处理 |
| --- | --- |
| 软触发自动摘要未采用，原业务仍可执行 | 重新确认原请求硬准入、连续性及活动状态后继续原任务，如实报告摘要未采用 |
| 同目标手动摘要无改善 | 不采用，返回现有 NOT_NEEDED／未缩减结果，不假称成功 |
| 手动摘要、模型切换或硬恢复无法完成 | 明确失败，不偷换 A、默认 reasoning 或临时投影业务输入 |
| 摘要 413 | 结束该摘要，不再减 R 或换来源重发；同目标软触发可按首行判断能否继续原业务，413 恢复则明确失败 |
| 取消、Host 关闭、writer 失效、关闭未确认 | 结束当前操作，不减 R、不再选来源，也不沿软失败分支继续业务 |
| FULL 后 continuation／最终准入失败 | 保留已采用事实并清理 continuation，不回滚 |

软失败继续原业务只适用于同一原业务目标且尚未采用，无语义／副作用不确定、无停止或关闭意图、来源及前缀仍有效。不能将此分支用于失败的模型切换或恢复一个已终止 turn。沿现有自动失败熔断计数，不新增跨任务总失败上限。

沿现有进度和失败 UI，不新增确认弹窗、降级记录表或事件种类。投影省略通过既有来源声明及简短通知表达。413 可使用：

- 恢复中：“请求体过大，正在压缩上下文后重试。”
- 重试仍 413：“服务端拒绝了过大的请求体；压缩后重试仍失败。请缩小本次输入或媒体读取范围后再试。”
- 摘要或采用失败：“服务端拒绝了过大的请求体，压缩恢复未完成。”附实际公开原因。

不将 413 说成 token 用尽，不猜测某张图片是原因。沿现有诊断保留真实状态、错误正文、实际请求和响应，只清除真实凭据值。

## 10. 实施落点与 hard cut

| 现有 owner／文件 | 修改职责 |
| --- | --- |
| `compaction/contracts.py`、`model_input/contracts.py`、`planner.py` | 删除 16k／55% 策略；carrier 改为 S+保留 entry 引用+最小 continuation；统一 H/R/N 与读取／替换边界，dry builder 包含显式 prefix R 和原 suffix |
| `compaction/model_call.py`、`prompt.py` | 共用摘要来源合同；append／独立表示；H 总结与 R 参考；新 carrier codec；合法修复、物理结算及输出校验 |
| `reader.py`、`model_input/lowering.py`、`compiler.py` | 同 cut 精确读取 prefix 引用和 suffix；S 只含解释1+摘要，R/N 用原 items 普通投影；计量、scope、来源和兼容 replay 一致 |
| `ports/system_prompt.py` | ROOT／Subagent 两处加入同一简短中立文案，不修改已安装 epoch 的 SYSTEM |
| `fork_history.py`、`_repository/fork.py` | 复制选中 prefix R 与有效 suffix，沿原事务重映射本地引用、cut、图片及 imported-history 归属，保持 child 自包含 |
| `compaction/coordinator.py` | 替代 ordinary／destination 两套不同保留与软落点选择；共用 full-first／projection 规划、PRE_FULL 改善判据和 POST_FULL 结构／硬准入；继续使用原采用 owner |
| `provider_dispatch.py`、continuity／safe point | frozen target、真实 source/suffix、dry/final 测量、共同补充输入与唯一 continuation；不扩大 rebase 权限 |
| `direct_model.py` | 物理关闭 state 与全部资源结算一致；保留既有结构化失败事实 |
| `runner.py` | 统一入口及成功出口；闭合旧操作后协调 413，保留原失败 quote／一次资格，最终 safe point 后检查业务重试 |
| 两个 OpenAI adapters、`retrying.py`、`normalized_transport.py` | 沿现有通道传递真实状态、语义输出及关闭失败，保持 413 不原样 transport retry |
| tests／dogfood／当前规格 | 迁移至新单一合同，删除只冻结旧阈值／旧 source-summary 层的用例，保留实际行为与失败边界断言 |

不复制第二套摘要／投影／采用框架；提取现有 owners 中合适的共用机制。删除未消费的 source-target 候选字段、numeric tier、已失效 `enforce_soft_target` 等结构；`force` 如仍用于启动判定，只表达是否进入摘要，不作为收益或资源准入后门。

不新增数据库表、durable event、subject、guard、relation、job 或恢复实体。新过程内值优先携带已有 owner 和完整 typed value；确有新的可操作对象身份时必须使用共享 ID 层，不自建 UUID／counter／hash ID。

按当前开发合同一次 hard cut：旧会话可丢弃。删除 snapshot 内 `recent_human_requests`／`retained_historical_requests` 正文副本、active 正文复制、旧 retained quote 分区及长 handoff 展示；采用单一 refs codec 和 reader，不保留 typed-content fallback、snapshot 多叶替代方案、H-only 413、旧最小收益／落点、旧 A 摘要或兼容开关。更新实际变化的 carrier／lowering／compiler 合同及 clean-v0 断言；使用现有表，不为 JSON 载体重构创建线上迁移链。

删除的长 handoff 文案仅指 snapshot 的旧 provider 展示；现有 `COMPACTION_RUNTIME_HANDOFF` 中机械维护的 TODO、资源状态与生命周期职责继续保留。

## 11. 必须通过的验收

1. **四入口共用内容规则**：idle、entry、mid-turn、切换 B 都验证完整 H+R、只替换 H、R 原样接回；实际发送的摘要指令能识别 H/R 边界，不仅内部 proof 正确。idle 不开业务 turn，active 不换 turn。手动低于 85% 仍摘要，无旧历史不调用。
2. **真实计量**：小于 16k 的正 token 收益可采用；后继在 55%～硬预算之间可采用；摘要及后继在 85%～硬预算之间不被软落点拒绝。wire 小幅增加但未越硬边界的普通 token 压缩可成功。
3. **校准口径**：source 使用 reported usage anchor、successor 是 cold heuristic 时，按两端相同 estimator 的 raw wire tokens 判断收益；不把校准差误算回收。跨目标及额度变化不比较 A/B token 差。
4. **来源选择**：完整来源可执行时不提前投影；全场景只在明确本地硬／历史能力需要时投影，选定 R 作为精确参考。省略 prior handoff／旧工具正文有真实声明及通知，canonical 不删，未交接的既有 retained entry 引用不误清空。
5. **保留与图片完整性**：并行工具批次、所有结果／closure／图像附件不拆分；N 不占可选 R；强制图文不受可选数量限制。text-only B 对强制新图片明确失败，历史可选图片只按已允许的投影处理。
6. **entry 与长 turn**：完整 initial group 始终受真实 ceiling 保护，重复准备不重置；旧前缀内当前用户请求按原 entry 引用读回，不复制正文、不退 cut；后继没有漏失或重复接纳请求。
7. **预算变化与保留调整**：固定完整 H+R 时减少 R 不被当作缩小摘要输入；真实后继硬不fit而扩大 H 时重新摘要，不复用未覆盖原 R 的摘要。无改善不追求旧落点反复摘要。
8. **pending 工具页与压缩**：工具未完成时只准备闭合旧 source；批次完成后才 finalize/adopt。强制触发压缩后模型仍能对应图片、索引与时间并续读 next，没有漏页、重复或工具重执行。
9. **采用与新补充**：PRE_FULL 通过后注入合法 steer，正式输入必须包含补充并重测；普通不因新内容增加而误判旧收益，413 仍执行最终字节条件。FULL 后失败不回滚、不另起摘要覆盖 winner。
10. **真实 413 链路**：本地真实 HTTP 端点返回 JSON／HTML 413，经过官方 SDK、两种 adapter、规范化 transport 与生产 ROOT／SUBAGENT runner 各自进入一次恢复。正文含“413”的 400、超时、断连不触发。
11. **按字节稳定拒绝的端点**：端点依据实际收到的请求字节拒绝，不能按第几次请求放行。构造失败业务 H+R+N 超限、H+R 摘要可通过、S+R+N 重试可通过的实例；验证原模型／binding 与最终严格字节缩减。
12. **摘要也超限**：H+R 摘要自身超限时失败；H 单独能发也不得偷偷切 H-only。摘要可以比原失败请求稍大但本地 hardfit，不受额外 summary<failed gate 拒绝；若端点拒绝则如实结束。
13. **413 资格和最终输入**：重试仍 413 不再恢复；资格穿过 source 选择、采用、steer、loop 与重新报价。覆盖恢复后业务重试先收到 503、SDK 按原策略重试后最终 413 的情况，中间错误／generation retry 不重置资格，只有 assistant 结果事务确认接受后才结束该业务恢复作用域。大 steer 使最终请求不再小于原失败请求时不发送、不丢 steer、不重新摘要；之后独立成功业务调用有独立资格。
14. **完整关闭**：413 加 SDK client 关闭失败、borrow 释放失败、取消中关闭均不进入摘要；state 只在全部承诺完成后发布。旧 dispatch handle／surface borrow 在恢复前释放，turn 尚未被通用异常出口终止。
15. **失败矩阵**：输出不完整、重复 tool-call repair、Hook 拒绝、来源／目标 drift、确认未知、关闭／权限变化、采用后停止各走真实 owner；无晚到业务重试、遗失 continuation、原工具副作用重做或新持久恢复状态。
16. **连续性与回归**：实际请求验证同 epoch SYSTEM/tools 不变、messages append-only；只有 cold／正式 adopted successor 改根。保留工具分页、模型切换、memory/TODO/runtime handoff、Plan、provider retry、取消与资源退役回归，不新增 skip/xfail 或放宽行为断言。

17. **实际消息形态与文案**：ROOT／Subagent system 均有共同短说明；mid-turn、entry、413 实际捕获到解释1+S、独立原角色 R/N 及原图片附件，没有解释2、R/N 分区或摘要 entry 内的原文副本。只验证文案中立及材料保真，不新增重叠率阈值。
18. **Prefix 引用与连续压缩**：活动请求／近期 USER 位于 replacement cut 之前时，reader 与 dry／FULL 读取均返回真实原 items；缺引用、越界、跨 session／scope 和类型不符准确失败。相同内容的不同 entry 不误合并；同 entry 多重选中不重复；再次压缩仍把保留项纳入有效 Q。
19. **计量与同会话副作用**：snapshot 只有短引用时仍计入原正文、图像及 native replay 开销；普通重投影不增加原 transcript rows，不重接纳请求、不重执行工具。兼容 replay 使用真实 assistant 来源，不给 snapshot 或 USER 原文伪造 replay。
20. **Fork 自包含**：只复制明确 prefix R 与有效 suffix，重映射原角色 items、引用、cut、scheduled／Plan provenance、图片和 annotation 来源；删除 parent 后 child 仍能读取 R，floor 内引用不与 suffix 重复。连续 fork／再次压缩保持相同规则，不恢复整个 H、不依赖旧 typed 正文或父会话。

## 12. 激活与证据

先补齐 refs carrier、reader／synthetic／实际采用 view、fork 自包含复制、读取／替换边界、收益口径、关闭状态与本地真实 HTTP 故障夹具，再完成全部入口 hard cut；不得以只改数值／放宽 validator 或只把 quoted 原文拆成几条 USER 宣称统一完成。

按仓库 `.venv` 执行聚焦测试、上述 PostgreSQL 集成矩阵、Chat Completions／Responses 的本地 HTTP SDK 集成、相关完整回归与实际 provider 验证。真实 provider 使用有效 Pulsara home 的 `LocalSettingsStore` 配置，只读加载、精确清除真实凭据；隔离数据库须验证为本地可丢弃目标，不重置未知或远程库。

真实 provider 至少覆盖一次普通活动压缩及一次已配置可用的目标切换；捕获摘要输入、实际摘要输出、采用后业务请求、usage、前缀及目标配置，证明模型能继续原任务。413 的确定性恢复由按实际字节拒绝的本地端点验收，不依赖付费 provider 偶然返回 413。

缺少数据库／模型设置时明确记录缺项，不编造凭据、不把跳过当通过。旧 2 MiB 删除的单元结果、静态审查或本文示例均不能代替本规格实施后的验收。全部必要行为证据齐备后才标记本规格实施完成。

### 12.1 已完成的验收

- 后端完整回归：6105 项通过，20 项既有 WSL/Linux 专属测试在 macOS 按平台条件跳过；唯一失败为可视化动画测试按名义等待时间计时。改为同一文档实际时钟比较，保留暂停与恢复断言后，单文件和并行复验通过。
- 真实保存的 DeepSeek 配置：idle、entry、turn 内、Chat→Responses 切换四场景全部通过，共 10 次请求，其中 4 次摘要调用；检查实际输入、输出、usage、目标及 reasoning、原始记录与工具副作用。
- 本地真实 HTTP 与官方 SDK：21 项覆盖两 API、ROOT／SUBAGENT、按请求字节拒绝的 413 及非 413 反例；另验收 FULL 后大 steer、关闭失败、未消费 text／image 补充等边界。
- 最新 hard-cut 定向回归 100 项通过；Ruff、编译与差异空白检查通过。测试与实验使用隔离 PostgreSQL，保存配置只读；不新增兼容路径或持久恢复实体。
