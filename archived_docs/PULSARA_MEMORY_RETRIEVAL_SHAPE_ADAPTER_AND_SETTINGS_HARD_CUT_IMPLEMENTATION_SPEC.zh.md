# Pulsara 记忆检索公共适配器与模型设置实施规格

日期：2026-10-10

状态：公共 adapter、设置持久化 / HTTP API、紧凑前端及显式 / 隐式召回已落地。OpenAI native Decisions 使用 SDK 原生资源；System One 共用于 Jev / OpenRouter。真实验证已覆盖 embedding、普通 rerank、OpenRouter Jev / Luna；Jev 官方及 OpenAI 官方入口缺少保存凭据，待补真实调用。生产设置 v2 → v3 已经用户明确授权转换并重启；不包含自动旧格式兼容。

## 1 产品目标与本期边界

用户在“设置 → 模型 → 记忆检索”中配置 embedding 模型、普通 rerank 模型和 decision 模型。普通 rerank 与 decision 是地位相同的重排序后端，每次操作使用用户选择的一条路径；可以分别保存两类配置，然后切换使用。两类模型不串联，不以其中一类失败后调用另一类。

embedding / 普通 rerank 使用手动连接配置，不依赖 models.dev 填充模型、地址或协议。embedding 请求格式固定；普通 rerank 的 shape 只选择请求格式，响应结果位置由适配器在有限的支持结构中识别。decision 保留两个显式协议：`system_one` 与 `openai_decisions`；前端提供 Jev、OpenRouter（System One）、OpenAI Decisions 三个连接预设。预设填充并灰置协议与请求地址，自定义时可编辑，模型 ID 与密钥由用户填写。运行时只按保存的 shape 选择适配器，共用 OpenAI SDK 与 HTTPX2，不从 hostname、模型前缀或失败响应猜测协议。只有完整请求 / 响应主体符合所选协议且满足本期物理边界的接口属于支持范围。

本期交付：

- 可编辑、测试、保存及清除三个模型槽位；Embedding 与重排分别用开关控制；开启后向下展开配置抽屉。重排开启时，Jev-like Decision / Rerank 切换位于重排标题与开关同一行；未配置警告出现在切换与开关之间，切换随之向左让位。
- Decision 槽位提供三个连接预设，落到两个协议适配器；模型选择不增加协议或配置槽位。
- 配置经前端、HTTP API、`LocalSettingsStore` 进入实际生产调用；已有 Host 在下一次相关操作中采用新配置。
- embedding 用于查询和已有后台向量维护；两条重排路径接入显式 `memory_search` 及既有相关旧记忆重排调用点。
- embedding 模型切换时旧向量失效，后台逐条覆盖重建，每条记忆只存一份向量。
- 完整移除代码中的 `DashScope` / `dashscope` 专用命名、固定连接和凭据路径。

本期不兼容供应商可选扩展字段，不提供自定义 JSON、任意字段映射、额外请求头编辑器或自定义排序指令。需要额外必填字段的原生接口暂不支持，用户可选择其提供的兼容入口。decision 问题自身的指令属于其主体协议，由 Pulsara 固定构造，不属于供应商扩展参数。

本期保留现有 PostgreSQL `vector(1024)` 及 HNSW 索引。这是已有存储边界：仅接入无需额外请求参数即可返回 1024 维浮点向量的模型。页面明确显示这一要求，连接测试及运行时都验证。不同维度的模型会得到具体错误；不得自动截断、补零或发送 `dimensions` 改写模型输出。放开存储维度是后续独立 schema 变更，不以“兼容 OpenAI”名义宣称本期已经支持任意 embedding 模型。

本次按用户“文档全部落地”的授权一并实施第 10 节：开启重排时隐式召回最多 20 个候选，一次批量打分后取最多 5 条；关闭时沿用原路径。“普通 rerank 与 decision 地位同等”指正式后端地位，不要求两条路径同时执行。

## 2 改动前实现与已切断的路径

| 原有代码 | 改动前行为 | 已实施变化 |
| --- | --- | --- |
| `src/pulsara_agent/settings.py` | 两个供应商专用密钥；没有可配置的检索模型与请求形状 | 保存三个中立配置槽位及重排选择 |
| `frontend/components/settings-view.tsx` | 模型页中的固定 embedding / reranker 密钥行 | 替换为记忆检索模型表单 |
| `frontend/lib/runtime-adapter.ts` | 专用凭据 DTO 与 PUT / DELETE 方法 | 替换为检索配置与连接测试方法 |
| `src/pulsara_agent/web_app/http_server.py` | 专用凭据路由及摘要 | 提供中立设置和测试 API |
| `src/pulsara_agent/conversation_kernel/host.py` | 构造固定 `RetrievalConfig()`，传给记忆工具端口 | 使用保存配置；操作开始时冻结完整 typed 值 |
| `src/pulsara_agent/retrieval/embedding/openai_compatible.py` | OpenAI SDK 请求，模型固定 `text-embedding-v4`、1024 维 | 保留向量校验，移除固定模型及专用凭据绑定 |
| `src/pulsara_agent/retrieval/rerank/dashscope.py` | HTTPX 请求，固定 `qwen3-rerank` 和路径，支持可选 `instruct` | 替换成按 shape 的公共 HTTP rerank adapter |
| `src/pulsara_agent/retrieval/config.py` | 固定供应商地址、模型及向量契约 | 保存及读取中立配置；仅保留本期必要物理边界 |
| `src/pulsara_agent/conversation_kernel/_repository/memory.py` | 向量按 fact 覆盖；缺失扫描不检查模型契约 | 扫描也识别不匹配的契约；覆盖时使用实际请求绑定 |
| `src/pulsara_agent/conversation_kernel/memory/recall.py` | dense SQL 使用模块级固定契约常量 | 使用本次查询冻结的模型契约 |

改动前 embedding 使用 OpenAI SDK 与 HTTPX2，普通 rerank 直接使用 HTTPX；现在两者统一使用 SDK / HTTPX2，固定模型和专用凭据分支已删除。

现有聊天 `ModelConnectionConfig` 带有 Chat / Responses、上下文额度、工具能力等产品语义，不适合直接承载检索模型。检索配置使用小的独立 typed 值，复用现有本机设置存储、密钥保护、HTTP 客户端和前端表单交互，不把检索配置伪装成聊天连接。

本规格替代历史文档中“只能使用固定 embedding / rerank 模型”及“只配置供应商专用密钥”的条款。现有记忆可见性、权限、kind、生命周期、关系与 provider 输入连续性规则继续有效。

## 3 公共 adapter 的职责

依赖边界分为三层：

- OpenAI SDK 负责 HTTP 请求入口、认证头、序列化 / 响应解析入口、超时、异常与显式重试配置；HTTPX2 负责连接复用和网络传输。统一注入现有 `ProcessCredentialBoundHttpx2Client`，由 `ProcessCredentialBoundary` 继续持有进程凭据准入。
- Pulsara 协议适配器负责最小请求主体、响应产品校验、候选对应关系，并输出统一的 `RerankResult(index, score)`；OpenAI native Decisions 复用 SDK 正式资源与类型。
- 记忆检索层负责候选选择、match tier、稳定排序、最终截断和失败时保留原 RRF / 阶段顺序。

### 3.1 单一 SDK 与生产传输路径

统一使用 OpenAI SDK，不引入 Typesafe SDK 或 OpenRouter SDK。生产调用采用以下唯一入口：

| 路径 | OpenAI SDK 入口 | Pulsara 的最小适配 |
| --- | --- | --- |
| `openai_embedding` | 公开 `client.post(endpoint, cast_to=CreateEmbeddingResponse, body=...)` | 仅发送 `model/input`，检查索引和向量 |
| 普通 rerank 的两种请求格式 | 公开 `client.post(endpoint, cast_to=..., body=...)` | 按保存的请求格式构造主体，识别三种受支持的响应结果位置并检查完整分数 |
| `system_one`：Jev / OpenRouter | 公开 `client.post(endpoint, cast_to=..., body=...)` | 共用 `state + questions{}` 构造和 `answers{}` / `noul` 校验 |
| `openai_decisions` | 原生 `await client.decisions.create(model=..., input=..., questions=...)` | 构造命名 predicate，并将 SDK 返回的 `name/probability` 对应到候选 |

