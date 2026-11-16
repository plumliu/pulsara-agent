# Pulsara MCP 接入工具修订与使用教程草案

共用规则见[原生工具修订草案索引](../PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章；完整保存、原生 12k／NEXT、JS 完整返回和 Cell 图文输出沿[统一交付草案](../PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)，存储沿[数据库 hardcut 草案](../PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md)。发现连接、工具目录及精确详情见[能力查询草案](capabilities.md)第 3 章。

状态：当前合同已接入生产代码，正在完成 hardcut 跨层验收；正常使用仍按实施计划停用。本文件确定 call_mcp_tool 和 read_mcp_resource 的参数、精简返回值及原生／JS 用法，架构沿[统一 meta 调用草案](../PULSARA_MCP_META_ONLY_CALLING_ARCHITECTURE_DRAFT.zh.md)。ROOT 与子代理统一通过元工具调用远端工具，并可使用资源目录／读取入口；适用 Code Mode 时使用相同固定 API 的 JS 方法。工具调用引用按同 session 共用并在完整查询结果接纳后生效，权限、凭据、Hook、取消及副作用结算复用现有 owner。

## 1. call_mcp_tool 的返回类型

### 1.1 共同业务值与原生交付字段

所有远端 MCP 工具通过 call_mcp_tool 调用；它自身是 Pulsara 原生工具，也提供 tools.call_mcp_tool。远端工具不独立安装进 provider tools 数组或 Cell 方法目录。原生与 JS 使用同一业务返回形状：

| 字段 | 明确含义 |
| --- | --- |
| meta.status | SUCCESS／ERROR／CANCELLED，meta 只含这一项 |
| content | 转换后的 Pulsara 有序 text／image 内容项数组 |
| structuredContent | MCP 协议的可选结构化业务值；服务未提供就省略，显式 null 则保留 null |

不返回业务层面的顶层 target，也不回显 MCP_TOOL_REF、服务名、工具名或调用参数，不为元工具另套 result。已有调用关联由原调用上下文承担。structuredContent 不推广为所有 Pulsara 工具的公共字段，不能要求每个 MCP 响应都具备它，也不能规定工具结果的主体一定在该字段中。

原生交付另按共用外壳并列呈现 observed_at_utc、observation_duration_microseconds、tool_result 和 next。tool_result 是本次完整结果的导出 Target；next 是未交付后缀的 NEXT Target，交付完为 null。没有业务 target 不影响这两个公共 Target。

JS 直接取得完整的 meta/content/structuredContent 业务值，不携带原生的时间外壳、tool_result 或展示 next。业务 JSON 内同名字段保持原义，不按名字删除或变成 Pulsara 控制字段。

```typescript
type McpJsonValue =
  | string | number | boolean | null
  | McpJsonValue[] | McpJsonObject;
type McpJsonObject = {[key: string]: McpJsonValue};

type McpImageRefTarget = {kind: "IMAGE_REF"; image_ref: string};
type McpTextPart = {type: "text"; value: string};
type McpImagePart = {type: "image"; value: McpImageRefTarget};
type McpContentPart = McpTextPart | McpImagePart;

interface McpToolSuccess<T extends McpJsonValue = McpJsonValue> {
  meta: {status: "SUCCESS"};
  content: McpContentPart[];
  structuredContent?: T;
}

interface McpToolFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: McpContentPart[];
  structuredContent?: McpJsonValue;
}

type McpToolResult<T extends McpJsonValue = McpJsonValue> =
  | McpToolSuccess<T>
  | McpToolFailure;
```

T 表示服务声明的成功 structuredContent 类型，不表示整个返回包。没有 output_schema 时保留真实 JSON 类型，不猜测固定业务字段；有 schema 时按其声明及当前 SDK 处理。失败 structuredContent 不保证符合成功 schema，不能用成功类型删改失败值。元工具复用同一返回类型，不新增专属返回包装。

### 1.2 只转换正式 content

MCP 的 content 是协议本来就有的有序内容数组。先按 unified 第 2.3 节过滤 audience 并移除该标注，再按原顺序转换：

| MCP 正式内容 | Pulsara content |
| --- | --- |
| TextContent | {type:"text", value:原 text 字符串} |
| ImageContent | 图片验证及保存后，形成 {type:"image", value:IMAGE_REF} |
| 音频、资源链接、嵌入资源等 | 按已定规则生成确定性文字，保留公开信息及所属位置，不自动读取或播放 |
| audience 之外需要公开的附带标注 | 作为紧邻所属项的文字保留；图片说明在图之前 |

call_mcp_tool 的结果适配不生成 object 或 target 内容项。不把 JSON 文字解析成 object，不把资源 URI、文件路径、链接或文字里的 Target 自动转换成 Pulsara Target，也不推断普通文字和相邻图片的关系。通过过滤的项不因只支持 text/image 而额外丢失；不把尚未适配的 SDK 对象交给 JS，等 console.log 时才报格式错误。第 4 章的资源目录记录由 Pulsara 显式组织为 object，不属于本项远端正文转换。

对通过 audience 过滤的正式 ImageContent 逐项解码和验证，复用现有图片 owner；不把整份多图结果交给遇坏图就整批拒绝的接口。成功项正常保存并使用真实 IMAGE_REF；某项 Base64 无效、图片损坏、格式不支持或 MIME 不符时，只将该位置接入为 text 失败说明，前后文字、其他有效图片及 structuredContent 继续保留原顺序，不跳过坏图位置或重新调用远端工具。

失败说明复用 view_image 与 unified 第 8.2 节的文字表达，保留实际原因，并明确“未交付实际图片”。例如：

```text
图片数据无法解码：Base64 格式无效，未交付实际图片。
```

新收到的坏图尚无已保存资产，不生成假 IMAGE_REF 或图片外键，保存的是该位置真实的 text 说明；已经有真实引用、仅后续读取失败的情况则保留引用及原因。后者继续采用共同投影规则，不改写已保存 image 项。整份结果本身无法可靠保存时仍沿共同保存失败规则处理，不能以局部占位冒充完整保存。

structuredContent 是独立业务 JSON，原样保留，不搬进 content、不包装成 object 项、不展开到 Pulsara 顶层。其内部即使包含 content、type、value、target、status、error、isError 或 Base64，也不按字段名扫描、解析、替换或过滤。只转换正式 ImageContent 中的 Base64；业务 JSON 中的 Base64 可以保留。这不等于另外保存整份原始 MCP JSON 备份。这里约束的是结果接入与保存；脚本实际输出时，合法 Pulsara image 内容项仍按共同 steer 规则递归投影，不改写 JS 原值。

content 与 structuredContent 可能重复，也可能互补，二者都按服务实际返回保留；不自动去重或仅选其中一份。structuredContent 在原生和导出中使用确定的 JSON 文字投影，不因此改变保存值或 JS 字段类型。

### 1.3 状态与失败

完成且图片接入正常的 MCP CallToolResult 按正式 isError 映射：true 对应 ERROR，false 或缺省对应 SUCCESS。发生单图接入失败时，完整接入结果为 ERROR，content 在失败位置说明原因并保留其他内容；JS 沿既有异常通道在 err.result 中提供这份完整值。ERROR 不表示远端操作未执行，也不改写已经确定的远端执行事实。不再单独返回 isError，也不另加 meta.error、错误枚举或公开的传输／业务／交付状态矩阵。structuredContent 里的错误、状态、重试参数等仍是远端业务数据。

不能根据文字包含 error、返回空数组、structuredContent.status 等改写调用状态。JSON-RPC 错误、参数无效、引用失效、权限拒绝、取消和通信失败沿既有错误及结算通道处理；没有远端业务响应时不伪造一份服务返回的 content 或 structuredContent。已知未派发、已发生效果或远端效果未确认时如实说明，不将失败统一解释为远端没有执行，不自动重试。

仅实际结算为取消时使用 CANCELLED。取消请求到达后若仍取得确定的成功或错误结果，保留真实 SUCCESS／ERROR 及已接受内容，不以取消到达证明请求未派发。

本地确认旧引用失效且未派发时，content 明确说明本次未发送，并指引重新查询；工具已删除则说明不可用。连接或目录暂不可用不等于引用定义改变，服务返回 isError、参数拒绝或输出 schema 不匹配也不自动证明引用过期。已发送后超时或响应丢失说明结果是否未确认，不因模型查到新引用就自动重做原操作。

正式 isError:true 的完整图文与结构化业务值仍保存。JS 沿现有异常通道抛出错误，有业务值时通过 err.result 取得同一完整失败值；没有业务值时不伪造 err.result。原生失败结果仍可分页，next:null 只表示本次失败结果已交付完。

正式图片的实际读取、模型能力投影、失败占位和原生 ERROR 沿 unified 第 8.2 节。仅交付投影失败不能反向修改 JS 中的远端业务事实；收到确定响应后也不能因图片适配失败就宣称远端效果未知或重发调用。结果保存／交付失败继续沿共同规则，不新增恢复任务。

### 1.4 共同返回示例

假设某工具实际返回了文字、图片和结构化结果。原生与 JS 调用都保存以下业务形状，JS 直接取得完整值；ID 为示意：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "搜索结果及页面截图：\n"},
    {"type": "image", "value": {"kind": "IMAGE_REF", "image_ref": "image:<图片ID>"}}
  ],
  "structuredContent": {
    "documents": [{"id": "guide-1", "title": "安装指南"}]
  }
}
```

假设这一结果一页即可交付，原生文字投影为：

```text
meta:
  status: SUCCESS

