# Pulsara HTML 与图片统一展示规格

归档说明（2026-10-14）：本规格范围内的代码实施与审阅已完成，验收范围和限制保留于 §12–§13；归档不代表磁盘生产配置已切换或所有可选 provider 场景均已验证。

状态：**已冻结并实施。** 2026-10-14 已完成统一展示、工作区共享读图、历史恢复、协议与 clean-v0 更新，并由原 GPT‑6 Astra（high）审阅代理复核代码，无剩余 P1/P2。实施验收记录见 §12。

本轮用户已确认的产品语义：**同一工作区内共享图片读取权限，保留消息等既有 owner 对图片的保存归属和现有 GC 规则。** 本篇明确共享读取、历史恢复与保存生命周期的边界，并闭合内容传输和异步准备两个实施接缝。

日期：2026-10-14

代码核对基线：`0fea8185`。身份规则见[统一身份 hard-cut 实施规格](PULSARA_UNIFIED_IDENTITY_HARD_CUT_IMPLEMENTATION_SPEC.zh.md)，其实施与验证记录见[验证记录](PULSARA_UNIFIED_IDENTITY_HARD_CUT_VALIDATION.zh.md)。图片引用已完成输入/offload/read 链路切换，本篇直接使用现行身份，不再提出短引用适配层或另一轮图片 ID 改造。

本次明确替代上述身份规格 §5.1–§5.2、§10 中图片读取资格须各自按会话建立的限制，及 §12 对“跨 session 无 owner 必须拒绝”的验收预期；也替代此前 offload 规格中“跨会话恢复不提供可操作引用”的一概限制。替代范围仅为本篇规定的图片读取、恢复与展示语义。身份编码、内容完整性、文本历史权限、保存归属、GC 和 provider 前缀连续性保持。本次实现已统一切换为工作区共享读取，不保留两种授权模式或会话级回退。

## 1 目标与范围

**模型用一个显式容器表达“把这些内容直接展示出来”。** 沿用 `pulsara-visualization` 围栏，扩展为可混合 HTML 文件、本地图片和当前工作区图片引用的图集。一个围栏是一组图集，主展示区一次呈现一个项目，用户通过缩略图切换。

统一展示协议、图集导航、消息归属与保存生命周期；内容内部保留 HTML 和图片类型。HTML 继续在沙箱内运行，图片使用直接图片渲染，不为普通图片生成 HTML 包装文件。

首版覆盖现有 HTML 路径以及图片读取链路支持的 PNG、JPEG、静态 WebP。本规格不扩展动画、多帧图像、远程下载、PDF、任意二进制预览或图片编辑能力。现有 Mermaid、SVG 代码块与远程 Markdown 图片行为不在本次改动范围内。

## 2 模型表达与分组

### 2.1 显式展示容器

下例中的 `image:<26位规范Base32>` 为协议示意；实际输出必须复制 runtime 已提供的完整图片引用，不能输出尖括号、猜测标识或自行截短。编码细节仅供实现者使用，不加入模型使用说明。

````markdown
```pulsara-visualization
test1.html
image:<26位规范Base32>
output/chart.png
```
````

三行对应三个项目，保持书写顺序。初次打开默认选中第一项：HTML 项显示可交互页面，图片项显示图片。HTML 使用现有截图缩略图，图片使用自身缩略图。

多个围栏互相独立。需要同时在正文展开两张图时，分别写两个围栏，可在中间插入解释。不得自动把整条回复、整个会话中的图片合并为一组，也不根据视觉相似度推测分组。

围栏保留现有语法：必须位于 Markdown 顶层，明确闭合，信息字符串仅为 `pulsara-visualization`，每个非空行是一项。空行、空围栏、缩进、换行规范化以及嵌套代码、引用、列表、数学块的识别沿用现有解析约定；空围栏保留一个失败项目，不能让后续 ordinal 错位。带语言参数的围栏不激活展示协议。

容器内不接受内联 HTML 源码、JSON 配置、caption 参数、工具调用或 Markdown 图片节点。模型只负责选择项目、分组和周围的说明文字。

### 2.2 普通引用保持文件样式

下列本地资源写法提供文件样式入口，不自动展开成图片或图集：

```markdown
![结果图](./output/chart.png)
![之前上传的图片](image:<26位规范Base32>)
![某个 HTML 文件](test.html)
```

普通链接同样不触发发布。路径入口点击后复用当前文件预览能力，读取当前文件；它不承诺提供当时的文件快照。图片引用入口点击后由已鉴权的当前会话确定工作区，按工作区共享规则读取对应已保存图片。文件样式可使用替代文字或文件名，不把完整内部哈希作为默认用户标题。

普通引用不创建新的图片 owner。链接被传给同工作区的另一会话或 fork 后，只要工作区内仍有合法可读 owner，即可打开；接收会话无需自己持有图片。链接本身不保证长期保存。显式容器成功提交后则创建消息拥有的展示位置，即使其他会话的原 owner 后来删除，这条展示消息仍保留图片。两种入口的外观可以相似，保存语义必须区分。

本规则适用于助手聊天正文。Markdown 文件预览中的相对图片资源、用户上传附件、SVG/Mermaid 渲染以及现有远程图片策略保持原范围，不能通过修改共享组件误伤这些场景。

### 2.3 引用类别

现行图片引用为 `image:` 加26位小写、无 padding 的规范 Base32，编码128 bit，完整字符串32字符。复用 Python `require_id(..., "image")` 与前端 `isCanonicalIdentity(..., "image")`；末位合法集合等规则由现有 identity 模块拥有，不在展示解析器中复制算法。

| 值 | 现行身份与用途 | 本篇决定 |
| --- | --- | --- |
| 模型图片引用 | `image:`；`D(image, content, [workspace, media_type, binary, 完整digest])` | 输入占位、`view_image.image_ref`、普通图片入口和展示容器共用同一 ID；精确解析 |
| HTML 保存内容 | `blob:` canonical 内容身份 | 继续由 blob owner 管理，不新增模型可用的 `html:` 引用 |
| 展示位置 | `(session_id, assistant_entry_id, ordinal)` | 保留现有复合键；不增加 `visualization:`、gallery ID 或随机展示 ID |
| 内容完整性 | `sha256:` 加64位 hex 与字节数 | 继续在存储、传输和读回校验中使用；不是模型展示引用，不能截短或拿来授权 |
| 文件版本 | `revision:` 完整256-bit值 | 保持现有文件工具契约，不用作图片展示身份 |

`image:` 已是图片 blob 的 canonical 主键，不是短串到 SHA 的别名。相同 workspace、相同 MIME/codec 和相同完整字节内容可以复用 ID；不同 workspace 的图片身份不同。同工作区的会话或子代理取得完整引用后，可按 §5.3 读取或发布，不再要求调用会话先有图片 owner。工作区由当前已鉴权执行上下文确定，引用本身不是跨工作区的能力令牌。原对象与 owner 全部回收后的剩余碰撞风险沿用统一身份规格，不新增永久墓碑。

现有 `CanonicalVisualization.visualization_ref` / 前端 `visualizationRef` 实际承载完整内容 digest，并非独立可视化 ID。本轮实现应在展示 DTO/wire 边界将其明确命名为 `content_digest` / `contentDigest`，保留摘要强度；图片可另外投影其已有 `image_ref`。不把所有含 `sha256:` 的字段再次当作身份缺陷。