公开 `post` 允许使用完整 URL 与自定义 JSON；使用 OpenAI SDK 不要求这些请求采用 OpenAI 的主体协议。普通 rerank / System One 的小型 typed 响应值由 Pulsara 持有，不复制 SDK 网络实现。OpenAI Decisions 使用原生入口，不再另写一份通用 POST 实现或按 SDK 版本协商。

截至本次修订，仓库依赖为 `openai>=3.26.0`、`httpx2>=2.12.0,<3`，锁定并实际安装的 OpenAI SDK 为 `3.26.1`。主模型 Chat / Responses 和当前 embedding 已使用 `ProcessCredentialBoundHttpx2Client`；MCP 复用同一个 HTTPX2 凭据客户端。现有普通 rerank 的直接 HTTPX 实现将在本期被 SDK 公开 `post` 路径替代；不要求顺带改写无关的 HTTPX 使用点。

仓库 `.venv` 和实际 `pulsara app` 所用的 uv tool 环境已经同步升级；用户已确认既有连接测试可用。这只证明当前生产路径可用，不代表尚未实现的记忆 decision adapter 已完成验收。今后激活依赖升级时必须同时核对实际启动解释器、SDK 版本和 editable 安装来源；升级磁盘依赖后重启旧进程，避免新源码搭配旧 SDK。

### 3.2 地址、认证与客户端所有权

配置统一保存完整 endpoint。公开 `post` 路径直接使用该地址，不附加供应商路径。OpenAI native Decisions 的资源固定请求 `/decisions`：其 endpoint 必须以 `/decisions` 结尾，由适配器去掉这个明确后缀得到 SDK `base_url`。例如 `https://api.openai.com/v1/decisions` 对应 `https://api.openai.com/v1`。允许兼容 host 与前缀路径；不符合这一原生资源路径的地址在配置校验时拒绝，不使用 HTTP hook 改写 URL，也不退回通用 POST。以实际 wire URL 验证拆分结果。

本期检索 adapter 的 SDK 重试统一显式设为 `max_retries=0`，采用调用方已有 deadline。主聊天模型的既有重试策略不因此变化。密钥、模型和地址均显式传入，避免 SDK 默认环境变量成为第二个配置来源。`authentication=none` 复用已有 OpenAI 客户端构造和 `openai_auth_request_options`：原生资源使用其 `extra_headers`，公开 `post` 使用等价 `options.headers`，以 SDK 的 `Omit` 显式移除 Authorization。两种认证都走同一个 SDK / HTTPX2 路径，不增加匿名 HTTP fallback。

provider 的既有生命周期 owner 持有并关闭 SDK client；注入的 HTTPX2 client 由 SDK 随关闭一并释放。复用既有凭据准入、取消与 in-flight settlement。连接测试关闭自己的短期客户端，不能关闭其他查询或主聊天模型持有的客户端。

请求 / 响应字节边界与重复 JSON key 检查通过 HTTPX2 的公开注入点落实，并在 SDK 解码前执行；原生资源需要按流读取响应时可用 SDK 的公开 streaming-response wrapper，仍由正式资源构造请求。不得为复用 SDK 而取消既有资源边界，也不得另建一套 HTTP 请求或重试框架。

建议保留公共 port，具体实现采用中立名称：

- `EmbeddingProvider` → `HttpEmbeddingProvider`。
- `RerankProvider` → `HttpRerankProvider` 或 `DecisionRerankProvider`。
- `build_embedding_provider`、`build_rerank_provider` 按 shape / 后端类型选择实现。

embedding 的业务输入为文本序列，输出为按输入次序对应的向量。rerank 的业务输入为当前 query 与候选文本，统一输出 `RerankResult(index, score)`；候选稳定 ID 可作为 decision 关联所需的 typed 输入，但不得因此给普通 rerank 请求增加额外字段。

候选 ID 直接使用已有 fact ID；测试候选使用调用方提供的稳定 ID。adapter 不产生新的 durable ID、候选映射 registry 或指纹查找表。请求内的 key 与 index 对应表只在当前调用中存在。

公共 port 删除 `instruction` 和 `top_n` 可选参数。调用方提交本次需要打分的完整候选集，adapter 要求响应完整覆盖；最终截断由记忆调用方执行。普通 rerank 只发送主体字段，不能为供应商默认只返回部分条目偷偷增加另一套兼容参数。

## 4 首版支持的主体 shape

shape 使用有限枚举与小的编解码函数。请求地址是完整 endpoint，必须包含最终路径；公开 `post` 直接使用该地址，OpenAI 原生资源按第 3.2 节明确拆分。运行时不探测地址后猜 shape，不在失败时尝试另一种 shape。

### 4.1 Embedding

首版 shape 固定为 `openai_embedding`，由表单提交固定值；前端不展示只有一个选项的协议下拉框。用户手动填写完整请求地址、模型 ID、API key；新配置默认 Bearer，编辑保留保存的认证方式，不提供高级设置或认证选择。

请求：

```json
{
  "model": "用户填写的模型 ID",
  "input": ["第一条文本", "第二条文本"]
}
```

响应主体：

```json
{
  "data": [
    {"index": 0, "embedding": [0.1, 0.2]},
    {"index": 1, "embedding": [0.3, 0.4]}
  ]
}
```

示例向量仅展示字段结构；实际必须是 1024 维。检查索引是合法整数、唯一且完整；向量是有限数值且范数非零。不接受 base64、量化整数编码、稀疏向量或多向量输出，也不发送选择这些格式的扩展参数。响应里的模型名、usage 等可保留为诊断，但不得用响应字段重绑定实际请求配置，也不得把字符串别名或版本后缀当成向量兼容性的独立证明。

复用 OpenAI SDK。已检查当前 `3.26.1` 的 `embeddings.create`：未指定 `encoding_format` 时仍会自动发送 `base64`，指定 float 也会增加该字段。为满足本期仅 `model/input` 的 wire 主体及完整 endpoint，采用公开 `post(endpoint, cast_to=CreateEmbeddingResponse, body=...)`，注入现有 HTTPX2 凭据客户端。SDK 持有 HTTP 调用及类型解析入口，Pulsara 保留索引 / 向量校验。删除固定模型绑定，保留一个生产实现；实际最小主体在本期 adapter 验收中验证。

### 4.2 普通 Rerank

用户手动填写完整请求地址、模型 ID、API key，并从有限下拉框选择“请求格式”。新配置默认 Bearer，编辑保留保存的认证方式，不提供高级设置或认证选择。不需要用户填写 JSON 或响应字段路径。

`RerankConnection.shape` 仅表达请求结构，使用以下两个 closed 值：

| 请求格式选项 | Shape | 请求主体 |
| --- | --- | --- |
| 标准格式（默认） | `flat_rerank` | `model`、`query`、`documents` |
| 嵌套格式 | `nested_rerank` | `model`、`input.query`、`input.documents` |

默认值只用于新建表单草稿，提交时明确携带所选 shape；服务端不靠省略字段或猜测供应商选择协议。编辑已有连接时准确回显保存值。切换请求格式只修改草稿，不自动测试或保存。

下拉框旁提供“格式示例”折叠入口，按需展示以下只读示例；默认不展开，不占用常态表单高度。示例不是可编辑请求模板。

标准格式：

```json
{
  "model": "用户填写的模型 ID",
  "query": "当前用户问题",
  "documents": ["第一条候选记忆", "第二条候选记忆"]
}
```

嵌套格式：

```json
{
  "model": "用户填写的模型 ID",
  "input": {
    "query": "当前用户问题",
    "documents": ["第一条候选记忆", "第二条候选记忆"]
  }
}
```

两种请求格式均使用 OpenAI SDK 公开 `post` 与同一个 HTTPX2 凭据客户端；一次调用只发送所选格式，不在失败后改用另一种格式重试。首版只发送文本字符串数组，不发送 `top_n`、`top_k`、`instruct`、`instruction`、`return_documents` 或其他供应商扩展项。

两种请求格式共用同一个响应解析器，支持以下三个明确的结果位置，与请求格式独立：

- 顶层 `results[]`。
- 顶层 `data[]`。
- `output.results[]`。

