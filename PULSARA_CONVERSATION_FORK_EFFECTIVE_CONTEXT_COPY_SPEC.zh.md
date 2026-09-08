# Pulsara Conversation Fork：有效上下文复制契约与 Hard-cut 设计

> 日期：2026-09-06。代码核对基线：`3ecb555d`。
>
> 状态：**ACTIVATED**（2026-09-08；实现与验收结果登记于第 13 节）。
> 本文固定产品契约、canonical hard cut、接口结果与验收要求。
> 同日二次代码审阅及独立 critic 已追踪 terminal 提交、idle compaction、历史 reader、
> carrier、工具 closure/result、artifact、会话创建与 terminal v3/前端投影。第 12 节记录
> 代码证据；第 7 节给出唯一采用的存储表示，不再把关键 schema 选择留给实施阶段。
>
> 2026-09-08 实施前复审增补：固定 Fork 的单事务 `REPEATABLE READ` 边界、
> `FrozenForkHistoricalMaterial` 纯历史读取 DTO、imported source attribution 的精确列联合、
> `retained_historical_requests` carrier 类型与 lowering、全 consumer owner-routing 矩阵，以及
> artifact handle 的 session-scoped 审计要求。以下增补是本文同一 hard-cut 的组成部分，
> 不形成第二种 Fork 路径或兼容层。
>
> 本文取代并删除原《Pulsara 会话 Fork 结构共享轻量调研》。
> 不再采用父会话历史引用、祖先链或跨会话 lineage reader 方案。

## 1. 核心决定

Fork 是从一条已结算 root turn 的最终自然语言助手消息，创建一个可独立继续的会话。
子会话复制分叉点当时的有效历史上下文，不复制整个父会话生命周期。

有已采用的压缩基底时，复制：

```text
分叉点当时最后已采用的摘要
  + 该基底保留的原始上下文窗口
  + 此后截至分叉点的已提交消息
```

没有压缩基底时，复制初始历史至分叉点的有效消息。

这里“复制”的是独立持有的历史数据，不是执行、事件重放或工具重跑。
子会话不需要父会话存在才能继续、分页、压缩或恢复。

这是一项有意限定的产品能力：保留有效上下文，不承诺在子会话中浏览压缩前的全部原文。
它也不是重新调用模型生成一份摘要；已有历史摘要忠实保留，不在 Fork 时另做总结。
这不等于原样复制整个 runtime carrier：其中的 continuation 控制与 ID 坐标必须按
已结算历史语义处理，不能把旧 `RESUME_ACTIVE_TURN` 当作子会话的新任务。详见第 12.3 节。

复制过来的消息保留历史内容，不自动继承每一条旧 final 的 Fork 能力。首版只保证本次
选中的 imported anchor 可以再次 Fork；子会话自己执行产生的合法 final 按普通规则可 Fork。
这项限制避免为了旧入口复制已经被替代的摘要链，详见第 2.3 节。

## 2. 永久有效的分叉点契约

### 2.1 唯一入口

只有同时满足以下条件的消息可以 Fork：

- 属于 root conversation，不是子任务内部消息；
- 所属 turn 已经结算，而非 RUNNING；
- 是该 turn 的 canonical 最终助手消息，而不是中间 commentary；
- 包含非空的最终自然语言回复，不是仅思考、仅工具或前端状态提示；
- 在当前 session 内有可独立解析的精确 Fork 基底；对 imported history 按第 2.3 节收窄。

root、已结算、canonical final、自然语言及禁止 mid-turn Fork 是长期产品契约，不是首版限制；
imported history 能保留哪些入口则由第 2.3 节明确收窄。
COMPLETED 或 INTERRUPTED 的状态本身不够；仍须有符合上述条件的最终消息。
没有最终自然语言助手消息的已结算 turn，不额外合成一个 Fork 节点。

“非空最终自然语言”的服务端唯一判据是：该 `ASSISTANT_MESSAGE` 至少有一个 `TEXT`
block，其 `content_codec` 为 UTF-8、内容严格解码成功，且后端 `decoded_text.strip() != ""`。
assistant parent 的 storage manifest、`DATA` block、reasoning、tool call、前端 fallback 拼接正文
均不计入。前端只消费派生资格，不重复实现这一判据。

父会话当前可以正在执行后续 turn。此时仍能从此前符合条件的消息分叉，
不得中断父会话，也不得将当前运行前沿当成用户选中的历史边界。

### 2.2 服务端确定历史 cut

客户端提交源会话 ID、所选最终消息的 canonical ID 和预先确定的 child session ID；
后者只用于创建结果识别，服务端仍仅从前两者对应的 canonical truth 解析：

- 消息所属 root turn、结算状态和最终消息关系；
- 截至该 turn 最终消息的准确历史上界；
- 该边界当时已经采用的上下文基底及保留内容。

前端不提交可信摘要正文，不自行推导 cut，不用消息数组下标、显示时间、turn 数量或
“最近一次压缩”代替精确关联。可见按钮只是入口，不能代替服务端验证。

最终回复必须包含在复制范围内。生产记录中的 `provider_input_through_sequence`
表示生成助手消息之前的输入 cut，不能直接作为 Fork 的完整上界，否则会漏掉所选回复。

若所选 entry 属于真实 executed turn，以它自身的 `context_binding_revision_id` 冻结生成该
最终回复的基底；若它是当前 session 的 imported anchor，则以该 session 的
`session_context_genesis` 为基底。其他 imported entry 在资格校验阶段已经拒绝。两条分支都以
所选 entry 的 `entry_sequence` 作为复制上界。

executed 分支不要读取 turn 现在的 `current_context_binding_revision_id` 来代替历史绑定：
当前代码允许在 turn 已结束后执行 idle compaction，并继续更新这个指针。后来手动压缩即使
没有新消息，也不改变“从这条历史回复分叉”的边界；用户选择的是消息，不是点击时的 session 状态。

生成回复前的 provider cut 与 entry 上界之间若有其他 canonical rows，也必须按现有
可见性/closure 规则读取，不用“原请求数组加一句最终回复”替代 canonical 历史解析。

### 2.3 imported final 的再次 Fork 资格

首版采用 anchor-only 规则：

- 子会话本地真实执行产生的 root terminal final，继续由 `turns.final_entry_id`、turn 结算状态、
  entry 自身 binding 与非空 `TEXT` block 派生资格；
- 创建该子会话时选中的 final，在 child 中成为唯一的 imported anchor；它必须由
  `session_context_genesis.anchor_entry_id` 和 child-local 基底完整支持，因此可再次 Fork；
- 其他随有效窗口复制进来的 imported final 一律不可 Fork，UI 不显示入口，伪造 API 请求也拒绝。

这里的“唯一 imported anchor”同时固定 child 的新老边界。Fork 创建事务提交时，child 尚无
真实 `turns`；实际复制的 `transcript_entries` 全部属于 `IMPORTED_HISTORY`，并构成一个封闭前缀，
其中 `session_context_genesis.anchor_entry_id` 必须同时满足：

- 是 ROOT scope、已结算 imported group 的 canonical final；
- 是本次复制上界，对应 child 中最大的 imported `entry_sequence`；
- 是整个 session 唯一可由 genesis 支持的 imported Fork 入口。

事务提交后该 imported 前缀永久封闭。普通 submit、steer、Plan、terminal、subagent、tool 或
compaction 写入口都不能追加 `IMPORTED_HISTORY`；第一条及其后的本地消息只能归
`EXECUTED_TURN`，sequence 必须大于 anchor。不得形成
`IMPORTED -> EXECUTED -> IMPORTED` 的交错历史。

以后无论 child 已追加多少本地 turn、采用了新摘要、切换了 session 当前模型，还是正在运行后续
turn，再点击 imported anchor 都固定执行同一条窄路径：以不可变
`session_context_genesis` 为基底，以 anchor 自身 `entry_sequence` 为上界。它不得读取 child
当前 binding/snapshot/model selection，不得包含 anchor 之后的 executed entries，也不因后续
状态变化而隐藏按钮。source 同时追加时，Fork 的一致视图仍只读取这个固定 cut。

因此 imported anchor 的按钮表示“从 child 的继承起点再建立一个平行分支”，不把其他 imported
history 升级为普通 executed final，也不允许由前端传入或推导另一条 imported cut。

这是明确的产品取舍，而非暂时漏做。反例是：A1 用 C1 生成，后来采用 C2 且 retained
window 仍含 A1，A2 用 C2 生成；从 A2 Fork 后 child 有 `C2 + A1 + A2`。若再从 A1 Fork，
C1 已不存在，而 C2 对 A1 是未来摘要。所谓“能重建就开放”的动态规则会重新引入多套旧 binding
和摘要链；首版不做。若未来要求所有 imported final 都可 Fork，必须先单独修订复制范围与存储契约。

资格始终是 canonical 关系的派生事实，不新增 durable `fork_eligible` 列、批准事件或 receipt。
terminal 协议可以投影布尔值，但服务端每次创建仍重新核验。

## 3. 如何选择有效上下文

### 3.1 历史 Fork 不读取未来摘要

设父会话现在已压缩到 Cn，但用户选中的是 Ci 与 Ci+1 之间的历史回复。
子会话使用 Ci，而不是 Cn，也不是后来才采用的 Ci+1。

只有已采用的 snapshot/binding 生效；仅生成、候选、失败或未采用的摘要不参与选择。
“第几次压缩”仅用于解释，不新增 durable compaction counter 作为真源。
executed anchor 依赖已有 canonical binding/snapshot 与消息边界；imported anchor 依赖其
session-local genesis 与消息边界。

### 3.2 一次 turn 横跨 mid-turn compaction

```text
旧基底 → 用户 U → 工具组 G1 → 工具组 G2
                                ↓
                           采用摘要 Ci
                                ↓
                         工具组 G3 → 最终回复 A ← Fork
```

假设 Ci 已覆盖 U、G1，但保留 G2。子会话复制：

```text
Ci + G2 + G3 + A
```

不是只复制“Ci 创建时间之后的 rows”：G2 可能更早产生，却仍属于有效窗口。
也不是复制整个原 turn：这会把摘要已经替代的 U、G1 再次塞入上下文。

准确范围由 adopted binding 的覆盖边界和现有 compiler 的有效上下文规则决定。
保留项只计入一次；不能把“保留窗口”和“摘要覆盖边界后的 suffix”重复拼接。

### 3.3 一个长程 turn 横跨任意多次压缩

```text
长程目标 U → C1 → …… → C2 → …… → C3 → …… → 最终回复 A ← Fork
```

在 A 分叉，只需 C3 的有效上下文：C3 摘要、其保留窗口及截至 A 的后续消息。
不复制 C1、C2，不补回 U 所在的整个长 turn，不新增 turn 长度或压缩次数限制。

长程目标通过摘要及现有保留上下文传递，不保证恢复摘要已省略的原文。
若这个长 turn 仍未结算，它尚无合法 Fork 入口，只能选此前已结算的最终回复。

### 3.4 不把 cold epoch 当作历史裁剪位置

cold epoch 是 provider-input root 的安装边界，不必然是摘要覆盖边界。
模型切换或其他已有显式冷启动路径，可能建立新的 root，而没有丢弃相同数量的历史。

Fork 选择的是“该点有效的上下文基底与历史材料”，不是“最后一个 cold epoch
开始之后插入的全部 rows”。已有模型切换压缩形成的合法基底也按同一规则处理。

## 4. 复制与隔离