容器中的 `image:` 值只走图片引用解析；错误长度、padding、大小写或类型不能回退为文件路径。`sha256:`、`blob:`、`revision:`、`photo:` 等不成为新的发布引用。需要表达包含冒号的真实文件名时使用明确的文件路径，如 `./image:example.png`。旧裸摘要没有兼容解析；[早期调查建议](PULSARA_MODEL_VISIBLE_REFERENCE_AUDIT_AND_DESIGN.zh.md)保留为历史背景，不覆盖已实施规则。

## 3 展示与模型视觉读取

展示是助手向用户交付内容的行为。模型读取是模型获取视觉输入的行为，两者分别触发：

| 行为 | 用户收到什么 | 模型收到什么 |
| --- | --- | --- |
| 围栏展示图片 | 保存后的图片与图集控件 | 不自动增加图片输入 |
| 围栏展示 HTML | 保存后的沙箱页面 | 不自动增加页面截图 |
| `view_image` 读取图片 | 沿用现有工具展示行为 | 图片视觉内容，受模型能力约束 |
| `view_image` 读取 HTML 路径 | 沿用现有工具展示行为 | runtime 对当前 HTML 文件生成的截图 |

纯文本模型可以通过已知路径或图片引用向用户展示图片。展示成功不能被解释为模型已经看过、核验过或理解了图片。是否需要先读图由当前任务决定，不设置发布前强制 `view_image` 的规则。

图集缩略图是 UI 派生数据。生成缩略图不自动产生可供模型使用的图片 owner，不触发视觉代读，也不写入模型对话。

HTML 内容摘要、HTML 展示项身份和截图图片引用仍属不同类型。`image:` 不能指向 HTML 字节。`view_image(path="test.html")` 读取的是调用时文件，不承诺等同于某次已保存的 HTML 展示快照；本次不新增“模型按引用读取历史 HTML 截图”的能力。

已经由 `view_image` 正常返回并保存的 HTML 截图若有合法 `image:` 引用，可以作为普通静态图片展示；这不会恢复原 HTML 的交互。UI 自行生成的 HTML 缩略图没有这种模型图片归属，不借展示缓存对外制造 `image:`。

## 4 解析与不可变发布

### 4.1 发布时机与路径

复用现有 tool-free 助手消息的可视化准备、消息提交和前端投影路径。流式正文可以显示占位或源码，但在助手消息正式提交前不据此持久化展示项；不因为 fence 先闭合就绕过原结算入口提前发布。

相对路径从会话 workspace 解析，与 terminal 当前目录无关。绝对路径和 `~` 沿用当前运行上下文的路径规则。解析使用 runtime 持有的现有路径与读取边界，不让浏览器直接访问任意本地路径。

不接受 URL、data URL 或图片内容的 base64 内联值。HTML 延续 `.html/.htm` 校验；图片复用既有图像读取、格式验证和资源限制，不能只依据文件扩展名信任内容。不通过新增总图数、会话寿命或历史数量限制简化实现。

### 4.2 两类图片来源

| 来源 | 发布规则 |
| --- | --- |
| 本地图片路径 | 读取一次，通过既有图像校验后保存相应不可变内容；展示绑定本次读取的字节 |
| 当前工作区图片引用 | 从同工作区任一合法可读 owner 定位已保存图片，校验内容后关联其 blob；不要求 owner 属于调用会话，不按裸摘要绕过工作区或 owner 校验 |

路径图片必须复用 `HostPromptImageValidator` 的实际解码、静态格式和资源校验，再用现有 workspace 绑定函数冻结 `image:`。提交通过 `publish_in_connection(..., validated_image=...)` 复用/发布图片；不能用普通 blob 分支把相同图片存成另一种 `blob:`，也不能从文件名直接构造引用。图片引用分支由原 `PostgresCanonicalImageReferenceReadPort` 按 §5.3 改为工作区共享读取，复用其完整内容检查；不能仅在展示分支绕过现有会话限制而让 `view_image` 保留另一种规则。两个分支得到同一类已验证不可变图片候选。

同一来源重复出现时，保留多个展示位置和原顺序；blob 可复用，展示位置不可因去重而消失。一个项目失败不阻止同组其余合法项目展示。

路径文件在读取期间变化时，沿用实际读取和校验的结果或明确失败。系统不承诺获得外部文件的事务快照，但一旦发布，之后的文件修改、删除或路径重用都不能改变已保存展示内容。

### 4.3 提交与失败

READY/FAILED 结果沿用现有助手消息拥有的可视化记录，与助手消息在原事务边界内提交。准备失败、取消、提交回滚不能留下可见的半条图集，不引入独立发布事件、完成收据、持久任务或补偿工作流。

无效引用、无权限、文件不存在、不支持的类型或资源超界，按既有失败类别映射为对应位置的失败项目。错误要能帮助用户理解原因，不能泄露无权访问资源的元数据，也不能静默跳过失败项而改变顺序。

区分已持久化的发布失败与临时读取失败。已发布 FAILED 项不因后来源文件出现而自动变为 READY；需要新回复重新发布。READY 项的前端读取发生暂时错误时，可以重试读取同一保存内容，不重新读取源路径、不重新运行工具。

“单项失败不影响其他项目”仅适用于可预期的来源/格式/资源错误。不同内容派生出同一 canonical ID、owner 关联损坏、事务失败、整体 deadline 或取消，继续走原有结算失败路径，不能吞为普通 FAILED 项。构造碰撞必须由现有 blob 发布 owner 比较完整 bytes、workspace、MIME、codec、digest；`IDENTITY_CONFLICT` 回滚本次关联写入，不能加盐、换号、覆盖或接受错误图片。提交确认与未知 ACK 重试比较同一个冻结候选的内容类型、元数据、全部字节、位置和失败事实，不重新读路径。

### 4.4 准备阶段与运行时职责

`prepare_visualizations` 已由原同步 HTML 准备入口改为异步；图片解码仍使用 Host 拥有的异步、短进程解码入口。runner 与 runtime 的调用契约为：

```python
await runtime.prepare_visualizations(
    body, io=runner_io, workspace_id=canonical_workspace_id,
    deadline_monotonic=preparation_deadline,
)
```

`canonical_workspace_id` 来自 runner 已有的 canonical workspace 解析，不能由模型提供。`runner_io` 必须是本次 runner 已有的 `KernelSessionIO` 实例；`preparation_deadline` 在本次准备开始时由既有 canonical deadline 取得一次。runner 直接 await，不能把异步入口再包进 `io.run`，也不能为每项刷新截止时间。

准备依次解析并处理来源：同步文件/数据库读取通过 `await io.run(..., deadline_monotonic=同一值)`；本地图片取得 bounded bytes 后，直接 await 已有 `HostPromptImageValidator.freeze_local_image(..., deadline_monotonic=同一值)`，再绑定当前 workspace。canonical 图片引用复用已保存图片的精确读取与校验，不重新走文件解码。HTML 保留既有 UTF-8 和大小检查。本次准备可缓存重复来源的冻结结果，但输出仍按每个 ordinal 保留位置；全部处理后返回 immutable tuple，再构造原 `PreparedAssistantMessageSettlement`。不新增后台任务、第二个 IO owner 或通用来源解析框架。

