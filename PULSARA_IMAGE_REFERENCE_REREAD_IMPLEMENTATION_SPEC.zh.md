# Pulsara 已知图片引用重读实施规范

状态：已实施并完成 R1–R4 验收。日期：2026-09-16。实际代码、验证范围与证据入口见 §14。

## 1. 产品目标与范围

用户已经确认：结合聊天图片持久化与本地图片重读的优点，但**不实现历史图片查询、搜索、分页发现或附件目录**。

本次产品行为是：Pulsara 向模型发送图片时，同时给出一个可以原样复制的 `image_ref`。模型仍知道该引用时，可以调用现有 `view_image` 重新取得数据库中保存的图片。原图已经退出有效上下文，也不妨碍这次明确的重读；重读作为新的工具结果追加，不能把历史 provider 前缀改回去。

本轮只闭合“已知引用 → 重新看图”。不保证模型在摘要后仍记得每个引用；没有引用时，模型应说明无法定位，请用户重新发送或指出仍在当前上下文中的图片，不得声称可以查询完整图片历史。

### 1.1 权威顺序

用户明确约定 → [AGENTS.md](AGENTS.md) → 本规格中明确扩展的部分 → [本地图片工具实施规范](PULSARA_LOCAL_IMAGE_TOOL_IMPLEMENTATION_SPEC.zh.md) → [图片输入设计](PULSARA_KERNEL_IMAGE_INPUT_AND_OPENAI_WIRE_ADAPTER_DESIGN.zh.md) 中仍适用的 D1–D4 与前端规则。

代码用于确定已有 owner 和修改落点。§14 只采用本扩展实施后的新证据；旧图片链路的既有验收不替代本扩展验收。

### 1.2 本次保留与扩展

- 保留 PostgreSQL `blobs`、`canonical_image_refs`、原 canonical 正文与事务边界。
- 保留 `view_image(path)`；同一个工具新增互斥的 `image_ref` 输入，不新增第二个图片工具。
- 用户图片、通过 `view_image(path)` 获得的工具图片，以及通过引用重新读取的工具图片，均可获得同一种内容引用。
- 保留图片原始编码字节，不为重读创建永久本地副本，不做缩放、重编码或 EXIF 修改。
- 保留两种 API 的共同工具图片投影：文本工具结果之后，同批成功图片合并成一条派生 `user` 消息。Responses 不另走原生图片 function output 分支。
- 保留现有文件权限、Hook、确认、attempt、并发窗口、取消、late settlement、FULL 确认及 D1/D2 额度机制。
- 保留用户消息、队列的 Figure 展示；工具卡仍在左侧，展开显示大图，无 Figure 编号。

不新增图片搜索、图片列表、分页接口、OCR 索引、语义检索、文件导出、附件后台任务、自动找回、引用补全、失效引用修复或模型主动访问数据库的说明。没有新增 UI 入口的需求。

## 2. 代码基线与复用边界

下表描述写作时的当前实现，不以代码 SHA 或固定行号建立新验收协议。

| 已有 owner / 符号 | 当前事实 | 本轮必要变化 |
|---|---|---|
| [llm/input.py](src/pulsara_agent/llm/input.py)：`LLMImagePart` | 携带完整图片值；已有内容摘要 | 复用现有摘要，不加 attachment ID 或额外 fingerprint |
| [prompt_content.py](src/pulsara_agent/conversation_kernel/prompt_content.py) | `pulsara.prompt/v1` 描述 MIME、尺寸、摘要、编码字节数 | canonical 图片结构不变；引用从已验证内容派生 |
| [blob.py](src/pulsara_agent/conversation_kernel/blob.py)：`PostgresCanonicalBlobStore`、`_blob_id` | workspace + 内容摘要唯一导出 blob 身份；支持 exact read | 继续作为唯一字节读取与内容完整性 owner |
| [prompt_storage.py](src/pulsara_agent/conversation_kernel/prompt_storage.py)：`resolve_canonical_prompt_image_reference`、`hydrate_canonical_prompt_owner` | 按已知消息 owner/ordinal 解析引用、核对 descriptor 与 refs、读取原图 | 增加已知内容引用的 session 归属解析；共用 descriptor/ref 校验与 exact read，不能调用全历史 hydration |
| [canonical_v3.py](src/pulsara_agent/terminal_protocol/canonical_v3.py)：`resolve_content_reference` | 浏览器 attachment/session 读取入口，允许 USER_MESSAGE、USER_STEER、TOOL_RESULT 图片 | 浏览器行为不变；工具不能伪造浏览器 attachment 或绕经 HTTP 调自己 |
| [model_input/lowering.py](src/pulsara_agent/model_input/lowering.py)：`lower_canonical_item`、`compaction_snapshot_provider_content` | USER 图片直接降低；snapshot 有独立内容遍历 | 两处共用图片引用文字渲染，覆盖普通输入与保留内容 |
| [model_input/compiler.py](src/pulsara_agent/model_input/compiler.py)：`_tool_attachment_message`、`tool_image_attachment_message` | 工具图片来源当前必有 `requested_path` | 将来源闭合成 path/ref 二选一，图片旁生成同样的 `image_ref` |
| [compaction/planner.py](src/pulsara_agent/conversation_kernel/compaction/planner.py)：`_render_destination_projection`；[compaction/model_call.py](src/pulsara_agent/conversation_kernel/compaction/model_call.py) | Tier 3 destination projection 直接保留用户/工具证据的 Image，summary 消息直接使用 projection.content，不经过普通 lowering | 在原 destination renderer 中复用同一引用 formatter，纳入原投影报价；纯文本目标先 P，model_call 不重复加标签 |
| [builtin_catalog.py](src/pulsara_agent/capability/builtin_catalog.py) | `view_image` 只有必填 path，filesystem family/read-only 分类 | 修改原 schema、描述；保留原工具身份与分类 |
| [filesystem.py](src/pulsara_agent/tools/builtins/filesystem.py)：`ViewImageTool.read_bounded` | 只负责文件路径读取 | 保留原文件职责，不让 filesystem 层读取数据库 |
| [tool_runtime.py](src/pulsara_agent/conversation_kernel/tool_runtime.py)：`DirectKernelToolPort.invoke` | 模态检查、图片额度、文件读取、共享 validator、typed result | 按来源选择文件读取或 canonical 图片 exact read，然后汇合到已有报价与返回路径 |
| [tool_contracts.py](src/pulsara_agent/conversation_kernel/tool_contracts.py) | invocation 已有 session/workspace、scope、冻结模态和图片额度 | 使用现有真实调用上下文；quote 的 `requested_path` 参数改为闭合来源值 |
| [runner.py](src/pulsara_agent/conversation_kernel/runner.py)：`_quote_post_response_resources`、`_ImageToolResourceQuoteOwner.quote` | 仅识别带 path 的 view_image，报价也调用带 path 的 carrier builder | 两种来源走同一 parser、效果前配额与真实 compiler 报价 |
| [tool_execution.py](src/pulsara_agent/conversation_kernel/tool_execution.py) | 按 view_image 分段并发、Hook/确认屏障、call-order settlement | 核对没有 path-only 前提；文件与引用调用可以混合在同一读图段 |
| [compaction/prompt.py](src/pulsara_agent/conversation_kernel/compaction/prompt.py) | 摘要指导已允许保留继续任务必要的精确 handle | 只补图片引用的简短指导，不添加摘要后的引用收集器 |
| [_repository/fork.py](src/pulsara_agent/conversation_kernel/_repository/fork.py) | fork 改消息 ID，复制正文与图片 refs，复用同 workspace blob | 引用不依赖消息 ID，不增加 fork 引用替换或父会话回退 |
| [tool_artifacts.py](src/pulsara_agent/conversation_kernel/tool_artifacts.py)：`PostgresToolArtifactReadPort` | 已有 session/workspace 绑定的只读端口和 Host 注入方式 | 只借用所有权/注入模式；图片不进入文本 artifact 的存储和分页机制 |