content:
  搜索结果及页面截图：
  图片引用：{"kind":"IMAGE_REF","image_ref":"image:<图片ID>"}

structuredContent:
  {
    "documents": [
      {
        "id": "guide-1",
        "title": "安装指南"
      }
    ]
  }

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整结果的导出 Target>
next: null
```

JSON 业务字段的实际投影统一使用固定两空格缩进。支持图片的模型还收到同页关联 user 图片消息；该消息不是新的人类输入。能力不足时按共同规则显示引用和固定说明，原生状态为 ERROR，不能声称实际图片已交付。

没有 structuredContent 的响应不增加空对象占位；仅结构化结果可为 content:[]；全部正式项被 audience 过滤为空时同样保留原业务状态和实际 structuredContent。

## 2. 调用前取得参数和返回结构说明

### 2.1 按需查询工具详情

模型沿能力查询的三层取得服务和工具信息：kind:MCP 服务列表 → MCP_CONNECTION 工具目录 → MCP_TOOL 精确详情。已经知道实际工具 Target 时可以直接精确查询。查询只观察既有目录，不自动安装、启用、连接或刷新服务，不公开 MCP_SERVER Target，也不向 provider 或 Cell 注册远端独立方法。

精确查询示例，目标来自实际目录：

```text
工具：query_capabilities
参数：
{
  "target":{"kind":"MCP_TOOL","server_id":"docs","remote_tool_name":"search"},
  "kind":"MCP"
}
```

返回的 object.value 提供 description、input_schema、服务实际声明时的 output_schema，以及 invocation。META 详情的完整 input_schema 必填，UNAVAILABLE 详情可省略无法提供的 schema；能力查询返回类型和固定 SDK 声明须表达这一条件。input_schema 用于 arguments 中的远端参数；output_schema 只描述成功 structuredContent，不描述整个 McpToolResult，也不在每次调用结果中重复返回。

| invocation.mode | 含义 |
| --- | --- |
| META | 使用 call_mcp_tool；target 原样复制 invocation.target，arguments 符合 input_schema |
| UNAVAILABLE | 当前不可调用，阅读实际原因，不拼造调用引用 |

MCP_TOOL 用于查询，MCP_TOOL_REF 用于执行；不能混填。所有工具使用 meta，不返回 DIRECT 或远端独立调用名。没有 output_schema 时，仍可使用共同包装、组合 content 或输出完整结果，不强制探测调用；只有需要访问未知业务字段时，才先观察真实结果，不为了解形状重复执行副作用。

### 2.2 先交付说明，再编写业务 Cell

模型可以直接原生查询详情，收到足够说明后原生调用 call_mcp_tool，也可以编写业务 Cell。另一种准备方式是先运行查询 Cell，显式输出详情：

```javascript
const inspected = await tools.query_capabilities({
  target: {kind: "MCP_TOOL", server_id: "docs", remote_tool_name: "search"},
  kind: "MCP"
});
console.log(inspected);
```

这个准备示例交付 description、input_schema、存在时的 output_schema 和调用引用。模型收到后，再编写第 3.3 节的业务 Cell。脚本在运行中取得 schema，不会自动让模型看到或补写代码；不把未知接口的查询和按该接口新写的业务代码当作一次自动完成的学习过程。已经掌握的接口可继续使用，不强制准备 Cell 或重复展示。

### 2.3 同 session 引用共用与 NEXT

同一 session 中，原生查询与各个 Cell 查询返回的 MCP_TOOL_REF 可供原生及其他 Cell 使用；切换 Cell、Cell 结束或模型 epoch 更换本身不使引用失效。ROOT 与子代理各用自己的 session 引用，不跨用。独立及 Plugin MCP 连接默认允许 ROOT 和子代理使用，用户可在能力页关闭子代理范围；子代理同样原生调用 call_mcp_tool，不因此获得 Code Mode 或管理权限。

详情查询结果完整保存、接纳后引用即可调用，不以 FULL 展示、读完整份 schema 或 JS 交付回调激活。原生按 12k／NEXT 交付，模型自行决定已有信息是否足够；参数及实际权限仍在调用时校验。NEXT 只补读冻结详情，不刷新、重发或激活引用。

引用对应该工具实际定义（含 description、input_schema、output_schema）、执行策略、范围和目标身份。实际依据变化或工具已删除时，拒绝旧引用的新调用；不判断变化是否兼容。其他工具增删不使未变引用失效。同目标普通重连完成发现、定义与策略仍一致时，可以复用引用，新调用解析当前执行器并取得物理连接许可。实际目标依据现有配置／连接／账户 owner 核对，不只比 server_id 和工具名；换 endpoint、执行账户或来源不得沿用旧引用或批准。全局 runtime 代次或整份服务配置标识的变化不单独决定引用失效。

必须删除旧每 epoch 1,024 个调用引用上限及专属错误分支、测试；不改成 session 总上限，不另设替代数值。复用有效引用、清理失效引用，沿进程内 owner 管理，不新增数据库引用表或跨重启恢复承诺。

## 3. call_mcp_tool：按查询引用调用

### 3.1 保留现有最简参数

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | MCP_TOOL_REF Target | 必填，放首位 | 精确查询在 META 分支实际返回的 invocation.target |
| arguments | JSON 对象 | 必填 | 符合该工具 input_schema 的参数；没有输入时填 {} |

```typescript
type McpToolRefTarget = {kind: "MCP_TOOL_REF"; tool_ref: string};