复用 `ViewImageTool` 现有路径及 bounded-read 核心；如现有方法要求 `ToolCall`，只抽出供工具与展示共用的读取小函数，不构造伪工具调用。Host 解码器继续拥有实际解码、排队与资源校验，不复制 Pillow 实现。

取消与超时继续由原机制完成物理清理：`KernelSessionIO.run` 等待已准入线程退出；Host 校验器回收已启动解码进程并 drain 接收任务。准备层在这些既有清理完成后传播异常，不叠加新的 shield/任务生命周期。特别是 `TimeoutError` 属于 `OSError` 子类，必须先传播，不能被来源文件错误捕获转成 FAILED；`CancelledError`、identity conflict、owner corruption 及事务错误同样不作为单项失败。已冻结候选的提交、取消和未知 ACK 确认继续由原 settlement owner 负责。

展示的文件读取与静态图片解码使用现有路径权限和物理资源边界；不调用 `view_image` 工具执行，不申请一次模型视觉输入，也不因为当前模型为纯文本或未安装视觉工具而拒绝用户展示。不得拿 provider 的 image modality、每次模型调用图片预算或视觉配额当作 UI 展示门槛。共用 Host 的既有解码资源限制不等于共用模型视觉能力门槛。保留 canonical reader 对来源元数据/body 与图片的既有物理读取上限，以及 Host 对单图 bytes、格式和像素的既有校验；它们不从当前 invocation 的 `image_resource_allowance` 或 provider 预算取得。展示来源按自己的展示行/blob 元数据校验，不为了套用 prompt body 校验而制造虚假的 prompt。

## 5 所有权与生命周期

### 5.1 复用现有展示 owner

扩展 `assistant_visualizations` 的既有展示项语义，使 READY 项可持有 HTML 或静态图片。该消息拥有的记录继续是展示内容的权威来源，blob 继续拥有不可变字节；前端状态和缩略图均为可丢弃派生数据。

沿现有表增加图片宽高的 nullable 字段，继续由关联 blob 的 `media_type` / `codec` 区分 HTML 和图片，不在展示行再复制一套完整摘要或持久类型权威。提交、读回、确认重试与 fork 使用相同的封闭组合校验：

| 结果 | 内容约束 | 展示记录的图片宽高 |
| --- | --- | --- |
| READY HTML | `blob:`，`text/html`，`utf-8`，非空且合法 UTF-8 | 均为空 |
| READY 图片 | `image:`，已支持静态图片 MIME，`binary`，已验证原始 bytes | 均为实际解码出的正整数 |
| FAILED | 无 blob；保留既有失败码与失败详情 | 均为空 |

宽高有独立产品用途：图片显示比例、历史恢复描述及精确读回。已有 blob 表继续拥有 bytes、完整 digest、size、MIME 和 codec。数据库保持原复合主键、外键及 READY/FAILED 互斥约束；跨表内容组合由接受和读回 owner 精确验证。准备 DTO 用明确的 HTML/图片分支承载冻结值，避免复用名为 `html` 的 bytes 字段装图片。

前端投影包括 `state`、`ordinal`、展示名称；READY 再带内容类型、完整 `content_digest`、`content_size`，图片另带宽高与其已有 `image_ref`。它们是原记录和 blob 的投影，不创建新实体或 DTO 指纹。引用来源没有文件名时，使用“图片”及组内位置等中立名称；不把原始 ID 伪装成文件名。前端依消息、ordinal 和服务端授权读取保存内容，不能持一个 digest 就直接读取任意 blob。

### 5.2 展示图片的后续引用

成功发布的图片必须由当前会话助手消息拥有，即使原始路径消失或原输入 owner 被删除，展示消息仍能保持自己的图片内容。

这一 owner 应纳入图片精确读取与合法历史恢复范围：新展示图片可以在后续需要时取得同一套 `image:` 引用；已有图片再次展示不会获得新的视觉含义。复用展示记录表达这项归属，不能再造一套独立图片真相。当前 `PostgresCanonicalImageReferenceReadPort` 仅覆盖原图片消息和 snapshot owner，且候选限定在调用会话。实施时将候选范围改为调用工作区，并增加 READY 图片展示分支：来源记录与其消息/snapshot 仍须精确关联来源 session/workspace，图片 blob 必须属于调用工作区，宽高、MIME、codec、ID 配方和完整 bytes 仍须一致。来源 session 可以不同于调用 session；不能因此删除来源外键校验或改成裸 blob 查询。

展示记录本身就是新 owner，不再为同一展示位置往 `canonical_image_refs` 补写影子归属。FAILED 展示项、普通正文字符串、HTML blob 和 UI 缩略图都不能满足图片读取归属。原图片输入、工具结果和 snapshot 的精确验证保持。

历史发现延续图片 offload 规格的分工，采用**单条消息尾部补充、按全消息展示 ordinal 排序**的读取投影，代替旧草案要求的 Markdown 位置插入：

1. 助手原文及其围栏不变，保持现有文本块拼接规则。只读取这条消息已提交的 READY 图片展示记录，不能扫描原文中的 `image:` 来推断归属或重新读取路径。
2. 同工作区的精确历史读取，包括另一会话，在消息末尾逐项追加“已保存展示项 N”及现有中立 `image_reference_text` 信息。N 对应现有 ordinal 的人类可读编号；不新增可调用的展示 ID。原文已写出同一引用时允许重复，保留每个真实展示位置，不为去重引入另一套 Markdown 源映射。
3. 搜索与自动召回仍仅追加中立图片存在标记，不自动附全量可操作引用。允许读取文字的另一工作区历史只补充非操作性图片说明；原文自带的路径或引用保持原文，不能凭此越过工作区边界。具体授权矩阵见 §5.4。
4. 新增标记/元数据属于派生显示片段，不进入词法命中字段；原文自身的词法行为保持。每个恢复描述为一个原子片段，复用现有 atomic ranges 与分页超界结果，不拆开 ID，不把整条消息或整个图集变成不可分页的大块。
5. 精确读取与搜索/召回采用同一个展示记录投影规则及各自的权限模式；保持既有 event cut 和历史可见范围。UI 正文不追加这段机器恢复元数据，批注坐标仍基于原助手正文。
6. 历史投影只精确联结来源消息、展示行和 blob 元数据，不读取图片 payload、不调用图片 reader、不解码图片。完整字节校验留在实际读图/发布路径。调用 workspace 复用 `SessionContentQuery.invoke` 已有授权查询的 canonical 结果，不能接受外部传入的 workspace 作为历史投影权限。

这类图片先前只有向用户展示的保存事实，并没有自动进入模型视觉上下文，因此不额外触发 image offload，也不修改 compaction snapshot schema。对现有历史查询范围内的原消息，模型再次需要旧展示图时，可以沿现有历史工具取得引用再自行选择是否调用 `view_image`；这不承诺 ROOT 历史工具能查询 worker 消息，见 §5.5。已经通过 `view_image` 读取的图片则继续走原工具图片/offload 链路。摘要可以省略引用；重复压缩不追加全量图片目录，不要求模型例行查历史。

### 5.3 工作区共享读取与保存归属

图片读取的授权单位改为当前 canonical `workspace_id`，不是 session、模型、ROOT/worker 角色、memory domain、工作区显示名称或浏览器提交的任意路径。同工作区已知引用可读；跨工作区即使处于同一 memory domain、能够查询历史文字或知道完整图片 ID，仍不可读取或发布该图片。不引入自动导入、跨区复制或另一工作区回退。

