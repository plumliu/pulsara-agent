# Pulsara：完整删除 Subagent Profile 实施规格

日期：2026-10-30。状态：**实施与验收完成。** 实际验证范围见 [验收报告](PULSARA_SUBAGENT_PROFILE_REMOVAL_ACCEPTANCE_REPORT.zh.md)。

## 1. 删除后的产品合同

Subagent 使用统一的 Subagent SYSTEM 提示词，每份 Work 通过 `instruction` 表达具体目标、角色、方法和交付要求。需要研究、审阅、验证或综合时，ROOT 直接在任务指令中说明。

Subagent 子系统不再提供 profile 概念：没有工具参数、预设枚举、默认选择、继承、持久字段、查询字段、独立身份提示词或模型输入来源。不得将其改名为 role、preset、worker type 等同类替代机制，也不保留空字段、兼容参数、静默忽略或默认通用 profile。

Agent 仍是可复用的独立会话，Work 仍是该会话中的串行请求。创建、复用、消息、等待、停止、依赖、材料、模型配置捕获和容量规则保持现有合同。

Subagent 接收的委派输入只传纯文本，包括 instruction、初始 context、依赖结果、显式材料及 Agent 消息。需要提供图片线索时，在文本中说明已有图片路径或引用，由接收方自行使用 `view_image` 查看，不将图片 part 直接注入上述输入，也不为删除 profile 扩增 Work 图片参数。`view_image` 返回的图像仍沿现有工具结果链路提供给模型。

本规格优先于当前文档中保留或暂缓修订 profile 的条款；实施时原地修改这些条款，不再保留相互冲突的当前说明。

## 2. 当前链路与删除清单

| 边界 | 当前落点 | 本次处理 |
| --- | --- | --- |
| 原生工具 schema | `capability/builtin_catalog.py` 的 `_run_agent_schema`、`PROFILES` 引用 | 删除 `run_agent.profile` 与枚举依赖 |
| 请求合同 | `conversation_kernel/subagents/collaboration_contracts.py` | 删除 `PROFILES`、profile 校验、`PreparedAgentRequest.profile`、参数构造与解析 |
| Work 接纳 | `conversation_kernel/_repository/subagents.py` | 删除 `_agent_profile_text`、最近 Work profile 查询、默认与继承计算、写入值及接纳确认比较 |
| 查询返回 | 上述 repository 的 `_agent_work_value`；`subagents/collaboration_api.py` 的 `_public_work`、`_public_agent` | 删除 profile 输出字段及透传分支 |
| 提示词生产 | `conversation_kernel/context_sources.py` | 删除五种预设、共用前后文与 `subagent_profile_text` |
| 初始输入组 | `conversation_kernel/_repository/prompts.py` 的 `_queued_ordered_inputs` | 删除 profile 文案的物化与独立 entry；保留其余完整组的原顺序 |
| Canonical 输入类型 | `model_input/contracts.py` | 删除 `CanonicalInputOriginKind.AGENT_PROFILE` 及所有允许集合中的该来源 |
| 读取与计量 | `conversation_kernel/reader.py`、`compaction/contracts.py` | 删除该来源的读取、图片与 typed prompt 计量分支 |
| Clean-v0 schema | `storage/migrations/sql/0000_conversation_kernel_baseline.sql` | 删除两列、相关 CHECK 和 transcript 输入来源枚举值 |
| Schema 校验资源 | `storage/migrations/resources/0000_conversation_kernel_expected_catalog_v1.json` | 根据重建后的真实 clean-v0 catalog 更新既有校验摘要；不得只修改 DDL 而保留旧 catalog |
| Code Mode | 共用请求合同与原生工具能力 schema；bundled 编排参考 | 自动沿共用 schema 去掉参数，删除教程和示例中的使用，不建立 JS 特例 |
| 前端残留 | `frontend/lib/runtime-adapter.ts` 的 `profileLabel`；桌面静态资源及 source map | 删除未使用的映射，通过既有桌面构建重新生成资源与 source map，不手改生成物 |
| Hook 残留 | `hooks/matcher.py` 的未使用 `agent_type` 参数；plugin installer 的旧 worker-profile matcher 教学 | 删除旧 Agent 类型残留，不更改现行公共 Hook 生命周期 |

