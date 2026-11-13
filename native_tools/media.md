# Pulsara 多媒体工具修订与使用教程草案

共用规则见[原生工具修订草案索引](../PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章；完整保存、原生 12k／NEXT 和 JS 完整返回沿[统一交付草案](../PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)，有序内容、图片资产及引用读取沿[数据库 hardcut 草案](../PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md)。

状态：讨论草案，未实施。本族四个工具的参数、完整业务值、原生示例及错误语义均在本文件确定。view_image、read_video_frames、read_transcript 提供 ROOT 原生入口和 Code Mode JS API；annotate_media 仅 ROOT 原生，不提供 JS API。本族不向 subagent 开放，不改变 Code Mode 的既有权限模式。本轮只改文档。

## 1. view_image：读取图片或静态渲染结果

### 1.1 用途与最简参数

读取一张本地图片，或凭已知 IMAGE_REF 读取当前数据库中的图片。PATH 也可指向 SVG、HTML，按现有静态渲染／截图规则生成图片；本工具不播放动画或音视频，不自动解释、描述或总结画面，也不等同于向用户打开文件。

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PATH 或 IMAGE_REF Target | 必填 | 本次要读取的本地文件或已保存图片引用 |

target 放首位，工具只接受这一个参数。PATH 只接受 kind、path，path 为非空字符串，支持工作区相对路径、绝对路径及 ~，沿现有路径解析和权限检查。IMAGE_REF 只接受 kind、image_ref，按共享 ID 层验证 image 身份并直接读取对应图片资产；不校验调用方与资产的 session／workspace、来源 entry 类型、队列消费状态或可视化 READY 资格。不接受 null、额外字段或其他 Target kind。

```typescript
type ViewImagePathTarget = {kind: "PATH"; path: string};
type ViewImageRefTarget = {kind: "IMAGE_REF"; image_ref: string};
type ViewImageTarget = ViewImagePathTarget | ViewImageRefTarget;

interface ViewImageArgs {
  target: ViewImageTarget;
}
```

保留 PNG、JPEG、静态 WebP 的实际解码验证；SVG 渲染为静态图片，HTML 按当前内容根或 1200×800 浅色视口、减少动态效果及既有资源访问规则截图。SVG 不执行脚本或加载外部资源，保持其既定尺寸或 viewBox 比例且不裁剪。APNG、动画和多帧图片继续不作为单张静态图片接受。不能仅按扩展名认定格式或验证成功。

不增加页长、图片数量、清晰度、裁剪、截图尺寸或模型展示预算参数；原有文件读取、渲染、解码、取消和真实资源边界由现有 owner 承担。删除旧展示及存储上限按 unified 处理，不因删除分页限制而取消独立图片验证。

### 1.2 最简返回值与有序 content

原生和 JS 使用同一份完整业务值：

| 字段 | 明确含义 |
| --- | --- |
| target | 本次实际读取对象；PATH 使用工作区内相对、工作区外绝对路径，IMAGE_REF 保留原引用 |
| meta.status | SUCCESS／ERROR／CANCELLED，表示本次业务调用的实际结算 |
| content | 有序的 text／image 内容项；成功为简短事实说明，紧随其后是一张实际图片 |

```typescript
interface ViewImageTextPart {
  type: "text";
  value: string;
}

interface ViewImageImagePart {
  type: "image";
  value: ViewImageRefTarget;
}

interface ViewImageSuccess {
  target: ViewImageTarget;
  meta: {status: "SUCCESS"};
  content: [ViewImageTextPart, ViewImageImagePart];
}

interface ViewImageFailure {
  target?: ViewImageTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: ViewImageTextPart[];
}

type ViewImageResult = ViewImageSuccess | ViewImageFailure;
```

说明使用实际读取／验证事实，例如“已载入图片（1920×1080，image/png）。\n”。SVG、HTML 还在同一文字项中说明实际渲染或截图的必要事实，不把静态截图解释为播放验证。文字项自行保存结束换行，runtime 不自动在 content 项之间添加分隔符。

top-level target 表达读取对象，image 项表达结果资产。例如读取 SVG 文件时，顶层仍是该 PATH，image 项指向实际生成的图片资产。重新读取 IMAGE_REF 时复用原图片身份，不因原生页或 JS 入口另造同图身份。

不另返回 image_ref、images 数组、独立附件清单、重复尺寸／MIME 字段或 Base64 图片。尺寸、MIME 和字节由图片资产 owner 管理，说明引用真实事实；meta 只保留 status。必要信息不通过 _hint、meta.error 或第二份 message 表达。

### 1.3 原生调用与结果投影

读取本地图片：

```text
工具：view_image
参数：
{
  "target": {"kind":"PATH","path":"output/chart.png"}
}
```

假设验证得到 1920×1080 PNG，保存的完整业务值为：

```json
{
  "target": {"kind": "PATH", "path": "output/chart.png"},
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "已载入图片（1920×1080，image/png）。\n"},
    {
      "type": "image",
      "value": {"kind": "IMAGE_REF", "image_ref": "image:<图片ID>"}
    }
  ]
}
```

假设这一页可以完整交付，原生 tool 消息为：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际调用耗时>

target: {"kind":"PATH","path":"output/chart.png"}

meta:
  status: SUCCESS

content:
  已载入图片（1920×1080，image/png）。
  图片引用：{"kind":"IMAGE_REF","image_ref":"image:<图片ID>"}

tool_result: <本次完整结果的导出 Target>
next: null
```

模型支持图片时，该页另外投影关联的 user 图片消息，包含实际 tool_call_id、同一 IMAGE_REF 标记和真实图片。两者共同构成一页交付，不仅返回引用而声称模型已看到图片。关联 user 消息不重复整段 tool 正文，不创建另一条人类 entry，不改变 turn 或权限来源。

当前模型不支持图片时，仍先读取、验证并保存完整图片结果，再从同一 content 投影纯文字：原生 meta.status 为 ERROR，每个 image 项在原位置投影图片引用及固定说明，不发送 user 图片附件。例如本次一页可交付完全部文字投影：

```text
target: {"kind":"PATH","path":"output/chart.png"}

meta:
  status: ERROR

content:
  已载入图片（1920×1080，image/png）。
  图片引用：{"kind":"IMAGE_REF","image_ref":"image:<图片ID>"}
  当前模型不支持图片输入，未交付实际图片。

tool_result: <已保存完整图片结果的导出 Target>
next: null
```

ERROR 表达本次看图交付未完成，说明同时保留图片读取和保存成功的事实。数据库仍保存原 text／image 项及图片资产，不把 image 改存为 text，不反写原完整业务值中的成功事实，也不把文字占位记作模型看过图片。此处 next:null 仅表示这次文字错误投影已交付完；若仍有结果后缀，继续使用 NEXT，不为能力缺失反复返回同一位置。

读取已知图片引用：

```text
工具：view_image
参数：
{
  "target": {"kind":"IMAGE_REF","image_ref":"image:<图片ID>"}
}
```

成功业务值仍为 target／meta／content，顶层 target 改为该 IMAGE_REF，content 中的 image 使用同一图片身份。图片从保存资产读取，不查回原文件或重新截图。

示例身份为说明占位。观察时间和实际微秒耗时在原生消息顶层并列，未测得耗时不得伪造为 0；它们不混入 JS 业务值。

### 1.4 JS 完整返回与显式图文输出

将 view_image 接入 Code Mode 的实际冻结工具目录及返回类型声明。tools.view_image 使用与原生相同的参数和业务生产者，取得请求范围内完整的 ViewImageSuccess；不返回原生页面、tool_result、next、观察时间外壳或 Base64 字节。

```javascript
const picture = await tools.view_image({
  target: {kind: "PATH", path: "output/chart.png"}
});

console.log({content: picture.content});
```

picture.content 是完整的有序对象数组，图片的 value 是 IMAGE_REF。图片字节由 runtime 图片 owner 读取和保存，不要求复制进 JS 内存；程序可以直接访问、筛选或组合这些内容项。console.log／return 在实际输出值中递归识别任意层级的 image 内容项，不要求它处于顶层 content；单独图片项、图片项数组、嵌套组合及完整结果都沿同一图片投影路径处理。

例如下面两种是独立用法，分别表示交付完整结果及只交付图片：

```javascript
const result = await tools.view_image({
  target: {kind: "PATH", path: "output/chart.png"}
});
console.log(result);
```

```javascript
const result = await tools.view_image({
  target: {kind: "PATH", path: "output/chart.png"}
});
return {
  content: result.content.filter(part => part.type === "image")
};
```

嵌套组合也可以直接交付，无需补 content 外壳：

```javascript
const result = await tools.view_image({
  target: {kind: "PATH", path: "output/chart.png"}
});
const image = result.content.find(part => part.type === "image");
console.log({comparison: {left: image}, note: "请检查趋势。"});
```

console.log(image)、console.log([image]) 及多参数日志中的图片项也采用相同规则。

脚本也可以只选择图片，添加自己的说明后交给 ROOT：

```javascript
const picture = await tools.view_image({
  target: {kind: "PATH", path: "output/chart.png"}
});

console.log({
  content: [
    {type: "text", value: "请检查图中的趋势。\n"},
    ...picture.content.filter(part => part.type === "image")
  ]
});
```

只有脚本实际输出的值按组合顺序交付，不扫描未输出的 JS 变量，不自动补回被筛掉的原说明。显式顶层 return 与 console.log 的每个参数采用相同递归图片识别规则；console.log／return 是独立 Cell steer，不套原生 tool 包装，不产生分页 next。既有异常诊断及晚到结果补发例外沿 unified 和 Code Mode 工具草案保持，不在此扩大。

JS 图片读取、验证和完整保存与 ROOT 展示预算分离。不能因 ROOT 当前不支持图片、原生页面余量不足或图片展示额度不足，提前拒绝有效的 JS 中间结果、缩图、截短或触发 ROOT 压缩。直到 console.log／显式 return 通过 steer 向当前模型交付时，才根据模型能力投影：支持图片则实际投图并计入图片／上下文准入预算；不支持则在每个 image 项原位置使用第 1.3 节相同的固定文字，不发送图片附件。JS 读取成功不代表图片已经交给 ROOT。

steer 不增加原生工具的 meta.status 外壳，不把程序值改成 ERROR，也不反向修改已经返回的 JS 结果。直接输出完整 result 时，其 meta 等字段仍是原业务事实的文字投影。每个图片节点独立读取并投影；引用无效、不存在、图片损坏或读取失败时，按第 1.7 节在原位置给出引用和实际原因，其他内容继续交付，不因单图失败拒绝整条 steer。

### 1.5 凭图片引用读取与数据库归属

PATH 沿现有文件读取权限处理。IMAGE_REF 则凭已知引用直接读取当前数据库中的对应图片资产，不检查 session、workspace、是否进入会话、来源 entry 类型、队列是否消费或可视化是否 READY。未消费、已取消或已拒绝队列中的图片，只要资产仍存在就同样可读；资产已被清理则按不存在处理。

view_image(IMAGE_REF) 和 Cell 输出递归识别到的 image 节点复用同一图片读取 owner，仅核对引用结构、实际资产存在性及图片有效性，不另建读取资格或模型已读规则。手写引用若确实对应真实图片，就正常读取；无效或不存在则在原位置给出文字说明。

递归识别的是 {type:"image",value:<IMAGE_REF Target>} 这样的实际对象节点，不解析字符串中的 JSON，不把裸 IMAGE_REF、URL 或 PATH 自动当作图片。模型仍须显式输出该节点；未输出的 JS 中间值不自动进入 ROOT。Cell steer 本身仍没有 read_file 读取、续读或导出入口。

数据库只维护同一完整结果：已声明的非 content 业务字段归现有工具结果行，有序说明和图片出现项归 content_parts，图片字节、MIME 和验证后的尺寸归 blob。image 项使用真实 blob 外键，公开 IMAGE_REF 从它产生；相同图片资产可复用，但每次结果中的出现及顺序保留。

图片资产本身不记录 session／workspace，只保留资产身份、不可变字节及固有图片属性；读取、发布和身份派生也不以二者为条件。图片出现项仍归实际消息或队列，按 blob_id→blobs.id 引用全局图片资产，不带 session／workspace 联合外键；不同会话和工作区可以复用同一图片。共用 blobs 表中，图片行的 workspace_id 为空，非图片正文 blob 保留既定归属。不保存平行图文 JSON、拼接全文、独立图片清单或原生页面正文，不恢复 canonical_image_refs 双轨，不新增读取资格、已投递标记或图片注册表。

### 1.6 原生 12k／NEXT 与 JS 的分离

图片项的模型能力投影统一用于原生页、NEXT 续页和 Cell steer，数据库原值始终保持有序图文。模型不支持图片时，每个 image 项固定投影为：

```text
图片引用：{"kind":"IMAGE_REF","image_ref":"image:<图片ID>"}
当前模型不支持图片输入，未交付实际图片。
```

console.log 的每个实际输出参数和显式 return 都递归遍历数组与对象；图片项可以位于任意字段、数组层级或 object.value 内，不要求顶层 content、单参数或工具结果品牌。数组顺序、普通对象的字段／层级及既有文字序列化顺序保留；每个图片节点在自己的位置投影，不另打印整份对象后再重复 content。同一图片多次出现仍保留每次出现。

已有 {content: ResultPart[]} 显式有序输出形式继续支持，content 按共同项类型校验，文字／Target 等沿既定投影；它只是组合方式，不是图片识别的必要外壳。普通字符串不被解析成对象，裸 Target 不触发文件读取。JS 原业务对象保持原值，仅在 Cell 输出接纳时将所选结构形成一份有序文字／图片内容；不引入结果注册表、嵌套正文实体或递归深度 cap。

原生调用先完整读取、验证并保存本次业务值，再按统一 12k token 启发式预算顺序交付。图片不可拆，说明按文字正常分页；说明与图片允许分属两页，不增加不可拆分组或绑定标记，不回退或复制上一页说明。

单图本身估值超过 12k 时适用独占页例外，仅图片及必要投影标记可以超过目标，说明不借此豁免。12k 产品计量仍以数据库规范结果为准；实际 provider 准入按真正发出的文字及图片计算。不支持图片时使用上述明确的文字错误投影；模型支持图片但实际资源仍不能接纳时报告交付失败，不将其混成能力缺失或仅给引用冒充已看图。

next 只使用完整 NEXT Target，原样交给 read_file；内部定位不向模型暴露。续读按同一能力规则投递所在页的真实图片或固定文字说明，不要求模型额外调用 view_image 才完成该次投影。NEXT 和导出只读冻结结果，不重新打开 PATH、查回原文件、解码生成新证据或再次渲染 SVG／HTML；原文件随后变化或移除不改变已保存结果。

JS 返回完整类型化值，不能复用原生第一页作为返回值，也不生成展示 next。console.log／return 组合输出首版不另设 12k 或其他展示预算、不自动分页，真实输入边界仍按 unified 处理。原生结果的分页、JS 程序值和 Cell steer 不各自维护一份平行业务正文；脚本显式组合形成的独立输出仍按其实际内容和来源保存。

### 1.7 错误、取消及交付失败

| 情况 | 公开处理 |
| --- | --- |
| 文件不存在、读取失败、格式不支持、图片损坏或渲染失败 | ERROR；content 说明实际原因及路径，不生成假图片引用 |
| IMAGE_REF 格式无效、资产不存在或读取／解码失败 | 原生 ERROR；content 保留请求引用及实际原因，不查 session／workspace 或来源资格，不生成假图片项 |
| 原生调用的当前模型不支持图片 | 完整读取、验证、保存后，以 ERROR 交付同一 content 的纯文字投影，在图片位置附固定说明；不发送图片附件，不改写保存内容 |
| 调用取消 | CANCELLED；说明实际情况，不伪造完整图片结果 |
| 单个图片节点无法读取或解码 | 该位置改为引用和实际失败说明；原生 ERROR，steer 其他内容继续交付；不把占位记为已看图 |
| 原生整页仍无法交付 | 沿统一错误路径保留有效完整结果 Target 和未交付起点的 NEXT，不把该页记为已读 |
| 保存失败或状态未确认 | 如实说明，不提供假 tool_result／NEXT，不自动重读或重渲染 |

失败值可以保留可解析的请求 target，这只表达请求对象，不证明图片资产存在；无法解析时不伪造 Target。失败值不带假图片项、尺寸或 MIME。meta.status 是唯一公开调用状态，具体原因放 content，不返回 meta.error、_hint 或额外错误枚举。业务错误正常保存并交付完毕时，tool_result 可以有效且 next:null；这只表示错误结果已读完。

格式正确但没有对应资产的引用，采用以下文字。steer 保留它前后的内容，不让整条输出因这个节点失败：

```text
图片引用：{"kind":"IMAGE_REF","image_ref":"image:<图片ID>"}
图片不存在或已不可用，未交付实际图片。
```

原生 view_image 读取同一不存在的引用时，例如错误值已正常保存并完整交付，结果为：

```text
target: {"kind":"IMAGE_REF","image_ref":"image:<图片ID>"}

meta:
  status: ERROR

content:
  图片引用：{"kind":"IMAGE_REF","image_ref":"image:<图片ID>"}
  图片不存在或已不可用，未交付实际图片。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

引用格式无效则保留原输入的文字引用并说明格式无效；读取超时使用“图片暂时无法读取，未交付实际图片。”，解码失败说明实际解码原因，不能把未知读取故障断言为不存在。不存在的图片不能保存为 image 外键；Cell 接纳时将该节点形成真实 text 说明。真实资产因模型不支持而显示文字时，数据库仍保存 image 项，二者不能混同。

文件不存在，且错误结果已经正常保存交付：

```text
target: {"kind":"PATH","path":"output/missing.png"}

meta:
  status: ERROR

content:
  无法读取图片 output/missing.png：文件不存在。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

JS 业务失败沿既有异常通道抛出，实际存在的完整失败业务值保留在 err.result，不分页。没有业务值的参数或运行时失败不伪造 err.result：

```javascript
try {
  const picture = await tools.view_image({
    target: {kind: "PATH", path: "output/missing.png"}
  });
  console.log({content: picture.content});
} catch (err) {
  if (err.result) {
    console.log({content: err.result.content});
  } else {
    console.log(String(err));
  }
}
```

当前模型不支持图片或单图读取失败时，在对应节点使用明确文字投影，不向已返回的 console.log 补抛异常，不改写原 JS 业务值。JS 的 view_image 调用本身若读取失败，仍沿上述业务异常通道提供 err.result。整条 ROOT 请求仍无法接纳时沿既有请求错误路径报告，不重做图片工具，不新增任何资源 cap；原生 12k 页预算及已确定的实际请求预算保持原义。

## 2. read_video_frames：取得视频图像证据与时间索引

### 2.1 参数、范围与三个 action

按指定 action 从本地视频取得图像证据和实际时间索引。一次调用完整完成本次请求范围的选择、渲染及保存；原生分页只决定交付范围，不改变证据选择。图片不等同于转录或视频内容总结，工具不自动生成对画面的语义描述。

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PATH Target | 必填 | 本地视频文件，沿现有文件读取规则解析 |
| from | number | overview／motion 可选，默认 0；detail 必填 | 开始秒数，包含该端点，支持小数 |
| to | number | overview／motion 可选，默认媒体结束；detail 必填 | 结束秒数，不包含该端点 |
| action | overview／detail／motion | 可选，默认 overview | 定位内容、局部清晰观察或连续过程观察 |
| crop | [left,top,right,bottom] | 可选，默认完整显示画面 | 对显示画面应用的归一化裁剪边界 |

参数只接受这五项，按表中顺序声明，target 放首位；PATH 只接受 kind、path，路径为非空字符串，支持工作区内相对、工作区外绝对路径及 ~。公开参数统一使用 action，删除 mode 及 continuation，不保留别名或另一种后继调用参数。

from／to 必须是有限数值，满足 0 ≤ from < to，不接受布尔、null、NaN 或无穷大。媒体时长已知时，to 不得超过媒体时长；时长未知而 to 省略时明确报错，不能猜测结束位置。默认范围在调用执行时解析一次，冻结后不因分页重新解析。

detail 要求显式 from、to，且 0 < to-from ≤ 2 秒。overview／motion 不增加总时长、总帧数或累计图片数 cap；内部有界分析／渲染批次只控制瞬时工作集，不能截掉本次请求后缀。

crop 是显示方向及像素宽高比校正后画面上的两个角点，满足 0 ≤ left < right ≤ 1、0 ≤ top < bottom ≤ 1，不是 [x,y,width,height]。只接受四个有限数值，沿既有裁剪及质量规则处理，不新增输出尺寸、FPS 或清晰度参数。

```typescript
type VideoCrop = [number, number, number, number];
type VideoFrameAction = "overview" | "detail" | "motion";

type ReadVideoFramesArgs =
  | {
      target: ViewImagePathTarget;
      from?: number;
      to?: number;
      action?: "overview" | "motion";
      crop?: VideoCrop;
    }
  | {
      target: ViewImagePathTarget;
      from: number;
      to: number;
      action: "detail";
      crop?: VideoCrop;
    };
```

| action | 保存的图文组织 | 保留的证据语义 |
| --- | --- | --- |
| overview | 范围说明；逐张拼图索引 object → 拼图 image | 现有稀疏代表状态选择、拼图质量和时间映射；不表示连续看过范围内每一帧 |
| detail | 范围说明；逐个观察时间 object → 清晰 image | 对有效视频交集选首、中、严格位于结束前的目标；同一源帧命中多个目标时合并为一次实际观察，完整计划为一至三个不同源观察 |
| motion | 范围说明；逐张索引 object → 等格拼图 image | 现行连续观察、真实时间顺序、裁剪及空间质量；完整请求范围先生成并保存，窗口仅内部使用 |

保留现有三个 action 的选择与质量，不采纳另行延后讨论的 motion 改版，不新增变化筛帧、降 FPS、多画布排版策略或采样类型。业务处理期间的文件绑定及实际变化检查仍由视频 owner 承担，不能把这一过程的瞬时检查变成模型续读条件。

### 2.2 最简返回值与索引类型

| 字段 | 明确含义 |
| --- | --- |
| target | 本次视频文件的 PATH，工作区内相对、工作区外绝对 |
| meta.status | SUCCESS／ERROR／CANCELLED，沿共同状态规则 |
| content | 简短范围／实际缺口说明，随后按顺序交错保存索引 object 和对应 image |

meta 只保留 status，不重复 action、from／to、crop、显示尺寸或分析统计。范围说明写入解析后的本次请求范围，不将第一张图或当前页面的时刻当作整个业务范围。视频起止与请求存在真实缺口时在文字中明确，不制造画面，也不用 next:null 掩盖缺口。

detail 索引直接描述紧随其后的单张观察图片；overview／motion 的每个索引只包含紧随其后那张拼图的格子。图片配对来自有序出现，不并列保存全局图片编号清单，不新建分组或绑定字段。

```typescript
type VideoTimeRange = [number, number];

interface VideoDetailIndex {
  t: number;
  visible_range: VideoTimeRange;
  dt?: number;
}

interface VideoGridCell {
  n: number;
  t: number;
  cell_rect: [number, number, number, number];
  dt?: number;
  visible_range?: VideoTimeRange;
  representative_range?: VideoTimeRange;
}

interface VideoCollageIndex {
  frames: VideoGridCell[];
}

interface VideoIndexPart<T> {
  type: "object";
  value: T;
}

interface VideoFramesSuccess<T> {
  target: ViewImagePathTarget;
  meta: {status: "SUCCESS"};
  content: (ViewImageTextPart | VideoIndexPart<T> | ViewImageImagePart)[];
}

type VideoOverviewSuccess = VideoFramesSuccess<VideoCollageIndex>;
type VideoDetailSuccess = VideoFramesSuccess<VideoDetailIndex>;
type VideoMotionSuccess = VideoFramesSuccess<VideoCollageIndex>;

interface VideoFramesFailure {
  target?: ViewImagePathTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: (
    | ViewImageTextPart
    | VideoIndexPart<VideoDetailIndex | VideoCollageIndex>
    | ViewImageImagePart
  )[];
}

type ReadVideoFramesResult =
  | VideoOverviewSuccess
  | VideoDetailSuccess
  | VideoMotionSuccess
  | VideoFramesFailure;
```

索引字段含义：

| 字段 | 明确含义 |
| --- | --- |
| t | 真实源帧展示起点秒数；帧可能从 from 之前就开始显示，不伪造为请求起点 |
| visible_range | 该观察实际可验证的展示区间与本次请求的适用交集，不表示两次稀疏观察之间没有变化 |
| dt | 视频 owner 已提供的适用相邻观察间隔，不按原生交付页重算 |
| n | 拼图中实际显示的格子编号；不因交付分页重新编号或重绘 |
| cell_rect | 拼图上的归一化 [left,top,right,bottom] 边界，基于该张完整拼图；与 crop 同为边界形式，但二者所指画布不同 |
| representative_range | overview 的代表区间，适用时保留；不是逐帧连续观察的覆盖承诺 |

detail 不返回无必要的整图 cell_rect、拼图格子 n 或独立图片数字编号。各 action 的可选索引字段只在视频 owner 有对应实际事实时提供，不填空占位或从图像内容猜测。

删除 video_frames 标记、complete、plan_delivery、observation_position、旧业务 next，以及用于复现旧源续读的 media_version、源位置和选择版本标签。probe_count、maximum_probe_gap、scanned_ranges、observed_cut_times 不作为默认公开字段；有用的实际缺口和诊断归 content，不机械回显整套分析状态，也不返回固定的复制 next 教学。

### 2.3 overview 原生与 JS 示例

原生请求一段稀疏概览：

```text
工具：read_video_frames
参数：
{
  "target": {"kind":"PATH","path":"videos/demo.mp4"},
  "from": 0,
  "to": 10,
  "action": "overview"
}
```

下例假设代表选择得到所示两格，仅示意返回形状，不规定固定采样数量或固定时刻：

```json
{
  "target": {"kind": "PATH", "path": "videos/demo.mp4"},
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "overview：请求 [0,10) 秒，以下为稀疏代表画面。\n"},
    {
      "type": "object",
      "value": {
        "frames": [
          {"n": 1, "t": 0.0, "cell_rect": [0.0, 0.0, 0.5, 1.0], "representative_range": [0.0, 5.0]},
          {"n": 2, "t": 5.0, "cell_rect": [0.5, 0.0, 1.0, 1.0], "representative_range": [5.0, 10.0]}
        ]
      }
    },
    {"type": "image", "value": {"kind": "IMAGE_REF", "image_ref": "image:<概览拼图>"}}
  ]
}
```

一页可交付完整且模型支持图片时，原生投影为：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际调用耗时>

target: {"kind":"PATH","path":"videos/demo.mp4"}

meta:
  status: SUCCESS

content:
  overview：请求 [0,10) 秒，以下为稀疏代表画面。
  {
    "frames": [
      {
        "n": 1,
        "t": 0.0,
        "cell_rect": [
          0.0,
          0.0,
          0.5,
          1.0
        ],
        "representative_range": [
          0.0,
          5.0
        ]
      },
      {
        "n": 2,
        "t": 5.0,
        "cell_rect": [
          0.5,
          0.0,
          1.0,
          1.0
        ],
        "representative_range": [
          5.0,
          10.0
        ]
      }
    ]
  }
  图片引用：{"kind":"IMAGE_REF","image_ref":"image:<概览拼图>"}

tool_result: <本次完整结果的导出 Target>
next: null
```

