# Pulsara 图片上下文卸载与历史引用恢复实施规格

日期：2026-10-14。状态：**已按冻结规格实施，两条恢复路径的真实视觉 provider 验收通过**。规格冻结前已完成主代理与 GPT-6 Astra（high）的代码对照审阅、修订讨论及最终复审。2026-10-14 完成实现、相关测试及真实 provider dogfood；具体验证范围和限制见第11节。

目标：ROOT 对话中的图片退出视觉上下文后，中立地保留可用的恢复信息；引用被后续摘要遗忘时，ROOT 模型仍有能力通过现有历史工具定位原消息、取得引用，并按需重新看图。占位和历史工具提供能力，不要求模型为了继续任务而先搜索、重读或验证旧图片。复用已有图片存储、压缩后继、历史查询和 `view_image`，不建立新的附件系统。

## 1. 产品范围与权威

用户确认的目标由两条互补路径完成：

1. **上下文内恢复**：压缩后仍保留的历史内容中，图片本体可在明确的压缩后继边界替换为程序生成的可重读占位。
2. **历史恢复**：整条消息被摘要掉、引用未被摘要保留且当前任务确实需要原图时，可以用 `read_session_content` 取得原消息中的图片引用，再按需调用 `view_image`。已知消息位置可直接读取；需要定位时才选择 `search_session_content` 或已有分页浏览，不把搜索设为读取前置条件。

第二条路径是长期可发现性的保障，不要求摘要永久携带所有图片。这里的保障是工具能够按已保存记录返回引用，不保证模型每次都会选对搜索词、主动调用工具，或仅凭模糊指代自动选中正确图片。

**按需原则**：占位是历史事实，不是任务、建议待办或必须执行的恢复步骤。当前图片、历史文字、摘要结论或用户提供的信息足够时，可以直接继续工作或回答，不必查询历史、再次看图或证明图片仍可读取。是否读取由当前用户问题及确实缺少的信息决定；系统不因发生压缩或存在占位自动发起任何查询/读图调用，也不预取旧图片。

本规格是本次扩展的实施依据；用户指令及 [AGENTS.md](../AGENTS.md) 优先。归档文档用于解释已有契约，不将其历史实现细节当作新增机制的理由。明确扩展的旧范围是：

- [已知图片引用重读规格](PULSARA_IMAGE_REFERENCE_REREAD_IMPLEMENTATION_SPEC.zh.md)原本只保证“已知引用 → 重读”，不提供历史发现。本次允许从既有 ROOT 历史查询中取得图片引用。
- [统一会话内容查询设计](PULSARA_UNIFIED_SESSION_CONTENT_QUERY_DESIGN.zh.md)及[工具语义补充](PULSARA_SESSION_CONTENT_TOOL_SEMANTICS_ADDENDUM.zh.md)原本主要返回保存文本。本次增加图片的文字占位与引用，不自动返回视觉载荷。
- 保留 [Fingerprint 减法契约](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)、provider 前缀连续性、原图存储与读取授权、当前活动请求的精确保留、已有资源接纳和长任务可用性。

### 1.1 三个已确认边界

**跨会话**：有权读取另一个会话的历史文字，不等于当前会话有权通过 `view_image` 读取其图片。本次不扩大图片工具的 session/workspace 范围，不增加跨会话图片导入、复制或父会话回退。跨会话历史结果可以说明原消息含图片，但不提供可在当前会话直接执行的读图指引。

**子代理**：新增自动卸载候选与两条恢复路径的完成承诺仅面向 ROOT，不开放子代理内部历史查询，也不为 worker 新增自动卸载候选。子代理已通过原有上下文传递获得的引用，仍按已有 `view_image` 调用权限、session/workspace 归属和资源规则处理；已有压缩行为不退化。共享 formatter 对既有纯文本历史省略位置的替换可沿原适用 scope 使用，但不因此授予 worker 历史发现能力。不得自动注入 ROOT 全部图片或扩大 worker history 范围。

**纯文本模型**：可以读取图片引用、元数据和历史分析文字，但这些不构成视觉理解。已知纯文本模型调用 `view_image` 继续返回 `MODEL_IMAGE_INPUT_UNSUPPORTED`，不自动 OCR、不调用替代视觉模型、不自动切换模型。能力未知时保留现有三态处理，不将未知推断为支持或不支持。

### 1.2 本次不做

不新增图片搜索工具、附件列表工具、OCR/图像语义索引、图片描述模型、永久本地副本、远程 URL 下载、图片编辑/缩放接口、跨会话图片授权、全历史引用清单或图片总量上限。重读仍取得已有 canonical owner 所持有的原始编码字节，不借鉴其他项目的规范化副本存储。

## 2. 实施前事实与问题