路径均相对于 `src/pulsara_agent/`，前端路径相对于仓库根目录。

五种待删除预设为 `general_worker`、`research_worker`、`review_worker`、`verification_worker`、`synthesizer`。删除的是整个生产与消费链，不是只删五段英文。

### 2.1 数据库

`prompt_queue_items` 删除：

- `effective_profile` 列及其枚举 CHECK。
- `profile_text` 列。
- 将 `AGENT_INSTRUCTION` 与上述两列非空绑定的 CHECK。

`transcript_entries.input_origin` 的 CHECK 删除 `AGENT_PROFILE`。请求队列本身的 `input_origin` 没有该值，不新增或搬迁它。

同步改写 INSERT 的列、占位符、参数和查询映射。其余来源、委派归因、初始 context、材料、等待条件、请求配置和权限快照不受影响。

用户明确授权生产数据库重置，数据不需要保留；按开发期 hard cut 修改 clean-v0 baseline，不建立在线迁移或旧数据兼容读取。实际重置前核实目标，数据库验证使用已核实的本地可丢弃数据库。

旧 profile entries、含该来源的压缩 snapshot、fork／imported 历史不读取或转换成新合同，不新增清洗、重放或修复任务。删除枚举后，旧 snapshot 解码及旧来源写入新 SQL CHECK 将不再合法；使用重建后的新数据验证 snapshot 解码、fork 和 replay。

baseline 修改后，同步更新既有 expected catalog 资源的 `observed_catalog_fingerprint`，通过现有 schema verifier 验证真实 catalog；不新建摘要注册表或兼容 manifest。

### 2.2 接纳、重试和查询

新建和复用 Agent 都不再计算任何身份预设。复用时不查询上一份 Work 的该字段；取消某份 Work 也不再涉及预设继承。

原子 batch、原生调用、Code Mode 调用及 ACK 未知后的确认使用同一份无 profile 的请求合同。删除确认比较中的该字段后，现存的 instruction、ROOT／Agent 身份、委派来源、初始 context、新建名称、模型配置及权限比较保持，不借本次变更扩大 ACK 确认合同。

工具 schema 和运行时闭合参数校验均不接受旧 `profile` 参数。工具与查询结果中也不出现该键，不返回 null 或隐含默认值。

原生与 JS 单次调用、batch 成员及 PreTool Hook 改写后的参数均沿现有共用校验拒绝旧字段；无论传入值还是 null 都不接受。batch 失败不允许部分接纳，不建立 JS 特例。

当前 protobuf 没有专门的 Subagent profile 字段，不为删除创造协议字段或消息。已有 `input_origin` 字符串投影随 canonical 来源删除而自然收束；若实际实现发现另有生产消费者，必须连同删除，不加兼容槽位。

## 3. 模型输入与压缩边界

删除后，每份 Work 的初始业务输入组按既有顺序包含：

1. 创建 Agent 时选取的初始 context（如有），每份仍是独立材料 entry。
2. ROOT 派发的 `instruction`，仍是 turn 的 initial entry 和 queue 的 consumed entry。
3. 依赖结果与显式材料，仍按既有种类及声明顺序分别追加。

初始 context 只在创建 Agent 时选取；复用 Agent 的新 Work 不接纳新的 `context` 参数。无 context 时 instruction 就是组首；有 context 时组首可在 instruction 前。所有序号由原分配器自然分配，不保留已删除 entry 的占位或固定序号。

不再在组前插入身份提示词，也不把已删除的预设文案拼入 instruction、SYSTEM、context 或压缩摘要。通用 Subagent SYSTEM 的职责由其当前正文承担。

**完整初始输入组仍可有材料先于 instruction。** 不得因为删除 profile 就把历史截止位置简化成 `instruction_sequence - 1`。首次调用前压缩的 source cut 仍在完整组之前，当前组全部作为精确 suffix 保留，采用目标仍是本次 RUNNING turn。

删除该输入来源后，以下事实仍保持：

