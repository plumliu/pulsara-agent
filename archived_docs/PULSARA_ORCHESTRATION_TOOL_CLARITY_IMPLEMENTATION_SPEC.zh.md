# Pulsara 编排与计划工具清晰化实施规范

日期：2026-10-26。状态：**已实施**；用户授权后，本组 12 个工具说明与 bundled Skill 已落地，并纳入最终 critic 的两处删除。没有新增 provider 调用。

## 1. 产品目标与范围

模型选择编排工具时，需要先知道要创建、查看、指导、等待还是结束工作，再知道输入中的身份、上下文、依赖和结果分别有什么作用。本次把这些决策直接写在对应位置：函数说明交代用途与操作后果，schema 保留输入形状，字段说明只解释字段名和结构无法表达的事实。减少重复叙述与需要重新拼装的关系，同时减少固定工具输入体积。

本规范只负责以下 12 个既有工具：`create_agent_tasks`、`list_agent_models`、`list_agents`、`spawn_agent`、`send_agent_message`、`stop_agent`、`wait_agent`、`report_agent_result`、`enter_plan`、`ask_plan_question`、`exit_plan`、`todo`。本组工具名、参数名、路由、权限、运行时行为和结果结构均保持现状。全目录基线为 40 个 builtin；合入 capability 组无参数全刷新方案后为 39 个，本文独立测量仅替换本组。没有拆工具、外层 request、dispatcher、逐轮工具筛选或新持久化机制。

7 个 ROOT 编排工具的实际调用条件仍是当前运行 `effective_mode="bypass-permissions"`。这是现有 `KernelSubagentManager.invoke` 和 tool authorization 的可用性条件；现有 SYSTEM 已集中陈述，本次从七个函数中删除重复说明。`report_agent_result` 面向 worker，计划工具面向 ROOT，todo 维持各自 execution 的独立状态。模型可见文本统一称 ROOT agent / ROOT conversation。

## 2. 依据及实现边界

本次逐项阅读了 `output/tool-schema-compatibility-20261026/` 中的 `REPORT.zh.md`、`EXPERIMENT_PLAN.zh.md`、`COMPRESSION_DISCUSSION.zh.md`、`COMPRESSION_CRITIC.zh.md`。190 次调用的证据说明：展平可能丢失字段间关系，根 schema description 在已测 Qwen 路径中不宜承载唯一关键规则。它不证明本文的 12 个候选已经在线验证，也不提供任意第三方接口的普遍保证。这些证据只用于本节设计依据，不进入模型可见工具文字。

### 现有 SYSTEM 中的公共可用性事实

`src/pulsara_agent/ports/system_prompt.py` 的 `DEFAULT_SYSTEM_PROMPT` 已写明：

```text
Subagent orchestration tools are available only to the ROOT agent in bypass-permissions mode.
```

生产调用链已经携带该事实：`desktop_app/session_controller.py` 的会话创建及恢复调用没有传入替代 system_prompt；`conversation_kernel/host.py` 在构造 `KernelContextSourceCollector` 时使用 `system_prompt or DEFAULT_SYSTEM_PROMPT`；`conversation_kernel/context_sources.py` 将该 base 保存为 `_base`，并在收集时无条件提供 BASE_SYSTEM。这里检查的是 Pulsara 实际生产上下文，不假设工具会脱离该上下文独立分发。Host 的注入参数不构成本次给七个工具再复制同一句规则的产品理由。

因此该规则在既有 SYSTEM 中保留一次，七个函数只说明各自操作。本次不修改 system_prompt.py、collector 或任何安装/重建边界。`report_agent_result` 仍明确“Finish your worker task ... to the ROOT agent”，因为这是它的调用角色与结果去向。独立阅读的 Skill 保留一段简短可用性背景；它不是每次工具定义中重复的前置段落。

本组唯一根 union 是 `wait_agent`。其 `task_ids` 与 `settle` 的关系移至 function.description 两行普通英文；canonical 两个封闭分支保持不变，provider 仍经现有 lowerer 展平为受控超集。该 schema **没有 discriminator**，所以删掉其 canonical 根 description 后，现行 `_root_union_requirement_description` 不会重新生成说明。其余 11 个工具保持当前结构；`context`、`reasoning`、`options` 的嵌套 union 继续使用既有路径，不增添新的形式语言。

当前仓库根目录没有仍在使用的独立编排/计划旧规范。`archived_docs/` 中相关文档只作历史；本规范以 AGENTS.md、当前 catalog、实际 parser/handler 及现有测试为实施基线。bundled `pulsara-subagent` Skill 用于交叉检查工作流，不替代实际 parser；其中历史文字把外部任务引用表述为再拼接 task:，本规范明确采用完整返回的 canonical task_id，避免重复前缀。