该页图片由关联 user 图片消息实际投递，object 遵循通用两空格缩进 JSON 文字规则。

JS 直接拿到同一完整业务值，可同时查看索引和输出图片：

```javascript
const result = await tools.read_video_frames({
  target: {kind: "PATH", path: "videos/demo.mp4"},
  from: 0,
  to: 10,
  action: "overview"
});
const indices = result.content.filter(part => part.type === "object");
console.log(result);
```

### 2.4 detail 原生与 JS 示例

```text
工具：read_video_frames
参数：
{
  "target": {"kind":"PATH","path":"videos/demo.mp4"},
  "from": 10,
  "to": 12,
  "action": "detail",
  "crop": [0.1, 0.2, 0.9, 0.8]
}
```

假设三个目标对应下列真实观察，保存的业务值为：

```json
{
  "target": {"kind": "PATH", "path": "videos/demo.mp4"},
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "detail：请求 [10,12) 秒，首、中、结束前三个时间目标。\n"},
    {"type": "object", "value": {"t": 10.0, "visible_range": [10.0, 10.04]}},
    {"type": "image", "value": {"kind": "IMAGE_REF", "image_ref": "image:<A>"}},
    {"type": "object", "value": {"t": 11.0, "visible_range": [11.0, 11.04]}},
    {"type": "image", "value": {"kind": "IMAGE_REF", "image_ref": "image:<B>"}},
    {"type": "object", "value": {"t": 11.96, "visible_range": [11.96, 12.0]}},
    {"type": "image", "value": {"kind": "IMAGE_REF", "image_ref": "image:<C>"}}
  ]
}
```

