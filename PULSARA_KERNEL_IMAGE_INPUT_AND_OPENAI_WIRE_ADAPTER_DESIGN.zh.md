# Pulsara Kernel 图片输入与 OpenAI 通用 Wire Adapter 修订设计

> 状态：**DRAFT — 2026-09-13**
>
> 修订：2026-09-12，补齐 pre-adoption 内容校验、ROOT 父上下文、typed ingress / Hook 与 multipart headroom 边界；纳入本地 Codex 源码对照。
>
> 补充：2026-09-13，吸收 OpenCode 调研中可用的输入比较、协议 lowering 与验证分层；明确计量单位和 exact request carrier 边界。状态仍为 DRAFT。
>
> D1 冻结：2026-09-13，经用户确认，将第 9 节的 28 网格 × 7/8、最低 256 算法作为正式设计契约，替代此前候选。参数依据为现有 368 次请求、开源尺寸函数对照及允许适当低估的取舍。D2–D4 仍待闭合，未激活生产图片输入。
>
> D2 最终冻结候选：2026-09-13，依据原 666 次资源实验、5 次 PNG 单位补测，与用户指定的 gpt-5.6-sol / max critic 讨论收敛第 9.3 节。采用动态输入接纳、效果发生前的输出/工具批次检查、明确的 G/R 分工和原资源失败路径。尚未标记用户已冻结；D1 不变，图片 kernel 尚未实施验收。
>
> 本轮交付：设计文档；尚未修改生产代码，尚未实施或验收图片输入。
>
> 范围：先修订 kernel 的模型输入、模型事实、wire lowering、预算与连续性契约。上传、展示等产品行为另议。
>
> 本文记录已确定方向和实施前必须闭合的问题。第 13 节闭合前，不视为可直接激活的实施规范，不标记 `ACTIVATED`。

## 1. 目标与权威关系

Pulsara 继续只实现 OpenAI-compatible 的两套通用 adapter：**Chat Completions** 与 **Responses**。图片是这两套协议中的输入内容，不构成第三套 adapter，也不按 provider 名称分叉。

本轮先明确 kernel 如何接纳、确认、表达、验证、冻结、计量并发送图片。**typed kernel 提交接口及其 Hook/Skill 投影属于本设计范围**；如何选择图片、上传及显示历史，后续单独决定。不能从本设计推导出自动读取截图、自动上传文件或新增浏览器附件流程的授权。

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
| [model_catalog.py](src/pulsara_agent/llm/model_catalog.py) 的 `ModelCatalogEntry` / `_parse_model_entry()` | 读取 limits、reasoning、tool_call、shape hint；丢弃 modalities / attachment | 保留有实际消费者的 input modalities；暂不加入 output modalities |
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
6. `output_modalities` 当前没有独立消费者，本轮不加入 catalog DTO、production target facts 或 Web 投影，也不为该字段新增缓存或持久化。原始外部响应中的 output 仅是调查事实；未来图片输出设计再引入实际所需字段。外部 output 声明不授权扩展当前 response parser。

不改变现有 catalog 产品过滤规则。既有 `user_declared` target 没有模态声明时，投影为 unknown；本轮不为此新增设置页选项或独立覆盖表。若后续需要用户显式声明，应修订既有 closed `UserDeclaredModelTarget`，不能建立另一份配置 authority。

仍由现有 process-local catalog owner 在原生命周期内刷新。请求期间不联网刷新 catalog，不用新快照改写已经冻结的 target、预算绑定或已安装前缀。

### 4.1 Successor 必须先证明内容可执行，再提交采用

复用 [validation.py](src/pulsara_agent/llm/validation.py) 的 `validate_model_context_shape_for_call()` 所拥有的内容、role、target 和 transport binding 校验，或从该函数收敛出唯一共享纯校验。它**不带 semantic token gate**；不能直接调用带该 gate 的 `validate_model_context_for_call()`，把 semantic estimate 重新提升为 final-wire admission authority。

`validate_compaction_wire_transition()` 与 `validate_model_switch_wire_transition()` 的 `PRE_FULL` 路径，均须对 dry successor 的实际内容和冻结目标调用这一校验，再联合既有 exact join、final-wire token、wire byte、reclaim 规则作出决定。它必须覆盖 active / idle、同目标压缩及 A→B 模型切换，不能以“idle 现在不打开 provider”为由省略。

该过程不安装 dry successor，不打开 provider，不触发额外 Hook 或工具。A 支持图片、B 明确不支持且 successor 保留图片时，失败必须发生在 `_settle_compaction_adoption()` 之前：canonical context binding、采用的 snapshot 指针、turn 的 context pointer 和旧 continuity epoch nonce/revision/prefix 均不推进。临时规划资源按原 owner 释放。

模态不兼容是内容/目标拒绝，不能包装成预算不足或可 reclaim 失败来触发删图、降级、tail-shrink 或切换协议。只允许按第 8 节明确的 source/retention 规则构造另一个合法候选，不能为让校验变绿而隐式改变保留规则。

`POST_FULL` 对真实重建结果继续复验相同内容契约和 final-wire admission；它不能替代 `PRE_FULL`。采用后发生其他失效时沿现有 post-adoption failure 结算，不新增回滚或恢复框架。

## 5. Kernel 的内容模型

在现有 `LLMMessage.content` 上形成唯一的有序 part 序列，最小 vocabulary 为：

```text
LLMTextPart(text)
LLMImagePart(media_type, immutable_bytes)
LLMMessage.content = tuple[LLMTextPart | LLMImagePart, ...]
```

这是 provider-neutral 输入值，不拥有执行生命周期、上传状态或持久化 authority。图片字节必须在进入 frozen input 前完成来源解析和必要验证；adapter 不读取本地路径、不下载 URL、不查询数据库，也不根据模型名称转换图片。

