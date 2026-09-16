# Pulsara Route + Wire API + Model Universe、GUI Local Configuration 与 Wire Adapter Hard-cut 实施规范

> 状态：**ACTIVATED — 2026-09-04**
>
> 冻结日期：2026-09-04
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
- `.env` 如何从 production configuration 中完全退役；模型、固定 DashScope
  embedding/reranker credential 与本机 PostgreSQL 均由图形界面配置；
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
输入、已开始 turn”增加最小的 model connection 与 exact reasoning selection 字段；模型连接与
本机 PostgreSQL、模型连接及其 API key、两枚固定 DashScope API key 使用一份
database-independent closed 本地配置；不增加系统 credential vault、PostgreSQL 配置表、event、
job、fingerprint registry 或 arbitrary key/value settings system。发生冲突时，优先级为：
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
- `.env`、`--env-file`、`--override-env`、production `from_env()` 与
  `PULSARA_{POSTGRES,EMBEDDING,RERANK}_*` product-configuration 路径；
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

Pulsara 的用户级设置可以保存多组 `ModelConnectionConfig`，每组都冻结一套 exact target、
endpoint 及其 API key。固定 DashScope embedding 与 reranker 各有一枚独立 API key；本机
PostgreSQL runtime/admin DSN、model-connection metadata 与全部 API key 共用一份权限受限的
closed 本地配置文件。每个会话只选择其中一组模型连接；主
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

本机 HTTP/设置壳不依赖 PostgreSQL，因而全新安装或数据库不可连接时仍可打开设置页。只有
会话/任务数据面需要已配置且通过验证的 runtime DSN；admin DSN 仅供用户显式发起的数据库
初始化/升级使用。缺少两枚可选 DashScope key 只让对应 advisory retrieval channel
退化，不得阻止 app、会话或主模型工作。

直接读 catalog 只发生在设置/配置校验或新 cold Host 解析边界；不得在单次 provider request
的路径上读网络，也不得热替换 active epoch 已冻结的 target。首个 hard cut 只在现有
`sessions`、`prompt_queue_items` 与 `turns` 上各增加一个必要的 `model_call_binding` 字段；
不新增 PostgreSQL 表、event、job、checkpoint、receipt、在线逐档 probe、
本地 model catalog 副本或第二套 normalized IR。用户保存的 API key 只进入
`${PULSARA_HOME}/local-settings.yaml`，永远不进入这些表、Web response、日志或浏览器持久化。

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
| `ModelConnectionConfig` | 运行时使用的 non-secret target/endpoint metadata；同一 local-settings record 另带 API key，但 API key 不进入该 DTO、reasoning 或派生 execution policy |
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

通用 `LLMMessage` 不承载 assistant thinking，也不把 canonical/public assistant 语义重新编码为
`reasoning_content`。Chat 的 `reasoning_content`、`reasoning`、`reasoning_details` 与 Responses
output items 只来自 adapter 对实际 provider 输出的 closed-shape 收集，并仅通过现有
target-compatible durable native replay 回放；process-local thinking event 只负责实时展示。

### 3.2 当前 target 与 reasoning request 的错误抽象

当前代码近似为：

```text
ProviderProfile.supports_reasoning: bool
ModelProfile.supports_reasoning: bool
LLMOptions.reasoning_effort: str | None
```

Chat 将 raw string 写入 `reasoning_effort`，Responses 将其写入
`reasoning={"effort": ..., "summary": "auto"}`，使显式启用 reasoning 的 turn 同时请求
provider 可公开展示的推理摘要；未指定时不发送。request owner 唯一来自 resolved target 的
reasoning control；provider output thinking 不进入 generic request DTO。

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

### 3.4 当前 `.env` 与 Web bootstrap 的错误边界

当前 `settings.py` 由 `PulsaraSettings.from_env()` / `from_env_file()` 同时装配主模型、retrieval与
PostgreSQL；CLI普遍暴露 `--env-file`，`LocalWebApplication.start()` 又在发布 HTTP listener前调用
`sessions.prepare()`。结果是缺少/写错任何关键环境变量或 PostgreSQL不可达时，用户无法进入唯一
可以修复配置的图形界面。

Retrieval真源同时表明 embedding与 reranker不是开放 provider universe：前者固定为 DashScope
compatible `text-embedding-v4` / 1024 dimensions，后者固定为 DashScope `qwen3-rerank`。把它们的
provider、model、endpoint与 tuning继续做成 production env变量既制造第二 owner，也会让 UI 暗示
Pulsara支持尚不存在的替代 backend。本轮只把两枚独立 API key交给用户管理，保持既有 fixed
semantic contracts。

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
`SelectableModelCatalog`、用户配置与本地 wire-dialect adapter 之上的 exact 投影：

```text
resolve_target(snapshot, route_id, model_id, wire_api)
    = exact eligible catalog entry
    + catalog-declared wire dialect
    + user-selected generic Chat/Responses adapter
```

models.dev 不声称某 model 必然同时支持 Chat 和 Responses；Pulsara 也不作这种推断。
`wire_api` 是用户对实际 endpoint contract 的显式配置。models.dev route 若以
`@ai-sdk/openai-compatible`（以及 Pulsara 已知等价的 OpenAI/OpenRouter package）声明
OpenAI-compatible dialect，Chat 与 Responses 均接入 Pulsara 已有通用 adapter；`shape` 只提供
推荐。若调用端点实际不支持用户选择的 API，则保留具体 provider/protocol error，不自动换 API。
catalog dialect 为 provider-native/unknown 时在 provider open 前拒绝。要支持新的 native wire，必须
显式增加并测试一个 wire dialect adapter；resolver 不按 route、model 名、family 或 HTTP 试错猜 dialect。

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
  `npm` 仍必须独立解析为 wire-dialect hint，`provider.shape` 仍必须独立解析为 API 推荐；
- 其他模型即使来自 Anthropic、Google、Vertex 或 gateway，也不因 provider 名被排除；
- 未来若 Pulsara 正式支持这些模型族，必须修改这一条产品 predicate 与 golden tests，不能
  在 adapter 中暗开例外。

这里的“唯一过滤”只指产品把可解析 row 从目录选择面排除的 predicate。通过该 predicate 的 row
不会再因为 reasoning option、adapter或 endpoint validation而被静默删掉：局部 reasoning
metadata异常按 §4.6降级；缺少可用 wire-dialect adapter、endpoint或 hard limits时，kernel在同一
read model返回 typed non-executable reason，UI保留 row并禁止确认该 wire组合。这样 catalog
drift可见但不被过度放大，也不会让根本无法发送的 target获得 execution authority。

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
- canonical endpoint binding继续进入 existing resolved target fact；不新增 endpoint/profile
  fingerprint registry。

### 4.4 RouteWireContract

```python
@dataclass(frozen=True, slots=True)
class RouteWireContract:
    transport_binding_id: str
    transport_contract_version: str
    model_identity_policy: ModelIdentityPolicy
    assistant_replay_contract: ProviderAssistantReplayContract
```

现有 registry 只以 `(wire_dialect, wire_api)` 查找通用 contract；request、stream、terminal与 replay
均不按 route或 model分支。Route不是allowlist：一个新的
`@ai-sdk/openai-compatible` models.dev route无需Pulsara发版或手工注册即可使用现有 Chat /
Responses transport。Contract回答：

- 请求发往哪类 route；
- 采用哪个 wire API 与 transport；
- request/stream/terminal/replay 怎样解释；
- adapter 能把哪些 typed reasoning selection lower 成该 route/wire 的 exact request。

API key 不进入 contract。Route 是直连 provider 还是 gateway 可作为 UI 描述，但不得驱动
Agent core 的不同正确性规则。OpenRouter 和 OpenAI 都是可被用户选择的 route。

`RouteWireContract` 是 Pulsara 需要自己维护的小而明确的 **wire dialect** 部分，不是 provider
目录。reasoning lowering 是该 contract 所绑定 adapter 的普通 typed 方法，不再另造 `request_codec_id`、
`reasoning_codec_ids` 或 codec registry；`transport_binding_id + transport_contract_version`
已经是现有运行时选择和复现 adapter 的边界。models.dev 的 `npm` 可以把 route解析到通用
OpenAI-compatible dialect；Pulsara 的通用 Chat/Responses adapters拥有 request主干、SSE、terminal、
tool correlation与closed observed-field replay，也分别统一拥有 Chat 的 `reasoning_effort` 与
Responses 的 `reasoning.effort + reasoning.summary=auto` request lowering。Pulsara不为 provider或
model复制这些 contract。
OpenAI route在models.dev缺少 `api` 时所需的默认 endpoint属于独立的 route endpoint fallback，
不得伪装成另一份 wire contract。

### 4.5 ModelTargetContract 的最小字段

```python
@dataclass(frozen=True, slots=True)
class ModelTargetContract:
    key: ModelTargetKey
    target_facts: ModelTargetFacts
    reasoning: ReasoningControlContract
    route_wire: RouteWireContract
```

```python
@dataclass(frozen=True, slots=True)
class ModelTargetFacts:
    display_name: str
    route_name: str
    tool_call: bool | None
    limits: ModelHardLimits
    wire_shape_hint: Literal["responses", "completions"] | None
```

Identity只存在于 `ModelCatalogEntryKey` / `ModelTargetKey`；display、limits与 tool-call
只存在于 `ModelTargetFacts`。目录连接从 models.dev row 构造该值，自定义连接从 closed
`UserDeclaredModelTarget` 构造同一个值；`ModelTargetContract` 不把这些 child fields再复制成 sibling字段，
也不增加 validator要求两份永远相等。Raw `reasoning/reasoning_options`只在 snapshot parser中
消费一次，解析后的 `reasoning` 是 target唯一 execution/UI事实，不把 raw与 normalized副本一起
塞进 target；`route_wire` 是本地 adapter binding。

首轮从 models.dev entry 直接投影的 execution facts 只有：

1. exact target identity；
2. context/input/output hard limits；
3. reasoning behavior/control 的真实形状、选项和默认；
4. `tool_call` 的 bool/unknown事实。
5. provider `npm` 解析出的 `openai_compatible | provider_native | unknown` wire dialect。

Pulsara 不再为这些字段维护一份 built-in per-model override table。若上游 entry 缺少
非预算性字段，按 §4.6 的 typed unknown/provider-default 语义局部降级；不用名称 heuristic
补齐，也不因一个无关 catalog row 异常关闭整份目录。

models.dev 当前不提供 exact `tool_choice` 子集、reasoning + tools 互操作矩阵、terminal
状态机或 replay contract。Pulsara 不得伪造这些 per-model facts：

- tool request/choice 形状归 `RouteWireContract`；
- `model.tool_call=false` 时，含 tools 的 call 在 open 前拒绝；
- `model.tool_call=true` 只表示 models.dev 声称该 route/model 可 tool-call，不额外
  推导未公开的 `tool_choice` 枚举；
- `model.tool_call` 缺失时保持 unknown：无 tools 的 call正常执行；含 tools 的真实用户 call可按
  已选 route/wire adapter发送并附带“目录未确认工具支持”的诊断，但失败后不自动去掉 tools重试。
  不能把 unknown伪装成 `true/false`，也不能把整条模型从目录隐藏；
- reasoning + tools 如果某 `route_id + wire_api` 有已知禁止条件，由该 route/wire
  adapter 在 open 前拒绝；否则不另加一个没有数据来源的强门禁。

以下字段不因“以后可能有用”加入首轮 runtime contract：

- pricing；
- benchmark/ranking；
- marketing description；
- modalities；
- structured output；
- image/audio generation；
- `interleaved` hint（Chat/Responses reasoning carrier、stream与 replay已有 adapter authority）；
- provider token estimator；
- health score 或实时 availability；
- 任意开放式 feature predicate map。

当产品真的使用某项能力时，再用一个明确产品需求扩展 exact contract。不能预先复制
models.dev 的全部 schema。

### 4.6 Catalog 缺失值的唯一语义

- `reasoning=false` -> `ReasoningUnavailable`；
- `reasoning=true` 且 `reasoning_options=[]` -> `ReasoningFixedOn`，按 models.dev 当前的
  明确编辑契约表示“有 reasoning，但没有 caller control”；
- `reasoning=true` 且 `reasoning_options` 含一个或多个可理解控制形状 -> 完整保留所有
  `effort` / `toggle` / `budget_tokens` option；
- `reasoning=true` 但 `reasoning_options` 缺失 -> `ReasoningProviderDefault`，不用模型名
  或同 family 补齐；
- `tool_call` 正常情况下直接使用 models.dev boolean；字段缺失或该单值非法时只把该 row 的
  tool-call support 记为 unknown，不把外部 advisory catalog 的局部质量问题升级成全目录失败；
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

Catalog parser 只在 response 不是可解析 JSON object、顶层结构无法枚举 provider/model，或
本次请求本身失败时令整个 snapshot unavailable。单个 provider/model 的 redundant ID 不一致、
未知 optional 字段、未知 reasoning option kind 或局部非法 option 只产生 row/option diagnostic：

- identity 可由 outer provider/model key 确定时，以 outer key 作为 models.dev canonical key，
  inner `id` 只作一致性诊断，不再让一条坏 row 拖垮其余数千条模型；
- 未知 option kind 原样留在诊断中但不进入可选 control；已知且合法的 sibling controls继续可用；
- 某一 control family 自相矛盾时只放弃该 family；若没有任何可安全 lower 的 selector，则降级为
  `ReasoningProviderDefault` omission；
- 只有缺少 `limit.context` / `limit.output`、值不合法或无法应用 256k predicate/本地 input budget
  的 row 自身不能成为 executable catalog-backed target。

这些是外部 advisory catalog 的局部降级，不是第二套产品 filter。Kernel read model可以附带
非阻塞诊断；前端无需为每条坏 metadata制造不可操作的错误卡片。

### 4.7 Limits 与本地 execution policy 的所有权

models.dev catalog entry 拥有目录 target 的 hard limits；自定义连接则由用户在创建时明确声明同一
组 target facts：

```text
total_context_tokens
max_input_tokens（provider 明确区分时）
max_output_tokens
```

目录连接不复制这些字段；自定义连接只保存用户声明的 context/output hard limits，不开放任意
execution policy。Pulsara 从两种来源解析出的同一 hard-limit value object
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

两个 `8_192` 复用当前 production `DEFAULT_MODEL_CONTEXT_LIMITS` 的既有 per-call output target与
input safety margin，不是新引入的 turn/task/Host lifetime cap。若实现审计发现当前 canonical
owner已有同名常量，直接复用，不再复制第二份数字常量。