| 内容 | 子会话行为 |
| --- | --- |
| 当时生效的摘要及其编译所需结构 | 保留全部历史材料，生成 child-local settled carrier；不只截取 UI 摘要，不照抄执行 continuation |
| 有效窗口中的用户消息、steer、助手正文、可见思考 | 保留顺序、内容和历史语义 |
| 已结算工具请求与结果 | 复制为历史事实，保留调用关联、原结果和无法从结果/cut 重建的 closure；不复制 attempt |
| provider-native replay carrier | 保存历史载体；实际发送时由现有 target 兼容路径选择，不按 Fork 时的配置提前丢弃 |
| 已投影到 root 历史的子任务通知、计划结果等 | 保留 entry kind、正文和冻结来源归因，不复制或连接其 live 运行对象 |
| 子会话的新消息、snapshot、binding 和本地身份 | 由子会话独立拥有 |
| active turn、queue、审批、plan workflow、子任务、终端进程 | 不继承、不恢复、不重跑；允许普通新会话原有 startup 行为 |
| writer、lease、reservation、工具执行授权及重试权限 | 不继承；需要时由普通新会话路径创建 |
| 原 agent events、session commands、通知消费状态 | 不复制为子会话执行记录 |
| 压缩前已被覆盖的原文和旧摘要链 | 不复制 |

“工具历史被复制”不等于“子会话执行过这些工具”。不得通过普通 submit/tool execution
路径回灌历史，以免触发 hooks、memory candidate、通知、计费或外部副作用。
不得为了满足 FK，复制原来的工具重试授权、计划工作流或全部子任务关系。

工具 closure 不是可丢弃的执行细节。当前 reader 在目标 cut 看不到 result 时，会借助
`attempt_id` 或 plan interaction 状态区分“派发前中断”“可能已部分执行”“计划交互中止”。
既然 child 不复制这些 live/execution rows，就必须为确实需要 closure 的 imported tool call
冻结该 closure kind；可见 result 与 late outcome 仍由 result entry 和映射后的 cut 推导，
不为它们另存证明性 receipt。详见第 7.4、12.6 节。

若协议所需的 user anchor、工具配对或 retained context 已由现有 compaction/compiler
表示，则复用该表示。不得为了补齐“完整 turn”拉回被覆盖的大段历史，也不得静默丢弃
无法表示的保留消息。遇到真实表示缺口，应补足最小历史表示，而不是伪造一次执行。

## 5. 新 cold epoch 与当前能力

子会话从新 cold epoch 开始。通过既有会话初始化、context compiler 和 capability
owner，采用当前配置、权限与实际连接状态下可用的 MCP、Skills、plugin 和工具目录。

不照搬父会话旧 `SYSTEM` 或旧 provider `tools` 数组；历史中的工具名、结果、
`mcpref`、终端句柄及任务 ID 只是背景，不产生当前可调用能力或活跃句柄。
工具消失时，历史调用与结果仍可被阅读，不要求旧 MCP 重连成功才能复制历史。

历史保留的 Skill 内容若属于摘要/上下文材料，不代表该 Skill 现在仍安装、启用或已获授权。
能力是否可用由当前 owner 决定，而不是根据历史自然语言推断。

共享 memory 也不是历史时光机。child 使用同一 memory domain 在新 cold epoch 下的当前合法
投影，因此可能观察到 Fork 点之后才写入或更新的 memory；Fork 的“无未来内容”只约束被复制的
conversation/snapshot 历史，不承诺回滚共享记忆。若将来要求 historical-memory fork，须另立产品边界。

因此父子在分叉点保持的是有效历史材料的语义一致，不保证完整请求字节相同、
工具数量相同、模型输出相同或 provider cache 命中。root 一旦安装，继续遵守 AGENTS.md：
SYSTEM/tools 固定，messages 仅追加；Fork 不新增任何 epoch 内隐式重建例外。

provider-native replay 使用现有通用 Chat/Responses 契约。canonical 新 ID 与历史 wire
call ID 不是一回事；不得全局替换 payload 内 ID，改写签名/加密块或破坏调用结果关联。
不引入逐 provider/model 的 Fork profile、额外兼容 registry 或请求试探。

## 6. 子会话默认行为

- 在同一 workspace 与 memory domain 创建独立 session；不创建 Git worktree，不复制目录。
- 快速会话分支也沿用源 workspace，不另建空目录假装拥有相同文件。
- 文件、项目能力配置和共享记忆仍属于其原有作用域；父子可能观察到彼此产生的变化。
- 不回滚文件、外部服务状态或记忆到历史 Fork 点。分叉的是对话，不是整个执行环境。
- 对 executed anchor，默认模型选择取所选 turn 的已保存 model-call binding，包括 reasoning；
  对 imported anchor，该值取 source session 的 genesis 冻结值，而不是后来可能已修改的 session 当前值。
  配置若已删除或不再可用，仍可保存分支并沿用现有“选择可用模型”提示，不偷偷换模型。
- 模型配置当前值与历史值可能不同；不复活旧 key，也不把历史凭据复制入会话。
- 后续显式选择其他模型复用现有模型选择、预算检查和压缩机制；不另造 Fork 降级算法。
- 权限、规划开关按普通新会话的现行产品规则初始化，不继承历史审批或已批准计划。
- 创建后显示已复制历史，保持 idle，等待用户的新消息；不自动发送“继续”或执行原目标。
- 当前 session 没有 durable title/rename owner，UI 标题由 session ID 临时生成；本轮不承诺
  “原标题 · 分支”或新增标题持久化。会话命名若进入产品范围，另行定义，不夹带进 Fork schema。

当前能力目录可能使新请求比父会话旧请求更大。第一轮发送仍经过既有 exact final-wire
预算检查与必要压缩；数据复制成功不等于可以跳过 provider 输入边界。

## 7. 最小存储与所有权设计

### 7.1 选择复制，不选择结构共享

子会话需要的摘要、历史内容及必要关联都归子会话读取，不经父 session 查询。
新 canonical ID 归子会话；local sequence 独立分配，不要求延续父会话的全局编号。
内容寻址的 immutable blob 可以复用既有存储，但生命周期不得依赖父会话保持存在。
当前 `PostgresCanonicalBlobStore.delete_orphans()` 已按 snapshot/entry/block/queue/result
的实际引用回收 blob，实施须使用这些既有保留入口，不把内容藏进 GC 不认识的临时容器。

允许在复制操作内部用临时 ID 映射连接消息、块、结果和基底；该映射随操作结束丢弃。
不保存通用 old-ID/new-ID registry，不以 fingerprint 代替完整复制值。

Fork-of-fork 执行完全相同的本地复制流程，不追溯祖先。
当前产品只有 `lifecycle=CLOSED`，没有物理删除会话的 owner。因此本轮承诺的是：父或子
关闭、进程重启及 blob GC 后，另一方仍独立可读可继续；不引入共享前缀删除 guard。
物理删除若进入产品范围，须另行定义。blob 回收仍由现有 owner 负责，不另建 Fork 引用计数体系。

### 7.2 采用的 canonical imported-history 联合

当前 schema 不能直接替换 `session_id`：`turns` 同时承载 initial/final entry、model binding、
权限和执行状态；retained window 又可能从原 turn 中途开始。首版明确采用以下单一路径：

1. **`turns` 只表示真实执行。** 不为 Fork 增加 `IMPORTED` turn，不把它的执行字段改成大面积
   nullable，也不拆一个影响全仓的 `turn_execution_state`。所有 submit、steer、compaction、
   plan、subagent、tool execution owner 继续只处理真实 `turns`。
2. **新增 `imported_history_groups` 作为只读历史分组。** 每组归一个 child session/workspace，
   只允许 ROOT scope，可保存源 turn 的已结算状态、可选 copied final、原 accepted/terminal 时间；
   不要求 initial entry，也没有 current binding、权限、workflow、task、writer 或调度状态。
3. **`transcript_entries` 使用严格 owner 联合。** `entry_owner_kind` 取
   `EXECUTED_TURN | IMPORTED_HISTORY`，且 `turn_id` 与 `imported_history_group_id` 恰一非空。
   前者继续 exact-FK 到 `turns`；后者 exact-FK 到 `imported_history_groups`。imported entry
   的 protocol `turn_id` 投影为本地 group ID，保持 UI 分组，但不能作为 execution target。
4. **assistant 生成元数据按 owner 闭合。** executed assistant 继续要求本地
   `context_binding_revision_id`；imported assistant 不伪造 child 执行 binding，该字段为空，
   但保留映射后的 `provider_input_through_sequence`、wire API、replay disposition 与本地 replay 关联。
5. **来源归因也使用严格联合。** 现有 `source_*` 对 executed entry 继续连接 live task/workflow/
   interaction/attempt；imported entry 只保存闭合枚举、冻结 source ID/transition 与 canonical 正文，
   不设这些 live FK，不据此查找或恢复运行对象。reader 和 terminal projection 必须先检查
   `entry_owner_kind`，再按相应分支校验 plan/subagent carrier；不能把旧 ID 当作 child authority。

`IMPORTED_HISTORY` 的写构造器只归原子 Fork owner 使用，并且只能在新 child 尚无真实 turn 时调用；
普通 entry/turn repository DTO 与 SQL 写入口 hard-code `EXECUTED_TURN` 分支，不能接收 imported
group ID。创建事务在提交前核验 imported anchor 是最大 imported sequence，且 session 中不存在
executed entry；提交后的任何 imported 插入请求一律冲突。该封闭写面与 owner 联合已经表达产品
边界，不为此新增 durable event、通用 append guard 或可在运行期重开 imported prefix 的命令。

这保留一套 `transcript_entries`、blocks、replay、result、分页和 sequence owner，同时从数据库层
隔绝 imported history 与执行。相比之下，把 imported 状态塞进 `turns` 会污染所有执行查询；
拆分整个 turn execution state 改动过宽；另建完整 imported transcript 表又会复制整套 reader。
因此三者均不采用。

上述 history group 与 entry owner 联合有独立产品必要性：前者解决中途 retained turn 的只读
分组和 final 身份，后者阻止历史获得执行权限。它们不是事件、receipt 或 replay job。
clean-v0、constraint/grant catalog 与测试必须在同一 hard cut 更新，不保留旧 schema 双读/双写。

#### 7.2.1 `transcript_entries` owner 联合的精确 DDL 形状

`transcript_entries` 必须新增：

```text
entry_owner_kind text NOT NULL
  CHECK (entry_owner_kind IN ('EXECUTED_TURN', 'IMPORTED_HISTORY'))

turn_id text NULL
imported_history_group_id text NULL
```

并以一个 closed XOR 约束固定 owner：

```text
entry_owner_kind = EXECUTED_TURN
  <=> turn_id IS NOT NULL
      AND imported_history_group_id IS NULL

entry_owner_kind = IMPORTED_HISTORY
  <=> turn_id IS NULL
      AND imported_history_group_id IS NOT NULL
      AND conversation_scope_kind = ROOT
      AND scope_subagent_task_id IS NULL
```

`turn_id` 的现有 FK 只约束非空的 executed 分支；`imported_history_group_id` exact-FK 到
`imported_history_groups(session_id,id)`。所有普通 entry writer 必须在 SQL 中显式写入
`entry_owner_kind='EXECUTED_TURN'`；只有原子 Fork owner 可以写 `IMPORTED_HISTORY`。

assistant metadata 不再用一个跨 owner 的大 CHECK 混在一起，而按以下 closed union 重写：

- executed assistant：继续要求本地 `context_binding_revision_id`、现有
  `provider_input_through_sequence`、wire API、replay disposition/fragment；