依赖分工：PostgreSQL 负责事务、外键、索引与字节保存；原 blob/codec owner 负责完整性和内容表示；Pulsara 负责本次调用可访问哪个会话、图片预算、模型模态、工具结果归属和投影。新增适配仅连接已知引用和已有图片结果，不复制存储引擎、图片解码器或权限框架。

## 3. 引用身份：复用图片现有内容摘要

### 3.1 固定表示

`image_ref` 的值就是已有图片 descriptor 的 `digest` / `LLMImagePart.content_digest`：`sha256:` 后跟 64 位小写十六进制字符。

它是图片内容引用，不是文件路径、消息 ID、Figure 编号、队列 ID 或新的持久对象。工具将其视为需原样复制的引用；不要求模型理解或计算摘要。

复用这一身份有具体原因：

1. 同一图片已有完整性摘要，无需增加哈希层、短 ID 或映射表。
2. `FrozenRetainedHistoricalRequest` 当前只有 kind/origin/content，没有原消息 ID；不能为生成引用给所有保留内容追加身份字段。
3. fork 会重写 entry ID，图片内容摘要保持不变；摘要文字里出现的引用也无需字符串替换。
4. 用户同图发送两次或工具重复读取时可以使用同一重读引用，既有 occurrence、Figure 和 D2 计量仍各算各的。

允许引用内容摘要，是复用既有不可变内容身份边界。禁止新增 DTO hash、引用指纹、签名 token、注册表、映射缓存或租约。不得把摘要相等当作权限成立。

### 3.2 存活与可访问性

有效引用必须同时满足：格式合法；当前调用 session/workspace 内有已提交的 canonical 图片 owner/ref；descriptor 与 blob 元数据一致；实际 blob 字节可 exact read。

允许的 owner 是已接纳的 USER_MESSAGE、USER_STEER、TOOL_RESULT，以及同 session 的已提交 context snapshot。原图退出当前 base/suffix 后，其仍存在的 canonical owner 继续可读。这是本功能的必要行为，不以当前 provider cut 是否包含图片作为重读条件。

pending queue 不是已接纳的模型历史，不作为本工具的授权 owner；孤立 blob、任意正文 blob、文本 artifact 或其他会话的 ref 也不成立。一个摘要字符串里出现 `image_ref` 本身不建立数据库可达性。

同一图片在当前 session 有多个候选 owner 时，严格按 §6.2 的稳定顺序选定一个，再完整校验；失败不尝试其他 owner。只返回选中的 occurrence，不向调用方枚举候选。最后一个合法 owner 被删除后，已知引用也失效；不为摘要中提到的引用建立额外 GC 根。现有 FK/GC 生命周期不变。

## 4. `view_image` 的模型接口

模型可调用两种形状：

```json
{"path":"/absolute/path/image.png"}
```

或：

```text
{"image_ref":"<从图片旁复制的完整 sha256:… 引用>"}
```

第二个示例展示参数形状，其中尖括号不是实际合法引用；测试和 dogfood 必须使用实际图片的完整 digest。

