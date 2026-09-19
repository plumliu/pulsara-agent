# Pulsara HTML 可视化订阅与渲染回看实施规格

状态：**设计定稿，尚未实施**。日期：2026-09-17。

## 0. 执行结论

Pulsara 新增一个模型可调用的 `visualization_render` 工具，但该工具不拥有 HTML 的创作、编辑或版本管理。模型继续复用现有 `write` / `edit` 文件工具创建和修改 HTML；`visualization_render` 只承担两件事：

1. 把一个本地 HTML 路径或已知 `visualization_ref` 登记为“在本轮 terminal assistant message 完成时展示”的进程内订阅；
2. 当调用显式携带 `review=true` 时，在保留订阅的同时，对该来源当前表示的 HTML 做一次即时渲染回看，并把截图沿现有工具图片链路交给模型。

普通订阅调用不读取 HTML、不渲染、不写 PostgreSQL，只返回简短的“已订阅”工具结果。路径来源在订阅后仍可由模型继续任意次数地编辑，无需重新订阅；引用来源始终指向已经冻结的 immutable HTML。

当本轮 terminal assistant message 已由模型生成、但尚未完成 canonical publication 时，runtime 才读取每个订阅路径的最终内容，完成安全校验和渲染准备，冻结确切 HTML 字节，并仅将这个最终版本写入现有 PostgreSQL blob 存储。最终消息引用该 immutable blob；以后修改或删除工作区文件都不得改变历史消息。

最终 HTML 在 publication 时才获得 `visualization_ref`，因此原 `visualization_render(path=...)` 工具结果不可能提前返回该引用。当前 terminal message 的自动展示不依赖模型知道引用；当后续用户输入触发下一次 provider call 时，prompt compiler 才在新进入 provider 历史的上一条 terminal assistant message 之后派生一个不可见于聊天 UI 的引用 carrier，让模型在后续轮次知道并复用该最终可视化。

路径订阅同时采用“删除即取消”：如果用户在 active turn 中 steer 表示不再需要该可视化，模型可以通过现有文件工具删除对应 HTML。terminal publication 最终读取时若路径已经不存在，runtime 静默把该项视为取消订阅，不生成错误、不保存 blob、不显示失败占位。

本规格不保存 HTML 编辑过程中的版本，不增加 visualization 专用编辑协议、版本图、草稿表、恢复任务、订阅收据或跨重启 replay 机制。

## 1. 产品目标与范围

### 1.1 用户可见目标

- 模型能用普通文件工具制作一份可交互 HTML 可视化。
- 模型调用一次 `visualization_render` 后，可以继续修改该文件；本轮最终展示自动采用 terminal message 落定时的最终文件内容。
- 工具调用本身仍是中间过程，只显示简短状态；可视化作为 terminal assistant message 的展示内容出现。
- 模型需要检查效果时，可用同一个工具请求一次截图回看；回看不会取消或替代最终展示订阅。
- 回看截图获得现有 `image_ref`，以后只要模型仍知道该引用，就可以通过 `view_image(image_ref=...)` 再次查看。
- 最终 HTML 获得独立的 `visualization_ref`；它在下一次 provider call 中由 compiler 告知模型，可由 `visualization_render(visualization_ref=...)` 重新订阅或回看已经冻结的可视化。
- 最终 HTML 不受普通对话正文的居中版心/max-width 约束，而是在 transcript 中使用中央工作区的完整可用横向空间；普通 assistant 文本仍保持原对话排版。
- 历史消息不依赖可变的本地文件；最终展示内容一经提交即不可变。

### 1.2 权威顺序

用户本轮明确约定 → [AGENTS.md](AGENTS.md) → 本规格 → [Pulsara 已知图片引用重读实施规范](archived_docs/PULSARA_IMAGE_REFERENCE_REREAD_IMPLEMENTATION_SPEC.zh.md) 与其他仍适用的 canonical/blob/tool execution 规格。

图片回看的引用、归属、工具图片 carrier、provider lowering 和 PostgreSQL 图片 blob 行为全部复用现有图片规范。本规格只定义 HTML 可视化订阅、即时回看和最终消息展示的新增边界。

## 2. 所有权边界

| 能力 | 唯一 owner | 本功能的行为 |
|---|---|---|
| HTML 创建与修改 | 现有 `write` / `edit` 文件工具 | 创建文件、增量修改、提供正常 diff 与文件权限语义 |
| 当前轮订阅 | conversation runtime 的 active-turn owner | 进程内登记闭合来源值、顺序和调用来源；不持久化草稿状态 |
| 即时回看 | `visualization_render(review=true)` 与现有图片结果链路 | 读取当前路径或已冻结引用、沙箱渲染截图、生成普通工具图片结果 |
| 最终 HTML 冻结 | terminal assistant message 的 canonical publication owner | 在消息提交前读取最终文件，只冻结并保存最终版本 |
| HTML 字节持久化 | 现有 PostgreSQL canonical blob owner | 保存最终 immutable bytes 与内容完整性信息 |
| 最终消息展示 | terminal assistant message 的 visualization occurrence / projection | 引用最终 HTML blob，在聊天中渲染；不借用 tool result artifact 字段 |
| 最终可视化重用 | `visualization_ref` 解析与 `visualization_render` | 只允许重用当前 session/workspace 已有 canonical occurrence 所拥有的 immutable HTML |
| 截图重读 | 现有 `view_image(image_ref=...)` | 按当前图片引用规范重读 review 截图 |

`visualization_render` 不得自行写文件、修改文件、生成 HTML、应用 patch、维护源码版本、回写格式化结果或替代 `write` / `edit`。PostgreSQL 中的最终 HTML blob 也不是后续编辑入口。

## 3. 工作区文件约定