本版以**已冻结字节的 inline 图片输入**作为 wire 基线。外部 URL、provider file ID、Files API 上传以及 provider 管理的媒体生命周期，不纳入本版。`data:` URL 是 adapter 的确定性编码结果，不作为 kernel 的第二份内容真相。

第一版 role 范围限定为 USER 中的图片输入；SYSTEM、assistant thinking、tool call arguments 和现有文本 ToolResult 保持原角色及内容语义。工具产生的图片如何进入模型上下文，需要后续明确工具输出与来源契约；不能为适配方便把 tool result 伪造成用户发言。

具体约束：

- 支持 USER 中有序的文本与图片 part；图片可以单独出现，不要求伪造占位文本。保留原 part 顺序和图片次数，不排序、不去重。
- `thinking` 与 tool-only 字段继续使用各自现有 closed shape；新增图片不放松 role validation。
- 文本构造器如 `LLMMessage.user(text)` 可保留为创建 text part 的便利函数；`content` 本身不同时接受旧字符串和新 part 两种内部表达。
- 纯文本消息的最终 wire 字符串、换行、空内容及 omission 行为保持当前结果。含图片的消息按 part 顺序投影，不额外插入说明文字。
- 不同时维护 `content`、`attachments`、`image_urls` 三份可独立变更的内容。
- 本版不提供跨服务“画质档位”产品参数。基线请求使用标准 `auto` detail；这只确定发送字段，不声称各服务的自动处理结果或成本相同。
- MIME、解码有效性和每次操作的物理边界必须在实施前闭合，见第 13 节。不根据文件后缀或客户端自报尺寸直接判定有效。

D2 的验证顺序应是：在已有单次字节边界内取得输入，先做实际字节的格式识别及 MIME 一致性检查，再通过维护中的库有界解码，取得尺寸、帧及内存信息；完成完整 multipart 的资源检查后，才冻结提交候选并进入原 Hook / canonical admission。文件读取入口若后续采用 sample sniff，sample 只能提前拒绝或选择验证器，不能代替完整读取上限和完整内容验证。首版不接纳的动画、多帧或格式应明确拒绝，不自动 resize、转码或取第一帧。

这里的“解码”必须区分 **base64 解码后的压缩图片文件字节**与**图像解码后的像素/帧内存**。前者的字节上限不能证明后者有界；D2 要说明所选库在大尺寸、损坏数据、多帧和取消时的资源边界及清理行为。MIME 来自验证后的实际格式，不在 Hook 或命令冻结后再次修改内容。

## 6. Canonical source 与 compiler 接入

### 6.1 最小 typed kernel 提交与完整命令身份

在现有 Host owner 上把 `submit_prompt(..., text: str, ...)` 收敛为接收一个不可变、有序的 `PromptContent`。它使用第 5 节相同的 Text / Image 语义；不是第二套通用多模态 IR。公开调用名和准确 DTO 定义随 D3 收敛，但以下行为已确定：

- 提交者提供文本及已取得的图片字节；kernel 在 Hook reservation / 命令候选冻结前完成 D2 要求的内容验证与冻结。Host 不接收待发送时再解析的可变本地路径或远程 URL。
- 空 part 序列、仅含空文本且无图片的内容非法；有合法图片的纯图内容合法。文本 part 继续执行既有控制字符与文本资源校验，不能因 Hook 文本为空而拒绝纯图。
- `run_turn()`、queued `NEW_TURN`、`STEER_ACTIVE_TURN` 及队列重定向/消费走相同内容契约，保留各自原有 command、target、permission、Hook 时机与 settlement 语义。不新建一条图片专用 runner，也不让 steer 额外跑一次 NEW_TURN Hook。
- 现有文本调用者在边界构造一个 Text part；kernel 内不同时保留 `text` 与 `content` 双入口。UI 仍可以只发文本，这只是调用适配，不要求本轮提供上传产品。

`command_id`、session/turn/queue 身份与内容分离，但同一 command 的兼容性必须比较**完整提交语义**：有序 part 类型、每个文本原值、图片不可变内容及其验证后的 MIME、所有影响输入的既定参数，以及既有 delivery / target / permission / model binding 规则。本版 detail 固定为 `auto`，不能在重试时另选 detail。相同文字但图片字节、图片顺序或出现次数不同，都不是兼容重试。

`_IngressHookReservationKey` 必须携带完整冻结值并按 exact equality 判定；不能继续用文本投影作为在途 join 身份。原 `PreparedPromptIngressCommand`、`build_prompt_ingress_command()`、`prompt_ingress_semantic_digest()` 及 repository confirmation 同步 hard cut。已有跨重启命令语义 digest 和不可变 blob digest 可以服务原边界；不另加 image/DTO fingerprint、receipt 或提交注册表。

Canonical publication 与 command/queue 的 full confirmation 必须覆盖完整 ordered body 及其不可变引用和真实 media type / codec。只读 `confirm_prompt_ingress()` 不能只比较文本、metadata 总大小，或继续硬编码整个内容为 `text/plain` / `utf-8`。D3 必须明确完整比较使用的 canonical 编码及引用完整性检查，防止悬空或错指图片被判为 FULL。

原提交 owner 在写入结果不确定后，通过既有只读 exact-confirm 判断 FULL / NONE / CONFLICT；不能重调 writer 代替查询。已 FULL 的 queued 相同命令沿原语义返回原结果、重发 wake hint，不重复 Hook、入队或执行；direct 调用保留其“已接纳后查询原 outcome”的区别，不扩展成执行重放。Hook 拒绝、资源失败和取消不得把半份内容当成已接纳 prompt，孤立 blob 按既有 GC 规则处理。

### 6.2 Hook 与 Skill/MCP 使用明确的文本投影