| 现有行为 | 当前代码入口 | 问题或保留点 |
| --- | --- | --- |
| 实际图片前附 `pulsara_image.image_ref` 标签 | [model_input/lowering.py](../src/pulsara_agent/model_input/lowering.py)、[compiler.py](../src/pulsara_agent/model_input/compiler.py) | 实际图像与标签都进入请求；不是将图片当普通文字 |
| snapshot 保留 active/recent/historical 内容时可携带完整图片 | [compaction/prompt.py](../src/pulsara_agent/conversation_kernel/compaction/prompt.py)、[planner.py](../src/pulsara_agent/conversation_kernel/compaction/planner.py) | 当前活动请求保持精确保留；近期内容还受后继接纳影响 |
| 摘要提示词要求按需要复制引用 | [compaction/prompt.py](../src/pulsara_agent/conversation_kernel/compaction/prompt.py) | 无程序级完整保留保证；重复压缩可能遗忘 |
| 文本目标的历史投影使用 `[Image omitted]` | [compaction/planner.py](../src/pulsara_agent/conversation_kernel/compaction/planner.py) | 图片身份随占位丢失；部分 recent 内容直接退出后继 |
| 历史文本解码及用户历史投影忽略图片 descriptor | [session_text.py](../src/pulsara_agent/conversation_kernel/session_text.py)、[history_projection.py](../src/pulsara_agent/conversation_kernel/history_projection.py) | 找到原消息仍不能取得程序生成的图片引用；纯图片消息没有有用的文字投影 |
| 图片引用按当前 session/workspace 查找已提交 owner 并 exact read | [prompt_storage.py](../src/pulsara_agent/conversation_kernel/prompt_storage.py) | 已能读取退出有效上下文的图片；不需要新的字节存储或读取工具 |

压缩不等于删除历史图片；但保存字节、当前模型看到图片、模型知道引用、模型有权读取，是四个不同事实。任何一步都不能由其他一步推断。

## 3. 对 DeepSeek Harness 的取舍

参考本地 `deepseek-harness` 的 `packages/compaction/compaction-image-offload/src/index.ts`、`image-offload.ts`、`projection.ts` 及 `packages/llm/llm/src/content.ts`：其图片请求超过预算时，记录 `image/offload`，选择旧图片 occurrence，后续请求改为带身份及可用文件路径的文字。该机制与普通全文摘要不同；其普通摘要也不保证保留每张图的路径。

Pulsara 采用“图片本体退出请求，恢复入口仍可用”的产品语义。恢复入口使用现有 `image_ref`，不用本地路径。不移植其 durable offload event、历史消息投影修改与文件对象存储，也不把占位变成模型必须执行的恢复指令。

Pulsara 的图像卸载只发生在**明确采用的压缩后继**中。同一 epoch 内的 SYSTEM、provider tools 及已安装 messages 前缀保持不变；工具发现图片、检索返回引用、预算压力或模型切换意图，都不能单独授权重写前缀。冷启动重建已采用的结果，不重新根据当时模型或可用空间选择是否恢复原图。

本次不承诺恢复“摘要调用自身已超过图片/provider 输入边界”的情况。现有流程先准备并执行 summary source，再构造后继；本次新增卸载不能倒过来改变已安装的 source。source 不可执行时沿现有失败/交接路径处理，不能临时删图、修改 source 或新增 provider 错误重试机制；第5.3节既有获准交接投影保持原边界。

## 4. 图片占位的表示与归属

### 4.1 复用身份与原始来源

`image_ref` 继续等于原图片 descriptor 的完整 `sha256:` 内容摘要。尺寸和 MIME 来自同一已验证 descriptor，不由模型补全，不从自由文本反解析。不增加图片短 ID、引用注册表、DTO fingerprint 或 `fingerprint -> object` 映射。

每个历史 occurrence 保留各自的图文位置。同图多次出现可以共享同一引用，但不能按 digest 合并用户提问、图片位置或工具结果。同一个引用只标识字节内容，不标识这次用户想问什么。snapshot 的 recent/historical 槽位顺序不被解释成全局时间顺序，不为实现卸载新增跨槽位排序或身份。

历史工具已有 `session_id + entry_id` 作为消息定位；图片位置沿用 owner 内的顺序。不为 snapshot 中原本没有 entry ID 的保留摘录增加虚构消息身份，也不把 UI 的消息内 Figure 编号当全局图片 ID。

### 4.2 占位内容

从真实图片值或经校验的 descriptor 生成普通文字占位，语义示例如下；尖括号仅用于规格示意，不是实际引用：

```text
[历史图片引用；此处未包含图像内容]
image_ref: sha256:<完整的64位小写十六进制内容摘要>
media_type: image/png; width: 1600; height: 900
```

上面的三行是完整恢复占位的固定文案；实现只替换 digest、MIME、十进制 width/height，并通过输出断言固定。占位不包含“请读取”“展开”“必须核对”或工具调用模板，不声称已读取像素、不承诺图片此刻可读，也不声称图片已被删除。`view_image` 对 `image_ref` 的支持由既有工具说明统一解释，不在每张图片后重复动作指引。完整占位用于压缩后继与当前会话的精确历史读取；搜索/自动召回使用第6节规定的简短存在标记。