- imported assistant：`context_binding_revision_id IS NULL`，但必须保存映射后的
  `provider_input_through_sequence`、原 wire API、原 replay disposition，并在 NATIVE_REPLAY
  时连接 child-local rebuilt fragment；
- 非 assistant entry：两类 owner 均不得携带 assistant generation metadata。

imported source attribution 不复用 live FK 字段。表中新增以下 frozen columns：

```text
imported_source_subagent_task_id text NULL
imported_source_plan_workflow_id text NULL
imported_source_plan_interaction_id text NULL
imported_source_plan_handoff_kind text NULL
  CHECK (imported_source_plan_handoff_kind IN (
    'ENTERED_PLAN', 'REVISION_REQUESTED', 'APPROVED_PLAN',
    'CANCELLED_PLAN', 'FORCE_EXITED_PLAN'
  ))
```

不新增 `imported_source_inter_agent_tool_attempt_id`：首版 imported history 只复制 ROOT scope；
ROOT `INTER_AGENT_MESSAGE` 的来源是 terminal subagent task，而不是 root 发往 child 的 live attempt。

source attribution 约束固定为：

- `EXECUTED_TURN`：四个 imported source columns 全空，现有五个 live `source_*` 字段及 FK/
  trigger 语义保持；
- `IMPORTED_HISTORY`：五个 live `source_*` 字段全空，只允许上述 frozen columns；
- imported `INTER_AGENT_MESSAGE` 必须且只携带 `imported_source_subagent_task_id`；
- imported `PLAN_CONTINUATION` 必须携带 frozen workflow/handoff，interaction 是否存在继续服从
  handoff kind 的现有语义；
- imported `USER_MESSAGE` 只有在 `CANCELLED_PLAN | FORCE_EXITED_PLAN` 时可携带 frozen plan source；
- 其他 imported entry 的 frozen source columns 全空。

frozen source ID 只用于 attribution/display/compiler origin；没有 FK，不得用它查询 child 的
`subagent_tasks`、`plan_workflows`、`plan_interactions` 或 attempts，也不能成为 command target。

现有 `(session_id, source_subagent_task_id)`、`(session_id, source_inter_agent_tool_attempt_id)`
table-level UNIQUE 必须改为仅覆盖 `EXECUTED_TURN` 且 source 非空的 partial unique index。
plan handoff 的两个现有 unique index 同样显式限定 `EXECUTED_TURN`。imported frozen columns 建立
语义对称但与 executed 分离的 partial unique index，使同一个 imported source occurrence 在一个
child session 中只出现一次，同时不与 child 后续创建的 live task/workflow ID 冲突。

现有 source lineage constraint trigger 的第一步必须读取 `entry_owner_kind`：

- executed 分支继续 exact-join live rows；
- imported 分支只验证上述 closed frozen shape，禁止访问或要求 child live rows；
- 未知 owner 或两组 source columns 混用立即 `23514`；
- 不允许因为 imported live FK 缺失而跳过所有 shape 校验。

### 7.3 session-local genesis 与 settled snapshot

每个 Fork child 新增且仅新增一条 `session_context_genesis`，普通新会话没有该行。它只保存
child-local 真值：

- `session_id/workspace_id`；
- `anchor_entry_id`，必须是本 session 的 imported group final；
- anchor 当时冻结的 model-call binding，包括 reasoning；
- `base_kind = FULL_HISTORY | SNAPSHOT`、本地 `source_through_sequence`，以及 SNAPSHOT 分支唯一的
  child-local `context_snapshot_id`。

不保存 parent session/turn/binding FK、祖先链或无独立用途的 source provenance。
Fork 事务同时用该冻结值初始化 `sessions.model_call_binding`；用户之后的模型切换只更新 session
当前选择，不改写 genesis，因此从旧 imported anchor 再 Fork 仍能得到原 anchor 默认值。
`session_context_genesis` 同时解决三个必要问题：首个本地 turn 的初始基底、imported anchor
再次 Fork 的精确基底，以及压缩基底的 UI 投影。它不是第二个 current-context owner：

- `_insert_initial_context_binding_revision` 先寻找最新的本地真实 execution predecessor；
- 仅当不存在该 predecessor 时，revision zero 采用 genesis 的 FULL_HISTORY/SNAPSHOT 联合；
- 一旦本地 turn 产生，后续 turn 仍只沿用正常 predecessor/current binding 路径。

对应的 session 历史形状固定为：

```text
[ imported entries ... imported anchor ][ executed entries ... ]
                                      ^
                         genesis.anchor_entry_id
```

从 imported anchor Fork 时，reader 只读取左侧前缀并再次创建 child-local genesis；从右侧任一合法
executed final Fork 时，reader 使用该 entry 自己的历史 binding 走普通路径。后者可以包含左侧
前缀作为上下文，但不会改变其 owner。已 settled 的 genesis snapshot 再次 Fork 时，carrier 转换
必须幂等：除新的 child-local ID、sequence、cut 与由此重建的 digest 外，历史语义不得继续变化。

不得为继承制造 fake turn、fake initial entry 或 `CompactionAdopted` event。

若源 binding 为 SNAPSHOT，Fork 创建新的 child-local snapshot，并把源 carrier 机械转成 settled
历史载体。carrier contract hard cut 增加有序列表 `retained_historical_requests`：

- 源为 `AWAIT_NEXT_USER` 时，保留 summary/recent messages 和完整历史 request 列表，active request 为空；
- 源为 `RESUME_ACTIVE_TURN` 且 request 位于 canonical suffix 时，request 已作为 imported entry
  复制，carrier 不再重复；
- 源为 `RESUME_ACTIVE_TURN` 且 request 为 `SNAPSHOT_EXACT` 时，将它的精确 provider-visible text
  与原 request kind/origin 追加到 `retained_historical_requests` 末尾；保留已有列表，它是历史材料，不是 human 新指令；
- child continuation 一律改为 `AWAIT_NEXT_USER`，`active_request` 一律为空。

不得把 `SNAPSHOT_EXACT` 文本简单清空，也不得无类型地塞进 `recent_user_messages`，否则 plan、
terminal observation 或 inter-agent request 可能被升级成 human instruction。新 carrier 使用当前
hard-cut contract 重建 canonical bytes，重算 `content_digest/content_size`。snapshot 的
`source_through_sequence` 使用 child-local cut；`source_digest` 由唯一 Fork-genesis builder 对
child session、本地 cut、settled carrier digest 与其 compiler/prompt/model contracts 计算，
只确认这个本地 snapshot base，不把 anchor 后的 suffix 重复算入 base lineage。父 snapshot 的
source digest 编入父 scope/binding，绝不照抄；
不另增 digest registry 或证明图。

#### 7.3.1 `retained_historical_requests` 的唯一类型与 carrier contract

carrier hard cut 将 `COMPACTION_SNAPSHOT_COMPILER_CONTRACT` 从当前 v2 提升为唯一的新 contract；
不保留 v2 parser、dual codec 或缺字段兼容。`CompactionSnapshotCarrier` 新增：

```text
retained_historical_requests: tuple[FrozenRetainedHistoricalRequest, ...]
```

列表按请求发生先后排列，每个成员的 exact value 为：

```text
FrozenRetainedHistoricalRequest
  item_kind:
    USER | PLAN_CONTINUATION | INTER_AGENT_MESSAGE | TERMINAL_OBSERVATION
  input_origin:
    HUMAN_MESSAGE | HUMAN_STEER | SUBAGENT_OBJECTIVE |
    PLAN_CONTINUATION | INTER_AGENT_MESSAGE | null
  text: non-empty strict UTF-8 provider-visible text
```

closed kind/origin 联合与现有 `FrozenProviderInputItem` 一致：

- `USER` 只允许 `HUMAN_MESSAGE | HUMAN_STEER | SUBAGENT_OBJECTIVE`；
- `PLAN_CONTINUATION` 只允许 `PLAN_CONTINUATION`；
- `INTER_AGENT_MESSAGE` 只允许 `INTER_AGENT_MESSAGE`；
- `TERMINAL_OBSERVATION` 的 `input_origin` 必须为空；
- 不保存父 entry ID、sequence、turn ID、task/workflow ID 或 permission；
- text 的 UTF-8 bytes 必须不超过现有 `MAXIMUM_CANONICAL_PROVIDER_INPUT_BYTES`，且整个 carrier
  继续满足既有 snapshot body/resource bounds；不新增独立 lifetime cap。

canonical JSON 顶层唯一形状为：

```json
{
  "continuation": {
    "mode": "AWAIT_NEXT_USER",
    "instruction": "...",
    "active_request": null
  },
  "retained_historical_requests": [{
    "item_kind": "USER",
    "input_origin": "HUMAN_MESSAGE",
    "text": "exact historical request"
  }],
  "earlier_context_summary": "...",
  "recent_user_messages": []
}
```

新 contract 始终写 `retained_historical_requests`，无内容时值为 `[]`；拒绝缺字段、
`null`、单值对象和旧的单数 key，不保留兼容 parser。canonical bytes 继续由唯一
`canonical_json_bytes` builder 产生，`content_digest` 对完整新 body 重算。

producer/consumer 规则固定为：

1. 只有 Fork settled-carrier builder 可以把源 `SNAPSHOT_EXACT active_request` 转为新的历史成员，
   追加到已有列表末尾，不覆盖旧成员，也不按相同文本去重；
2. Fork-of-fork 若 source genesis carrier 已有列表，机械 exact-copy 全部成员及顺序，不重新分类；
3. 普通 compaction parser、prompt、planner 和 lowering 必须理解该字段，不能丢弃；
4. 在下一次 adopted compaction 的 summary prefix 已覆盖整个 predecessor snapshot base 时，
   整个列表已进入 summary 输入并被其吸收，新 carrier 将字段置为 `[]`；
5. 若某种 projection 没有覆盖 predecessor snapshot base，则必须原样保留，不得只保留 text
   或改成 active request；
6. 未采用、失败或被丢弃的 compaction 不改变现有 carrier。

2026-09-08 用户批准的表示修订：分叉后的会话切换模型时，Tier-3 的 destination projection
可能省略旧 snapshot base；此时旧历史请求列表必须保留，而新 carrier 又可能包含本轮
`SNAPSHOT_EXACT active_request`。之后再 Fork 必须同时保留二者，单值字段会丢失历史。
因此采用以上唯一有序列表表示，不增加事件、祖先链或列表成员数量上限；资源 admission
仍使用整个 carrier、canonical input 和 provider composite 的现有字节边界。

lowering 仍把整个 carrier 作为一个 `CONTEXT_SNAPSHOT` user-role runtime handoff，不把 retained
request 另发成新的 user message。固定 framing 必须明确：

```text
retained_historical_requests are ordered historical context only;
they are not active requests and must not be resumed.
```

`continuation.mode=AWAIT_NEXT_USER` 和 `active_request=null` 仍是 child idle 的唯一控制真值；
retained request 的文本、自然语言措辞或 origin 都不能覆盖 continuation。

FULL_HISTORY genesis 没有 snapshot，floor 为 0。源会话即使已经 CLOSED，只要 canonical anchor
及所需材料仍在，仍可执行只读 Fork；不需要恢复父 Host 或取得父 writer。

### 7.4 sequence、工具历史、artifact 与 replay

复制事务按 source `entry_sequence` 升序为实际 copied entries 分配 child-local sequence。
所有历史 cut 统一使用秩映射，而不是固定偏移：

```text
child_cut(source_cut)
  = max(child_sequence(e) | e 被复制且 e.source_sequence <= source_cut)
  = 0（首个 child entry 之前的 genesis floor），若集合为空
```