解析后仍必须证明 output、pre-margin input 与最终 input budget 全部为正；否则 target typed
non-executable。Auxiliary purpose 的既有局部 output cap 只对该 call 取更小 output 并重算同一
budget，不修改 connection 或 hard limits。Catalog-backed target不允许 env任意重写 hard
limits/policy。

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

其中 `toggle` 表示独立 on/off control，`effort` 与 `budget_tokens` 表示该 target 公开的
两种 graded control surface。它们是**同一 request 的替代选择族**，不是要求 Pulsara 同时发送
多个 control。Pulsara 完整保留同 row 中的合法 family 供用户选择，但一次只 lower 用户选中的
一种 control，不把 toggle、effort 与 budget 擅自拼成新协议。

```python
ReasoningControlContract = (
    ReasoningSelectableControls
    | ReasoningFixedOn
    | ReasoningUnavailable
    | ReasoningProviderDefault
)

@dataclass(frozen=True, slots=True)
class ReasoningSelectableControls:
    effort: ReasoningEffortChoices | None
    toggle: ReasoningToggle | None
    budget: ReasoningTokenBudgetRange | None
```

三种 option 的存在与值从 models.dev entry 原样投影；exact lowering直接调用最终解析出的
`RouteWireContract`，不再把同一 adapter拆成可任意拼接的 codec ID。`npm` 只将已明确声明
`@ai-sdk/openai-compatible` 的 provider映射到 Pulsara通用 OpenAI-compatible dialect，不从任意
package名猜 request path或字段形状。

#### `ReasoningEffortChoices`

```python
@dataclass(frozen=True, slots=True)
class ReasoningEffortChoices:
    values: tuple[str | None, ...]
```

`values` 与 models.dev 的 `values` exact 对应。根据其公开编辑 contract，`null` 或 exact
`none` 表示关闭；其他字符串是该 route 公开的原始 control value。Pulsara 不再为
`minimal/low/medium/high/xhigh/max/auto/adaptive` 建全局枚举或 rank。用户可看到
本地化 label，高级信息仍显示 catalog raw value。

Kernel不为每个值再创建 `choice_id` 或持久化 `display_label`。前端 read-model projection可为
React key生成一次性 typed key，并本地化显示，但 command/DB/adapter始终携带 catalog raw value；
`ReasoningEffortSelection(value=None)` 与 binding-level `reasoning=None` 由外层 closed union明确
区分。高级信息中应能看到原始 value，便于用户理解实际发送的控制。

Choice 顺序只服务 UI，不建立跨 target 的全局 rank。即使两个 target 都有 `high`，也不能
据此认为它们具有可比较的计算量、token budget 或效果。

#### `ReasoningToggle`

```python
@dataclass(frozen=True, slots=True)
class ReasoningToggle:
    pass
```

UI 显示真实开关。首轮通用 OpenAI-compatible Chat contract 将 models.dev 的 `toggle`
统一 lower 为最终 HTTP JSON root `reasoning={"enabled": false|true}`；实现通过 OpenAI SDK 的
`extra_body` 参数携带该扩展，SDK 合并后 wire 上不存在 `extra_body` 包装。这是 Chat wire shape，
不是 provider/model 分支。Responses contract 暂不声明 toggle 支持。采用其他私有 toggle 形状的端点
不在该通用 lowering 内，Pulsara 不按 provider 名称改写，也不发送 `adaptive` 等厂商值。

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
    ReasoningToggleSelection
    | ReasoningEffortSelection
    | ReasoningBudgetSelection
)
```

- `None` 只用于 exact contract没有 caller selector，或唯一 option是不能形成安全数值选择的开放
  budget；它不是菜单中额外伪造的“第零档”，target contract仍区分 fixed-on、unavailable与
  provider-default产品文案；
- `ReasoningToggleSelection(enabled=False|True)` 只在存在 toggle 时可用，adapter 分别发送 exact
  toggle-off / toggle-on；
- `ReasoningEffortSelection` exact携带 catalog raw value，包括 `null/none` 这种 effort-owned
  disabled value；它不被改写成 toggle；
- `ReasoningBudgetSelection` 只携带 closed range 内的 exact整数；
- `toggle + effort` 或 `toggle + budget` 下，选择 toggle 时只发送该开关，选择 graded control时
  只发送其 exact effort或budget；toggle-on让 provider在已开启状态内决定 graded behavior；
- `effort + budget` 下菜单同时呈现“档位”和“Token 预算”两组，用户一次只能选择一组；adapter
  发送被选 control并 exact omission另一组；三种 option共存时再增加 toggle off/on两项，
  每次仍只发送一种 selected control；
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

- 已知 option type 为 models.dev schema 的 `effort/toggle/budget_tokens`；未来新增 kind只形成
  option diagnostic，不令整个 snapshot或 target失败；
- 每种已知 option family正常应至多一个，effort choices non-empty 且 exact value不重复；若某
  family不满足，只放弃该 family并保留合法 siblings；
- effort 含 `null/none` 且又声明 toggle并不构成 target-invalid。两者是同一 target公开的两种
  exact关闭入口，菜单可分别标注“关闭（档位）”与“关闭（开关）”；每次仍只选择并 lower其中
  一个，不能因此猜测二者 wire bytes相同；
- budget 的 min/max 同时存在时次序合法；
- `effort`、`toggle`、closed/open `budget` 及其全部合法组合都能被 typed parser表示；每个可执行
  selection kind必须被已解析的通用 dialect contract明确支持；adapter暂未实现的 family不进入可选
  菜单，但不阻止同 target通过其他已实现 family或 provider-default omission正常执行；
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
    > no-control omission
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
  `None`；
- `toggle + effort` 的默认是正向 effort中位值；`toggle + budget` 在 closed range下默认数值中点；
  `effort + budget` 或三者组合按同一优先级默认 effort，但菜单仍保留 toggle与closed budget
  替代项；adapter只发送当前 selected control；
- fixed-on、unavailable 与 provider-default 没有可选档位，不渲染 selector，也不保存伪选择。

这里的“向上取”只用于**同一 target 已公开选择列表里的默认下标或数值中点**，不是
跨 target projection。Pulsara 不把 `low/high/max` 建成全局可比 rank，也不把一个 target
的选择换算到另一个 target。

默认值一旦写入会话偏好，就与用户后来显式选择一样保持不变；catalog 列表顺序变化不会
重新挑选，只要原值仍属于 exact target contract。

### 5.4 Choice 通过 immutable connection 保持 target-local

`ModelConnectionConfig` 创建后不能原地换 target，因此 connection ID 已唯一决定 exact
`ModelTargetKey`。会话、queue 与 turn共用一个最小 frozen value：

```python
@dataclass(frozen=True, slots=True)
class ModelCallBinding:
    connection_id: ModelConnectionId
    reasoning: ReasoningSelection | None
```

`reasoning=None` 只表示该 target当前没有本地可执行 caller control，需要
fixed-on/unavailable/provider-default/open-budget omission。Binding不再重复保存
`ModelTargetKey`：consumer通过 immutable connection取得 target，
再验证 selection membership。把 connection、target与 selection三份相同关联同时持久化不会增加
correctness，反而会制造 drift和额外 comparison gate。

同一 target 内，当前选择在会话中保持锁定，跨 turn、页面刷新、应用重启和控制窗口接管
继续使用，直到用户在该会话的输入框中再次修改。切换 route、wire API 或 model 后：

- 旧 selection 不参与新 target，即使 raw value 同名也不复用；
- 新 target 按 §5.3 计算自己的上中位默认并保存；
- 不做 nearest、向上/向下 projection、alias 或字符串同名迁移；
- catalog 更新后，若 exact target 与原 choice 仍存在，继续锁定原值；若 choice 已消失，
  controller 在下一次 session snapshot读取、binding修改或 `NEW_TURN` admission时，于既有 session
  row lock内将其视为不可执行的 stale preference，按新 contract重新计算默认并保存；若由 admission
  触发，则同一 transaction冻结修订后的 binding。发生这次 reconciliation 的响应携带
  “可用推理选项已更新”提示，而不是静默把旧值投影到近似档位。该提示只是本次响应的产品文案，
  不新增“已经提示过”的 durable bit、event 或 revision。

### 5.5 Runtime truth 只保留两层

旧文档的 `requested/effective/wire` 三层在此产品流程中是不必要的。Runtime 只需：

```text
selected_reasoning
    用户或 exact omission 确定的 target-local state/control

wire_reasoning_control
    route/wire adapter 对该 control 的 exact serialization
```

两者之间是 adapter lowering，不是档位投影。Provider 若明确报告 actual/effective reasoning，
它只作为 telemetry；reasoning token usage 不能反推实际档位，也不能改变 routing、retry、
compaction 或 tool execution decision。

### 5.6 不新增 resolved wrapper；只保留唯一既有 compatibility digest

Resolver对 `ModelCallBinding.reasoning` 完成 target membership与 adapter-support校验后，仍传递
同一个 closed `ReasoningSelection | None`；不新增 `ResolvedReasoningControl`、
`ResolvedEffortChoice` 等只改名的 wrapper。

`ModelTargetContract` 是同进程携带的完整 frozen typed value，不为它新增 fingerprint字段。
现有 `ResolvedModelTargetFact.target_fingerprint` 只因为已经被 provider-input continuity、
frozen model context与 compaction binding独立消费而保留为**唯一 target compatibility digest**；
durable provider replay继续使用自己现有的 replay-target compatibility digest，二者不能再互相
包裹生成第三个 aggregate。Target digest不得退化成“把整个 DTO序列化后再 hash一次”的 self-check。

该 digest hard-cut 使用 namespace `resolved-model-target-compatibility:v5`，payload只允许以下
canonical source fields，不能把整个 `ResolvedModelTargetFact` dump 后再 hash：

```text
route_id
wire_api
model_id
canonical_endpoint_base_url
transport_binding_id
transport_contract_version
model_identity_policy
limits:
  total_context_tokens
  max_input_tokens
  max_output_tokens
  default_output_tokens
  input_safety_margin_tokens
```

`canonical_endpoint_base_url` 是 `canonicalize_endpoint()` 对 scheme、authority与完整 canonical
base path的直接渲染，例如 `https://api.example.test/v1`；它不含 userinfo、query或 fragment，默认
port、percent escape、dot segment与 trailing slash继续按现有规则归一化。`/v1` 与 `/api/v4` 即使
scheme/authority相同也必须得到不同 source value。Target fact hard-cut以该字段取代只含
scheme/authority的 `endpoint_origin`，不同时保存两份可互相派生的字符串。

这些字段共同表示 physical target 与由其 hard limits确定的 context/output budget。派生的
`ResolvedModelContextBudgetFact` 可由同一 limits 重算，不能再重复进入 payload。现有
`endpoint_fingerprint` 若仍被 durable provider replay的真实边界直接消费，可以作为同一 target
fact中的独立边界值保留，但 target digest直接纳入 `canonical_endpoint_base_url`，不得再嵌套 hash
`endpoint_fingerprint`。

以下内容明确排除：

- display name、catalog source、`provider.shape` hint与诊断；
- reasoning choices/control-domain列表、默认菜单算法与本 turn selection；
- `tool_call` catalog metadata本身（实际 provider tool surface已有独立 prefix identity）；
- `ModelConnectionId`、API key及其任何 hash；
- 已由 `ProviderInputEpochCompatibility.estimator_fingerprint` 独立比较的 estimator digest；
- 已由 `ProviderInputEpochCompatibility.provider_message_lowering_contract` 独立比较的 provider
  message/native-tool lowering contract；
- 已由 `ProviderInputEpochCompatibility.provider_assistant_replay_contract_fingerprint` 独立比较的
  assistant replay contract；
- 完整 nested DTO旁的 child/aggregate self fingerprint。

当前 `ResolvedModelTargetFact.provider_request_shape_fingerprint` 是被另一 aggregate读取的 child
fingerprint，hard cut中删除。若既有 `provider_wire_profile_fingerprint()` 仍作为 provider replay /
final-wire hydration 的独立 compatibility boundary被消费，它在唯一 builder中直接读取 frozen
route/wire adapter的 closed request fields、assistant replay contract与 native-tool lowering contract
计算原有边界值；不得从 target fact读取一个 child request-shape digest，也不得把该 wire-profile
digest嵌回 target digest。

本 turn 的 `ModelCallBinding` 整体进入 `ResolvedModelCallFact`；provider adapter只从
`call.binding.reasoning`读取 optional `ReasoningSelection`，credential owner只从
`call.binding.connection_id`读取 lookup key。当前 `ResolvedModelOptionsFact` /
`resolved_model_options_fingerprint()` 只有 selected reasoning这一项，因此在 hard cut中连类型与
计算一起删除，不能改成 property，也不能留下第二个 per-selection fingerprint。

同一 exact target上 catalog新增/删除一个未被本次 call使用的 reasoning choice，不会重建 epoch；
从 `low` 改为 `high` 也只改变下一条 call fact。真正的 route/wire/model、endpoint或 transport
变化由 target digest触发 `MODEL_TARGET_CHANGED`；provider message lowering、native-tool lowering、
assistant replay与 estimator变化分别由 `ProviderInputEpochCompatibility` 的既有独立槽位触发
不兼容，不借 target digest重复覆盖。Connection ID不进入 digest；provider-input compatibility按
§12.1直接比较 typed ID。

`src/pulsara_agent/model_input/compiler.py::_compatibility_reset_reason()` 必须把
`provider_assistant_replay_contract_fingerprint` 差异与 compiler/message-lowering差异一起映射到
既有 `ProviderInputEpochResetReason.PROVIDER_LOWERING_CHANGED`。不得新增 replay-specific reset enum；
完整 compatibility不等但没有 reset reason的状态不能进入 append，也不能卡死获准的 cold successor。
不得新增：

- target-contract fingerprint 字段；
- universe fingerprint；
- choice fingerprint；
- `fingerprint -> profile` registry。

`ResolvedModelTargetFact` 携带恢复执行所需的 frozen closed fields，并在唯一 target builder中生成
上述 compatibility digest；不在 constructor里用 digest自证同一个完整对象。`rebind_model_target()`
重现这些执行字段并在真正 continuity/replay consumer处验证 compatibility；恢复一条 call时再把
turn保存的 connection ID与 selection装回 `ResolvedModelCallFact`。两步都不能读取当前 session
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
5. 用户显式选择 Chat Completions 或 Responses 并填写 API key；目录“添加配置”不要求用户
   理解或填写 base URL；endpoint 按 model-level `provider.api`、provider-level `api`、Pulsara明确维护的
   route endpoint fallback 的顺序 exact 解析；三者都没有时返回 typed
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