工具定义只编辑 `src/pulsara_agent/capability/builtin_catalog.py` 的说明和列明的 default annotation；配套 bundled Skill 的五处同步修改见第 9 节：

| 位置 | 实施内容 |
| --- | --- |
| `_ROOT_ORCHESTRATION_PRECONDITION` | 删除常量及七处拼接；实际规则由现有 SYSTEM 集中陈述 |
| `_BUILTIN_DESCRIPTORS` 中本组 12 项 | 替换完整函数说明，按第 4 节修改 inline 字段说明 |
| `_SUBAGENT_TASK_DESCRIPTION`、`_SUBAGENT_PROFILE_DESCRIPTION`、`_SUBAGENT_CONTEXT_DESCRIPTION` | 替换为第 4 节共享字段文字 |
| `_SUBAGENT_CONTEXT_TURNS_DESCRIPTION`、`_subagent_context_schema` | 去掉 turns 与 worker_history.task_id 的重复说明；不改变分支 |
| `_subagent_model_schema` | 替换 model、connection_id、reasoning 说明 |
| `_wait_agent_schema` | 删除根说明；字段说明按表；补足已生效默认值的 schema annotation |

不需要编辑 `llm/adapters/openai/function_tools.py`。候选从上述 canonical 修改直接经过**未改动的** `lower_openai_function_parameters` 生成，没有 post-lowerer 删字段或补说明。没有自动 title 可删，也不引入全局 annotation 清洗。第 4 节每条修改都命名真实 schema 节点，业务属性 `description` 本身继续存在，例如 `options` 内选择项的说明字段。

## 3. 最终 function.description

下面每个代码块是完整英文定稿，包含标点与换行；直接替换相应 descriptor.description。Responses 的字段叫 description，Chat function envelope 对应 function.description；不把正文搬到 parameters.description。

### create_agent_tasks

```text
Schedule tasks together and return task IDs and initial statuses. Independent tasks run in parallel as capacity becomes available. A depends_on prerequisite must succeed before its dependent starts; its result becomes task context. An unsuccessful prerequisite blocks the dependent. Use material_task_ids for finished results or failures without waiting for success, and context for conversation history. Dependencies must be acyclic, with at most 64 references in one batch. Use spawn_agent for one task. See the pulsara-subagent Skill for workflow examples.
```

### list_agent_models

```text
List saved model connections and their supported reasoning choices for delegated tasks. Use the returned connection IDs and choices when selecting a different worker model.
```

### list_agents

```text
List this conversation's delegated tasks, including exact IDs, status, objectives, dependencies, pending-message counts and result summaries. Results are status snapshots; use wait_agent to wait for completion. Continue paging with next_cursor and unchanged max_items and include_dependencies. If a cursor is rejected, omit it to start a new listing.
```

### spawn_agent

```text
Delegate one self-contained task and return its task_id and initial status. Execution is asynchronous and may queue for capacity. Use create_agent_tasks for batches or success dependencies. context selects history for the new worker; worker_history creates a new task continuing a finished worker's public history. Workers share the workspace. See the pulsara-subagent Skill for workflow examples.
```

### send_agent_message

```text
Send additional guidance to an ACTIVE worker by task_id. A queued result means the message is accepted for delivery; the worker may read it later. For follow-up after a worker finishes, use spawn_agent with context.mode=worker_history and its old task_id.
```

### stop_agent

```text
Cancel a queued, dependency-waiting or running task and return its status. Finished tasks retain their final state. Cancelling a prerequisite blocks its dependents. Existing side effects, unrelated tasks and background commands remain.
```

### wait_agent

```text
Wait for delegated task completion. Results arrive as separate conversation messages after this call.
With task_ids: optional settle and timeout_seconds. settle=first waits for any target to finish; settle=all waits for every target.
Without task_ids: optional timeout_seconds is the only field; wait for any ready result or for nothing to remain pending.
Finished includes success, failure and cancellation. Current-reply guidance interrupts the wait with steer_available; later-reply queued guidance does not. timeout ends this wait while tasks continue.
```

### report_agent_result

```text
Finish your worker task and deliver its outcome to the ROOT agent. Use this as the only tool call in the response when the work is complete. Provide a self-contained summary with evidence, constraints and useful file or artifact locations. Optional data carries structured results; output_preview and diagnostics provide supporting detail.
```

### enter_plan

```text
Start a read-only planning phase when the user wants to review an approach before implementation, or an unresolved consequential implementation choice needs their decision. Investigate, use ask_plan_question for blocking choices, then submit the complete proposal with exit_plan. Use this as the only tool call in the response; it takes control of the turn and other calls in that response will not run.
```

### ask_plan_question