interface CallMcpToolArgs {
  target: McpToolRefTarget;
  arguments: McpJsonObject;
}

type CallMcpToolResult<T extends McpJsonValue = McpJsonValue> =
  McpToolResult<T>;
```

target 只含 kind、tool_ref，tool_ref 按现有共享 ID 层验证 mcpref 身份。引用按第 2.3 节同 session 共用，保留工具定义、执行策略及实际权限核对；不增加配置路径、服务名、远端工具名、scope、来源类别或页长字段。只接受现有这两个参数，不接受额外字段、null 或非对象 arguments。arguments 的具体字段、缺省与 null 接受范围由实际远端 schema 决定。

引用可以在同 session 有效范围内复用，不是每调用一次就重新查询。已确认引用失效时重新查询；其他拒绝按其实际原因处理，不一律通过重查恢复。重新查询不意味着重复此前结果不确定的业务操作。读取 NEXT 不执行这个动作或恢复其调用资格。

### 3.2 原生调用与结果

模型已从原生查询或准备 Cell 的输出取得足以构造 query 的说明和有效 MCP_TOOL_REF，即可直接原生调用：

```text
工具：call_mcp_tool
参数：
{
  "target":{"kind":"MCP_TOOL_REF","tool_ref":"mcpref:<查询返回的ID>"},
  "arguments":{"query":"安装方法"}
}
```

成功后的原生结果沿第 1 章，不回显参数中的引用。例如服务只返回一段文字，没有 structuredContent：

```text
meta:
  status: SUCCESS

