# Pulsara Hook 独立消息修订草案

状态：Hook 独立消息投影及候选隔离已接入生产代码，已完成 hardcut 跨层验收；正常应用尚未重启。本文件确定给模型阅读的 Hook 附加文字如何承载和交付；输入候选隔离沿第 3 节的当前 owner 落实。Hook 的阻止、权限裁决和续接控制保持现有底层语义；PreToolUse 不修改工具参数。普通 runtime observation、压缩与 Plan 续接沿[Runtime Observation 修订草案](PULSARA_RUNTIME_OBSERVATION_REVISION_DRAFT.zh.md)；Hook 执行反馈独立交付，安装、移除、信任及启停均不产生 runtime observation。

## 1. 模型可见消息

Hook 附加文字使用独立的 pulsara_hook_context 外壳，不进入 pulsara_runtime_observation。删除旧 HOOK_CONTEXT source，其执行反馈按事件位置交付。外壳位于模型消息正文中，不是 provider 请求 API 新增的顶层参数：

```json
{
  "pulsara_hook_context": [
    {
      "event": "PreToolUse",
      "text": "修改前先阅读 CONTRIBUTING.md。"
    },
    {
      "event": "PostToolUse",
      "text": "修改后请运行相关测试。"
    }
  ]
}
```

| 字段 | 含义 |
| --- | --- |
| pulsara_hook_context | 本次交付的 Hook 附加文字数组，按实际事件和处理顺序排列 |
| event | 既有 Hook 事件的公开名称，例如 PreToolUse，不带内部 Event 后缀 |
| text | 经过现有秘密清理后的附加文字，或既有续接控制需要告知模型的原因 |

每项只使用 event／text。不增加 source、presence、status、Target、定义摘要或内部执行序号；确需说明来源时，在 text 中使用已有来源标签，不另建来源对象。普通附加正文中，多个处理器的输出分别保留，重复文字也不因内容相同而合并成一次事件；Stop／SubagentStop 的控制原因只取既有规则最终胜出且实际采用的一项，不逐个展开所有控制原因。

Hook 文字继续作为运行时附加消息交付，沿既有 provider 消息角色和适配路径处理；即使编码为 user 消息，也不成为新的真实用户输入。它不能自行改变本轮权限、工具可用性或任务要求，实际控制由第 4 节的原 owner 执行。

没有有效正文时不发送空数组、空壳或“本次没有 Hook 输出”的占位消息。stderr、systemMessage 和内部诊断继续走现有诊断路径，不因调整外壳自动进入模型正文。文本输出不变成工具结果，不增加 12k／NEXT 读取链路。

## 2. 全部事件及交付位置

现行共有 11 种事件：9 种基础事件，以及 SubagentStart、SubagentStop 两种子代理事件。沿用现有触发、匹配、执行及作用域，不为消息展示增加事件。

| event | 触发及实际用途 | 模型附加文字的交付位置 |
| --- | --- | --- |
| SessionStart | 当前生命周期定义的启动点，包括压缩后的 source=compact；保留已有阻止语义 | 同步或已就绪的有效文字随启动点之后首次实际模型调用尝试交付；后台晚到沿第 3 节，压缩后的新输出不回填旧消息 |
| UserPromptSubmit | 对应输入提交时的检查；保留现有阻止语义及实际输入来源 | 同步或已就绪文字随实际采用的对应输入在同次模型调用中尝试交付；后台晚到沿第 3 节，仍须通过原输入采用关系，不提前交给别的 turn |
| PreToolUse | 工具执行前检查或阻止，参数保持原请求 | 附加文字在该工具结果或拒绝结果之后、后续模型调用前尝试交付；不插到模型生成该调用之前；Plan 跨 turn 处理沿第 4 节 |
| PermissionRequest | 由原权限流程处理允许、拒绝及原因 | 不产生普通附加正文，不为该事件发送空 Hook 消息；裁决结果沿既有权限／工具结果路径表达，不承诺新增自定义原因原文转发 |
| PostToolUse | 工具执行和结果结算后的反馈 | 有有效附加文字时，在对应工具结果之后、后续模型调用前尝试交付；Plan 跨 turn 处理沿第 4 节 |
| PreCompact | 压缩前检查及阻止 | 不产生普通附加正文；保持既有压缩控制和诊断路径，不发空消息 |
| PostCompact | 压缩采用成功后的检查及续接阻止 | 不产生普通附加正文；可以阻止采用后的继续，不回滚已成功的压缩；保留诊断，不发空消息 |
| Stop | 模型准备结束回复时，决定结束或按既有规则继续 | 只有实际决定继续、且确有后续模型调用时，交付说明此次 Hook 续接原因的文字；不把单纯结束决定另发给模型 |
| SessionEnd | 会话结束时的清理及诊断 | 不为展示 Hook 输出再次调用模型，不发空消息，不复活已结束任务 |
| SubagentStart | 子代理的对应启动点；保持附加上下文语义 | 同步或已就绪文字随对应子代理启动后的模型调用尝试交付；后台晚到沿第 3 节，不转发为 ROOT 的新用户输入 |
| SubagentStop | 子代理准备结束回复时的续接／结束控制 | 实际继续时，将原因交给对应子代理的后续调用；不转发为 ROOT 的新用户输入 |

