# Pulsara 记忆工具修订与使用教程草案

共用规则见[原生工具修订草案索引](../PULSARA_NATIVE_TOOL_CONTRACTS_AND_TUTORIAL_DRAFT.zh.md)第 1、2 章。记忆对象和 CRUD、范围、并发及隐式召回以[记忆 CRUD hardcut 草案](../PULSARA_MEMORY_CRUD_HARD_CUT_DRAFT.zh.md)为业务基础；完整保存与原生分页沿[统一交付草案](../PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)，存储归属沿[有序内容数据库草案](../PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md)。

状态：当前实现采用以下三个工具的参数、返回字段、类型和失败语义。本族只提供 ROOT 原生工具，不提供 JS API。本文件的 TypeScript 用于描述合同，不表示存在脚本调用入口。

## 1. remember：创建记忆

### 1.1 用途与范围

创建一个独立记忆条目，允许内容重复，每次成功创建取得新的 MEMORY Target。调用只填写可创建字段，Target、recorded_at、updated_at 由 runtime 生成。

范围为 GLOBAL 或 CURRENT_PROJECT。普通项目会话可保存到全局或当前工作区；快速会话只允许 GLOBAL。当前工作区不随终端 cd 改变。访问边界、用户禁用语义和写入权限沿共用业务 owner 检查。

### 1.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| context_kind | GLOBAL／CURRENT_PROJECT | 必填 | 记忆保存范围 |
| kind | USER_PROFILE／RESPONSE_PREFERENCE／FACT | 必填 | 创建后固定的类别 |
| title | string | 必填 | 创建后固定的非空单行标题，规范化后最多 128 个 Unicode 字符 |
| statement | string | 普通记忆必填；回答偏好可省略 | USER_PROFILE／FACT 的完整正文，规范化后非空且最多 8,000 个 Unicode 字符；回答偏好无论省略或提供文字，写库时都归一为空字符串 |

只接受上述四个参数，不接收 target、时间、based_on、context_target 或其他字段。偏好先置空，再应用普通正文校验；规范化、字符计量和数据库保证沿根目录草案。

```typescript
type MemoryContextKind = "GLOBAL" | "CURRENT_PROJECT";
type MemoryKind = "USER_PROFILE" | "RESPONSE_PREFERENCE" | "FACT";
type MemoryTarget = {kind: "MEMORY"; memory_id: string};

interface RememberArgs {
  context_kind: MemoryContextKind;
  kind: MemoryKind;
  title: string;
  statement?: string;
}
```

统一使用一个参数对象。普通记忆的 statement 必填规则由参数校验执行；回答偏好可省略 statement，提供时仍按数据库规则置空。

### 1.3 原生调用示例

普通记忆：

```text
工具：remember
参数：
  context_kind: CURRENT_PROJECT
  kind: FACT
  title: "项目 Python 版本"
  statement: "项目运行环境为 Python 3.12。"
```

回答偏好在 title 中表达完整内容：

```text
工具：remember
参数：
  context_kind: GLOBAL
  kind: RESPONSE_PREFERENCE
  title: "解释复杂问题时先给结论，再提供具体例子"
```

### 1.4 返回字段与类型

| 字段 | 成功时的内容 |
| --- | --- |
| target | 新创建条目的 MEMORY Target |
| meta.status | SUCCESS；失败及取消按第 1.7 节处理 |
| meta.updated_at | 新记忆实际更新时间，完整保留微秒，供 runtime 接续并发核对 |
| content | 简短保存说明的 text 项，随后是最多三条相关记忆的完整 object 项 |

不回传新保存记忆的全文，不重复返回其 kind、范围或标题来复述调用参数。相关候选完整返回，复用根目录的记忆对象形状，不另套 related_memories 数组，不添加数量、关系、inspect 或固定提醒布尔值。