定义唯一纯文本投影：按原 part 顺序取 Text part，以一个 `\n` 分隔，保留每个 part 的原始文本，不 trim、不 OCR、不读取 image bytes。只有一个 Text part 时结果与当前 `prompt` 完全相同；纯图投影为 `""`。图片的文件名、data URL、MIME 和缺失标记都不进入这一文本投影。

`UserPromptSubmitInput.prompt: str` 本版沿用该投影，Hook 的 session/turn/permission/cwd/model 身份及 allow/block/effect 规则保持原样。Hook 仍会按原 ingress 时机观察纯图提交，只是 `prompt` 为空；kernel 的内容非空判定不能复用 Hook prompt。Hook schema 本版不获得图片检查能力，也不声称已检查图片内容。

`_activation_subject_for_anchor()` 及 Skill/MCP 的文本匹配只从相同 canonical input 的 Text parts 取得文本；mention 不跨 part 边界拼接识别。纯图仍保留 HUMAN_MESSAGE / HUMAN_STEER 的 origin 和 trigger anchor，只是文本匹配输入为空；不能改成 NON_HUMAN，也不因此清空 configured/default active skills 或上一次无新 trigger 的 activation 语义。图片本身不激活新的 Skill/MCP。若现有 matcher 需要 part 边界信息，由原 matcher 接受有序文本输入，不另建识别框架。

Hook/Skill 的派生文本不参与完整命令身份，不替代 provider 输入或 canonical body。父上下文的缺失标记使用第 8.1 节独立的 advisory projection，不能回流进 Hook/Skill 匹配。

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

现有 [PostgresCanonicalBlobStore](src/pulsara_agent/conversation_kernel/blob.py) 已提供不可变字节、真实内容完整性验证和 exact read。若 canonical 图片需要落库，应复用此 owner；现有内容 digest 跨越真实不可变内容边界，可以保留，不新增 image DTO fingerprint 或图片注册表。

**已有 blob store 不等于已有图片消息存储契约。** 多 part 的 canonical body、引用归属、事务边界、hydration 与 GC 可达性仍需明确。不能把 blob ID 塞进任意 JSON 后假定当前 GC 已认识这些引用，也不能用 process-local map 弥补应有的 canonical 内容。

这项存储设计属于 kernel，可先于上传或历史 UI 定义。D3 必须同时给出 typed ingress、完整命令比较、最小关系形状、owner、事务与失败路径；若关系确需增加，应在本规范写明独立产品必要性并更新 clean-v0。不得先旁路接入图片，再以产品后议为由跳过入口、来源与引用完整性。

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

通用 base64 编码使用标准库；图片格式检查若需要解码，先检查现有依赖与维护中的库的适用 API，再选择最小调用边界。不要自行编写图片解码器或复制 SDK 协议实现。

## 8. 前缀连续性、重试与上下文生命周期

同一 epoch 内，历史图片的字节、media type、detail、part 顺序及编码结果一经安装，必须保持不变。添加后续消息只追加后缀；不得重新压缩旧图、更换 URL、改 detail，或因为 catalog 刷新而重新生成旧输入。

SYSTEM 与 provider tools 仍须 byte-identical。图片支持不是重建 root、重新发现工具或重放 Hook 的新边界。

透明传输重试继续沿用既有 semantic-output barrier，并消费同一个 frozen plan。provider 返回不支持图片或字段非法时，不能自动改成纯文本、自动去掉 detail、切到另一套 wire API 或重试另一种图片格式。拒绝必须保留真实 endpoint 错误及既有 terminal 语义。

冷 epoch 与明确采用的 compaction successor 仍是仅有的 root rebuild 边界。对被选入有效上下文的图片，应保留其 canonical 来源和内容；不能因只支持文本的旧投影而无声丢失。图片如何参加 summary source、recent window、fork 和 model switch 的合法上下文重建，应在第 13 节闭合，不能在 adapter 中临时选择“全部保留”或“全部删除”。

不向 memory 或其他 auxiliary call 自动附带全历史图片。每类调用仍由原 purpose 与 source selection 决定其输入；不能把 OCR 或模型生成的图片描述当作原始图片内容的无损替代。

**普通同模型压缩的 summary source 不是新的产品选择。** 当前 `direct_model.py::resolve_compaction_summary_call()` 复用 origin call 的 target；`compaction/model_call.py::prepare_compaction_summary_semantic()` 在合法 source prefix 后追加 `LLMMessage.user(summary_request)`，`_require_summary_wire_prefix()` 要求实际 wire 保留完整已安装前缀及相同 SYSTEM/tools。因此图片链路接通后，已安装前缀中的图片必须原样进入这次 summary 请求，不能先移除或 placeholder 化；也不因此补回此前已退出有效上下文的全历史图片。合法 safe cut 与 retained tail 继续由原 planner 决定。D4 需要实例化的是 successor 采用后的图片保留，以及已有模型切换 Tier 3 destination projection 的 typed 内容选择；不能借“摘要模型是否看图”重新开放普通压缩的前缀规则。

### 8.1 ROOT 父上下文：本版保留文本 advisory，明确视觉信息缺失

`_freeze_subagent_parent_context_call_subject()` 是每次 ROOT dispatch 的必经投影，当前位于 continuity install 之后，不以实际 spawn child 为条件。它及 `FrozenRootConversationContextUnitFact`、parent-context selection/rendering 必须在同一次 content hard cut 中修订；不能等待子代理图片工具功能。

本版继续使用现有 `NONE` / `LAST_N` 和 public ROOT conversation unit 的文本 advisory 契约，**父上下文不携带图片字节**，也不赋予 child 新的 blob 读取能力。投影必须：