读取请求仍来自已鉴权的会话/Host/工具上下文，服务端从它取得当前 workspace。构造绑定比较不能代替数据库存活检查：旧 reader 通过来源必须属于 caller session 间接挡住已删除调用会话；本次放宽后，必须在同一次 `REPEATABLE_READ` 读取事务中显式确认调用 session 存在且仍绑定该 workspace，再选择来源。不存在或绑定不匹配的 caller 不得借其他会话 owner 继续读取。既有工具权限检查保持；不能让客户端或模型通过填写来源 session/workspace 来切换读取权限。

在调用工作区内，原可读来源种类保持为已接受的 `USER_MESSAGE`、`USER_STEER`、`TOOL_RESULT`、context snapshot，并增加 READY 图片展示项。选中的来源 owner 仍需与其原消息/snapshot、workspace 和 blob 精确关联。只有 queue 的图片仍不是共享读取来源，未提交准备值和无 owner 的待 GC blob 也不公开；“阻止 GC 的记录”与“提供可读来源的记录”并不完全相同。候选显式携带 `source_session_id`；读取原消息、snapshot 和 refs 时使用来源 session 进行精确联结，不能继续误用 caller session。候选选择沿用既有确定性排序并扩展展示来源；选中来源的事实损坏应报 corruption，不尝试另一 owner 掩盖损坏。读取只返回图片及所需客观元数据，不额外返回来源会话正文或私有执行信息。

`view_image(image_ref=...)`、围栏引用发布和普通图片链接读取共用上述规则。共享的是读取资格，不是自动传图：`view_image` 仍受模型视觉能力、工具权限和原资源边界约束；纯文本模型仍可以向用户展示图片，却不能获得虚假的视觉成功。消息 `(entry_id, visualization_ordinal)` 的 UI 读取仍检查它自身的消息归属，不把所有会话正文或 HTML 展示项改成工作区公共资源。

保存归属保持不变。普通链接、历史恢复的引用文本、UI 查看/缩略图/下载及纯字节读取不新增 owner，也不延长保留期；正常 `view_image` 结果作为图片工具结果被接受，或显式展示消息被接受，才按各自原提交规则形成保存归属。不得为了预览添加 pin、lease、访问计数或图片注册表。

读取不保证未来永久可用。删除仍沿原 owner 级联和 blob GC 规则：任一既有存活 owner 都按原规则阻止 GC；全部 owner 消失后按现有回收条件处理，不因旧链接、摘要或其他会话知道 ID 而保留图片。没有合法可读来源时返回 unavailable，不借 GC 宽限期直接读孤立 blob。单次读取的授权时间点是原一致性事务内完成 caller/source 校验与完整 bytes 冻结；此后来源被删除，本次冻结候选仍可按原接受事务发布并取得自己的 owner。新读取必须重新授权；这不保证读取开始后永远保留来源。UI 分块请求的删除行为见 §6.2，不为消除并发删除窗口新增持久保留机制。

当前 blob GC 已把 `assistant_visualizations` 纳入存活 owner，无需新增引用计数表、回收任务或长期保留期限；需要验证图片分支确实采用这条既有外键与 GC 路径。图集选择、缩放和缓存均不成为 durable 权威。

### 5.4 跨会话历史恢复

历史文字的发现权限与图片的读取权限分别判断。当前 `SessionContentQuery` 在现有 memory domain 内授权会话，ROOT 历史查询保持原 scope、event cut、分页、排除项和工具可用性；本次既不扩大这些范围，也不把原本可读的跨工作区文字历史缩窄为同工作区。

| 已有历史读取权限及来源 | 派生图片恢复信息 | 图片按引用读取/展示 |
| --- | --- | --- |
| 有权精确读取，同一 workspace、同一 session | 完整规范引用与中立客观元数据 | 有合法可读来源时可用 |
| 有权精确读取，同一 workspace、另一 session | 同上；不再输出“另一会话所以无读图权限” | 同上；调用会话无需先拥有图片 |
| 有权精确读取，但来源是另一 workspace | 非操作性图片存在说明 | 不可用；文字历史权限不能代替图片权限 |
| 无权读取目标历史 | 沿用原历史拒绝结果，不新增恢复入口 | 历史拒绝本身不扩大或缩小工作区图片权限；若已通过任务等正常途径取得同区引用，仍按 §5.3 读取 |
| 搜索结果或自动召回 | 仅中立存在标记；不生成全量引用列表 | 可按任务需要再精确读历史；不强制下一步读图 |

判断同区必须比较服务端已核实的调用 workspace 与来源记录 workspace，不能继续用 `source_session_id == caller_session_id` 判断，也不能用同 memory domain、同名称或 ID 文本前缀替代。现有用户图、steer、图片工具结果的精确历史投影与新的展示图片投影一起更新，不能只修展示图而保留旧输入图的跨会话拒绝说明。来源不同工作区的非操作性说明应描述工作区边界，避免错误归因于会话不同。

生成的完整引用只来自所读 canonical 图片事实；恢复结果不建立保存归属，不复制来源图片，不自动调用 `view_image`。模型可省略摘要引用、也可暂不读取；引用遗忘时仍由已有、获准的精确历史读取恢复。不会因为启用共享而修改已安装 provider 前缀、补发历史图片或创建工作区图片目录。

### 5.5 子代理与 fork

子代理取得任务、材料、消息或既有上下文中的完整 `image:` 后，与 ROOT 使用相同的工作区读取规则；不要求图片源消息位于它继承的历史里，也不要求它先复制 owner。`context=NONE` 表示不继承历史，不表示禁止读取显式提供的同工作区图片引用。工具授权、模型视觉能力和资源约束仍分别成立。

发现范围不扩大：NONE 不自动附加历史；LAST_N 只带既有选定历史；WORKER_HISTORY 仍要求同会话合法 source task、已结束状态和精确切点，不能因为共享图片而读取另一 worker 的全量历史或跨会话任务树。当前 `search_sessions`、`search_session_content`、`read_session_content` 的工具可用性限 ROOT，本次不向 worker 开放这些工具，也不新增 worker 内部图片搜索、全工作区附件列表或 ROOT 图片自动注入。worker 缺少引用时，沿已有任务/消息协作传递；不为寻找图片另造执行恢复机制。

fork 的读取资格与独立保留分开：现行 fork 复用同一 workspace，child 知道未复制消息中的图片引用时，只要工作区仍有合法可读 owner 就可以读取；这不表示未复制的父消息自动成为 child 历史。复制图片消息/snapshot/展示项时，child 按原规则取得自己的保存归属，父会话删除后仍可保留。只有文本引用、未复制图片 owner 且未接受新图片结果/展示的 child，不获得独立保留承诺。

fork 中助手 `entry:` 按 child 派生，展示 ordinal 保持，相同内容复用同一 `image:`/`blob:`。不得因为 child session 不同就重新生成图片 ID。收集、完整内容校验及插入的 HTML 专用分支仍需扩展图片和宽高；普通 `blob:`/HTML 内容并未因图片共享而扩大读取范围。