SessionStart、SubagentStart、UserPromptSubmit 继续支持现行允许的普通 stdout 正文及正式 additionalContext。PreToolUse、PostToolUse 的附加正文继续按现有正式输出解析；不把这些事件的任意 stdout、控制 JSON 或 stderr 都当成模型上下文。UserPromptSubmit 的名称不改变其已有输入来源范围，也不把计划续接、定时或其他运行时来源冒充为人类消息。

事件名称与放置时机不等于模型提前知道输出。尤其是 PreToolUse：控制先作用于本次工具执行，但模型通常在取得结果的后续调用才看到附加文字；不为展示文字额外插入一次模型调用，也不暗示模型生成本次参数前已阅读该文字。

## 3. 顺序、后台输出与前缀连续性

同一次模型输入可以合并多个当前可交付的 Hook 项；同批按既有事件、来源和处理器顺序排列，保持相应 session 归属。后台完成先后可能跨越多个投递批次，不承诺跨批的全局事件顺序，也不等待较早事件补齐。工具调用、对应结果及附属内容仍保持原有完整关联，不为安排 Hook 文字拆散 provider 要求的工具交互。

后台 Hook 晚到的有效文字继续进入既有作用域缓冲，在到达后的下一次正常模型请求准备／安全点尝试追加，不等待后台任务赶上首次调用。没有下一次调用时，不因为暂存了文字而主动发送 steer、唤醒空闲模型或额外开启 turn。已关闭作用域的晚到处理、取消和清理沿现有 owner 生命周期，不新增跨重启恢复承诺。

输入 Hook 的同步和异步文字共用原输入候选的采用关系。候选尚未采用时，正文只归该候选暂存，不能直接进入可被活动 turn 消费的 session 缓冲；输入被拒绝、取消或确认未执行时，精确清理该候选的正文和晚到结果，不影响其他输入。排队输入必须在启动 FULL 确认且实际可执行后才激活；启动失败或执行前中断不能留下已转移正文。异步任务若在激活后完成，仍须能交付，不能永久滞留候选桶。

上述隔离必须覆盖后台完成先于同步拒绝、队列泵启动先于提交调用方返回，以及失败重试仍有旧后台任务等情况。复用原进程内输入候选与接纳 owner，保持每次实际输入尝试的归属，不仅凭相同队列 ID 接受旧尝试的结果；不增加永久登记、投递回执或重新执行 Hook 的任务。

一旦输入已确认采用，正常完成后的晚到处理仍沿现有 live scope 的尽力交付。普通后台输出的作用域由 Host／workspace／session 拥有，不按每个 turn 重新建立或在普通 turn 结束时全部清空；因此已采用输入或工具的后台输出可能进入同 session 的后续 turn 或 Plan 续接。这不同于尚未采用的队列 B 输出提前泄露给活动 A；后者必须阻止。

