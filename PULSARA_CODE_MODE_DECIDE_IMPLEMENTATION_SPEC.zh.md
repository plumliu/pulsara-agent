# Pulsara Code Mode 语义判断与代理工具入口实现规范

日期：2026-11-07。状态：已实施；本文规定当前工作树的接口、暴露矩阵与验收合同，验证结果见第 12 节。

本文规定 Code Mode 中 `tools.decide` 的批量真假判断合同、现有 Decision 配置与调用实现的复用方式，以及 ROOT 和子代理各自的工具入口。ROOT 的工作派发与等待统一使用 Code Mode，原生查询、通信与停止入口保持独立可用；bundled Code Mode Skill 和系统提示同步描述这一完整合同。

上述功能在同一个实现提交中完成：包括生产实现、工具暴露与准入、设置界面、系统提示、bundled Skill 和必要测试。

本文涉及的新增行为以本文为准。现有 Code Mode 执行、工具结果结算、记忆检索、内核生命周期和 provider 输入连续性合同在未涉及处保持有效。

## 0. 当前实现与所有权

| 位置 | 当前合同 |
| --- | --- |
| `capability/builtin_catalog.py`、`tool_runtime.py`、`tool_execution.py` | ROOT 原生 `run_agent`／`wait_agent`／`decide` 不暴露且拒绝执行；拒绝先于物理 binding 读取，模型误用不会令整轮异常退出 |
| `tool_surface.py` 与 `freeze_code_mode_tool_directory` | JS builtin 目录来自已封存、已授权的执行能力；MCP 目录沿用 prepared 授权范围，provider specs／bindings 仍严格对应 |
| `retrieval/decision.py`、`retrieval/rerank/decision.py`、`conversation_kernel/decision_tools.py` | 唯一共用协议服务分别接受记忆用途与通用谓词适配；普通重排保留自己的 HTTP 路径 |
| `KernelHostSession` | 每会话持有一个 Decision 服务和共享闸门，注入 memory 与 decide；Host 关闭同步封准入并等待物理清理完成 |
| `code_mode/host.py → node/sidecar.mjs → node/worker.mjs → node/guest.js` | 可用性 boolean 随 Cell 准入冻结，排队时保留；Guest 的 `available()` 同步、本地、不可改写 |
| `KernelToolInvocationContext.cell_cancel_event` 与共用 Decision 服务 | 主动观察 Cell Stop；全调用 deadline 只创建一次，排队、各子批及 SDK 清理沿用现有取消和关闭合同 |

实现保留 [停止链路方案](PULSARA_STOP_CHAIN_AUDIT_AND_IMPLEMENTATION_PLAN.zh.md) 的取消传播、精确结算和关闭汇合合同；Decision 的 HTTP 生命周期由现有 SDK 负责。

## 1. 产品目标与范围

为 Code Mode 提供通用的自然语言语义谓词：调用者给出判断条件和文本，Decision 模型逐项返回真假，JavaScript 将这些布尔值用于后续任意计算或控制流程。

普通工具调用和子代理操作已经属于同一个 Code Mode 环境。例如，`read_file`、`search_content`、`run_agent`、`query_agents` 都通过同一个 `tools` 对象组合。`decide` 可以安排在这些调用之前、之间或之后；输入可以来自一个调用，也可以由脚本组合多个来源。工具不按材料来源、代理角色或工作流阶段划分判断用途。

统一的数据流为：

```text
脚本中的文本值 + 自然语言条件 → tools.decide → boolean[]
                                              ↓
                            任意后续计算、工具调用或代理操作
```

结果可以用于筛选、条件分支、循环条件、追加取数、选择后续工具或安排代理工作。脚本可以按已写好的流程继续推进，不需要为了取得一个语义判断而额外唤醒 ROOT 模型。原有消息和输出交付语义保持不变。

“资料筛选”和“代理工作交接”是同一能力的使用示例，均不作为工具的专属用途或优先用途。工具只约定输入和真假输出，不限定调用者可以询问的业务领域或具体语义条件。

范围固定如下：

- 公开入口为 `tools.decide`，使用已有 ROOT、bypass-permissions 的 Code Mode 环境。脚本沿原工具接口调用和衔接子代理工作；本次不变更子代理本身的执行入口，也不新增同名的 provider 原生工具。
- Code Mode 始终提供 `tools.decide` 和它的同步方法 `tools.decide.available()`。后者返回本 Cell 启动时的 Decision 配置可用性；脚本先执行 `if (tools.decide.available())`，为真时调用，否则把材料及待判断的问题交回 ROOT，继续现有任务流程。
- 输入为一个条件和一组文本，成功结果为按输入顺序排列的布尔数组。单条输入也使用数组。
- 使用现有用户级 `Jev-like Decision` 连接，支持已有 `system_one` 和 `openai_decisions` 两种协议。
- Decision 请求只携带本次显式输入和执行该判断所需的固定指令。主会话、记忆、工具目录和 Skill 正文均不自动附带。
- Decision 模型不调用工具，不产生解释正文，不创建 Session、Agent 或 Work。它只承担此次判断。
- 初版只处理文本。图片、视频、音频、文件路径和工具 target 的解析继续由各自工具负责；调用者把取得的文本传入 `inputs`。
- ROOT 创建或续派代理工作、等待代理消息或工作结果，统一调用 Code Mode 中的相应工具；从 ROOT 的原生工具表移除 `run_agent`、`wait_agent`。
- ROOT 保留原生 `query_agents`、`send_agent_message`、`stop_agent`，脚本中也继续提供这三个操作。它们直接进入现有协作后端，执行不依赖 Cell、VM 名额或 Code Mode sidecar。
- 子代理继续使用原生 `query_agents`、`send_agent_message`、`wait_agent`。不向子代理新增 Code Mode、创建代理或停止其他代理的能力。
- 所有 Agent、Work、消息、队列与结果仍由现有协作 owner 管理，Cell 组织工具调用；同一对象不因调用入口改变而改变身份或生命周期。

语义判断模型为可选配置。未配置 Decision 时，ROOT 仍然通过 Code Mode 派发和等待代理工作，三个原生控制入口与子代理通信仍正常可用。

## 2. 公开接口

### 2.1 参数

```ts
interface CodeModeTools {
  decide: {
    (args: {
      condition: string;
      inputs: string[];
    }): Promise<{ decisions: boolean[] }>;
    available(): boolean;
  };
}
```

参数 schema：

```json
{
  "type": "object",
  "properties": {
    "condition": {
      "type": "string",
      "minLength": 1,
      "description": "One explicit yes/no condition to evaluate independently for each input."
    },
    "inputs": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Texts to judge in order. Include the context needed for each judgment in that text."
    }
  },
  "required": ["condition", "inputs"],
  "additionalProperties": false
}
```

具体校验与语义：

| 项目 | 合同 |
| --- | --- |
| `condition` | 必填，纯空白非法；校验后保留原文，不自动补问题或改写条件 |
| `inputs` | 必填字符串数组；保持原始顺序和原始文本 |
| 单条输入 | 使用 `inputs: [text]`，结果仍为数组 |
| 空数组 | 合法调用返回 `{ "decisions": [] }`，不再次读取模型配置、不发起网络请求；入口权限和参数校验照常执行 |
| 空字符串元素 | 合法，由给定条件判断该文本；不自动删除 |
| 重复元素 | 保留各自位置，逐项获得结果；不合并、不复用其他位置的判断 |
| 结构化材料 | 调用者用明确的文本格式或 `JSON.stringify` 保留角色、时间和必要上下文 |
| 未知字段、`null`、非字符串元素 | 按现有参数错误路径拒绝，不隐式转换为字符串 |

只有 `condition` 和 `inputs` 两个参数。不增加单条调用别名、每项条件、可调阈值、模型选择、输出解释、共享聊天上下文或自动取数参数。