使用普通 `LLMTextPart` 表达，不引入新的 prompt part kind。完整图片继续使用原 `LLMImagePart` 与既有旁置引用标签。两者是不同的模型输入内容；不可保留完整图片的同时宣称该位置已卸载。

持久占位不写“当前模型是纯文本”这种可能过时的运行状态。纯文本能力限制由实际目标校验和工具结果解释；切回视觉模型时，占位仍然成立，需要时显式重读，不能自动将旧图片重新装入原位置。

普通用户也可能输入长得相似的文字。系统不得扫描这种文字来创建图片 owner、执行工具、建立授权、恢复占位对象或改变存储可达性。formatter 的输入只能来自 typed canonical 图片事实；模型提交的引用最终仍由现有读取 owner 校验。

### 4.3 保存位置

压缩产生的占位作为后继 snapshot 中的历史文本保存，并与 snapshot/后继 binding 在原压缩采用事务中一起提交。原始 USER_MESSAGE、USER_STEER、TOOL_RESULT 正文和图片 refs 不被改写。

历史读取产生的占位属于普通查询工具输出，按现有工具结果生命周期追加；不写回原消息，不为图片建立新 GC 根。之后的重读产生新的图片工具结果，其发布、资源计量与 blob 复用保持原流程。

## 5. 压缩后继的选择规则

### 5.1 先保护已有精确内容

以下内容不属于任意卸载候选：当前活动请求、尚未正式接纳的新输入、受现有闭合规则保护的工具调用/结果，以及被选择为精确保留的原始 suffix。不能为了塞入预算而改写它们。若保持这些内容仍无法通过现有接纳，沿已有失败或压缩路径处理。

新发图片遇到已知纯文本模型仍按现有规则拒绝；不能将它当历史图片占位后宣称发送成功。

### 5.2 普通视觉目标

只在 ROOT 后继候选中扩展选择；保留已有安全切点、summary prefix、受保护 tail 和活动请求定位。复用原 recent suffix 枚举，对于同一个安全切点与同一个 recent 窗口，顺序固定为：

1. 尝试原带图候选。
2. 只有原候选以现有资源/回收不足类别失败，且 snapshot 的 `recent_human_requests` 或 `retained_historical_requests` 含真实图片时，再尝试一个占位候选：将这两个可选历史槽位中的全部图片换为完整占位，其他内容保持原位。
3. 若仍以可重试的资源/回收类别失败，进入原有更小 recent 窗口，再重复带图/占位两种表示；只有内容确实不同才尝试第二种表示。窗口耗尽后回到原有安全切点/保留组选择，不重写受保护 tail。

这是一项明确的产品取舍：优先保留整段历史文字和重读入口，不求在每个候选中保留最多图片。不做逐张卸载、图片子集组合搜索或按推测相关性排序。每窗口的两种表示是本产品的闭合集合，不是额外的图片数或重试上限；原窗口和切点仍按已有规则枚举。

所有候选复用同一份已生成摘要、冻结的 source 和原候选 family；改变图片表示不再调用一次摘要模型、不重新获取工具表，也不重新发布 Hook 效果。候选按需构造，不保存所有图片副本。普通视觉目标中的自动卸载不扩展到 SUBAGENT_TASK。

继续候选的条件复用 `_can_retry_compaction_with_smaller_recent` 对资源/回收失败的分类。授权、owner 完整性、目标/切点 drift、取消和未知异常不触发“删图再试”。占位触发已有 `FrozenPromptContent` 文本界限等资源界限时，由已知资源检查明确分类；禁止泛捕获 `ValueError` 来掩盖 descriptor 或正文损坏。

每次候选使用完整的 canonical/logical/final-wire/token 计量，包括占位、边界标签和包装；同时满足既有最小 token 回收、final-wire 缩小、soft target、`force` 例外和 PRE_FULL/POST_FULL 检查。不能只因少了图片就跳过原回收判定，也不假定小图片换成长占位必然节省资源。不增加普通调用中的实时图片淘汰器或新重试循环。

工具图片若保留在精确 tail 中，继续原样保留；若随整个旧工具组进入摘要，需要恢复时可按第6节处理。不为本次功能把所有工具图片额外复制到 snapshot。

### 5.3 纯文本目标交接

在已有获准的模型切换/压缩交接内，用可恢复占位替换历史图像省略位置，替代裸 `[Image omitted]`。原有投影的 part 次序、幂等性、活动请求排除、source/target 归属和失败结算均须保留。

ROOT 对原本仅因 recent 内容含图而整体放弃的路径，先尝试其文字与图片占位候选，再按实际预算决定是否舍弃；同一窗口无需先尝试注定不满足模态的带图版本。若缩小 recent 仍不足，沿既有 Tier 2/Tier 3 交接选择和失败结算，不绕过或改变 summary source 的 tier 语义。若完整消息仍进入摘要，则不保证原位保留，但在第6节合法 ROOT 历史范围内可找回。worker 不新增这组候选选择。

### 5.4 摘要与重复压缩