```typescript
interface MemoryRecord {
  target: MemoryTarget;
  context_kind: MemoryContextKind;
  kind: MemoryKind;
  title: string;
  statement: string;
  recorded_at: string;
  updated_at: string;
}

interface MemoryTextPart {
  type: "text";
  value: string;
}

interface MemoryRecordPart {
  type: "object";
  value: MemoryRecord;
}

interface RememberSuccess {
  target: MemoryTarget;
  meta: {
    status: "SUCCESS";
    updated_at: string;
  };
  content: (MemoryTextPart | MemoryRecordPart)[];
}

interface RememberFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: MemoryTextPart[];
}

type RememberResult = RememberSuccess | RememberFailure;
```

RememberFailure 描述实际未保存时产生的失败业务值，不提供假 Target 或更新时间。没有产生业务值的早期失败，以及保存／交付本身失败，沿共用错误路径报告，不为满足类型伪造结果。MemoryRecord 中回答偏好的 statement 始终为 ""。

模型无需复制 meta.updated_at；本 session 的完整成功结果保存后，runtime 自动取得新条目的旧时间依据，不等待分页交付完成。新条目身份和时间不因后续查询或原生补页重新生成。

### 1.5 相关候选

相关候选最多三条，返回实际找到的数量，不强凑数量，不包含新创建条目自身。每条候选的 value 都是完整 MemoryRecord；它们只是可供模型阅读和整理的相关记忆，不自动建立依赖、替代或冲突关系。

候选存在时，content 先写“已保存记忆。以下是相关记忆，可按需整理。”，随后按检索结果顺序返回 object 项。没有候选时，content 只保留“已保存记忆。”这条文字项，不额外输出空候选数组或教程。

说明后紧跟候选时，文字项自身带上必要的末尾换行；普通 text 项不依赖 runtime 自动增加分隔符。

创建成功但相关记忆检索未完成时，仍为 SUCCESS，content 说明“已保存记忆。相关记忆检索未完成。”；已经实际取得的候选可完整保留。不因为推荐失败重做创建或把创建成功改为创建失败，不恢复冗长检索诊断字段。

### 1.6 原生交付示例

例如保存“项目 Python 版本”成功，找到一条相关记忆，完整业务值为：

```json
{
  "target": {"kind": "MEMORY", "memory_id": "memory:<新记忆ID>"},
  "meta": {
    "status": "SUCCESS",
    "updated_at": "2026-11-13T02:00:00.123456Z"
  },
  "content": [
    {"type": "text", "value": "已保存记忆。以下是相关记忆，可按需整理。\n"},
    {
      "type": "object",
      "value": {
        "target": {"kind": "MEMORY", "memory_id": "memory:<相关记忆ID>"},
        "context_kind": "CURRENT_PROJECT",
        "kind": "FACT",
        "title": "项目 Python 环境",
        "statement": "项目使用 uv 管理 Python 环境。",
        "recorded_at": "2026-11-12T01:00:00Z",
        "updated_at": "2026-11-12T01:00:00Z"
      }
    }
  ]
}
```

这表示保存的完整业务值，不是 JS 调用返回示例。可完整装入一页时，原生投影为：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际耗时>
target: {"kind":"MEMORY","memory_id":"memory:<新记忆ID>"}

meta:
  status: SUCCESS
  updated_at: "2026-11-13T02:00:00.123456Z"

content:
  已保存记忆。以下是相关记忆，可按需整理。
  {
    "target": {
      "kind": "MEMORY",
      "memory_id": "memory:<相关记忆ID>"
    },
    "context_kind": "CURRENT_PROJECT",
    "kind": "FACT",
    "title": "项目 Python 环境",
    "statement": "项目使用 uv 管理 Python 环境。",
    "recorded_at": "2026-11-12T01:00:00Z",
    "updated_at": "2026-11-12T01:00:00Z"
  }

tool_result: <本次完整结果的导出 Target>
next: null
```

观察时间和实际微秒耗时在消息顶层，meta.updated_at 是记忆属性，不把二者互换；未测得耗时不伪造为 0。示例身份和时间均为说明占位。

没有相关候选时，原生仍保留新 target、meta.status、meta.updated_at，content 改为“已保存记忆。”。推荐读取失败但创建成功时，可为：

```text
observed_at_utc: <实际观察时间>
target: {"kind":"MEMORY","memory_id":"memory:<新记忆ID>"}

meta:
  status: SUCCESS
  updated_at: "2026-11-13T02:00:00.123456Z"