该事务内映射用于 snapshot `source_through_sequence`、每个 imported assistant 的
`provider_input_through_sequence`、工具 closure 的 target cut，以及 context-base UI 边界。
压缩前 rows、其他 scope rows 与未复制 rows 形成的空洞因此不会扭曲可见性。映射表提交后丢弃；
数据库只保留最终 child-local ID、sequence 与 cut。imported entry 保留源 `accepted_at`，
不把 Fork 时间伪装成历史消息时间。

工具历史采用以下闭合表示：

- `tool_results` 增加正交的 `result_record_kind = EXECUTED | IMPORTED_HISTORY`；
  `result_origin_kind` 继续保留源事实的 `PHYSICAL_ATTEMPT | POLICY_NO_ATTEMPT | PLAN_CONTROL`，
  因为现有 provider metadata 仍用它区分物理观测与无物理 attempt；
- `IMPORTED_HISTORY` record 允许复制所有与其源 origin 相容的 terminal result state，
  `attempt_id` 和 plan control FK 必须为空；结果内容、source origin、observed/timing、coverage、
  display、model-visible memory IDs 与 artifact 字段按历史事实保留；permission fingerprint 不复制，
  也没有任何字段可以据此授予 child 权限；
- 不复制 `tool_execution_attempts`、plan interactions 或授权；
- 新增最小 `imported_tool_call_closures`，仅为在该历史 target cut 看不到 result 的 imported
  tool call 保存映射后的 target cut，以及
  `INTERRUPTED_BEFORE_DISPATCH | INTERRUPTED_MAY_HAVE_PARTIALLY_EXECUTED |
  PLAN_INTERACTION_ABORTED` 三种既有 closure kind；
- target cut 已可见 result 时不写 closure 行，不保存冗余 `RESULT_VISIBLE` receipt；若真实 result
  在 closure 后才进入最终 child cut，reader 继续由 result sequence 推出 late outcome，不另建 durable 行。

`result_record_kind` 不是执行 receipt：它必须 exact-join result entry 的 owner kind，只用于让同一
结果表在行级约束中区分“本 session 执行”与“只读导入”，同时保留原 result origin 语义。
reader 必须验证每个 imported tool call 在其 target cut 恰有“可见 result”或“一条 frozen closure”，
不能因没有 attempt 而默认成 `INTERRUPTED_BEFORE_DISPATCH`，也不能用最终已知 result 改写过去。

#### 7.4.1 `tool_results` 的 exact owner/origin 联合

`tool_results` 新增：

```text
result_record_kind text NOT NULL
  CHECK (result_record_kind IN ('EXECUTED', 'IMPORTED_HISTORY'))
```

同时将 `permission_snapshot_fingerprint` 改为 nullable，并以 closed branch 固定语义：

- `EXECUTED`：`tool_call_entry_id` 与 `result_entry_id` 都必须属于同一真实 turn 的
  `EXECUTED_TURN` entry；`permission_snapshot_fingerprint` 非空并继续 exact-join 该 turn；
  `attempt_id`、plan control FK、origin/state/timing 的现有约束保持不变；
- `IMPORTED_HISTORY`：call entry 与 result entry 都必须属于同一
  `IMPORTED_HISTORY` group；`permission_snapshot_fingerprint`、`attempt_id`、
  `control_plan_workflow_id`、`control_plan_interaction_id` 全空；origin/state 只允许源关系中既有的
  三个闭合组合，即 `PHYSICAL_ATTEMPT` 对应物理 terminal states、`POLICY_NO_ATTEMPT` 对应 policy
  terminal states、`PLAN_CONTROL` 对应 plan-control terminal states；timing/observation 字段仍须满足
  其 origin 的既有 shape；
- 两个分支都要求 result row exact-join 同 session/workspace 的 `TOOL_RESULT` inline entry、
  call row exact-join 对应 `TOOL_CALL` block，并保留现有一 call 一 result 唯一性；
- 普通 tool settlement DTO/SQL hard-code `EXECUTED`；只有原子 Fork owner 可写
  `IMPORTED_HISTORY`，且不能随后由 attempt、plan 或 permission writer 更新。

imported result 不复制 permission fingerprint 到另一个字段。它既不参与 provider-visible 历史，
也不能证明 child permission；源 `result_origin_kind`、result state 与 observation 已足以保持工具历史语义。
constraint trigger 必须先按 `result_record_kind` 分支，再决定是否 join `turns`、attempt 或 plan rows；
不得因 imported `attempt_id IS NULL` 把物理成功误判成 policy result，也不得跳过 call/result 同 group 校验。

artifact 路径固定为 session-scoped identity：删除全局唯一索引，改为
`UNIQUE(session_id, output_artifact_id)`。imported result 保留原 artifact ID 和 canonical preview
原字节，并连接同 workspace 的同一 immutable blob；这样现有 session/workspace-scoped
`artifact_read` 仍能按 preview 中的 ID 精确读取。native 新 result 继续用现有 ID builder；
只有 `result_record_kind=IMPORTED_HISTORY` 不要求 artifact ID 由它的新 result entry ID 重新推导。
不生成跨 session alias，不解析重写自然语言、tool result preview 或 opaque provider payload。

entry、block、group、result row、snapshot、binding 与 replay row 使用新的 child-local ID。
provider `tool_call_id` 和 opaque replay payload 保持原样；replay ID、assistant entry identity、
public projection/fragment metadata 用现有 builder 按 child session/entry 重建并重新校验。
不照抄父 fragment fingerprint，不全局字符串替换 payload 内 ID，不伪造新的 replay target。

### 7.5 原子创建、Host 打开与结果联合

客户端在请求前生成一个高熵 opaque child session ID，请求只提交 source session ID、所选
canonical final entry ID 与该 child ID。服务端先验证 ID 格式且当前不存在，再在 PostgreSQL 一致视图内：

1. 重新核验 source entry 的 eligibility，并冻结其历史 binding、anchor model binding、有效范围；
2. 分批读取/准备历史，但在一个 canonical 写事务中原子插入 child session、imported groups、
   entries/blocks/results/closures/replay、可选 snapshot 与 genesis；不调用 LLM/MCP/hook；
3. 提交后把 child 发布到会话列表，再通过普通 resume/open 生命周期取得 writer、发现当前能力并启动 Host。

父会话后续追加不进入 cut；源只读，不 acquire 父 writer，也不新增父 execution lease。
复制事务失败整体回滚，不留下可见半成品。canonical commit 与 Host open 是两个明确阶段，API
返回封闭结果联合：

- `CREATED_AND_OPENED`：canonical child 已提交且 Host 已打开；
- `CREATED_OPEN_DEFERRED`：canonical child 已提交，但 Host 暂未打开；child 保留在列表中，
  后续只走普通 resume，不重做 Fork；
- `NOT_CREATED`：canonical 事务未提交，不存在 child。

`CREATED_OPEN_DEFERRED` 不能包装成“创建失败”，`NOT_CREATED` 也不能暗示存在可恢复分支。
若响应丢失，客户端只按预先确定的 child ID 查询 session：存在则走普通打开，不存在则提示重试；
不自动盲重发 INSERT，不新增 durable job、receipt、checkpoint、exactly-once 声明或后台修复流程。
按钮 busy 仅防止当前窗口连点。

会话列表保留 canonical activity 排序，但 imported entries 使用旧时间，因此 user-visible
`updated_at = GREATEST(session.created_at, COALESCE(last_entry.accepted_at, session.created_at))`；
后续新 entry 自然成为最新活动。

读取/准备应分页或流式，避免按完整生命周期一次物化；有效上下文本身仍可能较大。
不新增任意会话长度、turn 数量、压缩次数、Fork 次数、模型/工具调用次数、wall-clock lifetime
或 lineage depth cap。真正触及既有资源边界时返回 typed failure，不能悄悄少复制消息。

#### 7.5.1 唯一事务边界

canonical Fork 创建使用**一个 PostgreSQL `REPEATABLE READ` read-write transaction**。source
eligibility/historical extraction 与 child 所有 canonical inserts 必须使用同一 connection、同一
transaction snapshot；不允许先在 READ COMMITTED 或另一个 connection 读取，再把 Python DTO
交给独立写事务。

唯一流程为：

```text
BEGIN ISOLATION LEVEL REPEATABLE READ
  validate child ID absent + target workspace
  validate source session/anchor eligibility
  freeze executed binding or imported genesis
  keyset-page source entries/blocks/results/closures/replay/blob descriptors
  build and validate FrozenForkHistoricalMaterial batches
  allocate child-local IDs/sequences/cuts
  insert child session + imported prefix + optional snapshot + genesis
  revalidate all child-local relational invariants
COMMIT

after COMMIT
  publish list visibility
  ordinary resume/open Host
```

分页使用 source `entry_sequence` 加稳定 ID 的 keyset cursor，并始终带 anchor upper bound；不得用
offset paging，也不得在分页之间释放 transaction。父会话并发追加、idle compaction 或 late result
只能出现在这个 transaction snapshot 之外，不能进入已经冻结的 copy view；无需 acquire 父 writer。

transaction 内可以分批 decode/insert，但任一阶段的：

- eligibility drift；
- malformed canonical relation；
- pagination/resource bound；
- deadline；
- child ID/constraint conflict；
- blob/replay/artifact exact-join failure；

都回滚整个事务并返回 `NOT_CREATED`。deadline 必须使用现有 foreground canonical watchdog，
不新增 Fork lifetime cap。commit 已确认后发生的 Host/open failure只能返回
`CREATED_OPEN_DEFERRED`；commit ACK unknown 按预定 child ID 查询，不重跑复制事务。

#### 7.5.2 `FrozenForkHistoricalMaterial` 纯历史读取边界

新增一个 repository/reader 内部 frozen DTO；它是 Fork transaction 内的 exact value，不 durable、
不进入 provider、没有 fingerprint 字段：

```text
FrozenForkHistoricalMaterial
  source_session_id
  workspace_id
  anchor:
    source_entry_id
    source_entry_sequence
    owner_kind: EXECUTED_TURN | IMPORTED_HISTORY
    settled_status: COMPLETED | INTERRUPTED
    nonempty_text_block_ordinal
    anchor_model_call_binding
  base:
    base_kind: FULL_HISTORY | SNAPSHOT
    source_through_sequence
    snapshot_carrier | null
  imported_groups[]:
    source_group_key
    settled_status: COMPLETED | INTERRUPTED
    accepted_at
    terminal_at | null
    copied_final_source_entry_id | null
  entries[]:
    source_entry_id
    source_entry_sequence
    source_group_key
    entry_kind
    accepted_at
    exact content descriptor/body
    assistant blocks in block_ordinal order
    assistant generation metadata | null
    frozen source attribution | null
  tool_results[]
  required_tool_closures[]
  replay_fragments[]
  referenced_artifact_blobs[]
```

`source_group_key` 只是该 transaction 内的判等键：executed row 使用 source `turn_id`，imported row
使用 source `imported_history_group_id`。它不写入 child，也不形成 parent reference；builder 为每个键
创建新的 child-local group ID。`nonempty_text_block_ordinal` 是通过第 2.1 节判据的至少一个 `TEXT`
block ordinal，不是 hash、proof row 或新资格 authority；Fork owner仍会在同一 snapshot 中重新解码该 block。

`tool_results[]` 包含原 result state/origin、result entry、timing、coverage/display、memory IDs、
artifact handle/blob descriptor；不携带 permission fingerprint。`required_tool_closures[]` 只包含第 7.4 节
三种不可由 copied result/cut 重建的 closure。replay fragment 保存 validated opaque payload 与其
source semantic metadata，由 child builder 重建 local identity。

唯一读取接口为：

