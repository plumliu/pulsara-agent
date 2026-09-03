# Pulsara Route + Wire API + Model Universe、Model Connections 与 Wire Adapter Hard-cut 实施规范

> 状态：**IMPLEMENTATION-READY / 尚未激活**
>
> 冻结日期：2026-09-03
>
> OpenCode 代码真源基线：`dev @ f12e14c`
>
> models.dev catalog contract：`https://models.dev/api.json`（运行时直读，不 vendor）
>
> 本文依据当前 production code、`AGENTS.md`、Round 3.1、Round 5A.1、Round 5A.2、
> final-wire estimation hard-cut、已有多模型调研，以及本地 OpenCode `dev` 源码核验收敛而成。

本文只改变以下边界：

- Pulsara 如何直接读取 models.dev 的 `route + model` catalog，并与用户选择的
  `wire API` 解析为 exact target；
- 设置页如何保存多组用户级模型连接配置，以及每个会话如何从中选择唯一的一组；
- catalog 选择面如何只保留 `limit.context >= 256000` 且 model ID 末段不以
  `claude` / `gemini` 开头的模型；
- 每个精确 model target 的 reasoning request contract，以及会话输入框中唯一、粘性的
  reasoning 选择；
- 删除 PRO/FLASH 双模型职责；同一进程可以服务绑定不同配置的多个会话，但每个会话、
  turn 与其 auxiliary calls 只绑定一个 exact model connection/target；
- route/wire adapter 对 exact request shape 的唯一所有权；
- Chat streamed tool-call correlation 对非零、稀疏、复用和缺失 index 的通用处理。

本文不重写 Agent loop、canonical transcript、provider replay、compaction、memory、Agent
capability、tool execution、permission 或长程执行语义。数据库只为“当前会话选择、已排队
输入、已开始 turn”增加最小的 model connection 与 exact reasoning selection 字段；用户级
连接配置使用一个专用本地配置文件与系统 credential vault，不增加 PostgreSQL 配置表、event、
job 或通用设置系统。发生冲突时，优先级为：
`AGENTS.md`、用户当前要求、本文、仍然生效的既有规范。

本文 hard-cut 取代下列旧路径：

- `reasoning_effort: str | None` 的无验证原样透传；
- 在 Pulsara 内手工复制或逐条维护 provider/model catalog；
- provider/model 级 `supports_reasoning` 布尔值；
- 用一套 Pulsara 全局五档去投影未知或稀疏 provider 档位；
- 把省略 reasoning 字段一律解释为“模型会思考”；
- 由 generic `extra_body` 与 adapter 共同占有 reasoning request 字段；
- 用模型名、family substring、release date 或 HTTP 失败猜测支持项；
- 把 streamed tool-call index 当作从 0 开始的稠密数组位置，或只补一个 `index - 1`
  特例。
- `ModelRole.PRO/FLASH`、`LLMConfig.pro/flash`、`PULSARA_PRO_*` / `PULSARA_FLASH_*`
  及其 CLI、Web 和测试双槽投影。
- 进程启动时只能从一组 `PULSARA_MODEL` / `PULSARA_API_KEY` 环境变量取得全局模型的
  env-only 配置路径；
- 设置页只展示一个只读“当前模型”、而不能添加多组配置或由会话选择的旧投影。

Round 5A.1 的显式 terminal、完整 response 原子接受与 semantic-output retry barrier，Round
5A.2 的 exact native replay，Round 3.1 的 provider-input prefix continuity，以及 final-wire
materialization/estimation 契约继续完整生效。

---

## 0. 一句话结论

Pulsara **直接读取 models.dev 的 route/model catalog**，不再在仓库内复制一份
provider/model 表。用户在配置时显式选择 Chat Completions、Responses 或未来其他
已实现的 `wire_api`。内核将三者 exact join 为：

```text
ModelTargetKey = route_id + wire_api + model_id
```

Pulsara 的用户级设置可以保存多组 `ModelConnectionConfig`，每组都冻结一套 exact target 与
endpoint；API key 由固定 vault service + connection ID 定位。每个会话只选择其中一组；主
Agent、该会话的 summary、compaction、memory governance、subagent 和其他 auxiliary model call 不再被分配到
PRO/FLASH 两种模型职责，而是共享该会话选择的同一 connection/target，只保留各自真实需要的
purpose、output bound、deadline 和输入 contract。不同会话可并发使用不同配置。

其中，`route_id + model_id` 及其 limits、reasoning options、tool-call 标记直接来自同一份
models.dev snapshot；`wire_api` 来自用户配置；request/stream/terminal/replay 形状来自
Pulsara 本地 wire adapter。models.dev 的 `npm` 字段不代替用户的 wire API 选择。

设置页先从经过产品过滤的 catalog 选择 route 和 model，再显式选择 wire API、填写 API key，
保存为一组模型配置；会话页从已保存配置中选择一组，随后只展示该 exact catalog entry 公开的
reasoning control。用户选择的就是最终 control，不再经过全局档位投影：

```text
models.dev raw snapshot
        |
        +-- limit.context >= 256000
        `-- model ID 最后一个 `/` 段不以 claude/gemini 开头
        |
        v
从选择面选择 route / model，填写 API key
        |
        v
用户显式选择 wire API（Chat Completions / Responses / ...）
        |
        v
保存 ModelConnectionConfig；会话选择该 config
        |
        v
解析 models.dev entry + local route/wire adapter
        |
        +-- effort choices  -> 展示真实选项（可含关闭），用户 exact 选择
        +-- toggle          -> 展示真实开关
        +-- token budget    -> 仅 closed range展示真实数值选择；开放范围不伪造控件
        +-- fixed on        -> 不显示控件，provider contract 保证始终思考
        +-- unavailable     -> 不显示控件，作为普通非推理模型使用
        `-- provider default -> 不显示伪档位，由模型/服务决定
        |
        v
验证选择属于同一个 target contract
        |
        v
route/wire adapter 生成 exact request
```

因此不存在：

```text
requested xhigh -> effective high
```

只存在：

```text
OpenRouter / Chat / z-ai/glm-5.2 -> 用户可选 high 或 xhigh
Z.AI      / Chat / glm-5.2      -> 用户可选 high 或 max
```

同一 upstream model 经不同 route 暴露不同选项是正常事实。OpenRouter、LiteLLM 或企业
gateway 与直连 OpenAI、DeepSeek、Moonshot 等在产品选择层处于同一层级：它们都是 route，
不是 correctness 例外，也不是隐藏在“provider-neutral”背后的透明魔法。

直接读 catalog 只发生在设置/配置校验或新 cold Host 解析边界；不得在单次 provider request
的路径上读网络，也不得热替换 active epoch 已冻结的 target。首个 hard cut 只在现有
`sessions`、`prompt_queue_items` 与 `turns` 上增加必要的 model connection ID 与 reasoning
selection 字段；不新增 PostgreSQL 表、event、job、checkpoint、receipt、在线逐档 probe、
本地 model catalog 副本或第二套 normalized IR。用户保存的 API key 永远不进入这些表。

---

## 1. 命名边界：`capability` 保留给 Agent 能力系统

### 1.1 Pulsara 已有产品语义

在 Pulsara 中，`capability` 已明确指 Agent 可调用或可装载的能力域，包括：

- Tool；
- MCP；
- Skill；
- Plugin 及其能力集合。

仓库已有 `src/pulsara_agent/capability/`、`capability-view.tsx`、capability snapshot、
capability dispatch cut 等完整术语体系。LLM model support metadata 不得再次占用这一名称。

### 1.2 本文统一术语

生产代码、测试、API 与产品文案统一使用：

| 术语 | 含义 |
|---|---|
| `Route` | 实际发送请求的连接/路由入口；可以是直连 provider，也可以是 gateway |
| `WireApi` | 请求、stream、terminal 与 replay 的协议形状，例如 Chat Completions 或 Responses |
| `ModelCatalogEntry` | models.dev 中一条 exact `route_id + model_id` provider-model 记录 |
| `ModelCatalogSnapshot` | 一次直接读取并完整解析的 models.dev catalog 不可变值 |
| `SelectableModelCatalog` | 对 snapshot 应用 256k context 与 Claude/Gemini model-ID 过滤后的 UI/config 选择面 |
| `ModelTargetKey` | `route_id + wire_api + model_id` 的 exact identity |
| `ModelTargetContract` | catalog entry、用户 wire API 选择与本地 adapter contract 的 exact frozen join |
| `RouteWireContract` | route 在某个 wire API 上的 transport、request、stream、terminal 与 replay 契约 |
| `ModelTargetUniverse` | 当前 selectable catalog 中可与已注册 adapter exact join 的 target 投影 |
| `ModelConnectionId` | 一组用户保存连接配置的随机稳定本地 ID；不是 target fingerprint |
| `ModelConnectionConfig` | 用户保存的 target 与 endpoint；不含明文 API key、reasoning 或派生 execution policy |
| `ReasoningControlContract` | 某 target 真正提供的 reasoning 行为与控制形状 |
| `ModelTargetResolver` | 对已取得 snapshot 做 exact 查找、join 并冻结 target contract 的纯本地解析器 |

禁止新增：

```text
LLMCapability
ModelCapabilityProfile
CapabilityResolver  # 指模型协议时
supports_capability # 指模型协议时
```

外部项目和 provider 文档可能使用 “model capability API/catalog”。在调研引用中可以保留
原称，但映射进 Pulsara 后必须使用上述 target/contract/support 术语。

---

## 2. OpenCode `dev` 代码真源结论

本轮浅克隆：

```text
/Users/plumliu/Desktop/python_workspace/opencode
branch: dev
HEAD: f12e14c
clone: --branch dev --single-branch --depth 1
```

OpenCode 工作树保持干净；Pulsara 不依赖该 checkout，也不复制其代码。

### 2.1 值得借鉴的真实边界

以下源文件是本轮主要证据：

```text
packages/core/src/models-dev.ts
packages/opencode/src/provider/provider.ts
packages/opencode/src/provider/transform.ts
packages/opencode/src/cli/cmd/run/variant.shared.ts
packages/llm/src/providers/openrouter.ts
packages/llm/src/provider.ts
packages/opencode/test/tool/fixtures/models-api.json
```

代码真源证明：

1. `models-dev.ts` 把 reasoning control 分成 `effort(values)`、`toggle` 和
   `budget_tokens(min,max)`，而不是只有一个 `supports_reasoning` 布尔值。
2. `provider.ts` 的 model row 同时带 `providerID`、wire package/API、model ID、limits、tool
   support 与 reasoning options；model 不是脱离 route 的全局对象。
3. `transform.ts::reasoningVariants()` 优先消费 model row 的真实 `reasoning_options`；
   `reasoningEffort()` / `reasoningBudget()` 再按 wire adapter 生成 request shape。
   其中 `reasoning_options` 缺失会进入旧 heuristic，而显式空数组表示“不生成 variant”；
   Pulsara 不复制前一种 fallback：缺少已验证的 selector 时只标明 provider-default/无选择，
   不凭名称生成档位。
4. `variant.shared.ts::fitVariant()` 对 saved/session value 做 exact membership 检查；target
   不再提供该值时直接丢弃，而不是找“最近档位”。其显式 CLI input 当前绕过验证的弱点，
   Pulsara 不复制。
5. OpenRouter 在 provider/model selector 与 adapter 层都是一等 route，不被当作不可见的
   transport detail。

最有力的 fixture 例子是同一个 GLM 系列：

```text
openrouter / z-ai/glm-5.2 -> effort [high, xhigh]
zhipuai   / glm-5.2       -> effort [high, max]
```

这说明 reasoning 选项至少属于 `route + wire API + model`，绝不能只按 model family 或
model ID 建一张全局表。OpenRouter 的 `xhigh` 即使最终被 gateway 映射为 upstream native
`max`，对 Pulsara 用户而言，它仍是 OpenRouter route 真实公开的 `xhigh`；Pulsara 不应再做
一次跨 route 翻译。

### 2.2 直读 models.dev，但不复制 OpenCode 的产品 fallback

OpenCode 为广覆盖和兼容性保留了大量务实 fallback，Pulsara 不照搬：

- `transform.ts::variants()` 中按 provider、npm package、model substring、family 和 release
  date 猜 reasoning options；
- metadata 缺失时生成一组“广泛支持”的默认档位；
- 把 token-budget range 自动包装成产品自造的 `high/max` 两档；
- 直接把未经 typed semantic 标注的 `none`、toggle 与 provider JSON 当作通用产品选项；
- OpenCode 自有的磁盘 catalog cache、定时后台刷新、锁和 refresh event；
- 为特定模型改写历史消息来绕过 replay/prefix 错误。

本文另行采用 models.dev 公开的 `api.json` 作为 direct catalog source。这是“读取上游
数据”，不是“复制 OpenCode runtime”。这些代码说明真实 provider knowledge 必须有明确
归属，但不构成 Pulsara 增加 heuristic、durability 或 prefix rewrite 的理由。

---

## 3. 当前 Pulsara 真值与问题

### 3.1 已经正确且必须复用

当前仓库已经拥有：

- `LLMContext`、`ResolvedModelCall` 与 frozen provider-wire plan；
- Chat Completions / Responses 两个明确 wire adapter；
- adapter-owned final context-bearing materialization；
- provider-neutral normalized text/thinking/tool-call/usage/terminal events；
- explicit terminal 与 incomplete/failure fail-closed；
- semantic payload 出现后禁止 transparent retry；
- completed response 的 exact Chat/Responses reasoning replay；
- target-compatible durable replay hydration；
- same-epoch SYSTEM/tools/messages prefix continuity；
- provider usage 只作事后 telemetry；
- compaction 与普通 send 共用 exact final-wire lowering/estimator。

本轮不建立新的 Agent IR，也不把现有正确边界搬进 catalog。

### 3.2 当前 target 与 reasoning request 的错误抽象

当前代码近似为：

```text
ProviderProfile.supports_reasoning: bool
ModelProfile.supports_reasoning: bool
LLMOptions.reasoning_effort: str | None
```

Chat 将 raw string 写入 `reasoning_effort`，Responses 将其写入
`reasoning={"effort": ...}`；未指定时不发送。与此同时，`ThinkingProfile.enabled`、
`PULSARA_THINKING_*` 与 `request_extra_body.thinking` 又能成为第二个 request owner。

该结构无法证明：

