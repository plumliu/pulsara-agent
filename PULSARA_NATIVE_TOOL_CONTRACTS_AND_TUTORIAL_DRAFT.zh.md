# Pulsara 原生工具修订与使用教程草案

日期：2026-11-12。

状态：讨论草案，未实施。本文件记录逐个工具已确定的参数、业务返回值、原生交付形状及 Code Mode 用法，也作为后续 bundled skill 的教程底稿。本轮只写文档，不修改工具、数据库、SDK、提示词或 bundled skill。

## 1. 范围与阅读方式

本文件逐步覆盖 Pulsara 原生工具，以及与这些工具对应的 JS API。每个工具应明确读取或操作范围、参数、返回字段、错误含义和可运行的组合方式，并删除不帮助模型理解或继续操作的冗余内容。

当前确定范围：

| 工具与分支 | 本轮覆盖内容 |
| --- | --- |
| read_file，PATH Target | 参数、完整业务值、meta/content 包装、状态、行号投影、原生与 JS 示例 |
| write_file，PATH + content | 创建语义、参数、简短结果、原生与 JS 示例、错误语义 |
| edit_file，PATH Target | 精确唯一替换、整文件替换、空文件规则、diff 返回、原生与 JS 示例、错误语义 |
| find_files，目录 PATH Target | target 在先的最简参数、Target 内容项、无匹配说明、原生与 JS 示例、扫描失败及取消 |
| search_file_content，PATH Target | target 在先的参数、三种输出模式、匹配行及非重叠匹配次数、object 内容项、原生与 JS 示例 |

`read_file` 的 NEXT Target 分支和 `write_file` 的 source 导出分支暂不展开。续读时只需传入运行时返回的完整 Target，不公开或解释内部定位。其他工具按后续讨论逐项补充，不从本节推导尚未确定的业务字段。

本文件与[统一工具结果交付与分页草案](PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)配合使用：该草案定义完整保存、原生 12k 分页、JS 完整返回及 Cell 图文 steer；本文件细化工具本身的合同。这里已明确的工具参数、字段、meta/content 形状及状态表达，以本文件为准。

文中的原生调用与原生消息使用纯文本示意；JavaScript 代码块只表示 Code Mode 脚本，TypeScript 代码块只表示拟议 SDK 类型。示例中的尖括号表示说明占位，不是可直接使用的 Target 或观测值。

## 2. 本轮确定的结果表达

### 2.1 业务结果与原生交付

工具的完整业务值使用以下分工：

| 位置 | 职责 |
| --- | --- |
| target | 实际读取或操作的业务对象；文件工具为解析后的文件，find_files 为搜索目录，search_file_content 为搜索文件或目录 |
| meta | 本次调用状态及工具必要的类型化业务信息 |
| content | 最直接的可读内容，使用有序内容项对象数组 |

不再将业务 JSON 序列化为字符串塞入 `body`，也不再叠加 `pulsara_tool_result` 外壳。meta 中的业务字段保留各自类型，不能把它们全部变成需要脚本重新解析的说明文字。

原生工具消息从同一业务结果投影。正文、业务对象 Target 与 meta 直接呈现；原生页面另外提供完整结果的 `tool_result` Target 和未读内容的 `next`。`tool_result` 用于导出完整结果；`next` 仅用于结果分页。它们不混入 JS 的完整业务值，也不作为 Cell steer 的输出包装。

现有观察信息在原生消息顶层并列呈现：`observed_at_utc` 是实际观察时间，`observation_duration_microseconds` 是实际观察耗时，单位为微秒。二者与 target、meta、content、tool_result、next 同层，不放进 meta、content 或嵌套 observation。未测得耗时不得伪造为 0，仍沿既有观察 owner 表达实际可用事实，不为本次修订增加一套计时机制。

JS 的 `tools.read_file`、`tools.write_file`、`tools.edit_file`、`tools.find_files`、`tools.search_file_content` 成功时直接返回各节定义的完整业务值，不套用原生消息的时间外壳、12k 页预算、tool_result 或 next。数据库内容、JS 值和原生投影来自同一份结果，不保存第二份手工维护的业务正文。

content 的每项统一为 type/value：文字项的 value 是字符串，图片项的 value 是 IMAGE_REF Target，Target 项的 value 是 PATH Target，object 项的 value 是 JSON 对象。find_files 将命中文件保存为 Target 内容项，不并列返回 targets 或重复文字清单；search_file_content 的匹配行和计数使用 object 项。原生工具消息及显式 console.log／return 将 Target 项投影为文字引用，将 object 项投影为通用 JSON 文字，不自动打开、读取或加载其中的文件；只有图片项投影真实图片。普通文字项仍不自动增加分隔符。共同类型见第 2.4 节，投影及准入沿统一交付草案。

这些 PATH 工具不公开文件修订标记，也不接受模型提供的修订条件。读取、创建及结果交付不产生后续编辑资格，不维护已读行区间或通过结果窗口回填资格。edit_file 不要求模型先调用 read_file 或通过特定入口读过全文；文件路径权限与真实交付范围仍各自保持原义。

### 2.2 状态只表达一次

公开调用状态只使用 `meta.status`，枚举固定为 SUCCESS、ERROR、CANCELLED。不再并列输出外层 result_state、顶层 status、正文里的 status:ok 或另一组错误分类字段。

| 状态 | 含义 |
| --- | --- |
| SUCCESS | 本次工具调用成功；不表示模型已看完全部结果 |
| ERROR | 本次调用失败；具体原因、涉及路径及有用提示放在 content |
| CANCELLED | 本次调用已取消，包含派发前取消；实际已发生事实仍如实说明 |

公开状态从真实调用结算结果产生：成功为 SUCCESS，取消为 CANCELLED，其余失败为 ERROR。参数校验、权限、业务效果与取消结算由各工具明确的合同及既有 owner 承担，不把内部分类重新作为公开别名携带。参数或运行时失败与已产生业务结果的失败仍按实际事实处理；没有业务结果时，不为了填满本表而伪造一个业务对象。

状态不兼任交付进度字段。原生页的 SUCCESS 可以伴随非空 next；正常交付完成时的 next:null 不改变原业务状态。业务、保存或交付发生错误时，保留已经确定的业务事实，不用新增多组状态字段代替说明。保存或交付失败沿统一交付草案处理，不重新执行业务。

read_file、write_file、edit_file、find_files、search_file_content 的业务错误沿异常通道返回；在 `catch (err)` 中，err 是 JS 异常对象，`err.result` 是它携带的完整失败业务值，不是枚举或错误代码。确实存在该业务值时，也使用 target/meta/content 结构，meta.status 为 ERROR 或 CANCELLED，全部内容保留且不分页。两个搜索工具的失败业务值包含原因及实际已取得的文件或业务记录，不把它截成程序页。参数或运行时失败若没有业务结果，就不伪造 err.result。不把旧的重复结果 ID、读取指令、可用性布尔值或内部错误分类带回新的公开包装。

### 2.3 错误说明与提示统一进入 content

错误展示使用相同的结果结构，不另造错误正文包装：

| 位置 | 职责 |
| --- | --- |
| meta.status | ERROR 或 CANCELLED，不再细分公开错误枚举 |
| content | 用文字项表达简短、具体的错误原因、涉及路径及有用提示；搜索工具还保留实际已取得的 Target 项或 object 记录 |

删除 meta.error，不增加替代它的公开错误代码字段。具体失败原因直接写入 content，例如说明文件不存在、参数无效、权限不足或执行异常，并写明实际涉及的路径；脚本不依赖另一套 FILE_NOT_FOUND 等公开代码枚举判断分支。文件原文或其他工具真实业务数据中的同名字段不因此被扫描删除。

不再使用 `_hint` 字段，也不把它改名成另一个提示字段。原先 `_hint` 中有用的内容归入 content，可与错误说明合成一个文字项，或按顺序形成多个文字项；需要换行或分隔时由文字项自身提供。删除重复或没有实际帮助的劝告，不要求每次错误都附带操作教程。业务结果不再并列保存一份重复的 message；JS 异常自身的既有 message 仍按异常通道表达，不混作第二份业务正文。

本文 PATH 工具的失败业务值统一只保留 target、meta.status 和 content。文件属性只在各工具明确需要的成功值中作为类型化字段返回；失败时实际已知且有用的文件事实、已发生效果或不确定性写入 content，不在 meta 中保留可选文件属性，不伪造业务结果或已读范围。操作已经完成但保存或交付失败时，说明须保留已发生事实，不能统称为操作没有发生。

### 2.4 共同内容项类型

存储结果、JS 返回值、原生投影及显式 console.log／return 使用同一 type/value 内容项。type 决定如何交付，value 保留真实文字、类型化引用或 JSON 对象：

```typescript
type PathTarget = {
  kind: "PATH";
  path: string;
};

type ImageRefTarget = {
  kind: "IMAGE_REF";
  image_ref: string;
};

type JsonValue = string | number | boolean | null | JsonValue[] | JsonObject;
type JsonObject = {[key: string]: JsonValue};

interface TextPart {
  type: "text";
  value: string;
}

interface ImagePart {
  type: "image";
  value: ImageRefTarget;
}

interface TargetPart {
  type: "target";
  value: PathTarget;
}

interface ObjectPart {
  type: "object";
  value: JsonObject;
}

type ResultPart = TextPart | ImagePart | TargetPart | ObjectPart;
```

各工具声明具体的 content 类型：文件正文、创建说明与 diff 使用 TextPart[]；find_files 使用文字项与 Target 项；search_file_content 按模式声明 object 的精确 value 字段或 Target 项。object.value 的最外层是 JSON 对象，内部允许嵌套对象、数组及 JSON 基本值，不只声明为未知对象，也不新增 match 或 count 内容类型。输入参数中的 target、content 或 MCP 业务字段不因内容项统一而重命名。