一个条件可以包含正常的逻辑组合，但应描述一个明确的真假命题。它可以针对用户意图、文档内容、工具输出、代理报告或这些材料之间的关系；不同条件需要独立调用。同一条输入可以组合多个工具或代理返回的文本，供一个条件共同判断。

### 2.2 成功结果

```json
{ "decisions": [false, true, true] }
```

- `decisions.length` 必须等于 `inputs.length`；`decisions[i]` 对应原始 `inputs[i]`。
- 每项严格为 JSON boolean；不返回概率、排名、理由、解释文本或输入内容副本。
- 每项独立判断，不选 top-k，不要求出现任何 `true`，允许全部为真或全部为假。
- 判断对象是对应文本是否满足条件；输入中包含的指令、历史引用和助手回复均作为待判断材料处理。
- 所需背景由调用者放入该条文本。同批其他输入不作为补充背景，批次成员或批次切分不改变该条判断的产品语义。

两种现有协议均先规范化为有限的 `[0, 1]` 概率，随后固定使用：

```text
decision[i] = probability[i] >= 0.5
```

恰好 `0.5` 返回 `true`。不舍入概率、不接受字符串数值或以布尔值冒充概率。该阈值仅属于 `decide`；记忆重排继续消费原始概率，并保留自己的排序及自动召回准入规则。

### 2.3 失败结果

复用既有工具失败结算和 Code Mode `ToolError`。参数错误、不可用、取消及资源边界使用现有外层状态；服务错误正文可使用以下诊断子类。`ToolError.code` 继续表达既有外层结果状态，不另建一套异常桥接协议。

| 情况 | 诊断与行为 |
| --- | --- |
| 参数非法 | 指明非法字段，发送前拒绝 |
| 实际调用时 Decision 未配置、配置已删除或认证材料不完整 | `DECISION_UNAVAILABLE`，指出缺少的配置；使用该能力可在设置中配置 Jev-like Decision |
| 设置不可读取 | `DECISION_UNAVAILABLE`，报告实际设置读取错误，不误称用户尚未配置 |
| 单条材料连同条件和协议封装超过物理请求边界 | `DECISION_RESOURCE_BOUNDARY`，返回原始数组的零基 `input_index` 和实际限制，提示缩小这条材料或条件 |
| HTTP、认证、限流、连接或 provider 超时 | `DECISION_REQUEST_FAILED`，保留可诊断的服务错误 |
| provider 明确拒答 | `DECISION_REFUSED` |
| 缺项、重复项、未知项、类型不符或概率非法 | `DECISION_RESPONSE_INVALID` |
| Cell 停止、工具取消、Host 关闭或既有调用截止时间到达 | 使用对应的既有停止、取消或超时路径 |

成功需要所有输入均取得合法答案。任一物理子批失败时，本次调用整体失败，不返回成功前缀、不补 `false`、不跳过该项。已经发生的外部请求按实际执行情况保留普通诊断；不为部分成功建立恢复游标或持久任务。

错误结果不携带可被误用的 `decisions` 数组。调用者修正原因后可重新调用；实现不自动切换主模型、其他连接或记忆检索 fallback。

### 2.4 提前检查可用性

```js
if (tools.decide.available()) {
  const { decisions } = await tools.decide({ condition, inputs });
  console.log(candidates.filter((_, i) => decisions[i]));
} else {
  console.log({ candidates });
}
```

`available()` 是挂在 `decide` 函数上的无参同步方法，直接返回 boolean，不返回 Promise。它只读取宿主在 Cell 启动时提供的配置可用性，不调用模型、不访问网络、不触发工具结算，也不产生新的调用记录。`decide(...)` 继续只承担执行判断，调用结果仍只有 `decisions`。

JavaScript 中始终可以访问 `tools.decide.available()`，无需通过函数存在性判断来探测配置。配置完整时为 `true`，缺少配置或所需认证材料时为 `false`；设置不可读取时为 `false`，实际读取错误保留在现有宿主诊断中。

该值在本 Cell 内固定，新 Cell 读取最新状态。它用于提前选择处理流程；实际请求仍使用调用时的设置并遵循正常错误处理。`available()` 不执行网络健康检查。

## 3. 复用现有 Decision 配置

### 3.1 唯一配置来源

继续以 `LocalSettingsStore` 中的 `memory_retrieval.decision` 为唯一保存位置，复用现有 endpoint、model_id、shape、authentication 和 api_key 校验、编辑、保存、删除及公开投影。该位置同时服务记忆重排和 Code Mode，不复制出 `code_mode.decision` 或另一套模型路由。

保留这一单一保存位置是本次明确的配置合同；不增加迁移、双读、双写或旧新配置回退。连接支持范围仍为 `system_one`、`openai_decisions`，普通 rerank 连接不能用于 `decide`。

`decide` 的执行权限沿用现有 Code Mode 边界。每次启动 Cell 时，从当前用户设置取得 `available()` 所返回的配置状态；函数目录保持一致：

| 用户设置 | `tools.decide.available()` | 记忆重排 |
| --- | --- | --- |
| Decision 已配置，`ranking_mode=off` | `true` | 关闭 |
| Decision 已配置，`ranking_mode=rerank` | `true` | 使用普通 Rerank 连接 |
| Decision 已配置，`ranking_mode=decision` | `true` | 使用同一 Decision 连接 |
| Decision 未配置或认证缺失 | `false` | 沿现有设置与记忆检索规则处理 |

Embedding 和历史会话检索开关不控制 `decide`。它们仍决定相应取数能力；已有文本可以直接送入判断工具。

### 3.2 设置读取与在途调用

Cell 启动时读取配置完整性，向 Guest 提供一个 boolean，不探测网络、不发送测试请求，也不把凭证或连接对象交给 JavaScript。未配置、认证材料缺失或设置读取失败时提供 `false`；设置读取失败由现有诊断路径保留实际错误，不使其他 Code Mode 工具不可用。

非空调用在参数校验通过后读取一次当前 Decision 连接，取得不可变的完整连接值；随后按该连接的协议完成输入可装载性校验，再进入网络执行。本次调用的所有子批使用该值，排队期间或两批之间不重新选择模型。

配置修改或删除影响下一次实际调用；已准入调用使用已选连接完成，仍服从普通取消和 Host 关闭。运行中 Cell 的函数目录和 `available()` 返回值保持冻结，新 Cell 读取最新配置状态。启动时可用但随后配置被删除的 Cell，在实际调用时收到明确不可用错误。空输入仍可直接返回空数组。配置变更不重建 provider 输入。

记忆检索现有的配置前后校验、失败处理和结果采用规则继续由记忆 owner 负责；通用 Decision 服务不代替这些业务判断。

### 3.3 设置页与连接测试

Decision 配置卡应独立于“启用重排”的展开状态，关闭重排时仍能查看、配置和测试 Decision。复用已有表单和设置 API，只调整必要的组织和说明：

- Jev-like Decision 卡说明“用于 Code Mode 语义判断，也可用于记忆重排”。
- 重排开关和模式选择只表达记忆重排用途；选择 Decision 时使用同一张已配置连接，不再复制一张编辑卡。
- 保存 Decision 配置本身不自动开启记忆重排。不增加新的 `decide_enabled` 开关。
- Decision 的连接测试调用通用判断适配器，用简单的正、反样本验证两种协议；Embedding 和普通 Rerank 测试保留各自路径。
- 设置页继续由用户级 settings owner 提供数据和可用性，不依赖当前会话。

模型可见描述只指向设置中的 Jev-like Decision，不介绍产品 home、环境变量、配置文件位置或凭证内容。

### 3.4 未配置时的正常任务流程

没有 Decision 配置是正常的默认状态。`decide` 是可选的语义处理能力；应用启动、会话创建、Code Mode 启动及其他工具均不要求先配置 Decision。服务组装不为检查可用性而发起模型请求。