worker 真实助手消息同样经过原准备/接受链路，可以持有展示记录；其 UI 展示必须补齐现有两条投影路径：在线活动的 `projectSubagentRuns → SubagentActivity → MarkdownBody`，以及任务检查器的 `list_subagent_task_activities → session_controller → AgentTaskActivityRecord → projectAgentTaskConversation`。metadata 从原 canonical 展示记录投影，读取继续使用真实 `entry_id + visualization_ordinal`。任务检查器的当前对话与已有继承对话均传递内容/缩略图读取 callbacks，保留原 task、cut 和分页范围；已继承的真实消息按其来源 entry 读取，不扫描未继承消息或材料补齐图集。WORKER_HISTORY 当前保留真实的 `source_entry_id`，不得像 fork 复制那样为继承展示派生新的 entry ID；fork 的复制规则仍单独成立。

`subagent result`、`INTER_AGENT_MESSAGE` 和合成 `task-result:<id>` 是结果/协作文本，不是原助手展示 owner。不能重跑其中的围栏发布、自动附加新图片引用、复制 owner，或通过 body 相等为它们绑定某条真实消息的展示项。只有具有真实 canonical 来源及保存记录的助手活动才渲染已发布图集；结果摘要、初始材料与压缩摘要仍按原文本语义处理。

ROOT 的精确历史工具继续只覆盖其原 ROOT 范围，因此“从原历史恢复展示引用”的承诺仅覆盖原查询可见消息。worker 自己保存的展示不自动进入 ROOT 历史查询，也不自动改写最终结果/协作消息；已知同区引用仍可读取，缺少引用时沿既有任务/消息协作取得。本次不新增 worker 图片历史工具或自动恢复入口。

例如 A 上传图片，B 在同区只收到引用：B 可读，但 A 的最后一个保存 owner 删除后，该引用不承诺继续可用。若 B 已成功发布包含该图片的展示消息，或已接受正常图片工具结果，B 的 owner 会按原 GC 规则保留内容；最后所有 owner 删除后再按原条件回收。

## 6 前端体验

复用现有图集外壳、独立分组、缩略图切换和放大入口。单项无需展示空的切换栏；多项按顺序显示缩略图及可辨认名称，失败项同样占据自己的位置。

HTML 继续使用现有沙箱、网络隔离、主题适配与隐藏页面管理。已有 frame 驻留数量是浏览器资源边界，不能转化为图集总项目数上限。

图片直接渲染，保持比例、不裁掉有效内容；正文采用适应容器的展示尺寸，放大模式支持适应窗口、原始比例、缩放、拖动和下载当前保存图片。透明图片使用稳定底色或棋盘格，避免图片与背景聊天文字混合。复用可用的既有图片预览控件，缺失的交互才做局部补充。

切换项目、关闭放大和返回正文不改变原始文件或已保存内容。控件具有可访问名称与键盘操作；HTML 自己的按键、滚动和交互不能被图集全局快捷键无条件抢占。

首版不增加裁剪、标注、图片编辑、视觉相似排序或专用多图比较模式。模型可按任务需要制作包含比较布局的独立 HTML 页面。

### 6.1 现有前端需要的具体适配

- `InlineVisualization` 当前所有 READY 项均读取 UTF-8 HTML 并挂载 iframe。扩展为 HTML/图片两个渲染分支，共用当前选中项、缩略图条和放大外壳；图片直接消费已验证 bytes 与 MIME。现有三个 HTML frame 驻留的资源规则保持，不把图片数量纳入新的图集总量上限。
- `readVisualizationHtml` 与内容协议当前硬编码 `text/html` / `utf-8`。扩展既有 `(entry_id, visualization_ordinal)` 内容目标和前端读取适配，携带明确类型并校验完整 digest/size；图片分支禁止经过 UTF-8 解码或 HTML 截图服务。浏览器对象 URL、缩略图和解码对象为可丢弃状态，取消、切换会话、淘汰和卸载时按现有请求/资源生命周期释放。
- 普通 `image:` 链接当前会被 `classifyFileLink` 判为非法 scheme，并可能在 Markdown URL 清洗时丢失。只在显式标明“助手聊天”用途且已绑定会话的上下文中增加精确图片引用分支；仅检查 `FileLinkContext` 是否存在不够，因为 Markdown 文件预览也提供该 context。该聊天分支中，链接与图片节点均显示文件样式入口；不能全局放开任意 scheme，也不能把引用传给文件系统路径解析器。Markdown 文件预览内相对图片资源继续按原目录和 token 读取。
- 普通图片引用点击在既有会话鉴权的 HTTP/bridge 读边界确定调用 workspace，再经工作区共享图片 owner 取得明确 MIME/尺寸及经过完整校验的 bytes；不要求图片来源 session 与 UI 当前 session 相同。复用已有内容分块传输和图片预览组件；无新模型工具、图片注册表、临时文件别名或任意 blob 下载入口。请求目标、首块元数据及校验契约按 §6.2 实施，不能只修改 Markdown renderer 就宣称链路完成。切换会话/工作区后，旧未完成读取结果不得被当成新上下文的已授权结果，沿用现有请求与资源生命周期校验。
- worker 在线活动与任务检查器按 §5.5 贯穿 typed 展示 metadata 和读取 callbacks；复用同一图集组件，不只完成 ROOT 主聊天。任务结果摘要不能用合成 entry ID 请求内容。
- HTML 缩略图继续用现有沙箱截图及 owner 再校验。图片缩略图复用授权读到的图片 bytes，在浏览器缩放展示；下载亦使用当前保存 bytes，不重新访问来源路径，不触发模型调用。

### 6.2 普通图片引用的内容读取契约

复用 `ReadContentRequest` 和 `CanonicalContentChunk`，不增加 metadata endpoint、模型工具或图片下载服务。

| 边界 | 冻结契约 |
| --- | --- |
| 请求 target | 在既有 oneof 的 `entry_id`、`queue_item_id` 之外加入 `image_ref`。HTTP JSON 也必须恰有一个目标，不能由 protobuf 后写字段覆盖前写字段来消化冲突 |
| 图片目标参数 | `image_ref` 必须通过现有 image 身份校验，且不能同时带 `block_id`、`image_ref_ordinal`、`visualization_ordinal`。不接受来源 session/workspace 字段来切换授权；offset/limit 沿用既有分块范围约束 |
| 调用上下文 | live bridge 由有效 attachment/session，cold history/read-content 由原已鉴权历史会话取得 caller。两条入口均支持新 target，继续校验现有连接/代次/会话边界；最终 workspace 由 canonical session 确定 |
| 返回内容 | 保留 `request_id`、`digest`、`complete_size`、`offset_bytes`、`content`、`complete`；增加 `media_type`、`codec`。图片另返回明确存在的 `image_ref`、`width`、`height`，非图片不携带这三个图片字段。原 entry/queue/visualization 目标也从各自 canonical 事实投影类型和适用图片元数据 |
| 元数据获得 | offset=0 的首块同时提供元数据及图片 bytes；普通 image 链接不要求事先知道 digest/size，不另做一次 metadata 请求。后续每块重复同一元数据供前端比对 |
| 前端验证 | 复用 `readExactContentBytes` 的分块组装与完整性校验。普通图片读取从首块建立预期值，检查请求 image ID、支持的 MIME、binary codec、合法尺寸/size；逐块比对完整 digest、size、类型、ID、尺寸及 offset/progress，最后验证完整 SHA-256。已知消息元数据的读取仍必须对照原预期值，不能静默接受变化 |