- 每份 Work 只开启一个真实 turn；启动事务、revision0 和 queue CONSUMED 原子成立。
- 初始 context、instruction、依赖结果与显式材料保持独立 entry，来源归因准确。
- 复用 Agent 保持自身历史；每份 Work 沿该次派发时捕获的 ROOT 模型配置和权限执行，不继承上一份 Work 的配置。
- adopted safe point 仍按接纳顺序消费 steer 与 Agent 消息，并按完整最终输入测量预算。
- Work 初始输入组保持纯文本，不通过图片 fixture 暗示存在委派图片入口。通用 reader／压缩的图片合同及 `view_image` 工具结果处理沿其既有边界验证。
- 已安装 epoch 内 SYSTEM／tools 字节不变，messages 只追加。不得借本次删除引入在线前缀重写或新的 rebase 边界。

## 4. 文档与示例同步

当前文档原地修改，删除预设说明、参数、默认与继承规则和输入示意中的身份 entry：

- `PULSARA_SUBAGENT_WORK_CODE_MODE_DESIGN.zh.md`：请求表、事实归属、输入顺序、复用、示例、实施范围和验收。
- `PULSARA_AGENT_CODE_MODE_DEFERRED_SURFACES.zh.md`：删除预设提示词暂缓事项，以及依赖该概念的 SYSTEM 职责说明。
- `PULSARA_TOOL_QUERY_AND_CELL_ID_UNIFICATION_IMPLEMENTATION_SPEC.zh.md`、`PULSARA_CODE_MODE_TOOL_SUBSET_DESIGN.zh.md`：删除仍将 Subagent profile 列为暂缓事项的说明，保留其他独立暂缓事项与 Code Mode 资源配置概念。
- `PULSARA_COMPACTION_MODEL_CONFIGURATION_AND_MANUAL_IDLE_ONLY_IMPLEMENTATION_SPEC.zh.md`：以真实的初始 context／材料保留“instruction 前存在输入”的整组边界说明与验收。
- `bundled_skills/pulsara-code-mode/references/agent-orchestration/coordination.md`、`workflows.md`、`examples.md`：删除全部 profile 教学和示例参数。
- `bundled_skills/pulsara-plugin-installer/references/pulsara-hook-extension.md`：删除已经没有现行生产消费者的 worker-profile matcher 说明。

现有历史报告和 `archived_docs/` 可作为历史保留，不作为当前接口或通过证据。仍可执行的 dogfood 脚本中的旧参数和提示词必须删除；仅适用于退役 Task API 的实验段应删除或按当前 Work 行为改写，不能继续教模型使用旧预设。

已定位的退役可执行内容按以下范围处理：

- `tests/dogfood/run_round10_subagent_dogfood.py` 整份依赖已退役的 Task API，删除退役脚本，保留历史输出。
- `run_pr03_user_control_dogfood.py` 删除退役 Task 控制场景及调用、报告字段，保留其他控制场景和仍被导入的共用辅助函数。
- `run_pr04_prompt_queue_dogfood.py` 删除或按现行 Work 改写退役 wait／FIFO 子场景，保留独立队列验证及仍被其他脚本导入的辅助函数。
- 其他仍教模型使用旧 Task API 的脚本不能直接作为本次通过证据；仅当选作当前验收入口时，先按现行 Agent／Work 合同调整所用场景。不借本次删除扩展重写全部历史 dogfood。

前端源代码删除后运行既有 `npm run build:desktop`，同步提交生成的桌面资源及 source map；即使可执行 JS 已 tree-shake，内嵌旧源码的 source map 也不得残留五种预设。

## 5. 验证范围

优先调整现有测试，保留其独立行为断言；不通过删除整个行为测试来消除 profile 引用。