同一添加流程另有一个显式“自定义服务”入口。它不伪造 models.dev row，而是要求用户声明配置
名称、Base URL、Model ID、Chat/Responses、Bearer API key或无需认证、context window、最大输出、
tool calling布尔值，以及 `provider_default | toggle | exact effort list` 之一；toggle只在通用 Chat
wire contract中可选，Responses不静默丢弃它。Context window仍必须
至少为 256,000。该入口不开放 arbitrary JSON、header template、field path或 adapter code；两种
来源最终都解析为同一个 `ModelTargetContract` 并进入同一通用 Chat/Responses adapter。

确认提示是协议边界说明，不是新的 allowlist。§4.2 已明确排除 model ID leaf 以 `claude` 或
`gemini` 开头的条目；除此之外，UI 不再按模型名、provider 名或 family 猜测兼容性。选择
`Chat Completions` 或 `Responses` 也不构成真实可用性证明；实际不兼容时保留 provider 的具体
错误并引导返回该模型配置，不自动换 API。

### 6.2 Gateway target 的语义

对 gateway，`model_id` 是 models.dev 在该 gateway provider entry 下公布的 model identity。
Pulsara 不复制 gateway-level model row。若 gateway 遵循通用 OpenAI-compatible dialect，直接复用
通用 adapter。不同 request/stream/terminal/replay协议必须成为新的 dialect adapter，不能在
gateway或 model名下暗中替换通用 transport。

如果 gateway 可能把请求送到不同 deployment，Pulsara 不能把一次 HTTP 200 当作隐藏 upstream
support 的证明，也不能在 user turn 中逐 deployment 试错。但 gateway 本身就是用户选择的
route；只要 Pulsara 与该 gateway 的公开 wire contract可确定，就不因客户端无法证明其内部
routing细节而额外禁用 target。Adapter应使用 gateway公开提供的 parameter enforcement、route
pinning或 fallback control（若有），并诚实说明无法观察的 upstream行为；Pulsara自身绝不在
semantic output后换 target或重放。若用户选的 wire API实际不可用，本次 call以具体
provider/protocol error终止，UI提供“修改连接配置”入口，不自动改用另一个 wire API。

### 6.3 OpenRouter 仍使用同一通用 wire contract

OpenRouter 在 models.dev 中与 direct provider 平级；Pulsara 的默认选择器不需再另调
OpenRouter `/models` 来填 model 表。若未来引入 OpenRouter 实时 availability，它只能影响
“当前 credential 可见”的 UI 过滤，不覆盖 models.dev reasoning options，也不热改
active target。

OpenRouter 不取得独立 reasoning request lowerer；用户选择 Chat 或 Responses 后使用对应通用
request shape。`reasoning_details`由通用 Chat adapter的closed observed-field parser与exact replay处理。
`require_parameters`、fallback policy若以后成为产品控制，必须另行定义明确的 request owner；当前
不得仅因 route名称注入。Agent core、memory、tool executor、compaction不得出现
`if route == "openrouter"`。

---

## 7. Wire adapter 的唯一所有权

### 7.1 Model contract 说“可选什么”，adapter 说“怎样发送”

models.dev entry 回答“有哪些 control”；Resolver 将它与 `wire_dialect + wire_api` 通用 adapter join。最终选中的
adapter contract唯一拥有：

- reasoning 字段名、嵌套位置与 exact JSON type；
- request defaults 与冲突规则；
- stream framing 和 event normalization；
- tool-call identity correlation；
- terminal / incomplete / usage semantics；
- provider-native reasoning replay；
- exact provider error classification。

示意：

| target contract | route/wire adapter lowering |
|---|---|
| OpenAI Responses effort `high` | `reasoning={"effort":"high","summary":"auto"}` |
| OpenAI Chat effort `high` | root `reasoning_effort="high"` |
| OpenAI Chat toggle-on / toggle-off | SDK `extra_body` 携带、最终 HTTP root `reasoning={"enabled":true|false}` |
| effort `none`（若 target 定义为关闭） | adapter-owned exact disable value |
| fixed-on / no caller control | exact omission |
| unavailable / provider-default | 不发送 reasoning selector |

表中只说明责任边界；通用 Chat/Responses dialect的实际 shape由既有 adapter与request golden冻结，
不能靠models.dev `shape` hint改写。

Responses 的公开 reasoning summary 是有序多 part 值。stream中的每个 part必须以
`(output_index, summary_index)` 作为身份逐段累计、完成和校验，再按最终 `summary[]` 顺序投影；
不得忽略 `summary_index` 后把多个 part拼成一个 delta prefix。缺失 `summary_index` 只在单 part
兼容形状下等价于 `0`，不能据此猜测多 part identity。terminal adoption从exact replay重建公开
reasoning时也必须逐个保留这些part，不能把多个summary或content part连接成一个展示块。

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
    user_declared: UserDeclaredModelTarget | None
```

每条配置通过 `target` 保存 exact route/model/wire API IDs，并保存解析后的 endpoint。目录连接的
`user_declared=None`，不复制整份 models.dev row；自定义连接的 `user_declared` 只保存用户明确填写
的 display name、context/output limits、tool-call、reasoning control与认证种类。两者都不保存会话
reasoning选择或由 §4.7 确定性派生的 execution policy。`ModelConnectionConfig` 是 non-secret
runtime DTO；其 local-settings record 同时保存 nullable `api_key`，Bearer 连接必须为非空字符串，
no-auth 连接必须为 `null`。
`ModelConnectionId` 是普通随机本地主键，不由 target、endpoint 或
credential hash 派生，不建立 fingerprint registry。

首轮配置对象创建后不可原地修改 target、wire API 或 endpoint；需要另一组合时新增一条。
这避免一条已被 session/queue/turn 引用的 ID 在背后变成另一 target。本文首轮只冻结
add/list/select/delete，不渲染尚未实现的 edit/rotate 控件。删除按 exact `ModelConnectionId`
幂等执行，并在同一次 local-settings document replace 中同时移除 metadata 与该条 API key；它不
扫描或改写 canonical session、queue、turn、transcript或已安装 epoch。引用已删除 ID 的会话保持
原绑定并显示“原模型配置已删除，请重新选择”，不得静默绑定列表第一项。已经打开 provider call
并取得 key value 的 operation继续结算；尚未 resolve/open 的后续执行按缺失配置明确失败。

Catalog-backed target 的 hard limits 直接来自 frozen models.dev row；user-declared target 的
hard limits来自同一 closed metadata object。output/safety policy只按 §4.7 的 provider-neutral
公式派生。所有设置、CLI、Host 与 Web 路径都消费同一个 typed store/read model。

### 9.2 单一 local-settings document

`.env` 不能承担 GUI 首次启动所需的 canonical configuration，也不能表达多组模型配置。Hard cut
后，用户级 product configuration 的唯一 durable authority 是：

```text
${PULSARA_HOME}/local-settings.yaml
```

它是一份有固定 schema 的本机文件，同时保存 PostgreSQL DSN、模型连接及其 key、两枚固定
DashScope key：

```yaml
schema: pulsara-local-settings:v2
postgres:
  runtime_dsn: postgresql://...
  admin_dsn: postgresql://...   # optional；只供显式 initialize/migrate
model_connections:
  - id: model-connection:...
    route_id: zhipuai
    wire_api: openai_chat_completions
    model_id: glm-5.3
    base_url: https://...
    api_key: sk-...
  - id: model-connection:...
    route_id: user_declared
    wire_api: openai_responses
    model_id: local-model
    base_url: http://127.0.0.1:11434/v1
    user_declared:
      configuration_name: Local model
      total_context_tokens: 256000
      max_output_tokens: 8192
      tool_call: true
      reasoning: {kind: provider_default}
      authentication: none
    api_key: null
dashscope_credentials:
  embedding_api_key: sk-...     # optional
  rerank_api_key: sk-...        # optional
```

`postgres: null`、空 `model_connections` 与两枚 `null` DashScope key 都是合法初始状态。Bearer
连接必须在同一 record 中有 non-empty `api_key`；no-auth 连接必须为 `null`。文件使用 no-follow、
父目录 `0700`、文件 `0600`、1 MiB 物理大小边界与 write/fsync/atomic replace。API key 在文件中是
可直接使用的值，不再复制到 Keychain、PostgreSQL、另一个 sidecar、环境变量或 credential registry。
文件不保存 catalog snapshot、会话 reasoning preference、credential digest、revision、generation、
fingerprint 或数据库 row 副本，也不演化为 arbitrary key/value registry。

`pulsara-local-settings:v2` 是唯一接受的格式。Hard cut 不读取 v1、不从 `.env` 或 Keychain 导入，
也不做双读、迁移或 fallback；已有用户需要在 GUI 重新填写 key。格式或权限无效时普通 read 返回
typed unavailable，本机 HTTP/设置壳仍可启动；用户发起的显式设置 mutation可以用当前提交的
closed values重建文件。

同一 Web 进程内只有一个 process-local `LocalSettingsStore` mutation owner。模型配置添加、精确
删除、PostgreSQL保存与两枚 DashScope key的 replace/clear都必须在该 owner的同一异步互斥区内
重新读取最新 document、只修改目标字段并完成一次 atomic replace；调用方不得先读取旧 snapshot
后分别覆盖。Mutation从开始落盘到结果确定采用 cancellation-shielded局部 settlement；replace后
发生 commit-unknown时，同一 owner重新读取**完整目标 document**，exact相等即确认成功，不相等或
无法读取则返回 typed indeterminate。这里没有两存储提交，也不需要 orphan清理、receipt、repair
job、revision或跨进程 lock。

添加模型配置时，先对 frozen catalog或 closed user-declared facts完成 validation，再生成 random
`ModelConnectionId`，最后把 metadata与 nullable API key作为一个 document value发布。删除按 exact
ID幂等执行，同一 write同时移除该 record和 key，只保留其他模型配置、PostgreSQL与两枚 DashScope
字段。HTTP response与 bootstrap只返回 non-secret summary和 `credential_configured: bool`；不返回
key、masked key、persistent secret reference或文件片段。保存成功或失败后，前端都清空 password
input；不提供 reveal/read-back API。

两枚 DashScope key相互独立，用户可以填同一个阿里云百炼 key，也可以分别轮换；Pulsara不根据
`sk-*` 等外观猜 provider。Embedding继续使用既有 `text-embedding-v4` / 1024-dimension 与
DashScope-compatible endpoint，reranker继续使用 `qwen3-rerank` / DashScope contract。Provider、
model、endpoint、dimension、timeout/concurrency等执行参数不进入 GUI。缺少 embedding key时 dense
recall/governance relatedness按现有 advisory degradation工作；缺少 reranker key时 explicit search
使用现有 pre-rerank结果。两者均不阻止 app、用户 turn、lexical memory path或主模型 dispatch。

每次 provider、embedding或 rerank logical operation在真正 open时从本次已读取的
`LocalSettings` value取得一次 exact key；同一 operation的 bounded transparent physical retries
复用该值，不逐 attempt重读文件。已经取得值的 operation在用户随后删除/替换配置后继续结算，下一
operation读取新值或明确缺失。不得跨 operation缓存含 key 的 provider client，也不增加 key
fingerprint、generation、durable invalidation event或 credential-store abstraction。

Web mutation request中的 key只在 same-origin/loopback request body、React非持久输入与该次调用的
内存值中短暂存在。Mutation response、bootstrap、session snapshot、diagnostics与日志只返回
configured bool；browser `localStorage`、URL、PostgreSQL与 canonical transcript均不得包含 key。
现有 `ProcessCredentialBoundary` 若用于构造单次 provider client，只接受本 operation已读取的 value，
不是 durable store，也不从 `PULSARA_API_KEY` 回读全局 secret。

#### 9.2.1 PostgreSQL 配置与无数据库 bootstrap

本机 PostgreSQL 使用两个直接对应当前真源的字段，不复制一份结构化 host/user/password shadow：

```python
@dataclass(frozen=True, slots=True)
class LocalPostgresConfig:
    runtime_dsn: str
    admin_dsn: str | None
```

`runtime_dsn` 是 conversation kernel 唯一运行连接；`admin_dsn` 只供用户显式点击“初始化/升级”、确认“重置数据”或
CLI `pulsara db migrate`，不得在普通 Host启动、schema verify或 user turn中使用。两者是本仓库
允许直接检查的本机 DSN，不创建 DSN fingerprint、credential row、database identity cache或
PostgreSQL settings table。

`LocalWebApplication.start()` 必须先发布 static UI、closed local-settings endpoints 与设置页，
再尝试构造 database-backed `KernelHostCore`。不能像当前代码一样在 HTTP listener发布前调用
`sessions.prepare()` 并让 PostgreSQL失败杀死整个 Web app。数据面状态是普通 process-local
observation：

```text
database_not_configured
database_configured_unverified
database_unavailable
database_schema_action_required
database_reset_required
database_resetting
database_restart_required
ready
```

这些状态不持久化为 event/job/checkpoint。未 ready时 catalog、模型配置、DashScope key与 PostgreSQL
设置仍可读写；session/task/turn endpoints返回 typed service-unavailable，不能伪造空会话真值。

“本地服务”页面提供 runtime DSN、optional admin DSN、保存、只读连接检查与显式初始化/升级动作。
保存本身只验证 closed DSN syntax并原子更新本地文件，不偷偷连接、不自动 migrate、更不自动 reset。
连接检查有现有物理 deadline；初始化/升级复用现有 migration runner并在执行前展示 exact database/
role target。若当前进程尚未发布 kernel data plane，验证成功后可以构造并一次性发布它；若已有
kernel正在使用旧 DSN，保存不热换其 repository或中断健康工作，新值在下次 Pulsara进程启动时
生效，UI明确显示“已保存，重启后连接”。这是一条真实 PostgreSQL owner-lifetime边界，不新增
pending-config字段或 busy/no-pending gate。

本地服务另提供显式“重置数据”：确认弹窗展示已保存的 runtime/admin DSN，说明会删除目标数据库
中 `pulsara_v3` 与 Pulsara migration ledger（含会话、图片、任务、记忆），保留模型配置、密钥和
工作目录。仅该确认动作允许删除；初始化/升级仍不得隐式 reset。后端核对弹窗携带的完整 DSN
与当前配置相等，并由既有 migration runner 验证 admin/runtime 是同一实际数据库；删除与新
baseline 安装放在同一事务和既有 migration lock 内，失败回滚。reset 的 commit ACK 不明确时
不自动重试，也不能仅凭 baseline 相同宣称数据已清空。

`database_reset_required` 精确表示旧 universe 需要重置；与普通初始化、权限和连通错误区分。
重置期间 `database_resetting` 阻止数据面新请求。若已构造数据面，先通过既有 bridge/session/core
close owner 停止运行，关闭失败不删除；重置后保持 `database_restart_required`，提示重启 Pulsara，
不在存活进程内热换 repository。尚未构造数据面的设置壳可在重置初始化后直接完成首次发布。
这些状态均为可丢弃的进程观察，不增加事件、表、job、receipt 或新的 provider prefix 边界。
schema verification 的失败项在本次物理操作结束后移出缓存，现有等待者仍得到原失败；下次显式
检查重新校验，不自动重试。成功的已发布数据面仍保持原 owner lifetime。
HTTP 等待者取消不提前释放重置期间的互斥或恢复接纳，必须等物理事务结束；进程正常关闭也等待该操作结束。

### 9.3 设置页：模型配置卡片与“添加配置”

设置页的“模型”分区改为一组组已保存配置，而不是“主要模型/轻量模型”或单行“当前模型”：

```text
模型配置                                      [添加配置]

