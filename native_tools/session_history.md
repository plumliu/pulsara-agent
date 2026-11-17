# Pulsara 历史会话工具修订与使用教程草案

共用规则见[原生工具修订草案索引](../PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章。

状态：当前合同已接入生产代码，正在完成 hardcut 跨层验收；正常使用仍按实施计划停用。本族工具以[有序内容统一存储 hardcut 草案](../PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md#10-数据库优先与停用期间的分步实施)为存储基础，记录已经确定的参数、业务返回值及原生／JS 用法。共同存储、原生分页和 JS 完整返回使用当前单一路径；整体验收状态以实施计划为准。

Subagent 的本次初始输入顺序沿数据库草案：主指令在前，随后为 ROOT 选入的历史材料、依赖结果和其他材料，initial_entry_id 指主指令；只组装新增输入，不重排已有上下文。主指令和材料的来源身份仍然保留，不能仅因都投影为 user role 就将材料另算成开启 turn 的主指令；检索纳入范围继续沿各入口合同，不因内容存储统一而自动扩大。

## 1. search_sessions：查找会话

### 1.1 用途与范围

按关键词查找会话；省略 query、传入空字符串或全空白字符串时，按最近活动列出会话。lifecycle 用于选择全部、未归档或已归档会话，是业务筛选条件，不是交付分页参数。

搜索范围沿现有记忆域边界：查询调用者同一 memory_domain_id 下的可访问会话，可跨工作区，不按当前 workspace_id、工作目录或目录前缀筛选。lifecycle 只筛选会话生命周期；结果里的工作区路径用于辨认会话，不自动构成搜索范围条件，不增加路径筛选参数。权限和工具可用性继续沿既有合同，不扩大跨记忆域读取。

当前会话的正文搜索仍限定在已压缩出当前上下文的较早内容，并排除当前活动请求；其他可访问会话使用其已保存的可读正文。标题匹配不依赖当前会话的正文范围。

ENTRY 匹配使用与后两个工具相同的对话正文入口，保留用户消息、补充及助手公开正文，排除工具调用、工具结果、思考和思考摘要，不直接搜索共同表中的所有 text。

search_sessions 只返回会话信息和命中来源，不附带具体 entry；模型可在选定会话内搜索相关 QA／QAQ 摘录，摘录不足时再按需展开原文，不要求逐条读取 entry。search_session_content 和 read_session_content 的参数及结果见第 2、3 节。状态、空结果和失败值沿第 3.7 节及共用规则，不将当前生产实现的结果包装视为已采用合同。

### 1.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| query | string | 可选，默认空字符串 | 会话搜索关键词；省略、空字符串或全空白字符串时按最近活动列出 |
| lifecycle | ALL／OPEN／ARCHIVED | 可选，默认 ALL | 查询全部、未归档或已归档会话 |

```typescript
interface SearchSessionsArgs {
  query?: string;
  lifecycle?: "ALL" | "OPEN" | "ARCHIVED";
}
```

只接受 query、lifecycle，不接受 null 或额外字段。删除 limit、cursor 及要求重新执行搜索的续查参数；本次业务结果完整取得和保存，原生沿统一 12k 分页，JS 取得完整业务值。next 只补读已保存结果，不重新执行搜索。具体会话记录按第 1.4 节表达，完整指本次会话结果列表，不表示返回命中 entry 的正文。

### 1.3 参数用法

原生关键词查询：

```text
工具：search_sessions
参数：
  query: "视频 分页"
  lifecycle: "ALL"
```

JS 使用同一参数：

```javascript
const result = await tools.search_sessions({
  query: "视频 分页",
  lifecycle: "ALL"
});
```

不指定关键词，列出未归档会话：

```text
工具：search_sessions
参数：
  lifecycle: "OPEN"
```

两个参数都省略时，按最近活动列出全部生命周期范围内的会话：

```text
工具：search_sessions
参数：{}
```

以上展示调用参数，返回记录及交付示例见第 1.4、1.5 节。

### 1.4 会话记录与命中来源

content 中每个 object 项代表一个会话，同一会话只返回一项。会话信息使用 SESSION Target、title、workspace_path、updated_at；删除 project，workspace_path 始终表达该会话所属工作区的实际绝对根路径，不混用工作区显示名称。顶层不重复返回一个会话 Target，也不保留 items 数组包装。

有关键词时，对每个会话优先判断是否存在匹配 entry；存在则 matched_in 为 ENTRY。只有没有匹配 entry 时才用标题匹配兜底，命中时 matched_in 为 TITLE。无论匹配多少条 entry，同一会话都只返回一项，不选择或附送“最近命中的一条”。省略 query、传入空字符串或全空白字符串时属于列表查询，不返回 matched_in。

| value 字段 | 类型 | 含义 |
| --- | --- | --- |
| target | SESSION Target | 后续会话内搜索或读取使用的会话引用 |
| title | string | 会话标题，沿现有显示标题规则处理空标题 |
| workspace_path | string | 该会话所属工作区的实际绝对根路径，仅用于辨认会话 |
| updated_at | string | 最近活动时间，使用带时区的 ISO 时间字符串 |
| matched_in | ENTRY／TITLE，可省略 | 非空关键词查询时的命中来源；列表查询省略 |

```typescript
type SessionTarget = {
  kind: "SESSION";
  session_id: string;
};

type SearchSessionRecord = {
  target: SessionTarget;
  title: string;
  workspace_path: string;
  updated_at: string;
  matched_in?: "ENTRY" | "TITLE";
};

interface SearchSessionPart {
  type: "object";
  value: SearchSessionRecord;
}

interface SessionTextPart {
  type: "text";
  value: string;
}

interface SearchSessionsSuccess {
  meta: {status: "SUCCESS"};
  content: (SessionTextPart | SearchSessionPart)[];
}

interface SearchSessionsFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: (SessionTextPart | SearchSessionPart)[];
}
```

不返回 entry／hit 对象、命中 entry 的 Target、说话人、正文、摘录或摘录截断标记。不保留重复的 guidance、entry.read 或完整后继工具调用。ENTRY 只说明会话因正文匹配被找到，不赋予某条消息单独的返回位置；下一步 search_session_content 返回相关 QA／QAQ 摘录及其 Q₁ 定位。

### 1.5 原生投影与 JS 完整值

假设关键词查询找到一个正文命中的会话，且结果可完整装入一页。原生将整个 object.value 投影为两空格缩进 JSON，不特判 target：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际查询耗时>

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "SESSION",
      "session_id": "session:<会话ID>"
    },
    "title": "通用视频理解",
    "workspace_path": "/workspace/pulsara_agent",
    "updated_at": "2026-11-12T08:00:00+00:00",
    "matched_in": "ENTRY"
  }