Target 项展示引用，图片项交付真实图片。object 的文字投影只特判 value 自身顶层、名称恰好为 target 且符合当前正式 Target 合同的字段：先复用 Target 项的原生引用展示，再将其余字段按确定字段顺序、固定缩进投影为 JSON 文字；不把该 target 递归展开为普通 JSON，也不重复展示它。没有该字段或其值不是正式 Target 时，整个对象使用通用 JSON 投影；其余字段为空时不额外输出空对象。只有投影分开呈现，保存与 JS 返回的 value 仍为原完整对象。

特殊识别仅覆盖顶层精确 target 字段。targets 数组、更深层的 target 或其他相似字段暂按普通 JSON 数据展示，不递归查找或提升为 Target 项；后续遇到具体工具时再讨论是否扩展。嵌套图片引用同样不触发真实图片交付，不从路径或扩展名猜测媒体操作。

object.value 本身是唯一权威业务对象，引用及 JSON 文字按需投影，不再保存一份原始 JSON 备份或独立手写正文。原生按其文字投影的连续顺序进行 12k 分页，跨页片段明确属于正文片段，不冒充完整 JSON 对象；内部定位对模型透明，不生成对象字段游标或程序分片。JS 始终取得完整对象，console.log／return 沿既定 Cell 规则整条投影，不套用原生页预算。

## 3. read_file：PATH Target

### 3.1 用途与范围

读取本地普通 UTF-8 文本文件，可读取全文或指定行范围。原生调用与 `tools.read_file` 使用相同参数和业务范围；原生页面大小不改变读取或保存的范围。

保持当前 PATH 支持：工作区相对路径、绝对路径及 `~` 路径，沿既有路径解析、权限检查和文件 owner 处理。图片、音视频、二进制文件、设备路径和非普通文件不因本次修订成为文本读取对象。UTF-8 识别及 BOM 处理继续复用现有文件 owner，不新建解析或权限机制。

### 3.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PATH Target | 必填 | `{"kind":"PATH","path":"…"}`；path 保持上述路径支持 |
| offset | 非零整数 | 可选，默认 1 | 正数按原文件行号定位；负数从末尾定位，-1 为最后一行、-N 为倒数第 N 行。定位后始终正向读取 |
| limit | 正整数 | 可选，无默认行数上限 | 从解析后的起点最多读取多少行；省略则从该起点读到文件末尾 |

两个可选参数都省略时，从第 1 行读到文件末尾。只填 offset 时，从解析后的起点读到末尾。显式填写 limit 时，本次范围在这段行数处结束；到文件末尾则按实际内容结束。负数只改变起点，不改变阅读方向；不新增 tail 参数。

offset=0 无效。正向 offset 超过文件末尾时返回成功的空正文；负向 offset 超出文件开头时，起点取第 1 行。空文件无论使用正向还是负向合法 offset，均成功返回 content:[]，不伪造行内容。

以 100 行文件为例：

| 参数 | 本次请求范围 |
| --- | --- |
| offset、limit 均省略 | 第 1–100 行 |
| offset=80，limit 省略 | 第 80–100 行 |
| offset=-20，limit 省略 | 第 81–100 行，即最后 20 行 |
| offset=-20，limit=5 | 第 81–85 行，仍按原文顺序向后读 |
| offset=-1，limit 省略 | 第 100 行 |
| offset=-200，limit 省略 | 第 1–100 行 |
| offset=101，limit 省略 | 空正文 |

删除默认 2,000 行、最大 2,000 行等用于旧分页的限制。limit 仍须为正整数，但不设置任意产品上限；实际资源不足明确失败，不静默缩短业务范围或改成后台任务。

PATH 入口只有 target、offset、limit，不接受 continuation 或其子字段。offset/limit 是模型主动选择的业务范围；next 是结果的交付后继，两者不互相替代。

例如，offset=2、limit=2 只读取第 2–3 行。即使文件还存在第 4 行，也不会通过 next 继续扩大读取范围。若这两行本身很长、需要原生分页，next 只指向这两行已经保存的未交付部分。

拟议 SDK 参数声明：

```typescript
interface ReadFilePathArgs {
  target: PathTarget;
  offset?: number; // 非零整数，默认 1；负数从末尾定位，随后仍正向读取
  limit?: number;  // 正整数；省略则读到末尾，无固定行数上限
}
```

### 3.3 成功返回值

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| target | PATH Target | 实际解析后的文件；工作区内使用工作区相对路径，工作区外使用绝对路径 |
| meta.status | SUCCESS | 本次读取成功，仅出现这一处公开调用状态 |
| meta.total_lines | 非负整数 | 整个文件的总逻辑行数，不是当前请求或当前页行数 |
| meta.file_size | 非负整数 | 整个文件的原始字节数，不是本次正文或页面大小；包含原文件 BOM 等实际字节 |
| meta.had_utf8_bom | boolean | 原文件是否带 UTF-8 BOM；成功读取时始终返回 true 或 false |
| content | TextPart[] | 请求范围内完整的文字内容项数组，正文没有 runtime 添加的行号 |

meta 中的文件事实来自同一次读取。局部读取仍返回整个文件的总行数、字节数和 BOM 事实，不将它们改成所选片段的属性。

逻辑行按去除文件开头 BOM 后的正文计算：CRLF 是一个行分隔符，LF 与孤立 CR 也分行，不将其他 Unicode 分隔字符自动视为换行。非空正文中的最后一行即使没有结束符也计为一行；末尾结束符不额外产生一行。读取范围、正负 offset、total_lines 及原生行号投影统一使用此定义，不新增行类型或换行标志字段。

下表的转义表示真实换行字符：

| 正文或文件情况 | total_lines |
| --- | --- |
| 空正文 | 0 |
| 文件仅有 UTF-8 BOM | 0 |
| `a`、`a\n`、`a\r\n` | 1 |
| `\n`、`\r\n`、`\r` | 1 |
| `a\n\n` | 2 |
| `a\rb` | 2 |

因此 `a\n` 的 offset:-1 读取 a 所在的第 1 行；`a\n\n` 的 offset:-1 读取第 2 行空白行，返回的正文仍包含该行原有的换行字符，不定位到文件结束符之后的虚构空行。

成功值、实际存在的失败业务值及 JS 异常携带值的类型形状：

```typescript
type ToolStatus = "SUCCESS" | "ERROR" | "CANCELLED";

interface ReadFilePathFacts {
  total_lines: number;
  file_size: number;
  had_utf8_bom: boolean;
}

interface ReadFilePathSuccess {
  target: PathTarget;
  meta: ReadFilePathFacts & {status: "SUCCESS"};
  content: TextPart[];
}

interface ReadFilePathFailure {
  target: PathTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: TextPart[];
}

type ReadFilePathValue = ReadFilePathSuccess | ReadFilePathFailure;

interface ReadFilePathToolError extends Error {
  result?: ReadFilePathFailure;
}
```

`tools.read_file` 正常完成时返回 ReadFilePathSuccess。走异常通道且确有失败业务值时，err.result 为 ReadFilePathFailure；meta 只含 status，实际已知且有用的文件事实写入 content。没有失败业务值时不提供 result。该声明不要求取消或运行时异常一定产生业务值，也不把失败改成正常返回。

content 是内容项数组，即使本次只有纯文字也不返回裸字符串。文字项之间不自动插入分隔符；JS 可按顺序连接每项 value，取得完整请求正文。空文件或空范围统一返回 `content:[]`，不用伪造一行说明文字或一个空文字项。

不公开 access_scope、workspace_relative 或 position。路径和权限处理仍由既有 owner 承担；原请求行范围和分页行内位置仅供运行时分页及行号投影使用，不作为编辑资格。状态统一使用 meta.status，不带重复 body/observation 外壳。

### 3.4 正文、数据库和行号的边界

数据库中的权威业务正文保存未添加展示行号的 text。JS 直接取得该正文，不在 Code Mode 返回阶段扫描、剥离或恢复行号。文件本身恰好以 `2|` 等字符开头时，这些字符属于正文，必须保留。

原生交付时，在本页已经确定后，为实际交付的文件文字添加 `原文件行号|正文` 展示前缀。行号来自本次请求解析后的原文件起始行、正文换行和运行时的内部页面位置，不从重新读取文件得到，也不依赖模型计算公开位置字段。读取局部范围不从 1 重新编号；负向 offset 同样显示真实正向行号，跨页继续显示原文件行号。超长单行跨页时仍归属同一原文件行，不伪造成下一行。

```text
读取请求范围
  → 完整保存未加行号的正文、meta 与原请求关联
  → JS：返回完整 content 数组，不加行号
  → 原生：按 12k 确定本页，投影时添加原文件行号
```

12k 产品页预算统一按数据库中已接受的规范业务结果内容使用 v3 估计，包括本页选定的正文和保存的业务字段，不按数据库实际序列化字节计量。原生投影添加的行号、观察信息、读取引用与消息封装不计入这项预算，页边界确定后再添加展示行号。实际 provider 请求准入仍检查最终发送形状，不将展示差异重新变成产品分页字段或预算。不得先存带行号的正文，再为 JS 或导出剥离展示字符；也不另存一份权威的带行号正文。

既有对话及 provider 输入记录仍按原机制冻结实际交付的消息。它们保留带行号的实际投影，不意味着另一份业务正文成为权威。不能为了改显示行号重写同一 epoch 已安装的 provider 输入前缀。

完整入库不等于 ROOT 已看完整个文件。JS 取得完整业务值也不等于 ROOT 已见全部正文；只有实际交付的内容可作为模型已见依据。这一区别不产生编辑前置资格或已读行区间账本。

正文保存与 JS 返回保留请求范围内原有的行结束符和末尾换行，不统一 CRLF/LF，也不擅自补上或删除末尾换行。BOM 继续按既有文件 owner 解析，由 meta.had_utf8_bom 表达；file_size 覆盖整个原文件的实际字节。原生行号仅是显示前缀，不修改已保存的正文。

**反例：不能把不同原文折叠成相同正文。** 下表中的转义写法表示实际换行字符，不是文件里字面的反斜杠。旧展示路径将这三种文件都变成 `1|a\n2|b`，因此既不能将该投影存成权威正文，也不能仅剥掉行号后声称获得了原文：