原生按上述顺序投影为范围说明、每次索引的 JSON 文字及对应图片引用，并投递本页实际图片；target、meta 和时间外壳与 overview 相同。若页面只放得下前一部分，next 是同一冻结结果的 NEXT，不是另一段 detail 查询。完整结果保留三次独立观察，即使其中图片精确相同；跨页不靠上一页图片引用代替实际投递。

例如当前原生页只可交付第一次完整观察，后两次已经保存，返回可为：

```text
target: {"kind":"PATH","path":"videos/demo.mp4"}

meta:
  status: SUCCESS

content:
  detail：请求 [10,12) 秒，首、中、结束前三个时间目标。
  {
    "t": 10.0,
    "visible_range": [
      10.0,
      10.04
    ]
  }
  图片引用：{"kind":"IMAGE_REF","image_ref":"image:<A>"}

tool_result: <整个 detail 结果的导出 Target>
next: <指向后续已保存内容的完整 NEXT Target>
```

后续原生调用只使用 read_file，参数为 {target:<上面返回的完整 NEXT Target>}；不重新传 action、from、to 或 crop，也不再次调用 read_video_frames。

JS 可以把观察与图片放进任意组合，递归图片识别仍有效：

```javascript
const result = await tools.read_video_frames({
  target: {kind: "PATH", path: "videos/demo.mp4"},
  from: 10,
  to: 12,
  action: "detail",
  crop: [0.1, 0.2, 0.9, 0.8]
});
return {inspection: {evidence: result.content}};
```