只识别这三个位置，不递归搜索任意数组、不根据 hostname / 模型名称选择解析器。响应必须恰好包含一个受支持的结果位置，并且其值为数组；没有结果位置、同时出现多个结果位置或值类型错误，都视为响应非法。不能按优先级挑一个结果位置，也不能在一个位置校验失败后改读另一个位置。额外的不相关 metadata 不影响校验。

每条结果统一读取 `index`、`relevance_score`。每个候选恰好一个分数；index 不重复、不缺失、不越界；score 是有限数值。普通 rerank 的 score 不强制限制在 `[0,1]`，也不作为跨模型或跨请求校准后的概率。返回部分结果时整次重排降级，不能把缺失条目补成 0。

响应结构识别仅处理本次已收到的响应，不发送探测请求、不改写保存配置，也不新增持久 `response_shape`、`response_path` 或供应商识别字段。新的供应商符合上述请求及响应结构即可复用同一 adapter；实际支持仍需官方接口 / SDK fixture 或真实请求证据，不能仅凭目录收录或“兼容”宣传确认。

### 4.3 Decision 的两个协议与三个预设

`DecisionConnection.shape` 只有 `system_one` / `openai_decisions` 两个 closed 值。前端提供以下预设，直接填入 shape 与完整 endpoint：

| 连接预设 | Shape | 默认 endpoint | SDK 入口 |
| --- | --- | --- | --- |
| Jev | `system_one` | `https://api.typesafe.ai/v1/systemone` | 公开 `post` |
| OpenRouter（System One） | `system_one` | `https://openrouter.ai/api/v1/systemone` | 公开 `post` |
| OpenAI Decisions | `openai_decisions` | `https://api.openai.com/v1/decisions` | 原生 `decisions.create` |

预设只是前端表单填充动作，不另存 provider 字段，不进入运行时 factory 分支。预设协议与 endpoint 灰置，选择自定义才允许编辑；模型 ID 原样传入。OpenRouter 下的 Jev / Luna 共用 `system_one`，不因模型不同增加 adapter。只保存一个活动 DecisionConnection，不新增三个 decision 槽位，也不同时调用三个入口。

OpenRouter 统一使用 System One 兼容入口；不提供其 `/api/alpha/decisions` 独立选项、专用 adapter、自动探测或失败回退。三个连接预设仅用于方便填写不同服务地址，协议选择始终只有 System One 与 OpenAI Decisions 两项。2026-10-10 已用同一 `state + questions` 主体实测 OpenRouter 的 `typesafe/jev-1.13` 与 `openai/gpt-6-luna-decisions`，均返回 HTTP 200 和 `answers.*.noul`。这证明该入口的最小协议可用，尚不替代本期固定判别问题、完整候选校验及前端链路验收。

支持范围：明确排除 Respan；Cloudflare Workers AI 原生 REST 的 `result` 外壳不在首版支持范围，不增加自动解包或 `response_path` 配置，经 OpenRouter 使用的模型仍按其接口处理。Liquid AI 的公开 `state/questions → answers/noul` 主体可按 `system_one` 配置自定义 endpoint，但正式宣称支持前需通过真实 smoke；不提供专用 SDK、预设或供应商分支。不符合两种完整协议的其他接口暂不接入。

#### 4.3.1 System One：Jev 与 OpenRouter 共用

一次请求将当前 query 和全部候选放入共享 state，并为每个候选建立一个 `noul` 问题：

```json
{
  "model": "用户填写的模型 ID",
  "state": {
    "user_task": "当前用户问题",
    "candidates": [
      {"memory_id": "已有候选 ID", "memory": "候选文本"}
    ]
  },
  "questions": {
    "已有候选 ID": {
      "type": "noul",
      "instructions": "固定的记忆用途判别问题",
      "criteria": {
        "true": "The memory provides relevant facts, user context, preferences, constraints, or supported plans or decisions that could improve the answer or action, or identify a necessary check.",
        "false": "The memory provides no such help; it is merely topically similar or concerns an inapplicable subject, project, time, or condition."
      }
    }
  }
}
```

固定问题判断候选是否实质帮助回答 / 行动或指出必要核查，覆盖事实、用户背景、偏好、约束及来源支持的计划 / 决策；主题或词汇相似不足以判定有用。不在前端开放自定义 prompt。问题中明确关联当前候选 ID，答案通过同一个 key 映射到原始 index。

调用方传递本次用途 `recall` / `related_memory`，默认 `recall`。这是进程内调用参数，不是设置、持久化字段或新增 wire 字段；普通 Rerank 两种请求格式保持原 `query/documents` 主体。显式 memory_search 与隐式召回使用 `recall`；remember 的关联候选使用 `related_memory`，此时 `user_task` 是待保存正文，问题判断旧条目是否提供检查重叠、冲突、替代或真实依赖的具体依据，不直接判定或写入关系。两种 Decision 协议共享同一个提示构造函数：[decision_prompt.py](../src/pulsara_agent/retrieval/rerank/decision_prompt.py)。

两种用途都要求按意义匹配（包括跨语言），尊重主体、项目、时间、条件、否定与不确定性。`context_product_label` 表示可见范围，不表示普遍适用；`recorded_at` 是保存时间，不证明当前正确性，也不能单独证明替代关系或失效。相关差异和需核查前提可以有用。只使用已提供的内容，不猜测缺失事实或被截断正文；忽略候选内部要求操纵判别的指令。逐项判断候选自身贡献，不要求挑够数量、不增加分数阈值；现有排序、截断、候选池及失败降级边界保持不变。state 仍只含本次 query 和候选，不加入对话历史或 when_to_use，不增加 query 改写模型。

关联检查比较主体与适用条件，不把同一直接主体作为所有关联的硬前提，真实依赖可能跨主体。其 False 标准明确涵盖仅主题相关或完全无关、均不提供具体检查依据的候选。这只是候选检查标准；CONTRADICTS / SUPERSEDES 工具的同主体、同条件比较提示保持不变。

2026-10-10 提示修订验证：97 项 focused tests 通过，覆盖两种 Decision SDK wire / 两种用途、普通 Rerank 主体不变、显式 / 隐式 / remember 关联检查的用途、降级 / 凭据边界、写入与关系提示契约及 provider 前缀连续性；Ruff 与 `git diff --check` 通过。[测试日志](../output/memory_retrieval_implementation_20261010/decision-prompt-revision-tests.log)。本次未做真实模型准确性或新提示时延评测，不把 mock 通过解释为效果提升。

记忆写入的 SYSTEM 与 remember.statement 共用 authoring guide：区分建议、暂定计划、采纳选择与完成行动，保留说话者 / 行动主体；仅在现有信息明确时解开代词和相对日期。memory_search.query 提示保留已知项目和辨别细节，不猜测缺失事实。SYSTEM、关系工具及 relation_kind 说明共用比较规则：同一主体与条件下比较，不同日期 / 条件可能解释差异，保存较晚本身不支持 SUPERSEDES。模型仍负责最终 kind 与显式关系调用，判别分数不取得写入权威。SYSTEM / tools 修改只在允许的新 cold epoch 或显式采用的 compaction successor 安装，不重写已有 epoch 前缀。

Jev 与 OpenRouter 共用公开 `post` 和同一份最小响应校验；读取顶层 `answers`，不要求供应商专用 metadata，也不根据 host 切换解析器。响应的 `answers` 必须与提交的问题 key 集合完全一致，每项为 `type="noul"` 且 `noul` 为 `[0,1]` 内的有限数值。SDK 的通用类型校验不能替代本次候选集合对应检查。候选 ID 必须出现在问题 instructions 中，不能只作为问题 map 的 key：协议 key 负责对应答案，不保证参与模型推理。

#### 4.3.2 OpenAI native

使用 `AsyncOpenAI.decisions.create` 原生资源和 SDK `Decision` 响应类型。请求地址按第 3.2 节构造 `base_url`，认证、超时和关闭责任与公开 `post` 路径共用现有 owner。

将同一 query / 候选 state 编码为一个 JSON 文本字符串，放入 `input`。每个候选生成一个 `predicate` 问题，`name` 使用已有候选 ID，instructions 明确关联这个 ID，并包含同一固定用途判别标准。不得只将 `state` 字段改名为 `input` 后仍发送对象或 Jev 问题 map；不得原样发送 Jev 的 `noul` 类型或 `criteria` 字段。