tool_result: <完整结果的导出 Target>
next: null
```

相同结果进入 JS 的完整业务值如下。时间及路径均为示例值，不是实际查询记录：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {
      "type": "object",
      "value": {
        "target": {"kind": "SESSION", "session_id": "session:<会话ID>"},
        "title": "通用视频理解",
        "workspace_path": "/workspace/pulsara_agent",
        "updated_at": "2026-11-12T08:00:00+00:00",
        "matched_in": "ENTRY"
      }
    }
  ]
}
```

标题兜底命中时，记录形状相同，仅 matched_in 为 TITLE；无关键词列表查询使用同一记录形状并省略 matched_in。三种情况都不附带具体 entry。

```javascript
const result = await tools.search_sessions({
  query: "视频 分页",
  lifecycle: "ALL"
});

for (const part of result.content) {
  if (part.type === "object") {
    console.log(part.value.title, part.value.target);
  } else {
    console.log(part.value);
  }
}
console.log({content: result.content});
```

JS 取得本次完整业务值，不带原生观察外壳、tool_result 或 next。原生较大结果沿统一 12k 分页；补页、导出或显式输出这份结果都不自动搜索 entry 或展开消息正文。

本族工具的原生页可以停在 object 的 JSON 文字投影中间，NEXT 从该处继续；不要求单条记录整项装页，不增加 partial 字段。JS 始终取得完整对象，原生跨页片段不作为完整 JSON 对象解析。

## 2. search_session_content：会话内搜索 QA／QAQ

### 2.1 用途与问答单位

在选定会话内搜索相关对话，沿用与隐式历史召回共用的 QA／QAQ 检索与摘录语义，不改成逐条 entry 独立匹配：

- Q₁ 是沿既有资格规则开启一轮对话的输入；主指令和材料的区别按本文件开头及数据库草案处理，不因共同存储增加新的 Q。
- A 是该轮允许纳入的可见对话过程，可包含多条助手消息及用户中途补充；保留各自身份。
- Q₂ 是紧接着的下一轮输入，不包含下一轮助手回答。