每个调用点都通过 `if (tools.decide.available())` 选择处理路径。返回 `true` 时执行所需语义判断，返回 `false` 时将对应条件、材料和已有工作引用交给 ROOT 继续处理。这个可用性判断在 JavaScript 本地完成，不产生工具调用或 provider 请求；循环也可以整体放在同一个 `if` 分支内。

本 Cell 的 `available()` 返回 `false` 时，普通任务继续执行，不反复尝试 `decide` 或为探测配置而创建 Cell。用户配置完成后，新启动的 Cell 使用更新后的可用性状态。实际调用如遇配置变更或服务失败，仍沿普通工具错误路径处理。

脚本保留已经取得的结果、来源及 Agent／Work 引用，按需要输出材料或读取入口供 ROOT 继续处理。未配置不产生语义判断结果，也不自动选择工作流中的 false 分支。已完成的取数或代理工作继续复用。

普通任务不因缺少这项配置而停下，也不把配置请求变成完成任务的前置步骤。用户明确要求使用或配置 Decision 时，再说明具体缺项和设置入口。服务保持明确报错，后续判断和任务推进由主模型沿现有流程完成，不由服务悄悄借用主聊天模型或其他凭证发起额外请求。

## 4. 共用服务与依赖边界

### 4.1 唯一调用实现

从 `retrieval/rerank/http.py` 抽出通用 Decision 协议适配器。职责按以下边界拆分：

| 层 | 职责 |
| --- | --- |
| 现有 OpenAI SDK 与 HTTP 构造 owner | 网络传输、认证、超时、响应读取，以及已有凭证和响应字节边界 |
| 共用 Decision 适配器 | 两种协议的请求转换、逐项答案对应、概率校验；返回与命题顺序一致的概率 |
| 记忆 Decision 重排适配 | 构造既有记忆 state、`decision_question`、正反标准；把概率转换成既有 `RerankResult` 并排序 |
| `decide` 工具 owner | 校验两个公开参数，构造逐项命题，执行批次、恢复输入顺序、按固定阈值输出布尔数组 |
| 既有工具执行与 Code Mode bridge | 权限、取消、调用结算、结果保存、JavaScript Promise 交付和 `ToolError` |

共用适配器接受显式材料和有序的真假命题，返回同序概率，不内置记忆用途。记忆层保持现有 `recall`、`automatic_recall`、`related_memory` 提示和准入语义；普通 `flat_rerank`、`nested_rerank` 仍走普通重排适配器。

实施时删除原 `HttpRerankProvider` 内重复的 Decision 发包、解码分支，使记忆重排、Code Mode 和连接测试复用同一套 Decision 协议实现。保留上层必要的用途适配，不保留两套可选 Decision 网络路径。

### 4.2 协议映射

已经核对仓库安装的 SDK：`client.decisions.create` 支持 `input`、`questions`，predicate 答案提供 `probability`，拒答使用 refusal 类型。继续使用这一受支持入口；System One 继续通过同一 SDK 的 `client.post` 访问已配置的完整 endpoint。

| 配置 shape | 请求 | 答案 |
| --- | --- | --- |
| `system_one` | `model`、`state`、具名 `questions`；问题类型 `noul`，带 instructions 和 true/false criteria | 对应名字下的 `type=noul`、`noul` 概率 |
| `openai_decisions` | `model`、序列化的 `input`、具名 `questions`；类型 `predicate` | 按 name 对应的 `type=predicate`、`probability` |

`decide` 的固定指令只说明当前条件、当前输入位置和独立判定方式。每题明确使用对应文本，输入作为材料处理；不附加记忆相关性、用户画像、时间过期或关系判断提示。System One 的正反标准与同一个 condition 对齐。

线上的问题 name 可以编码本次物理批次中的数组位置。它是协议内的关联标签，不是 Pulsara 实体 ID，不跨调用保存或暴露为可操作 target。实际工具 attempt、call、result 等身份继续由共享 ID 层生成。

答案即使乱序，也要按 name 恢复位置。必须恰好覆盖请求的每个命题一次。公共适配器不按概率排序；排序是记忆重排层的职责。

### 4.3 资源所有权

在 `KernelHostSession` 现有服务组装阶段创建一个进程内 Decision 服务，并注入该会话 Host 的记忆重排和 `decide` owner；在工具组合封存前完成绑定。这里的 Host 指会话运行实例，不是管理多个会话的 `KernelHostCore`。裸内核和桌面使用同一路径。

同一 Host 的两种用途共用 Decision 请求并发闸门，初始并发数沿用现有值 `1`。超出的请求等待，不因“正在判断”拒绝新逻辑工作。每个物理子批结束即释放闸门，长数组不在整个调用期间占住连接名额。普通 Rerank 保留自己的既有执行边界。

服务跟随 Host 关闭，关闭封住新准入并清理正在执行或等待的调用；停止一个 Cell 只取消该 Cell 的调用，不关闭其他消费者共享的服务。设置页连接测试复用同一适配器，在用户级设置服务内管理自己的短期调用，不借用活动会话。

共享服务是 Decision 请求及其闸门的唯一资源 owner。现有 memory port 保留自身查询和业务 task 的生命周期，只取消／汇合自己发起的调用；不能由 memory port 提前关闭其他 Cell 仍使用的服务。Host 关闭须汇合这些调用及 SDK 关闭，不能仅把正在等待的 task 从集合删除便宣告物理结束。不同会话的关闭互不影响。

不引入全局连接注册表、持久任务、逐项事件、缓存、恢复检查点或新的 fingerprint。协议、业务结果和错误沿用已有调用记录，保留实际输入输出以便诊断；仅按现有凭证 owner 清除实际秘密值。

## 5. 批量执行与物理边界

逻辑输入数组可以超过一个 provider 批次。后端按原始顺序拆分，执行完成后合并成一个同长度布尔数组；调用者不需要了解 provider 的批次格式。

初版复用当前 Decision 路径已经采用的物理资源配置：

| 边界 | 初始值 | 作用范围与来源 |
| --- | --- | --- |
| 命题数量 | 每个物理请求最多 20 项 | 现有 Decision／rerank 批量配置，限制一次网络请求的工作量 |
| 请求大小 | 每个物理请求最多 192 KiB | 现有 JSON 请求字节保护，包含条件、问题指令和协议封装 |
| 响应大小 | 每个物理请求最多 256 KiB | 现有 bounded JSON response hook |
| provider 超时 | 每个物理请求 4 秒 | 现有 Decision 网络超时配置；取得并发名额后计时 |
| 同 Host Decision 并发 | 1 | 现有请求并发配置；两个消费方共用 |
| 工具及 bridge 边界 | 使用已有配置 | 已有单次工具截止时间、Code Mode JSON 值、Cell 和结果交付边界 |

这些值是单次物理操作配置，不是工具总调用数、材料总数、任务或 Cell 寿命限制。`decide` 不增加独立的累计上限。也不沿用记忆候选投影的单篇 8 KiB 截断；通用输入受实际封装后的请求边界约束。

执行要求：