| 范围 | 重点 |
| --- | --- |
| API／schema／Code Mode | 新建、复用与原子 batch 正常；旧参数值与 null 均拒绝；Hook 改写后同样拒绝且 batch 无部分接纳；原生和 JS 无分叉；Work／Agent／查询／wait／stop 返回无 profile 键 |
| `test_agent_collaboration_postgres.py`、`test_agent_collaboration_api.py` | 保留真实接纳 commit 后 ACK 丢失确认、提交与唤醒恰一次、原子 batch 拒绝；保留其余现存精确匹配和冲突断言，不用启动层确认替代 Work 接纳确认 |
| `test_agent_requests_persistence.py` | 去掉预设继承断言；保留取消、依赖等待不算 idle、Work 配置和精确确认等行为 |
| `test_agent_context_canonical.py`、`test_agent_context_input.py` | 无身份 entry；初始材料、instruction 与各依赖结果独立且有序；复用历史与 prefix 连续 |
| `test_canonical_request_start_postgres.py` | 完整初始组、initial／consumed 指向 instruction；接纳、确认、取消和接管保持 |
| `test_compaction_first_call_source_boundary.py`、`test_compaction_request_start_integration.py` | 分层验证真实新建 Work 的前置 context 与完整启动组、真实复用 Work 的前驱历史摘要与 instruction→材料 suffix；保留 summary 排除当前组、当前 turn 采用、首 ordinary wire、steer 和消息检查，不向复用 Work 虚构 context |
| `test_kernel_image_input_k3.py` | 删除 profile 图片来源；通用投影的合成图片测试可使用 `AGENT_MATERIAL` canonical 类型，但明确为合成 reader／planner 测试，不声称 Subagent 委派接口支持图片；保留前后位置与历史投影边界 |
| `test_agent_session_host.py`、`test_agent_session_runtime.py`、`test_hook_input_identity.py` | 真实创建／复用、排队完成、停止／关闭和 Hook 仍用 instruction；保留 scope、配置与权限、阻断、真实 Work 结果及 SYSTEM／tools／messages 前缀连续性 |
| `test_conversation_fork.py` 及 Agent canonical 预算测试 | 新合同下 fork 精确复制、无额外执行、ACK 丢失不重复复制；CODE_RESULT 的 UI 可见性及 provider／预算过滤保持 |
| `test_stage5_clean_migration.py` 及既有 schema drift 测试 | fresh install、二次 migrate、catalog 校验通过；两列不存在，transcript CHECK 拒绝已删来源并接受合法 Agent 来源 |
| `test_builtin_default_null_contracts.py` | 删除旧可选字段断言，保留其余可选参数省略与 null 的闭合合同 |
| Bundled Skill／前端／dogfood | 活跃教学与示例无旧参数；删除未使用标签函数；实际采用当前调用链 |

先跑共同请求、Agent 输入、接纳、压缩边界和受影响的 schema／Code Mode 定向回归，再做必要的静态检查。若进行真实 provider 验证，沿保存配置与既有隔离机制，验证一次无 profile 的 Work 首次输入和复用，不另建实验框架。

验收必须区分真实可达链路和合成算法输入：当前新建 Agent 可有前置 context，但没有本 Agent 的前驱 Work 历史；复用 Agent 可有前驱历史，却不能新传 context。不得为了同时组合两者扩大生产接口。算法层合成“前驱历史＋前置材料”可验证组首 cut，并标明证据层级；现有 SNAPSHOT 的继承摘要截止与本次组首截止不相等的检查也须保留。

`test_agent_context_input.py::_group(image=True)` 和 `test_compaction_first_call_source_boundary.py::_first_call_read` 中的图片同样属于合成 fixture：改用纯文本组，或明确标为通用 reader／planner 测试；不得作为真实委派图片输入的证据。

图片由 Subagent 自己调用 `view_image` 获取；验证委派 instruction、context、材料、依赖 final 及 Agent 消息均为纯文本，文本路径或图片引用不会被委派链路自动升级成图片 part。不得从 ROOT 图片输入或合成 canonical 图片测试推断 Subagent 接收接口也支持直接传图。

## 6. 完成条件

- Subagent 的参数、请求 DTO、数据库列、接纳确认、查询返回和提示词生产均无 profile。
- `AGENT_PROFILE` 从 canonical 类型、SQL CHECK、reader 与 compaction 中完整删除。
- 原生和 Code Mode 工具、当前文档与 bundled 示例不再出现五种预设或默认／继承说明。
- clean-v0 DDL 与 expected catalog 一致；桌面生成资源和 source map 无旧预设残留。
- 委派内容保持纯文本，图片通过 Subagent 主动调用 `view_image` 查看。
- 完整初始输入组、instruction 开启 entry、依赖顺序、实际模型输入、首次压缩与消息接纳的现有行为通过定向回归。
- 不保留旧参数兼容、空占位、隐藏默认或改名替代；不新增持久类别、后台清洗任务或恢复机制。
- 仅清除 Subagent 身份预设；模型 reasoning/wire profile、Hook 协议兼容 profile、用户资料等其他同名概念按其原有职责保留。

本文件中的旧名称用于定位删除范围；不构成继续支持它们的产品合同。