Q₁、A、Q₂ 均完整合格时使用 QAQ；Q₂ 不存在或不合格、但 Q₁ 和 A 完整合格时使用 QA。同一单元只选一种形态，不同时返回 QA 与 QAQ 两条记录。不完整合格的 Q₁ 或 A 不拼成残缺候选；资格判断与长度摘录仍是两件事。

每个单元沿现有规则生成最多 1024 个启发式 token 的摘录正文，必要时保留省略标记。角色、时间及 Target 等包装另计，不承诺整个 object 项严格小于 1024 token。保留原文顺序及说话者，不生成新的模型摘要。省略位置、批注及引用身份继续沿既有摘录规则处理。

正文范围遵循共同对话读取入口：保留用户消息、用户轮内补充、助手 commentary 和 final answer；排除工具调用、工具结果、思考及思考摘要。包含 TOOL_CALL 的助手 entry 仍保留其中的助手文字；不能因 entry 名称排除整条 commentary。运行时材料不因包含 text 或投影为 user role 自动成为对话正文。

搜索范围沿既有记忆域和历史可读边界；target 只选择其中一个会话，不扩大权限。当前会话仍只搜索已压缩出当前上下文的较早内容并排除当前活动请求；其他可访问会话沿已保存的可读范围。匹配、排序及隐式召回的选择与注入规则不因结果包装改变。

### 2.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | SESSION Target | 必填，放首位 | 要搜索的会话，使用 search_sessions 返回的会话 Target |
| query | string | 必填，不能是空字符串或全空白 | 在该会话的 QA／QAQ 对话单元中搜索的查询文字 |

```typescript
interface SearchSessionContentArgs {
  target: SessionTarget;
  query: string;
}
```

只接受 target、query，不接受 null 或额外字段。删除 include_tools、limit、cursor 及要求重新执行搜索的续查参数。本次搜索取得完整业务结果后保存，原生按 12k 分页，JS 返回完整值。每个单元的 1024-token 摘录规则继续生效，不把旧结果页长保留为本次业务结果条数上限。

### 2.3 四种消息标签与顺序

QA／QAQ 的 text 使用以下四种标签，标签来自实际消息职责，不靠正文措辞猜测：

| 标签 | 含义 |
| --- | --- |
| user message | 开启一轮的对话输入；沿既有来源资格判断，不把所有 user role 都归为真人输入 |
| user steer | 轮内用户补充 |
| assistant commentary | 助手过程中的公开文字，包括与工具调用同条保存的文字 |
| assistant final answer | 该轮的最终回答，使用既有最终回答关联判断 |

Q₁、Q₂ 都使用 user message，不使用“用户后续”或额外 Q₂ 标签；QA／QAQ 只决定选择的范围，不引入另一套消息标签。标签及时间写入摘录 text，不另存一份结构化消息列表。

摘录按原 entry 顺序正向排列，不按标签分组或重排。下面是一段完整顺序的示意，时间省略仅为缩短展示：

```text
user message:
工具结果怎么分页？

assistant commentary:
我先检查当前实现。

user steer:
先不要改代码。

assistant final answer:
原生按 12k 分页，JS 返回完整业务值。

user message:
那下一页怎么读取？
```

本例最后一条 user message 是 Q₂；user steer 是该轮回答结束前的补充。实际顺序来自保存的 entry，不为凑齐四种标签补造消息，不要求每个单元出现全部标签。原有时间信息继续保留，例如 `user message (2026-11-12T08:00:00+00:00):`。

### 2.4 最简结果字段与类型

content 是有序内容项数组，每个命中的 QA／QAQ 单元用 `{type:"object", value:{target, text}}` 表达，无结果时使用第 3.7 节的 text 说明。不是裸 object 数组，也不是额外嵌套的 messages 数组。

| value 字段 | 类型 | 含义 |
| --- | --- | --- |
| target | 单 ID 的 SESSION_ENTRY Target | 指向本单元的 Q₁，作为按需展开历史原文的起点；不表示关键词一定命中 Q₁，也不表示只读取 Q₁ 一条消息 |
| text | string | 本单元的 QA／QAQ 原文摘录，包含四种消息标签中实际存在的标签、时间与必要省略说明 |