沿现有 `object_schema` 构建闭合的顶层对象，允许 `path` 和 `image_ref` 两个非空字符串属性，禁止额外属性。两者在 schema 中为可选，由共同参数解析明确要求**恰有一个**。不要为这个小联合扩充通用 schema 转译层，也不要把 provider 对复杂 `oneOf` 的支持当作执行端校验。

以下情况用原 `INVALID_ARGUMENTS`：两者都没给、同时给、显式 null、类型不对、空字符串、ref 格式非法。不得默默优先 path、把无法解析的 ref 当路径、把路径当 URL 或尝试自动补全摘要。

工具描述必须说清：

- path 读取一个本地 PNG/JPEG/静态 WebP；相对路径仍基于 workspace。
- image_ref 重新读取本会话已经保存的图片；只能原样复制已出现的引用，不猜测、不枚举。
- 每次一张，多张用多个调用；不支持查询历史、URL、PDF、动画、resize 或 detail 参数。

工具仍为 `view_image`，read-only、concurrency-safe，保留现有 filesystem family、权限类别和 recovery 分类。本轮不按参数动态更换工具身份或新增许可类别；现有 PreToolUse、policy、确认、attempt 接纳均须走完，数据库归属校验是授权后的内容访问校验。

内部仅需一个闭合的文件来源/图片引用来源值，由原参数解析导出。runner、runtime、compiler、报价使用同一种判断，不能各自用 `arguments.get("path")` 猜测，也不能将 ref 填进 `requested_path` 假装成文件名。不建立通用媒体 source framework。

## 5. 模型如何获得引用

### 5.1 确定的图文投影

在每个实际发送的 Image part 之前，由公共 lowering/compiler 插入一个 Text part，其 canonical JSON 形状为：

```text
{"pulsara_image":{"image_ref":"<该 Image 的完整现有 digest>"}}
```

采用同一 formatter，固定字段与顺序，不附可变时间、随机标识、文件系统路径猜测或重复的图片元数据。这个标签描述紧随其后的一个图片 occurrence。同一张图出现两次，就有两个图片 part 和两个相同引用标签，不能按引用去重内容。

适用位置：

- 普通 USER 输入，包括 direct、已消费 queue、steer。
- snapshot 中实际保留的 active/recent/historical Image 内容。
- 普通及 late 工具图片的派生 carrier。
- Tier 3 的 destination projection：实际保留的用户图片、历史保留内容和 `DestinationToolEvidence.result_content` 中的图片。

保留原 Text/Image 的相对次序，仅在 Image 前增加派生文字；没有 Image 的消息不额外加空标记。不要扫描自然语言来删除看起来相似的用户文字。

`_render_destination_projection` 必须是同一 formatter 的直接 consumer：普通 Text 先完成原有 `display_json` 引用转义，再在每个实际 Image 前插入本节 formatter 生成的原样标签，不把生成的标签再次当作用户文字转义。标签在生成 `projection.content` 及原投影资源报价时就应存在，不能到 `model_call` 构造 summary 消息后才补入。已编译的 source messages 不重新遍历加标签，防止重复标记或改写前缀。纯文本 destination 先按原流程执行 P；已成为省略 Text 的位置不生成图片标签。

标签只在模型投影中出现，不写回 canonical 用户正文，不加入 Hook/Skill 用户文本匹配，不出现在右侧气泡、队列或工具卡。UI 的 `[Figure x]` 继续是每条消息内的显示编号。

### 5.2 工具来源与返回引用

工具 carrier 的原 `tool_image_source` 来源说明继续包含 `tool_call_id`，来源字段改为互斥的 `path` 或 `image_ref`。来源引用与结果图片的重读引用含义不同：前者说明本次从哪里读，后者始终来自实际返回图片的内容摘要。

`view_image(path)` 成功后也产生可重读引用；后来原文件被修改或删除，按返回的 `image_ref` 重读仍取得当时接纳的内容，按原 path 重读则读取文件当时的新内容。两种来源的语义不得混用。

成功结果仍是现有短 Text + 一张 Image 的 `FrozenPromptContent`。不要在 canonical 短文字中再复制引用 JSON，不新增 result image metadata 字段；compiler 从完整图片值生成标签。

### 5.3 不承诺自动发现

引用只随实际进入模型输入的图片出现。不得在 SYSTEM、工具描述、摘要或启动消息中自动塞入“本会话所有图片引用”，也不得维护一个无限增长的引用清单。

用户说“前面那张图”时：模型若仍持有对应引用即可读取；如果没有引用，只能基于当前材料回答或请求用户重发。此边界是本轮明确接受的功能限制，不通过 shell 查询数据库、内部 HTTP 绕路或新历史工具补齐。

## 6. 精确读取与权限

### 6.1 唯一读取入口

在 `prompt_storage.py` 所属 canonical 图片读取 owner 增加一个窄的只读方法：输入真实 invocation 的 session/workspace、已知 image_ref、本次 encoded byte 额度和绝对 deadline；输出一张经 exact read 的 `LLMImagePart`。

通过 Host 向 `DirectKernelToolPort` 注入这一最小读取能力，沿当前 artifact read port 的依赖注入方式；不让工具层依赖 browser protocol handler，不让 filesystem builtin 自行建立数据库连接，不新增 HTTP 路由或独立服务。

### 6.2 解析顺序