后端在现有 `prompt_storage` reader 内抽出连接级的来源解析和完整校验函数，供 model reader 与 canonical 内容读取 owner 共用。普通 image 目标在同一一致性事务内完成 caller 存活、workspace、source owner、描述符、ID 派生及完整 bytes 校验后，才从这份冻结图片切出请求分块。不得先裸查 blob 元数据再把 client image ID 当 blob 下载地址，也不得将真实图片伪装成普通 inline 内容以跳过现有校验。原消息/队列目标继续按自己的 owner 规则读取；queue owner 预览不因此变成已知 image 引用的共享来源。

每个分块请求独立重新授权，不建立跨请求快照或保留令牌。来源在分块间删除且再无合法来源时，后续请求返回 unavailable，前端丢弃未完整验证的拼接结果；若仍有另一合法来源，可以继续，但所有内容事实必须一致。不存在、跨区、无合法来源返回既有内容不可用类别，不泄露源会话；损坏返回既有 corruption 类别，非法目标与越界沿用对应请求错误。复用既有每块大小与单次读取资源边界，不增加图集总量或会话 lifetime 上限。

`AbortSignal`、会话切换和组件卸载贯穿读取与资源释放；迟到结果不能装入另一个会话/工作区的预览。已经完整授权、交付且校验的 bytes 可在原预览生命周期内继续使用，不承诺删除能撤回已交付内容，也不把缓存作为新读取的授权依据。没有新 owner、pin、lease 或访问续期。

图片放大优先复用仓库已安装的 `yet-another-react-lightbox` 与 Zoom 插件，以及 `prompt-content-view` 已有的尺寸、错误重试、焦点返回和请求清理方式；只适配图集所需的保存 bytes、当前项与 HTML 切换，不另造图片交互框架。HTML 仍由原 iframe/截图 owner 管理。

## 7 前缀连续性与中立性

发布图集不改变当前 epoch 的 SYSTEM、工具定义或已安装消息前缀，不将前端 READY/FAILED 状态静默追加为新的模型消息。展示项目的截图和图片字节不自动进入下一次 provider 请求。

更新后的展示说明与 SYSTEM 仅在已有获准的冷 epoch 或显式采用的压缩后继边界生效。本轮不再切换 `image:` 身份格式，也不能借此重写已安装 provider prefix。

沿用已冻结的图片占位中立性：历史占位本身不请求读图或展示；摘要可以省略引用；模型有需要才恢复。现行 `image:` 由原图片 owner 生成，模型无需理解26位编码、owner 表、内部摘要、缓存或截图实现。

模型说明只需表达以下事实：使用围栏直接展示；每行一项；一围栏一图集；使用分开的围栏同时展开；原样复制图片引用；已知同工作区的引用可读取或展示，无需先导入当前会话；展示不要求先读图。HTML 制作约束继续放在现有相关说明中，不在每个图片占位内重复。共享读取规则不构成要求模型查找或重读其他会话图片的任务指令。

## 8 实施边界与冻结决策

| 边界 | 现有 owner 或依赖 | 必须完成的最小适配 |
| --- | --- | --- |
| Markdown 识别 | 后端 markdown-it 与前端 remark AST | 保持相同顶层围栏、分组和 ordinal 语义，扩展项目类型 |
| 本地读取 | runtime 路径解析、现有图片验证、HTML 文件读取 | 使用已授权读取上下文，保存同一份校验后的内容 |
| 消息发布 | assistant settlement 与 `assistant_visualizations` | 接纳图片内容类型，保持原提交事务与失败结算 |
| 图片引用 | 已实施的 identity 编码与 canonical 图片读取 owner | 精确 `image:` 解析不变；可读来源由调用 session 改为调用 workspace，新增 READY 展示分支，保持来源 owner 联结及完整性验证 |
| 准备与取消 | runner/runtime、物理 IO owner、Host 图片解码器 | 协调同步读取和异步解码，冻结同一候选；不串入模型工具调用或后台 job |
| 历史读取 | session content、history projection 与 excerpt | 原图片和展示图一起按调用/来源 workspace 判断恢复引用；单消息末尾投影、cut、词法字段及原子分页保持 |
| 子代理 | 原 ROOT-only 历史工具、worker context/history owner 及活动 UI 投影 | 已知同区引用可用；贯穿在线活动和任务检查器的展示元数据/读取；NONE/LAST_N/WORKER_HISTORY 范围不扩大，结果摘要不成为展示 owner |
| UI 与传输 | canonical protocol、bridge、图集与图片预览组件 | 区分 typed 内容、digest 与 image ID；消息目标按消息归属读取，普通 image 引用经当前会话鉴权后按工作区共享读取 |
| 生命周期 | 现有 fork、删除、blob GC | 图片展示项与 HTML 一样随其助手消息保存和复制 |

图片身份已实施；本版确定：不新增实体身份，展示表只补图片宽高，类型与完整摘要来自 blob；原 reader 显式验证 caller 存活与 workspace，放宽来源 session、保留来源联结并增加展示分支；历史恢复以 workspace 判断并采用单消息尾部元数据补充。工作区共享读取和上述展示适配已按本版实施。

两个实施接缝已具体闭合：§6.2 在现有内容 API 增加互斥 image 目标，首块携带元数据并逐块授权/校验；§4.4 将准备入口改为 async，显式传入 runner 原 IO owner 与同一个 deadline，复用 Host 解码器的取消和物理清理。没有通用 SourceResolver、媒体服务、短 ID 注册层或额外持久状态。

预期不增加 durable 表、event kind、subject slot、append guard、job 或独立 relation 类别，展示项仍属于现有消息关系。若实施需要突破这一点，必须先按 AGENTS.md 补充独立产品必要性、owner、事务和失败路径并修订规格，不能编码时默认增加。

实施时按开发期 hard cut 更新 schema baseline、读写双方、fork、测试、展示/图片读取说明和文档；原图片读取与历史恢复测试中“同工作区跨会话必须拒绝”的预期应改为新的共享规则，并保留跨工作区拒绝、完整性和保存生命周期断言。terminal protocol major 已由4硬切为5，并同步更新 schema 身份及读写双方的精确版本检查。版本覆盖内容目标、分块类型元数据、typed visualization 与 digest 字段更名，不因 protobuf 容忍未知字段就让旧客户端误将图片作为 HTML。无需因此修改模型连接 ID 或 LocalSettings schema。现有 HTML 行为保留，移除“容器只能含 HTML”的旧限制；不添加新旧协议协商、兼容读取或后台修复机制。历史说明文档继续作为历史，不覆盖本规格的目标语义。

## 9 验收