```typescript
type SessionEntryTarget = {
  kind: "SESSION_ENTRY";
  entry_id: string;
};

interface SearchSessionContentRecord {
  target: SessionEntryTarget;
  text: string;
}

interface SearchSessionContentPart {
  type: "object";
  value: SearchSessionContentRecord;
}

interface SearchSessionContentSuccess {
  meta: {status: "SUCCESS"};
  content: (SessionTextPart | SearchSessionContentPart)[];
}

interface SearchSessionContentFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: (SessionTextPart | SearchSessionContentPart)[];
}
```

本族 SESSION_ENTRY Target 只接受 kind、entry_id，不再携带 session_id。entry_id 是全局唯一定位；runtime 查询其所属会话，并沿实际会话检查记忆域及历史可读范围，不将省略 session_id 解释为默认当前会话，也不凭 ID 格式授予访问资格。指向会话的 SESSION Target 仍使用 session_id。

每条记录只保留 Q₁ Target 和 text，不并列返回 title、score、messages、逐消息 Target、read 调用建议、guidance 或 QA／QAQ 分类字段。QA 与 QAQ 由摘录中实际包含的消息表达。外层不重复一个会话 Target，不套 items 包装。

Q₁ Target 是原文阅读起点，不是 NEXT，也不是完整搜索结果的导出 Target。使用它展开原文是一次业务读取；原生 next 只补读已经保存的本次搜索结果，二者不能互换。

### 2.5 原生投影与 JS 完整值

原生调用只传会话 Target 和查询文字，target 放首位：

```text
工具：search_session_content
参数：
  target: {"kind":"SESSION","session_id":"session:<会话ID>"}
  query: "工具结果 分页"
```

以下为一条 QAQ 摘录的 JS 完整业务值，Q₁ Target 只有 entry_id。各消息时间省略仅为缩短示例：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {
      "type": "object",
      "value": {
        "target": {
          "kind": "SESSION_ENTRY",
          "entry_id": "entry:<Q1条目ID>"
        },
        "text": "user message:\n工具结果怎么分页？\n\nassistant commentary:\n我先检查当前实现。\n\nuser steer:\n先不要改代码。\n\nassistant final answer:\n原生按 12k 分页，JS 返回完整业务值。\n\nuser message:\n那下一页怎么读取？"
      }
    }
  ]
}
```

原生按共同 object 投影规则，保留 JSON 字段和字符串转义，不将 target 特判拆出；本例结果可完整装入一页：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际查询耗时>

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "SESSION_ENTRY",
      "entry_id": "entry:<Q1条目ID>"
    },
    "text": "user message:\n工具结果怎么分页？\n\nassistant commentary:\n我先检查当前实现。\n\nuser steer:\n先不要改代码。\n\nassistant final answer:\n原生按 12k 分页，JS 返回完整业务值。\n\nuser message:\n那下一页怎么读取？"
  }

tool_result: <完整结果的导出 Target>
next: null
```

JS 调用及显式组合：

```javascript
const result = await tools.search_session_content({
  target: {kind: "SESSION", session_id: "session:<会话ID>"},
  query: "工具结果 分页"
});

for (const part of result.content) {
  if (part.type === "object") {
    console.log(part.value.target, part.value.text);
  } else {
    console.log(part.value);
  }
}
// 也可以选择整份摘录内容交给 ROOT。
console.log({content: result.content});
```

QA 使用相同记录形状，text 不含 Q₂；多条命中依次放入 content，不重复增加组级身份。搜索结果整体按原生 12k 规则交付，页边界不改变候选的摘录内容；NEXT 补读的是其余已保存结果，不重新搜索。JS 取得本次完整业务值，在 type 为 object 时直接访问 `part.value.target` 和 `part.value.text`，不带原生时间外壳、tool_result 或 next。

“完整业务值”指完整返回本次搜索取得的全部问答摘录与定位，不表示取消每候选的 1024-token 摘录规则，也不表示已返回各问答单元的完整历史原文。显式 console.log／return 沿既定 Cell 交付规则，不另套原生 12k 页预算。

## 3. read_session_content：按需展开历史原文

### 3.1 用途与范围

本工具是可选步骤。search_session_content 返回的 QA／QAQ 摘录已经足以回答时，模型可以直接使用，不要求随后调用 read_session_content，不要求逐条精读 entry。

需要查看被摘录省略的细节、核对精确原文或了解前后讨论时，模型可将 Q₁ Target 或已经取得的其他对话 entry Target 作为起点，按指定方向展开连续历史正文。读取范围不强行限定为一个 QA／QAQ 单元。