精确共享继续沿 unified 的既定规则：相同展示变换及固定质量下，仅未烧号 RGB 尺寸和全部像素精确相同时复用图片资产，观察及其时间项仍独立。原生同页可按既有 detail 投影共享视觉载荷，跨页重新投递；Cell 输出的每次图片出现仍独立投影，不扩展成跨输出共享。

### 2.5 motion 原生与 JS 示例

```text
工具：read_video_frames
参数：
{
  "target": {"kind":"PATH","path":"videos/demo.mp4"},
  "from": 20,
  "to": 21,
  "action": "motion"
}
```

完整业务值采用以下顺序，格子数量、时刻及拼图数量均来自现行连续观察与渲染，不由示例固定：

```text
target: <本次视频 PATH>
meta: {status: SUCCESS}
content:
  text：motion：请求 [20,21) 秒，按实际展示时间排列的连续观察。
  object：{frames: [本张拼图的全部格子索引]}
  image：<拼图 A 的 IMAGE_REF>
  object：{frames: [下一张拼图的全部格子索引]}
  image：<拼图 B 的 IMAGE_REF>
  ……直到本次请求范围的全部既定观察和拼图保存完成
```

原生从这份完整结果交付第一页，tool_result 指向整个结果，next 指向未交付后缀；已全部交付才为 null。不能仅生成第一张拼图就返回成功，再让 NEXT 去解码或渲染下一窗口。