主体示例（`input` 中的文本由运行时统一 JSON 序列化）：

```json
{
  "model": "用户填写的模型 ID",
  "input": "当前任务和全部候选的 JSON 文本",
  "questions": [
    {
      "type": "predicate",
      "name": "已有候选 ID",
      "instructions": "该 ID 对应的记忆是否为当前任务提供有用背景或指导？"
    }
  ]
}
```

响应主体：

```json
{
  "answers": [
    {"type": "predicate", "name": "已有候选 ID", "probability": 0.8}
  ]
}
```

通过 `name` 恢复原 index，要求名称完整且唯一，不依赖响应数组顺序。每项必须为 predicate、有限数值 probability 且处于 `[0,1]`；缺失 / null name、重复 name、错误 name、额外答案、非法值或 `refusal` 都使整次重排进入原序降级，不把拒绝或缺失记忆补成 0 分，也不切到其他协议。

#### 4.3.3 共用排序契约

两种协议仅将“该候选有用”的 `noul` / `probability` 转换为 `RerankResult(index, score)`，不将 choice / rubric score 混入首版。固定用途标准保持一致，主体表示分别按协议生成；统一数值范围不代表不同模型概率已经校准，不能跨模型比较分数或引入共用阈值。

重复 JSON key 视为畸形响应，在公开 HTTP response hook 中于 SDK 解码前检查原始字节。错误对应关系、缺失答案、错误类型或非法值使整次重排失败并降级，不采用部分结果。

两种协议候选最多 20 条时都只发送一个批量 decision 请求，不能拆成 20 个串行或并行计分请求。适配后的结果与普通 rerank 完全同等地进入上层排序。

## 5 本机配置与秘密持有

在 `LocalSettings` 中增加 `memory_retrieval`，包含固定的三个可空槽位及选择：

```text
embedding: EmbeddingConnection | null
embedding_enabled: bool
rerank: RerankConnection | null
decision: DecisionConnection | null
ranking_mode: off | decision | rerank
```

每种模型只保存一份当前配置。保存 embedding、rerank 或 decision 时直接替换对应槽位；不维护连接数组、配置历史、配置名称或连接 ID，不加入主聊天模型的连接列表，也不通过连接 ID 再选择活动配置。清除后该槽位为空。rerank 与 decision 可以各保留一份配置，由 `ranking_mode` 选择当前使用哪一类。

开关状态与连接配置分开持有。Embedding 关闭时保留配置和已有向量，不发起查询 embedding 或后台 embedding 维护，也不采用关闭后才返回的该通道结果。重新开启同一绑定时复用仍匹配正文及契约的向量，只补齐缺失或过期条目。`embedding_enabled=true` 要求 embedding 槽位配置完整且具备所需密钥；清除该槽位同时置为 false。开关不参与向量契约，不因单纯关闭 / 开启重建全部向量。

重排开关关闭时保存 off 并保留两个槽位，开启后在抽屉中选择 Jev-like Decision / Rerank。初次开启优先使用已配置的 Decision；只有 Rerank 配置完整时使用 Rerank；两者都未配置时仍展开，默认选中 Decision，并在开关左侧显示红色警告。页面内关开时保留最近选择的后端；这个偏好是 UI 临时值，不新增持久字段。刷新且当前 off 时重新按上述已配置优先级选择，不能声称持久记住关闭前的后端。运行时仍只使用 ranking_mode 的 off / decision / rerank 三个值，不增加自动模式或 Decision 失败后再调用 Rerank 的规则。

每个连接的非秘密值只有 `endpoint`、`model_id`、`shape`、`authentication`。认证支持 `bearer_api_key` 和 `none`，复用既有认证枚举或等价的最小 typed 值；不允许任意认证 header。密钥由 `LocalSettingsStore` 持有，三个槽位独立，标记 `repr=False`，读取 API 只返回 `credential_configured`。

EmbeddingConnection.shape 固定为 `openai_embedding`；RerankConnection.shape 只能为 `flat_rerank` / `nested_rerank`，不保存响应格式选择，也不保留此前将请求与响应位置组合的三种枚举别名。

DecisionConnection.shape 只能为 `system_one` / `openai_decisions`。这一个值贯穿表单、前端 DTO、HTTP API、保存配置及 provider factory；三个预设不形成额外持久字段，不另设可能相互矛盾的 provider / protocol 双字段。切换 decision 协议只影响后续重排操作，不触发 embedding 重建。

未配置 embedding 时保留关键词 / 精确记忆路径；未配置重排时保留既有候选排序。embedding 和两种重排独立：没有 embedding 也可以对稀疏候选重排。

默认三个槽位为空，`embedding_enabled=false`、`ranking_mode=off`。前端抽屉开关是页面内开启意图，和服务端实际生效状态分开：缺少模型或必需密钥时可以展开，但有效状态仍是 false / off，不请求不可用模型；取消或保存失败仍保留展开和警告。用户配置当前已展开且选中的未生效槽位时，保存配置与启用在同一次设置 mutation 中完成；普通编辑已生效槽位或非选中槽位不改变当前生效路径。配置完整只代表本机配置可用，不代表远端一定可达。

复用现有原子文件发布、文件权限、序列化与秘密处理。新配置不是新的聊天模型 catalog、连接注册中心或 durable 产品表。主聊天模型与检索模型不互相默认继承密钥、地址或模型。

新本机设置 closed schema 升级一次，删除旧专用凭据字段及其 parser / writer / methods / exports。不保留旧新双读、别名路由或供应商专用默认值。旧设置文件的激活转换作为一次明确的本机维护操作处理，保留聊天连接、PostgreSQL、MCP / OAuth 等不相关值；不能因新 parser 读不到旧文件而把整个用户配置修复为空。

本次文档任务和后续 dogfood 均不自动改写用户保存配置。验证从现有 owner 只读载入可用生产配置，通过已有注入点组装隔离环境的新 typed 配置。激活实际本机新格式时须明确处理旧文件，不能把永久兼容 reader 留在生产代码中，也不能把秘密搬到报告、fixture 或 shell 环境变量。

## 6 HTTP API 与前端链路

建议接口如下，全部走既有本地 HTTP 准入与设置 mutation owner：

| 接口 | 作用 |
| --- | --- |
| `GET /api/local-settings/memory-retrieval` | 三个槽位的非秘密配置、密钥是否存在、embedding 开关及当前重排选择 |
| `PUT /api/local-settings/memory-retrieval/{slot}` | 原子保存该槽位配置及明确密钥动作；显式 `activate=true` 时同次启用该槽位 |
| `DELETE /api/local-settings/memory-retrieval/{slot}` | 清除该槽位；embedding 同时关闭，当前重排槽位同时切到 off |
| `PUT /api/local-settings/memory-retrieval/embedding-enabled` | 开启 / 关闭 embedding，保留连接配置 |
| `PUT /api/local-settings/memory-retrieval/ranking-mode` | 选择 off / decision / rerank，保留连接配置 |
| `POST /api/local-settings/memory-retrieval/{slot}/test` | 使用表单值发送一个小的实际请求，不保存 |

路由显式区分 `embedding-enabled`、`ranking-mode` 与 slot，不能把它们作为普通槽位吞掉。`{slot}` 的 closed 枚举是 embedding / rerank / decision。槽位 PUT 的 `activate` 是本次提交意图，不持久化为另一个状态字段；编辑已生效或非选中槽位提交 false，配置已展开且当前选中但未生效的槽位提交 true。服务端同次验证配置、密钥、重建确认及启用状态，不能先保存再另发启用请求而留下半完成状态。

保存密钥动作采用有限的 `keep` / `replace` / `clear`，只有 replace 携带新 key；keep 只作用于同一槽位已经保存的 key。不得让掩码字符串成为新密钥，不得由空密码输入默默删除旧 key。测试草稿可明确使用当前槽位已保存的 key，也可以使用表单新 key；不创建临时持久连接。