content:
  已保存记忆。相关记忆检索未完成。

tool_result: <本次完整结果的导出 Target>
next: null
```

### 1.7 失败、取消与分页

| 情况 | 状态与返回 |
| --- | --- |
| 实际创建成功，候选检索成功或没有匹配 | SUCCESS，实际 Target 和 updated_at，以及相应说明和候选 |
| 实际创建成功，候选检索未完成 | SUCCESS，保留实际 Target 和 updated_at，content 说明推荐未完成及实际已取得候选 |
| 参数、范围、权限或实际写入失败，确认未保存 | ERROR，content 给出简短原因，不伪造 Target 或更新时间 |
| 创建前取消或结算确认未保存的取消 | CANCELLED，content 说明实际取消情况，不伪造保存事实 |

例如快速会话尝试创建当前工作区记忆，失败业务值为：

```json
{
  "meta": {"status": "ERROR"},
  "content": [
    {"type": "text", "value": "快速会话不能保存当前工作区记忆，请使用 GLOBAL。"}
  ]
}
```

同一错误的原生消息可为：

```text
observed_at_utc: <实际观察时间>

meta:
  status: ERROR

content:
  快速会话不能保存当前工作区记忆，请使用 GLOBAL。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

取消采用同样的 meta/content 形状，status 为 CANCELLED，说明实际取消原因。创建及结果结算沿既有事务和效果结算规则；不能仅因取消到达就把已确认保存说成未保存。保存或交付失败必须保留已知效果或不确定性，不自动重做创建，也不提供假装可读的结果引用。

本次完整业务值一次保存，原生按统一 12k 规则顺序分页；相关记忆 object 按共同 JSON 投影交付。NEXT 只补读这次结果，不重新创建记忆、不重新查候选、不刷新其中的时间。正常完整交付时 next:null 只表示结果交付完，不覆盖实际业务状态。不因展示余量截掉候选正文或提前拒绝创建。

## 2. query_memory：关键词查询或 Target 精确读取

### 2.1 用途与范围

查询持久记忆，直接返回当前完整记忆对象。搜索使用 title 与 statement 的统一检索文本，覆盖三个 kind，不提供类别优先及类型放宽参数。历史 QA／QAQ 由会话工具查询，不混入本工具结果；隐式召回仍按根目录草案进行混合选择。

可读范围沿当前记忆域及全局／当前工作区；快速会话只读全局。已知 Target 不扩大读取范围，也不要求先执行关键词搜索。

### 2.2 最简参数表与互斥分支

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | MEMORY Target | Target 分支必填 | 精确读取指定条目的当前对象；放在参数表首位 |
| query | string | 关键词分支必填 | 非空白关键词或自然语言需求 |
| limit | 正整数 | 关键词分支可选，默认 5 | 本次最多选择多少条相关记忆，不保证填满，不是原生交付页长 |

两个分支严格互斥：

- Target 分支只填写 target，不同时填写 query、limit。
- 关键词分支填写 query 和可选 limit，不填写 target。

不接受空对象、null、布尔值 limit 或额外字段；limit 的具体数值由模型选择，本轮不另定固定最大条数。query 要求非空白，不另设字符或 token 长度 cap；真实资源或服务限制如实报告。

```typescript
type QueryMemoryArgs =
  | {
      target: MemoryTarget;
      query?: never;
      limit?: never;
    }
  | {
      target?: never;
      query: string;
      limit?: number;
    };
```

### 2.3 原生调用示例

精确读取：

```text
工具：query_memory
参数：
  target: {"kind":"MEMORY","memory_id":"memory:<记忆ID>"}
```

关键词查询：

```text
工具：query_memory
参数：
  query: "项目 Python 版本"
  limit: 5
```

### 2.4 统一返回字段与类型

关键词查询与 Target 精确读取共用 meta/content 结构，没有顶层业务 target：

| 字段 | 含义 |
| --- | --- |
| meta.status | SUCCESS／ERROR／CANCELLED，沿共用调用状态规则 |
| content | 命中记忆使用 MemoryRecordPart；无匹配说明、失败及取消原因使用 MemoryTextPart |