content:
  找到安装指南。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整结果的导出 Target>
next: null
```

### 3.3 JS 调用与图文组合

以下是模型已经读过接口说明后编写的业务 Cell。target 原样使用同 session 先前查询返回的引用，无需在 Cell 内再次查询：

```javascript
const target = {kind: "MCP_TOOL_REF", tool_ref: "mcpref:<先前查询返回的ID>"};
const result = await tools.call_mcp_tool({
  target,
  arguments: {query: "安装方法"}
});
console.log({
  content: [
    {type: "text", value: "以下是选用的 MCP 内容：\n"},
    ...result.content
  ]
});
```

上例仅交付脚本选中的 content，不自动补 structuredContent。直接 console.log(result) 或显式 return result 则交付脚本实际返回的完整值；共同图片项在任意嵌套位置按既定规则识别，structuredContent 中普通 SDK image/data 对象或 Base64 字符串不被解析为真实图片。业务 JSON 原值不因包含相似字段而被修改。

业务错误仍可取得完整失败值并显式交付：

```javascript
try {
  const target = {kind: "MCP_TOOL_REF", tool_ref: "mcpref:<先前查询返回的ID>"};
  const result = await tools.call_mcp_tool({
    target,
    arguments: {query: "安装方法"}
  });
  console.log(result);
} catch (err) {
  if (err.result !== undefined) {
    console.log(err.result);
  } else {
    console.log(String(err.message ?? err));
  }
}
```

引用和接口说明沿第 2 章事先取得；此例不重新查询。err.result 的图片和 structuredContent 仍完整可访问；不能用成功 output_schema 假定失败值的形状，不要求为恢复失败对象而先读取文字正文。

## 4. read_mcp_resource：按需列目录与读取资源

### 4.1 两种 Target 分支

本工具只读，ROOT 与子代理沿各自当前可见服务范围使用，ROOT 适用 Code Mode 时也可调用 tools.read_mcp_resource。query_capabilities({kind:"MCP"}) 先给出服务连接及已知资源数量；本工具按需取得资源目录或具体内容，不把资源重新加入能力查询的默认目录。

| target.kind | 行为 | 本次业务范围 |
| --- | --- | --- |
| MCP_CONNECTION | 列该服务的资源和资源模板 | 当前范围内已接纳目录快照的全部资源／模板记录，不读取各资源正文 |
| MCP_RESOURCE | 读取具体 URI | 本次 resources/read 实际返回的完整 contents，可能包含多个资源或子资源 |

目录分支复用现有发现与目录 owner，不另建发现器；不隐式安装、启用、连接、授权或重新 listing。尚未取得可靠目录时如实说明，不用空列表断言远端没有资源。具体读取继续复用现有 SDK、连接准入、权限、取消和读取超时，不经 tools/call，也不要求 MCP_TOOL_REF。

### 4.2 参数与最小 Target

只接受必填 target，不增加 action、scope、kind、limit、游标或展示参数，不接受 null 和额外字段：

```typescript
type McpConnectionTarget = {
  kind: "MCP_CONNECTION";
  config_path: string;
  server_id: string;
};

