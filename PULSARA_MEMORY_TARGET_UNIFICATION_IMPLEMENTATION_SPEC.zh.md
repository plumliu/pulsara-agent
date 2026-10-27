# Memory 工具目标统一实施规格

日期：2026-10-28。状态：生产 hard cut 已实施，公开请求、结果及消费者采用本规格。验证与范围见[实施报告](output/target-hardcut-implementation-20261028/memory/REPORT.zh.md)；整体结果见[汇总](output/target-hardcut-implementation-20261028/root/REPORT.zh.md)。

## 1. 决策与范围

四个工具保留各自用途。`memory_get` 接受一个 MEMORY target；`memory_search` 保持查询参数，不增加 target；`remember` 创建新记忆，没有预先存在的主对象 target，但其依赖改为 `based_on` 中的 MEMORY targets；`mark_memory_relation` 使用 `source` 与 `target` 两个有明确角色的 MEMORY targets。

统一的是模型选定操作对象的接口，以及结果中可复制的对象身份。不是所有 ID、范围或 `target` 字样的统一命名工程。保留 `remember.context_target`：它选择新记忆的 GLOBAL/CURRENT_PROJECT 可见范围，不是待操作记忆的身份。保留 `kind` 作为记忆分类，`target.kind=MEMORY` 只是对象类型。

不新增工具、动作、关系类型、持久对象、事件、job、guard、注册表或散列。原生 DTO、数据库列、canonical ID、semantic digest、relation ID 与作用域 owner 不改。Memory 用户页的原生 fact_id/关系 DTO 与删除确认不改。明确排除 subagent 工具链、读写可见性与历史/记忆暴露策略；仅沿当前 descriptor 读写现有目标，不借本次重构扩大或缩小调用者范围。

### SUPERSEDES

本规格实施时，只替代[归档 Memory/session 工具清晰化规格](archived_docs/PULSARA_MEMORY_SESSION_TOOL_CLARITY_IMPLEMENTATION_SPEC.zh.md)中四个 memory 工具的裸 ID 请求、对应可操作结果身份及描述；其信息来源、分类、范围、advisory 和写入语义保留。session 工具及历史召回的 SESSION/SESSION_ENTRY targets 由独立 session 规格负责。

只在新 cold epoch 或明确采用的 compaction successor 安装新工具定义。运行中 epoch 的 SYSTEM/tools byte-identical、messages append-only。历史已提交请求/结果不回写、不添加双读 alias。新请求不接受旧字段。它不是新旧参数自动协商，也不增加 provider-input 重建边界。

## 2. 当前实现证据与所有权

本轮先读共同 BRIEF、根 AGENTS、capability target 规格、归档 capability/scheduling clarity 规格与 190 次 Responses 实验报告。归档 memory 文档用于理解既有取舍；以下生产代码是实际行为证据。