1. 发送前完成全部参数校验，并确认每一项能与 condition、固定指令及所选协议封装共同装入一个物理请求。超限项明确报错，不先发送前面部分再发现已知的输入问题。
2. 按输入顺序，以 20 项和实际序列化 UTF-8 字节数共同决定每个子批。保留文本原样，不按估算字符数截断，不把同一条文本拆成多个真假判断再合并。
3. 工具 owner 在本次非空调用的执行准入处，从既有 deadline factory 取得一次 `NONTERMINAL_TOOL_INVOCATION` 截止时间，后续子批共用，不在每批重置。逐批等待共享并发名额；取得名额前和发送前检查取消。排队和 HTTP 执行都观察该截止时间；4 秒 provider 超时只在取得名额后开始，受剩余工具时间约束。
4. 每个子批校验完整响应后，将概率放回原始数组位置。所有子批成功后一次性交付结果。
5. 沿用现有 SDK `max_retries=0`。失败返回正常工具错误，不在服务内增加自动重试链。
6. 明确接入 `KernelToolInvocationContext.cell_cancel_event`、调用者取消及服务关闭：排队时可退出且不发请求；HTTP 在途时取消可取消的 SDK 操作并等待其关闭；批次之间不再派发。停止一个 Cell 不取消其他 Cell 或记忆消费者。远端已接收请求按实际情况结算，不启动新的补偿判断。

当前 `tool_execution.py` 对异步工具执行叶直接 await，并不会自动为每个异步 owner 安装上述 timeout 或监听 Cell 事件。这些连接必须在新 Decision owner 中实现，复用现有 deadline、事件和 SDK 生命周期，不另造停止协议。关闭过程中重复取消仍须汇合同一次关闭，再释放并发名额；保留已有主异常和实际失败诊断。已取得的结果沿既有工具结算规则处理，Cell 已终止时不能据此恢复脚本或启动下一子批。

完整布尔数组的最大编码体积可以在发送前计算。准备阶段应按既有工具结果及 bridge 预算检查能否交付任意合法结果，计入对象封装和布尔数组的最长表示。确定无法交付时，沿现有资源边界报错并提示按来源页拆分调用，不支付模型请求后再发现已知的输出容量问题。这里不设置独立的 `maxItems`，不截掉尾部结果，也不为 `decide` 新建结果分页协议。其他交付故障继续使用既有已接受结果的保存和读取路径。

实现中应将这些 Decision 物理参数归到共用适配器或服务的唯一配置处，避免记忆层和 Code Mode 各复制一份。现有 `NONTERMINAL_TOOL_INVOCATION` 截止时间继续约束一次工具调用；不为 `decide` 增加新的任务级 watchdog 或延长现有截止时间。

## 6. Code Mode 接入与输入连续性

`decide` 是由宿主执行的正式工具调用。通过现有 `CodeModeToolDirectory` 提供冻结 schema 和真实执行 binding，再经 `invoke_code_mode`、普通工具执行叶、结果结算和 bridge 交付。Guest 不直接持有连接配置或凭证，不增加 `fetch`、模块导入或另一条模型请求通道。

初版只在 Code Mode 目录公开 `decide`。不要为了让现有“从原生工具取子集”的目录构造碰巧包含它，而把它顺带加入 provider 原生工具表；应显式将该 Code Mode 专用 schema 和 owner 绑定纳入现有目录构造。继续使用共同的权限校验和执行结算，直接原生调用或子代理调用按不可用处理。

Code Mode 目录始终包含 `decide` 的 schema 和真实执行 binding。每次创建 Cell 时，宿主从当前 Decision 设置取得配置完整性，随启动数据传给 Guest 一个 boolean。在 `guest.js` 冻结工具函数前，将本地 `available()` 方法挂到 `tools.decide` 上，方法和工具函数均冻结；方法只返回这个 boolean。

该 boolean 在 Python Cell 准入时与目录一起冻结，经过 `CodeModeHost.start` 的现有 start 消息、sidecar 排队数据和 worker 的 Guest 安装参数传递。排队等待 VM 的同一个 Cell 不在获得名额时重新读取设置；下一 Cell 才取得新的可用性。只传严格的 boolean，不把连接对象或凭据带入 Node／Guest；所有层保持同一值，不添加缺字段默认值或旧新启动格式兼容分支。

`available()` 不登记为另一个宿主工具，不经过 `invokeHost`，也不另建可用性查询接口、配置缓存服务或能力注册表。复用现有工具函数可携带方法的 Guest 形式，增加本地的只读查询即可。

目录和可用性值随 Cell 冻结，不在运行中更改。一次 `if` 只调用本地同步方法；实际 `decide` 调用仍由宿主读取当前配置并校验。重排开关不参与可用性计算。新 Cell 使用最新的 Decision 配置完整性事实；配置可用性的刷新不修改 provider 原生工具表。

成功值在 JavaScript 中直接是 `{ decisions: [...] }`，没有额外 `.data` 包装。工具结果留在脚本中，只有显式 `console.log` 的内容主动交给 ROOT；正常的失败与停止报告沿既有通道处理。

新工具说明和 Skill 内容只在现有允许的冷 epoch 或显式采用的 compaction successor 边界进入新构建的 provider 输入。已有 epoch 的 SYSTEM、provider tools 保持字节不变，messages 只追加。运行中 Cell 的目录保持冻结，不热补工具，不建立新的 rebase 边界。

建议工具说明：

> Evaluate one natural-language yes/no condition independently for each supplied text using the configured Jev-like Decision model. Returns `{decisions: boolean[]}` in input order. Compose this with any tools or agent operations in the script, using the results for filtering, branching, loops or further calls. Include the context needed for each judgment in that input. Guard calls with `if (tools.decide.available())`; this local synchronous method reports configuration availability captured at Cell startup. Otherwise pass the condition and material to ROOT to continue. Use JavaScript for exact comparisons. Empty inputs return an empty array. Service failures reject the call. Decision configuration is independent of memory reranking.

## 7. 代理工具入口合同

### 7.1 ROOT 编排与独立控制

ROOT 通过 Code Mode 创建、续派、批量安排和等待代理工作，并将这些操作与普通工具、`decide`、JavaScript 控制流组合。一次简单委派也采用同一入口，不再按“简单任务使用 native、复杂任务使用 Code Mode”分流。

ROOT 同时保留原生查询、发消息和停止能力。正在运行的脚本不必预先知道用户随后会提出哪些要求；ROOT 可以在新的模型执行中直接查看实际工作、补充指令或停止选定工作，不必先取得 VM 名额或结束原脚本。

这里调整的是公开调用入口。原有 `run_agent` 的工作准入、`depends_on`、`materials`、代理复用和排队语义保持不变，既有协作服务继续作为唯一执行 owner。

### 7.2 明确的暴露矩阵

以下“提供”均服从已有角色、bypass-permissions 和后端存活检查。Code Mode 列仅指现有 ROOT 的脚本环境。

| 操作 | ROOT 原生 | ROOT Code Mode | 子代理原生 |
| --- | --- | --- | --- |
| `run_agent` | 不提供 | 提供 | 不提供 |
| `run_agent.batch` | 不提供 | 提供既有批量 helper | 不提供 |
| `query_agents` | 提供 | 提供 | 提供，保留可见范围 |
| `send_agent_message` | 提供 | 提供 | 提供，保留目标及活跃工作语义 |
| `wait_agent` | 不提供 | 提供，等待 AGENT 消息或当前绑定工作 | 提供 |
| `wait_agent.works` | 不提供 | 提供，等待确切 WORK | 不提供 |
| `stop_agent` | 提供 | 提供 | 不提供 |
| `decide` | 不提供 | 提供 | 不提供 |
| `decide.available()` | 不是宿主工具 | 提供本地同步方法 | 不提供 |
| `exec_cell`、`query_cells`、`manage_cell` | 提供 | 不作为脚本内工具提供 | 不提供 |

`wait_agent` 的 schema 和执行实现仍为子代理及 Code Mode 所需，不能全局删除。同理，`run_agent` 保留 schema、执行 binding 和业务实现，只移除 ROOT 的原生调用入口。

普通工具、MCP、其他角色工具和权限模式的暴露范围不因本节扩大。子代理仍没有自己的 Code Mode 环境；其消息和等待继续直达既有协作 owner，不能改成由 ROOT 脚本逐条转发。

### 7.3 执行能力与 provider 暴露分离