摘要模型继续保留任务语义、已得出的结论、必要的引用及其用途。不能让模型生成图片引用或补全缺失摘要，也不在摘要之后用正则收集所有 `sha256:` 字符串建立目录。

对仍保留的 typed 历史内容，卸载占位由程序生成；对整条被摘要掉的内容，不追加全量图片引用清单。保持原有后继 continuation 对任务恢复/等待的判断，不另加逐次压缩的图片恢复指令。历史读取与图片引用能力在现有工具使用说明中统一解释，并保持“已有材料足够时无需再查”的原则；不对没有历史工具的 scope 宣传不存在的能力。

旧占位再次进入摘要时，允许摘要保留或省略其文字；长期恢复依靠原消息查询。不能把摘要中出现的引用提升为新的存储 owner，也不能要求历史图片引用随压缩次数累计增长。

### 5.5 采用与失败

只有后继正式采用后，卸载结果才生效。候选报价、摘要调用或采用失败、取消、Host 关闭不得改变已安装前缀或留下单独的 offload 状态。沿用原事务、确认和取消结算，不增加事件、收据、恢复任务或并行提交路径。

## 6. 历史发现与读取

### 6.1 工具分工

不增加工具参数或第四个查询工具。继续使用：

- `search_sessions`：定位会话，不承担附件枚举。
- `search_session_content`：需要定位相关消息时，可用原有真实文字、技术词或既有工具内容搜索。程序生成的搜索片段只展示短存在标记，不附图片引用；它不是 `read_session_content` 的必经前置调用。
- `read_session_content`：在消息原图文顺序中返回程序生成的完整图片占位，是获取引用的确定入口。
- `view_image(image_ref=...)`：唯一的已知引用视觉读取入口，保持现有参数形状。

已有文字、当前图片或历史摘录足够完成任务时，无需查询或重新看图。确实缺少回答所需的像素、颜色、位置等视觉信息时，模型可以选择重读；只是看到占位或知道发生过压缩，不构成读取理由。已知完整引用可直接 `view_image`，已知消息定位可直接 `read_session_content`，只有缺少定位时才考虑搜索或分页浏览。历史助手分析不是图像本体，也不因被保存而保证正确，但这不要求例行重新验证所有历史结论。

### 6.2 可查询范围

当前会话仍只查询实际安装基底所界定的较早 ROOT 原始记录，排除现有活动请求；显式传自身 session_id 不得扩大范围。FULL_HISTORY 没有更早区间时继续返回无更早内容。压缩摘要提到某条旧消息，不妨碍原规则允许的精确重读。

其他会话保留原授权、cut 与冷读范围，但图片投影使用非操作性说明，例如“该历史消息含 PNG 图片；图片属于另一个会话，本查询未授予当前会话读图权限”。不生成面向当前 `view_image` 的可执行恢复占位，不隐式导入图片。

这不要求删除历史原文中本来出现的引用字符串。此类字符串仍是历史文字，不构成访问授权；若相同图片另有当前会话的合法 owner，是否能读由现有读取校验决定，不能因它也出现在别的会话而额外拒绝。

Fork 只能依靠 child 真正拥有的消息、snapshot 和图片 refs；父会话已压缩掉、未复制到 child 的历史不属于 child 的恢复承诺。引用字符串即使复制到 child 摘要，也不证明 child 持有合法图片 owner。不得查询父会话或裸 blob 作为自动 fallback。

### 6.3 来源与读取成本

复用 canonical prompt codec，从已有消息正文取得有序图片 descriptor，并按现有 owner/ref 规则核对归属。描述符读取不 hydrate 图片 payload，不调用 Pillow，不重跑原工具，不打开 UI HTTP 入口。

返回的尺寸/MIME/引用说明历史保存内容，不表示本次已验证图像 payload 仍可读。真正执行 `view_image` 时再次按当前归属和原 exact read 规则校验；合法历史描述符存在但图片不可读时，读取必须显式失败。

USER_MESSAGE/USER_STEER 图片纳入普通消息投影；TOOL_RESULT 图片仅在既有 `include_tools=true` 范围内纳入。HTML 可视化正文/摘要不是图片引用；只有已经保存为图片工具结果的截图可按其图片 digest 重读。

### 6.4 纯图片消息与搜索语义

纯图片消息必须在历史分页读取中返回非空图片占位，不能因无文字而跳过或无进展循环。用户不知道关键词时，可使用 `read_session_content` 的既有默认位置、方向与 cursor 浏览历史。

BM25 继续检索原有真实文字字段。图片 digest、尺寸、占位文案和工具使用提示不作为新的关键词命中来源，不假装支持图像内容搜索。没有用户文字或相关回答提供检索线索时，不承诺“搜索图像内容”能够命中。

共享 canonical 图片事实及 part 顺序，不要求搜索 excerpt 与精确读取拥有相同的文本字节或字符 offset。两种输出明确分工：