| 原文件解码文字 | 应保存并返回给 JS 的正文文字 | 必须保留的区别 |
| --- | --- | --- |
| `a\r\nb\r\n` | `a\r\nb\r\n` | CRLF，存在末尾换行 |
| `a\nb\n` | `a\nb\n` | LF，存在末尾换行 |
| `a\nb` | `a\nb` | LF，不存在末尾换行 |

例如读取第一种文件全文时，content 为 `[{"type":"text","value":"a\r\nb\r\n"}]`。选择部分行时也保留实际选定行在源文件中的行结束符；文件末行原本没有换行就不添加。无需新增 newline 或尾部换行标志来恢复这些区别，正文自身保留它们。

### 3.5 原生分页与 JS 返回

本次请求范围先完整读取并保存，再决定交付入口：

- 原生调用：按统一 12k 预算交付，正文显示行号；正常页面保留 tool_result 和 next。
- JS 调用：成功时直接返回本次范围的完整 ReadFilePathSuccess；有失败业务值时经 err.result 提供 ReadFilePathFailure。二者都不分页，不带展示 next 或原生工具结果外壳。
- console.log / 顶层 return：仅交付脚本选择或组合的内容，沿 Cell steer 规则整条交付，不自动加原生 read_file 的行号、meta、tool_result 或 next。

`next:null` 只表示本次请求范围的保存结果已正常交付完，不表示文件已读到末尾，也不表示所有文件内容已经检查。是否读取了全文由调用范围和实际正文决定。

大文件可以内部流式处理或分块保存，但 JS 正常返回仍是完整业务值，不是第一页、读取句柄或懒加载代理。原生分页只读取这次已保存的正文，不重新打开源文件；原文件后来变化不改写该份结果。

负向 offset 仅在本次读取时，按该次文件的实际总行数解析一次并确定请求范围；next 继续读取这份固定保存结果，不重新按文件末尾定位，也不随文件后续增长或缩短移动范围。

### 3.6 教程：读取局部范围

假设工作区中 demo.txt 的字节为 `alpha\nbeta\ngamma\ndelta\n`，总计 4 行、23 字节，没有 UTF-8 BOM。

原生调用：

```text
工具：read_file
参数：
  target: {"kind":"PATH","path":"demo.txt"}
  offset: 2
  limit: 2
```

原生返回的文本示意；时间与耗时仅为示例数值：

```text
observed_at_utc: 2026-11-12T02:00:00.000000Z
observation_duration_microseconds: 1200

target: {"kind":"PATH","path":"demo.txt"}

meta:
  status: SUCCESS
  total_lines: 4
  file_size: 23
  had_utf8_bom: false

content:
  2|beta
  3|gamma

tool_result: <完整结果的导出 Target>
next: null
```

这里只读了第 2–3 行。next 为 null 是因为本次两行结果已经全部交付，不能据此声称看过第 1、4 行。

同一请求的 JS 调用：

```javascript
const result = await tools.read_file({
  target: {kind: "PATH", path: "demo.txt"},
  offset: 2,
  limit: 2
});
```

JS 完整返回值示意：

```json
{
  "target": {"kind": "PATH", "path": "demo.txt"},
  "meta": {
    "status": "SUCCESS",
    "total_lines": 4,
    "file_size": 23,
    "had_utf8_bom": false
  },
  "content": [
    {"type": "text", "value": "beta\ngamma\n"}
  ]
}
```

JS 访问字段和计算：

```javascript
const text = result.content.map(part => part.value).join("");
console.log(text.includes("gamma"));
```

该日志仅输出脚本计算出的布尔值，不自动将整份文件结果交给 ROOT。

显式把选定正文交给 ROOT：

```javascript
console.log({
  content: [
    {type: "text", value: "下面是 demo.txt 的第 2–3 行：\n"},
    ...result.content
  ]
});
```

这是一条脚本组合的 Cell steer。runtime 保留数组顺序，不额外插入分隔符，也不自动补上行号或文件 meta。若脚本决定在成功结束时输出，可改用 `return {content: result.content}`，无需同时 log 和 return 同一份材料。

### 3.7 教程：全文、尾部与大范围读取

原生全文请求：

```text
工具：read_file
参数：
  target: {"kind":"PATH","path":"demo.txt"}
```

该请求先取得全文，不因首屏最多 12k 而只保存首屏内容。原生未读完时提供 next；这里不展开 NEXT 的参数或内部位置。

JS 全文处理：

```javascript
const result = await tools.read_file({
  target: {kind: "PATH", path: "demo.txt"}
});

const text = result.content.map(part => part.value).join("");
console.log(text.includes("delta"));
```

即使全文超过原生页预算，JS 仍处理完整正文；只有脚本显式输出的内容进入 ROOT。

读取前例 demo.txt 的最后两行，原生参数为：

```text
工具：read_file
参数：
  target: {"kind":"PATH","path":"demo.txt"}
  offset: -2
```

该文件共 4 行，因此这次范围是第 3–4 行，原生 content 展示为：

```text
3|gamma
4|delta
```

这里只摘出正文投影；完整原生页面仍包含 target、meta、观察信息、tool_result 和 next。meta.total_lines 仍为 4，file_size 仍为 23；负向起点不改变这些完整文件属性，也不将行号显示成负数。

对应的 JS 读取及显式输出：

```javascript
const result = await tools.read_file({
  target: {kind: "PATH", path: "demo.txt"},
  offset: -2
});
console.log({content: result.content});
```

JS 的 result.content 为未添加行号、保留末尾换行的数组：

```json
[
  {"type": "text", "value": "gamma\ndelta\n"}
]
```

若在上述请求中再指定 limit:1，只读取第 3 行 gamma，不倒序读取最后一行，也不通过 next 扩大到第 4 行。需要最后一行时直接指定 offset:-1。

需要超过 2,000 行的明确业务范围时可以直接指定，例如：

```javascript
const result = await tools.read_file({
  target: {kind: "PATH", path: "large.txt"},
  offset: 3001,
  limit: 5000
});
```

该请求选择第 3001–8000 行，与原生页面数无关；文件不足该范围时只返回实际存在的内容。

### 3.8 业务错误示例

文件不存在时，meta.status 为 ERROR，具体原因与路径直接写进 content，检查路径的提示也放进 content。不返回 meta.error 或 `_hint`，不伪造 total_lines、file_size 或 BOM 事实。

原生错误正文的文本示意：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际可用耗时>

target: {"kind":"PATH","path":"missing.txt"}

meta:
  status: ERROR

content:
  Cannot read missing.txt: the file does not exist.
  Check the path.

tool_result: <完整错误结果的导出 Target>
next: null
```

该示意假定业务错误结果已完整保存、整条正常交付，因此同样返回 tool_result 和 next:null；这不表示文件读取成功。错误正文较长时仍可正常分页，保存或交付本身失败则沿统一草案报告真实情况，不能冒充正常交付完成。错误说明和提示按第 2.3 节表达，不增加第二个错误分类字段。

JS 调用抛出既有 ToolError；在 catch (err) 中，确有完整失败业务值时，err.result 形如：

```json
{
  "target": {"kind": "PATH", "path": "missing.txt"},
  "meta": {
    "status": "ERROR"
  },
  "content": [
    {"type": "text", "value": "Cannot read missing.txt: the file does not exist.\n"},
    {"type": "text", "value": "Check the path."}
  ]
}
```

脚本可检查失败值的 meta.status，或显式输出完整失败说明，不靠解析原生投影恢复类型，也不依赖已删除的公开错误代码。例如：

```javascript
try {
  await tools.read_file({
    target: {kind: "PATH", path: "missing.txt"}
  });
} catch (err) {
  if (!err.result) throw err;
  console.log({content: err.result.content});
}
```

该例只把实际取得的完整失败正文交给 ROOT。错误处理不自动创建 missing.txt，也不自动重新读取；后续行为由脚本或模型依据用户任务决定。

### 3.9 实施核对点

以下是采用本合同后的行为验证要求，本轮未实施或运行验证：

- 同一 PATH 请求的完整保存范围、JS 结果和原生逐页正文一致，原生 12k 不提前裁剪生产者返回值。
- 默认第 1 行至末尾、正向和负向 offset、显式有限行范围、跨 EOF、空文件和超长单行均符合本节范围语义；超过 2,000 行的合法请求可执行。不新增 tail 参数。
- offset=0 被拒绝；-1 定位最后一行，负向超出开头从第 1 行开始，正向超出末尾返回空正文；负向起点配合 limit 仍按原文顺序向后读取。
- 负向起点只按本次文件解析一次；原生显示真实行号，next 读取固定保存结果，不因文件后来增长或缩短重新定位。
- total_lines、读取范围、正负 offset 和原生行号采用同一逻辑行定义；空正文及 BOM-only 为 0 行，末尾结束符不产生额外一行，CRLF、LF、孤立 CR 均正确处理。
- JS content 始终为未加行号的文字项数组，可直接访问、连接和参与显式图文组合；业务字段有精确 SDK 声明。
- 原生局部范围及跨页行号来自原文件位置，行内切页不制造新行；文件本身的数字竖线前缀不会被剥离。
- 权威业务正文没有展示行号；12k 基于数据库规范结果内容估计，不计原生投影行号、观察信息、读取引用及封装增量。实际 provider 准入和既有输入前缀连续性保持。
- CRLF、LF、末尾有／无换行在保存正文及 JS 返回中可区分；局部范围保持源行结束符，空文件和空范围使用 content:[]。
- 总行数、原始字节数和 BOM 布尔值反映同一次文件读取；无公开文件修订字段，不维护或授予后续编辑的已读资格。分页与行号投影只依赖已保存内容和原请求范围。
- 文件缺失、非普通文件、不支持的编码、参数拒绝、运行时错误和取消沿既有边界结算；仅真实存在的完整失败业务值进入 err.result，不分页、不重执行业务。失败值仅有 target、meta.status、content，已知且有用的文件事实写入 content，不返回可选文件属性。
- 公开 meta.status 仅使用 SUCCESS、ERROR、CANCELLED；具体错误原因、路径和有用提示进入 content。原生与 JS 使用同一完整失败业务值，不带 meta.error、替代错误代码字段、`_hint` 或重复业务 message。

## 4. write_file：PATH + content

### 4.1 用途与范围

在指定 PATH 创建新的普通 UTF-8 文本文件。原生调用与 `tools.write_file` 使用相同参数和创建语义：成功后返回简短的创建说明及必要事实，不回传写入全文。

保持现有 PATH 支持和权限边界：工作区相对路径、绝对路径及 `~` 路径仍由既有 owner 解析和授权。允许写入的目标可按现有方式创建缺失的父目录；支持绝对路径不等于无条件允许工作区外写入。

write_file 只创建新文件。目标已存在时拒绝，不覆盖、不追加；普通文件、目录、符号链接和悬空符号链接均不能被替换。并发期间目标出现时同样拒绝，继续复用现有原子不覆盖发布机制。不增加 overwrite、append 或其他模式参数。

本节只定义 PATH + content，不定义 source 导出输入及续读行为。

### 4.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PATH Target | 必填 | `{"kind":"PATH","path":"…"}`；指定要创建的新文件 |
| content | string | 必填，无默认值 | 完整的待写入文本；空字符串表示创建空文件 |

输入 content 是字符串；返回值的 content 是内容项对象数组。二者承担不同职责，不要求为了统一名称把写入参数也改成数组。

```typescript
interface WriteFilePathContentArgs {
  target: PathTarget;
  content: string;
}
```

文本按 UTF-8 编码写入，保留 CRLF、LF 和末尾有／无换行，不进行换行归一化，也不自动补换行或 BOM。若字符串本身以 U+FEFF 开头，该字符照常编码写入。空字符串实际写入 0 字节。

读写衔接时须注意：read_file 将文件开头的 UTF-8 BOM 解析为 meta.had_utf8_bom，正文不包含被解析的开头 BOM。直接连接其 content 文字并传给 write_file，不会自动恢复该 BOM，因此不保证复制出的文件与原文件逐字节相同。需要保留时，由脚本根据该事实显式恢复开头的 U+FEFF；不新增工具参数或隐式补 BOM 行为。

沿用现有普通文本边界：设备路径、已识别的二进制扩展名、含 NUL 的内容及无法编码为 UTF-8 的字符串不接受。不用原生 12k 页预算限制输入字符串大小，也不因展示余量不足缩短写入内容；实际资源不足如实失败。

### 4.3 成功返回值与类型

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| target | PATH Target | 实际创建的文件；工作区内使用相对路径，工作区外使用绝对路径 |
| meta.status | SUCCESS | 本次文件创建成功 |
| meta.bytes_written | 非负整数 | 此次实际写入的字节数；包含输入自身的 BOM 字节，空文件为 0 |
| content | TextPart[] | 简短的创建说明，可包含路径；不回传写入全文 |

删除 files_modified：单文件创建的目标已由 target 表达，不再重复提供一个路径数组。不增加回显全文、行数或其他非必要属性。

```typescript
interface WriteFilePathSuccess {
  target: PathTarget;
  meta: {status: "SUCCESS"; bytes_written: number};
  content: TextPart[];
}