type McpResourceTarget = {
  kind: "MCP_RESOURCE";
  server_id: string;
  uri: string;
};

type ReadMcpResourceArgs = {
  target: McpConnectionTarget | McpResourceTarget;
};
```

MCP_CONNECTION 沿能力查询的共同 Target：config_path 定位配置文件，server_id 定位该文件中的服务声明，Plugin 同样按原 owner 解析。MCP_RESOURCE 的 server_id 则是目录返回的实际运行服务标识，uri 是具体资源地址。目录给出正确 Target 或模板的实际 server_id，模型不自行把 Plugin 配置内的服务键转换成运行标识；不额外公开 MCP_SERVER 或内部实例身份。

删除公开的 MCP_RESOURCE_TEMPLATE 读取分支及顶层 uri。模板以目录记录提供 uri_template，模型填写具体 URI 后使用 MCP_RESOURCE。现有具体 URI 读取已经支持已发现资源或匹配已发现模板；保留这一范围校验及原 URI 有效性边界，不增加模板选择证明、调用引用或已读资格。server_id＋uri 分别确定服务和地址，不能因不同服务使用相同 URI 就省掉服务定位。

### 4.3 精简返回类型与正文接入

两个分支均只返回 meta.status 和 content，不返回业务顶层 target，不回显连接，不另套 trust／result／contents。原生时间、tool_result 和 next 沿共同外壳；JS 取得完整业务值，不带这些展示字段。

| 分支 | content 内容 |
| --- | --- |
| 资源目录 | text 说明及 object 记录；具体资源给 Target，模板给实际 server_id 与 uri_template |
| 具体读取 | 按资源原顺序排列的 text／image；每项先给 URI 和实际 MIME 的文字说明，再给该项正文或图片 |

```typescript
interface McpResourceRecord {
  target: McpResourceTarget;
  name: string;
  description?: string;
  mime_type?: string;
}

interface McpResourceTemplateRecord {
  server_id: string;
  uri_template: string;
  name: string;
  description?: string;
  mime_type?: string;
}

type McpResourceDirectoryRecord = McpResourceRecord | McpResourceTemplateRecord;
type McpResourceDirectoryPart =
  | McpTextPart
  | {type: "object"; value: McpResourceDirectoryRecord};

interface McpResourceDirectoryResult {
  meta: {status: "SUCCESS" | "ERROR" | "CANCELLED"};
  content: McpResourceDirectoryPart[];
}

interface McpResourceReadResult {
  meta: {status: "SUCCESS" | "ERROR" | "CANCELLED"};
  content: McpContentPart[];
}