- `history_projection` 与搜索/自动召回采用固定的短标记 `[历史图片；此处未包含图像内容]`，不含 digest、尺寸或读取动作指引；持久的可重建投影保持 caller-neutral。
- `read_session_content` 在当前会话返回第4节完整占位，在其他会话返回第6.2节的非操作性说明。其字符坐标由这次固定目标会话的精确读取视图拥有，不使用搜索 excerpt 的字符坐标；两者通过已有 `session_id + entry_id` 连接。

短标记只在展示文本中出现，不进入 `fields`/BM25 body 或词法命中；但不得作为零成本的无限文本绕过最终工具/模型预算。保持既有 excerpt 正文配额与包装计量的分工，标记及包装均进入最终输出计量。原文中用户自己写的 `sha256:` 文字仍按普通真实文字处理，不以字符串形状删掉它。

复用已有可重建投影，不增加索引表、图片文本标签数据库、caller 专属缓存或后台重建任务。修改展示投影时须同步更新原有 canonical refetch 一致性校验和可重建数据，不能让旧投影因新标记永久失配而静默丢掉搜索结果。摘要自动召回没有返回某张图，不等于该图从历史消失。

### 6.5 分页与完整引用

完整图片占位是一个不可拆分的输出片段；普通长文字继续使用现有字符分段。同页必须同时满足 `max_chars`、结果包装字节和工具结果预算，不能仅计算引用本体。

- 页剩余空间不足以容纳下一个占位时，在占位之前结束，cursor 指向未消费的位置。
- 空页预算连一个完整占位都容纳不了时，返回现有资源不足类别，并说明需要更大的单次页预算；不能返回成功空页加不前进 cursor，也不能越过用户给定预算。
- 超长消息或大量图片使用连续页，不规定每消息、每会话可找回图片总数。
- 搜索 excerpt 的短存在标记同样整段保留或省略，不返回半截生成标签；精确展开原 entry 时仍可取得完整引用。只需在现有 excerpt 选择/渲染接缝处理这种确定的生成片段，不建立通用富文本分页框架。
- generated 占位不能伪装成用户原文，也不能参与现有批注 quote/comment 的权限或搜索语义。

占位加入后会改变模型文本视图及 offset。内容读取 cursor 判别版本需随新视图一次性 hard cut；旧 cursor 返回既有无效 cursor 错误并要求重开查询，不双读旧/新 offset。必要的搜索 continuation 与可重建投影版本沿现有 owner 更新，不为此创建新 generation、lease 或持久游标表。

## 7. 端到端模型行为

| 情况 | 模型与系统应采取的行为 |
| --- | --- |
| 图片仍在当前模型输入 | 可直接看图，不强制重复调用工具 |
| 当前上下文有完整图片占位 | 仅表示存在恢复入口；现有信息够用就直接继续，缺少必要视觉信息时可调用 `view_image` |
| 已有准确消息定位但没有图片引用 | 确实需要原图时可直接 `read_session_content`，无需先搜索 |
| 只记得“前面的报错截图”，没有引用或消息定位 | 现有文字不足以完成当前任务时，可搜索或浏览相关历史，再决定是否重读 |
| 原消息没有任何文字线索 | 需要找回时可按已有历史分页浏览；无法判断是哪张时可向用户澄清，不猜引用 |
| 多张图片或同图多次出现 | 结合消息、图文位置和用户问题判断；引用相同不合并不同历史语境 |
| 纯文本模型取得了引用 | 可理解文字记录和附件身份；需要视觉核对时说明能力限制，不声称已经看见 |
| 跨会话或 child 未持有合法 owner | 不扩大权限、不自动复制、不从父会话补图；沿现有不可用结果处理 |
| 引用读取失败或图片超本次额度 | 返回现有明确错误；不伪造 OCR/图片描述，不无限重读，不自动修改图片 |

重读的工具结果必须包含真实图片，并追加在当前前缀之后。存储去重不免除每次图片 occurrence 的资源计量；不把图片插回已安装的旧消息位置。

## 8. 实施所有权与修改落点

依赖分工：PostgreSQL 拥有事务、关系与字节保存，已有 blob/prompt codec 拥有内容完整性，已有 planner/continuity owner 拥有后继选择与采用，已有查询 owner 拥有范围、授权及分页，`view_image` 拥有工具执行与重读接纳。本次适配只连接这些已有 owner。

| 修改位置 | 最小职责 |
| --- | --- |
| `model_input/lowering.py` 或其已有图片 formatter 接缝 | 从 typed 图片事实渲染恢复占位；保留实际图片标签逻辑；不加泛化媒体框架 |
| `compaction/planner.py` | 形成历史图片占位候选、保留 source order 与保护范围、统一纯文本交接投影 |
| `compaction/coordinator.py` | 在原后继候选流程中接纳占位候选；不在采用前发布结果 |
| `compaction/prompt.py` | 摘要只按任务需要保留引用；不添加每次后继的强制恢复说明，不要求全量引用目录 |
| `history_projection.py`、`history_excerpt.py`、`session_content.py` | 共享图片事实与位置；caller-neutral 搜索存在标记与按目标会话精确读取分工；纯图片消息及原子分页 |
| `session_text.py` / canonical prompt 解码接缝 | 复用校验与 descriptor 提取；避免直接修改共享 UI 文字解码造成提示/摘要/搜索污染 |
| `prompt_storage.py` 现有图片读取 owner | 复用 metadata/ref 校验；不复制第二套图片解析或授权，不新增存储路径 |
| `capability/builtin_catalog.py`、`ports/system_prompt.py` | 工具统一说明引用也可来自历史读取；保持按需原则、直接读取能力及纯文本/跨会话限制，不重复催促检索 |
| 现有 schema/codec、cursor 和 clean-v0 定义 | 仅在内容视图改变实际需要的地方更新版本与断言；不维护兼容路径 |