1. 校验来源二选一及引用语法；从真实 invocation 取得 session/workspace。工具参数中不允许用户覆盖 session、workspace、scope、owner 或数据库路径。
2. 执行原 Hook、授权和 attempt 流程；按冻结 target fact 判断模态。已知不支持图片的模型在读取 payload 前返回 `MODEL_IMAGE_INPUT_UNSUPPORTED`；unknown 仍沿既有规则允许尝试。
3. 用现有 `_blob_id(workspace_id, digest)` 定位唯一候选 blob，沿现有 `canonical_image_refs` 的 blob 索引检查本 session 的允许 owner。先按 §3.2 的 session/workspace、owner 类型及已提交归属条件筛选，再按下述固定顺序选择一条。查询必须参数化，只取选中行所需的 owner/metadata，不能获取全历史列表或一次载入所有引用。
4. 共用原正文 codec、descriptor/ref 对应校验。可复用或提取现有 owner-local resolver 的核心；不要复制第二套 descriptor 解析、完整性比较或 SQL publication。选定 owner 后校验失败应报错，不能轮流尝试其他 owner 隐藏损坏。
5. 在读图片 payload 前检查 descriptor/MIME/codec/encoded size、D1 所需尺寸及本次额度。若需要读取 owner body 才能得到 descriptor，先按已有 canonical body 物理读界检查其 metadata，继续使用原有界读取。一次操作中的 body 与目标图片读取共同受已有 canonical 物理读预算约束，不分别重置预算；该读取工作集预算与新结果的 C/L/W/T 接纳计量分开。不得为了取一张图片 hydrate 同消息的所有图片或整个 snapshot。
6. 经 `PostgresCanonicalBlobStore.read_exact_in_connection` 等现有 exact read 核心，只取目标 blob 的字节，核验 workspace、digest、size、MIME、codec；结合已校验 descriptor 恢复 `LLMImagePart`。
7. 在同一调用 deadline 内完成读取；数据库连接/读取走既有 I/O lane 和取消/drain 所有权。随后进入公共实际结果报价，全部通过后才返回 SUCCESS。

第 3 步的完整排序键固定为 `(owner.content_size ASC, owner_kind_rank ASC, owner_id COLLATE "C" ASC, ref_ordinal ASC)`：`transcript_entry` 的 rank 为 0，`context_snapshot` 为 1，`owner_id` 分别取现有 entry/snapshot ID。所有字段均来自既有行；同一 owner 的多个相同图片 occurrence 最终由 ordinal 决定顺序。优先正文较小的 owner 是为了减少读取同一图片所需的正文开销，其他字段只负责稳定打破平局，不表示来源可信度或新的权限。

排序与单行选择在数据库内完成，正文校验和 payload 读取在选定后执行。不得先读取全部候选正文寻找“能成功的一条”，也不得因选定行损坏、超过 body+目标图片联合读取预算或其他校验失败而尝试下一条；沿既有完整性/资源失败结算。对于同一数据库快照，选择和结果不能依赖插入顺序、查询计划或默认字符排序。后续新增/删除 owner 可以按同一规则改变选中项，不为历史选择保存映射或收据。

canonical 图片在原接纳阶段已完成 D2 格式/像素校验；重读执行现有完整性校验，不再调用 Pillow 重新解码已验证的 immutable bytes。路径来源仍调用共享 Host validator。两者不能各加一个图片解码槽或后台缓存。

候选 owner 的 metadata/body 校验和 blob exact read 应在同一既有只读事务/快照中完成，避免中途使用脱离 owner 的裸 blob 读取。没有引用时不能仅因 blob 还没被 GC 就允许读取；读取期间已经取得的 frozen bytes 按现有进程内生命周期处理，不加 lease。

### 6.3 ROOT、子代理与会话边界

本扩展采用现有 artifact 读取的 **session + workspace** 范围：已知引用可以读取该 session 内的合法图片，不进一步要求图片仍在调用者的当前 provider cut 中。ROOT 与同 session 子代理都必须持有引用并正常通过工具权限；不因知道 workspace 或 digest 而允许跨 session。

这明确扩展旧图片设计中“父上下文不授予图片读取能力”的部分：父上下文本身仍仅提供既有文本 advisory/缺图说明，不自动附图或生成引用清单；如果父任务显式把已知引用写入子代理目标，子代理可通过同一个工具读取同 session 的图片。这是本工具的窄访问合同，不是 browser attachment 权限的直接继承，也不新增父子授权 token。

相同字节在另一个 session 有 blob/ref 不能构成当前 session 的授权。跨会话 fork 只有在原 fork 事务已为 child 创建合法 refs 后才可读取；不查询父会话作为 fallback。同 workspace 的两个独立会话仍隔离。

### 6.4 失败结果

| 情况 | 结果 |
|---|---|
| 参数缺失、混合来源、非法引用格式 | 原 INVALID_ARGUMENTS |
| 引用无本 session 合法 owner、只在 queue/其他 session、已被删除 | APPLICATION_ERROR，稳定 `IMAGE_REFERENCE_UNAVAILABLE`；不泄漏其他会话是否持有此图 |
| descriptor/ref/blob 不自洽、payload 完整性失败 | 沿现有 canonical/blob 完整性失败分类结算；不得返回图片或 SUCCESS |
| 本次 C/L/W/T 等额度不够 | 现有 `IMAGE_RESOURCE_EXCEEDED` |
| 纯文本目标 | 现有 `MODEL_IMAGE_INPUT_UNSUPPORTED`，payload 读取前失败 |
| 读取超时、取消、Host close | 沿现有 watchdog、取消和 physical drain 分类，不另加重试 |