```text
During an active planning phase, ask one question whose answer materially changes the plan. The user's answer becomes this tool's result, then planning continues. Use this as the only tool call in the response; other calls in that response will not run.
```

### exit_plan

```text
Submit a complete plan for user review, or leave planning without a proposal. A nonblank plan opens review: approval resumes implementation, revision continues read-only planning with feedback, and cancellation ends planning. Supply a complete replacement plan after revision. Omitting plan, passing null or blank text exits without review and automatically resumes with the permissions saved on entry. Use this as the only tool call in the response; other calls in that response will not run.
```

### todo

```text
Replace the current task's ordered progress checklist with the supplied items. Include every item to retain; items=[] clears it. Use at most one in_progress item, and update completed and next active items together. The ROOT checklist clears when execution becomes idle and remains while waiting for user input. This checklist is separate from the delegated tasks shown in the Tasks tab.
```

## 4. 精确字段修改清单

所有路径是以该工具 canonical `input_schema` 为根的 JSON Pointer；`/oneOf/0` 与 `/oneOf/1` 保留原有顺序。`删除`只删该 annotation；`设置`覆盖或补入该值。未列出的所有节点原样保留。完整输入 schema 见 `output/tool-clarity-specs-20261026/orchestration/proposed_canonical_schemas.json`；最终完整 Responses 定义见同目录 `proposed_tools.json`。`field_edits.json` 同时保存旧值和新值供实施时核对。

新增 default 只有四个位置：batch profile 为 general_worker、两个 wait 分支 timeout_seconds 为 30、targeted wait settle 为 all。这些值当前已由 parser/handler使用，新增的是 schema annotation，既不填充用户输入也不改变运行时默认值。spawn_agent.profile 的既有 default 原样保留。其他省略行为中，model 的继承和 context 的无历史语义仍用对应字段一处说明，因为它们不是简单数值默认值。

### create_agent_tasks

| JSON Pointer | 操作与最终值 |
| --- | --- |
| `/properties/tasks/items/properties/task/description` | 设置为 `"Self-contained objective, deliverable, constraints and exact source or file locations; up to 65536 UTF-8 bytes."` |
| `/properties/tasks/items/properties/profile/description` | 设置为 `"Working style: general_worker executes; research_worker investigates with sources; review_worker finds defects; verification_worker runs checks; synthesizer combines supplied findings."` |
| `/properties/tasks/items/properties/context/description` | 设置为 `"Omit for no history. last_n adds recent ROOT conversation turns, excluding tool calls and results. worker_history adds a started, finished worker's readable public history from this conversation. The new worker uses current tools and permissions."` |
| `/properties/tasks/items/properties/model/description` | 设置为 `"Omit to inherit the assigning agent's model and reasoning; otherwise select a saved connection."` |
| `/properties/tasks/items/properties/material_task_ids/description` | 设置为 `"Exact IDs of finished tasks in this conversation whose results or public failure details should be supplied as material."` |
| `/properties/tasks/items/properties/context/oneOf/1/properties/turns/description` | 删除 |
| `/properties/tasks/items/properties/context/oneOf/2/properties/task_id/description` | 删除 |
| `/properties/tasks/items/properties/model/properties/connection_id/description` | 设置为 `"Exact connection_id from list_agent_models."` |
| `/properties/tasks/items/properties/model/properties/reasoning/description` | 设置为 `"Copy a supported choice from list_agent_models; effort.value may be null when listed. Omit for this connection's default."` |
| `/properties/tasks/description` | 删除 |
| `/properties/tasks/items/properties/task_key/description` | 设置为 `"Unique, case-sensitive key that other tasks in this batch can reference in depends_on."` |
| `/properties/tasks/items/properties/label/description` | 设置为 `"Display name; dependencies use task_key or task_id."` |
| `/properties/tasks/items/properties/display_role/description` | 设置为 `"Display-only role; profile selects working style."` |
| `/properties/tasks/items/properties/depends_on/description` | 设置为 `"Prerequisites: another item's exact task_key in this batch, or an earlier task's complete task_id. Omit or use [] for independent work."` |
| `/properties/tasks/items/properties/profile/default` | 设置为 `"general_worker"` |

### list_agent_models

无字段改动。

### list_agents

| JSON Pointer | 操作与最终值 |
| --- | --- |
| `/properties/max_items/description` | 删除 |
| `/properties/include_dependencies/description` | 设置为 `"Include each task's prerequisites and their statuses."` |
| `/properties/cursor/description` | 设置为 `"Exact next_cursor from the preceding page; omit for the first page."` |

### spawn_agent