interface WriteFilePathFailure {
  target: PathTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: TextPart[];
}

type WriteFilePathValue = WriteFilePathSuccess | WriteFilePathFailure;

interface WriteFilePathToolError extends Error {
  result?: WriteFilePathFailure;
}
```

`tools.write_file` 正常完成时返回 WriteFilePathSuccess。走异常通道且确有完整失败业务值时，err.result 为 WriteFilePathFailure；meta 只含 status，实际已知的写入事实进入 content。失败值不返回 bytes_written，不从计划写入的字符串计算属性冒充实际写入事实，也不默认填写 bytes_written:0。

### 4.4 原生交付与 JS 返回

完整业务结果保存的是创建说明、target 和 meta，不把输入全文复制进结果正文。完整结果的 tool_result Target 因而指向这份创建结果，不等同于目标文件的全文。

原生结果沿统一 12k 规则分页，另加顶层观察信息、tool_result 和 next。简短创建说明通常可整条交付，此时 next:null。创建说明及错误说明不是原文件正文，不添加 read_file 专用的行号。

删除现有创建前按展示预算检查创建结果能否装入一页、装不下就拒绝创建的路径。业务执行遵守原有参数、权限、实际资源和原子创建边界；创建完成后再处理结果保存与交付。后续保存或交付失败须保留已发生的创建事实，不把展示失败说成文件未创建，也不自动重做创建。

JS 直接返回完整类型化创建结果，不分页、不附带原生时间外壳、tool_result 或 next。脚本通过 console.log／return 显式选择交给 ROOT 的内容；runtime 不自动回显输入文本，也不把中间调用结果自动加入 steer。

### 4.5 教程：创建文本文件

假设 notes/demo.txt 尚不存在。原生调用参数：

```text
write_file
  target: {"kind":"PATH","path":"notes/demo.txt"}
  content: "alpha\nbeta\n"
```

成功后的原生结果示意：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际可用耗时>

target: {"kind":"PATH","path":"notes/demo.txt"}

meta:
  status: SUCCESS
  bytes_written: 11

content:
  已创建 notes/demo.txt。

tool_result: <完整创建结果的导出 Target>
next: null
```

对应的 Code Mode 调用：

```javascript
const result = await tools.write_file({
  target: {kind: "PATH", path: "notes/demo.txt"},
  content: "alpha\nbeta\n"
});
console.log({content: result.content});
```

这里是同一次创建的两种调用方式示例，不能先执行原生示例再在同一路径执行 JS 示例并期待再次创建成功。JS 的 result 形如：

```json
{
  "target": {"kind": "PATH", "path": "notes/demo.txt"},
  "meta": {
    "status": "SUCCESS",
    "bytes_written": 11
  },
  "content": [
    {"type": "text", "value": "已创建 notes/demo.txt。"}
  ]
}
```

脚本可直接使用 result.meta.bytes_written 或组合 result.content。上例只向 ROOT 输出创建说明；不会隐式附加 alpha、beta 或原生页面字段。若任务需要输出原输入，应由脚本显式选择。

创建空文件时同样填写 content，只是值为 `""`；成功返回 bytes_written:0 和简短创建说明，不把空输入解释为缺少参数。

### 4.6 错误与取消

错误使用第 2 节的同一合同：meta.status 仅为 ERROR 或 CANCELLED，原因、路径和确有帮助的提示进入 content，不返回 meta.error、`_hint` 或另一组公开错误代码。

例如 notes/demo.txt 已存在时，原生结果示意：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际可用耗时>

target: {"kind":"PATH","path":"notes/demo.txt"}

meta:
  status: ERROR

content:
  无法创建 notes/demo.txt：目标已存在，未覆盖。
  如需修改已有文件，请使用 edit_file。

tool_result: <完整错误结果的导出 Target>
next: null
```

该示例假定完整错误结果保存及交付均成功。next:null 只表示错误结果已交付完毕，不表示文件创建成功。所有失败值的 meta 都只含 status；即使已知部分写入事实，也只在 content 中说明，不返回 bytes_written。

对应的完整 JS 失败业务值留在异常的 err.result 中：

```json
{
  "target": {"kind": "PATH", "path": "notes/demo.txt"},
  "meta": {"status": "ERROR"},
  "content": [
    {"type": "text", "value": "无法创建 notes/demo.txt：目标已存在，未覆盖。\n"},
    {"type": "text", "value": "如需修改已有文件，请使用 edit_file。"}
  ]
}
```

```javascript
try {
  await tools.write_file({
    target: {kind: "PATH", path: "notes/demo.txt"},
    content: "alpha\nbeta\n"
  });
} catch (err) {
  if (!err.result) throw err;
  console.log({content: err.result.content});
}
```

不把所有失败都说成“文件未创建”。沿现有创建 owner 的实际结算事实说明：确定未创建时才说未创建；已经发布但后续确认失败时，说明文件已发布而完成无法确认；无法确定时，说明目标可能已创建，并提示先检查路径再决定是否重试。父目录已创建等已知效果也不能被否认。使用相同的 ERROR 和说明文字即可，不增加不确定状态、发布标记或重试任务。

write_file 与 edit_file 使用同一提交边界结算取消：提交前取消且确认目标文件未创建时，返回 CANCELLED；已创建父目录等已知效果仍如实写入 content。进入不可安全中断的提交阶段后，须等待实际创建结算，不能先返回取消、再让后台创建继续生效。创建完成且结果正常产生时返回 SUCCESS；后到的取消不将已完成创建改报为未执行，可停止后续工作。

实际写入、确认、结果保存或交付失败时返回 ERROR，content 保留创建事实或不确定性，不自动重新写入，也不把它伪装为普通的“目标未创建”错误。参数或运行时失败没有业务结果时，不伪造 err.result；结果的保存和交付失败沿统一草案处理，不增加公开状态或并列结果字段。

### 4.7 实施核对点

以下是采用本合同后的行为验证要求，本轮未实施或运行验证：

- PATH + content 的原生与 JS 参数一致；空字符串可创建空文件，缺少 content 仍是参数错误。
- 保持路径授权及原子不覆盖创建；已有普通文件、目录、符号链接、悬空链接和并发出现的目标均不被替换。
- 实际 UTF-8 字节保留 CRLF、LF、末尾换行及显式 U+FEFF；bytes_written 与实际字节一致，无公开文件修订字段。
- read_file 正文不含被解析的开头 BOM，write_file 不隐式恢复 BOM；脚本显式恢复时按实际字符串编码写入。
- 成功结果只有 target、必要 meta 和简短 content；不带 files_modified，不回传输入全文，不为说明添加行号。
- JS 成功值及 err.result 保持完整类型；原生增加顶层观察和交付引用，12k 只约束结果页面，不约束写入业务范围。
- 删除创建前的展示预算预检；不能因创建结果装不进一页而拒绝业务创建，后续保存或交付失败如实保留创建事实。
- 错误及取消只用三个公开状态中的相应值；失败值仅有 target、meta.status、content，已知写入事实进入 content，不带可选文件属性，不否认已知副作用，不自动重做创建，不维护或授予后续编辑的已读资格。
- 取消按与 edit_file 相同的提交边界结算：提交前确认未创建时 CANCELLED，不可安全中断的提交等待实际结算；创建成功且结果正常产生时 SUCCESS，实际写入或后续保存、交付失败时 ERROR，保留已知效果或不确定性。

## 5. edit_file：PATH Target

### 5.1 用途与范围

修改现有普通 UTF-8 文本文件。工具提供局部精确替换和整文件替换两个互斥参数分支，不增加 mode 字段。目标不存在时失败；创建新文件使用 write_file。

PATH 的相对路径、绝对路径、`~` 解析及写入授权复用既有文件 owner。编辑不要求公开修订值，不检查模型是否调用过 read_file、是否读过相关行或是否读完全文。模型通过 terminal 等其他工具取得正文时也不需要登记读取资格。

目标必须是当前权限允许的现有普通 UTF-8 文本文件。沿现有文本文件边界拒绝设备、不支持的二进制类型、无效 UTF-8 及含 NUL 的正文或输入；不通过有损解码或重新编码隐式接受二进制内容。

### 5.2 最简参数表

| 分支 | 参数 | 类型 | 必填与含义 |
| --- | --- | --- | --- |
| 共用 | target | PATH Target | 必填，指定现有文件 |
| 局部替换 | old_text | string | 必填，要在当前正文中精确且唯一匹配的原文 |
| 局部替换 | new_text | string | 必填，替换文字；空字符串表示删除匹配片段 |
| 整文件替换 | content | string | 必填，新全文；空字符串表示清空现有文件 |

局部替换只接受 target、old_text、new_text；整文件替换只接受 target、content。两个分支不能混填，也不接受额外字段。整文件替换无需传入旧全文，不将新全文与旧文件做子串定位。

```typescript
interface EditFileTextArgs {
  target: PathTarget;
  old_text: string;
  new_text: string;
  content?: never;
}