- 这个 exact target 到底提供哪些选择；
- 省略字段是否仍保证 reasoning；
- reasoning 与 tools 是否可同时使用；
- route 改变后旧选择是否仍合法；
- 同一 model 经 Chat、Responses 或 gateway 是否仍是同一 contract。

### 3.3 当前 Chat tool-call index 的错误抽象

当前 accumulator 仍近似把 reported index 当作 dense array position。只兼容首个 index 为 0
或 1 的 origin subtraction，不能完整表达 arbitrary non-zero、sparse、reused、missing index
和 delayed ID。该问题属于 wire normalization，不属于 model target universe；但应在同一个
adapter hard cut 中移除临时 offset 路径。

---

## 4. Model Target Universe

### 4.1 Catalog 与 executable target 分层

models.dev 直接提供：

```text
CatalogEntryKey = route_id + model_id
```

Pulsara 不为 Chat 与 Responses 各复制一份 model row。用户在配置中选择一个已
实现的 `wire_api`，然后解析出：

```text
ModelTargetKey = CatalogEntryKey + user_selected_wire_api
```

因此 universe 不是 checked-in allowlist，也不是预先物化的笛卡尔积。它是一个当前
`SelectableModelCatalog`、用户配置与本地 adapter registry 之上的 exact 投影：

```text
resolve_target(snapshot, route_id, model_id, wire_api)
    = exact eligible catalog entry
    + exact registered wire adapter
    + exact route/wire request codec
```

models.dev 不声称某 model 必然同时支持 Chat 和 Responses；Pulsara 也不作这种推断。
`wire_api` 是用户对实际 endpoint contract 的显式配置。没有对应本地 adapter/codec 就在
provider open 前拒绝；resolver 不做近似匹配、family fallback 或自动换 wire API。

### 4.2 Selectable catalog 的唯一产品过滤

Kernel 必须先完整 parse models.dev response 为 raw immutable snapshot，再构造唯一的产品
选择面。一个 catalog model 只有同时满足下列条件才进入 `SelectableModelCatalog`：

```python
MINIMUM_SELECTABLE_CONTEXT_TOKENS = 256_000

def selectable_model(entry: ModelCatalogEntry) -> bool:
    leaf = entry.key.model_id.rsplit("/", 1)[-1].removeprefix("~").casefold()
    return (
        entry.limits.total_context_tokens >= MINIMUM_SELECTABLE_CONTEXT_TOKENS
        and not leaf.startswith("claude")
        and not leaf.startswith("gemini")
    )
```

这里的 `256_000` 是十进制 token 数，比较字段只能是 models.dev 的 `limit.context`。不得改用
`limit.input`、`limit.output`，也不得把 256k 当作 Pulsara 自己施加的实际输入/任务 lifetime
cap：通过选择门槛后，真实 admission 仍使用该 target 的完整 context/input/output hard limits。

Claude/Gemini 过滤是明确的当前产品范围，而不是 wire support 推断：

- raw model ID 保持 byte/exact 不变；只为过滤取最后一个 `/` 分段、移除该分段开头的一个
  `~` 并 `casefold`；
- 因而 `claude-*`、`gemini-*`、`anthropic/claude-*`、`google/gemini-*`、
  `~anthropic/claude-*` 都被排除；
- 不读取 `family`、display name、provider/route name、`npm` 或 `provider.shape` 扩大过滤；
- 其他模型即使来自 Anthropic、Google、Vertex 或 gateway，也不因 provider 名被排除；
- 未来若 Pulsara 正式支持这些模型族，必须修改这一条产品 predicate 与 golden tests，不能
  在 adapter 中暗开例外。

这里的“唯一过滤”只指产品把 row 从目录选择面排除的 predicate。通过该 predicate 的 row
不会再因为 reasoning option、adapter 或 endpoint validation 失败而被静默删掉；kernel 在同一
read model 上为每个用户可选 wire API 返回 typed `executable` 或精确的 non-executable reason，
UI 保留 row 并禁止确认无 executable contract 的组合。这样 catalog drift 是可见错误，不会变成
第二套隐藏 filter，也不会让无 codec 的 target 获得 execution authority。

Provider selector 只显示至少含一条通过上述产品 predicate 的 model 的 provider。Filter 在 kernel
只执行一次；前端、CLI、config-check 与 saved-connection resolver 消费同一 read model，不得各写
一份阈值或字符串规则。2026-09-03 本次审查对 live `api.json` 的非规范性核验为：7,521 条 raw
model，4,404 条满足 context 门槛，再排除 357 条 Claude 与 414 条 Gemini 后剩 3,633 条；这些数量会随
上游变化，不能写入生产断言。

### 4.3 Identity

Catalog identity 与 execution identity 明确分开：

```python
@dataclass(frozen=True, slots=True, order=True)
class ModelCatalogEntryKey:
    route_id: str
    model_id: str
```

```python
@dataclass(frozen=True, slots=True, order=True)
class ModelTargetKey:
    route_id: str
    wire_api: WireApi
    model_id: str
```

约束：

- 三个字段均 non-empty、canonical、case-sensitive；
- gateway model namespace 保持 route 原样，例如 `openai/...`、`z-ai/...`；
- direct model ID 与 gateway model ID 永远不是同一 key；
- provider alias、latest alias 与 date-pinned model 若语义可能变化，必须各自有 row；
- catalog row 来自 models.dev response 中的原始 provider/model key，Pulsara 不改名；
- selectable predicate 的 casefold/leaf 只服务产品过滤，不改变或替代 raw model identity；
- `wire_api` 必须来自用户当前配置，不从 models.dev 的 `npm`、`api`、
  optional `provider.shape` 或 model 名强制推导；
- custom endpoint 的 canonical endpoint binding 继续进入 existing resolved target fact；
  不新增 endpoint/profile fingerprint registry。

### 4.4 RouteWireContract

```python
@dataclass(frozen=True, slots=True)
class RouteWireContract:
    route_id: str
    display_name: str
    wire_api: WireApi
    default_base_url: str | None
    transport_binding_id: str
    transport_contract_version: str
    request_codec_id: str
    reasoning_codec_ids: tuple[str, ...]
    model_identity_policy: ModelIdentityPolicy
    assistant_replay_contract: ProviderAssistantReplayContract
```

它回答：

- 请求发往哪类 route；
- 采用哪个 wire API 与 transport；
- request/stream/terminal/replay 怎样解释；
- 哪些 closed reasoning codec 可用。

API key 不进入 contract。Route 是直连 provider 还是 gateway 可作为 UI 描述，但不得驱动
Agent core 的不同正确性规则。OpenRouter 和 OpenAI 都是可被用户选择的 route。

`RouteWireContract` 是 Pulsara 需要自己维护的小而明确的部分。models.dev 的 `npm` 只是
AI SDK 生态的 adapter hint，不能定义 Pulsara 的 request、SSE、terminal 或 replay
contract。同样是 Chat Completions，OpenAI 直连与 OpenRouter 的 reasoning request field
也可能不同；该差异留在 `route_id + wire_api` request codec，不落到 Agent core，也不
要求 Pulsara 复制每个 model row。

### 4.5 ModelTargetContract 的最小字段

```python
@dataclass(frozen=True, slots=True)
class ModelTargetContract:
    key: ModelTargetKey
    display_name: str
    catalog_facts: ModelCatalogFacts
    limits: ModelHardLimits
    reasoning: ReasoningControlContract
    tool_call: bool
    interleaved: InterleavedReasoningFact | None
    route_wire: RouteWireContract
```

```python
@dataclass(frozen=True, slots=True)
class ModelCatalogFacts:
    source: Literal["models.dev"]
    route_id: str
    model_id: str
    display_name: str
    reasoning: bool
    reasoning_options: tuple[CatalogReasoningOption, ...] | None
    tool_call: bool
    limits: ModelHardLimits
    interleaved: InterleavedReasoningFact | None
    wire_shape_hint: Literal["responses", "completions"] | None
```

首轮从 models.dev entry 直接投影的 model-specific facts 只有：

1. exact target identity；
2. context/input/output hard limits；
3. reasoning behavior/control 的真实形状、选项和默认；
4. `tool_call` 真值；
5. `interleaved` 字段（若上游提供）。

Pulsara 不再为这些字段维护一份 built-in per-model override table。若上游 entry 缺少
必要字段，按 §4.6 的 typed unknown/provider-default 语义降级；不用名称 heuristic 补齐。

models.dev 当前不提供 exact `tool_choice` 子集、reasoning + tools 互操作矩阵、terminal
状态机或 replay contract。Pulsara 不得伪造这些 per-model facts：

- tool request/choice 形状归 `RouteWireContract`；
- `model.tool_call=false` 时，含 tools 的 call 在 open 前拒绝；
- `model.tool_call=true` 只表示 models.dev 声称该 route/model 可 tool-call，不额外
  推导未公开的 `tool_choice` 枚举；
- reasoning + tools 如果某 `route_id + wire_api` 有已知禁止条件，由该 route/wire
  adapter 在 open 前拒绝；否则不另加一个没有数据来源的强门禁。

以下字段不因“以后可能有用”加入首轮 runtime contract：

- pricing；
- benchmark/ranking；
- marketing description；
- modalities；
- structured output；
- image/audio generation；
- provider token estimator；
- health score 或实时 availability；
- 任意开放式 feature predicate map。

当产品真的使用某项能力时，再用一个明确产品需求扩展 exact contract。不能预先复制
models.dev 的全部 schema。

### 4.6 Catalog 缺失值的唯一语义

- `reasoning=false` -> `ReasoningUnavailable`；
- `reasoning=true` 且 `reasoning_options=[]` -> `ReasoningFixedOn`，按 models.dev 当前的
  明确编辑契约表示“有 reasoning，但没有 caller control”；
- `reasoning=true` 且 `reasoning_options` 含一个或多个控制形状 -> 完整保留所有
  `effort` / `toggle` / `budget_tokens` option；
- `reasoning=true` 但 `reasoning_options` 缺失 -> `ReasoningProviderDefault`，不用模型名
  或同 family 补齐；
- `tool_call` 是 models.dev 的 required boolean，不再在 Pulsara 中增加第二个
  `UNKNOWN` 层；
- `limit.context` 与 `limit.output` 缺失/非法时，该 entry 不可成为 executable target；
  `limit.input` 缺失时等于 context limit，这是 catalog schema 的明确投影，不是
  provider heuristic。
- optional `model.provider.shape=responses|completions` 只投影为 `wire_shape_hint`；
  它可推荐 Responses 或 Chat Completions，但不改变用户所选 `wire_api`，也不构成
  硬可用性证明；缺失时不做任何推断。

Reasoning 不再是所有调用的强制要求。非推理模型、fixed-on 模型与无已知 selector
的模型均可正常执行，但 UI 必须按上述语义诚实展示。

`PROVIDER_DEFAULT` 只承诺 Pulsara 不发送 reasoning selector；它不承诺模型一定思考，也不
承诺一定不思考。这样 target 可以正常执行，同时 UI 对未知保持诚实。

### 4.7 Limits 与本地 execution policy 的所有权

models.dev catalog entry 拥有 provider/model 的 hard limits：

```text
total_context_tokens
max_input_tokens（provider 明确区分时）
max_output_tokens
```

首轮四字段 connection 不再保存或要求用户理解 output/safety policy。Pulsara 从同一 hard limits
以 provider-neutral 的固定本地 policy 确定性派生：

```python
DEFAULT_OUTPUT_TOKEN_TARGET = 8_192
INPUT_SAFETY_MARGIN_TARGET = 8_192

default_output_tokens = min(
    DEFAULT_OUTPUT_TOKEN_TARGET,
    max_output_tokens,
    total_context_tokens - 1,
)
pre_margin_input_tokens = min(
    max_input_tokens,
    total_context_tokens - default_output_tokens,
)
input_safety_margin_tokens = min(
    INPUT_SAFETY_MARGIN_TARGET,
    pre_margin_input_tokens - 1,
)
```

解析后仍必须证明 output、pre-margin input 与最终 input budget 全部为正；否则 target typed
non-executable。Auxiliary purpose 的既有局部 output cap 只对该 call 取更小 output 并重算同一
budget，不修改 connection 或 hard limits。Catalog-backed target 不允许 env 任意重写 hard
limits/policy；Custom target 提供 hard limits 后也使用同一公式，不能借“custom”恢复隐藏默认值。

---

## 5. Reasoning Control Contract：忠实呈现，不做投影

### 5.1 Reasoning 不是强制能力

Pulsara 不再要求每个模型调用必须思考。此前“始终使用正向 reasoning”只是缺少可靠 target
metadata 时的 compromise；建立 exact universe 后应删除该产品限制。

允许的 target 包括：

- 没有 reasoning 的普通模型；
- reasoning 固定开启、无 selector 的模型；
- 由 provider default决定、Pulsara没有已验证 selector 的模型；
- 支持 on/off toggle 的模型；
- 支持离散 effort choices 的模型；
- 支持 token budget 的模型。

### 5.2 Closed contract：完整保留 models.dev 的组合 option

models.dev 的 `reasoning_options` 是数组，不是单选 union。一个 model 可以同时声明：

```text
toggle + effort
toggle + budget_tokens
effort + budget_tokens
toggle + effort + budget_tokens
```

其中 `toggle` 表示独立 on/off wire control，`effort` 与 `budget_tokens` 表示该 target 公开的
两种 graded control surface。它们是**同一 request 的替代选择族**，不是要求 Pulsara 同时发送
effort 与 budget；当 toggle 与某个 graded control 共存时，选择 graded control 必须由 exact
codec 同时形成“启用 + 该 graded value”。Pulsara 不能像 OpenCode 当前的 effort-first 路径
一样丢掉同 row 中的 toggle 或 budget，也不能把两种 graded control 擅自拼成新协议。

```python
ReasoningControlContract = (
    ReasoningSelectableControls
    | ReasoningFixedOn
    | ReasoningUnavailable
    | ReasoningProviderDefault
)

@dataclass(frozen=True, slots=True)
class ReasoningSelectableControls:
    codec_id: str
    effort: ReasoningEffortChoices | None
    toggle: ReasoningToggle | None
    budget: ReasoningTokenBudgetRange | None
```

`codec_id` 由 exact `route_id + wire_api` adapter contract 给出；三种 option 的存在与值则从
models.dev entry 原样投影。不得由 catalog 的 `npm` 值猜 request path。

#### `ReasoningEffortChoices`