| Owner | 已核对的行为与实施触点 |
|---|---|
| `src/pulsara_agent/capability/builtin_catalog.py:206–299,1565–1624` | 四工具 schema/description；现行闭合普通 object，无 action union。记忆查询与变更独立 binding、权限分类不变。 |
| `conversation_kernel/tool_runtime.py:936–973,1854–1886,3352` | 已安装目录可见范围、frozen memory opt-out、canonical schema preflight、调用 memory owner。形状不合法在 admission 前为 INVALID_ARGUMENTS；已 admission 的业务拒绝不是同一状态。 |
| `conversation_kernel/memory_tools.py:246–347,448–633` | session/owner/policy 核验；写入解析、basis 逐条读取、搜索、point-read、结果与曝光 ID 集合。 |
| `conversation_kernel/memory/writes.py` | statement normalization/字节上限、basis 数量与顺序/去重、两个不同关系端点；prepared mutation 是现有 process-local 值。 |
| `conversation_kernel/_repository/memory.py:90–516` | tool-call exact join、basis 锁与当前性复核、SAVED/ALREADY_PRESENT、关系对称规范化、supersede 与 ToolResult 同事务提交。 |
| `conversation_kernel/_repository/tools.py:868–934` | 已提交记忆结果的 shape/identity/exposure/检索切面确认；不要求记忆现在仍存在。只改公开解析会在这里破坏确认。 |
| `conversation_kernel/memory/recall.py:266–298,494–605` | get 允许当前可读范围的 ACTIVE 或 SUPERSEDED item；关系读取只返回两端都可读的直接关系；搜索为 bounded recall，不是列表分页。 |
| `memory/scope.py:31–72`、`memory/contracts.py:99–111,162–168` | Host 的 domain/可读 context；project 为 GLOBAL+本项目，transient 只有 GLOBAL；basis 层级与 response-preference 输出。 |
| `conversation_kernel/memory_tools.py:800–859,1097–1197`、`memory/recall.py:300–363` | response-preference、自动召回 full/compact/reference 的 provider 可见身份；投影字节预算与降级必须用新正文真实计算。 |
| `conversation_kernel/memory/management.py:140–190`、`_repository/memory_management.py:476–520` | 用户删除只沿 incoming BASED_ON 传播；SUPERSEDES 参与恢复条件，CONTRADICTS 不传播删除；不受模型目标包装影响。 |
| `primitives/tool_result_projection.py:244–281,417–436`、`model_input/lowering.py:696` | 既有 `model_visible_memory_ids` exposure carrier 与 typed ID 集合。该角色保持，见第 6 节。 |
| `ports/system_prompt.py`、`memory/hints.py`、`bundled_skills/**/*.md` | SYSTEM/hint 无本次旧 ID 调用签名；bundled Skill 未发现 memory_get/based_on_memory_ids/source_memory_id/target_memory_id 调用例。无需凭空新建 memory Skill 或扩大 SYSTEM。 |
| `frontend/lib/builtin-tool-summary.ts`、`frontend/lib/memory-api.ts`、`frontend/components/memory-view.tsx` | 工具摘要依赖 query、relation_kind、status/数量，不依赖被移动的对象 ID；Memory 页是用户级独立服务，保留其原生 DTO，不接入 active session。 |

表内短路径统一位于 `src/pulsara_agent/`；行号定位本次调研快照，实施时按符号定位。适配最小化：公开 target 解包为现有 ID，现有 query/repository 执行资格、事务与关系算法。没有替换 SDK、数据库、检索或调度机制的必要。

## 3. 精确请求合同

唯一 MEMORY target 为闭合对象：

```json
{"kind":"MEMORY","memory_id":"memory:aaaaaaaaaaaaaaaaaaaaaaaaaa"}
```

`kind` 与 `memory_id` 必填、不可 null；无 scope/context/domain/lifecycle/版本字段。示例 ID 只说明形状，不代表存在的记录。ID 是原 canonical `memory:` ID，不能裁短、trim、解析后猜其他对象。schema 保留原 `memory_id` 的 string/minLength=1；业务解析继续 `require_id(value,"memory")`，不能把 schema 可接受性当作存在性、范围或授权证明。

不构建包含所有 target kinds 的通用 union。三个接受 MEMORY 身份的位置内联同一小形状即可。实施可用局部纯 helper 生成形状/解析精确对象并返回原 ID，不建设 target 注册表或跨领域 dispatch。canonical 与 wire 都闭合；沿当前 lowerer、strict:false；不新增 `$ref/$defs`、request 包装或 provider 特判。