| JSON Pointer | 操作与最终值 |
| --- | --- |
| `/properties/task/description` | 设置为 `"Self-contained objective, deliverable, constraints and exact source or file locations; up to 65536 UTF-8 bytes."` |
| `/properties/profile/description` | 设置为 `"Working style: general_worker executes; research_worker investigates with sources; review_worker finds defects; verification_worker runs checks; synthesizer combines supplied findings."` |
| `/properties/context/description` | 设置为 `"Omit for no history. last_n adds recent ROOT conversation turns, excluding tool calls and results. worker_history adds a started, finished worker's readable public history from this conversation. The new worker uses current tools and permissions."` |
| `/properties/model/description` | 设置为 `"Omit to inherit the assigning agent's model and reasoning; otherwise select a saved connection."` |
| `/properties/material_task_ids/description` | 设置为 `"Exact IDs of finished tasks in this conversation whose results or public failure details should be supplied as material."` |
| `/properties/context/oneOf/1/properties/turns/description` | 删除 |
| `/properties/context/oneOf/2/properties/task_id/description` | 删除 |
| `/properties/model/properties/connection_id/description` | 设置为 `"Exact connection_id from list_agent_models."` |
| `/properties/model/properties/reasoning/description` | 设置为 `"Copy a supported choice from list_agent_models; effort.value may be null when listed. Omit for this connection's default."` |
| `/properties/task_name/description` | 设置为 `"Case-sensitive display name for this task."` |

### send_agent_message

| JSON Pointer | 操作与最终值 |
| --- | --- |
| `/properties/task_id/description` | 设置为 `"Exact task_id returned by spawn_agent, create_agent_tasks or list_agents."` |
| `/properties/message/description` | 设置为 `"Self-contained guidance, evidence or correction; up to 16384 UTF-8 bytes."` |

### stop_agent

| JSON Pointer | 操作与最终值 |
| --- | --- |
| `/properties/task_id/description` | 设置为 `"Exact task_id returned by spawn_agent, create_agent_tasks or list_agents."` |
| `/properties/reason/description` | 设置为 `"Cancellation note; stored with the call, not delivered as a worker message."` |

### wait_agent

| JSON Pointer | 操作与最终值 |
| --- | --- |
| `/description` | 删除 |
| `/oneOf/0/properties/timeout_seconds/description` | 设置为 `"0 returns an immediate snapshot."` |
| `/oneOf/0/properties/timeout_seconds/default` | 设置为 `30` |
| `/oneOf/1/properties/timeout_seconds/description` | 设置为 `"0 returns an immediate snapshot."` |
| `/oneOf/1/properties/timeout_seconds/default` | 设置为 `30` |
| `/oneOf/1/properties/task_ids/description` | 设置为 `"Exact task IDs returned by spawn_agent, create_agent_tasks or list_agents."` |
| `/oneOf/1/properties/settle/description` | 删除 |
| `/oneOf/1/properties/settle/default` | 设置为 `"all"` |

### report_agent_result

| JSON Pointer | 操作与最终值 |
| --- | --- |
| `/properties/summary/description` | 设置为 `"Final conclusion, evidence, constraints and useful source locations; understandable on its own."` |
| `/properties/data/description` | 设置为 `"Structured result. Summary UTF-8 bytes plus canonical data JSON share a 16 KiB budget."` |
| `/properties/output_preview/description` | 设置为 `"Supporting excerpts, changed files or command outcomes; up to 32768 UTF-8 bytes."` |
| `/properties/diagnostics/description` | 设置为 `"Structured findings; canonical JSON is limited to 8 KiB per item and 64 KiB for the array."` |

### enter_plan

| JSON Pointer | 操作与最终值 |
| --- | --- |
| `/properties/reason/description` | 设置为 `"Decision or objective that needs a planning phase; up to 4096 UTF-8 bytes."` |

### ask_plan_question

| JSON Pointer | 操作与最终值 |
| --- | --- |
| `/properties/question/description` | 设置为 `"One self-contained question with the context needed to decide; up to 16384 UTF-8 bytes."` |
| `/properties/options/description` | 设置为 `"Omit or use [] for an open question with allow_free_text=true; otherwise supply two or three choices with unique labels."` |
| `/properties/allow_free_text/description` | 设置为 `"Allow a custom answer alongside the choices. Required true when options is omitted or empty."` |
| `/properties/options/anyOf/1/items/properties/label/description` | 设置为 `"Distinct choice label; up to 256 UTF-8 bytes."` |
| `/properties/options/anyOf/1/items/properties/description/description` | 设置为 `"Effect or tradeoff of this choice; up to 2048 UTF-8 bytes."` |
| `/properties/options/anyOf/1/items/properties/recommended/description` | 设置为 `"At most one choice may be true; omitted means false."` |

### exit_plan