embedding 有效绑定变化时，PUT 请求需要携带前端明确确认的 `confirm_reembed=true`，以及警告针对的旧非秘密绑定完整值（endpoint、model、shape，或 null）。服务端在同一设置 mutation 中比较当前值、确认依据与提交值，不相信前端单独计算的 changed 标志。只有改变 key 或认证方式而 endpoint、model、shape 不变时不触发重建确认。并发提交改变了确认依据时，返回当前值并要求前端重新展示具体变更；不加入确认 token registry、额外 fingerprint 或持久回执。

GET 和现有 `_settings_read_model` / bootstrap 摘要采用同一新 DTO；更新 CLI 健康信息与 overview，不能保留一套旧凭据状态让 UI 与运行时分叉。保存成功返回新摘要，前端采用服务器结果。

### 6.1 主页面：开关与紧凑配置摘要

“设置 → 模型”中的记忆检索沿用上方模型配置的分组标题、图标与字体。大标题“记忆检索”下只放一句“配置记忆召回使用的语义检索与重排模型。”。下方 Embedding 与重排使用相同的小标题行，右侧各有一个 toggle switch。关闭时仅保留标题行，开启后向下展开对应的紧凑配置抽屉。

```text
记忆检索
配置记忆召回使用的语义检索与重排模型。
Embedding                                     [开关]
  provider.example · text-embedding-v4     密钥已配置
  Embedding · 1024 维
  text-embedding-v4                       [修改] [删除]
重排                                          [开关]
                                  [Jev-like Decision | Rerank]
  provider.example · jev-1.13              密钥已配置
  System One
  jev-1.13                               [修改] [删除]
```

模型条目沿用上方主模型配置的单行宽度布局：左侧图标，中间粗体标题（保存地址的主机名与模型 ID）、协议说明及等宽模型 ID，右侧密钥状态与修改 / 删除。不猜测目录中的供应商名或模型名称；不在页面重复完整请求地址。重排类别显示为“Jev-like Decision / Rerank”，只显示当前选中类型的一个模型条目；切换类别保留另一槽位的保存值。所有空槽位（包括 Embedding）均只显示“未配置”和配置入口，不加类型前缀。不做三张大卡片，不展开表单字段。

关闭抽屉同时关闭对应检索通道，但保留槽位、密钥和向量。开启未配置槽位仅展开，不自动弹出配置窗口；开关左侧显示红色警告图标，hover / 键盘 focus / 点击显示小浮窗：“开关已打开，但尚未配置模型，语义检索 / 重排暂未启用。”缺必需密钥时明确显示尚未配置 API key。警告只检查该通道当前选中槽位；缺少非活动重排槽位不显示通道警告。复用现有 hover 定位与 portal，避免分组的 overflow 裁剪。

清除活动模型由现有服务端 owner 同时关闭实际路径，页面抽屉与开关保持打开并显示缺配置警告；不会自动切到另一后端。选择未配置的重排类型时先成功把旧有效路径设为 off，才切换页面选中项并显示警告，避免实际仍在使用旧模型。删除按钮沿用主模型行内取消 / 确认删除交互；取消或请求失败保留配置。

外观复用 `settings-group` / `model-config-card` / `model-config-card__actions`：图标底色、标题字号、模型 ID 等宽字体、分隔线与修改按钮一致。展开后的 Jev-like Decision / Rerank 二选一沿用已有蓝色选中配色，顺序 Jev-like Decision / Rerank，没有第三个“关闭”选项。抽屉向下出现的轻量动画尊重 reduced-motion；不设置撑高的 min-height 或固定展开高度。

两个开关具备 `role=switch`，回显抽屉开启意图；重排类型选择具备 radio group 与键盘语义。需要改变服务端有效状态的开关或选档操作按服务器结果更新，忙态阻止重复操作；失败保留原状态并显示一次错误反馈。缺配置的展开 / 收起不新增持久设置，也不发送无效启用请求。刷新按有效配置初始化抽屉；不持久记住未配置的开启意图。配置当前选中但未生效槽位时保存配置与启用保持原子；普通编辑已生效槽位保存仍保留当前有效路径。

每种类型只保存一份当前配置，不出现“添加配置”、配置列表、分页、配置命名、复制或多配置选择器。Decision 预设只填充一个槽位的草稿，不产生额外保存项。

### 6.2 配置弹窗

复用当前主模型配置的 native dialog、焦点管理、遮罩、关闭和表单控件风格；不复制其目录说明、配置来源面板或大块确认摘要。标题分别为“配置 Embedding”、“配置 Jev-like Decision”、“配置 Rerank”，右上角关闭。桌面弹窗约 640–680 px 宽，按内容自然高度，不设置撑高的 min-height；表单横向尽量两列，完整请求地址独占一行。窄屏自然改一列，弹窗内部可滚动，操作栏可达。

| 弹窗 | 默认可见字段 |
| --- | --- |
| Embedding | 请求地址；模型 ID 与 API key 两列 |
| Jev-like Decision | 连接预设与协议两列；请求地址；模型 ID 与 API key 两列 |
| Rerank | 请求地址；模型 ID 与请求格式两列；API key |

Embedding 协议固定，没有协议字段；模型 ID 标签旁只保留“1024 维”这一必要限制。Rerank 请求格式为“标准 / 嵌套”，格式示例默认折叠，只有用户主动查看时展开。Decision 协议显示“System One / OpenAI Decisions”：Jev / OpenRouter 预设固定为 System One，OpenAI 预设固定为 OpenAI Decisions，协议下拉框与请求地址灰置；自定义时才允许手动编辑。三个弹窗均无高级设置或认证选择；新配置默认 Bearer，编辑保留保存的认证方式。已有无需认证的配置隐藏 API key 并准确提交秘密动作。

底部单行：已配置时左侧“清除配置”，右侧“取消 / 测试连接 / 保存”；配置已展开且当前选中但未生效的槽位时主要按钮为“保存并启用”。普通编辑已生效槽位保持现有档位，关闭或取消丢弃草稿。API key 留空保留已保存密钥，用输入 placeholder 表达，不另加重复说明。

表单控件保持现有约 36 px 高，字段纵向间隔约 10–12 px，内容边距约 16 px；开关标题行约 44 px，模型条目高度随主模型三行信息自然排布。紧凑通过减少结构、说明和空区域实现，不缩小文字或可点击目标。所有标签、选项及按钮沿用现有产品字号，不用低对比的小字承载重要操作。

Embedding / Rerank 均不提供供应商或 models.dev 模型选择器。新配置默认 Bearer；已有无需认证配置的 API key 不必填写，运行时不发送 Authorization。响应结果位置由 adapter 识别，页面不增加响应格式下拉框。原有主聊天模型的 models.dev 目录使用不受影响。

Decision 弹窗以“连接预设”提供 Jev / OpenRouter（System One）/ OpenAI Decisions 三个选项，选择后填写第 4.3 节的固定协议与默认地址，灰置协议和请求地址；选择自定义后才允许编辑 endpoint 与两个支持的协议，不展示 SDK 或响应 DTO。Embedding / Rerank 请求地址仍可编辑。预设选择只修改草稿，不自动保存或测试，也不自动复用其他槽位密钥。重新打开时以保存的 shape / endpoint 为准回显；匹配预设展示名称并锁定协议和请求地址，否则显示自定义，推导标签不回写配置；不新增预设身份字段。

界面只展示用户需要选择的请求格式或 decision 协议，不显示内部 shape 枚举、类名、契约摘要、索引或 epoch。请求地址采用完整 URL，避免用户填写 base URL 后由运行时隐式猜路径。不开放候选数量、超时、重试、自定义排序指令或 JSON 编辑器。

### 6.3 文案与必要反馈

常态主页只保留大标题下上述一行说明，子分组不放说明小字；弹窗不加入副标题、功能介绍、字段释义、内部实现说明或重复确认表格。不得把规格条款、代码注释、降级策略、配置持久化规则或“保存与测试相互独立”逐条搬进界面。用户能从标签、控件和当前值理解的内容不另写说明。

辅助文字仅在帮助当前决策时出现：模型条目中的协议与凭据状态；Embedding 的 1024 维限制；测试按钮附近一句“测试会发送请求，可能计费”；缺配置警告的悬停浮窗；实际字段错误；向量生成 / 覆盖确认。模型切换的影响放在执行保存时的确认中，不作为主页或弹窗永久说明。错误要指出具体字段或请求失败原因，不能只写“配置不正确”。连接测试结果通过一次 toast 展示，不再加一块永久结果面板。