两种分支的 object.value 完全相同，复用第 1.4 节的完整 MemoryRecord，包含 target、context_kind、kind、title、statement、recorded_at、updated_at。精确读取成功时一项，关键词查询可为多项。查询记录中的 Target 不再提升到顶层，也不使用重复的 memories 数组。

```typescript
interface QueryMemorySuccess {
  meta: {status: "SUCCESS"};
  content: (MemoryTextPart | MemoryRecordPart)[];
}

interface QueryMemoryFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: MemoryTextPart[];
}

type QueryMemoryResult = QueryMemorySuccess | QueryMemoryFailure;
```

没有产生业务值的早期失败沿共用错误路径报告，不为满足上述类型伪造记忆对象。正常有匹配时直接返回 object 项，不额外添加“查到若干条”等文字。回答偏好也使用同一对象，statement 为 ""。

meta 只保留调用状态，不重复每条记忆的 updated_at。query、limit、调用分支、命中／显示／省略数量、匹配分数、检索通道、重排诊断均不返回。删除详情调用指引、固定长度摘录和截断标记，直接交付完整对象；不恢复关系字段或其他固定提醒包装。

### 2.5 两种查询的原生结果示例

关键词 query:"项目 Python"、limit:5 找到两条记忆，且结果可完整装入一页时：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际查询耗时>

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "MEMORY",
      "memory_id": "memory:<A>"
    },
    "context_kind": "CURRENT_PROJECT",
    "kind": "FACT",
    "title": "项目 Python 版本",
    "statement": "项目运行环境为 Python 3.12。",
    "recorded_at": "2026-11-12T01:00:00Z",
    "updated_at": "2026-11-13T02:00:00.123456Z"
  }
  {
    "target": {
      "kind": "MEMORY",
      "memory_id": "memory:<B>"
    },
    "context_kind": "CURRENT_PROJECT",
    "kind": "FACT",
    "title": "项目 Python 环境",
    "statement": "项目使用 uv 管理 Python 环境。",
    "recorded_at": "2026-11-12T02:00:00Z",
    "updated_at": "2026-11-12T02:00:00Z"
  }

tool_result: <完整查询结果的导出 Target>
next: null
```

保存的 content 对应两个 type:object/value:MemoryRecord 项，不先把它们存成展示文字。原生对每个 value 采用共同的两空格缩进 JSON 投影，不特判内部 target。

精确读取 MEMORY Target A 的正常结果使用同样的形状，只有一条 object：

```text
observed_at_utc: <实际观察时间>
observation_duration_microseconds: <实际读取耗时>

meta:
  status: SUCCESS

content:
  {
    "target": {
      "kind": "MEMORY",
      "memory_id": "memory:<A>"
    },
    "context_kind": "CURRENT_PROJECT",
    "kind": "FACT",
    "title": "项目 Python 版本",
    "statement": "项目运行环境为 Python 3.12。",
    "recorded_at": "2026-11-12T01:00:00Z",
    "updated_at": "2026-11-13T02:00:00.123456Z"
  }

tool_result: <完整读取结果的导出 Target>
next: null
```

观察信息仍在消息顶层，未测得微秒耗时不伪造为 0。上述身份及时间为示例值，不是实际查询记录。即使精确读取，也不在顶层重复 A 的业务 Target。

### 2.6 空结果、失败与取消

| 情况 | 状态 | content |
| --- | --- | --- |
| 关键词正常查询完成，无匹配 | SUCCESS | 一条文字项“没有找到匹配的记忆。” |
| 指定 Target 不存在或不可读 | ERROR | 一条文字项“该记忆不存在或当前不可读。” |
| 实际查询／读取失败 | ERROR | 简短实际原因，不描述为没有匹配 |
| 调用取消 | CANCELLED | 简短实际取消原因，不冒充正常完成 |

关键词无匹配的原生结果：

```text
observed_at_utc: <实际观察时间>

meta:
  status: SUCCESS

content:
  没有找到匹配的记忆。

tool_result: <本次完整结果的导出 Target>
next: null
```

指定 Target 不存在或不可读时：

```text
observed_at_utc: <实际观察时间>

meta:
  status: ERROR