| JSON Pointer | 操作与最终值 |
| --- | --- |
| `/properties/plan/description` | 设置为 `"Complete proposal: outcome, affected components, ordered changes, constraints and validation; up to 1048576 UTF-8 bytes."` |
| `/properties/summary/description` | 设置为 `"Brief overview of the complete plan; up to 8192 UTF-8 bytes."` |

### todo

| JSON Pointer | 操作与最终值 |
| --- | --- |
| `/properties/items/description` | 设置为 `"Complete ordered snapshot, up to 4 KiB of canonical JSON."` |
| `/properties/items/items/properties/text/description` | 设置为 `"Unique task label: one plain line in Unicode NFC, without surrounding whitespace, up to 192 UTF-8 bytes."` |
| `/properties/items/items/properties/status/description` | 删除 |

## 5. 保持的输入、结果与真实语义

结构相等检查覆盖 canonical 和 lowered schema：排除 schema 节点中的 description/title/default annotation 后，修改前后结构完全一致。保留 type、required、additionalProperties、enum/const、pattern、nullable、items、uniqueItems、min/max 等全部约束。脚本的比较访问 schema 位置，既不删除 properties 映射内的同名业务键，也不改 default/const 中的普通 JSON 数据。

| 工具/范围 | 现行语义与 owner，实施时保持 |
| --- | --- |
| spawn_agent / create_agent_tasks | `KernelSubagentManager._spawn` / `_create_agent_tasks`；前者将单任务归一化后走后者。只要求 task，省略 profile 为 general_worker、context 为 none；省略 model 继承 parent model 和 reasoning，明确 model 但省略 reasoning 取目标默认。每批 1–16 项、每任务依赖/材料各最多 16、每批最多 64 条依赖；这是既有每次操作边界，不是总任务数限制。任务排队保留。 |
| context | `_parse_context` 与 `_create_agent_tasks`；none 仅 mode；last_n 为 mode+turns、turns 1–3，复制 ROOT 最近公共对话而不含 tool calls/results；worker_history 为 mode+task_id，来源需已启动、已结束且有可读历史。当前 canonical 验证仍拒绝多余分支字段和 null context；parser 的宽松内部默认不用于扩大公共契约。 |
| model.reasoning | `reasoning_selection_from_dict` 与 launch preparation：effort 为 kind+value，value 在目标列明时可 null；toggle 为 kind+enabled；budget_tokens 为 kind+tokens。reasoning 本身不是 nullable，省略才表示默认。 |
| identity / dependencies | `task_key` 大小写敏感；`task_name` 归一化为 key 与 label；label/display_role 是显示文字。depends_on 使用本批 task_key 或先前返回的完整 task_id，成功才放行并提供结果；material_task_ids 提供同会话已结束任务结果或公共失败材料，不增加成功依赖。 |
| list_agent_models / list_agents | `KernelSubagentManager.invoke` 的模型列表分支及 `_list`；模型列表返回 models，任务列表返回 tasks、计数和 next_cursor。max_items 1–50、默认 50；include_dependencies 默认 true；游标与这两个参数绑定。结果没有完整 worker transcript。 |
| send_agent_message / stop_agent | `_send_message` 接受 ACTIVE worker，返回 queued/task_id；消息被接受和被阅读是不同阶段。`_stop` / `cancel_task` 返回最终状态，已结束任务不重新开始；取消不会撤销已经发生的副作用。reason 是 call 内的取消说明，不作为 worker 消息投递。 |
| wait_agent | `_validate_subagent_tool_arguments` 与 `_wait`：timeout_seconds 0–300、默认 30；指定 task_ids 为 1–16 个唯一完整 task ID，settle 默认 all；不指定 task_ids 时只能有 timeout_seconds。状态终结包含失败/取消。返回 outcome 和 satisfied/pending IDs，任务结果由现有 completion 消息路径送入 ROOT。timeout 不停止任务。 |
| report_agent_result | `_validate_subagent_tool_arguments`、`conversation_kernel/tool_execution.py` 的专门 completion 分支；summary 必填，summary UTF-8 与 canonical data JSON 共用 16384 字节；output_preview 32768 UTF-8 字节；diagnostics 最多 32 项、单项 canonical JSON 8192 字节、数组 65536 字节。保留 sole-call settlement。 |
| enter_plan / ask_plan_question | `PlanToolBatchCoordinator.accept_batch` 及 `primitives/plan_workflow.py` 的 `extract_plan_entry_reason` / `parse_plan_question_arguments`；单工具控制批次，进入 read-only planning；问题答案成为已有 tool result。options 省略或 [] 时 allow_free_text 必须 true；否则 2–3 项、label 唯一、最多一个 recommended=true。 |
| exit_plan | `parse_plan_exit_arguments`、`PlanToolBatchCoordinator.accept_batch` 和 repository plan owner；省略、null、空白 plan 表示无 proposal 退出并按 entry 保存权限自动续行；非空 plan 产生 review。approval/revision/cancel 保持各自后续行为。summary 只是概览；完整 plan 才是实施内容。 |
| todo | `parse_todo_replacement`、`DirectKernelToolPort` 与 `TodoRunStateOwner`；完整替换、最多 16 项、最多一项 in_progress；text 唯一、NFC、安全单行、无首尾空白、最多 192 UTF-8 字节，整个 canonical 快照 4096 字节。空数组清空；ROOT idle 清空而等待用户输入保留。 |