interface EditFileWholeArgs {
  target: PathTarget;
  content: string;
  old_text?: never;
  new_text?: never;
}

type EditFilePathArgs = EditFileTextArgs | EditFileWholeArgs;
```

`?: never` 仅在 SDK 类型声明中排除另一分支的字段，不是新增的工具参数，不进入模型参数表，也不表示可传 null 或 undefined。工具参数 schema 与运行时仍按两个闭合分支拒绝混填及额外字段；字段存在性与类型检查不能只依赖 TypeScript 声明。

输入 content 是新全文字符串，返回值的 content 是内容项数组；两者不混用。

### 5.3 精确匹配与空文件规则

非空 old_text 必须在当前正文中恰好出现一次。匹配逐字符进行，大小写、空白、真实换行和标点均属于匹配条件；不剥离行号，不做模糊匹配或自动归一化定位。找不到或存在多个匹配时失败，不选择第一处，也不批量替换。出现位置按每个可能起点判断，包括相互重叠的匹配。

例如当前正文为 `aaa`，old_text 为 `aa`，存在两个匹配起点，必须拒绝。需要定位重复内容中的某一处时，由调用者在 old_text 和 new_text 中保留足够的上下文，使匹配唯一。

old_text 为空字符串时，仅允许当前正文确实为空的现有文件；此时用 new_text 填充正文。全空白正文不等于空正文，目标不存在也不能通过空 old_text 隐式创建。该规则不产生读取资格或文件版本条件。

局部替换可以表达替换、删除、前插和后插，无需不同操作枚举。以下例子均以独立的原正文 `A\nB\nC\n` 为起点，转义表示实际 LF：

| 意图 | old_text | new_text | 结果 |
| --- | --- | --- | --- |
| 替换 B 行 | `B\n` | `X\n` | `A\nX\nC\n` |
| 删除 B 行 | `B\n` | 空字符串 | `A\nC\n` |
| 在 B 前插入 X | `B\n` | `X\nB\n` | `A\nX\nB\nC\n` |
| 在 B 后插入 X | `B\n` | `B\nX\n` | `A\nB\nX\nC\n` |

替换文字须包含希望保留或删除的分隔符；runtime 不额外吞掉相邻换行或自动补末尾换行。原生 read_file 的行号仅供阅读，不属于匹配原文。

### 5.4 文本保真与无变化

局部替换的匹配正文与 read_file 返回的正文一致：仅将原文件开头的 UTF-8 BOM 解析为文件标记，正文中的其他字符保持原样。原文件 BOM 在局部编辑后保留；仅有 BOM 的文件正文为空，可用空 old_text 填充，且保留原 BOM。

局部替换保留未修改部分的 CRLF、LF、孤立 CR 和末尾有／无换行。new_text 按字面插入，不剥离其展示字符、不进行空白或 Unicode 规范化、不额外吞掉相邻换行，也不将全文件换行统一成某种风格。读取正文可以直接用于构造匹配，不需要从显示行号或归一化文本中恢复原文。

整文件替换的 content 与 write_file 的输入规则一致：它就是完整的新文件文字，直接按 UTF-8 编码，不自动继承旧 BOM，不统一换行，也不补末尾换行。需要 BOM 时由 content 显式包含开头 U+FEFF；空 content 产生零字节文件，即使原文件只有 BOM 也会清空。整文件替换与局部替换对 BOM 的分工不通过额外参数表达。

无实际变化统一返回 SUCCESS 和 content:[]，不写盘，不因本次操作更新文件修改时间，不增加 changed、no_op 或操作计数字段。必须先完成参数、目标、权限及适用的匹配检查，再判断生成的完整文件字节是否与当前字节相同；不能在检查前仅凭 old_text 等于 new_text 就直接成功。

| 情况 | 结果 |
| --- | --- |
| old_text 恰好匹配一次，new_text 与其相同，最终字节不变 | SUCCESS，content:[]，不写盘 |
| old_text 未找到或不唯一，即使 old_text 与 new_text 相同 | ERROR，说明匹配问题和路径 |
| 现有空正文，old_text 与 new_text 均为空 | SUCCESS，content:[]，不写盘；已有 BOM 保留 |
| 整文件 content 编码后的字节与当前文件相同 | SUCCESS，content:[]，不写盘 |
| 整文件新旧正文相同，但 BOM 或换行等实际字节不同 | 执行实际修改，SUCCESS 时返回对应 diff |

### 5.5 返回值与交付

| 字段 | 成功 | 失败或取消 |
| --- | --- | --- |
| target | 实际文件 Target，工作区内相对路径、工作区外绝对路径 | 实际涉及的文件 Target |
| meta.status | SUCCESS | ERROR 或 CANCELLED |
| content | TextPart[]，完整的本次 unified diff；无实际变化为 [] | TextPart[]，简短原因、路径及确有帮助的提示或已知效果 |

成功不再并列返回 diff 字段、修改后窗口、窗口截断标记、操作计数或重复路径数组。diff 就是 content 的文字，不再另外提供一份修改说明或回显编辑后的全文。

diff 来自实际修改前后的内容，不能通过换行或 BOM 归一化把真实变化隐藏成空 diff。末尾无换行时使用标准的 `\ No newline at end of file` 标记，准确表达添加或删除末尾换行；不能只比较去除行结束符后的逻辑行。无变化使用空数组，不返回空文字项或伪造 diff。

```typescript
interface EditFilePathSuccess {
  target: PathTarget;
  meta: {status: "SUCCESS"};
  content: TextPart[];
}

interface EditFilePathFailure {
  target: PathTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: TextPart[];
}

type EditFilePathValue = EditFilePathSuccess | EditFilePathFailure;

interface EditFilePathToolError extends Error {
  result?: EditFilePathFailure;
}
```

完整业务结果保存 target、meta 和完整 diff 内容，原生与 JS 从同一结果产生。原生按 12k 交付，保留顶层观察信息、tool_result 和 next；JS 正常返回完整 EditFilePathSuccess，不分页、不带原生交付包装。diff 自身的 hunk 行号和 `+`、`-`、上下文前缀属于 diff 内容，原生不再添加 read_file 的行号前缀。

编辑不能因 diff 或结果说明装不进一页而在执行前被拒绝。结果先完整保存，原生再分页；失败沿统一保存与交付规则报告，不自动重新编辑文件。

### 5.6 教程：局部替换

假设现有 demo.txt 的正文为 `alpha\nbeta\ngamma\n`。原生调用：

```text
工具：edit_file
参数：
  target: {"kind":"PATH","path":"demo.txt"}
  old_text: "beta\n"
  new_text: "BETA\n"
```

成功后的原生结果示意：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际可用耗时>

target: {"kind":"PATH","path":"demo.txt"}

meta:
  status: SUCCESS

content:
  --- a/demo.txt
  +++ b/demo.txt
  @@ -1,3 +1,3 @@
   alpha
  -beta
  +BETA
   gamma

tool_result: <完整编辑结果的导出 Target>
next: null
```

同一编辑的 JS 调用方式：

```javascript
const result = await tools.edit_file({
  target: {kind: "PATH", path: "demo.txt"},
  old_text: "beta\n",
  new_text: "BETA\n"
});
console.log({content: result.content});
```

两种调用示例应分别从上述原正文开始，不是在同一文件上连续执行。JS 的完整 result 形如：

```json
{
  "target": {"kind": "PATH", "path": "demo.txt"},
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "--- a/demo.txt\n+++ b/demo.txt\n@@ -1,3 +1,3 @@\n alpha\n-beta\n+BETA\n gamma\n"}
  ]
}
```

脚本可以处理或组合 result.content。只有显式 console.log／return 选择的内容进入 ROOT，不自动附加新全文、窗口、读取资格或原生页面字段。

### 5.7 教程：整文件替换与空文件填充

替换现有 demo.txt 的全部正文，不传旧全文：