1. 沿原 compiled message placement 关联 canonical entry / turn / origin，再沿 part 原顺序呈现公开文本；保留原 turn-unit 分组、选择范围、ordered entry IDs 和 source identity。
2. 每个 image part 在原位置产生稳定标记，明确表达 `image part <ordinal> omitted from parent context; visual content unavailable`。不把图片跳过成空字符串，不伪造视觉描述。part ordinal 对应原内容位置；来源关联由已有 parent-context fact 保留，不能只剩一段无法追溯的拼接文本。
3. 纯图 USER 也保留相应 entry 和非空缺失标记，不能因为没有 text 而整条丢失；重复图片分别保留标记，不去重。
4. assistant 只贡献原有 public text；thinking、tool group、无 placement 的 runtime source 继续按现有规则排除。`NONE` 仍不提供父上下文；`LAST_N` 的子集选择不改成全历史。

该标记说明已有文本 advisory 的信息范围，**不是目标不支持图片时的 provider 降级方案**。ROOT 的实际图片请求仍发送原始图片；B 不兼容仍按第 4.1 节拒绝。未来若决定给 child 传图，需要显式修订 parent-context typed source 和其预算/归属契约，不能悄悄把此文本投影改为附件转发。

可能失败的 part validation、文本投影和资源检查在 continuity install **前**完成，绑定到同一 prepared dispatch 的内容和 placements；install 后只用原 permit 完成 subject 的 exact 绑定，不再执行图片读取、解码或新的内容遍历。保留原 owner 和 permit 校验，不增加 authority、receipt 或恢复层。

### 8.2 D4 必须闭合的保留矩阵

| 场景 | 已确定约束 / 仍需闭合的规则 |
| --- | --- |
| 活跃请求、同 epoch suffix、透明重试 | 已安装图像内容和 wire shape 不变；不重新读图或处理 detail |
| ROOT 父上下文；有 child / 无 child | 按第 8.1 节执行明确的文本 advisory；两者都走相同 ROOT 投影 |
| 普通压缩 / Tier 2 A summary source | 复用当前 source target，在合法 source prefix 后追加压缩 USER 请求；已安装前缀中的图片必须保留，不能作为 D4 可选的文本化策略 |
| Tier 3 B destination projection summary source | 沿既有模型切换专用分支实例化完整 typed 单位的选择、来源与预算；不冒充 A installed prefix，不自动有损替换选中的图片 |
| recent window / retained tail | D4 明确保留单位；图片、关联文本和来源必须按定义的原子内容单位处理，不留下脱离来源的 part |
| cold epoch / compaction successor | 重建选中 canonical 内容；PRE_FULL 校验内容与目标；retention 变化只能发生在批准的重建边界 |
| A→B model switch | 校验 B 的实际 successor，保留图片且 B 明确不支持时不采用；不能自动 placeholder 化 |
| fork | 对有效 canonical 上下文按既有 fork owner 复制；D4 明确图片引用、GC 与来源的复制规则，不使用 rollout replay |

所有行都属于 kernel 覆盖面；原本未决的保留策略仍需 D4 确定，不能把每次 ROOT 都会执行的父上下文投影遗漏在外。

### 8.3 Exact request carrier 的图片范围

当前 [compaction/contracts.py](src/pulsara_agent/conversation_kernel/compaction/contracts.py) 的 `FrozenCompactionActiveRequest` 在 `SNAPSHOT_EXACT` 中携带 `text`，`FrozenRetainedHistoricalRequest` 也只携带文本；[prompt.py](src/pulsara_agent/conversation_kernel/compaction/prompt.py) 的 carrier 编解码与 [model_call.py](src/pulsara_agent/conversation_kernel/compaction/model_call.py) 的 active placement 校验依赖该文本形状。这些都是 D3/D4 的实际 hard-cut 消费者，不能把图片改成 `[Attached ...]` 后仍称为 exact。

对于被选择为 exact 的 active/historical request，carrier 必须能无损恢复同一份已接纳 typed 内容及不可变引用、MIME、顺序和重复次数。`SNAPSHOT_EXACT` / `CANONICAL_SUFFIX` 的原 location 语义不变：active request 仍只有一个 Runtime authoritative placement，historical request 不获得执行身份；summary prose 不是恢复来源。选取和移除按 D4 定义的完整 request/turn 单位进行，不能为满足 tail 预算剪掉其中图片后保留“完整请求”的声明。

**`SNAPSHOT_EXACT` 不是新增的 durable provider-request checkpoint。** 它扩展的是已有 canonical continuation carrier。`FrozenProviderWireMaterialization` 继续由当前调用的 process-local owner 持有，用于 exact admission、install 和透明 retry；不把整份 wire plan、provider 控制字段或执行恢复状态持久化到 snapshot。冷 epoch 可从 canonical typed 内容按合法边界构建新 plan，不重新读取原文件或依赖 provider URI 恢复图片。

本节只确定 exact 内容不能被有损投影替代；普通 summary 必须保留已安装图片的规则见第 8 节，不再作为自由选项。D4 仍须实例化 successor 的 recent window / retained tail、既有模型切换 projection 与引用保留规则；第 8.1 节 ROOT 文本 advisory 和第 6.2 节 Hook/Skill 文本投影继续独立生效。

## 9. 预算：保持统一本地估算，分清 token 与字节

现有 `PulsaraHeuristicTokenEstimatorV1` 对 final wire JSON 按字符数估算。如果原样加入 data URL，base64 文本长度就会被当成模型输入 token；简单跳过该字符串又会把图片成本算为零。两者都不能作为图片支持的完成状态。

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

**D1 已经用户确认并冻结：采用允许适度低估的统一本地默认值。** 本节的公式、参数、取整顺序、逐 occurrence 计量及 final-wire payload 替换规则共同构成 D1 正式契约。此前候选仅作为实验对照，不作为实施选项或运行时 fallback。选择同时考虑容量损失、低估幅度和已读开源处理机制，不要求所有样本零低估。D2–D4 及实施验收闭合前仍不激活生产图片输入。

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

