# Pulsara Kernel 图片输入与 OpenAI 通用 Wire Adapter 修订设计

> 后续扩展（2026-09-16，设计待实施）：本地 `view_image` 的 typed 工具结果、并行执行、跨协议统一附件投影及左侧展示，见 [本地图片工具实施规范](PULSARA_LOCAL_IMAGE_TOOL_IMPLEMENTATION_SPEC.zh.md)。本篇已经验收的用户图片输入行为保持不变；工具图片新增范围以该实施规范为准，D1/D2 的冻结参数继续适用。

> 状态：**D1–D4 设计已冻结；K1–K3 已实施，K4 于 2026-09-15 验收通过；U1/U2 于 2026-09-15 实施并完成浏览器验收**
>
> 修订：2026-09-12，补齐 pre-adoption 内容校验、ROOT 父上下文、typed ingress / Hook 与 multipart headroom 边界；纳入本地 Codex 源码对照。
>
> 补充：2026-09-13，吸收 OpenCode 调研中可用的输入比较、协议 lowering 与验证分层；明确计量单位和 exact request carrier 边界。此处为此前修订记录，当前状态见文首。
>
> D1 冻结：2026-09-13，经用户确认，将第 9 节的 28 网格 × 7/8、最低 256 算法作为正式设计契约。参数、误差取舍及适用性限制保持不变。
>
> D2 冻结基线：2026-09-13，用户本轮明确要求以“冻结的 D1、D2”为准。沿用此前与 gpt-5.6-sol / max critic 收敛的第 9.3 节，不改变常数、G/R、输入/输出检查或 U_W/U_T；此前“最终冻结候选”的状态在此更新为冻结。资源依据仍为原 666 次实验及另列的 5 次 PNG 单位补测。
>
> D3/D4 冻结：2026-09-13，依据 external_agent_report.zh.md、当前生产代码与既有规范完成第 6、8、13 节，经主 agent 与指定 gpt-5.6-sol / max critic 多轮交叉核验，无剩余设计冻结阻塞。以冻结的 D1/D2 为基线，不改其算法和参数。外部报告不是代码权威，其项目调查结论未在本轮全部独立复验。
>
> UI 补充：2026-09-13，用户确认首版采用图文混排输入框。第 14 节确定编辑、完整提交、草稿、排队编辑与历史展示契约，消费已冻结的 D1–D4；本次没有修改 kernel 算法或实施生产功能。
>
> 模型切换简化：2026-09-14，用户明确采用原三级 compact：纯文本目标遇到有效历史图片时进入 Tier 2，由原模型总结；按现有规则选出的最近最多三条原话没有图片就保留，含图片才将 recent 置空。原模型不可用或 Tier 2 未形成合法 successor 时沿原 Tier 3，由新模型读取 Image → Text("[图片已省略]") 的历史投影，该档纯文本图片交接仍为 recent=0。模型选择隐含接受该后果，不设双模式、独立许可、专用省略 part 或永久通知状态。本次取代 2026-09-13 critic 版本的相关扩展；D1/D2 不变。
>
> 设计精简：2026-09-14，按用户确认统一新 snapshot/projection 的展示结构；提交 FULL 确认只核对完整正文、引用及 blob 身份/元数据，图片 payload 完整性在发布与读取时验证；将图片省略的禁止范围限定到普通调用、adapter 与透明重试。D1/D2 算法、参数与接纳规则不变。
>
> UI 显示冻结：2026-09-15，用户确认紧凑缩略图输入框、排队项的原位置 `[Figure x]` 蓝色链接，以及“上方缩略图带＋正文 Figure 链接”的已发送气泡。Figure 编号按每条消息的图片 occurrence 顺序在前端生成，不额外持久化、不进入模型输入；第 14 节替换旧的历史图片必须嵌在正文原位置的要求。随后用户删除添加图片、撤销和重做三个可见按钮，图片只从粘贴或文件拖入进入，撤销/重做保留编辑器标准快捷键。U1/U2 已按该最终界面实施，D1–D4 不变。
>
> 实施进度：K1 已落地 input modality 事实与 exact target 冻结、单一 typed content、共享 shape/target 校验、完整内容身份、D1 视觉公式、D2 headroom 常量以及 `pulsara.prompt/v1` codec/occurrence/metadata quote。K2 已将同一内容接入 Host direct/queued/steer、Hook/Skill 文本投影、Pillow 隔离验证、caller-owned transaction publication、`canonical_image_refs` clean-v0/GC、FULL exact-confirm、hydration、typed snapshot/fork、compiler/lowering 与 ROOT parent-context。K3 已接通 Chat/Responses 两套正式图片 wire、单次 final materialization、`pulsara_heuristic/v2`、PRE_FULL/POST_FULL、输入 G 与效果前输出 R/U_W/U_T 检查、ordinary recent 收缩，以及纯文本图片交接的 Tier 1/2/3、P 与 cold 入口。K4 已完成 I01–I44 核验、1979 项完整 non-live 回归（含 PostgreSQL）及修复后 isolated wheel 的 Chat/Responses 18 次真实调用，详见 [K4 验收记录](PULSARA_KERNEL_IMAGE_INPUT_K4_ACCEPTANCE.zh.md)。U1/U2 已完成 Tiptap 草稿、typed browser/Protocol-v3 hard cut、队列与历史图片读取、Figure 展示和两套真实浏览器 provider 链路；验收范围与限制见 [U1/U2 验收记录](PULSARA_KERNEL_IMAGE_INPUT_U1_U2_ACCEPTANCE.zh.md)。
>
> 范围：kernel 的模型输入、模型事实、wire lowering、预算与连续性，以及第 14 节的首版图文输入和展示。相机、图片加工、分享下载等扩展行为仍另议。
>
> 本文冻结 kernel 设计边界；设计闭合不等于生产激活，完成第 12 节前不标记 `ACTIVATED`。

## 1. 目标与权威关系

Pulsara 继续只实现 OpenAI-compatible 的两套通用 adapter：**Chat Completions** 与 **Responses**。图片是这两套协议中的输入内容，不构成第三套 adapter，也不按 provider 名称分叉。

本轮明确 kernel 如何接纳、确认、表达、验证、冻结、计量并发送图片。**typed kernel 提交接口及其 Hook/Skill 投影属于本设计范围**；用户随后确认的图片粘贴、拖入、光标位置插入和完整发送见第 14 节。图片只来自用户显式拖入或粘贴的内容；不会自动读取屏幕、目录或剪贴板，也不自动发送尚未提交的草稿。

以下既有约束继续生效：

- [AGENTS.md](AGENTS.md)：provider 前缀连续性、小范围 durability、依赖复用、完整 hard cut。
- [Route / Wire API / Model Universe 规范](PULSARA_ROUTE_WIRE_API_MODEL_UNIVERSE_AND_ADAPTER_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)：exact target、models.dev 单一 catalog、两套通用 adapter、非预算事实的 unknown 语义。
- [Structured Model Input Compiler 规范](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)：canonical source、纯编译与 source attribution。
- [Provider Input Prefix Continuity 规范](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)及当前 `AGENTS.md`：同一 epoch 内保持 root 和 tools 不变，messages 只追加后缀。
- [Final-Wire 本地估算规范](PULSARA_COMPACTION_FINAL_WIRE_ESTIMATION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)：统一本地 estimator、最终 materialization 的 quote、普通发送与 compaction 共用计量。
- [模型切换与 Compaction 规范](PULSARA_MODEL_SWITCH_HANDOVER_COMPACTION_AND_RECENT_WINDOW_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)、[Fork 规范](PULSARA_CONVERSATION_FORK_EFFECTIVE_CONTEXT_COPY_SPEC.zh.md)、[Fingerprint Subtraction 规范](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)：保留各自的来源、复制、边界与身份契约。

用户当前要求优先。本文不授权改写上述规范中的其他产品语义；待实施时，只针对闭合后的图片输入边界更新相关规范和生产代码。

## 2. 已核实的现状

### 2.1 models.dev 有输入模态，没有完整图片传输契约