本方案保持现有尽力交付：下一正常安全点是一次交付尝试，不是最终必达承诺。待发文字按原逻辑单次取出后，编译未选中、准入失败、超时或压缩后的可选 Hook 输入准备失败时，可以舍弃并继续原正常流程；不自动放回队列、不重做 Hook，也不新增恢复或确认机制。秘密清理失败及超额省略沿原 owner 处理。

已交付内容不在普通工具续跑中反复重发。既有 epoch 内 SYSTEM 和 provider tools 保持字节一致，messages 只追加；后台输出不能回插到已经安装的用户输入、工具调用或结果前面。新 cold 上下文及明确采用的 compaction successor 才按各自允许的边界建立新输入。

## 4. 底层控制与模型文字分开

本文件不把 Hook 的正式输出协议简化成 event／text。独立外壳只负责模型可读文字，以下控制仍由现有解析器、dispatcher 和业务 owner 执行：

PreToolUse 可以检查即将执行的参数、阻止调用并说明原因，不替换工具参数。需要调整时由模型显式提交新调用；工具自身的参数校验、默认值、路径解析和源码冻结仍沿既有 owner 处理，不新增有效参数副本。未支持的改参输出沿现有严格解析与诊断处理，不做兼容采用。

| 控制 | 保持的行为 |
| --- | --- |
| 阻止执行 | PreToolUse 等事件的实际阻止决定仍在原安全点生效，不执行被阻止操作；原因沿既有工具结果、输入拒绝或生命周期结果表达 |
| 权限裁决 | PermissionRequest 继续由原权限流程处理；不新增 PreToolUse 的放行能力、自定义原因原文转发或附加正文，不让文字替代确认或提升权限 |
| 续接控制 | Stop／SubagentStop 的结束优先级、既有次数保护及取消结算保持现状；只投影最终胜出且实际采用的续接原因，结算时由真实 steer 等既有条件抑制的续接不另发原因 |

成功进入／退出规划并完成原 turn 时，同步 PreToolUse 正文按现有路径退役，PostToolUse 附加正文不自动转发到后继 turn；runtime 控制的结束、新 turn 和恢复权限均保持原语义，不因统一展示外壳扩大跨 turn 转发。

同时保留现有来源解析、启用设置、信任、作用域、凭据秘密清理、执行超时与副作用结算。修改展示外壳不重新授予信任、不删除管理能力，也不改变已接纳操作的结果。阻止原因已经由工具或输入结果表达时，不为了新消息格式额外复制一条控制通知。

## 5. 与 observation、压缩和 Plan 的关系

runtime observation 按所有新 turn 首次输入、用户主动 steer 及活动 turn 压缩恢复三个入口，沿各来源既定规则组织。Hook 附加文字按实际事件交付，可以出现在工具后的续跑或其他非人类输入触发的调用中，不必等待下一条用户消息，也不因附加文字本身触发 Skill 或其他能力通知；实际开启新 turn 时，仍按该入口发送运行信息并检查相关能力净变化。