**本版按上述明确局限冻结 D1 默认值。** 当前实测全为 Chat Completions；Responses 只有离线 wire 控制验证，不宣称已完成真实图片链路。D2–D4 与实施验收仍开放：需验证合法边界、最大 multipart/reinjection/hydration 增量、混合内容、重复引用、generic/native/direct quote 等式以及 normal/compaction/pre-adoption/post-adoption 的实际 kernel 路径。独立 probe 不代替这些验收。

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

第 9.3 节最终候选明确区分 `G`（dispatch 前最低服务余量）和 `R`（已允许执行窗口的结算上界）：图片经过完整输入检查；provider 响应也须经过 canonical/effect 之前的输出检查。4 MiB 作为 `G_C/G_L` 保留，不再用 `max(assistant, 单个 ToolResult)` 宣称它能覆盖整轮工具结果。周期性 soft boundary 可继续按 `hard - G` 提前触发，但候选接纳要计完整 `S_B + G`，输出检查则按 actual assistant 和实际整批 result/closure 上界证明 `R`。不新增 durable quote 或通用阶段框架。

保留原 item headroom，并验证 multipart/batched admission 对 item 与 part working set 的影响。若合法路径使剩余额度无法容纳候选及必要输出，D2 必须调整本次接纳或有依据的物理预算，不能接受一个永远无法编译的输入，也不能用任意总图片数上限掩盖矛盾。

`reader` 的 metadata-only preflight、compaction planner 的 soft trigger、queue/steer 实际接纳点、continuity 的 logical byte 检查、cold successor/reinjection 与 final-wire admission 必须共用一致的边界定义。需要在原调度点预留或先进入 compaction；不能等图片进入有效历史、reader 已越界后才补做 preflight。具体推荐公式和数值见第 9.3 节，尚未冻结。

### 9.2 Quote 分项必须保留单位与计量对象

OpenCode 调研提出的“分项可解释”值得采用，但不能把 `encoded_image_cost`、`visual_image_cost`、decoded memory、canonical、epoch 和 final-wire 拼成一个没有单位的 total。D1/D2 应给出现有 quote 的最小必要分项，说明每项的单位、输入对象、归属及超界原因；不用新建一份 durable quote 或独立证明记录。

- **Token 维度**：文本与 framing 的本地估算，加每个实际图片 occurrence 的非零视觉估算。真实 base64 仍存在于被遍历的 final wire，token 计数按第 9 节保留的计量契约只扣除正式图片 payload 文本，保留 wrapper 并逐 item 取整；不将完整 base64 文本 token 与视觉 token 重复相加。D1 的最终数值参数按第 9 节执行。
- **物理维度**：分别测量压缩图片字节、像素解码峰值、canonical hydration、epoch logical bytes 和 wire bytes。即使都用 bytes，它们也不是同一个工作集，不能相加后对任意一个 hard bound 比较；同一图片的存储去重与逻辑重复出现分别计量。
- **Headroom 维度**：第 9.1 节的 `R >= max Δadmitted_path` 对各资源 owner 分别成立；`G` 不是这项结算上界。并发峰值、串行复用、批次和互斥路径按真实生命周期推导。不能把 pending、successor、assistant、ToolResult 的上界无条件叠加，也不能漏掉同窗口会共存的输入。

统一算法要求 semantic/final-wire/replay/compaction 遵循同一 token 契约；不要求 semantic token 数与 canonical 字节数相等，也不让 token estimator 接管全部物理计数。不得从 OpenCode 的 `round(string.length / 4)`、provider usage 或 URI/占位文本估算反推 Pulsara 图片参数。

### 9.3 D2 最终冻结候选：动态输入接纳与效果前输出检查

**状态：与 gpt-5.6-sol / max critic 收敛的最终冻结候选；尚未标记用户已冻结，尚未实施。** 本节将第 9.1 节具体化：保留原硬边界，图片单独验证，完整 multipart 动态接纳；完整模型输出在进入 canonical 和任何工具效果前再检查，G 与执行窗口的 R 分别计量。以下常数、两次接纳公式、明确失败语义及 D3/D4 必要实例化组成同一方案。

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

`M` 的 16 MiB 初筛由 `C_charge` 的 16 MiB reader 硬边界推导：单条不可拆提交自身展开后已经超过该接纳上限，就不存在合规的完整读取路径。D3 精确 body 编码决定实际包装字节，不能用假定的固定 metadata 额度代替。纯文本 wire golden 和原 prompt 文本上限保持；D3 存储形状及二进制扩展涉及的 `*_utf8_bytes` 命名/消费者一次 hard cut。

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

本节的像素/格式/依赖参数、单槽生命周期、两次接纳公式、G 与失败语义可以作为 D2 冻结契约；D3/D4 不再另选这些规则，但仍须完成其消费者的准确实例化：