不为失败自动改走 path、从其他 session 补图、删除图片后重试或返回“图片已省略”的成功结果。

## 7. 资源、并发与工具结果接纳

### 7.1 效果前与效果后计量

D1 的视觉算法、D2 的各资源硬界、G 常数及调用均分规则保持不变。引用让模型可以再次请求图片，不给其免除 token、字节、消息数或 canonical occurrence 成本。

必须更新以下真实落点：

1. `_quote_post_response_resources` 对合法 path/ref 两种 view_image 都分配冻结份额；非法参数仍由原错误路径结算，不使整个批次异常。
2. `ImageToolResourceQuoteOwner.quote` 的输入改为同一闭合来源值，调用与 compiler 相同的 carrier builder。消除其 `requested_path` 假设。
3. 效果前 R_base 包含实际工具参数和来源说明、错误/closure/late 原预留；新图片引用标签也必须纳入可推导的增量。尚未知 path 读出内容时，只能按固定引用格式与现有文字计量规则推导标签开销，不得随手加“安全余量”常数。
4. 取得完整图片后，用实际来源、实际 digest 标签、图片和完整 suffix 进行已有 C/L/W/T 报价；超过本次份额则失败。不能只数 data URL 或仅扣 blob 字节。
5. USER、snapshot 和 Tier 3 destination projection 的新标签进入各自原有的投影选择/报价及最终 logical/final-wire/token 计量。不能只在普通 compiler 路径计费，或在 destination 候选已通过预算后才添加标签。canonical C 仍按保存的正文/descriptor + 图片 E 计费，不把派生标签写回正文或反过来漏算 wire 成本。

同图重读会产生一个新的 TOOL_RESULT occurrence；数据库可以复用 blob，但 canonical 展开与实际 wire 中的每次图片出现都照常计量。不得因 ref 相同去重模型内容。

### 7.2 并发与生命周期

path/ref 混合的连续 view_image 段复用同一个 executor 有限窗口；真实匹配 Hook、确认、非图片工具的屏障不变。仍按原 call ordinal settle、展示和合并 carrier，不按物理完成顺序。

每次引用读取同样共享该调用原来的绝对 deadline；调度前等待不提前消耗 invocation deadline。保留既有 I/O 硬并发与 Host decoder 单槽；数据库重读无需占用 decoder 槽，不因此启动另一套不限量任务。

### 7.3 同一 canonical 接纳路径

引用读取成功后，继续走既有 typed ToolResult → 原 repository writer → 正文/blob/ref/tool row/原 event 同事务 → exact-confirm。

可以复用原 publication/exact-reuse，不要求为这一次重读增加来源关系、读取收据、reference-read event 或保存命中记录。原 tool arguments 已记录本次请求的引用，新工具结果正文和 refs 拥有本次实际返回的图片。

如果原 owner 后来删除，但新工具结果已经成功接纳，新结果自己的 ref 继续保证图片可达。若结果尚未接纳，按原事务失败/取消语义处理，不新增执行恢复。

## 8. 压缩、模型切换、cold 与 fork

### 8.1 普通压缩

- summary 调用仍在合法已安装前缀后追加请求；已经发送的图和引用文字原样保留，不重建 prefix。
- 在原摘要指导中增加一句：仅当继续任务确实需要再次查看某张图片时，原样保留其已经出现的 `image_ref` 及简短用途；不得计算、猜测或列举全部图片引用。
- 已保留的 typed 图片在新 snapshot lowering 时确定性生成标签；不在 carrier schema 中增加 `image_refs` 索引或第二份身份。
- 已退出 successor 的图片，如果 summary 确实保留其引用，就可以通过新工具调用重读。若引用也被摘要省略，本轮允许模型失去定位能力。
- 不扫描完整历史补齐引用，不强制每轮摘要保留全部引用，不检查摘要“引用清单完整率”，不为此重新调用摘要模型。

### 8.2 模型交接与 destination projection

Tier 3 选择支持图片的 destination 模型时，`_render_destination_projection` 对所选用户内容与工具证据中的实际 Image 按 §5.1 加引用标签，再形成原 summary 输入；unknown 模态仍遵守原判定，不擅自执行 P。普通 lowering、工具 carrier 和 destination renderer 共用 formatter，不能让交接路径成为无标签图片的例外。

沿冻结 Tier 1/2/3 和 Image → Text("[Image omitted]") 行为处理实际内容。工具读取能力不能绕过目标模态：切到已知纯文本模型时，view_image 的两种来源都拒绝返回图片。

summary 自然语言中仍然存在的引用文本不必强行删除，它只是文本；也不因此自动取图。目的投影 P 移除 Image 后，不单独为它生成新的图片标签。后来切回支持图片的模型，若引用仍已知且 owner 仍存在，可显式重读。

### 8.3 cold、fork 与重复压缩

- 关闭/恢复会话：从 canonical refs 和 blob 读取，不依赖进程内映射或原始上传文件。
- fork：图片内容引用不随 entry ID 更换而改变；只认 child 自己复制的 refs。父关闭/删除不能使 child 仍有引用的图片失效。
- fork cut 未包含对应 owner 时，即使 summary/其他文字偶然提到引用，child 也不能借父 session 获取图片。
- 重复压缩：无需改引用值，不新增引用历史。摘要是否继续保留它仍是原语义压缩的一部分。

## 9. 前缀连续性与 hard cut

本扩展改变 view_image schema/描述、模型可见图片投影和摘要指导，必须在同一 hard cut 更新以下三个现有版本 owner 及对应 golden/replay 测试：