新工具说明和系统提示只在既有允许的 epoch 根建立处进入请求，不得通过运行时能力刷新改写已安装 SYSTEM/tools。

不增加 durable event kind、live event kind、subject slot、append guard、product relation 或 durable job。snapshot 中占位是既有历史文本；不增加 offloaded registry 或图片存活租约。原有 `canonical_image_refs` 与 blob GC 继续拥有真实生命周期，文本占位不是新 GC 根。

## 9. 实施顺序与 hard cut

1. 在精确历史读取中补齐 descriptor → 完整引用占位，并闭合纯图片读取、分页和跨会话说明；搜索/召回仅增加短存在标记且排除生成的词法字段。
2. 让压缩 planner 在同一个后继接纳流程中使用中立占位；更新纯文本交接，保留原后继 continuation 语义。
3. 更新工具描述、提示词、cursor 视图版本、受影响的可重建投影、测试与 dogfood。
4. 一次性删除本范围内的裸图片省略与跳过图片的旧模型投影路径；生产只保留新契约。UI 的其他用途不被误删或强制套用模型提示文案。

这些是开发依赖顺序，不是长期双轨发布阶段。历史归档可保留；如果最终代码触及 canonical schema，按既有 clean-v0 hard cut 处理，不为未发布内部状态建立在线迁移链。

## 10. 验证与激活要求

### 10.1 定向测试

| 范围 | 必须证明的行为 |
| --- | --- |
| 占位 | 完整 digest/尺寸/MIME来自真实 descriptor；同图多 occurrence 保留顺序；伪造相似用户文字不建立 owner |
| 按需行为 | 占位无动作指令，压缩/标记本身不产生查询调用或任务；已知entry可直接read、已知ref可直接view，搜索不是前置条件 |
| 普通压缩 | ROOT 每个 recent 窗口按带图/全部可选历史图占位的闭合顺序选择；同一摘要与冻结source复用；当前活动请求和保护 tail 不变；非资源失败不降级 |
| 模型切换 | 文本目标无视觉 payload且保留可恢复身份；切回视觉不自动复原旧位置；新图仍按原门禁处理 |
| 连续压缩 | 引用被后续摘要完全省略，仍能搜索/分页定位 ROOT 原消息并重读；不累计全量引用清单 |
| 历史读取 | 文本夹图、纯图片、多图、工具图片均正确；include_tools 与当前原始历史范围不变 |
| 分页 | 极小页预算、占位恰好容纳、跨页前后文字、多字节文字、包装超界、旧 cursor 拒绝；无跳图/半引用/不前进循环 |
| 搜索 | caller-neutral短标记不制造命中、不产生零成本无限输出；原词法结果不退化；投影/refetch一致；同一entry可展开取得完整引用 |
| 权限 | 跨会话阅读不授予视觉访问；独立会话同 digest 不越权；pending queue/孤立 blob 不成为读取依据 |
| 生命周期 | 冷启动、重复压缩、已有 fork 复制范围、原 owner 删除、损坏、取消与采用失败符合原语义；worker不新增自动卸载/历史发现 |
| 输入连续性 | 采用前及同 epoch SYSTEM/tools/已安装 messages 前缀逐字节不变；重读仅追加新的结果与图片 |
| 资源 | 占位及包装计入实际报价；小图转文字不一定节省；文本上限拒绝不被当成完整性错误吞掉；原token/wire/soft/force规则保留；source超限不借本功能重写 |

优先复用并扩展 `tests/test_kernel_image_input_k3.py`、`tests/test_kernel_image_input_k4_lifecycle.py`、`tests/test_kernel_image_input_k2_postgres.py`、`tests/test_local_image_tool_executor.py`、`tests/test_session_content_query.py` 及既有历史检索测试；不通过删断言、skip/xfail 或模拟一个不经过真实 owner 的成功路径验收。

### 10.2 真实 provider 与长对话 dogfood

正式激活必须经过真实 provider 路径。使用 `LocalSettingsStore` 和 `require_pulsara_home()` 读取保存的生产配置，保持配置只读；隔离使用已验证的本地可丢弃数据库及已有注入方式。缺少视觉连接或必需配置时记录缺项，不编造成功证据，不引入环境凭证 fallback。

验收分开证明确定性恢复与真实模型使用，避免把摘要模型随机遗忘作为必须无限续跑等待的条件：