保存与测试独立。embedding 测一条短文本并验证 1024 维；两条重排路径使用同一 query 和两条候选，检查候选完整覆盖。测试响应包含具体成功 / 失败、耗时、实际向量维度或候选结果数；不持久化测试证明，不以测试过一次保证以后所有请求成功。关闭弹窗不能重复弹出测试 toast。

修改 embedding 时的确认文案：

> 将覆盖重建已有记忆向量，产生 API 费用。记忆内容保留；重建期间语义检索可能不完整。

首次配置且已有记忆时，也说明将为已有记忆生成向量；首次配置且无记忆时无需显示覆盖旧向量的警告。取消确认不保存配置、不清除向量、不发送建库请求。清除 embedding 配置关闭语义通道，不删除记忆文本；后续重新配置仍按实际绑定判断兼容性。

在 Embedding 关闭期间更改绑定，确认中将“将覆盖重建”改为“开启后将覆盖重建”；保存不立刻触发远端维护。开关重新开启时采用最新已确认配置并 wake 维护。普通开关重开且绑定未变不再次要求重建确认。

## 7 运行时采用与现有调用接线

移除 Host 固定 `RetrievalConfig()` 的供应商模型选择。每次查询、一次重排或一个后台 embedding 批次开始时，从普通设置 owner 读取并冻结完整 typed 配置及所需秘密。本次操作内使用同一个绑定；下一次操作读取最新值。

查询语义通道及后台维护都遵守 `embedding_enabled`，不以“已有配置”替代开启状态。重排只遵守 `ranking_mode`，关闭 embedding 不关闭基于稀疏候选的重排。结果采用检查包括本次相关通道的开启状态 / 档位与冻结绑定；用户关闭通道或切换后端后不安装晚到的旧结果，不增加状态 generation 或持久任务。

查询 embedding、dense SQL 和该结果的可用性检查必须共用同一个冻结绑定，不能发生查询向量来自旧模型而 SQL 过滤来自新模型。操作结束准备采用结果时，如果相关绑定已经改变，丢弃旧远端结果并沿用本次已有安全候选 / 稀疏降级，不为追赶配置修改而无限重试。

provider 实例可以按现有 port 生命周期管理，但不能因为 `_embedding` / `_rerank` 已缓存就永久忽略设置变化。关闭被替换的客户端时遵守取消及 in-flight settlement，不影响正在执行的主聊天模型请求。物理并发边界由已有调用 owner 持有，不能因每次创建 adapter 而失效。

两类重排共用候选投影、调用 deadline、完整性验证后的排序与降级出口：

候选投影保持合法 JSON 文本，保留完整 `kind`、`context_product_label`、`recorded_at`。现有单条 8192 字节边界包含元数据、JSON 转义及截断标记；超限时只对 `statement` 按 Unicode 字符保留头尾，在正文中插入明确省略标记，并携带 `statement_truncated=true`，再通过现有 canonical JSON encoder 编码。不得切开整个序列化 JSON。未截断条目保留原完整投影且不增加标记字段；若完整元数据与省略标记本身无法装入既有边界，沿用 NOT_APPLICABLE / 原序降级。该投影共用于普通 rerank、System One 和 native Decisions，不改变数据库正文、候选 ID 或评分映射。

`remember.statement` 的工具说明准确标明普通记忆最多 8192 UTF-8 字节、RESPONSE_PREFERENCE 最多 2048 UTF-8 字节，与现有写入校验一致；不改变已有存储限制。工具描述变更只在已有获准的新 provider 根边界安装，不热改已安装的 tools。

```text
已有 sparse / dense / RRF
  → canonical 候选
  → 用户选定的 Rerank 或 Decision
  → score 降序并稳定保留原候选次序
  → 既有最终结果数与关系投影
```

显式搜索保留现有 filter relaxation 阶段优先级，远端重排只在同一 match tier 内改变次序；不能把较宽松阶段的条目提升到严格阶段之前。保留当前候选池与最终 `limit` 分离的修复，以及 `remember` 相关旧记忆的最终条数语义。

未配置、远端失败、deadline 到期或响应非法时，用既有 RRF / 阶段顺序完成最终截断。不得只采用部分 decision 答案或部分 rerank 结果，不跨后端自动切换，也不吞掉 cancellation 继续工作。降级沿用现有 typed disposition 与诊断。

保持现有 embedding 的 batch / 并发边界、本地输入估算边界、重排最多 20 候选及单次 4 秒边界。现有总 deadline 比它们更早时取更早者；协议新增不获得额外总时间。现有本地 token 估算值属于 Pulsara 的单次请求准入，不能宣称它准确代表不同模型的 tokenizer 或远端限额；供应商更严格的模型限制按其响应及既有降级处理。保留通用 payload / 响应字节边界，不增加总记忆量、总模型调用数或任务生命周期上限。

配置变更及记忆变更不能重建现有 provider 输入前缀：SYSTEM 与 tools 字节保持不变，记忆结果只通过既有合法 source / message suffix 采用。不重启主模型 epoch，不增加 rebase 边界。

## 8 模型切换与向量覆盖

维持 `memory_embeddings` 主键 `(memory_domain_id, fact_id)`，继续 `ON CONFLICT ... DO UPDATE`。模型身份不加入主键，不为不同模型建立多份向量表或历史版本。只有一份当前保存值。

沿用数据库中的 `embedding_contract_id` / `embedding_contract_version` 作为持久兼容边界；移除固定供应商契约常量。绑定由 canonical endpoint、shape、配置模型 ID、1024 维、cosine 及现有文本投影版本派生，在唯一 ID builder 中计算稳定中立 ID。API key、超时、重排选择不参与向量身份。该摘要只用于跨重启向量兼容，不在 DTO 里重复携带一套 fingerprint，也不建立 fingerprint registry。

开启时读取匹配当前冻结契约且内容摘要仍一致的向量。缺失扫描同时选择以下 ACTIVE 事实：没有向量、内容摘要变化、向量契约不匹配。未配置或关闭 embedding 时不产生远端维护请求。

保存新 binding 后，旧 binding 的向量立即不再用于新操作；后台以新向量覆盖。向量写入携带发出请求时冻结的实际 binding，不能用写入时的“当前模型常量”给旧结果贴新标签。

复用已有 settings mutation owner 和 kernel IO owner，在最后的 binding 检查到数据库覆盖之间明确串行化本地配置采用与写入；远端 HTTP 不占用这段临界区。切换后旧批次不得覆盖新模型已写入的结果；事实被修改、删除或 supersede 时也不得写入旧正文结果。此行为必须用延迟响应和交错保存测试证明，而不以额外 nonce / generation / durable job 实现。

复用 `MemoryEmbeddingMaintainer` 的 Host 可读 context 范围、分页扫描、提交后 wake 及启动 wake。保存开启状态下的配置或重新开启后，对现存 Host best-effort wake；未打开项目在开启状态下的下一次 Host 启动 / 使用时补齐。失败或无进展时停止自唤醒，后续普通 wake 再扫描；不能承诺已重建所有未打开项目。重启后从行的实际匹配状态继续，不需要持久任务或完成回执。

设置 / 数据库跨边界不组成新的分布式事务：已发布的新设置决定当前语义，数据库向量是可重建派生值；发布后中途崩溃或重建失败时，正确结果是部分 / 暂无 dense 与正常稀疏路径。不得回退为继续使用旧模型向量。

## 9 删除清单与实施顺序

实施采用一次 hard cut，逐项检查，不只重命名 adapter：