content:
  该记忆不存在或当前不可读。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

实际读取失败时，可以使用“无法读取记忆：数据库读取失败。”；取消时使用 CANCELLED 和真实原因。失败值不伪造记忆属性、更新时间、空记忆对象或业务 Target，不增加 meta.error、_hint 或另一组错误分类。

上述完整结果 Target 只在实际结果成功保存、可读时提供。正常交付完错误结果的 next:null 不代表记忆读取成功；保存或交付失败仍按共用规则处理，不自动重查记忆。

### 2.7 冻结结果、分页与并发依据

两种查询都完整保存本次结果，再按原生 12k 分页；文字及 object 的 JSON 投影可以跨页，不要求一条记忆完整装入一页。NEXT 只读本次冻结结果，不重跑搜索、不重新读取当前对象、不扩大 limit。

当前数据库随后修改或删除记忆，不改变这次已保存记录及其中的时间。模型需要最新内容时，再调用 query_memory；补页不把旧记录刷新成新值。

updated_at 只在每条完整记忆对象中保留，模型无需复制为 manage_memory 参数。本 session 的完整查询结果保存后，其中所有记忆对象均可提供后续并发核对的旧时间，包括尚未展示的对象或跨页对象；不要求累计读完对象，也不建立独立已读资格或持久注册表。并发依据可用不代表模型已看完内容，实际交付范围仍按分页规则记录。

## 3. manage_memory：修改或删除记忆

### 3.1 用途与范围

按 MEMORY Target 操作现有条目。edit 可修改 statement、保存范围中的一项或两项；title 和 kind 创建后固定。delete 只删除指定条目及其派生索引，不传播删除，不恢复其他记忆。

实际对象及目标范围仍须获准操作；快速会话不能把全局记忆改存到当前目录。CURRENT_PROJECT 表示会话所属工作区，不是终端当前目录。

### 3.2 最简参数表

| 参数 | 类型 | 必填与默认 | 明确含义 |
| --- | --- | --- | --- |
| target | MEMORY Target | 必填 | 要修改或删除的条目，放在参数表首位 |
| action | edit／delete | 必填 | 修改或删除 |
| statement | string | edit 时可选 | 替换完整正文；普通记忆仍须满足正文规则，偏好写库时归一为 "" |
| context_kind | GLOBAL／CURRENT_PROJECT | edit 时可选 | 修改保存范围；未提供则保持原范围 |

edit 至少填写 statement 或 context_kind 之一；未提供的字段保持原值。delete 只填写 target、action，不接受 statement 或 context_kind。两个动作都不接受 title、kind、expected_updated_at 或其他字段。

```typescript
type ManageMemoryArgs =
  | ({target: MemoryTarget; action: "edit"} & (
      | {statement: string; context_kind?: MemoryContextKind}
      | {statement?: never; context_kind: MemoryContextKind}
    ))
  | {
      target: MemoryTarget;
      action: "delete";
      statement?: never;
      context_kind?: never;
    };
```

### 3.3 runtime 自动取得并发条件

模型只提供本节公开参数，不复制时间，不填写并发条件。runtime 从本 session 已完整保存的工具结果取得旧 updated_at：query_memory 的记忆对象、remember 的新条目及相关候选，以及 manage_memory 修改成功或无变化的结果均可提供依据；原有 runtime observation 中的记忆快照也继续可用。工具结果是否已经分页展示、对象是否已经读完，不影响并发依据；修改和删除都在数据库事务内将旧时间与当前业务行原子比对。

没有对应旧快照时不执行操作，提示先通过 query_memory 查询该记忆。数据库当前时间不能临时读来替代旧快照；其他 session 的结果或观察，以及未进入已保存工具结果的内部检索候选也不能代用。当前数据库值已变化时明确报告冲突，不自动查询再重做操作，不按页维护阅读资格，不增加持久已读登记、revision 或回执。

成功实际修改后，时间严格递增并完整保留微秒，身份和创建时间保持；无变化时不更新时间、不触发修改通知。修改成功和无变化都返回顶层 target 及 meta.updated_at，便于 runtime 接续后续操作。无变化也须通过同样的旧快照及并发核对，不能绕过冲突检查。