### 3.1 标准目录

模型生成的可视化 HTML 使用以下标准目录：

```text
<cwd>/.pulsara/visualizations/
```

首版以一个普通、可由用户直接用浏览器打开的 HTML 文件作为入口：

```text
.pulsara/visualizations/<descriptive-name>.html
```

为了便携和最终冻结，CSS、JavaScript 和可视化数据可以尽量内嵌，但这只是推荐而不是资源 allowlist。HTML 可以像普通网页一样引用相对或绝对的本地配套文件、模型通过现有终端与权限体系安装或构建的包产物，以及在现有网络权限允许时引用外部资源。文件名应稳定、可读并能表达内容；不得把随机临时名或 PostgreSQL blob ID 当作用户工作文件名。

该文件不是 Pulsara 私有片段格式。宿主专用 metadata 必须保持可选且能被普通浏览器安全忽略；可视化若使用宿主能力，应提供普通浏览器可用的资源引用或降级路径。用户手动打开工作文件是正式支持的创作与调试路径，而不是需要阻止的旁路。

`visualizations` 表示可继续编辑的可视化源文件，不使用容易与一次性输出混淆的 `renders`，也不使用会与现有 tool artifact 概念混淆的 `artifacts`。

### 3.2 目录不是新的权限边界

该目录是模型生成内容的标准落点，而不是第二套文件系统沙箱。`path` 沿用 Pulsara 现有本地路径解析、权限、Hook 与确认语义；绝对路径和含 `..` 的相对路径不得仅因离开 workspace 而被本工具额外拒绝。

当模型从头制作可视化时，工具描述和模型指令应要求优先写入标准目录。用户明确指定已有 HTML 文件时，可以直接订阅该本地路径，不必复制一份到标准目录。

### 3.3 文件与最终消息的关系

工作区文件是创作载体，不是历史展示的 durable authority。terminal message 提交前，文件可以继续变化；提交后，历史展示只认已经冻结的 blob，不再读取原路径。

在 terminal message 最终物化前删除路径来源文件，表示取消该路径的当前轮订阅。runtime 自身不替模型删除文件；删除仍由现有文件工具、权限、Hook 与确认语义拥有。文件已经冻结并随 terminal message 成功提交后，再删除原文件只清理创作载体，不会撤销或改变历史展示。

## 4. 模型工具接口

### 4.1 固定 schema

新建或继续编辑本地可视化时使用路径来源：

```json
{
  "path": ".pulsara/visualizations/example.html",
  "review": false
}
```

后续轮次重新订阅已经冻结的最终可视化时使用引用来源：

```json
{
  "visualization_ref": "sha256:...",
  "review": false
}
```

字段：

- `path`：可选、非空本地文件路径；相对路径基于本次调用冻结的 `cwd`。
- `visualization_ref`：可选、格式固定的已知最终可视化内容引用，只能原样复制 compiler 已提供的引用。
- `review`：可选布尔值，默认 `false`。`true` 表示在订阅之外立即生成一次当前版本的模型回看截图。

`path` 与 `visualization_ref` 必须恰有一个。顶层禁止额外字段。首版不提供 `html`、`visualization_id`、`title`、`mode`、`patch`、`replace`、`revision` 或内部数据库 blob ID 参数。

`visualization_ref` 使用最终 HTML 已有内容摘要的稳定表示，而不是暴露数据库 row/blob ID、消息 ID 或可枚举的 visualization registry。引用成立仍须验证当前 session/workspace 内存在拥有该内容的 canonical visualization occurrence；知道摘要字符串本身不建立权限。

### 4.2 工具描述必须说明

- 先用现有文件工具创建或编辑 HTML，再传入路径；需要重用既有最终可视化时，原样传入已知 `visualization_ref`。
- 路径来源会把该路径订阅到本轮最终回复，之后仍可继续编辑；引用来源订阅的是已经冻结的 immutable HTML，不能借此编辑源码。
- 对路径来源，在本轮 terminal publication 前删除对应 HTML 会静默取消最终展示；无需调用额外的 unsubscribe 工具。
- 普通调用不检查视觉效果，也不代表当前文件已经形成最终版本。
- `review=true` 会附加一次当前版本截图；路径来源的最终展示仍读取本轮结束时的最新文件，引用来源的最终展示继续使用同一 immutable HTML。
- 同一规范化路径或同一引用在同一轮重复调用不会产生重复的最终可视化。
- 工具不是浏览器导航器、HTML 编辑器、文件写入器或永久发布服务。

### 4.3 普通工具结果

工具结果只用短文本陈述本次调用观察到的订阅状态。首次登记与已经登记必须使用不同但同样中立的文案：

```text
Visualization is now subscribed for this response.
```

```text
Visualization is already subscribed for this response.
```

不得把后者描述为 duplicate call、redundant、unnecessary、ignored、no-op、warning 或模型错误。再次调用可能是模型忘记了先前状态，也可能只是为了在修改后通过 `review=true` 重新查看当前效果；工具结果不能猜测调用动机。

附加回看成功时，在对应订阅状态之后增加同一个中立后缀：

```text
Current preview attached.
```

因此，首次订阅并回看与已经订阅后再次回看分别形成：

```text
Visualization is now subscribed for this response. Current preview attached.
```

```text
Visualization is already subscribed for this response. Current preview attached.
```

结果不得包含 HTML 正文、数据库 blob ID、内部订阅对象、路径内容摘要、虚构的完成预览或“已经持久化”等不真实陈述。

`visualization_render` 的 canonical tool result 不是最终 HTML 的载体。最终 HTML 也不得写入 `tool_results.output_artifact_blob_id`，因为它不是工具执行输出 artifact，而是 terminal assistant message 的展示内容。

## 5. 当前轮订阅语义