```python
@dataclass(frozen=True, slots=True)
class ReasoningEffortChoice:
    choice_id: str  # null 在产品内使用不冲突的 typed ID
    display_label: str
    catalog_value: str | None

@dataclass(frozen=True, slots=True)
class ReasoningEffortChoices:
    choices: tuple[ReasoningEffortChoice, ...]
```

`choices` 与 models.dev 的 `values` exact 对应。根据其公开编辑 contract，`null` 或 exact
`none` 表示关闭；其他字符串是该 route 公开的原始 control value。Pulsara 不再为
`minimal/low/medium/high/xhigh/max/auto/adaptive` 建全局枚举或 rank。用户可看到
本地化 label，高级信息仍显示 catalog raw value。

对 effort 类型，`choice_id` 原则上就是 route 公开的 exact value；本地化
`display_label` 可以改善可读性，但不能遮蔽、重命名或合并真实选择。高级信息中应能看到原始
value，便于用户理解实际发送的控制。

Choice 顺序只服务 UI，不建立跨 target 的全局 rank。即使两个 target 都有 `high`，也不能
据此认为它们具有可比较的计算量、token budget 或效果。

#### `ReasoningToggle`

```python
@dataclass(frozen=True, slots=True)
class ReasoningToggle:
    pass
```

UI 显示真实开关。开与关都必须由 route/wire codec 生成 exact wire control；
不得假设 `false/true` 是所有 provider 的共同形状。

#### `ReasoningTokenBudgetRange`

```python
@dataclass(frozen=True, slots=True)
class ReasoningTokenBudgetRange:
    minimum_tokens: int | None
    maximum_tokens: int | None
```

若 route 真正暴露连续 budget，UI 显示 catalog 的真实数值范围。`min/max` 在
models.dev 中均可缺失；缺失的一侧不得由 Pulsara 猜全局数字，也绝不能从 model output limit
推导 reasoning budget。只有两侧都存在的 closed range 才渲染数值选择；开放 range 原样保留在
target contract 与详情说明中，但首轮不产生 `ReasoningBudgetSelection`。如果同 row 还有 effort
或 toggle，用户仍可选择那些 exact controls；如果只有开放 budget，则使用 provider-default
omission。禁止像 OpenCode 一样自动把中点和上限命名成 `high/max`。

#### 组合选择

```python
ReasoningSelection = (
    ReasoningUseProviderDefault
    | ReasoningToggleSelection
    | ReasoningEffortSelection
    | ReasoningBudgetSelection
)
```

- `ReasoningUseProviderDefault` 只用于 exact contract 没有 caller selector，或唯一 option 是
  不能形成安全数值选择的开放 budget；它不是菜单中额外伪造的“第零档”；
- `ReasoningToggleSelection(enabled=False|True)` 只在存在 toggle 时可用，codec 分别发送 exact
  toggle-off / toggle-on；
- `ReasoningEffortSelection` exact携带 catalog raw value，包括 `null/none` 这种 effort-owned
  disabled value；它不被改写成 toggle；
- `ReasoningBudgetSelection` 只携带 closed range 内的 exact整数；
- `toggle + effort` 下选择 effort 时，codec 发送 enable + exact effort；`toggle + budget`
  同理；单选 toggle-on 只发送 enable，让 provider 在已开启状态内决定 graded behavior；
- `effort + budget` 下菜单同时呈现“档位”和“Token 预算”两组，用户一次只能选择一组；codec
  发送被选 control并 exact omission另一组；三种 option 共存时再增加 toggle off/on 两项，
  graded selection仍只选 effort或 budget之一，并由 codec同时启用 toggle；
- 一次 request只能有一个 typed selection，adapter不得将 effort与 budget拼出无上游契约的组合。

#### `ReasoningFixedOn`

```python
@dataclass(frozen=True, slots=True)
class ReasoningFixedOn:
    pass
```

models.dev 对 `reasoning=true` 且 `reasoning_options=[]` 的编辑语义是“always-on / no
caller control”。Pulsara 不显示 selector，也不额外发送猜测字段。

#### `ReasoningUnavailable`

```python
@dataclass(frozen=True, slots=True)
class ReasoningUnavailable:
    pass
```

Official target contract明确不提供 reasoning。模型仍可正常用于其已验证的文本/tool用途，UI
只显示“无推理强度选项”，不渲染 disabled selector。

#### `ReasoningProviderDefault`

```python
@dataclass(frozen=True, slots=True)
class ReasoningProviderDefault:
    pass
```

models.dev entry 声称 `reasoning=true`，但缺少 `reasoning_options`。Pulsara 只承诺不发送
reasoning control。UI显示“由模型或服务决定”，不得把它写成“自动思考”或
“已关闭”。

Validator至少保证：

- option type 只能是 models.dev schema 的 `effort/toggle/budget_tokens`；
- 每种 option type 最多一个，effort choices non-empty 且 exact value 不重复；
- effort 含 `null/none` 且又声明 toggle 时，按 models.dev 的编辑契约产生 typed
  `model_catalog_reasoning_options_invalid`；row 仍通过 §4.2 的唯一产品 filter并在目录中显示错误，
  但任何 wire API 均不能据此创建 executable connection；不得静默删 toggle、合并两个 off
  controls或隐藏整条 row；
- budget 的 min/max 同时存在时次序合法；
- `effort`、`toggle`、closed/open `budget` 及其全部合法组合都能被 typed parser表示；每个可执行
  selection kind必须被 exact route/wire codec明确支持，缺 codec只使该 wire target
  non-executable，不构成第二个 catalog filter；
- unavailable/provider-default永远不生成 reasoning request字段。

### 5.3 Selectable target 的默认：真实列表的上中位项

不再存在“未指定就请求全局 MAX”，也不把省略字段伪装成某个档位。对于有 caller control
的 exact target，Pulsara 在会话第一次使用该 target 时，从**该 target 自己的真实选择域**
计算一个显式默认：

```python
def upper_middle(items: tuple[T, ...]) -> T:
    assert items
    return items[len(items) // 2]
```

默认只在同一 target 的 executable control domain 内选择，并采用下列唯一优先级：

```text
至少一个正向 effort choice
    > closed budget range
    > standalone/separate toggle
    > 唯一可用的 disabled effort choice
    > provider-default omission
```

这个优先级只是从同一 target 同时公开的替代 control family 中挑选首个会话默认，不丢弃其他
用户可选 family，也不是跨 target rank。具体语义：

- effort 按 models.dev `values` 的原始顺序展示；先排除已经被 typed contract 识别为
  disabled 的 `null/none`，再取 `len(values) // 2`，所以奇数取正中，偶数取偏强的
  上中位项；若只有 disabled，则默认 disabled；
- `[low, high, max]` 默认 `high`，`[high, max]` 默认 `max`，
  `[low, medium, high, xhigh, max]` 默认 `high`；
- effort 只有 disabled choice且另有 closed budget时，默认使用 budget；没有 closed budget但有
  separate toggle时默认 toggle-on；
- toggle 的两个真实状态按 `disabled, enabled` 展示；仅当没有正向 effort和 closed budget时，
  默认取上中位的 `enabled`；
- closed token-budget range 默认取 `ceil((minimum + maximum) / 2)`；任一边界缺失且 adapter
  不得从 output limit闭合，不伪造数值；若还有 effort/toggle则按上述优先级选择，否则退回
  `ReasoningUseProviderDefault`；
- `toggle + effort` 的默认是正向 effort中位值并由 codec同时启用 toggle；`toggle + budget` 在
  closed range下默认数值中点并同时启用 toggle；`effort + budget` 或三者组合按同一优先级默认
  effort，但菜单仍保留 closed budget替代项；
- fixed-on、unavailable 与 provider-default 没有可选档位，不渲染 selector，也不保存伪选择。

这里的“向上取”只用于**同一 target 已公开选择列表里的默认下标或数值中点**，不是
跨 target projection。Pulsara 不把 `low/high/max` 建成全局可比 rank，也不把一个 target
的选择换算到另一个 target。

默认值一旦写入会话偏好，就与用户后来显式选择一样保持不变；catalog 列表顺序变化不会
重新挑选，只要原值仍属于 exact target contract。

### 5.4 Choice 是 exact target-local 的会话偏好

Reasoning selection 的适用域是 exact `ModelTargetKey`：

```python
@dataclass(frozen=True, slots=True)
class SessionReasoningPreference:
    target: ModelTargetKey
    selection: ReasoningSelection
```

同一 target 内，当前选择在会话中保持锁定，跨 turn、页面刷新、应用重启和控制窗口接管
继续使用，直到用户在该会话的输入框中再次修改。切换 route、wire API 或 model 后：

- 旧 selection 不参与新 target，即使 raw value 同名也不复用；
- 新 target 按 §5.3 计算自己的上中位默认并保存；
- 不做 nearest、向上/向下 projection、alias 或字符串同名迁移；
- catalog 更新后，若 exact target 与原 choice 仍存在，继续锁定原值；若 choice 已消失，
  将其视为不可执行的 stale preference，按新 contract 重新计算默认，并向当前 UI 显示一次
  “可用推理选项已更新”提示，而不是静默把旧值投影到近似档位。

### 5.5 Runtime truth 只保留两层

旧文档的 `requested/effective/wire` 三层在此产品流程中是不必要的。Runtime 只需：

```text
selected_reasoning
    用户或 exact omission 确定的 target-local state/control

wire_reasoning_control
    route/wire adapter 对该 control 的 exact serialization
```

两者之间是 codec lowering，不是档位投影。Provider 若明确报告 actual/effective reasoning，
它只作为 telemetry；reasoning token usage 不能反推实际档位，也不能改变 routing、retry、
compaction 或 tool execution decision。

### 5.6 Resolved value 与 fingerprint

```python
ResolvedReasoningSelection = (
    ResolvedOmittedControl
    | ResolvedToggleControl
    | ResolvedEffortChoice
    | ResolvedTokenBudget
    | ResolvedFixedOn
    | ResolvedUnavailable
    | ResolvedProviderDefault
)
```

exact `ModelTargetContract`（包括完整 reasoning control domain 与 codec contract）进入
`ResolvedModelTargetFact` 及现有 target fingerprint；本 turn 的
`ResolvedReasoningSelection` **不进入** target fact/fingerprint。`ResolvedModelCallFact` exact携带
该次 frozen `ModelConnectionId` 与 selection，provider adapter也只从 call读取本次 output
control。当前 `ResolvedModelOptionsFact` / `resolved_model_options_fingerprint()` 只有 selected
reasoning这一项，因此在 hard cut中删除，不能留下第二个 per-selection fingerprint。

这是 same-epoch 正确性的必要边界：target control domain、wire codec、limits或 replay contract
变化仍会改变 target fingerprint；同一 target domain内从 `low` 改为 `high` 只改变下一条 call
fact，不得被 `_compatibility_reset_reason()` 误判为 `MODEL_TARGET_CHANGED`。Connection ID 也不是
model target 的语义字段，不把它或 API key hash塞进 target fingerprint；provider-input
compatibility按 §12.1 单独 exact比较 connection ID。
不得新增：

- target-contract fingerprint 字段；
- universe fingerprint；
- choice fingerprint；
- `fingerprint -> profile` registry。

现有 target fact携带完整冻结 target contract，按 fingerprint subtraction 规范继续使用唯一真实
边界。`rebind_model_target()` 只 exact重现 target contract；恢复一条 call时再把 turn保存的
connection ID与 selection exact装回 `ResolvedModelCallFact`。两步都不能读取当前 session
selection或用当前 catalog的同名 choice猜测。

---

## 6. Route 与 Gateway 的产品地位

### 6.1 用户先选择 route

设置/创建流程采用：

1. kernel 直接 GET `https://models.dev/api.json`，形成一份 immutable snapshot；
2. kernel 应用 §4.2 的 context 与 model-ID filter；
3. provider selector 直接展示仍有 selectable model 的 route，例如 OpenAI、OpenRouter、
   Zhipu AI、DeepSeek、Moonshot；
4. 用户选 route 后，model selector 只展示该 provider entry 的 selectable models；
5. 用户显式选择 Chat Completions 或 Responses 并填写 API key；普通“添加配置”不要求用户
   理解或填写 base URL；endpoint 按 model-level `provider.api`、provider-level `api`、对应
   `RouteWireContract.default_base_url` 的顺序 exact 解析；三者都没有时返回 typed
   `model_endpoint_unknown`，不能猜 endpoint；
6. 确认页固定说明：“Pulsara 当前只支持与 OpenAI Chat Completions 或 Responses 兼容的
   接口；模型出现在目录中不代表所选提供方一定支持你选择的 API 协议。”；
7. 保存成功后形成一条 `ModelConnectionConfig`；设置页可以继续添加其他配置；
8. 会话页从已保存配置中选择一条，随后根据其 exact target 展示唯一 reasoning selector；
9. 配置页只展示 model contract 摘要，不保存 reasoning 默认；
10. config-check 与 dispatch exact join `route + wire API + model`，并校验 session/turn
   selection membership。

产品可以把一级标签叫“模型提供方”或“连接方式”，但 kernel canonical identity 必须是
`route_id`。OpenRouter 不是 OpenAI route 的一个 checkbox，而是自己的 route。

确认提示是协议边界说明，不是新的 allowlist。§4.2 已明确排除 model ID leaf 以 `claude` 或
`gemini` 开头的条目；除此之外，UI 不再按模型名、provider 名或 family 猜测兼容性。选择
`Chat Completions` 或 `Responses` 也不构成真实可用性证明；实际不兼容时保留 provider 的具体
错误并引导返回该模型配置，不自动换 API。

### 6.2 Gateway target 的语义

对 gateway，`model_id` 是 models.dev 在该 gateway provider entry 下公布的 model identity。
Pulsara 不复制 gateway-level model row；它只维护该 gateway 对已选 wire API 的
route/wire adapter contract。

如果 gateway 可能把请求送到语义不一致的 deployment，又没有强 parameter enforcement，
Pulsara 不能把一次 HTTP 200 当作 universe 证据。可以禁用该 target、要求 pin route，或先完成
独立 conformance；不能在 user turn 中逐 deployment 试错。如果用户选的 wire API 在该
route/model 上不可用，本次 call 以具体 provider/protocol error 终止，UI 提供“修改连接
配置”入口；不自动改用另一个 wire API。

### 6.3 OpenRouter 的特殊知识只能留在 OpenRouter route