- **D3 内容表达：** 有序 body、现有 immutable blob 与可信 MIME/尺寸/长度形成唯一内容来源；metadata quote 与实际 charge 来自同一 cut、同一完整选择范围，前者可以保守但必须覆盖后者。full confirmation、GC、fork 和 carrier 使用同一引用。不能虚构固定 metadata 大小来跳过准确 DTO/schema。
- **D4 保留与 placement：** active exact 内容无损、retained unit 原子、ROOT advisory 按第 8.1 节；carrier 和 suffix 不制造重复 authoritative placement。最小合法 successor 必须包含真实 continuation 描述及必须保留内容，不把这些成本留到采用后。
- **输入边界：** 两张约 3 MiB 图片的同条原子提交、独立消息 FIFO 前缀、base snapshot 内多图及重复引用、C/L/W 各边界上下 1 byte、逻辑不可解 steer 的现有中断结果。
- **输出边界：** 覆盖 12 MiB−1 base + 8 KiB assistant/参数 + 64 个 preview 上界的反例；证明超界时无 accepted assistant、无本批工具授权/效果。再覆盖实际余量允许组合输出大于 4 MiB 的成功分支，不能把测试改成固定 4 MiB 整批上限。
- **工具结算和 wire/token：** 每条合法工具成功/失败/closure 分支均被预报价覆盖；已有副作用不因后续预算被丢弃。两套 adapter 对实际 native replacement、control characters、escaping、重复引用与最大结果证明 `actual_W <= U_W` 和 `actual_D1_quote <= U_T`。覆盖 bytes 通过但 FULL_REQUIRED token 不通过、存在 OMITTED_BODY 但实际 compiler 仍选 FULL 的反例；terminal 不无故预留下一请求，follow-up 不依赖理想 summary。
- **物理生命周期：** 跨 session 单 validator、逐图释放、冷 hydration 不重复 load、取消/deadline/close 后无存活 worker；实际 ingress 的峰值和延迟，以及 ICC/EXIF/XMP、像素数据后的 metadata、Unicode iTXt、单帧/多帧容器。不能把已有 PNG ASCII 样例或纯解码耗时冒充这些覆盖。

当前实验和 critic 讨论支撑的是设计选择，不是上述 kernel 验收已经通过。下一步证据来自正式引用/保留实例化和生产路径测试；不需要为 D2 再调用视觉 token provider。

## 10. 保留的语义与 hard-cut 删除范围

权限、Hook、工具执行、MCP permit、effect settlement、provider terminal/atomic acceptance、能力表单和 plan 的独立语义继续由原 owner 负责。新增内容类型不授予读取或发送任意文件的权限，不触发额外工具执行，也不改变已接纳决定的结算。

正式实施必须一次性替换相关内部表达及全部消费者，删除以下被取代的假设：

- `submit_prompt()` / `run_turn()`、Hook reservation 与 prompt ingress candidate 只接受/比较文本的契约，以及 publication / exact-confirm 对完整 prompt 固定使用 `text/plain` / `utf-8` 的假设。
- `LLMMessage.content` 只能是字符串的内部类型契约，以及 USER 必须恰有一个字符串的校验。
- canonical-to-model lowering 中将所有 USER 内容不可逆地收缩成单个文本字段的路径。
- `SNAPSHOT_EXACT` / retained historical request carrier、其编解码与 active placement 校验只能表达文本的路径；按第 8.3 节替换为完整 typed 内容，不新增 provider-request checkpoint。
- `_activation_subject_for_anchor()` 隐式读取单个 `item.text` 的路径；改用有定义的文本 part 投影，保留 origin 和 trigger 语义。
- ROOT parent-context freeze/selection/rendering 对 `message.content` 直接 `join()` 的路径；其内容投影不能在 install 后首次失败。
- adapter 对图片内容走 `join()` 的路径，以及任何 final payload 阶段追加图片的旁路。
- successor 仅检查 target join 与数值预算、直到 provider open 才验证内容/目标兼容的路径。
- estimator 将真实 image part 的 base64 当普通文本、或完全忽略图片成本的路径。
- headroom 与 reader metadata preflight 只计文本 prompt/assistant/ToolResult、未计 multipart 及引用图片 hydration 的推导。
- 如实施过程中出现旧/新 content 双读、附件 side channel、provider 名称特例，必须在同一次变更中删除。

这里的 hard cut 不要求改变纯文本的 provider 可见结果。字符串 wire form 是两套协议的合法输出形状，应由统一 typed content lowering 继续产生。

不新增 durable approval、receipt、重放 job、feature flag、跨重启执行恢复、provider compatibility registry 或图片 fingerprint。若 canonical 内容引用需要 schema 改动，必须以第 6 节的内容真相需求单独论证，不能以“方便验收”为理由增加关系。

## 11. 后续 kernel 实施顺序

本轮只写本文。正式修改生产行为前，先闭合第 13 节，再把本文收敛为可执行的 hard-cut 规范。

| 阶段 | 工作 | 出口 |
| --- | --- | --- |
| K1：事实与契约 | 仅 input 模态事实、target 冻结、共享内容校验；闭合 typed ingress、identity、预算、headroom 和 canonical 引用设计 | 单一类型与 owner；不依赖 provider 名单；D1–D4 覆盖全部 kernel 边界 |
| K2：提交、来源与编译 | Host direct/queued/steer、完整命令身份与 exact-confirm、Hook/Skill 投影、canonical schema/GC、hydration、compiler/lowering、ROOT parent-context 全部消费者 | 纯图经真实提交入口进入既有 compiler；无 child ROOT 也正常；没有第二条生产输入路径 |
| K3：Wire、采用与资源 | 两套 semantic wire group、frozen materialization、统一 estimator、PRE_FULL/POST_FULL 内容与数值校验、multipart headroom、连续性与生命周期矩阵 | adapter 消费的内容与 quote 完全一致；不先采用不可执行 successor；资源临界处仍可压缩 |
| K4：Kernel 验证 | 下节测试、真实 PostgreSQL、生产配置的真实 provider、完整回归与 isolated wheel | 有本轮 kernel 验收证据；不声称浏览器图片产品已完成 |

上传与展示可以后续再做，但 K2/K3 不能以此为由绕开 typed kernel ingress、完整命令确认、Hook/Skill/parent-context 投影、canonical source、source selection、预算或前缀规则。

## 12. Kernel 验收矩阵与证据要求

以下均为未来实施要求，**当前未执行、未通过**：