### 5.1 订阅登记

普通调用只执行以下工作：

1. 解析恰有一个来源：按冻结 `cwd` 规范化 `path`，或校验 `visualization_ref` 的固定语法；
2. 执行既有工具 schema、权限、Hook、attempt 和 settlement 流程；
3. 在当前 active turn 的进程内 visualization subscription owner 中登记闭合来源值；
4. 返回简短成功状态。

普通调用不打开文件、不读取文件字节、不解析 HTML、不读取 visualization blob、不启动浏览器、不生成截图、不建立 blob，也不向数据库写订阅行。引用的 canonical owner 与权限在 review 或 terminal publication 实际消费时验证；“已订阅”不声称引用已经成功物化。

来源登记成功只表示 runtime 接受了“本轮结束时尝试展示此来源”的请求，不表示路径当下存在、引用当下可解析、HTML 当下有效或最终结算一定成功。模型需要即时验证时应显式使用 `review=true`。

### 5.2 去重与顺序

- 同一 active turn 内，以闭合来源值作为进程内订阅槽位键：路径来源使用规范化本地路径，引用来源使用完整 `visualization_ref`。
- 第一次成功订阅决定该可视化在最终消息中的相对顺序。
- 同一闭合来源再次订阅保持原顺序且不增加第二项。
- 不同来源按首次成功订阅的调用顺序展示。
- 一个调用只订阅一个来源；多个可视化使用多个调用。
- 不向模型暴露 `visualization_id`。canonical identity 由最终消息 occurrence 和 blob owner 建立，不由路径充当跨消息身份。

订阅 owner 必须提供原子的 insert-if-absent 语义，并把“本次新登记”或“此前已登记”作为 process-local 调用结果返回给工具 renderer。即使两个相同来源的调用并发到达，也只能有一个调用观察到首次登记；其他调用观察到已经订阅。首次成功登记决定顺序，后续调用不得移动、替换或复制该槽位。

幂等键只使用 §5.2 的闭合来源值。两个不同写法规范化到同一路径时视为同一订阅；相同 `visualization_ref` 视为同一订阅。路径来源与引用来源不为了判断 HTML 内容是否相同而提前读取或跨来源去重，即使最终字节碰巧一致，也仍是两个明确来源的订阅。

幂等性只约束订阅状态。`review=true` 是本次调用请求的即时观察，不是订阅槽位属性，也不得因来源已经订阅而跳过。每次显式 review 都重新读取该调用时刻的路径内容或已冻结引用、重新渲染并产生本次截图结果。

### 5.3 订阅生命周期

订阅只属于当前 active turn：

- terminal assistant message 成功提交后清空；
- turn 被取消、失败或未形成 terminal message 时丢弃；
- 进程在 terminal publication 前崩溃时允许丢失；
- reconnect、snapshot 或历史读取不得尝试恢复未完成订阅；
- 子代理与 ROOT 各自拥有自己的 active-turn 订阅，不隐式继承或合并。

这是一项本轮输出提示，不是需要 durable recovery 的执行事实。不得为它新增 receipt、checkpoint、event、job、lease、generation、replay reducer 或 repair path。

### 5.4 删除即取消

路径订阅以 terminal publication 实际读取时的文件存在性作为最终取消判断：

- 如果路径不存在，移除该进程内订阅槽位并继续结算其他内容；
- 不返回新的工具结果，不生成 visualization occurrence、失败 placeholder、HTML blob、durable cancellation row 或事件；
- 多个路径中只取消缺失的项，其余项保持原相对顺序；成功 occurrence ordinal 按剩余项连续生成；
- 订阅后删除、随后又在最终读取前重新创建同一路径时，按最终存在的最新文件正常物化；runtime 不保存或解释中间存在性历史；
- 已经产生的 review 工具结果和截图 occurrence 不因后来删除源 HTML 而回滚；删除只取消 terminal message 的最终 HTML 展示；
- `visualization_ref` 来源没有可删除的本地路径，不适用本节规则。引用不可解析、无权访问或 blob 损坏仍按真实引用失败处理。

“路径不存在”只包含既有 filesystem owner 可靠识别的 not-found 结果。权限拒绝、读取 I/O 失败、路径指向目录/非普通文件、内容非法、安全校验失败或渲染失败不得被伪装成取消订阅。

## 6. `review=true`：订阅之外的即时回看

### 6.1 行为顺序

`review=true` 不建立另一种工具模式。runtime 先按 §5 接受订阅，再执行附加回看：

1. 原子登记来源并取得“本次新登记”或“此前已登记”的状态；无论是哪一种状态都继续本次回看；
2. 路径来源读取该文件在本次回看时刻的当前字节；引用来源按当前 session/workspace 的 canonical occurrence 解析并 exact read 已冻结的 HTML blob；
3. 完成 HTML 安全校验；
4. 在隔离渲染环境中加载并截图；
5. 让工具结果按 §4.3 如实报告订阅状态，并沿现有工具图片结果机制附带截图；
6. compiler 按现有图片规范派生 user-role 图片 carrier，并给截图提供普通 `image_ref`。

回看读取的 HTML 不写新的 PostgreSQL HTML blob，也不成为路径来源的最终展示版本。路径来源可以在模型看到截图后继续使用 `edit` 修改原文件；最终消息仍按 §7 重新读取当时的最新内容。引用来源保持原 immutable HTML，只额外产生截图结果。

### 6.2 截图引用复用

回看截图是普通 canonical 工具图片 occurrence，不新增 `visualization_image_ref` 或第二套媒体引用。只要模型仍知道其 `image_ref`，以后可以调用：

```json
{"image_ref":"sha256:..."}
```

由现有 `view_image` 重新读取截图。该引用只能恢复截图像素，不能恢复、反编译或编辑 HTML 源码。