1. 在 retrieval 与 settings 定义新的 typed 配置、三个槽位及 shape；删除供应商专用 credential 类型、字段、helper、factory 分支与默认 endpoint / model。
2. 复用已升级的 OpenAI SDK / HTTPX2 生产接线：embedding、普通 rerank、`system_one` 使用公开 `post`；`openai_decisions` 使用原生 `decisions.create`。实现两个 decision 协议适配器，不加入 Typesafe / OpenRouter SDK；删除旧直接 HTTPX 重排实现及 `instruction` / `top_n` port 参数。
3. 接通新 HTTP 路由、设置摘要、前端 runtime adapter、开关 / 抽屉 / 两种重排选择及配置弹窗；删除专用凭据路由、UI 组件、按钮方法及对应旧 DTO。
4. 接通操作级冻结配置，替换固定契约查询 / 写入；补齐不匹配向量的扫描和覆盖竞争处理。
5. 更新现有显式及相关旧记忆调用点、CLI 健康、overview、tests、dogfood、clean-v0 fixture 与活跃文档。
6. 运行第 11 节验收并保留真实请求证据；按本次完整落地授权同步实施第 10 节，明确记录缺凭据的入口，不能把 mock 验证称为真实调用。

源码、测试、SQL / catalog fixture、前端源文件及可执行 dogfood 中不得残留大小写任意的 `dashscope` 字面值、符号、文件名或路径。删除旧 embedding / rerank 的固定供应商域名与默认模型；它们由用户配置输入。第 4.3 节三个 decision 预设的标准地址仅作为前端表单默认值，不作为运行时隐式默认或分支依据。通用协议测试使用中立地址，预设填充测试核对表中标准地址。历史文档可保留历史名称，本文的删除说明也不属于生产路径。

检查范围包括 `src`、`tests`、`frontend/components`、`frontend/lib`、`frontend/app` 及项目脚本；不要把 node_modules、dist、缓存或 sourcemap 当作源码。重新构建或移除过时生成产物，不能把新源码已删除的 UI 留在交付包里。

不增加 durable event、live event kind、subject slot、append guard、产品 relation 或 durable job。连接测试、配置采用和重建进度都是已有 owner 的 typed 值或派生观察。保留现有 reader / writer 权限和 provider-input 连续性 oracle。

## 10 隐式重排接入

这一阶段已使用本期 adapter 和配置链路落地，不引入另一套 provider 或模型配置。

用户选择 rerank 或 decision 后，隐式召回候选流程改为最多 20 条 canonical 候选，完成一次批量重排后选最多 5 条注入。off 沿用现有无重排路径。保持当前 kind、可见性、回答偏好独立 source、权限和冲突规则。

不得先截成 5 条再打分，不为了凑满 20 条放宽过滤或复制候选；不增加分数阈值。稳定排序的 tie 使用本次原候选顺序。失败回到本次候选的 RRF 顺序再截取 5 条。

检查 `_recall_presentation_by_membership`：同一成员集合不能永久固定第一次排序并覆盖后续 query / 后端结果。以本次已验证排序投影 source，删除不再符合产品语义的旧排序缓存，保持 source append 的既有连续性规则。

复用既有 500 / 1000 / 1500 / 2000 / 3000 条语料与 prompt-to-provider-dispatch 计时口径。原 smoke 的 rerank 指令不属于本期普通 rerank 的主体支持范围，后续比较要使用真实生产 wire；不能沿用带 `instruct` 的旧 Qwen 时间或效果结论代表新通用请求。

## 11 验收与真实调用

### 11.1 配置及前端

- 三个槽位分别保存、修改、清除，刷新和进程重启后读取相同配置；非活动槽位不影响当前重排路径。
- 连续修改同一模型类型始终只有一份当前配置；设置 DTO 与持久化值不包含检索连接列表或活动连接 ID。前端每种类型只显示一个配置位置，没有添加、分页或多配置选择器；Decision 预设切换只改变草稿，保存后替换唯一 decision 槽位。
- API 不返回真实 key；keep / replace / clear 行为明确，不能将掩码保存为 key。修改一个槽位保留其他槽位及聊天 / PostgreSQL / MCP 配置。
- 请求格式不兼容、缺必填值、认证缺 key、维度不符均得到具体错误；成功测试不自动保存。
- Embedding 表单固定提交 `openai_embedding`，无协议选择；Rerank 新建草稿默认标准格式，可显式选择嵌套格式并准确保存 / 回显。两者手动填写连接信息，不依赖 models.dev，不要求输入 JSON 或响应路径。
- embedding 更改需明确确认，取消无副作用；仅修改 key 不触发向量重建。
- 页面位置为“设置 → 模型 → 记忆检索”，Embedding / 重排开关、抽屉展开状态及 Jev-like Decision / Rerank 选择准确回显；Embedding 与当前选中的重排模型摘要紧凑排列，非选中重排条目隐藏，字段仅在模态弹窗展示，无三张大卡片、大段常驻说明或重复确认摘要。
- 开关及档位关闭保留配置；Embedding 关闭不调用查询 embedding 或维护，不清向量，重开相同绑定复用可用向量。默认均关闭；Decision 是首选非关闭选项，显式 Rerank 选择不被自动覆盖。
- 未配置时开关仍展开抽屉，红色警告位于开关左侧，hover / focus 展示未生效原因；不自动弹窗，不发送无效启用请求。选择未配置后端时先关闭旧有效路径；清除活动槽位同步关闭实际路径，抽屉保留展开和警告。当前未生效槽位保存并启用原子完成，取消或失败不丢失配置、不改变有效状态；关闭抽屉保留配置。模型条目的标题、协议、ID、凭据状态与修改 / 删除复用主模型样式。窄屏、键盘、焦点返回、busy 状态及单次 toast 行为有效。
- Decision 的三个预设正确填充两个协议与默认地址；自定义 endpoint 可保存、测试并在重启后准确回显。只接受两个最终 shape 值，不保留旧枚举别名；运行时不依赖预设标签，协议切换不重建记忆向量。
- bootstrap、settings summary、CLI 和 overview 使用同一个新配置状态。

### 11.2 Adapter 与调用链

- 使用 MockTransport 覆盖实际 wire 主体，验证不发送供应商扩展字段；缺索引、重复索引、越界、NaN、Infinity、非数值和缺候选均失败。
- Rerank 覆盖两种请求格式与三种响应位置的组合；请求只使用保存的格式，响应解析不改变配置。无结果位置、多结果位置、非数组结果均整次降级，不挑选部分合法结果，不发起另一种格式的请求。
- 两种 decision 协议及三个预设 endpoint 分别验证一次提交完整候选；Jev / OpenRouter 的答案 key 与 OpenAI 的答案 name 都完整且唯一；重复 JSON key 拒绝；`noul` / `probability` 范围正确，正确恢复原 index。
- 两种 Decision 协议分别验证 recall / related_memory 的固定问题和一致判别标准；检查显式 / 隐式召回与 remember 关联预览的用途传递，确认未加入对话上下文，普通 Rerank 两种用途的 wire 主体不变。提示契约覆盖确定性、归属与关系比较条件；这些测试不替代准确性评测。
- OpenAI 答案打乱顺序仍正确对应；null name、重复 name、refusal、缺失或额外答案均整次降级；无按位置碰巧对应的路径。
- 使用实际 OpenAI SDK 和 HTTPX2 MockTransport 验证：Jev / OpenRouter 经公开 `post` 到达完整 endpoint，OpenAI 经原生 `decisions.create` 到达预期 `/decisions` 路径；原生 typed 响应和公共分数归一化正确。既有主模型及 embedding 请求继续通过契约测试；deadline / 取消 / 密钥准入 / 响应字节边界持续有效，本期检索 adapter 均关闭 SDK 默认重试。
- bearer 与 none 均走同一 SDK 路径；无认证请求不继承环境密钥、不发送 Authorization，不存在另一套匿名 HTTP 实现。
- 两条重排路径能将原排序后方条目提升；相同分值稳定；保留显式 match tier 优先级及最终 limit。
- 配置修改后，已有 Host 的下一次相关操作采用新配置；远端延迟返回和取消时无旧结果误采用。
- 未配置、错误、超时及畸形响应沿用安全的稀疏 / 原序降级；不会触发另一类后端的请求。
- 只读设置、凭据边界、请求资源约束和正常取消沿用已有生产 owner；不得为了测试跳过它们。

### 11.3 PostgreSQL 与覆盖重建