declare function read_mcp_resource(
  args: {target: McpConnectionTarget}
): Promise<McpResourceDirectoryResult>;
declare function read_mcp_resource(
  args: {target: McpResourceTarget}
): Promise<McpResourceReadResult>;
declare function read_mcp_resource(
  args: ReadMcpResourceArgs
): Promise<McpResourceDirectoryResult | McpResourceReadResult>;
```

类型也覆盖异常通道中实际存在的完整失败业务值；ERROR／CANCELLED 不作为正常成功返回。目录的可选描述和 MIME 只在服务实际提供时保留，不补空占位。具体资源不在 Target 外重复 server_id／uri，模板不假造可直接读取的 Target；模板数量不是它能展开出的资源数量。

ReadResourceResult 的正式数组叫 contents，其元素是 TextResourceContents 或 BlobResourceContents，不套用 CallToolResult 的 content／structuredContent／isError 合同：

| 正式资源内容 | 接入规则 |
| --- | --- |
| TextResourceContents.text | 原字符串直接成为 text，保留换行，不解析 JSON 文字或扫描正文中的伪图片 |
| 图片类型的 BlobResourceContents.blob | 逐项解码，复用图片 owner 核对实际格式与 MIME，保存真实资产后形成 image／IMAGE_REF |
| 未提供 MIME 的 BlobResourceContents.blob | 复用现有图片 owner 识别；能识别并通过图片校验时形成 image，无法识别为图片时按未知二进制完整保留为文字 |
| 其他二进制 blob | 确定性文字保留 URI、实际 MIME 及完整 Base64 数据，不自动解析文档、播放音视频或写入本地文件 |

每个资源的 URI 和 MIME 说明在其内容前，MIME 未提供时省略该说明部分；正文自身不加标题、不改换行。图片逐项处理，坏图在所属位置保存实际 text 原因并明确“未交付实际图片”，其他资源和有效图片继续保留；没有真实资产就不生成 IMAGE_REF。不能把资源目录元数据中的 URI／MIME 或普通文字自动升级成图片。

服务明确声明图片类型，或内容已可靠识别为图片后，图片验证失败按局部接入 ERROR 处理。缺 MIME 的 PDF 或其他无法识别为图片的字节保留完整 Base64 文字，不因图片 owner 无法识别而报告坏图。识别和校验复用现有图片 owner。

不新增 structuredContent。resultType、ttlMs、cacheScope 及内部 _meta 不进入公开业务值，协议与必要缓存事实沿原 owner 处理，不新建缓存机制。目录记录使用 object 是导航数据；具体读取不把服务正文推断为 object 或 Target。所有接入内容只保存一份共同正文，图片不另存原始 Base64 正文或整份原始响应副本。

### 4.4 原生目录与读取示例

连接 Target 来自 query_capabilities 的服务记录；以下为独立连接示意：

```text
工具：read_mcp_resource
参数：
{
  "target":{"kind":"MCP_CONNECTION","config_path":"/project/.pulsara/mcp.yaml","server_id":"docs"}
}
```

假设目录包含一个具体资源和一个模板，原生结果如下。object 使用共同缩进 JSON 投影，未展开资源正文：

```text
meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "MCP_RESOURCE",
      "server_id": "docs",
      "uri": "docs://guide"
    },
    "name": "使用指南",
    "mime_type": "text/markdown"
  }
  {
    "server_id": "docs",
    "uri_template": "docs://users/{name}",
    "name": "用户资料",
    "description": "name 为用户名。",
    "mime_type": "text/plain"
  }

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整资源目录的导出 Target>
next: null
```

原样使用资源 Target 读取正文：

```text
工具：read_mcp_resource
参数：
{
  "target":{"kind":"MCP_RESOURCE","server_id":"docs","uri":"docs://guide"}
}
```

相应业务值如下；没有 structuredContent 或原生时间外壳：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {"type": "text", "value": "资源：docs://guide\nMIME：text/markdown\n"},
    {"type": "text", "value": "# 使用指南\n先安装依赖。\n"}
  ]
}
```

原生结果为：

```text
meta:
  status: SUCCESS

content:
  资源：docs://guide
  MIME：text/markdown
  # 使用指南
  先安装依赖。

observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>

tool_result: <本次完整资源读取结果的导出 Target>
next: null
```

根据上述模板读取 alice 的资料时，只需将 target.uri 填为 docs://users/alice，server_id 使用模板记录给出的实际值，不再提交 uri_template。返回多项资源时按服务原顺序逐项保留；图片项在原生按共同图片投影交付，不只显示 Base64。

### 4.5 JS 完整返回与模型可见信息

可先原生查目录，也可用准备 Cell 取得目录并显式输出：

```javascript
const directory = await tools.read_mcp_resource({
  target: {
    kind: "MCP_CONNECTION",
    config_path: "/project/.pulsara/mcp.yaml",
    server_id: "docs"
  }
});
console.log(directory);
```