为满足现有图片引用的重读合同，截图字节按现有工具图片 blob 规则持久化。这是用户显式要求回看所产生的图片 occurrence，不等于保存 HTML 编辑历史。一次 turn 内多次显式 review 可以产生多张截图；HTML 仍只在 terminal message 落定时保存一个最终版本。

### 6.3 回看失败

回看失败不撤销已经接受的订阅。工具结果先按 §4.3 如实说明“本次新登记”或“此前已登记”，再中立说明当前预览未生成，并给出既有公开错误分类允许披露的具体原因。它不得因已经订阅而跳过回看，也不得把回看失败描述成重复订阅造成的错误。模型可以继续编辑，再次请求回看。

不得因回看失败写入一个 HTML 草稿 blob，也不得把上一次成功截图误称为当前文件预览。

## 7. Terminal message 落定与唯一 HTML 持久化时机

### 7.1 最终物化顺序

模型已经生成 terminal assistant message 的文本、但该消息尚未完成 canonical publication 时，terminal publication owner 对当前 turn 的订阅做一次最终物化：

1. 按订阅顺序逐个取得闭合来源值；
2. 路径来源在真实 filesystem 权限和资源边界内读取当时的最终文件字节；若得到可靠 not-found，则按 §5.4 静默取消该项并跳过后续物化步骤；引用来源验证当前 session/workspace 的 canonical owner 并 exact read 已有 immutable HTML；
3. 校验文件类型、UTF-8/HTML 结构和可视化安全策略；
4. 在隔离环境中确认内容可形成可展示结果；
5. 冻结确切 HTML 字节；
6. 路径来源通过现有 canonical blob owner 保存 immutable `text/html` 内容；引用来源直接复用已验证的现有 blob；
7. 在 terminal assistant message 上提交有序 visualization occurrence，引用对应 blob 和来源 tool call；
8. 消息 publication 成功后清空进程内订阅。

最终读取与冻结是 publication 的组成部分，不能先发布一条只含可变路径的历史消息，再异步补写内容。历史 UI 的加载不得回到本地路径重新读文件。

### 7.2 只保存最终版本

数据库只保存每个最终 visualization occurrence 所引用的最终 HTML 字节：

- `write` / `edit` 的中间版本不进入 visualization HTML blob；
- 普通订阅不产生 blob；
- review 不产生 HTML blob；
- 同一路径重复订阅不产生多个最终 occurrence；
- 同一 `visualization_ref` 重新订阅会创建当前 terminal message 的新 occurrence，但复用已有 HTML blob，不复制内容版本；
- 内容相同的最终 HTML 可以沿现有 blob 内容身份自然复用字节，但每条消息的展示 occurrence 仍分别成立；
- 不维护 visualization revision table、版本链、latest pointer、草稿表或 path-to-blob registry。

最终 occurrence 应引用 existing blob，并直接属于 terminal assistant message。不要把 HTML blob 挂到 canonical tool result，也不要为了证明订阅执行过而新增 durable event。

### 7.3 最终物化失败

某个订阅在 terminal publication 时无法读取、校验或渲染，不得持久化不可信 HTML，也不得回退到某次 review 的旧内容。其他成功订阅和 terminal 文本可以正常提交。

路径 not-found 不是本节所称失败，而是 §5.4 的取消信号。只有可靠区分为其他错误时才进入失败投影；unknown read failure 不得猜测成 not-found，也不得静默吞掉。

失败项应在该 terminal message 的展示投影中形成一个简短、用户可见的“可视化未能附加”占位及安全的公开原因；它不伪装成工具失败重跑，也不触发新的模型调用。实现应复用 terminal message 的 occurrence/投影事务边界表达该结果，不为失败增加后台修复任务。

如果 terminal message 的 canonical transaction 整体失败，则沿既有 message publication 失败语义处理；不得单独提交孤立 HTML blob owner 或无消息归属的 visualization occurrence。

## 8. Canonical、provider 与 UI 投影

### 8.1 Canonical 关系

最终需要的是“terminal assistant message 拥有零到多个有序 visualization occurrence”的窄关系。每个成功 occurrence 至少能解析到：

- terminal assistant entry；
- occurrence ordinal；
- 最终 HTML blob；
- 发起订阅的 tool call / attempt 归属；
- 固定的展示 kind。

具体 SQL 名称由实现阶段结合 clean-v0 baseline 确定。本规格不授权通用 artifact registry、visualization project 表、revision 表或跨消息 mutable pointer。路径已经存在于原 tool arguments，不应为展示再复制成新的 durable authority。

`visualization_ref` 复用最终 HTML blob 的内容摘要稳定表示。它是内容引用，不是 occurrence ID；同一 HTML 被多个消息展示时可以具有相同引用，而每条消息的 occurrence 与 ordinal 仍分别成立。解析引用必须同时验证当前 session/workspace 存在允许的 canonical visualization owner，不得仅凭内容摘要直接读取任意 blob。

### 8.2 Provider 输入

- 普通订阅只产生正常的 tool call + 短文本 tool result，保持现有消息 suffix 追加语义。
- 最终 HTML 和聊天 UI visualization projection 不自动注入后续 provider 输入，不把任意 HTML 当 prompt 文本。
- 只有 `review=true` 时，截图通过既有工具图片 carrier 成为一次明确的模型视觉输入。
- 不因订阅、最终物化或 UI 重载重建当前 epoch 的 SYSTEM/tools/messages 前缀。

### 8.3 下一轮的 `visualization_ref` carrier