原生页面示意如下，索引占位表示按第 2.2 节完整投影的本图 frames 对象；格子不被省略，后续拼图已完整保存：

```text
target: {"kind":"PATH","path":"videos/demo.mp4"}

meta:
  status: SUCCESS

content:
  motion：请求 [20,21) 秒，按实际展示时间排列的连续观察。
  <本张拼图完整格子索引的 JSON 文字>
  图片引用：{"kind":"IMAGE_REF","image_ref":"image:<拼图A>"}

tool_result: <整个 motion 结果的导出 Target>
next: <指向后续已保存内容的完整 NEXT Target>
```

JS 不受原生页长限制，直接得到全部已保存索引与图片引用：

```javascript
const result = await tools.read_video_frames({
  target: {kind: "PATH", path: "videos/demo.mp4"},
  from: 20,
  to: 21,
  action: "motion"
});
console.log({sequence: result.content});
```

### 2.6 冻结 12k／NEXT 与图片公共语义

三个 action 都是先完成本次请求的全部既定证据及保存，再交付原生第一页或返回 JS 值。图像清晰度、选择时刻、裁剪及拼图由视频 owner 冻结，不能因当前模型页余量重新选帧、降低 FPS 或缩图；独立 motion 修订仍延后。

原生按共同 12k 预算顺序分页；object 的 JSON 文字可以拆分，图片不可拆，索引和图片允许跨页，不保存绑定标记、不重排、不复制前页说明。单张超大拼图也适用独占页例外。next 仅是 NEXT Target，原样传给 read_file，不暴露源帧位置或字符偏移；正常首屏和续页的 tool_result 指向同一完整结果。

NEXT 和导出只读取冻结内容，不重新打开视频、重新探测、解码、渲染或验证原文件变版。内部分析窗口和渲染批次不是业务后继；原文件在完整接受后变化或删除，不改变保存结果和已接受观察。真实处理失败仍按实际结算，不能用“完整”掩盖未完成或缺失。

JS 返回完整类型化业务值，不携带原生 tool_result、next 或时间外壳。console.log／return 输出按第 1 章递归识别任意层级 image 节点；不额外设 steer 预算或自动分页。