1. **占位重读**：发送多张可区分的图片及交错文字，触发真实压缩后继的图片卸载，再询问必须核对图像细节的问题；观察模型复制完整引用调用 `view_image`，实际下一次 provider 输入收到图片并正确回答。
2. **引用缺失时的确定性恢复**：集成测试用明确标注的受控 summary 模型输出，经生产压缩采用与持久化路径构造不含目标图片和引用的后继，经历重复压缩及冷启动后，验证历史查询和重读仍成功。不得直接改 snapshot、删数据库字段或编辑证据绕过真实 owner。
3. **真实模型主动恢复**：在已确认当前输入不含目标图片和引用的合法后继中，向真实视觉 provider 提出现有文字无法回答的旧图视觉细节问题，观察其按需调用历史读取（必要时先搜索）取得引用，再调用 `view_image` 并回答细节。初始后继可以来自自然摘要，也可以复用上一项经生产采用的受控准备；若使用受控准备，证据必须区分“摘要准备为受控输出”和“历史恢复/读图/回答为真实 provider”，不能称整段压缩都是自然模型行为。

自然多轮摘要确实遗忘时，保留该额外长对话证据；没有自然遗忘不要求无限增加轮数。每个测试使用明确的场景输入与既有测试资源预算；未完成真实恢复调用链则仍未满足激活条件。受控准备不能替代第一项真实摘要与后继卸载验证。

额外验证纯文本模型能查到身份但不能看图、纯图片消息能够分页定位，以及压缩后继失败不会改变现有前缀。增加一个只凭现有文字即可回答、旧图片与当前问题无关的真实 provider 场景，观察占位是否诱发无必要的查询/读图；若出现则检查任务材料及提示，不能以“恢复链被触发”当成功。确定性测试须证明运行时不自动查询，文案不催促读取；不以固定工具调用次数建立模型行为或运行时总量上限。测试中的对话规模是覆盖样本，不得反向变成运行时图片数、轮数、重读次数或生命周期上限。

证据保留必要的用户输入、占位/查询输出、完整真实引用、工具调用、provider 图像 part 形状、模型回复和失败细节；只剔除实际凭证值。图片身份 digest 是现有内容边界，允许保留；不记录代码、文档或验收文件的 SHA。

### 10.3 完成条件

只有两条恢复路径都通过真实端到端验证，并完成权限、资源、长对话和前缀连续性检查后，才可将本文状态更新为已实施/已激活。记录实际命令、结果与证据位置，明确仍未覆盖的场景；不能把文档写完或单元测试通过当作功能完成。


## 11. 实施与验收记录（2026-10-14）

### 11.1 已落地行为

- ROOT 普通压缩沿原 recent suffix 顺序尝试原图、全部可选历史图占位；只对已有资源/回收不足类别继续候选。复用同一摘要和候选 family，保留原后继规划超时起点、活动请求、受保护 tail 和采用事务。
- 纯文本交接统一生成第4.2节的三行中立占位；ROOT 先保留近期文字和引用，再依实际资源缩小窗口。worker 未新增普通卸载候选。
- 当前会话精确历史读取返回完整引用，跨会话返回非操作性图片说明，工具图片仍受 `include_tools` 控制。只读取 canonical descriptor/ref 元数据；实际图像字节仍由原 `view_image` owner 读取。
- 搜索/自动召回只展示短存在标记，生成文本不进入词法字段；标记不会作为零成本内容无限进入 excerpt，并整体保留或省略。精确读取按占位边界分页，旧 `read_session_content.v2` cursor 拒绝，新视图使用 v3。
- 未增加表、事件种类、durable job、附件副本、图片引用注册表或新的图片授权。原始消息和图片 refs 不改写；占位是普通历史文本。

### 11.2 测试与本地派生数据

共 **452 项相关测试通过，无新增 skip/xfail**。覆盖 `test_stage2_conversation_runner`、`test_session_content_query`、`test_history_recall`、K2/K3/K4 图片测试、`test_local_image_tool_executor`、`test_image_history_recovery`、长上下文压缩、worker canonical history、fingerprint 架构和最终输入预算测试。新增断言覆盖实际采用、候选复用、活动请求不变、原子分页及包装边界、纯图片消息、搜索排除生成字段、跨会话说明、图片 payload 零读取、纯文本上限的明确资源失败。静态 Ruff 检查和 `git diff --check` 通过。

测试分组日志保存在 [验证目录](../output/image-history-verification-20261014/)：332 项相关回归、93 项 K2/K3，以及24项图片工具和3项图片历史测试；`image-local-history-108.log` 与上述 K3 有重复，不按日志简单相加。目录中的 `run_tests.py` 是本次实际使用的启动器：从只读 `LocalSettingsStore` 获取并确认 loopback PostgreSQL 配置，不读取 `.env`，测试 fixture 自建和销毁隔离数据库。

