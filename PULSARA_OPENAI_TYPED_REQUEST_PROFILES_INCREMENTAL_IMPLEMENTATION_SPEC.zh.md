# Pulsara OpenAI 两协议 Typed Request Profiles 增量实施规格

状态：实施稿  
适用范围：OpenAI-compatible Chat Completions 与 Responses 两条 adapter

## 1. 目标与边界

Pulsara 继续只实现两种 wire protocol：Chat Completions 与 Responses。第三方服务的差异不按供应商名称或模型名称分支，而是由每一条模型连接显式持有一个封闭、可验证、可冻结的请求 profile。models.dev 连接与自定义连接共用完全相同的 profile 类型、兼容性校验、target resolution 和 wire lowerer。

profile 是 Pulsara 自己的产品类型。运行时不得读取模型 ID 正则、供应商名称、错误消息或探测结果来改写 profile；调用失败必须如实返回，不得静默改字段重试。外部资料只用于设计阶段归纳常见请求形状，不进入产品来源、配置或运行时依赖。

协议仍拥有固定字段：Chat 使用 `max_completion_tokens`，Responses 使用 `max_output_tokens` 并保持 `store: false`。本增量只泛化 reasoning 请求形状。

profile 只描述“字段如何落到请求 JSON”；models.dev 或用户声明的 reasoning capability 只描述“模型允许怎样控制”。两者不得混为一层，也不得由 profile 反向伪造模型能力。

## 2. 封闭 profile 集合

连接内部的 `reasoning_wire_profile` 是 profile ID；它不是 provider hint。自定义连接在本机设置文件中仍将该值与用户声明的 reasoning 控制一起编码为一个封闭对象，但解析后必须提升为连接级 typed value，不能由自定义 target 独占。

### 2.1 两协议共有

- `provider_default`：不发送调用方 reasoning 字段，也不向会话暴露 reasoning 选择器。
- `effort`：用户必须声明非空、去重后的可选值列表；会话按列表选择一个值。
  - Chat：根字段 `reasoning_effort: <value>`。
  - Responses：根字段 `reasoning: {effort: <value>, summary: "auto"}`。
- `toggle`：会话选择开或关。
  - Chat：HTTP JSON 根形状 `reasoning: {enabled: <bool>}`；Python SDK 可用 `extra_body` 承载，但 `extra_body` 不是产品语义。
  - Responses：`reasoning: {effort: "high" | "none", summary: "auto"}`。

### 2.2 仅 Chat

- `enable_thinking`：`enable_thinking: <bool>`。
- `thinking_type`：`thinking: {type: "enabled" | "disabled"}`。
- `thinking_effort`：用户声明 effort 列表。选择 `none` 或 `disabled` 时只发送 `thinking: {type: "disabled"}`；其他值发送 `thinking: {type: "enabled"}` 与 `reasoning_effort: <value>`。选择 `enabled` 时 effort 归一为 `high`。
- `broad_compat`：用户声明 effort 列表，同时发送一组明确的兼容字段：
  - `thinking: {type: "enabled" | "disabled"}`；
  - `enable_thinking: <bool>`；
  - `reasoning_effort: <normalized>`；
  - `reasoning: {effort: <normalized>}`。
  `none`/`disabled` 归一为 `none`，`enabled` 归一为 `high`，其他值原样发送。

`broad_compat` 是用户显式承担严格服务端可能拒绝未知字段这一代价的 profile；它绝不是默认值或失败后的 fallback。

### 2.3 固定开启与可选择控制

同一个 profile 可以根据连接的 capability 产生不同的产品控制语义，但 wire 形状不变：

- capability 是可选择 toggle 时，会话显示开关，lowerer 按用户选择发送 enabled 或 disabled；
- capability 是固定开启时，会话不显示开关，支持固定开启的 profile 直接发送启用形状；
- 例如 `thinking_type` 配合可选择 toggle 时发送用户所选的 `enabled`/`disabled`；配合固定开启模型时始终发送 `thinking: {type: "enabled"}`。不为后者新增 `thinking_type_enabled` 之类重复 profile。

当前允许固定开启复用的 profile 为 Chat 的 `toggle`、`enable_thinking`、`thinking_type`，以及 Responses 的 `toggle`。其他 effort 组合不能凭空为固定开启模型创造 effort 选项。

## 3. 所有连接共用 profile

models.dev 目录只提供 endpoint、模型限制与能力事实，不决定第三方私有字段。设置页在用户选定模型与协议后，根据“目录能力 × 该协议已注册 profile”展示兼容 profile；用户明确选择，默认值为 `catalog_standard`。不得读取 route ID、provider 名称或 model ID 来自动选择 `thinking_type` 等形状。

`catalog_standard` 保留为目录连接的默认标准映射：Chat 对目录声明的 effort/toggle 做标准映射；Responses 对 effort/toggle 做标准映射；固定开启、不可用或只知 provider default 时不额外发送 reasoning 字段。用户也可以选择兼容的其他 typed profile；该选择保存到同一条连接，并经过与自定义连接相同的 resolution、冻结和 payload builder。

自定义连接必须保存上述显式 profile。`provider_default`、`effort`、`toggle` 是当前正式 profile，不是历史别名。Chat-only profile 若用于 Responses，配置在保存和解析边界直接失败。

兼容性必须在保存、测试与运行时 resolution 的共同边界验证。目录能力与 profile 不匹配时连接不可执行；不得等到 provider 报错后再猜另一形状。

## 4. 冻结、连续性与可观测性

- 选定 profile 是 resolved model target 的完整 typed value，并写入目标事实；不得以 hash 代替。
- profile 与 lowerer 在冷 epoch 或显式采用的 compaction successor 上冻结。既有 epoch 内配置变化不得重写已安装的 SYSTEM、tools 或历史 messages 前缀。
- 测试连接与真实调用必须经过同一 target resolution、同一 profile lowerer 和同一 adapter payload builder。
- profile 字段属于冻结调用目标，不属于对话正文；不得为了 profile 变化重建 provider-visible prefix。
- usage、缓存命中与错误仍由现有 adapter 归一化。profile 不承诺服务端支持，只承诺 Pulsara 发送的形状确定且可检查。

## 5. 设置页语义

models.dev 与自定义服务的设置页都展示 profile。目录路径只显示与目录 capability 兼容的选项；自定义路径按所选协议展示可用 profile，并用业务语言说明实际请求形状与风险。只有自定义 effort 类 profile 显示 effort 列表输入。Responses 不展示 Chat-only profile。

保存前必须完成：profile/协议/能力匹配、effort 列表非空、值为规范非空文本。页面不得根据 Model ID 自动选择 profile；连接测试也不得自动修正选择。

## 6. 验收

- 每个公开 profile 都有 payload 精确测试，覆盖根字段、SDK `extra_body` 承载和禁用/启用归一化。
- models.dev 固定开启模型可显式选择 `thinking_type`，会话无开关且请求固定发送 enabled；目录路径的兼容 profile 列表与保存 payload 有测试。
- closed settings codec、HTTP closed body、连接测试、target fact、epoch freeze 与 rebind 漂移检查均覆盖 profile。
- 前端覆盖协议切换、profile 选项过滤、effort 输入显隐与提交 payload。
- 现有目录连接、Chat/Responses tool calling、reasoning replay、prefix continuity 与真实 provider 错误透明性测试保持通过。