```text
read_fork_historical_material(
  connection,
  source_session_id,
  anchor_entry_id,
  deadline_monotonic,
) -> FrozenForkHistoricalMaterial
```

传入的 connection 必须已经处于第 7.5.1 节同一 `REPEATABLE READ` transaction。该 reader：

- 自己重新派生 eligibility，不信任 UI boolean；
- executed anchor 只接受 entry 自身 historical binding；
- imported anchor 只接受同 session genesis；
- 使用现有 pure content/block/tool closure/replay decoder；
- 扩展读取上界至 anchor entry 本身，而不是停在 provider input cut；
- 返回 source 坐标，child-local ID/cut 映射只由原子 Fork owner 完成。

它明确不得返回或安装：

- current permission snapshot 或 approval；
- writer lease/reservation；
- live attempt、plan interaction、task、terminal process、queue；
- current session model selection（anchor binding除外）；
- current capability/Skill/MCP inventory；
- Hook/memory candidate；
- executable provider dispatch、open attempt 或 continuation authority。

不得给 `read_frozen_dispatch` 增加通用 `ignore_stale=True`；可以抽取共用的纯 decoder/query helper，
但 foreground dispatch 与 Fork historical material 必须保持两个显式 authority 入口。

### 7.6 Oracle 增量与失败所有权

| Oracle 类别 | 本 hard cut | 独立必要性、owner 与失败路径 |
| --- | --- | --- |
| durable event / live event kind / subject slot / append guard | 不增加 | eligibility、initial context base 与创建结果均为关系/接口投影，不伪造事件证明复制成功 |
| product relation | 增加 `imported_history_groups` | 表示不完整 source turn 的只读分组/final；由 repository Fork owner 在 child 创建事务写入，任一约束失败整体回滚为 `NOT_CREATED` |
| product relation | 增加 `session_context_genesis` | 为首轮 binding、唯一 imported anchor 与 context-base UI 提供同一 child-local 真源；同事务写入，缺失或不闭合即回滚 |
| product relation | 增加 `imported_tool_call_closures` | 在不复制 attempt/plan object 时保留无法由 result/cut 推出的 closure kind；仅 closure-required call 写入，同事务失败即回滚 |
| existing relation shape | `transcript_entries` owner 联合；`tool_results.result_record_kind`；artifact 唯一键改为 session-scoped | 分别隔离执行权限、保留 result origin、维持现有 scoped reader；由 clean-v0 constraint/trigger exact-join，不增加新 authority owner |
| durable job / receipt / checkpoint / recovery owner | 不增加 | post-commit Host 失败由普通 resume 处理，响应丢失按预定 child ID 查询 canonical session |

唯一写 authority 是现有 `ConversationKernelRepository` 下的原子 Fork owner；reader、terminal 和
Host 只消费已提交值。Host 打开属于事务外普通 lifecycle，失败只能得到
`CREATED_OPEN_DEFERRED`，无权回滚或改写 canonical child。

## 8. UI 与 API 边界

在最终自然语言回复的“复制回复”图标旁增加 Fork 图标按钮，沿用现有尺寸、悬停和焦点风格。
中文 tooltip/aria-label 为“从此处分叉”。窄屏与键盘导航均可操作。

前端从 canonical projection 得到是否可分叉及所需 ID；不根据文本内容或最后一条
assistant 的位置猜测 terminal。中间回复、流式消息、工具卡、子任务回复不出现入口。

当前 `runtime-adapter.ts` 将所有 `ASSISTANT_MESSAGE` 标作 `assistantKind='terminal'`
且 `status='completed'`，这两个展示字段不能用于 Fork eligibility。
terminal v3 才是 Web 历史的实际协议落点，必须同步 hard cut：

- `CanonicalEntry` 增加 `fork_eligible` 和 `entry_owner_kind` 投影，随后重新生成 bindings；
- `canonical_v3.py` 的 entry reader 对 executed entry exact-join `turns.final_entry_id`、ROOT、
  结算状态和后端非空 `TEXT` 判据；对 imported entry 仅在它同时是 group final 与
  `session_context_genesis.anchor_entry_id` 时返回 true；
- initial snapshot、history page 与 committed immutable-entry observation 共用同一 entry
  投影逻辑，不能只有首屏或 `query.py` 路径带资格；
- `pulsara-types.ts`/runtime adapter 将该布尔值映射到 message，workbench 只据此显示按钮。

不为了这个派生事实新增数据库列或事件；创建 API 仍重新核验，不能信任客户端回传的 true。
imported anchor 的按钮在 child 追加本地 turn、完成后续压缩或切换当前模型后仍保留；这些变化只能
影响新 executed final 的普通入口，不能移动、替换或遮蔽 genesis anchor。点击该按钮时前端只提交
canonical anchor ID，不提交“当前摘要”、消息数组下标或自算边界。

点击后：

- 当前按钮显示创建中并禁止重复点击；不锁死整页，不停止父会话；
- `CREATED_AND_OPENED` 后刷新列表并打开 child，输入框为空且可继续输入；
- `CREATED_OPEN_DEFERRED` 刷新列表并提示稍后重试打开，不把已提交 child 说成创建失败；
- `NOT_CREATED` 或请求失败显示明确提示，保留原会话与滚动位置，不展示虚假的成功分支；
- 不弹出额外“审批”对话框，不要求用户理解 binding、cut 或 storage owner。

子会话仅展示复制的有效历史。存在摘要基底时复用既有上下文压缩分隔视觉，
简短说明“已保留分叉点的有效上下文，压缩前记录请在原会话查看”。
没有压缩时，不为了 Fork 虚构一次 compaction。

当前分隔线只从 `CompactionAdopted` event 投影，而 Fork 明确不复制、不伪造该事件。
因此 `CanonicalControl` 增加由 `session_context_genesis` 派生的 `initial_context_base`：包含
`base_kind`、本地 `source_through_sequence` 和映射后的 display boundary。前端把 SNAPSHOT
分支投影成同一视觉组件，但文案说明它是 inherited Fork context；FULL_HISTORY 不显示分隔线。
该 control 不是 durable event，也不冒充 child 发生过 compaction。

该说明不保存父引用。若将来增加“返回原会话”链接，必须另行为可失效导航定义来源数据；
它不能成为历史读取、关闭或启动前提。本轮不要求分支树、永久来源关系或该链接。

## 9. 代码真源与实施落点

本次核对的是 Pulsara 当前代码，没有重新执行旧 Codex checkout 的调研。
Codex 的结构共享实现只作为旧方案背景，不作为本方案的实现模板。

| 当前文件/owner | 已核实情况与实施职责 |
| --- | --- |
| `storage/migrations/sql/0000_conversation_kernel_baseline.sql` | hard cut entry owner 联合、`imported_history_groups`、`session_context_genesis`、imported result/closure 约束及 session-scoped artifact 唯一性；`turns` 保持 execution-only |
| `conversation_kernel/compaction/contracts.py`、`prompt.py`、`model_input/lowering.py` | settled carrier 的 `retained_historical_requests` 闭合联合、canonical 编解码与降级；普通 compaction 同步使用新 hard-cut contract |
| `conversation_kernel/reader.py` | 抽出 final-entry 历史材料读取；按 owner 解码 imported rows，消费 frozen closure、推导 late outcome，并让首轮 genesis binding 使用普通 compile 路径 |
| `conversation_kernel/query.py` | 单 session canonical pagination；继续保留，不改为 ancestor reader |
| `conversation_kernel/compaction/planner.py`、`coordinator.py` | 保留窗口、工具组与已采用基底；Fork 不另造窗口选择算法 |
| `conversation_kernel/_repository/` | 新增单一原子 Fork owner、genesis revision-zero 接线及严格 imported/execute 写入口；不经过 submit 或工具执行入口回灌 |
| `conversation_kernel/tool_artifacts.py` | 保持 session/workspace-scoped reader；native builder 不变，imported result 保留历史 handle/preview/blob |
| `llm/provider_replay.py`、`model_input/provider_replay.py` | 复用通用 replay builder/兼容选择；重建 local metadata，opaque payload 不变 |
| `terminal_protocol/schema/terminal_kernel_v3.proto`、generated bindings、`canonical_v3.py` | 投影 `fork_eligible`、entry owner 与 initial context base；snapshot/history/observation 一致 |
| `conversation_kernel/host.py` | 预建 child 的普通 resume/open、三类创建结果、列表 activity 的 `max(created_at,last_entry)` |
| `web_app/session_controller.py`、`http_server.py` | 接收预定 child ID，连接原子创建结果与普通会话生命周期；不先发布空 child |
| `frontend/lib/pulsara-types.ts`、`runtime-adapter.ts` | 消费 terminal canonical 资格/context base 与创建结果；不在浏览器推导 cut 或 terminal |
| `frontend/components/workbench-view.tsx` | 在现有复制回复按钮旁显示服务端授权的 Fork 入口，并处理 opened/deferred/not-created |

上述是 owner 范围，不要求为了每一行新增模块。实施先复用现有扩展点，新增关系仅限第 7 节已说明的
独立必要性。本方案需要 schema hard cut，必须同步 clean-v0、相关 catalog/grant oracle 与测试；
不为未发布内部设计建设在线迁移、双读或旧结构共享兼容路径。

### 9.1 全 consumer owner-routing 矩阵

所有直接或间接读取 `transcript_entries` 的代码必须登记到下面三类之一。任何 SQL join 不得通过
`turn_id IS NULL/NOT NULL` 暗猜 owner；先读取/限定 `entry_owner_kind`，再进入对应分支。

| Consumer 类别 | executed entries | imported entries | 规则 |
| --- | --- | --- | --- |
| history query/pagination | 读 | 读 | 统一按 child-local sequence；imported 的 protocol turn ID 投影为 group ID |
| terminal v3 snapshot/history/immutable observation | 读 | 读 | 共用 canonical entry projector；投影 owner 与服务端 fork eligibility |
| provider historical/context reader | 读 | 读 | imported 只作为 settled history；不得成为 active execution identity |
| compaction source/planner | 读 | 读 | 可压缩两者的历史内容；新 snapshot/binding 仍归真实 executed turn |
| artifact read/blob GC/replay hydration | 读 | 读 | 全部按 child session/workspace 与 rebuilt local relation exact-join |
| Fork historical extractor | 读 | 只读唯一 anchor | imported 只允许 genesis anchor；其他 imported final 拒绝 |
| prompt submit/steer/queue/command target | 读/写 | 拒绝 | target 必须 exact-join `EXECUTED_TURN` 或 session；不能把 group 当 turn |
| tool attempt/result settlement | 读/写 | 拒绝 | 只有 Fork owner 可插入 imported result；physical runtime 不可结算到 imported call |
| Plan workflow/interaction | 读/写 | 拒绝 | frozen imported plan attribution 不连接 live workflow |
| subagent task/message/completion | 读/写 | 拒绝 | frozen imported task attribution 不连接 live task/attempt |
| permission inheritance/admission | 读/写 | 拒绝 | imported group/frozen source 不参与 permission exact-join |
| Hook/notification/memory-candidate producer | 新 occurrence | 不产生 | import transaction及后续扫描都不能把 imported rows 当新 occurrence |
| canonical events/session commands | 真实执行写 | 不写 | Fork 不为 copied rows 伪造 event/command |
| 原子 Fork writer | 不伪造 executed | 唯一写者 | 仅在新 child 无 executed rows 时安装封闭 imported prefix |