Zhipu AI · GLM-5.3
Chat Completions · 1,000,000 上下文 · API key 已保存

OpenRouter · openai/gpt-5.6-luna
Responses · 400,000 上下文 · API key 已保存
```

“Models.dev 目录”入口只要求四项：

1. Provider/route；
2. Model ID；
3. API key；
4. `Chat Completions` 或 `Responses`。

Provider/model selectors 只消费 kernel 的 `SelectableModelCatalog`：只显示
`limit.context >= 256000` 且 model ID leaf 不以 `claude`/`gemini` 开头的条目；provider 过滤后
没有模型则不显示。前端不得再次实现 threshold、leaf 解析或 family/provider heuristics。

Base URL不是目录入口的第五个必填项，按 §6.1 的 exact precedence解析并在确认摘要显示。

相邻的“自定义服务”入口要求：配置名称、Base URL、Model ID、Chat/Responses、认证方式、
context window、最大输出、tool calling，以及 reasoning control。认证方式只有 Bearer API key与
无需认证；选择后者时 local-settings record的 `api_key` 必须为 `null`，且 SDK不得从环境变量补取
key。Reasoning只允许
`provider default`、Chat通用 `toggle` 或用户给出的 exact effort列表；unknown应选 provider default，
不在线试探。所有字段都是 user/operator asserted facts，不冒充 catalog evidence。

自定义入口同样执行 256,000 context最低门槛。它不允许 arbitrary request JSON、header模板、
provider/model条件分支或 adapter代码；用户选错 Chat/Responses时保留具体协议错误，不自动换 API。

若 selected model 有 `provider.shape=responses|completions`，对应 API 标为“models.dev 建议”；
用户选另一项时只显示 non-blocking warning，仍可确认保存。`provider.shape` 缺失时不显示推荐
或 mismatch warning，不从 `npm`、model family、provider 名或 endpoint 字符串补猜。

最终确认页始终显示 route、model ID、API protocol、resolved endpoint、context limit 和固定
说明：

> Pulsara 当前只支持与 OpenAI Chat Completions 或 Responses 兼容的接口。模型出现在目录中
> 不代表所选提供方一定支持你选择的 API 协议；若调用失败，请返回这里更换配置。

保存动作只做 local schema/contract validation 与一次 local-settings document write，不为了“验证”而偷偷发送
模型请求，也不把 HTTP 200 当作长期协议兼容证明。显式“测试连接”是独立动作：它通过用户所选
通用 adapter发送一次有 45 秒物理边界、64 output-token上限且不自动 retry的极短请求；临时 key只
存在于该请求进程内，不发布 local-settings record。测试失败不阻止随后保存，保存也不要求先
测试。保存成功后新卡片立即加入同一个 read model；目录连接的 reasoning control不在设置页编辑，
自定义连接仅声明 target control domain，仍不保存会话默认。

每张已保存模型卡片提供“删除”。第一次点击只打开行内确认，不显示不可用的 edit/rotate；确认后
调用 exact、幂等删除。成功文案明确说明配置与其中的 key均已移除、已有会话不会自动改用其他
模型。设置页立即采用返回的 non-secret列表；全局 refresh失败不能把已成功删除误报为“未删除”。

同一“模型”分区在模型配置卡片下显示一个固定的“记忆检索 · 阿里云百炼 DashScope”组：

```text
Embedding · text-embedding-v4 · 1024 维     [未配置 / 已配置] [填写或更新]
Reranker  · qwen3-rerank                    [未配置 / 已配置] [填写或更新]
```

这里只填写/替换/清除两枚 key，不显示 provider、endpoint、model 或高级参数 selector。文案明确
说明这是可选的记忆相关性增强；未配置不会阻止对话。Frontend永远只收到 configured bool，不把
input value回填为 masked secret，也不把 key放入 React持久状态、URL或 browser storage。

“本地服务”分区增加 PostgreSQL card，显示 data-plane状态、runtime DSN、optional admin DSN与
§9.2.1 的连接/初始化动作。数据库未配置或不可用时，设置导航和上述模型/credential页面仍正常
工作；会话入口显示真实不可用原因，而不是把整个浏览器连接标为失败。

首轮 HTTP surface 使用不与现有 live-control `/api/connections/...` 混淆的独立路径：

```text
GET  /api/model-catalog
POST /api/model-catalog/refresh
GET  /api/model-configurations
POST /api/model-configurations
DELETE /api/model-configurations/{connection_id}
GET  /api/local-settings
PUT  /api/local-settings/postgres
POST /api/local-settings/postgres/check
POST /api/local-settings/postgres/migrate
PUT  /api/local-settings/dashscope-credentials/{embedding|rerank}
DELETE /api/local-settings/dashscope-credentials/{embedding|rerank}
PUT  /api/sessions/{session_id}/model-call-binding
```

Catalog response 已经完成 §4.2 filter；configuration responses 只含 non-secret summary；session
binding mutation一次提交完整 `ModelCallBinding`，改变 connection或 reasoning都复用这一条接口，
并继续要求现有 controller/writer authority。Local-settings API只接受上述 closed operations，
不能变成 arbitrary JSON patch或 secret read-back。不要把 model configuration复用或塞进已有
browser connection resource。Catalog refresh只尝试构造并发布 §10.2 的 process-local immutable
snapshot；它不写 settings/数据库，也不热改 active target。

应用可以在零模型配置、零 DashScope key、未配置/不可连接 PostgreSQL且没有任何旧
product-config env时启动并打开设置。数据库 ready但未选择模型时，会话页显示“请先添加并选择
模型配置”，发送按钮不可执行，并提供直达设置页的入口；启动本身不能再因任一旧 `.env` 字段
缺失而失败。

### 9.4 会话页的模型配置 selector

对话输入框底部同时拥有“模型”与“推理”两个轻量 selector。模型 selector 列出全部已保存
配置，以 route display name + model display/ID + Chat/Responses 区分；选择的是
`ModelConnectionId`，不是临时 target 字符串。

- 每个 session 最多绑定一条 current connection；未绑定时可以浏览历史，但不能发送；
- 新 session 不暗选列表第一条，也不使用全局“默认模型”；用户在会话页显式选择；
- 选择成功后跨 turn、页面刷新、应用重启和 controller 接管保持，直到用户再次更改；
- 同一 connection 下更改 reasoning 只遵循 §9.5，不改变模型绑定；
- 模型选择可以在 running/queued期间修改，但只改变 session 的**下一条尚未 admission 的
  NEW_TURN** 默认；已经接纳、排队或运行的工作继续使用各自冻结的 binding，不取消、不重排、
  不重写，也不热换 active provider call；
- 从 A 改为 B 时，在一个 session mutation 中把整个 `ModelCallBinding` 换成 B connection +
  B target按 §5.3计算的默认；旧 choice即使同名也不迁移；
- 连接切换不回溯修改 canonical transcript。下一条 `NEW_TURN` dispatch 发现 connection 与
  当前 installed epoch 不同后，以 B 建立显式 new cold epoch；不得 same-epoch 热换 endpoint、
  model、wire API 或 credential；
- 同一进程中的另一个 session 可以继续使用 A，互不影响。

这里不增加 idle/no-pending 强门禁。队列顺序本身就是 execution sequence；若 A-bound prompt
之后排入 B-bound prompt，消费 B 时才在获准的下一 turn cold boundary安装 B。改变 composer
选择不是取消 authority，也不是修改任何既有 queue/turn row。

### 9.5 对话输入框是唯一 reasoning 选择入口

对话页输入框底部提供一个轻量“推理”按钮。它不是 per-turn 临时 override，也不提供
“设为连接配置默认”分支：

- 菜单来自当前 exact target 的 catalog controls，不是全局五档；
- 新会话按 §5.3 显示并保存当前 target 的上中位默认；
- 用户选择一个真实 control 后立即更新该会话偏好，按钮保持该值，直到用户再次修改；
- 每次提交 `NEW_TURN` 时，admission在现有 session row lock下取得 canonical
  `ModelCallBinding` 并写入 command；成功 admission 后该值属于这条输入，执行时不再读取
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
storage 因此直接落在现有三张表，不新建通用 settings 表。Connection与 reasoning本来就是
同一 execution binding，数据库不把它拆成六列：

```text
pulsara_v3.sessions.model_call_binding
    当前 composer选择；nullable jsonb，未配置时允许浏览但禁止 NEW_TURN

pulsara_v3.prompt_queue_items.model_call_binding
    NEW_TURN 提交时的 frozen exact copy；STEER_ACTIVE_TURN 必须为 null

pulsara_v3.turns.model_call_binding
    ROOT turn 从 direct command/queue exact copy；SUBAGENT_TASK 与 Plan/terminal continuation
    exact继承 origin/parent turn
```

三列复用同一个 `ModelCallBinding` closed JSON codec，只携带 `ModelConnectionId +
ReasoningSelection | null`，不重复 `ModelTargetKey`，也不另建 target/choice/binding fingerprint。
数据库只检查 nullable/object与 queue delivery/inheritance union；closed union、connection解析与
selection membership由唯一 repository/LLM boundary验证。不要为了让 SQL重新实现 Python type
system而拆列、复制枚举、建立 profile表或对 `${PULSARA_HOME}` 文件伪造 FK。

所有 user-originated ROOT admission path都在现有 controller/writer authority 下取得 canonical
`sessions.model_call_binding`，并把同一个 frozen value纳入 command payload、现有 durable semantic
digest、queue/turn row与 confirmation comparison。只扩充既有 digest builder的 semantic payload，
不新增 `binding_digest` 字段、receipt或 registry。`queue_prompt.v1` / 对应 direct submit schema
hard-cut bump；command一旦冻结 binding，后续 idempotent replay必须与该 frozen value比较，不能
回读 session稍后的 current choice。

Binding mutation 与 user-originated `NEW_TURN` admission锁同一 session row，因而形成一个普通的
数据库顺序。Admission在任何 queue item、entry或 turn写入前取得 canonical binding；queue消费
绝不能回读 session current binding。现有 controller/writer authority已经决定 caller能否 mutation
或 submit，本功能不再新增 `stale_model_selection`、expected-binding compare-and-reject gate、
revision、generation、lease或 fingerprint。若两个已获准操作真实竞态，由 row-lock顺序决定
admission取得哪一个 binding；mutation/admission response返回 resulting non-secret session snapshot，
供 UI 渲染 canonical choice。

Host在打开 canonical PostgreSQL transaction前取得本次 frozen process-local config/catalog
snapshots；事务内禁止读取 `${PULSARA_HOME}`、访问网络或取得 API key value。Queue binding已知，
因而可在事务外完成 non-secret pure resolution；direct admission必须先
在锁内读取 canonical session binding，再只针对上述已冻结内存值做 pure validation。队列消费采用：

1. 在事务外 decode queue payload的 non-secret binding，并从冻结 snapshots
   取得 immutable connection/target value；
2. connection metadata缺失、target hard limits非法、wire dialect adapter不存在，或 queue已冻结的
   explicit selection现在无法 membership/lower，属于永久 admission failure，
   复用现有 queue `REJECTED` + `PROMPT_REJECTED` settlement，写精确 terminal reason，不创建 turn；
   未被该 binding选择的 reasoning family异常按 §4.6/§5.2局部降级，不能把无关 catalog
   diagnostic升级成 queue rejection，也不能静默改写已冻结 selection；
   暂时没有可用 catalog snapshot时保持 `PENDING`并按现有 Host调度稍后再试，不增加 lifetime cap、
   job或 durable retry counter；
3. 进入 canonical transaction，锁定 pending queue head并 exact比较事务外 prepared 的 queue item
   identity与 frozen binding；
4. 将相同 binding写入新 turn，消费 queue item、接受用户 entry并创建
   turn；
5. commit后到该 turn第一次 provider open才执行一次 local-settings read，并按 binding的 frozen
   connection ID取得 API key value；resolved metadata/target不随这次 read重算。配置/key缺失按现有
   model-call failure路径终止已接纳 turn，不把它倒退成 queue rejection，也不作第二次读取。

Direct ROOT admission使用同一分界：事务前冻结 config/catalog snapshots，事务内锁 session、取得
canonical binding、对冻结内存值pure resolve并完成 turn/entry写入，commit后 provider open才读
一次当前 local-settings并取得 key。这样 filesystem/network延迟不会占用 session/queue数据库锁，
key value、prepared plan与 transport handle仍各自只有一个 operation owner。

`sessions.model_call_binding` 只表示下一条尚未 admission的新输入的当前选择，不是历史 turn
authority；历史执行以 queue/turn binding与 resolved model-call fact为准。
多窗口更新采用现有 controller/writer authority，最后一个成功的用户选择成为当前值。发送不能把
client payload中的旧 binding当成 per-turn override；admission在 row lock下冻结 canonical session
value。无需新增 preference-changed event：当前控制窗口直接采用 mutation/admission response，
接管或重连都从 canonical session snapshot读取。

### 9.7 Kernel wiring、config-check 与 env hard cut

`LLMConfig.pro/flash` 不收敛成一个新的全局 `LLMConfig.model`；它们整体由用户级
`LocalSettingsStore` 与 per-session binding 取代。`ModelRole` 整个
类型删除，而不是留下只有 `PRO` 的单值 enum。所有依赖 role 的 API 参数、fact 字段、CLI
option 与分支一并删除。

Main model call、subagent、summary、compaction 与 memory governance 直接取得当前 origin
session/turn 已冻结的同一 `ModelConnectionId` 和 `ResolvedModelTarget`。调用目的继续由已有
purpose 类型表达；purpose、output cap 与 deadline 不拥有 model routing authority，也不能
借 auxiliary 名义解析另一配置。没有 session/turn origin 的设置/catalog 操作不打开模型。

Production product configuration 的唯一入口是上述单一本机设置文件。删除：

```text
PULSARA_API_KEY
PULSARA_PROVIDER
PULSARA_API
PULSARA_BASE_URL
PULSARA_MODEL
PULSARA_PRO_*
PULSARA_FLASH_*
PULSARA_EMBEDDING_*
PULSARA_RERANK_*
PULSARA_MEMORY_AUTO_DENSE
PULSARA_MEMORY_EXPLICIT_RERANK
PULSARA_POSTGRES_DSN
PULSARA_POSTGRES_ADMIN_DSN
PULSARA_REQUEST_DEFAULTS_JSON
PULSARA_EXTRA_BODY_JSON
PULSARA_OMIT_PARAMS_WHEN_THINKING
PULSARA_SUPPORTS_TOOLS
PULSARA_SUPPORTS_REASONING
PULSARA_MODEL_IDENTITY_POLICY
PULSARA_THINKING_*
PULSARA_OPENAI_SDK_MAX_RETRIES
PULSARA_LLM_RETRY_*
--model-role
--env-file
--override-env
--prefix（作为 product-config namespace）
```

删除 eager `PulsaraSettings(llm, storage, retrieval)` production bootstrap、其 `from_env()` /
`from_env_file()`、`load_env_file()` 与 production CLI 对 `.env` 的加载；由 `LocalSettingsStore` 与
仅在 data plane启动时构造的 typed runtime inputs取代。删除 `.env.example`，README不再指导用户
创建 `.env`。不保留 env import、首次启动迁移、alias、fallback 或双读。`PULSARA_HOME` 这种安装根选择、terminal/hook process plumbing 与 CI/test
harness 的显式环境输入不是 product configuration，不因本节改名或塞进 GUI。Dogfood/test harness
可以从其环境取得 secret/临时 PostgreSQL fixture后显式注入 in-memory port或 constructor，但
production app、host、db CLI与 config-check不读取上述旧 product variables；harness全程仍不得
输出 key。

上述 hard cut不改变现有 provider-neutral physical retry语义，也不把 retry knobs搬进 GUI：删除
`retry_config_from_env()`，runtime直接使用当前 `LLMRetryConfig()` validated defaults（enabled、最多
3次 physical attempt、0.5秒 base delay、8秒局部 delay上限、0.2 jitter、30秒
`Retry-After` admission上限）。Pulsara safe retry启用时继续把 OpenAI SDK内建 retry明确设为 `0`，
避免隐藏双重 replay；删除 `openai_sdk_max_retries` product override。这里保留的是既有单次 transport
operation物理边界，不是 provider stream、turn、task或长程工作的 total lifetime cap，也不新增
reasoning/model fallback。

`pulsara app` / local Web server 在零配置时也必须启动。`pulsara db status|migrate|verify` 默认读取
同一 saved PostgreSQL config；migrate缺少 admin DSN时才返回 typed可操作错误，不回落环境变量。
`config-check` 读取同一 local settings与一次 catalog snapshot，分别报告
PostgreSQL配置/连通/schema状态、两枚 DashScope configured bool，以及每条 model connection
的 key-configured、catalog、adapter、endpoint与§4.7 budget状态。零配置返回可操作的 setup状态，但
不能阻止用户启动 app进入设置页。Config-check不发送模型/retrieval请求，不输出 key。

### 9.8 显式 user-declared target 来源

私有 endpoint、self-hosted deployment或 models.dev缺失的模型使用 §9.3 已暴露的 closed
`UserDeclaredModelTarget`。它与 models.dev 是两个事实来源，而不是第二套执行路径：resolver先把
两者变成同一 entry-shaped target facts，再 exact join通用 OpenAI-compatible Chat/Responses
route-wire contract。Models.dev不可用时 user-declared connection仍可解析；catalog connection仍
必须 exact命中当前 selectable snapshot。

该来源不支持 provider-specific field path、raw JSON、任意 header、model-family猜测、在线能力
探测或协议 fallback。新增真实 wire形状仍只能进入通用 adapter的 shape-based normalization，不能
进入某条自定义连接的配置文本。

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
provider: id, name, npm, api
model: id, name, reasoning, reasoning_options, tool_call,
       limit.context, limit.input?, limit.output,
       provider.api?, provider.shape?
```