| 必须更新的 owner | 变更原因与影响 |
|---|---|
| [model_input/continuity.py](src/pulsara_agent/model_input/continuity.py)：`PROVIDER_MESSAGE_LOWERING_CONTRACT` | USER/snapshot/tool/destination 的 provider bytes 改变；该版本参与 full-history 与 snapshot base identity，必须让原 identity/compatibility 派生链按新版本重新计算，不能仅改测试 golden |
| [model_input/compiler.py](src/pulsara_agent/model_input/compiler.py)：`COMPILER_CONTRACT_VERSION` | 编译出的图片标签和工具来源投影改变，原编译兼容性比较须识别本次变化 |
| [compaction/contracts.py](src/pulsara_agent/conversation_kernel/compaction/contracts.py)：`COMPACTION_SUMMARY_PROMPT_CONTRACT` | 摘要请求新增保留必要图片引用的指导，须更新其原 prompt 身份 |

`COMPACTION_SNAPSHOT_COMPILER_CONTRACT` 和 canonical `pulsara.prompt/v1` **保持不变**：本扩展没有改变 snapshot carrier/body 或图片正文 codec，显示层改变由上述 lowering/compiler 版本表达。不得新增旁路版本登记、兼容字段或冗余 fingerprint；工具 schema/描述变化继续由既有冻结工具表的身份机制覆盖。

只有新的 cold epoch 和明确采用的 compaction successor 可以安装新工具 schema/新投影。现存 epoch 的 SYSTEM/tools 和已安装 messages 必须保持原字节；不能回填旧图标签、热替换 tools，或因为发现新引用就重建输入根。

新编译规则不应作为第二套可切换“旧/新图片模式”长期存在。按仓库开发期 hard-cut 规则启动新实现；不兼容的旧已安装 epoch 不能通过新 compiler 悄悄解释，应沿原不兼容边界转入合法 cold/明确 successor。不得为兼容一次开发升级建立在线迁移、双读双写或旧引用 fallback。

新 epoch 内重复调用/重试必须保持 byte-identical；新增结果只作为 suffix。provider-native replay 仍只替换原允许的 assistant 单来源，不能绕过工具附件组合来源证明。

## 10. 前端与持久化增量

用户看见的图片、Figure、队列和 Lightbox 无需增加任何引用 UI。工具结果仍左侧卡片，收起摘要、展开大图。不得把派生标签展示成用户文字，也不把重读 carrier 展示成新用户消息。

持久化增量固定为：schema/table/product relation/durable event/live event/job/registry 全部 **+0**。继续使用现有 refs 和 blob。默认不需要修改 clean-v0 SQL；若实现发现必须新增持久身份或关系，应先修订本规格，不能自行补表。

不新增上传图片的工作区目录、永久文件副本、独立缓存清理服务。用户仍不能通过本地 path 直接读取一张只存在数据库的聊天图片，必须使用明确的 image_ref 来源。

## 11. 建议实施顺序

| 阶段 | 修改范围 | 完成条件 |
|---|---|---|
| R1 引用与读取合同 | 原 schema/来源解析；prompt_storage 已知 ref exact read；最小 Host 注入 | 真实 PostgreSQL 归属、exact bytes、隔离与额度检查成立 |
| R2 单一图片工具链路 | runtime 两来源汇合；runner/quote/compiler 的 path-only 假设清除 | path/ref 混合批次、同一 carrier、ordinary/late、接纳与确认通过 |
| R3 模型引用投影 | USER/snapshot/tool/Tier 3 destination 共用标签；摘要指导；三个现有版本 owner hard cut | canonical/UI 原样，provider 标签完整计费、前缀连续、fork 引用稳定 |
| R4 完整验收 | 定向回归、两协议真实 provider、浏览器回归、文档状态 | 已知引用重读端到端成立，未增加历史发现能力 |

上述阶段是工作组织，不是对外激活四条半成品路径。完整链路通过前不能宣布支持聊天图片重读。非必要不新增文件；一个最小 typed source/只读 port 若需新增，必须仅服务上述具体边界，不拓展成可插拔附件框架。

## 12. 验收矩阵