### 3.4 原生调用示例

修改正文，模型不填写更新时间：

```text
工具：manage_memory
参数：
  target: {"kind":"MEMORY","memory_id":"memory:<记忆ID>"}
  action: edit
  statement: "项目运行环境已升级到 Python 3.12。"
```

修改保存范围：

```text
工具：manage_memory
参数：
  target: {"kind":"MEMORY","memory_id":"memory:<记忆ID>"}
  action: edit
  context_kind: GLOBAL
```

删除：

```text
工具：manage_memory
参数：
  target: {"kind":"MEMORY","memory_id":"memory:<记忆ID>"}
  action: delete
```

### 3.5 返回字段与类型

| 情况 | 顶层 target | meta | content |
| --- | --- | --- | --- |
| 修改成功 | 同一记忆的 MEMORY Target | status:SUCCESS、实际 updated_at | “已修改记忆。” |
| 无变化 | 同一记忆的 MEMORY Target | status:SUCCESS、原 updated_at | “记忆未发生变化。” |
| 删除成功 | 不返回 | status:SUCCESS | “已删除记忆。” |
| 失败或取消 | 不返回 | status:ERROR／CANCELLED | 简短实际原因及必要的已知效果说明 |

content 均为 MemoryTextPart 数组。修改成功不回传完整正文、修改前后值、字段清单或相关候选；无变化不新增状态或 changed 布尔字段。删除成功不返回已删除对象副本、更新时间、删除时间、数量、关系影响或 deleted 布尔字段。

```typescript
interface ManageMemoryEditSuccess {
  target: MemoryTarget;
  meta: {
    status: "SUCCESS";
    updated_at: string;
  };
  content: MemoryTextPart[];
}

interface ManageMemoryDeleteSuccess {
  meta: {status: "SUCCESS"};
  content: MemoryTextPart[];
}

interface ManageMemoryFailure {
  meta: {status: "ERROR" | "CANCELLED"};
  content: MemoryTextPart[];
}

type ManageMemoryResult =
  | ManageMemoryEditSuccess
  | ManageMemoryDeleteSuccess
  | ManageMemoryFailure;
```

ManageMemoryEditSuccess 同时描述实际修改和无变化；两者都保留同一 Target，时间分别为修改后的实际值和未变化的原值。模型不复制 updated_at，本 session 的完整结果保存后由 runtime 自动接续并发依据，不等待分页交付完成。需要当前完整对象时，另行调用 query_memory。

### 3.6 原生结果示例

以下均示意结果已成功保存且一页完整交付；身份及时间为示例。观察时间和已测得的微秒耗时沿共用规则在消息顶层并列，不写入 meta，下面省略。

修改正文或保存范围成功：

```text
target: {"kind":"MEMORY","memory_id":"memory:<记忆ID>"}

meta:
  status: SUCCESS
  updated_at: "2026-11-13T03:00:00.123456Z"

content:
  已修改记忆。

tool_result: <本次完整结果的导出 Target>
next: null
```

提交内容经规范化后与当前值完全相同，且通过并发核对，返回原更新时间：

```text
target: {"kind":"MEMORY","memory_id":"memory:<记忆ID>"}

meta:
  status: SUCCESS
  updated_at: "2026-11-13T02:00:00.123456Z"

content:
  记忆未发生变化。

tool_result: <本次完整结果的导出 Target>
next: null
```

删除成功：

```text
meta:
  status: SUCCESS

content:
  已删除记忆。

tool_result: <本次完整结果的导出 Target>
next: null
```

修改或删除遇到并发冲突：

```text
meta:
  status: ERROR

content:
  该记忆在你上次读取后已发生变化，本次操作未执行。
  请先用 query_memory 查询该记忆，再决定是否操作。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

没有可用旧快照，修改或删除均不执行：

```text
meta:
  status: ERROR

content:
  尚未取得该记忆的可用旧快照，本次操作未执行。
  请先用 query_memory 查询该记忆。