| 工具/分支 | 请求 | 默认/省略/null 与实际行为 |
|---|---|---|
| `memory_search` 不指定 kind | `query`；可选 `limit` | query 非空白，strip 后 1..32768 UTF-8 bytes；limit 省略 5，1..50。没有 target/范围/cursor/offset。 |
| `memory_search` 指定 kind | 上述加 `kind` 四值之一 | kind 是偏好；exact 不足可扩到其他 kind，并返回 filter_match/retrieval_summary。不能改成硬过滤。kind/limit null 非法。 |
| `memory_get` | `target: MEMORY` | 无默认对象、分页或 nullable 身份。返回主体、kind/context/time/lifecycle、直接关系和可选 user_edit。不存在/不可读均沿原 memory not found。 |
| `remember` GLOBAL | `statement,context_target:"GLOBAL",kind`；可选 `based_on:[MEMORY,…]` | based_on 省略或 `[]` 为无依赖，null 非法；最多 8、不能重复、保持顺序。GLOBAL basis 只能来自 GLOBAL。 |
| `remember` CURRENT_PROJECT | 同上，context_target 为 CURRENT_PROJECT | 只在当前 project Host 可写；basis 可来自 GLOBAL 或本项目，不能来自其他项目。transient 拒绝 CURRENT_PROJECT。 |
| `remember` 四 kind | USER_PROFILE / RESPONSE_PREFERENCE / FACT / DECISION | 两范围均允许四 kind；statement NFC/换行归一化/strip；通常 1..8192 UTF-8 bytes，RESPONSE_PREFERENCE 1..2048。本次不改分类或来源标准。 |
| `mark_memory_relation` CONTRADICTS | `source:MEMORY,target:MEMORY,relation_kind:"CONTRADICTS"` | 两个不同、同 context、同 kind 的 active 记忆；关系对称，交换入参仍命中同一关系。 |
| `mark_memory_relation` SUPERSEDES | `source:MEMORY,target:MEMORY,relation_kind:"SUPERSEDES"` | source 是受支持替代，target 变 SUPERSEDED；同 kind 为 replacement，不同 kind 为现有 taxonomy correction；不删除 target。 |

所有未列字段拒绝。四工具的可选字段均不能以 null 替代省略；保持原公开 schema 行为。旧 `_string_sequence(None)` 的内部容错不构成公开 null 合同，不在新实现中保留已删除裸 ID 参数的隐藏入口。BASED_ON 只能通过 remember 创建依赖，不能借 mark_memory_relation 新增任意依赖边。

`source` 与 `target` 是关系角色，不能统一成无方向的 targets 数组；`based_on` 是 basis 角色列表，不能误命名为“待修改目标”。已知完整 target 可直接调用 memory_get，不增加必须先 search 的门槛；关系描述仍保留既有 inspected/same subject/conditions 要求，不引入新的 durable inspection receipt。

### 3.1 前后示例

以下 M1/M2 是完整 canonical memory ID 的示意，机器可检验示例使用真实格式的离线派生 ID，见附件。

```text
memory_get before: {"memory_id":"M1"}
memory_get after:  {"target":{"kind":"MEMORY","memory_id":"M1"}}

memory_search before/after:
{"query":"当前项目的发布决定","kind":"DECISION","limit":5}

remember before:
{"statement":"根据既有兼容性约束，项目采用方案 A。","context_target":"CURRENT_PROJECT","kind":"DECISION","based_on_memory_ids":["M1"]}
remember after:
{"statement":"根据既有兼容性约束，项目采用方案 A。","context_target":"CURRENT_PROJECT","kind":"DECISION","based_on":[{"kind":"MEMORY","memory_id":"M1"}]}

remember independent before/after:
{"statement":"用户倾向先看简短结论。","context_target":"GLOBAL","kind":"RESPONSE_PREFERENCE"}

mark_memory_relation before:
{"source_memory_id":"M2","target_memory_id":"M1","relation_kind":"SUPERSEDES"}
mark_memory_relation after:
{"source":{"kind":"MEMORY","memory_id":"M2"},"target":{"kind":"MEMORY","memory_id":"M1"},"relation_kind":"SUPERSEDES"}

mark_memory_relation CONTRADICTS after:
{"source":{"kind":"MEMORY","memory_id":"M1"},"target":{"kind":"MEMORY","memory_id":"M2"},"relation_kind":"CONTRADICTS"}
```

## 4. 必须保持的业务语义与错误边界

### 4.1 来源、范围、写入与去重