OpenRouter 在 models.dev 中与 direct provider 平级；Pulsara 的默认选择器不需再另调
OpenRouter `/models` 来填 model 表。若未来引入 OpenRouter 实时 availability，它只能影响
“当前 credential 可见”的 UI 过滤，不覆盖 models.dev reasoning options，也不热改
active target。

OpenRouter 的 `require_parameters`、fallback policy、reasoning request shape 与 replay fields
属于 OpenRouter `RouteWireContract` / adapter。Agent core、memory、tool executor、compaction
不得出现 `if route == "openrouter"`。

---

## 7. Wire adapter 的唯一所有权

### 7.1 Model contract 说“可选什么”，adapter 说“怎样发送”

models.dev entry 只回答“有哪些 control”，不携带 Pulsara `codec_id`，也不定义 exact
JSON path。Resolver 将它与 exact `route_id + wire_api` 的 closed codec join；route/wire
adapter 唯一拥有：

- reasoning 字段名、嵌套位置与 exact JSON type；
- request defaults 与冲突规则；
- stream framing 和 event normalization；
- tool-call identity correlation；
- terminal / incomplete / usage semantics；
- provider-native reasoning replay；
- exact provider error classification。

示意：

| target contract | route/wire codec lowering |
|---|---|
| OpenAI Responses effort `high` | `reasoning={"effort":"high"}` |
| OpenAI Chat effort `high` | root `reasoning_effort="high"` |
| OpenRouter Chat effort `xhigh` | `reasoning={"effort":"xhigh"}` |
| effort `none`（若 target 定义为关闭） | adapter-owned exact disable value |
| thinking toggle | adapter-owned exact enabled/disabled objects |
| fixed-on / no caller control | exact omission |
| unavailable / provider-default | 不发送 reasoning selector |

表中只说明责任边界；每个 `route_id + wire_api` codec 的实际 shape 必须由
official evidence 与 request golden 冻结，不能靠这张示意表或 models.dev `shape` hint
自动生成。

### 7.2 Hard-cut 删除重复 request owner

激活后：

- 删除 `PULSARA_SUPPORTS_REASONING`；
- 删除 `PULSARA_SUPPORTS_TOOLS` 及其他用 route-wide bool替代 exact target事实的路径；
- 删除 `ProviderProfile.supports_reasoning` / `ModelProfile.supports_reasoning`；
- 删除 raw `LLMOptions.reasoning_effort: str | None`；
- 删除 request-side `PULSARA_THINKING_TYPE/ENABLED` 拼装；
- `request_extra_body` 不得占有 adapter 声明的 reasoning root key；
- adapter 不按 provider/model 名称或 HTTP error 改写 reasoning selection；
- 不保留旧字段 alias、dual read/write、feature flag 或 v1/v2 negotiation。

现有 Chat reasoning delta/replay field registry 与 Responses ordered reasoning items 继续负责
**provider output carrier**。它们不提供 request-side target support 证据，本轮不改变其 exact
保存、public projection 与 replay 语义。

### 7.3 Initial wire API scope

首轮只激活仓库已有、拥有完整 terminal/replay/final-wire contract 的：

```text
openai_chat_completions
openai_responses
```

Anthropic Messages、Bedrock Converse、Gemini native、Mistral native 等必须以后新增完整
`RouteWireContract`、imperative adapter、recorded stream fixture 与 conformance suite；不能只在
catalog 中添加一个字符串便宣称支持。

---

## 8. Generic Chat streamed tool-call correlation hard cut

### 8.1 Index 是 hint，不是 identity 或数组位置

每个 response 使用 adapter-local `TrackedToolCall` object，并维护：

```text
non-empty call_id -> exact tracked call
reported index    -> zero/one/many active tracked calls
ordered tracked calls
```

Reported index 允许任意非负整数、非零起点、稀疏和复用。它不做 origin subtraction，不进入
canonical tool-call ID，不进入 durable replay identity，也不要求 contiguous。

### 8.2 Correlation 优先级

每个 delta 按以下顺序解析：

1. non-empty provider call ID exact 命中；
2. reported index 只对应一个 active/provisional call；
3. ID/index 均缺失时，整个 response 中只有一个可继续的 active call；
4. 否则抛 typed `transport_tool_call_correlation_ambiguous`。

“latest”只可作为第三条中唯一候选的实现细节；多个并行 call 存在时不得盲猜最近一个。

### 8.3 Delayed、reused 与 conflicting identity

- 首个 delta 只有 index 时可创建 provisional call；后续 non-empty ID 可一次性绑定；
- ID 一旦绑定不得改变或被另一个 call 复用；
- 同一 index 后来出现不同 non-empty ID 时可创建另一个 call，并把该 index 标为多候选；
- 多候选 index 的无 ID continuation 必须 fail closed；带 exact ID 的 continuation 仍可解析；
- 同一 ID 与不兼容的 name/function shape 冲突时 fail closed；
- empty ID 按缺失处理，不生成 canonical/local substitute；
- response terminal 前仍没有稳定 call ID 或 function name 时，整次 response fail closed。

### 8.4 Arguments 与 terminal

Arguments delta 只做 exact string append。即使中途累计字符串恰好是合法 JSON，也不能提前
宣告 tool call 完成，因为后续 chunk 仍可能继续追加。

只有 adapter 收到合法 explicit response terminal 后，才可：

1. 验证每个 call 的 ID/name 完整；
2. 解析完整 arguments 为 root JSON object；
3. emit/close normalized tool-call terminal；
4. 构造 completed assistant carrier。

任何 partial/ambiguous/invalid call 都遵循 Round 5A.1：整次 assistant response 不接受，
工具执行次数为 0。

### 8.5 不扩大 Responses parser

Responses 已有 item ID/call ID/output index 的 closed state machine。不能为了“共享代码”把
Chat weak-index tracker 强塞给 Responses。只有两种 wire API 真正拥有相同 correlation
contract 时才抽取共享 primitive；统一 normalized invariant，而不是统一实现形状。

---

## 9. Configuration 与 UI contract

### 9.1 用户级 `ModelConnectionConfig` 集合

模型配置不再是进程启动时的一组全局 env，也不是 PRO/FLASH slot。Kernel 暴露一个专用的
用户级配置集合：

```python
@dataclass(frozen=True, slots=True, order=True)
class ModelConnectionId:
    value: str  # model-connection:<random uuid hex>

@dataclass(frozen=True, slots=True)
class ModelConnectionConfig:
    id: ModelConnectionId
    target: ModelTargetKey
    base_url: str
```

每条配置通过 `target` 保存 exact route/model/wire API IDs，并保存解析后的 endpoint；不复制整份
models.dev row，不保存 reasoning选择或由 §4.7 确定性派生的 execution policy，也不含明文 API
key。`ModelConnectionId` 是普通随机本地主键，不由 target、endpoint 或
credential hash 派生，不建立 fingerprint registry。

首轮配置对象创建后不可原地修改 target、wire API 或 endpoint；需要另一组合时新增一条。
这避免一条已被 session/queue/turn 引用的 ID 在背后变成另一 target。本文首轮只冻结
add/list/select，不渲染尚未实现的 edit/delete/rotate 控件；以后增加删除或 key rotation 时必须
单独定义引用中 session、pending queue 与 credential 的结算语义，不能先放一个不可用按钮。

Catalog-backed target 的 hard limits 直接来自 frozen models.dev row；output/safety policy只按
§4.7 的 provider-neutral公式派生。Custom target 的 hard limits 来自完整 custom contract并使用
同一公式。所有设置、CLI、Host 与 Web 路径都消费同一个 typed store/read model。

### 9.2 Metadata 与 API key 的存储边界

用户级 non-secret metadata 的唯一 authority 是：

```text
${PULSARA_HOME}/model-connections.yaml
```

文件使用 closed `pulsara-model-connections:v1` codec、no-follow 检查、父目录 `0700`、文件
`0600` 与 write/fsync/atomic replace，沿用已有 user configuration 的安全写入 primitive。它只含
ordered `ModelConnectionConfig` metadata；不保存 catalog snapshot、reasoning preference、API
key、credential digest 或数据库 row 副本。

API key 的唯一 durable authority 是操作系统 credential vault。Production 通过窄的
`ModelCredentialStore` port，以固定 service name + `ModelConnectionId` 读写一条 secret；
metadata 不再保存第二个 credential reference。如果系统没有可用 secure
credential backend，添加配置以 typed `secure_credential_store_unavailable` 失败；禁止明文
YAML/JSON、PostgreSQL、browser `localStorage`、命令行参数、环境变量或“暂时 fallback”保存 key。
Tests 使用显式 in-memory fake，不把 production 降级成测试 backend。

添加一组配置时：

1. 对同一 frozen `SelectableModelCatalog` 验证 route/model、context/model-ID filter、wire API、
   endpoint 与 hard limits；
2. 生成新的 random `ModelConnectionId`；
3. 将 API key 写入 credential vault；
4. 原子发布 metadata；
5. 若第 4 步在当前进程内失败，立即删除刚写入的 secret；进程在两步间崩溃可以留下不可见
   orphan secret，不新增 repair job/receipt/checkpoint。它不构成可执行配置。

Web mutation request 中的 key 只在本地 same-origin/loopback request body 与该次 kernel call 内短暂
存在。Mutation response、bootstrap、session snapshot、diagnostics 与日志只能返回
`credential_present: bool`；密码输入保存成功或失败后都清空，不提供 reveal/read-back API。
Provider open 通过 selected connection 的短期 credential borrow 取得 exact secret；现有
`ProcessApiKeyBoundary` 相应 hard-cut 为 value-parameterized 的 process credential admission/
scrub boundary，不再从唯一 `PULSARA_API_KEY` 环境变量读取全局 secret，也不演化成 credential
store。

### 9.3 设置页：模型配置卡片与“添加配置”

设置页的“模型”分区改为一组组已保存配置，而不是“主要模型/轻量模型”或单行“当前模型”：

```text
模型配置                                      [添加配置]

Zhipu AI · GLM-5.3
Chat Completions · 1,000,000 上下文 · API key 已保存

OpenRouter · openai/gpt-5.6-luna
Responses · 400,000 上下文 · API key 已保存
```

“添加配置”只要求四项：

1. Provider/route；
2. Model ID；
3. API key；
4. `Chat Completions` 或 `Responses`。

Provider/model selectors 只消费 kernel 的 `SelectableModelCatalog`：只显示
`limit.context >= 256000` 且 model ID leaf 不以 `claude`/`gemini` 开头的条目；provider 过滤后
没有模型则不显示。前端不得再次实现 threshold、leaf 解析或 family/provider heuristics。

Base URL 不是普通流程的第五个必填项，按 §6.1 的 exact precedence 解析并在确认摘要显示。
Custom endpoint 走 §9.8 的独立高级入口，不能把普通流程变成任意 URL + 猜协议。

若 selected model 有 `provider.shape=responses|completions`，对应 API 标为“models.dev 建议”；
用户选另一项时只显示 non-blocking warning，仍可确认保存。`provider.shape` 缺失时不显示推荐
或 mismatch warning，不从 `npm`、model family、provider 名或 endpoint 字符串补猜。

最终确认页始终显示 route、model ID、API protocol、resolved endpoint、context limit 和固定
说明：

> Pulsara 当前只支持与 OpenAI Chat Completions 或 Responses 兼容的接口。模型出现在目录中
> 不代表所选提供方一定支持你选择的 API 协议；若调用失败，请返回这里更换配置。

保存动作只做 local schema/contract validation 与 credential write，不为了“验证”而偷偷发送
一个模型请求，也不把 HTTP 200 当作协议兼容证明。保存成功后新卡片立即加入同一个 read
model；reasoning control 不出现在设置页。

首轮 HTTP surface 使用不与现有 live-control `/api/connections/...` 混淆的独立路径：

```text
GET  /api/model-catalog
GET  /api/model-configurations
POST /api/model-configurations
POST /api/sessions/{session_id}/model-configuration
POST /api/sessions/{session_id}/reasoning-preference
```

Catalog response 已经完成 §4.2 filter；configuration responses 只含 non-secret summary；session
mutation 继续要求现有 controller/writer authority。不要把 model configuration 复用或塞进已有
browser connection resource。

应用可以在零模型配置、没有 `PULSARA_API_KEY` 环境变量时正常启动并打开设置。此时会话页
显示“请先添加并选择模型配置”，发送按钮不可执行，并提供直达设置页的入口；启动本身不能再
因缺少全局 API key 失败。

### 9.4 会话页的模型配置 selector

对话输入框底部同时拥有“模型”与“推理”两个轻量 selector。模型 selector 列出全部已保存
配置，以 route display name + model display/ID + Chat/Responses 区分；选择的是
`ModelConnectionId`，不是临时 target 字符串。

- 每个 session 最多绑定一条 current connection；未绑定时可以浏览历史，但不能发送；
- 新 session 不暗选列表第一条，也不使用全局“默认模型”；用户在会话页显式选择；
- 选择成功后跨 turn、页面刷新、应用重启和 controller 接管保持，直到用户再次更改；
- 同一 connection 下更改 reasoning 只遵循 §9.5，不改变模型绑定；
- 模型选择只在 session 没有 running turn 且没有 pending `NEW_TURN` 时可改；UI 在 busy/queued
  时禁用并说明“当前工作完成后可切换”，不取消、重排或重写已有工作；
- 从 A 改为 B 时，在一个 session mutation 中同时写入 B 的 connection ID，并把 reasoning
  preference 换成 B target 按 §5.3 计算的默认；旧 choice 即使同名也不迁移；
- 连接切换不回溯修改 canonical transcript。下一条 `NEW_TURN` dispatch 发现 connection 与
  当前 installed epoch 不同后，以 B 建立显式 new cold epoch；不得 same-epoch 热换 endpoint、
  model、wire API 或 credential；
- 同一进程中的另一个 session 可以继续使用 A，互不影响。

这项 idle/no-pending 条件是为了让一条会话队列保持单一 exact execution binding，而不是
turn/task lifetime cap。用户可以等待已有工作自然完成或显式取消它，再切换配置。

### 9.5 对话输入框是唯一 reasoning 选择入口

对话页输入框底部提供一个轻量“推理”按钮。它不是 per-turn 临时 override，也不提供
“设为连接配置默认”分支：

- 菜单来自当前 exact target 的 catalog controls，不是全局五档；
- 新会话按 §5.3 显示并保存当前 target 的上中位默认；
- 用户选择一个真实 control 后立即更新该会话偏好，按钮保持该值，直到用户再次修改；
- 每次提交 `NEW_TURN` 时，command 必须携带按钮当前的 exact
  `SessionReasoningPreference`；成功 admission 后该值属于这条输入，而不是执行时再读取
  `sessions` 的最新值；