| 场景 | 必须成立的结果 |
| --- | --- |
| 混合图集 | HTML、图片引用、本地图片按一围栏一组展示，顺序和缩略图对应；HTML 保留交互 |
| 多组与普通入口 | 分开围栏分别展开；普通本地 Markdown 引用保持文件样式；文件预览内原相对图片不受影响 |
| 解析一致性 | 闭合/未闭合围栏、空项、嵌套代码/引用/数学块、流式片段在两端得到相同边界与 ordinal |
| 图片发布 | 路径与图片引用得到正确图像；不支持格式和无权限项明确失败；其余项目可展示 |
| 身份与内容校验 | 精确接受规范 `image:`；拒绝裸 SHA、错 kind、截短和非规范 padding；同 workspace 同内容复用 ID，不用引用代替 digest/size 校验 |
| 构造碰撞与提交重试 | 注入不同 bytes 同 ID，整次接受事务回滚且原内容不变；未知 ACK 重试复用冻结图片及全部元数据，不重新读取已修改路径 |
| 快照 | 发布后修改/删除源文件，旧展示不变；临时读取重试读取相同保存字节 |
| 同区共享读取 | A 持有已接受图片，B 无图片 owner：同区的 view_image、围栏发布、普通链接均可按各自能力工作，且共用同一 image ID |
| 调用与来源分离 | A/B 同区且来源属于 A，B 可读；B 删除或绑定失配后旧 reader/请求不得借 A 继续读；来源精确联结使用 A，选中来源损坏不回退其他 owner |
| 普通引用内容协议 | live 与 cold 均可从首块取得元数据并完整展示；混合 target/附加 selector 被拒；错误 MIME、尺寸、ID、digest、size、offset 或零进度不能得到成功图片 |
| 分块并发删除 | 最后 owner 在分块间删除后可 unavailable，未完成结果被丢弃；已完整校验 bytes 仅在原预览生命周期可用，不延长 GC，不装入新会话 |
| 冻结后并发删除 | 授权事务内已冻结的候选可在来源删除后按原事务接受并取得自己的 owner；新的读取重新授权，未冻结读取不得直接利用孤立 blob |
| 跨区隔离 | 同 memory domain 的跨区历史文字可按原规则读取，但图片恢复不提供可操作指引；已知另一工作区 ID 仍不可读/发布，不泄露来源元数据 |
| 归属与生命周期 | 普通读取不新增 owner；B 成功接受图片结果/展示后形成原有保存归属，删除 A 不影响 B；最后全部 owner 删除后仍按原 GC 条件回收 |
| 普通引用与容器区别 | 普通链接/恢复文本不制造 owner；同区再无合法可读来源时 unavailable，即使孤立 bytes 尚未 GC；只有 queue 时仍拒绝共享读取但保留原 GC 阻挡 |
| fork 保留与可读性 | child 未复制图片 owner、仅知道引用时可读同区仍存活来源；复制了 owner 或后续接受新图片结果/展示，才有独立保留，不因共享继承未复制父历史 |
| 子代理边界 | NONE worker 显式拿到同区引用可以依工具/模型能力读取或展示；不自动注入图片、不开放 ROOT 历史工具，WORKER_HISTORY 保持原 source task/cut 校验 |
| worker 展示与摘要 | 在线活动、任务检查器当前及原切点继承的真实消息均可展示/重读图集；合成 task-result、协作文本、材料及压缩摘要不发布/复制展示，也不根据正文相等借用 owner；ROOT 历史查询仍不扩大到 worker |
| 历史与压缩 | 投影只读来源/展示/blob 元数据、不读 payload 或调用 reader/解码；展示图片从原消息末尾恢复；重复展示保持 ordinal；生成片段不参与词法命中；元数据分页不拆分；摘要省略引用仍可按原范围取回 |
| 跨会话恢复一致性 | 同区跨 session 精确读取原用户图、steer、工具图和展示图均给中立可操作引用；跨区均给非操作性说明；搜索/召回仍只生成存在标记 |
| 模型中立性 | 纯文本模型可发起展示；展示无隐式视觉调用；普通问题不因历史图片占位被要求重读 |
| HTML 与截图区别 | HTML 保留交互；已有合法截图引用显示静态图；UI 缩略图不获得模型 image owner；重新 view_image HTML 路径读取的是调用时文件 |
| 前缀连续性 | 发布、缩略图生成和 UI 切换均不修改 epoch 内的 SYSTEM、tools 或消息前缀 |
| 异步准备与结算失败 | 一个准备 deadline、原 runner IO 实例；排队/读取/解码期间取消或超时后完成原物理清理，不留下可见半图集；TimeoutError 不被 OSError 捕获成 FAILED，损坏/冲突/事务错误不被吞掉 |
| 协议与既有入口 | major5 精确匹配；原 HTML、entry/queue 图片预览仍工作；普通 image 入口不改变 Markdown 文件预览，不新增可任意读取 blob 的目标 |
| 浏览体验 | 宽图、长图、透明图、错误项、主题切换、缩放、键盘导航和下载均有针对性验证 |

实现阶段运行相关 Python、前端与 PostgreSQL 测试，并用真实浏览器检查混合图集。真实 provider 是否验收以及具体场景随实施授权确定；实施后追加的真实 provider 验证见 §13。

## 10 现有代码依据

- [围栏解析与 HTML 发布值](../src/pulsara_agent/conversation_kernel/visualization.py)
- [runtime 发布准备入口](../src/pulsara_agent/conversation_kernel/tool_runtime.py)
- [助手消息提交](../src/pulsara_agent/conversation_kernel/_repository/conversation.py)
- [展示与图片 owner baseline](../src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql)
- [现行身份编码](../src/pulsara_agent/identity.py)、[前端精确身份校验](../frontend/lib/identity.ts)
- [图片 workspace 身份绑定](../src/pulsara_agent/conversation_kernel/prompt_content.py)、[blob 碰撞检查与 GC](../src/pulsara_agent/conversation_kernel/blob.py)
- [canonical 图片精确读取](../src/pulsara_agent/conversation_kernel/prompt_storage.py)
- [Host 图片解码校验](../src/pulsara_agent/conversation_kernel/image_validation.py)、[runner 准备与提交](../src/pulsara_agent/conversation_kernel/runner.py)
- [内容协议投影与读取](../src/pulsara_agent/terminal_protocol/canonical_v3.py)、[wire schema](../src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto)
- [精确历史读取](../src/pulsara_agent/conversation_kernel/session_content.py)、[搜索/召回投影](../src/pulsara_agent/conversation_kernel/history_projection.py)
- [历史查询工具 scope](../src/pulsara_agent/capability/builtin_catalog.py)、[worker 历史来源与切点](../src/pulsara_agent/conversation_kernel/subagents/history.py)
- [fork 收集](../src/pulsara_agent/conversation_kernel/fork_history.py)、[fork 接受](../src/pulsara_agent/conversation_kernel/_repository/fork.py)
- [前端围栏解析](../frontend/lib/visualization-markers.ts)
- [现有图集](../frontend/components/inline-visualization.tsx)
- [Markdown 与文件预览](../frontend/components/markdown-body.tsx)
- [普通文件链接分类](../frontend/lib/file-preview.ts)、[会话文件上下文](../frontend/components/file-link-context.tsx)
- [内容读取适配](../frontend/lib/runtime-adapter.ts)、[现有图片预览及 Zoom 集成](../frontend/components/prompt-content-view.tsx)、[HTML 缩略图 owner](../src/pulsara_agent/web_app/visualization_preview.py)
- [分块 gateway 与协议版本](../src/pulsara_agent/terminal_protocol/v3_gateway.py)、[在线 bridge](../src/pulsara_agent/web_app/browser_bridge.py)、[冷历史 HTTP](../src/pulsara_agent/web_app/http_server.py)
- [runner 物理 IO 生命周期](../src/pulsara_agent/conversation_kernel/io.py)
- [worker 活动读取](../src/pulsara_agent/conversation_kernel/_repository/subagents.py)、[任务 HTTP 投影](../src/pulsara_agent/web_app/session_controller.py)、[在线 worker UI](../frontend/components/workbench-view.tsx)、[任务检查器](../frontend/components/task-workspace.tsx)
- [现有模型说明](../src/pulsara_agent/ports/system_prompt.py)
- [已实施的图片上下文卸载与历史恢复规格](PULSARA_IMAGE_CONTEXT_OFFLOAD_AND_HISTORY_RECOVERY_IMPLEMENTATION_SPEC.zh.md)