model-level `provider.api` 若存在，作为该 exact model 比 provider-level `api` 更具体的
endpoint 预填值。`provider.shape` 只接受 `responses|completions`，并只作 UI hint。
`npm` 只解析为 `openai_compatible | provider_native | unknown` dialect；它不生成 reasoning path、
stream状态机或 replay规则。Pricing、marketing description、benchmark、modalities等不进入首轮
execution contract。Outer provider/model key 是 catalog canonical identity；inner `id` 一致时作为
冗余确认，不一致时记录 row diagnostic但不推翻可由 outer key明确定位的其余数据。Parser不对
ID做 lower-case、alias、family或 substring normalization，也不因未知 optional字段拒绝整个
snapshot。

`limit.context < 256000`、Claude/Gemini leaf predicate 命中的 row 仍可存在于这次 raw
snapshot，以便给出准确 validation reason；它们永远不进入 UI provider/model list、不能创建
catalog-backed `ModelConnectionConfig`，也不能由 CLI/config-check 绕过。该过滤不使用或改变
`provider.shape`。

Catalog GET 不带 provider API key，不携带 prompt、model selection、workspace 或用户数据。

### 10.2 Fetch lifecycle：单一 process-local snapshot owner，不在 request path 热刷新

Local Web application内只有一个 process-local `ModelCatalogOwner`，持有最近一次成功 parse的
immutable `ModelCatalogSnapshot` pointer。设置 controller、model-connection validator、Host admission
与 queue consumer读取同一个 owner；不得各自持有互不相知的“设置页 snapshot”和“Host snapshot”。
Snapshot是可丢失 projection，不是 durable truth，不附加 fingerprint、revision或 generation。

允许发生直接 GET 的边界只有：

1. cold local Web/Host 启动；
2. 打开设置页或“添加模型配置”流程时 owner尚无 snapshot；
3. 用户显式点击“刷新模型列表”；
4. 独立 `config-check` 进程。

成功 fetch + parse先构造完整 immutable value，再原子替换 owner pointer；失败保留当前进程最近一次
成功 snapshot，若从未成功则状态为 `model_catalog_unavailable`。新 model connection必须针对 owner
当前 exact snapshot验证；保存成功后 Host立即能从同一个 pointer解析它。显式刷新发布的新 snapshot
只供尚未 admission的工作与后续设置读取；active epoch、已冻结 turn/provider retry/tool continuation、
summary和compaction继续使用自己的 frozen target，不读 owner，更不因刷新 rebase provider prefix。

Queue consumer若 owner没有 snapshot，保持现有 item `PENDING`；后来一次显式或 cold refresh成功
发布 snapshot后，普通 scheduler下一轮即可重新尝试，也可以触发一个纯 process-local wakeup。这个
wakeup不是 durable event/job/receipt，不增加 retry counter或 lifetime cap。Catalog option从新
snapshot消失时，controller在下一次 session snapshot读取、binding修改或 `NEW_TURN` admission时
按 §5.4于既有 session row lock内完成 reconciliation；只有实际写回 binding的那次响应携带提示，
不持久化 notice状态。

Provider request、retry、tool continuation、summary和compaction绝不触发网络 catalog fetch。不引入：

- checked-in `model_targets.json` 或 Python per-model constants；
- 本地磁盘 mirror/cache、TTL refresh worker 或 file lock；
- PostgreSQL table、event、job、checkpoint、receipt 或 catalog fingerprint registry。

一个正在运行的 session/epoch 在 models.dev 稍后改变或暂时不可达时仍持有已冻结 target。
独立 `config-check` fetch失败时返回 typed `model_catalog_unavailable`；Web进程只有在 owner从未有过
成功 snapshot时返回该状态，不回落到隐藏旧表或猜测数据。使用普通 connect/read-idle transport
watchdog，不为 Host/turn增加 total wall-clock lifetime cap。

### 10.3 责任分界

| 责任 | 所有者 |
|---|---|
| provider/model 列表、display name、默认 endpoint hint | models.dev |
| 目录 target 的 limits、reasoning options、tool-call 布尔值 | models.dev |
| 自定义 target 的 display name、endpoint、limits、reasoning/tool facts与认证种类 | 用户显式填写的 closed `UserDeclaredModelTarget` |
| 256k context 与 Claude/Gemini model-ID leaf 选择过滤 | Pulsara kernel 的单一 `SelectableModelCatalog` predicate |
| Responses / Completions 非强制建议（存在时） | models.dev `model.provider.shape` |
| Chat / Responses 的选择 | 用户保存的 `ModelConnectionConfig` |
| Bearer model API key durable value | 对应 model connection record的 `api_key`；no-auth为 `null` |
| DashScope embedding/reranker key durable value | `dashscope_credentials` 中两个独立 nullable字段 |
| endpoint、connection metadata、全部 API key 与本机 PostgreSQL DSN | `${PULSARA_HOME}/local-settings.yaml` |
| embedding/reranker provider、model、endpoint与执行参数 | 现有 fixed DashScope code contracts；不是 GUI/provider universe |
| provider `npm` 到通用 wire dialect 的解析 | models.dev metadata + Pulsara 的小型 dialect classifier |
| request主干、SSE、terminal、tool correlation、closed observed-field replay | Pulsara 通用 Chat/Responses adapter |
| models.dev缺失的已知 route endpoint | 独立 route endpoint fallback；不影响 adapter选择 |
| 当前会话 model-call binding | 会话页的模型/推理控件；session 保存，NEW_TURN admission 冻结 |
| local-settings 与 database-not-ready UI | PostgreSQL-independent local Web settings shell |

这个边界意味着 Pulsara 不再调研并手工录入每个 provider/model，也不维护 route allowlist。
全部 `@ai-sdk/openai-compatible` route 自动复用通用 Chat/Responses transport及其 reasoning
request/replay逻辑；provider与 model不会成为另一层 wire lowering owner。

User-declared facts不进入 `ModelCatalogSnapshot`，不反向修改 models.dev，也不建立另一份 catalog。
它们只随对应 connection保存在 local settings，并在 resolver边界物化为同一 target contract。

### 10.4 Zhipu AI / GLM-5.3 的端到端示例

2026-09-03 直读 models.dev 的 exact row 为：