schema 的 maxLength 是字符约束；现有 runtime 对多处文本另外实施 UTF-8 字节边界。本文保留双方，字段文字只在影响构造输入时明确 UTF-8；不把字符数假装成字节数。未改动的 task_id、label、reason 等原有 runtime byte 验证也继续存在。

### 删除的是重复指导，不是移走结果契约

本次**不迁移任何说明到运行时结果，不新增任何结果消息**。结果结构与生成 owner 全部不改；下表用于区别文字去重和功能删除：

| 原说明内容 | 定稿处理 |
| --- | --- |
| 七处重复 ROOT 与运行权限说明 | 全部删除，保留现有 SYSTEM 中唯一公共规则，不修改权限 owner |
| task/profile 名称解释、正则读法和字段 optional 复述 | 保留目标、工作方式与大小写含义，格式与必填性留给 schema |
| 每个 context 分支重复适用条件 | 原封闭分支表达字段组合，context 说明表达历史来源及省略含义 |
| wait 根说明与字段说明重复 settle 关系 | 移至同一函数正文两行；超时默认值进入 schema，0 含义在字段 |
| queued 再次投递禁令 | 正面说明 accepted-for-delivery 与稍后阅读，删除重复劝告；实际投递路径不变 |
| 反复告诫任务太大、例行选择不等于需要 Plan | enter_plan 直接写明用户审阅或重要未决决策这个选择条件 |
| todo 不用于简单任务、非 persistent backlog 等长说明 | 直接说明当前任务完整快照及 idle 生命周期；保留与 Tasks tab 的区别 |
| report 反复要求做完才报告、解释每个可选字段 | 函数保留完成与唯一调用行为；字段保留结果含义和不可由 schema 表达的合并字节预算 |

因此没有以“以后会给错误提示”作为删除前置调用知识的理由。stop 的副作用范围、wait 的结束条件、Plan 对控制流的改变都仍在调用前可见。运行时现有错误、status/outcome、补充 conversation 消息继续承担实际发生情况的输出。

## 6. 静态计量

同一 `PulsaraHeuristicTokenEstimatorV3.estimate_json` 计量 Responses `tools` 数组，包括 type/name/description/parameters/strict:false。各工具列是一个元素的数组；分组及全目录总数直接估算整个数组，不把已经舍入的单项估计相加。全目录只替换本组 12 项，其他 28 项来自当前生产 catalog。

| 工具 | 当前 | 候选 | 减少 |
| --- | ---: | ---: | ---: |

| ask_plan_question | 513 | 357 | 156 |
| create_agent_tasks | 1211 | 1002 | 209 |
| enter_plan | 294 | 175 | 119 |
| exit_plan | 424 | 242 | 182 |
| list_agent_models | 117 | 86 | 31 |
| list_agents | 281 | 213 | 68 |
| report_agent_result | 328 | 287 | 41 |
| send_agent_message | 244 | 186 | 58 |
| spawn_agent | 962 | 786 | 176 |
| stop_agent | 213 | 176 | 37 |
| todo | 424 | 253 | 171 |
| wait_agent | 390 | 283 | 107 |
| **12 工具完整数组** | **5396** | **4040** | **1356** |
| **40 工具完整目录，仅替换本组** | **22824** | **21468** | **1356** |

本组下降 25.13%；全目录下降 5.94%。分组与全目录均独立计量，本稿两者减少量恰好都是 1356；不是用单项舍入值相加得到。目录是 40 项静态集合，不等同每个会话实际安装的 scope-specific surface；这些数字也不是 provider 实收 usage。

认知上的预期收益是：区分任务创建/继续、依赖/材料、消息排队/已阅读、等待终结/任务成功、计划提案/开始执行；这些关系一次陈述且邻近输入决策。代价是读者仍需结合 schema 理解 context/reasoning 的字段形状，而 flat wait 仍是超集，需要本地 exact validator。token 缩减只证明固定输入变小，不能证明模型已经更容易理解或首轮正确率提高。

## 7. 实施步骤与必要检查