记忆保持 advisory。允许来源忠实的轻量推断与归属明确的工具观察；不要求自述、重复或高置信度。保留主体、时间、条件、否定、不确定性，以及建议/暂定计划/已采纳/已完成的区分。自然语言 statement 不变成原始工具输出、可执行程序、凭据或权限覆盖。target 不是引用/证据真实性声明，也不恢复已移除的 creation source、ToolResult citation 或 source-audit 工具。

scope 决定可见范围，不扩大命题的事实范围。不给 target 加 context_target 以允许调用者选择/伪造记忆归属；所有读取仍使用冻结 read_binding。写入时当前 project 再由 repository owner 验证；relation 两端均从同 memory_domain 读取且属于 GLOBAL 或当前 project。用户写入/全部使用 opt-out 仍由 frozen policy 先授权，owner 做原有再检查。

remember prepare 只形成 PreparedRememberWrite。canonical ToolResult acceptance 同事务决定事实与 BASED_ON 边；发布失败时回滚，不能先写记忆再返回成功。expected target 的公共结构要参与现有 tool-call exact join，但不另增 durable receipt。statement/kind/context 与依赖顺序须与冻结 mutation 精确一致。

SAVED 返回新 memory target；ALREADY_PRESENT 返回同 domain/context/kind+规范化 statement 的现有 active target，不重写 origin 或 dependencies。basis 在唯一性仲裁前锁定并复核当前 ACTIVE/范围，即使 statement 已存在，stale basis 也仍拒绝，不能把“去重命中”改成忽略错误依赖。related recall 仍是 best-effort、同 context、至多三个候选，不是已建立关系，检索失败不能否定合法写入。

### 4.2 关系与重试

新增关系要求两端不同、存在、当前允许、同 context、ACTIVE。CONTRADICTS 额外同 kind；SUPERSEDES 允许改 kind 的 taxonomy correction。CONTRADICTS 由原 owner 按端点排序规范化，所以结果 source/target 顺序可能与调用相反；不能为了“原样回显”改变 canonical 关系身份。SUPERSEDES 保持方向，source 不因时间较新就自动成为 replacement。

已有关系返回 ALREADY_PRESENT。特别是再次标记已存在 SUPERSEDES 时，target 已是 SUPERSEDED 仍应成功返回既有关系；不得把“新增关系两端 ACTIVE”误实现成所有重试都先拒绝 inactive。当前 repository 先检查 existing relation，再检查新关系 active 前提，此顺序保留。无关重复/失败关系调用不撤回先前已保存记忆。

### 4.3 读取、检索和删除

memory_get 可读取可见的 SUPERSEDED item，不收紧成 search 的 active-only。直接关系目前最多 100 条且没有公开分页参数，本次保留该现有 bounded read，不新增“已返回全图”的承诺或总图上限。user_edit.note 保留“正文已编辑，既有关系未重新评估”的实际提示。

memory_search 保留 lexical admission 先于可选远程 embedding/rerank、稀疏回退、kind relaxation、rerank 候选池与 limit 分离、关系 enrichment 可用性。返回 0 条不证明无记忆；完整性仍由 retrieval_summary 报告。没有 query target 或任意项目过滤入口，也不为目标统一添加分页/游标。

参数形状错在现有 schema preflight 拒绝；进入 owner 后的错误 canonical ID、范围不符、坏 basis、statement 业务字节边界等沿现有 APPLICATION_ERROR。owner 已关闭/会话不匹配仍 SYSTEM_ERROR；memory not found 仍 APPLICATION_ERROR。取消、超时、可选远程失败和 repository 冲突沿原 owner，不将所有异常吞成 not found。

删除仍从用户 Memory 页执行。BASED_ON 的 source 是依赖项、target 是依据；删依据递归删除依赖，删依赖不反删依据。CONTRADICTS 不传播删除。SUPERSEDES 边参与删除后的候选恢复、存活祖先和冲突/容量检查，不能简化为删除 source 就无条件恢复 target。删除不新增模型工具；用户页与 active session 保持独立。

## 5. 可复制结果与自动召回