```text
工具：edit_file
参数：
  target: {"kind":"PATH","path":"demo.txt"}
  content: "replacement\n"
```

对应的 JS 方式：

```javascript
const result = await tools.edit_file({
  target: {kind: "PATH", path: "demo.txt"},
  content: "replacement\n"
});
return {content: result.content};
```

两种入口成功均返回本次完整 diff，无变化时 content 为 []；不要求先读，不传模式、修订值或行操作数组。输入 content 为空字符串时清空现有文件，不删除文件本身。

填充现有空文件也可以显式使用局部替换分支：

```javascript
const result = await tools.edit_file({
  target: {kind: "PATH", path: "empty.txt"},
  old_text: "",
  new_text: "first line\n"
});
console.log({content: result.content});
```

如果 empty.txt 不存在或其正文不为空，该调用失败，不改为创建文件、覆盖非空文件或在某个任意位置插入。

无变化成功业务值示意如下。它表示目标及适用的匹配检查已通过，生成结果与当前文件字节相同，不是未执行校验的快捷返回：

```json
{
  "target": {"kind": "PATH", "path": "demo.txt"},
  "meta": {"status": "SUCCESS"},
  "content": []
}
```

原生正常交付该结果仍提供实际观察信息、完整结果 Target 和 next:null；JS 正常返回该完整业务值，不抛出无变化错误。

### 5.8 写入保护、错误与取消

复用现有同目标锁，从读取当前内容、匹配、准备新内容及 diff 到提交均处于同一锁范围，避免同一 owner 内对同一目标的修改相互覆盖。提交前再核对当前文件字节是否仍为本次调用读取的内容；若准备期间已变化，返回 ERROR，说明路径及文件在本次编辑期间发生变化，不自动重新匹配或重试。

这项核对只使用本次调用内的实际内容，不保存修订记录，不要求模型携带版本，也不检查模型先前读取的版本。调用开始前文件其他部分已经变化、但当前 old_text 仍精确唯一时，可以编辑。锁及提交前核对不构成对任意外部进程的文件系统原子比较交换；不承诺消除核对与替换之间的外部竞争。

新内容在同目录临时文件中准备，按既有文件 owner 保持必要权限，并通过原子替换提交；临时准备或提交失败时不退回原地截断写入。文件路径、权限及实际写入结算复用原 owner，不新增文件锁服务、修订状态、持久任务或恢复协议。

未找到 old_text、多匹配、目标不存在、空 old_text 用于非空正文、互斥参数混填等错误，使用 meta.status:ERROR 和简短 content，不新增错误代码字段或恢复指令包装。例如：

```json
{
  "target": {"kind": "PATH", "path": "demo.txt"},
  "meta": {"status": "ERROR"},
  "content": [
    {"type": "text", "value": "无法修改 demo.txt：未找到与 old_text 精确匹配的正文。"}
  ]
}
```

原生按相同业务值交付错误原因，另加实际可用的观察信息和交付引用；错误结果完整保存并整条交付时 next:null。JS 业务错误沿异常通道返回，存在上述完整失败业务值时通过 err.result 提供；没有业务值的参数或运行时失败不伪造 result。

提交前取消且确认文件未修改时，返回 CANCELLED。一旦进入不可安全中断的提交阶段，须等待本次实际写入结算，不能先返回取消、再让后台写入继续生效。修改成功且结果正常产生时返回 SUCCESS 和 diff；后到的取消不将已完成修改重新归类为未执行，可停止后续工作。

实际写入、校验、结果保存或交付失败时返回 ERROR，content 保留已知修改事实。不能确定文件是否已修改时，简短说明“文件可能已修改，请重新读取”及路径，不声称文件未变。保存和交付失败仍沿统一草案处理，不自动重新编辑，也不增加部分成功或写入不确定等公开状态。

### 5.9 实施核对点

以下是采用本合同后的行为验证要求，本轮未实施或运行验证：

- 两个参数分支互斥且字段闭合，SDK 类型、工具参数 schema 与运行时接受形状一致；类型中的 never 排除项不作为模型可填参数。原生与 JS 使用相同业务参数，目标不存在时不隐式创建。
- 精确唯一匹配覆盖换行、空白、重复及重叠匹配；无模糊回退、不按首个匹配猜测位置、不批量替换。
- 空 old_text 仅对当前正文为空的现有文件有效；整文件替换无需旧全文，空 content 可清空文件。
- 无效 UTF-8、含 NUL 的正文及输入、现有不支持的文件类型明确拒绝，不通过有损转换接受。
- 局部匹配正文与 read_file 一致，保留原 BOM 和未修改部分的原始换行；BOM-only 视为空正文。整文件输出由 content 的实际 UTF-8 编码决定，包括 BOM 和末尾换行。
- 无变化先完成适用校验，再返回 SUCCESS 和 content:[]，不写盘、不更改文件修改时间、不抛无变化错误；未找到或多匹配不能因 old_text=new_text 绕过拒绝。
- 不接受公开修订条件，不维护已读行区间、模型预读或全文已读资格，也不通过结果窗口授予后续编辑资格。
- 实际修改的成功 content 是完整 diff，无变化为空数组；diff 正确表达 BOM、换行及末尾无换行差异。失败 content 是简短原因、路径和已知效果，不带独立 diff 字段、修改窗口、窗口截断标记、操作计数、重复路径数组或公开文件修订字段。
- 原生 12k 只约束结果交付，JS 取得完整业务值；没有编辑前的展示预算拒绝，也不因保存或交付失败重做编辑。
- 复用同目标锁，提交前只核对本次读取内容是否变化；变化则拒绝且不自动重试，不宣称外部进程原子比较交换保证。
- 使用同目录临时文件及原子替换并保持必要权限，不在失败后退回原地截断写入；路径权限及实际写入 owner 保持。
- 提交前可确认取消时返回 CANCELLED；不可安全中断的提交等待实际结算，已完成修改不被取消误报为未执行。写入或后续保存、交付失败用 ERROR 并保留已知效果或不确定性。

## 6. find_files：目录 PATH Target

### 6.1 用途与范围

按文件名或相对于搜索目录的路径 glob 查找文件，返回命中文件的 PATH Target。target 指搜索目录，content 中的 Target 指实际命中文件，两者用途不同；本工具不读取文件正文，也不返回目录列表。

搜索目录保持既有 PATH 解析与授权方式，支持工作区相对路径、绝对路径及 `~`；省略时使用工作区根。匹配结果的路径统一以工作区为基准：工作区内相对，工作区外绝对，不随搜索目录变化而改成相对于搜索目录的路径。

例如 target 为 src 时，glob `*.py` 在 src 内递归匹配，而命中的 src/deep/c.py 仍返回 `{"kind":"PATH","path":"src/deep/c.py"}`，不是 `deep/c.py`。

### 6.2 最简参数表

target 始终放在参数表、类型声明及示例首位，即使它是可选的；不将必填 glob 移到 target 前面。

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PATH Target | 可选，默认工作区根 | 指定搜索目录，如 `{"kind":"PATH","path":"src"}` |
| glob | 非空字符串 | 必填 | 文件名或相对于搜索目录的路径匹配规则，不是正则表达式 |

```typescript
interface FindFilesPathArgs {
  target?: PathTarget;
  glob: string;
}
```

只接受 target 和 glob，不接受 limit、continuation 或其子字段。查询执行与结果保存覆盖本次搜索范围，不按旧的默认 50、最大 1,000 条或原生页预算提前停止；真实执行错误或取消不能被包装成完整成功。

### 6.3 匹配及结果顺序

保留现有匹配含义，区分大小写：

| glob | 含义 |
| --- | --- |
| `*.py` | 搜索目录下所有深度的 Python 文件 |
| `src/*.py` | 搜索目录下 src 直属目录中的 Python 文件 |
| `src/**/*.py` | 搜索目录下 src 目录及其后代中的 Python 文件 |
| `*config*` | 文件名包含 config |
| `config` | 文件名恰好是 config，不是子串匹配 |

目录遍历、ignore、隐藏文件及符号链接规则继续复用 ripgrep 与既有搜索 owner，默认不额外开启隐藏或忽略项、不跟随目录遍历中的符号链接。使用私有 ripgrep 且不加载用户的额外 rg 配置；按现有 rg 路径排序输出，不增加修改时间排序等参数。

图片、视频和二进制文件都可以作为路径命中；是否能读取或查看由后续工具按各自合同判断。搜索返回 Target 本身不读取文件、不验证其媒体内容，也不保证文件在后续调用时仍存在。

### 6.4 成功返回值与交付

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| target | PATH Target | 实际搜索目录；工作区根为 `.`，工作区内目录使用相对路径，工作区外使用绝对路径 |
| meta.status | SUCCESS | 本次搜索正常完成；不表示原生模型已经看完全部结果 |
| content | (TextPart \| TargetPart)[] | 有匹配时按顺序返回命中文件的 Target 项；无匹配时返回一句文字说明 |

```typescript
interface FindFilesPathSuccess {
  target: PathTarget;
  meta: {status: "SUCCESS"};
  content: (TextPart | TargetPart)[];
}

interface FindFilesPathFailure {
  target: PathTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: (TextPart | TargetPart)[];
}

type FindFilesPathValue = FindFilesPathSuccess | FindFilesPathFailure;

interface FindFilesPathToolError extends Error {
  result?: FindFilesPathFailure;
}
```

不返回 targets、access_scope、workspace_relative、结果总数字段或重复的自然语言清单。每个命中文件仅保存为 `{"type":"target","value":{"kind":"PATH","path":"…"}}`，JS 通过 value 直接使用，原生与 Cell 输出从同一内容项投影为可读引用。

原生按统一 12k 规则交付，另加顶层观察信息、tool_result 和 next；Target 项按文字计量，保持各项顺序，不拆开单个 Target 内容项。后续页面只读取已经保存的搜索结果，不重新遍历目录，也不向 find_files 传回游标。