读取与搜索使用相同的对话正文选择规则：保留用户消息、用户轮内补充、助手 commentary 和 final answer，排除工具调用、工具结果、思考及思考摘要。混有调用的助手 entry 只读取其公开正文，不因 entry 名称漏掉 commentary。运行时材料继续按真实来源及既有资格处理，不因共同存储或 role=user 自动纳入。

记忆域、跨工作区会话读取及当前会话的历史可读范围保持既有合同。SESSION 的 latest 指本次可读范围内的最新消息，不是绕过当前上下文限制读取数据库最新一行。

### 3.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | SESSION 或单 ID 的 SESSION_ENTRY Target | 必填，放首位 | SESSION 从本次可读范围的最新消息起读；SESSION_ENTRY 从指定消息起读，包含该条 |
| limit | 非零整数 | 必填，无默认值 | 符号表示阅读方向，绝对值表示本次最多读取的消息条数；SESSION 只接受负数，SESSION_ENTRY 接受正数或负数 |

```typescript
interface ReadSessionContentArgs {
  target: SessionTarget | SessionEntryTarget;
  limit: number; // 必填整数；SESSION 必须 < 0，SESSION_ENTRY 必须 != 0
}
```

只接受 target、limit，不接受 null 或额外字段。limit 省略、为 0、不是整数，或 SESSION 配正数均为参数错误。类型声明中的 number 不代替这些实际校验；不增加方向参数或另一套正负数包装类型。

删除 include_tools、direction、max_chars、cursor 及要求重新调用本工具续页的参数。不设置任意消息条数上限；真实资源无法完成时明确失败，不静默减少 limit、不以原生预算提前结束业务读取。

read_file 保持现有合同：负向 offset 从文件尾部定位后仍正向读取，limit 仍为可选正整数；本工具的正负 limit 不扩展到文件工具。

### 3.3 计数、方向与边界

一条具有本工具可读对话正文的 entry 算一条消息，同一 entry 的多个文字块仍算一条。纯工具调用、工具结果、思考及没有可读正文的 entry 不计数；正文中的空白和原始换行不因此被删除或归一化。历史图片及批注沿既有历史文字投影处理，不把一个图片说明另算成一条消息。

正数按 entry 顺序向后读取，负数向前读取；均包含起点。负向读取按从新到旧的顺序返回消息，单条消息内部的正文仍保持原顺序，不倒转字符、段落或内容项。

假设可读消息按保存顺序编号为 1～10，以下数字仅解释示例，不是公开参数：

| target | limit | 返回的消息顺序 |
| --- | --- | --- |
| SESSION | -3 | 10、9、8 |
| SESSION_ENTRY 指向第 5 条 | 3 | 5、6、7 |
| SESSION_ENTRY 指向第 5 条 | -3 | 5、4、3 |
| SESSION_ENTRY 指向第 2 条 | -5 | 2、1，已到可读开头 |
| SESSION_ENTRY 指向第 9 条 | 5 | 9、10，已到本次可读末尾 |

遇到可读范围边界时按实际条数结束；不会为了凑够条数越界，也不返回“来源还有后续”字段。SESSION_ENTRY 必须定位一条实际存在、获准且可读的消息；不能定位失败后悄悄改成从 latest 读取，也不能把不可读的纯工具 entry 当作成功起点。

调用时确定实际可读范围，并取得 limit 选择的完整内容；参数和该次实际内容保存后锁定。之后会话新增消息、发生压缩或活动范围改变，不让 NEXT 重新解析 SESSION 的 latest、重新选择消息或改变已保存正文。续读仍沿既有授权检查，不扩大读取范围。

原生 12k 只决定如何交付这份结果，与业务参数 limit 分开。例如 limit:-3 选择第 10、9、8 条，即使第一页只容纳第 10 条的一部分，NEXT 也只补读这三条已保存消息；不会继续读取第 7 条，也不会因出现第 11 条而重排。JS 一次取得本次选择的全部消息，不分页。

### 3.4 最简结果字段与类型

content 的每个 object 项代表一条读取到的消息，顺序与第 3.3 节一致：