实施必须用 `rg`/SQL inventory 列出所有直接查询 `transcript_entries`、`tool_results`、
`assistant_message_blocks` 的生产 consumer，并逐项记录上述分类。最低要求覆盖当前
`_repository/{conversation,kernel,prompts,tools,plans,subagents,memory,memory_management,completions}`、
`reader.py`、`query.py`、`host.py`、`blob.py`、`tool_artifacts.py`、terminal v3 与 Web adapter。

数据库层同时保证：

- 普通 `_insert_entry` 只能写 executed owner；
- execution-target FK/trigger 只接受 executed owner；
- imported prefix commit 后没有普通 append 入口；
- child 后续 first executed entry sequence 严格大于 imported anchor；
- memory、Hook、notification、tool/plan/subagent producer 不因 copied `accepted_at` 或新 session ID
  将 imported rows 识别为待处理 occurrence。

### 9.2 Artifact handle 全 consumer 审计

全局 artifact 唯一索引 hard-cut 为：

```sql
CREATE UNIQUE INDEX uq_pulsara_v3_tool_result_output_artifact_id
ON pulsara_v3.tool_results (session_id, output_artifact_id)
WHERE output_artifact_id IS NOT NULL;
```

同时完成以下单一路径审计：

- `_repository/tools.py`、`_repository/subagents.py`：native settlement 继续使用现有 ID builder，
  并以 session ID 确认 winner；
- `reader.py`：result/artifact metadata 必须由同 session result row 读取；
- `tool_artifacts.py`：保持 `session_id + workspace_id + output_artifact_id` 唯一查找，查询多行即 conflict；
- `blob.py::delete_orphans`：任何 parent 或 child imported result 引用同 workspace blob 都阻止 GC；
- terminal/provider projection：artifact handle 只在当前 session 内解释，不生成跨 session URL/ref；
- memory citation与 provider replay：若携带 artifact handle，必须同时从当前 result/session 关系取得，
  不得按裸 `output_artifact_id` 全库搜索；
- tests/diagnostics/admin SQL：不得保留“artifact ID 全局唯一”的断言或无 session predicate 查询。

imported result 保留 source artifact ID 是一种 session-local 历史 handle 复制，不改变 blob 的内容身份。
child 通过自己的 result row 引用同 workspace immutable blob；不复制 blob bytes，不新增 alias row，
不允许不同 workspace 复用该 FK。父或 child 任一真实引用存在时，现有 restrictive FK/GC 保留 blob；
两边都不再引用时才由现有 GC owner 回收。

## 10. 必须覆盖的验收场景

| 场景 | 必须得到的结果 |
| --- | --- |
| 无压缩、从第一条或后续最终回复分叉 | 初始有效历史至选中回复完整复制，最终回复不遗漏 |
| 父会话已有 Cn，从 Ci 之后的旧回复分叉 | 使用 Ci；Cn 与 Fork 点之后的内容完全不进入子会话 |
| C2 retained A1、从 A2 Fork 后在 child 查看入口 | imported A2 anchor 可再次 Fork；A1 无入口且 API 拒绝，绝不让 C2 成为 A1 的基底 |
| child 创建事务提交时的新老边界 | 全部 copied entries 构成封闭 imported prefix；anchor 是最大 imported sequence；尚无 executed entry |
| child 已产生 U3/A3，再从 imported A2 anchor Fork | 新分支止于 A2，完全不含 U3/A3；仍使用 A2 的 genesis，而不是 A3 的 binding |
| child 后来采用 C3、切换模型或正在追加新 turn，再从 A2 Fork | A2 按钮仍在；使用不可变 genesis/model binding，C3、当前选择及并发追加均不进入固定 cut |
| 普通运行写入口或已产生 executed entry 后尝试插入 imported history | repository/owner 联合拒绝；绝不形成 imported/executed 交错序列 |
| 已 settled 的 imported anchor 连续 Fork-of-fork | 每代只重建本地 ID/cut/digest，carrier 历史语义幂等，不重新激活旧请求 |
| 一次 turn 中途压缩 | 保留窗口即使早于摘要创建时间仍被复制，不重复、不补全旧 turn |
| 一个长程 turn 多次压缩后结束 | 只复制该点最后 adopted 基底与有效窗口，无旧摘要链依赖 |
| 压缩候选失败/未采用 | 继续使用此前有效基底，不引用候选内容 |
| 最终回复之后在同一 terminal turn 上手动压缩 | 所选 entry 仍绑定旧 revision；Fork 不采用后来更新的 turn.current binding |
| 最终回答意图产生但存在 pending steer | `final_entry_id` 未提交时不可 Fork，不能根据正文像答案就放行 |
| 父会话仍运行后续 turn | 允许选此前合法最终回复，父会话不受干扰，无新内容泄入 |
| 父会话在 Fork 分页期间并发 append/idle compaction/late result | 单一 REPEATABLE READ view 冻结 source；新提交不可半途进入 copied set，anchor upper bound 不移动 |
| RUNNING、中间正文、仅思考、仅工具、子任务消息 | UI 无入口；伪造 API 请求也被拒绝 |
| INTERRUPTED 但无 canonical 最终 NL | 无入口，不合成回复或 Fork 节点 |
| assistant manifest/DATA 有内容但 TEXT 为空白 | 无入口；只有解码并 trim 后非空的 TEXT block 才满足自然语言条件 |
| 摘要保留 Skill/user anchor/工具闭合材料 | 与现有编译语义一致，不仅复制展示字符串 |
| 旧工具已移除/权限变化 | 历史仍可阅读，当前 tools 按新会话状态生成，无旧调用授权 |
| Chat 与 Responses 的工具/思考历史 | 原有关联与兼容 replay 正确，不改签名，不重复执行 |
| 原模型配置删除或目标改变 | 不复活旧配置；按现有模型选择与预算语义处理 |
| imported anchor 所在 child 后来改过 session 模型 | 从旧 anchor 再 Fork 使用 genesis 冻结的 anchor model binding，不读取后来 session 当前值 |
| 共享 memory 在 Fork 点后变化 | child 新 cold epoch 可见当前合法 memory；测试不把 conversation 的历史 cut 错当 memory 回滚 |
| 子会话重启后继续及再次压缩 | 不需要父会话或原 live 对象，基底和 suffix 均从本地 canonical 读取 |
| child 只有复制的 snapshot 与历史，尚未产生新 turn | 第一轮 revision zero 确实采用复制基底，不默默退回 FULL_HISTORY |
| 源 mid-turn carrier 的 active_request 只存在于摘要 | 精确文本和 typed origin 进入 `retained_historical_requests`；continuation 为 AWAIT，不把旧 ID/请求当作 child 当前任务 |
| 无 result 的旧 tool call 曾有 physical attempt | frozen closure 保持 MAY_HAVE_PARTIALLY_EXECUTED，不因 child 无 attempt 降成 BEFORE_DISPATCH |
| 工具 closure 先出现、真实结果迟到 | target cut 保持原 closure，映射后 result sequence 推出 late outcome；不因复制时已知结果而改写过去，也不另存 late receipt |
| 工具原始结果 artifact 与 GC | child 中相同 artifact ID/preview 可按 session 读取同一 immutable blob；父子关闭、重启和 GC 后仍可读，无全局唯一键冲突 |
| 两个 session 持有相同 artifact ID | 两个 `(session_id, artifact_id)` 各自唯一可读；裸 artifact ID 查询被测试/审计拒绝 |
| child 的 entry/block/replay ID 与序号重分配 | 秩映射后的所有 cut/metadata 一致，旧 cut 相对可见性不变，opaque wire payload 与 tool_call_id 不改写 |
| imported entry 被提交给 steer/compact/tool/plan API | 数据库 owner 联合与服务端核验拒绝；它只能参与历史读取和唯一 anchor Fork |
| Fork 创建时检查 Hook/memory/notification/tool side effects | imported rows 不产生 occurrence、不调用 hook、不生成 memory candidate、不结算或重跑工具 |
| child 后续执行新 turn 后后台 consumer 扫描 imported prefix | imported rows 仍只作为历史输入，不被重新解释成待处理用户请求、计划、子任务或工具结果 |
| Fork-of-fork | 只读取当前源会话，行为相同，不建立祖先解析链 |
| source session 已 CLOSED | 仍可从 canonical anchor 只读 Fork，不恢复父 Host 或抢父 writer |
| 父/子分别关闭、重启并运行 blob GC | 另一会话完整可读可继续，无共享前缀门禁；物理删除不在本轮伪测 |
| 复制事务失败 | 返回 NOT_CREATED，无 child、半成品、事件或外部副作用 |
| canonical 已提交但 Host 打开失败 | 返回 CREATED_OPEN_DEFERRED，列表可见，后续普通 resume；不重做 Fork |
| 提交响应丢失、快速连点 | 按预定 child ID 查询；不自动盲重发；busy 只阻止本窗口重复操作 |
| imported 时间很旧的新 child | 历史保留原 accepted_at，但列表 activity 至少为 session.created_at，不沉到旧会话尾部 |
| SNAPSHOT genesis 的 UI | terminal control 投影 inherited context base 并复用视觉，不伪造 CompactionAdopted event |
| UI 桌面/窄屏/键盘/历史分页/observation | 各路径资格一致，入口稳定；opened 成功导航，deferred/not-created 不跳离或误报 |

自动测试检查内容、顺序、cut、关联与零执行副作用，不通过改写断言或重新总结原文获得通过。
真实 dogfood 至少验证一个无压缩分支、一个压缩后的分支及一次历史分叉；
由真实模型在子会话中继续，并核对 provider 实际输入没有未来内容和无关旧历史。
长程多次压缩、失败事务及边界攻击优先通过确定性测试覆盖，不需要人为消耗无限模型调用。

## 11. 不做的事项

- 不做 immutable-prefix reference、history_base 祖先链、LCA/merge、detach 或 lineage pagination。
- 不复制完整 canonical execution graph，不用事件回放重建子会话。
- 不复制旧摘要链来开放所有 imported final；首版只开放唯一 imported anchor。
- 不进行运行中分叉，不冻结或转移父会话的 live execution。
- 不造 fake turn、attempt、plan/task、用户消息或 CompactionAdopted event 满足 FK/UI。
- 不为 Fork 添加 provider 特例、在线能力试探或凭据迁移。
- 不保证历史文件环境和外部状态隔离；不暗中创建工作区副本。
- 不新增父子 provenance/FK、跨 session artifact alias、证明性 fingerprint、持久化收据、
  执行恢复框架或任意 lifetime cap。
- 不夹带 durable title/rename 或物理删除产品能力。

完整历史复制并非天然产生“可漂移 truth”；本方案舍弃它，是因为用户已明确不需要
复制被摘要覆盖的原文。结构共享也并非错误，只是不值得为当前产品契约引入其读取与删除成本。
本方案的取舍是：用一次有界于有效历史材料的独立复制，换取之后普通会话的简单生命周期。

## 12. 实施前二次代码核对与 critic 结论（历史记录）

以下结论来自实施前基线代码的静态链路审阅，不是当前实现状态；落地结果见第 13 节。
原有测试只能说明已有 owner 的契约，不能用来宣称 Fork 已可运行。
独立 `gpt-5.6-sol / max` critic 在不修改文件的前提下复核了第 2、7、8 节的风险；
其建议不是新 authority，只有经当前代码再次核验并写入本文的决定才成为实施契约。

复核执行了 `tests/test_round5b_long_horizon_context_compaction.py` 中的
`test_round5b_summary_normalizer_and_snapshot_carrier_are_bounded` 与
`test_round5b_repeated_compaction_carries_runtime_owned_active_request`：2 passed。
它们确认现有 carrier 的 continuation 联合语义及多次压缩保留 active request；
未运行真实模型、Fork prototype 或生产数据库写入。