现有 `CodeModeToolDirectory.freeze` 从 `prepared.model_surface.tool_specs` 筛出 JS 工具。这种构造要在本次硬切中改为从已组装、已授权的实际执行能力及其 schema 形成 Code Mode 目录，再按两种入口各自的暴露规则生成目录。

具体要求：

1. 同一个工具的参数 schema、执行 owner 和业务语义继续由原有 builtin catalog／工具组合持有；原生表与 JS 目录只选择各自需要的可见项，不复制两套 schema 或业务实现。
2. ROOT 原生表不包含 `run_agent`、`wait_agent`、`decide`；ROOT JS 目录仍包含它们及既有 JS 可用工具。子代理原生表继续包含自己的三个协作工具。
3. 调用准入校验角色与真实 invocation source。ROOT 的原生 `run_agent`、`wait_agent` 请求按不可用拒绝；Code Mode 调用同名操作正常进入现有执行叶；子代理原生 `wait_agent` 正常执行。
4. ROOT 原生 `query_agents`、`send_agent_message`、`stop_agent` 直接调用既有协作 owner，不在内部包装成 `exec_cell` 或经 JS 转发。
5. `run_agent.batch` 和 `wait_agent.works` 保留既有 Code Mode 专用语义与校验；不在原生侧添加对应别名。
6. MCP 的冻结签名、实际权限、consumer 绑定和调用引用保持现有合同。目录构造调整不能顺带让未授权的 MCP 或其他工具进入 JS。

通过现有 catalog、surface 与执行准入表达这一矩阵，不新建第二套工具注册表、工具执行器、权限框架或代理调度器。配置、代理发现、运行中的控制操作均不重建 provider 输入根。

`PreparedKernelToolSurface.__post_init__` 当前要求 provider specs 与 execution bindings 名称、顺序和 descriptor 逐项对应。保留这一不变量：不能向这个 provider surface 私塞隐藏的 JS binding，也不能仅从 specs 删除名字。两种目录从同一 sealed builtin composition／实际授权能力选择；JS 目录持有自己的冻结 schema 与原执行 binding，复用现有 access、borrow 和权限检查。`borrow_code_mode_surface` 为两个 helper 派生 binding 时仍须找到实际 `run_agent`／`wait_agent` owner。来源判断使用宿主建立的 invocation source 和 Cell borrow，不接受参数中自报“来自 Code Mode”。

### 7.4 控制入口的独立可用性

原生 `query_agents`、`send_agent_message`、`stop_agent` 保留的产品职责，是 ROOT 在脚本之外直接观察和干预同一批工作。它们是常规入口，不要求先证明某个脚本已经失败。

必须满足：

- Code Mode 所有 VM 名额被正在执行或等待的 Cell 占用时，三个原生操作仍可直接执行，不进入 Cell 排队队列。
- Node runtime 缺失、sidecar 已失效或脚本无法启动时，三个原生操作仍使用正常的协作后端、权限和截止时间；不先启动或探测 sidecar。
- 查询可以发现当前协作中保留的 Agent／Work，沿现有可见范围与分页返回，不要求 ROOT 先从某个脚本的日志中拿到 ID。
- `send_agent_message` 仍只补充目标的活跃工作。目标已完成或不活跃时报告原有状态，不用发消息暗中创建新 Work。
- `stop_agent` 仍支持 WORK 与 AGENT 两种目标；前者停止指定工作，后者停止当前及已排队工作，已完成历史和代理复用能力保持原状。

当前 Code Mode 配置为 4 个活动 VM，等待中的 Cell 也占名额。因此，控制请求需要独立于该资源池。这是保留上述原生入口的具体资源理由；本次不改变 VM 数量，不新增控制专用 VM、优先脚本队列或备用 runtime。

这一合同不将原生 `run_agent`、`wait_agent` 作为运行时失败回退保留下来。Code Mode 不可用时，ROOT 可以直接查询、通信、停止及使用其他正常工具；新的代理工作仍需在 Code Mode 恢复可用后通过 `exec_cell` 派发。

### 7.5 生命周期、消息与结果

Agent 与 Work 继续由原有 owner 管理，身份与执行不归某个 Cell 私有：

- 一个 Cell 可以使用已有 Agent 发起新 Work；其他 Cell 和 ROOT 的原生控制工具可操作同一对象。
- `manage_cell.stop` 停止脚本后续执行与提交，不自动停止已接受的代理工作。需要停止工作时继续使用 `stop_agent`。
- 单个 Cell 结束或失败后，已有工作和实际结果仍可查询；新的脚本可以按真实状态接续。不要仅因脚本失败重复创建代理、重复派发或重做已经完成的工作。
- ROOT 新收到用户要求时，可以直接 native 查询／发消息／停止；如需要给已完成代理追加工作，则通过新的 Cell 调用 `tools.run_agent`，复用实际 AGENT target。
- `tools.wait_agent` 继续等待消息或调用开始时绑定的当前工作；`tools.wait_agent.works` 继续等待确切 WORK，保留 `all`／`first`、停止和中断语义。移除 ROOT 原生等待不会把二者合并成另一种等待协议。
- 普通 Work final 不自动作为新输入送给 ROOT；脚本按需要等待、查询和输出。显式代理消息保留现有活跃轮次交付语义。
- 原生查询结果正常进入发起调用的模型上下文；JS 内部查询结果留在脚本中，按已有输出规则交给 ROOT。调用入口不改变 canonical 记录、工具结果保存或 sidebar 对实际工作的展示。

Code Mode 的持久化、断线及崩溃语义保持现状，不为入口收敛增加脚本恢复、重放或代理恢复机制。

### 7.6 与输入连续性共同激活

在同一个实现提交内完成入口矩阵、目录构造、实际准入、系统提示、Skill 和测试的硬切。移除 ROOT 原生派发／等待路径及对应的新使用指导，不保留配置开关、旧新模式或自动回退分支。

新的 provider 工具表只在已有 cold epoch 或明确采用的 compaction successor 边界构建。已有 epoch 的 SYSTEM、tools 字节和历史消息保持不变；不得为了隐藏两个入口立即重建根。

已安装的旧 schema 若仍列出 ROOT 原生 `run_agent` 或 `wait_agent`，实际调用仍按本规范的角色／来源准入拒绝，并在工具错误中直接指导使用 `exec_cell` 中的相应方法。不要将该请求静默转换成脚本，也不执行旧派发路径。历史已接受调用及其实际结果继续保留，不因暴露变化改写或重新执行。

## 8. Code Mode Skill 与系统提示的使用策略

实施时修改 bundled `pulsara-code-mode/SKILL.md` 及已有参考文件。核心组合方式进入 SKILL 正文，接口和示例进入 `references/tool-calling/patterns.md`、`examples.md`；在 `references/agent-orchestration/workflows.md` 的既有流程中加入语义条件分支，展示普通工具与代理操作的混合组合。失败语义在 `results-and-errors.md` 补充必要说明。沿用现有 Skill 结构，不新增 Skill、专用工作流框架或大段通用警告。

### 8.1 统一的工具组合方式

Skill 应直接告诉模型：