一个 target 可以原样复制到 memory_get.target、mark_memory_relation.source/target 或 remember.based_on 的单个元素。仅替换对象身份槽，不把整条结果嵌套为 target，也不额外保留旧裸 ID 镜像。

| 生产位置 | 新公开身份 | 保留内容 |
|---|---|---|
| memory_search.memories[] | `memory_id` → `target:MEMORY` | statement、记忆 kind、context_product_label/current_context、recorded_at、filter_match |
| memory_get 主体 | `memory_id` → `target:MEMORY` | lifecycle、正文、时间、advisory、stale/incomplete、user_edit |
| memory_get.relations[] | `source_memory_id/target_memory_id` → `source/target:MEMORY` | relation_id、kind（原结果字段名）、supersede_mode；不增加 MEMORY_RELATION target |
| remember SAVED/ALREADY_PRESENT | 主体 `memory_id` → `target:MEMORY`；related_memories[] 同样替换 | status、原 context_id/记忆 kind/正文/时间、retrieval_summary、relationship_labels_not_inferred；内部 related DTO 保持 |
| mark_memory_relation SAVED/ALREADY_PRESENT | `source_memory_id/target_memory_id` → `source/target:MEMORY` | relation_id、relation_kind、status、advisory；CONTRADICTS 按原 canonical 顺序 |
| search/自动召回 relation_warnings[] | `memory_id/other_memory_id` → `source/target:MEMORY` | `kind:"ACTIVE_CONTRADICTION"`，不新增有向矛盾含义 |
| 自动召回 full/compact MEMORY items | `memory_id` → `target:MEMORY` | `source:"memory"` 是来源类别标签，保持为字符串；记忆 kind、scope、statement、recorded_at |
| 自动召回 reference variant | `memory_refs:["M1",…]` → `memory_refs:[MEMORY target,…]` | `read_memory_with:"memory_get"`、遗漏数量与关系警告 |
| response-preference items | `memory_id` → `target:MEMORY` | scope、statement、recorded_at、warnings、选取/冲突省略规则 |

关系结果中的 `source` 是端点角色；混合自动召回 item 的 `source:"memory"` 是分类标记，两者位于不同结果对象，不机械替换。session history 的可复制身份与 omitted_conversations 由 session 组处理；不得用 MEMORY target 包装 SESSION_ENTRY。

例如 search 的一项可直接用于 get，并用同一对象作为新事实的 basis：

```json
{"target":{"kind":"MEMORY","memory_id":"memory:aaaaaaaaaaaaaaaaaaaaaaaaaa"},"kind":"FACT","statement":"项目的当前兼容性约束。","filter_match":"EXACT"}
```

这是结果片段，未显示的原有时间/范围/检索字段不能因示例省略而删除。出错且没有已确认记忆的结果不制造空 target。相关候选或 old/inactive target 只表示身份，不表示当前可写、可作为 basis 或已经验证关系。

自动召回仍用当前 full→compact→reference 降级机制，不能为 target 包装增加新 variant 或无限 token reserve。response-preference 16 KiB 和自动 source 预算要基于真实新序列化字节重新检查；原 bounds、选取和短缺行为保持，允许同一预算下较大的身份包装导致更早走原有降级路径。不能沿用旧裸 ID 的估价而低报体积，也不能为追回同样显示条数任意放宽预算。

## 6. 消费者与 hard-cut 实施清单