### 12.1 最终消息的精确身份已存在，但 UI 尚未使用

`_repository/conversation.py::commit_assistant_message` 在同一事务中写 entry 与 blocks。
只有 `complete_turn and not pending_steer` 时才写 `turns.final_entry_id` 并完成 turn。
因此，一个没有工具调用、已经显示出来的助手正文仍可能不是最终回复。
`interrupt_turn` 只结算状态，并不会替此前正文补一个 final_entry_id。

`query.py::_page_entries_on_connection` 当前主要读取 entries/blocks，没有提供每条 entry 的
root terminal-final eligibility。更关键的是 Web 历史经过 terminal v3：其 `CanonicalEntry`
没有资格字段，`canonical_v3.py::_entry` 未 join final 真值，`runtime-adapter.ts` 又把每个
ASSISTANT_MESSAGE 都展示为 terminal/completed。

实施必须按第 8 节在 terminal schema、generated bindings、snapshot/history/observation、
前端类型和 adapter 上贯通同一派生事实。非空条件只读取解码后 trim 非空的 TEXT block。
executed final 使用 turn/binding；imported history 只允许 genesis anchor，不能伪装成 child
新执行完成的 turn。不新增“Fork 已批准”状态、durable occurrence 或 terminal fingerprint。

### 12.2 历史 revision 不能用当前 reader 的普通入口直接读取

`_repository/conversation.py::adopt_context_snapshot` 会更新 turn 的 current binding；
`_require_compaction_target` 明确存在面向最新 terminal turn 的 `IDLE_BASE_ONLY` 分支。
所以 terminal turn 的 current binding 并非“生成最终回复时的 binding”的不可变别名。

与之相对，真实执行的助手 entry 在提交时持有自己的 `context_binding_revision_id` 和原 provider
cut；这两项足以定位生成所选最终回复的历史基底，不必新增终态摘要快照或消息 hash。
imported assistant 明确不伪造该 binding；只有 imported anchor 可改由 source session 的
`session_context_genesis` 定位，其他 imported final 已被资格规则排除。

`reader.py::read_frozen_dispatch` 当前默认要求所请求 revision 等于 turn.current，
不满足会报 `provider binding revision is stale`。已有
`read_memory_governance_historical_snapshot` 通过核验 producer entry 提供了一个窄的历史读取入口，
但它是 memory 专用语义，而且截止生成消息之前，不等于 Fork 的读取范围。

实施按第 7.5.2 节新增唯一
`read_fork_historical_material(connection, source_session_id, anchor_entry_id, deadline)`
入口：executed anchor 读取已核验的历史 revision，imported anchor 读取已核验的 session genesis，
两者都扩展至包含所选最终回复的 entry 上界，并在第 7.5.1 节同一 `REPEATABLE READ`
transaction 内完成。不得伪造 memory producer、临时把父 turn.current 改回旧值，或增加
`ignore_stale=True` 让普通 foreground 任意忽略 binding 约束。

读取历史内容与生成新的执行 compile snapshot 应分开：原 reader 还装配权限、计划、
freshness 等运行事实，这些不能随历史读取结果直接安装到 child。
所需的是同一套 canonical 材料解码/closure/replay 逻辑，不是父会话完整 dispatch 对象。

### 12.3 Snapshot carrier 同时包含历史和继续执行指令

`compaction/contracts.py::CompactionSnapshotCarrier` 实际包含：

```text
continuation.mode / instruction / active_request
earlier_context_summary
recent_user_messages
```

active_request 又包含 entry_id、entry_sequence、location、text。
`compaction/planner.py::freeze_compaction_continuation` 对 active installation 生成
`RESUME_ACTIVE_TURN`，对 idle compaction 生成 `AWAIT_NEXT_USER`。
当 location 为 `SNAPSHOT_EXACT` 时，原长程目标可能只剩在 active_request.text 中；
为 `CANONICAL_SUFFIX` 时，正文在保留窗口，不在 carrier 内重复存一份。
`model_input/lowering.py` 当前还明确要求模型遵循该 carrier 的 continuation 指令。

因此不能“复制完整摘要 carrier”。第 7.3.1 节已固定 exact hard cut：carrier contract 增加
`FrozenRetainedHistoricalRequest | None`，其 kind/origin/text、canonical JSON、size 与
lowering 均为 closed contract；child continuation 固定为 `AWAIT_NEXT_USER`，active request 为空；
仅当源 request 为 `SNAPSHOT_EXACT` 时，把精确 text 与从源 initial entry 核验出的 kind/origin
移入该历史槽。`CANONICAL_SUFFIX` request 已存在于 copied entry，不重复。

这是机械的历史/运行控制分离，不调用 LLM 改写摘要，不根据摘要里“未完成”“继续”等词猜测。
不能简单清空唯一原文，不能保留旧 ID 作为 child 当前请求，也不能把非 human 的 plan、terminal
observation 或 inter-agent request 无类型地降成 recent user message。builder、parser、lowering、
digest 与普通 compaction 测试必须在同一 contract hard cut 更新；复制函数不得私自拼 prompt。

### 12.4 “复制 snapshot”不等于“下一轮会采用 snapshot”

`_repository/kernel.py::_insert_initial_context_binding_revision` 的 revision-zero 初始化，
当前会通过旧 turns 的 initial entry 顺序寻找同 scope 的上一 turn，并继承其 current snapshot。
找不到 predecessor 时会选择 FULL_HISTORY。sessions 当前没有独立的初始历史基底字段。

所以，“创建 child + 插入 snapshot + 等待普通提交”不是一条已存在的可用路径。
第 7.3 节因此选定显式 `session_context_genesis`。它只在没有本地 execution predecessor 时
给 revision zero 提供 FULL_HISTORY/SNAPSHOT 基底；本地 turn 产生之后仍遵循正常最新基底规则，
不保留两套竞争的 current-context 真源。anchor 与冻结 model binding 也由这条 child-local 行持有，
从而支持 anchor-only Fork-of-fork，而无需保存 parent lineage。

不得创建一个假的已执行 turn、假的用户消息或假的 compaction event 来触发继承。
snapshot ID、覆盖序号、carrier bytes/content digest 与 source digest 都按 child 坐标重建。
父 source digest 的 lineage fingerprint 包含父 scope/binding，不能照抄；Fork-genesis builder
只对第 7.3 节列出的完整本地值建立跨重启确认，不新增 registry，也不能声称 child 自己执行了压缩。

### 12.5 复制历史与现有 execution graph 的耦合必须正面拆开

当前 clean-v0 有以下具体限制：

1. turns.initial_entry_id 必须指向同 turn/scope 的指定 request-like entry；
   constraint trigger 不允许用工具组的起始助手消息替代它。
2. turns 同时要求权限快照，计划权限还关联原 workflow；不是纯粹的历史消息分组。
3. tool_results 的 PHYSICAL_ATTEMPT 成功结果要求非空 attempt_id；POLICY_NO_ATTEMPT
   只允许拒绝/取消等状态，不能拿来存“复制来的成功结果”。PLAN_CONTROL 又要求计划关联。
4. ROOT INTER_AGENT_MESSAGE 和 PLAN_CONTINUATION 的来源字段、FK、decoder 需要原任务/计划信息。
   不能只把 source ID 设为 NULL 就声称保留原 entry kind。

这解释了为什么直接 INSERT SELECT 或复制几张表仍不够。
第 7.2、7.2.1 节已选择并闭合
`imported_history_groups + transcript entry owner union + frozen imported source columns`：真实 `turns` 与其权限/
执行约束不放松；imported group 可以没有完整 initial row，且不能成为任何执行 API target。
成功或失败结果使用正交的 `result_record_kind=IMPORTED_HISTORY`，不依赖 child attempt，同时保留
原 `result_origin_kind`；plan/subagent 来源保留为 frozen attribution，不连接 live object；
唯一 imported anchor 通过 session genesis 获得 Fork 基底。

不采用 critic 清单中的另一个候选——把所有历史/执行 turn 统一后再拆 1:1 execution state——
因为这会迫使所有现有 turn 查询跨表改写，远大于当前产品缺口。也不复用同名 `source_*` 而不设
owner 分支：实施必须让 frozen attribution 与 live FK 在 DDL/decoder 中构成明确联合，防止误查旧 ID。

### 12.6 历史工具结果不是按 entry 顺序简单拼出来的

`reader.py::_load_tool_state` 读取 attempts、results 和 plan interaction；随后结合
`_next_assistant_cuts`、`visible_tool_result_at_cut` 判断某个结果在历史 provider cut 当时
是否可见，生成 tool closure 和必要的 late outcome observation。

复制时即使已能读到所有最终结果，也不能将原来的 closure 替换成一个提前出现的成功结果。
既不重复执行，也不重新解释历史。新编号映射必须保持这些相对边界关系，
而不只是把目前展示出来的工具 JSON 粘到对应工具卡上。

把 `IMPORTED_HISTORY` 直接塞进 `result_origin_kind` 也不正确：当前 `_tool_result_metadata` 用原
origin 区分物理观测和 policy/plan 的无物理 attempt，覆盖它会丢历史语义。本文因此采用正交
record kind，并保留原 result origin。与此同时，当前 closure kind 根据 `attempt_id` 或 plan
interaction 推导；child 不复制它们时，“可能已部分执行”会被错误降成“派发前中断”。第 7.4 节增加
`imported_tool_call_closures`，只保存无法从 result/cut 重建的三种 frozen closure 与 target cut。
critic 提议为可见结果也保存 `RESULT_VISIBLE` settlement；本文不采纳，因为可见性已由 result entry
sequence 与 cut 完整决定，再存一行只是证明另一机制工作。late outcome 同理继续派生，不持久化。

`assistant_message_blocks` 的 TEXT/DATA/TOOL_CALL 顺序才是 compiler 语义来源；
assistant parent body 是 storage manifest，不能只复制前端拼好的正文。

### 12.7 Artifact、replay 与 ID 的边界不同

`tool_artifacts.py::PostgresToolArtifactReadPort` 以当前 session/workspace + artifact ID 查询；
schema 同时有全局唯一的 output_artifact_id 索引。仅复制 result 的原 artifact ID 会冲突，
仅复制显示用 HEAD_TAIL 文本又会让原来的按需读取失效。

第 7.4、9.2 节已选定 session-scoped handle并列出全 consumer 审计：唯一索引 hard cut 为
`UNIQUE(session_id, output_artifact_id)`，imported result 保留原 ID、preview 原字节、coverage/
disposition 并直接引用同 workspace immutable blob。仅保留 copied results 实际引用的 blob 关系，
不导入父会话全部 artifact 库。native result 的现有 ID builder 不变；imported record 是唯一不要求
由新 result entry ID 反推 handle 的分支。不得增加跨 session alias registry 或重写文本中的 ID。

`llm/provider_replay.py` 的 replay identity 和 fragment metadata 包含 session/assistant entry 身份；
`model_input/provider_replay.py` 的 manifest/hydration 也执行这些精确关联。
child 要通过现有 builder 重建相应 metadata，不照抄父 fragment fingerprint。
opaque payload 保持原样；原 replay target 不改写成新模型，否则会错误声明兼容。
这些是现有真实边界的重建，不授权新增 Fork DTO fingerprint 或 provider-specific profile。

### 12.8 会话创建不能复用成“先发布空会话、再补历史”

`web_app/session_controller.py::create_session` 调用 core.open_session 后发布 handle；
`host.py::_open_admitted` 则在 acquire_host_writer 之后建立 Host、启动 MCP、注册会话。
这条路径不是一个可任意填充历史后回滚的纯 SQL 工厂。