- 脚本中需要理解自然语言语义来取得一个真假值时，使用 `tools.decide`。可以将它放在任意工具或代理操作前后，不按调用来源或阶段区分用法。
- 所有 `decide` 调用都位于 `if (tools.decide.available())` 的可用分支内；`else` 保留并输出对应条件、材料和工作引用，供 ROOT 继续。`available()` 是本地同步方法，直接返回 boolean。
- 先写一个明确条件，再选定每项判断的材料单位。一项可以是文本、工具结果、带说话人的讨论、代理报告，或多个来源的组合；保留条件所需的上下文。
- 同一条件的一项或多项材料使用相同接口，按 `decisions[i]` 对应原始输入。来源 target 和工作引用留在脚本中的原始对象上。
- 将布尔值用于普通 JavaScript 表达式、筛选、分支和循环条件，再继续调用所需工具。后续可以读取文件、发起查询、安排或继续代理工作，也可以形成最终输出；没有强制的“判断后交回 ROOT”步骤。
- 数值比较、字段存在、日期范围、字符串匹配等确定性条件使用 JavaScript。主模型已经读到的少量材料可以直接判断。
- 普通工具调用、`run_agent`、`query_agents` 等都在同一个脚本环境中组合。复用原有等待、读取、材料传递和身份引用方式，不为 `decide` 增加另一套代理调度机制。
- ROOT 创建、续派和等待代理工作均使用 Code Mode，包括一次简单委派。把能直接连接的步骤放在同一个脚本中，不机械地为每个操作新建 Cell 或输出全部中间值。
- ROOT 可以直接使用原生 `query_agents`、`send_agent_message`、`stop_agent` 查看进度、补充要求或停止工作；同名 JS 方法继续可用。使用哪种控制入口按当前交互需要选择，不要求先停止脚本。
- 子代理使用原生查询、通信和等待与已知队友协作。将真实 Agent／Work target 通过既有 instruction、context、materials 或消息传递，不给子代理安排它没有的脚本环境。
- 对分页来源按问题需要逐页处理。只在流程需要 ROOT 接收信息时 `console.log`；中间结果可以继续留在脚本中供后续调用使用。
- 检索摘要截掉了判断需要的上下文时，先用返回的读取入口补齐对应材料。类似“对，就按这个来”的发言应连同前面的提议一起输入。
- 调用失败时保留并报告具体错误，或在原因明确后调整调用；不要将异常捕获后转换成一组 `false`。
- 本 Cell 的 `available()` 返回 `false` 时，将待判断的问题和已有结果交给 ROOT，保留来源及工作引用；继续现有任务流程，无需停下要求用户配置。用户完成配置后，新 Cell 会读取新的可用性状态。

这是一种可组合的工具选择策略，不要求每次搜索、读文件、回答用户或调用其他工具之前先运行 `decide`。不要求主模型为每个判断撰写解释，也不要求所有输入都经过第二个模型复核。

Skill 不把 `decide` 定位成专用筛选器、专用代理路由器或仅供某个阶段使用的助手。需要什么语义判断由当前任务和脚本决定，产品约束集中在明确的输入、真假输出和既有执行边界。

### 8.2 可采用的英文正文

> Use Code Mode to create agents, assign or continue work, and wait for their messages or results, including a single assignment. Compose these operations with ordinary tools in the same script. Agent and Work targets refer to the same objects across scripts and native controls. ROOT can use native `query_agents`, `send_agent_message` and `stop_agent` to inspect, steer or stop work directly while scripts run. Subagents keep their native query, message and wait tools.

> Use `tools.decide({condition, inputs})` wherever the script needs a true/false judgment about natural-language meaning. Ordinary tools and agent operations are calls in the same environment: compose `decide` before, between or after them. Inputs may contain text from any source or combine several results. Write one clear yes/no condition and include the context needed for each input. The result is `{decisions: boolean[]}` in input order; one input uses the same array interface as a batch.
>
> Use the booleans in expressions, filters, branches and loop conditions, then continue with the tools or agent operations the task needs. Keep source targets and Agent/Work references with the original objects. Use the existing wait, query and materials interfaces for agent work. Intermediate results can stay in the script; log information when ROOT should receive it. Use JavaScript for exact comparisons, counts, dates and string matching. If a call fails, handle its tool error explicitly.
>
> Guard calls with `if (tools.decide.available())`. This local synchronous method reports configuration availability captured when the Cell started and performs no tool or network call. If it returns false, pass the pending condition, material and relevant work references to ROOT to continue the task. The Decision connection works independently of memory reranking. A new Cell picks up configuration availability changes.

Skill 不写产品 home 路径或凭证读取方法，也不反复使用“不能证明”“只能说明”“不意味着”等防御性段落。准确的输入、返回值、操作选择和具体错误已经承载必要边界。

### 8.3 系统提示与参考文件同步

ROOT 系统提示移除“简单 subagent 调用用 native，复杂流程才读取 Code Mode Skill”的指导，明确：

> Use Code Mode to assign or continue subagent work and wait for results, including simple assignments. Read the bundled pulsara-code-mode Skill for the calling pattern. Native `query_agents`, `send_agent_message` and `stop_agent` remain available for direct observation and control.

同一提交同步以下现有文件：

- `pulsara-code-mode/SKILL.md`：description、入口说明、最小示例和等待说明符合矩阵；保留统一环境与简短脚本用法。
- `references/tool-calling/patterns.md`：JS 工具列表继续包含 `run_agent`、`wait_agent`，加入 `decide` 及本地 `available()`；只有两种入口都提供的工具才描述为“使用同一 native 参数”。
- `references/tool-calling/examples.md`：展示带 `available()` 分支的语义判断及普通工具组合；原生控制和 Code Mode 派发／等待的示例与入口矩阵一致。
- `references/agent-orchestration/coordination.md`：分别写清 ROOT 的 Code Mode 派发／等待、三个原生控制入口，以及子代理原生通信；保留 Work、依赖、材料和批量语义。
- `references/agent-orchestration/examples.md`：重写 native 创建／续派及 ROOT native 等待示例。已有 Agent 通过查询取得并在脚本中复用；保留脚本运行时 ROOT 直接补充消息、查询和停止的例子。
- `references/agent-orchestration/workflows.md`：展示一个脚本内连接工具、代理和语义判断的流程，按需要输出结果；不把 ROOT 在每一阶段重新判断作为固定步骤。
- `references/tool-calling/results-and-errors.md`：说明脚本异常时已接受工作与结果的实际状态，以及原生控制入口的用法；保持现有错误和保存结果协议。

子代理工具说明继续介绍其实际拥有的三个协作操作；不要把 ROOT 的派发与等待入口变化写成对子代理的限制。活动文档和模型可见说明只描述本规范的当前合同，历史调用记录保持原样。

## 9. 同一环境中的组合示例

这些例子展示同一个接口在不同位置的组合，不定义不同模式或专用执行路径：

| 脚本已有的值 | 语义条件示例 | 脚本如何使用结果 |
| --- | --- | --- |
| `search_session_content` 返回的讨论 | 用户是否提出了 Skill 编写要求？ | 筛选原始条目，再读取、整理或交给代理 |
| `read_file` 取得的文档与任务要求 | 文档是否包含任务要求的安装步骤？ | 选择继续读取、修改文件或安排代理工作 |
| `query_agents` 取得的审查报告 | 报告是否提出需要修改的具体问题？ | 将该 Work 作为材料继续安排修改，或进入后续步骤 |
| 多个工具和代理返回的材料组合 | 这些材料是否对同一条要求给出了相互冲突的描述？ | 发起进一步查询、安排核对或沿另一分支继续 |

### 9.1 找出过去的 Skill 编写要求

用户请求：

> 帮我回顾一下，我以前对 Skill 的编写方式提过哪些要求，整理出来。

实际工具组合：

1. `search_sessions` 按 `skill`、提示词等与问题相关的关键词查找会话，取得真实的 SESSION target。
2. `search_session_content` 在选定会话中取得相关问答单元。按需要复制 `next` 续页；每条命中保留 `messages` 中的说话人、文本和读取入口。
3. 用 `if (tools.decide.available())` 检查本 Cell 的配置可用性；为真时使用同一个条件：“这段讨论中，用户是否明确提出了关于 Skill 内容、写法或使用指导的要求？”每个问答单元作为一个 input。为假时将候选按页交给主模型判断。
4. JavaScript 按返回值筛出原始命中对象。通过 `read_session_content` 及命中返回的 `read` 按需要展开原文，将相关材料和来源交给主模型。
5. 主模型整理具体要求，结合后续发言处理修正，并给出答案。