1. 在 builtin_catalog 改 get、remember、relation 的闭合 schema 与四 description；search schema 原样。局部 target helper 接受精确 `{kind,memory_id}`、无额外字段/nullable，并调用既有 require_id。不要把 candidate 脚本作为生产导入依赖。
2. memory_tools 读取新位置后仍构造原生 PreparedRememberWrite/PreparedMemoryRelationWrite；所有原有 byte/scope/duplicate 检查保留。删除旧字段读取和无用 `_string_sequence`（若无其他使用），不增加 fallback。不要把请求先展平回旧 parser。
3. `_repository/memory.py` 的 accepted tool-call exact join 要比较新公开 `based_on`/source/target 的完整形状与原 mutation ID 序列；移除旧字段允许集。结果主身份、related、关系端点同步生产新对象。DB SQL、锁、derive_id、唯一性与提交边界保持。
4. `_repository/tools.py::_confirm_direct_memory_result_shape` 改从闭合 target 取 ID，维持 SAVED 派生 ID、related 数量、canonical 端点顺序、visible_ids 与 retrieval_summary 完整检查。确认已提交结果时不重查当前 memory row；后续用户编辑/删除不能使原确认失败。
5. memory_tools 的 search/get/automatic recall，contracts 的 response-preference renderer 与 recall 的预算估算共用同一新呈现形状，覆盖 full/compact/reference。`memory_fact_ids`、semantic membership/order/warning IDs 等内部 typed 元数据不重命名。
6. **保留既有 `pulsara_tool_result.model_visible_memory_ids` exposure 元数据和 artifact_read 的对应暴露集合。** 它是跟踪模型已见记忆的既有独立角色，不能机械删除或换成新权限来源；本次不增加第二份裸 ID 结果镜像。统一对象接口并不要求所有 provider 可见 ID 都改名。scope、visibility、invalidation/compaction 的原 typed ID 集合保持完全一致。
7. SYSTEM 的 advisory/opt-out/搜索指导和 memory write hint 不需增加 target 教程。搜索未发现 bundled Skill 的旧调用例；实施后精确扫四旧请求字段，只修真实调用文档/测试/dogfood。不要编辑用户安装 Skill、历史证据或无关 subagent Skill。
8. frontend 工具摘要当前无需结构改动；继续依赖 relation_kind/status/query，raw tool details 显示新 JSON。MemoryView/API/controller 属于用户级 owner，不改为从活跃 chat 取 target，也不改其 read/refresh/delete/edit 可用性。现有历史原始 JSON 可以继续展示，不新建旧/新结果双 parser。
9. 同一变更更新受影响测试、dogfood 调用与有效规格。无 schema 迁移需求；没有新增 clean-v0 列/类别或在线迁移链。新运行采用新定义，不修改已安装 epoch；旧历史保留为历史。

## 7. 英文 function.description 候选

完整值同时保存于 [descriptions.json](output/tool-target-hardcut-20261028/memory/descriptions.json)。描述只解释新调用与有效产品语义，不放实验历史或实现术语。

`memory_search`：

```text
Find saved user or project information by meaning or keywords across readable memory contexts. Use memory_get when you already have a MEMORY target; copy a result's target to read it. retrieval_summary reports search completeness, filter expansion, ranking and relation-check availability; filter_match identifies relaxed matches, and relation_warnings identifies active contradictions.
```

`memory_get`：

```text
Read a saved memory by target, including its statement, kind, context, recorded time, lifecycle, direct relations and any user-edit note. Copy a MEMORY target from memory_search or another memory result; a known exact target can be used directly.
```

`remember`：

```text
Save reusable information for future conversations. Source-faithful lightweight inferences from conversation, behavior, tool choice or planning are welcome; self-report, repetition and high confidence are not required. Save natural-language context, including attributed observations from tool results. Exclude secrets, credentials, raw tool dumps, detailed executable procedures and permission overrides. Saving takes effect immediately and creates no task, reminder or execution authority. SAVED returns the new MEMORY target; ALREADY_PRESENT returns the existing item without changing its origin or dependencies. Users can delete saved items on the Memory page. Supply statement, context_target and kind; optional based_on contains the MEMORY targets this statement depends on.
```

`mark_memory_relation`：

```text
Relate two inspected saved memories about the same subject and conditions. Supply source and target as MEMORY targets, plus relation_kind. New relations require both items active and in the same context. Related memories returned by remember are suggestions to inspect, not established relations.
CONTRADICTS: keep two incompatible memories of the same kind active when neither is the supported replacement; endpoint order does not matter.
SUPERSEDES: source is the supported replacement, including a corrected statement or kind; make target inactive without deleting it.
```