JS 正常返回完整 FindFilesPathSuccess，不分页、不带原生时间与交付引用。显式 console.log({content: result.content}) 或相同形式的 return 可以直接交付这些路径引用；runtime 只展示引用，不自动调用 read_file、view_image 或其他工具。

### 6.5 教程：搜索目录并使用文件 Target

假设工作区内存在 src/b.py、src/deep/c.py 和 src/notes.txt。原生调用：

```text
工具：find_files
参数：
  target: {"kind":"PATH","path":"src"}
  glob: "*.py"
```

完整结果可以装入一页时，原生返回示意：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际可用耗时>

target: {"kind":"PATH","path":"src"}

meta:
  status: SUCCESS

content:
  {"kind":"PATH","path":"src/b.py"}
  {"kind":"PATH","path":"src/deep/c.py"}

tool_result: <完整搜索结果的导出 Target>
next: null
```

对应的 JS 调用：

```javascript
const result = await tools.find_files({
  target: {kind: "PATH", path: "src"},
  glob: "*.py"
});
console.log({content: result.content});
```

JS 的完整 result 为：

```json
{
  "target": {"kind": "PATH", "path": "src"},
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "target", "value": {"kind": "PATH", "path": "src/b.py"}},
    {"type": "target", "value": {"kind": "PATH", "path": "src/deep/c.py"}}
  ]
}
```

脚本按 type 筛选 Target 项，再直接将 value 交给文件工具；文字说明不会被误当成文件，不解析原生展示文字，也不重建 PATH Target：

```javascript
for (const part of result.content) {
  if (part.type === "target") {
    const file = await tools.read_file({target: part.value});
    // 在 JS 中处理 file.content；读取结果不会自动进入 ROOT。
  }
}
```

省略 target 时搜索工作区根，例如原生调用：

```text
工具：find_files
参数：
  glob: "*.py"
```

结果顶层 target 使用 `{"kind":"PATH","path":"."}`。工作区外的例子：搜索目录 `/data/project` 命中 `/data/project/main.py` 时，结果中的目录及文件 Target 都使用相应绝对路径。

### 6.6 无匹配与特殊文件名

本次搜索正常完成但无匹配时返回 SUCCESS，并在 content 中提供一句文字说明，不返回空数组：

```json
{
  "target": {"kind": "PATH", "path": "src"},
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "未找到匹配文件。"}
  ]
}
```

原生正常交付该说明仍提供实际观察信息、tool_result 和 next:null；JS 正常返回该业务值。没有 type:target 项表示未找到文件，不增加伪文件 Target、独立计数字段或另一套空结果包装。read_file 的空正文及 edit_file 的无变化仍按各自合同返回 []，不因此改成说明文字。

Target 项 value.path 保留实际路径字符。原生及 Cell 文字投影使用 JSON 转义表达换行、引号等特殊字符，不把一个含换行的文件名误展示为多个文件；JS 始终取得原始 path 字符串，不需要拆分显示行或反向解析转义清单。

### 6.7 扫描失败、取消与完整失败值

目录无法完整遍历、执行错误或超时时，返回 ERROR；扫描取消时返回 CANCELLED。两者都不将已经找到的一部分文件说成完整成功：content 先用文字项简短说明原因、路径及结果不完整，再按实际结果顺序保留所有已找到文件的 Target 项。不丢弃已取得的文件，也不为说明再复制一份文件清单。

正常遵守 ignore、隐藏文件及符号链接规则而跳过的路径，属于本次搜索规则，不算扫描缺口或失败。没有匹配且搜索正常完成时，按第 6.6 节返回 SUCCESS 和一句说明，不能与扫描失败但暂未取得文件混同。

失败业务值示例：

```json
{
  "target": {"kind": "PATH", "path": "src"},
  "meta": {"status": "ERROR"},
  "content": [
    {"type": "text", "value": "搜索 src 未完成：无法访问 src/private。以下仅为已找到的文件，结果不完整。\n"},
    {"type": "target", "value": {"kind": "PATH", "path": "src/a.py"}},
    {"type": "target", "value": {"kind": "PATH", "path": "src/b.py"}}
  ]
}
```

取消同样保留简短取消说明及已找到文件，meta.status 为 CANCELLED；尚未找到文件时只返回原因文字项。没有实际业务结果的参数或运行时失败不伪造失败值或 Target。

完整保存的是本次失败观察，包括原因与实际取得的文件，不声称保存了完整成功查询的清单。原生沿统一 12k 规则交付这份结果；错误结果全部正常交付后可以 next:null，这仅表示本次结果交付结束，不表示扫描完整成功。

JS 遇到失败或取消走异常通道，实际存在的完整 FindFilesPathFailure 放入 err.result，不分页、不截断。脚本可按同一 type/value 规则输出完整内容或选取已找到的文件：

```javascript
try {
  const result = await tools.find_files({
    target: {kind: "PATH", path: "src"},
    glob: "*.py"
  });
  console.log({content: result.content});
} catch (err) {
  if (!err.result) throw err;
  console.log({content: err.result.content});
  const files = err.result.content
    .filter(part => part.type === "target")
    .map(part => part.value);
  // files 是实际已找到的 PATH Target；不自动重新扫描。
}
```

结果交付分页与扫描是否完成是不同事实。任何返回的 next 只读取已保存的本次结果，不执行后续扫描。重新搜索由模型或脚本显式发起新的 find_files 调用；不新增 PARTIAL 状态、partial 字段、来源穷尽字段、limit、搜索 continuation 或后台扫描任务。

### 6.8 实施核对点

以下是采用本合同后的行为验证要求，本轮未实施或运行验证：

- 参数仅 target、glob，target 在参数表、SDK 声明及示例首位；省略时搜索工作区根。
- target 表达搜索目录，content 中每个 type:target 项的 value 表达命中文件；返回路径以工作区为基准，内部相对、外部绝对，不相对搜索子目录重新编码。
- 匹配与遍历复用现有 glob、ripgrep 及路径 owner，保留 ignore、隐藏文件、符号链接和路径排序规则；不隐式读取或加载结果文件。
- 删除 limit、continuation、旧条数上限及展示预算导致的提前停止；正常查询完整保存后，原生按 12k 交付，JS 取得完整数组。
- content 项仅使用 type/value：文件命中为 Target 项，不再同时返回 targets、重复文字清单、access_scope、workspace_relative 或总数字段；无匹配正常返回 SUCCESS 和一句文字说明。
- 原生与显式 Cell 输出投影完整 PATH 引用，特殊文件名正确转义；JS 按 type 筛选后直接传递 value，next 只补读固定保存结果。
- Target 项可直接参加 console.log／return 组合；只展示 value 中的引用，不自动打开文件，不扫描普通日志中的路径或 JSON。JS 按 type 筛选后直接使用 value。
- 扫描错误或超时为 ERROR，扫描取消为 CANCELLED；content 先说明原因及不完整事实，再保留已找到文件的 Target 项。正常 ignore／隐藏项跳过不算失败，不新增 partial 字段或状态。
- JS 通过 err.result 取得完整失败值；原生正常分页该值，next:null 不意味着扫描成功，续读不重新扫描，不自动重试。

## 7. search_file_content：文件正文搜索

### 7.1 用途与范围

在指定文件或目录中搜索文件正文，唯一公开工具名为 search_file_content。文件名搜索使用 find_files；本工具的 pattern 是内容正则，不是文件名 glob。生产代码、SDK、目录及工具说明采用同一名称，不提供同义旧入口。

搜索目录或单个文件使用 PATH Target，省略 target 时默认工作区根。target 在参数表、SDK 声明及示例中始终放首位，即使它是可选的。顶层 target 表达实际搜索对象；命中记录中的文件路径继续统一为工作区内相对、工作区外绝对。

### 7.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | PATH Target | 可选，默认工作区根 | 搜索目录或指定单个文件 |
| pattern | 非空字符串 | 必填 | Rust 正则表达式，用于匹配文件正文 |
| file_glob | 非空字符串 | 可选，无额外文件名筛选 | 筛选参与搜索的文件，采用 find_files 的 glob 规则 |
| output_mode | content／files_only／count | 可选，默认 content | 完整匹配行、命中文件或每文件非重叠匹配次数 |

```typescript
interface SearchFileContentArgs {
  target?: PathTarget;
  pattern: string;
  file_glob?: string;
  output_mode?: "content" | "files_only" | "count";
}
```

只接受这四个参数，删除 limit、continuation 及其子字段；不引入 offset、结果条数页长或另一套搜索游标。输出模式选择业务结果，不改变匹配表达式或参与搜索的文件范围。

目录目标的 file_glob 相对于该搜索目录解释；单文件目标则对该文件应用同一文件筛选含义。省略 file_glob 不额外按文件名筛选。pattern 不擅自去除首尾空白；正则语法与默认大小写行为沿 Rust 正则引擎，不另做 Python 或 JS 正则回退。

### 7.3 三种模式的业务语义

三个模式共用文件选择、正则匹配、错误及取消规则，首版按行搜索，不增加跨行模式或额外上下文参数。目录遍历、ignore、隐藏文件、符号链接和二进制检测复用既有 ripgrep owner；显式文件目标保持其既定含义，不通过更换模式偷偷改变过滤范围。

| 模式 | 一项业务记录 | content 内容项 |
| --- | --- | --- |
| content | 一个匹配行；同一行多次命中仍只返回一次 | object，value 含 target、line、text |
| files_only | 一个有匹配的文件；同一文件只返回一次 | target，value 为 PATH Target |
| count | 一个文件的非重叠正则匹配次数 | object，value 含 target、count |

content 模式返回完整匹配行，保留行文本及原行结束符，不附加显示行号，不按固定字符数截摘录；超长行仍完整保存和返回 JS。原生仅对 object 的通用 JSON 文字投影分页，需要更多上下文时调用 read_file，不为搜索额外制造上下文记录。

count 统计匹配出现次数，不是匹配行数、文件数或捕获组数量。一行中的多个非重叠命中分别计数；例如 `foo foo\nfoo\n` 搜索 foo 得 3 次，`aaa` 搜索 aa 得 1 次。由同一正则引擎按非重叠匹配口径执行，不自行改成重叠匹配计数。局部 edit_file 的唯一位置检查仍使用其独立合同，不将该计数规则套到编辑定位。

files_only 和 count 只返回有匹配的文件，不额外生成零计数项。结果按现有路径顺序排列，content 模式在同一文件内按行号递增；不增加修改时间排序或全局总计字段。JS 可对成功的完整计数记录自行求和，原生交付页长不改变统计范围或数字。

### 7.4 类型化记录与返回值

匹配行和计数使用同一种 object 内容类型，各自的 value 具有精确字段，不增加 match、count 内容类型，也不要求程序解析可读文字来恢复字段：

```typescript
type FileLineObject = {
  type: "object";
  value: {
    target: PathTarget;
    line: number; // 原文件行号，从 1 开始
    text: string; // 完整匹配行，保留原行结束符，不带显示行号
  };
};