- 点击发送后即使该 prompt 排队很久，也使用提交时的冻结值；
- 同一 turn 的 provider retry、tool continuation 与 model continuation 全部使用 turn 的
  冻结值；
- 流正在运行时改按钮只改变会话的下一次 `NEW_TURN` 选择，不热改已经开始的 turn；
- `STEER_ACTIVE_TURN` 不改变当前 turn 的 reasoning；如果用户先改按钮再发送 steer，新的
  选择仍只作用于下一条新 turn；
- 与同一已接纳用户工作绑定的内部 Plan continuation/terminal continuation 继承 origin
  turn 的冻结值，不回读当前按钮；
- subagent task属于该用户工作的 agentic分支，exact继承 parent/origin turn 的 connection +
  reasoning selection；它不按启动时的 session按钮重算，也不改用 auxiliary默认；
- summary/governance 等 auxiliary call 使用同一个 exact target，但不读取 composer 偏好；
  它们使用 §5.3 的无 UI 默认，且不能改变会话按钮。

Reasoning control 不是 context-bearing message，因此会话偏好变化不写 transcript，不重写
SYSTEM/tools/messages prefix，也不触发新 epoch。它仍必须在 prompt admission 时 exact
resolve，在 turn dispatch 前以同一冻结值验证，并进入该 turn 的 `ResolvedModelCall`。

### 9.6 Session、queue、turn 的最小持久化与 admission freeze

只把这个选择留在 React state 或 `localStorage` 不够：页面刷新、另一个控制窗口、CLI、Host
重启会看到不同值；更重要的是，队列中的旧 prompt 可能误用稍后改变的值。Canonical
storage 因此直接落在现有三张表，不新建通用 settings 表。Model connection selection 与
reasoning selection 在同一 admission boundary 冻结：

```text
pulsara_v3.sessions.reasoning_preference
    当前输入框偏好；nullable jsonb，编码完整 SessionReasoningPreference

pulsara_v3.sessions.model_connection_id
    当前会话选择；nullable text，未配置时允许浏览但禁止 NEW_TURN

pulsara_v3.prompt_queue_items.reasoning_selection
    NEW_TURN 提交时的 frozen exact copy；STEER_ACTIVE_TURN 必须为 null；无 caller
    control 的 target 可为 null

pulsara_v3.prompt_queue_items.model_connection_id
    NEW_TURN 提交时的 frozen ModelConnectionId；STEER_ACTIVE_TURN 必须为 null

pulsara_v3.turns.reasoning_selection
    turn admission 时从 direct command 或 queue item exact copy；无 caller control 的
    target 可为 null；SUBAGENT_TASK 与 Plan/terminal continuation exact继承
    origin/parent turn

pulsara_v3.turns.model_connection_id
    ROOT turn 从 direct command/queue exact copy；SUBAGENT_TASK、Plan/terminal continuation
    exact 继承 origin/parent turn
```

reasoning 三列中的非 null 值复用同一个 `TargetReasoningSelection` closed codec；JSON 对象完整携带
`ModelTargetKey` 与 `ReasoningSelection`，不另建 target/choice fingerprint。数据库只检查
nullable/object 与 queue delivery union，caller-control 是否要求 selection 由唯一
codec/repository boundary 对 frozen target contract 验证；不要为了约束而拆出四个重复列或
创建 profile 表。Connection ID 列只检查 closed textual ID 与 delivery/inheritance union；
PostgreSQL 不复制 user config，不对 `${PULSARA_HOME}` 文件伪造 FK。

所有 user-originated ROOT admission path 都必须把 model connection 与 preference 纳入 exact
command payload、semantic digest 与 confirmation comparison。`queue_prompt.v1` / 对应 direct
submit schema hard-cut bump；不能让同一个 command ID 用不同 connection 或 reasoning selection
仍被判 compatible。

Connection/preference mutation 与 user-originated `NEW_TURN` admission必须锁同一 session row。
Admission 在任何 queue item、entry或 turn写入前，将 command携带的完整 connection + preference
与 canonical `sessions.model_connection_id + reasoning_preference` exact比较；不一致返回 typed
`stale_model_selection` conflict并携带最新 non-secret session snapshot，数据库无写入。它只防止
旧窗口在 B 已成为当前选择后仍提交 A，不影响已经成功 admission并在 queue中冻结的旧 prompt；
queue消费绝不能回读 session current pair。

配置 metadata、catalog target与 selection validation都是 non-secret pure value resolution，在打开
canonical PostgreSQL transaction前从 Host本次冻结的 process-local snapshots完成；事务内禁止读取
`${PULSARA_HOME}`、访问 OS credential vault或取得 credential borrow。队列消费采用：

1. 在事务外 exact decode queue payload的 non-secret connection ID + selection，并从冻结 snapshots
   取得 immutable connection/target value；
2. metadata缺失、catalog semantic invalid或 route/wire codec不支持属于永久 admission failure，
   复用现有 queue `REJECTED` + `PROMPT_REJECTED` settlement，写精确 terminal reason，不创建 turn；
   暂时没有可用 catalog snapshot时保持 `PENDING`并按现有 Host调度稍后再试，不增加 lifetime cap、
   job或 durable retry counter；
3. 进入 canonical transaction，锁定 pending queue head并 exact比较事务外 prepared 的 queue item
   identity、connection ID与 selection；
4. 将相同 connection ID 与 typed selection写入新 turn，消费 queue item、接受用户 entry并创建
   turn；
5. commit后到该 turn第一次 provider open才以 frozen connection ID取得唯一短期 credential borrow；
   metadata/target不再重读。Vault backend/key此时缺失按现有 model-call failure路径终止已接纳 turn，
   不把它倒退成 queue rejection，也不作第二次 borrow。

Direct ROOT admission使用同一分界：事务前pure resolve，事务内锁 session并做 stale pair check及
turn/entry写入，commit后 provider open只 borrow一次 credential。这样 filesystem/vault延迟不会
占用 session/queue数据库锁，borrow、prepared plan与 transport handle仍各自只有一个 linear owner。

`sessions.reasoning_preference` 只表示下一条新输入的当前偏好，不是历史 turn authority。
`sessions.model_connection_id` 同样只表示下一个 NEW_TURN 的当前选择；历史执行以 queue/turn 与
resolved model-call fact 为准。
多窗口更新采用现有 controller/writer authority，最后一个成功的用户选择成为当前值；发送
请求必须携带 UI 已确认的同一值，并接受上述 stale conflict，而不能把 payload中的旧 pair当成
per-turn override。无需新增 preference-changed event：当前控制窗口直接采用 mutation response，
stale conflict、接管或重连都从 canonical session snapshot读取。

### 9.7 Kernel wiring、config-check 与 env hard cut

`LLMConfig.pro/flash` 不收敛成一个新的全局 `LLMConfig.model`；它们整体由用户级
`ModelConnectionStore`、`ModelCredentialStore` 与 per-session binding 取代。`ModelRole` 整个
类型删除，而不是留下只有 `PRO` 的单值 enum。所有依赖 role 的 API 参数、fact 字段、CLI
option 与分支一并删除。

Main model call、subagent、summary、compaction 与 memory governance 直接取得当前 origin
session/turn 已冻结的同一 `ModelConnectionId` 和 `ResolvedModelTarget`。调用目的继续由已有
purpose 类型表达；purpose、output cap 与 deadline 不拥有 model routing authority，也不能
借 auxiliary 名义解析另一配置。没有 session/turn origin 的设置/catalog 操作不打开模型。

生产模型配置的唯一入口是上述 store。删除：

```text
PULSARA_API_KEY
PULSARA_PROVIDER
PULSARA_API
PULSARA_BASE_URL
PULSARA_MODEL
PULSARA_PRO_*
PULSARA_FLASH_*
--model-role
```

不保留 env import、首次启动迁移、alias、fallback 或双读。Retry/transport 等不属于某个 model
connection 的 process policy env 不在此列。Dogfood 可以由测试 harness 从环境读取 secret 后
注入同一个 in-memory credential-store port，但 production app/config-check 不读取它；harness
全程仍不得输出 key。

`pulsara app` / local Web server 在零配置时也必须启动。`config-check` 读取同一 metadata/
credential store 与一次 catalog snapshot，逐条报告：credential 是否存在、entry 是否仍在
selectable catalog、route/wire adapter 是否注册、endpoint 是否可解析、§4.7 派生 budget 是否
合法。零配置返回可操作的 `model_connection_required` 状态，但不能阻止用户启动 app
进入设置页。Config-check 不发送模型请求，不输出 key。

### 9.8 Custom route

Custom endpoint 必须显式给出：

- route identity 与 exact wire API；
- endpoint；
- 完整 `ModelTargetContract`；
- closed request/reasoning codec ID；
- hard limits；
- reasoning 与 tool-use facts。

Custom row 只用于 models.dev 不存在的私有 endpoint/model。它必须整体提供同等 catalog facts，
并走同一 validator/resolver；不得按 field 覆盖一条 models.dev row，也不为 arbitrary custom
wire shape开放 JSON field-path DSL。它不受 models.dev context/name selector filter，因为不是
models.dev row；首轮普通“添加配置”不渲染 custom route 表单。

---

## 10. Catalog 与维护流程

### 10.1 models.dev API 的直接调用

唯一 production catalog source 是：

```http
GET https://models.dev/api.json
Accept: application/json
```

该接口返回一个以 provider ID 为 key 的大 JSON object，没有分页或逐 provider 查询协议。
Kernel 一次取得完整 response，一次性 parse 为 immutable `ModelCatalogSnapshot`，再按 §4.2
构造 `SelectableModelCatalog`。Raw parser 只投影：

```text
provider: id, name, env, api, doc
model: id, name, reasoning, reasoning_options, tool_call,
       interleaved, limit.context, limit.input?, limit.output,
       provider.api?, provider.shape?
```

model-level `provider.api` 若存在，作为该 exact model 比 provider-level `api` 更具体的
endpoint 预填值。`provider.shape` 只接受 `responses|completions`，并只作 UI hint。
`npm`、pricing、marketing description、benchmark、modalities 等即使存在，也不进入首轮
execution contract。Parser 要求 outer provider key = `provider.id`、outer model key = `model.id`；
不对 ID 做 lower-case、alias、family 或 substring normalization。

`limit.context < 256000`、Claude/Gemini leaf predicate 命中的 row 仍可存在于这次 raw
snapshot，以便给出准确 validation reason；它们永远不进入 UI provider/model list、不能创建
catalog-backed `ModelConnectionConfig`，也不能由 CLI/config-check 绕过。该过滤不使用或改变
`provider.shape`。

Catalog GET 不带 provider API key，不携带 prompt、model selection、workspace 或用户数据。

### 10.2 Fetch lifecycle：直读，但不在 request path 热刷新

允许发生直接 GET 的边界只有：

1. 打开设置页或“添加模型配置”流程；
2. 用户显式点击“刷新模型列表”；
3. `config-check` 或新 cold Host 启动时解析 saved model connections。

每次成功取得的 snapshot 在该次配置会话/Host 内冻结。Provider call、retry、tool
continuation、summary 和 compaction 不再读网络 catalog。不引入：

- checked-in `model_targets.json` 或 Python per-model constants；
- 本地磁盘 mirror/cache、TTL refresh worker 或 file lock；
- PostgreSQL table、event、job、checkpoint、receipt 或 catalog fingerprint registry。

一个正在运行的 session/epoch 在 models.dev 稍后改变或暂时不可达时仍持有已冻结 target。
新的设置会话/config-check 获取失败时返回 typed `model_catalog_unavailable`，不回落到
隐藏的旧表或猜测数据。使用普通 connect/read-idle transport watchdog，不为 Host/turn 增加
total wall-clock lifetime cap。

### 10.3 责任分界

| 责任 | 所有者 |
|---|---|
| provider/model 列表、display name、默认 endpoint hint | models.dev |
| limits、reasoning options、tool-call 布尔值、interleaved hint | models.dev |
| 256k context 与 Claude/Gemini model-ID leaf 选择过滤 | Pulsara kernel 的单一 `SelectableModelCatalog` predicate |
| Responses / Completions 非强制建议（存在时） | models.dev `model.provider.shape` |
| Chat / Responses 的选择 | 用户保存的 `ModelConnectionConfig` |
| API key durable value | OS credential vault；只由 `ModelCredentialStore` borrow |
| endpoint 与 non-secret connection metadata | `${PULSARA_HOME}/model-connections.yaml` |
| request field、SSE、terminal、tool correlation、replay | Pulsara route/wire adapter |
| 当前会话 model connection | 会话页；session 保存，NEW_TURN admission 冻结 |
| 当前会话 reasoning preference | 用户对话输入框；session 保存，NEW_TURN admission 冻结 |

这个边界意味着 Pulsara 不再调研并手工录入每个 provider/model。它仍需维护少量
`route_id + wire_api` request codec：例如 Zhipu Chat 的 `thinking.type` +
`reasoning_effort`，与 OpenRouter Chat 的 reasoning body 形状可能不同。

### 10.4 Zhipu AI / GLM-5.3 的端到端示例

2026-09-03 直读 models.dev 的 exact row 为：

```text
route_id: zhipuai
route name: Zhipu AI
provider.api: https://open.bigmodel.cn/api/paas/v4
credential hint: ZHIPU_API_KEY
model_id: glm-5.3
model name: GLM-5.3
reasoning options: effort [low, high, max]
tool_call: true
interleaved.field: reasoning_content
provider.shape: absent
context: 1,000,000
output: 131,072
```

因为该 row 当前没有 `provider.shape`，GUI 对 Chat/Responses 保持中性，不显示假推荐。

傻瓜式 GUI 配置为：

```text
提供方        Zhipu AI
模型            GLM-5.3
API 协议        Chat Completions       # 用户也可选 Responses
API Key         ••••••••
[继续]

确认
Endpoint        https://open.bigmodel.cn/api/paas/v4  # 自动解析，只读
[添加配置]
```

确认页同时显示“Pulsara 当前只支持与 OpenAI Chat Completions 或 Responses 兼容的接口”；
`provider.shape` 缺失不产生推荐。保存后设置页新增一张配置卡片，但不会发送测试模型请求。