字段文案：remember 的 statement/context_target/kind 保留生产原文；based_on 保留原依赖说明（真依赖、范围方向、删 basis 连带删 dependent），只把 items 换成 MEMORY shape。get.target：“Copy a MEMORY target from a memory result or reference.”；relation.source：“For SUPERSEDES, the supported replacement that remains active.”；relation.target：“For SUPERSEDES, the item made inactive.”。内层 memory_id 不重复相同的复制说明，保留 string/minLength。类型、默认值、字节限制与 maxItems 各保留原职责。

## 8. 离线计量与明确取舍

复现：

```bash
.venv/bin/python output/tool-target-hardcut-20261028/memory/design_schema.py
```

使用仓库 `.venv`、真实 `lower_openai_function_parameters` 与 `PulsaraHeuristicTokenEstimatorV3.estimate_json`。计量单位是完整 Responses tools 数组、strict:false；单行用单元素数组，组行另按四元素数组计算。不是 provider 实收 usage，也不是推理 token 或正确率。

| 工具 | 当前生产 | 只改结构、旧 prose（不可发布的消融） | 完整候选 | 候选差值 |
|---|---:|---:|---:|---:|
| memory_search | 275 | 275 | 284 | +9 |
| memory_get | 116 | 153 | 170 | +54 |
| remember | 658 | 679 | 709 | +51 |
| mark_memory_relation | 266 | 314 | 341 | +75 |
| 四工具数组 | **1313** | **1419** | **1502** | **+189，约 +14.4%** |

结构消融仍含旧 prose 的旧字段名，不能作为另一个可发布方案；只用于说明增加来自何处。该组没有 union 膨胀，成本主要是重复内联小目标、role 字段与复制说明。外形统一并不节省静态 tokens。

同一离线 canonical ID 下，裸 get 参数为估算 13 → 19 tokens（49 → 76 JSON bytes）；无 basis 的 remember 完全不变；两个 basis 的示例 41 → 53；关系调用 35 → 45；三个 recall reference 片段 40 → 62。片段不含 envelope/正文，不能按此推断全会话费用，完整数值见 measurements.json。

建议接受这项显式成本：搜索、保存、召回、关系端点给出的身份能原样复制，不需把同一 ID 换填 memory_id/source_memory_id/target_memory_id/based_on_memory_ids。对于 memory 单 ID，理论收益比 capability 多字段身份更小；保留 search 无 target、remember 无主 target、context_target 不改名，就是避免无意义包裹。没有模型证据证明这一定降低总 token、出错率或交互次数。

离线已检查 15 个正例同时通过 canonical 与当前 lowered wire；19 个旧形状/错 kind/漏字段/额外字段/null/越界反例被 canonical 拒绝；3 个 schema 可接受但必须由原业务 owner 拒绝的例子分别是非 canonical ID、重复 basis、相同关系端点。脚本未伪称候选 parser 已存在，也未调用生产执行器。保留原 schema ID 约束不意味着放宽 require_id。

证据：

- [候选生成与检查脚本](output/tool-target-hardcut-20261028/memory/design_schema.py)
- [当前 canonical](output/tool-target-hardcut-20261028/memory/before.canonical.json)、[候选 canonical](output/tool-target-hardcut-20261028/memory/after.canonical.json)
- [当前完整 wire](output/tool-target-hardcut-20261028/memory/before.responses.json)、[候选完整 wire](output/tool-target-hardcut-20261028/memory/after.responses.json)
- [正例](output/tool-target-hardcut-20261028/memory/examples.json)、[形状反例](output/tool-target-hardcut-20261028/memory/negative_examples.json)、[原生语义检查例](output/tool-target-hardcut-20261028/memory/semantic_only_examples.json)
- [测量结果](output/tool-target-hardcut-20261028/memory/measurements.json)

## 9. 实施验证与剩余不确定性