type FileCountObject = {
  type: "object";
  value: {
    target: PathTarget;
    count: number; // 正整数，非重叠匹配次数
  };
};

type SearchFileContentPart = TextPart | TargetPart | FileLineObject | FileCountObject;

interface SearchFileContentSuccess {
  target: PathTarget;
  meta: {status: "SUCCESS"};
  content: SearchFileContentPart[];
}

interface SearchFileContentFailure {
  target: PathTarget;
  meta: {status: "ERROR" | "CANCELLED"};
  content: SearchFileContentPart[];
}

type SearchFileContentValue = SearchFileContentSuccess | SearchFileContentFailure;

interface SearchFileContentToolError extends Error {
  result?: SearchFileContentFailure;
}
```

上面列出该工具所有模式的结果项联合；实际 SDK 声明须按 output_mode 收窄返回内容：默认及 content 为 TextPart／FileLineObject，files_only 为 TextPart／TargetPart，count 为 TextPart／FileCountObject。失败值保留同一请求模式实际已取得的记录。meta 仅保留 status，不回显 output_mode，也不增加 matches、counts、targets、计数总和、access_scope 或 workspace_relative 等并列字段。

JS 正常返回本次完整业务值；原生另外提供顶层观察信息、tool_result 和 next，按统一 12k 交付。object 的顶层 target 按共同规则展示为原生 Target 引用，其余字段投影为 JSON；不自动读取文件。JS 仍直接使用完整的 part.value.target、line、text 或 count。

### 7.5 教程：三个模式的完整业务值

假设 src/a.py 的正文为 `foo foo\nbar\n`，src/b.py 的正文为 `foo\n`。共同搜索范围为 src，pattern 为 foo，file_glob 为 *.py。三种请求独立执行，参数顺序一致：

```text
工具：search_file_content
参数：
  target: {"kind":"PATH","path":"src"}
  pattern: "foo"
  file_glob: "*.py"
  output_mode: "content"
```

content 模式的完整业务值：

```json
{
  "target": {"kind": "PATH", "path": "src"},
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "object", "value": {"target": {"kind": "PATH", "path": "src/a.py"}, "line": 1, "text": "foo foo\n"}},
    {"type": "object", "value": {"target": {"kind": "PATH", "path": "src/b.py"}, "line": 1, "text": "foo\n"}}
  ]
}
```

同一组参数选择 output_mode:files_only 时：

```json
{
  "target": {"kind": "PATH", "path": "src"},
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "target", "value": {"kind": "PATH", "path": "src/a.py"}},
    {"type": "target", "value": {"kind": "PATH", "path": "src/b.py"}}
  ]
}
```

同一组参数选择 output_mode:count 时：

```json
{
  "target": {"kind": "PATH", "path": "src"},
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "object", "value": {"target": {"kind": "PATH", "path": "src/a.py"}, "count": 2}},
    {"type": "object", "value": {"target": {"kind": "PATH", "path": "src/b.py"}, "count": 1}}
  ]
}
```

content 模式一共两条匹配行，count 模式合计三次命中；不为了统计出现次数在 content 模式重复同一行。

### 7.6 原生投影与 JS 组合

原生对 object 使用共同投影：仅把顶层精确 target 字段按 Target 引用展示，其余字段使用通用 JSON，不为匹配行或计数定义专门渲染器。以下是 count 模式完整装入一页的文本示意：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际可用耗时>

target: {"kind":"PATH","path":"src"}

meta:
  status: SUCCESS

content:
  {"kind":"PATH","path":"src/a.py"}
  {
    "count": 2
  }
  {"kind":"PATH","path":"src/b.py"}
  {
    "count": 1
  }

tool_result: <完整搜索结果的导出 Target>
next: null
```

content 模式同样先展示 Target 引用，再以 JSON 展示 line、text；text 中的原始换行按 JSON 规则转义，不再另加 read_file 的显示行号。files_only 则直接按共同 Target 项投影，保持引用完整。分页可以切开 object 的 JSON 文字投影，但不能拆开引用、把页面片段当成完整 JSON 或让 JS 接收不完整对象。

JS 计数与字段访问：

```javascript
const result = await tools.search_file_content({
  target: {kind: "PATH", path: "src"},
  pattern: "foo",
  file_glob: "*.py",
  output_mode: "count"
});

let total = 0;
for (const part of result.content) {
  if (part.type === "object" && "count" in part.value) {
    total += part.value.count;
  }
}
console.log(total);
```

该日志只输出计算结果，不自动披露所有中间记录。需要将完整搜索材料交给 ROOT 时，显式使用 `console.log({content: result.content})` 或相同形式的 return；文件定位可以直接使用记录中的 value.target，不解析原生 JSON 展示正文。

### 7.7 无匹配、错误与取消

三种模式正常搜索完成且无匹配时，都返回 SUCCESS 和一句文字说明，不返回空匹配对象、伪文件或零计数列表：

```json
{
  "target": {"kind": "PATH", "path": "src"},
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "未找到匹配内容。"}
  ]
}
```

非法正则、路径错误、扫描缺口、执行失败或超时为 ERROR，扫描取消为 CANCELLED；正常遵守 ignore、隐藏文件及二进制检测规则不算扫描失败。非法正则即使没有候选文件也要报告错误，不能改成成功无匹配。

实际存在的失败业务值先用文字项说明原因、路径及搜索未完成，再按当前模式保留已经取得的完整业务记录：content 为匹配行 object，files_only 为 Target 项，count 为计数 object。不把未完成搜索中的计数称为完整查询总数，不补零、不自动推断未扫描文件的结果，也不为保留部分结果增加字段或公开状态。

例如 count 模式执行期间发生扫描错误：

```json
{
  "target": {"kind": "PATH", "path": "src"},
  "meta": {"status": "ERROR"},
  "content": [
    {"type": "text", "value": "搜索 src 未完成：无法访问 src/private。以下计数仅为实际已取得的结果，不是完整查询总数。\n"},
    {"type": "object", "value": {"target": {"kind": "PATH", "path": "src/a.py"}, "count": 2}}
  ]
}
```

JS 沿异常通道通过 err.result 提供实际存在的完整 SearchFileContentFailure，不分页、不截断；没有业务结果时不伪造 result。原生正常分页保存的失败值，next:null 只表示这份结果交付完毕，不表示搜索成功。任何 next 都不继续扫描或重新请求正则搜索，重新搜索须由模型或脚本显式发起。

### 7.8 实施核对点

以下是采用本合同后的行为验证要求，本轮未实施或运行验证：

- 唯一公开名称为 search_file_content，参数只有 target、pattern、file_glob、output_mode；target 在参数表、类型和示例首位，默认根与单文件／目录行为明确。
- 三模式使用同一文件筛选及正则语义，不 trim pattern，不用其他正则引擎回退，不引入跨行模式；非法正则不因候选文件为空而被忽略。
- content 按匹配行返回完整文字及原行号，不重复同行，不保留固定长度摘录及其位置／截断字段；files_only 每文件一次；count 每文件统计非重叠匹配次数，不计捕获组或匹配行数。
- 匹配行和计数都使用 type:object，value 具有精确字段；files_only 复用 type:target，无匹配使用 type:text 说明，不增加 match、count 内容类型或并列结果数组。
- 原生只使用共同 object／Target 文本投影和 12k 分页：顶层精确 target 复用引用展示，其余字段为 JSON，不递归特判 targets 数组或更深层 target。JS 取得完整对象，可直接访问定位与计数，不返回结构化分片或依赖模型解析正文。
- 扫描错误、超时和取消分别按三公开状态结算，保留原因和已取得业务记录至完整 err.result；不把部分计数称为完整总数，不为失败新增 partial 字段或扫描任务。
- 删除 limit、continuation、旧条数页长及固定正文摘录限制；完整保存后交付，next 只补读固定结果，不重新扫描、不自动重试。

## 8. 当前生产实现参考

- [read_file 当前参数定义](src/pulsara_agent/tools/builtins/filesystem_contracts.py)
- [write_file 当前参数定义](src/pulsara_agent/tools/builtins/filesystem_contracts.py)
- [edit_file 当前参数定义](src/pulsara_agent/capability/builtin_catalog.py)
- [find_files 当前参数定义](src/pulsara_agent/tools/builtins/filesystem_contracts.py)
- [文件正文搜索的当前参数定义](src/pulsara_agent/tools/builtins/filesystem_contracts.py)
- [文件搜索与路径枚举当前实现](src/pulsara_agent/tools/builtins/filesystem_search.py)
- [文件读取、创建与编辑的当前实现](src/pulsara_agent/tools/builtins/filesystem.py)
- [路径解析与工作区文件 owner](src/pulsara_agent/tools/builtins/workspace.py)
- [Code Mode 当前调用入口](src/pulsara_agent/conversation_kernel/tool_execution.py)
- [当前原生工具结果投影](src/pulsara_agent/primitives/tool_result_projection.py)

这些链接用于核对实施接点，不表示当前代码已经符合本草案。后续 bundled skill 采用教程前，必须与已实施的参数、返回声明和执行值一起更新；不得先让模型按未实施接口调用工具。