1. 在 builtin_catalog.py 逐项应用第 3–4 节，删除已无引用的 `_SUBAGENT_CONTEXT_TURNS_DESCRIPTION` 和 `_ROOT_ORCHESTRATION_PRECONDITION`。保留既有 parser、handler、权限、result owner 和 lowerer。四个新增 default 仅为当前默认行为的 annotation。
2. 用此目录 measure.py 及候选 JSON 对照最终 provider 定义。measure.py 已固定读取实施前保存的 baseline_catalog.json，再经当前未改动 lowerer 生成旧值和候选；不会把实施后的生产 catalog 当作旧值。当前生产与 proposed_tools.json 另作逐项精确比较。不新增版本兼容或 fallback。
3. 只对已知关系核验：wait 有/无 task_ids 与 settle 的合法组合；context 三分支及 reasoning 三分支、effort.value=null 和省略 reasoning 的区别；Plan 空 proposal 与 optionless question；todo 空列表与 UTF-8/快照边界。现有测试对应本组契约，文本断言应依据正面语义更新，不能降低实质断言。
4. 新工具文本只随 cold epoch 或显式采纳的 compaction successor 安装。既有 epoch 的 SYSTEM/tools 和 append-only messages 不在本变更中重新编写。使用既有 prefix 连续性检查，不新增 rebase 边界、事件、凭据设置或计量框架。

本轮已执行：24 个 canonical/wire 候选 schema 自检、前后验证结构相等检查及静态估算；相关既有单元用例 **71 passed、49 deselected**，结果保存在 focused-tests.txt。最初未设 PYTHONPATH 的 pytest 启动失败于 `ModuleNotFoundError: pulsara_agent`；以仓库 `.venv` 加 `PYTHONPATH=src` 重跑的结果见该文件。它是环境装载修正，不是测试跳过或断言修改。

复现命令：

```sh
.venv/bin/python output/tool-clarity-specs-20261026/orchestration/measure.py
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_builtin_default_null_contracts.py tests/test_lightweight_todo_refinement.py -k 'subagent_context_public_variants or subagent_reasoning_exact_shape or wait_join_shape or todo_complete_snapshot_contract_and_byte_truth or todo_text_accepts_exact_utf8_boundaries or todo_item_and_aggregate_bounds_are_independent or todo_invalid_candidates_fail_before_owner_mutation'
```

不把新的在线实验、全量长时程 dogfood 或额外审批框架设为文字清理的前置门槛。本轮未在线验证候选，这是测量的明确界限；以后在真实使用中出现具体误选或误填时，按对应关系修正文字即可。

## 8. 交付与未决项

- `output/tool-clarity-specs-20261026/orchestration/proposed_tools.json`：完整最终 12 工具 Responses 数组。
- `proposed_canonical_schemas.json`：精确 canonical 输入定义。
- `current_tools.json`：本轮现行 wire 定义。
- `field_edits.json`：所有字段修改的旧值/新值及 JSON Pointer。
- `measure.py`、`measurement.json`：可复现静态测量与结构核对。
- `focused-tests.txt`：本轮与现有语义相关的测试结果。

本组已完整落地。bundled Skill 的任务引用已改为完整返回 task_id；没有增加 ID 格式或 runtime 行为。生产定义、候选 JSON、表格与函数定稿逐项一致。全目录聚合与最终汇总由 ROOT 统一完成。

## 9. bundled pulsara-subagent Skill 的分工与同步

已完整阅读 `src/pulsara_agent/bundled_skills/pulsara-subagent/SKILL.md`。该文件没有需要进一步打开的引用文档；它当前提供任务/worker 区别、选择下一步动作表、上下文与材料选择、worker_history 后续分支、model/reasoning 选择、典型调用和结果收集。它是调用时可发现并读取的工作流指南，不应假定每次模型已读取，也不能把它的长文视作固定工具定义之外的免费上下文。

工具中保留两处简短发现入口：spawn_agent 与 create_agent_tasks 的 “See the pulsara-subagent Skill for workflow examples.”。工作流示例、如何选择足够上下文、跨工具续做方法的展开由 Skill 负责；具体调用前必须知道的默认继承、无历史省略语义、context/模型变体、成功依赖和失败材料的区别、精确 ID、wait 条件及 completion 输出位置仍直接留在工具中；ROOT 可用性由现有 SYSTEM 直接提供。Skill 的存在不作为删除这些必要事实的理由。其他单操作工具直接给够下一步信息，不重复要求先读整份 Skill。

与本组工具清晰化同一变更同步以下五处，其他 Skill 原文及 YAML frontmatter 保持不变。只修正 ROOT 称谓、减短可用性长段，并修正实际会导致错误输入的重复 task: 前缀说法；实验模型、兼容性调查和 token 计量不写入 Skill。以下给出四段完整英文替换稿及一处整句删除。