最终引用在 terminal publication 时才存在，不能伪造为更早的 tool result。下一次真实用户输入触发 provider call 时，上一条 terminal assistant entry 也首次成为 provider 输入的新 suffix；prompt compiler 在该 assistant entry 之后、真实用户消息之前派生一个独立的 user-role metadata carrier。每个实际 visualization occurrence 使用同一 formatter，形如：

```text
{"pulsara_visualization":{"visualization_ref":"sha256:..."}}
```

该 carrier：

- 不是 canonical 用户消息，不写回用户正文，不显示在聊天 UI；
- 不改变 recent human、队列归属、Hook/Skill 文本匹配或用户消息 Figure 语义；
- 不与下一条用户正文拼接，避免让模型误认为该 JSON 是用户输入；
- 只描述紧邻的上一条 terminal assistant message 所拥有的实际可视化 occurrence；同一内容出现多次时不按摘要删除 occurrence；
- 在后续 compiler 调用中保持在同一历史位置和相同字节，满足 epoch 内 provider prefix append-only；
- 不主动触发一次额外模型调用。没有下一条用户输入时，不需要为了“回传 ID”唤醒模型；
- 在 compaction 中随其所属 assistant evidence 一起保留或省略，不维护无限增长的全会话引用清单。

这与 review 图片的 `image_ref` 时序不同：review 图片引用在工具结果阶段已经存在，可以在当前轮立即交给模型；最终 HTML 的 `visualization_ref` 只能在后续 provider call 中首次出现。

### 8.4 前端展示

- 可视化展示属于 terminal assistant message，而不是中间 tool bubble。
- tool bubble 只显示“已订阅”或“已订阅并附加回看”的简短状态；review 截图仍按现有工具图片 UI 展示。
- 最终可视化按 occurrence ordinal 出现在 terminal message 的引用区域；canonical ownership 不因宽屏布局而改变。
- 历史重载直接读取 canonical blob projection；不依赖 cwd、原文件或仍存活的 runtime。
- HTML 必须运行在隔离容器中，不能直接注入 Pulsara 应用 DOM。

### 8.5 完整工作区宽度，而不是对话版心宽度

普通用户/assistant 消息、思考过程和工具卡可以继续使用居中的 conversation measure。最终 HTML visualization 是该排版的明确例外：它虽然在 transcript 流中由 terminal assistant message 拥有，但 visualization shell 必须横向 breakout 到当前中央工作区的完整可用宽度。

这里的“完整可用宽度”指：

```text
主工作区可滚动内容视口宽度
- visualization shell 自身必要的左右安全留白
= visualization 可用宽度
```

它不包括全局导航栏、左侧会话栏、右侧当前会话/能力检查器，也不能延伸到这些面板下方。右侧检查器打开、关闭或调整宽度时，可视化应随中央工作区重新布局；不得继续使用一个基于普通正文的固定 `max-width`。窄屏或面板压缩时同样以当时的中央工作区为准，不强行维持桌面宽度，不造成应用级横向滚动。

实现可以使用 transcript grid 的 full-bleed track、受控 portal 或等价布局机制，但必须满足：

- assistant 的头像、正文、引用说明和操作仍留在普通对话版心；
- 只有最终 HTML visualization shell 横向突破版心；
- shell 使用 `inline-size: 100%` 对应中央工作区可用 track，并取消 conversation message 的 max-inline-size 继承；
- iframe/document viewport 跟随 shell 宽度，HTML 内部可以使用响应式布局；
- 多个 visualization occurrence 各自占一行完整可用宽度，仍按 occurrence ordinal 排列；
- 宽屏布局只是前端 projection，不复制消息、不改变 canonical entry 或 provider 内容。

用户提供截图中的绿色框只标识本节的**横向范围**。绿色框的上、下边界和纵向高度不构成产品要求，也不得据此写死 iframe 高度、最小高度、最大高度、viewport 比例或占满剩余屏幕等规则。可视化的纵向尺寸需要由独立的高度/内容适配合同决定；在该合同明确前，本规格只冻结横向 full-width 行为。

## 9. HTML 安全与资源边界

### 9.1 Sandbox 安全边界

Pulsara 负责安全展示用户/模型提供的 HTML，而不是信任其脚本。首版至少应满足：

- 使用隔离 iframe / 独立受限 origin 或等价隔离边界；
- 禁止访问 Pulsara 页面 DOM、认证信息、cookies、本地存储和应用内部 API；
- 顶层导航、弹窗、下载和本地文件访问继续受 sandbox 与现有权限策略控制；
- 网络请求、外部脚本和其他资源遵循调用时已经存在的网络权限、Hook 与用户确认语义，不由 visualization 功能另建一套永久禁止规则，也不得借渲染绕过现有策略；
- 不通过前端 `dangerouslySetInnerHTML` 一类路径把 HTML 直接注入主应用；
- 最终物化和 review 使用同一安全策略，避免“回看可用、最终不可用”或安全边界分叉；
- 继续使用现有文件读取、工具执行、浏览器渲染和 blob 物理资源边界，不为本功能发明任意总历史或总任务上限。

这里必须区分两层边界：Pulsara 内嵌 renderer 要隔离宿主权限，但工作区中的 HTML 仍是用户拥有的普通物理文件。模型可以在现有授权范围内使用本地模块、多个配套文件、包管理器构建产物和联网资源；用户也可以脱离 Pulsara 手动打开它。`visualization_render` 本身只订阅或回看，不负责安装依赖，安装、构建和文件修改继续由现有 terminal / write / edit 及其权限 owner 负责。

### 9.2 内置常用可视化库池

首版预装以下三个本地、版本固定的库 profile。它们作为普通、版本化的本地资源按需提供给 visualization HTML，但不加入普通 Pulsara 聊天页面的首屏 bundle：