```text
route_id: zhipuai
route name: Zhipu AI
provider.api: https://open.bigmodel.cn/api/paas/v4
model_id: glm-5.3
model name: GLM-5.3
reasoning options: effort [low, high, max]
tool_call: true
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

用户若不修改，选 Chat 发送时，通用 Chat adapter 产生的关键 request material 为：

```json
{
  "model": "glm-5.3",
  "messages": ["...canonical lowered messages..."],
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

Catalog-backed saved connection只保存 exact target IDs 与 endpoint，不保存派生 policy、
reasoning 或 catalog row。新 cold Host 读到的 models.dev row 若已删除、`limit.context` 已降到
256,000 以下，或现在命中 Claude/Gemini leaf filter，该 connection 标为不可选择并要求用户
新增另一配置；不自动换 model/route/API。会话 binding中的 reasoning若仍属于 exact target
就继续使用；若 target 或 choice 已失效，按 §5.4 重新计算默认并给出 UI 提示，不做 projection
或 alias。Active turn/installed epoch 已冻结的 connection、target 与 selection 不受 catalog
refresh 影响。

User-declared saved connection保存其 closed target facts，不依赖 catalog refresh；其会话 selection
仍按相同 validation/reconciliation算法处理。修改事实不原地更新 connection，而是新增另一配置。

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

Pulsara只对自己发送给 gateway以及从 gateway收到的 wire contract负责，不发明一个“证明
gateway内部所有 deployment行为一致”的 admission gate。Route adapter在 gateway公开支持时
应发送 parameter-enforcement、pinning或 disable-fallback字段；不支持这些控制时，UI/诊断诚实
说明内部 routing不可观察，但 target仍按 gateway自身公开协议执行。

无论 gateway内部如何实现，Pulsara自身都不得因错误或 telemetry自动换 model/route/effort，
也不得在已收到 semantic output后发起透明重放。Gateway telemetry不重写 resolved target；真实
stream若违反 adapter terminal/replay contract，仍按既有 protocol failure fail closed。

---

## 12. Prefix、replay、compaction 与 ownership

### 12.1 Prefix continuity

Universe/catalog 变化不能成为第三种 prefix rebase boundary。当前 session/epoch 冻结的
connection/target 继续使用。会话在 §9.4 修改下一条输入的 connection 后，首条使用该 connection
的后续 NEW_TURN dispatch使用既有 **new cold epoch** 边界；显式 adopted compaction successor仍是另一条获准
边界。设置页新增别的配置不会影响任何现有 session。
对话输入框的会话级 reasoning preference 不改变 target 或 context-bearing prefix；每条
`NEW_TURN` 输入在点击发送时冻结自己的副本，只作为该 turn 的 output control。

同一 epoch 内：

- SYSTEM 与 provider tools byte-identical；
- messages 只追加 suffix；
- reasoning control 不注入 dynamic pre-prefix content；
- 后续 catalog refresh 不热替换 route/wire/model contract；
- API key value、配置列表变化或另一个 session 的选择不改当前 epoch。

Existing provider-input compatibility join 增加 frozen `ModelConnectionId` 的 exact equality；不把
它哈希进另一个 fingerprint。同 target但不同 connection仍不兼容，必须走 new cold epoch。

### 12.2 Provider replay

Reasoning request selection 是 output control，不翻译、不改写、不删除历史 native reasoning
carrier。更换 `ModelConnectionId` 一律按 cold semantic continuation 处理，即使新旧 model ID
相同；真正改变 wire API、endpoint、credential binding、normalized model identity、transport
binding、adapter contract 或 replay contract 时更不能 same-epoch 继续。

### 12.3 Final-wire estimation 与 compaction

Reasoning selector、output cap、model ID、stream/usage 均不是 context-bearing provider input：

- selected reasoning进入 call binding/request，不进入 target compatibility digest；
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
execution authority。只有在 turn/admission transaction已 commit、dispatch已冻结
`ModelConnectionId` 且真正进入 provider open时，adapter才从 local settings读取一次 API key；
任何 canonical PostgreSQL transaction内都不得读取 settings文件。现有 prepared
plan/open-once/transport handle 仍是唯一 execution owner：

- resolver不 materialize provider input；
- 同一 logical provider call只取得一份 API key value，其全部 byte-equivalent transparent
  physical retries复用该值；call结算/close/cancel后不可复用，单个 retry attempt不得重新读取；
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
- 完整保留组合 reasoning options、provider `npm` dialect与 optional `provider.shape` hint；
- exact catalog lookup、closed user-declared facts、generic dialect adapter join与同一 validation；
- top-level fetch/JSON结构失败使 snapshot unavailable；row/option局部异常按 §4.6 诊断并降级；
  invalid hard limits只拒绝该 row；
- user-declared target不进入 catalog、不在线探测，也不携带 arbitrary wire配置。

不得新增 production `model_targets.v*.json`、per-model Python constants 或后台 refresh worker。

不得创建 `llm/capabilities.py`。

### 13.2 `llm/provider.py` 与 route/wire types

- 将 overloaded `ProviderProfile` hard-cut 为 route/wire contract；
- 删除 model support bool；
- 保留并明确 output-side Chat replay field contract；
- request defaults只能包含 adapter allowlist 中的非 reasoning keys；
- adapter-owned reasoning key发生冲突时构造阶段拒绝；
- OpenRouter 等 gateway继续保留 models.dev route identity，并与其他 OpenAI-compatible route共享
  generic Chat/Responses transport及reasoning lowering；
- OpenAI缺失的默认 endpoint以独立 endpoint fallback提供，不复制 Chat/Responses contract。

文件名是否继续叫 `provider.py` 不影响语义；类型名与 public contract 不得继续使用
`ModelCapability*`。

### 13.3 Local settings、model connection 与 API key

```text
src/pulsara_agent/settings.py
src/pulsara_agent/llm/model_connections.py
src/pulsara_agent/retrieval/config.py
pyproject.toml
uv.lock
tests/test_settings.py
tests/test_llm_model_connections.py
tests/test_retrieval_credentials.py
```

- hard-cut `settings.py` 的 eager `PulsaraSettings` / `.env` parser / monolithic env config，实施
  `${PULSARA_HOME}/local-settings.yaml` v2 closed codec、no-follow、0700/0600 与 atomic replace；
- 同一 local-settings document实现 PostgreSQL config、model connection metadata + nullable key、
  DashScope两枚 nullable key，以及 add/list/read/delete/replace/clear exact operations；由一个
  process-local mutation owner串行化 read-modify-write；catalog连接不保存 catalog row，自定义连接
  只保存 closed user-declared target facts；两者都不保存会话 reasoning默认或 arbitrary settings，
  不增加跨进程 lock/revision；
- hard-cut 删除 `local_credentials.py`、`keyring` dependency、Keychain backend/fake与全部双存储结算；
  不读取或迁移旧 Keychain item；
- retrieval config删除 provider/model/base URL/tuning/feature-toggle env owner，只保留现有 fixed
  DashScope contracts，并由每次 logical operation从local settings读取一次对应key；
- Web/API/read model从不返回 secret，只返回 configured bool；
- `ProcessCredentialBoundary` 若被 provider SDK/client边界复用，只接收该 logical operation已从
  settings读取的 exact value，不观察 product环境变量，也不成为 store；
- add/delete/replace都是单个完整 document mutation；commit-unknown exact reread完整目标值，
  不增加 durable recovery machinery。

### 13.4 `llm/models.py`、`llm/config.py`

- `ModelProfile` 不再从 provider profile复制 support bool；
- 删除 `ModelRole`、`ModelProfile.role`、`LLMConfig.pro/flash`、`pro_model/flash_model` aliases、
  `model_for(role)` 与 `slot_for(role)`；
- 不新增全局 `LLMConfig.model`；model target/endpoint来自per-session selected
  `ModelConnectionConfig`，API key由adapter open时按其connection ID读取；
- hard limits从 frozen models.dev entry取得；
- connection config保留 ID/target/endpoint与 nullable user-declared facts；output/safety policy按
  §4.7 确定性派生；
- `config-check` 逐条完成 local settings/key-configured/catalog validation，但零配置不阻止 app启动；
- hard-cut 删除旧 thinking/support env路径、global model/API key env、全部 PRO/FLASH、retrieval、
  PostgreSQL product-config env与 display-only compatibility alias。
- `src/pulsara_agent/llm/retry.py` 删除 `retry_config_from_env()`；
  `src/pulsara_agent/llm/adapters/openai/retrying.py` / adapter wiring删除 SDK retry env override，保留
  §9.7 冻结的既有 provider-neutral physical retry与 semantic-output barrier。

### 13.5 `llm/request.py`、`llm/resolution.py`、`primitives/model_call.py`、`model_input/compiler.py`

- raw `reasoning_effort` 改为 closed `ReasoningSelection`；
- resolver 不再接收 role，直接 exact join唯一 route/wire/model/adapter/limits/selection；
- `ResolvedModelCallFact` 只携带一个 frozen `ModelCallBinding`；provider
  credential lookup与 request builder不得回读 session current selection；
- `ResolvedModelTargetFact` contract version hard-cut bump；
- `canonicalize_endpoint()` 输出并冻结包含完整 base path的 `canonical_endpoint_base_url`；target fact
  以它取代 origin-only字段，target digest直接消费该字符串，existing replay endpoint digest仍只在
  自己的真实边界使用；
- 删除 durable/frozen fact 中的 `model_role`，target identity 只由真实 target contract构成；
- target fact携带恢复执行所需的 closed fields但不把 catalog/UI control domain塞进 compatibility
  digest，也不携带本次 selection；删除 `provider_request_shape_fingerprint` child字段，让真正的
  provider-wire compatibility consumer直接对 frozen adapter contract计算；删除只为 selected reasoning存在的
  `ResolvedModelOptionsFact` / options fingerprint，不新增 fingerprint字段；
- `rebind_model_target()` exact重现 target；call恢复另从 turn binding装回 connection + selection；
- tools存在时验证 catalog tool fact与 route/wire tool lowering；明确 `false`只拒绝该次含 tools
  call，unknown允许一次正常用户请求并保留诊断，二者都不拒绝无 tools call或整条 saved connection；
- invalid selection在 provider open前 typed reject；某一 selector family没有本地 lowering时只
  不暴露该 family，缺少整个 route/wire adapter才使 target non-executable。
- `_compatibility_reset_reason()` 将 assistant-replay compatibility差异映射到既有
  `PROVIDER_LOWERING_CHANGED`，不新增 reset reason。

### 13.6 Chat / Responses adapters

- payload builder只消费 typed resolved reasoning selection；
- route/wire adapter唯一 materialize exact wire shape；
- final context-bearing materializer继续与 ordinary send共享；
- Chat accumulator改为 §8 tracker，删除 dense/origin path；
- Responses保留 existing exact item state machine；
- Responses message 的 `phase` 接纳标准值 `commentary` / `final_answer`（也可缺省），由
  已完成 output item 确定并按原值 native replay；`output_item.added` 是进行中快照，其
  `phase` 可在 `item.done` 改变，不参与 type/id/role 的身份比较。`item.done` 与 terminal
  output 若都给出 phase 仍必须相等，沿用既有 terminal 字段省略规则。SDK 负责 SSE 解码；
  通用 adapter 负责上述完成值核对，不按 provider 名称分支。phase 仅是 provider 输出标注，
  不新增 Pulsara turn 状态或自动继续路径；仍由既有 terminal/tool-call settlement 决定后续。
  单条可选 message 位于 function calls 之前的 canonical 顺序限制保持不变，completed-output
  source contract 升至 v5。2026-09-15 DeepSeek 实测可在工具调用前从 `final_answer` 变为
  `commentary`，此前将 phase 视为身份及只允许 final_answer 的限制在此删除；
- terminal/retry/usage/replay行为不变。

### 13.7 Kernel、auxiliary、CLI 与 Web 的 per-session single-connection wiring

至少修改：

```text
src/pulsara_agent/conversation_kernel/direct_model.py
src/pulsara_agent/conversation_kernel/auxiliary_model.py
src/pulsara_agent/conversation_kernel/host.py
src/pulsara_agent/web_app/application.py
src/pulsara_agent/web_app/session_controller.py
src/pulsara_agent/web_app/http_server.py
src/pulsara_agent/settings.py
src/pulsara_agent/cli.py
src/pulsara_agent/storage/migrations/runner.py
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
- CLI 删除 `--model-role`、`.env` loader/flags与 global product env config；db命令读取 saved
  local PostgreSQL config；bootstrap/read model删除 `pro_model/flash_model`，改为 non-secret
  local-settings/data-plane状态与 `model_connections` collection；
- `LocalWebApplication` 先发布 database-independent settings shell，再按 runtime DSN可用性发布
  Kernel/Session data plane；不能在 HTTP start前强制 `sessions.prepare()`；
- Local Web application唯一持有并向 settings/Host共享 `ModelCatalogOwner` 与
  `LocalSettingsStore` mutation owner；catalog refresh只原子发布 process-local immutable pointer，
  local-settings mutation只在 owner锁内 reread/merge/write；
- 设置页删除“主要模型/轻量模型”两行与单行“当前模型”，显示 model cards + 添加流程、固定
  DashScope embedding/reranker credential rows与 PostgreSQL card；
- app无任何 product configuration也能启动；database data plane未 ready或 provider dispatch没有
  selected connection时分别 typed拒绝对应操作，而非关闭整个 app；
- PostgreSQL save/check/migrate是 closed endpoints；普通启动/保存不自动 migrate或 reset；
- 不新增 `AUXILIARY`、`DEFAULT` 等替代 role enum，也不保留单值 `PRO` enum。

### 13.8 Session model-call binding、prompt admission 与 PostgreSQL baseline

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

- baseline 直接 hard-cut 在 sessions、prompt_queue_items、turns各增加一个 §9.6
  `model_call_binding` 字段；本地 disposable PostgreSQL reset，不写线上迁移
  或 dual-read；
- 提供 controller-owned session binding mutation API；running/queued工作不阻止修改下一条输入的
  binding；mutation与 NEW_TURN admission锁同一 session row，admission取得 canonical binding；
- session snapshot/connect payload带 current binding与 non-secret connection summaries；
- 所有 NEW_TURN command carry/freeze一个 binding，STEER不改当前 turn；
- 既有 prompt command digest、confirmation 与 queue consumption纳入同一个 binding；不增加
  binding fingerprint；
- Host先在 transaction外取得 frozen process-local config/catalog snapshots；queue item可在事务外
  pure resolve，direct admission则在锁内取得 canonical binding后只对这些已冻结内存值做 pure
  validation，不在 transaction内读 filesystem/network/settings；queue -> turn在唯一 transaction中
  复制同一 ID/value；API key只在 commit后的 provider open读取一次；subagent/continuation exact继承；
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

前端只消费 local Web/kernel投影，不直接二次 parse models.dev JSON。Settings显示配置 cards、
目录四字段入口与 closed自定义入口；不保存会话 reasoning默认，不在 React持久状态、browser
storage或 response中保留 API key；DashScope secret fields只能 replace/clear且
永不回填；PostgreSQL card在 database-not-ready时仍可操作。对话输入框提供 saved-connection
selector与唯一 reasoning selector，selector mutation先更新 session canonical binding，NEW_TURN
admission再冻结该值。增加 §9.3 的 closed local-settings/catalog/list/add/delete/session endpoints；
所有 key相关 response均只返回 configured bool，所有 data-plane unavailable
状态均使用 typed read model。显式测试与保存使用同一个 closed request shape，但测试不发布任何配置。

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
.env.example（删除）
```

---

## 14. 实施顺序

1. 先写 `.env` loader/全部 production env owner删除、single-document local-settings mutation owner、
   model/DashScope key fields、exact model deletion、zero-database Web bootstrap与 PostgreSQL GUI flow
   的 failing tests。
2. 写 models.dev recorded fixture parser、256k/Claude/Gemini filter、exact selection、
   shape-warning、model-connection store、request golden与 binding-freeze concurrency tests。
3. 新增 direct catalog client、`ModelCatalogSnapshot`、`SelectableModelCatalog` 与 closed
   reasoning contracts。
4. 用至少两个“同 model、不同 route、不同 choices”的 fixture证明 identity；另覆盖
   shape present/absent。
5. 实现 `route + user-selected wire API + model` exact resolver，不建 per-model local table。
6. 实现统一 local-settings v2 document及 process-local mutation owner；model key与两枚 retrieval
   key随同一 closed document原子保存，embedding/reranker继续使用 fixed DashScope contracts；删除
   Keychain模块、依赖与双存储 settlement。
7. hard-cut config/models/resolution/facts，删除 PRO/FLASH、global env target、role参数/fact、
   bool/raw/omit-default旧路径。
8. hard-cut CLI、Host/Web wiring与 bootstrap：先启设置壳，再可选发布 PostgreSQL-backed data plane；
   settings与Host共享 process-local catalog owner，每个 session/turn绑定一条 connection。
9. 让 Chat/Responses adapter消费 typed selection并删除 generic reasoning owner。
10. 用 generic tracker取代 Chat dense/origin index accumulator。
11. hard-cut baseline、session binding API、prompt command/queue/turn freeze 与 exact
    confirmation；reset 已核验的本地 disposable PostgreSQL。
12. 接入 local-settings/catalog read model、设置页 model cards/add/delete flow、DashScope key rows、
    PostgreSQL card，以及 composer model/reasoning selector；删除后的 stale session保持原 ID并要求
    用户显式重选。
13. 实现 catalog unavailable、filter reason、wire shape mismatch warning、database-not-ready与
    “修改连接配置”错误入口。
14. 跑 terminal/retry/replay/prefix/final-wire focused regressions。
15. 更新 config-check、db CLI、README与架构说明，删除 `.env.example`，只描述新路径。
16. 运行 PostgreSQL schema/replay regression，确认 event/subject/guard/relation/job oracle
    类别没有增加。
17. 对通用 Chat/Responses dialect选取有可用 credential的代表 target运行 real-provider dogfood；
    无凭据的代表 route精确报告环境阻塞。

不得保留旧的 local model table等待以后切换。Production有 models.dev与显式 user-declared两种
target fact source，但只有一条 resolved target / generic adapter执行路径。

---

## 15. Required test matrix

### 15.1 Universe identity 与 validation

- direct GET 成功解析完整 models.dev fixture 为一份 immutable snapshot；
- provider/model outer keys 与 inner IDs 不一致时记录对应 row diagnostic；outer identity仍明确的
  其他合法 rows继续可用，不令整个 snapshot失败；
- `limit.context=255999` 不进入 selectable catalog，`256000` exact进入；filter只读
  `limit.context`，不误用 input/output；
- raw `claude-*` / `gemini-*`、`anthropic/claude-*`、`google/gemini-*`、
  `~anthropic/claude-*`、`~Claude-*` 与 `provider/~GeMiNi-*` 均被 model-ID leaf predicate排除；
  `~~claude-*` 只移除一个 `~` 后不命中，证明实现没有 strip-all；大小写 predicate结果一致但
  raw ID保持不变；
- provider/family/display name/shape 不触发额外模型过滤；过滤后无模型的 provider不显示；
- 通过唯一产品 filter但某个 reasoning option未知/非法或 adapter不支持该 family时，合法 sibling
  controls继续可选；没有可用 selector时降级 provider-default，connection仍可保存；
- frontend、CLI与 config-check消费同一 filtered read model，不各自计算阈值或 startswith；
- exact `route + wire API + model` 命中；
- route相同但 wire不同不误命中；
- model相同但 route不同不误命中；
- unknown model不做 substring/family fallback；
- 新的 `@ai-sdk/openai-compatible` route不经手工注册即可分别选择通用 Chat/Responses；provider-native/
  unknown dialect始终 target non-executable、添加 connection拒绝，catalog row仍可见；
- catalog option kind未知或某 family缺少 route/wire lowering时只不暴露该 family，不拒绝其他
  controls/provider-default；
- invalid/contradictory limits只拒绝对应 row；
- tools存在且 `model.tool_call=false` 时在 open前拒绝该 call；unknown按 adapter正常发送并保留
  diagnostic，不自动无 tools重试；无 tools call不受影响；
- `model.provider.api` 正确覆盖 provider-level endpoint 预填值；
- `provider.shape=responses|completions` 正确投影为 hint，缺失为 neutral；
- user-declared target要求 context/output/tool/reasoning/auth的 closed完整形状，context `255999`
  被拒绝、`256000` exact接受；它不进入 catalog，也不允许 env/raw JSON/header绕过；
- user-declared target在 catalog owner无 snapshot时仍可解析，并与 catalog target复用同一
  `ModelTargetContract`、budget derivation及 Chat/Responses adapter；
- catalog fetch不携带 API key/prompt/workspace数据；
- snapshot取得后 request lookup不读网络、不写数据库、不产生 event/job；
- fetch/top-level JSON结构失败产生 typed `model_catalog_unavailable/invalid`，不回落本地旧表；
  单 row/option schema drift只产生局部 diagnostic。
- Local Web settings、connection validator、Host admission与 queue consumer读取同一个
  `ModelCatalogOwner` pointer；成功 refresh原子发布新 immutable snapshot，失败保留现有成功值；
- owner从无 snapshot时 queue保持 `PENDING`，显式 refresh成功后普通调度可继续；不创建 durable
  wakeup/event/job/retry counter；
- 新 connection针对 owner当前 snapshot保存后 Host立即可解析；refresh不改变 active epoch/turn的
  frozen target；
- stale session choice只在 controller持有既有 session row lock的 snapshot/mutation/admission路径
  重算并保存；admission冻结修订值，发生写回的响应带提示，不持久化 notice bit。

### 15.2 Exact reasoning selection

- effort choices按 target exact展示；
- provider真实 `minimal/auto/adaptive` 作为 catalog raw value 保留，不建全局 rank；
- provider真实 `null/none` 按 models.dev contract 显示为 disabled；
- `toggle + effort`、`toggle + budget`、`effort + budget` 与三者组合均完整保留；
- 三种 family作为替代 selection一次只能选择一个；不得把 toggle、effort与budget拼成一个请求；
- effort含 `null/none` 又声明 toggle时保留两个 exact关闭入口；选择任一只发送该 selection定义的
  wire control，不使 target不可执行；
- explicit choice必须 exact membership；
- `{low, high}` target 收到 `medium` 必须拒绝，不能投影到任一档；
- OpenRouter GLM catalog fixture保留 toggle + `high/xhigh` + budget原始事实；通用 adapter解析后的
  可执行选择只保留其支持的 `high/xhigh` effort；direct Z.AI fixture只接受 `high/max`；
- 同名 choice不能跨 target复用旧 selection；
- `[low, high, max]` 默认 `high`，`[high, max]` 默认 `max`，五个正向档默认正中；
- disabled choice不参与正向 effort 的中位计算，只有 disabled 时默认 disabled；
- 默认 family优先级 exact为 positive effort > closed budget > toggle > sole disabled effort >
  provider-default；
- closed budget range使用向上取整的数值中点；开放边界原样保留但不渲染数值控件、不从 output
  limit推导，仍可使用同 row的 effort/toggle，只有开放 budget时用 provider-default；
- target切换清除不适用 choice并计算新 target自己的上中位默认，不迁移同名值；
- 同 target preference仍合法时不因 catalog顺序变化而重算；
- catalog parser保留toggle/budget事实；当前通用Chat adapter明确支持 toggle并通过 SDK
  `extra_body` lower为最终 HTTP root `reasoning={"enabled":false|true}`，Responses与两种通用
  adapter尚未实现的budget不进入可执行
  菜单；
- fixed-on exact omission；
- unavailable正常执行且不发送 selector；
- provider-default正常执行、不发送 selector且不宣称实际 reasoning state；
- token budget低于/高于 range拒绝；
- budget不自动生成 `high/max` 名称；
- provider open次数在所有 validation failure下均为 0。
- 本 turn selection或 catalog control-domain列表改变只改变 call/UI value，不改变 target
  compatibility digest，也不触发 `MODEL_TARGET_CHANGED`；route/wire/model、endpoint、transport
  exact变化改变 target digest；provider message/native-tool lowering、assistant replay或 estimator
  分别只改变 `ProviderInputEpochCompatibility` 的既有独立槽位并触发不兼容，不能同时改变 target
  digest。测试对 `resolved-model-target-compatibility:v5` exact payload作 golden断言。
- `https://api.example.test/v1` 与 `https://api.example.test/api/v4` 这两个同 scheme/authority、不同
  canonical base path的 endpoint得到不同 target digest；target payload含直接 base URL source
  string而不是 `endpoint_fingerprint`。
- `ResolvedModelTargetFact` 不再含 `provider_request_shape_fingerprint`；既有 provider-wire profile
  compatibility digest在其唯一真实 consumer builder中直接使用 frozen adapter fields；测试只冻结
  该真实边界的 replay/final-wire incompatibility行为，不把旧 child digest字节当外部产品输出保留。

### 15.3 Request golden 与 owner

- OpenAI Chat effort exact root field；
- OpenAI Responses effort exact nested field；
- 不同OpenAI-compatible route在同一Chat/Responses选择下使用相同effort request shape；
- fixed-on / no caller control 的 omission exact；
- unavailable/provider-default exact omission；
- effort disabled choice发送 contract声明的 exact value；
- generic defaults/extra body不能覆盖 reasoning-owned key；
- output cap、store、stream、usage与 existing request fields不漂移；
- reasoning control不出现在 context-bearing final-wire projection；
- canonical数据库 transaction内不读 local-settings文件；prepared call只在 commit后的 provider
  open按 frozen connection ID读取一次 key，不回读 session current value；同一 logical call的全部
  transparent physical retries复用该值，测试断言 settings read次数为 1；
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
- runtime固定使用现有 `LLMRetryConfig()` defaults，OpenAI SDK retry为 0；所有旧
  `PULSARA_LLM_RETRY_*` / `PULSARA_OPENAI_SDK_MAX_RETRIES` process env均不改变 production行为；
- semantic event后 transport error不 retry；
- unsupported reasoning choice不发请求、更不降档 retry；
- incomplete/unknown terminal不 retry；
- Chat actual reasoning fields仍 exact replay；
- Responses ordered reasoning/message/function_call仍 exact replay；
- Responses 多段 reasoning summary逐个 exact join其 `summary_index`，并在live与terminal replay
  projection中保留独立展示块和公开顺序；
- reasoning choice改变不翻译历史 carrier；
- wire API/model/endpoint/adapter contract/replay contract变化仍走 cold semantic continuation。

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
- 仅 assistant replay contract变化时，target digest保持，
  `_compatibility_reset_reason()` 返回既有 `PROVIDER_LOWERING_CHANGED`并建立 cold successor；没有
  reset reason的 incompatible append不得发生。

### 15.7 UI / config-check

- 设置页显示零到多张 saved model-connection cards与“添加配置”，不存在单行全局“当前模型”；
- 目录添加流程只要求 provider、model ID、API key、Chat/Responses；endpoint按 exact precedence
  自动解析并在确认页展示；自定义入口完整显示配置名称、Base URL、Model ID、API、认证、context、
  output、tool calling与 reasoning control；
- route selector只投影仍有 selectable model 的 direct/gateway entries；model列表只来自
  selected provider 的 filtered entries；
- context/Claude/Gemini filter的 UI fixture与 kernel projection exact一致；
- Chat/Responses 始终是用户选择，不被 catalog 强制；
- shape match 显示推荐，shape mismatch 只 warning 仍可保存，shape absent 不警告；
- 所有确认页均显示“只支持 OpenAI Chat Completions / Responses compatible endpoint”说明；
- 保存配置不打开 provider；独立测试发送一次有界极短请求，失败不禁止保存且不发布配置；保存
  成功后 key input清空，Bearer read model只显示 `credential_configured=true`，no-auth为 false且
  单独显示“无需认证”；任何 read response都不返回 secret；
- 模型卡片删除需行内确认；DELETE按 exact ID幂等，同一次 document replace移除 metadata + key，
  保留其他模型、PostgreSQL与DashScope字段；引用该 ID的会话不自动改绑，composer显示重新选择引导；
- reasoning列表与 exact contract一致；
- effort/budget同时存在时单个推理菜单分组展示两种替代 control，三种合法组合及默认优先级均
  与 kernel read model一致；开放 budget不出现伪数值输入；局部未知/非法 option只显示非阻塞
  诊断并省略该 control family，不阻止使用合法 sibling或 provider-default保存；
- toggle有真实开关，fixed-on/unavailable/provider-default没有伪开关；
- connection config页面不存在 reasoning default控件；
- 模型页显示两个 fixed DashScope rows，只允许填写/替换/清除各自 key，不显示 provider/model/
  endpoint selector；response与重载后的 input都只显示 typed state而非 secret；
- 本地服务页显示 runtime/admin DSN、database data-plane状态、保存/检查/显式迁移动作；保存不连接，
  打开页面不 migrate/reset；
- app在零 product configuration时仍启动 settings shell；database未 ready时 session endpoints明确
  unavailable，database ready但无模型配置时可浏览 session且 send不可执行，并均有准确设置入口；
- composer model selector只列 saved connections；未选择时不暗选第一条；
- 新 session选择 connection后显示 exact target上中位默认；用户修改后跨 turn、刷新、Host重启与控制窗口接管
  保持，直到再次修改；
- session busy或有 pending NEW_TURN时仍可修改下一条输入的 connection；既有 work bindings不变，
  不会取消/重排工作；
- switch更新 session binding与新 target reasoning默认，首条使用新 connection的后续 dispatch创建
  new cold epoch；
- 两个 session并行选择不同 connections时各自 exact dispatch，互不覆盖；
- stale session choice按新 target contract重置默认并提示，不做 projection；
- send 时冻结，running turn不热改；steer不改变当前 turn；
- 两条排队 prompt之间修改 preference时，每条最终使用各自提交时的值；
- 不支持的 user-selected API 失败后显示原错误与修改配置入口，不自动 fallback；
- headless与 UI config走同一 validation；
- secret不进入 diagnostics、universe或 client projection；
- legacy supports/thinking/raw effort字段不存在。

### 15.8 Local settings、API key 与 Session / queue / turn storage

- `local-settings.yaml` v2的 nullable PostgreSQL config、ordered catalog/user-declared connection + key、
  两枚DashScope key、stable random ID、closed codec、no-follow、0700/0600与atomic replace均通过；文件
  可含自定义 target声明的 reasoning control domain，但不含会话 reasoning preference/default、
  fingerprint、revision或 catalog副本；
- Bearer model、DashScope embedding与DashScope rerank API key只进入该文件；PostgreSQL、Web response、
  browser storage、log与 diagnostics逐字扫描均不含 exact secret；no-auth record的 key exact为 null；
- production不存在 Keychain/keyring/credential-store backend、fake、fallback或迁移；v1与旧Keychain
  item不读取，用户重新填写；
- 同一 `LocalSettingsStore` mutation owner并发执行 add/add、add/delete与add/PostgreSQL-save时不
  lost update；cancellation必须join单次document settlement；before-replace、after-replace与
  parent-directory-fsync failure injection均覆盖；commit-unknown exact reread完整目标document，且
  不增加 revision/receipt/repair job；
- delete exact ID同时移除 record + key并保持siblings/PostgreSQL/DashScope；重复delete幂等；不改
  session/queue/turn binding，已open operation继续，下一次读取观察缺失；
- embedding key缺失只禁用 dense channel，rerank key缺失只使用 pre-rerank结果；二者互不借用、
  不阻止 app/turn/主模型；
- replace/clear不改变已开始 retrieval operation已取得的key及其 bounded physical retries，下一
  operation取得新值/缺失状态；
  provider client不跨 operation持有旧 key，且没有 key generation/fingerprint/event；
- PostgreSQL未配置、runtime不可达、schema需动作三种状态都不阻止设置 HTTP/UI；admin DSN缺失只
  阻止显式 migrate/reset，普通 runtime verify不使用 admin DSN；
- local-settings损坏时 app仍发布可修复的设置壳；保存新 DSN不热换正在运行的 repository，且不
  建 pending row/event/job；无 data plane时验证后可一次性发布 kernel；
- sessions、prompt_queue_items、turns各只增加一个 `model_call_binding` JSONB；binding只含
  connection ID + selection/null，不重复 target，不新增 PostgreSQL settings/profile子表；
- binding不新增 fingerprint、revision、generation或 companion列；
- controller binding mutation成功后 snapshot/reconnect读回同一 value；
- binding mutation与 NEW_TURN admission锁同一 session row；model-switch-vs-send、
  preference-change-vs-send竞态由该现有锁串行化，每次 admission都按 lock顺序冻结当时的 canonical
  binding，不增加 stale-composer rejection path；
- NEW_TURN queue row保存提交时的完整 binding，STEER该列为 null；
- direct ROOT admission与 queued ROOT consumption都把同一 binding写入 turn；
- queue consumption不回读 sessions当前值；
- 既有 command semantic digest payload包含 admission冻结的 binding；同 command ID 的 idempotent
  replay继续确认原 binding且不回读 session新值，但没有新增 binding digest字段；
- queue confirmation与 turn confirmation直接比较 binding；
- subagent、Plan/terminal continuation继承 origin/parent turn binding，新的 user NEW_TURN由
  admission读取 canonical session binding；
- binding中的 connection ID不伪造到 filesystem config的数据库 FK；metadata缺失、hard limits
  permanent invalid或整个 route/wire adapter缺失使 pending queue复用现有 `REJECTED` settlement且
  不创建 turn；局部 catalog option diagnostic不拒绝；暂时没有 catalog
  snapshot保持 `PENDING`；配置/key缺失只在 commit后 provider open终止已接纳 turn；各路径均不
  重复读取key；
- reasoning preference change不产生 transcript entry或新 prefix root；connection switch只走
  approved new cold epoch；两者都不新增 agent event/job/checkpoint；
- clean-v0 schema与 expected catalog更新，event/subject/guard/relation/job oracle计数不增加。

### 15.9 Connection 与 PRO/FLASH hard cut

- 不存在 global `LLMConfig.model`、`pro`、`flash`、`model_for(role)` 或 `slot_for(role)`；
- `ModelRole` 类型、`ModelProfile.role` 与 `ResolvedModelTargetFact.model_role` 均不存在；
- production不读取 `PULSARA_API_KEY` / `PULSARA_MODEL` / `PULSARA_PRO_*` /
  `PULSARA_FLASH_*` / `PULSARA_EMBEDDING_*` / `PULSARA_RERANK_*` /
  `PULSARA_POSTGRES_*` / `PULSARA_REQUEST_DEFAULTS_JSON` / `PULSARA_EXTRA_BODY_JSON` /
  `PULSARA_OMIT_PARAMS_WHEN_THINKING` / `PULSARA_SUPPORTS_*` /
  `PULSARA_MODEL_IDENTITY_POLICY` / `PULSARA_THINKING_*` /
  `PULSARA_OPENAI_SDK_MAX_RETRIES` / `PULSARA_LLM_RETRY_*`，也不做 import、alias或 fallback；
- `PulsaraSettings.from_env*`、`load_env_file`、CLI `--env-file` / `--override-env` / product
  `--prefix`、`--model-role` 与 `.env.example` 已删除；
- 同一 session/turn 的 main、subagent、summary、compaction 与 memory governance resolve 出同一
  `ModelConnectionId` + `ModelTargetKey`；不同 sessions可不同；
- auxiliary 局部 output cap不修改 base model config，也不创建第二个 model slot；
- auxiliary purpose/deadline/input/output bounds仍各自生效；
- bootstrap只返回 non-secret connection collection；设置页不存在“主要模型/轻量模型”；
- tests/support builders注入包含connection/key的 `LocalSettingsStore`，不用相同值伪造双 slot；
- process credential boundary取得caller为本operation读取的exact secret，不回读global environment；
- production 与 active docs 中不存在 `ModelRole.FLASH`、`PULSARA_FLASH_*`、
  `flash_model` 或 display-only compatibility alias。

---

## 16. 验证命令与 real-provider dogfood

实施完成后至少运行：

```bash
uv run pytest -q tests/test_llm_model_catalog.py tests/test_llm_model_target.py
uv run pytest -q tests/test_llm_model_connections.py tests/test_retrieval_credentials.py
uv run pytest -q tests/test_settings.py
uv run pytest -q tests/test_local_web_http_surface.py -k 'settings or database or zero_config'
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
4. 一个 gateway target（优先 OpenRouter）：models.dev effort choice经用户所选通用 wire API形成
   对应Chat/Responses request；
5. 同一 model family经 direct/gateway暴露不同 choices时，两条请求各自只发送所选真实值；
6. 至少覆盖 fixed-on/unavailable/provider-default中的一个 no-effort-list target；无
   credential时精确报告该 target环境阻塞；
7. provider返回 usage/reasoning telemetry不改变 selection；
8. 同一 session connection下至少实际触发一个 main call 与一个 auxiliary call，证实二者使用
   同一 route/wire/model target，而各自 output bound/deadline仍生效；
9. 若可用 credential允许，创建 direct/gateway两组 connections并让两个 sessions各发送一轮，
   证明配置不会串线；缺少第二凭据时精确报告环境阻塞；
10. 从无 product config启动 local Web，实机打开设置页，保存并检查已核验的本机 disposable
    PostgreSQL；证明 HTTP/settings shell在数据库配置前可用，data plane验证后可进入 ready；
11. 若两枚 DashScope key可用，通过 GUI write-only endpoints配置后各执行一次真实 embedding/
    rerank并验证缺少任一枚时的独立退化；不可用时精确报告环境阻塞；
12. Real-provider dogfood通过临时 `PULSARA_HOME`或用户明确配置的GUI路径写入v2 local settings；
    不输出key，结束后删除临时目录。不得恢复production env读取、in-memory credential-store fake或
    Keychain旁路。
13. 对临时 local settings完成 model add → configured read model → provider open → delete → missing
    smoke；response/log不含sentinel，文件权限保持0700/0600，finally删除临时目录。

每个 production wire dialect必须有 official wire evidence、request golden与 recorded stream
fixture/normalized contract。Live dogfood覆盖当前可用 credential，并至少尽力覆盖一个
direct Chat、一个 direct Responses与一个 gateway target；没有某 route凭据时精确报告环境阻塞，
不伪称该 route live通过，但也不把“测试者没有所有厂商 key”提升为代码 activation gate。
models.dev新增 model不要求 Pulsara为每条 row重跑 conformance；用户所选 target的实际 protocol
失败按 §6.2 处理。

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
    `PULSARA_FLASH_*`、`PULSARA_{EMBEDDING,RERANK,POSTGRES}_*` 作为 production config；
28. 前端再次 parse models.dev 并自行实现 256k、Claude/Gemini 或 shape filter；
29. 用 provider name、family、display name过滤 Claude/Gemini，或只检查完整 raw ID开头而漏掉
    `google/gemini-*` / `anthropic/claude-*`；
30. 把任一 model/embedding/rerank API key写入 `${PULSARA_HOME}/local-settings.yaml` 之外的
    metadata sidecar、PostgreSQL、localStorage、日志、response，或提供 reveal API；
31. 将 saved connection列表第一项静默当作所有新会话的全局默认；
32. running/pending工作存在时热换 connection、取消旧队列或在同一 epoch 改 endpoint/model/API；
33. 配置 ID 原地改成另一个 target，或让 auxiliary call选择另一配置；
34. 为实现 model-connection add/list/select/delete 顺手增加未定义语义的 connection edit/rotate UI，
    或删除时扫描/重绑已有 session、queue、turn；
35. 把完整 `ModelTargetContract`、reasoning control domain、display metadata或 selection再次 hash进
    target compatibility digest；
36. 在现有 transport binding之外新增 request/reasoning codec ID registry或 profile fingerprint；
37. 因一个 catalog row、未知 optional字段或单个 reasoning option异常而拒绝整份 snapshot；
38. 为 connection + target + selection建立重复数据库列、binding fingerprint或 PostgreSQL profile表；
39. 仅为了简化竞态而在 running/pending工作期间禁止用户修改下一条输入的模型选择。
40. 让 user-declared target携带 field override、arbitrary wire DSL/header模板、adapter代码，或把它
    实现为第二套 transport/resolution状态机。
41. 保留 `.env` parser、`--env-file`、production `from_env()` 或 local-settings/env双读迁移期；
42. 为 PostgreSQL/DashScope设置建立 PostgreSQL表、event、job、receipt、revision、watcher或
    local-settings fingerprint；
43. 让 `sessions.prepare()`、PostgreSQL connect/schema failure继续阻止 static HTTP与设置页启动；
44. 因 embedding/reranker key缺失或远端失败而阻止 app、主模型、用户 turn或 lexical memory path；
45. 保存 PostgreSQL DSN时自动 migrate/reset、热换正在运行的 repository，或为此增加
    busy/no-pending gate与 pending-config authority。
46. 保留 Keychain/keyring/credential-store、明文sidecar或环境变量作为第二secret owner、迁移源、
    fallback或测试专用production分支；
47. 在API响应中返回 key、masked key、配置文件片段或persistent secret reference；
48. settings、Host与 queue各自维护互不相知的 catalog snapshot，或用数据库
    fingerprint/generation协调它们；
49. local-settings publish发生 commit-unknown时凭猜测报告成功/失败，或为解决它增加durable
    receipt/repair worker，而不是exact reread完整目标document；
50. 只删除显眼的 model/key env，却保留 request defaults、thinking、support、model identity或
    retry env从 production旁路改变 adapter行为。

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
7. `ModelRole`、PRO/FLASH slots、`.env`/production product-config env、CLI/fact/Web旧投影与
   compatibility aliases全部不存在；
8. Raw catalog经过唯一 kernel predicate，只向产品暴露 `limit.context >= 256000` 且 model ID
   leaf不以 Claude/Gemini 开头的 rows；前端不重复过滤；
9. 设置页以 cards/add/delete flow管理配置；目录流程只需 provider、model ID、API key、Chat/Responses，
   自定义流程收集 §9.3 的 closed user assertions；会话 composer从已保存配置中选择一条；
10. 所有 API key只持久化在 `${PULSARA_HOME}/local-settings.yaml` v2；Bearer model key与对应
    connection record同存，no-auth exact为null，embedding/reranker使用两个fixed nullable字段；
    PostgreSQL与client projections只含connection ID/configured bool，不提供read-back；
11. UI/CLI只展示 exact catalog entry 的真实 reasoning controls，完整保留组合
   toggle/effort/budget；effort/budget是替代 selection family；局部未知/非法 family被诊断并
   省略，合法 sibling或 provider-default仍可执行；
12. `provider.shape` 只产生推荐/不匹配 warning，不强制 API；缺失时保持中性；确认页始终说明
    只支持 OpenAI Chat Completions / Responses compatible endpoint；
13. Reasoning唯一入口是会话输入框；选择 connection时按其 exact target真实选项取上中位默认，
    用户更改后保持到再次修改，每条 NEW_TURN admission冻结 connection + selection；
14. Connection可随时修改为下一条尚未 admission输入的默认；既有 queue/turn不变，首条使用新
   connection的后续 dispatch建立 new cold epoch；
15. 不存在 global five-rung projection、nearest/tie-up或跨 target choice迁移；
16. 每个 provider call在 open前已解析为 optional effort/toggle/budget control；`None` 的具体
    fixed-on/unavailable/provider-default语义只由 target contract持有，不复制成 call union；
17. `supports_reasoning` bool、raw passthrough、把 unproved omission解释成具体 reasoning状态，
    以及 reasoning extra-body旧路径全部删除；
18. Model contract只投影当前执行需要的 models.dev 最小字段；
19. Route/wire adapter唯一拥有 exact request、stream、terminal与 replay shape；
20. Chat tool-call parser支持 arbitrary non-negative/sparse/reused/missing index，并在歧义时
    fail closed；
21. semantic-output retry barrier与 exact prepared-request retry保持；
22. Chat/Responses terminal、reasoning replay、atomic assistant acceptance没有回归；
23. prefix continuity与 final-wire estimation契约没有回归；
24. reasoning selection与 catalog control-domain只属于 call/UI，不进入 target compatibility
    digest；同 target改 selection或可选菜单不重建 epoch，真实 route/wire/model、endpoint、
    transport变化改变唯一 target digest；endpoint source包含 canonical full base path，同源异路径不
    相等；message/native-tool lowering、assistant replay与 estimator
    由 `ProviderInputEpochCompatibility` 既有独立槽位比较，变化仍不兼容且不被 target digest重复 hash；
25. connection只存 ID/target/endpoint与 nullable closed user-declared facts，output/safety policy
    由 hard limits确定性派生；目录流程没有隐藏字段，自定义流程不开放 arbitrary wire配置；
26. NEW_TURN admission与 session binding mutation由现有 session row lock串行化；admission取得
    canonical binding且不增加 stale-composer rejection path；数据库 transaction内不访问
    filesystem/settings，API key只在 commit后的 provider open读取一次；
27. catalog client没有新增 durable cache authority；PostgreSQL只在 sessions/queue/turn各增加
    一个最小 `model_call_binding` 字段，event/subject/guard/relation/job oracle类别无变化；
28. models.dev与显式 user-declared是两种 typed fact source，但共享单一 target
    validation/resolution路径；custom不依赖 catalog snapshot，也不形成第二套 registry；
29. focused、full、PostgreSQL与可用 real-provider dogfood通过；
30. 每个 production-supported wire dialect都有 official evidence与 golden/fixture；所有
    可用 credential执行代表 live smoke，缺失凭据逐 route报告而不伪称通过；
31. 外部阻塞逐 target精确报告，API key从未输出或落入 single local-settings之外的 persistence；
32. production、tests、README与 config-check只描述新路径，无 compatibility alias、feature flag
    或双读写；
33. `${PULSARA_HOME}/local-settings.yaml` 是 model-connection metadata + key、两枚DashScope key与
    本机PostgreSQL DSN的单一closed authority，不含fingerprint、revision、catalog副本或arbitrary KV；
34. 设置页可配置多组主模型、两枚 fixed DashScope retrieval key与 runtime/optional admin DSN；
    embedding/reranker没有伪 provider/model selector，admin DSN只用于显式 migration 或确认后的 reset；
35. static HTTP/settings shell在 PostgreSQL未配置、不可达或 schema需动作时仍可启动；data plane
    状态真实可见，不伪造空 session；
36. 缺少 embedding/reranker key只触发各自 advisory degradation，不阻止 app、turn、主模型或
    lexical memory path；
37. 本轮没有新增 config/catalog/credential/binding fingerprint、PostgreSQL settings table、
    event、job、checkpoint、receipt、watcher或 total-lifetime cap。
38. 同一进程所有 local-settings mutation由一个 owner串行化并在锁内 reread；单document写入的
    cancellation/commit-unknown按 §9.2精确结算，不增加跨进程事务、revision或 repair authority；
39. Settings、connection validation、Host admission与 queue consumption共享一个 disposable
    process-local catalog owner；成功 refresh只影响未来 admission，active epoch不 rebase，且没有
    durable cache/event/job；
40. model与两枚retrieval key的configured/missing语义、closed字段映射、exact delete/replace以及
    logical-operation单次读取均由tests证明；
41. `retry_config_from_env()`、SDK retry override及 request/thinking/support hidden env owner全部删除；
    既有 physical retry defaults与 semantic-output retry barrier保持，未变成 GUI或新 total cap。
42. assistant replay contract独立变化复用既有 `PROVIDER_LOWERING_CHANGED` cold-reset reason；没有新增
    enum，且不存在 compatibility已变化却无法建立 cold successor的断路。

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