以下为判断语义示例，不是实际执行结果：

| 输入材料中的用户发言 | 预期 |
| --- | --- |
| “安装 impeccable skill 到用户目录。” | `false` |
| “skill 也无法打开或者关闭了。” | `false` |
| “主要是 bundled skill 一定要好好写，写清楚。不要写大量过度防御性的话术。” | `true` |
| “明确在 skill 里面说无需先打开或者在最后打开。” | `true` |

下面演示一个已取得搜索结果页的筛选。`page` 是实际 `search_session_content` 返回值；发送判断前按任务需要补足被截断的上下文。外层按实际 `next` 继续取页，可以逐页执行同一段逻辑：

```js
const candidates = page.items;
const condition = "这段讨论中，用户是否明确提出了关于 Skill 内容、写法或使用指导的要求？";
if (tools.decide.available()) {
  const { decisions } = await tools.decide({
    condition,
    inputs: candidates.map(item => JSON.stringify(
      item.messages.map(({ role, at, text }) => ({ role, at, text }))
    ))
  });

  const selected = candidates.filter((_, i) => decisions[i]);
  if (selected.length) console.log({ matches: selected });
} else {
  console.log({ condition, candidates }); // ROOT 按页阅读并判断，保留各项来源。
}
```

读取原文时使用命中返回的真实 `message.read` 或 `message.target`，不构造 entry ID。长消息续读按 `read_session_content` 合同处理；阅读范围由当前问题决定，不为展开一个命中而自动读完整个会话。

这个案例的分工必须在 Skill 中保持清楚：搜索取得候选，Decision 识别用户是否正在提出编写要求，主模型提炼要求的具体内容。来源 target 保留在原始对象中，不需要 Decision 模型生成或复制。

用户没有配置 Decision 时，沿用同样的搜索与原文读取工具，省去第 3 步，把候选按页交给主模型识别并整理要求；已经取得的搜索结果继续使用。

### 9.2 在已有工具与代理流程中加入语义条件

使用 bundled Skill 现有“调查、审查、修订、实施”流程中的真实工具接口：

1. 脚本按已有 `run_agent`、`wait_agent.works`、`query_agents` 合同取得已完成审查的结果，并保留实际 Agent／Work target。
2. 按任务需要用 `read_file`、`search_content` 或其他工具取得相关材料，与审查正文一起组成 `inputs` 中的一项。普通工具结果和代理结果作为同一种输入文本参与判断。
3. 在 `tools.decide.available()` 为真时，调用 `decide`，例如询问“审查意见是否要求对本次任务的实现继续做出修改？”
4. JavaScript 根据布尔值继续原有流程：需要修改时，用原实施代理的 target 发起新 Work，并将审查 Work 放入 `materials`；否则继续后续工具或代理步骤。具体分支由任务决定。
5. 对新 Work 的等待和结果读取仍使用已有接口。中间判断与分派留在脚本中，流程需要输出时再 `console.log`；不为了让 ROOT 再做一遍同样判断而插入固定停顿。

这里没有新增子代理脚本、专用代理判断模式或第二套执行环境。`decide` 只提供一个布尔值，普通 JavaScript 和现有工具接口完成后续组合。未配置时，输出条件、已有材料及相关 Work 引用，由 ROOT 接着处理；已完成工作不重复运行。

### 9.3 ROOT 在脚本外补充要求

用户在实施代理运行期间补充要求时，ROOT 用原生 `query_agents` 取得实际 AGENT／WORK target，再用原生 `send_agent_message` 向当前工作传达要求；需要停止时直接调用原生 `stop_agent`。这些操作不等待新的 Cell 获得运行名额，原脚本可以继续按实际工作状态推进。

如果目标工作已经完成，消息操作按原合同报告不活跃。ROOT 通过一个新的 `exec_cell` 调用 `tools.run_agent`，向同一 Agent 追加工作，不用原生发消息模拟派发。

Code Mode 运行时不可用时，上述原生查询和控制仍直接使用已有协作后端；ROOT 不创建控制脚本来执行它们。Code Mode 恢复后，新脚本通过真实查询结果接续未完成目标。

## 10. 同一提交的实施落点

| 位置 | 必要改动 |
| --- | --- |
| `retrieval/decision.py`、`retrieval/rerank/decision.py`、`http.py` 与 factory | 唯一 Decision 协议实现、记忆用途适配及普通重排路径 |
| `retrieval/http.py` 与现有 SDK client owner | 复用认证、请求准入、超时、凭证和响应边界 |
| `retrieval/config.py` | 保留唯一连接保存位置；将共用 Decision 物理配置归到唯一 owner |
| `conversation_kernel/host.py`、`memory_tools.py`、`decision_tools.py` | 注册前完成共用服务组装和注入，保留记忆业务规则及关闭合同 |
| `capability/builtin_catalog.py` 与现有暴露规则 | 表达角色和入口矩阵，保留共用 schema、执行 owner 与权限合同 |
| `conversation_kernel/tool_surface.py`、`tool_runtime.py`、`tool_execution.py` 与 Code Mode 目录 | 从执行能力构造原生／JS 各自目录，保证 ROOT 原生派发／等待拒绝而 JS 正常执行；加入 `decide` 分支，复用原执行叶 |
| `conversation_kernel/subagents/collaboration_api.py` 及相关准入 owner | 保留唯一协作执行路径、子代理通信、Work 生命周期和独立原生控制；不经 Code Mode 转发控制请求 |
| `code_mode/host.py`、`node/sidecar.mjs`、`node/worker.mjs` 与 `node/guest.js` | 在 Cell 准入、排队和 Guest 安装全链路传递冻结 boolean，为 `tools.decide` 提供本地同步的 `available()` 方法 |
| `desktop_app/http_server.py`、现有设置服务与 `frontend/components/memory-retrieval-settings.tsx` | 复用保存接口及配置，拆开 Decision 卡与记忆重排展开状态，测试连接走共用适配器 |
| `ports/system_prompt.py` 与 bundled `pulsara-code-mode` | 同步 ROOT 派发／等待入口、独立控制、子代理通信、通用语义判断和全部相关参考示例 |

无需新增数据库表、事件种类、持久 job、恢复协议、公开 target 或独立配置存储。需要的普通调用身份、执行事实和输出使用现有 owner。模型提示、公开接口和设置说明只描述本规范确定的单一路径。

该实现提交必须同时完成 `decide` 与代理工具入口调整，不能仅隐藏原生工具而遗漏 JS 目录，也不能发布新入口而保留要求模型走已移除路径的提示和示例。

## 11. 验收与验证范围

优先改造现有 `test_memory_retrieval_adapters.py`、Code Mode 执行／运行时、协作工具暴露与消费者测试，以及设置测试中的相应用例。新测试只补公开合同和实际集成缺口，不另建一套 SDK 协议测试工程。不要以全局删除 `wait_agent` 的断言替代角色与入口矩阵验收。

必须覆盖：