| 编号 | 必须证明的产品行为 |
|---|---|
| R01 | path 单独合法；ref 单独合法；同时/缺失/null/错误格式拒绝，无隐式来源 fallback |
| R02 | direct、queue 消费、steer 的图片按顺序生成引用标签；无图消息不变化；canonical 原文与 Hook/Skill 文本不夹带标签 |
| R03 | snapshot active/recent/historical、ordinary/late 工具图片、Tier 3 destination 的用户图/工具证据共用 formatter；多模态 destination 的实际 summary 输入中每张图均有且仅有一个生成标签，标签不被二次引用转义且进入 L/W/T 报价；纯文本 destination 先 P，无图位置不生成标签 |
| R04 | 同字节重复 occurrence 引用相同，Figure/顺序/资源费用仍逐次计；多 owner 按 §6.2 全排序键选定同一 occurrence，覆盖不同 body 大小、entry/snapshot 平局、ID 与 ordinal 平局处理，不依赖插入/返回顺序；不存在新图片 ID 或映射表 |
| R05 | 已知引用在图片退出当前 provider context 后仍可精确读到数据库字节；只查目标引用，不加载全部历史 |
| R06 | 跨 session、错 workspace、仅 orphan blob、仅 pending queue、非图片正文/artifact 不能被读；未授权与不存在不泄漏外部归属 |
| R07 | ROOT/同 session 子代理显式传递已知引用后可读；父上下文不自动附图、不输出全量引用；正常权限/Hook 屏障仍执行 |
| R08 | descriptor/ref/blob 不一致、字节损坏不得 SUCCESS；多 owner 中选中行损坏时明确失败，即使另一候选完整也不 fallback；不同 body 大小造成联合读取预算结果不同时仍按固定顺序选择，选中行超界后不读 payload、不改选；owner metadata 与目标 payload 分阶段有界读取，不读取同 owner 的其他图片 payload |
| R09 | path 读取后删除/修改源文件，image_ref 仍返回已接纳原图；path 再读按文件当时内容处理 |
| R10 | 数据库来源复用 canonical exact read，不新增 Pillow 解码；文件来源仍经过原共享 validator |
| R11 | path/ref 混合连续调用可并发，确认/匹配 Hook/非图工具仍为屏障；逆序完成仍按 ordinal 合并，成功/失败混合仅合并成功图片 |
| R12 | 两种来源都进入原效果前 R 与同一冻结份额；C/L/W/T 边界两侧正确，长路径/特殊字符/ref 标签不漏计；同图不免单 |
| R13 | 成功结果与 blob/refs/tool row 原事务一致；失败回滚、exact-confirm、新结果对 blob 的独立可达性正确，无新事件类别 |
| R14 | 已知非图像目标在 payload I/O 前拒绝；unknown 不预先判不支持；Tier 3 不靠引用隐式恢复已省略图片 |
| R15 | 标签不回填旧 prefix，tools 在现有 epoch 不变；lowering/compiler/summary prompt 三个版本按 §9 更新并进入原身份与兼容性链，canonical prompt/snapshot body 契约不变；retry 字节相同，late 只加 suffix，实际 HTTP 等于 frozen materialization |
| R16 | cold/resume 可重读；fork 更换 entry ID 后引用仍可用，父删除不损坏 child；fork cut 外图片无父 session fallback |
| R17 | ordinary compact 中保留的图正常带引用；仅在 summary 保留 ref 时保证可重读已退出图；未保留 ref 不触发搜索、自动清单或引用修复 |
| R18 | UI 历史/刷新、队列、Figure 和工具大图不变；不显示引用 JSON，不产生伪用户气泡 |

复用并扩展现有相邻测试：`tests/test_local_image_tool_executor.py`、`tests/test_kernel_image_input_k2_postgres.py`、`tests/test_kernel_image_input_k3.py`、`tests/test_stage2_direct_model.py`、`tests/test_round3_structured_model_input_compiler.py`、`tests/test_round5b_long_horizon_context_compaction.py`，以及现有 frontend adapter/component tests。只为新增可观察合同补测试，不复制图片库、SQL 驱动或通用 parser 的完整测试套件。

### 12.1 真实 provider 验收

沿现有 `tools/run_kernel_image_input_dogfood.py` 扩展最小场景，使用已保存配置与 verified disposable PostgreSQL，不修改生产设置，不输出密钥：

1. 用正常用户图文入口发送一张自生成的可识别测试图；核对实际 wire 中的 Image 与完整引用。
2. 让模型用该 image_ref 调用 view_image；断言实际工具参数、返回字节、canonical TOOL_RESULT 与同批单一 carrier，而不只看模型声称“我看到了”。
3. 通过真实普通 compact 让目标图片退出有效 successor，然后在新用户文字中显式提供先前观察到的同一引用，请求重读。验收器先断言目标图确已退出，再验证工具重读及视觉回答；提供引用是测试已知引用功能，不是自动发现。
4. 增加一个 path/ref 混合批次，核对同批合并和来源；重启恢复、fork 权限与事务边界以定向 PostgreSQL 测试为主。
5. Chat 与 Responses 均通过正式 adapter 运行。Responses 优先使用用户已保存的官方 DeepSeek 配置；第三方中转失败单独记录，不能靠 provider 名称分支绕过。

模型未调用工具、答错图、provider 拒绝以及基础设施故障必须分开记录。不得为了强迫自然语言摘要永久记住引用而改变产品语义、无限重复实验或篡改验收结果。

前端复用普通聊天与工具大图的浏览器验收，确认新增模型标签未进入 UI。最终只做适度相邻回归与项目必要静态检查，不以新增“全量历史扫描测试”偷偷扩大功能。

## 13. 完成标准与文档同步

完成时必须同时成立：图片拥有可复制的已知引用；模型通过同一 view_image 可以重读本 session 的已保存图片；两种 API 走同一内容与接纳路径；引用/标签全部计费；prefix、权限、GC、取消与模型切换合同保持；没有历史图片发现能力或附带的长期存储机制。

实现后更新本稿状态、原图片设计实施状态与本地图片工具规格中被本稿扩展的 path-only 表述；仅修改明确变更的范围，D1/D2 公式与参数不动。报告区分真实 provider、PostgreSQL、确定性测试和仅代码检查的证据，未实跑项明确列出。写完规格、单个 helper 可运行或旧验收通过，都不等于本功能完成。

## 14. 实施与验收记录（2026-09-16）

追加浏览器验收：`output/browser-image-e2e-20260916-prompts/report.zh.md` 记录了当前提示词下的 DeepSeek Responses / Chat 三格式本地读取、原路径移走后的引用重读、已采用压缩后从零图片上下文重读、刷新/大图/放大展示与跨会话拒绝。此次发现 `KernelSessionIO.run_tool_invocation` 的按时异常在等待阶段提前抛出，绕过已有结果分类，使引用不可用被泛化为系统错误；现已让异常进入原有精确结果分类，由原工具 owner 返回 `IMAGE_REFERENCE_UNAVAILABLE` 或 `IMAGE_RESOURCE_EXCEEDED`。未增加协议或存储路径。两项工具错误回归及一项物理异常回归补齐，50 项相邻测试通过；修复后新进程的两种 API 正向链路与跨会话负向链路均重新通过。具体浏览器范围与未覆盖项以该报告为准。