### Task and subagent: assignment paragraph

```text
The ROOT agent assigns tasks, and workers execute them and return results.
Delegation tools belong to the ROOT agent. A follow-up creates a new task and
worker execution, including when it inherits an earlier worker's conversation.
Use the returned task_id to wait, send guidance or cancel that execution.
```

### Task and subagent: availability paragraph

```text
ROOT orchestration tools, including lists and waits, are available when the
current run's effective_mode is "bypass-permissions". Plan approval follows its
own permission workflow and does not change this requirement.
```

### Supply just enough context: last_n bullet

```text
- **`last_n`**: add 1–3 recent ROOT conversation turns using `turns`, excluding
  their tool calls and results. Include required tool evidence in the task.
```

### Common calls: dependency-reference paragraph

```text
Within one `create_agent_tasks` call, dependencies use another item's exact
`task_key`. Keys preserve case. For a prerequisite from an earlier call, copy
its complete returned `task_id`, including its existing `task:` prefix.
Labels are for display. Use `list_agents` to recover task IDs.
```

### Choose the next action: remove redundant conclusion warning

删除以下整句，无替换文字；前后的后续任务路径与分支事实保留。

```text
Do not conclude that further work is impossible merely because the old task ended.
```

Skill 独立计量：`PulsaraHeuristicTokenEstimatorV3.estimate_text` 对包括原有 YAML frontmatter 的整个文件计数，**1916 → 1830，减少 86**。它是按需读取的文本，与 Responses tools 数组口径不同，**不加进第 6 节的 40 工具总数**。frontmatter 的 Skill catalog 描述未变，工具目录没有额外隐藏收益。

完整候选为 `output/tool-clarity-specs-20261026/orchestration/proposed_pulsara-subagent_SKILL.md`；`skill_measurement.json` 保留四个原段与替换段及终审删除项，`measure_skill.py` 可复现精确替换及独立估算。实施 owner 是上述 bundled Skill 文件，无 runtime 或 Skill discovery 机制变化。验证只需核对四处替换和一处删除、task_id 引用与现有 parser 一致以及剩余内容原样保留；不用另建 Skill 评测框架。

```sh
.venv/bin/python output/tool-clarity-specs-20261026/orchestration/measure_skill.py
```

## 10. 实施记录

本组现已完成以下生产修改：

- `src/pulsara_agent/capability/builtin_catalog.py`：12 个 descriptor 的函数/字段说明、两个共享常量删除、4 个已有运行时默认值 annotation。使用局部 hunks 修改，保留其他组同步编辑的内容。
- `src/pulsara_agent/bundled_skills/pulsara-subagent/SKILL.md`：四段明确替换与终审指定的整句删除；完整返回 task_id 与 ROOT 称谓已同步。

最终 critic 的两处精简均已落地：allow_free_text 的说明以 `Required true when options is omitted or empty.` 结束；Skill 删除预设“旧任务结束就无法继续”的告诫。原有 worker_history 后续任务、分支及共享工作目录事实保留。

生产 12 项经现有 lowerer 导出的 Responses 定义与 `proposed_tools.json` 逐项精确相等，canonical/wire 验证结构与冻结旧基线相同；Skill 正文与完整候选精确相等。没有新增测试或改变既有断言、skip、xfail。既有聚焦测试覆盖 context/reasoning、wait 的字段关系、todo 的字节/快照边界、Plan 问题与无 proposal 退出，实际命令：

```sh
PYTHONPATH=src .venv/bin/python -m pytest -q tests/test_builtin_default_null_contracts.py tests/test_lightweight_todo_refinement.py tests/test_round4_plan_workflow.py -k 'subagent_context_public_variants or subagent_reasoning_exact_shape or wait_join_shape or todo_complete_snapshot_contract_and_byte_truth or todo_text_accepts_exact_utf8_boundaries or todo_item_and_aggregate_bounds_are_independent or todo_invalid_candidates_fail_before_owner_mutation or question or exit_plan_empty_union or root_plan_tools'
```

结果：**89 passed、60 deselected，0.87 秒**，见 `implementation-tests.txt`。`git diff --check` 在本组两个生产文件通过。此前 71 项结果属于规格阶段；本节记录实际落地后的检查。

`measure.py` 与 `measure_skill.py` 使用实施前保存的 `baseline_catalog.json` 和 `baseline_pulsara-subagent_SKILL.md`，保留 before 的真实口径。完整 39 工具的合并计量与系统级最终核对由 ROOT 汇总，不拿本组独立替换的 40 工具数字代替合并结果。本组没有待解决的阻塞项，也没有新增 provider 实验。