图片使用全局资产 ID，不与 session／workspace 绑定。模型支持图片则计算实际图片预算并投递；能力不足或单图无法读取时，按第 1 章在原位置投影引用和具体说明，原生 ERROR，steer 其他内容继续交付。不要将失败图片变成已看图事实，不新增资源 cap、恢复任务或另一套视频交付协议。

### 2.7 错误及取消

| 情况 | 公开处理 |
| --- | --- |
| 参数无效、detail 未明确端点或超两秒、crop 无效 | ERROR，content 给简短原因及必要路径，不执行另一种 action 代替 |
| 视频不存在、没有可用视频轨或请求内无可用观察 | ERROR，简短说明，不伪造帧或索引 |
| 实际视频起止与请求存在缺口，但成功取得其余既定观察 | SUCCESS，保留实际图像和明确缺口说明；不把轨道缺口改称分页未读，也不声称连续覆盖整个请求 |
| 解码、渲染或文件绑定失败 | ERROR，说明读取未完成并保留已取得的内容；这份失败业务值完整保存后，照常返回 tool_result 并按 NEXT 分页，不自动重做 |
| 结果保存失败或未确认 | ERROR，说明已知事实，不提供假可读 tool_result／NEXT，不自动重做 |
| 调用取消 | CANCELLED，按现有取消及清理 owner 结算，保留确实存在的失败业务值 |
| 图片模型能力或单图读取失败 | 沿第 1 章和统一投影规则原位说明，不删除其他证据，不重新解码视频 |

失败值可保留可解析的请求 target；已取得且属于真实失败业务值的文字、索引及图片继续完整保存，不能将原生错误页作为 JS 完整值。JS 失败沿既有异常通道，在确有业务值时通过 err.result 取得全部失败内容；没有业务值就不伪造。

例如 detail 请求跨度过大，且错误业务值已经正常保存、完整交付时：

```text
target: {"kind":"PATH","path":"videos/demo.mp4"}

meta:
  status: ERROR

content:
  无法读取 videos/demo.mp4：detail 要求明确的 from／to，且范围不超过 2 秒。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

next:null 只表示这份错误正文已交付完，不证明视频读取成功。整个页面或请求仍无法交付时，沿 unified 的实际未交付位置及保存失败规则处理，不重新执行原工具。

## 3. read_transcript：取得带时间的字幕或语音文字

### 3.1 用途与最简参数

从本地视频或音频取得请求范围内带时间的文字。优先读取内嵌文字字幕；没有可用文字字幕时，复用成功的 ASR 缓存并识别缺失音频。工具不总结、改写或核验字幕与语音是否一致，不对硬字幕做 OCR，也不将未取得文字解释为没有声音。

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PATH Target | 必填 | 本地视频或音频，沿现有文件读取规则解析 |
| from | number | 可选，默认 0 | 包含的起始秒数，支持小数 |
| to | number | 可选，默认媒体结束 | 不包含的结束秒数；媒体时长未知时必填 |

只接受这三个参数，target 放首位；PATH 使用第 1 章的路径形状与解析规则。删除 continuation，不保留别名或后续识别调用参数，也不新增 action、字幕轨、语言、模型、粒度或页长参数。

from／to 必须为有限数值，满足 0 ≤ from < to，不接受布尔、null、NaN 或无穷大；to 不超过已知媒体时长。省略端点在本次执行时解析一次，解析后的业务范围固定，不因交付页大小改变。

```typescript
interface ReadTranscriptArgs {
  target: ViewImagePathTarget;
  from?: number;
  to?: number;
}
```

### 3.2 业务选择、完整执行与 ASR

保留当前字幕轨选择、字幕解码、ASR 成功缓存复用及实际时间粒度。选中可用文字字幕轨后，即使请求区间内没有字幕，也不自动对字幕空白补 ASR；没有可用文字字幕时才走 ASR 路径。缓存与新识别文字均为 asr，不再公开缓存 ID 或逐条标记缓存命中。

删除单次调用累计新增尝试 180 秒 ASR 的限制及对应累计停止逻辑。按本次解析后的完整请求范围取得字幕，或处理全部缺失音频，完整保存业务结果后，才交付原生第一页或返回 JS 值。已有文字放满一页不能阻止后续业务读取或 ASR；不得把旧累计限制改成总时长、总识别次数或另一个隐藏 cap。

实施时必须同时删除三类旧提前返回路径：字幕因 maximum_text_bytes 达到旧展示／结果字节上限而截断并返回源位置；页面装满后停止后续读取或 ASR；开始 ASR 前以最小文字和完整旧 next 能否装页作为执行条件。字幕内部可分批读取，但必须在本次调用中继续取得剩余内容；这些旧页长和字节限制不能换名后继续截断业务结果。

内部可按现有音频提取、实际上传及服务边界分批处理，继续复用缓存和已接受的共享识别工作；这些批次不产生模型可操作的后继。实际权限、取消、超时、文件变化和远端提交结算仍按既有 owner 处理；真实失败如实结束，不为完成范围自动重做结果未确认的 ASR，不增加持久任务、恢复机制或资源 cap。

### 3.3 最简返回值与完整类型

| 字段 | 明确含义 |
| --- | --- |
| target | 实际媒体 PATH；工作区内相对、工作区外绝对 |
| meta.status | SUCCESS／ERROR／CANCELLED |
| meta.source | 已确定的 subtitle 或 asr；失败前尚未确定就省略 |
| meta.language | 当前实际存在的字幕语言等来源信息；没有就省略，不推断或补空值 |
| content | 请求范围说明的 text，随后是按现有时间顺序排列的逐段 object，以及必要诊断文字 |

每段 object 保留 from、to、text；speaker 只在来源实际提供时存在。原片段跨越请求边界时保留 visible_range，表示与请求的交集，不把完整片段的文字按时间比例裁掉，也不伪造片段端点。保留原文字和实际时间粒度，不因展示分页重分句或重算时间。

```typescript
interface TranscriptSegment {
  from: number;
  to: number;
  text: string;
  speaker?: string;
  visible_range?: {from: number; to: number};
}

interface TranscriptSegmentPart {
  type: "object";
  value: TranscriptSegment;
}

interface ReadTranscriptSuccess {
  target: ViewImagePathTarget;
  meta: {
    status: "SUCCESS";
    source: "subtitle" | "asr";
    language?: string;
  };
  content: (ViewImageTextPart | TranscriptSegmentPart)[];
}

interface ReadTranscriptFailure {
  target?: ViewImagePathTarget;
  meta: {
    status: "ERROR" | "CANCELLED";
    source?: "subtitle" | "asr";
    language?: string;
  };
  content: (ViewImageTextPart | TranscriptSegmentPart)[];
}