directory.content 的 object.value 使用第 4.3 节的目录记录。模型实际收到目录后，再选择资源或填写模板 URI；不能把仅存在于 JS 内存中的未知模板当成模型已看过。已知具体资源 Target 时可以直接读取，无须重复列目录：

```javascript
const result = await tools.read_mcp_resource({
  target: {kind: "MCP_RESOURCE", server_id: "docs", uri: "docs://guide"}
});
console.log(result);
```

读取图片资源时，result.content 中的 image 项使用真实 IMAGE_REF；脚本可以选取、组合或输出完整结果。console.log／return 的任意层级图片识别、模型能力投影和独立 steer 交付沿共同规则，未输出的中间值不自动交给模型。

有完整失败值时可显式输出它：

```javascript
try {
  const result = await tools.read_mcp_resource({
    target: {kind: "MCP_RESOURCE", server_id: "docs", uri: "docs://guide"}
  });
  console.log(result);
} catch (err) {
  console.log(err.result !== undefined ? err.result : String(err.message ?? err));
}
```

### 4.6 空结果、失败与冻结交付

| 情况 | 返回语义 |
| --- | --- |
| 目录完整、没有可见资源和模板 | SUCCESS，content 为一句 text：“该服务当前没有可见资源或资源模板。” |
| 目录尚未取得、查询不完整或实际不可读取 | ERROR，说明实际原因，保留本次已可靠取得的记录；不以空列表表示未知 |
| 连接 Target 不存在、超出范围或具体 URI 不符合当前资源／模板范围 | 沿实际校验结果报告错误，不伪造资源 Target、正文或图片 |
| resources/read 完成且 contents 为空 | SUCCESS，content:[]，保留真实空响应，不猜测资源不存在 |
| RPC 拒绝、通信失败或超时 | ERROR，说明实际原因及已知派发事实，不将正文包含 error 当作协议错误，也不自动重新请求 |
| 图片接入失败 | ERROR，失败位置为 text，保留其余有效内容，JS 通过 err.result 取得完整失败值 |
| 取消 | 仅实际结算为取消时使用 CANCELLED；已派发后取消到达，但最终取得确定成功或错误结果时保留真实结果。已接受的读取继续按原许可结算，不以取消到达证明请求未发出 |

没有产生业务值的早期失败不伪造 err.result；结果保存或交付失败沿共同规则报告。原生模型不支持图片或图片读取失败时，按统一能力投影返回 ERROR 和原位文字说明；已保存的 image 项及 JS 业务值保持原义。

两个分支均完整保存本次目录或资源读取结果，原生再按 12k／NEXT 顺序分页，目录 object 的 JSON 投影可以跨页；JS 返回完整类型化值，不分页。NEXT 和导出只读本次冻结结果，不重新 listing、读取配置、刷新目录、请求 resources/read 或展开更多模板实例。源资源和目录后来发生变化，不改写已保存内容；需要新观察时显式重调原工具。

## 5. 冻结交付与实施核对

call_mcp_tool 或具体资源读取的一次成功响应可以只包含远端的一个业务范围；完整结果表示本次远端响应完整接入保存，不承诺自动取得整个远端数据集。外部 cursor、next、has_more、jobId 或运行状态保留在原业务值里，不变成 Pulsara 控制字段。资源目录分支则完整交付当前已接纳目录快照，不拿协议 listing 的第一页伪称完整目录。

原生首屏、NEXT 与导出都由同一已保存结果产生。content 及 call_mcp_tool 实际存在的 structuredContent 的可读投影按确定顺序纳入 12k 顺序分页；图片不可拆，超大单图独占页规则保持。object／结构化值的 JSON 文字可跨页，但 JS 始终访问完整值，不返回 JSON 分片或字段游标。NEXT 只读冻结结果，不重新 tools/call、resources/read 或 listing，不自动追远端下一页、不重新执行有副作用操作。

call_mcp_tool 的原生与 JS 入口复用现有 meta executor、SDK 校验、实际远端调用、结果接入及保存路径。删除旧原生 body／read 指引及重复公开字段按共同 hardcut 处理；只适配正式 content，不另存原始 JSON、副本正文或一套 META 结果。structuredContent 沿现有 public_fields 业务字段职责保存，content 使用共同有序内容行，不在 public_fields 再放一份 content。已有内部执行身份、协议控制及取消结算事实仍由原 owner 保留，不因公开字段减法删除。