已有开发库 `postgresql://pulsara:pulsara@localhost:5432/pulsara` 经原 schema verification owner 验证后，调用现有 `rebuild_history_projection` 一次性重建展示投影。重建前后均为6条派生文档，未更改 canonical 消息/图片；记录见 `projection-rebuild.log`。新安装沿原 clean-v0 正常生成新投影，不需要 schema 迁移或兼容双读。其他已有开发库更新本代码时也应使用该已有重建函数更新其可丢弃投影。

### 11.3 真实 provider 验收

使用保存的 `openai/gpt-6-luna` 连接，通过生产 Host、SDK、HTTP、PostgreSQL、压缩采用及工具 owner 执行。保存配置只读；运行使用经确认的 loopback 临时数据库，结束后已删除。没有导出模型凭证、改写生产设置或使用环境凭证 fallback。

最终通过命令：

```sh
.venv/bin/python -m tests.dogfood.run_image_history_recovery_dogfood \
  --model openai/gpt-6-luna \
  --output output/image-history-recovery-20261014-h
```

[最终原始报告](../output/image-history-recovery-20261014-h/report.json) 为 `passed: true`，包含15次真实 provider 调用及2次明确标记的受控摘要输出。所有真实请求均核对实际 HTTP 200 请求体与冻结 final-wire materialization 相等。

| 场景 | 实际结果 |
| --- | --- |
| 真实摘要后采用图片卸载 | 同一摘要依次尝试四个候选：原窗口带图、不带图均不回收；缩小窗口带图回收不足；占位候选通过。源报价33312 tokens，采用候选26427 tokens，无图片 visual tokens。没有为每个候选重新调用摘要。 |
| 中立性 | 后继仅有图片占位时，独立问题 `4 + 7` 直接回答 `11`，没有历史查询或读图工具调用。 |
| 占位重读 | 模型复制 alpha 完整引用调用 `view_image`，下一次真实请求包含对应图片，回答 `A magenta triangle.`；同 epoch 前缀保持相同根和追加关系。 |
| 重复压缩与冷启动 | 两次受控摘要均经生产采用，不编辑 snapshot；丢弃会话运行态后冷恢复，程序通过原历史读取及原图片读取 owner 找回 beta 原始编码字节。 |
| 无引用的真实主动恢复 | 冷恢复后的初次模型输入不含目标图片或完整引用。模型先分页 `read_session_content`，随后选择 `search_session_content` 定位，再精确读取取得引用，最后 `view_image`；真实请求含 beta 图片，回答 `The bottom-left shape is a blue oval.`。搜索由模型自行选择，不是读取前置条件。 |

受控部分仅用于确定“后继不带目标图片及引用”的准备；第一项摘要是真实模型输出，历史检索、读图和最后回答也均为真实 provider。测试中的消息规模、资源策略和验收 deadline 仅属于场景配置，没有成为生产生命周期或图片数量上限。

### 11.4 明确的验证限制与失败证据

- 真实 dogfood 使用自定义简短 SYSTEM；中立性结果覆盖该场景中的占位行为，不代表已验证完整生产 `DEFAULT_SYSTEM_PROMPT` 的交互效果。实施 review 后按用户要求不重跑真实 provider dogfood。
- 保存配置中没有可确认的纯文本连接：已解析连接为多模态，另有一条能力未知。纯文本新图拒绝、交接身份保留及 `MODEL_IMAGE_INPUT_UNSUPPORTED` 已由定向测试覆盖；本次不声称跑过真实纯文本 provider。
- 真实模型并非每次都正确恢复。先前试跑曾抄漏 digest 字符而被参数校验拒绝，也曾误用 `search_sessions` 后放弃；这些报告保留在同日 `-e`、`-f` 目录，没有放宽引用校验或偷偷替模型补齐引用。最终问题明确指向本会话旧图，没有把检索要求塞入占位。
- 验收脚本也修正了两个旧接口/环境假设（Host 关闭参数、macOS 临时目录规范路径），以及一个 fixture 标签错误：beta 实际绘制的是160×180的蓝色椭圆，先前误标为 circle，模型正确回答 oval。旧失败报告保留，最终运行采用正确的形状断言，没有接受错误形状来获取通过。
- 本次没有将当前视觉模型的成功提升为所有模型都必然主动调用工具的承诺，也没有把受控摘要称作自然遗忘。跨会话授权、child 自有 owner 范围和纯文本视觉限制仍按第1.1节执行。

### 11.5 实施 review 后的修正验证

恢复第6.4节规定的计量分工：生成的图片标记不占 excerpt 正文配额，完整渲染仍受现有搜索工具输出与自动召回准入预算约束。`view_image` 顶层说明同步接受本会话图片旁或历史图片占位中的完整引用，消除与参数说明的冲突；占位固定文案不变。

本轮 `test_image_history_recovery`、`test_history_recall`、`test_session_content_query` 共101项测试通过，包含真实 PostgreSQL 搜索对接近1024-token正文配额的批注附图回归，以及大量图片标记的最终输出预算校验。Ruff 与 `git diff --check` 通过。本轮没有新增真实 provider 验收记录。