type ReadTranscriptResult = ReadTranscriptSuccess | ReadTranscriptFailure;
```

删除公开 media_version。现行该字段是文件修改时间 modified_ns 与字节数 size，用于旧源续读核对，不是内部格式版本；实际业务处理期间仍可在内部检查文件变化，但不向模型返回这些属性，也不在 NEXT 中要求模型携带它们。

删除正文顶层 status、entries、complete、旧业务 next，以及逐段 source、position。meta.source 表达本次来源，content 直接承载全部段落；请求范围用简短 text 表达，不并列 requested_range。正常完成不返回 processed_range；失败时将已知处理范围及未完成区间写入 content。

diagnostics、errors、remaining_gaps、message 和 sound_observations 不再单列字段：有用的诊断、失败区间和实际声音观察写入 content 的简短 text。远端提交结果未确认等重要事实必须保留；声音说明只能来自实际音频观察，不能从空文字推导。

### 3.4 原生调用与结果投影

```text
工具：read_transcript
参数：
{
  "target": {"kind":"PATH","path":"videos/demo.mp4"},
  "from": 10,
  "to": 20
}
```

假设取得两条字幕，且本页可完整交付，原生投影如下。object 使用共同的两空格缩进 JSON 文字规则：

```text
target: {"kind":"PATH","path":"videos/demo.mp4"}

meta:
  status: SUCCESS
  source: subtitle
  language: zh

content:
  请求 [10,20) 秒的文字字幕。
  {
    "from": 10.2,
    "to": 12.8,
    "text": "先打开设置。"
  }
  {
    "from": 13.1,
    "to": 15.4,
    "text": "然后选择导出。"
  }

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整结果的导出 Target>
next: null
```

若内容超过一页，仍先保存整个业务结果，原生只交付按 12k 估值装入的顺序前缀，并返回指向未交付后缀的完整 NEXT Target。后续原生调用是 read_file，参数只填 {target:<返回的完整 NEXT Target>}，不再调用 read_transcript 或携带原请求参数推进识别。

### 3.5 JS 完整返回与组合输出

JS 接入同一业务执行与保存路径。下列调用取得完整业务值，不取得原生页面：

```javascript
const result = await tools.read_transcript({
  target: {kind: "PATH", path: "videos/demo.mp4"},
  from: 10,
  to: 20
});
console.log(result);
```

与第 3.4 节对应的 result 为：

```json
{
  "target": {"kind": "PATH", "path": "videos/demo.mp4"},
  "meta": {"status": "SUCCESS", "source": "subtitle", "language": "zh"},
  "content": [
    {"type": "text", "value": "请求 [10,20) 秒的文字字幕。\n"},
    {"type": "object", "value": {"from": 10.2, "to": 12.8, "text": "先打开设置。"}},
    {"type": "object", "value": {"from": 13.1, "to": 15.4, "text": "然后选择导出。"}}
  ]
}
```

脚本也可访问完整段落字段并自行组合：

```javascript
const result = await tools.read_transcript({
  target: {kind: "PATH", path: "videos/demo.mp4"},
  from: 10,
  to: 20
});
const segments = result.content
  .filter(part => part.type === "object")
  .map(part => part.value);
return {segments};
```

JS 不携带 tool_result、next 或原生观察时间外壳，不按 12k 截短数组、文字或对象。console.log／return 按脚本实际组合的结果沿 Cell steer 整条交付，首版不额外设置展示预算或自动分页；也可与其他工具的完整 content 组合，沿共同递归图片投影规则处理其中实际输出的图片。

### 3.6 冻结分页、空结果与错误

正常调用的全部文字及实际业务字段先保存一次；原生按共同 12k 预算顺序分页，长文字及 object 的 JSON 文字可跨页，JS 始终取得完整对象。正文不另存原生页面或 JSON 展示副本。正常首屏及续页的 tool_result 指向同一完整结果，next 只定位该结果尚未交付的位置，内部位置对模型透明。

NEXT、导出和恢复已接受观察只读取冻结结果，不重新打开媒体、读取字幕、探测音轨、查询识别缓存或发起 ASR。原媒体变化、删除或缓存清理不改变已经保存的结果。扩大业务范围须发起新的原工具调用，不能将 NEXT 作为继续识别剩余音频的入口。

| 情况 | 公开处理 |
| --- | --- |
| 范围正常处理完成，但没有文字 | SUCCESS，content 给一句说明；不能据此断言静音。存在实际声音观察时可一并说明 |
| 无可用文字字幕且没有音轨 | ERROR，简短说明路径及原因，不伪造来源或段落 |
| 参数无效、媒体不可读、ASR 未配置或实际处理失败 | ERROR，content 保留原因、必要路径及实际已取得的文字；未完成范围或远端提交不确定性如实说明 |
| 取消 | CANCELLED，沿既有取消及效果结算，保留实际已取得内容 |
| 完整保存或交付失败 | 沿共同失败规则处理，不返回假可读 Target，不自动重做字幕读取或 ASR |

仅有缓存刷新等不影响本次完整结果的诊断时，不因此伪造业务失败。确实存在的失败业务值也完整保存，原生可按 12k／NEXT 交付这份冻结失败结果，next 不意味着能继续完成失败的识别。JS 沿异常通道提供实际存在的完整 err.result；无业务值时不伪造，不增加 partial 状态、meta.error 或错误枚举。

例如识别前缀成功、后续提交结果未确认，且失败业务值已保存并完整交付：

```text
target: {"kind":"PATH","path":"audio/demo.wav"}

meta:
  status: ERROR
  source: asr

content:
  请求 [0,240) 秒的语音文字。
  {
    "from": 0,
    "to": 30,
    "text": "已经取得的开头文字。"
  }
  audio/demo.wav 的 [30,240) 秒未完成：远端 ASR 提交结果未确认，可能已计费；未自动重试。

tool_result: <本次完整失败结果的导出 Target>
next: null
```

next:null 仅表示这份失败结果已交付完，不表示请求范围识别成功。成功的空结果同样使用 text 内容项，例如“该范围内未取得文字；这不证明没有声音或语音。”，不额外返回空 entries 或 sound 状态字段。

## 4. annotate_media：添加时间标注

### 4.1 用途与最简参数

将模型对视频或音频某个时间范围的观察写为用户可阅读、可点击定位的时间标注。保留当前追加语义，不修改媒体文件、播放状态或已有标注，不增加标注修改／删除动作。仅 ROOT 原生调用，不提供 JS API，也不将它作为 Cell 组合结果或图文 steer 的输出入口。

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PATH Target | 必填 | 本地视频或音频，沿现有文件读取规则解析 |
| from | number | 必填 | 包含的起始秒数，支持小数 |
| to | number | 必填 | 不包含的结束秒数，支持小数 |
| text | string | 必填 | 写给用户的标注文字，不能全为空白，可包含换行 |

只接受这四个参数，target 放首位，PATH 沿第 1 章形状和路径规则。from／to 必须为有限数值，满足 0 ≤ from < to ≤ 媒体时长，不接受布尔、null、NaN 或无穷大；媒体时长未知时失败。text 不新增长度 cap 或单行限制。

不要求先通过 read_video_frames、read_transcript 或特定读取入口取得已读资格。标注是模型提交的观察，不由 runtime 判断内容是否正确或自动生成解释。

```typescript
interface AnnotateMediaArgs {
  target: ViewImagePathTarget;
  from: number;
  to: number;
  text: string;
}
```

### 4.2 精简返回值与内部文件绑定

| 字段 | 明确含义 |
| --- | --- |
| target | 实际媒体 PATH；工作区内相对、工作区外绝对 |
| meta.status | SUCCESS／ERROR／CANCELLED |
| content | 成功时一句“已添加时间标注。”；失败或取消时写简短原因、必要路径及已知效果 |

删除公开 media_annotation 标记，用 content 表达已添加。不回传模型刚提交的标注全文、from／to、计数或标注 ID，不增加另一组标注状态。

```typescript
interface AnnotateMediaSuccess {
  target: ViewImagePathTarget;
  meta: {status: "SUCCESS"};
  content: ViewImageTextPart[];
}