| value 字段 | 类型 | 含义 |
| --- | --- | --- |
| target | 单 ID 的 SESSION_ENTRY Target | 这条消息的定位，只有 kind 和 entry_id，可作为后续业务读取的起点 |
| role | 四种消息标签之一 | user message、user steer、assistant commentary 或 assistant final answer，沿第 2.3 节确定 |
| at | string | 该条消息原有接受时间，带时区的 ISO 时间，不是本次读取时间 |
| text | string | 该条消息的完整可读正文，不加 QA／QAQ 摘录上限或摘录省略标记 |

```typescript
type SessionMessageRole =
  | "user message"
  | "user steer"
  | "assistant commentary"
  | "assistant final answer";

interface ReadSessionMessageRecord {
  target: SessionEntryTarget;
  role: SessionMessageRole;
  at: string;
  text: string;
}

interface ReadSessionMessagePart {
  type: "object";
  value: ReadSessionMessageRecord;
}

interface ReadSessionContentSuccess {
  meta: {status: "SUCCESS"};
  content: (SessionTextPart | ReadSessionMessagePart)[];
}

interface ReadSessionContentFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: (SessionTextPart | ReadSessionMessagePart)[];
}
```

顶层不重复会话 Target，不保留 items、内部 sequence、命中分数、partial、逐消息 read 指引或公开的范围游标。role 已表达消息标签，不再并列 label 或另一套角色分类。text 是用户／助手正文的历史阅读投影，不承诺包含被排除的调用、结果或思考。

历史图片在 text 的原位置使用短文字占位，例如 `[历史图片；此处未包含图像内容]`；已保存的展示图片可附展示项说明。图片项不附带 IMAGE_REF Target，不读取或投递真实图片，看到占位不表示模型已看到画面。搜索摘录及同会话、跨会话的历史读取都使用这项规则。每条消息的 target 仍为该消息的 SESSION_ENTRY，不另建 images 数组。原有文字、批注及引用的身份、原始文字和必要说明沿共同历史投影保留。

### 3.5 原生与 JS 示例

先给出这组示例共有的时间正序，用于核对读取方向：

| 消息 | 标签 | 正文 |
| --- | --- | --- |
| Q₁ | user message | 工具结果怎么分页？ |
| 过程消息 | assistant commentary | 我先检查当前实现。 |
| 补充消息 | user steer | 先不要改代码。 |
| 最终回答 | assistant final answer | 原生按 12k 分页，JS 返回完整业务值。 |
| Q₂ | user message | 那下一页怎么读取？ |

从 Q₁ 正向读两条，原生参数为：

```text
工具：read_session_content
参数：
  target: {"kind":"SESSION_ENTRY","entry_id":"entry:<Q1条目ID>"}
  limit: 2
```

原生按两空格缩进 JSON 投影每条 object；假设这次结果完整装入一页：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际读取耗时>

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "SESSION_ENTRY",
      "entry_id": "entry:<Q1条目ID>"
    },
    "role": "user message",
    "at": "2026-11-12T08:00:00+00:00",
    "text": "工具结果怎么分页？"
  }
  {
    "target": {
      "kind": "SESSION_ENTRY",
      "entry_id": "entry:<过程消息ID>"
    },
    "role": "assistant commentary",
    "at": "2026-11-12T08:00:01+00:00",
    "text": "我先检查当前实现。"
  }

tool_result: <完整结果的导出 Target>
next: null
```

JS 使用相同参数：

```javascript
const result = await tools.read_session_content({
  target: {kind: "SESSION_ENTRY", entry_id: "entry:<Q1条目ID>"},
  limit: 2
});
console.log({content: result.content});
```

result 的完整业务值为：

```json
{
  "meta": {"status": "SUCCESS"},
  "content": [
    {
      "type": "object",
      "value": {
        "target": {"kind": "SESSION_ENTRY", "entry_id": "entry:<Q1条目ID>"},
        "role": "user message",
        "at": "2026-11-12T08:00:00+00:00",
        "text": "工具结果怎么分页？"
      }
    },
    {
      "type": "object",
      "value": {
        "target": {"kind": "SESSION_ENTRY", "entry_id": "entry:<过程消息ID>"},
        "role": "assistant commentary",
        "at": "2026-11-12T08:00:01+00:00",
        "text": "我先检查当前实现。"
      }
    }
  ]
}
```

从会话 latest 向前读两条，原生参数为：

```text
工具：read_session_content
参数：
  target: {"kind":"SESSION","session_id":"session:<会话ID>"}
  limit: -2