当前 Pulsara 使用 [models.dev 的 provider-scoped API](https://models.dev/api.json)。在 `route.models[model_id]` 上可读取：

```json
{
  "attachment": true,
  "modalities": {
    "input": ["text", "image", "pdf"],
    "output": ["text"]
  }
}
```

上例只展示字段形状。判断图片输入声明应检查 `modalities.input` 是否包含 `image`。`attachment` 是更宽泛的附件标记，不能替代图片模态，也不能作为图片模态的额外 AND 条件。字段定义参见 [models.dev 元数据说明](https://github.com/anomalyco/models.dev#adding-model-metadata)。

2026-09-12 使用仓库 `ModelsDevCatalogClient._fetch_http()` 读取的快照包含 213 个 route、7,754 个 model entry。其中，175 条记录是 `attachment=true` 且没有 image input，177 条记录是 `attachment=false` 且有 image input。这是本轮调查时的观察，不是需要维护的模型名单或运行时断言。

该快照没有提供图片 MIME 白名单、单图字节/像素限制、请求图片数量限制、`detail` 支持范围、图片 token 算法，或按 provider 定义的图片 JSON 编码规则。`provider.shape` 仍只是 `responses` / `completions` 的 wire 提示，不能解释成图片传输 profile。

使用同一份 provider-scoped entry 中的事实；不额外读取 base-model 数据建立第二套合并 authority，也不通过模型名称补全这些缺失字段。

### 2.2 当前代码没有贯通图片输入

| 当前 owner | 已有行为 | 本次需要修订的边界 |
| --- | --- | --- |
| [model_catalog.py](src/pulsara_agent/llm/model_catalog.py) 的 `ModelCatalogEntry` / `_parse_model_entry()` | 读取 limits、reasoning、tool_call、shape hint；丢弃 modalities / attachment | 保留 input modalities；output modalities 仅供配置页展示，不进入 frozen target |
| [model_target.py](src/pulsara_agent/llm/model_target.py) 的 `ModelTargetFacts` / `RouteWireRegistry` | 按 `(wire_dialect, wire_api)` 选择 adapter，并冻结 exact target | 将模态事实沿原解析路径冻结；不改 adapter 选择原则 |
| [host.py](src/pulsara_agent/conversation_kernel/host.py) 的 `submit_prompt()` / `run_turn()` / `_IngressHookReservationKey` | 接收、校验和在途比较都使用文本；Hook 收到解码后的文本 | 统一 typed ingress、完整内容命令身份、独立文本投影；纯图合法 |
| [steer.py](src/pulsara_agent/conversation_kernel/steer.py) / [prompts.py](src/pulsara_agent/conversation_kernel/_repository/prompts.py) | prompt candidate 从文本字节构建；exact-confirm 硬编码 `text/plain` / `utf-8` | 全内容身份、真实内容编码及只读 exact-confirm；保留 NEW_TURN / STEER 的独立语义 |
| [input.py](src/pulsara_agent/llm/input.py) 的 `LLMMessage` | `content: tuple[str, ...]` | 改为单一、有序、typed content vocabulary |
| [validation.py](src/pulsara_agent/llm/validation.py) | USER content 只能有一个字符串 | 改为验证合法内容 part、role 与目标输入模态 |
| [contracts.py](src/pulsara_agent/model_input/contracts.py) / [lowering.py](src/pulsara_agent/model_input/lowering.py) | `FrozenProviderInputItem.text` 降低为文本 message | 保留 canonical 来源的同时传递有序图片内容 |
| [Chat adapter](src/pulsara_agent/llm/adapters/openai/chat_completions.py) / [Responses adapter](src/pulsara_agent/llm/adapters/openai/responses.py) | 当前消息内容均走字符串投影 | 各自在已有 semantic wire group 中编码图片 |
| [direct_model.py](src/pulsara_agent/conversation_kernel/direct_model.py) | 先构造并冻结最终 wire plan，payload builder 消费该 plan | 图片必须在 plan 冻结前进入相同路径 |
| [compaction/coordinator.py](src/pulsara_agent/conversation_kernel/compaction/coordinator.py) 的两个 wire transition validator | 验证 target join、token、wire bytes；未完整验证 successor 内容/目标形状 | 普通压缩与模型切换均在 canonical adoption 前执行共享内容校验 |
| [provider_dispatch.py](src/pulsara_agent/conversation_kernel/provider_dispatch.py) / [subagents/contracts.py](src/pulsara_agent/conversation_kernel/subagents/contracts.py) | 每次 ROOT install 后生成父上下文；公开内容直接 `join(message.content)` | install 前完成可失败的 typed 内容投影；保留来源、顺序及明确的图片缺失标记 |
| [compaction/contracts.py](src/pulsara_agent/conversation_kernel/compaction/contracts.py) / [reader.py](src/pulsara_agent/conversation_kernel/reader.py) | headroom 从文本 prompt、assistant、ToolResult 推导；reader 通过内容大小预检 | multipart、图片 hydration、epoch 和最大单次接纳增量共同决定资源预留 |
| [estimator.py](src/pulsara_agent/llm/estimator.py) | 文本及 JSON 字符数启发式估算 | 区分图片内容、文本 token 估算和 wire 字节数 |

因此，修改最后的 `build_chat_completions_payload()` / `build_responses_payload()` 并不足以支持图片：生产调用到达它们时，context-bearing projection 已经冻结。

## 3. 三个独立问题，分别由已有 owner 负责

| 问题 | 依据与 owner | 不能推导的结论 |
| --- | --- | --- |
| 该 route/model 是否声明图片输入 | models.dev entry → `ModelTargetFacts` | 有 `image` 不等于某 endpoint 已通过图片实测 |
| 当前请求使用哪套 JSON 形状 | 显式选择的 `wire_api` → 两套通用 adapter | `npm`、模型名称、shape hint 不自动切换 wire API |
| 请求是否满足本地预算及物理边界 | 统一 estimator、compiler、final-wire admission | 相同 JSON 形状不等于各服务有相同 token 成本或图片限制 |

“OpenAI compatible”提供可复用的标准请求形状，不能保证每家服务实现了全部可选字段和相同的资源边界。本设计统一标准形状；对不接受该形状的 endpoint，保留真实失败，不在失败后猜测另一套编码。

允许按明确的 wire API、typed part 和字段形状分支。禁止按 route ID、provider 名称、模型名称、family、URL 子串或错误文案选择图片处理路径；禁止维护 provider 图片 profile 表、自动探测缓存或学习出来的兼容补丁。

## 4. 模态事实、调用 preflight 与 pre-adoption admission

沿 `ModelCatalogEntry → ModelTargetFacts → resolved/frozen target → call preflight` 传递：

```python
input_modalities: tuple[str, ...] | None
```

这是字段形状提案，最终命名随现有类型风格收敛。只保留这一份完整模态事实，不再持久保存冗余的 `supports_image` 布尔值。模型事实不新增名为 `ModelCapability` 的体系；`capability` 继续保留给 Agent Tool、MCP、Skill、Plugin。

解析及使用规则：

1. 合法 input 字符串列表表示已知声明；缺失或形状非法表示 `None`，附局部 diagnostic。不能根据 output 或 attachment 补全 input。
2. 未识别但形状合法的模态名称保留为数据，不因此拒绝整个 catalog。非法成员不能通过静默删除，被误读成“完整列表中没有 image”。
3. 已知 input 列表不含 `image`：实际请求或待接纳 successor 含图片时必须拒绝；普通调用在 continuity install / provider open 前拒绝，successor 在 canonical adoption 前拒绝。纯文本调用继续执行，不隐藏模型或禁用整条 connection。
4. input 未知：保持 unknown。若内容、wire shape 与预算均有效，可按已有非预算事实的规则发送正常请求并保留诊断；不把 unknown 写成 supported，也不发送额外 probe。
5. `attachment` 不参与图片 admission；当前没有独立消费者时，无需为它新增 production 字段。
6. 2026-09-15 配置页模态展示修订：`output_modalities` 由 models.dev 的原 `modalities.output` 进入现有 catalog DTO 与 Web 展示；缺失/非法仍为 unknown，合法未知名称保留。它只说明所选 provider/model 的输出声明，不进入 production target facts、输入 admission、fingerprint 或 response parser，不新增缓存/持久化。配置页分别显示输入和输出模态，并说明 Pulsara 当前支持文字、图片输入及文字回复。

不改变现有 catalog 产品过滤规则。2026-09-15 用户确认：自定义配置新增默认不勾选的“支持图像输入”复选框。新 Web 提交显式携带 `input_modalities`：勾选为 `("text", "image")`，不勾选为 `("text",)`；测试连接与保存使用同一候选。该值由既有 closed `UserDeclaredModelTarget` 和 LocalSettingsStore 持有，直接进入原 frozen target 与 preflight，不持久化冗余布尔值或覆盖表。未声明的本地自定义 target 仍可用 `None` 表示 unknown，不推断其能力，不改写用户已保存配置。目录配置按 provider/model 原记录显示全部模态；模型一经选中即可查看，已保存卡片也展示输入/输出声明（自定义仅展示所声明的输入）。

仍由现有 process-local catalog owner 在原生命周期内刷新。请求期间不联网刷新 catalog，不用新快照改写已经冻结的 target、预算绑定或已安装前缀。

### 4.1 Successor 必须先证明内容可执行，再提交采用

复用 [validation.py](src/pulsara_agent/llm/validation.py) 的 `validate_model_context_shape_for_call()` 所拥有的内容、role、target 和 transport binding 校验，或从该函数收敛出唯一共享纯校验。它**不带 semantic token gate**；不能直接调用带该 gate 的 `validate_model_context_for_call()`，把 semantic estimate 重新提升为 final-wire admission authority。

`validate_compaction_wire_transition()` 与 `validate_model_switch_wire_transition()` 的 `PRE_FULL` 路径，均须对 dry successor 的实际内容和冻结目标调用这一校验，再联合既有 exact join、final-wire token、wire byte、reclaim 规则作出决定。它必须覆盖 active / idle、同目标压缩及 A→B 模型切换，不能以“idle 现在不打开 provider”为由省略。

该过程不安装 dry successor，不打开 provider，不触发额外 Hook 或工具。A 支持图片、B 明确不支持且 successor 保留图片时，失败必须发生在 `_settle_compaction_adoption()` 之前：canonical context binding、采用的 snapshot 指针、turn 的 context pointer 和旧 continuity epoch nonce/revision/prefix 均不推进。临时规划资源按原 owner 释放。

模态不兼容是内容/目标拒绝，不能包装成预算不足来触发任意删图或 tail-shrink。切换到明确纯文本目标时，第 8.6 节的固定三级规则负责交接：有有效历史图片即进入 Tier 2，由原模型总结；原规则选中的 recent 窗口无图则保留，有图则整体置空。必要时 Tier 3 使用第 8.9 节的普通文本替换。选择该模型即接受这些固定后果，不新增省略许可或第二套模式；普通同 epoch 调用及 adapter 不取得改写图片的权限。

Tier 1/2 的图片不兼容按第 8.6 节转入下一档，不能为了绕过校验而搜索“保留部分图片”的候选。完整性错误、非法源、取消及 source drift 继续按原分类处理，不冒充模型不可用或图片兼容问题。无旧 epoch 时按第 8.9 节从同一 canonical 来源进入既有 destination summary/adoption。

`POST_FULL` 对真实重建结果继续复验相同内容契约和 final-wire admission；它不能替代 `PRE_FULL`。采用后发生其他失效时沿现有 post-adoption failure 结算，不新增回滚或恢复框架。

## 5. Kernel 的内容模型

在现有 `LLMMessage.content` 上形成唯一的有序 part 序列，最小 vocabulary 为：

```text
LLMTextPart(text)
LLMImagePart(media_type, immutable_bytes, width, height)
LLMMessage.content = tuple[LLMTextPart | LLMImagePart, ...]
```

这是 provider-neutral 输入值，不拥有执行生命周期、上传状态或持久化 authority。图片字节必须在进入 frozen input 前完成来源解析和必要验证；adapter 不读取本地路径、不下载 URL、不查询数据库，也不根据模型名称转换图片。

width/height 随同一份经 D2 验证的完整 Image 值从 ingress/canonical hydration 贯穿 compiler、semantic groups 与 materialization；不在转换为 LLMImagePart 时丢弃。公开 ingress 不接受调用方自报尺寸，内部也不能用另传的尺寸字典替代该 frozen 值。宽高仅供内容校验与 D1，本版实际 wire 不新增这两个字段，L 仍按冻结 D2 计量。

本版以**已冻结字节的 inline 图片输入**作为 wire 基线。外部 URL、provider file ID、Files API 上传以及 provider 管理的媒体生命周期，不纳入本版。`data:` URL 是 adapter 的确定性编码结果，不作为 kernel 的第二份内容真相。

本篇已实施的第一版 role 范围限定为 USER 中的图片输入；SYSTEM、assistant thinking、tool call arguments 和现有文本 ToolResult 保持原角色及内容语义。后续本地工具图片的输出与来源契约由 [本地图片工具实施规范](PULSARA_LOCAL_IMAGE_TOOL_IMPLEMENTATION_SPEC.zh.md) 明确：canonical 归属 TOOL_RESULT，两种协议均在公共编译层派生 user role 图片附件；不创建 canonical 用户提交，UI 仍展示为左侧工具结果。该扩展尚待实施，不能用本篇已有验收记录作为其通过证据。

具体约束：

- 支持 USER 中有序的文本与图片 part；图片可以单独出现，不要求伪造占位文本。保留原 part 顺序和图片次数，不排序、不去重。
- `thinking` 与 tool-only 字段继续使用各自现有 closed shape；新增图片不放松 role validation。
- 文本构造器如 `LLMMessage.user(text)` 可保留为创建 text part 的便利函数；`content` 本身不同时接受旧字符串和新 part 两种内部表达。
- 普通纯文本消息的最终 wire 字符串、换行、空内容及 omission 行为保持当前结果；全部为 Text 时沿现有 adapter 规则用一个 `\n` 连接各 part（单 Text 不变）。含图片的普通消息按 part 原顺序输出真实 parts，不插入连接换行或图片说明；新 snapshot/projection 统一采用第 8.5 节展示结构，其实际包装正常计量。
- 不同时维护 `content`、`attachments`、`image_urls` 三份可独立变更的内容。
- 本版不提供跨服务“画质档位”产品参数。基线请求使用标准 `auto` detail；这只确定发送字段，不声称各服务的自动处理结果或成本相同。
- MIME、解码有效性和每次操作的物理边界按第 9.3 节冻结契约执行。不根据文件后缀或客户端自报尺寸直接判定有效。

D2 的验证顺序应是：在已有单次字节边界内取得输入，先做实际字节的格式识别及 MIME 一致性检查，再通过维护中的库有界解码，取得尺寸、帧及内存信息；完成完整 multipart 的资源检查后，才冻结提交候选并进入原 Hook / canonical admission。文件读取入口若后续采用 sample sniff，sample 只能提前拒绝或选择验证器，不能代替完整读取上限和完整内容验证。首版不接纳的动画、多帧或格式应明确拒绝，不自动 resize、转码或取第一帧。

这里的“解码”必须区分 **base64 解码后的压缩图片文件字节**与**图像解码后的像素/帧内存**。前者的字节上限不能证明后者有界；D2 要说明所选库在大尺寸、损坏数据、多帧和取消时的资源边界及清理行为。MIME 来自验证后的实际格式，不在 Hook 或命令冻结后再次修改内容。

## 6. Canonical source 与 compiler 接入

### 6.1 最小 typed kernel 提交与完整命令身份

在现有 Host owner 上把 `submit_prompt(..., text: str, ...)`、`run_turn(..., text: str, ...)` 一次性改为 `content: PromptContent`。它使用第 5 节相同的 Text / Image 语义；不是第二套通用多模态 IR。准确值边界如下：

```text
PromptContent.parts = tuple[Text(text: str) | Image(original_bytes: bytes, declared_mime: str), ...]
  -- 原 Host 的 D2 验证与冻结 -->
FrozenPromptContent.parts = tuple[Text(text) | Image(media_type, immutable_bytes, width, height), ...]
  -- 原 canonical content owner 的唯一编码 -->
CanonicalPromptBody = canonical_json_bytes({"schema": "pulsara.prompt/v1", "parts": [...]})
```

Text 保留每个原值和边界；Image 的尺寸和 MIME 只能由同一输入字节的 D2 验证产生。引用正文与 hydrated 值是同一内容的持久化/读取表示，不允许两个独立可变的 parts authority。非文本请求的 adapter 直接消费完整 frozen parts；非 USER 来源仍受第 8.3 节的 kind/origin 联合约束。

- 提交者提供文本及已取得的图片字节；kernel 在 Hook reservation / 命令候选冻结前完成 D2 要求的内容验证与冻结。Host 不接收待发送时再解析的可变本地路径或远程 URL。
- 空 part 序列、仅含空文本且无图片的内容非法；有合法图片的纯图内容合法。文本 part 继续执行既有控制字符与文本资源校验，不能因 Hook 文本为空而拒绝纯图。
- `run_turn()`、queued `NEW_TURN`、`STEER_ACTIVE_TURN` 及队列重定向/消费走相同内容契约，保留各自原有 command、target、permission、Hook 时机与 settlement 语义。不新建一条图片专用 runner，也不让 steer 额外跑一次 NEW_TURN Hook。
- 现有文本调用者在边界构造一个 Text part；kernel 内不同时保留 `text` 与 `content` 双入口。UI 仍可以只发文本，这只是调用适配，不要求本轮提供上传产品。

`command_id`、session/turn/queue 身份与内容分离，但同一 command 的兼容性必须比较**完整提交语义**：有序 part 类型、每个文本原值、图片不可变内容及其验证后的 MIME、所有影响输入的既定参数，以及既有 delivery / target / permission / model binding 规则。本版 detail 固定为 `auto`，不能在重试时另选 detail。相同文字但图片字节、图片顺序或出现次数不同，都不是兼容重试。

`_IngressHookReservationKey` 必须携带完整冻结值并按 exact equality 判定；不能继续用文本投影作为在途 join 身份。原 `PreparedPromptIngressCommand`、`build_prompt_ingress_command()`、`prompt_ingress_semantic_digest()` 及 repository confirmation 同步 hard cut。已有跨重启命令语义 digest 和不可变 blob digest 可以服务原边界；不另加 image/DTO fingerprint、receipt 或提交注册表。

Canonical publication 与 command/queue 的 full confirmation 必须覆盖完整 ordered body 及其不可变引用和真实 media type / codec。只读 `confirm_prompt_ingress()` 不能只比较文本、metadata 总大小，或继续硬编码整个内容为 `text/plain` / `utf-8`；完整比较与引用核验按第 6.5、6.8 节执行。

原提交 owner 在写入结果不确定后，通过既有只读 exact-confirm 判断 FULL / NONE / CONFLICT；不能重调 writer 代替查询。已 FULL 的 queued 相同命令沿原语义返回原结果、重发 wake hint，不重复 Hook、入队或执行；direct 调用保留其“已接纳后查询原 outcome”的区别，不扩展成执行重放。Hook 拒绝、资源失败和取消不得把半份内容当成已接纳 prompt，孤立 blob 按既有 GC 规则处理。

### 6.2 Hook 与 Skill/MCP 使用明确的文本投影

定义唯一纯文本投影：按原 part 顺序取 Text part，以一个 `\n` 分隔，保留每个 part 的原始文本，不 trim、不 OCR、不读取 image bytes。只有一个 Text part 时结果与当前 `prompt` 完全相同；纯图投影为 `""`。图片的文件名、data URL、MIME 和缺失标记都不进入这一文本投影。

`UserPromptSubmitInput.prompt: str` 本版沿用该投影，Hook 的 session/turn/permission/cwd/model 身份及 allow/block/effect 规则保持原样。Hook 仍会按原 ingress 时机观察纯图提交，只是 `prompt` 为空；kernel 的内容非空判定不能复用 Hook prompt。Hook schema 本版不获得图片检查能力，也不声称已检查图片内容。

`_activation_subject_for_anchor()` 及 Skill/MCP 的文本匹配只从相同 canonical input 的 Text parts 取得文本；mention 不跨 part 边界拼接识别。纯图仍保留 HUMAN_MESSAGE / HUMAN_STEER 的 origin 和 trigger anchor，只是文本匹配输入为空；不能改成 NON_HUMAN，也不因此清空 configured/default active skills 或上一次无新 trigger 的 activation 语义。图片本身不激活新的 Skill/MCP。若现有 matcher 需要 part 边界信息，由原 matcher 接受有序文本输入，不另建识别框架。

Hook/Skill 的派生文本不参与完整命令身份，不替代 provider 输入或 canonical body。父上下文的缺失标记使用第 8.1 节独立的 advisory projection，不能回流进 Hook/Skill 匹配。

K2 同步迁移现有 memory 文本消费者：human 正文经同一 prompt codec/引用核验取得 Text，typed snapshot 使用统一 lowering 后的文本展示；不能把 descriptor JSON 当用户原话，也不能让纯文本 snapshot 因严格字符串 accessor 而失败。图片不自动附加到 memory auxiliary call；未呈现的图片通过原 `truncated`、omitted 和 source coverage 字段表达，证据不足仍沿原 memory owner 的结算规则，不新增图片描述、治理状态或恢复路径。

### 6.3 单一 canonical-to-wire 链路

目标生产链路是：

```text
typed kernel ingress（完整内容身份；独立 Hook/Skill 文本投影）
  → 现有 command / queue / canonical source owner
  → 解析并验证引用内容，冻结有序 typed parts
  → FrozenProviderInputItem（保留 entry / sequence / turn / origin）
  → structured model-input compiler / lowering
  → ProviderWireSemanticInput
  → 当前 wire_api 的 semantic wire groups
  → 现有 native replay replacement
  → FrozenProviderWireMaterialization + final-wire quote
  → 现有 continuity install / provider dispatch
```

图片不能绕过 canonical source attribution，也不能由 UI、HTTP builder 或 transport 临时添加到编译结果。修订 `FrozenProviderInputItem.text` 时，应在该 owner 中形成唯一内容表达，不新增与原 source 平行的“多模态上下文管理器”。现有文本 origin wrapper、tool result context 和 call correlation 必须保持各自用途。

`FrozenProviderInputItem` 使用单一 `content` 字段并由现有 item_kind 判别：CONTEXT_SNAPSHOT 携带 typed carrier；其余 message-like 项携带同一有序 Text/Image 值，按原 role/origin 约束合法部分。本版只有 HUMAN_MESSAGE/HUMAN_STEER 的 USER 及 carrier 中机械保留的这些请求可含图；其他项限制为现有文本/空内容形状，tool_calls、ToolResult context/delivery 等既有 closed 字段保留。不得另保留可独立修改的 `text` 与 `content`；确需字符串的 PLAN/terminal/ToolResult decoder 从严格 text-only 内容取得原值，复用原 kind-specific renderer，不改变原结构化内容的协议。

K2 实现中，hydrated `CompactionSnapshotCarrier` 本身就是 CONTEXT_SNAPSHOT item 的 `content`；reader、compaction read/source view 与 compiler 均从该 item 取得同一对象，normal lowering 才生成 provider USER parts。不存在另行传递的 carrier sink/sidecar 或预渲染 snapshot content。

carrier 的 canonical expanded charge 从正文与逐 occurrence 图片字节派生；compiler 仍须另外计入实际展示超过该 charge 的部分，包含分段标签及 `display_json` 转义增量。不能把整个 lowered message 当作已经计费的 canonical 正文，从而漏掉展示开销。

现有 [PostgresCanonicalBlobStore](src/pulsara_agent/conversation_kernel/blob.py) 已提供不可变字节、真实内容完整性验证和 exact read。若 canonical 图片需要落库，应复用此 owner；现有内容 digest 跨越真实不可变内容边界，可以保留，不新增 image DTO fingerprint 或图片注册表。

多 part 的 canonical body、引用归属、事务边界、hydration 与 GC 可达性统一按第 6.5–6.9 节执行。不能把 blob ID 塞进任意 JSON 后假定当前 GC 已认识这些引用，也不能用 process-local map 弥补应有的 canonical 内容。

这项存储设计属于 kernel，可先于上传或历史 UI 实施。第 6.6 节仅增加一张有独立可达性职责的关系；clean-v0、权限及关系 oracle 同步 hard cut。不得旁路接入图片或绕过入口、来源与引用完整性。

### 6.4 最小 owner 分工

以下是既有 owner 的职责划分，不要求新增同名 service、registry 或统一媒体框架：

| Owner | 图片输入职责 |
| --- | --- |
| Host ingress / source | 验证并冻结 typed 提交值；完整命令比较；派生 Hook/Skill 文本；source boundary 对应最终被 canonical 接纳的该份内容 |
| Canonical content / blob / repository | 有序 body 与不可变引用、事务、exact-confirm 和 GC 可达性；URI 文本相等不能替代图片内容相等 |
| Reader / compiler / continuity | 可信 hydration、source attribution、内容顺序、各自的 canonical/epoch working-set 计量；不重新处理原图 |
| 两套 wire adapter | 确定性协议编码；不取得文件、网络或数据库读取职责 |
| Token estimator | 从同一冻结内容及实际 wire 形状产生统一的本地 token 估算；不吞并 reader、decoder 和 continuity 的物理资源 owner |
| Compaction / source selection / fork | 在各自原边界选择完整的 typed 内容单位，保留选中内容的来源与引用；summary 与文本 advisory 的用途分别定义 |

D3 的引用可达性必须覆盖原消息、已采用 successor 中保留的内容以及 fork 后的独立 canonical 所有者。共享不可变 blob 不等于共享可变消息身份；删除一个会话不能使其他仍有合法引用的内容失效。活动请求已持有的 frozen bytes 沿现有 process-local 生命周期保留，不能为此新增 durable lease 或把 provider plan 变成 GC/内容 authority。

### 6.5 D3 正文编码与图片 occurrence 身份

所有经上述 typed 提交入口建立的 prompt 正文，包括纯文本，使用同一个 `pulsara.prompt/v1` schema；不保留按新旧版本双读的生产路径。body 的 media type 为 `application/vnd.pulsara.prompt+json`、codec 为 `utf-8`。复用现有 `canonical_json_bytes()` 的 UTF-8、排序键及紧凑 JSON 规则，严格拒绝未知字段、重复键、非法联合、非 canonical 编码和无效值。其他现有非 prompt 内容格式不被伪装成 prompt。

```json
{
  "schema": "pulsara.prompt/v1",
  "parts": [
    {"type": "text", "text": "先看这张"},
    {
      "type": "image",
      "digest": "sha256:<原始编码字节的摘要>",
      "encoded_bytes": 12345,
      "media_type": "image/png",
      "width": 1024,
      "height": 768
    },
    {"type": "text", "text": "再比较下一张"}
  ]
}
```

示例是字段形状，摘要占位不能用于测试。Image 的字段集合固定为上述六项；不保存路径、URL、文件名、像素、base64、用户可变 detail 或独立图片 ID。blob 的 codec 固定为 `binary`，media type 为已验证的实际 MIME。内容摘要复用既有 SHA-256 内容完整性边界；原 `_blob_id(workspace_id, digest)` 继续唯一导出同 workspace 的 blob 身份。

现有 blob owner 同时 exact 比较 MIME/codec；若相同 workspace/digest 已有不一致的 blob 描述，沿原 `blob identity conflict` 失败，不能覆盖其元数据、忽略冲突、另造图片 ID 或修改其他 blob 使用者的契约。本版格式验证通过不承诺一个与既有 canonical blob 元数据冲突的值能发布；冲突在原写入事务提交前终止。

**part 顺序本身定义 occurrence；正文不保存 owner-local ref ordinal 或 blob ID。** 同一图片 A 在 Text/A/Text/A 中出现两次，正文有两个 Image descriptor；不能去重或合并相邻 Text parts。命令内容身份比较这份完整 canonical 正文及原命令字段，在途比较则包含完整 bytes。正文 digest 进入既有跨重启命令语义；不新增摘要层次或 DTO fingerprint。

引用行的 `ref_ordinal` 仅为某一 owner 正文的第几个 Image，从 0 连续编号。prompt 按 parts 顺序遍历；snapshot 的固定遍历顺序为 active exact content → recent requests（从旧到新）→ retained historical requests（原有顺序），每份 content 内按 parts 顺序遍历。不存在的 active 或 `CANONICAL_SUFFIX` 不产生引用。这个遍历由唯一正文编解码 owner 实现并由 publication、reader、confirmation、fork 共用，不依赖 JSON key 的偶然迭代顺序。

普通 exact 保留中，同一请求从 entry 移入 snapshot 后，即使图片从 ref 0 变成 ref 3，请求正文及 parts 仍逐字节相同；改变的是 snapshot 自己的引用坐标。第 8.9 节的 Tier 3 图片转文字属于明确允许的历史投影，新的 retained body 按这份投影编码，原 entry/queue 正文和完整命令身份不改。snapshot digest 不作为原 prompt command 的身份；来源证明比较原完整内容或规定的 P(原内容)。

原 direct / queued prompt 的 request_schema_version 分别 hard cut 为 `submit_prompt.v3` / `queue_prompt.v3`，完整内容正文及其原命令字段进入各自既有语义 digest builder；移除 v2 接纳/确认分支。重定向/取消继续由既有 action 命令引用不可变 queue 身份，不额外生成图片 command 或 receipt。

### 6.6 唯一新增关系及其必要性

新增 `pulsara_v3.canonical_image_refs`，只承载 **canonical owner 中的 Image occurrence → 既有 blob** 的引用。正文拥有语义，blob 拥有原始编码字节，关系拥有 FK 可达性；不保存第二份 MIME、尺寸、digest、字节数或验证状态。

| 列/约束 | 冻结形状 |
| --- | --- |
| 公共 scope | `session_id text NOT NULL`、`workspace_id text NOT NULL`；复合 FK → `sessions(id, workspace_id)` |
| 三个真实 owner | `queue_item_id`、`transcript_entry_id`、`context_snapshot_id` 为 nullable text；`num_nonnulls(...) = 1` |
| Owner FKs | 各自以 `(session_id, owner_id)` 引用对应表的 `(session_id, id)`，`ON DELETE CASCADE` |
| 引用 | `ref_ordinal integer NOT NULL CHECK (ref_ordinal >= 0)`；`blob_id text NOT NULL` |
| Blob FK | `(blob_id, workspace_id) → blobs(id, workspace_id) ON DELETE RESTRICT` |
| 唯一性 | 三个 partial UNIQUE indexes：每个非空 owner 的 `(session_id, owner_id, ref_ordinal)` 唯一；不得仅用包含多个 NULL 的普通组合 UNIQUE |
| GC 反查 | 以 `blob_id` 为前导列的索引；引用行没有独立 ID、状态机、时间线或可更新内容 |

现有 queue/entry/snapshot 的 scope FK 与公共 scope 一起排除跨 session/workspace 错绑。body descriptor 与 refs 数量/顺序的双向完备性由同一 repository writer 在原事务内检查，并由 reader/confirmation 再核验；FK 只证明目标存在，不宣称 SQL FK 已验证 JSON 内容。禁止增加一个 JSON parser trigger、append guard 或修复 job 来代替这个 owner。

当前 `accept_root_turn_intent()` 在同一 writer transaction 内建立 turn、initial binding、entry 与 direct command，不存在第四个已提交但无 entry 的 direct body owner；`session_commands` 不新增图片 refs。pending queue 则确实在 entry 出现前独立持有内容，必须有自己的 refs。

独立产品必要性是防止 GC 删除仍被正文使用的图片，并让 snapshot/fork 拥有独立可读内容；不是用于证明流程执行过。durability delta 固定为 **product relation +1；durable/live event kind、subject slot、append guard、durable job 均 +0**。同步更新 clean-v0 schema、runtime grant、FK/index/schema verifier 和关系 oracle。`canonical_image_refs` 的 runtime grant 仅 SELECT/INSERT，无 UPDATE/DELETE；GC 仍只 DELETE blobs，真实 owner 删除由既有高权限路径触发 FK cascade，cascade 不要求 runtime 获得 child DELETE 权限。不添加通用媒体表或 owner_kind 字符串关系。

### 6.7 Publication 与各条写入事务

`CanonicalContentPublisher` / `PostgresCanonicalBlobStore` 继续是唯一 inline/blob 选择和不可变 blob SQL owner；当前它们自行取得连接，实施时将 SQL 核心收敛为可使用 caller-owned connection 的内部路径。连接由原 repository transaction 提供；该路径不得重新开连接、commit、rollback 或进入另一条 I/O lane。不复制 publication SQL；其他原有独立发布用途可由同一核心使用原有连接包装。

**inline 阈值只用于正文。** prompt/snapshot 的 JSON body 继续按原不超过 64 KiB 的阈值选择 InlineContent 或 body blob；每张图片不论大小都通过同一 blob publish/exact-reuse 核心得到 BlobContent，供 image ref FK 引用。不能把小图送入 `materialize()` 后拿 InlineContent 冒充 blob，也不添加第二份 inline image 存储形式。

当前 fork 新建 snapshot 直接写 `inline_content=carrier.body` 的分支也要收敛到这个 caller-connection body publisher；复制未改动的 entry/body 可沿原内容描述复用。不能只更新 Host ingress，却让 fork 自行选择另一套新正文存储形状。

顺序固定为：有界输入与 D2 验证 → 冻结完整值并完成原 Hook → 本阶段要求的 D2 接纳准备 → 原 writer transaction 内重新验证 fence/preconditions → 发布/精确复用 unique 图片 blob 和 body blob → 写 owner/body 与全部 refs → 核验完整对应 → 原 command/event/adoption → 一次 commit。图像解码和网络调用不放入数据库事务。未通过入口合法性或原 Hook 的候选不先发布图片 blob。

**存入 pending queue 与进入有效 canonical 输入分开。** 入队以完整 M、D2 图片/文本合法性、原权限/target/binding 规则和原入队边界为准，允许当前 source 暂时放不下；pending queue 不计入当前 epoch 的 C/L/W。将 queue 消费为 entry、direct 接纳为有效 entry、或采用 successor 前，才必须针对同一 source/target 的实际完整候选通过 D2 C/L/N/W/token/G。不能为了入队先安装内容，也不能因入队已经 FULL 就跳过消费时的资源/目标检查。已确定冷上下文仍不可能接纳的候选沿 D2 明确拒绝，不以“已入队”承诺必然执行。

| 路径 | 原事务内必须共同成立的内容 |
| --- | --- |
| direct | turn、binding、initial entry 正文、该 entry refs、图片/body blobs、原 command/event 一起提交 |
| queued NEW_TURN / STEER 入队 | 原 command 与 queue 正文、queue refs、图片/body blobs 一起提交；入队不等于已消费或安装，保留原提交/执行时序 |
| queue/steer 消费 | 当前完整批次接纳后，在原消费事务建立 entry 正文及独立 refs，再更新原 queue 状态；原 queue 正文和 refs 保留 |
| queue 重定向 | 当前 owner 会 INSERT 一个 replacement queue，再将 source 标为 CANCELLED；在同一事务为 replacement 按 ordinal 复制完整 refs，并保留 source body/refs。重定向命令自己的原身份/权限规则不变 |
| queue 终止 | CANCELLED/REJECTED/CONSUMED 等状态本身均不是删除 refs 的依据；没有新 owner 时不复制引用 |
| compaction adoption | PRE_FULL 后在原采用事务发布最终 carrier body、建立 snapshot refs 并推进原 binding/pointer；failed/unadopted 候选不写 durable refs |
| fork | 原 REPEATABLE READ 中读 anchor-effective 内容，建立 child-local entry/snapshot/body/refs；helper 全部复用该 connection |

对已由 canonical owner 持有的图片，snapshot/fork/queue 消费精确复用 blob，不重解码、不重写 bytes。新 owner refs 是复制可达关系，**不是从仍保留正文的旧 owner 转移引用**。旧 snapshot 的 base 指针退出当前有效输入，也不等于旧 snapshot 被删除；历史 anchor/fork 仍可能需要它。

Hook 拒绝、precondition 失效、取消或提交前异常沿原路径退出，事务内未提交的 body/blob/ref 一起回滚。提交确认不确定时只查询，不重跑 writer/Hook。源漂移沿原 fence 路径重新准备，不能在旧 quote 下提交新正文。

### 6.8 完整提交确认、读取与计量

**提交 confirmation 的 FULL 表示完整命令及其内容引用已经提交，不表示此刻重新检查了全部图片 payload。** 只读 confirmation 复用 repository 的连接/deadline，在一个有界的只读 REPEATABLE READ 视图内检查 command、真实 owner、正文、refs 与 blob 元数据；不持有 writer generation、不调用 publisher、不尝试修复。FULL 条件为：

1. 原 command/session/delivery/target/permission/model binding 规则全部匹配。
2. 原 owner 的完整 body 经过长度/digest/media type/codec 校验，严格解码；重编码正文与由完整冻结候选导出的 canonical body 完全相同。正文若存于 blob，仍读取并校验其实际正文 bytes。
3. body 中每个 Image 都有唯一相应 ref，ordinal 连续，无缺失、额外或重复 ref；同 scope 的 blob 行必须存在，其 blob ID、digest、长度、MIME、codec 与该 descriptor 和候选匹配。逐 occurrence 检查引用，即使它们指向相同 blob；只读这些图片 blob 的元数据，不读取 payload。

正文有界解码后，refs 明细最多取得预期 image occurrence 数加一行，以检测额外 ref。正文读取前的 metadata quote 使用同 scope 的有界返回聚合和原查询 deadline；确认无需 hydrate 图片，逻辑内容初筛仍按完整展开字节计量。不新增固定图片数量 cap。

这一分工依赖第 6.7 节的原子 publication、不可变 blob 和真实 FK：发布时精确核验原始 bytes，正常 hydration 校验长度/digest。缺正文、缺 ref/blob、错指或元数据冲突不能 FULL；仅图片 payload 损坏而引用/元数据匹配时可确认已提交，读取时仍须失败，不能发送损坏内容。这项收窄仅用于提交确认，不改变消费、compaction PRE_FULL/POST_FULL 或实际输入校验。

原 command 与 owner 均不存在才返回原 NONE；命令不同或上述不完整形状沿原 CONFLICT/内容完整性错误分类。I/O、取消和 deadline 只表示此次查询未完成，不能冒充 NONE、CONFLICT 或永久图片损坏，也不能授权重做 writer。queued FULL 返回原结果/wake hint；direct 保留查询原 outcome 的区别。

正文读取及正常图片 hydration 的 exact-read SQL 核心接收 caller connection，沿用 digest/size 并核验同 scope 的 MIME/codec；不调用会自开连接的 wrapper。publication、reader、confirmation、snapshot/fork 共用正文与引用遍历，但仅实际发布/读取图片的路径校验 payload，不为确认另建完整性算法。

普通 reader 的顺序为：冻结现有 scope/cut → bounded metadata preflight → 有界读取所选正文 → 唯一 decoder/引用遍历与描述核验 → 计算完整展开 charge → 有界 unique blob exact hydration → freeze typed input → compiler/真实 wire materialization → 原接纳 gate。初筛与最终通过明确分开；metadata 数量不能代替 exact W/D1，不能宣称“尚未 hydrate 就已经证明可发送”。

在读取正文前，使用 owner 的 content_size 加同 cut refs 对应的 blob logical_size 逐 occurrence 求和，连同原 tool result/envelope/参数上界形成保守 `C_quote`；读取正文也受原 16 MiB 物理读取预算约束。随后核对 descriptor 与数据库元数据一致，`C_quote >= C_charge`。覆盖完整 base snapshot 和 suffix；原 post-base 专用 SQL 不能漏算 nested active/recent/historical 图片。完整正文计一次，其中每个实际 descriptor 加其 `E_i`；正文文本不再另加一次。

`build_synthetic_compaction_dispatch_read()` 与 `_compaction_cut_lineage_exactly_joins()` 当前使用 `len(snapshot.text)+sum(suffix.text)` 的等式，必须与真实 reader 同步改为同一 C_charge 函数。dry 候选使用完整 frozen 内容和预计 body 描述即可纯计算，不提前发表 blob；POST_FULL 的真实读取必须与 dry 候选的 typed 内容、引用语义、scope/cut 和各项 charge 相等，不能只让 final wire 的图片 golden 通过。

物理 hydration 可去重，同一个过程只保留有界的原始 bytes；独立 owner/placement 的逻辑收费不去重。snapshot 和 suffix 对 active 的唯一 placement 按第 8 节判定；未选中的历史 owner 虽继续维持 GC 引用，不进入本轮 C/L/W/D1。current source、候选与旧/新 materialization 的并存继续受原 planning 资源 owner 约束。

reader 缺失或损坏按既有 `CanonicalProviderContinuityError` / repository integrity 错误体系失败；不补图、不取原文件、不读 provider URL、不退回文本。宽高可信性由已验证 ingress 写入和相同 immutable bytes 的完整性链保证，不每次冷读取 load 像素；独立 wire 校验仍服从冻结的 D1/D2。

### 6.9 GC 与删除竞态

沿用 `delete_orphans()` 的 grace、bounded batch、同语句 NOT EXISTS/DELETE 和现有 scanner advisory lock；增加对 `canonical_image_refs` 的 NOT EXISTS。仍保留所有原 body/artifact 引用条件。引用存在时 blob FK 必须阻止删除；删除 owner 时 refs 由对应 FK cascade，不能按“已压缩”“已消费”“父会话关闭”清除仍存在正文的引用。

新增 attach 与 orphan DELETE 的最终并发仲裁由真实 FK/数据库事务承担：attach 先成功提交则 GC 不得提交删除；DELETE 先成功则旧 blob attach 必须失败并使所在写入事务不提交。原始 ingress 可在其正常 publication 内发布完整 bytes；不能用忽略 FK、悬空 ref、额外 lease 或在确认路径重发 writer 绕过竞争。数据库并发/完整性异常按原事务失败分类处理，不声称任何顺序都保证一次成功。

实施验收必须以真实 PostgreSQL 双连接覆盖两种提交顺序、复用旧 blob、事务回滚与父/子分别删除。K2 已在 disposable PostgreSQL 上以重叠事务覆盖 attach 先提交与 DELETE 先提交、重复 blob 复用、整事务回滚、独立 owner 依次删除及 FK 最终仲裁；K4 已将这些真实事务与并发回归纳入最终完整验收；U1/U2 随后完成浏览器链路。

## 7. 两套 adapter 的唯一编码路径

同一组“文本 + 图片”内容，分别产生以下标准形状。示例中的省略号是格式说明，不能直接作为图片测试数据。

Chat Completions：

```json
{
  "role": "user",
  "content": [
    {"type": "text", "text": "说明这张图"},
    {
      "type": "image_url",
      "image_url": {
        "url": "data:image/png;base64,...",
        "detail": "auto"
      }
    }
  ]
}
```

Responses：

```json
{
  "role": "user",
  "content": [
    {"type": "input_text", "text": "说明这张图"},
    {
      "type": "input_image",
      "image_url": "data:image/png;base64,...",
      "detail": "auto"
    }
  ]
}
```

协议参考：[OpenAI 图片输入指南](https://platform.openai.com/docs/guides/images-vision)、官方 SDK 的 [Chat 图片 part 类型](https://github.com/openai/openai-python/blob/main/src/openai/types/chat/chat_completion_content_part_image_param.py)与 [Responses 图片 part 类型](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_input_image_param.py)。本轮也核对了仓库 `.venv` 内对应 SDK 类型：Chat 的 URL 位于对象内，Responses 的 URL 是直接字段；两者不能混用。

实施落点：

- Chat 在 `chat_semantic_wire_group()` 使用的消息投影函数中处理 typed parts。
- Responses 在 `responses_semantic_wire_group()` 使用的消息投影函数中处理 typed parts。
- 两者继续被普通调用、wire planning 及相关 auxiliary planning 复用；不在各消费者复制图片编码。
- 纯文本继续使用当前字符串投影，特别保留 Responses 既有 assistant string form、function call/output 和 native replay 形状。
- `build_*_payload()` 消费已经冻结的 context-bearing projection，不后补图片、不替换 URL、不改变 detail。
- Responses 继续遵守当前 `store=false` 与手工完整输入路径；不引入 `previous_response_id` 或 provider 文件上传作为恢复机制。

final-wire estimator 的集成接口同步接收 **同一次完整 frozen semantic content 与真实 ordered wire items**，按原 semantic wire group 的来源及正式 USER part 坐标并行遍历：逐个核对 role、part 类型、MIME、固定 detail 和实际 data URL 编码内容与该 Image bytes 一致后，复用随其携带的可信宽高计算 D1。不能仅按“第几张图”或另给 width/height 表配对。首版 native replay replacement 只替换既有 assistant 分组，USER 图片的来源必须在 generic、replacement 后的 final 与 direct traversal 中仍精确对应。

这实例化的是 D1 已允许的完整 frozen 内容复用，不改变公式或把 semantic estimate 升级为 admission。当前 `direct_model.py` 仅把 wire JSON 交给 estimator 的调用点必须同步调整，不能在每个 recent/tail 候选丢掉尺寸后又重跑像素解码。核对实际编码时复用同次标准编码的冻结结果，或用标准库有界分块比对，不为每个 occurrence 再保留整份 base64/像素副本。独立输入仅有 raw wire、没有可精确对应的完整 frozen Image 时，先由原 D2 验证 owner 恢复可信内容，再调用纯 estimator；不新增 fingerprint→object cache 或另一份尺寸 authority。

通用 base64 编码使用标准库；图片格式检查若需要解码，先检查现有依赖与维护中的库的适用 API，再选择最小调用边界。不要自行编写图片解码器或复制 SDK 协议实现。

## 8. 前缀连续性、重试与上下文生命周期

同一 epoch 内，历史图片的字节、media type、detail、part 顺序及编码结果一经安装，必须保持不变。添加后续消息只追加后缀；不得重新压缩旧图、更换 URL、改 detail，或因为 catalog 刷新而重新生成旧输入。

SYSTEM 与 provider tools 仍须 byte-identical。图片支持不是重建 root、重新发现工具或重放 Hook 的新边界。

透明传输重试继续沿用既有 semantic-output barrier，并消费同一个 frozen plan。provider 返回不支持图片或字段非法时，不能自动改成纯文本、自动去掉 detail、切到另一套 wire API 或重试另一种图片格式。拒绝必须保留真实 endpoint 错误及既有 terminal 语义。

冷 epoch 与明确采用的 compaction successor 仍是仅有的 root rebuild 边界。对被选入有效上下文的图片，应保留其 canonical 来源和内容；不能因只支持文本的旧投影而无声丢失。图片参加 summary source、recent window、fork 和 model switch 的规则按本节执行，不能在 adapter 中临时选择“全部保留”或“全部删除”。

不向 memory 或其他 auxiliary call 自动附带全历史图片。每类调用仍由原 purpose 与 source selection 决定其输入；不能把 OCR 或模型生成的图片描述当作原始图片内容的无损替代。

**普通同模型压缩的 summary source 不是新的产品选择。** 当前 `direct_model.py::resolve_compaction_summary_call()` 复用 origin call 的 target；`compaction/model_call.py::prepare_compaction_summary_semantic()` 在合法 source prefix 后追加 `LLMMessage.user(summary_request)`，`_require_summary_wire_prefix()` 要求实际 wire 保留完整已安装前缀及相同 SYSTEM/tools。因此图片链路接通后，已安装前缀中的图片必须原样进入这次 summary 请求，不能先移除或 placeholder 化；也不因此补回此前已退出有效上下文的全历史图片。合法 safe cut 与 retained tail 继续由原 planner 决定。D4 需要实例化的是 successor 采用后的图片保留，以及已有模型切换 Tier 3 destination projection 的 typed 内容选择；不能借“摘要模型是否看图”重新开放普通压缩的前缀规则。

### 8.1 ROOT 父上下文：本版保留文本 advisory，明确视觉信息缺失

`_freeze_subagent_parent_context_call_subject()` 是每次 ROOT dispatch 的必经投影，当前位于 continuity install 之后，不以实际 spawn child 为条件。它及 `FrozenRootConversationContextUnitFact`、parent-context selection/rendering 必须在同一次 content hard cut 中修订；不能等待子代理图片工具功能。

本版继续使用现有 `NONE` / `LAST_N` 和 public ROOT conversation unit 的文本 advisory 契约，**父上下文不携带图片字节**，也不赋予 child 新的 blob 读取能力。投影必须：

1. 沿原 compiled message placement 关联 canonical entry / turn / origin，只对原本符合条件、具有 entry 来源的公开 ROOT USER/assistant 构造 unit；USER 继续限定 HUMAN_MESSAGE/HUMAN_STEER。沿 part 原顺序呈现公开文本，保留原 turn-unit 分组、选择范围、ordered entry IDs 和 source identity；不能仅按 turn_id 重新分组。
2. 上述符合条件的 HUMAN USER 中，每个 image part 在原位置产生稳定标记，明确表达 `image part <ordinal> omitted from parent context; visual content unavailable`。不把图片跳过成空字符串，不伪造视觉描述。part ordinal 对应原内容位置；来源关联由已有 parent-context fact 保留，不能只剩一段无法追溯的拼接文本。
3. 符合条件的纯图 USER 也保留相应 entry 和非空缺失标记，不能因为没有 text 而整条丢失；重复图片分别保留标记，不去重。
4. assistant 只贡献原有 public text；thinking、tool group、无 entry/turn 来源的 runtime source 继续按现有规则排除。CONTEXT_SNAPSHOT 整体及其中 nested active/recent/historical 都不成为 LAST_N public unit：reader 的 snapshot item 没有原 entry/turn 身份，active exact 的执行证明也不等于完整的 parent-context unit 来源。不为此重建历史身份、扩充 carrier attribution 或新增 refs。`NONE` 仍不提供父上下文；`LAST_N` 的子集选择不改成全历史。

该标记说明已有文本 advisory 的信息范围，**不是目标不支持图片时的 provider 降级方案**。ROOT 的实际图片请求仍发送原始图片；B 不兼容仍按第 4.1 节拒绝。未来若决定给 child 传图，需要显式修订 parent-context typed source 和其预算/归属契约，不能悄悄把此文本投影改为附件转发。

可能失败的 part validation、文本投影和资源检查在 continuity install **前**完成，绑定到同一 prepared dispatch 的内容和 placements；install 后只用原 permit 完成 subject 的 exact 绑定，不再执行图片读取、解码或新的内容遍历。保留原 owner 和 permit 校验，不增加 authority、receipt 或恢复层。

### 8.2 D4 完整 source / retention 矩阵

“仍存在数据库”“summary 这次看到原图”“successor 再次发送原图”是三件不同的事。有效输入退出不删除原正文和 refs；summary prose 不能恢复退出的原图，本版不增加按历史 ID 自动找图的能力。

| 场景 | 选择/保留单位与模型可见内容 | 资源、采用与生命周期 |
| --- | --- | --- |
| 同 epoch suffix / retry | 完整已接纳请求，原 parts 与 wire 不变 | 原 plan/permit；新后缀按 D2 接纳，重试不重读原文件 |
| 普通 summary / Tier 2 A summary | 原 planner 允许的 installed source prefix；其中全部图片原样进入同目标 summary 请求 | 不重建 A prefix；summary call 本身也要能通过原 final-wire gate |
| 普通 successor | 新 summary、必须保留的 active/historical、完整 canonical suffix/tool groups、可选 recent | 第 8.4 节顺序；同一实际候选通过 D2 与原 PRE_FULL/reclaim/soft target |
| 冷 epoch / Tier 1 B | 当前有效 canonical base+suffix 的完整 typed 内容；不另做 recent 搜索 | B 实际输入的内容/模态、D1/D2 与原 trigger；失败不安装 |
| Tier 2 B successor | A 的原前缀 summary + 必须内容 + recent；先按原规则选 recent，纯文本 B 且所选窗口有图才整体置空，否则原样保留 | 不裁 A summary source 的原图；唯一候选，未形成合法 B successor 才沿原分类进入 Tier 3 |
| Tier 3 B summary | 当前 source + 历史 projection USER + exact active USER + summary USER；纯文本 B 所选历史中的 Image 原位变为 Text("[图片已省略]") | 原连续 dialogue suffix/evidence 选择；临时 projection 不安装、不落库 |
| Tier 3 successor | B summary + exact active + 必须历史 + 按规则选定的 recent；纯文本图片交接 recent=0，必须历史使用同一文本替换 | 唯一实际 successor；先校验后采用，不重新注入旧图 |
| fork | anchor 当时有效的完整 canonical 内容；source active 转 historical | child-local owner refs，共享同 workspace bytes；不复制执行身份 |
| ROOT；有 child / 无 child | 主调用正常发送原图；独立父上下文按原顺序输出缺图文本标记 | 第 8.1 节 install 前投影；不新增 media refs |

不以 digest 合并两次语义 occurrence，也不拆一条请求的部分图片来填余量。mandatory canonical suffix 仍按原 safe boundary 和完整 tool-call/result group 选择，不能为了 recent 或某张图跨越不可安全切割的边界。

### 8.3 Typed carrier：内容、来源与唯一 placement

保留现有 request 分类，不能将“request”统称 HUMAN。图片入口仍只授权 human typed prompt，其他来源在本版保持其现有文本产品语义；将来扩展工具或子代理传图需另行授权入口。

| Carrier | 合法 kind / origin |
| --- | --- |
| active | USER 的 HUMAN_MESSAGE 或 SUBAGENT_OBJECTIVE；以及现有 PLAN_CONTINUATION、INTER_AGENT_MESSAGE、TERMINAL_OBSERVATION 合法来源 |
| recent human | USER 的 HUMAN_MESSAGE / HUMAN_STEER，排除 active entry |
| retained historical | USER 的 HUMAN_MESSAGE / HUMAN_STEER / SUBAGENT_OBJECTIVE / USER_CONTROL_FEEDBACK；PLAN_CONTINUATION / PLAN_CONTINUATION；INTER_AGENT_MESSAGE / INTER_AGENT_MESSAGE；TERMINAL_OBSERVATION / None |

`FrozenCompactionActiveRequest` 保留 entry_id、entry_sequence、location，增加冻结的 item_kind/input_origin，并将 `text` 替换为 `content: FrozenPromptContent | None`；相应 durable 值使用第 6.5 节的 descriptor body。`SNAPSHOT_EXACT ⇔ content 非空`，`CANONICAL_SUFFIX ⇔ content 为 None`。有效的纯图 content 非空，不能用 text.strip() 判断。

`FrozenRetainedHistoricalRequest` 保留 kind/origin，将 text 替换为同一 `FrozenPromptContent`，但不携带 active entry 执行身份。`recent_user_messages` hard cut 为 `recent_human_requests`：复用该 request 值的有序序列，仅允许 USER + HUMAN_MESSAGE/HUMAN_STEER 子集；字段所在位置决定 recent 语义，不升级为 mandatory historical。entry/sequence 继续在本次 selection 的 `RecentHumanMessageProof` 中验证，不为历史 recent 制造 active 身份。非 HUMAN 来源的 content 仍为原 text-only 形状，复用 kind-specific lowering。图片省略只是第 8.9 节产生的普通 Text，不增加另一种 content 类型或正文 schema。

snapshot 顶层固定为 `continuation`、`earlier_context_summary`、`recent_human_requests`、`retained_historical_requests`；continuation 保留 mode/instruction/active_request，request 中的 content 使用第 6.5 节正文对象。编解码由原 `build_compaction_snapshot_carrier()` / `parse_compaction_snapshot_carrier()` 及 contracts 的 canonical equality 检查共用唯一 codec。现有 snapshot compiler contract 更新为 `pulsara.context-snapshot-carrier.v4-typed-content`，media type/UTF-8 codec 保持，不保留旧 text carrier 生产 decoder。其 body 仍用既有 inline/blob 单一选择，图片通过 snapshot refs 独立可达。

当前 active 被 cut 覆盖则放入 SNAPSHOT_EXACT；未覆盖则只记录 CANONICAL_SUFFIX 位置。连续压缩后原 active entry 不在当前展开 suffix 中时，从唯一 predecessor carrier 机械取得相同 kind/origin/content；不能从 summary 猜测、从已退出全历史搜索或另发模型请求恢复。每个 successor 都以 typed content 与 canonical attribution 验证 active 恰好一个 authoritative placement，不能只验证 message 文本子串或 blob digest。

旧 snapshot 在后续 turn 中只具有历史地位，其 RESUME 文案受既有“较新 turn/request 优先”的语义约束；本次 active 身份由当前 turn/binding 决定。选择 recent 时排除本次 active entry，不能靠内容相等删除不同历史请求。

**Exact carrier 是既有 canonical continuation 内容，不是 durable provider checkpoint。** wire plan、permit、nonce 和 retry 状态仍只在原 process-local owner 中；不持久化 provider payload，也不把文本描述当 exact image。

### 8.4 Recent 与 protected tail 的确定选择顺序

普通同模型 successor 的选择规则如下；它改变的是合法重建边界的 retention，不能用于已安装 epoch：

2 MiB 的计量落点仍是 `freeze_compaction_canonical_range()` 所用的 `provider_input_item_logical_utf8_bytes()`：原函数计 `canonical_json_bytes(provider_input_item_leaf(item))` 的长度，本来就不是 reader C。typed hard cut 后，该 leaf 以 canonical descriptor 表达图片，不序列化 raw bytes/base64；其结构长度用于 tail 政策，完整图片 E 由 D2 展开计量函数单独计入 C/L/W/D1。不能将两种“canonical bytes”因旧字段命名相近而合并。

1. 原 planner 按既有 retained tool group 优先顺序选择 safe cut 和 summary source；完整 tool group 仍为单位。保留原最多 3 组和 2 MiB tail 政策，`maximum_retained_tail_utf8_bytes` 继续限制文本/结构字节：沿原 tail range 规则计文字、参数和原包装，新 prompt body 的 Image descriptor 计入结构字节，但被引用的图片 E 不进入这个文本政策。**完整图片 E 始终进入独立 D2 C/L/W/D1，不被免单；不能把完整 C_charge 直接与 2 MiB 比较。** 这避免 source prefix 因 summary 预算截短时，将必须留在 suffix 的大图误判为超出 2 MiB 单图上限；mandatory 请求仍不可拆。它不放宽原工具文本尾部，也不代替真实 successor 的完整硬接纳。
2. 对固定 cut，选 source_through_sequence 以内的 human 请求，排除 active，只从当前有效 canonical items 取候选，不重读已退出的全历史。保留 `maximum_recent_human_messages = 3`；将 byte 字段 hard cut 为 `maximum_recent_human_text_utf8_bytes = 65_536`，只累计 Text parts 的原始 UTF-8 字节，不计图片 E 或 JSON 包装。
3. 从最新向前选择连续窗口。最新一条文字自身超界则 recent 为空；遇到下一条使累计文字超界就停止，不跳过它取更老的请求。图片不进 64 KiB，但选中的完整请求必须全额进入 C/L/W/D1 和 item 检查。
4. A 只对该固定 source 做一次 summary。冻结同一 summary、cut、active、historical、tail、runtime/current-source observations 及其有序 variant 候选集合，依次试 initial recent、去掉最老一条后的后缀，直到空。每次仍调用唯一原 compiler，按原规则重新选择合法 variant，再对完整结果做 C/L/N/W/D1；不要求 selected variant 不变，也不假定“少 recent 必然少 wire”，不能用单调性或简单减 token 代替测量。该循环不重跑 summary、Hook、工具或外部 source 读取/发现，不写 body/blob/refs，不取得 install authority；先释放上个 candidate 的 materialization。
5. 接受第一个同时满足内容/目标、D2 C/L/N/W/token、原 reclaim、minimum gain 和 ordinary soft target 的完整候选。因资源或回收不足才可收缩 recent；模态不兼容、引用损坏、非法内容或 source drift 不能通过删 recent 处理。drift 沿原 fence 重新准备。
6. recent 已空仍无合法候选时，才按原可重试失败集合进入更小 protected tail 的普通候选路径；必要时该新 cut 重新 summary。不能用 recent 搜索取代原 safe-boundary 或无收益处理。无合法回收但输入仍合规时沿 D2 already-compact 语义；mandatory 内容不合规时明确资源失败。

这里明确 **优先保留当前候选的 protected tool tail，再尽可能保留 recent**；active exact、必须机械携带的 historical 和不可跨越 suffix 不参加 recent 收缩。每次最多检查 initial recent 所有后缀，界来自既有 3 条窗口，不增加模型调用或任务生命周期 cap。同一份图片即使同时被不同合法历史请求引用，也逐 occurrence 计量。

**模型切换不使用上述 recent 收缩循环。** Tier 1 原样冷构建。Tier 2 在原 planner 选定的 cut 上，先按步骤 2–3 一次选出最近最多三条原话；B 明确支持 text、不支持 image 时，检查这个已选窗口的完整 typed content：没有 Image 就原样保留，任一请求含 Image 就将整个 recent 置空。不得只删含图请求、另取更老的纯文字补位或循环试不同数量。更早的有效历史图片仍使 Tier 1 转入 Tier 2，并由 A 沿原 source 总结，但不使这个纯文字窗口置空。

Tier 3 保持原约定：纯文本 B 且执行 P、截短 projection 前的冻结有效历史含 Image 时，recent 固定为空；不能因替换后或较短 projection 中已无 Image 而恢复窗口。其他场景按步骤 2–3 一次选定 recent。以上均是本次 attempt 的确定规则，不改全局 ordinary policy，也不新增用户配置项。每档只组装其唯一 successor，失败沿第 8.6 节处理。recent=0 不删除当前 active、未被摘要覆盖的 mandatory historical 或不可跨越的 canonical suffix。

Tier 2 的图片检查只针对按原规则实际选中的请求，在任何文本投影前判定真实 Image part；不是扫描 UI 上最后三条消息，也不因图片出现在窗口外而清空窗口。窗口原本为空就保持为空，不足三条按实际条数处理；普通 Text("[图片已省略]") 仍是文字。

### 8.5 Snapshot 与 projection 如何真正发送图片

新生成的 snapshot / Tier 3 projection 统一由原 compiler/lowering 的 renderer 产出一条 USER message 的有序 Text/Image parts，纯文字使用相同展示结构。存储 carrier JSON 不直接透传为 wire，不另保留旧纯文本 carrier 展示格式。

统一使用 `display_json(value)` 展示正文、header 与 metadata：先调用原 `canonical_json_bytes(value)` 并解码为 UTF-8，再将每次出现的字面前缀 `[PULSARA_RETAINED_CONTENT`、`[/PULSARA_RETAINED_CONTENT` 的起始 `[` 转义为字面 `\u005b`。有效 JSON 中这两种前缀只可能位于字符串内，因此标准 JSON 解码仍恢复原值，而不可信文字不能生成裸段标记。无图、含图、summary 和工具证据都用同一规则，不另建 parser 或存储 schema。

- **Snapshot header：** 首个 Text 为按下述位置指引更新的 snapshot notice，加 `display_json({"continuation":{"mode":mode,"instruction":instruction},"earlier_context_summary":summary})`；随后按 active → recent → retained_historical 输出完整 content。`CANONICAL_SUFFIX` 不重复输出 active。
- **Projection header：** 首个 Text 为 `display_json({"projection_notice":原notice,"prior_handoff":null或{"earlier_context_summary":原summary}})`；随后按 prior_recent → prior_historical → 原 turn/entry 顺序输出完整 content。
- **内容分段：** 每份 content 由 Text `\n[PULSARA_RETAINED_CONTENT <display_json(metadata)>]\n` 与 Text `\n[/PULSARA_RETAINED_CONTENT]\n` 包围。metadata 只保留 section、role；section 为 active/recent/retained_historical/prior_recent/prior_historical/dialogue。dialogue 另含零基 turn_index 以保留原 turn 分组，以及原存在的 requested_tools 列表；每项保留 name、result_status 与原规则选定的 retained_result 或 result_omitted，不展开到标签顶层。entry 顺序由输出顺序表达；entry ID、sequence、item_kind/input_origin 和内部 placement 留在 typed source/proof 中，不重复塞进展示 header 或标签。
- **内容与来源：** 每个 Text 原值用 `display_json(text)` 作为 JSON 字符串展示，Image 保留真实 part 和原始 bytes，原 part 顺序不变。其他合法来源先复用原 kind-specific lowering/wrapper，再对其 Text 使用相同引用规则，保留 PLAN/terminal 等语义。projection 的 assistant public text 与工具证据同样引用；空文本展示为 JSON 空字符串。notice 明确这些是引用内容，分段 role 描述原发言角色，不把历史 assistant、工具证据或非 HUMAN 请求提升为新用户指令。
- **统一位置指引：** `SNAPSHOT_EXACT` 一律指向 section=active 的内容；`CANONICAL_SUFFIX` 继续指向 snapshot 后的原 canonical 请求；AWAIT_NEXT_USER、RESUME 和较新请求优先的语义保持。同步更新 lowering 的 snapshot notice、handoff instruction 和 summary request 中的旧展示字段指引，历史请求指向相应 section，不再引用 active_request.text 或已退出展示的字段；不因有无图片切换。内部 exact attribution 与唯一 placement 校验不依赖模型读取标签或数据库 ID。

第 8.9 节在渲染前将图片变为普通 Text("[图片已省略]")，随后使用同一 renderer，不切回旧 JSON 展示。最终全部为 Text 时仍按第 5 节 adapter 规则连接成字符串，含 Image 时使用标准 parts 数组；这是相同展示结构的协议 lowering。C 按实际 canonical body 与图片展开计量，实际展示文字、标签及图片按 D2 的 L/W 和 D1 计量，不额外附一份完整 carrier JSON。

引用/转义只改变本节的展示，不修改 canonical Text、命令身份、Hook/Skill 投影、普通消息或 P 的输入输出；typed exact proof 比较原内容，wire 校验消费同一 renderer 的实际输出。只有 renderer 自己生成的裸开闭标记构成分段，原文中的同名标签和 JSON key 仍是数据。

本次明确放宽的是**新 snapshot/projection 的旧纯文本 wire golden**，实施时更新这些 golden 与位置指引；普通纯文本消息、角色/工具协议及 D1/D2 算法不变。新 snapshot 只在冷 epoch 或已采用 successor 的合法边界进入有效输入，临时 projection 不安装；已安装前缀及普通 summary 的原 source 不重渲染、不替换。

整体仍是一条 source-bound USER placement。active exact、nested 内容和 refs 的内部证明完整保留，不新增 durable placement 或来源身份。ROOT public source 按第 8.1 节排除整个 snapshot 及其 nested 内容，ROOT 主调用仍接收 D4 选中的原图；图文分段不制造新的 human trigger 或 child advisory 范围。

### 8.6 模型切换：沿原三级 compact，纯文本目标的图片处理

用户选择模型即接受该目标的输入限制，不增加“常规/允许省略”双模式、提交许可或逐图确认。以下规则作用于本次冻结目标与**当前有效 canonical base+suffix**，不扫描已退出有效范围的全历史图片。B 的 input modalities 必须已知含 text 且不含 image，才使用纯文本规则；unknown 保持原语义，不由 provider 错误或模型名称猜测。

| 档位 | 固定行为 |
| --- | --- |
| Tier 1：直接切换 | B 能接纳当前完整输入时直接冷构建。若 B 是纯文本且有效历史有图片，即使 token/bytes 足够，也进入 Tier 2；不在 Tier 1 就地删图 |
| Tier 2：原模型总结 | A 沿原已安装前缀总结，source 中的图片照常给 A。先按现有规则选最近最多三条原话；纯文本 B 且所选窗口含 Image 时，将整个 recent 置空，否则保留原窗口。较早的历史图片不影响纯文字窗口的保留。A summary 后构造唯一 B successor，完整可执行就采用 |
| Tier 3：新模型总结 | A 不可用，或 Tier 2 按原允许分类未能形成合法 B successor 时，由 B 读取当前有效历史的 destination projection 并总结。B 为纯文本时，所选历史中每个 Image 原位变成普通 Text("[图片已省略]")；该档纯文本图片交接 recent 仍固定为 0。B summary 后构造唯一 successor，检查通过才采用 |

**recent=0 只关闭旧用户原话窗口，不删本次请求，也不把尚未总结的必要历史假装已经总结。** Tier 2 的 tool-group ceiling 仍为 0；safe cut、A summary source 和 mandatory suffix 继续沿原 planner。A 实际覆盖 predecessor base 后可按原规则清空已覆盖 historical；若未覆盖的 historical/suffix 仍带图，唯一 B successor 仍不能通过模态校验，此时进入 Tier 3，不偷偷删除它们，也不循环试不同 recent 数量。

Tier 2 保留原 closed fallback：完成 A safe-prefix 搜索后无可执行候选、A 的 typed execution/output-incomplete、原 repair 后仍无合法 summary，以及原允许的 B resource/compile failure；历史图片使唯一 B successor 不兼容也明确进入 Tier 3，不把模态原因改名为预算不足。已持有合法 A 来源，但连接/凭证不可执行时不要求成功调用 A；source/epoch owner 丢失或不匹配仍是完整性错误。取消、Hook block、planning 未完成和 drift 沿原路径处理，不借此降级。

Tier 3 保留原 `freeze_destination_dialogue_projection_plan()` 的当前有效 lineage、完整 turn 单位、连续 suffix/evidence 顺序及“current sources + projection USER + exact active USER + summary USER”形状。current active 从历史中排除，使用原 kind-specific lowering 单独完整追加；PLAN/terminal 等来源不改成 HUMAN。先尝试带 prior 的历史 backbone，再按原序尝试较短连续 suffix；纯文本候选在 quote 前统一执行 P，不能混合搜索原图/部分省略版本。只有原允许的实际 token/W 不足才继续下一 suffix；引用损坏、C/L/N 或 invariant 失败不能冒充这个条件。

同一 stable fenced capture 中仍是一个逻辑 B summary（仅保留原有限 repair）、固定 recent 和唯一实际 successor。summary 不执行工具，projection 不直接安装；PRE_FULL 通过后才沿原 adoption 事务推进，POST_FULL 重建复验。两档均保持原 B_trigger 与 hard admission，不要求 ordinary 0.55 target；失败不增加第四档、模型调用循环或绕过主开关。模型选择仍只作用于下一 NEW_TURN 的首次主调用，不热替换已产生输出或工具效果的旧 turn。

master compaction.enabled=false 时仍只有 Tier 1；图片或其他边界使直接切换不合法，则返回原 MODEL_SWITCH_REQUIRES_COMPACTION。automatic_enabled/manual_enabled 不关闭已有 handover。普通同模型 compaction、视觉目标的 recent=3 规则和所有已安装前缀保持原契约。

原 DestinationDialogueEntry 保留 role/requested_tools，将 text 改为同一完整 content 并携带 item_kind/input_origin；prior_handoff 复用相同 request 值。无需新 IR、图片状态、独立许可或 projection fingerprint。

### 8.7 Retained historical 的覆盖与连续压缩

普通 summary 实际覆盖 predecessor snapshot base 时，其 retained historical 已作为原 typed 内容进入模型输入，成功 successor 可按原规则清空；未覆盖时机械完整带入。不能按 digest 合并独立请求或把历史提升为 active。

Tier 3 的 cut 到达 safe head，不等于模型读过未选中的 prior。继续用 `projection.prior_handoff is not None` 判断真实 coverage：选中 prior，summary 可覆盖其实际输入内容；没选中，successor 必须机械保留旧 mandatory historical。纯文本 B 的这份保留值为 P(旧 historical)，不能恢复 raw Image，也不能仅因 recent=0 清空它。最终 successor 的真实内容全部计入 D1/D2。

失败或未采用的 summary 不改变 predecessor、旧 refs 或有效 binding。原 recent 不升级为 mandatory historical。已成为普通省略 Text 的历史按原文字保留/摘要规则处理，不解析标记找回图片，也不新增需要跨所有后续压缩永久传播的省略状态。

### 8.8 Fork 与 cold rebuild

Fork 沿既有 historical anchor、scope、effective binding 与 REPEATABLE READ 读取完整被选中内容；不读取 anchor 后父会话追加的图片或后续 summary。entry/snapshot/body 与 child refs 同事务写入，图片 blob 仅在同 workspace 共享。父 owner 的存在不作为 child 读取 authority。

source active 转为 child 的 retained historical，保留原 kind/origin/完整 content，不复制 active execution identity；child carrier 为 AWAIT_NEXT_USER。既有 historical 保留原顺序和次数，包括已经是普通 Text 的省略标记；不从 summary 猜内容。Fork-of-fork 对当时 child 的有效来源做相同复制。child-local 序号重映射不改内容身份，只为真实 Image 建立 refs。

Fork 是历史复制，不为适配 child 模型或 G 余量预先压缩/删图；沿原有界读取和完整性规则执行。随后首次 NEW_TURN cold dispatch 再按实际 B 与第 8.6 节交接；没有可用 A installed epoch 时按第 8.9 节复用 Tier 3。已经省略的图片不从退出的父/旧历史自动恢复。原父/子正文继续各自持有真实图片 refs，关闭不等于删除 owner。

### 8.9 普通文字替换、冷启动与采用边界

唯一替换在原 compaction/source owner 中完成，不在 adapter 内改 payload：

```text
P(Text(t))    = Text(t)
P(Image(...)) = Text("[图片已省略]")
P(parts)      = 按原顺序逐项应用，不合并或去重
```

P 只用于第 8.6 节纯文本 B 的 Tier 3 历史 projection，以及该 successor 必须携带的历史内容。保持原 kind/origin；当前 active 仍完整，公开向已知纯文本模型新提交的图片继续按入口限制拒绝。Text 原值与边界不变，包括用户自己写的相同标记；不 OCR、不补图片描述、不解析文字赋权。重复图片留下重复标记，纯图历史变为非空 Text，P 再次执行也不会增加标记。继续复用 FrozenPromptContent、pulsara.prompt/v1 和原 codec，不增加 part、历史专用 schema、许可字段或通知字段。

只对实际选中的完整历史 request 逐 occurrence 替换。原 Tier 3 整体淘汰的 prior/turn 不必留下逐图标记；prior_handoff 仍只携带原 summary/recent/historical，不把旧 snapshot-active 升格为新的 mandatory。recent=0 不妨碍 B summary 读取原 projection 中选中的历史文字；它只是让这些原话不再额外回填最终 successor。

重启或 fork 后可能没有旧 installed epoch：完整 B cold input 若含不兼容历史图片，就从当前 canonical 来源直接复用相同 Tier 3 summary/adoption，不伪造 A、不重建它的执行状态。原 trigger/准备入口只需允许“无旧 epoch、无 A target”的冷来源；若本来有 epoch 却丢失其 owner，则仍失败。没有第四种 compact、独立队列或恢复机制。

现有 prepare_compaction_source() 会按 target override 编译原 source，不能在 P 之前用 B 的执行模态校验堵死这条路径。沿原 safe-point/reader 先完整验证来源、refs、scope/cut 与物理读取界，再派生 P 后的 B candidate，最后由原 compiler/adapter 做完整目标与实际 wire admission；原来源的临时 quote 不取得 open/install 权限。不复制 compiler 或 source owner。

来源证明比较完整保留值是否等于同一冻结来源的 P，当前 active 仍 exact join。Tier 3 覆盖同一 exact safe head，不留下未处理的 raw-image suffix；mandatory historical 用相同 P，避免 summary 无图、successor 却重新塞图。原 source 的 hydration/并存成本不免除，P 后的真实 Text/body 按 D1/D2 计量；图片字节、base64、视觉 token 不再收费，标记文字正常收费。原三档的 PRE_FULL、一次原子 adoption、POST_FULL、取消与失败结算全部保留。

最终 snapshot 保存的是 summary 与实际保留的普通 Text/Image 内容。被替换的图片不产生新 refs；原 entry/queue/旧 snapshot 的正文、refs 和完整命令确认均不变，历史 UI 仍能查看原图。重启、再次压缩和 fork 机械使用这份有效内容；切回视觉模型也不自动复活退出的图片，用户可重新附图。不保存“曾经省略过图片”的永久状态或增加证明它发生过的事件。

失败不推进有效 binding/snapshot/context pointer 或旧 epoch；已保存的模型选择仍按原产品语义保留。模型选择和命令接纳不等于 handover 已成功，UI 使用原执行结果显示失败，无新增批准或回滚流程。

## 9. 预算：保持统一本地估算，分清 token 与字节

实施前的 `PulsaraHeuristicTokenEstimatorV1` 对 final wire JSON 按字符数估算。如果原样加入 data URL，base64 文本长度就会被当成模型输入 token；简单跳过该字符串又会把图片成本算为零。两者都不能作为图片支持的完成状态。

本次继承 final-wire 规范：**仍使用 Pulsara 统一、本地、确定性的 estimator**。不增加厂商 tokenizer、token-count 请求、provider usage admission、按模型名匹配的视觉成本表，或请求失败后的预算学习缓存。

需要同时保留三个不同量：

| 量 | 正确用途 |
| --- | --- |
| 图片原始字节与解码尺寸 | 内容验证、内存及单次操作物理边界 |
| 最终 context-bearing JSON 的 UTF-8 字节数 | 现有 `final_wire_utf8_bytes`；包含实际 base64，不能以缩略图或占位符替代 |
| 含图片的本地输入 token estimate | compiler 选择、final-wire admission、compaction trigger 与 reclaim |

`final_wire_utf8_bytes` 当前计量 context-bearing projection，不是整个 HTTP 请求的全部字节；transport controls 的边界仍按原规范处理，不借此偷换计量口径。

新的图片估算必须满足：

1. 在正式图片 wire part 的语义位置识别图片，不能扫描任意字符串或任意名为 `image_url` 的 JSON 字段就忽略其文本成本。用户引用的 JSON、工具正文、schema 示例仍按实际文本计量。
2. 给图片计入非零的、明确说明局限的本地视觉估算；该算法不声称复现所有 provider 的真实视觉 token。
3. 直接遍历最终 wire materialization 能得到同一个 quote。不能只在 semantic estimate 上另加一个 adapter 看不到的图片附加值，也不能用 side table 成为第二份计量 authority。
4. 保留 generic component、native replay replacement 与 direct final traversal 的一致性断言；normal dispatch、compaction dry run 和 post-adoption gate 共用算法。
5. 算法及其现有 estimator contract/version 同步更新；不为图片新增 fingerprint 体系，不通过热更新改写已冻结绑定。

**D1 已经用户确认并冻结：采用允许适度低估的统一本地默认值。** 本节的公式、参数、取整顺序、逐 occurrence 计量及 final-wire payload 替换规则共同构成 D1 正式契约。此前候选仅作为实验对照，不作为实施选项或运行时 fallback。选择同时考虑容量损失、低估幅度和已读开源处理机制，不要求所有样本零低估。完成第 12 节实施验收前仍不激活生产图片输入。

### D1（已冻结）：28 像素网格乘 7/8，单图最低视觉估值 256

对每次实际出现在最终 wire 中、已通过 D2 验证的单帧图片，取其原始字节对应的正整数宽高 `W, H`，单位为像素：

```text
cells = ((W + 27) // 28) * ((H + 27) // 28)
V(W, H) = max(256, (7 * cells + 7) // 8)    # 单位：本地估算 token
```

即 `max(256, ceil(7/8 × ceil(W/28) × ceil(H/28)))`。全部使用整数算术：先分别对边长向上取整，再相乘、乘以 7/8 并向上取整，最后应用 256 最低值。不能先算总面积除以 784，也不能将整批图片合并后只取整或应用一次最低值。同一 blob 出现多次就独立计量多次，不按 digest 去重。

256 是视觉估值下限，不是尺寸/接纳/图片数量的限制。7/8 只作用于视觉网格，不作用于文本、MIME、wrapper、framing 或任何字节数。不加视觉 token 截顶，不模拟 provider resize，不修改图片，也不按模型名、压缩率、缓存命中或上一次 usage 调参。

**参数依据与误差选择：**

- **28** 保留近期 Kimi K3 / MiniMax M3 的 `14 × 2` 有效空间网格，以及非方图 padding 的行列差异。只在最后折减位置数，不使用更粗网格隐式删除行列。已读 Kimi 尺寸函数对 4096×57 返回 441 个视觉位置；`32 网格 × 1.2`（最低 256）给 308，选定公式给 386，分别低估约 30.16% 与 12.47%。这是纯尺寸函数对照，不是 Kimi API 实测。[Kimi K3 源码](https://huggingface.co/moonshotai/Kimi-K3/blob/main/media_utils.py)、[MiniMax M3 源码](https://huggingface.co/MiniMaxAI/MiniMax-M3/blob/main/image_processor.py)
- **7/8** 是允许适度低估后的明确预算策略，不是任何 provider 的真实 token 系数。12.5% 折减避免完整预留较密网格，同时小于当前 `auto_trigger_ratio=0.85` 对应的 15% 阈值余量。若整份输入均满足估算不少于实际的 87.5%，则本地 85% 对应实际约 97.1%；这只是选择幅度的参考，不是安全证明，不能忽略文本误差、未知视觉配置或一次新接纳增量。当前实测中 0.8 / 0.75 系数的最大低估约为 18.53% / 23.51%，不采用。[当前 compaction policy](src/pulsara_agent/conversation_kernel/compaction/contracts.py)
- **256** 保留适中的小图预算。已读 Gemma 4 的 280 档位在方图上产生 256 个位置，本轮尺寸集合中该档位最多为 279；128 最低值可能将这种机制低估一半以上。比最低值 224 多预留 32，仅使本次去重矩阵的估算总和增加约 0.81%，却将该档位的纯视觉最大低估从约 20% 降至 8.24%。它不是某个 API 小图 usage 的精确拟合值，也不是全家族默认预算。[Gemma 4 processor](https://github.com/huggingface/transformers/blob/main/src/transformers/models/gemma4/image_processing_gemma4.py)、[31B-it 配置](https://huggingface.co/google/gemma-4-31B-it/blob/main/processor_config.json)

**已知例外必须保留：** Gemma 4 的 1,120 高预算档位会将小图放大，本轮纯尺寸计算出现 1,118 个视觉位置，公式只给 256，短缺约 77.1%。这不能称为小幅低估，也不能宣称所有部署都有 11% 或 12.5% 的误差上界。当前明确选择一个统一默认值，避免用该高预算档位抬高所有服务的小图成本；没有按模型分支或自动扩大最低值。未知部署的图像预算无法只从原始宽高恢复，这是统一启发式的适用性限制。[官方预算说明](https://ai.google.dev/gemma/docs/capabilities/vision)

源码读取范围、辅助函数而非完整模型执行的限制，以及全部候选与误差见 [D1 综合评估](scratch/vision-token-research/20260913/d1_balanced/decision.zh.md)。这项选择不声明统计上唯一最优，也不将 provider usage 或开源 tokenizer 纳入运行时 admission。

**Final-wire 的替换与汇总规则：**

1. 只在已验证的正式 USER 图片 part 位置识别：Chat 的 `messages[i].content[j]` 中 `type=image_url` 的 `image_url.url`；Responses 的 USER message content 中 `type=input_image` 的 `image_url`。实际 adapter 的 ordered item 保留同一角色、类型与嵌套位置约束。任意 text、tool result、tool arguments、schema、SYSTEM 或字符串内的同名 JSON 均不享受图片扣减。
2. 图片必须是本设计固定 `detail=auto` 的合法 inline data URL。真实 wire 完整保留 `data:<mime>;base64,<payload>`。**仅在 token 计数时**，将逗号之后的 base64 payload 视为长度零；保留 `data:<mime>;base64,`、引号、字段名、`type`、`detail`、数组、消息及其他全部 JSON 内容。这个计数视图不发送、不持久化，也不是第二份内容 authority。非法、未验证、无法恢复可信尺寸的正式图片 part 走既定内容校验失败，不按零图片成本或普通文本继续发送。
3. 对第 `i` 个最终 ordered wire item，记按上条规则计数后的 canonical JSON 的 Python code point 数为 `C_i`。沿用现有 JSON/消息 framing 常数：`Q_i = 4 + (C_i + 1) // 2 + Σ V(W,H)`；求和只包括该 item 中的每次图片出现。先汇总该 item 的 JSON 字符再向上取整一次，不能每个 key/part 单独取整。没有图片时行为与现有 JSON primitive 相同。
4. 最终 quote 为 `3 + estimate_json(fixed_context) + Σ Q_i`。`fixed_context` 仍是既有 adapter-owned、ordered input 数组为空的 exact context-bearing projection；不把 output budget、timeout 等 transport control 拼进来。fixed context 中没有首版合法图片位置，其中的文字或 schema 仍完整计数。
5. 最小解释分项为 `text_and_framing_tokens` 与 `visual_image_tokens`，二者均为本地估算 token，合计保持上述整数算式。保留原有分项/owner 即可，不新增 durable quote 或计量 registry。semantic compiler 的图片分项共用 `V`，但其文本表示不同，**不要求完整 semantic estimate 等于 final-wire quote**。最终 admission/reclaim 只消费后者；generic wire component、native replay replacement 和 direct final traversal 的等式继续成立。
6. 尺寸必须能从该次遍历的真实图片内容，经 D2 的共享、受资源约束的验证路径得到；不能仅相信调用方填入的 `width/height` 或旁路表。可以复用随完整冻结内容携带的可信验证结果，但独立遍历 exact wire 必须恢复相同值。不得因此无界重复解码像素或额外物化整份 base64 副本；D2 仍拥有验证工作内存、hydration 和取消边界。
7. 实施时将现有 estimator contract 一次性更新为 `pulsara_heuristic/v2`，在已有 `TokenEstimatorFact` 中纳入 `image_grid_pixels=28`、`image_scale_numerator=7`、`image_scale_denominator=8`、`image_min_tokens=256`、正式位置识别、payload 扣减和逐 item 取整规则。纯文本常数不变；移除生产 V1 路径，不增加新 fingerprint 系统或兼容双计量。已冻结绑定不能热切参数；按既定冷 epoch / 已采用 successor 边界和实现 hard cut 契约处理。

**边界与实例（仅视觉值 V，不含实际文本/wrapper）：**

| 输入 | 28 像素网格 | V |
| --- | --- | ---: |
| 31×31 / 128×128 / 256×256 | 2×2 / 5×5 / 10×10 | 各 256 |
| 448×448 / 449×449 | 16×16 / 17×17 | 各 256 |
| 476×476 / 477×477 | 17×17 / 18×18 | 256 / 284 |
| 512×512 | 19×19 | 316 |
| 1008×1008 / 1009×1009 | 36×36 / 37×37 | 1,134 / 1,198 |
| 1024×1024 | 37×37 | 1,198 |
| 1920×1080 / 1080×1920 | 69×39 / 39×69 | 各 2,355 |
| 2048×2048 / 4096×4096 | 74×74 / 147×147 | 4,792 / 18,908 |
| 4096×57 / 57×4096 | 147×3 / 3×147 | 各 386 |
| 同一张 1024×1024 出现三次 | 每次独立计算 | 3,594 |
| 同一张 128×128 出现八次 | 每次独立应用最低值 | 2,048 |

相同原始尺寸的压缩率、PNG/JPEG/WebP 编码及噪声内容不改变 V；不同 MIME 和 wrapper 按实际字符计量。文本中的 data URL 保持全文本估算。跨消息每个 item 单独计算 JSON 取整/framing，不承诺完整 quote 与合并为一条消息相等。

**验证与接受的取舍：** 四组稳定配置的 full + 定向补测共 368 次请求，含 344 条图片观测和 24 条纯文本基线；本次仅离线重算，没有新增 provider 请求。按模型、尺寸、图片次数、消息布局折叠重复与格式/内容控制后为 152 例；候选比较是后验描述，没有独立留出集。比较的量是 `Q(图像请求) - Q(匹配文本基线)` 对 provider input usage 增量，包含实际 wire wrapper，不把它声称为真实视觉序列长度。

- 全部 344 条中，66 条低估，4 条超过 10%，没有超过 15%；最大低估为 Muse 的 1080p 横/竖图，2,396 对 2,693，短缺 297（11.03%）。去重后为 28/152 条低估、2 条超过 10%。
- 最大绝对短缺为 Muse 的八张 1024 方图：9,912 对 10,968，短缺 1,056（9.63%）。低估按实际 occurrence 累积，不能靠存储去重抵消。
- 全部样本的估算总和 / 观测总和从 1,120 旧候选的 1.986 降为 1.450；去重后从 2.052 降为 1.479。总和比值不是平均误差，也不预测生产流量分布。
- 对已审阅纯尺寸函数的 842 个有限组合，Qwen3.8 发布配置无低估，Kimi K3 与 MiniMax helper 默认值最大低估 12.5%，Gemma 4 的 280 档位最大低估 8.24%；1,120 档位例外如上。该集合不是 D2 的生产尺寸上下限，也不是 842 次完整模型调用。

本次 PNG 单图 wrapper 增量为 41（派生值，不是新常数）：512 方图本地增量 357，实测 184–363；1024 方图本地 1,239，实测 652–1,371；4K 方图本地 18,949，实测 994–19,661。4K 在 DeepSeek 上仍高估约 19.06 倍，在 Luna 上低估约 3.62%。不能一面要求 Luna 上仅小幅低估，一面又用统一尺寸公式把同图估到接近 DeepSeek 的 994；不通过硬截顶或隐藏的次线性缩放把这种差异转为大幅低估。

**本版按上述明确局限冻结 D1 默认值。** 当前实测全为 Chat Completions；Responses 只有离线 wire 控制验证，不宣称已完成真实图片链路。D2–D4 设计见相应冻结条款；实施验收仍须验证合法边界、最大 multipart/reinjection/hydration 增量、混合内容、重复引用、generic/native/direct quote 等式以及 normal/compaction/pre-adoption/post-adoption 的实际 kernel 路径。独立 probe 不代替这些验收。

现有 [kernel limits](src/pulsara_agent/conversation_kernel/limits.py) 分别限制 prompt、ToolResult、blob、hydration 等不同资源。不能直接把 `prompt_hard_bytes` 当图片限制、把 `canonical_blob_hard_bytes` 当 provider 许可，或为通过图片样例无依据放大它们。新增边界只能是有依据的单次操作/物理边界，不设累计图片数、历史数或会话寿命上限。

### 9.1 D2 同时拥有 multipart admission 与 compaction headroom 推导

2026-09-12 调用 `resolved_compaction_headroom_bounds()` 核实：canonical reader 上限为 **16 MiB**，continuity epoch logical bytes 上限为 **64 MiB**，两者当时 byte reserve 均为 **4 MiB**。这个 16 MiB 是 canonical 读取预算；即使它与当时单 blob 上限数值相同，也不是同一个资源契约。

当前 reserve 来自 `max(prompt_hard_bytes, maximum_completed_assistant_message_bytes, tool_result_hard_bytes)`。图片提交的最大合法增量若不进入该推导，旧 soft boundary 以下的一次接纳就可能使下一次 source reader 越过 hard boundary，导致压缩来不及启动。只验证单图能发送不能证明此边界正确。

D2 必须为**一个完整 multipart 提交及一个实际 admission 批次**定义以下计量；批次可能包含多条 queue/steer 输入，不能只验证其中最大一张图：

- 全部 text UTF-8、图片编码字节、part/reference metadata 的总量；不能只限制单图。
- reference hydration 所需的完整图片字节与解码内存；metadata-only headroom preflight 必须从可信 canonical 描述计入被引用内容，不能只数短 blob ID 或 manifest 的字节。
- 相同图片重复出现时的逻辑内容计费，以及并发 hydration 的物理内存峰值。存储去重不消除 prompt 中的重复出现，不得据此少计逻辑输入。
- compiler canonical working set、continuity epoch logical bytes 和含 base64 的 final-wire bytes 各自计量定义；不把一种量原样充当另一种量。若旧 `*_utf8_bytes` 字段扩展到二进制语义，必须显式修订命名/契约及全部消费者。
- 当前已有内容、待接纳输入、successor 的 root/runtime/source reinjection 和最大合法 assistant/ToolResult 增量如何分别参与原 admission 和 compaction 规则；视觉 token quote 继续独立计算。

执行窗口的结算预留必须由接纳路径机械推导，而不是调节常数直到测试通过。设 `Δcanonical(a)` / `Δepoch(a)` 为两次可阻止新增内容的预算检查之间，已经允许执行的路径 `a` 在对应计量中的最大不可拒绝增量，则：

```text
R_canonical >= max_admitted_path Δcanonical(a)
R_epoch     >= max_admitted_path Δepoch(a)
current_charge + R <= corresponding_hard_boundary
```

第 9.3 节冻结契约明确区分 `G`（dispatch 前最低服务余量）和 `R`（已允许执行窗口的结算上界）：图片经过完整输入检查；provider 响应也须经过 canonical/effect 之前的输出检查。4 MiB 作为 `G_C/G_L` 保留，不再用 `max(assistant, 单个 ToolResult)` 宣称它能覆盖整轮工具结果。周期性 soft boundary 可继续按 `hard - G` 提前触发，但候选接纳要计完整 `S_B + G`，输出检查则按 actual assistant 和实际整批 result/closure 上界证明 `R`。不新增 durable quote 或通用阶段框架。

保留原 item headroom，并验证 multipart/batched admission 对 item 与 part working set 的影响。若合法路径使剩余额度无法容纳候选及必要输出，D2 必须调整本次接纳或有依据的物理预算，不能接受一个永远无法编译的输入，也不能用任意总图片数上限掩盖矛盾。

`reader` 的 metadata-only preflight、compaction planner 的 soft trigger、queue/steer 实际接纳点、continuity 的 logical byte 检查、cold successor/reinjection 与 final-wire admission 必须共用一致的边界定义。需要在原调度点预留或先进入 compaction；不能等图片进入有效历史、reader 已越界后才补做 preflight。冻结公式和数值见第 9.3 节。

### 9.2 Quote 分项必须保留单位与计量对象

OpenCode 调研提出的“分项可解释”值得采用，但不能把 `encoded_image_cost`、`visual_image_cost`、decoded memory、canonical、epoch 和 final-wire 拼成一个没有单位的 total。D1/D2 应给出现有 quote 的最小必要分项，说明每项的单位、输入对象、归属及超界原因；不用新建一份 durable quote 或独立证明记录。

- **Token 维度**：文本与 framing 的本地估算，加每个实际图片 occurrence 的非零视觉估算。真实 base64 仍存在于被遍历的 final wire，token 计数按第 9 节保留的计量契约只扣除正式图片 payload 文本，保留 wrapper 并逐 item 取整；不将完整 base64 文本 token 与视觉 token 重复相加。D1 的最终数值参数按第 9 节执行。
- **物理维度**：分别测量压缩图片字节、像素解码峰值、canonical hydration、epoch logical bytes 和 wire bytes。即使都用 bytes，它们也不是同一个工作集，不能相加后对任意一个 hard bound 比较；同一图片的存储去重与逻辑重复出现分别计量。
- **Headroom 维度**：第 9.1 节的 `R >= max Δadmitted_path` 对各资源 owner 分别成立；`G` 不是这项结算上界。并发峰值、串行复用、批次和互斥路径按真实生命周期推导。不能把 pending、successor、assistant、ToolResult 的上界无条件叠加，也不能漏掉同窗口会共存的输入。

统一算法要求 semantic/final-wire/replay/compaction 遵循同一 token 契约；不要求 semantic token 数与 canonical 字节数相等，也不让 token estimator 接管全部物理计数。不得从 OpenCode 的 `round(string.length / 4)`、provider usage 或 URI/占位文本估算反推 Pulsara 图片参数。

### 9.3 D2（已冻结）：动态输入接纳与效果前输出检查

**状态：本轮用户明确指定的冻结基线；K1 已实现计量纯契约与 G 常量，K2 已实现 Host 图片验证、canonical metadata preflight 与 hydration，K3 已实现动态输入接纳及效果前输出检查。** 本节将第 9.1 节具体化：保留原硬边界，图片单独验证，完整 multipart 动态接纳；完整模型输出在进入 canonical 和任何工具效果前再检查，G 与执行窗口的 R 分别计量。以下常数、两次接纳公式、明确失败语义及 D3/D4 必要实例化组成同一方案。

依据是 [D2 本地资源实验报告](/Users/plumliu/Desktop/python_workspace/pulsara_agent/scratch/image-resource-probe/20260913T053759102238Z/report.zh.md)：666 次独立进程实验、333 组重复；原有 214 张素材全部完成两次解码。普通 4096² PNG/JPEG 的峰值 RSS 增量约 64 MiB，渐进式 JPEG 约 112 MiB，透明 WebP 约 265 MiB；8 路大 WebP 约 2 GiB。64×64 PNG 还可以携带解压后 64 MiB 的文本。这些是本机有限样本的结果，不是所有输入的内存上界。

#### 9.3.1 可接纳图片：格式与资源分开判定

| 项目 | 推荐规则 | 依据及产品含义 |
| --- | --- | --- |
| 实际格式 | 静态 PNG、JPEG、WebP | Pillow 识别并完整验证；声明 MIME 必须与实际格式对应，文件扩展名不是依据 |
| 帧 | 完整验证确认有效帧数恰为 1；识别为 APNG 时拒绝 | 不取多帧的第一帧。GIF、TIFF、APNG、多帧 WebP 不接纳；不承诺识别 Pillow 未公开区分的单帧 WebP 容器标志 |
| 单图像素 | `1 <= W × H <= 16_777_216`，宽高均为正整数 | 采用已补测的 4096² 面积档位；覆盖 A4 300 DPI 的约 870 万像素，控制 32 MiPixels WebP 实测约 513 MiB 的解码工作量 |
| 比例、DPI、单边 | 不设比例白名单，不额外发明统一单边上限；遵守对应 codec 自身限制 | 16:9、A4、照片及极端横竖图均按实际像素判定；4096 是面积示例，不是最长边上限；DPI 不进入资源公式 |
| 文本 | 一条提交中所有 Text parts 的 UTF-8 合计仍不超过现有 1 MiB | 不因拆成多个 Text part 绕过原 prompt 文本边界 |
| 完整提交初筛 | `M(request) <= 16 MiB`，定义见下一节 | 继承 reader 对一个完整不可拆内容的必要边界；这是初筛上界，不是“单图保证支持 16 MiB” |
| PNG 文本元数据 | `MAX_TEXT_CHUNK = 1 << 20` bytes；`MAX_TEXT_MEMORY = 1 << 20` 按 Pillow 文本计数单位 | 前者限制有界压缩块解压；后者累计文本值长度，iTXt 按 Unicode 字符计数，不能写成累计 UTF-8 或 RSS 字节上限 |
| 原始内容 | 保留原文件字节、MIME、顺序与次数 | 不缩放、转码、去元数据、修改 EXIF 方向或自动换格式 |

透明、调色板、灰度、16 位 PNG，普通/渐进式/CMYK JPEG，以及静态透明/无损 WebP，在满足相同边界且解码成功时允许进入本地内容契约；不另按颜色模式静默转换。本地验证通过不保证每个远端 endpoint 接受全部模式，远端拒绝继续沿原错误路径返回。

这两项 PNG 参数是特定解码路径的限制，不能写成“所有图片元数据或所有解压内存最多 1 MiB”。[冻结前单位补测](/Users/plumliu/Desktop/python_workspace/pulsara_agent/scratch/d2-freeze-review/20260913/metadata-results.json) 的 5 次独立 worker 验证：累计 1,048,576 个 ASCII/中文/emoji 字符分别对应 1/3/4 MiB UTF-8，均可通过；超过字符计数阈值的样例被拒绝。这 5 次不并入原 666 次资源实验。保留库的计数规则，不复制 PNG parser，也不把 `image.text` 中覆盖重复 key 后的最终字典当作累计解压量。

当前 Pillow 的 PNG ICC 解压也使用其有界解压函数；其余原始 EXIF/ICC/XMP 占用编码输入额度，本路径不调用 EXIF 展开、色彩转换或 OCR。codec 可能忽略部分无效辅助 metadata；这里保证的是所选解码器的识别/验证契约，不宣称实现了全部格式的严格一致性审计。

#### 9.3.2 五种成本的准确口径

设一个 image occurrence 引用的原文件长度为 `E_i`。D3 的 body 表达文本及有序图片描述/引用，图片 payload 放在现有不可变 blob 中；不得把同一 payload 既内嵌又引用。推荐计量如下：

| 成本 | 定义与责任 owner |
| --- | --- |
| 完整 multipart `M` | 该提交的确定性 canonical body 字节数，加每个 image occurrence 的 `E_i`；body 已包含文字、引用、MIME、可信尺寸和必要 metadata，文字不再重复加一次。Host ingress 负责初筛 |
| Canonical expanded admission charge `C_charge` | 当前选中的完整 snapshot/carrier 与 suffix，含图片描述及逐 occurrence 的编码字节；覆盖原 reader 的实际有界内容读取/参数展开与逻辑内容检查。包括 base snapshot 的图片，不能只看 post-base。即使物理读取去重，逻辑 charge 不减；它不是 RSS |
| Epoch logical `L` | 延续原 provider-neutral 字符串标量计量；每个 image occurrence 加 `E_i + len(media_type.encode("utf-8"))`。首版 `detail="auto"` 仅为 wire 固定字段，进入 W/D1 framing，不为 L 额外制造 JSON 包装；宽高、blob ID 也不冒充 provider 输入。continuity 拥有唯一计量函数 |
| Final wire `W` | 最终 context-bearing projection 的实际 canonical JSON UTF-8 长度；图片 payload 精确为 `4 × ((E_i + 2) // 3)`，再加真实协议包装和 JSON 转义。既有 final-wire owner 负责，仍不是整个 HTTP 请求长度 |
| 验证/编译实际内存 | 编码输入、像素、metadata、codec 临时副本、hydration、base64/JSON 物化及旧/新 plan 的实际存活重叠；由各物理 owner 控制生命周期，不能从 `C`、`L` 或 `W` 直接等同出 RSS 上限 |

`M` 的 16 MiB 初筛由 `C_charge` 的 16 MiB reader 硬边界推导：单条不可拆提交自身展开后已经超过该接纳上限，就不存在合规的完整读取路径。D3 精确 body 编码决定实际包装字节，不能用假定的固定 metadata 额度代替。普通纯文本消息的 wire golden 和原 prompt 文本上限保持，新 snapshot/projection 的展示按第 8.5 节；D3 存储形状及二进制扩展涉及的 `*_utf8_bytes` 命名/消费者一次 hard cut。

metadata-only `C_quote` 必须来自同一 cut、同一完整选择范围，满足 `C_quote >= C_charge`，不要求恒等。当前 reader 的 post-base TOOL_RESULT 预测包含重复投影和 envelope 的保守量，而实际读取、tool arguments 与完整 base snapshot 又有各自的计费；实施时必须保留这些真实边界，不能拿最小的一个计数替代全部检查。保守 quote 过大时可在既有有界读取条件下精化；不能仅凭松上界把输入宣称为数学上永久不可容纳。

同图出现三次，`M/C/L/W` 在各自真实选择范围内计三次。存储、读取与 bytes/data URL 对象可以共享；同一 request 在 carrier 和 suffix 中的两个存储位置也不能自动变成两次 provider placement。逻辑 occurrence 由选中内容及 placement 决定，物理完整性检查则覆盖所有实际引用。

#### 9.3.3 图片验证：Host 共享一个物理执行槽

推荐由现有 `KernelHostCore` 持有一个 process-local 图片验证槽，所有 session 共用；每个 admitted multipart 在一个短生命周期子进程中逐图验证，始终只保留当前图的像素，结束后退出进程。每次只把该提交的有界原始内容交给 worker；其余候选通过原调用/排队机制等待，不预先展开整个 pending 队列。它是并发限制，不是只允许一个会话、一个任务或一张图片。

选择单槽的理由是：实测大 WebP 在 2 路就约增加 513 MiB，8 路约 2 GiB，而 A4/4096² 单图实际解码通常是几十毫秒。首版先控制这项新增的集中内存负载，不使用未经校准的“每像素 4 bytes”权重调度，也不直接复用 blob hydration 的 4/8 路并发。进程启动和完整 kernel 的吞吐仍须在实施时实测，不能把实验中的纯解码耗时当作用户延迟。

复用边界必须明确：Pillow 拥有格式识别、帧判断和解码，Python 标准库拥有子进程创建/通信/等待；Pulsara 只负责允许的输入、单槽调度、验证结果及清理。当前 `KernelSessionIO` 是会话级线程执行器，取消后会等待线程实际结束，不能终止 codec；已有 Terminal `ProcessRegistry` 则绑定终端身份、输出和进程产品生命周期。因此推荐增加的是 Host 内的最小无副作用 validator worker，不复制 Terminal 管理器，不新增 durable job、terminal、事件、lease 或恢复状态。

worker 在首次打开图片前固定设置 PNG 文本限制，避免在主进程临时修改 Pillow 全局常量。路径为：有界输入 → 限定格式识别 → MIME/动画/帧及正整数宽高检查 → 像素检查 → `verify()` → 关闭并释放头部对象 → 重新打开并 `load()` → 释放像素。返回 MIME、尺寸及单帧等必要事实和原内容的对应关系；不返回像素数组，也不保留整批 `Image` 对象。

帧检查使用库的真实能力：PNG 的 `get_format_mimetype()` 识别出 `image/apng` 时拒绝，包括单帧 APNG；WebP 以完整验证后的有效帧数恰为 1 为准。Pillow 给静态 WebP 也提供 loop 信息，而 `is_animated` 依赖帧数，不能借这些字段声称可靠识别全部单帧动画容器。出现“退回默认帧”等解码警告时不静默放行。宽高等描述取得后重新确认完整 `M`，补齐 metadata 不得绕过总量检查。

采用原 ingress 的操作 deadline 与取消语义，不照搬实验脚本的 30 秒常数。等待或验证超时是本次操作未完成，不把图片标成永久损坏。取消、deadline、worker 异常及 Host close 均须先结束并 wait/reap 实际 worker，再释放槽和返回；不因取消在后台继续跑解码。正式实现应验证启动、通信中断和关闭时的这些路径。

**本版硬检查的是编码字节、像素、PNG 文本与同时解码数；没有把 265 MiB 或人为加余量后的 512 MiB 声称为操作系统强制的 RSS 上限。** 子进程隔离支持清理和终止，也不自动构成内存沙箱。本实验没有证明所有恶意文件的最大内存。若产品另需“无论输入如何，整个 Host 都不超过指定 RSS”，需定义并验证平台资源隔离边界；不能由一个经验公式代替。

#### 9.3.4 验证一次，后续读取编码字节

原 ingress 在 Hook reservation / 命令候选冻结前完成合法性、完整提交初筛及验证；发布者将可信 MIME、尺寸、文件长度与同一不可变内容关联。具体关系形状由 D3 在已有 canonical owner 中定义，这是内容真相，不新增验证 receipt 或图片 fingerprint。

正常 reader、compaction、fork 和重试使用 canonical 内容与其完整性检查；不每次 hydrate 都重新 load 像素。冷读取必须验证引用长度及已有 blob digest，不能仅凭数据库里一个宽高值接受不同的 bytes；错误返回原内容完整性失败，不继续发送。纯编译器和 adapter 不取得解码/数据库职责。

D1 独立 wire 遍历若需要恢复可信尺寸，仍走共享验证路径并受同一槽约束；已经携带同一份完整 frozen 内容及可信验证值的遍历可以复用，不能凭空信任调用方填入的尺寸。禁止为每个 quote 或重复 occurrence 无界复制 base64、重复保留像素；不建立跨请求的 fingerprint→object 注册表。

blob 读取继续由原 I/O owner 执行，按可信 metadata 先选定有界批次，取得编码 bytes 即可。候选、当前 source、旧/新 plan 同时存活的物理副本仍要进入原 compiler/planning working-set 生命周期检查；compiler 的逻辑 64 MiB 不声明为 Python 进程的 64 MiB RSS。final-wire 候选失败或仅保留 quote 后及时释放物化结果。

#### 9.3.5 两个接纳点：输入检查与输出结算检查

保留 canonical expanded admission charge **16 MiB**、epoch logical **64 MiB**、final wire **64 MiB** 与现有 item 硬边界。新增图片不引入会话总图片数、总工具调用数、任务寿命或累计重试次数上限。

**4 MiB 的角色改为最低服务余量 `G`，不再声称它覆盖整轮 assistant 与所有工具结果的最大增加量。** 现有 runner 会先 settle assistant，再执行整个工具批次，之后才回到 provider safe point；而 open tool batch 中不能压缩。普通 tool result 的 canonical preview 上界为 65,536 bytes。仅 `C = 12 MiB - 1`、8 KiB assistant/参数和 64 个达到该上界的结果，就会在下一 safe point 前超过 16 MiB。把 `max(assistant, 单个 ToolResult)` 当作整条执行路径的上界不成立。

本版采用两个已有 owner 内的检查点，不新增通用阶段框架：

**输入检查。** 令 `S_B` 为当前完整候选：包括一条 multipart 或可接纳的 FIFO 前缀 `B`，以及本阶段必然出现的 source/suffix。每种内容只进入其真正所属的计量，SYSTEM 不因此重复算入 canonical。先检查：

```text
C_charge(S_B) + G_C <= 16 MiB       G_C = 4 MiB
L(S_B)        + G_L <= 64 MiB       G_L = 4 MiB
N(S_B)        + G_N <= 4096        G_N = 296
W_exact(S_B)        <= 64 MiB
D1 final-wire token quote 与原 target/output reservation 合规
```

`G` 为一个同时满足既有 item、replay 和协议条件的最大 assistant-only 内容响应保留基本字节空间；它不保证任意数量的空 block、任意 native replay 或任意工具批次都能接纳。**输入检查之后，provider 返回的内容仍须通过输出检查，才成为 accepted assistant。** 不能把成功发出请求理解成无条件接受之后所有输出。

对当前可直接追加的 `B`，等价的接纳前边界为：

```text
Delta_C(B) = C_charge(S_B) - C_charge(S)
Delta_L(B) = L(S_B) - L(S)

admission_boundary_C(B) = 16 MiB - Delta_C(B) - G_C
admission_boundary_L(B) = 64 MiB - Delta_L(B) - G_L
```

已知候选按这条边界提前腾空间。无候选时继续使用现有周期性 compaction trigger；不能等旧 soft trigger 命中后才检查待加入的大图。冷输入与 successor 必须使用自身完整候选；追加差分公式不拿来给旧/新 epoch 相减计费。

所以不增加固定的 4 MiB 或 8 MiB 图片消息上限。`M <= 16 MiB` 是原子内容的取得/验证初筛，当前输入检查留下最多 12 MiB 的完整 canonical 候选区；还需扣除必须保留的 active request、snapshot、描述及其他内容，L/W/token 也可能先到边界。最小合法 continuation/carrier 的包装也必须进入冷可行性判断，不把它假定为零。

例如两张实验图片编码总量约 6.006 MiB，需把旧 canonical 内容降到约 **5.994 MiB 减去新增文字/描述及必然增量**，才能作为一条消息接纳。两张图不拆开；若它们属于两条独立提交，可选择满足全部预算的 FIFO 前缀。

**输出检查。** 在 `runner` 收齐完整 provider 响应之后、`AssistantMessageSettlementOwner.settle()` 之前、任何本批工具的授权/调用/副作用之前，冻结实际 assistant blocks、tool-call 形状和已返回的 native replay。令 `A` 为这份完整响应，`T` 为其实际工具调用集合：

```text
C_upper_after(A, T) = 当前 exact C + actual assistant charge
                     + 本批所有 result/closure 的既有上界
                     + 到下个检查点前必然共同发生的 canonical 增量
L_upper_after(A, T) = 同一执行分支对应的 epoch logical 上界
N_upper_after(A, T) = 同一执行分支对应的 item 上界

C_upper_after(A, T) <= 16 MiB
L_upper_after(A, T) <= 64 MiB
N_upper_after(A, T) <= 4096
```

这里每个调用的结果或 closure 属于互斥结果时取最大；可能共同出现的内容相加，已在 actual assistant 中计过的参数不重复算。65,536 bytes 只用于确实走现有 canonical preview owner 的结果；Plan、特殊终端结果、closure、correlation、late correction 和 Hook/source 等按各自已有契约报价，不把每种结果都假定成一个裸 64 KiB 文本。reader 的保守上界和实际 charge 的差异必须明确。

实际 native replay 还必须通过既有 continuity 的 replay resident capacity gate，包括 `reserve_assistant_replay_fragment` 所属的 epoch/Host installed 及 installed+prepared 物理边界，并保持 canonical/effect 前失败。C/L/N、U_W 和 U_T 都不替代这个原物理 owner，也不把 replay 存储体积当成 provider context 字节。

整批可以使用当时实际剩余的硬额度，**不要求 `assistant + tool results <= 4 MiB`**。检查应位于 response 收齐后的首个准备阶段，不能先运行会产生本批效果的 Stop Hook、inferred completion 或工具路径，再补资源判断；这些后续必然上下文与结果仍按原 owner 的有限边界覆盖。若组合输出无法通过，沿既有 provider/kernel 失败结算返回明确的 output resource interruption：本批 assistant 不进入 accepted canonical，工具不获得授权且不执行；已经显示的 live draft 仍是未接纳观察，不能冒充正式结果。保留真实原因和实际/上界/可用额度。此时已越过原 semantic-output barrier，不自动重放 provider 请求、删掉部分调用或修改图片重试。

需要 provider follow-up 的分支，额外要求同一合法后继输入同时满足：

```text
U_W(已安装前缀 + actual assistant/native replay
    + bounded result/closure suffix + 必然 source) <= 64 MiB
U_T(同一后继输入，按冻结的 D1 及实际 resolved target)
    <= effective_input_budget_tokens
```

`U_W` / `U_T` 是**已知部分精确、未知结果部分保守**的上界，由当前 resolved Chat/Responses wire owner 与现有 D1 estimator 对同一真实 lowering 推导，包含 escaping、correlation、envelope、逐 item framing/取整和 native replacement；证明义务为 `actual_W <= U_W`、`actual_D1_quote <= U_T`。effective input budget 已由原 target owner 扣除输出等预留，不自行再扣一次。不能拿 replay 存储大小、canonical bytes 或 L 代替，也不能按 provider 实际 usage 学习一个补偿系数。

上界必须覆盖原 normal compiler 对所有允许结果可能实际选中的表示。可使用工具参数与已有 output owner 的更紧边界；可用较小的既有变体减少预留，前提是原选择规则和效果发生前已知的事实保证实际编译不会选择比该界更大的表示。仅知道 variants 中存在 COMPACT/REF_ONLY/OMITTED_BODY 不够：FULL 若在原语义 token 检查中可选，final-wire 超界本身并不自动触发再次降级；REF_ONLY 也不能依赖尚未成功的 artifact 发布。FULL_REQUIRED 的成功分支（如适用的 artifact_read）必须按 FULL 覆盖，不能为证明通过而另设省略模式或改变原 compiler 选择顺序。

首版证明保持已安装前缀的、在本地统一计量下可 dispatch 的直接后继，不用尚未生成的 summary 或理想 successor 假设它能放下；因此也不依赖先发送一条可能已经超界的 summary 来自救。terminal 分支无需预留未来 wire/token。完整 canonical 结算仍按完整结果收费，不能因 provider 可能使用较小变体而少计 C。

没有可证明的有限上界时，输出检查拒绝本批，不先执行工具再碰运气。下一 safe point 仍以实际结果重新 compile，并以 exact materialization 确认 W/D1 quote。资源证明绑定原 exact scope、前缀、target 与事实条件；用户在此后切换模型等独立变化继续走自己的 admission，不能把原证明沿用到新 target。D1 仍允许低估，远端也可能拒绝；这里不保证真实 provider token、网络成功或压缩模型的行为。

这是本方案的明确容量取舍：结果尚未知时，即使本次真实结果后来本可很小，完整合法上界仍可能使组合输出被提前拒绝。保留这个效果前失败，换取已开始的工具批次有本地可接纳的后继；不据此发明固定的工具调用次数上限，不降低 D1 参数，也不自动拆分并部分执行被拒绝的响应。

#### 9.3.6 固定的阶段与资源所有权

| 阶段 | 检查与余量 | 允许推进什么 |
| --- | --- | --- |
| INPUT / COLD / SUCCESSOR，provider dispatch 前 | 完整 `S_B` 通过 C/L/N/W/token；另留 `G_C = G_L = 4 MiB`、`G_N = 296` | 完整用户输入/批次沿原事务接纳；successor 另满足原 PRE_FULL 内容/目标/回收条件 |
| POST-RESPONSE，assistant settle 前 | actual A 与 native replay，加整批 result/closure/共存增量的各 owner 上界；有 follow-up 时同时检查 `U_W` / `U_T`；保留原 replay resident capacity gate | 全批合法才接纳 assistant，并进入原工具路径；超界在任何本批工具授权/效果前失败 |
| TOOL-BATCH，直到现有 safe point | 前一步已覆盖所有可能结算；局部额度随现有 runner/executor 调用生命周期持有 | 所有已发生效果按原 owner 结算，不能执行后因预算拒收；批次结束后实际重新检查，未用额度随局部对象消失 |
| 其他 canonical/source producer 的原接纳点 | 在其原事务/准备 owner 中检查实际增量及同窗口必然增量，复用既有 exclusion/fence | 不允许异步 producer 越过已报价的执行窗口偷占额度；source 改变导致 exact join 失效时在原准备路径重算 |

`G` 与执行窗口内的最大不可拒绝增量 `R` 是不同量。`R` 的公式仍是同一路径相加、互斥路径取最大；TOOL-BATCH 的 `R` 在输出检查时已由实际调用形状确定并覆盖。provider 响应在输出检查前尚不是 accepted canonical 增量，因此无需在请求发出前猜测任意工具调用组合，更不能继续用旧 4 MiB 常量证明这种组合的上界。

这些额度只是原 prepared 调用的 process-local 值，不新增 durable reservation、receipt、checkpoint、注册表或重启恢复流程；既有 writer、source exclusion、permit 与 exact-confirm 继续拥有真实性及事务边界。不可在事务外读一个剩余值后无条件提交，也不可在效果发生后补做首次容量检查。

#### 9.3.7 接纳、压缩与等待的闭合规则

1. **入口合法性。** 完整初筛、格式/帧/像素/metadata 和解码在 Hook reservation / 候选冻结前完成。资源或内容失败返回具体原因；通过验证不等于已消费队列或已安装输入。
2. **直接接纳。** metadata preflight 在展开引用前检查所选范围；精确编译、目标检查和实际 final materialization 对同一候选通过后，沿既有事务接纳完整 multipart/FIFO 前缀。不先推进有效历史，再发现 reader 或 wire 超界。
3. **可回收。** 未接纳输入保持 pending，在原允许的 compaction 边界腾空间。因某个 pending 候选而尝试压缩时，应同时检查 successor 自身，以及随后完整加入该候选的预算。采用与 queue 消费各自走原事务及只读 exact-confirm，不虚构跨事务的原子提交。
4. **暂时物理占用。** 使用原排队/调用等待与操作 deadline。只有存在不依赖消费该 FIFO 头、已经在途且会改变可用额度的阶段，才能把逻辑容量不足也解释成暂时等待；不能只因为 active 尚未结束就承诺以后会腾出空间。
5. **逻辑上不可解决。** 最小合法 successor、必须保留的 exact active 内容、FIFO 头与 G 仍不合规时，返回资源边界，不能无限等 active 自己结束。steer 复用既有 `STEER_INPUT_RESOURCE_EXHAUSTED` / turn interruption；其他入口沿各自既有资源失败结果。不改成 new turn，不越过 FIFO，不新增失败次数上限，也不丢掉 active 图片后继续。

仅命中资源 soft trigger 不等于违反硬接纳式。无合法回收但当前完整输入仍通过对应阶段检查时，复用已有 already-compact / normal prepare 行为，不采用无收益 successor，也不对同一 prepared 状态忙重试。token 硬预算、显式 model switch 和其他原有阻断条件仍生效。

successor 的 retained content、root/runtime/source reinjection 按各自实际归属进入 C/L/W；旧 epoch 的逻辑字节不与新 epoch 相加，构造中的旧/新物理重叠另计。只有原有冷 epoch 或 adopted successor 可以重建 root。PRE_FULL 的内容、目标与资源失败不推进 binding 或旧 epoch；已经合法完成的 compaction 与随后失败的独立 queue 消费不能混称一笔回滚事务。

#### 9.3.8 D3/D4 的实例化及实施验收

本节的像素/格式/依赖参数、单槽生命周期、两次接纳公式、G 与失败语义为 D2 冻结契约；D3/D4 已在第 6、8 节完成设计实例化，以下保留为实施与验收义务：

- **D3 内容表达：** 有序 body、现有 immutable blob 与可信 MIME/尺寸/长度形成唯一内容来源；metadata quote 与实际 charge 来自同一 cut、同一完整选择范围，前者可以保守但必须覆盖后者。full confirmation、GC、fork 和 carrier 使用同一引用。不能虚构固定 metadata 大小来跳过准确 DTO/schema。
- **D4 保留与 placement：** active exact 内容无损、retained unit 原子、ROOT advisory 按第 8.1 节；纯文本图片交接采用第 8.6 节 recent 规则（Tier 2 检查所选窗口，Tier 3 置空）与第 8.9 节普通 Text 替换。carrier/suffix 不制造重复 placement，最小 successor 包含真实 continuation 和必须内容，全部按实际 D2 计量。

原资源实验和 critic 讨论属于设计证据；本轮 K4 已另以生产路径、真实 PostgreSQL 与完整回归完成上述验收，见第 12 节及 K4 验收记录。D2 参数不变。

## 10. 保留的语义与 hard-cut 删除范围

权限、Hook、工具执行、MCP permit、effect settlement、provider terminal/atomic acceptance、能力表单和 plan 的独立语义继续由原 owner 负责。新增内容类型不授予读取或发送任意文件的权限，不触发额外工具执行，也不改变已接纳决定的结算。

正式实施必须一次性替换相关内部表达及全部消费者，删除以下被取代的假设：

- `submit_prompt()` / `run_turn()`、Hook reservation 与 prompt ingress candidate 只接受/比较文本的契约，以及 publication / exact-confirm 对完整 prompt 固定使用 `text/plain` / `utf-8` 的假设。
- `LLMMessage.content` 只能是字符串的内部类型契约，以及 USER 必须恰有一个字符串的校验。
- canonical-to-model lowering 中将所有 USER 内容不可逆地收缩成单个文本字段的路径。
- `SNAPSHOT_EXACT` / retained historical request carrier、其编解码与 active placement 校验只能表达文本的路径；按第 8.3 节替换为完整 typed 内容，不新增 provider-request checkpoint。
- queue redirect 只复制 body 描述而遗漏 replacement image refs、fork 新 snapshot 自行固定 inline、以及 synthetic/real reader 的字节等式只求 text 长度的路径；统一按第 6.7–6.8 节实现。
- `_activation_subject_for_anchor()` 隐式读取单个 `item.text` 的路径；改用有定义的文本 part 投影，保留 origin 和 trigger 语义。
- ROOT parent-context freeze/selection/rendering 对 `message.content` 直接 `join()` 的路径；其内容投影不能在 install 后首次失败。
- adapter 对图片内容走 `join()` 的路径，以及任何 final payload 阶段追加图片的旁路。
- successor 仅检查 target join 与数值预算、直到 provider open 才验证内容/目标兼容的路径。
- estimator 将真实 image part 的 base64 当普通文本、或完全忽略图片成本的路径。
- headroom 与 reader metadata preflight 只计文本 prompt/assistant/ToolResult、未计 multipart 及引用图片 hydration 的推导。
- 如实施过程中出现旧/新 content 双读、附件 side channel、provider 名称特例，必须在同一次变更中删除。

普通纯文本消息的 provider 可见结果保持；新 snapshot/projection 的展示统一按第 8.5 节更新，不保留旧纯文本 carrier 展示分支。字符串 wire form 仍是两套协议的合法输出形状，由统一 typed content lowering 产生。

不新增 durable approval、receipt、重放 job、feature flag、跨重启执行恢复、provider compatibility registry 或图片 fingerprint。若 canonical 内容引用需要 schema 改动，必须以第 6 节的内容真相需求单独论证，不能以“方便验收”为理由增加关系。

## 11. 后续实施顺序

按第 13 节冻结决定和下列顺序实施完整 hard cut；K1–K3 已完成，K4 已验收，U1/U2 已沿同一单一路径完成。

K2 的调用者迁移也包括既有 Web 纯文本展示、排队正文读取和草稿回填：按新 MIME 解码 `pulsara.prompt/v1`，全 Text 内容按固定 `\n` 投影，保持原话。U1/U2 已将 Web 消费者 hard cut 到同一 typed 内容：Image 不再经过旧字符串不可展示分支，descriptor、Figure 标签或图片占位文字也不会变成可编辑、可重新提交的正文。

| 阶段 | 工作 | 出口 |
| --- | --- | --- |
| K1：事实与契约（已实施） | 仅 input 模态事实、target 冻结、共享内容校验；实现已冻结的 typed ingress、identity、预算、headroom 和 canonical 引用契约 | 单一类型与 owner；不依赖 provider 名单；D1–D4 覆盖全部 kernel 边界 |
| K2：提交、来源与编译（已实施） | Host direct/queued/steer、完整命令身份与 exact-confirm、Hook/Skill 投影、canonical schema/GC、hydration、compiler/lowering、ROOT parent-context 全部消费者 | 纯图经真实提交入口进入既有 compiler；无 child ROOT 也正常；没有第二条生产输入路径 |
| K3：Wire、采用与资源（已实施） | 两套 semantic wire group、materialization、estimator、PRE_FULL/POST_FULL、multipart headroom，以及纯文本图片交接的 recent 窗口检查/P 和冷入口 | quote 与实际输入一致；原三级与原事务完成交接，无专用许可或省略状态 |
| K4：Kernel 验证（已验收） | 下节测试、真实 PostgreSQL、生产配置的真实 provider、完整回归与 isolated wheel | Kernel 验收证据独立于后续浏览器验收 |
| U1：图文编辑器（已实施） | 第 14 节的 Tiptap 接入、图片节点、顺序序列化、会话草稿和编辑行为 | 纯文本逐字一致，混合内容顺序、快捷键撤销/重做与资源释放正确 |
| U2：浏览器完整链路（已验收） | typed browser command / bridge / Protocol-v3、图片字节读取、队列恢复、历史展示和模型选择，接通 K2/K3 | 浏览器经 Host 的 Chat/Responses 正式图片请求均完成，实际请求与 frozen wire 核对一致 |

U1/U2 已沿既有控制与来源完成同次 hard cut，没有留下旧/新入口或临时降级路径；第 8.6/8.9 节仍是模型选择的正式三级交接行为，前端与 adapter 不自行替换。

## 12. Kernel 验收矩阵与证据要求

以下是完整 Kernel 验收要求。K1 已执行对应的纯契约单元；K2 已执行 Host/validator、typed source/compiler、ROOT/fork 与真实 PostgreSQL owner/GC/confirmation/hydration 测试及既有回归。K3 已执行两套图片 wire/final materialization、v2 estimator、输入 headroom、效果前输出 gate、PRE_FULL/POST_FULL、ordinary recent 与 Tier 1/2/3/P/cold 的聚焦单元和 PostgreSQL 生产入口测试。K4 于 2026-09-15 完成下面 I01–I44 的核验及完整 non-live 回归（1979 passed，含 PostgreSQL，正常退出 0）。修复后的独立 wheel 分别通过 Chat 与 DeepSeek Responses 的正式 Host 图片链路，各九次调用；逐项测试、失败与复验记录见 [K4 验收记录](PULSARA_KERNEL_IMAGE_INPUT_K4_ACCEPTANCE.zh.md)。U1/U2 后续另行完成浏览器产品验收，不能反向代替这里的 Kernel 证据。

| 编号 | 必须证明的行为 |
| --- | --- |
| I01 | 解析 input；缺失、非法成员、未知模态名称分别保留正确事实和诊断，不污染整个 catalog；不新增无消费者的 output production 字段 |
| I02 | `attachment=true` 无 image、`attachment=false` 有 image，均按 input modalities 判定 |
| I03 | 不支持图片只拒绝实际含图调用；unknown 保持未知并走普通标准请求；纯文本不受影响 |
| I04 | 任意新 route/model 使用两套已有通用 adapter；不依赖已知 provider 名单或名称片段 |
| I05 | 文本/图片交错、多图、纯图、重复图片保持顺序和次数；非法 role/part 被明确拒绝 |
| I06 | 普通纯文本 USER、assistant、thinking、tool call/result 的 exact wire golden 与现有输出一致；新 snapshot/projection 的统一展示 golden 按第 8.5 节更新，已安装前缀不重写 |
| I07 | 两套图片 wire golden 使用真实有效图片；URL 嵌套、part 类型、MIME 与 detail 正确 |
| I08 | 经真实 canonical source、hydration、compiler、wire plan 的图片内容完整；source attribution 不丢失 |
| I09 | blob 引用事务、读取失败、内容完整性、GC 可达性及必要 clean-v0 改动经真实 PostgreSQL 验证 |
| I10 | final materialization 与实际 adapter payload 完全一致；发送前无新增、重编码或替换图片 |
| I11 | 图片视觉估算非零；base64 进入字节计量；图片样例、文本中的 data URL、工具 JSON 示例不被混淆 |
| I12 | semantic/final-wire 及 replay replacement 的计量契约满足原断言；预算边界两侧结果正确 |
| I13 | 同一 epoch 多轮图片、工具调用、重连保持 SYSTEM/tools 和已安装 messages 前缀不变 |
| I14 | 活跃请求、summary source、recent window、cold epoch、compaction successor、model switch、fork、ROOT parent-context 全部满足第 8.2 节冻结的来源/保留矩阵 |
| I15 | 普通调用的取消、资源、endpoint/流失败沿原 settlement，不在 adapter 丢图重试；模型交接中 A 的原 typed 失败按第 8.6 节进入 Tier 3，不增加协议切换或额外重试 |
| I16 | 权限、Hook、MCP permit、effect settlement、能力表单与 plan 的相关回归通过 |
| I17 | 真实 provider 分别覆盖 Chat 与 Responses 的标准图片请求；记录实际 target、输入图片、请求形状、回复与失败 |
| I18 | 根目录 uv/.venv 的相关及完整回归通过；isolated wheel 中重走必要 kernel 图片链路 |
| I19 | 内容不兼容的 dry successor 在 PRE_FULL 拒绝，覆盖 active/idle 共享校验；纯文本历史图按原三级交接，不能先装再拒绝。失败时有效 binding/snapshot/context pointer 与旧 epoch 不推进；idle 选择模型本身不产生 adoption |
| I20 | 含图片、纯图和重复图片的 ROOT dispatch 即使从未 spawn child，也能完成原路径；父上下文按顺序保留来源与缺失标记。NONE/LAST_N 选择正确，投影失败发生在 install 前 |
| I21 | direct / queued NEW_TURN / steer 的纯图与混合提交合法；Hook 文本、文本为空时的 allow/block、原 Hook 次数、Skill/MCP 文本边界与 human trigger attribution 均正确 |
| I22 | 相同 command ID、相同文字但不同图片/顺序/次数，在 in-flight join 与已落库确认中均冲突；完整相同内容重试遵守各原 ingress 语义。COMMIT acknowledgement 丢失后只读 exact-confirm 正确，不重跑 Hook/writer 代替查询；悬空/错指图片不判 FULL |
| I23 | multipart 总量、重复引用、批量 queue/steer、metadata-only quote、实际 hydration、canonical/epoch/final-wire 计量一致；不能以单图合规绕过批次资源边界 |
| I24 | 在候选相关 admission boundary 及周期性 soft/hard boundary 两侧检查完整 multipart/批次与后续 reserve；接纳后 source 仍可在 hard bound 内读取并进入压缩。pending incoming、base snapshot 和 successor reinjection 均进入原 admission；无法回收的 pending 不忙重试，合法的资源 soft-trigger 状态不因无收益压缩而永久阻塞 |
| I25 | MIME/格式不一致、损坏图片、超界解码及首版不接纳的多帧内容在既定 ingress 边界拒绝；清理验证资源，不 resize/转码/占位后继续。像素内存不因 base64 文件字节合规而绕过检查 |
| I26 | exact active/historical request carrier 编解码、重复压缩及冷重建保留 typed 内容、引用和次数；active placement 恰好一处，historical 不变成 active，不新增 durable wire plan。各 quote 分项有明确单位且无 base64/视觉重复计量 |
| I27 | 输出检查在 assistant settle 及本批 Hook/tool 效果之前覆盖整批 canonical/epoch/item 结算；需要 follow-up 时证明同一 normal compiler/两套 wire 下 actual W/D1 quote 不超过 U_W/U_T。超界无 accepted assistant/工具效果，已通过的工具效果不因结算预算被丢弃；不固定整批 4 MiB，也不引入调用次数上限 |
| I28 | Text/A/Text/A 保持两个 A occurrence；移动到含其他图片的 snapshot、queue 消费或 fork 后 owner-local ordinal 可变，原 request canonical bytes/命令语义不变；反向缺/多 ref、scope 错绑、metadata 与实际 bytes 冲突均失败 |
| I29 | direct body/refs/command 同事务；小于 64 KiB 的图片也有 blob FK，只有正文可 inline；pending queue 自己可读；redirect replacement 同事务取得完整 refs且source保留；消费后 queue 与 entry 均保留独立 refs，原 FULL 确认仍成立；旧 snapshot/父会话关闭不能删除仍保留正文的图片 |
| I30 | 真实 PostgreSQL 双连接测试 GC 与 attach 两种提交顺序、旧 blob 复用、回滚/取消、owner 删除 cascade；最终不存在引用已提交而 blob 被删除的状态；body/图片 publication helper 不暗开新连接 |
| I31 | FULL 在同一个 RR connection 中核对完整正文、有序 refs 及 blob 身份/元数据，不读取图片 payload；重复 occurrence 不漏查，缺 ref/blob 或错指不判 FULL。仅图片 payload 损坏时提交仍可确认，实际 hydration 必须报完整性错误且不发送；正文 blob 的实际 bytes 仍须校验。deadline/I/O 未完成不冒充 NONE/CONFLICT，不重跑 Hook/writer；direct 仍查询原 outcome |
| I32 | active/historical 原 kind/origin 全联合保持；纯图 active、旧 carrier 多次机械恢复、CANONICAL_SUFFIX 去重、fork active→historical、较新 turn 优先；混合 carrier 覆盖 SUBAGENT_OBJECTIVE 和 USER_CONTROL_FEEDBACK 的 text-only USER，以及 PLAN/terminal 原 kind-specific lowering |
| I33 | ordinary 固定 cut/summary/tail 下 recent 依次去最老直到空，覆盖 text 64 KiB 两侧、纯图、最新超界不取更老、reclaim/soft target；本地候选不重复 summary/Hook、不能用模态/损坏失败触发 shrink |
| I34 | Tier 1/2/3 保持原候选数量；Tier 2 对原规则所选窗口检查，纯文本 B 且窗口含 Image 才整体置空，否则完整保留；不逐条滤图、补位或循环收缩。Tier 3 的纯文本图片交接仍 recent=0。Tier 2 仍有未覆盖 raw Image 时进入 Tier 3，不能靠清空 mandatory 避图；prior 未读则必须保留原或 P 后 historical |
| I35 | 纯文本、含图及经 P 省略的 snapshot/Tier 3 projection 使用同一展示结构、引用规则和位置指引；真实 Image 进入 Chat/Responses 图片 part，P 后为普通 Text。user/assistant/summary/工具证据含真实开闭标签、伪 section=active、JSON key、引号/反斜杠/换行时仍可逆表达，不能伪造分段；typed active placement、历史 role/origin、turn/entry 顺序及 C/L/W/D1 一致。不透传内部身份或保留旧 carrier 展示分支，ROOT 不把 snapshot 历史变成新 human trigger |
| I36 | summary 预算导致 safe prefix 截短、mandatory suffix 含大于 2 MiB 图片但完整 D2 可接纳时，不被文本 tail 政策误拒；同例 D2 完整图片 charge 超界则失败。保留原工具文本 tail 边界；混合 carrier 的 PLAN/terminal wrapper 与空 assistant/tool-request 文本正确 |
| I37 | 可信尺寸沿完整 frozen Image 到 LLMMessage/materialization 不丢失；generic/final/direct 正式 wire 位置逐项与真实 bytes/MIME 对应，错位或篡改失败；多个本地候选不因丢失尺寸重复 load 像素；无完整 frozen 内容的独立 wire 按 D2 验证 |
| I38 | Tier 3 按原实际 token/W 不足缩连续 suffix；纯文本候选统一先 P 后 quote，不搜索部分省略。未知模态不因错误而自动丢图；refs runtime 无 UPDATE/DELETE grant，真实 owner 删除仍 cascade |
| I39 | compaction 后 ROOT 主调用真实接收 snapshot 图片，但 nested active/recent/historical 不产生 parent-context public unit；符合原 eligibility 的 suffix HUMAN 图片产生有来源的缺失标记，原 turn-unit 分组及 LAST_N/NONE 行为保持 |
| I40 | 有效历史图片无论在 recent 窗口内外，都使纯文本 B 的 Tier 1 转入 Tier 2，即使数值预算足够；A summary 仍真实接收所选原图。覆盖旧图在窗口外而最近纯文字原样保留，所选任一位置含图则整个 recent 为空；图片因 cut/64 KiB 规则未被选入不触发置空。空窗口、不足三条、active 排除和普通省略 Text 遵守原选择与 typed 判定。已退出有效 scope 的旧图不触发额外 summary；普通/视觉切换窗口不变 |
| I41 | A 的 typed 不可用、原 repair/预算失败及无旧 epoch 的 cold/fork 入口使用同一 Tier 3；P 保留 Text、顺序、重复次数与 kind/origin，纯图历史成为普通标记文本。没有独立许可字段、第三种 part、额外 schema、永久通知状态或 UI 二次选择 |
| I42 | current active 保持 exact，recent=0 不清空未覆盖的 mandatory；Tier 3 projection 与最终 mandatory 使用同一 P，真实 B wire 不重新出现历史 Image，完整 source proof、实际文本计费与 PRE_FULL/POST_FULL 一致 |
| I43 | 已采用普通省略 Text 在重启、连续压缩、fork/再 fork 和换回视觉模型时不恢复原图；原消息、refs、FULL-confirm 仍完整。用户输入相同标记仍是普通文字，不被解析为权限或找图指令；历史投影不产生新 human/Skill trigger；本次向纯文本模型新发图片仍明确拒绝 |
| I44 | 源捕获不因 B 对原图的模态拒绝而在 P 前堵塞；真实 B candidate 仍完整校验。原 source 损坏、取消、Hook block、未完成 planning、B 失败与总开关关闭不被丢图绕过；失败保留有效上下文和原结算规则 |

真实 provider 验证通过 `LocalSettingsStore` 和 `require_pulsara_home()` 使用用户保存的生产连接，配置只读；实际 credential 值不得进入证据。不用 fixture、旧探针或以前纯文本通过的结果冒充图片实测。不具备某条 wire API 的可用实测配置时，如实记录缺口，不能据另一条的成功宣称覆盖。

Kernel dogfood 的图片应包含需要观察图像才能回答的内容，并保存实际图像和回复供核验；不能仅测试 HTTP 200，或在文字 prompt 中直接泄露期望答案。真实 provider 覆盖证明所测 target 的行为，不推导出所有 OpenAI-compatible 服务已验证。

K1 阶段只将本轮新增或更新的事实、内容、identity、计量、headroom 与 canonical 纯契约测试记为 K1 证据。K2 证据只覆盖本阶段的真实提交/source/compiler、Pillow worker 与 PostgreSQL 引用链；改动前的 catalog/target 基线及此前 PR05 的 provider/browser 证据都不是图片生产链路验收。K3 证据覆盖本阶段的 wire、资源接纳与模型交接聚焦路径；K4 已生成本轮真实 provider、并发/取消、isolated wheel 与完整链路的新证据；provider 结论仅适用于记录中的实际配置。

## 13. D1–D4 决策与实施出口

本节是 kernel 设计状态，不依赖上传按钮或历史图片 UI。D1/D2 保持原冻结算法及参数；D3/D4 的模型切换及本轮三处精简按用户于 2026-09-14 确认的规则更新于第 6、8、15、16 节。旧双模式、专用省略表示、永久通知状态、旧纯文本 carrier 展示分支和提交确认的图片 payload 重读均不再作为实施要求。

本轮 GPT-5.6-sol / max 独立只读审阅指出了分段标签与不可信正文碰撞的问题，其余精简未发现阻塞冲突。第 8.5 节已补入统一 JSON 引用与保留标签转义，并完成 11 例本地可逆性/碰撞检查；该补充由主 agent 核验，未再进行一轮独立审阅。内置子代理创建受线程数量限制，本次独立审阅通过本地 Codex CLI 的临时只读进程完成。以上都是设计证据，不是生产 renderer 或 kernel 验收。

| 决策 | 规范位置 | 闭合内容 |
| --- | --- | --- |
| D1 | 第 9 节 | 28 网格 × 7/8、最低 256；逐 occurrence、正式 wire 位置、payload 扣减、逐 item 取整；已知误差限制保留 |
| D2 | 第 9.3 节 | 原格式/像素/metadata/单验证槽；M/C/L/W、G/R、动态输入接纳、效果前整批输出检查、U_W/U_T 和原失败路径，参数不变 |
| D3 | 第 6.1、6.5–6.9 节 | 单一 typed ingress；确定性正文与内容身份；一张三类真实 owner 的 FK 引用关系；原事务发布、完整只读确认、有界 hydration、GC 与 child-local refs |
| D4 | 第 8.1–8.9 节 | exact active/typed history；ordinary recent；纯文本图片交接的 Tier 2 所选窗口检查、Tier 3 recent=0 和普通 Text 替换；实际 coverage、原子采用、fork 与 ROOT advisory |

K1–K3 已实现事实、纯契约、提交、canonical 来源、hydration、typed compiler、图片 wire、资源接纳与模型交接边界，K4 已完成完整 Kernel 验收，U1/U2 已完成浏览器产品 hard cut 和验收。被本设计明确修订的 model-switch/recent、final-wire、browser command 与内容展示规范已同步实施状态。旧规范中相应文本字段、字节含义与 carrier 形状由本文替代；未明确修改的权限、safe boundary、tier 失败集合和执行身份规则继续生效。

第 14 节为随后确认的 UI 产品契约，不构成新的 kernel 决策或另一条 provider 输入路径。实施时同步更新 [前端应用规范](PULSARA_FRONTEND_APPLICATION_SPEC.zh.md)中 composer、草稿、完整内容读取与展示的相关条款；保留原权限、command outcome 和排队动作语义。

第 12 节的 Kernel 生产路径证据已由 K4 完成，包括 D2 U_W/U_T、两套真实 adapter/HTTP payload、模型交接、isolated wheel、SQL/事务/取消组合与全量回归。第 14.8 节 U1/U2 另以真实浏览器经 bridge/Host 发起的 Chat 与 Responses 图片请求完成验收；两类证据仍各自成立，未互相替代。

## 14. 首版图文混排输入框与展示

### 14.1 产品选择与证据范围

首版使用**可在文字中插入图片的统一编辑区**。用户可以输入说明、插图、再写说明；图片不用全部归到输入框上方的独立附件栏。编辑顺序就是提交顺序，不按 provider 名称或模型识别结果自动分组。

2026-09-15 用户确认以下三处显示方式，以连续阅读文字为主。输入框保留图片节点；排队项与已发送正文在图片原位置显示完整、可点击的蓝色 `[Figure x]`，关联规则见第 14.6 节。

| 位置 | 冻结的显示方式 |
| --- | --- |
| 输入框 | 约 48 × 36 CSS px 的紧凑缩略图作为完整 inline 节点，跟随文字排列；连续图片自然并排，空间不足自动折行 |
| 排队项 | 正文仅显示原文字与原位置的 Figure 链接，不显示缩略图；默认最多两行，显示图片总数并可展开完整正文 |
| 已发送用户消息 | 缩略图带右对齐放在气泡上方；下方正文保留原文字与原位置的 Figure 链接，图片不再撑高正文行。缩略图图片区域约 72 × 72 CSS px，下方标示对应 Figure 编号；默认只占一行，放不下的图片通过 `+N 张` 打开预览 |

上述尺寸是默认视觉基准，可随主题、窄屏与触控命中区域调整，不构成图片像素、字节或总数量限制。缩略图保持完整比例、允许留白、不裁切；纯图消息也显示对应 Figure 链接。多图折叠只影响展示，完整消息与每张图仍可访问。

这里的“富文本”首先指图文混排。文本继续按现有原始文字/Markdown 源输入处理；首版不自动把 Markdown 转成加粗、列表、代码块等编辑节点，不引入字体、颜色、表格或 HTML 正文。未来增加格式化编辑时，须另定义其进入 Text 的序列化规则。

2026-09-13 的[交错实验报告](scratch/image-interleaving-probe/20260913T125159841946Z/analysis.zh.md)记录六组保存配置、84 次主矩阵与 10 次针对性补测，94 次均正常协议终止。主矩阵 48 次交错中 47 次内容答案完全正确；Qwen 的数字误读也出现在连续图片布局，不能据此判定交错协议不合法。这是独立 provider probe，支持有序 Text/Image 的可行性，不替代 kernel 或浏览器验收，也不保证模型总能准确理解图文关系。

### 14.2 编辑器依赖与职责

采用 **Tiptap 的 React 编辑器与现有 ProseMirror 机制**，复用选择、光标、图片节点、拖动与撤销/重做。首版按需要启用 Document、Paragraph、Text、HardBreak、Image 与历史管理，不直接打开整套富文本格式化规则。版本在实施时经现有前端构建验证后锁入 package-lock，不引入独立协作文档服务或云端草稿存储。

[官方 Image API](https://tiptap.dev/docs/editor/extensions/nodes/image)及[已检查的扩展源码](https://github.com/ueberdosis/tiptap/blob/main/packages/extension-image/src/image.ts)支持 inline、draggable 图片与插入命令；图片扩展只负责编辑和显示，不实现上传。其默认 HTML/Markdown 图片解析会接收 src，不能原样等同于 Pulsara 的受控图片输入。

排队项与消息气泡使用普通 React 渲染上述只读投影，不为每条历史消息创建编辑器。图片弹窗采用 [Yet Another React Lightbox](https://yet-another-react-lightbox.com/documentation) 与 [Zoom 插件](https://yet-another-react-lightbox.com/plugins/zoom)，复用显示、缩放、前后切图和关闭机制；Pulsara 提供本条内容中有权读取的图片、点击位置及加载结果。预览加载范围服从第 14.6 节，不使用远程图片抓取、上传服务或图库作为第二内容来源。依赖版本在实施时经构建验证后锁定。

| Owner | 职责 |
| --- | --- |
| Tiptap / ProseMirror | 文档编辑、选择和光标、节点移动、history、React DOM 协调；复用其机制和支持的扩展点 |
| Pulsara composer | 用户动作、当前会话草稿、图片节点到原始 File/Blob 的本地关联、只用于显示的预览 URL、发送时的单次完整内容快照 |
| 现有 Web bridge / Protocol-v3 | 当前 attachment/controller 校验、typed 命令与内容读取、实际传输边界；不直连 provider 或绕过 Host |
| 原 Host / canonical / compiler / adapter | D2 验证与接纳、D3 身份和持久化、D4 保留、唯一 frozen wire；编辑器状态没有这些权威 |

唯一有序编辑文档拥有草稿顺序；本地 File/Blob 关联只保存节点需要的不可变文件值，不能再维护一份可独立排序的 attachments 真相。节点 key、DOM、编辑器 JSON、预览 object URL 都是进程内编辑/展示数据，不成为 canonical ID、provider URL、验证 receipt 或持久化图片目录。

### 14.3 图片插入与文字编辑

- 文件拖入接受用户实际交付的图片文件；粘贴接受该次 paste 事件中的实际 image File/Blob。首版范围仍为 D2 的静态 PNG/JPEG/WebP，最终合法性由 Host 判定，不能只信扩展名、浏览器 MIME 或 naturalWidth。composer 不提供添加图片按钮或隐藏文件选择器。
- 粘贴在当前光标/选区位置插入，文件拖入使用实际落点；有选区时替换该选区。一次多图按事件提供的文件顺序形成一个编辑操作；异步读取只能填充预先保留的节点位置，不能按完成先后倒序插入，也不能在用户删除节点或切换会话后插回当前草稿。
- 图片作为可选中的完整节点；支持在其前后输入、键盘删除、拖动移动及编辑器标准撤销/重做快捷键。composer 不显示撤销、重做按钮，也不增加图片打开快捷键。移动不增加 occurrence；显式复制或再次插入同一图片可以产生新的 occurrence，不按相同 bytes 去重。
- 缩略图按第 14.1 节的紧凑尺寸与文字同排，点击可打开图片弹窗；显示尺寸、浏览器渲染和 object URL 不修改原始编码字节，不执行 resize、转码、方向修正或 OCR 后再发送。
- 外部文本粘贴优先使用 text/plain，保持文本及换行，不自动导入网页 HTML 样式或抓取其中的远程 img URL。输入 Markdown 图片语法也只是 Text；关闭编辑器默认的 URL→Image 自动转换。用户通过图片粘贴明确提供字节时才形成 Image。
- 保留中文输入法组合输入处理、普通 Enter 发送/运行中排队、Shift+Enter 换行，以及原显式引导动作。IME 的 Enter 只用于确认候选，不发送或吞掉输入；不借编辑器替换重新定义快捷键。
- 图片读取中或某个节点失败时显示该节点的状态与原因；发送必须等待完整内容可提交。用户可删除失败图片，或重新粘贴/拖入图片；不能悄悄只发其余文字和图片。纯图草稿合法，纯空白且无图不能发送。

### 14.4 从编辑文档到完整提交

首版 editor schema 使用一个 paragraph 容纳有序 Text / HardBreak / inline Image；换行是 HardBreak，外部多行文本转为相同逻辑序列。布局自动折行不产生换行。唯一 serializer 从头到尾遍历：Text 原值与 HardBreak 的 `\n` 累计成连续文本段，遇 Image 先输出非空 Text 段，再输出对应原始文件内容，最后输出剩余非空 Text。

```text
编辑内容：说明甲 → 图片 A → 换行 → 说明乙 → 图片 B
提交 parts：[Text("说明甲"), Image(A), Text("\n说明乙"), Image(B)]
```

serializer 不 trim 文本，不在图片两侧增加换行、标签、文件名或 Markdown 图片链接。纯文本产生一个 Text，值与原 textarea 的逻辑文字相同。编辑器 text node 的相邻碎片可以在**新提交冻结前**合成该连续段；D3 接收到的 Text 边界此后保持，不在重试、存储或编译时重新合并。

第 14.6 节自动生成的 Figure 链接、缩略图编号与上方图片带都只属于显示投影，不序列化为 Text，不添加 provider 标签，也不改变 Text/Image 的顺序、边界与重复次数。用户自己键入的 `[Figure 1]` 则属于原始 Text，按原值提交。

点击发送时，按当前会话、权限、模型绑定及原 delivery 语义冻结这一份完整候选。读取、转换与响应都绑定到该次候选；后续键入、图片移动、文件选择或会话切换不能修改已发命令。传给 Host 的是第 6.1 节的完整 PromptContent；不把 editor.getHTML()、editor.getText()、blob URL 或单独附件清单充当模型输入。

现有 [RuntimeConnection.submitPrompt](frontend/lib/runtime-adapter.ts)、[browser bridge](src/pulsara_agent/web_app/browser_bridge.py)、[Protocol-v3](src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto) 与 Host 的 text-only 输入必须同次 hard cut 为 typed 完整内容；不保留纯文本旧命令分支作为图片失败的 fallback。原 File/Blob 可以按既有 JSON/protobuf bytes 规则编码传输，原 gateway 在受限读取/解码后交 Host；provider data URL 仍只由第 7 节 adapter 生成。

**传输边界独立于 D2。** 当前 [HTTP server](src/pulsara_agent/web_app/http_server.py) 的 client_max_size 与 [Protocol-v3 gateway](src/pulsara_agent/terminal_protocol/v3_gateway.py) 的最大 frame 均为 8 MiB。首版保留这些既有界，完整候选必须同时通过实际 JSON 请求体、protobuf frame 与 D2 的各自预算；客户端可提前报价，服务端仍独立检查。图片在 JSON 中的 base64 膨胀、文字 escaping 和命令包装都计入实际传输长度，不能用 raw image E 直接代替；不能从 D2 的 M≤16 MiB 宣称网页可上传同样大小，也不另造固定“单图 6 MiB”额度。

超过本次传输额度时整条拒绝，保留草稿并说明图片和文字超过当前上传容量；不拆一条 multipart、不截掉末尾图片、不绕过已有协议另发附件。分帧上传、断点续传或扩大 transport bound 属于后续显式协议修订，本次不增加 durable upload、预发布 blob 或恢复 job。当前通路能传入的完整合法内容沿 D2 入队/接纳；文件读取或上传完成本身不等于 canonical 接受。

### 14.5 草稿、确认与排队编辑

当前 [workbench-view.tsx](frontend/components/workbench-view.tsx) 按 session 保存字符串草稿、等待 onSend 确认后清空，并保护队列编辑恢复不覆盖现有草稿。改为编辑文档后保持这些行为，以完整候选和草稿修订比较代替字符串相等，不添加 DTO fingerprint。

- 每个会话独立保存 process-local 文档、文件关联和编辑状态；切到 child 不带入父草稿，返回父会话仍保留。页面刷新/进程退出不承诺恢复未提交草稿，不增加 localStorage/IndexedDB 或服务端草稿持久化。
- 提交期间保留该候选及实际图片字节。收到原语义的明确接纳结果后，仅当当前草稿仍是这次提交的那一版才清空；用户后来输入的新内容不能被迟到响应清掉。明确拒绝保留可编辑草稿与具体原因。
- command outcome 不确定时沿已有 query/reconciliation 查询同一命令，不因超时新建命令重发，也不从后来改变的 editor 状态重新构造“重试”。当前草稿、发送中项、待确认项以原 UI owner 管理；原 queue 已接纳但等待执行不应被误判为提交失败。
- 排队项的 Figure 链接与“展开完整正文”只执行查看，不发送、取消或编辑队列项；展开后仍以文字和 Figure 链接呈现。发送中、待确认和被拒绝项按对应冻结候选生成同一预览，图片不能因尚未执行而从显示中消失；原状态与错误说明保留。
- 排队编辑保留既有“先确认取消原项，再恢复到空 composer”流程，不直接修改已接纳正文。取消前先完整取得该项的 Text/Image，读取失败则保留原项；取消 outcome 不明时不提前恢复。恢复目标已有草稿则保留两份内容并等待用户处理，不能覆盖。修改后作为新命令提交，原 queue refs 按 D3 保持。
- 暂存文件/预览的生命周期覆盖草稿、撤销/重做、正在提交的候选与待恢复的队列内容；仅在这些 owner 都不再需要时释放相关 File/Blob 引用、撤销预览 object URL。删除一个节点不能让另一个重复图片或可撤销操作失去原图，也不能把关闭会话前的迟到读取附到别的会话。

### 14.6 Figure 编号、历史展示与内容读取

**Figure 编号由前端按当前一条完整消息的图片出现顺序生成，不额外持久化。** 从左到右遍历 Text/Image，只有 Image 使编号递增，第一张为 1；不是所有 part 的序号，也不是 blob 的全局编号。已有 canonical 正文、refs 与 occurrence 顺序足够，不新增 Figure 字段、编号表、映射注册表或 fingerprint。

- 每条消息独立从 1 开始；同一图片出现多次，分别编号。草稿重排、增删图片后重新计算；已接纳消息顺序固定，刷新、排队转为正式消息或切换显示方式后自然得到相同编号。
- 编号依据整条消息中的 occurrence，不依据当前可见、已经加载或尚未折叠的缩略图数量。分页、懒加载及图片失败不改变编号，不因漏读前面的图片而从 1 重新计数。
- `[Figure x]` 是完整的蓝色链接式控件，整体换行、可键盘激活，点击打开本条消息对应图片的弹窗；弹窗标题与缩略图标示相同编号。链接关联既有 owner/occurrence 或本次冻结候选的实际图片引用，不能仅凭 `x` 找图。关闭或 Escape 返回原查看位置；弹窗不触发提交或队列动作。
- 只有真实 Image 产生此控件。用户手打的 `[Figure 1]` 保持普通文字，不解析为图片引用、不自动变成链接。自动 Figure 标签不进入 canonical、Hook/Skill 文本投影或 provider 输入，也不属于第 8.9 节的图片省略文字。

```text
实际内容：Image(A) → Text("这是一条测试信息") → Image(B) → Text("测试") → Image(A)
排队正文：[Figure 1]这是一条测试信息[Figure 2]测试[Figure 3]
已发送：  上方缩略图带 A / B / A，分别标示 Figure 1 / Figure 2 / Figure 3
          下方气泡正文与上述排队正文的文字和链接顺序相同
```

上述气泡布局允许把缩略图集中到上方，但正文仍在每个 Image 的原位置保留链接，文字沿现有原始文字与换行显示规则呈现。缩略图带与正文从同一份有序内容派生，不维护可独立排序的附件真相；气泡正文与实际 provider 输入也不从上方图片带反向重建。纯文字消息沿原样显示，纯图消息的正文为按序 Figure 链接。

图片读取复用现有 attachment、session 和 canonical content owner 的权限边界。entry/queue/snapshot 的正文及对应 ref 决定可读取哪个 occurrence；原只读内容查询扩展为 typed 正文及按 owner/occurrence 读取原始字节，不接受任意本地路径、外部 URL 或仅凭猜到 blob digest 的全局读取。排队项仅为生成 Figure 链接不读取图片 payload；点击预览再按对应引用读取，编辑恢复仍按第 14.5 节先取得完整图片。历史浏览使用原分页范围，图片按可见缩略图/用户打开动作有界加载；显示尺寸小不代表原图下载、解码成本小，不能全历史预加载。observer 的显示权限不授予发送权限。

缩略图或弹窗加载失败时保留对应 Figure 链接与编号，在相应位置显示失败状态并允许重读；不删除或重新编号图片，不把展示失败当作 canonical 丢图，更不修改已安装 provider 输入。刷新后根据已持久化的 D3 内容重新读取；fork 的图片使用 child 自己的 owner/ref，不依赖父页面的 object URL。UI 缓存与预览只是派生数据；不增加图片表、缓存权威或另一套 GC。

### 14.7 切换纯文本模型

只保留原模型选择操作，不增加“常规/允许省略”双模式、checkbox、逐图确认或提交许可参数。选择纯文本模型即接受第 8.6 节后果：有效历史有图时优先由原模型总结，按现有规则选中的最近最多三条原话无图则保留、有图则整体置空；无法形成合法交接时由新模型读取普通省略文字再总结，该档图片交接仍不保留 recent。界面可在模型旁说明“此模型不接收图片，历史图片将通过摘要或省略文字交接”，这是说明，不是批准步骤。

模型选择仍只影响下一 NEW_TURN；运行中的旧 turn 不热切。原消息继续显示原图，不能把有效上下文的省略文字覆盖回历史；本次新附件遇到纯文本目标仍明确拒绝并保留草稿。切回视觉模型不自动重发旧图，用户可重新粘贴/拖入。

选择偏好、命令接纳、交接成功仍使用现有各自的结果，不保存永久省略状态。交接失败显示原具体原因；关闭 compaction 总开关时按原规则说明无法完成所需交接，不偷偷绕过设置。

### 14.8 浏览器验收与后续范围

以下 U1/U2 验收已于 2026-09-15 完成。真实浏览器图片发送依赖第 12 节 Kernel 能力；本轮另从浏览器实际编辑、提交、队列/历史展示到两套正式 provider 请求核对完整链路，没有用第 12 节证据替代浏览器证据。逐项自动化、浏览器记录与明确未重跑的组合见 [U1/U2 验收记录](PULSARA_KERNEL_IMAGE_INPUT_U1_U2_ACCEPTANCE.zh.md)。

| 编号 | 必须证明的行为 |
| --- | --- |
| U01 | 纯文本、空白、换行、Markdown 源与 IME 输入保持；紧凑缩略图与文字同排，图片前后光标、键盘删除、选区替换、撤销/重做正确，Enter 不误触发送 |
| U02 | 粘贴/拖入的多图保留位置和顺序；乱序完成、读取失败、删除后完成、会话切换后完成不丢图、不插错位置；外部 HTML/Markdown URL 不隐式取图 |
| U03 | 单图、纯图、Text/Image 交错及重复图片在编辑器→serializer→Host→canonical→实际 provider wire 顺序与 bytes 一致；移动不复制，显示缩放不修改原图，自动 Figure 标签不进入提交或模型输入 |
| U04 | 发送中继续编辑、切换会话、迟到接纳、明确拒绝和 query outcome 不确定时，完整候选不变且不丢草稿、不重复提交；Plan/权限/排队和原显式引导动作保持 |
| U05 | 排队项以文字和 Figure 链接折叠/展开，显示图片总数；查看链接不发送或取消，生成链接不读取图片 payload。编辑前图片完整读取后才取消恢复，恢复实际图片节点而非标签文字；读取失败/取消未知/恢复冲突保留内容，新编辑生成新命令，旧 owner refs 不被 UI 删除 |
| U06 | 当前 HTTP/Protocol-v3 实际 8 MiB 边界两侧，覆盖 base64 与文字 escaping；超界整条拒绝且草稿可恢复；D2 验证失败不得部分提交或绕过单槽/资源边界 |
| U07 | 已发送消息为上方单行缩略图带与下方原位置 Figure 链接；窄屏及 `+N 张` 不遮盖正文或丢失可访问图片。历史分页、刷新、observer 和 fork 均通过各自合法 owner 读取原图；缺图状态可见，跨 session 错绑和仅凭 digest 读取被拒绝 |
| U08 | 预览、重复图片、undo、正在提交候选和队列恢复的 File/Blob/object URL 生命周期正确；离开/取消后无错误回填，不提前释放仍可使用的原图 |
| U09 | 真实浏览器输入至少一组必须观察图片才能回答的交错内容，并核对正式 Chat/Responses 路径的实际请求和回复；独立 probe、静态截图和仅返回 HTTP 成功均不能代替 |
| U10 | 直接使用原模型选择触发固定三级行为，无模式/许可/二次确认；纯文本模型的新附件拒绝且草稿保留。原历史图仍显示，执行失败不冒充交接完成；核对 Tier 2 所选窗口无图时保留、有图时整体置空，以及 Tier 3 recent=0 和实际 B 的标记文本 |
| U11 | Figure 按每条完整消息的图片 occurrence 从 1 编号；重复图片分别编号，草稿重排后更新，刷新/排队转正式消息/懒加载/失败时不漂移。手打相同标签仍为普通 Text；自动链接与缩略图打开同一图片，弹窗标题/前后切图编号一致，键盘打开及关闭恢复查看位置；无新增持久化编号 |

相机采集、后台屏幕读取、工具/MCP 图片输出、网页图片自动抓取、跨应用富文本 HTML 导入、图片裁剪/转码/画质选项、OCR、自动图片摘要、分享下载，以及 PDF/音频/视频仍不在首版范围。读取文件或截图的工具入口怎样获得授权与进入模型上下文，继续另行定义。

## 15. 本地 Codex 源码对照与取舍

2026-09-12 参考用户提供的 Codex 调研报告，并只读抽查 `/Users/plumliu/Desktop/python_workspace/codex` 的下列源码。这里是设计对照，不是 Pulsara 的外部 runtime 依赖，也不是两者的真实 provider 验收。未复跑报告中的所有路径；仅将当次核对的代码事实列在表中。

| 本轮抽查事实 | 对 Pulsara 的取舍 |
| --- | --- |
| [UserInput](../codex/codex-rs/protocol/src/user_input.rs) 区分 Text、Image、LocalImage 等 typed 输入 | 借鉴有序 typed ingress；本版 Pulsara 接受冻结字节，不复制 Codex 的 URL/path variant 或上传生命周期 |
| [Hook runtime](../codex/codex-rs/core/src/hook_runtime.rs) 使用 [UserMessageItem.message()](../codex/codex-rs/protocol/src/items.rs) 的文本提取；该提取把非 Text 映射为空并直接拼接 | 借鉴完整内容与 Hook 文本的分层；Pulsara 按第 6.2 节明确文本分隔和 mention 边界，不用文本投影判提交非空或命令相等 |
| [History estimator](../codex/codex-rs/core/src/context_manager/history.rs) 在 typed ResponseItem 的图片位置替换 base64 估算；非 Original 使用 7,373 等效 bytes，Original 使用 patch 估算 | 借鉴确定性的本地视觉估算和正式图片位置识别；不照搬系数、patch 数、缓存或上限，不把其结果当 provider exact token |
| [Image utility](../codex/codex-rs/utils/image/src/lib.rs) 使用维护中的 image crate，声明 2,048 尺寸值和 1 GiB 输入 sanity guard | 借鉴依赖分工；Pulsara D2 必须从自己的 canonical/epoch/hydration/headroom 推导物理边界，不能复制这些数值 |
| [Prompt request preparation](../codex/codex-rs/core/src/client_common.rs) clone input 后按 model 信息规范化 detail；[history normalize](../codex/codex-rs/core/src/context_manager/normalize.rs) 将不支持的图片替换成文本 | Pulsara 在 frozen materialization 前完成唯一编码；普通调用、adapter 与透明重试不自行省略图片或重写已装前缀。纯文本目标的模型交接按第 8.6–8.9 节总结或替换历史图片，再经原子采用生效 |
| [Local compaction](../codex/codex-rs/core/src/compact.rs) 的 compacted user message 使用 `user.message()` 文本 | 不能作为 Pulsara 图片 retention 的默认答案；原图是否被选中必须由 D4 和 canonical source owner 明确决定 |
| [Turn pre-compaction](../codex/codex-rs/core/src/session/turn.rs) 的 TODO 明确尚需预估 pending context updates / new user input | 借鉴其暴露的边界问题；Pulsara 第 9.1 节必须覆盖尚未进入历史的最大合法输入和 reinjection，不能只看已有历史 |
| [WireApi](../codex/codex-rs/model-provider-info/src/lib.rs) 当前只有 Responses，`chat` 配置显式报 removed | Codex 当前代码不能提供 Chat 图片 adapter 的实现依据；Pulsara 继续独立维护两套已确定的标准 wire 形状 |

Pulsara 的 immutable canonical 内容、只读 exact-confirm、source-bound compiler、frozen final wire、PRE_FULL admission 与同 epoch prefix continuity 继续由现有 owner 负责。不能因 Codex 使用 rollout、请求时 projection 或恢复时图片处理，就为 Pulsara 引入同类执行恢复路径；也不把上述差异写成已经观察到 Codex 每次必然改变图片字节的结论。

## 16. OpenCode 对照：可复用的边界与不采用的推论

2026-09-13 参考用户提供的 OpenCode 调研报告，并只读抽查 `/Users/plumliu/Desktop/python_workspace/opencode` 的以下源码。OpenCode 的 V2 core、legacy session 与独立 LLM package 是不同层次/路径；不能将它们拼成一条已经验收的图片链路。本轮未运行 OpenCode 测试或真实 provider，也不依据局部搜索断言整个仓库绝不存在某种机制。

| 核对的代码事实 | 对本设计的有限参考 |
| --- | --- |
| [LLM messages](../opencode/packages/llm/src/schema/messages.ts) 使用有序 content parts；[Chat](../opencode/packages/llm/src/protocols/openai-chat.ts) 与 [Responses](../opencode/packages/llm/src/protocols/openai-responses.ts) 分别把 media 降低为原生图片项 | 支持本设计“typed content → 唯一协议 lowering”的分工。其 `MediaPart` 同时接受 string/bytes，不证明 URI 已被冻结；不照搬该数据真相或其他 provider 特例 |
| V2 [input.ts](../opencode/packages/core/src/session/input.ts) 查找/接纳既有 ID，`equivalent()` 比较 session、delivery 及编码后的完整 Prompt JSON；[Session owner](../opencode/packages/core/src/session.ts) 在 `admit()` 返回后执行此检查 | 借鉴完整值比较和 admission/执行分离；不能把“按 ID 找到记录”当作 exact-confirm。Pulsara 使用第 6.1 节完整内容及不可变引用语义，不复制 `PromptAdmitted` event/replay 架构 |
| [shared.ts](../opencode/packages/llm/src/protocols/shared.ts) 检查 MIME、canonical base64 和编码前后字节上限；legacy [Image.normalize](../opencode/packages/opencode/src/image/image.ts) 另用 Photon 解码并可能 resize/转码，发生于 [chat.message Hook 之后](../opencode/packages/opencode/src/session/prompt.ts) | 借鉴分层检查，补齐第 5 节验证顺序；其 base64 decoded limit 不是像素内存上限。Pulsara 在候选/Hook 冻结前验证，不在之后改变图片 |
| [ACP content](../opencode/packages/opencode/src/acp/content.ts) 可保留 HTTP URI；V2 [to-llm-message](../opencode/packages/core/src/session/runner/to-llm-message.ts) 把 file URI 放入 media.data，tool URI materialization 仍有 TODO；[session SQL](../opencode/packages/core/src/session/sql.ts) 以 JSON 保存输入/消息内容 | 可见 typed 值并不自动保证来源 hydration 或媒体引用生命周期。Pulsara 第 6.4 节继续由 canonical/blob/reader 分工，不把 URI/JSON-only 存储当作 immutable image/GC 的实现依据 |
| V2 [Token.estimate](../opencode/packages/core/src/util/token.ts) 按字符串长度估算；[compaction](../opencode/packages/core/src/session/compaction.ts) 估算 `LLMRequest` 的 system/messages/tools，并把 summary source 中的文件序列化为 `[Attached ...]`；legacy [overflow](../opencode/packages/opencode/src/session/overflow.ts) 使用 provider usage | 这些不构成正式 final-wire 图片 quote 或视觉启发式的依据。只吸收第 9.2 节分项可解释性的要求，不复制常数、占位估算或 usage admission |

报告建议中的 exact active request、retained history 和引用生命周期，应落实到 Pulsara 已有 carrier/owner，见第 6.4、8.3 节；不能解释为新增 durable frozen-wire snapshot。报告没有替 D1 给出可直接采用的视觉算法，也没有替 D2 给出各资源 reserve 的数值。D3 最小 schema 和 D4 retention 已在第 6、8 节实例化；summary 不因外部工程只读文本就自动改为文本输入，ROOT 父上下文仍按已确定的文本 advisory 契约执行。

此前补齐的 pre-adoption、ROOT 投影、typed ingress/Hook 与 headroom 边界继续生效。普通调用、adapter 与透明重试不自行省略图片，模型交接统一按第 8.6–8.9 节执行；不增加 provider 名称分支、非法 prefix 重建或执行恢复框架。外部对照不作为本设计已实施或验收的证据。

## 17. `view_image` 本地工具扩展实施状态（2026-09-16）

独立 builtin `view_image(path)` 已按 [本地图片工具实施规格](PULSARA_LOCAL_IMAGE_TOOL_IMPLEMENTATION_SPEC.zh.md) 完成。该扩展复用本文 D1/D2/D3/D4：本地文件经共享静态图片 validator 取得不可变字节与实际 MIME，成功结果以 typed `TOOL_RESULT` 正文及原 canonical blob/ref owner 保存；Chat 与 Responses 都把工具结果降低为普通文本结果，并在同一 assistant tool-call 批次后追加一条派生 user 图片 carrier。该 carrier 不是 canonical 用户提交，不能改变 recent human、权限或消息归属。

原第 14.8 节“工具/MCP 图片输出不在首版范围”的范围说明，自本节起仅被上述独立本地 builtin 覆盖；任意 MCP/远程工具图片输出仍不在本次范围。D1–D4 的算法、资源硬界、canonical owner 和 retention 规则没有因此改写。

工具卡使用独立显示 variant：左侧卡片收起时只显示工具摘要，展开后直接显示一张按容器自适应的大图，点击沿用原 Lightbox；不显示 Figure 编号、图片链接、重复的成功文字或原始 JSON。第 14.6 节 Figure 规则继续适用于用户消息、composer 与队列，不适用于工具卡图片。

本次实际验收入口如下：

- Chat 正式链路：`output/local-image-acceptance/chat-20260916-r11/report.json`。使用保存的 Chat connection，14 次真实 HTTP 覆盖单图、同批三次调用中的成功/失败/成功、单一批次 carrier、源文件删除后的 canonical replay、cold resume、parent close 后 fork、图片可见的普通 summary、compacted successor、同 epoch prefix 连续性，以及实际 HTTP 与 frozen materialization 相等。
- Responses 正式链路：`output/local-image-acceptance/responses-deepseek-20260916/report.json`。使用保存的 DeepSeek Responses connection，运行同一套 14 次真实 HTTP 验收并通过。第三方 GPT-5.5 Responses 线路曾在功能入口前返回 `response.failed` 或长时间无进展，其失败记录保留在 `output/local-image-acceptance/responses-20260916-r2`、`responses-20260916-r3`，不据此判断 adapter 失败。
- 浏览器与 UI：`output/playwright/local-image-ui-large-20260916`。真实保存的 Chat connection 调用 `view_image` 后，收起卡片仅有摘要；展开显示 640×480 大图且无 Figure、成功文字或 JSON；Lightbox 无编号；刷新后重新展开仍从 canonical owner 完整加载。
- PostgreSQL 与相邻回归：typed tool result 的正文/blob/ref/tool row 同事务、FULL exact-confirm、失败回滚、ordinary/late hydration 与计量、组合 placement、final-wire quote、并发窗口、Hook/确认屏障、compact/Tier 3、fork 与 Protocol-v3 均由实施规格第 13 节列出的相邻测试覆盖。最终集中运行结果为 474 passed、runner/protocol/replay/hooks 202 passed、架构检查 5 passed；前端相邻测试 24 passed，并通过 ESLint 与 `build:local`。

普通 compact 中，已经被 summary 覆盖的旧工具图片可以按第 8.2 节退出 successor；这不是丢图。protected tail 中保留的完整工具组仍须连同所有图片保留。真实 provider 验收证明 summary 输入看到了工具图片，cold/fork 在 compact 前从 canonical 工具结果恢复图片；protected-tail 不可拆分与保留行为由相邻 compact 合同测试证明，不把“所有已摘要图片永久再注入 successor”作为通过条件。

后续待实施的 [已知图片引用重读规格](PULSARA_IMAGE_REFERENCE_REREAD_IMPLEMENTATION_SPEC.zh.md) 允许模型拿已知引用调用同一 `view_image` 读取 canonical 图片；不增加历史图片查询、自动发现或全量引用保留。该扩展尚未实施，不属于上述验收结论，也不改变本文 D1/D2 公式与参数。