R1–R4 已沿单一路径完成。`view_image` 的公共参数解析现在产生 path/ref 闭合来源；引用读取由 session/workspace 绑定的 canonical 只读端口在同一只读事务中选定 owner、校验正文/descriptor/ref 并 exact read 原 blob。runtime 继续复用原 Hook、权限、attempt、模态、逐调用额度、deadline、physical drain、typed `TOOL_RESULT` 接纳与 settlement。普通 USER、snapshot、工具 carrier 和 Tier 3 destination projection 共用图片引用 formatter；本稿 §9 指定的三个契约版本已经 hard cut，canonical `pulsara.prompt/v1` 与 snapshot body 契约未改变。clean-v0 schema、durable event/relation/job/registry 数量均未增加。

确定性与 PostgreSQL 证据：本轮主相邻 Python 测试 319 项通过，覆盖 path/ref 参数、同段并发与屏障、known/unknown/纯文本模态、ordinary/late carrier、引用标签与正式报价、queue/orphan/跨 session 拒绝、稳定 owner 选择后损坏不 fallback、fork/cut/父 snapshot 删除、ordinary/Tier 3 compaction 以及 Chat/Responses 最终 materialization；规格点名的输入、生命周期、前缀连续性、公开工具投影和 artifact 相邻回归另有 122 项通过；架构与 fingerprint subtraction 检查 23 项通过。frontend 的相邻 adapter/component 回归 84 项通过，继续证明 canonical 标签不进入页面；新的真实浏览器证据见下文。

真实 provider 使用 `LocalSettingsStore` 与 `require_pulsara_home()` 只读取得保存配置，并使用一次性本地 PostgreSQL。Chat 记录位于 `output/image-reference-reread/chat-20260916-r2/report.json`，Responses 使用保存的官方 DeepSeek 配置，记录位于 `output/image-reference-reread/responses-deepseek-20260916-r1/report.json`。两次均经正式 Host/compiler/adapter/HTTP 路径各完成 18 次真实调用，报告中的 10 项检查全部为真；覆盖 provider 标签、path 后按引用重读、path/ref 混合批次、删除原文件后重放、cold/fork、真实普通 compact 后图片退出 successor 再按显式引用重读，以及 actual HTTP 与 frozen materialization 一致。首次 Chat 运行只因模型把可见数字返回为 JSON number 而非 string 未通过 harness 的表示断言，修正 harness 比较后复验通过；没有为 provider 增加分支或重试语义。

新的浏览器回归位于 `output/playwright/image-reference-reread-20260916/report.json`。真实 Chat 会话先调用 path 来源，再只用模型刚获得的 `image_ref` 调用同一工具，两个 canonical `TOOL_RESULT` 的 digest 相同，模型正确回答测试图数字。刷新后第二个工具卡仍可展开原大图；页面没有显示派生 `pulsara_image` 标签、digest、Figure 或成功正文。报告 8 项检查全部为真，配套 AX snapshot 与截图保存在同目录；一次性数据库在服务退出后删除。

真实 provider 没有逐项实跑所有故障注入：跨 session/orphan/pending 拒绝、选定 owner 损坏或联合读取超界不 fallback、取消/Host close、纯文本目标、Tier 3 模型交接和 fork cut 由定向自动化测试证明。该证据边界不改变产品限制：没有引用时仍无历史图片查询、搜索、分页、附件目录或自动发现能力。

## 附录：开源调研依据与本轮取舍

本次只读调研使用本地 Codex 与 OpenCode 仓库，不把其产品行为直接当作 Pulsara 的协议或资源契约。

- Codex 在 [local_image_content_items](../codex/codex-rs/protocol/src/models.rs) 中把本地路径写在图片旁的文本标签里；`view_image` 根据已知路径读图。这说明模型需要一个明确、可复制的重读入口，但操作系统临时路径的寿命不能替代 Pulsara 的 canonical 存储。
- Codex 的 [clipboard_paste.rs](../codex/codex-rs/tui/src/clipboard_paste.rs) 为 TUI 粘贴创建 PNG 临时文件；rollout 保存的是处理后的 response 内容。Desktop 私有附件服务不在本地开源仓库的可证明范围内。
- OpenCode 的 [submit.ts](../opencode/packages/app/src/components/prompt-input/submit.ts)、[build-request-parts.ts](../opencode/packages/app/src/components/prompt-input/build-request-parts.ts) 把上传图片转成 data URL，随后保存于消息数据；[session/sql.ts](../opencode/packages/core/src/session/sql.ts) 定义 SQLite 的消息/part 存储。Pulsara 继续复用自己的 blob/ref，不照搬 data URL 内嵌存储。
- 两仓库的现有图片文件工具均不是历史附件查询工具。图片仍在有效历史时的 replay、客户端回看和模型主动查找旧图是不同能力；本轮明确不补第三项。

因此本稿采用“模型可见的已知引用 + 已有 canonical 保存 + 现有图片工具重读”，不采用图片目录、历史搜索或跨压缩自动恢复。内容摘要只解决稳定定位，不承诺模型永远记得引用。