该 connection config 不保存 reasoning。用户在一个会话中选择它时，真实列表
`[low, high, max]` 的上中位项为 `high`，因此对话输入框底部显示：

```text
[模型：Zhipu AI · GLM-5.3 · Chat ▾] [推理：High ▾] [发送]
```

用户若不修改，选 Chat 发送时，Zhipu Chat route/wire codec 产生的关键 request material 为：

```json
{
  "model": "glm-5.3",
  "messages": ["...canonical lowered messages..."],
  "thinking": {"type": "enabled"},
  "reasoning_effort": "high",
  "stream": true
}
```

若用户在输入框改为 `max`，该会话随后所有新 turn 默认继续使用 `max`，直到用户再次修改；
已经排队或已经开始的 turn 保持各自提交时的冻结值。

若用户改选 Responses，就使用 Responses adapter 和该 endpoint 配置。如 route/model 实际不
接受该 API，Pulsara 显示原始的 404/400/protocol error 与“修改连接配置”；不在背景
自动改用 Chat，也不重放已产生 semantic output 的 turn。

### 10.5 Catalog 变化

Saved connection 只保存 exact target IDs 与 endpoint，不保存派生 policy、
reasoning 或 catalog row。新 cold Host 读到的 models.dev row 若已删除、`limit.context` 已降到
256,000 以下，或现在命中 Claude/Gemini leaf filter，该 connection 标为不可选择并要求用户
新增另一配置；不自动换 model/route/API。会话保存的 reasoning preference 若仍属于 exact target
就继续使用；若 target 或 choice 已失效，按 §5.4 重新计算默认并给出 UI 提示，不做 projection
或 alias。Active turn/installed epoch 已冻结的 connection、target 与 selection 不受 catalog
refresh 影响。

---

## 11. Retry、fallback 与 transport

### 11.1 保留现有 transparent retry barrier

唯一 transparent retry 仍是：

```text
retryable physical/provider exception
AND no normalized semantic payload emitted
AND no adapter terminal emitted
AND exact same prepared request
AND existing finite physical-attempt budget remains
```

Retry 不重新解析 universe、不改 target、不改 reasoning selection、不重新 compile context、不换
wire API，也不重新选择 tool surface。

### 11.2 明确禁止通过请求错误发现 target support

虽然外部调研常称其为 “capability probing”，Pulsara 中该路径一律禁止：

- `max -> xhigh -> high -> medium -> low` 网络试探；
- 任意 400 被解释为 effort 不支持；
- 401/403/429/context overflow 被当作 model support 证据；
- provider 接受未知字段后缓存为“已支持”；
- semantic output 后更换 model/route/effort 重跑；
- 根据 reasoning usage 自动换 choice。

若未来某 adapter 对一个精确 provider protocol error 有已证明、语义不变且 pre-semantic 的恢复
动作，必须单独修改 adapter contract 与测试。它不能演化为通用 model negotiation。

### 11.3 Gateway fallback

Gateway 自身的 upstream fallback 必须被其 `RouteWireContract` 明确约束。若 route 无法保证：

- selected reasoning control（若有）不会被忽略；
- tool/replay/terminal 语义不漂移；
- semantic stream 已开始后不会透明换模型；

则 Pulsara 要求 pin/disable fallback，或将该 route/wire adapter 标为不可执行。Gateway
telemetry 不重写 resolved target。

---

## 12. Prefix、replay、compaction 与 ownership

### 12.1 Prefix continuity

Universe/catalog 变化不能成为第三种 prefix rebase boundary。当前 session/epoch 冻结的
connection/target 继续使用。会话在 §9.4 的 idle boundary 选择另一 connection 后，下一条
NEW_TURN 使用既有 **new cold epoch** 边界；显式 adopted compaction successor 仍是另一条获准
边界。设置页新增别的配置不会影响任何现有 session。
对话输入框的会话级 reasoning preference 不改变 target 或 context-bearing prefix；每条
`NEW_TURN` 输入在点击发送时冻结自己的副本，只作为该 turn 的 output control。

同一 epoch 内：

- SYSTEM 与 provider tools byte-identical；
- messages 只追加 suffix；
- reasoning control 不注入 dynamic pre-prefix content；
- 后续 catalog refresh 不热替换 route/wire/model contract；
- credential borrow、配置列表变化或另一个 session 的选择不改当前 epoch。

Existing provider-input compatibility join 增加 frozen `ModelConnectionId` 的 exact equality；不把
它哈希进另一个 fingerprint。同 target但不同 connection仍不兼容，必须走 new cold epoch。

### 12.2 Provider replay

Reasoning request selection 是 output control，不翻译、不改写、不删除历史 native reasoning
carrier。更换 `ModelConnectionId` 一律按 cold semantic continuation 处理，即使新旧 model ID
相同；真正改变 wire API、endpoint、credential binding、normalized model identity、transport
binding、request codec 或 replay contract 时更不能 same-epoch 继续。

### 12.3 Final-wire estimation 与 compaction

Reasoning selector、output cap、model ID、stream/usage 均不是 context-bearing provider input：

- resolved selection进入 call/request fact，不进入 target fingerprint；
- normal payload builder发送它；
- final context-bearing materialization/estimator 不计入它；
- 已接受的 provider replay carrier仍按 exact wire materialization计入；
- summary、source quote、successor quote 与 ordinary send复用同一 resolved target和 adapter；
- compaction 不解析 route/model 名称，不调用 provider token-count API，也不从 usage 判断
  reasoning 是否生效。

若未来某 reasoning option 实际注入 model-visible prompt/content，它必须改归 final-wire
materializer，而不能继续伪装成 output control。

### 12.4 Linear owner

Universe resolution 与 settings read model 都是 pure value construction，不取得 provider
execution authority。Credential store 只在 turn/admission transaction已 commit、dispatch已冻结
`ModelConnectionId` 且真正进入 provider open时签发一份短期 linear borrow；任何 canonical
PostgreSQL transaction内都不得访问 vault。现有 prepared plan/open-once/transport handle 仍是
唯一 execution owner：

- resolver不 materialize provider input；
- 同一 provider attempt只消费一份 credential borrow，close/cancel后不可复用；
- adapter内不重新 resolve target；
- retry只重放同一个 prepared physical request；
- 不因 route fallback创建 alias plan；
- cancellation/close继续由现有 transport owner结算。

---

## 13. 文件级实施清单

### 13.1 新增 models.dev catalog client 与 target resolver

```text
src/pulsara_agent/llm/model_catalog.py
src/pulsara_agent/llm/model_target.py
tests/test_llm_model_catalog.py
tests/test_llm_model_target.py
```

职责：

- 直接 GET/parse models.dev `api.json`；
- `ModelCatalogSnapshot`、`SelectableModelCatalog`、`ModelCatalogEntryKey`、`ModelTargetKey`、
  `ModelTargetContract`；
- §4.2 的 exact 256k + model-ID leaf filter，以及过滤后空 provider 删除；
- 完整保留组合 reasoning options 与 optional `provider.shape` hint；
- exact catalog lookup、route/wire codec join 与 validation；
- provider/model key 不一致、unknown schema、missing codec、invalid limits 与 invalid selection 拒绝；
- custom target 与 catalog-backed target 共享 resolved contract validator。

不得新增 production `model_targets.v*.json`、per-model Python constants 或后台 refresh worker。

不得创建 `llm/capabilities.py`。

### 13.2 `llm/provider.py` 与 route/wire types

- 将 overloaded `ProviderProfile` hard-cut 为 route/wire contract；
- 删除 model support bool；
- 保留并明确 output-side Chat replay field contract；
- request defaults只能包含 adapter allowlist 中的非 reasoning keys；
- adapter-owned reasoning key发生冲突时构造阶段拒绝；
- OpenRouter 等 gateway以独立 route注册。

文件名是否继续叫 `provider.py` 不影响语义；类型名与 public contract 不得继续使用
`ModelCapability*`。

### 13.3 Model connection 与 credential store

```text
src/pulsara_agent/llm/model_connections.py
src/pulsara_agent/llm/model_credentials.py
src/pulsara_agent/process_api_key_boundary.py
tests/test_llm_model_connections.py
tests/test_llm_model_credentials.py
```

- 实现 `${PULSARA_HOME}/model-connections.yaml` closed codec、no-follow、0600 与 atomic replace；
- 实现 add/list/read exact `ModelConnectionConfig`，不保存 catalog row 或 reasoning；
- production `ModelCredentialStore` 使用 OS credential vault；test fake 显式注入；
- Web/API/read model 从不返回 secret，只返回 `credential_present`；
- `ProcessApiKeyBoundary` hard-cut 为由 caller 提供 exact borrowed secret 的 process credential
  boundary，不再观察唯一环境变量，也不成为 store；
- add metadata 失败时 best-effort 删除当次刚写入的 secret，不增加 durable recovery machinery。

### 13.4 `llm/models.py`、`llm/config.py`

- `ModelProfile` 不再从 provider profile复制 support bool；
- 删除 `ModelRole`、`ModelProfile.role`、`LLMConfig.pro/flash`、`pro_model/flash_model` aliases、
  `model_for(role)` 与 `slot_for(role)`；
- 不新增全局 `LLMConfig.model`；model target/endpoint/credential 来自 per-session selected
  `ModelConnectionConfig`；
- hard limits从 frozen models.dev entry取得；
- connection config只保留 ID/target/endpoint；output/safety policy按 §4.7 确定性派生；
- `config-check` 逐条完成 exact validation，但零配置不阻止 app启动；
- hard-cut 删除旧 thinking/support env路径、global model/API key env、全部 PRO/FLASH env 与
  display-only compatibility alias。

### 13.5 `llm/request.py`、`llm/resolution.py`、`primitives/model_call.py`

- raw `reasoning_effort` 改为 closed `ReasoningSelection`；
- resolver 不再接收 role，直接 exact join唯一 route/wire/model/codec/limits/selection；
- `ResolvedModelCallFact` exact携带 `ModelConnectionId` 与本次 reasoning selection；provider
  credential lookup与 request builder不得回读 session current selection；
- `ResolvedModelTargetFact` contract version hard-cut bump；
- 删除 durable/frozen fact 中的 `model_role`，target identity 只由真实 target contract构成；
- target fact携带完整 control-domain contract但不携带本次 selection；删除只为 selected
  reasoning存在的 `ResolvedModelOptionsFact` / options fingerprint，不新增 fingerprint字段；
- `rebind_model_target()` exact重现 target；call恢复另从 turn exact装回 connection + selection；
- tools存在时 exact验证 catalog `tool_call=true` 与 route/wire tool codec；
- invalid selection、`tool_call=false` 或缺少本地 codec 时在 provider open前 typed reject。

### 13.6 Chat / Responses adapters

- payload builder只消费 typed resolved reasoning selection；
- closed codec唯一 materialize exact wire shape；
- final context-bearing materializer继续与 ordinary send共享；
- Chat accumulator改为 §8 tracker，删除 dense/origin path；
- Responses保留 existing exact item state machine；
- terminal/retry/usage/replay行为不变。

### 13.7 Kernel、auxiliary、CLI 与 Web 的 per-session single-connection wiring

至少修改：

```text
src/pulsara_agent/conversation_kernel/direct_model.py
src/pulsara_agent/conversation_kernel/auxiliary_model.py
src/pulsara_agent/conversation_kernel/host.py
src/pulsara_agent/web_app/application.py
src/pulsara_agent/web_app/session_controller.py
src/pulsara_agent/settings.py
src/pulsara_agent/cli.py
frontend/lib/runtime-adapter.ts
frontend/components/settings-view.tsx
frontend/app/pulsara-app.tsx
```

- Host/session open、resume 与 Web application 删除 `model_role` 参数；
- `DirectKernelModelPort`、subagent launch、summary、compaction 与 memory governance全部绑定
  origin session/turn 冻结的同一个 connection/target；不同 session可使用不同 connection；
- auxiliary `_with_output_cap` 只构造该次 call 的 bounded output options，不再复制或改写一个
  FLASH slot；
- call purpose、局部 output cap 与 deadline继续保持，不被误删成“所有调用参数相同”；
- CLI 删除 `--model-role` 与 global model/env config；bootstrap/read model删除
  `pro_model/flash_model`，改为 non-secret `model_connections` collection；
- 设置页删除“主要模型/轻量模型”两行与单行“当前模型”，显示 cards + 添加流程；
- app无配置也能启动；provider dispatch没有 selected connection时 typed拒绝；
- 不新增 `AUXILIARY`、`DEFAULT` 等替代 role enum，也不保留单值 `PRO` enum。

### 13.8 Session connection/preference、prompt admission 与 PostgreSQL baseline

至少修改：

```text
src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql
src/pulsara_agent/storage/migrations/resources/0000_conversation_kernel_expected_catalog_v1.json
src/pulsara_agent/conversation_kernel/_repository/authority.py
src/pulsara_agent/conversation_kernel/_repository/prompts.py
src/pulsara_agent/conversation_kernel/_repository/conversation.py
src/pulsara_agent/conversation_kernel/steer.py
src/pulsara_agent/conversation_kernel/contracts.py
src/pulsara_agent/conversation_kernel/host.py
src/pulsara_agent/web_app/session_controller.py
src/pulsara_agent/web_app/http_server.py
```

职责：

- baseline 直接 hard-cut 增加 §9.6 的六个字段；本地 disposable PostgreSQL reset，不写线上迁移
  或 dual-read；
- 提供 controller-owned、idle/no-pending gated session connection + preference mutation API；
  mutation与 NEW_TURN admission锁同一 session row并 exact比较 command/canonical pair；
- session snapshot/connect payload 带 current connection ID、target-local preference 与 non-secret
  connection summaries；
- 所有 NEW_TURN command exact carry/freeze connection + selection，STEER 不改当前 turn；
- prompt command digest、confirmation 与 queue consumption exact compare两者；
- non-secret config/target在 transaction外 pure resolve；queue -> turn 在唯一 transaction 中复制
  同一 ID/value；credential只在 commit后的 provider open borrow；subagent/continuation exact继承；
- connection switch在下一次 dispatch走 new cold epoch，不 same-epoch rebase；
- 不增加 preference/connection event、job、checkpoint、revision table 或 PostgreSQL settings表。

### 13.9 Settings 与 composer read model

本轮必须同时启用设置与 composer UI，至少修改：