| 编号 | 必须证明的行为 |
| --- | --- |
| I01 | 解析 input；缺失、非法成员、未知模态名称分别保留正确事实和诊断，不污染整个 catalog；不新增无消费者的 output production 字段 |
| I02 | `attachment=true` 无 image、`attachment=false` 有 image，均按 input modalities 判定 |
| I03 | 不支持图片只拒绝实际含图调用；unknown 保持未知并走普通标准请求；纯文本不受影响 |
| I04 | 任意新 route/model 使用两套已有通用 adapter；不依赖已知 provider 名单或名称片段 |
| I05 | 文本/图片交错、多图、纯图、重复图片保持顺序和次数；非法 role/part 被明确拒绝 |
| I06 | 纯文本 USER、assistant、thinking、tool call/result 的 exact wire golden 与现有输出一致 |
| I07 | 两套图片 wire golden 使用真实有效图片；URL 嵌套、part 类型、MIME 与 detail 正确 |
| I08 | 经真实 canonical source、hydration、compiler、wire plan 的图片内容完整；source attribution 不丢失 |
| I09 | blob 引用事务、读取失败、内容完整性、GC 可达性及必要 clean-v0 改动经真实 PostgreSQL 验证 |
| I10 | final materialization 与实际 adapter payload 完全一致；发送前无新增、重编码或替换图片 |
| I11 | 图片视觉估算非零；base64 进入字节计量；图片样例、文本中的 data URL、工具 JSON 示例不被混淆 |
| I12 | semantic/final-wire 及 replay replacement 的计量契约满足原断言；预算边界两侧结果正确 |
| I13 | 同一 epoch 多轮图片、工具调用、重连保持 SYSTEM/tools 和已安装 messages 前缀不变 |
| I14 | 活跃请求、summary source、recent window、cold epoch、compaction successor、model switch、fork、ROOT parent-context 全部满足第 8.2 节闭合后的来源/保留矩阵 |
| I15 | 取消、局部资源失败、endpoint 拒绝和流式失败沿原 terminal/settlement 路径退出；不降级丢图或换协议重试 |
| I16 | 权限、Hook、MCP permit、effect settlement、能力表单与 plan 的相关回归通过 |
| I17 | 真实 provider 分别覆盖 Chat 与 Responses 的标准图片请求；记录实际 target、输入图片、请求形状、回复与失败 |
| I18 | 根目录 uv/.venv 的相关及完整回归通过；isolated wheel 中重走必要 kernel 图片链路 |
| I19 | token / bytes 均可通过但内容不兼容的 successor 在 PRE_FULL 被拒绝；包括 A 支持图片、B 明确不支持的 active/idle 切换。失败时 canonical binding/snapshot/context pointer 及旧 epoch nonce/revision/prefix 不推进；不删图重试 |
| I20 | 含图片、纯图和重复图片的 ROOT dispatch 即使从未 spawn child，也能完成原路径；父上下文按顺序保留来源与缺失标记。NONE/LAST_N 选择正确，投影失败发生在 install 前 |
| I21 | direct / queued NEW_TURN / steer 的纯图与混合提交合法；Hook 文本、文本为空时的 allow/block、原 Hook 次数、Skill/MCP 文本边界与 human trigger attribution 均正确 |
| I22 | 相同 command ID、相同文字但不同图片/顺序/次数，在 in-flight join 与已落库确认中均冲突；完整相同内容重试遵守各原 ingress 语义。COMMIT acknowledgement 丢失后只读 exact-confirm 正确，不重跑 Hook/writer 代替查询；悬空/错指图片不判 FULL |
| I23 | multipart 总量、重复引用、批量 queue/steer、metadata-only quote、实际 hydration、canonical/epoch/final-wire 计量一致；不能以单图合规绕过批次资源边界 |
| I24 | 在候选相关 admission boundary 及周期性 soft/hard boundary 两侧检查完整 multipart/批次与后续 reserve；接纳后 source 仍可在 hard bound 内读取并进入压缩。pending incoming、base snapshot 和 successor reinjection 均进入原 admission；无法回收的 pending 不忙重试，合法的资源 soft-trigger 状态不因无收益压缩而永久阻塞 |
| I25 | MIME/格式不一致、损坏图片、超界解码及首版不接纳的多帧内容在既定 ingress 边界拒绝；清理验证资源，不 resize/转码/占位后继续。像素内存不因 base64 文件字节合规而绕过检查 |
| I26 | exact active/historical request carrier 编解码、重复压缩及冷重建保留 typed 内容、引用和次数；active placement 恰好一处，historical 不变成 active，不新增 durable wire plan。各 quote 分项有明确单位且无 base64/视觉重复计量 |
| I27 | 输出检查在 assistant settle 及本批 Hook/tool 效果之前覆盖整批 canonical/epoch/item 结算；需要 follow-up 时证明同一 normal compiler/两套 wire 下 actual W/D1 quote 不超过 U_W/U_T。超界无 accepted assistant/工具效果，已通过的工具效果不因结算预算被丢弃；不固定整批 4 MiB，也不引入调用次数上限 |

真实 provider 验证通过 `LocalSettingsStore` 和 `require_pulsara_home()` 使用用户保存的生产连接，配置只读；实际 credential 值不得进入证据。不用 fixture、旧探针或以前纯文本通过的结果冒充图片实测。不具备某条 wire API 的可用实测配置时，如实记录缺口，不能据另一条的成功宣称覆盖。

Kernel dogfood 的图片应包含需要观察图像才能回答的内容，并保存实际图像和回复供核验；不能仅测试 HTTP 200，或在文字 prompt 中直接泄露期望答案。真实 provider 覆盖证明所测 target 的行为，不推导出所有 OpenAI-compatible 服务已验证。

文档阶段只检查文档本身。现有 catalog/target 测试基线与此前 PR05 的 PostgreSQL、provider/browser 证据，都不是本设计的实施验收证据。

## 13. 实施前需闭合的 kernel 决策