```

本例依次返回 Q₂ 的 user message、此前的 assistant final answer。各条仍为上述完整 object，Target 分别指向对应 entry，at 和 text 均保持原值，不把 Q₂ 标成“用户后续”，也不为负向阅读将最终回答改成 commentary。

JS 可按同样顺序取得完整值并选择输出：

```javascript
const recent = await tools.read_session_content({
  target: {kind: "SESSION", session_id: "session:<会话ID>"},
  limit: -2
});
console.log({content: recent.content});
```

以最终回答为锚点向前读三条，原生参数为：

```text
工具：read_session_content
参数：
  target: {"kind":"SESSION_ENTRY","entry_id":"entry:<最终回答ID>"}
  limit: -3
```

本例返回最终回答、user steer、assistant commentary，包含锚点，不包含后面的 Q₂。JS 使用相同单 ID Target 和负数 limit：

```javascript
const preceding = await tools.read_session_content({
  target: {kind: "SESSION_ENTRY", entry_id: "entry:<最终回答ID>"},
  limit: -3
});
console.log({content: preceding.content});
```

上述 JS 结果都不携带原生时间外壳、tool_result 或 next。console.log／顶层 return 按脚本显式组合的内容交付，首版不另加原生 12k 页预算。

### 3.6 JS 返回声明与链路分工

以下是拟议 SDK 的成功返回声明；业务失败沿异常通道提供第 3.7 节的实际失败值，不以正常返回掩盖失败：

```typescript
declare namespace tools {
  function search_sessions(args: SearchSessionsArgs): Promise<SearchSessionsSuccess>;
  function search_session_content(args: SearchSessionContentArgs): Promise<SearchSessionContentSuccess>;
  function read_session_content(args: ReadSessionContentArgs): Promise<ReadSessionContentSuccess>;
}
```

会话发现、问答搜索和原文展开由模型按任务需要选择，不由 runtime 自动串行执行。Q₁ Target／逐消息 Target 是下一次业务读取的位置；NEXT 只读取冻结工具结果的未交付部分，不增加第四个会话续读工具。

本次业务读取返回的原文与原会话是两次不同业务发生的内容，完整保存本次结果符合数据库草案，不为避免文字相同而把阅读结果改成需要模型逐条解引用的清单。

### 3.7 状态、空结果与失败值

三个工具均沿共用规则使用 SUCCESS、ERROR、CANCELLED。错误原因、涉及的会话或 entry 及必要说明放在 content 的 text 项，不增加 meta.error、错误枚举、_hint 或并列 message 字段。

正常查询无结果时，使用 SUCCESS，content 只放一条 type:text 说明，不伪造 object 记录：

| 情况 | 说明文字 |
| --- | --- |
| search_sessions 无结果 | 没有找到匹配的会话。 |
| search_session_content 无结果 | 没有找到匹配的对话。 |
| read_session_content 使用 SESSION 且无可读历史 | 该会话当前没有可读的历史消息。 |

该结果完整保存和交付后，原生正常返回 tool_result 和 next:null；JS 按 type 区分文字说明与 object 记录。SESSION_ENTRY 不存在或不可读仍为 ERROR，不归为空结果。

失败时不伪造会话或消息属性。确实已产生业务结果时，JS 的 err.result 保留完整失败业务值，包括原因和实际已取得的业务记录；没有业务结果的参数或运行时失败，不伪造 err.result。原生错误正文若已正常保存，仍沿统一交付规则提供实际可读的完整结果 Target 和 next；保存／交付失败不得冒充正常读完，不自动重做查询。

## 4. 当前生产实现参考

- [历史会话工具当前参数及 Target 定义](../src/pulsara_agent/ports/session_content.py)
- [会话查询与结果包装当前实现](../src/pulsara_agent/conversation_kernel/session_content.py)
- [会话关键词搜索当前实现](../src/pulsara_agent/conversation_kernel/session_search.py)
- [QA／QAQ 规范内容构建](../src/pulsara_agent/conversation_kernel/history_projection.py)
- [显式与隐式共用的历史检索](../src/pulsara_agent/conversation_kernel/history_search.py)
- [历史问答摘录](../src/pulsara_agent/conversation_kernel/history_excerpt.py)
- [会话工具执行与错误适配](../src/pulsara_agent/conversation_kernel/tool_runtime_management.py)

这些链接用于核对当前实现及调试接点。参数、返回声明、执行值和 bundled skill 使用同一当前合同；完整应用的验收与恢复使用条件以实施计划为准。