```text
frontend/components/settings-view.tsx
frontend/components/model-configuration-dialog.tsx（若不内联于 settings view）
frontend/lib/pulsara-types.ts
frontend/lib/runtime-adapter.ts
frontend/components/settings-view.test.tsx（或现有相邻测试）
frontend/components/workbench-view.tsx
frontend/components/composer.tsx（若 composer仍在该文件）
frontend/app/pulsara-app.tsx
frontend/app/pulsara-app.test.tsx
frontend/app/styles/settings.css
frontend/app/styles/workbench.css（或当前 composer 所在样式文件）
```

前端只消费 kernel 投影，不直接二次 parse models.dev JSON。Settings 显示配置 cards 与四字段
添加流程，不保存 reasoning、不保留 API key；对话输入框提供 saved-connection selector 与唯一
reasoning selector，并把两者随每条 NEW_TURN command发送。增加对应 catalog/list/add/session
selection HTTP/controller endpoints；所有 response均为 non-secret read model。

### 13.10 Existing regressions

至少检查：

```text
tests/test_settings.py
tests/test_round5a1_provider_output_termination.py
tests/test_round5a2_durable_provider_replay.py
tests/test_round3_1_provider_input_prefix_continuity.py
tests/test_round5b_long_horizon_context_compaction.py
tests/test_round5a2_durable_provider_replay_postgres.py
README.md
README.zh-CN.md
.env.example（若存在）
```

---

## 14. 实施顺序

1. 写 models.dev recorded fixture parser、256k/Claude/Gemini filter、exact selection、
   shape-warning、model-connection store、request golden 和 session target-switch failing tests。
2. 新增 direct catalog client、`ModelCatalogSnapshot`、`SelectableModelCatalog` 与 closed
   reasoning contracts。
3. 用至少两个“同 model、不同 route、不同 choices”的 fixture证明 identity；另覆盖
   shape present/absent。
4. 实现 `route + user-selected wire API + model` exact resolver，不建 per-model local table。
5. 实现 non-secret model-connection metadata store、OS credential store 与 secret borrow boundary。
6. hard-cut config/models/resolution/facts，删除 PRO/FLASH、global env target、role参数/fact、
   bool/raw/omit-default旧路径。
7. hard-cut CLI、Host/Web wiring与 bootstrap，使每个 session/turn绑定一条 connection。
8. 让 Chat/Responses adapter消费 typed selection并删除 generic reasoning owner。
9. 用 generic tracker取代 Chat dense/origin index accumulator。
10. hard-cut baseline、session connection/preference API、prompt command/queue/turn freeze 与 exact
    confirmation；reset 已核验的本地 disposable PostgreSQL。
11. 接入 kernel catalog read model、设置页 cards/add flow，以及 composer model/reasoning selector。
12. 实现 catalog unavailable、filter reason、wire shape mismatch warning 与“修改连接配置”错误入口。
13. 跑 terminal/retry/replay/prefix/final-wire focused regressions。
14. 更新 config-check、README与架构说明，只描述新路径。
15. 运行 PostgreSQL schema/replay regression，确认 event/subject/guard/relation/job oracle
    类别没有增加。
16. 对每个已实现 route/wire codec 选取代表 target 运行 real-provider dogfood。

不得保留旧的 local model table 等待以后切换。Production 只能有 models.dev-backed 与
explicit custom target 两种来源，并共用一条 exact resolved path。

---

## 15. Required test matrix

### 15.1 Universe identity 与 validation

- direct GET 成功解析完整 models.dev fixture 为一份 immutable snapshot；
- provider/model outer keys 与 inner IDs 不一致时拒绝；
- `limit.context=255999` 不进入 selectable catalog，`256000` exact进入；filter只读
  `limit.context`，不误用 input/output；
- raw `claude-*` / `gemini-*`、`anthropic/claude-*`、`google/gemini-*`、
  `~anthropic/claude-*`、`~Claude-*` 与 `provider/~GeMiNi-*` 均被 model-ID leaf predicate排除；
  `~~claude-*` 只移除一个 `~` 后不命中，证明实现没有 strip-all；大小写 predicate结果一致但
  raw ID保持不变；
- provider/family/display name/shape 不触发额外模型过滤；过滤后无模型的 provider不显示；
- 通过唯一产品 filter但 reasoning metadata或本地 codec invalid的 row仍在同一 catalog read model
  中显示 typed non-executable reason，不被第二个 filter静默删除，也不能保存 connection；
- frontend、CLI与 config-check消费同一 filtered read model，不各自计算阈值或 startswith；
- exact `route + wire API + model` 命中；
- route相同但 wire不同不误命中；
- model相同但 route不同不误命中；
- unknown model不做 substring/family fallback；
- route/wire缺少 registered adapter时该 target non-executable且添加 connection拒绝，catalog row
  仍可见；
- catalog option kind/组合缺少 route/wire codec时拒绝该 explicit selection；
- invalid/contradictory limits拒绝；
- tools存在但 `model.tool_call=false` 时在 open前拒绝；
- `model.provider.api` 正确覆盖 provider-level endpoint 预填值；
- `provider.shape=responses|completions` 正确投影为 hint，缺失为 neutral；
- custom/catalog-backed走同一 resolved validator，custom不 field-patch catalog row；
- catalog fetch不携带 API key/prompt/workspace数据；
- snapshot取得后 request lookup不读网络、不写数据库、不产生 event/job；
- fetch/schema失败产生 typed `model_catalog_unavailable/invalid`，不回落本地旧表。

### 15.2 Exact reasoning selection

- effort choices按 target exact展示；
- provider真实 `minimal/auto/adaptive` 作为 catalog raw value 保留，不建全局 rank；
- provider真实 `null/none` 按 models.dev contract 显示为 disabled；
- `toggle + effort`、`toggle + budget`、`effort + budget` 与三者组合均完整保留；
- effort与budget作为替代 family一次只能选择一个；有 toggle时任一 graded selection均同时
  exact启用 toggle，不能发送 effort+budget或漏掉 required enable；
- effort含 `null/none` 又声明 toggle时产生 typed catalog-invalid、row可见但 target不可执行；
- explicit choice必须 exact membership；
- `{low, high}` target 收到 `medium` 必须拒绝，不能投影到任一档；
- OpenRouter GLM fixture保留 toggle + `high/xhigh`；direct Z.AI fixture只接受 `high/max`；
- 同名 choice不能跨 target复用旧 selection；
- `[low, high, max]` 默认 `high`，`[high, max]` 默认 `max`，五个正向档默认正中；
- disabled choice不参与正向 effort 的中位计算，只有 disabled 时默认 disabled；
- 默认 family优先级 exact为 positive effort > closed budget > toggle > sole disabled effort >
  provider-default；
- closed budget range使用向上取整的数值中点；开放边界原样保留但不渲染数值控件、不从 output
  limit推导，仍可使用同 row的 effort/toggle，只有开放 budget时用 provider-default；
- target切换清除不适用 choice并计算新 target自己的上中位默认，不迁移同名值；
- 同 target preference仍合法时不因 catalog顺序变化而重算；
- toggle的 enabled/disabled均生成各自 exact control；
- fixed-on exact omission；
- unavailable正常执行且不发送 selector；
- provider-default正常执行、不发送 selector且不宣称实际 reasoning state；
- token budget低于/高于 range拒绝；
- budget不自动生成 `high/max` 名称；
- provider open次数在所有 validation failure下均为 0。
- 本 turn selection改变只改变 `ResolvedModelCallFact`，不改变 target fingerprint，也不触发
  `MODEL_TARGET_CHANGED`；control domain/codec改变仍改变 target fingerprint。

### 15.3 Request golden 与 owner

- OpenAI Chat effort exact root field；
- OpenAI Responses effort exact nested field；
- OpenRouter Chat effort exact nested field；
- toggle enabled/disabled shape均 exact；
- fixed-on / no caller control 的 omission exact；
- unavailable/provider-default exact omission；
- effort disabled choice发送 contract声明的 exact value；
- generic defaults/extra body不能覆盖 reasoning-owned key；
- output cap、store、stream、usage与 existing request fields不漂移；
- reasoning control不出现在 context-bearing final-wire projection；
- canonical数据库 transaction内不访问 credential store；prepared call只在 commit后的 provider
  open从其 frozen connection ID borrow一次 credential，不回读 session current value；
- core与 compaction中不存在 route/model名称分支。

### 15.4 Tool-call correlation

- 0起点连续；
- 1起点连续；
- arbitrary non-zero起点；
- sparse `1,3`；
- reused index + distinct non-empty IDs；
- continuation只有 ID；
- continuation只有唯一 index；
- ID/name延迟；
- empty ID；
- 两个 active calls下 ID/index均缺失；
- reused index后无 ID continuation；
- ID 与 index conflict；
- partial JSON暂时合法但后续仍追加；
- terminal前缺 ID/name；
- invalid/root-non-object arguments；
- 任一 ambiguity/error下 assistant adoption与 tool execution均为 0。

### 15.5 Retry、terminal 与 replay

- physical retry只重放 byte-equivalent prepared request；
- semantic event后 transport error不 retry；
- unsupported reasoning choice不发请求、更不降档 retry；
- incomplete/unknown terminal不 retry；
- Chat actual reasoning fields仍 exact replay；
- Responses ordered reasoning/message/function_call仍 exact replay；
- reasoning choice改变不翻译历史 carrier；
- wire API/model/endpoint/request codec/replay contract变化仍走 cold semantic continuation。

### 15.6 Prefix、compaction 与 auxiliary calls

- 同 epoch connection/target冻结；reasoning selection在每个 dispatch/turn 冻结；
  SYSTEM/tools/messages保持 strict prefix；
- 同 target内相邻 NEW_TURN选择不同 reasoning时沿用同一 epoch/prefix，不产生
  `MODEL_TARGET_CHANGED`；每个 request仍发送各自 frozen selection；
- 同 target但不同 connection仍触发 new cold epoch；compatibility exact compare ID而不增加
  connection fingerprint；
- summary、governance、compaction extraction均取得其 exact target 的上中位无 UI 默认，
  不读取或改变 session composer preference；
- summary 与 ordinary call使用同一 universe resolver/adapter lowering；
- final-wire source/successor quote不计 reasoning selector；
- provider usage reported/missing/inconsistent均不改变 compaction decision；
- catalog变化不热改 active epoch。

### 15.7 UI / config-check

- 设置页显示零到多张 saved model-connection cards与“添加配置”，不存在单行全局“当前模型”；
- 添加流程只要求 provider、model ID、API key、Chat/Responses；endpoint按 exact precedence自动
  解析并在确认页展示；
- route selector只投影仍有 selectable model 的 direct/gateway entries；model列表只来自
  selected provider 的 filtered entries；
- context/Claude/Gemini filter的 UI fixture与 kernel projection exact一致；
- Chat/Responses 始终是用户选择，不被 catalog 强制；
- shape match 显示推荐，shape mismatch 只 warning 仍可保存，shape absent 不警告；
- 所有确认页均显示“只支持 OpenAI Chat Completions / Responses compatible endpoint”说明；
- 保存配置不打开 provider；保存成功后 key input清空且 read model只显示
  `credential_present=true`；
- reasoning列表与 exact contract一致；
- effort/budget同时存在时单个推理菜单分组展示两种替代 control，三种合法组合及默认优先级均
  与 kernel read model一致；开放 budget不出现伪数值输入；catalog-invalid row显示精确错误且
  不能确认保存；
- toggle有真实开关，fixed-on/unavailable/provider-default没有伪开关；
- connection config页面不存在 reasoning default控件；
- app在零配置且无 global API-key env时可启动；session可浏览但 send不可执行，并有设置入口；
- composer model selector只列 saved connections；未选择时不暗选第一条；
- 新 session选择 connection后显示 exact target上中位默认；用户修改后跨 turn、刷新、Host重启与控制窗口接管
  保持，直到再次修改；
- session busy或有 pending NEW_TURN时不能切 connection；不会取消/重排工作；
- idle switch同时更新 connection与新 target reasoning默认，下一次 dispatch创建 new cold epoch；
- 两个 session并行选择不同 connections时各自 exact dispatch，互不覆盖；
- stale session choice按新 target contract重置默认并提示，不做 projection；
- send 时冻结，running turn不热改；steer不改变当前 turn；
- 两条排队 prompt之间修改 preference时，每条最终使用各自提交时的值；
- 不支持的 user-selected API 失败后显示原错误与修改配置入口，不自动 fallback；
- headless与 UI config走同一 validation；
- secret不进入 diagnostics、universe或 client projection；
- legacy supports/thinking/raw effort字段不存在。

### 15.8 Session / queue / turn storage

- user connection metadata round-trip、stable random ID、closed codec、ordered output、no-follow、
  0700/0600 与 atomic replace均通过；
- API key只进入 credential-store fake/production port；metadata文件、PostgreSQL、Web response、
  log与 diagnostics逐字扫描均不含 exact secret；
- secure backend不可用 typed失败；metadata publish失败会清理当次 secret；无 plaintext fallback；
- session row只保存 current `model_connection_id` 与一份 `SessionReasoningPreference`，没有
  PostgreSQL settings/profile 子表；
- preference JSON exact携带完整 `ModelTargetKey`，不新增 fingerprint；
- controller connection/preference mutation成功后 snapshot/reconnect读回同一 pair；
- connection/preference mutation与 NEW_TURN admission锁同一 session row；两个 stale composer在
  model-switch-vs-send、preference-change-vs-send竞态下，旧 pair均返回
  `stale_model_selection`与最新 snapshot，且没有 queue/entry/turn写入；
- NEW_TURN queue row保存提交时 exact connection ID + reasoning copy，STEER两列均为 null；
- direct ROOT admission与 queued ROOT consumption都把 exact pair写入 turn；
- queue consumption不回读 sessions当前值；
- command semantic digest包含 connection + selection；同 command ID + 任一不同均为 conflict；
- queue confirmation与 turn confirmation exact compare pair；
- subagent、Plan/terminal continuation继承 origin/parent turn pair，新的 user NEW_TURN读取
  composer当前 pair；
- session connection ID不伪造到 filesystem config的数据库 FK；metadata缺失或 catalog/codec
  permanent invalid使 pending queue复用现有 `REJECTED` settlement且不创建 turn；暂时没有 catalog
  snapshot保持 `PENDING`；vault/key缺失只在 commit后 provider open终止已接纳 turn；各路径均不
  double-borrow；
- reasoning preference change不产生 transcript entry或新 prefix root；connection switch只走
  approved new cold epoch；两者都不新增 agent event/job/checkpoint；