实现已在原 owner 中完成；新增 `memory/targets.py` 只负责闭合 MEMORY 地址的形状、解析与呈现，内部 ID/DTO/SQL/关系算法保持。删除两个仅冻结 schema 的测试文件 `test_memory_tool_surface.py`、`test_direct_memory_prompt_contract.py`，没有重新冻结新 schema/prose/token 数值。以下为行为验证范围：

1. 完整 target 从 search/get/remember/related/relations/response-preference/自动召回各 variant 原样进入 get、source/target 与 based_on；闭合对象、wrong kind/null/旧 flat 参数拒绝。无依赖与空数组等价，最多 8/重复/顺序、canonical ID 与 UTF-8 边界沿原规则。
2. `test_memory_tool_result_rejection.py` 通过真实 owner 验证返回 target 可用于后续读、basis 和关系，并保留 APPLICATION_ERROR/SYSTEM_ERROR 的真实状态区别；旧/错对象在读取前拒绝。target 不是新权限；原 visible scopes 与 opt-out 用既有行为测试证明不变。
3. `test_direct_advisory_memory.py` 覆盖 direct save/duplicate、stale basis 先拒绝、ROOT tool exact join、body 与 mutation/exposure 不能伪配、ToolResult 发布失败回滚、已提交确认在用户编辑/删除后仍有效、同 domain/current workspace、CONTRADICTS 两种顺序与同 kind、新/重复 SUPERSEDES 与 taxonomy correction、get SUPERSEDED 与 user_edit。
4. `test_direct_memory_related_fallback.py`、`test_memory_search_rerank_selection.py`、`test_direct_memory_preference_refresh.py` 与现有召回测试覆盖新的结果身份、三 variant 成本/单调降级、related exact-context 与失败回退、kind relaxation/limit、偏好预算。真实 rendering 与 quote 使用同一形状；不得通过调高预算掩盖增加成本。
5. `test_memory_management_contracts.py` 与现有 PostgreSQL 删除集成用例保持 incoming BASED_ON cascade、SUPERSEDES 恢复条件、CONTRADICTS 非传播和用户页独立可用；不用为公开字段改名重写删除算法。
6. 更新 `tests/dogfood/run_direct_memory_dogfood.py` 的调用与结果取值。未来真实 provider 验证只在用户另行要求或激活规格要求时运行；应分别测单工具 schema 接受、复制 target、关系方向、basis/related 区别与错误后修正，不能用 190 次旧实验冒充本候选。
7. 复用已有 provider-input 连续性测试确认合法 epoch 边界，无新前缀路径；目录/subagent exposure 元数据不变。没有新增寿命上限、事件/guard/关系类别或 proof 机制。

聚焦验证结果：

- `tests/test_direct_advisory_memory.py`：**43 passed**；覆盖直接保存/重复、关系方向/重试、事务回滚/确认、用户编辑/删除、原作用域和依赖级联。
- `tests/test_memory_tool_result_rejection.py`、`test_memory_search_rerank_selection.py`、`test_direct_memory_related_fallback.py`、`test_direct_memory_preference_refresh.py`、`test_memory_management_contracts.py`：**76 passed**。包括真实 owner 的返回 target→get/based_on/source/target 复用、旧参数/错误对象在查询前拒绝、元数据曝光不变，以及混合召回最终 target 不进入 rerank 文档。
- runner 中保存结果直接用作 basis、坏 citation 结算后继续、opt-out steer；permanent deletion/fork 的记忆独立性：**6 passed**。
- 本组生产 owner 与受影响行为测试的 Ruff 检查通过。一次性 JSON 序列化后对照确认四份完整 wire 与 ROOT 获批候选逐值相等；该检查未加入冻结测试。

实现报告与复核记录在 [记忆组实施报告](output/target-hardcut-implementation-20261028/memory/REPORT.zh.md)。当前请求/结果合同没有待选结构分叉；保留 exposure metadata 已与 ROOT 对齐。模型认知收益和完整目录的长期调用成功率未作测量；此次 provider 小样本没有覆盖记忆工具。