TURN_CONTEXT、PLAN_MODE、APPROVED_PLAN 的发送时机和位置保持[既定规则](PULSARA_RUNTIME_OBSERVATION_REVISION_DRAFT.zh.md#6-本轮运行信息与-plan-observation)：活动 turn 压缩恢复对应运行信息；批准后的执行续接携带完整获批计划；已结束 turn 的空闲压缩不提前注入 Runtime 交接。Hook 附加文字不并入这三类块，也不成为 TODO／PCA／待读 Skill 旁的固定交接清单。

此前已交付的 Hook 文字正常参与历史压缩，由压缩手册保留仍相关的信息，并按正常规则保留近期原始交互；不另行恢复全部历史 Hook 原文，不重复执行历史事件。活动 turn 压缩采用后，在继续调用前执行既有 SessionStart(source=compact)；空闲压缩只标记该启动边界，等下一次实际输入准备时才执行，不为了 Hook 提前开启模型调用。新产生的有效输出按第 2 节单独尝试交付，属于新事件反馈，不是重放旧输出。

Hook 安装、移除、信任、撤销信任、启用和停用均不主动通知模型，不初始注入 Hook 目录，不在 pulsara_runtime_observation 中发送任何 Hook 目录、变化或状态信息，也不为这些变化新建 Hook context 通知。模型主动查询或管理 Hook 时，仍正常返回对应工具结果；实际生效规则保持，不等待通知。

## 6. 实施范围与核对

实施时删除旧 HOOK_CONTEXT source 及其专用注册、采集、投影和消费路径，Hook 附加文字统一接入独立的 pulsara_hook_context。不保留空 source、别名或双轨外壳，也不将同一段附加文字同时放进两类消息。原 Hook 执行与上下文缓冲 owner 继续复用；以同一份有效输出内容产生 event／text 列表，不另外维护相同正文的 FULL／COMPACT 副本。

现行 additionalContextLimit 的单处理器额度、整批暂存边界及超额省略行为尚未在本次讨论中重新决定；本方案不顺带删除或新增这些限制，也不以换外壳假称已解决内容丢弃。实施前沿其实际 owner 核对封装计量；这些额度的后续取舍单独讨论，不能误删真正的输出解析、执行或 provider 资源边界。

迁出旧 source 时同步调整 runner 的响应后输入预算预留、Hook 正文上界和最终 wire 计量，计算 event／text 的真实 JSON 转义及消息封装；不继续叠加旧 observation 外壳，也不把尽力交付改成必选正文或重试队列。

| 当前接点 | 修订或保留范围 |
| --- | --- |
| [事件与控制类型](src/pulsara_agent/hooks/contracts.py)及[输出解析](src/pulsara_agent/hooks/output_parser.py) | 沿用 11 类事件及实际允许的正文／控制字段，不为模型展示修改正式控制协议 |
| [Hook dispatcher](src/pulsara_agent/hooks/dispatcher.py) | 保留匹配、执行、控制汇总和后台处理；有效附加文字仍由其产生 |
| [附加上下文 owner](src/pulsara_agent/hooks/context.py)及[输入接纳](src/pulsara_agent/conversation_kernel/host_prompts.py) | 复用原 owner，闭合异步输入候选绑定、拒绝／取消清理和 FULL 后激活，已激活后的晚到正文仍可交付；保留作用域、顺序和秘密清理 |
| [来源采集](src/pulsara_agent/conversation_kernel/context_sources.py)、[输入编译](src/pulsara_agent/model_input/compiler.py)与[模型输入准备](src/pulsara_agent/conversation_kernel/provider_dispatch.py) | 删除旧 HOOK_CONTEXT source 及其专用生产消费路径；独立 Hook context 按事件在合法追加位置交付，不改已安装前缀 |
| [生命周期 Hook](src/pulsara_agent/conversation_kernel/runner_hooks.py)、[工具执行](src/pulsara_agent/conversation_kernel/tool_execution.py)及[输入接纳](src/pulsara_agent/conversation_kernel/host_prompts.py) | 保持控制和结算，只衔接新的模型可读消息 |
| [runner 响应后预算预留](src/pulsara_agent/conversation_kernel/runner.py) | 替换旧 _hook_output_source_upper 包装计量，核对新 JSON 正文、预留和实际 wire 字节／token 一致 |

行为核对应覆盖：启动／输入／工具前后输出的实际顺序，ROOT 与子代理归属，多处理器同次输出和同文不同事件，PreToolUse 阻止及附加文字、PermissionRequest 裁决、Stop／SubagentStop 续接和结束不变，无正文事件不发空消息，排队输入未采用不交付，后台晚到不唤醒或回插历史，实际秘密不进入正文，以及压缩、Plan 续接与同 epoch 前缀连续性保持。保留有意义的控制与交付验证，不新增只证明旧字段不存在的测试。

输入隔离另覆盖：活动 A 期间排队 B、B 的后台正文先于／晚于采用到达、同步拒绝与取消、启动确认失败及确认后执行前中断、相同输入 ID 的失败尝试重试、scope 关闭，以及已成功采用后后台正常晚到。压缩另覆盖 active／idle 的 compact 启动时机、可选输入准备失败舍弃与不重做；Plan 另覆盖其既有参数和跨 turn 正文边界。