- clean-v0 schema与 expected catalog更新，event/subject/guard/relation/job oracle计数不增加。

### 15.9 Connection 与 PRO/FLASH hard cut

- 不存在 global `LLMConfig.model`、`pro`、`flash`、`model_for(role)` 或 `slot_for(role)`；
- `ModelRole` 类型、`ModelProfile.role` 与 `ResolvedModelTargetFact.model_role` 均不存在；
- production不读取 `PULSARA_API_KEY` / `PULSARA_MODEL` / `PULSARA_PRO_*` /
  `PULSARA_FLASH_*`，也不做 import、alias或 fallback；
- CLI `--model-role` 已删除；
- 同一 session/turn 的 main、subagent、summary、compaction 与 memory governance resolve 出同一
  `ModelConnectionId` + `ModelTargetKey`；不同 sessions可不同；
- auxiliary 局部 output cap不修改 base model config，也不创建第二个 model slot；
- auxiliary purpose/deadline/input/output bounds仍各自生效；
- bootstrap只返回 non-secret connection collection；设置页不存在“主要模型/轻量模型”；
- tests/support builders注入 connection/credential stores，不用相同值伪造双 slot；
- process credential boundary取得 caller-borrowed exact secret，不回读 global environment；
- production 与 active docs 中不存在 `ModelRole.FLASH`、`PULSARA_FLASH_*`、
  `flash_model` 或 display-only compatibility alias。

---

## 16. 验证命令与 real-provider dogfood

实施完成后至少运行：

```bash
uv run pytest -q tests/test_llm_model_catalog.py tests/test_llm_model_target.py
uv run pytest -q tests/test_llm_model_connections.py tests/test_llm_model_credentials.py
uv run pytest -q tests/test_settings.py
uv run pytest -q tests/test_round5a1_provider_output_termination.py
uv run pytest -q tests/test_round5a2_durable_provider_replay.py
uv run pytest -q tests/test_round3_1_provider_input_prefix_continuity.py -k 'wire or prefix or replay'
uv run pytest -q tests/test_round5b_long_horizon_context_compaction.py -k 'wire or target or usage'
uv run pytest -q tests/test_round5a2_durable_provider_replay_postgres.py
uv run pytest -q
```

设置 UI 与 composer selector 属于本轮 required scope：

```bash
npm --prefix frontend test -- --run
npm --prefix frontend run lint
npm --prefix frontend run build:local
```

Real-provider dogfood 至少覆盖：

1. 直接读取当前 models.dev `api.json`，证明当前 dogfood target通过 256k/name filter，并由设置页
   exact加入一组 connection；
2. 一个 direct Responses target：exact reasoning choice、正文、tool call、terminal、下一轮 replay；
3. 一个 direct Chat target：exact displayed choice、tool call、下一轮 replay；
4. 一个 gateway target（优先 OpenRouter）：models.dev choice 与 outbound control exact一致；
5. 同一 model family经 direct/gateway暴露不同 choices时，两条请求各自只发送所选真实值；
6. 至少覆盖 fixed-on/unavailable/provider-default/toggle中的一个 no-effort-list target；无
   credential时精确报告该 target环境阻塞；
7. provider返回 usage/reasoning telemetry不改变 selection；
8. 同一 session connection下至少实际触发一个 main call 与一个 auxiliary call，证实二者使用
   同一 route/wire/model target，而各自 output bound/deadline仍生效；
9. 若可用 credential允许，创建 direct/gateway两组 connections并让两个 sessions各发送一轮，
   证明配置不会串线；缺少第二凭据时精确报告环境阻塞；
10. 测试 harness即使从 `PULSARA_API_KEY` 取得 dogfood secret，也只注入 in-memory
    `ModelCredentialStore`，全程不输出或持久化其值。

每个 route/wire codec 在宣称 production-supported 前，必须至少有 request golden、recorded
stream fixture/normalized contract 和一次代表 target live smoke。models.dev 新增 model 不要求
Pulsara 为每条 row 重跑 conformance；用户所选 target 的实际 protocol 失败按 §6.2 处理。外部
credential不可用时可以完成全部本地验证并报告阻塞，不能声称对应 codec已通过
live conformance。

---

## 17. 明确禁止的伪修复

1. 建一套 Pulsara 全局 `low/medium/high/xhigh/max`，再向 target 投影；
2. target切换后按同名或最近档迁移 reasoning choice；
3. provider 400 后逐档重发完整 turn；
4. semantic output后换 model/route/effort；
5. 把 HTTP 200、model ID存在或未知字段未报错当作 support证据；
6. 从 usage token数反推实际 reasoning level；
7. 把 unknown伪装成 supported、unsupported、fixed-on或 disabled；
8. 在 runtime用 model substring、正则、family或 release date生成 reasoning choices；§4.2 的
   closed selectable-product predicate 是唯一 model-ID leaf filter，不得扩张成 support heuristic；
9. 把 budget range擅自命名成 global high/max；
10. `index - 1`、provider-name index offset或 dense array indexing；
11. 多个 active tool calls时用 latest盲猜；
12. arguments中途可解析就提前执行工具；
13. 把 provider-specific reasoning/replay block转成通用文本；
14. 在 Agent core、capability、memory、tool executor或 compaction中新增 route/model名称分支；
15. 让 arbitrary JSON catalog拥有 request field path；
16. 让 generic extra body与 adapter同时拥有 reasoning字段；
17. 同时保留 raw string/bool与 typed universe双路径；
18. 为 universe/discovery新增数据库 catalog；为 model connection/reasoning preference新增
    PostgreSQL settings table、event、job、receipt、revision registry或后台 authority；
19. 用 provider `/models` 或 models.dev refresh 热改 active epoch；
20. 把 gateway当作 replay/retry安全性的无条件证明；
21. 为健康 provider stream、turn、task或 Host增加 total lifetime cap；
22. 改写 canonical transcript、provider-input prefix或 durable replay来简化适配；
23. 把 models.dev `provider.shape` 当成禁止用户选择另一 wire API 的硬门禁；
24. `provider.shape` 缺失时用 `npm`、endpoint 字符串或 model family 猜一个 shape。
25. 删除 FLASH 后保留只有 PRO 的单值 `ModelRole`、`pro` slot或 `pro_model` alias；
26. 把 auxiliary purpose重新命名成另一种隐形 model role，或为 summary/governance悄悄
    解析第二个 target；
27. 为兼容旧环境变量继续读取 `PULSARA_API_KEY`、`PULSARA_MODEL`、`PULSARA_PRO_*` /
    `PULSARA_FLASH_*` 作为 production model config；
28. 前端再次 parse models.dev 并自行实现 256k、Claude/Gemini 或 shape filter；
29. 用 provider name、family、display name过滤 Claude/Gemini，或只检查完整 raw ID开头而漏掉
    `google/gemini-*` / `anthropic/claude-*`；
30. 把 API key写入 metadata文件、PostgreSQL、localStorage、日志、response，或提供 reveal API；
31. 将 saved connection列表第一项静默当作所有新会话的全局默认；
32. running/pending工作存在时热换 connection、取消旧队列或在同一 epoch 改 endpoint/model/API；
33. 配置 ID 原地改成另一个 target，或让 auxiliary call选择另一配置；
34. 为实现 add/list/select 顺手增加未定义语义的 edit/delete/rotate UI。

---

## 18. Definition of Done

只有以下条件全部成立，本文才可标记 ACTIVATED：

1. `capability` 继续只指 Tool/MCP/Skill/Plugin 等 Agent 能力；LLM 支持元数据统一命名为
   target/contract/universe；
2. Core继续只消费 existing normalized IR；
3. Production 直接读 models.dev，不存在 checked-in per-model catalog 副本；
4. OpenRouter等 gateway与 direct provider均是一等 route；
5. exact execution identity 为 models.dev `route + model` 与用户选择 `wire API` 的 join；
6. 用户可以保存多组 model connections，不同 sessions可并发使用不同配置；每个 session/turn
   与其 main/auxiliary calls只共享一条 frozen connection/target；
7. `ModelRole`、PRO/FLASH slots、global model/API-key env、CLI/fact/Web旧投影与 compatibility
   aliases全部不存在；
8. Raw catalog经过唯一 kernel predicate，只向产品暴露 `limit.context >= 256000` 且 model ID
   leaf不以 Claude/Gemini 开头的 rows；前端不重复过滤；
9. 设置页以 cards/add flow管理配置，普通流程只需 provider、model ID、API key、Chat/Responses；
   会话 composer从已保存配置中选择一条；
10. API key只持久化在 OS credential vault；固定 service + connection ID 是唯一 lookup key；
    metadata、PostgreSQL与 client projections只含 connection ID/presence，不提供 read-back；零配置
    app仍可启动；
11. UI/CLI只展示 exact catalog entry 的真实 reasoning controls，完整保留组合
   toggle/effort/budget；effort/budget是替代 selection family，catalog-invalid row可见但不可执行；
12. `provider.shape` 只产生推荐/不匹配 warning，不强制 API；缺失时保持中性；确认页始终说明
    只支持 OpenAI Chat Completions / Responses compatible endpoint；
13. Reasoning唯一入口是会话输入框；选择 connection时按其 exact target真实选项取上中位默认，
    用户更改后保持到再次修改，每条 NEW_TURN admission冻结 connection + selection；
14. Connection只能在 idle/no-pending boundary切换，并在下一次 dispatch建立 new cold epoch；
15. 不存在 global five-rung projection、nearest/tie-up或跨 target choice迁移；
16. 每个 provider call在 open前已解析为 effort、toggle、budget、fixed-on、unavailable或
    provider-default之一；
17. `supports_reasoning` bool、raw passthrough、把 unproved omission解释成具体 reasoning状态，
    以及 reasoning extra-body旧路径全部删除；
18. Model contract只投影当前执行需要的 models.dev 最小字段；
19. Route/wire adapter唯一拥有 exact request、stream、terminal与 replay shape；
20. Chat tool-call parser支持 arbitrary non-negative/sparse/reused/missing index，并在歧义时
    fail closed；
21. semantic-output retry barrier与 exact prepared-request retry保持；
22. Chat/Responses terminal、reasoning replay、atomic assistant acceptance没有回归；
23. prefix continuity与 final-wire estimation契约没有回归；
24. reasoning selection只属于 call，不进入 target fingerprint；同 target改 selection不重建
    epoch，而 target control domain/codec变化仍不兼容；
25. connection只存 ID/target/endpoint，output/safety policy由 hard limits确定性派生；四字段添加
    流程没有隐藏的第五/第六项；
26. NEW_TURN admission与 session mutation exact串行化并拒绝 stale composer；数据库 transaction
    内不访问 filesystem/vault，credential只在 commit后的 provider open取得一次；
27. catalog client没有新增 durable cache authority；PostgreSQL只在 sessions/queue/turn增加最小
    connection/reasoning字段，event/subject/guard/relation/job oracle类别无变化；
28. catalog-backed/custom target走单一 typed validation/resolution路径；
29. focused、full、PostgreSQL与可用 real-provider dogfood通过；
30. 每个 production-supported route/wire codec 都有 official evidence、golden/fixture 与代表 live
    smoke；
31. 外部阻塞逐 target精确报告，API key从未输出或落入非 credential-vault persistence；
32. production、tests、README与 config-check只描述新路径，无 compatibility alias、feature flag
    或双读写。

---

## 19. 调研证据如何影响本文

本文吸收的是共同架构结论，而不是复制任一框架：

- OpenCode `dev` 证明 exact model variants按 provider/route/model存在，selector可以展示真实
  choices；它的 `reasoning_options` 优先路径值得借鉴，heuristic `variants()` 不值得复制；
- 同一 GLM fixture经 OpenRouter与 direct Z.AI暴露不同 choices，直接否定 model-only profile与
  global projection；
- Vercel AI SDK 的 streamed tool-call tracker证明 non-zero/sparse/reused/missing index应作为
  generic tolerant normalization，而不是 provider offset hack；
- Pydantic AI 的 model profile / parts manager证明 model support facts与 vendor part identity应
  分层；
- LiteLLM与 Bifrost证明 projection只能消费已知 supported set，不能发现它；本文进一步依据
  Pulsara“先选 exact target”的产品流程删除 projection本身；
- models.dev 公开 API 已直接提供 route-scoped model facts；本文直读它，不再复制一份
  checked-in 子集，也不复制 OpenCode 的磁盘 cache/refresh machinery；
- models.dev optional `model.provider.shape` 证明 catalog 可以提供 wire hint；由于它是 optional
  且不是 supported-set 协议，本文只用它做推荐/警告；
- OpenAI Agents Python证明 semantic event一旦发出，transparent replay必须被否决；
- OpenRouter、Bedrock Converse与 models.dev共同证明不存在跨 provider的完整 support
  negotiation标准。

主要代码锚点：

```text
/Users/plumliu/Desktop/python_workspace/opencode/packages/core/src/models-dev.ts
/Users/plumliu/Desktop/python_workspace/opencode/packages/opencode/src/provider/provider.ts
/Users/plumliu/Desktop/python_workspace/opencode/packages/opencode/src/provider/transform.ts
/Users/plumliu/Desktop/python_workspace/opencode/packages/opencode/src/cli/cmd/run/variant.shared.ts
/Users/plumliu/Desktop/python_workspace/opencode/packages/llm/src/providers/openrouter.ts
/Users/plumliu/Desktop/python_workspace/opencode/packages/opencode/test/tool/fixtures/models-api.json
```

外部一手契约：

- models.dev API：<https://models.dev/api.json>
- models.dev README：<https://github.com/anomalyco/models.dev/blob/dev/README.md>
- models.dev contributor contract（Reasoning options）：
  <https://github.com/anomalyco/models.dev/blob/dev/AGENTS.md#reasoning-options>
- models.dev reasoning-options audit contract：
  <https://github.com/anomalyco/models.dev/blob/dev/.opencode/skills/audit-reasoning-options/SKILL.md>
- models.dev schema：<https://github.com/anomalyco/models.dev/blob/dev/packages/core/src/schema.ts>
- Zhipu GLM-5.3：<https://docs.z.ai/guides/llm/glm-5.3>
- Zhipu Chat Completion：<https://docs.z.ai/api-reference/llm/chat-completion>

models.dev 是 route/model catalog facts 的直接上游；Pulsara 的 wire correctness authority 始终是本文、
既有 active spec、production typed contracts、tests 与 exact real-provider evidence。