以下属于 kernel 设计工作，无需先决定上传按钮、缩略图或浏览器交互，但不能留给代码中的隐式默认值：

| 编号 | 待确定内容 | 必须交付的结果 |
| --- | --- | --- |
| D2 | 第一版图片、multipart/batched admission 与 headroom；第 9.3 节为最终冻结候选 | 16,777,216 像素、Pillow 的准确参数单位、Host 单验证槽、16/64/64 MiB 硬边界；动态输入检查与效果前输出检查、G/R 分工及 U_W/U_T 均已给出。D3/D4 实例化准确表示与保留，不另选一套算法；生产激活仍须证明 quote/charge 和所有结算分支 |
| D3 | typed kernel ingress 到 canonical 的完整内容 owner | 第 6.1 节接口 DTO、direct/queued/steer 内容流、Hook reservation exact equality、完整命令的 canonical 编码/confirmation、Hook/Skill 文本投影、body/ref 及 exact request carrier、事务/hydration/GC/fork 引用；关系增减及 clean-v0 改动 |
| D4 | 第 8.2 节完整 source/retention 矩阵 | 活跃请求、summary、recent window、cold epoch、successor、model switch、fork 及无 child / 有 child 的 ROOT parent-context；落实第 8.1 节文本 advisory 与第 4.1 节 pre-adoption 内容校验，不增加重建边界 |

本文已经确定两套通用 wire 形状、input 模态事实来源、typed ingress/content、完整命令比较、Hook/Skill 文本投影、父上下文文本 advisory、pre-adoption 内容校验及时序，以及资源预留必须机械推导的原则。D1 已冻结；D2 与指定 critic 收敛的最终冻结候选见第 9.3 节，尚未标记用户已冻结。D3 准确 DTO/schema、D4 其余保留策略仍需实例化相关契约；外部工程实现、本地资源实验和 critic 意见均不替代正式 kernel 验收。

上述剩余三项闭合后，应直接修订本文相应条款并移除已解决的开放项；不另建恢复框架、兼容分支或一套旁路规格来规避它们。

## 14. 留待后续讨论的产品行为

上传、拖放、粘贴、相机、图片选择器、预览/缩略图、进度与错误文案、排队消息编辑、历史图片展示、分享下载，以及是否提供画质/压缩选项，均不在本轮确定。

读取文件、浏览器截图、MCP 图片结果如何成为已授权的模型输入，另行定义具体入口和工具结果语义。也不在本轮引入 OCR、自动裁剪、自动图片摘要、图片输出、PDF、音频、视频或其他 provider-native adapter。

这些产品决策将消费已经闭合的 kernel 契约。Kernel 验收完成与浏览器图片产品可用，是分别需要证据的两个完成条件。

## 15. 本地 Codex 源码对照与取舍

2026-09-12 参考用户提供的 Codex 调研报告，并只读抽查 `/Users/plumliu/Desktop/python_workspace/codex` 的下列源码。这里是设计对照，不是 Pulsara 的外部 runtime 依赖，也不是两者的真实 provider 验收。未复跑报告中的所有路径；仅将当次核对的代码事实列在表中。

| 本轮抽查事实 | 对 Pulsara 的取舍 |
| --- | --- |
| [UserInput](../codex/codex-rs/protocol/src/user_input.rs) 区分 Text、Image、LocalImage 等 typed 输入 | 借鉴有序 typed ingress；本版 Pulsara 接受冻结字节，不复制 Codex 的 URL/path variant 或上传生命周期 |
| [Hook runtime](../codex/codex-rs/core/src/hook_runtime.rs) 使用 [UserMessageItem.message()](../codex/codex-rs/protocol/src/items.rs) 的文本提取；该提取把非 Text 映射为空并直接拼接 | 借鉴完整内容与 Hook 文本的分层；Pulsara 按第 6.2 节明确文本分隔和 mention 边界，不用文本投影判提交非空或命令相等 |
| [History estimator](../codex/codex-rs/core/src/context_manager/history.rs) 在 typed ResponseItem 的图片位置替换 base64 估算；非 Original 使用 7,373 等效 bytes，Original 使用 patch 估算 | 借鉴确定性的本地视觉估算和正式图片位置识别；不照搬系数、patch 数、缓存或上限，不把其结果当 provider exact token |
| [Image utility](../codex/codex-rs/utils/image/src/lib.rs) 使用维护中的 image crate，声明 2,048 尺寸值和 1 GiB 输入 sanity guard | 借鉴依赖分工；Pulsara D2 必须从自己的 canonical/epoch/hydration/headroom 推导物理边界，不能复制这些数值 |
| [Prompt request preparation](../codex/codex-rs/core/src/client_common.rs) clone input 后按 model 信息规范化 detail；[history normalize](../codex/codex-rs/core/src/context_manager/normalize.rs) 将不支持的图片替换成文本 | Pulsara 在 frozen materialization 前完成唯一编码；不在请求/重试时按目标重写已装前缀，不对不兼容目标自动丢图或 placeholder 化 |
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

报告建议中的 exact active request、retained history 和引用生命周期，应落实到 Pulsara 已有 carrier/owner，见第 6.4、8.3 节；不能解释为新增 durable frozen-wire snapshot。报告没有替 D1 给出可直接采用的视觉算法，也没有替 D2 给出各资源 reserve 的数值。D3 最小 schema 和 D4 其余 retention 仍需本地设计闭合；summary 不因外部工程只读文本就自动改为文本输入，ROOT 父上下文仍按已确定的文本 advisory 契约执行。

此前补齐的 pre-adoption、ROOT 投影、typed ingress/Hook 与 headroom 边界继续生效。这次增加外部对照和具体消费者，不将它们标为已经实现或验收，也不重新引入 provider 名称分支、占位降级、prefix 重建或执行恢复框架。