1. **接口与对应关系**：单项、多项、空数组、空文本、重复文本；非法参数发送前拒绝；乱序响应恢复原顺序；`0`、`0.5`、`1` 的布尔转换正确。
2. **协议与失败**：两种 shape 均走共用实现；缺项、重复、未知项、非法概率、拒答和网络失败产生正常工具错误；无成功前缀或补假值；实际凭证不会进入诊断输出。
3. **物理批次**：超过 20 项能分批且逐项返回；实际 UTF-8 封装字节参与切批；单项超限在发送任何批次前指出原始位置；已知结果容量不足在发送前报资源边界；子批失败停止后续发送。
4. **配置与记忆回归**：`ranking_mode=off/rerank/decision` 时 `decide` 均按同一 Decision 配置工作；删除或修改配置影响后续调用；在途调用保持同一连接；记忆排序、原概率和自动召回阈值保持不变。
5. **真实 Code Mode 边界**：`decide` 和 `available()` 始终存在；后者同步返回 boolean，无工具或网络调用，方法及配置值不能被 Guest 改写；未配置或认证缺失时返回 `false`，脚本交回待判断的问题和材料，不将其当成语义 false 选择业务分支；实际判断经普通执行叶返回数组，`decide` 的原生或子代理入口不可用；空输入直接返回空数组；原 Cell 目录及可用性固定，新 Cell 按最新配置取得状态；执行时配置删除由宿主正常报不可用；共享闸门、停止、关闭和名额释放通过已有执行测试验证；配置变化保持 provider prefix 不变。
6. **组合、Skill 与设置**：替换现有 Code Mode 组合流程中的相关用例，覆盖普通工具结果、代理结果及二者共同作为判断输入，布尔值能继续控制后续调用；正常中间结果留在脚本中。Skill 明确统一组合方式，不限定筛选或代理交接用途；示例先执行 `available()` 检查，未配置分支保留材料和 Work 引用交给 ROOT 继续；Decision 卡在重排关闭时可用；无产品 home 暴露或泛化的防御性话术。
7. **角色与入口矩阵**：ROOT 原生表只保留三个代理控制工具，JS 中五个协作操作及两个既有 helper 均可用；子代理仍可原生查询、通信、等待，不能创建／停止代理或执行 Code Mode。`read-only`、`ask-permissions`、`accept-edits`、`bypass-permissions` 四种权限模式保持原准入规则；本规范的协作与 Code Mode 入口仍仅在 bypass 下可用。未配置 Decision 不影响这些接口。
8. **真实独立控制**：使用现有运行时测试占满 VM 名额并让 Cell 保持等待，原生查询、消息和停止仍可完成且没有新建／排队控制 Cell；覆盖 Code Mode 无法启动或 sidecar 已失效时相同的后端控制路径。停止 Cell 后，已接受 Work 保留真实状态，原生查询和停止可继续针对同一 Work 操作。
9. **入口硬切与连续性**：保留已安装旧 schema 的测试中，SYSTEM/tools 字节不变，ROOT 原生 `run_agent`／`wait_agent` 调用被拒绝且不产生新 Work；JS 同名调用及子代理原生等待正常。新合法构建的 ROOT 工具表符合矩阵；执行能力与 JS 目录不依赖这两个原生条目的存在。所有调用继续复用同一结果结算和身份 owner。
10. **提示与文档一致性**：活动系统提示、Skill 正文及参考文件不再指导 ROOT 原生创建／续派／等待；保留子代理原生等待的正确示例。简单委派使用短脚本，原生控制不要求先停脚本；正常任务不因未配置 Decision 而要求用户设置。

对实施接缝补充以下集成断言，纳入上述用例而不另建框架：

- 用真实 Node／worker／Guest 路径验证 `available()` 传递和冻结；Cell 排队期间修改配置，原 Cell 保持准入时的 boolean，新 Cell 使用新值，实际请求仍按调用时配置处理。
- ROOT provider 表缺少 `run_agent`／`wait_agent` 时，真实 Cell 仍能调用两者及其 helper；新旧 provider 表上的 native 尝试均不能伪造调用来源。保持 provider surface specs／binding 对应约束及 MCP 授权回归。
- Decision 排队、HTTP 在途、两子批之间分别触发 Cell Stop；没有后续子批，SDK 关闭只汇合一次，共享名额在关闭后释放，其他消费者继续执行。另覆盖重复取消和关闭失败，不能把失败包装成成功或复活已终止 Cell。
- 同一非空调用的工具截止时间贯穿全部子批和等待；多批不能续期，也不能将单次 4 秒 HTTP timeout 错用于整个逻辑数组。会话 Host 关闭只影响其服务，最终无未汇合请求。
- 保持停止链路的既有 IO、provider cleanup、Host 与 Cell 取消回归通过；本文真实 Decision 验收必须独立完成，不能用之前 DeepSeek 主模型停止验收替代。

实施完成后，使用用户已保存的 Decision 连接，经真实 Code Mode 工具路径运行一次上述小型正反样本批次，检查实际布尔结果、来源筛选和错误可见性，并记录实际耗时。配置只读，复用生产 settings 和凭证 owner；若缺少配置，明确记录缺少的设置，不改用其他模型冒充验证。测试不要求对相同模型反复采样或添加任意性能门槛。

## 12. 实施验证

验证日期：2026-11-07。Python 使用仓库 `.venv`；数据库用独立创建的本地 PostgreSQL 集群，`CI=1` 下执行，不以数据库缺失跳过集成测试。前端使用真实 Node 工具链。

- 核心组合回归：751 passed。覆盖 Decision 两协议、工具与设置 owner、真实 Host／Node／Guest、记忆重排、MCP、共享生命周期、停止链路与 handoff。
- 代理与入口广范围回归：263 passed、15 subtests passed。覆盖派发／等待、持久确认、权限与 prefix、进程恢复、MCP readiness、压缩、FIFO、Cell 失败后续行、sidebar 与 hook；与核心组有部分重叠，不累计为独立用例总数。
- dogfood 异步结算专项：1 passed，并经独立复跑。真实 Host／QuickJS 覆盖源 ROOT 先完成、派工仍未受理、输出交付触发的新 ROOT turn 仍在运行等窗口；最终严格只有一个 Agent／Work，等待验收脚本不等待子代理结束，也不重复派工。
- 前端全套：102 个文件、1660 passed；TypeScript 检查、修改组件 ESLint 与桌面构建通过。构建保留现有大 chunk 提示。
- 系统提示与 bundled Skill 的可执行示例通过真实 Guest 验证；代理集成使用真实 `exec_cell → tools.run_agent`，保留 Work 身份、消息隔离、压缩连续性、hook 来源和关闭结算断言。

真实 Decision 验收由 `tests/dogfood/run_code_mode_decide_dogfood.py` 执行，本地报告为 `output/decide-dogfood/report.json`。使用只读保存的 `system_one` 连接、模型 `perplexity/pplx-decider-v1.2`，在 `ranking_mode=off` 下经真实 Host、Node、Guest 与工具结算链路发出一次 HTTP 请求，耗时 1.398 秒。四份文件材料返回 `[false, true, false, true]`，保留 `writing.txt`、`guidance.txt` 的原文与来源后写入结果文件。同步可用性、空数组、非法条件、目录矩阵、provider prefix 和物理关闭均有断言。

该验收的 ROOT 使用确定性驱动，真实网络验收对象是保存的 Decision 服务；两协议、分批、取消及完整权限矩阵由自动测试覆盖。辅助 chat 尝试在 SDK 入口被阻断，未发 chat HTTP，不将此报告视为真实 ROOT 模型的代理规划验收。配置未写回，凭证值不进入报告。

复用了原来的八位 GPT-6 Astra subagent，交叉检查了服务协议与生命周期、工具及 Host 注入、入口准入、Node 冻结路径、设置界面、提示与 Skill，以及真实验收证据。复审发现的非法概率类型转换、超大整数概率、待投递取消、关闭失败后准入及提前读取 binding 问题均已修复，原反例经独立复核关闭。

现行 `unified_safe_point`、`known_agents_handoff`、`builtin_default_null` dogfood 的派发说明、入口识别和 trace reader 已同步；共享 trace 从 canonical invocation 读取真实 Code Mode 来源。三个更早的脚本 `run_prompt_cognitive_dogfood.py`、`run_unified_final_input_budget_dogfood.py`、`run_unified_reference_dogfood.py` 仍引用本次任务前已失效的 `spawn_agent`、旧 task／model API，不作为当前验收证据，本次未扩展到其历史接口重写。