| alias | sandbox 全局 | 主要用途 |
|---|---|---|
| `echarts` | `window.echarts` | 默认通用方案；折线、柱状、散点、饼图、热力、关系图和常规交互式 dashboard 图表 |
| `vega-lite` | `window.vega`、`window.vegaLite`、`window.vegaEmbed` | 声明式统计图、分面、组合视图、自动比例尺/图例和可验证 JSON specification |
| `d3` | `window.d3` | 需要自定义 SVG/Canvas、比例尺、布局、过渡或非标准图形时的底层能力 |

这些 profile 按抽象层和任务类型分工：一般业务图、交互图和 dashboard 优先 ECharts；字段编码、统计变换、分面和声明式分析图优先 Vega-Lite；只有成品图表库无法表达的自定义图形、布局或地图才使用 D3。非常简单且无需库能力的单图继续使用原生 SVG。默认每份 HTML 只声明一个主要 profile；不得仅为“可能用到”而同时声明多个库。

ECharts profile 至少包含 line、bar、scatter、pie，以及 grid、dataset、transform、title、tooltip、legend、dataZoom 和一种浏览器 renderer。Vega-Lite profile 作为一个闭合能力同时提供 Vega、Vega-Lite 与 Vega-Embed；不能要求 HTML 自行从网络拼齐三项依赖。D3 使用官方浏览器 bundle。

Chart.js 不进入首版默认库池。它的常用 line、bar、scatter、pie/doughnut、radar、bubble、tooltip、legend、动画和响应式能力与 ECharts 高度重叠；主要差异是 API 简洁度和 bundle 取舍，而不是新增一类 Pulsara 无法完成的日常可视化。这只是 Pulsara 默认分发面的取舍，不是使用禁令；模型或用户仍可在既有权限下安装、打包或正常引用 Chart.js。

Plotly 不进入首版默认库池。其当前官方条款对随桌面软件分发另列许可要求；只有完成明确的产品/法务许可审查后，Pulsara 才能把它作为随应用分发的 versioned profile。该分发决定不限制用户或模型在普通文件中自行安装、打包或引用 Plotly；这类使用继续遵循用户授权、网络策略和适用许可。

### 9.3 声明、按需加载与版本冻结

HTML 通过固定 metadata 声明实际需要的内置 alias，并用普通浏览器资源标签引用工具描述所给出的具体版本路径。例如，下列 `<profile-version>` 在实际文件中必须替换为 concrete version：

```html
<meta name="pulsara-visualization-libs" content="echarts">
<script src="./_vendor/echarts/<profile-version>/echarts.min.js"></script>
```

确有必要时可以用逗号声明多个不同 alias：

```html
<meta name="pulsara-visualization-libs" content="d3, echarts">
<script src="./_vendor/d3/<profile-version>/d3.min.js"></script>
<script src="./_vendor/echarts/<profile-version>/echarts.min.js"></script>
```

没有该 metadata 时，runtime 不解析或映射任何 Pulsara 内置图表 profile；HTML 自己通过普通 `<script>`、`<link>`、ES module 或构建后的 bundle 引用其他资源仍然有效。runtime 必须在作者脚本执行前解析、去重、校验声明的 profile，并把对应 concrete local asset path 映射进隔离 renderer；真正的库加载仍由 HTML 的标准资源标签完成。未知 alias 只表示 Pulsara 无法提供该内置 profile，并明确报告这一点；它不构成对 HTML 里其他正常资源引用的全局否决。

“预装”表示宿主为离线、稳定和低摩擦创作提供可信的本地 immutable library assets，而不是把所有库注入每个 iframe，也不是形成唯一 allowlist。内置 profile 应暴露为普通 HTML 可以引用的、版本固定的本地资源（例如 `.pulsara/visualizations/_vendor/<alias>/<profile>/...`）；metadata 只帮助 runtime 解析、校验、映射和冻结对应 manifest，不能成为只能在 Pulsara 中运行的私有加载协议。review 和 terminal publication 只映射该 HTML 明确声明的内置 profile；普通聊天前端和未使用相应库的可视化不承担其下载、解析或执行成本。

模型若要安装其他依赖，应调用现有 terminal 工具并走相同的 permission / Hook / 用户确认链路；`visualization_render` 不运行 `npm install`，也不偷偷从用户环境猜测包位置。安装完成后，HTML 可以引用明确的本地构建产物。CDN 或其他远程资源在 renderer 中能否访问，由现有网络权限和 sandbox policy 决定；被拒绝时应报告具体受阻资源，不能提升权限或静默替换依赖。

final visualization occurrence 除 HTML blob 外，还要冻结已经解析的 versioned runtime profile/asset manifest。历史重载、即时 review 和最终展示必须使用相同 profile；应用升级不得让已有 occurrence 自动漂移到新的库版本。旧 profile 的保留属于真实的历史渲染依赖边界，不是 HTML 草稿版本或 visualization revision history。

上述冻结保证只覆盖 Pulsara 自带 profile。用户自行引用的 CDN、工作区包产物或其他配套文件不会因为订阅而自动复制进 PostgreSQL；如果这些资源后来变化或消失，历史展示可能无法逐字节复现。需要长期可复现时，模型应把依赖打包进单文件 HTML 或稳定的本地 bundle。首版接受这一明确限制，不以禁止外部库来伪造可复现性。

tool description / 模型指令应列出可用的内置 alias、对应全局和首选场景，并明确它们不是外部库 allowlist，同时给出响应式模板。由于可视化 shell 会随左右面板变化，ECharts 等需要显式 resize 的库必须通过 `ResizeObserver` 或宿主提供的薄生命周期适配响应容器尺寸变化，并在 iframe 销毁时释放实例。

### 9.4 普通浏览器打开合同