Fork 应在现有 repository/Host owner 间提供“先完成 canonical child 创建，再按普通生命周期打开”
的接线，不在前端先创建空会话再连续发送历史消息。源会话只读，不 acquire 其 writer 来抢占正在运行的父任务。

普通新会话原有的 capability discovery 与 startup hook 是否运行，继续按现行生命周期契约；
“不继承副作用”的含义是不回放源 turn/tool/approval，不是保证新会话打开完全不进行网络或 startup 操作。
复制事务内不运行网络调用或 hook。打开失败时保留已提交的历史，并按普通 resume 路径重试打开，
不重新执行创建，不保留一套 Fork 专用后台恢复任务。

这要求 controller/API 不再用单一 success/error 覆盖两个阶段，而采用第 7.5 节的
`CREATED_AND_OPENED | CREATED_OPEN_DEFERRED | NOT_CREATED`。child ID 在请求前确定；响应丢失后
按该 ID 查询 session row，而不是在一批 ID-derived 标题中猜测或盲重发。当前 session summary
标题只是 ID 投影，不存在可复用 rename；列表查询又直接优先最后 entry 的 accepted time，
所以本轮不扩展 title，并把 user-visible activity 改为
`GREATEST(created_at, COALESCE(last_entry.accepted_at, created_at))`。

### 12.9 实施准备度结论

已确认能复用：最终 entry 的历史 binding、不可变内容读取、retained window 与 closure/late 推导规则、
通用 replay、单 session 分页、blob GC 和现有会话运行生命周期。

必须补足的具体接线/表示：

- terminal-final 派生投影与以其为锚的只读历史 extraction；
- 第 7.2 节 imported group/entry owner 联合及普通 reader 解码；
- 第 7.3 节 settled carrier、genesis digest 与 revision-zero 采用；
- 第 7.4 节 frozen closure、artifact/replay/sequence 本地关联；
- 第 7.5、8 节原子创建结果、Host 打开、terminal v3 与 GUI 入口。

因此仍推荐有效上下文复制，但不能称为“只加按钮或直接复用全部现有 reader 即可”。
它省掉的是祖先共享体系，不是全部 schema 工作。本文现已选定实现表示，可以进入代码实施；
“准备就绪”只表示设计不再保留上述产品分岔，不表示代码或 Fork 测试已经存在。

实施必须先用最小数据样例锁住 entry owner、genesis、carrier、closure 和秩映射，再贯通 terminal/API；
不得把已经关闭的选择重新交给实现者用 fake execution rows、广泛 nullable、旧基底复制、
兼容双写或通用 fallback 临场绕过。真实 dogfood 仍是激活条件，不得由静态审阅替代。

## 13. 2026-09-08 实施记录

### 13.1 唯一落地路径

`fork_history.py` 在 Fork writer 提供的同一个 REPEATABLE READ connection 内冻结材料，
`_repository/fork.py` 是唯一 imported writer。所有 ID/cut 重新绑定到 child，genesis 最后写入，
deferred constraints 在 commit 前集中验证。canonical 成功后再普通 resume；失败不重做复制。

clean-v0 新增且仅新增 `imported_history_groups`、`session_context_genesis`、
`imported_tool_call_closures`。当前 oracle 为 **29 committed / 24 live / 11 subjects /
1 append guard / 28 product relations / 0 durable jobs**。catalog、runtime grants、terminal
protobuf/schema contract、架构测试和 README 同步；没有在线迁移或祖先关系。

carrier 唯一版本为 `pulsara.context-snapshot-carrier.v3-retained-history`，provider lowering
为 `pulsara.provider-message-lowering.prefix-continuity.v8-retained-history`。有序 typed 列表的 producer、parser、
planner、Tier-3 omission 保留和再分叉追加同时切换；空列表与旧单值协议没有兼容双读。
列表测试包含相同文字的两个不同请求，确保没有靠去重抹掉历史。

### 13.2 生产 consumer 查询清单与 owner-routing

使用以下查询与 Python AST 的函数归属扫描核对实际 SQL；动态 helper 的调用者及模块级 SQL
也人工纳入，没有将“没有直接出现表名”等同于无需审计：

```sh
rg -n 'transcript_entries|tool_results|assistant_message_blocks' src/pulsara_agent \
  --glob '*.py' --glob '!**/generated*/**'
```

`E` 表示真实 executed-only 的执行/确认，`H` 表示两类 owner 的历史读取，`F` 表示唯一 Fork 写边界。

| 文件 | 实际查询/调用入口与分类 |
| --- | --- |
| `_repository/conversation.py` | E：`confirm_root_turn_intent`、`confirm_terminal_observation_winner`、`_require_compaction_target`、`confirm_assistant_message_winner`、`query_command` 的执行 target exact-join；H：`rehydrate_session` 的只读历史投影 |
| `_repository/kernel.py` | E：`_require_provider_safe_turn_in_transaction`、`_resolve_event_turn_id`、`_insert_entry`、`_insert_assistant_block`、`_accepted_entry`；`_insert_initial_context_binding_revision` / `_initial_context_binding_revision_matches` 仅为真实新 turn 消费 child genesis |
| `_repository/prompts.py` | E：`confirm_prepared_prompt_head_consumption`、`confirm_prepared_prompt_steer`；队列 consumed entry、steer target 不接受 imported owner |
| `_repository/tools.py` | E：`accept_tool_capability_decision`、`accept_tool_attempt`、`accept_tool_result`、`confirm_tool_result_winner`、`accept_tool_interaction_decision`；全部 native result 明确 `EXECUTED` |
| `_repository/plans.py` | E：`accept_plan_tool_batch`、`_accept_rejected_plan_tool_batch_in_transaction`、`resolve_plan_question`、`resolve_plan_draft_review`、`inspect_plan_continuation`、`_plan_interaction_content_row`、`_confirm_plan_tool_batch_in_transaction`、`_confirm_plan_resolution_in_transaction`、`_eligible_plan_handoff` |
| `_repository/subagents.py` | E：`accept_subagent_task_batch` / `confirm_subagent_task_batch`、`accept_explicit_subagent_result` / `confirm_explicit_subagent_result`、`confirm_subagent_turn_admission`、`query_subagent_task`、`list_subagent_tasks`、`accept_inter_agent_mailbox_batch` / `confirm_inter_agent_mailbox_batch` |
| `_repository/completions.py` | E：`accept_subagent_completion_into_root`、`_accepted_completion_row`；imported frozen source ID 不连接新 live task |
| `_repository/memory.py` | E：`claim_memory_candidate_for_governance`、`_read_memory_governance_terminal_candidate` / `_read_memory_governance_terminal_suffix`；`_read_memory_governance_tool_evidence` / `_read_entry_public_body` / `_read_assistant_public_blocks` 读取由真实 candidate/window 选出的证据，不产生 imported occurrence |
| `_repository/memory_management.py` | H：`_PROJECTS` 的会话 activity；E：`memory_management_detail` 的 candidate provenance exact-join |
| `memory/recall.py` | E：`provenance` 通过真实 memory candidate 取得 source，不把 imported entry 再生为记忆 |
| `reader.py` | H：`read_frozen_dispatch`、block metadata/payload、entry payload、`_load_tool_state`；按 owner 分支读 frozen attribution/closure。E：headroom 的当前 turn、`read_memory_governance_historical_snapshot` 的候选来源、`_plan_handoff_compile_facts`、`_load_round7_scope_facts` 的执行前驱/attempt/outcome |
| `query.py` | H：`_page_entries_on_connection`、`inspect`；单 session 本地序列，不解析祖先 |
| `fork_history.py` | H：`read_fork_anchor` 对 executed final 或唯一 genesis anchor 核验；`read_fork_historical_material` 读取该 anchor 的有效本地历史；RR + keyset 固定 upper bound |
| `_repository/fork.py` | F：`fork_conversation` / 本模块私有 `_insert`；imported group/entry/result/closure 与 genesis 同事务发布，无 runtime invoke |
| `host.py` | H：`_list_resumable_session_rows`、`_list_resumable_session_rows_across_workspaces`、`_read_resumable_session_row` 的 activity；`fork_conversation` 复用 admission/shutdown settlement，只连接 repository F 与普通 resume |
| `blob.py` | H：`delete_orphans` 检查任一 session 的 canonical 引用；父子 result 任一存在即保留 blob |
| `tool_artifacts.py` | H：`_fetch` 按 session + workspace + artifact handle exact-join；native/ imported 共用 reader，不新增 alias |
| `terminal_protocol/canonical_v3.py` | H：`snapshot`、`history_page`、`observe_committed`、`resolve_content_reference`、`_resolve_reasoning_content`、共用 `_entry`；`_control` 的初始 base 读 genesis，其 attempt/task/plan/event targets 仍 E |
| `model_input/compiler.py`、`ports/artifact.py` | 不直接查询数据库；消费上述 frozen 历史和 scoped artifact 接口，不新增 execution authority |
| `web_app/session_controller.py` / `http_server.py`、Web adapter | 消费 canonical eligibility、scoped session query 与三类结果；断线不取消已接纳 owner，shutdown 等待 settlement，不从展示状态推导身份 |
| `storage/migrations/manifest.py` / baseline / grants | 关系与约束的唯一 schema 声明；不是新增的业务 consumer 或 registry |

imported entry 的 execution-target 拒绝同时落在 repository 和数据库约束：turn initial/final、
event/command target、queue consumed entry、tool attempt、plan interaction、memory candidate
均不得指向 imported owner。历史结果与调用保持同 group；旧真实 tool result 的权限/attempt/plan
引用不复制为 child 权威。纯 frozen Plan/terminal/subagent decoder 可读历史，不恢复对应 owner。

### 13.3 验证与已知边界

测试、真实 provider 的完整输入输出、首次失败的夹具诊断以及桌面/窄屏截图见
[验收记录](dogfood_evidence/conversation_fork/README.zh.md)。

- Fork + compaction focused：`CI=1 .venv/bin/python -m pytest -q tests/test_conversation_fork.py tests/test_round5b_long_horizon_context_compaction.py`，92 passed。
- 完整回归：`CI=1 .venv/bin/python -m pytest -q`，**1681 passed / 0 skipped / 19 warnings**，431.18 秒；warnings 为已有 aiohttp shutdown timeout 弃用提示。
- continuity：`CI=1 .venv/bin/python -m pytest -q tests/test_round3_1_provider_input_prefix_continuity.py`，10 passed。
- 前端：128 passed，TypeScript、修改文件 ESLint、local build 通过。
- protocol generator `--check`、`uv lock --check`、Ruff 修改文件检查、`git diff --check` 通过。
- 独立 wheel + 非源码目录 launcher + packaged schema/protobuf/carrier/static 验证通过。
- 真实 provider：8 次调用，三类分叉全部通过；不把静态/fixture 验证称为真实模型验证。

完整回归中的 64-call long-horizon 夹具原先使用裸 Runner 却没有 Host 的 30 秒 writer lease 续期；
并行负载下出现 `StaleHostWriter`，同一测试单跑通过。夹具现在在每次 model preflight 前调用现有
`renew_host_writer`，保留全部 64-call 断言，没有拉长 turn lifetime、加 skip 或改变产品边界。

未改动的 memory-view 全量 lint 两项错误和大 chunk 警告保留并记录。Fork 不隔离实际文件或当前
advisory memory；不复制父执行图、不保证被原摘要省略的全文信息。旧开发数据库仍需要现有
reset-only 流程重建；本次只使用临时数据库验收，没有重置用户配置的数据库。