## 11 冻结复核记录

2026-10-14，主代理与用户指定的 GPT‑6 Astra（high）审阅代理对照当前生产代码交叉复核，确认可冻结。此次关闭的事项为：

- 显式校验 caller session 存活及 workspace 绑定；来源使用自己的 session 精确联结，选中来源损坏不回退。
- 普通 image 引用复用现有 live/cold 内容 API 的互斥 target、首块元数据、逐块授权及完整性校验。
- 异步准备显式复用 runner IO owner、单次 deadline 与 Host 解码清理；取消/超时不被单项失败吞掉。
- 历史投影只读元数据、保持原查询 scope；worker 两条 UI 贯穿真实展示记录，继承 entry 与结果摘要不混淆。
- 区分读取资格与保存归属，明确授权冻结后与分块期间的删除语义，保留原 GC，不增加 pin、lease 或注册表。

审阅结论为无剩余 P1/P2 规格阻塞；两个原待决接缝已由 §4.4 和 §6.2 闭合。冻结确认产品及实施契约，不表示功能已实现或验证通过。冻结时的生产实现仍遵循此前会话范围规则；当时仅检查文档引用和 diff 格式，未运行测试或 provider dogfood。此后实施与验证见 §12。


## 12 实施与验收记录（2026-10-14）

已实施单一生产路径：同一 `pulsara-visualization` 围栏接受 HTML、静态图片路径和精确 `image:` 引用，保存后以一组缩略图切换展示。普通图片引用保持文件样式并在点击时读取。静态图片直接显示保存字节，HTML 继续使用原沙箱与缩略图机制。工作区共享读取与消息保存归属分离，当前调用会话须存活且仍绑定工作区；保存、fork、删除和 GC 继续使用原 owner。

失败保持本地展示事实：准备失败保存 FAILED 项，读取失败在 UI 内显示并允许原入口重试；不生成额外消息、不触发模型轮次，不在下一轮 prompt compiler 注入渲染回执。历史图片描述仅从元数据生成，保持中立，不要求模型重读或展示。

验证记录：

- 相邻 pytest 首批111项通过，第二批225项通过，补充 unified/runner 批次131项通过；批次含重复用例，不相加作为唯一用例总数。覆盖存储、协议/HTTP、历史恢复、fork、图片工具、准备与生命周期。
- 补充真实 PostgreSQL worker 当前/继承历史用例2项通过：在线 projection 保留所有实际展示来源，冷任务历史只保留原 source task/cut 下的来源 entry。UI 投影和图集测试验证在线与冷加载图片元数据不丢失，合成结果摘要不绑定展示 owner。
- unified 场景最终3项通过，包括不同 bytes 构造相同图片 ID 后整组发布回滚、原图不变；冻结候选提交确认以及尺寸漂移拒绝；最后来源删除后的分块读取失效；路径冻结、符号链接分类、超时传播和入口互斥。
- 前端 runtime、图集、工作台、应用、任务检查器、Markdown、文件预览和输入内容相邻测试通过。扩展应用测试发现 Provider 的会话 key 范围过大，HEAD 隔离对照152项通过；修复为仅重建 `SavedImage` 后本次应用152项也全部通过，另验证同会话等价重投影不取消读图、切会话取消旧读取并丢弃迟到 bytes。
- TypeScript、修改范围的 Python Ruff、前端 ESLint、协议生成校验和本地产品构建通过。构建仅有既有大 chunk 提示，已更新随应用分发的静态产物。
- 真实浏览器使用临时组件验收页面验证 HTML/图片/失败项混合切换、透明背景、图片缩放、普通引用点击预览、关闭后焦点返回和原图下载；下载1409字节 PNG 与保存内容逐字节一致。HTML 沙箱正常显示；本轮浏览器自动化工具无法把点击送入其嵌套 iframe，未将交互点击计为通过，原 HTML 沙箱路径及其相邻测试保留。临时验收页面已删除。

原 **Review unified visualization spec**（GPT‑6 Astra high）在相邻 pytest 后审阅实现，发现并复核关闭3项 P2：HTML 符号链接分类、等价投影引起的图片重读、嵌套图片链接按钮。后续又复核图片预览清理范围与 worker 来源证据，结论为无剩余 P1/P2。没有增加服务、身份注册表、模型回执或额外持久生命周期。此阶段未运行真实 provider dogfood，后续追加记录见 §13。


## 13 追加真实 provider 验证（2026-10-14）

经用户明确要求，核实并重置本机 PostgreSQL `localhost:5432/pulsara` 后，在 `/Users/plumliu/Desktop/hello` 新建会话 `session:gfx3kmlkcnpadd7slrpwg4wj4y`，标题为“hello · 图片图集实测”。使用已保存的 DeepSeek V4.1 Flash 连接与 high 推理设置，输入“请展示当前目录里的三张 PNG 图片，放在同一组中供我切换查看。”，未在输入中提供围栏语法。

真实模型调用 `find_files`、`terminal`，未调用 `view_image`，随后输出含三个 PNG 路径的一个 `pulsara-visualization` 围栏。轮次 COMPLETED，三个展示项均 READY，尺寸均为1254×1254。真实浏览器确认三项缩略图切换、图片放大预览，以及刷新后重新打开会话仍能读取保存的图集。源目录文件未修改。

保存的生产配置保持只读；因已有配置尚为旧版，本次启动通过旧 owner 读取后在内存中构造当前设置，并未添加产品兼容路径。临时测试启动器首次缺少 multiprocessing 主入口保护，导致图片解码子进程启动失败；修正启动器并重新清理测试状态后，上述完整链路通过。该记录不将首次失败计为成功，也不表示磁盘配置已经完成版本切换。

用户体验复核后，将图片预览工具栏的“下载”文字改为下载图标，保留悬停提示、无障碍名称和原下载行为。相关前端19项测试、ESLint 与本地构建通过，实际产品页面已确认图标样式。


后续同一真实模型会话又生成 `pulsara-card.html`，与三张原 PNG 放入同一围栏；浏览器确认 HTML 排第一项、图片缩略图切换正常，用户确认 HTML 交互正常。另生成 `pulsara-card-with-image.html`（498,965字节），通过脚本把金色 PNG 编码成 `data:image/png;base64` 内嵌；独立解码与原图完整字节一致，浏览器确认图文处于同一卡片。自动化工具对嵌套 iframe 的点击限制保留为测试工具边界，不将自动点击计为通过。

随后在现有自包含 HTML 的模型说明中补充一句：图片由脚本从本地文件生成 data URL，Lucide 等图标使用内联 SVG。沿用原沙箱和资源内嵌机制，没有新增工具或资源解析器；相关默认 system prompt 编译测试1项及 Ruff 检查通过。