interface AnnotateMediaFailure {
  target?: ViewImagePathTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: ViewImageTextPart[];
}

type AnnotateMediaResult = AnnotateMediaSuccess | AnnotateMediaFailure;
```

这些类型描述原生工具的业务结果，不因此生成 JS API。实际观察时间与微秒耗时继续在原生消息顶层并列呈现。

标注文字和时间继续以冻结工具调用参数为权威，不在结果中再复制一遍，也不新建标注正文实体或标注登记表。前端沿现有成功调用与结果的关联读取这些参数，保留原有读取范围及重复副本处理语义。

保留规范绝对文件路径与实际文件修改时间的内部绑定，用来将标注匹配到用户正在查看的那份媒体；复用现有 tool_results.result_facts 保存 canonical_path、file_mtime_ns，与成功标注结果在同一接受事务中保存。不进入公开 meta/content 或只承载公开字段的 public_fields，不要求模型填写或复制。前端通过成功结果及其关联调用取得 from／to／text，不再依赖公开 media_annotation 标记，也不另存标注正文或建立标注表。

这项文件绑定是时间标注的业务关联，不是内部格式版本、读取资格或 NEXT 续读条件；不据此恢复公开 media_version，也不改变前述全局图片资产规则。

### 4.3 原生调用与成功示例

```text
工具：annotate_media
参数：
{
  "target": {"kind":"PATH","path":"videos/demo.mp4"},
  "from": 10.2,
  "to": 15.4,
  "text": "打开设置后选择导出，随后出现格式选择窗口。"
}
```

成功业务值的 content 为 [{type:"text",value:"已添加时间标注。"}]，原生投影如下：

```text
target: {"kind":"PATH","path":"videos/demo.mp4"}

meta:
  status: SUCCESS

content:
  已添加时间标注。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整结果的导出 Target>
next: null
```

成功表示标注按现有接受路径成立，不增加用户已查看或点击的确认承诺。

### 4.4 错误、取消与冻结结果读取

参数无效、媒体不可读、时长未知、范围超出媒体或标注未成功接受时返回 ERROR，原因和必要路径写入 content；调用取消为 CANCELLED，沿现有取消及结算 owner 如实说明已知效果，不将已成立的标注冒充撤回，也不自动重做。

例如已知媒体时长为 60 秒，模型提交的 to 为 80 秒，错误业务值已保存并完整交付时：

```text
target: {"kind":"PATH","path":"videos/demo.mp4"}

meta:
  status: ERROR

content:
  无法添加时间标注：结束时间 80 秒超过 videos/demo.mp4 的媒体时长 60 秒。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

只保留实际可解析的 target；不伪造文件属性或成功标注。结果保存、交付失败时沿共同规则说明真实效果或不确定性，不返回假可读 Target，也不新增错误枚举、partial 状态或重试机制。

结果沿共同 12k／NEXT 规则保存和交付，正常的简短成功回执通常一页即可。NEXT、导出和恢复已接受观察只读取冻结工具结果，不重新执行 annotate_media、不重复添加标注、不检查媒体当前是否仍匹配，也不将原标注 text 当作结果正文重新导出。重新调用 annotate_media 仍是另一次追加操作，不因文字相同自动合并。

## 5. 当前生产实现参考

- [工具参数与现有支持范围](../src/pulsara_agent/capability/builtin_catalog.py)
- [目标解析及本地图片读取](../src/pulsara_agent/tools/builtins/filesystem.py)
- [图片读取、SVG／HTML 渲染及当前原生准入](../src/pulsara_agent/conversation_kernel/tool_runtime.py)
- [图片实际解码验证](../src/pulsara_agent/conversation_kernel/image_validation.py)
- [当前图片引用读取实现](../src/pulsara_agent/conversation_kernel/prompt_storage.py)
- [共同存储接入前的图文内容及图片绑定](../src/pulsara_agent/conversation_kernel/prompt_content.py)
- [当前图文结果的文字投影](../src/pulsara_agent/llm/input.py)
- [当前 Code Mode 工具目录](../src/pulsara_agent/conversation_kernel/tool_surface.py)
- [视频参数及校验](../src/pulsara_agent/tools/builtins/media.py)
- [视频选择及拼图渲染](../src/pulsara_agent/media/frames.py)
- [detail 目标范围](../src/pulsara_agent/media/frames_detail.py)
- [detail 精确图片共享](../src/pulsara_agent/media/frames_detail_render.py)
- [待替换的源续读实现](../src/pulsara_agent/media/frames_paging.py)
- [视频执行和当前图文页面准入](../src/pulsara_agent/media/execution.py)
- [转录业务、累计 ASR 限制及旧结果字段](../src/pulsara_agent/media/transcript.py)
- [待替换的转录源位置与文字分页](../src/pulsara_agent/media/transcript_paging.py)
- [字幕解码与轨道选择](../src/pulsara_agent/media/subtitles.py)
- [已接受 ASR 工作及结算](../src/pulsara_agent/media/transcript_work.py)
- [ASR 实际识别、缓存与失败事实](../src/pulsara_agent/media/transcript_recognition.py)
- [时间标注的成功记录与文件匹配读取](../src/pulsara_agent/conversation_kernel/media_annotations.py)
- [当前标注内部事实与公开正文投影](../src/pulsara_agent/primitives/tool_result_projection.py)

这些链接用于核对实施接点，不表示当前生产代码已经实现本草案。实施须接入前三个工具的 JS 目录及返回声明，视频公开 action 取代 mode，删除源 continuation 和旧结果字段，先完整生成保存再分页；同时切换共同存储、全局图片资产、递归图片识别及逐图失败占位。转录须删除单次累计 180 秒 ASR 限制、展示预算对业务读取的提前截停及公开 media_version，保留实际识别、共享工作、缓存和取消结算。annotate_media 继续仅原生，替换公开成功标记为简短 text，保留调用参数中的标注与内部文件绑定；前端读取适配同一结果事实，不增加重复标注正文或因 NEXT 再次追加。