- 两个同为 1024 维但不同契约的模型相互隔离；不匹配契约进入补齐扫描。
- 重建前后每条 fact 最多一个向量；正文、kind、关系及来源不被改写。
- 新模型查询不使用旧向量；旧请求延迟完成不能覆盖新向量；事实变更 / 删除后旧结果不得安装。
- 覆盖一部分后模拟进程结束，重启按不匹配事实继续；未打开项目在使用时补齐，不将部分完成报告为全库完成。
- clean-v0 及 catalog / grants oracle 一致；没有新 durable 工作机制。

### 11.4 比例合适的验证

使用仓库根 `.venv` / uv 执行后端 focused tests，覆盖 settings、检索凭据、web HTTP surface、direct advisory memory 和 rerank selection；必要的 PostgreSQL 集成测试用已验证的隔离本地库。前端执行 settings / overview / runtime adapter 相关 Vitest，以及 TypeScript 检查和本地构建。新改动导致失败时修复，不弱化断言。

真实调用至少证明：一个 embedding 主体接口、一个普通 rerank 主体接口、Jev native System One、OpenRouter System One 与 OpenAI native Decisions 三个预设入口（两个协议）；OpenRouter 路径覆盖 Jev 与 Luna 两个模型，并核对实际请求路径为 `/api/v1/systemone`。每个标准入口的模型 ID 按当前服务要求显式配置，不自动增删供应商前缀。通过正常设置 API 保存到隔离 Pulsara home，再让真实检索调用读取这些配置，不能只手工构造 adapter 宣称前端已接通。确认实际 wire、响应、候选对应、最终结果、取消和降级；最后运行命名删除扫描。某条路径缺少有效已授权凭据时报告具体缺项，不能以另一个协议的成功冒充这一协议已验证。

真实调用使用用户当前仍有效且已授权的配置 / 密钥。此前临时 OpenRouter key 可能已被用户废弃，不能以重新打印或写入源码作为复现步骤。只读加载保存配置，通过现有测试注入和临时 home / 已验证本地库隔离；记录实际请求及响应，精确排除秘密值。

本期不要求执行原计划全部 1200 个时延样本，也不宣称检索准确性得到验证；规模化时延比较继续按原实验计划开展。

## 12 参考与既有证据

- [隐式记忆时延实验计划（归档）](/Users/plumliu/Desktop/python_workspace/pulsara_agent/archived_docs/PULSARA_IMPLICIT_MEMORY_RECALL_LATENCY_EXPERIMENT_PLAN.zh.md)
- [既有 smoke 说明](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/implicit_memory_latency_20261009/README.zh.md)
- [OpenRouter instruct 实测](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/implicit_memory_latency_20261009/openrouter_instruct_probe/README.zh.md)：HTTP 200 不证明额外字段被采用。
- [OpenRouter embedding 主体](https://openrouter.ai/docs/api/api-reference/embeddings/submit-an-embedding-request)、[OpenRouter rerank 主体](https://openrouter.ai/docs/api/api-reference/rerank/submit-a-rerank-request)。
- [OpenAI Python SDK：通用请求与 HTTP client 注入](https://developers.openai.com/api/reference/python#making-customundocumented-requests)、[OpenAI 官方 decision 主体](https://developers.openai.com/api/reference/python/resources/decisions/methods/create)。
- [OpenAI Decisions 指南及 SDK 最低版本](https://developers.openai.com/api/docs/guides/decisions)、[OpenAI SDK 3.26.1 的正式 decisions 资源](https://github.com/openai/openai-python/blob/v3.26.1/src/openai/resources/decisions.py)。
- [SDK 升级集成测试](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_openai_sdk_integration.py)：实际 SDK 的原生 Decisions、公开 POST、认证边界及现有 embedding 注入；不替代后续记忆 adapter 的真实调用验收。
- [Jev native API 主体](https://docs.typesafe.ai/api)、[OpenRouter System One 兼容入口说明](https://openrouter.ai/blog/insights/what-is-jev/)。
- [OpenRouter System One 真实请求与响应](/Users/plumliu/Desktop/python_workspace/pulsara_agent/output/implicit_memory_latency_20261009/openrouter_systemone_probe/report.json)：Jev 与 Luna 使用相同主体，均返回 `answers.*.noul`；每个模型仅一次请求，不作性能或准确性比较。
- [Cohere rerank](https://docs.cohere.com/v2/reference/rerank)、[Voyage rerank](https://docs.voyageai.com/reference/reranker-api)、[Voyage 官方 SDK 响应解析](https://github.com/voyage-ai/voyageai-python/blob/main/voyageai/object/reranking.py)、[Jina API](https://api.jina.ai/scalar)、[硅基流动 rerank](https://docs.siliconflow.cn/docs/api/rerank-post)：共同主体不等于扩展字段统一；Voyage 的 HTTP 结果位于 `data[]`，SDK 再将它转换为 `results`。

实施时以本规格及人类后续修订为范围，历史实验和历史规格只用于核对保留行为，不能恢复已删除的固定供应商路径。

## 13 本次实现与验证记录

- 统一入口：`HttpEmbeddingProvider` 与 `HttpRerankProvider`，只有保存的请求 shape 分支；无供应商运行时默认连接。SDK 客户端由每次操作的冻结连接创建并关闭，同一 embedding 批次复用其客户端；Host 的共享 semaphore 延续并发边界。HTTPX2 负责解压，响应字节界限按解压后内容校验；原生 typed Decisions 与公开 POST 共享凭据准入。
- 本机设置采用 `pulsara-local-settings:v3`，关闭与配置分别保存。旧 schema 明确返回不可原地修复的错误，阻止设置操作把旧文档修复为空。已有 9 个聊天连接、PostgreSQL 及 1 个 MCP 凭据的离线转换预览已确认；生产文件在 dogfood 中保持不变。
- 后台安装与设置发布共享既有 settings mutation lane；安装 SQL 锁定仍为 ACTIVE 且正文 digest 匹配的 fact，只有一条向量覆盖路径。测试覆盖旧绑定延迟结果拒绝、覆盖一条后中断、重新启动只补剩余条目、关闭时不调用维护。没有新表、事件、任务或 generation。
- 隐式链路覆盖 20 候选 → 一次重排 → 5 条；失败保留 RRF 原序，同分稳定。原成员集合排序缓存已删除，source 使用当次排序；未改写已安装 provider prefix。
- [真实调用报告](../output/memory_retrieval_implementation_20261010/real-provider-report.json)与[实际请求 / 响应](../output/memory_retrieval_implementation_20261010/real-provider-wire.json)：经正常设置 API 写入临时 home，真实 Kernel 查询与数据库维护采用这些设置。Embedding 返回 1024 维并安装 3 条向量；普通 Rerank、OpenRouter Jev / Luna 的隐式最终顺序均与远端分数一致。各后端单次完整召回约 2.0–2.6 秒，仅为小样本 smoke。Jev 官方与 OpenAI 官方没有可用保存凭据，未宣称真实验证完成。
- 前端无需实际浏览器自动化：用户自行验看；执行组件、主界面、runtime adapter 的 Vitest、TypeScript、ESLint 与本地静态构建。源码与交付静态资源完成旧命名删除扫描。

生产激活须先停止旧进程，再执行一次[离线转换](../output/memory_retrieval_implementation_20261010/maintenance_settings.py)，然后启动新版。脚本默认仅预览；`--apply` 才调用现有原子设置 writer 替换原文件，复核旧 typed 值未变并验证新 owner 读回。它读取 Git 保存的改动前 owner，不是生产兼容 reader；完整保留不相关设置，两个检索开关初始关闭。不删除记忆或已有向量；开启 embedding 后旧契约向量按本规格逐条覆盖，可能产生 API 费用。

用户已明确授权停止当前应用、转换并重启。激活完成：原子写入 v3 后通过正常 owner 读回核对，保留全部 9 个聊天连接、PostgreSQL 及 1 个 MCP 凭据；未删除数据库记忆或向量。新版 HTTP bootstrap 返回 runtime / database `ready`，检索开关初始为 false / off。验证记录：[设置激活](../output/memory_retrieval_implementation_20261010/settings-activation.log)、[生产 bootstrap](../output/memory_retrieval_implementation_20261010/production-bootstrap.json)。本次最终验证为后端 163 项、PostgreSQL / Host 38 项、前端 266 项通过；TypeScript、ESLint、本地构建、Ruff 和命名扫描通过。