- 工作文件必须是合法的完整 HTML document，用户可从文件系统直接打开；Pulsara metadata 在普通浏览器中应无害地被忽略。
- 内置 profile 的示例模板应使用普通、可解析的本地资源引用；不能只依赖宿主注入一个浏览器外不存在的全局变量。
- 如果作者选择 CDN、开发服务器或其他远程依赖，手动打开时由浏览器与用户环境决定是否可访问；Pulsara 不伪装成这些依赖的唯一入口。
- 如果作者选择需要构建的 npm 包，产出的 HTML / bundle 应是可被普通浏览器消费的文件；安装和构建不是 `visualization_render` 的职责。
- 离开 Pulsara 手动打开时不再受 Pulsara iframe sandbox 保护，而受浏览器自身安全模型、用户系统配置和资源来源策略约束。

## 10. 并发、取消与文件变化

- 路径来源只在 terminal publication 时读取一次最终版本；runtime 不建立文件 watcher。引用来源只解析并 exact read 已冻结内容。
- `write` / `edit` 与 review 的文件读取按既有工具调用顺序和并发规则结算。
- 相同来源的并发订阅通过 active-turn owner 的原子 insert-if-absent 线性化；只能有一个“首次订阅”结果，最终仍只有一个槽位。
- review 只描述它实际读取到的那个时刻，不承诺与最终版本相同。
- terminal publication 读取期间取得的完整字节形成 frozen candidate；之后文件再变化不影响本次消息。
- 如果读取期间检测到既有文件完整性/稳定性条件不成立，按最终物化失败处理，不循环重试直到“碰巧稳定”。
- 用户取消 turn 时，尚未提交的订阅和进程内 HTML bytes 一并丢弃；已经接纳的 review 图片结果按既有 tool settlement 语义处理。
- 用户 steer 后由文件工具完成的删除若先于最终物化结算，则该路径按 §5.4 取消；runtime 不需要 watcher、steer 专用事件或额外 unsubscribe 状态。

## 11. 明确不做

本轮不实现：

- 在 `visualization_render` 参数里直接传 HTML；
- visualization 专用 write/edit/patch/diff 工具；
- 根据截图反向编辑 HTML；
- 自动保存每次 `edit`、每次订阅或每次 review 对应的 HTML 版本；
- visualization revision history、undo、branch、merge 或 latest-version registry；
- 未完成订阅的跨重启恢复；
- 后台发布站点、分享链接或永久 URL；
- visualization 列表、搜索、分页、历史发现或数据库源码编辑入口；
- 独立的 `visualization_unsubscribe` 工具或持久化取消记录；路径来源通过最终读取时 not-found 表达取消；
- 把 `image_ref` 当 HTML 引用；
- 在 provider 历史中自动重放 HTML；
- 让前端从任意历史本地路径重新加载内容；
- 让 `visualization_render` 自己安装依赖、调用包管理器、修改 lockfile 或替模型完成构建；这些动作继续由现有 terminal 与权限链路负责；
- 在未获得现有权限或用户确认时自动联网、安装包或执行外部资源；
- 承诺把任意 CDN、本地配套文件或用户安装依赖自动冻结进 canonical HTML blob；首版只对 HTML bytes 和已解析的 Pulsara 内置 profile 提供历史冻结；
- 未经许可审查把 Plotly 作为 Pulsara 默认随应用分发的内置 profile；这不限制用户自行安装或引用；
- 为本功能增加 feature flag、旧/新双轨、兼容 schema 或迁移期 fallback。

## 12. 实施落点原则

实施前必须先检查并复用现有 owner：

1. builtin catalog 与 tool schema/description；
2. DirectKernelToolPort、Hook、permission、attempt、settlement；
3. active-turn process-local observation/state owner；
4. terminal assistant message publication transaction；
5. PostgreSQL canonical blob store；
6. `view_image` 工具图片 result、derived user-role carrier 与 `image_ref` lowering；
7. 浏览器/前端对 canonical terminal message 的有序投影；
8. 前端构建系统、sandbox asset loader 与第三方 license/NOTICE 汇总 owner；
9. terminal 包管理器调用、网络访问、Hook、permission 与用户确认 owner。

只增加 visualization 所必需的窄 typed value、进程内订阅 owner 和最终 message occurrence。若现有 blob、图片 carrier、安全浏览器或 message attachment 扩展点已经足够，必须使用这些扩展点，不复制存储引擎、图片协议、tool result compiler 或 iframe runtime。

由于仓库仍处于开发阶段，实施采用 clean-v0 hard cut：生产代码、数据库 baseline、协议 DTO、前端 projection、工具描述、测试和本规格一起更新，不增加 legacy alias、双写、fallback 或迁移期适配。

## 13. 验收矩阵

至少覆盖以下行为：