tool_result: <本次完整错误结果的导出 Target>
next: null
```

多行说明在文字项的 value 中保留必要换行，不依赖 runtime 自动插入分隔符。

### 3.7 失败、取消与结果结算

缺少旧快照、并发冲突、条目不存在或不可操作、参数错误及实际执行失败，均用 ERROR 和简短说明；取消用 CANCELLED 和真实取消原因。不增加 meta.error、_hint、错误枚举或另一组状态。并发冲突不返回数据库最新时间或最新正文，不自动查询并重做操作。

只有确认未执行时才说明“本次操作未执行”。进入不可安全中断的提交后等待实际结果：确认完成且结果正常产生时按 SUCCESS 返回；执行或后续保存、交付失败时如实说明已知效果，仍无法确认则说明状态未确认，不把取消到达当作未执行证明，不自动重做修改或删除。失败值不伪造 Target、更新时间或完整记忆对象；没有产生业务值的早期失败，以及保存／交付本身失败，沿共用错误路径处理。

完整结果 Target 仅在实际结果成功保存且可读时提供。原生按统一 12k 规则顺序分页，NEXT 只补读本次冻结结果，不重新执行操作、刷新时间或重查当前记忆。错误结果已正常交付完的 next:null 不表示业务操作成功。

工具结果和隐式召回 notices 由各自路径表达，不把删除结果当成额外持久通知实体，也不因 CRUD 结果改写已安装的 provider 输入。

## 4. 共同约束与实施接点

上述参数是单一现行合同。公开工具族为 remember、query_memory、manage_memory；删除独立 get_memory、旧 search_memory 及 mark_memory_relation，不提供兼容别名或关系入口。内存中的瞬时检索与模型结果复用根目录草案规定的底层路径；正常工具返回不使用隐式召回的增量 i／j 去重，仍完整保存本次业务结果。

隐式召回的 MEMORY_RECALL observation 沿[Runtime Observation 修订草案](../PULSARA_RUNTIME_OBSERVATION_REVISION_DRAFT.zh.md)使用 source／body 外壳，body 为 text＋可选 target 对象数组，不公开 presence。偏好刷新、混合召回和旧召回记忆失效核对，只随新采用的 ROOT 真人消息／主动 steer 检查并发送，普通工具续跑不刷新。必要读取失败时用文字说明整条记忆信息暂不可用，不交付部分增量，也不推进 P／i；具体记忆增量规则沿根目录草案。本文三个显式工具仍按工具合同读取当前业务状态，使用各自的 meta.status／content 返回值。

MEMORY_WRITE_HINT 另按[Runtime Observation 草案第 10 节](../PULSARA_RUNTIME_OBSERVATION_REVISION_DRAFT.zh.md#10-记忆写入提醒-memory_write_hint)发送一条 text 建议，提醒模型自行判断是否保存或更新；它不自动调用工具，不改变本文参数、权限和结果合同。

完整结果和原生交付沿索引共用规则：工具状态只表达一次，错误说明进入文字 content；tool_result 指向完整工具结果，NEXT 只补读已保存内容。时间观察信息在原生消息顶层，不混入记忆业务对象的 recorded_at／updated_at。具体工具结果使用本文规定的类型，不恢复旧业务 JSON 或关系包装。

三个工具的返回值及原生示例见第 1 至 3 章。当前工具、共享 CRUD、数据库和隐式召回已接通；完整应用的验收与恢复使用条件以实施计划为准。

当前生产接点：

- [工具目录](../src/pulsara_agent/capability/builtin_catalog.py)：公开声明、权限分类及 ROOT 原生边界。
- [记忆工具](../src/pulsara_agent/conversation_kernel/memory_tools.py)：当前创建、查询及记忆工具执行 owner。
- [记忆事务](../src/pulsara_agent/conversation_kernel/_repository/memory.py)：创建与结果结算、旧去重与关系路径。
- [页面管理](../src/pulsara_agent/conversation_kernel/_repository/memory_management.py)：现有更新时间比对、修改与删除。
- [记忆检索](../src/pulsara_agent/conversation_kernel/memory/recall.py)：关键词、向量及精确读取。
- [MEMORY Target](../src/pulsara_agent/memory/targets.py)和[范围](../src/pulsara_agent/memory/scope.py)：身份与可读范围校验。