查询与新调用解析读取当前已接纳目录，复用现有通知、初次发现、完整刷新、已配置周期刷新和重连路径。目录变动通知先暂挡新准入、排空已接纳调用后再更新 SDK schema／header 缓存，不把这条实际执行边界随原生兼容和降级体系删除。只有完整刷新结果才能确认具体工具移除；失败不伪造删除。没有通知或观察后才发生的远端变化不保证能提前发现，不新增逐调用 listing。已准备或已接纳调用保持原执行器、参数及许可，不静默转发新目标；已接纳操作继续结算，不自动取消重做。

业务执行与展示预算分离，删除因原生结果页预算不足而在 MCP 派发前拒绝调用的旧检查。实际物理资源、SDK、权限及协议边界仍由原 owner 负责，真实失败明确报告；不新增总量 cap，不截断业务值、不伪造完整保存，也不自动重做。两个入口均按真实结算结果确定公开状态；取消请求到达不改写已确定的 SUCCESS／ERROR 或已保存内容。原生与 JS 的共同结果不改变当前 epoch 的 SYSTEM／provider tools 和 messages 前缀连续性。

实施核对至少覆盖：删除远端 direct 安装、兼容、绑定及执行分支，保留内置原生安装和前缀冻结；ROOT／子代理统一 meta；同 session 原生与不同 Cell 共用引用、接纳后可调用、无每 epoch 1,024 个引用上限；原生与 JS 同形业务值；纯文字、纯图、多图和图文交错；混合正常图与坏图逐项处理，失败位置为 text、没有假引用，其他有效图片真实保存并回读，接入 ERROR 的 err.result 保留完整剩余内容；structuredContent 缺失/null/对象/数组/标量；JSON text 不解析；结构化值内相同 Base64 原样保留；业务失败携图及不符合成功 schema 的失败值；原生 12k／NEXT、JS 完整值和脚本显式 steer；引用失效、协议/通信失败及确定副作用不重放。

资源另按第 4 章核对：两种 Target 分支与固定 JS 返回类型、独立／Plugin 连接映射及各调用范围、资源与模板共同目录、未知目录不冒充零项、具体资源 Target 及模板实际 server_id、模板实例不再要求模板 Target、文本与多资源顺序、图片 blob 接入及坏图保留其余结果、缺 MIME 的有效图片接入和无法识别内容的完整二进制文字保留、真实空 contents、读取失败和取消；已派发读取取得确定结果时不被后到取消改写；NEXT 不重新列目录或请求资源，不依赖工具调用引用，也不自动展开模板实例。

引用变化另覆盖：A 变化只影响其引用、B 新增或改变不影响未变 A、同目标普通重连复用、实际地址／账户变化拒绝旧资格、刷新失败不误报删除、未知变化按真实结果处理；当前目录查询与历史 NEXT 分离；已接纳调用按原许可结束，未派发与已派发失败准确说明。仅验证原生兼容分类和降级的测试随旧实现删除，这些实际产品行为仍需验证。

## 6. 当前生产实现参考

- [当前元工具参数与说明](../src/pulsara_agent/capability/builtin_catalog.py)
- [能力目录、查询结果及路由](../src/pulsara_agent/conversation_kernel/capability_query.py)
- [待收敛的混合调用、参数验证和公共结果入口](../src/pulsara_agent/conversation_kernel/tool_runtime.py)
- [元工具引用解析、权限及执行器绑定](../src/pulsara_agent/conversation_kernel/tool_runtime_authorization.py)
- [公共结果转换与已派发操作结算](../src/pulsara_agent/conversation_kernel/tool_runtime_results.py)
- [SDK 调用及现有 CallToolResult 序列化](../src/pulsara_agent/conversation_kernel/mcp/supervisor.py)
- [现有资源与模板发现](../src/pulsara_agent/conversation_kernel/mcp/discovery.py)
- [资源读取及 URI／模板校验](../src/pulsara_agent/conversation_kernel/mcp/standard_operations.py)与[URI owner](../src/pulsara_agent/conversation_kernel/mcp/resource_uris.py)
- [SDK 输出 schema 校验及协议边界](../src/pulsara_agent/conversation_kernel/mcp/sdk_facade.py)
- [当前业务值解析](../src/pulsara_agent/conversation_kernel/tool_contracts.py)
- [Code Mode 正常值与异常结果交付](../src/pulsara_agent/conversation_kernel/tool_execution.py)

以上仅用于核对现状，不表示本草案已实施；SDK 负责协议和内容类型，Pulsara 只承担既定过滤、结果适配、存储及模型投影，不复制解析器或新增执行框架。