1. 普通订阅只返回短状态，不打开/读取 HTML，也不写数据库。
2. 订阅后继续多次 `edit`，最终只展示 terminal publication 时的最新内容。
3. 同一规范化路径或同一 `visualization_ref` 重复订阅只产生一个最终 occurrence，顺序保持第一次订阅位置。
4. 多个不同来源按首次订阅顺序展示。
5. 首次订阅返回中立的 now subscribed 状态；后续相同来源返回中立的 already subscribed 状态，不出现 duplicate、ignored、unnecessary、warning 或归责文案。
6. 相同来源并发调用只能有一个首次订阅结果，最终仍只有一个订阅槽位和 occurrence。
7. `review=true` 在首次和后续订阅调用中都执行当前回看；随后编辑不会让最终消息错误使用旧 review 版本。
8. review 截图具有合法 `image_ref`，可由 `view_image(image_ref=...)` 重读。
9. 多次 review 可以形成多张图片 occurrence，但数据库中不产生对应的 HTML 草稿版本。
10. 路径来源在 terminal publication 时只保存一个最终 HTML blob/occurrence；引用来源复用已有 blob，只新增当前消息 occurrence。
11. 提交后修改或删除原文件，历史消息展示不变。
12. review 失败不撤销订阅；工具结果保留准确且中立的首次/已订阅状态，修复文件后再次 review 可以成功。
13. 最终读取失败不回退到旧 review，不提交不可信 HTML，terminal 文本和其他成功可视化仍可用。
14. turn 取消或进程在 publication 前退出，不留下订阅行、孤立 occurrence、恢复 job 或 HTML 草稿版本。
15. 当前轮普通订阅不产生图片 carrier；review 才复用现有工具图片 carrier。
16. 下一次 provider call 在上一条 terminal assistant entry 后产生稳定的 `visualization_ref` metadata carrier；它不进入用户正文、UI、recent human 或 Hook/Skill 匹配。
17. `visualization_ref` 可以在后续轮次由同一工具重新订阅或 review；解析必须验证当前 session/workspace canonical owner，不能仅凭 digest 读取 blob。
18. 最终 HTML 正文不进入 provider prompt，也不写入 tool result artifact 字段。
19. 相对路径按冻结 cwd 解析；绝对路径与 `..` 不因 workspace 边界被该只读工具额外拒绝，仍完整执行既有权限和 Hook。
20. iframe/渲染隔离阻止 HTML 访问 Pulsara DOM、凭据、存储和内部 API；顶层导航、弹窗、下载、本地文件与网络访问遵循 sandbox 和现有权限策略，不能借 visualization 提权。
21. 冷启动、append、compaction、reconnect 和历史重载不改变同一 epoch 的 provider prefix，也不尝试恢复进程内订阅。
22. 最终 HTML shell 不继承普通 conversation measure/max-width，在左右面板之间占用中央工作区的完整可用横向空间；assistant 正文仍保持原版心。
23. 打开、关闭或调整右侧检查器后，visualization shell 随中央工作区宽度响应，不进入侧栏下方，也不造成应用级横向滚动。
24. 不从参考截图绿色框的纵向尺寸推导或写死 iframe 高度；横向 full-width 与纵向 sizing 分别验证。
25. 路径订阅后删除文件，terminal publication 静默取消该项：无错误、无失败占位、无 HTML blob、无 occurrence 或 durable cancellation 记录。
26. 用户 steer → 文件工具删除 → terminal publication 的真实路径可取消最终展示；此前 review 图片仍保留原有 canonical 结果。
27. 删除后在最终读取前重建同一路径时，使用重建后的最新内容正常展示；不追踪中间删除历史。
28. permission denied、I/O failure、目录/非普通文件、无效 HTML、安全校验或渲染失败不会被误判为 not-found 取消；unknown failure 也不得静默吞掉。
29. 多个订阅中删除一个路径只取消该项，其余成功 occurrence 保持相对顺序并使用连续 ordinal；`visualization_ref` 来源不适用删除取消。
30. ECharts、Vega-Lite 与 D3 三个内置 alias 均能离线按需加载，并分别暴露规格固定的 sandbox 全局；普通聊天 bundle 不因未使用而加载它们。
31. 单库 HTML 只映射声明的内置 profile；多库声明按固定顺序去重映射；无 metadata 时 runtime 不映射内置图表库，但 HTML 自己的普通资源引用仍可工作。
32. 未知内置 alias 明确报告“宿主不提供该 profile”，但不把正常 `<script>`、`<link>`、ES module、本地 bundle 或经授权的远程资源误判为非法；`visualization_render` 不自行运行包管理器。
33. review 与 terminal publication 对同一 HTML 使用完全相同的 resolved runtime profile；截图与最终展示不因库版本不同而分叉。
34. final occurrence 冻结 versioned runtime profile/asset manifest；应用升级后重载历史消息仍使用原 profile，不自动漂移到新版本。
35. ECharts 图表在右侧检查器开关和中央工作区 resize 后正确 resize，iframe 销毁时释放实例与监听器。
36. 首版没有默认 Chart.js 或 Plotly profile；测试确认对应 alias 未登记，但用户或模型经现有权限链路安装、打包或正常引用后仍可使用它们。
37. 工作文件可以由普通浏览器直接打开；Pulsara metadata 会被安全忽略，内置 profile 模板不依赖只能由宿主注入的私有协议。
38. 经现有权限允许的 CDN、开发服务器和本地配套资源可在 renderer 中加载；被权限或 sandbox 拒绝时返回具体资源错误，不静默提权或替换依赖。
39. 通过 terminal 安装并构建的第三方库产物可以被 HTML 正常引用；安装动作仍产生现有 permission / Hook / settlement 事实，visualization 工具不新增安装旁路。
40. 使用非内置依赖的最终消息只冻结 HTML bytes；删除或改变未捕获的外部资源可能影响历史渲染，该限制被明确呈现而不是通过禁用第三方库掩盖。

## 14. 激活条件

本文当前只记录已确认的产品合同，不宣称已经实现。只有在以下条件全部满足后，状态才可改为 ACTIVATED：

- clean-v0 schema 与生产实现完成 hard cut；
- focused backend、storage、compiler 和 frontend tests 全部通过；
- 浏览器验收确认中间工具卡、即时 review、最终引用展示、三个内置库 profile 与历史重载；
- 真实 provider dogfood 证明模型能用 `write` / `edit` → subscribe → review → edit → terminal display 的完整路径；
- dogfood 证据保留实际工具调用、短 tool result、截图 `image_ref`、最终消息 occurrence、最终 HTML blob 与冻结 runtime profile/asset manifest 的数据库事实；
- 未新增 HTML 版本表、订阅恢复机制、visualization 专用编辑工具或 provider prefix 重建边界。
